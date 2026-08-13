# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Shared CSRF helpers (#148 — gap surfaced by S05 #4 razzfazz-backup-management).

Drop-in replacement for the F-049 helpers that backup-management kept
local because S03 didn't ship a CSRF module. Gives every UI container the
same `{{ csrf_token() }}` template helper + `_csrf_token` form-field +
`X-CSRF-Token` header verification, with one call:

    from razzfazz_common.csrf import enable_csrf
    enable_csrf(app)

What it wires:
- `@app.context_processor`: makes `csrf_token` callable from templates.
- `@app.before_request`: on POST, verifies the form/header token against
  the session-bound value and `abort(403)` on mismatch.
- The `/healthz` and `/static/` paths are exempt (consistent with
  `create_base_app`'s auth exemptions).

Token storage: Flask `session` (signed cookie). Tokens are 32 bytes of
hex (256 bits of entropy), generated lazily on first read.

Caller can opt out of POST-method verification (e.g. for an API-only
container that uses bearer-token auth instead) via `enable_csrf(app,
verify_methods=())`. Default verifies POST + PUT + PATCH + DELETE.
"""

from __future__ import annotations

import logging
import secrets
from typing import Iterable

from flask import Flask, abort, request, session

logger = logging.getLogger(__name__)

_DEFAULT_VERIFY_METHODS: tuple[str, ...] = ('POST', 'PUT', 'PATCH', 'DELETE')
_EXEMPT_PATHS: tuple[str, ...] = ('/healthz',)
_EXEMPT_PREFIXES: tuple[str, ...] = ('/static/',)


def get_csrf_token() -> str:
    """Generate (lazy) or return the session-bound CSRF token."""
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


def verify_csrf_token() -> None:
    """Verify request token against the session token. abort(403) on mismatch.

    Reads from `request.form['_csrf_token']` first (templates' `<input
    type="hidden" name="_csrf_token">`), then `X-CSRF-Token` header (for
    JS/fetch callers). Logs at WARNING on mismatch so the operator audit
    log captures the rejection.
    """
    token = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
    if not token or token != session.get('_csrf_token'):
        logger.warning(
            'CSRF token mismatch on %s %s', request.method, request.path
        )
        abort(403)


def enable_csrf(
    app: Flask,
    verify_methods: Iterable[str] = _DEFAULT_VERIFY_METHODS,
) -> None:
    """Wire CSRF into a Flask app.

    Registers a context_processor exposing `csrf_token` to templates and a
    before_request hook that runs `verify_csrf_token` on the configured
    HTTP methods (default: POST/PUT/PATCH/DELETE). The /healthz path and
    /static/* prefix are always exempt.
    """
    methods = {m.upper() for m in verify_methods}

    @app.context_processor
    def _inject_csrf_token():
        return {'csrf_token': get_csrf_token}

    @app.before_request
    def _csrf_protect():
        if request.method not in methods:
            return None
        if request.path in _EXEMPT_PATHS:
            return None
        if any(request.path.startswith(p) for p in _EXEMPT_PREFIXES):
            return None
        verify_csrf_token()
        return None
