#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/owui-demote-user.sh
# Demote an Open WebUI admin user to a regular user, safely.
#
# Why this script exists
# ----------------------
# Open WebUI ships with a single admin role; demoting the LAST admin to
# 'user' locks the install out of its own admin panel — pipes vanish, no
# admin UI, no model management. On a single-user dev box this is fatal:
# the operator (memory feedback_dev_owui_no_demote.md) burned themselves
# twice on 2026-05-12 typing `UPDATE "user" SET role='user' …` by hand.
#
# This script is the canonical, guarded way to issue that demotion.
# Future razzfazz-managed code MUST go through this script (or its
# embedded guard logic) instead of writing UPDATE SQL directly.
#
# Guard rules
# -----------
# 1. Refuse to demote if it would leave 0 admins.
# 2. Refuse to demote `akadmin` when it is the only admin (single-user
#    dev-box trap — even more emphatic message).
# 3. Refuse to demote a user that is not currently an admin (no-op +
#    clear error).
# 4. Refuse to operate on an unknown email.
# 5. Override: --force-leave-no-admin. Verbose-on-purpose; bare --force
#    is rejected so operator muscle-memory can't bypass the guard.
#    See BSB-15-DEC-02.
#
# Usage
# -----
#   scripts/owui-demote-user.sh --email user@example.com
#   scripts/owui-demote-user.sh --email user@example.com --force-leave-no-admin
#   scripts/owui-demote-user.sh --help
#
# Test hooks (do nothing in production — see BSB-15-DEC-03)
# ---------------------------------------------------------
#   RAZZFAZZ_TEST_DRY_RUN=1            short-circuit the actual UPDATE
#   RAZZFAZZ_TEST_FAKE_ADMIN_COUNT=N   pretend OWUI has N admins
#   RAZZFAZZ_TEST_FAKE_USER_ROLE=role  pretend the target user has this role
# =============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---- Help ------------------------------------------------------------------
show_help() {
    cat <<'EOF'
scripts/owui-demote-user.sh — safely demote an Open WebUI admin to user

USAGE:
    scripts/owui-demote-user.sh --email <ADDR> [--force-leave-no-admin]
    scripts/owui-demote-user.sh --help

OPTIONS:
    --email <ADDR>              Email address of the OWUI user to demote.
    --force-leave-no-admin      Override the last-admin protection.
                                Verbose on purpose: bare --force is rejected
                                so operator muscle-memory can't bypass the
                                guard. Use only during a controlled rebuild
                                (e.g. wiping the openwebui DB and reseeding).
    -h, --help                  Show this help.

GUARD RULES:
    1. Refuses to demote if it would leave 0 admins.
    2. Refuses to demote 'akadmin' when it is the only admin (single-user
       dev-box trap — see memory feedback_dev_owui_no_demote.md).
    3. Refuses to demote a non-admin user.
    4. Refuses to operate on an unknown email.

REMEDIATION when the guard fires:
    First promote a different user to admin (via the OWUI UI, or by
    running an UPDATE that SETS role='admin' for another email), THEN
    re-run this script.

EXIT CODES:
    0   demotion completed (or dry-run announced)
    1   guard fired / invalid args / DB unreachable
    2   bare --force given (rejected — use --force-leave-no-admin)

EOF
}

# ---- Parse args ------------------------------------------------------------
TARGET_EMAIL=""
FORCE_LEAVE_NO_ADMIN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --email)
            [[ $# -ge 2 ]] || { echo "ERROR: --email requires an argument" >&2; exit 1; }
            TARGET_EMAIL="$2"
            shift 2
            ;;
        --force-leave-no-admin)
            FORCE_LEAVE_NO_ADMIN=1
            shift
            ;;
        --force)
            # Hard-rejected (BSB-15-DEC-02). Operator muscle-memory bypass.
            cat <<'EOF' >&2
ERROR: bare --force is not accepted by this script.

The override flag is intentionally verbose:
    --force-leave-no-admin

This is a guard against typing --force without reading what it overrides.
See scripts/owui-demote-user.sh --help.
EOF
            exit 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "ERROR: unknown option: $1" >&2
            echo "Try: $0 --help" >&2
            exit 1
            ;;
    esac
done

if [[ -z "$TARGET_EMAIL" ]]; then
    echo "ERROR: --email <ADDR> is required" >&2
    echo "Try: $0 --help" >&2
    exit 1
fi

# ---- Load .env (targeted grep, never source — see lib.sh design rules) ----
ENV_FILE="${REPO_ROOT}/.env"
read_env_value() {
    local key="$1" file="${2:-$ENV_FILE}"
    [[ -f "$file" ]] || return 0
    # Last assignment wins; tolerate quoted/unquoted forms.
    local line
    line=$(grep -E "^${key}=" "$file" | tail -n 1 || true)
    [[ -z "$line" ]] && return 0
    local val="${line#*=}"
    val="${val%\"}"; val="${val#\"}"
    val="${val%\'}"; val="${val#\'}"
    printf '%s' "$val"
}

POSTGRES_USER="$(read_env_value POSTGRES_USER)"
POSTGRES_USER="${POSTGRES_USER:-docker}"
OPENWEBUI_DB="$(read_env_value OPENWEBUI_DB)"
OPENWEBUI_DB="${OPENWEBUI_DB:-openwebui_db}"

# ---- Discover admin count + target's current role -------------------------
# Test hooks short-circuit the docker exec entirely (BSB-15-DEC-03).
admin_count=""
target_role=""

if [[ -n "${RAZZFAZZ_TEST_FAKE_ADMIN_COUNT:-}" ]]; then
    admin_count="$RAZZFAZZ_TEST_FAKE_ADMIN_COUNT"
fi
if [[ -n "${RAZZFAZZ_TEST_FAKE_USER_ROLE+x}" ]]; then
    # Set even if empty — empty role means "no such user".
    target_role="$RAZZFAZZ_TEST_FAKE_USER_ROLE"
fi

if [[ -z "$admin_count" ]] || [[ -z "${RAZZFAZZ_TEST_FAKE_USER_ROLE+x}" ]]; then
    # Need to consult the real DB for at least one of the two values.
    if ! command -v docker >/dev/null 2>&1; then
        echo "ERROR: docker not installed — cannot reach openwebui DB." >&2
        exit 1
    fi
    if ! docker exec postgres psql -U "$POSTGRES_USER" -d "$OPENWEBUI_DB" \
            -c "SELECT 1" >/dev/null 2>&1; then
        echo "ERROR: postgres container not running or openwebui DB unreachable." >&2
        echo "       Tried: docker exec postgres psql -U $POSTGRES_USER -d $OPENWEBUI_DB" >&2
        exit 1
    fi
    if [[ -z "$admin_count" ]]; then
        admin_count=$(docker exec postgres psql -U "$POSTGRES_USER" \
            -d "$OPENWEBUI_DB" -t -c \
            "SELECT count(*) FROM \"user\" WHERE role = 'admin';" \
            2>/dev/null | tr -d '[:space:]')
    fi
    if [[ -z "${RAZZFAZZ_TEST_FAKE_USER_ROLE+x}" ]]; then
        target_role=$(docker exec postgres psql -U "$POSTGRES_USER" \
            -d "$OPENWEBUI_DB" -t -c \
            "SELECT role FROM \"user\" WHERE email = '${TARGET_EMAIL}';" \
            2>/dev/null | tr -d '[:space:]')
    fi
fi

# Sanity: admin_count must be a non-negative integer.
if ! [[ "$admin_count" =~ ^[0-9]+$ ]]; then
    echo "ERROR: could not determine current admin count (got: '$admin_count')" >&2
    exit 1
fi

# ---- Guard 4: unknown user ------------------------------------------------
if [[ -z "$target_role" ]]; then
    cat <<EOF >&2
ERROR: no user found with email '${TARGET_EMAIL}' in openwebui DB.

The user does not exist. Either:
  - Check the email spelling (the OWUI lookup is case-sensitive).
  - List existing admins:
      docker exec postgres psql -U ${POSTGRES_USER} -d ${OPENWEBUI_DB} \\
        -c "SELECT email, role FROM \\"user\\" ORDER BY role, email;"
EOF
    exit 1
fi

# ---- Guard 3: not currently an admin --------------------------------------
if [[ "$target_role" != "admin" ]]; then
    cat <<EOF >&2
ERROR: user '${TARGET_EMAIL}' is not an admin (current role: ${target_role}).

There is nothing to demote. If you intended to demote a DIFFERENT user,
re-run with the correct --email.
EOF
    exit 1
fi

# ---- Guard 1+2: last-admin protection -------------------------------------
if [[ "$admin_count" -le 1 ]]; then
    if [[ "$FORCE_LEAVE_NO_ADMIN" -ne 1 ]]; then
        # Build the akadmin-emphasis prefix if applicable.
        akadmin_warning=""
        if [[ "$TARGET_EMAIL" == "akadmin@local" ]] || [[ "$TARGET_EMAIL" =~ ^akadmin($|@) ]]; then
            akadmin_warning=$(cat <<'EOF'

This is the bootstrap 'akadmin' account on what looks like a single-user
dev-style box. Demoting akadmin here is the EXACT scenario from operator
memory feedback_dev_owui_no_demote.md — "single-user dev box; demoting
from admin after temp-elevation locks operator out (pipes vanish, no
admin panel)". Operator burned twice on 2026-05-12. DO NOT proceed.
EOF
            )
        fi
        cat <<EOF >&2
ERROR: refusing to demote — '${TARGET_EMAIL}' is the LAST remaining admin
       in the openwebui DB (admin_count=${admin_count}). Demoting it would
       leave 0 admins; the OWUI admin panel would become unreachable
       (pipes vanish, no model management, no user admin).
${akadmin_warning}

REMEDIATION:
  1. Promote a DIFFERENT user to admin first, e.g. via the OWUI UI
     (Settings → Admin Panel → Users → set role to admin), or:
       docker exec postgres psql -U ${POSTGRES_USER} -d ${OPENWEBUI_DB} -c \\
         "UPDATE \\"user\\" SET role='admin' WHERE email='other@example.com';"
  2. Re-run this script — the guard will allow the demotion now that
     another admin exists.

OVERRIDE (not recommended in normal operation):
       $0 --email '${TARGET_EMAIL}' --force-leave-no-admin

  Use the override only during a controlled rebuild (e.g. wiping the
  openwebui DB and reseeding).
EOF
        exit 1
    else
        # Override path — log loudly so the audit trail captures it.
        echo "WARN: --force-leave-no-admin given — proceeding with demotion of last admin '${TARGET_EMAIL}'." >&2
    fi
fi

# ---- Apply ----------------------------------------------------------------
if [[ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]]; then
    echo "DRY-RUN: would demote '${TARGET_EMAIL}' (admin → user) in ${OPENWEBUI_DB}."
    if [[ "$FORCE_LEAVE_NO_ADMIN" -eq 1 ]]; then
        echo "DRY-RUN: --force-leave-no-admin would have been honored."
    fi
    exit 0
fi

docker exec postgres psql -U "$POSTGRES_USER" -d "$OPENWEBUI_DB" -c \
    "UPDATE \"user\" SET role='user' WHERE email='${TARGET_EMAIL}';"

echo "OK: demoted '${TARGET_EMAIL}' from admin to user."
