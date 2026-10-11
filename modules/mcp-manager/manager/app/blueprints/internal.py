# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Internal service-to-service endpoints (#36 gap 2).

NOT routed through Caddy's mcp.<domain> forward-auth block. The agent-manager
reaches these directly at http://mcp-manager:5000/internal/... on the shared
docker `default` network — the SAME pattern as Caddy -> agent-manager's
/api/tls/ask. Caddy's mcp.<domain> site forward-auths every external path, so
these are not reachable from outside the box.

GET /internal/agent-wiring/<user_slug>
    Returns the per-user MCP-proxy wiring blocks (moltis env / hermes add
    commands / opencode mcp block + the raw proxy id list) for that user's
    RUNNING proxies. The agent-manager pulls this at agent launch/relaunch time
    and injects the blocks into the user's hermes/moltis/opencode instances.

SECURITY: the response only ever describes ONE user's proxies (the DB query is
scoped to user_slug). No credential material is ever returned — only opaque
per-instance proxy URLs (the token-subdomain scheme, username never embedded).
"""

import hmac
import logging
import os

from flask import Blueprint, current_app, jsonify, request

from app.services import agent_wiring

logger = logging.getLogger(__name__)

internal_bp = Blueprint("internal", __name__, url_prefix="/internal")

INTERNAL_TOKEN_HEADER = "X-MCP-Internal-Token"


def _internal_token() -> str:
    """The shared service-to-service secret. Read from config first (test/app
    factory inject it there) then the environment."""
    return str(current_app.config.get("MCP_INTERNAL_TOKEN")
               or os.environ.get("MCP_INTERNAL_TOKEN", ""))


@internal_bp.before_request
def _require_internal_token():
    """HIGH-1 (#61): fail-closed shared-secret gate on EVERY /internal/* route.

    These endpoints are service-to-service (agent-manager -> mcp-manager, both
    on the `default` network) and are NOT behind Caddy's forward-auth. This
    shared-token gate is the PRIMARY control — and since #619/#639 put
    mcp-manager on `mcp-network` (its wildcard relay dials the proxies by
    name), it is the ONLY thing between a compromised per-user proxy and
    /internal/* (#61 NEW-2 updated; see compose.yml + docker_client).
    Without the correct shared token the endpoint returns 401 and leaks nothing.
    A constant-time compare avoids a timing oracle on the token.
    """
    expected = _internal_token()
    if not expected:
        # Mis-provisioned: no token minted. Fail CLOSED rather than open.
        logger.error("MCP_INTERNAL_TOKEN not set — refusing internal request")
        return jsonify({"error": "internal endpoint not configured"}), 401
    presented = request.headers.get(INTERNAL_TOKEN_HEADER, "")
    if not hmac.compare_digest(presented, expected):
        return jsonify({"error": "unauthorized"}), 401
    return None


@internal_bp.route("/agent-wiring/<user_slug>", methods=["GET"])
def agent_wiring_for_user(user_slug):
    mcp_domain = current_app.config.get("MCP_DOMAIN", "")
    try:
        payload = agent_wiring.wiring_for_user(
            current_app.db, current_app.catalog, user_slug, mcp_domain)
    except Exception:
        logger.exception("agent-wiring build failed for %s", user_slug)
        # Fail-safe: an empty wiring must never break agent provisioning.
        payload = {"moltis_env": {}, "hermes_specs": [], "opencode_block": {},
                   "claude_mcp": {"mcpServers": {}}, "codex_mcp": {}, "proxies": []}
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Integration governance (#36 follow-up). Config-UI (a trusted `default`-network
# service) reads + writes which integrations are AVAILABLE and their min tier.
# Gated by the SAME internal-token gate above; NOT reachable from outside Caddy.
# ---------------------------------------------------------------------------

@internal_bp.route("/governance", methods=["GET"])
def get_governance():
    """Every catalog integration with its effective governance (row or default).

    Config-UI renders the availability + tier controls from this.
    """
    from app.services.governance import effective_governance
    from app.services.tier import VALID_MIN_TIERS
    out = []
    for m in current_app.catalog.all():
        gov = effective_governance(current_app.db, m["id"])
        out.append({
            "mcp_id": m["id"],
            "display_name": m.get("display_name", m["id"]),
            "tenancy": m.get("tenancy", "per-user"),
            "available": gov["available"],
            "min_tier": gov["min_tier"],
        })
    return jsonify({"integrations": out, "tiers": list(VALID_MIN_TIERS)})


@internal_bp.route("/governance/<mcp_id>", methods=["PUT"])
def put_governance(mcp_id):
    """Set availability + min tier for one integration. Validated + persisted
    to the DB (mcp_integration_governance), never to .env."""
    from app.services.tier import VALID_MIN_TIERS
    if not current_app.catalog.get(mcp_id):
        return jsonify({"error": "Unknown integration"}), 404
    body = request.get_json(silent=True) or {}
    available = bool(body.get("available", True))
    min_tier = str(body.get("min_tier", "regular"))
    if min_tier not in VALID_MIN_TIERS:
        return jsonify({"error": f"invalid min_tier (allowed: {list(VALID_MIN_TIERS)})"}), 400
    try:
        current_app.db.set_governance(mcp_id, available, min_tier)
    except Exception:
        logger.exception("governance write failed for %s", mcp_id)
        return jsonify({"error": "persist failed"}), 500
    return jsonify({"mcp_id": mcp_id, "available": available, "min_tier": min_tier})


# ---------------------------------------------------------------------------
# Company Brain admin flow (#36 two-tier cognee). Config-UI (token-gated) lets an
# admin create a shared cognee dataset and grant an Authentik group READ. The
# mcp-manager holds the cognee admin creds; it creates the dataset + a cognee
# role and grants the role read on the dataset (cognee /permissions API).
# ---------------------------------------------------------------------------

def _cognee_admin():
    import os
    return (os.environ.get("COGNEE_BASE_URL", "http://cognee:8000"),
            os.environ.get("COGNEE_ADMIN_EMAIL", ""),
            os.environ.get("COGNEE_ADMIN_PASSWORD", ""))


@internal_bp.route("/company-brain", methods=["POST"])
def create_company_brain():
    """Create (idempotent) a shared company-brain dataset. Body: {name}."""
    from app.services import cognee_identity
    base, ae, ap = _cognee_admin()
    if not ae or not ap:
        return jsonify({"error": "cognee admin creds not configured"}), 400
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    if not name:
        return jsonify({"error": "dataset name required"}), 400
    try:
        res = cognee_identity.create_company_brain(base, ae, ap, name)
    except Exception as e:
        logger.exception("company-brain create failed")
        return jsonify({"error": str(e)}), 500
    # Record the dataset name against the cognee-company governance row so the
    # provisioner scopes instances to it.
    try:
        current_app.db.set_governance_config("cognee-company",
                                             {"company_dataset": name})
    except Exception:
        logger.warning("could not persist company_dataset config")
    return jsonify(res)


@internal_bp.route("/company-brain/grant", methods=["POST"])
def grant_company_brain():
    """Grant a principal (cognee role/user id) READ on a company dataset.
    Body: {principal_id, dataset_id, permission?}."""
    from app.services import cognee_identity
    base, ae, ap = _cognee_admin()
    if not ae or not ap:
        return jsonify({"error": "cognee admin creds not configured"}), 400
    body = request.get_json(silent=True) or {}
    pid = str(body.get("principal_id", "")).strip()
    ds = str(body.get("dataset_id", "")).strip()
    perm = str(body.get("permission", "read")).strip() or "read"
    if not pid or not ds:
        return jsonify({"error": "principal_id and dataset_id required"}), 400
    ok = cognee_identity.grant_principal_read(base, ae, ap, pid, ds, permission=perm)
    return (jsonify({"granted": True}) if ok
            else (jsonify({"error": "grant failed"}), 500))
