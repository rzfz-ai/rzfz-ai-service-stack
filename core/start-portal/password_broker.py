# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Central password broker (#54) — True-SSO epic #33.

A blueprint on the start-portal Flask app. The SSO-authenticated user sets/
changes a local-auth password; the broker intercepts the set-password moment
(the only time plaintext exists) and fans it out to every backend that holds a
local credential:

  * Authentik  (set-password REST API)        — optional for self-service
  * Dify       (pbkdf2 into the accounts table)
  * Cognee     (fastapi-users user-manager)

Net model (issue #54): Authentik is the identity source; OIDC apps (OWUI/Gitea)
need no local password; the local-auth apps (Dify/Cognee) get their password
set/propagated here. Federated (Google/Entra) users have no local Authentik
password — for them the broker still writes the local-auth apps, and offers an
opt-in "also set a local Authentik password" path (Authentik permits a
federated identity to hold a local password too).

SECURITY (this is the sensitive part):
  * Identity comes STRICTLY from the X-Authentik-* forward-auth headers
    (g.user). A client-supplied "whose password" field is never trusted for
    self-service.
  * Only-own-password unless the caller is in the Authentik admin group
    (g.user['is_admin'], derived server-side from X-Authentik-Groups). Admins
    may pass target_email to reset another user.
  * Plaintext is read from the form, passed to the helper on STDIN, and never
    logged, echoed, or returned. The helper (cli/set-password.sh) runs via the
    docker-socket-proxy and itself never puts the password in argv.
  * CSRF protected (shared enable_csrf); POST-only for state change; basic
    password-policy validation.
  * Fail closed: no Authentik username header → deny.
"""

from __future__ import annotations

import logging
import os
import subprocess

import requests
from flask import Blueprint, g, jsonify, render_template, request

from razzfazz_common.csrf import verify_csrf_token

logger = logging.getLogger(__name__)

password_broker = Blueprint("password_broker", __name__)

# Backends the broker always fans a local-auth password out to.
LOCAL_AUTH_APPS = ("dify", "cognee")

# Minimum password length (policy). Kept conservative; the upstream apps have
# their own policies too. NB: the modal's live rules-checklist mirrors THIS
# exact threshold + the complexity rule in _validate() — keep them in sync.
MIN_PASSWORD_LEN = 12

ADMIN_GROUP = "authentik Admins"
SUPER_ADMIN_GROUP = "razzfazz.ai Super Admins"

# Where the rzfz set-password helper lives inside the container. The portal
# image copies the cli/ + scripts/ tree to /app/cli + /app/scripts; override
# via RZFZ_SETPW_CMD for tests / alternate layouts.
SETPW_CMD = os.environ.get("RZFZ_SETPW_CMD", "/app/cli/set-password.sh")

# Authentik API base for the federation probe. In-stack the broker reaches the
# server container directly; the token is the same bootstrap token the
# set-password helper uses (injected via env_file: ../.env).
AUTHENTIK_BASE = os.environ.get("RZFZ_AUTHENTIK_BASE", "http://authentik-server:9000")


def _is_admin(user: dict) -> bool:
    groups = user.get("groups", []) or []
    return ADMIN_GROUP in groups or SUPER_ADMIN_GROUP in groups


def _is_federated(email: str) -> bool:
    """Is the Authentik user with this email a FEDERATED identity?

    A federated user signed in through an external IdP (Google/Entra/etc.),
    represented in Authentik as a non-builtin *source* on the user object.
    A purely local user has an empty `sources` list. We query the Authentik
    REST API (`/api/v3/core/users/?email=`) with the bootstrap token and
    inspect the first matching user's `sources`.

    Behaviour matters for the #54 default-Authentik fix:
      * LOCAL user  → Authentik is their real login → update it by DEFAULT.
      * FEDERATED   → don't silently mint a local Authentik password; the
                      modal offers it as an explicit opt-in instead.

    Fail-safe: if the probe can't run (no token, API unreachable, no match)
    we return False = "treat as local". The common deployment is local
    Authentik, and for a federated user the worst case is that the broker
    *includes* Authentik in the default fan-out — which only ADDS a usable
    local password (Authentik permits a federated identity to hold one);
    it never breaks the federated login. Returning False therefore biases
    toward the key fix (the SSO login actually changing) without ever
    locking anyone out. The modal still shows the federated opt-in based on
    this same probe, so the UI and the server agree.
    """
    token = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN", "")
    if not token or not email:
        return False
    try:
        resp = requests.get(
            f"{AUTHENTIK_BASE}/api/v3/core/users/?email={requests.utils.quote(email)}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=5,
        )
        if getattr(resp, "status_code", 200) != 200:
            return False
        results = (resp.json() or {}).get("results", []) or []
        if not results:
            return False
        sources = results[0].get("sources", []) or []
        return len(sources) > 0
    except Exception as e:  # noqa: BLE001 — never let a probe failure 500 the form
        logger.info("federation probe failed for a user (treating as local): %s", e)
        return False


# Authentik flow-executor — slug of the default authentication flow. The
# headless executor runs the SAME identification→password stages a browser
# login uses, so a successful password stage proves the credential is valid
# against Authentik itself (the identity source) — NOT against a possibly-stale
# downstream hash.
AUTHENTIK_AUTH_FLOW = os.environ.get(
    "RZFZ_AUTHENTIK_AUTH_FLOW", "default-authentication-flow"
)


def _verify_current_password(username: str, current_pw: str) -> bool:
    """Verify `current_pw` for `username` against AUTHENTIK — fail-closed.

    Security-critical (Fix 2, #54). We drive Authentik's headless flow-executor
    through the real default-authentication-flow:

      1. GET  /api/v3/flows/executor/<flow>/?query=   → identification stage
      2. POST {uid_field: username}                   → advances to password stage
      3. POST {password: current_pw}                  →
           * WRONG    : HTTP 200, JSON body with response_errors.password
                        (component stays "ak-stage-password")  → return False
           * CORRECT  : HTTP 302 redirect (flow advances past the password
                        stage) OR HTTP 200 with NO password error → return True

    This contract was confirmed empirically against a live Authentik
    (2026.x) on the reference box. We submit ONLY identification + password and
    stop — we do NOT complete any later MFA/login stage, so this never mints a
    session and never depends on the user's MFA state.

    Properties:
      * Verifies against the IDENTITY SOURCE (Authentik), so an out-of-band
        Authentik password change is honoured (the trap called out in #54).
      * No bootstrap token needed — this is the public login flow. (We pass no
        Authorization header; a fresh anonymous executor session is used.)
      * Plaintext is sent only in the POST body to the in-stack Authentik over
        the internal network; never logged, never put in argv, never echoed.
      * FAIL-CLOSED: empty password, any exception, a non-reachable executor,
        an unexpected response shape, or "couldn't get to the password stage"
        all return False (deny). The ONLY True path is an unambiguous
        password-stage pass.
    """
    if not username or not current_pw:
        return False
    base = AUTHENTIK_BASE.rstrip("/")
    url = f"{base}/api/v3/flows/executor/{AUTHENTIK_AUTH_FLOW}/?query="
    hdrs = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        sess = requests.Session()
        # 1. Prime the flow (sets the executor's flow-plan cookie) + read the
        #    identification stage.
        r0 = sess.get(url, headers={"Accept": "application/json"}, timeout=8)
        comp0 = ""
        try:
            comp0 = (r0.json() or {}).get("component", "")
        except Exception:  # noqa: BLE001
            comp0 = ""
        if comp0 and comp0 != "ak-stage-identification":
            # Flow doesn't start where we expect (custom flow). Fail closed.
            logger.warning("pw-verify: unexpected first stage %r — denying", comp0)
            return False

        # 2. Submit the identification (username). The executor 302s; re-GET to
        #    read the next stage.
        sess.post(url, headers=hdrs, json={"uid_field": username},
                  timeout=8, allow_redirects=False)
        r_after = sess.get(url, headers={"Accept": "application/json"}, timeout=8)
        try:
            comp_after = (r_after.json() or {}).get("component", "")
        except Exception:  # noqa: BLE001
            comp_after = ""
        if comp_after != "ak-stage-password":
            # Could not reach the password stage (unknown user, flow diverged).
            # Deny — we cannot positively verify the password.
            logger.warning("pw-verify: not at password stage (%r) — denying", comp_after)
            return False

        # 3. Submit the password. CORRECT advances (302); WRONG re-renders the
        #    password stage with response_errors.password.
        r_pw = sess.post(url, headers=hdrs, json={"password": current_pw},
                         timeout=8, allow_redirects=False)
        status = getattr(r_pw, "status_code", 0)
        if status in (301, 302, 303, 307, 308):
            return True  # flow advanced past the password stage ⇒ correct
        if status == 200:
            try:
                body = r_pw.json() or {}
            except Exception:  # noqa: BLE001
                # 200 with a non-JSON body is not a recognised "wrong" signal;
                # fail closed rather than guess.
                logger.warning("pw-verify: 200 non-JSON password response — denying")
                return False
            errs = (body.get("response_errors") or {})
            if errs.get("password"):
                return False  # explicit invalid-password error ⇒ wrong
            comp_pw = body.get("component", "")
            # 200 with NO password error and the stage has changed ⇒ accepted.
            if comp_pw and comp_pw != "ak-stage-password":
                return True
            # Still on the password stage with no error is ambiguous → deny.
            logger.warning("pw-verify: ambiguous password response (%r) — denying", comp_pw)
            return False
        # Any other status (4xx/5xx) → deny.
        logger.warning("pw-verify: unexpected HTTP %s — denying", status)
        return False
    except Exception as e:  # noqa: BLE001 — never log the password; fail closed
        logger.warning("pw-verify: verification could not run (%s) — denying", type(e).__name__)
        return False


def _run_helper(app: str, email: str, password: str) -> tuple[bool, str]:
    """Invoke the shared set-password helper for one backend.

    The plaintext is written to the helper's STDIN — never as an argument, so
    it can't leak via /proc, ps, or this process's argv. Returns (ok, message).
    Never logs the password.
    """
    try:
        proc = subprocess.run(
            [SETPW_CMD, "--app", app, "--email", email],
            input=password,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        logger.error("set-password helper not found at %s", SETPW_CMD)
        return False, "password helper unavailable"
    except subprocess.TimeoutExpired:
        logger.error("set-password helper timed out for app=%s", app)
        return False, "timeout"
    if proc.returncode == 0:
        return True, "ok"
    if proc.returncode == 2:
        # Container not running — skipped, not a hard failure.
        logger.info("set-password: %s not running (skipped)", app)
        return True, "skipped (not running)"
    # Log stderr but NOT stdin/password. stderr from the helper never contains
    # the plaintext (the helper is careful), but trim it defensively.
    logger.warning("set-password failed app=%s rc=%s", app, proc.returncode)
    return False, f"{app}: failed (rc={proc.returncode})"


def _validate(new_pw: str, confirm: str) -> str | None:
    """Return an error string, or None when the password is acceptable."""
    if not new_pw:
        return "Password is required."
    if new_pw != confirm:
        return "Passwords do not match."
    if len(new_pw) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    # Basic complexity: at least one letter and one non-letter.
    if new_pw.isalpha() or new_pw.isdigit():
        return "Password must mix letters with digits or symbols."
    return None


def _fan_out(target_email: str, password: str, set_authentik: bool) -> list[dict]:
    """Set `password` for `target_email` in the local-auth apps (+ optionally
    Authentik). Returns a per-app result list (no plaintext)."""
    apps = list(LOCAL_AUTH_APPS)
    if set_authentik:
        apps.append("authentik")
    results = []
    for app in apps:
        ok, msg = _run_helper(app, target_email, password)
        results.append({"app": app, "ok": ok, "message": msg})
    return results


@password_broker.route("/password", methods=["GET"])
def password_form():
    user = g.get("user") or {}
    if not user.get("username"):
        # Fail closed — no SSO identity.
        return ("Unauthorized: Authentik identity required", 401)
    federated = _is_federated((user.get("email") or "").strip())
    return render_template(
        "password.html",
        user=user,
        is_admin=_is_admin(user),
        is_federated=federated,
        results=None,
        error=None,
        notice=None,
    )


@password_broker.route("/password", methods=["POST"])
def password_set():
    user = g.get("user") or {}
    # Fail closed: identity strictly from the SSO headers.
    if not user.get("username"):
        return ("Unauthorized: Authentik identity required", 401)

    # CSRF: verify the form/header token against the session value. abort(403)
    # on mismatch. (Global verification is intentionally off — see app.py — so
    # we verify explicitly here, the one state-changing surface.)
    verify_csrf_token()

    acting_email = (user.get("email") or "").strip()
    is_admin = _is_admin(user)

    # Resolve the TARGET. Self-service is always the caller's own email,
    # derived from the headers — a client-supplied target is honoured ONLY for
    # an admin caller. Non-admins are forced to their own identity (defense in
    # depth: even if the form carries target_email, it is ignored).
    requested_target = (request.form.get("target_email") or "").strip()
    if is_admin and requested_target:
        target_email = requested_target
    else:
        target_email = acting_email

    if not target_email:
        return (
            render_template(
                "password.html", user=user, is_admin=is_admin, results=None,
                error="No target identity could be established.", notice=None,
            ),
            400,
        )

    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    current_pw = request.form.get("current_password", "")
    opt_in_authentik = request.form.get("set_authentik") in ("on", "true", "1", "yes")

    # Is this a self-service change (caller changing their OWN password)? Only
    # then do we verify the current password. An admin resetting ANOTHER user is
    # an override (gated by the admin group) and supplies no current password.
    is_self_service = (target_email == acting_email)

    # Does the caller want a JSON (fetch / modal) response, or the HTML page
    # (no-JS fallback)? Honour an explicit Accept: application/json.
    wants_json = "application/json" in (request.headers.get("Accept", "") or "")

    def _respond_error(msg, code=400):
        if wants_json:
            return jsonify({"ok": False, "error": msg, "results": []}), code
        return (
            render_template(
                "password.html", user=user, is_admin=is_admin,
                is_federated=_is_federated(target_email),
                results=None, error=msg, notice=None,
            ),
            code,
        )

    err = _validate(new_pw, confirm)
    if err:
        # NB: never echo new_pw back into the template.
        return _respond_error(err)

    # #54 KEY BEHAVIOUR: Authentik IS the user's sign-in identity, so a normal
    # "change password" must update it. For a LOCAL Authentik user we therefore
    # include Authentik in the fan-out BY DEFAULT (covers OWUI/Gitea/GPUStack/
    # all SSO apps). For a FEDERATED user (external IdP) we do NOT silently mint
    # a local password — Authentik is included only when the user explicitly
    # opts in via the modal checkbox.
    federated = _is_federated(target_email)
    set_authentik = opt_in_authentik if federated else True

    # ── Fix 2 (#54): verify the CURRENT password BEFORE changing anything ──────
    # A LOCAL self-service NON-ADMIN caller must prove they know their current
    # password.
    #   * Federated self-service: no local Authentik password to verify — they
    #     re-authenticated via SSO to reach this broker. Skip.
    #   * Admin (resetting another user OR changing their own): admin override,
    #     group-gated server-side. Skip. (Matches the template, which hides the
    #     field whenever is_admin — so an admin is never asked for it and must
    #     never be blocked for not supplying it.)
    # Verification is fail-closed: a wrong/empty current password, or a
    # verification that cannot run, denies the change and touches NOTHING.
    if is_self_service and not federated and not is_admin:
        verify_username = user.get("username") or ""
        try:
            current_ok = _verify_current_password(verify_username, current_pw)
        except Exception:  # noqa: BLE001 — never leak the password; fail closed
            logger.warning("password broker: current-pw verification raised for actor=%s "
                           "— denying (fail-closed)", who if (who := user.get("username")) else "?")
            current_ok = False
        if not current_ok:
            # Don't distinguish "wrong" from "couldn't verify" to the client
            # beyond an actionable message; never echo the current password.
            return _respond_error(
                "Current password is incorrect, or it could not be verified. "
                "Nothing was changed.",
                code=403,
            )

    # Audit (no plaintext): who set whose password, and which backends.
    who = user.get("username")
    logger.info(
        "password broker: actor=%s admin=%s target=%s federated=%s authentik=%s",
        who, is_admin, target_email, federated, set_authentik,
    )

    results = _fan_out(target_email, new_pw, set_authentik)
    # Drop the plaintext references promptly.
    del new_pw, confirm, current_pw

    all_ok = all(r["ok"] for r in results)
    if all_ok:
        notice = (
            "Sign-in password updated ✓ — used for OpenWebUI, Gitea, "
            "GPUStack and all SSO apps. Synced to Dify and Cognee."
            if set_authentik else
            "Password updated for the local-auth apps (Dify, Cognee). "
            "Your federated sign-in is unchanged."
        )
    else:
        notice = "Some targets could not be updated — see details below."

    if wants_json:
        return jsonify({"ok": all_ok, "notice": notice, "results": results,
                        "set_authentik": set_authentik})
    return render_template(
        "password.html", user=user, is_admin=is_admin, is_federated=federated,
        results=results, error=None, notice=notice,
    )
