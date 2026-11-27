#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# #1428 — the box lock (CLAUDE.md, multi-agent rule 5), done so it cannot lie.
#   scripts/box-lock.sh run --agent <id> --issue <n> -- <command…>
#   scripts/box-lock.sh status
# NEVER with sudo: /var/lock is sticky + world-writable and fs.protected_regular=2
# refuses root a write-open of a file it does not own (flock exit 66).
# Exit: the command's code · 75 held by another holder · 3 cannot open the lock
#       · 4 flock itself failed · 2 usage.
# The lock is a real flock(2) on the file. Who holds it is answered from the
# kernel's view (processes that have the lock file open), never from the text in
# an announcement file; the announcement (<lock>.info) only adds agent/issue/age
# and is trusted only when its pid is one of the real holders.
set -euo pipefail

LOCK="${RAZZFAZZ_BOX_LOCK:-/var/lock/razzfazz-agent.lock}"
INFO="${LOCK}.info"

# Open the lock file the way the caller will and report the failure text.
# Prints nothing and returns 0 when the open works; $1 is ">>" or "<".
probe_open() {
    local err
    if [ "$1" = "<" ]; then
        err=$( { : <"$LOCK"; } 2>&1 ) || { printf '%s\n' "${err##*: }"; return 1; }
    else
        err=$( { : >>"$LOCK"; } 2>&1 ) || { printf '%s\n' "${err##*: }"; return 1; }
    fi
}

cannot_open() {
    local why="$1"
    echo "cannot open ${LOCK} (${why}) — the lock is NOT known to be held."
    case "$why" in
        *"Permission denied"*)
            echo "Likely fs.protected_regular: a file in a sticky world-writable directory that you do not own cannot be write-opened, not even by root. Drop sudo and run as the file's owner; a foreign-owned lock file has to be removed once by the operator. (#1428)" ;;
    esac
}

# PIDs that hold the lock file open, matched by device:inode (not by path).
# Processes we may not inspect (other users, other pid namespaces) are invisible.
holder_pids() {
    local id line
    id=$(stat -c '%d:%i' "$LOCK" 2>/dev/null) || return 0
    # one stat call for every fd; unreadable /proc entries are silently skipped
    stat -L -c '%d:%i %n' /proc/[0-9]*/fd/* 2>/dev/null |
        while read -r line; do
            [ "${line%% *}" = "$id" ] || continue
            line="${line#* /proc/}"
            echo "${line%%/*}"
        done | sort -un || true
}

# is the lock really held right now? 0 = held, 1 = free
lock_is_held() {
    local rc=0
    ( flock -n -E 75 9 ) 9<"$LOCK" 2>/dev/null || rc=$?
    [ "$rc" = 75 ]
}

info_field() { { sed -n "s/^$1=//p" "$INFO" 2>/dev/null || true; } | head -n 1; }

describe() {
    local pid pids kids age agetxt ann ipid start now n=0
    ipid=$(info_field pid)
    now=$(date +%s)
    pids=$(holder_pids)
    for pid in $pids; do
        [ "$pid" = "$$" ] && continue
        n=$((n + 1))
        kids=$(pgrep -c -P "$pid" 2>/dev/null) || true
        ann="no announcement"
        age="unknown"; agetxt="unknown"
        if [ -n "$ipid" ] && [ "$ipid" = "$pid" ]; then
            ann="$(info_field agent) issue-$(info_field issue) since $(info_field started)"
            start=$(info_field start)
            case "$start" in ''|*[!0-9]*) ;; *) age=$((now - start)); agetxt="${age}s" ;; esac
        fi
        echo "held by pid ${pid} (${agetxt}, children: ${kids:-unknown}) — ${ann}"
    done
    [ "$n" -gt 0 ] || echo "held by pid unknown (no visible process has the lock file open) — no announcement"
}

cmd="${1:-}"; shift || true
case "$cmd" in
    status)
        [ -e "$LOCK" ] || { echo "free"; exit 0; }
        if ! why=$(probe_open "<"); then cannot_open "$why"; exit 3; fi
        if lock_is_held; then describe; else echo "free"; fi
        exit 0 ;;
    run) ;;
    *) echo "usage: $0 run --agent <id> --issue <n> -- <command…> | status" >&2; exit 2 ;;
esac

AGENT=""; ISSUE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --agent) [ $# -ge 2 ] || { echo "--agent needs a value" >&2; exit 2; }; AGENT="$2"; shift 2 ;;
        --issue) [ $# -ge 2 ] || { echo "--issue needs a value" >&2; exit 2; }; ISSUE="$2"; shift 2 ;;
        --) shift; break ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
{ [ -n "$AGENT" ] && [ -n "$ISSUE" ] && [ $# -gt 0 ]; } || { echo "run needs --agent, --issue and a command" >&2; exit 2; }

# A failed open must never look like "held": tell it apart by the open itself.
# (A failed `exec 9>>` would otherwise end this script under `set -e`.)
if ! why=$(probe_open ">>"); then cannot_open "$why"; exit 3; fi
if ! { exec 9>>"$LOCK"; } 2>/dev/null; then cannot_open "the open failed after the probe succeeded"; exit 3; fi

# Cleanup first, so no window exists in which a signal finds the default
# disposition: release() only removes what is ours (the announcement when its pid
# is $$, our own temp file), so it is safe before the lock is taken and before
# the announcement exists. fwd() forwards to the command once there is one; before
# that there is nothing to forward to, so the signal ends the wrapper at once with
# 128+sig (the EXIT trap runs release).
got=""; child=""
release() { [ "$(info_field pid)" = "$$" ] && rm -f "$INFO" 2>/dev/null; rm -f "${INFO}.$$" 2>/dev/null; return 0; }
fwd() {
    got="$1"
    if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || true; else exit "$1"; fi
}
trap 'release' EXIT
trap 'fwd 130' INT
trap 'fwd 143' TERM
trap 'fwd 129' HUP

frc=0
flock -n -E 75 9 || frc=$?
case "$frc" in
    0) ;;
    75)  # drop our own fd first: our helper subshells would inherit it and show up as holders
        exec 9>&-
        echo "box is locked:"; describe; exit 75 ;;
    *) echo "flock failed with exit ${frc} on ${LOCK}" >&2; exit 4 ;;
esac

# Announcement: complete-or-absent (tmp + rename), world-readable, removed on
# release. Best effort: a sticky directory can refuse a foreign leftover.
announce() {
    local tmp="${INFO}.$$"
    {
        printf 'agent=%s\nissue=%s\npid=%s\nstart=%s\nstarted=%s\n' \
            "$AGENT" "$ISSUE" "$$" "$(date +%s)" "$(date -Iseconds)"
    } > "$tmp" 2>/dev/null && chmod 0644 "$tmp" && mv -f "$tmp" "$INFO" 2>/dev/null \
        || { rm -f "$tmp" 2>/dev/null; echo "warning: could not write ${INFO}; the lock is held but unannounced" >&2; }
}
announce

# Test-only hook (#1428): while the file named by RZFZ_BOX_LOCK_TEST_PAUSE_AFTER_ANNOUNCE
# exists, wait right after the announcement, so a test can deliver a signal exactly
# there. Unset in production: a no-op.
if [ -n "${RZFZ_BOX_LOCK_TEST_PAUSE_AFTER_ANNOUNCE:-}" ]; then
    while [ -e "$RZFZ_BOX_LOCK_TEST_PAUSE_AFTER_ANNOUNCE" ]; do sleep 0.05; done
fi

# The command runs with fd 9 closed: an orphan (tee, daemon) must not keep the
# box locked after we exit. It runs in the background so that a signal to this
# wrapper is seen at once (the trap forwards SIGTERM; an async child has SIGINT
# ignored, so INT is converted to TERM too) and the announcement is removed.
"$@" 9>&- <&0 &
child=$!
rc=0
while :; do
    wait "$child" && rc=0 || rc=$?
    kill -0 "$child" 2>/dev/null || break
done
[ -z "$got" ] || rc="$got"
exit "$rc"
