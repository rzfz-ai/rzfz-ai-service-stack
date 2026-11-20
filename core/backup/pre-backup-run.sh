#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# pre-backup-run.sh — run pre-backup.sh and KEEP what it says (#2549)
# ==============================================================================
# offen/docker-volume-backup runs the archive-pre label through `docker exec`
# and keeps the command's stdout to itself: not one hook line reached
# `rzfz backup backup` or `docker logs backup-service` (measured on a test box, 2026.09-ga.4).
# This runner tees the hook into $LOG_DIR/pre-backup.log, copies the log to the
# container's own stdout (PID 1, what `docker logs` shows, also for the nightly
# cron run) and writes $LOG_DIR/pre-backup.status for the razzfazz-backup
# wrapper and `rzfz backup status`. Its exit code is the hook's.
# (BusyBox sh: POSIX only, no pipefail needed — the rc travels via a file.)
#
# The heartbeat: offen reads the exec's output stream until EOF and then starts
# the archive. The stream runs through the docker socket proxy, whose HAProxy
# closes a connection after `timeout client`/`timeout server` (10m) with no
# byte in either direction, and that close is a clean EOF: offen takes it as
# "the hook finished" and starts tarring /backup while the hook is still
# writing (#2584 — a production box, 2026-09-30: a 64 GB agent tar grew under offen's tar
# and the whole nightly failed with "archive/tar: write too long"). The hook is
# silent for as long as one agent volume takes to tar. So while the hook runs,
# a line goes to the stream (stderr, the one offen attaches) every HEARTBEAT_S
# seconds; the stream then ends when the hook ends, and offen's archive step
# starts only after it.
# ==============================================================================
set -eu

HOOK="${RZFZ_PRE_BACKUP_HOOK:-/scripts/pre-backup.sh}"
LOG_DIR="${RZFZ_PRE_BACKUP_LOG_DIR:-/backup/databases}"
CONTAINER_STDOUT="${RZFZ_CONTAINER_STDOUT:-/proc/1/fd/1}"
LOG="$LOG_DIR/pre-backup.log"
STATUS="$LOG_DIR/pre-backup.status"
# #2584: LOG_DIR is inside the backup source, and the log grows for as long as
# the hook runs. Everything this runner writes goes to the staging dir the
# archive excludes (see pre-backup.sh) and is moved into place when complete.
# `runner/` is the one entry pre-backup.sh leaves alone when it clears staging.
RUN_STAGE="$LOG_DIR/.staging/runner"
LOG_PART="$RUN_STAGE/pre-backup.log"
RC_FILE="$RUN_STAGE/pre-backup.rc"
# Printed, never a reason for DEGRADED: configuration advice, not content.
ADVISORY='NOTIFICATION_URLS is not configured|root disk [0-9]+% used'

# Well inside the proxy's 10 minutes; whole seconds only.
HEARTBEAT_S="${RZFZ_PRE_BACKUP_HEARTBEAT_S:-60}"
case "$HEARTBEAT_S" in ''|*[!0-9]*|0) HEARTBEAT_S=60 ;; esac

# The hook's own lock, held for as long as the hook runs. offen's run lock
# says nothing about it: when the exec stream is cut, offen stops waiting and
# may finish (lock released) while the hook is still running. `rzfz status`
# probes this lock (cli/lib-backup-run.sh) to say which it is. Only this
# runner and the pipeline that waits for the hook hold it; the hook itself,
# tee and the heartbeat get the descriptor closed, so nothing the hook leaves
# behind keeps it. Without a flock command the run is unchanged and the probe
# reads "unknown".
HOOK_LOCK="${RZFZ_PRE_BACKUP_LOCK:-/var/lock/rzfz-pre-backup.lock}"
# (`command exec`: a failed redirection must not end a POSIX shell.)
if command -v flock >/dev/null 2>&1 && mkdir -p "${HOOK_LOCK%/*}" 2>/dev/null \
    && { command exec 9>"$HOOK_LOCK"; } 2>/dev/null; then
    flock -n 9 || true
fi

if [ -L "$LOG_DIR/.staging" ]; then rm -f "$LOG_DIR/.staging"; fi
mkdir -p "$LOG_DIR" "$RUN_STAGE"
# #2759: the staging dir holds unencrypted work in progress: owner-only
chmod 0700 "$LOG_DIR/.staging" 2>/dev/null || true
rm -f "$LOG" "$RUN_STAGE"/*
started=$(date +%s)

# #2584: the run is on record BEFORE the hook starts. A run that never reaches
# its last line (container restarted, hook killed) leaves this marker — no
# finished_epoch — instead of no report at all, and `rzfz status` names it.
{
    echo "rc=running"
    echo "started_epoch=$started"
} > "$RUN_STAGE/pre-backup.status"
mv -f "$RUN_STAGE/pre-backup.status" "$STATUS"

# To the exec stream only, never into the log — and on STDERR: offen creates
# the exec with AttachStdin + AttachStderr and no AttachStdout
# (cmd/backup/exec.go), and dockerd copies only attached streams to the
# connection (daemon/exec.go: `if ec.OpenStdout`). Everything the hook prints
# goes to stdout and never reaches the proxy; this line is the only byte on the
# wire. The sleep holds no copy of stdout/stderr, so killing the loop leaves
# nothing of it on the stream.
# #3199: a scheduled backup that starts while `rzfz upgrade` runs waits for it.
# The upgrade replaces containers; a backup that had stopped one of them could
# not restart it ("No such container") and threw that night's finished archive
# away. The upgrade leaves a marker in this container's /tmp (a volume, so it
# survives the upgrade recreating backup-service) and removes it when it ends,
# also on failure. This runs before pre-backup.sh, so before anything is dumped
# or stopped. A backup asked for now (the upgrade's own pre-upgrade backup among
# them) does not wait; a marker older than 6 h is left over from an upgrade that
# could not clean up, and is removed.
UPGRADE_MARK="${RZFZ_UPGRADE_MARK:-/tmp/rzfz-upgrade-in-progress}"
UPGRADE_ON_DEMAND_MARK="${RZFZ_BACKUP_ON_DEMAND_MARK:-/tmp/rzfz-backup-on-demand}"
UPGRADE_WAIT_MAX_S="${RZFZ_UPGRADE_WAIT_MAX_S:-3600}"
UPGRADE_WAIT_POLL_S="${RZFZ_UPGRADE_WAIT_POLL_S:-15}"
UPGRADE_MARK_STALE_S=21600

_fresh_epoch_file() {    # $1 = file; true when it holds an epoch younger than 6 h
    [ -f "$1" ] || return 1
    _s=$(cat "$1" 2>/dev/null)
    case "$_s" in ''|*[!0-9]*) return 1 ;; esac
    [ $(( $(date +%s) - _s )) -lt "$UPGRADE_MARK_STALE_S" ]
}

wait_for_upgrade() {
    [ -f "$UPGRADE_MARK" ] || return 0
    if _fresh_epoch_file "$UPGRADE_ON_DEMAND_MARK"; then
        echo "An upgrade is running; this backup was asked for now and does not wait for it."
        return 0
    fi
    if ! _fresh_epoch_file "$UPGRADE_MARK"; then
        echo "WARNING: an upgrade marker more than 6 h old is left over from an upgrade that could not clean up; it is removed and the backup goes ahead."
        rm -f "$UPGRADE_MARK"
        return 0
    fi
    _since=$(date -d "@$(cat "$UPGRADE_MARK")" '+%H:%M' 2>/dev/null || echo "?")
    echo "An upgrade is running (since ${_since}); the scheduled backup waits for it to finish, at most ${UPGRADE_WAIT_MAX_S}s, before it stops any container."
    _waited=0
    while [ -f "$UPGRADE_MARK" ] && [ "$_waited" -lt "$UPGRADE_WAIT_MAX_S" ]; do
        sleep "$UPGRADE_WAIT_POLL_S"
        _waited=$((_waited + UPGRADE_WAIT_POLL_S))
    done
    if [ -f "$UPGRADE_MARK" ]; then
        echo "WARNING: the upgrade is still running after ${_waited}s; the backup goes ahead and may meet containers the upgrade replaces."
    else
        echo "The upgrade finished after ${_waited}s of waiting; the backup goes ahead."
    fi
}

runner_pid=$$
(
    waited=0
    while kill -0 "$runner_pid" 2>/dev/null; do
        sleep "$HEARTBEAT_S" </dev/null >/dev/null 2>&1
        waited=$((waited + HEARTBEAT_S))
        echo "pre-backup: still running (${waited}s)" >&2 || exit 0
    done
) 9>&- &
heartbeat_pid=$!

{ rc=0; wait_for_upgrade; sh "$HOOK" 9>&- || rc=$?; echo "$rc" > "$RC_FILE"; } 2>&1 | tee "$LOG_PART" 9>&-
kill "$heartbeat_pid" 2>/dev/null || true
wait "$heartbeat_pid" 2>/dev/null || true
rc=$(cat "$RC_FILE" 2>/dev/null || echo 1)
rm -f "$RC_FILE"

# #2584: did offen's archive step begin while this hook was still running?
# offen hands the hook the path its archive will be written to
# (COMMAND_RUNTIME_ARCHIVE_FILEPATH, cmd/backup/exec.go) and creates that file
# when compression starts, right after its walk of /backup (archive.go);
# encryption adds `<path>.gpg`; both stay until the run ends. Either one
# existing now means the walk already happened — whatever the hook published
# after it is not in that archive. This is the archive's START, measured here;
# the archive's mtime in backups/ is only its end.
archive_began_early=0
ARCHIVE_TMP="${COMMAND_RUNTIME_ARCHIVE_FILEPATH:-}"
if [ -n "$ARCHIVE_TMP" ] && { [ -e "$ARCHIVE_TMP" ] || [ -e "$ARCHIVE_TMP.gpg" ]; }; then
    archive_began_early=1
    echo "ERROR: the archive step started before this pre-backup step finished — the archive of this run may lack files written after that. Run a backup again." >> "$LOG_PART"
fi
mv -f "$LOG_PART" "$LOG"

if [ -w "$CONTAINER_STDOUT" ]; then
    sed 's/^/pre-backup: /' "$LOG" > "$CONTAINER_STDOUT" 2>/dev/null || true
fi

warnings=$(grep -E '(^|[[:space:]])(WARNING|SKIP):' "$LOG" | grep -cvE "$ADVISORY" || true)
errors=$(grep -cE '(^|[[:space:]])(ERROR|CRITICAL):' "$LOG" || true)
agent_failures=$(sed -n 's/.*Per-user agents: .* \([0-9][0-9]*\) failure(s)\..*/\1/p' "$LOG" | tail -n 1)
# A volume above BACKUP_AGENT_VOL_MAX_MB is skipped by EVERY nightly, not once:
# the wrapper makes that PARTIAL (exit 3). pre-backup.sh's own cap line:
#   SKIP: <vol> (allocated <N> MiB on disk (du -sm) > <cap> MiB cap — …
caps=$(sed -n 's/^ *SKIP: \([^ ]*\) (allocated \([0-9][0-9]*\) MiB on disk (du -sm) > \([0-9][0-9]*\) MiB cap.*/\1:\2:\3/p' "$LOG")
skipped=$(printf '%s\n' "$caps" | sed -n 's/^\([^:]*:[0-9]*\):[0-9]*$/\1/p' | paste -sd, -)
vol_cap=$(printf '%s\n' "$caps" | sed -n 's/^.*:\([0-9]*\)$/\1/p' | tail -n 1)
# #2709: every skipped agent volume with its reason, from the hook's SKIP-REASON
# lines: <vol>:<reason>:<size_mb or ->:<cap_mb or ->, comma separated.
skipped_detail=$(sed -n 's/^ *SKIP-REASON: vol=\([^ ]*\) reason=\([a-z_]*\) size_mb=\([0-9-]*\) cap_mb=\([0-9-]*\) .*/\1:\2:\3:\4/p' "$LOG" | paste -sd, -)
{
    echo "rc=$rc"
    echo "warnings=${warnings:-0}"
    echo "errors=${errors:-0}"
    echo "agent_failures=${agent_failures:-0}"
    echo "skipped_volumes=${skipped}"
    echo "vol_cap_mb=${vol_cap}"
    echo "skipped_detail=${skipped_detail}"
    echo "archive_began_early=$archive_began_early"
    echo "started_epoch=$started"
    echo "finished_epoch=$(date +%s)"
} > "$RUN_STAGE/pre-backup.status"
mv -f "$RUN_STAGE/pre-backup.status" "$STATUS"
exit "$rc"
