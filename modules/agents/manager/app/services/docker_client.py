# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Docker client wrapper for agent container lifecycle management."""

import logging
import os
import re

import docker

# Absolute, matching catalog.py's sibling imports — NOT `from . import`: this
# module is also loaded standalone by file path (test_oom_visibility loads it
# via spec_from_file_location), where a relative import has no parent package.
from app.services import egress

logger = logging.getLogger(__name__)


def parse_oom_kills(text) -> int | None:
    """Parse a cgroup memory-accounting blob into an OOM-kill count (#238).

    Accepts either form:
      * cgroup v2 `memory.events` — key/value lines, we want `oom_kill N`
        (NOT `oom_group_kill`, and NOT `oom`, which counts times the limit was
        hit, including hits that reclaimed rather than killed);
      * cgroup v1 `memory.failcnt` — a bare integer.

    Returns ``None`` for empty/garbage input. "Unknown" must never be rendered
    as 0, which would claim the agent has never been OOM-killed.
    """
    if not text or not str(text).strip():
        return None
    body = str(text)
    m = re.search(r'^oom_kill[ \t]+(\d+)\s*$', body, re.MULTILINE)
    if m:
        return int(m.group(1))
    stripped = body.strip()
    if re.fullmatch(r'\d+', stripped):
        return int(stripped)
    return None



#: #692 — one cgroup parent for EVERY agent container. Per-container limits
#: cannot stop N compliant containers from jointly draining the host (the
#: 30-GB-box overcommit class); the slice's memory.max can. The slice unit
#: (with MemoryMax) is installed host-side by scripts/harden-host.sh; docker
#: creates the slice on demand either way, so setting the parent is safe
#: even before the unit exists (it is just unlimited until then).
AGENTS_CGROUP_PARENT = "razzfazz-agents.slice"

#: #3060 — /dev/shm of every agent container (Docker's default is 64 MiB).
#: One value for all types and tiers: it is a ceiling, not a reservation —
#: shm pages count against the container's own mem_limit as they are used, so
#: a small-tier agent cannot get more memory through it.
AGENT_SHM_SIZE = "1g"


class AgentDockerClient:
    def __init__(self, base_url: str, network: str):
        self._client = docker.DockerClient(base_url=base_url)
        # #692: a ".slice" cgroup parent is only valid under the systemd
        # cgroup driver (Ubuntu default). Probe ONCE; on cgroupfs we skip the
        # parent instead of failing every container create.
        try:
            self._cgroup_driver = (self._client.info() or {}).get("CgroupDriver", "")
        except Exception:
            self._cgroup_driver = ""
        self._network = network
        self._ensure_network()

    def _host_gateway_ip(self) -> str:
        """Return the docker default-bridge gateway IP (usually 172.17.0.1).

        #36: the `extra_hosts={'host.docker.internal': 'host-gateway'}` magic
        keyword is resolved by the Docker DAEMON at container-create time. When
        agent containers are created THROUGH docker-socket-proxy, the daemon
        cannot map `host-gateway` to a real address at that moment (the
        container is created on agent-network only; the `bridge` attach happens
        AFTER create), so it writes the literal string `invalid IP` into the
        container's /etc/hosts. `socket.gethostbyname("host.docker.internal")`
        then raises gaierror (verified on 0.91). Resolving the real bridge
        gateway IP here and passing it as a concrete extra_host makes the
        mapping deterministic and proxy-independent.
        """
        try:
            b = self._client.networks.get('bridge')
            for cfg in (b.attrs.get('IPAM', {}).get('Config') or []):
                gw = cfg.get('Gateway')
                if gw:
                    return gw
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not resolve docker bridge gateway IP: {e}")
        # docker0 default; correct on every stock Docker install we ship on.
        return '172.17.0.1'

    def _ensure_network(self):
        """Create the agent network(s) if they don't exist."""
        try:
            self._client.networks.get(self._network)
        except docker.errors.NotFound:
            logger.info(f"Creating agent network: {self._network}")
            self._client.networks.create(self._network, driver='bridge')
        # #36 security-review — the egress-controlled `coding-agents` network
        # for sandboxed coding agents. Normally created by core/compose.yml
        # (declared there so whitelisted services can join it), but create it
        # here too so a sandboxed provision never fails on a fresh box where
        # the compose-declared network hasn't materialised yet. Non-internal
        # (keeps internet egress for git/npm/pip + cloud APIs).
        try:
            self._client.networks.get('coding-agents')
        except docker.errors.NotFound:
            logger.info("Creating coding-agents network (egress-controlled sandbox)")
            self._client.networks.create('coding-agents', driver='bridge')
        # W4a (#256): egress-controlled net for the socket-less assistant
        # classes (hermes). Mirrors coding-agents: declared in core/compose.yml
        # so whitelisted services (gpustack, agent-manager) join by name-merge;
        # created here too so a fresh box can provision before compose has
        # materialised it. Non-internal (hermes needs API/MCP egress).
        try:
            self._client.networks.get('agent-assistants')
        except docker.errors.NotFound:
            logger.info("Creating agent-assistants network (egress-controlled)")
            self._client.networks.create('agent-assistants', driver='bridge')

    def create_container(self, name: str, image: str, version: str,
                         environment: dict, volumes: dict,
                         mem_limit: str = '256m', cpu_limit: float = 1.0,
                         docker_socket: bool = False,
                         labels: dict = None,
                         command=None,
                         entrypoint=None,
                         sandbox: bool = False,
                         sandbox_writable: list | None = None,
                         pids_limit: int | None = None,
                         assistant_net: bool = False,
                         coding_net: bool = False,
                         secrets_uid: int | None = None,
                         blocked_hosts=None) -> str:
        """Create a new agent container (stopped). Returns container ID.

        `blocked_hosts`: host names pinned to 0.0.0.0 in the container's
        /etc/hosts (#2717: Claude Code's telemetry intake). Exact names only.

        `secrets_uid`: mount an owner-only tmpfs at /run/rzfz for the owner's
        short-lived OpenBao token (#2476), owned by this uid.

        #36 security-review — `sandbox=True` (the socket-less coding-agent
        types: opencode / gsd-pi / codex / user-defined) applies a hostile-
        container hardening profile. Each such container runs arbitrary
        LLM-driven code and is treated as untrusted:

          * Egress isolation: attached to the `coding-agents` bridge network
            ONLY — NOT the agent-network and NOT razzfazz-stack_default. It
            reaches the whitelisted supporting services (which also join
            `coding-agents`) by container name, plus the internet (the network
            is non-internal), but CANNOT reach postgres / valkey / authentik /
            config-portal / backup or any non-whitelisted module.
          * Breakout prevention: cap_drop ALL (no cap_add), no-new-privileges,
            default seccomp (NOT unconfined), non-root (image USER agent),
            no privileged, no host bind-mounts (only the per-user named
            volumes), read-only root fs with a small writable set (the per-user
            /workspace + /home/agent volumes) + tmpfs /tmp.
          * Resource caps: mem_limit (arg), cpu quota (nano_cpus), pids_limit.

        NOTE deliberately unchanged: hermes / moltis are NOT sandboxed here —
        they have their own trust model; passing sandbox=False keeps their
        exact pre-existing wiring.

        The RAW host docker socket is never bound: it is root on the host.
        `docker_socket=True` is refused (#2661 took it from moltis). Guarded by
        tests/unit/agent-manager/test_2661_no_raw_docker_socket.py.
        """
        image_ref = f"{image}:{version}"
        logger.info(f"Creating container {name} from {image_ref}"
                    f"{' [SANDBOX]' if sandbox else ''}")

        # #2664: no container gets the raw host socket, whatever its catalog
        # row says. A stale DB row with requires_docker_socket=true fails the
        # create loudly instead of handing out host root.
        if docker_socket:
            raise ValueError(
                f"refusing to create {name}: the raw Docker socket is never "
                "granted to an agent container (#2664)")

        volume_binds = {}
        for vol_name, mount_path in volumes.items():
            volume_binds[vol_name] = {'bind': mount_path, 'mode': 'rw'}

        # The box's trust bundle, on EVERY box (#2472 BC5). certs/caddy-ca.pem
        # is always the system CAs plus the box's own root
        # (scripts/lib.sh::ensure_oidc_ca_superset; Open WebUI and Gitea mount
        # it on every box), so pointing the agent's TLS stacks at it keeps
        # public issuers valid and adds the box's internal CA — without it an
        # agent cannot reach its own https://<id>.mcp.<domain> MCP servers on a
        # TLS_MODE=internal box. Behind a corporate proxy the same file also
        # carries the proxy CA (#281).
        # CA env + mount go TOGETHER (empty-dir x509 trap). The bind SOURCE
        # MUST be a HOST path — the daemon behind docker-socket-proxy resolves
        # it on the host fs, NOT inside this manager container. Mirrors the
        # {{STACK_HOST_PATH}} host_path convention (catalog.py).
        _inject = {}
        _stack_host = os.environ.get('STACK_HOST_PATH', '')
        if _stack_host:
            _ca_in = os.environ.get('SSL_CERT_FILE') or '/certs/caddy-ca.pem'
            volume_binds['%s/certs/caddy-ca.pem' % _stack_host] = {
                'bind': _ca_in, 'mode': 'ro',
            }
            for _k in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE',
                       'GIT_SSL_CAINFO', 'PIP_CERT', 'NODE_EXTRA_CA_CERTS'):
                _inject[_k] = _ca_in

        # #281: corporate-proxy for socket-spawned containers. The compose
        # corporate-proxy overlay only reaches compose SERVICES; per-user agent
        # containers created here go through docker-socket-proxy and get nothing.
        # When the box is proxied, propagate the SAME proxy routing the
        # manager itself received (agent-manager is in the overlay egress
        # allow-list, so these vars are in the manager's own env).
        if os.environ.get('RAZZFAZZ_CORPORATE_PROXY') == '1':
            _inject.update({
                'HTTP_PROXY':  os.environ.get('HTTP_PROXY', ''),
                'HTTPS_PROXY': os.environ.get('HTTPS_PROXY', ''),
                'http_proxy':  os.environ.get('http_proxy', os.environ.get('HTTP_PROXY', '')),
                'https_proxy': os.environ.get('https_proxy', os.environ.get('HTTPS_PROXY', '')),
                'NO_PROXY':    os.environ.get('NO_PROXY', ''),
                'no_proxy':    os.environ.get('no_proxy', os.environ.get('NO_PROXY', '')),
            })
            if not _stack_host:
                logger.warning(
                    "RAZZFAZZ_CORPORATE_PROXY=1 but STACK_HOST_PATH unset — "
                    "skipping CA bind for %s (proxy env still injected)", name)
        # Catalog-provided env wins; only fill in vars we actually have.
        environment = {**{_k: _v for _k, _v in _inject.items() if _v},
                       **environment}

        # #959 W4 / #802 — default-deny egress for the FENCED classes. Opt-in:
        # `egress.proxy_env()` is empty under the `open` default (and under a
        # corporate-proxy conflict, which the block above already served), so a
        # box that has not opted in gets a byte-identical environment to today.
        # Only the fenced classes are pointed at the proxy — an unfenced agent
        # (paperclip) lives on the stack default net, where the
        # egress proxy is not, so the var would break it for no security gain.
        # Same precedence as #281: catalog-provided env wins.
        if sandbox or coding_net or assistant_net:
            _conflict = egress.policy_conflict(os.environ)
            if _conflict:
                # Refused, not silently downgraded: an operator who asked for a
                # fence and did not get one must see why in the manager log.
                logger.warning("%s: %s", name, _conflict)
            else:
                _egress = egress.proxy_env(os.environ)
                if _egress:
                    logger.info(
                        "Egress allowlist policy active — %s routed via %s",
                        name, egress.PROXY_SERVICE)
                    environment = {**_egress, **environment}

        nano_cpus = int(cpu_limit * 1e9)

        # Sandboxed coding agents attach to the dedicated egress-controlled
        # `coding-agents` network at CREATE time (so they never briefly sit on
        # the stack default net) and get NO other network below.
        # W4a (#256): socket-less assistant classes live on `agent-assistants`
        # ONLY — the same fence pattern as the coding sandbox, WITHOUT the
        # hostile-container hardening profile (their own trust model applies,
        # unchanged).
        # #256: `coding_net` is the SAME fence without the hardening — the
        # pre-split `coding-tools` type (see CODING_NET_TYPES).
        if sandbox or coding_net:
            initial_network = 'coding-agents'
        elif assistant_net:
            initial_network = 'agent-assistants'
        else:
            initial_network = self._network

        create_kwargs = dict(
            image=image_ref,
            name=name,
            environment=environment,
            volumes=volume_binds,
            mem_limit=mem_limit,
            nano_cpus=nano_cpus,
            network=initial_network,
            labels=labels or {},
            restart_policy={'Name': 'unless-stopped'},
            detach=True,
            # #2664: every agent container runs with no-new-privileges. The
            # one exemption was the raw-socket holder, and there is none.
            security_opt=['no-new-privileges:true'],
            # #3060: no core dumps. The host's kernel.core_pattern is a bare
            # `core`, so a crashing process writes a full core into its cwd —
            # the user's persistent /workspace volume. A Playwright Chromium
            # crash burst on one box wrote 1167 of them (33 GB allocated, 732 TB
            # apparent) in six hours, and every nightly backup walked them.
            # hard=0 too: a process may raise its soft limit only up to the
            # hard one, so wanting cores stays the admin's choice.
            ulimits=[docker.types.Ulimit(name='core', soft=0, hard=0)],
            # #3060: Docker's default /dev/shm is 64 MiB, the documented cause
            # of headless Chromium crashes in containers (Playwright's Docker
            # guidance: --ipc=host or a large shm). A size cap only: the
            # tmpfs is charged to the container's memory as it is used.
            shm_size=AGENT_SHM_SIZE,
        )
        # #692: put every agent under the shared slice so a joint build/run
        # storm OOMs INSIDE the agent budget (kills hit the agents' own
        # processes) instead of freezing the whole host — per-container
        # limits held on 0.208 and the box still died globally.
        # getattr: several test harnesses build this client via __new__
        # (bypassing __init__) — treat an unprobed driver as unknown/skip.
        if getattr(self, "_cgroup_driver", "") == "systemd":
            create_kwargs['cgroup_parent'] = AGENTS_CGROUP_PARENT
        if sandbox:
            # ── Hostile-container hardening ──────────────────────────────────
            create_kwargs['cap_drop'] = ['ALL']            # drop all Linux caps
            create_kwargs['security_opt'] = ['no-new-privileges:true']  # + default seccomp
            create_kwargs['read_only'] = True              # read-only root fs
            create_kwargs['pids_limit'] = pids_limit or 512
            # #215: reaping init as PID 1. The tmux server,
            # the backgrounded local-model shim, and per-session PTY shells all
            # leave orphaned/double-forked children that reparent to whatever is
            # PID 1 and become permanent zombies there. With no reaper they
            # accumulate until pids_limit (512) is hit → the container can no
            # longer fork() → the agent dies (looks like OOM but is PID
            # exhaustion; raising mem_limit does nothing). init=True makes Docker
            # run docker-init (tini) as PID 1, which reaps them. Only the
            # sandbox path: hermes/moltis are sandbox=False and run their own
            # s6-overlay/tini as PID 1, so they must NOT get a docker-init on top.
            create_kwargs['init'] = True
            # Writable set: the per-user named volumes (already rw binds) cover
            # /workspace + /home/agent (installs land in ~/.local). tmpfs for
            # the paths a read-only root still needs to write.
            #
            # PR #84 regression fix: /tmp MUST be `exec`. The bundled coding
            # CLIs are Bun/Node single-file executables (opencode, codex) that
            # extract their embedded runtime to /tmp and exec it at startup; a
            # `noexec` /tmp (Docker's default for --tmpfs) made every launch die
            # with "Error: Unexpected error / An error occurred in
            # Effect.tryPromise" — the split regressed these vs the old
            # coding-tools container, which had no read-only-root + noexec-tmpfs
            # sandbox. We keep nosuid+nodev (block setuid escalation + device
            # nodes) but allow exec so the agent binaries run. /run stays
            # noexec (nothing exec's from there).
            tmpfs = {'/tmp': 'rw,nosuid,nodev,exec,size=512m',
                     '/run': 'rw,nosuid,nodev,size=16m'}
            for extra in (sandbox_writable or []):
                tmpfs[extra] = 'rw,nosuid,nodev,size=64m'
            create_kwargs['tmpfs'] = tmpfs
            # NO extra_hosts (host-gateway) for sandboxed agents — they must not
            # reach the host docker gateway.
        else:
            # rc6.7 #85: the unfenced per-user agents keep a
            # host.docker.internal alias (the host's docker0 gateway).
            # #36: pass the CONCRETE bridge gateway IP rather than the
            # `host-gateway` keyword — through docker-socket-proxy the keyword
            # resolves to `invalid IP` in /etc/hosts (see _host_gateway_ip
            # docstring).
            # W4a: assistant-net containers are socket-less by definition —
            # nothing of theirs lives behind the host gateway, so no
            # host.docker.internal alias (it would be a needless hole in the
            # fence).
            if not (assistant_net or coding_net):
                create_kwargs['extra_hosts'] = {'host.docker.internal': self._host_gateway_ip()}
        if secrets_uid is not None:
            # #2476: the owner's short-lived OpenBao token lives here (A21).
            # Owner-only, RAM-backed, never executable; mounted over the
            # sandbox's world-writable /run tmpfs, or on its own otherwise.
            tmpfs = dict(create_kwargs.get('tmpfs') or {})
            tmpfs['/run/rzfz'] = (f'rw,nosuid,nodev,noexec,size=1m,mode=0700,'
                                  f'uid={int(secrets_uid)},gid={int(secrets_uid)}')
            create_kwargs['tmpfs'] = tmpfs
        if blocked_hosts:
            # #2717: pinned to 0.0.0.0, so a lookup through /etc/hosts never
            # reaches the real intake. Added to any host-gateway alias above;
            # a sandbox still gets no host-gateway entry.
            hosts = dict(create_kwargs.get('extra_hosts') or {})
            hosts.update({h: '0.0.0.0' for h in blocked_hosts})
            create_kwargs['extra_hosts'] = hosts
        if command:
            create_kwargs['command'] = command
        if entrypoint:
            # rc6.7 #50 fix: per-agent ENTRYPOINT override. Lets the catalog
            # wrap the upstream image's entrypoint with init shims.
            create_kwargs['entrypoint'] = entrypoint

        container = self._client.containers.create(**create_kwargs)

        # Sandboxed coding agents get NO additional networks — they live ONLY
        # on `coding-agents` (egress-controlled). Return early so the default-
        # net + bridge attaches below never run for them.
        if sandbox or assistant_net or coding_net:
            return container.id

        # Also connect to the default stack network so agents can reach
        # shared services (gpustack, postgres, valkey, smtp-relay, etc.)
        try:
            default_net = self._client.networks.get('razzfazz-stack_default')
            default_net.connect(container)
            logger.info(f"Connected {name} to razzfazz-stack_default network")
        except Exception as e:
            logger.warning(f"Could not connect {name} to default network: {e}")

        return container.id

    def migrate_to_fenced_net(self, container_id_or_name: str, net: str) -> bool:
        """W4a (#256) self-healing migration — operator decision 3: an existing
        container moves onto its fence net ONLY at its NEXT start, no big-bang
        recreate. Connect the fence net first, then drop the flat nets (stack
        default / agent-network / bridge), so the container is never
        network-less in between. Returns True when anything changed. Errors are
        the caller's to log-and-continue — a failed migration must never block
        the start itself.

        The net is a parameter because two fences exist for the same reason:
        `agent-assistants` for hermes and `coding-agents` for the pre-split
        `coding-tools`. The migration is identical; only the destination
        differs, and duplicating it would be the place the two drift apart.
        """
        container = self._client.containers.get(container_id_or_name)
        nets = (container.attrs.get('NetworkSettings', {}) or {}).get('Networks', {}) or {}
        changed = False
        if net not in nets:
            self._client.networks.get(net).connect(container)
            changed = True
        for flat in (self._network, 'razzfazz-stack_default', 'bridge'):
            if flat in nets:
                try:
                    self._client.networks.get(flat).disconnect(container, force=True)
                    changed = True
                except docker.errors.NotFound:
                    pass
        if changed:
            logger.info(f"Migrated {container.name} to {net}-only networking")
        return changed

    def migrate_to_assistant_net(self, container_id_or_name: str) -> bool:
        """W4a's original entry point — the assistant fence. Kept as the named
        call site so the hermes path reads as what it is."""
        return self.migrate_to_fenced_net(container_id_or_name, 'agent-assistants')

    def start_container(self, container_id_or_name: str):
        """Start a stopped container."""
        container = self._client.containers.get(container_id_or_name)
        container.start()
        logger.info(f"Started container {container.name}")

    def stop_container(self, container_id_or_name: str, timeout: int = 10):
        """Stop a running container."""
        try:
            container = self._client.containers.get(container_id_or_name)
            container.stop(timeout=timeout)
            logger.info(f"Stopped container {container.name}")
        except docker.errors.NotFound:
            logger.warning(f"Container {container_id_or_name} not found for stop")

    def remove_container(self, container_id_or_name: str, force: bool = True):
        """Remove a container (force-kill if running).

        IMPORTANT: this calls container.remove(force=force) WITHOUT v=True,
        meaning anonymous volumes attached to the container survive removal.
        That's deliberate for M030: agent-manager owns volume lifecycle
        explicitly via remove_volume() in delete(), so anonymous volumes
        from pre-M030 instances linger as orphans (S5 will migrate them)
        and named volumes survive container recreates (S2 upgrade() relies
        on this).
        """
        try:
            container = self._client.containers.get(container_id_or_name)
            container.remove(force=force)
            logger.info(f"Removed container {container.name}")
        except docker.errors.NotFound:
            logger.warning(f"Container {container_id_or_name} not found for removal")

    def restart_container(self, container_id_or_name: str, timeout: int = 10):
        """Restart a container in place (SIGTERM → grace → SIGKILL → start).

        ga.2 (#219): preserves the container, its named volumes and its
        identity (same name/ID) — only the process inside is bounced. Used by
        provisioner.restart() to recover a wedged agent without losing state.
        Raises docker.errors.NotFound if the container is gone (caller
        pre-checks existence and surfaces a "use Start" message).
        """
        container = self._client.containers.get(container_id_or_name)
        container.restart(timeout=timeout)
        logger.info(f"Restarted container {container.name}")

    def exec_in_container(self, container_id_or_name: str,
                          cmd: list | str, timeout: int = 10) -> tuple[int, bytes]:
        """Run a command inside a running container, return (exit_code, output).

        M030-S2 Q6: used for per-agent pre-stop hooks (e.g. moltis SQLite
        WAL flush). Best-effort — caller is expected to log + tolerate
        failures; an unhealthy container should not block its own removal.

        The Docker SDK's exec_run does not support timeout natively; we
        rely on the daemon's behavior + Python-side socket timeouts.
        Most clean-shutdown commands complete in well under 10s; if not,
        the next docker stop will SIGTERM-then-SIGKILL the process anyway.
        """
        try:
            container = self._client.containers.get(container_id_or_name)
            result = container.exec_run(cmd)
            return result.exit_code, result.output
        except docker.errors.NotFound:
            logger.warning(f"Container {container_id_or_name} not found for exec")
            return -1, b''
        except Exception as e:
            logger.warning(f"exec_in_container({container_id_or_name}) failed: {e}")
            return -1, str(e).encode()

    def container_ips(self, container_id_or_name: str) -> set:
        """#2476 (A21): the container's addresses on its networks (the bao
        proxy accepts an agent's token only from its own container)."""
        c = self._client.containers.get(container_id_or_name)
        nets = ((c.attrs or {}).get('NetworkSettings') or {}).get('Networks') or {}
        return {n.get('IPAddress') for n in nets.values() if isinstance(n, dict) and n.get('IPAddress')}

    def get_container_state(self, container_id_or_name: str) -> str | None:
        """Get container state: running, exited, paused, etc. None if not found."""
        try:
            container = self._client.containers.get(container_id_or_name)
            return container.status
        except docker.errors.NotFound:
            return None

    def stale_networks(self, container_id_or_name: str) -> list[str] | None:
        """The networks this container is pinned to that no longer exist (#2589).

        A container records each network by ID. `docker compose down` + `up`
        (the backup restore, `rzfz down` + `rzfz start`) removes the project
        networks and creates new ones under the same names, so a container
        created outside the project keeps the old IDs, and `docker start` fails
        with "network <id> not found". Only a recreate fixes it.

        Returns the names whose recorded ID no longer matches the live network
        of that name (or no network of that name exists any more); [] when all
        match; None when the container is gone or docker cannot be read
        ("cannot tell").
        """
        try:
            attrs = self._client.containers.get(container_id_or_name).attrs or {}
        except docker.errors.NotFound:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"stale_networks({container_id_or_name}) failed: {e}")
            return None
        nets = (attrs.get('NetworkSettings') or {}).get('Networks') or {}
        stale = []
        for name, endpoint in nets.items():
            pinned = (endpoint or {}).get('NetworkID') or ''
            try:
                live = self._client.networks.get(name)
            except docker.errors.NotFound:
                stale.append(name)
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning(f"stale_networks: network {name} unreadable: {e}")
                return None
            if pinned and live.id != pinned:
                stale.append(name)
        return stale

    def get_image_id(self, image_ref: str) -> str | None:
        """Resolve the concrete image ID (sha256:...) for an ``image:tag`` ref.

        #36 follow-up — the `:latest`→`:latest` reprovision no-op fix. A
        mutable tag like ``razzfazz-stack-paperclip:latest`` points at whatever
        image ID was last built/pulled; a rebuild moves the tag to a NEW ID.
        The upgrade() tag-string compare (``latest == latest``) can't see this,
        so a rebuilt image is never picked up. Comparing this resolved ID
        against the running container's image ID (see get_container_image_id)
        detects the rebuild and forces a recreate. Returns None if the tag isn't
        present locally (never built/pulled) — the caller treats that as
        "cannot tell → don't force" and falls through to normal handling.
        """
        try:
            return self._client.images.get(image_ref).id
        except docker.errors.ImageNotFound:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"get_image_id({image_ref}) failed: {e}")
            return None

    def get_container_image_id(self, container_id_or_name: str) -> str | None:
        """The image ID (sha256:...) the given container is actually running.

        Paired with get_image_id() to detect a rebuilt mutable tag: if the tag
        now resolves to a different ID than the one the container was created
        from, the container is stale and must be recreated. Returns None if the
        container is gone.
        """
        try:
            container = self._client.containers.get(container_id_or_name)
            # `.image` is the Image object the container was created from;
            # `.id` is its content-addressable sha256 digest.
            return container.image.id if container.image else None
        except docker.errors.NotFound:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"get_container_image_id({container_id_or_name}) failed: {e}")
            return None

    #: The host daemon socket containers created before #2661/#2664 hold.
    RAW_DOCKER_SOCKET = '/var/run/docker.sock'

    def holds_raw_docker_socket(self, container_id_or_name: str) -> bool | None:
        """Does this container have the host's docker socket BIND-mounted?

        #2661: a container keeps the mounts it was created with, so a type that
        loses `requires_docker_socket` still hands the socket to every instance
        created before the change until that container is recreated. The
        caller uses this to find them.

        Only a `bind` whose source is the host socket counts. An image that
        declares `VOLUME /var/run/docker.sock` (the moltis image does) gets an
        anonymous volume there when nothing is bound — an empty directory, not
        the daemon.

        None when the container is gone or unreadable ("cannot tell").
        """
        try:
            attrs = self._client.containers.get(container_id_or_name).attrs or {}
        except docker.errors.NotFound:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"holds_raw_docker_socket({container_id_or_name}) failed: {e}")
            return None
        for m in attrs.get('Mounts') or []:
            if (m.get('Type') == 'bind'
                    and m.get('Source') == self.RAW_DOCKER_SOCKET):
                return True
        for b in (attrs.get('HostConfig') or {}).get('Binds') or []:
            if isinstance(b, str) and b.split(':', 1)[0] == self.RAW_DOCKER_SOCKET:
                return True
        return False

    def get_container_env(self, container_id_or_name: str) -> dict | None:
        """The environment a RUNNING container was created with, as a dict.

        #1446 (cutover C6, point 4): an agent's wiring — LLM endpoint, LLM key,
        the user's own MCP proxies — is resolved at CREATE time and baked into
        the container. Nothing re-reads it, so after an upgrade an existing
        instance keeps whatever the box looked like when its owner provisioned
        it. To re-wire only the instances that actually need it, the caller has
        to be able to SEE what a container currently carries.

        None when the container is gone or unreadable — "cannot tell", which
        the caller must not read as "in sync".
        """
        try:
            container = self._client.containers.get(container_id_or_name)
            raw = (container.attrs.get("Config") or {}).get("Env") or []
        except docker.errors.NotFound:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"get_container_env({container_id_or_name}) failed: {e}")
            return None
        env = {}
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, _, value = item.partition("=")
            env[key] = value
        return env

    def get_container_command(self, container_id_or_name: str) -> list | None:
        """The command a RUNNING container was created with (None = cannot tell).

        Hermes carries its LLM endpoint + key in the COMMAND, not in the env
        (`hermes config set model.base_url …`), so an env-only comparison would
        call a stale hermes in sync (#1446 C6/4).
        """
        try:
            container = self._client.containers.get(container_id_or_name)
            return (container.attrs.get("Config") or {}).get("Cmd")
        except docker.errors.NotFound:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"get_container_command({container_id_or_name}) failed: {e}")
            return None

    def get_oom_kill_count(self, container_id_or_name: str) -> int | None:
        """How many processes the cgroup OOM-killer has killed in this
        container, or ``None`` when it cannot be determined (#238).

        `docker inspect`'s `State.OOMKilled` is NOT usable here: it flips only
        when PID 1 itself is killed. The prod incident was the opposite case —
        two Claude Code sessions were killed inside a container whose entrypoint
        survived, so Docker reported `OOMKilled=false, ExitCode=0,
        RestartCount=0` and the agent "looked up" while the user's sessions were
        gone. The kernel does record it, in the cgroup's own accounting.

        A container can read its OWN cgroup, so we ask it over the existing exec
        path rather than requiring a host mount of /sys/fs/cgroup:

            cgroup v2:  /sys/fs/cgroup/memory.events        -> "oom_kill N"
            cgroup v1:  /sys/fs/cgroup/memory/memory.failcnt -> "N"

        Returns ``None`` rather than 0 when unreadable — "unknown" must never
        render as "never OOM'd".
        """
        for path in ('/sys/fs/cgroup/memory.events',
                     '/sys/fs/cgroup/memory/memory.failcnt'):
            try:
                rc, out = self.exec_in_container(
                    container_id_or_name, ['cat', path])
            except Exception as exc:  # container gone mid-probe, daemon blip…
                logger.debug("OOM probe failed on %s (%s): %s",
                             container_id_or_name, path, exc)
                continue
            if rc != 0:
                continue
            text = out.decode('utf-8', errors='replace') if isinstance(
                out, (bytes, bytearray)) else str(out)
            kills = parse_oom_kills(text)
            if kills is not None:
                return kills
        return None

    def get_container_stats(self, container_id_or_name: str,
                            include_oom: bool = False) -> dict | None:
        """Get live resource stats for a container (memory only).

        ``include_oom`` adds an ``oom_kills`` field (#238). It is OPT-IN and
        defaults off on purpose: this method runs once per RUNNING instance
        while the dashboard renders, and the OOM figure costs a container exec.
        PR #84 removed ~1s/container of avoidable latency from exactly this
        path; per-instance views (which look at one container) ask for it, the
        list view does not.

        PR #84 (dashboard-slowness fix): pass ``one_shot=True``. The default
        ``/containers/{id}/stats`` call BLOCKS ~1s per container because the
        Engine samples the CPU counter TWICE (a ~1s window) to compute a CPU
        delta. The dashboard calls this once per RUNNING instance
        *synchronously while rendering*, so N running agents added ~N seconds of
        TTFB (measured on 0.91: 6 instances → ~11.6s blank-page wait). We only
        use the MEMORY figures here (no CPU), and ``one_shot=True`` returns a
        single immediate sample — ~2ms vs ~1005ms measured — with identical
        memory values. (docker-py forwards unknown kwargs to the low-level API.)
        """
        try:
            container = self._client.containers.get(container_id_or_name)
            stats = container.stats(stream=False, one_shot=True)
            mem_usage = stats.get('memory_stats', {}).get('usage', 0)
            mem_limit = stats.get('memory_stats', {}).get('limit', 1)
            out = {
                'mem_usage_mb': round(mem_usage / (1024 * 1024), 1),
                'mem_limit_mb': round(mem_limit / (1024 * 1024), 1),
                'mem_percent': round(mem_usage / mem_limit * 100, 1) if mem_limit else 0,
            }
            if include_oom:
                kills = self.get_oom_kill_count(container_id_or_name)
                if kills is not None:
                    out['oom_kills'] = kills
            return out
        except (docker.errors.NotFound, docker.errors.APIError):
            return None

    def get_container_logs(self, container_id_or_name: str, tail: int = 200) -> str:
        """Get last N lines of container logs."""
        try:
            container = self._client.containers.get(container_id_or_name)
            return container.logs(tail=tail).decode('utf-8', errors='replace')
        except docker.errors.NotFound:
            return ''

    def host_mem_total_mb(self) -> int:
        """Total physical RAM of the docker HOST, in MB (read LIVE).

        #36 / PR #84 — the global agents-memory budget's ceiling is bounded by
        the host's REAL memory (host RAM minus a safety reserve for the core
        stack), NOT a hardcoded cap. Computed per-box at read time so it's
        generous on prod (lots of RAM) and tight on a Strix-Halo box where most
        RAM is BIOS-pinned VRAM.

        Prefers the Docker daemon's own view (`/info` MemTotal) since the
        manager talks to the daemon through docker-socket-proxy and may not see
        the host's /proc/meminfo directly; falls back to /proc/meminfo, then 0.
        """
        try:
            info = self._client.info()
            total = info.get('MemTotal')
            if total and int(total) > 0:
                return int(int(total) / (1024 * 1024))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"docker /info MemTotal unavailable: {e}")
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        kb = int(line.split()[1])
                        return int(kb / 1024)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"/proc/meminfo unavailable: {e}")
        return 0

    def update_container_memory(self, container_id_or_name: str, mem_limit: str):
        """Apply a new memory limit to a RUNNING container LIVE via the Docker
        Engine's update endpoint (no recreate). #36 / PR #84.

        Sets memory + memory-swap to the same value (swap == limit disables
        swap growth beyond the RAM limit). `mem_limit` is a docker string
        ('8g'). Raises on failure so the caller can fall back to a recreate.
        """
        container = self._client.containers.get(container_id_or_name)
        # docker-py's update() takes ints (bytes) for mem_limit/memswap_limit.
        from app.services.database import parse_mem_to_mb
        bytes_limit = parse_mem_to_mb(mem_limit) * 1024 * 1024
        container.update(mem_limit=bytes_limit, memswap_limit=bytes_limit)
        logger.info(f"Live-updated memory for {container.name} → {mem_limit}")

    def update_container_pids(self, container_id_or_name: str, pids_limit: int):
        """Apply a new PID cap to a RUNNING container LIVE (#232).

        Goes through the raw Engine API rather than docker-py's typed
        `container.update(...)`: that helper's signature predates `PidsLimit`
        (docker-py 7.2.0 accepts only blkio/cpu/mem/restart-policy), so the
        kwarg would be silently dropped and we would report a cap that is not
        in force. The Engine endpoint has supported it since API 1.23.

        Raises on failure so the caller can tell the user it did not apply.
        """
        container = self._client.containers.get(container_id_or_name)
        api = self._client.api
        resp = api._post_json(
            api._url("/containers/{0}/update", container.id),
            data={"PidsLimit": int(pids_limit)},
        )
        api._raise_for_status(resp)
        logger.info(f"Live-updated pids_limit for {container.name} → {pids_limit}")

    def create_volume(self, name: str) -> str:
        """Create a named volume. Returns volume name."""
        self._client.volumes.create(name=name)
        logger.info(f"Created volume {name}")
        return name

    def remove_volume(self, name: str):
        """Remove a named volume."""
        try:
            vol = self._client.volumes.get(name)
            vol.remove(force=True)
            logger.info(f"Removed volume {name}")
        except docker.errors.NotFound:
            logger.warning(f"Volume {name} not found for removal")

    # ── E1 (#2518): what an instance's containers ACTUALLY mount ─────────────
    # Recreate and delete used to recompute volume names from the row's slug +
    # the current catalog. These readers let them bind to reality instead.

    #: the label every agent container (primary AND companion) carries since
    #: launch stamped it; the instance id, never a slug.
    INSTANCE_LABEL = 'razzfazz.agent.instance'
    COMPANION_LABEL = 'razzfazz.agent.companion'

    @staticmethod
    def volume_mounts_from_attrs(attrs: dict | None) -> dict:
        """{volume name: destination} for the Type=volume entries of a
        container's inspect `Mounts` (bind mounts — the docker socket, the CA
        pem, host_path specs — are the host's, never the instance's)."""
        out = {}
        for m in ((attrs or {}).get('Mounts') or []):
            if not isinstance(m, dict) or m.get('Type') != 'volume':
                continue
            name, dest = m.get('Name'), m.get('Destination')
            if name and dest:
                out[name] = dest
        return out

    def _brief(self, container) -> dict:
        labels = dict(getattr(container, 'labels', None) or {})
        return {
            'id': container.id,
            'name': container.name,
            'labels': labels,
            'companion': labels.get(self.COMPANION_LABEL) == 'true',
            'mounts': self.volume_mounts_from_attrs(container.attrs),
        }

    def inspect_container_brief(self, container_id_or_name: str) -> dict | None:
        """{id, name, labels, companion, mounts} for one container, None when
        it does not exist. Any other docker error RAISES — "cannot tell" must
        never read as "has no volumes"."""
        try:
            container = self._client.containers.get(container_id_or_name)
        except docker.errors.NotFound:
            return None
        return self._brief(container)

    def find_instance_containers(self, instance_id) -> list[dict]:
        """Every container (running or not) labelled with this instance id —
        the primary and its companion(s). Errors RAISE (see above)."""
        containers = self._client.containers.list(
            all=True, filters={'label': f'{self.INSTANCE_LABEL}={instance_id}'})
        return [self._brief(c) for c in containers]

    def volume_exists(self, name: str) -> bool:
        """True iff the named volume exists. Errors other than NotFound RAISE."""
        try:
            self._client.volumes.get(name)
            return True
        except docker.errors.NotFound:
            return False

    def containers_using_volume(self, name: str) -> list[str]:
        """Names of every container (running or not) that mounts `name`."""
        return [c.name for c in self._client.containers.list(
            all=True, filters={'volume': name})]

    def list_volume_names(self, prefix: str = '') -> list[str]:
        """Names of every volume starting with `prefix` (report use only)."""
        return sorted(v.name for v in self._client.volumes.list()
                      if v.name and v.name.startswith(prefix))

    @classmethod
    def read_only(cls, base_url: str) -> 'AgentDockerClient':
        """A client for read-only CLI reports: no network creation, no cgroup
        probe side effects (`slug_migration --dry-run` must not change a box)."""
        self = cls.__new__(cls)
        self._client = docker.DockerClient(base_url=base_url)
        self._cgroup_driver = ''
        self._network = ''
        return self

    #: #2660: what makes a container an AGENT INSTANCE. `razzfazz.managed` is
    #: shared by every socket-provisioned container (LLM engines, the LLM
    #: Manager, MCP servers) and says nothing about agents. `razzfazz.agent.type`
    #: is stamped by this manager on every primary and companion since M011 —
    #: older than `razzfazz.agent.instance` (#2518), so instances launched
    #: before that label existed are still recognised and need no migration.
    AGENT_TYPE_LABEL = 'razzfazz.agent.type'

    #: Defence in depth: labels that mark a container as somebody else's even if
    #: it ever also wears a razzfazz.agent.* label. `rzfz.role` is set by the LLM
    #: node-agent on every engine (any value: llm-engine today, others later).
    FOREIGN_ROLE_LABELS = ('rzfz.role',)

    def list_agent_containers(self) -> list[dict]:
        """Containers this manager created as agent instances (primary or
        companion): managed AND carrying `razzfazz.agent.type`, and not marked
        as another component's by a foreign role label (#2660)."""
        containers = self._client.containers.list(
            all=True,
            filters={'label': ['razzfazz.managed=true', self.AGENT_TYPE_LABEL]}
        )
        out = []
        for c in containers:
            labels = c.labels or {}
            if not labels.get(self.AGENT_TYPE_LABEL):
                continue
            if any(k in labels for k in self.FOREIGN_ROLE_LABELS):
                continue
            out.append({
                'id': c.id,
                'name': c.name,
                'status': c.status,
                'agent_type': labels.get(self.AGENT_TYPE_LABEL, ''),
                'user': labels.get('razzfazz.agent.user', ''),
            })
        return out

    def agent_container_envs(self) -> list[dict]:
        """#1039: every razzfazz-managed container (running or not) with the
        environment it was created with: `{'name', 'instance_id', 'env'}`.

        Unlike get_container_env this RAISES on any docker error, and on a
        container whose config cannot be read: the caller decides whether a
        database role is in use, and "cannot tell" must never read as
        "nobody uses it". A container absent from a successful listing is
        positively absent."""
        out = []
        for c in self._client.containers.list(all=True,
                                              filters={'label': 'razzfazz.managed=true'}):
            config = (c.attrs or {}).get('Config')
            if not isinstance(config, dict):
                raise RuntimeError(f"container {c.name}: no readable config")
            env = {}
            for item in config.get('Env') or []:
                if isinstance(item, str) and '=' in item:
                    key, _, value = item.partition('=')
                    env[key] = value
            out.append({'name': c.name,
                        'instance_id': (c.labels or {}).get('razzfazz.agent.instance', ''),
                        'env': env})
        return out

    def ping(self) -> bool:
        """Check Docker daemon connectivity."""
        try:
            self._client.ping()
            return True
        except Exception:
            return False

    # ── Rework-W1 (#244 item 12): files panel ────────────────────────────────
    # Archive-based file transfer against a RUNNING container — the manager has
    # no host mount of the per-instance volumes (by design: the sandboxes'
    # data never appears in the manager's own filesystem namespace); the
    # docker API's archive endpoints are the sanctioned path in/out.

    #: #595 review: `find -printf` is NOT busybox-portable (busybox find has no
    #: -printf) — and paperclip is an EXTERNAL image whose base we don't
    #: control. A portable sh loop instead: POSIX `[ -d ]` for the kind and
    #: `wc -c < file` for the size (stat's format flags differ between
    #: coreutils/busybox; wc -c is identical everywhere). One exec, no parsing
    #: ambiguity: tab-separated, filenames last so tabs in names cannot shift
    #: earlier fields.
    _LIST_SH = (
        'cd "$1" || exit 44; for f in * .[!.]* ..?*; do '
        '[ -e "$f" ] || continue; '
        'if [ -d "$f" ]; then printf "d\t0\t%s\n" "$f"; '
        'else printf "f\t%s\t%s\n" "$(wc -c < "$f" 2>/dev/null || echo 0)" "$f"; fi; '
        'done')

    def list_path(self, container_name: str, path: str) -> list:
        """Entries under ``path`` inside the container: [{name, size, dir}]."""
        c = self._client.containers.get(container_name)
        code, out = c.exec_run(["sh", "-c", self._LIST_SH, "sh", path],
                               demux=False)
        if code == 44:
            raise FileNotFoundError(path)
        entries = []
        for line in (out or b"").decode("utf-8", "replace").splitlines():
            try:
                kind, size, name = line.split("\t", 2)
            except ValueError:
                continue
            entries.append({"name": name, "size": int(size.strip() or 0),
                            "dir": kind == "d"})
        return sorted(entries, key=lambda e: (not e["dir"], e["name"].lower()))

    def read_file_tar(self, container_name: str, path: str):
        """(tar_byte_iterator, stat) for ``path`` — the file wrapped in a tar
        stream, exactly what docker's get_archive yields. Caller unpacks."""
        c = self._client.containers.get(container_name)
        stream, stat = c.get_archive(path)
        return stream, stat

    def path_owner(self, container_name: str, path: str):
        """(uid, gid) that own ``path`` inside the container, or None.

        #2947: an upload is written with the owner of the folder it lands in.
        put_archive extracts with the uid/gid of the tar header (0:0 unless
        set), and an agent's volume folders belong to its working user (agent
        1000, hermes 10000, moltis ...), whatever user the image's entrypoint
        starts as. `ls -nd` is POSIX and in busybox; numeric ids only."""
        c = self._client.containers.get(container_name)
        code, out = c.exec_run(["sh", "-c", 'ls -nd "$1"', "sh", path], demux=False)
        if code != 0:
            return None
        fields = (out or b"").decode("utf-8", "replace").split()
        try:
            uid, gid = int(fields[2]), int(fields[3])
        except (IndexError, ValueError):
            return None
        return (uid, gid) if uid >= 0 and gid >= 0 else None

    def write_file_tar(self, container_name: str, dest_dir: str, tar) -> bool:
        """Extract ``tar`` (a tar archive: bytes, or an open file that is
        streamed from disk, #2947) into ``dest_dir``.

        Raises FileNotFoundError when ``dest_dir`` does not exist in the
        container: docker answers 404 for it, and the route turns that into a
        404 with the folder named instead of a bare 500 (#3106 box pass) —
        the same contract as ``list_path``."""
        c = self._client.containers.get(container_name)
        try:
            return bool(c.put_archive(dest_dir, tar))
        except Exception as exc:  # docker.errors.NotFound, by name: no SDK import here
            if type(exc).__name__ == "NotFound":
                raise FileNotFoundError(dest_dir) from exc
            raise
