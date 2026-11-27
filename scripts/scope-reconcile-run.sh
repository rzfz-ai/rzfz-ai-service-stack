#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scope-reconcile-run.sh — the hourly knowledge-scope and role reconcile
# =============================================================================
# Run every hour by razzfazz-scope-reconcile.timer (scripts/systemd/, installed
# by scripts/install-scope-reconcile-timer.sh), as the stack's owner.
#
# Dify and cognee enforce a knowledge scope only through the reconcile
# (`rzfz provision-sso-users --reconcile-roles --app all`): someone removed
# from a scope kept reading its datasets until the next upgrade or a manual
# run. This runs the same reconcile every hour, so a removal takes effect
# within the hour. Role changes and disabled accounts follow on the same run;
# the reconcile's own rules hold (it only undoes what it did itself, never
# removes the last admin, changes nothing when an input cannot be read).
#
#   * one run at a time: a run still going makes the next one skip (logged);
#   * bounded: the reconcile is stopped after RZFZ_SCOPE_RECONCILE_TIMEOUT
#     seconds (default 900); each dataset change is one database transaction,
#     so a stopped run leaves no half-applied change, and the next run goes on;
#   * a start line and a result line in the journal
#     (`journalctl -u razzfazz-scope-reconcile`); every change is printed by
#     the reconcile and written to the portal audit log (category identity,
#     user "hourly reconcile ...");
#   * the last result in .rzfz-state/scope-reconcile.last (at, rc, seconds).
#
# Exit: the reconcile's exit code (124 = stopped at the limit); 0 when there is
# nothing to do here (no .env: not a master) or the previous run still goes.
# Failure policy: set -e is on; a tolerated failure carries an explicit `|| ...`.
# =============================================================================
set -euo pipefail

STACK_DIR="${RZFZ_STACK_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}"
LIMIT="${RZFZ_SCOPE_RECONCILE_TIMEOUT:-900}"
STATE="${STACK_DIR}/.rzfz-state"
ACTOR="hourly reconcile (razzfazz-scope-reconcile.timer)"

log() { echo "rzfz-scope-reconcile: $*"; }

if [ ! -f "${STACK_DIR}/.env" ]; then
    log "no .env in ${STACK_DIR} (not a master): nothing to reconcile"
    exit 0
fi
case "$LIMIT" in ''|*[!0-9]*|0) LIMIT=900 ;; esac

mkdir -p "$STATE" || { log "cannot create ${STATE}: not run"; exit 1; }
exec 9>"${STATE}/scope-reconcile.lock" || { log "cannot open the lock file: not run"; exit 1; }
if ! flock -n 9; then
    log "the previous run is still going: this one is skipped"
    exit 0
fi

start=$(date +%s)
log "start (limit ${LIMIT}s)"
rc=0
# timeout runs the reconcile in its own process group and signals the whole
# group, so psql and docker clients it started stop with it.
RZFZ_RECONCILE_ACTOR="$ACTOR" timeout --kill-after=30 "$LIMIT" \
    bash "${STACK_DIR}/cli/provision-sso-users.sh" --reconcile-roles --app all </dev/null 9>&- || rc=$?
secs=$(( $(date +%s) - start ))
case "$rc" in
    0)       log "done in ${secs}s" ;;
    124|137) log "stopped after ${secs}s: over the ${LIMIT}s limit; the next run carries on" ;;
    *)       log "finished with errors (exit ${rc}) after ${secs}s: see the lines above" ;;
esac
{
    printf 'at=%s\nrc=%s\nseconds=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc" "$secs" > "${STATE}/scope-reconcile.last.tmp" \
        && mv -f "${STATE}/scope-reconcile.last.tmp" "${STATE}/scope-reconcile.last"
} || log "the result could not be written to ${STATE}/scope-reconcile.last"
exit "$rc"
