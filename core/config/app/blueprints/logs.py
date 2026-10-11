# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Logs blueprint — log snapshot creation, listing, download, and deletion."""

import os
import subprocess
import tarfile
import time
from io import BytesIO
from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, session, url_for

logs_bp = Blueprint('logs', __name__, url_prefix='/logs')

SNAPSHOT_DIR = '/data/log-snapshots'


def _ensure_dir():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)


def _list_snapshots():
    _ensure_dir()
    snapshots = []
    for f in sorted(os.listdir(SNAPSHOT_DIR), reverse=True):
        if f.endswith('.tar.gz'):
            path = os.path.join(SNAPSHOT_DIR, f)
            stat = os.stat(path)
            snapshots.append({
                'filename': f,
                'size_mb': round(stat.st_size / (1024 * 1024), 2),
                'modified': stat.st_mtime,
            })
    return snapshots


@logs_bp.route('/')
def index():
    snapshots = _list_snapshots()
    support_email = current_app.config_manager.read_env().get('SUPPORT_EMAIL', 'support@razzfazz.ai')
    return render_template('logs/index.html', snapshots=snapshots, support_email=support_email)


@logs_bp.route('/create', methods=['POST'])
def create():
    _ensure_dir()
    timestamp = time.strftime('%Y%m%d-%H%M%S')
    filename = f'logs-{timestamp}.tar.gz'
    filepath = os.path.join(SNAPSHOT_DIR, filename)

    try:
        with tarfile.open(filepath, 'w:gz') as tar:
            # System info
            sysinfo = _collect_system_info()
            _add_string_to_tar(tar, 'system-info.txt', sysinfo)

            # Container logs (last 500 lines each)
            try:
                result = subprocess.run(
                    ['docker', 'ps', '--format', '{{.Names}}'],
                    capture_output=True, text=True, timeout=10,
                )
                for name in result.stdout.strip().splitlines():
                    name = name.strip()
                    if not name:
                        continue
                    try:
                        log_result = subprocess.run(
                            ['docker', 'logs', '--tail', '500', name],
                            capture_output=True, text=True, timeout=15,
                        )
                        content = log_result.stdout + log_result.stderr
                        _add_string_to_tar(tar, f'containers/{name}.log', content)
                    except Exception:
                        pass
            except Exception:
                pass

            # Audit log
            audit_path = current_app.config.get('AUDIT_LOG_PATH', '')
            if os.path.exists(audit_path):
                tar.add(audit_path, arcname='audit-log.jsonl')

        user = session.get('admin_username', 'admin')
        current_app.audit_logger.log(
            'logs.create_snapshot', user=user,
            category='logs', action='create_snapshot', target=filename,
            outcome='success',
        )
        flash(f'Log snapshot created: {filename}', 'success')
    except Exception as e:
        # #1312 re-review, finding 6: the failure path wrote nothing. A
        # half-written logs-*.tar.gz can stay behind in SNAPSHOT_DIR, and the
        # trail named neither the attempt nor the file it left there.
        current_app.audit_logger.log(
            'logs.create_snapshot', user=session.get('admin_username', 'admin'),
            source_ip=request.headers.get('X-Forwarded-For', request.remote_addr),
            category='logs', action='create_snapshot', target=filename,
            detail=f'Snapshot creation failed: {str(e)[:200]}',
            outcome='failure', error=str(e)[:200],
        )
        flash(f'Failed to create snapshot: {str(e)[:200]}', 'error')

    return redirect(url_for('logs.index'))


@logs_bp.route('/download/<filename>')
def download(filename):
    if '/' in filename or '..' in filename:
        flash('Invalid filename.', 'error')
        return redirect(url_for('logs.index'))
    path = os.path.join(SNAPSHOT_DIR, filename)
    if not os.path.exists(path):
        flash('File not found.', 'error')
        return redirect(url_for('logs.index'))
    return send_file(path, as_attachment=True)


@logs_bp.route('/delete', methods=['POST'])
def delete():
    filename = request.form.get('filename', '')
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    def _audit(outcome, detail):
        current_app.audit_logger.log(
            'logs.delete', user=user, source_ip=source_ip,
            category='logs', action='delete', target=filename or '<empty>',
            detail=detail, risk='caution', outcome=outcome,
        )

    if not filename or '/' in filename or '..' in filename:
        # #1312 review, finding 7: this path wrote nothing. A refused path
        # traversal against the snapshot directory is not a non-event — it is
        # the single most interesting thing this route can record, and the
        # trail said nothing about it while faithfully recording the ordinary
        # deletions around it.
        _audit('failure',
               f'Refused a log-snapshot deletion for an unsafe filename: {filename[:120]!r}')
        flash('Invalid filename.', 'error')
        return redirect(url_for('logs.index'))
    path = os.path.join(SNAPSHOT_DIR, filename)
    if not os.path.exists(path):
        # Not a deletion, but the operator asked for one and nothing happened;
        # without a line, "I deleted it" and "it was already gone" look the
        # same in the trail.
        _audit('failure', f'No log snapshot named {filename} — nothing deleted')
        return redirect(url_for('logs.index'))
    # #1312 (BS-SECCOMP-BUG-03): a support snapshot is evidence; deleting one is
    # a mutation and belongs in the trail. Logged AFTER the removal so the entry
    # describes what actually happened.
    os.remove(path)
    _audit('success', f'Deleted the log snapshot {filename}')
    flash(f'Deleted {filename}.', 'success')
    return redirect(url_for('logs.index'))


def _collect_system_info():
    lines = []
    lines.append(f'Timestamp: {time.strftime("%Y-%m-%d %H:%M:%S %Z")}')
    try:
        result = subprocess.run(['uname', '-a'], capture_output=True, text=True, timeout=5)
        lines.append(f'Kernel: {result.stdout.strip()}')
    except Exception:
        pass
    try:
        result = subprocess.run(['docker', 'version', '--format', '{{.Server.Version}}'],
                                capture_output=True, text=True, timeout=5)
        lines.append(f'Docker: {result.stdout.strip()}')
    except Exception:
        pass
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith(('MemTotal', 'MemAvailable')):
                    lines.append(line.strip())
    except Exception:
        pass
    return '\n'.join(lines)


def _add_string_to_tar(tar, name, content):
    import io
    data = content.encode('utf-8', errors='replace')
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


@logs_bp.route('/send-support', methods=['POST'])
def send_support():
    """Send a log snapshot to support via SMTP relay."""
    from flask import jsonify
    email = request.form.get('email', '')
    description = request.form.get('description', '')
    filename = request.form.get('filename', '')
    create_new = request.form.get('create_new') == 'true'

    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    # #1312 re-review, finding 1: this is the route that mails the audit log
    # itself off the box — the snapshot carries `audit-log.jsonl` plus the last
    # 500 lines of every container, and `test_secret_redaction.py::
    # test_log_manager_has_redaction_pass` is xfail, so it is packed unredacted.
    # It used to write an entry only on the success path, which means an
    # attempt that built the snapshot and then failed at the relay left the
    # file on disk and nothing in the trail. One helper, and every exit below
    # goes through it.
    def _audit(outcome, detail, error=None):
        current_app.audit_logger.log(
            'logs.send_support', user=user, source_ip=source_ip,
            category='logs', action='send_support', target=email or '<empty>',
            detail=detail, risk='danger', outcome=outcome, error=error,
        )

    def _fail(message, error=None):
        _audit('failure', message, error=error)
        return jsonify({'success': False, 'message': message})

    if not email:
        return _fail('Email address required.')

    # Create new snapshot if requested
    if create_new or not filename:
        _ensure_dir()
        timestamp = time.strftime('%Y%m%d-%H%M%S')
        filename = f'logs-{timestamp}.tar.gz'
        filepath = os.path.join(SNAPSHOT_DIR, filename)
        try:
            with tarfile.open(filepath, 'w:gz') as tar:
                _add_string_to_tar(tar, 'system-info.txt', _collect_system_info())
                try:
                    result = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'],
                                            capture_output=True, text=True, timeout=10)
                    for name in result.stdout.strip().splitlines():
                        name = name.strip()
                        if not name:
                            continue
                        try:
                            log_result = subprocess.run(
                                ['docker', 'logs', '--tail', '500', name],
                                capture_output=True, text=True, timeout=15)
                            _add_string_to_tar(tar, f'containers/{name}.log',
                                               log_result.stdout + log_result.stderr)
                        except Exception:
                            pass
                except Exception:
                    pass
                audit_path = current_app.config.get('AUDIT_LOG_PATH', '')
                if os.path.exists(audit_path):
                    tar.add(audit_path, arcname='audit-log.jsonl')
        except Exception as e:
            return _fail(f'Failed to create snapshot: {str(e)[:200]}', error=str(e)[:200])
    else:
        filepath = os.path.join(SNAPSHOT_DIR, filename)

    if not os.path.exists(filepath):
        return _fail(f'Snapshot file not found: {filename}')

    # Send via SMTP relay using docker exec into smtp-relay
    env = current_app.config_manager.read_env()
    domain = env.get('MAIN_DOMAIN', 'razzfazz.ai')
    subject = f'[razzfazz.ai] Support logs from {domain} — {time.strftime("%Y-%m-%d %H:%M")}'
    body = f'Domain: {domain}\nDescription: {description}\nSnapshot: {filename}\n'

    try:
        # Use Python's smtplib to send via the local SMTP relay
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders

        msg = MIMEMultipart()
        msg['From'] = env.get('SMTP_FROM', f'noreply@{domain}')
        msg['To'] = email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with open(filepath, 'rb') as f:
            attachment = MIMEBase('application', 'gzip')
            attachment.set_payload(f.read())
            encoders.encode_base64(attachment)
            attachment.add_header('Content-Disposition', f'attachment; filename="{filename}"')
            msg.attach(attachment)

        with smtplib.SMTP('smtp-relay', 587, timeout=30) as server:
            server.sendmail(msg['From'], [email], msg.as_string())

        _audit('success', f'Sent {filename} to {email}')
        return jsonify({'success': True, 'message': f'Log snapshot sent to {email}.'})

    except Exception as e:
        # The snapshot exists on disk at this point; say so, otherwise the
        # trail reads as if nothing was produced.
        return _fail(f'Failed to send: {str(e)[:200]}', error=str(e)[:200])
