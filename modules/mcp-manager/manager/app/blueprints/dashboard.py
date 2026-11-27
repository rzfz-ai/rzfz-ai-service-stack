# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""User-facing dashboard for the personal MCP manager (#36).

Renders the credential + instance management UI behind Authentik SSO. All data
shown is only-own: every query is scoped to the caller's sub (X-Authentik-Uid, #2472).
The page itself drives the JSON API (api.py) for mutations; the credential
forms POST plaintext over the SSO-gated TLS connection straight to
/api/credentials/<id>, which encrypts before persisting.
"""

from flask import Blueprint, current_app, render_template

from razzfazz_common.auth import parse_authentik_headers

from app.blueprints.api import _reconnect_notices, make_user_slug
from app.services.gateway_identity import legacy_slug_for, request_sub

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
def index():
    info = parse_authentik_headers()
    sub = request_sub()  # #2472: X-Authentik-Uid itself, never the username fallback
    if not info["username"] or not sub:
        return render_template("error.html",
                               message="Authentik authentication required."), 401
    user_slug = make_user_slug(info["username"])

    catalog = current_app.catalog.all()
    # undecided pre-2026.11 rows: visible only while no read-only token is
    # set (B6 fix rounds 1 and 2)
    legacy = legacy_slug_for(current_app, user_slug)
    instances = {i["mcp_id"]: dict(i)
                 for i in current_app.db.get_user_instances(sub, legacy_slug=legacy)}
    cred_summary = {
        m["id"]: current_app.cred_store.credential_summary(sub, m["id"], legacy_slug=legacy)
        for m in catalog
    }
    # #3424: a card whose engine module is off, or whose OAuth provider is not
    # set up, says so and offers no connect (oauth.py refuses it too).
    from app.services import card_state
    profiles = card_state.active_profiles()
    flow = getattr(current_app, "oauth_flow", None)
    unavailable = {m["id"]: card_state.unavailable_reason(current_app.catalog, flow, m["id"], profiles)
                   for m in catalog}
    return render_template(
        "dashboard/index.html",
        connected_notice=_connected_notice(),
        username=info["username"],
        user_email=info["email"],
        catalog=catalog,
        instances=instances,
        cred_summary=cred_summary,
        unavailable=unavailable,
        reconnect_notices=(_reconnect_notices(user_slug) + _openbao_reconnect_notices(sub, legacy, catalog)
                           + _m365_write_notice(sub, legacy)),
        is_admin=_is_admin(info["groups"]),
    )


#: #2472 B22: a pre-2026.11 OAuth connection is not carried into OpenBao.
OPENBAO_RECONNECT_NOTICE = ("Your {integration} connection from before this upgrade is not carried over. "
                            "Connect {integration} again once.")


def _openbao_reconnect_notices(sub, legacy, catalog) -> list:
    out = []
    for m in catalog:
        try:
            if current_app.cred_store.reconnect_needed(sub, m["id"], legacy_slug=legacy):
                name = m.get("display_name") or m["id"]
                out.append({"mcp_id": m["id"], "message": OPENBAO_RECONNECT_NOTICE.format(integration=name)})
        except Exception:  # noqa: BLE001 — a notice must never break the page
            continue
    return out


#: #3433: read-write is on, and this person's Microsoft 365 grant is still the
#: read-only one (or lacks Mail.Send while sending is on).
M365_WRITE_NOTICE = ("Microsoft 365 can write and send on this box: reconnect Microsoft 365 to grant write "
                     "access. Until then your connection keeps working with what it has.")


def _m365_write_notice(sub, legacy) -> list:
    from app.services import m365_guard
    try:
        settings = m365_guard.settings()
        if not settings.read_write or current_app.catalog.get("m365") is None:
            return []
        creds = current_app.cred_store.get_decrypted(sub, "m365", legacy_slug=legacy) or {}
        access = creds.get("oauth_access")
        if not access:
            return []
        scopes = m365_guard.granted_scopes({"Authorization": "Bearer " + access})
        if m365_guard.needs_write_reconnect(scopes, settings):
            return [{"mcp_id": "m365", "message": M365_WRITE_NOTICE}]
    except Exception:  # noqa: BLE001 — a notice must never break the page
        return []
    return []


def _connected_notice() -> str:
    """#2554: the notice after an OAuth authorization returned here
    (`?connected=<id>`). Only a catalogue id is named, by its display name;
    anything else in the query string is never echoed."""
    from flask import request
    mcp_id = request.args.get("connected", "")
    integ = current_app.catalog.get(mcp_id) if mcp_id else None
    if not isinstance(integ, dict):
        return ""
    name = integ.get("display_name") or mcp_id
    note = f"{name} is connected. Your agents and chats use it as you."
    if request.args.get("restarted") == "1":
        note += " Your running proxy was restarted with the new authorization."
    return note


def _is_admin(groups):
    from app.services.tier import is_mcp_admin
    return is_mcp_admin(groups)


@dashboard_bp.route("/admin/identity")
def identity_admin():
    """#2472 (design record §2.3): the admin's list of quarantined MCP rows
    and of credentials still unattributed 30 days after the migration, with
    the admin-only purge. Owner hint = the former slug; never a value, never
    ciphertext. platform-admin / authentik-admin only (A1 D3)."""
    from app.services.tier import is_mcp_admin
    info = parse_authentik_headers()
    if not info["username"] or not request_sub():
        return render_template("error.html",
                               message="Authentik authentication required."), 401
    if not is_mcp_admin(info["groups"]):
        return render_template("error.html", message="Administrators only."), 403
    return render_template(
        "dashboard/identity_admin.html",
        username=info["username"],
        quarantined=current_app.db.list_quarantine(),
        unattributed=current_app.db.list_unattributed_credentials(30),
        two_memories=current_app.db.cognee_dual_memories(),
    )
