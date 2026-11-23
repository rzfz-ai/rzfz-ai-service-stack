#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# verified-backup-before-upgrade.sh — a backup that is checked to be complete,
# taken by hand on a 2026.08 or 2026.09 box before it upgrades to 2026.11.
# ==============================================================================
# Why: on 2026.08 and 2026.09 the backup service reads the pre-backup step's
# output through the Docker socket proxy, which closes that stream after 10
# minutes without output. The backup then archives while the pre-backup step is
# still writing (database dumps, agent volumes) and reports success anyway. The
# backup that `rzfz upgrade` takes first runs that same code, and its
# "Pre-upgrade backup complete." message is printed whatever the outcome.
# 2026.11 fixes both, but only once it is installed.
#
# What this script does, without changing the box's code:
#   1. starts `rzfz backup backup` (the normal manual backup, all agents);
#   2. every 10 seconds while the pre-backup step runs, writes one line into its
#      error output, the stream the backup service listens to, so the proxy
#      never sees 10 idle minutes (2026.11 does the same from inside);
#   3. checks that the pre-backup step had ended before the backup service
#      began its archive, that the service logged no error, that every file the
#      step writes is from this run and was finished before the archive began,
#      and that an archive from this run exists.
#
# Exit 0 and "VERIFIED COMPLETE" = the archive is complete. Anything else: do
# not rely on this archive.
#
# Get it on a 2026.08/2026.09 box (from the stack directory):
#   git fetch --tags origin
#   git show 2026.11-ga:scripts/verified-backup-before-upgrade.sh > verified-backup-before-upgrade.sh
# Run:
#   bash verified-backup-before-upgrade.sh [stack directory]
#
# Run it as the stack's user. It reads no secret and prints none.
# ==============================================================================
set -euo pipefail

case "${1:-}" in
    -h|--help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

ST="${1:-$PWD}"
cd "$ST" || exit 2
[ -x "$ST/rzfz" ] && [ -f "$ST/.env" ] || { echo "Run this from the stack directory (rzfz and .env not found in $ST)." >&2; exit 2; }
OUT="$ST/backups/logs/verified-backup-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$ST/backups/logs"
C=backup-service

# the pre-backup step's process ids inside the backup service ([/] keeps pgrep from matching itself)
hook_pids() { docker exec "$C" sh -c 'pgrep -f "[/]scripts/pre-backup[.]sh" || true' 2>/dev/null || true; }
docker inspect "$C" >/dev/null 2>&1 || { echo "The container $C is not there; is the stack running?" >&2; exit 2; }
if [ -n "$(hook_pids)" ]; then echo "A backup is already running in $C; wait for it to finish." >&2; exit 2; fi

# The old pre-backup step writes agents/instances.sql only while agent-manager runs, and
# agents/owui-users.sql.enc only when agent instances exist: what it must write depends on the box.
amgr=0
[ "$(docker inspect -f '{{.State.Running}}' agent-manager 2>/dev/null || true)" = true ] && amgr=1

START=$(date +%s)
echo "start $(date -Is); log: $OUT"
"$ST/rzfz" backup backup > "$OUT" 2>&1 < /dev/null &
BPID=$!

# keepalive, and when the pre-backup step was last seen running
seen=0; hook_end=""; last_seen=0
while kill -0 "$BPID" 2>/dev/null; do
    p=$(hook_pids)
    if [ -n "$p" ]; then
        seen=1; last_seen=$(date +%s)
        docker exec "$C" sh -c 'for p in $(pgrep -f "[/]scripts/pre-backup[.]sh"); do echo "keepalive $(date +%T)" > /proc/$p/fd/2 2>/dev/null || true; done' >/dev/null 2>&1 || true
    elif [ "$seen" = 1 ] && [ -z "$hook_end" ]; then
        hook_end=$(date +%s)
        echo "pre-backup step ended at $(date -d "@$hook_end" -Is) ($((hook_end - START)) s after start)"
    fi
    sleep "${RZFZ_KEEPALIVE_SECONDS:-10}" &
    wait $! || true
done
rc=0; wait "$BPID" || rc=$?
echo "rzfz backup backup exit $rc"
fail=0

# the backup service's own log lines: when did it stop containers / archive?
stop_line=$(grep -m1 -E 'Stopping [0-9]+ out of|Created backup of' "$OUT" || true)
stop_ts=$(printf '%s\n' "$stop_line" | sed -n 's/^time=\([^ ]*\) .*/\1/p')
stop_epoch=""
[ -z "$stop_ts" ] || stop_epoch=$(date -d "$stop_ts" +%s 2>/dev/null || true)
if [ -z "$stop_epoch" ]; then
    echo "FAIL: the start of the archive step was not found in the run log"; fail=1
elif [ "$seen" != 1 ]; then
    # A step shorter than one check interval is never seen running (a small box: a few seconds). offen's
    # own log line says it ran; a step that short cannot be the 10-minute cut, and its files are checked below.
    # Counts only for a step that short: offen's archive-pre line to its archive step within two
    # intervals. A longer step the probe never saw was not watched at all.
    pre_ts=$(grep -m1 'archive-pre command' "$OUT" | sed -n 's/^time=\([^ ]*\) .*/\1/p' || true)
    pre_epoch=""
    [ -z "$pre_ts" ] || pre_epoch=$(date -d "$pre_ts" +%s 2>/dev/null || true)
    iv=${RZFZ_KEEPALIVE_SECONDS:-10}
    if [ -z "$pre_epoch" ]; then
        echo "FAIL: the pre-backup step was never seen running"; fail=1
    elif [ $((stop_epoch - pre_epoch)) -le $((2 * iv)) ]; then
        echo "OK: the pre-backup step ran (backup service log) and ended within one check interval (${iv} s), before the archive began ($stop_ts)"
    else
        echo "FAIL: the pre-backup step ran $((stop_epoch - pre_epoch)) s (backup service log) but was never seen: the probe could not watch it"; fail=1
    fi
elif [ "$last_seen" -ge "$stop_epoch" ]; then
    echo "FAIL: the archive began at $stop_ts while the pre-backup step was still running at $(date -d "@$last_seen" -Is): the archive is INCOMPLETE"; fail=1
else
    echo "OK: the pre-backup step was last seen running at $(date -d "@$last_seen" -Is), before the archive began ($stop_ts)"
fi
if grep -q 'Fatal error\|level=ERROR' "$OUT"; then echo "FAIL: the backup service reported an error (see $OUT)"; fail=1; fi

# Live agent instances in THIS run: a dump of agent_instances written by this run with a row that is not
# destroyed. The old steps do not clear what an earlier run left in the db-dumps volume (2026.08-ga.15
# never; 2026.09-ga.5 only the per-instance directories, and only while agent-manager runs), so a file or
# directory alone says nothing about this run.
inst=0
it=$(docker exec "$C" sh -c 'stat -c %Y /backup/databases/agents/instances.sql 2>/dev/null' 2>/dev/null || true)
if [ -n "$it" ] && [ "$it" -ge "$START" ]; then
    live=$(docker exec "$C" sh -c 'cat /backup/databases/agents/instances.sql' 2>/dev/null | awk -F'\t' '
        index($0, "COPY public.agent_instances ") == 1 {
            h = $0; sub(/^[^(]*\(/, "", h); sub(/\).*/, "", h); n = split(h, c, ", ")
            k = 0; for (i = 1; i <= n; i++) if (c[i] == "state") k = i
            on = 1; next }
        on && $0 == "\\." { on = 0; next }
        on && k && $k != "destroyed" { r++ }
        END { print r + 0 }' || true)
    [ "${live:-0}" -gt 0 ] && inst=1
fi

# everything the pre-backup step writes must be from THIS run and finished before the archive began
if ! chk=$(docker exec "$C" sh -c '
    s='"$START"'
    e='"${stop_epoch:-0}"'
    amgr='"$amgr"'
    inst='"$inst"'
    for m in /backup/databases/agents/*/manifest.json; do [ -f "$m" ] && [ "$(stat -c %Y "$m")" -ge "$s" ] && inst=1; done
    for f in /backup/databases/postgres_core.sql /backup/databases/env-backup.enc /backup/databases/agents/instances.sql /backup/databases/agents/owui-users.sql.enc; do
        need=1
        case "$f" in
            */agents/instances.sql) need=$amgr ;;
            */agents/owui-users.sql.enc) need=$inst ;;
        esac
        if [ ! -e "$f" ]; then [ "$need" = 1 ] && echo "MISSING $f"; continue; fi
        t=$(stat -c %Y "$f")
        if [ "$t" -lt "$s" ]; then
            if [ "$need" = 1 ]; then echo "STALE $f"; else echo "LEFTOVER $f"; fi
            continue
        fi
        [ "$e" -eq 0 ] || [ "$t" -le "$e" ] || echo "LATE $f (written after the archive began)"
    done
    # agents/<slug>/manifest.json came with 2026.09-ga.4 (#2531); a 2026.08 step writes only the tars,
    # so there every file under the agent directory must be from this run and finished before the archive.
    mf=0
    grep -q "manifest.json" /scripts/pre-backup.sh 2>/dev/null && mf=1
    for d in /backup/databases/agents/*/; do
        [ -d "$d" ] || continue
        if [ "$mf" = 0 ]; then
            n=0
            for f in $(find "$d" -type f); do
                t=$(stat -c %Y "$f")
                # a 2026.08 step never refreshes a tar (#2544): one from an earlier run is not data of this run
                if [ "$t" -lt "$s" ]; then echo "OLDSNAP $f"; continue; fi
                n=$((n + 1))
                [ "$e" -eq 0 ] || [ "$t" -le "$e" ] || echo "LATE $f (written after the archive began)"
            done
            # #2544: a 2026.08 step cannot snapshot agent volumes at all (BusyBox tar has no -S, and the
            # error went to /dev/null), so on 2026.08 an agent directory is empty in every backup.
            [ "$n" -gt 0 ] || echo "NOSNAP $d"
            continue
        fi
        m="${d}manifest.json"
        # without agent-manager the step skips the agent section and clears nothing: a directory is from an
        # earlier run. With it, the step clears every directory first, so an old one is a real FAIL.
        if [ ! -f "$m" ]; then if [ "$amgr" = 1 ]; then echo "NO-MANIFEST $d"; else echo "LEFTOVER $d"; fi; continue; fi
        t=$(stat -c %Y "$m")
        if [ "$t" -lt "$s" ]; then if [ "$amgr" = 1 ]; then echo "STALE $m"; else echo "LEFTOVER $d"; fi; continue; fi
        [ "$e" -eq 0 ] || [ "$t" -le "$e" ] || echo "LATE $m (written after the archive began)"
        grep -o "\"name\": \"[^\"]*\"[^}]*\"archived\": false" "$m" | sed "s/^/NOT-ARCHIVED /" || true
    done
    echo CHECKED'); then
    echo "FAIL: could not check the backup files in $C"; fail=1; chk=""
fi
if [ -n "$chk" ] && ! grep -qx 'CHECKED' <<<"$chk"; then echo "FAIL: the file check in $C did not finish"; fail=1; fi
# here-strings, not printf | grep -q: an early grep exit would SIGPIPE printf and pipefail would hide the match
if grep -qE '^(MISSING|STALE|LATE|NO-MANIFEST)' <<<"$chk"; then
    grep -E '^(MISSING|STALE|LATE|NO-MANIFEST)' <<<"$chk" | sed 's/^/FAIL: /'; fail=1
fi
grep '^NOT-ARCHIVED' <<<"$chk" | sed 's/^/NOTE (left out by the size limit or a failed snapshot, as in every nightly backup): /' || true
grep '^LEFTOVER' <<<"$chk" | sed 's/^LEFTOVER /NOTE: left over from an earlier run (not written by this one; what it holds is old): /' || true
grep '^OLDSNAP' <<<"$chk" | sed 's/^OLDSNAP /NOTE: NOT in this archive: the current agent volume — this is an earlier run'"'"'s snapshot (a 2026.08 box cannot snapshot agent volumes, #2544), copy the volumes separately before the upgrade: /' || true
grep '^NOSNAP' <<<"$chk" | sed 's/^NOSNAP /NOTE: NOT in this archive: the agent volumes of /; s/$/ (a 2026.08 box cannot snapshot agent volumes, #2544): copy them separately before the upgrade/' || true

N=$(ls -t "$ST"/backups/backup-*.tar.gz.gpg 2>/dev/null | head -n 1 || true)
if [ -z "$N" ] || [ "$(stat -c %Y "$N")" -lt "$START" ]; then
    echo "FAIL: no archive from this run in backups/"; fail=1
else
    echo "archive: $N ($(stat -c %s "$N") bytes)"
fi
tail -n 5 "$OUT" || true
if [ "$fail" -eq 0 ] && [ "$rc" -eq 0 ]; then echo "VERIFIED COMPLETE"; exit 0; fi
echo "NOT VERIFIED: do not rely on this archive"; exit 1
