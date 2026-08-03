# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Governance blueprint — checksum snapshots, diff, and audit log viewer."""

import json
import os
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

governance_bp = Blueprint('governance', __name__, url_prefix='/governance')


@governance_bp.route('/')
def index():
    """Main governance page with tabs: Checksums + Audit Log."""
    tab = request.args.get('tab', 'checksums')

    # Checksums
    csm = current_app.checksum_manager
    history = csm.get_history(limit=50)
    current_overall = csm.get_current_overall()
    last_overall = history[0]['overall_sha256'] if history else None
    drift = current_overall != last_overall if last_overall else None

    # Audit log
    entries = []
    log_path = current_app.config.get('AUDIT_LOG_PATH', '')
    if os.path.exists(log_path):
        try:
            with open(log_path) as f:
                lines = f.readlines()
            for line in reversed(lines[-100:]):
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

    event_filter = request.args.get('event', '')
    user_filter = request.args.get('user', '')
    if event_filter:
        entries = [e for e in entries if event_filter in e.get('event', '')]
    if user_filter:
        entries = [e for e in entries if user_filter in e.get('user', '')]

    all_events = sorted(set(e.get('event', '') for e in entries if e.get('event')))
    all_users = sorted(set(e.get('user', '') for e in entries if e.get('user')))

    return render_template('governance/index.html',
                           tab=tab,
                           history=history,
                           current_overall=current_overall,
                           drift=drift,
                           entries=entries,
                           event_filter=event_filter,
                           user_filter=user_filter,
                           all_events=all_events,
                           all_users=all_users)


@governance_bp.route('/take', methods=['POST'])
def take_checksum():
    comment = request.form.get('comment', 'Manual snapshot')
    user = session.get('admin_username', 'admin')
    result = current_app.checksum_manager.take_checksum(comment, source='ui')
    current_app.audit_logger.log(
        'governance.snapshot', user=user,
        category='governance', action='checksum_take',
        target=f'checksum_set:{result["overall_sha256"][:12]}',
        detail=f'Checksum snapshot: {comment} ({result["file_count"]} files, {result["overall_sha256"][:12]}...)',
        outcome='success',
    )
    flash(f'Checksum snapshot taken: {result["file_count"]} files, overall: {result["overall_sha256"][:16]}...', 'success')
    return redirect(url_for('governance.index', tab='checksums'))


@governance_bp.route('/detail/<int:set_id>')
def detail(set_id):
    data = current_app.checksum_manager.get_set_detail(set_id)
    if not data:
        flash('Checksum set not found.', 'error')
        return redirect(url_for('governance.index', tab='checksums'))
    return render_template('governance/detail.html', data=data)


@governance_bp.route('/diff')
def diff():
    id_a = request.args.get('a', type=int)
    id_b = request.args.get('b', type=int)
    if not id_a or not id_b:
        flash('Select two checksum sets to compare.', 'error')
        return redirect(url_for('governance.index', tab='checksums'))
    data = current_app.checksum_manager.get_diff(id_a, id_b)
    if not data:
        flash('One or both checksum sets not found.', 'error')
        return redirect(url_for('governance.index', tab='checksums'))
    return render_template('governance/diff.html', data=data)
