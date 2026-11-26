# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# scripts/lib-backup-keyring.sh — #2132: rotate BACKUP_ENCRYPTION_PASSWORD and
# keep the previous generations (BACKUP_ENCRYPTION_PASSWORD_PREVIOUS_1..3).
# SOURCED: no top-level `set`. Needs scripts/lib.sh (update_env_value) first.
# The plan comes from razzfazz_common.backup_keyring (one implementation for
# the host and the containers); the writes use update_env_value, which keeps
# the .env inode the containers' single-file binds point at (#1224).
# ==============================================================================

# backup_keyring_rotate <env_file> <new_value>
# The plan holds cleartext passphrases: it lives in a private (0700) directory and
# is removed on EVERY path out of the function, including a failed write. The
# write loop handles failure explicitly (set -e is not reliable inside a caller's
# conditional) and no trap is set, so a caller's traps are left alone.
backup_keyring_rotate() {
    local env_file=$1 new=$2 root dir plan kv rc=0
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/rzfz-keyring.XXXXXX")" || return 1
    plan="${dir}/plan"
    if ! printf '%s' "$new" \
        | PYTHONPATH="${root}/core/common${PYTHONPATH:+:$PYTHONPATH}" \
          python3 -m razzfazz_common.backup_keyring plan --env "$env_file" > "$plan"; then
        rm -rf "$dir"
        return 1
    fi
    # The plan lists the previous slots first and the current key last: a failure
    # part-way never loses the old passphrase.
    while IFS= read -r -d '' kv; do
        if ! update_env_value "$env_file" "${kv%%=*}" "${kv#*=}"; then
            rc=1
            break
        fi
    done < "$plan"
    rm -rf "$dir"
    return "$rc"
}
