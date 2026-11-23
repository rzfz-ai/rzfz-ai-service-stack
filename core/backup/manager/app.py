# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
from flask import render_template, request, redirect, url_for, flash, jsonify, session, abort
import subprocess
import os
import threading
import time
import uuid
import datetime
import re
import secrets
import stat
import logging

from razzfazz_common.flask_app import create_base_app
from razzfazz_common.env_utils import read_env_key, write_env_value as _write_env_value
from razzfazz_common.env_mount import StaleEnvMountError, env_write_guard, inspect_env_mounts
from razzfazz_common.csrf import enable_csrf

# M026 S05 #4 + #148: shared base app provides /healthz, session config, the
# {razzfazz_version, main_domain, brand_color} template context, and a
# global X-Authentik-Username header check (require_auth=True). The header
# check matches the prior @require_authentik_auth behaviour applied to
# every non-healthz route in this file. Caddy `forward_auth` upstream
# enforces actual Authentik authn/authz (admin-only access is configured
# in init-authentik.sh's PolicyBinding step); the header check here is a
# defense-in-depth proxy-trust gate.
#
# secret_key_env=BACKUP_WEBUI_SECRET_KEY preserves the prior env-var name
# so existing operator .env files keep working without migration.
app = create_base_app(
    __name__,
    secret_key_env='BACKUP_WEBUI_SECRET_KEY',
    service_name='razzfazz-backup-management',
    require_auth=True,
)

# F-049 CSRF: replaced 26 lines of local helpers with the shared module
# (#148). enable_csrf wires the same context_processor exposing csrf_token
# to templates and the before_request hook that returns 403 on mismatch
# for POST/PUT/PATCH/DELETE.
enable_csrf(app)

logger = logging.getLogger(__name__)

# F-050: In-memory store for delete nonces (nonce -> filename)
_delete_nonces = {}

SCRIPT_PATH = "/app/backup_manager.py"
LOG_FILE = "/var/log/backup_manager.log"
# rc6.7 #28: Resolve archive dir from env (default `/archive`, the
# canonical bind-mount inside this container — see core/compose.yml
# `../backups:/archive`). Mirrors the Config UI's `_archive_dir()`
# (core/config/app/blueprints/backup.py) so both UIs stay in sync.
BACKUP_DIR = os.environ.get('BACKUP_DIR', '/archive')
ENV_FILE = "/stack/.env"
#: #1224: this container binds .env/.env.dify as single files (core/compose.yml)
#: — the same stale-inode class as the Portal's (#1189). Names the recovery.
ENV_MOUNT_SERVICE = "razzfazz-backup-management"
#: I-08: the volumes a partial restore may target are the ones mounted here
#: (core/compose.yml `<volume>:/restore_targets/<volume>`). backup_manager.py
#: checks the same rule again (`mounted_restore_target`); this one answers the
#: form at once instead of failing in the background.
RESTORE_ROOT = "/restore_targets"
_RESTORE_TARGET_RE = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z')


def _is_mounted_restore_target(name):
    """I-08: True only for a single volume name whose RESTORE_ROOT/<name> is a
    real directory (not a symlink, not a path inside a volume)."""
    if not name or not _RESTORE_TARGET_RE.match(name):
        return False
    try:
        st = os.lstat(os.path.join(RESTORE_ROOT, name))
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)

def sizeof_fmt(num, suffix='B'):
    for unit in ['','Ki','Mi','Gi','Ti','Pi','Ei','Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)

def run_manager(args):
    """Run backup_manager.py and return output."""
    cmd = ["python3", "-u", SCRIPT_PATH] + args
    try:
        # Use Popen to run in background if needed, but for 'list' we wait.
        # For this function, we assume blocking execution unless called in a thread.
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout + "\n" + result.stderr
    except Exception as e:
        return str(e)

# A restore's result, not only its log: run_manager drops the exit code, a
# restore job keeps it so the page can say OK / PARTIAL / FAILED.
# The store is bounded: finished jobs expire after _RESTORE_JOB_TTL seconds and
# at most _RESTORE_JOBS_MAX jobs are kept (finished ones are evicted first,
# oldest first).
_RESTORE_JOBS: dict = {}
_RESTORE_JOBS_LOCK = threading.Lock()
_RESTORE_JOBS_MAX = 50
_RESTORE_JOB_TTL = 3600


def _restore_verdict(rc):
    try:
        from backup_manager import EXIT_RESTORE_PARTIAL
    except Exception:  # noqa: BLE001 -- the constant's value on 2026.09-ga.4
        EXIT_RESTORE_PARTIAL = 3
    if rc == 0:
        return "OK"
    return "PARTIAL" if rc == EXIT_RESTORE_PARTIAL else "FAILED"


def _prune_restore_jobs(now=None):
    """Drop expired finished jobs, then enforce the cap. Caller holds the lock."""
    now = time.time() if now is None else now
    for jid in [j for j, v in _RESTORE_JOBS.items()
                if v.get("finished_at") and now - v["finished_at"] > _RESTORE_JOB_TTL]:
        del _RESTORE_JOBS[jid]
    excess = len(_RESTORE_JOBS) - _RESTORE_JOBS_MAX
    if excess > 0:
        # finished first, then oldest started
        order = sorted(_RESTORE_JOBS, key=lambda j: (_RESTORE_JOBS[j]["state"] != "done",
                                                     _RESTORE_JOBS[j].get("started_at", 0)))
        for jid in order[:excess]:
            del _RESTORE_JOBS[jid]


#: the manager's own account of what a PARTIAL restore did not bring back
#: (backup_manager.restore_full logs it as "PARTIAL RESTORE: …")
_PARTIAL_MARK = "PARTIAL RESTORE:"


def _missing_lines(text):
    """The manager's PARTIAL RESTORE lines, without the log prefix. They come
    before the stack restart's lines, so the job's tail does not hold them."""
    out = []
    for line in text.splitlines():
        i = line.find(_PARTIAL_MARK)
        if i >= 0:
            out.append(line[i:].strip()[:2000])
    return out[:10]


def _run_restore_job(job_id, args):
    cmd = ["python3", "-u", SCRIPT_PATH] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        rc, text = proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 -- a job that cannot start is FAILED, with the reason
        rc, text = -1, str(exc)
    with _RESTORE_JOBS_LOCK:
        job = _RESTORE_JOBS.get(job_id)
        if job is not None:
            verdict = _restore_verdict(rc)
            job.update(state="done", rc=rc, verdict=verdict,
                       tail=[l for l in text.strip().splitlines() if l.strip()][-5:],
                       missing=_missing_lines(text) if verdict == "PARTIAL" else [],
                       finished_at=time.time())

# ==============================================================================
# Backup Schedule Configuration
# ==============================================================================

# #139: BACKUP_EXCLUDE_REGEXP is RECONCILED, never rebuilt. Every token this
# UI does not own (runbook/operator excludes — e.g. the regenerable caches
# from the prod 2026-06-18 postmortem) survives a save verbatim; only the
# toggle-owned tokens and the #182 always-on DB-datadir tokens are managed.
# The block is duplicated verbatim in core/config/app/blueprints/backup.py
# and core/backup/manager/app.py (separate images/build contexts) — a unit
# test pins the two copies byte-identical.
_MODEL_TOKENS = ('gpustack-data', 'speaches-data', 'llm-node-models', 'llm-registry-data')  # #1450: manager model stores
_DIFY_TOKEN = 'dify-plugin-daemon'
_ALWAYS_TOKENS = ('postgres-data', 'valkey-data', 'postgres-komodo-data')
# #2833: the Wazuh event store follows BACKUP_INCLUDE_WAZUH_INDEX (default true =
# archived, as decided in #855); false puts this token in.
_WAZUH_INDEX_VOLUME = 'wazuh-indexer-data'


def reconcile_exclude_regexp(current, include_models, exclude_dify_plugins, include_wazuh_index=None):
    """The new BACKUP_EXCLUDE_REGEXP after a settings save.

    ``exclude_dify_plugins=None`` = this UI has no dify toggle — preserve
    the token's current presence instead of deciding. The same for
    ``include_wazuh_index=None`` and the Wazuh event-store token (#2833).
    """
    tokens = [t for t in (current or '').split('|') if t]
    if exclude_dify_plugins is None:
        exclude_dify_plugins = _DIFY_TOKEN in tokens
    if include_wazuh_index is None:
        include_wazuh_index = _WAZUH_INDEX_VOLUME not in tokens
    managed = set(_MODEL_TOKENS) | {_DIFY_TOKEN, _WAZUH_INDEX_VOLUME} | set(_ALWAYS_TOKENS)
    kept = [t for t in tokens if t not in managed]
    out = []
    if not include_models:
        out += list(_MODEL_TOKENS)
    out += list(_ALWAYS_TOKENS)
    if exclude_dify_plugins:
        out.append(_DIFY_TOKEN)
    if not include_wazuh_index:
        out.append(_WAZUH_INDEX_VOLUME)
    return '|'.join(out + kept)


def read_env_value(key):
    """Read a value from the .env file.

    M026 S05 #4: thin shim over razzfazz_common.env_utils.read_env_key
    to preserve the existing call sites (which expect None on miss vs
    the shared helper's default=''). Returns None when the file is
    missing OR the key is absent so get_backup_config()'s `or default`
    fallbacks keep working unchanged.
    """
    if not os.path.exists(ENV_FILE):
        return None
    # Sentinel default so we can distinguish "key absent" from "key set to ''".
    _MISSING = object()
    val = read_env_key(ENV_FILE, key, default=_MISSING)
    return None if val is _MISSING else val

def write_env_value(key, value):
    """Write/update a value in the .env file.

    M026 S05 #4: delegates to razzfazz_common.env_utils.write_env_value.
    Note the on-disk format change: the local pre-S05 writer wrapped
    every value in double quotes (`KEY="value"`); the shared writer
    writes `KEY=value` unquoted to match scripts/lib.sh's
    `update_env_value`. Both forms parse identically per the .env spec
    (operator-edited files already mix both styles).
    """
    if not os.path.exists(ENV_FILE):
        return False
    # #1224: a stale single-file bind turns the write into a bare EROFS/EBUSY;
    # the guard names the one-time recovery (recreate THIS container).
    with env_write_guard(ENV_FILE, service=ENV_MOUNT_SERVICE):
        _write_env_value(ENV_FILE, key, value)
    return True

def parse_cron_expression(cron_expr):
    """Parse a cron expression into user-friendly components."""
    parts = cron_expr.split()
    if len(parts) != 5:
        return None
    
    minute, hour, day_of_month, month, day_of_week = parts
    
    result = {
        'minute': minute,
        'hour': hour,
        'day_of_month': day_of_month,
        'month': month,
        'day_of_week': day_of_week,
        'frequency': 'custom'
    }
    
    # T07 / S04-BUG-01 fix (2026-05-16): gate the 'daily' / 'weekly' /
    # 'monthly' labels on the minute+hour fields being concrete digits.
    # Previously `*/15 * * * *` was labelled 'daily' because day_of_month
    # == month == day_of_week == '*' (correct) BUT the minute field was
    # `*/15` (not a daily pattern — runs every 15 minutes). Per-minute or
    # per-15-minute patterns should fall through to 'custom'.
    def _is_concrete_digits(field: str) -> bool:
        # Accept things like "0", "30", "0,15,30,45" — not "*", "*/15",
        # ranges, or step expressions.
        if not field or field == '*':
            return False
        # Disallow any cron metacharacter that signals a recurrence
        # pattern rather than a fixed value list.
        if any(c in field for c in '*/-'):
            return False
        return all(seg.isdigit() for seg in field.split(','))

    minute_concrete = _is_concrete_digits(minute)
    hour_concrete = _is_concrete_digits(hour) or hour == '*'  # hourly allowed

    # Detect common patterns
    if (minute_concrete and hour_concrete
            and day_of_month == '*' and month == '*'):
        if day_of_week == '*':
            result['frequency'] = 'daily'
        elif day_of_week in ['0', '7', 'SUN']:
            result['frequency'] = 'weekly_sunday'
        elif day_of_week in ['1', 'MON']:
            result['frequency'] = 'weekly_monday'

    if (minute_concrete and hour_concrete
            and day_of_month == '1' and month == '*' and day_of_week == '*'):
        result['frequency'] = 'monthly'

    return result

def build_cron_expression(frequency, hour, minute):
    """Build a cron expression from user-friendly inputs."""
    hour = str(hour).zfill(2)
    minute = str(minute).zfill(2)
    
    if frequency == 'daily':
        return f"{minute} {hour} * * *"
    elif frequency == 'weekly_sunday':
        return f"{minute} {hour} * * 0"
    elif frequency == 'weekly_monday':
        return f"{minute} {hour} * * 1"
    elif frequency == 'monthly':
        return f"{minute} {hour} 1 * *"
    elif frequency == 'hourly':
        return f"{minute} * * * *"
    else:
        return f"{minute} {hour} * * *"  # Default to daily

def get_backup_config():
    """Get current backup configuration."""
    cron_expr = read_env_value('BACKUP_CRON_EXPRESSION') or '0 3 * * *'
    retention = read_env_value('BACKUP_RETENTION_DAYS') or '7'
    filename = read_env_value('BACKUP_FILENAME') or 'backup-%Y-%m-%d-%H%M.tar.gz'
    include_models = read_env_value('BACKUP_INCLUDE_MODEL_FILES') or os.environ.get('BACKUP_INCLUDE_MODEL_FILES', 'false')
    encryption_password = read_env_value('BACKUP_ENCRYPTION_PASSWORD') or ''
    
    parsed = parse_cron_expression(cron_expr)
    
    return {
        'cron_expression': cron_expr,
        'retention_days': retention,
        'filename_pattern': filename,
        'include_model_files': include_models.lower() == 'true',
        'encryption_password_set': bool(encryption_password),
        'parsed': parsed
    }

def parse_backup_filename(filename):
    """
    Parses backup filename to extract metadata.
    Format:
       Full: backup-%Y-%m-%d-%H%M.tar.gz[.gpg]
       Partial: partial-{target}-%Y-%m-%d-%H%M.tar.gz[.gpg]

    rc6.7 #6: handle the optional `.gpg` suffix added by F-A2 backup
    encryption (rc2+). Use backup_basename to strip whichever suffix is
    present so the timestamp split works on both forms.

    rc6.7 #28: also expose `display_name` — the basename without the
    `.gpg` suffix — so the table cell shows the canonical archive name
    regardless of encryption state, plus an `encrypted` flag for
    optional UI hinting. The `name` field still carries the on-disk
    filename (used by restore/delete).
    """
    encrypted = filename.endswith('.gpg')
    display_name = filename[:-4] if encrypted else filename
    try:
        # Inline equivalent of backup_manager.backup_basename — strip
        # whichever extension(s) are present so the timestamp split
        # works on plain (.tar.gz), encrypted (.tar.gz.gpg), and any
        # mix in between. We avoid importing backup_manager because
        # its module-level docker.from_env() call crashes inside this
        # container (DOCKER_HOST=tcp://docker-socket-proxy, no
        # /var/run/docker.sock). See rc6.7 #28 / Fix #34.
        # T07 / S04-BUG-02 fix (2026-05-16): strip BOTH .gpg AND
        # .tar.gz, not just .gpg. Without this, on a plain (unencrypted)
        # backup the timestamp split would leave ".tar.gz" in the
        # time field (e.g. "1300.tar.gz" → time="13:00.tar.gz").
        bare = filename
        if bare.endswith('.gpg'):
            bare = bare[:-4]
        if bare.endswith('.tar.gz'):
            bare = bare[:-7]
        if filename.startswith("partial-"):
            parts = bare.split("-")
            # partial-target-target-YYYY-MM-DD-HHMM
            # The date/time is at the end. 
            # YYYY is parts[-4], MM is parts[-3], DD is parts[-2], HHMM is parts[-1]
            date_str = f"{parts[-4]}-{parts[-3]}-{parts[-2]}"
            time_str = parts[-1]
            formatted_time = f"{time_str[:2]}:{time_str[2:]}"
            
            # Content is everything between 'partial-' and the date
            content = "-".join(parts[1:-4])
            return {
                "name": filename,
                "display_name": display_name,
                "encrypted": encrypted,
                "type": "Partial",
                "content": content,
                "date": date_str,
                "time": formatted_time
            }
        elif filename.startswith("backup-"):
            # backup-YYYY-MM-DD-HHMM.tar.gz[.gpg]
            # backup-YYYY-MM-DD.tar.gz[.gpg] (legacy)
            parts = bare.split("-")
            if len(parts) >= 5: # Includes HHMM
                date_str = f"{parts[1]}-{parts[2]}-{parts[3]}"
                time_str = parts[4]
                formatted_time = f"{time_str[:2]}:{time_str[2:]}"
            else:
                date_str = f"{parts[1]}-{parts[2]}-{parts[3]}"
                formatted_time = "03:00" # Default cron time if missing
            
            return {
                "name": filename,
                "display_name": display_name,
                "encrypted": encrypted,
                "type": "Full",
                "content": "All Volumes",
                "date": date_str,
                "time": formatted_time
            }
    except Exception:
        return {
            "name": filename,
            "display_name": display_name,
            "encrypted": encrypted,
            "type": "Unknown",
            "content": "?",
            "date": "?",
            "time": "?"
        }
    return None

# /healthz is provided by create_base_app via razzfazz_common.health
# (returns {"status": "ok", "service": "razzfazz-backup-management"}).
# The pre-S05 path returned `"service": "razzfazz-backup"`; the new value
# matches the container_name in core/compose.yml for monitor disambiguation.

@app.route('/')
def index():
    # List backups.
    #
    # rc6.7 #28: list directly from the archive directory instead of
    # shelling out to `backup_manager.py list`. The subprocess path
    # imported `backup_manager` at the top of the module, which eagerly
    # constructs `docker.DockerClient(base_url='unix://var/run/docker.sock')`
    # on import — but this container talks to docker via DOCKER_HOST=
    # tcp://docker-socket-proxy and has no unix socket mounted, so the
    # subprocess crashed before printing anything and the UI showed
    # zero backups even when files existed in /archive. Mirror the
    # Config UI's `_list_backups()` (core/config/app/blueprints/backup.py)
    # which scans `_archive_dir()` directly and accepts both `.tar.gz`
    # and `.tar.gz.gpg` via `is_backup_file()`.
    try:
        # Inline equivalent of backup_manager.is_backup_file — accept
        # both unencrypted (.tar.gz) and encrypted (.tar.gz.gpg) archive
        # filenames. We avoid importing backup_manager because its
        # module-level docker.from_env() call crashes inside this
        # container (DOCKER_HOST=tcp://docker-socket-proxy, no
        # /var/run/docker.sock). The except-clause used to swallow the
        # ImportError silently, leaving the UI showing zero backups even
        # when files existed in /archive. See rc6.7 #28 / Fix #34.
        backups = []
        if os.path.isdir(BACKUP_DIR):
            raw_files = sorted(
                (f for f in os.listdir(BACKUP_DIR)
                 if f.endswith(('.tar.gz', '.tar.gz.gpg'))),
                reverse=True,
            )
            for f in raw_files:
                meta = parse_backup_filename(f)
                if meta:
                    path = os.path.join(BACKUP_DIR, f)
                    try:
                        meta["size"] = sizeof_fmt(os.path.getsize(path))
                    except OSError:
                        meta["size"] = "Unknown"
                    backups.append(meta)
    except Exception as e:
        backups = []
        flash(f"Error listing backups: {str(e)}")

    # Get backup config for display
    backup_config = get_backup_config()

    return render_template('index.html', backups=backups, backup_config=backup_config)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    """Backup schedule configuration page."""
    if request.method == 'POST':
        frequency = request.form.get('frequency', 'daily')
        hour = int(request.form.get('hour', 3))
        minute = int(request.form.get('minute', 0))
        retention = request.form.get('retention', '7')
        include_models = request.form.get('include_models', 'off') == 'on'
        
        # Build cron expression
        cron_expr = build_cron_expression(frequency, hour, minute)
        
        # Save to .env
        write_env_value('BACKUP_CRON_EXPRESSION', cron_expr)
        write_env_value('BACKUP_RETENTION_DAYS', retention)
        write_env_value('BACKUP_INCLUDE_MODEL_FILES', 'true' if include_models else 'false')
        # #182 + #139: reconcile (never rebuild) the exclude regexp. The model
        # tokens follow the include_models toggle, the physical DB datadirs
        # (captured logically by core/backup/pre-backup.sh) stay always-on
        # (removing them REQUIRES restoring the stop labels — #157), the dify
        # toggle lives only in the Config UI (None = keep current state), and
        # every operator/runbook token survives the save.
        # #2833: the Wazuh event store has no UI toggle; .env decides.
        include_wazuh_index = (read_env_value('BACKUP_INCLUDE_WAZUH_INDEX') or 'true').strip('"\' ').lower() != 'false'
        write_env_value('BACKUP_EXCLUDE_REGEXP', reconcile_exclude_regexp(
            read_env_value('BACKUP_EXCLUDE_REGEXP') or '', include_models, None, include_wazuh_index))
        
        # Handle encryption password — #2132: through the keyring (the old value
        # is kept as ..._PREVIOUS_1); #2409: "clear" is "back to the admin
        # password", never an empty passphrase (the cron would write plaintext).
        encryption_password = request.form.get('encryption_password', '')
        encryption_action = request.form.get('encryption_action', 'keep')
        if encryption_action in ('set', 'clear'):
            from razzfazz_common.backup_keyring import rotate as keyring_rotate
            from razzfazz_common.env_utils import read_env_file
            new_pw = (encryption_password if encryption_action == 'set'
                      else (read_env_value('AUTHENTIK_BOOTSTRAP_PASSWORD') or ''))
            try:
                for line in keyring_rotate(
                        read_env_file(ENV_FILE), new_pw,
                        lambda u: [write_env_value(k, v) for k, v in u.items()]):
                    flash(line, 'warning' if line.startswith('WARNING') else 'info')
            except ValueError as e:
                flash(f'Encryption password NOT changed: {e}', 'error')
                return redirect(url_for('settings'))
        
        # BSB-04: BACKUP_ENCRYPTION_PASSWORD is re-read from .env at every
        # backup invocation now (CLI + UI paths via the razzfazz-backup
        # wrapper, plus the restore path via _read_passphrase_from_env_file).
        # No container restart needed for the manually-triggered paths.
        # Other settings (BACKUP_CRON_EXPRESSION, retention, filename) are
        # consumed by offen's daemon at start and DO still need a restart;
        # message reflects the partial-restart reality.
        flash(
            'Backup settings saved. Encryption-password rotation takes effect '
            'immediately for manually-triggered backups (CLI + UI). Schedule, '
            'retention, and filename changes require a backup-service restart.',
            'success'
        )
        return redirect(url_for('settings'))
    
    config = get_backup_config()
    return render_template('settings.html', config=config,
                           env_mount=env_mount_state())


def env_mount_state():
    """#1224: is this container's view of .env / .env.dify the host's current
    file? A failing probe hides the banner, never the page."""
    try:
        return inspect_env_mounts(os.path.dirname(ENV_FILE), service=ENV_MOUNT_SERVICE)
    except Exception:
        return None


@app.errorhandler(StaleEnvMountError)
def _stale_env_mount(exc):
    """#1224: every .env writer in this app goes through env_write_guard; the
    actionable message (which container to recreate) reaches the operator
    instead of a 500 with `[Errno 30] Read-only file system`."""
    flash(str(exc), 'error')
    return redirect(request.referrer or url_for('settings'))

@app.route('/api/backup-config')
def api_backup_config():
    """API endpoint for backup configuration."""
    return jsonify(get_backup_config())

@app.route('/delete_backup', methods=['POST'])
def delete_backup_route():
    """F-050: Nonce-based two-step delete confirmation.

    First POST (without nonce): returns a nonce that must be sent back.
    Second POST (with matching nonce): performs the actual deletion.
    """
    b_file = request.form.get('file')
    if not b_file:
         return jsonify({"status": "error", "message": "No file specified."}), 400

    # Sanitize
    if "/" in b_file or "\\" in b_file:
         return jsonify({"status": "error", "message": "Invalid filename."}), 400

    nonce = request.form.get('delete_nonce')
    username = request.headers.get('X-Authentik-Username', 'unknown')

    if not nonce:
        # Step 1: Generate nonce and return it
        new_nonce = secrets.token_hex(16)
        _delete_nonces[new_nonce] = b_file
        logger.info(f"F-050: Delete nonce generated for '{b_file}' by user '{username}'")
        return jsonify({
            "status": "confirm",
            "message": f"Confirm deletion of {b_file}?",
            "delete_nonce": new_nonce
        })

    # Step 2: Verify nonce and execute deletion
    expected_file = _delete_nonces.pop(nonce, None)
    if expected_file is None or expected_file != b_file:
        logger.warning(f"F-050: Invalid delete nonce for '{b_file}' from user '{username}'")
        return jsonify({"status": "error", "message": "Invalid or expired confirmation nonce."}), 400

    # F-050: Audit log for destructive operation
    logger.info(f"AUDIT: Backup deletion confirmed — file='{b_file}', user='{username}'")

    # #2713: synchronous (a delete is a few unlinks), so the answer is the
    # result: a no-op or a half-done delete is an error naming what is left.
    rc, text = _run_manager_rc(["delete", "--file", b_file])
    if rc != 0:
        logger.warning(f"Backup deletion of '{b_file}' failed (rc={rc})")
        return jsonify({"status": "error",
                        "message": f"{b_file} was not (fully) deleted: "
                                   + (_last_lines(text) or "see the backup log.")}), 500
    return jsonify({"status": "deleted", "message": f"Deleted {b_file}."})

@app.route('/logs')
def get_logs():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return f.read()
    return ""

def _run_manager_rc(args):
    """Like run_manager, but the exit code is kept: (rc, output)."""
    try:
        proc = subprocess.run(["python3", "-u", SCRIPT_PATH] + args,
                              capture_output=True, text=True)
        return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 -- a manager that cannot start is a failure, with the reason
        return -1, str(exc)


def _last_lines(text, n=3):
    return " ".join([l.strip() for l in (text or "").splitlines() if l.strip()][-n:])[:500]


def _archives_from(text):
    """The manager prints one `ARCHIVE: <name>` line per archive a backup run wrote (#2713)."""
    return [l.split("ARCHIVE:", 1)[1].strip() for l in (text or "").splitlines()
            if l.startswith("ARCHIVE:")]


def run_backup_background(b_type, target=None, job_id=None):
    args = ["backup", "--type", b_type]
    if target:
        args.extend(["--target", target])
    if job_id is None:
        run_manager(args)
        return
    rc, text = _run_manager_rc(args)
    with _RESTORE_JOBS_LOCK:
        job = _RESTORE_JOBS.get(job_id)
        if job is not None:
            job.update(state="done", rc=rc, archives=_archives_from(text),
                       verdict="OK" if rc == 0 else "FAILED", finished_at=time.time(),
                       tail=[l for l in text.strip().splitlines() if l.strip()][-5:])

@app.route('/backup', methods=['POST'])
def backup():
    b_type = request.form.get('type')
    target = request.form.get('target')
    # #2639: the same target check as the restore form (I-08)
    if b_type == 'partial' and not _is_mounted_restore_target(target):
        return jsonify({"status": "error",
                        "message": "Unknown backup target: enter the name of a mounted volume."}), 400

    # Run in background thread
    # #2713: a job id resolves to the archive name(s) via /backup/status/<id>
    job_id = uuid.uuid4().hex
    with _RESTORE_JOBS_LOCK:
        _RESTORE_JOBS[job_id] = {"state": "running", "rc": None, "verdict": None, "tail": [],
                                 "archives": [], "started_at": time.time()}
        _prune_restore_jobs()
    t = threading.Thread(target=run_backup_background, args=(b_type, target, job_id))
    t.start()

    return jsonify({"status": "started", "job_id": job_id,
                    "message": "Backup process started in background."})


@app.route('/backup/status/<job_id>')
def backup_status(job_id):
    with _RESTORE_JOBS_LOCK:
        _prune_restore_jobs()
        job = _RESTORE_JOBS.get(job_id)
        if job is None:
            return jsonify({"status": "error", "message": "Unknown backup job."}), 404
        return jsonify({k: job.get(k) for k in ("state", "rc", "verdict", "tail", "archives")})

@app.route('/restore', methods=['POST'])
def restore():
    b_file = request.form.get('file')
    b_type = request.form.get('restore_type')
    target = request.form.get('restore_target')
    confirmation = request.form.get('confirmation')

    if b_type == 'full' and confirmation != "I am totally sure":
        return jsonify({"status": "error", "message": "Incorrect confirmation phrase."}), 400
    
    if not b_file:
        return jsonify({"status": "error", "message": "No backup file selected."}), 400
    if '/' in b_file or '..' in b_file:
        return jsonify({"status": "error", "message": "Invalid filename."}), 400

    if b_type == 'partial' and not _is_mounted_restore_target(target):
        return jsonify({"status": "error",
                        "message": "Unknown restore target: enter the name of a mounted volume."}), 400

    args = ["restore", "--file", b_file, "--type", b_type]
    if target:
        args.extend(["--target", target])
        
    # Run in background thread
    job_id = uuid.uuid4().hex
    with _RESTORE_JOBS_LOCK:
        _RESTORE_JOBS[job_id] = {"state": "running", "rc": None, "verdict": None, "tail": [],
                                 "started_at": time.time()}
        _prune_restore_jobs()
    t = threading.Thread(target=_run_restore_job, args=(job_id, args))
    t.start()

    return jsonify({"status": "started", "job_id": job_id,
                    "message": "Restore process started in background."})


@app.route('/restore/status/<job_id>')
def restore_status(job_id):
    with _RESTORE_JOBS_LOCK:
        _prune_restore_jobs()
        job = _RESTORE_JOBS.get(job_id)
        if job is None:
            return jsonify({"status": "error", "message": "Unknown restore job."}), 404
        out = {k: job.get(k) for k in ("state", "rc", "verdict", "tail")}
        if job.get("missing"):
            out["missing"] = job["missing"]
        return jsonify(out)

if __name__ == "__main__":
    # F-049: In production, gunicorn is used (see Dockerfile CMD).
    # For local development, bind to 127.0.0.1 only.
    app.run(host='127.0.0.1', port=5000)
