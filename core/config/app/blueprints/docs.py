# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Documentation blueprint — sends the user to the standalone Help Center.

History (#171): this used to server-side *proxy* the razzfazz-help container into
the Config UI and rewrite its links. That was fundamentally fragile — it dropped
the X-Authentik-* identity headers (every page rendered "anonymous"), its naive
href/src `/`->`/docs/` rewrite double-prefixed the help center's own `/docs/`
paths (a direct `/docs/gitea/` resolved to help `/gitea/` -> 404), and it mangled
the asset URLs of the externally-mirrored doc sites (gitea/dify/etc. rendered
unstyled/broken). Own-docs pages happened to work; external mirrors did not.

The Help Center (razzfazz-help at help.<domain>) is a full, Authentik-gated app
that works perfectly when opened directly. So we simply redirect there: the
browser carries the Authentik SSO session, so the user stays signed in, and all
CSS/JS/links + per-module routing resolve natively against help.<domain>.
"""
import logging
from flask import Blueprint, current_app, redirect, render_template

logger = logging.getLogger(__name__)

docs_bp = Blueprint('docs', __name__, url_prefix='/docs')


def _help_domain():
    try:
        return current_app.config_manager.read_env().get('HELP_DOMAIN', '').strip()
    except Exception:
        logger.exception('could not read HELP_DOMAIN')
        return ''


@docs_bp.route('/')
@docs_bp.route('/<path:path>')
def index(path=''):
    """Redirect into the standalone Help Center (deep-linking module pages)."""
    domain = _help_domain()
    if not domain:
        return render_template(
            'docs/error.html',
            message='Help Center is not configured (HELP_DOMAIN is unset).')
    if path:
        # Map a module id (and any legacy double-prefixed /docs/docs/<id>) onto
        # the Help Center's /docs/<id>/ structure.
        p = path[5:] if path.startswith('docs/') else path
        return redirect(f'https://{domain}/docs/{p.strip("/")}/')
    return redirect(f'https://{domain}/')
