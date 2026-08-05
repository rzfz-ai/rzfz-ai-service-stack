# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Shared Flask session-security config.

Separable from `flask_app.create_base_app` so callers that build their
Flask app the long way can still pick up the canonical session flags +
the documented secret-key resolution chain.
"""

from __future__ import annotations

import os
import secrets

from flask import Flask


def configure_session(
    app: Flask,
    secret_key_env: str = 'WEBUI_SECRET_KEY',
    cookie_name: str | None = None,
) -> None:
    """Apply session-security flags + resolve the secret key.

    Cookie flags (matches all 5 existing UI containers):
        SESSION_COOKIE_SECURE   = True   (Caddy serves all UIs over HTTPS)
        SESSION_COOKIE_HTTPONLY = True
        SESSION_COOKIE_SAMESITE = 'Lax'
        PERMANENT_SESSION_LIFETIME = 3600  (1 hour, matches admin login timeout)

    Secret key lookup chain:
        1. os.environ[secret_key_env]            (caller-specified)
        2. os.environ['CONFIG_SECRET_KEY']       (Config UI legacy)
        3. os.environ['WEBUI_SECRET_KEY']        (stack-wide shared secret)
        4. secrets.token_hex(32)                  (random per-process fallback)

    The random fallback means a misconfigured container still boots, but
    sessions are invalidated on every restart — operators see the
    breakage on the next page reload, not silently.

    `cookie_name` (#148): when set, overrides Flask's default `session`
    cookie name. Each UI container should pass its own (e.g.
    `'razzfazz_config'`, `'razzfazz_setup'`) so cookies don't collide
    across subdomains served by the same browser session. When None,
    Flask's default `session` is used.
    """
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['PERMANENT_SESSION_LIFETIME'] = 3600
    if cookie_name:
        app.config['SESSION_COOKIE_NAME'] = cookie_name

    key = (
        os.environ.get(secret_key_env)
        or os.environ.get('CONFIG_SECRET_KEY')
        or os.environ.get('WEBUI_SECRET_KEY')
        or secrets.token_hex(32)
    )
    app.secret_key = key
