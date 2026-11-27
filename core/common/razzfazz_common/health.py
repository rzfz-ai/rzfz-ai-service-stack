# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Health check blueprint shared by all UI containers.

Mounts `GET /healthz` returning a constant 200 JSON payload. Intended to
be unauthenticated — the auth middleware short-circuits this path so
external monitors (Caddy, Komodo, docker healthcheck) can probe without
hitting Authentik.
"""

from flask import Blueprint, jsonify


def get_health_blueprint(service_name: str = 'razzfazz') -> Blueprint:
    """Return a Blueprint with `GET /healthz → 200 OK`.

    `service_name` is echoed back in the JSON body so the operator can tell
    which container responded when probing through a reverse proxy.
    """
    bp = Blueprint('healthz', __name__)

    @bp.route('/healthz')
    def healthz():
        return jsonify({'status': 'ok', 'service': service_name}), 200

    return bp
