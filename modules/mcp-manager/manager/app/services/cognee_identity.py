# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Per-user Cognee identity provisioning + access-control probe (#36 follow-up).

Two-tier Cognee memory needs REAL per-user isolation, which only holds when the
cognee backend runs with access-control ON. This module:

  * ``backend_access_control_on(base_url, admin_email, admin_password)`` — probes
    the live backend and returns True only when access-control is actually
    enforced (a fresh JWT resolves to a principal WITH an id; access-control-off
    cognee returns only the email). Used by the fail-closed provisioning gate so
    a per-user cognee entry is NEVER provisioned while it would silently leak.

  * ``ensure_user(base_url, admin_email, admin_password, user_email,
    user_password)`` — idempotently registers a per-user cognee account (as a
    sub-user of the admin via the admin JWT) and mints THAT user's own API key,
    which the user's cognee-mcp instance then presents. So the instance
    authenticates AS the user and sees only their own datasets.

  * ``grant_group_read(base_url, admin_*, dataset_id, role_id)`` /
    ``create_company_brain(...)`` — company-brain (shared) support: create a
    shared dataset and grant a role/group READ, so every entitled user can query
    it while WRITE stays with admin/power.

HTTP shapes are the live cognee 1.1.0 API (verified on 0.91):
  POST /api/v1/auth/login                     (form: username,password) -> access_token
  GET  /api/v1/auth/me                         (Bearer) -> {email[, id, tenant_id,...]}
  POST /api/v1/auth/register                   (Bearer) {email,password,is_active,...}
  POST /api/v1/auth/api-keys                    (Bearer) {name} -> {key}
  POST /api/v1/permissions/roles?role_name=X    (Bearer)
  POST /api/v1/permissions/users/{uid}/roles?role_id=Y (Bearer)
  POST /api/v1/permissions/datasets/{principal_id}?permission_name=read  body:[dataset_id]
  POST /api/v1/datasets                         (Bearer) {name}

Nothing here logs credential values.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Indirection so tests can inject a fake HTTP client. Real impl uses httpx.
_http = None


def _client():
    global _http
    if _http is None:
        import httpx
        _http = httpx  # module exposes .request(method, url, **kw)
    return _http


def _login(base_url: str, email: str, password: str) -> str:
    """Return an access_token for (email,password), or '' on failure."""
    r = _client().request(
        "POST", f"{base_url}/api/v1/auth/login",
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if getattr(r, "status_code", 500) >= 400:
        return ""
    try:
        return r.json().get("access_token", "") or ""
    except Exception:
        return ""


def backend_access_control_on(base_url: str, admin_email: str, admin_password: str) -> bool:
    """True only when the cognee backend enforces access control.

    With access-control OFF, cognee's /auth/me returns just {email}; with it ON,
    the authenticated principal resolves WITH an id (and tenant). We treat the
    presence of a principal id as the signal. Fail-safe: any error -> False
    (fail closed — we would rather refuse to provision than leak).
    """
    tok = _login(base_url, admin_email, admin_password)
    if not tok:
        return False
    me = _whoami(base_url, tok)
    return bool(me.get("id") or me.get("tenant_id"))


def _whoami(base_url: str, token: str) -> dict:
    """Resolve the authenticated principal. With access-control ON, cognee's
    /api/v1/users/me returns the user object WITH an id (the principal_id); with
    access-control OFF the /auth/me shim returns only the email. We try
    /users/me first (the id source) and fall back to /auth/me."""
    for path in ("/api/v1/users/me", "/api/v1/auth/me"):
        try:
            r = _client().request("GET", f"{base_url}{path}",
                                  headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if getattr(r, "status_code", 500) < 400:
                return r.json() or {}
        except Exception:
            continue
    return {}


def ensure_user(base_url: str, admin_email: str, admin_password: str,
                user_email: str, user_password: str) -> dict:
    """Idempotently ensure a per-user cognee account and return a usable token.

    Flow (cognee 1.2.2 access-control):
      1. admin logs in (superuser JWT).
      2. register the user (idempotent — 'already exists' is fine). Capture the
         id from the register response when created.
      3. admin PATCHes the user: sets the deterministic password + is_verified
         (fastapi-users register ignores is_verified and may not persist the
         admin-supplied password on an existing row, so PATCH is authoritative).
      4. log in AS the user to obtain their JWT — cognee-mcp presents API_TOKEN as
         `Authorization: Bearer <token>` for self-hosted, and cognee accepts a
         per-user JWT there (an api-key would be rejected on that header). The
         proxy re-provisions on relaunch, refreshing the token.

    Returns {token (JWT), user_email, user_id, scoped_to_user: True}. Raises
    RuntimeError if a per-user token can't be obtained (fail-closed upstream).
    """
    admin_tok = _login(base_url, admin_email, admin_password)
    if not admin_tok:
        raise RuntimeError("cognee admin login failed — cannot provision per-user identity")

    # Register (idempotent).
    r = _client().request(
        "POST", f"{base_url}/api/v1/auth/register",
        json={"email": user_email, "password": user_password,
              "is_active": True, "is_superuser": False, "is_verified": True},
        headers={"Authorization": f"Bearer {admin_tok}", "Content-Type": "application/json"},
        timeout=20,
    )
    code = getattr(r, "status_code", 500)
    user_id = ""
    if code in (200, 201):
        try:
            user_id = str(r.json().get("id", "") or "")
        except Exception:
            user_id = ""
    elif code >= 400:
        try:
            detail = str((r.json() or {}).get("detail", "")).upper()
        except Exception:
            detail = (getattr(r, "text", "") or "").upper()
        if "ALREADY" not in detail and code not in (409,):
            raise RuntimeError(f"cognee register failed for user ({code})")

    # PATCH the user (password + verify) so the deterministic password is
    # authoritative and login is unblocked. Needs the user id; resolve it if the
    # account pre-existed (register returned no id) via the admin whoami-of-user
    # is unavailable, so we look it up through the login-then-me of the user
    # AFTER a first PATCH attempt is impossible — instead resolve by re-register
    # dry run. Practically: when register created the row we have the id; when it
    # already existed we obtain the id by logging in as the user with the SAME
    # deterministic password (a prior provision set it). If that login fails, we
    # PATCH via the id from a users search — cognee lacks it, so we fall through
    # to a best-effort login and, failing that, raise.
    if user_id:
        _client().request(
            "PATCH", f"{base_url}/api/v1/users/{user_id}",
            json={"password": user_password, "is_verified": True},
            headers={"Authorization": f"Bearer {admin_tok}",
                     "Content-Type": "application/json"},
            timeout=20,
        )

    user_tok = _login(base_url, user_email, user_password)
    if not user_tok and not user_id:
        # Pre-existing account whose password diverged and we couldn't get its id
        # to PATCH. Fail closed rather than fall back to an admin-scoped token
        # (that would break per-user isolation).
        raise RuntimeError("cognee per-user account exists but is not usable "
                           "(password divergence); cannot provision isolated memory")
    if not user_tok and user_id:
        # We PATCHed the password above; retry the login once.
        user_tok = _login(base_url, user_email, user_password)
    if not user_tok:
        raise RuntimeError("cognee per-user login failed after provisioning")
    return {"token": user_tok, "user_email": user_email,
            "user_id": user_id, "scoped_to_user": True}


# ---- company brain (shared dataset) ----------------------------------------

def create_company_brain(base_url: str, admin_email: str, admin_password: str,
                         dataset_name: str) -> dict:
    """Create (idempotent) a shared dataset owned by the admin. Returns {id,name}."""
    tok = _login(base_url, admin_email, admin_password)
    if not tok:
        raise RuntimeError("cognee admin login failed")
    r = _client().request(
        "POST", f"{base_url}/api/v1/datasets",
        json={"name": dataset_name},
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        timeout=20,
    )
    if getattr(r, "status_code", 500) >= 400:
        # may already exist — list and find it
        lr = _client().request("GET", f"{base_url}/api/v1/datasets",
                              headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        for d in (lr.json() if getattr(lr, "status_code", 500) < 400 else []):
            if d.get("name") == dataset_name:
                return {"id": d.get("id"), "name": dataset_name}
        raise RuntimeError("cognee company-brain create failed")
    body = r.json()
    return {"id": body.get("id") or body.get("dataset_id"), "name": dataset_name}


def grant_principal_read(base_url: str, admin_email: str, admin_password: str,
                         principal_id: str, dataset_id: str,
                         permission: str = "read") -> bool:
    """Grant a principal (user or role id) a permission on a dataset."""
    tok = _login(base_url, admin_email, admin_password)
    if not tok:
        return False
    r = _client().request(
        "POST",
        f"{base_url}/api/v1/permissions/datasets/{principal_id}?permission_name={permission}",
        json=[dataset_id],
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        timeout=20,
    )
    return getattr(r, "status_code", 500) < 400
