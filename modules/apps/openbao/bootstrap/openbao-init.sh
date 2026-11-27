#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# openbao-init.sh — the two one-shots around OpenBao's static seal (#2476, D9).
#
#   keys           openbao-keygen, BEFORE the server: volume ownership, the
#                  static seal key (generated once), and the refusals.
#   bootstrap      openbao-bootstrap, AFTER the server is up: operator init
#                  once, the AppRoles, then the root-token revoke.
#   show-recovery  print the recovery key ONCE for the operator (rzfz openbao
#                  show-recovery-key); a second call refuses.
#
# Key material lives only in $OPENBAO_KEYS_DIR (the openbao-keys volume), is
# never passed on argv, never printed (show-recovery aside) and never reaches
# .env. Refusals (exit 3/5, 4) are deliberate: an OpenBao that cannot open its
# data must stay down or sealed and loud, never be re-keyed or re-initialised
# (the #2471 Infisical lesson). Operator-facing messages carry no issue numbers.
#
# Exit codes — keys: 0 ok, 2 a volume is not mounted, 3 storage holds data but
# seal.key is missing, 5 storage is empty but seal.key exists. bootstrap: 0 ok,
# 4 initialised but still sealed, 7 a bootstrap step failed (the root token is
# kept for the next run), or the keys volume belongs to another install, or
# setup cannot complete (a first boot whose init answer was lost gets the reset
# steps; a store that ever completed setup gets the restore steps, never a
# volume removal), 10 OpenBao not answering. show-recovery: 0 shown, 2 no
# recovery key saved, 6 already shown.
#
# Crash- and race-safe (review round 1): the bootstrap and show-recovery hold
# an exclusive flock on $KEYS_DIR/.bootstrap.lock; `operator init`'s one answer
# is written with noclobber to a per-run temp file and hard-linked into place
# (never truncated, never overwritten); recovery.key is persisted before the
# root token is used; the root token file goes only after the AppRoles exist
# and the revoke succeeded. A re-run resumes from whatever a killed run left,
# and never exits 0 while the AppRoles or the recovery key are missing.
set -eu

DATA_DIR="${OPENBAO_DATA_DIR:-/openbao/data}"
KEYS_DIR="${OPENBAO_KEYS_DIR:-/openbao/keys}"
CRED_DIR="${OPENBAO_CRED_DIR:-/openbao/cred}"
OWNER="${OPENBAO_OWNER:?OPENBAO_OWNER (uid:gid of the openbao user) is required}"
KEY_FILE="$KEYS_DIR/seal.key"
RECOVERY_FILE="$KEYS_DIR/recovery.key"
ROOT_FILE="$KEYS_DIR/bootstrap-root.token"
INIT_FILE="$KEYS_DIR/init-output.json"
SHOWN_FILE="$KEYS_DIR/recovery.shown"
DONE_FILE="$KEYS_DIR/setup.done"
LOCK_FILE="$KEYS_DIR/.bootstrap.lock"
# The AppRole credentials the bootstrap writes (0400, the openbao uid).
# The policy files shared with the identity reconcile (one source).
POLICY_DIR="${OPENBAO_POLICY_DIR:-/bootstrap/policies}"
CRED_FILES="config.role_id config.secret_id backup.role_id backup.secret_id portal.role_id portal.secret_id"
export BAO_ADDR="${BAO_ADDR:-http://openbao:8200}"

field() { printf '%s\n' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([a-z]*\).*/\1/p" | head -n 1; }

keys() {
    [ -d "$DATA_DIR" ] && [ -d "$KEYS_DIR" ] || { echo "openbao-keygen: $DATA_DIR or $KEYS_DIR is not mounted" >&2; return 2; }
    for d in ${OPENBAO_OWNED_DIRS:-$DATA_DIR}; do
        chown "$OWNER" "$d" && chmod 0700 "$d" || { echo "openbao-keygen: cannot own $d" >&2; return 2; }
    done
    chmod 0755 "$KEYS_DIR"
    # An unreadable data dir must never read as "empty" (that path mints a key).
    listing=$(ls -A "$DATA_DIR") || { echo "openbao-keygen: cannot list $DATA_DIR" >&2; return 2; }
    if [ -n "$listing" ]; then
        if [ ! -s "$KEY_FILE" ]; then   # -s: a zero-byte key is no key
            echo "REFUSING: OpenBao storage holds data but $KEY_FILE is missing or empty. Restore the openbao-keys volume from the backup (databases/openbao/keys.tar.enc, scripts/openbao-restore.sh). A new key over this data would make it unreadable for good." >&2
            return 3
        fi
        return 0
    fi
    if [ -e "$KEY_FILE" ]; then
        # The key has sealed something only once the store was initialised
        # (recovery.key exists). A key from a first boot that never reached
        # the server opened nothing: keep it and go on.
        if [ -e "$RECOVERY_FILE" ] || [ -e "$SHOWN_FILE" ] || [ -e "$DONE_FILE" ]; then
            echo "REFUSING: $KEY_FILE exists but OpenBao storage is EMPTY: the data volume was lost or replaced. Restore it (rzfz backup restore, scripts/openbao-restore.sh); a new store is never initialised under an old key." >&2
            return 5
        fi
        [ -s "$KEY_FILE" ] || { echo "REFUSING: $KEY_FILE is empty. Remove it and start again (the store is empty, nothing is lost)." >&2; return 5; }
        return 0
    fi
    umask 077
    # Raw 32 random bytes, exactly as the 2.6.3 measurement generated it (A1
    # record D9/M2): not hex, not base64.
    rm -f "$KEY_FILE.tmp"
    head -c 32 /dev/urandom > "$KEY_FILE.tmp"
    chown "$OWNER" "$KEY_FILE.tmp" && chmod 0400 "$KEY_FILE.tmp" && mv "$KEY_FILE.tmp" "$KEY_FILE"
    echo "openbao-keygen: static seal key generated"
}

lock() {
    # One bootstrap (or show-recovery) at a time on this keys volume. flock is
    # released by the kernel when the holder dies, so a killed run never
    # leaves a stale lock. 0644 and empty: the backup reads the volume as uid 100.
    exec 9>>"$LOCK_FILE"
    flock 9
}

show_recovery() {
    # Decision record D9: the recovery key is shown ONCE, for the operator's
    # password manager or offline storage; the encrypted escrow in every backup
    # (keys.tar.enc, sealed with the backup passphrase) is the other copy.
    # Its role, as measured on 2.6.3 (D9 note, controller ruling on review I2):
    # it opens OpenBao's recovery mode (`bao server -recovery`) for storage
    # repair, nothing else. It does NOT regain root or admin access; that is
    # `rzfz openbao admin-shell` (the rzfz-breakglass AppRole, a one-login
    # secret_id minted per shell).
    lock
    if [ ! -s "$RECOVERY_FILE" ]; then
        if [ -e "$SHOWN_FILE" ] || [ -e "$DONE_FILE" ]; then
            echo "openbao: the recovery key file is missing from the keys volume, although OpenBao was set up. Restore the keys volume from the backup (databases/openbao/keys.tar.enc, scripts/openbao-restore.sh)." >&2
        else
            echo "openbao: no recovery key saved yet: OpenBao has not finished its first boot (docker logs openbao-bootstrap)" >&2
        fi
        return 2
    fi
    if [ -e "$SHOWN_FILE" ]; then
        echo "REFUSING: the OpenBao recovery key was already shown ($(cat "$SHOWN_FILE")). The encrypted copy is in every backup: databases/openbao/keys.tar.enc, sealed with the backup passphrase." >&2
        return 6
    fi
    printf 'RECOVERY-KEY %s\n' "$(cat "$RECOVERY_FILE")"
    echo "Store it in your password manager or offline NOW: it is not shown again. An encrypted copy travels in every backup (keys.tar.enc)."
    echo "What it is for: it opens OpenBao's recovery mode (bao server -recovery) to repair its storage. It does not give you root or admin access back; admin access is: rzfz openbao admin-shell."
    umask 077
    rm -f "$SHOWN_FILE.tmp"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$SHOWN_FILE.tmp"
    # Owned by the openbao uid like every escrowed file: the backup reads the
    # keys volume through the server (uid 100).
    chown "$OWNER" "$SHOWN_FILE.tmp" && chmod 0400 "$SHOWN_FILE.tmp" && mv "$SHOWN_FILE.tmp" "$SHOWN_FILE"
}

setup() {
    # Called as `if ! setup`: set -e is OFF in here, so every step stops explicitly.
    BAO_TOKEN=$(cat "$ROOT_FILE") || return 1
    [ -n "$BAO_TOKEN" ] || return 1
    export BAO_TOKEN
    # The audit device is declarative (openbao.hcl `audit "file" "file"`): 2.x
    # refuses API-created audit devices. Without it nothing below is audited.
    bao audit list 2>/dev/null | grep -q '^file/' || { echo "openbao-bootstrap: the file audit device from openbao.hcl is missing" >&2; return 1; }
    bao secrets enable -path=secret kv-v2 >/dev/null 2>&1 || bao secrets list 2>/dev/null | grep -q '^secret/' || return 1
    bao auth enable approle >/dev/null 2>&1 || bao auth list 2>/dev/null | grep -q '^approle/' || return 1
    # Technical users get their OWN AppRole mount. AppRole role names are
    # case-insensitive while ACL paths are not, so no deny glob can keep a
    # role on a shared mount (svc-MCP-manager is svc-mcp-manager): the
    # boundary is the mount. The stack's roles (rzfz-*, svc-mcp-manager,
    # svc-agent-manager) live on approle/, which the Portal cannot touch.
    bao auth enable -path=approle-apps approle >/dev/null 2>&1 || bao auth list 2>/dev/null | grep -q '^approle-apps/' || return 1
    # rzfz-config: the host's management role (`rzfz post-install --refresh`
    # reconciles OIDC, policies and identity with it) and the minter of the
    # break-glass secret_id. It can write any policy, so it is root-equivalent;
    # the Configuration Portal never uses it (rzfz-portal below). It also
    # restores a backup's raft snapshot over this store
    # (scripts/openbao-restore.sh; update on snapshot-force, measured on 2.6.3
    # to need no sudo). The policy is one file shared with the refresh.
    bao policy write rzfz-config - >/dev/null < "$POLICY_DIR/rzfz-config.hcl" || return 1
    printf '%s\n' 'path "sys/storage/raft/snapshot" { capabilities = ["read"] }' \
        | bao policy write rzfz-backup - >/dev/null || return 1
    # rzfz-app: the ONE policy of every technical user's AppRole (on the
    # approle-apps/ mount). Templated on the alias's role_name (set by OpenBao
    # from the role, never from a secret_id), so svc-x reads secret/apps/svc-x/
    # only and the Portal never has to write a policy.
    acc=$(bao read -field=accessor sys/auth/approle-apps) && [ -n "$acc" ] || return 1
    # #2472 B22: never the raw OAuth token the MCP manager keeps under oauth/
    printf '%s\n%s\n' "path \"secret/data/apps/{{identity.entity.aliases.$acc.metadata.role_name}}/*\" { capabilities = [\"read\"] }" \
        "path \"secret/data/apps/{{identity.entity.aliases.$acc.metadata.role_name}}/oauth/*\" { capabilities = [\"deny\"] }" \
        | bao policy write rzfz-app - >/dev/null || return 1
    # rzfz-portal: the Configuration Portal's everyday role. Only what its
    # features need: the technical users' AppRoles, on their own mount
    # approle-apps/ and nowhere else (every role there carries the policy
    # rzfz-app, the fixed limits and Caddy's fixed address, all REQUIRED on
    # every write, so a role can never be recreated looser), and the
    # deleted-user purge (KV users/ and purged/, the entity, the token
    # accessors). Nothing on approle/, where the stack's roles are; no
    # sys/auth, no sys/policies: it cannot widen anyone's access.
    bao policy write rzfz-portal - >/dev/null < "$POLICY_DIR/rzfz-portal.hcl" || return 1
    for role in config backup portal; do
        bao write "auth/approle/role/rzfz-$role" token_policies="rzfz-$role" token_ttl=15m token_max_ttl=1h >/dev/null || return 1
        for f in role_id secret_id; do
            # a killed run may have left this temp file 0400: never re-open it
            rm -f "$CRED_DIR/$role.$f.tmp" || return 1
            if [ "$f" = role_id ]; then
                bao read -field=role_id "auth/approle/role/rzfz-$role/role-id" > "$CRED_DIR/$role.$f.tmp" || return 1
            else
                bao write -f -field=secret_id "auth/approle/role/rzfz-$role/secret-id" > "$CRED_DIR/$role.$f.tmp" || return 1
            fi
            [ -s "$CRED_DIR/$role.$f.tmp" ] || return 1
            chown "$OWNER" "$CRED_DIR/$role.$f.tmp" && chmod 0400 "$CRED_DIR/$role.$f.tmp" || return 1
            mv -f "$CRED_DIR/$role.$f.tmp" "$CRED_DIR/$role.$f" || return 1
        done
    done
    # rzfz-breakglass: `rzfz openbao admin-shell`. The rzfz-config policy, but
    # no stored secret_id: each shell mints one (rzfz-config), valid 10
    # minutes for ONE login, with metadata purpose=admin-shell, so the audit
    # log tells a break-glass session apart from every other caller.
    bao write auth/approle/role/rzfz-breakglass token_policies=rzfz-config secret_id_ttl=10m \
        secret_id_num_uses=1 token_ttl=15m token_max_ttl=1h >/dev/null || return 1
}

complete() {
    # Setup is done when the recovery key and all the AppRole files exist.
    [ -s "$RECOVERY_FILE" ] || return 1
    for f in $CRED_FILES; do
        [ -s "$CRED_DIR/$f" ] || return 1
    done
}

answers() {
    # Every saved init answer, the linked one first (a killed run may have
    # left only its per-run temp file).
    for f in "$INIT_FILE" "$INIT_FILE".*; do
        [ -s "$f" ] && printf '%s\n' "$f"
    done
    return 0
}

persist_answer() {
    # $1: a saved `operator init` answer. recovery.key FIRST (before the root
    # token is ever used), then the root token; each via a temp file + rename.
    # The answer files go only once both are on disk.
    rk=$(awk '/"recovery_keys_b64"/{f=1;next} f&&/"/{gsub(/[ ",]/,"");printf "%s",$0;exit}' "$1")
    rt=$(sed -n 's/.*"root_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -n 1)
    [ -n "$rk" ] && [ -n "$rt" ] || return 2
    umask 077
    if [ ! -s "$RECOVERY_FILE" ]; then
        rm -f "$RECOVERY_FILE.tmp" || return 1
        printf '%s' "$rk" > "$RECOVERY_FILE.tmp" || return 1
        # 0400 owned by the openbao uid: the backup escrows the keys volume
        # by reading it through the server, which runs as that uid.
        chown "$OWNER" "$RECOVERY_FILE.tmp" && chmod 0400 "$RECOVERY_FILE.tmp" || return 1
        mv -f "$RECOVERY_FILE.tmp" "$RECOVERY_FILE" || return 1
    fi
    if [ ! -s "$ROOT_FILE" ]; then
        rm -f "$ROOT_FILE.tmp" || return 1
        printf '%s' "$rt" > "$ROOT_FILE.tmp" && chmod 0400 "$ROOT_FILE.tmp" || return 1
        mv -f "$ROOT_FILE.tmp" "$ROOT_FILE" || return 1
    fi
    sync 2>/dev/null || true
    for f in $(answers); do rm -f "$f"; done
    return 0
}

initialise() {
    # `operator init` answers exactly once. Noclobber into a per-run temp
    # file, then a hard link into place: a racing run can neither truncate nor
    # replace an answer, and a killed run leaves a complete temp file behind.
    tmp="$INIT_FILE.$$"
    rm -f "$tmp"
    ( umask 077; set -C; bao operator init -recovery-shares=1 -recovery-threshold=1 -format=json > "$tmp" ) || {
        [ -s "$tmp" ] || rm -f "$tmp"
        return 1
    }
    chmod 0400 "$tmp"
    ln "$tmp" "$INIT_FILE" || return 1
    rm -f "$tmp"
    persist_answer "$INIT_FILE"
}

ever_set_up() {
    # Evidence that this store completed its setup at least once: then it may
    # hold secrets, and nothing may ever suggest removing a volume.
    [ -e "$RECOVERY_FILE" ] || [ -e "$SHOWN_FILE" ] || [ -e "$DONE_FILE" ] && return 0
    for f in $CRED_FILES; do
        [ -e "$CRED_DIR/$f" ] && return 0
    done
    return 1
}

missing_files() {
    m=""
    [ -s "$RECOVERY_FILE" ] || m="$m recovery.key"
    for f in $CRED_FILES; do
        [ -s "$CRED_DIR/$f" ] || m="$m $f"
    done
    printf '%s' "${m# }"
}

refuse_lost() {
    if ever_set_up; then
        # A store that was set up and lost files from its keys or cred volume.
        # It may hold every secret of this box: restore, never reset.
        echo "ERROR: OpenBao is initialised and was set up before, but files are missing: $(missing_files) ($1). Its data is intact; do NOT remove or reset any OpenBao volume. Restore the keys and credentials from the latest backup: rzfz backup restore, then scripts/openbao-restore.sh <backup-dir> (it restores databases/openbao/keys.tar.enc into the openbao-keys and openbao-cred volumes). Then start the profile again." >&2
        return 7
    fi
    # A first boot whose one init answer never reached disk (killed during
    # operator init): no recovery key, no AppRole, no setup marker ever
    # existed, and the root token was held by nobody, so nothing can have
    # been stored in it.
    echo "ERROR: OpenBao is initialised, but its first boot never finished and its init answer is gone: $1. Nothing can have been stored in it (no AppRole and no recovery key ever existed), so reset it; no data is lost. From the stack directory: docker compose --profile openbao stop openbao; docker compose --profile openbao rm -f openbao openbao-keygen openbao-bootstrap; then remove the three volumes that end in _openbao-data, _openbao-keys and _openbao-cred (docker volume ls | grep openbao; docker volume rm <them>); then docker compose --profile openbao up -d." >&2
    return 7
}

mark_done() {
    [ -e "$DONE_FILE" ] && return 0
    rm -f "$DONE_FILE.tmp"
    ( umask 077; date -u +%Y-%m-%dT%H:%M:%SZ > "$DONE_FILE.tmp" ) || return 1
    # owned by the openbao uid: the backup reads the keys volume as that uid
    chown "$OWNER" "$DONE_FILE.tmp" && chmod 0400 "$DONE_FILE.tmp" && mv -f "$DONE_FILE.tmp" "$DONE_FILE"
}

bootstrap() {
    lock
    tries=0
    while :; do
        rc=0
        st=$(bao status -format=json 2>/dev/null) || rc=$?
        if [ "$rc" -ne 1 ] && [ -n "$st" ]; then break; fi
        tries=$((tries + 1))
        if [ "$tries" -ge "${OPENBAO_WAIT_TRIES:-30}" ]; then
            echo "openbao-bootstrap: OpenBao not answering at $BAO_ADDR (rc=$rc)" >&2
            return 10
        fi
        sleep "${OPENBAO_WAIT_SECONDS:-2}"
    done
    init=$(field "$st" initialized)
    sealed=$(field "$st" sealed)
    if [ "$init" = false ]; then
        if [ -n "$(answers)" ] || [ -e "$ROOT_FILE" ] || [ -e "$RECOVERY_FILE" ]; then
            echo "REFUSING: OpenBao is not initialised, but $KEYS_DIR already holds an init answer, a root token or a recovery key: this keys volume belongs to another install. Nothing was initialised. Restore the matching openbao-data volume, or reset both volumes." >&2
            return 7
        fi
        irc=0
        initialise || irc=$?
        if [ "$irc" -eq 2 ]; then
            echo "ERROR: OpenBao was initialised, but its answer holds no readable recovery key or root token. It is kept, root-only, in $INIT_FILE; nothing was revoked. Read it before anything else touches this volume." >&2
            return 7
        elif [ "$irc" -ne 0 ]; then
            echo "ERROR: OpenBao initialisation failed; nothing was revoked. docker logs openbao" >&2
            return 7
        fi
        echo "openbao-bootstrap: initialised. Show the recovery key once and store it: rzfz openbao show-recovery-key"
        st=$(bao status -format=json 2>/dev/null) || true
        sealed=$(field "$st" sealed)
    fi
    if [ "$sealed" != false ]; then
        echo "ERROR: OpenBao storage is initialised but still sealed: $KEY_FILE does not open it (the keys volume and the data volume come from different installs). Restore the matching openbao-keys volume; never re-initialise." >&2
        return 4
    fi
    # Resume what a killed run left: a saved answer not yet persisted.
    for a in $(answers); do
        prc=0
        persist_answer "$a" || prc=$?
        if [ "$prc" -eq 0 ]; then
            echo "openbao-bootstrap: resumed from the saved init answer"
            break
        elif [ "$prc" -eq 2 ]; then
            echo "ERROR: the saved init answer $a holds no readable recovery key or root token; it is kept, root-only. Nothing was revoked." >&2
        else
            echo "ERROR: the saved init answer $a is readable, but writing the recovery key or root token from it failed; it is kept, root-only, and the next run retries. Nothing was revoked." >&2
        fi
        return 7
    done
    if [ -e "$ROOT_FILE" ]; then
        BAO_TOKEN=$(cat "$ROOT_FILE") || return 7
        export BAO_TOKEN
        lrc=0
        lout=$(bao token lookup 2>&1 >/dev/null) || lrc=$?
        if [ "$lrc" -ne 0 ]; then
            case "$lout" in
                *"permission denied"*|*"bad token"*|*"invalid token"*)
                    # The token is dead: a previous run revoked it and was killed
                    # before removing the file (or someone revoked it).
                    unset BAO_TOKEN
                    if complete; then
                        mark_done || return 7
                        rm -f "$ROOT_FILE"
                        return 0
                    fi
                    refuse_lost "the saved root token no longer works"
                    return 7 ;;
                *)
                    echo "ERROR: cannot check the saved root token (OpenBao answered: $(printf '%s\n' "$lout" | head -n 1)); nothing was changed, the next run retries." >&2
                    return 7 ;;
            esac
        fi
        if ! setup; then
            echo "ERROR: an OpenBao bootstrap step failed. The root token stays in $ROOT_FILE for the next run; nothing was revoked." >&2
            return 7
        fi
        complete || { echo "ERROR: the AppRole files are incomplete after setup; the root token is kept for the next run." >&2; return 7; }
        # durable before the revoke: after a power loss the AppRole files must
        # not be the thing that is missing while the revoke already happened
        sync 2>/dev/null || true
        bao token revoke -self >/dev/null || { echo "ERROR: the root token revoke failed; it is kept for the next run." >&2; return 7; }
        mark_done || { echo "ERROR: cannot write $DONE_FILE" >&2; return 7; }
        unset BAO_TOKEN
        rm -f "$ROOT_FILE"
    fi
    if ! complete; then
        refuse_lost "no saved init answer and no root token"
        return 7
    fi
    mark_done || { echo "ERROR: cannot write $DONE_FILE" >&2; return 7; }
    return 0
}

case "${1:-}" in
    keys) keys ;;
    bootstrap) bootstrap ;;
    show-recovery) show_recovery ;;
    *) echo "usage: $0 keys|bootstrap|show-recovery" >&2; exit 2 ;;
esac
