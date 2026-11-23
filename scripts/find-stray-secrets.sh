#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# scripts/find-stray-secrets.sh — secret-bearing leftovers OUTSIDE the install
# path: plaintext stack backups (backup-*/partial-*.tar.gz, no .gpg), env files
# that carry a stack secret, and parked clones of the stack tree.
#
# Usage: find-stray-secrets.sh --stack-dir <install path> [--root <dir>]...
#   Default roots: the install path's parent directory and $HOME. Read-only;
#   never follows another filesystem (-xdev), depth <= 4, and each root's scan
#   is cut off after RZFZ_STRAY_SCAN_TIMEOUT seconds (default 20).
# Output: one line per finding — KIND|path|age_days|mode — KIND is one of
#   PLAINTEXT_BACKUP, STRAY_ENV, PARKED_CLONE, INCOMPLETE (the scan of
#   that root did not finish: the path field is the root, age holds the reason), or
#   SKIPPED (scanned, but some directories were unreadable: informational).
#   Secret values are never printed.
#   Exit 0: findings are data; exit 2: usage error.
#
# Why: an abandoned clone can hold plaintext stack backups (hundreds of GB,
# months old, mode 0644) and a plaintext .env/.env.dify, and parked .env copies
# (mode 0664) can sit in the operator's home. `rzfz status` already flags
# plaintext archives INSIDE backups/.
# ==============================================================================
set -euo pipefail

usage() { sed -n '4,15p' "$0" | sed 's/^# \{0,1\}//'; }

STACK_DIR=""
ROOTS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --stack-dir) STACK_DIR="${2:?--stack-dir needs a path}"; shift 2 ;;
        --root)      ROOTS+=("${2:?--root needs a path}"); shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        *)           echo "find-stray-secrets: unknown option: $1" >&2; exit 2 ;;
    esac
done
[ -n "$STACK_DIR" ] || { echo "find-stray-secrets: --stack-dir is required" >&2; exit 2; }
[ -d "$STACK_DIR" ] || { echo "find-stray-secrets: not a directory: $STACK_DIR" >&2; exit 2; }
STACK_DIR="$(cd "$STACK_DIR" && pwd -P)"
if [ "${#ROOTS[@]}" -eq 0 ]; then
    ROOTS=("$(dirname "$STACK_DIR")")
    if [ -n "${HOME:-}" ] && [ -d "$HOME" ]; then ROOTS+=("$HOME"); fi
fi
SCAN_TIMEOUT="${RZFZ_STRAY_SCAN_TIMEOUT:-20}"

# A value right after `=`: an empty template line (config/.env.example) is not a secret.
SECRET_RE='^(export[[:space:]]+)?(POSTGRES_PASSWORD|AUTHENTIK_SECRET_KEY|AUTHENTIK_BOOTSTRAP_PASSWORD|BACKUP_ENCRYPTION_PASSWORD|VALKEY_PASSWORD)=[^[:space:]]'
now=$(date +%s)
declare -A seen=()

emit() {  # <kind> <path>
    local m mode
    m=$(stat -c '%Y' -- "$2" 2>/dev/null || echo "$now")
    mode=$(stat -c '%a' -- "$2" 2>/dev/null || echo '?')
    printf '%s|%s|%s|%s\n' "$1" "$2" "$(( (now - m) / 86400 ))" "$mode"
}

declare -A scanned=()
skipped() {  # <root> <note>: a scan whose only errors were unreadable dirs counts as done
    printf 'SKIPPED|%s|%s|-\n' "$1" "$2"
}
errf="$(mktemp "${TMPDIR:-/tmp}/find-stray-secrets.XXXXXX")"
trap 'rm -f "$errf"' EXIT
incomplete() {  # <root> <reason>: a scan that did not finish must never read as clean
    printf 'INCOMPLETE|%s|%s|-\n' "$1" "$2"
}

for root in "${ROOTS[@]}"; do
    [ -d "$root" ] || continue
    root="$(cd "$root" && pwd -P)"
    if [ -n "${scanned[$root]:-}" ]; then continue; fi
    scanned[$root]=1
    if ! command -v timeout >/dev/null 2>&1; then
        incomplete "$root" "timeout is not installed, root not scanned"
        continue
    fi
    # find's exit code travels through the stream as a final "__RC__=<n>" record:
    # a process substitution loses it otherwise.
    while IFS= read -r -d '' f; do
        case "$f" in
            __RC__=0) continue ;;
            __PERM__=*) skipped "$root" "${f#__PERM__=} unreadable dirs"; continue ;;
            __RC__=124) incomplete "$root" "timed out after ${SCAN_TIMEOUT}s"; continue ;;
            __RC__=*) incomplete "$root" "find failed (exit ${f#__RC__=})"; continue ;;
        esac
        if [ -n "${seen[$f]:-}" ]; then continue; fi
        seen[$f]=1
        case "$(basename "$f")" in
            upgrade.sh)
                clone="$(dirname "$(dirname "$f")")"
                if [ -f "$clone/compose.yml" ]; then emit PARKED_CLONE "$clone"; fi ;;
            backup-*.tar.gz|partial-*.tar.gz)
                emit PLAINTEXT_BACKUP "$f" ;;
            *)
                if grep -Eqs "$SECRET_RE" -- "$f"; then emit STRAY_ENV "$f"; fi ;;
        esac
    done < <(
        rc=0
        timeout "$SCAN_TIMEOUT" find "$root" -xdev -maxdepth 4 \
                \( -path "$STACK_DIR" -o -name node_modules -o -name .git -o -name .cache -o -name tests \) -prune \
             -o -type f -path '*/cli/upgrade.sh' -print0 \
             -o -type f \( -name 'backup-*.tar.gz' -o -name 'partial-*.tar.gz' \) -print0 \
             -o -type f -size -2M \( -name '.env' -o -name '.env.*' -o -name 'env.*' -o -name '*.env' \) ! -name '*.example' -print0 \
             2>"$errf" || rc=$?
        # Only "Permission denied" errors (rc 1): the root was scanned, some
        # directories were unreadable. Any other error, or a timeout, is not.
        nperm=$(LC_ALL=C grep -c 'Permission denied$' "$errf" || true)
        nother=$(LC_ALL=C grep -vc 'Permission denied$' "$errf" || true)
        if [ "$rc" -eq 1 ] && [ "${nperm:-0}" -gt 0 ] && [ "${nother:-0}" -eq 0 ]; then
            printf '__PERM__=%s\0' "$nperm"
        else
            printf '__RC__=%s\0' "$rc"
        fi)
done
exit 0
