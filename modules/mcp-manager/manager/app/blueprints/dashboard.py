# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""User-facing dashboard for the personal MCP manager (#36).

Renders the credential + instance management UI behind Authentik SSO. All data
shown is only-own: every query is scoped to the caller's server-derived slug.
The page itself drives the JSON API (api.py) for mutations; the credential
forms POST plaintext over the SSO-gated TLS connection straight to
/api/credentials/<id>, which encrypts before persisting.
"""

from flask import Blueprint, current_app, render_template

from razzfazz_common.auth import parse_authentik_headers

from app.blueprints.api import make_user_slug

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
def index():
    info = parse_authentik_headers()
    if not info["username"]:
        return render_template("error.html",
                               message="Authentik authentication required."), 401
    user_slug = make_user_slug(info["username"])

    catalog = current_app.catalog.all()
    instances = {i["mcp_id"]: dict(i) for i in current_app.db.get_user_instances(user_slug)}
    cred_summary = {
        m["id"]: current_app.cred_store.credential_summary(user_slug, m["id"])
        for m in catalog
    }
    return render_template(
        "dashboard/index.html",
        username=info["username"],
        user_email=info["email"],
        catalog=catalog,
        instances=instances,
        cred_summary=cred_summary,
    )
