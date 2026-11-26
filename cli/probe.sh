#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz probe — run the acceptance probes the Configuration Portal queued (#1223)
# ==============================================================================
# Usage:
#   rzfz probe --pending            # run every queued request, oldest first
#   rzfz probe <module> [<module>…] # run these acceptance probes directly
#   rzfz probe --list               # show queued requests without running them
#   rzfz probe --help               # this text
#
# WHY THIS EXISTS (operator decision E4, #1223 / #979): the Portal container
# mounts the repo read-only (BSB-03), so the in-container probe runner died at
# its first `mkdir tests/results/…` and reported SKIPPED on every hardened box.
# The Portal now only QUEUES a request; this command is the host side that runs
# it. Nothing here needs the Portal — a queued request is a JSON file.
#
# Exit codes: 0 = every probe passed (or nothing was queued), 1 = a probe
# failed, 2 = usage error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Every cli/ entrypoint runs from the stack root (tests/unit/consistency/
# test_cli_entrypoints_cd_to_stack_root.py): `rzfz` execs without changing
# directory, cli/test.sh resolves module paths relative to the cwd, and an
# operator may call this from anywhere.
cd "$SCRIPT_DIR"
PROBE_ROOT="$SCRIPT_DIR"
QUEUE_DIR="${RAZZFAZZ_PROBE_QUEUE:-$PROBE_ROOT/.probe-queue}"
RUNNER="${RAZZFAZZ_PROBE_RUNNER:-$PROBE_ROOT/cli/test.sh}"

# #266 (E7): for rzfz_node_role / refuse_on_worker_node — see probe_main.
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/scripts/lib.sh"

# #1223 review, finding 2: #1190's readiness wait. `wait_for_containers_ready`
# lives in core/config/app/services/post_toggle_probe.py — stdlib-only, so it
# loads straight from the file without the Portal's app package. Waiting HERE
# is what the design intends: the toggle is not delayed, and the probe measures
# containers that have actually come up.
_probe_wait_ready() {
    local profile="$1" svcs="$2"
    [ -n "$svcs" ] || { echo "[probe] no services recorded in the request — measuring immediately (queued before #1223 rev-B?)" >&2; return 0; }
    PROBE_PROFILE="$profile" PROBE_SERVICES="$svcs" PROBE_ROOT="$PROBE_ROOT" python3 - <<'PYEOF'
import importlib.util, os, sys
from pathlib import Path

root = Path(os.environ["PROBE_ROOT"])
mod_path = root / "core" / "config" / "app" / "services" / "post_toggle_probe.py"
spec = importlib.util.spec_from_file_location("rzfz_post_toggle_probe", mod_path)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except Exception as exc:                       # never block the probe on this
    print(f"[probe] readiness wait unavailable ({exc!r}) — measuring now", file=sys.stderr)
    sys.exit(0)

services = [s for s in os.environ["PROBE_SERVICES"].splitlines() if s.strip()]
r = mod.wait_for_containers_ready(os.environ["PROBE_PROFILE"], services, stack_root=root)
if r.get("unknown_reason"):
    print(f"[probe] readiness unknown: {r['unknown_reason']} — measuring now")
elif r.get("timed_out"):
    print(f"[probe] readiness timed out after {r['waited_ms']} ms; not ready: "
          f"{[n.get('service') for n in r.get('not_ready') or []]} — measuring anyway")
else:
    print(f"[probe] containers ready after {r['waited_ms']} ms "
          f"({r['polls']} poll(s)): {', '.join(r.get('ready') or [])}")
PYEOF
}

probe_usage() {
    sed -n '/^# Usage:/,/^[^#]/p' "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \{0,1\}//'
}

_probe_json_field() {
    # $1 = file, $2 = key → value(s), one per line. python3 is a hard
    # dependency of the stack (post-install, the portal, the generators).
    PROBE_F="$1" PROBE_K="$2" python3 - <<'PYEOF'
import json, os, sys
try:
    d = json.load(open(os.environ["PROBE_F"], encoding="utf-8"))
except Exception:
    sys.exit(3)
v = d.get(os.environ["PROBE_K"])
if isinstance(v, list):
    print("\n".join(str(x) for x in v))
elif v is not None:
    print(v)
PYEOF
}

_probe_write_result() {
    # $1 = result file, $2 = request id, $3 = overall (true|false),
    # $4 = targets (newline-separated), $5 = per-target verdict lines,
    # $6 = optional refusal reason (no targets ran)
    PROBE_OUT="$1" PROBE_ID="$2" PROBE_OK="$3" PROBE_TARGETS="$4" PROBE_ROWS="$5" \
        PROBE_REASON="${6:-}" python3 - <<'PYEOF'
import json, os, time
rows = []
for line in (os.environ["PROBE_ROWS"] or "").splitlines():
    if not line.strip():
        continue
    target, rc, ms = line.split("|", 2)
    rows.append({"target": target, "pass": rc == "0", "exit_code": int(rc),
                 "duration_ms": int(ms)})
out = {
    "id": os.environ["PROBE_ID"],
    "ran_at": int(time.time()),
    "ran_by": "rzfz probe",
    "overall_pass": os.environ["PROBE_OK"] == "true",
    "targets": rows,
}
# #1223 review, finding 3: a refused request still gets an answer — a silent
# missing result file is indistinguishable from "not run yet".
if os.environ.get("PROBE_REASON"):
    out["refused_reason"] = os.environ["PROBE_REASON"]
tmp = os.environ["PROBE_OUT"] + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=2)
    fh.write("\n")
os.replace(tmp, os.environ["PROBE_OUT"])
PYEOF
}

# _probe_run_targets <target…> → 0 when all passed. Prints one line per target.
# Milliseconds since the epoch, without trusting `date +%3N`.
#
# #1223 review, finding 4: on Ubuntu 26.04 `date` is uutils coreutils 0.8.0,
# which does not honour the field width — `date +%3N` returns NINE digits and
# `date +%s%3N` nineteen. The difference was therefore in NANOseconds while the
# field was called `duration_ms`: off by ~10⁶, and no test looked at the
# magnitude. bash's own EPOCHREALTIME has no such variance; `date` stays as the
# fallback for a shell without it, and there the nanoseconds are divided down.
_probe_now_ms() {
    if [ -n "${EPOCHREALTIME:-}" ]; then
        local whole frac
        whole="${EPOCHREALTIME%%[.,]*}"
        frac="${EPOCHREALTIME#*[.,]}"
        frac="${frac}000000"
        printf '%s' "$(( whole * 1000 + 10#${frac:0:3} ))"
        return 0
    fi
    local ns
    ns="$(date +%s%N 2>/dev/null || echo 0)"
    case "$ns" in
        ''|*[!0-9]*) printf '0' ;;
        *)           printf '%s' "$(( ns / 1000000 ))" ;;
    esac
}

_probe_run_targets() {
    local rc_all=0 t start end rc ms
    PROBE_ROWS=""
    for t in "$@"; do
        [ -n "$t" ] || continue
        echo "[probe] $t: running acceptance probe..."
        start=$(_probe_now_ms)
        rc=0
        "$RUNNER" "$t" --acceptance --no-coverage || rc=$?
        end=$(_probe_now_ms)
        ms=$(( end > start ? end - start : 0 ))
        if [ "$rc" = "0" ]; then
            echo "[probe] $t: PASS (${ms} ms)"
        else
            echo "[probe] $t: FAIL (exit $rc, ${ms} ms)" >&2
            rc_all=1
        fi
        PROBE_ROWS+="${t}|${rc}|${ms}"$'\n'
    done
    return $rc_all
}

_probe_pending_files() {
    # Requests without a result, oldest first (the id starts with a UTC stamp).
    local f id
    [ -d "$QUEUE_DIR" ] || return 0
    for f in "$QUEUE_DIR"/*.request.json; do
        [ -e "$f" ] || continue
        id="$(basename "$f" .request.json)"
        [ -e "$QUEUE_DIR/$id.result.json" ] && continue
        printf '%s\n' "$f"
    done | sort
}

probe_main() {
    local mode="" targets=()
    case "${1:-}" in
        -h|--help|"") probe_usage; return 0 ;;
        --pending) mode=pending ;;
        --list)    mode=list ;;
        -*) echo "rzfz probe: unknown argument: $1" >&2; probe_usage >&2; return 2 ;;
        *) mode=direct; targets=("$@") ;;
    esac
    # #266 (E7): the fifth of the five stack-bound verbs. The other four
    # (security-check, provision-sso-users, set-admin-password, rotate-secret)
    # already say what this box is instead of reading into nothing; probe was
    # named in the same cut and left out, which is worse than never naming it.
    # What it probes is the stack — Caddy, the portals, a module's containers —
    # and the queue it drains is written by the Configuration Portal, which
    # does not run here. On a worker `--pending` would answer "nothing queued"
    # (true, and useless) and a direct target would hand cli/test.sh a module
    # this box does not have. Placed AFTER the --help branch so help renders
    # anywhere, and after argument validation so a typo is still a usage error.
    refuse_on_worker_node probe "The Portal that queues these probes, and the stack they measure, live on the master."
    if [ "$mode" = "direct" ]; then
        [ -x "$RUNNER" ] || { echo "rzfz probe: runner not found: $RUNNER" >&2; return 2; }
        _probe_run_targets "${targets[@]}"
        return $?
    fi

    local files rc_all=0 count=0
    files="$(_probe_pending_files)"
    if [ -z "$files" ]; then
        echo "[probe] no queued acceptance probes in $QUEUE_DIR"
        return 0
    fi
    if [ "$mode" = "list" ]; then
        local f
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            echo "[probe] queued: $(basename "$f" .request.json) → $(_probe_json_field "$f" targets | tr '\n' ' ')"
        done <<< "$files"
        return 0
    fi

    [ -x "$RUNNER" ] || { echo "rzfz probe: runner not found: $RUNNER" >&2; return 2; }
    local f id tlist ok
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        id="$(basename "$f" .request.json)"
        tlist="$(_probe_json_field "$f" targets || true)"
        if [ -z "$tlist" ]; then
            echo "[probe] $id: no targets in the request — skipped" >&2
            continue
        fi
        count=$((count + 1))
        local _profile
        _profile="$(_probe_json_field "$f" profile || echo '?')"
        echo "[probe] request $id (profile ${_profile})"
        _probe_wait_ready "$_profile" "$(_probe_json_field "$f" services || true)"
        ok=true
        # #1223 review, finding 3: this used to be `_probe_run_targets $tlist`,
        # unquoted. The file it comes from is written by the CONTAINER, and
        # probe.sh has already `cd`'d to the stack root, so both word splitting
        # AND globbing applied: a `targets` value of `dify_api --ci-mode *`
        # expanded to 36 arguments, among them every entry of the repo root.
        # Not RCE — the runner maps a name to a subtree under tests/ — but it is
        # argument injection into a host-side runner, steered from a file the
        # container writes, and #1505 treats "reached a Portal write path" as an
        # attacker by name. So: read into an array, and let nothing through that
        # is not a plain target name.
        local -a tvec=()
        local t bad=""
        while IFS= read -r t; do
            [ -n "$t" ] || continue
            case "$t" in
                *[!A-Za-z0-9_-]*) bad="$t"; break ;;
            esac
            tvec+=("$t")
        done <<< "$tlist"
        if [ -n "$bad" ]; then
            echo "[probe] $id: refused — target name is not [A-Za-z0-9_-]+: ${bad}" >&2
            _probe_write_result "$QUEUE_DIR/$id.result.json" "$id" false "$tlist" "" \
                "illegal target name (must be [A-Za-z0-9_-]+)"
            rc_all=1
            continue
        fi
        if [ "${#tvec[@]}" -eq 0 ]; then
            echo "[probe] $id: no usable targets — skipped" >&2
            continue
        fi
        _probe_run_targets "${tvec[@]}" || { ok=false; rc_all=1; }
        _probe_write_result "$QUEUE_DIR/$id.result.json" "$id" "$ok" "$tlist" "$PROBE_ROWS"
        echo "[probe] $id: result written to $QUEUE_DIR/$id.result.json"
    done <<< "$files"
    echo "[probe] ran $count queued request(s)"
    return $rc_all
}

probe_main "$@"
