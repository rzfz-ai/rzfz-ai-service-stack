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
import re
from urllib.parse import quote

from flask import Blueprint, current_app, jsonify, redirect, request

from razzfazz_common.auth import parse_authentik_headers

from app.blueprints.api import make_user_slug
from app.services import card_state, remote_mcp
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
    # #3424: a card whose engine module is off cannot be connected, whatever
    # the page shows: refused here, before any state is minted.
    refused = _refuse_unavailable(mcp_id, provider) or _refuse_governance(owner, mcp_id, "start")
    if refused:
        return refused
    spec = remote_mcp.spec_for(integ)
    if spec is not None:
        # #3429: no app to set up — the box registers itself on first use.
        return _start_remote(spec, owner, user_slug, provider, flow)
    # #2472: an endpoint left empty at load (an unset or refused
    # ${M365_TENANT_ID}) is "not configured" too, not a rejected URL.
    # #3424: so is an empty client secret — the exchange would fail after
    # the person consented.
    if not flow or flow.setup_gaps(provider):
        return jsonify({
            "error": "oauth_not_configured",
            "message": card_state.admin_hint(current_app.catalog, flow, mcp_id,
                                             _redirect_uri(provider)),
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
    error = request.args.get("error", "")
    if not state or not (code or error):
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
    integ = current_app.catalog.get(mcp_id)
    spec = remote_mcp.spec_for(integ)
    if error:
        # #3429: the provider sent the person back without a code (consent
        # denied, request refused): said in plain words, nothing stored.
        name = (spec.display if spec else (integ or {}).get("display_name")) or mcp_id
        # The provider's error code is recorded only in its RFC 6749 shape.
        seen = error if re.fullmatch(r"[a-z_]{1,48}", error) else "unrecognised"
        current_app.db.log_audit(owner, "oauth_callback_error", mcp_id,
                                 {"provider": provider, "error": seen})
        if spec is not None and getattr(flow, "remote", None) is not None:
            flow.remote.discard_pending(spec, owner, payload.get("nonce", ""))
        return _plain_error(400, "authorization_not_granted",
                            remote_mcp.callback_error_message(name, seen))
    # #3424: the engine was switched off between start and callback: no
    # exchange, nothing stored. #3477: the same for the Settings page's
    # availability and minimum tier.
    refused = _refuse_unavailable(mcp_id, provider) or _refuse_governance(owner, mcp_id, "callback")
    if refused:
        if spec is not None and getattr(flow, "remote", None) is not None:
            flow.remote.discard_pending(spec, owner, payload.get("nonce", ""))
        return refused
    if spec is not None:
        return _callback_remote(spec, owner, user_slug, provider, payload, code)
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

    return _store_and_finish(owner, user_slug, mcp_id, tokens)


def _store_and_finish(owner, user_slug, mcp_id, tokens):
    """Keep the person's new grant and send them back (shared by both flows)."""
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


def _plain_error(status: int, code: str, message: str):
    """#3429: an error the person reads: a short page for a browser, JSON
    for an API client. `message` is ours (never a provider's raw text)."""
    if _wants_html():
        from html import escape
        page = ("<!doctype html><meta charset=utf-8><title>Not connected</title>"
                f"<p>{escape(message)}</p><p><a href=\"/\">Back to the MCP integrations</a></p>")
        return current_app.response_class(page, status=status, mimetype="text/html")
    return jsonify({"error": code, "message": message}), status


def _start_remote(spec, owner, user_slug, provider, flow):
    """#3429: a remote MCP server by URL — the box registers itself at the
    provider on first use (no app, no operator secret), then the person
    authorizes with PKCE."""
    if not flow or getattr(flow, "remote", None) is None:
        return _plain_error(503, "oauth_not_configured",
                            "Connections are not available on this box right now.")
    nonce = remote_mcp.new_nonce()
    state = flow.make_state(owner, spec.mcp_id, provider, nonce=nonce)
    try:
        url = flow.remote.authorize_url(spec, owner, nonce, state, _redirect_uri(provider))
    except remote_mcp.RemoteMcpError as e:
        current_app.db.log_audit(owner, "oauth_start_failed", spec.mcp_id,
                                 {"provider": provider, "reason": e.code})
        return _plain_error(502, e.code, e.message)
    current_app.db.log_audit(owner, "oauth_start", spec.mcp_id, {"provider": provider,
                                                                 "kind": "remote_mcp"})
    return redirect(url)


def _callback_remote(spec, owner, user_slug, provider, payload, code):
    try:
        tokens = current_app.oauth_flow.remote.exchange(
            spec, owner, payload.get("nonce", ""), code, _redirect_uri(provider),
            iss=request.args.get("iss"))
    except remote_mcp.RemoteMcpError as e:
        logger.warning("remote MCP exchange failed for %s/%s: %s", user_slug, spec.mcp_id, e.code)
        current_app.db.log_audit(owner, "oauth_exchange_failed", spec.mcp_id,
                                 {"provider": provider, "reason": e.code})
        return _plain_error(502, e.code, e.message)
    return _store_and_finish(owner, user_slug, spec.mcp_id, tokens)


def _refuse_governance(owner: str, mcp_id: str, stage: str):
    """#3477 (A2-01): the answer when the Settings page's availability or
    minimum tier stops this person connecting `mcp_id` (audited as refused),
    else None. An unreadable governance row refuses too (503)."""
    from app.services import governance
    try:
        reason = governance.person_refusal(current_app.db, mcp_id, parse_authentik_headers()["groups"])
    except Exception:  # noqa: BLE001 — undecidable: nothing is started or stored
        logger.exception("oauth %s: governance for %s could not be read", stage, mcp_id)
        return _plain_error(503, "governance_unavailable",
                            "This connection cannot be checked right now; try again shortly.")
    if not reason:
        return None
    try:
        current_app.db.log_audit(owner, f"oauth_{stage}_refused", mcp_id, {"reason": "governance"})
    except Exception:  # noqa: BLE001 — the refusal stands without its row
        logger.warning("oauth %s: refusal audit failed", stage, exc_info=True)
    return _plain_error(403, "not_available", reason)


def _refuse_unavailable(mcp_id: str, provider: str):
    """#3424: the 503 answer for a card whose engine module is off, else None.
    #3427: an entry the catalogue marks unusable is refused the same way."""
    gone = card_state.not_available(current_app.catalog, mcp_id)
    if gone:
        return jsonify({"error": "not_available", "message": gone}), 503
    if not card_state.engine_off(current_app.catalog, mcp_id):
        return None
    return jsonify({
        "error": "engine_not_enabled",
        "message": card_state.admin_hint(current_app.catalog, current_app.oauth_flow, mcp_id,
                                         _redirect_uri(provider)),
    }), 503


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
