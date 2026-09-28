#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz.ai Stack - .env Snapshot Utility
# ==============================================================================
# Creates encrypted snapshots of .env and .env.dify before config changes.
# Encryption uses AES-256-CBC with the admin password (or BACKUP_ENCRYPTION_PASSWORD).
#
# Usage (sourced by other scripts):
#   source scripts/env-snapshot.sh
#   env_snapshot "reason for snapshot"
#
# Usage (standalone):
#   ./scripts/env-snapshot.sh take "before upgrade"
#   ./scripts/env-snapshot.sh list
#   ./scripts/env-snapshot.sh restore <snapshot-name>
#
# Snapshots are stored in backups/env-snapshots/ as encrypted .tar.gz.enc files.
# ==============================================================================

_ENV_SNAPSHOT_DIR=""
_ENV_SNAPSHOT_SCRIPT_DIR=""

_env_snapshot_init() {
    _ENV_SNAPSHOT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local stack_dir="${_ENV_SNAPSHOT_SCRIPT_DIR}/.."
    _ENV_SNAPSHOT_DIR="${stack_dir}/backups/env-snapshots"
    
    # Try to create dir — fall back to .gsd/ if backups/ is not writable
    if ! mkdir -p "$_ENV_SNAPSHOT_DIR" 2>/dev/null; then
        _ENV_SNAPSHOT_DIR="${stack_dir}/.gsd/env-snapshots"
        mkdir -p "$_ENV_SNAPSHOT_DIR" 2>/dev/null || true
    fi
}

_env_snapshot_get_password() {
    # Source .env to get the encryption password
    local stack_dir="${_ENV_SNAPSHOT_SCRIPT_DIR}/.."
    local env_file="${stack_dir}/.env"
    
    if [ ! -f "$env_file" ]; then
        echo ""
        return
    fi
    
    # Use dedicated BACKUP_ENCRYPTION_PASSWORD only (no fallback to admin password)
    local pass
    pass=$(grep "^BACKUP_ENCRYPTION_PASSWORD=" "$env_file" 2>/dev/null | cut -d= -f2-)
    echo "$pass"
}

env_snapshot() {
    local reason="${1:-auto-snapshot}"
    _env_snapshot_init
    
    local stack_dir="${_ENV_SNAPSHOT_SCRIPT_DIR}/.."
    local env_file="${stack_dir}/.env"
    local env_dify="${stack_dir}/.env.dify"
    local timestamp
    timestamp=$(date +%Y%m%d-%H%M%S)
    local snapshot_name="env-${timestamp}"
    local snapshot_file="${_ENV_SNAPSHOT_DIR}/${snapshot_name}.tar.gz.enc"
    
    # Check if .env exists
    if [ ! -f "$env_file" ]; then
        return 0  # No .env to snapshot — silent skip
    fi
    
    # Get encryption password
    local pass
    pass=$(_env_snapshot_get_password)
    if [ -z "$pass" ]; then
        # No password available — store unencrypted tar.gz as fallback
        snapshot_file="${_ENV_SNAPSHOT_DIR}/${snapshot_name}.tar.gz"
        local files=".env"
        [ -f "$env_dify" ] && files="$files .env.dify"
        
        tar czf "$snapshot_file" -C "$stack_dir" $files 2>/dev/null
        
        # Write metadata
        echo "$reason" > "${_ENV_SNAPSHOT_DIR}/${snapshot_name}.reason"
        return 0
    fi
    
    # Create encrypted snapshot (disable pipefail for tar|openssl pipe)
    local files=".env"
    [ -f "$env_dify" ] && files="$files .env.dify"
    
    set +eo pipefail 2>/dev/null
    tar czf - -C "$stack_dir" $files 2>/dev/null | \
        openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
        -pass "pass:${pass}" -out "$snapshot_file" 2>/dev/null
    local snap_rc=$?
    set -eo pipefail 2>/dev/null
    
    if [ $snap_rc -eq 0 ] && [ -f "$snapshot_file" ]; then
        # Write metadata (unencrypted reason file)
        echo "$reason" > "${_ENV_SNAPSHOT_DIR}/${snapshot_name}.reason"
        
        # Cleanup: keep only last 50 snapshots
        local count
        count=$(find "$_ENV_SNAPSHOT_DIR" -name "env-*.tar.gz*" -type f 2>/dev/null | wc -l)
        if [ "$count" -gt 50 ]; then
            find "$_ENV_SNAPSHOT_DIR" -name "env-*.tar.gz*" -type f -printf '%T@ %p\n' 2>/dev/null | \
                sort -n | head -n -50 | awk '{print $2}' | while read -r old; do
                rm -f "$old" "${old%.tar.gz.enc}.reason" "${old%.tar.gz}.reason" 2>/dev/null
            done
        fi
    fi
}

env_snapshot_list() {
    _env_snapshot_init
    
    echo "Available .env snapshots:"
    echo ""
    
    local found=0
    for f in $(find "$_ENV_SNAPSHOT_DIR" -name "env-*.tar.gz*" -type f 2>/dev/null | sort); do
        found=1
        local name
        name=$(basename "$f")
        local base="${name%.tar.gz.enc}"
        base="${base%.tar.gz}"
        local reason=""
        [ -f "${_ENV_SNAPSHOT_DIR}/${base}.reason" ] && reason=$(cat "${_ENV_SNAPSHOT_DIR}/${base}.reason")
        local size
        size=$(du -h "$f" 2>/dev/null | awk '{print $1}')
        local encrypted="🔒"
        echo "$name" | grep -q ".enc$" || encrypted="⚠️"
        
        printf "  %s %-40s %6s  %s\n" "$encrypted" "$name" "$size" "$reason"
    done
    
    if [ $found -eq 0 ]; then
        echo "  (no snapshots found)"
    fi
}

env_snapshot_restore() {
    local snapshot_name="$1"
    _env_snapshot_init
    
    local snapshot_file="${_ENV_SNAPSHOT_DIR}/${snapshot_name}"
    if [ ! -f "$snapshot_file" ]; then
        echo "ERROR: Snapshot not found: $snapshot_file"
        return 1
    fi
    
    local stack_dir="${_ENV_SNAPSHOT_SCRIPT_DIR}/.."
    local pass
    pass=$(_env_snapshot_get_password)
    
    if echo "$snapshot_file" | grep -q ".enc$"; then
        if [ -z "$pass" ]; then
            echo "ERROR: No decryption password available."
            return 1
        fi
        
        openssl enc -d -aes-256-cbc -salt -pbkdf2 -iter 100000 \
            -pass "pass:${pass}" -in "$snapshot_file" 2>/dev/null | \
            tar xzf - -C "$stack_dir" 2>/dev/null
    else
        tar xzf "$snapshot_file" -C "$stack_dir" 2>/dev/null
    fi
    
    if [ $? -eq 0 ]; then
        echo "Restored .env from: $snapshot_name"
    else
        echo "ERROR: Failed to restore snapshot."
        return 1
    fi
}

# ==============================================================================
# Standalone mode
# ==============================================================================
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-}" in
        take)
            env_snapshot "${2:-manual snapshot}"
            echo "Snapshot created."
            env_snapshot_list | tail -3
            ;;
        list)
            env_snapshot_list
            ;;
        restore)
            if [ -z "$2" ]; then
                echo "Usage: $0 restore <snapshot-name>"
                exit 1
            fi
            env_snapshot_restore "$2"
            ;;
        *)
            echo "Usage: $0 {take [reason]|list|restore <name>}"
            exit 1
            ;;
    esac
fi
