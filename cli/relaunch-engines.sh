#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz relaunch-engines — put this box's LLM engines on the release's runner
# and hardening (#3293)
#
#   rzfz relaunch-engines            switch the engines that need it
#   rzfz relaunch-engines --check    say which would be switched, change nothing
#
# An engine container keeps the image and the container settings it was created
# with. A new release changes both — the runner build (runners.yaml) and the
# least-privilege settings the worker agent launches with (non-root, no
# capabilities, read-only root, pids and memory limits) — but they reach an
# engine only when it is started again, and nothing starts a serving engine
# again on its own. Measured on an upgraded box: four engines created by the
# previous release survived two upgrades on the old runner, as root, with every
# capability and a writable root.
#
# The switch is the LLM Manager's runner upgrade, the one the console offers
# under Fleet -> <worker> -> Runner images: per worker, the image is pulled
# first (nothing is touched if that fails), each engine is started again on it
# — next to the old one when the weights fit twice, otherwise in its place —
# and must report healthy and be routable before the switch counts as done. An
# engine that does not come up is put back on the runner it ran before. So a
# failed switch leaves the models served, and this script says so and exits
# non-zero.
#
# What it touches: the worker agent of THIS box (the engines `docker inspect`
# can see here, measured against what the agent says it launches with today).
# Remote workers are named with the console action, not switched: the master
# cannot see their containers, nor which runner build their GPU needs.
#
# It never overrides an operator's runner pin: a deployment pinned to another
# runner stops the switch for that worker, and the pin is named. The switch
# pins every deployment it relaunches; the ones that followed the node's
# default before are unpinned afterwards, so they follow the next release too.
#
# Exit: 0 nothing left to do · 1 something is still not on the release runner
# or not hardened (refused, failed, pinned, unmanaged, still running) · 2 could
# not be measured (no manager, agent did not answer) · 3 a thin worker node.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
cd "$SCRIPT_DIR" || exit 1
[ "$PWD" = "$SCRIPT_DIR" ] || exit 1
# shellcheck source=../scripts/lib.sh
. "${SCRIPT_DIR}/scripts/lib.sh"
# shellcheck disable=SC2034  # read by _llmm_admin_api (the admin groups)
ENV_FILE="${SCRIPT_DIR}/.env"

POLL_S="${RZFZ_RELAUNCH_POLL_S:-10}"
# The manager's own deadlines are 30 min for the pull and 15 for the relaunch;
# this only bounds how long the caller waits for the verdict.
TIMEOUT_S="${RZFZ_RELAUNCH_TIMEOUT_S:-3000}"

usage() {
    cat <<EOF
rzfz relaunch-engines — move this box's LLM engines onto this release's runner
and engine hardening, through the LLM Manager's runner switch

Usage:
  rzfz relaunch-engines           switch the engines that are not on the release
                                  runner or not hardened (one worker at a time;
                                  an engine that does not come up is put back on
                                  its previous runner, and this exits non-zero)
  rzfz relaunch-engines --check   list them and change nothing

'rzfz upgrade' runs this at its end unless --no-engine-relaunch is given.
Remote workers are listed with the console action (Fleet -> worker -> Runner
images); they are not switched from here.
EOF
}

CHECK=false
while [ $# -gt 0 ]; do
    case "$1" in
        --check|--dry-run) CHECK=true ;;
        --from-upgrade)    : ;;
        -h|--help)         usage; exit 0 ;;
        *) echo "rzfz relaunch-engines: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

refuse_on_worker_node "relaunch-engines" "Its engines are switched from the master: run 'rzfz relaunch-engines' there, or use Fleet -> this worker -> Runner images in the LLM Manager console."

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

running="$(docker ps --format '{{.Names}}' 2>/dev/null)" || {
    print_error "docker did not answer — nothing was measured or changed."
    exit 2
}
_running() {
    case $'\n'"$running"$'\n' in *$'\n'"$1"$'\n'*) return 0 ;; esac
    return 1
}

if ! _running llm-worker-agent; then
    print_info "No LLM worker agent runs on this box — there is no engine to relaunch."
    exit 0
fi

expect="$(razzfazz_engine_expectation)" || expect=""
if [ -z "$expect" ]; then
    print_warning "llm-worker-agent did not say which runner and settings it launches engines with — nothing was changed."
    exit 2
fi
printf '%s' "$expect" > "$WORK/expect.json"
IFS=$'\t' read -r WANT REF WORKER < <(python3 -c '
import json, sys
e = json.load(open(sys.argv[1]))
print("%s\t%s\t%s" % (e.get("image") or "", e.get("ref") or "", e.get("worker") or ""))
' "$WORK/expect.json") || true
if [ -z "$WANT" ] || [ -z "$REF" ] || [ -z "$WORKER" ]; then
    print_warning "llm-worker-agent gave no runner image or worker name — nothing was changed."
    exit 2
fi

# STALE_LINES: "<name>\t<image>\t<reasons>" per engine that needs a relaunch.
STALE_LINES=""
N_OK=0
measure() {
    local names inspect verdict kind name img why
    STALE_LINES=""
    N_OK=0
    names="$(docker ps --filter label=rzfz.role=llm-engine --format '{{.Names}}' 2>/dev/null)" || return 1
    names="$(printf '%s\n' "$names" | sed '/^$/d')"
    [ -n "$names" ] || return 0
    # shellcheck disable=SC2086  # one container name per word
    inspect="$(docker inspect $names 2>/dev/null)" || return 1
    verdict="$(printf '%s' "$inspect" | razzfazz_engine_runtime_verdict "$expect")" || return 1
    while IFS=$'\t' read -r kind name img why; do
        case "$kind" in
            OK)    N_OK=$((N_OK + 1)) ;;
            STALE) STALE_LINES="${STALE_LINES}${name}"$'\t'"${img}"$'\t'"${why}"$'\n' ;;
        esac
    done <<< "$verdict"
    return 0
}

print_stale() {
    local name img why
    while IFS=$'\t' read -r name img why; do
        if [ -n "$name" ]; then print_info "    ${name}: ${why}"; fi
    done <<< "$STALE_LINES"
    return 0
}

if ! measure; then
    print_warning "Could not inspect the engine containers on this box — nothing was changed."
    exit 2
fi

# The manager's view: needed for the switch, and for the remote report.
MANAGER_UP=false
if _running llm-manager; then
    # shellcheck source=lib-llm-manager-deploy.sh
    . "${SCRIPT_DIR}/cli/lib-llm-manager-deploy.sh"
    if _llmm_admin_api GET /api/workers > "$WORK/workers.json" 2>/dev/null \
       && _llmm_admin_api GET /api/deployments > "$WORK/deployments.json" 2>/dev/null; then
        MANAGER_UP=true
    fi
fi

# What the manager knows about this worker and the others, in one pass:
#   WORKER <id> <status>            this box's worker (NOWORKER when absent)
#   UNMANAGED <container>           a stale engine no instance of it names
#   PIN <dep> <model> <pin>         an operator pin on another runner
#   UNPIN <dep> <model>             follows the node default today
#   REMOTE <worker> <model> <ran> <default>
plan() {
    STALE_NAMES="$(printf '%s' "$STALE_LINES" | cut -f1)" WANT="$WANT" WORKER="$WORKER" \
        EXPERIMENTAL="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("experimental_image") or "")' "$WORK/expect.json" 2>/dev/null || true)" \
        python3 -c '
import json, os, sys
def leaf(ref):
    ref = str(ref or "").split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    return last if ":" in last else last + ":latest"
def rows(path):
    d = json.load(open(path))
    if isinstance(d, dict):
        d = d.get("workers") or d.get("items") or []
    return [r for r in d if isinstance(r, dict)]
workers, deps = rows(sys.argv[1]), rows(sys.argv[2])
local, want = os.environ["WORKER"], leaf(os.environ["WANT"])
# #3403: a pin to the experimental runner of this release is not "another runner":
# the switch keeps it (the manager does not repin it), so it is neither a
# refusal (PIN) nor a pin to clear afterwards (UNPIN).
experimental = leaf(os.environ["EXPERIMENTAL"]) if os.environ.get("EXPERIMENTAL") else None
stale = [n for n in os.environ.get("STALE_NAMES", "").split("\n") if n]
me = next((w for w in workers if w.get("name") == local), None)
if me is None:
    print("NOWORKER")
else:
    print("WORKER\t%s\t%s" % (me.get("id"), me.get("status") or ""))
    known = {str(i.get("container") or "") for i in (me.get("instances") or []) if isinstance(i, dict)}
    for n in stale:
        if n not in known:
            print("UNMANAGED\t" + n)
    for d in deps:
        on_me = any(isinstance(i, dict) and (str(i.get("worker_id") or "") == str(me.get("id"))
                                             or i.get("worker") == local)
                    for i in (d.get("instances") or []))
        if not on_me:
            continue
        pin = d.get("runner_image")
        model = d.get("model_name") or d.get("id")
        if not pin:
            print("UNPIN\t%s\t%s" % (d.get("id"), model))
        elif experimental and leaf(pin) == experimental:
            continue
        elif leaf(pin) != want:
            print("PIN\t%s\t%s\t%s" % (d.get("id"), model, pin))
for w in workers:
    if w is me or w.get("external") or w.get("agent_managed"):
        continue
    default = str(w.get("engine_version") or "")
    for i in (w.get("instances") or []):
        ran = str((i or {}).get("engine_version") or "")
        if default and ran and ran != default:
            print("REMOTE\t%s\t%s\t%s\t%s" % (w.get("display_name") or w.get("name") or "?",
                                              i.get("model_name") or "?", ran, default))
' "$WORK/workers.json" "$WORK/deployments.json"
}

report_remote() {
    local kind a b c d any=false
    while IFS=$'\t' read -r kind a b c d; do
        [ "$kind" = REMOTE ] || continue
        if [ "$any" = false ]; then
            print_warning "Engines on remote workers run another build than their worker's default runner — they are not switched from here:"
            any=true
        fi
        print_info "    worker ${a}: ${b} runs ${c}, the worker's default is ${d}"
    done <<< "$PLAN"
    if [ "$any" = true ]; then
        print_info "    Switch each from the LLM Manager console: Fleet -> <worker> -> Runner images -> Upgrade (or update that worker first: rzfz node-upgrade on it)."
    fi
    return 0
}

PLAN=""
if [ "$MANAGER_UP" = true ]; then
    PLAN="$(plan)" || PLAN=""
fi

if [ -z "$STALE_LINES" ]; then
    if [ "$N_OK" -gt 0 ]; then
        print_success "All ${N_OK} engine(s) on this box run ${WANT} with this release's engine settings — nothing to relaunch."
    else
        print_info "No engine runs on this box — nothing to relaunch; new engines start on ${WANT}."
    fi
    report_remote
    exit 0
fi

print_step "Engines on this box that are not on ${WANT} or not hardened (they keep what they were created with until relaunched):"
print_stale

if [ "$MANAGER_UP" != true ]; then
    print_warning "The LLM Manager did not answer (or llm-manager is not running) — the engines cannot be switched now. Start it, then run: rzfz relaunch-engines"
    exit 2
fi

rc=0
N_UNMANAGED=0
WID=""
WSTATUS=""
UNPIN_IDS=""
PINNED=false
while IFS=$'\t' read -r kind a b c _; do
    case "$kind" in
        NOWORKER)
            print_warning "The LLM Manager lists no worker named '${WORKER}' — these engines are not its own and cannot be switched by it."
            rc=1 ;;
        WORKER)    WID="$a"; WSTATUS="$b" ;;
        UNMANAGED)
            print_warning "Engine ${a} is not one the LLM Manager supervises — a runner switch does not reach it. If it is not yours, remove it: docker rm -f ${a}"
            N_UNMANAGED=$((N_UNMANAGED + 1))
            rc=1 ;;
        PIN)
            print_warning "Deployment ${b} is pinned to runner ${c} — set by an operator, so it is not replaced."
            PINNED=true ;;
        UNPIN)     UNPIN_IDS="${UNPIN_IDS} ${a}" ;;
    esac
done <<< "$PLAN"

if [ -z "$WID" ] || [ "$N_UNMANAGED" -ge "$(printf '%s' "$STALE_LINES" | grep -c .)" ]; then
    report_remote
    exit 1
fi
if [ "$PINNED" = true ]; then
    print_info "    The switch would repin every deployment on '${WORKER}' to ${REF}. Clear or change the pin in the console (Deployments -> the model -> Runner), or switch the worker there (Fleet -> ${WORKER} -> Runner images), then run: rzfz relaunch-engines"
    report_remote
    exit 1
fi
if [ "$WSTATUS" = "draining" ]; then
    print_warning "Worker '${WORKER}' is draining — no engine is placed on it, so nothing is switched. Its engines go when the drain completes."
    report_remote
    exit 1
fi
if [ "$CHECK" = true ]; then
    print_info "Would switch worker '${WORKER}' to ${REF} (--check: nothing was changed)."
    report_remote
    exit 1
fi

# #3375: the pull below is the host daemon's; on TLS_MODE=internal it must
# trust Caddy's root for the hub first, or it ends in x509.
rzfz_trust_box_hub_ca "$ENV_FILE" || true
print_substep "Switching worker '${WORKER}' to ${REF} through the LLM Manager (pull first; each engine must be healthy and routable before the old one goes)..."
body="$(REF="$REF" python3 -c 'import json, os; print(json.dumps({"image": os.environ["REF"]}))')"
start_rc=0
start="$(LLMM_POST_TIMEOUT=60 _llmm_admin_api POST "/api/workers/${WID}/upgrade-runner" "$body" 2>&1)" || start_rc=$?
UPID=""
if [ "$start_rc" -eq 0 ]; then
    UPID="$(printf '%s' "$start" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("upgrade_id") or "")' 2>/dev/null)" || UPID=""
fi
if [ -z "$UPID" ]; then
    print_error "The LLM Manager did not start the runner switch: ${start}"
    print_info "    Nothing on the node was changed; the engines keep serving on their current runner."
    report_remote
    exit 1
fi

deadline=$(( $(date +%s) + TIMEOUT_S ))
state=""
seen=""
error=""
while :; do
    st="$(_llmm_admin_api GET "/api/runner-upgrades/${UPID}" 2>/dev/null)" || st=""
    if [ -n "$st" ]; then
        IFS=$'\t' read -r state error < <(printf '%s' "$st" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("%s\t%s" % (d.get("state") or "", str(d.get("error") or "").replace("\n", " ").replace("\t", " ")))
' 2>/dev/null) || true
        if [ -n "$state" ] && [ "$state" != "$seen" ]; then
            print_substep "runner switch: ${state}"
            seen="$state"
        fi
        case "$state" in done|failed|rolled_back) break ;; esac
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        print_warning "The runner switch on '${WORKER}' is still '${state:-unknown}' after ${TIMEOUT_S}s. The LLM Manager carries on by itself; follow it in the console (Fleet -> ${WORKER} -> Runner images) and run 'rzfz status' when it ends."
        report_remote
        exit 1
    fi
    sleep "$POLL_S"
done

if [ "$state" != "done" ]; then
    print_error "The runner switch on '${WORKER}' ended '${state}': ${error:-no reason recorded}"
    print_info "    Every engine that did not come up was put back on its previous runner by the LLM Manager, and those deployments stay pinned to it. Fix the cause, then run: rzfz relaunch-engines"
    report_remote
    exit 1
fi

for id in $UNPIN_IDS; do
    if ! _llmm_admin_api PATCH "/api/deployments/${id}" '{"runner_image": null}' >/dev/null 2>&1; then
        print_warning "Deployment ${id} stays pinned to ${REF} (the switch pins what it relaunches; clearing it failed). It serves correctly; clear the pin in the console so it follows the next release's runner."
    fi
done

if ! measure; then
    print_warning "The switch is done, but the engines could not be inspected again — check 'rzfz status'."
    exit 1
fi
if [ -n "$STALE_LINES" ]; then
    print_error "The runner switch is done, but these engines are still not on ${WANT} or not hardened:"
    print_stale
    rc=1
else
    print_success "${N_OK} engine(s) now run ${WANT} with this release's engine settings."
fi
report_remote
exit "$rc"
