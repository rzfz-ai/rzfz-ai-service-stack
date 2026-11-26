# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""A per-user cognee instance renews its one-hour cognee token here.

POST /instance-auth/cognee-token  {"instance": "<id>"}
     Authorization: Bearer <the instance's renewal secret>

Called by the cognee-authshim inside the instance container over
mcp-network. NOT Caddy-anchored and trusts no identity header: the only
credential is the renewal secret, which the manager handed to that one
container at launch and keeps only as a sha256 (provisioner
renew_cognee_token). The answer is a one-hour token for the account that
instance already acts as, so a holder gains nothing it did not have.
Instance hosts never reach this route (the wildcard hook handles them).
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

instance_auth_bp = Blueprint("instance_auth", __name__)


@instance_auth_bp.route("/instance-auth/cognee-token", methods=["POST"])
def cognee_token():
    auth = request.headers.get("Authorization", "")
    secret = auth[7:].strip() if auth.startswith("Bearer ") else ""
    body = request.get_json(silent=True) or {}
    token, status, error = current_app.provisioner.renew_cognee_token(
        str(body.get("instance") or ""), secret)
    if token:
        resp = jsonify({"token": token})
    else:
        resp = jsonify({"error": error})
        resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    return resp
