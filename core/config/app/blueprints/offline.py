# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Offline / Network blueprint — read-only offline-readiness status (#184 P2).

Renders the box's network mode (online / proxied / offline) + registry mirror,
which overlays are composed, and the ``rzfz verify-images`` / ``rzfz
verify-models`` present/missing summaries. Super-admin gated via the app-wide
before_request middleware (app/auth.py) — no per-route decorator needed. No
mutations: it only reads ``.env`` and shells out to the two verify CLIs (which
themselves only inspect local images / the gpustack volume).

One write: the Time source form saves ``TIME_SOURCE`` / ``TIME_SERVERS`` and queues
the request for the host (services/time_source.py).
"""

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from app.services import offline_status, time_source

offline_bp = Blueprint('offline', __name__, url_prefix='/offline')


@offline_bp.route('/')
def index():
    stack_root = current_app.config['STACK_ROOT']
    # collect() shells out to cli/verify-images.sh + cli/verify-models.sh (same
    # subprocess pattern the rest of the portal uses for docker work) and parses
    # their summary lines; it degrades to `unknown` if docker/exec is unavailable.
    status = offline_status.collect(stack_root)
    ts = time_source.read(getattr(current_app, 'config_manager', None))
    return render_template('offline/index.html', status=status, time_source=ts,
                           time_pending=time_source.pending(stack_root), time_result=time_source.result(stack_root), time_modes=time_source.MODES,
                           time_apply_command=time_source.APPLY_COMMAND)


@offline_bp.route('/time-source', methods=['POST'])
def time_source_save():
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    mode, servers, error = time_source.validate(request.form.get('time_source'), request.form.get('time_servers'))
    if error:
        flash(error, 'error')
        return redirect(url_for('offline.index'))
    queued = time_source.save(current_app.config_manager, current_app.config['STACK_ROOT'], mode, servers)
    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='settings', action='update', target='time-source',
        detail=f'Time source set to {mode}' + (f' ({" ".join(servers)})' if servers and mode == 'lan' else ''),
        risk='safe', outcome='success',
    )
    if queued:
        flash(f'Time source saved ({mode}). The box applies it within a few seconds.', 'success')
    else:
        flash(f'Time source saved ({mode}), but the request could not be queued. Apply it on the box with {time_source.APPLY_COMMAND}', 'warning')
    return redirect(url_for('offline.index'))
