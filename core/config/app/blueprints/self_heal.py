# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3058 — what the admin sees while the Settings portal repairs its `.env` bind.

Every writer of `.env` raises `StaleEnvMountError` on a stale bind before it
writes, so the handler below is the one place a settings save lands in. The form
the admin submitted goes back to their browser in the answer and nowhere else:
it may hold a password, and the state on the volume holds no value.
"""
from urllib.parse import urlsplit

from flask import (Blueprint, current_app, jsonify, make_response, render_template, request,
                   session)
from razzfazz_common.csrf import get_csrf_token

from app.services import env_mount

self_heal_bp = Blueprint('self_heal', __name__)


def request_identity():
    return (session.get('admin_username', request.headers.get('X-Authentik-Username', 'admin')),
            request.headers.get('X-Forwarded-For', request.remote_addr))


def _wants_json():
    # fetch() and HTMX callers send the header; a plain form sends the field.
    return request.path.startswith('/api/') or request.is_json or 'X-CSRF-Token' in request.headers


def local_path(url):
    path = urlsplit(url or '').path
    return path if path.startswith('/') and not path.startswith('//') else None


def handle_stale_env(error):
    user, source_ip = request_identity()
    # The page the admin came from: the route that failed may be POST-only.
    page = local_path(request.referrer)
    description = f'Your change on {page or request.path}'
    outcome = current_app.self_heal.begin(
        description, user, source_ip, pending={'kind': 'offer', 'description': description, 'url': page})
    if _wants_json():
        return jsonify({'error': outcome['message'], 'self_heal': outcome}), 503
    has_files = any(f and f.filename for f in request.files.values())
    fields = [(k, v) for k, v in request.form.items(multi=True) if k != '_csrf_token']
    response = make_response(render_template(
        'self_heal.html', outcome=outcome, form_action=request.path, form_fields=fields,
        has_files=has_files, self_heal_page=True))
    # The page may carry what the admin typed into the form.
    response.headers['Cache-Control'] = 'no-store'
    return response


@self_heal_bp.route('/api/self-heal')
def status():
    body = current_app.self_heal.status()
    body['csrf_token'] = get_csrf_token()
    return jsonify(body)


@self_heal_bp.route('/api/self-heal', methods=['POST'])
def start():
    """The banner's "Reconnect now": a stale bind found without any change waiting."""
    stack_root = current_app.config['STACK_ROOT']
    if not env_mount.inspect_env_mounts(stack_root)['stale']:
        return jsonify({'self_heal': {'state': 'healthy', 'message': 'The Settings portal is connected.'}})
    user, source_ip = request_identity()
    outcome = current_app.self_heal.begin('Reconnect now', user, source_ip)
    return jsonify({'self_heal': outcome})


@self_heal_bp.route('/api/self-heal/ack', methods=['POST'])
def acknowledge():
    current_app.self_heal.acknowledge()
    return jsonify({'ok': True})
