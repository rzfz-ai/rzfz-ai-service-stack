#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/lib-journal.sh — structured upgrade journal (#143 A+B)
# =============================================================================
# Adds a machine-readable (JSON-lines) record of an upgrade run ON TOP OF the
# human-readable log that scripts/lib.sh already produces via $RAZZFAZZ_LOG_FILE
# (every print_* call appends an ANSI-stripped line there).
#
# Goal: when a tester's upgrade breaks, there is a deterministic befund —
# every step + timing + pre/post version + the exact break point + the failing
# container's logs — instead of guessing from a blank "Server Error".
#
# Opt-in: until journal_init runs, every helper here is a silent no-op, so
# sourcing this file changes nothing for callers that don't use it.
#
# Layout (under ./backups/logs/):
#   upgrade-journal-<run_id>.log    human-readable   (via lib.sh RAZZFAZZ_LOG_FILE)
#   upgrade-journal-<run_id>.jsonl  structured       (this file)
#
# Survives a failed/rolled-back upgrade: every event is appended immediately
# (line-buffered, never only-at-success), and journal_finalize runs from an
# EXIT trap.
# =============================================================================

# _journal_esc: minimal JSON string escaping (backslash, quote, control chars).
_journal_esc() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/ /g' | tr -d '\000-\037'
}

# _journal_now: ISO-8601 timestamp (shell date is fine here — not the JS sandbox).
_journal_now() { date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z'; }

# journal_init <dir> <run_id> [k=v ...]
#   - sets RAZZFAZZ_JOURNAL_JSON (structured log — also auto-fed by lib.sh
#     _log_to_file, so every print_* becomes a JSON event too)
#   - records the retained per-run human-log path (filled at finalize from the
#     caller's existing $RAZZFAZZ_LOG_FILE — we do NOT repoint it, so the
#     script's .upgrade.log behaviour is preserved)
#   - writes the metadata header line
journal_init() {
    local dir=$1 run_id=$2; shift 2 || true
    # Pick a WRITABLE journal dir. The preferred dir (e.g. ./backups/logs) is
    # often root-owned on a live box (the backup container writes it), while the
    # upgrade runs as the operator — so fall back to where .upgrade.log already
    # lives (next to RAZZFAZZ_LOG_FILE), then /tmp. If none is writable, leave
    # the journal disabled (every helper no-ops) rather than spam errors.
    local fallback1 fallback2 d ok=""
    fallback1="$(dirname "${RAZZFAZZ_LOG_FILE:-/tmp/x}")/.upgrade-journals"
    fallback2="/tmp/razzfazz-upgrade-journals"
    for d in "$dir" "$fallback1" "$fallback2"; do
        mkdir -p "$d" 2>/dev/null || true
        if { : >> "${d}/.jwtest.$$"; } 2>/dev/null; then
            rm -f "${d}/.jwtest.$$" 2>/dev/null || true
            ok="$d"; break
        fi
    done
    [[ -z "$ok" ]] && return 0   # nowhere writable → stay disabled
    export RAZZFAZZ_JOURNAL_JSON="${ok}/upgrade-journal-${run_id}.jsonl"
    export RAZZFAZZ_JOURNAL_HUMAN="${ok}/upgrade-journal-${run_id}.log"
    : > "$RAZZFAZZ_JOURNAL_JSON" 2>/dev/null || true
    local line
    line="{\"ts\":\"$(_journal_now)\",\"event\":\"init\",\"run_id\":\"$(_journal_esc "$run_id")\""
    local kv k v
    for kv in "$@"; do
        k=${kv%%=*}; v=${kv#*=}
        line="${line},\"$(_journal_esc "$k")\":\"$(_journal_esc "$v")\""
    done
    line="${line}}"
    printf '%s\n' "$line" >> "$RAZZFAZZ_JOURNAL_JSON" 2>/dev/null || true
}

# journal_active: true iff a journal is open.
journal_active() { [[ -n "${RAZZFAZZ_JOURNAL_JSON:-}" ]]; }

# journal_event <phase> <status> <message>   (status: start|ok|warn|fail|info)
journal_event() {
    journal_active || return 0
    printf '{"ts":"%s","phase":"%s","status":"%s","msg":"%s"}\n' \
        "$(_journal_now)" "$(_journal_esc "${1:-}")" "$(_journal_esc "${2:-info}")" \
        "$(_journal_esc "${3:-}")" >> "$RAZZFAZZ_JOURNAL_JSON" 2>/dev/null || true
}

# journal_capture_failure <failing_container>
#   On failure, append the last 60 log lines of the container that broke, so the
#   befund travels with the journal (no second round-trip to the box needed).
journal_capture_failure() {
    journal_active || return 0
    local c=$1
    [[ -z "$c" ]] && return 0
    journal_event "capture" "info" "capturing last logs of ${c}"
    {
        printf '\n===== FAILING CONTAINER: %s =====\n' "$c"
        docker logs --tail 60 "$c" 2>&1 || echo "(could not read logs for $c)"
    } >> "${RAZZFAZZ_LOG_FILE:-/dev/null}" 2>/dev/null || true
}

# journal_finalize <result>   (result: success|failure|rolled-back)
#   Retains a per-run copy of the human log (the script's rolling .upgrade.log,
#   which would otherwise be overwritten next run) next to the JSON.
journal_finalize() {
    journal_active || return 0
    journal_event "finalize" "${1:-unknown}" "upgrade ${1:-unknown}"
    if [[ -n "${RAZZFAZZ_JOURNAL_HUMAN:-}" && -n "${RAZZFAZZ_LOG_FILE:-}" && -f "${RAZZFAZZ_LOG_FILE}" ]]; then
        cp -f "$RAZZFAZZ_LOG_FILE" "$RAZZFAZZ_JOURNAL_HUMAN" 2>/dev/null || true
    fi
}
