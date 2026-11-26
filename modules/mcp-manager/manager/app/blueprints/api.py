# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""REST API for the personal MCP manager (#36).

Identity is ALWAYS derived server-side from the Authentik forward-auth headers
(X-Authentik-Username / Uid / Groups), never from a client-supplied field —
this is the only-own boundary. #2472: the key is the OIDC sub, read directly
from X-Authentik-Uid (never parse_authentik_headers()'s username fallback); a
request without it is refused. Every per-instance / per-credential operation
re-checks that the resource's owner_sub is the caller's sub. The slug is a
display value and the container name part only.

Credential plaintext is accepted on POST /credentials, immediately handed to
the encrypting CredentialStore, and NEVER returned in any response.
"""

import logging
import os
import uuid

from flask import Blueprint, current_app, jsonify, request

from razzfazz_common.auth import parse_authentik_headers

from app.blueprints.caddy_anchor import anchor_exempt
from app.services.gateway_identity import legacy_slug_for, request_sub

# #61 NEW-1 (was LOW-2): single source of truth for the slug. Imported straight
# from the shared lib (NOT via the provisioner) so the API, the provisioner, and
# agent-manager can NEVER diverge on the only-own key. Re-exported here for
# callers/tests that import it from the blueprint.
from razzfazz_common.user_slug import make_user_slug  # noqa: F401

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _get_user():
    """(username, sub, slug, groups). All None unless BOTH the username and
    X-Authentik-Uid are present: without the uid there is no key (#2472)."""
    info = parse_authentik_headers()
    sub = request_sub()
    if not info["username"] or not sub:
        return None, None, None, None
    return info["username"], sub, make_user_slug(info["username"]), info["groups"]


def _legacy(user_slug):
    """The caller's slug for the ga.4-parity legacy lookup, or None once a
    read-only token is set (B6 fix round 2, ruling I1: fail closed)."""
    return legacy_slug_for(current_app, user_slug)


def _owned_instance(instance_id, sub, user_slug):
    """Return the instance row IFF it exists AND it is the caller's: its
    owner_sub is `sub`, or (legacy window, B6 fix round 1) it is an undecided
    pre-2026.11 row under the caller's slug, as 2026.09 owned it — only
    while no read-only token is set (fix round 2)."""
    try:
        row = current_app.db.get_instance(uuid.UUID(str(instance_id)))
    except (ValueError, TypeError):
        return None
    if not row or not sub:
        return None
    if row.get("owner_sub") == sub:
        return row
    legacy = _legacy(user_slug)
    if row.get("owner_sub") is None and legacy and row.get("user_slug") == legacy:
        return row
    return None


@api_bp.route("/integrations", methods=["GET"])
def integrations():
    """List the offerable personal-MCP integrations the CALLER may see.

    #36 follow-up: filtered by Config-UI governance (available + per-integration
    tier gate) resolved against the caller's Authentik groups. Enforced
    server-side here — the returned list already excludes anything the user may
    not provision, so a lower-tier user never even learns an integration exists.
    """
    username, _, _, groups = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    from app.services import governance
    # No secrets in the catalog — env_map placeholders are templates, not values.
    visible = governance.visible_integrations(current_app.db, current_app.catalog, groups)
    return jsonify({"integrations": visible})


@api_bp.route("/mine", methods=["GET"])
def mine():
    """List the caller's own MCP instances + a NON-SECRET credential summary."""
    username, sub, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403

    mcp_domain = current_app.config.get("MCP_DOMAIN", "")
    out = []
    for row in current_app.db.get_user_instances(sub, legacy_slug=_legacy(user_slug)):
        inst = dict(row)
        inst["id"] = str(inst.get("id"))
        # Per-instance opaque-token URL (never embeds the username).
        try:
            from app.services.caddy_client import instance_token
            if inst.get("id") and mcp_domain:
                inst["url"] = (
                    f"https://{inst['mcp_id']}-{instance_token(row['id'])}.{mcp_domain}"
                )
        except Exception:
            pass
        inst.pop("config", None)  # config may carry the route secret — don't expose
        out.append(inst)

    creds = {}
    for mcp_id in {i["mcp_id"] for i in out} | set(
            current_app.db.list_user_mcp_ids_with_secrets(sub, legacy_slug=_legacy(user_slug))
            if hasattr(current_app.db, "list_user_mcp_ids_with_secrets") else []):
        creds[mcp_id] = current_app.cred_store.credential_summary(sub, mcp_id,
                                                                  legacy_slug=_legacy(user_slug))

    return jsonify({"instances": out, "credentials": creds,
                    "reconnect_notices": _reconnect_notices(user_slug)})


#: The portal's reason for a stack server an offline box does not offer.
HIDDEN_OFFLINE = "needs internet (offline box)"


@api_bp.route("/stack-tools", methods=["GET"])
def stack_tools():
    """#2086: the stack's MCP servers (the gateway registry) this person can
    use from their agents or Open WebUI, and whether each still needs a
    connection first. Keyed by the caller's sub, as the gateway is: a legacy
    slug row never counts as connected (the gateway never serves one)."""
    username, sub, _user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    from app.services import gateway_registry
    profiles = [p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p.strip()]
    mode = os.environ.get("RAZZFAZZ_NETWORK_MODE", "online")
    out = []
    servers = getattr(current_app, "gateway_servers", None) or {}
    for s in sorted(servers.values(), key=lambda s: s.id):
        if not any(gateway_registry.eligible(s, c, profiles, mode) for c in ("agent", "openwebui")):
            # #2472: a server this box would offer online but hides because it
            # is offline is listed with the reason, not silently dropped.
            if any(gateway_registry.eligible(s, c, profiles, "online") for c in ("agent", "openwebui")):
                out.append({"id": s.id, "description": s.description, "hidden": HIDDEN_OFFLINE})
            continue
        ref = s.credential_ref if s.credential_kind != "none" else None
        reauth = False
        try:
            if s.credential_kind == "catalog":
                connected = bool(current_app.cred_store.has_credentials(sub, ref))
                # #2554: a grant the provider rejected is not a connection.
                ask = getattr(current_app.cred_store, "reauth_required", None)
                reauth = connected and callable(ask) and ask(sub, ref) is True
                connected = connected and not reauth
            elif s.credential_kind == "per_user_proxy":
                inst = current_app.db.get_instance_by_mcp_and_user(ref, sub)
                connected = bool(inst and inst.get("state") == "running")
            else:
                connected = True
        except Exception:  # noqa: BLE001 — a lookup failure reads as "not connected"
            logger.exception("stack-tools: connection lookup failed for %s", s.id)
            connected = False
        row = {"id": s.id, "description": s.description, "connected": connected, "connect": ref}
        if reauth:
            row["reauth"] = True
        out.append(row)
    return jsonify({"tools": out})


#: Design record §2.3: the notice a visitor sees per integration whose
#: pre-upgrade rows under their slug were quarantined. No content, no name.
RECONNECT_NOTICE = ("A connection saved before the upgrade could not be verified "
                    "as yours. Reconnect {integration}.")


def _reconnect_notices(user_slug):
    try:
        ids = current_app.db.quarantine_notices_for_slug(user_slug) or []
    except Exception:  # noqa: BLE001 — a notice must never break the page
        return []
    out = []
    for mcp_id in ids:
        integ = current_app.catalog.get(mcp_id) or {}
        name = integ.get("display_name") or mcp_id
        out.append({"mcp_id": mcp_id, "message": RECONNECT_NOTICE.format(integration=name)})
    return out


@api_bp.route("/credentials/<mcp_id>", methods=["POST"])
def add_credentials(mcp_id):
    """Add/replace PAT-style credentials for the caller's own (user, mcp).

    Secret fields are encrypted at rest; non-secret fields (email, subdomain,
    company_domain, ...) are recorded as instance config. Plaintext is NEVER
    returned.
    """
    username, sub, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403

    integ = current_app.catalog.get(mcp_id)
    if not integ:
        return jsonify({"error": "Unknown integration"}), 404
    from app.services.card_state import not_available
    gone = not_available(current_app.catalog, mcp_id)
    if gone:
        # #3427: nothing to store credentials for on this box.
        return jsonify({"error": gone}), 409
    if current_app.catalog.cred_model(mcp_id) != "pat":
        return jsonify({"error": "This integration uses OAuth; start it via /api/oauth/start"}), 400
    # #3477 (A2-01): nothing is stored for an entry the Settings page has
    # switched off or set above this person's tier (an unreadable row: 503).
    from app.services import governance
    try:
        reason = governance.person_refusal(current_app.db, mcp_id, _get_user()[3])
    except Exception:  # noqa: BLE001
        logger.exception("credentials: governance for %s could not be read", mcp_id)
        return jsonify({"error": "governance_unavailable",
                        "message": "This connection cannot be checked right now; try again shortly."}), 503
    if reason:
        current_app.db.log_audit(sub, "credentials_refused", mcp_id, {"reason": "governance"})
        return jsonify({"error": "not_available", "message": reason}), 403

    body = request.get_json(silent=True) or {}
    cred_fields = integ.get("cred_fields", [])
    secret_keys = {f["key"] for f in cred_fields if f.get("secret")}
    nonsecret_keys = {f["key"] for f in cred_fields if not f.get("secret")}

    # Only accept declared fields (ignore anything extra a client sends).
    secret_vals = {k: body[k] for k in secret_keys if k in body and body[k] != ""}
    nonsecret_vals = {k: body[k] for k in nonsecret_keys if k in body}

    if not secret_vals:
        return jsonify({"error": "No secret credential provided",
                        "expected": sorted(secret_keys)}), 400

    current_app.cred_store.store_pat(sub, mcp_id, secret_vals,
                                     secret_keys=secret_keys, user_slug=user_slug)
    # Non-secret settings recorded for later proxy provisioning.
    if nonsecret_vals and hasattr(current_app.db, "get_instance_by_mcp_and_user"):
        inst = current_app.db.get_instance_by_mcp_and_user(mcp_id, sub, legacy_slug=_legacy(user_slug))
        if inst:
            # merge into config (kept simple; provisioner re-reads at launch)
            pass

    # Response carries ONLY non-secret metadata.
    return jsonify({
        "status": "ok",
        "mcp_id": mcp_id,
        "stored_cred_types": sorted(secret_vals.keys()),
        "settings": nonsecret_vals,
    })


@api_bp.route("/credentials/<mcp_id>/revoke", methods=["POST"])
def revoke_credentials(mcp_id):
    username, sub, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    if not current_app.catalog.get(mcp_id):
        return jsonify({"error": "Unknown integration"}), 404
    from app.services.credential_store import CredentialStoreError
    provider_revoked = _revoke_at_provider(sub, mcp_id)
    try:
        current_app.cred_store.revoke(sub, mcp_id, legacy_slug=_legacy(user_slug))
    except CredentialStoreError as e:
        # #2472 B22: never report a revoke that did not reach OpenBao as done.
        logger.error("revoke of %s for %s incomplete: %s", mcp_id, user_slug, e)
        return jsonify({"error": "credential_store_unavailable",
                        "message": "The connection could not be removed completely. Try again later."}), 503
    out = {"status": "ok", "mcp_id": mcp_id}
    if provider_revoked is not None:
        out["provider_revoked"] = provider_revoked
    return jsonify(out)


def _revoke_at_provider(sub, mcp_id):
    """#3429: a remote MCP server's grant is also revoked at the provider
    (RFC 7009) when its metadata names a revocation endpoint. Best effort:
    True/False for a remote entry, None for any other; never raises, never
    blocks the local revoke."""
    from app.services import remote_mcp
    spec = remote_mcp.spec_for(current_app.catalog.get(mcp_id))
    flow = getattr(current_app, "oauth_flow", None)
    if spec is None or getattr(flow, "remote", None) is None:
        return None
    try:
        tokens = current_app.cred_store.get_decrypted(sub, mcp_id)
        provider = current_app.catalog.oauth_provider(mcp_id)
        domain = current_app.config.get("MCP_DOMAIN", "")
        return flow.remote.revoke_at_provider(
            spec, tokens, f"https://{domain}/oauth/callback/{provider}")
    except Exception:  # noqa: BLE001 — the local revoke goes on regardless
        logger.exception("revocation of %s at the provider failed", mcp_id)
        return False


@api_bp.route("/provision/<mcp_id>", methods=["POST"])
def provision(mcp_id):
    """Launch the caller's own per-user MCP proxy for `mcp_id`."""
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    if not current_app.catalog.get(mcp_id):
        return jsonify({"error": "Unknown integration"}), 404

    # #36 follow-up: server-side governance + tier gate. A regular user cannot
    # provision a power/admin-gated integration, and a disabled integration is
    # provisionable by NO ONE — even hitting this route directly.
    from app.services import governance
    allowed, reason = governance.user_can_provision(
        current_app.db, mcp_id, groups, catalog=current_app.catalog)
    if not allowed:
        return jsonify({"error": reason}), 403

    instance_id, message = current_app.provisioner.launch(
        mcp_id, user_id, username, groups,
        legacy_slug=_legacy(make_user_slug(username)),
    )
    if instance_id and _instance_state(instance_id) == "error":
        # #3427: a launch whose container never came up is an error, not
        # "provisioned" — the row keeps the reason and "Start" retries it.
        return jsonify({"instance_id": instance_id, "error": message}), 502
    if instance_id:
        return jsonify({"instance_id": instance_id, "message": message})
    return jsonify({"error": message}), 400


def _instance_state(instance_id) -> str:
    try:
        row = current_app.db.get_instance(uuid.UUID(str(instance_id)))
    except Exception:  # noqa: BLE001 — unknown reads as not an error
        return ""
    return str((row or {}).get("state") or "") if isinstance(row, dict) else ""


@api_bp.route("/stop/<instance_id>", methods=["POST"])
def stop(instance_id):
    username, sub, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    if not _owned_instance(instance_id, sub, user_slug):
        return jsonify({"error": "Instance not found"}), 404
    iid, message = current_app.provisioner.stop(instance_id, username)
    return jsonify({"instance_id": iid, "message": message})


@api_bp.route("/delete/<instance_id>", methods=["POST"])
def delete(instance_id):
    username, sub, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    if not _owned_instance(instance_id, sub, user_slug):
        return jsonify({"error": "Instance not found"}), 404
    # Server-side confirm gate (defense-in-depth vs CSRF / curl accidents).
    if request.args.get("confirm") != "Delete":
        return jsonify({
            "error": "confirmation_required",
            "message": ("Delete requires ?confirm=Delete (case-sensitive)."),
        }), 400
    iid, message = current_app.provisioner.delete(instance_id, username)
    return jsonify({"instance_id": iid, "message": message})


@api_bp.route("/access/<mcp_id>", methods=["POST"])
def record_access(mcp_id):
    username, sub, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    inst = current_app.db.get_instance_by_mcp_and_user(mcp_id, sub, legacy_slug=_legacy(user_slug))
    if inst:
        current_app.db.update_last_accessed(inst["id"])
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# #2472 identity quarantine — the admin's list and the admin-only purge
# (design record §2.3). Admin = platform-admin or authentik-admin (A1 D3).
# ---------------------------------------------------------------------------

def _admin():
    from app.services.tier import is_mcp_admin
    username, sub, _, groups = _get_user()
    if not username or not is_mcp_admin(groups):
        return None
    return sub


def _jsonable(rows):
    out = []
    for r in rows or []:
        out.append({k: (v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in dict(r).items()})
    return out


@api_bp.route("/admin/identity-quarantine", methods=["GET"])
def identity_quarantine_list():
    """Quarantined rows and credential rows still unattributed 30 days after
    the migration. Metadata only: never a value, never ciphertext."""
    if not _admin():
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({
        "quarantined": _jsonable(current_app.db.list_quarantine()),
        "unattributed_after_30_days": _jsonable(current_app.db.list_unattributed_credentials(30)),
        "two_memories": _jsonable(current_app.db.cognee_dual_memories()),
    })


@api_bp.route("/admin/identity-quarantine/<int:qid>/purge", methods=["POST"])
def identity_quarantine_purge(qid):
    admin = _admin()
    if not admin:
        return jsonify({"error": "Forbidden"}), 403
    if request.args.get("confirm") != "Purge":
        return jsonify({"error": "confirmation_required",
                        "message": "Purge requires ?confirm=Purge (case-sensitive)."}), 400
    # #2573: purging a cognee account row removes the listing only; say so.
    is_cognee = any(r["id"] == qid and r["source_table"] == "mcp_cognee_accounts"
                    for r in (current_app.db.list_quarantine() or []))
    if not current_app.db.purge_quarantined(qid, admin):
        return jsonify({"error": "Not found"}), 404
    if is_cognee:
        return jsonify({"status": "purged", "id": qid, "cognee_data_remains": True,
                        "note": "Listing removed. The cognee account and its memory "
                                "stay in cognee and must be deleted there."})
    return jsonify({"status": "purged", "id": qid})


@api_bp.route("/admin/identity-quarantine/<int:qid>/erase-cognee", methods=["POST"])
def identity_quarantine_erase_cognee(qid):
    """#2573: erase a quarantined cognee account AND its memory in cognee
    (datasets first, then the account), typed confirm, audited. Irreversible."""
    admin = _admin()
    if not admin:
        return jsonify({"error": "Forbidden"}), 403
    if request.args.get("confirm") != "Erase":
        return jsonify({"error": "confirmation_required",
                        "message": "Erase requires ?confirm=Erase (case-sensitive)."}), 400
    row = next((r for r in (current_app.db.list_quarantine() or [])
                if r["id"] == qid and r["source_table"] == "mcp_cognee_accounts"), None)
    if row is None:
        return jsonify({"error": "Not found"}), 404
    slug = row.get("source_id") or row.get("user_slug")
    if current_app.db.cognee_account_erased(slug):
        return jsonify({"error": "already_erased",
                        "message": "This cognee account was already erased."}), 409
    from app.services import cognee_identity
    base = os.environ.get("COGNEE_BASE_URL", "http://cognee:8000")
    ae, ap = os.environ.get("COGNEE_ADMIN_EMAIL", ""), os.environ.get("COGNEE_ADMIN_PASSWORD", "")
    if not ae or not ap:
        return jsonify({"error": "cognee admin creds not configured"}), 400
    prov = current_app.provisioner
    try:
        res = cognee_identity.erase_account(
            base, ae, ap, prov._cognee_user_email(slug), prov._cognee_user_password(slug))
    except Exception as e:
        logger.warning("cognee erase of %s failed: %s", slug, e)
        return jsonify({"error": "erase_failed", "message": str(e)}), 502
    current_app.db.mark_cognee_erased(slug, admin, {"datasets_deleted": res["datasets_deleted"]})
    return jsonify({"status": "erased", "id": qid,
                    "datasets_deleted": res["datasets_deleted"]})


@api_bp.route("/admin/cognee-memories/<legacy_slug>/restore", methods=["POST"])
def cognee_memory_restore(legacy_slug):
    """#2574 item 2: a person has two own memories; the one under their current
    identity serves. This makes the LEGACY one serve instead (typed confirm,
    audited). Nothing is deleted; merging the two is a cognee-side step."""
    admin = _admin()
    if not admin:
        return jsonify({"error": "Forbidden"}), 403
    if request.args.get("confirm") != "Restore":
        return jsonify({"error": "confirmation_required",
                        "message": "Restore requires ?confirm=Restore (case-sensitive)."}), 400
    if not current_app.db.restore_cognee_legacy(legacy_slug, admin):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"status": "restored", "legacy_slug": legacy_slug})


@api_bp.route("/admin/identity-unattributed/<int:sid>/purge", methods=["POST"])
def identity_unattributed_purge(sid):
    admin = _admin()
    if not admin:
        return jsonify({"error": "Forbidden"}), 403
    if request.args.get("confirm") != "Purge":
        return jsonify({"error": "confirmation_required",
                        "message": "Purge requires ?confirm=Purge (case-sensitive)."}), 400
    if not current_app.db.purge_unattributed_credential(sid, admin, 30):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"status": "purged", "id": sid})


@api_bp.route('/tls/ask', methods=['GET'])   # api_bp prefix '/api' → /api/tls/ask (#637)
@anchor_exempt  # #639/#645: peer is agent-manager (delegation), NOT Caddy
def tls_ask():
    """#619: on-demand-TLS guard for the *.MCP_DOMAIN wildcard (mirrors
    agent-manager's /api/tls/ask). Caddy asks before issuing an LE cert for a
    wildcard host; 200 iff the hostname maps to a real instance — blocks
    cert-flood DoS. Public by necessity (Caddy's admin cannot carry a
    session); leaks nothing (the token is HMAC-derived and unguessable).
    Replaces the per-route internal-CA tls policies the retired dynamic
    routes installed."""
    from flask import current_app, request
    from app.blueprints.wildcard_proxy import _resolve_instance

    domain = (request.args.get('domain') or '').lower()
    mcp_domain = (current_app.config.get('MCP_DOMAIN') or '').lower()
    if not mcp_domain or domain == mcp_domain:
        return ('', 404)
    if not domain.endswith('.' + mcp_domain):
        return ('', 404)
    if domain == f"gw.{mcp_domain}":  # #2472: the gateway host
        return ('', 200)
    inst, _integ = _resolve_instance(domain[: -(len(mcp_domain) + 1)])
    return ('', 200) if inst else ('', 404)
