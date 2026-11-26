# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2476 (A21) — `/bao/v1/<path>`: the agents' read-only way to OpenBao
(services/bao_proxy.py decides). 404 when OpenBao is not wired."""
from __future__ import annotations

import json

from flask import Blueprint, Response, current_app, request

bao_bp = Blueprint('bao', __name__)


@bao_bp.route('/bao/v1/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def bao(path):
    proxy = getattr(current_app, 'bao_proxy', None)
    if proxy is None:
        return Response('Not found', status=404, content_type='text/plain; charset=utf-8')
    query = request.args.to_dict(flat=False)
    if any(len(v) != 1 for v in query.values()):
        status, body = 403, {"errors": ["permission denied"]}
    else:
        status, body = proxy.handle(request.method, path, {k: v[0] for k, v in query.items()},
                                    request.headers.get('X-Vault-Token', ''), request.remote_addr,
                                    request.host, request.get_json(silent=True))
    return Response('' if body is None else json.dumps(body), status=status,
                    content_type='application/json')
