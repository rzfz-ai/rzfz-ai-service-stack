# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Backup blueprint — full port of core/backup/manager/ functionality.

Operations are proxied to the razzfazz-backup-management container via Docker exec,
since that container has all the volume mounts needed for backup/restore.
"""

import json
import logging
import os
import re
import subprocess
import threading
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for

logger = logging.getLogger(__name__)

backup_bp = Blueprint('backup', __name__, url_prefix='/backup')

LOG_FILE_REMOTE = '/var/log/backup_manager.log'
BACKUP_SCRIPT = '/app/backup_manager.py'
BACKUP_CONTAINER = 'razzfazz-backup-management'


def _archive_dir():
    """Resolve the host-bind path to the backups directory at call time.

    rc6.7 #8 fix (also covers #6 / #4 lurker bug): the previous
    `ARCHIVE_DIR = '/stack/backups'` constant assumed the legacy
    `/stack` mount path. The current razzfazz-config container bind-
    mounts the repo at its host path, not at `/stack`, so the constant
    pointed at a non-existent directory and every backup-listing call
    in this blueprint silently returned an empty list. Look up
    STACK_ROOT from the Flask app config (set in app/__init__.py from
    the env var of the same name) at call time. Fallback to the env
    var directly so module-level callers also work.
    """
    try:
        from flask import current_app
        root = current_app.config.get('STACK_ROOT')
        if root:
            return os.path.join(root, 'backups')
    except RuntimeError:
        pass
    return os.path.join(os.environ.get('STACK_ROOT', '/stack'), 'backups')

# rc6.7 #6: backup files come in two flavours since rc2 enabled GPG
# encryption (F-A2). Pre-rc2 boxes have plain `.tar.gz`; post-rc2 boxes
# have `.tar.gz.gpg`. Both can coexist on a box that upgraded across the
# encryption-enable boundary. Recognise both via this canonical pattern.
BACKUP_SUFFIXES = ('.tar.gz.gpg', '.tar.gz')


def _is_backup_file(filename):
    return any(filename.endswith(sfx) for sfx in BACKUP_SUFFIXES)


def _backup_basename(filename):
    """Strip the backup suffix; used to parse the embedded timestamp."""
    for sfx in BACKUP_SUFFIXES:
        if filename.endswith(sfx):
            return filename[: -len(sfx)]
    return filename


def _docker_exec(container, cmd, timeout=600):
    """Execute a command in a container and return output."""
    full_cmd = ['docker', 'exec', container] + cmd
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return 'Command timed out', 1
    except Exception as e:
        return str(e), 1


def _run_backup_manager(args, timeout=600):
    """Run backup_manager.py inside the backup container."""
    return _docker_exec(BACKUP_CONTAINER, ['python3', '-u', BACKUP_SCRIPT] + args, timeout)


def _parse_backup_filename(filename):
    """Parse backup filename into metadata.
    rc6.7 #6: handles both .tar.gz and .tar.gz.gpg via _backup_basename."""
    try:
        bare = _backup_basename(filename)
        encrypted = filename.endswith('.gpg')
        if filename.startswith('partial-'):
            parts = bare.split('-')
            date_str = f'{parts[-4]}-{parts[-3]}-{parts[-2]}'
            time_str = parts[-1]
            content = '-'.join(parts[1:-4])
            return {
                'name': filename, 'type': 'Partial', 'content': content,
                'date': date_str, 'time': f'{time_str[:2]}:{time_str[2:]}',
                'encrypted': encrypted,
            }
        elif filename.startswith('backup-'):
            parts = bare.split('-')
            date_str = f'{parts[1]}-{parts[2]}-{parts[3]}'
            time_str = parts[4] if len(parts) >= 5 else '0300'
            return {
                'name': filename, 'type': 'Full', 'content': 'All Volumes',
                'date': date_str, 'time': f'{time_str[:2]}:{time_str[2:]}',
                'encrypted': encrypted,
            }
    except Exception:
        pass
    return {'name': filename, 'type': 'Unknown', 'content': '?', 'date': '?', 'time': '?', 'encrypted': False}


def _list_backups():
    """List backups from the archive directory."""
    backups = []
    if not os.path.exists(_archive_dir()):
        return backups
    for f in sorted(os.listdir(_archive_dir()), reverse=True):
        if _is_backup_file(f):
            meta = _parse_backup_filename(f)
            path = os.path.join(_archive_dir(), f)
            try:
                size = os.path.getsize(path)
                if size >= 1024 * 1024 * 1024:
                    meta['size'] = f'{size / (1024**3):.1f} GiB'
                elif size >= 1024 * 1024:
                    meta['size'] = f'{size / (1024**2):.1f} MiB'
                else:
                    meta['size'] = f'{size / 1024:.0f} KiB'
            except Exception:
                meta['size'] = 'Unknown'
            backups.append(meta)
    return backups


def _get_backup_config():
    """Get current backup configuration from .env."""
    env = current_app.config_manager.read_env()
    cron_expr = env.get('BACKUP_CRON_EXPRESSION', '0 3 * * *')
    # Parse cron for human-readable display
    parts = cron_expr.split()
    if len(parts) == 5:
        minute, hour = parts[0], parts[1]
        dom, month, dow = parts[2], parts[3], parts[4]
        if dom == '*' and month == '*' and dow == '*':
            freq = 'daily'
        elif dom == '*' and month == '*' and dow in ('0', '7', 'SUN'):
            freq = 'weekly_sunday'
        elif dom == '*' and month == '*' and dow in ('1', 'MON'):
            freq = 'weekly_monday'
        elif dom == '1' and month == '*' and dow == '*':
            freq = 'monthly'
        else:
            freq = 'custom'
    else:
        minute, hour, freq = '0', '3', 'custom'

    return {
        'cron_expression': cron_expr,
        'frequency': freq,
        'hour': hour,
        'minute': minute,
        'retention_days': env.get('BACKUP_RETENTION_DAYS', '7'),
        'include_model_files': env.get('BACKUP_INCLUDE_MODEL_FILES', 'false').lower() == 'true',
        'exclude_dify_plugins': env.get('BACKUP_EXCLUDE_DIFY_PLUGINS', 'false').lower() == 'true',
        'encryption_password_set': bool(env.get('BACKUP_ENCRYPTION_PASSWORD', '')),
    }


def _list_env_snapshots():
    """List env-snapshots from backups/env-snapshots/ (rc6.7 #8).

    Snapshots are written by `scripts/env-snapshot.sh` which is sourced by
    `razzfazz-upgrade.sh migrate_env`. Filename shape:
      env-YYYYMMDD-HHMMSS.tar.gz.enc   (encrypted; default)
      env-YYYYMMDD-HHMMSS.tar.gz       (unencrypted; only when no
                                        BACKUP_ENCRYPTION_PASSWORD)
    A sibling `env-YYYYMMDD-HHMMSS.reason` text file carries the
    one-line reason (e.g. "pre-upgrade-migration"). Treat its absence
    as a soft warning, not an error.
    """
    snap_dir = os.path.join(_archive_dir(), 'env-snapshots')
    snapshots = []
    if not os.path.exists(snap_dir):
        return snapshots
    for f in sorted(os.listdir(snap_dir), reverse=True):
        if not (f.endswith('.tar.gz.enc') or f.endswith('.tar.gz')):
            continue
        path = os.path.join(snap_dir, f)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        # Strip suffix to find the timestamp + sibling .reason file.
        if f.endswith('.tar.gz.enc'):
            base = f[:-len('.tar.gz.enc')]
            encrypted = True
        else:
            base = f[:-len('.tar.gz')]
            encrypted = False
        # base is `env-YYYYMMDD-HHMMSS`
        ts_part = base[len('env-'):] if base.startswith('env-') else base
        # Render `YYYY-MM-DD HH:MM:SS` for the table.
        try:
            d, t = ts_part.split('-', 1)  # `YYYYMMDD`, `HHMMSS`
            display_date = f'{d[0:4]}-{d[4:6]}-{d[6:8]}'
            display_time = f'{t[0:2]}:{t[2:4]}:{t[4:6]}'
        except (ValueError, IndexError):
            display_date = '?'
            display_time = '?'
        # Reason file (sidecar) — not an error if missing.
        reason_path = os.path.join(snap_dir, f'{base}.reason')
        reason = ''
        if os.path.exists(reason_path):
            try:
                with open(reason_path) as rf:
                    reason = rf.read().strip()[:200]
            except OSError:
                pass
        snapshots.append({
            'name': f,
            'date': display_date,
            'time': display_time,
            'size_kb': round(stat.st_size / 1024, 1),
            'encrypted': encrypted,
            'reason': reason,
        })
    return snapshots


@backup_bp.route('/')
def index():
    backups = _list_backups()
    env_snapshots = _list_env_snapshots()
    config = _get_backup_config()
    return render_template('backup/list.html',
                           backups=backups,
                           env_snapshots=env_snapshots,
                           config=config)


@backup_bp.route('/create', methods=['POST'])
def create():
    """rc6.7 #61: kick the backup off in a background thread instead of
    waiting for it inline. The pre-fix POST blocked the only gunicorn
    sync worker (`gunicorn -w 1 --timeout 300`) for the entire backup
    duration — minutes for a full backup. The 3-second JS poll on
    /backup/logs queued behind the worker, hit gunicorn's 300s timeout,
    and the operator saw `Log fetch failed (HTTP 500)` in the operations
    pane (the worker was killed mid-request). Same async pattern as
    the standalone backup-management Flask app already uses."""
    b_type = request.form.get('type', 'full')
    target = request.form.get('target', '')

    args = ['backup', '--type', b_type]
    if target and b_type == 'partial':
        args.extend(['--target', target])

    user = session.get('admin_username', 'admin')
    detail = f'type={b_type}' + (f' target={target}' if target else '')
    audit = current_app.audit_logger

    def _run_async(args, user, detail, audit):
        output, code = _run_backup_manager(args, timeout=3600)
        # 1h timeout — full backups normally finish in <5m but huge stacks
        # with --include-models can run longer; the audit trail still
        # lands correctly when the subprocess returns.
        audit.log(
            'backup.create', user=user,
            category='backup', action='create', target=b_type,
            detail=detail,
            outcome='success' if code == 0 else 'failure',
        )
        if code != 0:
            logger.warning('async backup failed (code=%s): %s', code, output[:500])

    threading.Thread(
        target=_run_async, args=(args, user, detail, audit), daemon=True,
    ).start()

    # Return immediately so the polling /backup/logs sees real progress
    # in the operations pane. Browser shows the `started` toast and the
    # log pane (which reads /var/log/backup_manager.log inside the
    # backup-management container) fills in line-by-line over 30-300s.
    return jsonify({
        'status': 'started',
        'message': f'Backup ({b_type}) started in background. Watch the operations pane for progress.',
    })


@backup_bp.route('/restore', methods=['POST'])
def restore():
    b_file = request.form.get('file', '')
    b_type = request.form.get('restore_type', 'full')
    target = request.form.get('restore_target', '')
    confirmation = request.form.get('confirmation', '')

    if not b_file:
        return jsonify({'status': 'error', 'message': 'No backup file selected.'}), 400
    if '/' in b_file or '..' in b_file:
        return jsonify({'status': 'error', 'message': 'Invalid filename.'}), 400
    if b_type == 'full' and confirmation != 'I am totally sure':
        return jsonify({'status': 'error', 'message': 'Incorrect confirmation phrase.'}), 400

    args = ['restore', '--file', b_file, '--type', b_type]
    if target:
        args.extend(['--target', target])

    output, code = _run_backup_manager(args, timeout=600)

    user = session.get('admin_username', 'admin')
    current_app.audit_logger.log(
        'backup.restore', user=user,
        category='backup', action='restore', target=b_file,
        detail=f'file={b_file} type={b_type}',
        risk='danger',
        outcome='success' if code == 0 else 'failure',
    )
    return jsonify({'status': 'started' if code == 0 else 'error', 'message': output[:500]})


@backup_bp.route('/delete', methods=['POST'])
def delete():
    b_file = request.form.get('file', '')
    if not b_file or '/' in b_file or '..' in b_file:
        return jsonify({'status': 'error', 'message': 'Invalid filename.'}), 400

    output, code = _run_backup_manager(['delete', '--file', b_file])

    user = session.get('admin_username', 'admin')
    current_app.audit_logger.log(
        'backup.delete', user=user,
        category='backup', action='delete', target=b_file,
        outcome='success' if code == 0 else 'failure',
    )
    return jsonify({'status': 'success' if code == 0 else 'error', 'message': output[:500]})


@backup_bp.route('/logs')
def logs():
    """Return the backup operation log from the backup container."""
    output, _ = _docker_exec(BACKUP_CONTAINER, ['cat', LOG_FILE_REMOTE], timeout=5)
    return output or 'No logs available.'


@backup_bp.route('/clear-logs', methods=['POST'])
def clear_logs():
    """Clear the backup operation log."""
    _docker_exec(BACKUP_CONTAINER, ['sh', '-c', f'> {LOG_FILE_REMOTE}'], timeout=5)
    return jsonify({'status': 'ok'})


@backup_bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        frequency = request.form.get('frequency', 'daily')
        hour = int(request.form.get('hour', 3))
        minute = int(request.form.get('minute', 0))
        retention = request.form.get('retention', '7')
        include_models = request.form.get('include_models', 'off') == 'on'
        exclude_dify_plugins = request.form.get('exclude_dify_plugins', 'off') == 'on'

        # Build cron expression
        m, h = str(minute).zfill(2), str(hour).zfill(2)
        cron_map = {
            'daily': f'{m} {h} * * *',
            'weekly_sunday': f'{m} {h} * * 0',
            'weekly_monday': f'{m} {h} * * 1',
            'monthly': f'{m} {h} 1 * *',
            'hourly': f'{m} * * * *',
        }
        cron_expr = cron_map.get(frequency, f'{m} {h} * * *')

        # Derive the offen exclude-regexp from BOTH toggles. Model files
        # (gpustack-data|speaches-data) are excluded unless the operator opts in;
        # dify-plugin-daemon is excluded only when its own toggle is on (#150).
        exclude_parts = []
        if not include_models:
            exclude_parts += ['gpustack-data', 'speaches-data']
        # #182: ALWAYS exclude the physical DB datadirs. They are captured
        # logically by core/backup/pre-backup.sh (pg_dumpall + valkey SAVE) and
        # imported on restore via psql, so the live tar must never touch them —
        # otherwise offen races pg_wal recycling (`lstat …/pg_wal/…: no such
        # file` → whole backup fails silently, #157). Appending them here (never
        # toggle-gated) is what makes it safe for postgres / postgres-komodo to
        # run with stop-during-backup=false (no nightly DB blip). If these are
        # ever removed from the regexp, the stop labels in core/compose.yml +
        # modules/monitor/compose.yml MUST go back to `true`.
        exclude_parts += ['postgres-data', 'valkey-data', 'postgres-komodo-data']
        if exclude_dify_plugins:
            exclude_parts.append('dify-plugin-daemon')

        updates = {
            'BACKUP_CRON_EXPRESSION': cron_expr,
            'BACKUP_RETENTION_DAYS': retention,
            'BACKUP_INCLUDE_MODEL_FILES': 'true' if include_models else 'false',
            'BACKUP_EXCLUDE_DIFY_PLUGINS': 'true' if exclude_dify_plugins else 'false',
            'BACKUP_EXCLUDE_REGEXP': '|'.join(exclude_parts),
        }

        # Encryption
        enc_action = request.form.get('encryption_action', 'keep')
        enc_password = request.form.get('encryption_password', '')
        if enc_action == 'clear':
            updates['BACKUP_ENCRYPTION_PASSWORD'] = ''
        elif enc_action == 'set' and enc_password:
            updates['BACKUP_ENCRYPTION_PASSWORD'] = enc_password

        current_app.config_manager._write_env_file(current_app.config_manager.env_path, updates)

        user = session.get('admin_username', 'admin')
        current_app.audit_logger.log(
            'backup.settings', user=user,
            category='backup', action='update_settings', target='backup-settings',
            outcome='success',
        )
        flash('Backup settings saved. Restart backup-service to apply.', 'success')
        return redirect(url_for('backup.settings'))

    config = _get_backup_config()
    return render_template('backup/settings.html', config=config)
