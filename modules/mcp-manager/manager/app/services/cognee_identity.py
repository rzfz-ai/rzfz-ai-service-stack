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

HTTP shapes are the live cognee 1.1.0 API (verified on a test box):
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


class CogneeAccountDeactivated(RuntimeError):
    """The per-user cognee account was disabled by a control other than this
    module. The message is credential-free, so the renewal path may show and
    log it; every other failure keeps the generic type-name-only answer.
    Never re-activated from here."""


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
    # deterministic password (a prior provision set it). If that login fails,
    # _recover_existing reads the id through the admin-only
    # POST /api/v1/users/get-user-id and PATCHes the derived password on that
    # same account; anything it cannot confirm raises (fail closed).
    if user_id:
        _set_password(base_url, admin_tok, user_id, user_password)

    user_tok = _login(base_url, user_email, user_password)
    if not user_tok and not user_id:
        # The account existed already and its password is not the derived one
        # (the "password divergence" a relaunch used to stop at). The account
        # under this derived address is the caller's by construction, so the
        # admin path sets the derived password again on THAT account: never a
        # second account, never an admin-scoped token.
        user_id = _recover_existing(base_url, admin_tok, admin_email,
                                    user_email, user_password)
        user_tok = _login(base_url, user_email, user_password)
    elif not user_tok and user_id:
        # We PATCHed the password above; retry the login once.
        user_tok = _login(base_url, user_email, user_password)
    if not user_tok:
        raise RuntimeError("cognee per-user login failed after provisioning")
    return {"token": user_tok, "user_email": user_email,
            "user_id": user_id, "scoped_to_user": True}


def _set_password(base_url: str, admin_tok: str, user_id: str, password: str) -> bool:
    r = _client().request(
        "PATCH", f"{base_url}/api/v1/users/{user_id}",
        json={"password": password, "is_verified": True},
        headers={"Authorization": f"Bearer {admin_tok}",
                 "Content-Type": "application/json"},
        timeout=20,
    )
    return getattr(r, "status_code", 500) < 300


def _recover_existing(base_url: str, admin_tok: str, admin_email: str,
                      user_email: str, user_password: str) -> str:
    """Set the derived password on the EXISTING account `user_email` through
    the admin path and return its id. Raises (fail closed) when cognee cannot
    name the account, when the address is a superuser or the admin itself,
    or when the reset is refused. Nothing is created."""
    divergence = ("cognee per-user account exists but is not usable (password "
                  "divergence) and could not be recovered: ")
    if (user_email or "").strip().lower() == (admin_email or "").strip().lower():
        raise RuntimeError(divergence + "it is the admin account")
    try:
        r = _client().request(
            "POST", f"{base_url}/api/v1/users/get-user-id", json={"email": user_email},
            headers={"Authorization": f"Bearer {admin_tok}",
                     "Content-Type": "application/json"},
            timeout=15)
        user_id = (str((r.json() or {}).get("user_id") or "")
                   if getattr(r, "status_code", 500) < 300 else "")
    except Exception as e:  # noqa: BLE001 — never a fallback, only the reason
        raise RuntimeError(divergence + f"its id could not be read ({type(e).__name__})") from e
    if not user_id:
        raise RuntimeError(divergence + "cognee did not return its id")
    try:
        u = _client().request("GET", f"{base_url}/api/v1/users/{user_id}",
                              headers={"Authorization": f"Bearer {admin_tok}"}, timeout=15)
        row = (u.json() or {}) if getattr(u, "status_code", 500) < 300 else None
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        raise RuntimeError(divergence + "the account could not be read")
    if row.get("is_superuser") or \
            str(row.get("email") or "").strip().lower() != user_email.strip().lower():
        raise RuntimeError(divergence + "the id cognee returned is not this per-user account")
    if not _set_password(base_url, admin_tok, user_id, user_password):
        raise RuntimeError(divergence + "the admin password reset was refused")
    if row.get("is_active") is False:
        # Never re-activated from here: another control disabled it.
        raise CogneeAccountDeactivated(
            "the cognee account behind this personal memory is deactivated; "
            "an administrator must re-activate it in cognee")
    logger.warning("cognee: the per-user account %s had a diverged password; reset it "
                   "through the admin path", user_id)
    return user_id


def user_token(base_url: str, admin_email: str, admin_password: str,
               user_email: str, user_password: str) -> dict:
    """A fresh JWT for an EXISTING per-user account: {token, user_id}.

    A plain login first (no admin call); when that fails, ensure_user, which
    recovers a diverged password on the same account. Used to renew the token
    of a running instance: cognee's JWTs live one hour (JWT_LIFETIME_SECONDS)."""
    tok = _login(base_url, user_email, user_password)
    if tok:
        return {"token": tok, "user_id": str(token_claims(tok).get("sub") or "")}
    res = ensure_user(base_url, admin_email, admin_password, user_email, user_password)
    return {"token": res["token"], "user_id": res.get("user_id", "")}


def token_claims(token: str) -> dict:
    """The UNVERIFIED payload of a cognee JWT: `sub` is the cognee user id,
    `exp` the expiry. {} for anything that is not a JWT. Used only to see
    which account a running instance holds, never to authorise anything."""
    import base64
    import json
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


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


def find_dataset_id(base_url: str, admin_email: str, admin_password: str,
                    dataset_name: str):
    """The id of the admin-visible dataset named `dataset_name`, or None
    (never creates one). Raises when cognee cannot be asked."""
    tok = _login(base_url, admin_email, admin_password)
    if not tok:
        raise RuntimeError("cognee admin login failed")
    r = _client().request("GET", f"{base_url}/api/v1/datasets",
                          headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    if getattr(r, "status_code", 500) >= 400:
        raise RuntimeError("cognee dataset listing failed")
    hits = [d.get("id") for d in (r.json() or []) if isinstance(d, dict)
            and d.get("name") == dataset_name and d.get("id")]
    return hits[0] if len(hits) == 1 else None


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


# ---- erase one account and its memory (#2573) -------------------------------
#
# Cognee 1.5.3 API (read from the package's routers, not guessed):
#   GET    /api/v1/datasets          (Bearer) -> the caller's datasets
#   DELETE /api/v1/datasets          (Bearer) deletes ALL datasets the caller
#                                    owns and everything derived from them
#                                    (graph, vectors, relational rows, sessions)
#   DELETE /api/v1/users/{id}        (Bearer, superuser) fastapi-users route
# Dataset deletion acts as the authenticated user, so the datasets are deleted
# as the account itself (its credentials are derived by the manager), and only
# then does the admin delete the account.

def _ok(r) -> bool:
    return getattr(r, "status_code", 500) < 300


def erase_account(base_url: str, admin_email: str, admin_password: str,
                  user_email: str, user_password: str) -> dict:
    """Delete one cognee account's datasets, then the account.

    Returns {"datasets_deleted": n, "user_id": id}. Raises RuntimeError at the
    first step that fails, BEFORE any later step runs: the account is only
    deleted once its datasets are verified gone (deleting the account first
    would orphan the data where nobody can reach it)."""
    admin_tok = _login(base_url, admin_email, admin_password)
    if not admin_tok:
        raise RuntimeError("cognee admin login failed")
    user_tok = _login(base_url, user_email, user_password)
    if not user_tok:
        raise RuntimeError("cannot log in as the cognee account (already erased, or its "
                           "password differs) - nothing was deleted")
    user_id = str(_whoami(base_url, user_tok).get("id") or "")
    if not user_id:
        raise RuntimeError("cognee did not return the account's id - nothing was deleted")
    bearer = {"Authorization": f"Bearer {user_tok}"}

    lr = _client().request("GET", f"{base_url}/api/v1/datasets", headers=bearer, timeout=30)
    if not _ok(lr):
        raise RuntimeError("could not list the account's datasets - nothing was deleted")
    count = len(lr.json() or [])
    if count:
        dr = _client().request("DELETE", f"{base_url}/api/v1/datasets", headers=bearer, timeout=600)
        if not _ok(dr):
            raise RuntimeError(f"deleting the account's datasets failed ({dr.status_code}); "
                               "the account was not deleted")
        left = _client().request("GET", f"{base_url}/api/v1/datasets", headers=bearer, timeout=30)
        if not _ok(left) or (left.json() or []):
            raise RuntimeError("datasets remain after the delete; the account was not deleted")

    ur = _client().request("DELETE", f"{base_url}/api/v1/users/{user_id}",
                           headers={"Authorization": f"Bearer {admin_tok}"}, timeout=60)
    if not _ok(ur):
        raise RuntimeError(f"datasets deleted, but deleting the account failed ({ur.status_code})")
    return {"datasets_deleted": count, "user_id": user_id}
