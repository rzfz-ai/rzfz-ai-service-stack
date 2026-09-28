#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/rotate-bootstrap-password.sh
# Single-script rotation of the Authentik bootstrap admin password.
#
# Why this script exists
# ----------------------
# The bootstrap admin password (akadmin) is the single recovery secret on
# the device sticker. Rotating it correctly is currently a 3-step manual
# operator dance (docs/security-architecture.md §16):
#
#   1. Log in to https://auth.<domain>, navigate to Directory → Users →
#      akadmin → Set Password.
#   2. Edit .env BACKUP_ENCRYPTION_PASSWORD.
#   3. docker compose up -d --force-recreate backup-service.
#
# Easy to skip step 2 or 3, leaving the customer with a sticker that
# decrypts no future backup. This script collapses all three steps into
# one idempotent invocation. See B-2 / R-DEF-02.
#
# What it touches (in order)
# --------------------------
#   1. Authentik DB — POST /api/v3/core/users/<pk>/set_password/
#      (the value Authentik actually checks at login time; .env is
#      ignored after first init — §8 caveat).
#   2. .env AUTHENTIK_BOOTSTRAP_PASSWORD — for source-of-truth /
#      sticker / handover-doc consistency.
#   3. .env BACKUP_ENCRYPTION_PASSWORD — IFF it currently equals the
#      OLD AUTHENTIK_BOOTSTRAP_PASSWORD (the sticker single-secret
#      pattern). If the operator already customized it, leave alone.
#   4. docker compose up -d --force-recreate backup-service — refresh
#      the cached GPG_PASSPHRASE per §11 caveat (and matching memory).
#
# Idempotency contract (BSB-02-DEC-03)
# ------------------------------------
# Running twice with the same password is a safe no-op:
#   - if .env already holds the supplied password, we still call the
#     Authentik API (in case .env and Authentik DB drifted), but skip
#     the env writes and the backup-service force-recreate.
#   - exit 0 with a clear "no-op" message.
#
# Per-module CLIENT_SECRET cascade (BSB-02-DEC-01)
# ------------------------------------------------
# A survey of razzfazz-init.sh confirmed that no per-module
# *_CLIENT_SECRET is currently derived from AUTHENTIK_BOOTSTRAP_PASSWORD
# — they are independently `generate_hex_secret 32`'d. So the cascade
# scope from the spec ("per-module *_CLIENT_SECRETs originally seeded
# from the bootstrap") is currently a NULL set. The script is
# structured so that future cascade entries plug in here cleanly.
#
# Usage
# -----
#   scripts/rotate-bootstrap-password.sh                # generate 32-char strong pw
#   scripts/rotate-bootstrap-password.sh '<NEW_PW>'     # use the supplied pw
#   scripts/rotate-bootstrap-password.sh --help
#
# Test hooks (do nothing in production)
# -------------------------------------
#   RAZZFAZZ_TEST_DRY_RUN=1                short-circuit docker / curl
#   RAZZFAZZ_TEST_FAKE_ENV_FILE=<path>     read/write this .env (test fixture)
#   RAZZFAZZ_TEST_FAKE_AUTHENTIK_OK=1      pretend the API call returned 204
#   RAZZFAZZ_TEST_FAKE_AUTHENTIK_FAIL=msg  pretend the API call returned the
#                                          named failure (script exits 1)
#
# Exit codes
# ----------
#   0   rotation completed (or idempotent no-op)
#   1   any failure (API unreachable, weak pw, missing token, .env not found)
# =============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=lib.sh disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

# ---- Help ------------------------------------------------------------------
show_help() {
    cat <<'EOF'
scripts/rotate-bootstrap-password.sh — rotate the Authentik bootstrap admin
password in one shot, with cascade to BACKUP_ENCRYPTION_PASSWORD and
backup-service force-recreate.

USAGE:
    scripts/rotate-bootstrap-password.sh                # generate strong pw
    scripts/rotate-bootstrap-password.sh '<NEW_PW>'     # use supplied pw
    scripts/rotate-bootstrap-password.sh --help

    🟡 COORDINATED: re-keys live Authentik admin + may re-key the backup
    encryption password and force-recreate backup-service.

ALSO REACHABLE AS:
    razzfazz-setup.sh --rotate-bootstrap-password [<NEW_PW>]

WHAT IT TOUCHES (in order):
    1. Authentik DB                       — POST /api/v3/core/users/<pk>/set_password/
                                             (this is the value Authentik really
                                             checks at akadmin login — .env is
                                             ignored after first init)
    2. .env AUTHENTIK_BOOTSTRAP_PASSWORD  — source of truth / sticker
    3. .env BACKUP_ENCRYPTION_PASSWORD    — IFF currently equals the old
                                             admin password (sticker pattern).
                                             Operator-customized values are
                                             left alone.
    4. docker compose up -d --force-recreate backup-service
                                          — refresh cached GPG_PASSPHRASE.

REQUIREMENTS:
    - .env must exist and contain MAIN_DOMAIN + AUTHENTIK_BOOTSTRAP_TOKEN.
    - The bootstrap token must belong to a Super Admin (default akadmin).
    - Supplied passwords must be at least 12 characters.

IDEMPOTENCY:
    Re-running with the same password is a safe no-op (exit 0, message
    explains nothing changed).

EXIT CODES:
    0   rotation completed (or idempotent no-op)
    1   any failure

SEE ALSO:
    docs/security-architecture.md §8 (rotation caveat)
    docs/security-architecture.md §16 (handover checklist)
EOF
}

# ---- Parse args ------------------------------------------------------------
NEW_PW_FROM_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        --*)
            echo "ERROR: unknown option: $1" >&2
            echo "Try: $0 --help" >&2
            exit 1
            ;;
        *)
            if [[ -n "$NEW_PW_FROM_ARG" ]]; then
                echo "ERROR: multiple positional args; expected at most one password." >&2
                exit 1
            fi
            NEW_PW_FROM_ARG="$1"
            shift
            ;;
    esac
done

# ---- Locate .env -----------------------------------------------------------
ENV_FILE="${RAZZFAZZ_TEST_FAKE_ENV_FILE:-${REPO_ROOT}/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    print_error "rotate-bootstrap-password: .env not found at $ENV_FILE"
    print_info  "  Run razzfazz-init.sh first, or set RAZZFAZZ_TEST_FAKE_ENV_FILE."
    exit 1
fi

# ---- Read current state ----------------------------------------------------
MAIN_DOMAIN="$(read_env_value "$ENV_FILE" "MAIN_DOMAIN")"
AUTHENTIK_BOOTSTRAP_TOKEN="$(read_env_value "$ENV_FILE" "AUTHENTIK_BOOTSTRAP_TOKEN")"
OLD_BOOTSTRAP_PW="$(read_env_value "$ENV_FILE" "AUTHENTIK_BOOTSTRAP_PASSWORD")"
OLD_BACKUP_PW="$(read_env_value "$ENV_FILE" "BACKUP_ENCRYPTION_PASSWORD")"

if [[ -z "$AUTHENTIK_BOOTSTRAP_TOKEN" ]]; then
    print_error "rotate-bootstrap-password: AUTHENTIK_BOOTSTRAP_TOKEN is empty in $ENV_FILE"
    print_info  "  Cannot reach the Authentik API without it."
    print_info  "  Re-generate via razzfazz-setup.sh, or run razzfazz-init.sh."
    exit 1
fi

# In production we also need MAIN_DOMAIN (for the Authentik API URL).
# The test path short-circuits the curl entirely so MAIN_DOMAIN is
# only consulted when neither test-OK nor test-FAIL is set.
if [[ -z "$MAIN_DOMAIN" ]] \
   && [[ -z "${RAZZFAZZ_TEST_FAKE_AUTHENTIK_OK:-}" ]] \
   && [[ -z "${RAZZFAZZ_TEST_FAKE_AUTHENTIK_FAIL:-}" ]]; then
    print_error "rotate-bootstrap-password: MAIN_DOMAIN is empty in $ENV_FILE"
    exit 1
fi

# ---- Determine the new password --------------------------------------------
# generate_strong_password: 32 chars from a curated alphanumeric+symbol set.
# Avoids characters that wreck shell quoting (`'`, `"`, `\`, backtick) and
# the operator's typing surface (` `, `;`).
generate_strong_password() {
    # 24 bytes of entropy → 32 base64 chars; trim to 32, replace `+/=` with
    # safer printable substitutes so the password remains strong but
    # operator-typeable.
    local raw
    raw=$(LC_ALL=C tr -dc 'A-Za-z0-9!@#%^&*_+-' </dev/urandom | head -c 32)
    if [[ ${#raw} -ne 32 ]]; then
        # /dev/urandom should never fall short, but guard anyway.
        echo "ERROR: failed to generate strong password" >&2
        exit 1
    fi
    printf '%s' "$raw"
}

if [[ -n "$NEW_PW_FROM_ARG" ]]; then
    NEW_PW="$NEW_PW_FROM_ARG"
    PW_SOURCE="supplied"
else
    NEW_PW="$(generate_strong_password)"
    PW_SOURCE="generated"
fi

# Validate password strength (12+ chars).
if [[ ${#NEW_PW} -lt 12 ]]; then
    print_error "rotate-bootstrap-password: password too short (${#NEW_PW} chars; minimum 12)."
    print_info  "  A weak admin password defeats the rotation. Re-run with a stronger value."
    exit 1
fi

# ---- Idempotency probe -----------------------------------------------------
# If .env already holds the supplied password, we still call the API
# (in case .env and the Authentik DB drifted), but we skip env writes
# and the backup-service force-recreate.
NOOP=0
if [[ "$NEW_PW" == "$OLD_BOOTSTRAP_PW" ]]; then
    NOOP=1
fi

# ---- Look up akadmin's pk + call set_password -----------------------------
# Test hooks short-circuit BOTH the lookup and the write.
api_set_password() {
    # Short-circuit paths (test only).
    if [[ -n "${RAZZFAZZ_TEST_FAKE_AUTHENTIK_FAIL:-}" ]]; then
        echo "FAKE-AUTHENTIK-FAIL: ${RAZZFAZZ_TEST_FAKE_AUTHENTIK_FAIL}" >&2
        return 1
    fi
    if [[ -n "${RAZZFAZZ_TEST_FAKE_AUTHENTIK_OK:-}" ]]; then
        echo "FAKE-AUTHENTIK-OK: pretending /set_password/ returned 204" >&2
        return 0
    fi

    # Real path: hit Authentik through Caddy at https://auth.<domain>.
    if ! command -v curl >/dev/null 2>&1; then
        print_error "rotate-bootstrap-password: curl not installed."
        return 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        print_error "rotate-bootstrap-password: python3 not installed (need json parser)."
        return 1
    fi

    local base="https://auth.${MAIN_DOMAIN}"
    # TLS: never `-k`/`--insecure` — these calls carry the admin bootstrap
    # token and the new plaintext password (#857). On TLS_MODE=internal the
    # box's self-signed chain is verified against certs/caddy-ca.pem.
    authentik_cacert_args

    # 1. Find the superuser's pk (Super-Admin scope).
    #
    # #1148 review: this asked for `?username=akadmin` literally. On a NEW box
    # the superuser is `rzfz-admin`, so the lookup returned no results and this
    # script aborted — and this script is the primary route for a lost admin
    # password (docs/enterprise/how-to/lost-admin-reset.md:16) and the thing
    # that keeps the backup passphrase current
    # (docs/security-architecture.md:425). It would have broken on the first
    # box that needed it most.
    #
    # Both names are tried, configured first: a box mid-migration can have
    # either, and an operator who renamed the account by hand set
    # RAZZFAZZ_ADMIN_USERNAME to match.
    local pk_json pk _admin_user _try
    _admin_user="${RAZZFAZZ_ADMIN_USERNAME:-}"
    [[ -z "$_admin_user" ]] && _admin_user=$(read_env_value "$ENV_FILE" "RAZZFAZZ_ADMIN_USERNAME" 2>/dev/null || true)
    pk=""
    for _try in "$_admin_user" akadmin; do
        [[ -z "$_try" ]] && continue
        pk_json=$(curl -s --max-time 30 "${RZFZ_AUTHENTIK_CACERT_ARGS[@]}" \
            -H "Authorization: Bearer ${AUTHENTIK_BOOTSTRAP_TOKEN}" \
            -H "Accept: application/json" \
            "${base}/api/v3/core/users/?username=${_try}" || true)
        if [[ -z "$pk_json" ]]; then
            print_error "rotate-bootstrap-password: Authentik API unreachable at ${base}"
            return 1
        fi
        pk=$(printf '%s' "$pk_json" \
            | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get("results",[]); print(r[0]["pk"] if r else "")' 2>/dev/null || true)
        if [[ -n "$pk" ]]; then
            _admin_user="$_try"
            break
        fi
    done
    if [[ -z "$pk" ]]; then
        print_error "rotate-bootstrap-password: no superuser found via ${base}/api/v3/core/users/ (tried '${_admin_user}' and 'akadmin')"
        print_info  "  Token may be invalid, or the account was renamed to something else —"
        print_info  "  set RAZZFAZZ_ADMIN_USERNAME in .env to the name Authentik actually holds."
        return 1
    fi

    # 2. POST the new password as a JSON body. -f makes curl exit non-zero
    #    on 4xx/5xx; -w '%{http_code}' captures the status to stdout for
    #    auditing; -o /dev/null discards the body (which is empty on 204).
    local code
    code=$(curl -s --max-time 30 "${RZFZ_AUTHENTIK_CACERT_ARGS[@]}" -o /dev/null -w '%{http_code}' \
        -X POST \
        -H "Authorization: Bearer ${AUTHENTIK_BOOTSTRAP_TOKEN}" \
        -H "Content-Type: application/json" \
        --data-binary "$(printf '{"password":%s}' "$(printf '%s' "$NEW_PW" | python3 -c 'import sys,json; sys.stdout.write(json.dumps(sys.stdin.read()))')")" \
        "${base}/api/v3/core/users/${pk}/set_password/" || true)
    if [[ "$code" != "204" && "$code" != "200" ]]; then
        print_error "rotate-bootstrap-password: /set_password/ returned HTTP $code"
        return 1
    fi
    return 0
}

print_step "BSB-02 — rotating Authentik bootstrap admin password"
print_substep "env file:           $ENV_FILE"
print_substep "Authentik:          ${MAIN_DOMAIN:+https://auth.}${MAIN_DOMAIN}"
print_substep "password source:    $PW_SOURCE"
if [[ "$NOOP" -eq 1 ]]; then
    print_substep "idempotency:        old == new (no-op path will be taken)"
fi

# 1. Authentik API — must succeed BEFORE we touch .env (so a failure
#    leaves the operator's recovery state consistent).
if ! api_set_password; then
    print_error "rotate-bootstrap-password: aborting BEFORE .env writes — Authentik update failed."
    print_info  "  No state change. .env still holds the OLD password."
    exit 1
fi
print_substep "Authentik /set_password/ accepted."

# 2. .env writes (idempotent path: skip if old==new).
if [[ "$NOOP" -eq 1 ]]; then
    print_substep ".env: AUTHENTIK_BOOTSTRAP_PASSWORD unchanged (no-op)."
    if [[ -n "$OLD_BACKUP_PW" && "$OLD_BACKUP_PW" == "$OLD_BOOTSTRAP_PW" ]]; then
        print_substep ".env: BACKUP_ENCRYPTION_PASSWORD unchanged (already equals new)."
    fi
else
    update_env_value "$ENV_FILE" "AUTHENTIK_BOOTSTRAP_PASSWORD" "$NEW_PW"
    print_substep ".env: AUTHENTIK_BOOTSTRAP_PASSWORD updated."

    # 3. Cascade to BACKUP_ENCRYPTION_PASSWORD in the single-secret cases:
    #    - it EQUALS the old bootstrap password (the sticker single-secret
    #      pattern), OR
    #    - it is EMPTY (the unset/default case — compose has no fallback, so an
    #      empty value means backups refuse; rotating must set it, not skip it.
    #      Also self-heals pre-fix fresh installs that were left empty — ga.4
    #      0.91 finding).
    #    Only a genuinely-customized DISTINCT non-empty value is left alone.
    if [[ -z "$OLD_BACKUP_PW" || "$OLD_BACKUP_PW" == "$OLD_BOOTSTRAP_PW" ]]; then
        update_env_value "$ENV_FILE" "BACKUP_ENCRYPTION_PASSWORD" "$NEW_PW"
        if [[ -z "$OLD_BACKUP_PW" ]]; then
            print_substep ".env: BACKUP_ENCRYPTION_PASSWORD set (was empty → single-secret default)."
        else
            print_substep ".env: BACKUP_ENCRYPTION_PASSWORD cascaded (was equal to old admin pw)."
        fi
        BACKUP_CASCADED=1
    else
        print_substep ".env: BACKUP_ENCRYPTION_PASSWORD left alone (operator-customized)."
        BACKUP_CASCADED=0
    fi
fi

# 4. backup-service force-recreate (only if anything actually changed).
if [[ "$NOOP" -eq 0 ]] && [[ "${BACKUP_CASCADED:-0}" -eq 1 ]]; then
    # NOTE: the compose SERVICE is `backup` (its container is named backup-service);
    # recreating by the container name fails with "no such service" — which used to
    # exit 1 here, silently skipping the GPG re-key AND aborting before the
    # razzfazz-config refresh below. Use the service name and keep failures non-fatal
    # (the .env + Authentik writes — the part that matters — already succeeded).
    if [[ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]]; then
        print_substep "DRY-RUN: would force-recreate backup-service"
        print_info    "         (real cmd: docker compose up -d --force-recreate backup)"
    else
        if command -v docker >/dev/null 2>&1; then
            print_substep "force-recreating backup-service so cached GPG_PASSPHRASE refreshes…"
            (cd "$REPO_ROOT" && docker compose up -d --force-recreate backup) || {
                print_warning "rotate-bootstrap-password: backup force-recreate failed."
                print_info  "  .env + Authentik writes succeeded; please run manually:"
                print_info  "    docker compose up -d --force-recreate backup"
            }
        else
            print_info "docker not installed; skipping backup force-recreate."
            print_info "  Please run manually on the host:"
            print_info "    docker compose up -d --force-recreate backup"
        fi
    fi
fi

# 5. razzfazz-config force-recreate (whenever the bootstrap pw actually changed).
#    The Configuration Portal's own login uses ADMIN_PASSWORD=${AUTHENTIK_BOOTSTRAP_PASSWORD},
#    baked into the container at start. Without a recreate it keeps validating
#    against the OLD bootstrap password after a rotation (settings.<domain> login
#    breaks). Always needed on a real rotation — independent of the backup cascade.
if [[ "$NOOP" -eq 0 ]]; then
    if [[ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]]; then
        print_substep "DRY-RUN: would force-recreate razzfazz-config"
        print_info    "         (real cmd: docker compose up -d --force-recreate razzfazz-config)"
    elif command -v docker >/dev/null 2>&1; then
        print_substep "force-recreating razzfazz-config so its ADMIN_PASSWORD refreshes…"
        (cd "$REPO_ROOT" && docker compose up -d --force-recreate razzfazz-config) || {
            print_warning "rotate-bootstrap-password: razzfazz-config force-recreate failed."
            print_info  "  .env + Authentik writes succeeded; please run manually:"
            print_info  "    docker compose up -d --force-recreate razzfazz-config"
        }
    else
        print_info "docker not installed; skipping razzfazz-config force-recreate."
        print_info "  Please run manually on the host:"
        print_info "    docker compose up -d --force-recreate razzfazz-config"
    fi
fi

# ---- Audit + final report --------------------------------------------------
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [[ "$NOOP" -eq 1 ]]; then
    print_success "BSB-02 rotate-bootstrap-password OK (no-op, no change) ${TS}"
    print_info  "  The supplied password already matched .env; Authentik API call"
    print_info  "  was made anyway in case the DB had drifted, and accepted."
    exit 0
fi

print_success "BSB-02 rotate-bootstrap-password OK ${TS}"
echo ""
echo "================================================================"
echo "  NEW PASSWORD (capture this — also stored in .env now):"
echo ""
echo "      ${NEW_PW}"
echo ""
echo "  Update the device sticker / customer password vault."
echo "================================================================"

if [[ "${BACKUP_CASCADED:-0}" -eq 1 ]] && [[ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]]; then
    # Test path: surface the would-be force-recreate in the success
    # message so operators reading transcripts (and tests asserting on
    # the output) see the cascade was acknowledged.
    echo "  backup-service: would have been force-recreated (dry-run)"
fi

exit 0
