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
from app.services.gateway_identity import legacy_slug_for

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
    """`?sub=<owner id>&username=<owner username>` (#2086, ruling I5): the
    Agent Manager's stored owner id. It is the owner's sub only if the
    read-only Authentik directory confirms an active internal user with
    exactly that uid AND it differs from the username (the header parser
    falls back to the username when X-Authentik-Uid is missing). Only then
    are the user's proxies selected by the sub and the stack's gateway
    servers wired with the owner's agent token; otherwise the agent gets no
    gateway server (and no token is minted) and the 2026.09 slug wiring
    applies unchanged."""
    mcp_domain = current_app.config.get("MCP_DOMAIN", "")
    owner_sub = _verified_owner_sub(user_slug)
    try:
        payload = agent_wiring.wiring_for_user(
            current_app.db, current_app.catalog, user_slug, mcp_domain,
            legacy_slug=legacy_slug_for(current_app, user_slug),
            owner_sub=owner_sub,
            gateway_servers=getattr(current_app, "gateway_servers", None),
            cred_store=getattr(current_app, "cred_store", None),
            active_profiles=[p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",")
                             if p.strip()])
    except Exception:
        logger.exception("agent-wiring build failed for %s", user_slug)
        # Fail-safe: an empty wiring must never break agent provisioning.
        payload = {"moltis_env": {}, "hermes_specs": [], "opencode_block": {},
                   "claude_mcp": {"mcpServers": {}}, "codex_mcp": {}, "proxies": []}
    return jsonify(payload)


def _verified_owner_sub(user_slug):
    """The owner's sub, or None (ruling I5; any doubt is None). A directory
    that cannot be read (or times out) is a WARN, not an error: the agent
    still gets its user's own proxies, only the gateway servers — which need
    the confirmed sub — are left out."""
    sub = (request.args.get("sub") or "").strip()
    username = request.args.get("username")
    if not sub:
        return None
    directory = getattr(current_app, "authentik_directory", None)
    if directory is None:
        logger.warning("agent-wiring %s: no read-only Authentik directory; "
                       "the agent gets no gateway server", user_slug)
        return None
    try:
        state = directory.owner_uid_state(sub, username=username)
    except Exception:  # noqa: BLE001 — a failed check is a refusal
        logger.exception("agent-wiring %s: uid check failed", user_slug)
        state = "unavailable"
    if state == "unavailable":
        logger.warning("agent-wiring %s: Authentik directory unavailable; the agent gets "
                       "its own proxies but no gateway server this time", user_slug)
        return None
    if state != "active":
        logger.warning("agent-wiring %s: owner id is not an Authentik uid; "
                       "the agent gets no gateway server", user_slug)
        return None
    return sub


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


@internal_bp.route("/technical-users/<name>/instances/<mcp_id>", methods=["POST"])
def technical_instance(name, mcp_id):
    """#2472 (B9): provision a SHARED integration (the cognee company brain)
    for a technical user, so Dify reaches it through the gateway's
    `shared_proxy` entry as that technical user. The instance is keyed by
    `~svc-<name>` alone. A per-user entry is never provisioned for a
    technical user: it would hold one person's data. Called by the config
    portal on a platform admin's action (the internal-token gate above)."""
    from app.services.gateway_identity import technical_principal
    from app.services.governance import effective_governance
    integ = current_app.catalog.get(mcp_id)
    if not integ or integ.get("tenancy") != "shared":
        return jsonify({"error": "only shared integrations can be provisioned for a technical user"}), 400
    try:
        principal = technical_principal(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not effective_governance(current_app.db, mcp_id)["available"]:
        return jsonify({"error": "this integration is not available on this box"}), 403
    iid, msg = current_app.provisioner.launch(mcp_id, principal, name, [], principal=principal)
    if not iid:
        return jsonify({"error": msg}), 409
    inst = current_app.db.get_instance_by_mcp_and_user(mcp_id, principal)
    if not inst or inst.get("state") != "running":
        return jsonify({"error": msg, "instance_id": iid}), 502
    return jsonify({"instance_id": iid}), 201


@internal_bp.route("/gateway/stats", methods=["GET"])
def gateway_stats():
    """#2472: the MCP gateway's counters for status/metrics — anonymous
    refusals (never written to the database, only counted and logged at most
    once per peer per minute), refusals over the concurrency caps, and the
    relays held right now. Numbers only; no caller, peer or credential."""
    from app.blueprints.gateway import runtime
    rt = runtime(current_app)
    return jsonify({"anonymous_refused": rt.anonymous.total,
                    "over_caller_limit": rt.limiter.refused["per_caller"],
                    "over_gateway_limit": rt.limiter.refused["total"],
                    "relays_held": rt.limiter.held(),
                    "limits": {"per_caller": rt.limits.per_caller, "total": rt.limits.total,
                               "stream_idle_timeout": rt.limits.stream_idle_timeout}})


# ---------------------------------------------------------------------------
# #2472 technical users. The source of truth is A17's Authentik service accounts
# (A1 D7); the config portal pushes each change here so the gateway can decide
# without an Authentik token. Same internal-token gate as everything above.
# No response ever carries a stored credential; a minted token is returned once
# (only its SHA-256 is kept).
# ---------------------------------------------------------------------------

#: The credential fields a technical user's upstream credential may carry
#: (gateway_credentials reads api_key/pat; client_id/client_secret are the
#: client-credentials pair). An oauth_* row is never written from here: it
#: would be injected ahead of the PAT.
TECHNICAL_CREDENTIAL_FIELDS = frozenset({"api_key", "pat", "client_id", "client_secret"})
_MAX_CREDENTIAL_VALUE = 4096


def _jsonable(row):
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(row).items()}


def _technical_name_error(name):
    """None when `name` is a technical user's name (A17's NAME_RE, not
    reserved); else the 400 response."""
    from app.services.gateway_identity import technical_principal
    try:
        technical_principal(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return None


def _live_technical_user(name):
    tu = current_app.db.get_technical_user(name)
    return tu if tu and not tu.get("revoked_at") else None


@internal_bp.route("/technical-users", methods=["GET"])
def list_technical_users():
    return jsonify({"technical_users": [_jsonable(r) for r in current_app.db.list_technical_users()]})


@internal_bp.route("/technical-users/<name>", methods=["PUT"])
def mirror_technical_user(name):
    bad = _technical_name_error(name)
    if bad:
        return bad
    body = request.get_json(silent=True) or {}
    active = body.get("is_active") is True
    owner_sub = str(body.get("owner_sub") or "").strip()
    display = str(body.get("owner_username") or "").strip()[:150]
    bindings = [b for b in (body.get("bindings") or []) if isinstance(b, str)]
    pk = body.get("authentik_pk")
    if not isinstance(pk, int) or isinstance(pk, bool):
        pk = None
    if active and not owner_sub:
        return jsonify({"error": "an active technical user needs owner_sub, the owner's OIDC sub"}), 400
    since = _aware_timestamp(body.get("active_since"))
    before = current_app.db.get_technical_user(name)
    old_pk = (before or {}).get("authentik_pk")
    if isinstance(old_pk, int) and isinstance(pk, int) and old_pk != pk:
        # The same name, another Authentik account (deleted and re-created):
        # nothing the old account was given carries over.
        current_app.db.drop_technical_credentials(name)
        current_app.db.log_audit(None, "technical_user_replaced", None, {"name": name})
        # Before the new account's row is written: until the old account's
        # OpenBao grants are gone the name stays the old account's (pk) and
        # the next push repeats all of this.
        if not _purge_openbao_grants(name, "replaced"):
            return jsonify({"error": "the previous account's OAuth grants could not be removed "
                                     "from OpenBao yet; retried on the next push"}), 503
    row = current_app.db.upsert_technical_user(name, owner_sub, pk, bindings, active, display,
                                               active_since=since)
    if _mirror_changed(before, row):
        current_app.db.log_audit(None, "technical_user_sync", None,
                                 {"name": name, "active": not (row or {}).get("revoked_at"),
                                  "bindings": bindings})
    return jsonify(_jsonable(row or {}))


def _aware_timestamp(value):
    """An ISO-8601 timestamp WITH a zone, or None (a naive or malformed
    value never re-enables a revoked row)."""
    from datetime import datetime
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None and ts.utcoffset() is not None else None


_MIRROR_FIELDS = ("authentik_pk", "owner_sub", "owner_username", "bindings")


def _mirror_changed(before, after) -> bool:
    """A reconcile pushes every technical user on every page load and sweep
    tick; only a push that changed something is worth an audit row."""
    if not before or not after:
        return True
    if bool(before.get("revoked_at")) != bool(after.get("revoked_at")):
        return True
    return any(before.get(k) != after.get(k) for k in _MIRROR_FIELDS)


@internal_bp.route("/technical-users/<name>", methods=["DELETE"])
def delete_technical_user(name):
    bad = _technical_name_error(name)
    if bad:
        return bad
    revoked = current_app.db.revoke_technical_user(name)
    if revoked:
        current_app.db.log_audit(None, "technical_user_revoke", None, {"name": name})
    if not _purge_openbao_grants(name, "revoked"):
        # revoked here (no token resolves), but not finished in OpenBao: the
        # portal shows "revocation unfinished" and its Retry repeats this
        return jsonify({"error": "revocation unfinished: the OAuth grants in OpenBao could not "
                                 "be removed yet"}), 503
    return jsonify({"revoked": bool(revoked)})


@internal_bp.route("/technical-users/<name>/tokens", methods=["POST"])
def mint_technical_token(name):
    from app.services.gateway_identity import TECH_CONSUMERS, mint_token, technical_principal
    bad = _technical_name_error(name)
    if bad:
        return bad
    consumer = str((request.get_json(silent=True) or {}).get("consumer", ""))
    if not _live_technical_user(name):
        return jsonify({"error": "unknown or revoked technical user"}), 404
    if consumer not in TECH_CONSUMERS:
        return jsonify({"error": "consumer must be dify or agent (a person's chat never uses a technical user)"}), 400
    principal = technical_principal(name)
    current_app.db.revoke_gateway_tokens(principal, consumer)
    token = mint_token(current_app.db, principal, "technical", consumer)
    current_app.db.log_audit(principal, "technical_token_mint", None, {"consumer": consumer})
    return jsonify({"token": token}), 201


def _technical_credential_ref(server_id):
    """The catalogue id a technical caller's credential for gateway server
    `server_id` is vaulted under (`catalog:<id>` servers that accept
    technical users), or None."""
    server = (getattr(current_app, "gateway_servers", None) or {}).get(server_id)
    if server is None or not server.technical or server.credential_kind != "catalog":
        return None
    return server.credential_ref or None


def _drop_machine_token(principal, ref):
    """The cached machine token of a rotated client pair, in Postgres and,
    with OpenBao on, in OpenBao. If OpenBao cannot be reached the token stays
    there, but it is never served: its stored fingerprint names the old pair
    (gateway_credentials._client_credentials_upstream)."""
    from app.services.credential_store import CredentialStoreError
    try:
        current_app.cred_store.drop_oauth(principal, ref)
    except CredentialStoreError as e:
        logger.warning("machine token of %s for %s not dropped: %s", principal, ref, e)


def _purge_openbao_grants(name, why) -> bool:
    """#2472 B22: a technical user's OpenBao OAuth grants go with it (revoked,
    or its name now belongs to another account). Postgres rows go in the same
    request's DB call. True when nothing is left in OpenBao. A failure is
    logged and audited, and the caller answers 503: the portal then keeps the
    revocation (or the re-mirroring) unfinished and retries it."""
    from app.services.credential_store import CredentialStoreError
    from app.services.gateway_identity import technical_principal
    principal = technical_principal(name)
    try:
        n = current_app.cred_store.purge_oauth(principal)
    except CredentialStoreError as e:
        logger.error("OpenBao OAuth grants of %s not purged (%s): %s", principal, why, e)
        current_app.db.log_audit(principal, "oauth_purge_failed", None, {"why": why})
        return False
    if n:
        current_app.db.log_audit(principal, "oauth_purged", None, {"why": why, "grants": n})
    return True


@internal_bp.route("/technical-users/<name>/credentials/<mcp_id>", methods=["PUT"])
def put_technical_credentials(name, mcp_id):
    from app.services.gateway_identity import technical_principal
    bad = _technical_name_error(name)
    if bad:
        return bad
    if not _live_technical_user(name):
        return jsonify({"error": "unknown or revoked technical user"}), 404
    ref = _technical_credential_ref(mcp_id)
    if ref is None:
        return jsonify({"error": f"no gateway server {mcp_id!r} takes a technical user's credential"}), 404
    fields = (request.get_json(silent=True) or {}).get("fields")
    if not isinstance(fields, dict) or not fields:
        return jsonify({"error": "fields required"}), 400
    unknown = sorted(str(k) for k in fields if k not in TECHNICAL_CREDENTIAL_FIELDS)
    if unknown:
        return jsonify({"error": f"unknown credential fields {unknown}; "
                                 f"allowed: {sorted(TECHNICAL_CREDENTIAL_FIELDS)}"}), 400
    if not all(isinstance(v, str) and v and len(v) <= _MAX_CREDENTIAL_VALUE for v in fields.values()):
        return jsonify({"error": "every field needs a non-empty string value"}), 400
    principal = technical_principal(name)
    current_app.cred_store.store_pat(principal, ref, dict(fields))
    if {"client_id", "client_secret"} & set(fields):
        # #2472: a machine token fetched for the OLD client pair must not
        # outlive it (a rotated secret takes effect on the next call).
        _drop_machine_token(principal, ref)
    current_app.db.log_audit(principal, "technical_credential_store", ref, {"fields": sorted(fields)})
    return jsonify({"stored": sorted(fields)})


#: #2554: the gateway server whose vaulted credential is a technical user's
#: Gitea service-user token (core/mcp/mcp-servers.yaml).
GITEA_SERVER = "gitea"


@internal_bp.route("/technical-users/<name>/git-credentials", methods=["GET"])
def technical_git_credentials(name):
    """#2554: an autonomous agent's git identity is the technical user's Gitea
    service user: {token, name, email}, only while the technical user is live,
    bound to gitea, its Gitea service user live and its token vaulted.
    Agents only (`?consumer=agent`, the default); everything else is refused,
    and the Agent Manager refuses the launch rather than fall back to the
    owner's own token."""
    from app.services.gateway_identity import technical_principal
    bad = _technical_name_error(name)
    if bad:
        return bad
    if request.args.get("consumer", "agent") != "agent":
        return jsonify({"error": "git credentials go to agents only"}), 400
    tu = current_app.db.get_technical_user(name)
    if (not tu or tu.get("revoked_at") or tu.get("gitea_state") in ("owed", "revoked")
            or not current_app.db.technical_bound(name, GITEA_SERVER)):
        return jsonify({"error": f"not a live technical user bound to {GITEA_SERVER}"}), 403
    principal = technical_principal(name)
    # #3477 (A2-01): the Settings page's availability of the `gitea` entry
    # holds for this credential too (a technical user has no tier).
    from app.services import gateway_credentials as gc
    try:
        refusal = gc._governance_refusal(current_app.db, GITEA_SERVER, person=False)
    except gc.Unavailable as e:
        return jsonify({"error": str(e)}), 503
    if refusal:
        current_app.db.log_audit(principal, "technical_git_credentials_refused", GITEA_SERVER,
                                 {"reason": "governance"})
        return jsonify({"error": refusal}), 403
    tok = (current_app.cred_store.get_decrypted(principal, GITEA_SERVER) or {}).get("api_key")
    if not tok:
        return jsonify({"error": "no Gitea service user yet"}), 403
    dom = current_app.config.get("MAIN_DOMAIN") or os.environ.get("MAIN_DOMAIN", "localhost")
    current_app.db.log_audit(principal, "technical_git_credentials", GITEA_SERVER, {"consumer": "agent"})
    # The address is the one the config portal gives the Gitea service user
    # (core/config/app/services/gitea_admin.py), so Gitea attributes the commits.
    return jsonify({"token": tok, "name": name, "email": f"{name}@noreply.{dom}"})


@internal_bp.route("/technical-users/<name>/gitea", methods=["PUT"])
def technical_gitea_state(name):
    """#2554: what the config portal owes Gitea for this technical user. The
    portal does every Gitea call (it holds the Gitea admin; mcp-manager never
    does) and records here, durably:

    - "live" BEFORE it creates or refreshes the Gitea service user, with the
      Authentik pk it is made for (which must be this row's pk) and, once
      known, the Gitea user id: the ONE Gitea user the portal may take over;
    - "owed" when its revoke is due while the row may be live (a token the
      gateway did not store; a pk change sets it itself, see
      drop_technical_credentials);
    - "revoked" once Gitea confirmed the tokens deleted and the login
      prohibited.

    The listing carries all three columns, so the portal's reconcile retries
    an owed revoke even for an account deleted from Authentik by hand. The
    token itself is vaulted through the credentials route above."""
    bad = _technical_name_error(name)
    if bad:
        return bad
    body = request.get_json(silent=True) or {}
    state = body.get("state")
    if state not in ("live", "owed", "revoked"):
        return jsonify({"error": "state must be live, owed or revoked"}), 400
    ids = {}
    for key, col in (("gitea_user_id", "gitea_user_id"), ("authentik_pk", "authentik_pk")):
        if key in body:
            v = body[key]
            if v is not None and (not isinstance(v, int) or isinstance(v, bool)):
                return jsonify({"error": f"{key} must be an integer or null"}), 400
            ids[col] = v
    tu = current_app.db.get_technical_user(name)
    if not tu:
        return jsonify({"error": "unknown technical user"}), 404
    if state == "live":
        if tu.get("revoked_at"):
            return jsonify({"error": "unknown or revoked technical user"}), 404
        if f"mcp-integration:{GITEA_SERVER}" not in (tu.get("bindings") or []):
            return jsonify({"error": f"bind mcp-integration:{GITEA_SERVER} first"}), 409
        pk = ids.get("authentik_pk")
        if not isinstance(pk, int) or pk != tu.get("authentik_pk"):
            return jsonify({"error": "authentik_pk must be the account this technical user is mirrored for"}), 409
    elif ids:
        return jsonify({"error": "the ids are recorded with state live only"}), 400
    current_app.db.set_gitea_state(name, state, **ids)
    if state == "live" and (current_app.db.get_technical_user(name) or {}).get("revoked_at"):
        # Revoked while this ran: "live" stays recorded (the revoke is owed),
        # but nothing may be made for it.
        return jsonify({"error": "unknown or revoked technical user"}), 404
    return jsonify({"name": name, "gitea_state": state, **ids})
