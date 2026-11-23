#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# scripts/openbao-restore.sh <backup-dir> [--env <file>] [--keys-only] [--yes]
#   — restore OpenBao from a backup (#2476, decision record D9).
#
# <backup-dir> holds databases/openbao/{raft.snap,keys.tar.enc} (an extracted
# archive; its backup/databases/openbao/ layout is found as well), written by
# core/backup/pre-backup.sh §8.
#
# Static seal: the snapshot's keyring is sealed by the SOURCE box's seal key.
# So, on a box whose `openbao` profile is on and whose OpenBao is running:
#   1. find the passphrase that opens keys.tar.enc (touches nothing);
#   2. restore the snapshot with THIS box's own rzfz-config login
#      (`bao operator raft snapshot restore -force`);
#   3. stop openbao; replace the contents of the openbao-keys, openbao-cred and
#      openbao-mcp-cred volumes with the archive's (the source's seal key,
#      recovery key and AppRole credentials, which the restored store accepts);
#   4. start openbao-keygen (its checks pass: data and key both present), then
#      openbao, and wait until it is unsealed;
#   5. log in with the restored rzfz-config credentials, which proves the
#      credentials and the store belong together.
# --keys-only skips step 2: for a box whose OpenBao data is intact but whose
# keys or credentials volume was lost (openbao-keygen refuses with "storage
# holds data but seal.key is missing"); OpenBao need not be running then. It
# refuses (exit 2, nothing changed) when the box's seal.key is present,
# non-empty and not the backup's: that key may be the only one that opens this
# box's data (compared by hash, never printed).
#
# Step 2 is the point of no return: it replaces this box's whole store. It is
# announced and needs a typed "yes" on a terminal, or --yes; without a
# terminal and without --yes the script refuses. From there on, every failure
# prints the exact command that finishes the restore (--keys-only with the
# same --env and flags).
#
# The escrow passphrase is the backup passphrase: ENCRYPTION_PASS when given,
# then the keyring in <file> (default: the stack's .env), current first, then
# the kept previous generations (razzfazz_common.backup_keyring.KEYRING_KEYS,
# read through its own reader, the one the backup side uses),
# so a backup made before a passphrase rotation still restores. Every
# candidate travels in the environment (-pass env:), never on argv.
#
# The recovery key in the archive opens OpenBao's recovery mode for storage
# repair; it does not regain root (D9 note). Admin access after a restore is
# `rzfz openbao admin-shell`, with the restored credentials.
#
# Exit: 0 ok; 2 usage, the box is not ready, not confirmed, or --keys-only
# would replace a different seal key (nothing was changed);
# 3 keys archive or snapshot missing; 4 no passphrase decrypts the keys
# archive (or there is none); 5 the snapshot restore failed (the keys were not
# changed, but the snapshot may already be in: the message says how to
# finish); 6 not unsealed in time, or the restored credentials do not log in
# (the snapshot and the keys archive are not one backup set); 7 the keys could
# not be put in place (the snapshot IS restored: re-run with --keys-only and the
# same backup). An interrupt (INT/TERM/HUP) after the point of no return exits
# 130/143/129 and prints the finishing command too.
set -euo pipefail

STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${STACK_ROOT}/scripts/lib.sh"

usage() { echo "usage: scripts/openbao-restore.sh <backup-dir> [--env <file>] [--keys-only] [--yes]" >&2; exit 2; }

dir=""
env_file="${STACK_ROOT}/.env"
env_given=false
keys_only=false
assume_yes=false
while [ $# -gt 0 ]; do
    case "$1" in
        --env) [ $# -ge 2 ] || usage; env_file=$2; env_given=true; shift 2 ;;
        --keys-only) keys_only=true; shift ;;
        --yes) assume_yes=true; shift ;;
        -h|--help) usage ;;
        -*) usage ;;
        *) [ -z "$dir" ] || usage; dir=$1; shift ;;
    esac
done
[ -n "$dir" ] || usage

src="$dir/databases/openbao"
[ -d "$src" ] || [ ! -d "$dir/backup/databases/openbao" ] || src="$dir/backup/databases/openbao"
snap="$src/raft.snap"
keys="$src/keys.tar.enc"
[ -s "$keys" ] || { echo "ERROR: $keys is missing: this backup cannot open its OpenBao snapshot. Nothing was changed." >&2; exit 3; }
if [ "$keys_only" = false ] && [ ! -s "$snap" ]; then
    echo "ERROR: $snap is missing. Nothing was changed." >&2
    exit 3
fi

# Kept equal to pre-backup.sh's OB_ESCROW_REQUIRED (test_2476_openbao_restore.py).
REQUIRED="keys/seal.key keys/recovery.key cred/config.role_id cred/config.secret_id cred/backup.role_id cred/backup.secret_id cred/portal.role_id cred/portal.secret_id"

# The passphrase is a plain shell variable, put into ONE process's environment
# (openssl's); every other child (docker, tar, ...) never sees it (m2).
_given=${ENCRYPTION_PASS:-}
unset ENCRYPTION_PASS
_pass=""
decrypt() { ENCRYPTION_PASS="$_pass" openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -in "$keys" -pass env:ENCRYPTION_PASS; }

# 1. The passphrase: it must decrypt the archive AND yield every required file.
opens() {
    local listing f
    _pass=$1
    listing=$(decrypt 2>/dev/null | tar tf - 2>/dev/null) || return 1
    for f in $REQUIRED; do
        printf '%s\n' "$listing" | grep -qxF "$f" || return 1
    done
}
candidates=()
if [ -n "$_given" ]; then candidates+=("$_given"); fi
# #2607: the keyring is read by razzfazz_common.backup_keyring, the reader the
# backup side uses, so a quoted value both sides must see alike cannot differ.
# NUL-separated: a passphrase may hold spaces or a newline.
while IFS= read -r -d '' v; do
    candidates+=("$v")
done < <(PYTHONPATH="${STACK_ROOT}/core/common${PYTHONPATH:+:$PYTHONPATH}" \
         python3 -m razzfazz_common.backup_keyring candidates --env "$env_file" 2>/dev/null || true)
if [ "${#candidates[@]}" -eq 0 ]; then
    echo "ERROR: no backup passphrase: set ENCRYPTION_PASS, or BACKUP_ENCRYPTION_PASSWORD in $env_file. Nothing was changed." >&2
    exit 4
fi
found=""
for c in "${candidates[@]}"; do
    if opens "$c"; then found=$c; break; fi
done
unset candidates c v _given
if [ -z "$found" ]; then
    echo "ERROR: $keys does not open with ENCRYPTION_PASS or with the current or any kept previous backup passphrase in $env_file (or it lacks a required file). Nothing was changed." >&2
    exit 4
fi
_pass=$found
unset found
echo "STEP keys-decrypt-ok"

# The box: both containers exist, and the volumes come from the server's own
# mounts (never from a name guess). Read before anything changes.
mount_of() {
    docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$1\"}}{{.Name}}{{end}}{{end}}" openbao 2>/dev/null
}
image=$(docker inspect -f '{{.Config.Image}}' openbao 2>/dev/null) || image=""
kvol=$(mount_of /openbao/keys) || kvol=""
cvol=$(mount_of /openbao/cred) || cvol=""
mvol=$(mount_of /openbao/mcp-cred) || mvol=""
if [ -z "$image" ] || [ -z "$kvol" ] || [ -z "$cvol" ] || [ -z "$mvol" ]; then
    echo "ERROR: the openbao container or its keys/cred/mcp-cred volumes were not found. Enable the openbao profile first (docker compose --profile openbao up -d). Nothing was changed." >&2
    exit 2
fi
docker inspect -f '{{.Name}}' openbao-keygen >/dev/null 2>&1 || {
    echo "ERROR: the openbao-keygen container was not found (docker compose --profile openbao up -d). Nothing was changed." >&2
    exit 2
}
# The profile, from the STACK's .env (never --env, which may hold passphrases
# only): compose leaves the containers behind when the profile is turned off.
case ",$(read_env_value "${RAZZFAZZ_ENV_FILE:-${STACK_ROOT}/.env}" COMPOSE_PROFILES | tr -d ' ')," in
    *,openbao,*) ;;
    *) echo "ERROR: the openbao profile is not on in ${RAZZFAZZ_ENV_FILE:-${STACK_ROOT}/.env} (COMPOSE_PROFILES). Enable it first. Nothing was changed." >&2
       exit 2 ;;
esac

# I1: --keys-only never replaces a seal key that is not the backup's. Read-only
# mounts; the hashes stay inside the helper, only its exit code comes back.
if [ "$keys_only" = true ]; then
    CHECK='new=$(tar xf - -O keys/seal.key | sha256sum | cut -d" " -f1)
[ -s /openbao/keys/seal.key ] || exit 0
old=$(sha256sum < /openbao/keys/seal.key | cut -d" " -f1)
[ "$old" = "$new" ] && exit 0
exit 20'
    crc=0
    decrypt | docker run --rm -i --network none --user 0:0 \
        -v "$kvol:/openbao/keys:ro" -v "$cvol:/openbao/cred:ro" -v "$mvol:/openbao/mcp-cred:ro" \
        --entrypoint sh "$image" -c "$CHECK" || crc=$?
    if [ "$crc" -eq 20 ]; then
        echo "ERROR: --keys-only refused: this box's seal.key is present and is NOT the one in $keys. It may be the only key that opens this box's data; replacing it would make that data unreadable. Use the backup taken on THIS install, or, to replace the whole store, run without --keys-only. Nothing was changed." >&2
        exit 2
    elif [ "$crc" -ne 0 ]; then
        echo "ERROR: --keys-only could not compare this box's seal.key with the backup's (helper exit $crc). Nothing was changed." >&2
        exit 2
    fi
fi

# From the first change on, every failure says how to finish (I2).
rerun="scripts/openbao-restore.sh $(printf '%q' "$dir") --keys-only"
if [ "$env_given" = true ]; then rerun="$rerun --env $(printf '%q' "$env_file")"; fi
if [ "$assume_yes" = true ]; then rerun="$rerun --yes"; fi
finish_hint=true   # false where the message already names the right next step
completed=false    # set at the very end: on_exit keys on it, never on $? alone
sig_rc=""
on_exit() {
    local rc=$?
    [ "${BASHPID:-$$}" = "$$" ] || return 0   # a subshell that caught the signal says nothing
    [ -z "$sig_rc" ] || rc=$sig_rc
    if [ "$completed" != true ] && [ "$finish_hint" = true ]; then
        echo "ERROR: the restore stopped after changing this box (exit $rc). Finish it with: $rerun" >&2
        echo "       (with the same ENCRYPTION_PASS in the environment if you used one)" >&2
    fi
    # N7: bash does not always carry a trap's `exit 129` through (a signal
    # that lands while the shell forks a pipeline leaves $? = 2); exiting
    # here, in the EXIT trap, sets the final status deterministically.
    [ -z "$sig_rc" ] || exit "$sig_rc"
}
# Armed AT the point of no return. A signal must reach on_exit as a failure:
# bash runs the EXIT trap of a signalled script with $? = 0, so the signal
# traps exit with 130/143/129 and record it, and on_exit keys on `completed`.
armed=false
arm() {
    [ "$armed" = true ] && return 0
    armed=true
    trap on_exit EXIT
    trap 'sig_rc=130; exit 130' INT
    trap 'sig_rc=143; exit 143' TERM
    trap 'sig_rc=129; exit 129' HUP
}

# 2. The snapshot, with this box's own management login. The snapshot travels
# on stdin; the token lives only inside the container's shell.
LOGIN='export BAO_ADDR=http://127.0.0.1:8200; BAO_TOKEN=$(bao write -field=token auth/approle/login role_id=@/openbao/cred/config.role_id secret_id=@/openbao/cred/config.secret_id) && [ -n "$BAO_TOKEN" ] && export BAO_TOKEN'
if [ "$keys_only" = false ]; then
    echo "STEP point-of-no-return"
    echo "The next step REPLACES this box's whole OpenBao store with the backup's snapshot. Every secret written here since that backup is lost." >&2
    if [ "$assume_yes" != true ]; then
        if [ ! -t 0 ]; then
            echo "ERROR: not confirmed: run it on a terminal, or pass --yes. Nothing was changed." >&2
            exit 2
        fi
        printf 'Type yes to replace it: ' >&2
        answer=""
        read -r answer || true
        if [ "$answer" != yes ]; then
            echo "ERROR: not confirmed. Nothing was changed." >&2
            exit 2
        fi
    fi
    arm
    if ! docker exec -i openbao sh -c "$LOGIN"' || exit 1; bao operator raft snapshot restore -force /dev/stdin' < "$snap"; then
        finish_hint=false
        echo "ERROR: the snapshot restore failed; the keys were not changed. If bao status no longer answers unsealed, the snapshot may be in: finish with $rerun. Likely causes: OpenBao not running or sealed (docker logs openbao), or a 403 because this store was bootstrapped before rzfz-config could restore snapshots (reset its volumes)." >&2
        exit 5
    fi
    echo "STEP snapshot-restored"
fi
arm

# 3. The keys: staged and checked in the helper first, then the three volumes'
# contents are REPLACED (a file the backup does not carry, such as this box's
# own svc-mcp-manager credentials, would not match the restored store).
docker stop openbao >/dev/null
echo "STEP server-stopped"
EXTRACT='set -eu
umask 077
st=$(mktemp -d)
trap '"'"'rm -rf "$st"'"'"' EXIT
tar xf - -C "$st"
for f in '"$REQUIRED"'; do
    [ -s "$st/$f" ] || { echo "the archive lacks $f" >&2; exit 3; }
done
# --keys-only: the same seal.key check as before the stop, in the step that
# writes, so nothing can change the key between the check and the delete.
if [ '"$keys_only"' = true ] && [ -s /openbao/keys/seal.key ]; then
    old=$(sha256sum < /openbao/keys/seal.key | cut -d" " -f1)
    new=$(sha256sum < "$st/keys/seal.key" | cut -d" " -f1)
    [ "$old" = "$new" ] || exit 20
fi
for d in keys cred mcp-cred; do
    find "/openbao/$d" -mindepth 1 -delete
    if [ -d "$st/$d" ]; then cp -a "$st/$d/." "/openbao/$d/"; fi
done
# #2472 B22: mcp-manager (uid 100, no capabilities) reads its AppRole files
# from this volume; whatever uid the archive carries, uid 100 owns them now
chown -R 100:1000 /openbao/mcp-cred
rm -rf "$st"'
# The plaintext is staged on a tmpfs in the helper (never on the host disk).
xrc=0
decrypt | docker run --rm -i --network none --user 0:0 --tmpfs /tmp:rw,mode=0700,size=4m \
        -v "$kvol:/openbao/keys" -v "$cvol:/openbao/cred" -v "$mvol:/openbao/mcp-cred" \
        --entrypoint sh "$image" -c "$EXTRACT" || xrc=$?
if [ "$xrc" -eq 20 ]; then
    finish_hint=false
    echo "ERROR: --keys-only refused: this box's seal.key changed after the first check and is NOT the one in $keys. Nothing was written. OpenBao is stopped now (it may not have been running before); to run it again with the box's own key: docker start openbao" >&2
    exit 2
elif [ "$xrc" -ne 0 ]; then
    echo "ERROR: the keys could not be put into the openbao-keys/openbao-cred volumes. OpenBao is stopped." >&2
    exit 7
fi
echo "STEP keys-restored"

# 4. keygen re-checks the pair (data present + key present), then the server.
if ! docker start -a openbao-keygen; then
    echo "ERROR: openbao-keygen refused the restored keys (docker logs openbao-keygen). OpenBao stays stopped." >&2
    exit 6
fi
docker start openbao >/dev/null
echo "STEP server-started"

tries=0
until docker exec openbao bao status -format=json 2>/dev/null | grep -q '"sealed"[[:space:]]*:[[:space:]]*false'; do
    tries=$((tries + 1))
    if [ "$tries" -ge "${OPENBAO_WAIT_TRIES:-60}" ]; then
        finish_hint=false
        echo "ERROR: OpenBao is still sealed: the restored seal key does not open the restored data (docker logs openbao)." >&2
        echo "       If it was only slow to start, finish with: $rerun" >&2
        echo "       If raft.snap and keys.tar.enc are not from the same backup, a re-run cannot help: restore ONE backup set with: scripts/openbao-restore.sh <that backup-dir> --yes" >&2
        exit 6
    fi
    sleep "${OPENBAO_WAIT_SECONDS:-2}"
done
echo "STEP unsealed"

# 5. The restored credentials belong to the restored store.
if ! docker exec openbao sh -c "$LOGIN"' || exit 1; bao token revoke -self >/dev/null'; then
    finish_hint=false
    echo "ERROR: OpenBao is unsealed, but the restored rzfz-config credentials do not log in: the keys archive and the snapshot do not come from one install. A --keys-only re-run cannot fix that. Restore ONE backup set (raft.snap and keys.tar.enc of the same backup) with: scripts/openbao-restore.sh <that backup-dir> --yes; admin access meanwhile: rzfz openbao admin-shell (only if this box's credentials are intact)." >&2
    exit 6
fi
echo "STEP credentials-ok"
completed=true
echo "OpenBao restored. Next: rzfz post-install --refresh (re-applies sign-in and policies; mints svc-mcp-manager's credentials if the backup had none)."
