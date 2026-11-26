# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3058 — the Settings portal repairs its own `.env` bind.

A single-file bind is resolved when the container is created, so only recreating
the portal repairs it, and a container cannot do that from inside: the command
removes the process running it. So the portal starts a detached one-shot
container (`self_heal_helper.py`, same image, same docker path through the socket
proxy, no new mount of the docker socket) that runs
`docker compose up -d --force-recreate --no-deps razzfazz-config`. That container
sees `.env` through the stack directory's bind, i.e. the host's CURRENT file.

The state lives on the audit volume, the one place of the old portal that the new
one still sees:

    (none) -> requested -> healed            bind sound in the successor
                        -> failed            helper failed, timed out, or the bind
                                             is still broken after the recreate

`status()` (what `/api/self-heal` answers) reports a recorded `healed` (or no
record) as `blocked` (inside the loop guard) or `stale` when the bind is broken
again right now, so a client polling only the API sees what the banner shows.

The successor moves it on at boot (`on_boot`). It tells itself from its
predecessor by the container id. A module toggle resumes on its own (COMPOSE_PROFILES
toggles are idempotent); everything else is offered again, and its values are
never stored here: they stay in the admin's browser.

Loop guard: at most one attempt per MIN_INTERVAL, recorded BEFORE the helper
starts. A state that cannot be written means no attempt, because the guard could
not hold across the recreate.
"""
import fcntl
import json
import os
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager

from .compose_allowlist import ComposeFileRejected, StackHostPathMissing, compose_argv, compose_env
from . import env_mount
from .self_heal_helper import RESULT_NAME

MIN_INTERVAL = 15 * 60
HELPER_GRACE = 2.0
HELPER_COMMAND_TIMEOUT = 150
#: The old portal gives up on a helper that neither finished nor replaced it.
HELPER_DEADLINE = 200
#: A resume or an offer older than this no longer reflects what the admin wanted.
PENDING_TTL = 30 * 60

HELPER_NAME = 'razzfazz-config-self-heal'
HELPER_SCRIPT = '/app/app/services/self_heal_helper.py'
STATE_NAME = 'state.json'

RECONNECTING = 'Settings portal is reconnecting to its configuration …'
NOT_APPLIED = 'The change was not applied yet — apply again.'


def _env_path(stack_root):
    return os.path.join(stack_root, '.env')


class PortalSelfHeal:
    def __init__(self, stack_root, state_dir, audit_logger=None, apply_manager=None,
                 run=subprocess.run, clock=time.time, container_id=socket.gethostname):
        self.stack_root = stack_root
        self.state_dir = state_dir
        self.audit_logger = audit_logger
        self.apply_manager = apply_manager
        self.run = run
        self.clock = clock
        self.container_id = container_id
        self.min_interval = MIN_INTERVAL
        self.grace = HELPER_GRACE
        self.deadline = HELPER_DEADLINE

    # ── state ────────────────────────────────────────────────────────────────
    def _state_path(self):
        return os.path.join(self.state_dir, STATE_NAME)

    def _read(self):
        try:
            with open(self._state_path(), encoding='utf-8') as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, state):
        os.makedirs(self.state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix='.tmp-state-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(state, fh)
            os.replace(tmp, self._state_path())
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @contextmanager
    def _locked(self):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, '.lock'), 'a', encoding='utf-8') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield

    def _helper_result(self, heal_id):
        try:
            with open(os.path.join(self.state_dir, RESULT_NAME), encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get('heal_id') == heal_id else None

    def _audit(self, outcome, state, detail, error=None, action='self_heal'):
        if self.audit_logger is None:
            return
        try:
            self.audit_logger.log(
                'portal.self_heal', user=state.get('user'), source_ip=state.get('source_ip'),
                category='settings', action=action, target=env_mount.PORTAL_CONTAINER,
                detail=detail, outcome=outcome, error=error)
        except Exception:  # noqa: BLE001 - the audit must not change the repair's outcome
            pass

    # ── messages ─────────────────────────────────────────────────────────────
    def fallback_message(self):
        """The host command, with the real path: what is left when healing is not."""
        return env_mount.explain_stale(_env_path(self.stack_root), os.strerror(30))

    def _refusal(self, state, lead):
        return {'state': state, 'message': f'{lead} {NOT_APPLIED} {self.fallback_message()}',
                'fallback': self.fallback_message()}

    # ── start ────────────────────────────────────────────────────────────────
    def _apply_running(self):
        manager = self.apply_manager
        action = getattr(manager, '_current_action', None)
        return action is not None and not action.done

    def _inspect_self(self):
        """What the helper must share with this container; no environment, so no
        secret passes through this process's memory or an error message."""
        fmt = ('{"image":{{json .Image}},"labels":{{json .Config.Labels}},'
               '"networks":{{json .NetworkSettings.Networks}},"mounts":{{json .Mounts}}}')
        r = self.run(['docker', 'inspect', '--format', fmt, env_mount.PORTAL_CONTAINER],
                     capture_output=True, text=True, timeout=20, env=compose_env(self.stack_root))
        if r.returncode != 0:
            raise RuntimeError(f'docker inspect exited {r.returncode}: {(r.stderr or "").strip()[:300]}')
        return json.loads(r.stdout)

    def _helper_argv(self, heal_id):
        """`docker run` of the one-shot. Raises when it cannot be built safely."""
        me = self._inspect_self()
        networks = [n for n in (me.get('networks') or {}) if n.endswith('docker-ctl')]
        if not networks or not me.get('image'):
            raise RuntimeError('the portal container has no socket-proxy network or image id')
        labels = me.get('labels') or {}
        project = labels.get('com.docker.compose.project')
        compose_args = (['-p', project] if project else []) + [
            'up', '-d', '--force-recreate', '--no-deps', env_mount.PORTAL_CONTAINER]
        # Through the chokepoint: the COMPOSE_FILE allow-list and the host-path
        # precondition hold here like for every other compose run of the portal.
        command = compose_argv(self.stack_root, *compose_args)
        volume = next((m for m in me.get('mounts') or []
                       if m.get('Type') == 'volume' and m.get('Name')
                       and (self.state_dir + '/').startswith(str(m.get('Destination', '\0')).rstrip('/') + '/')),
                      None)
        argv = ['docker', 'run', '-d', '--rm', '--name', HELPER_NAME, '--pull', 'never',
                '--network', networks[0], '--security-opt', 'no-new-privileges:true',
                '--memory', '512m', '--label', 'com.razzfazz.role=portal-self-heal',
                '--workdir', self.stack_root,
                '-v', f'{self.stack_root}:{self.stack_root}:ro']
        if volume:
            argv += ['-v', f'{volume["Name"]}:{volume["Destination"]}']
        for key, value in sorted(compose_env(self.stack_root).items()):
            if key.startswith('DOCKER_') or key in ('STACK_HOST_PATH', 'COMPOSE_PROJECT_NAME', 'TZ'):
                argv += ['-e', f'{key}={value}']
        argv += [me['image'], 'python', '-I', HELPER_SCRIPT, '--state-dir', self.state_dir,
                 '--heal-id', heal_id, '--grace', str(self.grace),
                 '--timeout', str(HELPER_COMMAND_TIMEOUT), '--', *command]
        return argv

    def begin(self, trigger, user, source_ip, pending=None):
        """Start one repair for the write that just found the bind stale.

        Returns `{state, message, fallback, ...}` with state `reconnecting`,
        `busy`, `blocked` or `failed`. Only `reconnecting` started anything."""
        try:
            with self._locked():
                return self._begin_locked(trigger, user, source_ip, pending)
        except OSError as exc:
            return self._refusal('failed', f'The Settings portal cannot record a repair ({exc.strerror or exc}).')

    def _begin_locked(self, trigger, user, source_ip, pending):
        now = self.clock()
        state = self._read()
        who = {'user': user, 'source_ip': source_ip}

        if self._apply_running():
            # Recreating now would kill the running apply. Nothing is spent.
            out = self._refusal('busy', 'Another change is still being applied.')
            out['message'] = (f'Another change is still being applied. {NOT_APPLIED} '
                              'When it has finished, the Settings portal reconnects to its configuration.')
            return out

        if state.get('status') == 'requested' and now - float(state.get('requested_at') or 0) < self.deadline:
            # One repair is running; this change did not start it and is offered
            # again, it cannot be queued behind a state only one change fits in.
            return {'state': 'reconnecting', 'message': RECONNECTING, 'heal_id': state.get('heal_id'),
                    'fallback': self.fallback_message(), 'resume_expected': False}

        last = float(state.get('requested_at') or 0)
        if last and 0 <= now - last < self.min_interval:
            self._audit('refused', who, f'Self-heal refused, the last attempt was '
                                        f'{int((now - last) // 60)} min ago; triggered by: {trigger}')
            return self._refusal(
                'blocked', 'The Settings portal already tried to reconnect a moment ago and will not retry yet.')

        heal_id = uuid.uuid4().hex[:12]
        new_state = {'status': 'requested', 'heal_id': heal_id, 'requested_at': now,
                     'trigger': trigger, 'user': user, 'source_ip': source_ip,
                     'container_id': self.container_id(), 'pending': pending}
        try:
            argv = self._helper_argv(heal_id)
        except (ComposeFileRejected, StackHostPathMissing, RuntimeError, OSError, ValueError,
                subprocess.SubprocessError) as exc:
            return self._launch_failed(new_state, str(exc))

        self._write(new_state)
        self._audit('started', new_state, f'Self-heal started; triggered by: {trigger}')
        try:
            r = self.run(argv, capture_output=True, text=True, timeout=30, env=compose_env(self.stack_root))
            if r.returncode != 0:
                return self._launch_failed(new_state, (r.stderr or '').strip()[:300] or f'exit {r.returncode}')
        except (OSError, subprocess.SubprocessError) as exc:
            return self._launch_failed(new_state, str(exc))
        return {'state': 'reconnecting', 'message': RECONNECTING, 'heal_id': heal_id,
                'fallback': self.fallback_message(),
                'resume_expected': bool(pending and pending.get('kind') == 'profile_toggle')}

    def _launch_failed(self, state, why):
        state = {**state, 'status': 'failed', 'error': why, 'failed_at': self.clock()}
        try:
            self._write(state)
        except OSError:
            pass
        self._audit('failure', state, f'Self-heal could not start; triggered by: {state.get("trigger")}', error=why)
        return self._refusal('failed', 'The Settings portal could not reconnect by itself.')

    # ── after the recreate ───────────────────────────────────────────────────
    def on_boot(self):
        """Runs once in every new portal process. Does nothing unless this process
        is the successor of a container that asked for a repair."""
        with self._locked():
            state = self._read()
            if state.get('status') != 'requested' or state.get('container_id') == self.container_id():
                return state
            report = env_mount.inspect_env_mounts(self.stack_root)
            if report['stale']:
                state.update(status='failed', failed_at=self.clock(),
                             error='the bind is still broken after the recreate')
                self._write(state)
                self._audit('failure', state, f'Self-heal did not repair the bind; triggered by: {state.get("trigger")}',
                            error=state['error'])
                return state
            # Written before the resume: a crash in between must offer the
            # change again rather than apply it twice.
            state.update(status='healed', healed_at=self.clock())
            self._write(state)
            self._audit('success', state, f'Self-heal repaired the bind; triggered by: {state.get("trigger")}')
            return self._resume_locked(state)

    def _resume_locked(self, state):
        pending = state.get('pending') or {}
        age = self.clock() - float(state.get('requested_at') or 0)
        if pending.get('kind') != 'profile_toggle' or self.apply_manager is None or age > PENDING_TTL:
            return state
        action_id, error = self.apply_manager.apply_profile_toggle(
            pending.get('profile_id'), bool(pending.get('enable')), state.get('user'), state.get('source_ip'))
        if error:
            state['resume_error'] = str(error)
            self._audit('failure', state, f'The interrupted change was not resumed: {pending.get("description")}',
                        error=str(error), action='self_heal_resume')
        else:
            state['resumed_action_id'] = action_id
            self._audit('success', state, f'The interrupted change was resumed: {pending.get("description")}',
                        action='self_heal_resume')
        self._write(state)
        return state

    def status(self):
        """What the reconnecting page polls. `requested` that has a verdict moves on here."""
        try:
            with self._locked():
                state = self._read()
                if state.get('status') == 'requested':
                    # A failed exit code says something only while the portal that
                    # asked is still here; a successor decides at boot.
                    same = state.get('container_id') == self.container_id()
                    result = (self._helper_result(state.get('heal_id')) or {}) if same else {}
                    code = result.get('exit_code')
                    late = self.clock() - float(state.get('requested_at') or 0) > self.deadline
                    if code not in (None, 0) or late:
                        why = (f'the recreate exited {code}: {str(result.get("output", ""))[-300:]}'
                               if code not in (None, 0) else 'the recreate did not finish in time')
                        state.update(status='failed', failed_at=self.clock(), error=why)
                        self._write(state)
                        self._audit('failure', state, f'Self-heal failed; triggered by: {state.get("trigger")}', error=why)
        except OSError:
            state = self._read()
        out = self._public(state)
        if out['status'] in ('none', 'healed'):
            # The record describes the LAST repair. A bind that broke again
            # since then is today's answer, as the banner already shows it.
            out.update(self._stale_now(state) or {})
        return out

    def _stale_now(self, state):
        """`blocked` or `stale` when the bind is broken right now, else None."""
        try:
            if not env_mount.inspect_env_mounts(self.stack_root)['stale']:
                return None
        except Exception:  # noqa: BLE001 - a failing probe must not break the poll
            return None
        last = float(state.get('requested_at') or 0)
        if last and 0 <= self.clock() - last < self.min_interval:
            refusal = self._refusal(
                'blocked', 'The Settings portal already tried to reconnect a moment ago and will not retry yet.')
            return {'status': 'blocked', 'message': refusal['message'], 'fallback': refusal['fallback']}
        return {'status': 'stale', 'fallback': self.fallback_message(),
                'message': ('The Settings portal has lost its connection to its configuration and '
                            f'reconnects on the next change. {self.fallback_message()}')}

    def _public(self, state):
        status = state.get('status') or 'none'
        out = {'status': status, 'heal_id': state.get('heal_id'),
               'resumed_action_id': state.get('resumed_action_id'),
               'pending': self._public_pending(state)}
        if status == 'requested':
            out['message'] = RECONNECTING
        elif status == 'healed':
            out['message'] = ('The Settings portal is reconnected.' if state.get('resumed_action_id')
                              else f'The Settings portal is reconnected. {NOT_APPLIED}')
        elif status == 'failed':
            out['message'] = (f'The Settings portal could not reconnect by itself. {NOT_APPLIED} '
                              f'{self.fallback_message()}')
        return out

    @staticmethod
    def _public_pending(state):
        pending = state.get('pending') or {}
        return {'description': pending.get('description'), 'url': pending.get('url')} if pending else None

    def notice(self):
        """A change that is still owed to the admin after the page that asked for
        it is gone. None when there is none."""
        state = self._read()
        if (state.get('status') != 'healed' or state.get('resumed_action_id') or not state.get('pending')
                or state.get('acknowledged')
                or self.clock() - float(state.get('requested_at') or 0) > PENDING_TTL):
            return None
        return {'message': NOT_APPLIED, **self._public_pending(state)}

    def acknowledge(self):
        with self._locked():
            state = self._read()
            if state:
                state['acknowledged'] = True
                self._write(state)


def status_report(stack_root=None, state_dir=None, out=print):
    """For `rzfz status` (run with docker exec, because only this container sees
    its own binds): `BIND<TAB>ok|stale<TAB>files` and, when a repair ran,
    `HEAL<TAB>status<TAB>epoch<TAB>error`. Never prints a value from `.env`."""
    stack_root = stack_root or os.environ.get('STACK_ROOT', '/stack')
    state_dir = state_dir or default_state_dir()
    report = env_mount.inspect_env_mounts(stack_root)
    names = ','.join(f['name'] for f in report['files'] if f['stale'])
    out(f'BIND\t{"stale" if report["stale"] else "ok"}\t{names}')
    state = PortalSelfHeal(stack_root, state_dir)._read()
    if state.get('status'):
        error = ' '.join(str(state.get('error') or '').split())
        out(f'HEAL\t{state["status"]}\t{int(float(state.get("requested_at") or 0))}\t{error}')


def default_state_dir():
    audit = os.environ.get('AUDIT_LOG_PATH', '/var/log/razzfazz/config-audit.log')
    return os.path.join(os.path.dirname(audit), 'self-heal')
