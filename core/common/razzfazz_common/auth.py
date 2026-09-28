# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Authentik forward-auth helpers for the razzfazz UI containers.

Consolidates the four near-duplicate implementations that lived in
core/config/app/auth.py, core/setup/app/auth.py, core/help/app.py
(inline), core/backup/manager/app.py (inline). Each container's auth
module had subtly different behaviour:

  - Config UI: full session-based admin password flow + group check
  - Setup UI: similar but with its own session keys
  - Help UI:  inline header parsing only (no group check, no password)
  - Backup UI: inline header parsing only

The consolidated helper exposes a single decorator factory that can be
configured per-route:

    @app.route('/admin')
    @require_authentik_auth(require_groups=['razzfazz.ai Super Admins'])
    def admin_page():
        return render_template('admin.html', user=g.user)

    @app.route('/dashboard')
    @require_authentik_auth(fallback_password_env='CONFIG_ADMIN_PASSWORD')
    def dashboard():
        ...

Header semantics (set by Caddy's forward_auth Authentik integration):
    X-Authentik-Username   single value
    X-Authentik-Email      single value
    X-Authentik-Groups     pipe-separated ('Group A|Group B|...')

Parsed user info is stored on `flask.g.user`:
    g.user = {
        'username': 'jdoe',
        'email': 'jdoe@example.com',
        'groups': ['razzfazz.ai Super Admins', 'data-team'],
        'auth_method': 'authentik' | 'fallback_password',
    }

The fallback-password flow is opt-in (callers pass `fallback_password_env=`).
It compares the password from the `password=` form field on POST against
`os.environ[fallback_password_env]`. Successful auth sets a session flag
(`razzfazz_fallback_authenticated`) so subsequent GETs from the same
session don't re-prompt within `PERMANENT_SESSION_LIFETIME`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from functools import wraps
from typing import Callable

from flask import current_app, g, jsonify, request, session

# Session keys for the local-password fallback path. Namespaced so they
# don't collide with caller session data.
_FALLBACK_AUTH_KEY = 'razzfazz_fallback_authenticated'
_FALLBACK_AUTH_TIME_KEY = 'razzfazz_fallback_auth_time'
_FALLBACK_USERNAME_KEY = 'razzfazz_fallback_username'

# Public constant — the universal Super Admins group used everywhere in
# the stack. Callers can pass this in `require_groups=` for clarity.
SUPER_ADMINS_GROUP = 'razzfazz.ai Super Admins'


def parse_authentik_headers() -> dict:
    """Extract user info from the X-Authentik-* request headers.

    Returns a dict with keys `username`, `email`, `groups` (list[str]),
    and `uid` (str).
    Missing headers yield empty values — callers decide what to do
    (typically: deny if username is empty AND fallback isn't configured).
    """
    username = request.headers.get('X-Authentik-Username', '').strip()
    email = request.headers.get('X-Authentik-Email', '').strip()
    raw_groups = request.headers.get('X-Authentik-Groups', '')
    groups = [g.strip() for g in raw_groups.split('|') if g.strip()]
    # uid: X-Authentik-Uid, falling back to username (matches agent-manager's
    # prior behavior). Empty when there's no username at all.
    uid = request.headers.get('X-Authentik-Uid', '').strip() or username
    return {'username': username, 'email': email, 'groups': groups, 'uid': uid}


def _check_groups(user_groups: list[str], require_groups: Iterable[str]) -> bool:
    """Return True if the user is in any of the required groups, OR is
    in the Super Admins group (super-admin shortcut — they always pass).
    """
    if SUPER_ADMINS_GROUP in user_groups:
        return True
    required = set(require_groups)
    return bool(required.intersection(user_groups))


def _check_fallback_password(env_var: str) -> tuple[bool, str]:
    """Validate a POSTed `password` against $env_var. Returns (ok, error).

    On success the session is marked authenticated for `PERMANENT_SESSION_LIFETIME`.
    On failure, error contains a user-displayable message.
    """
    expected = os.environ.get(env_var, '')
    if not expected:
        return False, f'Local password auth not configured ({env_var} unset)'
    submitted = request.form.get('password', '')
    if submitted and submitted == expected:
        session[_FALLBACK_AUTH_KEY] = True
        session[_FALLBACK_AUTH_TIME_KEY] = time.time()
        session[_FALLBACK_USERNAME_KEY] = request.form.get('username', 'admin')
        session.permanent = True
        return True, ''
    return False, 'Invalid password'


def _fallback_session_valid() -> bool:
    """True if the current session has a non-expired fallback auth flag."""
    if not session.get(_FALLBACK_AUTH_KEY):
        return False
    auth_time = session.get(_FALLBACK_AUTH_TIME_KEY, 0)
    timeout = current_app.config.get('PERMANENT_SESSION_LIFETIME', 3600)
    if time.time() - auth_time > timeout:
        session.pop(_FALLBACK_AUTH_KEY, None)
        session.pop(_FALLBACK_AUTH_TIME_KEY, None)
        session.pop(_FALLBACK_USERNAME_KEY, None)
        return False
    return True


def require_authentik_auth(
    require_groups: Iterable[str] | None = None,
    fallback_password_env: str | None = None,
) -> Callable:
    """Route decorator factory. Returns 401/403 when the user can't be
    authenticated, otherwise sets `flask.g.user` and calls the wrapped view.

    Arguments:
        require_groups: optional iterable of group names. If set, the
            user must be in at least one of them (or in the Super Admins
            group, which always passes). 403 if not.
        fallback_password_env: optional env-var name. If set AND the
            Authentik headers are absent, fall back to a local password
            check against $env_var. POST with `password=...` to set;
            session sticky thereafter for PERMANENT_SESSION_LIFETIME
            seconds. 401 if not authenticated.

    The wrapped view sees:
        g.user = {'username': str, 'email': str, 'groups': list[str],
                  'auth_method': 'authentik' | 'fallback_password'}
    """
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args, **kwargs):
            parsed = parse_authentik_headers()

            if parsed['username']:
                # Authentik forward-auth path — headers populated.
                if require_groups and not _check_groups(parsed['groups'], require_groups):
                    return _forbidden('Insufficient group membership')
                g.user = {**parsed, 'auth_method': 'authentik'}
                return view(*args, **kwargs)

            # Authentik headers absent — try fallback password.
            if fallback_password_env:
                if _fallback_session_valid():
                    g.user = {
                        'username': session.get(_FALLBACK_USERNAME_KEY, 'admin'),
                        'email': '',
                        'groups': [SUPER_ADMINS_GROUP],  # local-pw is always admin
                        'auth_method': 'fallback_password',
                    }
                    return view(*args, **kwargs)
                # Not authenticated yet. If this is a POST with a
                # password field, attempt to authenticate inline.
                if request.method == 'POST' and 'password' in request.form:
                    ok, _err = _check_fallback_password(fallback_password_env)
                    if ok:
                        g.user = {
                            'username': session.get(_FALLBACK_USERNAME_KEY, 'admin'),
                            'email': '',
                            'groups': [SUPER_ADMINS_GROUP],
                            'auth_method': 'fallback_password',
                        }
                        return view(*args, **kwargs)
                return _unauthorized(
                    'Authentik headers missing and no valid fallback session. '
                    'POST password to authenticate.'
                )

            return _unauthorized('Authentik headers missing')
        return wrapper
    return decorator


def _unauthorized(msg: str):
    return jsonify({'status': 'error', 'message': msg}), 401


def _forbidden(msg: str):
    return jsonify({'status': 'error', 'message': msg}), 403
