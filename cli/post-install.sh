#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz.ai Stack - Post-Install Provisioning Script
# ==============================================================================
# This script provisions a freshly installed rzfz.ai stack with models,
# service configurations, and integrations. Run after razzfazz-init.sh.
#
# Usage:
#   rzfz post-install --preset standard     # Standard model set
#   rzfz post-install --preset developer     # Developer model set
#   rzfz post-install --verify               # Run verification only
#   rzfz post-install --preset standard --verify  # Provision + verify
#   rzfz post-install --help
#
# Prerequisites:
#   - razzfazz-init.sh has completed successfully
#   - Stack is running (docker compose up -d)
#   - Internet access for model downloads
#
# ==============================================================================

set -eo pipefail

# ------------------------------------------------------------------------------
# #427: unconditional run log. Every post-install run tees its full output to a
# timestamped file — "did the image load fail, or was it skipped?" must never
# again require re-running a 30-minute provisioning step because the console
# scrollback is gone. Skipped when a parent razzfazz process already captures
# (upgrade re-exec), so one run = one log.
# --help/-h/help runs stay un-teed: the command-reference generator captures
# their stdout verbatim (a tee process-substitution can swallow it on fast
# exit), and a help call must not mint a junk run log.
_rzfz_wants_help=false
for _rzfz_a in "$@"; do case "$_rzfz_a" in -h|--help|help) _rzfz_wants_help=true ;; esac; done
if [ "$_rzfz_wants_help" = false ] && [ -z "${RZFZ_RUN_LOG:-}" ]; then
    RZFZ_LOG_DIR="${RZFZ_LOG_DIR:-$HOME/.razzfazz/logs}"
    # review #653: logs can carry env-adjacent output (set -x under
    # DEBUG_MODE would land here in full) — operator-only.
    mkdir -p "$RZFZ_LOG_DIR" 2>/dev/null && chmod 700 "$RZFZ_LOG_DIR" 2>/dev/null || RZFZ_LOG_DIR="/tmp"
    export RZFZ_RUN_LOG="$RZFZ_LOG_DIR/razzfazz-post-install-$(date -u +%Y%m%dT%H%M%SZ).log"
    # retention: keep the last 10 runs per verb
    # #667: on the verb's first-ever run the glob matches nothing → ls exits 2
    # → pipefail + set -e killed the script BEFORE any output. Cleanup must
    # never kill the run.
    ls -1t "$RZFZ_LOG_DIR"/razzfazz-post-install-*.log 2>/dev/null | tail -n +11 | xargs -r rm -f -- || true
    exec > >(tee -a "$RZFZ_RUN_LOG") 2>&1
    echo "[log] full run log: $RZFZ_RUN_LOG"
    trap 'echo "[log] full run log: $RZFZ_RUN_LOG"' EXIT
fi

# Debug mode: set -x for verbose trace output
DEBUG_MODE=false

# ==============================================================================
# M026 / S02 #6: source the shared library for colors, print_*, and
# read_env_value. Note that this script defines its own load_env() and
# update_env_value() further down — those local definitions intentionally
# override lib's because their semantics differ here (load_env eval-exports
# .env into the current shell; update_env_value triggers an env-snapshot
# side-effect on first call). print_step is also kept locally because it
# carries a horizontal-rule visual decorator that the long interactive
# provisioning UX depends on.
# ==============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# House rule, matching init.sh:86 / upgrade.sh:54 / lifecycle.sh:26 /
# setup.sh:104 / worker-join.sh:40 / hub-credentials.sh:20 — `rzfz` execs the
# target WITHOUT changing directory. Without this cd, every `docker compose`
# call resolves a RELATIVE COMPOSE_FILE chain against the operator's cwd (and
# most of them are `>/dev/null 2>&1 || true`, so the failure is invisible),
# ensure_oidc_ca_superset writes ./certs/caddy-ca.pem into the operator's cwd
# and reads no ./.env, and `read_env_value .env …` silently returns nothing.
cd "$SCRIPT_DIR"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"
# #1185/#908: the OWUI backend resolvers, key validation and retrieval
# reconcile are shared with cli/upgrade.sh — ONE implementation.
# shellcheck source=scripts/lib-owui.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib-owui.sh"
# #1250b: deploying + verifying the standard model set on a box whose LLM
# backend is the LLM Manager (no GPUStack to deploy into). Its own sourced
# library, same shape as lib-owui.sh.
# shellcheck source=cli/lib-llm-manager-deploy.sh disable=SC1091
source "${SCRIPT_DIR}/cli/lib-llm-manager-deploy.sh"

# Local print_step preserves the original horizontal-rule decorator above the
# [STEP] line — lib.sh's print_step has no rule.
print_step() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}[STEP]${NC} $1"
}

# ==============================================================================
# #178(c): real per-step completion summary
# ==============================================================================
# The end-of-run block used to print a STATIC six-line checklist (six literal
# `echo "... ✓ ..."` lines, always a pass, regardless of what actually
# happened in THIS run). A box where Dify provisioning genuinely failed still
# ended with a green "✓ Dify — plugins installed" — the checklist could not
# tell "verified good" from "never ran" from "failed ten minutes ago".
#
# STEP_STATUS threads the REAL outcome of each major provisioning stage
# through to the final summary. record_step_status() is a pure reporter — it
# never fails and never aborts the run (it is a plain array assignment), so
# it is safe to call from anywhere, including deep inside `set -eo pipefail`
# code. A stage that never calls record_step_status renders as SKIP, not a
# silent OK — that is deliberate: "never reached" is a distinct, honest state.
# ------------------------------------------------------------------------------
declare -A STEP_STATUS=()

# record_step_status <key> <OK|WARN|FAIL>
record_step_status() {
    STEP_STATUS["$1"]="$2"
}

# step_status_line <key> <label>: render one "Services configured" row using
# the REAL recorded outcome for <key>, defaulting to SKIP when nothing was
# ever recorded for it (profile disabled / stage never reached this run).
step_status_line() {
    local key="$1" label="$2"
    local status="${STEP_STATUS[$key]:-SKIP}"
    local color="$NC"
    case "$status" in
        OK)   color="$GREEN" ;;
        WARN) color="$YELLOW" ;;
        FAIL) color="$RED" ;;
        *)    status="SKIP" ;;
    esac
    printf '    %b[%-4s]%b %s\n' "$color" "$status" "$NC" "$label"
}

# print_services_summary: the real, per-run replacement for the old static
# checklist. Same six rows, same labels — the only thing that changed is that
# the status word now comes from what THIS run actually recorded.
print_services_summary() {
    echo "  Services configured:"
    # #1250b: on an LLM-Manager box the models are deployed through the
    # manager, not GPUStack — the row must name the backend this run used.
    # Unset (every GPUStack box) keeps the original wording verbatim.
    # NB: no apostrophe in the ${VAR:-default} below. A single quote inside the
    # DEFAULT of a parameter expansion opens a quote and breaks the parse — the
    # first draft of this used "the box's workers" and `bash -n` failed 380 lines
    # earlier, in unrelated code. The plain assignment of the same string
    # elsewhere is fine; only the :- default is not.
    # #2051: the backend row renders the key that was actually RECORDED. Before
    # the split both backends shared the `gpustack` key, so one row could serve
    # both; now the manager has its own and the row has to pick.
    if [ -n "${STEP_STATUS[llm-manager]:-}" ]; then
        step_status_line llm-manager "${LLM_BACKEND_SUMMARY_LABEL:-LLM Manager — models deployed on its workers}"
    else
        step_status_line gpustack  "${LLM_BACKEND_SUMMARY_LABEL:-GPUStack   — API key + models deployed}"
    fi
    step_status_line openwebui "Open WebUI — endpoint, audio, embedding, search"
    step_status_line dify      "Dify       — plugins installed"
    step_status_line speaches  "Speaches   — STT + TTS models downloaded"
    step_status_line lightrag  "LightRAG   — model names in .env"
    step_status_line cognee    "Cognee     — model names in .env"
}

# ==============================================================================
# Prerequisites
# ==============================================================================
check_prerequisites() {
    local missing=""
    for cmd in curl python3 docker; do
        if ! command -v "$cmd" > /dev/null 2>&1; then
            missing="$missing $cmd"
        fi
    done
    if [ -n "$missing" ]; then
        print_error "Missing required tools:$missing"
        print_info "Install with: sudo apt install -y$missing"
        exit 1
    fi
}

# ==============================================================================
# .env path (SCRIPT_DIR is set above where lib.sh is sourced)
# ==============================================================================
ENV_FILE="${SCRIPT_DIR}/.env"

# #949: expand `${IDENTIFIER}` references in an .env value against the
# variables exported so far — the same sequential interpolation Compose
# applies to .env itself, so the exported environment and Compose agree.
# Pure string-walk, NO eval: `$(…)`, backticks, and brace-less `$VAR` pass
# through byte-for-byte, and a `${…}` whose name is not a plain identifier
# (`${VAR:-def}`) or not yet set stays literal rather than being emptied.
_expand_env_refs() {
    local val="$1" out="" rest name
    while :; do
        case "$val" in *'${'*) ;; *) break ;; esac
        out="$out${val%%'${'*}"
        rest="${val#*'${'}"
        case "$rest" in
            *'}'*) ;;
            *) val='${'"$rest"; break ;;   # unterminated `${` — keep literal
        esac
        name="${rest%%\}*}"
        val="${rest#*\}}"
        case "$name" in
            ''|*[!A-Za-z0-9_]*) out="$out"'${'"$name"'}' ;;
            *) if [ -n "${!name+x}" ]; then out="$out${!name}"; else out="$out"'${'"$name"'}'; fi ;;
        esac
    done
    printf '%s' "$out$val"
}

load_env() {
    if [ ! -f "$ENV_FILE" ]; then
        print_error ".env not found at $ENV_FILE"
        print_info "Run razzfazz-init.sh first."
        exit 1
    fi
    # Source .env safely — only export simple KEY=VALUE lines.
    # 2026-05-09 fix: previous version did `eval "$(... sed 's/^/export /')"`
    # which broke on values containing shell metachars. Caught on the dev
    # box where BACKUP_CRON_EXPRESSION="00 03 * * *" caused the `*` to
    # glob-expand against the current directory and produce a flood of
    # "export: ‹filename›: not a valid identifier" errors. Now: parse
    # line-by-line and assign with proper quoting + globbing disabled, so
    # operator-edited .env values with spaces/`*`/`?` survive.
    set +e
    set -f  # disable pathname expansion for the duration of this load
    while IFS='=' read -r key value; do
        # Skip comments and malformed lines.
        case "$key" in ''|\#*) continue ;; esac
        # Only accept identifier-shaped keys.
        case "$key" in
            [A-Za-z_]*[!A-Za-z0-9_]*) continue ;;
            [A-Za-z_]*) ;;
            *) continue ;;
        esac
        # Strip surrounding double-quotes if the value is wrapped (operator
        # convention, not required by .env). Single-quoted values are kept
        # literal.
        case "$value" in
            \"*\") value="${value#\"}"; value="${value%\"}" ;;
        esac
        # #949: expand ${VAR} refs before exporting. Without this the RAW
        # template is exported (`AUTHENTIK_DOMAIN=auth.${MAIN_DOMAIN}`); a
        # later `docker compose up --force-recreate` prefers the shell env
        # over .env and does NOT recurse, so the unexpanded literal is baked
        # into containers — the OWUI OIDC login-500 clean-install regression
        # (also hit dify/cognee/model-sync/postgres URLs). Sequential
        # expansion matches Compose's own .env interpolation order.
        # Single-quoted values stay literal (same rule as the quote-strip
        # above keeping them verbatim).
        case "$value" in
            \'*\') ;;
            *'${'*) value="$(_expand_env_refs "$value")" ;;
        esac
        export "$key=$value"
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | grep -v '[<>]')
    set +f
    set -e
}

update_env_value() {
    local file="$1" key="$2" value="$3"
    # Snapshot before first .env change
    if [ -z "$_ENV_SNAPSHOT_DONE" ]; then
        if [ -f "${SCRIPT_DIR}/scripts/env-snapshot.sh" ]; then
            source "${SCRIPT_DIR}/scripts/env-snapshot.sh"
            env_snapshot "pre-post-install"
            _ENV_SNAPSHOT_DONE=1
        fi
    fi
    # #1618: post-install carries its OWN update_env_value (the snapshot
    # hook). It must ask the same question scripts/lib.sh asks, or the two
    # write different files from the same input.
    if grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" 2>/dev/null; then
        # INODE-PRESERVING (sed -i creates a new inode → breaks razzfazz-config's
        # single-file .env bind mount → portal module-toggle fails; see
        # project_config_ui_env_write_broken). cat > truncates the same inode.
        local _t="${file}.tmp.$$"
        if sed -E "s|^([[:space:]]*(export[[:space:]]+)?)${key}=.*|\\1${key}=${value}|" "$file" > "$_t"; then
            cat "$_t" > "$file"
        fi
        rm -f "$_t"
    else
        echo "${key}=${value}" >> "$file"
    fi
    # #1486: load_env() exported EVERY .env line into this process — template
    # defaults included (`COGNEE_LLM_ENDPOINT=`, `COGNEE_LLM_MODEL=openai/gemma4`
    # on a fresh install). `docker compose` resolves `${KEY:-default}` from the
    # environment BEFORE the project .env, so a key written here and rendered by
    # a `compose up` later in the same run used the STALE exported copy: cognee
    # came up on gpustack:9090 / gemma4 / nomic-embed on every clean manager box
    # while .env and `compose config` both said llm-manager / qwen3.6, every
    # embedding 422'd and --verify stayed green (third instance of the #949 /
    # #1250 class; #1254 patched one key by hand). Mirror the file into the
    # exported copy whenever this process already holds the key. Keys never
    # loaded stay unexported — compose reads those from the file, as before.
    # rev-B befund 2: only the file this process LOADED may drive its exported
    # copy. Four call sites write `.env.dify`, and six keys live in both
    # templates (COMPOSE_PROFILES among them) — mirroring a .env.dify write into
    # the environment would point every later `docker compose` in this run at a
    # different profile set.
    [ "$file" = "$ENV_FILE" ] || return 0
    if [ -n "${!key+x}" ]; then
        # rev-B befund 1: expand `${VAR}` refs exactly as load_env does (#949).
        # Exporting the raw template bakes the literal into the next container:
        # `.env.example` ships values of that shape (LLM_DOMAIN=llm.${MAIN_DOMAIN}),
        # so one future caller writing a template value back would reopen #949.
        local _exported="$value"
        case "$_exported" in
            \'*\') ;;
            *'${'*) _exported="$(_expand_env_refs "$_exported")" ;;
        esac
        export "$key=$_exported"
    fi
}

# ==============================================================================
# HTTP Helpers
# ==============================================================================

# GPUStack runtime detection. Caches in _GPUSTACK_RUNTIME so we only probe once
# per script run. Returns "0.7" (the only runtime the product ships since
# #1447 / cutover C7a) or "2.x".
#
# #1447: this used to CHOOSE between two code paths. GPUStack 2.x and its `llm`
# profile are removed, so it now answers a different question — is this box
# still running a container from before the migration? A `--refresh` on a box
# whose upgrade died between "code updated" and "containers recreated" finds
# 2.x listening on the port, and the deploy would otherwise POST a 0.7 payload
# at a 2.x API and fail one model at a time, 404 by 404.
#
# Probe: GET /v2/models without auth. 0.7.x has no /v2/* surface and returns
# 404; 2.x has /v2/models gated by auth and returns 401. The status code is the
# signal — no key needed. (/v1/version is a v2-only endpoint despite the /v1
# prefix and 404s on 0.7.x; measured on 0.91 during M029-S04.)
#
# An UNREACHABLE or unexpected answer resolves to 0.7, not to 2.x: 0.7 is what
# the product ships, and the refusal below exists for the case we can actually
# recognise.
gpustack_runtime_version() {
    if [ -n "${_GPUSTACK_RUNTIME:-}" ]; then
        echo "$_GPUSTACK_RUNTIME"
        return
    fi
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        "http://127.0.0.1:${GPUSTACK_PORT:-9090}/v2/models" 2>/dev/null)
    case "$code" in
        401|200) _GPUSTACK_RUNTIME="2.x" ;;
        *)       _GPUSTACK_RUNTIME="0.7" ;;
    esac
    echo "$_GPUSTACK_RUNTIME"
}

# The GPUStack API prefix. One runtime, one prefix (#1447).
gpustack_api_prefix() {
    echo "/v1"
}

# Refuse, once and with the way out, when a removed runtime is still answering.
# Called before anything is deployed — a wrong-runtime box must not be told
# "deploy failed" model by model when the cause is one migration that did not
# finish.
_refuse_gpustack_2x() {
    [ "$(gpustack_runtime_version)" = "2.x" ] || return 0
    print_error "GPUStack 2.x is answering on port ${GPUSTACK_PORT:-9090}, and 2.x was REMOVED in 2026.09 (#979/#1447)."
    print_info "  This box still runs a container from before the migration — most likely an upgrade that stopped between updating the code and recreating the containers."
    print_info "  Finish it: rzfz upgrade   (it migrates a 2.x box to the LLM Manager and re-creates the stack)"
    print_info "  Then re-run this command."
    return 1
}

gpustack_api() {
    local method="$1" path="$2" data="$3"
    local url="http://127.0.0.1:${GPUSTACK_PORT:-9090}${path}"
    # #187: bound EVERY probe so an unresponsive / still-loading gpustack can never
    # hang post-install indefinitely (the untimed `curl -s` used to block forever on
    # a CPU box whose embedding model was still downloading). On timeout curl exits
    # non-zero and prints nothing → the callers' manifest-dim / best-effort fallbacks
    # take over. Override the max via GPUSTACK_API_TIMEOUT if a slow op needs longer.
    local _t=(--connect-timeout 5 --max-time "${GPUSTACK_API_TIMEOUT:-30}")

    if [ -n "$GPUSTACK_API_KEY" ]; then
        if [ -n "$data" ]; then
            curl -s "${_t[@]}" -X "$method" "$url" \
                -H "Authorization: Bearer $GPUSTACK_API_KEY" \
                -H "Content-Type: application/json" \
                -d "$data" 2>/dev/null
        else
            curl -s "${_t[@]}" -X "$method" "$url" \
                -H "Authorization: Bearer $GPUSTACK_API_KEY" 2>/dev/null
        fi
    else
        # Fall back to basic auth (fresh install, no API key yet)
        local admin_pass="${GPUSTACK_ADMIN_PASSWORD:-${AUTHENTIK_BOOTSTRAP_PASSWORD}}"
        if [ -n "$data" ]; then
            curl -s "${_t[@]}" -X "$method" "$url" \
                -u "admin:${admin_pass}" \
                -H "Content-Type: application/json" \
                -d "$data" 2>/dev/null
        else
            curl -s "${_t[@]}" -X "$method" "$url" \
                -u "admin:${admin_pass}" 2>/dev/null
        fi
    fi
}

# ==============================================================================
# Wait Helpers
# ==============================================================================
wait_for_service() {
    local name="$1" url="$2" max_wait="${3:-120}"
    local waited=0 interval=5
    print_substep "Waiting for $name to be ready ($url)..."
    while [ $waited -lt $max_wait ]; do
        if curl -sf --max-time 5 "$url" > /dev/null 2>&1; then
            print_success "$name is ready."
            return 0
        fi
        sleep $interval
        waited=$((waited + interval))
        if [ $((waited % 30)) -eq 0 ]; then
            print_info "Still waiting for $name... (${waited}s / ${max_wait}s)"
        fi
    done
    print_error "$name did not become ready within ${max_wait}s."
    print_info "URL: $url"
    print_info "Try: curl -sf --max-time 5 '$url'"
    return 1
}

# #2183: is a GPUStack instance message a SETTLED scheduling verdict? These
# are what GPUStack writes when no worker can take the model; they do not
# change by waiting (only by freeing memory or picking another model).
_gpustack_verdict_is_settled() {
    local msg="$1"
    case "$msg" in
        *"Cannot find a suitable worker combination"*|*"requires approximately"*|*"no worker"*|*"No worker"*) return 0 ;;
        *) return 1 ;;
    esac
}

wait_for_model() {
    local model_name="$1" max_wait="${2:-1800}"  # 30 min default for large models
    local waited=0 interval=15
    # The model-instances path (one runtime since #1447, resolved centrally)
    local prefix
    prefix=$(gpustack_api_prefix)
    print_substep "Waiting for model '$model_name' to be ready (downloading + loading)..."
    while [ $waited -lt $max_wait ]; do
        local state state_msg
        # #2183: state AND GPUStack's own message. A PENDING can be a settled
        # verdict ("Cannot find a suitable worker combination", "requires
        # approximately … RAM") that no amount of waiting changes — journey C's
        # 0.79 spent 4 × 3600 s on such verdicts, on every post-install run.
        state=$(gpustack_api GET "${prefix}/model-instances" | \
            python3 -c "
import sys,json
d=json.load(sys.stdin)
for mi in d.get('items',[]):
    if mi.get('model_name') == '$model_name':
        print(mi.get('state','unknown'))
        print((mi.get('state_message') or '').replace(chr(10), ' ')[:300])
        break
else:
    print('not_found')
" 2>/dev/null)
        state_msg=$(printf '%s\n' "$state" | sed -n 2p)
        state=$(printf '%s\n' "$state" | sed -n 1p)

        case "$state" in
            running)
                print_success "Model '$model_name' is running."
                return 0
                ;;
            pending|scheduled)
                if _gpustack_verdict_is_settled "$state_msg"; then
                    print_error "Model '$model_name' cannot be placed — GPUStack's verdict is settled, not transient (#2183): ${state_msg}"
                    print_info "  Pick a model that fits this box in the GPUStack UI or free memory; post-install continues with the next model."
                    return 1
                fi
                if [ $((waited % 60)) -eq 0 ] && [ $waited -gt 0 ]; then
                    print_info "  Still waiting... ($state, ${waited}s elapsed)"
                fi
                ;;
            downloading|initializing|starting)
                if [ $((waited % 60)) -eq 0 ] && [ $waited -gt 0 ]; then
                    print_info "  Still waiting... ($state, ${waited}s elapsed)"
                fi
                ;;
            error|failed)
                print_error "Model '$model_name' failed to start (state: $state)."
                return 1
                ;;
            not_found)
                if [ $waited -gt 30 ]; then
                    print_warning "Model '$model_name' instance not found yet..."
                fi
                ;;
        esac
        sleep $interval
        waited=$((waited + interval))
    done
    print_error "Model '$model_name' did not become ready within ${max_wait}s."
    return 1
}

# ==============================================================================
# GPUStack Provisioning
# ==============================================================================

# OWUI_OPENAI_KEYS cannot use the ${GPUSTACK_API_KEY} indirection from .env.example
# (docker-compose does NOT recursively expand a .env value -> Open WebUI gets the literal
# string and GPUStack 401s: "GPUStack connection not enabled"/no models). Resolve it to
# the live key here (runs on BOTH fresh + --refresh, whether the key was just created or
# validated). Only overwrite when unset or still the literal placeholder, so a
# multi-backend (mac gateway) override the operator / Config Portal set is preserved.
_resolve_owui_openai_keys() {
    local cur
    # `|| true` is load-bearing: this file runs under `set -eo pipefail`, and a
    # BARE `x=$(grep … | cut …)` inherits the pipeline's status. When the key is
    # ABSENT grep exits 1, pipefail propagates it, and the run dies HERE — before
    # the very next line, which already handles the empty case. (#200; same shape
    # as the #755 regression fixed in #793.)
    cur=$(grep -m1 '^OWUI_OPENAI_KEYS=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-) || true
    if [ -z "$cur" ] || [ "$cur" = '${GPUSTACK_API_KEY}' ]; then
        update_env_value "$ENV_FILE" "OWUI_OPENAI_KEYS" "$GPUSTACK_API_KEY"
        # OWUI reads OPENAI_API_KEYS from its ENV at startup and the ENV OVERRIDES its
        # stored config — so if OWUI started earlier in the upgrade with the stale
        # literal, recreate it now so the resolved key actually reaches the container.
        docker compose up -d --force-recreate openwebui >/dev/null 2>&1 || true
    fi
}

# ==============================================================================
# #320 (operator option 1): wire the llm-manager line into OWUI + Dify
# ==============================================================================
# The manager's /v1 hot path is key-authed, enforced and METERED — so the
# consumers get minted SERVICE keys (cost-centres stack/openwebui and
# stack/dify) and chargeback separates operator/customer/service traffic.
# Runs on --preset AND --refresh; every step is idempotent and non-fatal
# (a box without the llm-manager profile is untouched).
# _llm_manager_mint_service_key lives in scripts/lib-owui.sh (#1185).


# Set by _llm_manager_enroll_embedded_worker, consumed by the verify suite and
# by the exit-code hook at the end of this script (#1250a). Values: "" = never
# attempted (no llm-manager profile / not enforce mode / already enrolled),
# "ok" = the manager lists a live worker row, "failed" = mint or registration
# did not succeed.
LLM_WORKER_ENROLL_STATUS=""

# #1425: the provisioning steps whose FAIL means the box cannot do its core
# job. Same threshold #1250a set for the embedded-worker enrol — "cannot place
# a single model" is a FAILED provisioning run, not a green exit with a red row
# in the summary:
#   gpustack  THE llm backend (the label covers the Manager branch too). A FAIL
#             means no model is servable at all.
#   dify      A Dify with no model provider is a broken box, not a warning —
#             the propagation at `dify_configure_manager_models || return 1`
#             says exactly that, and BOTH callers used to undo it.
# Deliberately NOT included, so this stays the narrow fix it claims to be:
#   openwebui one consumer's wiring; models stay servable via the API and Dify.
#   speaches  optional STT/TTS — it already distinguishes WARN from FAIL.
#   lightrag / cognee  only ever record OK or WARN, never FAIL.
# Extending the set is a one-word change here; do it with a reason.
# #2051 (operator decision, 2026.09-rc1 cut): GPUStack is NOT critical. Since the
# #1443/#1445 cutover the LLM Manager is the front end of every box and GPUStack is
# an OPTIONAL backend registered behind it — a box with no `llm-legacy` profile
# failing post-install "because gpustack" is backwards. What IS critical on a
# 2026.09 box is whether the MANAGER provisioned, which is what this list was
# really gating all along: the manager's outcome was recorded under the `gpustack`
# key (see the split at the three sites below), so the old value gated the right
# thing under the wrong name. Swapping the name without splitting the key first
# would have silently stopped gating it at all.
POST_INSTALL_CRITICAL_STEPS="llm-manager dify"

# #2051: the exit line used to print raw KEYS while the summary printed labels,
# so an operator read "[FAIL] LLM Manager — …" and then "FAILED: gpustack, dify".
# One source for both.
_critical_step_label() {
    case "$1" in
        llm-manager) printf '%s' "${LLM_BACKEND_SUMMARY_LABEL:-LLM Manager — models deployed on its workers}" ;;
        gpustack)    printf '%s' "GPUStack — API key + models deployed" ;;
        dify)        printf '%s' "Dify — plugins installed" ;;
        *)           printf '%s' "$1" ;;
    esac
}

_exit_on_failed_provisioning() {
    # #1250a rev-B: the exit contract has to hold on EVERY path out of this
    # script, not only the one that runs off the end. `--refresh` without
    # `--verify` exits 0 early (DO_VERIFY defaults to false) — and that is
    # exactly the path the failure message tells the operator to retry with,
    # and the one `rzfz upgrade` runs. Reporting a 401-looping box as a
    # successful refresh there would be this same silent-success defect, one
    # level up. Call this immediately before any early exit.
    #
    # #1425 generalises it: the enrol was the only failure that ever reached
    # the exit code, so a box whose Dify came out with NO model provider still
    # exited 0 — post-install had printed the defect in full and then declared
    # success over it (0.91, run 20260904T182401Z). `rzfz upgrade` calls
    # `--refresh` best-effort and only WARNS on a non-zero exit, so widening
    # this cannot fail an upgrade.
    local _step _failed=""
    for _step in $POST_INSTALL_CRITICAL_STEPS; do
        if [ "${STEP_STATUS[$_step]:-}" = "FAIL" ]; then
            _failed="${_failed:+$_failed, }$(_critical_step_label "$_step")"
        fi
    done
    if [ -n "$_failed" ]; then
        print_error "post-install FAILED: ${_failed} — this box cannot serve models. See the verify report above; re-run 'rzfz post-install --refresh' once the cause is fixed. (#1425)"
        exit 1
    fi
    [ "${LLM_WORKER_ENROLL_STATUS:-}" = "failed" ] || return 0
    print_error "post-install FAILED: the LLM Manager's embedded worker is not registered (#1250) — no model can be placed on this box."
    exit 1
}

_llm_worker_report_interval() {
    # The agent's report period, in whole seconds. Everything that waits for a
    # registration is expressed in this unit: the FIRST report only happens one
    # full interval after the container comes up.
    local interval="${LLM_WORKER_REPORT_INTERVAL:-30}"
    case "$interval" in ''|*[!0-9]*) interval=30 ;; esac
    printf '%s' "$interval"
}

_llm_worker_registration_budget() {
    # Seconds to wait for the first ACCEPTED registration. #1250a rev-B: a
    # hard-coded 60 s could never go green on a box configured with
    # LLM_WORKER_REPORT_INTERVAL=60 — the first report lands at t+60 — so a
    # perfectly healthy box would have been reported as a FAILED provisioning
    # run (and, since rev-A, exited non-zero). Scale with the interval, with a
    # floor in the same order as the 120 s #1255 allows for the same event.
    local budget
    budget=$(( 2 * $(_llm_worker_report_interval) + 30 ))
    [ "$budget" -lt 90 ] && budget=90
    printf '%s' "$budget"
}

_llm_worker_container_name() {
    # The LLM_WORKER_NAME the RUNNING agent container actually carries. This —
    # not .env — is the name the agent REPORTS, and therefore the name the
    # manager keys both the worker row and the command-key HMAC on. Empty when
    # no such container (or no such variable) exists.
    docker inspect llm-worker-agent --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^LLM_WORKER_NAME=//p' | head -1 || true
}

_llm_manager_worker_row() {
    # Print "<worker id>|<seconds since last heartbeat, or `never`>" for the
    # worker NAMED $1, or NOTHING when the manager has no row under that name.
    #
    # A row is created ONLY by a successful POST /api/workers — the enroll
    # exchange (/api/workers/enroll) mints a key and creates no row — so a row
    # proves the registration was accepted AT SOME POINT. Rows are never
    # deleted, which is why the heartbeat age comes with it: "registered once"
    # and "is being accepted right now" are different claims, and only the
    # second one means the box can place a model (#1250a rev-B). Read through
    # the manager's own session (no new endpoint, no DB credential on the
    # host), same rationale as the mint.
    docker exec -i llm-manager python3 - "$1" 2>/dev/null <<'WORKERROWEOF' || true
import datetime, sys
sys.path.insert(0, "/app")
from app.db import session_scope
from app.models import Worker
with session_scope() as s:
    w = s.query(Worker).filter(Worker.name == sys.argv[1].strip()).first()
    if w is not None:
        hb = getattr(w, "last_heartbeat", None)
        if hb is None:
            age = "never"
        else:
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            age = str(int(max(0, (now - hb).total_seconds())))
        print("%s|%s" % (w.id, age))
WORKERROWEOF
}

_llm_manager_live_worker_id() {
    # Print the manager's worker id for $1 ONLY while that worker is LIVE, i.e.
    # it heartbeat recently — which is what "the manager accepts this
    # credential" actually means. The bare row would only prove the worker
    # registered once, so a box that registered and then started 401-looping
    # after a rename or a key rotation would keep reading as healthy (#1250a
    # rev-B; models.py::Worker.last_heartbeat is written on every accepted
    # POST /api/workers, including the first).
    local row id age window
    row=$(_llm_manager_worker_row "$1")
    [ -n "$row" ] || return 0
    id=${row%%|*}
    age=${row##*|}
    case "$age" in ''|*[!0-9]*) return 0 ;; esac
    # Never tighter than the manager's own staleness window
    # (LLM_MANAGER_WORKER_STALE_SECONDS, default 90 s, api/inventory.py):
    # below that we would call a worker dead that the manager itself still
    # schedules on.
    window=$(( 2 * $(_llm_worker_report_interval) ))
    [ "$window" -lt 90 ] && window=90
    [ "$age" -le "$window" ] && printf '%s' "$id"
    return 0
}

_llm_manager_await_worker_registration() {
    # Wait for the agent named $1 to be LIVE in the manager and print its
    # worker id, or return 1 once _llm_worker_registration_budget is spent.
    local name="$1" budget waited wid
    budget=$(_llm_worker_registration_budget)
    waited=0
    while :; do
        wid=$(_llm_manager_live_worker_id "$name")
        if [ -n "$wid" ]; then
            printf '%s' "$wid"
            return 0
        fi
        [ "$waited" -ge "$budget" ] && return 1
        sleep 3
        waited=$((waited + 3))
    done
}

_llm_manager_report_enroll_failure() {
    # $1 = the name that was expected to register, $2 = the headline. Prints
    # the OBSERVED state, because the diagnosis is always the same comparison:
    # the command key is HMAC(node_key, the name the agent REPORTS).
    local name="$1" headline="$2" env_name ctr_name row age
    env_name=$(read_env_value "$ENV_FILE" LLM_WORKER_NAME 2>/dev/null || true)
    ctr_name=$(_llm_worker_container_name)
    row=$(_llm_manager_worker_row "$name")
    print_error "$headline"
    print_info  "  .env      LLM_WORKER_NAME=${env_name:-<empty>}"
    print_info  "  container LLM_WORKER_NAME=${ctr_name:-<unset>}   (a mismatch IS the 401: the command key is HMAC(node_key, worker name))"
    if [ -n "$row" ]; then
        age=${row##*|}
        case "$age" in
            never) print_info "  manager: a row for '$name' exists (id ${row%%|*}) that has NEVER heartbeat." ;;
            *)     print_info "  manager: a row for '$name' exists (id ${row%%|*}) but its last heartbeat is ${age}s old — it registered once and is being REJECTED now." ;;
        esac
    else
        print_info "  manager: no worker row for '$name' at all."
    fi
    docker logs --tail 200 llm-manager 2>&1 | grep -iE '401|command-channel credential' | tail -5 | while IFS= read -r _l; do
        print_info "  manager: $_l"
    done
    return 0
}

_llm_manager_repair_mis_named_worker() {
    # #1250a rev-B: a box that ALREADY holds a command key is not necessarily a
    # healthy box. 0.91 holds a key minted for `vukos-box` while its container
    # runs as `master`; rev-A returned 0 on sight of the key, so
    # `rzfz post-install --refresh` — the very command the failure message
    # recommends, and the one `rzfz upgrade` runs — was a no-op on exactly the
    # boxes that needed it.
    #
    # The repair is deliberately the SMALLEST one that can work: the key is
    # HMAC(node_key, name) over the name that is in .env, and that name has not
    # changed, so the key is still valid and is NOT re-minted. Only the
    # container's environment is re-applied, then the registration is verified
    # exactly as on the mint path. Idempotent: a container already carrying the
    # .env name is left completely alone, so this is safe on every --refresh.
    local ckey="$1" name ctr_name wid
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-worker-agent || return 0
    name=$(read_env_value "$ENV_FILE" LLM_WORKER_NAME 2>/dev/null || true)
    # No name in .env means there is nothing to compare the container against,
    # and no way to know which name the existing key was minted for. Leave it.
    [ -n "$name" ] || return 0
    ctr_name=$(_llm_worker_container_name)
    [ "$ctr_name" = "$name" ] && return 0

    print_warning "Embedded worker runs under the WRONG name (#1250): container LLM_WORKER_NAME=${ctr_name:-<unset>}, .env says '$name'. The command key is HMAC(node_key, the REPORTED name), so every registration is 401-rejected. Recreating the agent with the correct name — no re-mint, the existing key stays valid."
    export LLM_WORKER_NAME="$name"
    LLM_WORKER_NAME="$name" LLM_WORKER_COMMAND_KEY="$ckey" \
        docker compose up -d --no-deps --force-recreate llm-worker-agent >/dev/null 2>&1 || true

    wid=$(_llm_manager_await_worker_registration "$name") || wid=""
    if [ -n "$wid" ]; then
        LLM_WORKER_ENROLL_STATUS="ok"
        print_substep "Embedded worker '$name' re-registered (worker id $wid) after the name repair (#1250)."
        return 0
    fi
    LLM_WORKER_ENROLL_STATUS="failed"
    _llm_manager_report_enroll_failure "$name" \
        "Embedded worker '$name' did NOT register within $(_llm_worker_registration_budget)s after the name repair (#1250) — so NO model can be placed on this box."
    return 1
}

_llm_manager_enroll_embedded_worker() {
    # #1079: on an enforce-mode box (#285) the embedded worker-agent has no
    # per-worker command key — its shared-key registration is 401-rejected on
    # every report cycle, so a fresh single-box install shows ZERO workers
    # forever (silent: the worker-agent swallows registration failures by
    # design). Run the same mint+exchange flow node-init performs for thin
    # nodes, for the LOCAL embedded worker. Operator privilege via docker
    # exec (same rationale as _llm_manager_mint_service_key: no new network
    # endpoint); the enroll exchange itself is token-authenticated
    # (single-use JTI, #340) against the manager's own loopback.
    # Idempotent: no-ops unless enforce mode is on, the command key is still
    # empty, and an embedded worker-agent container actually exists.
    #
    # #1250a — MEASURED on 0.91 (clean install of main, Manager-shaped
    # profiles): this ran, wrote both values to .env, printed "enrolled", and
    # left a worker-agent running as name `master` holding the key minted for
    # `vukos-box`. The manager validates the presented key as
    # HMAC(node_key, the name the agent REPORTS) → 401 on every cycle, forever,
    # silently; /api/workers stayed [] and no model could ever be placed.
    #
    # CAUSE (reproduced in tests/unit/razzfazz-setup-cli with a fake `docker`
    # that records the environment each invocation was handed): load_env()
    # exports EVERY key of .env into this script's environment at startup —
    # including `LLM_WORKER_NAME=` (empty), which config/.env.example ships.
    # `docker compose` gives the process environment PRECEDENCE over the .env
    # FILE and does not re-read the file for an already-set variable, so
    # `${LLM_WORKER_NAME:-master}` in modules/llm/node-agent/compose.yml
    # resolved against the set-but-EMPTY exported copy and rendered the
    # `master` default — even though update_env_value had just written the real
    # name to the file one line earlier. LLM_WORKER_COMMAND_KEY has no line in
    # .env.example, so it is NOT exported, and there the fresh file value DID
    # reach the container: that asymmetry is exactly the observed state. Same
    # class as the #949 clean-install regression.
    #
    # So: resolve the name FIRST, persist it and re-export it BEFORE minting,
    # pass both values EXPLICITLY on the recreate so the container cannot
    # diverge from .env, and then VERIFY that a worker actually registered.
    # CONTRACT CHANGE (#1250a): this function is no longer `return 0` always —
    # a failed enrolment returns 1, is recorded in LLM_WORKER_ENROLL_STATUS,
    # and makes post-install exit non-zero. A box whose embedded worker never
    # registered cannot place a single model, so that is a failed provisioning
    # run, not a warning. (`rzfz upgrade` already treats a non-zero
    # post-install --refresh as non-fatal, so this never fails an upgrade.)
    local mode ckey name out wid
    mode=$(read_env_value "$ENV_FILE" LLM_MANAGER_COMMAND_KEY_MODE 2>/dev/null || true)
    [ "$mode" = "enforce" ] || return 0
    ckey=$(read_env_value "$ENV_FILE" LLM_WORKER_COMMAND_KEY 2>/dev/null || true)
    if [ -n "$ckey" ]; then
        # rev-B: a key on disk is NOT proof of a working enrolment — see
        # _llm_manager_repair_mis_named_worker, which is the no-re-mint repair
        # for the state 0.91 is in today.
        _llm_manager_repair_mis_named_worker "$ckey"
        return $?
    fi
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-worker-agent || return 0

    # (1) The NAME is decided ONCE, before anything is derived from it, and is
    #     persisted + re-exported so .env, this shell and Compose all agree.
    name=$(read_env_value "$ENV_FILE" LLM_WORKER_NAME 2>/dev/null || true)
    [ -n "$name" ] || name="$(hostname -s 2>/dev/null || echo embedded)"
    update_env_value "$ENV_FILE" "LLM_WORKER_NAME" "$name"
    export LLM_WORKER_NAME="$name"

    # (2) Mint the per-worker key for exactly that name.
    out=$(docker exec -i llm-manager python3 - "$name" <<'ENROLLEOF'
import json, sys, urllib.request
sys.path.insert(0, "/app")
from app.api.enroll import mint_enroll_token
from app.config import get_settings

name = sys.argv[1].strip()
settings = get_settings()
if not settings.node_key:
    raise SystemExit("no node_key configured")
token, _exp = mint_enroll_token(name, settings.node_key, 120)
req = urllib.request.Request(
    "http://127.0.0.1:8080/api/workers/enroll",
    data=json.dumps({"token": token}).encode(),
    headers={"Content-Type": "application/json"})
resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
print(resp["command_key"])
ENROLLEOF
    ) || out=""
    if [ -z "$out" ]; then
        LLM_WORKER_ENROLL_STATUS="failed"
        print_error "Embedded-worker auto-enroll FAILED for '$name' (#1079/#1250) — no command key could be minted; the worker-agent will keep 401-looping against the manager and NO model can be placed on this box."
        print_info  "  Retry with: rzfz post-install --refresh   (manual recipe in issue #1079)"
        return 1
    fi
    update_env_value "$ENV_FILE" "LLM_WORKER_COMMAND_KEY" "$out"
    # NOT exported (rev-B): the recreate below hands the key to that ONE
    # command explicitly, so an export would only widen the secret's blast
    # radius — every later child process of this run would inherit it — while
    # changing nothing about the outcome. The NAME export below/above stays: it
    # has to correct the stale EMPTY copy load_env put in this shell.

    # (3) Recreate ONLY the agent (broad recreates are a known footgun on
    #     running stacks), with BOTH values handed to Compose EXPLICITLY in the
    #     command's environment so a stale exported copy can never shadow them
    #     again, and --force-recreate so the container is rebuilt from the
    #     values we just resolved rather than kept on a matching config hash.
    LLM_WORKER_NAME="$name" LLM_WORKER_COMMAND_KEY="$out" \
        docker compose up -d --no-deps --force-recreate llm-worker-agent >/dev/null 2>&1 || true

    # (4) VERIFY. The agent registers on its report cycle
    #     (LLM_WORKER_REPORT_INTERVAL, 30 s by default) after the container
    #     comes up, and the FIRST report lands a whole interval late — so the
    #     budget is derived from that interval, never a fixed 60 s (rev-B: a
    #     box with interval 60 could not have gone green at all).
    wid=$(_llm_manager_await_worker_registration "$name") || wid=""
    if [ -n "$wid" ]; then
        LLM_WORKER_ENROLL_STATUS="ok"
        print_substep "Embedded worker '$name' enrolled AND registered (worker id $wid) — per-worker command key minted, worker-agent recreated (#1079/#1250)."
        return 0
    fi

    LLM_WORKER_ENROLL_STATUS="failed"
    _llm_manager_report_enroll_failure "$name" \
        "Embedded worker '$name' did NOT register within $(_llm_worker_registration_budget)s (#1250) — the manager lists no live worker under that name, so NO model can be placed on this box."
    return 1
}

_owui_append_endpoint() {
    # $1=current bases  $2=current keys  $3=url  $4=key
    # Prints two lines: new bases / new keys. Appends ONLY when the url is
    # absent; existing multi-backend combos (mac-llm) are preserved verbatim.
    local bases="$1" keys="$2" url="$3" key="$4"
    case ";${bases};" in
        *";${url};"*) printf '%s\n%s\n' "$bases" "$keys"; return 0 ;;
    esac
    # #976: an EMPTY existing list (a gpustack-disabled llm-manager box, where
    # the gpustack default is no longer seeded) must yield the url ALONE — not
    # ";url" with an empty leading entry, which OWUI would try to query as a
    # zero-length base URL.
    if [ -z "$bases" ]; then
        printf '%s\n%s\n' "$url" "$key"; return 0
    fi
    printf '%s;%s\n%s;%s\n' "$bases" "$url" "$keys" "$key"
}

# ── LLM backend source-of-truth (#976) ───────────────────────────────────────
# _gpustack_profile_active / _llm_manager_profile_active / _llm_manager_running
# / _llm_manager_active / _owui_llm_base_url / _owui_llm_key / _owui_rerank_url
# live in scripts/lib-owui.sh (#1185/#908): the SINGLE place that decides
# which backend URL + key a consumer gets, shared with cli/upgrade.sh.
# _owui_llm_key VALIDATES the .env key against the manager before it is
# used (#1185) — a dead key never overwrites a working in-app one.

# #976: drop any gpustack endpoint (and its paired key) from a ;-joined
# base/key pair. On a box that MOVED OFF gpustack (e.g. an upgrade that
# disabled gpustack and enabled llm-manager) the stale gpustack entry was
# already persisted in OWUI_OPENAI_BASE_URLS, so avoiding-the-seed alone is not
# enough — the existing dead endpoint must be pruned, else OWUI keeps querying a
# backend that no longer runs. Prints two lines: filtered bases / filtered keys.
#
# #1306: the second thing that must not sit in that list is OPEN WEBUI'S OWN
# CLOUD DEFAULT. Since #1266/#1252 the container gets an explicitly EMPTY
# OPENAI_API_BASE_URLS, and Open WebUI resolves an empty entry to
# `https://api.openai.com/v1` with an empty key. A dead in-stack endpoint was
# harmless; a cloud endpoint is not — it is visible in the UI, and on an
# air-gapped appliance it is a statement we do not want to make.
#
# The prune is deliberately NARROW: only an api.openai.com entry whose paired
# key is EMPTY or a placeholder. That shape is the default artefact and nothing
# else — an operator who deliberately connected OpenAI has a real key there,
# and their connection survives untouched.
_owui_strip_gpustack() {
    local IFS=';'
    local -a _bs _ks
    read -ra _bs <<< "$1"
    read -ra _ks <<< "$2"
    local ob="" ok="" i b k
    for i in "${!_bs[@]}"; do
        b="${_bs[$i]}"
        [ -z "$b" ] && continue
        case "$b" in *gpustack:9090*) continue ;; esac
        k="${_ks[$i]:-}"
        case "$b" in
            *api.openai.com*)
                case "$k" in
                    ""|CHANGEME*|changeme*|sk-CHANGEME*|"<"*) continue ;;
                esac
                ;;
        esac
        ob="${ob:+$ob;}$b"
        ok="${ok:+$ok;}$k"
    done
    printf '%s\n%s\n' "$ob" "$ok"
}

# ==============================================================================
# #245: observability consumers — Dify OTEL + personal-agent OTLP seam
# ==============================================================================
# The observability profile deploys OpenLIT/ClickHouse/otel-collector, but
# nothing instrumented the LLM surfaces: Dify ships ENABLE_OTEL=false and
# the agents got no OTLP env. Idempotent on --preset AND --refresh; only
# ever ENABLES (an operator who set values manually keeps them — this
# never flips observability off when the profile goes away, it just stops
# injecting into NEW agents via the emptied seam).
wire_observability_consumers() {
    if ! echo "${COMPOSE_PROFILES:-}" | grep -qw "observability"; then
        # profile off: empty the agent seam so NEW instances stop pointing
        # at a collector that is not running (existing ones keep their env
        # until recreate — harmless, the SDK just fails to export).
        local cur_ep
        cur_ep=$(read_env_value "$ENV_FILE" OBSERVABILITY_OTEL_AGENTS_ENDPOINT 2>/dev/null || true)
        if [ -n "$cur_ep" ]; then
            update_env_value "$ENV_FILE" "OBSERVABILITY_OTEL_AGENTS_ENDPOINT" ""
            # review #683: the provisioner reads the RUNNING container's
            # os.environ — without a recreate it keeps injecting the dead
            # collector into every NEW agent instance.
            docker compose up -d agent-manager >/dev/null 2>&1 || true
        fi
        # LLM-MANAGER-OTEL-OFF-BEGIN (#2003; sliced by
        # tests/unit/consistency/test_2003_the_manager_seam_agrees.py)
        # Symmetric with the ON branch below, for the same reason the agents
        # seam is: a pointer at a collector that is not running. Here it also
        # costs a restart — app/config.py writes a `callbacks:["otel"]` into the
        # router config while this is set, so leaving it set after the profile
        # goes away means the router keeps trying to export on every request.
        local cur_mgr
        cur_mgr=$(read_env_value "$ENV_FILE" LLM_MANAGER_OTEL_ENDPOINT 2>/dev/null || true)
        if [ -n "$cur_mgr" ]; then
            update_env_value "$ENV_FILE" "LLM_MANAGER_OTEL_ENDPOINT" ""
            docker compose up -d llm-manager llm-manager-router >/dev/null 2>&1 || true
        fi
        # LLM-MANAGER-OTEL-OFF-END
        return 0
    fi
    print_step "Observability: wiring LLM-surface consumers (#245)..."
    local changed=false
    # ── agents seam ─────────────────────────────────────────────────────
    # AGENTS-OTEL-BEGIN (#2015: sliced by tests/unit/consistency/test_2015_*,
    # which RUNS this and profile_provisioner.agents_otel_updates side by side)
    local ep
    ep=$(read_env_value "$ENV_FILE" OBSERVABILITY_OTEL_AGENTS_ENDPOINT 2>/dev/null || true)
    if [ "$ep" != "http://otel-collector:4318" ]; then
        update_env_value "$ENV_FILE" "OBSERVABILITY_OTEL_AGENTS_ENDPOINT" "http://otel-collector:4318"
        docker compose up -d agent-manager >/dev/null 2>&1 || true
        print_substep "Agent OTLP seam set — new/recreated agent instances emit traces."
    fi
    # AGENTS-OTEL-END
    # OWUI-OTEL-BEGIN (#2015: the Open WebUI seam was wired by the Portal toggle
    # only. A box upgraded from before #245 still carries OPENLIT_OTLP_ENDPOINT=
    # http://openlit:4318 — a host with no OTLP receiver — and this path never
    # healed it, so "rzfz post-install" produced a different box than the toggle.
    # Same value the toggle writes; `pipelines` reads it at container CREATE time.)
    local owui_ep
    owui_ep=$(read_env_value "$ENV_FILE" OPENLIT_OTLP_ENDPOINT 2>/dev/null || true)
    if [ "$owui_ep" != "http://otel-collector:4318/v1/traces" ]; then
        update_env_value "$ENV_FILE" "OPENLIT_OTLP_ENDPOINT" "http://otel-collector:4318/v1/traces"
        if echo "${COMPOSE_PROFILES:-}" | grep -qw "chat"; then
            docker compose up -d --no-deps --force-recreate pipelines >/dev/null 2>&1 || true
        fi
        print_substep "Open WebUI OTLP seam set — pipelines emit traces to the collector (was ${owui_ep:-<empty>})."
    fi
    # OWUI-OTEL-END
    # ── Dify ────────────────────────────────────────────────────────────
    # DIFY-OTEL-BEGIN  (sliced verbatim by
    # tests/unit/consistency/test_245_dify_otel_wiring_paths_agree.py, which
    # RUNS this block and compares the four writes it makes against the Config
    # Portal's half of the same wiring —
    # core/config/app/services/profile_provisioner.py::dify_otel_updates.
    # Keep the banner comments; the slice is anchored on them.)
    #
    # Every key is checked on its OWN value. The old shape read ENABLE_OTEL
    # and, once that said `true`, never looked at the endpoint again — a stale
    # or hand-edited OTLP_BASE_ENDPOINT could not be healed by this path at
    # all. Two of the four keys are new; the full reasoning sits next to
    # dify_otel_updates(), and in short:
    #   * OTEL_EXPORTER_OTLP_PROTOCOL — modules/dify/compose.yml gives dify-api
    #     BOTH env files, and the stack's own .env carries `grpc` for the
    #     collector's EXTERNAL log-export leg (#197). Dify reads the same NAME
    #     and would build a gRPC exporter against http://otel-collector:4318 —
    #     the HTTP receiver (gRPC is 4317). Every span dropped, silently.
    #   * OTEL_SAMPLING_RATE — upstream ships 0.1 for SaaS volume. One workflow
    #     run is ONE root trace, so nine runs out of ten emit nothing.
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "dify"; then
        local dify_otel dify_ep dify_proto dify_rate
        dify_otel=$(read_env_value ".env.dify" ENABLE_OTEL 2>/dev/null || true)
        if [ "$dify_otel" != "true" ]; then
            update_env_value ".env.dify" "ENABLE_OTEL" "true"
            changed=true
        fi
        dify_ep=$(read_env_value ".env.dify" OTLP_BASE_ENDPOINT 2>/dev/null || true)
        if [ "$dify_ep" != "http://otel-collector:4318" ]; then
            update_env_value ".env.dify" "OTLP_BASE_ENDPOINT" "http://otel-collector:4318"
            changed=true
        fi
        dify_proto=$(read_env_value ".env.dify" OTEL_EXPORTER_OTLP_PROTOCOL 2>/dev/null || true)
        if [ "$dify_proto" != "http/protobuf" ]; then
            update_env_value ".env.dify" "OTEL_EXPORTER_OTLP_PROTOCOL" "http/protobuf"
            changed=true
        fi
        # Only the UPSTREAM default is raised — an operator who chose a rate of
        # their own keeps it.
        dify_rate=$(read_env_value ".env.dify" OTEL_SAMPLING_RATE 2>/dev/null || true)
        if [ -z "$dify_rate" ] || [ "$dify_rate" = "0.1" ]; then
            update_env_value ".env.dify" "OTEL_SAMPLING_RATE" "1.0"
            changed=true
        fi
        # DIFY-OTEL-END
        if [ "$changed" = true ]; then
            print_substep "Dify OTEL enabled — recreating dify-api/dify-worker to pick it up."
            docker compose up -d --force-recreate dify-api dify-worker >/dev/null 2>&1 || true
        else
            print_substep "Dify OTEL already enabled."
        fi
    fi
    # ── LLM Manager + its router ────────────────────────────────────────
    # LLM-MANAGER-OTEL-BEGIN (#2003; sliced by
    # tests/unit/consistency/test_2003_the_manager_seam_agrees.py, which RUNS
    # this block and compares its write against the Config Portal's half.
    # Keep the banner comments; the slice is anchored on them.)
    #
    # This was the gap #2003 is about: `.env.example` carried
    # `LLM_MANAGER_OTEL_ENDPOINT=` with a comment telling the reader to set it
    # when the profile is on, and NOTHING set it. Since the 2026.09 cutover
    # every LLM call in the stack goes through the manager, so the one surface
    # that sees all of them emitted nothing while the dashboards were called
    # empty. `grep -rn LLM_MANAGER_OTEL_ENDPOINT cli/ core/config/app/ scripts/`
    # returned no hits at all.
    #
    # One value, two consumers: modules/llm/manager/compose.yml forwards it to
    # the manager AND to llm-manager-router (as OTEL_EXPORTER_OTLP_ENDPOINT),
    # and app/config.py turns a non-empty value into the router's `otel`
    # callback. So the router config CHANGES here, which the entrypoint watcher
    # turns into a LiteLLM restart — the one #1955 measured at ~11 s of refused
    # connections and has since cushioned. Wiring observability therefore costs
    # a reload, once, and that is stated rather than discovered.
    local mgr_ep
    mgr_ep=$(read_env_value "$ENV_FILE" LLM_MANAGER_OTEL_ENDPOINT 2>/dev/null || true)
    if [ "$mgr_ep" != "http://otel-collector:4318" ]; then
        update_env_value "$ENV_FILE" "LLM_MANAGER_OTEL_ENDPOINT" "http://otel-collector:4318"
        docker compose up -d llm-manager llm-manager-router >/dev/null 2>&1 || true
        print_substep "LLM Manager + router OTLP seam set — every /v1 call now emits a span (one router reload, #1955)."
    fi
    # LLM-MANAGER-OTEL-END
    print_success "Observability consumer wiring done."
}

wire_llm_manager_consumers() {
    # #1445 (C5c) review: these two lines used to be hand-copied predicates.
    # They behave identically to `_llm_manager_profile_active` /
    # `_llm_manager_running` TODAY — and that is the problem: the moment the
    # predicate learns something (federation, another alias, a renamed
    # profile), a copy stays on the old answer, silently. The guard that pins
    # this classification could not see them either, because it looks for the
    # predicate by name.
    _llm_manager_profile_active || return 0
    if ! _llm_manager_running; then
        print_warning "llm-manager profile active but container not running — consumer wiring skipped (re-run 'rzfz post-install --refresh' once it is up)."
        return 0
    fi
    print_step "LLM Manager: wiring OWUI/Dify consumers (#320)..."

    # #1079: make sure the LOCAL embedded worker is enrolled before anything
    # else — with zero workers every deploy 409s and the console stays empty.
    # #1250a: the enrol now RETURNS non-zero when the worker did not actually
    # register. Keep going (the OWUI/Dify key wiring below is independent and
    # still worth doing) but let the recorded status reach the verify report
    # and the run's exit code — a bare call would abort here under `set -e`.
    _llm_manager_enroll_embedded_worker || \
        print_warning "Embedded-worker enrolment did not complete — see the error above (#1250); post-install will exit non-zero."

    # ── OWUI: service key + second endpoint ─────────────────────────────────
    # #1185: the key is RESOLVED, not read — _owui_resolve_manager_key validates
    # the .env value against the manager, adopts a valid key OWUI already holds
    # when .env is stale (and writes it back), and mints only when nothing valid
    # exists. A stale .env key must never be seeded over a working config.
    # #1441 rev-C: this entry IS the manager's URL, so it always carries the
    # manager's own key — never the ownership-following _owui_llm_key, which on
    # a dual box (GPUStack owns the models) resolves to GPUStack's key and would
    # pair http://llm-manager:8080/v1 with a credential the manager 401s.
    local owui_key
    owui_key=$(_owui_resolve_manager_key) || owui_key=""
    if [ -z "$owui_key" ]; then
        print_warning "Could not mint the OWUI service key — endpoint not appended."
    fi
    if [ -n "$owui_key" ]; then
        local bases keys out orig_bases orig_keys
        orig_bases=$(read_env_value "$ENV_FILE" OWUI_OPENAI_BASE_URLS 2>/dev/null || true)
        orig_keys=$(read_env_value "$ENV_FILE" OWUI_OPENAI_KEYS 2>/dev/null || true)
        bases="$orig_bases"
        keys="$orig_keys"
        # #976: on a box that moved OFF gpustack (llm-manager active, no gpustack
        # profile) PRUNE any stale gpustack endpoint so OWUI stops querying a
        # dead backend — covers the gpustack→manager transition, not just fresh
        # installs where the list starts empty.
        if ! _gpustack_profile_active; then
            local _stripped
            _stripped=$(_owui_strip_gpustack "$bases" "$keys")
            bases=$(printf '%s' "$_stripped" | sed -n 1p)
            keys=$(printf '%s' "$_stripped" | sed -n 2p)
        fi
        # #1445 (C5c): no gpustack seed at all any more. The append below
        # makes the canonical endpoint the sole entry on EVERY box — the
        # manager fronts GPUStack (#1442), so a second, backend-named entry
        # would only be a duplicate of the same models with the wrong key.
        out=$(_owui_append_endpoint "$bases" "$keys" "$LLM_CANONICAL_ENDPOINT" "$owui_key")
        local new_bases new_keys
        new_bases=$(printf '%s' "$out" | sed -n 1p)
        new_keys=$(printf '%s' "$out" | sed -n 2p)
        # #1185: append is a no-op when the manager URL is already listed, so
        # a STALE key at that position survived every run. Pin the entry to
        # the resolved key — .env and the app config are written as a pair.
        new_keys=$(_owui_set_endpoint_key "$new_bases" "$new_keys" "$LLM_CANONICAL_ENDPOINT" "$owui_key")
        # #976: compare against the ORIGINAL .env values, NOT the post-prune
        # locals — otherwise a run whose ONLY change is the gpustack prune (no
        # append needed, the manager is already present) would see
        # new_bases == bases and skip the write, never persisting the prune.
        if [ "$new_bases" != "$orig_bases" ] || [ "$new_keys" != "$orig_keys" ]; then
            update_env_value "$ENV_FILE" "OWUI_OPENAI_BASE_URLS" "$new_bases"
            update_env_value "$ENV_FILE" "OWUI_OPENAI_KEYS" "$new_keys"
            print_substep "OWUI endpoint list now carries the manager (models auto-discovered via /v1/models)."
            docker compose up -d --force-recreate openwebui >/dev/null 2>&1 || true
        else
            print_substep "OWUI already wired to the manager."
        fi
        # #976: the env above is only a FIRST-BOOT seed — OWUI persists its
        # OpenAI connection in openwebui_db and ignores env on later boots, so an
        # already-initialised box keeps Admin → Connections pointed at gpustack.
        # Repoint the PERSISTED connection (base URL + key) to the manager in the
        # DB so the UI + models actually move. (Superseded in 2026.09 by the
        # canonical `llm` endpoint, which makes this wiring backend-invariant.)
        _owui_repoint_persisted_openai_to_manager
    fi

    # ── Config Portal: its OWN read-only service key (EXO-13 / CFG-14) ─────
    # The admin dashboard used to authenticate to the manager by BORROWING
    # LLM_MANAGER_OWUI_KEY (falling back to the Dify key). That is wrong on
    # three counts: the portal's reads are metered against the stack/openwebui
    # cost centre and pollute the very usage series the dashboard renders;
    # rotating or rate-limiting the OWUI key silently darkens the ADMIN GPU
    # panel with a misleading auth error; and one long-lived credential ends
    # up spanning two trust domains, so revoking it for a compromised OWUI
    # also breaks admin observability. Its own cost centre (stack/config-portal)
    # separates the traffic and makes the key independently revocable.
    # NOTE: minted with NO allowed_models — an empty allow-list means
    # unrestricted, which is what an admin "what does this box serve?" panel
    # needs; the OWUI key may legitimately be model-restricted (#329) and
    # would show a FILTERED list as if it were the fleet.
    local cfg_key
    cfg_key=$(read_env_value "$ENV_FILE" LLM_MANAGER_CONFIG_KEY 2>/dev/null || true)
    if [ -z "$cfg_key" ]; then
        cfg_key=$(_llm_manager_mint_service_key config-portal) || cfg_key=""
        if [ -n "$cfg_key" ]; then
            update_env_value "$ENV_FILE" "LLM_MANAGER_CONFIG_KEY" "$cfg_key"
            print_substep "Minted stack/config-portal service key (admin dashboard reads)."
            docker compose up -d --force-recreate razzfazz-config >/dev/null 2>&1 || true
        else
            print_warning "Could not mint the Config Portal service key — the dashboard falls back to the OWUI key (EXO-13)."
        fi
    fi

    # ── Dify: service key + register the currently-served chat models ───────
    echo "${COMPOSE_PROFILES:-}" | grep -qw "dify" || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx dify-api || return 0
    local dify_key
    dify_key=$(read_env_value "$ENV_FILE" LLM_MANAGER_DIFY_KEY 2>/dev/null || true)
    if [ -z "$dify_key" ]; then
        dify_key=$(_llm_manager_mint_service_key dify) || dify_key=""
        [ -n "$dify_key" ] && update_env_value "$ENV_FILE" "LLM_MANAGER_DIFY_KEY" "$dify_key" \
            && print_substep "Minted stack/dify service key."
    fi
    [ -n "$dify_key" ] || return 0
    # review #666: /v1/models proxies the router list WITHOUT a task filter
    # — registering embed/rerank models as mode=chat would give Dify broken
    # provider entries. Enumerate CHAT deployments straight from the
    # manager's rows instead (same docker-exec pattern as the mint), and
    # keep the implicit key probe as a cheap /v1/models 200-check.
    if ! curl -fsS -m 10 -o /dev/null -H "Authorization: Bearer $dify_key" \
        "http://127.0.0.1:${LLM_MANAGER_PORT:-8091}/v1/models" 2>/dev/null; then
        print_warning "stack/dify service key failed its /v1/models probe — Dify registration skipped."
        return 0
    fi
    local served
    # -i as above; and "serving now" means a READY instance, not a Deployment
    # status literal — on the live box every deployment sat at 'pending'
    # while its engines were ready, so status == "active" enumerated nothing.
    # Same semantics as the router config's generate_from_db.
    served=$(docker exec -i llm-manager python3 - <<'CHATEOF'
import sys
sys.path.insert(0, "/app")
from app.db import session_scope
from app.models import Deployment, DeploymentInstance
with session_scope() as s:
    for d in (s.query(Deployment)
              .filter(Deployment.task == "chat")
              .join(DeploymentInstance,
                    DeploymentInstance.deployment_id == Deployment.id)
              .filter(DeploymentInstance.status == "ready")
              .distinct()
              .all()):
        print(d.model_name)
CHATEOF
    ) || served=""
    if [ -z "$served" ]; then
        print_substep "Manager serves no models yet — Dify registration deferred to the next --refresh."
        return 0
    fi
    local admin_email
    admin_email=$(_resolve_dify_admin_email)
    # #1273: register EACH model with the context the manifest declares for it,
    # not one flat 32768 for all. A clean install goes through
    # dify_configure_manager_models, which reads `per_slot_context` per alias;
    # this path used a single hardcoded value, so a box that was WIRED here (an
    # upgrade, or any --refresh before the models were deployed) kept 32768 for
    # a model the manifest gives 262144 — and both paths write the same
    # credential label, so the wrong value simply stayed. Same manifest helper,
    # same fallback, one model at a time.
    local _m _ctx _reg_ok=0 _reg_total=0
    while IFS= read -r _m; do
        [ -n "$_m" ] || continue
        _ctx=$(_manifest_per_slot_context "$_m" 32768)
        _reg_total=$((_reg_total + 1))
        # #1445 (C5c): the canonical endpoint, not the manager's own host —
        # every consumer address goes through `llm:8080/v1` since the C5
        # rochade, and this call site was the last literal.
        # #1959: in a condition, not bare — the return value is the registration
        # verdict now, and one unregistrable model must not abort the run.
        if _dify_register_models "$admin_email" "$dify_key" "$LLM_CANONICAL_ENDPOINT" \
                "LLM Manager" "$_ctx" "$_m"; then
            _reg_ok=$((_reg_ok + 1))
        fi
    done <<< "$served"
    # #1959: this line used to read "done." whatever came back. It now says what
    # actually reached Dify — a box whose admin account no longer matches the
    # derived address (#183) registers NOTHING, and that must not look green.
    if [ "$_reg_ok" -eq "$_reg_total" ]; then
        print_success "LLM Manager consumer wiring done (${_reg_ok}/${_reg_total} models registered with Dify)."
    elif [ "$_reg_ok" -eq 0 ]; then
        print_error "LLM Manager consumer wiring: NONE of the ${_reg_total} models registered with Dify — the lines above say why (#1959)."
        return 1
    else
        print_warning "LLM Manager consumer wiring: only ${_reg_ok} of ${_reg_total} models registered with Dify — the lines above say why (#1959)."
        return 1
    fi
}

# #813: ONE Dify chat-model registration path, shared by every OpenAI-compatible
# 2nd endpoint the stack wires up (the LLM Manager, #320/#666; the Mac gateway).
# Extracted rather than copied: both callers register through the SAME Dify
# provider plugin with the same duplicate-tolerant semantics, and two copies of
# this block would drift the moment one of them learns something the other does
# not — the review constraint #240 already made once, for _owui_append_endpoint.
#
# The GPUStack path further down is deliberately NOT folded in here: it targets a
# different provider plugin (langgenius/gpustack) with its own credential shape.
#
# usage: _dify_register_models <admin_email> <api_key> <endpoint_url> \
#                              <credential_label> <context_size> <models;list> \
#                              [model_type] [extra_credentials_json]
#
# context_size is a PARAMETER with no discovery behind it, on purpose: an
# OpenAI-compatible /v1/models response carries no context length (the same
# no-auto-discovery wall the GPUStack wiring hit), so the value is declared by
# the caller and wrong-but-declared beats guessed.
#
# #1248 added the last two parameters instead of a second registration function:
# a Manager box needs the SAME provider + credential for its embedding and
# rerank models too, and a copy of this block per model type is precisely the
# drift #240/#813 already forbade (tests/unit/.../test_813_* asserts there are
# exactly TWO create_model_credential sites in this file — this one and the
# GPUStack provider's). `model_type` defaults to llm, which keeps the two
# pre-#1248 call sites byte-identical in behaviour; `mode: chat` is sent for llm
# only, because it is an llm-only credential field. Keys the plugin does not
# declare for the given model type are dropped by Dify's
# validate_credential_form_schemas, so an extra flag is safe but never silently
# authoritative.
#
# The raw registration output is also published in _DIFY_REG_LAST_OUTPUT so a
# caller can tell "nothing registered" from "registered fine" — the pipeline
# form alone always returned 0 and hid a total failure (the S20 lesson the
# GPUStack path already learned the hard way).
#
# #1959: publishing it was not enough. TWO of the three callers never read it
# and printed their success line regardless, so a Dify that answered
# `Admin account not found` — every model unregistered — came out as
# "[✓] Mac gateway Dify registration done." with rc 0 (measured on 0.91,
# 2026-09-11). The verdict therefore lives HERE now, one level below the
# per-type count #1248 rev-B added to dify_configure_manager_models:
#
#   _DIFY_REG_TOTAL  models asked for
#   _DIFY_REG_OK     models that produced a success line
#   return 0         iff OK == TOTAL
#
# Counted are the SUCCESS forms, not the known failure forms. A list of the
# three errors the embedded Python knows today (`Admin account not found`,
# `Tenant not found`, `… error: <e>`) is a blocklist, and the next shape Dify
# invents falls straight through it again; the success lines are ours and we
# know all of them. It also covers the case that has no line at all — a
# `docker exec` that never ran.
#
# The return value is a verdict, not an abort: every caller consumes it in a
# condition, because a single unregistrable model must not stop the
# provisioning run (and `set -e` would do exactly that on a bare call).
_dify_register_models() {
    local admin_email="$1" api_key="$2" endpoint_url="$3"
    local cred_label="$4" context_size="$5" models="$6"
    local model_type="${7:-llm}" extra_creds="${8:-}"
    [ -n "$extra_creds" ] || extra_creds='{}'
    _DIFY_REG_LAST_OUTPUT=""
    # Nothing asked for is nothing missing — 0 of 0 is a pass, and the counters
    # are published on this path too so a caller never reads a stale pair.
    _DIFY_REG_OK=0
    _DIFY_REG_TOTAL=0
    [ -n "$models" ] || return 0
    local _dify_reg_py="
import json, sys
sys.path.insert(0, '/app/api')
from app import create_app
from extensions.ext_database import db
from models.account import Account, TenantAccountJoin
from services.model_provider_service import ModelProviderService
_, app = create_app()
with app.app_context():
    account = db.session.query(Account).filter(Account.email == sys.argv[1]).first()
    if not account:
        print('Admin account not found'); sys.exit(1)
    tj = db.session.query(TenantAccountJoin).filter(TenantAccountJoin.account_id == account.id).first()
    if not tj:
        print('Tenant not found'); sys.exit(1)
    mps = ModelProviderService()
    provider = 'langgenius/openai_api_compatible/openai_api_compatible'
    label = sys.argv[5]
    mtype = sys.argv[7] or 'llm'
    try:
        extra = json.loads(sys.argv[8] or '{}')
    except Exception as exc:
        print(f'{label} {mtype} extra-credentials unreadable ({exc}) — ignored.')
        extra = {}
    for name in sys.argv[4].split(';'):
        if not name: continue
        creds = {'api_key': sys.argv[2],
                 'endpoint_url': sys.argv[3],
                 'context_size': sys.argv[6]}
        if mtype == 'llm':
            creds['mode'] = 'chat'
        elif mtype == 'text-embedding':
            # #1509: the openai_api_compatible plugin declares max_chunks as a
            # REQUIRED credential-form field for text-embedding (and only for it
            # — llm and rerank do not have it). Without it the plugin's schema
            # validator raises ValueError 'Variable max_chunks is required'
            # BEFORE any network call, so the whole embedding half of the
            # registration is refused while chat and rerank register fine. That
            # is a Dify with no embedding model and no embedding default: a
            # knowledge base cannot index, and nothing says why.
            # '1' is the plugin's own declared default (measured on 0.175,
            # Dify 1.15) — one text per embeddings request. The batch size
            # is not ours to raise blind: #1057 has the manager 429 on inputs a
            # runner's ubatch cannot take.
            creds['max_chunks'] = '1'
        creds.update(extra)
        try:
            mps.create_model_credential(
                tenant_id=tj.tenant_id, provider=provider, model=name,
                model_type=mtype,
                credentials=creds,
                credential_name=label)
            print(f'{label} {mtype} model [{name}] added.')
        except Exception as e:
            msg = str(e).lower()
            if 'already' in msg or 'duplicate' in msg or 'exists' in msg:
                # #1308: create-or-skip left a ROTATED key behind for good — the
                # stored credential never learned the new LLM_MANAGER_DIFY_KEY
                # and every model call 401'd behind three green lines. Refresh
                # the existing credential instead (Dify 1.17:
                # update_model_credential, id via the available-credentials list).
                try:
                    avail = mps.get_provider_model_available_credentials(
                        tenant_id=tj.tenant_id, provider=provider, model_type=mtype, model=name)
                    cid = next((c.credential_id for c in avail
                                if getattr(c, 'credential_name', None) == label), None)
                    # Only OUR label is refreshed — a foreign credential on a
                    # customer box (their own entry) is never overwritten with
                    # the stack key (review #1387, Befund 6).
                    if cid is None:
                        others = ', '.join(str(getattr(c, 'credential_name', '?')) for c in avail) or 'none'
                        print(f'{label} {mtype} model [{name}] already configured (no credential labelled {label!r} to refresh; present: {others}) - left untouched.')
                    else:
                        mps.update_model_credential(
                            tenant_id=tj.tenant_id, provider=provider, model=name,
                            model_type=mtype, credentials=creds,
                            credential_id=cid, credential_name=label)
                        print(f'{label} {mtype} model [{name}] updated (credential refreshed).')
                except Exception as e2:
                    print(f'{label} {mtype} model [{name}] error: refresh failed: {e2}')
            else:
                print(f'{label} {mtype} model [{name}] error: {e}')
"
    _DIFY_REG_LAST_OUTPUT=$(docker exec dify-api python3 -c "$_dify_reg_py" \
        "$admin_email" "$api_key" "$endpoint_url" "$models" "$cred_label" \
        "$context_size" "$model_type" "$extra_creds" 2>/dev/null) || true
    # An `if` (not `[ … ] && …`) is load-bearing: as the last command of the loop
    # body a failed test makes the while — and the pipeline — return 1, and under
    # the `set -e` this script declares that aborts the caller before the
    # `return 0` below. An empty output is a normal outcome (the #813
    # Mac-gateway harness runs exactly that path).
    printf '%s\n' "$_DIFY_REG_LAST_OUTPUT" | while IFS= read -r line; do
        if [ -n "$line" ]; then print_substep "  $line"; fi
    done

    # #1959: the verdict. One line per model, matched as a FIXED string against
    # the three success forms the embedded Python prints — `grep -F` on purpose,
    # because model aliases carry dots (`mac-qwen3.6`) that a regex would happily
    # match against anything.
    local _name
    _DIFY_REG_OK=0
    _DIFY_REG_TOTAL=0
    while IFS= read -r _name; do
        [ -n "$_name" ] || continue
        _DIFY_REG_TOTAL=$((_DIFY_REG_TOTAL + 1))
        if printf '%s\n' "$_DIFY_REG_LAST_OUTPUT" | grep -qF \
                -e "${cred_label} ${model_type} model [${_name}] added." \
                -e "${cred_label} ${model_type} model [${_name}] updated (credential refreshed)." \
                -e "${cred_label} ${model_type} model [${_name}] already configured"; then
            _DIFY_REG_OK=$((_DIFY_REG_OK + 1))
        fi
    done <<< "$(printf '%s' "$models" | tr ';' '\n')"
    [ "$_DIFY_REG_OK" -eq "$_DIFY_REG_TOTAL" ]
}

# _dify_pick_default <preferred> <newline-separated candidates>
# The preferred alias when it is among the candidates, else the first candidate,
# else empty. A workspace default may only name a model that was actually
# registered — selecting the manifest default unconditionally is how a box whose
# always-on set does not contain it ends up with a default nothing can serve.
_dify_pick_default() {
    local want="$1" candidates="$2"
    if [ -n "$want" ] && printf '%s\n' "$candidates" | grep -Fxq "$want"; then
        printf '%s' "$want"
        return 0
    fi
    printf '%s\n' "$candidates" | awk 'NF {print; exit}'
}

# usage: _dify_set_default_models <admin_email> <provider> <type=model;type=model;…>
#
# The workspace-level "System Model Settings" defaults. Registering a credential
# is only half the wiring: with no default text-generation / text-embedding /
# rerank model a new Dify app has no model selected and a knowledge base cannot
# index at all (the `{"data":null}` the #1248 day-1 tests read back).
#
# Model types are the ModelType enum values (llm / text-embedding / rerank);
# Dify's ModelType.value_of also accepts the legacy origin names, which is why
# the GPUStack path's 'text-generation' works.
#
# Raw output is published in _DIFY_DEF_LAST_OUTPUT so the caller can assert that
# each default it asked for was really selected.
_dify_set_default_models() {
    local admin_email="$1" provider="$2" pairs="$3"
    _DIFY_DEF_LAST_OUTPUT=""
    [ -n "$pairs" ] || return 0
    local _dify_def_py="
import sys
sys.path.insert(0, '/app/api')
from app import create_app
from extensions.ext_database import db
from models.account import Account, TenantAccountJoin
from services.model_provider_service import ModelProviderService
_, app = create_app()
with app.app_context():
    account = db.session.query(Account).filter(Account.email == sys.argv[1]).first()
    if not account:
        print('Admin account not found'); sys.exit(1)
    tj = db.session.query(TenantAccountJoin).filter(TenantAccountJoin.account_id == account.id).first()
    if not tj:
        print('Tenant not found'); sys.exit(1)
    mps = ModelProviderService()
    for pair in sys.argv[3].split(';'):
        if not pair or '=' not in pair: continue
        mtype, mname = pair.split('=', 1)
        if not mtype or not mname: continue
        try:
            mps.update_default_model_of_model_type(
                tenant_id=tj.tenant_id, model_type=mtype,
                provider=sys.argv[2], model=mname)
            print(f'Default {mtype} set to {mname}.')
        except Exception as e:
            print(f'Default {mtype} error: {e}')
"
    _DIFY_DEF_LAST_OUTPUT=$(docker exec dify-api python3 -c "$_dify_def_py" \
        "$admin_email" "$provider" "$pairs" 2>/dev/null) || true
    printf '%s\n' "$_DIFY_DEF_LAST_OUTPUT" | while IFS= read -r line; do
        if [ -n "$line" ]; then print_substep "  $line"; fi
    done
    return 0
}

# _dify_align_account_timezone <admin_email>
# Dify hardcodes a new account to America/New_York, so the console renders
# workflow-run + log timestamps in EDT instead of local time. Idempotent nudge to
# the stack TZ. Extracted from dify_configure_model by #1248: the Manager branch
# replaces that whole function on a Manager box, and leaving the nudge inside it
# would have quietly un-fixed the timezone on exactly the boxes this release
# rewires.
_dify_align_account_timezone() {
    local admin_email="$1" tz="${TZ:-UTC}"
    [ -n "$admin_email" ] || return 0
    local _dify_tz_py="
import sys
sys.path.insert(0, '/app/api')
from app import create_app
from extensions.ext_database import db
from models.account import Account
_, app = create_app()
with app.app_context():
    account = db.session.query(Account).filter(Account.email == sys.argv[1]).first()
    if not account:
        print('Admin account not found'); sys.exit(1)
    if account.timezone != sys.argv[2]:
        account.timezone = sys.argv[2]
        db.session.commit()
        print(f'Account timezone set to {sys.argv[2]}.')
"
    local out
    out=$(docker exec dify-api python3 -c "$_dify_tz_py" "$admin_email" "$tz" 2>/dev/null) || true
    if [ -n "$out" ]; then print_substep "  $out"; fi
    return 0
}

# #690: RAG consumers (cognee, lightrag) hard-pinned gpustack:9090 — on an
# llm-manager box nothing listens there and cognee's GRAPH_COMPLETION sat in
# litellm retry backoff (every /search a timeout, seen live on 0.91). A
# SEPARATE function (not a tail on wire_llm_manager_consumers): that one
# early-returns inside its Dify branch, which would silently skip anything
# appended after it.
# The canonical in-network LLM endpoint (#979 E1): the manager, port 8080.
LLM_CANONICAL_ENDPOINT="http://llm:8080/v1"

# #1445 (cutover C5): one table for every consumer post-install wires to the
# canonical endpoint http://llm:8080/v1 (#979) with a cost-centred service key
# (stack/<svc>). Columns: svc | profile | env prefix | endpoint keys | key keys.
# The compose defaults already say the canonical URL and an EMPTY key — this
# is what fills the key in (and re-points a box that carried GPUStack-era
# values). lightrag's rerank rides the manager too (#976 proxies /v1/rerank).
RAZZFAZZ_LLM_CONSUMERS="cognee|cognee|COGNEE|LLM_ENDPOINT,EMBEDDING_ENDPOINT|LLM_API_KEY,EMBEDDING_API_KEY
lightrag|lightrag|LIGHTRAG|LLM_ENDPOINT,EMBEDDING_ENDPOINT,RERANK_ENDPOINT|LLM_API_KEY,EMBEDDING_API_KEY,RERANK_API_KEY
openhands|openhands|OPENHANDS|LLM_ENDPOINT|LLM_API_KEY
paperclip|paperclip|PAPERCLIP|LLM_ENDPOINT|LLM_API_KEY
ollama-proxy|llm-manager|OLLAMA_PROXY||LLM_API_KEY"

# #1507 — the consumers must end up with the set the box actually SERVES.
#
# post-install deploys the model set, waits a bounded window, and only then
# configures Dify (provider + defaults) and OWUI (model-sync). On a CPU box the
# chat model routinely outlasts that window: Round 7 on 0.175 ended the wait at
# `3/4 ready after 540s — waiting on: qwen3.6(pulling)`, the two consumer steps
# enumerated the three models that WERE ready, and nothing ever re-read the
# list. Twenty minutes later /api/deployments and /v1/models both served all
# four while Open WebUI's model list lacked the chat model and Dify had no
# default model at all — a customer-visible dead end on a healthy box (the
# #1248 family, by timing rather than by name).
#
# This is deliberately NOT another wait inside the deploy: the deploy's window
# is about "did the box come up", and stretching it delays every install. Here
# the expensive work is done and only the enumeration is stale, so a second,
# clearly-labelled pass is cheap and honest — and if the model still is not
# ready, the operator gets the one command that fixes it.
# _llmm_all_absent <pending-list> — true when EVERY entry is `name(absent)`,
# i.e. the manager knows none of the wanted models. _llmm_ready_report writes
# `absent` only for a name with no deployment row at all.
_llmm_all_absent() {
    local list="$1" entry rc=0
    [ -n "$list" ] || return 1
    # No pipeline: a `while read` on the right-hand side would run in a
    # subshell AND drop the last entry (no trailing newline), which is exactly
    # how the first cut of this predicate said "all absent" for a single
    # pending model. Split on commas in this shell instead.
    local _oldifs="$IFS"
    IFS=','
    # shellcheck disable=SC2086  # deliberate word split on the comma list
    for entry in $list; do
        case "$entry" in
            *"(absent)") ;;
            *) rc=1 ;;
        esac
    done
    IFS="$_oldifs"
    return "$rc"
}

LLM_MANAGER_LATE_READY_TIMEOUT="${LLM_MANAGER_LATE_READY_TIMEOUT:-900}"
LLM_MANAGER_LATE_READY_POLL="${LLM_MANAGER_LATE_READY_POLL:-15}"

llm_manager_reconcile_consumers_late() {
    [ "${_LLMM_DEPLOY_INCOMPLETE:-0}" = "1" ] || return 0
    _llm_manager_active || return 0
    # The --refresh arm deploys the `standard` set with no PRESET set at all
    # (--preset and --refresh are mutually exclusive), so the fallback here is
    # the same literal that arm passes to llm_manager_deploy_standard_set.
    local preset="${PRESET:-standard}"
    local waited=0 pending
    print_step "Model deploy was incomplete — waiting for the slow model before re-wiring the consumers (#1507)..."
    while [ "$waited" -lt "$LLM_MANAGER_LATE_READY_TIMEOUT" ]; do
        pending=$(_llmm_pending_standard_models "$preset")
        if [ "$pending" = "?" ]; then
            # No verdict (manifest or manager unreadable) — never silently
            # claim readiness; nothing is re-wired on a guess, and the tail's
            # "still waiting on ?" would be noise, so leave here.
            print_warning "Cannot read the deployment state — leaving the consumer wiring as it is."
            return 0
        fi
        # Every wanted model absent from the manager's deployment list is not
        # "a slow pull" — it is a box whose served set does not live there at
        # all (a GPUStack/federated backend, or a deploy the manager refused
        # outright). Waiting the full window would buy nothing and delay the
        # install; say what to run instead. Only checked on the FIRST reading:
        # once something has appeared, a later all-absent reading would be a
        # deployment being deleted under us, which the tail reports.
        # #1760: the manager has already said it cannot place these — waiting
        # is the wrong answer to "impossible". Checked every pass, not only the
        # first: a row can lose its weight source between readings (a redeploy
        # that failed), and the operator should hear it then, not 15 minutes on.
        if _llmm_all_pending_are_unplaceable "$pending" "$(_llmm_unplaceable_standard_models "$preset")"; then
            print_warning "Not waiting: the manager cannot place ${pending} — no weight source is recorded."
            print_info "  Re-deploy them from the LLM Manager console (Catalog), then run:"
            print_info "      rzfz post-install --refresh"
            print_info "  Dify and Open WebUI keep the previous model set until then (#1507/#1760)."
            return 0
        fi
        if [ "$waited" -eq 0 ] && _llmm_all_absent "$pending"; then
            print_warning "The LLM Manager's deployment list carries none of the preset's models — not waiting for them here."
            print_info "  Once the models are served, run 'rzfz post-install --refresh' to re-register Dify's provider + defaults and re-sync Open WebUI's model list (#1507)."
            return 0
        fi
        if [ -z "$pending" ]; then
            print_success "All deployments ready after ${waited}s — re-wiring Dify + Open WebUI with the full model set."
            # Both are idempotent; they are the two steps that ENUMERATE the
            # served set, so they are exactly the two that went stale.
            if echo "${COMPOSE_PROFILES:-}" | grep -qw "dify"; then
                # #2195: the earlier pass may have recorded WARN for a Dify that
                # had no served models yet. It has them now, and the end-of-run
                # summary must say so rather than report the state it was
                # repaired from.
                if step_dify_provisioning; then
                    record_step_status dify OK
                else
                    print_warning "Dify re-wiring returned non-zero — check 'rzfz post-install --verify'."
                fi
            fi
            recreate_active_model_sync
            # #2195 rev-B (journey A-prime, 0.91): the model ROWS are a third
            # enumerating step, and it was missing here. It had run once, early,
            # while only the reranker was served, so it wrote exactly one row —
            # and `qwen3-embedding` and `granite-docling` were then offered in
            # the chat picker, against the stated intent of the line that writes
            # them. `recreate_active_model_sync` restarts a container; it does
            # not rewrite the hide-list. The function self-guards on ownership,
            # the manager, postgres and a non-empty served set, so this is
            # idempotent on every box that does not need it.
            owui_reconcile_model_rows || true
            return 0
        fi
        sleep "$LLM_MANAGER_LATE_READY_POLL"
        waited=$((waited + LLM_MANAGER_LATE_READY_POLL))
    done
    print_warning "Still waiting on ${pending} after ${waited}s — Open WebUI and Dify carry an INCOMPLETE model set."
    print_info "  Once the deployment is ready (rzfz llm status / the console), run:"
    print_info "      rzfz post-install --refresh"
    print_info "  That re-registers Dify's provider + defaults and re-syncs OWUI's model list (#1507)."
    return 0
}

wire_llm_manager_rag_consumers() {
    # Same two hand-copied predicates as in wire_llm_manager_consumers above,
    # same reason for replacing them.
    _llm_manager_profile_active || return 0
    _llm_manager_running || return 0
    local row svc profile env_prefix ep_keys key_keys key_var key changed ek kk want
    while IFS='|' read -r svc profile env_prefix ep_keys key_keys; do
        [ -n "$svc" ] || continue
        echo "${COMPOSE_PROFILES:-}" | grep -qw "$profile" || continue
        key_var="LLM_MANAGER_$(printf '%s' "$env_prefix")_KEY"
        key=$(read_env_value "$ENV_FILE" "$key_var" 2>/dev/null || true)
        if [ -z "$key" ]; then
            key=$(_llm_manager_mint_service_key "$svc") || key=""
            if [ -n "$key" ]; then
                update_env_value "$ENV_FILE" "$key_var" "$key"
                print_substep "Minted stack/$svc service key."
            else
                print_warning "Could not mint the stack/$svc service key — $svc keeps its current .env values."
                continue
            fi
        fi
        changed=0
        for ek in $(printf '%s' "$ep_keys" | tr ',' ' '); do
            want="$LLM_CANONICAL_ENDPOINT"
            case "$ek" in *RERANK*) want="${LLM_CANONICAL_ENDPOINT}/rerank" ;; esac
            if [ "$(read_env_value "$ENV_FILE" "${env_prefix}_${ek}" 2>/dev/null || true)" != "$want" ]; then
                update_env_value "$ENV_FILE" "${env_prefix}_${ek}" "$want"; changed=1
            fi
        done
        for kk in $(printf '%s' "$key_keys" | tr ',' ' '); do
            if [ "$(read_env_value "$ENV_FILE" "${env_prefix}_${kk}" 2>/dev/null || true)" != "$key" ]; then
                update_env_value "$ENV_FILE" "${env_prefix}_${kk}" "$key"; changed=1
            fi
        done
        if [ "$changed" = "1" ]; then
            # recreate only a RUNNING consumer so it re-reads the env
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$svc"; then
                docker compose up -d --no-deps --force-recreate "$svc" >/dev/null 2>&1 || true
                print_substep "$svc now talks to the LLM Manager at ${LLM_CANONICAL_ENDPOINT} (recreated)."
            else
                print_substep "$svc wired to the LLM Manager at ${LLM_CANONICAL_ENDPOINT} (takes effect on next start)."
            fi
        else
            print_substep "$svc already wired to the LLM Manager."
        fi
    done <<< "$RAZZFAZZ_LLM_CONSUMERS"
    return 0
}

# #240: enabling mac-llm never wired the gateway into OWUI's second OpenAI
# endpoint — `config/.env.example` claims the P2 Config Portal toggle writes
# `OWUI_OPENAI_BASE_URLS`/`OWUI_OPENAI_KEYS` automatically, but nothing did
# (apply_manager.py #206 only seeds config.yaml). Same shape as the #320
# llm-manager wiring above: reuse `_owui_append_endpoint` rather than
# reimplementing its append/idempotency logic. Dify half deferred (#240
# follow-up) — the manager's Dify path enumerates served CHAT models
# straight from its own DB rows (review #666); the Mac gateway is a plain
# LiteLLM proxy with no equivalent cheap enumeration, and half-registering
# Dify without a model list would be a broken provider entry.
wire_mac_llm_consumers() {
    echo "${COMPOSE_PROFILES:-}" | grep -qw "mac-llm" || return 0
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-mac-gateway; then
        print_warning "mac-llm profile active but llm-mac-gateway container not running — consumer wiring skipped (re-run 'rzfz post-install --refresh' once it is up)."
        return 0
    fi
    local mac_key
    mac_key=$(read_env_value "$ENV_FILE" MAC_GATEWAY_MASTER_KEY 2>/dev/null || true)
    if [ -z "$mac_key" ]; then
        print_warning "MAC_GATEWAY_MASTER_KEY is empty — Mac gateway endpoint not wired into OWUI (#240)."
        return 0
    fi
    print_step "Mac gateway: wiring OWUI consumer (#240)..."

    # #1235 rev-C: keep the ORIGINAL list for the write comparison below — the
    # strip rewrites the locals, so comparing against them would hide a strip
    # whenever the gateway is already listed (append no-op) and leave the dead
    # gpustack:9090 entry in .env for good. Same shape as the template above.
    local bases keys out orig_bases orig_keys
    orig_bases=$(read_env_value "$ENV_FILE" OWUI_OPENAI_BASE_URLS 2>/dev/null || true)
    orig_keys=$(read_env_value "$ENV_FILE" OWUI_OPENAI_KEYS 2>/dev/null || true)
    bases="$orig_bases"
    keys="$orig_keys"
    # #1235: the same #976 shape as owui_configure_connection above. This used
    # to default an EMPTY list to gpustack unconditionally, so a mac-llm box
    # without GPUStack (llm-manager front, or Mac-only) got the dead
    # gpustack:9090 entry back that #976 removed everywhere else — a queued
    # connection OWUI keeps polling, with a placeholder key. Seed gpustack only
    # while a gpustack profile is active; otherwise strip a stale entry and let
    # the append below make the gateway the sole endpoint.
    if ! _gpustack_profile_active; then
        local _stripped
        _stripped=$(_owui_strip_gpustack "$bases" "$keys")
        bases=$(printf '%s' "$_stripped" | sed -n 1p)
        keys=$(printf '%s' "$_stripped" | sed -n 2p)
    fi
    if [ -z "$bases" ] && _gpustack_profile_active; then
        bases="http://gpustack:9090/v1-openai"
        keys="${keys:-$GPUSTACK_API_KEY}"
    fi
    out=$(_owui_append_endpoint "$bases" "$keys" "http://llm-mac-gateway:4000/v1" "$mac_key")
    local new_bases new_keys
    new_bases=$(printf '%s' "$out" | sed -n 1p)
    new_keys=$(printf '%s' "$out" | sed -n 2p)
    # #1185 class: an already-listed gateway entry keeps its OLD key through
    # the append (no-op) — pin the entry to the current master key so a
    # rotated MAC_GATEWAY_MASTER_KEY reaches OWUI.
    new_keys=$(_owui_set_endpoint_key "$new_bases" "$new_keys" "http://llm-mac-gateway:4000/v1" "$mac_key")
    if [ "$new_bases" != "$orig_bases" ] || [ "$new_keys" != "$orig_keys" ]; then
        update_env_value "$ENV_FILE" "OWUI_OPENAI_BASE_URLS" "$new_bases"
        update_env_value "$ENV_FILE" "OWUI_OPENAI_KEYS" "$new_keys"
        print_substep "OWUI endpoint list now carries the Mac gateway (models auto-discovered via /v1/models)."
        docker compose up -d --force-recreate openwebui >/dev/null 2>&1 || true
    else
        print_substep "OWUI already wired to the Mac gateway."
    fi
    print_success "Mac gateway consumer wiring done."
}

# #813 (split out of #240): the DIFY half of the Mac-gateway wiring.
#
# OWUI needs only an endpoint — it discovers models itself from /v1/models. Dify
# needs CONCRETE chat models, and review #666 settled how they may be found:
# /v1/models proxies the router list WITHOUT a task filter, so enumerating from
# it and guessing by name-shape registers embed/rerank models as mode=chat and
# leaves broken provider entries behind. The manager side avoids that by
# enumerating its own DB rows with `Deployment.task == "chat"`; the gateway is a
# plain LiteLLM proxy with no such table, so the task is DECLARED in its
# config.yaml (`model_info.mode`, #813) and read back through the generator that
# writes it — `gen_config.py --chat-models`, which stays the single source of
# truth instead of the shell re-deriving the rule in yq/grep.
#
# A SEPARATE function from wire_mac_llm_consumers, not a tail on it — the #690
# lesson, stated a few functions above: that one early-returns on an empty
# MAC_GATEWAY_MASTER_KEY, and anything appended after such a return is silently
# skipped on exactly the boxes that most need the diagnostic.
wire_mac_llm_dify_consumer() {
    echo "${COMPOSE_PROFILES:-}" | grep -qw "mac-llm" || return 0
    echo "${COMPOSE_PROFILES:-}" | grep -qw "dify" || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-mac-gateway || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx dify-api || return 0

    local mac_key
    mac_key=$(read_env_value "$ENV_FILE" MAC_GATEWAY_MASTER_KEY 2>/dev/null || true)
    if [ -z "$mac_key" ]; then
        print_warning "MAC_GATEWAY_MASTER_KEY is empty — Mac gateway chat models not registered with Dify (#813)."
        return 0
    fi

    local gw_dir="${SCRIPT_DIR}/modules/llm/mac-gateway"
    if [ ! -f "${gw_dir}/config.yaml" ]; then
        # config.yaml is per-box and gitignored; the gateway cannot even start
        # without it, so this is the "operator has not declared any Mac yet"
        # case, not an error.
        print_warning "${gw_dir}/config.yaml not found — no Mac chat models to register with Dify (#813)."
        return 0
    fi

    print_step "Mac gateway: registering chat models with Dify (#813)..."
    local served
    served=$(python3 "${gw_dir}/gen_config.py" --chat-models < "${gw_dir}/config.yaml" 2>/dev/null) || served=""
    if [ -z "$served" ]; then
        # Empty stdout with exit 0 is the generator's documented "none yet";
        # a broken call exits non-zero and lands here too, which is why this
        # says "deferred" rather than claiming there are none.
        print_substep "Gateway config declares no chat models — Dify registration deferred to the next --refresh."
        return 0
    fi

    local admin_email
    admin_email=$(_resolve_dify_admin_email)
    # 32768 mirrors the LLM-Manager path's declared value. There is nothing to
    # discover it from: Ollama's OpenAI-compatible /v1/models reports no context
    # length, so this is a declared default an operator can correct in Dify.
    # #1959: the call site the defect was MEASURED on. `Admin account not found`
    # printed as a substep between two progress lines, and this function said
    # "done" underneath it with rc 0.
    if _dify_register_models "$admin_email" "$mac_key" "http://llm-mac-gateway:4000/v1" \
            "Mac LLM Gateway" "32768" "$(printf '%s' "$served" | tr '\n' ';')"; then
        print_success "Mac gateway Dify registration done (${_DIFY_REG_OK}/${_DIFY_REG_TOTAL} chat models registered)."
    elif [ "${_DIFY_REG_OK:-0}" -eq 0 ]; then
        print_error "Mac gateway Dify registration registered NONE of the ${_DIFY_REG_TOTAL} chat models — the lines above say why (#1959)."
        return 1
    else
        print_warning "Mac gateway Dify registration incomplete: ${_DIFY_REG_OK} of ${_DIFY_REG_TOTAL} chat models registered — the lines above say why (#1959)."
        return 1
    fi
}

ensure_gpustack_api_key() {
    print_step "GPUStack: Ensuring API key exists..."
    
    # Disable errexit for this function — we handle errors ourselves
    set +e
    
    if [ -n "$GPUSTACK_API_KEY" ]; then
        # Verify the existing key works
        local resp
        resp=$(gpustack_api GET "/v1-openai/models" 2>/dev/null)
        if echo "$resp" | python3 -c "import sys,json; json.load(sys.stdin)['data']" > /dev/null 2>&1; then
            print_success "Existing API key is valid."
            _resolve_owui_openai_keys
            set -e
            return 0
        fi
        print_warning "Existing API key is invalid. Creating new one..."
    fi
    
    # Create new API key via basic auth
    print_substep "Creating GPUStack API key..."
    local admin_pass="${GPUSTACK_ADMIN_PASSWORD:-${AUTHENTIK_BOOTSTRAP_PASSWORD}}"
    if [ -z "$admin_pass" ]; then
        print_error "No admin password found (GPUSTACK_ADMIN_PASSWORD or AUTHENTIK_BOOTSTRAP_PASSWORD)."
        set -e
        return 1
    fi
    
    # Force basic auth (clear API key temporarily)
    local saved_key="$GPUSTACK_API_KEY"
    GPUSTACK_API_KEY=""

    # The api-keys path (one runtime since #1447, resolved centrally)
    local prefix
    prefix=$(gpustack_api_prefix)

    # Delete existing key if present (can't retrieve value of existing keys)
    local existing_id
    existing_id=$(gpustack_api GET "${prefix}/api-keys" | python3 -c "
import sys, json
try:
    for k in json.load(sys.stdin).get('items', []):
        if k.get('name') == 'razzfazz-post-install':
            print(k['id']); break
except: pass
" 2>/dev/null)

    if [ -n "$existing_id" ]; then
        print_info "Removing old key (id=$existing_id)..."
        gpustack_api DELETE "${prefix}/api-keys/$existing_id" > /dev/null 2>&1
        sleep 1
    fi

    # Create fresh key
    local key_resp
    key_resp=$(gpustack_api POST "${prefix}/api-keys" '{"name":"razzfazz-post-install"}')
    
    local new_key
    new_key=$(echo "$key_resp" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('value', ''))
except: print('')
" 2>/dev/null)
    
    GPUSTACK_API_KEY="$saved_key"
    
    if [ -z "$new_key" ] || [ "$new_key" = "None" ]; then
        print_error "Failed to create API key."
        print_info "Response: $(echo "$key_resp" | head -c 300)"
        set -e
        return 1
    fi
    
    GPUSTACK_API_KEY="$new_key"
    update_env_value "$ENV_FILE" "GPUSTACK_API_KEY" "$new_key"
    export GPUSTACK_API_KEY="$new_key"
    _resolve_owui_openai_keys
    print_success "API key created and saved to .env."
    set -e
}

# ── #184 P1 / WS7b — offline local-GGUF helpers ──────────────────────────────
# The gpustack-data volume mounts at /var/lib/gpustack in the gpustack container;
# OFFLINE-sideloaded GGUFs live under $RAZZFAZZ_LOCAL_MODELS_DIR there. When the
# box is offline, models are registered with source=local_path pointing at that
# path (never huggingface.co) — see core/llm/model_source.py.

# _local_model_path <huggingface_filename>: print the in-container GGUF path.
_local_model_path() {
    printf '%s/%s\n' "$RAZZFAZZ_LOCAL_MODELS_DIR" "${1#/}"
}

# _local_gguf_present <huggingface_filename>: return 0 if at least one GGUF
# matching <filename> (which may be a glob or carry a sub-dir) exists under
# $RAZZFAZZ_LOCAL_MODELS_DIR INSIDE the running gpustack container. The check
# runs there, not on the host, because the volume is a docker named volume.
_local_gguf_present() {
    local filename="$1" path
    path="$(_local_model_path "$filename")"
    docker exec gpustack sh -c "ls -1 ${path} 2>/dev/null | head -n1 | grep -q ." 2>/dev/null
}

# ── Appliance offline models: stage baked GGUFs into the gpustack-data volume ──
# The appliance ISO (build-appliance-usb.sh --offline-package) bakes the offline
# package — INCLUDING the model GGUFs (models/ subtree; that's what makes the ISO
# tens-of-GB) — onto the box at $APPLIANCE_OFFLINE_PKG. Firstboot leaves the package
# untouched; `rzfz init` loads its images/ before compose-up. Models are NOT staged
# earlier (firstboot / init pre-compose) because pre-creating the gpustack-data
# volume would trip razzfazz-init.sh's existing-install guard. So we stage them
# HERE, after the stack is up, into
# $RAZZFAZZ_LOCAL_MODELS_DIR via `docker cp` into the running gpustack — mirroring
# the offline `rzfz upgrade --package` path (cli/upgrade.sh WS7a). deploy_all_models
# then registers them source=local_path with ZERO downloads. Idempotent (skips when
# the volume is already populated), non-fatal, and harmless online (the GGUFs just
# sit in the volume until an offline deploy references them).
APPLIANCE_OFFLINE_PKG="${APPLIANCE_OFFLINE_PKG:-/opt/razzfazz-appliance/razzfazz-offline.tar.gz}"

stage_baked_appliance_models() {
    [ -f "$APPLIANCE_OFFLINE_PKG" ] || return 0
    # #2227: the runtime is whichever models container runs on THIS box
    # (gpustack on a legacy box, llm-worker-agent on an LLM-Manager box).
    if [ -z "$(razzfazz_models_runtime)" ]; then
        print_warning "Baked appliance package present but neither gpustack nor llm-worker-agent is running — skipping model staging (re-run 'rzfz post-install' once the stack is up)."
        return 0
    fi
    # Idempotent: a populated models volume means we already staged.
    # (Checked BEFORE the expensive extract so re-runs are instant.)
    if razzfazz_models_volume_populated; then
        print_info "Baked model GGUFs already present in the box's models volume — skipping staging."
        return 0
    fi
    print_step "Appliance: staging baked model GGUFs into ${RAZZFAZZ_LOCAL_MODELS_DIR} (offline, zero-download)..."
    # Transient-space warning (extract needs ~the models/ subtree size in /var/tmp;
    # the package size is a conservative upper bound). Non-fatal — let it try.
    local avail_kb pkg_kb
    avail_kb=$(df -Pk /var/tmp 2>/dev/null | awk 'NR==2{print $4}')
    pkg_kb=$(du -k "$APPLIANCE_OFFLINE_PKG" 2>/dev/null | cut -f1)
    if [ -n "$avail_kb" ] && [ -n "$pkg_kb" ] && [ "$avail_kb" -lt "$pkg_kb" ]; then
        print_warning "Low free space in /var/tmp ($((avail_kb/1024/1024)) GiB) for extracting the baked models (~up to $((pkg_kb/1024/1024)) GiB) — staging may fail; free space or stage manually."
    fi
    local tmp
    tmp="$(mktemp -d /var/tmp/rzfz-appliance-models.XXXXXX)" || { print_warning "mktemp failed — skipping model staging."; return 0; }
    # ONE scan of the archive extracts just the models/ subtree (members can be
    # anywhere in the tar, so tar reads the whole file — a one-time install cost).
    # #428: index consulted first — the old two-attempt extract was the
    # identical full 63 GB scan run twice (tar normalizes ./, so attempt two
    # could never succeed where one failed), ~10 min per post-install run.
    local _idx _prefix
    _idx="$(appliance_pkg_index "$APPLIANCE_OFFLINE_PKG")" || _idx=""
    if [ -n "$_idx" ] && ! _prefix="$(appliance_pkg_prefix "$_idx" models)"; then
        print_info "Baked appliance package carries no models/ subtree (index consulted — no scan wasted). An air-gapped box gets ZERO models from this stick (#426)."
        rm -rf "$tmp"
        return 0
    fi
    if [ -n "$_idx" ] && tar xzf "$APPLIANCE_OFFLINE_PKG" -C "$tmp" "$_prefix" 2>/dev/null; then
        # #782: same physical medium, same risk #755 closed for the upgrade
        # package. `docker cp` copies a symlink through as a symlink, so a
        # prepared models/ subtree would plant one inside the gpustack volume.
        #
        # Operator decision 2026-08-26: DISCARD the payload, do not abort — the
        # step is best-effort and an installation must not be stoppable by a
        # prepared stick. Reported separately from "no models/ subtree", so the
        # operator is not told a poisoned package was merely an images-only one.
        # Tri-state + `set -eo pipefail`: a bare assignment inherits the CLEAN
        # status (1) and would abort here. Same regression as #755/#793.
        _bad=""; _link_rc=0
        _bad="$(razzfazz_find_escaping_links "$tmp")" || _link_rc=$?
        if [ -n "$_bad" ] || [ "$_link_rc" -eq 2 ]; then
            [ -n "$_bad" ] || _bad="(could not inspect ${tmp})"
            print_warning "Baked appliance package contains symlinks pointing OUTSIDE the package — NOT staging its models (#782):"
            printf '%s\n' "$_bad" | sed "s|^${tmp}/|    |" >&2
            print_warning "  The package is not trustworthy; do not reuse this medium. Offline model deploy will report missing GGUFs."
            rm -rf "${tmp:?}/models" 2>/dev/null || true
            rm -rf "$tmp"
            return 0
        fi
        if ls "$tmp"/models/* >/dev/null 2>&1; then
            if razzfazz_stage_package_models "$tmp/models"; then
                print_success "Baked model GGUFs staged into the box's models volume (#2227)."
            fi
        else
            print_info "Baked appliance package carries no models/ subtree (images-only package) — nothing to stage."
        fi
    else
        print_warning "Could not extract models/ from ${APPLIANCE_OFFLINE_PKG} — offline model deploy may report missing GGUFs. Re-check with 'rzfz verify-models'."
    fi
    rm -rf "$tmp"
}

deploy_model() {
    local name="$1" repo="$2" filename="$3" category="$4"
    shift 4
    local backend_params="$1" extra_json="${2:-}" mmproj_filename="${3:-}"

    # #1447 (cutover C7a): one runtime, one schema. The dual-track branching
    # (0.7.x legacy vs 2.x experimental) is gone with the `llm` profile. A box
    # still answering 2.x is refused here, before the first POST — see
    # _refuse_gpustack_2x for why one refusal beats a per-model 404.
    _refuse_gpustack_2x || return 1
    local prefix
    prefix=$(gpustack_api_prefix)

    # Check if model already exists
    local existing
    existing=$(gpustack_api GET "${prefix}/models" | \
        python3 -c "
import sys,json
d=json.load(sys.stdin)
for m in d.get('items',[]):
    if m['name'] == '$name':
        print(m['id'])
        break
else:
    print('')
" 2>/dev/null)

    if [ -n "$existing" ]; then
        print_info "Model '$name' already exists (id=$existing). Skipping deployment."
        return 0
    fi

    # #184 P1 / WS7b — offline: register from the local GGUF (source=local_path),
    # never huggingface.co. Assert the sideloaded GGUF is present first so a
    # missing bundle fails CLEAR (pointing at verify-models) instead of GPUStack
    # silently trying to reach the internet. Online/proxied keeps source=huggingface.
    local _off="0"
    if razzfazz_is_offline; then
        _off="1"
        if ! _local_gguf_present "$filename"; then
            print_error "Offline: GGUF for '$name' not found at $(_local_model_path "$filename") in the gpustack-data volume."
            print_info  "  Bundle it with 'rzfz package --include-models' (dev box) or sideload via settings.<domain> → LLM/Models, then re-run. Check with 'rzfz verify-models'."
            return 1
        fi
    fi

    # Build backend_parameters JSON array (same on both runtimes)
    local bp_json="[]"
    if [ -n "$backend_params" ]; then
        bp_json=$(echo "$backend_params" | python3 -c "
import sys,json
params = sys.stdin.read().strip().split()
print(json.dumps(params))
" 2>/dev/null)
    fi

    # #138: vision mmproj sidecar. Offline the HF cache path in --mmproj (or
    # GPUStack's HF auto-detect) can never work — rewrite/add --mmproj to the
    # sideloaded local-models/<repo>/ copy. If the sidecar wasn't bundled,
    # STRIP the param instead: llama-box aborts on a nonexistent --mmproj
    # path, and a text-only model beats no model. Online is a no-op.
    if [ -n "$mmproj_filename" ]; then
        local mmproj_mode="online"
        if [ "$_off" = "1" ]; then
            if _local_gguf_present "${repo}/${mmproj_filename}"; then
                mmproj_mode="offline"
            else
                mmproj_mode="strip"
                print_warning "Offline: vision sidecar for '$name' not found at $(_local_model_path "${repo}/${mmproj_filename}") — deploying TEXT-ONLY. Bundle it with 'rzfz package --include-models' and re-run."
            fi
        fi
        local _bp_new
        _bp_new=$(RZFZ_BP="$bp_json" RZFZ_REPO="$repo" RZFZ_MMPROJ="$mmproj_filename" \
            RZFZ_MODE="$mmproj_mode" LLM_LIB_DIR="$SCRIPT_DIR/core/llm" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ['LLM_LIB_DIR'])
import model_source
print(json.dumps(model_source.apply_mmproj(
    json.loads(os.environ['RZFZ_BP']), os.environ['RZFZ_REPO'],
    os.environ['RZFZ_MMPROJ'], mode=os.environ['RZFZ_MODE'])))
" 2>/dev/null) && [ -n "$_bp_new" ] && bp_json="$_bp_new" || true  # keep prior params if the rewrite helper fails (set -e)
    fi

    # #2146: cpu_offloading follows the box. A CPU-only worker (HARDWARE=cpu:
    # the gpustack cpu image, no /dev/kfd, no /dev/dri) has nothing but CPU to
    # place a model on; a hardcoded False told GPUStack the model may not be
    # offloaded to CPU on a worker that has only CPU, so EVERY model was
    # unplaceable by construction ("Cannot find a suitable worker
    # combination"), a 0.98 GiB embedding model included — measured on 0.79
    # (ga.15 baseline, 30 GB RAM, 3.1 TB disk). A GPU box keeps False: models
    # stay on the GPU, and the kworker-storm reasoning below still holds.
    local _cpu_offload="False"
    if [ "${HARDWARE:-}" = cpu ]; then _cpu_offload="True"; fi

    local payload
    # Payload for GPUStack 0.7.x: no cluster_id, no backend name — 0.7.x has a
    # single bundled llama-box and routes by category alone. backend_parameters
    # still carry the model tunings (ctx-size, parallel, flash-attn, cache
    # types). The 2.x arm that stood beside this one is gone with #1447.
    # Legacy v0.7.x: simpler schema. No cluster_id, no backend name —
    # 0.7.x has a single bundled llama-box and uses categories alone for
    # routing. Still passes backend_parameters so model tunings (ctx-size,
    # parallel, flash-attn, cache types) carry over.
    print_substep "Deploying model '$name' from $repo (runtime=0.7.x, legacy schema)..."
    payload=$(LLM_LIB_DIR="$SCRIPT_DIR/core/llm" RZFZ_OFFLINE="$_off" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ['LLM_LIB_DIR'])
import model_source
model = {
    'name': '$name',
    'categories': ['$category'],
    'replicas': 1,
    'backend_parameters': $bp_json,
    'cpu_offloading': $_cpu_offload,
    'placement_strategy': 'spread',
    # restart_on_error=False: when a worker briefly drops out of master's
    # heartbeat (e.g. during the Strix Halo amdgpu kworker storm at model
    # load), master would otherwise re-spawn the model on another worker,
    # which triggers ANOTHER load storm there → cascade. With False the
    # existing instance stays placed; once the worker's heartbeat catches
    # up, the same instance comes back ready. See memory
    # project_strix_halo_kworker_storm.md.
    'restart_on_error': False,
}
# #184 P1 / WS7b: online → source=huggingface (+ repo/filename); offline →
# source=local_path pointing at the sideloaded GGUF. One place decides the split.
model.update(model_source.model_source_fields('$filename', '$repo', offline=os.environ.get('RZFZ_OFFLINE') == '1'))
extra = '$extra_json'
if extra:
    model.update(json.loads(extra))
print(json.dumps(model))
" 2>/dev/null)
    local resp
    resp=$(gpustack_api POST "${prefix}/models" "$payload")

    local model_id
    model_id=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)

    if [ -z "$model_id" ]; then
        print_error "Failed to deploy model '$name'."
        print_info "Response: $(echo "$resp" | head -c 300)"
        return 1
    fi

    # 0.7.x auto-exposes a deployed model on /v1-openai — no explicit route,
    # which is what the removed 2.x arm had to create by hand (rc6.7 #45).
    print_success "Model '$name' deployed (id=$model_id). Download starting..."
}

# M033 S19: single source-of-truth model enumerator. Emits one TAB-separated
# row per model that ships under the given preset + active profiles, read from
# core/llm/standard-models.yaml. Columns: alias  repo  filename  category  bp.
# EVERY consumer (deploy_all_models / wait_for_all_models / Dify registration /
# update_model post-deploy config) reads THIS, so no site can hardcode-drift
# from the YAML again — the qwen3.5-vs-qwen3.6 class of bug (S19) that hung the
# post-install wait-loop on a model name that doesn't exist.
#
# Preset filter:
#   - models with `presets: [standard, developer]` (or empty) ship to both
#   - `presets: [standard]` ships only under standard, etc.
#   - `requires_profile: docling` ships only when the docling profile is active
# Each field that could contain shell metacharacters (the backend_parameters
# joined string, the filename) is just data passed straight through.
_model_rows_for_preset() {
    local preset="$1" hw
    local active_profiles="${COMPOSE_PROFILES:-}"
    # #2158: the box's hardware class decides which entries are always-on here
    # (the GPUStack branch and the consumer registrations run on the box's own
    # worker); the manager-native deploy uses the worker the manager reports.
    hw=$(read_env_value "$ENV_FILE" HARDWARE 2>/dev/null) || hw=""
    PRESET="$preset" PROFILES="$active_profiles" HW="$hw" YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 - <<'PYEOF'
import os, sys
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not available — install python3-yaml or adjust the script.", file=sys.stderr)
    sys.exit(2)
sys.path.insert(0, os.path.dirname(os.environ["YAML_PATH"]))
import hardware_catalog as hc   # #2158: the one hardware rule

preset = os.environ["PRESET"]
profiles = set((os.environ.get("PROFILES", "") or "").split(","))
yaml_path = os.environ["YAML_PATH"]

with open(yaml_path) as f:
    spec = yaml.safe_load(f) or {}

# Map YAML role(s) → GPUStack category. Models can declare multiple roles;
# pick the first one that maps to a known GPUStack category.
ROLE_TO_CATEGORY = {
    "embedding": "embedding",
    "reranker": "reranker",
    # everything else (chat / coding / general / vision-* etc.) goes to llm
}

def category_for(roles):
    for r in roles:
        if r in ROLE_TO_CATEGORY:
            return ROLE_TO_CATEGORY[r]
    return "llm"

for alias, m in (spec.get("models") or {}).items():
    presets = m.get("presets") or ["standard", "developer"]
    if preset not in presets:
        continue
    needs_profile = m.get("requires_profile")
    if needs_profile and needs_profile not in profiles:
        continue
    repo = m.get("huggingface_repo_id", "")
    filename = m.get("huggingface_filename", "")
    if not (repo and filename):
        # Skip placeholder/aspirational models (no GGUF yet).
        continue
    bp = " ".join(m.get("backend_parameters") or [])
    roles = m.get("roles") or []
    cat = category_for(roles)
    # Column 6 = comma-joined roles. The GPUStack deploy *category* (col 4)
    # sends doc-conversion models like granite-docling to "llm", but the
    # Dify/OWUI chat-registration sites must key on the `chat` ROLE so they
    # don't register a non-chat model as a selectable chat LLM (S19).
    # Col 7 = auto_start ("true"/"false"). auto_start:false models are registered
    # then scaled to 0 replicas (deployed last, on-demand — ga.1 Issue I) so the
    # sole always-on default (qwen3.6 at ctx=1M par=4) keeps the VRAM and the box
    # is chat-functional without waiting on a multi-GB spare download.
    auto_start = str(hc.entry_auto_start(m, os.environ.get("HW"))).lower()
    # Col 8 = vision mmproj sidecar filename ('' for text-only models). #138:
    # deploy_model needs it to rewrite --mmproj at the local-models copy on
    # offline boxes. Shared derivation with the packaging enumerator.
    sys.path.insert(0, os.path.dirname(yaml_path))
    import expected_models as _em
    mmproj = _em.mmproj_filename(m)
    print("\t".join([alias, repo, filename, cat, bp, ",".join(roles), auto_start, mmproj]))
PYEOF
}

# scale_model_to_zero <name>: PUT the gpustack model with replicas=0 so it stays
# registered + its GGUF cached on disk, but no instance runs (0 VRAM). Best-effort:
# a failure leaves the model running (the pre-2026.06 behavior) and only warns.
scale_model_to_zero() {
    local name="$1" prefix mid payload
    prefix=$(gpustack_api_prefix)
    mid=$(gpustack_api GET "${prefix}/models" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for m in d.get('items',[]):
    if m.get('name')=='$name': print(m.get('id','')); break
" 2>/dev/null)
    [ -z "$mid" ] && { print_warning "scale-to-0: model '$name' not found (left as-is)."; return 0; }
    payload=$(gpustack_api GET "${prefix}/models/$mid" | python3 -c "
import sys,json
m=json.load(sys.stdin); m['replicas']=0
for k in ('id','created_at','updated_at','deleted_at'): m.pop(k,None)
print(json.dumps(m))
" 2>/dev/null)
    if [ -n "$payload" ] && gpustack_api PUT "${prefix}/models/$mid" "$payload" >/dev/null 2>&1; then
        print_substep "Model '$name' scaled to 0 replicas (downloaded, not running)."
    else
        print_warning "scale-to-0 for '$name' failed (best-effort — model left running; stop it from the GPUStack UI)."
    fi
}

# _model_exists <name>: true if a GPUStack model with this name is already
# registered. Used by deploy_all_models PASS 1 so a RE-RUN doesn't block an hour
# in wait_for_model on an auto_start:false model that was pre-downloaded + scaled
# to 0 in a prior run (no instance will ever appear at replicas=0 → 3600s timeout).
_model_exists() {
    local n="$1" prefix
    prefix=$(gpustack_api_prefix)
    gpustack_api GET "${prefix}/models" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('yes' if any(m.get('name')=='$n' for m in d.get('items',[])) else '')
" 2>/dev/null | grep -q yes
}

deploy_all_models() {
    local preset="$1"
    print_step "GPUStack: Deploying models (preset: $preset)..."

    # Appliance offline install: stage the baked model GGUFs into the gpustack-data
    # volume BEFORE deploy (so the source=local_path split below finds them). No-op
    # when there's no baked package (normal online installs). See the function.
    stage_baked_appliance_models

    # #184 P1 / WS7b: OFFLINE no longer skips model deployment wholesale (the
    # WS2b behaviour) — it registers from the GGUFs sideloaded into the
    # gpustack-data volume ($RAZZFAZZ_LOCAL_MODELS_DIR) via source=local_path
    # (deploy_model handles the payload split). GPUStack never reaches
    # huggingface.co. A model whose GGUF wasn't bundled is SKIPPED with a clear
    # pointer — not a 1 h wait-loop on a download that can't happen offline.
    # Online/proxied is unchanged (source=huggingface, GGUF pulled at load).
    local _offline=false
    if razzfazz_is_offline; then
        _offline=true
        print_info "Offline (RAZZFAZZ_NETWORK_MODE=offline): deploying models from local GGUFs in the gpustack-data volume (${RAZZFAZZ_LOCAL_MODELS_DIR}); models not bundled are skipped. Verify with 'rzfz verify-models'."
    fi

    # Model set read from standard-models.yaml via _model_rows_for_preset (S19).
    local model_list
    model_list=$(_model_rows_for_preset "$preset")

    if [ -z "$model_list" ]; then
        print_warning "No models to deploy for preset '$preset' — check standard-models.yaml."
        return 0
    fi

    # Two passes: PASS 1 (pass=true) deploys the ALWAYS-ON models (qwen3.6 default +
    # embedding + reranker) FIRST so the box becomes chat-functional as soon as
    # qwen3.6 is running — wait_for_all_models below blocks on exactly these. PASS 2
    # (pass=false) then registers the auto_start:false spares (gemma4, coder-next)
    # and scales them to 0 WITHOUT waiting on their download.
    #
    # ga.1 (Issue I) reorder rationale: the previous order (spares first — download
    # the 26.9 GB gemma4 + wait, then deploy qwen3.6) meant qwen3.6 wasn't even
    # submitted until the spare downloads finished → box unusable for ~1.5-2 h. The
    # spares are on-demand extras; they must not gate time-to-chat. Deploying them
    # last + scaling to 0 immediately (below) also avoids briefly loading a second
    # large model into VRAM alongside the running qwen3.6 (host-OOM on the
    # unified-memory Strix Halo — project_gemma4_swa_full_oom_trap), which the old
    # "spares while VRAM is free" ordering was working around.
    # Parse with `cut -fN` not `IFS=$'\t' read` (read collapses empty tab fields — S19).
    local pass
    for pass in true false; do
        while IFS= read -r _row; do
            [ -z "$_row" ] && continue
            local alias repo filename category backend_params auto_start mmproj
            alias=$(printf '%s' "$_row" | cut -f1)
            repo=$(printf '%s' "$_row" | cut -f2)
            filename=$(printf '%s' "$_row" | cut -f3)
            category=$(printf '%s' "$_row" | cut -f4)
            backend_params=$(printf '%s' "$_row" | cut -f5)
            auto_start=$(printf '%s' "$_row" | cut -f7)
            mmproj=$(printf '%s' "$_row" | cut -f8)
            [ "$auto_start" = "false" ] || auto_start=true   # default true when col absent
            [ -z "$alias" ] && continue
            [ "$auto_start" != "$pass" ] && continue
            # Detect prior existence BEFORE deploy purely for the log line below
            # (a re-run leaves the spare already registered + at 0 replicas).
            local _pre_exists=false
            if [ "$pass" = "false" ] && _model_exists "$alias"; then
                _pre_exists=true
            fi
            # #184 P1 / WS7b: offline, only deploy models whose GGUF is actually
            # sideloaded — skip the rest cleanly so wait_for_all_models doesn't
            # block on a download that can't run offline.
            if [ "$_offline" = true ] && ! _local_gguf_present "$filename"; then
                print_info "Offline: skipping '$alias' — no local GGUF at $(_local_model_path "$filename"). Bundle via 'rzfz package --include-models' or sideload; check 'rzfz verify-models'."
                continue
            fi
            deploy_model "$alias" "$repo" "$filename" "$category" "$backend_params" "" "$mmproj"
            if [ "$pass" = "false" ]; then
                # ga.1 (Issue I): NEVER block on a spare's download and NEVER let it
                # sit at replicas=1 (which would try to load it into VRAM next to the
                # running qwen3.6). Scale to 0 immediately so it's registered +
                # visible in the GPUStack UI; the GGUF downloads on demand the first
                # time an operator starts it from the UI. This is what removes the
                # ~1.5-2 h time-to-chat block. --skip-wait no longer changes spare
                # behaviour here (spares are always non-blocking now).
                if [ "$_pre_exists" = true ]; then
                    print_info "auto_start:false model '$alias' already present (prior run) — leaving at 0 replicas."
                else
                    print_info "auto_start:false model '$alias' registered (on-demand: not pre-downloaded; starts from the GPUStack UI)."
                fi
                scale_model_to_zero "$alias"
            fi
        done <<< "$model_list"
    done

    print_success "All models for preset '$preset' submitted (always-on deployed first; auto_start:false spares registered on-demand)."
}

wait_for_all_models() {
    local preset="$1"
    print_step "GPUStack: Waiting for all models to be ready..."
    print_info "This can take 30-60 minutes depending on download speeds."
    
    # S19: wait for exactly the models deploy_all_models submitted, read from
    # the same standard-models.yaml enumerator. No hardcoded list to drift —
    # this is the site that hung on the non-existent "qwen3.5" alias.
    # Each row: alias \t repo \t filename \t category \t bp \t roles
    local rows
    rows=$(_model_rows_for_preset "$preset")

    local failed_critical="" failed_optional=""
    while IFS= read -r _row; do
        [ -z "$_row" ] && continue
        local model roles auto_start filename
        model=$(printf '%s' "$_row" | cut -f1)
        filename=$(printf '%s' "$_row" | cut -f3)
        roles=$(printf '%s' "$_row" | cut -f6)
        auto_start=$(printf '%s' "$_row" | cut -f7)
        # auto_start:false models are registered + scaled to 0 in deploy_all_models
        # (on-demand, ga.1 Issue I) — don't wait for them to be "running" (they aren't).
        [ "$auto_start" = "false" ] && continue
        # #184 P1 / WS7b: offline, deploy_all_models only registers models whose
        # GGUF was bundled — don't wait on one that was skipped (it never appears).
        if razzfazz_is_offline && ! _local_gguf_present "$filename"; then
            print_info "Offline: not waiting on '$model' — its GGUF was not bundled (skipped at deploy). Check 'rzfz verify-models'."
            continue
        fi
        if ! wait_for_model "$model" 3600; then
            # chat + embedding are CRITICAL — the stack is unusable without
            # them. Everything else (reranker, etc.) is OPTIONAL and must not
            # abort the rest of provisioning (OWUI / Dify / Speaches config).
            if echo ",$roles," | grep -qE ',chat,|,embedding,'; then
                failed_critical="$failed_critical $model"
            else
                failed_optional="$failed_optional $model"
            fi
        fi
    done <<< "$rows"

    if [ -n "$failed_optional" ]; then
        print_warning "Optional model(s) failed to start:$failed_optional — continuing provisioning (stack stays usable; re-deploy from the GPUStack UI later)."
    fi
    if [ -n "$failed_critical" ]; then
        print_error "CRITICAL model(s) failed to start:$failed_critical — chat/embedding unavailable."
        return 1
    fi
    # #178 review (LOW, folded in): an optional-only failure is not a clean
    # pass — return a distinct code (2) so the call site can record gpustack
    # as WARN instead of OK, keeping the OK/WARN/FAIL vocabulary consistent
    # with how LightRAG/Cognee already report a degraded-but-not-fatal outcome.
    if [ -n "$failed_optional" ]; then
        return 2
    fi
    print_success "All models are running."
    return 0
}

# ── #429: prefetch auto_start:false spare GGUFs (online boxes) ───────────────
# ga.1 (Issue I) took the spares off the critical path; that left ONLINE
# installs permanently incomplete, and a box later switched to offline can
# NEVER fetch them. This closes the gap without touching the ga.1 timeline:
# AFTER the always-on models are up, a BACKGROUND job downloads the missing
# spare GGUFs.
#
# #640 review (agent-seqis, empirical): GPUStack caches in the OFFICIAL
# huggingface_hub layout (models--{org}--{repo}/{blobs,snapshots,refs}) — so
# the download runs INSIDE the gpustack container via huggingface_hub
# (snapshot_download, a GPUStack dependency): layout guaranteed correct incl.
# resume, glob patterns handled natively (allow_patterns), no /var/tmp
# staging, no docker cp. Container name is detected (#278 variants:
# gpustack | gpustack-legacy | gpustack-cpu), and the log lives under the
# invoking user's home — post-install does not run as root (review SF1/SF2).
PREFETCH_LOG="${HOME}/.razzfazz/model-prefetch.log"
PREFETCH_LOCK="${HOME}/.razzfazz/model-prefetch.lock"

_gpustack_container() {
    local c
    for c in gpustack gpustack-legacy gpustack-cpu; do
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
            printf '%s\n' "$c"; return 0
        fi
    done
    return 1
}

_hf_cached_present() {
    # $1 container, $2 repo, $3 filename (may be a glob / carry a sub-dir).
    # Checks the OFFICIAL hub layout (snapshots/<rev>/<file>) plus the flat
    # legacy form, mirroring expected_models.model_present.
    local cn="$1" repo="$2" fn="${3#/}" hub
    hub="models--$(printf '%s' "$repo" | sed 's|/|--|g')"
    docker exec "$cn" sh -c         "ls -1 /var/lib/gpustack/cache/huggingface/${hub}/snapshots/*/${fn}               /var/lib/gpustack/cache/huggingface/${repo}/${fn} 2>/dev/null | head -n1 | grep -q ." 2>/dev/null
}

prefetch_spare_ggufs() {
    local preset="$1"
    razzfazz_is_offline && return 0
    [ "${SKIP_MODEL_PREFETCH:-false}" = "true" ] && { print_info "Spare-model prefetch skipped (SKIP_MODEL_PREFETCH=true)."; return 0; }
    local cn
    cn=$(_gpustack_container) || return 0
    mkdir -p "${HOME}/.razzfazz" 2>/dev/null || return 0
    local rows _row alias repo filename auto_start missing=""
    rows=$(_model_rows_for_preset "$preset")
    while IFS= read -r _row; do
        [ -z "$_row" ] && continue
        alias=$(printf '%s' "$_row" | cut -f1)
        repo=$(printf '%s' "$_row" | cut -f2)
        filename=$(printf '%s' "$_row" | cut -f3)
        auto_start=$(printf '%s' "$_row" | cut -f7)
        [ "$auto_start" = "false" ] || continue
        if _hf_cached_present "$cn" "$repo" "$filename" || _local_gguf_present "$filename"; then
            continue
        fi
        missing="${missing}${alias}\t${repo}\t${filename}\n"
    done <<< "$rows"
    if [ -z "$missing" ]; then
        print_info "All auto_start:false spare GGUFs already present — nothing to prefetch."
        return 0
    fi
    print_step "Prefetching spare model GGUFs in the background (log: ${PREFETCH_LOG})..."
    print_info "Chat is already up — this does not block. 'rzfz verify-models' flips the spares from deferred to present as downloads finish."
    printf '%b' "$missing" | nohup env PREFETCH_CN="$cn"         flock -n "$PREFETCH_LOCK"         bash -c '
        while IFS=$(printf "\t") read -r alias repo filename; do
            [ -z "$alias" ] && continue
            echo "[prefetch] $(date -Is) $alias ($repo :: $filename)"
            # snapshot_download inside the gpustack container: official hub
            # layout, native glob handling, built-in resume. #640 re-review
            # (empirical, 0.208): GPUStack passes cache_dir PROGRAMMATICALLY —
            # models-- sits DIRECTLY under .../huggingface, no hub/ level.
            # HF_HOME would make hf_hub write to .../hub/models--… (one level
            # too deep, invisible to GPUStack AND our matchers); HF_HUB_CACHE
            # targets the model cache itself.
            if docker exec -e HF_HUB_CACHE=/var/lib/gpustack/cache/huggingface "$PREFETCH_CN"                 python3 -c "
import sys
from huggingface_hub import snapshot_download
snapshot_download(repo_id=sys.argv[1], allow_patterns=[sys.argv[2]])
" "$repo" "${filename#/}"; then
                echo "[prefetch] done: $alias"
            else
                echo "[prefetch] FAILED: $alias (re-run rzfz post-install to retry)"
            fi
        done
        echo "[prefetch] $(date -Is) all spares processed."
        ' >> "$PREFETCH_LOG" 2>&1 &
    disown 2>/dev/null || true
}

# Resolve the always-on default chat alias from standard-models.yaml
# (`defaults.chat`). This is the single model every consumer (Dify/Onyx/
# OpenWebUI default + the verify smoke) should wire as its default
# text-generation model. auto_start:false models (gemma4, qwen3-coder-next)
# are registered but scaled to 0 replicas (on-demand) — wiring them as the live
# default 503s on credential validation, so they must NOT be the default.
_default_chat_alias() {
    local a
    # #2158: the box's hardware class may override the default (a CPU box
    # cannot place the fleet default chat model).
    a=$(HW="$(read_env_value "$ENV_FILE" HARDWARE 2>/dev/null || true)" YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os, sys
sys.path.insert(0, os.path.dirname(os.environ['YAML_PATH']))
import hardware_catalog as hc
print(hc.defaults_for(yaml.safe_load(open(os.environ['YAML_PATH'])), os.environ.get('HW')).get('chat', ''))
" 2>/dev/null)
    echo "${a:-qwen3.6}"
}

# Resolve the fleet-standard embedding alias from standard-models.yaml
# (`defaults.embedding`). qwen3-embedding (8K ctx, dim 4096) replaced nomic
# (2048-tok cap) as the standard — Dify's default text-embedding must follow it
# so newly-created knowledge bases use the same model as cognee/lightrag/OWUI RAG.
_default_embedding_alias() {
    local a
    # #2158: the box's hardware class may override the default (a CPU box
    # cannot place the fleet default chat model).
    a=$(HW="$(read_env_value "$ENV_FILE" HARDWARE 2>/dev/null || true)" YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os, sys
sys.path.insert(0, os.path.dirname(os.environ['YAML_PATH']))
import hardware_catalog as hc
print(hc.defaults_for(yaml.safe_load(open(os.environ['YAML_PATH'])), os.environ.get('HW')).get('embedding', ''))
" 2>/dev/null)
    echo "${a:-qwen3-embedding}"
}

# The declared output dimension of the fleet-standard embedding model. Callers
# that must state a dim to a consumer (Onyx's search-settings, #1786) read it
# from the manifest instead of carrying a literal: `768` was nomic's, and a dim
# that disagrees with what the endpoint really emits is the #70 failure —
# pgvector tables built at one width, vectors arriving at another, inserts
# failing with nothing in the run output to explain why.
_default_embedding_dim() {
    local d
    d=$(YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os
spec = yaml.safe_load(open(os.environ['YAML_PATH']))
alias = spec['defaults'].get('embedding', '')
print(spec['models'].get(alias, {}).get('embedding_dimensions', ''))
" 2>/dev/null)
    printf '%s' "$d" | grep -Eq '^[0-9]+$' || d=""
    # EMPTY when the manifest cannot be read — deliberately NOT a literal
    # fallback (review of #1793). A dimension is not a name: a wrong alias makes
    # a consumer point at a model that is not there and the failure is loud,
    # while a wrong dimension builds the pgvector tables at one width and lets
    # vectors arrive at another — inserts fail with nothing in the run output to
    # explain it (#70). The caller must decide, and `_default_embedding_alias`
    # keeps its literal fallback because a name has no such property.
    # Same shape as the LightRAG path, which warns and leaves the value as-is
    # rather than writing a number it cannot justify.
    printf '%s' "$d"
}

# _manifest_per_slot_context <alias> <fallback>
# `models.<alias>.per_slot_context` from standard-models.yaml — the usable
# context of ONE request slot (ctx-size ÷ parallel), which is exactly what an
# OpenAI-compatible consumer's `context_size` credential means. #1248 needs it
# because a Dify provider entry carries a DECLARED context (no discovery exists —
# see _dify_register_models) and the manifest is the only place that knows the
# real number: hardcoding it is how the GPUStack branch ended up declaring 32768
# for a qwen3-embedding that serves 8192 (#1058's batch/ubatch fix moved it).
_manifest_per_slot_context() {
    local alias="$1" fallback="$2" v
    v=$(ALIAS="$alias" YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import os, yaml
spec = yaml.safe_load(open(os.environ['YAML_PATH'], encoding='utf-8')) or {}
m = (spec.get('models') or {}).get(os.environ['ALIAS']) or {}
print(m.get('per_slot_context') or '')
" 2>/dev/null) || v=""
    printf '%s' "${v:-$fallback}"
}

update_env_model_config() {
    local preset="$1"
    print_step "Updating .env with model configuration..."

    # M031 S3: aliases come from standard-models.yaml's `defaults` block
    # (e.g. defaults.chat → qwen3.6, defaults.embedding → qwen3-embedding).
    # Cognee's COGNEE_LLM_MODEL needs the litellm `openai/` prefix; the
    # other consumers want bare aliases.
    # #2158: both come through the hardware-aware resolver (a CPU box's chat
    # default is the 4B model, its embedding default is unchanged).
    local chat_alias embed_alias
    chat_alias=$(_default_chat_alias)
    embed_alias=$(_default_embedding_alias)

    if [ -z "$chat_alias" ] || [ -z "$embed_alias" ]; then
        print_warning "standard-models.yaml defaults.chat/embedding missing — falling back to baseline (qwen3.6 + qwen3-embedding)."
        chat_alias="${chat_alias:-qwen3.6}"
        embed_alias="${embed_alias:-qwen3-embedding}"
    fi

    # LightRAG
    update_env_value "$ENV_FILE" "LIGHTRAG_LLM_MODEL" "$chat_alias"
    update_env_value "$ENV_FILE" "LIGHTRAG_EMBEDDING_MODEL" "$embed_alias"
    # LIGHTRAG_EMBEDDING_DIM MUST track the embedding model. Historically this was
    # left at nomic's 768 after the model switched to qwen3-embedding, so LightRAG
    # built *_768d pgvector tables while the endpoint emits 4096-dim vectors →
    # every document insert failed on a dimension mismatch. The GPUStack/llama-box
    # endpoint IGNORES the OpenAI `dimensions` param, so the ONLY reliable source
    # of truth is the endpoint's actual output length — probe it here; fall back to
    # the manifest's declared dim only when the endpoint isn't reachable yet.
    # #187: only probe the LIVE endpoint when models were actually deployed. With
    # --skip-models (the default on CPU boxes now) nothing is loaded, so a live probe
    # can only ever time out — skip straight to the manifest dim below.
    local embed_dim=""
    if [ "${SKIP_MODELS:-false}" = false ]; then
        embed_dim=$(gpustack_api POST /v1-openai/embeddings \
            "{\"model\": \"$embed_alias\", \"input\": \"x\"}" 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data'][0]['embedding']))" 2>/dev/null) || true   # #1076: bare assignment under set -e killed the run on #976 boxes (no gpustack) before the manifest-dim fallback below could engage
    else
        print_substep "Skipping live embedding probe (--skip-models); using manifest dim for $embed_alias."
    fi
    if ! printf '%s' "$embed_dim" | grep -Eq '^[0-9]+$'; then
        embed_dim=$(YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" EMBED_ALIAS="$embed_alias" python3 -c "
import yaml, os
m = yaml.safe_load(open(os.environ['YAML_PATH']))['models'].get(os.environ['EMBED_ALIAS'], {})
print(m.get('embedding_dimensions', ''))
" 2>/dev/null)
        [ -n "$embed_dim" ] && print_warning "LightRAG embed-dim: endpoint unreachable, using manifest dim $embed_dim for $embed_alias (verify it matches the live endpoint)."
    fi
    if printf '%s' "$embed_dim" | grep -Eq '^[0-9]+$'; then
        update_env_value "$ENV_FILE" "LIGHTRAG_EMBEDDING_DIM" "$embed_dim"
        # The endpoint ignores `dimensions`; sending it is misleading.
        update_env_value "$ENV_FILE" "LIGHTRAG_EMBEDDING_SEND_DIM" "false"
        print_success "LightRAG embed-dim set to $embed_dim (native $embed_alias output)."
        # #178(c): only record a real outcome when the profile is actually
        # enabled — a box without `lightrag` should read SKIP, not OK, for a
        # stage whose env vars this function writes unconditionally anyway.
        if echo "${COMPOSE_PROFILES:-}" | grep -qw "lightrag"; then
            record_step_status lightrag OK
        fi
    else
        print_warning "LightRAG embed-dim could not be determined; leaving LIGHTRAG_EMBEDDING_DIM as-is (check it matches $embed_alias)."
        if echo "${COMPOSE_PROFILES:-}" | grep -qw "lightrag"; then
            record_step_status lightrag WARN
        fi
    fi

    # Cognee (needs openai/ prefix for litellm routing)
    update_env_value "$ENV_FILE" "COGNEE_LLM_MODEL" "openai/$chat_alias"
    update_env_value "$ENV_FILE" "COGNEE_EMBEDDING_MODEL" "$embed_alias"
    # #413: cognee needs the SAME measured dim LightRAG just got. It used to
    # receive only the two model names, so its pgvector tables were built from a
    # static default — correct only for as long as the deployed embedding
    # variant happened to match that constant. It did not: cognee built 768-dim
    # tables while the endpoint emitted 4096, every insert failed, and the
    # knowledge graph stayed empty forever while the container reported healthy
    # and the API answered 200. The dim is variant-dependent (the same alias
    # measured 2560 on prod and 4096 on 0.91), so the probe above — not a
    # constant — is the only trustworthy source. #70 wired that probe for
    # LightRAG and stopped there.
    if printf '%s' "$embed_dim" | grep -Eq '^[0-9]+$'; then
        update_env_value "$ENV_FILE" "COGNEE_EMBEDDING_DIM" "$embed_dim"
        print_success "Cognee embed-dim set to $embed_dim (native $embed_alias output)."
        if echo "${COMPOSE_PROFILES:-}" | grep -qw "cognee"; then
            record_step_status cognee OK
        fi
    else
        print_warning "Cognee embed-dim could not be determined; leaving COGNEE_EMBEDDING_DIM as-is (check it matches $embed_alias)."
        if echo "${COMPOSE_PROFILES:-}" | grep -qw "cognee"; then
            record_step_status cognee WARN
        fi
    fi

    # Model Sync (uses the same API key)
    # GPUSTACK_API_KEY already set by ensure_gpustack_api_key

    print_success ".env updated with model names (chat=$chat_alias, embedding=$embed_alias)."
}

# ==============================================================================
# M031 S3: sync_consumers — push standard-models.yaml to every consumer
# ==============================================================================
# After the GPUStack deploy, ensure every gpustack-consuming service
# (Open WebUI, Dify, Cognee, moltis, hermes) is reconciled to what the
# YAML says — model aliases, per-model context windows, embedding
# dimensions. core/llm/sync.py is idempotent: it inspects the live state
# and only writes diffs.
#
# Cognee is invoked separately when its profile is active so the .env
# changes (LLM_ARGS max_tokens hint) take effect at the next cognee
# container restart.
sync_consumers() {
    print_step "Reconciling LLM consumers via core/llm/sync.py..."

    if [ ! -f "$SCRIPT_DIR/core/llm/sync.py" ]; then
        print_warning "core/llm/sync.py not present — skipping consumer reconciliation."
        return 0
    fi

    # Build target list dynamically based on which profiles are active.
    # gpustack itself is reconciled by deploy_all_models above, so we
    # only need the consumer-side targets here.
    local targets="openwebui"
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "dify"; then targets="$targets,dify"; fi
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "cognee"; then targets="$targets,cognee"; fi
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "lightrag"; then targets="$targets,lightrag"; fi
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "agents"; then
        # All five per-user agent types are reachable via the agents profile.
        # Each sync_<agent> function iterates over running instances; safe to
        # call even when no instances exist (returns "no running instances").
        targets="$targets,moltis,hermes,paperclip,openhands"
    fi

    print_substep "Targets: $targets"
    if python3 "$SCRIPT_DIR/core/llm/sync.py" --target "$targets" 2>&1 | sed 's/^/  /'; then
        print_success "Consumer reconciliation complete."
    else
        print_warning "core/llm/sync.py reported issues — review the output above."
    fi
}

# ------------------------------------------------------------------------------
# cognee API helpers (#1249)
# ------------------------------------------------------------------------------
# cognee creates its admin user in the FastAPI *lifespan*: `cognee/api/client.py`
# calls `get_default_user()`, which falls through to `create_default_user()` and
# reads DEFAULT_USER_EMAIL / DEFAULT_USER_PASSWORD — the two keys
# modules/knowledge/cognee/compose.yml feeds from COGNEE_ADMIN_PASSWORD (with
# AUTHENTIK_BOOTSTRAP_PASSWORD as the fallback). On a FRESH volume that lifespan
# first runs the alembic migrations, creates the database and initialises the
# ladybug graph store, so uvicorn is not serving yet — the compose healthcheck
# budgets 180s for exactly that (`start_period`).
#
# #1249: the mint gated on `docker ps --format … | grep -qx cognee`, i.e. on
# RUNNING, not on SERVING, and a login against a not-yet-serving container came
# back EMPTY — which the code reported as "cognee pw divergence", impossible on
# a clean install (cli/init.sh writes COGNEE_ADMIN_PASSWORD before the first
# `docker compose up -d`). On an llm-manager box it is not even a rare race:
# wire_llm_manager_rag_consumers force-recreates cognee at Step 1 of
# post-install, seconds before this runs.
#
# The helpers talk to cognee's own API from INSIDE the container (curl is part
# of the cognee image — verified in cognee/cognee:1.4.0 and the pinned 1.5.3),
# so neither a published host port nor a host proxy exemption is needed. Every
# call is --max-time bounded: the pre-#1249 calls had NO timeout and cognee
# binds its port before it serves, so they could hang for minutes. Each helper
# prints ONE parseable line, "<http-status>|<value>|<body excerpt>", so a
# failure can be reported with cognee's own answer instead of a guess.
_cognee_health_code() {
    local out
    out=$(docker exec cognee sh -c \
        'curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:8000/health' \
        2>/dev/null || true)
    printf '%s' "${out:-000}" | tr -cd '0-9' | tail -c 3
}

# Credentials travel as `docker exec -e` environment (expanded by the
# CONTAINER's shell) so no host-side quoting can corrupt a password, and
# --data-urlencode keeps operator-typed specials (& = + %) intact.
_cognee_login() {
    # rev-B (review F2): stderr must NOT join the stream whose LAST LINE is
    # parsed as the HTTP status. stdout is block-buffered in a pipe, stderr is
    # not, so `curl: (28) Operation timed out …` can land last and be read as
    # status `001` — which matches neither `000` nor `5*`, so the retry/backoff
    # branch falls out exactly in the transport failure it exists for.
    local em="$1" pw="$2" raw full status body tok err
    err=$(mktemp 2>/dev/null || echo "/tmp/rzfz-cognee-$$.err")
    raw=$(docker exec -e RZ_EM="$em" -e RZ_PW="$pw" cognee sh -c \
        'curl -sS --max-time 15 -w "\n%{http_code}" -X POST \
             http://localhost:8000/api/v1/auth/login \
             -H "Content-Type: application/x-www-form-urlencoded" \
             --data-urlencode "username=$RZ_EM" \
             --data-urlencode "password=$RZ_PW"' 2>"$err" || true)
    status=$(printf '%s' "$raw" | tail -n1 | tr -cd '0-9' | tail -c 3)
    full=$(printf '%s' "$raw" | sed '$d')
    tok=$(printf '%s' "$full" | python3 -c \
        'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
    body=$(printf '%s%s' "$full" "$(cat "$err" 2>/dev/null || true)" | tr -d '\r\n' | head -c 200)
    rm -f "$err"
    printf '%s|%s|%s\n' "${status:-000}" "$tok" "$body"
}

_cognee_mint_api_key() {
    # rev-B (review F2): see _cognee_login — stderr is captured separately so
    # the status parse only ever sees curl's own -w line.
    local tok="$1" raw full status body key err
    err=$(mktemp 2>/dev/null || echo "/tmp/rzfz-cognee-$$.err")
    raw=$(docker exec -e RZ_TOK="$tok" cognee sh -c \
        'curl -sS --max-time 15 -w "\n%{http_code}" -X POST \
             http://localhost:8000/api/v1/auth/api-keys \
             -H "Authorization: Bearer $RZ_TOK" \
             -H "Content-Type: application/json" \
             -d "{\"name\":\"cognee-mcp\"}"' 2>"$err" || true)
    status=$(printf '%s' "$raw" | tail -n1 | tr -cd '0-9' | tail -c 3)
    full=$(printf '%s' "$raw" | sed '$d')
    key=$(printf '%s' "$full" | python3 -c \
        'import sys,json;print(json.load(sys.stdin).get("key",""))' 2>/dev/null || true)
    body=$(printf '%s%s' "$full" "$(cat "$err" 2>/dev/null || true)" | tr -d '\r\n' | head -c 200)
    rm -f "$err"
    printf '%s|%s|%s\n' "${status:-000}" "$key" "$body"
}

# What the RUNNING container was created with — a container recreate re-reads
# .env, an existing DB user is never re-hashed (see cli/set-admin-password.sh
# ::apply_cognee), so these two can legitimately disagree. That makes the
# mismatch a DECIDABLE cause rather than a guess.
_cognee_seed_password() {
    docker inspect cognee --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^DEFAULT_USER_PASSWORD=//p' | head -n1 || true
}

# Mint COGNEE_MCP_API_KEY. Returns 0 only when the key was actually written.
# Every command substitution is `|| true`-guarded: `--refresh` calls
# provision_mcp_registry UNGUARDED under `set -eo pipefail`, where a bare
# non-zero assignment aborts the whole refresh before ensure_mcp_manager_secret
# / reconcile_authentik_bindings / run_security_selfcheck ever run.
# #1806: cognee signs its session tokens with FASTAPI_USERS_JWT_SECRET, fed from
# COGNEE_JWT_SECRET (compose). An EMPTY value is not a fallback to a default —
# cognee reads os.getenv(name, "super_secret"), so empty means an EMPTY HMAC key
# (trivially forgeable), and cognee's API is exempt from the SSO gate (F-034).
# init/upgrade generate the secret, but a box wired by hand (or mid-migration)
# can reach here with it empty. Refuse to proceed rather than mint an MCP key
# for a cognee whose sessions anyone can forge. Returns non-zero; the caller
# already treats a cognee readiness failure as non-fatal + actionable.
cognee_require_jwt_secret() {
    local v
    v="$(read_env_value "$ENV_FILE" COGNEE_JWT_SECRET 2>/dev/null || true)"
    if [ -z "$v" ]; then
        print_warning "cognee: COGNEE_JWT_SECRET is empty — cognee would sign session tokens with an EMPTY key, and its API is exempt from the SSO gate (#1806). Refusing to wire cognee."
        print_info "  Fix: rzfz setup --regenerate-secrets  (or set COGNEE_JWT_SECRET in .env), then: rzfz post-install --refresh"
        return 1
    fi
    return 0
}

_provision_cognee_mcp_key() {
    local em live_em target cap abp seed_pw
    local ready_max waited code res status body tok key attempt pw

    cognee_require_jwt_secret || return 1

    em="razzfazz-ai-admin@$(read_env_value "$ENV_FILE" MAIN_DOMAIN 2>/dev/null || true)"
    cap="$(read_env_value "$ENV_FILE" COGNEE_ADMIN_PASSWORD 2>/dev/null || true)"
    abp="$(read_env_value "$ENV_FILE" AUTHENTIK_BOOTSTRAP_PASSWORD 2>/dev/null || true)"

    # 1) Readiness: SERVING, not merely running.
    # Budget = SLEEP time, not wall clock (review F3): each probe may add up
    # to its own --max-time on top, so the worst case is about double. Same
    # shape as _llm_manager_await_worker_registration (#1254).
    # The default is pinned to the cognee service's compose start_period by
    # tests/unit/consistency/test_1249_cognee_timeout_matches_compose.py —
    # a comment alone let a 180 -> 5 mutation survive the whole suite.
    ready_max="${COGNEE_MCP_READY_TIMEOUT:-180}"
    waited=0
    code="$(_cognee_health_code)"
    while [ "$code" != "200" ]; do
        if [ "$waited" -ge "$ready_max" ]; then
            print_warning "cognee is running but never answered /health 200 within ${ready_max}s (last status ${code}) — COGNEE_MCP_API_KEY not minted."
            print_info "  step: readiness. This is NOT a password problem: cognee creates its admin user in its startup lifespan (on a fresh volume: DB migrations + graph init first), so a login before that returns nothing."
            print_info "  Check: docker inspect -f '{{.State.Health.Status}}' cognee ; docker logs --tail 50 cognee"
            print_info "  Then re-run: rzfz post-install --refresh"
            return 1
        fi
        [ "$waited" = "0" ] && print_substep "Waiting for cognee to serve /health before the admin login (max ${ready_max}s)..."
        sleep 5
        waited=$((waited + 5))
        code="$(_cognee_health_code)"
    done

    # cognee's OWN view of the admin address (same probe cli/set-admin-password.sh
    # uses) — authoritative over the .env-derived guess.
    live_em=$(docker exec cognee python3 -c \
        "from cognee.base_config import get_base_config; print(get_base_config().default_user_email or '')" \
        2>/dev/null | head -n1 || true)
    target="${live_em:-$em}"

    # 2) Login. Both .env candidates, retried on a transient (000/5xx) answer:
    #    a 4xx is a credential verdict and retrying it only wastes time.
    #    NOT fallback-when-empty (ga.6): a set-but-wrong COGNEE_ADMIN_PASSWORD
    #    must not block the AUTHENTIK_BOOTSTRAP_PASSWORD attempt.
    tok=""; status="000"; body=""
    for attempt in 1 2 3; do
        for pw in "$cap" "$abp"; do
            [ -z "$pw" ] && continue
            res="$(_cognee_login "$target" "$pw")"
            status="${res%%|*}"; res="${res#*|}"
            tok="${res%%|*}"; body="${res#*|}"
            [ -n "$tok" ] && break 2
            [ "$cap" = "$abp" ] && break   # identical → don't retry the same pw
        done
        case "$status" in
            000|5*) sleep $((attempt * 5)) ;;
            *)      break ;;
        esac
    done

    if [ -z "$tok" ]; then
        seed_pw="$(_cognee_seed_password)"
        print_warning "cognee admin login failed — COGNEE_MCP_API_KEY not minted, so the cognee-mcp sidecar stays unauthenticated (agents get no cognee memory)."
        print_info "  step: login  email: ${target}  http: ${status}  body: ${body:-<empty>}"
        if [ -n "$live_em" ] && [ "$live_em" != "$em" ]; then
            print_info "  cause: cognee seeded '${live_em}' while .env implies '${em}' — the admin addresses disagree."
            print_info "  Fix: align MAIN_DOMAIN / DEFAULT_USER_EMAIL, then: cli/set-admin-password.sh cognee"
        elif [ -z "$cap" ] && [ -z "$abp" ]; then
            print_info "  cause: COGNEE_ADMIN_PASSWORD and AUTHENTIK_BOOTSTRAP_PASSWORD are BOTH empty in .env — cognee seeded its upstream default admin instead."
            print_info "  Fix: set COGNEE_ADMIN_PASSWORD in .env, then: cli/set-admin-password.sh cognee"
        elif [ -n "$seed_pw" ] && [ "$seed_pw" != "$cap" ] && [ "$seed_pw" != "$abp" ]; then
            print_info "  cause: the RUNNING container was created with a DEFAULT_USER_PASSWORD that matches neither .env candidate (a password rotation after cognee's first boot)."
            print_info "  Fix: cli/set-admin-password.sh cognee   (updates the hash through cognee's own user-manager)"
        else
            print_info "  cause: .env and the container agree on the seed password, so the stored hash was changed after cognee's first boot."
            print_info "  Fix: cli/set-admin-password.sh cognee   (updates the hash through cognee's own user-manager)"
        fi
        return 1
    fi

    # 3) Mint.
    res="$(_cognee_mint_api_key "$tok")"
    status="${res%%|*}"; res="${res#*|}"
    key="${res%%|*}"; body="${res#*|}"
    if [ -z "$key" ]; then
        print_warning "cognee api-key mint failed — COGNEE_MCP_API_KEY not minted (the admin login itself succeeded)."
        print_info "  step: mint  http: ${status}  body: ${body:-<empty>}"
        return 1
    fi
    update_env_value "$ENV_FILE" "COGNEE_MCP_API_KEY" "$key"
    print_success "Minted COGNEE_MCP_API_KEY for cognee-mcp sidecar."
    # cognee-mcp reads API_TOKEN=${COGNEE_MCP_API_KEY} at container CREATE, so a
    # sidecar that started before the mint is still holding an empty token —
    # exactly the "MCP server dead for agents" symptom. Recreate that ONE
    # service by name (never a blanket `up -d` on a loaded box).
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx cognee-mcp; then
        docker compose up -d --no-deps --force-recreate cognee-mcp >/dev/null 2>&1 \
            || print_warning "cognee-mcp recreate returned non-zero — the new key takes effect at its next start."
    fi
    return 0
}

# ==============================================================================
# MCP registry (M035 P1)
# ==============================================================================
# Mint a cognee API key for the cognee-mcp sidecar (COGNEE_MCP_API_KEY) so it
# can authenticate to cognee in API mode, then validate the MCP registry. The
# key lets MCP agents (Moltis/Hermes/OpenCode) use cognee as memory via
# http://cognee-mcp:8000/mcp. Idempotent: skips if the key is already set.
provision_mcp_registry() {
    case "${COMPOSE_PROFILES:-}" in *cognee*) ;; *) return 0 ;; esac
    print_step "MCP registry: provisioning cognee-mcp credentials..."
    local key_missing=0
    if [ -n "$(read_env_value "$ENV_FILE" COGNEE_MCP_API_KEY 2>/dev/null || true)" ]; then
        print_info "COGNEE_MCP_API_KEY already set."
    elif ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx cognee; then
        print_warning "cognee not running — skipping COGNEE_MCP_API_KEY mint."
        key_missing=1
    else
        _provision_cognee_mcp_key || key_missing=1
    fi
    # Validate the MCP registry (non-fatal advisory).
    if [ -f "$SCRIPT_DIR/core/mcp/sync.py" ]; then
        python3 "$SCRIPT_DIR/core/mcp/sync.py" --check 2>&1 | sed 's/^/  /' \
            || print_warning "MCP registry validation reported issues."
    fi
    # #1249: that validation checks core/mcp/mcp-servers.yaml and knows nothing
    # about COGNEE_MCP_API_KEY — its "mcp registry: valid" line printed straight
    # after a failed mint made the step read as a success. The missing key gets
    # the last word instead (and --verify fails on it, see the cognee section of
    # run_verification).
    if [ "$key_missing" = "1" ]; then
        print_warning "cognee-mcp has NO credential: COGNEE_MCP_API_KEY is empty (the registry line above validates the YAML only). MCP agents have no cognee memory backend until this is minted."
    fi
    # Never leak a trailing non-zero (the `if [ -f … ]` guard above returns 1
    # when core/mcp/sync.py is absent) up to a bare `set -e` caller.
    return 0
}
# Personal MCP Manager (#36): mint the AES-256-GCM master key + DB password if
# absent (idempotent — does NOT rotate an existing key, which would orphan every
# already-encrypted credential). Mirrors the ensure-if-missing pattern used for
# other secrets on upgrade/refresh paths.
ensure_mcp_manager_secret() {
    case "${COMPOSE_PROFILES:-}" in *mcp*) ;; *) return 0 ;; esac
    if [ -z "$(read_env_value "$ENV_FILE" MCP_MANAGER_SECRET_KEY)" ]; then
        update_env_value "$ENV_FILE" "MCP_MANAGER_SECRET_KEY" "$(openssl rand -base64 32 | tr -d '\n')"
        print_success "Minted MCP_MANAGER_SECRET_KEY (was absent)."
    fi
    if [ -z "$(read_env_value "$ENV_FILE" MCP_MANAGER_DB_PASSWORD)" ]; then
        update_env_value "$ENV_FILE" "MCP_MANAGER_DB_PASSWORD" "$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
        print_success "Minted MCP_MANAGER_DB_PASSWORD (was absent)."
    fi
    # #61 NEW-3: the shared service-to-service secret gating mcp-manager's
    # /internal/* routes (agent-manager presents it on every agent launch to
    # fetch the per-user MCP wiring). It is minted by init.sh on a fresh box, but
    # an mcp-enabled box that was UPGRADED (not freshly init'd) never ran init's
    # regenerate_all_secrets, so the key stayed empty → /internal fails closed
    # (401) → wiring breaks (the per-proxy bearer never reaches the user's
    # agents). Mint-if-absent here so the self-heal path populates it. Same hex
    # width as init's `generate_hex_secret 32` (64 hex chars).
    if [ -z "$(read_env_value "$ENV_FILE" MCP_INTERNAL_TOKEN)" ]; then
        update_env_value "$ENV_FILE" "MCP_INTERNAL_TOKEN" "$(openssl rand -hex 32)"
        print_success "Minted MCP_INTERNAL_TOKEN (was absent — /internal wiring would 401)."
    fi
    # The mcp.<domain> forward-auth client secret (Authentik proxy provider).
    # Same upgrade gap; mint-if-absent.
    if [ -z "$(read_env_value "$ENV_FILE" MCP_CLIENT_SECRET)" ]; then
        update_env_value "$ENV_FILE" "MCP_CLIENT_SECRET" "$(openssl rand -hex 32)"
        print_success "Minted MCP_CLIENT_SECRET (was absent)."
    fi
    # A trailing `if [ -z … ]` whose secret is ALREADY set returns 1; never let
    # that abort a bare `set -e` caller (project_postinstall_set_e_abort_pattern).
    return 0
}

reconcile_authentik_bindings() {
    # ga.6 backstop for the init-time outpost-attach race. init-authentik.sh runs
    # apply-policy-bindings.py during init (always, even fast-path — ga.4 round 17),
    # but its poll can settle on a PARTIAL ProxyProvider set while the blueprint
    # controller is still creating later modules' providers, leaving those providers
    # un-attached to the embedded outpost → their subdomains 404 from Authentik
    # ("Not Found" / broken start portal; seen on culturehack-001 + 0.208 — only
    # 10 of 24 providers attached). At post-install time every ProxyProvider already
    # exists and is stable, so re-running the same idempotent reconcile here attaches
    # the full set reliably (no race). Runs on --preset AND --refresh.
    docker ps --format '{{.Names}}' | grep -qx authentik-worker \
        || { print_substep "authentik-worker not running — skipping outpost reconcile."; return 0; }
    local script="$SCRIPT_DIR/core/Authentik/apply-policy-bindings.py"
    [ -f "$script" ] || { print_substep "apply-policy-bindings.py not found — skipping outpost reconcile."; return 0; }
    print_step "Reconciling Authentik bindings + embedded-outpost providers (post-install backstop)..."
    if docker cp "$script" authentik-worker:/tmp/apply_policy_bindings.py 2>/dev/null \
       && docker exec -e COMPOSE_PROFILES="${COMPOSE_PROFILES:-}" authentik-worker python /tmp/apply_policy_bindings.py; then
        print_success "Authentik bindings + outpost providers reconciled."
    else
        print_warning "Authentik binding reconcile reported issues — re-run: docker exec -e COMPOSE_PROFILES=\"\$(grep -oE '^COMPOSE_PROFILES=.*' .env | cut -d= -f2-)\" authentik-worker python /tmp/apply_policy_bindings.py"
    fi
}

# ==============================================================================
# DNS Provisioning
# ==============================================================================
setup_local_dns() {
    print_step "DNS: Setting up /etc/hosts entries..."

    local domain="${MAIN_DOMAIN}"
    if [ -z "$domain" ]; then
        print_error "MAIN_DOMAIN not set in .env"
        return 1
    fi

    # Get local IP
    local local_ip
    local_ip=$(hostname -I | awk '{print $1}')

    # rc6.4 fix (M023-S05.2 follow-up): derive the FQDN list from .env's
    # `*_DOMAIN` keys instead of a hardcoded subdomain string. New modules
    # (e.g. crawl4ai added in M023-S03 / rc6) carry their own *_DOMAIN env
    # entry; the previous hardcoded list drifted every release because there
    # was nothing forcing it to stay in sync. Static modules where the
    # Caddyfile uses `<sub>.{$MAIN_DOMAIN}` directly (docling, pdf — no
    # per-module *_DOMAIN) are added below as a small static suffix list.
    #
    # External *_DOMAIN values (e.g. GOOGLE_OAUTH_DOMAIN=seqis.com) are
    # excluded by checking that the resolved FQDN actually ends with
    # `.${MAIN_DOMAIN}`; only locally-served domains land in /etc/hosts.
    local entries_needed=""
    local added=0
    local -A fqdns=()
    fqdns["$domain"]=1

    while IFS='=' read -r key value; do
        # Skip MAIN_DOMAIN itself (we already added the bare domain)
        [ "$key" = "MAIN_DOMAIN" ] && continue
        # Strip surrounding quotes if any
        value="${value%\"}"; value="${value#\"}"
        # Strip an inline ` # comment` (rc6.2 read_env_value semantics)
        value=$(printf '%s' "$value" | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//')
        # Expand `${MAIN_DOMAIN}` literal
        value="${value//\$\{MAIN_DOMAIN\}/$domain}"
        # Skip empties / non-local
        [ -z "$value" ] && continue
        case "$value" in
            *.${domain}|${domain}) fqdns["$value"]=1 ;;
            *) ;;  # external domain, skip
        esac
    done < <(grep -E '^[A-Z][A-Z_0-9]*_DOMAIN=' "$ENV_FILE" 2>/dev/null)

    # Static fallback for Caddyfile entries that do NOT use a per-module
    # *_DOMAIN env (kept short — anything new should adopt the env-var
    # pattern rather than grow this list).
    for static_sub in docling pdf; do
        fqdns["${static_sub}.${domain}"]=1
    done

    for fqdn in "${!fqdns[@]}"; do
        if ! grep -qF " ${fqdn}" /etc/hosts 2>/dev/null \
           && ! grep -qE "^[^#]*\b${fqdn//./\\.}\b" /etc/hosts 2>/dev/null; then
            entries_needed="${entries_needed}${local_ip}   ${fqdn}\n"
            added=$((added + 1))
        fi
    done
    
    if [ $added -eq 0 ]; then
        print_info "All DNS entries already present in /etc/hosts."
        return 0
    fi
    
    # Write entries — try direct write first, fall back to sudo
    if [ -w /etc/hosts ]; then
        echo -e "$entries_needed" >> /etc/hosts
    else
        set +e
        echo -e "$entries_needed" | sudo tee -a /etc/hosts > /dev/null 2>&1
        local dns_rc=$?
        set -e
        if [ $dns_rc -ne 0 ]; then
            print_warning "Could not write to /etc/hosts (no sudo access)."
            print_info "Add these entries manually:"
            echo -e "$entries_needed" | sed 's/^/    /'
            return 0
        fi
    fi
    
    print_success "Added $added DNS entries to /etc/hosts."
}

# ==============================================================================
# Speaches & Auxiliary Provisioning
# ==============================================================================
speaches_download_model() {
    local model_id="$1" model_type="$2"
    local speaches_url="http://127.0.0.1:${SPEACHES_PORT:-5003}"

    # #184 WS2b: the POST below tells Speaches to DOWNLOAD the model from
    # HuggingFace → internet egress. Skip on an air-gapped box (the model ships in
    # the offline package / is preloaded into the speaches cache volume).
    if razzfazz_offline_skip "Speaches ${model_type} model download from HuggingFace ('${model_id}')"; then
        return 0
    fi

    # Check if already installed
    local installed
    installed=$(curl -s "$speaches_url/v1/models/$model_id" 2>/dev/null | \
        python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('id') else 'no')" 2>/dev/null)
    
    if [ "$installed" = "yes" ]; then
        print_info "$model_type model '$model_id' already installed."
        return 0
    fi
    
    print_substep "Downloading $model_type model '$model_id'..."
    local resp
    resp=$(curl -s --max-time 300 -X POST "$speaches_url/v1/models/$model_id" 2>&1)
    
    if echo "$resp" | grep -qi "downloaded\|already exists"; then
        print_success "$model_type model '$model_id' ready."
        return 0
    else
        print_warning "$model_type model download response: $resp"
        # #178 review: this is the one place a caller needs to tell "download
        # actually failed" from "already installed / just downloaded" — an
        # explicit return 1 here is what lets step_speaches_provisioning
        # record WARN instead of a silent OK for an optional model that
        # didn't come down.
        return 1
    fi
}

step_speaches_provisioning() {
    print_step "Speaches & Auxiliary: Provisioning..."

    # #178 review: this function used to call every sub-step as a bare
    # statement and END on print_success — a bash function's implicit
    # return status is that of its LAST command, so the ORIGINAL body
    # structurally returned 0 no matter what happened above, even when
    # Speaches never came up. Speaches being unreachable is a genuine FAIL;
    # propagate it.
    wait_for_service "Speaches" "http://127.0.0.1:${SPEACHES_PORT:-5003}/v1/models" 60 || return 1

    # The model downloads are best-effort/optional (a box can run with the
    # service up but a model not yet cached — the operator can retry from
    # the Speaches API later). A download failure must not FAIL the whole
    # step, but it must not render a silent OK either: track it and report
    # WARN via a distinct return code (2) that the caller maps to WARN —
    # the same call-site dispatch pattern used for gpustack/wait_for_all_models.
    local download_failed=false

    # Download STT model
    speaches_download_model "Systran/faster-whisper-small" "STT" || download_failed=true

    # Download TTS model/voice
    speaches_download_model "ufozone/piper-de_DE-jarvis-high" "TTS" || download_failed=true

    # Model sync — GPUSTACK_API_KEY already in .env from ensure_gpustack_api_key
    print_substep "Model sync: API key already configured in .env."

    # LightRAG + Cognee model names already set in update_env_model_config
    print_substep "LightRAG + Cognee: model names already configured in .env."

    if [ "$download_failed" = true ]; then
        print_warning "Speaches & auxiliary provisioning completed with warnings (an optional model download failed — see above; Speaches itself is up)."
        return 2
    fi

    print_success "Speaches & auxiliary provisioning complete."
}

# ==============================================================================
# Dify Provisioning
# ==============================================================================
DIFY_ACCESS=""
DIFY_CSRF=""

dify_login() {
    local dify_email="$1"
    local dify_pass="$2"
    # #1164: the password (base64, as Dify's login wants it) travels on STDIN
    # through a JSON body built by json.dumps — never in docker-exec argv
    # (host ps / audit), never string-interpolated into JSON (a rotated
    # password with a quote or backslash broke or injected the body).
    local resp
    resp=$(printf '%s' "$dify_pass" | DIFY_EMAIL="$dify_email" python3 -c '
import base64, json, os, sys
print(json.dumps({"email": os.environ["DIFY_EMAIL"],
                  "password": base64.b64encode(sys.stdin.buffer.read()).decode()}))' \
        | docker exec -i dify-api curl -s -D- -X POST http://localhost:5001/console/api/login \
        -H "Content-Type: application/json" \
        -d @- 2>&1)
    
    DIFY_ACCESS=$(echo "$resp" | grep -oP 'access_token=\K[^;]+')
    DIFY_CSRF=$(echo "$resp" | grep -oP 'csrf_token=\K[^;]+')
    
    [ -n "$DIFY_ACCESS" ] && [ -n "$DIFY_CSRF" ]
}

dify_api() {
    local method="$1" path="$2" data="${3:-}"
    local url="http://localhost:5001${path}"
    # #1164: argv array + body on STDIN — the old `sh -c "$cmd"` string with
    # `-d '$data'` broke (or injected) on any single quote in the payload.
    if [ -n "$data" ]; then
        printf '%s' "$data" | docker exec -i dify-api curl -s -X "$method" \
            -H "Cookie: access_token=${DIFY_ACCESS}; csrf_token=${DIFY_CSRF}" \
            -H "X-CSRF-Token: ${DIFY_CSRF}" \
            -H "Content-Type: application/json" -d @- "$url" 2>/dev/null
    else
        docker exec dify-api curl -s -X "$method" \
            -H "Cookie: access_token=${DIFY_ACCESS}; csrf_token=${DIFY_CSRF}" \
            -H "X-CSRF-Token: ${DIFY_CSRF}" "$url" 2>/dev/null
    fi
}

# S20: resolve the Dify admin account email. `razzfazz-ai-admin@$MAIN_DOMAIN`
# is correct on a fresh install, but if the operator changed MAIN_DOMAIN after
# init the account row keeps its ORIGINAL email — so the derived address finds
# no account, the model-config Python sys.exit(1)s, and (pre-fix) the bash
# success-check only grepped output → post-install "completed" with ZERO Dify
# models (operator-reported on culturehack-001 after a razzfazz.ai →
# razzfazz-culturehack.eu domain change). This helper tries the derived email
# first, then falls back to the actual admin row (oldest account). On a
# pre-setup box with no accounts it returns the derived email (which dify
# setup will then create).
_resolve_dify_admin_email() {
    local derived="razzfazz-ai-admin@${MAIN_DOMAIN}"
    local hit
    hit=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${DIFY_DB:-dify_db}" -tAc \
        "SELECT 1 FROM accounts WHERE email='${derived}' LIMIT 1;" 2>/dev/null | xargs)
    if [ "$hit" = "1" ]; then
        echo "$derived"; return 0
    fi
    local actual
    actual=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${DIFY_DB:-dify_db}" -tAc \
        "SELECT email FROM accounts ORDER BY created_at LIMIT 1;" 2>/dev/null | xargs)
    if [ -n "$actual" ]; then
        echo "$actual"; return 0
    fi
    echo "$derived"; return 0
}

# #1139: the ORDERED list of passwords that may belong to the Dify console
# admin account, most-authoritative first, one per line.
#
# `DIFY_ADMIN_PASSWORD` is the .env RECORD of that account: cli/init.sh writes it
# (#952), `cli/set-admin-password.sh dify` rotates it (its APPS registry maps
# `dify:DIFY_ADMIN_PASSWORD`), and the day-1 `admin_password_verifies[dify]` probe
# checks against it. `AUTHENTIK_BOOTSTRAP_PASSWORD` is only the FLEET default —
# it is what pre-#952 boxes happened to seed the Dify account with, so it stays
# as a LOGIN fallback for them and for nothing else.
#
# Using the bootstrap password as the primary source is what made an operator
# rotation self-destruct: after `set-admin-password.sh dify` the account and
# DIFY_ADMIN_PASSWORD hold the new password while the bootstrap one is stale, so
# every login below failed and the last-resort DB reset quietly rewrote the
# account back to the FLEET password — undoing the rotation and re-opening the
# .env-vs-DB divergence (M033 S17) that reset exists to close.
#
# Empty values are dropped (an old .env may not carry the key at all; an empty
# candidate would burn a login attempt and, on the setup path, create an admin
# with no password), and a duplicate is emitted once — on a normal post-#952
# install both variables hold the SAME value, and two identical failing logins
# only double the pressure on Dify's login rate limiter.
_dify_admin_passwords() {
    local primary="${DIFY_ADMIN_PASSWORD:-}"
    local fallback="${AUTHENTIK_BOOTSTRAP_PASSWORD:-}"
    [ -n "$primary" ] && printf '%s\n' "$primary"
    if [ -n "$fallback" ] && [ "$fallback" != "$primary" ]; then
        printf '%s\n' "$fallback"
    fi
    return 0
}

dify_ensure_admin() {
    local admin_email="razzfazz-ai-admin@${MAIN_DOMAIN}"
    # #1139: candidates in priority order; [0] is the password this box CLAIMS
    # the account has, so it is the one used to CREATE the account on a fresh
    # setup and the one the self-heal below converges the account onto.
    local -a admin_pw_candidates=()
    local _dify_pw_line
    while IFS= read -r _dify_pw_line; do
        [ -n "$_dify_pw_line" ] && admin_pw_candidates+=("$_dify_pw_line")
    done < <(_dify_admin_passwords)
    local admin_pass="${admin_pw_candidates[0]:-}"
    if [ -z "$admin_pass" ]; then
        # Both keys empty: there is no password to log in with OR to create the
        # account with. Proceeding would run /console/api/setup with an EMPTY
        # password and leave a password-less Dify admin behind — fail loudly
        # instead, the .env is the thing that needs fixing.
        print_error "Neither DIFY_ADMIN_PASSWORD nor AUTHENTIK_BOOTSTRAP_PASSWORD is set in .env — cannot provision the Dify admin account."
        return 1
    fi

    # Wait for dify-api to actually answer before ANY /console/api call.
    # dify-api may have just been (re)created by a preceding `docker compose up`
    # (env/profile change, module enable); while it is cold the setup + login
    # endpoints refuse the connection / 5xx. Previously the ONLY readiness wait
    # lived inside the INIT_PASSWORD>30 self-heal branch below, so the normal
    # case (a <=30-char INIT_PASSWORD) skipped it entirely and fired
    # /console/api/init at a not-ready API: init silently no-ops, setup stays
    # "not_started", and every later login 401s "not_init_validated" ->
    # "Failed to authenticate with Dify" on an otherwise-healthy box.
    local _dify_wait
    for _dify_wait in $(seq 1 40); do
        docker exec dify-api curl -sf --max-time 5 \
            http://localhost:5001/console/api/setup >/dev/null 2>&1 && break
        sleep 3
    done

    # Check setup status
    local setup_status
    setup_status=$(docker exec dify-api curl -s http://localhost:5001/console/api/setup 2>/dev/null | \
        python3 -c "import sys,json; print(json.load(sys.stdin).get('step',''))" 2>/dev/null)

    if [ "$setup_status" != "finished" ]; then
        # Two-step Dify setup:
        # 1. Validate init password (sets session cookie)
        # 2. Create admin user (requires session from step 1)
        print_substep "Running Dify initial setup..."
        local init_pass
        # `|| true` is load-bearing: this file runs under `set -eo pipefail`, and a
        # BARE `x=$(grep … | cut …)` inherits the pipeline's status. When the key is
        # ABSENT grep exits 1, pipefail propagates it, and the run dies HERE — before
        # the very next line, which already handles the empty case. (#200; same shape
        # as the #755 regression fixed in #793.)
        init_pass=$(grep "^INIT_PASSWORD=" "${SCRIPT_DIR}/.env.dify" 2>/dev/null | cut -d= -f2-) || true
        # Dify 1.14+ caps INIT_PASSWORD at 30 chars (init_validate.py max_length=30).
        # The box admin password is often longer (operator-generated); if it leaked
        # into INIT_PASSWORD, /console/api/init 422s forever and Dify never gets set
        # up. INIT_PASSWORD is ONLY the first-run gate — self-heal it to a dedicated
        # <=30 token and recreate dify-api so the container env matches, then create
        # the admin below with the real (good) admin password.
        if [ -z "$init_pass" ] || [ "${#init_pass}" -gt 30 ]; then
            init_pass=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 24)
            print_substep "Dify INIT_PASSWORD missing/>30 chars — setting a <=30 gate + recreating dify-api..."
            update_env_value "${SCRIPT_DIR}/.env.dify" "INIT_PASSWORD" "$init_pass"
            docker compose up -d --no-deps --force-recreate dify-api >/dev/null 2>&1 || true
            for _i in $(seq 1 40); do
                docker exec dify-api curl -s http://localhost:5001/console/api/setup >/dev/null 2>&1 && break
                sleep 3
            done
        fi

        # Step 1: Validate init password (capture session cookie)
        local init_resp
        # #1164: secrets on STDIN via json.dumps, never in argv (see dify_login).
        init_resp=$(printf '%s' "$init_pass" | python3 -c '
import json, sys
print(json.dumps({"password": sys.stdin.read()}))' \
            | docker exec -i dify-api curl -s -c /tmp/dify_setup.jar \
            -X POST http://localhost:5001/console/api/init \
            -H "Content-Type: application/json" \
            -d @- 2>&1)
        print_info "Init response: $init_resp"
        
        # Step 2: Create admin user (with session cookie from step 1)
        # NOTE: setup endpoint expects PLAINTEXT password (no base64 encoding)
        local setup_resp
        setup_resp=$(printf '%s' "$admin_pass" | DIFY_EMAIL="$admin_email" python3 -c '
import json, os, sys
print(json.dumps({"email": os.environ["DIFY_EMAIL"], "name": "razzfazz.ai Admin",
                  "password": sys.stdin.read()}))' \
            | docker exec -i dify-api curl -s -b /tmp/dify_setup.jar \
            -X POST http://localhost:5001/console/api/setup \
            -H "Content-Type: application/json" \
            -d @- 2>&1)
        print_info "Setup response: $setup_resp"
        sleep 2

        # Verify the two-step setup actually took. A transient during init/setup
        # (e.g. dify-api still warming) otherwise leaves Dify at "not_started",
        # and every subsequent login 401s "not_init_validated". Retry the
        # init+setup pair once before falling through to the login attempts.
        local setup_after
        setup_after=$(docker exec dify-api curl -s http://localhost:5001/console/api/setup 2>/dev/null | \
            python3 -c "import sys,json; print(json.load(sys.stdin).get('step',''))" 2>/dev/null)
        if [ "$setup_after" != "finished" ]; then
            print_substep "Dify setup did not complete on first attempt — retrying init+setup once..."
            # #1164: same stdin transport as the first attempt above.
            printf '%s' "$init_pass" | python3 -c '
import json, sys
print(json.dumps({"password": sys.stdin.read()}))' \
                | docker exec -i dify-api curl -s -c /tmp/dify_setup.jar \
                -X POST http://localhost:5001/console/api/init \
                -H "Content-Type: application/json" \
                -d @- >/dev/null 2>&1
            printf '%s' "$admin_pass" | DIFY_EMAIL="$admin_email" python3 -c '
import json, os, sys
print(json.dumps({"email": os.environ["DIFY_EMAIL"], "name": "razzfazz.ai Admin",
                  "password": sys.stdin.read()}))' \
                | docker exec -i dify-api curl -s -b /tmp/dify_setup.jar \
                -X POST http://localhost:5001/console/api/setup \
                -H "Content-Type: application/json" \
                -d @- >/dev/null 2>&1
            sleep 2
        fi
    fi
    
    # Try login with current domain email — DIFY_ADMIN_PASSWORD first, then the
    # bootstrap fallback for accounts seeded before #952 (see
    # _dify_admin_passwords). A hit here means the box is already consistent, so
    # nothing is written: the self-heal below is for accounts we CANNOT reach.
    # #1739: only the PRIMARY candidate means agreement. A login with a LATER
    # candidate says the account is reachable and on a password `.env` does not
    # name — the state agent-seqis measured on 0.79, where DIFY_ADMIN_PASSWORD
    # did not verify against the live hash while every other app did. Returning
    # here leaves an operator holding the right value out of `.env` and unable
    # to log in, with the one explanation they can rule out (noted it down
    # wrong) being the only one that is true of the file. So a fallback hit
    # falls THROUGH to the converge below, which is what the comment on
    # `admin_pw_candidates[0]` promises.
    local _dify_pw _dify_i=0 _dify_converge=false
    for _dify_pw in "${admin_pw_candidates[@]}"; do
        if dify_login "$admin_email" "$_dify_pw"; then
            if [ "$_dify_i" -eq 0 ]; then
                print_success "Logged in to Dify as $admin_email."
                set -e
                return 0
            fi
            print_substep "Dify admin answers to the fallback password, not to the one .env names — converging it onto DIFY_ADMIN_PASSWORD."
            _dify_converge=true
            break
        fi
        _dify_i=$((_dify_i + 1))
    done

    # Try with old domain (existing installation) — skipped once we already know
    # the account is reachable and needs converging (#1739).
    local existing_email
    [ "$_dify_converge" = true ] || existing_email=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${DIFY_DB:-dify_db}" -t -c \
        "SELECT email FROM accounts LIMIT 1;" 2>/dev/null | xargs)

    if [ -n "$existing_email" ] && [ "$existing_email" != "$admin_email" ]; then
        print_info "Found existing Dify admin: $existing_email"
        _dify_i=0
        for _dify_pw in "${admin_pw_candidates[@]}"; do
            if dify_login "$existing_email" "$_dify_pw"; then
                if [ "$_dify_i" -eq 0 ]; then
                    print_success "Logged in to Dify as $existing_email."
                    set -e
                    return 0
                fi
                # #1739, same rule for the old-domain account.
                print_substep "Dify admin ($existing_email) answers to the fallback password — converging it onto DIFY_ADMIN_PASSWORD."
                break
            fi
            _dify_i=$((_dify_i + 1))
        done
    fi

    # Last resort: reset password in DB using Dify's own password hashing.
    # Mechanically unchanged by #1139 — but `$admin_pass` is now the PRIMARY
    # candidate (DIFY_ADMIN_PASSWORD when set), so an unreachable account is
    # converged onto what .env claims instead of away from it.
    print_substep "Resetting Dify admin password via database..."
    local target_email="${existing_email:-${admin_email}}"
    
    # #1164: the plaintext arrives on STDIN — not inside the Python source in
    # docker-exec argv (host ps / audit), where a quote also broke the program.
    printf '%s' "$admin_pass" | docker exec -i dify-api python3 -c "
import base64, hashlib, binascii, os, sys
pw = sys.stdin.buffer.read()
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha256', pw, salt, 10000)
print(base64.b64encode(binascii.hexlify(dk)).decode())
print(base64.b64encode(salt).decode())
" 2>/dev/null | {
        read -r pass_hash
        read -r salt_b64
        if [ -n "$pass_hash" ] && [ -n "$salt_b64" ]; then
            docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${DIFY_DB:-dify_db}" -c \
                "UPDATE accounts SET password='${pass_hash}', password_salt='${salt_b64}' WHERE email='${target_email}';" > /dev/null 2>&1
            if [ "$target_email" != "$admin_email" ]; then
                docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${DIFY_DB:-dify_db}" -c \
                    "UPDATE accounts SET email='${admin_email}' WHERE email='${target_email}';" > /dev/null 2>&1
            fi
        fi
    }
    
    sleep 1
    if dify_login "$admin_email" "$admin_pass"; then
        print_success "Dify admin password reset and logged in."
        set -e
        return 0
    fi
    
    print_error "Failed to authenticate with Dify."
    set -e
    return 1
}

# Resolve the EXACT marketplace package identifier (name:version@checksum) for a
# PINNED plugin version via the Dify marketplace REST API, queried from inside
# dify-api. This REPLACES the console marketplace-search route that Dify 1.16
# removed (see dify_install_plugin). We pin the *version* (deliberate — e.g.
# gpustack must be >=0.0.15 for the thinking-param passthrough) but resolve the
# package *hash* dynamically, because the marketplace can RE-PUBLISH a version
# with a new checksum (it re-signed gpustack 0.0.15 on 2026-07-16, invalidating
# our old pin hash → install 400 "file not found" → the "zero Dify models"
# defect, #116). Empty output on any error → caller uses the hardcoded fallback.
_dify_marketplace_pinned_identifier() {
    local name="$1" version="$2"
    [ -n "$name" ] && [ -n "$version" ] || return 0
    local mkt
    mkt=$(docker exec dify-api sh -c 'printf %s "${MARKETPLACE_API_URL:-https://marketplace.dify.ai}"' 2>/dev/null)
    [ -n "$mkt" ] || mkt="https://marketplace.dify.ai"
    docker exec dify-api sh -c "curl -s --max-time 15 '${mkt}/api/v1/plugins/${name}/${version}'" 2>/dev/null | \
        python3 -c "
import sys, json
try:
    v = json.load(sys.stdin).get('data', {}).get('version', {})
    ident = v.get('unique_identifier') or ''
    if not ident and v.get('plugin_tuple') and v.get('checksum'):
        ident = '%s@%s' % (v['plugin_tuple'], v['checksum'])
    if ident:
        print(ident)
except Exception:
    pass
" 2>/dev/null
}

# #1972: make the plugin pins above mean something. Dify's own default for the
# MODEL plugin category is LATEST (every other category is fix_only —
# api/services/plugin/plugin_auto_upgrade_service.py, and migration
# 2026_06_15_1200 stamps that default onto every tenant that never touched it),
# so the versions dify_install_plugin pins are install-time floors: 0.91 already
# ran openai_api_compatible 0.0.66 and gpustack 0.0.16 BEFORE #1968 pinned them.
# Operator decision 2026-09-12: fix_only for model plugins too. Keyed on
# (tenant_id, category) — one row per tenant per category, UNIQUE — so this is
# one idempotent UPDATE for every tenant, plus a row for any tenant that has
# none (an ABSENT row means "default", and the default is LATEST).
dify_pin_model_plugin_strategy() {
    local _psql="docker exec postgres psql -U ${POSTGRES_USER:-docker} -d ${DIFY_DB:-dify_db} -tAc"
    local off_default missing
    off_default=$($_psql "select count(*) from tenant_plugin_auto_upgrade_strategies where category='model' and strategy_setting<>'fix_only';" 2>/dev/null | tr -d '[:space:]')
    missing=$($_psql "select count(*) from tenants t where not exists (select 1 from tenant_plugin_auto_upgrade_strategies s where s.tenant_id=t.id and s.category='model');" 2>/dev/null | tr -d '[:space:]')
    if [ -z "$off_default" ] || [ -z "$missing" ]; then
        print_warning "Dify: could not read tenant_plugin_auto_upgrade_strategies — model plugins may still auto-upgrade to latest and the pins above are install-time values only (#1972)."
        return 0
    fi
    if [ "$off_default" = "0" ] && [ "$missing" = "0" ]; then
        print_substep "Dify: model plugin auto-upgrade is fix_only for every tenant (#1972)."
        return 0
    fi
    if [ "$off_default" != "0" ]; then
        $_psql "update tenant_plugin_auto_upgrade_strategies set strategy_setting='fix_only', updated_at=now() where category='model' and strategy_setting<>'fix_only';" >/dev/null 2>&1 || true
    fi
    if [ "$missing" != "0" ]; then
        $_psql "insert into tenant_plugin_auto_upgrade_strategies (id, tenant_id, strategy_setting, upgrade_time_of_day, upgrade_mode, exclude_plugins, include_plugins, created_at, updated_at, category) select gen_random_uuid(), t.id, 'fix_only', 0, 'exclude', '[]', '[]', now(), now(), 'model' from tenants t where not exists (select 1 from tenant_plugin_auto_upgrade_strategies s where s.tenant_id=t.id and s.category='model');" >/dev/null 2>&1 || true
    fi
    local still
    still=$($_psql "select (select count(*) from tenant_plugin_auto_upgrade_strategies where category='model' and strategy_setting<>'fix_only') + (select count(*) from tenants t where not exists (select 1 from tenant_plugin_auto_upgrade_strategies s where s.tenant_id=t.id and s.category='model'));" 2>/dev/null | tr -d '[:space:]')
    if [ "$still" = "0" ]; then
        print_substep "Dify: model plugin auto-upgrade set to fix_only (${off_default} tenant(s) were on LATEST, ${missing} had no row) — the pins above now mean the version in service (#1972)."
    else
        print_warning "Dify: ${still:-?} tenant(s) still let model plugins auto-upgrade — the UPDATE did not take; check tenant_plugin_auto_upgrade_strategies by hand (#1972)."
    fi
}

# #2222 — install a Dify plugin from the offline package's plugins/dify/ tree.
# $1 = <vendor>/<name>. Returns 0 when installed or already installed, 0 with a
# stated skip when the package carries no plugins, 1 when the package carries
# the plugin and the install failed.
#
# Route, measured on 0.175 (2026-09-16): `install/pkg` resolves its identifier
# against plugin_declarations in dify_plugin_db and NEVER looks at the daemon's
# file cache — a file placed at the identifier's path answered 500 "plugin not
# found / failed to get plugin declaration". Only the UPLOAD path writes the
# declaration row, so the file is uploaded through the console
# (`/plugin/upload/pkg`, multipart) and the identifier the upload returns is
# what `install/pkg` is given. The identifier in the file's path is a NAME (its
# digest is not a hash of the file); integrity is the package's MANIFEST.sha256.
_dify_api_upload_pkg() {
    # $1 = path of the .difypkg inside dify-api → the upload response (JSON)
    docker exec dify-api curl -s -X POST \
        -H "Cookie: access_token=${DIFY_ACCESS}; csrf_token=${DIFY_CSRF}" \
        -H "X-CSRF-Token: ${DIFY_CSRF}" \
        -F "pkg=@$1" "http://localhost:5001/console/api/workspaces/current/plugin/upload/pkg" 2>/dev/null
}

# #2320 — the pin reconcile. A package upgrade left an installed plugin at its
# old version (0.79, 2026-09-20, 2026.08-ga.15 -> 2026.09-rc13:
# openai_api_compatible 0.0.34 kept while the package carried 0.0.66): the
# package path returned on "already installed" without looking at the version,
# and the marketplace path issued a plain install/marketplace over the old
# version, which the daemon refuses INSIDE the task (curd.InstallPlugin:
# ErrPluginAlreadyInstalled, keyed on tenant + plugin_id) while the caller only
# reported "initiation". The pin is the version in service (#1972), so an
# installed plugin is reconciled to it, on both paths:
#   installed at the pin  -> untouched, no install call;
#   installed elsewhere   -> upgraded to the pin. The daemon's install/upgrade
#                            installs the NEW runtime first and swaps the
#                            tenant's installation only on success, so a
#                            failed build leaves the old version in service;
#   not installed         -> installed at the pin (as before).
# Every outcome is one line naming the plugin, was, now.

# "<vendor>/<name>:<version>@<digest>" -> "<version>"; no ':' -> empty.
_dify_plugin_version() {
    local v="${1#*:}"
    [ "$v" != "$1" ] || v=""
    printf '%s' "${v%%@*}"
}

# The tenant's installed identifier for a plugin, from the console's list —
# the one place both install paths consulted (plugin_id first, the bare name
# as the fallback the package path always had). Empty when not installed.
_dify_plugin_installed_id() {
    local search_name="$1"
    dify_api GET "/console/api/workspaces/current/plugin/list?page=1&page_size=100" | \
        python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    for p in d.get('plugins',[]):
        if p.get('plugin_id')=='${search_name}' or p.get('name')=='${search_name}'.split('/')[-1]:
            u=p.get('plugin_unique_identifier') or ''
            if not u and p.get('version'): u='%s:%s' % (p.get('plugin_id',''), p['version'])
            print(u); break
except Exception: pass" 2>/dev/null || true
}

# The per-plugin outcome line (#2320): $1 = name, $2 = was, $3 = now, $4 = why.
_dify_plugin_outcome() {
    print_info "Dify plugin '$1': was ${2:-none}, now ${3:-none} — $4 (#2320)"
}

# Poll an install/upgrade task to its verdict: 0 success, 1 failed (the
# daemon's message in _DIFY_PLUGIN_TASK_DETAIL), 2 not settled in the poll
# budget. The task, not the declaration row, is the verdict (0.175, #2222).
_DIFY_PLUGIN_TASK_DETAIL=""
_dify_plugin_task_wait() {
    local task_id="$1" i status
    _DIFY_PLUGIN_TASK_DETAIL=""
    for i in $(seq 1 "${RZFZ_DIFY_PLUGIN_TASK_POLLS:-24}"); do
        sleep "${RZFZ_DIFY_PLUGIN_TASK_POLL_SECONDS:-5}"
        status=$(dify_api GET "/console/api/workspaces/current/plugin/tasks/${task_id}" | python3 -c "import sys,json
try:
    d=json.load(sys.stdin); t=d.get('task', d) or {}
    s=t.get('status') or ''
    msgs='; '.join((p.get('message') or '') for p in (t.get('plugins') or []) if p.get('message'))
    print(s + '\t' + msgs)
except Exception: print('')" 2>/dev/null || true)
        _DIFY_PLUGIN_TASK_DETAIL="${status#*	}"; status="${status%%	*}"
        case "$status" in
            success) return 0 ;;
            failed)  return 1 ;;
        esac
    done
    return 2
}

# Swap the tenant's installation from one identifier to another through the
# daemon's own upgrade route — the request Dify's PluginInstaller.upgrade_plugin
# sends, issued from inside dify-api with the daemon URL and key it holds. The
# console's upgrade/marketplace wrapper is not used on the package path: it
# posts an install event to marketplace.dify.ai before it reaches the daemon
# and raises when that call fails, which on an air-gapped box it does. Prints
# the task id, or DONE when the daemon swapped synchronously; on any refusal
# prints "ERR<TAB><the daemon's message>" and returns 1 (stdout, because the
# caller reads it through a command substitution, where a variable set here
# would die with the subshell).
_dify_plugin_upgrade_via_daemon() {
    local old_id="$1" new_id="$2" tenant resp out
    tenant=$(dify_api GET "/console/api/workspaces/current" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('id') or '')
except Exception: print('')" 2>/dev/null || true)
    [ -n "$tenant" ] || tenant=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${DIFY_DB:-dify_db}" -tAc \
        "select id from tenants limit 1;" 2>/dev/null | tr -d '[:space:]' || true)
    if [ -z "$tenant" ]; then
        printf 'ERR\tcould not resolve the Dify tenant'
        return 1
    fi
    resp=$(printf '{"original_plugin_unique_identifier":"%s","new_plugin_unique_identifier":"%s","source":"package","meta":{"plugin_unique_identifier":"%s"}}' \
            "$old_id" "$new_id" "$new_id" | \
        docker exec -i dify-api sh -c 'curl -s --max-time 60 -X POST -H "X-Api-Key: ${PLUGIN_DAEMON_KEY}" -H "Content-Type: application/json" -d @- "${PLUGIN_DAEMON_URL:-http://dify-plugin-daemon:5002}/plugin/$1/management/install/upgrade"' _ "$tenant" 2>/dev/null || true)
    out=$(printf '%s' "$resp" | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    if d.get('code',0)!=0:
        print('ERR\t' + (d.get('message') or 'daemon error'))
    else:
        t=d.get('data') or {}
        print('DONE' if t.get('all_installed') else (t.get('task_id') or ''))
except Exception: print('')" 2>/dev/null || true)
    case "$out" in
        ERR*) printf '%s' "$out"; return 1 ;;
        "")   printf 'ERR\tunparseable answer: %s' "$(printf '%s' "$resp" | head -c 200)"; return 1 ;;
    esac
    printf '%s' "$out"
}

# Does the package at APPLIANCE_OFFLINE_PKG carry this plugin? (index read
# only, no extraction) — the operator's package-first rule: what the package
# carries is used regardless of RAZZFAZZ_NETWORK_MODE; the mode decides only
# whether the marketplace may be asked for what the package lacks.
_dify_package_carries_plugin() {
    local search_name="$1" pkg="${APPLIANCE_OFFLINE_PKG:-}" _idx=""
    [ -n "$pkg" ] && [ -f "$pkg" ] || return 1
    _idx="$(appliance_pkg_index "$pkg")" || return 1
    grep -q "plugins/dify/${search_name}:" "$_idx" 2>/dev/null
}

_dify_install_plugin_from_package() {
    local search_name="$1" pkg="${APPLIANCE_OFFLINE_PKG:-}" _idx="" _prefix="" tmp="" file="" rel=""
    if [ -z "$pkg" ] || [ ! -f "$pkg" ]; then
        print_info "Offline box and no offline package at APPLIANCE_OFFLINE_PKG — Dify plugin '${search_name}' not installed (#2222)."
        return 0
    fi
    _idx="$(appliance_pkg_index "$pkg")" || _idx=""
    if [ -z "$_idx" ] || ! _prefix="$(appliance_pkg_prefix "$_idx" plugins)"; then
        print_warning "Offline package carries no plugins/ subtree — Dify plugin '${search_name}' not installed; this box has NO model provider until a package built with the daemon's plugin cache is applied (#2222)."
        return 0
    fi
    tmp="$(mktemp -d /var/tmp/rzfz-dify-plugins.XXXXXX 2>/dev/null || mktemp -d)" || return 1
    if ! tar xzf "$pkg" -C "$tmp" "$_prefix" 2>/dev/null; then
        print_warning "Could not extract ${_prefix} from the offline package — Dify plugin '${search_name}' not installed (#2222)."
        rm -rf "$tmp"; return 1
    fi
    # the file's path relative to plugins/dify names the plugin: <vendor>/<name>:<version>@<digest>
    file="$(find "$tmp" -type f -path "*plugins/dify/${search_name}:*" | head -1)"
    if [ -z "$file" ]; then
        print_warning "Offline package carries plugins/ but not '${search_name}' — not installed; the package was built on a box without it (#2222)."
        rm -rf "$tmp"; return 0
    fi
    rel="${file#*plugins/dify/}"
    # The daemon builds the plugin's Python environment at install time with uv
    # and fetches from PyPI whatever its cache lacks — under a real air gap the
    # task dies ("failed to init environment") unless the cache is seeded. The
    # package carries the daemon's own cache (plugins/dify-uv-cache); it is
    # restored ONCE per run, BEFORE the first upload, because the build is part
    # of the install task. Measured on 0.175: cache alone → success in 8 s.
    if [ "${_RZFZ_DIFY_WHEELHOUSE_STAGED:-0}" != "1" ]; then
        # #2272: the wheelhouse is what the daemon's uv resolves from on an
        # air-gapped box (the bound uv.toml names it with no-index); the cache
        # below is kept for the `uv venv` step only.
        local wh_dir wroot
        wh_dir="$(find "$tmp" -type d -path "*plugins/dify-wheelhouse" | head -1)"
        if [ -n "$wh_dir" ] && [ -n "$(ls -A "$wh_dir" 2>/dev/null)" ]; then
            wroot="$(docker exec dify-plugin-daemon sh -c 'printf %s "${PLUGIN_STORAGE_LOCAL_ROOT:-/app/storage}"' 2>/dev/null)"; wroot="${wroot:-/app/storage}"
            if docker exec dify-plugin-daemon mkdir -p "${wroot}/cwd/.wheelhouse" 2>/dev/null \
               && docker cp "${wh_dir}/." "dify-plugin-daemon:${wroot}/cwd/.wheelhouse/" 2>/dev/null; then
                print_substep "Staged the plugin daemon's wheelhouse from the offline package (${wroot}/cwd/.wheelhouse, $(find "$wh_dir" -type f -name '*.whl' | wc -l | tr -d ' ') wheels) — the plugins resolve from it and from nothing else (#2272)."
                _RZFZ_DIFY_WHEELHOUSE_STAGED=1
            else
                print_warning "Could not stage the plugin daemon's wheelhouse from the package — the plugin install will not resolve offline (#2272)."
            fi
        else
            print_warning "Offline package carries no plugins/dify-wheelhouse — built before #2272; the plugin install will not resolve offline."
            _RZFZ_DIFY_WHEELHOUSE_STAGED=1   # say it once, not per plugin
        fi
    fi
    if [ "${_RZFZ_DIFY_UV_CACHE_SEEDED:-0}" != "1" ]; then
        local cache_dir root
        cache_dir="$(find "$tmp" -type d -path "*plugins/dify-uv-cache" | head -1)"
        if [ -n "$cache_dir" ] && [ -n "$(ls -A "$cache_dir" 2>/dev/null)" ]; then
            root="$(docker exec dify-plugin-daemon sh -c 'printf %s "${PLUGIN_STORAGE_LOCAL_ROOT:-/app/storage}"' 2>/dev/null)"; root="${root:-/app/storage}"
            if docker exec dify-plugin-daemon mkdir -p "${root}/cwd/.uv-cache" 2>/dev/null \
               && docker cp "${cache_dir}/." "dify-plugin-daemon:${root}/cwd/.uv-cache/" 2>/dev/null; then
                print_substep "Seeded the plugin daemon's uv cache from the offline package (${root}/cwd/.uv-cache) — the plugins' Python environments build without PyPI (#2222)."
                _RZFZ_DIFY_UV_CACHE_SEEDED=1
            else
                print_warning "Could not seed the plugin daemon's uv cache from the package — the plugin install will need PyPI, which an air-gapped box does not have (#2222)."
            fi
        else
            print_warning "Offline package carries no plugins/dify-uv-cache — the plugin install will need PyPI, which an air-gapped box does not have; expect 'failed to init environment' (#2222)."
            _RZFZ_DIFY_UV_CACHE_SEEDED=1   # say it once, not per plugin
        fi
    fi
    # #2320: reconcile to the package's version — presence alone is not the
    # answer. The version is compared, not the digest: the path's digest is a
    # name the daemon minted on the build box and the marketplace re-signs
    # packages (#116); the pin is the version (#1972).
    local installed_id installed_ver pkg_ver
    installed_id=$(_dify_plugin_installed_id "$search_name")
    installed_ver=$(_dify_plugin_version "$installed_id")
    pkg_ver=$(_dify_plugin_version "$rel")
    if [ -n "$installed_id" ] && [ "$installed_ver" = "$pkg_ver" ]; then
        print_info "Plugin '${search_name}' already installed at the package pin (${installed_id})."
        _dify_plugin_outcome "$search_name" "$installed_ver" "$installed_ver" "at the package pin, untouched"
        rm -rf "$tmp"; return 0
    fi
    if [ -n "$installed_id" ]; then
        print_substep "Upgrading plugin '${search_name}' from the offline package (${installed_id} -> ${rel})..."
    else
        print_substep "Installing plugin '${search_name}' from the offline package (${rel})..."
    fi
    local inbox="/tmp/rzfz-plugins" base
    base="$(basename "$file")"
    if ! docker exec dify-api mkdir -p "$inbox" 2>/dev/null || ! docker cp "$file" "dify-api:${inbox}/${base}" 2>/dev/null; then
        print_warning "Could not place '${rel}' into dify-api:${inbox} — plugin not installed (#2222)."
        rm -rf "$tmp"; return 1
    fi
    rm -rf "$tmp"
    local up ident
    up=$(_dify_api_upload_pkg "${inbox}/${base}")
    ident=$(printf '%s' "$up" | python3 -c "import sys,json
try:
    d=json.load(sys.stdin); print(d.get('unique_identifier') or d.get('plugin_unique_identifier') or '')
except Exception: print('')" 2>/dev/null)
    docker exec dify-api rm -f "${inbox}/${base}" >/dev/null 2>&1 || true
    if [ -z "$ident" ]; then
        print_warning "Plugin '${search_name}': the upload of ${rel} returned no identifier — $(printf '%s' "$up" | head -c 200)"
        return 1
    fi
    local resp task_id rc
    if [ -n "$installed_id" ]; then
        # #2320: an installed plugin is SWAPPED to the uploaded file through the
        # daemon's upgrade route; install/pkg over an installed plugin_id fails
        # inside the task ("plugin already installed").
        task_id=$(_dify_plugin_upgrade_via_daemon "$installed_id" "$ident") || true
        case "$task_id" in ""|ERR*)
            print_warning "Plugin '${search_name}': the daemon refused the upgrade ${installed_id} -> ${ident}: ${task_id#ERR	} (#2320)"
            _dify_plugin_outcome "$search_name" "$installed_ver" "$installed_ver" "upgrade refused, the installed version stays"
            return 1 ;;
        esac
    else
        resp=$(dify_api POST "/console/api/workspaces/current/plugin/install/pkg" \
            "{\"plugin_unique_identifiers\": [\"${ident}\"]}")
        task_id=$(printf '%s' "$resp" | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    print('DONE' if d.get('all_installed') else (d.get('task_id') or ''))
except Exception: print('')" 2>/dev/null || true)
        if [ -z "$task_id" ]; then
            print_warning "Plugin '${search_name}' install from the offline package answered: $(echo "$resp" | head -c 200)"
            return 1
        fi
        if [ "$task_id" = "DONE" ]; then
            print_success "Plugin '${search_name}' already installed (${ident})."
            _dify_plugin_outcome "$search_name" "" "$pkg_ver" "installed at the package pin"
            return 0
        fi
    fi
    # The task, not the declaration row, is the verdict (0.175 under a real air
    # gap): the declaration row survives a failed install, and "task created"
    # is not "installed" — the daemon builds the plugin's Python environment
    # after the task starts and reports the failure IN the task.
    rc=0
    [ "$task_id" = "DONE" ] || _dify_plugin_task_wait "$task_id" || rc=$?
    case "$rc" in
        0)
            if [ -n "$installed_id" ]; then
                print_success "Plugin '${search_name}' upgraded from the offline package (${installed_id} -> ${ident})."
                _dify_plugin_outcome "$search_name" "$installed_ver" "$pkg_ver" "upgraded to the package pin"
            else
                print_success "Plugin '${search_name}' installed from the offline package (${ident})."
                _dify_plugin_outcome "$search_name" "" "$pkg_ver" "installed at the package pin"
            fi
            return 0 ;;
        1)
            if [ -n "$installed_id" ]; then
                print_warning "Plugin '${search_name}' upgrade FAILED in the daemon: ${_DIFY_PLUGIN_TASK_DETAIL:-no message} — the installed version stays in service (#2320)"
                _dify_plugin_outcome "$search_name" "$installed_ver" "$installed_ver" "upgrade failed, the installed version stays"
            else
                print_warning "Plugin '${search_name}' install FAILED in the daemon: ${_DIFY_PLUGIN_TASK_DETAIL:-no message} (#2222)"
            fi
            print_info "  The plugin's Python environment is built at install time; on an air-gapped box its dependencies must come from the package's seeded cache."
            return 1 ;;
    esac
    if [ -n "$installed_id" ]; then
        print_warning "Plugin '${search_name}' upgrade task ${task_id} did not settle in time — not reporting it upgraded (#2320)."
        _dify_plugin_outcome "$search_name" "$installed_ver" "$installed_ver" "upgrade unsettled, the installed version is what the list shows"
    else
        print_warning "Plugin '${search_name}' install task ${task_id} did not settle in time — not reporting it installed (#2222)."
    fi
    return 1
}

dify_install_plugin() {
    local search_name="$1"

    # #184 WS2b: resolving + installing a plugin queries the Dify marketplace
    # (marketplace.dify.ai) and fetches the package → internet egress. On an
    # air-gapped box the plugin comes from the offline package instead (#2222):
    # journey B measured a fresh offline install with every belt armed and no
    # model provider at all, because this used to skip and nothing else loaded
    # the plugin. The package's plugins/dify/ tree carries the daemon's own
    # package cache (path = identifier, used as a NAME) and plugins/dify-uv-cache
    # the daemon's dependency cache; the cache is seeded, the file is UPLOADED
    # through the console and installed by the identifier the upload returns,
    # and the install task is the verdict — no marketplace call.
    # #2320 + the operator's package-first rule: a plugin the package carries is
    # used regardless of RAZZFAZZ_NETWORK_MODE — the mode decides only whether
    # the marketplace may be asked for a plugin the package lacks.
    if _dify_package_carries_plugin "$search_name"; then
        print_substep "Plugin '${search_name}': the package at ${APPLIANCE_OFFLINE_PKG} carries it — package-first, the marketplace is not asked (#2320)."
        _dify_install_plugin_from_package "$search_name"
        return $?
    fi
    if razzfazz_offline_skip "Dify plugin install from marketplace ('${search_name}') — installing it from the offline package instead"; then
        _dify_install_plugin_from_package "$search_name"
        return $?
    fi

    # Per-plugin PINNED VERSION + a full offline fallback identifier
    # (name:version@checksum). We pin the version deliberately — gpustack MUST be
    # >=0.0.15 for correct thinking-param passthrough
    # (chat_template_kwargs.enable_thinking); 0.0.8 silently drops it, breaking
    # structured-extraction workflows.
    local pin_version pin_fallback
    case "$search_name" in
        langgenius/gpustack)
            # #1968: 0.0.15 -> 0.0.16 (Marktplatz-Stand 2026-09-12). Icons +
            # SDK-Abhaengigkeiten; keine Aenderung an den Zugangsdatenfeldern.
            pin_version="0.0.16"
            pin_fallback="langgenius/gpustack:0.0.16@f288904dcea6f54811d7f6795aacdc395368556163bca1253fa2f2eca8c21b30" ;;
        langgenius/openai_api_compatible)
            # #1968: 0.0.34 -> 0.0.66, 32 Versionen. Das ist der Provider, ueber
            # den JEDE Modellregistrierung dieses Stacks laeuft, und der Abstand
            # war entsprechend teuer.
            #
            # Vertraeglichkeit GEPRUEFT, nicht angenommen: beide Pakete geholt
            # und `provider/openai_api_compatible.yaml` verglichen. 0.0.66
            # verlangt genau drei Pflichtfelder — endpoint_url (immer),
            # context_size (llm/text-embedding/rerank) und max_chunks
            # (text-embedding, #1509). Alle drei sendet _dify_register_models
            # bereits. Jedes der 16 neuen Felder ist optional mit Vorgabewert.
            #
            # Nebenbefund, der hierher gehoert: `max_chunks` steht in 0.0.34
            # UEBERHAUPT NICHT im Schema, in 0.0.66 schon. #1509 wurde auf 0.175
            # gegen ein Plugin gemessen, das es verlangte — also gegen eine
            # NEUERE Version als diesen Pin. Auf mindestens einer Box lief nicht
            # die gepinnte Version. Ungemessen, in #1968 festgehalten.
            pin_version="0.0.66"
            pin_fallback="langgenius/openai_api_compatible:0.0.66@51801b51069b2578c3fcf6d38edc45c45efd8b7ef4fdb25f096bd84d4f95a53d" ;;
        abesticode/knowledge_pro)
            pin_version="0.0.6"
            pin_fallback="abesticode/knowledge_pro:0.0.6@73873eda46ee70c8d528c409381f705f24206d34f913ac4bfee35812c6d4a6e2" ;;
        junjiem/mcp_sse)
            pin_version="0.2.3"
            pin_fallback="junjiem/mcp_sse:0.2.3@ce2b8fa30300474f51da7e7d38543a2d15f4b720aee305e1dad0a8fdf27b9892" ;;
        *)
            pin_version=""; pin_fallback="" ;;
    esac

    # Resolve the TARGET identifier FIRST (before the installed-check) so we can
    # upgrade a stale plugin, not just skip-if-present.
    #
    # Dify 1.16 REMOVED the console marketplace-SEARCH proxy route
    # (/console/api/workspaces/current/plugin/market/plugins → HTTP 404); only the
    # per-identifier /plugin/marketplace/pkg fetch and /plugin/install/marketplace
    # remain. The old console-search "resolve latest identifier" therefore returned
    # EMPTY on 1.16, so every install fell through to the pinned id — and the
    # gpustack pin's hash had gone stale (re-published 0.0.15), so
    # install/marketplace 400'd "file not found" and langgenius/gpustack never
    # installed → zero Dify models (#116). Resolve the exact package identifier for
    # the pinned version straight from the marketplace REST API (self-heals a
    # re-signed package hash); fall back to the hardcoded pin only when the
    # marketplace is unreachable (air-gapped/customer boxes).
    local plugin_id
    plugin_id=$(_dify_marketplace_pinned_identifier "$search_name" "$pin_version")
    [ -n "$plugin_id" ] || plugin_id="$pin_fallback"

    if [ -z "$plugin_id" ]; then
        print_error "Failed to resolve a version for plugin '$search_name'"
        return 1
    fi

    # Version-aware (#2320): installed at the pinned VERSION -> untouched (the
    # digest is not compared — the marketplace re-signs packages, #116);
    # installed at another version -> the console's upgrade route, which swaps
    # the tenant's installation (a plain install/marketplace over an installed
    # plugin_id fails inside the daemon's task with "plugin already installed",
    # and this path never read the task); not installed -> install as before.
    local installed_id installed_ver pin_ver
    installed_id=$(_dify_plugin_installed_id "$search_name")
    installed_ver=$(_dify_plugin_version "$installed_id")
    pin_ver=$(_dify_plugin_version "$plugin_id")
    if [ -n "$installed_id" ] && [ "$installed_ver" = "$pin_ver" ]; then
        print_info "Plugin '$search_name' already at target version ($installed_id)."
        _dify_plugin_outcome "$search_name" "$installed_ver" "$installed_ver" "at the pin, untouched"
        return 0
    elif [ -n "$installed_id" ]; then
        print_substep "Upgrading plugin '$search_name' from the marketplace ($installed_id -> $plugin_id)..."
        local resp task_id rc
        resp=$(dify_api POST "/console/api/workspaces/current/plugin/upgrade/marketplace" \
            "{\"original_plugin_unique_identifier\":\"${installed_id}\",\"new_plugin_unique_identifier\":\"${plugin_id}\"}")
        task_id=$(printf '%s' "$resp" | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    print('DONE' if d.get('all_installed') else (d.get('task_id') or ''))
except Exception: print('')" 2>/dev/null || true)
        if [ -z "$task_id" ]; then
            print_warning "Plugin '$search_name' upgrade from the marketplace answered: $(echo "$resp" | head -c 200) (#2320)"
            _dify_plugin_outcome "$search_name" "$installed_ver" "$installed_ver" "upgrade refused, the installed version stays"
            return 1
        fi
        rc=0
        [ "$task_id" = "DONE" ] || _dify_plugin_task_wait "$task_id" || rc=$?
        case "$rc" in
            0) print_success "Plugin '$search_name' upgraded from the marketplace (${installed_id} -> ${plugin_id})."
               _dify_plugin_outcome "$search_name" "$installed_ver" "$pin_ver" "upgraded to the pin from the marketplace"
               return 0 ;;
            1) print_warning "Plugin '$search_name' upgrade FAILED in the daemon: ${_DIFY_PLUGIN_TASK_DETAIL:-no message} — the installed version stays in service (#2320)"
               _dify_plugin_outcome "$search_name" "$installed_ver" "$installed_ver" "upgrade failed, the installed version stays"
               return 1 ;;
        esac
        print_warning "Plugin '$search_name' upgrade task ${task_id} did not settle in time — not reporting it upgraded (#2320)."
        _dify_plugin_outcome "$search_name" "$installed_ver" "$installed_ver" "upgrade unsettled, the installed version is what the list shows"
        return 1
    fi

    print_substep "Installing plugin '$search_name' ($plugin_id)..."
    local resp
    resp=$(dify_api POST "/console/api/workspaces/current/plugin/install/marketplace" \
        "{\"plugin_unique_identifiers\":[\"${plugin_id}\"]}")

    # Check for success (the response contains task info)
    if echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'all_installed' in d or 'task_id' in str(d)" 2>/dev/null; then
        print_success "Plugin '$search_name' installation initiated."
        _dify_plugin_outcome "$search_name" "" "$pin_ver" "install initiated from the marketplace, the task is not awaited here"
    else
        print_warning "Plugin '$search_name' install response: $(echo "$resp" | head -c 200)"
    fi
}

# ── #160: Dify model-provider icons render as broken images ──────────────────
# The provider list / model-config screens render each provider icon as a plain
#   <img src="{CONSOLE_API_URL}/console/api/workspaces/<tenant>/model-providers/
#            <provider>/icon_small/<lang>">
# dify-api serves those bytes from the dify-plugin-daemon (content-hash assets).
# The WHOLE serving chain — the frontend <img>, the CONSOLE_API_URL absolute-URL
# builder (services/entities/model_provider_entities.py ProviderResponse), the
# UNAUTHENTICATED <path:provider> icon route, and Caddy's /console/api/*
# SSO-exemption — is byte-identical & correct 1.14.2 -> 1.16.0 and verified
# working live (see modules/dify/Dockerfile header). So a broken icon is a
# RUNTIME condition of THAT box's dify-api/plugin-daemon icon endpoint, not a
# code/build gap. This probe makes it self-diagnosing: it emits, one per line,
# "<provider><TAB><http_status>" for every installed model-provider whose icon
# endpoint did NOT return an image (empty output = all icons render).
#
# Self-contained: needs NO console session (works in verify-only runs). Resolves
# the tenant from postgres, enumerates installed model-provider plugins from the
# plugin-daemon inner API, then probes each provider's UNAUTHENTICATED icon
# endpoint on dify-api — the exact URL the browser <img> hits.
dify_broken_provider_icons() {
    local tenant server_key providers
    tenant=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d dify_db -tAc \
        "select id from tenants limit 1;" 2>/dev/null | tr -d '[:space:]')
    [ -n "$tenant" ] || return 0
    server_key=$(docker exec dify-plugin-daemon sh -c 'printf %s "$SERVER_KEY"' 2>/dev/null)
    [ -n "$server_key" ] || return 0
    # Installed model-provider ids = "<plugin_id>/<declaration.provider>" (only
    # those that declare a small icon).
    providers=$(docker exec dify-plugin-daemon sh -c \
        "curl -s --max-time 15 -H 'X-Api-Key: ${server_key}' 'http://127.0.0.1:5002/plugin/${tenant}/management/models?page=1&page_size=100'" 2>/dev/null | \
        python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = d.get('data') if isinstance(d, dict) else d
out = set()
for p in (items or []):
    decl = p.get('declaration', {}) or {}
    pid, prov = p.get('plugin_id'), decl.get('provider')
    if decl.get('icon_small') and pid and prov:
        out.add('%s/%s' % (pid, prov))
for x in sorted(out):
    print(x)
" 2>/dev/null)
    [ -n "$providers" ] || return 0
    printf '%s\n' "$providers" | while IFS= read -r prov; do
        [ -n "$prov" ] || continue
        local code
        code=$(docker exec dify-api curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
            "http://localhost:5001/console/api/workspaces/${tenant}/model-providers/${prov}/icon_small/en_US" 2>/dev/null)
        [ "$code" = "200" ] || printf '%s\t%s\n' "$prov" "${code:-000}"
    done
}

# ── #1248 verify: Dify's workspace default models ────────────────────────────
# "Dify: setup complete" + "model-provider icons render" both PASSED on the box
# whose Dify had no model provider at all: setup-complete is about the install
# wizard, and the icon probe reports on whatever providers happen to be
# installed. Neither notices that no default text-generation / embedding /
# rerank model is selected — the state in which a Dify app has no model and a
# knowledge base cannot index.
#
# The defaults are read back through Dify's OWN service layer (the same call the
# setter uses) rather than off a `tenant_default_models` SELECT: Dify stores the
# LEGACY origin spelling of the model type, and a schema guess here would turn
# every box red for the wrong reason.
_dify_default_models_dump() {
    local admin_email="$1"
    local _dify_get_py="
import sys
sys.path.insert(0, '/app/api')
from app import create_app
from extensions.ext_database import db
from models.account import Account, TenantAccountJoin
from services.model_provider_service import ModelProviderService
_, app = create_app()
with app.app_context():
    account = db.session.query(Account).filter(Account.email == sys.argv[1]).first()
    if not account:
        print('FATAL|admin account not found'); sys.exit(1)
    tj = db.session.query(TenantAccountJoin).filter(TenantAccountJoin.account_id == account.id).first()
    if not tj:
        print('FATAL|tenant not found'); sys.exit(1)
    mps = ModelProviderService()
    for mtype in ('llm', 'text-embedding', 'rerank'):
        try:
            d = mps.get_default_model_of_model_type(tenant_id=tj.tenant_id, model_type=mtype)
        except Exception as exc:
            print('%s|ERROR|%s' % (mtype, exc)); continue
        if not d:
            print('%s||' % mtype); continue
        prov = getattr(d, 'provider', None)
        pname = getattr(prov, 'provider', None) or getattr(prov, 'provider_name', None) or ''
        print('%s|%s|%s' % (mtype, pname, getattr(d, 'model', '') or ''))
"
    docker exec dify-api python3 -c "$_dify_get_py" "$admin_email" 2>/dev/null
}

# _dify_verify_one_default <model_type> <what> <want_model> <want_provider>
#                          <want_label> <dump>
_dify_verify_one_default() {
    local mtype="$1" what="$2" want_model="$3" want_provider="$4"
    local want_label="$5" dump="$6"
    local row prov model
    row=$(printf '%s\n' "$dump" | awk -F'|' -v t="$mtype" '$1 == t {print; exit}')
    if [ -z "$row" ]; then
        verify_check "Dify: default $what wired to $want_label" "fail" \
            "Dify reported no default $what at all — re-run 'rzfz post-install --refresh' (#1248)"
        return 0
    fi
    prov=$(printf '%s' "$row" | awk -F'|' '{print $2}')
    model=$(printf '%s' "$row" | awk -F'|' '{print $3}')
    if [ "$prov" = "ERROR" ]; then
        verify_check "Dify: default $what wired to $want_label" "fail" \
            "Dify could not resolve its default $what: $(printf '%s' "$model" | head -c 120)"
        return 0
    fi
    if [ -z "$model" ]; then
        verify_check "Dify: default $what wired to $want_label" "fail" \
            "no default $what selected — a Dify app has no model and a knowledge base cannot index (#1248)"
        return 0
    fi
    case "$prov" in
        *"$want_provider"*) ;;
        *)
            verify_check "Dify: default $what wired to $want_label" "fail" \
                "selected '$model' under provider '$prov', but this box's LLM backend is $want_label — Dify is calling an endpoint that is not there (#1248)"
            return 0
            ;;
    esac
    if [ -n "$want_model" ] && [ "$model" != "$want_model" ]; then
        verify_check "Dify: default $what wired to $want_label" "fail" \
            "selected '$model', expected '$want_model' from core/llm/standard-models.yaml"
        return 0
    fi
    verify_check "Dify: default $what wired to $want_label ($model)" "pass"
    return 0
}

# #1308 — "wired" is not "authenticated". _dify_verify_one_default proves a
# default exists and names the right provider; a ROTATED LLM_MANAGER_DIFY_KEY
# that never reached Dify (the old create-or-skip) still passed all three
# lines while every model call 401'd. This asks Dify to actually USE each
# default through its own path — ModelManager with the STORED credential, not
# .env's key — one minimal call per type. Rows: <mtype>|OK| ,
# <mtype>|AUTHFAIL|<reason> , <mtype>|SKIP| (no default — reported above).
_dify_probe_default_models() {
    local admin_email="$1"
    local _dify_probe_py="
import sys
sys.path.insert(0, '/app/api')
from app import create_app
from extensions.ext_database import db
from models.account import Account, TenantAccountJoin
from services.model_provider_service import ModelProviderService
from core.model_manager import ModelManager
try:
    # Dify >= 1.10 ships the runtime as the graphon package (measured in
    # dify-api 1.15/1.17 — review #1387); older releases under core.
    from graphon.model_runtime.entities.model_entities import ModelType
    from graphon.model_runtime.entities.message_entities import UserPromptMessage
except ImportError:
    from core.model_runtime.entities.model_entities import ModelType
    from core.model_runtime.entities.message_entities import UserPromptMessage
import inspect, signal
def _call(fn, **kw):
    # bind only the kwargs this Dify generation knows (user= vanished in 1.x)
    sig = inspect.signature(fn)
    return fn(**{k: v for k, v in kw.items() if k in sig.parameters})
def _verdict(exc):
    # AUTHFAIL only for an authentication failure; anything else (backend
    # down, model not deployed, timeout) is ERROR = no verdict, not red.
    m = str(exc).lower()
    for t in ('401', '403', 'unauthorized', 'authenticat', 'invalid api key', 'invalid_api_key', 'incorrect api key', 'forbidden'):
        if t in m:
            return 'AUTHFAIL'
    return 'ERROR'
def _model_type(name):
    # N3 (review): the graphon enum is a StrEnum — `ModelType(name)`. The 0.x
    # class method `value_of` does not exist there.
    try:
        return ModelType(name)
    except (ValueError, TypeError):
        return ModelType.value_of(name)
def _on_alarm(*_a):
    print('FATAL|probe timed out after 60 s (a cold model load?) - no verdict; re-run later')
    sys.stdout.flush(); sys.exit(3)
signal.signal(signal.SIGALRM, _on_alarm); signal.alarm(60)
_, app = create_app()
with app.app_context():
    account = db.session.query(Account).filter(Account.email == sys.argv[1]).first()
    if not account:
        print('FATAL|admin account not found'); sys.exit(1)
    tj = db.session.query(TenantAccountJoin).filter(TenantAccountJoin.account_id == account.id).first()
    if not tj:
        print('FATAL|tenant not found'); sys.exit(1)
    mps = ModelProviderService()
    # N1 (review, measured in dify-api 1.15): ModelManager takes a REQUIRED
    # provider_manager in 1.x. Dify builds it exactly like this
    # (core/app/llm/model_access.py). The 0.x no-arg form is the fallback.
    try:
        from core.plugin.impl.model_runtime_factory import create_plugin_provider_manager
        _pm = create_plugin_provider_manager(tenant_id=tj.tenant_id, user_id=account.id)
        mm = ModelManager(provider_manager=_pm)
    except (ImportError, TypeError):
        mm = ModelManager()
    for mtype in ('llm', 'text-embedding', 'rerank'):
        try:
            d = mps.get_default_model_of_model_type(tenant_id=tj.tenant_id, model_type=mtype)
        except Exception as exc:
            print('%s|ERROR|default unreadable: %s' % (mtype, str(exc).replace('|', '/')[:160])); continue
        model = getattr(d, 'model', None) if d else None
        if not model:
            print('%s|SKIP|' % mtype); continue
        prov = getattr(d, 'provider', None)
        pname = getattr(prov, 'provider', None) or getattr(prov, 'provider_name', None) or ''
        try:
            mi = mm.get_model_instance(tenant_id=tj.tenant_id, provider=pname,
                                       model_type=_model_type(mtype), model=model)
            if mtype == 'llm':
                _call(mi.invoke_llm, prompt_messages=[UserPromptMessage(content='ping')],
                      model_parameters={'max_tokens': 1}, stream=False)
            elif mtype == 'text-embedding':
                _call(mi.invoke_text_embedding, texts=['ping'])
            else:
                _call(mi.invoke_rerank, query='ping', docs=['ping', 'pong'])
            print('%s|OK|' % mtype)
        except Exception as exc:
            print('%s|%s|%s' % (mtype, _verdict(exc), str(exc).replace('|', '/').replace(chr(10), ' ')[:160]))
"
    # stderr goes to the operator when the probe produced NO rows — a broken
    # import, a missing container and a missing admin account used to look
    # identical ("no verdict") behind 2>/dev/null (review #1387, Befund 3).
    local _errf _out _rc
    _errf=$(mktemp)
    # N2 (review): `create_app()` prints framework warnings on STDOUT, so the
    # raw output is never empty and the FATAL/no-verdict path could not fire.
    # Keep only OUR rows; anything else is noise from Dify's own boot.
    _out=$(docker exec dify-api python3 -c "$_dify_probe_py" "$admin_email" 2>"$_errf" \
           | grep -E '^(llm|text-embedding|rerank|FATAL)\|' || true); _rc=${PIPESTATUS[0]}
    if [ -z "$_out" ]; then
        local _err
        _err=$(grep -v '^[[:space:]]*$' "$_errf" | tail -1 | head -c 160)
        printf 'FATAL|no output from the probe (rc %s)%s\n' "$_rc" "${_err:+: $_err}"
    else
        printf '%s\n' "$_out"
    fi
    rm -f "$_errf"
}

# _dify_verify_probe_row <model_type> <what> <probe> <want_label>
_dify_verify_probe_row() {
    local mtype="$1" what="$2" probe="$3" want_label="$4" row verdict reason
    row=$(printf '%s\n' "$probe" | awk -F'|' -v t="$mtype" '$1 == t {print; exit}')
    [ -n "$row" ] || return 0
    verdict=$(printf '%s' "$row" | awk -F'|' '{print $2}')
    reason=$(printf '%s' "$row" | cut -d'|' -f3-)
    case "$verdict" in
        OK)   verify_check "Dify: default $what answers through its stored credential ($want_label)" "pass" ;;
        SKIP) ;;   # no default at all — the wiring check above already failed it
        AUTHFAIL)
              verify_check "Dify: default $what answers through its stored credential ($want_label)" "fail" \
                  "$(printf '%s' "$reason" | head -c 160) — a rotated LLM_MANAGER_DIFY_KEY that Dify never learned? re-run 'rzfz post-install --refresh' (#1308)" ;;
        *)    # ERROR (backend down, model not deployed, timeout…) — unmeasured
              # is not green, but it is not a key problem either (#1301 lesson).
              print_warning "  Dify: default $what could not be probed — $(printf '%s' "$reason" | head -c 160) (no verdict on authentication, #1308)" ;;
    esac
    return 0
}

dify_verify_default_models() {
    local want_provider want_label
    if llm_manager_owns_standard_set; then   # #1441: one predicate for "the manager owns this box's models"
        want_provider="openai_api_compatible"
        want_label="the LLM Manager"
    else
        want_provider="gpustack"
        want_label="GPUStack"
    fi

    local admin_email dump
    admin_email=$(_resolve_dify_admin_email)
    dump=$(_dify_default_models_dump "$admin_email")
    if [ -z "$dump" ] || printf '%s\n' "$dump" | grep -q '^FATAL|'; then
        # ONE line, not three identical ones, when the READ itself failed.
        verify_check "Dify: default models wired to $want_label" "fail" \
            "could not read Dify's default models: ${dump:-no output from dify-api}"
        return 0
    fi

    _dify_verify_one_default "llm" "text model" "$(_default_chat_alias)" \
        "$want_provider" "$want_label" "$dump"
    _dify_verify_one_default "text-embedding" "embedding model" \
        "$(_default_embedding_alias)" "$want_provider" "$want_label" "$dump"
    # The rerank default is checked WITHOUT pinning an expected alias: the
    # reranker is the one role an operator legitimately swaps per box, and what
    # must not be missing is the wiring, not a particular model.
    _dify_verify_one_default "rerank" "rerank model" "" \
        "$want_provider" "$want_label" "$dump"
    # #1308: now USE each default through Dify's own path (stored credential).
    local probe
    probe=$(_dify_probe_default_models "$admin_email")
    if [ -z "$probe" ] || printf '%s\n' "$probe" | grep -q '^FATAL|'; then
        local _why
        _why=$(printf '%s\n' "$probe" | grep '^FATAL|' | head -1 | cut -d'|' -f2-)
        print_warning "  Dify: could not probe the default models through their stored credentials — no verdict on authentication (#1308)${_why:+: $_why}"
    else
        _dify_verify_probe_row "llm" "text model" "$probe" "$want_label"
        _dify_verify_probe_row "text-embedding" "embedding model" "$probe" "$want_label"
        _dify_verify_probe_row "rerank" "rerank model" "$probe" "$want_label"
    fi
}

# ── #1248: the Dify model provider on an LLM-Manager box ─────────────────────
# `dify_configure_model` below wires Dify to `http://gpustack:9090` through the
# langgenius/gpustack plugin. A Manager-shaped box runs no GPUStack at all, so on
# a clean install every credential validation died on
# `NameResolutionError(host='gpustack', port=9090)`, no default
# text-generation / embedding / rerank model was ever selected, and Dify shipped
# with no usable model provider — the last member of the #976 rewiring class
# (Open WebUI moved in #976, the model DEPLOY in #1250b/#1255, Dify here).
#
# The backend is the manager's metered OpenAI-compatible surface —
# http://llm-manager:8080/v1, key-authenticated with the stack/dify service key,
# `/v1/rerank` included — so the provider is langgenius/openai_api_compatible and
# the registration goes through the SAME shared helper the manager's chat wiring
# already uses (#813/#666), extended by #1248 to the embedding and rerank model
# types plus the workspace defaults.
#
# The model set comes from core/llm/standard-models.yaml, filtered the way the
# GPUStack branch and #1255's manager deploy filter it (preset, role,
# auto_start): it is the SOLL this very post-install run just deployed.
# Enumerating the manager's READY deployments instead — what
# wire_llm_manager_consumers does — registers nothing while a weight download is
# still finishing, and that deferral ("deferred to the next --refresh") is part
# of why a clean Manager box came up unwired at all.
dify_configure_manager_models() {
    print_substep "Configuring LLM Manager models in Dify..."

    # Liveness decides whether to SKIP an authoritative write, never WHICH
    # backend a consumer is pointed at (#976). With the manager down we cannot
    # mint a key and Dify's own credential validation would fail anyway, so
    # refuse loudly instead of storing a provider that 401s or points nowhere.
    if ! _llm_manager_running; then
        print_error "The llm-manager profile is active but the llm-manager container is not running — a Dify provider registered now would store credentials for an endpoint that is not there. Start the stack and re-run 'rzfz post-install --refresh'. (#1248)"
        return 1
    fi

    local admin_email
    admin_email=$(_resolve_dify_admin_email)

    # The stack/dify service key is minted by wire_llm_manager_consumers on a box
    # where the manager was ALREADY serving; on a clean install that ran BEFORE
    # the models were deployed and returned early, so mint here too rather than
    # register an unauthenticated provider (#1185).
    local dify_key
    dify_key=$(read_env_value "$ENV_FILE" LLM_MANAGER_DIFY_KEY 2>/dev/null || true)
    if [ -z "$dify_key" ]; then
        dify_key=$(_llm_manager_mint_service_key dify) || dify_key=""
        if [ -n "$dify_key" ]; then
            update_env_value "$ENV_FILE" "LLM_MANAGER_DIFY_KEY" "$dify_key"
            print_substep "Minted the stack/dify service key."
        fi
    fi
    if [ -z "$dify_key" ]; then
        print_error "No stack/dify LLM Manager service key (LLM_MANAGER_DIFY_KEY is empty and the mint failed) — Dify would get a model provider that 401s on every call. (#1248/#1185)"
        return 1
    fi

    local preset="${PRESET:-standard}"
    local endpoint="$LLM_CANONICAL_ENDPOINT"
    local label="LLM Manager"
    local provider="langgenius/openai_api_compatible/openai_api_compatible"

    local rows
    rows=$(_model_rows_for_preset "$preset") || rows=""
    if [ -z "$rows" ]; then
        print_error "No models for preset '$preset' in core/llm/standard-models.yaml — there is nothing to register in Dify."
        return 1
    fi

    # Always-on models only, keyed on the ROLE column (col 6) exactly like the
    # GPUStack branch: an auto_start:false spare is not running, so registering it
    # makes Dify offer a model whose every call 503s.
    #
    # #1786 rev-B: the WILDCARD column too. `auto_start != false` is only half of
    # "the standard set actually placed this" — `_llmm_row_deployable` also
    # refuses a wildcard weight filename (col 3), because the unattended deploy
    # cannot expand a glob without an HF listing call. `nomic-embed-text` is
    # exactly that shape ("*f16*.gguf", deliberately, so it stays a console
    # option), so it is registered but never served, and Dify's credential
    # validation answers
    #     400 Client Error: Bad Request for url: http://llm:8080/v1/embeddings
    # on EVERY post-install run of EVERY manager box (measured on 0.91,
    # 2026-09-08). Same rule, one place: a row this box does not serve is not a
    # row Dify may offer.
    local chat_rows embed_aliases rerank_aliases
    chat_rows=$(printf '%s\n' "$rows" | awk -F'\t' '$6 ~ /(^|,)chat(,|$)/ && $7 != "false" && $3 !~ /[*?]/ {print $1 "\t" $6}')
    embed_aliases=$(printf '%s\n' "$rows" | awk -F'\t' '$6 ~ /(^|,)embedding(,|$)/ && $7 != "false" && $3 !~ /[*?]/ {print $1}')
    rerank_aliases=$(printf '%s\n' "$rows" | awk -F'\t' '$6 ~ /(^|,)reranker(,|$)/ && $7 != "false" && $3 !~ /[*?]/ {print $1}')

    # #1442 rev-D (journey C on 0.79): the manifest says what the standard set
    # WANTS placed; the manager says what it SERVES. On a federated legacy box
    # the reranker was in error in GPUStack and never federated, and this step
    # still registered `qwen3-reranker` at /v1/rerank — a 404 for every Dify
    # retrieval, `CredentialsValidateFailedError` in the log. When the manager
    # answers with a served list, only served rows are registered and the
    # skipped ones are named; when it answers nothing (models still pulling on
    # a clean install), the manifest decides as before and the #1507 late pass
    # re-wires once they are ready.
    #
    # #2195 (journey A, 0.91, a fresh CPU install): there is a THIRD case, and
    # it is the default one. `granite-docling` is 258M and carries a ready
    # instance within seconds, while the three models this function cares about
    # are still pulling — so the manager answers with a list that is neither
    # complete nor empty, the filter below empties all three lists, and the
    # verdict at the end of this function blamed the MANIFEST for it. The
    # manifest is fine. Remember which of the two it was.
    local served_ids _readiness_incomplete=false
    served_ids=$(_owui_manager_served_models "$dify_key" 2>/dev/null) || served_ids=""
    if [ -n "$served_ids" ]; then
        local _skipped _wanted
        _wanted=$(printf '%s\n%s\n%s\n' "$(printf '%s\n' "$chat_rows" | cut -f1)" "$embed_aliases" "$rerank_aliases" | grep -v '^$' || true)
        # The names the manifest wants and the manager does not serve — computed
        # HERE, not inside the filter: a command substitution is a subshell and
        # would keep the list to itself.
        _skipped=$(printf '%s\n' "$_wanted" | grep -vxF -f <(printf '%s\n' "$served_ids") | paste -sd ',' - | sed 's/,/, /g') || _skipped=""
        _dify_only_served() {   # $1 = rows (one per line, first column = alias) → the served subset
            local line alias
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                alias=$(printf '%s' "$line" | cut -f1)
                printf '%s\n' "$served_ids" | grep -qxF -- "$alias" && printf '%s\n' "$line"
            done <<< "$1"
            return 0
        }
        chat_rows=$(_dify_only_served "$chat_rows")
        embed_aliases=$(_dify_only_served "$embed_aliases")
        rerank_aliases=$(_dify_only_served "$rerank_aliases")
        # #2195 rev-B (journey A-prime, 0.91, 23:46:32): ANY wanted row the
        # manager does not serve makes this a readiness state — not only the
        # case where every one of them is missing. The first cut required chat
        # AND embed AND rerank to be empty, and the box that followed served the
        # reranker and the doc model while the chat model and the embedder were
        # still pulling: the condition was false, the old path ran, and Dify was
        # configured with a reranker and nothing else UNDER A GREEN LINE. The
        # cost was immediate — the example KB was not created, because Dify
        # answered `{"code":"invalid_param","message":"Default model not found
        # for text-embedding"}`. `_skipped` is already exactly this list, and it
        # is already printed above; here it decides.
        if [ -n "$_skipped" ]; then
            _readiness_incomplete=true
        fi
        if [ -n "$_skipped" ]; then
            print_warning "  Not registered in Dify — the LLM Manager does not serve: ${_skipped} (#1442). Deploy them, then re-run 'rzfz post-install --refresh'."
        fi
    fi

    local reg_all="" alias roles ctx extra
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        alias=$(printf '%s' "$line" | cut -f1)
        roles=$(printf '%s' "$line" | cut -f2)
        [ -n "$alias" ] || continue
        ctx=$(_manifest_per_slot_context "$alias" 32768)
        # Capability flags Dify's agent / workflow / vision features gate on.
        #
        # #1979: the second spelling `agent_though_support` is GONE. The comment
        # that used to stand here said the openai_api_compatible plugin declares
        # the typo'd variable and the gpustack fork the correct one, so sending
        # both would land the flag either way. Measured across four shipped
        # plugin packages — the typo appears NOWHERE:
        #
        #     plugin                              agent_thought_  agent_though_
        #     openai_api_compatible 0.0.66             16              0
        #     openai_api_compatible 0.0.34             10              0
        #     gpustack              0.0.16             10              0
        #     gpustack              0.0.15             10              0
        #
        # (DevBox-Vuko measured the same against the three versions installed on
        # 0.91, in the provider schema AND the model sources.) Both plugins use
        # the correct spelling only, so the extra key had no reader on any
        # version we have ever shipped — it was never a safeguard, and the
        # comment defending it was never checked against a package.
        #
        # #1966: `compatibility_mode: extended` is what makes the thinking
        # switch REACH the model. Read in the shipped plugin
        # (openai_api_compatible 0.0.66, models/llm/llm.py:581-616): with
        # `agent_thought_support: supported` the plugin POPS `enable_thinking`
        # out of the model parameters (:591) and only puts a thinking
        # instruction back — `chat_template_kwargs.enable_thinking`, the form
        # llama.cpp reads — when `compatibility_mode == "extended"` (:597-605).
        # The default is `strict`, and under strict the switch is dropped in
        # BOTH directions: neither a workflow's `enable_thinking: false` nor a
        # credential's forced value ever leaves Dify.
        #
        # Measured by DevBox-Vuko on 0.91 against this router (#1966): without
        # the kwarg qwen3.6 spends its whole budget in the thinking channel and
        # returns `content: ''` with `finish_reason: length`; with it, the same
        # call returns the JSON and `finish_reason: stop`. The PSA workflow sets
        # `enable_thinking: false` on five of its six LLM nodes and still got
        # empty content — because the flag never arrived.
        #
        # `extended` is right for THIS credential and would not be for every
        # endpoint: it permits non-standard parameters, and a strict OpenAI
        # gateway can reject them. This credential names our own LLM Manager,
        # which is LiteLLM in front of llama-box — the runtime those parameters
        # were invented for. The Mac-gateway registration deliberately sends no
        # extra credentials at all and is unaffected.
        extra='{"max_tokens_to_sample":"8192","agent_thought_support":"supported","compatibility_mode":"extended","function_calling_type":"tool_call","stream_function_calling":"supported","structured_output_support":"supported"'
        case ",${roles}," in
            *,vision,*|*,vision-*) extra="${extra},\"vision_support\":\"support\"}" ;;
            *)                     extra="${extra},\"vision_support\":\"no_support\"}" ;;
        esac
        # #1959: the verdict is deliberately NOT consumed per call here. This
        # function keeps its own, richer one — the per-model-TYPE count below,
        # which #1248 rev-B added for exactly this path and which must see every
        # model's output before it judges. `|| true` only keeps `set -e` from
        # cutting the loop short at the first refusal.
        _dify_register_models "$admin_email" "$dify_key" "$endpoint" "$label" \
            "$ctx" "$alias" "llm" "$extra" || true
        reg_all="${reg_all}${_DIFY_REG_LAST_OUTPUT}
"
    done <<< "$chat_rows"

    while IFS= read -r alias; do
        [ -n "$alias" ] || continue
        ctx=$(_manifest_per_slot_context "$alias" 8192)
        _dify_register_models "$admin_email" "$dify_key" "$endpoint" "$label" \
            "$ctx" "$alias" "text-embedding" "{}" || true   # #1959: see above
        reg_all="${reg_all}${_DIFY_REG_LAST_OUTPUT}
"
    done <<< "$embed_aliases"

    # The rerank half is not optional decoration: with no rerank model wired,
    # Dify's knowledge retrieval silently drops to embedding-only and answers
    # plausibly from the wrong chunks — the failure #908/#1058 chased through the
    # Open WebUI RAG path. The manager exposes it at /v1/rerank, which is where
    # the plugin's rerank model posts.
    while IFS= read -r alias; do
        [ -n "$alias" ] || continue
        ctx=$(_manifest_per_slot_context "$alias" 8192)
        _dify_register_models "$admin_email" "$dify_key" "$endpoint" "$label" \
            "$ctx" "$alias" "rerank" "{}" || true   # #1959: see above
        reg_all="${reg_all}${_DIFY_REG_LAST_OUTPUT}
"
    done <<< "$rerank_aliases"

    # rev-B (review F1): count PER MODEL TYPE, not across all of them. A global
    # count of 1 passed a run in which the chat credential registered and EVERY
    # embedding and rerank credential came back `error:` — and the workspace
    # defaults below were then set for text-embedding and rerank with nothing
    # behind them. That is the #1248 symptom one level up, and the same class
    # this cycle is busy abolishing (#1247: the installer reports success while
    # the box is broken). A type we did not ask for (empty alias list) is not
    # required to produce anything.
    # #2197: `${ok_count}` on the success line below was never assigned —
    # every manager box printed "( model credential(s), …)". Count what the
    # per-type check counts, so the number on the success line is the number
    # the verdict was made from.
    local missing="" mt aliases_for_type ok_type ok_count=0
    for mt in llm text-embedding rerank; do
        case "$mt" in
            llm)            aliases_for_type=$(printf '%s\n' "$chat_rows" | cut -f1) ;;
            text-embedding) aliases_for_type="$embed_aliases" ;;
            rerank)         aliases_for_type="$rerank_aliases" ;;
        esac
        [ -n "$(printf '%s' "$aliases_for_type" | tr -d '[:space:]')" ] || continue
        ok_type=$(printf '%s\n' "$reg_all" | grep -cE "${mt} model \[[^]]*\] (added|updated \(credential refreshed\)|already configured)\.") || ok_type=0
        [ "${ok_type:-0}" -ge 1 ] || missing="${missing:+${missing}, }${mt}"
        ok_count=$((ok_count + ${ok_type:-0}))
    done
    if [ -n "$missing" ]; then
        print_error "Dify model registration against the LLM Manager produced no usable credential for: ${missing}"
        print_error "  (a workspace default without a credential behind it is exactly the #1248 state; refusing to set one)"
        printf '%s\n' "$reg_all" | while IFS= read -r line; do
            if [ -n "$line" ]; then print_error "  $line"; fi
        done
        return 1
    fi

    # ── workspace defaults ────────────────────────────────────────────────────
    local chat_default embed_default rerank_default
    chat_default=$(_dify_pick_default "$(_default_chat_alias)" \
        "$(printf '%s\n' "$chat_rows" | cut -f1)")
    embed_default=$(_dify_pick_default "$(_default_embedding_alias)" "$embed_aliases")
    rerank_default=$(_dify_pick_default "$(_llmm_default_alias reranker qwen3-reranker)" \
        "$rerank_aliases")

    local pairs=""
    if [ -n "$chat_default" ]; then pairs="llm=${chat_default}"; fi
    if [ -n "$embed_default" ]; then pairs="${pairs:+${pairs};}text-embedding=${embed_default}"; fi
    if [ -n "$rerank_default" ]; then pairs="${pairs:+${pairs};}rerank=${rerank_default}"; fi
    if [ -z "$pairs" ]; then
        # #2195: "not yet" and "never" are different answers and used to print
        # the same line. `dify` is one of POST_INSTALL_CRITICAL_STEPS, so
        # returning 1 for a box that is merely still downloading exits
        # post-install 1 with "this box cannot serve models" — measured on 0.91
        # on a box that served a real chat completion five minutes later.
        if [ "$_readiness_incomplete" = true ]; then
            print_warning "No Dify workspace default yet: the manifest's chat, embedding and rerank models are still DOWNLOADING — the LLM Manager serves none of them at this moment. The manifest is fine; the weights are not here yet. On a fresh CPU box this is the normal shape, because --skip-wait is the default there."
            print_info "  Nothing is registered with nothing behind it (#1248), so Dify keeps no half-configured provider."
            print_info "  The late pass (#1507) re-runs this for you when the deployments land inside its window. If it does not, run:"
            print_info "      rzfz post-install --refresh"
            return 3
        fi
        print_error "Nothing to select as a Dify workspace default — the manifest's always-on set for preset '$preset' has no chat, embedding or rerank model."
        return 1
    fi

    _dify_set_default_models "$admin_email" "$provider" "$pairs"

    # Only the defaults we ASKED for are asserted: a box whose always-on set has
    # no reranker never asks for one, and must not fail for it.
    local missing="" pair mt
    for pair in $(printf '%s' "$pairs" | tr ';' ' '); do
        mt="${pair%%=*}"
        if ! printf '%s\n' "$_DIFY_DEF_LAST_OUTPUT" | grep -q "^Default ${mt} set to "; then
            missing="${missing}${mt} "
        fi
    done
    if [ -n "$missing" ]; then
        print_error "Dify workspace default not selected for: ${missing}— a Dify app has no model and a knowledge base cannot index. (#1248)"
        printf '%s\n' "$_DIFY_DEF_LAST_OUTPUT" | while IFS= read -r line; do
            if [ -n "$line" ]; then print_error "  $line"; fi
        done
        return 1
    fi

    # #2195 rev-B: what was served IS registered and its defaults ARE set — a
    # partial set is not a reason to discard what the box has. But the step is
    # not FINISHED, and printing success over it is what let a Dify with a
    # reranker and no chat model read as a green install (0.91, journey
    # A-prime). The verdict is chosen here; the cleanup below still runs once,
    # after the defaults, which is the #1338 ordering.
    local _dcm_verdict=0
    if [ "$_readiness_incomplete" = true ]; then
        _dcm_verdict=3
        print_warning "Dify has only PART of its model set: ${pairs}. The LLM Manager does not serve these: ${_skipped}."
        print_info "  Either they are still downloading — a fresh CPU box wires its consumers before the weights land — or this box cannot place them at all. 'rzfz llm status' says which."
        print_info "  A Dify with no embedding default cannot index a knowledge base (the example KB seed answers 'Default model not found for text-embedding'); with no rerank default its retrieval silently drops to embedding-only and answers plausibly from the wrong chunks (#908/#1058)."
        print_info "  The late pass (#1507) re-runs this once the deployments are ready, and does NOT wait for models the manager says it cannot place (#1760). If Dify stays short, run:"
        print_info "      rzfz post-install --refresh"
    else
        print_success "Dify LLM Manager models configured (${ok_count} model credential(s), defaults: ${pairs})."
    fi
    # After the defaults are set, not before: tenant_default_models is unique per
    # (tenant_id, model_type), so setting a default REPOINTS the row the stale
    # provider held. What the cleanup then removes is only what stayed dangling.
    _dify_drop_inactive_provider_rows "$provider"
    return "$_dcm_verdict"
}

# #1338 — a backend switch must take the OLD provider's rows with it.
#
# `dify_configure_manager_models` (above) and `dify_configure_model` (the
# GPUStack branch) each write one of the stack's TWO providers. Neither ever
# deleted the other's rows, and `core/llm/sync.py::sync_dify` — the only place
# that deletes anything — filters on `provider_name = langgenius/gpustack/…`
# and on aliases that vanished from standard-models.yaml. A migrated box's
# aliases have NOT vanished; they hang off the wrong provider. So the old
# provider survives every upgrade and every `--refresh`, and the operator sees
# a second model list in every Dify picker, pointing at `http://gpustack:9090`
# — a host a Manager box does not resolve. No error anywhere; the failure
# arrives at runtime as a NameResolutionError.
#
# WHICH TABLES: the issue names `provider_models`. Measured on 0.78 (a real
# GPUStack box) the provider lives in FOUR:
#
#     provider_models             9   the model list
#     provider_model_credentials  n   the per-model endpoint+key (customizable-
#                                     model providers keep credentials here)
#     provider_model_settings     1   per-model enable/disable
#     tenant_default_models       6   the workspace defaults
#
# Dropping only the first would leave the credentials and the workspace
# defaults pointing at the dead provider, so all four are cleaned.
#
# NEVER touches a third-party provider. The delete is keyed on the ONE other
# stack-owned provider name — an operator's own OpenAI or Anthropic entry has a
# different `provider_name` and is not part of this decision. And it runs only
# after the active provider was written successfully (the caller returns
# earlier on failure), so a half-configured box keeps the rows it still has.
_dify_drop_inactive_provider_rows() {
    local active="$1" stale="" scope=""
    case "$active" in
        *openai_api_compatible*)
            # The gpustack provider plugin is OURS end to end: an operator does
            # not hand-register models under `langgenius/gpustack`, the stack's
            # own writer is the only thing that ever puts rows there. Everything
            # of that provider goes.
            stale="langgenius/gpustack/gpustack"
            scope="all"
            ;;
        *gpustack*)
            # #1338 review (agent-rzfz), and this one is load-bearing:
            # `openai_api_compatible` is Dify's GENERIC provider for "any
            # OpenAI-compatible endpoint", and it is a customizable-model
            # provider — every model an operator adds by hand (a partner API,
            # their own vLLM, a second manager) sits under the SAME
            # provider_name as ours. A provider-wide DELETE here takes their
            # entries with it, credentials and workspace defaults included. It
            # would also delete the Mac gateway's registration (#813), which is
            # a LIVE integration on a box that switches to GPUStack.
            #
            # So on this side only OUR credential is removed, by its label.
            stale="langgenius/openai_api_compatible/openai_api_compatible"
            scope="ours"
            ;;
        *) return 0 ;;
    esac
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres || return 0

    local _psql="docker exec postgres psql -U ${POSTGRES_USER:-docker} -d ${DIFY_DB:-dify_db} -tAc"

    # The model names to remove. With scope=ours that is "whatever hangs off a
    # credential labelled 'LLM Manager'" — the label `_dify_register_models`
    # writes (credential_name=label). With scope=all it is the whole provider.
    local names_sql
    if [ "$scope" = "ours" ]; then
        names_sql="SELECT DISTINCT pm.model_name FROM provider_models pm
                     JOIN provider_model_credentials c ON c.id = pm.credential_id
                    WHERE pm.provider_name = '${stale}'
                      AND c.provider_name = '${stale}'
                      AND c.credential_name = 'LLM Manager';"
    else
        names_sql="SELECT DISTINCT model_name FROM provider_models WHERE provider_name = '${stale}';"
    fi
    local names
    names=$($_psql "$names_sql" 2>/dev/null | sed '/^$/d')
    if [ -z "$names" ] && [ "$scope" = "ours" ]; then
        # Nothing of ours under the retired provider. Silent: this runs on every
        # --refresh, and on a box that never switched there is nothing to say.
        #
        # #1338 review (agent-rzfz), finding 1: this return used to fire for
        # BOTH scopes, and on the `all` side that was a NARROWING against the
        # code it replaced. There the model names are only a convenience — the
        # whole provider goes — so an empty `provider_models` meant nothing was
        # deleted at all, not even the credentials, the settings or the
        # workspace defaults. That state is not hypothetical: it is what a
        # registration that died after writing its credential leaves behind,
        # and a dead provider remnant is precisely what this function is for.
        return 0
    fi

    # `'a','b'` for the IN-lists below. Dify model names are plugin-declared
    # identifiers, but a stray quote would end the literal, so double it.
    local in_list=""
    local _n
    while IFS= read -r _n; do
        [ -n "$_n" ] || continue
        _n=${_n//\'/\'\'}
        in_list="${in_list:+${in_list},}'${_n}'"
    done <<< "$names"

    print_step "Dify: removing the retired model provider's rows (#1338)"
    local rows_deleted=0 t n where
    for t in provider_models provider_model_settings tenant_default_models; do
        # scope=all: the whole provider goes, so the name list is not a filter —
        # it is only the thing that makes the OURS side safe. Restricting the
        # `all` side by it would leave every row whose model has already gone
        # from provider_models.
        if [ "$scope" = "all" ]; then
            where="provider_name = '${stale}'"
        else
            where="provider_name = '${stale}' AND model_name IN (${in_list})"
        fi
        n=$($_psql "DELETE FROM ${t} WHERE ${where};" 2>/dev/null | sed -n 's/^DELETE //p')
        case "${n:-}" in ''|*[!0-9]*) n=0 ;; esac
        [ "$n" = "0" ] || print_substep "  ${t}: ${n} row(s)"
        rows_deleted=$((rows_deleted + n))
    done
    # Credentials LAST: provider_models.credential_id points at them, and the
    # name lookup above joined through it.
    if [ "$scope" = "ours" ]; then
        where="provider_name = '${stale}' AND credential_name = 'LLM Manager'"
    else
        where="provider_name = '${stale}'"
    fi
    n=$($_psql "DELETE FROM provider_model_credentials WHERE ${where};" 2>/dev/null | sed -n 's/^DELETE //p')
    case "${n:-}" in ''|*[!0-9]*) n=0 ;; esac
    [ "$n" = "0" ] || print_substep "  provider_model_credentials: ${n} row(s)"
    rows_deleted=$((rows_deleted + n))

    print_substep "Removed ${rows_deleted} row(s) of ${stale} — this box serves its models through ${active##*/}."
}

dify_configure_model() {
    print_substep "Configuring GPUStack models in Dify..."

    # S20: resolve the real admin email (survives a post-init MAIN_DOMAIN change)
    local admin_email
    admin_email=$(_resolve_dify_admin_email)
    # #1445 (C5c) rev-G: this used to be flipped to `http://llm:8080` "because
    # the canonical endpoint is canonical". It is the wrong function for that,
    # and the review measured it from inside a running dify-api:
    #
    #     llm:8080/v1-openai/models          -> 000  (name does not resolve)
    #     gpustack:9090/v1-openai/models     -> 401  (host there, wants its key)
    #
    # `step_dify_provisioning` picks THIS branch exactly when
    # `llm_manager_owns_standard_set` is FALSE — a pure GPUStack box, or a dual
    # box that is not federated yet. The `llm` alias hangs on the manager
    # service alone (modules/llm/manager/compose.yml), so on a box without the
    # manager profile it does not resolve at all and Dify ends up with no
    # default text/embedding/rerank model: the #1248 failure mode, mirrored.
    #
    # And the address is only half of it. This branch registers through the
    # `langgenius/gpustack` provider plugin with GPUSTACK_API_KEY. Pointing that
    # at the manager would send a backend's own credential to a metered
    # endpoint, which is precisely the 401 that #1444's breaking change exists
    # to produce. Address and key belong together; the manager-shaped box has
    # its own function (`dify_configure_manager_models`), and this one is the
    # GPUStack branch.
    local gpustack_url="http://gpustack:9090"
    local api_key="${GPUSTACK_API_KEY}"
    local preset="${PRESET:-standard}"

    # S19: the LLM-category aliases that ship under this preset, read from
    # standard-models.yaml (the same enumerator deploy/wait use). Replaces the
    # hardcoded gemma4 + qwen3.5/qwen3-coder-next list that drifted from the
    # YAML and registered a non-existent model in Dify (404 on validation).
    # Only always-on chat models (auto_start != false, col 7). Stopped/optional
    # models (gemma4, qwen3-coder-next at 0 replicas) 503 on Dify credential
    # validation, so they are not registered here; they get wired manually if
    # the operator scales them up. default_chat is the system default text-gen.
    local llm_aliases default_chat default_embedding
    # #1786 rev-B: `$3 !~ /[*?]/` — see the note in the manager branch above. A
    # wildcard weight filename is never placed by the standard set, so a model
    # carrying one must not be registered here either.
    local _rows_for_dify
    _rows_for_dify=$(_model_rows_for_preset "$preset")
    llm_aliases=$(printf '%s\n' "$_rows_for_dify" | awk -F'\t' '$6 ~ /(^|,)chat(,|$)/ && $7 != "false" && $3 !~ /[*?]/ {print $1}' | tr '\n' ' ')
    local embed_aliases_gs rerank_aliases_gs embed_ctx_pairs rerank_ctx_pairs _a
    embed_aliases_gs=$(printf '%s\n' "$_rows_for_dify" | awk -F'\t' '$6 ~ /(^|,)embedding(,|$)/ && $7 != "false" && $3 !~ /[*?]/ {print $1}' | tr '\n' ' ')
    rerank_aliases_gs=$(printf '%s\n' "$_rows_for_dify" | awk -F'\t' '$6 ~ /(^|,)reranker(,|$)/ && $7 != "false" && $3 !~ /[*?]/ {print $1}' | tr '\n' ' ')
    # #1786 rev-C (agent-seqis, Gegenlesen): the context size comes from the
    # MANIFEST, like the manager branch beside it already does
    # (`_manifest_per_slot_context`). The previous form was
    #   'context_size': '32768' if _alias == 'qwen3-embedding' else '8192'
    # — a typed value wearing the clothes of a derivation, and it disagreed with
    # the manifest in BOTH arms (qwen3-embedding declares 8192, nomic 2048). The
    # fallbacks below are the ones the manager branch uses, so a model with no
    # per_slot_context lands on the same number on both paths.
    embed_ctx_pairs=""
    for _a in $embed_aliases_gs; do
        embed_ctx_pairs="${embed_ctx_pairs}${_a}=$(_manifest_per_slot_context "$_a" 8192) "
    done
    rerank_ctx_pairs=""
    for _a in $rerank_aliases_gs; do
        rerank_ctx_pairs="${rerank_ctx_pairs}${_a}=$(_manifest_per_slot_context "$_a" 8192) "
    done
    default_chat=$(_default_chat_alias)
    default_embedding=$(_default_embedding_alias)

    local py_script="
import sys
sys.path.insert(0, '/app/api')

from app import create_app
from extensions.ext_database import db
from models.account import Account, TenantAccountJoin
from services.model_provider_service import ModelProviderService

# Dify 1.14: create_app() returns (socketio_app, app); was a single Flask app pre-1.14.
_, app = create_app()
with app.app_context():
    try:
        account = db.session.query(Account).filter(Account.email == '${admin_email}').first()
        if not account:
            print('Admin account not found')
            sys.exit(1)
        # The admin-account timezone nudge lives in _dify_align_account_timezone
        # (#1248) — step_dify_provisioning applies it to BOTH provider branches,
        # so it is not repeated here.
        tenant_join = db.session.query(TenantAccountJoin).filter(TenantAccountJoin.account_id == account.id).first()
        if not tenant_join:
            print('Tenant not found')
            sys.exit(1)
        tenant_id = tenant_join.tenant_id
        mps = ModelProviderService()
        provider = 'langgenius/gpustack/gpustack'
        
        # Note: langgenius/gpustack uses a 'customizable-model' type — credentials are
        # set per model, not at the provider level. No provider_credential needed.
        
        # ── Per-model credential helper ──────────────────────────────────────
        def add_model(name, model_type, extra_creds):
            base = {'endpoint_url': '${gpustack_url}', 'api_key': '${api_key}'}
            base.update(extra_creds)
            try:
                mps.create_model_credential(
                    tenant_id=tenant_id, provider=provider,
                    model=name, model_type=model_type,
                    credentials=base, credential_name='GPUStack'
                )
                print(f'{model_type} model [{name}] added.')
            except Exception as e:
                if 'already' in str(e).lower() or 'duplicate' in str(e).lower() or 'exists' in str(e).lower():
                    print(f'{model_type} model [{name}] already configured.')
                else:
                    print(f'{model_type} model [{name}] error: {e}')

        # ── LLM models — preset-dependent ────────────────────────────────────
        llm_defaults = {
            'mode': 'chat',
            'context_size': '262144',
            'max_tokens_to_sample': '8192',
            'agent_thought_support': 'supported',
            # compatibility_mode=extended (the 'Enable extra settings' UI option) is
            # REQUIRED for the gpustack 0.0.15 plugin to forward extra_body — without it
            # the thinking-param
            # passthrough (chat_template_kwargs.enable_thinking) is dropped, so qwen3.6
            # keeps reasoning in structured-extraction. strict = plain OpenAI-compatible.
            'compatibility_mode': 'extended',
            'function_calling_type': 'tool_call',
            'stream_function_calling': 'supported',
            'vision_support': 'support',
            'structured_output_support': 'supported',
            'stream_mode_delimiter': r'\n\n',
        }
        # S19: register exactly the LLM-category models that ship under this
        # preset, read from standard-models.yaml via _model_rows_for_preset
        # (bash interpolates the space-separated alias list). Replaces the
        # hardcoded gemma4 + qwen3.5/qwen3-coder-next block that drifted from
        # the YAML and 404'd Dify validation on the non-existent qwen3.5.
        # llm_defaults already carries vision_support + 262144 context; the
        # explicit repeats below preserve parity with the prior per-model config.
        for _alias in '${llm_aliases}'.split():
            add_model(_alias, 'llm', {**llm_defaults,
                'context_size': '262144', 'vision_support': 'support',
            })

        # ── Embedding + rerank models ─────────────────────────────────────────
        # #1786: read from the manifest, like the chat block above, instead of
        # two typed literals. `nomic-embed-text` was registered unconditionally
        # while the standard set skips it (wildcard weight filename), so Dify
        # validated credentials against a model the box does not serve — a 400
        # on every run. The bash-interpolated lists already carry the same
        # filter the deploy uses.
        for _alias, _ctx in [p.split('=', 1) for p in '${embed_ctx_pairs}'.split()]:
            add_model(_alias, 'text-embedding', {'context_size': _ctx})

        for _alias, _ctx in [p.split('=', 1) for p in '${rerank_ctx_pairs}'.split()]:
            add_model(_alias, 'rerank', {'context_size': _ctx, 'timeout': '600'})

        # ── System default models ─────────────────────────────────────────────
        try:
            for mtype, mname in [
                ('text-generation', '${default_chat}'),
                ('text-embedding',  '${default_embedding}'),
                ('rerank',          'qwen3-reranker'),
            ]:
                try:
                    mps.update_default_model_of_model_type(
                        tenant_id=tenant_id,
                        model_type=mtype,
                        provider=provider,
                        model=mname
                    )
                    print(f'Default {mtype} set to {mname}.')
                except Exception as e:
                    print(f'Default {mtype} error: {e}')
        except Exception as e:
            print('System defaults error:', e)

    except Exception as e_main:
        print('Error:', e_main)
"
    
    echo "$py_script" | docker exec -i dify-api sh -c "cat > /tmp/config_models.py"
    local output rc
    # #2010: the capture has to sit in a condition. post-install.sh runs under
    # `set -e`, so a failing `output=$(...)` ends the script AT THIS LINE and the
    # `rc=$?` below — plus the whole S20 handler that makes the exit code
    # authoritative — never runs. The failure S20 was written to report was the
    # one failure it could not reach. Measured as the same shape in
    # core/init-authentik.sh, where it ended rzfz init with no message at all.
    if output=$(docker exec -i dify-api python3 /tmp/config_models.py 2>&1); then
        rc=0
    else
        rc=$?
    fi

    # S20: the model-config Python sys.exit(1)s on admin/tenant-not-found.
    # Pre-fix the bash only grepped output for "model [" and ignored $?, so a
    # non-zero exit silently passed and post-install "completed" with ZERO
    # Dify models. Make the exit code authoritative + return non-zero so the
    # caller (step_dify_provisioning) fails instead of declaring success.
    if [ "$rc" -ne 0 ]; then
        print_error "Dify model configuration FAILED (python exit $rc):"
        echo "$output" | while IFS= read -r line; do print_error "  $line"; done
        return 1
    fi
    if echo "$output" | grep -q "model \["; then
        print_success "Dify GPUStack models configured."
        echo "$output" | while IFS= read -r line; do print_info "  $line"; done
        # #1338: symmetric to the manager branch — a box that moved BACK to
        # GPUStack must not keep the manager's rows either.
        _dify_drop_inactive_provider_rows "langgenius/gpustack/gpustack"
    else
        # rc==0 but nothing registered — still a failure worth surfacing.
        print_error "Dify model config produced no registrations: $output"
        return 1
    fi
}

step_dify_provisioning() {
    print_step "Dify: Provisioning..."
    # #2195: reset per call — the --refresh arm and the #1507 late pass both
    # call this function again in the same process, and a marker left standing
    # would make a repaired Dify keep reporting the state it was repaired from.
    _DIFY_DEFERRED_UNTIL_READY=0
    
    # Dify API is not exposed to host — check via docker exec
    print_substep "Waiting for Dify API to be ready..."
    local dify_waited=0
    while [ $dify_waited -lt 120 ]; do
        if docker exec dify-api curl -sf --max-time 5 http://localhost:5001/console/api/setup > /dev/null 2>&1; then
            print_success "Dify API is ready."
            break
        fi
        sleep 5
        dify_waited=$((dify_waited + 5))
    done
    if [ $dify_waited -ge 120 ]; then
        print_error "Dify API did not become ready within 120s."
        return 1
    fi
    
    dify_ensure_admin || return 1

    # #1248: WHICH model provider Dify gets follows the box's ACTUAL LLM
    # backend, decided by the PROFILE (the #976 source-of-truth rule) and
    # exclusive in the same direction as #1255's deploy: a box that RUNS GPUStack
    # keeps the GPUStack provider, a Manager-only box gets the OpenAI-compatible
    # provider against the manager's metered /v1. Pre-#1248 the GPUStack branch
    # was unconditional, so a Manager box got a provider for
    # `http://gpustack:9090` — a host that does not resolve there — and ended up
    # with no default text / embedding / rerank model at all.
    # #1968 (Betreiberentscheid 2026-09-12): die Dify-Verdrahtung folgt ab
    # 2026.09 dem LLM MANAGER, nicht mehr der Eigentumsfrage. Woertlich: "the
    # wiring should be fully oriented on llm manager … But gpustack can still be
    # enabled and if the user decides be accessed directly bypassing llm
    # manager … the gpustack plugin should still be there with the latest
    # version. But no models configured, that the user has to do on his own".
    #
    # Damit zerfaellt die eine Frage von #1248 in ZWEI unabhaengige:
    #
    #   Ist GPUStack auf dieser Box?   -> bekommt Dify das gpustack-PLUGIN?
    #   Ist der Manager auf dieser Box? -> wer verdrahtet die MODELLE?
    #
    # Vorher entschied `llm_manager_owns_standard_set` beides zugleich, und auf
    # einer nicht foederierten Doppelbox hiess das: Modelle an GPUStack. Das ist
    # jetzt falsch — der Manager ist die Adresse, GPUStack der Handbetrieb.
    local _dify_gpustack_present=false
    local _dify_manager_present=false
    _gpustack_profile_active     && _dify_gpustack_present=true
    _llm_manager_profile_active  && _dify_manager_present=true

    # The admin-account timezone nudge applies to both branches (#1248).
    _dify_align_account_timezone "$(_resolve_dify_admin_email)"

    # Install plugins (pinned versions with content hashes)
    # These identifiers are from the reference box — update when upgrading plugins
    # Install Dify plugins (using short names will install the latest available versions).
    # A single plugin failing to resolve/install (transient marketplace error, or
    # an upstream plugin without a hardcoded fallback id — e.g. junjiem/mcp_sse)
    # must NOT abort the whole post-install: dify_install_plugin `return 1`s on an
    # unresolvable plugin, and under `set -e` a bare call here aborted everything
    # downstream (Speaches, Gitea, the verify suite). Each install is best-effort;
    # the operator can install any that failed from the Dify marketplace UI later.
    #
    # langgenius/gpustack is installed on every box that RUNS GPUStack — and,
    # since #1968, also when the Manager owns the wiring. The #1248 reason for
    # skipping it holds only where it was measured: on a box with NO GPUStack
    # the plugin is a provider for a host that does not resolve, and every model
    # under it fails credential validation with a NameResolutionError the
    # operator has to read. Where GPUStack IS running, the provider resolves —
    # it simply has no models until the operator adds them, which is exactly
    # what the 2026.09 decision asks for.
    if [ "$_dify_gpustack_present" = true ]; then
        dify_install_plugin "langgenius/gpustack" || print_warning "Dify plugin 'langgenius/gpustack' install failed — install it later via the Dify marketplace."
    else
        print_substep "Skipping the 'langgenius/gpustack' plugin — no GPUStack profile on this box, so its provider would point at a host that does not resolve (#1248)."
    fi
    dify_install_plugin "langgenius/openai_api_compatible" || print_warning "Dify plugin 'langgenius/openai_api_compatible' install failed — install it later via the Dify marketplace."
    dify_install_plugin "abesticode/knowledge_pro" || print_warning "Dify plugin 'abesticode/knowledge_pro' install failed — install it later via the Dify marketplace."
    # M035 P4: MCP client plugin so Dify can use the stack's MCP servers (e.g.
    # cognee-mcp). Runs IN dify-plugin-daemon, which is PROXY-FREE — unlike
    # Dify's built-in Tools->MCP, whose client routes through the SSRF proxy and
    # can't reach internal stack hosts (confirmed in M033 S12). App-builders add
    # the "MCP SSE/StreamableHTTP" tool to a workflow + point it at, e.g.,
    # {"cognee":{"transport":"streamable_http","url":"http://cognee-mcp:8000/mcp"}}.
    dify_install_plugin "junjiem/mcp_sse" || print_warning "Dify plugin 'junjiem/mcp_sse' (MCP client) install failed — install it later via the Dify marketplace."
    dify_pin_model_plugin_strategy
    
    # Wait for plugins to be fully installed before configuring models
    print_substep "Waiting for plugins to finish installing..."
    # #1248: wait for the provider plugin(s) this box actually installed. Waiting
    # on langgenius/gpustack on a Manager box burned the full 120 s and then
    # warned "model configuration may fail" on every run.
    local _dify_required_plugins="langgenius/openai_api_compatible"
    if [ "$_dify_gpustack_present" = true ]; then
        _dify_required_plugins="langgenius/gpustack langgenius/openai_api_compatible"
    fi
    local waited=0
    while [ $waited -lt 120 ]; do
        local installed
        installed=$(dify_api GET "/console/api/workspaces/current/plugin/list?page=1&page_size=100" | \
            python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    ids=[p.get('plugin_id') for p in d.get('plugins',[])]
    req='${_dify_required_plugins}'.split()
    ready = all(r in ids for r in req)
    print('yes' if ready else 'no')
except: print('no')
" 2>/dev/null)
        if [ "$installed" = "yes" ]; then
            print_success "Plugins installed."
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done
    if [ $waited -ge 120 ]; then
        print_warning "Plugin install timed out — model configuration may fail."
    fi

    # S20: propagate model-config failure (admin-not-found / zero models) so
    # post-install fails loudly instead of silently "completing" with no Dify
    # models registered. #1248: same propagation for the Manager branch — a Dify
    # with no model provider is a broken box, not a warning.
    # #2195: `|| _dcm_rc=$?`, not a bare call — under `set -e` a bare non-zero
    # here kills the script, and 3 ("still downloading") is not a failure. The
    # declaration sits OUTSIDE the branch on purpose: #1273's guard asserts that
    # dify_configure_manager_models is the first statement the manager-presence
    # flag selects, and that property is worth keeping literal.
    local _dcm_rc=0
    if [ "$_dify_manager_present" = true ]; then
        dify_configure_manager_models || _dcm_rc=$?
        case "$_dcm_rc" in
            0) ;;
            3) _DIFY_DEFERRED_UNTIL_READY=1 ;;   # still downloading, not broken
            *) return 1 ;;
        esac
        if [ "$_dify_gpustack_present" = true ]; then
            print_info "GPUStack's Dify provider is installed but carries NO models on purpose (#1968): the wiring goes through the LLM Manager. Add GPUStack models in Dify yourself if you want to bypass the manager."
        fi
    else
        # #1968: `dify_configure_model` — the GPUStack model wiring — is
        # deliberately NOT called any more. On a box with no manager that leaves
        # Dify with a working provider and no models, and the operator has to
        # add them. Saying so is the whole of the difference between a decision
        # and a defect: a silent Dify with no models is #1248 all over again.
        print_warning "No LLM Manager profile on this box — Dify gets NO models automatically (#1968). The 'langgenius/gpustack' provider is installed; add the models you want in Dify (Settings -> Model Provider), or enable the llm-manager profile to have them wired for you."
    fi

    # M019 S03 — Seed example KB and (if operator created the matching apps in
    # the UI) extract their service API keys into DIFY_APPS_JSON so the
    # OpenWebUI dify_pipe.py picks them up. Idempotent: KB skipped if already
    # seeded; apps skipped if not yet created. Operator gets a clear NEXT-STEP
    # banner with the exact app names to create.
    if [ -f "${SCRIPT_DIR}/modules/dify/seed-apps.sh" ]; then
        print_step "Seeding Dify example KB + extracting app API keys (M019)..."
        # seed-apps.sh relies on THIS script's helper functions (dify_api,
        # update_env_value, print_*) and its non-exported session vars
        # (DIFY_ACCESS/DIFY_CSRF, SCRIPT_DIR). Running it via `bash` spawns a
        # child that inherits none of them -> `dify_api: command not found`, KB
        # seed a silent no-op (#189; surfaced fleet-wide once the --refresh path
        # started calling step_dify_provisioning). Source it in a SUBSHELL: the
        # fork inherits all functions + vars, while the seeder's own
        # `set -eo pipefail` and `exit 0` stay contained and can't abort us.
        if ! ( . "${SCRIPT_DIR}/modules/dify/seed-apps.sh" ); then
            print_warning "Dify seed-apps.sh exited non-zero (non-fatal). Re-run razzfazz-post-install.sh after creating the apps in the Dify UI."
        fi
    fi

    # #160: model-provider icons are backend-served <img>s (plugin-daemon asset
    # endpoint). Reinstalling the provider plugins above re-extracts their icon
    # assets, so a fresh provision/refresh self-heals a stale icon store. Probe
    # once and report (non-fatal) so the operator sees icon health immediately.
    local dify_broken_icons
    dify_broken_icons=$(dify_broken_provider_icons)
    if [ -n "$dify_broken_icons" ]; then
        print_warning "Dify: model-provider icon endpoint still failing for: $(echo "$dify_broken_icons" | awk '{printf "%s(HTTP %s) ", $1, $2}')(#160). If it persists, capture the icon 'src' status in DevTools -> Network and file it on the issue."
    else
        print_info "Dify: model-provider icons render (#160 check passed)."
    fi

    if [ "${_DIFY_DEFERRED_UNTIL_READY:-0}" = "1" ]; then
        # #2195: everything else in this step DID succeed — the provider, the
        # plugins, the KB seed. What is missing is the model set, because it was
        # not served yet. Say that, and do not let it be read as a green run.
        print_warning "Dify provisioning finished WITHOUT a model set — the models were still downloading when the provider was configured (see above). The box is not broken; it is not finished."
        return 3
    fi
    print_success "Dify provisioning complete."
}

# ==============================================================================
# Open WebUI Provisioning
# ==============================================================================
# owui_api / owui_ensure_admin live in scripts/lib-owui.sh (#908): the
# post-upgrade retrieval reconcile needs the same admin session.


owui_configure_models() {
    print_substep "Configuring Open WebUI models (capabilities & visibility)..."

    # #1266: on a box whose backend is the LLM Manager the rows below do not
    # exist to be patched — there is no `model-sync` container to INSERT them,
    # so `openwebui_db.model` was EMPTY and every model the manager serves
    # (embedder and reranker included) showed up in the chat picker with no
    # capabilities at all. owui_reconcile_model_rows (scripts/lib-owui.sh)
    # WRITES the rows from the manager's own model list and sets visibility by
    # task; it returns non-zero when it could not (manager down, nothing served
    # yet, OWUI not seeded), in which case we fall through to the GPUStack-era
    # path below rather than leaving the models unconfigured. The GPUStack path
    # itself is unchanged.
    # rev-B (review F3): the same box shape #1272 decides with — a dual box that
    # still serves GPUStack keeps the GPUStack-era patching (capabilities +
    # hidden) for the rows `model-sync` writes. Gating on the manager profile
    # alone silently changed behaviour for exactly that shape.
    if llm_manager_owns_standard_set \
       && owui_reconcile_model_rows; then   # #1441
        return 0
    fi

    # Wait for models to sync from GPUStack
    local waited=0
    local synced_models=0
    while [ $waited -lt 30 ]; do
        synced_models=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -t -c "SELECT count(*) FROM model;" 2>/dev/null | xargs)
        if [ -n "$synced_models" ] && [ "$synced_models" -gt 0 ]; then
            break
        fi
        sleep 2
        waited=$((waited + 2))
    done
    
    if [ "$synced_models" -eq 0 ]; then
        print_info "No models synced to Open WebUI yet. Skipping model configuration."
        return 0
    fi
    
    # We use a python one-liner inside the postgres container to update the JSON safely
    local update_cmd="
import json, sys
def process_meta(meta_str, hidden_val, image_gen_val, vision_val):
    try:
        m = json.loads(meta_str) if meta_str and meta_str != 'null' else {}
    except:
        m = {}
    if hidden_val != 'keep':
        m['hidden'] = hidden_val == 'true'
    if 'capabilities' not in m: m['capabilities'] = {}
    if image_gen_val != 'keep':
        m['capabilities']['image_generation'] = image_gen_val == 'true'
    if vision_val != 'keep':
        m['capabilities']['vision'] = vision_val == 'true'
    return json.dumps(m)

args = sys.argv[1:]
print(process_meta(args[0], args[1], args[2], args[3]))
"

    update_model() {
        local model_id="$1"
        local hidden="$2"
        local image_gen="$3"
        local vision="$4"
        
        local current_meta
        current_meta=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -t -c "SELECT meta FROM model WHERE id = '${model_id}';" 2>/dev/null | xargs)
        
        if [ -n "$current_meta" ]; then
            local new_meta
            new_meta=$(python3 -c "$update_cmd" "$current_meta" "$hidden" "$image_gen" "$vision")
            # Escape single quotes for SQL
            new_meta=$(echo "$new_meta" | sed "s/'/''/g")
            docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c "UPDATE model SET meta='${new_meta}' WHERE id='${model_id}';" > /dev/null 2>&1
        fi
    }

    # Hide auxiliary models
    update_model "nomic-embed-text" "true" "keep" "keep"
    update_model "qwen3-embedding" "true" "keep" "keep"
    update_model "qwen3-reranker" "true" "keep" "keep"
    
    # Configure capabilities (vision on, image_generation off) for every
    # LLM-category model that ships under this preset. S19: read from
    # standard-models.yaml — replaces the hardcoded gemma4 + qwen3.5 pair
    # (the latter drifted to qwen3.6, leaving OWUI mis-tagging the chat model).
    local _owui_preset="${PRESET:-standard}"
    while read -r _llm; do
        [ -n "$_llm" ] && update_model "$_llm" "keep" "false" "true"
    done < <(_model_rows_for_preset "$_owui_preset" | awk -F'\t' '$6 ~ /(^|,)chat(,|$)/{print $1}')

    print_success "Open WebUI models configured."
}

# ==============================================================================
# #1266 / #1252 — OWUI model rows + ".env agrees with the persisted config"
# ==============================================================================
# Three findings the pre-existing "Open WebUI: connection configured" check
# cannot see, because it only asks OWUI whether SOME key is set:
#
#   1. the chat models the backend serves have no `model` row, so they carry no
#      capabilities and day-1's model-count checks stay red;
#   2. an embedding / reranker / doc-conversion model IS offered in the chat
#      picker (what a user reads as "three models that cannot talk");
#   3. `.env` and the persisted DB config disagree about the backend URL or its
#      key — the two-truths state (#1185's class): chat works today because the
#      DB is right, and the next upgrade re-seeds OWUI from the WRONG .env.
#
# Registered through post-install's own verify_check, so counts and the summary
# block are the shared ones. Best-effort: never returns non-zero.
# #1401: the Pipelines server must be a registered Open WebUI connection,
# otherwise every pipeline filter (openlit_filter — #245 chat attribution) is
# silently inert. Judged from the same endpoint the registration reads back.
verify_owui_pipelines_registered() {
    local listed
    if [ -z "${OWUI_TOKEN:-}" ]; then
        verify_check "Open WebUI: Pipelines server registered (pipeline filters active)" "fail" \
            "not judged: no Open WebUI admin token in this run"
        return 0
    fi
    listed=$(owui_api GET "/api/v1/pipelines/list" 2>/dev/null) || listed=""
    if printf '%s' "$listed" | grep -q 'pipelines:9099'; then
        verify_check "Open WebUI: Pipelines server registered (pipeline filters active)" "pass"
    else
        verify_check "Open WebUI: Pipelines server registered (pipeline filters active)" "fail" \
            "GET /api/v1/pipelines/list has no http://pipelines:9099 — openlit_filter never runs (#1401). Re-run 'rzfz post-install --refresh'."
    fi
}

verify_owui_model_rows_and_env() {
    echo "${COMPOSE_PROFILES:-}" | grep -qw "chat" || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx openwebui || return 0
    if ! _llm_manager_profile_active && ! _gpustack_profile_active; then
        return 0
    fi
    print_substep "Checking Open WebUI model rows + .env/DB agreement..."

    # ── (1)+(2) the rows, against the manager's own model list ──────────────
    if llm_manager_owns_standard_set; then   # #1441
        local args out rc=0 vkey
        # A verify must not MUTATE: pass the .env key rather than letting the
        # resolver mint one (and write .env) from inside a read-only check.
        vkey="${LLM_MANAGER_OWUI_KEY:-}"
        [ -n "$vkey" ] || vkey=$(read_env_value "$ENV_FILE" LLM_MANAGER_OWUI_KEY 2>/dev/null) || vkey=""
        # rev-B (review F1): passing the key was not enough. With an EMPTY key
        # `_owui_manager_model_args` falls through to `_owui_llm_key` →
        # `_owui_resolve_manager_key`, which MINTS a `stack/openwebui` key and
        # writes it to .env — from inside a read-only check, on every run, so a
        # box with a dead key rotated its key at each `--verify`. A missing key
        # is a FINDING here, never something a verify repairs.
        if [ -z "$vkey" ]; then
            verify_check "Open WebUI: chat-model rows written for every served chat model" "fail" \
                "LLM_MANAGER_OWUI_KEY is empty — nothing to ask the manager with. Run 'rzfz post-install --preset <preset>' (or --refresh) to mint and wire it; --verify never writes .env (#1266/#1252)."
            verify_check "Open WebUI: no embedding/reranker model in the chat picker" "fail" \
                "not judged: LLM_MANAGER_OWUI_KEY is empty (see the line above)"
            args=""
        else
            args=$(_owui_manager_model_args "$vkey") || args=""
        fi
        if [ -z "$vkey" ]; then
            :  # already reported above — do not repeat the generic message
        elif [ -z "$args" ]; then
            verify_check "Open WebUI: chat-model rows written for every served chat model" "fail" \
                "the LLM Manager served no model to LLM_MANAGER_OWUI_KEY — either nothing is deployed (#1250b) or that key is dead (#1185); see the 'LLM Manager:' checks above"
        else
            local -a model_args=()
            local line
            while IFS= read -r line; do
                [ -n "$line" ] && model_args+=(--model "$line")
            done <<< "$args"
            out=$(python3 "${SCRIPT_DIR}/scripts/owui_model_rows.py" verify \
                "${model_args[@]}" \
                --manifest "${SCRIPT_DIR}/core/llm/standard-models.yaml" \
                --db "${OPENWEBUI_DB:-openwebui_db}" \
                --pg-user "${POSTGRES_USER:-docker}" 2>&1) || rc=$?
            if [ "$rc" -ne 0 ]; then
                verify_check "Open WebUI: chat-model rows written for every served chat model" "fail" \
                    "could not read openwebui_db.model: $(printf '%s\n' "$out" | head -n1) (#1266)"
            else
                local absent visible
                absent=$(printf '%s\n' "$out" | sed -n 's/^\(MISSING\|INACTIVE\) //p' | tr '\n' ' ')
                visible=$(printf '%s\n' "$out" | sed -n 's/^VISIBLE //p' | tr '\n' ' ')
                if [ -z "$absent" ]; then
                    verify_check "Open WebUI: chat-model rows written for every served chat model" "pass"
                else
                    verify_check "Open WebUI: chat-model rows written for every served chat model" "fail" \
                        "no usable row for: ${absent}— they carry no capabilities and the chat model list is built from these rows; re-run 'rzfz post-install --refresh' (#1266)"
                fi
                if [ -z "$visible" ]; then
                    verify_check "Open WebUI: no embedding/reranker model in the chat picker" "pass"
                else
                    verify_check "Open WebUI: no embedding/reranker model in the chat picker" "fail" \
                        "offered as chat models: ${visible}— they cannot answer a prompt; re-run 'rzfz post-install --refresh' (#1266)"
                fi
            fi
        fi
    fi

    # ── (3) .env vs the persisted config ────────────────────────────────────
    local url env_bases env_keys env_key db_out db_present db_key
    url=$(_owui_llm_base_url)
    env_bases=$(read_env_value "$ENV_FILE" OWUI_OPENAI_BASE_URLS 2>/dev/null) || env_bases=""
    env_keys=$(read_env_value "$ENV_FILE" OWUI_OPENAI_KEYS 2>/dev/null) || env_keys=""
    env_key=$(_owui_env_endpoint_key "$env_bases" "$env_keys" "$url")
    db_out=$(python3 "${SCRIPT_DIR}/scripts/owui_config_reconcile.py" pair --url "$url" \
        --db "${OPENWEBUI_DB:-openwebui_db}" --pg-user "${POSTGRES_USER:-docker}" 2>/dev/null) || db_out=""
    db_present=$(printf '%s\n' "$db_out" | sed -n 's/^URL_PRESENT=//p' | head -n1)
    db_key=$(printf '%s\n' "$db_out" | sed -n 's/^KEY=//p' | head -n1)
    case ";${env_bases};" in
        *";${url};"*) ;;
        *)  verify_check "Open WebUI: .env connection matches the persisted config" "fail" \
                "OWUI_OPENAI_BASE_URLS in .env does not carry the active backend ${url} (it holds '${env_bases:-<empty>}'), so the container env feeds compose's GPUStack default while the DB is wired to the manager — the next re-seed clobbers the working connection (#1252)"
            return 0 ;;
    esac
    if [ "$env_key" = "gpustack_CHANGEME_AFTER_FIRST_START" ]; then
        verify_check "Open WebUI: .env connection matches the persisted config" "fail" \
            "OWUI_OPENAI_KEYS still carries the gpustack placeholder for ${url} (#1252)"
    elif [ -z "$db_out" ] || [ "$db_present" != "1" ]; then
        verify_check "Open WebUI: .env connection matches the persisted config" "fail" \
            "the persisted config holds no entry for ${url} — .env and the DB disagree (#1252)"
    elif [ "$env_key" != "$db_key" ]; then
        verify_check "Open WebUI: .env connection matches the persisted config" "fail" \
            "the key .env pairs with ${url} is not the one the persisted config holds — two truths, and an upgrade re-seeds from .env (#1252)"
    else
        verify_check "Open WebUI: .env connection matches the persisted config" "pass"
    fi
    return 0
}



owui_configure_connection() {
    # #1013 (EXO-5): resolve through the same _owui_llm_base_url/_owui_llm_key
    # pair the #1003 repoint uses (manager-first, gpustack fallback), and skip
    # cleanly when neither backend profile is active instead of posting a dead
    # endpoint.
    #
    # #1185: this used to POST a NESTED {"openai": {...}} to
    # /api/v1/configs/import. On OWUI 0.11's per-key `config` table that writes
    # a dead `openai` blob row OWUI never reads (the "legacy blob" from the
    # 0.91 forensics) and asserts nothing about the live connection — while the
    # key it carried was the UNVALIDATED .env value. The connection is now
    # asserted through owui_reconcile_openai_keys: an upsert per base URL
    # (never a duplicate), the whole key family as one set, and a key that
    # _owui_llm_key has already validated against the manager.
    local llm_base llm_key
    llm_base=$(_owui_llm_base_url)
    llm_key=$(_owui_llm_key)
    if ! _llm_manager_profile_active && ! _gpustack_profile_active; then
        print_substep "No LLM backend profile active — skipping OWUI OpenAI connection config."
        return 0
    fi
    # #1445 (C5c) re-review: this is the FOURTH authoritative write into OWUI's
    # persisted config — owui_reconcile_openai_keys upserts the connection row,
    # rag.openai.api_key and the reranker URL, writes the .env pair and
    # recreates `openwebui`. It asked the profiles and never the liveness, so
    # three lines before owui_configure_embedding correctly refuses, this one
    # stamped `http://llm:8080/v1` in while the manager was down. Same guard,
    # same reason: skipping is recoverable, a wrong authoritative write is not
    # (#976).
    if _llm_manager_profile_active && ! _llm_manager_running; then
        print_warning "llm-manager profile is enabled but the container is not running — SKIPPING the authoritative OWUI OpenAI-connection write so it is not repointed at a backend this box does not run. Re-run 'rzfz post-install --refresh' once llm-manager is up."
        return 0
    fi
    print_substep "Configuring LLM OpenAI-compatible connection (${llm_base})..."
    owui_reconcile_openai_keys "$llm_base" "$llm_key" "$(_owui_rerank_url)"
    # Signup stays closed on a provisioned box. FLAT dotted key — the per-key
    # store is keyed by path, a nested {"ui": {...}} would be another dead blob.
    local resp
    resp=$(owui_api POST "/api/v1/configs/import" '{"config": {"ui.enable_signup": false}}')
    if echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('ui.enable_signup') is False or d.get('ui', {}).get('enable_signup') is False" 2>/dev/null; then
        print_success "OpenAI-compatible connection configured (${llm_base})."
    else
        print_warning "Could not verify the OWUI connection/signup config."
    fi
}

owui_configure_audio() {
    print_substep "Configuring Speech-to-Text and Text-to-Speech (Speaches)..."
    
    # Get current config, modify, send back (API requires all fields)
    local result
    result=$(python3 -c "
import json, urllib.request

headers = {'Authorization': 'Bearer ${OWUI_TOKEN}', 'Content-Type': 'application/json'}
url = 'http://127.0.0.1:${OPENWEBUI_PORT:-8080}'

req = urllib.request.Request(f'{url}/api/v1/audio/config', headers=headers)
current = json.loads(urllib.request.urlopen(req).read())

# STT: Speaches with faster-whisper
current['stt']['ENGINE'] = 'openai'
current['stt']['OPENAI_API_BASE_URL'] = 'http://speaches:8000/v1'
current['stt']['OPENAI_API_KEY'] = 'sk-111'
current['stt']['MODEL'] = 'Systran/faster-whisper-small'

# TTS: Speaches with piper German voice
current['tts']['ENGINE'] = 'openai'
current['tts']['OPENAI_API_BASE_URL'] = 'http://speaches:8000/v1'
current['tts']['OPENAI_API_KEY'] = 'sk-111'
current['tts']['MODEL'] = 'ufozone/piper-de_DE-jarvis-high'
current['tts']['VOICE'] = 'de_DE-jarvis-high'
current['tts']['SPLIT_ON'] = 'punctuation'

data = json.dumps(current).encode()
req = urllib.request.Request(f'{url}/api/v1/audio/config/update', data=data, headers=headers)
resp = json.loads(urllib.request.urlopen(req).read())
print('OK' if resp.get('tts',{}).get('ENGINE') == 'openai' else 'FAIL')
" 2>/dev/null)
    
    if [ "$result" = "OK" ]; then
        print_success "Audio config set (STT: faster-whisper-small, TTS: piper-de_DE-jarvis-high)."
    else
        print_warning "Audio config update may have failed."
    fi
}

owui_configure_embedding() {
    # Fleet standard embedding (qwen3-embedding) — matches prod + the Dify/cognee/
    # lightrag RAG path. Read from standard-models.yaml defaults.embedding so it
    # never drifts back to the old nomic hardcode.
    local emb_alias
    emb_alias=$(_default_embedding_alias)

    # #243: OWUI's Chroma collections are built at whatever dimension the
    # embedding model emitted AT INSERT TIME. Flip RAG_EMBEDDING_MODEL to a
    # different alias (prod: all-MiniLM-L6-v2 384-dim -> qwen3-embedding
    # 2560-dim) and every existing knowledge base keeps its OLD-dim vectors
    # while OWUI now queries at the NEW dim — collections either
    # dimension-mismatch or 404 (chromadb.errors.NotFoundError), and RAG
    # silently answers "Keine Quellen gefunden" with nothing in the run
    # output to explain why (chat model irrelevant). Persist the model we
    # last successfully applied and compare on every run so a change is
    # reported instead of swapped in silently — same idea as the LightRAG/
    # Cognee live-dim probe (#413/#70), applied to OWUI's missing guard.
    # `|| true` is load-bearing under `set -eo pipefail`: grep finds nothing
    # on a fresh box / first run and must not kill it (#200/#793 shape).
    local prior_emb
    prior_emb=$(grep -m1 '^OWUI_EMBEDDING_MODEL_APPLIED=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-) || true
    if [ -n "$prior_emb" ] && [ "$prior_emb" != "$emb_alias" ]; then
        print_warning "OWUI embedding model changed ($prior_emb -> $emb_alias) — RE-INDEX REQUIRED: existing knowledge-base collections are still built at the OLD model's dimension and will silently return zero sources (dimension mismatch / Chroma NotFoundError) until every KB is re-indexed against $emb_alias (#243)."
    fi

    # #976: resolve the OpenAI-compatible embedding backend — the LLM Manager
    # (metered) when it is active, else GPUStack. Hardcoding gpustack:9090 here
    # left RAG embedding pointed at a dead backend on a manager-only box.
    #
    # Same rule as owui_configure_retrieval_env: this POST is authoritative, so
    # a manager box whose manager is momentarily down must not have its
    # embedding endpoint rewritten to GPUStack.
    # #1445 (C5c / E1): the address is canonical on every box, so the
    # precondition is "is the manager supposed to run here, and does it?"
    # — not "does it own the models?". See scripts/lib-owui.sh.
    if _llm_manager_profile_active && ! _llm_manager_running; then   # #1445
        print_warning "llm-manager profile is enabled but the container is not running — SKIPPING the authoritative OWUI embedding-config write. Re-run 'rzfz post-install --refresh' once llm-manager is up."
        return 0
    fi
    local _emb_url _emb_key _emb_backend
    _emb_url=$(_owui_llm_base_url)
    _emb_key=$(_owui_llm_key)
    # One decision, reported honestly: the label must name the backend the URL
    # above actually points at. Deriving it from a SECOND probe let a state flip
    # mid-function print "GPUStack" for a manager URL (and vice versa).
    # #1445 (C5c / E1): _owui_llm_base_url is canonical now, so the honest label
    # follows the same question _llm_backend_display_name asks — is the manager
    # the front of this box? Spelled out rather than delegated: this function is
    # extracted and run on its own by test_243, and a call into lib-owui.sh
    # would make the label depend on a stub instead of on the rule.
    if _llm_manager_profile_active; then _emb_backend="LLM Manager"; else _emb_backend="GPUStack"; fi   # #1445
    print_substep "Configuring embedding model ($emb_alias via $_emb_backend)..."
    local resp
    resp=$(owui_api POST "/api/v1/retrieval/embedding/update" "{
        \"RAG_EMBEDDING_ENGINE\": \"openai\",
        \"RAG_EMBEDDING_MODEL\": \"$emb_alias\",
        \"RAG_EMBEDDING_BATCH_SIZE\": 1,
        \"ENABLE_ASYNC_EMBEDDING\": false,
        \"RAG_EMBEDDING_CONCURRENT_REQUESTS\": 1,
        \"openai_config\": {
            \"url\": \"$_emb_url\",
            \"key\": \"$_emb_key\"
        }
    }")

    if echo "$resp" | python3 -c "import sys,json; assert json.load(sys.stdin).get('RAG_EMBEDDING_MODEL') == '$emb_alias'" 2>/dev/null; then
        print_success "Embedding model configured ($emb_alias)."
        update_env_value "$ENV_FILE" "OWUI_EMBEDDING_MODEL_APPLIED" "$emb_alias"
    else
        print_warning "Embedding config update may have failed."
    fi
}

owui_configure_content_extraction() {
    # #23: when the docling profile is enabled, make docling the Open WebUI document
    # extractor so uploads route through the internal docling server. Profile-gated —
    # a no-op otherwise (OWUI keeps its built-in extractor; the compose default for
    # OWUI_CONTENT_EXTRACTION_ENGINE is empty, safe when docling isn't running).
    if ! echo "${COMPOSE_PROFILES:-}" | grep -qw "docling"; then
        return 0
    fi
    print_substep "Setting docling as the Open WebUI document extractor..."
    update_env_value "$ENV_FILE" "OWUI_CONTENT_EXTRACTION_ENGINE" "docling"
    # Best-effort: also push to a running OWUI so it doesn't wait for a recreate.
    owui_api POST "/api/v1/retrieval/config/update" \
        "{\"CONTENT_EXTRACTION_ENGINE\": \"docling\", \"DOCLING_SERVER_URL\": \"http://docling:5001\"}" \
        >/dev/null 2>&1 || true
    print_success "docling set as the OWUI content extractor (applies on next OWUI recreate)."
}

# #908: _owui_retrieval_defaults (THE retrieval defaults, in ONE place) and
# _owui_retrieval_mismatches (the read-back verdict) live in
# scripts/lib-owui.sh — the same table + push serve post-install (--preset /
# --refresh) AND the post-upgrade reconcile in cli/upgrade.sh.


owui_configure_retrieval_env() {
    # This function performs an AUTHORITATIVE write: it rewrites OWUI's
    # persisted RAG config (embedding + completion + reranker endpoints). On a
    # box wired to the LLM Manager, doing that while the manager is DOWN used
    # to silently repoint RAG at GPUStack and leave it there. Refuse to write
    # instead: the config on disk is already correct, and a later run with the
    # manager up applies any real change. Skipping is recoverable; a wrong
    # authoritative write is not (#976).
    # #1445 (C5c / E1): the address is canonical on every box, so the
    # precondition is "is the manager supposed to run here, and does it?"
    # — not "does it own the models?". See scripts/lib-owui.sh.
    if _llm_manager_profile_active && ! _llm_manager_running; then   # #1445
        print_warning "llm-manager profile is enabled but the container is not running — SKIPPING the authoritative OWUI retrieval-config write so it is not repointed at a backend this box does not run. Re-run 'rzfz post-install --refresh' once llm-manager is up."
        return 0
    fi

    # NOTE: The retrieval config/update API has a bug in Open WebUI 0.8.12
    # (references non-existent FETCH_URL_MAX_CONTENT_LENGTH config key).
    # Workaround: set via environment variables in modules/chat/compose.yml
    # These are read by PersistentConfig on first startup and stored in DB.
    print_substep "Setting retrieval config via environment variables..."
    
    local compose_file="${SCRIPT_DIR}/modules/chat/compose.yml"
    # #976: resolve the RAG completion + reranker backend once — the LLM
    # Manager (metered) when active, else GPUStack. Both were hardcoded to
    # gpustack:9090, so on a manager-only box RAG completion 404'd and the
    # reranker silently fell back to unranked hits.
    local _rag_url _rag_key _rerank_url
    _rag_url=$(_owui_llm_base_url)
    _rag_key=$(_owui_llm_key)
    _rerank_url=$(_owui_rerank_url)
    # #908: the STATIC retrieval defaults come from _owui_retrieval_defaults —
    # the same table the authoritative API push below and the read-back verdict
    # read. Chunking rationale (unchanged, and the reason the values are what
    # they are): the markdown-header splitter (OWUI default ON) splits each doc
    # per ## section, orphaning the facts (rates) from the entity (client name
    # in the header) — retrieval then cannot connect "client + topic". Turn it
    # OFF and use a whole-document character chunk (≤ qwen3-embedding's
    # 2048-token ctx ≈ ~5000 chars) so client + facts stay in one chunk. Also
    # disable the LLM retrieval-query rewrite (it mistranslates domain terms,
    # e.g. "Tagsätze"→bank "Tagesgeldsatz"). See project_owui_rag_recipe
    # (2026-06-13); measured again live in #908 (price list rank #18/55 → #3).
    local _row _env_key _api_key _val _typ
    while IFS='|' read -r _env_key _api_key _val _typ; do
        [ -n "$_env_key" ] || continue
        update_env_value "$ENV_FILE" "$_env_key" "$_val"
    done <<< "$(_owui_retrieval_defaults)"

    # The per-box RESOLVED values (#976) and the web-search block: not part of
    # the static table because they are computed here, not declared.
    local env_vars_needed=(
        # OpenWebUI's external reranker POSTs the body to RAG_EXTERNAL_RERANKER_URL
        # AS-IS — it does NOT append "/rerank" the way the embedding code appends
        # "/embeddings" to the OpenAI base URL. The full endpoint must be given
        # here, otherwise every retrieval emits "404 Client Error" + a silent
        # fall-back to unranked vector hits, dropping retrieval quality.
        # See memory: project_openwebui_external_reranker_url.md.
        "RAG_EXTERNAL_RERANKER_URL=$_rerank_url"
        "RAG_EXTERNAL_RERANKER_API_KEY=$_rag_key"
        "ENABLE_WEB_SEARCH=true"
        "WEB_SEARCH_ENGINE=searxng"
        "SEARXNG_QUERY_URL=http://searxng:8080/search?q=<query>"
        "WEB_SEARCH_RESULT_COUNT=5"
        "RAG_OPENAI_API_BASE_URL=$_rag_url"
        "RAG_OPENAI_API_KEY=$_rag_key"
    )

    # Add env vars to the openwebui service environment in compose
    # We do this by updating .env which is referenced by the compose file
    for var in "${env_vars_needed[@]}"; do
        local key="${var%%=*}"
        local val="${var#*=}"
        update_env_value "$ENV_FILE" "$key" "$val"
    done

    print_success "Retrieval env vars set in .env (seed for fresh first-start)."

    # #908 follow-up: the authoritative API push + read-back verdict is ONE
    # implementation in scripts/lib-owui.sh, shared with the post-upgrade
    # reconcile in cli/upgrade.sh (pure API push, no model deploy).
    owui_push_retrieval_defaults "$_rag_url" "$_rag_key" "$_rerank_url"
}

owui_upload_pipes() {
    print_substep "Uploading per-user agent pipes (hermes/moltis/opencode/dify)..."
    # The hermes/moltis/opencode/dify pipes ship as files at
    # modules/chat/functions/*.py but are NOT auto-loaded by OpenWebUI — they have
    # to be POSTed into /api/v1/functions/. Without this step the "Open in
    # Chat" deep-link from agent-manager (chat.<domain>/?models=hermes.personal)
    # has no matching model and the user lands on chat with nothing
    # selected.
    #
    # Pipes carry `# @include _lib/<file>` directives that we inline before
    # upload. The included helper files may carry their own
    # `from __future__` imports — strip those from inlined content because
    # `from __future__` must appear at the very top of a Python source
    # file (mid-file → SyntaxError → upload validation rejects).
    #
    # Each pipe is uploaded once (create) on first install, then updated
    # in place on subsequent runs (POST /api/v1/functions/id/<id>/update).
    local pipes_dir="${SCRIPT_DIR}/modules/chat/functions"
    local lib_dir="${pipes_dir}/_lib"
    for pipe_file in hermes_pipe moltis_pipe opencode_pipe dify_pipe cognee_pipe; do
        local src="${pipes_dir}/${pipe_file}.py"
        [ -f "$src" ] || continue
        local work
        work=$(mktemp)
        # Inline @include directives, strip __future__ from included content
        python3 - "$src" "$lib_dir" "$work" <<'PYEOF'
import os, re, sys
src_path, lib_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(src_path).read()
def repl(m):
    inc = m.group(1).strip()
    # _lib/<file>.py — resolve relative to modules/chat/functions/
    p = os.path.join(os.path.dirname(src_path), inc)
    if not os.path.exists(p):
        return m.group(0)
    inc_text = open(p).read()
    inc_text = re.sub(r"^from __future__.*$", "", inc_text, flags=re.M)
    return inc_text
out = re.sub(r"^# @include (.+)$", repl, src, flags=re.M)
open(out_path, "w").write(out)
PYEOF
        # Extract id/title/description from the YAML-style header
        local pipe_id pipe_title pipe_desc
        pipe_id=$(grep -m1 '^id:' "$work" | sed 's/^id:[[:space:]]*//' | tr -d ' \r')
        pipe_title=$(grep -m1 '^title:' "$work" | sed 's/^title:[[:space:]]*//' | tr -d '\r')
        pipe_desc=$(grep -m1 '^description:' "$work" | sed 's/^description:[[:space:]]*//' | tr -d '\r')
        # Build JSON body
        local body
        body=$(WORK="$work" PIPE_ID="$pipe_id" PIPE_TITLE="$pipe_title" PIPE_DESC="$pipe_desc" python3 -c '
import json, os
print(json.dumps({
    "id": os.environ["PIPE_ID"],
    "name": os.environ["PIPE_TITLE"],
    "meta": {"description": os.environ["PIPE_DESC"], "manifest": {}},
    "content": open(os.environ["WORK"]).read(),
    "type": "pipe",
}))')
        # Try create; on "already registered" fall through to update
        local create_resp
        create_resp=$(echo "$body" | docker exec -i openwebui curl -s -X POST \
            -H "Authorization: Bearer $OWUI_TOKEN" -H 'Content-Type: application/json' \
            -d @- "http://localhost:8080/api/v1/functions/create" 2>/dev/null || true)
        if echo "$create_resp" | grep -q '"already registered"'; then
            echo "$body" | docker exec -i openwebui curl -s -X POST \
                -H "Authorization: Bearer $OWUI_TOKEN" -H 'Content-Type: application/json' \
                -d @- "http://localhost:8080/api/v1/functions/id/${pipe_id}/update" >/dev/null 2>&1 || true
            print_info "  Updated existing pipe: $pipe_id"
        else
            print_info "  Created pipe: $pipe_id"
        fi
        # Ensure it's active (toggle once if currently disabled)
        local state
        state=$(docker exec openwebui curl -s -H "Authorization: Bearer $OWUI_TOKEN" \
            "http://localhost:8080/api/v1/functions/id/${pipe_id}" \
            | python3 -c 'import json,sys;print(json.load(sys.stdin).get("is_active","?"))' 2>/dev/null)
        if [ "$state" = "False" ]; then
            docker exec openwebui curl -s -X POST -H "Authorization: Bearer $OWUI_TOKEN" \
                "http://localhost:8080/api/v1/functions/id/${pipe_id}/toggle" >/dev/null 2>&1 || true
            print_info "  Activated pipe: $pipe_id"
        fi
        rm -f "$work"
    done
    print_success "Pipe upload complete."
}

# M035 P3: seed Open WebUI native MCP tool-server connections from the registry.
# OWUI 0.9.x supports type='mcp' (streamable-HTTP) connections via
# POST /api/v1/configs/tool_servers. core/mcp/sync.py --emit owui produces the
# connection list (filtered by COMPOSE_PROFILES, secrets resolved from .env).
# Merge-preserving by URL so operator-added servers + prior registry entries
# survive. Idempotent.
owui_seed_mcp_servers() {
    [ -f "$SCRIPT_DIR/core/mcp/sync.py" ] || return 0
    local emit
    emit=$(python3 "$SCRIPT_DIR/core/mcp/sync.py" --emit owui 2>/dev/null)
    [ -z "$emit" ] || [ "$emit" = "[]" ] && { print_substep "No registry MCP servers eligible for Open WebUI."; return 0; }
    print_step "Open WebUI: seeding MCP tool servers from the registry..."
    local current merged
    current=$(docker exec openwebui curl -s -H "Authorization: Bearer $OWUI_TOKEN" \
        http://localhost:8080/api/v1/configs/tool_servers 2>/dev/null)
    merged=$(EMIT="$emit" CURRENT="$current" python3 - <<'PY'
import json, os
emit = json.loads(os.environ["EMIT"])
try:
    cur = json.loads(os.environ.get("CURRENT") or "{}").get("TOOL_SERVER_CONNECTIONS", [])
except Exception:
    cur = []
by_url = {c.get("url"): c for c in cur}
for c in emit:                      # registry entries win for their URL
    by_url[c["url"]] = c
print(json.dumps({"TOOL_SERVER_CONNECTIONS": list(by_url.values())}))
PY
)
    if echo "$merged" | docker exec -i openwebui curl -s -o /dev/null -w '%{http_code}' \
        -X POST http://localhost:8080/api/v1/configs/tool_servers \
        -H "Authorization: Bearer $OWUI_TOKEN" -H 'Content-Type: application/json' -d @- 2>/dev/null \
        | grep -q '^200$'; then
        print_substep "MCP tool servers seeded ($(echo "$emit" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))') server(s))."
    else
        print_warning "Open WebUI MCP tool-server seeding returned non-200 (non-fatal)."
    fi
}

step_openwebui_provisioning() {
    print_step "Open WebUI: Provisioning..."

    # #178 review: every sub-step below used to be called as a bare
    # statement and the function ENDED on print_success — a bash function's
    # implicit return status is that of its LAST command, so this function
    # structurally returned 0 regardless of what happened above, even when
    # Open WebUI never came up or admin auth failed. Those two are genuine
    # FAILs and must propagate (mirrors step_dify_provisioning's
    # `... || return 1` pattern).
    wait_for_service "Open WebUI" "http://127.0.0.1:${OPENWEBUI_PORT:-8080}/health" 120 || return 1
    owui_ensure_admin || return 1
    # The remaining sub-steps are genuinely best-effort — each one already
    # warns internally on a failed API call and never returns non-zero for a
    # partial-config failure (owui_configure_content_extraction's only
    # `return 0` is the deliberate docling-profile-disabled no-op). Leaving
    # them un-guarded is correct: a reranker/audio/embedding config hiccup
    # must not mask (or be conflated with) the two real failure modes above.
    owui_configure_connection
    # #1401: pipeline filters (openlit_filter, #245) run only through a
    # registered Pipelines server — register it right after the inference
    # connection, idempotently.
    owui_register_pipelines_connection
    owui_configure_audio
    owui_configure_embedding
    owui_configure_content_extraction
    owui_configure_retrieval_env
    owui_configure_models
    owui_upload_pipes
    owui_seed_mcp_servers

    print_success "Open WebUI provisioning complete."
}

# ==============================================================================
# Verification Suite
# ==============================================================================
VERIFY_PASS=0
VERIFY_FAIL=0
VERIFY_RESULTS=""

verify_check() {
    local name="$1" result="$2" detail="$3"
    if [ "$result" = "pass" ]; then
        VERIFY_PASS=$((VERIFY_PASS + 1))
        VERIFY_RESULTS="${VERIFY_RESULTS}  ${GREEN}✓ PASS${NC}  $name\n"
    else
        VERIFY_FAIL=$((VERIFY_FAIL + 1))
        VERIFY_RESULTS="${VERIFY_RESULTS}  ${RED}✗ FAIL${NC}  $name  ($detail)\n"
    fi
}

# ==============================================================================
# #1486 — a RAG consumer runs with the wiring .env says, not the exported copy
# ==============================================================================
# "Cognee: healthy" proves /health answers; it says nothing about WHICH backend
# the container talks to. On the Round-6 clean install (0.175) the container
# held gpustack:9090 / gemma4 / nomic-embed (template defaults) while .env held
# the manager values — every embedding 422'd, the graph stayed empty, --verify
# was green. Compare the running container's environment with the persisted
# .env for the keys compose maps 1:1. An empty .env value means "compose
# default" and is skipped; API keys are deliberately not compared here (they
# would land in the verify report).
#   $1 = container, $2 = display label, $3 = .env key prefix,
#   $4.. = ENVSUFFIX:CONTAINERVAR pairs
verify_rag_consumer_wiring() {
    local svc="$1" label="$2" prefix="$3"
    shift 3
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$svc" || return 0
    local ctr_env pair env_key ctr_var want have drift="" compared=0
    ctr_env=$(docker inspect "$svc" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null) || ctr_env=""
    for pair in "$@"; do
        env_key="${prefix}_${pair%%:*}"
        ctr_var="${pair#*:}"
        want=$(read_env_value "$ENV_FILE" "$env_key" 2>/dev/null || true)
        [ -n "$want" ] || continue
        compared=$((compared + 1))
        have=$(printf '%s\n' "$ctr_env" | grep -E "^${ctr_var}=" | head -n1 | cut -d'=' -f2-)
        if [ "$have" != "$want" ]; then
            drift="${drift:+$drift; }${ctr_var}=${have:-<unset>} (.env ${env_key}=${want})"
        fi
    done
    if [ "$compared" = "0" ]; then
        # rev-B befund 3: every key empty in .env means the box runs on compose
        # defaults — nothing was compared, so "matches" would be a green tick
        # for a check that never happened.
        verify_check "${label}: wiring matches .env" "fail" \
            "no ${prefix}_* endpoint/model values in ${ENV_FILE} — the container runs on compose defaults; run 'rzfz post-install --refresh' to wire ${svc}"
    elif [ -z "$drift" ]; then
        verify_check "${label}: wiring matches .env" "pass"
    else
        verify_check "${label}: wiring matches .env" "fail" \
            "${drift} — heal: docker compose up -d --force-recreate ${svc}"
    fi
}

# #2149 (QA RZFZAI-1961): a consumer whose model name the manager does not
# serve fails every ingestion with nothing naming the cause, and a consumer
# without a manager key fails the same way one layer up (KeyError on the
# empty bearer). Both were "healthy" to every check above. This one asks the
# manager, with the CONSUMER'S OWN key, which models it serves — so a dead or
# missing key fails here too — and compares the names the consumer was given.
#   $1 = service (container name)   $2 = label   $3 = env prefix
#   $4.. = env keys (without prefix) that hold model names
verify_consumer_models_served() {
    local svc="$1" label="$2" prefix="$3"
    shift 3
    _llm_manager_profile_active || return 0
    local key
    key=$(read_env_value "$ENV_FILE" "LLM_MANAGER_${prefix}_KEY" 2>/dev/null || true)
    [ -n "$key" ] || key=$(read_env_value "$ENV_FILE" "${prefix}_LLM_API_KEY" 2>/dev/null || true)
    if [ -z "$key" ]; then
        verify_check "${label}: LLM Manager service key present" "fail" \
            "no LLM_MANAGER_${prefix}_KEY / ${prefix}_LLM_API_KEY in ${ENV_FILE} — ${svc} calls the manager without a key (#2149); run 'rzfz post-install --refresh'"
        return 0
    fi
    local served
    served=$(curl -s --max-time 5 -H "Authorization: Bearer ${key}" \
        "http://127.0.0.1:${LLM_MANAGER_PORT:-8091}/v1/models" 2>/dev/null | \
        python3 -c 'import sys, json
for m in json.load(sys.stdin).get("data", []):
    print(m.get("id", ""))' 2>/dev/null) || served=""
    if [ -z "$served" ]; then
        verify_check "${label}: LLM Manager accepts its service key" "fail" \
            "GET /v1/models with the ${prefix} key returned no models (dead key, or the manager is down) — run 'rzfz post-install --refresh'"
        return 0
    fi
    local k name missing=""
    for k in "$@"; do
        name=$(read_env_value "$ENV_FILE" "${prefix}_${k}" 2>/dev/null || true)
        [ -n "$name" ] || continue
        if ! printf '%s\n' "$served" | grep -qxF -- "$name"; then
            missing="${missing:+$missing; }${prefix}_${k}=${name}"
        fi
    done
    if [ -z "$missing" ]; then
        verify_check "${label}: model names are served by the LLM Manager" "pass"
    else
        verify_check "${label}: model names are served by the LLM Manager" "fail" \
            "${missing} not in the served list ($(printf '%s' "$served" | tr '\n' ' ' | sed 's/ $//')) — set a served model in ${ENV_FILE} and: docker compose up -d --force-recreate ${svc}"
    fi
}

# ==============================================================================
# LLM Manager (#1250a) — the embedded worker is REGISTERED
# ==============================================================================
# "Enrolled" only means a key was minted. What makes the box usable is a
# `workers` row, which appears ONLY after the agent's POST /api/workers is
# ACCEPTED. On the 0.91 clean install the agent ran under a different name than
# its key was minted for, was 401-rejected on every cycle, and nothing said so:
# the console showed zero workers and every deployment 409'd.
verify_llm_worker_enrollment() {
    # Third copy of the same predicate (#1445 review). Same replacement.
    _llm_manager_profile_active || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-worker-agent || return 0
    local env_name ctr_name name wid
    env_name=$(read_env_value "$ENV_FILE" LLM_WORKER_NAME 2>/dev/null || true)
    ctr_name=$(_llm_worker_container_name)
    # rev-B: ask about the name the CONTAINER reports, not the one .env holds.
    # The manager keys the worker row on the REPORTED name, so that is the only
    # name a row can exist under. `master` is compose's documented default for
    # an un-named agent (modules/llm/node-agent/compose.yml) and is the healthy
    # steady state of a box that is not in command_key_mode=enforce — there
    # .env LLM_WORKER_NAME is legitimately empty, and the rev-A `hostname -s`
    # fallback invented a third name that nothing on the box uses, FAILing a
    # perfectly healthy box.
    name="$ctr_name"
    [ -n "$name" ] || name="${env_name:-master}"
    wid=$(_llm_manager_live_worker_id "$name")
    if [ -n "$wid" ]; then
        verify_check "LLM Manager: embedded worker '$name' registered" "pass"
    else
        verify_check "LLM Manager: embedded worker '$name' registered" "fail" \
            "no live worker row — the agent's registration is being rejected (401) or it stopped reporting; container LLM_WORKER_NAME=${ctr_name:-<unset>} vs .env LLM_WORKER_NAME=${env_name:-<empty>} (#1250)"
    fi
    # A SEPARATE finding: the agent is registered, but under a name .env does
    # not agree with. Nothing is broken this second, yet the next mint or key
    # rotation derives the credential from the .env name and the worker starts
    # 401-looping — so it must not hide behind a green registration line.
    if [ -n "$env_name" ] && [ -n "$ctr_name" ] && [ "$env_name" != "$ctr_name" ]; then
        verify_check "LLM Manager: embedded worker name matches .env" "fail" \
            "container LLM_WORKER_NAME='$ctr_name' but .env LLM_WORKER_NAME='$env_name' — the per-worker command key is HMAC(node_key, the REPORTED name), so the next mint or rotation 401-loops (#1250)"
    fi
    return 0
}

# ==============================================================================
# == Wazuh agent auto-enrolment (#1874 parts 2/3) ==============================
# Optionally enrol THIS box's wazuh-agent at post-install so a fresh master or
# worker appears in Wazuh automatically — journald, host FIM, and an allow-listed
# set of container logs (the stack's Docker-security surface: container stdout,
# NOT the docker-listener socket).
#
# SECURITY: ships OFF (WAZUH_AGENT_AUTO_ENROLL=false, config/.env.example).
# Auto-enrolment enlarges the SIEM's enrolment surface (and a fleet manager may
# need a LAN-facing WAZUH_MANAGER_HOST_BIND), so flipping the default ON is an
# explicit operator act gated by the release security assessment
# (pre-tag-check.sh). Idempotent (the installer prunes the block it wrote last
# time) and best-effort (never aborts the run).
provision_wazuh_agent() {
    [ "$(read_env_value "$ENV_FILE" WAZUH_AGENT_AUTO_ENROLL 2>/dev/null || true)" = "true" ] || return 0

    local authd mgr containers installer pwfile
    authd="$(read_env_value "$ENV_FILE" WAZUH_AUTHD_PASSWORD 2>/dev/null || true)"
    if [ -z "$authd" ]; then
        print_warning "Wazuh agent auto-enrol is ON but WAZUH_AUTHD_PASSWORD is empty - skipping (run 'rzfz init' / 'rzfz upgrade')."
        return 0
    fi

    mgr="$(read_env_value "$ENV_FILE" WAZUH_AGENT_MANAGER_ADDRESS 2>/dev/null || true)"
    if [ -z "$mgr" ]; then
        case "${COMPOSE_PROFILES:-}" in
            *wazuh*) mgr="127.0.0.1" ;;
            *) print_warning "Wazuh agent auto-enrol is ON but WAZUH_AGENT_MANAGER_ADDRESS is empty and this box runs no wazuh manager - skipping. Set it to the fleet manager's LAN address."
               return 0 ;;
        esac
    fi

    containers="$(read_env_value "$ENV_FILE" WAZUH_AGENT_CONTAINERS 2>/dev/null || true)"
    [ -n "$containers" ] || containers="caddy,postgres,authentik-server"

    installer="$SCRIPT_DIR/scripts/install-wazuh-agent.sh"
    if [ ! -x "$installer" ]; then
        print_warning "Wazuh agent installer missing at $installer - skipping."
        return 0
    fi

    print_step "Wazuh: enrolling this box's agent at ${mgr} (containers: ${containers})..."
    # Secret via a 0600 temp file + --password-file: never argv
    # (/proc/<pid>/cmdline is world-readable), no pipe (SIGPIPE under pipefail),
    # no sudoers env_keep dependency. apt needs root; `sudo -n` so a
    # non-privileged run degrades to a clear warning instead of blocking.
    pwfile="$(mktemp)"; chmod 600 "$pwfile"; printf '%s' "$authd" > "$pwfile"
    if sudo -n "$installer" "$mgr" --password-file "$pwfile" \
            --containers "$containers" --agent-name "$(hostname)"; then
        print_success "Wazuh agent enrolled at ${mgr}."
    else
        print_warning "Wazuh agent enrol did not complete (needs root for apt, or the manager at ${mgr} was unreachable). Enrol manually - see docs/enterprise/how-to/wazuh.md."
    fi
    rm -f "$pwfile"
    return 0
}

# OpenUEM (#1907) — seed a stack-identity super-admin the operator can log in
# with. OpenUEM's console has NO app-level OIDC and NO admin-sets-password API
# (user creation is a pure invite/self-register flow), and NO per-user RBAC in
# 0.12.0 — any completed `users` row is a full-access operator. So the supported
# non-interactive way to give a stack-password login is a direct seed into
# openuem_db, mirroring the console's own built-in-admin insert
# (Model.CreateDefaultAdminPassword). Profile-gated; CREATE-IF-ABSENT only, so an
# akadmin whose password the operator later changed is never re-passworted.
# Best-effort: never aborts the run. Re-verify the columns/params on an
# OPENUEM_VERSION bump (0.12.0-pinned; #1907 records the source).
provision_openuem_admin() {
    echo "${COMPOSE_PROFILES:-}" | grep -qw "openuem" || return 0

    local pw db pguser admin_user existing hash
    pw="$(read_env_value "$ENV_FILE" AUTHENTIK_BOOTSTRAP_PASSWORD 2>/dev/null || true)"
    if [ -z "$pw" ]; then
        print_warning "OpenUEM admin seed: AUTHENTIK_BOOTSTRAP_PASSWORD is empty - skipping (run 'rzfz init')."
        return 0
    fi
    # The OpenUEM admin mirrors the STACK admin identity, which is configurable.
    admin_user="$(read_env_value "$ENV_FILE" RAZZFAZZ_ADMIN_USERNAME 2>/dev/null || true)"
    [ -n "$admin_user" ] || admin_user="akadmin"   # RAZZFAZZ_ADMIN_USERNAME default (#1148)
    # The username is interpolated into SQL below (psql does NOT expand :'var' in
    # -c/-tAc, only in stdin, so we build the statements in the shell). Reject a
    # username with anything outside a safe id charset so that interpolation can
    # never break the statement or inject.
    case "$admin_user" in
        ''|*[!A-Za-z0-9._-]*)
            print_warning "OpenUEM admin seed: admin username '${admin_user}' has unexpected characters - skipping."
            return 0 ;;
    esac
    db="$(read_env_value "$ENV_FILE" OPENUEM_DB 2>/dev/null || true)"; [ -n "$db" ] || db="openuem_db"
    pguser="$(read_env_value "$ENV_FILE" POSTGRES_USER 2>/dev/null || true)"; [ -n "$pguser" ] || pguser="postgres"

    docker ps --format '{{.Names}}' | grep -qx postgres || { print_warning "OpenUEM admin seed: postgres not running - skipping (re-run 'rzfz post-install --refresh')."; return 0; }
    docker ps --format '{{.Names}}' | grep -qx authentik-server || { print_warning "OpenUEM admin seed: authentik-server not running - skipping."; return 0; }

    # CREATE-IF-ABSENT: never touch an existing admin account's password.
    existing="$(docker exec postgres psql -U "$pguser" -d "$db" -tAc "SELECT 1 FROM users WHERE uid='${admin_user}' LIMIT 1;" 2>/dev/null | tr -d '[:space:]')"
    if [ "$existing" = "1" ]; then
        print_substep "OpenUEM: ${admin_user} already exists - leaving its password untouched (#1907)."
        return 0
    fi

    print_step "OpenUEM: seeding stack-identity super-admin '${admin_user}'..."
    # Argon2id PHC hash with OpenUEM's params (alexedwards/argon2id DefaultParams:
    # id, v=19, m=65536, t=1, p=1, salt=16, key=32) — generated inside
    # authentik-server (it ships argon2-cffi). The password reaches the hasher on
    # STDIN, never argv/logs; only the HASH leaves the container. The hasher
    # round-trips its own output (assert verify) so a hash is emitted ONLY if it
    # authenticates the password — the guard is "can log in", not "row exists".
    hash="$(printf '%s' "$pw" | docker exec -i authentik-server python3 -c '
import sys
from argon2 import PasswordHasher
from argon2.low_level import Type
pw = sys.stdin.read()
ph = PasswordHasher(time_cost=1, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)
h = ph.hash(pw)
assert ph.verify(h, pw)
sys.stdout.write(h)
' 2>/dev/null)"
    case "$hash" in
        '$argon2id$v=19$m=65536,t=1,p=1$'*) : ;;
        *) print_warning "OpenUEM admin seed: could not generate a verifiable argon2id hash - skipping."; return 0 ;;
    esac

    # register='users.completed' => no forced change / 2FA / email confirmation;
    # passwd=true => userpass login. No role/tenant rows (0.12.0 has no per-user
    # RBAC). The HASH (not the password) is the only value handed to psql; the
    # statement is built in the shell (validated username, argon2 hash — no ':'
    # or quote) and fed on stdin.
    local seed_sql
    seed_sql="INSERT INTO users (uid, name, email, email_verified, register, openid, passwd, use2fa, hash, created, modified) VALUES ('${admin_user}', '${admin_user}', 'admin@localhost', true, 'users.completed', false, true, false, '${hash}', now(), now()) ON CONFLICT (uid) DO NOTHING;"
    if printf '%s\n' "$seed_sql" | docker exec -i postgres psql -U "$pguser" -d "$db" -v ON_ERROR_STOP=1 >/dev/null 2>&1
    then
        existing="$(docker exec postgres psql -U "$pguser" -d "$db" -tAc "SELECT 1 FROM users WHERE uid='${admin_user}' AND passwd AND register='users.completed' LIMIT 1;" 2>/dev/null | tr -d '[:space:]')"
        if [ "$existing" = "1" ]; then
            print_success "OpenUEM: ${admin_user} seeded - log in with '${admin_user}' + the stack admin password (full console access)."
        else
            print_warning "OpenUEM admin seed: insert ran but the row is not present/usable - seed manually or use the built-in 'openuem' account."
        fi
    else
        print_warning "OpenUEM admin seed: the DB insert failed - see 'docker logs postgres'. Use the built-in 'openuem' account meanwhile."
    fi
    return 0
}

# OpenUEM (#1992) — hand the module the stack's mail configuration.
#
# OpenUEM keeps NO mail setting in the environment. The console's SMTP form and
# openuem-worker-notification read the SAME single row: the GLOBAL settings row
# of openuem_db (`settings` WHERE tenant_settings IS NULL). Verified against the
# pinned sources (OPENUEM_VERSION=0.12.0, github.com/open-uem/ent@2d3649b3da04):
#   console  internal/models/smtp.go:89      IsSMTPConfigured() -> the red banner
#                                            returns SMTPServer != "" && SMTPPort != 0
#   worker   internal/models/settings.go:29  GetSMTPSettings()  -> what it sends with
# So "OpenUEM inherits the stack mail configuration" can only mean: seed that row.
# There is no API and no env var to do it with — same situation as the admin seed
# in #1907, and the same remedy.
#
# SEED-IF-EMPTY, exactly like #1907: an operator who typed their own SMTP server
# into the console is NEVER overwritten. `OPENUEM_SMTP_HOST=` (empty) opts out
# entirely, mirroring VAULTWARDEN_SMTP_HOST.
#
# #2027 — why `smtp_auth='LOGIN'` and not 'NOAUTH', which is the value the
# worker's code names explicitly. Both reach the SAME branch, because the test
# is an OR (openuem-worker/internal/common/notifications/notifications.go:71):
#
#     if settings.SMTPAuth == "NOAUTH" || (settings.SMTPUser == "" && settings.SMTPPassword == "")
#
# With an empty user AND an empty password the second half is true, so no
# credentials are offered either way. What differs is what the CONSOLE can do
# with the value. At 0.12.0 its auth control is a select of exactly
# LOGIN / PLAIN / XOAUTH2 / SCRAM-SHA-256 — NOAUTH is not in it, and no option
# is marked selected. Measured on 0.91 (DevBox-Vuko, 2026-09-13):
#
#   * an operator who opens Admin -> SMTP and presses Save without touching
#     anything silently rewrites NOAUTH to LOGIN — we would be seeding a value
#     the product's own UI destroys on first visit;
#   * the console's Test button refuses the stored value outright with
#     "Auth type is not valid", so the one button an operator would press to
#     check our seed cannot be pressed.
#
# LOGIN with empty credentials is therefore identical to the worker, survives an
# accidental Save, makes Test usable, and is already the right value if the
# operator later fills in a username and password.
#
# Best-effort: never aborts the run. Re-verify the columns on an OPENUEM_VERSION
# bump — the row is upstream's schema, not ours.
#
# NOT CLAIMED HERE: that a mail is delivered. openuem-worker builds its client as
# `mail.NewClient(server, WithPort(port))` and never touches SMTPTLS/SMTPStarttls,
# so go-mail v0.7.2's DefaultTLSPolicy (TLSMandatory, verified ServerName) applies.
# Whether the stack relay's STARTTLS certificate satisfies that is a BOX question,
# tracked in #1992 — this function makes the console stop warning and points the
# worker at the right host; it does not prove the mail arrives.
provision_openuem_smtp() {
    echo "${COMPOSE_PROFILES:-}" | grep -qw "openuem" || return 0

    local host port mail_from db pguser rows row_id current
    host="$(read_env_value "$ENV_FILE" OPENUEM_SMTP_HOST 2>/dev/null || true)"
    if [ -z "$host" ]; then
        # #2022: ABSENT and EMPTY are not the same state, and read_env_value
        # returns "" for both. Measured on 0.91 (DevBox-Vuko, 2026-09-13): the
        # key was absent because that box had not been through the 2026.09-rc1
        # env migration, and the operator was told "leaving OpenUEM's own mail
        # settings alone" — which names a CHOICE they never made. The two need
        # different remedies: one is "you opted out, nothing to do", the other
        # is "your .env is behind, run the migration".
        if grep -qE '^[[:space:]]*(export[[:space:]]+)?OPENUEM_SMTP_HOST=' "$ENV_FILE" 2>/dev/null; then
            print_substep "OpenUEM: OPENUEM_SMTP_HOST is empty - leaving OpenUEM's own mail settings alone."
        else
            print_warning "OpenUEM: OPENUEM_SMTP_HOST is not in ${ENV_FILE} - this box predates the key, so mail is NOT configured."
            print_info    "  It arrives with the 2026.09 env migration (\`rzfz upgrade\`), or add it by hand:"
            print_info    "    OPENUEM_SMTP_HOST=smtp-relay"
            print_info    "    OPENUEM_SMTP_PORT=587"
        fi
        return 0
    fi
    port="$(read_env_value "$ENV_FILE" OPENUEM_SMTP_PORT 2>/dev/null || true)"; [ -n "$port" ] || port="587"
    # The From address is NOT cosmetic: the relay's ALLOWED_SENDER_DOMAINS is
    # ${MAIN_DOMAIN}, so a sender outside it is refused at the relay. Reuse the
    # stack's own SMTP_FROM, expanded the way load_env expands it (#949) - the
    # raw file value is `razzfazz-ai-box-1@${MAIN_DOMAIN}`.
    mail_from="$(read_env_value "$ENV_FILE" SMTP_FROM 2>/dev/null || true)"
    case "$mail_from" in *'${'*) mail_from="$(_expand_env_refs "$mail_from")" ;; esac
    if [ -z "$mail_from" ]; then
        print_warning "OpenUEM SMTP seed: SMTP_FROM is empty - skipping (a message with no From is refused by the relay)."
        return 0
    fi

    # Everything below is interpolated into SQL (psql expands :'var' only for
    # stdin scripts, not for -c/-tAc, so the statements are built in the shell).
    # Refuse anything outside a safe charset rather than quote-escape it.
    case "$host" in
        *[!A-Za-z0-9.-]*) print_warning "OpenUEM SMTP seed: OPENUEM_SMTP_HOST '${host}' has unexpected characters - skipping."; return 0 ;;
    esac
    case "$port" in
        ''|*[!0-9]*) print_warning "OpenUEM SMTP seed: OPENUEM_SMTP_PORT '${port}' is not a number - skipping."; return 0 ;;
    esac
    case "$mail_from" in
        *[!A-Za-z0-9.@_+-]*) print_warning "OpenUEM SMTP seed: SMTP_FROM '${mail_from}' has unexpected characters - skipping."; return 0 ;;
    esac

    db="$(read_env_value "$ENV_FILE" OPENUEM_DB 2>/dev/null || true)"; [ -n "$db" ] || db="openuem_db"
    pguser="$(read_env_value "$ENV_FILE" POSTGRES_USER 2>/dev/null || true)"; [ -n "$pguser" ] || pguser="postgres"

    docker ps --format '{{.Names}}' | grep -qx postgres || { print_warning "OpenUEM SMTP seed: postgres not running - skipping (re-run 'rzfz post-install --refresh')."; return 0; }
    # The `settings` table is created by the console's ent auto-migration at its
    # first start. Before that there is nothing to seed, and a missing table is a
    # normal state on a box that just enabled the profile - not an error.
    if [ "$(docker exec postgres psql -U "$pguser" -d "$db" -tAc "SELECT to_regclass('public.settings') IS NOT NULL;" 2>/dev/null | tr -d '[:space:]')" != "t" ]; then
        print_warning "OpenUEM SMTP seed: openuem_db has no 'settings' table yet - re-run 'rzfz post-install --refresh' once openuem-console has started."
        return 0
    fi

    # The console reads the global row with First() and GetSMTPSettings() reads it
    # with Only(). More than one global row therefore already breaks the module on
    # its own; do not add a second write to that mess.
    rows="$(docker exec postgres psql -U "$pguser" -d "$db" -tAc "SELECT count(*) FROM settings WHERE tenant_settings IS NULL;" 2>/dev/null | tr -d '[:space:]')"
    case "$rows" in
        0)
            print_step "OpenUEM: seeding the stack mail configuration (${host}:${port}, From ${mail_from})..."
            # created/modified have NO database default - ent fills them in Go
            # (time.Now), so an INSERT that goes around ent has to set them.
            printf '%s\n' "INSERT INTO settings (smtp_server, smtp_port, smtp_user, smtp_password, smtp_auth, smtp_tls, smtp_starttls, message_from, created, modified) VALUES ('${host}', ${port}, '', '', 'LOGIN', false, false, '${mail_from}', now(), now());" \
                | docker exec -i postgres psql -U "$pguser" -d "$db" -v ON_ERROR_STOP=1 >/dev/null 2>&1 || {
                    print_warning "OpenUEM SMTP seed: the insert failed - see 'docker logs postgres'."; return 0; }
            ;;
        1)
            current="$(docker exec postgres psql -U "$pguser" -d "$db" -tAc "SELECT coalesce(smtp_server,'') FROM settings WHERE tenant_settings IS NULL;" 2>/dev/null | tr -d '[:space:]')"
            if [ -n "$current" ]; then
                print_substep "OpenUEM: SMTP server already set to '${current}' - leaving it untouched (#1992)."
                return 0
            fi
            row_id="$(docker exec postgres psql -U "$pguser" -d "$db" -tAc "SELECT id FROM settings WHERE tenant_settings IS NULL;" 2>/dev/null | tr -d '[:space:]')"
            case "$row_id" in ''|*[!0-9]*) print_warning "OpenUEM SMTP seed: could not read the global settings row id - skipping."; return 0 ;; esac
            print_step "OpenUEM: seeding the stack mail configuration (${host}:${port}, From ${mail_from})..."
            printf '%s\n' "UPDATE settings SET smtp_server='${host}', smtp_port=${port}, smtp_user='', smtp_password='', smtp_auth='LOGIN', smtp_tls=false, smtp_starttls=false, message_from='${mail_from}', modified=now() WHERE id=${row_id};" \
                | docker exec -i postgres psql -U "$pguser" -d "$db" -v ON_ERROR_STOP=1 >/dev/null 2>&1 || {
                    print_warning "OpenUEM SMTP seed: the update failed - see 'docker logs postgres'."; return 0; }
            ;;
        *)
            print_warning "OpenUEM SMTP seed: openuem_db holds ${rows} global settings rows - the console itself cannot read that (Only()). Not writing."
            return 0
            ;;
    esac

    # Verify against the predicate the CONSOLE uses for its banner, not against
    # the statement we just ran: server non-empty AND port non-zero, read back
    # from the same row the console reads (internal/models/smtp.go:89).
    if [ "$(docker exec postgres psql -U "$pguser" -d "$db" -tAc "SELECT smtp_server <> '' AND smtp_port <> 0 FROM settings WHERE tenant_settings IS NULL;" 2>/dev/null | tr -d '[:space:]')" = "t" ]; then
        print_success "OpenUEM: mail points at the stack relay ${host}:${port} (From ${mail_from}); the console's 'no SMTP server' banner is gone."
    else
        print_warning "OpenUEM SMTP seed: the write ran but the console would still call it unconfigured - check 'settings' in ${db}."
    fi
    return 0
}

# Wazuh (#855) — post-install verification
# ==============================================================================
# Wazuh has no models to deploy and no seed data to load, so there is nothing
# to PROVISION here. What there is to verify is the thing that actually goes
# wrong: a failed one-shot leaves the three services stuck "starting" forever
# rather than reporting a clear error, because they are gated on
# service_completed_successfully.
verify_wazuh() {
    echo "${COMPOSE_PROFILES:-}" | grep -qw "wazuh" || return 0

    print_substep "Checking Wazuh..."

    local one_shot exit_code state status
    for one_shot in wazuh-certs-generator wazuh-securityconfig-init \
                    wazuh-dashboard-config-init wazuh-securityadmin; do
        # rev-B (review LOW 17): .State.ExitCode is 0 on a container that is
        # still RUNNING, so the old check reported "completed cleanly" for a
        # one-shot that had not finished — the single most likely state during
        # a post-install right after `up`. Status must be `exited` AND the code
        # must be 0.
        status="$(docker inspect -f '{{.State.Status}}' "$one_shot" 2>/dev/null || echo "missing")"
        exit_code="$(docker inspect -f '{{.State.ExitCode}}' "$one_shot" 2>/dev/null || echo "missing")"
        if [ "$status" = "exited" ] && [ "$exit_code" = "0" ]; then
            verify_check "Wazuh: $one_shot completed cleanly" "pass"
        elif [ "$status" = "running" ]; then
            verify_check "Wazuh: $one_shot completed cleanly" "fail" \
                "still running — it has not finished yet (docker logs -f $one_shot)"
        else
            verify_check "Wazuh: $one_shot completed cleanly" "fail" \
                "status=$status exit=$exit_code (docker logs $one_shot)"
        fi
    done

    local svc
    for svc in wazuh-indexer wazuh-manager wazuh-dashboard; do
        state="$(docker inspect -f '{{.State.Health.Status}}' "$svc" 2>/dev/null || echo "missing")"
        if [ "$state" = "healthy" ]; then
            verify_check "Wazuh: $svc healthy" "pass"
        else
            verify_check "Wazuh: $svc healthy" "fail" "health=$state"
        fi
    done

    # No shipped default credential may survive (#855 WZ-1). This is the one
    # check that cannot be done statically: it proves the generated
    # internal_users.yml actually reached the running security index.
    # `--noproxy '*'` for the same reason every healthcheck carries it (#1130).
    # The URL is the indexer's SAN name `wazuh.indexer` (#1259) — it is the only
    # name on the certificate, and this probe verifies it (`--cacert`, no `-k`),
    # so the hyphenated spelling would fail before any auth happens and report
    # an INCONCLUSIVE 000 forever.
    #
    # rev-B (review LOW 17): the old form piped into `grep -q '^401$'` and
    # treated ANY non-401 as a failure — including curl's own error path, where
    # `-w '%{http_code}'` prints 000 or nothing at all. A DNS hiccup or a
    # not-yet-listening indexer therefore reported "the indexer still accepts
    # Wazuh's PUBLISHED demo password", which is a very loud claim to make on
    # no evidence. The three outcomes are now separated.
    local demo_code
    demo_code="$(docker exec wazuh-indexer curl --noproxy '*' -s -o /dev/null \
         -w '%{http_code}' \
         --cacert /usr/share/wazuh-indexer/config/certs/root-ca.pem \
         -u 'admin:SecretPassword' \
         https://wazuh.indexer:9200/_cluster/health 2>/dev/null || echo "000")"
    case "$demo_code" in
        401|403)
            verify_check "Wazuh: upstream demo credential rejected by the indexer" "pass" ;;
        000|"")
            verify_check "Wazuh: upstream demo credential rejected by the indexer" "fail" \
                "INCONCLUSIVE — the probe could not reach the indexer at all (curl error), so nothing was proven either way; re-run once wazuh-indexer is healthy" ;;
        *)
            verify_check "Wazuh: upstream demo credential rejected by the indexer" "fail" \
                "the indexer answered HTTP $demo_code to Wazuh's PUBLISHED demo password — do not expose this box" ;;
    esac

    # #855 rev-B (review MEDIUM 12): the enrolment secret is passed through the
    # ENVIRONMENT, never on argv — on the command line it is readable in
    # /proc/<pid>/cmdline by every local user for the whole apt run and lands in
    # root history. `sudo -E` is what carries the variable through sudo.
    print_info "  Next: monitor another box with"
    print_info "    WAZUH_AUTHD_PASSWORD='<from this box .env>' \\"
    print_info "      sudo -E scripts/install-wazuh-agent.sh <this box WAZUH_MANAGER_HOST_BIND IP> \\"
    print_info "        --containers 'caddy,postgres,authentik-server'"
    print_info "  (or --password-file <path>, or pipe the secret on stdin — never as an argument)"
    print_info "  Confirm enrolment: docker exec wazuh-manager /var/ossec/bin/agent_control -l"
    print_info "  Re-run the installer after every stack upgrade: container IDs change"
    print_info "  and the shipped container-log paths go stale silently."
}

# ==============================================================================
# #1247 — every razzfazz.init one-shot exited 0
# ==============================================================================
# `--verify` is what an operator re-runs when a module misbehaves, so it has to
# answer "did the thing that BUILDS this module actually succeed?". On the 0.91
# clean install it could not: `moltis-image-builder` sat at Exited(127) and
# nothing in init, --verify or `rzfz status` looked. verify_wazuh above already
# did exactly this check for its four NAMED one-shots; this is the same check
# made label-driven for every module, through the shared helper in
# scripts/lib.sh (`razzfazz_init_oneshot_status`) that init and status read too.
#
# A one-shot still running is INFO, not FAIL: a heavy image build legitimately
# outlives the run that started it, and --verify is read-only. So is one that a
# stack stop signalled out from under (137/143) — several agent builders sit on
# an entrypoint that ignores their `command: ["true"]` and only ever die that
# way, and a check that FAILs a stopped box is a check operators mute.
verify_init_oneshots() {
    print_substep "Checking razzfazz.init one-shots (#1247)..."
    local records st name code state log
    records=$(razzfazz_init_oneshot_status) || true
    if [ -z "$records" ]; then
        # #1301 (F6): an empty set is not a result — do not count a PASS for it.
        print_info "  Init one-shots: none labelled razzfazz.init on this box — nothing to verify"
        return 0
    fi
    while IFS='|' read -r st name code state log; do
        [ -n "$st" ] || continue
        case "$st" in
            OK)
                verify_check "Init one-shot: $name exited 0" "pass"
                ;;
            FAIL)
                verify_check "Init one-shot: $name exited 0" "fail" \
                    "exited $code: $log (docker logs $name)"
                ;;
            PENDING)
                print_info "  Init one-shot: $name is still ${state} — not finished, exit code not judged yet"
                ;;
            STOPPED)
                print_info "  Init one-shot: $name was signalled ($code) before it finished — nothing proven; re-check once the stack is up"
                ;;
            MISSING)
                # #1302: labelled in the active profiles, never created.
                verify_check "Init one-shot: $name was created" "fail" "$log"
                ;;
            UNKNOWN)
                # #1301: no measurement — a warning, never a PASS.
                print_warning "  Init one-shots: NOT inspected ($name: $log) — no verdict; fix docker access and re-run --verify"
                ;;
        esac
    done <<< "$records"
    return 0
}

# #1165 — the --verify login uses the same candidate list as owui_ensure_admin
# (OPENWEBUI_ADMIN_PASSWORD, then the fleet bootstrap password). Hard-coding the
# bootstrap password made --verify FAIL on every box whose operator rotated with
# `set-admin-password.sh openwebui` — a red line for a healthy account.
owui_verify_admin_login() {
    local owui_url="http://127.0.0.1:${OPENWEBUI_PORT:-8080}"
    local admin_email="razzfazz-ai-admin@${MAIN_DOMAIN}"
    local _cand tried=0
    while IFS= read -r _cand; do
        [ -n "$_cand" ] || continue
        tried=$((tried + 1))
        if owui_signin "$owui_url" "$admin_email" "$_cand"; then
            verify_check "Open WebUI: admin login" "pass"
            return 0
        fi
    done < <(_owui_admin_passwords)
    if [ "$tried" -eq 0 ]; then
        verify_check "Open WebUI: admin login" "fail" "neither OPENWEBUI_ADMIN_PASSWORD nor AUTHENTIK_BOOTSTRAP_PASSWORD is set in .env"
    else
        verify_check "Open WebUI: admin login" "fail" "login failed with every known password (${tried} tried) — run cli/set-admin-password.sh openwebui"
    fi
    return 0
}

# ==============================================================================
# Worker bundle served by Caddy is CURRENT (#1406)
# ==============================================================================
# The files a joining thin node fetches from this master (/worker-bundle/*,
# /install-worker.sh — #1059) are single-file bind mounts into the caddy
# container. A `git checkout`/`git pull` replaces the source file by rename,
# the container keeps the OLD inode, and the master serves a bundle that no
# longer matches its own checkout (0.91, 2026-09-05: lib.sh link count 0,
# SHA differing from the repo) until caddy is recreated. `rzfz upgrade` ends in
# `--force-recreate`; a manual checkout does not. Nothing said so — this does.
#: served-path=repo-path, exactly the mounts in core/compose.yml.
RAZZFAZZ_WORKER_BUNDLE_MOUNTS="/srv/worker-bundle/worker-join.sh=cli/worker-join.sh /srv/worker-bundle/lib.sh=scripts/lib.sh /srv/worker-bundle/compose.thin.yml=modules/llm/node-agent/compose.thin.yml /srv/worker-bundle/node.env.example=config/node.env.example /srv/install-worker/install-worker.sh=scripts/install-worker.sh.tmpl"

verify_worker_bundle_current() {
    print_substep "Checking the worker bundle Caddy serves is current (#1406)..."
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx caddy; then
        print_info "  Worker bundle: caddy is not running — nothing served, nothing judged"
        return 0
    fi
    local pair served repo want got stale="" missing=""
    for pair in $RAZZFAZZ_WORKER_BUNDLE_MOUNTS; do
        served="${pair%%=*}"; repo="${pair#*=}"
        if [ ! -f "${SCRIPT_DIR}/${repo}" ]; then
            missing="${missing}${repo} "
            continue
        fi
        want=$(sha256sum "${SCRIPT_DIR}/${repo}" 2>/dev/null | cut -c1-64)
        got=$(docker exec caddy sha256sum "$served" 2>/dev/null | cut -c1-64)
        if [ -z "$got" ]; then
            missing="${missing}${served} "
        elif [ "$got" != "$want" ]; then
            stale="${stale}${served##*/} "
        fi
    done
    if [ -n "$stale" ] || [ -n "$missing" ]; then
        verify_check "Worker bundle: files Caddy serves match this checkout" "fail" \
            "stale: ${stale:-none}; unreadable: ${missing:-none} — the bind-mounted source was replaced (checkout/pull); recreate the edge: docker compose up -d --force-recreate caddy"
        return 0
    fi
    verify_check "Worker bundle: files Caddy serves match this checkout" "pass"
    return 0
}

# #1370 — report blueprint instances that are not `successful`.
#
# Split out of the verify suite so the same reader can be used from anywhere
# (and so it is testable without running the whole suite). Never fatal on its
# own terms: a box where the query cannot run reports that, rather than a green
# tick or a red one it cannot justify.
_bp_broken_blueprints() {
    docker exec postgres psql -U "${POSTGRES_USER:-docker}" \
        -d "${AUTHENTIK_DB:-authentik_db}" -tA -F'|' -c \
        "SELECT name, path, status, EXTRACT(EPOCH FROM (now() - last_applied))::bigint
           FROM authentik_blueprints_blueprintinstance
          WHERE status <> 'successful' AND enabled
          ORDER BY last_applied;" 2>/dev/null || true
}

_bp_check_authentik_blueprints() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres || return 0

    local rows
    rows=$(_bp_broken_blueprints)
    if [ -z "$(printf '%s' "$rows" | tr -d '[:space:]')" ]; then
        verify_check "Authentik: blueprints applied" "pass"
        return 0
    fi

    # A row that errored SECONDS ago is a window we ran into, not a verdict:
    # authentik re-applies periodically and it may well be green by the time the
    # operator reads this. One that has sat in error for longer is stuck.
    # verify_check knows only pass and fail — anything else prints FAIL — so a
    # fresh row is WAITED OUT rather than reported in a third colour that does
    # not exist.
    local fresh_window="${RZFZ_BLUEPRINT_FRESH_SECONDS:-300}"
    local settle="${RZFZ_BLUEPRINT_SETTLE_SECONDS:-60}"
    local stuck="" fresh="" name path status age
    while IFS='|' read -r name path status age; do
        [ -n "$name" ] || continue
        case "${age:-0}" in ''|*[!0-9-]*) age=0 ;; esac
        if [ "$age" -gt "$fresh_window" ]; then
            stuck="${stuck:+${stuck}, }${name} (${path})"
        else
            fresh="${fresh:+${fresh}, }${name} (${path})"
        fi
    done <<< "$rows"

    if [ -z "$stuck" ] && [ -n "$fresh" ]; then
        print_substep "  Blueprints applying right now (${fresh}) — waiting up to ${settle}s before judging."
        local waited=0
        while [ "$waited" -lt "$settle" ]; do
            sleep 10
            waited=$((waited + 10))
            rows=$(_bp_broken_blueprints)
            if [ -z "$(printf '%s' "$rows" | tr -d '[:space:]')" ]; then
                verify_check "Authentik: blueprints applied" "pass"
                return 0
            fi
        done
        stuck="$fresh"
    fi

    verify_check "Authentik: blueprints applied" "fail" \
        "not applied: ${stuck} — the module's application and its policy bindings are missing, so its tile is gone and forward-auth 404s. The reason is only in the worker log at apply time: docker logs authentik-worker | grep -v 'deadlock detected' (#1350/#1370)"
}

run_verification_suite() {
    print_step "Running verification suite..."
    VERIFY_PASS=0
    VERIFY_FAIL=0
    VERIFY_RESULTS=""
    
    load_env
    local api_key="${GPUSTACK_API_KEY}"
    
    # Disable errexit for verification (checks may fail intentionally)
    set +e
    
    # #1250b: the three GPUStack checks below only mean anything on a box that
    # actually RUNS GPUStack. On an LLM-Manager box they probed a dead endpoint
    # with a GPUStack key that box never had, under model names it never used
    # (`nomic-embed-text`), and reported three red FAILs for a healthy stack.
    # Backend-gated: GPUStack box -> GPUStack checks, Manager box -> Manager
    # checks against the canonical /v1 endpoint, a box running both -> both.
    if _gpustack_profile_active; then
        # 1. GPUStack — models listed
        print_substep "Checking GPUStack..."
        local gpustack_models
        gpustack_models=$(curl -sf --max-time 10 -H "Authorization: Bearer $api_key" \
            "http://127.0.0.1:${GPUSTACK_PORT:-9090}/v1-openai/models" 2>/dev/null | \
            python3 -c "import sys,json; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null)
        if [ -n "$gpustack_models" ] && [ "$gpustack_models" -gt 0 ]; then
            verify_check "GPUStack: $gpustack_models models available" "pass"
        else
            verify_check "GPUStack: models available" "fail" "no models or unreachable"
        fi
    
        # 2. GPUStack — chat completion against the always-on default chat model
        # (NOT a hardcoded alias — gemma4/coder-next are auto_start:false at 0
        # replicas and would 503). qwen3.6 ships thinking-ON by default (ga.2 /
        # #150), so give a generous output budget (2048) so reasoning AND the final
        # answer both fit — that's the whole point of the day-1 fix. We still fall
        # back to reasoning_content for proof-of-life, but with this budget a healthy
        # box lands a real answer in `content`.
        local chat_model
        chat_model=$(_default_chat_alias)
        local chat_resp
        chat_resp=$(curl -sf --max-time 90 -H "Authorization: Bearer $api_key" \
            -H "Content-Type: application/json" \
            "http://127.0.0.1:${GPUSTACK_PORT:-9090}/v1-openai/chat/completions" \
            -d "{\"model\":\"${chat_model}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}],\"max_tokens\":2048}" 2>/dev/null | \
            python3 -c "
import sys, json
try:
    msg = json.load(sys.stdin)['choices'][0]['message']
    content = msg.get('content', '') or msg.get('reasoning_content', '')
    print(content[:20] if content else 'empty')
except: pass
" 2>/dev/null)
        if [ -n "$chat_resp" ]; then
            verify_check "GPUStack: chat completion ($chat_model)" "pass"
        else
            verify_check "GPUStack: chat completion ($chat_model)" "fail" "no response"
        fi
    
        # 3. GPUStack — embedding
        local embed_resp
        embed_resp=$(curl -sf --max-time 15 -H "Authorization: Bearer $api_key" \
            -H "Content-Type: application/json" \
            "http://127.0.0.1:${GPUSTACK_PORT:-9090}/v1-openai/embeddings" \
            -d '{"model":"nomic-embed-text","input":"test"}' 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data'][0]['embedding']))" 2>/dev/null)
        if [ -n "$embed_resp" ] && [ "$embed_resp" -gt 0 ]; then
            verify_check "GPUStack: embedding (nomic-embed-text, dim=$embed_resp)" "pass"
        else
            verify_check "GPUStack: embedding (nomic-embed-text)" "fail" "no response"
        fi
    fi

    # 1b. LLM Manager — the same four questions asked of the backend this box
    # really uses: the metered /v1 surface, with the stack/openwebui service
    # key, under the manifest's own default aliases (#1250b). #1441: the gate
    # asks the SAME question the deploy arms ask — llm_manager_owns_standard_set
    # — so a box where a GPUStack profile holds the GPU gets ONE line naming
    # the cause (and #1442) instead of four probe failures that hide it.
    if llm_manager_owns_standard_set; then
        llm_manager_verify_models
        llm_manager_verify_manifest_drift   # #1263
    elif _llm_manager_profile_active; then
        llm_manager_verify_standard_set_ownership
    fi

    # 4. Open WebUI — login
    print_substep "Checking Open WebUI..."
    
    # Wait briefly for Open WebUI to start if it was just restarted
    local owui_waited=0
    while [ $owui_waited -lt 30 ]; do
        if curl -sf --max-time 2 "http://127.0.0.1:${OPENWEBUI_PORT:-8080}/health" > /dev/null 2>&1; then
            break
        fi
        sleep 2
        owui_waited=$((owui_waited + 2))
    done

    owui_verify_admin_login
    

    
    # 4c. Komodo — health (login omitted due to cookie auth complexity)
    if echo "${COMPOSE_PROFILES:-}" | grep -q "monitor"; then
        print_substep "Checking Komodo health..."
        if curl -sf --max-time 10 "http://127.0.0.1:${KOMODO_PORT:-8180}/" > /dev/null 2>&1; then
            verify_check "Komodo: healthy" "pass"
        else
            verify_check "Komodo: healthy" "fail" "unreachable"
        fi
    fi
    
    # 5. Open WebUI — GPUStack connection: enabled AND a RESOLVED key (not the ${...}
    # literal). OWUI 0.6.x exposes this at /openai/config (ENABLE_OPENAI_API +
    # OPENAI_API_KEYS); the old /api/v1/configs/export::openai.enable probe was a
    # false-negative on 0.6.x (#200). chr(36)='$' detects an unresolved ${...} key.
    if [ -n "$owui_token" ]; then
        local owui_cfg
        owui_cfg=$(curl -sf --max-time 10 -H "Authorization: Bearer $owui_token" \
            "http://127.0.0.1:${OPENWEBUI_PORT:-8080}/openai/config" 2>/dev/null)
        if echo "$owui_cfg" | python3 -c "import sys,json; d=json.load(sys.stdin); ks=d.get('OPENAI_API_KEYS',[]); sys.exit(0 if (d.get('ENABLE_OPENAI_API') and ks and ks[0] and not ks[0].startswith(chr(36))) else 1)" 2>/dev/null; then
            verify_check "Open WebUI: $(_llm_backend_display_name) connection configured" "pass"
        else
            verify_check "Open WebUI: $(_llm_backend_display_name) connection configured" "fail" "not enabled or key unresolved"
        fi
    fi

    # 5b. Open WebUI — the `model` rows (#1266) and the .env/DB agreement
    # (#1252). Check 5 above only asks OWUI whether SOME key is set; neither an
    # empty `model` table nor a .env that disagrees with the persisted config
    # is visible from there.
    verify_owui_model_rows_and_env
    verify_owui_pipelines_registered

    # 6. Dify — setup complete & login
    print_substep "Checking Dify..."
    local dify_setup
    dify_setup=$(docker exec dify-api curl -sf --max-time 10 http://localhost:5001/console/api/setup 2>/dev/null | \
        python3 -c "import sys,json; print(json.load(sys.stdin).get('step',''))" 2>/dev/null)
    if [ "$dify_setup" = "finished" ]; then
        verify_check "Dify: setup complete" "pass"

        # 6b. Dify — model-provider icons (#160). Each provider list / model-config
        # icon is an <img> to the plugin-daemon-backed icon endpoint; a runtime
        # failure of that endpoint renders a broken image. Probe every installed
        # provider's icon endpoint and surface the exact failing provider+status
        # (500 => plugin-daemon/plugin asset; 404 => host/route; 302/HTML => SSO).
        local broken_icons
        broken_icons=$(dify_broken_provider_icons)
        if [ -z "$broken_icons" ]; then
            verify_check "Dify: model-provider icons render" "pass"
        else
            verify_check "Dify: model-provider icons render" "fail" \
                "broken $(echo "$broken_icons" | awk '{printf "%s(HTTP %s) ", $1, $2}') — re-run 'rzfz post-install' (reinstalls provider plugins); #160"
        fi

        # 6c. #1248: the workspace defaults must EXIST and name the backend this
        # box actually runs. Icons rendering says nothing about whether Dify has
        # a model it can call.
        dify_verify_default_models
    else
        verify_check "Dify: setup complete" "fail" "step=$dify_setup"
    fi



    # 7. SearXNG — search endpoint reachability + engine health.
    # #201: pass on reachable + valid JSON. A 0-result probe is a transient
    # external-engine/rate-limit condition (the upstream engines, not the stack),
    # so it must NOT fail the verify — only unreachable/invalid-JSON does.
    print_substep "Checking SearXNG..."
    local searx_n
    searx_n=$(curl -sf --max-time 15 \
        "http://127.0.0.1:${SEARXNG_PORT:-8088}/search?q=test&format=json" 2>/dev/null | \
        python3 -c "import sys,json; print(len(json.load(sys.stdin).get('results',[])))" 2>/dev/null)
    if [ -z "$searx_n" ]; then
        # SearXNG may be on a different host port — probe the container directly.
        searx_n=$(docker exec searxng curl -sf --max-time 10 \
            "http://localhost:8080/search?q=test&format=json" 2>/dev/null | \
            python3 -c "import sys,json; print(len(json.load(sys.stdin).get('results',[])))" 2>/dev/null)
    fi
    if [ -z "$searx_n" ]; then
        verify_check "SearXNG: search endpoint reachable" "fail" "unreachable or invalid JSON"
    elif [ "$searx_n" -gt 0 ]; then
        verify_check "SearXNG: search returns results ($searx_n)" "pass"
    else
        # Reachable + valid JSON but zero results — external engines transiently
        # empty / rate-limited. The search backend is up; not a stack break.
        verify_check "SearXNG: search endpoint reachable (0 results — external engines transiently empty/rate-limited)" "pass"
    fi
    
    # 8. Speaches — models installed
    print_substep "Checking Speaches..."
    local speaches_models
    speaches_models=$(curl -sf --max-time 10 \
        "http://127.0.0.1:${SPEACHES_PORT:-5003}/v1/models" 2>/dev/null | \
        python3 -c "import sys,json; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null)
    if [ -n "$speaches_models" ] && [ "$speaches_models" -ge 2 ]; then
        verify_check "Speaches: $speaches_models models installed" "pass"
    else
        verify_check "Speaches: models installed" "fail" "found ${speaches_models:-0}"
    fi
    
    # 9. LightRAG — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -q "lightrag"; then
        print_substep "Checking LightRAG..."
        local lightrag_health
        lightrag_health=$(curl -sf --max-time 10 \
            "http://127.0.0.1:${LIGHTRAG_PORT:-9621}/health" 2>/dev/null | \
            python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
        if [ "$lightrag_health" = "ready" ] || [ "$lightrag_health" = "healthy" ]; then
            verify_check "LightRAG: healthy" "pass"
        else
            verify_check "LightRAG: healthy" "fail" "status=$lightrag_health"
        fi
        # #1486: healthy ≠ wired — compare the container with .env.
        verify_rag_consumer_wiring lightrag LightRAG LIGHTRAG \
            LLM_ENDPOINT:LLM_BINDING_HOST LLM_MODEL:LLM_MODEL \
            EMBEDDING_ENDPOINT:EMBEDDING_BINDING_HOST EMBEDDING_MODEL:EMBEDDING_MODEL \
            EMBEDDING_DIM:EMBEDDING_DIM
        # #2149: wired ≠ served — the key must be accepted and the names must exist.
        verify_consumer_models_served lightrag LightRAG LIGHTRAG LLM_MODEL EMBEDDING_MODEL
    fi
    
    # 10. Cognee — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -q "cognee"; then
        print_substep "Checking Cognee..."
        local cognee_health
        cognee_health=$(curl -sf --max-time 10 \
            "http://127.0.0.1:${COGNEE_PORT:-8000}/health" 2>/dev/null | \
            python3 -c "import sys,json; print(json.load(sys.stdin).get('health',''))" 2>/dev/null)
        if [ "$cognee_health" = "healthy" ]; then
            verify_check "Cognee: healthy" "pass"
        else
            verify_check "Cognee: healthy" "fail" "health=$cognee_health"
        fi
        # #1486: healthy ≠ wired — compare the container with .env.
        verify_rag_consumer_wiring cognee Cognee COGNEE \
            LLM_ENDPOINT:LLM_ENDPOINT LLM_MODEL:LLM_MODEL \
            EMBEDDING_ENDPOINT:EMBEDDING_ENDPOINT EMBEDDING_MODEL:EMBEDDING_MODEL \
            EMBEDDING_DIM:EMBEDDING_DIMENSIONS
        # #1249: a healthy cognee with an EMPTY COGNEE_MCP_API_KEY still leaves
        # every MCP agent without a memory backend — cognee-mcp reads that key
        # as API_TOKEN. The mint step's "mcp registry: valid" line validates
        # core/mcp/mcp-servers.yaml only, so a failed mint used to pass --verify
        # in silence.
        if [ -n "$(read_env_value "$ENV_FILE" COGNEE_MCP_API_KEY 2>/dev/null || true)" ]; then
            verify_check "Cognee: cognee-mcp API key minted" "pass"
        else
            verify_check "Cognee: cognee-mcp API key minted" "fail" \
                "COGNEE_MCP_API_KEY empty — cognee-mcp cannot authenticate to cognee; re-run 'rzfz post-install --refresh'"
        fi
    fi

    # 10b. Apache Tika — health + smoke test (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "tika"; then
        print_substep "Checking Apache Tika..."
        local tika_version
        tika_version=$(curl -sf --max-time 10 \
            "http://127.0.0.1:${TIKA_PORT:-9998}/tika" 2>/dev/null | head -1)
        if [ -n "$tika_version" ]; then
            verify_check "Tika: healthy (${tika_version:0:40})" "pass"
        else
            verify_check "Tika: healthy" "fail" "unreachable"
        fi
        # Functional smoke test: PUT plain text → expect text/plain extraction
        local tika_smoke
        tika_smoke=$(curl -sf --max-time 10 \
            -X PUT "http://127.0.0.1:${TIKA_PORT:-9998}/tika" \
            -H "Content-Type: text/plain" \
            -H "Accept: text/plain" \
            --data "Hello Tika smoke test" 2>/dev/null)
        if [ -n "$tika_smoke" ]; then
            verify_check "Tika: text extraction smoke test" "pass"
        else
            verify_check "Tika: text extraction smoke test" "fail" "no response"
        fi
    fi

    # 10c. Docling — health + VLM config check (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "docling"; then
        print_substep "Checking Docling..."
        local docling_health
        docling_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${DOCLING_PORT:-5001}/health" 2>/dev/null | \
            python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
        if [ "$docling_health" = "ok" ] || [ -n "$docling_health" ]; then
            verify_check "Docling: healthy" "pass"
        else
            verify_check "Docling: healthy" "fail" "status=${docling_health:-unreachable}"
        fi
        # Confirm the container is up: /version returns 200. docling-serve's /version
        # is COMPONENT-KEYED, e.g. {"docling-serve":"1.27.0","docling":"2.113.0",...} —
        # there is no flat "version" key, so read the docling-serve component version.
        local docling_version
        docling_version=$(curl -sf --max-time 10 \
            "http://127.0.0.1:${DOCLING_PORT:-5001}/version" 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('docling-serve') or d.get('version') or '')" 2>/dev/null)
        if [ -n "$docling_version" ]; then
            verify_check "Docling: version endpoint (${docling_version})" "pass"
        else
            verify_check "Docling: version endpoint" "fail" "no response"
        fi
        # Confirm VLM configured: docling 2026.08 uses DOCLING_SERVE_DEFAULT_VLM_PRESET
        # (superseded the pre-2026.08 ALLOWED_VLM_ENGINES env). Non-empty preset = ok.
        local docling_vlm
        docling_vlm=$(docker inspect docling 2>/dev/null | \
            python3 -c "
import sys,json
d=json.load(sys.stdin)
env = d[0].get('Config',{}).get('Env',[])
for e in env:
    if 'DOCLING_SERVE_DEFAULT_VLM_PRESET' in e:
        print(e.split('=',1)[-1])
        break
" 2>/dev/null)
        if [ -n "$docling_vlm" ]; then
            verify_check "Docling: VLM preset (${docling_vlm})" "pass"
        else
            verify_check "Docling: VLM preset" "fail" "DOCLING_SERVE_DEFAULT_VLM_PRESET not set"
        fi
    fi

    # 10d. Presidio — health + analyzer smoke test (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "presidio"; then
        print_substep "Checking Presidio..."
        # Analyzer health
        local analyzer_health
        analyzer_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${PRESIDIO_ANALYZER_PORT:-5100}/health" 2>/dev/null)
        if [ -n "$analyzer_health" ]; then
            verify_check "Presidio Analyzer: healthy" "pass"
        else
            verify_check "Presidio Analyzer: healthy" "fail" "unreachable"
        fi
        # Anonymizer health
        local anonymizer_health
        anonymizer_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${PRESIDIO_ANONYMIZER_PORT:-5300}/health" 2>/dev/null)
        if [ -n "$anonymizer_health" ]; then
            verify_check "Presidio Anonymizer: healthy" "pass"
        else
            verify_check "Presidio Anonymizer: healthy" "fail" "unreachable"
        fi
        # Image Redactor health
        local redactor_health
        redactor_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${PRESIDIO_IMAGE_REDACTOR_PORT:-5200}/health" 2>/dev/null)
        if [ -n "$redactor_health" ]; then
            verify_check "Presidio Image Redactor: healthy" "pass"
        else
            verify_check "Presidio Image Redactor: healthy" "fail" "unreachable"
        fi
        # Analyzer smoke test: detect PERSON entity in sample text
        local entity_count
        entity_count=$(curl -sf --max-time 20 \
            -X POST "http://127.0.0.1:${PRESIDIO_ANALYZER_PORT:-5100}/analyze" \
            -H "Content-Type: application/json" \
            -d '{"text":"My name is John Smith, call me at 212-555-0100","language":"en"}' \
            2>/dev/null | \
            python3 -c "import sys,json; r=json.load(sys.stdin); print(len(r))" 2>/dev/null)
        if [ -n "$entity_count" ] && [ "$entity_count" -gt 0 ]; then
            verify_check "Presidio Analyzer: entity detection ($entity_count entities)" "pass"
        else
            verify_check "Presidio Analyzer: entity detection" "fail" "count=${entity_count:-0}"
        fi
    fi

    # 10e. Stirling-PDF — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "stirling-pdf"; then
        print_substep "Checking Stirling-PDF..."
        local stirling_health
        stirling_health=$(curl -sf --max-time 10 \
            "http://127.0.0.1:${STIRLING_PDF_PORT:-8181}/api/v1/info/status" 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null)
        if [ "$stirling_health" = "UP" ] || [ -n "$stirling_health" ]; then
            verify_check "Stirling-PDF: healthy (status=${stirling_health})" "pass"
        else
            verify_check "Stirling-PDF: healthy" "fail" "status=${stirling_health:-unreachable}"
        fi
    fi

    # 10f. Gotenberg — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "gotenberg"; then
        print_substep "Checking Gotenberg..."
        local gotenberg_health
        gotenberg_health=$(curl -sf --max-time 10 \
            "http://127.0.0.1:${GOTENBERG_PORT:-3005}/health" 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null)
        if [ "$gotenberg_health" = "up" ] || [ -n "$gotenberg_health" ]; then
            verify_check "Gotenberg: healthy (status=${gotenberg_health})" "pass"
        else
            verify_check "Gotenberg: healthy" "fail" "unreachable — run: docker compose logs gotenberg"
        fi
    fi

    # 10g. Paperclip — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "paperclip"; then
        print_substep "Checking Paperclip..."
        local paperclip_health
        paperclip_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${PAPERCLIP_PORT:-3100}/api/health" 2>/dev/null)
        if [ -n "$paperclip_health" ]; then
            verify_check "Paperclip: healthy" "pass"
        else
            verify_check "Paperclip: healthy" "fail" "unreachable — run: docker compose logs paperclip"
        fi
    fi

    # 10h. Moltis — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "moltis"; then
        print_substep "Checking Moltis..."
        local moltis_health
        moltis_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${MOLTIS_PORT:-13131}/health" 2>/dev/null)
        if [ -n "$moltis_health" ]; then
            verify_check "Moltis: healthy" "pass"
        else
            verify_check "Moltis: healthy" "fail" "unreachable — run: docker compose logs moltis"
        fi
    fi

    # 10i. Hermes Agent status page — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "hermes"; then
        print_substep "Checking Hermes Agent..."
        local hermes_health
        hermes_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${HERMES_PORT:-3101}/health" 2>/dev/null)
        if [ -n "$hermes_health" ]; then
            verify_check "Hermes Agent: status page healthy" "pass"
        else
            verify_check "Hermes Agent: status page healthy" "fail" "unreachable — run: docker compose logs hermes-agent"
        fi
    fi

    # 10j. Synapse + Element Web — health (only if matrix profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "matrix"; then
        print_substep "Checking Synapse..."
        local synapse_health
        synapse_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:8008/_matrix/client/versions" 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('versions') else 'empty')" 2>/dev/null)
        if [ "$synapse_health" = "ok" ]; then
            verify_check "Synapse: Matrix client API healthy" "pass"
        else
            verify_check "Synapse: Matrix client API healthy" "fail" "unreachable — run: docker compose logs synapse"
        fi
        print_substep "Checking Element Web..."
        local element_health
        element_health=$(curl -sf --max-time 10 \
            "http://127.0.0.1:8009/" 2>/dev/null | grep -c "Element" || true)
        if [ "${element_health:-0}" -gt 0 ]; then
            verify_check "Element Web: healthy" "pass"
        else
            verify_check "Element Web: healthy" "fail" "unreachable — run: docker compose logs element-web"
        fi
    fi

    # 10k. Paperless-ngx — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "paperless-ngx"; then
        print_substep "Checking Paperless-ngx..."
        local paperless_health
        paperless_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${PAPERLESS_PORT:-8001}/accounts/login/" 2>/dev/null | grep -c "Paperless" || true)
        if [ "${paperless_health:-0}" -gt 0 ]; then
            verify_check "Paperless-ngx: healthy" "pass"
        else
            verify_check "Paperless-ngx: healthy" "fail" "unreachable — run: docker compose logs paperless-ngx"
        fi
    fi

    # 10l. Vaultwarden — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "vaultwarden"; then
        print_substep "Checking Vaultwarden..."
        local vaultwarden_health
        vaultwarden_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${VAULTWARDEN_PORT:-8222}/alive" 2>/dev/null)
        if [ -n "$vaultwarden_health" ]; then
            verify_check "Vaultwarden: healthy" "pass"
        else
            verify_check "Vaultwarden: healthy" "fail" "unreachable — run: docker compose logs vaultwarden"
        fi
    fi

    # 10m. Infisical — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "infisical"; then
        print_substep "Checking Infisical..."
        local infisical_health
        infisical_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${INFISICAL_PORT:-8888}/api/status" 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('date') else 'empty')" 2>/dev/null)
        if [ "$infisical_health" = "ok" ]; then
            verify_check "Infisical: healthy" "pass"
        else
            verify_check "Infisical: healthy" "fail" "unreachable — run: docker compose logs infisical"
        fi
    fi

    # 10n. Onyx — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "onyx"; then
        print_substep "Checking Onyx..."
        local onyx_health
        onyx_health=$(docker exec onyx-api python -c \
            "import urllib.request; urllib.request.urlopen('http://localhost:8080/health'); print('ok')" 2>/dev/null)
        if [ "$onyx_health" = "ok" ]; then
            verify_check "Onyx API: healthy" "pass"
        else
            verify_check "Onyx API: healthy" "fail" "unreachable — run: docker compose logs onyx-api"
        fi
    fi

    # 10o. OpenHands — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "openhands"; then
        print_substep "Checking OpenHands..."
        local openhands_health
        openhands_health=$(curl -sf --max-time 15 \
            "http://127.0.0.1:${OPENHANDS_PORT:-3006}/api/options" 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('version') else 'empty')" 2>/dev/null)
        if [ "$openhands_health" = "ok" ]; then
            verify_check "OpenHands: healthy" "pass"
        else
            verify_check "OpenHands: healthy" "fail" "unreachable — run: docker compose logs openhands"
        fi
    fi

    # 10p. Coding Tools — health (only if profile enabled)
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "coding-tools"; then
        print_substep "Checking Coding Tools..."
        local coding_tools_health
        coding_tools_health=$(docker exec coding-tools gsd --version 2>/dev/null)
        if [ -n "$coding_tools_health" ]; then
            verify_check "Coding Tools: gsd ${coding_tools_health} present" "pass"
        else
            verify_check "Coding Tools: gsd present" "fail" "gsd binary missing — run: docker compose logs coding-tools"
        fi
    fi

    # 11. Authentik — health
    # #2213: the authentik-server image ships neither curl nor wget, so the
    # old `docker exec authentik-server curl …` failed with exec rc 127 on
    # EVERY box and `2>/dev/null` turned "no probe binary" into "unreachable"
    # — a false FAIL about a security-critical service, on every run of this
    # suite (0.79, journey C, twice). Probe with the interpreter the image has,
    # and keep the exec's own failure apart from the service's answer.
    print_substep "Checking Authentik..."
    local authentik_probe_rc=0 authentik_probe_out=""
    authentik_probe_out=$(docker exec authentik-server python3 -c '
import sys, urllib.request
try:
    with urllib.request.urlopen("http://localhost:9000/-/health/live/", timeout=10) as r:
        print("HTTP", r.status)
        sys.exit(0 if 200 <= r.status < 300 else 2)
except Exception as e:
    print("ERR", e)
    sys.exit(2)
' 2>&1) || authentik_probe_rc=$?
    case "$authentik_probe_rc" in
        0)   verify_check "Authentik: healthy" "pass" ;;
        2)   verify_check "Authentik: healthy" "fail" "health endpoint answered: ${authentik_probe_out:-no response}" ;;
        126|127)
             verify_check "Authentik: healthy" "fail" "probe could not run inside authentik-server (exec rc ${authentik_probe_rc}: ${authentik_probe_out:-no interpreter}) — this is the probe, not the service (#2213)" ;;
        *)   verify_check "Authentik: healthy" "fail" "probe failed (rc ${authentik_probe_rc}): ${authentik_probe_out:-container not running?}" ;;
    esac

    # 11b. Authentik — blueprint STATE, not reachability (#1370).
    #
    # A box with twelve broken blueprints passes the liveness check above. The
    # failure has no other reporter: BlueprintInstance carries no error column,
    # `git grep BlueprintInstance` over cli/ scripts/ core/ finds one comment
    # and no reader, and the reason exists only in the worker log at apply time
    # — where #1350's deadlock lines bury it. A blueprint in `error` means the
    # module's Application and its policy bindings are missing, so the tile is
    # gone and forward-auth 404s: a wiring defect, not a cosmetic one.
    #
    # `last_applied` decides how loud to be. Authentik re-applies periodically,
    # so a row that errored SECONDS ago is very likely a window we ran into and
    # will heal itself; one that has been in error for an hour is stuck.
    _bp_check_authentik_blueprints

    # 12. Supporting Containers
    print_substep "Checking Supporting Containers..."
    
    if curl -sfk --max-time 5 "https://help.${MAIN_DOMAIN}/healthz" > /dev/null 2>&1; then
        verify_check "Supporting: Help Center" "pass"
    else
        verify_check "Supporting: Help Center" "fail" "unreachable"
    fi
    
    if curl -sfk --max-time 5 "https://license.${MAIN_DOMAIN}/healthz" > /dev/null 2>&1; then
        verify_check "Supporting: Licenses" "pass"
    else
        verify_check "Supporting: Licenses" "fail" "unreachable"
    fi
    
    # #22: setup.<domain> removed — its functions live in the Config Portal.
    if curl -sfk --max-time 5 "https://backup.${MAIN_DOMAIN}/healthz" > /dev/null 2>&1; then
        verify_check "Supporting: Backup UI" "pass"
    else
        verify_check "Supporting: Backup UI" "fail" "unreachable"
    fi

    if echo "${COMPOSE_PROFILES:-}" | grep -q "gitea"; then
        if curl -sf --max-time 5 "http://127.0.0.1:${GITEA_HTTP_PORT:-3000}/api/healthz" > /dev/null 2>&1; then
            verify_check "Supporting: Gitea" "pass"
        else
            verify_check "Supporting: Gitea" "fail" "unreachable"
        fi
    fi

    # 13. Wazuh (#855) — one-shot exit codes, service health, and proof that
    #     the upstream demo credential is rejected. No-ops when the opt-in
    #     profile is disabled.
    verify_wazuh

    # 14. #1250a: the LLM Manager's embedded worker is REGISTERED, not merely
    #     "enrolled". Read-only and independent of whether this run did the
    #     enrolment, so a `--verify`-only run catches a box that silently
    #     401-loops (the 0.91 clean-install defect). Without a worker row the
    #     manager can place no model at all, so this is a FAIL, not a warning.
    verify_llm_worker_enrollment

    # 15. #1247: every razzfazz.init one-shot (the agent image builders, the
    #     *-init bootstraps) exited 0. A non-zero exit means the artifact a
    #     module needs was never built — the 0.91 clean install shipped a
    #     moltis-image-builder at Exited(127) and every gate said "green".
    verify_init_oneshots

    # 9c. Worker bundle (#1406) — a stale bind-mounted inode is invisible until a
    # thin node joins with a lib.sh that does not match its worker-join.sh.
    verify_worker_bundle_current

    # ── Report ────────────────────────────────────────────────────────
    echo ""
    echo -e "${BLUE}======================================================================${NC}"
    echo -e "${BLUE}     Verification Report${NC}"
    echo -e "${BLUE}======================================================================${NC}"
    echo -e "$VERIFY_RESULTS"
    echo -e "${BLUE}----------------------------------------------------------------------${NC}"
    local total=$((VERIFY_PASS + VERIFY_FAIL))
    if [ $VERIFY_FAIL -eq 0 ]; then
        echo -e "  ${GREEN}All $total checks passed.${NC}"
    else
        echo -e "  ${GREEN}$VERIFY_PASS passed${NC}, ${RED}$VERIFY_FAIL failed${NC} (of $total)"
    fi
    echo -e "${BLUE}======================================================================${NC}"
    echo ""
    
    set -e
    return $VERIFY_FAIL
}

# ==============================================================================
# CLI & Main
# ==============================================================================
print_help() {
    echo "rzfz.ai Post-Install Provisioning Script"
    echo ""
    echo "Usage:"
    echo "  $0 --preset standard|developer    Provision models and services"
    echo "  $0 --refresh                      Idempotent re-config (DNS + API key)"
    echo "  $0 --verify                       Run verification checks only"
    echo "  $0 --preset standard --verify     Provision + verify"
    echo "  $0 --help                         Show this help"
    echo ""
    echo "Modes:"
    echo "  --preset PRESET     Model preset: 'standard' or 'developer'."
    echo "                      DESTRUCTIVE on an existing stack: redeploys"
    echo "                      default models, resets Open WebUI / Dify /"
    echo "                      Gitea defaults to script-managed values."
    echo "                      Refuses to run on a previously-initialised box"
    echo "                      unless --force is also passed."
    echo "  --refresh           IDEMPOTENT post-upgrade re-config: refreshes"
    echo "                      /etc/hosts entries for any new module"
    echo "                      subdomains and auto-provisions GPUSTACK_API_KEY"
    echo "                      if it is empty/placeholder. Does NOT touch"
    echo "                      models, plugins, or operator-set defaults."
    echo "                      This is what razzfazz-upgrade.sh's post-upgrade"
    echo "                      reminder points at."
    echo "  --verify            Read-only API + UI verification suite."
    echo ""
    echo "Options:"
    echo "  --force             Allow --preset on an existing stack (operator"
    echo "                      acknowledges destruction risk to non-default"
    echo "                      models / Open WebUI / Dify / Gitea state)."
    echo "  --skip-models       Skip model deployment (configure services only)."
    echo "                      Other --preset steps still run, so this still"
    echo "                      requires --force on an existing stack."
    echo "  --skip-dns          Skip /etc/hosts DNS setup (in --preset / --refresh)."
    echo "  --skip-wait         Skip waiting for model downloads (deploy only)."
    echo "  --reconcile-models  With --refresh / --preset on an LLM Manager box: apply"
    echo "                      manifest changes (params/task) to existing standard-set"
    echo "                      deployments (#1263). Without it, drift is only reported."
    echo "  --debug             Enable verbose bash trace output (set -x)."
    echo ""
    echo "Presets:"
    echo "  standard   qwen3.6 (default, always-on) + gemma4 (on-demand spare) + embeddings + reranker"
    echo "  developer  qwen3.6 (default, always-on) + qwen3-coder-next (on-demand spare) + embeddings + reranker"
    echo ""
    echo "Prerequisites:"
    echo "  - razzfazz-init.sh completed successfully"
    echo "  - Stack is running: docker compose ps"
    echo "  - Internet access for model downloads (--preset only)"
}

# Parse arguments
PRESET=""
DO_VERIFY=false
DO_REFRESH=false
DO_FORCE=false
SKIP_MODELS=false
SKIP_DNS=false
SKIP_WAIT=false
SKIP_WAIT_AUTO=false   # #2195: true only when we chose it, not the operator
DO_RECONCILE_MODELS=false   # #1263: apply manifest drift to existing LLM Manager deployments

while [[ $# -gt 0 ]]; do
    case "$1" in
        --preset)
            PRESET="$2"
            if [[ "$PRESET" != "standard" && "$PRESET" != "developer" ]]; then
                print_error "Invalid preset: $PRESET (must be 'standard' or 'developer')"
                exit 1
            fi
            shift 2
            ;;
        --verify)
            DO_VERIFY=true
            shift
            ;;
        --refresh)
            DO_REFRESH=true
            shift
            ;;
        --force)
            DO_FORCE=true
            shift
            ;;
        --skip-models)
            SKIP_MODELS=true
            shift
            ;;
        --skip-dns)
            SKIP_DNS=true
            shift
            ;;
        --skip-wait)
            SKIP_WAIT=true
            shift
            ;;
        --reconcile-models)
            DO_RECONCILE_MODELS=true
            shift
            ;;
        --debug)
            DEBUG_MODE=true
            set -x
            shift
            ;;
        --help|-h)
            print_help
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            print_help
            exit 1
            ;;
    esac
done

# Require at least one mode
if [ -z "$PRESET" ] && [ "$DO_VERIFY" = false ] && [ "$DO_REFRESH" = false ]; then
    print_error "Please specify --preset, --refresh, and/or --verify"
    echo ""
    print_help
    exit 1
fi

# ==============================================================================
# Main Execution
# ==============================================================================
echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}     rzfz.ai Post-Install Provisioning${NC}"
if [ -n "$PRESET" ]; then
echo -e "${BLUE}     Preset: ${GREEN}${PRESET}${NC}"
fi
echo -e "${BLUE}======================================================================${NC}"

# Load environment
check_prerequisites
load_env
print_info "Domain: ${MAIN_DOMAIN:-NOT SET} | GPUStack port: ${GPUSTACK_PORT:-9090 (default)}"

# #187: CPU boxes deploy models slowly (no GPU). Waiting for the downloads inline is
# what made post-install appear to "hang" at "Updating .env with model configuration".
# Default CPU installs to --skip-wait so models deploy in the background; the operator
# can still force a blocking wait by not passing --skip-wait on a GPU box. HARDWARE is
# populated by load_env above; SKIP_WAIT is the parsed flag (default false).
if [ "${HARDWARE:-}" = cpu ] && [ "$SKIP_WAIT" = false ]; then
    SKIP_WAIT=true
    # #2195: WHO chose the skip decides what happens afterwards. An operator who
    # typed --skip-wait asked not to wait and keeps #1507 review finding 4's
    # behaviour; a CPU box that got it by default asked for nothing, and its
    # consumer wiring must still be repaired once the weights land.
    SKIP_WAIT_AUTO=true
    print_info "HARDWARE=cpu detected — defaulting to --skip-wait (models deploy in background)."
fi

# rc6.4: destructive-state guard. --preset re-deploys default models, resets
# Open WebUI / Dify / Gitea defaults, and re-installs Dify plugins. On a
# previously-initialised stack that's almost always destructive of operator
# state the upgrade just preserved. Detect operator state up-front and
# refuse to proceed without --force; --verify and --refresh stay safe.
detect_existing_state() {
    local indicators=()

    # BUG-1 (2026-05-16): the previous version of this function fired on:
    # (a) .razzfazz_initialized marker present and (b) GPUSTACK_API_KEY set
    # but model_count == 0. Both signals fire on EVERY first-time --preset
    # run because razzfazz-init.sh creates the marker and populates the
    # API key as part of its own flow — before --preset ever runs.
    # We now only fire on REAL operator-set state: actual models deployed
    # in GPUStack. An API key alone is not operator state; it's init
    # bootstrap. The marker alone is not operator state; it's init
    # bootstrap. Without this fix, every wipe + init + post-install
    # --preset on a fresh box needs --force.

    local current_key
    current_key=$(read_env_value "$ENV_FILE" GPUSTACK_API_KEY)
    if [ -n "$current_key" ] && [ "$current_key" != "your-gpustack-api-key-here" ]; then
        # An API key already exists — query GPUStack for any deployed models.
        local model_count=0
        if curl -fsS --max-time 5 -o /tmp/.gpustack-models.$$ \
            -H "Authorization: Bearer $current_key" \
            "http://127.0.0.1:${GPUSTACK_PORT:-9090}/v1/models" 2>/dev/null; then
            model_count=$(python3 -c "
import json, sys
try:
    with open('/tmp/.gpustack-models.$$') as f:
        d = json.load(f)
    items = d.get('items', d.get('data', []))
    print(len(items) if isinstance(items, list) else 0)
except Exception:
    print(0)
" 2>/dev/null || echo 0)
        fi
        rm -f "/tmp/.gpustack-models.$$"
        if [ "$model_count" -gt 0 ]; then
            indicators+=("GPUStack already has $model_count model(s) — --preset would (re-)deploy default models alongside")
        fi
    fi

    if [ ${#indicators[@]} -eq 0 ]; then
        return 1
    fi
    print_warning "Detected existing operator state on this box:"
    for ind in "${indicators[@]}"; do
        print_info "  • $ind"
    done
    print_warning "What --preset would touch:"
    print_info "  • GPUStack: deploy_all_models adds the preset's qwen3.6/gemma4/etc."
    print_info "  • Open WebUI: default model selection, embedding, audio config reset to script defaults"
    print_info "  • Dify: re-installs default plugins; resets configured model providers"
    print_info "  • Gitea: resets admin user password to AUTHENTIK_BOOTSTRAP_PASSWORD"
    print_info "  • Onyx: re-runs first-time provisioning"
    return 0
}

# rc6.4: --refresh — idempotent post-upgrade safe re-config. Runs ONLY:
#   - setup_local_dns (data-driven from .env *_DOMAIN keys; idempotent)
#   - ensure_gpustack_api_key (only fires when key is empty/placeholder)
# This is what razzfazz-upgrade.sh's post-upgrade reminder points operators
# at after a stack-version bump that introduced new module subdomains.
# Every image:tag the manifest defines, for the hardware class given ($1), plus
# its legacy alias. Used by the build and by the publish step.
_runner_images_for_hardware() {
    local want="$1" row image dockerfile hw legacy args
    while IFS=$'\t' read -r image dockerfile hw legacy args; do
        [ -n "$image" ] || continue
        [ "$hw" = "$want" ] || continue
        printf '%s\n' "$image"
    done < <(razzfazz_runner_manifest_rows "$SCRIPT_DIR")
}

refresh_llama_vulkan_runner() {
    # rc6.7 #18 / rc6.8: build the local llama-* runner images if missing.
    # Already triggered from razzfazz-init.sh (Step 7b) and razzfazz-
    # upgrade.sh (Step 9b), but boxes that were init'd before those
    # steps existed (or where the build failed silently in the past)
    # still lack the images. Adding a third trigger here makes the
    # operator's normal post-upgrade --refresh workflow self-heal.
    # Idempotent: skipped if the image is already present.
    #
    # rc6.8: now covers all three runner images (vulkan / rocm / cpu),
    # all of which bundle the llama-server-shim that translates GPUStack
    # `--flag=value` backend_parameters into `--flag value`.
    # #184 WS2b: building a runner image pulls a base image + apt → internet
    # egress. On an air-gapped box these are loaded from the offline package.
    if razzfazz_offline_skip "llama-runner image builds"; then
        return 0
    fi
    # #1373: the images are consumed by the `llm` custom backends AND by the
    # `llm-worker-agent` node-agent (its AMD/CPU defaults ARE these images).
    # Gate on the shared consumer list; read the FILE, not the load_env export.
    local _rl_profiles
    _rl_profiles=$(read_env_value .env COMPOSE_PROFILES)
    if ! razzfazz_runner_images_wanted "$_rl_profiles"; then
        print_substep "No runner-image consumer profile active (one of: ${RAZZFAZZ_RUNNER_IMAGE_CONSUMER_PROFILES}) — skipping llama-runner image builds."
        return 0
    fi
    local hw="${HARDWARE:-amd}"
    local runners_dir="${SCRIPT_DIR}/modules/llm/runners"
    if [ ! -d "$runners_dir" ]; then
        return 0
    fi
    _build_one() {
        # $1 image:tag, $2 dockerfile (relative to runners_dir's parent),
        # $3 optional build-arg (KEY=VALUE). #628: the vulkan Dockerfile's
        # LLAMA_CPP_TAG default moves with the current release — building an
        # OLD tag without pinning the arg silently produces the NEW binary
        # under the old tag (a rollback that isn't one).
        local img="$1" df="$2" barg="$3"
        if docker image inspect "$img" >/dev/null 2>&1; then
            return 0
        fi
        print_step "Building $img (llama-server-shim runner image)..."
        if docker build -t "$img" ${barg:+--build-arg "$barg"} -f "${SCRIPT_DIR}/$df" "$runners_dir" 2>&1 | tail -5; then
            print_success "$img built."
        else
            print_warning "$img build failed — models targeting the matching backend will fail to start."
            print_info "  Re-run manually: docker build -t $img ${barg:+--build-arg $barg} -f $df modules/llm/runners"
        fi
    }
    # #1516 (E5): driven by modules/llm/runners/runners.yaml. `nvidia`/`cuda`
    # boxes build BOTH CUDA targets — sm_120 (RTX PRO 6000) and sm_121a (GB10):
    # CUDA 12.8's nvcc does not know sm_121, so one image cannot serve both, and
    # running the wrong one dies with "no kernel image is available for
    # execution on the device" (measured on the GB10, 2026-09-05). Which of the
    # two a node uses is decided on the node (#1517), not here.
    local _hw_class
    _hw_class=$(razzfazz_runner_hw_class "$hw")
    local _rows _image _dockerfile _rhw _legacy _args _built_any=0
    _rows=$(razzfazz_runner_manifest_rows "$SCRIPT_DIR")
    if [ -n "$_rows" ]; then
        while IFS=$'\t' read -r _image _dockerfile _rhw _legacy _args; do
            [ -n "$_image" ] || continue
            [ "$_rhw" = "$_hw_class" ] || continue
            _build_one "$_image" "$_dockerfile" $_args
            _built_any=1
            # One cycle of back-compat: a box pinned to the old name via
            # RAZZFAZZ_ENGINE_IMAGE_* must keep resolving locally.
            if [ -n "$_legacy" ] && docker image inspect "$_image" >/dev/null 2>&1; then
                docker tag "$_image" "$_legacy" >/dev/null 2>&1 || true
            fi
        done <<< "$_rows"
    fi
    if [ "$_built_any" = "0" ]; then
        print_substep "No runner target for hardware '${_hw_class}' in modules/llm/runners/runners.yaml — nothing built."
    fi
}


# #1472: publish the init-built runner images into the hub registry, so a thin
# node that lacks one can pull `<hub>/runners/<image>` (node-agent
# ensure_engine_image) instead of letting docker resolve the bare name against
# docker.io (404 — measured on 0.175, and a public-registry reach #307 forbids).
# Zot is reachable from THIS host at 127.0.0.1:${LLM_REGISTRY_PORT} (plain
# HTTP, in-network — no credential; the hub's Basic auth sits at the Caddy
# edge, #571). Idempotent: a push of an unchanged image is a no-op for Zot.
# Self-gated: llm-registry profile off → nothing; image absent → skipped.
RAZZFAZZ_RUNNER_REPO_PREFIX="runners"
# #1438 (E2): the node-agent image itself is delivered the same way — a thin
# node never builds it (compose.thin.yml has no build: block any more);
# worker-join pulls <hub>/node/razzfazz-llm-worker-agent:latest at join time.
RAZZFAZZ_NODE_REPO_PREFIX="node"
RAZZFAZZ_PUBLISHED_NODE_IMAGES="razzfazz-llm-worker-agent:latest"
# #1516 (E5): no hand-kept list any more — every target in
# modules/llm/runners/runners.yaml is a publish candidate, and each is pushed
# only if it is present locally. That is what makes #1497 impossible to repeat:
# the list and the build read the same file. Legacy aliases are published too,
# for one cycle, so a pinned box keeps resolving.
_published_runner_images() {
    local image dockerfile hw legacy args
    while IFS=$'\t' read -r image dockerfile hw legacy args; do
        [ -n "$image" ] || continue
        printf '%s\n' "$image"
        [ -n "$legacy" ] && printf '%s\n' "$legacy"
    done < <(razzfazz_runner_manifest_rows "$SCRIPT_DIR")
}
push_runner_images_to_registry() {
    local _profiles
    _profiles=$(read_env_value .env COMPOSE_PROFILES)
    if ! printf '%s' "$_profiles" | tr ',' '\n' | grep -qx "llm-registry"; then
        print_info "llm-registry profile not active — runner images stay local (nothing to publish)."
        return 0
    fi
    local port registry i
    port=$(read_env_value .env LLM_REGISTRY_PORT); port="${port:-8093}"
    registry="127.0.0.1:${port}"
    for i in 1 2 3 4 5 6; do
        curl -fsS --max-time 5 "http://${registry}/v2/" >/dev/null 2>&1 && break
        sleep 10
    done
    if ! curl -fsS --max-time 5 "http://${registry}/v2/" >/dev/null 2>&1; then
        print_warning "Hub registry not reachable on http://${registry}/v2/ — runner images NOT published; thin nodes without a local runner cannot serve until the next --refresh (#1472)."
        return 0
    fi
    local img pushed=0 failed=0 missing_node=0
    for img in $(_published_runner_images); do
        docker image inspect "$img" >/dev/null 2>&1 || continue
        if docker tag "$img" "${registry}/${RAZZFAZZ_RUNNER_REPO_PREFIX}/${img}" >/dev/null 2>&1 \
           && docker push "${registry}/${RAZZFAZZ_RUNNER_REPO_PREFIX}/${img}" >/dev/null 2>&1; then
            pushed=$((pushed + 1))
        else
            failed=$((failed + 1))
            print_warning "  push of ${img} to ${registry}/${RAZZFAZZ_RUNNER_REPO_PREFIX}/ failed (non-fatal)."
        fi
    done
    for img in $RAZZFAZZ_PUBLISHED_NODE_IMAGES; do
        # rev-B (#1438 review befund 5): a missing node image was skipped in
        # silence, the summary read "0 pushed", and a joining node then sent the
        # operator back here — a loop, because this run publishes nothing again.
        if ! docker image inspect "$img" >/dev/null 2>&1; then
            missing_node=$((missing_node + 1))
            print_warning "  ${img} is not on this master — nothing to publish. Build it first" \
                          "(docker compose --profile llm-worker-agent build), then re-run --refresh;" \
                          "a thin node cannot pull what the hub never received."
            continue
        fi
        if docker tag "$img" "${registry}/${RAZZFAZZ_NODE_REPO_PREFIX}/${img}" >/dev/null 2>&1 \
           && docker push "${registry}/${RAZZFAZZ_NODE_REPO_PREFIX}/${img}" >/dev/null 2>&1; then
            pushed=$((pushed + 1))
        else
            failed=$((failed + 1))
            print_warning "  push of ${img} to ${registry}/${RAZZFAZZ_NODE_REPO_PREFIX}/ failed (non-fatal)."
        fi
    done
    print_success "Runner + node images published to the hub registry (${RAZZFAZZ_RUNNER_REPO_PREFIX}/…, ${RAZZFAZZ_NODE_REPO_PREFIX}/…): ${pushed} pushed, ${failed} failed${missing_node:+, ${missing_node} node image(s) absent}."
}


prepull_agent_manager_images() {
    # #36 (pre-pull all module images, #120 principle): make the agent-manager-
    # offered EXPERIMENTAL agents actually provisionable out of the box.
    #
    # The agent-manager (agents profile) offers openhands + paperclip in its
    # catalog regardless of whether the `openhands`/`paperclip` COMPOSE_PROFILES
    # are on — it spawns per-user containers directly via the docker socket. But
    # these images are NEVER covered by `docker compose pull`:
    #   - openhands / agent-server are runtime-only (agent-manager pulls them at
    #     provision time; they don't appear in `docker compose config --images`).
    #   - razzfazz-stack-paperclip is a catalog-only image name (now built by the
    #     paperclip-image helper in modules/agents/compose.yml, but only when a
    #     `docker compose build` runs for the agents profile).
    # Without this prepull, the FIRST time a user provisions openhands/paperclip
    # they hit ImageNotFound → the `_friendly_launch_error` "isn't available on
    # this box yet" dead-end. Pre-pull/-build here so the offer matches reality.
    #
    # Gated on the `agents` profile (that's what enables the agent-manager),
    # NOT on openhands/paperclip. Idempotent: skips images already present.
    if ! echo ",${COMPOSE_PROFILES:-}," | grep -q ",agents,"; then
        return 0
    fi

    # #184 WS2b: this function `docker pull`s openhands (+ runtime) and `docker
    # build`s paperclip → registry / base-image egress. On an air-gapped box those
    # images are loaded from the offline package; skip the network fetch here.
    if razzfazz_offline_skip "agent-manager image pre-pull/build (openhands, paperclip)"; then
        return 0
    fi

    # Source the openhands + sandbox-runtime pins from the agent-manager catalog
    # so they stay coherent with what the manager will actually request (single
    # source of truth — no second copy of the version to drift).
    local catalog="${SCRIPT_DIR}/modules/agents/manager/app/services/catalog.py"
    local oh_ver oh_runtime_img
    if [ -f "$catalog" ]; then
        # openhands version: the first `'version': 'X'` line AFTER the
        # `'id': 'openhands'` marker (awk window avoids matching hermes/moltis).
        oh_ver=$(awk "/'id': 'openhands'/{f=1} f && /'version':/{print; exit}" "$catalog" \
                 | sed "s/.*'version': *'\([^']*\)'.*/\1/")
        # 1.6.0's CodeAct sandbox image (QUOTED-KEY match — comment lines have no
        # quotes around the key). On a future 1.8.0 bump this key changes to
        # AGENT_SERVER_IMAGE_* — update this extraction alongside the bump.
        oh_runtime_img=$(grep -E "'SANDBOX_RUNTIME_CONTAINER_IMAGE': *'" "$catalog" | head -1 | sed "s/.*': *'\([^']*\)'.*/\1/")
    fi
    oh_ver="${oh_ver:-1.6.0}"
    oh_runtime_img="${oh_runtime_img:-ghcr.io/openhands/runtime:1.6.0-nikolaik}"

    _prepull_one() {  # $1 image:tag, $2 human label
        local img="$1" label="$2"
        if docker image inspect "$img" >/dev/null 2>&1; then
            print_substep "$label already present ($img)."
            return 0
        fi
        print_step "Pre-pulling $label ($img)..."
        if docker pull "$img" 2>&1 | tail -2; then
            print_success "$label pulled."
        else
            print_warning "$label pull failed — provisioning it will fall back to the friendly 'not available' error until pulled."
            print_info "  Re-run manually: docker pull $img"
        fi
    }

    print_step "Pre-pulling agent-manager experimental-agent images (openhands, paperclip)..."
    _prepull_one "ghcr.io/openhands/openhands:${oh_ver}" "OpenHands ${oh_ver}"
    # OpenHands spawns this per-conversation sandbox runtime; pre-pull so the
    # first task doesn't stall on a multi-GB download (or fall back to BUILDING
    # a runtime image, which fails on apt-get in restricted-network sites).
    _prepull_one "${oh_runtime_img}" "OpenHands sandbox runtime"

    # Paperclip: catalog-named image built by the paperclip-image helper. Build
    # if missing (registry pull would 404 — it's a local build).
    if docker image inspect "razzfazz-stack-paperclip:latest" >/dev/null 2>&1; then
        print_substep "Paperclip image already present (razzfazz-stack-paperclip:latest)."
    else
        print_step "Building Paperclip image (razzfazz-stack-paperclip:latest)..."
        local pc_tag
        pc_tag=$(read_env_value .env PAPERCLIP_VERSION 2>/dev/null)
        pc_tag="${pc_tag:-v2026.609.0}"
        if docker build --build-arg "PAPERCLIP_TAG=${pc_tag}" \
                -t razzfazz-stack-paperclip:latest \
                "${SCRIPT_DIR}/modules/apps/paperclip" 2>&1 | tail -5; then
            print_success "Paperclip image built (${pc_tag})."
        else
            print_warning "Paperclip build failed — provisioning it will fall back to the friendly 'not available' error until built."
            print_info "  Re-run manually: docker build --build-arg PAPERCLIP_TAG=${pc_tag} -t razzfazz-stack-paperclip:latest modules/apps/paperclip"
        fi
    fi
}


prebuild_all_custom_images() {
    # Issue A (ga.1) — pre-build EVERY custom-build (build:) module image now, so a
    # later module-enable never triggers a LIVE build on the box. init's
    # `build --parallel` + `pull` only cover the profiles active AT init (single-box:
    # chat,dify,llm-legacy,monitor,searxng,stts,gotenberg,gitea), so cognee / agents /
    # mcp / paperclip — and dify-web / openwebui-seed when their profile was off at
    # init — have NO local image. The Config-portal enable path
    # (core/config/.../apply_manager.py: `docker compose up -d --no-deps
    # --force-recreate <svc>`, no pre-pull, no image-presence check) then does a live
    # `docker compose build` → base-image pull + apt/pip/npm → HTTP 403 "Request
    # forbidden by administrative rules" on egress-restricted / air-gapped boxes.
    # Building them HERE (same window init used, egress still open) makes every later
    # enable instant + offline-safe. #36 / #120.
    #
    # Idempotent: skips any image already built (the active-profile images built at
    # init are no-ops). Best-effort PER image — one failing optional build warns +
    # continues, never aborts post-install (project_postinstall_set_e_abort_pattern).
    # gpustack*/model-sync* are excluded: HARDWARE-specific llm-runtime images, built
    # at init for the active llm profile; the heavy inactive variants (e.g.
    # gpustack:vulkan on a CPU box) are out of scope for the enable-403 class.
    print_step "Pre-building custom module images (so later module-enable is offline-safe)..."

    # #184 WS2b: a `docker compose build` pulls base images + apt/pip/npm →
    # internet egress. On an air-gapped box every custom image is loaded from the
    # offline package, so there is nothing to pre-build here — skip and rely on
    # 'rzfz verify-images' to confirm the loaded set is complete.
    if razzfazz_offline_skip "custom module-image pre-build (loaded from the offline package)"; then
        return 0
    fi

    # The FULL custom-build image set — and the profile set used to render it —
    # come from the SHARED source-of-truth module
    # core/config/app/services/build_preflight.py, the SAME code the Config-Portal
    # module-enable pre-flight (apply_manager.py) uses to decide "which images
    # must exist". Deriving both from ONE module means the pre-build set and the
    # enable-time check can never drift — a module the pre-flight demands but the
    # pre-build skipped is exactly the #174/#184 enable-403 (the ga.1
    # razzfazz-mcp-manager / cognee regression). The module:
    #   • drops the three llm-runtime profiles (llm / llm-legacy / llm-cpu) whose
    #     shared gpustack/model-sync `container_name` would abort `compose config`
    #     → an empty enumeration → NOTHING pre-built;
    #   • excludes gpustack*/model-sync* (HARDWARE-specific, built at init for the
    #     ACTIVE llm profile);
    #   • resolves the derived `<project>-<service>` image name for build services
    #     with no explicit `image:` (cognee → razzfazz-stack-cognee, etc.), so the
    #     rows carry the REAL ref the present-check needs.
    # Every docker call it makes is a client-side compose parse or an IMAGES-endpoint
    # read (no /build), so it is safe to run here. #36 / #120 / #174 / #184.
    local preflight="${SCRIPT_DIR}/core/config/app/services/build_preflight.py"
    if [ ! -f "$preflight" ]; then
        print_warning "Shared build-image enumerator not found ($preflight) — skipping custom-image pre-build."
        print_info "  Module-enable may trigger a live build on egress-restricted boxes; re-run later: rzfz post-install --refresh"
        return 0
    fi

    # Profile set to render/build with (all profiles minus the llm-runtime trio).
    local all_profiles
    all_profiles=$(python3 "$preflight" --stack-root "$SCRIPT_DIR" --build-profiles 2>/dev/null)
    if [ -z "$all_profiles" ]; then
        print_warning "Could not list compose profiles — skipping custom-image pre-build."
        print_info "  Module-enable may trigger a live build on egress-restricted boxes; re-run later: rzfz post-install --refresh"
        return 0
    fi

    # (service<TAB>resolved-image) for EVERY custom-build service across all
    # profiles — the full set apply_manager's pre-flight demands.
    local rows
    rows=$(python3 "$preflight" --stack-root "$SCRIPT_DIR" --all-build-images 2>/dev/null)
    if [ -z "$rows" ]; then
        print_warning "Could not enumerate build services (docker compose config failed?) — skipping pre-build."
        print_info "  Module-enable may trigger a live build on egress-restricted boxes; re-run later: rzfz post-install --refresh"
        return 0
    fi

    # #184 WS2a: strip the no-build overlay so `docker compose build <svc>` below
    # actually builds (the overlay neutralises `build:`; this is the explicit
    # pre-build path that must keep the build: contexts). No-op pre-#184.
    local _build_cf; _build_cf="$(compose_file_for_build)"
    # #2006 part 2: "present" used to be the skip rule — and a present image can be
    # nine days older than the Dockerfile fix (0.175, #2105), or carry the right
    # tag from a different tree. Ask the provenance verdicts ONCE for the whole
    # set and skip ONLY what is this tree's build (or the package's build of the
    # same context). Stale images are rebuilt; a missing helper falls back to
    # the presence rule and says so.
    local verdicts; verdicts="$(razzfazz_custom_image_verdicts "$all_profiles")" || verdicts=""
    [ -n "$verdicts" ] || print_warning "  Provenance verdicts unavailable — falling back to the presence rule (a present image is skipped even if it predates this tree; #2006)."
    local built=0 present=0 failed=0 stale=0 svc img verdict detail
    while IFS=$'\t' read -r svc img; do
        [ -z "$svc" ] && continue
        verdict=""; detail=""
        if [ -n "$img" ] && [ -n "$verdicts" ]; then
            verdict="$(printf '%s\n' "$verdicts" | awk -F'\t' -v r="$img" '$1==r{print $3; exit}')"
            detail="$(printf '%s\n' "$verdicts" | awk -F'\t' -v r="$img" '$1==r{print $4; exit}')"
        fi
        case "$verdict" in
            this-build|package-build|unknown)
                # unknown = present, digest not computable (no git) — presence is
                # all that can be known there; the verdict says so, not hidden.
                present=$((present + 1)); continue ;;
            stale)
                print_substep "Rebuilding ${svc} (${img}) — present but NOT this tree's build: ${detail}"
                stale=$((stale + 1)) ;;
            missing) ;;
            *)
                # No verdict for this image (helper unavailable, or the image is
                # not in the render): the old presence rule.
                if [ -n "$img" ] && docker image inspect "$img" >/dev/null 2>&1; then
                    present=$((present + 1)); continue
                fi ;;
        esac
        print_substep "Building ${svc}${img:+ ($img)}..."
        if COMPOSE_FILE="$_build_cf" COMPOSE_PROFILES="$all_profiles" docker compose build "$svc" >/dev/null 2>&1; then
            print_success "Built ${svc}."
            built=$((built + 1))
        else
            print_warning "Build of '${svc}' failed (non-fatal) — enabling it later may trigger a live build. Re-run: docker compose build ${svc}"
            failed=$((failed + 1))
        fi
    done <<< "$rows"
    # #2006 part 2: record what this pass built (and re-record what it verified)
    # so the next init/post-install can tell this tree's images from adopted ones.
    razzfazz_record_custom_image_builds "$all_profiles" built

    # #433: a run with failures must not end on a green check mark.
    if [ "$failed" -gt 0 ]; then
        print_warning "Custom-image pre-build finished WITH FAILURES (built=$built of which stale-rebuilt=$stale, already-this-tree=$present, failed=$failed) — enabling an affected module later may trigger a live build."
    else
        print_success "Custom-image pre-build done (built=$built of which stale-rebuilt=$stale, already-this-tree=$present, failed=$failed)."
    fi
}


# Issue B (ga.1) PRIMARY fix — "OWUI shows 0 models".
# OpenWebUI runs with ENABLE_PERSISTENT_CONFIG (modules/chat/compose.yml), so the
# OpenAI connection (base URL + OPENAI_API_KEYS) is read from env ONLY on the
# container's FIRST boot and thereafter lives in the `config` table (openwebui_db).
# That first boot was during `rzfz init`, when .env still held the placeholder
# GPUSTACK_API_KEY — so OWUI's persisted connection carries the DEAD placeholder
# key, can't auth to gpustack (401 on /v1-openai/models) and shows 0 models. NO
# .env edit or plain container recreate fixes it: the persisted DB value wins over
# env. We rewrite the placeholder → the REAL key ensure_gpustack_api_key wrote,
# directly in the persisted JSON, so the openwebui recreate in Step 8 reloads a
# working connection. This is the fix that actually makes models appear. Idempotent
# (the LIKE guard makes it a no-op once the placeholder is gone); best-effort.
_owui_fix_persisted_gpustack_key() {
    # #1250b: GPUStack-only by construction — the placeholder it rewrites is a
    # GPUStack key, and a Manager box's OWUI connection is reconciled by
    # scripts/owui_config_reconcile.py (#1185) instead. Without this gate a
    # clean Manager install printed "live GPUStack key not available yet —
    # skipping" as a WARNING for something it will never have.
    _gpustack_profile_active || return 0
    local placeholder="gpustack_CHANGEME_AFTER_FIRST_START" real_key
    real_key=$(read_env_value "$ENV_FILE" GPUSTACK_API_KEY)
    if [ -z "$real_key" ] || [ "$real_key" = "$placeholder" ]; then
        print_warning "OWUI key-fix: live $(_llm_backend_display_name) key not available yet — skipping persisted-key rewrite."
        return 0
    fi
    local db="${OPENWEBUI_DB:-openwebui_db}" pguser="${POSTGRES_USER:-docker}"
    # OWUI's `config` table schema differs by version:
    #   • ≤0.9.x  — a SINGLE row with a `data` json/jsonb column holding the whole
    #               config tree (openai.api_keys[] etc. nested inside `data`).
    #   • 0.10.x  — a KEY-VALUE table: columns `key` (text) + `value` (json), one
    #               row per dotted config path (`openai.api_keys`, plus a nested
    #               `openai` blob). The placeholder lives in the flattened
    #               `openai.api_keys` row, which OWUI reads on boot — the admin-API
    #               `/api/v1/configs/import` only rewrites the nested `openai` blob,
    #               NOT the flattened leaf, so it does NOT clear the placeholder.
    # Detect which schema is present and rewrite the placeholder in-place in EVERY
    # row that still carries it (covers openai.api_keys[], api_configs.*, and the
    # RAG openai/reranker keys — all seeded from the same placeholder). ::text
    # round-trip is shape-agnostic; psql -v :'…' quoting is injection-safe.
    #
    # ga.1 iter4 ROOT-CAUSE FIX: the UPDATE below MUST be fed on STDIN (here-string),
    # NOT via `-c`. psql performs `:'var'` client-side interpolation only when the
    # SQL is read from a file or stdin — with `-c` the `:'ph'`/`:'key'` tokens reach
    # the server verbatim and it errors "syntax error at or near :". The prior `-c`
    # form silently failed (its output is swallowed by `> /dev/null 2>&1`), so the
    # placeholder in the flattened `openai.api_keys` leaf was NEVER rewritten and
    # OWUI kept showing 0 gpustack models (qwen3.6 absent) even after this fix
    # "ran". Same constraint cli/lib-set-password.sh documents at its :'em' UPDATE.
    local data_col value_col
    data_col=$(docker exec postgres psql -U "$pguser" -d "$db" -tAc \
        "SELECT data_type FROM information_schema.columns WHERE table_name='config' AND column_name='data';" \
        2>/dev/null | tr -d '[:space:]')
    value_col=$(docker exec postgres psql -U "$pguser" -d "$db" -tAc \
        "SELECT data_type FROM information_schema.columns WHERE table_name='config' AND column_name='value';" \
        2>/dev/null | tr -d '[:space:]')

    local target_col target_type
    if [ "$data_col" = "json" ] || [ "$data_col" = "jsonb" ]; then
        target_col="data";  target_type="$data_col"           # ≤0.9.x single-row
    elif [ "$value_col" = "json" ] || [ "$value_col" = "jsonb" ]; then
        target_col="value"; target_type="$value_col"          # 0.10.x key-value
    else
        print_warning "OWUI key-fix: no config json column (neither 'data' nor 'value' present — OWUI config not initialised?) — skipping."
        return 0
    fi

    if docker exec -i postgres psql -v ON_ERROR_STOP=1 -U "$pguser" -d "$db" \
            -v ph="$placeholder" -v key="$real_key" \
            <<< "UPDATE config SET ${target_col} = replace(${target_col}::text, :'ph', :'key')::${target_type} WHERE ${target_col}::text LIKE '%' || :'ph' || '%';" \
            > /dev/null 2>&1; then
        print_substep "OWUI persisted OpenAI-connection key rewritten to the live $(_llm_backend_display_name) key (config.${target_col})."
    else
        print_warning "OWUI key-fix: persisted-key UPDATE returned non-zero (non-fatal) — OWUI may still show 0 models."
    fi
}

# #976: repoint OWUI's PERSISTED OpenAI connection to the LLM Manager.
#
# Same ENABLE_PERSISTENT_CONFIG trap as _owui_fix_persisted_gpustack_key: the
# connection base URL + key live in `openwebui_db.config` and the env
# OPENAI_API_BASE_URLS/KEYS are read ONLY on first boot. So on an llm-manager
# box a .env edit + recreate never moves the persisted connection.
#
# #1185: this used to be an SQL text `replace()` over the serialised JSON —
# which is exactly how `[gpustack, manager]` became `[manager, manager]`
# (two identical connections, both with the stale .env key) on 0.91. It is
# now the JSON-aware upsert in scripts/owui_config_reconcile.py, driven by
# owui_reconcile_openai_keys with a key _owui_llm_key has VALIDATED. Runs
# from wire_llm_manager_consumers, AFTER the env seed. Best-effort.
_owui_repoint_persisted_openai_to_manager() {
    # #1445 (C5c) re-review: this writes an ADDRESS into OWUI's persisted
    # config, and since C5 the address is canonical on every box — so the
    # question is "does this box run the manager, and is it up?", not "does the
    # manager own the models?". On a dual, not-yet-federated box ownership was
    # false and the repoint simply never ran, leaving .env and openwebui_db
    # disagreeing with each other.
    _llm_manager_profile_active && _llm_manager_running || return 0   # #1445
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx openwebui || return 0
    local owui_key
    owui_key=$(_owui_llm_key)
    if [ -z "$owui_key" ]; then
        print_warning "OWUI repoint: no manager service key available — persisted connection left as-is."
        return 0
    fi
    owui_reconcile_openai_keys "$(_owui_llm_base_url)" "$owui_key" "$(_owui_rerank_url)"
}


# Issue B (ga.1) fix #1 — recreate the ACTIVE model-sync variant so it re-reads the
# real GPUStack API key. model-sync polls gpustack /v1-openai/models and writes the
# model list into openwebui_db; init started it with the placeholder key, so without
# a recreate it keeps 401'ing and writing 0 models. --no-deps is MANDATORY: every
# model-sync variant depends_on gpustack(-legacy), and a plain recreate would pull +
# recreate gpustack and unload the running models (this actually broke the box during
# ga.1 testing). Picks the variant by the active llm profile. Best-effort.
recreate_active_model_sync() {
    local msync=""
    # #1447 (cutover C7) DoD: the two arms below used to pick `model-sync-cpu`
    # and `model-sync`, and NEITHER SERVICE EXISTS any more — C7a removed the
    # GPUStack 2.x stack and C7b folded the CPU variant into llm-legacy. On a
    # box that still carried the retired token this ran
    # `docker compose up -d --no-deps --force-recreate model-sync-cpu`, compose
    # refused an undefined service, and the else-branch below printed
    # "model-sync-cpu recreate returned non-zero — see verify report" — a
    # warning that points the operator at a report instead of saying the service
    # was retired. Best-effort, so nothing broke; it just misinformed.
    case ",$(read_env_value "$ENV_FILE" COMPOSE_PROFILES)," in
        *,llm-legacy,*) msync="model-sync-legacy" ;;
    esac
    [ -z "$msync" ] && return 0
    if docker compose up -d --no-deps --force-recreate "$msync" > /dev/null 2>&1; then
        print_substep "model-sync ($msync) recreated with the live $(_llm_backend_display_name) key (--no-deps)."
    else
        print_warning "$msync recreate returned non-zero — see verify report below."
    fi
}


# ga.1 iter3 (agents → gpustack local-model wire). Coding-agent instances get
# their {{GPUSTACK_API_KEY}} env placeholder substituted from the AGENT-MANAGER
# container's OWN os.environ at provision time (provisioner._resolve_env). The
# agent-manager container is created during `rzfz init` while .env still holds the
# placeholder GPUStack key, so its env carries the dead placeholder and EVERY
# agent it provisions (incl. the user's own coding agents) is wired with
# `OPENAI_API_KEY=gpustack_CHANGEME_AFTER_FIRST_START` → gpustack 401 → local
# inference fails. `sync_consumers` does not re-key existing instances, and
# provisioner.upgrade() short-circuits ("nothing to do") when the image version is
# unchanged, so a plain relaunch won't re-resolve the env either.
#
# Fix, in two halves:
#   (a) recreate agent-manager (--no-deps) so its env picks up the live key from
#       .env → all FUTURE provisions are correct.
#   (b) force-relaunch every RUNNING instance whose baked wiring no longer
#       matches a fresh provision, via provisioner.upgrade(force=True), so the
#       baked OPENAI_API_KEY / GPUSTACK_API_KEY are re-resolved to the live key.
#
# #1446 (cutover C6, point 4): (b) used to cover the four SANDBOXED_TYPES only.
# hermes, moltis, openhands, paperclip and pre-split coding-tools were never
# re-wired by an upgrade — they kept the LLM endpoint, the LLM key and the
# user's MCP proxies (the per-user cognee proxy among them, #785 LOW 1) that
# were resolved when their owner provisioned them. On a cutover box that is the
# retired gpustack endpoint, and the agent goes quiet rather than loud: the
# container is healthy, it just talks to something that is not there.
# The filter is now the DIFFERENCE, not the type: provisioner.stale_wiring()
# compares what the container carries against what a fresh provision would
# produce, so an in-sync instance is left alone whatever its type.
# Profile-gated (agents) + best-effort. No-op on a fresh box (no instances yet —
# the user self-provisions later against the now-correct agent-manager).
rekey_coding_agents() {
    echo "${COMPOSE_PROFILES:-}" | grep -qw "agents" || return 0
    docker ps --format '{{.Names}}' | grep -qx "agent-manager" || return 0
    # #1250b: the step is backend-agnostic (it recreates agent-manager so its
    # env reloads whatever key .env now holds); only the LABEL was GPUStack-era.
    # #1445 (C5c) re-review: this is a LABEL, and it contradicted the label two
    # lines down. `_rekey_backend` followed ownership while
    # `_llm_backend_display_name` follows the profile, so on a dual,
    # not-yet-federated box the same step printed "re-keying to the live
    # GPUStack key" and then "recreated with the live LLM Manager key". One
    # question, one answer: the helper that exists for exactly this.
    local _rekey_backend
    _rekey_backend=$(_llm_backend_display_name)
    print_step "Coding agents: re-keying to the live ${_rekey_backend} key..."

    # (a) recreate agent-manager so its env reloads the real GPUSTACK_API_KEY.
    if docker compose up -d --no-deps --force-recreate agent-manager > /dev/null 2>&1; then
        print_substep "agent-manager recreated with the live $(_llm_backend_display_name) key (--no-deps)."
    else
        print_warning "agent-manager recreate returned non-zero — newly-provisioned agents may still carry the placeholder."
    fi

    # Wait for the Flask app inside agent-manager to be importable again.
    local waited=0
    while [ "$waited" -lt 60 ]; do
        if docker exec agent-manager python3 -c "from app import create_app; create_app()" > /dev/null 2>&1; then
            break
        fi
        sleep 3; waited=$((waited + 3))
    done

    # (b) force-relaunch running sandboxed coding-agent instances so their env is
    # re-resolved with the live key. Uses the manager's own provisioner/catalog
    # context (authoritative), exactly like the day-1 provisioner probe.
    docker exec -e RAZZFAZZ_ADMIN_USERNAME="${RAZZFAZZ_ADMIN_USERNAME:-}" \
        agent-manager python3 -c '
import json
import os
from app import create_app
app = create_app()
rekeyed = []; skipped = []; failed = []; unknown = []
with app.app_context():
    for inst in app.db.get_all_instances():
        if inst.get("state") != "running":
            skipped.append((inst.get("container_name"), inst.get("state")))
            continue
        # #1148 review: the bare literal here made a nameless instance
        # rekey under the OLD superuser name on a box that no longer has one.
        who = (inst.get("user_id") or inst.get("user_slug")
               or os.environ.get("RAZZFAZZ_ADMIN_USERNAME") or "akadmin")
        # #1446 (C6/4): recreate the instances whose BAKED wiring no longer
        # matches a fresh provision — whatever their type. The old filter was
        # the four sandboxed coding types, so hermes / moltis / openhands /
        # paperclip kept the endpoint, the key and the MCP proxies they were
        # created with, through every upgrade (#785 LOW 1).
        try:
            stale = app.provisioner.stale_wiring(inst, who)
        except Exception as e:
            unknown.append((inst.get("container_name"), repr(e)))
            continue
        if stale is None:
            unknown.append((inst.get("container_name"), "cannot tell"))
            continue
        if not stale:
            skipped.append((inst.get("container_name"), "in sync"))
            continue
        try:
            iid, msg = app.provisioner.upgrade(inst["id"], who, force=True)
            (rekeyed if iid else failed).append((inst.get("container_name"), stale, msg))
        except Exception as e:
            failed.append((inst.get("container_name"), stale, repr(e)))
print("REKEY_JSON:" + json.dumps({"rekeyed": rekeyed, "skipped": skipped,
                                  "failed": failed, "unknown": unknown}))
' 2>&1 | while IFS= read -r line; do
        case "$line" in
            REKEY_JSON:*) print_substep "coding-agent re-key: ${line#REKEY_JSON:}" ;;
        esac
    done
    print_success "Coding-agent re-key complete."
}


# ga.1 (upgrade day-1-green) — the idempotent GPUStack-key/wiring self-heal subset.
# This is the SAME provisioning fix set the --preset "Step 8" restart block applies
# on a fresh install (KEEP IN SYNC with cli/post-install.sh Step 8, ~the
# `_owui_fix_persisted_gpustack_key` / `recreate_active_model_sync` / Dify-pgvector /
# `rekey_coding_agents` sequence). Splitting it into a named function lets the
# --refresh path run it too, so an UPGRADED box self-heals to day-1-green without a
# destructive --preset --force re-deploy: `rzfz upgrade` calls `post-install --refresh`
# automatically after the upgrade. Every step is idempotent, profile-gated and
# best-effort (never aborts). It does NOT deploy/redeploy models and has no
# "already has models" short-circuit — so it runs regardless of GPUStack model state.
refresh_apply_day1_wiring_fixes() {
    print_step "Applying day-1 wiring self-heal (OWUI key, model-sync, Dify pgvector, coding-agent re-key)..."
    # #152 (2026.07-ga.3): rebuild certs/caddy-ca.pem as an OIDC CA superset
    # (system public CA bundle + Caddy internal root CA on TLS_MODE=internal) and
    # restart the running OIDC clients (openwebui/gitea/vaultwarden). Upgraded boxes
    # need this precisely because init.sh does NOT run on `rzfz upgrade`; prod 8.246
    # broke OIDC/SSO login on Let's Encrypt (authlib CERTIFICATE_VERIFY_FAILED) until
    # this superset was rebuilt by hand. Run it FIRST so the CA is correct before the
    # openwebui recreate below (the helper also restarts openwebui; the recreate that
    # follows is idempotent). ensure_oidc_ca_superset is defined in scripts/lib.sh,
    # sourced at the top of this script; best-effort under set -e (returns 0).
    ensure_oidc_ca_superset
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "chat"; then
        # Rewrite OWUI's persisted placeholder GPUStack key -> the live key, THEN
        # recreate openwebui (--no-deps) so it reloads a working OpenAI connection
        # and models finally appear. See _owui_fix_persisted_gpustack_key.
        _owui_fix_persisted_gpustack_key
        docker compose up -d --no-deps --force-recreate openwebui > /dev/null 2>&1 \
            || print_warning "openwebui recreate returned non-zero (non-fatal) — OWUI may still show 0 models until re-run."
    fi
    # Recreate the ACTIVE model-sync variant (--no-deps) so it re-reads the live key
    # and writes the real model list into openwebui_db. Gated on the active llm profile.
    recreate_active_model_sync
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "dify"; then
        # #160 (model-provider icons broken since Dify 1.15): dify-api builds provider
        # icon_small / file URLs from its public-URL env (CONSOLE_API_URL, FILES_URL, …),
        # sourced from .env.dify as `https://${DIFY_DOMAIN}`. Compose's interpolation of the
        # nested ${DIFY_DOMAIN}->dify.${MAIN_DOMAIN} at container-create is context-flaky and
        # has left the literal `dify.${MAIN_DOMAIN}` in the running env → the console renders
        # <img src="https://dify.${MAIN_DOMAIN}/…"> against a bogus host → broken icons.
        # Resolve the real host from .env (respecting a custom DIFY_DOMAIN) and bake it into
        # .env.dify so dify-api never depends on interpolation; also self-heals a post-init
        # MAIN_DOMAIN change (S20 class). Idempotent. The recreate below applies it.
        _md160=$(grep -E '^MAIN_DOMAIN=' "${SCRIPT_DIR}/.env" 2>/dev/null | cut -d= -f2- | tr -d '"') || true
        _dd160=$(grep -E '^DIFY_DOMAIN=' "${SCRIPT_DIR}/.env" 2>/dev/null | cut -d= -f2- | tr -d '"') || true
        _dd160="${_dd160//\$\{MAIN_DOMAIN\}/$_md160}"; _dd160="${_dd160//\$MAIN_DOMAIN/$_md160}"
        if [ -n "$_dd160" ] && ! printf '%s' "$_dd160" | grep -q '[${]'; then
            for _k in CONSOLE_API_URL CONSOLE_WEB_URL APP_API_URL APP_WEB_URL FILES_URL TRIGGER_URL WEB_API_CORS_ALLOW_ORIGINS CONSOLE_CORS_ALLOW_ORIGINS; do
                # #1381: inode-preserving. .env.dify is a single-FILE bind mount
                # (core/compose.yml → razzfazz-config /stack/.env.dify); `sed -i`
                # renamed a new inode over it and the Config UI kept the stale one
                # (#4 class). update_env_value truncates the same inode.
                grep -qE "^${_k}=https?://" "${SCRIPT_DIR}/.env.dify" 2>/dev/null \
                    && update_env_value "${SCRIPT_DIR}/.env.dify" "$_k" "https://${_dd160}"
            done
            print_substep "Baked resolved host into .env.dify Dify URLs -> https://${_dd160} (#160) — provider icons render regardless of Compose interpolation."
        fi
        # Recreate the pgvector-consuming Dify services so the corrected PGVECTOR_PASSWORD
        # lands (heals dataset-indexing 401s) AND the #160 URL bake above takes effect. --no-deps.
        docker compose up -d --no-deps --force-recreate dify-api dify-worker dify-worker-beat > /dev/null 2>&1 \
            || print_warning "Dify pgvector-service recreate returned non-zero (non-fatal) — see verify report."
    fi
    # Re-key provisioned coding agents to the live GPUStack key (gated on agents).
    rekey_coding_agents
    # Give the recreated openwebui + model-sync time to come up and complete their
    # first gpustack /v1-openai/models poll → write the model list into openwebui_db,
    # so a day-1 check that runs right after (e.g. the post-upgrade suite) sees the
    # models. Mirrors the fresh-install Step 8 "stabilize" wait. Only when chat/llm
    # is active (nothing to wait for otherwise).
    # #1447: the retired `llm` / `llm-cpu` tokens are gone from this gate. They
    # could only match a box the upgrade has not migrated yet, and on such a box
    # the wait is for a model-sync that does not start.
    if echo ",${COMPOSE_PROFILES:-}," | grep -qE ',chat,|,llm-legacy,'; then
        print_substep "Waiting 15s for openwebui + model-sync to stabilize..."
        sleep 15
    fi
    print_success "Day-1 wiring self-heal complete."
}


# ga.1 (§16 handover): leave an as-built security-posture assessment on the box.
# The security-architecture doc §16 (customer day-1 handover checklist) lists the
# posture a fresh box must satisfy — public signup closed on the user-facing apps,
# host firewall restricted, secrets/TLS/backups sane. `rzfz security-check` is the
# tool that verifies exactly that; until now NOTHING ran it, contradicting the
# script's own "what rzfz.ai runs on each box post-install" claim. Run it here
# (both --preset and --refresh) so every box carries a dated as-built posture
# report under security-run/. `--no-cve` skips the slow Trivy/Grype pass (a
# release-time / on-demand concern that can starve a freshly-provisioned stack);
# the posture checks (signup / firewall / secrets / TLS / backups) are what the
# day-1 handover needs. Non-fatal: a posture WARNING must not abort provisioning,
# but the operator is told where the report is + whether anything FAILED.
run_security_selfcheck() {
    local script="${SCRIPT_DIR}/cli/security-check.sh"
    [ -x "$script" ] || script="${SCRIPT_DIR}/legacy/razzfazz-security-check.sh"
    if [ ! -x "$script" ]; then
        print_warning "security-check.sh not present — skipping day-1 posture assessment."
        return 0
    fi
    print_step "Day-1 security posture: leaving an as-built assessment (rzfz security-check --no-cve)..."
    local rc=0
    "$script" --no-cve || rc=$?
    if [ "$rc" -ne 0 ]; then
        print_warning "security-check reported ${rc} FAIL item(s) — a §16 posture item needs attention (see the dated report under security-run/). Non-fatal for provisioning."
    fi
    return 0
}


# ga.1 (#147 follow-up — upgrade day-1-green, MISSING-MODELS case): make sure the
# standard preset's DEFAULT models are actually DEPLOYED in gpustack after the
# wiring self-heal. refresh_apply_day1_wiring_fixes above re-wires OWUI / model-sync /
# Dify / coding-agents to the live GPUStack key, but it deliberately never deploys
# models (see its header: "does NOT deploy/redeploy models"). So a box whose gpustack
# came up on a fresh / version-reset gpustack_db with 0 registered models (native
# /v1/models=0 — e.g. after the 2026.07 vulkan-image rebuild) stays at 0 models: OWUI
# + gpustack list nothing, and the AFTER-upgrade day-1 suite fails
# (test_gpustack_lists_at_least_one_model, test_openwebui_lists_at_least_one_model,
# test_default_chat_model_answers_non_empty, the wire1/wire2 model checks, OWUI-knowledge).
#
# Idempotent by construction:
#   • the DEFAULT aliases come from standard-models.yaml's `defaults` block
#     (distinct values across chat/coding/general/vision/embedding/reranker — never
#     hardcoded; today qwen3.6 + qwen3-embedding + qwen3-reranker);
#   • we query the live registered-model set ONCE and only invoke the deploy path
#     when at least one default is MISSING;
#   • when a real customer's models already persist across the upgrade every default
#     is present → we SKIP entirely (no re-download, no reset, no scale change);
#   • the deploy itself reuses deploy_all_models, which is also idempotent
#     (deploy_model early-returns for any already-registered model) and honours the
#     iteration-1 order (always-on qwen3.6 first; auto_start:false spares registered
#     + scaled to 0, non-blocking — no wait_for_all_models so `rzfz upgrade` doesn't
#     hang on multi-GB downloads).
#
# Profile-gated + best-effort (never aborts the refresh). The gate is
# `llm_manager_deploys_standard_set || _gpustack_profile_active`, and since
# #1447 the latter matches `llm-legacy` ALONE — `llm` is removed and
# `llm-cpu` folded in. The old list of three named a gate that no longer
# exists, which is how a reader learns a rule the code stopped following.
refresh_ensure_default_models_deployed() {
    # #1441/#1442: the manager arm is chosen by llm_manager_deploys_standard_set
    # (the --preset arm asks the same); a GPUStack box deploys in GPUStack below
    # and federates it afterwards. No LLM profile at all → nothing to do.
    if ! llm_manager_deploys_standard_set && ! _gpustack_profile_active; then
        return 0
    fi
    if llm_manager_deploys_standard_set; then
        # rev-B (2): `rzfz upgrade` provisions through `post-install --refresh`,
        # and --refresh is MUTUALLY EXCLUSIVE with --preset — so the #1250b
        # manager deploy, which lives in the --preset arm, never runs on an
        # upgrade. An upgraded Manager box therefore kept the exact defect
        # #1250b fixes: .env pointing every consumer at qwen3.6 /
        # qwen3-embedding while /api/deployments stayed []. Same call, same
        # single manifest, idempotent by construction (an existing deployment is
        # left untouched), and best-effort — a refresh must never abort here.
        # SKIP_WAIT is honoured so `--skip-wait` still returns immediately
        # instead of blocking the upgrade on a multi-GB weight download.
        if _llm_manager_running; then
            local _llmm_refresh_wait
            if [ "${SKIP_WAIT:-false}" = true ]; then _llmm_refresh_wait=false; else _llmm_refresh_wait=true; fi
            # #1507 review finding 2: `rzfz upgrade` comes through HERE, not
            # through the --preset arm, and this arm is worse off — OWUI's
            # model-sync already ran (refresh_apply_day1_wiring_fixes, above),
            # so its list predates this deploy. Remember both cases: a deploy
            # that did not reach "all ready", and one that CREATED something
            # the earlier model-sync could not have seen. The late reconcile
            # after Dify's step re-runs exactly the two enumerating steps.
            if llm_manager_deploy_standard_set "standard" "$_llmm_refresh_wait"; then
                if [ "${_LLMM_DEPLOYED_NEW:-0}" -gt 0 ]; then
                    _LLMM_DEPLOY_INCOMPLETE=1
                fi
            else
                print_warning "LLM Manager standard-set deploy reported an issue (non-fatal) — open the LLM Manager console, or re-run 'rzfz post-install --preset standard'."
                _LLMM_DEPLOY_INCOMPLETE=1
            fi
            # #1263: a manifest change never reached a box that had already
            # deployed (the deploy above skips existing rows). Report always;
            # apply only when the operator asked for it.
            llm_manager_manifest_drift_report "standard" || true
            if [ "${DO_RECONCILE_MODELS:-false}" = true ]; then
                llm_manager_reconcile_standard_set "standard" \
                    || print_warning "LLM Manager reconcile reported an issue (non-fatal) — see above."
            fi
        else
            print_warning "llm-manager profile active but the container is not running — the standard model set was not deployed. Start the stack and re-run 'rzfz post-install --preset standard'. (#1250b)"
        fi
        return 0
    fi

    print_step "Ensuring the standard preset's default models are deployed (post-upgrade day-1)..."

    # GPUStack must be up + keyed to query/deploy. Read the key straight from .env
    # (grep, never source .env); gpustack_api / deploy_model consume the exported var.
    local gp_key
    gp_key=$(read_env_value "$ENV_FILE" GPUSTACK_API_KEY)
    if [ -z "$gp_key" ] || [ "$gp_key" = "gpustack_CHANGEME_AFTER_FIRST_START" ]; then
        print_warning "GPUSTACK_API_KEY not set yet — skipping default-model ensure (re-run --refresh once the key exists)."
        return 0
    fi
    if ! curl -fsS --max-time 5 "http://127.0.0.1:${GPUSTACK_PORT:-9090}/healthz" >/dev/null 2>&1; then
        print_warning "GPUStack not reachable — skipping default-model ensure."
        return 0
    fi
    export GPUSTACK_API_KEY="$gp_key"

    # Resolve the DEFAULT model aliases from standard-models.yaml `defaults` block
    # (distinct values). This is the single source-of-truth — never a hardcoded
    # list; if the operator repoints a default the check follows automatically.
    # #2193 (journey C's rc4 dry run on 0.79): this read the FLAT defaults
    # block, so a box the upgrade had just migrated to HARDWARE=cpu still asked
    # for the 1M-context fleet default — which was registered and pending
    # forever — and never for the CPU chat default #2158 gives it. The aliases
    # come through the one hardware rule, like every other resolver in this
    # file (_default_chat_alias, _model_rows_for_preset).
    local default_aliases
    default_aliases=$(HW="$(read_env_value "$ENV_FILE" HARDWARE 2>/dev/null || true)" YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os, sys
sys.path.insert(0, os.path.dirname(os.environ['YAML_PATH']))
import hardware_catalog as hc
try:
    spec = yaml.safe_load(open(os.environ['YAML_PATH'])) or {}
except Exception:
    spec = {}
seen = []
models = spec.get('models') or {}
for v in hc.defaults_for(spec, os.environ.get('HW')).values():
    # a default that is a SPARE on this hardware (vision on a cpu box: the
    # fleet model, not always-on there) is not demanded by this gate
    if v and v not in seen and hc.entry_auto_start(models.get(v) or {}, os.environ.get('HW')):
        seen.append(v)
print('\n'.join(seen))
" 2>/dev/null)
    if [ -z "$default_aliases" ]; then
        print_warning "standard-models.yaml defaults block unreadable — skipping default-model ensure."
        return 0
    fi

    # #2193: a default is satisfied only when it is registered AND SERVING — a
    # running instance. "Registered" alone let a row that will never start
    # (qwen3.6 pending forever at ~36 GiB on a 30 GB box, GPUStack's verdict
    # settled within seconds) satisfy the gate, and the upgrade reported
    # success on a box that could not answer a chat request. One model list,
    # one instance list; a registered-but-not-serving default is named with
    # GPUStack's own state and message.
    local prefix serving registered_only
    prefix=$(gpustack_api_prefix)
    serving=$( { gpustack_api GET "${prefix}/models"; echo "=====INSTANCES====="; gpustack_api GET "${prefix}/model-instances"; } | python3 -c "
import sys, json
raw = sys.stdin.read()
models_raw, _, inst_raw = raw.partition('=====INSTANCES=====')
def items(t):
    try:
        return (json.loads(t) or {}).get('items', [])
    except Exception:
        return []
running = {i.get('model_name') for i in items(inst_raw) if str(i.get('state') or '').lower() == 'running'}
for m in items(models_raw):
    n = m.get('name')
    if not n:
        continue
    if n in running:
        print('serving\t' + n)
    else:
        inst = next((i for i in items(inst_raw) if i.get('model_name') == n), None)
        st = (inst or {}).get('state') or ('no instance' if (m.get('replicas') or 0) else '0 replicas')
        msg = ((inst or {}).get('state_message') or '').replace(chr(10), ' ')[:160]
        print('registered\t' + n + '\t' + str(st) + ('\t' + msg if msg else ''))
" 2>/dev/null)
    registered_only=$(printf '%s\n' "$serving" | awk -F'\t' '$1=="registered"')
    serving=$(printf '%s\n' "$serving" | awk -F'\t' '$1=="serving"{print $2}')

    # Which defaults are MISSING from the SERVING set?
    local missing="" a row
    while IFS= read -r a; do
        [ -z "$a" ] && continue
        if ! printf '%s\n' "$serving" | grep -qxF "$a"; then
            missing="${missing:+$missing }$a"
            row=$(printf '%s\n' "$registered_only" | awk -F'\t' -v n="$a" '$2==n{print $3 ($4?" — "$4:"")}' | head -n1)
            if [ -n "$row" ]; then
                print_warning "  Default '$a' is registered but NOT serving (${row}) — a registered row is not a served model (#2193)."
            fi
        fi
    done <<< "$default_aliases"

    if [ -z "$missing" ]; then
        print_success "All default models registered AND serving ($(printf '%s' "$default_aliases" | tr '\n' ' ')) — skipping deploy (customer models preserved)."
        return 0
    fi

    print_info "Default model(s) not serving: ${missing} — deploying the standard preset set (idempotent; already-registered models are skipped)."
    # Reuse the canonical deploy path. deploy_all_models submits the always-on
    # defaults (qwen3.6 + embedding + reranker) FIRST, then registers the
    # auto_start:false spares scaled to 0 (non-blocking). Best-effort — a non-zero
    # return must not abort the surrounding refresh.
    deploy_all_models "standard" \
        || print_warning "deploy_all_models returned non-zero (non-fatal) — check the GPUStack UI / re-run --refresh."

    # Post-upgrade only: we just deployed missing defaults, so wait for the
    # always-on default CHAT model to become READY — otherwise the box is
    # chat-blind for the ~20 min the model takes to download after an upgrade.
    # The skip branch above already returned, so a real customer whose models
    # persisted never reaches here and is never delayed. Only the chat default is
    # waited on (the auto_start:false spares stay non-blocking). Read from
    # defaults.chat — never hardcode. Best-effort: a timeout warns + returns 0
    # (the model finishes in the background), it must not abort the refresh.
    local chat_alias=""
    # #2193: the box's chat default, through the hardware rule. If the shared
    # resolver is unavailable or answers nothing, ask the rule directly (same
    # catalogue, same HARDWARE) rather than skip the wait — the wait is the
    # point, and the flat defaults block is never the answer on a cpu box.
    chat_alias=$(_default_chat_alias 2>/dev/null) || chat_alias=""
    if [ -z "$chat_alias" ]; then
        chat_alias=$(HW="$(read_env_value "$ENV_FILE" HARDWARE 2>/dev/null || true)" YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os, sys
sys.path.insert(0, os.path.dirname(os.environ['YAML_PATH']))
import hardware_catalog as hc
spec = yaml.safe_load(open(os.environ['YAML_PATH'])) or {}
print(hc.defaults_for(spec, os.environ.get('HW')).get('chat') or '')
" 2>/dev/null) || chat_alias=""
    fi
    if [ -n "$chat_alias" ]; then
        local m chat_was_missing=0
        for m in $missing; do [ "$m" = "$chat_alias" ] && chat_was_missing=1; done
        if [ "$chat_was_missing" = "1" ]; then
            wait_for_model "$chat_alias" 2400 \
                || print_warning "Default chat model '$chat_alias' not READY within the wait window (non-fatal) — it will finish downloading in the background."
        fi
    fi
    # #1442: the refresh path deploys into GPUStack above; federate (or re-sync
    # the model list) so the manager fronts what GPUStack serves.
    llm_manager_federate_gpustack || true
}


openuem_report_readiness() {
    # #1075 — OpenUEM readiness report. Non-fatal by design: an opt-in module
    # must never fail post-install. Profile-gated, idempotent, read-only.
    case ",${COMPOSE_PROFILES:-}," in
        *,openuem,*) ;;
        *) return 0 ;;
    esac

    print_step "OpenUEM: verifying PKI bootstrap and console readiness..."

    # 1. The one-shot must have exited 0 — everything else depends on it.
    local certs_exit
    certs_exit=$(docker inspect -f '{{.State.ExitCode}}' openuem-certs 2>/dev/null || echo "missing")
    if [ "$certs_exit" != "0" ]; then
        print_warning "openuem-certs did not complete successfully (exit=${certs_exit})."
        print_info    "  Inspect with: docker compose logs openuem-certs"
        return 0
    fi
    print_success "OpenUEM PKI bootstrap completed."

    # 2. Console health.
    local health
    health=$(docker inspect -f '{{.State.Health.Status}}' openuem-console 2>/dev/null || echo "missing")
    if [ "$health" = "healthy" ]; then
        print_success "OpenUEM console is healthy at https://${OPENUEM_DOMAIN:-openuem.${MAIN_DOMAIN}}"
    else
        print_warning "OpenUEM console health is '${health}' — it may still be starting."
    fi

    # 3. The three things an operator needs and cannot guess.
    print_info "First login: the built-in 'openuem' account's password is printed once by the console."
    print_info "  docker compose logs openuem-console | grep -i password"
    print_info "Agent enrolment material:"
    print_info "  docker cp openuem-certs:/certificates/agents/agent.cer ."
    print_info "  docker cp openuem-certs:/certificates/agents/agent.key ."
    print_info "Admin user certificate (not required for login here):"
    print_info "  docker cp openuem-certs:/certificates/users/admin.pfx ."
    if [ "${OPENUEM_NATS_HOST_BIND:-127.0.0.1}" = "127.0.0.1" ]; then
        print_info "Broker is loopback-bound: agents on OTHER machines cannot reach it yet."
        print_info "  Set OPENUEM_NATS_HOST_BIND to a specific LAN IP (never 0.0.0.0) to serve a fleet."
    fi
    return 0
}


openlit_align_admin() {
    # rc6.7 #44: replace OpenLit's seeded `user@openlit.io` / `openlituser`
    # with the stack admin (razzfazz-ai-admin@<MAIN_DOMAIN>, password =
    # AUTHENTIK_BOOTSTRAP_PASSWORD) so operators don't carry a separate
    # OpenLit credential. OpenLit has no INIT_USER_* env hook upstream;
    # we generate a bcrypt hash via an ephemeral node container, then
    # update the User row through the bundled Prisma client. Idempotent
    # — re-runs no-op if the email already matches.
    if ! docker ps --format '{{.Names}}' | grep -qx openlit; then
        return 0
    fi
    print_step "OpenLit: aligning admin to stack credentials..."
    local admin_email="razzfazz-ai-admin@${MAIN_DOMAIN}"
    local admin_pass="${AUTHENTIK_BOOTSTRAP_PASSWORD:-}"
    if [ -z "$admin_pass" ]; then
        print_warning "AUTHENTIK_BOOTSTRAP_PASSWORD empty — skipping OpenLit admin alignment."
        return 0
    fi
    # Skip if already aligned.
    local current_email
    current_email=$(docker exec openlit sh -c 'cd /app/client && node -e "
const { PrismaClient } = require(\"@prisma/client\");
const p = new PrismaClient();
p.user.findFirst().then(u => { console.log(u ? u.email : \"\"); process.exit(0); });
"' 2>/dev/null | tail -1)
    if [ "$current_email" = "$admin_email" ]; then
        print_info "OpenLit admin already aligned ($admin_email)."
        return 0
    fi
    # Generate bcrypt hash via ephemeral node container.
    local hash
    hash=$(docker run --rm -e PW="$admin_pass" node:20-alpine sh -c \
        'cd /tmp && npm install --silent bcryptjs >/dev/null 2>&1 && node -e "console.log(require(\"bcryptjs\").hashSync(process.env.PW,10))"' 2>/dev/null)
    if [ -z "$hash" ]; then
        print_warning "Failed to generate bcrypt hash — skipping OpenLit admin alignment."
        return 0
    fi
    if docker exec -e EMAIL="$admin_email" -e HASH="$hash" openlit sh -c 'cd /app/client && node -e "
const { PrismaClient } = require(\"@prisma/client\");
const p = new PrismaClient();
p.user.updateMany({ data:{ email: process.env.EMAIL, password: process.env.HASH, name: \"razzfazz-ai-admin\" }}).then(r => { console.log(\"updated\", r.count, \"row(s)\"); process.exit(0); }).catch(e => { console.error(e.message); process.exit(1); });
"' 2>&1 | tail -1; then
        print_success "OpenLit admin aligned ($admin_email)."
    else
        print_warning "OpenLit admin alignment failed (non-fatal)."
    fi
}


refresh_help_cache() {
    # rc6.7 #15: warm the help docs cache for any uncached app. The
    # razzfazz-help container's autowarm thread (rc6.7 #14) does the
    # same on container start, but operators upgrading without recreating
    # the help container won't trigger it. Calling mirror_all from outside
    # via docker exec is idempotent (CacheManager.mirror_docs short-
    # circuits already-cached apps).
    print_step "Warming help documentation cache (background)..."
    if ! docker ps --format '{{.Names}}' | grep -qx razzfazz-help; then
        print_warning "razzfazz-help not running — skipping help cache warm."
        return 0
    fi
    # #210: an offline / air-gapped box cannot reach the upstream doc sites, so
    # the per-app `wget --mirror` would only burn 3×30s timeouts each with nothing
    # to show for it. Skip the external warm in offline mode — the Help-UI serves
    # the already-cached / community-bundled docs.
    if [ "$(read_env_value "$ENV_FILE" RAZZFAZZ_NETWORK_MODE)" = "offline" ]; then
        print_info "Offline network mode — skipping external help-doc mirror (serving cached docs only)."
        return 0
    fi
    # #210: warm the cache in the BACKGROUND (docker exec -d — same pattern as the
    # /api/refresh-all trigger elsewhere) so it NEVER blocks the upgrade. This
    # mirror walks ~30 upstream doc sites via `wget --mirror`; running it on the
    # upgrade's critical path took 30–60 min and made upgrades look hung. The
    # razzfazz-help container's own autowarm thread does the same on (re)start;
    # this detached trigger covers the no-recreate case. mirror_docs still
    # short-circuits already-cached apps. Per-app progress → razzfazz-help logs.
    #
    # #1284: AS appuser. `docker exec` runs as the container's default user —
    # root — while the app itself is dropped to appuser by the entrypoint. A
    # warm started here mirrored as root and left root-owned trees under
    # /data/cache for exactly the apps that were not cached yet; every later
    # refresh by the app (appuser) then died with "Cannot write to
    # /data/cache/<app>/index.html (Permission denied)" — a permanent `failed`
    # in cache-status until the next container start's chown (0.91, openlit,
    # 2026-09-05). The user exists in the image (core/help/Dockerfile).
    if docker exec -d --user appuser razzfazz-help python3 -c "
import sys; sys.path.insert(0, '/app')
from cache_manager import CacheManager
cm = CacheManager()
for entry in cm.get_config().get('apps', []):
    aid = entry.get('id')
    if not aid: continue
    if cm.get_app_cache_status(aid).get('cached'): continue
    try: cm.mirror_docs(aid)
    except Exception: pass
" 2>/dev/null; then
        print_success "Help cache warm triggered in background (progress in razzfazz-help logs)."
    else
        print_warning "Help cache warm trigger failed (non-fatal)."
    fi
}


if [ "$DO_REFRESH" = true ]; then
    if [ -n "$PRESET" ]; then
        print_error "--refresh is mutually exclusive with --preset."
        exit 1
    fi
    print_step "Refresh mode (idempotent post-upgrade re-config)"
    if [ "$SKIP_DNS" = false ]; then
        setup_local_dns
    fi
    # #1442 rev-B (review): federate BEFORE the wiring block. The wiring writes
    # the endpoint list from llm_manager_owns_standard_set, so on the run that
    # flips the box the list would still be written GPUStack-first and only the
    # NEXT run would converge. Registration needs a running manager + running
    # GPUStack, nothing from this run's deploy — and it must not hang off
    # --skip-models (the post-deploy calls below only re-sync the model list).
    llm_manager_federate_gpustack || true
    # #320: consumer wiring for the llm-manager line (idempotent; no-op
    # without the profile). Runs early so a later failure in the gpustack
    # block cannot skip it.
    wire_llm_manager_consumers || true
    wire_llm_manager_rag_consumers || true
    # #240: same idempotent shape for the Mac gateway (mac-llm profile).
    wire_mac_llm_consumers || true
    # #813: the Dify half of the same wiring — its OWN function, because the
    # OWUI half above early-returns on an empty master key (#690).
    wire_mac_llm_dify_consumer || true
    wire_observability_consumers || true
    if curl -fsS --max-time 5 "http://127.0.0.1:${GPUSTACK_PORT:-9090}/healthz" >/dev/null 2>&1; then
        ensure_gpustack_api_key
    else
        print_warning "GPUStack not reachable on http://127.0.0.1:${GPUSTACK_PORT:-9090} — skipping API-key refresh."
        print_info "  Re-run --refresh once GPUStack is up, or set the key manually with:"
        print_info "    rzfz setup --set-gpustack-api-key <KEY>"
    fi
    refresh_llama_vulkan_runner
    push_runner_images_to_registry
    # ga.1 (Issue A): also pre-build all custom-build images on --refresh (the
    # documented post-upgrade path) so later module-enable stays offline-safe.
    prebuild_all_custom_images
    prepull_agent_manager_images
    # ga.1 (upgrade day-1-green): apply the OWUI-key / model-sync / Dify-pgvector /
    # coding-agent wiring self-heal on --refresh too. Without this, an upgraded box
    # keeps its init-time placeholder GPUStack key in OWUI's persisted config (0
    # models), an un-recreated model-sync (writes 0 models) and placeholder-keyed
    # coding agents — the exact day-1 failure cluster seen after an upgrade. Runs
    # after ensure_gpustack_api_key above wrote the live key to .env; idempotent.
    refresh_apply_day1_wiring_fixes
    # ga.1 (#147 follow-up): after the wiring self-heal, make sure the standard
    # preset's DEFAULT models are DEPLOYED — an upgraded box whose gpustack came up
    # with 0 registered models (degraded baseline) otherwise self-heals its wiring
    # but still lists 0 models and fails the AFTER-upgrade day-1 suite. Idempotent:
    # deploys only MISSING defaults, SKIPS entirely when a real customer's models
    # already persist (no re-download / reset). Profile-gated + best-effort.
    # #187: gated on --skip-models — deploying/ensuring models here would re-trigger
    # the untimed download-and-wait path the flag exists to avoid.
    if [ "${SKIP_MODELS:-false}" = false ]; then
        refresh_ensure_default_models_deployed
    else
        print_substep "Skipping default-model ensure (--skip-models)."
    fi
    # RZFZAI-1337: the --refresh path (what `rzfz upgrade` calls) previously
    # NEVER ran Dify initialisation — only the --preset path reached Step 6
    # (step_dify_provisioning). So an upgraded box, or one where the `dify`
    # profile was enabled AFTER the original install, was left with an
    # uninitialised Dify console (stuck on /install) and no GPUStack model
    # provider / example KB. Run the SAME idempotent provisioning step here,
    # gated on the dify profile and best-effort so it can never abort the
    # refresh. Idempotent: dify_ensure_admin skips setup when already
    # "finished", plugin installs skip-if-present, model config is idempotent,
    # and seed-apps.sh is flag-guarded. Ordered AFTER refresh_apply_day1_wiring_fixes
    # (which force-recreates dify-api for the pgvector password) and after the
    # default models are deployed, so Dify's model-provider config sees them.
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "dify"; then
        # #1425: RECORD the outcome here too. Before this the refresh path
        # swallowed a failed Dify provisioning entirely — no status, so the
        # summary rendered SKIP ("never reached") for a step that had just
        # failed, and nothing could reach the exit contract.
        if step_dify_provisioning; then
            record_step_status dify OK
        else
            # #2195: 3 is "the models were not served yet", not "this box is
            # broken". `dify` is a CRITICAL step, so FAIL here exits 1.
            _dify_rc=$?
            if [ "$_dify_rc" -eq 3 ]; then
                record_step_status dify WARN
            else
                record_step_status dify FAIL
                print_warning "Dify init on --refresh reported an issue — re-run 'rzfz post-install --refresh' or check the Dify console."
            fi
        fi
    fi
    # #1507: same late re-wire as the --preset arm, placed after the LAST step
    # of this arm that enumerates the served set (Dify's provisioning; OWUI's
    # model-sync ran even earlier, in refresh_apply_day1_wiring_fixes). Fires
    # only when the deploy above set the marker.
    llm_manager_reconcile_consumers_late || true
    # #258: the --refresh path never touched OWUI's retrieval config. Same shape
    # as the Dify gap above (RZFZAI-1337): the reconcile existed, but only on the
    # --preset path, so the command an operator actually reaches for silently did
    # nothing for RAG.
    #
    # That matters because the config does not merely go missing at install time,
    # it gets DESTROYED later: OWUI's /admin/settings/web Save posts the whole
    # retrieval config without the reranker field and the server REPLACES rather
    # than merges, so `RAG_RERANKING_MODEL` becomes null. PersistentConfig never
    # re-seeds from env once the DB key exists, so nothing brings it back. With
    # hybrid search on and no reranker, web search silently stops running and the
    # model answers from memory — no sources, no error, a plausible answer.
    #
    # Profile-gated, best-effort, idempotent (GET -> merge -> POST), exactly like
    # the Dify block: a failure here must never abort a refresh.
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "chat"; then
        if wait_for_service "Open WebUI" "http://127.0.0.1:${OPENWEBUI_PORT:-8080}/health" 60 2>/dev/null \
           && owui_ensure_admin >/dev/null 2>&1; then
            # #976: pair the EMBEDDING reconcile with the retrieval one. On a box
            # whose LLM backend changed (e.g. gpustack → llm-manager on an
            # upgrade) the embedding endpoint must be re-resolved too, else RAG
            # embedding keeps hitting the old backend. Same #258 rationale that
            # put owui_configure_retrieval_env on the --refresh path.
            owui_configure_embedding \
                || print_warning "Embedding reconcile reported an issue (non-fatal) — check Chat > Admin > Settings > Documents."
            owui_register_pipelines_connection \
                || print_warning "Pipelines registration reported an issue (non-fatal, #1401) — chat attribution (openlit_filter) may stay inert."
            owui_configure_retrieval_env \
                || print_warning "Retrieval/RAG reconcile reported an issue (non-fatal) — check Chat > Admin > Settings > Documents."
            # #1266: the model ROWS too. --refresh is the path `rzfz upgrade`
            # takes and is mutually exclusive with --preset, so without this an
            # upgraded Manager box keeps an empty `model` table: no
            # capabilities, and the embedder/reranker in the chat picker. Same
            # idempotent reconcile the --preset path runs (a no-op when the
            # rows already match); it reports its own reason for skipping.
            if llm_manager_owns_standard_set; then   # #1441
                owui_reconcile_model_rows || true
            fi
        else
            print_warning "Open WebUI not reachable — retrieval/RAG reconcile skipped (#258)."
            print_info    "  Re-run 'rzfz post-install --refresh' once chat is up."
        fi
    fi
    refresh_help_cache
    openlit_align_admin
    # #1075: report OpenUEM readiness on --refresh too, so a box that enables the
    # opt-in module after install still gets the enrolment/first-login pointers.
    openuem_report_readiness
    # ga.6: provision MCP registry on --refresh too (was --preset-only — upgraded
    # boxes following the documented post-upgrade --refresh flow otherwise ended up
    # with an empty COGNEE_MCP_API_KEY + a non-functional cognee-mcp sidecar).
    # Profile-gated (no-op without cognee) + idempotent (skips if key already set).
    provision_mcp_registry
    ensure_mcp_manager_secret
    reconcile_authentik_bindings
    provision_wazuh_agent     || print_warning "Wazuh agent auto-enrol reported an issue - see verify report / docs/enterprise/how-to/wazuh.md."
    provision_openuem_admin   || print_warning "OpenUEM admin seed reported an issue - see 'docker logs postgres' / #1907."
    provision_openuem_smtp    || print_warning "OpenUEM SMTP seed reported an issue - see 'docker logs postgres' / #1992."
    # §16 handover: refresh the as-built day-1 security-posture assessment too, so
    # an upgraded box carries a current report (matches the --preset path).
    run_security_selfcheck
    print_success "Refresh complete."
    if [ "$DO_VERIFY" = false ]; then
        # #1250a rev-B: this early exit is the path `rzfz upgrade` takes and
        # the one the enrol failure message recommends retrying with. Without
        # this call a failed enrolment printed "Refresh complete." and exited
        # 0 — the same silent success the fix is about.
        _exit_on_failed_provisioning
        exit 0
    fi
fi

if [ -n "$PRESET" ] && [ "$DO_FORCE" != true ]; then
    if detect_existing_state; then
        echo ""
        print_error "Refusing to run --preset on a previously-initialised stack without --force."
        print_info "  Safe alternatives:"
        print_info "    --refresh    Idempotent: refresh /etc/hosts + GPUSTACK_API_KEY only"
        print_info "    --verify     Read-only verification suite"
        print_info "  If you really want to overwrite operator state with the preset defaults,"
        print_info "  re-run with --force."
        exit 1
    fi
fi

if [ -n "$PRESET" ]; then
    # #1802: the OIDC CA superset belongs on BOTH paths, and it runs FIRST here
    # for the same reason it runs first in refresh_apply_day1_wiring_fixes — every
    # container recreated below bind-mounts certs/caddy-ca.pem, and Docker
    # materialises a missing source as a root-owned DIRECTORY (#1080). Once that
    # has happened, SSL_CERT_FILE and REQUESTS_CA_BUNDLE inside Open WebUI point
    # at a directory and the app verifies NO TLS at all — measured on 0.79
    # (2026-09-08): `CERTIFICATE_VERIFY_FAILED — unable to get local issuer
    # certificate` against its own auth.<domain>.
    #
    # Until now this step lived only in the --refresh path, so a box provisioned
    # with `--preset` never rebuilt the bundle and never repaired that directory,
    # while `rzfz status` and the day1 probe both told the operator to run
    # `--refresh` — the SMALLER command. Idempotent, self-diagnosing, cheap.
    # The generalised repair FIRST (#1595), then the bundle it protects. This is
    # the order cli/init.sh uses, and the reason is the same: post-install
    # recreates containers, which is exactly the moment a missing bind source
    # materialises as a directory. `repair_empty_dir_bind_sources` is called
    # today by init.sh and upgrade.sh only — post-install called neither.
    repair_empty_dir_bind_sources || true
    ensure_oidc_ca_superset

    # Step 1: DNS
    if [ "$SKIP_DNS" = false ]; then
        setup_local_dns
    fi

    # #1442 rev-C (review N2): NO pre-wiring federation on the --preset path.
    # A fresh box has no models in GPUStack yet (deploy_all_models runs much
    # later), so the call could only print "nothing to federate — re-run
    # --refresh" at a point where the very same run federates in Step 3b. The
    # refresh path DOES pre-federate, because there the models already exist.
    # #320/#976: consumer wiring for the llm-manager line runs FIRST — a
    # gpustack-disabled box has no gpustack to wait for, and the wiring must
    # never be skipped by a failure in the gpustack block below (matches the
    # --refresh path, which already wires before gating gpustack). Idempotent;
    # no-op without the profile.
    wire_llm_manager_consumers || true
    wire_llm_manager_rag_consumers || true
    # #240: same idempotent shape for the Mac gateway (mac-llm profile).
    wire_mac_llm_consumers || true
    # #813: the Dify half of the same wiring — its OWN function, because the
    # OWUI half above early-returns on an empty master key (#690).
    wire_mac_llm_dify_consumer || true
    wire_observability_consumers || true

    # Step 2: GPUStack API key — only when a GPUStack profile is actually
    # active. On an llm-manager-only box (gpustack disabled) there is nothing
    # to wait for; hard-waiting here aborted the ENTIRE --preset provision with
    # "GPUStack did not become ready within 120s" (#976), so the RAG-config and
    # wiring steps never ran. The LLM Manager is the backend in that case.
    if _gpustack_profile_active; then
        wait_for_service "GPUStack" "http://127.0.0.1:${GPUSTACK_PORT:-9090}/healthz" 120
        ensure_gpustack_api_key
    else
        print_substep "No GPUStack profile active — skipping GPUStack API key (the LLM Manager is the LLM backend). (#976)"
    fi


    # #1373: build the llama-* runner images BEFORE the first deploy. Until now
    # only `--refresh` built them; a box init'd without Step 7b (the 2026.09
    # manager profile set) failed its first --preset deploy on a missing
    # `llama-vulkan-runner:b9851`. Self-gated: offline / consumer profile /
    # hardware / already present.
    if [ "$SKIP_MODELS" = false ]; then
        refresh_llama_vulkan_runner
        push_runner_images_to_registry
    fi

    # Step 3: Deploy Models — the GPUStack model set. Only on a box that
    # actually runs GPUStack; on an llm-manager-only box the models are served
    # by the manager's own workers (deployed via the LLM Manager console /
    # deploy API, not this gpustack path), so skip cleanly instead of failing
    # against a gpustack that isn't running. (#976)
    # #1441/#1442: the arm is chosen by llm_manager_deploys_standard_set (manager
    # profile on, no GPUStack profile) — GPUStack deploys its own set and, once
    # federated, the manager fronts it (llm_manager_owns_standard_set).
    if [ "$SKIP_MODELS" = false ] && llm_manager_deploys_standard_set; then
        # #1250b: "the manager's workers serve the models" was only ever half
        # true — nothing deployed them. #976 removed the GPUStack deploy and put
        # NOTHING in its place, so a clean Manager install ended with .env
        # pointing every consumer at qwen3.6/qwen3-embedding while
        # /api/deployments was []. Deploy the same manifest set through the
        # manager instead (idempotent; the GPUStack branch below is untouched).
        if _llm_manager_running; then
            # The summary row must name the backend this box actually used.
            LLM_BACKEND_SUMMARY_LABEL="LLM Manager — models deployed on the box's workers"
            if [ "$SKIP_WAIT" = true ]; then _llmm_do_wait=false; else _llmm_do_wait=true; fi
            if llm_manager_deploy_standard_set "$PRESET" "$_llmm_do_wait"; then
                # #2051: the LLM MANAGER succeeded — record it under its OWN
                # key. This branch used to write `gpustack`, so the exit
                # contract named a module the box may not even run.
                record_step_status llm-manager OK
                # #2195: a deploy that never waited succeeded at DEPLOYING and
                # at nothing else — every step below that enumerates the served
                # set is about to read an incomplete one.
                _llmm_mark_incomplete_if_auto_skip_wait "$_llmm_do_wait"
            else
                # #2051: the LLM MANAGER failed. Recorded under its own key so
                # `_exit_on_failed_provisioning` names the thing that actually
                # failed — measured on 0.175: the summary row already said
                # "[FAIL] LLM Manager — models deployed on the box's workers"
                # while the exit line two lines later said "gpustack, dify".
                record_step_status llm-manager FAIL
                # #1507: the deploy did not reach "all ready". Everything that
                # ENUMERATES the served set afterwards (Dify's provider + its
                # defaults, OWUI's model-sync) would then persist an incomplete
                # list — and nothing re-reads it when the pull finishes minutes
                # later. Measured on 0.175 (Round 7, CPU box): `3/4 ready after
                # 540s — waiting on: qwen3.6(pulling)`, and twenty minutes later
                # /api/deployments showed all four active while OWUI knew
                # ['granite-docling','qwen3-embedding','qwen3-reranker'] and Dify
                # had NO default model at all. Remembered here, acted on after
                # the consumer steps.
                _LLMM_DEPLOY_INCOMPLETE=1
            fi
            # #1263: same report/apply as the --refresh arm.
            llm_manager_manifest_drift_report "$PRESET" || true
            if [ "${DO_RECONCILE_MODELS:-false}" = true ]; then
                llm_manager_reconcile_standard_set "$PRESET" \
                    || print_warning "LLM Manager reconcile reported an issue (non-fatal) — see above."
            fi
        else
            # rev-B (8): without the label this FAIL rendered as
            # "[FAIL] GPUStack — API key + models deployed" on a box that has no
            # GPUStack at all. rev-B (1): the deploy lives in the --preset arm,
            # and --preset is mutually exclusive with --refresh, so the hint has
            # to name the command that actually deploys.
            LLM_BACKEND_SUMMARY_LABEL="LLM Manager — models deployed on the box's workers"
            print_warning "llm-manager profile active but the container is not running — no models deployed. Start the stack and re-run 'rzfz post-install --preset standard'. (#1250b)"
            # #2051: same key split — this is the manager's failure, not GPUStack's.
            record_step_status llm-manager FAIL
        fi
    elif [ "$SKIP_MODELS" = false ] && ! _gpustack_profile_active; then
        print_substep "No LLM backend profile active — skipping model deploy. (#976)"
    elif [ "$SKIP_MODELS" = false ]; then
        deploy_all_models "$PRESET"

        if [ "$SKIP_WAIT" = false ]; then
            # Never let a model-wait failure abort the rest of provisioning.
            # wait_for_all_models already loudly reports critical vs optional
            # failures; downstream service config (OWUI / Dify / Speaches) and
            # the verify suite must still run so the operator gets a fully
            # configured box + an accurate report instead of a half-provisioned
            # one. (Under `set -e` a bare call here aborted everything when a
            # single optional model — e.g. the reranker — failed to start.)
            # #178(c): the `if` (not a bare `||`) is what lets us capture the
            # REAL exit status for the end-of-run summary without changing the
            # non-abort behaviour above — a critical model that never came up
            # is a genuine FAIL, not a silent ✓.
            if wait_for_all_models "$PRESET"; then
                record_step_status gpustack OK
            else
                # #178 review (LOW): rc=2 means "optional model(s) failed,
                # critical models are fine" — record WARN, not FAIL, so the
                # summary line doesn't over-report a degraded-but-usable box
                # as broken.
                _gpustack_rc=$?
                if [ "$_gpustack_rc" -eq 2 ]; then
                    record_step_status gpustack WARN
                else
                    record_step_status gpustack FAIL
                fi
                # #1507 review finding 3: after C1/C2 this is the FEDERATED
                # shape — GPUStack serves, the manager fronts — and a slow
                # model leaves the same stale enumeration behind. The late
                # reconcile asks the manager; when the manager's deployment
                # list carries none of the preset's models (a plain GPUStack
                # box), it says so and returns instead of waiting.
                _LLMM_DEPLOY_INCOMPLETE=1
            fi
            # #429: background-prefetch the on-demand spares so the box is
            # genuinely complete (and offline-switchable) without blocking.
            prefetch_spare_ggufs "$PRESET" || true
        else
            print_info "Skipping model wait (--skip-wait). Models downloading in background."
            # Key + deploy both succeeded to get here; the wait was skipped
            # deliberately, not because anything failed.
            record_step_status gpustack OK
        fi
    fi
    
    # Step 3b (#1442, cutover C2): a box that just deployed its models in
    # GPUStack registers GPUStack as the manager's external backend — from here
    # on llm_manager_owns_standard_set is true and every consumer below wires
    # to the manager, which fronts GPUStack.
    llm_manager_federate_gpustack || true

    # Step 4: Update .env with model names
    update_env_model_config "$PRESET"

    # Step 4b (M031 S3): reconcile every gpustack-consuming service to the
    # YAML — Open WebUI per-model num_ctx, Dify provider rows, Cognee env,
    # moltis [models.<id>].context_window, hermes config set
    # model.context_length. sync.py is idempotent, so it's safe to re-run
    # on subsequent post-install invocations and after operator-side YAML
    # edits.
    #
    # ga.1 (project_postinstall_set_e_abort_pattern): these four reconcile steps
    # run under `set -eo pipefail`. Each ends on an `if [ … ]` / pipeline whose
    # exit status can be non-zero even on a fully-successful run (e.g.
    # ensure_mcp_manager_secret's trailing `if [ -z "$(read_env_value …)" ]`
    # returns 1 when the last secret is ALREADY set) — which would abort
    # post-install RIGHT HERE, before Step 8 recreates model-sync with the live
    # GPUStack key + rewrites OWUI's persisted placeholder key. That is exactly
    # the "OWUI shows 0 models" class. Guard every one with `|| print_warning` so
    # a non-zero return can NEVER short-circuit the run before Step 8. Each
    # function already logs its own real progress/warnings.
    sync_consumers            || print_warning "consumer reconciliation reported an issue — see verify report below."
    provision_mcp_registry    || print_warning "MCP registry provisioning reported an issue — see verify report below."
    ensure_mcp_manager_secret || print_warning "MCP-manager secret provisioning reported an issue — see verify report below."
    reconcile_authentik_bindings || print_warning "Authentik binding reconcile reported an issue — see verify report below."
    provision_wazuh_agent     || print_warning "Wazuh agent auto-enrol reported an issue — see verify report / docs/enterprise/how-to/wazuh.md."
    provision_openuem_admin   || print_warning "OpenUEM admin seed reported an issue — see 'docker logs postgres' / #1907."
    provision_openuem_smtp    || print_warning "OpenUEM SMTP seed reported an issue — see 'docker logs postgres' / #1992."

    # Steps 5-7 are best-effort: a failure inside any single service-provisioning
    # step (a flaky marketplace plugin, an unreachable model download, a service
    # still warming up) must NOT abort the remaining steps or the final verify
    # suite under `set -e`. Each step already logs its own progress/warnings; the
    # verify suite at the end reports whatever didn't come up so the operator gets
    # an accurate, complete picture instead of a half-provisioned box.

    # Step 5: Open WebUI provisioning
    # #178(c): `if/else` (not a bare `||`) so the real exit status feeds the
    # end-of-run summary — behaviour is unchanged, a failure here still only
    # warns and never aborts the remaining steps.
    if step_openwebui_provisioning; then
        record_step_status openwebui OK
    else
        record_step_status openwebui FAIL
        print_warning "Open WebUI provisioning reported an issue — see verify report below."
    fi

    # Step 6: Dify provisioning
    if step_dify_provisioning; then
        record_step_status dify OK
    else
        # #2195: same split as the --refresh arm above. Mirrors the speaches
        # rc=2 dispatch two blocks down.
        _dify_rc=$?
        if [ "$_dify_rc" -eq 3 ]; then
            record_step_status dify WARN
        else
            record_step_status dify FAIL
            print_warning "Dify provisioning reported an issue — see verify report below."
        fi
    fi

    # Step 7: Speaches + auxiliary
    # #178 review: rc=2 means "Speaches is up but an optional model download
    # failed" — record WARN, not FAIL/OK, mirroring the gpustack dispatch above.
    if step_speaches_provisioning; then
        record_step_status speaches OK
    else
        _speaches_rc=$?
        if [ "$_speaches_rc" -eq 2 ]; then
            record_step_status speaches WARN
            print_warning "Speaches provisioning completed with warnings (an optional model download failed) — see above."
        else
            record_step_status speaches FAIL
            print_warning "Speaches provisioning reported an issue — see verify report below."
        fi
    fi
    
    # Step 7: Gitea Configuration
    step_gitea_provisioning() {
        if echo "${COMPOSE_PROFILES:-}" | grep -q "gitea"; then
            print_step "Gitea: Provisioning..."
            
            local gitea_url="http://127.0.0.1:${GITEA_HTTP_PORT:-3000}"
            print_substep "Waiting for Gitea to be ready (${gitea_url}/api/healthz)..."
            
            local g_waited=0
            while [ $g_waited -lt 60 ]; do
                if curl -sf --max-time 5 "${gitea_url}/api/healthz" > /dev/null 2>&1; then
                    break
                fi
                sleep 5
                g_waited=$((g_waited + 5))
            done
            
            if [ $g_waited -ge 60 ]; then
                print_error "Gitea did not become ready."
            else
                print_success "Gitea is ready."
                print_substep "Configuring Gitea admin user..."
                
                local gitea_admin="${GITEA_ADMIN_USER:-admin}"
                local gitea_pass="${AUTHENTIK_BOOTSTRAP_PASSWORD}"
                local gitea_email="razzfazz-ai-admin@${MAIN_DOMAIN}"
                
                # Use docker exec to check if user exists
                if docker exec gitea su-exec git gitea admin user list 2>/dev/null | grep -iq "${gitea_admin}"; then
                    # User exists, update password
                    docker exec -u root gitea su-exec git gitea admin user change-password \
                        --username "${gitea_admin}" \
                        --password "${gitea_pass}" > /dev/null 2>&1 || true

                    # Update email in DB
                    docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${GITEA_DB:-gitea_db}" -c \
                        "UPDATE \"user\" SET email='${gitea_email}' WHERE lower_name=lower('${gitea_admin}');" > /dev/null 2>&1 || true

                    print_info "Gitea admin user updated (email & password)."
                else
                    docker exec -u root gitea su-exec git gitea admin user create \
                        --admin \
                        --username "${gitea_admin}" \
                        --password "${gitea_pass}" \
                        --email "${gitea_email}" \
                        --must-change-password=false > /dev/null 2>&1 || true
                    print_success "Gitea admin user created."
                fi

                # Always re-assert: admin password is the stack admin password,
                # period — no first-login prompt. The `change-password` CLI
                # path above doesn't touch this flag, and an admin originally
                # created via the gitea web installer (legacy installs) carries
                # `must_change_password=true` indefinitely. The
                # `gitea admin user must-change-password` CLI only supports
                # `--all` (the docs imply a positional username works, but
                # gitea silently ignores it and reports "Updated 0 users") —
                # so we toggle the flag directly via SQL, scoped to this user.
                docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${GITEA_DB:-gitea_db}" -c \
                    "UPDATE \"user\" SET must_change_password=false WHERE lower_name=lower('${gitea_admin}');" \
                    > /dev/null 2>&1 || true
                print_success "Gitea provisioning complete."
            fi
        fi
    }
    step_gitea_provisioning

    # Step 8a: Onyx provisioning
    step_onyx_provisioning() {
        if ! echo "${COMPOSE_PROFILES:-}" | grep -qw "onyx"; then
            return 0
        fi
        print_step "Onyx: Provisioning..."

        local onyx_api_url="http://127.0.0.1:$(docker inspect onyx-api \
            --format '{{range $p,$conf := .NetworkSettings.Ports}}{{if $conf}}{{(index $conf 0).HostPort}}{{end}}{{end}}' 2>/dev/null || echo '')"

        # Wait for onyx-api to be ready — asking exactly what its own
        # healthcheck asks (#1786).
        #
        # This probe used to differ from `modules/apps/onyx/compose.yml`'s
        # healthcheck in TWO ways, and both can make it time out against a
        # container docker already calls healthy (measured on 0.91,
        # 2026-09-09: healthcheck green after 40 s, this loop still timing out
        # at 120 s):
        #
        #   path   `/api/health` here vs `/health` in the healthcheck
        #   proxy  plain urlopen() here — which HONOURS http_proxy from the
        #          container env — vs `build_opener(ProxyHandler({}))` there,
        #          which is exactly why the healthcheck bypasses it. On a box
        #          with a corporate proxy (#181) a plain urlopen of
        #          `localhost` goes to the proxy and fails.
        #
        # So: same URL, same proxy-free opener. The healthcheck is the
        # authority on "is this API up"; a second, differently-worded opinion
        # is how the two drift apart.
        local waited=0
        while [ $waited -lt 120 ]; do
            if docker exec onyx-api python3 -c \
                "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://localhost:8080/health', timeout=3)" \
                > /dev/null 2>&1; then
                break
            fi
            sleep 5; waited=$((waited + 5))
        done
        if [ $waited -ge 120 ]; then
            # #1786/#1805: NOT a silent skip. Onyx then keeps its bundled
            # default embedding (nomic-ai/nomic-embed-text-v1, 768 dim) while
            # this box serves qwen3-embedding at 4096 — every Onyx search runs
            # against a model the box does not have, and nothing says so. The
            # step still returns 0 (post-install must not stop for one module),
            # but it says what it left behind and how to finish it.
            print_error "Onyx API did not answer /health inside 120s — provisioning SKIPPED."
            print_info  "  Onyx keeps its BUNDLED default embedding model, not this box's"
            print_info  "  ($(_default_embedding_alias)). Its search index is then built"
            print_info  "  against a model the box does not serve."
            print_info  "  Docker health of onyx-api: $(docker inspect --format '{{.State.Health.Status}}' onyx-api 2>/dev/null || echo unknown)"
            print_info  "  Re-run once it is up:  rzfz post-install --refresh"
            record_step_status onyx WARN
            return 0
        fi
        print_substep "Onyx API ready, configuring..."

        # Write provisioning script into the container and run it
        docker exec -i onyx-api bash -c 'cat > /tmp/onyx_post_install.py' << 'PYEOF'
import urllib.request, urllib.parse, json, http.cookiejar, sys, os
try:
    import psycopg2
except ImportError:
    psycopg2 = None

BASE = "http://localhost:8080"
EMAIL = "razzfazz-ai-admin@" + os.environ["MAIN_DOMAIN"]
PASSWORD = os.environ["AUTHENTIK_BOOTSTRAP_PASSWORD"]
GPUSTACK_KEY = os.environ["GPUSTACK_API_KEY"]
GPUSTACK_BASE = "http://llm:8080/v1"   # #1445: canonical endpoint (the app's variable name stays)
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "docker")
POSTGRES_PASS = os.environ.get("POSTGRES_PASSWORD", "")
POSTGRES_DB   = os.environ.get("POSTGRES_DB", "onyx_db")
DEFAULT_CHAT  = os.environ.get("DEFAULT_CHAT", "qwen3.6")
DEFAULT_EMBED = os.environ.get("DEFAULT_EMBED", "qwen3-embedding")
_dim_raw = (os.environ.get("DEFAULT_EMBED_DIM") or "").strip()
DEFAULT_EMBED_DIM = int(_dim_raw) if _dim_raw.isdigit() else None

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def req(method, path, data=None, params=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Cookie": "; ".join(f"{c.name}={c.value}" for c in cj)}
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(r, timeout=30) as resp:
            raw = resp.read(); return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: rb = json.loads(raw)
        except: rb = raw.decode()[:300]
        return e.code, rb

def db_exec(sql, params=()):
    if not psycopg2: return
    conn = psycopg2.connect(host=POSTGRES_HOST, user=POSTGRES_USER,
                            password=POSTGRES_PASS, dbname=POSTGRES_DB)
    cur = conn.cursor(); cur.execute(sql, params); conn.commit()
    result = cur.fetchall() if cur.description else []
    conn.close(); return result

# 1. Ensure admin user exists and is superuser
login_data = urllib.parse.urlencode({"username": EMAIL, "password": PASSWORD}).encode()
try:
    opener.open(urllib.request.Request(BASE+"/auth/login", data=login_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"))
except Exception:
    # Register first
    req("POST", "/auth/register", {"email": EMAIL, "password": PASSWORD})
    opener.open(urllib.request.Request(BASE+"/auth/login", data=login_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"))

s, me = req("GET", "/me")
if not me.get("is_superuser"):
    db_exec("UPDATE public.user SET is_superuser=true WHERE email=%s", (EMAIL,))
    print("  promoted to superuser")
    opener.open(urllib.request.Request(BASE+"/auth/login", data=login_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"))

# 2. LLM Provider
s, existing = req("GET", "/admin/llm/provider")
providers = existing.get("providers", []) if isinstance(existing, dict) else []
gpustack = next((p for p in providers if p.get("name") == "GPUStack"), None)
# The always-on default (qwen3.6, multimodal) is the primary model + system
# default. gemma4 / qwen3-coder-next are auto_start:false (0 replicas) — listed
# as optional/selectable but never the default (selecting one while stopped
# errors until the operator scales it up).
model_cfgs = [
    {"name": DEFAULT_CHAT, "is_visible": True, "max_input_tokens": None,
     "supports_image_input": True, "display_name": "Qwen3.6 (default)"},
    {"name": "gemma4", "is_visible": True, "max_input_tokens": None,
     "supports_image_input": False, "display_name": "Gemma 4 (optional)"},
    {"name": "qwen3-coder-next", "is_visible": True, "max_input_tokens": None,
     "supports_image_input": False, "display_name": "Qwen3 Coder Next (optional)"},
]
if gpustack:
    payload = {**gpustack, "api_key": GPUSTACK_KEY, "api_base": GPUSTACK_BASE,
               "model_configurations": model_cfgs}
    s, r = req("PUT", "/admin/llm/provider", payload, {"is_creation": "false"})
    provider_id = gpustack["id"]
else:
    s, r = req("PUT", "/admin/llm/provider", {
        "name": "GPUStack", "provider": "openai", "api_key": GPUSTACK_KEY,
        "api_base": GPUSTACK_BASE, "api_version": None, "custom_config": {},
        "is_public": True, "is_auto_mode": False, "groups": [], "personas": [],
        "deployment_name": None, "model_configurations": model_cfgs
    }, {"is_creation": "true"})
    provider_id = r.get("id") if isinstance(r, dict) else None
print(f"  LLM provider: {s} id={provider_id}")

# 3. Default model
if provider_id:
    s, r = req("POST", "/admin/llm/default", {"provider_id": provider_id, "model_name": DEFAULT_CHAT})
    print(f"  Default model: {s}")

# 4. Embedding provider
s, r = req("PUT", "/admin/embedding/embedding-provider", {
    "provider_type": "litellm", "api_key": GPUSTACK_KEY,
    "api_url": GPUSTACK_BASE, "api_version": None, "deployment_name": None
})
print(f"  Embedding provider: {s}")

# 5. Search settings (the fleet-standard embedding model, #1786)
#    Model and dim come from standard-models.yaml `defaults.embedding`, not from
#    a literal: the old `nomic-embed-text`/768 pair named a model the standard
#    set does not deploy, at nomic's dim rather than the served one (#70).
if DEFAULT_EMBED_DIM is None:
    # No guessed number. Onyx keeps whatever it has, and the run says why —
    # a dimension that disagrees with the endpoint is the #70 failure, and it
    # fails at insert time with nothing pointing back here.
    print("  Search settings (embedding): SKIPPED — the embedding dimension could "
          "not be read from core/llm/standard-models.yaml. Fix the manifest and "
          "re-run 'rzfz post-install --refresh'.")
else:
    s, r = req("POST", "/search-settings/set-new-search-settings", {
        "model_name": "openai/" + DEFAULT_EMBED, "normalize": True,
        "query_prefix": "", "passage_prefix": "", "api_url": GPUSTACK_BASE,
        "provider_type": "litellm", "api_key": GPUSTACK_KEY, "model_dim": DEFAULT_EMBED_DIM,
        "index_name": None, "multipass_indexing": True,
        "embedding_precision": "float", "enable_contextual_rag": False
    })
    print(f"  Search settings (embedding): {s}")

# 6. Web search (SearXNG)
s, existing_ws = req("GET", "/admin/web-search/search-providers")
if not any(p.get("provider_type") == "searxng" for p in (existing_ws if isinstance(existing_ws, list) else [])):
    s, r = req("POST", "/admin/web-search/search-providers", {
        "name": "SearXNG", "provider_type": "searxng",
        "config": {"base_url": "http://searxng:8080"}, "api_key": None, "activate": True
    })
    print(f"  Web search (SearXNG): {s}")
else:
    print("  Web search (SearXNG): already configured")

# 7. Voice provider (Speaches) — insert via DB to bypass SSRF check
if psycopg2:
    rows = db_exec("SELECT id FROM voice_provider WHERE name='Speaches'")
    if rows:
        db_exec("UPDATE voice_provider SET api_base=%s, is_default_stt=true, is_default_tts=true WHERE name='Speaches'",
                ("http://speaches:8000",))
        print("  Voice (Speaches): updated")
    else:
        db_exec("""INSERT INTO voice_provider
            (name,provider_type,api_key,api_base,custom_config,stt_model,tts_model,
             default_voice,is_default_stt,is_default_tts)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            ("Speaches","speaches",None,"http://speaches:8000","{}",
             None,None,None,True,True))
        print("  Voice (Speaches): inserted")
else:
    print("  Voice (Speaches): skipped (psycopg2 not available)")

print("Onyx provisioning complete.")
PYEOF

        docker exec \
            -e MAIN_DOMAIN="${MAIN_DOMAIN}" \
            -e AUTHENTIK_BOOTSTRAP_PASSWORD="${AUTHENTIK_BOOTSTRAP_PASSWORD}" \
            -e GPUSTACK_API_KEY="${GPUSTACK_API_KEY}" \
            -e POSTGRES_HOST="postgres" \
            -e POSTGRES_USER="${POSTGRES_USER:-docker}" \
            -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
            -e POSTGRES_DB="${ONYX_DB:-onyx_db}" \
            -e DEFAULT_CHAT="$(_default_chat_alias)" \
            -e DEFAULT_EMBED="$(_default_embedding_alias)" \
            -e DEFAULT_EMBED_DIM="$(_default_embedding_dim)" \
            onyx-api python3 /tmp/onyx_post_install.py 2>&1 | \
            while IFS= read -r line; do print_substep "$line"; done

        print_success "Onyx provisioning complete."
    }
    step_onyx_provisioning

    # Step 8b: OpenHands provisioning
    step_openhands_provisioning() {
        if ! echo "${COMPOSE_PROFILES:-}" | grep -qw "openhands"; then
            return 0
        fi
        print_step "OpenHands: Provisioning default settings..."

        local waited=0
        while [ $waited -lt 60 ]; do
            if docker exec openhands curl -sf http://localhost:3000/api/options/models \
                > /dev/null 2>&1; then
                break
            fi
            sleep 5; waited=$((waited + 5))
        done

        # Write LLM settings
        docker exec openhands python3 -c "
from openhands.storage.data_models.settings import Settings
import os, json
s = Settings(
    llm_model='openai/qwen3-coder-next',
    llm_api_key=os.environ.get('LLM_API_KEY',''),
    llm_base_url=os.environ.get('LLM_BASE_URL','http://llm:8080/v1'),   # #1445
    agent='CodeActAgent',
    language='en',
    confirmation_mode=False,
    security_analyzer=None,
)
# OpenHands v1.6.0 moved settings storage from /.openhands-state/ to
# /.openhands/. Write to BOTH so the seeding works against pre-v1.6 and
# v1.6+ images. Also force-overwrite — the curated UI dropdown can store
# `openhands/claude-opus-...` (a SaaS-routed model with no base_url/api_key)
# which leaves the agent stuck in "Starting" with no LLM endpoint to hit.
# Operators can still change the model via the UI; we only re-seed during
# post-install / --refresh.
data = s.model_dump_json(context={'expose_secrets': True})
for path in ('/.openhands/settings.json', '/.openhands-state/settings.json'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(data)
    print(f'wrote {path}')
" 2>/dev/null && print_success "OpenHands: LLM settings written (qwen3-coder-next)." \
            || print_warning "OpenHands: LLM settings provisioning failed"

        # Wire Gitea as GitHub-compatible provider (only if gitea profile active)
        if echo "${COMPOSE_PROFILES:-}" | grep -qw "gitea"; then
            local gitea_token
            gitea_token=$(docker exec gitea su-exec git gitea admin user generate-access-token \
                --username "${GITEA_ADMIN_USER:-admin}" \
                --token-name openhands \
                --raw \
                --scopes "write:repository,write:issue,read:user,read:organization" 2>/dev/null \
                | tr -d '[:space:]')

            if [ -n "${gitea_token}" ]; then
                docker exec openhands python3 -c "
import os, json
path = '/.openhands-state/secrets.json'
os.makedirs('/.openhands-state', exist_ok=True)
if not os.path.exists(path):
    secrets = {
        'provider_tokens': {
            'github': {
                'token': '${gitea_token}',
                'host': 'gitea:3000',
                'user_id': '${GITEA_ADMIN_USER:-admin}'
            }
        },
        'custom_secrets': {}
    }
    with open(path, 'w') as f:
        json.dump(secrets, f)
    print('written')
else:
    print('already exists, skipping')
" 2>/dev/null && print_success "OpenHands: Gitea provider token written." \
                || print_warning "OpenHands: Gitea secrets provisioning failed"
            else
                print_warning "OpenHands: could not generate Gitea token — skipping"
            fi
        fi
    }
    step_openhands_provisioning

    # Step 8b0 (ga.1, Issue A): pre-build ALL custom-build module images so a later
    # Config-portal module-enable never hits a live `docker compose build` → 403 on
    # egress-restricted boxes. Runs regardless of active profiles; idempotent.
    prebuild_all_custom_images

    # Step 8b1 (#36): pre-pull/-build the agent-manager experimental-agent
    # images (openhands + agent-server + paperclip) so users can provision them
    # from the My Agents drawer without hitting the "not available on this box
    # yet" dead-end. Gated on the `agents` profile; idempotent.
    prepull_agent_manager_images

    # Step 8b2: OpenLit admin alignment (rc6.7 #44)
    openlit_align_admin

    # Step 8b3: OpenUEM readiness report (#1075). Profile-gated + non-fatal.
    openuem_report_readiness

    # (#191) Reusable paperless-ngx superuser promotion. Authentik forwards
    # the X-Authentik-Username header on first login, which paperless-ngx's
    # header-auth middleware uses to auto-CREATE a Django user — but never a
    # superuser, so it can't see folder-consumed (no-owner) documents. akadmin
    # is the header value for the built-in Authentik admin; razzfazz-ai-admin
    # is auto-created on first SSO login by any other operator account.
    # Promote both to cover either login path. Idempotent: get_or_create + an
    # unconditional is_superuser=True write on every call. Reused by both
    # step_paperless_provisioning (Step 8c, below — the --preset flow) and
    # paperless_m007_report (the M007 repair pass, further down — the
    # toggle-only-enable flow) so neither reimplements the promotion.
    paperless_promote_superusers() {
        docker exec paperless-ngx python3 manage.py shell -c "
from django.contrib.auth.models import User
from documents.models import UiSettings

def promote_or_create(username, email, password):
    u, created = User.objects.get_or_create(username=username)
    u.email = email
    u.is_staff = True
    u.is_superuser = True
    if created:
        u.set_password(password)
    u.save()
    ui, _ = UiSettings.objects.get_or_create(user=u)
    settings = ui.settings or {}
    settings.setdefault('theme', {})['color'] = '#CD1719'
    ui.settings = settings
    ui.save()
    print(f'  {username}: superuser={u.is_superuser} (created={created})')

promote_or_create('${RAZZFAZZ_ADMIN_USERNAME:-akadmin}', 'razzfazz-ai-admin@${MAIN_DOMAIN}', '${AUTHENTIK_BOOTSTRAP_PASSWORD}')
promote_or_create('razzfazz-ai-admin', 'razzfazz-ai-admin@${MAIN_DOMAIN}', '${AUTHENTIK_BOOTSTRAP_PASSWORD}')
"
    }

    # Step 8c: Paperless-ngx provisioning
    step_paperless_provisioning() {
        if ! echo "${COMPOSE_PROFILES:-}" | grep -qw "paperless-ngx"; then
            return 0
        fi
        print_step "Paperless-ngx: Provisioning..."

        local waited=0
        while [ $waited -lt 120 ]; do
            if docker exec paperless-ngx curl -sf http://localhost:8000/accounts/login/ \
                > /dev/null 2>&1; then
                break
            fi
            sleep 5; waited=$((waited + 5))
        done
        if [ $waited -ge 120 ]; then
            print_warning "Paperless-ngx not ready after 120s — skipping"
            return 0
        fi

        # Authentik sends X-Authentik-Username as the akadmin username
        # razzfazz-ai-admin is auto-created when they first log in via SSO
        # We promote both to superuser to cover both cases
        paperless_promote_superusers 2>&1 | while IFS= read -r line; do print_substep "$line"; done \
            && print_success "Paperless-ngx: users promoted + brand colour set." \
            || print_warning "Paperless-ngx: provisioning failed"
    }
    step_paperless_provisioning

    # Komodo hardening (#539). Komodo's Server.auto_prune defaults to TRUE, which
    # makes komodo-periphery run `docker image prune -a -f` every night at 00:00
    # UTC over the raw docker socket. That deletes every image no container
    # references — i.e. the custom-built images of DISABLED modules, which on an
    # offline or air-gapped box cannot be rebuilt or re-pulled, so the module can
    # never be enabled again. Measured on a customer box: 29 images in one burst.
    # We never chose that default; we inherited it by creating the server via
    # KOMODO_FIRST_SERVER. Disk pressure is handled deliberately elsewhere.
    step_komodo_hardening() {
        echo "${COMPOSE_PROFILES:-}" | grep -qw "monitor" || return 0
        print_step "Komodo: disabling nightly image auto-prune..."
        local komodo_out="" komodo_rc=0
        komodo_out="$(razzfazz_komodo_disable_auto_prune 2>&1)" || komodo_rc=$?
        [ -n "$komodo_out" ] && printf '%s\n' "$komodo_out" \
            | while IFS= read -r line; do print_substep "$line"; done
        if [ "$komodo_rc" -eq 0 ]; then
            print_success "Komodo: auto_prune disabled (custom-built images of disabled modules are safe)."
        else
            print_warning "Komodo: could not disable auto_prune — images of DISABLED modules may be pruned nightly (#539). Check Servers → Local → Auto Prune."
        fi
    }
    step_komodo_hardening

    # Step 8: Restart services that read .env
    print_step "Restarting services to apply new configuration..."
    # Issue B (ga.1) PRIMARY fix: rewrite OWUI's persisted (placeholder) GPUStack
    # key in openwebui_db to the live key BEFORE recreating openwebui, so the
    # recreate reloads a working OpenAI connection and models finally appear.
    _owui_fix_persisted_gpustack_key
    # Best-effort: a recreate that returns non-zero (transient docker error, a
    # dependency still settling) must not abort post-install before the verify
    # suite runs — the verify report reflects each service's real state.
    #
    # --no-deps is MANDATORY (ga.1 iter3): a plain `--force-recreate openwebui`
    # ALSO recreates its depends_on chain — postgres (+ its health-wait) and the
    # one-shot openwebui-migrate-reconcile — and that whole dance returned
    # non-zero during ga.1 testing, so openwebui was NEVER recreated and its
    # container env `OPENAI_API_KEYS` kept the placeholder (`docker exec openwebui
    # printenv OPENAI_API_KEYS` = gpustack_CHANGEME…). With --no-deps only
    # openwebui is recreated (postgres is already up), so it reliably reloads the
    # live GPUStack key from .env AND the persisted config the key-fix above just
    # corrected — mirrors the model-sync recreate below.
    docker compose up -d --no-deps --force-recreate openwebui > /dev/null 2>&1 || print_warning "openwebui recreate returned non-zero — see verify report below."
    # Issue B (ga.1) fix #1: recreate the ACTIVE model-sync variant (--no-deps) so
    # it stops using the placeholder key and starts writing the real model list
    # into openwebui_db.
    recreate_active_model_sync
    # #1507: if the deploy above never reached "all ready", the two steps that
    # enumerate the served set ran against an incomplete one. Give the slow
    # model a bounded second chance and, when it arrives, re-run exactly those
    # two (both idempotent) so the consumers end up with what the box serves.
    llm_manager_reconcile_consumers_late || true
    # ga.1 iter3 (Dify KB indexing): recreate the pgvector-consuming Dify services
    # so the corrected PGVECTOR_PASSWORD (= POSTGRES_PASSWORD, matching the docker
    # PGVECTOR_USER) lands. Without this an existing box keeps the stale env that
    # 401'd every dataset-indexing embedding write ("password authentication
    # failed for user docker"). Fresh installs get it from `rzfz init` already;
    # this heals re-runs. --no-deps so we don't drag postgres/valkey.
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "dify"; then
        docker compose up -d --no-deps --force-recreate dify-api dify-worker dify-worker-beat > /dev/null 2>&1 \
            || print_warning "Dify pgvector-service recreate returned non-zero — see verify report below."
    fi
    # ga.1 iter3 (coding-agent local-model wire): re-key provisioned coding agents
    # to the live GPUStack key. See rekey_coding_agents() for the why.
    rekey_coding_agents
    # Only restart lightrag/cognee if they're in COMPOSE_PROFILES
    if echo "${COMPOSE_PROFILES:-}" | grep -q "lightrag"; then
        docker compose up -d --force-recreate lightrag > /dev/null 2>&1 || print_warning "lightrag recreate returned non-zero — see verify report below."
    fi
    if echo "${COMPOSE_PROFILES:-}" | grep -q "cognee"; then
        docker compose up -d --force-recreate cognee > /dev/null 2>&1 || print_warning "cognee recreate returned non-zero — see verify report below."
    fi
    print_success "Services restarted."
    
    # Wait for services to stabilize after restart
    print_substep "Waiting 15s for services to stabilize..."
    sleep 15
    
    # Step 9: Help Cache Warming
    # Must happen AFTER restart so the container is running and not immediately killed
    print_step "Help Center: Pre-warming cache..."
    print_substep "Triggering /api/refresh-all..."
    # The help container has no mapped ports to the host (accessed only via Caddy).
    # We trigger the cache refresh internally using wget.
    docker exec -d razzfazz-help wget -qO- --post-data="" --header="X-Authentik-Groups: razzfazz.ai Super Admins" "http://127.0.0.1:5000/api/refresh-all" > /dev/null 2>&1
    print_success "Cache refresh triggered."

    # Step 10 (release-cycle Phase 7b on the customer side): regenerate the
    # operator-facing PDFs. They embed live data (MAIN_DOMAIN, current
    # GPUStack model list) so they MUST be regenerated per-install — they
    # are gitignored for that reason. Both scripts depend on the gotenberg
    # container, so this must happen after the stack is up + Step 8's
    # restart-and-stabilize wait completed.
    print_step "Operator-facing PDFs: regenerating getting-started.pdf + quickstart.pdf..."
    if docker ps --format '{{.Names}}' | grep -q '^gotenberg$'; then
        if [ -x "${SCRIPT_DIR}/scripts/generate-getting-started-pdf.sh" ]; then
            "${SCRIPT_DIR}/scripts/generate-getting-started-pdf.sh" 2>&1 | tail -2 || \
                print_warning "getting-started.pdf generation failed — re-run later: ./scripts/generate-getting-started-pdf.sh"
        fi
        if [ -x "${SCRIPT_DIR}/scripts/generate-quickstart-pdf.sh" ]; then
            "${SCRIPT_DIR}/scripts/generate-quickstart-pdf.sh" 2>&1 | tail -2 || \
                print_warning "quickstart.pdf generation failed — re-run later: ./scripts/generate-quickstart-pdf.sh"
        fi
        print_success "Day-1 PDFs regenerated against current install state."
    else
        print_warning "gotenberg container not running — skipping PDF regeneration."
        print_info "Re-run later when gotenberg is up: ./scripts/generate-getting-started-pdf.sh && ./scripts/generate-quickstart-pdf.sh"
    fi

    # Step 11 (§16 handover): leave an as-built day-1 security-posture assessment.
    run_security_selfcheck

    echo ""
    echo -e "${GREEN}======================================================================${NC}"
    echo -e "${GREEN}     Post-Install Provisioning Complete${NC}"
    echo -e "${GREEN}======================================================================${NC}"
    echo ""
    echo "  Preset:       $PRESET"
    echo "  Domain:       ${MAIN_DOMAIN}"
    echo "  Admin email:  razzfazz-ai-admin@${MAIN_DOMAIN}"
    echo ""
    # #178(c): real per-step OK/WARN/FAIL, not a static checklist — see
    # record_step_status / print_services_summary near the top of this file.
    print_services_summary
    echo ""
    if [ "$SKIP_MODELS" = false ] && [ "$SKIP_WAIT" = true ]; then
        echo -e "  ${YELLOW}Note: Models are still downloading in background.${NC}"
        echo "  Check progress: https://${GPUSTACK_DOMAIN:-gpustack.${MAIN_DOMAIN}}"   # #1444: GPUStack moved off llm.<domain>
        echo ""
    fi
    echo "  Next steps:"
    echo "    • Verify: rzfz post-install --verify"
    echo "    • Open:   https://chat.${MAIN_DOMAIN}"
    echo ""
    # M007 manual setup reminders.
    # 2026-05-09 fix: this code is at the script's top level (not inside a
    # function), so `local` errors with "Kann nur innerhalb einer Funktion
    # benutzt werden" / "can only be used in a function" and bash exits 2.
    # Plain assignment is fine — top-level vars are global anyway.
    m007_notes=false
    # (#191) step_paperless_provisioning (Step 8c, above) only runs inline in
    # THIS --preset invocation, right after `docker compose up` — it never
    # fires for paperless-ngx toggled on later via the Config Portal, which
    # does not call post-install at all. paperless_m007_report re-attempts
    # the SAME idempotent promotion here as a repair pass, so a later
    # `rzfz post-install --preset ...` re-run (the standard recipe elsewhere
    # in this file for fixing a half-provisioned module) actually closes the
    # gap instead of just re-printing a manual instruction nobody runs.
    paperless_m007_report() {
        if ! echo "${COMPOSE_PROFILES:-}" | grep -qw "paperless-ngx"; then
            return 0
        fi
        local promote_rc=0
        paperless_promote_superusers >/dev/null 2>&1 || promote_rc=$?
        if [ "$promote_rc" -eq 0 ]; then
            echo "  ✓ Paperless-ngx: superuser auto-promoted (${RAZZFAZZ_ADMIN_USERNAME:-akadmin} / razzfazz-ai-admin)."
            return 0
        fi
        if [ "$m007_notes" = false ]; then
            echo "  M007 manual setup required:"
            m007_notes=true
        fi
        echo "    • Paperless-ngx superuser (auto-promotion failed — repair manually):"
        echo "      docker exec -it paperless-ngx python manage.py createsuperuser"
    }
    paperless_m007_report
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "infisical"; then
        if [ "$m007_notes" = false ]; then
            echo "  M007 manual setup required:"
            m007_notes=true
        fi
        echo "    • Infisical admin signup: https://infisical.${MAIN_DOMAIN}/admin/signup"
    fi
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "onyx"; then
        if [ "$m007_notes" = false ]; then
            echo "  M007 manual setup required:"
            m007_notes=true
        fi
        echo "    • Onyx connectors: https://onyx.${MAIN_DOMAIN}"
        echo "      - GitHub connector: URL=http://gitea:3000/api/v1, token=<Gitea API token>"
        echo "      - File connector: path=/mnt/paperless-media"
        echo "      Set GITEA_API_TOKEN in .env after creating token in Gitea"
    fi
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "openhands"; then
        if [ "$m007_notes" = false ]; then
            echo "  M007 manual setup required:"
            m007_notes=true
        fi
        echo "    • OpenHands LLM: set LLM_MODEL env var in openhands/compose.yml"
        echo "      to a model available in GPUStack (e.g. qwen3-coder:latest)"
    fi
    if [ "$m007_notes" = true ]; then echo ""; fi
fi

# Verification (S07)
if [ "$DO_VERIFY" = true ]; then
    run_verification_suite
fi

# #1250a: a box whose embedded worker never registered cannot place a single
# model — the LLM Manager has nothing to schedule on and every deploy 409s.
# That is a FAILED provisioning run, so say so in the exit code instead of
# printing a green summary over it. (`rzfz upgrade` runs
# `post-install --refresh` best-effort and only warns on a non-zero exit, so
# this never fails an upgrade.)
_exit_on_failed_provisioning
