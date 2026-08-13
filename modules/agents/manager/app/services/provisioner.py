# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Provisioning engine — creates and manages per-user agent instances."""

import json
import logging
import os
import secrets

import psycopg2

# #61 NEW-1: SINGLE source of truth for the user slug, shared with mcp-manager.
# Both services bind-mount core/common/razzfazz_common at /app/common and set
# PYTHONPATH=/app/common, so this import resolves identically in both. Re-exported
# here so existing `from app.services.provisioner import make_user_slug` callers
# (and the cross-service equality test) keep working. The two derivations MUST
# agree byte-for-byte or agent-manager's /internal/agent-wiring/<slug> lookup
# misses mcp-manager's instances and the per-proxy bearer never reaches the agent.
#
# #192: also re-export slug_candidates (current + legacy pre-hash slug) so
# ownership checks in proxy.py / api.py can accept instances provisioned
# before make_user_slug grew its hash suffix.
from razzfazz_common.user_slug import make_user_slug, slug_candidates  # noqa: F401

logger = logging.getLogger(__name__)

# #36 security-review — the socket-less coding-agent types that run
# arbitrary/LLM-driven code and MUST be sandboxed (egress-controlled
# `coding-agents` net only, cap_drop ALL, read-only root + tmpfs, pids_limit,
# no host bind-mounts, no docker socket). Kept as an authoritative constant
# here (rather than a DB column) so enforcement is migration-free and cannot
# be weakened by a stale/partial DB row: a type is sandboxed iff it's in this
# set, regardless of what the DB carries. openhands/hermes/moltis/paperclip are
# deliberately NOT here — they keep their existing (unsandboxed) wiring.
SANDBOXED_TYPES = frozenset({'opencode', 'gsd-pi', 'codex', 'user-defined'})

# #36 / PR #84 — agent memory governance.
# Per-instance memory presets offered in the UI (GB). The default is the floor
# every user gets; power/admin may raise up to the per-instance max.
MEM_PRESETS_GB = (2, 4, 8, 16)
# Only a power/admin tier may set a custom per-instance memory. We derive
# "power/admin" from the EXISTING tier model (resolve_user_tier → priority):
# agent-basic=0, agent-power=10, agent-admin=99, unlimited-admin=1000. Anyone at
# or above this threshold is a power/admin; a regular (basic) user is below it
# and always gets the default, server-side (the UI just hides the field).
POWER_TIER_MIN_PRIORITY = 10


# ── Gitea checkout wiring (#165) ─────────────────────────────────────────────
def _gitea_internal_url() -> str:
    """The Gitea endpoint reachable from inside the sandbox (agent net).
    Defaults to the docker-DNS name every stack service uses."""
    return (os.environ.get('GITEA_INTERNAL_URL', '') or '').strip() or 'http://gitea:3000'


def _gitea_external_url() -> str:
    """The public Gitea base URL the web UI shows (and the entrypoint rewrites
    to the internal host). Prefer an explicit GITEA_EXTERNAL_URL; else derive
    from GITEA_DOMAIN (default `git.<MAIN_DOMAIN>`, matching .env.example). Guard
    against an unexpanded `${MAIN_DOMAIN}` literal (env_file doesn't expand it)."""
    ext = (os.environ.get('GITEA_EXTERNAL_URL', '') or '').strip()
    if ext:
        return ext
    main_domain = os.environ.get('MAIN_DOMAIN', 'localhost')
    gdom = (os.environ.get('GITEA_DOMAIN', '') or '').strip()
    if not gdom or '${' in gdom:
        gdom = f'git.{main_domain}'
    return f'https://{gdom}'


def _tier_allows_custom_memory(tier: dict | None) -> bool:
    """Server-side tier gate for the per-instance memory control. True only for
    power/admin tiers (priority >= POWER_TIER_MIN_PRIORITY)."""
    if not tier:
        return False
    try:
        return int(tier.get('priority') or 0) >= POWER_TIER_MIN_PRIORITY
    except (TypeError, ValueError):
        return False


def _is_sandboxed(agent_type: str, type_info: dict | None = None) -> bool:
    """Authoritative sandbox decision. True for the coding-agent split types.

    Also honours an explicit `sandbox: True` in the catalog SEED_TYPES dict if
    it's ever surfaced through the DB row, but the membership test is the source
    of truth so the sandbox can never be silently dropped."""
    if agent_type in SANDBOXED_TYPES:
        return True
    return bool(type_info and type_info.get('sandbox'))


class Provisioner:
    def __init__(self, db, docker_client, caddy_client, catalog, config: dict,
                 authentik_client=None):
        self._db = db
        self._docker = docker_client
        self._caddy = caddy_client
        self._catalog = catalog
        self._config = config
        self._authentik = authentik_client

    def _instance_host(self, agent_type: str, instance_id) -> str:
        """The per-instance forward-auth host (matches caddy_client's route)."""
        from app.services.caddy_client import instance_token
        agents_domain = self._config.get('AGENTS_DOMAIN', '')
        return f"{agent_type}-{instance_token(instance_id)}.{agents_domain}"

    def _register_authentik(self, agent_type: str, instance_id):
        """Register the per-instance forward_single Authentik provider (PR #84
        C1). Best-effort: a failure logs but doesn't abort the launch — the C1
        anchors still fence the instance, and reconcile/retry re-registers."""
        if not self._authentik:
            return
        try:
            host = self._instance_host(agent_type, instance_id)
            self._authentik.register_instance(host)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "Authentik register for %s/%s failed (non-fatal): %s",
                agent_type, instance_id, e)

    def _deregister_authentik(self, agent_type: str, instance_id):
        """Remove the per-instance Authentik provider on stop/delete."""
        if not self._authentik:
            return
        try:
            host = self._instance_host(agent_type, instance_id)
            self._authentik.deregister_instance(host)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "Authentik deregister for %s/%s failed (non-fatal): %s",
                agent_type, instance_id, e)

    def check_quota(self, user_slug: str, user_groups: list[str],
                    agent_type: str) -> tuple[bool, str]:
        """Check if the user can launch an instance of this agent type.

        Returns (allowed, reason).
        """
        tier = self._db.resolve_user_tier(user_groups)
        if not tier:
            return False, 'No agent access. Contact your admin to be added to an agent group.'

        type_info = self._catalog.get_type(agent_type)
        if not type_info:
            return False, f'Unknown agent type: {agent_type}'

        if not type_info['enabled']:
            return False, f'{type_info["display_name"]} is currently disabled.'

        # Check allowed_types
        allowed = tier.get('allowed_types')
        if allowed and agent_type not in allowed:
            return False, f'Your tier ({tier["display_name"]}) does not include {type_info["display_name"]}.'

        # Check per-type limit
        type_count = self._db.count_user_instances(user_slug, agent_type=agent_type)
        if type_count >= tier['max_per_type']:
            return False, f'You already have {type_count} {type_info["display_name"]} instance(s) (max {tier["max_per_type"]}).'

        # Check heavy tier limit
        if type_info['tier'] == 'heavy':
            heavy_count = self._db.count_user_instances(user_slug, tier='heavy')
            if heavy_count >= tier['max_heavy']:
                return False, f'You have reached your heavy agent limit ({tier["max_heavy"]}).'

        # Check global limit
        total = self._db.count_all_instances()
        if total >= self._config['AGENT_MAX_INSTANCES']:
            return False, 'System-wide agent instance limit reached. Try again later.'

        return True, 'OK'

    # ── Memory governance (#36 / PR #84) ──────────────────────────────────────

    def _per_instance_max_gb(self) -> int:
        return int(self._config.get('AGENT_MEM_PER_INSTANCE_MAX_GB', 16))

    def _default_mem_gb(self) -> int:
        return int(self._config.get('AGENT_MEM_DEFAULT_GB', 2))

    def resolve_mem_limit(self, tier: dict | None, type_info: dict,
                          requested_gb) -> str:
        """L1 — resolve the effective per-instance mem_limit ('Ng'), SERVER-SIDE.

        * A regular (non-power) tier ALWAYS gets the default — a requested value
          is ignored (the UI hides the field; the server enforces it).
        * A power/admin tier may raise it, CLAMPED to the per-instance max.
        * No/blank request → default. Below default → default (never smaller
          than the floor the agent needs).
        """
        default_gb = self._default_mem_gb()
        if not _tier_allows_custom_memory(tier):
            return f"{default_gb}g"
        try:
            gb = int(requested_gb)
        except (TypeError, ValueError):
            return f"{default_gb}g"
        gb = max(default_gb, min(gb, self._per_instance_max_gb()))
        return f"{gb}g"

    def effective_mem_budget_mb(self, configured_mb: int | None = None) -> int:
        """L2 — the effective global agents-memory budget in MB, BOUNDED by the
        host's REAL memory (host RAM − a core-stack safety reserve), computed
        LIVE. Never a hardcoded cap: on a big-RAM prod box the ceiling is high;
        on a Strix-Halo box (most RAM pinned as VRAM) it's tight.

        `configured_mb` is the operator-set budget (from agent_settings). The
        returned value is min(configured, host_ceiling) — an admin can't set a
        budget larger than the box can actually back.
        """
        reserve = int(self._config.get('AGENT_CORE_STACK_RESERVE_MB', 8192))
        host_total = 0
        try:
            host_total = int(self._docker.host_mem_total_mb())
        except Exception:  # noqa: BLE001
            host_total = 0
        host_ceiling = max(0, host_total - reserve) if host_total else 0
        if configured_mb is None:
            raw = self._db.get_agent_setting('global_mem_budget_mb', None)
            configured_mb = int(raw) if raw not in (None, '') else host_ceiling
        else:
            configured_mb = int(configured_mb)
        if host_ceiling:
            return min(configured_mb, host_ceiling)
        return configured_mb

    def _per_user_cap_mb(self) -> int | None:
        """L3 — the per-user memory cap in MB, or None if unset (no cap)."""
        raw = self._db.get_agent_setting('per_user_mem_mb', None)
        if raw in (None, ''):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def check_memory_budget(self, user_slug: str, mem_limit: str,
                            exclude_instance_mb: int = 0) -> tuple[bool, str]:
        """L2 + L3 — refuse a launch/increase that would exceed the global
        budget or the per-user cap. `exclude_instance_mb` subtracts the current
        allocation of the instance being resized (so a raise counts only the
        DELTA against the sums). Returns (allowed, reason)."""
        from app.services.database import parse_mem_to_mb
        want_mb = parse_mem_to_mb(mem_limit)

        # Global budget (bounded by real host memory).
        budget = self.effective_mem_budget_mb()
        if budget and budget > 0:
            running = int(self._db.sum_running_mem_mb()) - int(exclude_instance_mb)
            if running + want_mb > budget:
                return False, (
                    'Agents memory budget exhausted — stop an instance or ask '
                    f'an admin to raise the budget (budget {budget} MB, in use '
                    f'{max(0, running)} MB, requested {want_mb} MB).'
                )

        # Per-user cap.
        cap = self._per_user_cap_mb()
        if cap and cap > 0:
            user_running = int(self._db.sum_user_running_mem_mb(user_slug)) - int(exclude_instance_mb)
            if user_running + want_mb > cap:
                return False, (
                    'Your personal agents memory cap would be exceeded — stop '
                    f'one of your agents first (cap {cap} MB, you are using '
                    f'{max(0, user_running)} MB, requested {want_mb} MB).'
                )
        return True, 'OK'

    def launch(self, agent_type: str, user_id: str, username: str,
               user_groups: list[str], user_config: dict = None) -> tuple[str | None, str]:
        """Launch a new agent instance. Returns (instance_id, message)."""
        user_slug = make_user_slug(username)

        # Check if already exists
        existing = self._db.get_instance_by_type_and_user(agent_type, user_slug)
        if existing:
            if existing['state'] == 'stopped':
                return self.start(existing['id'], username)
            return str(existing['id']), f'Instance already {existing["state"]}.'

        # Quota check
        allowed, reason = self.check_quota(user_slug, user_groups, agent_type)
        if not allowed:
            return None, reason

        type_info = self._catalog.get_type(agent_type)
        container_name = f"agent-{agent_type}-{user_slug}"

        # ── Memory governance (#36 / PR #84) ──────────────────────────────────
        # L1: resolve the effective per-instance mem_limit, SERVER-SIDE and
        # tier-gated (a regular user's requested value is ignored → default;
        # power/admin may raise it, clamped to the per-instance max).
        tier = self._db.resolve_user_tier(user_groups)
        mem_limit = self.resolve_mem_limit(
            tier, type_info, (user_config or {}).get('mem_gb'))
        # L2 + L3: refuse a launch that would exceed the global budget (bounded
        # by real host RAM) or the per-user cap.
        ok, mem_reason = self.check_memory_budget(user_slug, mem_limit)
        if not ok:
            return None, mem_reason

        # Create DB record
        instance_config = user_config or {}
        instance_config['_generated_secret'] = secrets.token_urlsafe(24)
        # #165: mint a PER-USER Gitea token for the coding agents so they can
        # clone/push the box Gitea out of the box. Best-effort + persisted here
        # (in instance_config) so it survives an upgrade/recreate; empty → the
        # entrypoint skips the credential block and the user pastes their own PAT
        # (public clones still work via the entrypoint's insteadOf rewrite).
        if agent_type in SANDBOXED_TYPES:
            instance_config['_gitea_token'] = self._mint_gitea_token(
                username, user_slug)
        # Persist the chosen limit so it survives a recreate (upgrade() re-reads
        # config) and so the budget sums see the real per-instance allocation.
        instance_config['mem_limit'] = mem_limit
        instance_id = self._db.create_instance(
            agent_type, user_id, user_slug, container_name, instance_config
        )

        try:
            # Create volumes
            volumes = {}
            for vol_spec in (json.loads(type_info['volumes'])
                             if isinstance(type_info['volumes'], str)
                             else type_info['volumes']):
                # rc6.7 #91: support `host_path` bind mounts in addition to
                # named volumes. Used by openhands to bind the monkey-patch
                # script from STACK_HOST_PATH so the per-user backend can
                # apply the same readiness-probe rewrite the global compose
                # uses. Templated like other catalog values.
                if 'host_path' in vol_spec:
                    host_path = (vol_spec['host_path']
                                 .replace('{{STACK_HOST_PATH}}',
                                          os.environ.get('STACK_HOST_PATH', '')))
                    volumes[host_path] = vol_spec['mount']
                else:
                    vol_name = f"agent-{agent_type}-{user_slug}-{vol_spec['name_suffix']}"
                    self._docker.create_volume(vol_name)
                    volumes[vol_name] = vol_spec['mount']

            # Create per-instance database if needed
            if type_info['requires_db']:
                self._create_instance_db(agent_type, user_slug, instance_config)

            # Resolve environment variables
            env = self._resolve_env(type_info, user_slug, username, instance_config,
                                    instance_id=instance_id)
            # #36 gap 2 — merge the user's OWN running MCP-proxy endpoints.
            env = self._inject_user_mcp(env, agent_type, user_slug)

            # Create container
            ports = (json.loads(type_info['ports'])
                     if isinstance(type_info['ports'], str) else type_info['ports'])
            labels = {
                'razzfazz.managed': 'true',
                'razzfazz.agent.type': agent_type,
                'razzfazz.agent.user': user_slug,
                'razzfazz.agent.instance': str(instance_id),
            }

            container_id = self._docker.create_container(
                name=container_name,
                image=type_info['image'],
                version=type_info['version'],
                environment=env,
                volumes=volumes,
                # #36 / PR #84 — the tier-gated per-instance mem_limit (persisted
                # on instance_config above), not the catalog default.
                mem_limit=mem_limit,
                cpu_limit=type_info['cpu_limit'],
                # #221 — per-type PID cap (coding family = 2048); None falls back
                # to docker_client's 512 fork-bomb-hardening default.
                pids_limit=type_info.get('pids_limit'),
                docker_socket=type_info['requires_docker_socket'],
                labels=labels,
                command=type_info.get('command') or None,
                entrypoint=type_info.get('entrypoint') or None,
                # #36 security-review — sandbox the coding-agent split types.
                sandbox=_is_sandboxed(agent_type, type_info),
            )

            # Start container
            self._docker.start_container(container_id)

            # M020 S02 — companion container (e.g. hermes-workspace alongside
            # hermes-agent). Same instance, same network, separate container
            # lifecycle managed in lockstep with the primary in start/stop/delete.
            companion_image = type_info.get('companion_image')
            if companion_image:
                companion_suffix = type_info.get('companion_suffix') or 'workspace'
                companion_name = f"{container_name}-{companion_suffix}"
                companion_volumes = {}
                companion_vol_specs = (json.loads(type_info.get('companion_volumes') or '[]')
                                       if isinstance(type_info.get('companion_volumes'), str)
                                       else (type_info.get('companion_volumes') or []))
                for vol_spec in companion_vol_specs:
                    vol_name = f"agent-{agent_type}-{user_slug}-{vol_spec['name_suffix']}"
                    self._docker.create_volume(vol_name)
                    companion_volumes[vol_name] = vol_spec['mount']
                companion_env = self._resolve_env(
                    type_info, user_slug, username, instance_config,
                    template_field='companion_env_template',
                    instance_id=instance_id,
                )
                companion_labels = {**labels, 'razzfazz.agent.companion': 'true'}
                companion_id = self._docker.create_container(
                    name=companion_name,
                    image=companion_image,
                    version=type_info.get('companion_version') or 'latest',
                    environment=companion_env,
                    volumes=companion_volumes,
                    mem_limit=type_info['mem_limit'],   # shares the type's budget
                    cpu_limit=type_info['cpu_limit'],
                    docker_socket=False,                 # companions don't need it today
                    labels=companion_labels,
                    # #36: optional companion command override (e.g. hermes-
                    # workspace seeds its own config.yaml before exec'ing the
                    # workspace server). None → image's default entrypoint/CMD.
                    command=type_info.get('companion_command') or None,
                )
                self._docker.start_container(companion_id)
                logger.info(f"Launched companion {companion_name} (id={companion_id}) for {container_name}")

            # Register Caddy route — points at primary by default. For bundled
            # types where the user-facing UI is on the companion, the catalog
            # places the companion's UI port at `internal` and the primary's
            # API port at `<extra>_internal`; the route still uses the
            # primary container_name as the target *unless* an explicit
            # routing override is specified in the catalog. See hermes for the
            # canonical example: companion (workspace) is the user-facing UI,
            # primary (agent) is the gateway. Caddy routes the user-facing UI
            # by binding to the companion-suffix container name.
            route_target = (container_name if not companion_image
                            else f"{container_name}-{type_info.get('companion_suffix') or 'workspace'}")
            self._caddy.register_route(
                agent_type, user_slug, route_target,
                ports['internal'], username, instance_id=instance_id,
            )
            # PR #84 C1: register the per-instance Authentik forward-auth provider
            # so the embedded outpost authenticates this subdomain.
            self._register_authentik(agent_type, instance_id)

            # Update state. M030-S2: also stamp image_version so the
            # dashboard's update-available detector has accurate ground
            # truth from the first launch onward.
            self._db.update_instance_state(
                instance_id, 'running',
                container_id=container_id,
                image_version=type_info['version'],
            )
            self._db.log_audit(user_id, 'launch', agent_type, instance_id,
                               {'container': container_name,
                                'image_version': type_info['version']})

            return str(instance_id), f'{type_info["display_name"]} launched successfully.'

        except Exception as e:
            logger.exception(f"Failed to launch {container_name}")
            msg = self._friendly_launch_error(e, type_info)
            self._db.update_instance_state(
                instance_id, 'error', error_message=msg[:500])
            self._db.log_audit(user_id, 'launch_failed', agent_type, instance_id,
                               {'error': str(e)[:200]})
            return str(instance_id), msg

    @staticmethod
    def _friendly_launch_error(exc, type_info) -> str:
        """Translate raw docker errors into a clear, non-leaky user message
        (PR #84 review). A missing image (module not enabled / image never
        built or pulled on this box) is the common case for openhands /
        paperclip when their profile is off — surface an actionable message
        instead of a raw `404 Client Error ... No such image`.
        """
        text = str(exc)
        try:
            import docker.errors as _de
            is_missing = isinstance(exc, _de.ImageNotFound)
        except Exception:  # noqa: BLE001
            is_missing = False
        if is_missing or 'No such image' in text or 'not found' in text.lower() and 'image' in text.lower():
            image = f"{type_info.get('image', '?')}:{type_info.get('version', '?')}"
            display = type_info.get('display_name', type_info.get('id', 'This agent'))
            return (f"{display} isn't available on this box yet — its container "
                    f"image ({image}) hasn't been built or pulled. Enable the "
                    f"agent's module / run the post-install image pre-pull, then "
                    f"try again.")
        return f'Launch failed: {text}'

    def _companion_name(self, instance, type_info) -> str | None:
        """Return companion container name for this instance, or None.

        M020 S02 — companion containers are named `<primary>-<suffix>` where
        suffix defaults to 'workspace'. type_info may be None (e.g. when the
        catalog has been reseeded but the instance pre-dates the change);
        in that case we fall back to no-companion for graceful degradation.
        """
        if not type_info or not type_info.get('companion_image'):
            return None
        suffix = type_info.get('companion_suffix') or 'workspace'
        return f"{instance['container_name']}-{suffix}"

    def start(self, instance_id, username: str) -> tuple[str, str]:
        """Start a stopped instance (and its companion if any).

        M031-FOLLOWUPS A1: if the container is missing entirely (e.g. a
        previous failed upgrade removed it before failing to recreate),
        delegate to upgrade() to recreate from scratch with the current
        catalog. Volumes are named by user_slug and survive the recreate.
        Without this self-heal the operator is stuck — start() 404s on the
        missing container, and the dashboard offers no other action.
        """
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'
        if instance['state'] == 'running':
            return str(instance_id), 'Already running.'

        type_info = self._catalog.get_type(instance['agent_type'])

        # A1 self-heal: if the container is gone, run upgrade() which will
        # see the missing-container case (its remove step is already
        # tolerant of NotFound), then re-create with the current catalog.
        if self._docker.get_container_state(instance['container_name']) is None:
            logger.warning(
                f"start: container {instance['container_name']} missing — "
                f"falling back to upgrade()/recreate (A1 self-heal)"
            )
            return self.upgrade(instance_id, username)

        try:
            self._docker.start_container(instance['container_name'])
            companion = self._companion_name(instance, type_info)
            if companion:
                try:
                    self._docker.start_container(companion)
                except Exception as ce:
                    logger.warning(f"Failed to start companion {companion}: {ce} (primary started ok)")

            ports = (json.loads(instance['ports'])
                     if isinstance(instance['ports'], str) else instance['ports'])
            # Caddy route targets the user-facing UI — companion when present,
            # primary otherwise. Matches the launch-time logic.
            route_target = companion or instance['container_name']
            self._caddy.register_route(
                instance['agent_type'], instance['user_slug'],
                route_target, ports['internal'], username,
                instance_id=instance_id,
            )
            # PR #84 C1: (re)register the per-instance Authentik forward-auth
            # provider on start (idempotent — was deregistered on stop).
            self._register_authentik(instance['agent_type'], instance_id)

            self._db.update_instance_state(instance_id, 'running')
            self._db.log_audit(instance['user_id'], 'start', instance['agent_type'], instance_id)
            return str(instance_id), f'{instance["type_display_name"]} started.'
        except Exception as e:
            logger.exception(f"Failed to start {instance['container_name']}")
            return str(instance_id), f'Start failed: {e}'

    def update_memory(self, instance_id, username: str,
                      user_groups: list[str], mem_gb) -> tuple[str | None, str]:
        """#36 / PR #84 — change an instance's memory limit from the settings
        page. SERVER-SIDE tier-gated + budget-checked (the UI only reflects it).

        Sequence:
          1. Load the instance; resolve the caller's tier.
          2. L1 tier gate: only power/admin may change memory. A regular user
             is refused (returns None + a clear reason).
          3. Resolve + clamp the requested value (per-instance max).
          4. L2 + L3: refuse if the DELTA would exceed the global budget / the
             per-user cap (exclude this instance's current allocation).
          5. Apply: on a RUNNING container, `docker update --memory` LIVE (no
             restart). If the live update fails, fall back to a recreate via
             upgrade() (which reads the persisted mem_limit) and warn.
          6. Persist the chosen limit on instance.config so it survives a
             recreate and the budget sums see it.

        Returns (instance_id_str, message); instance_id_str is None on a hard
        refusal (not-found / tier-denied / budget-denied).
        """
        import uuid
        if isinstance(instance_id, str):
            # Best-effort UUID coercion (the API already validated the form).
            try:
                instance_id = uuid.UUID(instance_id)
            except (ValueError, TypeError):
                pass

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'

        tier = self._db.resolve_user_tier(user_groups)
        if not _tier_allows_custom_memory(tier):
            return None, ('Your tier does not permit changing agent memory. '
                          'Ask an admin to raise it.')

        type_info = self._catalog.get_type(instance['agent_type'])
        new_mem = self.resolve_mem_limit(tier, type_info or {}, mem_gb)

        # Current per-instance allocation (exclude it from the sums so only the
        # DELTA counts against budget/cap).
        from app.services.database import parse_mem_to_mb
        cfg = instance.get('config') or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except (ValueError, TypeError):
                cfg = {}
        current_mb = parse_mem_to_mb(cfg.get('mem_limit')
                                     or (type_info or {}).get('mem_limit') or '0')

        ok, reason = self.check_memory_budget(
            instance['user_slug'], new_mem, exclude_instance_mb=current_mb)
        if not ok:
            return None, reason

        # Persist first so a recreate (fallback) reads the new value.
        cfg['mem_limit'] = new_mem
        self._db.update_instance_config(instance_id, cfg)

        applied_live = False
        if instance['state'] == 'running':
            try:
                self._docker.update_container_memory(
                    instance['container_name'], new_mem)
                applied_live = True
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"update_memory: live docker update failed for "
                    f"{instance['container_name']} ({e}); falling back to recreate")
                iid, msg = self.upgrade(instance_id, username,
                                        target_version=instance.get('image_version'))
                self._db.log_audit(
                    instance['user_id'], 'update_memory', instance['agent_type'],
                    instance_id, {'mem_limit': new_mem, 'method': 'recreate'})
                return iid, (f'Memory set to {new_mem} — the agent was restarted '
                             f'to apply it (live update was not possible).')

        self._db.log_audit(
            instance['user_id'], 'update_memory', instance['agent_type'],
            instance_id,
            {'mem_limit': new_mem, 'method': 'live' if applied_live else 'persisted'})
        if applied_live:
            return str(instance_id), f'Memory updated to {new_mem} (applied live).'
        return str(instance_id), (f'Memory set to {new_mem}; it takes effect the '
                                   f'next time this agent starts.')

    def reconcile_routes(self) -> dict:
        """Re-register the Caddy admin-API route for every running instance.

        Per-user agent subdomains ({type}-{token}.agents.<domain>) are
        registered in Caddy via its admin API at launch/start time. Caddy
        boots from its static Caddyfile (`caddy run --config`), which does
        NOT carry these dynamic routes — so any Caddy restart or box reboot
        drops every per-instance route. The agent container itself keeps
        running (restart:unless-stopped relaunches its backends), and the
        static `*.agents` wildcard still resolves to this manager's httpx
        fallback proxy, so the instance page still *opens*. But that
        fallback cannot upgrade WebSockets — so the in-agent terminals
        report "Websocket not connected" and the dashboard restart buttons
        fail, until the user deletes + redeploys (which calls
        register_route again).

        This reconcile restores the direct, WS-capable routes without that
        dance. It is idempotent (skips routes already present) and is run
        periodically by the lifecycle scheduler, so it self-heals on boot,
        on a Caddy restart, and for containers that come up after this
        manager has already started.

        Returns a summary dict: {checked, reconciled, skipped, failed}.
        """
        summary = {'checked': 0, 'reconciled': 0, 'skipped': 0, 'failed': 0}
        try:
            instances = self._db.get_all_instances()
        except Exception:
            logger.exception("reconcile_routes: could not enumerate instances")
            return summary

        for row in instances:
            if row.get('state') != 'running':
                continue
            summary['checked'] += 1
            try:
                # PR #84 C1: reconcile the per-instance Authentik provider for
                # EVERY running instance (idempotent), independent of the Caddy
                # route check below — a Caddy route can survive a reboot while
                # the Authentik provider needs re-registering (or vice-versa),
                # so we must not skip this on the route-exists fast path.
                self._register_authentik(row['agent_type'], row['id'])
                # Already-present route → nothing more to do (cheap GET).
                if self._caddy.route_exists(row['agent_type'], row['id']):
                    summary['skipped'] += 1
                    continue
                # Only register for containers that are actually up; a
                # user-driven start() will register the rest.
                instance = self._db.get_instance(row['id'])
                if self._docker.get_container_state(
                        instance['container_name']) != 'running':
                    summary['skipped'] += 1
                    continue
                type_info = self._catalog.get_type(instance['agent_type'])
                ports = (json.loads(instance['ports'])
                         if isinstance(instance['ports'], str)
                         else instance['ports'])
                # Route targets the user-facing UI — companion when present,
                # primary otherwise. Mirrors launch()/start().
                companion = self._companion_name(instance, type_info)
                route_target = companion or instance['container_name']
                ok = self._caddy.register_route(
                    instance['agent_type'], instance['user_slug'],
                    route_target, ports['internal'], instance['user_slug'],
                    instance_id=instance['id'],
                )
                # (Authentik provider already reconciled at the top of the loop.)
                if ok:
                    summary['reconciled'] += 1
                    logger.info(
                        "reconcile_routes: restored Caddy route for %s",
                        instance['container_name'])
                else:
                    summary['failed'] += 1
            except Exception:
                summary['failed'] += 1
                logger.exception(
                    "reconcile_routes: failed for instance %s", row.get('id'))

        # PR #84 C1 (LOW-2): sweep orphan Authentik forward-auth providers/apps
        # whose instance no longer exists (a failed-delete deregister or an
        # upgrade leftover). Build the set of live per-instance hosts from ALL
        # current DB instances (not just running — a stopped instance keeps its
        # provider only while running, but we key the sweep on existence so a
        # stopped-then-restarted instance isn't reaped mid-cycle). Best-effort.
        if self._authentik:
            try:
                live_hosts = set()
                for row in instances:
                    try:
                        live_hosts.add(self._instance_host(row['agent_type'], row['id']))
                    except Exception:  # noqa: BLE001
                        pass
                sweep = self._authentik.sweep_orphans(live_hosts)
                if sweep.get('orphans_removed') or sweep.get('failed'):
                    logger.info("reconcile_routes authentik sweep: %s", sweep)
            except Exception as e:  # noqa: BLE001
                logger.warning("reconcile_routes authentik sweep failed: %s", e)

        if summary['reconciled'] or summary['failed']:
            logger.info("reconcile_routes summary: %s", summary)
        return summary

    def stop(self, instance_id, username: str) -> tuple[str, str]:
        """Stop a running instance (preserve container + volumes). Includes companion if any."""
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'

        type_info = self._catalog.get_type(instance['agent_type'])
        companion = self._companion_name(instance, type_info)
        if companion:
            try:
                self._docker.stop_container(companion)
            except Exception as ce:
                logger.warning(f"Failed to stop companion {companion}: {ce}")
        self._docker.stop_container(instance['container_name'])
        self._caddy.remove_route(instance['agent_type'], instance['user_slug'],
                                 instance_id=instance_id)
        # PR #84 C1: deregister the per-instance Authentik provider on stop
        # (re-registered on start). Keeps the outpost's provider list tidy.
        self._deregister_authentik(instance['agent_type'], instance_id)
        self._db.update_instance_state(instance_id, 'stopped')
        self._db.log_audit(instance['user_id'], 'stop', instance['agent_type'], instance_id)
        return str(instance_id), f'{instance["type_display_name"]} stopped.'

    def restart(self, instance_id, username: str) -> tuple[str | None, str]:
        """ga.2 (#219): restart a RUNNING instance in place (docker restart of
        the primary + companion), preserving all state.

        Non-destructive: the container, its named volumes, the Caddy route
        (its upstream is the unchanged container_name via docker DNS) and the
        per-instance Authentik provider all stay in place — only the process
        inside is bounced (SIGTERM → grace → SIGKILL → start). This is the
        recover-a-wedged-agent action (hung UI, stuck runtime) that keeps
        chats/skills/files/config intact.

        Only valid for a RUNNING instance: a stopped one must be *started*
        (Start re-registers the route + provider, which a bare docker restart
        would not), and a missing container is pointed at Start (which
        self-heals/recreates from the catalog). The DB state stays 'running'
        throughout — we never flip it, so the dashboard doesn't flicker to
        'stopped' mid-bounce.

        Returns (instance_id_str, message); instance_id_str is None only on
        not-found. A restart failure returns (id, 'Restart failed: …') so the
        API surfaces it as a 5xx and the dashboard renders the reason.
        """
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'
        if instance['state'] != 'running':
            return str(instance_id), 'Instance is not running — use Start instead.'
        if self._docker.get_container_state(instance['container_name']) is None:
            return str(instance_id), 'Container is missing — use Start to recreate it.'

        type_info = self._catalog.get_type(instance['agent_type'])
        companion = self._companion_name(instance, type_info)
        try:
            self._docker.restart_container(instance['container_name'])
            if companion:
                try:
                    self._docker.restart_container(companion)
                except Exception as ce:
                    logger.warning(
                        f"Failed to restart companion {companion}: {ce} "
                        f"(primary restarted ok)")
            self._db.log_audit(instance['user_id'], 'restart',
                               instance['agent_type'], instance_id)
            return str(instance_id), f'{instance["type_display_name"]} restarted.'
        except Exception as e:
            logger.exception(f"Failed to restart {instance['container_name']}")
            return str(instance_id), f'Restart failed: {e}'

    def upgrade(self, instance_id, username: str,
                target_version: str | None = None,
                force: bool = False) -> tuple[str | None, str]:
        """M030-S2: in-place upgrade of an instance to a new image version.

        ``force=True`` (ga.1 iter3) skips the "same version + same image digest →
        nothing to do" short-circuit and ALWAYS stops + recreates the container
        with freshly-resolved env. Used by post-install's coding-agent re-key to
        re-inject the live GPUStack key into instances that were provisioned while
        the agent-manager still held the placeholder key (a pure env refresh, not
        an image change).

        Sequence:
          1. Look up the instance + current catalog type_info
          2. Determine target version (catalog default unless overridden)
          3. Run per-agent pre_stop_command (Q6 decision — moltis SQLite WAL
             flush, openhands runtime drain, etc.) — best-effort, timeboxed
          4. Stop + remove containers (companion first); DO NOT remove volumes
          5. Re-create container(s) with the SAME named volumes (catalog
             vol_specs are unchanged; create_volume is idempotent for an
             existing name) but with the new image:version + freshly-resolved
             env (in case env_template gained new keys).
          6. Re-register Caddy route, mark instance running
          7. Stamp image_version + last_upgraded_at; audit-log "upgrade"
          8. On failure between steps 4-6, leave volumes intact and DB in
             "error" state so the operator can investigate or roll back via
             a fresh upgrade(target_version=<old>).

        Returns (instance_id_str, message). instance_id_str is None on
        not-found / invalid-input; the message is user-facing and surfaced
        in the dashboard toast.
        """
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'

        type_info = self._catalog.get_type(instance['agent_type'])
        if not type_info:
            return str(instance_id), f'Catalog type {instance["agent_type"]!r} not found — cannot upgrade.'

        # Resolve target image:version. Default = catalog's current version.
        new_version = target_version or type_info['version']
        old_version = instance.get('image_version') or '(unknown)'
        # #36 follow-up — the `:latest`→`:latest` reprovision no-op fix.
        # A naive tag-string compare (`new_version == image_version`) treated a
        # rebuilt mutable tag (`razzfazz-stack-paperclip:latest`, coding-agent
        # `:latest`, …) as "nothing to do" and short-circuited — so a rebuild
        # was never picked up without a sentinel `image_version` bump. When the
        # tag matches, we now also compare the container's ACTUAL running image
        # ID against what the tag resolves to locally: if a rebuild moved the
        # tag to a new digest, the container is stale → fall through to a
        # recreate. Only skip when tag matches AND the digest is unchanged
        # (a genuine no-op). See project_coding_tools_latest_tag_no_autoupdate.
        if not force and new_version == instance.get('image_version'):
            image_ref = f"{type_info['image']}:{new_version}"
            tagged_id = self._docker.get_image_id(image_ref)
            running_id = self._docker.get_container_image_id(
                instance['container_name'])
            # Force a recreate only when we can positively confirm a drift
            # (both IDs known and different). Unknown/None on either side →
            # can't prove a rebuild, so keep the historical no-op behaviour.
            stale = bool(tagged_id and running_id and tagged_id != running_id)
            if not stale:
                return str(instance_id), f'Already on {new_version}; nothing to do.'
            logger.info(
                "upgrade: %s tag %s unchanged but image rebuilt "
                "(container %s → tag %s); recreating onto the new image.",
                instance['container_name'], new_version,
                (running_id or '')[:19], (tagged_id or '')[:19])

        # 1. Pre-stop hook (Q6) — clean-shutdown the runtime before docker stop.
        self._run_pre_stop(instance, type_info)

        # 2. Stop + remove containers (companion first to avoid dangling reference).
        #    CRITICAL: remove_container without -v so named volumes survive.
        companion = self._companion_name(instance, type_info)
        if companion:
            try:
                self._docker.remove_container(companion)
            except Exception as ce:
                logger.warning(f"upgrade: failed to remove companion {companion}: {ce}")
        try:
            self._docker.remove_container(instance['container_name'])
        except Exception as e:
            logger.exception(f"upgrade: failed to remove primary {instance['container_name']}")
            self._db.update_instance_state(instance_id, 'error', error_message=f'remove failed: {e}')
            return str(instance_id), f'Upgrade failed during stop: {e}'

        self._caddy.remove_route(instance['agent_type'], instance['user_slug'],
                                 instance_id=instance_id)

        # 3. Re-create with new image. Volumes get re-created by name
        #    (idempotent) and Docker reattaches them.
        try:
            volumes = {}
            for vol_spec in (json.loads(type_info['volumes'])
                             if isinstance(type_info['volumes'], str)
                             else type_info['volumes']):
                if 'host_path' in vol_spec:
                    host_path = (vol_spec['host_path']
                                 .replace('{{STACK_HOST_PATH}}',
                                          os.environ.get('STACK_HOST_PATH', '')))
                    volumes[host_path] = vol_spec['mount']
                else:
                    vol_name = (f"agent-{instance['agent_type']}-"
                                f"{instance['user_slug']}-{vol_spec['name_suffix']}")
                    self._docker.create_volume(vol_name)  # idempotent
                    volumes[vol_name] = vol_spec['mount']

            instance_config = (instance.get('config') or {})
            if isinstance(instance_config, str):
                instance_config = json.loads(instance_config)
            # #36 / PR #84 — recreate with the SAME persisted per-instance
            # mem_limit (a power user's raised memory survives an upgrade);
            # fall back to the catalog default for pre-governance instances.
            recreate_mem = instance_config.get('mem_limit') or type_info['mem_limit']
            env = self._resolve_env(type_info, instance['user_slug'], username, instance_config,
                                    instance_id=instance['id'])
            # #36 gap 2 — re-merge the user's MCP-proxy endpoints on relaunch
            # (picks up MCPs connected since the last launch).
            env = self._inject_user_mcp(env, instance['agent_type'], instance['user_slug'])
            ports = (json.loads(type_info['ports'])
                     if isinstance(type_info['ports'], str) else type_info['ports'])
            labels = {
                'razzfazz.managed': 'true',
                'razzfazz.agent.type': instance['agent_type'],
                'razzfazz.agent.user': instance['user_slug'],
                'razzfazz.agent.instance': str(instance_id),
            }

            container_id = self._docker.create_container(
                name=instance['container_name'],
                image=type_info['image'],
                version=new_version,
                environment=env,
                volumes=volumes,
                mem_limit=recreate_mem,
                cpu_limit=type_info['cpu_limit'],
                # #221 — per-type PID cap (coding family = 2048); None falls back
                # to docker_client's 512 fork-bomb-hardening default.
                pids_limit=type_info.get('pids_limit'),
                docker_socket=type_info['requires_docker_socket'],
                labels=labels,
                command=type_info.get('command') or None,
                entrypoint=type_info.get('entrypoint') or None,
                # #36 security-review — keep the sandbox on upgrade/recreate too.
                sandbox=_is_sandboxed(instance['agent_type'], type_info),
            )
            self._docker.start_container(container_id)

            # Companion (e.g. hermes-workspace) — same dance with new version.
            if type_info.get('companion_image'):
                companion_suffix = type_info.get('companion_suffix') or 'workspace'
                companion_name = f"{instance['container_name']}-{companion_suffix}"
                companion_volumes = {}
                for vol_spec in (json.loads(type_info.get('companion_volumes') or '[]')
                                 if isinstance(type_info.get('companion_volumes'), str)
                                 else (type_info.get('companion_volumes') or [])):
                    vol_name = (f"agent-{instance['agent_type']}-"
                                f"{instance['user_slug']}-{vol_spec['name_suffix']}")
                    self._docker.create_volume(vol_name)
                    companion_volumes[vol_name] = vol_spec['mount']
                companion_env = self._resolve_env(
                    type_info, instance['user_slug'], username, instance_config,
                    template_field='companion_env_template',
                    instance_id=instance['id'],
                )
                companion_labels = {**labels, 'razzfazz.agent.companion': 'true'}
                companion_id = self._docker.create_container(
                    name=companion_name,
                    image=type_info['companion_image'],
                    version=type_info.get('companion_version') or 'latest',
                    environment=companion_env,
                    volumes=companion_volumes,
                    mem_limit=type_info['mem_limit'],
                    cpu_limit=type_info['cpu_limit'],
                    docker_socket=False,
                    labels=companion_labels,
                    # #36: same companion command override as the provision
                    # path — the upgrade()'d workspace must re-seed its
                    # config.yaml too (fresh volume or new image).
                    command=type_info.get('companion_command') or None,
                )
                self._docker.start_container(companion_id)

            # Re-register Caddy route to the (possibly companion) target.
            route_target = (instance['container_name'] if not type_info.get('companion_image')
                            else f"{instance['container_name']}-"
                                 f"{type_info.get('companion_suffix') or 'workspace'}")
            self._caddy.register_route(
                instance['agent_type'], instance['user_slug'], route_target,
                ports['internal'], username, instance_id=instance_id,
            )
            # PR #84 C1: (re)register the per-instance Authentik provider after
            # the upgrade recreate (idempotent — the host/token is unchanged).
            self._register_authentik(instance['agent_type'], instance_id)

            # 4. Mark upgraded — image_version + last_upgraded_at + state running.
            self._db.update_instance_state(
                instance_id, 'running',
                container_id=container_id, image_version=new_version,
            )
            self._db.mark_instance_upgraded(instance_id, new_version)
            self._db.log_audit(
                instance['user_id'], 'upgrade', instance['agent_type'], instance_id,
                {'old_version': old_version, 'new_version': new_version},
            )
            # #36 follow-up: on a same-tag rebuild the human message
            # `latest → latest` is confusing — say it was recreated instead.
            if old_version == new_version:
                return str(instance_id), (
                    f'{instance["type_display_name"]} recreated onto the '
                    f'rebuilt {new_version} image.')
            return str(instance_id), (f'{instance["type_display_name"]} upgraded '
                                       f'{old_version} → {new_version}.')

        except Exception as e:
            logger.exception(f"upgrade: failed to recreate {instance['container_name']}")
            self._db.update_instance_state(
                instance_id, 'error', error_message=f'upgrade rollout failed: {e}')
            self._db.log_audit(
                instance['user_id'], 'upgrade_failed', instance['agent_type'], instance_id,
                {'error': str(e)[:200], 'attempted_version': new_version},
            )
            return str(instance_id), (f'Upgrade failed: {e}. Volumes preserved; '
                                       f'try again or contact admin.')

    def _run_pre_stop(self, instance, type_info) -> None:
        """M030-S2 Q6: run a per-agent clean-shutdown command via docker exec
        before stopping the container. Best-effort — failures and timeouts
        log a warning but don't block the stop.

        Catalog field shape:
          'pre_stop_command': ['sh', '-c', '...'] | None
          'pre_stop_timeout': 10 | None  (seconds)

        Examples:
          moltis: PRAGMA wal_checkpoint(FULL) on every .db file in
                  /home/moltis/.moltis to flush SQLite WAL → main DB.
        """
        cmd = type_info.get('pre_stop_command')
        if not cmd:
            return
        if isinstance(cmd, str):
            try:
                cmd = json.loads(cmd)
            except (ValueError, json.JSONDecodeError):
                logger.warning(f"pre_stop_command for {type_info['id']} is a string but not JSON; skipping")
                return
        timeout = type_info.get('pre_stop_timeout') or 10
        try:
            logger.info(f"pre-stop hook for {instance['container_name']}: {cmd}")
            self._docker.exec_in_container(instance['container_name'], cmd, timeout=timeout)
        except Exception as e:
            logger.warning(f"pre_stop hook failed for {instance['container_name']}: {e} "
                           f"(continuing with stop anyway)")

    def delete(self, instance_id, username: str) -> tuple[str, str]:
        """Delete an instance — remove container(s), volumes, and database."""
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'

        type_info = self._catalog.get_type(instance['agent_type'])

        # Stop and remove containers (companion first to avoid dangling reference)
        companion = self._companion_name(instance, type_info)
        if companion:
            try:
                self._docker.remove_container(companion)
            except Exception as ce:
                logger.warning(f"Failed to remove companion {companion}: {ce}")
        self._docker.remove_container(instance['container_name'])
        self._caddy.remove_route(instance['agent_type'], instance['user_slug'],
                                 instance_id=instance_id)
        # PR #84 C1: deregister the per-instance Authentik provider on delete.
        self._deregister_authentik(instance['agent_type'], instance_id)

        # Remove volumes — primary + companion
        if type_info:
            for field in ('volumes', 'companion_volumes'):
                raw = type_info.get(field) or '[]'
                vol_specs = (json.loads(raw) if isinstance(raw, str) else raw)
                for vol_spec in vol_specs:
                    # rc6.7 #91: skip bind-mount specs — they reference host
                    # paths the manager doesn't own.
                    if 'host_path' in vol_spec:
                        continue
                    vol_name = f"agent-{instance['agent_type']}-{instance['user_slug']}-{vol_spec['name_suffix']}"
                    try:
                        self._docker.remove_volume(vol_name)
                    except Exception as ve:
                        logger.warning(f"Failed to remove volume {vol_name}: {ve}")

        # Drop per-instance database
        if type_info and type_info['requires_db']:
            self._drop_instance_db(instance['agent_type'], instance['user_slug'])

        self._db.delete_instance(instance_id)
        self._db.log_audit(instance['user_id'], 'delete', instance['agent_type'], instance_id)
        return str(instance_id), f'{instance["type_display_name"]} deleted.'

    def _resolve_env(self, type_info: dict, user_slug: str,
                     username: str, instance_config: dict,
                     template_field: str = 'env_template',
                     instance_id=None) -> dict:
        """Resolve env_template placeholders to concrete values.

        `template_field` selects which JSONB column to resolve — defaults to
        'env_template' (primary container); pass 'companion_env_template' for
        the bundled companion (M020 S02).

        `instance_id` (M031-FOLLOWUPS B1/B2) — when provided, exposes
        {{instance_hash}} (the 8-hex-char DNS token from caddy_client) so
        env vars like SANDBOX_CONTAINER_URL_PATTERN and AGENT_INSTANCE_HOSTNAME
        can use the same hostname Caddy registers (`<type>-<hash>.agents.<domain>`).
        Fixes the long-standing "openhands-{slug} ≠ openhands-{hash}" mismatch
        and unblocks paperclip's auto-allowlist registration.
        """
        raw = type_info.get(template_field) or '{}'
        template = (json.loads(raw) if isinstance(raw, str) else raw)
        env = {}
        import os
        if not template:
            return env
        # Compute instance_hash if available
        instance_hash = ''
        if instance_id is not None:
            try:
                from app.services.caddy_client import instance_token
                instance_hash = instance_token(instance_id)
            except Exception:
                pass
        for key, value in template.items():
            resolved = str(value)
            resolved = resolved.replace('{{user_slug}}', user_slug)
            resolved = resolved.replace('{{user_id}}', username)
            resolved = resolved.replace('{{instance_hash}}', instance_hash)
            resolved = resolved.replace('{{generated_secret}}',
                                        instance_config.get('_generated_secret', ''))
            resolved = resolved.replace('{{MAIN_DOMAIN}}',
                                        os.environ.get('MAIN_DOMAIN', 'localhost'))
            resolved = resolved.replace('{{AGENTS_DOMAIN}}',
                                        os.environ.get('AGENTS_DOMAIN',
                                            f"agents.{os.environ.get('MAIN_DOMAIN', 'localhost')}"))
            resolved = resolved.replace('{{GPUSTACK_API_KEY}}',
                                        os.environ.get('GPUSTACK_API_KEY', ''))
            resolved = resolved.replace('{{VALKEY_PASSWORD}}',
                                        os.environ.get('VALKEY_PASSWORD', ''))
            # ── Gitea checkout wiring (#165) ─────────────────────────────────
            # {{gitea_token}} → the PER-USER token minted at launch (stored on
            # instance_config so it survives an upgrade/recreate); empty when
            # Gitea is off / the user isn't in Gitea yet. {{GITEA_EXTERNAL_URL}}
            # / {{GITEA_INTERNAL_URL}} give the container the external form to
            # rewrite (insteadOf) and the reachable internal endpoint.
            resolved = resolved.replace('{{gitea_token}}',
                                        instance_config.get('_gitea_token', ''))
            resolved = resolved.replace('{{GITEA_EXTERNAL_URL}}',
                                        _gitea_external_url())
            resolved = resolved.replace('{{GITEA_INTERNAL_URL}}',
                                        _gitea_internal_url())
            resolved = resolved.replace('{{AUTHENTIK_BOOTSTRAP_PASSWORD}}',
                                        os.environ.get('AUTHENTIK_BOOTSTRAP_PASSWORD', ''))
            # Per-instance DB placeholders
            db_name = f"agent_{type_info['id']}_{user_slug}_db"
            db_user = f"agent_{type_info['id']}_{user_slug}"
            db_pass = instance_config.get('_db_password', '')
            resolved = resolved.replace('{{instance_db_name}}', db_name)
            resolved = resolved.replace('{{instance_db_user}}', db_user)
            resolved = resolved.replace('{{instance_db_password}}', db_pass)
            resolved = resolved.replace('{{valkey_db_index}}', '10')
            env[key] = resolved
        return env

    def _inject_user_mcp(self, env: dict, agent_type: str, user_slug: str) -> dict:
        """Merge the user's OWN running MCP-proxy endpoints into a resolved env.

        #36 gap 2 — per-user personal MCPs. Distinct from the stack-wide MCP
        registry, which mcp_config bakes into the static catalog at seed time
        (no user context). Here we pull THIS user's proxies from mcp-manager at
        launch time and merge per agent type:
          moltis        -> MOLTIS_MCP__SERVERS__* env keys (moltis maps the
                           double-underscore env to [mcp.servers.<id>])
          coding-tools  -> merged under the `mcp` key of OPENCODE_CONFIG_JSON
                           (preserving the stack-wide entries already there)
          hermes        -> RAZZFAZZ_USER_MCP_JSON env carrying the STRUCTURED
                           [{id,url}] specs as JSON; the hermes boot script parses
                           it in Python and execs `hermes mcp add` with a quoted
                           argv list (NO shell, NO eval).

        SECURITY (commit-review CRITICAL #2): the hermes wiring is passed as
        structured JSON DATA, never a shell-command string — the prior
        `eval`-based approach was a command-injection sink for cross-service /
        user-influenced ids/urls. ids are strict-validated upstream in
        agent_wiring; here we only carry data.

        Idempotent and only-own: the endpoint returns a single user's proxies,
        and re-running with the same proxy set produces the same env. Fail-safe:
        a missing/empty wiring leaves `env` unchanged.
        """
        # opencode/coding-tools and hermes/moltis were the original MCP consumers.
        # #36 follow-up: the sandboxed coding agents (codex + whatever runs Claude
        # Code / gsd-pi / pi) also consume per-user MCP. `codex` gets its own
        # ~/.codex/config.toml [mcp_servers.*] block; ALL coding-agent kinds get a
        # project /workspace/.mcp.json for Claude Code (installed by the user into
        # any coding-agent container). So the coding-agent kinds are wired too.
        _CODING_AGENT_KINDS = ("coding-tools", "opencode", "codex",
                               "gsd-pi", "user-defined")
        if agent_type not in (("moltis", "hermes") + _CODING_AGENT_KINDS):
            return env
        from app.services import mcp_manager_client
        wiring = mcp_manager_client.fetch_user_wiring(user_slug)

        if agent_type == "moltis":
            env.update(wiring.get("moltis_env", {}))
        elif agent_type in _CODING_AGENT_KINDS:
            block = wiring.get("opencode_block", {})
            if block:
                raw = env.get("OPENCODE_CONFIG_JSON")
                try:
                    cfg = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    cfg = {}
                cfg.setdefault("mcp", {}).update(block)
                env["OPENCODE_CONFIG_JSON"] = json.dumps(cfg)
            # Claude Code .mcp.json (project-scoped): the entrypoint writes a
            # MANAGED block into /workspace/.mcp.json every boot, never clobbering
            # the user's manual `claude mcp add` entries. Carried as JSON DATA
            # (no shell) — same safety posture as the hermes specs.
            claude = wiring.get("claude_mcp", {}) or {}
            servers = claude.get("mcpServers") if isinstance(claude, dict) else None
            if servers:
                env["RAZZFAZZ_CLAUDE_MCP_JSON"] = json.dumps(servers)
            # Codex ~/.codex/config.toml [mcp_servers.*]: only for the codex kind.
            if agent_type == "codex":
                codex_block = wiring.get("codex_mcp", {}) or {}
                if codex_block:
                    env["RAZZFAZZ_CODEX_MCP_JSON"] = json.dumps(codex_block)
        elif agent_type == "hermes":
            specs = wiring.get("hermes_specs", [])
            if specs:
                # Structured JSON only — parsed + run with argv (no shell) in
                # the hermes boot script. Keep {id,url,headers}; headers carry
                # the per-proxy bearer (#61 CRITICAL-1) the boot script passes as
                # `--header`. headers omitted for a legacy bearer-less proxy.
                env["RAZZFAZZ_USER_MCP_JSON"] = json.dumps(
                    [{"id": s["id"], "url": s["url"],
                      **({"headers": s["headers"]} if s.get("headers") else {})}
                     for s in specs if s.get("id") and s.get("url")])
        return env

    def _mint_gitea_token(self, username: str, user_slug: str) -> str:
        """#165: best-effort mint of a PER-USER Gitea access token via the gitea
        admin CLI (`gitea admin user generate-access-token`), run with docker
        exec through the socket-proxy — the same EXEC path _run_pre_stop uses.

        PER-USER (never the shared box admin token) so one user's sandbox cannot
        reach a peer's repos. Returns '' (non-fatal) when:
          * the `gitea` profile is inactive (no gitea container),
          * the user hasn't logged into Gitea yet (OIDC auto-provision — the CLI
            errors "user does not exist"), or
          * the CLI otherwise errors.
        In every '' case the coding-agent entrypoint skips the credential block;
        the user can still clone PUBLIC repos via the insteadOf rewrite and clone
        PRIVATE repos by pasting their own PAT in the web-UI "Clone from Gitea"
        flow (which GITEA_EXTERNAL_URL now surfaces).
        """
        profiles = [p.strip()
                    for p in os.environ.get('COMPOSE_PROFILES', '').split(',')]
        if 'gitea' not in profiles:
            return ''
        # Unique-per-instance token name so a re-provision never collides with a
        # leftover token from a previously-deleted instance of the same user.
        token_name = f"coding-agent-{user_slug}-{secrets.token_hex(3)}"
        cmd = ['su-exec', 'git', 'gitea', 'admin', 'user',
               'generate-access-token', '--username', username,
               '--token-name', token_name, '--raw',
               '--scopes', 'write:repository,read:user,read:organization']
        try:
            rc, out = self._docker.exec_in_container('gitea', cmd, timeout=15)
        except Exception as e:  # noqa: BLE001
            logger.warning("gitea token mint for %s failed: %s", username, e)
            return ''
        text = (out.decode('utf-8', 'replace')
                if isinstance(out, (bytes, bytearray)) else str(out or '')).strip()
        if rc != 0:
            logger.info("gitea token mint for %s rc=%s (%s) — skipping "
                        "(user can paste a PAT in the UI)",
                        username, rc, text[:120])
            return ''
        # `--raw` prints ONLY the token; take the last non-empty line + sanity-check.
        tok = ''
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line:
                tok = line
                break
        return tok if tok and ' ' not in tok and len(tok) <= 100 else ''

    def _create_instance_db(self, agent_type: str, user_slug: str,
                            instance_config: dict):
        """Create a per-instance PostgreSQL database and user."""
        import os
        db_name = f"agent_{agent_type}_{user_slug}_db"
        db_user = f"agent_{agent_type}_{user_slug}"
        db_pass = secrets.token_urlsafe(24)
        instance_config['_db_password'] = db_pass

        # Connect to postgres database (admin)
        admin_dsn = self._config['DATABASE_URL'].rsplit('/', 1)[0] + '/postgres'
        conn = psycopg2.connect(admin_dsn)
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{db_name}"')
                logger.info(f"Created database {db_name}")
            cur.execute(f"SELECT 1 FROM pg_roles WHERE rolname = %s", (db_user,))
            if not cur.fetchone():
                cur.execute(f"CREATE USER \"{db_user}\" WITH PASSWORD %s", (db_pass,))
                cur.execute(f'GRANT ALL PRIVILEGES ON DATABASE "{db_name}" TO "{db_user}"')
                logger.info(f"Created user {db_user}")
            else:
                # rc6.7 #72: re-provisioning a previously-destroyed instance
                # generates a fresh `_db_password` and hands it to the new
                # container via DATABASE_URL, but the existing postgres
                # role still carries the OLD password — auth fails with
                # `password authentication failed for user
                # "agent_paperclip_<slug>"` and the container restart-loops.
                # Sync the role's password to the freshly generated one.
                # Existing per-instance DB data is preserved (no DROP).
                cur.execute(f"ALTER USER \"{db_user}\" WITH PASSWORD %s", (db_pass,))
                logger.info(f"Reset password for existing user {db_user}")
            # Grant schema-level permissions (required for CREATE TABLE etc.)
            conn.close()
            conn = psycopg2.connect(admin_dsn.rsplit('/', 1)[0] + f'/{db_name}')
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(f'GRANT ALL ON SCHEMA public TO "{db_user}"')
            cur.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{db_user}"')
            cur.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{db_user}"')
            logger.info(f"Granted schema permissions to {db_user}")
        finally:
            cur.close()
            conn.close()

    def _drop_instance_db(self, agent_type: str, user_slug: str):
        """Drop a per-instance PostgreSQL database and user."""
        db_name = f"agent_{agent_type}_{user_slug}_db"
        db_user = f"agent_{agent_type}_{user_slug}"

        admin_dsn = self._config['DATABASE_URL'].rsplit('/', 1)[0] + '/postgres'
        conn = psycopg2.connect(admin_dsn)
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            cur.execute(f'DROP USER IF EXISTS "{db_user}"')
            logger.info(f"Dropped database {db_name} and user {db_user}")
        finally:
            cur.close()
            conn.close()
