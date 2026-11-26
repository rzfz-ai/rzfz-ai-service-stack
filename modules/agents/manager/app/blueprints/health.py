# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Health check endpoint — unauthenticated."""

from flask import Blueprint, current_app, jsonify

health_bp = Blueprint('health', __name__)


@health_bp.route('/healthz')
def healthz():
    docker_ok = current_app.docker_client.ping()
    db_ok = True
    try:
        with current_app.db.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        db_ok = False

    status = 'ok' if (docker_ok and db_ok) else 'degraded'
    code = 200 if status == 'ok' else 503

    return jsonify({
        'status': status,
        'service': 'agent-manager',
        'checks': {
            'database': 'ok' if db_ok else 'error',
            'docker': 'ok' if docker_ok else 'error',
        }
    }), code
