#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# stage-live-volumes.sh — archive a running service's volume from a copy (#3450)
# ==============================================================================
# Some services keep running through the nightly backup on purpose: stopping
# wazuh-manager is a scheduled detection gap (#855 DECISION-10). But it appends
# to files in its volume all the time, and offen's tar fails the WHOLE archive
# on a file that grows between its header and its copy ("archive/tar: write
# too long"): measured on a production box, no archive was written at all.
#
# So the archive never reads such a volume live. This script, run by
# pre-backup.sh before the dumps, copies each volume below from its read-only
# /backup/<volume> mount into the hook's staging dir and moves the finished
# copy to /backup/databases/<volume> (the db-dumps volume, archived). The live
# /backup/<volume> is excluded from the archive by a token core/compose.yml and
# the razzfazz-backup wrapper always append. A restore reads the copy
# (core/backup/manager/backup_manager.py STAGED_VOLUMES).
#
# `cp -a` copies a file that grows as far as it has read it, never an error;
# what changed during the copy is the state a moment later, as with any copy
# of a running service. Sockets and FIFOs are runtime endpoints, not data: the
# service recreates them on start, and the archive could not hold them.
#
# The previous run's copy is removed BEFORE the copy starts, and free space is
# measured after that, so the docker disk never has to hold two copies.
#
# The copy is checked three ways, and each failure is an ERROR line (the backup
# reports DEGRADED): cp's exit code (only an entry the service removed during
# the walk is forgiven), every source entry against the copy, and every regular
# file's size (a file shorter in the copy is fine only when the service changed
# it during the copy, i.e. its size now differs from before). The copy is still
# published and the archive still runs. Exit 1 when anything was not staged
# completely, 0 otherwise.
#
# BusyBox sh, POSIX only. `--list` prints the volumes and exits.
# ==============================================================================
set -u
VOLUMES="${RZFZ_STAGE_VOLUMES:-wazuh-manager-data}"
if [ "${1:-}" = "--list" ]; then
    for v in $VOLUMES; do echo "$v"; done
    exit 0
fi
SRC_ROOT="${RZFZ_STAGE_SOURCE_ROOT:-/backup}"
DEST_ROOT="${RZFZ_STAGE_DEST_ROOT:-/backup/databases}"
STAGING="${RZFZ_STAGE_STAGING_DIR:-$DEST_ROOT/.staging}"
# Headroom over the volume's size before a copy is started (percent).
MARGIN_PCT=10
RC=0

say() { echo "stage-live: $*"; }
kb() { du -sk "$1" 2>/dev/null | awk '{print $1+0}'; }
avail_kb() {
    if [ -n "${RZFZ_STAGE_AVAIL_KB:-}" ]; then echo "$RZFZ_STAGE_AVAIL_KB"; return; fi
    df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $4+0}'
}
# Relative paths of every entry below $1 that the copy has to hold.
entries() { (cd "$1" && find . -mindepth 1 ! -type s ! -type p) | LC_ALL=C sort; }
# "<size> <relative path>" for every regular file below $1.
sizes() { (cd "$1" && find . -type f -exec stat -c '%s %n' {} +); }
# #3478: mark_error <vol> <reason> [<key=value> ...] — the staging result
# TRAVELS WITH THE ARCHIVE: /backup/databases/<vol>.staging-error lies beside
# the copy in the archived db-dumps volume. A restore that finds it does not
# replace the live volume with this copy unless told to
# (RAZZFAZZ_RESTORE_ALLOW_INCOMPLETE_STAGED=1, backup_manager.py). Written in
# the staging dir and moved into place, like every file of the hook.
mark_error() {
    _mv="$1"; _mr="$2"; shift 2
    _mt="$STAGING/$_mv.staging-error"
    if {
        printf 'reason=%s\n' "$_mr"
        for _kv in "$@"; do printf '%s\n' "$_kv"; done
        printf 'written=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$_mt" && mv -f "$_mt" "$DEST_ROOT/$_mv.staging-error"; then
        return 0
    fi
    rm -f "$_mt" 2>/dev/null
    say "ERROR: $_mv: could not write the marker $DEST_ROOT/$_mv.staging-error (reason: $_mr)"
    return 1
}

umask 077
mkdir -p "$STAGING" || { say "ERROR: cannot create $STAGING; nothing staged"; exit 1; }

for vol in $VOLUMES; do
    src="$SRC_ROOT/$vol"
    final="$DEST_ROOT/$vol"
    tmp="$STAGING/$vol"
    rm -rf "$tmp"
    if [ ! -d "$src" ] || [ -z "$(ls -A "$src" 2>/dev/null)" ]; then
        # The module is off (an empty volume is still mounted). A copy from an
        # earlier run would be archived as if it were current: it goes, and
        # its marker with it.
        rm -f "$DEST_ROOT/$vol.staging-error"
        if [ -e "$final" ]; then
            rm -rf "$final"
            say "$vol: empty or not mounted; the copy from an earlier run is removed"
        else
            say "$vol: empty or not mounted; nothing to stage"
        fi
        continue
    fi

    # #3478 review: the marker goes down FIRST and comes off LAST. A failure
    # can leave the disk too full to write a marker afterwards (the ENOSPC
    # that cut the copy off fails the marker too), and a hook killed between
    # the copy and its checks writes nothing at all — so "copy not finished"
    # is written before anything is touched, overwritten with the reason when
    # a check fails, and removed only when every check has passed.
    # If even this first marker cannot be written, nothing is touched: the
    # previous run's copy stays together with its own marker state (complete
    # and unmarked, or marked), so the archive is consistent — an older copy
    # restored as one, never a truncated one restored silently. Removing it
    # instead would leave an archive with neither copy nor marker, which a
    # restore skips without a word.
    if ! mark_error "$vol" "copy not finished: started $(date -u +%Y-%m-%dT%H:%M:%SZ) and did not complete (the run was stopped or failed before every check had passed)"; then
        say "ERROR: $vol: not staged: the marker could not be written up front, so no copy was started. This backup holds the PREVIOUS run's copy of $vol as it was (with its own marker, if it had one). Free space on the docker disk."
        RC=1
        continue
    fi

    # The previous run's copy goes FIRST: the docker disk never holds two
    # copies, and the space check below sees only what is really free.
    rm -rf "$final"
    if [ -e "$final" ]; then
        say "ERROR: $vol: the previous copy could not be removed — this backup does NOT hold a current $vol"
        mark_error "$vol" "not staged: the previous copy could not be removed (the copy in this archive is an older one)"
        RC=1
        continue
    fi
    need=$(kb "$src")
    free=$(avail_kb "$DEST_ROOT")
    want=$(( ${need:-0} * (100 + MARGIN_PCT) / 100 ))
    if [ "$want" -gt "${free:-0}" ]; then
        say "ERROR: $vol: not staged: the copy needs $(( (want + 1023) / 1024 )) MiB and the db-dumps volume has $(( ${free:-0} / 1024 )) MiB free — this backup does NOT hold $vol. Free space on the docker disk."
        mark_error "$vol" "not staged: too little space on the db-dumps volume (needs ${want} KiB, ${free:-0} KiB free)"
        RC=1
        continue
    fi

    work=$(mktemp -d) || { say "ERROR: $vol: mktemp failed; not staged"; mark_error "$vol" "not staged: mktemp failed"; RC=1; continue; }
    list_before="$work/before"; list_copy="$work/copy"; cperr="$work/cperr"
    entries "$src" > "$list_before"
    sizes "$src" > "$work/sizes-before"
    cp -a "$src" "$tmp" 2> "$cperr"
    cprc=$?
    if [ ! -d "$tmp" ]; then
        say "ERROR: $vol: the copy failed (cp exit $cprc: $(head -c 400 "$cperr" | tr '\n' ' ')) — this backup does NOT hold $vol"
        mark_error "$vol" "not staged: cp failed (exit $cprc): $(head -c 400 "$cperr" | tr '\n' ' ')" "cp_exit=$cprc"
        rm -rf "$work"
        RC=1
        continue
    fi
    # cp's exit code counts. The one failure that is not an error is an entry
    # the service removed while cp walked the tree ("No such file or
    # directory"); anything else (a full disk, a permission, an I/O error) is.
    cp_errors=$(grep -v 'No such file or directory' "$cperr" | head -c 400 | tr '\n' ' ')
    if [ "$cprc" -ne 0 ] && [ -z "$cp_errors" ] && [ ! -s "$cperr" ]; then
        cp_errors="exit $cprc without a message"
    fi
    find "$tmp" \( -type s -o -type p \) -exec rm -f {} \; 2>/dev/null
    entries "$tmp" > "$list_copy"

    # Missing = listed before the copy, absent from it, and still in the
    # volume (an entry the service removed during the copy is not missing).
    missing=0
    gone=0
    missing_names=""
    while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        if [ -e "$src/$rel" ] || [ -L "$src/$rel" ]; then
            missing=$((missing + 1))
            [ "$missing" -le 5 ] && missing_names="$missing_names ${rel#./}"
        else
            gone=$((gone + 1))
        fi
    done <<EOF
$(LC_ALL=C comm -23 "$list_before" "$list_copy")
EOF

    # Sizes: a regular file whose copy differs in size from the source as it
    # was before the copy is fine only when the service changed it meanwhile
    # (its size now differs from that too). Same size before and now, but a
    # different size in the copy: the copy was cut off (a full disk) — ERROR.
    sizes "$tmp" > "$work/sizes-copy"
    short=0
    changed=0
    short_names=""
    while IFS=' ' read -r before copy rel; do
        [ -n "$rel" ] || continue
        now=$(stat -c %s "$src/$rel" 2>/dev/null) || now=gone
        if [ "$now" = "$before" ]; then
            short=$((short + 1))
            [ "$short" -le 5 ] && short_names="$short_names ${rel#./} ($copy of $before bytes)"
        else
            changed=$((changed + 1))
        fi
    done <<EOF
$(awk 'NR == FNR { s = $1; sub(/^[0-9]+ /, ""); b[$0] = s; next }
       { s = $1; sub(/^[0-9]+ /, ""); if (($0 in b) && b[$0] != s) print b[$0], s, $0 }' \
     "$work/sizes-before" "$work/sizes-copy")
EOF

    files=$(find "$tmp" -type f | wc -l | tr -d ' ')
    dirs=$(find "$tmp" -mindepth 1 -type d | wc -l | tr -d ' ')
    links=$(find "$tmp" -type l | wc -l | tr -d ' ')
    total=$(wc -l < "$list_copy" | tr -d ' ')
    size=$(( ($(kb "$tmp") + 1023) / 1024 ))

    if ! mv "$tmp" "$final"; then
        say "ERROR: $vol: the finished copy could not be moved into place — this backup does NOT hold $vol"
        mark_error "$vol" "not staged: the finished copy could not be moved into place"
        rm -rf "$tmp" "$final" "$work"
        RC=1
        continue
    fi
    say "$vol: staged $total entries ($files files, $dirs dirs, $links links), $size MiB; $gone removed and $changed changed by the service during the copy"
    if [ -n "$cp_errors" ]; then
        say "ERROR: $vol: cp failed (exit $cprc): $cp_errors — the copy in this backup may be incomplete"
        RC=1
    fi
    if [ "$missing" -gt 0 ]; then
        say "ERROR: $vol: $missing entries are NOT in the copy although the volume still has them:$missing_names$( [ "$missing" -gt 5 ] && echo ' …') — a restore of this backup lacks them"
        RC=1
    fi
    if [ "$short" -gt 0 ]; then
        say "ERROR: $vol: $short files are cut off in the copy although the volume did not change them:$short_names$( [ "$short" -gt 5 ] && echo ' …') — a restore of this backup has them truncated"
        RC=1
    fi
    if [ -n "$cp_errors" ] || [ "$missing" -gt 0 ] || [ "$short" -gt 0 ]; then
        _why=""
        [ -n "$cp_errors" ] && _why="cp failed (exit $cprc): $cp_errors; "
        [ "$missing" -gt 0 ] && _why="${_why}$missing entries missing:$missing_names; "
        [ "$short" -gt 0 ] && _why="${_why}$short files cut off:$short_names; "
        mark_error "$vol" "incomplete copy: ${_why%; }" "entries=$total" "files=$files" \
            "missing=$missing" "cut_off=$short" "cp_exit=$cprc"
    elif ! rm -f "$DEST_ROOT/$vol.staging-error"; then
        # every check passed: only now does the marker come off
        say "ERROR: $vol: the copy is complete but its marker could not be removed — a restore of this backup will refuse it"
        RC=1
    fi
    rm -rf "$work"
done
exit "$RC"
