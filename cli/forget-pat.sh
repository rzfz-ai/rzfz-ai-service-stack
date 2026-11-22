#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# rzfz forget-pat [--dry-run] — remove a stored Gitea credential from this box.
#
# Removes every place the git.razzfazz.ai credential is kept, or would be kept,
# and touches no other host: credential helpers for that host (one that also
# serves other hosts is disabled for git.razzfazz.ai only), its entries in the
# credential files (other hosts' lines stay), user info for that host in the
# checkout's remote URLs and url.<base>.insteadOf rewrites, and in .env
# STACK_GIT_CREDENTIALS_PATH and any value with user info for that host.
# Reports what it removed by name only, never a value. Run it as the stack's
# admin user, or with sudo (the admin user's files are covered too).
#   --dry-run   list what would be removed; change nothing
# Afterwards upgrades ask for the PAT at a terminal and do not store it, or use
# an offline package: rzfz upgrade --package <file>.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${SCRIPT_DIR}"
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

usage() { sed -n '5,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

mode=remove
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --dry-run) mode=list ;;
    "") ;;
    *) echo "ERROR: unknown option '$1'" >&2; usage >&2; exit 2 ;;
esac

found=$(razzfazz_git_credential_scan "$SCRIPT_DIR" "$mode")
if [ -z "$found" ]; then
    echo "No stored Gitea credential on this box."
    exit 0
fi
printf '%s\n' "$found" | while IFS=$'\t' read -r done_ what where; do
    verb="$done_"                       # removed / disabled for git.razzfazz.ai
    [ "$mode" = list ] && verb="would remove"
    printf '%s: %s (%s)\n' "$verb" "$what" "$where"
done
if printf '%s\n' "$found" | grep -q 'STACK_GIT_CREDENTIALS_PATH'; then
    echo "The Settings portal still mounts the old path until it is recreated:"
    echo "    docker compose up -d razzfazz-config"
fi
if printf '%s\n' "$found" | grep -q 'a credential helper'; then
    echo "A keyring or credential manager behind a helper may still hold the token: remove it there."
    echo "(A helper that also serves other hosts is kept for them and disabled for git.razzfazz.ai only.)"
fi
if printf '%s\n' "$found" | grep -q 'with a password in its text' \
   && printf '%s\n' "$found" | grep -v '/\.git/config$' | grep -q 'with a password in its text'; then
    echo "A global git config has a helper with a password written into its text. It stays for other hosts"
    echo "and is disabled for git.razzfazz.ai, but the password is still on disk: remove that helper by hand."
fi
[ "$mode" = list ] && echo "Nothing was changed (--dry-run)."
exit 0
