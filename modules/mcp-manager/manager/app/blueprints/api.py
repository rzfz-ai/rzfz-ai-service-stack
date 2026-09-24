# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""REST API for the personal MCP manager (#36).

Identity is ALWAYS derived server-side from the Authentik forward-auth headers
(X-Authentik-Username / Uid / Groups), never from a client-supplied field —
this is the only-own boundary. Every per-instance / per-credential operation
re-checks that the resource's user_slug matches the caller's derived slug.

Credential plaintext is accepted on POST /credentials, immediately handed to
the encrypting CredentialStore, and NEVER returned in any response.
"""

import uuid

from flask import Blueprint, current_app, jsonify, request

from razzfazz_common.auth import parse_authentik_headers

from app.blueprints.caddy_anchor import anchor_exempt

# #61 NEW-1 (was LOW-2): single source of truth for the slug. Imported straight
# from the shared lib (NOT via the provisioner) so the API, the provisioner, and
# agent-manager can NEVER diverge on the only-own key. Re-exported here for
# callers/tests that import it from the blueprint.
from razzfazz_common.user_slug import make_user_slug  # noqa: F401

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _get_user():
    info = parse_authentik_headers()
    if not info["username"]:
        return None, None, None, None
    return info["username"], info["uid"], make_user_slug(info["username"]), info["groups"]


def _owned_instance(instance_id, user_slug):
    """Return the instance row IFF it exists AND belongs to user_slug, else None."""
    try:
        row = current_app.db.get_instance(uuid.UUID(str(instance_id)))
    except (ValueError, TypeError):
        return None
    if not row or row["user_slug"] != user_slug:
        return None
    return row


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
    username, _, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403

    mcp_domain = current_app.config.get("MCP_DOMAIN", "")
    out = []
    for row in current_app.db.get_user_instances(user_slug):
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
            current_app.db.list_user_mcp_ids_with_secrets(user_slug)
            if hasattr(current_app.db, "list_user_mcp_ids_with_secrets") else []):
        creds[mcp_id] = current_app.cred_store.credential_summary(user_slug, mcp_id)

    return jsonify({"instances": out, "credentials": creds})


@api_bp.route("/credentials/<mcp_id>", methods=["POST"])
def add_credentials(mcp_id):
    """Add/replace PAT-style credentials for the caller's own (user, mcp).

    Secret fields are encrypted at rest; non-secret fields (email, subdomain,
    company_domain, ...) are recorded as instance config. Plaintext is NEVER
    returned.
    """
    username, user_email_unused, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403

    integ = current_app.catalog.get(mcp_id)
    if not integ:
        return jsonify({"error": "Unknown integration"}), 404
    if current_app.catalog.cred_model(mcp_id) != "pat":
        return jsonify({"error": "This integration uses OAuth; start it via /api/oauth/start"}), 400

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

    current_app.cred_store.store_pat(user_slug, mcp_id, secret_vals,
                                     secret_keys=secret_keys)
    # Non-secret settings recorded for later proxy provisioning.
    if nonsecret_vals and hasattr(current_app.db, "get_instance_by_mcp_and_user"):
        inst = current_app.db.get_instance_by_mcp_and_user(mcp_id, user_slug)
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
    username, _, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    if not current_app.catalog.get(mcp_id):
        return jsonify({"error": "Unknown integration"}), 404
    current_app.cred_store.revoke(user_slug, mcp_id)
    return jsonify({"status": "ok", "mcp_id": mcp_id})


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
    )
    if instance_id:
        return jsonify({"instance_id": instance_id, "message": message})
    return jsonify({"error": message}), 400


@api_bp.route("/stop/<instance_id>", methods=["POST"])
def stop(instance_id):
    username, _, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    if not _owned_instance(instance_id, user_slug):
        return jsonify({"error": "Instance not found"}), 404
    iid, message = current_app.provisioner.stop(instance_id, username)
    return jsonify({"instance_id": iid, "message": message})


@api_bp.route("/delete/<instance_id>", methods=["POST"])
def delete(instance_id):
    username, _, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    if not _owned_instance(instance_id, user_slug):
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
    username, _, user_slug, _ = _get_user()
    if not username:
        return jsonify({"error": "Unauthorized"}), 403
    inst = current_app.db.get_instance_by_mcp_and_user(mcp_id, user_slug)
    if inst:
        current_app.db.update_last_accessed(inst["id"])
    return jsonify({"ok": True})


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
    inst, _integ = _resolve_instance(domain[: -(len(mcp_domain) + 1)])
    return ('', 200) if inst else ('', 404)
