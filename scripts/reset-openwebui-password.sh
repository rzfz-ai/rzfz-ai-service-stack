#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/reset-openwebui-password.sh
# Reset the password of an Open WebUI admin user.
#
# Usage:
#   ./scripts/reset-openwebui-password.sh
#   ./scripts/reset-openwebui-password.sh --email admin@example.com
#   ./scripts/reset-openwebui-password.sh --email admin@example.com --password 'NewPass123!'
#
# Requires: docker, running postgres container
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

# ---- Load .env --------------------------------------------------------------
[[ -f "$ENV_FILE" ]] || { echo "ERROR: .env not found at $ENV_FILE"; exit 1; }
source "$ENV_FILE"

POSTGRES_USER="${POSTGRES_USER:-docker}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
OPENWEBUI_DB="${OPENWEBUI_DB:-openwebui_db}"

# ---- Parse arguments --------------------------------------------------------
TARGET_EMAIL=""
NEW_PASSWORD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --email)    TARGET_EMAIL="$2";   shift 2 ;;
        --password) NEW_PASSWORD="$2";   shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ---- Check postgres is running ----------------------------------------------
docker exec postgres psql -U "$POSTGRES_USER" -d "$OPENWEBUI_DB" -c "SELECT 1" \
    > /dev/null 2>&1 || { echo "ERROR: postgres container not running or unreachable."; exit 1; }

# ---- List admins and select target ------------------------------------------
echo ""
echo "Open WebUI admin users:"
echo "─────────────────────────────────────────────"
docker exec postgres psql -U "$POSTGRES_USER" -d "$OPENWEBUI_DB" -t -c \
    "SELECT '  ' || email FROM \"user\" WHERE role = 'admin' ORDER BY email;" \
    | grep -v "^$"
echo "─────────────────────────────────────────────"
echo ""

if [[ -z "$TARGET_EMAIL" ]]; then
    read -rp "Enter admin email to reset: " TARGET_EMAIL
fi

# Verify the email exists and is admin
ROLE=$(docker exec postgres psql -U "$POSTGRES_USER" -d "$OPENWEBUI_DB" -t -c \
    "SELECT role FROM \"user\" WHERE email = '${TARGET_EMAIL}';" | tr -d '[:space:]')

[[ -n "$ROLE" ]]   || { echo "ERROR: No user found with email: $TARGET_EMAIL"; exit 1; }
[[ "$ROLE" == "admin" ]] || {
    echo "WARNING: User '$TARGET_EMAIL' has role '$ROLE' (not admin)."
    read -rp "Continue anyway? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
}

# ---- Get new password -------------------------------------------------------
if [[ -z "$NEW_PASSWORD" ]]; then
    while true; do
        read -rsp "New password: " NEW_PASSWORD; echo
        read -rsp "Confirm password: " CONFIRM_PASSWORD; echo
        [[ "$NEW_PASSWORD" == "$CONFIRM_PASSWORD" ]] && break
        echo "Passwords do not match. Try again."
    done
fi

[[ ${#NEW_PASSWORD} -ge 8 ]] || { echo "ERROR: Password must be at least 8 characters."; exit 1; }

# ---- Hash password with bcrypt (via Python in postgres container) -----------
HASHED=$(docker exec postgres python3 -c \
    "import bcrypt; print(bcrypt.hashpw('${NEW_PASSWORD}'.encode(), bcrypt.gensalt(rounds=12)).decode())" \
    2>/dev/null) || {
    # Fallback: run python in a temporary container if postgres image lacks bcrypt
    HASHED=$(docker run --rm python:3.11-alpine sh -c \
        "pip install bcrypt -q && python3 -c \"import bcrypt; print(bcrypt.hashpw('${NEW_PASSWORD}'.encode(), bcrypt.gensalt(rounds=12)).decode())\"")
}

[[ "$HASHED" == '$2b$'* ]] || { echo "ERROR: Failed to generate bcrypt hash."; exit 1; }

# ---- Update password in database --------------------------------------------
docker exec postgres psql -U "$POSTGRES_USER" -d "$OPENWEBUI_DB" -c \
    "UPDATE auth SET password = '${HASHED}' WHERE id = (SELECT id FROM \"user\" WHERE email = '${TARGET_EMAIL}');"

echo ""
echo "Password successfully reset for: $TARGET_EMAIL"
echo "They can now log in with the new password."
echo ""
