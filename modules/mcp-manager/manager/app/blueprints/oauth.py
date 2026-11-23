# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""OAuth start + callback routes for personal MCP integrations (#36, P3).

Both routes sit behind Authentik forward-auth (Caddy), so the caller's identity
is the X-Authentik-* headers. /start mints a signed+expiring state bound to the
authenticated user; /callback verifies it (rejecting forged/expired/cross-user),
exchanges the code for tokens server-side, and stores them encrypted.

Real external-provider handshake needs the operator's real client credentials
on a test box — until a provider's client_id/secret are configured the start route
returns a clear "not configured" message.
"""

import logging
from urllib.parse import quote

from flask import Blueprint, current_app, jsonify, redirect, request

from razzfazz_common.auth import parse_authentik_headers

from app.blueprints.api import make_user_slug
from app.services.gateway_identity import request_sub
from app.services.ssrf_guard import SSRFError

logger = logging.getLogger(__name__)

oauth_bp = Blueprint("oauth", __name__, url_prefix="/oauth")


def _user():
    """(sub, slug). #2472: the state and the stored tokens are bound to the
    sub read from X-Authentik-Uid; no uid, no flow."""
    info = parse_authentik_headers()
    sub = request_sub()
    if not info["username"] or not sub:
        return None, None
    return sub, make_user_slug(info["username"])


def _redirect_uri(provider: str) -> str:
    domain = current_app.config.get("MCP_DOMAIN", "")
    return f"https://{domain}/oauth/callback/{provider}"


@oauth_bp.route("/start/<mcp_id>", methods=["GET", "POST"])
def start(mcp_id):
    owner, user_slug = _user()
    if not owner:
        return jsonify({"error": "Unauthorized"}), 403

    integ = current_app.catalog.get(mcp_id)
    if not integ or current_app.catalog.cred_model(mcp_id) != "oauth":
        return jsonify({"error": "Integration is not an OAuth integration"}), 400

    provider = current_app.catalog.oauth_provider(mcp_id)
    flow = current_app.oauth_flow
    # #2472: an endpoint left empty at load (an unset or refused
    # ${M365_TENANT_ID}) is "not configured" too, not a
    # rejected URL.
    if (not flow or provider not in flow._providers
            or not flow._providers[provider].get("client_id")
            or not flow._providers[provider].get("auth_url")):
        return jsonify({
            "error": "oauth_not_configured",
            "message": (f"OAuth provider '{provider}' is not configured on this box. "
                        f"The operator must register an OAuth app and set client_id "
                        f"in core/mcp/oauth-providers.yaml + the client secret in .env."),
        }), 503

    state = flow.make_state(owner, mcp_id, provider)
    try:
        url = flow.authorize_url(provider, mcp_id, state, _redirect_uri(provider))
    except SSRFError as e:
        # #67: the provider's auth_url failed the https-only / host-allowlist
        # SSRF check — refuse to redirect the browser at it. Fail closed.
        logger.warning("oauth start blocked for %s/%s: %s", user_slug, provider, e)
        current_app.db.log_audit(owner, "oauth_start_blocked_ssrf", mcp_id,
                                 {"provider": provider})
        return jsonify({"error": "provider_url_rejected", "message": str(e)}), 502
    current_app.db.log_audit(owner, "oauth_start", mcp_id, {"provider": provider})
    return redirect(url)


@oauth_bp.route("/callback/<provider>", methods=["GET"])
def callback(provider):
    owner, user_slug = _user()
    if not owner:
        return jsonify({"error": "Unauthorized"}), 403

    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not state or not code:
        return jsonify({"error": "Missing state/code"}), 400

    flow = current_app.oauth_flow
    try:
        payload = flow.verify_state(state, expected_user_slug=owner)
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
        current_app.db.log_audit(owner, "oauth_exchange_blocked_ssrf", mcp_id,
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

    from app.services.credential_store import CredentialStoreError
    try:
        current_app.cred_store.store_oauth_tokens(
            owner, mcp_id, access=access, refresh=refresh, expires_at=expires_at,
            user_slug=user_slug)
    except CredentialStoreError as e:
        # #2472 B22: OpenBao is where this grant must live and it is not
        # reachable: refused, never kept anywhere else, no token in the body.
        logger.error("oauth grant for %s/%s not stored: %s", user_slug, mcp_id, e)
        return jsonify({"error": "credential_store_unavailable",
                        "message": "The connection could not be saved. Try again later."}), 500
    restarted = _restart_running_proxy(owner, user_slug, mcp_id)
    if _wants_html():
        # #2554: a browser returns to the MCP page, which says it worked; an
        # API client keeps the JSON answer.
        return redirect("/?connected=" + quote(mcp_id, safe="")
                        + ("&restarted=1" if restarted else ""), code=303)
    # NEVER include the tokens in the response.
    return jsonify({"status": "authorized", "mcp_id": mcp_id, "restarted": restarted})


def _wants_html() -> bool:
    """A browser navigation (Accept prefers text/html over JSON). A client
    that names no type, or `*/*`, gets JSON as before."""
    accept = request.accept_mimetypes
    return accept.best_match(["application/json", "text/html"]) == "text/html" \
        and accept["text/html"] > accept["application/json"]


def _restart_running_proxy(owner: str, user_slug: str, mcp_id: str) -> bool:
    """B9 fix round 1 (I2): the proxy holds the token it was started with, so
    after a successful (re)connect the caller's OWN running proxy of this
    integration is started again with the new token. The same governance
    gate as the portal's Start applies. A failure is logged and never fails
    the callback (the tokens are stored; "Start" recovers)."""
    from app.services import governance
    from app.services.gateway_identity import legacy_slug_for
    try:
        legacy = legacy_slug_for(current_app, user_slug)
        inst = current_app.db.get_instance_by_mcp_and_user(mcp_id, owner, legacy_slug=legacy)
        if not inst or inst.get("state") != "running":
            return False
        info = parse_authentik_headers()
        allowed, _reason = governance.user_can_provision(
            current_app.db, mcp_id, info["groups"], catalog=current_app.catalog)
        if not allowed:
            return False
        iid, msg = current_app.provisioner.launch(
            mcp_id, owner, info["username"], info["groups"], legacy_slug=legacy, restart=True)
        # `restarted` means the container really came back up: launch also
        # returns an id for a launch that failed (its row is then "error").
        after = current_app.db.get_instance_by_mcp_and_user(mcp_id, owner, legacy_slug=legacy)
        up = bool(iid) and bool(after) and after.get("state") == "running" \
            and str(after.get("id")) == str(iid)
        if not up:
            logger.warning("oauth callback: %s proxy not restarted: %s", mcp_id, msg)
        return up
    except Exception:  # noqa: BLE001
        logger.exception("oauth callback: restarting the %s proxy failed", mcp_id)
        return False
