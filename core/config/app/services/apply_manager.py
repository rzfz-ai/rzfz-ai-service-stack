# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Apply manager — handles config changes, impact preview, and docker compose execution."""

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from .compose_allowlist import (  # noqa: F401 (re-export for callers/tests)
    ComposeFileRejected, StackHostPathMissing, assert_compose_file_allowed, compose_argv,
    compose_env, stack_host_path_problem,
)
from .env_mount import env_write_guard, explain_stale, host_command, inspect_env_mounts
from .stateful_pins import pin_sync_decision

logger = logging.getLogger(__name__)

# #2681: what an enable's post-start hook and binding check did belongs in the
# container log too (`docker logs razzfazz-config`). Nothing configures
# logging in this process, so the root logger drops INFO; this one logger
# writes its own lines to stderr.
enable_logger = logging.getLogger('razzfazz.config.enable')
if not enable_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(asctime)s [enable] %(message)s'))
    enable_logger.addHandler(_h)
    enable_logger.setLevel(logging.INFO)
    enable_logger.propagate = False


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
        self.store = None          # #2794: ApplyStateStore, set by the manager
        self._last_save = 0.0
        # #2993: the job status a page shows while the action runs.
        self.step = ''
        self.step_index = 0
        self.step_total = 0
        self.step_started = self.start_time
        self.waiting_for = []      # containers the current step waits for
        self.cancellable = False
        self.cancel_note = ''      # what "Cancel" does at this step, in the UI's words
        self.cancel_requested = False
        self.cancelled = False

    def add_line(self, text):
        with self._lock:
            self.lines.append(text)
        # Persist at most twice a second; finish() always persists.
        if self.store is not None and time.time() - self._last_save >= 0.5:
            self._last_save = time.time()
            self.store.save(self)

    def set_step(self, name, index, total, *, cancellable=False, note=''):
        """#2993 — the step the action is in, and what a cancel does there."""
        with self._lock:
            self.step, self.step_index, self.step_total = name, index, total
            self.step_started = time.time()
            self.waiting_for = []
            self.cancellable, self.cancel_note = bool(cancellable), note
        self.add_line(f'[{index}/{total}] {name}')

    def set_waiting(self, names):
        with self._lock:
            self.waiting_for = sorted(names)
        if self.store is not None:
            self.store.save(self)

    def request_cancel(self):
        """(accepted, message). Accepted only at a step that defines a cancel;
        the running thread acts on it at its next check (#2993)."""
        with self._lock:
            if self.done:
                return False, 'The action has already finished.'
            if self.cancel_requested:
                return True, 'Cancel already requested.'
            if not self.cancellable:
                return False, ('This step cannot be cancelled. ' + self.cancel_note).strip()
            self.cancel_requested = True
            note = self.cancel_note
        self.add_line('Cancel requested by the user.')
        return True, note

    def state(self):
        if self.done:
            if self.cancelled:
                return 'cancelled'
            return 'succeeded' if self.success else 'failed'
        return 'cancelling' if self.cancel_requested else 'running'

    def status(self):
        """The job status /api/apply/<id> answers with (#2993)."""
        now = time.time()
        with self._lock:
            return {
                'state': self.state(),
                'step': self.step, 'step_index': self.step_index, 'step_total': self.step_total,
                'step_elapsed_s': int(now - self.step_started) if not self.done else 0,
                'elapsed_s': int(now - self.start_time) if not self.done else int(self.duration_ms / 1000),
                'waiting_for': list(self.waiting_for),
                'cancellable': bool(self.cancellable and not self.done and not self.cancel_requested),
                'cancel_note': self.cancel_note if not self.done else '',
            }

    def finish(self, success, error=None, cancelled=False):
        with self._lock:
            self.done = True
            self.success = success
            self.error = error
            self.cancelled = bool(cancelled)
            self.waiting_for = []
            self.duration_ms = int((time.time() - self.start_time) * 1000)
        if self.store is not None:
            self.store.save(self)

    def get_lines_from(self, index):
        with self._lock:
            return list(self.lines[index:]), self.done, self.success, self.error, self.duration_ms


class ApplyBusy(str):
    """The refusal of a second apply while one runs (#2993).

    Still the error string every caller already handles (flash, jsonify, 409),
    but it names the running action and carries its id, so the API can hand
    the caller that action's progress instead of a bare "already running".
    """

    def __new__(cls, action):
        desc = (getattr(action, 'description', '') or '').strip()
        text = 'Another apply action is already running'
        obj = super().__new__(cls, f'{text}: {desc}' if desc else text)
        obj.action_id = getattr(action, 'action_id', None)
        obj.description = desc
        return obj


class EnvBindStale(str):
    """The refusal of a change because the Portal's `.env` bind is stale (#3058).

    Like `ApplyBusy`, still the error string every caller already handles, and
    it holds the host command. An API caller that can do better starts the
    repair (`PortalSelfHeal.begin`) instead of showing it."""


class _StoredActions(dict):
    """The in-process action registry; every action put in it is also persisted
    (#2794), so another worker or a restarted portal can answer for it."""

    def __init__(self, store):
        super().__init__()
        self.store = store

    def __setitem__(self, key, action):
        if self.store is not None:
            action.store = self.store
            self.store.save(action)
        super().__setitem__(key, action)


class ApplyManager:
    def __init__(self, stack_root, profile_manager, resource_monitor, audit_logger,
                 config_manager=None, state_dir=None):
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
        self.state_store = None
        self.state_dir = state_dir
        if state_dir:
            from .apply_state_store import ApplyStateStore
            self.state_store = ApplyStateStore(state_dir)
            self.state_store.cleanup()
        self._actions = _StoredActions(self.state_store)
        self._current_action = None
        self._lock = threading.Lock()

    def _compose(self, *args, env=None):
        """#1226 — THE place this process builds a `docker compose` argv.

        Validates the effective COMPOSE_FILE chain against the allow-list
        first: a state-changing verb on a chain that names a Portal-writable
        path raises `ComposeFileRejected` instead of handing the daemon a
        service definition an attacker could have written (the `:ro` root does
        not cover `.env` or `backups/`). Read-only verbs only log.
        """
        return compose_argv(self.stack_root, *args, env=env, logger=logger)

    def _clean_env(self):
        """The environment of EVERY docker / docker compose child (#2816).

        Never the Portal's inherited ``os.environ``: that carries the values
        the container was created with (``MAIN_DOMAIN`` ...), and a shell
        variable beats ``.env`` in compose interpolation, so a service
        recreated by an apply would get the OLD value of a key the apply just
        changed. See :func:`compose_allowlist.compose_env`."""
        return compose_env(self.stack_root)

    def _host_path_block_reason(self):
        """#2831 — precondition of EVERY apply: the Portal must know the stack
        directory's HOST path. Without it compose would run from the Portal's
        own in-container path and docker would create root-owned empty
        directories on the host for each bind mount. Checked before the first
        .env write or compose call, so a refused apply changes nothing.
        Returns the message, or None when the path is sound."""
        try:
            problem = stack_host_path_problem(self.stack_root)
        except Exception as exc:  # noqa: BLE001 - an unjudgeable path is not a sound one
            problem = f'STACK_HOST_PATH could not be checked ({exc!r})'
        if problem is None:
            return None
        return str(StackHostPathMissing(problem)).replace(
            'refusing to run docker compose: ', 'Apply refused: ', 1)

    def _compose_chain_block_reason(self):
        """#3412 — the COMPOSE_FILE chain, judged BEFORE anything is written.

        `_compose` refuses a state-changing run on a chain the #1226 allow-list
        rejects, but a toggle reaches its first compose run only after it has
        written COMPOSE_PROFILES: a disable then left .env saying "off" while
        the container ran on. The same check, done first, refuses while the box
        is still exactly as it was. Returns the message, or None when the chain
        is allowed."""
        try:
            assert_compose_file_allowed(self.stack_root, self._clean_env())
        except ComposeFileRejected as exc:
            return ('Nothing was changed. ' + str(exc).replace(
                'refusing to run docker compose: ', 'COMPOSE_FILE is not allowed: ', 1))
        except Exception as exc:  # noqa: BLE001 - an unjudgeable chain is not an allowed one
            return f'Nothing was changed. COMPOSE_FILE could not be checked ({exc!r}).'
        return None

    def _half_applied_note(self, profile_id, profile, enable, profiles_before):
        """#3412 — after a toggle failed: what state the box is in, and how to
        finish or undo the change. Measured from .env, not inferred from where
        the failure happened: a start that was rolled back has the module list
        as before and needs no note. None = .env is as before the toggle."""
        if profiles_before is None:
            return None   # failed before step 2: nothing was written
        try:
            now = self._read_compose_profiles()
        except Exception as exc:  # noqa: BLE001
            return (f'State: .env could not be read back ({exc!r}), so whether the module '
                    'list changed is unknown. Check COMPOSE_PROFILES in .env before retrying.')
        if (profile_id in now) == (profile_id in profiles_before):
            return None
        name = profile.get('name', profile_id)
        services = ' '.join(self._to_services(
            [c['name'] for c in profile.get('containers', [])])) or '<its services>'
        if enable:
            return (f'State: HALF-APPLIED. .env already has {name} switched ON, but the enable '
                    'did not finish, so its containers and the proxy and sign-in steps may not '
                    f'match. To finish: fix the error above, then disable and enable {name} '
                    f'again on the Modules page. To undo: disable {name} on the Modules page.')
        stop = host_command(f'docker compose --profile {profile_id} stop {services}')
        return (f'State: HALF-APPLIED. .env already has {name} switched OFF, but its '
                'containers may still be running, and the proxy and sign-in steps did not '
                f'run. To finish: fix the error above, then enable and disable {name} again '
                f'on the Modules page — or stop the containers on the host: `{stop}`. '
                f'To undo: enable {name} again on the Modules page.')

    def _env_bind_block_reason(self):
        """#3058 — checked before a change starts its background thread, where a
        stale bind could only end as a failed action. Refused here, nothing has
        been written and the caller can repair the bind and resume."""
        try:
            report = inspect_env_mounts(self.stack_root)
        except Exception:  # noqa: BLE001 - the write guard still catches a real failure
            return None
        if not report['stale']:
            return None
        stale = next(f for f in report['files'] if f['stale'])
        return EnvBindStale(explain_stale(stale['path'], stale['reason']))

    def _auto_checksum(self, comment):
        """Take an automatic governance checksum after a successful change."""
        if self.checksum_manager:
            try:
                self.checksum_manager.take_checksum(f'UI: {comment}', source='ui-auto')
            except Exception:
                pass

    def _audit_before_done(self, apply_action, event, **kwargs):
        """Write one audit entry BEFORE `action.finish()`, never raising.

        CFG-7/8/9 — the #743/#900 invariant, extracted so every executor can
        hold it, not just `_execute_toggle`:

          * `action.done` is observed by other threads (the SSE stream, the
            `/api/apply/<id>` poller) WITHOUT the audit writer's lock, so
            `done == True` must always imply the entry already exists on
            disk. Callers therefore invoke this *before* `finish()`.
          * The duration is computed inline rather than read from
            `action.duration_ms`, which `finish()` has not yet set.
          * An audit-write failure must never change the OUTCOME of the
            action: before CFG-7 a failing `audit_logger.log()` on the
            re-domain success path fell into the outer `except`, flipping a
            completed re-domain to `success=False` and writing a second,
            contradictory `outcome='failure'` entry. It is swallowed here and
            surfaced as an action-log line instead.
        """
        kwargs.setdefault(
            'duration_ms',
            int((time.time() - apply_action.start_time) * 1000))
        try:
            self.audit_logger.log(event, **kwargs)
        except Exception as audit_exc:
            apply_action.add_line(
                f'Warning: audit log write failed (non-fatal): {audit_exc!r}'
            )

    def apply_service_restart(self, containers, description, user, source_ip,
                              category='settings', risk='caution', followup=None):
        """Restart specific containers after a settings change. Returns action_id.

        `followup` (#2794): one-shot services recreated AFTER the restart
        succeeded, e.g. `authentik-init`, which delivers the SSO source
        blueprints a bare Authentik restart never does.
        """
        err = self._host_path_block_reason()
        if err:
            return None, err

        with self._lock:
            if self._current_action and not self._current_action.done:
                return None, ApplyBusy(self._current_action)

        action_id = str(uuid.uuid4())[:8]
        action = ApplyAction(action_id, description)

        with self._lock:
            self._current_action = action
            self._actions[action_id] = action

        thread = threading.Thread(
            target=self._execute_restart,
            args=(action, containers, description, user, source_ip, category, risk, followup),
            daemon=True,
        )
        thread.start()
        return action_id, None

    def _execute_restart(self, action, containers, description, user, source_ip, category, risk,
                         followup=None):
        try:
            action.add_line(f'Restarting: {", ".join(containers)}...')
            cmd = self._compose('up', '-d', '--no-deps', '--force-recreate') + self._to_services(containers)
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=self.stack_root, env=self._clean_env(),
            )
            for line in iter(process.stdout.readline, ''):
                line = line.rstrip()
                if line:
                    action.add_line(line)
            exit_code = process.wait()

            if exit_code == 0 and followup:
                # #2794: a one-shot (authentik-init) that must run once the
                # restarted services are up. `up -d` returns as soon as the
                # container is started, so this does not hold the apply lock
                # for the init's runtime; a failure is reported, not fatal.
                action.add_line(f'Running: {", ".join(followup)}...')
                fproc = subprocess.Popen(
                    self._compose('up', '-d', '--no-deps', '--force-recreate')
                    + self._to_services(followup),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, cwd=self.stack_root, env=self._clean_env(),
                )
                for line in iter(fproc.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        action.add_line(line)
                f_code = fproc.wait()
                if f_code != 0:
                    action.add_line(f'Warning: {", ".join(followup)} exited {f_code} (non-fatal)')
                    exit_code = f_code
                elif 'authentik-init' in followup:
                    self._verify_sso_applied(action)

            if exit_code == 0:
                action.add_line('Done.')
                # CFG-8: audit BEFORE finish() — see _audit_before_done.
                self._audit_before_done(
                    action, 'config.apply', user=user, source_ip=source_ip,
                    category=category, action='restart',
                    detail=description,
                    containers_affected=containers,
                    risk=risk, outcome='success',
                )
                action.finish(success=True)
                # CFG-30: the governance checksum is bookkeeping — take it
                # after finish() so the busy lock is not held for the walk.
                self._auto_checksum(description)
            else:
                # CFG-9: a failed settings-apply restart used to leave NO
                # audit trail at all, so the log showed only the applies that
                # worked.
                action.add_line(f'Error: exit code {exit_code}')
                self._audit_before_done(
                    action, 'config.apply', user=user, source_ip=source_ip,
                    category=category, action='restart',
                    detail=f'Failed: {description}',
                    containers_affected=containers,
                    risk=risk, outcome='failure',
                    error=f'exit code {exit_code}',
                )
                action.finish(success=False, error=f'exit code {exit_code}')
        except Exception as e:
            action.add_line(f'Error: {e}')
            self._audit_before_done(
                action, 'config.apply', user=user, source_ip=source_ip,
                category=category, action='restart',
                detail=f'Failed: {description}: {e}',
                containers_affected=containers,
                risk=risk, outcome='failure', error=str(e),
            )
            action.finish(success=False, error=str(e))

    def _verify_sso_applied(self, action):
        """#2794: after the SSO step, wait for the SSO blueprint
        instances and the sources (SSO_VERIFY_TIMEOUT, default 5 min); a refused source fails the apply with the
        reason instead of reporting "saved". Raises RuntimeError."""
        from . import sso_verify
        env = self.config_manager.read_env() if self.config_manager else {}
        action.add_line('Waiting for Authentik to apply the SSO sources...')
        reason = sso_verify.verify(sso_verify.expected_providers(env),
                                   timeout=sso_verify.verify_timeout(env),
                                   on_progress=action.add_line)
        if reason:
            raise RuntimeError(f'SSO sources not applied: {reason}')
        action.add_line('SSO sources applied.')

    # ga.15 re-domain: services that BAKE the domain (env / persisted config /
    # served host) and must be recreated so they follow a MAIN_DOMAIN change.
    # EXCLUDES: the LLM runtime (gpustack*/model-sync* — recreating them unloads
    # running models and they don't carry the domain), the shared datastores
    # (postgres/valkey), and the config UI ITSELF (we run inside it). Both
    # OpenWebUI service spellings are listed; only the running one is acted on.
    # #567: entries MUST be the exact compose SERVICE names (_running_services
    # returns those; the filter silently drops anything misspelled — that is
    # how the start portal kept its old domain on the first production
    # re-domain). Audited against the compose files 2026-08-23:
    # razzfazz-start-portal (core/compose.yml), element-web (matrix module);
    # the never-existing 'open-webui' spelling is gone.
    _DOMAIN_BAKING_SERVICES = [
        'caddy', 'authentik-server', 'authentik-worker',
        'openwebui', 'pipelines',
        # #2510: dify-worker-dataset carries the same CONSOLE_URL/API_URL/…
        # env block as dify-worker.
        'dify-api', 'dify-web', 'dify-worker', 'dify-worker-dataset', 'dify-plugin-daemon',
        'gitea', 'razzfazz-start-portal', 'openlit', 'synapse', 'element-web',
    ]

    #: Containers authentik-init copies scripts into and execs in.
    _AUTHENTIK_RUNTIME = ('authentik-server', 'authentik-worker')
    _AUTHENTIK_READY_SECONDS = 300
    _AUTHENTIK_INIT_SECONDS = 900

    def _wait_authentik_ready(self, action, services, timeout=None, interval=3):
        """#2816: block until each of `services` is running and, when it has a
        healthcheck, healthy. authentik-init copies scripts into the worker and
        execs there; started while the worker is being recreated it hit "No such
        container" and skipped the outpost reconcile. Raises RuntimeError with
        the reason on timeout."""
        timeout = self._AUTHENTIK_READY_SECONDS if timeout is None else timeout
        deadline = time.monotonic() + timeout
        pending = {}
        while True:
            pending = {}
            for svc in services:
                try:
                    r = subprocess.run(
                        ['docker', 'inspect', '-f',
                         '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}', svc],
                        capture_output=True, text=True, timeout=30)
                    state = (r.stdout or '').split() if r.returncode == 0 else ['missing']
                except (OSError, subprocess.SubprocessError):
                    state = ['unknown']
                ok = bool(state) and state[0] == 'running' and (len(state) < 2 or state[1] == 'healthy')
                if not ok:
                    pending[svc] = ' '.join(state) or 'unknown'
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    'Authentik did not come back after the recreate (' +
                    ', '.join(f'{k}: {v}' for k, v in sorted(pending.items())) +
                    f') within {timeout}s; authentik-init was NOT started because it needs both '
                    'containers. Check them, then run: rzfz post-install --refresh')
            time.sleep(interval)

    def _wait_authentik_init_result(self, action, timeout=None):
        """#2816: authentik-init is started detached. A run that skipped the
        module-binding / outpost reconcile exits non-zero; the apply must fail
        with that, not report success. Raises RuntimeError."""
        timeout = self._AUTHENTIK_INIT_SECONDS if timeout is None else timeout
        action.add_line('Waiting for authentik-init to finish...')
        try:
            r = subprocess.run(['docker', 'wait', 'authentik-init'], capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'authentik-init did not finish within {timeout}s; the outpost '
                               'bindings are unverified. Run: rzfz post-install --refresh')
        code = (r.stdout or '').strip()
        action.add_line(f'  authentik-init exited {code or "?"}')
        if r.returncode != 0 or code != '0':
            raise RuntimeError(f'authentik-init failed (exit {code or "?"}): the module bindings / '
                               'outpost reconcile did not complete (see its log above). Run: '
                               'rzfz post-install --refresh')

    def apply_domain_change(self, old_domain, new_domain, user, source_ip, risk='danger'):
        """Full re-domain apply (ga.15). After ConfigManager.update_domain has
        rewritten the *_DOMAIN .env vars, this: (1) reconciles the OpenWebUI DB
        (webui.url / oauth.provider_url persist the old host and override env),
        (2) re-templates Authentik blueprints for the new domain (authentik-init;
        the #183 handler fires because MAIN_DOMAIN now differs — works offline via
        the baked init tools), (3) clears Authentik's cache so the re-templated
        sources/providers are served immediately, and (4) recreates every RUNNING
        domain-baking service so it re-reads the new host. Returns (action_id, err)."""
        err = self._host_path_block_reason()
        if err:
            return None, err

        with self._lock:
            if self._current_action and not self._current_action.done:
                return None, ApplyBusy(self._current_action)
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

            # 1b) #2816: recreate the Authentik containers FIRST and wait for them,
            #     so authentik-init (which copies scripts into authentik-worker and
            #     execs there) never starts while the worker is being recreated.
            running_now = self._running_services() or set()
            authentik_targets = [s for s in self._AUTHENTIK_RUNTIME if s in running_now]
            for svc in authentik_targets:
                action.add_line(f'Recreating {svc} before the blueprint re-template...')
                self._run_stream(action, self._compose('up', '-d', '--no-deps',
                                                       '--force-recreate', svc))
            if authentik_targets:
                self._wait_authentik_ready(action, authentik_targets)

            # 2) Re-template Authentik blueprints for the new domain.
            action.add_line('Re-templating Authentik blueprints (authentik-init)...')
            self._run_stream(action, self._compose('up', '-d', '--no-deps',
                                      '--force-recreate', 'authentik-init'))
            # #2816: a skipped binding / outpost reconcile exits non-zero: fail the apply.
            self._wait_authentik_init_result(action)

            # 2b) The database rows the blueprints do not rewrite (proxy
            #     provider external_host, the outpost's browser host, OAuth2
            #     redirect URIs, launch URLs). Nonfatal like the CA refresh
            #     below: a leftover must not stop the cache clear and the
            #     recreate that follow.
            db_unverified = False
            if old_domain and old_domain != new_domain:
                action.add_line('Rewriting the old domain in the Authentik and Open WebUI databases...')
                try:
                    self._run_stream(action, ['bash', 'scripts/redomain-reconcile.sh', '--db-only',
                                              old_domain, new_domain])
                except Exception as e:
                    _cmd = host_command(f'scripts/redomain-reconcile.sh --db-only {old_domain} {new_domain}')
                    action.add_line(f'  WARN: the database rewrite did not finish ({e}); on the host, '
                                    f'run: {_cmd} (non-fatal here)')
                    db_unverified = True

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
            # `or set()`: #1285 rev-B made _running_services return None when
            # compose could not be asked. Here that stays what it always was —
            # recreate nothing, the re-domain is best-effort anyway.
            running = self._running_services() or set()
            targets = [s for s in self._DOMAIN_BAKING_SERVICES
                       if s in running and s not in authentik_targets]
            if targets:
                action.add_line(f'Recreating domain-baking services (best-effort): {", ".join(targets)}')
                skipped = []
                for svc in targets:
                    try:
                        self._run_stream(action, self._compose('up', '-d',
                                                  '--no-deps', '--force-recreate', svc))
                    except Exception as e:
                        skipped.append(svc)
                        action.add_line(f'  WARN: could not recreate {svc} ({e}); continuing')
                if skipped:
                    action.add_line(f'Recreated; skipped {len(skipped)}: {", ".join(skipped)}')
            else:
                action.add_line('No domain-baking services running to recreate.')

            # #631: a re-domain can make Caddy mint a NEW internal PKI.
            # certs/caddy-ca.pem is a file SNAPSHOT of that root — without a
            # refresh here, every OIDC client that mounts it (openwebui,
            # gitea, vaultwarden) keeps trusting the OLD root and the next
            # FRESH login 500s with CERTIFICATE_VERIFY_FAILED while cookie
            # sessions keep working: the silent time bomb seen live on 0.91
            # ("SSO broken after maintenance"). ensure_oidc_ca_superset
            # (scripts/lib.sh) rebuilds the superset bundle from the LIVE
            # caddy root and restarts the mount consumers itself — including
            # vaultwarden, which is deliberately NOT in
            # _DOMAIN_BAKING_SERVICES.
            action.add_line('Refreshing OIDC CA trust bundle from the live Caddy root (#631)...')
            try:
                self._run_stream(action, [
                    'bash', '-c',
                    'cd "$1" && source scripts/lib.sh && ensure_oidc_ca_superset',
                    '_', self.stack_root])
            except Exception as e:
                # exit 3 = a container that mounts the bundle was running before
                # and is not running after the reload; other codes = the refresh
                # itself failed.
                if str(e).endswith('exited 3'):
                    action.add_line(
                        f'  WARN: a container that mounts the OIDC CA bundle is not '
                        f'running after the reload (named above, with the command that '
                        f'recreates it) — run that command, then '
                        f'`rzfz post-install --refresh` (non-fatal here)')
                else:
                    action.add_line(
                        f'  WARN: CA-bundle refresh failed ({e}) — if Caddy re-minted '
                        f'its PKI, fresh OIDC logins will fail until '
                        f'`rzfz post-install --refresh` runs (non-fatal here)')

            if db_unverified:
                # nonfatal by design, but never reported as a plain success:
                # this line and the audit detail are the only record of it.
                _cmd = host_command(f'scripts/redomain-reconcile.sh --db-only {old_domain} {new_domain}')
                action.add_line(f'Done with warnings: the database rewrite is NOT verified '
                                f'(on the host, run: {_cmd}).')
            else:
                action.add_line('Done.')
            # CFG-7/CFG-8: audit BEFORE finish(), and never let an
            # audit-write failure flip a completed re-domain to failed.
            self._audit_before_done(
                action, 'config.apply', user=user, source_ip=source_ip,
                category='settings', action='redomain',
                detail=description + ('; database rewrite NOT verified' if db_unverified else ''),
                containers_affected=targets, risk=risk, outcome='success',
            )
            action.finish(success=True)
            self._auto_checksum(description)  # CFG-30: after finish().
        except Exception as e:
            action.add_line(f'Error: {e}')
            self._audit_before_done(
                action, 'config.apply', user=user, source_ip=source_ip,
                category='settings', action='redomain', detail=description,
                containers_affected=targets, risk=risk, outcome='failure',
                error=str(e))
            action.finish(success=False, error=str(e))

    def _reconcile_openwebui_domain(self, action, env, old_domain, new_domain):
        """Rewrite the old domain to the new one inside the OpenWebUI persisted
        config JSON, by WHOLE host: the shared scripts/sql/redomain-openwebui.sql
        that `rzfz setup --set-domain` runs too (`mybox.local` survives a
        `box.local` re-domain; both the `data` and the `value` schema; one
        transaction). Best-effort (non-fatal)."""
        pg_user = env.get('POSTGRES_USER') or 'docker'
        pg_pw = env.get('POSTGRES_PASSWORD', '')
        owui_db = env.get('OPENWEBUI_DB') or 'openwebui_db'
        try:
            with open(os.path.join(self.stack_root, 'scripts', 'sql', 'redomain-openwebui.sql'),
                      encoding='utf-8') as fh:
                sql = fh.read()
        except OSError as e:
            action.add_line(f'  OpenWebUI reconcile skipped: the shared SQL is missing ({e}).')
            return
        action.add_line(f'Reconciling OpenWebUI persisted domain ({old_domain} -> {new_domain})...')
        # psql `:'var'` interpolation does NOT happen for the `-c` argument, only
        # for SQL read from STDIN / a file, hence input=. The password goes in
        # by NAME (`-e PGPASSWORD` takes it from this process's env), never in argv.
        try:
            out = subprocess.run(
                ['docker', 'exec', '-i', '-e', 'PGPASSWORD', 'postgres', 'psql', '-X', '-q', '-tA',
                 '-v', 'ON_ERROR_STOP=1', '-U', pg_user, '-d', owui_db,
                 '-v', f'old_re={re.escape(old_domain)}', '-v', f'new={new_domain}'],
                input=sql, capture_output=True, text=True, timeout=60,
                env={**os.environ, 'PGPASSWORD': pg_pw})
            # Counts and names only (the SQL's report lines, psql's own error
            # line): a DETAIL/CONTEXT line can quote a fragment of the row.
            for line in ((out.stdout or '') + (out.stderr or '')).strip().splitlines():
                if re.match(r'^(rewritten|skipped|leftover|warn|warn-regex) |^\d+$|^psql:|^ERROR:|^WARNING: +missing ', line):
                    action.add_line(f'  {line}')
            if out.returncode != 0:
                action.add_line(f'  OpenWebUI reconcile psql exited {out.returncode}: rolled back, '
                                f'nothing changed (non-fatal)')
        except Exception as e:
            action.add_line(f'  OpenWebUI reconcile error (non-fatal): {e}')

    def _psql_run(self, pg_user, pg_pw, db, sql):
        """#2471: a scalar psql query; None on ANY error (the data-bound guard
        must tell 'no answer' from 'empty' — _psql_scalar cannot). The password
        travels in the subprocess environment, never in docker's argv."""
        try:
            out = subprocess.run(
                ['docker', 'exec', '-e', 'PGPASSWORD', 'postgres', 'psql',
                 '-U', pg_user, '-d', db, '-tAc', sql],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, 'PGPASSWORD': pg_pw})
        except Exception:
            return None
        return out.stdout if out.returncode == 0 else None

    def _psql_scalar(self, pg_user, pg_pw, db, sql):
        """Run a scalar psql query inside the postgres container; '' on any error."""
        try:
            # #2746: forwarded by name; the value lives in the client's environment
            out = subprocess.run(
                ['docker', 'exec', '-e', 'PGPASSWORD', 'postgres', 'psql',
                 '-U', pg_user, '-d', db, '-tAc', sql],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, 'PGPASSWORD': pg_pw})
            return out.stdout.strip()
        except Exception:
            return ''

    #: #1285 rev-B: "not down" is more than `running`. A container autoheal has
    #: just `docker restart`ed sits in `restarting` while it backs off, and a
    #: `paused` container is not stopped either — neither appears under
    #: `--status running`. Measured on compose 2.40.3 (fleet standard) with a
    #: crash-looping busybox: 24 of 25 samples had the container in `restarting`,
    #: invisible to `--status running` and visible with `--status restarting`;
    #: same for `paused`. The flag is repeatable.
    NOT_DOWN_STATUSES = ('running', 'restarting', 'paused')

    def _running_services(self, profile_id=None, statuses=('running',)):
        """Compose service names currently in one of `statuses`; None on error.

        `profile_id` (#1285 rev-B): compose is asked with ``--profile`` when the
        caller knows which profile it is talking about, mirroring
        ``_compose_profile_services`` (#538) and
        ``post_toggle_probe.query_service_states``, so the answer never depends
        on the ``COMPOSE_PROFILES`` write having landed in compose's view. The
        toggle writes ``.env`` BEFORE it stops anything, so by the time a
        disable verifies, the profile is already gone from ``.env``.

        Returns **None** when compose could not be asked (non-zero exit,
        timeout, no docker). The old contract folded that into an empty set,
        which reads as "nothing is running" — for a verification that is a false
        all-clear, so the two cases are now distinguishable and every caller
        decides for itself.
        """
        # #1226: build the argv tail FIRST so `_compose` sees the verb it is
        # validating for (an empty call would look like a read-only run).
        tail = ['--profile', profile_id] if profile_id else []
        tail += ['ps', '--services']
        for status in statuses:
            tail += ['--status', status]
        cmd = self._compose(*tail)
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 cwd=self.stack_root, env=self._clean_env(), timeout=30)
            if out.returncode != 0:
                return None
            return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}
        except Exception:
            return None

    def _autoheal_labelled_services(self, profile_id):
        """Services of `profile_id` that autoheal is allowed to restart.

        autoheal (core/compose.yml) only touches containers carrying the label
        named by ``AUTOHEAL_CONTAINER_LABEL`` — `autoheal`. That opt-in is what
        bounds the #1285 race: a profile with no labelled service has nothing
        that can start its containers behind the operator's back, and its
        disable does not have to be watched for a full autoheal round.

        Returns None when the compose model cannot be read, so the caller can
        watch for the long window rather than trust a blank answer.
        """
        try:
            result = subprocess.run(
                self._compose('--profile', profile_id,
                 'config', '--format', 'json'),
                capture_output=True, text=True, cwd=self.stack_root, env=self._clean_env(), timeout=120,
            )
            if result.returncode != 0:
                return None
            model = json.loads(result.stdout)
        except Exception:
            return None
        labelled = set()
        for name, svc in (model.get('services') or {}).items():
            if not isinstance(svc, dict):
                continue
            labels = svc.get('labels') or {}
            if isinstance(labels, list):
                labels = dict((item.split('=', 1) + [''])[:2]
                              for item in labels if isinstance(item, str))
            if not isinstance(labels, dict):
                continue
            if str(labels.get('autoheal', '')).strip().lower() in ('true', '1', 'yes'):
                labelled.add(name)
        return labelled

    #: #1285 — the disable watches for a container that comes back after the
    #: stop and re-stops it. rev-B sizes the watch on the ONE thing that can
    #: start a container nobody asked for, autoheal, instead of on a guess:
    #: `core/compose.yml` sets ``AUTOHEAL_INTERVAL: "30"`` (one health scan every
    #: 30 s — the file's own comment says "autoheal then `docker restart`s it
    #: within ~30s") and ``AUTOHEAL_DEFAULT_STOP_TIMEOUT: "30"`` (a restart the
    #: same scan queued ahead of ours blocks that long before ours runs). A scan
    #: that listed the container just before our stop can therefore land its
    #: restart tens of seconds later. rev-A watched 3 x 3 s = 9 s against a
    #: docstring that said the poll was 5 s; both numbers were wrong.
    #: 8 x 5 s = 40 s outlasts one full autoheal round.
    #:
    #: Only a RUNNING container can be listed by a health scan, so once it has
    #: stayed down for one round no later scan can bring it back — the watch is
    #: bounded, not a poll loop. `docker ps -f health=unhealthy` never lists a
    #: stopped container.
    STOP_VERIFY_ATTEMPTS = 3
    STOP_VERIFY_DELAY_S = 5.0
    #: Clean samples needed before a disable is declared verified when the
    #: profile owns an autoheal-labelled service …
    STOP_VERIFY_WATCH_SAMPLES = 8
    #: … and when it does not: nothing in the stack can restart those, so one
    #: confirming sample after the stop is the whole guarantee that is available.
    STOP_VERIFY_QUICK_SAMPLES = 1

    def _verify_stopped(self, action, services, profile_id):
        """After a disable, make sure the services are really down (#1285).

        The Portal's disable is `docker compose stop`. autoheal
        (core/compose.yml, willfarrell/autoheal, container label `autoheal`)
        issues `docker restart` on any labelled container it finds UNHEALTHY —
        and `docker restart` STARTS a container that was just stopped. A stop
        that lands inside such an unhealthy window therefore leaves the module
        running on a DISABLED profile. Measured on 0.91 (Phase-3 round 3):
        presidio-analyzer was still `Up` three hours after its disable while its
        siblings sat cleanly at `Exited (0)`; its warm-up made it unhealthy
        (#1260 rev-B widened that budget, which removes the usual trigger — this
        closes the race itself, for every present and future autoheal service).

        rev-B: the first sample is taken AFTER a settle delay, never before it.
        rev-A returned "verified" on the very first sample, which is the one
        moment the defect cannot show in: autoheal's sequence is
        `docker ps -f health=unhealthy` and only THEN `docker restart`, so a
        stop landing between the two is answered by a container that is still
        down when asked and back a moment later. That is the normal shape of
        the race, not an edge case.

        Never fails the toggle — the profile IS disabled in `.env` either way,
        and a stubborn container is reported as a warning the operator can act
        on rather than a rollback of a completed change.

        Returns a summary dict for the action stream AND the audit entry:
        ``{'verified': True|False|None, 'still_running': [...], 're_stops': n,
        'samples': n, 'watched': bool}``. ``verified=None`` means compose could
        not be asked — explicitly not the same as "verified".
        """
        wanted = set(services)
        summary = {'verified': True, 'still_running': [], 're_stops': 0,
                   'samples': 0, 'watched': False}
        if not wanted:
            return summary

        # Sizing: watch the long window when something in this profile is
        # allowed to be restarted by autoheal — or when we could not find out.
        labelled = self._autoheal_labelled_services(profile_id)
        at_risk = sorted(wanted & labelled) if labelled is not None else None
        if at_risk is None:
            needed = self.STOP_VERIFY_WATCH_SAMPLES
            action.add_line(
                'Could not read the compose model to see which containers autoheal '
                f'may restart — watching all of them for '
                f'{int(needed * self.STOP_VERIFY_DELAY_S)} s (#1285).')
        elif at_risk:
            needed = self.STOP_VERIFY_WATCH_SAMPLES
            action.add_line(
                f'{", ".join(at_risk)} carries the `autoheal` label: autoheal restarts '
                'an unhealthy container, and a restart starts a stopped one. Watching '
                f'for {int(needed * self.STOP_VERIFY_DELAY_S)} s that it stays '
                'down (#1285)...')
        else:
            needed = self.STOP_VERIFY_QUICK_SAMPLES
        summary['watched'] = needed > self.STOP_VERIFY_QUICK_SAMPLES

        clean = 0
        while True:
            time.sleep(self.STOP_VERIFY_DELAY_S)
            summary['samples'] += 1
            running = self._running_services(profile_id,
                                             statuses=self.NOT_DOWN_STATUSES)
            if running is None:
                action.add_line(
                    'Could not verify that the containers are stopped: compose did not '
                    'answer `ps`. The profile IS disabled in .env — check `docker ps` '
                    'for leftovers of this module (#1285).')
                summary['verified'] = None
                return summary

            back = sorted(wanted & running)
            if not back:
                clean += 1
                if clean < needed:
                    continue
                if summary['re_stops'] or summary['watched']:
                    action.add_line(
                        'Verified: every container of the profile is stopped.')
                return summary

            clean = 0
            if summary['re_stops'] >= self.STOP_VERIFY_ATTEMPTS:
                break
            summary['re_stops'] += 1
            action.add_line(
                f'Still running after the stop: {", ".join(back)} — a container that was '
                'unhealthy at that moment gets restarted by autoheal, and a restart starts '
                f'a stopped container (#1285). Stopping again ({summary["re_stops"]}/'
                f'{self.STOP_VERIFY_ATTEMPTS})...')
            # #1226: verb-first, so the allow-list check sees `stop`.
            tail = ['--profile', profile_id] if profile_id else []
            tail += ['stop'] + back
            cmd = self._compose(*tail)
            try:
                subprocess.run(cmd, capture_output=True, text=True,
                               cwd=self.stack_root, env=self._clean_env(), timeout=120)
            except Exception as exc:  # noqa: BLE001 — a failed re-stop is reported, never fatal
                action.add_line(f'Re-stop failed: {exc}')
                break

        action.add_line(
            f'WARNING: {", ".join(back)} still running after '
            f'{summary["re_stops"]} stop attempts. The profile is disabled in '
            '.env, but something keeps starting the container — check its health and '
            'whether it carries the `autoheal` label (#1285).')
        summary['verified'] = False
        summary['still_running'] = back
        return summary

    def _run_stream(self, action, cmd):
        """Run a command streaming stdout into the action; raise on non-zero exit."""
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, cwd=self.stack_root, env=self._clean_env())
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
                                text=True, cwd=self.stack_root, env=self._clean_env())
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
        err = self._host_path_block_reason()
        if err:
            return None, err

        # Same direction rule as the CLI's pin sync: a service that migrates
        # its data is never lowered (reachable only by a crafted request —
        # the manifest normally offers newer versions).
        allowed, why = pin_sync_decision(env_var, self._read_env_var(env_var), new_version)
        if not allowed:
            return None, why

        # #3412: the update writes the version to .env and then recreates; a
        # chain the allow-list refuses would stop it between the two.
        err = self._compose_chain_block_reason()
        if err:
            return None, err

        err = self._env_bind_block_reason()
        if err:
            return None, err

        with self._lock:
            if self._current_action and not self._current_action.done:
                return None, ApplyBusy(self._current_action)

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
                self._compose('pull') + self._to_services(containers),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=self.stack_root, env=self._clean_env(),
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
                # CFG-9 (same class): audit the failure, before finish().
                self._audit_before_done(
                    action, 'config.apply', user=user, source_ip=source_ip,
                    category='update', action='image_update',
                    detail=f'Failed: {description} (pull)',
                    containers_affected=containers,
                    risk='caution', outcome='failure',
                    error=f'docker compose pull failed (exit {pull_code})',
                )
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
                    self._compose('stop') + self._to_services(containers),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, cwd=self.stack_root, env=self._clean_env(),
                )
                for line in iter(stop.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        action.add_line(f'  {line}')
                stop.wait()

                action.add_line('Done.')
                # CFG-8: audit BEFORE finish().
                self._audit_before_done(
                    action, 'config.apply', user=user, source_ip=source_ip,
                    category='update', action='image_update',
                    detail=f'{description} (module disabled — recreate skipped, #176)',
                    containers_affected=containers,
                    risk='caution', outcome='success',
                )
                action.finish(success=True)
                self._auto_checksum(description)  # CFG-30: after finish().
                return

            # Step 3: Recreate containers
            action.add_line(f'Recreating: {", ".join(containers)}...')
            up = subprocess.Popen(
                self._compose('up', '-d', '--no-deps', '--force-recreate') + self._to_services(containers),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=self.stack_root, env=self._clean_env(),
            )
            for line in iter(up.stdout.readline, ''):
                line = line.rstrip()
                if line:
                    action.add_line(line)
            up_code = up.wait()

            if up_code == 0:
                action.add_line('Done.')
                # CFG-8: audit BEFORE finish().
                self._audit_before_done(
                    action, 'config.apply', user=user, source_ip=source_ip,
                    category='update', action='image_update',
                    detail=description,
                    containers_affected=containers,
                    risk='caution', outcome='success',
                )
                action.finish(success=True)
                self._auto_checksum(description)  # CFG-30: after finish().
            else:
                action.add_line(f'Error: exit code {up_code}')
                if old_version is not None:
                    self._update_env_var(env_var, old_version)
                    action.add_line(f'Reverted {env_var} to {old_version} (recreate failed).')
                # CFG-9 (same class): a failed image update was silently
                # unlogged while its success twin was audited.
                self._audit_before_done(
                    action, 'config.apply', user=user, source_ip=source_ip,
                    category='update', action='image_update',
                    detail=f'Failed: {description}',
                    containers_affected=containers,
                    risk='caution', outcome='failure',
                    error=f'exit code {up_code}',
                )
                action.finish(success=False, error=f'exit code {up_code}')
        except Exception as e:
            action.add_line(f'Error: {e}')
            self._audit_before_done(
                action, 'config.apply', user=user, source_ip=source_ip,
                category='update', action='image_update',
                detail=f'Failed: {description}: {e}',
                containers_affected=containers,
                risk='caution', outcome='failure', error=str(e),
            )
            action.finish(success=False, error=str(e))

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
        # Truncate IN PLACE (same inode) — see config_manager._write_env_file
        # and env_mount.py (#1189). Third Portal .env writer (image-update
        # flow); a stale bind otherwise leaks the bare EROFS into the action
        # stream. `env_write_guard` is a module-level import (like
        # config_manager's), not a call-time one: the test tiers evict
        # `app*` from sys.modules between files, and a call-time import
        # would raise a StaleEnvMountError from a re-imported module whose
        # class no test can name (rev-B).
        with env_write_guard(self.env_path):
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
                self._compose('--profile', profile_id,
                 'config', '--format', 'json'),
                capture_output=True, text=True, cwd=self.stack_root, env=self._clean_env(), timeout=120,
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
        # #1443 (cutover C3): always-on modules — the LLM Manager trio — can be
        # enabled (a box that predates the cutover) but never disabled from
        # here: the manager IS `llm`, every consumer on the box wires to it.
        if not enable and profile.get('always_on'):
            return {'error': f'{profile.get("name", profile_id)} is always-on since the LLM-Manager cutover (#979) and cannot be disabled'}

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
        # CFG-10: the shape below is unchanged; the #946 NVIDIA rule is
        # applied through the shared helper further down.
        for excl in profile.get('mutual_exclusion', []):
            if excl in enabled:
                excl_profile = self.profile_manager.get_profile(excl)
                excl_name = excl_profile.get('name', excl) if excl_profile else excl
                exclusion_conflict = f'Cannot enable: conflicts with {excl_name} (mutually exclusive)'

        # #946: NVIDIA must never enable the retired v2.x `llm` profile — direct
        # the operator to llm-legacy + HARDWARE=nvidia (GPUStack 0.7.1 + custom
        # CUDA, #1448) instead.
        # CFG-10: shared with the WRITE path (apply_profile_toggle) so the two
        # can never disagree.
        exclusion_conflict = (self._llm_nvidia_block_reason(profile_id, enable)
                              or exclusion_conflict)

        # A deprecated module still works, but turning it on deserves a warning
        # in the dialog the operator confirms.
        deprecation_warning = ''
        if enable and profile.get('maturity') == 'deprecated':
            deprecation_warning = profile.get('deprecation_note') or (
                f'{profile.get("name", profile_id)} is deprecated and will be removed in a later release.')
        impact_text = profile.get('enable_impact', '') if enable else profile.get('disable_impact', '')
        if deprecation_warning:
            impact_text = f'DEPRECATED: {deprecation_warning} {impact_text}'.strip()

        return {
            'action': 'profile_toggle',
            'profile_id': profile_id,
            'profile_name': profile.get('name', profile_id),
            'enable': enable,
            'deprecation_warning': deprecation_warning,
            'containers': containers,
            'container_count': len(containers),
            'ram_estimate_mb': ram_mb,
            'risk': risk,
            'enable_impact': profile.get('enable_impact', ''),
            'disable_impact': profile.get('disable_impact', ''),
            'impact_text': impact_text,
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

    #: #946 — the retired GPUStack v2.1.x `llm` profile has no NVIDIA path.
    LLM_NVIDIA_BLOCK_REASON = (
        'Cannot enable `llm` (GPUStack v2.1.x) on NVIDIA — that runtime is '
        'retired here. Enable `llm-legacy` instead (GPUStack 0.7.1 + custom '
        'CUDA llama.cpp; HARDWARE=nvidia picks the CUDA overlay, #1448).')

    def _llm_nvidia_block_reason(self, profile_id, enable):
        """Return the #946 block reason, or None when the toggle is allowed.

        CFG-10 — one predicate, used by BOTH `preview_profile_toggle` (which
        renders it as `exclusion_conflict` so the UI greys the button out)
        and `apply_profile_toggle` (which refuses the write). Before this the
        rule existed only in the preview, i.e. only in JavaScript.
        """
        # #1447: the `llm` profile it guarded is removed, so nothing can be
        # enabled into the blocked state any more. Kept as the single predicate
        # both the preview and the write consult (CFG-10) — it now always
        # allows, and the NVIDIA rule lives in the profile set itself.
        if not (enable and profile_id == 'llm'):
            return None
        try:
            hw = (self._read_env_value('HARDWARE') or 'amd').strip()
        except Exception:
            return None
        return self.LLM_NVIDIA_BLOCK_REASON if hw == 'nvidia' else None

    #: `{env.FOO}` and `{$FOO}` — the two spellings the Caddyfile uses to reach
    #: the process environment. Both are read, because both appear.
    _CADDY_ENV_REF = re.compile(r'\{\s*(?:env\.|\$)([A-Z][A-Z0-9_]*)\s*\}')

    def _caddy_env_keys(self):
        """Which env vars the SHIPPED Caddyfile reads.

        Derived from the file, never maintained as a list (#1899): a new module
        whose credential Caddy injects then falls under this automatically, and
        nobody has to remember. An unreadable Caddyfile yields an empty set —
        the caller then does nothing, which is the old behaviour.
        """
        try:
            with open(os.path.join(self.stack_root, 'core', 'Caddy', 'Caddyfile'),
                      encoding='utf-8') as fh:
                return set(self._CADDY_ENV_REF.findall(fh.read()))
        except OSError:
            return set()

    def _mint_llm_manager_service_key(self, service):
        """#2149: mint `stack/<service>` on the LLM Manager for a consumer the
        Portal is enabling — the SAME function post-install uses
        (`_llm_manager_mint_service_key`, scripts/lib-owui.sh: docker exec into
        llm-manager, operator privilege, no new network endpoint), so the two
        enable paths cannot drift. Captured, never streamed: the plaintext key
        must not land in the action log. Returns '' when the manager is not up
        or the mint fails; the provisioner then says so and names the refresh."""
        try:
            out = subprocess.run(
                ['bash', '-c',
                 'cd "$1" && source scripts/lib.sh && source scripts/lib-owui.sh '
                 '&& _llm_manager_mint_service_key "$2"',
                 '_', self.stack_root, service],
                capture_output=True, text=True, timeout=60, cwd=self.stack_root,
                env=self._clean_env())
        except Exception:
            return ''
        if out.returncode != 0:
            return ''
        key = (out.stdout or '').strip().splitlines()
        return key[-1].strip() if key else ''

    def _recreate_caddy_for_new_secrets(self, action, minted_keys):
        """Recreate `caddy` iff the toggle just minted a value Caddy INJECTS.

        #1899, measured on 0.91: enabling crawl4ai mints CRAWL4AI_API_TOKEN, and
        the Caddyfile injects it as `Bearer {env.CRAWL4AI_API_TOKEN}`. Caddy
        receives its environment through `env_file`, which docker reads when the
        container is CREATED — so a caddy created before the mint carries the
        old (empty) value, injects `Bearer `, and crawl4ai answers
        `{"detail": "Authentication required"}` for a module the operator just
        enabled successfully.

        Same class as #1878: a value minted at enable-time, and a long-running
        consumer of it that nobody re-creates.

        Deliberately NOT every toggle. ga.6 cut the cascade recreate on purpose
        (`--no-deps`), and recreating the one container that terminates every
        connection on the box is not something to do on a hunch. Only when the
        intersection is non-empty — i.e. Caddy's environment is provably stale.
        """
        overlap = sorted(minted_keys & self._caddy_env_keys())
        if not overlap:
            return None
        action.add_line(
            f'Caddy injects {", ".join(overlap)}, which this enable just '
            f'generated — recreating caddy so it carries the new value...')
        proc = subprocess.Popen(
            self._compose('up', '-d', '--no-deps', '--force-recreate', 'caddy'),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=self.stack_root, env=self._clean_env(),
        )
        for line in iter(proc.stdout.readline, ''):
            line = line.rstrip()
            if line:
                action.add_line(line)
        code = proc.wait()
        if code != 0:
            action.add_line(f'caddy recreate exited {code} (non-fatal) — the '
                            f'module is up but its route may still answer '
                            f'"unauthenticated" until caddy is recreated by hand')
        return code

    _CADDY_GATE_LOOP = re.compile(r'^for _gate in ([A-Z0-9_:a-z -]+); do', re.M)

    def _caddy_gated_profiles(self):
        """The compose profiles whose site names Caddy's entrypoint gates (#2955).

        Read from core/Caddy/entrypoint.sh (the `for _gate in KEY:PROFILE ...`
        line), never kept as a second list: a newly gated key falls under the
        recreate by itself. Unreadable file -> empty set (do nothing)."""
        try:
            with open(os.path.join(self.stack_root, 'core', 'Caddy', 'entrypoint.sh'),
                      encoding='utf-8') as fh:
                m = self._CADDY_GATE_LOOP.search(fh.read())
        except OSError:
            return set()
        if not m:
            return set()
        return {pair.split(':', 1)[1] for pair in m.group(1).split() if ':' in pair}

    def _recreate_caddy_for_gated_profiles(self, action, changed_profiles):
        """Recreate `caddy` iff a profile it gates site names on just changed.

        #2955, measured on a box: `matrix.<domain>` / `element.<domain>` failed
        TLS after the Matrix enable. The entrypoint (#2920) turns a name whose
        module is off into an inert `<label>.off.internal` using the
        COMPOSE_PROFILES of the caddy CONTAINER, fixed when it was created, so a
        caddy older than the toggle kept the module's names inert. Disabling is
        the same mechanism the other way round: the names go inert again.
        Only a change to a gated profile recreates caddy (it terminates every
        connection on the box). Returns None when nothing was needed."""
        hit = sorted(set(changed_profiles) & self._caddy_gated_profiles())
        if not hit:
            return None
        action.add_line(
            f'Caddy gates site names on {", ".join(hit)} - recreating caddy so '
            f'they follow the new module state...')
        proc = subprocess.Popen(
            self._compose('up', '-d', '--no-deps', '--force-recreate', 'caddy'),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=self.stack_root, env=self._clean_env(),
        )
        for line in iter(proc.stdout.readline, ''):
            line = line.rstrip()
            if line:
                action.add_line(line)
        code = proc.wait()
        if code != 0:
            action.add_line(f'caddy recreate exited {code} (non-fatal) - the '
                            f'module site names stay as they were until caddy '
                            f'is recreated by hand')
        return code

    def _reconcile_service_databases(self, action):
        """Create/refresh the per-service postgres roles from .env (#1878).

        The `postgres-db-reconcile` one-shot re-applies every per-service
        role/password in `.env` and is idempotent. `--force-recreate` is the
        load-bearing flag: without it compose sees a container that already
        exited 0 at install and does nothing, which is exactly how a role minted
        by a later profile-enable never reaches postgres.

        Never fatal. A box without the postgres profile has nothing to
        reconcile, and a toggle must not fail on that.
        """
        action.add_line('Reconciling per-service databases (postgres-db-reconcile)...')
        db_proc = subprocess.Popen(
            self._compose('up', '-d', '--no-deps',
                          '--force-recreate', 'postgres-db-reconcile'),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=self.stack_root, env=self._clean_env(),
        )
        for line in iter(db_proc.stdout.readline, ''):
            line = line.rstrip()
            if line:
                action.add_line(line)
        db_code = db_proc.wait()
        if db_code != 0:
            action.add_line(f'postgres-db-reconcile exited {db_code} (non-fatal)')
        return db_code

    def apply_profile_toggle(self, profile_id, enable, user, source_ip):
        """Execute a profile toggle. Returns action_id for SSE streaming."""
        err = self._host_path_block_reason()
        if err:
            return None, err

        with self._lock:
            if self._current_action and not self._current_action.done:
                return None, ApplyBusy(self._current_action)

        profile = self.profile_manager.get_profile(profile_id)
        if not profile:
            return None, f'Unknown profile: {profile_id}'

        # CFG-10 / #946: the "never enable the retired v2.x `llm` profile on
        # NVIDIA" rule was enforced ONLY by preview_profile_toggle's
        # `exclusion_conflict`, whose sole consumer is main.js (it greys the
        # button out). A direct `POST /api/apply` from an authenticated admin
        # therefore still produced exactly the configuration #946 exists to
        # make unreachable. Re-check it server-side, on the write path,
        # before any .env mutation.
        err = self._llm_nvidia_block_reason(profile_id, enable)
        if err:
            return None, err
        # #1443 review (finding 5): the SAME asymmetry was open for `core`, and
        # it is the worst instance of it. preview_profile_toggle refuses
        # profile_id == 'core' outright; the write path did not, and `core`
        # carries no `always_on`. So POST /api/apply with profile_id=core and
        # enable=false ran through to `docker compose stop caddy postgres
        # authentik …` — without even the admin-password prompt, because that
        # is gated on risk_on_toggle and core declares none. An authenticated
        # admin could stop the box's own front door through the API.
        #
        # The refusal belongs where the write happens, in the same words the
        # preview uses. Pre-existing, older than this PR, and fixed here
        # because this PR is what made the write path the place that decides.
        if profile_id == 'core':
            return None, 'Core infrastructure cannot be toggled'
        # #1443 (cutover C3): same lesson, same place — the always-on lock must
        # hold on the WRITE path, not only in the preview main.js greys out.
        if not enable and profile.get('always_on'):
            return None, f'{profile.get("name", profile_id)} is always-on since the LLM-Manager cutover (#979) and cannot be disabled'

        err = self._env_bind_block_reason()
        if err:
            return None, err

        action_word = 'Enable' if enable else 'Disable'
        action_id = str(uuid.uuid4())[:8]
        action = ApplyAction(action_id, f'{action_word} {profile.get("name", profile_id)}')

        with self._lock:
            # #2993: checked again where it is set: two toggles confirmed at
            # the same moment both passed the first check.
            if self._current_action and not self._current_action.done:
                return None, ApplyBusy(self._current_action)
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

    # ── #3077: the per-user agents follow the agents module ─────────────────
    # The agent manager creates every user's agent through the docker API, so
    # they are not compose services and `compose stop` of the module left them
    # running, while the preview said they are stopped. They are now stopped
    # (containers and their data volumes kept) after the module's own
    # services, so the agent manager is already down and cannot act on them.
    #
    # Stopping alone would NOT be safe: when the module is turned on again the
    # agent manager marks each agent whose container it finds stopped as
    # `stopped` (stamping stopped_at), and its nightly cleanup deletes agents
    # that stayed stopped for AGENT_CLEANUP_AFTER_DAYS (30), volumes included.
    # So the names are kept in a snapshot in the Portal's persistent state
    # dir, and the enable starts exactly those again BEFORE the agent manager
    # starts: it finds them running, as they were. Agents a user had stopped
    # are not touched either way.
    AGENT_INSTANCE_LABEL = 'razzfazz.agent.instance'
    #: sandboxes an agent started through the docker guard; the shared
    #: OpenHands module's own (owner `shared`) are not the agents module's.
    AGENT_SANDBOX_OWNER_LABEL = 'razzfazz.docker-guard.owner'
    AGENT_SANDBOX_SHARED_OWNER = 'shared'
    AGENTS_SNAPSHOT_NAME = 'agents-stopped-with-module.json'

    def _agents_snapshot_path(self):
        if not self.state_dir:
            return None
        return os.path.join(self.state_dir, self.AGENTS_SNAPSHOT_NAME)

    def _running_agent_containers(self):
        """Names of the RUNNING per-user agent containers (instances, their
        companions, their sandboxes); None when docker could not be asked."""
        names = set()
        fmt = '{{.Names}}\t{{.Label "%s"}}' % self.AGENT_SANDBOX_OWNER_LABEL
        for label in (self.AGENT_INSTANCE_LABEL, self.AGENT_SANDBOX_OWNER_LABEL):
            try:
                r = subprocess.run(['docker', 'ps', '--filter', f'label={label}',
                                    '--format', fmt],
                                   capture_output=True, text=True, timeout=60,
                                   cwd=self.stack_root, env=self._clean_env())
            except Exception:  # noqa: BLE001 — reported by the caller
                return None
            if r.returncode != 0:
                return None
            for line in r.stdout.splitlines():
                name, _, owner = line.strip().partition('\t')
                if not name:
                    continue
                if (label == self.AGENT_SANDBOX_OWNER_LABEL
                        and owner == self.AGENT_SANDBOX_SHARED_OWNER):
                    continue
                names.add(name)
        return sorted(names)

    def _stop_agent_containers(self, action):
        """After the module's services are stopped: stop the per-user agents
        and remember them. Never fails the disable; what it could not do is
        said in the action stream and returned for the audit entry."""
        summary = {'stopped': [], 'failed': [], 'listed': True}
        path = self._agents_snapshot_path()
        if not path:
            action.add_line('Warning: the per-user agents are left running: this Portal has no '
                            'state directory to remember them, and stopping them without it would '
                            'let the agent manager clean them up later.')
            summary['listed'] = False
            return summary
        names = self._running_agent_containers()
        if names is None:
            action.add_line('Warning: could not list the per-user agent containers (docker did not '
                            'answer). They may still be running; stop them with '
                            '`docker stop $(docker ps -q --filter label=razzfazz.agent.instance)`.')
            summary['listed'] = False
            return summary
        if not names:
            action.add_line('No per-user agents are running.')
            return summary
        # remember BEFORE stopping: a stop that dies half way must still be
        # undone by the next enable. Merged with an older snapshot.
        from app.services import agents_snapshot
        try:
            agents_snapshot.write(path, names)
        except OSError as exc:
            action.add_line(f'Warning: the per-user agents are left running: they could not be '
                            f'remembered for the next enable ({exc}).')
            summary['listed'] = False
            return summary
        action.add_line(f'Stopping {len(names)} per-user agent container(s); their data volumes '
                        f'are kept and they start again when the module is turned on: '
                        f'{", ".join(names)}')
        try:
            r = subprocess.run(['docker', 'stop', *names], capture_output=True, text=True,
                               timeout=300, cwd=self.stack_root, env=self._clean_env())
            if r.returncode != 0 and r.stderr:
                action.add_line(f'  docker stop: {r.stderr.strip()[-400:]}')
        except Exception as exc:  # noqa: BLE001
            action.add_line(f'  docker stop failed: {exc}')
        still = self._running_agent_containers()
        if still is None:
            action.add_line('Warning: could not check that the per-user agents stopped.')
            summary['stopped'] = names
            return summary
        summary['failed'] = sorted(set(names) & set(still))
        summary['stopped'] = sorted(set(names) - set(still))
        if summary['failed']:
            action.add_line(f'Warning: still running after the stop: {", ".join(summary["failed"])}.')
        else:
            action.add_line(f'Stopped {len(names)} per-user agent container(s).')
        return summary

    def _start_remembered_agents(self, action):
        """On enable, BEFORE the agent manager starts: start the agents the
        last disable stopped. One that cannot start is left to the agent
        manager (it recreates an agent stranded on removed networks) or to its
        owner's Start in the Agent Portal."""
        from app.services import agents_snapshot
        path = self._agents_snapshot_path()
        names = agents_snapshot.read(path)
        if not names:
            return {'started': [], 'failed': []}
        action.add_line(f'Starting the {len(names)} per-user agent container(s) that were running '
                        f'when the module was turned off...')
        started, failed = [], []
        for name in names:
            try:
                r = subprocess.run(['docker', 'start', name], capture_output=True, text=True,
                                   timeout=120, cwd=self.stack_root, env=self._clean_env())
                (started if r.returncode == 0 else failed).append(name)
            except Exception:  # noqa: BLE001
                failed.append(name)
        if failed:
            action.add_line(f'  Could not start: {", ".join(failed)}. The agent manager starts an '
                            'agent again if its network was recreated; otherwise its owner can start '
                            'it in the Agent Portal.')
        action.add_line(f'  Started {len(started)} of {len(names)}.')
        agents_snapshot.clear(path)
        return {'started': started, 'failed': failed}

    def _execute_toggle(self, action, profile_id, profile, enable, user, source_ip):
        """Background execution of profile toggle.

        #2993: a job in four steps, each saying what a cancel does there
        (TOGGLE_CANCEL_NOTES): before the change and while the configuration
        is written, a cancel leaves the box as it was; while an enable starts
        the containers it stops the start and switches the module off again;
        a disable's stop and the final securing steps cannot be cancelled."""
        total = 4
        action.set_step('Checking prerequisites', 1, total, cancellable=True,
                        note=self.TOGGLE_CANCEL_NOTES['check'])
        profiles_before = None   # set when step 2 reads the module list (#3412)
        try:
            # #3412: the compose chain FIRST. Every later step ends in a compose
            # run the #1226 allow-list may refuse; judged only there, a disable
            # had already written COMPOSE_PROFILES and stopped half-applied.
            # Refused here, before every write (the mac-llm seed below included).
            chain_msg = self._compose_chain_block_reason()
            if chain_msg:
                action.add_line(f'Error: {chain_msg}')
                duration_ms = int((time.time() - action.start_time) * 1000)
                try:
                    self.audit_logger.log(
                        f'profile.{"enable" if enable else "disable"}',
                        user=user, source_ip=source_ip,
                        category='profile', action='enable' if enable else 'disable',
                        target=profile_id,
                        detail=f'{"Enable" if enable else "Disable"} of '
                               f'{profile.get("name", profile_id)} blocked — COMPOSE_FILE '
                               'is not allowed; nothing was changed',
                        outcome='failure', error=chain_msg,
                        duration_ms=duration_ms,
                    )
                except Exception as audit_exc:
                    action.add_line(
                        f'Warning: audit log write failed (non-fatal): {audit_exc!r}')
                action.finish(success=False, error=chain_msg)
                return

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
                # #2659: a PRESENT image that is not this tree's build is no
                # better than a missing one — compose would start it with this
                # release's bind-mounted code (an agent-manager crash loop,
                # #2659). The Portal cannot build, so it refuses the same way.
                not_ours = []
                if not missing:
                    try:
                        from app.services import build_preflight
                        not_ours = build_preflight.not_this_tree_build_images(
                            self.stack_root, profile_id)
                    except Exception:
                        not_ours = []
                if missing or not_ours:
                    from app.services import build_preflight
                    if missing:
                        msg = build_preflight.format_missing_error(profile_id, missing)
                        why = 'custom image(s) not built'
                        names = ', '.join(s for s, _ in missing)
                    else:
                        msg = build_preflight.format_not_this_tree_error(profile_id, not_ours)
                        why = "custom image(s) not this tree's build"
                        names = ', '.join(row[0] for row in not_ours)
                    action.add_line(f'Error: {msg}')

                    # #900: audit before done — same invariant as #743 (see
                    # the comment block on the success/failure/exception
                    # paths of this method): `action.done` is observed by
                    # other threads without the audit write's own lock, so
                    # `done == True` must always imply the audit entry
                    # already exists. An audit-write failure must never
                    # hang the action.
                    duration_ms = int((time.time() - action.start_time) * 1000)
                    try:
                        self.audit_logger.log(
                            'profile.enable',
                            user=user, source_ip=source_ip,
                            category='profile', action='enable', target=profile_id,
                            detail=f'Enable of {profile.get("name", profile_id)} '
                                   f'blocked — {why} ({names})',
                            outcome='failure', error=msg,
                            duration_ms=duration_ms,
                        )
                    except Exception as audit_exc:
                        action.add_line(
                            f'Warning: audit log write failed (non-fatal): {audit_exc!r}'
                        )

                    action.finish(success=False, error=msg)
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
                        "has not been rendered on this box. The Portal mounts the "
                        "repo read-only, so it cannot render it here — synapse would crash-loop on a missing config "
                        "path. Run `rzfz module prepare matrix` on the host (it "
                        "fills the matrix secrets and renders the config), then "
                        "enable matrix here again — or `rzfz module enable "
                        "matrix` does both."
                    )
                    action.add_line(f'Error: {msg}')

                    # #900: audit before done — same invariant as #743/the
                    # build-preflight gate above. An audit-write failure
                    # must never hang the action.
                    duration_ms = int((time.time() - action.start_time) * 1000)
                    try:
                        self.audit_logger.log(
                            'profile.enable',
                            user=user, source_ip=source_ip,
                            category='profile', action='enable', target=profile_id,
                            detail='Enable of Matrix blocked — Synapse config not '
                                   'rendered (install-time step; Portal repo mount '
                                   'is read-only)',
                            outcome='failure', error=msg,
                            duration_ms=duration_ms,
                        )
                    except Exception as audit_exc:
                        action.add_line(
                            f'Warning: audit log write failed (non-fatal): {audit_exc!r}'
                        )

                    action.finish(success=False, error=msg)
                    return

            # #2471: a data-bound secret (it ENCRYPTS the module's stored data,
            # core/config/profiles.yaml `data_bound_secrets:`) that is EMPTY while
            # the module's database already holds data must never be generated —
            # provision_profile below would mint a new key and start the app over
            # data it can no longer read (a demo box: Infisical, 2026-09-24). Refuse
            # BEFORE mutating .env, like the two gates above.
            if enable and self.config_manager is not None:
                from razzfazz_common.data_bound_secrets import (
                    check_generation, docker_volume_lister, env_getter)
                env_now = self.config_manager.read_env()
                pg_user = env_now.get('POSTGRES_USER') or 'docker'
                pg_pw = env_now.get('POSTGRES_PASSWORD', '')
                try:
                    # run_ls: F8's volume form (OpenBao's key volume, A18).
                    refusal = check_generation(
                        profile_id, profile, env_getter(env_now),
                        lambda db, q: self._psql_run(pg_user, pg_pw, db, q),
                        run_ls=docker_volume_lister())
                    refusal_msg = refusal.message() if refusal else None
                except ValueError as e:
                    refusal_msg = (f"Refusing to enable '{profile_id}': its "
                                   f"data_bound_secrets declaration is malformed ({e}).")
                if refusal_msg:
                    action.add_line(f'Error: {refusal_msg}')
                    duration_ms = int((time.time() - action.start_time) * 1000)
                    try:
                        self.audit_logger.log(
                            'profile.enable',
                            user=user, source_ip=source_ip,
                            category='profile', action='enable', target=profile_id,
                            detail=f'Enable of {profile.get("name", profile_id)} blocked — '
                                   f'a data-bound secret is empty over existing data',
                            outcome='failure', error=refusal_msg,
                            duration_ms=duration_ms,
                        )
                    except Exception as audit_exc:
                        action.add_line(
                            f'Warning: audit log write failed (non-fatal): {audit_exc!r}')
                    action.finish(success=False, error=refusal_msg)
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

            if action.cancel_requested:
                self._finish_toggle_cancelled(
                    action, profile_id, profile, enable, user, source_ip,
                    'Cancelled before anything was changed.')
                return
            action.set_step('Writing the configuration', 2, total, cancellable=True,
                            note=self.TOGGLE_CANCEL_NOTES['config'])

            # Step 1: Update .env
            action.add_line(f'Updating COMPOSE_PROFILES in .env...')
            enabled = self._read_compose_profiles()
            profiles_before = set(enabled)

            if enable:
                enabled.add(profile_id)
                # Check mutual exclusions and remove conflicting profiles
                for excl in profile.get('mutual_exclusion', []):
                    if excl in enabled:
                        enabled.discard(excl)
                        action.add_line(f'  Removing conflicting profile: {excl}')
            else:
                enabled.discard(profile_id)

            enabled = self._write_compose_profiles(enabled, action)
            action.add_line(f'  COMPOSE_PROFILES={",".join(sorted(enabled))}')

            # Step 1b: When enabling, mirror razzfazz-init.sh's per-profile
            # secret generation so the new profile's containers don't crash
            # on empty SSO secrets, etc. Idempotent — re-runs are no-ops.
            # (Public hostnames internal consumers need to resolve are
            # served via Caddy network aliases; no extra_hosts/CADDY_IP
            # plumbing required — see core/compose.yml.)
            minted_keys = set()
            if enable and self.config_manager is not None:
                try:
                    from app.services.profile_provisioner import provision_profile
                    # #1899: read `.env` on both sides and diff. The provisioner
                    # reports what it did in prose; what matters here is which
                    # VALUES actually changed, and measuring that costs one file
                    # read. A caller that trusts the report measures the report.
                    before = self.config_manager.read_env()
                    # #2015: .env.dify is written through its own accessor, so
                    # a diff of .env alone would never see Dify's OTEL keys move.
                    try:
                        before_dify = self.config_manager.read_dify_env()
                    except Exception:
                        before_dify = None
                    provision_profile(profile_id, self.config_manager, action,
                                      mint_service_key=self._mint_llm_manager_service_key)
                    after = self.config_manager.read_env()
                    minted_keys = {k for k, v in after.items()
                                   if before.get(k) != v}
                    if before_dify is not None:
                        try:
                            after_dify = self.config_manager.read_dify_env()
                            minted_keys |= {f'.env.dify:{k}' for k, v in after_dify.items()
                                            if before_dify.get(k) != v}
                        except Exception:
                            pass
                except Exception as e:
                    action.add_line(f'Provisioning warning: {e} (non-fatal)')

            if action.cancel_requested:
                enabled = set(profiles_before)
                enabled = self._write_compose_profiles(enabled, action)
                self._finish_toggle_cancelled(
                    action, profile_id, profile, enable, user, source_ip,
                    'Cancelled: the module list in .env is back as it was; no container was '
                    'started or stopped.')
                return

            # Step 2: Run docker compose
            # #1973: the second repair GPUStack needs when enabled later — the
            # #536 non-root preparation that cli/upgrade.sh Step 7c only runs
            # when the profile is ALREADY enabled at upgrade time. Measured on
            # 0.79: without it the worker dies on PermissionError while the
            # container reports healthy. The preparation measures the effect
            # (volume ownership after the chown, image label) and, on any
            # doubt, keeps gpustack on root explicitly rather than hanging it.
            if enable and profile_id == 'llm-legacy' and self.config_manager is not None:
                try:
                    from app.services import gpustack_nonroot
                    _upd, _lines = gpustack_nonroot.prepare_nonroot(
                        self.stack_root, self.config_manager.read_env())
                except Exception as _e:
                    _upd = {'GPUSTACK_UID': '0', 'GPUSTACK_GID': '0', 'GPUSTACK_NONROOT_DEFERRED': '1'}
                    _lines = [f'  #536: the non-root preparation itself failed ({_e!r}) — keeping gpustack on ROOT '
                              'rather than starting it de-rooted on an unprepared box (#1973).']
                for _k, _v in _upd.items():
                    self.config_manager.update_env_var(_k, _v)
                for _l in _lines:
                    action.add_line(_l)
            # #2015: the observability consumers read their OTLP endpoint at
            # container CREATE time, so the .env writes above are inert until they
            # are recreated. post-install recreates them; the toggle only NAMED them
            # (CFG-11, "this module has no docker handle") - this method has one.
            # Same set, same recreate, so the two paths produce the same box.
            if enable and profile_id == 'observability' and minted_keys:
                try:
                    from app.services import profile_provisioner as _pp
                    _env_now = self.config_manager.read_env()
                    _targets = _pp.observability_recreate_targets(
                        _env_now,
                        dify_changed=any(k.startswith('.env.dify:') for k in minted_keys),
                        owui_changed='OPENLIT_OTLP_ENDPOINT' in minted_keys,
                        agents_changed='OBSERVABILITY_OTEL_AGENTS_ENDPOINT' in minted_keys,
                        manager_changed='LLM_MANAGER_OTEL_ENDPOINT' in minted_keys)
                except Exception as _e:
                    _targets = []
                    action.add_line(f'  Warning: could not work out which consumers to recreate ({_e!r}) - '
                                    'recreate them by hand as the NOTE above says (#2015).')
                if _targets:
                    action.add_line(f'  Recreating the consumers that read the OTLP env at create time: {", ".join(_targets)} (#2015)')
                    try:
                        _rc = subprocess.run(self._compose('up', '-d', '--no-deps', '--force-recreate', *_targets),
                                             capture_output=True, text=True, timeout=600,
                                             cwd=self.stack_root, env=self._clean_env())
                        if _rc.returncode != 0:
                            action.add_line(f'  Warning: recreate returned {_rc.returncode} - spans will not flow until '
                                            f'{", ".join(_targets)} are recreated by hand: {(_rc.stderr or "")[-300:]}')
                    except Exception as _e:
                        action.add_line(f'  Warning: recreate failed ({_e!r}) - spans will not flow until '
                                        f'{", ".join(_targets)} are recreated by hand (#2015).')
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
                # #1878: BEFORE the containers, not after.
                #
                # ga.6 added this call because `--no-deps` (below) cut the
                # depends_on chain that used to drag postgres-db-reconcile in.
                # It ran AFTER the module's `up`, and only when that `up` exited
                # 0 — so on a box enabling a profile whose per-service role does
                # not exist yet, the module started first, failed DB auth
                # ("password authentication failed for user openuem_user",
                # SQLSTATE 28P01), and the role was created afterwards, or not at
                # all if the failing `up` took the exit code with it. Measured on
                # 0.91 enabling OpenUEM from the portal.
                #
                # A role the module needs at startup has to exist BEFORE startup.
                # `depends_on: service_completed_successfully` does not save us
                # either: the one-shot already completed at install, so compose
                # considers the condition met and never re-runs it — which is why
                # the recovery on the box needed --force-recreate.
                #
                # Still --no-deps (attach to the RUNNING postgres, never recreate
                # it) and still non-fatal: a box with no postgres profile at all
                # has nothing to reconcile, and that must not block a toggle.
                self._reconcile_service_databases(action)
                # #3077: before the agent manager, so it finds them running.
                if profile_id == 'agents':
                    self._start_remembered_agents(action)
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
                cmd = self._compose('up', '-d', '--no-deps', '--force-recreate', '--remove-orphans') + services
            else:
                action.add_line(f'Stopping containers: {", ".join(containers)}...')
                cmd = self._compose('stop') + services

            if enable:
                action.set_step('Starting containers', 3, total, cancellable=True,
                                note=self.TOGGLE_CANCEL_NOTES['start'])
            else:
                action.set_step('Stopping containers', 3, total, cancellable=False,
                                note=self.TOGGLE_CANCEL_NOTES['stop'])
            exit_code, outcome, unhealthy = self._run_watched(action, cmd)
            crashed = []
            if enable and outcome == 'done' and exit_code == 0:
                # compose waits for health only where a healthcheck and a
                # dependency ask for it: a container without a healthcheck
                # that crash-loops is "Started" all the same (#2993).
                outcome, crashed = self._watch_started(action, profile_id, services)

            if outcome in ('cancelled', 'timeout', 'crashed'):
                # The start was cut off: its containers are in no defined
                # state. Back to the state before the toggle (#2993).
                self._roll_back_enable(action, profile_id, profiles_before, services)
                name = profile.get('name', profile_id)
                if outcome == 'cancelled':
                    self._finish_toggle_cancelled(
                        action, profile_id, profile, enable, user, source_ip,
                        f'Cancelled: the start was stopped and {name} is switched off again, '
                        'as before the change.')
                    return
                if outcome == 'crashed':
                    raise RuntimeError(
                        f'{"; ".join(why for _, why in crashed)} right after the start, so {name} was '
                        f'switched off again. See why in its log: the Logs page of this portal, or '
                        f'`docker logs {crashed[0][0]}` on the host.')
                who = ', '.join(unhealthy) or 'a container'
                raise RuntimeError(
                    f'{who} did not become healthy within {int(self.HEALTH_WAIT_TIMEOUT_S)} s, so the '
                    f'start was stopped and {name} was switched off again. See why in its log: the '
                    f'Logs page of this portal, or `docker logs {unhealthy[0] if unhealthy else "<container>"}` '
                    'on the host.')

            if exit_code == 0:
                action.set_step('Securing and registering the module' if enable
                                else 'Updating the proxy and sign-in', 4, total,
                                cancellable=False,
                                note=self.TOGGLE_CANCEL_NOTES['finish' if enable else 'finish_disable'])
                # #2681: the module's post-start hook (module_hooks.POST_START_HOOKS),
                # the SAME one `rzfz module enable` runs — matrix's synapse-data
                # chown to 991 among them. A failed step fails the enable
                # (PostEnableFailed → the failure path below), never a warning.
                if enable:
                    self._run_post_start_hooks(profile_id, action)
                # "Done." is the LAST line of a successful toggle (#2681): it
                # used to be printed here, before the binding check that can
                # still fail the enable.

                # #1899: after the module is up (so the route has an upstream),
                # before the follow-ups. Only fires when Caddy's environment is
                # provably stale — see _recreate_caddy_for_new_secrets.
                _caddy_done = None
                if enable and minted_keys:
                    _caddy_done = self._recreate_caddy_for_new_secrets(action, minted_keys)

                # #2955: Caddy's entrypoint decides at CREATE time which optional
                # site names are live, from the COMPOSE_PROFILES it was created
                # with. Enabling OR disabling such a module changes that answer,
                # so caddy is recreated (once, even when the secrets recreate
                # above already did it - that one reads the same .env).
                if _caddy_done is None:
                    self._recreate_caddy_for_gated_profiles(
                        action, profiles_before ^ set(enabled))

                # #1285: a disable is only done when the containers are actually
                # down — autoheal can restart one that was unhealthy when it was
                # stopped, leaving a disabled module running. `services` (the
                # compose-authority list, #538) — NOT `containers`: profiles.yaml
                # container names are not compose service names, and
                # _running_services answers in service names, so the container
                # list would intersect to nothing and verify everything green.
                stop_verification = None
                if not enable:
                    stop_verification = self._verify_stopped(
                        action, services, profile_id)
                    # #3077: the module's services (the agent manager among
                    # them) are down now; the per-user agents follow.
                    if profile_id == 'agents':
                        self._stop_agent_containers(action)

                # Fix #36: re-run Authentik init so launch URLs get re-rendered and any
                # new providers attach to the embedded outpost (and conversely so any
                # disabled-profile providers get their attachments cleaned up). We
                # invoke the `authentik-init` compose service via `up -d
                # --force-recreate` — it's a one-shot init container that exits when
                # the script finishes, so this is bounded by the script's own runtime.
                # #3388: never start the init against a worker that is still
                # starting (the previous toggle restarted it), and remember
                # which init container ran before, so a failed recreate is not
                # answered by that container's old exit code.
                self._wait_authentik_runtime(action, 'before authentik-init')
                init_before = self._container_id('authentik-init')
                action.add_line('Re-running Authentik init (launch URLs + outpost attachments)...')
                ai_proc = subprocess.Popen(
                    self._compose('up', '-d', '--no-deps', '--force-recreate', 'authentik-init'),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=self.stack_root,
                    env=self._clean_env(),
                )
                for line in iter(ai_proc.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        action.add_line(line)
                ai_code = ai_proc.wait()
                if ai_code != 0:
                    action.add_line(f'authentik-init recreate exited {ai_code} (non-fatal)')

                # #3388: wait for the init on EVERY toggle before the worker
                # restart below. Only an enable of a module with gated apps
                # used to wait (#2681, in _ensure_module_apps_bound); a disable,
                # or an enable of a module without one (searxng), restarted the
                # worker while the init was still copying scripts into it and
                # exec'ing there: "Failed to execute binding script", init
                # exit 1 and blueprints left in error after a searxng
                # disable/enable round-trip.
                init_now = self._container_id('authentik-init')
                if init_now and init_now == init_before:
                    init_ref = None                  # the recreate left the old container
                elif init_now:
                    init_ref = init_now
                elif ai_code != 0:
                    init_ref = None                  # recreate failed, no container to wait on
                else:
                    init_ref = 'authentik-init'      # ids unreadable: wait by name
                init_code = self._wait_for_authentik_init(profile_id, action, init_ref)

                # #2681: the enable is not done while one of the module's
                # Authentik applications has no policy binding — it is open to
                # every signed-in user (synapse, measured). Wait for the init's
                # binding pass, then check; an unbound app is closed to Super
                # Admins and the enable FAILS. Before the worker restart below,
                # which would otherwise cut the init's `docker exec` short.
                if enable:
                    self._ensure_module_apps_bound(profile_id, action)

                # Restart authentik-worker so it picks up the refreshed configuration.
                # #3388: NOT while the init still runs (the wait timed out): the
                # restart would cut its `docker exec` into the worker short.
                if init_code is None:
                    action.add_line('authentik-worker NOT restarted: authentik-init is still running.')
                else:
                    action.add_line('Restarting authentik-worker...')
                    aw_proc = subprocess.Popen(
                        self._compose('restart', 'authentik-worker'),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        cwd=self.stack_root,
                        env=self._clean_env(),
                    )
                    for line in iter(aw_proc.stdout.readline, ''):
                        line = line.rstrip()
                        if line:
                            action.add_line(line)
                    aw_code = aw_proc.wait()
                    if aw_code != 0:
                        action.add_line(f'authentik-worker restart exited {aw_code} (non-fatal)')
                    # #3388: the toggle is not done while the worker is still
                    # starting — the next toggle's init would exec into it.
                    self._wait_authentik_runtime(action, 'after the worker restart')

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
                    self._compose('up', '-d', '--no-deps', '--force-recreate',
                     'razzfazz-start-portal'),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=self.stack_root,
                    env=self._clean_env(),
                )
                for line in iter(sp_proc.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        action.add_line(line)
                sp_code = sp_proc.wait()
                if sp_code != 0:
                    action.add_line(f'razzfazz-start-portal recreate exited {sp_code} (non-fatal)')

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
                        _sr = _Path(self.stack_root)
                        targets = _ptp.resolve_probe_targets(
                            profile, stack_root=_sr,
                        )
                        # #1223 (operator decision E4): the Portal does NOT
                        # run the probe. The repo is mounted :ro here
                        # (BSB-03), so `cli/test.sh` died at its first mkdir
                        # and every hardened box reported SKIPPED — the probe
                        # existed but never ran where it mattered. The Portal
                        # QUEUES the request instead; the host runs it with
                        # `rzfz probe --pending` (cli/probe.sh), which is also
                        # where the readiness wait now happens — waiting 90 s
                        # here would only delay the toggle for a run that
                        # takes place later anyway.
                        probe_summary = _ptp.enqueue_probe_request(
                            profile_id, targets,
                            stack_root=_sr, action=action,
                            # #1223 review, finding 2: the host runner has to
                            # wait for THESE containers before it measures.
                            services=[c.get('name') for c in
                                      (profile.get('containers') or [])],
                        )
                        if probe_summary.get('queued'):
                            action.add_line(
                                f'  post-toggle probe: QUEUED for the host '
                                f'(request {probe_summary.get("request_id")}) — '
                                f'the toggle itself is done; run '
                                f'`rzfz probe --pending` on the box to execute '
                                f'the acceptance probe (#1223)'
                            )
                        elif probe_summary.get('overall_pass') is False:
                            # No results file: the container mounts the repo
                            # read-only, so the runner tail streamed above
                            # and the audit entry ARE the record (#1190).
                            action.add_line(
                                f'  Note: {profile_id} is enabled and its '
                                f'containers are up. The post-enable acceptance '
                                f'smoke did not pass on this run — the module may '
                                f'still be warming up (this does NOT affect the '
                                f'enable). The probe output above shows why; the '
                                f'outcome is also recorded in the audit log.'
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

                # #2681: the operator's view (and /api/apply/<id>) shows the
                # TAIL of the output; the hook lines ran minutes earlier, under
                # the init and worker output. Repeat them as one line, then the
                # verdict, so the last lines say what the enable checked.
                if enable and getattr(action, 'hook_lines', None):
                    action.add_line('Post-enable checks: ' + '; '.join(action.hook_lines))
                # #3388: an Authentik step that did not finish is named in the
                # tail too (the toggle itself stands; the remedy is the refresh).
                if getattr(action, 'authentik_warning', None):
                    action.add_line('WARNING: ' + action.authentik_warning)
                action.add_line(f'Done. Profile {"enabled" if enable else "disabled"} successfully.')

                # #743: write the audit entry BEFORE marking the action
                # done — `action.done` is observed by other threads (SSE
                # pollers, apply_profile_toggle callers) without the audit
                # write's own lock, so `done == True` must always imply the
                # audit entry already exists on disk. Compute the duration
                # inline instead of relying on action.finish()'s
                # duration_ms (that runs a few ms later, which is fine —
                # arguably more correct, since it excludes the audit write
                # itself).
                duration_ms = int((time.time() - action.start_time) * 1000)

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
                    duration_ms=duration_ms,
                )
                if probe_summary is not None:
                    from app.services.post_toggle_probe import summarize_for_audit
                    audit_kwargs['post_toggle_probe'] = summarize_for_audit(probe_summary)
                # #1285 rev-B: a PII module that is still reachable after its
                # disable deserves more than one line in a scrolled action
                # stream. `outcome` stays 'success' — the toggle itself did
                # succeed and the profile IS out of COMPOSE_PROFILES — the
                # leftover is a named field next to it, the same shape
                # post_toggle_probe uses for a failed probe.
                if stop_verification is not None:
                    audit_kwargs['stop_verification'] = stop_verification

                # An audit-write failure must NEVER prevent the action from
                # finishing (or the operation hangs forever waiting on
                # `done`) — log it into the SSE stream and move on.
                try:
                    self.audit_logger.log(
                        f'profile.{"enable" if enable else "disable"}',
                        **audit_kwargs,
                    )
                except Exception as audit_exc:
                    action.add_line(
                        f'Warning: audit log write failed (non-fatal): {audit_exc!r}'
                    )

                action.finish(success=True)

                # CFG-30: the governance checksum walks the whole stack tree
                # and is pure bookkeeping (it swallows its own errors), so it
                # runs AFTER finish() — holding the single-apply busy lock and
                # the SSE `done` flag across it made the UI show "applying"
                # and refuse the next apply for the duration of the walk.
                # It must stay after the audit write, which is what `done`
                # promises to imply (#743).
                self._auto_checksum(
                    f'{"Enabled" if enable else "Disabled"} '
                    f'{profile.get("name", profile_id)}')
            else:
                error_msg = f'docker compose exited with code {exit_code}'
                if unhealthy:
                    error_msg = (f'{", ".join(unhealthy)} is unhealthy (docker compose exited with code '
                                 f'{exit_code}). See why in its log: the Logs page of this portal, or '
                                 f'`docker logs {unhealthy[0]}` on the host.')
                if enable:
                    # #2993: a failed start goes back to the state before the
                    # toggle, like a timed-out or cancelled one: the module out
                    # of COMPOSE_PROFILES and the containers it started stopped.
                    self._roll_back_enable(action, profile_id, profiles_before, services)
                    error_msg += f' {profile.get("name", profile_id)} was switched off again.'
                state_note = self._half_applied_note(profile_id, profile, enable, profiles_before)
                if state_note:
                    error_msg += ' ' + state_note
                enable_logger.error('%s %s failed: %s', 'enable' if enable else 'disable', profile_id, error_msg)

                # #743: audit before done — see comment on the success path
                # above for the invariant this establishes.
                duration_ms = int((time.time() - action.start_time) * 1000)
                try:
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
                        duration_ms=duration_ms,
                    )
                except Exception as audit_exc:
                    action.add_line(
                        f'Warning: audit log write failed (non-fatal): {audit_exc!r}'
                    )

                action.add_line(f'Error: {error_msg}')   # #2681: the last visible line
                action.finish(success=False, error=error_msg)

        except Exception as e:
            # #2681: the failure is the LAST visible line — the audit
            # warning (if any) goes before it, not after.
            enable_logger.error('%s %s failed: %s', 'enable' if enable else 'disable', profile_id, e)
            # #3412: a failure after the .env write says what state the box is
            # in and how to finish or undo — never just the raw error.
            error_text = str(e)
            try:
                state_note = self._half_applied_note(profile_id, profile, enable, profiles_before)
            except Exception:  # noqa: BLE001 - the note must never mask the failure
                state_note = None
            if state_note:
                error_text = f'{error_text} {state_note}'

            # #743: audit before done, same invariant as the success and
            # compose-failure paths above — an unhandled exception during
            # a toggle must still leave an audit trail before `done`
            # becomes observable, and a failure to write it must not hang
            # the action.
            duration_ms = int((time.time() - action.start_time) * 1000)
            try:
                self.audit_logger.log(
                    f'profile.{"enable" if enable else "disable"}',
                    user=user,
                    source_ip=source_ip,
                    category='profile',
                    action='enable' if enable else 'disable',
                    target=profile_id,
                    detail=f'Failed to {"enable" if enable else "disable"} '
                           f'{profile.get("name", profile_id)}: {e}',
                    outcome='failure',
                    error=error_text,
                    duration_ms=duration_ms,
                )
            except Exception as audit_exc:
                action.add_line(
                    f'Warning: audit log write failed (non-fatal): {audit_exc!r}'
                )

            action.add_line(f'Error: {error_text}')
            action.finish(success=False, error=error_text)

    # ------------------------------------------------------------- #2993 job

    #: What "Cancel" does at each step of a module toggle; the dialog shows
    #: the note of the running step next to the button.
    TOGGLE_CANCEL_NOTES = {
        'check': 'Nothing has been changed yet: cancelling stops here.',
        'config': 'Cancelling puts the module list back as it was; no container is started '
                  'or stopped.',
        'start': 'Cancelling stops the start, switches the module off again and stops its '
                 'containers, so the box is as before the change. Settings the enable '
                 'generated for the module stay and are used the next time it is enabled.',
        'stop': 'A disable cannot be cancelled once its containers are stopping: a half-stopped '
                'module is not a state to leave. It finishes on its own.',
        'finish': 'The containers are up; these steps secure and register the module and '
                  'cannot be skipped. To undo the change, toggle the module again when this '
                  'finishes.',
        'finish_disable': 'The containers are stopped; these steps update the proxy and the '
                          'sign-in and cannot be skipped. To undo the change, toggle the module '
                          'again when this finishes.',
    }

    #: How long an enable watches the containers it started before it calls
    #: the start good (#2993). Measured on a box: a module that exits 3 s
    #: after its start had restarted 8 times within 20 s.
    START_SETTLE_S = 15.0
    #: How often the settle watch looks at the containers.
    START_SETTLE_POLL_S = 1.5

    def _container_states(self, profile_id, services):
        """{container name: state} for the module's containers, from `docker
        inspect`; None when they could not be read."""
        try:
            r = subprocess.run(self._compose('--profile', profile_id, 'ps', '-a', '-q', *services),
                               capture_output=True, text=True, cwd=self.stack_root,
                               env=self._clean_env(), timeout=30)
            if r.returncode != 0:
                return None
            ids = r.stdout.split()
            if not ids:
                return {}
            r = subprocess.run(['docker', 'inspect', '--type', 'container', *ids],
                               capture_output=True, text=True, env=self._clean_env(), timeout=30)
            if r.returncode != 0:
                return None
            out = {}
            for d in json.loads(r.stdout or '[]'):
                st = d.get('State') or {}
                out[(d.get('Name') or '?').lstrip('/')] = {
                    'status': st.get('Status', ''),
                    'restarting': bool(st.get('Restarting')) or st.get('Status') == 'restarting',
                    'exit_code': st.get('ExitCode', 0),
                    'health': (st.get('Health') or {}).get('Status', ''),
                    'restarts': int(d.get('RestartCount') or 0),
                }
            return out
        except Exception:  # noqa: BLE001 - "could not read" is reported by the caller
            return None

    @staticmethod
    def _crash_reason(name, now, first):
        """Why `name` counts as failed right after its start, or None."""
        if now['restarting']:
            return f'{name} keeps restarting'
        if first is not None and now['restarts'] > first['restarts']:
            return f'{name} restarted {now["restarts"] - first["restarts"]} times'
        # The exit state first: a one-shot (the *-image-builder services) that
        # exits 0 is done, although docker still reports the HEALTHCHECK it
        # inherits from its image as "unhealthy" on the exited container. A
        # restarting policy that brings an exit-0 container back shows up as
        # a restart above. Health counts for running containers only.
        if now['status'] in ('exited', 'dead'):
            return f'{name} exited with code {now["exit_code"]}' if now['exit_code'] != 0 else None
        if now['status'] == 'running' and now['health'] == 'unhealthy':
            return f'{name} is unhealthy'
        return None

    def _watch_started(self, action, profile_id, services):
        """After compose reported the start done, watch the module's
        containers for START_SETTLE_S (#2993): a container that restarts,
        keeps restarting, exits non-zero or turns unhealthy fails the start,
        as an unhealthy one does in compose's own wait. Returns (outcome,
        [(name, reason)]) with outcome 'done' | 'crashed' | 'cancelled'."""
        first = self._container_states(profile_id, services)
        if not first:
            if first is None:
                action.add_line('Note: could not read the started containers, so a crash right '
                                'after the start is not checked.')
            return 'done', []
        action.add_line(f'Checking that the started containers stay up ({int(self.START_SETTLE_S)} s)...')
        end = time.time() + self.START_SETTLE_S
        now = first
        while True:
            bad = [(n, why) for n, why in ((n, self._crash_reason(n, s, first.get(n)))
                                           for n, s in sorted(now.items())) if why]
            if bad:
                for _, why in bad:
                    action.add_line(f'{why} - stopping the start.')
                return 'crashed', bad
            if time.time() >= end:
                return 'done', []
            step_end = min(end, time.time() + self.START_SETTLE_POLL_S)
            while time.time() < step_end:
                if action.cancel_requested:
                    action.add_line('Stopping the start...')
                    return 'cancelled', []
                time.sleep(min(self.WATCH_POLL_S, max(0.0, step_end - time.time())))
            if action.cancel_requested:
                action.add_line('Stopping the start...')
                return 'cancelled', []
            now = self._container_states(profile_id, services) or now

    #: How long a start may wait for one container to become healthy before
    #: the toggle stops it and fails, naming the container (#2993). Measured
    #: first starts take up to about five minutes (an indexer warming up).
    HEALTH_WAIT_TIMEOUT_S = 600.0
    #: How often the start checks for a cancel or the deadline.
    WATCH_POLL_S = 0.5

    _COMPOSE_PROGRESS = re.compile(
        r'^\s*Container\s+(\S+)\s+(Waiting|Healthy|Started|Running|Error|Exited|Stopped|Created|Recreated)\b')
    _COMPOSE_UNHEALTHY = re.compile(r'container\s+(\S+)\s+is unhealthy')

    def _run_watched(self, action, cmd):
        """Run a compose command, streaming its lines into the action, and
        watch it (#2993): which container it waits for, a cancel, and the
        health-wait deadline. Returns (exit_code, outcome, unhealthy) with
        outcome 'done' | 'cancelled' | 'timeout'; exit_code is None unless done."""
        import queue
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, cwd=self.stack_root, env=self._clean_env())
        lines = queue.Queue()

        def reader():
            try:
                for ln in iter(proc.stdout.readline, ''):
                    lines.put(ln)
            finally:
                lines.put(None)
        threading.Thread(target=reader, daemon=True).start()
        waiting, unhealthy = {}, []
        while True:
            try:
                ln = lines.get(timeout=self.WATCH_POLL_S)
            except queue.Empty:
                ln = ''
            if ln is None:
                return proc.wait(), 'done', unhealthy
            ln = ln.rstrip()
            if ln:
                action.add_line(ln)
                m = self._COMPOSE_PROGRESS.match(ln)
                if m:
                    before = set(waiting)
                    if m.group(2) == 'Waiting':
                        waiting.setdefault(m.group(1), time.time())
                    else:
                        waiting.pop(m.group(1), None)
                    if set(waiting) != before:
                        action.set_waiting(waiting)
                bad = self._COMPOSE_UNHEALTHY.search(ln)
                if bad and bad.group(1) not in unhealthy:
                    unhealthy.append(bad.group(1))
            if action.cancel_requested:
                action.add_line('Stopping the start...')
                self._stop_process(proc)
                return None, 'cancelled', unhealthy
            now = time.time()
            stuck = sorted(n for n, since in waiting.items() if now - since > self.HEALTH_WAIT_TIMEOUT_S)
            if stuck:
                action.add_line(f'{", ".join(stuck)} is still not healthy after '
                                f'{int(self.HEALTH_WAIT_TIMEOUT_S)} s - stopping the start.')
                self._stop_process(proc)
                return None, 'timeout', stuck

    @staticmethod
    def _stop_process(proc):
        """`docker compose up -d` interrupted leaves the containers it already
        created; the caller stops them. Terminate, then kill."""
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - still running, or already gone
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass

    def _roll_back_enable(self, action, profile_id, profiles_before, services):
        """An enable whose start was cancelled or timed out goes back to the
        state before it: the module list as it was, the module's containers
        stopped. Caddy, Authentik and the Start Portal were not touched yet."""
        action.add_line('Switching the module off again...')
        enabled = set(profiles_before)
        enabled = self._write_compose_profiles(enabled, action)
        tail = ['--profile', profile_id, 'stop'] + list(services)
        try:
            r = subprocess.run(self._compose(*tail), capture_output=True, text=True,
                               cwd=self.stack_root, env=self._clean_env(), timeout=300)
            if r.returncode != 0:
                action.add_line(f'Warning: stopping the containers exited {r.returncode}: '
                                f'{(r.stderr or "")[-300:]} - check `docker ps` for leftovers.')
            else:
                action.add_line(f'Stopped: {", ".join(services)}')
        except Exception as exc:  # noqa: BLE001 - reported, the .env is already back
            action.add_line(f'Warning: stopping the containers failed ({exc}) - check `docker ps` '
                            'for leftovers.')

    def _finish_toggle_cancelled(self, action, profile_id, profile, enable, user, source_ip, msg):
        word = 'enable' if enable else 'disable'
        self._audit_before_done(
            action, f'profile.{word}', user=user, source_ip=source_ip, category='profile',
            action=word, target=profile_id,
            detail=f'{word.capitalize()} of {profile.get("name", profile_id)} cancelled: {msg}',
            outcome='cancelled')
        action.add_line(msg)
        action.finish(success=False, error=msg, cancelled=True)

    def cancel_action(self, action_id):
        """(http_status, body) for POST /api/apply/<id>/cancel (#2993)."""
        action = self.get_action(action_id)
        if action is None:
            return 404, {'error': 'Action not found'}
        cancel = getattr(action, 'request_cancel', None)
        ok, msg = cancel() if cancel else (False, 'This action cannot be cancelled.')
        body = {'action_id': action_id, 'message': msg,
                'description': getattr(action, 'description', '')}
        if hasattr(action, 'status'):
            body.update(action.status())
        if not ok:
            body['error'] = msg
            return 409, body
        return 202, body

    def current_action(self):
        """The running action, or None (#2993: a page opened while one runs)."""
        with self._lock:
            a = self._current_action
        return a if a is not None and not a.done else None

    def _hook_run(self, argv, **kw):
        """The docker calls of module_hooks — through this module's
        `subprocess`, so the Portal's tests and the hooks share one seam."""
        if argv and argv[0] == 'docker':
            kw.setdefault('env', self._clean_env())
        return subprocess.run(argv, **kw)

    def _hook_env(self):
        if self.config_manager is not None:
            try:
                return self.config_manager.read_env()
            except Exception:
                pass
        return {k: self._read_env_value(k) for k in ('COMPOSE_PROJECT_NAME', 'TLS_MODE', 'MAIN_DOMAIN',
                                  'AUTHENTIK_BOOTSTRAP_PASSWORD',
                                  'POSTGRES_USER', 'COGNEE_DB',
                                  'MCP_MANAGER_AUTHENTIK_READONLY_TOKEN')}

    def _hook_set_env(self, key, value):
        """One .env value written by a post-start hook (#3050: mcp-manager's
        read-only token). Through the config manager when there is one (its
        in-place write and stale-bind guard), else in place here."""
        if self.config_manager is not None:
            self.config_manager.update_env_var(key, value)
        else:
            self._update_env_var(key, value)

    @staticmethod
    def _hook_log(profile_id, action):
        """#2681 — one message of the post-start hook / binding check: into the
        action's output where it runs, into the container log, and kept for
        the summary line before "Done." (progress lines ending in "..." are
        not repeated there)."""
        if not hasattr(action, 'hook_lines'):
            action.hook_lines = []

        def log(msg):
            msg = str(msg).strip()
            action.add_line(f'  {msg}')
            enable_logger.info('enable %s: %s', profile_id, msg)
            if not msg.endswith('...'):
                action.hook_lines.append(msg)
        return log

    def _run_post_start_hooks(self, profile_id, action):
        """#2681 — raises module_hooks.PostEnableFailed."""
        from app.services import module_hooks
        module_hooks.run_post_start(
            profile_id, self.stack_root, self._hook_env(), run=self._hook_run,
            compose=lambda *a: self._compose(*a), log=self._hook_log(profile_id, action),
            set_env=self._hook_set_env)

    #: #3388: what _wait_for_authentik_init answers when the init was NOT
    #: recreated (its previous run's container is still the one there).
    INIT_NOT_RECREATED = 'not recreated'

    def _container_id(self, name):
        """The id of container `name`, '' when it cannot be read."""
        try:
            r = self._hook_run(['docker', 'inspect', '-f', '{{.Id}}', name],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ''
        out = (getattr(r, 'stdout', '') or '').strip()
        return out if getattr(r, 'returncode', 1) == 0 and re.fullmatch(r'[0-9a-f]{12,64}', out) else ''

    def _wait_authentik_runtime(self, action, when):
        """#3388: authentik-init copies its scripts into authentik-worker and
        execs there, so it must not start against a worker that is still
        starting — e.g. the one the PREVIOUS toggle restarted. Waits (the #2816
        helper) for the Authentik containers that run; never fails the toggle,
        the init's own exit code says whether its work got done."""
        running = self._running_services() or set()
        targets = [s for s in self._AUTHENTIK_RUNTIME if s in running]
        if not targets:
            return True
        try:
            self._wait_authentik_ready(action, targets)
            return True
        except RuntimeError as e:
            action.add_line(f'  WARNING ({when}): {e}')
            action.authentik_warning = (
                f'Authentik was not healthy {when}; run: rzfz post-install --refresh')
            return False

    def _wait_for_authentik_init(self, profile_id, action, container='authentik-init'):
        """#3388 — block until the authentik-init just recreated has exited, so
        the worker restart that follows cannot cut its `docker exec` into the
        worker short. A non-zero exit is reported with the fix; it fails the
        toggle only where it already did (_ensure_module_apps_bound, an enable
        of a module with gated apps). Returns the exit code; None when the init
        is still running after the wait (then the caller must NOT restart the
        worker), INIT_NOT_RECREATED when `container` is None (the recreate
        failed and the container there is the previous run's)."""
        from app.services import module_hooks
        log = self._hook_log(profile_id, action)
        if container is None:
            code = self.INIT_NOT_RECREATED
            log('WARNING: authentik-init was not recreated, so the Authentik bindings / '
                'blueprints of this change did not run (the container there is the previous '
                "run's; not waited on). Run: rzfz post-install --refresh")
            action.authentik_warning = 'authentik-init was not recreated; run: rzfz post-install --refresh'
        else:
            code = module_hooks.wait_for_authentik_init(run=self._hook_run, log=log,
                                                        container=container)
            if code is None:
                log(f'WARNING: authentik-init is still running after '
                    f'{module_hooks.INIT_WAIT_SECONDS}s; authentik-worker was NOT restarted so '
                    'it is not cut short. Check `docker logs authentik-init`; when it has '
                    'finished, run: rzfz post-install --refresh')
                action.authentik_warning = (
                    'authentik-init still running (timed out waiting for it); authentik-worker '
                    'not restarted; run: rzfz post-install --refresh once it has finished')
            elif code not in ('', '0'):
                log(f'WARNING: authentik-init exited {code}: the Authentik bindings / blueprints '
                    'of this change did not complete (see `docker logs authentik-init`). '
                    'Run: rzfz post-install --refresh')
                action.authentik_warning = (
                    f'authentik-init exited {code}; run: rzfz post-install --refresh')
        action.authentik_init_code = code
        return code

    def _ensure_module_apps_bound(self, profile_id, action):
        """#2681 — raises module_hooks.PostEnableFailed."""
        from app.services import module_hooks
        log = self._hook_log(profile_id, action)
        if not module_hooks.app_slugs(profile_id, self.stack_root):
            return
        if hasattr(action, 'authentik_init_code'):
            # #3388: the toggle already waited for this init (once, bounded).
            code = action.authentik_init_code
        else:
            code = module_hooks.wait_for_authentik_init(run=self._hook_run, log=log)
        if code not in (None, '', '0'):
            # #2816: a run that skipped the binding / outpost reconcile exits non-zero.
            raise module_hooks.PostEnableFailed(
                f'authentik-init {"was " if code == self.INIT_NOT_RECREATED else "exited "}{code}: '
                'the module bindings / outpost reconcile did not '
                'complete (see its log). Run: rzfz post-install --refresh')
        module_hooks.close_unbound_apps(profile_id, self.stack_root, run=self._hook_run, log=log)

    def get_action(self, action_id):
        action = self._actions.get(action_id)
        if action is None and self.state_store is not None:
            # #2794: started by another worker, or before a restart.
            action = self.state_store.load(action_id)
        return action

    def _read_env_value(self, key):
        """Read a single .env value; returns empty string if missing.

        Doesn't source the file (an operator-edited .env carries inline
        comments and shell metacharacters — see feedback_dotenv_no_source).
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

    def _normalise_compose_profiles(self, enabled, action=None):
        """#1232 — the WHOLE set is validated on every write, not just the
        toggle target.

        The toggle read COMPOSE_PROFILES from .env and wrote it back after one
        add/discard with no validation: a profile that no longer exists, or
        one that is retired on this hardware (#946 blocked only an EXPLICIT
        enable of `llm`), rode along unchanged on the next toggle of anything
        and `docker compose up` pulled it up.

        Two different lists, deliberately (verify-first 2026-09-04):
        * unknown -> dropped with a WARN line — profiles.yaml is the authority;
        * retired -> dropped via the #946 predicate (`llm` on NVIDIA), i.e.
          hardware-conditional. NOT lib-owui's `_gpustack_profile_active`
          set and NOT the day-1 autoenable block-list (#1231: llm, llm-cpu,
          llm-legacy, mac-llm): llm-legacy stays THE optional GPUStack backend
          after the cutover (#1448 merged llm-cuda into it), and stripping it
          here would kill the AMD/NVIDIA backend on the next unrelated toggle.
        Returns the cleaned set; every removal is an action line.
        """
        enabled = set(enabled)

        def say(line):
            if action is not None:
                action.add_line(line)

        try:
            known = set(self.profile_manager.get_all_profiles() or {})
        except Exception:
            known = set()
        for pid in sorted(enabled):
            if known and pid not in known:
                enabled.discard(pid)
                say(f"  WARNING: removed '{pid}' from COMPOSE_PROFILES: not a profile in profiles.yaml (#1232)")
                continue
            reason = self._llm_nvidia_block_reason(pid, True)
            if reason:
                enabled.discard(pid)
                say(f"  Removed '{pid}' from COMPOSE_PROFILES: retired on this hardware (#946/#1232) — {reason}")
        return enabled

    def _write_compose_profiles(self, profiles, action=None):
        """Update COMPOSE_PROFILES in .env. Returns the set actually written
        (#1232: normalised — see _normalise_compose_profiles)."""
        profiles = self._normalise_compose_profiles(profiles, action)
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
        # Truncate IN PLACE (same inode) — see config_manager._write_env_file
        # and env_mount.py (#1189).
        with env_write_guard(self.env_path):
            with open(self.env_path, 'w') as f:
                f.writelines(lines)
        return profiles
