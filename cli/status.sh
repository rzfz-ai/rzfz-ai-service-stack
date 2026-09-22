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
    grep "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^"//;s/"$//' | tr -d '\r'
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
        fail "registry: fell back to the compose name — this node can pull NO runner. Set LLM_HUB_DOMAIN or LLM_WORKER_RUNNER_REGISTRY in .env.node, docker login, restart the agent (#1860)"
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
        # The engines the master placed here. Named by the master, so they are
        # counted, not enumerated against a list this box does not own.
        _engines="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c '^engine-' || true)"
        if [ "${_engines:-0}" -gt 0 ]; then
            info "engines placed by the master: ${_engines}"
        else
            info "engines placed by the master: none right now"
        fi
    fi

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

# ============================================================================
# CATEGORY 1 — CODE STATE
# ============================================================================
cat_begin "CODE STATE"
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

    # #602 — release channel + upgrade-source Ampel. Answers "will the next
    # `rzfz upgrade` work?" here instead of at the upgrade stumble: channel,
    # origin fit, credentials fit, and one cheap reachability probe.
    channel=$(env_get RAZZFAZZ_CHANNEL); channel="${channel:-internal}"
    # #1738: RAW, so this line and the pre-flight that acts on it agree, and
    # so an operator reads back what stands in .git/config. The reachability
    # probe below asks git for the remote by NAME and therefore still contacts
    # the rewritten URL, which is the right one to contact.
    origin_url=$(razzfazz_origin_url "$SCRIPT_DIR")
    # REDACT userinfo before display. A remote configured as
    # https://<user>:<PAT>@git.razzfazz.ai/… — a common alternative to the
    # credential-store flow checked just above — would otherwise leak the token
    # into stdout, into --json, into any --no-color log capture, and into
    # `rzfz security-check`, which embeds this output verbatim. status.sh is
    # the only reporter that PRINTS the origin URL; init.sh/upgrade.sh read it
    # but never print it. The unredacted value stays in $origin_url for the
    # ls-remote probe below.
    origin_disp="$(printf '%s' "$origin_url" | sed -E 's#://[^/@]*@#://<redacted>@#')"
    has_pat=false
    # #635 review: also check the invoking user's home under sudo (HOME=/root)
    for _credf in "$HOME/.git-credentials"                   "${SUDO_USER:+/home/$SUDO_USER/.git-credentials}"; do
        [ -n "$_credf" ] && [ -f "$_credf" ] && grep -q "git.razzfazz.ai" "$_credf" 2>/dev/null && has_pat=true
    done
    case "$channel" in
        public)
            if [ "$has_pat" = "true" ]; then
                fail "channel=public but a git.razzfazz.ai PAT is present — fleet-shaped box on the lagging mirror (fix: RAZZFAZZ_CHANNEL=internal; see #602)"
            else
                info "channel=public (upgrades: GitHub mirror, anonymous)"
            fi
            ;;
        internal)
            if [ "$has_pat" = "true" ]; then
                info "channel=internal (upgrades: git.razzfazz.ai, PAT present)"
            else
                warn "channel=internal but NO git.razzfazz.ai PAT — the next 'rzfz upgrade' will abort in the pre-flight (onboard: credential.helper store + PAT in ~/.git-credentials)"
            fi
            ;;
    esac
    if [ -n "$origin_url" ]; then
        if GIT_TERMINAL_PROMPT=0 timeout 8 git -C "$SCRIPT_DIR" ls-remote --exit-code origin HEAD >/dev/null 2>&1; then
            pass "upgrade source reachable+authenticated ($origin_disp)"
        else
            warn "upgrade source NOT reachable/authenticated ($origin_disp) — offline box, wrong channel, or missing/expired credentials"
        fi
    fi

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
        fail "upgrade to ${upgrade_in_progress} did NOT finish — this box runs that code, but RAZZFAZZ_VERSION still says ${recorded_version:-unknown}. Re-run \`rzfz upgrade\` (the steps are idempotent) rather than setting the stamp by hand (#1593)."
    fi
    if [ "$upgrade_method" = "offline-package" ]; then
        # #272: offline-package boxes carry a fresh VERSION/RAZZFAZZ_VERSION, but
        # the offline rsync intentionally does NOT move .git HEAD (and origin may
        # be unreachable anyway). RAZZFAZZ_COMMIT records the package's baked
        # commit, not this box's git HEAD, so a git-vs-origin tag-move comparison
        # is meaningless and produced a false "tag MOVED". Trust the version.
        pass "code version (offline-package): ${recorded_version:-unknown} — git tag-drift check skipped (#272)"
        recorded_version=""   # make the case below a no-op
    fi
    case "$recorded_version" in
        v*-ga*|*-ga.*)
            # RAZZFAZZ_VERSION is stored without the leading 'v' (from the
            # VERSION file), but git tags carry it (v2026.05-ga.4). Query both
            # forms so the lookup hits regardless of which convention is recorded.
            tag="$recorded_version"
            vtag="v${recorded_version#v}"
            # ls-remote is lightweight (no fetch); short timeout so an offline
            # or slow box just skips the check instead of hanging the status run.
            # GIT_TERMINAL_PROMPT=0 + GIT_ASKPASS=true: NEVER prompt for git
            # credentials. origin is the auth-gated Gitea; on a customer box with
            # no cached creds, plain `git ls-remote` PROMPTS for a username on the
            # TTY and hangs (timeout can't interrupt the terminal read). With
            # prompting disabled git fails fast → the empty-result skip below
            # fires cleanly. Where creds ARE cached (vendor boxes) the check still
            # works via the non-interactive credential helper.
            remote_line=$(GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true \
                timeout 8 git ls-remote --tags origin \
                "$tag" "${tag}^{}" "$vtag" "${vtag}^{}" 2>/dev/null)
            if [ -z "$remote_line" ]; then
                skip "tag-drift check: origin unreachable or '$tag' not on origin"
            else
                # Prefer the dereferenced (^{}) line — that's the commit for an
                # annotated tag; lightweight tags only have the plain line.
                remote_commit=$(printf '%s\n' "$remote_line" | awk '/\^\{\}$/{print $1}' | head -1)
                [ -z "$remote_commit" ] && remote_commit=$(printf '%s\n' "$remote_line" | awk 'NR==1{print $1}')
                remote_short=$(printf '%s' "$remote_commit" | cut -c1-7)
                if [ -z "$recorded_commit" ] || [ "$recorded_commit" = "unknown" ]; then
                    skip "tag-drift check: no RAZZFAZZ_COMMIT recorded to compare against"
                elif [ "${remote_commit#$recorded_commit}" != "$remote_commit" ] \
                     || [ "$remote_short" = "$recorded_commit" ]; then
                    pass "tag '$tag' on origin matches deployed commit ($recorded_commit)"
                else
                    fail "tag '$tag' has MOVED on origin: deployed=$recorded_commit, origin now=$remote_short"
                    warn "  same tag name = different code. GA tags must not be re-pointed;"
                    warn "  reconcile by upgrading to the next ga.N rather than re-pulling this tag."
                fi
            fi
            ;;
    esac
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
    warn "    The alias is in modules/llm/manager/compose.yml (#1445) — this container"
    warn "    predates it and was never recreated. Nothing is broken WHILE the consumers"
    warn "    still name 'llm-manager', but an upgrade that repoints them at the canonical"
    warn "    address leaves every one of them dialling a name no DNS knows (#979)."
    warn "    Fix: 'docker compose up -d llm-manager' (recreates it with the alias)."
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
    # manager (#1713): status, health, ready_instances, replicas, instances,
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
        print("%s\t%s" % (w["id"], w.get("name") or w["id"]))
' 2>/dev/null || true)"
    [ -n "$workers" ] || { pass "LLM Manager: no workers to check for runner switches"; return 0; }

    rows=""
    while IFS=$'\t' read -r wid wname; do
        [ -n "$wid" ] || continue
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
    warn "    A failed switch stays drained on purpose (#1677): traffic must not"
    warn "    return to a node whose runner state is unknown. The decision is"
    warn "    yours — roll back, or fix the image coordinate and switch again."
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
    warn "    with 'docker volume inspect <name>' before deciding (#1667)"
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
    warn "    needs the volume to exist (#1709)."
}

# _wazuh_fim_verdict <last_scan_epoch> <frequency_seconds> <now_epoch> [agent_id]
# Prints "<pass|warn>\t<text>". Split out from the row below so the decision can
# be measured without a box — the row itself needs docker and a running manager.
#
# #1936: the row NAMES the agent it read. It reads `syscheck/000/last_scan`,
# and 000 — the manager's own local agent — is the one agent whose on-demand
# rescan does not work: measured on 0.91 (2026-09-08) that both documented
# handles (`agent_control -r -a`, `PUT /syscheck?agents_list=000`) report
# success and leave the timestamp untouched for 240 s, while DevBox-Vuko
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
        stale_hint=" — an on-demand rescan of agent 000 reports success without running (#1936), so only the schedule or a manager restart moves this"
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
    # would put the password in the host's process table. The user is
    # `wazuh-wui`; `wazuh` and `admin` are rejected (measured).
    last="$(docker exec wazuh-manager sh -c '
        t=$(curl -sk -u "wazuh-wui:$WAZUH_API_PASSWORD" -X POST \
              "https://localhost:55000/security/user/authenticate?raw=true" 2>/dev/null)
        [ -n "$t" ] || exit 1
        curl -sk -H "Authorization: Bearer $t" \
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
    if [ "$unhealthy" -eq 0 ]; then
        pass "no unhealthy/restarting containers"
    else
        fail "$unhealthy unhealthy/restarting containers"
        while read -r l; do
            warn "$l"
        done < <(docker ps --format '  {{.Names}}: {{.Status}}' 2>/dev/null | grep -E "unhealthy|Restarting" | head -3)
    fi
    # Orphan ad-hoc containers (no compose project label) — F-RC5-4 territory.
    # Skip gpustack-spawned model pods: names ending in -pause or -run-<N>
    # are containers that gpustack creates dynamically as inference workers
    # (one pause + one or more run-N per loaded model). These legitimately
    # lack a compose project label — they're managed by gpustack, not compose.
    orphan_count=0
    orphan_names=""
    for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
        if [ -z "$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null)" ]; then
            case "$c" in
                *-pause|*-run-[0-9]*) continue ;;  # gpustack model pod
            esac
            orphan_count=$((orphan_count + 1))
            orphan_names="$orphan_names $c"
        fi
    done
    if [ "$orphan_count" -eq 0 ]; then
        pass "no ad-hoc orphan containers (gpustack model pods excluded)"
    else
        warn "$orphan_count ad-hoc orphan container(s):${orphan_names}"
    fi
    status_orphan_volume_rows
    status_backup_only_volume_rows
    status_canonical_alias_row
    status_package_key_row
    status_llm_deployment_rows
    status_failed_runner_upgrade_rows
    status_wazuh_fim_scan_row
    status_init_oneshot_rows
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
    drift=$(MANIFEST="$MANIFEST_FILE" ACTIVE_PROFILES="$active_profiles" python3 - 2>/dev/null <<'PY'
import json, os, subprocess
manifest = os.environ["MANIFEST"]
active = set(p.strip() for p in os.environ.get("ACTIVE_PROFILES","").split(",") if p.strip())
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
for l in drift_lines:
    print(l)
PY
)
    align=$(echo "$drift" | grep '^COUNT|' | cut -d'|' -f2)
    drift_n=$(echo "$drift" | grep -c '^DRIFT|' || true)
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
        pass "COMPOSE_PROFILES on 'llm-legacy' (stable GPUStack 0.7.1 runtime — the one GPUStack profile since #1447)"
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
                    fail "COMPOSE_FILE='$compose_file' is missing modules/llm/compose.devices.${hardware}.yml — the merged llm-legacy service gets its image and GPU wiring from it (#1448). Run 'rzfz upgrade' to repair it"
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
                    fail "COMPOSE_FILE='$compose_file' is missing modules/llm/compose.devices.cpu.yml — llm-legacy would start the AMD GPU image on this CPU box (#1447). Run 'rzfz upgrade' to repair it"
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
        pass "COMPOSE_PROFILES on 'llm-manager' (the canonical LLM front end since #1443 — serves http://llm:8080/v1)"
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

# ============================================================================
# CATEGORY 5 — NETWORK EXPOSURE
# ============================================================================
cat_begin "NETWORK EXPOSURE"
if ! command -v docker >/dev/null 2>&1; then
    skip "docker unavailable"
else
    # Acceptable 0.0.0.0 binds: caddy 80/443, gitea SSH 2222
    bad_binds=""
    while read line; do
        [ -z "$line" ] && continue
        name=$(echo "$line" | awk '{print $1}')
        ports=$(echo "$line" | cut -d$'\t' -f2-)
        # Look for 0.0.0.0:<port>-> patterns
        for bind in $(echo "$ports" | grep -oE "0\.0\.0\.0:[0-9-]+->[0-9-]+/(tcp|udp)" || true); do
            host_port=$(echo "$bind" | sed -E 's|0\.0\.0\.0:([0-9-]+)->.*|\1|')
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
        if echo "$gpustack_ports" | grep -q "0\.0\.0\.0"; then
            fail "gpustack ports bound on 0.0.0.0 — set GPUSTACK_HOST_BIND=127.0.0.1"
        else
            pass "gpustack on 127.0.0.1 only"
        fi
    fi
fi

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
offline_flag=$(env_get RAZZFAZZ_OFFLINE)
info "RAZZFAZZ_OFFLINE=${offline_flag:-0}"
mirror=$(env_get RAZZFAZZ_REGISTRY_MIRROR)
if [ -n "$mirror" ]; then
    info "registry mirror: $mirror (install/upgrade pulls redirected)"
else
    info "registry mirror: none (upstream registries)"
fi
# Overlays composed (COMPOSE_FILE chain)
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
            if [ "${v_miss:-1}" -eq 0 ]; then
                pass "$tool: $v_pres/$v_exp present, 0 missing (offline-ready)"
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
        pass "Docker daemon has no-new-privileges (F-059)"
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
cat_begin "SSH POSTURE"
if [ -r /etc/ssh/sshd_config ]; then
    pa=$(grep -E '^[[:space:]]*PasswordAuthentication[[:space:]]+' /etc/ssh/sshd_config 2>/dev/null | tail -1 | awk '{print $2}')
    pr=$(grep -E '^[[:space:]]*PermitRootLogin[[:space:]]+' /etc/ssh/sshd_config 2>/dev/null | tail -1 | awk '{print $2}')
    case "${pa:-default-yes}" in
        no) info "PasswordAuthentication=no (key-only)" ;;
        *)  info "PasswordAuthentication=${pa:-default-yes} (operator choice for dev/test boxes)" ;;
    esac
    case "${pr:-default-prohibit-password}" in
        no) info "PermitRootLogin=no" ;;
        *)  info "PermitRootLogin=${pr:-default-prohibit-password}" ;;
    esac
else
    skip "sshd_config not readable (need sudo)"
fi
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

# ============================================================================
# CATEGORY 10 — BACKUPS
# ============================================================================
cat_begin "BACKUPS"
backup_dir="$SCRIPT_DIR/backups"
if [ -d "$backup_dir" ]; then
    # Look only for actual backup tarballs (skip env-snapshots/ subdir etc)
    last=$(ls -1t "$backup_dir"/*.tar.gz* 2>/dev/null | head -1)
    if [ -n "$last" ]; then
        fname=$(basename "$last")
        last_ts=$(stat -c '%y' "$last" 2>/dev/null | cut -d. -f1)
        case "$fname" in
            *.gpg) pass "last backup: $fname (GPG-encrypted, $last_ts)" ;;
            *)     fail "last backup: $fname NOT GPG-encrypted ($last_ts) — F-A2 risk" ;;
        esac
    else
        warn "no .tar.gz backup files in $backup_dir — first backup hasn't run yet"
    fi
else
    info "no backups/ directory (first install)"
fi

# ============================================================================
# CATEGORY 10b — SSO FORWARD-AUTH BINDINGS (#459)
# ============================================================================
# A ProxyProvider attached to NO outpost makes Authentik answer Caddy's
# forward_auth with 404, which Caddy passes straight through. The symptom is a
# whole domain returning 404 while /healthz is 200, the container is healthy and
# every other check here is green — box 0.78 sat like that for four days, and
# culturehack-001 hit it on 2026-05-21 with 14 providers unbound.
#
# The repair has existed since then (apply-policy-bindings.py, idempotent) and is
# correct. What was missing is anyone NOTICING: the repair runs on the init
# fast-path, and `docker compose up -d` is not init, so a box that never re-runs
# init stays 404 indefinitely.
#
# Read-only: a Django query inside authentik-worker, no writes, no lock needed.
cat_begin "SSO FORWARD-AUTH"
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^authentik-worker$'; then
    info "authentik-worker not running — forward-auth bindings not checked"
else
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
audit_pick=$(razzfazz_select_assessment "$installed_tag" "${audit_dirs[@]}" 2>/dev/null || true)
if [ -n "$audit_pick" ]; then
    latest_audit="${audit_pick%%$'\t'*}"
    [ "${audit_pick##*$'\t'}" = "exact" ] && audit_matches_tag=true
fi

if [ -n "$latest_audit" ]; then
    info "latest audit: $(basename "$latest_audit")"
    if [ "$audit_matches_tag" = true ]; then
        pass "audit matches the installed version ($installed_tag)"
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
# CATEGORY — POST-INSTALL COMPLETENESS (#179)
# ============================================================================
# Prior categories check containers/images/profiles in isolation but never
# asked "did post-install actually FINISH its job?" — the gap that let a
# customer box (Care Solutions, migration post-mortem 2026-07-14) run fully
# unwired: every container healthy, every other check green, yet OpenWebUI /
# Dify / Cognee never got pointed at GPUStack. This category answers that
# directly, in the three shapes #179 names:
#   1. every custom-build module image is present locally — reuses
#      build_preflight.all_build_images(), the SAME source of truth the
#      Config-Portal module-enable pre-flight uses, so a FAIL here means a
#      later module-enable would hit the raw docker-socket-proxy 403 (#174);
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
                _img_missing=$((_img_missing + 1))
                _img_missing_names="$_img_missing_names $_svc"
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
    app = create_app()
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
            -H "Authorization: Bearer $gpu_key" \
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
