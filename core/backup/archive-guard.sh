#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# archive-guard.sh — keep the nightly backup out of the no-space spiral (#2833)
# ==============================================================================
# Measured on a production box: four nightly runs failed with "no space left on device".
# offen prunes expired archives only AFTER a successful run, so once one run
# failed for space nothing was pruned again and every later run failed the same
# way (12 archives, 382 GB, the oldest 11 days past a 7-day retention). One run
# also left a truncated 27.8 GB archive in /archive (copy_file_range ENOSPC) that
# looked like a backup.
#
# Two modes, both run INSIDE backup-service (BusyBox sh, POSIX only):
#
#   preflight       called by pre-backup.sh as its last step (the archive-pre
#                   hook), i.e. before offen writes anything. Removes a failed
#                   run's leftovers in /tmp, estimates what this run needs and,
#                   when the archive disk is short, frees space FIRST: archives
#                   that never got a completion mark, then archives past
#                   BACKUP_RETENTION_DAYS (never the newest complete one).
#                   Still short → exit 1, which pre-backup.sh counts as a
#                   critical failure, so offen aborts the run before it writes
#                   a partial archive. Exit 0 otherwise.
#   mark-complete   the copy-post hook (core/compose.yml label): offen stored
#                   this run's archive in /archive, so the newest archive gets a
#                   completion mark (.rzfz-complete/<name>, holding its size)
#                   — unless the pre-backup hook's report says offen began the
#                   archive while the hook still ran (#3146): then no mark,
#                   an ERROR line and exit 1, so offen's run ends in an error.
#
# The mark is what tells a finished archive from a truncated one. Until the
# first mark exists (a box on its first run with this script, or an offen
# without copy-post) no archive is treated as unmarked, so nothing is ever
# removed for lacking a mark. Archives older than the .since file (written on
# the first preflight) predate the marks and count as complete.
#
# Every step logs one "archive-guard: <step>: ..." line, so a box run shows the
# order (leftovers → estimate → free → verdict).
# ==============================================================================
set -u

ARCHIVE_DIR="${RZFZ_ARCHIVE_DIR:-/archive}"
TMP_DIR="${RZFZ_BACKUP_TMP_DIR:-/tmp}"
SOURCE_DIR="${RZFZ_BACKUP_SOURCE_DIR:-/backup}"
MARK_DIR="${RZFZ_COMPLETE_DIR:-$ARCHIVE_DIR/.rzfz-complete}"
PREFIX="${BACKUP_PRUNING_PREFIX:-backup-}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
# The next archive is estimated as the last complete one plus this much growth
# (percent). Measured on a production box: 28 → 53 GB in seven nights.
GROWTH_PCT="${RZFZ_BACKUP_GROWTH_PCT:-125}"
# The pre-backup hook's report (core/backup/pre-backup-run.sh writes it: rc=running
# before the hook, finished_epoch + archive_began_early when it is done).
PRE_BACKUP_STATUS="${RZFZ_PRE_BACKUP_LOG_DIR:-/backup/databases}/pre-backup.status"

RETENTION_DAYS=$(printf '%s' "$RETENTION_DAYS" | tr -d '"'"'"' ')
case "$RETENTION_DAYS" in ''|*[!0-9]*) RETENTION_DAYS=7 ;; esac
case "$GROWTH_PCT" in ''|*[!0-9]*) GROWTH_PCT=125 ;; esac
[ -n "$PREFIX" ] || PREFIX="backup-"

# offen runs the copy-post label through `docker exec` and keeps that command's
# output to itself, so the hook's lines never reached `docker logs
# backup-service` (the mark file was written, the log said nothing). In
# mark-complete mode every line is also copied to the container's own stdout
# (PID 1), the way pre-backup-run.sh does for the archive-pre hook. preflight
# already runs inside that runner's log copy and must not print twice.
CONTAINER_STDOUT="${RZFZ_CONTAINER_STDOUT:-/proc/1/fd/1}"
MIRROR=0
say() {
    echo "archive-guard: $*"
    if [ "$MIRROR" = 1 ] && [ -w "$CONTAINER_STDOUT" ]; then
        echo "archive-guard: $*" >> "$CONTAINER_STDOUT" 2>/dev/null || true
    fi
}
now() { echo "${RZFZ_GUARD_NOW:-$(date +%s)}"; }
mib() { echo $(( ($1 + 1048575) / 1048576 )); }

# Archives of this stack in ARCHIVE_DIR, newest first: "<mtime> <size> <name>".
archives() {
    for f in "$ARCHIVE_DIR"/"$PREFIX"*; do
        [ -f "$f" ] || continue
        case "$f" in *.sha256|*.md5) continue ;; esac
        printf '%s %s %s\n' "$(stat -c %Y "$f")" "$(stat -c %s "$f")" "${f##*/}"
    done | sort -rn
}

marks_active() {
    for m in "$MARK_DIR"/*; do [ -f "$m" ] && return 0; done
    return 1
}

since() { cat "$MARK_DIR/.since" 2>/dev/null || echo 0; }

# complete <mtime> <name>: an archive is complete when it carries a mark, when
# marks are not in use yet, or when it predates the marks.
complete() {
    marks_active || return 0
    [ -f "$MARK_DIR/$2" ] && return 0
    [ "$1" -le "$(since)" ] && return 0
    return 1
}

# Free bytes on the filesystem holding $1.
avail_bytes() { df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $4 * 1024}'; }
same_fs() { [ "$(stat -c %d "$1")" = "$(stat -c %d "$2")" ]; }

# The staging token core/compose.yml and razzfazz-backup append (#2584): RE2
# only (`\z`), and it names nothing at the top level of /backup.
STAGING_EXCLUDE='^/backup/(databases|clickhouse-backup)/\.staging(/|\z)'

# The no-archive estimate: what offen would tar (top-level source dirs not
# matched by BACKUP_EXCLUDE_REGEXP, the staging token taken out first).
source_bytes() {
    _ere=$(printf '%s\n' "${BACKUP_EXCLUDE_REGEXP:-}" | T="$STAGING_EXCLUDE" awk '{
        t = ENVIRON["T"]
        while ((i = index($0, t)) > 0) $0 = substr($0, 1, i - 1) substr($0, i + length(t))
        print }' | tr '|' '\n' | grep -v '\\' | grep -v '^$' | paste -sd'|' -)
    _sum=0
    for d in "$SOURCE_DIR"/*; do
        [ -e "$d" ] || continue
        if [ -n "$_ere" ] && printf '%s\n' "$d" | grep -Eq "$_ere"; then continue; fi
        _k=$(du -sk "$d" 2>/dev/null | awk '{print $1}')
        _sum=$((_sum + ${_k:-0} * 1024))
    done
    echo "$_sum"
}

remove() {  # remove <path> <why>
    if rm -f -- "$1"; then say "removed ${1##*/} ($2)"; else say "WARNING: could not remove ${1##*/} ($2)"; fi
}

cmd_mark_complete() {
    MIRROR=1
    mkdir -p "$MARK_DIR" || { say "WARNING: cannot create $MARK_DIR; this archive gets no completion mark"; return 0; }
    _newest=$(archives | head -n 1)
    if [ -z "$_newest" ]; then say "mark-complete: no archive in $ARCHIVE_DIR"; return 0; fi
    set -- $_newest
    # #3146: an archive offen began while the hook was still writing is not
    # complete, whatever offen says. Measured on a production box before the #2584 heartbeat:
    # the exec stream was cut at 600 s on most nights, offen archived /backup
    # mid-hook, and the archive lacked an agent instance and held a users dump
    # days old. No mark (rzfz status FAILs on it, the preflight never protects
    # it as the newest complete one), and a non-zero exit: offen's run ends in
    # an error, so its notification fires and the wrapper says FAILED.
    # No report at all (a label without the runner) marks as before.
    if [ -f "$PRE_BACKUP_STATUS" ]; then
        if ! grep -q '^finished_epoch=[0-9]' "$PRE_BACKUP_STATUS"; then
            say "ERROR: mark-complete: $3 is NOT marked complete: the pre-backup step was still running when this archive was written, so it may lack the database dumps and agent snapshots written after that. Run the backup again."
            return 1
        fi
        if grep -q '^archive_began_early=1' "$PRE_BACKUP_STATUS"; then
            say "ERROR: mark-complete: $3 is NOT marked complete: the archive step began before the pre-backup step finished, so it may lack files written after that. Run the backup again."
            return 1
        fi
    fi
    printf '%s\n' "$2" > "$MARK_DIR/$3.tmp" && mv -f "$MARK_DIR/$3.tmp" "$MARK_DIR/$3"
    say "mark-complete: $3 ($(mib "$2") MiB) marked complete"
    # Marks of archives that are gone (pruned, deleted) go with them.
    for m in "$MARK_DIR"/*; do
        [ -f "$m" ] || continue
        [ -f "$ARCHIVE_DIR/${m##*/}" ] || rm -f -- "$m"
    done
    return 0
}

cmd_preflight() {
    mkdir -p "$MARK_DIR" 2>/dev/null || true
    [ -f "$MARK_DIR/.since" ] || now > "$MARK_DIR/.since" 2>/dev/null || true

    # 1. A failed run's temp files: this run has not started its archive yet,
    #    so anything of ours in /tmp is a leftover.
    _n=0
    for f in "$TMP_DIR"/"$PREFIX"*; do
        [ -f "$f" ] || continue
        remove "$f" "leftover of a failed run in the temp dir"; _n=$((_n + 1))
    done
    say "leftovers: $_n temp file(s) of earlier runs removed"

    # 2. Estimate this run's archive.
    _last=""
    while read -r m s n; do
        [ -n "$m" ] || continue
        if complete "$m" "$n"; then _last="$s $n"; break; fi
    done <<EOF
$(archives)
EOF
    if [ -n "$_last" ]; then
        _est=$(( ${_last%% *} / 100 * GROWTH_PCT ))
        say "estimate: $(mib "$_est") MiB (last complete archive ${_last#* }, $(mib "${_last%% *}") MiB, +$((GROWTH_PCT - 100)) %)"
    else
        _est=$(source_bytes)
        say "estimate: $(mib "$_est") MiB (no complete archive yet: the size of the backup sources)"
    fi

    # 3. What each disk must hold: offen keeps the .tar.gz and the .gpg in /tmp
    #    and copies the .gpg into /archive before it deletes either.
    if same_fs "$TMP_DIR" "$ARCHIVE_DIR"; then
        _need=$((_est * 3)); _need_tmp=0
    else
        _need=$_est; _need_tmp=$((_est * 2))
    fi
    _have=$(avail_bytes "$ARCHIVE_DIR"); _have=${_have:-0}
    _have_tmp=$(avail_bytes "$TMP_DIR"); _have_tmp=${_have_tmp:-0}
    say "space: archive disk needs $(mib "$_need") MiB, has $(mib "$_have") MiB free$( [ "$_need_tmp" -gt 0 ] && echo "; temp disk needs $(mib "$_need_tmp") MiB, has $(mib "$_have_tmp") MiB free")"

    if [ "$_have" -ge "$_need" ] && { [ "$_need_tmp" -eq 0 ] || [ "$_have_tmp" -ge "$_need_tmp" ]; }; then
        say "verdict: OK, enough space for this run"
        return 0
    fi

    # 4. Short: free space before the run instead of failing in the middle.
    #    a) archives a run never finished (no completion mark)
    #    b) archives past the retention, oldest first, never the newest complete one
    _cut=$(( $(now) - RETENTION_DAYS * 86400 ))
    _keep=${_last#* }
    while read -r m s n; do
        [ -n "$m" ] || continue
        complete "$m" "$n" || remove "$ARCHIVE_DIR/$n" "no completion mark: its run failed while writing it"
    done <<EOF
$(archives)
EOF
    archives | sort -n > "$MARK_DIR/.list" 2>/dev/null || true
    while read -r m s n; do
        [ -n "$m" ] || continue
        [ "$m" -lt "$_cut" ] || continue
        [ "$n" = "$_keep" ] && continue
        [ -f "$ARCHIVE_DIR/$n" ] || continue
        remove "$ARCHIVE_DIR/$n" "older than BACKUP_RETENTION_DAYS=$RETENTION_DAYS"
        rm -f -- "$MARK_DIR/$n"
    done < "$MARK_DIR/.list"
    rm -f -- "$MARK_DIR/.list"

    _have=$(avail_bytes "$ARCHIVE_DIR"); _have=${_have:-0}
    _have_tmp=$(avail_bytes "$TMP_DIR"); _have_tmp=${_have_tmp:-0}
    if [ "$_have" -ge "$_need" ] && { [ "$_need_tmp" -eq 0 ] || [ "$_have_tmp" -ge "$_need_tmp" ]; }; then
        say "verdict: OK after freeing space: archive disk has $(mib "$_have") MiB free, needs $(mib "$_need") MiB"
        return 0
    fi
    say "ERROR: not enough space for this backup run: the archive disk has $(mib "$_have") MiB free and needs $(mib "$_need") MiB$( [ "$_need_tmp" -gt 0 ] && echo ", the temp disk has $(mib "$_have_tmp") MiB and needs $(mib "$_need_tmp") MiB") (estimated archive $(mib "$_est") MiB). Nothing past BACKUP_RETENTION_DAYS=$RETENTION_DAYS is left to remove. The run is stopped before it writes a partial archive. Free space on the backup disk, lower BACKUP_RETENTION_DAYS, or exclude more volumes (BACKUP_EXCLUDE_REGEXP)."
    return 1
}

case "${1:-}" in
    preflight) cmd_preflight ;;
    mark-complete) cmd_mark_complete || exit 1 ;;
    *) echo "usage: archive-guard.sh preflight|mark-complete" >&2; exit 2 ;;
esac
