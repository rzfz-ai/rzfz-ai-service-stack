# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Authentik-SSO authentication middleware + break-glass login routes.

#18 (Option A) — the Configuration Portal now TRUSTS the Authentik SSO it
already sits behind. `config.<domain>` is gated by Caddy `forward_auth` →
Authentik, which injects the `X-Authentik-*` headers on every authenticated
request. Previously this app ALSO demanded its own static `ADMIN_PASSWORD`
in a second login page — a redundant double-login. That is gone.

New behaviour (matches `razzfazz_common.auth.require_authentik_auth`):

  * Normal access via Caddy (Authentik headers present) → allowed by SSO,
    NO second password page. Gated on the admin group
    (`razzfazz.ai Super Admins` — the same group the `administration`
    Authentik application binds to, see core/Authentik/apply-policy-bindings.py).
    A non-admin SSO user is denied (403); they are NOT bounced to a password
    page (a password wouldn't help — the gate is group membership).

  * Break-glass (direct host-port access, no Authentik headers — e.g. SSO /
    Authentik is down and the operator hits 127.0.0.1:5007) → fall back to a
    local password (`ADMIN_PASSWORD`, operator/CLI-managed, NOT broker-managed)
    via the `/auth/login` page so the box stays recoverable.

Net effect for #18: the "config-UI password" for normal use IS the admin's
Authentik password (broker-managed) → no separate change-password UI needed.
"""

import time
from collections import defaultdict
from urllib.parse import urlparse
from flask import (
    Blueprint, current_app, g, jsonify, redirect, render_template,
    request, session, url_for,
)

from razzfazz_common.auth import (
    SUPER_ADMINS_GROUP,
    _check_groups,
    parse_authentik_headers,
)

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

# Simple per-IP rate limiting for login attempts
_login_attempts = defaultdict(list)  # ip -> [timestamp, ...]
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes
_LOGIN_LOCKOUT_SECONDS = 600  # 10 minutes

# The Configuration Portal is the single most powerful UI in the stack, so it
# is gated on the universal super-admin group only (same group that the
# `administration` Authentik application binds to). Kept as a module constant
# so tests + future callers can reference the exact gate.
_CONFIG_ADMIN_GROUPS = (SUPER_ADMINS_GROUP,)


def _forbidden():
    """403 for an authenticated-but-non-admin SSO user.

    A password page is NOT offered — the gate is group membership, and
    bouncing them to /auth/login would be misleading (the break-glass
    password is for the no-SSO-headers case only).
    """
    msg = ('Forbidden: this portal requires membership of the '
           f'"{SUPER_ADMINS_GROUP}" group.')
    if request.accept_mimetypes.best == 'application/json' or request.is_json:
        return jsonify({'status': 'error', 'message': msg}), 403
    return (msg, 403)


def register_auth_middleware(app):
    @app.before_request
    def require_admin_auth():
        # Health check — fully unauthenticated
        if request.path == '/healthz':
            return None
        # Static assets — no auth needed.
        # `/static/` is the Flask default; this app's flask_app helper sets
        # `static_url_path='/branding'` so static files (logo, module icons,
        # htmx.min.js, main.js) are served at /branding/<rel>. Both must
        # bypass the gate or the LOGIN PAGE itself can't render its logo
        # (caught by operator on prod 2026.05 cutover — F-PROD-9: "config
        # login razzfazz logo still broken").
        if request.path.startswith(('/static/', '/branding/')):
            return None
        # Docs and licenses — Authentik SSO only (no admin password).
        # Handled by their own blueprints; bypass the admin-group gate here.
        if request.path.startswith(('/docs/', '/licenses/')):
            return None
        # Auth routes themselves — avoid redirect loop
        if request.path.startswith('/auth/'):
            return None

        # --- Normal access via Caddy: trust Authentik SSO -----------------
        parsed = parse_authentik_headers()
        if parsed['username']:
            # Authentik forward-auth headers present. Gate on admin group.
            if not _check_groups(parsed['groups'], _CONFIG_ADMIN_GROUPS):
                return _forbidden()
            # Allowed by SSO — no second password. Mirror the identity into
            # the session so the blueprints' audit logging (which reads
            # session['admin_username']) keeps recording the real user, and
            # expose g.user for templates/view code.
            g.user = {**parsed, 'auth_method': 'authentik'}
            session['admin_username'] = parsed['username']
            return None

        # --- Break-glass: no Authentik headers (direct host-port) ---------
        # SSO/Authentik unreachable — fall back to the local ADMIN_PASSWORD
        # session so the box is still recoverable. Authenticated via the
        # /auth/login page below.
        if not session.get('admin_authenticated'):
            return redirect(url_for('auth.login', next=request.url))
        # Check session timeout
        auth_time = session.get('admin_auth_time', 0)
        timeout = current_app.config.get('PERMANENT_SESSION_LIFETIME', 3600)
        if time.time() - auth_time > timeout:
            session.pop('admin_authenticated', None)
            session.pop('admin_auth_time', None)
            return redirect(url_for('auth.login', next=request.url))
        return None


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    if request.method == 'POST':
        # Rate limiting — prune old attempts and check
        now = time.time()
        attempts = _login_attempts[source_ip]
        _login_attempts[source_ip] = [t for t in attempts if now - t < _LOGIN_LOCKOUT_SECONDS]
        recent = [t for t in _login_attempts[source_ip] if now - t < _LOGIN_WINDOW_SECONDS]

        if len(recent) >= _LOGIN_MAX_ATTEMPTS:
            current_app.audit_logger.log_auth(
                request.headers.get('X-Authentik-Username', 'admin'), source_ip, False)
            error = 'Too many login attempts. Please wait before trying again.'
            return render_template('auth/login.html', error=error)

        password = request.form.get('password', '')
        admin_password = current_app.config.get('ADMIN_PASSWORD', '')
        username = request.headers.get('X-Authentik-Username', 'admin')

        if password and password == admin_password:
            _login_attempts.pop(source_ip, None)
            session['admin_authenticated'] = True
            session['admin_auth_time'] = time.time()
            session['admin_username'] = username
            session.permanent = True

            current_app.audit_logger.log_auth(username, source_ip, True)

            next_url = request.args.get('next', url_for('dashboard.index'))
            parsed = urlparse(next_url)
            if parsed.netloc or parsed.scheme:
                next_url = url_for('dashboard.index')
            return redirect(next_url)
        else:
            _login_attempts[source_ip].append(now)
            current_app.audit_logger.log_auth(username, source_ip, False)
            error = 'Invalid admin password.'

    return render_template('auth/login.html', error=error)


@auth_bp.route('/logout')
def logout():
    username = session.get('admin_username', 'unknown')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    session.pop('admin_authenticated', None)
    session.pop('admin_auth_time', None)
    session.pop('admin_username', None)

    current_app.audit_logger.log(
        'auth.logout',
        user=username,
        source_ip=source_ip,
        category='auth',
        action='logout',
    )

    return redirect(url_for('auth.login'))
