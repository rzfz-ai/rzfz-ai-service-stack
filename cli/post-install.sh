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
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

# Local print_step preserves the original horizontal-rule decorator above the
# [STEP] line — lib.sh's print_step has no rule.
print_step() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}[STEP]${NC} $1"
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
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        # INODE-PRESERVING (sed -i creates a new inode → breaks razzfazz-config's
        # single-file .env bind mount → portal module-toggle fails; see
        # project_config_ui_env_write_broken). cat > truncates the same inode.
        local _t="${file}.tmp.$$"
        if sed "s|^${key}=.*|${key}=${value}|" "$file" > "$_t"; then
            cat "$_t" > "$file"
        fi
        rm -f "$_t"
    else
        echo "${key}=${value}" >> "$file"
    fi
}

# ==============================================================================
# HTTP Helpers
# ==============================================================================

# M029-S04: GPUStack runtime version detection. Caches in _GPUSTACK_RUNTIME
# so we only probe once per script run. Returns one of:
#   0.7   — legacy path (llm-legacy / llm-cpu profiles)
#   2.x   — new path (llm profile)
#   ?     — unable to detect (fall through to v2.x assumption + log)
#
# Probe: GET /v2/models without auth. v0.7.x has no /v2/* surface so
# returns 404; v2.x has /v2/models gated by auth so returns 401. No
# auth required for the probe itself — the status code is the signal.
# (/v1/version returns 404 on 0.7.x — it's a v2-only endpoint despite
#  the /v1 prefix. Confirmed empirically on 0.91 during S04.)
gpustack_runtime_version() {
    if [ -n "${_GPUSTACK_RUNTIME:-}" ]; then
        echo "$_GPUSTACK_RUNTIME"
        return
    fi
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        "http://127.0.0.1:${GPUSTACK_PORT:-9090}/v2/models" 2>/dev/null)
    case "$code" in
        404) _GPUSTACK_RUNTIME="0.7" ;;
        401|200) _GPUSTACK_RUNTIME="2.x" ;;
        *)   _GPUSTACK_RUNTIME="?" ;;
    esac
    echo "$_GPUSTACK_RUNTIME"
}

# Returns "/v1" for legacy GPUStack (0.7.x), "/v2" for 2.x. Used by the
# model-deploy / api-key / cluster lookups so the post-install runs on
# either runtime without forking the script.
gpustack_api_prefix() {
    case "$(gpustack_runtime_version)" in
        0.7) echo "/v1" ;;
        2.x) echo "/v2" ;;
        *)   echo "/v2" ;;  # fall through to v2.x; deploy will fail with a clear error if the runtime is something we don't know
    esac
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

wait_for_model() {
    local model_name="$1" max_wait="${2:-1800}"  # 30 min default for large models
    local waited=0 interval=15
    # M029-S04: model-instances path is /v1/... on 0.7.x, /v2/... on 2.x
    local prefix
    prefix=$(gpustack_api_prefix)
    print_substep "Waiting for model '$model_name' to be ready (downloading + loading)..."
    while [ $waited -lt $max_wait ]; do
        local state
        state=$(gpustack_api GET "${prefix}/model-instances" | \
            python3 -c "
import sys,json
d=json.load(sys.stdin)
for mi in d.get('items',[]):
    if mi.get('model_name') == '$model_name':
        print(mi.get('state','unknown'))
        break
else:
    print('not_found')
" 2>/dev/null)
        
        case "$state" in
            running)
                print_success "Model '$model_name' is running."
                return 0
                ;;
            downloading|initializing|starting|pending|scheduled)
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
    cur=$(grep -m1 '^OWUI_OPENAI_KEYS=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    if [ -z "$cur" ] || [ "$cur" = '${GPUSTACK_API_KEY}' ]; then
        update_env_value "$ENV_FILE" "OWUI_OPENAI_KEYS" "$GPUSTACK_API_KEY"
        # OWUI reads OPENAI_API_KEYS from its ENV at startup and the ENV OVERRIDES its
        # stored config — so if OWUI started earlier in the upgrade with the stale
        # literal, recreate it now so the resolved key actually reaches the container.
        docker compose up -d --force-recreate openwebui >/dev/null 2>&1 || true
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

    # M029-S04: api-key endpoint is /v1/api-keys on 0.7.x, /v2/api-keys on 2.x
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

_pick_default_cluster_id() {
    # rc6.7 #24: GPUStack v2.x added multi-cluster support and made cluster_id
    # NOT NULL on the models table. The API does NOT default it server-side —
    # callers must include cluster_id in the model creation payload or the
    # INSERT fails with `null value in column "cluster_id" violates not-null
    # constraint`. There is always a "Default Cluster" auto-created on first
    # worker register; we look it up by is_default=true and cache the id for
    # the rest of this run.
    #
    # M029-S04: GPUStack 0.7.x has no clusters at all — return empty so the
    # caller knows to skip cluster_id from the deploy payload.
    if [ "$(gpustack_runtime_version)" = "0.7" ]; then
        echo ""
        return 0
    fi
    if [ -n "${_GPUSTACK_DEFAULT_CLUSTER_ID:-}" ]; then
        echo "$_GPUSTACK_DEFAULT_CLUSTER_ID"
        return
    fi
    local cid
    cid=$(gpustack_api GET "/v2/clusters" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for c in d.get('items', []):
        if c.get('is_default'):
            print(c['id'])
            break
except Exception:
    pass
" 2>/dev/null)
    if [ -z "$cid" ]; then
        return 1
    fi
    _GPUSTACK_DEFAULT_CLUSTER_ID="$cid"
    echo "$cid"
}

_pick_backend_name() {
    # rc6.7 #12: derive the GPUStack backend name from HARDWARE.
    # GPUStack v2.x dropped the bare `llama-box` shipped in v0.7.1; on v2.x
    # the runners are registered as named custom backends by
    # modules/llm/gpustack/init-backends.py:
    #   amd     → llama-box-vulkan-custom  (M022 preferred over ROCm runner
    #                                        on chat/code latency benchmarks;
    #                                        AMD operators can also pick the
    #                                        ROCm variant by setting
    #                                        GPUSTACK_BACKEND env override.)
    #   cpu     → llama-box-cpu-custom    (ggml-org/llama.cpp:server)
    #   nvidia  → vllm                    (built-in; works with NVIDIA out
    #                                        of the box. NVIDIA operators
    #                                        wanting llama.cpp can override
    #                                        to llama-box-vulkan-custom or
    #                                        register their own.)
    # GPUSTACK_BACKEND wins if explicitly set in .env (e.g. for AMD ROCm
    # runner: GPUSTACK_BACKEND=llama-box-rocm-custom).
    local override="${GPUSTACK_BACKEND:-}"
    if [ -n "$override" ]; then
        echo "$override"
        return
    fi
    case "${HARDWARE:-amd}" in
        cpu)    echo "llama-box-cpu-custom" ;;
        amd)    echo "llama-box-vulkan-custom" ;;
        nvidia) echo "vllm" ;;
        *)      echo "llama-box-vulkan-custom" ;;
    esac
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

deploy_model() {
    local name="$1" repo="$2" filename="$3" category="$4"
    shift 4
    local backend_params="$1" extra_json="${2:-}"

    # M029-S04: dual-track GPUStack 0.7.x (legacy stable) and 2.x (experimental).
    # The endpoints, schema, and post-deploy steps differ enough that branching
    # is clearer than threading a prefix through every line.
    local runtime prefix
    runtime=$(gpustack_runtime_version)
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
            print_info  "  Bundle it with 'rzfz package --include-models' (dev box) or sideload via config.<domain> → LLM/Models, then re-run. Check with 'rzfz verify-models'."
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

    local payload
    if [ "$runtime" = "0.7" ]; then
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
    'cpu_offloading': False,
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
    else
        # v2.x path (M029-EXPERIMENTAL): full custom-backend + cluster schema.
        local backend_name cluster_id
        backend_name=$(_pick_backend_name)
        cluster_id=$(_pick_default_cluster_id) || {
            print_error "Failed to resolve default GPUStack cluster id (no default cluster yet?)."
            return 1
        }
        print_substep "Deploying model '$name' from $repo (runtime=2.x, backend=$backend_name, cluster_id=$cluster_id)..."
        payload=$(BACKEND_NAME="$backend_name" CLUSTER_ID="$cluster_id" LLM_LIB_DIR="$SCRIPT_DIR/core/llm" RZFZ_OFFLINE="$_off" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ['LLM_LIB_DIR'])
import model_source
model = {
    'name': '$name',
    'backend': os.environ['BACKEND_NAME'],
    'cluster_id': int(os.environ['CLUSTER_ID']),
    'categories': ['$category'],
    'replicas': 1,
    'backend_parameters': $bp_json,
    'cpu_offloading': False,
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
    fi

    local resp
    resp=$(gpustack_api POST "${prefix}/models" "$payload")

    local model_id
    model_id=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)

    if [ -z "$model_id" ]; then
        print_error "Failed to deploy model '$name'."
        print_info "Response: $(echo "$resp" | head -c 300)"
        return 1
    fi

    # M029-S04: model-routes only exist on v2.x. v0.7.x auto-exposes deployed
    # models on /v1-openai without an explicit route — same OpenAI-compatible
    # surface, simpler model.
    local route_id=""
    if [ "$runtime" != "0.7" ]; then
        # rc6.7 #45: GPUStack v2 doesn't auto-create a model_route when a model
        # is POSTed via /v2/models — the OpenAI-compatible /v1-openai/models
        # endpoint only exposes models that have a route with at least one
        # target. Without this, OpenWebUI / Dify / any OpenAI client only sees
        # the bootstrapped model. Create the route + add the model as a target.
        # Idempotent: if a route with the same name exists, reuse it.
        local route_resp
        route_resp=$(gpustack_api POST "/v2/model-routes" "{\"name\":\"$name\",\"categories\":[\"$category\"]}")
        route_id=$(echo "$route_resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','') if isinstance(d,dict) else '')" 2>/dev/null)
        if [ -z "$route_id" ]; then
            route_id=$(gpustack_api GET "/v2/model-routes?perPage=100" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for r in d.get('items',[]):
    if r.get('name') == '$name':
        print(r['id']); break
" 2>/dev/null)
        fi
        if [ -n "$route_id" ]; then
            gpustack_api POST "/v2/model-routes/$route_id/add-targets" \
                "[{\"model_id\":$model_id,\"weight\":100,\"name\":\"${name}-deployment\"}]" >/dev/null 2>&1 || true
        fi
    fi

    print_success "Model '$name' deployed (id=$model_id${route_id:+, route_id=$route_id}). Download starting..."
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
    local preset="$1"
    local active_profiles="${COMPOSE_PROFILES:-}"
    PRESET="$preset" PROFILES="$active_profiles" YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 - <<'PYEOF'
import os, sys
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not available — install python3-yaml or adjust the script.", file=sys.stderr)
    sys.exit(2)

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
    auto_start = str(m.get("auto_start", True)).lower()
    print("\t".join([alias, repo, filename, cat, bp, ",".join(roles), auto_start]))
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
            local alias repo filename category backend_params auto_start
            alias=$(printf '%s' "$_row" | cut -f1)
            repo=$(printf '%s' "$_row" | cut -f2)
            filename=$(printf '%s' "$_row" | cut -f3)
            category=$(printf '%s' "$_row" | cut -f4)
            backend_params=$(printf '%s' "$_row" | cut -f5)
            auto_start=$(printf '%s' "$_row" | cut -f7)
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
            deploy_model "$alias" "$repo" "$filename" "$category" "$backend_params"
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
    [ -z "$failed_optional" ] && print_success "All models are running."
    return 0
}

# Resolve the always-on default chat alias from standard-models.yaml
# (`defaults.chat`). This is the single model every consumer (Dify/Onyx/
# OpenWebUI default + the verify smoke) should wire as its default
# text-generation model. auto_start:false models (gemma4, qwen3-coder-next)
# are registered but scaled to 0 replicas (on-demand) — wiring them as the live
# default 503s on credential validation, so they must NOT be the default.
_default_chat_alias() {
    local a
    a=$(YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os
print(yaml.safe_load(open(os.environ['YAML_PATH']))['defaults'].get('chat', ''))
" 2>/dev/null)
    echo "${a:-qwen3.6}"
}

# Resolve the fleet-standard embedding alias from standard-models.yaml
# (`defaults.embedding`). qwen3-embedding (32K ctx, dim 2560) replaced nomic
# (2048-tok cap) as the standard — Dify's default text-embedding must follow it
# so newly-created knowledge bases use the same model as cognee/lightrag/OWUI RAG.
_default_embedding_alias() {
    local a
    a=$(YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os
print(yaml.safe_load(open(os.environ['YAML_PATH']))['defaults'].get('embedding', ''))
" 2>/dev/null)
    echo "${a:-qwen3-embedding}"
}

update_env_model_config() {
    local preset="$1"
    print_step "Updating .env with model configuration..."

    # M031 S3: aliases come from standard-models.yaml's `defaults` block
    # (e.g. defaults.chat → qwen3.6, defaults.embedding → qwen3-embedding).
    # Cognee's COGNEE_LLM_MODEL needs the litellm `openai/` prefix; the
    # other consumers want bare aliases.
    local chat_alias embed_alias
    chat_alias=$(YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os
print(yaml.safe_load(open(os.environ['YAML_PATH']))['defaults'].get('chat', ''))
" 2>/dev/null)
    embed_alias=$(YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os
print(yaml.safe_load(open(os.environ['YAML_PATH']))['defaults'].get('embedding', ''))
" 2>/dev/null)

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
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data'][0]['embedding']))" 2>/dev/null)
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
    else
        print_warning "LightRAG embed-dim could not be determined; leaving LIGHTRAG_EMBEDDING_DIM as-is (check it matches $embed_alias)."
    fi

    # Cognee (needs openai/ prefix for litellm routing)
    update_env_value "$ENV_FILE" "COGNEE_LLM_MODEL" "openai/$chat_alias"
    update_env_value "$ENV_FILE" "COGNEE_EMBEDDING_MODEL" "$embed_alias"

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
    if [ -n "$(read_env_value "$ENV_FILE" COGNEE_MCP_API_KEY)" ]; then
        print_info "COGNEE_MCP_API_KEY already set."
    elif ! docker ps --format '{{.Names}}' | grep -qx cognee; then
        print_warning "cognee not running — skipping COGNEE_MCP_API_KEY mint."
    else
        local em pw tok key _cap _abp
        em="razzfazz-ai-admin@$(read_env_value "$ENV_FILE" MAIN_DOMAIN)"
        # Try COGNEE_ADMIN_PASSWORD first, then AUTHENTIK_BOOTSTRAP_PASSWORD.
        # NOT just fallback-when-empty (ga.6 fix): the known cognee pw-divergence
        # leaves COGNEE_ADMIN_PASSWORD set-but-wrong while the DB seed is the
        # bootstrap pw, so a set-but-wrong value must not silently block the mint.
        _cap="$(read_env_value "$ENV_FILE" COGNEE_ADMIN_PASSWORD)"
        _abp="$(read_env_value "$ENV_FILE" AUTHENTIK_BOOTSTRAP_PASSWORD)"
        tok=""
        for pw in "$_cap" "$_abp"; do
            [ -z "$pw" ] && continue
            tok=$(docker exec cognee sh -c "curl -fsS -X POST http://localhost:8000/api/v1/auth/login -H 'Content-Type: application/x-www-form-urlencoded' --data-urlencode 'username=$em' --data-urlencode 'password=$pw'" 2>/dev/null \
                  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)
            [ -n "$tok" ] && break
            [ "$_cap" = "$_abp" ] && break   # identical → don't retry the same pw
        done
        if [ -z "$tok" ]; then
            print_warning "cognee login failed with COGNEE_ADMIN_PASSWORD + AUTHENTIK_BOOTSTRAP_PASSWORD — cannot mint COGNEE_MCP_API_KEY (cognee pw divergence? realign the cognee DB pw)."
        else
            key=$(docker exec cognee sh -c "curl -fsS -X POST http://localhost:8000/api/v1/auth/api-keys -H 'Authorization: Bearer $tok' -H 'Content-Type: application/json' -d '{\"name\":\"cognee-mcp\"}'" 2>/dev/null \
                  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("key",""))' 2>/dev/null)
            if [ -n "$key" ]; then
                update_env_value "$ENV_FILE" "COGNEE_MCP_API_KEY" "$key"
                print_success "Minted COGNEE_MCP_API_KEY for cognee-mcp sidecar."
            else
                print_warning "cognee api-key mint failed."
            fi
        fi
    fi
    # Validate the MCP registry (non-fatal advisory).
    if [ -f "$SCRIPT_DIR/core/mcp/sync.py" ]; then
        python3 "$SCRIPT_DIR/core/mcp/sync.py" --check 2>&1 | sed 's/^/  /' \
            || print_warning "MCP registry validation reported issues."
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
       && docker exec authentik-worker python /tmp/apply_policy_bindings.py; then
        print_success "Authentik bindings + outpost providers reconciled."
    else
        print_warning "Authentik binding reconcile reported issues — re-run: docker exec authentik-worker python /tmp/apply_policy_bindings.py"
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
    else
        print_warning "$model_type model download response: $resp"
    fi
}

step_speaches_provisioning() {
    print_step "Speaches & Auxiliary: Provisioning..."
    
    wait_for_service "Speaches" "http://127.0.0.1:${SPEACHES_PORT:-5003}/v1/models" 60
    
    # Download STT model
    speaches_download_model "Systran/faster-whisper-small" "STT"
    
    # Download TTS model/voice
    speaches_download_model "ufozone/piper-de_DE-jarvis-high" "TTS"
    
    # Model sync — GPUSTACK_API_KEY already in .env from ensure_gpustack_api_key
    print_substep "Model sync: API key already configured in .env."
    
    # LightRAG + Cognee model names already set in update_env_model_config
    print_substep "LightRAG + Cognee: model names already configured in .env."
    
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
    local b64_pass
    b64_pass=$(echo -n "$dify_pass" | base64)
    
    local resp
    resp=$(docker exec dify-api curl -s -D- -X POST http://localhost:5001/console/api/login \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"${dify_email}\",\"password\":\"${b64_pass}\"}" 2>&1)
    
    DIFY_ACCESS=$(echo "$resp" | grep -oP 'access_token=\K[^;]+')
    DIFY_CSRF=$(echo "$resp" | grep -oP 'csrf_token=\K[^;]+')
    
    [ -n "$DIFY_ACCESS" ] && [ -n "$DIFY_CSRF" ]
}

dify_api() {
    local method="$1" path="$2" data="$3"
    local url="http://localhost:5001${path}"
    local cmd="curl -s -X $method"
    cmd="$cmd -H 'Cookie: access_token=${DIFY_ACCESS}; csrf_token=${DIFY_CSRF}'"
    cmd="$cmd -H 'X-CSRF-Token: ${DIFY_CSRF}'"
    
    if [ -n "$data" ]; then
        cmd="$cmd -H 'Content-Type: application/json' -d '$data'"
    fi
    cmd="$cmd '$url'"
    
    docker exec dify-api sh -c "$cmd" 2>/dev/null
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

dify_ensure_admin() {
    local admin_email="razzfazz-ai-admin@${MAIN_DOMAIN}"
    local admin_pass="${AUTHENTIK_BOOTSTRAP_PASSWORD}"
    
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
        init_pass=$(grep "^INIT_PASSWORD=" "${SCRIPT_DIR}/.env.dify" 2>/dev/null | cut -d= -f2-)
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
        init_resp=$(docker exec dify-api curl -s -c /tmp/dify_setup.jar \
            -X POST http://localhost:5001/console/api/init \
            -H "Content-Type: application/json" \
            -d "{\"password\":\"${init_pass}\"}" 2>&1)
        print_info "Init response: $init_resp"
        
        # Step 2: Create admin user (with session cookie from step 1)
        # NOTE: setup endpoint expects PLAINTEXT password (no base64 encoding)
        local setup_resp
        setup_resp=$(docker exec dify-api curl -s -b /tmp/dify_setup.jar \
            -X POST http://localhost:5001/console/api/setup \
            -H "Content-Type: application/json" \
            -d "{\"email\":\"${admin_email}\",\"name\":\"razzfazz.ai Admin\",\"password\":\"${admin_pass}\"}" 2>&1)
        print_info "Setup response: $setup_resp"
        sleep 2
    fi
    
    # Try login with current domain email
    if dify_login "$admin_email" "$admin_pass"; then
        print_success "Logged in to Dify as $admin_email."
        set -e
        return 0
    fi
    
    # Try with old domain (existing installation)
    local existing_email
    existing_email=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${DIFY_DB:-dify_db}" -t -c \
        "SELECT email FROM accounts LIMIT 1;" 2>/dev/null | xargs)
    
    if [ -n "$existing_email" ] && [ "$existing_email" != "$admin_email" ]; then
        print_info "Found existing Dify admin: $existing_email"
        if dify_login "$existing_email" "$admin_pass"; then
            print_success "Logged in to Dify as $existing_email."
            set -e
            return 0
        fi
    fi
    
    # Last resort: reset password in DB using Dify's own password hashing
    print_substep "Resetting Dify admin password via database..."
    local target_email="${existing_email:-${admin_email}}"
    
    docker exec dify-api python3 -c "
import base64, hashlib, binascii, os
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha256', '${admin_pass}'.encode('utf-8'), salt, 10000)
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

dify_install_plugin() {
    local search_name="$1"

    # #184 WS2b: resolving + installing a plugin queries the Dify marketplace
    # (marketplace.dify.ai) and fetches the package → internet egress. Skip on an
    # air-gapped box (a bundled plugin is loaded another way; the box stays usable
    # without it — the caller already treats a failed install as non-fatal).
    if razzfazz_offline_skip "Dify plugin install from marketplace ('${search_name}')"; then
        return 0
    fi

    # Per-plugin PINNED VERSION + a full offline fallback identifier
    # (name:version@checksum). We pin the version deliberately — gpustack MUST be
    # >=0.0.15 for correct thinking-param passthrough
    # (chat_template_kwargs.enable_thinking); 0.0.8 silently drops it, breaking
    # structured-extraction workflows.
    local pin_version pin_fallback
    case "$search_name" in
        langgenius/gpustack)
            pin_version="0.0.15"
            pin_fallback="langgenius/gpustack:0.0.15@0f855990202d90d9e4ffeff0fc5ef8a7b8e51e0d432f14a32ff522af8609a5bd" ;;
        langgenius/openai_api_compatible)
            pin_version="0.0.34"
            pin_fallback="langgenius/openai_api_compatible:0.0.34@e48cbbb045473a9d04289becacf7626701e9d1b9c860fe5d9a4b87ad7fdb62bd" ;;
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

    # Version-aware skip: only skip if the EXACT target identifier is already
    # installed. Otherwise fall through to install — Dify's marketplace install
    # upgrades an older version (e.g. gpustack 0.0.8 → 0.0.15). A plain present/absent
    # check would freeze boxes on a stale plugin forever, even after the pin is bumped.
    local installed_id
    installed_id=$(dify_api GET "/console/api/workspaces/current/plugin/list?page=1&page_size=100" | \
        python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    for p in d.get('plugins',[]):
        if p.get('plugin_id') == '$search_name':
            print(p.get('plugin_unique_identifier') or (str(p.get('plugin_id',''))+':'+str(p.get('version',''))))
            break
except: pass
" 2>/dev/null)
    if [ -n "$installed_id" ] && [ "$installed_id" = "$plugin_id" ]; then
        print_info "Plugin '$search_name' already at target version."
        return 0
    elif [ -n "$installed_id" ]; then
        print_substep "Upgrading plugin '$search_name' ($installed_id -> $plugin_id)..."
    fi
    
    print_substep "Installing plugin '$search_name' ($plugin_id)..."
    local resp
    resp=$(dify_api POST "/console/api/workspaces/current/plugin/install/marketplace" \
        "{\"plugin_unique_identifiers\":[\"${plugin_id}\"]}")
    
    # Check for success (the response contains task info)
    if echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'all_installed' in d or 'task_id' in str(d)" 2>/dev/null; then
        print_success "Plugin '$search_name' installation initiated."
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

dify_configure_model() {
    print_substep "Configuring GPUStack models in Dify..."

    # S20: resolve the real admin email (survives a post-init MAIN_DOMAIN change)
    local admin_email
    admin_email=$(_resolve_dify_admin_email)
    local gpustack_url="http://gpustack:9090"
    local api_key="${GPUSTACK_API_KEY}"
    local preset="${PRESET:-standard}"
    local tz="${TZ:-UTC}"

    # S19: the LLM-category aliases that ship under this preset, read from
    # standard-models.yaml (the same enumerator deploy/wait use). Replaces the
    # hardcoded gemma4 + qwen3.5/qwen3-coder-next list that drifted from the
    # YAML and registered a non-existent model in Dify (404 on validation).
    # Only always-on chat models (auto_start != false, col 7). Stopped/optional
    # models (gemma4, qwen3-coder-next at 0 replicas) 503 on Dify credential
    # validation, so they are not registered here; they get wired manually if
    # the operator scales them up. default_chat is the system default text-gen.
    local llm_aliases default_chat default_embedding
    llm_aliases=$(_model_rows_for_preset "$preset" | awk -F'\t' '$6 ~ /(^|,)chat(,|$)/ && $7 != "false"{print $1}' | tr '\n' ' ')
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
        # Dify hardcodes new accounts to America/New_York, so the UI renders
        # workflow-run + log timestamps in EDT instead of local time. Align the
        # admin account timezone with the stack TZ (idempotent).
        if account.timezone != '${tz}':
            account.timezone = '${tz}'
            db.session.commit()
            print('Account timezone set to ${tz}.')
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

        # ── Embedding models ──────────────────────────────────────────────────
        add_model('nomic-embed-text', 'text-embedding', {'context_size': '8192'})
        add_model('qwen3-embedding',  'text-embedding', {'context_size': '32768'})

        # ── Rerank models ─────────────────────────────────────────────────────
        add_model('qwen3-reranker', 'rerank', {'context_size': '8192', 'timeout': '600'})

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
    output=$(docker exec -i dify-api python3 /tmp/config_models.py 2>&1)
    rc=$?

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
    else
        # rc==0 but nothing registered — still a failure worth surfacing.
        print_error "Dify model config produced no registrations: $output"
        return 1
    fi
}

step_dify_provisioning() {
    print_step "Dify: Provisioning..."
    
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
    
    # Install plugins (pinned versions with content hashes)
    # These identifiers are from the reference box — update when upgrading plugins
    # Install Dify plugins (using short names will install the latest available versions).
    # A single plugin failing to resolve/install (transient marketplace error, or
    # an upstream plugin without a hardcoded fallback id — e.g. junjiem/mcp_sse)
    # must NOT abort the whole post-install: dify_install_plugin `return 1`s on an
    # unresolvable plugin, and under `set -e` a bare call here aborted everything
    # downstream (Speaches, Gitea, the verify suite). Each install is best-effort;
    # the operator can install any that failed from the Dify marketplace UI later.
    dify_install_plugin "langgenius/gpustack" || print_warning "Dify plugin 'langgenius/gpustack' install failed — install it later via the Dify marketplace."
    dify_install_plugin "langgenius/openai_api_compatible" || print_warning "Dify plugin 'langgenius/openai_api_compatible' install failed — install it later via the Dify marketplace."
    dify_install_plugin "abesticode/knowledge_pro" || print_warning "Dify plugin 'abesticode/knowledge_pro' install failed — install it later via the Dify marketplace."
    # M035 P4: MCP client plugin so Dify can use the stack's MCP servers (e.g.
    # cognee-mcp). Runs IN dify-plugin-daemon, which is PROXY-FREE — unlike
    # Dify's built-in Tools->MCP, whose client routes through the SSRF proxy and
    # can't reach internal stack hosts (confirmed in M033 S12). App-builders add
    # the "MCP SSE/StreamableHTTP" tool to a workflow + point it at, e.g.,
    # {"cognee":{"transport":"streamable_http","url":"http://cognee-mcp:8000/mcp"}}.
    dify_install_plugin "junjiem/mcp_sse" || print_warning "Dify plugin 'junjiem/mcp_sse' (MCP client) install failed — install it later via the Dify marketplace."
    
    # Wait for plugins to be fully installed before configuring models
    print_substep "Waiting for plugins to finish installing..."
    local waited=0
    while [ $waited -lt 120 ]; do
        local installed
        installed=$(dify_api GET "/console/api/workspaces/current/plugin/list?page=1&page_size=100" | \
            python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    ids=[p.get('plugin_id') for p in d.get('plugins',[])]
    ready = 'langgenius/gpustack' in ids and 'langgenius/openai_api_compatible' in ids
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
    # models registered.
    dify_configure_model || return 1

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

    print_success "Dify provisioning complete."
}

# ==============================================================================
# Open WebUI Provisioning
# ==============================================================================
OWUI_TOKEN=""

owui_api() {
    local method="$1" path="$2" data="$3"
    local url="http://127.0.0.1:${OPENWEBUI_PORT:-8080}${path}"
    if [ -n "$data" ]; then
        curl -s -X "$method" "$url" \
            -H "Authorization: Bearer $OWUI_TOKEN" \
            -H "Content-Type: application/json" \
            -d "$data" 2>/dev/null
    else
        curl -s -X "$method" "$url" \
            -H "Authorization: Bearer $OWUI_TOKEN" 2>/dev/null
    fi
}

owui_configure_models() {
    print_substep "Configuring Open WebUI models (capabilities & visibility)..."
    
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

owui_ensure_admin() {
    local owui_url="http://127.0.0.1:${OPENWEBUI_PORT:-8080}"
    local admin_email="razzfazz-ai-admin@${MAIN_DOMAIN}"
    local admin_pass="${AUTHENTIK_BOOTSTRAP_PASSWORD}"
    local admin_name="razzfazz.ai Admin"
    
    # Try login first (user might already exist)
    local login_resp
    login_resp=$(curl -s -X POST "$owui_url/api/v1/auths/signin" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"${admin_email}\",\"password\":\"${admin_pass}\"}" 2>/dev/null)
    
    OWUI_TOKEN=$(echo "$login_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
    
    if [ -n "$OWUI_TOKEN" ]; then
        print_success "Logged in as existing admin ($admin_email)."
        return 0
    fi
    
    # Try signup (first user becomes admin)
    print_substep "Creating admin user ($admin_email)..."
    local signup_resp
    signup_resp=$(curl -s -X POST "$owui_url/api/v1/auths/signup" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"${admin_email}\",\"password\":\"${admin_pass}\",\"name\":\"${admin_name}\"}" 2>/dev/null)
    
    OWUI_TOKEN=$(echo "$signup_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)

    if [ -n "$OWUI_TOKEN" ]; then
        print_success "Admin user created."
        # rc6.7 #75: backfill `username` on the admin user record. OpenWebUI's
        # /api/v1/auths/signup payload accepts {email, password, name} — there
        # is no username field, so the column stays NULL after local-password
        # signup. Per-user agent pipes (M020 hermes/moltis/opencode/openhands/
        # paperclip) build their X-Authentik-Username from
        # `__user__["username"] or __user__["id"]` — when username is NULL,
        # they fall through to the OpenWebUI internal UUID, agent-manager
        # derives a different user_slug, and the find returns 404 so every
        # pipe greets the user with "You don't have a … agent yet" even
        # right after they provisioned one. OAUTH_USERNAME_CLAIM=preferred_username
        # IS set in env, but it only fires on OIDC login; admins seeded via
        # this signup flow never trigger it. Backstop here with a SQL update
        # mirroring the Authentik username (the local part of the admin
        # email is "razzfazz-ai-admin", but Authentik knows them as
        # "akadmin" — so we use the explicit operator-facing slug).
        docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
            "UPDATE \"user\" SET username='akadmin' WHERE email='${admin_email}' AND username IS NULL;" \
            > /dev/null 2>&1 || true
        return 0
    fi
    
    # Signup disabled (user exists but password wrong) — reset via DB
    print_substep "Resetting admin password via database..."
    
    # Find existing admin email (may differ from current domain)
    local existing_email
    existing_email=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -t -c \
        "SELECT a.email FROM auth a JOIN \"user\" u ON a.id = u.id WHERE u.role = 'admin' LIMIT 1;" 2>/dev/null | xargs)
    
    if [ -z "$existing_email" ]; then
        print_error "No admin user found in database."
        return 1
    fi
    
    if [ "$existing_email" != "$admin_email" ]; then
        print_info "Found existing admin: $existing_email (updating to $admin_email)"
        # Update email to match current domain
        docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
            "UPDATE auth SET email='${admin_email}' WHERE email='${existing_email}';" > /dev/null 2>&1
        docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
            "UPDATE \"user\" SET email='${admin_email}' WHERE email='${existing_email}';" > /dev/null 2>&1
    fi
    
    local hash
    hash=$(docker exec openwebui python3 -c "
import bcrypt
print(bcrypt.hashpw(b'${admin_pass}', bcrypt.gensalt(12)).decode())
" 2>/dev/null)
    
    if [ -z "$hash" ]; then
        print_error "Failed to generate password hash."
        return 1
    fi
    
    docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
        "UPDATE auth SET password='${hash}' WHERE email='${admin_email}';" > /dev/null 2>&1
    # rc6.7 #75: same username backfill on the password-reset path —
    # see the matching block in the signup branch above.
    docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
        "UPDATE \"user\" SET username='akadmin' WHERE email='${admin_email}' AND username IS NULL;" \
        > /dev/null 2>&1 || true

    # Retry login
    login_resp=$(curl -s -X POST "$owui_url/api/v1/auths/signin" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"${admin_email}\",\"password\":\"${admin_pass}\"}" 2>/dev/null)
    
    OWUI_TOKEN=$(echo "$login_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
    
    if [ -n "$OWUI_TOKEN" ]; then
        print_success "Admin password reset and logged in."
        return 0
    fi
    
    print_error "Failed to authenticate with Open WebUI."
    return 1
}

owui_configure_connection() {
    print_substep "Configuring GPUStack OpenAI-compatible connection..."
    local resp
    resp=$(owui_api POST "/api/v1/configs/import" "{
        \"config\": {
            \"openai\": {
                \"enable\": true,
                \"api_base_urls\": [\"http://gpustack:9090/v1-openai\"],
                \"api_keys\": [\"${GPUSTACK_API_KEY}\"],
                \"api_configs\": {
                    \"0\": {
                        \"enable\": true,
                        \"tags\": [],
                        \"prefix_id\": \"\",
                        \"model_ids\": [],
                        \"connection_type\": \"external\",
                        \"auth_type\": \"bearer\"
                    }
                }
            },
            \"ui\": {
                \"enable_signup\": false
            }
        }
    }")
    
    if echo "$resp" | python3 -c "import sys,json; assert json.load(sys.stdin)['openai']['enable']" 2>/dev/null; then
        print_success "GPUStack connection configured."
    else
        print_warning "Could not verify connection config."
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
    print_substep "Configuring embedding model ($emb_alias via GPUStack)..."
    local resp
    resp=$(owui_api POST "/api/v1/retrieval/embedding/update" "{
        \"RAG_EMBEDDING_ENGINE\": \"openai\",
        \"RAG_EMBEDDING_MODEL\": \"$emb_alias\",
        \"RAG_EMBEDDING_BATCH_SIZE\": 1,
        \"ENABLE_ASYNC_EMBEDDING\": false,
        \"RAG_EMBEDDING_CONCURRENT_REQUESTS\": 1,
        \"openai_config\": {
            \"url\": \"http://gpustack:9090/v1-openai\",
            \"key\": \"${GPUSTACK_API_KEY}\"
        }
    }")

    if echo "$resp" | python3 -c "import sys,json; assert json.load(sys.stdin).get('RAG_EMBEDDING_MODEL') == '$emb_alias'" 2>/dev/null; then
        print_success "Embedding model configured ($emb_alias)."
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

owui_configure_retrieval_env() {
    # NOTE: The retrieval config/update API has a bug in Open WebUI 0.8.12
    # (references non-existent FETCH_URL_MAX_CONTENT_LENGTH config key).
    # Workaround: set via environment variables in modules/chat/compose.yml
    # These are read by PersistentConfig on first startup and stored in DB.
    print_substep "Setting retrieval config via environment variables..."
    
    local compose_file="${SCRIPT_DIR}/modules/chat/compose.yml"
    local env_vars_needed=(
        "ENABLE_RAG_HYBRID_SEARCH=true"
        "HYBRID_BM25_WEIGHT=0.5"
        "RAG_RERANKING_MODEL=qwen3-reranker"
        "RAG_RERANKING_ENGINE=external"
        # OpenWebUI's external reranker POSTs the body to RAG_EXTERNAL_RERANKER_URL
        # AS-IS — it does NOT append "/rerank" the way the embedding code appends
        # "/embeddings" to the OpenAI base URL. The full endpoint must be given
        # here, otherwise every retrieval emits "404 Client Error" + a silent
        # fall-back to unranked vector hits, dropping retrieval quality.
        # See memory: project_openwebui_external_reranker_url.md.
        "RAG_EXTERNAL_RERANKER_URL=http://gpustack:9090/v1/rerank"
        "RAG_EXTERNAL_RERANKER_API_KEY=${GPUSTACK_API_KEY}"
        # Chunking defaults for short business documents (offers/contracts/price
        # lists): the markdown-header splitter (default ON) splits each doc per
        # ## section, orphaning the facts (rates) from the entity (client name in
        # the header) — retrieval then can't connect "client + topic". Turn it OFF
        # and use a whole-document character chunk (≤ qwen3-embedding's 2048-token
        # ctx ≈ ~5000 chars) so client + facts stay in one chunk. Also disable the
        # LLM retrieval-query rewrite (it mistranslates domain terms, e.g.
        # "Tagsätze"→bank "Tagesgeldsatz"). See project_owui_rag_recipe (2026-06-13).
        "ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER=false"
        "CHUNK_SIZE=5000"
        "CHUNK_OVERLAP=500"
        "ENABLE_RETRIEVAL_QUERY_GENERATION=false"
        "RAG_TOP_K=10"
        "ENABLE_WEB_SEARCH=true"
        "WEB_SEARCH_ENGINE=searxng"
        "SEARXNG_QUERY_URL=http://searxng:8080/search?q=<query>"
        "WEB_SEARCH_RESULT_COUNT=5"
        "RAG_OPENAI_API_BASE_URL=http://gpustack:9090/v1-openai"
        "RAG_OPENAI_API_KEY=${GPUSTACK_API_KEY}"
    )
    
    # Add env vars to the openwebui service environment in compose
    # We do this by updating .env which is referenced by the compose file
    for var in "${env_vars_needed[@]}"; do
        local key="${var%%=*}"
        local val="${var#*=}"
        update_env_value "$ENV_FILE" "$key" "$val"
    done
    
    print_success "Retrieval env vars set in .env (seed for fresh first-start)."

    # AUTHORITATIVE: env vars are only read by OWUI's PersistentConfig on the FIRST
    # start, so on a re-run / already-booted stack they are silently ignored and the
    # document/web-search settings end up missing. Push the same settings via the
    # retrieval config API (GET -> merge -> POST) so they land in the DB regardless.
    print_substep "Applying retrieval (RAG) + web search via OWUI API (persists in DB)..."
    local cfg merged
    cfg=$(owui_api GET "/api/v1/retrieval/config")
    merged=$(printf '%s' "$cfg" | GPUSTACK_API_KEY="$GPUSTACK_API_KEY" python3 -c "
import sys, json, os
key = os.environ.get('GPUSTACK_API_KEY', '')
try:
    d = json.load(sys.stdin)
    assert isinstance(d, dict)
except Exception:
    sys.exit(1)
d.pop('status', None)
d.update({
    'ENABLE_RAG_HYBRID_SEARCH': True,
    'HYBRID_BM25_WEIGHT': 0.5,
    'RAG_RERANKING_MODEL': 'qwen3-reranker',
    'RAG_RERANKING_ENGINE': 'external',
    'RAG_EXTERNAL_RERANKER_URL': 'http://gpustack:9090/v1/rerank',
    'RAG_EXTERNAL_RERANKER_API_KEY': key,
    'ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER': False,
    'CHUNK_SIZE': 5000,
    'CHUNK_OVERLAP': 500,
    'TOP_K': 10,
})
w = d.setdefault('web', {})
w.update({
    'ENABLE_WEB_SEARCH': True,
    'WEB_SEARCH_ENGINE': 'searxng',
    'SEARXNG_QUERY_URL': 'http://searxng:8080/search?q=<query>',
    'WEB_SEARCH_RESULT_COUNT': 5,
})
print(json.dumps(d))
" 2>/dev/null)
    if [ -z "$merged" ]; then
        print_warning "  Could not read OWUI retrieval config — left env-var seed only."
    else
        owui_api POST "/api/v1/retrieval/config/update" "$merged" >/dev/null 2>&1
        local check
        check=$(owui_api GET "/api/v1/retrieval/config" | python3 -c "import sys,json; d=json.load(sys.stdin); print('rerank='+str(d.get('RAG_RERANKING_MODEL')), 'web='+str(d.get('web',{}).get('ENABLE_WEB_SEARCH')))" 2>/dev/null)
        print_success "Retrieval + web search applied via API ($check)."
    fi
    print_info "  Web search: SearXNG, Hybrid search: enabled, Reranker: qwen3-reranker"
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

    wait_for_service "Open WebUI" "http://127.0.0.1:${OPENWEBUI_PORT:-8080}/health" 120
    owui_ensure_admin
    owui_configure_connection
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

run_verification_suite() {
    print_step "Running verification suite..."
    VERIFY_PASS=0
    VERIFY_FAIL=0
    VERIFY_RESULTS=""
    
    load_env
    local api_key="${GPUSTACK_API_KEY}"
    
    # Disable errexit for verification (checks may fail intentionally)
    set +e
    
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

    local owui_token
    owui_token=$(curl -sf --max-time 10 -X POST \
        "http://127.0.0.1:${OPENWEBUI_PORT:-8080}/api/v1/auths/signin" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"razzfazz-ai-admin@${MAIN_DOMAIN}\",\"password\":\"${AUTHENTIK_BOOTSTRAP_PASSWORD}\"}" 2>/dev/null | \
        python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
    if [ -n "$owui_token" ]; then
        verify_check "Open WebUI: admin login" "pass"
    else
        verify_check "Open WebUI: admin login" "fail" "login failed"
    fi
    

    
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
            verify_check "Open WebUI: GPUStack connection configured" "pass"
        else
            verify_check "Open WebUI: GPUStack connection configured" "fail" "not enabled or key unresolved"
        fi
    fi
    
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
    print_substep "Checking Authentik..."
    local authentik_health
    authentik_health=$(docker exec authentik-server curl -sf --max-time 10 \
        "http://localhost:9000/-/health/live/" 2>/dev/null)
    if [ $? -eq 0 ]; then
        verify_check "Authentik: healthy" "pass"
    else
        verify_check "Authentik: healthy" "fail" "unreachable"
    fi

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
    local hw="${HARDWARE:-amd}"
    local runners_dir="${SCRIPT_DIR}/modules/llm/runners"
    if [ ! -d "$runners_dir" ]; then
        return 0
    fi
    _build_one() {
        # $1 image:tag, $2 dockerfile (relative to runners_dir's parent)
        local img="$1" df="$2"
        if docker image inspect "$img" >/dev/null 2>&1; then
            return 0
        fi
        print_step "Building $img (llama-server-shim runner image)..."
        if docker build -t "$img" -f "${SCRIPT_DIR}/$df" "$runners_dir" 2>&1 | tail -5; then
            print_success "$img built."
        else
            print_warning "$img build failed — models targeting the matching backend will fail to start."
            print_info "  Re-run manually: docker build -t $img -f $df modules/llm/runners"
        fi
    }
    if [ "$hw" = "amd" ]; then
        _build_one "llama-vulkan-runner:b8943"     "modules/llm/runners/llama-vulkan/Dockerfile"
        _build_one "llama-rocm-runner:rocm-7.2.1" "modules/llm/runners/llama-rocm/Dockerfile"
    fi
    if [ "$hw" = "cpu" ]; then
        _build_one "llama-cpu-runner:b8000"        "modules/llm/runners/llama-cpu/Dockerfile"
    fi
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
    local built=0 present=0 failed=0 svc img
    while IFS=$'\t' read -r svc img; do
        [ -z "$svc" ] && continue
        # Skip when the resolved image already exists locally (init-built /
        # prior run). The enumerator resolves the derived name for image-less
        # services, so `img` is populated for every service and this skip-check
        # is always meaningful (a re-run is a fast, no-op present-check).
        if [ -n "$img" ] && docker image inspect "$img" >/dev/null 2>&1; then
            present=$((present + 1))
            continue
        fi
        print_substep "Building ${svc}${img:+ ($img)}..."
        if COMPOSE_FILE="$_build_cf" COMPOSE_PROFILES="$all_profiles" docker compose build "$svc" >/dev/null 2>&1; then
            print_success "Built ${svc}."
            built=$((built + 1))
        else
            print_warning "Build of '${svc}' failed (non-fatal) — enabling it later may trigger a live build. Re-run: docker compose build ${svc}"
            failed=$((failed + 1))
        fi
    done <<< "$rows"

    print_success "Custom-image pre-build done (built=$built, already-present=$present, failed=$failed)."
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
    local placeholder="gpustack_CHANGEME_AFTER_FIRST_START" real_key
    real_key=$(read_env_value "$ENV_FILE" GPUSTACK_API_KEY)
    if [ -z "$real_key" ] || [ "$real_key" = "$placeholder" ]; then
        print_warning "OWUI key-fix: live GPUStack key not available yet — skipping persisted-key rewrite."
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
        print_substep "OWUI persisted OpenAI-connection key rewritten to the live GPUStack key (config.${target_col})."
    else
        print_warning "OWUI key-fix: persisted-key UPDATE returned non-zero (non-fatal) — OWUI may still show 0 models."
    fi
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
    case ",$(read_env_value "$ENV_FILE" COMPOSE_PROFILES)," in
        *,llm-legacy,*) msync="model-sync-legacy" ;;
        *,llm-cpu,*)    msync="model-sync-cpu" ;;
        *,llm,*)        msync="model-sync" ;;
    esac
    [ -z "$msync" ] && return 0
    if docker compose up -d --no-deps --force-recreate "$msync" > /dev/null 2>&1; then
        print_substep "model-sync ($msync) recreated with the live GPUStack key (--no-deps)."
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
#   (b) force-relaunch every RUNNING sandboxed coding-agent instance via
#       provisioner.upgrade(force=True) so their baked OPENAI_API_KEY /
#       GPUSTACK_API_KEY are re-resolved to the live key.
# Profile-gated (agents) + best-effort. No-op on a fresh box (no instances yet —
# the user self-provisions later against the now-correct agent-manager).
rekey_coding_agents() {
    echo "${COMPOSE_PROFILES:-}" | grep -qw "agents" || return 0
    docker ps --format '{{.Names}}' | grep -qx "agent-manager" || return 0
    print_step "Coding agents: re-keying to the live GPUStack key..."

    # (a) recreate agent-manager so its env reloads the real GPUSTACK_API_KEY.
    if docker compose up -d --no-deps --force-recreate agent-manager > /dev/null 2>&1; then
        print_substep "agent-manager recreated with the live GPUStack key (--no-deps)."
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
    docker exec agent-manager python3 -c '
import json
from app import create_app
from app.services.provisioner import SANDBOXED_TYPES
app = create_app()
rekeyed = []; skipped = []; failed = []
with app.app_context():
    for inst in app.db.get_all_instances():
        if inst["agent_type"] not in SANDBOXED_TYPES:
            continue
        if inst.get("state") != "running":
            skipped.append((inst.get("container_name"), inst.get("state")))
            continue
        who = inst.get("user_id") or inst.get("user_slug") or "akadmin"
        try:
            iid, msg = app.provisioner.upgrade(inst["id"], who, force=True)
            (rekeyed if iid else failed).append((inst.get("container_name"), msg))
        except Exception as e:
            failed.append((inst.get("container_name"), repr(e)))
print("REKEY_JSON:" + json.dumps({"rekeyed": rekeyed, "skipped": skipped, "failed": failed}))
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
        _md160=$(grep -E '^MAIN_DOMAIN=' "${SCRIPT_DIR}/.env" 2>/dev/null | cut -d= -f2- | tr -d '"')
        _dd160=$(grep -E '^DIFY_DOMAIN=' "${SCRIPT_DIR}/.env" 2>/dev/null | cut -d= -f2- | tr -d '"')
        _dd160="${_dd160//\$\{MAIN_DOMAIN\}/$_md160}"; _dd160="${_dd160//\$MAIN_DOMAIN/$_md160}"
        if [ -n "$_dd160" ] && ! printf '%s' "$_dd160" | grep -q '[${]'; then
            for _k in CONSOLE_API_URL CONSOLE_WEB_URL APP_API_URL APP_WEB_URL FILES_URL TRIGGER_URL WEB_API_CORS_ALLOW_ORIGINS CONSOLE_CORS_ALLOW_ORIGINS; do
                grep -qE "^${_k}=https?://" "${SCRIPT_DIR}/.env.dify" 2>/dev/null \
                    && sed -i -E "s#^(${_k})=https?://.*#\\1=https://${_dd160}#" "${SCRIPT_DIR}/.env.dify"
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
    if echo ",${COMPOSE_PROFILES:-}," | grep -qE ',chat,|,llm,|,llm-legacy,|,llm-cpu,'; then
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
# Profile-gated (llm / llm-legacy / llm-cpu) + best-effort (never aborts the refresh).
refresh_ensure_default_models_deployed() {
    local active_profiles
    active_profiles=$(read_env_value "$ENV_FILE" COMPOSE_PROFILES)
    if ! echo ",${active_profiles}," | grep -qE ',llm,|,llm-legacy,|,llm-cpu,'; then
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
    local default_aliases
    default_aliases=$(YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os
try:
    spec = yaml.safe_load(open(os.environ['YAML_PATH'])) or {}
except Exception:
    spec = {}
seen = []
for v in (spec.get('defaults') or {}).values():
    if v and v not in seen:
        seen.append(v)
print('\n'.join(seen))
" 2>/dev/null)
    if [ -z "$default_aliases" ]; then
        print_warning "standard-models.yaml defaults block unreadable — skipping default-model ensure."
        return 0
    fi

    # Query the live REGISTERED model set once (gpustack native management API:
    # /v1/models on 0.7.x, /v2/models on 2.x — resolved by gpustack_api_prefix).
    # A name listed here is registered/deployed regardless of replica count.
    local prefix registered
    prefix=$(gpustack_api_prefix)
    registered=$(gpustack_api GET "${prefix}/models" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
for m in d.get('items', []):
    n = m.get('name')
    if n:
        print(n)
" 2>/dev/null)

    # Which defaults are MISSING from the registered set?
    local missing="" a
    while IFS= read -r a; do
        [ -z "$a" ] && continue
        if ! printf '%s\n' "$registered" | grep -qxF "$a"; then
            missing="${missing:+$missing }$a"
        fi
    done <<< "$default_aliases"

    if [ -z "$missing" ]; then
        print_success "All default models already registered ($(printf '%s' "$default_aliases" | tr '\n' ' '))— skipping deploy (customer models preserved)."
        return 0
    fi

    print_info "Missing default model(s): ${missing} — deploying the standard preset set (idempotent; already-registered models are skipped)."
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
    local chat_alias
    chat_alias=$(YAML_PATH="$SCRIPT_DIR/core/llm/standard-models.yaml" python3 -c "
import yaml, os
try:
    spec = yaml.safe_load(open(os.environ['YAML_PATH'])) or {}
except Exception:
    spec = {}
print(((spec.get('defaults') or {}).get('chat') or ''))
" 2>/dev/null)
    if [ -n "$chat_alias" ]; then
        local m chat_was_missing=0
        for m in $missing; do [ "$m" = "$chat_alias" ] && chat_was_missing=1; done
        if [ "$chat_was_missing" = "1" ]; then
            wait_for_model "$chat_alias" 2400 \
                || print_warning "Default chat model '$chat_alias' not READY within the wait window (non-fatal) — it will finish downloading in the background."
        fi
    fi
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
    if docker exec -d razzfazz-help python3 -c "
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
    if curl -fsS --max-time 5 "http://127.0.0.1:${GPUSTACK_PORT:-9090}/healthz" >/dev/null 2>&1; then
        ensure_gpustack_api_key
        # rc6.9: --refresh now also re-registers custom backends. The
        # M029-S04 LLM Runtime toggle resets gpustack_db when flipping
        # between v0.7.1 and v2.x (their alembic schemas are
        # incompatible) — that wipes the custom backend registrations
        # too. Operators who flip to v2.x then ran `--refresh` (per
        # the post-upgrade reminder block) lost the v2.x custom
        # backends until they figured out they also needed --preset.
        # Idempotent: init-backends.py skips if backends already match.
        # Gate on active profile: init-backends targets the v2.x
        # /v2/inference-backends endpoint, which v0.7.1 doesn't expose
        # (404). The custom-backend mechanism is a v2.x concept; on
        # llm-legacy / llm-cpu the bundled gpustack runner does the job.
        # NB: NOT `local` — this --refresh block runs at script-top-level
        # (the enclosing refresh_help_cache() closes ~line 2610). Sister to
        # the line-2716 fix (commit 0776cb4e); this second occurrence in the
        # --refresh path bailed bash with "local: can only be used in a
        # function" right after the API-key check (hit on 0.91 during the
        # 2026-05-24 domain-change reconcile). M033 S28.
        active_profiles=$(read_env_value .env COMPOSE_PROFILES)
        if [ -f "${SCRIPT_DIR}/modules/llm/gpustack/init-backends.py" ] \
           && echo ",$active_profiles," | grep -q ',llm,'; then
            print_step "Refreshing GPUStack custom backends..."
            if GPUSTACK_API="http://127.0.0.1:${GPUSTACK_PORT:-9090}" \
               GPUSTACK_API_KEY="$(read_env_value .env GPUSTACK_API_KEY)" \
               python3 "${SCRIPT_DIR}/modules/llm/gpustack/init-backends.py"; then
                print_success "GPUStack custom backends up to date."
            else
                print_warning "Backend registration exited non-zero — model deploys may stall."
                print_info "  Re-run manually: python3 modules/llm/gpustack/init-backends.py"
            fi
        elif echo ",$active_profiles," | grep -qE ',llm-legacy,|,llm-cpu,'; then
            print_substep "Skipping custom-backend registration (llm-legacy / llm-cpu uses bundled runner — no v2 backends needed)."
        fi
    else
        print_warning "GPUStack not reachable on http://127.0.0.1:${GPUSTACK_PORT:-9090} — skipping API-key refresh."
        print_info "  Re-run --refresh once GPUStack is up, or set the key manually with:"
        print_info "    rzfz setup --set-gpustack-api-key <KEY>"
    fi
    refresh_llama_vulkan_runner
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
        step_dify_provisioning || print_warning "Dify init on --refresh reported an issue (non-fatal) — re-run 'rzfz post-install --refresh' or check the Dify console."
    fi
    refresh_help_cache
    openlit_align_admin
    # ga.6: provision MCP registry on --refresh too (was --preset-only — upgraded
    # boxes following the documented post-upgrade --refresh flow otherwise ended up
    # with an empty COGNEE_MCP_API_KEY + a non-functional cognee-mcp sidecar).
    # Profile-gated (no-op without cognee) + idempotent (skips if key already set).
    provision_mcp_registry
    ensure_mcp_manager_secret
    reconcile_authentik_bindings
    # §16 handover: refresh the as-built day-1 security-posture assessment too, so
    # an upgraded box carries a current report (matches the --preset path).
    run_security_selfcheck
    print_success "Refresh complete."
    if [ "$DO_VERIFY" = false ]; then
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
    # Step 1: DNS
    if [ "$SKIP_DNS" = false ]; then
        setup_local_dns
    fi

    # Step 2: GPUStack API Key
    wait_for_service "GPUStack" "http://127.0.0.1:${GPUSTACK_PORT:-9090}/healthz" 120
    ensure_gpustack_api_key

    # Step 2b (rc6.7 #64 BLOCKER on fresh init): register the custom inference
    # backends (llama-box-vulkan-custom / llama-box-rocm-custom /
    # llama-box-cpu-custom) BEFORE deploy_models tries to use them. The same
    # call lives in razzfazz-init.sh near the end, but on first install it
    # runs BEFORE GPUSTACK_API_KEY is populated by ensure_gpustack_api_key
    # (which only happens here in post-install) — so the init-time
    # invocation prints "skipping backend registration" and exits clean.
    # Without backends registered, every model deploy hangs at
    # `state_message: No backend versions are available for GPU device
    # (rocm)` and post-install times out per model after 1 hour each.
    # Idempotent: init-backends.py skips if backends already match.
    # NB: gate on -f, not -x — init-backends.py is committed mode 0644 and
    # invoked via `python3`, so the executable bit is meaningless. The
    # original rc6.7 #64 commit used -x and the whole block was silently
    # skipped on every fresh install (rc6.7 #67).
    # BUG-3 (2026-05-16): gate on active profile, same as the --refresh path
    # at line 2633. init-backends targets the v2.x /v2/inference-backends
    # endpoint; v0.7.1 (llm-legacy) and llm-cpu's bundled runner don't
    # expose it and return 404 on every POST. Without this gate, fresh
    # installs of the single-box / master-cpu / testvm-cpu presets all
    # see three "POST /v2/inference-backends failed: HTTP 404" failures
    # and post-install exits rc=1 even though the runtime is fine.
    # NB: NOT `local` — this block runs at script-top-level (the enclosing
    # function closes at line ~2610 with refresh_help_cache). Pre-fix, bash
    # bailed with "local: can only be used in a function" right after
    # "[✓] Existing API key is valid." on operator runs of post-install in
    # the default preset path (culturehack-001, 2026-05-21).
    _active_profiles_post=$(read_env_value .env COMPOSE_PROFILES)
    if [ -f "${SCRIPT_DIR}/modules/llm/gpustack/init-backends.py" ] \
       && echo ",$_active_profiles_post," | grep -q ',llm,'; then
        print_step "Registering GPUStack custom backends (post-install)..."
        if GPUSTACK_API="http://127.0.0.1:${GPUSTACK_PORT:-9090}" \
           GPUSTACK_API_KEY="$(read_env_value .env GPUSTACK_API_KEY)" \
           python3 "${SCRIPT_DIR}/modules/llm/gpustack/init-backends.py"; then
            print_success "GPUStack custom backends registered."
        else
            print_warning "Backend registration exited non-zero — model deploys may stall."
            print_info "  Re-run manually: python3 modules/llm/gpustack/init-backends.py"
        fi
    elif echo ",$_active_profiles_post," | grep -qE ',llm-legacy,|,llm-cpu,'; then
        print_substep "Skipping custom-backend registration (llm-legacy / llm-cpu uses bundled runner — no v2 backends needed)."
    fi

    # Step 3: Deploy Models
    if [ "$SKIP_MODELS" = false ]; then
        deploy_all_models "$PRESET"
        
        if [ "$SKIP_WAIT" = false ]; then
            # Never let a model-wait failure abort the rest of provisioning.
            # wait_for_all_models already loudly reports critical vs optional
            # failures; downstream service config (OWUI / Dify / Speaches) and
            # the verify suite must still run so the operator gets a fully
            # configured box + an accurate report instead of a half-provisioned
            # one. (Under `set -e` a bare call here aborted everything when a
            # single optional model — e.g. the reranker — failed to start.)
            wait_for_all_models "$PRESET" || true
        else
            print_info "Skipping model wait (--skip-wait). Models downloading in background."
        fi
    fi
    
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

    # Steps 5-7 are best-effort: a failure inside any single service-provisioning
    # step (a flaky marketplace plugin, an unreachable model download, a service
    # still warming up) must NOT abort the remaining steps or the final verify
    # suite under `set -e`. Each step already logs its own progress/warnings; the
    # verify suite at the end reports whatever didn't come up so the operator gets
    # an accurate, complete picture instead of a half-provisioned box.

    # Step 5: Open WebUI provisioning
    step_openwebui_provisioning || print_warning "Open WebUI provisioning reported an issue — see verify report below."

    # Step 6: Dify provisioning
    step_dify_provisioning || print_warning "Dify provisioning reported an issue — see verify report below."

    # Step 7: Speaches + auxiliary
    step_speaches_provisioning || print_warning "Speaches provisioning reported an issue — see verify report below."
    
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

        # Wait for onyx-api to be healthy
        local waited=0
        while [ $waited -lt 120 ]; do
            if docker exec onyx-api python3 -c \
                "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health', timeout=3)" \
                > /dev/null 2>&1; then
                break
            fi
            sleep 5; waited=$((waited + 5))
        done
        if [ $waited -ge 120 ]; then
            print_warning "Onyx API not ready after 120s — skipping provisioning"
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
GPUSTACK_BASE = "http://gpustack:9090/v1"
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "docker")
POSTGRES_PASS = os.environ.get("POSTGRES_PASSWORD", "")
POSTGRES_DB   = os.environ.get("POSTGRES_DB", "onyx_db")
DEFAULT_CHAT  = os.environ.get("DEFAULT_CHAT", "qwen3.6")

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

# 5. Search settings (nomic-embed-text)
s, r = req("POST", "/search-settings/set-new-search-settings", {
    "model_name": "openai/nomic-embed-text", "normalize": True,
    "query_prefix": "", "passage_prefix": "", "api_url": GPUSTACK_BASE,
    "provider_type": "litellm", "api_key": GPUSTACK_KEY, "model_dim": 768,
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
    llm_base_url=os.environ.get('LLM_BASE_URL','http://gpustack:9090/v1'),
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

promote_or_create('akadmin', 'razzfazz-ai-admin@${MAIN_DOMAIN}', '${AUTHENTIK_BOOTSTRAP_PASSWORD}')
promote_or_create('razzfazz-ai-admin', 'razzfazz-ai-admin@${MAIN_DOMAIN}', '${AUTHENTIK_BOOTSTRAP_PASSWORD}')
" 2>&1 | while IFS= read -r line; do print_substep "$line"; done \
            && print_success "Paperless-ngx: users promoted + brand colour set." \
            || print_warning "Paperless-ngx: provisioning failed"
    }
    step_paperless_provisioning

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
    echo "  Services configured:"
    echo "    ✓ GPUStack   — API key + models deployed"
    echo "    ✓ Open WebUI — endpoint, audio, embedding, search"
    echo "    ✓ Dify       — plugins installed"
    echo "    ✓ Speaches   — STT + TTS models downloaded"
    echo "    ✓ LightRAG   — model names in .env"
    echo "    ✓ Cognee     — model names in .env"
    echo ""
    if [ "$SKIP_MODELS" = false ] && [ "$SKIP_WAIT" = true ]; then
        echo -e "  ${YELLOW}Note: Models are still downloading in background.${NC}"
        echo "  Check progress: https://llm.${MAIN_DOMAIN}"
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
    if echo "${COMPOSE_PROFILES:-}" | grep -qw "paperless-ngx"; then
        if [ "$m007_notes" = false ]; then
            echo "  M007 manual setup required:"
            m007_notes=true
        fi
        echo "    • Paperless-ngx superuser:"
        echo "      docker exec -it paperless-ngx python manage.py createsuperuser"
    fi
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
