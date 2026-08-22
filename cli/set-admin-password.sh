#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz-set-admin-password.sh  (#47)
# ==============================================================================
# Set the ADMIN password of one app, all apps, or reset to the fleet default —
# both in .env AND on the live running app (the .env value alone does NOT change
# an admin user that was already created with the old password).
#
# This is the "safe half": it changes per-APP admin LOGIN passwords. It does NOT
# rotate infrastructure secrets (POSTGRES_PASSWORD, AUTHENTIK_SECRET_KEY,
# VALKEY_PASSWORD, BACKUP_ENCRYPTION_PASSWORD, ...) — that is the dangerous half
# and lives in a separate, gated tool (razzfazz-rotate-secret.sh, #149).
#
# Modes:
#   --app <name> [--password <pw>]      Set ONE app's admin password.
#   --all [--password <pw>]             Set EVERY app's admin password (central).
#   --reset-to-default [--app <name>]   Reset to AUTHENTIK_BOOTSTRAP_PASSWORD
#                                       (the fleet default). Omit --app = all.
#   --status                            Show, per app, whether its .env password
#                                       matches the fleet bootstrap password.
#
# If --password is omitted (and not --reset-to-default), you are prompted twice.
# Use --yes to skip the confirmation prompt (for automation).
#
# Apps: authentik, gitea, dify, openwebui, gpustack, cognee, lightrag, komodo.
# Only apps whose container is running are touched; others are reported skipped.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"
# #54 — DRY: the per-account "set Dify/Cognee password for email X" logic now
# lives in ONE place (cli/lib-set-password.sh), shared with the start-portal
# password broker. apply_dify / apply_cognee below delegate to it instead of
# carrying their own copy.
# shellcheck source=cli/lib-set-password.sh
source "${SCRIPT_DIR}/cli/lib-set-password.sh"
ENV_FILE="${SCRIPT_DIR}/.env"

[ -f "$ENV_FILE" ] || { print_error ".env not found at $ENV_FILE"; exit 1; }

# Targeted reads only — never source operator-edited .env (memory rule).
PG_USER="$(read_env_value "$ENV_FILE" POSTGRES_USER)";  PG_USER="${PG_USER:-docker}"
MAIN_DOMAIN="$(read_env_value "$ENV_FILE" MAIN_DOMAIN)"
FLEET_PW="$(read_env_value "$ENV_FILE" AUTHENTIK_BOOTSTRAP_PASSWORD)"
ADMIN_EMAIL="razzfazz-ai-admin@${MAIN_DOMAIN}"

# App registry: "name:env_key" (apply handled by apply_<name>).
APPS=(
    "authentik:AUTHENTIK_BOOTSTRAP_PASSWORD"
    "gitea:GITEA_ADMIN_PASSWORD"
    "dify:DIFY_ADMIN_PASSWORD"
    "openwebui:OPENWEBUI_ADMIN_PASSWORD"
    "gpustack:GPUSTACK_ADMIN_PASSWORD"
    "cognee:COGNEE_ADMIN_PASSWORD"
    "lightrag:LIGHTRAG_AUTH_ACCOUNTS"
    "komodo:KOMODO_INIT_ADMIN_PASSWORD"
)

env_key_for() { local a; for a in "${APPS[@]}"; do [ "${a%%:*}" = "$1" ] && { echo "${a#*:}"; return 0; }; done; return 1; }
app_names()   { local a; for a in "${APPS[@]}"; do echo "${a%%:*}"; done; }

# ── per-app live-apply ────────────────────────────────────────────────────────
# Each returns 0 on success, 1 on failure, 2 = container not running (skip).

apply_authentik() {  # delegate to the dedicated, tested rotation script
    # rotate-bootstrap-password.sh takes a positional <NEW_PW>, is
    # non-interactive, and itself updates .env AUTHENTIK_BOOTSTRAP_PASSWORD +
    # re-keys backup-service, so we do NOT write_env_for authentik separately.
    local pw=$1
    check_container authentik-server || return 2
    "${SCRIPT_DIR}/scripts/rotate-bootstrap-password.sh" "$pw" >/dev/null 2>&1
}

apply_gitea() {
    local pw=$1
    check_container gitea || return 2
    local user; user="$(read_env_value "$ENV_FILE" GITEA_ADMIN_USER)"; user="${user:-admin}"
    docker exec -u root gitea su-exec git gitea admin user change-password \
        --username "$user" --password "$pw" --must-change-password=false >/dev/null 2>&1
}

apply_dify() {  # Dify hashes with PBKDF2-sha256 (10k) — match its scheme
    local pw=$1
    check_container dify-api || return 2
    local db; db="$(read_env_value "$ENV_FILE" DIFY_DB)"; db="${db:-dify_db}"
    local target; target="$(docker exec postgres psql -U "$PG_USER" -d "$db" -tAc \
        "SELECT email FROM accounts ORDER BY created_at LIMIT 1;" 2>/dev/null | head -n1)"
    target="${target:-$ADMIN_EMAIL}"
    # #54 — delegate to the shared helper (one pbkdf2 + parameterized-psql
    # implementation). Password on stdin, never argv.
    printf '%s' "$pw" | rzfz_set_password_for_app dify "$target"
}

apply_openwebui() {  # OpenWebUI hashes with bcrypt
    local pw=$1
    check_container openwebui || return 2
    local db; db="$(read_env_value "$ENV_FILE" OPENWEBUI_DB)"; db="${db:-openwebui_db}"
    local target; target="$(docker exec postgres psql -U "$PG_USER" -d "$db" -tAc \
        "SELECT a.email FROM auth a JOIN \"user\" u ON a.id=u.id WHERE u.role='admin' LIMIT 1;" 2>/dev/null | head -n1)"
    target="${target:-$ADMIN_EMAIL}"
    local hash; hash="$(docker exec openwebui python3 -c "import bcrypt; print(bcrypt.hashpw(b'''${pw}''', bcrypt.gensalt(12)).decode())" 2>/dev/null)"
    [ -n "$hash" ] || return 1
    docker exec postgres psql -U "$PG_USER" -d "$db" -c \
        "UPDATE auth SET password='${hash}' WHERE email='${target}';" >/dev/null 2>&1
}

apply_gpustack() {  # gpustack hashes the admin password with argon2id in gpustack_db.users
    local pw=$1
    check_container gpustack || return 2
    # The bundled `gpustack reset-admin-password` CLI is interactive and version-
    # dependent (v0.7.1 takes no --password — it expects --server-url/--api-key and
    # prompts; its argparse error even exits 2, which used to be mis-read as
    # "container not running"). Set the hash directly with gpustack's OWN
    # get_secret_hash() — same direct-DB approach as apply_dify/apply_openwebui, and
    # version-robust because we reuse gpustack's hashing rather than its CLI.
    local db; db="$(read_env_value "$ENV_FILE" GPUSTACK_DB)"; db="${db:-gpustack_db}"
    local hash; hash="$(docker exec -e GPW="$pw" gpustack python3 -c \
        "import os; from gpustack.security import get_secret_hash; print(get_secret_hash(os.environ['GPW']))" 2>/dev/null)"
    [ -n "$hash" ] || return 1
    docker exec postgres psql -U "$PG_USER" -d "$db" -c \
        "UPDATE users SET hashed_password='${hash}' WHERE username='admin';" >/dev/null 2>&1
}

# lightrag reads its admin creds from AUTH_ACCOUNTS (admin:<pw>) at start, so a
# real recreate after the .env write applies the new password. komodo's admin pw
# is init-only; we keep .env in sync but cannot change it live.
apply_lightrag() { local pw=$1; check_container lightrag || return 2; _recreate lightrag; }
apply_komodo()   { local pw=$1; check_container komodo-core || return 2; print_info "komodo admin password is init-only; .env updated (no live change)."; return 0; }

# cognee seeds its admin into the relational store on FIRST init only; a plain
# container recreate never re-hashes the password (that was the cognee live-reset
# bug). Update it through cognee's OWN user-manager so the hash matches its login
# scheme and the write is storage-agnostic (works whether the relational backend
# is the shared Postgres or cognee's bundled store). The target email is cognee's
# configured default_user_email, resolved inside the container — never guessed.
apply_cognee() {
    local pw=$1
    check_container cognee || return 2
    # #54 — delegate to the shared helper, which updates through cognee's OWN
    # fastapi-users user-manager (never hand-hashes), storage-agnostic. We resolve
    # the target email the same way the shared lib does for arbitrary users; for
    # the admin path that is cognee's configured default_user_email, read inside
    # the container so it is never guessed.
    local target
    target="$(docker exec cognee python3 -c \
        "from cognee.base_config import get_base_config; print(get_base_config().default_user_email or '')" 2>/dev/null | head -n1)"
    target="${target:-$ADMIN_EMAIL}"
    printf '%s' "$pw" | rzfz_set_password_for_app cognee "$target"
}

# _recreate must ACTUALLY run docker compose. docker_compose_cmd only *prints* the
# invocation ("docker compose" / "docker-compose"); calling it bare — as this used
# to — just echoed that string and returned 0 WITHOUT recreating anything. That
# silent no-op was the lightrag live-reset bug (.env updated, container untouched).
_recreate() {
    local dc; dc="$(docker_compose_cmd 2>/dev/null)" || dc="docker compose"
    ( cd "$SCRIPT_DIR" && $dc up -d --force-recreate "$1" >/dev/null 2>&1 )
}

# lightrag stores admin:<pw> in LIGHTRAG_AUTH_ACCOUNTS — keep the user prefix.
write_env_for() {
    local app=$1 pw=$2 key; key="$(env_key_for "$app")" || return 1
    if [ "$app" = "lightrag" ]; then
        local cur user; cur="$(read_env_value "$ENV_FILE" "$key")"; user="${cur%%:*}"; user="${user:-admin}"
        update_env_value "$ENV_FILE" "$key" "${user}:${pw}"
    else
        update_env_value "$ENV_FILE" "$key" "$pw"
    fi
}

# ── post-set verification ──────────────────────────────────────────────────────
# apply_<app> writes the hash and returns 0 even when the DB UPDATE succeeded but
# the hash SCHEME is wrong — a [✓] on an admin that CANNOT log in (gpustack argon2
# / cognee fastapi-users bcrypt, 2026-08 Profida field bug). verify_one re-checks
# with the app's OWN hasher inside its container; pw + stored hash are passed as
# SEPARATE argv, so the argon2/bcrypt `$` sigils never touch a shell.
#   returns: 0 verified · 1 mismatch (login would fail) · 2 cannot-check (skip)
verify_one() {
    local app=$1 pw=$2 db stored code
    case "$app" in
        openwebui)
            check_container openwebui || return 2
            db="$(read_env_value "$ENV_FILE" OPENWEBUI_DB)"; db="${db:-openwebui_db}"
            stored="$(docker exec postgres psql -U "$PG_USER" -d "$db" -tAc "select password from auth order by email limit 1;" 2>/dev/null | head -n1)"
            [ -n "$stored" ] || return 2
            code='import sys,bcrypt; sys.exit(0 if bcrypt.checkpw(sys.argv[1].encode(), sys.argv[2].encode()) else 1)'
            docker exec openwebui python3 -c "$code" "$pw" "$stored" >/dev/null 2>&1 ;;
        gpustack)
            check_container gpustack || return 2
            db="$(read_env_value "$ENV_FILE" GPUSTACK_DB)"; db="${db:-gpustack_db}"
            stored="$(docker exec postgres psql -U "$PG_USER" -d "$db" -tAc "select hashed_password from users where username='admin';" 2>/dev/null | head -n1)"
            [ -n "$stored" ] || return 2
            code='import sys,gpustack.security as s; sys.exit(0 if s.verify_hashed_secret(sys.argv[2], sys.argv[1]) else 1)'
            docker exec gpustack python3 -c "$code" "$pw" "$stored" >/dev/null 2>&1 ;;
        cognee)
            check_container cognee || return 2
            stored="$(docker exec postgres psql -U "$PG_USER" -d cognee_db -tAc "select hashed_password from public.users order by email limit 1;" 2>/dev/null | head -n1)"
            [ -n "$stored" ] || return 2
            code='import sys
from fastapi_users.password import PasswordHelper
sys.exit(0 if PasswordHelper().verify_and_update(sys.argv[1], sys.argv[2])[0] else 1)'
            docker exec cognee python3 -c "$code" "$pw" "$stored" >/dev/null 2>&1 ;;
        dify)
            check_container dify-api || return 2
            db="$(read_env_value "$ENV_FILE" DIFY_DB)"; db="${db:-dify_db}"
            stored="$(docker exec postgres psql -U "$PG_USER" -d "$db" -tAc "select password || '|' || password_salt from accounts order by created_at limit 1;" 2>/dev/null | head -n1)"
            [ -n "$stored" ] || return 2
            code='import sys,base64,binascii,hashlib
h,salt=sys.argv[2].split("|"); salt=base64.b64decode(salt)
exp=base64.b64encode(binascii.hexlify(hashlib.pbkdf2_hmac("sha256",sys.argv[1].encode(),salt,10000))).decode()
sys.exit(0 if exp==h else 1)'
            docker exec dify-api python3 -c "$code" "$pw" "$stored" >/dev/null 2>&1 ;;
        *) return 2 ;;  # authentik(rotate verifies) · gitea(change-password authoritative) · komodo(init-only) · lightrag(recreate)
    esac
}

# ── orchestration ─────────────────────────────────────────────────────────────
set_one() {  # set_one <app> <password>
    local app=$1 pw=$2
    print_substep "Setting admin password for ${app}..."
    # authentik's live re-key is delegated to rotate-bootstrap-password.sh, which
    # OWNS the .env write — and treats "requested pw already in .env" as a no-op
    # (skipping the Authentik API call AND the backup/config-UI recreates). Pre-
    # writing .env here would therefore turn that rotate into a no-op: akadmin
    # never actually re-keyed, config-UI never refreshed. So skip the pre-write for
    # authentik and let the rotate script do both the .env write and the live work.
    [ "$app" = "authentik" ] || write_env_for "$app" "$pw"
    local rc=0; "apply_${app}" "$pw" || rc=$?
    case "$rc" in
        0)
            # Never trust apply's exit code alone: a DB write can succeed with the
            # WRONG hash scheme (gpustack/cognee) — [✓] on an un-loginable admin.
            local vrc=0; verify_one "$app" "$pw" || vrc=$?
            case "$vrc" in
                0) print_success "${app}: admin password updated + VERIFIED (live + .env)." ;;
                1) print_error   "${app}: password WRITTEN but does NOT verify against the app's own hasher — login would FAIL. apply_${app}'s hash scheme is wrong; do not trust this update." ; SET_ONE_VERIFY_FAILED=1 ;;
                *) print_success "${app}: admin password updated (live + .env); no live verify for this app." ;;
            esac
            ;;
        2) print_warning "${app}: container not running — .env updated, live change skipped." ;;
        *) print_error   "${app}: live update FAILED (rc=$rc) — .env was updated; re-run when the app is healthy." ;;
    esac
    return 0
}

show_status() {
    print_step "Admin-password status (vs fleet AUTHENTIK_BOOTSTRAP_PASSWORD)"
    local app key val mark
    for app in $(app_names); do
        key="$(env_key_for "$app")"; val="$(read_env_value "$ENV_FILE" "$key")"
        [ "$app" = "lightrag" ] && val="${val#*:}"
        if [ -z "$val" ]; then mark="(empty → uses fleet default)";
        elif [ "$val" = "$FLEET_PW" ]; then mark="= fleet password";
        else mark="CUSTOM (differs from fleet)"; fi
        printf "  %-12s %s\n" "$app" "$mark"
    done
}

prompt_password() {
    local p1 p2
    read -r -s -p "New admin password: " p1; echo
    read -r -s -p "Repeat:             " p2; echo
    [ "$p1" = "$p2" ] || { print_error "Passwords do not match."; exit 1; }
    [ ${#p1} -ge 8 ] || { print_error "Password too short (min 8)."; exit 1; }
    printf '%s' "$p1"
}

usage() {
    sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ── arg parse ─────────────────────────────────────────────────────────────────
MODE="" APP="" PASSWORD="" ASSUME_YES=false
while [ $# -gt 0 ]; do
    case "$1" in
        --app)              APP="${2:?--app needs a name}"; shift 2 ;;
        --password)         PASSWORD="${2:?--password needs a value}"; shift 2 ;;
        --all)              MODE="all"; shift ;;
        --reset-to-default) MODE="reset"; shift ;;
        --status)           MODE="status"; shift ;;
        --yes|-y)           ASSUME_YES=true; shift ;;
        -h|--help)          usage 0 ;;
        *) print_error "Unknown option: $1"; usage 1 ;;
    esac
done

# default mode: single app if --app given
[ -z "$MODE" ] && [ -n "$APP" ] && MODE="one"
[ -z "$MODE" ] && usage 1

# validate app name where given
if [ -n "$APP" ] && ! env_key_for "$APP" >/dev/null; then
    print_error "Unknown app: $APP. Known: $(app_names | tr '\n' ' ')"; exit 1
fi

case "$MODE" in
    status) show_status; exit 0 ;;
    one)
        [ -n "$PASSWORD" ] || PASSWORD="$(prompt_password)"
        set_one "$APP" "$PASSWORD"
        ;;
    all)
        [ -n "$PASSWORD" ] || PASSWORD="$(prompt_password)"
        if [ "$ASSUME_YES" != true ]; then
            printf 'Set the admin password on ALL apps (%s)? [y/N] ' "$(app_names | tr '\n' ' ')"
            read -r ans; case "$ans" in y|Y|yes) ;; *) print_info "Aborted."; exit 0 ;; esac
        fi
        for app in $(app_names); do set_one "$app" "$PASSWORD"; done
        ;;
    reset)
        [ -n "$FLEET_PW" ] || { print_error "AUTHENTIK_BOOTSTRAP_PASSWORD is empty — nothing to reset to."; exit 1; }
        local_targets="$(app_names)"; [ -n "$APP" ] && local_targets="$APP"
        if [ "$ASSUME_YES" != true ]; then
            printf 'Reset admin password to the fleet default for: %s ? [y/N] ' "$(echo "$local_targets" | tr '\n' ' ')"
            read -r ans; case "$ans" in y|Y|yes) ;; *) print_info "Aborted."; exit 0 ;; esac
        fi
        for app in $local_targets; do set_one "$app" "$FLEET_PW"; done
        ;;
esac

print_success "Done. Re-run with --status to verify."

# Fail loud (non-zero) if any app's password did not verify against its own hasher
# — otherwise a broken set (gpustack/cognee) reads as success to callers/automation.
if [ "${SET_ONE_VERIFY_FAILED:-0}" = "1" ]; then
    print_error "One or more apps' passwords did NOT verify (see above). The admin CANNOT log in there — re-run when the app is healthy or fix apply_<app>."
    exit 1
fi
