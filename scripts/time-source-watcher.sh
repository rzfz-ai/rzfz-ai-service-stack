#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# time-source-watcher.sh — apply a time source change the Settings portal queued
# =============================================================================
# Run as root by razzfazz-time-source.service when the systemd path unit sees
# <stack>/.probe-queue/time-source.pending. The portal container writes that file
# and is NOT trusted: the file is DATA. It is read as JSON by python, checked with
# the portal's own validator (core/config/app/services/time_source.py: one of the
# four modes, servers that are IP addresses or host names) and only the validated
# values reach scripts/apply-time-source.sh, as arguments and never through a
# shell. Anything else is rejected and logged, and nothing is run.
#
# Every run is logged to the journal (tag razzfazz-time-source), writes
# <stack>/.probe-queue/time-source.result (status=applied|failed|rejected, mode,
# reason, at) for the portal and `rzfz status`, and removes the queue file.
# The manual `sudo rzfz setup --time-source` stays as the fallback.
#
# Failure policy: set -e is on; a step that may fail says so with `|| ...`.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
QUEUE_DIR="${RAZZFAZZ_TIME_QUEUE_DIR:-${SCRIPT_DIR}/.probe-queue}"
PENDING="$QUEUE_DIR/time-source.pending"
RESULT="$QUEUE_DIR/time-source.result"
VALIDATOR="${SCRIPT_DIR}/core/config/app/services/time_source.py"
APPLY="${SCRIPT_DIR}/scripts/apply-time-source.sh"
MAX_BYTES=4096

log() {
    echo "razzfazz-time-source: $*"
    logger -t razzfazz-time-source -- "$*" 2>/dev/null || true   # the journal already has stdout; logger is a convenience
}

# clean <text> — one printable line, bounded, for the result file and the log.
clean() { printf '%s' "$1" | tr -cd '[:print:]' | cut -c1-200; }

write_result() {   # <status> <mode> <reason>
    local tmp
    tmp="$(mktemp "$QUEUE_DIR/.time-source.result.XXXXXX")" || { log "cannot write the result file in $QUEUE_DIR"; return 1; }
    printf 'status=%s\nmode=%s\nreason=%s\nat=%s\n' "$1" "$(clean "$2")" "$(clean "$3")" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$RESULT"
}

finish() {   # <status> <mode> <reason> <exit code>
    log "$1: mode=$(clean "$2") $(clean "$3")"
    write_result "$1" "$2" "$3" || log "the result could not be recorded"
    rm -f "$PENDING" || log "could not remove $PENDING"
    exit "$4"
}

[ -e "$PENDING" ] || [ -L "$PENDING" ] || exit 0

# A symlink or a device would make root read something the portal chose.
if [ -L "$PENDING" ] || [ ! -f "$PENDING" ]; then
    finish rejected "" "the request is not a plain file" 0
fi
size="$(stat -c %s "$PENDING")" || size=$((MAX_BYTES + 1))
if [ "$size" -gt "$MAX_BYTES" ]; then
    finish rejected "" "the request is larger than $MAX_BYTES bytes" 0
fi

# Parse and validate as data. Output: "OK<TAB>mode<TAB>servers" or "ERR<TAB>reason".
verdict="$(python3 -I -c '
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("time_source", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
try:
    with open(sys.argv[2], "rb") as fh:
        data = json.loads(fh.read(4096).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("the request is not a JSON object")
    mode = data.get("mode")
    servers = data.get("servers", [])
    if isinstance(servers, str):
        servers = [servers]
    if not isinstance(mode, str) or not isinstance(servers, list) or not all(isinstance(s, str) for s in servers):
        raise ValueError("mode and servers must be strings")
    mode, parsed, error = mod.validate(mode, " ".join(servers))
    if error:
        raise ValueError(error)
    print("OK\t%s\t%s" % (mode, " ".join(parsed)))
except Exception as exc:
    print("ERR\t%s" % str(exc).replace("\n", " ").replace("\t", " ")[:160])
' "$VALIDATOR" "$PENDING")" || verdict="ERR	the request could not be read"

IFS=$'\t' read -r kind v_mode v_servers <<<"$verdict"
if [ "$kind" != "OK" ]; then
    finish rejected "" "${v_mode:-the request is invalid}" 0
fi

args=(--mode "$v_mode")
if [ -n "$v_servers" ]; then args+=(--servers "$v_servers"); fi
log "applying mode=$v_mode servers=${v_servers:-none}"
if out="$(bash "$APPLY" "${args[@]}" 2>&1)"; then
    printf '%s\n' "$out"
    finish applied "$v_mode" "applied" 0
fi
printf '%s\n' "$out"
reason="$(printf '%s\n' "$out" | sed 's/\x1b\[[0-9;]*m//g' | grep -E '\[(ERROR|✗)\]|ERROR|did not|No time daemon' | tail -1)" || reason=""
finish failed "$v_mode" "${reason:-apply-time-source.sh failed}" 1
