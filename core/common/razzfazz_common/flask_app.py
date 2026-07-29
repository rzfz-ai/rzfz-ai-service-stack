# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Shared Flask app factory.

Builds a Flask app pre-wired with the things every razzfazz UI container
needs: session security, /healthz, the version + brand context processor,
and (opt-out) a global Authentik-header check.

Containers still register their own blueprints + service objects on the
returned app — this only handles the cross-container scaffolding.
"""

from __future__ import annotations

import os
from typing import Any

from flask import Blueprint, Flask, g, jsonify, request
from jinja2 import ChoiceLoader, FileSystemLoader

from .auth import parse_authentik_headers
from .health import get_health_blueprint
from .session_config import configure_session
from .version import register_context_processor

# M026 S06: shared CSS / static dir lives next to this module. We use
# `os.path.dirname(__file__)/static` instead of importlib.resources because
# Flask's Blueprint API expects a filesystem path; the wheel ships the
# directory as package data (see core/common/pyproject.toml).
_SHARED_STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')

# URL prefix chosen so it cannot collide with a container's own /static/
# tree (Flask's per-app static endpoint stays as /static/<file>; the
# shared CSS lives one level deeper at /static/razzfazz-common/<file>).
_SHARED_STATIC_URL_PATH = '/static/razzfazz-common'

# M026 S08: shared Jinja templates (razz_base.html and friends) live next
# to this module. Wired into the app's loader as a ChoiceLoader fallback
# so per-container templates always win, and only un-resolved names fall
# through to the shared shell.
_SHARED_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def create_base_app(
    name: str,
    secret_key_env: str = 'WEBUI_SECRET_KEY',
    *,
    service_name: str | None = None,
    cookie_name: str | None = None,
    require_auth: bool = True,
    audit_logging: bool = False,  # reserved; per-container audit wiring lands in S05
    static_folder: str | None = None,
    template_folder: str | None = None,
    context_extras=None,
    **flask_kwargs: Any,
) -> Flask:
    """Create a Flask app with the razzfazz baseline wired in.

    Arguments:
        name: Flask import-name (typically `__name__` from caller).
        secret_key_env: env-var to read the session secret from. See
            `session_config.configure_session` for the full lookup chain.
        service_name: human-readable name echoed in /healthz JSON.
            Defaults to `name` (the Flask import-name). Override when
            the import-name is generic (e.g. `app` under gunicorn) so
            operators can disambiguate which container responded.
        cookie_name (#148): when set, overrides Flask's default
            `session` cookie name. Pass each UI's distinct value (e.g.
            `'razzfazz_config'`, `'razzfazz_setup'`) to keep cookies
            from colliding across subdomains in the same browser session.
        context_extras (#148): optional callable returning a dict of
            extra template vars to merge into every render. Lets
            containers add nav-data / module counts / etc. without
            registering a second `@app.context_processor`. The shared
            `register_context_processor` hands this through.
        require_auth: when True (default), every request must carry the
            Authentik forward-auth headers (X-Authentik-Username). The
            `/healthz`, `/static/`, and `/auth/` paths are exempt.
            Containers that need finer-grained auth (per-route group
            checks, fallback password) should pass `require_auth=False`
            and use the `@require_authentik_auth(...)` decorator on the
            specific routes instead.
        audit_logging: reserved flag for S05 audit wiring. Currently a
            no-op — the per-container audit logger is wired by callers.
        static_folder, template_folder: passed through to Flask. Leave
            unset to use Flask's defaults (static/, templates/ relative
            to the caller's package). The shared package itself does NOT
            ship templates or CSS in S03 — those land in S06/S08.
        **flask_kwargs: forwarded to the Flask constructor.

    Returns:
        A configured Flask app. Caller registers blueprints + services.
    """
    app_kwargs: dict[str, Any] = dict(flask_kwargs)
    if static_folder is not None:
        app_kwargs['static_folder'] = static_folder
    if template_folder is not None:
        app_kwargs['template_folder'] = template_folder

    app = Flask(name, **app_kwargs)

    # M026 S08: layer the shared template dir under the caller's loader via
    # a ChoiceLoader. The original (per-container) loader stays first so
    # local names always win; only un-resolved names (e.g. `razz_base.html`)
    # fall through to the shared shell. Guarded with isdir() so the factory
    # still works if the templates dir hasn't shipped yet — same defensive
    # pattern used by the shared static blueprint above.
    if os.path.isdir(_SHARED_TEMPLATE_DIR) and app.jinja_loader is not None:
        app.jinja_loader = ChoiceLoader([
            app.jinja_loader,
            FileSystemLoader(_SHARED_TEMPLATE_DIR),
        ])

    # Session security + secret key (cookie flags + 1h lifetime).
    configure_session(app, secret_key_env=secret_key_env, cookie_name=cookie_name)

    # /healthz blueprint — must be registered BEFORE the auth middleware
    # is wired so the exemption check ordering is consistent.
    app.register_blueprint(get_health_blueprint(service_name or name))

    # M026 S06: shared static blueprint serves razzfazz-base.css (and any
    # future shared assets) at /static/razzfazz-common/<file>. Registered
    # before the auth middleware so the /static/ exemption naturally covers
    # it. The per-container /static/ endpoint is unaffected — Flask routes
    # exact paths first, then falls back to blueprint-mounted prefixes.
    if os.path.isdir(_SHARED_STATIC_DIR):
        shared_static_bp = Blueprint(
            'razzfazz_common_static',
            __name__,
            static_folder=_SHARED_STATIC_DIR,
            static_url_path=_SHARED_STATIC_URL_PATH,
        )
        app.register_blueprint(shared_static_bp)

    # Template context: razzfazz_version, stack_version, main_domain,
    # brand_color, plus any caller-supplied extras (#148).
    register_context_processor(app, extras=context_extras)

    # Cheap header parsing into flask.g for templates / view code.
    @app.before_request
    def _populate_user_context():
        parsed = parse_authentik_headers()
        # Always populate so `g.user` is safe to access in templates.
        g.user = {**parsed, 'auth_method': 'authentik' if parsed['username'] else None}

    if require_auth:
        @app.before_request
        def _enforce_authentik_headers():
            # Exempt: health checks, static assets, and the local auth
            # blueprint (per-container login pages live there).
            path = request.path
            if path == '/healthz' or path.startswith(('/static/', '/auth/')):
                return None
            if not g.user.get('username'):
                # M026 #148 (last gap): negotiate response shape from the
                # request's Accept header. JSON callers (XHR/fetch from the
                # SPA) get the canonical envelope `{"status":"error",
                # "message":"..."}` matching what the pre-S05 backup
                # decorator + the per-route @require_authentik_auth helper
                # both return. Browser callers (text/html) keep the plain
                # text body so the 401 page renders naturally rather than
                # dumping JSON into the page. Status code stays 401 either
                # way (the auth decorator returns 403 for group-mismatch;
                # this is the no-auth-headers-at-all case).
                if request.accept_mimetypes.best == 'application/json' or \
                        request.is_json:
                    return jsonify({
                        'status': 'error',
                        'message': 'Unauthorized: Authentik headers missing',
                    }), 401
                return ('Unauthorized: Authentik headers missing', 401)
            return None

    return app
