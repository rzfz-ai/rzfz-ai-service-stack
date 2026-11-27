#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# run-outcome.sh — every backup run leaves one audit event (#3458)
# ==============================================================================
# Measured on a production box: the nightly failed two nights running
# ("archive/tar: write too long", then no space left while encrypting) and the
# only trace was one ERROR line in `docker logs backup-service`; no
# notification URL was set. The archive guard can also refuse a run for space,
# and that refusal repeats every night until someone frees space.
#
# So every run writes ONE line, in the JSON Lines schema of the stack's
# AuditLogger (core/common/razzfazz_common/audit_log.py), to
#   $RZFZ_BACKUP_AUDIT_LOG   (default /archive/.rzfz-audit/backup-audit.log,
#                             i.e. backups/.rzfz-audit/ on the host)
# The Wazuh agent installer ships that file with the `rzfz.audit` label
# (scripts/install-wazuh-agent.sh), and rzfz_audit_rules.xml raises:
#   event backup.run, action run.refused / run.failed  -> alert, level 10
#   event backup.run, action run.degraded / run.partial -> level 6
#   event backup.run, action run.ok                     -> the base rule (3)
# Nothing has to be configured for it: no notification URL, no mail relay.
#
# Modes, all inside backup-service (BusyBox sh, POSIX only):
#
#   hook <rc> <started_epoch>
#       Called by pre-backup-run.sh once the pre-backup step has ended and its
#       report (pre-backup.status) is written. rc != 0 means offen aborts the
#       run here, so the outcome is known now: REFUSED when the archive guard
#       stopped it for space, FAILED otherwise. rc = 0: offen goes on to
#       archive, encrypt and copy, and a failure there runs no hook of ours
#       (offen v2.50.0 runs no -post label after an error). So a watcher is
#       started, detached from the exec stream, that waits for offen's run
#       lock and then judges the run by what it left (watch below).
#   watch <started_epoch>
#       Waits while offen holds /var/lock/dockervolumebackup.lock (it holds
#       it for the whole run), then: an archive written since the run started
#       that carries its completion mark (archive-guard.sh mark-complete) is
#       the run's archive -> OK, or DEGRADED / PARTIAL by the pre-backup
#       report (the razzfazz-backup wrapper's rules); an archive without the
#       mark -> FAILED (it was cut off, or began before the pre-backup step
#       ended); no archive -> FAILED.
#   record <outcome> <action> <target> <detail> [<error>]
#       The writer; the two modes above use it.
#
# Every event is also one "backup-run: ..." line in `docker logs
# backup-service`. The texts are this script's own and the archive guard's
# and pre-backup step's lines: no secret reaches them. They are cut to plain
# printable ASCII and 512 bytes, so every line stays valid JSON.
# ==============================================================================
set -u

AUDIT_LOG="${RZFZ_BACKUP_AUDIT_LOG:-/archive/.rzfz-audit/backup-audit.log}"
ARCHIVE_DIR="${RZFZ_ARCHIVE_DIR:-/archive}"
MARK_DIR="${RZFZ_COMPLETE_DIR:-$ARCHIVE_DIR/.rzfz-complete}"
PREFIX="${BACKUP_PRUNING_PREFIX:-backup-}"
[ -n "$PREFIX" ] || PREFIX="backup-"
LOG_DIR="${RZFZ_PRE_BACKUP_LOG_DIR:-/backup/databases}"
STATUS="$LOG_DIR/pre-backup.status"
LOG="$LOG_DIR/pre-backup.log"
RUN_LOCK="${RZFZ_BACKUP_RUN_LOCK:-/var/lock/dockervolumebackup.lock}"
CONTAINER_STDOUT="${RZFZ_CONTAINER_STDOUT:-/proc/1/fd/1}"
POLL_S="${RZFZ_OUTCOME_POLL_S:-30}"
WAIT_MAX_S="${RZFZ_OUTCOME_WAIT_MAX_S:-82800}"
case "$POLL_S" in ''|*[!0-9]*|0) POLL_S=30 ;; esac
case "$WAIT_MAX_S" in ''|*[!0-9]*) WAIT_MAX_S=82800 ;; esac
# Rotated by rename at this size (one line per run: years of nights).
ROTATE_BYTES=1048576
BACKUP_LOG_HINT="backup log: docker logs backup-service, and docker exec backup-service cat /backup/databases/pre-backup.log"

say() {
    echo "backup-run: $*"
    if [ -w "$CONTAINER_STDOUT" ]; then
        echo "backup-run: $*" >> "$CONTAINER_STDOUT" 2>/dev/null || true
    fi
}

# A JSON string body: printable ASCII only (tabs and line breaks become a
# space, the em dash a hyphen, any other byte "?"), at most 512 bytes,
# backslash and quote escaped.
json_str() {
    printf '%s' "$1" | sed 's/—/-/g' | LC_ALL=C tr '\t\r\n' '   ' | LC_ALL=C tr -c ' -~' '?' \
        | head -c 512 | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

when() { date -u -d "@$1" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || echo "epoch $1"; }

# record <outcome> <action> <target> <detail> [<error>]
cmd_record() {
    _outcome=$1; _action=$2; _target=$3; _detail=$4; _error=${5:-}
    case "$_outcome" in
        success)  _level=INFO ;;
        degraded) _level=WARNING ;;
        *)        _level=ERROR ;;
    esac
    _line="{\"timestamp\": \"$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')\", \"level\": \"$_level\", \"event\": \"backup.run\", \"category\": \"backup\", \"action\": \"$(json_str "$_action")\", \"target\": \"$(json_str "$_target")\", \"detail\": \"$(json_str "$_detail")\", \"outcome\": \"$(json_str "$_outcome")\""
    [ -n "$_error" ] && _line="$_line, \"error\": \"$(json_str "$_error")\""
    _line="$_line}"
    _dir=${AUDIT_LOG%/*}
    if mkdir -p "$_dir" 2>/dev/null; then
        chmod 0750 "$_dir" 2>/dev/null || true
        _size=0
        [ -f "$AUDIT_LOG" ] && _size=$(wc -c < "$AUDIT_LOG")
        if [ "${_size:-0}" -ge "$ROTATE_BYTES" ] 2>/dev/null; then
            mv -f "$AUDIT_LOG" "$AUDIT_LOG.1" 2>/dev/null || true
        fi
        if printf '%s\n' "$_line" >> "$AUDIT_LOG" 2>/dev/null; then
            chmod 0640 "$AUDIT_LOG" 2>/dev/null || true
        else
            say "WARNING: could not write the backup audit log $AUDIT_LOG (disk full?)"
        fi
    else
        say "WARNING: cannot create $_dir; the backup audit log is not written"
    fi
    case "$_outcome" in
        success)  say "OK: $_target — $_detail" ;;
        degraded) say "$(echo "${_action#run.}" | tr '[:lower:]' '[:upper:]'): $_target — $_detail" ;;
        *)        say "$(echo "${_action#run.}" | tr '[:lower:]' '[:upper:]'): $_error — $_detail" ;;
    esac
    return 0
}

status_val() { sed -n "s/^$1=//p" "$STATUS" 2>/dev/null | head -n 1; }

# hook <rc> <started_epoch>
cmd_hook() {
    _rc=$1; _started=${2:-}
    case "$_started" in ''|*[!0-9]*) _started=$(date +%s) ;; esac
    _run="the backup run started $(when "$_started")"
    if [ "$_rc" != 0 ]; then
        _guard=$(grep -m1 'archive-guard: ERROR: not enough space' "$LOG" 2>/dev/null | sed 's/^.*archive-guard: //')
        if [ -n "$_guard" ]; then
            cmd_record failure run.refused "no archive" \
                "$_run was refused by the space check before it wrote anything; it repeats every night until space is freed — $BACKUP_LOG_HINT" \
                "$_guard"
        else
            _why=$(grep -E '(^|[[:space:]])(ERROR|CRITICAL):' "$LOG" 2>/dev/null \
                | grep -v 'Pre-backup completed with' | head -n 1 | sed 's/^[[:space:]]*//')
            cmd_record failure run.failed "no archive" \
                "$_run stopped in its pre-backup step (exit $_rc); no archive was written — $BACKUP_LOG_HINT" \
                "${_why:-the pre-backup step exited $_rc}"
        fi
        return 0
    fi
    # Detached: no copy of the exec stream (offen reads it until EOF) and not
    # the runner's lock descriptor.
    sh "$0" watch "$_started" </dev/null >/dev/null 2>&1 9>&- &
    return 0
}

marks_active() {
    for _m in "$MARK_DIR"/*; do [ -f "$_m" ] && return 0; done
    return 1
}

# watch <started_epoch>
cmd_watch() {
    _started=$1
    case "$_started" in ''|*[!0-9]*) return 0 ;; esac
    if ! command -v flock >/dev/null 2>&1; then
        say "no flock in this container: the outcome of the run started $(when "$_started") is not recorded (rzfz status still judges it)"
        return 0
    fi
    _waited=0
    # offen creates the lock file before its first run. `flock -n` exits 0
    # when the lock is free and 1 while offen holds it (BusyBox, measured in
    # the backup-service image); anything else means the lock cannot be read,
    # and then nothing is recorded rather than a guess.
    while [ -e "$RUN_LOCK" ]; do
        _lrc=0; flock -n "$RUN_LOCK" true 2>/dev/null || _lrc=$?
        [ "$_lrc" = 0 ] && break
        if [ "$_lrc" != 1 ]; then
            say "cannot read offen's run lock (flock exit $_lrc): the outcome of the run started $(when "$_started") is not recorded (rzfz status still judges it)"
            return 0
        fi
        if [ "$_waited" -ge "$WAIT_MAX_S" ]; then
            cmd_record failure run.failed "no archive" \
                "the backup run started $(when "$_started") was still running after $((_waited / 3600)) h — $BACKUP_LOG_HINT" \
                "the run did not end within $((WAIT_MAX_S / 3600)) h"
            return 0
        fi
        sleep "$POLL_S"
        _waited=$((_waited + POLL_S))
    done
    judge "$_started"
}

# judge <started_epoch>: the run has ended; what did it leave?
judge() {
    _started=$1; _run="the backup run started $(when "$_started")"
    _name=""; _mt=0
    for _f in "$ARCHIVE_DIR"/"$PREFIX"*; do
        [ -f "$_f" ] || continue
        case "$_f" in *.sha256|*.md5) continue ;; esac
        _t=$(stat -c %Y "$_f" 2>/dev/null || echo 0)
        if [ "$_t" -ge "$_started" ] && [ "$_t" -ge "$_mt" ]; then _mt=$_t; _name=${_f##*/}; fi
    done
    if [ -z "$_name" ]; then
        cmd_record failure run.failed "no archive" \
            "$_run wrote no archive: offen's archive, encryption or copy step failed after the pre-backup step — $BACKUP_LOG_HINT" \
            "$_run wrote no archive"
        return 0
    fi
    if marks_active && [ ! -f "$MARK_DIR/$_name" ]; then
        if [ "$(status_val archive_began_early)" = 1 ]; then
            _why="its archive step began before the pre-backup step finished"
        else
            _why="the run ended in an error after writing it"
        fi
        cmd_record failure run.failed "$_name" \
            "$_run wrote $_name but it is NOT marked complete ($_why); do not rely on it — $BACKUP_LOG_HINT" \
            "$_name is not marked complete: $_why"
        return 0
    fi
    _err=$(status_val errors); _warn=$(status_val warnings); _agents=$(status_val agent_failures)
    case "$_err" in ''|*[!0-9]*) _err=0 ;; esac
    case "$_warn" in ''|*[!0-9]*) _warn=0 ;; esac
    case "$_agents" in ''|*[!0-9]*) _agents=0 ;; esac
    # The wrapper's PARTIAL: an agent volume that holds data is not in the
    # archive (a volume that does not exist has nothing to back up).
    _not_in=$(status_val skipped_detail | tr ',' '\n' | awk -F: 'NF >= 2 && $1 != "" && $2 != "missing_volume" {print $1}' | paste -sd, -)
    [ -n "$_not_in" ] || _not_in=$(status_val skipped_volumes | tr ',' '\n' | sed 's/:.*//' | grep -v '^$' | paste -sd, -)
    if [ -n "$_not_in" ]; then
        cmd_record degraded run.partial "$_name" \
            "$_run wrote $_name without agent volume(s) that hold data: $_not_in — $BACKUP_LOG_HINT"
    elif [ "$_err" -gt 0 ] || [ "$_warn" -gt 0 ] || [ "$_agents" -gt 0 ]; then
        cmd_record degraded run.degraded "$_name" \
            "$_run wrote $_name, DEGRADED: $_err error(s), $_warn warning(s), $_agents agent volume(s) not in it — $BACKUP_LOG_HINT"
    else
        cmd_record success run.ok "$_name" "$_run wrote $_name"
    fi
    return 0
}

case "${1:-}" in
    hook)   shift; cmd_hook "${1:-1}" "${2:-}" ;;
    watch)  shift; cmd_watch "${1:-}" ;;
    record) shift; cmd_record "${1:-failure}" "${2:-run.failed}" "${3:-}" "${4:-}" "${5:-}" ;;
    *) echo "usage: run-outcome.sh hook <rc> <started>|watch <started>|record <outcome> <action> <target> <detail> [<error>]" >&2; exit 2 ;;
esac
exit 0
