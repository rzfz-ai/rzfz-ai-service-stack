#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/rotate-db-password.sh
# Single-script rotation of a per-module Postgres user password.
#
# Why this script exists
# ----------------------
# Each module on the stack (authentik, openwebui, gpustack, dify,
# dify-plugin, gitea, paperless, infisical, onyx, paperclip, synapse) has
# its own per-service Postgres user with a per-service password in .env.
# Rotating one of those was a 4-step manual operator dance:
#
#   1. docker exec postgres psql -U postgres -c \
#         "ALTER USER <module>_user WITH PASSWORD '<new>'"
#   2. Hand-edit .env to update <MODULE>_DB_PASSWORD=
#   3. docker compose up -d --force-recreate <module-services>
#   4. Verify the module's container reconnects (no restart loop)
#
# Easy to skip step 2 (operator typo) or step 3 (forget the recreate),
# leaving the module in a restart loop authenticating with the OLD
# password. This script collapses all four into one idempotent
# invocation. See B-5 / R-DEF-05 / docs/security-architecture.md §10.
#
# What it touches (in order, atomic on Postgres failure)
# ------------------------------------------------------
#   1. Postgres role — ALTER USER <user> WITH PASSWORD '<new>'
#      (executed via `docker exec postgres psql -U <admin>`).
#   2. .env <MODULE>_DB_PASSWORD — for source-of-truth + restart parity.
#   3. docker compose up -d --force-recreate <svc1> <svc2> …
#      — refresh the module's containers so they reconnect.
#   4. healthz probe (best-effort) — confirms reconnection. WARN-only on
#      failure; some modules take 30-60 s to come up after recreate.
#
# Idempotency contract (BSB-05-DEC-04)
# ------------------------------------
# Running twice with the same password is a safe no-op:
#   - if .env already holds the supplied password, we still call
#     ALTER USER (in case Postgres and .env drifted), but skip env
#     writes and the container force-recreate.
#   - exit 0 with a clear "no-op" message.
#
# Atomicity (BSB-05-DEC-02)
# -------------------------
# If ALTER USER fails, abort BEFORE rewriting .env so the operator's
# recovery state stays consistent (.env still matches what Postgres
# has).
#
# If force-recreate fails AFTER ALTER USER + .env succeeded, do NOT
# auto-rollback. Surface a loud WARN with the manual recreate command.
# Re-rotating a working password is its own footgun.
#
# Module → service mapping
# ------------------------
# Hard-coded inside this script — mirrors the canonical list in
# razzfazz-upgrade.sh:1740 (per-service DB user migration). Extending
# this script for a new module is a 2-line edit (MODULE_TO_USER +
# MODULE_TO_SERVICES).
#
# Dify special case
# -----------------
# The .env.dify file does NOT carry an independent DIFY_DB_PASSWORD —
# it references ${DIFY_DB_PASSWORD:-${POSTGRES_PASSWORD}} from .env, so
# updating .env is sufficient. We force-recreate dify-api + dify-worker
# + dify-worker-beat together.
#
# Usage
# -----
#   scripts/rotate-db-password.sh <module> [<NEW_PW>]
#   scripts/rotate-db-password.sh all     [<NEW_PW>]   # iterate every module
#   scripts/rotate-db-password.sh --list-modules
#   scripts/rotate-db-password.sh --help
#
# Test hooks (do nothing in production)
# -------------------------------------
#   RAZZFAZZ_TEST_DRY_RUN=1                short-circuit docker compose
#   RAZZFAZZ_TEST_FAKE_ENV_FILE=<path>     read/write this .env (test fixture)
#   RAZZFAZZ_TEST_FAKE_POSTGRES_OK=1       pretend ALTER USER succeeded
#   RAZZFAZZ_TEST_FAKE_POSTGRES_FAIL=msg   pretend ALTER USER failed
#   RAZZFAZZ_TEST_FAKE_HEALTH_OK=1         pretend healthz returned OK
#   RAZZFAZZ_TEST_FAKE_HEALTH_FAIL=msg     pretend healthz failed
#
# Exit codes
# ----------
#   0   rotation completed (or idempotent no-op)
#   1   any failure (Postgres unreachable, weak pw, missing token,
#                    .env not found, unknown module, all-mode partial fail)
# =============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=lib.sh disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

# ---- Module catalog --------------------------------------------------------
# Single source of truth for the module → (Postgres user, compose services)
# mapping. Adding a new module here + ensuring its <MOD>_DB_PASSWORD lives
# in .env is the entire integration cost.
#
# The user-name strategy:
#   - For modules that have a <MOD>_DB_USER env var, prefer that (operator
#     may have customized it; matches init-db.sh behaviour).
#   - For modules without a <MOD>_DB_USER (paperclip, synapse), use the
#     hard-coded fallback below — these match the upgrade migration at
#     razzfazz-upgrade.sh:1740 ("paperclip_user", "synapse_user").
declare -A MODULE_TO_PW_KEY=(
    [authentik]="AUTHENTIK_DB_PASSWORD"
    [openwebui]="OPENWEBUI_DB_PASSWORD"
    [gpustack]="GPUSTACK_DB_PASSWORD"
    [dify]="DIFY_DB_PASSWORD"
    [dify-plugin]="DIFY_PLUGIN_DB_PASSWORD"
    [gitea]="GITEA_DB_PASSWORD"
    [paperless]="PAPERLESS_DB_PASSWORD"
    [infisical]="INFISICAL_DB_PASSWORD"
    [onyx]="ONYX_DB_PASSWORD"
    [paperclip]="PAPERCLIP_DB_PASSWORD"
    [synapse]="SYNAPSE_DB_PASSWORD"
)

declare -A MODULE_TO_USER_KEY=(
    [authentik]="AUTHENTIK_DB_USER"
    [openwebui]="OPENWEBUI_DB_USER"
    [gpustack]="GPUSTACK_DB_USER"
    [dify]="DIFY_DB_USER"
    [dify-plugin]="DIFY_PLUGIN_DB_USER"
    [gitea]="GITEA_DB_USER"
    [paperless]="PAPERLESS_DB_USER"
    [infisical]="INFISICAL_DB_USER"
    [onyx]="ONYX_DB_USER"
    # paperclip + synapse intentionally omitted — see fallback below.
)

declare -A MODULE_TO_USER_FALLBACK=(
    [paperclip]="paperclip_user"
    [synapse]="synapse_user"
)

declare -A MODULE_TO_SERVICES=(
    [authentik]="authentik-server authentik-worker postgres-db-reconcile"
    [openwebui]="open-webui"
    [gpustack]="gpustack"
    [dify]="dify-api dify-worker dify-worker-beat"
    [dify-plugin]="dify-plugin-daemon"
    [gitea]="gitea"
    [paperless]="paperless-ngx"
    [infisical]="infisical"
    [onyx]="onyx-api onyx-background onyx-web onyx-model-server onyx-model-indexer"
    [paperclip]="paperclip"
    [synapse]="synapse"
)

declare -A MODULE_TO_HEALTH_CONTAINER=(
    [authentik]="authentik-server"
    [openwebui]="open-webui"
    [gpustack]="gpustack"
    [dify]="dify-api"
    [dify-plugin]="dify-plugin-daemon"
    [gitea]="gitea"
    [paperless]="paperless-ngx"
    [infisical]="infisical"
    [onyx]="onyx-api"
    [paperclip]="paperclip"
    [synapse]="synapse"
)

# ---- Help ------------------------------------------------------------------
show_help() {
    cat <<'EOF'
scripts/rotate-db-password.sh — rotate a per-module Postgres user password
in one shot, with .env update + container force-recreate + healthz probe.

USAGE:
    scripts/rotate-db-password.sh <module> [<NEW_PW>]
    scripts/rotate-db-password.sh all     [<NEW_PW>] [--fail-fast]
    scripts/rotate-db-password.sh --list-modules
    scripts/rotate-db-password.sh --help

    🟡 COORDINATED: re-keys a live DB role + force-recreates its containers.

ALSO REACHABLE AS:
    razzfazz-setup.sh --rotate-db-password <module> [<NEW_PW>]
    razzfazz-setup.sh --rotate-db-password all     [<NEW_PW>]
    razzfazz-setup.sh --rotate-db-password --list-modules

WHAT IT TOUCHES (in order, atomic on Postgres failure):
    1. Postgres role          — ALTER USER <user> WITH PASSWORD '<new>'
    2. .env <MODULE>_DB_PASSWORD — source of truth
    3. docker compose up -d --force-recreate <module-services>
                              — refresh module containers
    4. healthz probe (best effort, WARN-only)
                              — confirm container reconnected

VALID MODULES (canonical list — script auto-skips modules not present in .env):
    authentik     openwebui    gpustack       dify         dify-plugin
    gitea         paperless    infisical      onyx         paperclip       synapse
    all           (iterate every module present in .env)

PASSWORD GENERATION:
    Omit <NEW_PW> to generate a strong 32-char password (alphanumeric +
    safe symbols). Printed to stdout for capture.

REQUIREMENTS:
    - .env must exist with POSTGRES_USER, POSTGRES_PASSWORD set.
    - Postgres container ('postgres') must be running for non-test path.
    - Supplied passwords must be at least 12 characters.

IDEMPOTENCY:
    Re-running with the same password is a safe no-op. ALTER USER is
    still issued (in case Postgres and .env drifted), but env writes
    and the container force-recreate are skipped.

ATOMICITY:
    Postgres failure → abort BEFORE rewriting .env.
    force-recreate failure → loud WARN, no auto-rollback (rolling back
    a working ALTER USER is its own footgun); manual recreate command
    is printed.

`all` MODE:
    - Default: continue-on-error (still rotate every module, exit 1 at
      end if any failed).
    - --fail-fast: abort on first failure.

EXIT CODES:
    0   rotation completed (or idempotent no-op)
    1   any failure (postgres, weak pw, .env, unknown module, all-mode partial)

SEE ALSO:
    docs/security-architecture.md §10 (database passwords category)
    docs/security-architecture.md §16 (handover checklist — DB password rotation)
EOF
}

list_modules() {
    local env_file=$1
    local mod
    # Iterate sorted module list; print only those whose <MOD>_DB_PASSWORD
    # is present in .env (the canonical "is this module configured" probe).
    local sorted
    sorted=$(printf '%s\n' "${!MODULE_TO_PW_KEY[@]}" | sort)
    while IFS= read -r mod; do
        local pw_key="${MODULE_TO_PW_KEY[$mod]}"
        if grep -qE "^${pw_key}=" "$env_file" 2>/dev/null; then
            local current
            current="$(read_env_value "$env_file" "$pw_key")"
            if [[ -n "$current" ]]; then
                printf '  %-14s (key=%s, services=%s)\n' \
                    "$mod" "$pw_key" "${MODULE_TO_SERVICES[$mod]}"
            fi
        fi
    done <<< "$sorted"
}

# ---- Parse args ------------------------------------------------------------
MODULE=""
NEW_PW_FROM_ARG=""
FAIL_FAST=0
LIST_MODE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        --list-modules)
            LIST_MODE=1
            shift
            ;;
        --fail-fast)
            FAIL_FAST=1
            shift
            ;;
        --*)
            print_error "unknown option: $1"
            print_info  "Try: $0 --help"
            exit 1
            ;;
        *)
            if [[ -z "$MODULE" ]]; then
                MODULE="$1"
            elif [[ -z "$NEW_PW_FROM_ARG" ]]; then
                NEW_PW_FROM_ARG="$1"
            else
                print_error "too many positional args; expected <module> [<pw>]"
                exit 1
            fi
            shift
            ;;
    esac
done

# ---- Locate .env -----------------------------------------------------------
ENV_FILE="${RAZZFAZZ_TEST_FAKE_ENV_FILE:-${REPO_ROOT}/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    print_error "rotate-db-password: .env not found at $ENV_FILE"
    print_info  "  Run razzfazz-init.sh first, or set RAZZFAZZ_TEST_FAKE_ENV_FILE."
    exit 1
fi

# ---- --list-modules path ---------------------------------------------------
if [[ "$LIST_MODE" -eq 1 ]]; then
    echo "Modules with a configured *_DB_PASSWORD in $ENV_FILE:"
    list_modules "$ENV_FILE"
    exit 0
fi

# ---- Validate <module> arg -------------------------------------------------
if [[ -z "$MODULE" ]]; then
    print_error "rotate-db-password: missing <module> argument."
    print_info  "Usage: $0 <module> [<NEW_PW>]"
    print_info  "       $0 all     [<NEW_PW>]"
    print_info  "       $0 --list-modules    # see what's configured"
    exit 1
fi

if [[ "$MODULE" != "all" ]] && [[ -z "${MODULE_TO_PW_KEY[$MODULE]:-}" ]]; then
    print_error "rotate-db-password: '$MODULE' is not a valid module name."
    print_info  "Known modules:"
    sorted_known=$(printf '%s\n' "${!MODULE_TO_PW_KEY[@]}" | sort)
    while IFS= read -r mod; do
        print_info "  $mod"
    done <<< "$sorted_known"
    print_info "Use '$0 --list-modules' to see which are present in .env."
    exit 1
fi

# ---- Password helpers ------------------------------------------------------
generate_strong_password() {
    local raw
    raw=$(LC_ALL=C tr -dc 'A-Za-z0-9!@#%^&*_+-' </dev/urandom | head -c 32)
    if [[ ${#raw} -ne 32 ]]; then
        echo "ERROR: failed to generate strong password" >&2
        exit 1
    fi
    printf '%s' "$raw"
}

validate_pw_strength() {
    local pw=$1
    if [[ ${#pw} -lt 12 ]]; then
        print_error "rotate-db-password: password too short (${#pw} chars; minimum 12)."
        print_info  "  A weak DB password defeats the rotation. Use a 12+ char value."
        return 1
    fi
    return 0
}

# ---- Postgres ALTER USER ---------------------------------------------------
# Issue ALTER USER for the named role with the given new password.
# Test hooks short-circuit BOTH the docker exec and the SQL.
postgres_alter_user() {
    local pg_admin_user=$1 pg_admin_pass=$2 db_user=$3 new_pw=$4

    if [[ -n "${RAZZFAZZ_TEST_FAKE_POSTGRES_FAIL:-}" ]]; then
        echo "FAKE-POSTGRES-FAIL: ${RAZZFAZZ_TEST_FAKE_POSTGRES_FAIL}" >&2
        return 1
    fi
    if [[ -n "${RAZZFAZZ_TEST_FAKE_POSTGRES_OK:-}" ]]; then
        echo "FAKE-POSTGRES-OK: pretending ALTER USER \"${db_user}\" succeeded" >&2
        return 0
    fi

    if ! command -v docker >/dev/null 2>&1; then
        print_error "rotate-db-password: docker not installed."
        return 1
    fi

    # Escape single quotes in the password for the SQL literal.
    local safe_pw="${new_pw//\'/\'\'}"
    local sql="ALTER USER \"${db_user}\" WITH PASSWORD '${safe_pw}';"

    PGPASSWORD="$pg_admin_pass" docker exec -i postgres \
        psql -v ON_ERROR_STOP=1 -U "$pg_admin_user" -d postgres \
        -c "$sql" >/dev/null 2>&1 || {
            print_error "rotate-db-password: ALTER USER failed for '${db_user}'."
            print_info  "  Check: docker logs postgres | tail -40"
            return 1
        }
    return 0
}

# ---- Container force-recreate ---------------------------------------------
recreate_services() {
    local services=$1   # space-separated list

    if [[ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]]; then
        print_substep "DRY-RUN: would force-recreate: ${services}"
        print_info    "         (real cmd: docker compose up -d --force-recreate ${services})"
        return 0
    fi

    if ! command -v docker >/dev/null 2>&1; then
        print_info "docker not installed; skipping container force-recreate."
        print_info "  Please run manually on the host:"
        print_info "    docker compose up -d --force-recreate ${services}"
        return 0
    fi

    # shellcheck disable=SC2086
    (cd "$REPO_ROOT" && docker compose up -d --force-recreate $services)
}

# ---- Healthz probe (best-effort) ------------------------------------------
healthz_probe() {
    local container=$1

    if [[ -n "${RAZZFAZZ_TEST_FAKE_HEALTH_FAIL:-}" ]]; then
        print_warning "FAKE-HEALTH-FAIL: ${RAZZFAZZ_TEST_FAKE_HEALTH_FAIL}"
        return 1
    fi
    if [[ -n "${RAZZFAZZ_TEST_FAKE_HEALTH_OK:-}" ]]; then
        print_substep "FAKE-HEALTH-OK: container '${container}' reports healthy"
        return 0
    fi

    if ! command -v docker >/dev/null 2>&1; then
        print_info "docker not installed; skipping healthz probe."
        return 0
    fi

    # Wait up to 30s for the container to come back. We poll docker
    # inspect — Authentik / Synapse / Onyx all take noticeably longer
    # than that to report healthy after a recreate, so this is best-effort
    # only; failure produces a WARN, never a hard exit.
    local i status=""
    for i in $(seq 1 30); do
        status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "missing")
        if [[ "$status" = "healthy" ]]; then
            print_substep "healthz: '${container}' is healthy."
            return 0
        fi
        if [[ "$status" = "starting" ]] || [[ "$status" = "missing" ]]; then
            sleep 1
            continue
        fi
        # unhealthy → break early, don't wait the full 30s.
        break
    done
    print_warning "healthz: '${container}' is '${status}' after 30s — verify manually."
    print_info    "  docker logs ${container} | tail -50"
    return 1
}

# ---- Single-module rotation (the workhorse) -------------------------------
# Returns 0 on success or idempotent no-op, 1 on any failure.
# Caller reads the global FAILED counter for `all` mode.
rotate_one_module() {
    local module=$1 explicit_pw=$2

    local pw_key="${MODULE_TO_PW_KEY[$module]}"
    local user_key="${MODULE_TO_USER_KEY[$module]:-}"
    local user_fallback="${MODULE_TO_USER_FALLBACK[$module]:-}"
    local services="${MODULE_TO_SERVICES[$module]}"
    local health_container="${MODULE_TO_HEALTH_CONTAINER[$module]}"

    # Read current state.
    local pg_admin_user pg_admin_pass current_pw db_user
    pg_admin_user="$(read_env_value "$ENV_FILE" "POSTGRES_USER")"
    pg_admin_pass="$(read_env_value "$ENV_FILE" "POSTGRES_PASSWORD")"
    current_pw="$(read_env_value "$ENV_FILE" "$pw_key")"

    if [[ -n "$user_key" ]]; then
        db_user="$(read_env_value "$ENV_FILE" "$user_key")"
    fi
    if [[ -z "${db_user:-}" ]]; then
        db_user="$user_fallback"
    fi

    if [[ -z "$db_user" ]]; then
        print_error "rotate-db-password[$module]: no DB user known (neither ${user_key} in .env nor a hardcoded fallback)."
        return 1
    fi

    if [[ -z "$pg_admin_user" ]] || [[ -z "$pg_admin_pass" ]]; then
        print_error "rotate-db-password[$module]: POSTGRES_USER / POSTGRES_PASSWORD missing in .env."
        return 1
    fi

    # Determine new password.
    local new_pw pw_source
    if [[ -n "$explicit_pw" ]]; then
        new_pw="$explicit_pw"
        pw_source="supplied"
    else
        new_pw="$(generate_strong_password)"
        pw_source="generated"
    fi

    if ! validate_pw_strength "$new_pw"; then
        return 1
    fi

    # Idempotency probe.
    local noop=0
    if [[ "$new_pw" == "$current_pw" ]]; then
        noop=1
    fi

    print_step    "BSB-05 — rotating DB password for module '$module'"
    print_substep "env file:           $ENV_FILE"
    print_substep "module:             $module"
    print_substep "DB user:            $db_user"
    print_substep "<MOD>_DB_PASSWORD:  $pw_key"
    print_substep "compose services:   $services"
    print_substep "password source:    $pw_source"
    if [[ "$noop" -eq 1 ]]; then
        print_substep "idempotency:        old == new (no-op path will be taken)"
    fi

    # 1. Postgres ALTER USER — must succeed BEFORE we touch .env.
    if ! postgres_alter_user "$pg_admin_user" "$pg_admin_pass" "$db_user" "$new_pw"; then
        print_error "rotate-db-password[$module]: aborting BEFORE .env write — Postgres update failed."
        print_info  "  No state change. .env still holds the OLD password."
        return 1
    fi
    print_substep "Postgres ALTER USER accepted."

    # 2. .env write (idempotent path: skip if old==new).
    if [[ "$noop" -eq 1 ]]; then
        print_substep ".env: $pw_key unchanged (no-op)."
        print_success "BSB-05 rotate-db-password[$module] OK (no-op, no change) $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        return 0
    fi

    update_env_value "$ENV_FILE" "$pw_key" "$new_pw"
    print_substep ".env: $pw_key updated."

    # Dify cascade hint (operator-facing — .env.dify reads from .env so
    # nothing actually has to be edited there; but operators don't always
    # know that).
    if [[ "$module" = "dify" ]]; then
        print_substep ".env.dify cascade: DB_PASSWORD references \${DIFY_DB_PASSWORD} — no separate edit needed."
    fi

    # 3. force-recreate the module's services.
    if ! recreate_services "$services"; then
        # See BSB-05-DEC-02: do NOT auto-rollback. .env + Postgres are
        # consistent; only the running containers still have the old
        # value cached. Operator can re-run the recreate themselves.
        print_warning "rotate-db-password[$module]: container force-recreate FAILED."
        print_info    "  Postgres + .env hold the NEW password — they are consistent."
        print_info    "  Containers may still be using the OLD password until you run:"
        print_info    "    docker compose up -d --force-recreate $services"
        print_info    "  No auto-rollback (re-rotating a working password is its own footgun)."
        return 1
    fi
    print_substep "containers force-recreated."

    # 4. healthz probe (best-effort, WARN-only). Skipped in dry-run.
    if [[ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]] \
       && [[ -z "${RAZZFAZZ_TEST_FAKE_HEALTH_OK:-}" ]] \
       && [[ -z "${RAZZFAZZ_TEST_FAKE_HEALTH_FAIL:-}" ]]; then
        print_substep "DRY-RUN: skipping healthz probe."
    else
        if ! healthz_probe "$health_container"; then
            print_warning "rotate-db-password[$module]: healthz probe failed — verify manually."
            # Best-effort: not a hard failure.
        fi
    fi

    print_success "BSB-05 rotate-db-password[$module] OK $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [[ "$pw_source" = "generated" ]]; then
        echo ""
        echo "================================================================"
        echo "  NEW PASSWORD for module '$module' (capture this — also in .env now):"
        echo ""
        echo "      ${new_pw}"
        echo ""
        echo "  Update the customer password vault if applicable."
        echo "================================================================"
    fi
    return 0
}

# ---- Drive: single module or all ------------------------------------------
if [[ "$MODULE" = "all" ]]; then
    print_step "BSB-05 — rotating DB password for ALL configured modules"
    if [[ "$FAIL_FAST" -eq 1 ]]; then
        print_substep "fail-mode: --fail-fast (abort on first failure)"
    else
        print_substep "fail-mode: continue-on-error (default)"
    fi

    sorted_modules=$(printf '%s\n' "${!MODULE_TO_PW_KEY[@]}" | sort)
    failed_modules=()
    succeeded_modules=()
    skipped_modules=()
    while IFS= read -r mod; do
        pw_key="${MODULE_TO_PW_KEY[$mod]}"
        # Skip modules whose <MOD>_DB_PASSWORD is empty / absent — they
        # aren't actually configured on this stack.
        current="$(read_env_value "$ENV_FILE" "$pw_key")"
        if [[ -z "$current" ]]; then
            print_substep "skipping module '$mod' — $pw_key not set in .env"
            skipped_modules+=("$mod")
            continue
        fi

        if rotate_one_module "$mod" "$NEW_PW_FROM_ARG"; then
            succeeded_modules+=("$mod")
        else
            failed_modules+=("$mod")
            if [[ "$FAIL_FAST" -eq 1 ]]; then
                print_error "rotate-db-password: --fail-fast — aborting after '$mod' failed."
                break
            fi
        fi
    done <<< "$sorted_modules"

    echo ""
    print_step "BSB-05 — all-mode summary"
    print_substep "succeeded: ${#succeeded_modules[@]}  ${succeeded_modules[*]:-(none)}"
    print_substep "failed:    ${#failed_modules[@]}  ${failed_modules[*]:-(none)}"
    print_substep "skipped:   ${#skipped_modules[@]}  ${skipped_modules[*]:-(none)}"

    if [[ "${#failed_modules[@]}" -gt 0 ]]; then
        print_error "BSB-05 rotate-db-password ALL: ${#failed_modules[@]} module(s) failed."
        exit 1
    fi
    print_success "BSB-05 rotate-db-password ALL OK $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 0
fi

# Single-module path.
if rotate_one_module "$MODULE" "$NEW_PW_FROM_ARG"; then
    exit 0
else
    exit 1
fi
