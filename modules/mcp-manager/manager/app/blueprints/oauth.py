# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""OAuth start + callback routes for personal MCP integrations (#36, P3).

Both routes sit behind Authentik forward-auth (Caddy), so the caller's identity
is the X-Authentik-* headers. /start mints a signed+expiring state bound to the
authenticated user; /callback verifies it (rejecting forged/expired/cross-user),
exchanges the code for tokens server-side, and stores them encrypted.

Real external-provider handshake needs the operator's real client credentials
on 0.91 — until a provider's client_id/secret are configured the start route
returns a clear "not configured" message.
"""

import logging

from flask import Blueprint, current_app, jsonify, redirect, request

from razzfazz_common.auth import parse_authentik_headers

from app.blueprints.api import make_user_slug
from app.services.ssrf_guard import SSRFError

logger = logging.getLogger(__name__)

oauth_bp = Blueprint("oauth", __name__, url_prefix="/oauth")


def _user():
    info = parse_authentik_headers()
    if not info["username"]:
        return None, None
    return make_user_slug(info["username"]), info["email"]


def _redirect_uri(provider: str) -> str:
    domain = current_app.config.get("MCP_DOMAIN", "")
    return f"https://{domain}/oauth/callback/{provider}"


@oauth_bp.route("/start/<mcp_id>", methods=["GET", "POST"])
def start(mcp_id):
    user_slug, _ = _user()
    if not user_slug:
        return jsonify({"error": "Unauthorized"}), 403

    integ = current_app.catalog.get(mcp_id)
    if not integ or current_app.catalog.cred_model(mcp_id) != "oauth":
        return jsonify({"error": "Integration is not an OAuth integration"}), 400

    provider = current_app.catalog.oauth_provider(mcp_id)
    flow = current_app.oauth_flow
    if not flow or provider not in flow._providers or not flow._providers[provider].get("client_id"):
        return jsonify({
            "error": "oauth_not_configured",
            "message": (f"OAuth provider '{provider}' is not configured on this box. "
                        f"The operator must register an OAuth app and set client_id "
                        f"in core/mcp/oauth-providers.yaml + the client secret in .env."),
        }), 503

    state = flow.make_state(user_slug, mcp_id, provider)
    try:
        url = flow.authorize_url(provider, mcp_id, state, _redirect_uri(provider))
    except SSRFError as e:
        # #67: the provider's auth_url failed the https-only / host-allowlist
        # SSRF check — refuse to redirect the browser at it. Fail closed.
        logger.warning("oauth start blocked for %s/%s: %s", user_slug, provider, e)
        current_app.db.log_audit(user_slug, "oauth_start_blocked_ssrf", mcp_id,
                                 {"provider": provider})
        return jsonify({"error": "provider_url_rejected", "message": str(e)}), 502
    current_app.db.log_audit(user_slug, "oauth_start", mcp_id, {"provider": provider})
    return redirect(url)


@oauth_bp.route("/callback/<provider>", methods=["GET"])
def callback(provider):
    user_slug, _ = _user()
    if not user_slug:
        return jsonify({"error": "Unauthorized"}), 403

    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not state or not code:
        return jsonify({"error": "Missing state/code"}), 400

    flow = current_app.oauth_flow
    try:
        payload = flow.verify_state(state, expected_user_slug=user_slug)
    except ValueError as e:
        # fail-closed on forged/expired/cross-user state
        logger.warning("oauth callback rejected for %s: %s", user_slug, e)
        return jsonify({"error": "invalid_state", "message": str(e)}), 400

    if payload.get("provider") != provider:
        return jsonify({"error": "provider_mismatch"}), 400

    mcp_id = payload["mcp_id"]
    try:
        tokens = flow.exchange_code(provider, code, _redirect_uri(provider))
    except SSRFError as e:
        # #67: token_url failed the SSRF check — never POST the client secret +
        # auth code at a non-allowlisted / non-https host.
        logger.warning("oauth exchange blocked for %s/%s: %s", user_slug, provider, e)
        current_app.db.log_audit(user_slug, "oauth_exchange_blocked_ssrf", mcp_id,
                                 {"provider": provider})
        return jsonify({"error": "provider_url_rejected", "message": str(e)}), 502
    except Exception as e:
        logger.error("oauth exchange failed for %s/%s: %s", user_slug, provider, e)
        return jsonify({"error": "exchange_failed"}), 502

    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in")
    expires_at = None
    if expires_in:
        from datetime import datetime, timedelta, timezone
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    if not access:
        return jsonify({"error": "no_access_token"}), 502

    current_app.cred_store.store_oauth_tokens(
        user_slug, mcp_id, access=access, refresh=refresh, expires_at=expires_at)
    # NEVER include the tokens in the response.
    return jsonify({"status": "authorized", "mcp_id": mcp_id})
