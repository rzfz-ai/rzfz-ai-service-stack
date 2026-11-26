# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The person's confirmation page for a Microsoft 365 send or delete (#3433).

GET  /confirm/<id>  what the call will do, and a Confirm button
POST /confirm/<id>  confirm it (CSRF token, same origin only)

On mcp.<domain>, behind Authentik forward-auth and Caddy-anchored like the
portal: the identity is X-Authentik-Uid as Caddy delivered it, and only the
person the approval belongs to sees or confirms it. A gateway token, an Open
WebUI session or an agent's credential is never an identity here, so a model
cannot confirm its own call (app/services/m365_confirm.py). GET never
confirms anything."""
from __future__ import annotations

import hmac
import logging
from urllib.parse import urlsplit

from flask import Blueprint, current_app, render_template, request

from app.services import m365_confirm
from app.services.gateway_identity import request_sub

logger = logging.getLogger(__name__)

m365_confirm_bp = Blueprint("m365_confirm", __name__)

_GONE = ("This confirmation is not available: it was already used, it expired "
         "(after 10 minutes), or it belongs to someone else. Ask in chat again.")


def _page(status=200, **ctx):
    resp = current_app.make_response((render_template("m365_confirm.html", **ctx), status))
    # Never framed: a page that runs a send must not sit under someone
    # else's overlay.
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _same_origin() -> bool:
    """A browser POST from this very site. Origin when the browser sends it
    (all current ones do on POST), else Sec-Fetch-Site; neither = refused."""
    domain = (current_app.config.get("MCP_DOMAIN") or "").lower()
    origin = request.headers.get("Origin")
    if origin is not None:
        parts = urlsplit(origin)
        return parts.scheme == "https" and (parts.netloc or "").lower() == domain
    return (request.headers.get("Sec-Fetch-Site") or "") == "same-origin"


def _secret() -> str:
    return str(current_app.secret_key or "")


@m365_confirm_bp.route("/confirm/<aid>", methods=["GET"])
def show(aid):
    sub = request_sub()
    if not sub:
        return _page(401, error="Authentik authentication required.")
    a = m365_confirm.store(current_app).pending_for(aid, sub)
    if a is None:
        return _page(404, error=_GONE)
    return _page(tool=a.tool, tool_class=a.tool_class, preview=a.preview, aid=a.id,
                 csrf=m365_confirm.csrf_token(_secret(), a.id, sub, a.view_digest))


@m365_confirm_bp.route("/confirm/<aid>", methods=["POST"])
def confirm(aid):
    sub = request_sub()
    if not sub:
        return _page(401, error="Authentik authentication required.")
    if not _same_origin():
        return _page(403, error="This confirmation must be given on this page.")
    token = request.form.get("csrf", "")
    st = m365_confirm.store(current_app)
    a = st.pending_for(aid, sub)
    if a is None:
        return _page(404, error=_GONE)
    # The token is bound to what the page showed: recomputed from the
    # approval's preview now, so a page showing anything else cannot confirm.
    want = m365_confirm.csrf_token(_secret(), aid, sub, m365_confirm.view_digest(a.preview))
    if not token or not hmac.compare_digest(token, want):
        return _page(403, error="This confirmation must be given on the page that shows it. Reload it.")
    if not st.approve(aid, sub):
        return _page(404, error=_GONE)
    try:
        current_app.db.log_audit(sub, "gw_m365_confirmation", "m365", {
            "tool": a.tool, "tool_class": a.tool_class, "caller_class": a.who,
            "outcome": "confirmation_given"})
    except Exception:  # noqa: BLE001 — the relay writes its own row before it dials
        logger.exception("m365 confirm: audit row failed")
    from app.services import m365_audit
    m365_audit.write(sub, a.tool, a.tool_class, a.who, "confirmation_given", {})
    return _page(done=True, tool=a.tool, tool_class=a.tool_class,
                 minutes=int(m365_confirm.TTL_SECONDS // 60))

