#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# rzfz openbao <command> — OpenBao operator actions.
#
#   show-recovery-key   print OpenBao's recovery key ONCE, for your password
#                       manager or offline storage. An encrypted copy is in
#                       every backup (keys.tar.enc, sealed with the backup
#                       passphrase). A second call refuses. The key opens
#                       OpenBao's recovery mode to repair its storage; it
#                       does NOT give you root or admin access back.
#   admin-shell         break-glass admin access: a shell inside the openbao
#                       container, logged in with the rzfz-breakglass AppRole
#                       (manages auth, policies, identity; token valid 15
#                       minutes, 1 hour at most). Each shell mints its own
#                       one-login credential, marked admin-shell in the audit
#                       log. Nothing is printed.
#   confirm-purge       list the OpenBao users held above the per-run purge
#                       cap; purge them only after you type PURGE <n>.
#   purge-user <sub>    purge ONE user deleted in Authentik whose deletion
#                       the reconcile cannot confirm (rzfz status names
#                       them); only after you type PURGE <first 12 of sub>.
#
# The OpenBao CLI is used on this box: in admin-shell, or with docker exec.
# bao.<domain> serves the UI behind Authentik; a CLI login from outside
# (bao login -method=oidc) is not supported.
#
# #2476 (decision record D9 and its 2.6.3 note). The recovery key is read
# inside the openbao-keygen one-shot (no network, the keys volume). The
# admin shell's login runs inside the openbao container: the rzfz-config
# AppRole (read-only /openbao/cred mount) mints ONE break-glass secret_id
# (10 minutes, one login, metadata purpose=admin-shell), which reaches the
# login on stdin; the rzfz-config token is revoked before the shell starts.
# No credential or token touches argv, .env or the terminal.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
cd "${SCRIPT_DIR}"

usage() { sed -n '5,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# Runs INSIDE the container; tokens and the secret_id only ever live in that
# shell's variables. `@file` makes bao read the rzfz-config credentials from
# the files; `secret_id=-` reads the minted one from stdin.
ADMIN_LOGIN='fail() { echo "ERROR: $1 (docker logs openbao-bootstrap)" >&2; exit 7; }
CT=$(bao write -field=token auth/approle/login role_id=@/openbao/cred/config.role_id secret_id=@/openbao/cred/config.secret_id) || fail "the rzfz-config AppRole login failed"
RID=$(BAO_TOKEN=$CT bao read -field=role_id auth/approle/role/rzfz-breakglass/role-id) || fail "the rzfz-breakglass AppRole is missing"
SID=$(BAO_TOKEN=$CT bao write -field=secret_id auth/approle/role/rzfz-breakglass/secret-id metadata="{\"purpose\":\"admin-shell\",\"by\":\"${RZFZ_BY:-unknown}\"}") || fail "minting the break-glass credential failed"
BAO_TOKEN=$(printf "%s" "$SID" | bao write -field=token auth/approle/login role_id="$RID" secret_id=-) || fail "the break-glass login failed"
unset SID
BAO_TOKEN=$CT bao token revoke -self >/dev/null 2>&1 || echo "WARNING: the rzfz-config token that minted this shell could not be revoked; it expires within 15 minutes" >&2
unset CT RID
export BAO_TOKEN HISTFILE=/dev/null
echo "OpenBao admin shell (break-glass, 15 minutes). exit to leave." >&2
exec sh'

# Who runs a command, for the audit log (a login name only: anything else is
# dropped before it reaches the container).
operator_name() {
    local by
    by=$(printf '%s' "${SUDO_USER:-${USER:-unknown}}" | tr -cd 'A-Za-z0-9._-' | cut -c1-64)
    printf '%s' "${by:-unknown}"
}

case "${1:-}" in
    show-recovery-key)
        exec docker compose --profile openbao run --rm --no-deps -T openbao-keygen show-recovery ;;
    admin-shell)
        # Who opened it, for the audit log's secret_id metadata.
        by=$(operator_name)
        if [ -t 0 ] && [ -t 1 ]; then
            exec docker exec -it -e "RZFZ_BY=${by}" openbao sh -c "$ADMIN_LOGIN"
        fi
        exec docker exec -i -e "RZFZ_BY=${by}" openbao sh -c "$ADMIN_LOGIN" ;;
    confirm-purge)
        if [ -t 0 ] && [ -t 1 ]; then
            exec docker exec -it razzfazz-config python -m app.services.openbao_purge confirm
        fi
        exec docker exec -i razzfazz-config python -m app.services.openbao_purge confirm ;;
    purge-user)
        sub="${2:-}"
        case "$sub" in *"
"*) sub="" ;; esac   # grep -x below is per line
        if ! printf '%s' "$sub" | grep -Eqx '[A-Za-z0-9_-]{1,128}'; then
            echo "ERROR: purge-user needs the user's OpenBao id (the 64-character sub rzfz status names)" >&2
            exit 2
        fi
        # The operator's judgement replaces the pk confirmation here: a person
        # at a terminal types the word, and the audit log names them.
        if ! [ -t 0 ] || ! [ -t 1 ]; then
            echo "ERROR: purge-user asks for a typed confirmation; run it on a terminal" >&2
            exit 2
        fi
        exec docker exec -it -e "RZFZ_BY=$(operator_name)" razzfazz-config python -m app.services.openbao_purge purge-user "$sub" ;;
    -h|--help|"") usage; exit 0 ;;
    *) echo "ERROR: unknown command: $1" >&2; usage >&2; exit 2 ;;
esac
