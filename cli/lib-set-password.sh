# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# shellcheck shell=bash
# ==============================================================================
# cli/lib-set-password.sh — shared "set a local-auth password for email X" lib
# ==============================================================================
# #54 (True-SSO epic #33). The ONE implementation of "set the password for a
# given account in a given local-auth backend", reused by BOTH:
#
#   * cli/set-admin-password.sh  (operator CLI — admin accounts)
#   * core/start-portal/password_broker.py  (self-service + admin reset, via
#     `rzfz set-password`, invoked through docker-socket-proxy)
#
# DESIGN — security-sensitive, read this before editing:
#   * The plaintext password is ALWAYS read from STDIN, never from argv. argv is
#     world-readable via /proc/<pid>/cmdline and lands in `ps`, shell history,
#     and audit logs. STDIN does not.
#   * The TARGET is an email address (resolved per-backend), NOT a hardcoded
#     admin user — so the same code path serves any user.
#   * No plaintext is ever echoed, logged, or interpolated into a shell string.
#     It is piped into the relevant container's own hashing routine (Dify
#     pbkdf2 / Cognee user-manager / Authentik set_password API) via an env var
#     or stdin inside `docker exec`, never as a command argument.
#   * SQL is parameterized: the Dify lookup uses psql `-v em=... :'em'` so a
#     hostile email cannot break out of the WHERE clause. The pbkdf2 hash is
#     base64 (safe charset) and computed inside the dify container.
#
# Sourceable, not executable. Idempotent re-source. Requires lib.sh already
# sourced by the caller (for read_env_value / check_container / print_*).
# ==============================================================================

# Sourceable guard — `return` only succeeds inside a sourced file.
(return 0 2>/dev/null) || {
    echo "cli/lib-set-password.sh must be sourced, not executed" >&2
    exit 1
}
[[ -n "${_RZFZ_LIB_SET_PASSWORD_LOADED:-}" ]] && return 0
readonly _RZFZ_LIB_SET_PASSWORD_LOADED=1

# The .env file the helper reads targeted keys from. Callers may override
# RZFZ_SET_PW_ENV_FILE before sourcing; defaults to the repo-root .env (one
# level up from cli/). Inside the broker container the stack .env is mounted at
# /stack/.env.
: "${RZFZ_SET_PW_ENV_FILE:=}"

_setpw_env_file() {
    if [[ -n "${RZFZ_SET_PW_ENV_FILE:-}" ]]; then
        printf '%s\n' "$RZFZ_SET_PW_ENV_FILE"; return 0
    fi
    # Resolve repo-root .env relative to this lib (cli/ -> repo root).
    local here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if [[ -f "$here/.env" ]]; then printf '%s\n' "$here/.env"; return 0; fi
    # In-container fallback.
    if [[ -f /stack/.env ]]; then printf '%s\n' /stack/.env; return 0; fi
    printf '%s\n' "$here/.env"
}

# _setpw_value KEY [default]: resolve a config value, preferring the live
# ENVIRONMENT (the in-stack containers get .env injected via `env_file:`) and
# falling back to a targeted .env file read (NEVER source). The env-first order
# matters on hardened installs where .env is mode 0600 and the container's
# non-root appuser cannot read the mounted /stack/.env — there the env var is
# the only available source (cf. core/start-portal/app.py _read_compose_profiles
# documenting the same 0600 case).
_setpw_value() {
    local key=$1 default=${2:-}
    local v="${!key:-}"
    if [[ -z "$v" ]]; then
        local f; f="$(_setpw_env_file)"; v="$(read_env_value "$f" "$key")"
    fi
    printf '%s\n' "${v:-$default}"
}

_setpw_pg_user() { _setpw_value POSTGRES_USER docker; }

# ──────────────────────────────────────────────────────────────────────────────
# Per-backend apply functions. Each:
#   * takes the target email as $1
#   * reads the plaintext password from STDIN
#   * returns 0 on success, 1 on failure, 2 = container not running (skip)
# Never logs/echoes the password.
# ──────────────────────────────────────────────────────────────────────────────

# Authentik: set a user's password via the REST API (set_password). Reuses the
# exact pattern from scripts/rotate-bootstrap-password.sh but generalised to an
# arbitrary email (resolve pk by email, not username). The token is read from
# .env; for a federated user this CREATES a usable local Authentik password
# (Authentik permits a federated identity to also hold one).
_setpw_apply_authentik() {
    local email=$1 pw; IFS= read -r pw || true
    command -v curl >/dev/null 2>&1 || { print_error "authentik: curl missing"; return 1; }
    command -v python3 >/dev/null 2>&1 || { print_error "authentik: python3 missing"; return 1; }
    local token base
    token="$(_setpw_value AUTHENTIK_BOOTSTRAP_TOKEN)"
    [[ -n "$token" ]] || { print_error "authentik: AUTHENTIK_BOOTSTRAP_TOKEN empty"; return 1; }
    # In-stack containers reach Authentik directly; host callers via MAIN_DOMAIN.
    base="${RZFZ_AUTHENTIK_BASE:-http://authentik-server:9000}"

    # Never `-k`/`--insecure`: these two calls carry the admin bootstrap token
    # and (below) the new plaintext password (#857).
    authentik_cacert_args

    local pk_json pk
    pk_json=$(curl -s --max-time 30 "${RZFZ_AUTHENTIK_CACERT_ARGS[@]}" \
        -H "Authorization: Bearer ${token}" -H "Accept: application/json" \
        "${base}/api/v3/core/users/?email=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$email")" || true)
    [[ -n "$pk_json" ]] || { print_error "authentik: API unreachable at ${base}"; return 1; }
    pk=$(printf '%s' "$pk_json" \
        | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get("results",[]); print(r[0]["pk"] if r else "")' 2>/dev/null || true)
    [[ -n "$pk" ]] || { print_error "authentik: no user with email ${email}"; return 1; }

    # JSON-encode the password (handles quotes/specials) via python; the
    # plaintext is fed on stdin, never as an argv element.
    local body code
    body=$(printf '%s' "$pw" | python3 -c 'import sys,json; sys.stdout.write(json.dumps({"password": sys.stdin.read()}))')
    code=$(printf '%s' "$body" | curl -s --max-time 30 "${RZFZ_AUTHENTIK_CACERT_ARGS[@]}" -o /dev/null -w '%{http_code}' \
        -X POST -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
        --data-binary @- "${base}/api/v3/core/users/${pk}/set_password/" || true)
    [[ "$code" == "204" || "$code" == "200" ]] || { print_error "authentik: set_password HTTP ${code}"; return 1; }
    return 0
}

# Dify: pbkdf2-sha256 (10k) into the `accounts` table. The hash + salt are
# computed inside the dify-api container; the UPDATE targets the row by email
# using a PARAMETERIZED psql variable (:'em') so the email can't inject SQL.
_setpw_apply_dify() {
    local email=$1 pw; IFS= read -r pw || true
    check_container dify-api || return 2
    local db pg
    db="$(_setpw_value DIFY_DB dify_db)"
    pg="$(_setpw_pg_user)"
    # Compute hash+salt inside dify-api; pw fed via env (DPW), not argv.
    local h s
    { read -r h; read -r s; } < <(docker exec -e DPW="$pw" dify-api python3 -c "
import base64, hashlib, binascii, os
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha256', os.environ['DPW'].encode('utf-8'), salt, 10000)
print(base64.b64encode(binascii.hexlify(dk)).decode())
print(base64.b64encode(salt).decode())
" 2>/dev/null)
    [[ -n "$h" && -n "$s" ]] || { print_error "dify: hash computation failed"; return 1; }
    # Parameterized UPDATE: -v binds the values; :'name' emits a safely-quoted
    # SQL literal so the (Authentik-sourced) email can't break out of / inject
    # into the WHERE clause. h/s are base64 (safe charset) but bound too for
    # consistency. CRITICAL: psql's :'var' substitution only works when the SQL
    # is read from a FILE or STDIN — NOT with -c (which is a literal string).
    # So the statement is fed on stdin (here-string) and docker exec needs -i.
    # (Same constraint provision-sso-users.sh documents for its :'em' lookups.)
    docker exec -i postgres psql -v ON_ERROR_STOP=1 -U "$pg" -d "$db" \
        -v em="$email" -v ph="$h" -v ps="$s" \
        <<< "UPDATE accounts SET password = :'ph', password_salt = :'ps' WHERE email = :'em';" \
        >/dev/null 2>&1 || { print_error "dify: UPDATE failed"; return 1; }
    return 0
}

# Cognee: set the password through fastapi-users' OWN user-manager (.update),
# NEVER hand-hash. Storage-agnostic; the target user is resolved by email.
# Password fed via env (CGPW), not argv. (Mirrors apply_cognee in
# set-admin-password.sh, but parameterised on email.)
_setpw_apply_cognee() {
    local email=$1 pw; IFS= read -r pw || true
    check_container cognee || return 2
    local pyscript
    pyscript="$(cat <<'PY'
import asyncio, os
from cognee.infrastructure.databases.relational import get_relational_engine
from cognee.modules.users.get_user_manager import get_user_manager_context
from cognee.modules.users.get_user_db import get_user_db_context
from cognee.modules.users.models.User import UserUpdate

async def main():
    email = os.environ["CGEMAIL"]
    engine = get_relational_engine()
    async with engine.get_async_session() as session:
        async with get_user_db_context(session) as user_db:
            async with get_user_manager_context(user_db) as user_manager:
                user = await user_db.get_by_email(email)
                if user is None:
                    raise SystemExit(3)
                await user_manager.update(UserUpdate(password=os.environ["CGPW"]), user)

asyncio.run(main())
PY
)"
    docker exec -e CGPW="$pw" -e CGEMAIL="$email" cognee python3 -c "$pyscript" >/dev/null 2>&1 \
        || { print_error "cognee: user-manager update failed (user missing? rc above)"; return 1; }
    return 0
}

# ──────────────────────────────────────────────────────────────────────────────
# Public entry point.
#
#   rzfz_set_password_for_app <app> <email>     # password on STDIN
#
# <app> ∈ { authentik, dify, cognee }. Returns the apply function's rc
# (0 ok / 1 fail / 2 container-not-running).
# ──────────────────────────────────────────────────────────────────────────────
rzfz_set_password_for_app() {
    local app=$1 email=$2
    [[ -n "$app" && -n "$email" ]] || { print_error "set-password: app + email required"; return 1; }
    case "$app" in
        authentik) _setpw_apply_authentik "$email" ;;
        dify)      _setpw_apply_dify      "$email" ;;
        cognee)    _setpw_apply_cognee    "$email" ;;
        *) print_error "set-password: unknown app '$app' (authentik|dify|cognee)"; return 1 ;;
    esac
}
