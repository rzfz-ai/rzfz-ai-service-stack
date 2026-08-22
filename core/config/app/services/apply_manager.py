# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Apply manager — handles config changes, impact preview, and docker compose execution."""

import json
import os
import re
import subprocess
import threading
import time
import uuid


class ApplyAction:
    """Represents a running or completed apply action."""
    def __init__(self, action_id, description):
        self.action_id = action_id
        self.description = description
        self.lines = []
        self.done = False
        self.success = False
        self.start_time = time.time()
        self.duration_ms = 0
        self.error = None
        self._lock = threading.Lock()

    def add_line(self, text):
        with self._lock:
            self.lines.append(text)

    def finish(self, success, error=None):
        with self._lock:
            self.done = True
            self.success = success
            self.error = error
            self.duration_ms = int((time.time() - self.start_time) * 1000)

    def get_lines_from(self, index):
        with self._lock:
            return list(self.lines[index:]), self.done, self.success, self.error, self.duration_ms


class ApplyManager:
    def __init__(self, stack_root, profile_manager, resource_monitor, audit_logger,
                 config_manager=None):
        self.stack_root = stack_root
        self.env_path = os.path.join(stack_root, '.env')
        self.profile_manager = profile_manager
        self.resource_monitor = resource_monitor
        self.audit_logger = audit_logger
        # Optional — used by apply_profile_toggle to fill empty per-profile
        # secrets before container start. None is tolerated for back-compat
        # (older callers); profile enable then silently skips the
        # autoprovisioning step.
        self.config_manager = config_manager
        self.checksum_manager = None  # Set from app factory after init
        self._actions = {}
        self._current_action = None
        self._lock = threading.Lock()

    def _auto_checksum(self, comment):
        """Take an automatic governance checksum after a successful change."""
        if self.checksum_manager:
            try:
                self.checksum_manager.take_checksum(f'UI: {comment}', source='ui-auto')
            except Exception:
                pass

    def apply_service_restart(self, containers, description, user, source_ip,
                              category='settings', risk='caution'):
        """Restart specific containers after a settings change. Returns action_id."""
        with self._lock:
            if self._current_action and not self._current_action.done:
                return None, 'Another apply action is already running'

        action_id = str(uuid.uuid4())[:8]
        action = ApplyAction(action_id, description)

        with self._lock:
            self._current_action = action
            self._actions[action_id] = action

        thread = threading.Thread(
            target=self._execute_restart,
            args=(action, containers, description, user, source_ip, category, risk),
            daemon=True,
        )
        thread.start()
        return action_id, None

    def _execute_restart(self, action, containers, description, user, source_ip, category, risk):
        try:
            action.add_line(f'Restarting: {", ".join(containers)}...')
            cmd = ['docker', 'compose', 'up', '-d', '--no-deps', '--force-recreate'] + self._to_services(containers)
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=self.stack_root,
            )
            for line in iter(process.stdout.readline, ''):
                line = line.rstrip()
                if line:
                    action.add_line(line)
            exit_code = process.wait()

            if exit_code == 0:
                action.add_line('Done.')
                action.finish(success=True)
                self._auto_checksum(description)
                self.audit_logger.log(
                    'config.apply', user=user, source_ip=source_ip,
                    category=category, action='restart',
                    detail=description,
                    containers_affected=containers,
                    risk=risk, outcome='success',
                    duration_ms=action.duration_ms,
                )
            else:
                action.add_line(f'Error: exit code {exit_code}')
                action.finish(success=False, error=f'exit code {exit_code}')
        except Exception as e:
            action.add_line(f'Error: {e}')
            action.finish(success=False, error=str(e))

    # ga.15 re-domain: services that BAKE the domain (env / persisted config /
    # served host) and must be recreated so they follow a MAIN_DOMAIN change.
    # EXCLUDES: the LLM runtime (gpustack*/model-sync* — recreating them unloads
    # running models and they don't carry the domain), the shared datastores
    # (postgres/valkey), and the config UI ITSELF (we run inside it). Both
    # OpenWebUI service spellings are listed; only the running one is acted on.
    _DOMAIN_BAKING_SERVICES = [
        'caddy', 'authentik-server', 'authentik-worker',
        'openwebui', 'open-webui', 'pipelines',
        'dify-api', 'dify-web', 'dify-worker', 'dify-plugin-daemon',
        'gitea', 'start-portal', 'openlit', 'synapse', 'element',
    ]

    def apply_domain_change(self, old_domain, new_domain, user, source_ip, risk='danger'):
        """Full re-domain apply (ga.15). After ConfigManager.update_domain has
        rewritten the *_DOMAIN .env vars, this: (1) reconciles the OpenWebUI DB
        (webui.url / oauth.provider_url persist the old host and override env),
        (2) re-templates Authentik blueprints for the new domain (authentik-init;
        the #183 handler fires because MAIN_DOMAIN now differs — works offline via
        the baked init tools), (3) clears Authentik's cache so the re-templated
        sources/providers are served immediately, and (4) recreates every RUNNING
        domain-baking service so it re-reads the new host. Returns (action_id, err)."""
        with self._lock:
            if self._current_action and not self._current_action.done:
                return None, 'Another apply action is already running'
        action_id = str(uuid.uuid4())[:8]
        description = f'Re-domain {old_domain or "?"} -> {new_domain}'
        action = ApplyAction(action_id, description)
        with self._lock:
            self._current_action = action
            self._actions[action_id] = action
        thread = threading.Thread(
            target=self._execute_domain_change,
            args=(action, old_domain, new_domain, description, user, source_ip, risk),
            daemon=True,
        )
        thread.start()
        return action_id, None

    def _execute_domain_change(self, action, old_domain, new_domain, description,
                               user, source_ip, risk):
        targets = []
        try:
            env = self.config_manager.read_env() if self.config_manager else {}

            # 1) OpenWebUI persisted-domain reconcile.
            if old_domain and old_domain != new_domain:
                self._reconcile_openwebui_domain(action, env, old_domain, new_domain)
            else:
                action.add_line('OpenWebUI DB reconcile skipped (no prior domain).')

            # 2) Re-template Authentik blueprints for the new domain.
            action.add_line('Re-templating Authentik blueprints (authentik-init)...')
            self._run_stream(action, ['docker', 'compose', 'up', '-d', '--no-deps',
                                      '--force-recreate', 'authentik-init'])

            # 3) Clear Authentik's cache (Valkey) so re-templated sources/providers
            #    take effect without waiting for a TTL (the 2026-08 re-domain kept
            #    a cached OAuth source on the old host until the cache was cleared).
            action.add_line('Clearing Authentik cache...')
            self._run_capture(action, ['docker', 'exec', 'authentik-server', 'ak', 'shell',
                                       '-c', 'from django.core.cache import cache; '
                                       'cache.clear(); print("cache cleared")'], nonfatal=True)

            # 4) Recreate every RUNNING domain-baking service — one at a time,
            #    best-effort. A single service whose module has a compose quirk
            #    (e.g. a module appended directly to COMPOSE_FILE whose own
            #    `env_file: ../../.env` resolves oddly) must NOT abort the whole
            #    re-domain; the domain routing lives in Caddy + the Authentik
            #    providers (already updated above), so a skipped container just
            #    keeps a stale derived env until its next restart.
            running = self._running_services()
            targets = [s for s in self._DOMAIN_BAKING_SERVICES if s in running]
            if targets:
                action.add_line(f'Recreating domain-baking services (best-effort): {", ".join(targets)}')
                skipped = []
                for svc in targets:
                    try:
                        self._run_stream(action, ['docker', 'compose', 'up', '-d',
                                                  '--no-deps', '--force-recreate', svc])
                    except Exception as e:
                        skipped.append(svc)
                        action.add_line(f'  WARN: could not recreate {svc} ({e}); continuing')
                if skipped:
                    action.add_line(f'Recreated; skipped {len(skipped)}: {", ".join(skipped)}')
            else:
                action.add_line('No domain-baking services running to recreate.')

            action.add_line('Done.')
            action.finish(success=True)
            self._auto_checksum(description)
            self.audit_logger.log(
                'config.apply', user=user, source_ip=source_ip,
                category='settings', action='redomain', detail=description,
                containers_affected=targets, risk=risk, outcome='success',
                duration_ms=action.duration_ms,
            )
        except Exception as e:
            action.add_line(f'Error: {e}')
            action.finish(success=False, error=str(e))
            try:
                self.audit_logger.log(
                    'config.apply', user=user, source_ip=source_ip,
                    category='settings', action='redomain', detail=description,
                    containers_affected=targets, risk=risk, outcome='failure', error=str(e))
            except Exception:
                pass

    def _reconcile_openwebui_domain(self, action, env, old_domain, new_domain):
        """Rewrite the old domain to the new one inside the OpenWebUI persisted
        config JSON. Handles both schemas: ≤0.9.x single-row `data` column and
        0.10.x key-value `value` column. Best-effort (non-fatal)."""
        pg_user = env.get('POSTGRES_USER') or 'docker'
        pg_pw = env.get('POSTGRES_PASSWORD', '')
        owui_db = env.get('OPENWEBUI_DB') or 'openwebui_db'
        col = col_type = None
        for cand in ('data', 'value'):
            dtype = self._psql_scalar(
                pg_user, pg_pw, owui_db,
                "SELECT data_type FROM information_schema.columns "
                f"WHERE table_name='config' AND column_name='{cand}';")
            if dtype in ('json', 'jsonb'):
                col, col_type = cand, dtype
                break
        if not col:
            action.add_line('  OpenWebUI config has no json column — skipping reconcile (not initialised?).')
            return
        sql = (f"UPDATE config SET {col} = replace({col}::text, :'old', :'new')::{col_type} "
               f"WHERE {col}::text LIKE '%' || :'old' || '%';")
        action.add_line(f'Reconciling OpenWebUI persisted domain ({old_domain} -> {new_domain}, config.{col})...')
        # psql `:'var'` interpolation does NOT happen for the `-c` argument — it
        # only runs for SQL read from STDIN / a file (same reason post-install's
        # OWUI key-fix feeds SQL via a heredoc). So feed the statement on stdin.
        try:
            out = subprocess.run(
                ['docker', 'exec', '-i', '-e', f'PGPASSWORD={pg_pw}', 'postgres', 'psql',
                 '-v', 'ON_ERROR_STOP=1', '-U', pg_user, '-d', owui_db,
                 '-v', f'old={old_domain}', '-v', f'new={new_domain}'],
                input=sql, capture_output=True, text=True, timeout=60)
            for line in ((out.stdout or '') + (out.stderr or '')).strip().splitlines():
                action.add_line(f'  {line}')
            if out.returncode != 0:
                action.add_line(f'  OpenWebUI reconcile psql exited {out.returncode} (non-fatal)')
        except Exception as e:
            action.add_line(f'  OpenWebUI reconcile error (non-fatal): {e}')

    def _psql_scalar(self, pg_user, pg_pw, db, sql):
        """Run a scalar psql query inside the postgres container; '' on any error."""
        try:
            out = subprocess.run(
                ['docker', 'exec', '-e', f'PGPASSWORD={pg_pw}', 'postgres', 'psql',
                 '-U', pg_user, '-d', db, '-tAc', sql],
                capture_output=True, text=True, timeout=30)
            return out.stdout.strip()
        except Exception:
            return ''

    def _running_services(self):
        """Set of compose service names currently running (empty set on error)."""
        try:
            out = subprocess.run(
                ['docker', 'compose', 'ps', '--services', '--status', 'running'],
                capture_output=True, text=True, cwd=self.stack_root, timeout=30)
            return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}
        except Exception:
            return set()

    def _run_stream(self, action, cmd):
        """Run a command streaming stdout into the action; raise on non-zero exit."""
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, cwd=self.stack_root)
        for line in iter(proc.stdout.readline, ''):
            line = line.rstrip()
            if line:
                action.add_line(line)
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f'`{" ".join(cmd[:5])} ...` exited {code}')
        return code

    def _run_capture(self, action, cmd, nonfatal=False):
        """Run a command capturing combined output into the action; raise on
        non-zero unless nonfatal."""
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, cwd=self.stack_root)
        out, _ = proc.communicate()
        if out and out.strip():
            for line in out.strip().splitlines():
                action.add_line(f'  {line}')
        if proc.returncode != 0 and not nonfatal:
            raise RuntimeError(f'`{" ".join(cmd[:4])} ...` exited {proc.returncode}')
        return proc.returncode

    def apply_image_update(self, env_var, new_version, containers, description,
                           user, source_ip, profile_id=None):
        """Bump an image version in .env, pull, and recreate. Returns action_id.

        `profile_id` (#176): the module/profile this image belongs to, so the
        execute step can refuse to recreate a service whose profile is NOT in
        the active COMPOSE_PROFILES — see _execute_image_update for why.
        Callers that omit it (or pass 'core', which is always-on and never
        COMPOSE_PROFILES-gated) get the unconditional recreate behavior.
        """
        with self._lock:
            if self._current_action and not self._current_action.done:
                return None, 'Another apply action is already running'

        action_id = str(uuid.uuid4())[:8]
        action = ApplyAction(action_id, description)

        with self._lock:
            self._current_action = action
            self._actions[action_id] = action

        thread = threading.Thread(
            target=self._execute_image_update,
            args=(action, env_var, new_version, containers, description,
                  user, source_ip, profile_id),
            daemon=True,
        )
        thread.start()
        return action_id, None

    def _execute_image_update(self, action, env_var, new_version, containers,
                              description, user, source_ip, profile_id=None):
        try:
            # Step 1: Update .env (capture the old value so we can roll it back
            # if the pull/recreate fails — otherwise .env says the new version
            # while the image was never updated, and the item silently
            # disappears from the "updates available" list. Operator-reported.)
            action.add_line(f'Updating {env_var}={new_version} ...')
            old_version = self._read_env_var(env_var)
            self._update_env_var(env_var, new_version)

            # Step 2: Pull new image
            action.add_line(f'Pulling images for: {", ".join(containers)}...')
            pull = subprocess.Popen(
                ['docker', 'compose', 'pull'] + self._to_services(containers),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=self.stack_root,
            )
            for line in iter(pull.stdout.readline, ''):
                line = line.rstrip()
                if line:
                    action.add_line(line)
            pull_code = pull.wait()
            if pull_code != 0:
                action.add_line(f'Pull failed (exit {pull_code})')
                if old_version is not None:
                    self._update_env_var(env_var, old_version)
                    action.add_line(f'Reverted {env_var} to {old_version} (pull failed — '
                                    f'item stays in the updates list).')
                action.finish(success=False, error=f'docker compose pull failed')
                return

            # Step 2b (#176): a module whose profile is NOT in the active
            # COMPOSE_PROFILES must never be recreated here. `docker compose
            # up` starts the named service unconditionally, regardless of
            # profile gating — so recreating a disabled module's container
            # leaves it running while the UI still shows the module as
            # disabled (operator-reported orphan: openlit + searxng stayed
            # up after an "update available" bump). The new image is already
            # pulled above and .env already carries the new version, so the
            # module will come up on the new version the next time it's
            # enabled. `profile_id` of None/'core' means "not profile-gated"
            # (core infra always runs) — those keep the unconditional
            # recreate below.
            if (profile_id and profile_id != 'core'
                    and profile_id not in self._read_compose_profiles()):
                action.add_line(
                    f'Module "{profile_id}" is disabled (not in COMPOSE_PROFILES) — '
                    f'image updated but NOT started, to avoid an orphaned container. '
                    f'Enable the module to run the new version.'
                )
                # Best-effort: stop the containers in case one is already
                # running from before this fix (or any other out-of-profile
                # start) — never fails the update either way.
                stop = subprocess.Popen(
                    ['docker', 'compose', 'stop'] + self._to_services(containers),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, cwd=self.stack_root,
                )
                for line in iter(stop.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        action.add_line(f'  {line}')
                stop.wait()

                action.add_line('Done.')
                action.finish(success=True)
                self._auto_checksum(description)
                self.audit_logger.log(
                    'config.apply', user=user, source_ip=source_ip,
                    category='update', action='image_update',
                    detail=f'{description} (module disabled — recreate skipped, #176)',
                    containers_affected=containers,
                    risk='caution', outcome='success',
                    duration_ms=action.duration_ms,
                )
                return

            # Step 3: Recreate containers
            action.add_line(f'Recreating: {", ".join(containers)}...')
            up = subprocess.Popen(
                ['docker', 'compose', 'up', '-d', '--no-deps', '--force-recreate'] + self._to_services(containers),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=self.stack_root,
            )
            for line in iter(up.stdout.readline, ''):
                line = line.rstrip()
                if line:
                    action.add_line(line)
            up_code = up.wait()

            if up_code == 0:
                # M018 Phase 6 / S06.4: post-image-update hook for gpustack —
                # the upstream v2.x image ships no runner for AMD Strix Halo
                # (gfx1151) or CPU, so we register custom backends after every
                # recreate. Idempotent: reports "unchanged" when nothing drifted.
                if env_var == 'GPUSTACK_VERSION':
                    self._post_gpustack_recreate(action)
                action.add_line('Done.')
                action.finish(success=True)
                self._auto_checksum(description)
                self.audit_logger.log(
                    'config.apply', user=user, source_ip=source_ip,
                    category='update', action='image_update',
                    detail=description,
                    containers_affected=containers,
                    risk='caution', outcome='success',
                    duration_ms=action.duration_ms,
                )
            else:
                action.add_line(f'Error: exit code {up_code}')
                if old_version is not None:
                    self._update_env_var(env_var, old_version)
                    action.add_line(f'Reverted {env_var} to {old_version} (recreate failed).')
                action.finish(success=False, error=f'exit code {up_code}')
        except Exception as e:
            action.add_line(f'Error: {e}')
            action.finish(success=False, error=str(e))

    def _post_gpustack_recreate(self, action):
        """Register GPUStack custom backends after a gpustack version bump.

        M018 Phase 6 / S06.4. Invokes modules/llm/gpustack/init-backends.py
        against the freshly-recreated gpustack container. Idempotent — safe
        even when the script is re-run after a no-op image refresh.
        """
        script = os.path.join(self.stack_root, 'modules', 'llm', 'gpustack', 'init-backends.py')
        if not os.path.isfile(script):
            action.add_line('init-backends.py not found — skipping backend registration')
            return
        env_path = os.path.join(self.stack_root, '.env')
        # rc6.7: shared env_utils (strips inline ` # comment`).
        from .env_utils import parse_env_file
        env_vars = parse_env_file(env_path)
        api_key = env_vars.get('GPUSTACK_API_KEY', '')
        if not api_key:
            action.add_line('GPUSTACK_API_KEY missing — skipping backend registration')
            return
        port = env_vars.get('GPUSTACK_PORT', '9090')
        action.add_line('Registering GPUStack custom backends...')
        proc = subprocess.Popen(
            ['python3', script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=self.stack_root,
            env={
                **os.environ,
                'GPUSTACK_API': f'http://localhost:{port}',
                'GPUSTACK_API_KEY': api_key,
            },
        )
        for line in iter(proc.stdout.readline, ''):
            line = line.rstrip()
            if line:
                action.add_line(line)
        rc = proc.wait()
        if rc != 0:
            action.add_line(f'Backend registration exited {rc} (non-fatal)')

    def _update_env_var(self, key, value):
        """Update a single variable in .env."""
        lines = []
        if os.path.exists(self.env_path):
            with open(self.env_path) as f:
                lines = f.readlines()
        updated = False
        new_lines = []
        for line in lines:
            if line.strip().startswith('#') or '=' not in line:
                new_lines.append(line)
                continue
            k = line.split('=')[0].strip()
            if k == key:
                new_lines.append(f'{key}={value}\n')
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f'{key}={value}\n')
        with open(self.env_path, 'w') as f:
            f.writelines(new_lines)

    def _read_env_var(self, key):
        """Read a single var's current value from .env (or None)."""
        try:
            with open(self.env_path) as f:
                for line in f:
                    s = line.strip()
                    if s.startswith('#') or '=' not in s:
                        continue
                    k, _, v = s.partition('=')
                    if k.strip() == key:
                        return v.strip()
        except OSError:
            pass
        return None

    # Compose SERVICE names differ from CONTAINER names for a handful of
    # services: profiles.yaml + the docker-client status checks use container
    # names, but the `docker compose` CLI takes SERVICE names — passing a
    # container name yields "no such service" and aborts the pull/recreate
    # (operator hit this on a core update: `backup-service` is the container,
    # `backup` is the service). Map the known container_name overrides back to
    # their service key. Keep in sync with `container_name:` lines in the
    # compose files that differ from the service key. NB: gpustack*/model-sync*
    # are intentionally absent — multiple services share one container_name
    # across HARDWARE variants (ambiguous); those go through the LLM-runtime
    # toggle, not the generic image-update list.
    _CONTAINER_TO_SERVICE = {
        'backup-service': 'backup',
        'coding-tools-image-builder': 'coding-tools-image',
        'hermes-agent-image-builder': 'hermes-agent-image',
        'hermes-workspace-image-builder': 'hermes-workspace-image',
        'moltis-image-builder': 'moltis-image',
    }

    def _to_services(self, names):
        """Translate container names to compose service names for `docker compose`."""
        return [self._CONTAINER_TO_SERVICE.get(n, n) for n in names]

    def _compose_profile_services(self, profile_id):
        """The service names compose itself assigns to `profile_id`.

        #538: the profile toggle used to start the hand-maintained `containers:`
        list from profiles.yaml, translated through `_CONTAINER_TO_SERVICE`. Both
        lists drifted from compose — 6 profiles under-declared 15 containers — so
        enabling a module started only part of it and still reported success. On a
        customer box that left cognee with a healthy API but no UI and no MCP
        endpoint, and their unreferenced images were then eaten by the nightly
        `docker image prune -a` (#539).

        Compose is the only authority on what a profile contains, so ask it.
        `--profile` is passed explicitly so the answer does not depend on
        COMPOSE_PROFILES having been written first.

        Returns None if the compose model cannot be read, so the caller can fall
        back to the declared list rather than starting nothing at all.
        """
        try:
            result = subprocess.run(
                ['docker', 'compose', '--profile', profile_id,
                 'config', '--format', 'json'],
                capture_output=True, text=True, cwd=self.stack_root, timeout=120,
            )
            if result.returncode != 0:
                return None
            model = json.loads(result.stdout)
        except Exception:
            return None

        services = [
            name for name, svc in (model.get('services') or {}).items()
            if isinstance(svc, dict) and profile_id in (svc.get('profiles') or [])
        ]
        return sorted(services) or None

    def preview_profile_toggle(self, profile_id, enable):
        """Generate impact preview for enabling/disabling a profile."""
        profile = self.profile_manager.get_profile(profile_id)
        if not profile:
            return {'error': f'Unknown profile: {profile_id}'}

        if profile_id == 'core':
            return {'error': 'Core infrastructure cannot be toggled'}

        enabled = self.profile_manager.get_enabled_profiles()
        currently_enabled = profile_id in enabled

        if enable and currently_enabled:
            return {'error': f'{profile.get("name", profile_id)} is already enabled'}
        if not enable and not currently_enabled:
            return {'error': f'{profile.get("name", profile_id)} is already disabled'}

        containers = [c['name'] for c in profile.get('containers', [])]
        ram_mb = profile.get('ram_estimate_mb', 0)
        risk = profile.get('risk_on_toggle', 'safe')

        # Memory advisory
        system = self.resource_monitor.get_system_resources()
        total_mb = system.get('total_mb', 0)
        used_mb = system.get('used_mb', 0)

        if enable:
            projected_mb = used_mb + ram_mb
        else:
            projected_mb = max(0, used_mb - ram_mb)

        projected_pct = (projected_mb / total_mb * 100) if total_mb else 0

        if projected_pct > 95:
            memory_level = 'blocked'
        elif projected_pct > 85:
            memory_level = 'critical'
        elif projected_pct > 70:
            memory_level = 'warning'
        else:
            memory_level = 'ok'

        # Check mutual exclusions
        exclusion_conflict = None
        for excl in profile.get('mutual_exclusion', []):
            if excl in enabled:
                excl_profile = self.profile_manager.get_profile(excl)
                excl_name = excl_profile.get('name', excl) if excl_profile else excl
                exclusion_conflict = f'Cannot enable: conflicts with {excl_name} (mutually exclusive)'

        return {
            'action': 'profile_toggle',
            'profile_id': profile_id,
            'profile_name': profile.get('name', profile_id),
            'enable': enable,
            'containers': containers,
            'container_count': len(containers),
            'ram_estimate_mb': ram_mb,
            'risk': risk,
            'enable_impact': profile.get('enable_impact', ''),
            'disable_impact': profile.get('disable_impact', ''),
            'impact_text': profile.get('enable_impact', '') if enable else profile.get('disable_impact', ''),
            'stack_impact': profile.get('stack_impact', 'none'),
            'memory': {
                'total_mb': total_mb,
                'current_used_mb': used_mb,
                'projected_used_mb': projected_mb,
                'delta_mb': ram_mb if enable else -ram_mb,
                'projected_pct': round(projected_pct, 1),
                'level': memory_level,
            },
            'requires_password': risk in ('caution', 'danger'),
            'exclusion_conflict': exclusion_conflict,
            'dependencies': profile.get('dependencies', {}),
        }

    def apply_profile_toggle(self, profile_id, enable, user, source_ip):
        """Execute a profile toggle. Returns action_id for SSE streaming."""
        with self._lock:
            if self._current_action and not self._current_action.done:
                return None, 'Another apply action is already running'

        profile = self.profile_manager.get_profile(profile_id)
        if not profile:
            return None, f'Unknown profile: {profile_id}'

        action_word = 'Enable' if enable else 'Disable'
        action_id = str(uuid.uuid4())[:8]
        action = ApplyAction(action_id, f'{action_word} {profile.get("name", profile_id)}')

        with self._lock:
            self._current_action = action
            self._actions[action_id] = action

        # Run in background thread
        thread = threading.Thread(
            target=self._execute_toggle,
            args=(action, profile_id, profile, enable, user, source_ip),
            daemon=True
        )
        thread.start()

        return action_id, None

    def _execute_toggle(self, action, profile_id, profile, enable, user, source_ip):
        """Background execution of profile toggle."""
        try:
            # #174: the Config Portal drives docker via the docker-socket-proxy,
            # which DENIES the image /build endpoint (BUILD: 0, by design). A
            # module whose custom image was never pre-built (cognee, dify-web,
            # mcp-manager, paperclip, agent-manager, …) would make the
            # `docker compose up` below try to build it → a raw, opaque
            # `error from daemon: 403 forbidden`. Detect that up-front and
            # abort with an actionable message BEFORE mutating .env, so the
            # profile is never left half-enabled. Fail-open when we can't
            # enumerate (no docker / no compose) — mirrors
            # razzfazz-post-install.sh's prebuild_all_custom_images.
            if enable:
                try:
                    from app.services import build_preflight
                    missing = build_preflight.missing_build_images(
                        self.stack_root, profile_id)
                except Exception:
                    missing = []
                if missing:
                    from app.services import build_preflight
                    msg = build_preflight.format_missing_error(profile_id, missing)
                    action.add_line(f'Error: {msg}')
                    action.finish(success=False, error=msg)
                    self.audit_logger.log(
                        'profile.enable',
                        user=user, source_ip=source_ip,
                        category='profile', action='enable', target=profile_id,
                        detail=f'Enable of {profile.get("name", profile_id)} '
                               f'blocked — custom image(s) not built '
                               f'({", ".join(s for s, _ in missing)})',
                        outcome='failure', error=msg,
                        duration_ms=action.duration_ms,
                    )
                    return

            # (#191) matrix first-run render gate — same fail-clear philosophy
            # as the build pre-flight above. The synapse container mounts
            # ./synapse/homeserver.yaml.rendered (SYNAPSE_CONFIG_PATH); that
            # file is rendered from the homeserver.yaml template by
            # razzfazz-init.sh Step 5b (envsubst) at INSTALL time. A
            # Config-Portal toggle-enable never runs Step 5b, and this container
            # mounts the repo READ-ONLY (only .env is rw — see core/compose.yml),
            # so provision_profile CANNOT render it here. Without the rendered
            # file synapse mounts a non-existent config path and crash-loops.
            # Detect that up-front and fail clear BEFORE mutating .env, so
            # matrix is never left half-enabled with a broken homeserver. A box
            # where matrix WAS rendered once (then disabled) keeps the file and
            # re-enables cleanly — this only blocks a box that never rendered it.
            if enable and profile_id == 'matrix':
                rendered = os.path.join(
                    self.stack_root, 'modules', 'apps', 'matrix', 'synapse',
                    'homeserver.yaml.rendered')
                if not os.path.exists(rendered):
                    msg = (
                        "Cannot enable 'matrix' from the Configuration Portal: "
                        "Synapse's first-run config "
                        "(modules/apps/matrix/synapse/homeserver.yaml.rendered) "
                        "has not been rendered on this box. That render is an "
                        "install-time step (razzfazz-init.sh Step 5b) and the "
                        "Portal mounts the repo read-only, so it cannot render "
                        "it here — synapse would crash-loop on a missing config "
                        "path. Select matrix at install (rzfz init renders it "
                        "when matrix is in the chosen profiles), or render it on "
                        "the host, then re-enable. (#191)"
                    )
                    action.add_line(f'Error: {msg}')
                    action.finish(success=False, error=msg)
                    self.audit_logger.log(
                        'profile.enable',
                        user=user, source_ip=source_ip,
                        category='profile', action='enable', target=profile_id,
                        detail='Enable of Matrix blocked — Synapse config not '
                               'rendered (install-time step; Portal repo mount '
                               'is read-only)',
                        outcome='failure', error=msg,
                        duration_ms=action.duration_ms,
                    )
                    return

            # (#206) mac-llm — seed the gateway config.yaml BEFORE the up. The gateway
            # compose mounts ./config.yaml:/app/config.yaml, so with no pre-existing
            # file `docker compose up llm-mac-gateway` makes config.yaml a DIRECTORY,
            # and the mac_backends panel's atomic write then 500s (IsADirectoryError).
            # Unlike the matrix render gate above (which the Portal can't do), the
            # Portal CAN write this — core/compose.yml gives the config container a :rw
            # mount for modules/llm/mac-gateway (#206) — so seed from the template
            # instead of failing. Idempotent; harmless no-op if config.yaml exists.
            if enable and profile_id == 'mac-llm':
                _mg = os.path.join(self.stack_root, 'modules', 'llm', 'mac-gateway')
                _cfg = os.path.join(_mg, 'config.yaml')
                _tmpl = os.path.join(_mg, 'config.example.yaml')
                if not os.path.exists(_cfg) and os.path.exists(_tmpl):
                    try:
                        import shutil
                        shutil.copyfile(_tmpl, _cfg)
                        action.add_line('Seeded modules/llm/mac-gateway/config.yaml from template (#206).')
                    except Exception as _e:
                        action.add_line(f'Warning: could not seed mac-gateway config.yaml: {_e} (non-fatal)')

            # Step 1: Update .env
            action.add_line(f'Updating COMPOSE_PROFILES in .env...')
            enabled = self._read_compose_profiles()

            if enable:
                enabled.add(profile_id)
                # Check mutual exclusions and remove conflicting profiles
                for excl in profile.get('mutual_exclusion', []):
                    if excl in enabled:
                        enabled.discard(excl)
                        action.add_line(f'  Removing conflicting profile: {excl}')
            else:
                enabled.discard(profile_id)

            self._write_compose_profiles(enabled)
            action.add_line(f'  COMPOSE_PROFILES={",".join(sorted(enabled))}')

            # Step 1b: When enabling, mirror razzfazz-init.sh's per-profile
            # secret generation so the new profile's containers don't crash
            # on empty SSO secrets, etc. Idempotent — re-runs are no-ops.
            # (Public hostnames internal consumers need to resolve are
            # served via Caddy network aliases; no extra_hosts/CADDY_IP
            # plumbing required — see core/compose.yml.)
            if enable and self.config_manager is not None:
                try:
                    from app.services.profile_provisioner import provision_profile
                    provision_profile(profile_id, self.config_manager, action)
                except Exception as e:
                    action.add_line(f'Provisioning warning: {e} (non-fatal)')

            # Step 2: Run docker compose
            containers = [c['name'] for c in profile.get('containers', [])]
            # #538: ask compose what is in this profile. The declared list is the
            # fallback only, and the fallback is announced — a partial start that
            # reports success is the whole defect.
            services = self._compose_profile_services(profile_id)
            if services is None:
                services = self._to_services(containers)
                action.add_line(
                    'Note: could not read the compose model — falling back to the '
                    'container list declared in profiles.yaml.'
                )
            if enable:
                action.add_line(f'Starting containers: {", ".join(containers)}...')
                # Use 'up -d' with specific service names to avoid recreating the entire stack.
                # Fix #8: --force-recreate guards against stale NetworkID references on
                # containers in `Created` state from a previous `docker compose down -v`
                # session. Without it, `up -d` fails with "network <id> not found" because
                # compose tries to attach the existing container to a network that no
                # longer exists. Recreate makes compose drop the stale container and
                # build a fresh one wired to the current network.
                # --remove-orphans (#184 WS4): silence the "Found orphan containers"
                # warning compose emits when a prior profile rename left containers
                # compose no longer maps; it removes only containers not in the
                # CURRENT (post-write) compose model, so an enabled profile's
                # containers are never touched. NO --build here: the runtime never
                # builds (WS2a); a missing custom image is caught by the
                # build_preflight gate above (fails clear), never silently built.
                cmd = ['docker', 'compose', 'up', '-d', '--no-deps', '--force-recreate', '--remove-orphans'] + services
            else:
                action.add_line(f'Stopping containers: {", ".join(containers)}...')
                cmd = ['docker', 'compose', 'stop'] + services

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=self.stack_root
            )

            for line in iter(process.stdout.readline, ''):
                line = line.rstrip()
                if line:
                    action.add_line(line)

            exit_code = process.wait()

            if exit_code == 0:
                action.add_line(f'Done. Profile {"enabled" if enable else "disabled"} successfully.')

                # ga.6: with --no-deps above (so toggling a module no longer
                # cascade-recreates postgres/valkey/caddy — see the up cmd), the
                # postgres-db-reconcile one-shot that USED to ride in as a
                # depends_on of the module no longer runs automatically. A newly
                # enabled module that owns a postgres DB (gitea_db, dify_db, …)
                # needs that DB created, so run the reconcile explicitly here —
                # itself with --no-deps so it connects to the RUNNING postgres
                # instead of recreating it. Idempotent: only creates missing DBs.
                if enable:
                    action.add_line('Reconciling per-service databases (postgres-db-reconcile)...')
                    db_proc = subprocess.Popen(
                        ['docker', 'compose', 'up', '-d', '--no-deps',
                         '--force-recreate', 'postgres-db-reconcile'],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, cwd=self.stack_root,
                    )
                    for line in iter(db_proc.stdout.readline, ''):
                        line = line.rstrip()
                        if line:
                            action.add_line(line)
                    db_code = db_proc.wait()
                    if db_code != 0:
                        action.add_line(f'postgres-db-reconcile exited {db_code} (non-fatal)')

                # Fix #36: re-run Authentik init so launch URLs get re-rendered and any
                # new providers attach to the embedded outpost (and conversely so any
                # disabled-profile providers get their attachments cleaned up). We
                # invoke the `authentik-init` compose service via `up -d
                # --force-recreate` — it's a one-shot init container that exits when
                # the script finishes, so this is bounded by the script's own runtime.
                action.add_line('Re-running Authentik init (launch URLs + outpost attachments)...')
                ai_proc = subprocess.Popen(
                    ['docker', 'compose', 'up', '-d', '--no-deps', '--force-recreate', 'authentik-init'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=self.stack_root,
                )
                for line in iter(ai_proc.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        action.add_line(line)
                ai_code = ai_proc.wait()
                if ai_code != 0:
                    action.add_line(f'authentik-init recreate exited {ai_code} (non-fatal)')

                # Restart authentik-worker so it picks up the refreshed configuration.
                action.add_line('Restarting authentik-worker...')
                aw_proc = subprocess.Popen(
                    ['docker', 'compose', 'restart', 'authentik-worker'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=self.stack_root,
                )
                for line in iter(aw_proc.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        action.add_line(line)
                aw_code = aw_proc.wait()
                if aw_code != 0:
                    action.add_line(f'authentik-worker restart exited {aw_code} (non-fatal)')

                # razzfazz-start-portal reads COMPOSE_PROFILES from its own
                # container env (set at container create-time from .env via
                # `env_file: ../.env`). Its in-process fallback to /stack/.env
                # is blocked because the .env file is mode 0600 on hardened
                # installs and the portal runs as appuser. So toggling a
                # profile here without recreating the portal leaves stale
                # state — tiles for the just-enabled profile stay hidden,
                # or tiles for the just-disabled profile stay visible.
                # Recreate to re-evaluate env_file. Cheap: ~3s, no data loss
                # (per-user prefs live in postgres, not the container fs).
                action.add_line('Recreating razzfazz-start-portal so COMPOSE_PROFILES propagates...')
                sp_proc = subprocess.Popen(
                    ['docker', 'compose', 'up', '-d', '--no-deps', '--force-recreate',
                     'razzfazz-start-portal'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=self.stack_root,
                )
                for line in iter(sp_proc.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        action.add_line(line)
                sp_code = sp_proc.wait()
                if sp_code != 0:
                    action.add_line(f'razzfazz-start-portal recreate exited {sp_code} (non-fatal)')

                # rc6.7 #46 v3: when openhands is being enabled, attach it to
                # the docker default `bridge` network (where its spawned sandbox
                # runtimes live) and re-run the URL monkey-patch. Compose can't
                # manage the default bridge attachment (aliases unsupported).
                # See docs/upstream-monkey-patches.md.
                if enable and profile_id == 'openhands':
                    action.add_line('Attaching openhands to docker default bridge + applying URL monkey-patch...')
                    try:
                        subprocess.run(
                            ['docker', 'network', 'connect', 'bridge', 'openhands'],
                            check=False, capture_output=True, text=True,
                        )
                        subprocess.run(
                            ['docker', 'exec', 'openhands', 'sh', '/opt/openhands-monkeypatch.sh'],
                            check=False, capture_output=True, text=True,
                        )
                        subprocess.run(
                            ['docker', 'restart', 'openhands'],
                            check=False, capture_output=True, text=True,
                        )
                        action.add_line('  openhands re-attached + patched + restarted')
                    except Exception as e:
                        action.add_line(f'  openhands bridge attach failed: {e} (non-fatal)')

                # BSB-16: post-toggle Tier-D acceptance smoke. ENABLE only —
                # nothing to verify on disable. Probe failures are surfaced
                # as warning lines in the SSE stream + reflected in the audit
                # entry; they do NOT roll back the toggle (the operator may
                # want to keep the module enabled while debugging the probe
                # failure). See decisions log BSB-16-DEC-02.
                probe_summary = None
                if enable:
                    try:
                        from app.services import post_toggle_probe as _ptp
                        from pathlib import Path as _Path
                        targets = _ptp.resolve_probe_targets(
                            profile, stack_root=_Path(self.stack_root),
                        )
                        probe_summary = _ptp.run_probes(
                            targets,
                            stack_root=_Path(self.stack_root),
                            action=action,
                        )
                        if probe_summary.get('overall_pass') is False:
                            action.add_line(
                                f'  Note: {profile_id} is enabled and its '
                                f'containers are up. The post-enable acceptance '
                                f'smoke did not pass on this run — the module may '
                                f'still be warming up (this does NOT affect the '
                                f'enable). If it keeps misbehaving after a minute, '
                                f'see tests/results/ for details.'
                            )
                        elif probe_summary.get('overall_pass') is True:
                            action.add_line(
                                f'  post-toggle probe: all targets PASSED'
                            )
                        else:
                            # overall_pass is None == skipped. Surface the
                            # reason explicitly so the operator/UI doesn't
                            # think the probe ran when it didn't.
                            reason = probe_summary.get('skipped_reason') or 'unknown'
                            action.add_line(
                                f'  post-toggle probe: SKIPPED ({reason})'
                            )
                    except Exception as _probe_exc:
                        # Probe runner crashes must NEVER fail the toggle —
                        # the toggle itself succeeded. Record the crash in
                        # the action stream and move on.
                        action.add_line(
                            f'  [post-toggle probe] runner crashed '
                            f'(non-fatal): {_probe_exc!r}'
                        )
                        probe_summary = {
                            'overall_pass': None,
                            'targets': [],
                            'skipped_reason': f'runner exception: {_probe_exc!r}',
                        }

                action.finish(success=True)
                self._auto_checksum(f'{"Enabled" if enable else "Disabled"} {profile.get("name", profile_id)}')

                # Build audit kwargs incrementally so the BSB-16 probe
                # summary only appears on enable+probe-ran paths.
                audit_kwargs = dict(
                    user=user,
                    source_ip=source_ip,
                    auth_method='authentik+password',
                    category='profile',
                    action='enable' if enable else 'disable',
                    target=profile_id,
                    detail=f'{"Enabled" if enable else "Disabled"} {profile.get("name", profile_id)}',
                    risk=profile.get('risk_on_toggle', 'safe'),
                    changes=[{
                        'var': 'COMPOSE_PROFILES',
                        'new': ','.join(sorted(enabled)),
                    }],
                    containers_affected=[c['name'] for c in profile.get('containers', [])],
                    outcome='success',
                    duration_ms=action.duration_ms,
                )
                if probe_summary is not None:
                    from app.services.post_toggle_probe import summarize_for_audit
                    audit_kwargs['post_toggle_probe'] = summarize_for_audit(probe_summary)

                self.audit_logger.log(
                    f'profile.{"enable" if enable else "disable"}',
                    **audit_kwargs,
                )
            else:
                error_msg = f'docker compose exited with code {exit_code}'
                action.add_line(f'Error: {error_msg}')
                action.finish(success=False, error=error_msg)

                self.audit_logger.log(
                    f'profile.{"enable" if enable else "disable"}',
                    user=user,
                    source_ip=source_ip,
                    category='profile',
                    action='enable' if enable else 'disable',
                    target=profile_id,
                    detail=f'Failed to {"enable" if enable else "disable"} {profile.get("name", profile_id)}',
                    outcome='failure',
                    error=error_msg,
                    duration_ms=action.duration_ms,
                )

        except Exception as e:
            action.add_line(f'Error: {str(e)}')
            action.finish(success=False, error=str(e))

    def get_action(self, action_id):
        return self._actions.get(action_id)

    # ── M029-S04: LLM Runtime stable/experimental toggle ────────────────

    LLM_PROFILE_TOKENS = ('llm', 'llm-legacy', 'llm-cpu')

    def get_llm_runtime_state(self):
        """Inspect COMPOSE_PROFILES + HARDWARE to report current LLM runtime.

        Returns: {
            current_profile: 'llm-legacy'|'llm-cpu'|'llm'|None,
            current_runtime: 'stable'|'experimental'|None,
            hardware: 'amd'|'cpu'|'nvidia'|None,
            stable_profile: 'llm-legacy'|'llm-cpu'|None,  # what stable would be for this hardware
            can_toggle: bool,                              # NVIDIA stays on `llm` (vLLM upstream); no toggle
            toggle_blocked_reason: str|None,
        }
        """
        profiles = self._read_compose_profiles()
        hardware = self._read_env_value('HARDWARE') or 'amd'
        current_profile = next((p for p in self.LLM_PROFILE_TOKENS if p in profiles), None)
        if current_profile == 'llm':
            current_runtime = 'experimental'
        elif current_profile in ('llm-legacy', 'llm-cpu'):
            current_runtime = 'stable'
        else:
            current_runtime = None

        if hardware == 'nvidia':
            can_toggle = False
            blocked = ('NVIDIA only ships on the v2.x runtime (vLLM upstream); '
                       'there is no v0.7.1 NVIDIA path to fall back to.')
            stable_profile = 'llm'
        elif hardware == 'cpu':
            stable_profile = 'llm-cpu'
            can_toggle = current_profile is not None
            blocked = None if can_toggle else 'No LLM profile is currently active.'
        else:
            stable_profile = 'llm-legacy'
            can_toggle = current_profile is not None
            blocked = None if can_toggle else 'No LLM profile is currently active.'

        return {
            'current_profile': current_profile,
            'current_runtime': current_runtime,
            'hardware': hardware,
            'stable_profile': stable_profile,
            'experimental_profile': 'llm',
            'can_toggle': can_toggle,
            'toggle_blocked_reason': blocked,
        }

    def apply_llm_runtime_toggle(self, target_runtime, user, source_ip):
        """Flip the LLM runtime between stable (v0.7.1) and experimental (v2.1.x).

        target_runtime: 'stable' or 'experimental'.

        Mechanics:
          1. Determine current and target profile tokens (per hardware).
          2. Substitute in COMPOSE_PROFILES, write .env.
          3. `docker compose stop gpustack model-sync` (containers are
             container-named identically across profiles, so a stop+up
             cycle is the cleanest swap).
          4. `docker compose --profile <new> up -d gpustack-<variant>
             model-sync-<variant>` to bring up the chosen runtime.

        Returns: (action_id, error_msg)
        """
        if target_runtime not in ('stable', 'experimental'):
            return None, f'Invalid target_runtime: {target_runtime}'

        state = self.get_llm_runtime_state()
        if not state['can_toggle']:
            return None, state['toggle_blocked_reason'] or 'Cannot toggle LLM runtime'
        if state['current_runtime'] == target_runtime:
            return None, f'Already on {target_runtime} runtime'

        target_profile = (state['stable_profile'] if target_runtime == 'stable'
                          else state['experimental_profile'])
        old_profile = state['current_profile']

        with self._lock:
            if self._current_action and not self._current_action.done:
                return None, 'Another apply action is already running'

        action_id = str(uuid.uuid4())[:8]
        action = ApplyAction(
            action_id,
            f'LLM runtime: {state["current_runtime"]} → {target_runtime} '
            f'({old_profile} → {target_profile})',
        )
        with self._lock:
            self._current_action = action
            self._actions[action_id] = action

        thread = threading.Thread(
            target=self._execute_llm_runtime_toggle,
            args=(action, old_profile, target_profile, target_runtime, user, source_ip),
            daemon=True,
        )
        thread.start()
        return action_id, None

    def _execute_llm_runtime_toggle(self, action, old_profile, target_profile,
                                     target_runtime, user, source_ip):
        try:
            action.add_line(f'Updating COMPOSE_PROFILES: {old_profile} → {target_profile}...')
            enabled = self._read_compose_profiles()
            enabled.discard(old_profile)
            enabled.add(target_profile)
            self._write_compose_profiles(enabled)
            action.add_line(f'  COMPOSE_PROFILES={",".join(sorted(enabled))}')

            # Stop + remove existing gpustack + model-sync (container_names
            # are shared across profiles). Just stopping leaves a stale
            # container holding the /gpustack name slot, so the next
            # `docker compose up` aborts with `Conflict. The container
            # name "/gpustack" is already in use`. rc6.6 hit this exact
            # bug in migrate_llm_profiles; same fix here.
            action.add_line('Stopping + removing current LLM stack (gpustack + model-sync)...')
            for c in ('gpustack', 'model-sync'):
                p = subprocess.Popen(
                    ['docker', 'rm', '-f', c],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    cwd=self.stack_root,
                )
                for line in iter(p.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        action.add_line(f'  {line}')
                p.wait()

            # rc6.9: kill orphan v2.x runner pods. v2.x spawns inference
            # workers as separate containers via the docker socket
            # (gemma4-XXXXX-run-0, qwen3-coder-next-XXXXX-run-0, …) with
            # `restart: unless-stopped`, so they survive `docker compose
            # down` and keep their host port bindings (40001-40063).
            # When v0.7.1 starts up it tries to bind 40001 and fails:
            # "failed to bind host port 127.0.0.1:40001/tcp: address
            # already in use". Kill them before bringing up the new stack.
            if old_profile == 'llm':
                action.add_line('Removing orphan v2.x runner pods (release worker ports)...')
                p = subprocess.Popen(
                    ['docker', 'ps', '-a', '--format', '{{.Names}}',
                     '--filter', 'name=-run-'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                runners, _ = p.communicate()
                for runner in runners.split():
                    if not runner:
                        continue
                    rm = subprocess.Popen(
                        ['docker', 'rm', '-f', runner],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    )
                    out, _ = rm.communicate()
                    if out.strip():
                        action.add_line(f'  removed: {out.strip()}')

            # rc6.9: gpustack v0.7.1 and v2.x have incompatible alembic
            # schemas. v2.x writes revisions v0.7.1 doesn't know about,
            # so a v2.x → v0.7.1 flip aborts with: "Database migration
            # failed: Can't locate revision identified by '8ad0f94c92e8'".
            # The dialog already tells the operator that model state
            # doesn't auto-migrate; reflect that by resetting the DB
            # whenever the runtime version changes. Fresh DB lets each
            # gpustack version run its own migrations from scratch.
            if (old_profile == 'llm' and target_profile in ('llm-legacy', 'llm-cpu')) \
               or (old_profile in ('llm-legacy', 'llm-cpu') and target_profile == 'llm'):
                action.add_line('Resetting gpustack_db (incompatible alembic schema across runtime versions)...')
                pg_user = self._read_env_value('POSTGRES_USER') or 'docker'
                pg_pw = self._read_env_value('POSTGRES_PASSWORD')
                env = os.environ.copy()
                if pg_pw:
                    env['PGPASSWORD'] = pg_pw
                for sql in ('DROP DATABASE IF EXISTS gpustack_db;',
                            f'CREATE DATABASE gpustack_db OWNER {pg_user};'):
                    p = subprocess.Popen(
                        ['docker', 'exec', '-e', f'PGPASSWORD={pg_pw}',
                         'postgres', 'psql', '-U', pg_user, '-d', 'postgres', '-c', sql],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    )
                    out, _ = p.communicate()
                    if out.strip():
                        action.add_line(f'  {out.strip()}')

            # Bring up ONLY the target profile's LLM services. We can't
            # use `docker compose up --force-recreate` against the whole
            # project (or even the whole profile) because that recreates
            # docker-socket-proxy too — and razzfazz-config talks to
            # docker through it via DOCKER_HOST=tcp://docker-socket-proxy.
            # When socket-proxy goes down mid-flight the next compose op
            # fails with "Cannot connect to the Docker daemon" and the
            # toggle aborts.
            #
            # Service-name mapping by profile (mutually exclusive trio
            # sharing the gpustack / model-sync container_name; ollama-
            # proxy is profile-shared and will be picked up implicitly
            # by deps):
            llm_services = {
                'llm':        ['gpustack',        'model-sync'],
                'llm-legacy': ['gpustack-legacy', 'model-sync-legacy'],
                'llm-cpu':    ['gpustack-cpu',    'model-sync-cpu'],
            }.get(target_profile, [])
            action.add_line(f'Bringing up {target_profile} stack ({", ".join(llm_services)})...')
            p = subprocess.Popen(
                ['docker', 'compose', '--profile', target_profile,
                 'up', '-d', *llm_services],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                cwd=self.stack_root,
            )
            for line in iter(p.stdout.readline, ''):
                line = line.rstrip()
                if line:
                    action.add_line(line)
            ec = p.wait()

            if ec == 0:
                # rc6.9: gpustack_db reset (above) also wiped the API key
                # row + the v2.x custom backend registrations. Run
                # razzfazz-post-install.sh --refresh to re-mint the API
                # key and re-register the custom backends. Idempotent —
                # skips if already current. --skip-dns because /etc/hosts
                # didn't change and the script tries to sudo for it.
                action.add_line('Re-bootstrapping API key + custom backends (post-install --refresh)...')
                refresh_script = os.path.join(self.stack_root, 'razzfazz-post-install.sh')
                if os.path.exists(refresh_script):
                    p = subprocess.Popen(
                        [refresh_script, '--refresh', '--skip-dns'],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        cwd=self.stack_root,
                    )
                    for line in iter(p.stdout.readline, ''):
                        line = line.rstrip()
                        if line:
                            action.add_line(f'  {line}')
                    p.wait()  # don't fail the toggle if refresh has issues
                action.add_line(
                    f'Done. LLM runtime is now {target_runtime} ({target_profile}).'
                )
                self.audit_logger.log(
                    'llm_runtime.toggle',
                    user=user, source_ip=source_ip,
                    category='llm_runtime',
                    action='toggle',
                    detail=f'{old_profile} → {target_profile} ({target_runtime})',
                )
                action.finish(success=True)
            else:
                action.finish(
                    success=False,
                    error=f'docker compose up exited {ec}; check logs',
                )
        except Exception as e:
            action.finish(success=False, error=str(e))

    def _read_env_value(self, key):
        """Read a single .env value; returns empty string if missing.

        Used by get_llm_runtime_state() for the HARDWARE lookup. Doesn't
        source the file (operator-edited .env carries inline comments —
        see memory feedback_dotenv_no_source.md).
        """
        if not os.path.exists(self.env_path):
            return ''
        with open(self.env_path) as f:
            for line in f:
                line = line.rstrip('\n')
                if line.startswith(f'{key}='):
                    from .env_utils import parse_env_value
                    return parse_env_value(line.split('=', 1)[1])
        return ''

    def _read_compose_profiles(self):
        """Read COMPOSE_PROFILES from .env."""
        profiles = set()
        if not os.path.exists(self.env_path):
            return profiles
        with open(self.env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('COMPOSE_PROFILES='):
                    # rc6.7: shared env_utils (strips inline ` # comment`).
                    from .env_utils import parse_env_value
                    value = parse_env_value(line.split('=', 1)[1])
                    profiles = set(p.strip() for p in value.split(',') if p.strip())
                    break
        return profiles

    def _write_compose_profiles(self, profiles):
        """Update COMPOSE_PROFILES in .env."""
        new_value = ','.join(sorted(profiles))
        lines = []
        found = False
        with open(self.env_path) as f:
            for line in f:
                if line.strip().startswith('COMPOSE_PROFILES='):
                    lines.append(f'COMPOSE_PROFILES={new_value}\n')
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f'COMPOSE_PROFILES={new_value}\n')
        with open(self.env_path, 'w') as f:
            f.writelines(lines)
