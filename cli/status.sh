#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# razzfazz-status.sh — read-only assessment of stack + host posture
#
# Produces a structured PASS/WARN/FAIL/INFO/SKIP report covering: code state,
# running stack, image versions vs manifest, LLM profile coherence, network
# exposure, host hardening (ufw/fail2ban/auditd/sysctl/Docker daemon),
# secrets, SSH posture (informational), TLS, backups, and the latest audit
# report's open-finding count.
#
# No mutations. No sudo (gracefully degrades any check that needs root).
# Read-only on .env / manifests / docker / filesystem.
#
# Usage:
#   rzfz status                # full report, colored, exit 0/1
#   rzfz status --short        # one-line summary per category
#   rzfz status --json         # machine-parseable
#   rzfz status --no-color     # plain text (for log capture)
#
# Exit codes:
#   0 — all PASS or only WARN/INFO/SKIP
#   1 — at least one FAIL
#   2 — usage error
# =============================================================================
# Note: deliberately NOT using `set -u` — too aggressive in subshells that
# re-source the operator's shell snapshot (which may reference unset vars).
# We rely on explicit empty-checks instead.
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# `rzfz` execs this script without changing directory, and the CODE STATE block
# below only cds inside its "is a git repo" branch — so on a box installed from
# an offline package (no .git) every relative path resolved against the
# operator's cwd. cd unconditionally, like the other CLI entry points.
cd "$SCRIPT_DIR" || true
ENV_FILE="$SCRIPT_DIR/.env"
MANIFEST_FILE="$SCRIPT_DIR/config/manifests/versions.json"
HARDENING_MARKER="/etc/razzfazz/host-hardened"

# M026 / S02 #3: source the shared library for color constants. We keep the
# script-local single-letter aliases (R/G/Y/B/N) so the existing $COLOR toggle
# (--no-color, --json) still works; D (dim/grey, 0;90m) stays local because
# lib.sh does not provide a dim equivalent.
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

# ---- Flags ------------------------------------------------------------------
SHORT=false
JSON=false
COLOR=true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --short)    SHORT=true; shift ;;
        --json)     JSON=true; COLOR=false; shift ;;
        --no-color) COLOR=false; shift ;;
        -h|--help)
            sed -n '2,/^# ===/p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

# ---- Output helpers ---------------------------------------------------------
if $COLOR; then
    R="$RED"; G="$GREEN"; Y="$YELLOW"; B="$BLUE"; D='\033[0;90m'; N="$NC"
else
    R=''; G=''; Y=''; B=''; D=''; N=''
fi

# Counters (per category we track items; per item we record status + msg)
declare -a CATEGORIES=()
declare -A ITEMS_PASS=() ITEMS_WARN=() ITEMS_FAIL=() ITEMS_INFO=() ITEMS_SKIP=()
declare -A LINES=()           # CATEGORY|N → "STATUS|message"
declare -A LINE_COUNT=()      # CATEGORY → N

PASS=0; WARN=0; FAIL=0; INFO=0; SKIP=0
CURRENT_CAT=""

cat_begin() {
    CURRENT_CAT="$1"
    CATEGORIES+=("$CURRENT_CAT")
    LINE_COUNT["$CURRENT_CAT"]=0
}
add_line() {
    local status="$1" msg="$2"
    local n=${LINE_COUNT["$CURRENT_CAT"]}
    LINES["${CURRENT_CAT}|${n}"]="${status}|${msg}"
    LINE_COUNT["$CURRENT_CAT"]=$((n + 1))
    case "$status" in
        PASS) PASS=$((PASS + 1)) ;;
        WARN) WARN=$((WARN + 1)) ;;
        FAIL) FAIL=$((FAIL + 1)) ;;
        INFO) INFO=$((INFO + 1)) ;;
        SKIP) SKIP=$((SKIP + 1)) ;;
    esac
}
pass() { add_line PASS "$1"; }
warn() { add_line WARN "$1"; }
fail() { add_line FAIL "$1"; }
info() { add_line INFO "$1"; }
skip() { add_line SKIP "$1"; }

# Safe .env read (no shell sourcing — operator-edited .env carries spaces and
# shell metachars; per memory feedback_dotenv_no_source).
env_get() {
    local key="$1"
    [ -f "$ENV_FILE" ] || { echo ""; return; }
    # "…" or '…' (compose's env_file takes both; #2584 review: a single-quoted
    # value was read with its quotes)
    grep "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' \
        | sed -e 's/^"//;s/"$//' -e "s/^'\(.*\)'\$/\1/"
}

# JSON string escaping (#1889). `printf %q` is SHELL quoting, not JSON: it
# emits `CODE\ STATE` for a name with a space, so `rzfz status --json` has never
# produced parseable JSON — `json.loads` stops at the first category. Anything
# consuming it either wrote its own parser or gave up quietly.
#
# Done in bash rather than shelling out: `rzfz status` runs on boxes where
# python3 is not guaranteed, and a status command that needs an interpreter to
# print its own output has the dependency the wrong way round.
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"      # backslash first, or it doubles the escapes below
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    # Any other C0 control character would still be invalid JSON; these messages
    # are plain prose, so drop them rather than emit something unparseable.
    s="$(printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037')"
    printf '"%s"' "$s"
}

# ============================================================================
# THE REPORT WRITER — #266 (E7): a function, so the worker path can render the
# same report and stop, instead of falling through the stack categories that do
# not exist on a thin inference node. Body unchanged; only indented.
#
# Defined here, CALLED at the bottom under the `# RENDER` banner — that banner
# is the end marker several guards slice against (test_179 reads the
# post-install section as "from its cat_begin to # RENDER"), so it stays where
# the checks end, not where the function is written.
# ============================================================================
render_report() {
    echo ""
    if $JSON; then
        # JSON output
        printf '{\n  "summary": {"pass": %d, "warn": %d, "fail": %d, "info": %d, "skip": %d},\n  "categories": [\n' \
            "$PASS" "$WARN" "$FAIL" "$INFO" "$SKIP"
        first_cat=true
        for cat in "${CATEGORIES[@]}"; do
            $first_cat && first_cat=false || printf ',\n'
            printf '    {"name": %s, "items": [' "$(json_escape "$cat")"
            n=${LINE_COUNT["$cat"]}
            first_item=true
            for ((i=0; i<n; i++)); do
                line="${LINES["${cat}|${i}"]}"
                status="${line%%|*}"; msg="${line#*|}"
                $first_item && first_item=false || printf ', '
                printf '{"status": "%s", "msg": %s}' "$status" "$(json_escape "$msg")"
            done
            printf ']}'
        done
        printf '\n  ]\n}\n'
        exit $([ "$FAIL" -gt 0 ] && echo 1 || echo 0)
    fi

    # Human-readable rendering
    host=$(hostname)
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    date_now=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "${B}${REPORT_TITLE:-rzfz.ai stack} — assessment ${date_now}${N}"
    echo -e "${D}host: $host ($ip)  user: $(id -un)${N}"
    echo ""

    for i in "${!CATEGORIES[@]}"; do
        cat="${CATEGORIES[$i]}"
        n=${LINE_COUNT["$cat"]}
        [ "$n" -eq 0 ] && continue
        num=$((i + 1))
        echo -e "${B}[$num] $cat${N}"
        if $SHORT; then
            # one-line summary per category
            c_pass=0; c_warn=0; c_fail=0; c_info=0; c_skip=0
            for ((j=0; j<n; j++)); do
                line="${LINES["${cat}|${j}"]}"
                status="${line%%|*}"
                case "$status" in
                    PASS) c_pass=$((c_pass+1));;
                    WARN) c_warn=$((c_warn+1));;
                    FAIL) c_fail=$((c_fail+1));;
                    INFO) c_info=$((c_info+1));;
                    SKIP) c_skip=$((c_skip+1));;
                esac
            done
            echo -e "  ${G}${c_pass}P${N} ${Y}${c_warn}W${N} ${R}${c_fail}F${N} ${B}${c_info}i${N} ${D}${c_skip}-${N}"
        else
            for ((j=0; j<n; j++)); do
                line="${LINES["${cat}|${j}"]}"
                status="${line%%|*}"; msg="${line#*|}"
                case "$status" in
                    PASS) printf "  ${G}✓${N} %s\n" "$msg" ;;
                    WARN) printf "  ${Y}!${N} %s\n" "$msg" ;;
                    FAIL) printf "  ${R}✗${N} %s\n" "$msg" ;;
                    INFO) printf "  ${B}i${N} %s\n" "$msg" ;;
                    SKIP) printf "  ${D}-${N} %s${N}\n" "$msg" ;;
                esac
            done
        fi
        echo ""
    done

    echo -e "${B}Summary:${N} ${G}${PASS} PASS${N}  ${Y}${WARN} WARN${N}  ${R}${FAIL} FAIL${N}  ${B}${INFO} info${N}  ${D}${SKIP} n/a${N}"
    [ "$FAIL" -gt 0 ] && exit 1 || exit 0
}

# ============================================================================
# #2392 — weights the node set aside because their bytes were not the ones the
# catalogue approved (`<name>.rejected-<sha12>` in the models volume). They are
# kept as evidence and never evicted automatically, so the one place an
# operator hears about them is here: a WARN with their count and size. Silent
# when there are none, or when this box runs no worker agent. Defined above
# the thin-node branch because both reports call it.
# ============================================================================
status_quarantined_weight_rows() {
    command -v docker >/dev/null 2>&1 || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-worker-agent || return 0
    local out n bytes names
    out="$(docker exec llm-worker-agent python3 -c '
import os, re
pat = re.compile(r"^.+\.(gguf|safetensors|bin|pt|pth|onnx)\.rejected-[0-9a-f]{12}$", re.I)
n = size = 0
names = []
for e in os.scandir("/models"):
    if e.is_file(follow_symlinks=False) and pat.match(e.name):
        n += 1
        size += e.stat().st_size
        names.append(e.name)
print(n, size, " ".join(sorted(names)))
' 2>/dev/null)" || return 0
    read -r n bytes names <<<"$out"
    case "$n" in ''|*[!0-9]*) return 0 ;; esac
    [ "$n" -gt 0 ] || return 0
    warn "$n weight file(s) quarantined in the models volume ($(awk -v b="$bytes" 'BEGIN{printf "%.1f GB", b/1e9}')): their sha256 differed from the catalogue pin"
    warn "    ${names}"
    warn "    Kept as evidence, never removed automatically: note the names, then delete them from the llm-node-models volume."
}

# ============================================================================
# #2393 — engines run with least privilege unless the operator opened the
# escape hatch (LLM_ENGINE_HARDENING=off). Read from the RUNNING worker-agent,
# the process that launches engines — not from .env, which it may not have
# picked up yet. Silent when this box runs no worker agent.
#
# #3293: and the verdict is about the engines that RUN, not the setting. An
# engine keeps the image and container settings it was created with until it
# is relaunched, so an upgraded box reported "on" over four engines created by
# the previous release: root, every capability, writable root, no pids limit,
# the old runner. Each engine container is inspected and measured against what
# the agent would launch it with today (razzfazz_engine_expectation).
# ============================================================================
status_engine_hardening_row() {
    command -v docker >/dev/null 2>&1 || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-worker-agent || return 0
    local v off=false expect names inspect verdict n_ok=0 kind name img why bad="" want
    v="$(docker inspect llm-worker-agent --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^LLM_ENGINE_HARDENING=//p' | head -1 || true)"
    if [ "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" = off ]; then
        off=true
        warn "Engine hardening: OFF (LLM_ENGINE_HARDENING=off) — engines launch as root with default capabilities and a writable root. Note the reason; set it back to empty and recreate llm-worker-agent once fixed."
    fi
    expect="$(razzfazz_engine_expectation)" || expect=""
    if [ -z "$expect" ]; then
        warn "Engines: llm-worker-agent did not say which runner and settings it launches engines with — the running engines were not checked"
        return 0
    fi
    local as_root=""
    IFS=$'\t' read -r want as_root < <(printf '%s' "$expect" | python3 -c '
import json, sys
e = json.load(sys.stdin)
print("%s\t%s" % (e.get("image") or "?", "root" if (e.get("cap_drop") and not e.get("user")) else ""))' 2>/dev/null) || true
    want="${want:-?}"
    if ! names="$(docker ps --filter label=rzfz.role=llm-engine --format '{{.Names}}' 2>/dev/null)"; then
        warn "Engines: could not list the engine containers — not checked"
        return 0
    fi
    names="$(printf '%s\n' "$names" | sed '/^$/d')"
    if [ -z "$names" ]; then
        [ "$off" = true ] || pass "Engine hardening: on — no engine is running on this box; new engines launch on ${want}, hardened"
        return 0
    fi
    # shellcheck disable=SC2086  # one container name per word
    if ! inspect="$(docker inspect $names 2>/dev/null)" \
       || ! verdict="$(printf '%s' "$inspect" | razzfazz_engine_runtime_verdict "$expect")"; then
        warn "Engines: could not inspect the engine containers — not checked"
        return 0
    fi
    while IFS=$'\t' read -r kind name img why; do
        case "$kind" in
            OK)    n_ok=$((n_ok + 1)) ;;
            STALE) bad="${bad}${name} (${why})"$'\n' ;;
        esac
    done <<< "$verdict"
    if [ -n "$bad" ]; then
        warn "Engines not on this release's runner or not hardened — they keep what they were created with until relaunched:"
        while IFS= read -r name; do
            [ -n "$name" ] && warn "    ${name}"
        done <<< "$bad"
        if [ "$(rzfz_node_role 2>/dev/null)" = worker ]; then
            warn "    Relaunch them from the master: LLM Manager console, Fleet -> this worker -> Runner images -> Upgrade (an engine that does not come up goes back to its previous runner)"
        else
            warn "    Relaunch them: rzfz relaunch-engines   (the LLM Manager's runner switch; an engine that does not come up goes back to its previous runner)"
        fi
        return 0
    fi
    [ "$off" = true ] && return 0
    if [ "$as_root" = root ]; then
        pass "Engine hardening: on — ${n_ok} engine(s) on ${want}, capless, read-only, pid-limited; root, because the agent has no GPU group ids (RAZZFAZZ_GPU_GROUP_GIDS)"
    else
        pass "Engine hardening: on — ${n_ok} engine(s) on ${want}, non-root, capless, read-only, pid-limited"
    fi
}

# Cloud metadata block (scripts/metadata-block.sh): containers must not reach
# 169.254.169.254 / 100.100.100.200 / fd00:ec2::254. The rules are read as root
# (sudo -n, like ufw above); without a grant the unit's state is the evidence,
# and the row says the rules themselves were not read. Defined before the
# thin-node block below, which reports it too (#3306).
status_metadata_block_row() {
    local script="${SCRIPT_DIR}/scripts/metadata-block.sh" out rc=0 unit
    local fix="sudo bash ${script} install"
    out=$(sudo -n bash "$script" status 2>/dev/null) || rc=$?
    if [ -n "$out" ] && [ "$rc" -ne 3 ]; then
        if [ "$rc" -eq 0 ]; then
            pass "Cloud metadata block: active for all containers ($(printf '%s' "$out" | tr '\n' ';' | sed 's/;$//; s/;/; /g'))"
        else
            fail "Cloud metadata block: NOT active ($(printf '%s' "$out" | tr '\n' ';' | sed 's/;$//; s/;/; /g')) — containers can reach cloud credentials; run: ${fix}"
        fi
        return 0
    fi
    unit=$(systemctl is-active rzfz-metadata-block.service 2>/dev/null || true)
    case "$unit" in
        active) warn "Cloud metadata block: unit active, rules not read (needs passwordless sudo); check: sudo bash ${script} status" ;;
        *)      fail "Cloud metadata block: not installed (unit ${unit:-unknown}) — containers can reach cloud credentials; run: ${fix}" ;;
    esac
}

# ============================================================================
# #266 (E7) — A THIN INFERENCE NODE ANSWERS AS ONE
#
# Every category below assumes a stack: Caddy vhosts, Authentik, postgres,
# profiles, the portals. A thin node has none of them. It ran them anyway
# against a `.env` that is not there, so an operator who typed `rzfz status` on
# a worker got a wall of failures about things that were never installed —
# the result of a read into nothing, not a report about the box.
#
# What a worker CAN answer is short, and all of it is on the box: which master
# it belongs to, whether its agent is up, which engines the master placed here,
# and whether it can pull a runner at all (#1860). That is the report; then it
# stops. `rzfz status` on the MASTER remains the place to ask about the fleet.
# ============================================================================
if [ "$(rzfz_node_role)" = "worker" ]; then
    NODE_ENV="${SCRIPT_DIR}/.env.node"
    node_get() {
        [ -f "$NODE_ENV" ] || { echo ""; return; }
        grep "^${1}=" "$NODE_ENV" 2>/dev/null | head -1 | cut -d= -f2- \
            | sed 's/^"//;s/"$//' | tr -d '\r'
    }

    # The header names what was actually looked at. "stack — assessment" over a
    # three-line node report is the same lie as the empty fields were.
    REPORT_TITLE="rzfz.ai thin inference node"
    cat_begin "NODE"
    info "role: thin inference node — the worker agent plus the engines the master deploys"
    _master="$(node_get LLM_MANAGER_URL)"
    if [ -n "$_master" ]; then
        pass "master: ${_master}"
    else
        fail "master: LLM_MANAGER_URL is empty in .env.node — this node belongs to nobody"
    fi
    _name="$(node_get LLM_WORKER_NAME)"
    [ -n "$_name" ] && info "name: ${_name}" || info "name: not pinned (the master names it at enrolment)"
    _hw="$(node_get HARDWARE)"
    [ -n "$_hw" ] && info "hardware: ${_hw}" || warn "HARDWARE is unset — the master cannot pick a runner class"
    if [ -n "$(node_get LLM_WORKER_COMMAND_KEY)" ]; then
        pass "enrolled: a command key is present"
    else
        fail "not enrolled: no LLM_WORKER_COMMAND_KEY — the master's commands will be refused"
    fi

    # #1860: the registry a runner pull would use, and WHICH source answered.
    # The value alone says nothing — `llm-registry:5000` is correct when an
    # operator set it and a dead end when the node fell back to it, because the
    # pull is performed by the host daemon, which is not on the compose network.
    cat_begin "RUNNER REGISTRY"
    _reg_explicit="$(node_get LLM_WORKER_RUNNER_REGISTRY)"
    _reg_hub="$(node_get LLM_HUB_DOMAIN)"
    if [ -n "$_reg_explicit" ]; then
        pass "registry: ${_reg_explicit} (LLM_WORKER_RUNNER_REGISTRY)"
    elif [ -n "$_reg_hub" ]; then
        pass "registry: ${_reg_hub} (LLM_HUB_DOMAIN)"
    else
        fail "registry: fell back to the compose name — this node can pull NO runner. Set LLM_HUB_DOMAIN or LLM_WORKER_RUNNER_REGISTRY in .env.node, docker login, restart the agent"
    fi

    cat_begin "CONTAINERS"
    if ! command -v docker >/dev/null 2>&1; then
        skip "docker is not on PATH — cannot look at the containers"
    elif ! docker ps >/dev/null 2>&1; then
        skip "docker is not answering — cannot look at the containers"
    else
        _agent_state="$(docker inspect -f '{{.State.Status}}' llm-worker-agent 2>/dev/null || true)"
        case "$_agent_state" in
            running) pass "llm-worker-agent: running" ;;
            "")      fail "llm-worker-agent: no such container — the node is not serving the master" ;;
            *)       fail "llm-worker-agent: ${_agent_state}" ;;
        esac
        status_quarantined_weight_rows
        status_engine_hardening_row
        # The engines the master placed here. Named by the master, so they are
        # counted, not enumerated against a list this box does not own.
        _engines="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c '^engine-' || true)"
        if [ "${_engines:-0}" -gt 0 ]; then
            info "engines placed by the master: ${_engines}"
        else
            info "engines placed by the master: none right now"
        fi
    fi

    cat_begin "HOST HARDENING"
    status_metadata_block_row

    render_report
fi

# ============================================================================
# #1902 — is GPUStack part of THIS box?
#
# The one place that answers it, so the SECRETS check and the COMPLETENESS
# check cannot drift apart. The profile names are the ones the LLM RUNTIME
# category already tests: `llm` (pre-2026.09) and `llm-legacy` (the single
# GPUStack profile since #1447; `llm-cpu` was folded into it).
#
# Reads COMPOSE_PROFILES, not the running containers: a stopped GPUStack is
# still part of the box's configuration, and a status check that answered
# differently depending on what happened to be up would be a coin toss.
# ============================================================================
_gpustack_on_this_box() {
    # `$profiles` is the script's own COMPOSE_PROFILES read (set before the
    # LLM RUNTIME category). Falling back to a fresh read keeps the function
    # usable from anywhere — including the sliced harness in test_179, which
    # sets `profiles` and no .env.
    case ",${profiles:-$(env_get COMPOSE_PROFILES)}," in
        *,llm,*|*,llm-legacy,*) return 0 ;;
        *) return 1 ;;
    esac
}

# STATUS_UPGRADE_SOURCE-BEGIN
# #602 — release channel + upgrade-source Ampel. Answers "will the next
# `rzfz upgrade` work?" here instead of at the upgrade stumble: channel,
# origin fit, credentials fit, and one cheap reachability probe.
#
# #2690: the credentials come from razzfazz_git_credential_scan (scripts/lib.sh),
# the same scan `rzfz forget-pat` removes with. Rental is the default; only
# RAZZFAZZ_STORE_PAT=true marks a fleet box that may store the PAT. A rental
# box that stores one gets a WARN naming where; a box with no marker yet (from
# before the key) gets an INFO saying what its next upgrade decides. An
# internal box with an https origin and no stored credential is not probed: the
# update check needs the operator's PAT, or an offline package, and the rows
# say so instead of "unreachable". No git command here ever prompts: no
# terminal prompt and no askpass program (one would block an unattended run).
# Sets STATUS_GIT_NEEDS_PAT for status_tag_drift_row. A FUNCTION so the real
# lines run in the tests.
status_upgrade_source_rows() {
    local channel origin_url origin_disp found names
    local has_pat=false has_cred=false
    STATUS_GIT_NEEDS_PAT=0
    channel=$(env_get RAZZFAZZ_CHANNEL); channel="${channel:-internal}"
    # #1738: RAW, so this line and the pre-flight that acts on it agree, and
    # so an operator reads back what stands in .git/config. The reachability
    # probe below asks git for the remote by NAME and therefore still contacts
    # the rewritten URL, which is the right one to contact.
    origin_url=$(razzfazz_origin_url "$SCRIPT_DIR")
    # REDACT userinfo before display. A remote configured as
    # https://<user>:<PAT>@git.razzfazz.ai/… would otherwise leak the token
    # into stdout, into --json, into any --no-color log capture, and into
    # `rzfz security-check`, which embeds this output verbatim. The unredacted
    # value stays in $origin_url for the ls-remote probe below.
    origin_disp="$(printf '%s' "$origin_url" | sed -E 's#://[^/@]*@#://<redacted>@#')"
    # #635 review: the scan also looks in the invoking user's home under sudo.
    found=$(razzfazz_git_credential_scan "$SCRIPT_DIR" list 2>/dev/null || true)
    if printf '%s\n' "$found" | grep -q '^cred'; then
        has_cred=true
        # A credential helper of another kind (a keyring) may serve any host:
        # it alone does not make the box fleet-shaped.
        printf '%s\n' "$found" | grep '^cred' | grep -vq '(a credential helper' && has_pat=true
    fi
    names=$(printf '%s\n' "$found" | awk -F'\t' 'NF>=3{printf "%s%s (%s)", (n++ ? "; " : ""), $2, $3}')
    # A global helper with a password in its text is only disabled for the
    # Gitea host, never unset (it may serve other hosts): neither forget-pat
    # nor the upgrade removes it, so the operator is told to, as forget-pat does.
    local by_hand="" rest
    if printf '%s\n' "$found" | awk -F'\t' '$2 ~ /with a password in its text/ && $3 !~ /\/\.git\/config$/' | grep -q .; then
        by_hand="A global git config has a helper with a password written into its text. It stays for other hosts and is disabled for git.razzfazz.ai, but the password is still on disk: remove that helper by hand."
    fi
    rest=$(printf '%s\n' "$found" | awk -F'\t' 'NF>=3 && !($2 ~ /with a password in its text/ && $3 !~ /\/\.git\/config$/)')
    case "$(razzfazz_store_pat_marker "$SCRIPT_DIR/.env")" in
        true)
            info "fleet box (RAZZFAZZ_STORE_PAT=true): may store the Gitea PAT"
            ;;
        false)
            if [ -n "$found" ]; then
                if [ -n "$rest" ]; then
                    warn "rental box stores a Gitea credential: ${names}. Remove it: rzfz forget-pat (the next rzfz upgrade removes it too)${by_hand:+ $by_hand}"
                else
                    warn "rental box stores a Gitea credential: ${names}. ${by_hand}"
                fi
            else
                pass "rental box: no Gitea credential stored"
            fi
            ;;
        *)
            if razzfazz_pat_adoptable "$SCRIPT_DIR"; then
                warn "no RAZZFAZZ_STORE_PAT marker yet; this box stores a Gitea credential, so the next rzfz upgrade marks it a fleet box (RAZZFAZZ_STORE_PAT=true) and keeps it. A rental box: set RAZZFAZZ_STORE_PAT=false first"
            elif [ -n "$found" ]; then
                if [ -n "$rest" ]; then
                    warn "rental box (no RAZZFAZZ_STORE_PAT marker) keeps: ${names}. The next rzfz upgrade removes it, or now: rzfz forget-pat${by_hand:+ $by_hand}"
                else
                    warn "rental box (no RAZZFAZZ_STORE_PAT marker) keeps: ${names}. ${by_hand}"
                fi
            else
                pass "rental box: no Gitea credential stored"
            fi
            ;;
    esac
    case "$channel" in
        public)
            if [ "$has_pat" = "true" ]; then
                fail "channel=public but a git.razzfazz.ai PAT is present — fleet-shaped box on the lagging mirror (fix: RAZZFAZZ_CHANNEL=internal)"
            else
                info "channel=public (upgrades: GitHub mirror, anonymous)"
            fi
            # #3265: nothing from git.razzfazz.ai stays on a public-channel box.
            if ! _dev_said=$(razzfazz_prune_dev_history "$SCRIPT_DIR" check 2>/dev/null); then
                warn "development history from git.razzfazz.ai in this checkout (${_dev_said}): the next verified rzfz upgrade removes it"
            fi
            ;;
        internal)
            if ! razzfazz_internal_channel_allowed "$SCRIPT_DIR"; then
                # #3265: rental boxes run the public channel only.
                _who="rental box"; [ "$RAZZFAZZ_FLEET_MAY_USE_INTERNAL_CHANNEL" = 1 ] || _who="box"
                fail "${_who} on channel=internal: it would fetch from git.razzfazz.ai. $(razzfazz_rental_internal_fixes | tr '\n' ' ')"
                case "$origin_url" in https://*) [ "$has_cred" = "true" ] || STATUS_GIT_NEEDS_PAT=1 ;; esac
            elif [ "$has_cred" = "true" ]; then
                info "channel=internal (upgrades: git.razzfazz.ai, PAT present)"
            else
                case "$origin_url" in https://*) STATUS_GIT_NEEDS_PAT=1 ;; esac
                if razzfazz_rental_box "$SCRIPT_DIR/.env"; then
                    info "channel=internal, no stored PAT (rental box): the update check needs the operator's PAT, or an offline package"
                else
                    warn "channel=internal but no stored git.razzfazz.ai PAT: the update check needs the operator's PAT, or an offline package ('rzfz upgrade' asks for the PAT at a terminal and does not store it)"
                fi
            fi
            ;;
    esac
    if [ -n "$origin_url" ]; then
        if [ "$STATUS_GIT_NEEDS_PAT" = 1 ]; then
            skip "upgrade source check ($origin_disp): needs the operator's PAT, or an offline package"
        elif GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true SSH_ASKPASS= \
                timeout 8 git -C "$SCRIPT_DIR" ls-remote --exit-code origin HEAD >/dev/null 2>&1 </dev/null; then
            pass "upgrade source reachable+authenticated ($origin_disp)"
        else
            warn "upgrade source NOT reachable/authenticated ($origin_disp) — offline box, wrong channel, or missing/expired credentials"
        fi
    fi
    return 0
}
# STATUS_UPGRADE_SOURCE-END

# STATUS_TAG_DRIFT-BEGIN
# M033 S29 — tag-move drift detection (body; the comment at the call site says
# why). A FUNCTION so the real lines run against a real origin in the tests.
#
# #2707: on the public mirror a release tag points at the curated EXPORT commit.
# A box installed from an internal checkout and switched to the public channel
# records the internal commit, so the shas never agree although the release is
# the same. When origin's tagged commit carries a PUBLIC_EXPORT_OF that names the
# deployed commit (release_tag_commit or source_commit), that is a PASS. The
# stamp comes from the tagged commit's git objects; when the commit is not in
# this clone yet, one quiet fetch of that tag ref brings its objects and
# nothing else: no tags, no FETCH_HEAD, and --refmap= so a configured
# `+refs/tags/*:refs/tags/*` refspec cannot move a local ref; 15 s cap. A missing or malformed stamp, or one naming another commit,
# leaves the FAIL as it was.
status_tag_drift_row() {
    local recorded_version="$1" recorded_commit="$2"
    local tag vtag remote_line remote_commit remote_short remote_ref matched_key
    case "$recorded_version" in
        v*-ga*|*-ga.*)
            # RAZZFAZZ_VERSION is stored without the leading 'v' (from the
            # VERSION file), but git tags carry it (v2026.05-ga.4). Query both
            # forms so the lookup hits regardless of which convention is recorded.
            tag="$recorded_version"
            vtag="v${recorded_version#v}"
            # #2690: no stored credential for an auth-gated origin: asking it
            # could only fail, and "unreachable" would be the wrong reason.
            if [ "${STATUS_GIT_NEEDS_PAT:-0}" = 1 ]; then
                skip "tag-drift check: needs the operator's PAT, or an offline package"
                return 0
            fi
            # ls-remote is lightweight (no fetch); short timeout so an offline
            # or slow box just skips the check instead of hanging the status run.
            # GIT_TERMINAL_PROMPT=0 + GIT_ASKPASS=true: NEVER prompt for git
            # credentials. origin is the auth-gated Gitea; on a customer box with
            # no cached creds, plain `git ls-remote` PROMPTS for a username on the
            # TTY and hangs (timeout can't interrupt the terminal read). With
            # prompting disabled git fails fast → the empty-result skip below
            # fires cleanly. Where creds ARE cached (vendor boxes) the check still
            # works via the non-interactive credential helper.
            # #2690: SSH_ASKPASS emptied and stdin closed as well — git runs an
            # askpass program even with terminal prompts off.
            remote_line=$(GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true SSH_ASKPASS= \
                timeout 8 git ls-remote --tags origin \
                "$tag" "${tag}^{}" "$vtag" "${vtag}^{}" 2>/dev/null </dev/null)
            if [ -z "$remote_line" ]; then
                skip "tag-drift check: origin unreachable or '$tag' not on origin"
            else
                # Prefer the dereferenced (^{}) line — that's the commit for an
                # annotated tag; lightweight tags only have the plain line.
                remote_commit=$(printf '%s\n' "$remote_line" | awk '/\^\{\}$/{print $1}' | head -1)
                [ -z "$remote_commit" ] && remote_commit=$(printf '%s\n' "$remote_line" | awk 'NR==1{print $1}')
                remote_short=$(printf '%s' "$remote_commit" | cut -c1-7)
                remote_ref=$(printf '%s\n' "$remote_line" | awk -v c="$remote_commit" '$1==c{print $2; exit}')
                remote_ref="${remote_ref%^\{\}}"
                if [ -z "$recorded_commit" ] || [ "$recorded_commit" = "unknown" ]; then
                    skip "tag-drift check: no RAZZFAZZ_COMMIT recorded to compare against"
                elif [ "${remote_commit#$recorded_commit}" != "$remote_commit" ] \
                     || [ "$remote_short" = "$recorded_commit" ]; then
                    pass "tag '$tag' on origin matches deployed commit ($recorded_commit)"
                # #3253: a stick installs a release tree — one commit cut from the tagged commit,
                # whose own PUBLIC_EXPORT_OF names origin's tag commit. Read from the deployed
                # commit, which is local; checked BEFORE the fetch below, which would otherwise
                # pull the tag's whole history onto the box just to compare.
                elif matched_key=$(razzfazz_export_stamp_names . "$recorded_commit" "$remote_commit"); then
                    pass "deployed commit $recorded_commit is the release tree of tag '$tag' on origin ($remote_short, PUBLIC_EXPORT_OF ${matched_key})"
                else
                    if ! git cat-file -e "${remote_commit}^{commit}" 2>/dev/null \
                       && [ -n "$remote_ref" ]; then
                        GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true SSH_ASKPASS= \
                            timeout 15 git fetch -q --no-tags --no-write-fetch-head --refmap= \
                            origin "$remote_ref" >/dev/null 2>&1 </dev/null || true
                    fi
                    if matched_key=$(razzfazz_export_stamp_names . "$remote_commit" "$recorded_commit"); then
                        pass "tag '$tag' on origin ($remote_short) is the public export of deployed commit $recorded_commit (PUBLIC_EXPORT_OF ${matched_key})"
                    else
                        fail "tag '$tag' has MOVED on origin: deployed=$recorded_commit, origin now=$remote_short"
                        warn "  same tag name = different code. GA tags must not be re-pointed;"
                        warn "  reconcile by upgrading to the next ga.N rather than re-pulling this tag."
                    fi
                fi
            fi
            ;;
    esac
}
# STATUS_TAG_DRIFT-END

# ============================================================================
# CATEGORY 1 — CODE STATE
# ============================================================================
cat_begin "CODE STATE"
# #3491: an upgrade that stopped after it changed the code and BEFORE the
# restart (the marker cli/upgrade.sh keeps for exactly that window). Here a
# re-run is the wrong advice: it would take its restore point from the
# half-upgraded tree. The way back is the rollback, which still holds the
# original restore point.
interrupted_marker="$SCRIPT_DIR/backups/state/upgrade-interrupted.env"
if [ -f "$interrupted_marker" ]; then
    interrupted_from=$(read_env_value "$interrupted_marker" FROM_VERSION 2>/dev/null || true)
    interrupted_to=$(read_env_value "$interrupted_marker" TO_VERSION 2>/dev/null || true)
    fail "half-upgraded box: the upgrade from ${interrupted_from:-unknown} to ${interrupted_to:-unknown} stopped after it changed the code and before the restart. Do not re-run the upgrade. From 2026.09 or newer: \`rzfz upgrade --rollback\`, then check \`rzfz status\`. From 2026.08, or when the rollback fails: stop and contact support."
fi
if ! command -v git >/dev/null 2>&1 || [ ! -d "$SCRIPT_DIR/.git" ]; then
    skip "git not available or not a git repo"
else
    cd "$SCRIPT_DIR"
    version_file=$(cat VERSION 2>/dev/null | tr -d '\n')
    head_commit=$(git rev-parse --short HEAD 2>/dev/null)
    head_ref=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
    if [ -n "$version_file" ]; then
        pass "VERSION file: $version_file"
    else
        warn "VERSION file empty/missing"
    fi
    if [ "$head_ref" = "HEAD" ]; then
        warn "git on detached HEAD at $head_commit (operator should checkout main if no local edits pending)"
    else
        pass "git on branch '$head_ref' at $head_commit"
    fi
    if git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null; then
        pass "git working tree clean"
    else
        warn "git working tree has uncommitted changes ($(git status --porcelain | wc -l) entries)"
    fi

    status_upgrade_source_rows

    # M033 S29 — tag-move drift detection.
    # razzfazz-init / razzfazz-upgrade record the commit a release tag resolved
    # to at deploy time in RAZZFAZZ_COMMIT. GA tags must never be re-pointed
    # (the release-cycle skill cuts ga.N+1 instead), but if one was, the same
    # tag name on origin now resolves to a *different* commit than this box
    # deployed — "v2026.05-ga.4 here" silently means different code than
    # "v2026.05-ga.4 on the dev box". Surface that so an operator never debugs
    # a phantom version mismatch again. Best-effort + offline-safe.
    recorded_version=$(env_get RAZZFAZZ_VERSION)
    recorded_commit=$(env_get RAZZFAZZ_COMMIT)
    upgrade_method=$(env_get RAZZFAZZ_UPGRADE_METHOD)

    # #1593: an upgrade that checked out the new code but did not finish leaves
    # this set. Without it the box reports the OLD version while running the NEW
    # code, and nothing anywhere says the run is unfinished — measured on a
    # customer box: tree v2026.08-ga.15, .env RAZZFAZZ_VERSION=2026.08-ga.9.
    # Say the pair rather than one of the halves.
    upgrade_in_progress=$(env_get RAZZFAZZ_UPGRADE_IN_PROGRESS)
    if [ -n "$upgrade_in_progress" ]; then
        fail "upgrade to ${upgrade_in_progress} did NOT finish — this box runs that code, but RAZZFAZZ_VERSION still says ${recorded_version:-unknown}. Re-run \`rzfz upgrade\` (the steps are idempotent) rather than setting the stamp by hand."
    fi
    if [ "$upgrade_method" = "offline-package" ]; then
        # #272: offline-package boxes carry a fresh VERSION/RAZZFAZZ_VERSION, but
        # the offline rsync intentionally does NOT move .git HEAD (and origin may
        # be unreachable anyway). RAZZFAZZ_COMMIT records the package's baked
        # commit, not this box's git HEAD, so a git-vs-origin tag-move comparison
        # is meaningless and produced a false "tag MOVED". Trust the version.
        pass "code version (offline-package): ${recorded_version:-unknown} — git tag-drift check skipped"
        recorded_version=""   # make the case below a no-op
    fi
    status_tag_drift_row "$recorded_version" "$recorded_commit"
fi

# #1247 — razzfazz.init one-shots (the agent image builders, the *-init
# bootstraps) must have exited 0. A non-zero exit means the artifact a module
# needs was never built, and until #1247 nothing said so: `rzfz init` reported
# RC 0 over a moltis-image-builder at Exited(127) and this report was silent
# too. Discovery + classification come from the shared, label-driven helper in
# scripts/lib.sh that init and `post-install --verify` read as well.
#
# A one-shot still running is INFO, not FAIL — a heavy image build legitimately
# outlives the run that started it, and this report is read-only. Same for one
# a stack stop signalled out from under it (137/143): `rzfz status` runs on
# stopped stacks all the time, and several agent builders only ever die that way.
# Uses a here-string (not a pipe) so pass/fail/info update THIS shell's
# counters; a `cmd | while` loop would discard them.
# Orphan VOLUMES (#1667). The container block above asks about containers;
# nothing asked about storage, and that is how 72 GB of model weights sat in a
# volume from a compose project that no longer exists — invisible, and
# re-downloaded from scratch by the container that replaced it.
#
# Reported, never removed: weights are recoverable but slowly, and a cleaner
# that gets it wrong costs a day of downloading. The operator decides.
#
# A FUNCTION, like status_init_oneshot_rows below it, for one reason (#1693):
# inline, this could only be grepped. The verdict in scripts/lib.sh was driven
# for real by seven pins while the lines that turn it into what an operator
# READS were not — and a mutation swapping the loop's input from the orphans to
# the whole listing reported every mounted volume as an orphan and stayed
# green. `docker system df -v` is the only box-dependent part, so a fake
# `docker` on PATH is enough to drive the whole thing.
# The LLM deployments (#1713). Nothing looked at them: `rzfz status` had no
# word for a deployment, and post-install warns only inside its own wait
# window, so a later look at the box learns nothing. A box can therefore serve
# chat while RAG and reranking are dead, with every display green — measured on
# 0.79, where three of four rows sat `pending` with no weight source at all.
#
# A FUNCTION, like status_orphan_volume_rows above and status_init_oneshot_rows
# below, for the reason #1693 spelled out: inline, only the verdict would be
# tested and the lines an operator READS would not.
#
# Silent when this box has no manager — most boxes do not, and a status command
# that complains about an absent module is a status command people stop reading.
# #2426: name the on-demand models an offline box cannot deploy -- informational,
# the default set is what the gate judges. Silent when verify-models printed no
# UNAVAILABLE OFFLINE line (complete, online, or an unreadable volume: unknown is
# never reported as unavailable).
status_models_unavailable_row() {
    local names
    names="$(printf '%s\n' "$1" | sed -n 's/^UNAVAILABLE OFFLINE: //p' | head -1)"
    if [ -n "$names" ]; then
        info "verify-models: not deployable offline (on-demand, not on this box): ${names}"
    fi
    return 0
}

status_package_key_row() {
    # #781: can this box check a signed offline package at all?
    #
    # `cli/upgrade.sh` already says it — but only while the operator is holding
    # the stick, which is too late for both halves of the rollout question:
    # which boxes have the anchor yet, and did a rollout arrive. This is the
    # same reading, asked early.
    local found src path
    found="$(razzfazz_package_key_source "${SCRIPT_DIR:-}" || true)"
    if [ -n "$found" ]; then
        src="${found%%	*}"; path="${found#*	}"
        pass "package signatures verifiable — trust anchor from ${src}: ${path}"
        return 0
    fi
    # NOT a failure: until the first GA ships signed packages there is nothing
    # for a box to hold, and refusing (or failing) now would flag the whole
    # fleet for a state that is expected. It is a fact the operator needs, not
    # a defect (#781, flag-day decision 2026-09-02).
    warn "no package trust anchor on this box — a signed offline package cannot be verified here"
    warn "    Looked at: \$RAZZFAZZ_PACKAGE_PUBKEY, ${RAZZFAZZ_PACKAGE_KEY_FLEET_DEFAULT},"
    warn "    <stack>/${RAZZFAZZ_PACKAGE_KEY_REPO_RELATIVE}"
    warn "    Until the fleet key is rolled out, check a stick with"
    warn "    'rzfz upgrade --package <file> --expect-sha256 <hash from the release notes>'."
}

# STACK_HOST_PATH is the stack directory as the DOCKER HOST sees it. The Config
# portal hands it to compose; without it (or with a different one) compose runs
# from the portal's own in-container path and docker creates root-owned empty
# directories on the host for every bind mount, which breaks the containers
# whose bind mount is a file. No .env yet = nothing to judge.
status_stack_host_path_row() {
    [ -f "$ENV_FILE" ] || return 0
    local want logical val
    want="$(cd "$SCRIPT_DIR" 2>/dev/null && pwd -P)"
    logical="$(cd "$SCRIPT_DIR" 2>/dev/null && pwd)"
    val="$(env_get STACK_HOST_PATH)"
    if [ -z "$val" ]; then
        fail "STACK_HOST_PATH is missing in .env — the Config portal refuses to apply changes until it is set to ${want} ('rzfz upgrade' writes it), then recreate razzfazz-config"
    elif [ "$val" != "$want" ] && [ "$val" != "$logical" ]; then
        fail "STACK_HOST_PATH=${val} differs from the real stack directory ${want} — set it to ${want}, then recreate razzfazz-config"
    else
        pass "STACK_HOST_PATH matches the stack directory (${val})"
    fi
}

status_canonical_alias_row() {
    # #979: the manager must answer to the backend-invariant name `llm`. The
    # alias lives in modules/llm/manager/compose.yml since #1445; a container
    # created before that keeps running happily WITHOUT it, and nothing says so
    # — `ps` is healthy, `/health` answers, only the name is missing.
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    local listing missing n
    listing="$(docker inspect llm-manager \
        --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}	{{range $v.Aliases}}{{.}} {{end}}{{println}}{{end}}' \
        2>/dev/null || true)"
    if [ -z "$listing" ]; then
        warn "could not read the llm-manager networks — the canonical '${RAZZFAZZ_CANONICAL_LLM_ALIAS:-llm}' alias not checked"
        return 0
    fi
    missing="$(printf '%s\n' "$listing" | razzfazz_missing_canonical_alias || true)"
    if [ -z "$missing" ]; then
        pass "llm-manager answers to the canonical '${RAZZFAZZ_CANONICAL_LLM_ALIAS:-llm}' name on every network"
        return 0
    fi
    n=$(printf '%s\n' "$missing" | grep -c . || true)
    warn "llm-manager does NOT answer to '${RAZZFAZZ_CANONICAL_LLM_ALIAS:-llm}' on $n network(s): $(printf '%s' "$missing" | tr '\n' ' ')"
    warn "    The alias is in modules/llm/manager/compose.yml — this container"
    warn "    predates it and was never recreated. Nothing is broken WHILE the consumers"
    warn "    still name 'llm-manager', but an upgrade that repoints them at the canonical"
    warn "    address leaves every one of them dialling a name no DNS knows."
    warn "    Fix: 'docker compose up -d llm-manager' (recreates it with the alias)."
}

# #2571: Caddy's admin API (:2019) is unauthenticated. Anything that can open a
# TCP connection to it can read the whole ingress config and replace it
# (POST /load), which means removing forward_auth in front of every app.
# Measured on a fleet test box before the fix: GET caddy:2019/config/ returned
# 200 from dify-sandbox (ssrf_proxy_network, including the uid that runs user
# code), from pipelines and from dify-plugin-daemon (default). The fix binds
# the listener to Caddy's own loopback (`admin localhost:2019`); this row
# checks the effect from the inside, the only place the answer is real: one
# container on each network asks caddy:2019 for /config/. caddy, agent-manager
# and mcp-manager are never the probe (the managers were the admin API's
# clients until #606/#619; an answer from them would say nothing about the
# network).
#   answered              → FAIL (the exposure is back)
#   refused / no answer   → PASS
#   nobody could ask      → WARN (unknown; never a silent pass)
# Read-only: one GET, nothing is ever sent but the request line.
_CADDY_ADMIN_PROBE_PY='
import socket
try:
    s = socket.create_connection(("caddy", 2019), 4)
except socket.gaierror:
    print("UNRESOLVED caddy does not resolve")
except ConnectionRefusedError:
    print("REFUSED connection refused")
except OSError as e:
    print("NOANSWER " + type(e).__name__)
else:
    try:
        s.settimeout(4)
        s.sendall(b"GET /config/ HTTP/1.0\r\nHost: caddy:2019\r\n\r\n")
        line = s.recv(128).split(b"\r\n")[0].decode("latin-1")
    except OSError as e:
        line = "connected, then " + type(e).__name__
    print("ANSWERED " + (line or "connected, empty reply"))
'

# One probe from inside container $1. Prints `<VERDICT> <detail>` where VERDICT
# is ANSWERED, REFUSED, NOANSWER, UNRESOLVED or NOTOOL. python3 first (a raw
# socket ignores the container's HTTP(S)_PROXY — dify-sandbox has one, and the
# proxy's 403 would read as "safe"), curl with --noproxy as the fallback.
_caddy_admin_probe_from() {
    local c="$1" out rc
    out=$(timeout 20 docker exec "$c" python3 -c "$_CADDY_ADMIN_PROBE_PY" 2>/dev/null)
    case "$out" in
        ANSWERED*|REFUSED*|NOANSWER*|UNRESOLVED*) printf '%s\n' "${out%%$'\n'*}"; return 0 ;;
    esac
    out=$(timeout 20 docker exec "$c" curl -s -o /dev/null --noproxy '*' --max-time 5 \
        -w '%{http_code}' http://caddy:2019/config/ 2>/dev/null)
    rc=$?
    case "$rc" in
        0|52) echo "ANSWERED HTTP ${out:-empty reply}" ;;
        7)    echo "REFUSED connection refused" ;;
        6)    echo "UNRESOLVED caddy does not resolve" ;;
        28)   echo "NOANSWER timeout" ;;
        *)    echo "NOTOOL neither python3 nor curl ran (rc=$rc)" ;;
    esac
}

status_caddy_admin_exposure_row() {
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx caddy; then
        skip "caddy not running — admin API exposure not probed"
        return 0
    fi
    local net members c res verdict detail tried
    # Containers known to carry python3 go first; they are also the ones the
    # measurement found on each net. Any other member is tried after them.
    local preferred="dify-sandbox pipelines dify-plugin-daemon open-webui authentik-server authentik-worker"
    for net in default ssrf_proxy_network; do
        members=$(docker network inspect "razzfazz-stack_${net}" \
            --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}' 2>/dev/null \
            | grep -vxE 'caddy|agent-manager|mcp-manager|' || true)
        if [ -z "$members" ]; then
            [ "$net" = "default" ] && warn "Caddy admin API exposure on ${net}: unknown — no container on razzfazz-stack_${net} to probe from"
            continue
        fi
        verdict=""; detail=""; tried=0
        for c in $( { for p in $preferred; do printf '%s\n' "$members" | grep -x "$p"; done
                      printf '%s\n' "$members"; } | awk 'NF && !seen[$0]++' ); do
            [ "$tried" -ge 3 ] && break
            tried=$((tried + 1))
            res=$(_caddy_admin_probe_from "$c")
            case "$res" in
                ANSWERED*|REFUSED*|NOANSWER*) verdict="${res%% *}"; detail="${res#* }"; break ;;
            esac
        done
        case "$verdict" in
            ANSWERED)
                fail "Caddy admin API ANSWERS on ${net} (from ${c}: ${detail}) — any container there can rewrite the ingress config. Fix: recreate caddy from the shipped Caddyfile, 'docker compose up -d caddy'" ;;
            REFUSED|NOANSWER)
                pass "Caddy admin API not reachable from ${net} (probed from ${c}: ${detail})" ;;
            *)
                warn "Caddy admin API exposure on ${net}: unknown — no container there could run the probe" ;;
        esac
    done
}

# Caddy sitting at its cgroup memory limit thrashes the whole host: the
# kernel evicts Caddy's own file-backed pages (binary, libs) and Caddy
# refaults them from disk — measured with memory.events max in the millions,
# load ~100, iowait 50-64%, and every Caddy-fronted site answering 502, with
# nothing OOM-killed. The limit itself is configurable now (CADDY_MEM_LIMIT,
# core/compose.yml); this row is the early signal: WARN when memory.current
# is above 80% of memory.max, or when memory.events records a non-zero `max`
# (the limit was hit and pages reclaimed). Read-only: it only reads the
# container's cgroup files — no sudo, no docker exec. STATUS_CGROUP_ROOT
# overrides the cgroup mount for tests.
status_caddy_memory_row() {
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx caddy; then
        skip "caddy not running — memory pressure not checked"
        return 0
    fi
    local root="${STATUS_CGROUP_ROOT:-/sys/fs/cgroup}"
    local id dir current max maxev pct d
    id=$(docker inspect -f '{{.Id}}' caddy 2>/dev/null || true)
    dir=""
    if [ -n "$id" ]; then
        # systemd cgroup driver, then cgroupfs, then the short id
        for d in "$root/system.slice/docker-${id}.scope" "$root/docker/${id}" \
                 "$root/system.slice/docker-${id:0:12}.scope"; do
            if [ -d "$d" ]; then dir="$d"; break; fi
        done
    fi
    if [ -z "$dir" ]; then
        warn "caddy memory: its cgroup is not under ${root} (docker-${id:-unknown}.scope) — pressure unknown"
        return 0
    fi
    current=$(cat "$dir/memory.current" 2>/dev/null || true)
    max=$(cat "$dir/memory.max" 2>/dev/null || true)
    maxev=$(awk '$1 == "max" { print $2 }' "$dir/memory.events" 2>/dev/null || true)
    if [ -z "$current" ] || [ -z "$max" ]; then
        warn "caddy memory: cannot read memory.current/memory.max in ${dir} — pressure unknown"
        return 0
    fi
    if [ "$max" = "max" ]; then
        pass "caddy memory: no hard limit (memory.max=max), current ${current} bytes"
        return 0
    fi
    case "${current}${max}${maxev}" in
        *[!0-9]*)
            warn "caddy memory: unparseable cgroup values in ${dir} (current=${current} max=${max} max=${maxev})"
            return 0 ;;
    esac
    maxev="${maxev:-0}"
    pct=$(( current * 100 / max ))
    if [ "$maxev" -gt 0 ]; then
        warn "caddy memory: the ${max}-byte limit was hit ${maxev} time(s) (memory.events max) — pages are being reclaimed at the limit; raise CADDY_MEM_LIMIT if this recurs"
    elif [ $(( current * 100 )) -gt $(( max * 80 )) ]; then
        warn "caddy memory: ${pct}% of its ${max}-byte limit (memory.current=${current}) — above 80%"
    else
        pass "caddy memory: ${pct}% of its ${max}-byte limit (memory.current=${current}), no limit hits"
    fi
}

# Docling MCP's egress sidecar denies the box's own addresses: HOST_IP and
# whatever MAIN_DOMAIN resolves to INSIDE a container. A domain that lives only
# in the host's /etc/hosts (or an offline box) does not resolve there; the
# sidecar then starts on HOST_IP alone and records that in its state file.
# Say so: the box's public addresses are then not in the deny list.
status_docling_egress_row() {
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx docling-egress; then
        return 0
    fi
    local state
    state=$(timeout 20 docker exec docling-egress cat /tmp/docling-egress.status 2>/dev/null || true)
    case "$state" in
        *main_domain=resolved*)
            pass "Docling MCP egress denies the box's own addresses (MAIN_DOMAIN and HOST_IP)" ;;
        *main_domain=unresolved*|*main_domain=unset*)
            warn "Docling MCP egress runs WITHOUT MAIN_DOMAIN's addresses in its deny list (it does not resolve inside containers; only HOST_IP is denied). Make MAIN_DOMAIN resolvable in DNS, then 'docker compose up -d --force-recreate docling-egress'" ;;
        *)
            warn "Docling MCP egress: deny-list state unknown (no state file in docling-egress)" ;;
    esac
}

# The Ray job API and dashboard agent of the legacy GPUStack are unauthenticated:
# a reachable one is code execution in the container. The ufw row above reads the
# firewall; this one reads what the container ACTUALLY publishes, which is true
# on a box without ufw and when a rule is missing. 8265 and 52365 may only be
# published on loopback, whatever GPUSTACK_HOST_BIND says. Silent without GPUStack.
status_gpustack_ray_binds_row() {
    local ports open
    ports=$(docker port gpustack 2>/dev/null || true)
    [ -n "$ports" ] || return 0
    open=$(printf '%s\n' "$ports" \
        | awk '$1 ~ /^(8265|52365)\/(tcp|udp)$/ && $2 == "->" { addr=$3; sub(/:[0-9]+$/, "", addr);
                 if (addr != "127.0.0.1" && addr != "::1" && addr != "[::1]") print $1 " on " addr }' \
        | awk '!seen[$0]++' | paste -sd, - || true)
    if [ -n "$open" ]; then
        fail "GPUStack Ray ports published beyond loopback: ${open} — the Ray job API is unauthenticated (remote code execution); remove the extra bind from modules/llm/compose.yml and recreate gpustack"
    else
        pass "GPUStack Ray ports (8265, 52365) published on loopback only"
    fi
}

# The GPUStack master ports open to anywhere (and 8265, the unauthenticated Ray
# dashboard / job API, which no GPUStack rule should ever open). Earlier harden-host runs opened
# 9090, 10150-10151, 40000-40103 and 52365 from anywhere whenever
# GPUSTACK_MODE=master, profile or not; the current script scopes them to the
# worker subnet and removes its earlier rules on a re-run. Without GPUStack
# any such rule is a WARN with a PASS otherwise; on a GPUStack box an
# any-source rule is a WARN and a clean firewall prints nothing. Reads
# `ufw status` (needs sudo) and the profile, never the containers.
status_gpustack_master_ports_row() {
    command -v ufw >/dev/null 2>&1 || return 0
    local out open hint
    out=$(sudo -n ufw status 2>/dev/null || true)
    if [ -z "$out" ]; then
        _gpustack_on_this_box || info "GPUStack master ports: not checked (ufw status needs sudo)"
        return 0
    fi
    open=$(printf '%s\n' "$out" \
        | grep -E '^([^[:space:]]+ )?(8265|9090|10150:10151|40000:40103|52365)(/tcp)?( \(v6\))?( on [^[:space:]]+)?[[:space:]]+ALLOW( IN)?[[:space:]]+Anywhere' \
        | grep -oE '(^| )(8265|9090|10150:10151|40000:40103|52365)(/tcp)?' | tr -d ' ' \
        | awk '!seen[$0]++' | paste -sd, - || true)
    hint="earlier harden-host rules are removed on re-run (sudo bash scripts/harden-host.sh); remove hand-made rules with 'ufw delete'"
    if _gpustack_on_this_box; then
        [ -n "$open" ] && warn "GPUStack master ports open to anywhere: ${open} — they should allow the worker subnet only; ${hint}"
        return 0
    fi
    if [ -n "$open" ]; then
        warn "GPUStack master ports open to anywhere without GPUStack on this box: ${open} — ${hint}"
    else
        pass "GPUStack master ports closed (GPUStack not on this box)"
    fi
}

# A world-writable Docker socket is root for every local user. Nothing in the
# stack makes it so, but a host script that once ran its own comments as root did
# (chmod 666 on /var/run/docker.sock), and the mode then lasts until Docker is
# restarted. FAIL on any "other" write bit; the upgrade repairs it.
status_docker_socket_row() {
    local sock="${RZFZ_DOCKER_SOCKET:-/var/run/docker.sock}" mode
    [ -S "$sock" ] || return 0
    mode=$(stat -c %a "$sock" 2>/dev/null || true)
    case "$mode" in
        [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
        *) info "Docker socket mode: not readable"; return 0 ;;
    esac
    if [ $(( 8#$mode & 2 )) -ne 0 ]; then
        fail "Docker socket ${sock} is world-writable (mode ${mode}) — every local user is root: sudo systemctl restart docker.socket docker, then check 'stat -c %a ${sock}' prints 660 (the upgrade also repairs it)"
    else
        pass "Docker socket not world-writable (mode ${mode})"
    fi
}

# The Dify SSRF proxy's deny list (DIFY_SSRF_DENY_HOSTS) is substituted into a
# CEL expression in the Caddyfile: a quote or backslash in it breaks the
# expression and Caddy does not start; an empty value is a bare `deny`. Hostnames
# (`*.example.com` wildcards), IP addresses and CIDRs only.
status_dify_ssrf_deny_row() {
    local raw v tok toks bad="" n=0
    raw=$(grep -c "^DIFY_SSRF_DENY_HOSTS=" "$ENV_FILE" 2>/dev/null || true)
    [ "${raw:-0}" -gt 0 ] || return 0
    v="$(env_get DIFY_SSRF_DENY_HOSTS)"
    if [ -z "$v" ]; then
        fail "Dify SSRF deny list: DIFY_SSRF_DENY_HOSTS is set but empty, Caddy refuses to start; use disabled.invalid"
        return 0
    fi
    read -r -a toks <<< "$v"    # no pathname expansion of *.domain
    for tok in "${toks[@]}"; do
        if [[ "$tok" =~ ^(\*\.)?[A-Za-z0-9._:/-]+$ ]]; then n=$((n + 1)); else bad="${bad:+$bad }$tok"; fi
    done
    if [ -n "$bad" ]; then
        fail "Dify SSRF deny list: DIFY_SSRF_DENY_HOSTS holds entries that are not hostnames (${bad}), Caddy refuses to start; hostnames, IPs and *.domain wildcards only"
    elif [ "$v" != "disabled.invalid" ]; then
        pass "Dify SSRF deny list: the proxy refuses ${n} named host(s)"
    fi
}

status_llm_deployment_rows() {
    local json rows stuck n name state hint
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    # The manager refuses anything that did not come through the ingress proxy
    # (source-IP check), so the question is asked from inside caddy. That, and
    # WHICH identity to send, is already solved once in
    # cli/lib-llm-manager-deploy.sh::_llmm_admin_api — including the
    # LLM_MANAGER_ADMIN_USER -> RAZZFAZZ_ADMIN_USERNAME chain (#1148) and the
    # comma-to-pipe translation of LLM_MANAGER_ADMIN_GROUPS. Writing that chain
    # a fourth time would put a fourth copy of the pre-#1148 bootstrap name in
    # the tree and let four call sites drift; so it is borrowed, not copied. The
    # library
    # is safe to source: it defines functions and eight plain assignments, and
    # carries no top-level `set` (house rule #382).
    if ! declare -F _llmm_admin_api >/dev/null 2>&1; then
        # shellcheck source=/dev/null
        # #1713 rev-B: the helper is at cli/lib-llm-manager-deploy.sh and
        # SCRIPT_DIR (line 32) is the REPO ROOT, so the `cli/` segment is
        # required — cli/post-install.sh:85 has always had it. Without it the
        # source failed and `|| { warn; return 0; }` turned that into a clean
        # "not checked", so this whole check never ran on any box. The fallback
        # resolves to the repo root too, or it would reintroduce the bug the
        # moment SCRIPT_DIR is unset.
        . "${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/cli/lib-llm-manager-deploy.sh" 2>/dev/null || {
            warn "could not load the LLM Manager admin helper — deployments not checked"
            return 0
        }
    fi
    json="$(_llmm_admin_api GET /api/deployments 2>/dev/null || true)"
    if [ -z "$json" ]; then
        # No answer is not a clean answer — the same rule as the volume report.
        warn "could not read the LLM Manager deployments — not checked"
        return 0
    fi
    rows="$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rows = d if isinstance(d, list) else (d.get("deployments") or d.get("items") or [])
for r in rows:
    if not isinstance(r, dict):
        continue
    name = r.get("model_name") or r.get("name") or ""
    if not name:
        continue
    # The keys the list endpoint actually carries, measured against a live
    # manager: status, health, ready_instances, replicas, instances,
    # last_error. The weight source is NOT among them.
    print("\t".join((
        str(name),
        str(r.get("status") or r.get("state") or ""),
        str(r.get("ready_instances") if r.get("ready_instances") is not None else 0),
        str(r.get("replicas") if r.get("replicas") is not None else 1),
        str(len(r.get("instances") or [])),
        str(r.get("last_error") or ""),
    )))
' 2>/dev/null || true)"
    if [ -z "$rows" ]; then
        warn "the LLM Manager returned no readable deployment list — not checked"
        return 0
    fi
    stuck="$(printf '%s\n' "$rows" | razzfazz_stuck_deployments || true)"
    if [ -z "$stuck" ]; then
        pass "LLM Manager: every deployment is running"
        return 0
    fi
    n=$(printf '%s\n' "$stuck" | grep -c . || true)
    warn "$n LLM deployment(s) NOT running — chat may work while RAG/reranking do not:"
    while IFS=$'\t' read -r name state hint; do
        [ -n "$name" ] || continue
        warn "    $name  ($state) — $hint"
    done <<< "$stuck"
}

# #2897: a remote worker agent on another release line than this master. The
# manager does the comparison (agent_version_skew on /api/workers); this only
# reports it. Silent without a manager, WARN-only (a stale agent still serves).
status_llm_worker_skew_rows() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    declare -F _llmm_admin_api >/dev/null 2>&1 || return 0
    local json lines n name worker master cmd
    json="$(_llmm_admin_api GET /api/workers 2>/dev/null || true)"
    [ -n "$json" ] || return 0
    lines="$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(rows, list):
    sys.exit(0)
for r in rows:
    k = r.get("agent_version_skew") if isinstance(r, dict) else None
    if k:
        print("\t".join((str(r.get("display_name") or r.get("name") or "?"), str(k.get("worker")), str(k.get("master")), str(k.get("update")))))
' 2>/dev/null || true)"
    if [ -z "$lines" ]; then
        pass "LLM workers: every agent runs the master's release line"
        return 0
    fi
    n=$(printf '%s\n' "$lines" | grep -c . || true)
    warn "$n LLM worker agent(s) run another release than this master:"
    while IFS=$'\t' read -r name worker master cmd; do
        [ -n "$name" ] || continue
        warn "    $name  agent ${worker}, master ${master} — update it on that worker: ${cmd}"
    done <<< "$lines"
}

# #2394: every deployment that is not a verified catalogue model, and who said
# yes. Uses the admin helper status_llm_deployment_rows has loaded (called
# right after it); without it, or without a manager, the row is skipped.
# D15's reconcile backfill makes every catalogue row `verified`, so a box that
# runs the standard set alone PASSes.
status_llm_unverified_rows() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    declare -F _llmm_admin_api >/dev/null 2>&1 || return 0
    local json line pol
    # The value the manager actually reads (its container environment). A
    # value it does not recognise is ignored there, in favour of the default.
    pol="$(docker exec llm-manager printenv LLM_MANAGER_ALLOW_UNVERIFIED_MODELS 2>/dev/null || true)"
    case "$(printf '%s' "$pol" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
        ""|1|true|yes|on|0|false|no|off) ;;
        *) warn "LLM_MANAGER_ALLOW_UNVERIFIED_MODELS='$pol' is not a recognised value — the manager ignores it and uses the default (true: unverified models deploy after an acknowledgement). Set true or false." ;;
    esac
    json="$(_llmm_admin_api GET /api/deployments 2>/dev/null || true)"
    [ -n "$json" ] || return 0
    line=$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(rows, list):
    sys.exit(0)
bad = [r for r in rows if isinstance(r, dict) and r.get("verification") != "verified"]
if bad:
    def who(r):
        ack = r.get("unverified_ack") or {}
        if not ack:
            return "%s (deployed before acknowledgements were recorded)" % r.get("model_name")
        return "%s (acknowledged by %s at %s)" % (r.get("model_name"), ack.get("user", "?"), ack.get("at", "?"))
    names = ", ".join(who(r) for r in bad)
    print("%d unverified model deployment(s): %s" % (len(bad), names))
else:
    print("OK")' 2>/dev/null || true)
    case "$line" in
        "") return 0 ;;
        OK) pass "every model deployment is a verified catalogue model" ;;
        *)  warn "$line" ;;
    esac
}

# #3403 review: deployments on the experimental runner while the box has the
# runner switched off (RAZZFAZZ_EXPERIMENTAL_RUNNER): the box no longer builds
# or loads that runner, so they cannot be started, scaled or re-placed, and an
# engine of theirs that stops stays down. Named with the one-deployment rollback.
status_llm_experimental_off_row() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    declare -F _llmm_admin_api >/dev/null 2>&1 || return 0
    declare -F razzfazz_experimental_runner_enabled >/dev/null 2>&1 || return 0
    razzfazz_experimental_runner_enabled "${ENV_FILE:-.env}" && return 0
    local json movable needs
    json="$(_llmm_admin_api GET /api/deployments 2>/dev/null || true)"
    [ -n "$json" ] || return 0
    # #3468: two lists. A model the catalogue ties to the experimental runner
    # (`runner_required`) does not load on the stable one: its way out is
    # removing the deployment, never "back to stable".
    names() {
        printf '%s' "$json" | WANT="$1" python3 -c '
import json, os, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
want = os.environ["WANT"] == "needs"
print(", ".join(str(r.get("model_name")) for r in rows if isinstance(rows, list)
                and isinstance(r, dict) and r.get("runner_experimental")
                and (r.get("runner_required") == "experimental") == want))' 2>/dev/null || true
    }
    movable="$(names movable)"
    needs="$(names needs)"
    if [ -n "$movable" ]; then
        warn "Deployments on the experimental runner while it is off on this box (RAZZFAZZ_EXPERIMENTAL_RUNNER): ${movable} — they cannot be started, scaled or re-placed, and the box no longer loads that runner. Move each back to the stable runner (LLM Manager -> Models -> the model -> Back to stable runner), or turn the runner on again (RAZZFAZZ_EXPERIMENTAL_RUNNER=true, then rzfz post-install --refresh)."
    fi
    if [ -n "$needs" ]; then
        warn "Deployments of models that only the experimental runner loads, while it is off on this box (RAZZFAZZ_EXPERIMENTAL_RUNNER): ${needs} — they cannot be started, scaled or re-placed, and they do not load on the stable runner. Remove the deployment (LLM Manager -> Models -> the model -> Undeploy model), or turn the runner on again (RAZZFAZZ_EXPERIMENTAL_RUNNER=true, then rzfz post-install --refresh)."
    fi
}

# #3498: deployments whose stored engine params still carry a legacy key
# spelling (`n_parallel`, `ctx_size`, `temperature`, ...) or two spellings of
# one flag. The node launches the canonical one; before 2026.11-ga.3 it emitted
# both and llama.cpp kept the last, so `parallel: 2` + `n_parallel: 1` ran one
# slot. Reported, never rewritten: the operator re-saves the model once.
status_llm_param_alias_row() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    declare -F _llmm_admin_api >/dev/null 2>&1 || return 0
    local json lines
    json="$(_llmm_admin_api GET /api/deployments 2>/dev/null || true)"
    [ -n "$json" ] || return 0
    lines="$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for r in rows if isinstance(rows, list) else []:
    if not isinstance(r, dict):
        continue
    rep = r.get("params_aliases") or {}
    if not (rep.get("aliases") or rep.get("conflicts")):
        continue
    eff = r.get("params_effective") or {}
    par = eff.get("parallel") or "engine default"
    ctx = eff.get("ctx_size") or "engine default"
    per = eff.get("ctx_per_slot")
    what = "legacy keys " + ", ".join(rep.get("aliases") or [])
    if rep.get("conflicts"):
        what += "; stored twice: " + ", ".join(rep["conflicts"])
    name = r.get("model_name")
    msg = (f"{name} ({what}) -> launches with parallel {par}, "
           f"context {ctx}" + (f" ({per} per slot)" if per else ""))
    old = r.get("params_effective_before") or {}
    if (rep.get("conflicts") and old
            and (old.get("parallel"), old.get("ctx_size")) != (eff.get("parallel"), eff.get("ctx_size"))):
        def shape(e):
            n = e.get("parallel")
            slots = "the default slot count" if n is None else (f"{n} slot" + ("s" if n != 1 else ""))
            per_slot = e.get("ctx_per_slot") or e.get("ctx_size") or "the default"
            return f"{slots} x {per_slot} context"
        msg += f"; will run {shape(eff)} at next launch, was {shape(old)}"
    print(msg)
' 2>/dev/null || true)"
    [ -n "$lines" ] || return 0
    local line
    while IFS= read -r line; do
        [ -n "$line" ] && warn "Deployment params use more than one name for an engine setting: ${line}. The canonical name wins at launch. To store one name per setting, open the model in LLM Manager -> Models -> Edit and Save once."
    done <<< "$lines"
}

# #2628: an AMD unified-memory worker whose admission budget is the BIOS VRAM
# carve-out (under 8 GB) although the box has far more host memory. Current
# agents derive the budget from host memory in that case; an old agent (or a
# probe that failed) reports the carve-out, and then every large model is
# refused as "over-subscribe" while the box could place it.
status_llm_small_carveout_row() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    declare -F _llmm_admin_api >/dev/null 2>&1 || return 0
    local json line
    json="$(_llmm_admin_api GET /api/workers 2>/dev/null || true)"
    [ -n "$json" ] || return 0
    line=$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if isinstance(rows, dict):
    rows = rows.get("workers") or rows.get("items") or []
bad = []
for w in rows if isinstance(rows, list) else []:
    if not isinstance(w, dict):
        continue
    h = (w.get("hardware") or "").lower()
    if not any(k in h for k in ("amd", "gfx", "rocm", "vulkan")):
        continue
    try:
        vram, mem = float(w.get("vram_total_gb") or 0), float(w.get("mem_total_gb") or 0)
    except (TypeError, ValueError):
        continue
    if 0 < vram < 8 and mem >= 32:
        bad.append("%s (budget %.1f GB, %.0f GB host memory)" % (w.get("name"), vram, mem))
print("; ".join(bad) if bad else "OK")' 2>/dev/null || true)
    case "$line" in
        "") return 0 ;;
        OK) pass "no AMD worker budgets against a small BIOS carve-out" ;;
        *)  warn "LLM worker budget comes from a small BIOS VRAM carve-out, not from the box's memory: ${line}. Large models are refused as over-subscribed. Raise the BIOS UMA frame buffer size, upgrade the worker agent, or set LLM_WORKER_MEM_BUDGET_GB (for example 96) in .env and recreate llm-worker-agent." ;;
    esac
}

# #2562: the manager's own view of its datastores (GET /readyz). A Valkey
# outage DEGRADES — the manager keeps serving, API keys are still checked
# against Postgres, but the rate/budget counters fail open — so it is a WARN
# naming that consequence. Postgres down means keys cannot be checked, and a
# rejected Valkey password (or other cache misconfiguration) fails every /v1
# request: both FAIL. --max-time 12 outlasts the manager's 10 s Postgres
# connect timeout, so a blackholed Postgres reads as FAIL, not "not answered".
# `case` rather than `| grep -q`: under pipefail an early-exiting grep can
# SIGPIPE the printf and turn a match into a miss.
status_llm_manager_ready_row() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    local port out code body
    port=$(env_get LLM_MANAGER_PORT | grep -E '^[0-9]+$' || echo 8091)
    out=$(curl -s --max-time 12 -w '\n%{http_code}' "http://127.0.0.1:${port}/readyz" 2>/dev/null) || out=""
    code="${out##*$'\n'}"
    body="${out%$'\n'*}"
    [ "$code" = "$out" ] && body=""
    case "$code" in
        200) pass "llm-manager ready (postgres, valkey)" ;;
        503) case "$body" in
                 *'"postgres":"down'*|*'"postgres": "down'*|*'"valkey":"error'*|*'"valkey": "error'*)
                     fail "llm-manager NOT ready: ${body}" ;;
                 *)
                     warn "llm-manager degraded: Valkey unavailable — API keys are checked against Postgres, but rate/budget limits are NOT enforced until it returns" ;;
             esac ;;
        *)   warn "llm-manager readiness not answered (HTTP ${code:-none})" ;;
    esac
}

# #2512: the master's LLM role, and the promise a `fleet` master makes — it
# hosts NO models. An engine on a fleet master eats the stack's memory (prod
# 8.246, #2499: the standard set on the CPU master while four AMD workers
# idled): FAIL, naming the models. A not-yet-drained master or a fleet
# without a ready GPU worker is a WARN. A standalone box may host engines.
status_llm_master_role_row() {
    local role master wjson verdict kind msg
    if ! declare -F llm_master_role >/dev/null 2>&1; then
        # shellcheck source=/dev/null
        . "${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/cli/lib-llm-manager-deploy.sh" 2>/dev/null || {
            warn "could not load the LLM Manager helper — the master role is not checked"
            return 0
        }
    fi
    role=$(llm_master_role "$ENV_FILE")
    if [ "$role" != "fleet" ]; then
        pass "LLM master role: standalone — this box hosts its own models (LLM_MASTER_ROLE)"
        return 0
    fi
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager; then
        warn "LLM master role: fleet — llm-manager is not running, the master's engines are not checked"
        return 0
    fi
    master=$(read_env_value "$ENV_FILE" LLM_WORKER_NAME 2>/dev/null) || master=""
    [ -n "$master" ] || master="master"
    wjson="$(_llmm_admin_api GET /api/workers 2>/dev/null || true)"
    if [ -z "$wjson" ]; then
        warn "LLM master role: fleet — could not read the LLM Manager workers, the master's engines are not checked"
        return 0
    fi
    verdict="$(printf '%s' "$wjson" | MASTER="$master" python3 -c '
import json, os, sys
DEAD = {"failed", "stopped", "evicted", "gone"}
GPU = ("amd", "nvidia", "apple")
def fam(v):
    h = (v or "").lower()
    if ("amd" in h) or ("gfx" in h) or ("rocm" in h) or ("vulkan" in h): return "amd"
    if ("nvidia" in h) or ("cuda" in h): return "nvidia"
    if ("apple" in h) or ("metal" in h) or ("mlx" in h): return "apple"
    return h
try:
    rows = json.load(sys.stdin)
except Exception:
    print("UNREADABLE\t"); sys.exit(0)
if isinstance(rows, dict):
    rows = rows.get("workers") or rows.get("items") or []
master = os.environ["MASTER"]
me = next((w for w in rows if isinstance(w, dict) and w.get("name") == master), None)
gpus = [w for w in rows if isinstance(w, dict) and w.get("name") != master
        and w.get("status") == "ready" and not w.get("external") and fam(w.get("hardware")) in GPU]
if me:
    live = sorted({str(i.get("model_name") or "?") for i in (me.get("instances") or [])
                   if isinstance(i, dict) and str(i.get("status") or "").lower() not in DEAD})
    if live:
        print("ENGINE\t" + ", ".join(live))
    if me.get("status") == "ready":
        print("NOTDRAINED\t")
if not gpus:
    print("NOGPU\t")
print("GPUS\t%d" % len(gpus))
' 2>/dev/null || true)"
    local n_gpu bad=0 supervised=0
    n_gpu=$(printf '%s\n' "$verdict" | awk -F'\t' '$1=="GPUS"{print $2}')
    while IFS=$'\t' read -r kind msg; do
        case "$kind" in
            UNREADABLE)
                warn "LLM master role: fleet — the LLM Manager returned no readable worker list, not checked"; return 0 ;;
            ENGINE)
                fail "LLM master role: fleet, but the master's worker '${master}' hosts an engine: ${msg} — a fleet master hosts NO models. Undeploy them from the master in the LLM Manager console; placement puts them on the GPU workers."
                bad=1; supervised=1 ;;
            NOTDRAINED)
                warn "LLM master role: fleet, but the master's worker '${master}' is ready, not draining — placement may pick it. 'rzfz post-install --refresh' drains it."
                bad=1 ;;
            NOGPU)
                warn "LLM master role: fleet, but no ready GPU worker is joined — nothing serves the models."
                bad=1 ;;
        esac
    done <<< "$verdict"
    # #2536 item 16: the manager's list is not the host. An engine container
    # left on this (master) host after a drain, or started by hand, is in no
    # worker's instances yet eats the stack's memory. Every node-agent engine
    # carries rzfz.role=llm-engine (drivers/{amd,cpu,cuda_llamacpp}.py).
    # Assumption: no GPU worker-agent shares this host with the master. Engine
    # containers carry no worker identity (drivers set only rzfz.role), so a
    # co-hosted worker's legitimate engines would read as the master's (#2536).
    local host_engines host_rc=0
    host_engines=$(docker ps --filter label=rzfz.role=llm-engine --format '{{.Names}}' 2>/dev/null) || host_rc=$?
    if [ "$host_rc" -ne 0 ]; then
        warn "LLM master role: fleet — could not list the containers on this host, engines not checked"
        bad=1
    else
        host_engines=$(printf '%s\n' "$host_engines" | sed '/^$/d' | paste -sd ',' - | sed 's/,/, /g')
    fi
    if [ -n "$host_engines" ]; then
        if [ "$supervised" -eq 1 ]; then
            # The manager still supervises these (the ENGINE row above): a plain
            # removal is relaunched (#2289); the console undeploy is the remedy.
            fail "LLM master role: fleet, but engine container(s) run on this host: ${host_engines} — the LLM Manager still supervises them (see the row above): undeploy them in the console; do NOT docker rm -f, they would be relaunched"
        else
            fail "LLM master role: fleet, but engine container(s) run on this host: ${host_engines} — a fleet master hosts NO models. Check that the LLM Manager console lists them on no worker, then remove them: docker rm -f <name>"
        fi
        bad=1
    fi
    [ "$bad" -eq 0 ] && pass "LLM master role: fleet — the master hosts no models; ${n_gpu:-0} GPU worker(s) serve them"
    return 0
}

status_failed_runner_upgrade_rows() {
    # #1677: a failed runner switch leaves the worker drained and its engine
    # stopped — on purpose, so an operator decides. Until now only the manager's
    # API knew. Measured on 0.79: worker draining, model pending, no engine
    # container, and every display we ship silent.
    local wjson workers wid wname json rows failed n uid image reason
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    if ! declare -F _llmm_admin_api >/dev/null 2>&1; then
        # shellcheck source=/dev/null
        # #1713 rev-B: the helper is at cli/lib-llm-manager-deploy.sh and
        # SCRIPT_DIR (line 32) is the REPO ROOT, so the `cli/` segment is
        # required — cli/post-install.sh:85 has always had it. Without it the
        # source failed and `|| { warn; return 0; }` turned that into a clean
        # "not checked", so this whole check never ran on any box. The fallback
        # resolves to the repo root too, or it would reintroduce the bug the
        # moment SCRIPT_DIR is unset.
        . "${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/cli/lib-llm-manager-deploy.sh" 2>/dev/null || {
            warn "could not load the LLM Manager admin helper — runner switches not checked"
            return 0
        }
    fi
    wjson="$(_llmm_admin_api GET /api/workers 2>/dev/null || true)"
    if [ -z "$wjson" ]; then
        warn "could not read the LLM Manager workers — runner switches not checked"
        return 0
    fi
    workers="$(printf '%s' "$wjson" | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if isinstance(rows, dict):
    rows = rows.get("workers") or rows.get("items") or []
for w in rows or []:
    if isinstance(w, dict) and w.get("id"):
        print("%s\t%s\t%s" % (w["id"], w.get("name") or w["id"], w.get("status") or ""))
' 2>/dev/null || true)"
    [ -n "$workers" ] || { pass "LLM Manager: no workers to check for runner switches"; return 0; }

    rows=""
    while IFS=$'\t' read -r wid wname wstatus; do
        [ -n "$wid" ] || continue
        # A failed switch is an operator's problem only while it keeps the node
        # DRAINED. A switch refused before anything stopped, or rolled back,
        # leaves the node serving; and an operator who undrained it has decided.
        # Reporting those as "drained and serving nothing" was a FAIL that
        # nothing could clear. An unknown status (an older manager) is still
        # reported — silence is the wrong direction to err in here.
        if [ -n "$wstatus" ] && [ "$wstatus" != "draining" ]; then
            continue
        fi
        # 404 is the normal answer for a worker that was never switched.
        json="$(_llmm_admin_api GET "/api/workers/${wid}/upgrade-runner" 2>/dev/null || true)"
        [ -n "$json" ] || continue
        rows="${rows}$(printf '%s' "$json" | WORKER="$wname" python3 -c '
import json, os, sys
try:
    up = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(up, dict) or not up.get("state"):
    sys.exit(0)
print("%s\t%s\t%s\t%s\t%s" % (
    os.environ.get("WORKER", "?"), up.get("state", ""),
    up.get("upgrade_id", ""), up.get("image", ""),
    (up.get("error") or "").replace("\n", " ")))
' 2>/dev/null || true)"$'\n'
    done <<< "$workers"

    failed="$(printf '%s' "$rows" | razzfazz_failed_runner_upgrades || true)"
    if [ -z "$failed" ]; then
        pass "LLM Manager: no runner switch is waiting for a decision"
        return 0
    fi
    n=$(printf '%s\n' "$failed" | grep -c . || true)
    while IFS=$'\t' read -r wname uid image reason; do
        [ -n "$wname" ] || continue
        fail "runner switch FAILED on worker '$wname' — the node is drained and serving nothing: $reason"
        warn "    image: $image"
        # No `rzfz` verb is named here because none exists: the rollback lives
        # only on the manager API today. Naming a command that is not there
        # would send the operator looking for it while the node serves nothing.
        warn "    back: POST /api/runner-upgrades/$uid/rollback   (measured: relaunches the captured deployments)"
    done <<< "$failed"
    warn "    A failed switch stays drained on purpose: traffic must not"
    warn "    return to a node whose runner state is unknown. The decision is"
    warn "    yours — roll back, or fix the image coordinate and switch again."
}

# Orphan ad-hoc containers (no compose project label) — F-RC5-4 territory.
# #2660: a container without a compose project is NOT an orphan when another
# component owns its lifecycle. Same exclusion rule as agent-manager's orphan
# sweep (docker_client.list_agent_containers): `rzfz.role` marks an LLM engine
# (any value), and `razzfazz.managed=true` marks everything the socket
# provisioners create (engines, the LLM Manager, MCP servers, agent instances);
# real orphans of THOSE are the owner's business (the sweep checks the DB).
# gpustack model pods (names ending in -pause or -run-<N>) are likewise
# created dynamically and carry no project label.
status_adhoc_orphan_row() {
    local c labels project managed role orphan_count=0 orphan_names=""
    for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
        labels=$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "razzfazz.managed"}}|{{index .Config.Labels "rzfz.role"}}' 2>/dev/null) || labels=""
        project=${labels%%|*}
        role=${labels##*|}
        managed=${labels#*|}; managed=${managed%%|*}
        [ -z "$project" ] || continue
        [ -z "$role" ] || continue
        [ "$managed" != "true" ] || continue
        case "$c" in
            *-pause|*-run-[0-9]*) continue ;;  # gpustack model pod
        esac
        orphan_count=$((orphan_count + 1))
        orphan_names="$orphan_names $c"
    done
    if [ "$orphan_count" -eq 0 ]; then
        pass "no ad-hoc orphan containers (gpustack model pods, LLM engines and managed containers excluded)"
    else
        warn "$orphan_count ad-hoc orphan container(s):${orphan_names}"
    fi
}

status_orphan_volume_rows() {
    local listing orphans n vname vsize
    listing="$(docker system df -v \
        --format '{{range .Volumes}}{{.Name}}\t{{.Links}}\t{{.Size}}{{println}}{{end}}' \
        2>/dev/null || true)"
    if [ -z "$listing" ]; then
        # No answer is not the same as a clean answer. Saying "no orphans"
        # here would be a claim we did not measure.
        warn "could not read volume usage (docker system df) — orphan volumes not checked"
        return 0
    fi
    orphans="$(printf '%s\n' "$listing" | razzfazz_orphaned_stack_volumes || true)"
    if [ -z "$orphans" ]; then
        pass "no orphaned stack volumes"
        return 0
    fi
    n=$(printf '%s\n' "$orphans" | grep -c . || true)
    warn "$n stack volume(s) that NO container mounts — nothing is removed automatically:"
    while IFS=$'\t' read -r vname vsize; do
        [ -n "$vname" ] || continue
        warn "    $vname  ($vsize)"
    done <<< "$orphans"
    warn "    these are left over from an earlier compose project name; inspect"
    warn "    with 'docker volume inspect <name>' before deciding"
}

status_backup_only_volume_rows() {
    # #1709: the volumes the report above CANNOT see — mounted, but only by the
    # backup plumbing, which copies volumes rather than using them. Measured on
    # 0.79: 44.28 GB of GPUStack state on a box that runs no GPUStack, invisible
    # to `docker volume prune` (not dangling) and to the orphan report (two
    # links).
    local listing line vname vlinks vsize holders rows reclaim n
    listing="$(docker system df -v \
        --format '{{range .Volumes}}{{.Name}}\t{{.Links}}\t{{.Size}}{{println}}{{end}}' \
        2>/dev/null || true)"
    if [ -z "$listing" ]; then
        warn "could not read volume usage (docker system df) — reclaimable volumes not checked"
        return 0
    fi
    # Who holds each volume, asked per volume. An empty answer is passed on as
    # empty: the rule treats "unknown" as "not a verdict" rather than guessing
    # that nobody holds it (#1709).
    rows=""
    while IFS=$'\t' read -r vname vlinks vsize; do
        [ -n "$vname" ] || continue
        holders="$(docker ps -a --filter "volume=${vname}" --format '{{.Names}}' 2>/dev/null \
            | paste -sd, - 2>/dev/null || true)"
        rows="${rows}${vname}	${vlinks}	${vsize}	${holders}"$'\n'
    done <<< "$listing"
    reclaim="$(printf '%b' "$rows" | razzfazz_backup_only_volumes || true)"
    if [ -z "$reclaim" ]; then
        pass "no volumes held only by the backup plumbing"
        return 0
    fi
    n=$(printf '%s\n' "$reclaim" | grep -c . || true)
    warn "$n volume(s) that only the backup plumbing mounts — no service on this box uses their contents:"
    while IFS=$'\t' read -r vname vsize holders; do
        [ -n "$vname" ] || continue
        warn "    $vname  ($vsize)  held by: $holders"
    done <<< "$reclaim"
    warn "    Nothing is removed automatically. These survive 'docker volume prune'"
    warn "    (they are not dangling) and the orphan report above (they have links)."
    warn "    Check the contents, then 'docker volume rm <name>' if you are done with"
    warn "    them — a restore of an ARCHIVE older than the backup exclusion still"
    warn "    needs the volume to exist."
}

# _wazuh_fim_verdict <last_scan_epoch> <frequency_seconds> <now_epoch> [agent_id]
# Prints "<pass|warn>\t<text>". Split out from the row below so the decision can
# be measured without a box — the row itself needs docker and a running manager.
#
# #1936: the row NAMES the agent it read. It reads `syscheck/000/last_scan`,
# and 000 — the manager's own local agent — is the one agent whose on-demand
# rescan does not work: measured on 0.91 (2026-09-08) that both documented
# handles (`agent_control -r -a`, `PUT /syscheck?agents_list=000`) report
# success and leave the timestamp untouched for 240 s, while a reviewer
# measured on 2026-09-10 that for the REMOTE agent 001 both move it to the
# second of the call.
#
# So "last scan 300m ago" carried two different meanings and looked identical:
# on a remote agent it is a schedule to look into and can be refreshed on
# demand; on 000 it cannot be refreshed at all. An operator who cannot tell
# which agent the number belongs to cannot act on it — and the obvious action
# (force a rescan) is the one that silently does nothing here.
#
# Naming the agent is decision-free. WHICH agent this row should read is NOT
# (a remote one is the more accountable source because it can be refreshed, or
# the row could name several) — that is open in #1936 and deliberately not
# decided here.
_wazuh_fim_verdict() {
    local last="$1" freq="$2" now="$3" agent="${4:-000}" age human_freq human_age stale_hint
    human_freq=$(( freq / 3600 ))
    # The hint belongs on the WARN paths only: it tells the operator why the
    # obvious next step will appear to work and won't.
    stale_hint=""
    if [ "$agent" = "000" ]; then
        stale_hint=" — an on-demand rescan of agent 000 reports success without running, so only the schedule or a manager restart moves this"
    fi
    if [ -z "$last" ] || [ "$last" = "0" ]; then
        printf 'warn\tWazuh FIM (agent %s): no completed scan recorded yet (checks every %sh) — a file change is invisible until the first scan finishes%s\n' "$agent" "$human_freq" "$stale_hint"
        return 0
    fi
    age=$(( now - last ))
    [ "$age" -ge 0 ] || age=0
    human_age=$(( age / 60 ))
    # One full period plus 10%: a scan that starts on time still takes a while
    # to finish, and the timestamp is the END of the previous one.
    if [ "$age" -le $(( freq + freq / 10 )) ]; then
        printf 'pass\tWazuh FIM (agent %s): last scan %sm ago, checks every %sh\n' "$agent" "$human_age" "$human_freq"
    else
        printf 'warn\tWazuh FIM (agent %s): last scan %sm ago but it checks every %sh — the schedule is not being kept, and a file change stays invisible until it is%s\n' "$agent" "$human_age" "$human_freq" "$stale_hint"
    fi
}

status_wazuh_fim_scan_row() {
    # #1788: three healthy containers say nothing about whether anything is
    # being WATCHED. The manager's own FIM has no `realtime` and a 12h
    # `frequency`, so between two scans a file change is invisible — and until
    # now no display we ship said when the last one was. Measured on 0.91
    # (2026-09-08): FIM detected a change and never alerted, because alerts do
    # not form during the scan_on_start pass; only the scheduled run produced
    # rule 550. An operator reading three green containers concluded the
    # opposite.
    #
    # This row shows the TIME, not the cycle: "checks every 12h" is a
    # configuration value and does not answer the question an operator has after
    # a suspicion, which is "when did it last look?". It also makes a second
    # finding visible for free — both documented on-demand scans report success
    # without running, so the operator presses the button and the timestamp does
    # not move.
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx wazuh-manager || return 0

    local freq last now verdict level text
    # The RUNNING config, not the shipped one: a box may carry an operator edit.
    freq="$(docker exec wazuh-manager sh -c \
        "sed -n 's:.*<frequency>\\([0-9]\\+\\)</frequency>.*:\\1:p' /var/ossec/etc/ossec.conf | head -1" \
        2>/dev/null || true)"
    printf '%s' "$freq" | grep -Eq '^[0-9]+$' || {
        warn "Wazuh FIM: could not read the scan frequency from the manager's ossec.conf — cannot say whether the schedule is being kept"
        return 0
    }

    # The API is reachable only from inside the container, and the credential
    # lives in ITS environment — passing it on a `docker exec` command line
    # would put the password in the host's process table. The curl config
    # goes on stdin (`-K -`), which works in any POSIX sh inside the container. The user is
    # `wazuh-wui`; `wazuh` and `admin` are rejected (measured).
    last="$(docker exec wazuh-manager sh -c '
        t=$(printf '"'"'user = "wazuh-wui:%s"\n'"'"' "$WAZUH_API_PASSWORD" | curl -sk -K - -X POST \
              "https://localhost:55000/security/user/authenticate?raw=true" 2>/dev/null)
        [ -n "$t" ] || exit 1
        printf '"'"'header = "Authorization: Bearer %s"\n'"'"' "$t" | curl -sk -K - \
              "https://localhost:55000/syscheck/000/last_scan" 2>/dev/null
    ' 2>/dev/null | python3 -c '
import json, sys, calendar, time
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = ((d.get("data") or {}).get("affected_items") or [])
end = (items[0].get("end") if items and isinstance(items[0], dict) else None)
if not end:
    sys.exit(0)
for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d %H:%M:%S"):
    try:
        print(int(calendar.timegm(time.strptime(end, fmt)))); break
    except ValueError:
        continue
' 2>/dev/null || true)"

    now="$(date -u +%s)"
    # #1936: the agent id appears TWICE — in the API path above and here. It
    # cannot be a shell variable: the URL sits inside a single-quoted `sh -c`
    # script so the API password never reaches the host's process table, and a
    # variable would not expand there. So both are literals, and a guard
    # (tests/unit/status/test_1936_*) asserts they name the SAME agent — the
    # row must never be able to label a number it did not read.
    verdict="$(_wazuh_fim_verdict "$last" "$freq" "$now" "000")"
    level="${verdict%%$'\t'*}"
    text="${verdict#*$'\t'}"
    case "$level" in
        pass) pass "$text" ;;
        *)    warn "$text" ;;
    esac
}

# #2566: Komodo's auto_prune runs `docker image prune -a` every night. Read the
# LIVE flag (API key, admin login, else Komodo's store) — never the last log.
status_komodo_auto_prune_row() {
    case ",$(env_get COMPOSE_PROFILES)," in *,monitor,*) ;; *) return 0 ;; esac
    local st
    st=$(KOMODO_API_KEY="$(env_get KOMODO_API_KEY)" KOMODO_API_SECRET="$(env_get KOMODO_API_SECRET)" \
         KOMODO_INIT_ADMIN_USERNAME="$(env_get KOMODO_INIT_ADMIN_USERNAME)" \
         KOMODO_INIT_ADMIN_PASSWORD="$(env_get KOMODO_INIT_ADMIN_PASSWORD)" \
         KOMODO_PORT="$(env_get KOMODO_PORT)" \
         POSTGRES_USER="$(env_get POSTGRES_USER)" POSTGRES_PASSWORD="$(env_get POSTGRES_PASSWORD)" \
         razzfazz_komodo_auto_prune_state 2>/dev/null) || st=unknown
    case "$st" in
        off) pass "Komodo auto_prune: off" ;;
        on)  fail "Komodo auto_prune is ON — its nightly 'docker image prune -a' deletes images this stack needs. Fix: rzfz post-install --refresh (or Komodo: Servers → Local → Auto Prune off)" ;;
        *)   warn "Komodo auto_prune: cannot read the live setting (no working API key or admin login, Komodo's store unreadable) — run: rzfz post-install --refresh" ;;
    esac
    return 0
}

# #3385: Komodo's own cluster must not trust a NON-LOOPBACK network login.
# Clusters initialised before F-018 end their pg_hba with `host all all all
# trust`; the postgres-komodo-reconcile one-shot hardens those rules (#3374),
# but when it cannot write pg_hba.conf it only logs a WARNING and exits 0, so
# Komodo keeps running. Then every container on Komodo's network logs in with
# any password, and this row is what reports it. The condition is the
# reconcile's own NETWORK_TRUST (a test keeps the two equal): loopback trust is
# the image's own and FerretDB's DocumentDB needs it. Read-only, over the
# container's local socket (trusted there), SQL on stdin: no secret anywhere.
status_komodo_pg_hba_trust_row() {
    case ",$(env_get COMPOSE_PROFILES)," in *,monitor,*) ;; *) return 0 ;; esac
    local user n
    user="$(env_get POSTGRES_USER)"
    n=$(timeout 20 docker exec -i postgres-komodo \
            psql -X -U "${user:-docker}" -d postgres -tA -v ON_ERROR_STOP=1 2>/dev/null <<'SQL' | tr -d '[:space:]'
SELECT count(*) FROM pg_hba_file_rules WHERE type LIKE 'host%' AND auth_method = 'trust' AND error IS NULL AND NOT (
    address IN ('localhost', 'samehost')
    OR (address ~ '^127[.]' AND netmask ~ '^255[.]')
    OR (address = '::1' AND lower(netmask) = 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'));
SQL
    )
    case "$n" in
        0)
            pass "postgres-komodo: no network pg_hba rule trusts a login (loopback trust kept for FerretDB)" ;;
        ''|*[!0-9]*)
            warn "postgres-komodo: cannot read its pg_hba rules (container not running or not answering) — network trust not checked" ;;
        *)
            fail "postgres-komodo: ${n} network pg_hba rule(s) trust every login (host ... trust) — any container on Komodo's network logs in with any password. Fix: re-run the reconcile with pg_hba.conf writable (docker compose up -d --force-recreate postgres-komodo-reconcile; see docker logs postgres-komodo-reconcile), or change those rules to scram-sha-256 by hand and reload postgres-komodo" ;;
    esac
    return 0
}

status_init_oneshot_rows() {
    local records st name code state log fails=0 ok=0 unknown=0
    # `|| true`: the helper returns 1 when it found a FAIL. status.sh runs
    # without `set -e`, but the function must not depend on that.
    records=$(razzfazz_init_oneshot_status) || true
    if [ -z "$records" ]; then
        skip "no razzfazz.init one-shot containers present"
        return 0
    fi
    while IFS='|' read -r st name code state log; do
        [ -n "$st" ] || continue
        case "$st" in
            OK)      ok=$((ok + 1)) ;;
            WARN)    ok=$((ok + 1))
                     warn "$name: $log (docker logs $name)" ;;
            FAIL)    fails=$((fails + 1))
                     fail "$name exited $code: $log (docker logs $name)" ;;
            PENDING) info "$name is still ${state} (init one-shot, not finished)" ;;
            STOPPED) info "$name was signalled ($code) before it finished (init one-shot)" ;;
            MISSING) fails=$((fails + 1))
                     fail "$name — $log" ;;                                  # #1302
            UNKNOWN) unknown=$((unknown + 1))
                     warn "razzfazz.init one-shots NOT inspected ($name: $log) — no verdict" ;;  # #1301
        esac
    done <<< "$records"
    if [ "$fails" -eq 0 ] && [ "$unknown" -gt 0 ] && [ "$ok" -eq 0 ]; then
        return 0   # #1301: nothing measured, nothing claimed
    fi
    if [ "$fails" -eq 0 ]; then
        pass "all $ok completed razzfazz.init one-shot(s) exited 0"
    elif [ "$ok" -gt 0 ]; then
        # Account for the healthy ones too — one FAIL row must not read as
        # "every builder on this box is broken".
        info "$ok other razzfazz.init one-shot(s) exited 0"
    fi
    return 0
}

# #2873: a core service that is not up, or any enabled service left in Created /
# Exited(non-zero), is a FAIL - the same census the upgrade/init final gate uses.
# STATUS_FINAL_HEALTH-BEGIN
status_final_health_rows() {
    local findings name why left
    left=$(razzfazz_inactive_leftovers_hint)
    if findings=$(razzfazz_final_health_findings); then
        pass "core services up; nothing left in Created"
        [ -z "$left" ] || info "$left"
        return 0
    fi
    [ -z "$left" ] || info "$left"
    while IFS='|' read -r name why; do
        [ -n "$name" ] || continue
        fail "$name: $why (docker compose ps -a)"
    done <<< "$findings"
    return 0
}
# STATUS_FINAL_HEALTH-END

# #2403: the Authentik blueprints reached this box only if the stored marker
# (authentik-media volume, read through the running server) equals the tree's
# INIT_VERSION, and no start stamp is left from an apply that never finished.
# STATUS_AUTHENTIK_MARKER-BEGIN
status_authentik_init_marker_row() {
    local tree stored started
    command -v docker >/dev/null 2>&1 || { skip "authentik init marker: docker CLI not available"; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx authentik-server || { skip "authentik init marker: authentik-server not running"; return 0; }
    tree=$(grep -oE '^INIT_VERSION="[^"]+"' "${STACK_ROOT:-.}/core/init-authentik.sh" 2>/dev/null | head -1 | cut -d'"' -f2)
    [ -n "$tree" ] || { warn "authentik init marker: core/init-authentik.sh declares no INIT_VERSION"; return 0; }
    stored=$(docker exec authentik-server cat /data/media/.authentik_initialized 2>/dev/null | tr -d '[:space:]')
    started=$(docker exec authentik-server cat /data/media/.authentik_initialized.started 2>/dev/null | tr -d '[:space:]')
    if [ -n "$started" ]; then
        fail "authentik init marker ${stored:-(none)} vs tree ${tree}: an apply of ${started} started and never completed (partial) — run: docker compose up -d authentik-init && docker wait authentik-init"
    elif [ "$stored" = "$tree" ]; then
        pass "authentik init marker ${stored} == tree INIT_VERSION"
    else
        fail "authentik init marker ${stored:-(none)} vs tree ${tree}: blueprints not applied — run: docker compose up -d authentik-init && docker wait authentik-init"
    fi
    return 0
}
# STATUS_AUTHENTIK_MARKER-END

# A legacy LLM Manager group list REPLACES the role catalogue for its tier; say so.
# STATUS_RBAC_LEGACY-BEGIN
status_rbac_legacy_rows() {
    local k v
    for k in LLM_MANAGER_ADMIN_GROUPS LLM_MANAGER_ADMIN_TIER_GROUPS LLM_MANAGER_USER_TIER_GROUPS; do
        v=$(env_get "$k")
        [ -z "$v" ] || warn "$k overrides the role catalogue for this tier; remove it to use the catalogue"
    done
    return 0
}
# STATUS_RBAC_LEGACY-END

# The LLM Manager reports on /healthz whether it could load the role catalogue.
# Unavailable = every tier without a legacy override is empty (nobody is admin).
# STATUS_LLM_CATALOGUE-BEGIN
status_llm_catalogue_row() {
    local body
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    body=$(docker exec llm-manager python -c 'import urllib.request as u;print(u.urlopen("http://127.0.0.1:8080/healthz",timeout=5).read().decode())' 2>/dev/null || true)
    case "$body" in
        *'"role_catalogue":"unavailable"'*)
            fail "llm-manager cannot read its role catalogue: nobody holds an admin or user tier there until it is readable (check the mounts of /etc/razzfazz/rbac/roles.yaml and /opt/razzfazz/rbac/razzfazz_rbac.py and the llm-manager log)" ;;
        *'"role_catalogue":"ok"'*) pass "llm-manager reads its role catalogue" ;;
    esac
    return 0
}
# STATUS_LLM_CATALOGUE-END

# An Authentik bump still loads our blueprints only if no enabled blueprint
# instance sits in `error` (the model has no error column, so nothing else shows it).
# A module that is not enabled does not count: right after a portal toggle the
# re-copied blueprints of switched-off modules are still being re-applied and sit
# in `error` for a while, and status must not report a module nobody runs.
# A concurrent apply after a toggle can deadlock and leave an instance in `error`
# for a few minutes before it applies on its own; an instance whose row changed
# within `window` seconds (600) is reported as being re-applied (WARN), not as a failure.
# STATUS_AUTHENTIK_BLUEPRINTS-BEGIN
# The profile that must be enabled for a module blueprint to count ("" = always).
status_blueprint_profile() {
    case "${1##*/}" in
        10-lightrag.yaml) echo lightrag ;;
        11-cognee.yaml) echo cognee ;;
        12-docling.yaml) echo docling ;;
        13-stirling-pdf.yaml) echo stirling-pdf ;;
        16-paperclip.yaml) echo paperclip ;;
        17-element-web.yaml|18-synapse-oidc.yaml) echo matrix ;;
        19-paperless-ngx.yaml) echo paperless-ngx ;;
        20-vaultwarden.yaml) echo vaultwarden ;;
        21-infisical.yaml) echo infisical ;;
        22-onyx.yaml) echo onyx ;;
        26-agents.yaml) echo agents ;;
        27-observability.yaml) echo observability ;;
        29-crawl4ai.yaml) echo crawl4ai ;;
        31-mcp-manager.yaml) echo mcp ;;
        35-openuem.yaml) echo openuem ;;
        36-searxng.yaml) echo searxng ;;
        37-wazuh.yaml) echo wazuh ;;
        38-openbao.yaml) echo openbao ;;
    esac
}
status_authentik_blueprint_rows() {
    local raw lines total bad_old bad_new snippet profiles kept name st path age need window=600
    command -v docker >/dev/null 2>&1 || { skip "authentik blueprints: docker CLI not available"; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx authentik-worker \
        || { skip "authentik blueprints: authentik-worker not running"; return 0; }
    snippet='
import sys
from django.utils import timezone
from authentik.blueprints.models import BlueprintInstance
for b in BlueprintInstance.objects.filter(enabled=True).order_by("name"):
    age = int((timezone.now() - b.last_updated).total_seconds())
    sys.stdout.write("RZFZ-BP\t%s\t%s\t%s\t%d\n" % (b.name, b.status, b.path, age))
'
    raw=$(timeout 25 docker exec authentik-worker ak shell -c "$snippet" 2>/dev/null)
    lines=$(printf '%s\n' "$raw" | grep "^RZFZ-BP$(printf '\t')" || true)
    if [ -z "$lines" ]; then
        warn "authentik blueprints: the instance probe returned nothing usable — this check did NOT run"
        return 0
    fi
    profiles=",$(env_get COMPOSE_PROFILES | tr -d ' '),"
    kept=""
    while IFS=$'\t' read -r _ name st path age; do
        need=$(status_blueprint_profile "$path")
        if [ -n "$need" ]; then
            case "$profiles" in *",${need},"*) ;; *) continue ;; esac
        fi
        kept+="${name}"$'\t'"${st}"$'\t'"${age}"$'\n'
    done <<< "$lines"
    if [ -z "$kept" ]; then
        pass "authentik blueprints: no instance of an enabled module to check"
        return 0
    fi
    total=$(printf '%s' "$kept" | grep -c .)
    # An unknown age counts as old: only a measured fresh change is given the benefit.
    bad_old=$(printf '%s' "$kept" | awk -F'\t' -v w="$window" \
        '$2 != "successful" && !($3 ~ /^[0-9]+$/ && $3 + 0 < w) {printf "%s%s (%s)", sep, $1, $2; sep=", "}')
    bad_new=$(printf '%s' "$kept" | awk -F'\t' -v w="$window" \
        '$2 != "successful" && ($3 ~ /^[0-9]+$/ && $3 + 0 < w) {printf "%s%s (%s)", sep, $1, $2; sep=", "}')
    if [ -n "$bad_old" ]; then
        fail "authentik blueprints not applied: ${bad_old} — see Admin → Customization → Blueprints"
    elif [ -n "$bad_new" ]; then
        warn "authentik blueprints: ${bad_new} being re-applied after a recent change — usually settles within a few minutes; run status again shortly"
    else
        pass "authentik blueprints: ${total} instances, all successful"
    fi
    return 0
}
# STATUS_AUTHENTIK_BLUEPRINTS-END
# #2420: a RAG consumer's configured embedding dimension must equal what the
# box's LLM endpoint emits for its configured model — a disagreeing pair makes
# every vector write fail while the consumer keeps answering 200. Measured, not
# read from a manifest (the same alias emits 2560 on one backend and 4096 on
# another). One embeddings call per active consumer, read-only.
# STATUS_EMBED_SPACE-BEGIN
# #2435: an index built under one embedding space and queried in another still
# answers — at flat scores, with the wrong neighbours. The only honest check is
# to re-embed a few stored chunks per index and compare (scripts/embedding_space_check.py).
status_embedding_space_rows() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres || { skip "embedding spaces: postgres not running"; return 0; }
    local deps="" line verdict text
    if declare -F _llmm_admin_api >/dev/null 2>&1 || \
       . "${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/cli/lib-llm-manager-deploy.sh" 2>/dev/null; then
        deps="$(_llmm_admin_api GET /api/deployments 2>/dev/null || true)"
    fi
    local key; key=$(env_get LLM_MANAGER_OWUI_KEY)
    local out rc=0
    out=$(DEPLOYMENTS_JSON="$deps" EMBED_KEY="$key" \
          EMBED_URL="http://127.0.0.1:$(env_get LLM_MANAGER_PORT | grep -E '^[0-9]+$' || echo 8091)/v1/embeddings" \
          POSTGRES_USER="$(env_get POSTGRES_USER)" DIFY_DB="$(env_get DIFY_DB)" OPENWEBUI_DB="$(env_get OPENWEBUI_DB)" \
          LIGHTRAG_DB="$(env_get LIGHTRAG_DB)" LIGHTRAG_EMBEDDING_MODEL="$(env_get LIGHTRAG_EMBEDDING_MODEL)" \
          OWUI_EMBEDDING_MODEL="$(env_get OWUI_EMBEDDING_MODEL_APPLIED)" \
          COMPOSE_PROFILES="$(env_get COMPOSE_PROFILES)" \
          python3 "${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/embedding_space_check.py" 2>/dev/null) || rc=$?
    # #2536 item 12: print what came back, then say so when the checker died —
    # also after its first (pooling) line. Nothing to check + exit 0 stays quiet.
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        verdict="${line%%|*}"; text="${line#*|}"
        case "$verdict" in
            PASS) pass "$text" ;;
            FAIL) fail "$text" ;;
            WARN) warn "$text" ;;
            INFO) info "$text" ;;
        esac
    done <<< "$out"
    if [ "$rc" -ne 0 ]; then
        warn "embedding spaces: the check exited ${rc}$([ -z "$out" ] && echo ' and produced no verdict' || echo ' — its verdicts may be incomplete') — run scripts/embedding_space_check.py by hand to see why"
    fi
    return 0
}
# STATUS_EMBED_SPACE-END

# #2673 / #2672: Open WebUI 0.11 hides a resource without a read grant from
# every non-admin — and admins never notice, because they bypass the check.
# Names each platform chat model and registry MCP tool server a user cannot
# see (scripts/owui_access_check.py). Read-only; silent without chat/postgres.
# STATUS_OWUI_ACCESS-BEGIN
status_owui_access_rows() {
    case ",$(env_get COMPOSE_PROFILES | tr -d ' ')," in *,chat,*) ;; *) return 0 ;; esac
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres || return 0
    local out rc=0 line verdict text
    out=$(OPENWEBUI_DB="$(env_get OPENWEBUI_DB)" POSTGRES_USER="$(env_get POSTGRES_USER)" \
          python3 "${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/owui_access_check.py" 2>/dev/null) || rc=$?
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        verdict="${line%%|*}"; text="${line#*|}"
        case "$verdict" in
            PASS) pass "$text" ;;
            WARN) warn "$text" ;;
            INFO) info "$text" ;;
        esac
    done <<< "$out"
    if [ "$rc" -ne 0 ]; then
        warn "Open WebUI access check exited ${rc} — run scripts/owui_access_check.py by hand to see why"
    fi
    return 0
}
# STATUS_OWUI_ACCESS-END

# #3494: Open WebUI 0.12 gates users' own (direct) tool servers behind a
# server-wide switch (direct.integrations.enable). Users whose permission still
# admits a stored server lose it from chat without a word while it is off; while
# it is on, those servers are called from the browser and bypass the MCP
# gateway. Read-only; silent without chat/postgres and when nobody holds a server.
status_owui_direct_integrations_row() {
    case ",$(env_get COMPOSE_PROFILES | tr -d ' ')," in *,chat,*) ;; *) return 0 ;; esac
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres || return 0
    local out rc=0 switch users admitted db pg_user
    local bypass="such user-configured tool servers bypass the MCP gateway's audit and its availability/off switch"
    db=$(env_get OPENWEBUI_DB); pg_user=$(env_get POSTGRES_USER)
    out=$(python3 "${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/owui_config_reconcile.py" \
          direct-integrations --check --db "${db:-openwebui_db}" --pg-user "${pg_user:-docker}" 2>/dev/null) || rc=$?
    [ "$rc" -eq 3 ] && return 0   # legacy single-row schema: nothing to judge yet
    if [ "$rc" -ne 0 ]; then
        warn "Open WebUI direct tool server check exited ${rc} — run scripts/owui_config_reconcile.py direct-integrations --check by hand to see why"
        return 0
    fi
    switch=$(printf '%s\n' "$out" | sed -n 's/^SWITCH=//p')
    users=$(printf '%s\n' "$out" | sed -n 's/^USERS=//p')
    admitted=$(printf '%s\n' "$out" | sed -n 's/^ADMITTED=//p')
    case "$users" in ''|*[!0-9]*) users=0 ;; esac
    case "$admitted" in ''|*[!0-9]*) admitted=0 ;; esac
    [ "$users" -gt 0 ] || return 0
    if [ "$switch" = on ]; then
        info "Open WebUI: Direct Integrations is on and ${users} user(s) have their own tool servers — ${bypass}"
    elif [ "$admitted" -gt 0 ]; then
        warn "Open WebUI: ${admitted} user(s) have their own tool servers, but Direct Integrations is off — their servers do not reach chat. Switch on: Open WebUI Admin Settings > Connections > Direct Integrations (note: ${bypass})"
    fi
    return 0
}

# #2471: a module whose database holds data while a key that encrypts it is
# empty — the state a new key would make permanent. Read-only.
status_data_bound_secret_rows() {
    local pairs p msg profiles
    command -v python3 >/dev/null 2>&1 || return 0
    pairs=$(PYTHONPATH="$SCRIPT_DIR/core/common" python3 -m razzfazz_common.data_bound_secrets all \
            --profiles-yaml "$SCRIPT_DIR/core/config/profiles.yaml" 2>/dev/null) || return 0
    profiles=",$(env_get COMPOSE_PROFILES),"
    for p in $(printf '%s\n' "$pairs" | awk 'NF==2 {print $1}' | sort -u); do
        if [ "$p" != core ]; then
            case "$profiles" in *",$p,"*) ;; *) continue ;; esac
        fi
        if ! msg=$(PYTHONPATH="$SCRIPT_DIR/core/common" python3 -m razzfazz_common.data_bound_secrets check \
                   --profile "$p" --profiles-yaml "$SCRIPT_DIR/core/config/profiles.yaml" --env "$ENV_FILE" 2>&1); then
            fail "${msg}"
        fi
    done
}

# STATUS_EMBED_DIM-BEGIN
# #2506: cognee ran at 2560 while status read .env's 4096 and said PASS — the
# RUNNING container had been created before an .env edit, and nothing
# recreated it. `.env` is the box's INTENT; the container's own env is what it
# is actually doing right now, so that is what this row measures. `.env` is
# used only as a fallback when the container cannot be asked (not running, or
# the docker CLI can't reach it), and that fallback is named in the row so a
# PASS built on it is never mistaken for a live measurement.
status_embedding_dim_rows() {
    local key port pair profile prefix container dim_key model_key rest model dim measured note running
    key=$(env_get LLM_MANAGER_OWUI_KEY); port=$(env_get LLM_MANAGER_PORT); [ -n "$port" ] || port=8091
    for pair in "cognee:COGNEE:cognee:EMBEDDING_DIMENSIONS:EMBEDDING_MODEL" \
                "lightrag:LIGHTRAG:lightrag:EMBEDDING_DIM:EMBEDDING_MODEL"; do
        profile="${pair%%:*}"; rest="${pair#*:}"
        prefix="${rest%%:*}"; rest="${rest#*:}"
        container="${rest%%:*}"; rest="${rest#*:}"
        dim_key="${rest%%:*}"; model_key="${rest#*:}"
        case ",$(env_get COMPOSE_PROFILES)," in *",${profile},"*) ;; *) continue ;; esac
        running=""
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$container"; then
            running=1
        fi
        model=""; dim=""
        if [ -n "$running" ]; then
            model=$(docker exec "$container" printenv "$model_key" 2>/dev/null)
            dim=$(docker exec "$container" printenv "$dim_key" 2>/dev/null)
        fi
        if [ -n "$model" ] && [ -n "$dim" ]; then
            note=""
        else
            model=$(env_get "${prefix}_EMBEDDING_MODEL"); dim=$(env_get "${prefix}_EMBEDDING_DIM")
            note=" (from .env; container not running)"
        fi
        if [ -z "$model" ]; then
            fail "${profile}: ${prefix}_EMBEDDING_MODEL is empty — run 'rzfz post-install --refresh'"; continue
        fi
        if [ -z "$key" ]; then
            warn "${profile}: embedding dimension of ${model} not measured (no LLM_MANAGER_OWUI_KEY) — ${container} ${dim_key}=${dim:-<empty>}${note} unverified"; continue
        fi
        measured=$(curl -sf --max-time 20 -K <(printf 'header = "Authorization: Bearer %s"\n' "$key") -H "Content-Type: application/json"             "http://127.0.0.1:${port}/v1/embeddings" -d "{\"model\":\"${model}\",\"input\":\"x\"}" 2>/dev/null             | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data'][0]['embedding']))" 2>/dev/null)
        if ! printf '%s' "$measured" | grep -Eq '^[0-9]+$'; then
            fail "${profile}: ${model} is not served by the LLM Manager — vector writes cannot work; run 'rzfz post-install --refresh'"
        elif [ "$measured" = "$dim" ]; then
            pass "${profile}: ${model} emits ${measured}-dim vectors, ${container}'s ${dim_key}=${dim}${note} agrees"
        else
            fail "${profile}: ${container}'s ${dim_key}=${dim:-<empty>}${note} but ${model} emits ${measured}-dim vectors — every vector write fails silently; run 'rzfz post-install --refresh'"
        fi
    done
    # cognee logs the failure it swallows; surface it.
    if case ",$(env_get COMPOSE_PROFILES)," in *,cognee,*) true ;; *) false ;; esac && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx cognee; then
        local n
        n=$(docker logs --since 24h cognee 2>&1 | grep -c "expected [0-9]* dimensions, not" || true)
        [ "${n:-0}" -eq 0 ] || fail "cognee: ${n} dimension-mismatch write failure(s) in the last 24 h ('expected N dimensions, not M') — the brain is not being indexed"
    fi
    return 0
}
# STATUS_EMBED_DIM-END

# STATUS_CHAT_MODEL-BEGIN
# #2815: cognee/lightrag configured with a chat model the LLM Manager does not
# serve (the template default on a box that serves only the 4B): every cognify
# call is refused with 400 "Invalid model name" and retried with backoff for
# ever, the container is healthy and the brain stays empty. The value the RUNNING
# container uses is compared with the manager's /v1/models; .env is the fallback.
status_rag_chat_model_rows() {
    local key port pair profile prefix model note served names
    key=$(env_get LLM_MANAGER_OWUI_KEY); port=$(env_get LLM_MANAGER_PORT); [ -n "$port" ] || port=8091
    for pair in "cognee:COGNEE" "lightrag:LIGHTRAG"; do
        profile="${pair%%:*}"; prefix="${pair#*:}"
        case ",$(env_get COMPOSE_PROFILES)," in *",${profile},"*) ;; *) continue ;; esac
        model=""; note=""
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$profile"; then
            model=$(docker exec "$profile" printenv LLM_MODEL 2>/dev/null)
        fi
        if [ -z "$model" ]; then
            model=$(env_get "${prefix}_LLM_MODEL"); note=" (from .env; container not running)"
        fi
        if [ -z "$model" ]; then
            fail "${profile}: ${prefix}_LLM_MODEL is empty - run 'rzfz post-install --refresh'"; continue
        fi
        if [ -z "$key" ]; then
            warn "${profile}: chat model ${model}${note} not checked (no LLM_MANAGER_OWUI_KEY)"; continue
        fi
        served=$(curl -s --max-time 10 -K <(printf 'header = "Authorization: Bearer %s"\n' "$key") \
            "http://127.0.0.1:${port}/v1/models" 2>/dev/null \
            | python3 -c 'import sys, json
for m in json.load(sys.stdin).get("data", []):
    print(m.get("id", ""))' 2>/dev/null)
        if [ -z "$served" ]; then
            warn "${profile}: chat model ${model}${note} not checked (the LLM Manager's /v1/models returned no models)"; continue
        fi
        names=$(printf '%s' "$served" | paste -sd, - | sed 's/,/, /g')
        if printf '%s\n' "$served" | grep -qxF -- "${model#openai/}"; then
            pass "${profile}: chat model ${model}${note} is served by the LLM Manager"
        else
            fail "${profile}: chat model ${model}${note} is not served by the LLM Manager (served: ${names}) - every request is refused with 400 'Invalid model name'; run 'rzfz post-install --refresh'"
        fi
    done
    return 0
}
# STATUS_CHAT_MODEL-END

# #2404: the upstream Cognee development account must not exist as a login,
# and the only superuser should be the stack identity. The users table is the
# authoritative half; the login probe is the live half — and the two halves
# must agree, or the row says UNVERIFIED rather than green.
# STATUS_DIFY_DATASETS-BEGIN
# #2427: a Dify knowledge base bound to a (provider, model) pair that no provider
# serves lists its documents and chunk count and fails only when someone
# retrieves, with a message about credentials. The row names every such base.
status_dify_dataset_provider_row() {
    case ",$(env_get COMPOSE_PROFILES)," in *,dify,*) ;; *) return 0 ;; esac
    command -v docker >/dev/null 2>&1 || { skip "dify knowledge bases: docker CLI not available"; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres || { skip "dify knowledge bases: postgres not running"; return 0; }
    local db total dead
    db=$(env_get DIFY_DB); [ -n "$db" ] || db="dify_db"
    total=$(docker exec postgres psql -U "$(env_get POSTGRES_USER)" -d "$db" -tAc "SELECT count(*) FROM datasets WHERE indexing_technique = 'high_quality';" 2>/dev/null | tr -d '[:space:]')
    case "${total:-}" in ''|*[!0-9]*) warn "dify knowledge bases: datasets table not readable — UNVERIFIED"; return 0 ;; esac
    [ "$total" -gt 0 ] || return 0
    dead=$(docker exec postgres psql -U "$(env_get POSTGRES_USER)" -d "$db" -tAc "SELECT d.name || ' (' || d.embedding_model_provider || ' / ' || d.embedding_model || ')' FROM datasets d WHERE d.indexing_technique = 'high_quality' AND NOT EXISTS (SELECT 1 FROM provider_models pm WHERE pm.provider_name = d.embedding_model_provider AND pm.model_name = d.embedding_model);" 2>/dev/null | sed '/^$/d' | tr '\n' ';' | sed 's/;$//; s/;/, /g')
    if [ -n "$dead" ]; then
        fail "dify: knowledge base(s) whose embedding model no provider serves — retrieval fails: ${dead}; run 'rzfz post-install --refresh'"
    else
        pass "dify: all ${total} knowledge base(s) resolve to a served embedding model"
    fi
    return 0
}
# STATUS_DIFY_DATASETS-END

# #3233: one structured-output node path (boolean field) on Dify's default chat
# model — capability, call and validation with Dify's own code, no app; bounded.
status_dify_structured_output_row() {
    case ",$(env_get COMPOSE_PROFILES)," in *,dify,*) ;; *) return 0 ;; esac
    command -v docker >/dev/null 2>&1 || { skip "dify structured output: docker CLI not available"; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx dify-api || { skip "dify structured output: dify-api not running"; return 0; }
    local db user email row verdict text
    db=$(env_get DIFY_DB); [ -n "$db" ] || db="dify_db"
    user=$(env_get POSTGRES_USER); [ -n "$user" ] || user="docker"
    email="razzfazz-ai-admin@$(env_get MAIN_DOMAIN)"
    if [ "$(docker exec postgres psql -U "$user" -d "$db" -tAc "SELECT 1 FROM accounts WHERE email='${email}' LIMIT 1;" 2>/dev/null | tr -d '[:space:]')" != 1 ]; then
        email=$(docker exec postgres psql -U "$user" -d "$db" -tAc "SELECT email FROM accounts ORDER BY created_at LIMIT 1;" 2>/dev/null | tr -d '[:space:]')
    fi
    row=$(razzfazz_dify_structured_output_probe "$email")
    verdict=$(printf '%s' "$row" | cut -d'|' -f2)
    text=$(printf '%s' "$row" | cut -d'|' -f3-)
    case "$verdict" in
        OK)      pass "dify: a structured-output node with a boolean field runs on the default chat model" ;;
        REFUSED) fail "dify: structured-output workflow nodes are refused: ${text}; run 'rzfz post-install --refresh'" ;;
        SKIP)    skip "dify structured output: ${text}" ;;
        *)       warn "dify structured output: no verdict — ${text}" ;;
    esac
    return 0
}

# STATUS_DIFY_VALKEY-BEGIN
# Dify keeps its own copies of the cache password in .env.dify (REDIS_PASSWORD
# and the password inside CELERY_BROKER_URL). When VALKEY_PASSWORD in .env was
# rotated without them, the Dify workers fail to authenticate and restart every
# half minute while every other row stays green. Compared as hashes: a value
# never reaches the output.
_status_hash8() { printf '%s' "$1" | sha256sum 2>/dev/null | cut -c1-8; }
status_dify_valkey_sync_row() {
    local dfile="${DIFY_ENV_FILE:-$SCRIPT_DIR/.env.dify}"
    [ -f "$dfile" ] || return 0
    local want redis broker bad=""
    want=$(env_get VALKEY_PASSWORD)
    [ -n "$want" ] || return 0
    redis=$(grep '^REDIS_PASSWORD=' "$dfile" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r')
    broker=$(grep '^CELERY_BROKER_URL=' "$dfile" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' \
             | sed -n 's#^redis://[^:/@]*:\(.*\)@[^@]*$#\1#p')
    [ -z "$redis" ] || [ "$(_status_hash8 "$redis")" = "$(_status_hash8 "$want")" ] || bad="REDIS_PASSWORD"
    [ -z "$broker" ] || [ "$(_status_hash8 "$broker")" = "$(_status_hash8 "$want")" ] || bad="${bad:+$bad and }CELERY_BROKER_URL"
    if [ -n "$bad" ]; then
        fail "dify: ${bad} in .env.dify carries a different Valkey password than .env (workers cannot authenticate); run 'rzfz upgrade' to sync it, then recreate the dify services"
    elif [ -n "$redis$broker" ]; then
        pass "dify: .env.dify carries the same Valkey password as .env"
    fi
    return 0
}

# A container that keeps restarting is broken whatever its last status line
# says. RestartCount is cumulative, so a count alone would flag a service that
# was fixed days ago: it has to be restarting now (or have started within the
# last ${RZFZ_STATUS_CRASHLOOP_WINDOW:-600}s) as well.
status_crashloop_rows() {
    command -v docker >/dev/null 2>&1 || return 0
    local threshold="${RZFZ_STATUS_CRASHLOOP_THRESHOLD:-5}" window="${RZFZ_STATUS_CRASHLOOP_WINDOW:-600}"
    local ids line name count st started up_s now bad="" n=0
    ids=$(docker ps -aq 2>/dev/null | tr '\n' ' ')
    [ -n "$ids" ] || return 0
    now=$(date +%s)
    # shellcheck disable=SC2086
    while IFS='|' read -r name count st started; do
        [ -n "$name" ] || continue
        case "$count" in ''|*[!0-9]*) continue ;; esac
        [ "$count" -ge "$threshold" ] || continue
        if [ "$st" != restarting ]; then
            up_s=$(date -d "$started" +%s 2>/dev/null) || continue
            [ $((now - up_s)) -lt "$window" ] || continue
        fi
        n=$((n + 1))
        [ "$n" -gt 5 ] || bad="${bad:+$bad, }${name#/} (${count} restarts)"
    done < <(docker inspect --format '{{.Name}}|{{.RestartCount}}|{{.State.Status}}|{{.State.StartedAt}}' $ids 2>/dev/null)
    if [ "$n" -gt 0 ]; then
        fail "${n} container(s) crash-looping: ${bad}; read their logs with 'rzfz logs <service>'"
    else
        pass "no crash-looping containers"
    fi
    return 0
}
# STATUS_DIFY_VALKEY-END

# STATUS_DELIVERY_BLOB-BEGIN
status_delivery_blob_row() {
    # #2913: a full factory reset restores the password sealed in
    # .delivery-credentials.enc. When it is no longer the admin password .env
    # holds (the box was handed over with set-admin-password by a release that
    # did not re-seal), a reset would put an OLD password back. Nothing is ever
    # printed of either value. A box without the blob (not set up for delivery)
    # says nothing.
    # The row reports every outcome (#2913 follow-up): a match used to be silent,
    # which could not be told from a check that did not run at all.
    [ -f "${SCRIPT_DIR}/.delivery-credentials.enc" ] || return 0
    # shellcheck source=scripts/lib-delivery-blob.sh disable=SC1091
    source "${SCRIPT_DIR}/scripts/lib-delivery-blob.sh"
    local st=0
    delivery_blob_state "${SCRIPT_DIR}/.env" || st=$?
    case "$st" in
        0) pass "delivery credentials: a factory reset restores the current admin password" ;;
        1) warn "delivery credentials: the sealed password is not the admin password in .env — a factory reset would restore an old one; run: rzfz set-admin-password --all" ;;
        3) info "delivery credentials: not checked — .env holds no admin password to compare with" ;;
        *) info "delivery credentials: not checked — the sealed file could not be read (it is readable by root only; run: sudo rzfz status)" ;;
    esac
    return 0
}
# STATUS_DELIVERY_BLOB-END

# #3044: Vaultwarden with a mail host it cannot reach refuses every first master
# password. The verdict (scripts/lib.sh vaultwarden_smtp_verdict) is shared with
# the post-install verify suite.
# STATUS_VAULTWARDEN_SMTP-BEGIN
status_vaultwarden_smtp_row() {
    case ",$(env_get COMPOSE_PROFILES)," in *,vaultwarden,*) ;; *) return 0 ;; esac
    local verdict
    verdict=$(vaultwarden_smtp_verdict "$(env_get VAULTWARDEN_SMTP_HOST)")
    case "${verdict%%|*}" in
        pass) pass "${verdict#*|}" ;;
        fail) fail "${verdict#*|}" ;;
        skip) skip "${verdict#*|}" ;;
        *)    info "${verdict#*|}" ;;
    esac
    return 0
}
# STATUS_VAULTWARDEN_SMTP-END

# STATUS_OPENLIT_DEFAULT-BEGIN
status_openlit_default_login_row() {
    # #2900: OpenLIT's only login is the upstream seed account with a PUBLISHED
    # password until the stack admin replaces it. The enable hook LOCKS the seed
    # (OpenLIT re-creates a missing one on every start) and marks it by name,
    # after proving with real sign-ins that the public pair is refused; the
    # marker is the "locked" verdict (module_hooks.OPENLIT_LOCKED_NAME).
    # Otherwise the probe is the bcrypt check of that public pair against the
    # stored hash, inside the OpenLIT container
    # (the NextAuth credentials flow does exactly this compare); nothing secret
    # is printed, and the public default is only compared, never echoed.
    case ",$(env_get COMPOSE_PROFILES)," in *,observability,*) ;; *) return 0 ;; esac
    command -v docker >/dev/null 2>&1 || { skip "openlit default login: docker CLI not available"; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx openlit || { skip "openlit default login: openlit not running"; return 0; }
    local seed="user@openlit.io" out verdict
    out=$(docker exec openlit sh -c 'cd /app/client && node -e "
const { PrismaClient } = require(\"@prisma/client\");
const p = new PrismaClient();
p.user.findFirst({ where: { email: \"user@openlit.io\" } }).then(u => {
  if (!u) { console.log(\"RZFZ-OPENLIT-SEED absent\"); process.exit(0); }
  if (u.name === \"Sign-in disabled (public default login)\") { console.log(\"RZFZ-OPENLIT-SEED locked\"); process.exit(0); }
  let b = null;
  for (const m of [\"bcryptjs\", \"bcrypt\", \"bcrypt-ts\"]) { try { b = require(m); break; } catch (e) {} }
  if (!b || !b.compareSync) { console.log(\"RZFZ-OPENLIT-SEED unverifiable\"); process.exit(0); }
  console.log(\"RZFZ-OPENLIT-SEED \" + (b.compareSync(\"openlituser\", u.password) ? \"default\" : \"changed\"));
  process.exit(0);
}).catch(e => { console.log(\"RZFZ-OPENLIT-SEED error\"); process.exit(0); });
"' 2>/dev/null | sed -n 's/^RZFZ-OPENLIT-SEED //p' | tail -1)
    verdict="${out:-error}"
    case "$verdict" in
        default)      fail "openlit: the upstream seed login ${seed} AUTHENTICATES with its public default password — run 'rzfz post-install --refresh'" ;;
        unverifiable) fail "openlit: the upstream seed login ${seed} exists and its password could not be compared with the public default — run 'rzfz post-install --refresh'" ;;
        changed)      warn "openlit: the upstream seed login ${seed} still exists (password changed from the default); the stack admin login is razzfazz-ai-admin@$(env_get MAIN_DOMAIN)" ;;
        absent)       pass "openlit: no upstream seed login; the public default password is refused" ;;
        locked)       pass "openlit: the upstream seed login ${seed} is locked by the stack (its public default password is refused); the admin login is razzfazz-ai-admin@$(env_get MAIN_DOMAIN)" ;;
        *)            warn "openlit default login: user table not readable — UNVERIFIED" ;;
    esac
    return 0
}
# STATUS_OPENLIT_DEFAULT-END

# STATUS_COGNEE_DEFAULT-BEGIN
status_cognee_default_account_row() {
    # status.sh reads profiles from .env (env_get); it never has COMPOSE_PROFILES
    # set — the earlier gate on that variable returned silently on every box.
    case ",$(env_get COMPOSE_PROFILES)," in *,cognee,*) ;; *) return 0 ;; esac
    command -v docker >/dev/null 2>&1 || { skip "cognee default account: docker CLI not available"; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres || { skip "cognee default account: postgres not running"; return 0; }
    local db stack_id rows upstream="default_user@example.com" up_row others="" email su active
    db=$(env_get COGNEE_DB); [ -n "$db" ] || db="cognee_db"
    stack_id="razzfazz-ai-admin@$(env_get MAIN_DOMAIN)"
    rows=$(docker exec postgres psql -U "$(env_get POSTGRES_USER)" -d "$db" -tAc "SELECT email || '|' || is_superuser || '|' || is_active FROM users;" 2>/dev/null | sed '/^$/d')
    [ -n "$rows" ] || { warn "cognee default account: users table not readable — UNVERIFIED"; return 0; }
    while IFS='|' read -r email su active; do
        [ -n "$email" ] || continue
        # Postgres prints `bool || text` as true/false (#2404): normalize to t/f.
        case "$su" in t|true) su=t ;; *) su=f ;; esac
        case "$active" in t|true) active=t ;; *) active=f ;; esac
        [ "$email" = "$upstream" ] && up_row="su=${su},active=${active}"
        [ "$su" = "t" ] && [ "$email" != "$stack_id" ] && [ "$email" != "$upstream" ] && others="${others}${others:+, }${email}"
    done <<< "$rows"
    local code
    code=$(docker exec cognee sh -c 'curl -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST http://localhost:8000/api/v1/auth/login -H "Content-Type: application/x-www-form-urlencoded" --data-urlencode "username=default_user@example.com" --data-urlencode "password=default_password"' 2>/dev/null | tr -cd '0-9' | tail -c 3)
    if [ "$code" = "200" ]; then
        fail "cognee: the upstream development account ${upstream} AUTHENTICATES (HTTP 200) — a superuser session without Authentik; run 'rzfz post-install --refresh'"
    elif [ -n "$up_row" ] && [ "${up_row##*active=}" = "t" ]; then
        fail "cognee: ${upstream} exists and is active (${up_row}) — run 'rzfz post-install --refresh'"
    elif [ -n "$up_row" ]; then
        pass "cognee: ${upstream} present but deactivated (${up_row}); login refused (HTTP ${code:-000})"
    else
        pass "cognee: no upstream development account; login refused (HTTP ${code:-000})"
    fi
    [ -z "$others" ] || warn "cognee: superuser(s) besides the stack identity: ${others}"
    return 0
}
# STATUS_COGNEE_DEFAULT-END

# #3303: Google sign-in still admitting the old template default domain on a
# public-channel box (scripts/lib.sh::razzfazz_google_domain_is_old_default).
# Only a WARN: the value may be intended, and status never changes it.
status_google_domain_old_default_row() {
    razzfazz_google_domain_is_old_default "$ENV_FILE" || return 0
    warn "Google sign-in allowed domain: $(razzfazz_google_domain_old_default_advice)"
}

# #3512: a Google client id with no allowed domain. Every reader treats an unset
# or empty GOOGLE_OAUTH_DOMAIN as "admit nobody", but only once the SSO setup
# has rendered it: the .env alone does not say what Authentik enforces, and
# Gitea's Google source (added once, behind its marker) is never updated.
status_google_domain_unset_row() {
    local d
    [ -n "$(env_get GOOGLE_CLIENT_ID)" ] || return 0
    d=$(env_get GOOGLE_OAUTH_DOMAIN | tr -d ', \t')
    [ -z "$d" ] || return 0
    warn "Google sign-in is configured with no allowed domain (GOOGLE_OAUTH_DOMAIN); re-run the SSO setup if it was set before: docker compose up -d authentik-init && docker wait authentik-init"
    if docker exec gitea test -f /data/gitea/.google_oauth_configured >/dev/null 2>&1; then
        warn "Gitea's Google source still restricts to a domain set earlier; remove or update it in Gitea admin (Site Administration → Authentication Sources)"
    fi
}

# #2535: an SSO switch that is on without its credentials does nothing, and
# Open WebUI's derived login keys can disagree with the switches until the
# next recreate. Rules are ConfigManager.sso_switch_findings (parity: test_2535).
# STATUS_SSO_SWITCHES-BEGIN
status_sso_switch_rows() {
    local sw needs k missing bad=0 tmp
    for sw in "ENABLE_GOOGLE_OAUTH:GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET" \
              "ENABLE_ENTRA_OAUTH:ENTRA_CLIENT_ID ENTRA_CLIENT_SECRET ENTRA_TENANT_ID" \
              "ENABLE_OPENWEBUI_OIDC:OPENWEBUI_OIDC_CLIENT_ID OPENWEBUI_OIDC_CLIENT_SECRET" \
              "ENABLE_GITEA_AUTHENTIK_OIDC:GITEA_OIDC_CLIENT_ID GITEA_OIDC_CLIENT_SECRET"; do
        needs="${sw#*:}"; sw="${sw%%:*}"
        [ "$(env_get "$sw")" = true ] || continue
        missing=""
        for k in $needs; do [ -n "$(env_get "$k")" ] || missing="$missing $k"; done
        if [ -n "$missing" ]; then
            fail "$sw is on, but${missing} empty — the switch has no effect (Settings → Auth)"
            bad=1
        fi
    done
    tmp=$(mktemp "${TMPDIR:-/tmp}/rzfz-sso.XXXXXX")
    cp "$ENV_FILE" "$tmp"
    if [ -n "$(derive_owui_sso_env "$tmp")" ]; then
        fail "Open WebUI's login keys are not applied to the SSO switches — run: rzfz post-install --refresh"
        bad=1
    fi
    rm -f "$tmp"
    # #2933: what the RUNNING Open WebUI got. It lists the Authentik provider
    # (and its sign-in button) iff OAUTH_CLIENT_ID and OPENID_PROVIDER_URL are
    # both non-empty (open-webui v0.12.4 config.py:2805). Switch off + provider
    # advertised = a button that answers 500; switch on + none = no native login.
    # Silent when the container is not running (nothing to read).
    local owui_env owui_url owui_cid
    owui_env=$(docker inspect openwebui --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null) || owui_env=""
    if [ -n "$owui_env" ]; then
        owui_url=$(printf '%s\n' "$owui_env" | sed -n 's/^OPENID_PROVIDER_URL=//p' | head -n 1)
        owui_cid=$(printf '%s\n' "$owui_env" | sed -n 's/^OAUTH_CLIENT_ID=//p' | head -n 1)
        if [ "$(env_get ENABLE_OPENWEBUI_OIDC)" = true ]; then
            if [ -z "$owui_url" ] || [ -z "$owui_cid" ]; then
                warn "Open WebUI: ENABLE_OPENWEBUI_OIDC is on but the running container has no OIDC provider configured — run: rzfz post-install --refresh"
                bad=1
            fi
        elif [ -n "$owui_url" ] && [ -n "$owui_cid" ]; then
            warn "Open WebUI offers 'Sign in with Authentik' but ENABLE_OPENWEBUI_OIDC is off (no openwebui-oidc provider exists, the button answers HTTP 500) — recreate it: rzfz upgrade, or docker compose up -d openwebui"
            bad=1
        fi
    fi
    [ "$bad" = 1 ] || pass "SSO switches: every enabled switch has its credentials; Open WebUI's login keys follow them"
    return 0
}
# STATUS_SSO_SWITCHES-END

# #2473 D7: the technical-user owner check (Config Portal, every 5 min, only
# with AUTHENTIK_BOOTSTRAP_TOKEN): accounts whose revocation keeps failing, an
# Authentik it could not read, and a check that stopped. No file = the check
# never ran here (no technical users' Authentik token) - silent.
# The Settings portal's single-file binds of .env and .env.dify, measured where
# they exist: inside its container. It repairs a stale bind by itself on the next
# change, so a stale bind alone is a warning. A repair that did not help, or a
# stale bind right after one, needs the host command.
# STATUS_PORTAL_ENV_BIND-BEGIN
status_portal_env_bind_row() {
    local out rc bind_state bind_files heal_state heal_at heal_err recreate
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx razzfazz-config || return 0
    recreate="cd $(printf '%q' "${SCRIPT_DIR:-.}") && docker compose up -d --force-recreate --no-deps razzfazz-config"
    rc=0
    out=$(timeout 25 docker exec razzfazz-config python3 -c \
        'from app.services.self_heal import status_report; status_report()' 2>/dev/null) || rc=$?
    bind_state=$(printf '%s\n' "$out" | awk -F'\t' '$1 == "BIND" {print $2}')
    if [ "$rc" -ne 0 ] || [ -z "$bind_state" ]; then
        warn "Settings portal .env bind: could not be measured - docker logs razzfazz-config"
        return 0
    fi
    bind_files=$(printf '%s\n' "$out" | awk -F'\t' '$1 == "BIND" {print $3}')
    heal_state=$(printf '%s\n' "$out" | awk -F'\t' '$1 == "HEAL" {print $2}')
    heal_at=$(printf '%s\n' "$out" | awk -F'\t' '$1 == "HEAL" {print $3}')
    heal_err=$(printf '%s\n' "$out" | awk -F'\t' '$1 == "HEAL" {print $4}')
    if [ "$bind_state" = stale ]; then
        if [ "$heal_state" = failed ]; then
            fail "Settings portal .env bind: broken (${bind_files}), and the portal's own repair failed at $(date -d "@${heal_at:-0}" '+%F %T' 2>/dev/null) (${heal_err:-no detail}) - run: ${recreate}"
        else
            warn "Settings portal .env bind: stale (${bind_files}) - the portal reconnects on the next change, or run: ${recreate}"
        fi
    elif [ "$heal_state" = failed ]; then
        warn "Settings portal .env bind: in effect now, but the portal's last repair failed at $(date -d "@${heal_at:-0}" '+%F %T' 2>/dev/null) (${heal_err:-no detail})"
    else
        pass "Settings portal .env bind: in effect"
    fi
    return 0
}
# STATUS_PORTAL_ENV_BIND-END

# STATUS_TECHNICAL_USER_OWNER_CHECK-BEGIN
status_technical_user_owner_check_row() {
    local f last age line
    f="${STACK_ROOT:-.}/.technical-users-state/owner-check.status"
    [ -s "$f" ] || return 0
    last=$(sed -n 's/^LAST_RUN\t\([0-9][0-9]*\)$/\1/p' "$f" | head -n 1)
    if [ -z "$last" ]; then
        warn "technical-user owner check: status file unreadable - docker logs razzfazz-config"
        return 0
    fi
    age=$(( $(date +%s) - last ))
    if [ "$age" -gt 1800 ]; then
        warn "technical-user owner check: has not run for $((age / 60)) min - docker logs razzfazz-config"
        return 0
    fi
    if grep -q '^WARN	' "$f"; then
        while IFS= read -r line; do
            warn "technical-user owner check: ${line#WARN	}"
        done < <(grep '^WARN	' "$f")
    else
        pass "technical-user owner check: last run $((age / 60)) min ago, nothing to report"
    fi
    return 0
}
# STATUS_TECHNICAL_USER_OWNER_CHECK-END

# #2476 D7: the Dify secret_id rotator's warnings (unpublished drafts, low uses,
# near expiry) and a rotator that stopped. It runs hourly in the Config Portal
# once OpenBao is on; the status file holds no secret (accessors only).
# STATUS_SECRET_ID_ROTATION-BEGIN
status_secret_id_rotation_row() {
    local profiles f last age line
    profiles=",$(env_get COMPOSE_PROFILES),"
    case "$profiles" in *,openbao,*) ;; *) return 0 ;; esac
    f="${STACK_ROOT:-.}/.openbao-state/secret-id-rotation.status"
    if [ ! -s "$f" ]; then
        info "secret_id rotation: no run yet (no Dify app bound to a technical user, or the Config Portal just started)"
        return 0
    fi
    last=$(sed -n 's/^LAST_RUN\t\([0-9][0-9]*\)$/\1/p' "$f" | head -n 1)
    if [ -z "$last" ]; then
        warn "secret_id rotation: status file unreadable - docker logs razzfazz-config"
        return 0
    fi
    age=$(( $(date +%s) - last ))
    if [ "$age" -gt 7200 ]; then
        warn "secret_id rotation: has not run for $((age / 3600)) h - docker logs razzfazz-config"
        return 0
    fi
    if grep -q '^WARN' "$f"; then
        while IFS= read -r line; do
            warn "secret_id rotation: ${line#WARN	}"
        done < <(grep '^WARN	' "$f")
    else
        pass "secret_id rotation: last run $((age / 60)) min ago, nothing to report"
    fi
    return 0
}
# STATUS_SECRET_ID_ROTATION-END

# #2473 D5: Dify, cognee and Synapse roles are reconciled from the role
# catalogue by `provision-sso-users --reconcile-roles`; this row runs its dry
# run and names every app whose accounts differ. The dry run's lines are read
# even when it exits non-zero: one app it could not check must not hide the
# verdict for the others, and must never read as "matches".
# STATUS_ROLE_DRIFT-BEGIN
status_role_drift_rows() {
    local out rc=0 tag app n m seen=0 ak_said=0 limit="${RZFZ_ROLE_DRIFT_TIMEOUT:-60}"
    local -A drift=() matched=() unchecked=() unmanaged=() unmanaged_admins=() accounts=()
    local -a order=()
    out=$(timeout "$limit" "${SCRIPT_DIR}/cli/provision-sso-users.sh" --reconcile-roles --app all --dry-run --summary 2>/dev/null </dev/null) || rc=$?
    while IFS=$'\t' read -r tag app n m; do
        case "$tag" in RZFZ-SCOPE*) continue ;; RZFZ-*) ;; *) continue ;; esac
        [ -n "$app" ] || continue
        seen=1
        [[ " ${order[*]} " == *" $app "* ]] || order+=("$app")
        case "$tag" in
            RZFZ-DRIFT)     drift[$app]=$n ;;
            RZFZ-MATCHED)   matched[$app]=$n ;;
            RZFZ-ACCOUNTS)  accounts[$app]=$n ;;
            RZFZ-UNMANAGED) unmanaged[$app]=$n; unmanaged_admins[$app]=${m:-0} ;;
            RZFZ-UNCHECKED) unchecked[$app]=$n ;;
            RZFZ-HELD)      warn "role reconcile: ${app} has ${n} hand-granted role(s) the catalogue no longer maps — left in place; review them in ${app}" ;;
            RZFZ-BANNED)    info "role reconcile: ${app} has ${n} account(s) disabled by hand — kept disabled" ;;
            RZFZ-REFUSED)   warn "role reconcile: ${app}: ${n} change(s) refused — they would remove the last ${app} admin" ;;
        esac
    done <<< "$out"
    for app in "${order[@]}"; do
        if [ -n "${unchecked[$app]:-}" ]; then
            case "${unchecked[$app]}" in
                "Authentik not answering"*)
                    # One row for the whole reconcile: the first unanswered call ended them all.
                    [ "$ak_said" -eq 1 ] || warn "role reconcile: ${unchecked[$app]} — drift NOT checked for any app"
                    ak_said=1 ;;
                *) warn "role reconcile: ${app} NOT checked — ${unchecked[$app]}" ;;
            esac
            continue
        fi
        [ -n "${drift[$app]+x}" ] || continue            # skipped (not running)
        n=${drift[$app]}
        case "$n" in ''|*[!0-9]*) warn "role reconcile: ${app}: unreadable drift count — NOT checked"; continue ;; esac
        m=${matched[$app]:-}
        case "$m" in ''|*[!0-9]*) m=0 ;; esac
        if [ "$m" -eq 0 ] && [ "${accounts[$app]:-}" = 0 ]; then
            # An app nobody has logged in to yet: nothing to check, nothing wrong.
            case "$app" in synapse) info "role reconcile: no Matrix accounts yet" ;;
                           *) info "role reconcile: no ${app} accounts yet" ;; esac
            continue
        fi
        if [ "$m" -eq 0 ]; then
            # Nothing matched is never "matches": the check saw no account.
            warn "role reconcile: ${app}: no account is linked to Authentik — ${unmanaged[$app]:-0} unmanaged (${unmanaged_admins[$app]:-0} admin); roles NOT checked"
            continue
        fi
        if [ "$n" -eq 0 ]; then pass "role reconcile: ${app} matches the catalogue (${m} account(s))"
        else warn "role reconcile: ${app} has ${n} pending change(s) — run: rzfz provision-sso-users --reconcile-roles --app ${app}"; fi
        if [ "${unmanaged[$app]:-0}" -gt 0 ]; then
            if [ "${unmanaged_admins[$app]:-0}" -gt 0 ]; then
                warn "role reconcile: ${app}: ${unmanaged[$app]} account(s) not linked to Authentik, ${unmanaged_admins[$app]} of them admin — not managed"
            else
                info "role reconcile: ${app}: ${unmanaged[$app]} account(s) not linked to Authentik — not managed"
            fi
        fi
    done
    if [ "$seen" -eq 0 ]; then
        if [ "$rc" -eq 124 ]; then warn "role reconcile: the dry run timed out after ${limit}s — drift NOT checked"
        else warn "role reconcile: the dry run failed (rc=${rc}) — drift NOT checked"; fi
    fi
    # The same dry run reports the knowledge-scope datasets (RZFZ-SCOPE* lines).
    [ "$seen" -eq 0 ] || status_scope_drift_rows "$out" "$ak_said"
    return 0
}
# STATUS_ROLE_DRIFT-END

# #2473 D6: a dataset in a knowledge scope is readable by exactly the scope's
# members and its owner. The role reconcile's dry run plans the dataset ACLs
# too; this reads its RZFZ-SCOPE* lines. A dataset that someone OUTSIDE its
# scope reads right now (a member removed from the scope, a team-wide dataset
# newly tagged, a dataset that left its scope with its readers) is a leak until
# the next reconcile: FAIL. Drift that only lacks a member is a WARN. A system
# that could not be checked never reads as "matches". Arg 2 = 1 when the role
# row already said Authentik is not answering (no second row for that).
# STATUS_SCOPE_DRIFT-BEGIN
status_scope_drift_rows() {
    local out=$1 ak_said=${2:-0} tag sys a b c nscopes="" said_all=0 checked=0
    while IFS=$'\t' read -r tag sys a b c; do
        case "$tag" in
            RZFZ-SCOPES) nscopes=$a ;;
            RZFZ-SCOPEUNCHECKED)
                case "$a" in "Authentik not answering"*) [ "$ak_said" -eq 1 ] && continue ;; esac
                if [ "$sys" = all ]; then
                    [ "$said_all" -eq 1 ] || warn "knowledge scopes: NOT checked — ${a}"
                    said_all=1
                else
                    warn "knowledge scopes: ${sys} NOT checked — ${a}"
                fi ;;
            RZFZ-SCOPEERR)
                warn "knowledge scopes: ${sys}: ${a} problem(s) — see: rzfz provision-sso-users --reconcile-roles --app ${sys} --dry-run" ;;
            RZFZ-SCOPEDRIFT)
                checked=1
                case "$a$b$c" in ''|*[!0-9]*) warn "knowledge scopes: ${sys}: unreadable drift count — NOT checked"; continue ;; esac
                if [ "$b" -gt 0 ]; then
                    fail "knowledge scopes: ${sys}: ${b} dataset(s) readable by accounts outside their scope — run: rzfz provision-sso-users --reconcile-roles --app ${sys}"
                elif [ "$a" -gt 0 ]; then
                    warn "knowledge scopes: ${sys}: ${a} dataset(s) differ from their scope (no one outside it reads them) — run: rzfz provision-sso-users --reconcile-roles --app ${sys}"
                elif [ "$c" -gt 0 ]; then
                    pass "knowledge scopes: ${sys}: ${c} scoped dataset(s) match their scopes"
                elif [ "${nscopes:-0}" = 0 ]; then
                    info "knowledge scopes: ${sys}: no knowledge scope defined"
                else
                    info "knowledge scopes: ${sys}: no dataset is in a knowledge scope"
                fi ;;
        esac
    done <<< "$out"
    [ "$checked" -eq 1 ] || [ "$said_all" -eq 1 ] || [ -z "$nscopes" ] \
        || info "knowledge scopes: ${nscopes} scope(s), no Dify or cognee datasets checked"
    return 0
}
# STATUS_SCOPE_DRIFT-END

# #2473 D4: upstream group sync health. The sync runs inside Authentik at each
# sign-in through the Microsoft or Google source; it never runs from here.
# This row reads, read-only: the reconcile policy is bound on the source
# sign-in (else upstream removals never apply: FAIL), each enabled source runs
# its group lookup mapping, which groups carry a mapping (one on an
# administrator group is ignored by the sync: WARN), whether any lookup ever
# succeeded, and who still holds sync-granted groups from a lookup older than
# the sync's 24 h limit (removed only at their next sign-in: WARN). Skipped
# without a probe when neither source is enabled in .env.
# STATUS_UPSTREAM_SYNC-BEGIN
status_upstream_sync_row() {
    local en_entra en_google want="" snippet raw line live bound nomap ngroups refused linked never stale newest
    en_entra=$(env_get ENABLE_ENTRA_OAUTH | tr '[:upper:]' '[:lower:]')
    en_google=$(env_get ENABLE_GOOGLE_OAUTH | tr '[:upper:]' '[:lower:]')
    [ "$en_entra" = true ] && want="entra"
    [ "$en_google" = true ] && want="${want:+${want},}google"
    if [ -z "$want" ]; then
        info "upstream group sync: no Microsoft or Google sign-in enabled — nothing to sync"
        return 0
    fi
    snippet='
import os, sys, time
from authentik.core.models import Group, User, UserSourceConnection
from authentik.policies.models import PolicyBinding
from authentik.sources.oauth.models import OAuthSource
POLICY = "razzfazz.ai - Reconcile upstream groups"
MAPPING = {"entra": "razzfazz.ai - Upstream groups (Entra)", "google": "razzfazz.ai - Upstream groups (Google)"}
ENV = [os.environ.get(k, "") for k in ("SSO_ADMIN_GROUP", "OWUI_OAUTH_ADMIN_ROLES")]
NAMES = ["razzfazz.ai Super Admins", "authentik Admins"] + ENV + [p for v in ENV for p in v.split(",")]
ADMIN = {" ".join(n.split()).casefold() for n in NAMES if n.strip()}
want = [s for s in os.environ["RZFZ_SYNC_SOURCES"].split(",") if s]
src = {s.slug: s for s in OAuthSource.objects.filter(slug__in=want, enabled=True)}
live = sorted(src)
nomap = sorted(k for k in live if not src[k].user_property_mappings.filter(name=MAPPING[k]).exists())
bound = PolicyBinding.objects.filter(policy__name=POLICY, enabled=True).count()
groups = list(Group.objects.filter(attributes__has_key="rzfz_upstream_ids"))
refused = sorted(g.name for g in groups if g.is_superuser or " ".join(g.name.split()).casefold() in ADMIN)
ids = set(UserSourceConnection.objects.filter(source__slug__in=live).values_list("user_id", flat=True))
now, never, stale, newest = time.time(), 0, 0, 0.0
for u in User.objects.filter(pk__in=ids):
    a = u.attributes if isinstance(u.attributes, dict) else {}
    ok = a.get("rzfz_upstream_ok_at")
    if not isinstance(ok, (int, float)) or isinstance(ok, bool):
        never += 1
        ok = None
    else:
        newest = max(newest, float(ok))
    if a.get("rzfz_upstream_owned") and (ok is None or now - ok > 24 * 3600):
        stale += 1
age = int(now - newest) if newest else -1
sys.stdout.write("RZFZ-SYNC\t%s\t%d\t%s\t%d\t%s\t%d\t%d\t%d\t%d\n" % (",".join(live), bound, ",".join(nomap),
                 len(groups), "|".join(refused), len(ids), never, stale, age))
'
    raw=$(timeout 25 docker exec -e RZFZ_SYNC_SOURCES="$want" authentik-worker ak shell -c "$snippet" 2>/dev/null)
    line=$(printf '%s\n' "$raw" | grep -m1 "^RZFZ-SYNC$(printf '\t')" || true)
    if [ -z "$line" ]; then
        warn "upstream group sync: the probe returned nothing usable — this check did NOT run"
        return 0
    fi
    live=$(printf '%s' "$line" | cut -f2)
    bound=$(printf '%s' "$line" | cut -f3)
    nomap=$(printf '%s' "$line" | cut -f4)
    ngroups=$(printf '%s' "$line" | cut -f5)
    refused=$(printf '%s' "$line" | cut -f6)
    linked=$(printf '%s' "$line" | cut -f7)
    never=$(printf '%s' "$line" | cut -f8)
    stale=$(printf '%s' "$line" | cut -f9)
    newest=$(printf '%s' "$line" | cut -f10)
    case "$bound$ngroups$linked$never$stale" in
        ''|*[!0-9]*) warn "upstream group sync: the probe output is unreadable — this check did NOT run"; return 0 ;;
    esac
    case "$newest" in -1|[0-9]*) ;; *) warn "upstream group sync: the probe output is unreadable — this check did NOT run"; return 0 ;; esac
    if [ -z "$live" ]; then
        warn "upstream group sync: ${want} enabled in .env, but no such source is enabled in Authentik — run: rzfz post-install --refresh"
        return 0
    fi
    if [ "$bound" -eq 0 ]; then
        fail "upstream group sync: the reconcile policy is not bound to the source sign-in — upstream changes (removals too) never apply; run: rzfz post-install --refresh"
        return 0
    fi
    [ -z "$nomap" ] || warn "upstream group sync: ${nomap} sign-in runs no group lookup — no upstream group is read there; run: rzfz post-install --refresh"
    [ -z "$refused" ] || warn "upstream group sync: the mapping on ${refused//|/, } is ignored — an administrator group is never synced; grant it by hand"
    if [ "$ngroups" -eq 0 ]; then
        info "upstream group sync: ${live} enabled, no group carries a mapping (rzfz_upstream_ids) — sign-ins sync nothing"
        return 0
    fi
    if [ "$linked" -eq 0 ]; then
        info "upstream group sync: ${ngroups} mapped group(s), nobody has signed in through ${live} yet"
        return 0
    fi
    [ "$stale" -eq 0 ] || warn "upstream group sync: ${stale} user(s) hold sync-granted groups from a lookup older than 24 h — they lose them at their next sign-in, not before"
    if [ "$newest" = -1 ]; then
        warn "upstream group sync: no group lookup has succeeded yet for ${linked} source user(s) — check the admin consent (Microsoft) or the groups scope (Google)"
    else
        pass "upstream group sync: ${live}, ${ngroups} mapped group(s), last successful lookup $((newest / 3600)) h ago (${never} of ${linked} source user(s) never)"
    fi
    return 0
}
# STATUS_UPSTREAM_SYNC-END

# A1 D10 (staging canary): with RAZZFAZZ_ACCESS_GROUP set, EVERY application
# must carry the access-group gate under policy_engine_mode `all`; one app
# without it is open to every user outside the group. The group must exist
# and have members (the apply refuses otherwise and changes nothing), and the
# live gate must name the group .env names. Always — set or not, snapshot or
# not — an app that carries the restriction marker (or is in the apply's
# snapshot) and has NO enabled binding is open to every signed-in user (a
# hand-deleted policy cascades its bindings away): FAIL, never PASS or INFO.
# So the probe runs on every box. The name travels by `-e`, never spliced into
# the Python source. No issue numbers in the rows.
# STATUS_ACCESS_GROUP-BEGIN
status_access_group_row() {
    local group snippet raw line total bad exists members stale snap open marked legacy writable
    local snapfile="${RZFZ_ACCESS_SNAPSHOT:-/data/.razzfazz/access-group-snapshot.json}"
    local legacyfile="${RZFZ_ACCESS_SNAPSHOT_LEGACY:-/data/media/razzfazz/access-group-snapshot.json}"
    group=$(env_get RAZZFAZZ_ACCESS_GROUP)
    snippet='
import json, os, sys
from authentik.core.models import Application, Group
from authentik.policies.models import PolicyBinding
from authentik.policies.expression.models import ExpressionPolicy
MARK = "[rzfz: access group]"
GATE = "razzfazz.ai - Box access group"
name = os.environ.get("RAZZFAZZ_ACCESS_GROUP", "").strip()
new, old = os.environ["RZFZ_ACCESS_SNAPSHOT"], os.environ["RZFZ_ACCESS_SNAPSHOT_LEGACY"]
legacy = int(os.path.exists(old))
d = os.path.dirname(new)
writable = int(os.access(d, os.W_OK | os.X_OK) if os.path.isdir(d)
               else not os.path.exists(d) and os.access(os.path.dirname(d), os.W_OK | os.X_OK))
snapped, snap = set(), 0
try:
    with open(new if os.path.exists(new) or not legacy else old, encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("complete") is not True or not isinstance(data.get("apps"), dict):
        raise ValueError("incomplete")
    snapped, snap = set(data["apps"]), 1
except FileNotFoundError:
    pass
except Exception:
    snap = 2
g = Group.objects.filter(name=name).first() if name else None
members = len(list(g.users.values_list("pk", flat=True))) if g is not None else 0
gate = ExpressionPolicy.objects.filter(name=GATE).first()
stale = 0 if gate is not None and repr(name) in gate.expression else 1
apps = list(Application.objects.all())
bad = [a.slug for a in apps if a.policy_engine_mode != "all" or not PolicyBinding.objects.filter(
    target_id=a.pk, policy__name=GATE, enabled=True).exists()]
marked = [a.slug for a in apps if MARK in (a.meta_description or "")]
opened = [a.slug for a in apps if (a.slug in marked or a.slug in snapped)
          and not PolicyBinding.objects.filter(target_id=a.pk, enabled=True).exists()]
sys.stdout.write("RZFZ-ACCESS\t%d\t%s\t%d\t%d\t%d\t%d\t%s\t%s\t%d\t%d\n" % (len(apps), ",".join(sorted(bad)),
                 int(g is not None), members, stale, snap, ",".join(sorted(opened)), ",".join(sorted(marked)), legacy,
                 writable))
'
    raw=$(timeout 25 docker exec -e RAZZFAZZ_ACCESS_GROUP="$group" -e RZFZ_ACCESS_SNAPSHOT="$snapfile" \
          -e RZFZ_ACCESS_SNAPSHOT_LEGACY="$legacyfile" authentik-worker ak shell -c "$snippet" 2>/dev/null)
    line=$(printf '%s\n' "$raw" | grep -m1 "^RZFZ-ACCESS$(printf '\t')" || true)
    if [ -z "$line" ]; then
        if [ -n "$group" ]; then
            warn "access group: '${group}' is set but the probe returned nothing usable — this check did NOT run"
        else
            warn "access group: unrestricted in .env, but the probe returned nothing usable — the live check did NOT run"
        fi
        return 0
    fi
    total=$(printf '%s' "$line" | cut -f2)
    bad=$(printf '%s' "$line" | cut -f3)
    exists=$(printf '%s' "$line" | cut -f4)
    members=$(printf '%s' "$line" | cut -f5)
    stale=$(printf '%s' "$line" | cut -f6)
    snap=$(printf '%s' "$line" | cut -f7)
    open=$(printf '%s' "$line" | cut -f8)
    marked=$(printf '%s' "$line" | cut -f9)
    legacy=$(printf '%s' "$line" | cut -f10)
    writable=$(printf '%s' "$line" | cut -f11)
    case "$total$exists$members$stale$snap$legacy$writable" in
        ''|*[!0-9]*) warn "access group: the probe output is unreadable — this check did NOT run"; return 0 ;;
    esac
    if [ -n "$open" ]; then
        fail "access group: ${open} carry the restriction but have no enabled binding — open to every signed-in user; run: rzfz post-install --refresh"
    elif [ "$snap" = 2 ]; then
        fail "access group: the restriction snapshot is unreadable — the apply changes nothing until it is restored from a backup"
    elif [ "$writable" != 1 ] && { [ -n "$group" ] || [ "$snap" = 1 ] || [ -n "$marked" ]; }; then
        fail "access group: the snapshot location ${snapfile%/*} is not writable by authentik-worker — the apply can neither restrict nor lift; run: docker compose up authentik-media-migrator, then rzfz post-install --refresh"
    elif [ -z "$group" ]; then
        if [ "$legacy" = 1 ]; then
            warn "access group: the restriction snapshot is still at its old path — run: rzfz post-install --refresh (it moves it)"
        elif [ "$snap" = 1 ] || [ -n "$marked" ]; then
            warn "access group: RAZZFAZZ_ACCESS_GROUP is empty but the restriction was not lifted yet${marked:+ (${marked})} — run: rzfz post-install --refresh"
        else
            info "access group: unrestricted (RAZZFAZZ_ACCESS_GROUP empty)"
        fi
    elif [ "$exists" != 1 ]; then
        fail "access group: '${group}' does not exist in Authentik — nothing is restricted to it; create the group or correct RAZZFAZZ_ACCESS_GROUP, then run: rzfz post-install --refresh"
    elif [ "$members" -eq 0 ]; then
        fail "access group: '${group}' has no members — it would block every user, so the apply refuses it; add the users, then run: rzfz post-install --refresh"
    elif [ -n "$bad" ]; then
        fail "access group '${group}' NOT applied to: ${bad} — run: rzfz post-install --refresh"
    elif [ "$stale" != 0 ]; then
        fail "access group: the live restriction does not name '${group}' — run: rzfz post-install --refresh"
    elif [ "$legacy" = 1 ]; then
        # after every FAIL check: an old-path file never hides a FAIL
        warn "access group: the restriction snapshot is still at its old path — run: rzfz post-install --refresh (it moves it)"
    else
        pass "access group: all ${total} applications restricted to '${group}' (${members} member(s))"
    fi
    return 0
}
# STATUS_ACCESS_GROUP-END

# #2551 / A1 D11: the admin-MFA control is only real if the LIVE dedicated
# stage configures a missing factor, our admin policy routes to it on EVERY
# admin login flow present (default, source, both domain enrollments) with the
# stage before the login stage, and Authentik's own MFA stage still skips (it
# is shared with every user; configuring it would force everyone to enrol).
# .env and the B-11 log line said "enforced" for a whole cycle while the stage
# skipped, and a Google login never met the stage at all.
# Called BEFORE the authentik-worker test: the not-enforced INFO row must show with the
# worker down, and a worker that is down is "unknown", never silence.
# STATUS_ADMIN_MFA-BEGIN
status_admin_mfa_row() {
    local want raw line action nstages bound present upstream snippet
    want=$(env_get RAZZFAZZ_REQUIRE_ADMIN_MFA | tr '[:upper:]' '[:lower:]')
    case "${want:-false}" in
        false|0|no|off) info "admin MFA: not enforced (default) — enable after the handover: RAZZFAZZ_REQUIRE_ADMIN_MFA=true"; return 0 ;;
    esac
    command -v docker >/dev/null 2>&1 || { warn "admin MFA: unknown — docker CLI not available, the live stage was not read"; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx authentik-worker \
        || { warn "admin MFA: unknown — authentik-worker not running, the live stage was not read"; return 0; }
    snippet='
import sys
from authentik.flows.models import Flow, FlowStageBinding
from authentik.policies.models import PolicyBinding
from authentik.stages.authenticator_validate.models import AuthenticatorValidateStage
FLOWS = ("default-authentication-flow", "default-source-authentication",
         "google-domain-enrollment", "entra-domain-enrollment")
LOGINS = ("default-authentication-login", "default-source-authentication-login")
s = AuthenticatorValidateStage.objects.filter(name="razzfazz.ai - Admin MFA validation").first()
u = AuthenticatorValidateStage.objects.filter(name="default-authentication-mfa-validation").first()
ua = u.not_configured_action if u is not None else "skip"
if s is None:
    sys.stdout.write("RZFZ-MFA\tmissing\t0\t0\t0\t%s\n" % ua); sys.exit(0)
present = bound = 0
for slug in FLOWS:
    fl = Flow.objects.filter(slug=slug).first()
    if fl is None:
        continue
    present += 1
    f = FlowStageBinding.objects.filter(target=fl, stage=s).first()
    if f is None:
        continue
    logins = [b.order for b in FlowStageBinding.objects.filter(target=fl) if getattr(b.stage, "name", "") in LOGINS]
    if logins and f.order >= min(logins):
        continue
    tid = getattr(f, "policybindingmodel_ptr_id", None) or f.pk
    bound += int(PolicyBinding.objects.filter(target_id=tid, enabled=True,
                 policy__name="razzfazz.ai - Require Admin MFA").exists())
sys.stdout.write("RZFZ-MFA\t%s\t%d\t%d\t%d\t%s\n" % (s.not_configured_action, s.configuration_stages.count(), bound, present, ua))
'
    raw=$(timeout 25 docker exec authentik-worker ak shell -c "$snippet" 2>/dev/null)
    line=$(printf '%s\n' "$raw" | grep -m1 "^RZFZ-MFA$(printf '\t')" || true)
    if [ -z "$line" ]; then
        warn "admin MFA: unknown — the live-stage probe returned nothing usable, this check did NOT run"
        return 0
    fi
    action=$(printf '%s' "$line" | cut -f2)
    nstages=$(printf '%s' "$line" | cut -f3)
    bound=$(printf '%s' "$line" | cut -f4)
    present=$(printf '%s' "$line" | cut -f5)
    upstream=$(printf '%s' "$line" | cut -f6)
    if [ "${upstream:-skip}" != skip ]; then
        fail "admin MFA: Authentik's own MFA stage is set to not_configured_action=${upstream}, which forces EVERY user to enrol — set it back to skip"
    elif [ "$action" = configure ] && [ "${nstages:-0}" -gt 0 ] && [ "${present:-0}" -ge 2 ] \
       && [ "${bound:-0}" = "${present:-x}" ]; then
        pass "admin MFA enforced: the dedicated stage configures a missing factor (${nstages} setup stage(s)), admin policy bound on ${bound}/${present} login flows"
    else
        fail "admin MFA required but NOT enforced: not_configured_action=${action:-?}, setup stages=${nstages:-0}, admin policy bound before login on ${bound:-0}/${present:-0} login flows — run: docker compose up -d authentik-init && docker wait authentik-init"
    fi
    return 0
}
# STATUS_ADMIN_MFA-END

# STATUS_GOOGLE_MODE-BEGIN
# Google Workspace through the MCP proxy (#3453): GOOGLE_MCP_MODE (read-write
# | read-only) and two true/false switches, all open by default. The proxy
# reads any other spelling as the strictest value, so a typo closes what the
# admin meant to keep open; this row says so. Valid values say nothing.
status_google_mode_rows() {
    local v k bad=""
    case ",$(env_get COMPOSE_PROFILES)," in *,mcp,*) ;; *) return 0 ;; esac
    v=$(env_get GOOGLE_MCP_MODE)
    case "$v" in ''|read-write|read-only) ;; *) bad="${bad} GOOGLE_MCP_MODE" ;; esac
    for k in GOOGLE_MCP_ALLOW_SEND GOOGLE_MCP_SEND_DELETE_AGENTS; do
        v=$(env_get "$k")
        case "$v" in ''|true|false) ;; *) bad="${bad} ${k}" ;; esac
    done
    if [ -n "$bad" ]; then
        warn "Google Workspace MCP:${bad} has an invalid value and is read as the strictest (read-only / false) — valid: GOOGLE_MCP_MODE=read-write|read-only, the others true|false"
    fi
    return 0
}
# STATUS_GOOGLE_MODE-END

# STATUS_MCP_IDENTITY-BEGIN
# mcp-manager keys MCP rows by the OIDC sub. Rows from before 2026.11 are
# re-keyed (or quarantined) only with the dedicated read-only Authentik token.
# Without one, they keep working through the old slug lookup and the
# migration waits. With one, an undecided row is served to nobody until an
# attribution confirmed its owner, so a failed attribution is a WARN. A
# credential whose owner never returns is listed 30 days after the migration
# COMPLETED (mcp-manager Admin -> Identity).
# A failed run of the sweep that deletes departed people's stored credentials
# is a WARN (they stay until a run succeeds).
# While mcp-manager runs without the token, its identity checks (the company
# brain's knowledge scope, a raised min_tier) behave as in 2026.09 and a new
# user's memory account is keyed by the username (a reused username inherits
# the former owner's memory). Every enable path mints the token (install,
# upgrade, --refresh, the Portal toggle), so a box still without it is a FAIL
# that names the fix (and the waiting migration, when pending).
# The migration rows below read database state that outlives a broken service:
# a mcp-manager that crash-loops must not be reported next to a green "complete"
# row. Prints nothing when the container is up and healthy, else the reason.
_mcp_manager_unhealthy() {
    local info status restarting restarts health started up_s now
    info=$(docker inspect mcp-manager --format '{{.State.Status}}|{{.State.Restarting}}|{{.RestartCount}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.StartedAt}}' 2>/dev/null) || info=""
    # No such container (or docker cannot say): the container rows of the
    # report already name a service that is not there; claim nothing here.
    [ -n "$info" ] || return 0
    IFS='|' read -r status restarting restarts health started <<< "$info"
    case "$restarts" in ''|*[!0-9]*) restarts=0 ;; esac
    if [ "$status" = restarting ] || [ "$restarting" = true ]; then
        echo "crash-looping (restarted ${restarts} times)"; return 0
    fi
    if [ "$status" != running ]; then
        echo "not running (state: ${status:-unknown})"; return 0
    fi
    if [ "$restarts" -ge 3 ]; then
        now=$(date +%s); up_s=$(date -d "$started" +%s 2>/dev/null) || up_s=""
        case "$up_s" in ''|*[!0-9]*) ;; *)
            if [ $((now - up_s)) -lt 600 ]; then
                echo "crash-looping (restarted ${restarts} times, last start $((now - up_s)) s ago)"; return 0
            fi ;;
        esac
    fi
    [ "$health" != unhealthy ] || echo "unhealthy (its healthcheck fails)"
    return 0
}

status_mcp_identity_rows() {
    case ",$(env_get COMPOSE_PROFILES)," in *,mcp,*) ;; *) return 0 ;; esac
    command -v docker >/dev/null 2>&1 || { skip "MCP identity migration: docker CLI not available"; return 0; }
    local _bad
    _bad=$(_mcp_manager_unhealthy)
    if [ -n "$_bad" ]; then
        fail "mcp-manager is ${_bad}: its identity migration state is not trustworthy until it runs — see: docker logs mcp-manager"
        return 0
    fi
    local running db state n failed pend_cognee compat=""
    running=$(docker ps --format '{{.Names}}' 2>/dev/null)
    grep -qx postgres <<< "$running" || { skip "MCP identity migration: postgres not running"; return 0; }
    db=$(env_get MCP_MANAGER_DB); [ -n "$db" ] || db="mcp_manager_db"
    state=$(docker exec postgres psql -U "$(env_get POSTGRES_USER)" -d "$db" -tAc "SELECT CASE WHEN instances_done_at IS NULL THEN 'pending' ELSE 'done' END FROM mcp_identity_migration WHERE id = 1;" 2>/dev/null | tr -d '[:space:]')
    case "$state" in pending|done) ;; *) return 0 ;; esac   # an mcp-manager from before 2026.11
    if [ -z "$(env_get MCP_MANAGER_AUTHENTIK_READONLY_TOKEN)" ] && grep -qx mcp-manager <<< "$running"; then
        compat=1
        if [ "$state" = pending ]; then
            fail "MCP identity checks are in 2026.09 compatibility mode: read-only Authentik token not set (MCP_MANAGER_AUTHENTIK_READONLY_TOKEN), so a new user's memory account is keyed by the username; the identity migration waits for it — run: rzfz post-install --refresh"
        else
            fail "MCP identity checks are in 2026.09 compatibility mode: read-only Authentik token not set (MCP_MANAGER_AUTHENTIK_READONLY_TOKEN), so a new user's memory account is keyed by the username — run: rzfz post-install --refresh"
        fi
    fi
    # A cognee account registered inside the no-token window stays 'pending'
    # until the migration decides whose it is; the row must not read "complete"
    # while one is undecided.
    pend_cognee=$(docker exec postgres psql -U "$(env_get POSTGRES_USER)" -d "$db" -tAc "SELECT count(*) FROM mcp_cognee_accounts WHERE state = 'pending';" 2>/dev/null | tr -d '[:space:]')
    case "${pend_cognee:-}" in ''|*[!0-9]*) pend_cognee=0 ;; esac
    case "$state" in
        pending)
            if [ -z "$(env_get MCP_MANAGER_AUTHENTIK_READONLY_TOKEN)" ]; then
                [ -n "$compat" ] || warn "MCP identity migration pending: read-only Authentik token not set (MCP_MANAGER_AUTHENTIK_READONLY_TOKEN)"
            else
                warn "MCP identity migration pending: re-key not completed — check the read-only Authentik token and the mcp-manager log"
            fi ;;
        done)
            if [ "$pend_cognee" -gt 0 ]; then
                warn "MCP identity migration: ${pend_cognee} cognee account(s) still undecided (pending) — check the mcp-manager log and Admin -> Identity"
            else
                pass "MCP identity migration complete"
            fi ;;
    esac
    failed=$(docker exec postgres psql -U "$(env_get POSTGRES_USER)" -d "$db" -tAc "SELECT to_char(attribution_error_at, 'YYYY-MM-DD HH24:MI') FROM mcp_identity_migration WHERE id = 1 AND attribution_error_at IS NOT NULL AND (attribution_ok_at IS NULL OR attribution_error_at > attribution_ok_at);" 2>/dev/null | sed '/^$/d' | head -1)
    [ -z "$failed" ] || warn "MCP identity attribution failed (last at ${failed}): pre-2026.11 rows stay withheld until it succeeds — check the read-only Authentik token and the mcp-manager log"
    # The sweep that deletes the stored credentials of people who left
    # (deleted at once, deactivated after 30 days) records its last outcome.
    local sweep
    sweep=$(docker exec postgres psql -U "$(env_get POSTGRES_USER)" -d "$db" -tAc "SELECT to_char(departed_sweep_error_at, 'YYYY-MM-DD HH24:MI') || '|' || COALESCE(departed_sweep_error, '') FROM mcp_identity_migration WHERE id = 1 AND departed_sweep_error_at IS NOT NULL AND (departed_sweep_ok_at IS NULL OR departed_sweep_error_at > departed_sweep_ok_at);" 2>/dev/null | sed '/^$/d' | head -1)
    [ -z "$sweep" ] || warn "Deleting departed people's stored MCP credentials failed (last at ${sweep%%|*}): ${sweep#*|} — they are kept and retried; check the mcp-manager log"
    n=$(docker exec postgres psql -U "$(env_get POSTGRES_USER)" -d "$db" -tAc "SELECT (SELECT count(*) FROM mcp_instances WHERE owner_sub IS NULL AND state != 'destroyed') + (SELECT count(*) FROM mcp_type_bindings WHERE owner_sub IS NULL) + (SELECT count(*) FROM mcp_user_secrets WHERE owner_key IS NULL) FROM mcp_identity_migration WHERE id = 1 AND instances_done_at IS NOT NULL AND instances_done_at <= NOW() - interval '30 days';" 2>/dev/null | tr -d '[:space:]')
    case "${n:-}" in ''|*[!0-9]*) n=0 ;; esac
    [ "$n" -gt 0 ] && info "${n} MCP rows unattributed for more than 30 days — mcp-manager Admin -> Identity"
    return 0
}
# STATUS_MCP_IDENTITY-END

# STATUS_M365_MODE-BEGIN
# Microsoft 365 through the MCP gateway (#3433): M365_MCP_MODE (read-write |
# read-only) and four true/false switches. Every reader (the ms365-mcp start
# script, mcp-manager's gateway and OAuth scopes) treats an unset key as its
# default and any other spelling as the strictest value, so a typo never
# opens anything; this row says so instead of leaving the admin wondering why
# a setting does not take. It also names the restrictions that are on (the
# defaults restrict nothing and say nothing).
status_m365_mode_rows() {
    local v k bad="" on=""
    case ",$(env_get COMPOSE_PROFILES)," in *,mcp-m365,*) ;; *) return 0 ;; esac
    v=$(env_get M365_MCP_MODE)
    case "$v" in ''|read-write) ;; read-only) on="${on}, read-only" ;; *) bad="${bad} M365_MCP_MODE" ;; esac
    for k in M365_MCP_ALLOW_SEND M365_MCP_SEND_DELETE_AGENTS M365_MCP_SEND_DELETE_WORKFLOWS M365_MCP_CONFIRM_SEND_DELETE; do
        v=$(env_get "$k")
        case "$v" in ''|true|false) ;; *) bad="${bad} ${k}" ;; esac
    done
    if [ -n "$bad" ]; then
        warn "Microsoft 365 MCP:${bad} has an invalid value and is read as the strictest (read-only, false; true for M365_MCP_CONFIRM_SEND_DELETE) — valid: M365_MCP_MODE=read-write|read-only, the others true|false"
    fi
    [ "$(env_get M365_MCP_ALLOW_SEND)" = "false" ] && on="${on}, sending off"
    [ "$(env_get M365_MCP_SEND_DELETE_AGENTS)" = "false" ] && on="${on}, send/delete off for agents"
    [ "$(env_get M365_MCP_SEND_DELETE_WORKFLOWS)" = "false" ] && on="${on}, send/delete off for workflows"
    [ "$(env_get M365_MCP_CONFIRM_SEND_DELETE)" = "true" ] && on="${on}, chat send/delete confirmed per call"
    if [ -n "$on" ]; then
        info "Microsoft 365 MCP restrictions: ${on#, }"
    fi
    return 0
}
# STATUS_M365_MODE-END

# STATUS_GITEA_REVIEW-BEGIN
# Agents reach Gitea through the MCP gateway (and coding agents with their
# owner's git credentials). The gateway keeps them off a default branch, but a
# person's agent may merge a pull request: only branch protection with at
# least 1 required approval makes that a reviewed change. One WARN names the
# organisation repos whose default branch lacks it (scripts/
# gitea_branch_protection_check.py: GET only, one call per repo, bounded,
# skipped at once when Gitea does not answer). The admin login is read from
# .env and handed over on stdin, never on a command line.
status_gitea_review_rows() {
    local profiles out line verdict text port user pw
    profiles=",$(env_get COMPOSE_PROFILES),"
    case "$profiles" in *,gitea,*) ;; *) return 0 ;; esac
    case "$profiles" in *,mcp,*|*,agents,*) ;; *) return 0 ;; esac
    command -v python3 >/dev/null 2>&1 || { skip "Gitea review requirement: python3 not available"; return 0; }
    if command -v docker >/dev/null 2>&1; then
        docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gitea \
            || { skip "Gitea review requirement: gitea not running"; return 0; }
    fi
    port=$(env_get GITEA_HTTP_PORT); port=${port:-3000}
    case "$port" in *[!0-9]*) skip "Gitea review requirement: GITEA_HTTP_PORT is not a port"; return 0 ;; esac
    user=$(env_get GITEA_ADMIN_USER); pw=$(env_get GITEA_ADMIN_PASSWORD)
    out=$(printf '%s\n%s\n' "$user" "$pw" | GITEA_URL="http://127.0.0.1:${port}" timeout 60 \
          python3 "${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/gitea_branch_protection_check.py" 2>/dev/null) || true
    [ -n "$out" ] || { skip "Gitea review requirement: the check produced no verdict"; return 0; }
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        verdict="${line%%|*}"; text="${line#*|}"
        case "$verdict" in
            PASS) pass "$text" ;;
            WARN) warn "$text" ;;
            SKIP) skip "$text" ;;
            *) info "$text" ;;
        esac
    done <<< "$out"
    return 0
}
# STATUS_GITEA_REVIEW-END

# STATUS_OPENBAO-BEGIN
# #2476 (decision record D9): OpenBao's static seal. PASS when initialised and
# unsealed; FAIL when it does not answer, is not initialised, or stays sealed
# (the seal key does not open this storage). WARN until the operator has been
# shown the recovery key once. The age of the last backup snapshot: PASS under
# 26 h, WARN under 7 d, FAIL beyond; with none, WARN for 26 h after the setup,
# then FAIL. Skipped unless the openbao profile is on.
status_openbao_rows() {
    local profiles st sealed init
    profiles=",$(env_get COMPOSE_PROFILES),"
    case "$profiles" in *,openbao,*) ;; *) return 0 ;; esac
    command -v docker >/dev/null 2>&1 || { skip "openbao: docker CLI not available"; return 0; }
    st=$(timeout 10 docker exec openbao bao status -format=json 2>/dev/null) || true
    [ -n "$st" ] || { fail "openbao: not answering. Check: docker logs openbao-keygen; docker logs openbao"; return 0; }
    init=$(printf '%s\n' "$st" | sed -n 's/.*"initialized"[[:space:]]*:[[:space:]]*\([a-z]*\).*/\1/p' | head -1)
    sealed=$(printf '%s\n' "$st" | sed -n 's/.*"sealed"[[:space:]]*:[[:space:]]*\([a-z]*\).*/\1/p' | head -1)
    if [ "$init" != true ]; then
        fail "openbao: not initialised. Check: docker logs openbao-bootstrap"
    elif [ "$sealed" != false ]; then
        fail "openbao: SEALED: the static seal key does not open this storage. Check: docker logs openbao-bootstrap"
    else
        pass "openbao: initialised and unsealed"
    fi
    # Decision record D9: the operator holds one copy of the recovery key.
    if [ "$init" = true ] && ! timeout 10 docker exec openbao test -e /openbao/keys/recovery.shown >/dev/null 2>&1; then
        warn "openbao: the recovery key has not been shown to the operator yet. Run: rzfz openbao show-recovery-key"
    fi
    # The backup's half (pre-backup.sh section 8): latest.snap is renamed into
    # place only when a backup carried both the snapshot and the encrypted
    # keys, so its age is the age of the last backup that holds the store.
    # Only for a store that is up: a sealed or absent one already FAILs above.
    if [ "$init" = true ] && [ "$sealed" = false ]; then
        local mt age
        mt=$(timeout 10 docker exec openbao stat -c %Y /openbao/snapshots/latest.snap 2>/dev/null) || mt=""
        case "$mt" in ''|*[!0-9]*) mt="" ;; esac
        if [ -z "$mt" ]; then
            # A fresh install gets one nightly backup's grace: WARN while its
            # setup (setup.done) is younger than 26 h, FAIL after that.
            local sd
            sd=$(timeout 10 docker exec openbao stat -c %Y /openbao/keys/setup.done 2>/dev/null) || sd=""
            case "$sd" in ''|*[!0-9]*) sd="" ;; esac
            # dated more than 5 min in the future (a clock that was ahead at
            # install): unreadable, so FAIL, never an endless grace
            if [ -n "$sd" ] && [ $(( sd - $(date +%s) )) -gt 300 ]; then sd=""; fi
            if [ -n "$sd" ] && [ $(( $(date +%s) - sd )) -lt 93600 ]; then
                warn "openbao: no backup holds the secrets store yet (set up $(( ($(date +%s) - sd) / 3600 )) h ago). Run: rzfz backup backup"
            else
                fail "openbao: no backup holds the secrets store yet. Run: rzfz backup backup"
            fi
        else
            age=$(( $(date +%s) - mt ))
            if [ "$age" -lt -300 ]; then warn "openbao: the last backup snapshot is dated in the future; check the clock (date; hwclock)"
            elif [ "$age" -lt 93600 ]; then pass "openbao: last snapshot in a backup $((age / 3600)) h ago"
            elif [ "$age" -lt 604800 ]; then warn "openbao: last snapshot in a backup $((age / 86400)) d ago. Check: rzfz backup status"
            else fail "openbao: last snapshot in a backup $((age / 86400)) d ago. Check: rzfz backup status"; fi
        fi
    fi
    return 0
}
# STATUS_OPENBAO-END

# #2476 D9 (ruling 12): the deleted-user purge — off until the sub gate, a
# FAIL when more users look deleted than the per-run cap allows, a WARN when
# the Portal's 10-minute reconcile stopped. The file holds subs, no secret.
# STATUS_OPENBAO_PURGE-BEGIN
status_openbao_purge_row() {
    local profiles f res text n last age
    profiles=",$(env_get COMPOSE_PROFILES),"
    case "$profiles" in *,openbao,*) ;; *) return 0 ;; esac
    f="${STACK_ROOT:-.}/.openbao-state/purge.status"
    [ -s "$f" ] || { info "openbao purge: no run yet (the Configuration Portal runs it every 10 minutes)"; return 0; }
    res=$(sed -n 's/^RESULT\t\([A-Z]*\)\t.*/\1/p' "$f" | head -n 1)
    text=$(sed -n 's/^RESULT\t[A-Z]*\t//p' "$f" | head -n 1)
    n=$(grep -c '^PENDING' "$f" || true)
    last=$(sed -n 's/^LAST_RUN\t\([0-9][0-9]*\)$/\1/p' "$f" | head -n 1)
    if [ -n "$last" ] && [ "$res" != DISABLED ]; then
        age=$(( $(date +%s) - last ))
        if [ "$age" -gt 1800 ]; then
            warn "openbao purge: has not run for $((age / 60)) min - docker logs razzfazz-config"
            return 0
        fi
    fi
    case "$res" in
        OK) pass "openbao purge: ${text}" ;;
        DISABLED) info "openbao purge: ${text}" ;;
        FAIL) fail "openbao purge: ${text} (${n} pending)" ;;
        *) warn "openbao purge: ${text:-unreadable state}" ;;
    esac
    return 0
}
# STATUS_OPENBAO_PURGE-END

# STATUS_SPEACHES_MODELS-BEGIN
# #2700: Speaches up but without its STT/TTS models. A full restore recreates
# the speaches-data volume and backups exclude model files, so after a restore
# stts was enabled while Speaches held no model — and no row said so. The ids
# are the pinned defaults post-install provisions (scripts/lib.sh); Speaches is
# asked once (GET /v1/models, 10 s cap). No stts profile: no row.
status_speaches_models_row() {
    case ",$(env_get COMPOSE_PROFILES)," in *,stts,*) ;; *) return 0 ;; esac
    local port url missing hand
    port=$(env_get SPEACHES_PORT | grep -E '^[0-9]+$' || echo 5003)
    url="http://127.0.0.1:${port}"
    hand="curl -X POST ${url}/v1/models/${RAZZFAZZ_SPEACHES_STT_MODEL} && curl -X POST ${url}/v1/models/${RAZZFAZZ_SPEACHES_TTS_MODEL}"
    if ! missing=$(razzfazz_speaches_missing_models "$url"); then
        warn "Speaches (stts) did not answer at ${url}/v1/models: its STT/TTS models are not checked"
        return 0
    fi
    if [ -z "$missing" ]; then
        pass "Speaches lists the STT/TTS models (${RAZZFAZZ_SPEACHES_STT_MODEL}, ${RAZZFAZZ_SPEACHES_TTS_MODEL})"
    else
        missing=$(printf '%s' "$missing" | paste -sd, - | sed 's/,/, /g')
        warn "Speaches (stts) lists NO model ${missing}: speech-to-text/text-to-speech will fail (after a restore the models are not in the backup). Download: ${hand}"
    fi
    return 0
}
# STATUS_SPEACHES_MODELS-END

# STATUS_HELP_SETS-BEGIN
# Help Center documentation sets with no readable copy, once the help container
# has run longer than the warm window. The external manuals are downloaded after
# install by a sequential warm; before the window a missing set is expected.
# The state and reason come from the help container's own records.
HELP_SETS_WINDOW_H=6
status_help_sets_row() {
    command -v docker >/dev/null 2>&1 || return 0
    local names started started_s now_s age_h out rows line id state reason list="" n=0
    names=$(docker ps --format '{{.Names}}' 2>/dev/null) || names=""
    case $'\n'"$names"$'\n' in *$'\n'razzfazz-help$'\n'*) ;; *) return 0 ;; esac
    started=$(docker inspect -f '{{.State.StartedAt}}' razzfazz-help 2>/dev/null) || started=""
    started_s=$(date -d "$started" +%s 2>/dev/null) || started_s=""
    now_s=$(date +%s)
    age_h=""
    case "$started_s" in ''|*[!0-9]*) ;; *) age_h=$(( (now_s - started_s) / 3600 )) ;; esac
    out=$(docker exec --user appuser razzfazz-help python3 -c '
import sys
sys.path.insert(0, "/app")
from cache_manager import CacheManager
for r in CacheManager().never_captured():
    print("RZFZ-HELP-SET %s|%s|%s" % (r["id"], r["state"], r["reason"].replace("\n", " ")))
print("RZFZ-HELP-SETS-END")
' 2>/dev/null) || out=""
    case "$out" in
        *RZFZ-HELP-SETS-END*) ;;
        *) warn "Help Center documentation sets: the help container's records could not be read — not checked"; return 0 ;;
    esac
    rows=$(printf '%s\n' "$out" | sed -n 's/^RZFZ-HELP-SET //p')
    while IFS='|' read -r id state reason; do
        [ -n "$id" ] || continue
        n=$((n + 1))
        if [ -n "$reason" ]; then
            list="${list:+${list}, }${id} (${state}: ${reason})"
        else
            list="${list:+${list}, }${id} (${state})"
        fi
    done <<< "$rows"
    if [ "$n" -eq 0 ]; then
        pass "Help Center: every external documentation set has a readable copy"
    elif [ -n "$age_h" ] && [ "$age_h" -lt "$HELP_SETS_WINDOW_H" ]; then
        info "Help Center: ${n} documentation set(s) not downloaded yet, the warm is still inside its ${HELP_SETS_WINDOW_H} h window (help container up ${age_h} h): ${list}"
    else
        warn "Help Center: ${n} documentation set(s) have no readable copy after ${HELP_SETS_WINDOW_H} h: ${list}. Retry under https://help.$(env_get MAIN_DOMAIN)/admin/cache; 'host unreachable' means this box cannot reach the vendor site (proxy, firewall)."
    fi
    return 0
}
# STATUS_HELP_SETS-END

# ============================================================================
# CATEGORY 2 — STACK STATE
# ============================================================================
cat_begin "STACK STATE"
if ! command -v docker >/dev/null 2>&1; then
    fail "docker CLI not available"
else
    running=$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l)
    unhealthy=$(docker ps --format '{{.Status}}' 2>/dev/null | grep -c -E "unhealthy|Restarting" || true)
    pass "$running containers running"
    status_cognee_default_account_row
    status_openlit_default_login_row
    status_delivery_blob_row
    status_vaultwarden_smtp_row
    status_embedding_space_rows
    status_owui_access_rows
    status_owui_direct_integrations_row
    status_authentik_init_marker_row
    status_rbac_legacy_rows
    status_llm_catalogue_row
    status_authentik_blueprint_rows
    status_embedding_dim_rows
    status_rag_chat_model_rows
    status_dify_dataset_provider_row
    status_dify_structured_output_row
    status_dify_valkey_sync_row
    status_crashloop_rows
    status_help_sets_row
    status_final_health_rows
    status_google_mode_rows
    status_caddy_memory_row
    status_mcp_identity_rows
    status_m365_mode_rows
    status_gitea_review_rows
    if [ "$unhealthy" -eq 0 ]; then
        pass "no unhealthy/restarting containers"
    else
        fail "$unhealthy unhealthy/restarting containers"
        while read -r l; do
            warn "$l"
        done < <(docker ps --format '  {{.Names}}: {{.Status}}' 2>/dev/null | grep -E "unhealthy|Restarting" | head -3)
    fi
    status_adhoc_orphan_row
    status_orphan_volume_rows
    status_backup_only_volume_rows
    status_canonical_alias_row
    status_package_key_row
    status_stack_host_path_row
    status_portal_env_bind_row
    status_llm_deployment_rows
    status_llm_unverified_rows
    status_llm_experimental_off_row
    status_llm_param_alias_row
    status_llm_worker_skew_rows
    status_llm_small_carveout_row
    status_llm_manager_ready_row
    status_speaches_models_row
    status_quarantined_weight_rows
    status_engine_hardening_row
    status_llm_master_role_row
    status_failed_runner_upgrade_rows
    status_wazuh_fim_scan_row
    status_init_oneshot_rows
    status_openbao_rows
    status_openbao_purge_row
    status_komodo_auto_prune_row
    status_komodo_pg_hba_trust_row
fi

# ============================================================================
# CATEGORY 3 — IMAGE VERSIONS (deployed vs manifest)
# ============================================================================
cat_begin "IMAGE VERSIONS"
if [ ! -f "$MANIFEST_FILE" ] || ! command -v python3 >/dev/null 2>&1; then
    skip "manifest or python3 unavailable"
else
    # Capture both stdout and stderr to /tmp for debugging if it goes blank
    # Pass active COMPOSE_PROFILES so the probe skips manifest entries on
    # profiles the operator hasn't enabled (e.g. `llm-legacy` rollback path
    # contains `gpustack-legacy` which would otherwise false-flag against
    # the v2.1.2 running gpustack on the `llm` profile).
    active_profiles=$(env_get COMPOSE_PROFILES)
    drift=$(MANIFEST="$MANIFEST_FILE" ACTIVE_PROFILES="$active_profiles" HARDWARE="$(env_get HARDWARE)" python3 - 2>/dev/null <<'PY'
import json, os, subprocess
manifest = os.environ["MANIFEST"]
active = set(p.strip() for p in os.environ.get("ACTIVE_PROFILES","").split(",") if p.strip())
hw = os.environ.get("HARDWARE", "").strip().lower()
fam = "nvidia" if hw in ("nvidia", "cuda") else hw
# A hardware line we do not recognise (or none) must not silently skip every
# hardware-scoped image: compare them all and tell the operator (HWUNKNOWN).
hw_known = fam in ("amd", "nvidia", "cpu")
m = json.load(open(manifest))
align = 0
drift_lines = []
try:
    r = subprocess.run(["docker","ps","--format","{{.Image}}"], capture_output=True, text=True, timeout=5)
    running = r.stdout.splitlines()
except Exception:
    running = []
for section in ("images","hardcoded"):
    for name, e in m.get(section, {}).items():
        # Skip entries whose profile isn't active (when the entry declares one)
        prof = e.get("profile")
        if prof and prof not in active and prof != "core":
            continue
        # #2301: an entry that names its hardware line is only compared on a
        # box of that line - gpustack-legacy (vulkan) and gpustack-cuda share
        # one repository, and an AMD box must not be measured against :cuda.
        hw_lines = e.get("hardware") or []
        if hw_lines and hw_known and fam not in hw_lines:
            continue
        manifest_v = str(e.get("current",""))
        repo = (e.get("image","") or "").split(":", 1)[0]
        if not manifest_v or not repo:
            continue
        # Match running images against this repo (allow optional -cpu form)
        matched = [i for i in running if i.startswith(repo + ":") or i == repo]
        if not matched:
            continue
        for img in matched:
            tag = img.split(":", 1)[1] if ":" in img else ""
            if tag in (manifest_v, manifest_v + "-cpu") or tag.startswith(manifest_v):
                align += 1
                break
            else:
                drift_lines.append(f"DRIFT|{name}: {img} (manifest expects :{manifest_v})")
                break
print(f"COUNT|{align}")
if not hw_known:
    print("HWUNKNOWN|1")
for l in drift_lines:
    print(l)
PY
)
    align=$(echo "$drift" | grep '^COUNT|' | cut -d'|' -f2)
    drift_n=$(echo "$drift" | grep -c '^DRIFT|' || true)
    if echo "$drift" | grep -q '^HWUNKNOWN|'; then
        warn "HARDWARE is unset or unrecognised - image versions checked for every hardware line"
    fi
    if [ -z "$align" ]; then
        warn "image-version probe produced no output (manifest schema mismatch?)"
    elif [ "$drift_n" -eq 0 ]; then
        pass "all $align comparable services aligned with manifest"
    else
        # Use process substitution (not pipe) so `fail` updates the parent
        # shell's counters/arrays — `cmd | while` runs the loop in a subshell
        # whose state is discarded on close.
        while IFS='|' read -r _ msg; do
            fail "$msg"
        done < <(echo "$drift" | grep '^DRIFT|')
    fi
fi

# ============================================================================
# CATEGORY 4 — LLM PROFILE COHERENCE
# ============================================================================
cat_begin "LLM PROFILE"
profiles=$(env_get COMPOSE_PROFILES)
hardware=$(env_get HARDWARE)
compose_file=$(env_get COMPOSE_FILE)
case ",$profiles," in
    *,llm,*)
        # #1447 (cutover C7a): `llm` was the GPUStack 2.x profile and is
        # REMOVED. A box that still carries the token starts nothing under it —
        # docker compose accepts an unknown profile in silence — so this is a
        # FAIL with the one command that fixes it, not an informational note.
        fail "COMPOSE_PROFILES contains 'llm' (GPUStack 2.x), a profile REMOVED in 2026.09 — nothing starts under it. Run 'rzfz upgrade' to migrate this box to the LLM Manager."
        ;;
    *,llm-box,*|*,llm-experimental,*)
        # #367: WARN, not FAIL — and the remediation must be 'rzfz upgrade'
        # (its migration maps the retired name to the supported profile for
        # this hardware). The old text told the customer to move to 'llm',
        # which on AMD Strix Halo is the experimental runtime we deliberately
        # hold back — advice that can break a working box. Still a WARN
        # (not pass): the retired name matches no compose profile on this
        # tree, so it starts nothing by itself.
        warn "COMPOSE_PROFILES contains a retired LLM profile name — run 'rzfz upgrade' to migrate it to the supported runtime profile automatically"
        ;;
    *,llm-cpu,*)
        # #1447 part b: folded into llm-legacy + HARDWARE=cpu. A box still
        # carrying the token starts nothing under it — compose accepts an
        # undefined profile in silence — so this is a FAIL with the one command
        # that fixes it.
        fail "COMPOSE_PROFILES contains 'llm-cpu', a profile folded into 'llm-legacy' in 2026.09 — nothing starts under it. Run 'rzfz upgrade' to migrate this box (it sets HARDWARE=cpu and the CPU overlay)."
        ;;
    *,llm-legacy,*)
        # #367/#946/#1448: the SUPPORTED default for every GPU box — AMD (custom
        # Vulkan build) and NVIDIA (GPUStack 0.7.1 + custom CUDA llama.cpp) share
        # ONE profile since C8; HARDWARE picks the device overlay. Green state,
        # no 'move to llm' nudge.
        pass "COMPOSE_PROFILES on 'llm-legacy' (stable GPUStack 0.7.1 runtime — the one GPUStack profile)"
        case "$hardware" in
            amd|nvidia)
                pass "HARDWARE=$hardware"
                # #1448: the merged service carries NO device wiring of its own —
                # it comes from the overlay. Without it in the chain the container
                # refuses to start (its HARDWARE guard), so a missing overlay is a
                # hard finding, not a nuance.
                if echo "$compose_file" | grep -q "compose.devices.${hardware}.yml"; then
                    pass "COMPOSE_FILE includes the ${hardware} device overlay"
                else
                    fail "COMPOSE_FILE='$compose_file' is missing modules/llm/compose.devices.${hardware}.yml — the merged llm-legacy service gets its image and GPU wiring from it. Run 'rzfz upgrade' to repair it"
                fi
                ;;
            cpu)
                # #1447 part b: the CPU line is this service plus the CPU
                # overlay, which supplies the UPSTREAM v0.7.1-cpu image. Without
                # the overlay the box would start the AMD Vulkan build with no
                # GPU, so the overlay is as load-bearing here as on a GPU box.
                if echo "$compose_file" | grep -q "compose.devices.cpu.yml"; then
                    pass "llm-legacy with HARDWARE=cpu + the CPU device overlay (upstream v0.7.1-cpu image)"
                else
                    fail "COMPOSE_FILE='$compose_file' is missing modules/llm/compose.devices.cpu.yml — llm-legacy would start the AMD GPU image on this CPU box. Run 'rzfz upgrade' to repair it"
                fi
                ;;
            *)
                warn "llm-legacy active but HARDWARE='$hardware' (expected amd, nvidia or cpu)"
                ;;
        esac
        ;;
    *,llm-manager,*)
        # #2164: the DEFAULT box shape since #1443 (cutover C3). The manager
        # trio is the LLM front end of EVERY box and cannot be toggled off, yet
        # this case had no arm for it — so the standard install fell through to
        # the `*)` below and was reported as having no LLM profile at all. It
        # printed exactly that on 0.91 (journey A, #2126) while the box's LLM
        # front end was genuinely broken (#2156), which is the wrong direction
        # for the reporter an operator reads first.
        #
        # Deliberately placed AFTER the llm-legacy arm: a dual box carrying
        # both matches that one, and there GPUStack's hardware/overlay checks
        # are the ones that matter.
        pass "COMPOSE_PROFILES on 'llm-manager' (the canonical LLM front end — serves http://llm:8080/v1)"
        case ",$profiles," in
            *,llm-worker-agent,*)
                pass "llm-worker-agent present — engine containers are launched on this box"
                ;;
            *)
                # Not a fault: a front-end-only box placing models on remote
                # nodes is a supported shape. Worth saying, because the usual
                # reason to see this is a profile set that lost the agent.
                warn "llm-manager without 'llm-worker-agent' — no local worker registers here, so models can only be placed on remote nodes"
                ;;
        esac
        ;;
    *)
        skip "no LLM profile active"
        ;;
esac

# #434: health ≠ function. A GPU box can serve its LLM on CPU for weeks with
# every signal green (observed on 0.236: COMPOSE_FILE had lost the device
# overlay; qwen3.6 Q8 answered happily — just slowly — the whole time). So
# don't trust the compose CHAIN alone: inspect what the RUNNING gpustack
# container actually got. AMD (both `llm` and the merged `llm-legacy`):
# /dev/kfd must be in HostConfig.Devices. NVIDIA: a gpu DeviceRequest or
# `runtime: nvidia`. CPU boxes have nothing to assert.
case ",$profiles," in
    *,llm,*|*,llm-legacy,*)
        if ! command -v docker >/dev/null 2>&1; then
            skip "GPU device mapping: docker unavailable"
        elif [ "$hardware" = "amd" ] || [ "$hardware" = "nvidia" ]; then
            if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gpustack; then
                skip "GPU device mapping: gpustack not running (unverifiable)"
            elif [ "$hardware" = "amd" ]; then
                gs_devices=$(docker inspect gpustack --format '{{json .HostConfig.Devices}}' 2>/dev/null)
                if echo "$gs_devices" | grep -q "/dev/kfd"; then
                    pass "gpustack has /dev/kfd mapped (GPU inference wired)"
                else
                    fail "gpustack is RUNNING without /dev/kfd — inference silently on CPU. Check COMPOSE_FILE for modules/llm/compose.devices.amd.yml (HARDWARE=amd), then recreate gpustack"
                fi
            else
                # NVIDIA: the v2.x `llm` path wires the GPU via a DeviceRequest;
                # the #946/#1448 0.7.1 path wires it via `runtime: nvidia`
                # (HostConfig.Runtime), NOT a DeviceRequest — both come from
                # compose.devices.nvidia.yml. Accept either, else an NVIDIA box
                # silently degrades to CPU (#434).
                gs_devreq=$(docker inspect gpustack --format '{{json .HostConfig.DeviceRequests}}' 2>/dev/null)
                gs_runtime=$(docker inspect gpustack --format '{{.HostConfig.Runtime}}' 2>/dev/null)
                if echo "$gs_devreq" | grep -qi "gpu\|nvidia" || [ "$gs_runtime" = "nvidia" ]; then
                    pass "gpustack has NVIDIA GPU access (device request or nvidia runtime — GPU inference wired)"
                else
                    fail "gpustack is RUNNING without GPU access (no device request, runtime='$gs_runtime') — inference silently on CPU. Ensure modules/llm/compose.devices.nvidia.yml is in COMPOSE_FILE and the nvidia-container-toolkit is installed, then recreate gpustack"
                fi
            fi
        fi
        ;;
esac

# _host_bind_owners: which container (and which container ports) each
# *_HOST_BIND key exposes, as `KEY container port/proto,...` (`*` = every port of
# that container; its port is itself env-driven). Kept in step with the compose
# files by tests/unit/status/test_2526_status_names_intended_lan_binds.py.
_host_bind_owners() {
    cat <<'EOF'
WAZUH_MANAGER_HOST_BIND wazuh-manager 1514/tcp,1515/tcp
WAZUH_SYSLOG_HOST_BIND wazuh-manager 514/tcp,514/udp
OPENUEM_NATS_HOST_BIND openuem-nats *
OPENUEM_OCSP_HOST_BIND openuem-ocsp-responder *
GPUSTACK_HOST_BIND gpustack *
GPUSTACK_WORKER_HOST_BIND gpustack 10150/tcp,10151/tcp
EOF
}

# _lan_bind_verdict <container> <ip:port->port/proto> <declared KEY=ip pairs, space-separated>
# #2526/#2527: a bind to a SPECIFIC LAN address is intended only when a
# *_HOST_BIND key names that address AND that key owns this container and this
# container port (_host_bind_owners). The same address bound by any other
# container or port is a FAIL; an address no key names is a WARN.
# Loopback and wildcards are not this function's business (the caller FAILs
# 0.0.0.0 and [::]).
_lan_bind_verdict() {
    local name="$1" bind="$2" declared="$3" ip="${2%%:*}" cport="${2#*->}"
    local pair key kip owner ports on_address=""
    case "$ip" in 127.*|0.0.0.0) printf 'skip\t\n'; return 0 ;; esac
    for pair in $declared; do
        key="${pair%%=*}"; kip="${pair#*=}"
        [ "$kip" = "$ip" ] || continue
        on_address="${on_address:+$on_address, }$key"
        read -r owner ports < <(_host_bind_owners | awk -v k="$key" '$1 == k {print $2, $3}') || true
        [ "$owner" = "$name" ] || continue
        case ",$ports," in
            *,\*,*|*",$cport,"*)
                printf 'pass\tintended LAN bind: %s %s (named by %s)\n' "$name" "$bind" "$key"
                return 0 ;;
        esac
    done
    if [ -n "$on_address" ]; then
        printf 'fail\tLAN bind not covered by its key: %s %s — %s names %s for another container or port\n' "$name" "$bind" "$on_address" "$ip"
    else
        printf 'warn\tundeclared LAN bind: %s %s — no *_HOST_BIND key in .env names %s\n' "$name" "$bind" "$ip"
    fi
}

# The NETWORK EXPOSURE rows that read `docker ps`: a 0.0.0.0 bind outside the
# allowed set FAILs (and so does a [::] bind); a bind to a specific LAN address
# is named: PASS when the *_HOST_BIND key that owns the container and port
# declares it, FAIL when the address belongs to another key's container or
# port, WARN when no key names it (#2526/#2527).
status_network_exposure_rows() {
    local line name ports bind host_port bad_binds verdict gpustack_ports declared
    if ! command -v docker >/dev/null 2>&1; then
        skip "docker unavailable"
        return 0
    fi
    declared="$(grep -E '^[A-Z0-9_]+_HOST_BIND=' "$ENV_FILE" 2>/dev/null | tr -d "\"'\r" | tr '\n' ' ')"
    # Acceptable 0.0.0.0 binds: caddy 80/443, gitea SSH 2222
    bad_binds=""
    while read -r line; do
        [ -z "$line" ] && continue
        name=$(echo "$line" | awk '{print $1}')
        ports=$(echo "$line" | cut -d$'\t' -f2-)
        # Look for 0.0.0.0:<port>-> and [::]:<port>-> (the IPv6 wildcard) patterns
        for bind in $(echo "$ports" | grep -oE "(0\.0\.0\.0|\[::\]):[0-9-]+->[0-9-]+/(tcp|udp)" || true); do
            host_port="${bind%%->*}"; host_port="${host_port##*:}"
            case "$name" in
                caddy)
                    case "$host_port" in
                        80|443) ;;
                        *) bad_binds="$bad_binds\n$name $bind (unexpected port)" ;;
                    esac
                    ;;
                gitea)
                    case "$host_port" in
                        2222) ;;
                        *) bad_binds="$bad_binds\n$name $bind (unexpected port)" ;;
                    esac
                    ;;
                *)
                    bad_binds="$bad_binds\n$name $bind"
                    ;;
            esac
        done
        # A bind to a specific address: intended when a *_HOST_BIND key names it
        for bind in $(echo "$ports" | grep -oE "([0-9]{1,3}\.){3}[0-9]{1,3}:[0-9-]+->[0-9-]+/(tcp|udp)" | grep -v '^0\.0\.0\.0:' || true); do
            verdict="$(_lan_bind_verdict "$name" "$bind" "$declared")"
            case "${verdict%%$'\t'*}" in
                pass) pass "${verdict#*$'\t'}" ;;
                warn) warn "${verdict#*$'\t'}" ;;
                fail) fail "${verdict#*$'\t'}" ;;
            esac
        done
    done < <(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null)
    if [ -z "$bad_binds" ]; then
        pass "only intended LAN binds (caddy 80/443, gitea 2222)"
    else
        while IFS= read -r b; do
            [ -n "$b" ] && fail "unexpected LAN bind: $b"
        done < <(echo -e "$bad_binds" | tail -n +2)
    fi
    # F-RC5-1 specific check: gpustack admin UI loopback-only
    gpustack_ports=$(docker port gpustack 2>/dev/null || true)
    if [ -n "$gpustack_ports" ]; then
        local wild_main wild_worker gmode
        gmode="$(env_get GPUSTACK_MODE)"
        # Which key owns a wildcard-published port: 10150/10151 follow
        # GPUSTACK_WORKER_HOST_BIND (0.0.0.0 is what init writes for master and
        # worker mode, on purpose); every other port follows GPUSTACK_HOST_BIND.
        wild_main=$(printf '%s\n' "$gpustack_ports" | awk '$2 == "->" && $3 ~ /^(0\.0\.0\.0|\[?::\]?):/ { split($1, a, "/"); if (a[1] != 10150 && a[1] != 10151) print $1 }' | paste -sd, - || true)
        wild_worker=$(printf '%s\n' "$gpustack_ports" | awk '$2 == "->" && $3 ~ /^(0\.0\.0\.0|\[?::\]?):/ { split($1, a, "/"); if (a[1] == 10150 || a[1] == 10151) print $1 }' | paste -sd, - || true)
        if [ -n "$wild_main" ]; then
            fail "gpustack ports bound on all interfaces: ${wild_main} — set GPUSTACK_HOST_BIND to 127.0.0.1 (standalone) or to the one LAN address the workers use (master)"
        fi
        if [ -n "$wild_worker" ]; then
            case "$gmode" in
                master|worker) pass "gpustack worker ports ${wild_worker} on all interfaces, as GPUSTACK_MODE=${gmode} intends (firewall them to the worker subnet)" ;;
                *) fail "gpustack worker ports bound on all interfaces: ${wild_worker} — set GPUSTACK_WORKER_HOST_BIND=127.0.0.1 (standalone); GPUSTACK_HOST_BIND does not control them" ;;
            esac
        fi
        if [ -z "$wild_main" ] && [ -z "$wild_worker" ]; then
            pass "gpustack on 127.0.0.1 only"
        fi
    fi
    return 0
}

# ============================================================================
# CATEGORY 5 — NETWORK EXPOSURE
# ============================================================================
cat_begin "NETWORK EXPOSURE"
status_network_exposure_rows
if command -v docker >/dev/null 2>&1; then
    # #2571: the Caddy admin API must not answer from a shared network
    status_caddy_admin_exposure_row
    # #2614: the unauthenticated Ray ports are published on loopback only
    status_gpustack_ray_binds_row
    # #2086: the Docling MCP egress deny list names the box's own addresses
    status_docling_egress_row
    # #2524: the Dify SSRF deny list is well-formed (Caddy starts with it)
    status_dify_ssrf_deny_row
fi

# status_egress_firewall_row <mode> — the host egress firewall against the mode.
# Online behind the firewall is a broken box; offline without it is a normal,
# optional state (the firewall is opt-in), so that one is informational.
status_egress_firewall_row() {
    local mode="$1"
    if razzfazz_offline_egress_firewall_active; then
        case "$mode" in
            online)  fail "network mode is online but the host egress firewall is still active (all non-LAN egress is dropped): $(razzfazz_offline_egress_undo_command)" ;;
            offline) pass "host egress firewall active (kernel-enforced offline)" ;;
            *)       info "host egress firewall active: only LAN destinations are reachable. Lift: $(razzfazz_offline_egress_undo_command)" ;;
        esac
    elif [ "$mode" = "offline" ]; then
        info "host egress firewall not active (optional: harden-offline-host.sh --egress-firewall makes offline kernel-enforced; the nft table is only readable as root)"
    fi
}

# status_time_source_row <network-mode> — the time source setting (TIME_SOURCE) and
# what chrony measures. PASS within STATUS_TIME_WARN_SECS (default 1 s) of the
# source, WARN up to STATUS_TIME_FAIL_SECS (default 30 s: the TOTP window), FAIL
# beyond. A configured source that is not synchronised is a WARN; "none" is an
# informational state (the clock is free-running by choice).
status_time_source_row() {
    local net="$1" mode servers eff tracking leap off secs warn_s fail_s label
    mode="$(env_get TIME_SOURCE)"; mode="${mode:-internet}"
    servers="$(env_get TIME_SERVERS)"
    eff="$mode"
    [ "$net" = "offline" ] && [ "$mode" = "internet" ] && eff="none"
    label="time source: $mode${servers:+ ($servers)}"
    local queue="${RAZZFAZZ_TIME_QUEUE_DIR:-${SCRIPT_DIR:-.}/.probe-queue}" rstatus rreason
    if [ -f "$queue/time-source.pending" ]; then
        warn "$label was saved in the portal but is not applied on this box yet (the host applies it automatically; if this stays: sudo rzfz setup --time-source)"
        return 0
    fi
    rstatus="$(sed -n 's/^status=//p' "$queue/time-source.result" 2>/dev/null | head -1)" || rstatus=""
    case "$rstatus" in
        failed|rejected)
            rreason="$(sed -n 's/^reason=//p' "$queue/time-source.result" 2>/dev/null | head -1)" || rreason=""
            warn "$label: the last change from the portal was $rstatus (${rreason:-no reason recorded}). Apply it by hand: sudo rzfz setup --time-source"
            return 0 ;;
    esac
    if [ "$eff" = "none" ]; then
        if [ "$mode" = "none" ]; then
            info "$label: no time server, the clock is free-running"
        else
            warn "$label, but this box is offline: the pools are unreachable and the clock is free-running. Set a LAN server: rzfz setup --time-source lan --servers HOST"
        fi
        return 0
    fi
    if ! command -v chronyc >/dev/null 2>&1; then
        warn "$label: chronyc not found, the sync state cannot be read"
        return 0
    fi
    tracking="$(timeout 5 chronyc tracking 2>/dev/null)" || tracking=""
    leap="$(printf '%s\n' "$tracking" | sed -n 's/^Leap status *: *//p' | head -1)"
    if [ -z "$leap" ]; then
        warn "$label: chrony does not answer (systemctl status chrony)"
        return 0
    fi
    if [ "$leap" != "Normal" ]; then
        warn "$label: not synchronised (leap status: $leap). Check the source is reachable: chronyc sources"
        return 0
    fi
    secs="$(printf '%s\n' "$tracking" | sed -n 's/^System time *: *\([0-9.eE+-]*\) seconds.*/\1/p' | head -1)"
    warn_s="${STATUS_TIME_WARN_SECS:-1}"; fail_s="${STATUS_TIME_FAIL_SECS:-30}"
    off="${secs:-0}"
    if awk -v o="$off" -v t="$fail_s" 'BEGIN{exit !(o+0 >= t+0)}'; then
        fail "$label: the clock is ${off} s off the source (limit ${fail_s} s): TLS, MFA codes and log correlation break"
    elif awk -v o="$off" -v t="$warn_s" 'BEGIN{exit !(o+0 >= t+0)}'; then
        warn "$label: the clock is ${off} s off the source (limit ${warn_s} s)"
    else
        pass "$label: synchronised, offset ${off} s"
    fi
}

# #3230: every COMPOSE_FILE entry must name a repo-relative path that stays
# inside the stack directory. An absolute entry — or one that resolves outside
# the tree through `..` or a symlink — is unreadable inside razzfazz-config's
# read-only mount of the repo, so `docker compose ps` fails there and the
# Settings dashboard has to fall back to `docker ps`. WARN, not FAIL: the
# allow-list already refuses `compose up` on such a chain, so nothing
# dangerous runs; the operator just has to move the overlay into the tree.
status_compose_file_tree_row() {
    [ -f "$ENV_FILE" ] || return 0
    local chain e root resolved _ifs bad=0
    chain="$(env_get COMPOSE_FILE)"
    if [ -z "$chain" ]; then
        pass "COMPOSE_FILE not set — compose discovers the in-tree compose.yml itself"
        return 0
    fi
    root="$(cd "${SCRIPT_DIR:-$(dirname "$ENV_FILE")}" 2>/dev/null && pwd -P)"
    [ -n "$root" ] || root="$(dirname "$ENV_FILE")"
    _ifs="$IFS"; IFS=':'
    for e in $chain; do
        [ -n "$e" ] || continue
        case "$e" in
            /*|'\'*|~*)
                warn "COMPOSE_FILE entry '$e' is an absolute path outside the stack directory ($root) — the Settings portal cannot read it from its read-only mount, so 'docker compose ps' fails there. Move the overlay into the stack directory as a repo-relative 'compose.local.<name>.yml' (the place for box-local overlays) and point COMPOSE_FILE at it"
                bad=1
                continue
                ;;
        esac
        resolved="$(realpath -m -- "$root/$e" 2>/dev/null || true)"
        if [ -n "$resolved" ]; then
            case "$resolved" in
                "$root"|"$root"/*) continue ;;
            esac
        else
            # realpath failed: judge textually, conservatively — only a
            # leading/trailing '..' clearly escapes the tree.
            case "$e" in
                ../*|..|*/..) ;;
                *) continue ;;
            esac
        fi
        warn "COMPOSE_FILE entry '$e' resolves outside the stack directory ($root -> ${resolved:-unresolvable}) — the Settings portal cannot read it from its read-only mount, so 'docker compose ps' fails there. Move the overlay into the stack directory as a repo-relative 'compose.local.<name>.yml' (the place for box-local overlays) and point COMPOSE_FILE at it"
        bad=1
    done
    IFS="$_ifs"
    if [ "$bad" -eq 0 ]; then
        pass "COMPOSE_FILE chain stays inside the stack directory ($chain)"
    fi
    return 0
}

# #3412: box-local overlays — compose.local.<name>.yml in the stack root, the
# sanctioned place for an overlay that belongs to one box. Listed (INFO), so an
# operator sees what this box adds to the shipped compose model; an entry whose
# file is missing WARNs (compose refuses the whole chain); a file on disk that
# COMPOSE_FILE does not name is listed too (it does nothing until it is named).
# A box without any prints nothing.
status_compose_local_overlays_row() {
    [ -f "$ENV_FILE" ] || return 0
    local chain e root _ifs used="" missing="" unused="" f base size
    # The same literal as razzfazz_common/compose_local.py NAME_ERE and
    # scripts/lib.sh (a test pins all three equal): the names a backup carries.
    local ere='^compose\.local\.[A-Za-z0-9._-]+\.yml$' max=1048576
    chain="$(env_get COMPOSE_FILE)"
    root="${SCRIPT_DIR:-$(dirname "$ENV_FILE")}"
    _ifs="$IFS"; IFS=':'
    for e in $chain; do
        case "${e#./}" in
            */*) ;;
            compose.local.?*.yml)
                if [ ! -e "$root/${e#./}" ] && [ ! -L "$root/${e#./}" ]; then
                    missing="${missing}${e}"$'\n'
                elif [[ "${e#./}" =~ $ere ]]; then
                    used="${used} ${e}"
                fi   # a name outside the rule is named below, with the file
                ;;
        esac
    done
    IFS="$_ifs"
    for f in "$root"/compose.local.?*.yml; do
        [ -f "$f" ] || [ -L "$f" ] || continue
        base="${f##*/}"
        if ! [[ "$base" =~ $ere ]]; then
            warn "box-local overlay '$base' cannot be backed up and the Settings portal refuses it in COMPOSE_FILE — rename it with only letters, digits, '.', '_' and '-' after compose.local."
            continue
        fi
        if [ -L "$f" ]; then
            warn "box-local overlay '$base' is a symlink — a backup does not follow it; make it a regular file"
        else
            size=$(stat -c %s "$f" 2>/dev/null || echo 0)
            if [ "$size" -gt "$max" ]; then
                warn "box-local overlay '$base' is larger than 1 MiB — a backup does not carry it"
            fi
        fi
        case " ${used} " in *" ${base} "*|*" ./${base} "*) ;; *) unused="${unused} ${base}" ;; esac
    done
    while IFS= read -r e; do
        [ -n "$e" ] || continue
        warn "COMPOSE_FILE names the box-local overlay '$e', but the file is missing from the stack directory ($root) — docker compose refuses the whole chain. Restore the file, or remove the entry from COMPOSE_FILE"
    done <<< "$missing"
    if [ -n "$used" ]; then
        info "box-local compose overlays in use:${used} (kept across upgrades)"
    fi
    if [ -n "$unused" ]; then
        info "box-local overlay file(s) present but not in COMPOSE_FILE (no effect):${unused}"
    fi
    return 0
}

# ============================================================================
# CATEGORY — OFFLINE / NETWORK (#184 P2)
# ============================================================================
# The box's egress axis (online|proxied|offline) + registry mirror, which
# overlays are composed, and the verify-images / verify-models present/missing
# counts. lib.sh (sourced above) provides razzfazz_network_mode, the overlay
# constants, and compose_file_overlay_present.
cat_begin "OFFLINE / NETWORK"
net_mode=$(razzfazz_network_mode "$ENV_FILE" 2>/dev/null)
case "$net_mode" in
    offline) info "network mode: offline (no internet egress; LAN stays up)" ;;
    proxied) info "network mode: proxied (egress via corporate proxy)" ;;
    online)  info "network mode: online (direct egress — default)" ;;
    *)        info "network mode: ${net_mode:-unknown}" ;;
esac
status_egress_firewall_row "$net_mode"
status_time_source_row "$net_mode"
offline_flag=$(env_get RAZZFAZZ_OFFLINE)
info "RAZZFAZZ_OFFLINE=${offline_flag:-0}"
mirror=$(env_get RAZZFAZZ_REGISTRY_MIRROR)
if [ -n "$mirror" ]; then
    info "registry mirror: $mirror (install/upgrade pulls redirected)"
else
    info "registry mirror: none (upstream registries)"
fi
# Overlays composed (COMPOSE_FILE chain)
status_compose_file_tree_row
status_compose_local_overlays_row
if compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_NOBUILD_OVERLAY"; then
    pass "no-build overlay composed (up/enable/disable never build)"
else
    warn "no-build overlay NOT composed (compose.no-build.yml) — 'rzfz upgrade' wires it"
fi
if [ "$net_mode" = "offline" ]; then
    if compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_OFFLINE_OVERLAY"; then
        pass "offline overlay composed (pull_policy: never + telemetry off)"
    else
        warn "offline mode but compose.offline.yml not composed — 'rzfz setup --network-mode --mode offline'"
    fi
fi
if [ -n "$mirror" ]; then
    if compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_REGISTRY_MIRROR_OVERLAY"; then
        pass "registry-mirror overlay composed"
    else
        warn "RAZZFAZZ_REGISTRY_MIRROR set but compose.registry-mirror.yml not composed"
    fi
fi
# verify-images / verify-models present/missing counts (best-effort — needs docker;
# `timeout` guards a large image set). Same summary line the release gate parses.
if command -v docker >/dev/null 2>&1; then
    for tool in verify-images verify-models; do
        vscript="$SCRIPT_DIR/cli/${tool}.sh"
        if [ ! -x "$vscript" ] && [ ! -f "$vscript" ]; then
            skip "$tool: script not present"
            continue
        fi
        vout=$(timeout 90 bash "$vscript" --quiet 2>&1)
        vline=$(echo "$vout" | grep -oE '[0-9]+ expected, [0-9]+ present, [0-9]+ missing' | head -1)
        if [ -n "$vline" ]; then
            v_exp=$(echo "$vline" | awk '{print $1}')
            v_pres=$(echo "$vline" | awk '{print $3}')
            v_miss=$(echo "$vline" | awk '{print $5}')
            if [ "${v_miss:-1}" -eq 0 ] && [ "$tool" = verify-images ] && printf '%s' "$vout" | grep -q '\[  stale\]'; then
                # #2757: present is not this tree's build. Never "offline-ready".
                _stale_n=$(printf '%s\n' "$vout" | grep -c '\[  stale\]' || true)
                if [ "$(env_get RAZZFAZZ_NETWORK_MODE)" = "offline" ]; then
                    fail "$tool: $v_pres/$v_exp present, but ${_stale_n} custom image(s) are NOT this tree's build — needs a package with images or an online rebuild"
                else
                    warn "$tool: $v_pres/$v_exp present, but ${_stale_n} custom image(s) are NOT this tree's build (an online upgrade rebuilds them)"
                fi
            elif [ "${v_miss:-1}" -eq 0 ]; then
                pass "$tool: $v_pres/$v_exp present, 0 missing (offline-ready)"
                if [ "$tool" = verify-models ]; then status_models_unavailable_row "$vout"; fi
            else
                # Operator rule #430 (2026-08-18), refined in #367 (2026-08-24):
                # scope stays the FULL catalog — "offline-ready" means EVERY
                # image is staged; which profiles are enabled is the customer's
                # call and may change later. Severity follows
                # RAZZFAZZ_NETWORK_MODE: an offline box FAILs on even one
                # missing image (it can never fetch it); an online box gets a
                # plain INFO (obtainable on demand — not even a warning).
                # env_get, NOT `read_env_value .env`: `rzfz` execs this
                # script without changing directory and the `cd "$SCRIPT_DIR"`
                # above lives inside the "is a git repo" branch. On a box
                # installed from an OFFLINE PACKAGE (no .git) — precisely the
                # box most likely to be RAZZFAZZ_NETWORK_MODE=offline — the
                # relative path resolved against the operator's cwd, the read
                # returned nothing, and the check that should FAIL
                # "unobtainable" degraded to a plain info. env_get uses the
                # absolute $ENV_FILE, like every other value in this script.
                _net_mode="$(env_get RAZZFAZZ_NETWORK_MODE)"
                if [ "${_net_mode:-online}" = "offline" ]; then
                    fail "$tool: $v_miss of $v_exp missing (not offline-ready — offline box, unobtainable)"
                else
                    info "$tool: $v_miss of $v_exp missing (not offline-ready; obtainable on demand — ${_net_mode:-online} mode)"
                fi
            fi
        else
            skip "$tool: could not determine (enumeration degraded / no docker exec)"
        fi
    done
else
    skip "verify-images/verify-models need docker (unavailable)"
fi

# ============================================================================
# CATEGORY 6 — HOST HARDENING
# ============================================================================
cat_begin "HOST HARDENING"
if [ -f "$HARDENING_MARKER" ]; then
    ts=$(grep '^hardened_at=' "$HARDENING_MARKER" | cut -d= -f2-)
    pass "host-hardened marker present ($ts)"
else
    fail "host-hardened marker missing — scripts/harden-host.sh has not been run"
fi
# UFW (needs sudo to query status; degrade gracefully)
if command -v ufw >/dev/null 2>&1; then
    ufw_out=$(sudo -n ufw status 2>/dev/null | head -1 || true)
    if [ -z "$ufw_out" ]; then
        # check the systemd unit instead — works without sudo
        case "$(systemctl is-active ufw 2>/dev/null)" in
            active) pass "ufw active (via systemctl)" ;;
            *)      fail "ufw inactive" ;;
        esac
    else
        case "$ufw_out" in
            *active*|*Aktiv*|*aktiv*) pass "ufw active" ;;
            *)                        fail "ufw inactive" ;;
        esac
    fi
fi
status_gpustack_master_ports_row
status_docker_socket_row
status_metadata_block_row
# fail2ban
if command -v systemctl >/dev/null 2>&1; then
    case "$(systemctl is-active fail2ban 2>/dev/null)" in
        active) pass "fail2ban active" ;;
        *) fail "fail2ban inactive" ;;
    esac
    case "$(systemctl is-active auditd 2>/dev/null)" in
        active) pass "auditd active" ;;
        *) warn "auditd inactive (F-071)" ;;
    esac
    case "$(systemctl is-active unattended-upgrades 2>/dev/null)" in
        active) pass "unattended-upgrades active" ;;
        *) warn "unattended-upgrades inactive (F-058)" ;;
    esac
fi
# sysctl files
_SYSCTL_SRC="$SCRIPT_DIR/core/sysctl/99-razzfazz-stability.conf"
if [ -f /etc/sysctl.d/99-razzfazz-stability.conf ]; then
    if [ -r "$_SYSCTL_SRC" ] && ! cmp -s "$_SYSCTL_SRC" /etc/sysctl.d/99-razzfazz-stability.conf; then
        # #855: presence alone said nothing. A box installed before a tunable
        # landed keeps the old file and reports green while the service that
        # needs the tunable cannot boot (wazuh-indexer / vm.max_map_count).
        warn "sysctl stability tunables STALE (drop-in differs from the repo copy)"
        info "  sudo cp $_SYSCTL_SRC /etc/sysctl.d/ && sudo sysctl --system"
    else
        # Unreadable repo copy (offline package layout, permissions) degrades to
        # the pre-#855 behaviour rather than a false STALE — status.sh is a
        # posture reporter, not a gate.
        pass "sysctl stability tunables present and current"
    fi
else
    warn "sysctl stability tunables missing (run scripts/harden-host.sh OR sudo cp core/sysctl/...)"
fi
if [ -f /etc/sysctl.d/99-razzfazz-hardening.conf ]; then
    pass "sysctl hardening tunables present (F-060)"
else
    warn "sysctl hardening tunables missing (F-060)"
fi
# Docker daemon
#
# #546: these were WARN. The host-hardened marker above is a PASS, and it
# attests only that harden-host.sh RAN — not that every step applied. A box
# whose Docker hardening silently no-opped therefore rendered as
#   [7] HOST HARDENING  ok marker present ... ! missing no-new-privileges
# which reads as "hardened" at a glance, and did on a customer box.
# no-new-privileges is the strongest container-runtime control we ship: without
# it a compromised process in any container can regain privileges it dropped.
# Its absence is a FAIL, so the category cannot report green around it.
if [ -f /etc/docker/daemon.json ]; then
    if grep -q "no-new-privileges" /etc/docker/daemon.json 2>/dev/null; then
        # daemon.json is what the NEXT daemon start reads; a `systemctl reload`
        # does not activate no-new-privileges, and the daemon copies its default
        # only into containers it CREATES (live-restore keeps the old ones
        # running across a restart). So: the daemon's SecurityOptions, and one
        # running service that relies on the default, read from /proc.
        _nnp_fix="cd ${SCRIPT_DIR} && ./rzfz down && sudo bash ${SCRIPT_DIR}/scripts/harden-host.sh && ./rzfz start (maintenance window). If harden-host fails, the stack stays down: run ./rzfz start, fix the cause from the log, and repeat."
        _nnp_running=$(docker info --format '{{json .SecurityOptions}}' 2>/dev/null || true)
        _nnp_daemon=unknown
        case "$_nnp_running" in
            "") ;;
            *name=no-new-privileges*) _nnp_daemon=on ;;
            *)  _nnp_daemon=off
                # Docker < 24 never lists it, set or not
                _nnp_ver=$(docker info --format '{{.ServerVersion}}' 2>/dev/null || true)
                case "${_nnp_ver%%.*}" in ''|*[!0-9]*) ;; *) [ "${_nnp_ver%%.*}" -ge 24 ] || _nnp_daemon=old ;; esac ;;
        esac
        _nnp_sample=$(razzfazz_nnp_sample)
        _nnp_svc="${_nnp_sample% *}"; _nnp_val="${_nnp_sample##* }"
        if [ "$_nnp_daemon" = off ]; then
            fail "no-new-privileges is in daemon.json but NOT active in the running Docker daemon (F-059) — recreate the stack: ${_nnp_fix}"
        elif [ -n "$_nnp_sample" ] && [ "$_nnp_val" = 0 ]; then
            fail "$([ "$_nnp_daemon" = on ] && echo "Docker daemon has no-new-privileges" || echo "no-new-privileges is in daemon.json"), but container '${_nnp_svc}' runs WITHOUT it (NoNewPrivs 0; created before the setting) (F-059) — recreate the stack: ${_nnp_fix}"
        elif [ -n "$_nnp_sample" ]; then
            pass "Docker no-new-privileges active (F-059; sampled '${_nnp_svc}': NoNewPrivs 1)"
        elif [ "$_nnp_daemon" = on ]; then
            pass "Docker daemon has no-new-privileges (F-059; no running container verified)"
        elif [ "$_nnp_daemon" = old ]; then
            warn "no-new-privileges is in daemon.json, but not verified: Docker ${_nnp_ver} (older than 24) does not report the setting and no running container could be read (F-059)"
        else
            pass "Docker daemon has no-new-privileges in daemon.json (F-059; running daemon not readable)"
        fi
    else
        fail "Docker daemon present but missing no-new-privileges (F-059) — re-run 'sudo ./scripts/harden-host.sh'"
    fi
    # #139: without json-file log caps every chatty container grows an
    # unbounded log on the root disk — one of the slow fillers behind the
    # prod disk-full outages. harden-host writes max-size/max-file; a box
    # that skipped it deserves a warning, not silence.
    if grep -q "max-size" /etc/docker/daemon.json 2>/dev/null; then
        pass "Docker log rotation configured (max-size, F-059/#139)"
    else
        warn "Docker log rotation NOT configured — container logs grow unbounded on the root disk. Re-run 'sudo ./scripts/harden-host.sh' (applies on daemon restart)."
    fi
else
    fail "/etc/docker/daemon.json absent (F-059) — re-run 'sudo ./scripts/harden-host.sh'"
fi

# #139: root-disk usage gauge. Both prod outages (2026-06-11, 2026-08-03)
# were silent 100%-fills that only surfaced as "Authentik is down" — a
# postgres end-of-recovery checkpoint cannot write and crash-loops. Warn
# early, fail loud. Threshold overridable for boxes that legitimately run
# full (RAZZFAZZ_DISK_WARN_PCT, default 85; FAIL at 95).
_disk_warn="${RAZZFAZZ_DISK_WARN_PCT:-85}"
_disk_used=$(df -P / 2>/dev/null | awk 'NR==2 {gsub(/%/,""); print $5}')
_disk_avail=$(df -Ph / 2>/dev/null | awk 'NR==2 {print $4}')
case "$_disk_used" in ''|*[!0-9]*) _disk_used="" ;; esac
if [ -z "$_disk_used" ]; then
    warn "Could not read root-disk usage (#139)"
elif [ "$_disk_used" -ge 95 ]; then
    fail "Root disk ${_disk_used}% full (${_disk_avail} free) — postgres crash-loops at 100% (#139). Free space NOW: 'docker builder prune -af', prune old backups, check /var/lib/docker/volumes with 'du -x -d1 | sort -rh'."
elif [ "$_disk_used" -ge "$_disk_warn" ]; then
    warn "Root disk ${_disk_used}% full (${_disk_avail} free, warn at ${_disk_warn}%) — see #139. Candidates: 'docker builder prune -af', old backups, regenerable caches."
else
    pass "Root disk ${_disk_used}% used (${_disk_avail} free)"
fi

# #692 overcommit lamp: the SUM of running agent-container memory limits vs
# host RAM. Per-container limits all held on 0.208 and the host still froze —
# distributed load under compliant limits drains RAM jointly. The durable cap
# is razzfazz-agents.slice (harden-host); this lamp makes the exposure visible.
if command -v docker >/dev/null 2>&1; then
    _agent_sum_b=0
    while IFS= read -r _cid; do
        [ -n "$_cid" ] || continue
        _m=$(docker inspect -f '{{.HostConfig.Memory}}' "$_cid" 2>/dev/null)
        [ "${_m:-0}" -gt 0 ] 2>/dev/null && _agent_sum_b=$(( _agent_sum_b + _m ))
    done < <(docker ps -q --filter "label=razzfazz.agent.type" 2>/dev/null)
    if [ "$_agent_sum_b" -gt 0 ]; then
        _agent_sum_gb=$(( _agent_sum_b / 1024 / 1024 / 1024 ))
        _host_ram_gb=$(( $(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1024 / 1024 ))
        if [ -f /etc/systemd/system/razzfazz-agents.slice ]; then
            pass "agent memory: ${_agent_sum_gb}G summed limits under razzfazz-agents.slice (host ${_host_ram_gb}G)"
        elif [ "$_host_ram_gb" -gt 0 ] && [ $(( _agent_sum_gb * 2 )) -ge "$_host_ram_gb" ]; then
            warn "agent memory OVERCOMMIT: summed agent limits ${_agent_sum_gb}G vs ${_host_ram_gb}G host RAM and NO razzfazz-agents.slice — a joint agent load can freeze the host (#692). Fix: re-run 'sudo ./scripts/harden-host.sh'"
        else
            info "agent memory: ${_agent_sum_gb}G summed limits, no shared slice yet (host ${_host_ram_gb}G) — harden-host installs razzfazz-agents.slice (#692)"
        fi
    fi
fi

# ============================================================================
# CATEGORY 7 — SECRETS
# ============================================================================
cat_begin "SECRETS"
required_secrets="AUTHENTIK_BOOTSTRAP_PASSWORD AUTHENTIK_SECRET_KEY POSTGRES_PASSWORD VALKEY_PASSWORD WEBUI_SECRET_KEY"
for k in $required_secrets; do
    v=$(env_get "$k")
    if [ -z "$v" ]; then
        fail "$k empty (required)"
    fi
done
backup_pw=$(env_get BACKUP_ENCRYPTION_PASSWORD)
if [ -n "$backup_pw" ]; then
    pass "BACKUP_ENCRYPTION_PASSWORD set (F-A2 closed)"
else
    fail "BACKUP_ENCRYPTION_PASSWORD empty (F-A2)"
fi
status_data_bound_secret_rows
gpu_key=$(env_get GPUSTACK_API_KEY)
# #1902: only ask when GPUStack is ON THIS BOX. A manager-only box has no
# GPUStack to issue the key and no consumer that uses it — the key is
# legitimately empty there, and asking anyway put three FAILs on a healthy box.
# Same profile test the LLM RUNTIME category above already uses.
#
# `skip`, not `pass`: a pass would claim something was verified. Nothing was —
# the question does not apply here, and that is what the line says.
if _gpustack_on_this_box; then
    case "$gpu_key" in
        ""|*CHANGEME*|*PLACEHOLDER*) fail "GPUSTACK_API_KEY is placeholder/empty — init-backends.py won't authenticate" ;;
        *) pass "GPUSTACK_API_KEY non-placeholder (length ${#gpu_key})" ;;
    esac
else
    skip "GPUSTACK_API_KEY not required — no GPUStack profile on this box (the LLM Manager fronts /v1)"
fi
# count empty secrets in general
empty_secrets=0
while IFS= read -r line; do
    case "$line" in
        *PASSWORD=|*SECRET=|*TOKEN=|*KEY=) empty_secrets=$((empty_secrets + 1)) ;;
    esac
done < <(grep -E '^[A-Z_]+=$' "$ENV_FILE" 2>/dev/null)
if [ "$empty_secrets" -eq 0 ]; then
    pass "no empty *_PASSWORD/SECRET/TOKEN/KEY entries in .env"
else
    info "$empty_secrets empty secret-shaped entries in .env (may be optional / per-profile)"
fi

# ============================================================================
# CATEGORY 8 — SSH POSTURE (informational only)
# ============================================================================
# sshd keeps the FIRST value it reads for a keyword, with each Include expanded
# in place (glob in lexical order, relative paths next to the main file), and a
# Match line ends the global part of the file it is in. Prints
# "<value> <file>" for the first global <key>; 1 when no file sets it.
_sshd_first_value() {
    local key="$1" file="$2" base="$3" line k v pat f
    local -a pats=()
    [ -r "$file" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"
        read -r k v <<<"$line"
        [ -n "$k" ] || continue
        case "${k,,}" in
            match) return 1 ;;
            include)
                read -r -a pats <<<"$v"
                for pat in "${pats[@]}"; do
                    [[ "$pat" == /* ]] || pat="$base/$pat"
                    for f in $pat; do
                        [ -f "$f" ] || continue
                        _sshd_first_value "$key" "$f" "$base" && return 0
                    done
                done ;;
            "${key,,}") read -r v _ <<<"$v"; printf '%s %s\n' "$v" "$file"; return 0 ;;
        esac
    done < "$file"
    return 1
}

# Prints "<value>|<source>" for <key>: `sshd -T` when it runs (it needs root:
# directly or via `sudo -n`), else the files in sshd's order, else the default.
_sshd_effective() {
    local key="$1" main="$2" default="$3" t="$4" hit
    if [ -n "$t" ]; then
        hit=$(awk -v k="${key,,}" '$1 == k { print $2; exit }' <<<"$t")
        if [ -n "$hit" ]; then printf '%s|sshd -T\n' "$hit"; return 0; fi
    fi
    if hit=$(_sshd_first_value "$key" "$main" "$(dirname "$main")"); then
        printf '%s|from %s\n' "${hit%% *}" "${hit#* }"
    else
        printf '%s|default; no file sets it\n' "$default"
    fi
}

# SSH POSTURE rows. Reads the effective values, not the last line of
# sshd_config: a drop-in that sorts first (cloud-init's 50-cloud-init.conf)
# overrides the main file.
status_ssh_posture_rows() {
    local main="${1:-/etc/ssh/sshd_config}" sshd_bin t="" pa pr
    sshd_bin=$(command -v sshd 2>/dev/null || echo /usr/sbin/sshd)
    if [ -x "$sshd_bin" ]; then
        t=$("$sshd_bin" -T 2>/dev/null) || t=$(sudo -n "$sshd_bin" -T 2>/dev/null) || t=""
    fi
    if [ -z "$t" ] && [ ! -r "$main" ]; then
        skip "sshd_config not readable (need sudo)"
        return 0
    fi
    pa=$(_sshd_effective PasswordAuthentication "$main" yes "$t")
    pr=$(_sshd_effective PermitRootLogin "$main" prohibit-password "$t")
    case "${pa%%|*}" in
        no) info "PasswordAuthentication=no (key-only; ${pa#*|})" ;;
        *)  info "PasswordAuthentication=${pa%%|*} (operator choice for dev/test boxes; ${pa#*|})" ;;
    esac
    info "PermitRootLogin=${pr%%|*} (${pr#*|})"
}

cat_begin "SSH POSTURE"
status_ssh_posture_rows
# authorized_keys count for STACK_USER
stack_user=$(env_get STACK_USER)
[ -z "$stack_user" ] && stack_user="razzfazz-ai-admin"
ak="/home/$stack_user/.ssh/authorized_keys"
if [ -r "$ak" ]; then
    n=$(grep -c '^ssh-' "$ak" 2>/dev/null | head -1 | tr -d '\n ')
    info "${n:-0} pubkey(s) in $stack_user's authorized_keys"
else
    info "no authorized_keys for $stack_user (or not readable)"
fi

# ============================================================================
# CATEGORY 9 — TLS
# ============================================================================
cat_begin "TLS"
domain=$(env_get MAIN_DOMAIN)
tls_mode=$(env_get TLS_MODE)
if [ -z "$domain" ]; then
    skip "MAIN_DOMAIN not set"
else
    info "MAIN_DOMAIN=$domain  TLS_MODE=${tls_mode:-letsencrypt}"
    if command -v openssl >/dev/null 2>&1 && command -v docker >/dev/null 2>&1; then
        cert_info=$(echo | openssl s_client -servername "$domain" -connect "127.0.0.1:443" 2>/dev/null | openssl x509 -noout -dates 2>/dev/null || true)
        if [ -n "$cert_info" ]; then
            not_after=$(echo "$cert_info" | grep notAfter | cut -d= -f2-)
            pass "TLS cert present, expires: $not_after"
        else
            warn "could not retrieve TLS cert from caddy (may be starting up)"
        fi
        # #631: certs/caddy-ca.pem is a SNAPSHOT of Caddy's internal root that
        # the native-OIDC clients (openwebui, gitea, vaultwarden) mount as
        # their ENTIRE trust store. If Caddy ever re-mints its PKI (re-domain,
        # TLS-mode change, wiped caddy_data) and the export is not refreshed,
        # fresh OIDC logins 500 with CERTIFICATE_VERIFY_FAILED while existing
        # cookie sessions keep working — a silent break, found live on 0.91.
        # Probe: the LIVE caddy root's SHA-256 fingerprint must match one of
        # the certificates inside the export bundle.
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx caddy \
           && docker exec caddy test -f /data/caddy/pki/authorities/local/root.crt 2>/dev/null; then
            live_fp=$(docker exec caddy cat /data/caddy/pki/authorities/local/root.crt 2>/dev/null \
                | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
            if [ -z "$live_fp" ]; then
                warn "could not fingerprint the live Caddy internal root CA"
            elif [ ! -s "$SCRIPT_DIR/certs/caddy-ca.pem" ]; then
                fail "certs/caddy-ca.pem missing/empty while Caddy has an internal root — OIDC clients trust NOTHING. Fix: rzfz post-install --refresh (rebuilds the bundle + restarts openwebui/gitea/vaultwarden)"
            else
                # Split the bundle and fingerprint each certificate. awk writes
                # one temp file per cert; a bundle is small (dozens of certs).
                _ca_tmp=$(mktemp -d)
                awk -v dir="$_ca_tmp" '/-----BEGIN CERTIFICATE-----/{n++; f=dir "/c" n ".pem"} f{print > f} /-----END CERTIFICATE-----/{close(f); f=""}' \
                    "$SCRIPT_DIR/certs/caddy-ca.pem" 2>/dev/null
                _drift="yes"
                for _f in "$_ca_tmp"/c*.pem; do
                    [ -f "$_f" ] || continue
                    _fp=$(openssl x509 -in "$_f" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
                    if [ -n "$_fp" ] && [ "$_fp" = "$live_fp" ]; then _drift="no"; break; fi
                done
                rm -rf "$_ca_tmp" 2>/dev/null
                if [ "$_drift" = "no" ]; then
                    pass "OIDC CA bundle contains the live Caddy internal root (no drift)"
                else
                    fail "certs/caddy-ca.pem does NOT contain the LIVE Caddy internal root — fresh OIDC logins will fail with CERTIFICATE_VERIFY_FAILED while existing sessions keep working (silent break, #631). Fix: rzfz post-install --refresh"
                fi
            fi
        fi
    fi
fi

# #2719: a container's environment is fixed when it is created; a restart or a
# reboot never refreshes it. After a domain change every container that was
# not recreated still carries the old MAIN_DOMAIN / *_DOMAIN values (the start
# portal's tiles pointed at the old domain on a delivered box). Read-only,
# bounded and timed (scripts/lib/domain-env-drift.sh).
if [ -n "$domain" ] && command -v docker >/dev/null 2>&1 \
   && [ -r "${SCRIPT_DIR}/scripts/lib/domain-env-drift.sh" ]; then
    # shellcheck source=scripts/lib/domain-env-drift.sh
    . "${SCRIPT_DIR}/scripts/lib/domain-env-drift.sh"
    _drift_proj="$(timeout 10 docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' postgres 2>/dev/null || true)"
    [ -n "$_drift_proj" ] || _drift_proj="$(cd "$SCRIPT_DIR" && pwd -P)"
    _drift_rc=0
    _drift_out="$(razzfazz_domain_env_drift "${SCRIPT_DIR}/.env" "" "$_drift_proj")" || _drift_rc=$?
    if [ "$_drift_rc" -eq 2 ]; then
        warn "running containers' environment vs MAIN_DOMAIN: not measured (docker did not answer in time, or no container of this stack matched)"
    else
        _drift_rec="$(razzfazz_domain_env_drift_filter "$_drift_out" recreate)"
        _drift_prot="$(razzfazz_domain_env_drift_filter "$_drift_out" protected)"
        _drift_man="$(razzfazz_domain_env_drift_filter "$_drift_out" manual)"
        if [ "$_drift_rc" -eq 3 ]; then
            warn "running containers' environment vs MAIN_DOMAIN: not fully measured ($(printf '%s\n' "$_drift_out" | sed -n 's/^# //p'))"
        fi
        if [ -n "$_drift_rec" ]; then
            _drift_sum="$(razzfazz_domain_env_drift_summary "$_drift_rec")"
            fail "running containers still carry an old domain in their environment (fixed when a container is created; tiles and links show it): ${_drift_sum%%|*}. Fix: ${_drift_sum#*|}"
        elif [ "$_drift_rc" -eq 0 ] && [ -z "$_drift_prot" ] && [ -z "$_drift_man" ]; then
            pass "running containers' environment matches MAIN_DOMAIN and the .env domain keys"
        fi
        if [ -n "$_drift_man" ]; then
            _drift_sum="$(razzfazz_domain_env_drift_summary "$_drift_man")"
            fail "running containers still carry an old domain in their environment and are never recreated automatically (they run engines): ${_drift_sum%%|*}. Drain the engines, then: ${_drift_sum#*|}"
        fi
        if [ -n "$_drift_prot" ]; then
            warn "$(razzfazz_domain_env_drift_names "$_drift_prot") still carry an old domain in their environment; they do not route by it and pick up the current one at their next planned recreate (datastore, LLM runtime, backup or config UI)"
        fi
    fi
    unset _drift_proj _drift_rc _drift_out _drift_sum _drift_rec _drift_prot _drift_man
fi

# ============================================================================
# CATEGORY 10 — BACKUPS
# ============================================================================
cat_begin "BACKUPS"
backup_dir="$SCRIPT_DIR/backups"
# #2584: the last run, not the newest file. A nightly that wrote no archive
# left the previous night's archive as "last backup" below — a PASS the morning
# after a failed backup. The run is judged first (see cli/lib-backup-run.sh);
# when it FAILs, the newest archive is only information, never a PASS.
# The verdict may be two lines, a missed slot first and then the last run;
# `rzfz backup status` prints the same lines.
_run=""; _run_fail=""
# shellcheck source=cli/lib-backup-run.sh disable=SC1091
if . "$SCRIPT_DIR/cli/lib-backup-run.sh" 2>/dev/null; then
    _rep=$(backup_run_report)
    _run=$(backup_run_archive_verdict "$backup_dir" "$_rep" "$(backup_run_lock_state)" \
        "$(backup_run_cron_from_env "$ENV_FILE")" "$(env_get TZ)" "$(env_get BACKUP_FILENAME)" \
        "$(backup_run_hook_lock_state)" "$(backup_run_service_started)")
    while IFS= read -r _line; do
        case "${_line%%|*}" in
            OK)      pass "last backup run wrote its archive: ${_line#*|}" ;;
            RUNNING) info "${_line#*|}" ;;
            INFO)    info "${_line#*|}" ;;
            FAIL)    fail "${_line#*|}"; _run_fail=1 ;;
        esac
    done <<< "$_run"
    # #3458: the archive was written but does not hold everything (DEGRADED or
    # PARTIAL): a WARN, named, beside a run that did not FAIL.
    if [ -z "$_run_fail" ] && declare -F backup_run_quality_verdict >/dev/null 2>&1; then
        _q=$(backup_run_quality_verdict "$_rep")
        [ -n "$_q" ] && warn "${_q#*|}"
    fi
fi
# #2833: an archive without its completion mark was cut off by a failed run.
if declare -F backup_run_unmarked_archives >/dev/null 2>&1; then
    _unmarked=$(backup_run_unmarked_archives "$backup_dir" | tr '\n' ' ')
    if [ -n "$_unmarked" ]; then
        fail "backup archive(s) a run never finished (no completion mark, probably truncated): ${_unmarked}— do not restore from them; the next backup run removes them when it needs the space"
    fi
fi
if [ -d "$backup_dir" ]; then
    # Look only for actual backup ARCHIVES (skip env-snapshots/ subdir etc).
    # #2452: `*.tar.gz*` also matched the `.sha256` sidecars, so the verdict
    # could name a checksum file as the "last backup".
    last=$(ls -1t "$backup_dir"/*.tar.gz "$backup_dir"/*.tar.gz.gpg 2>/dev/null | head -1)
    # #2452: every plaintext archive is named, not only the newest. A partial
    # backup of valkey-data holds session and credential caches. Nothing is
    # deleted here: whether to keep a plaintext copy is the operator's call.
    plain=$(ls -1 "$backup_dir"/*.tar.gz 2>/dev/null | xargs -r -n1 basename | tr '\n' ' ')
    if [ -n "$plain" ] && [ "$(env_get BACKUP_ENCRYPTION_PASSWORD)" != "" ]; then
        fail "plaintext backup archive(s) in backups/ although encryption is configured: ${plain}— written before 2026.09-ga.2; remove them once an encrypted backup covers the same data"
    fi
    if [ -n "$last" ]; then
        fname=$(basename "$last")
        last_ts=$(stat -c '%y' "$last" 2>/dev/null | cut -d. -f1)
        case "$fname" in
            *.gpg) if [ -n "$_run_fail" ]; then
                       info "newest archive: $fname (GPG-encrypted, $last_ts) — see the failed backup run above"
                   else
                       pass "last backup: $fname (GPG-encrypted, $last_ts)"
                   fi ;;
            *)     fail "last backup: $fname NOT GPG-encrypted ($last_ts) — F-A2 risk" ;;
        esac
    else
        warn "no .tar.gz backup files in $backup_dir — first backup hasn't run yet"
    fi
else
    info "no backups/ directory (first install)"
fi

# Plaintext backups, env files with stack secrets and parked copies of the
# stack tree OUTSIDE the install path (its parent and $HOME). Read-only and
# bounded (depth, one filesystem, time limit); paths only, never contents. A
# failing scan must not abort the checks after it (no -e here).
if [ -x "$SCRIPT_DIR/scripts/find-stray-secrets.sh" ]; then
    stray=$("$SCRIPT_DIR/scripts/find-stray-secrets.sh" --stack-dir "$SCRIPT_DIR" 2>/dev/null)
    stray_rc=$?
    if [ "$stray_rc" -ne 0 ]; then
        warn "scan incomplete: $SCRIPT_DIR (the finder failed with exit $stray_rc) — leftovers outside the install path may not have been found"
    fi
    if [ "$stray_rc" -eq 0 ] && ! printf '%s\n' "$stray" | grep -Eq '^(PLAINTEXT_BACKUP|STRAY_ENV|PARKED_CLONE|INCOMPLETE)\|'; then
        pass "no plaintext backup, stray .env or parked stack clone outside the install path"
    fi
    if [ -n "$stray" ]; then
        while IFS='|' read -r kind path age mode; do
            case "$kind" in
                PLAINTEXT_BACKUP) fail "plaintext stack backup outside the install path: $path (${age} days old, mode $mode) — it holds every secret the box had then. Once nothing references it: shred -u '$path'" ;;
                STRAY_ENV)        fail "env file with stack secrets outside the install path: $path (${age} days old, mode $mode) — once nothing references it: shred -u '$path'" ;;
                SKIPPED)          info "$age skipped under $path (not readable by this user)" ;;
                INCOMPLETE)       warn "scan incomplete: $path ($age) — leftovers under it may not have been found" ;;
                PARKED_CLONE)     warn "parked copy of the stack tree: $path — check that no container mounts it, then remove it (docs/enterprise/how-to/backup.md, 'Stray secrets outside the install path')" ;;
            esac
        done <<< "$stray"
    fi
fi

# ============================================================================
# CATEGORY 10b — SSO FORWARD-AUTH BINDINGS
# ============================================================================
# A ProxyProvider attached to NO outpost makes Authentik answer Caddy's
# forward_auth with 404, which Caddy passes straight through. The symptom is a
# whole domain returning 404 while /healthz is 200, the container is healthy and
# every other check here is green — box 0.78 sat like that for four days, and
# a test box hit it on 2026-05-21 with 14 providers unbound.
#
# The repair has existed since then (apply-policy-bindings.py, idempotent) and is
# correct. What was missing is anyone NOTICING: the repair runs on the init
# fast-path, and `docker compose up -d` is not init, so a box that never re-runs
# init stays 404 indefinitely.
#
# Read-only: a Django query inside authentik-worker, no writes, no lock needed.
cat_begin "SSO FORWARD-AUTH"
status_admin_mfa_row
status_sso_switch_rows
status_google_domain_old_default_row
status_google_domain_unset_row
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^authentik-worker$'; then
    info "authentik-worker not running — forward-auth bindings not checked"
else
    # #2473 D5: app roles vs the role catalogue (needs Authentik up).
    status_role_drift_rows
    # #2476 D7: the Dify secret_id rotator (openbao profile only).
    status_secret_id_rotation_row
    # #2473 D7: technical users whose owner left, and revocations that keep failing.
    status_technical_user_owner_check_row
    # A1 D10: the staging canary's access-group restriction covers every app.
    status_access_group_row
    # #2473 D4: upstream group sync from Microsoft / Google (sources enabled only).
    status_upstream_sync_row
    # `ak shell -c`, NOT `python -c`: a bare interpreter in that container has no
    # DJANGO_SETTINGS_MODULE and no django.setup(), so the first model import
    # raises ImproperlyConfigured — the query never runs. `ak shell` is authentik's
    # own management shell and bootstraps Django; it is the idiom the rest of the
    # tree already uses (docs/authentik-upgrade.md:78). The alternative
    # (DJANGO_SETTINGS_MODULE=... + django.setup(), as in scripts/upgrade-diagnose.sh)
    # works too but hard-codes a settings path that upstream can move.
    #
    # Output is SENTINEL-PREFIXED because `ak shell` is a shell: any banner,
    # deprecation warning or startup line it prints lands on the same stdout. An
    # unprefixed parse would read such a line as a provider name with no attached
    # flag and report it as ORPHANED — a false 404 alarm, which costs this report
    # exactly the credibility it needs to be worth printing.
    _ak_snippet='
import sys
try:
    from authentik.outposts.models import Outpost
    from authentik.providers.proxy.models import ProxyProvider
    attached = set()
    for o in Outpost.objects.all():
        attached |= set(o.providers.values_list("pk", flat=True))
    for p in ProxyProvider.objects.all().order_by("name"):
        sys.stdout.write("RZFZ-PROVIDER\t%s\t%d\t%s\n" % (p.name, 1 if p.pk in attached else 0, p.external_host or ""))
except Exception as e:
    sys.stderr.write("ERR %s\n" % e)
    sys.exit(9)
'
    # BOUNDED. `ak shell` cold-starts a full Django app, and this runs on every
    # `rzfz status` — the command an operator reaches for when the box is ALREADY
    # unhealthy, which is exactly when authentik-worker or its database may be
    # wedged. An unbounded `docker exec` there hangs the whole status report, so a
    # detector meant to make a broken box visible would instead make it harder to
    # inspect. Same reason the git and verify calls above are wrapped.
    #
    # 25s: generous for a cold Django start on a loaded box, short enough that a
    # hung worker costs a delay rather than the report. `timeout` exits 124.
    _ak_raw="$(timeout 25 docker exec authentik-worker ak shell -c "$_ak_snippet" 2>/dev/null)"
    _ak_rc=$?
    _ak_out="$(printf '%s\n' "$_ak_raw" | razzfazz_ak_provider_lines)"
    if [ "$_ak_rc" -eq 9 ]; then
        # The snippet's own error path: Authentik answered, the query failed.
        info "Authentik provider query failed inside authentik-worker — bindings not checked"
    elif [ "$_ak_rc" -eq 124 ]; then
        warn "forward-auth binding probe timed out after 25s — authentik-worker is not answering"
    elif [ "$_ak_rc" -ne 0 ]; then
        # Any other rc means the PROBE is broken, not that the box is fine. `info`
        # is too quiet for "this check is not working" — that is how a detector
        # sits next to the bug it exists to find and says nothing.
        warn "forward-auth binding probe failed (rc=$_ak_rc) — this check did NOT run"
    elif [ -z "$_ak_out" ] && [ -n "$(printf '%s' "$_ak_raw" | tr -d '[:space:]')" ]; then
        warn "forward-auth binding probe returned unrecognised output — this check did NOT run"
    elif [ -z "$_ak_out" ]; then
        info "no forward-auth providers defined"
    else
        _total=$(printf '%s\n' "$_ak_out" | grep -c . || true)
        _orphans="$(printf '%s\n' "$_ak_out" | razzfazz_orphaned_providers)"
        if [ -z "$_orphans" ]; then
            pass "all $_total forward-auth providers are attached to an outpost"
        else
            _n=$(printf '%s\n' "$_orphans" | grep -c . || true)
            fail "$_n of $_total forward-auth providers are attached to NO outpost — those domains return 404"
            while IFS= read -r _p; do
                [ -n "$_p" ] || continue
                _host="$(printf '%s\n' "$_ak_out" | razzfazz_provider_host_for "$_p")"
                # Authentik's own external_host when it gave us one; the derived
                # guess only as a fallback (#527 — the guess is wrong whenever a
                # provider's host differs from its display name).
                [ -n "$_host" ] || _host="$(razzfazz_provider_domain "$_p")"
                warn "  404: $_host  (provider: $_p)"
            done <<< "$_orphans"
            info "  repair: rzfz post-install --refresh   (reconcile_authentik_bindings)"
        fi
    fi
fi

# ============================================================================
# CATEGORY 11 — AUDIT POSTURE
# ============================================================================
cat_begin "AUDIT POSTURE"
# #423: report the assessment for the tag this box is ACTUALLY RUNNING, not
# whatever `ls -1t` happened to return.
#
# Two compounding bugs made this customer-facing output wrong:
#   1. `ls -1t` sorts by mtime, and all 42 assessments share the single mtime of
#      the checkout/package apply that delivered them. The tie-break is
#      arbitrary — on box 0.236 it picked an APRIL file for an August install,
#      and `rzfz security-check` embeds this output verbatim.
#   2. The age regex only matches the six legacy date-named files
#      (`...-2026-04-23.md`). The other 36 are TAG-named
#      (`...-v2026.08-ga.8.md`), so for those `audit_date` was empty and no age
#      was reported at all.
#
# Selection order: the assessment matching the installed tag (enterprise overlay
# first, since customer boxes carry the current per-tag one there), then the
# newest by CalVer-sorted FILENAME. Age never comes from mtime — see
# razzfazz_artifact_age_days in scripts/lib.sh (#380 is the same root cause).
audit_dirs=("$SCRIPT_DIR/overlay/enterprise/security-run" "$SCRIPT_DIR/security-run")

installed_tag=$(env_get RAZZFAZZ_VERSION 2>/dev/null || true)
[ -n "$installed_tag" ] || installed_tag=$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null | tr -d '\n')
installed_tag="${installed_tag#v}"

latest_audit=""
audit_matches_tag=false
audit_kind=""
audit_pick=$(razzfazz_select_assessment "$installed_tag" "${audit_dirs[@]}" 2>/dev/null || true)
if [ -n "$audit_pick" ]; then
    latest_audit="${audit_pick%%$'\t'*}"
    audit_kind="${audit_pick##*$'\t'}"
    [ "$audit_kind" = "exact" ] && audit_matches_tag=true
fi

if [ -n "$latest_audit" ]; then
    info "latest audit: $(basename "$latest_audit")"
    if [ "$audit_matches_tag" = true ]; then
        pass "audit matches the installed version ($installed_tag)"
    elif [ "$audit_kind" = "cycle" ]; then
        # task G1: a GA patch release with no assessment of its own falls back
        # WITHIN its cycle — that is closely related code, not the "showing
        # the newest on disk" case below, so it is an INFO, not a WARN.
        cyc_ver=$(basename "$latest_audit" .md | grep -oE '[0-9]{4}\.[0-9]{2}-ga(\.[0-9]+)?' | tail -1)
        info "patch release: newest assessment of this cycle (${cyc_ver:-$installed_tag})"
    elif [ -n "$installed_tag" ]; then
        warn "no audit for the installed version ($installed_tag) — showing the newest on disk; its findings describe DIFFERENT code"
    fi

    days_old=$(razzfazz_artifact_age_days "$latest_audit" 2>/dev/null || true)
    if [ -n "$days_old" ]; then
        if [ "$days_old" -gt 30 ]; then
            warn "audit is $days_old days old (>30 — recommend re-run)"
        else
            pass "audit is $days_old days old"
        fi
    else
        info "audit age unknown (no git history and no readable mtime)"
    fi

    # Count critical/high lines roughly (simple heuristic)
    # NB `grep -c` exits 1 when the count is zero, so the old
    # `$(grep -c ... || echo 0)` printed grep's own "0" AND the fallback "0",
    # producing a literal "0\n0" mid-sentence. Assign, then default on failure.
    crit=$(grep -cE '^\| [0-9]+ \| \*\*F-[A-Z0-9-]+\*\*.*\| Critical' "$latest_audit" 2>/dev/null) || crit=0
    high=$(grep -cE '^\| [0-9]+ \| \*\*F-[A-Z0-9-]+\*\*.*\| High' "$latest_audit" 2>/dev/null) || high=0
    if [ "$audit_matches_tag" = true ]; then
        info "top-N table indicates ~$crit Critical, ~$high High items in latest audit"
    else
        info "top-N table indicates ~$crit Critical, ~$high High items in $(basename "$latest_audit") (NOT this version)"
    fi
else
    info "no security audit report found in security-run/"
fi

# ============================================================================
# CATEGORY — POST-INSTALL COMPLETENESS
# ============================================================================
# Prior categories check containers/images/profiles in isolation but never
# asked "did post-install actually FINISH its job?" — the gap that let a
# customer box (migration post-mortem 2026-07-14) run fully
# unwired: every container healthy, every other check green, yet OpenWebUI /
# Dify / Cognee never got pointed at GPUStack. This category answers that
# directly, in the three shapes #179 names:
#   1. every custom-build module image is present locally — reuses
#      build_preflight.all_build_images(), the SAME source of truth the
#      Config-Portal module-enable pre-flight uses, so a FAIL here means a
#      later module-enable would hit the raw docker-socket-proxy 403;
#   2. consumer wiring — the GPUStack API key OpenWebUI/coding-agents
#      authenticate with (compose default binding) is not the init-time
#      placeholder, and Cognee's COGNEE_LLM_MODEL/COGNEE_EMBEDDING_MODEL are
#      set when the cognee profile is enabled;
#   3. Dify's GPUStack provider registration, and whether the
#      defaults.chat/embedding/reranker models (core/llm/standard-models.yaml)
#      are actually deployed+servable in GPUStack — both live inside Dify's
#      Postgres / a running GPUStack API and cannot be asserted from source,
#      so these two probes are best-effort, timeout-bounded, and degrade to
#      INFO (never FAIL the verdict) when the box isn't reachable — the same
#      idiom as the SSO FORWARD-AUTH / TLS categories above.
#
# Verdict: COMPLETE only if every DETERMINABLE sub-check passed.
cat_begin "POST-INSTALL COMPLETENESS"
_pic_incomplete=false
_pic_reasons=""

# --- 1. custom-build images -------------------------------------------------
# CUSTOM_BUILD_IMAGES-BEGIN
_bp_module="$SCRIPT_DIR/core/config/app/services/build_preflight.py"
if ! command -v docker >/dev/null 2>&1; then
    skip "custom-build images: docker unavailable"
elif [ ! -f "$_bp_module" ]; then
    skip "custom-build images: enumerator not found"
else
    _bp_list=$(cd "$SCRIPT_DIR" && timeout 60 python3 "$_bp_module" --stack-root "$SCRIPT_DIR" --all-build-images 2>/dev/null)
    if [ -z "$_bp_list" ]; then
        skip "custom-build images: could not enumerate (no compose.yml / no profiles)"
    else
        _img_total=0; _img_missing=0; _img_missing_names=""
        while IFS=$'\t' read -r _svc _img; do
            [ -z "$_img" ] && continue
            _img_total=$((_img_total + 1))
            if ! docker image inspect "$_img" >/dev/null 2>&1; then
                if razzfazz_hub_copy_trusted "$_img" >/dev/null 2>&1; then
                    # #2566: present only under the hub tag (the recorded build).
                    # Status never changes the box: the lock-holding refresh re-tags.
                    warn "custom-build image ${_img} is present only under its hub tag (the recorded build) — run: rzfz post-install --refresh (re-tags, no rebuild, no download)"
                else
                    _img_missing=$((_img_missing + 1))
                    _img_missing_names="$_img_missing_names $_svc"
                fi
            fi
        done <<< "$_bp_list"
        if [ "$_img_total" -eq 0 ]; then
            skip "custom-build images: enumerator returned no build services"
        elif [ "$_img_missing" -eq 0 ]; then
            pass "custom-build images: $_img_total/$_img_total present"
        else
            fail "custom-build images: $_img_missing of $_img_total missing (Config-Portal enable would 403):$_img_missing_names"
            _pic_incomplete=true
            _pic_reasons="$_pic_reasons custom-images-missing"
        fi
    fi
fi
# CUSTOM_BUILD_IMAGES-END

# --- 2. consumer wiring: GPUStack API key -----------------------------------
# #1902: gated on GPUStack being on this box — see the SECRETS category. On a
# manager box this used to make POST-INSTALL COMPLETENESS say INCOMPLETE for
# ever, which is the one line an operator reads after an upgrade. A line that
# is permanently red has stopped meaning anything.
if _gpustack_on_this_box; then
    case "$gpu_key" in
        ""|*CHANGEME*|*PLACEHOLDER*)
            fail "consumer wiring: GPUSTACK_API_KEY is placeholder/empty — OpenWebUI/coding-agents cannot authenticate"
            _pic_incomplete=true
            _pic_reasons="$_pic_reasons gpustack-key-unwired"
            ;;
        *)
            pass "consumer wiring: GPUSTACK_API_KEY set (OpenWebUI/coding-agents default binding)"
            ;;
    esac
else
    skip "consumer wiring: no GPUStack profile — consumers bind to the LLM Manager (llm:8080/v1)"
fi

# --- 3. consumer wiring: Cognee (only when the profile is enabled) --------
case ",$profiles," in
    *,cognee,*)
        _cognee_llm=$(env_get COGNEE_LLM_MODEL)
        _cognee_emb=$(env_get COGNEE_EMBEDDING_MODEL)
        if [ -n "$_cognee_llm" ] && [ -n "$_cognee_emb" ]; then
            pass "consumer wiring: Cognee COGNEE_LLM_MODEL/COGNEE_EMBEDDING_MODEL set"
        else
            fail "consumer wiring: cognee profile enabled but COGNEE_LLM_MODEL/COGNEE_EMBEDDING_MODEL not set — run 'rzfz post-install --refresh'"
            _pic_incomplete=true
            _pic_reasons="$_pic_reasons cognee-unwired"
        fi
        ;;
    *) skip "consumer wiring: cognee profile not enabled" ;;
esac

# --- 4. Dify GPUStack provider (live-box probe; best-effort, never FAILs the
# category on unreachability — see the category header note) ---------------
if ! command -v docker >/dev/null 2>&1; then
    skip "Dify GPUStack provider: docker unavailable"
elif ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx dify-api; then
    case ",$profiles," in
        *,dify,*) info "Dify GPUStack provider: dify-api not running — not checked (live-box only)" ;;
        *)        skip "Dify profile not enabled" ;;
    esac
else
    _dify_check='
import sys
try:
    from models.provider import Provider
    from app_factory import create_app
    res = create_app()
    app = res[1] if isinstance(res, tuple) else res
    with app.app_context():
        n = Provider.query.filter(Provider.provider_name.ilike("%gpustack%")).count()
    print("RZFZ-DIFY-PROVIDERS\t%d" % n)
except Exception as e:
    sys.stderr.write("ERR %s\n" % e)
    sys.exit(9)
'
    _dify_out=$(timeout 20 docker exec dify-api python -c "$_dify_check" 2>/dev/null)
    _dify_rc=$?
    _dify_n=$(printf '%s\n' "$_dify_out" | grep '^RZFZ-DIFY-PROVIDERS' | cut -f2)
    if [ "$_dify_rc" -eq 0 ] && [ -n "$_dify_n" ] && [ "$_dify_n" -gt 0 ]; then
        pass "Dify GPUStack provider registered ($_dify_n)"
    elif [ "$_dify_rc" -eq 0 ]; then
        fail "Dify running but no GPUStack provider registered — run 'rzfz post-install --refresh'"
        _pic_incomplete=true
        _pic_reasons="$_pic_reasons dify-unwired"
    else
        info "Dify GPUStack provider: probe inconclusive (rc=$_dify_rc) — not checked"
    fi
fi

# --- 5. default models (defaults.chat/embedding/reranker) deployed+servable
# in GPUStack (live-box probe; best-effort, same non-FAIL-on-unreachable rule)
_std_models="$SCRIPT_DIR/core/llm/standard-models.yaml"
# The port comes from .env, not from the shell. This script deliberately never
# sources .env and reads every other value via env_get; `${GPUSTACK_PORT:-9090}`
# expanded a SHELL variable that is always unset here, so a box that changed
# GPUSTACK_PORT (config/.env.example makes it configurable) reported "GPUStack
# API not reachable — not checked" forever and never said why.
_gs_port="$(env_get GPUSTACK_PORT)"; _gs_port="${_gs_port:-9090}"
if ! command -v docker >/dev/null 2>&1; then
    skip "default models deployed: docker unavailable"
elif ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gpustack; then
    info "default models deployed: gpustack not running — not checked (live-box only)"
elif [ -z "$gpu_key" ] || [ ! -f "$_std_models" ]; then
    info "default models deployed: prerequisites missing (API key / standard-models.yaml) — not checked"
else
    _defaults=$(python3 -c "
import yaml
try:
    d = yaml.safe_load(open('$_std_models')).get('defaults', {})
    for k in ('chat', 'embedding', 'reranker'):
        v = d.get(k)
        if v:
            print(v)
except Exception:
    pass
" 2>/dev/null)
    if [ -z "$_defaults" ]; then
        info "default models deployed: could not read defaults from standard-models.yaml — not checked"
    else
        _gs_models=$(timeout 15 curl -s --connect-timeout 3 \
            -K <(printf 'header = "Authorization: Bearer %s"\n' "$gpu_key") \
            "http://127.0.0.1:${_gs_port}/v1/models" 2>/dev/null)
        if [ -z "$_gs_models" ]; then
            info "default models deployed: GPUStack API not reachable — not checked"
        else
            _missing_models=""
            while IFS= read -r _m; do
                [ -z "$_m" ] && continue
                echo "$_gs_models" | grep -q "\"$_m\"" || _missing_models="$_missing_models $_m"
            done <<< "$_defaults"
            if [ -z "$_missing_models" ]; then
                pass "default models (chat/embedding/reranker) present in GPUStack"
            else
                fail "default models missing from GPUStack:$_missing_models — run 'rzfz post-install --preset standard'"
                _pic_incomplete=true
                _pic_reasons="$_pic_reasons models-missing"
            fi
        fi
    fi
fi

# --- overall verdict ---------------------------------------------------------
if [ "$_pic_incomplete" = true ]; then
    fail "POST-INSTALL COMPLETENESS: INCOMPLETE —$_pic_reasons"
else
    pass "POST-INSTALL COMPLETENESS: COMPLETE — wiring applied, images built, provisioning done"
fi

# ============================================================================
# RENDER
# ============================================================================
render_report
