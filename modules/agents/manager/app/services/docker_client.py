# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Docker client wrapper for agent container lifecycle management."""

import logging
import os
import re

import docker

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



class AgentDockerClient:
    def __init__(self, base_url: str, network: str):
        self._client = docker.DockerClient(base_url=base_url)
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
        then raises gaierror and OpenHands' sandbox-readiness probe to
        `http://host.docker.internal:<port>` fails → "Sandbox entered error
        state" (verified on 0.91). Resolving the real bridge gateway IP here and
        passing it as a concrete extra_host makes the mapping deterministic and
        proxy-independent. The spawned oh-agent-server-* runtimes land on
        172.17.0.x and publish to the host, reachable via this gateway.
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

    def create_container(self, name: str, image: str, version: str,
                         environment: dict, volumes: dict,
                         mem_limit: str = '256m', cpu_limit: float = 1.0,
                         docker_socket: bool = False,
                         labels: dict = None,
                         command=None,
                         entrypoint=None,
                         sandbox: bool = False,
                         sandbox_writable: list | None = None,
                         pids_limit: int | None = None) -> str:
        """Create a new agent container (stopped). Returns container ID.

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

        NOTE deliberately unchanged: openhands / hermes / moltis are NOT
        sandboxed here — openhands needs the docker socket + host-gateway and
        hermes/moltis have their own trust model; passing sandbox=False keeps
        their exact pre-existing wiring.
        """
        image_ref = f"{image}:{version}"
        logger.info(f"Creating container {name} from {image_ref}"
                    f"{' [SANDBOX]' if sandbox else ''}")

        volume_binds = {}
        for vol_name, mount_path in volumes.items():
            volume_binds[vol_name] = {'bind': mount_path, 'mode': 'rw'}

        if docker_socket:
            volume_binds['/var/run/docker.sock'] = {
                'bind': '/var/run/docker.sock', 'mode': 'rw'
            }
            logger.warning(f"Container {name} has Docker socket access")

        # #281: corporate-proxy for socket-spawned containers. The compose
        # corporate-proxy overlay only reaches compose SERVICES; per-user agent
        # containers created here go through docker-socket-proxy and get nothing.
        # When the box is proxied, propagate the SAME proxy routing + CA trust the
        # manager itself received (agent-manager is in the overlay egress
        # allow-list, so these vars are in the manager's own env). No-op otherwise.
        if os.environ.get('RAZZFAZZ_CORPORATE_PROXY') == '1':
            _inject = {
                'HTTP_PROXY':  os.environ.get('HTTP_PROXY', ''),
                'HTTPS_PROXY': os.environ.get('HTTPS_PROXY', ''),
                'http_proxy':  os.environ.get('http_proxy', os.environ.get('HTTP_PROXY', '')),
                'https_proxy': os.environ.get('https_proxy', os.environ.get('HTTPS_PROXY', '')),
                'NO_PROXY':    os.environ.get('NO_PROXY', ''),
                'no_proxy':    os.environ.get('no_proxy', os.environ.get('NO_PROXY', '')),
            }
            # CA env + mount go TOGETHER (empty-dir x509 trap). The bind SOURCE
            # MUST be a HOST path — the daemon behind docker-socket-proxy resolves
            # it on the host fs, NOT inside this manager container. Mirrors the
            # openhands {{STACK_HOST_PATH}} host_path convention (catalog.py).
            _stack_host = os.environ.get('STACK_HOST_PATH', '')
            if _stack_host:
                _ca_in = os.environ.get('SSL_CERT_FILE') or '/certs/caddy-ca.pem'
                volume_binds['%s/certs/caddy-ca.pem' % _stack_host] = {
                    'bind': _ca_in, 'mode': 'ro',
                }
                for _k in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE',
                           'GIT_SSL_CAINFO', 'PIP_CERT', 'NODE_EXTRA_CA_CERTS'):
                    _inject[_k] = _ca_in
            else:
                logger.warning(
                    "RAZZFAZZ_CORPORATE_PROXY=1 but STACK_HOST_PATH unset — "
                    "skipping CA bind for %s (proxy env still injected)", name)
            # Catalog-provided env wins; only fill in vars we actually have.
            environment = {**{_k: _v for _k, _v in _inject.items() if _v},
                           **environment}

        nano_cpus = int(cpu_limit * 1e9)

        # Sandboxed coding agents attach to the dedicated egress-controlled
        # `coding-agents` network at CREATE time (so they never briefly sit on
        # the stack default net) and get NO other network below.
        initial_network = 'coding-agents' if sandbox else self._network

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
            security_opt=['no-new-privileges:true'] if not docker_socket else [],
        )
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
            # rc6.7 #85: per-user agents that spawn child containers via
            # the Docker socket (openhands → oh-agent-server-*) need to
            # reach those children via host.docker.internal — the random
            # host port Docker maps when spawning the runtime is on the
            # host's docker0 gateway, not on agent-network.
            # #36: pass the CONCRETE bridge gateway IP rather than the
            # `host-gateway` keyword — through docker-socket-proxy the keyword
            # resolves to `invalid IP` in /etc/hosts and the openhands sandbox
            # probe fails (see _host_gateway_ip docstring).
            create_kwargs['extra_hosts'] = {'host.docker.internal': self._host_gateway_ip()}
        if command:
            create_kwargs['command'] = command
        if entrypoint:
            # rc6.7 #50 fix: per-agent ENTRYPOINT override. Lets the catalog
            # wrap the upstream image's entrypoint with init shims (e.g. the
            # OpenHands chown-then-exec pattern that the shared compose uses
            # for /.openhands volume permissions).
            create_kwargs['entrypoint'] = entrypoint

        container = self._client.containers.create(**create_kwargs)

        # Sandboxed coding agents get NO additional networks — they live ONLY
        # on `coding-agents` (egress-controlled). Return early so the default-
        # net + bridge attaches below never run for them.
        if sandbox:
            return container.id

        # Also connect to the default stack network so agents can reach
        # shared services (gpustack, postgres, valkey, smtp-relay, etc.)
        try:
            default_net = self._client.networks.get('razzfazz-stack_default')
            default_net.connect(container)
            logger.info(f"Connected {name} to razzfazz-stack_default network")
        except Exception as e:
            logger.warning(f"Could not connect {name} to default network: {e}")

        # rc6.7 #86: agents that spawn child containers via the Docker
        # socket (openhands → oh-agent-server-* runtimes) need an
        # interface on Docker's default `bridge` network too. Children
        # spawned via the Docker socket land on `bridge` by default, and
        # without a direct interface on it, the parent agent's packets
        # to the children's host-published ports get blocked by docker's
        # inter-bridge isolation (host.docker.internal:<port> connects
        # but never routes through to 172.17.0.x:<int_port>). Mirrors the
        # same dual-network setup the global apps/openhands/compose.yml
        # uses (rc6.7 #46 v3). Skip silently if the agent doesn't need
        # docker_socket — no harm being on `bridge` either, but no need.
        if docker_socket:
            try:
                bridge_net = self._client.networks.get('bridge')
                bridge_net.connect(container)
                logger.info(f"Connected {name} to docker default `bridge` (sandbox parent)")
            except Exception as e:
                logger.warning(f"Could not connect {name} to docker bridge: {e}")

        return container.id

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

    def get_container_state(self, container_id_or_name: str) -> str | None:
        """Get container state: running, exited, paused, etc. None if not found."""
        try:
            container = self._client.containers.get(container_id_or_name)
            return container.status
        except docker.errors.NotFound:
            return None

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

    def list_agent_containers(self) -> list[dict]:
        """List all containers with the razzfazz.managed=true label."""
        containers = self._client.containers.list(
            all=True,
            filters={'label': 'razzfazz.managed=true'}
        )
        return [
            {
                'id': c.id,
                'name': c.name,
                'status': c.status,
                'agent_type': c.labels.get('razzfazz.agent.type', ''),
                'user': c.labels.get('razzfazz.agent.user', ''),
            }
            for c in containers
        ]

    def ping(self) -> bool:
        """Check Docker daemon connectivity."""
        try:
            self._client.ping()
            return True
        except Exception:
            return False
