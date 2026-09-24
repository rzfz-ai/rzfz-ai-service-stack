# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#559 — forward_auth backend for the fleet hub's /v2 docker API.

Caddy's hub.<domain> site forward_auths every /v2 request here. Docker clients
cannot SSO, so the gate is HTTP Basic against LLM_HUB_REGISTRY_USER /
LLM_HUB_REGISTRY_PASSWORD (minted by `rzfz hub-credentials`).

Why here and not Caddy `basic_auth`: Caddy verifies bcrypt hashes, and a
bcrypt hash stored in .env is mangled by compose v2's env-file interpolation
($2b$10$… parses as variable references). A plain high-entropy hex password is
interpolation-safe, and the comparison lives in code we can test — constant
time via hmac.compare_digest on BOTH fields, no short-circuit.

FAIL-CLOSED: an empty password (unprovisioned box) refuses everything — the
hub edge simply stays shut until the operator mints credentials. In-network
consumers use llm-registry:5000 directly and never pass through this gate.

The 401 always carries WWW-Authenticate: docker's login flow probes /v2/
unauthenticated and only sends credentials after that challenge.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

_CHALLENGE = {"WWW-Authenticate": 'Basic realm="razzfazz-hub"'}


def verify_hub_basic(authorization: Optional[str], user: str, password: str) -> bool:
    """True iff `authorization` is a well-formed Basic header matching
    user:password. Pure + constant-time on both fields; empty password is
    always False (fail-closed while unprovisioned)."""
    if not password:
        return False
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    got_user, sep, got_pass = decoded.partition(":")
    if not sep:
        return False
    # & not `and`: evaluate both digests so a wrong username costs the same
    # time as a wrong password.
    return bool(
        hmac.compare_digest(got_user.encode(), user.encode())
        & hmac.compare_digest(got_pass.encode(), password.encode())
    )


def register_hub_auth_api(app) -> None:
    # NO admin/SSO dependency: this IS the auth check Caddy delegates to.
    # It can only answer 204 or 401 and leaks nothing either way.
    router = APIRouter()

    @router.get("/api/hub-auth")
    def hub_auth(authorization: Optional[str] = Header(default=None)):
        user = os.environ.get("LLM_HUB_REGISTRY_USER") or "fleet"
        password = os.environ.get("LLM_HUB_REGISTRY_PASSWORD") or ""
        if not verify_hub_basic(authorization, user, password):
            raise HTTPException(status_code=401, detail="authentication required",
                                headers=_CHALLENGE)
        return {"status": "ok"}

    app.include_router(router)
