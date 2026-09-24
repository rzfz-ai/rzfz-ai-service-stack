#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# cli/lib-llm-manager-deploy.sh — deploy + verify the standard model set on an
# LLM-Manager box (#1250b)
# =============================================================================
# #976 stopped post-install from deploying the standard model set into a
# GPUStack that a Manager box does not run. It never put anything in its place:
# `rzfz post-install --preset standard` printed "No GPUStack profile active —
# skipping GPUStack model deploy" and deployed NOTHING. `.env` was still wired
# to `qwen3.6` / `qwen3-embedding`, OWUI/Cognee/LightRAG were pointed at those
# names, and the manager's /api/deployments stayed `[]` — a chat UI with no
# model. `--verify` then reported three red GPUStack lines carrying model names
# that box never had (`nomic-embed-text`). This library is the missing half.
#
# TWO WIRES, deliberately different, because the manager gates them differently:
#
#   • DEPLOY — the admin surface (`/api/*`) is anchored on the ingress SOURCE IP
#     (app/authz.py::from_caddy): a co-resident container with forged
#     `X-Authentik-*` headers must not be able to provision capacity. So the
#     deploy calls run `wget` INSIDE the caddy container, which is the only
#     peer the manager accepts, carrying the same forward-auth identity Caddy
#     injects for a console session. Verified: BusyBox wget honours a
#     caller-supplied `Content-Type: application/json` (it does not append its
#     own form-urlencoded one), which FastAPI needs to parse the body.
#   • VERIFY — the `/v1` hot path is key-authed (rzfz-sk), NOT ingress-anchored,
#     and the manager publishes it on loopback (127.0.0.1:${LLM_MANAGER_PORT}).
#     So the verify probes are plain host `curl`, exactly like the GPUStack
#     checks they replace, using the stack/openwebui service key from `.env`.
#
# The model SET comes from `core/llm/standard-models.yaml` — the same single
# source `cli/post-install.sh::_model_rows_for_preset` and
# `update_env_model_config` read, so what gets deployed cannot drift from what
# `.env` tells the consumers to ask for.
#
# Sourced library: NO top-level `set` line (options would leak into every
# sourcing shell, #382). Requires scripts/lib.sh (print_*, read_env_value) to be
# sourced first.
# =============================================================================

# Repo root for the manifest — SCRIPT_DIR when the caller set it (post-install
# does), else derived from this file's location.
_LLMM_REPO_ROOT="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# ── admin API (ingress-anchored) ─────────────────────────────────────────────
# _llmm_admin_api <GET|POST> <path> [json-body]
# Prints the response body on stdout and returns 0 on success; on failure
# prints wget's diagnostic (which carries the HTTP status line — BusyBox wget
# discards the error BODY) and returns non-zero. Never aborts a `set -e`
# caller: the substitution's status is captured, not inherited.
_llmm_admin_api() {
    local method="$1" path="$2" body="${3:-}" out rc=0
    local url="http://llm-manager:8080${path}"
    # #1148 review: this default was a bare `akadmin`, and LLM_MANAGER_ADMIN_USER
    # is set nowhere in the repo — so the default always won. On a new box the
    # operator is `rzfz-admin` while post-install deployed the standard set as
    # `akadmin`, and the two names drifted apart in whatever the manager hangs
    # off the identity (usage attribution, key ownership). Follow the same
    # variable the rest of the stack follows; `akadmin` stays as the last
    # resort for a box that predates the rename.
    local user="${LLM_MANAGER_ADMIN_USER:-${RAZZFAZZ_ADMIN_USERNAME:-akadmin}}"
    # The manager's admin tier is driven by Authentik group membership
    # (LLM_MANAGER_ADMIN_GROUPS, comma-separated in .env). Caddy forwards them
    # PIPE-separated, so translate; fall back to the shipped defaults.
    local groups
    groups=$(read_env_value "${ENV_FILE:-.env}" LLM_MANAGER_ADMIN_GROUPS 2>/dev/null) || groups=""
    [ -n "$groups" ] || groups="razzfazz.ai Super Admins,authentik Admins"
    groups=$(printf '%s' "$groups" | tr ',' '|')
    if [ "$method" = "GET" ]; then
        out=$(docker exec caddy wget -q -O - \
            --header="X-Authentik-Username: $user" \
            --header="X-Authentik-Groups: $groups" \
            "$url" 2>&1) || rc=$?
    else
        out=$(docker exec caddy wget -q -O - \
            --header="X-Authentik-Username: $user" \
            --header="X-Authentik-Groups: $groups" \
            --header="Content-Type: application/json" \
            --post-data="$body" \
            "$url" 2>&1) || rc=$?
    fi
    # #2156: BusyBox wget discards a 4xx BODY, so a refusal reached the log as
    # "409 Conflict" and nothing more — while the manager had written a full
    # sentence ("would over-subscribe vukos-box: 126.9 GB requested … > 27.5 GB
    # usable"). Recover it, best-effort, with the curl caddy carries; a caddy
    # without curl, or a body that is not the manager's JSON, changes nothing.
    if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qE 'server returned error: HTTP/1\.[01] 4'; then
        local detail=""
        if [ "$method" = "GET" ]; then
            detail=$(docker exec caddy curl -sS --max-time 15 \
                -H "X-Authentik-Username: $user" -H "X-Authentik-Groups: $groups" \
                "$url" 2>/dev/null) || detail=""
        else
            detail=$(docker exec caddy curl -sS --max-time 15 -X "$method" \
                -H "X-Authentik-Username: $user" -H "X-Authentik-Groups: $groups" \
                -H "Content-Type: application/json" --data-binary "$body" \
                "$url" 2>/dev/null) || detail=""
        fi
        detail=$(printf '%s' "$detail" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    sys.exit(0)
if isinstance(d, dict) and d.get("detail"):
    print(str(d["detail"]).strip())
' 2>/dev/null) || detail=""
        [ -n "$detail" ] && out="${out} — ${detail}"
    fi
    printf '%s' "$out"
    return "$rc"
}

# ── the model set (single source: core/llm/standard-models.yaml) ─────────────
# One row per deployable model, tab-separated:
#   alias \t task \t hf_repo \t filename \t params-json \t auto_start \t mmproj
# Filtering mirrors _model_rows_for_preset (presets[], requires_profile,
# placeholder rows without a GGUF). The manager takes params as a DICT, not a
# CLI string, so the manifest's `backend_parameters` are translated here —
# which is what carries #1058's `--batch-size/--ubatch-size = ctx` onto the
# embedding + reranker deploys.
_llmm_model_rows() {
    local preset="$1" hw
    # #2158: auto_start is a property of the entry AND the worker it lands on.
    # The target worker's hardware as the manager reports it (set by
    # _llmm_wait_for_worker); before a worker is known, the box's own class.
    hw="${_LLMM_WORKER_HW:-}"
    [ -n "$hw" ] || hw=$(read_env_value "${ENV_FILE:-.env}" HARDWARE 2>/dev/null) || hw=""
    PRESET="$preset" PROFILES="${COMPOSE_PROFILES:-}" HW="$hw" \
    YAML_PATH="${_LLMM_REPO_ROOT}/core/llm/standard-models.yaml" python3 - <<'PYEOF'
import json, os, sys
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not available — install python3-yaml.", file=sys.stderr)
    sys.exit(2)
sys.path.insert(0, os.path.dirname(os.environ["YAML_PATH"]))
import hardware_catalog as hc   # #2158: the one hardware rule

preset = os.environ["PRESET"]
profiles = set((os.environ.get("PROFILES") or "").split(","))
with open(os.environ["YAML_PATH"], encoding="utf-8") as fh:
    spec = yaml.safe_load(fh) or {}

# Flags the node driver ALREADY injects for every llama.cpp launch
# (drivers/base.py::llamacpp_command adds --cache-ram 0 --ctx-checkpoints 0),
# plus --mmproj, whose manifest value is an absolute GPUStack-volume path that
# means nothing on a manager worker (the node derives its own from `files`),
# plus the banned --swa-full. Passing them again is noise at best.
#
# #1256: dropping the PARAMETER is right, but the projector still has to reach
# the node — as a FILE. The sidecar therefore rides out as its own column
# (huggingface_mmproj_filename, the manifest's single declaration; the
# --mmproj= basename is the fallback for a hand-edited manifest, same rule as
# core/llm/expected_models.py::mmproj_filename) and lands in the deploy's
# `files`, where drivers/base.py::pick_mmproj finds it and adds the flag with
# a path that exists on the worker. Before this, `files` carried the weight
# quant only and a clean box served the vision default text-only.
DROP = {"cache-ram", "ctx-checkpoints", "mmproj", "swa-full"}


def mmproj_for(m):
    """The vision projector filename declared for one model, or "".

    Declared `huggingface_mmproj_filename` wins; else the basename of an
    explicit `--mmproj=<path>` backend parameter. Byte-identical rule to
    core/llm/expected_models.py::mmproj_filename and to the manager's
    app/model_manifest.py — one declaration, three readers, compared by
    tests/unit/consistency/test_1256_mmproj_single_source.py.
    """
    declared = str(m.get("huggingface_mmproj_filename") or "").strip()
    if declared:
        return declared
    for raw in m.get("backend_parameters") or []:
        raw = str(raw)
        if raw.startswith("--mmproj="):
            return os.path.basename(raw.split("=", 1)[1])
    return ""


def task_for(roles):
    """Manifest role -> manager serve task (chat | embed | rerank)."""
    if "embedding" in roles:
        return "embed"
    if "reranker" in roles:
        return "rerank"
    return "chat"


def _coerce(value):
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def params_for(backend_parameters):
    """`--ctx-size=8192` -> {"ctx-size": 8192}; a bare `--no-cache-prompt` ->
    {"no-cache-prompt": True}. The node normalises the keys back to real
    llama.cpp flags (drivers/base.py::params_to_cli_flags)."""
    out = {}
    for raw in backend_parameters or []:
        flag = str(raw).strip()
        if not flag.startswith("--"):
            continue
        flag = flag[2:]
        if "=" in flag:
            key, value = flag.split("=", 1)
            value = _coerce(value)
        else:
            key, value = flag, True
        key = key.strip()
        if not key or key in DROP:
            continue
        out[key] = value
    return out


for alias, m in (spec.get("models") or {}).items():
    if preset not in (m.get("presets") or ["standard", "developer"]):
        continue
    needs = m.get("requires_profile")
    if needs and needs not in profiles:
        continue
    repo = m.get("huggingface_repo_id", "")
    filename = m.get("huggingface_filename", "")
    if not (repo and filename):
        continue
    print("\t".join([
        alias,
        task_for(m.get("roles") or []),
        repo,
        filename,
        json.dumps(params_for(m.get("backend_parameters")), separators=(",", ":")),
        str(hc.entry_auto_start(m, os.environ.get("HW"))).lower(),
        mmproj_for(m),
    ]))
PYEOF
}

# _llmm_default_alias <role> <fallback> — `defaults.<role>` from the manifest.
_llmm_default_alias() {
    local role="$1" fallback="$2" alias hw
    hw="${_LLMM_WORKER_HW:-}"
    [ -n "$hw" ] || hw=$(read_env_value "${ENV_FILE:-.env}" HARDWARE 2>/dev/null) || hw=""
    alias=$(ROLE="$role" HW="$hw" YAML_PATH="${_LLMM_REPO_ROOT}/core/llm/standard-models.yaml" python3 -c '
import os, sys, yaml
sys.path.insert(0, os.path.dirname(os.environ["YAML_PATH"]))
import hardware_catalog as hc   # #2158
spec = yaml.safe_load(open(os.environ["YAML_PATH"], encoding="utf-8")) or {}
print(hc.defaults_for(spec, os.environ.get("HW")).get(os.environ["ROLE"], ""))
' 2>/dev/null) || alias=""
    printf '%s' "${alias:-$fallback}"
}

# _llmm_deploy_payload <name> <task> <repo> <filename> <params-json> <preset> [mmproj]
# The body POST /api/deployments takes, built the way the console's deploy
# editor builds it (model_name = served_model = the manifest alias, the weight
# file in `files`, `hf_repo` so the node can fetch what the registry cache does
# not already hold, the manifest params, one replica).
#
# #1256: a multimodal model's vision projector goes in `files` NEXT TO the
# weight quant — the node pulls both and drivers/base.py::pick_mmproj turns the
# second one into `--mmproj`. Empty/omitted 7th arg = the model declares no
# projector; the list then holds the weight file alone (an empty string in
# `files` would have the node 404 a pull of "").
#
# Two deliberate omissions:
#   • `worker_id` — the manager's own placement (_pick_worker) already excludes
#     external endpoint backends and stale nodes; hard-coding an id here would
#     re-implement that rule worse. One worker on a single-box install.
#   • `est_gb` — the VRAM admission gate only engages when the caller supplies
#     an estimate. This is the box's OWN fleet-standard set on its OWN worker,
#     sized in the manifest; a 409 here would block provisioning rather than
#     protect it. The console (which can offer the force decision) keeps it.
_llmm_deploy_payload() {
    NAME="$1" TASK="$2" REPO="$3" FILE="$4" PARAMS="$5" PRESET="$6" MMPROJ="${7:-}" python3 -c '
import json, os
params = json.loads(os.environ["PARAMS"] or "{}")
files = [os.environ["FILE"]] if os.environ["FILE"] else []
sidecar = (os.environ.get("MMPROJ") or "").strip()
if sidecar and files:
    files.append(sidecar)
print(json.dumps({
    "model_name": os.environ["NAME"],
    "served_model": os.environ["NAME"],
    "files": files,
    "hf_repo": os.environ["REPO"],
    "task": os.environ["TASK"],
    "params": params,
    "replicas": 1,
    "tags": ["standard-set", os.environ["PRESET"]],
}, separators=(",", ":")))
'
}

# #1649: name<TAB>id for deployments the manager HOLDS but never PLACED — a row
# with replicas > 0 and no live instance. Measured on 0.79: after the cutover
# migration the box had four correct rows and no model, because the reconciler
# runs `observe` by default (it is a workload-mover, opt-in) and this function
# read "exists" as "done". Both decisions are right alone; together they leave a
# box that looks configured and serves nothing.
#
# Derived from the SAME payload as the name list rather than a second GET: an
# extra round trip would also shift the response sequence every caller sees,
# which is how the first attempt at this broke two unrelated tests.
_llmm_unplaced_from_json() {
    printf '%s' "$1" | python3 -c '
import json, sys
DEAD = {"failed", "stopped", "evicted", "gone"}
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
for row in rows if isinstance(rows, list) else []:
    if not isinstance(row, dict):
        continue
    name, did = row.get("model_name"), row.get("id")
    if not name or not did:
        continue
    if (row.get("replicas") or 0) < 1:
        continue                      # 0 replicas is a deliberate parking spot
    live = [i for i in (row.get("instances") or [])
            if isinstance(i, dict) and str(i.get("status") or "").lower() not in DEAD]
    if not live:
        print(f"{name}\t{did}")
'
}

# Names of the deployments the manager already holds, one per line.
_llmm_names_from_json() {
    printf '%s' "$1" | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
for row in rows if isinstance(rows, list) else []:
    name = (row or {}).get("model_name") if isinstance(row, dict) else None
    if name:
        print(name)
'
}

# stdin = GET /api/deployments body; $1 = newline-separated wanted names.
# Prints two lines: the READY count, then a comma-joined "name(state)" list of
# everything not ready yet. "Ready" means a READY INSTANCE, not a Deployment
# status literal — on a live box the deployment row sits at `pending` while its
# engines are serving (same semantics as the router config and the Dify
# registration probe in post-install).
_llmm_ready_report() {
    WANT="$1" python3 -c '
import json, os, sys
want = [n for n in os.environ["WANT"].split("\n") if n.strip()]
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
by = {}
for row in rows if isinstance(rows, list) else []:
    if isinstance(row, dict) and row.get("model_name"):
        by[row["model_name"]] = row
ready, pending, unplaceable, failed = [], [], [], []
for name in want:
    row = by.get(name)
    if row is None:
        pending.append("%s(absent)" % name)
        continue
    instances = row.get("instances") or []
    if any((i or {}).get("status") == "ready" for i in instances):
        ready.append(name)
        continue
    state = (instances[0] or {}).get("status") if instances else None
    state = state or row.get("health") or row.get("status") or "pending"
    pending.append("%s(%s)" % (name, state))
    # #2367 (fourth line): every instance of the row has FAILED — the node
    # tried, retried, and gave up (a stalled weight stream past its retry
    # budget, a launch that died). Nothing is in flight for it any more, so
    # waiting for it is the 20 minutes 0.79 spent twice on a dead state.
    if instances and all((i or {}).get("status") == "failed" for i in instances):
        failed.append(name)
    # #1760 (third line): a pending row whose reason is a PRECONDITION the
    # manager states — not a slow pull. Exactly one marker counts: a node
    # failure can be healed by the reconciler, a missing weight source cannot,
    # and waiting on it buys nothing but time. On a box whose
    # manager predates #1760 the field is empty, this list stays empty, and the
    # callers wait exactly as they did before.
    if "no weight source recorded" in (row.get("last_error") or ""):
        unplaceable.append(name)
print(len(ready))
print(",".join(pending))
print(",".join(unplaceable))
print(",".join(failed))
'
}

# _llmm_all_pending_are_terminal <pending-list> <unplaceable-list> <failed-list>
#
# #2367: true when EVERY still-pending model is either one the manager cannot
# place (#1760) or one whose every instance has failed — nothing left that can
# still arrive on its own. Measured on 0.79 (2026-09-21, twice): two models
# whose pulls had failed kept post-install waiting the full 1200 s budget,
# and the install ended with no chat and no embedding model either way.
_llmm_all_pending_are_terminal() {
    local pending="$1" unplaceable="$2" failed="$3" entry name
    [ -n "$pending" ] || return 1
    [ -n "${unplaceable}${failed}" ] || return 1
    local IFS=','
    for entry in $pending; do
        name="${entry%%(*}"
        [ -n "$name" ] || continue
        case ",${unplaceable},${failed}," in
            *",${name},"*) : ;;
            *) return 1 ;;
        esac
    done
    return 0
}

# _llmm_all_pending_are_unplaceable <pending-list> <unplaceable-list>
#
# #1760: true when EVERY still-pending model is one the manager has already
# said it cannot place. Measured on 0.79: three deployments with no weight
# source kept `post-install` waiting 20 minutes in the deploy loop and another
# 15 in the late-consumer loop, for an event that cannot occur — and the
# consumers were then left wired to the old backend (#1507/#1446).
#
# ALL, not ANY: one genuinely slow model next to one broken one still deserves
# its full budget. Only when nothing left in the list can ever arrive is
# waiting the wrong answer.
_llmm_all_pending_are_unplaceable() {
    local pending="$1" unplaceable="$2" entry name
    [ -n "$pending" ] || return 1
    [ -n "$unplaceable" ] || return 1
    local IFS=','
    for entry in $pending; do
        name="${entry%%(*}"
        [ -n "$name" ] || continue
        case ",${unplaceable}," in
            *",${name},"*) : ;;
            *) return 1 ;;
        esac
    done
    return 0
}

# _llmm_unplaceable_standard_models <preset> — line 3 of the report, or empty.
_llmm_unplaceable_standard_models() {
    local preset="${1:-standard}" wanted json report
    wanted=$(_llmm_wanted_names "$preset") || return 0
    [ -n "$wanted" ] || return 0
    json=$(_llmm_admin_api GET /api/deployments) || return 0
    report=$(printf '%s' "$json" | _llmm_ready_report "$wanted") || return 0
    printf '%s\n' "$report" | sed -n 3p
}


# ── worker gate ──────────────────────────────────────────────────────────────
# Set by _llmm_wait_for_worker (bash has no tuple return, and the progress
# lines have to stay on stdout where the operator sees them).
_LLMM_WORKER_NAME=""
_LLMM_WORKER_HW=""

_llmm_wait_for_worker() {
    local timeout="${LLM_MANAGER_WORKER_TIMEOUT:-120}"
    local poll="${LLM_MANAGER_WORKER_POLL:-5}"
    local waited=0 json line rc want
    want=$(read_env_value "${ENV_FILE:-.env}" LLM_WORKER_NAME 2>/dev/null) || want=""
    # rev-B (7): #1254/#1250a already waited ~60s for the SAME worker to enrol
    # and published its verdict in LLM_WORKER_ENROLL_STATUS. When that verdict
    # is `failed` there is nothing left to wait for — polling another 120s only
    # delays the operator's first sight of the real cause. Short-circuit onto
    # the same #1250a pointer.
    if [ "${LLM_WORKER_ENROLL_STATUS:-}" = "failed" ]; then
        print_error "The embedded worker failed to enrol with the LLM Manager (LLM_WORKER_ENROLL_STATUS=failed) — there is nothing to deploy onto (#1250a, the embedded-worker enrolment gap). Check 'docker logs llm-worker-agent', then re-run 'rzfz post-install --preset standard' once the worker shows up in the console."
        return 1
    fi
    while :; do
        rc=0
        json=$(_llmm_admin_api GET /api/workers) || rc=$?
        if [ "$rc" -eq 0 ]; then
            line=$(printf '%s' "$json" | WANT_NAME="$want" python3 -c '
import json, os, sys
want = (os.environ.get("WANT_NAME") or "").strip()
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
# #1264: choose what placement will ACCEPT, not what is merely `ready`. The
# manager reports `placeable` from the same predicate its 409 uses; an external
# endpoint backend (federated GPUStack/Ollama, #979) is `ready` and yet no
# target. Older managers without the key: fall back to ready-and-not-external.
def placeable(r):
    if "placeable" in r:
        return r.get("placeable") is True
    return r.get("status") == "ready" and not r.get("external")
ready = [r for r in (rows if isinstance(rows, list) else [])
         if isinstance(r, dict) and placeable(r)]
pick = next((r for r in ready if want and r.get("name") == want), None) or (ready[0] if ready else None)
if pick:
    print("\t".join([str(pick.get("name") or ""), str(pick.get("hardware") or "")]))
') || line=""
            if [ -n "$line" ]; then
                _LLMM_WORKER_NAME=$(printf '%s' "$line" | cut -f1)
                _LLMM_WORKER_HW=$(printf '%s' "$line" | cut -f2)
                return 0
            fi
        fi
        [ "$waited" -ge "$timeout" ] && break
        [ "$waited" -eq 0 ] && print_substep "Waiting for a worker to register with the LLM Manager…"
        sleep "$poll"
        waited=$((waited + poll))
    done
    print_error "No worker is registered with the LLM Manager after ${timeout}s — there is nothing to deploy onto (#1250a, the embedded-worker enrolment gap). Check 'docker logs llm-worker-agent', then re-run 'rzfz post-install --preset standard' once the worker shows up in the console."
    return 1
}

# ── the readiness wait ───────────────────────────────────────────────────────
# #1507: which of the preset's models are NOT ready right now — the same
# want-set and the same report the deploy's wait uses, without the waiting.
# Empty output means "all ready". Used by post-install's late consumer
# reconcile, which re-runs the two steps that ENUMERATE the served set (Dify's
# provider + defaults, OWUI's model-sync) once a slow model finally lands.
# ── the want-set, decided ONCE ───────────────────────────────────────────────
# Review of #1507 (agent-rzfz) caught the real defect: the pending query took
# EVERY manifest row while the deploy places only a subset, so on the shipped
# `standard` preset (three of six rows are on-demand spares or wildcard
# filenames) `pending` could never go empty and the late re-wire — the whole
# point of the fix — never fired. Both callers now ask the same predicate.
#
# _llmm_row_deployable <auto_start> <filename> — 0 = the deploy places it.
_llmm_row_deployable() {
    local auto="$1" filename="$2"
    # auto_start:false = on-demand spare (see the deploy loop's note).
    [ "$auto" = "false" ] && return 1
    # A wildcard weight filename would 404 at pull time; the deploy skips it.
    case "$filename" in
        *'*'*|*'?'*) return 1 ;;
    esac
    return 0
}

# _llmm_wanted_names <preset> — the names the deploy places, one per line.
# Non-zero when the manifest cannot be read at all (the caller must not read
# that as "nothing wanted").
_llmm_wanted_names() {
    local preset="${1:-standard}" rows row name filename auto
    rows=$(_llmm_model_rows "$preset") || return 1
    [ -n "$rows" ] || return 1
    while IFS= read -r row; do
        [ -z "$row" ] && continue
        name=$(printf '%s' "$row" | cut -f1)
        filename=$(printf '%s' "$row" | cut -f4)
        auto=$(printf '%s' "$row" | cut -f6)
        [ -n "$name" ] || continue
        _llmm_row_deployable "$auto" "$filename" || continue
        printf '%s\n' "$name"
    done <<< "$rows"
}

_llmm_pending_standard_models() {
    local preset="${1:-standard}" wanted json report
    # "Cannot tell" must not read as "all ready" (an empty answer). Measured
    # while box-proving #1507: sourced from the wrong directory the manifest
    # lookup failed, and an `|| return 0` would have reported an empty pending
    # list — i.e. success — for a box nobody had looked at. The caller treats
    # `?` as "no verdict" and says so.
    wanted=$(_llmm_wanted_names "$preset") || { echo "?"; return 0; }
    [ -n "$wanted" ] || { echo "?"; return 0; }
    json=$(_llmm_admin_api GET /api/deployments) || { echo "?"; return 0; }
    report=$(printf '%s' "$json" | _llmm_ready_report "$wanted") || { echo "?"; return 0; }
    printf '%s\n' "$report" | sed -n 2p
}

_llmm_wait_ready() {
    local want="$1"
    local budget="${LLM_MANAGER_DEPLOY_TIMEOUT:-1200}"
    local poll="${LLM_MANAGER_DEPLOY_POLL:-10}"
    local want_n waited=0 last=0 json report ready pending unplaceable failed
    local failed_seen="" failed_streak=0 failed_stable_polls
    # #2367: a failed set must hold still for this long before it counts as
    # terminal. Measured on 0.79: a refresh watched qwen3.6(failed) for 90 s
    # before a redeploy turned it around, so leaving on the first sighting would
    # break the very recovery the node's retries add. 180 s by default.
    local failed_stable_s="${LLM_MANAGER_FAILED_STABLE_SECONDS:-180}"
    failed_stable_polls=$(( failed_stable_s / (poll > 0 ? poll : 1) ))
    [ "$failed_stable_polls" -ge 1 ] || failed_stable_polls=1
    want_n=$(printf '%s\n' "$want" | grep -c '[^[:space:]]') || want_n=0
    [ "$want_n" -gt 0 ] || return 0
    ready=0
    pending=""
    print_info "Waiting for $want_n model(s) to become ready (up to $((budget / 60)) minutes — first deploy downloads the weights)."
    while [ "$waited" -lt "$budget" ]; do
        json=$(_llmm_admin_api GET /api/deployments) || json=""
        report=$(printf '%s' "$json" | _llmm_ready_report "$want") || report=""
        ready=$(printf '%s\n' "$report" | sed -n 1p)
        pending=$(printf '%s\n' "$report" | sed -n 2p)
        unplaceable=$(printf '%s\n' "$report" | sed -n 3p)
        failed=$(printf '%s\n' "$report" | sed -n 4p)
        printf '%s' "$ready" | grep -Eq '^[0-9]+$' || ready=0
        if [ "$ready" -ge "$want_n" ]; then
            print_success "All $want_n model(s) are ready on the LLM Manager's workers."
            return 0
        fi
        # #1760: nothing left that can still arrive — leave now instead of
        # spending the rest of the budget on it.
        if _llmm_all_pending_are_unplaceable "$pending" "$unplaceable"; then
            print_warning "Not waiting: the manager cannot place ${unplaceable} — no weight source is recorded for it."
            print_info "  These will never become ready on their own. Re-deploy them from the"
            print_info "  LLM Manager console (Catalog), or run 'rzfz post-install --preset standard',"
            print_info "  which deploys the standard set unconditionally (#1760)."
            return 1
        fi
        if [ -n "$failed" ] && [ "$failed" = "$failed_seen" ]; then
            failed_streak=$((failed_streak + 1))
        else
            failed_seen="$failed"; failed_streak=0
        fi
        # #2367: nothing left that can still arrive — every pending model is
        # either unplaceable (#1760, no weight source recorded) or has FAILED on
        # the node past its own retries, and the failed set has held still for
        # ${failed_stable_polls} polls (the manager was given its chance to re-place).
        if [ "$failed_streak" -ge "$failed_stable_polls" ] \
           && _llmm_all_pending_are_terminal "$pending" "$unplaceable" "$failed"; then
            print_warning "Not waiting: the weight pull or launch FAILED on the node for ${failed}$( [ -n "$unplaceable" ] && printf ', and the manager cannot place %s (no weight source recorded)' "$unplaceable" )."
            print_info "  The node retries a stalled download on its own and keeps the partial for"
            print_info "  the next attempt; a deployment that still failed needs a new attempt:"
            print_info "  run 'rzfz post-install --preset standard' (deploys the standard set"
            print_info "  unconditionally and resumes the download) or redeploy it from the LLM"
            print_info "  Manager console (Catalog). Open WebUI and Dify carry an INCOMPLETE model"
            print_info "  set until then (#2367)."
            return 1
        fi
        if [ $((waited - last)) -ge 30 ]; then
            print_substep "… $ready/$want_n ready after ${waited}s — waiting on: ${pending:-?}"
            last=$waited
        fi
        sleep "$poll"
        waited=$((waited + poll))
    done
    print_error "LLM Manager: only $ready/$want_n model(s) became ready within $((budget / 60)) minutes — still pending: ${pending:-?}. Open the deployment in the LLM Manager console and read its engine log; re-run 'rzfz post-install --preset standard' once the cause is fixed."
    return 1
}

# =============================================================================
# llm_manager_deploy_standard_set <preset> [wait=true]
# =============================================================================
# The replacement for deploy_all_models + wait_for_all_models on a box whose
# LLM backend is the manager. Idempotent: a model that already has a deployment
# is left exactly as it is (params edits belong to the console, not to a
# re-run of post-install).
# =============================================================================
# _llmm_mark_incomplete_if_auto_skip_wait <did_wait>   (#2195)
# =============================================================================
# A deploy that did not wait leaves every consumer step that ENUMERATES the
# served set reading an incomplete one — the #1507 failure, reached through a
# different door. #1507 review finding 4 deliberately left the marker unset for
# `--skip-wait`, on the reasoning that an operator who types it has asked not to
# wait. That reasoning does not cover the case which did not exist yet: since
# HARDWARE=cpu turns --skip-wait on by DEFAULT, the flag is now set on boxes
# whose operator asked for nothing at all, and journey A measured what that
# costs — Open WebUI left with one hidden doc-conversion row (an empty chat
# picker) and Dify with no provider, on a box that was serving all four models
# five minutes later.
#
# So: mark it when WE chose the skip, leave it alone when the operator did.
_llmm_mark_incomplete_if_auto_skip_wait() {
    [ "${1:-true}" = false ] || return 0                 # it waited; nothing is stale
    [ "${SKIP_WAIT_AUTO:-false}" = true ] || return 0    # the operator's own choice
    _LLMM_DEPLOY_INCOMPLETE=1
    print_info "The wait was skipped for you (HARDWARE=cpu), so Open WebUI and Dify would be wired from a half-ready set — they are re-wired once the deployments land (#1507/#2195)."
    return 0
}

llm_manager_deploy_standard_set() {
    local preset="${1:-standard}" do_wait="${2:-true}"
    print_step "LLM Manager: deploying the standard model set (preset: $preset)..."

    if ! _llmm_wait_for_worker; then
        return 1
    fi
    print_substep "Target worker: '${_LLMM_WORKER_NAME}' (${_LLMM_WORKER_HW:-hardware unknown})."

    # #2227: an offline install's baked model GGUFs go into the node agent's
    # models volume BEFORE the deploy, so the worker finds each weight by name
    # and downloads nothing (ensure_file() returns "cached" on an existing
    # basename). Journey B on 2026.09-rc9 measured 5 GGUFs in the package and 0
    # in the volume after post-install: the only staging call lived inside
    # deploy_all_models(), the GPUStack arm, which a manager box never enters.
    # This is that arm's mirror of it. No-op without a baked package. The
    # function is post-install's; this library is also sourced by status.sh
    # and lib-owui.sh, where no staging exists and none is wanted.
    if declare -F stage_baked_appliance_models >/dev/null 2>&1; then
        stage_baked_appliance_models
    fi

    # #1518 (E5): the EXO-4 skip stood here — an NVIDIA/CUDA worker was warned
    # and handed NOTHING, because the stack built no llama.cpp CUDA runner image
    # and the GGUF set would have died at container create. Both halves of that
    # are gone: #1516/#1517 build and publish two CUDA runners (sm_120 and
    # sm_121a, picked by the GPU's compute capability) and the catalog entries
    # carry "nvidia" again. An NVIDIA node therefore gets the same standard set
    # as every other class — which is the point of having one model
    # architecture. A box with no runner image now fails LOUDLY at the deploy
    # instead of silently ending up with no model.

    local existing
    # #1649: ONE GET, two derived lists. A second round trip would also shift
    # the response sequence every caller sees — that is how the first attempt
    # at this broke two unrelated tests.
    local _dep_json _dep_rc=0 unplaced
    _dep_json=$(_llmm_admin_api GET /api/deployments) || _dep_rc=$?
    if [ "$_dep_rc" -eq 0 ]; then
        existing=$(_llmm_names_from_json "$_dep_json")
        unplaced=$(_llmm_unplaced_from_json "$_dep_json")
    else
        existing=""; unplaced=""
    fi

    local rows
    rows=$(_llmm_model_rows "$preset") || rows=""
    if [ -z "$rows" ]; then
        print_error "No deployable models for preset '$preset' in core/llm/standard-models.yaml — the box would end up with a chat UI and no model."
        return 1
    fi

    # Review finding 2 (#1507): the --refresh arm runs OWUI's model-sync BEFORE
    # this deploy, so a box that GAINS a deployment here has a consumer list
    # written from the set that existed a minute ago — stale even when the
    # deploy then succeeds completely. The caller uses this count to decide
    # whether the late re-wire is needed. Reset per call; the loop below runs
    # in this shell (here-string, not a pipe), so the increments survive it.
    _LLMM_DEPLOYED_NEW=0
    local wanted="" refused="" row name task repo filename params auto mmproj payload out rc
    _LLMM_DEPLOY_REFUSED=""
    while IFS= read -r row; do
        [ -z "$row" ] && continue
        name=$(printf '%s' "$row" | cut -f1)
        task=$(printf '%s' "$row" | cut -f2)
        repo=$(printf '%s' "$row" | cut -f3)
        filename=$(printf '%s' "$row" | cut -f4)
        params=$(printf '%s' "$row" | cut -f5)
        auto=$(printf '%s' "$row" | cut -f6)
        mmproj=$(printf '%s' "$row" | cut -f7)
        [ -n "$name" ] || continue
        # auto_start:false models are the on-demand spares (gemma4,
        # qwen3-coder-next, qwen3.8-27b). GPUStack could register them at 0
        # replicas; the manager has no such state — a deployment always places
        # an instance — and loading a second large model next to the always-on
        # default is the documented unified-memory host-OOM trap. They stay a
        # console action.
        if ! _llmm_row_deployable "$auto" "$filename"; then
            if [ "$auto" = "false" ]; then
                print_substep "On-demand spare '$name' not deployed (auto_start:false) — deploy it from the LLM Manager console when it is actually needed."
            else
                print_info "Skipping '$name': its manifest weight filename is a wildcard ($filename), which the standard set does not expand. Deploy it from the console's HuggingFace browser if this box needs it."
            fi
            continue
        fi
        # Why the standard set still skips a glob, though the node CAN expand
        # one: since #360 the node's puller resolves a pattern against the repo
        # listing (`hf_pull.resolve_glob`), so a wildcard entry deploys fine from
        # the console. What it cannot do is resolve it CHEAPLY here — the
        # expansion needs an HF listing call per entry, and an unattended install
        # would either block on the network or guess. The console browser is
        # where an operator picks such a model deliberately; the standard set
        # deploys only entries whose weights are already named. Skip it loudly.
        if printf '%s\n' "$existing" | grep -Fxq "$name"; then
            wanted="${wanted}${name}
"
            # #1649: "exists" splits in two. A row WITH a live instance is
            # finished and stays untouched — params belong to the console, not
            # to a post-install re-run. A row WITHOUT one is an unfinished
            # order, and finishing it is what this function is for. Starting is
            # NOT re-POSTing: the row, its params and its history are kept.
            _unplaced_id=$(printf '%s\n' "${unplaced:-}" | awk -F'\t' -v n="$name" '$1==n {print $2; exit}')
            if [ -n "$_unplaced_id" ]; then
                print_substep "Deployment '$name' exists but was never placed — starting it."
                # The refusal is CAPTURED and named (#1677). "could not be
                # started" sent me diagnosing for twenty minutes on 0.79 what
                # the manager had already said: three rows carried no weight
                # source at all, so `/start` answers 409 "no weight source
                # recorded" and no number of re-runs will ever change that.
                # busybox wget prints the status line to stderr and nothing to
                # stdout on an error, so the CODE is what can be recovered here
                # — and the code is what distinguishes "never will work" from
                # "not right now".
                _start_out=$(_llmm_admin_api POST "/api/deployments/${_unplaced_id}/start" '{}' 2>&1) || _start_rc=$?
                _start_code=$(printf '%s' "${_start_out:-}" | sed -n 's/.*HTTP\/1\.[01] \([0-9][0-9][0-9]\).*/\1/p' | head -1)
                if [ "${_start_rc:-0}" = "0" ]; then
                    print_substep "  '$name' placed."
                    # #2198: a row that was just STARTED is not ready yet, and
                    # the consumer lists written after this loop cannot have
                    # seen it — exactly what this counter stands for (#1507
                    # reads it as "the lists are stale"). A successful /start
                    # counts like a successful POST; the marker then triggers
                    # the late re-wire on a CPU box whose auto --skip-wait
                    # would otherwise leave Dify and Open WebUI without it.
                    _LLMM_DEPLOYED_NEW=$((_LLMM_DEPLOYED_NEW + 1))
                else
                    case "${_start_code:-}" in
                        409)
                            print_warning "  '$name' was refused (409). The row exists but the manager will not place it."
                            print_info "    Most often: no weight source recorded — the row carries neither hf_repo nor files, so it can never start, and re-running this does not help. Delete it in the LLM Manager console and deploy the model again from the catalog."
                            print_info "    Also 409: no worker has room, or the only worker is not ready yet."
                            ;;
                        "")
                            print_warning "  '$name' could not be started — no status line from the manager (is it up?)."
                            ;;
                        *)
                            print_warning "  '$name' could not be started — the manager answered ${_start_code}."
                            ;;
                    esac
                    print_info "    'rzfz status' lists every deployment that is not running (#1713)."
                fi
                unset _start_rc
            else
                print_substep "Deployment '$name' already exists — left untouched (idempotent re-run)."
            fi
            continue
        fi
        payload=$(_llmm_deploy_payload "$name" "$task" "$repo" "$filename" "$params" "$preset" "$mmproj") || payload=""
        if [ -z "$payload" ]; then
            print_error "Could not build the deploy payload for '$name' (manifest row unreadable)."
            return 1
        fi
        rc=0
        out=$(_llmm_admin_api POST /api/deployments "$payload") || rc=$?
        if [ "$rc" -ne 0 ]; then
            # #2156 (journey A, 0.91, a 30 GB CPU box): this was `return 1`
            # inside the loop. The first row (the 1M-context chat default) was
            # refused, and the three rows that would have fitted — the OCR
            # model, the embedder, the reranker — were never attempted: the box
            # installed clean, exit 0, 41 healthy containers, ZERO models.
            # The 409 arm for an existing row, thirty lines above, already did
            # the right thing: say why, carry on. Same here. The refused row
            # stays out of `wanted` so the readiness wait does not spend its
            # budget on a model that was never scheduled.
            print_warning "The LLM Manager refused the deploy of '$name': ${out:-no response from the manager}"
            refused="${refused}${name}
"
            continue
        fi
        wanted="${wanted}${name}
"
        _LLMM_DEPLOYED_NEW=$((_LLMM_DEPLOYED_NEW + 1))
        if [ -n "$mmproj" ]; then
            print_substep "Deploy scheduled: '$name' (task: $task, vision projector: $mmproj) on '${_LLMM_WORKER_NAME}'."
        else
            print_substep "Deploy scheduled: '$name' (task: $task) on '${_LLMM_WORKER_NAME}'."
        fi
    done <<< "$rows"

    # #2156: the verdict of the loop, in one place, before the wait.
    if [ -n "$refused" ]; then
        _LLMM_DEPLOY_REFUSED=$(printf '%s' "$refused" | grep -c .)
        local _refused_list
        _refused_list=$(printf '%s\n' "$refused" | grep . | paste -sd ',' - | sed 's/,/, /g')
        if [ -z "$wanted" ]; then
            print_error "The LLM Manager refused EVERY model of the standard set (${_refused_list}) — this box has a chat UI and no model."
            print_info "  The reasons are above. Pick a model that fits this worker in the LLM Manager console (Catalog), or free host RAM; then 'rzfz post-install --refresh --reconcile-models'."
            print_info "  A plain 'rzfz post-install --refresh' re-enters this deploy and gets the same answers (#2156)."
            return 1
        fi
        print_warning "Not every model of the standard set was placed — refused: ${_refused_list}. The rest continue below (#2156)."
        print_info "  Pick a fitting alternative in the LLM Manager console (Catalog), or free host RAM; then 'rzfz post-install --refresh --reconcile-models'."
    fi

    if [ "$do_wait" != "true" ]; then
        print_info "Skipping the readiness wait (--skip-wait) — the weights download and the engines start in the background; watch the LLM Manager console."
        # Review finding 4 (#1507): with no wait, the consumer steps that
        # follow ENUMERATE a set that is guaranteed to be incomplete, and the
        # incomplete-deploy marker is never set (this returns 0). Say so, and
        # name the command that repairs it — the same one the timeout path
        # names.
        print_warning "Open WebUI and Dify are wired from the models that are ready RIGHT NOW, which after --skip-wait is an incomplete set. Once the deployments are ready, run 'rzfz post-install --refresh' to re-register Dify's provider + defaults and re-sync OWUI's model list (#1507)."
        [ -z "$refused" ] || return 2   # #2156: a refused row is not "placed, just not waited for"
        return 0
    fi
    _llmm_wait_ready "$wanted" || return 1
    # #2156: the models that were scheduled are ready; the refused ones are
    # not, and the caller must not read this as "all placed" — 2 says so.
    [ -z "$refused" ] || return 2
    return 0
}

# =============================================================================
# llm_manager_verify_models
# =============================================================================
# The `--verify` counterpart. Registers its results through post-install's own
# `verify_check`, so the counts and the summary block are the shared ones.
# Probes the canonical endpoint the stack's consumers actually use — the
# manager's metered /v1 surface — with the stack/openwebui service key, NOT
# GPUStack's /v1-openai with a GPUStack key that a Manager box never had.
#
# DUAL-PROFILE BOX (a GPUStack profile AND `llm-manager` both on) — #1441
# (cutover C1): deploy and verify used to answer DIFFERENT questions. The
# deploy arms fired the GPUStack path whenever a GPUStack profile was active
# and never reached the manager, while the verify gate asked only "is the
# manager profile on" — so such a box got the three GPUStack lines AND four
# red "LLM Manager:" probe lines (models / chat / embedding / rerank, each
# "no response") that named the symptom, never the cause. Both sides now ask
# ONE predicate, llm_manager_owns_standard_set (scripts/lib-owui.sh); a box
# where the manager does not own the set gets ONE line that says why
# (llm_manager_verify_standard_set_ownership). rev-B: a WARN, because every
# consumer resolver follows the same predicate — such a box is coherent
# (models in GPUStack, consumers wired to GPUStack) and the idle manager is
# not a defect. #1442 (GPUStack fronted by the manager) flips the predicate.
# ── #1263: manifest drift — the manager-side counterpart of core/llm/sync.py ──
# llm_manager_deploy_standard_set is idempotent by SKIPPING every model that
# already has a deployment, so a manifest change (the #1058 batch sizes, a
# context size, a quantisation) never reached a box that had already deployed,
# and a model dropped from the manifest kept its deployment — a zombie holding
# VRAM. These three functions read the live deployments, compare them with the
# manifest rows and either report (always) or apply (opt-in, --reconcile-models).
#
# Ownership marker: the `standard-set` tag every _llmm_deploy_payload carries.
# A deployment WITHOUT it is operator-owned (console edit that should stick):
# reported as CONSOLE, never touched. Zombies are reported, never deleted.
#
# _llmm_drift_lines <preset> — one structured line per finding:
#   DRIFT|<id>|<name>|<json body for POST .../reconcile>|<human diff>
#   ZOMBIE|<id>|<name>||<human>       CONSOLE|<id>|<name>||<human>
#   ABSENT||<name>||<human>
_llmm_drift_lines() {
    local preset="${1:-standard}" rows deps rc=0
    rows=$(_llmm_model_rows "$preset") || rows=""
    deps=$(_llmm_admin_api GET /api/deployments) || rc=$?
    if [ "$rc" -ne 0 ]; then
        printf 'ERROR|||%s|the LLM Manager did not answer GET /api/deployments\n' ""
        return 1
    fi
    ROWS="$rows" DEPS="$deps" python3 - <<'PYEOF'
import json, os
rows = {}
for line in os.environ.get("ROWS", "").splitlines():
    c = line.split("\t")
    if len(c) < 7 or not c[0]:
        continue
    name, task, _repo, fname, params, auto, _mmproj = c[:7]
    if auto == "false" or "*" in fname or "?" in fname:
        continue            # the deploy skips these too (spares, wildcard weights)
    try:
        p = json.loads(params or "{}")
    except Exception:
        p = {}
    rows[name] = {"task": task or "chat", "params": p if isinstance(p, dict) else {}}
try:
    deps = json.loads(os.environ.get("DEPS") or "[]")
except Exception:
    deps = []
deps = [d for d in deps if isinstance(d, dict) and d.get("model_name")]
by_name = {d["model_name"]: d for d in deps}
def managed(d):
    return "standard-set" in (d.get("tags") or [])
def norm(p):
    return json.dumps(p or {}, sort_keys=True)
# #2435: keys that belong to the BOX when the manifest does not declare them.
# `pooling` is pinned per deployment by the manager's migration 0024 (or by an
# operator) to the space the box's indexes were built in; a reconcile that
# dropped it would silently move every new query into another vector space.
BOX_LOCAL = {"pooling"}
def flag(k):
    return str(k).lstrip("-").replace("_", "-").lower()
def box_local(live, want):
    declared = {flag(k) for k in want}
    return {k: v for k, v in live.items() if flag(k) in BOX_LOCAL and flag(k) not in declared}
out = []
for name, want in sorted(rows.items()):
    d = by_name.get(name)
    if d is None:
        out.append("ABSENT||%s||not deployed - 'rzfz post-install --refresh' deploys it" % name)
        continue
    live_all = d.get("params") or {}
    kept = box_local(live_all, want["params"])
    live_p = {k: v for k, v in live_all.items() if k not in kept}
    live_t = d.get("task") or "chat"
    diffs = []
    if norm(live_p) != norm(want["params"]):
        for k in sorted(set(live_p) | set(want["params"])):
            if live_p.get(k) != want["params"].get(k):
                diffs.append("%s: %s -> %s" % (k, live_p.get(k, "-"), want["params"].get(k, "-")))
    if live_t != want["task"]:
        diffs.append("task: %s -> %s" % (live_t, want["task"]))
    if not diffs:
        continue
    human = "; ".join(diffs)
    if managed(d):
        body = json.dumps({"params": dict(want["params"], **kept), "task": want["task"]}, separators=(",", ":"))
        out.append("DRIFT|%s|%s|%s|%s" % (d.get("id", ""), name, body, human))
    else:
        out.append("CONSOLE|%s|%s||operator-owned (no standard-set tag), differs from the manifest: %s - not applied" % (d.get("id", ""), name, human))
for d in deps:
    if managed(d) and d["model_name"] not in rows:
        out.append("ZOMBIE|%s|%s||tagged standard-set but not in the manifest (removed or renamed) - holds VRAM; remove it in the console or re-add it to the manifest" % (d.get("id", ""), d["model_name"]))
print("\n".join(out))
PYEOF
}

# llm_manager_manifest_drift_report <preset> — render; 0 when nothing drifts and
# nothing is a zombie, 1 otherwise, 2 when the manager could not be asked.
LLMM_DRIFT_SUMMARY=""
llm_manager_manifest_drift_report() {
    local preset="${1:-standard}" lines kind id name body human drift=0 zombie=0 console=0 absent=0
    lines=$(_llmm_drift_lines "$preset") || {
        print_warning "LLM Manager: could not compare deployments with the manifest (manager unreachable) - no verdict (#1263)."
        LLMM_DRIFT_SUMMARY="UNKNOWN"
        return 2
    }
    while IFS='|' read -r kind id name body human; do
        case "$kind" in
            DRIFT)   drift=$((drift + 1));     print_warning "  [DRIFT] $name - $human (manifest changed after this box deployed; apply with 'rzfz post-install --refresh --reconcile-models')" ;;
            ZOMBIE)  zombie=$((zombie + 1));   print_warning "  [ZOMBIE] $name - $human" ;;
            CONSOLE) console=$((console + 1)); print_info "  [CONSOLE] $name - $human" ;;
            ABSENT)  absent=$((absent + 1));   print_info "  [ABSENT] $name - $human" ;;
        esac
    done <<< "$lines"
    LLMM_DRIFT_SUMMARY="DRIFT=$drift ZOMBIE=$zombie CONSOLE=$console ABSENT=$absent"
    print_substep "LLM Manager manifest drift: $LLMM_DRIFT_SUMMARY"
    [ $((drift + zombie)) -eq 0 ]
}

# llm_manager_reconcile_standard_set <preset> — apply every DRIFT to the
# manifest-owned deployments (POST .../reconcile: params/task, live engines
# relaunched by the manager). Zombies and console-owned rows are never touched.
llm_manager_reconcile_standard_set() {
    local preset="${1:-standard}" lines kind id name body human applied=0 failed=0 out rc
    lines=$(_llmm_drift_lines "$preset") || return 2
    while IFS='|' read -r kind id name body human; do
        [ "$kind" = "DRIFT" ] || continue
        rc=0
        out=$(_llmm_admin_api POST "/api/deployments/${id}/reconcile" "$body") || rc=$?
        if [ "$rc" -eq 0 ]; then
            applied=$((applied + 1))
            print_substep "Reconciled '$name' from the manifest ($human) - live engines relaunch with the new params."
        else
            failed=$((failed + 1))
            print_error "The LLM Manager refused the reconcile of '$name': ${out:-no response from the manager}"
        fi
    done <<< "$lines"
    print_substep "LLM Manager reconcile: applied=$applied failed=$failed (zombies and console-owned deployments are reported, never changed)"
    [ "$failed" -eq 0 ]
}

# llm_manager_verify_manifest_drift — the --verify line. No verdict when the
# manager could not be asked (the #1301 lesson: an unmeasured check is not a PASS).
# #1441 (cutover C1): the ownership predicate llm_manager_owns_standard_set lives
# in scripts/lib-owui.sh (cli/upgrade.sh sources only that file, and the
# consumer resolvers there ask the same question). This is the verify line for a
# manager-profile box that does NOT own the set. rev-B: a WARN, not a FAIL —
# with every consumer following ownership (#1441 rev-B) such a box is coherent
# (models in GPUStack, consumers wired to GPUStack) and the idle manager is a
# state to know about, not a defect. #1442 fronts GPUStack through the manager.
llm_manager_verify_standard_set_ownership() {
    print_warning "  LLM Manager: idle on this box — a GPUStack profile holds the GPU, post-install deployed nothing through the manager and every consumer is wired to GPUStack (#1441). Run 'rzfz post-install --refresh' with GPUStack up to federate it behind the manager (#1442), or disable the GPUStack profile."
    return 0
}

# ── #1442 (cutover C2): GPUStack 0.7.1 as the manager's EXTERNAL backend ──────
# Operator decision D2 (2026-09-04): the manager is the only front, GPUStack an
# optional backend behind it. The mechanism is the #307/#318 external-backend
# registration (labels.external, per-model endpoint + api_key + task) — the
# same POST /api/workers scripts/register-external-backend.sh sends. What was
# missing is the box doing it for its OWN GPUStack: this step discovers the
# models GPUStack serves, registers them (chat/embed on /v1-openai, rerank on
# /v1 — the manager appends /rerank), hands the manager GPUStack's key as the
# backend api_key, reserves the GPU for GPUStack (#330 stage 1, so the embedded
# worker never double-books it) and writes the marker that flips
# llm_manager_owns_standard_set (scripts/lib-owui.sh) to "the manager owns the
# box": consumers wire to http://llm:8080/v1 and verify probes the manager.
# Idempotent: POST /api/workers upserts by name. Never fatal.
LLMM_GPUSTACK_WORKER_NAME="gpustack"
LLMM_GPUSTACK_OPENAI_BASE="http://gpustack:9090/v1-openai"
LLMM_GPUSTACK_RERANK_BASE="http://gpustack:9090/v1"
LLMM_GPUSTACK_RESERVED_GB="100000"   # "GPUStack holds the whole GPU" — no own engine fits

# _llmm_mint_command_key <worker-name> → per-worker command key on stdout (the
# enroll exchange the embedded worker uses, #1083), rc 1 when it cannot be minted.
_llmm_mint_command_key() {
    docker exec -i llm-manager python3 - "$1" <<'MINTEOF'
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
MINTEOF
}

# _llmm_gpustack_models → the ids GPUStack's OpenAI surface lists, one per line.
# rc 0 = a verdict (possibly an empty list), rc 1 = NO verdict, with the reason
# on stderr for the caller to relay.
#
# Review finding 4 (#1442): `curl -sf | python3 … 2>/dev/null` threw away the
# status code, the body and every parse error, so a 401 from a rotated
# GPUSTACK_API_KEY produced the same line as a healthy GPUStack with no models:
# "deploy the standard set first". The operator then went looking in the wrong
# place. Measured with the harness: curl rc 22, message unchanged.
# _llmm_gpustack_get <url> → body on stdout, rc 1 with the reason on stderr.
# One place for the reachability / key / parse verdicts (review finding 4).
_llmm_gpustack_get() {
    local url="$1" key="${GPUSTACK_API_KEY:-}"
    [ -n "$key" ] || key=$(read_env_value "${ENV_FILE:-.env}" GPUSTACK_API_KEY 2>/dev/null) || key=""
    local body code rc=0
    body=$(curl -s --max-time 10 -w '\n%{http_code}' -H "Authorization: Bearer $key" "$url" 2>/dev/null) || rc=$?
    if [ "$rc" -ne 0 ]; then
        printf 'unreachable (curl rc %s)\n' "$rc" >&2
        return 1
    fi
    code=$(printf '%s' "$body" | tail -n1)
    body=$(printf '%s' "$body" | sed '$d')
    case "$code" in
        200) ;;
        401|403) printf 'denied (HTTP %s — check GPUSTACK_API_KEY)\n' "$code" >&2; return 1 ;;
        *)       printf 'unexpected HTTP %s\n' "${code:-<none>}" >&2; return 1 ;;
    esac
    printf '%s' "$body"
}

# _llmm_gpustack_models → one line per model GPUStack HOLDS: name<TAB>status<TAB>detail.
#
# #1442 rev-D (journey C on 0.79, 2026-09-15): this used to read the OpenAI list
# /v1-openai/models, which names only models with a RUNNING instance — so the
# two replicas=0 spares, the pending and the errored model of a real ga.15
# catalogue never reached the manager (3 of 7 federated) while the run wired the
# consumers as if they had. The admin lists carry the whole catalogue:
# /v1/models (every model, with replicas) and /v1/model-instances (state per
# instance). Status per model, in the manager's own vocabulary:
#   ready    — at least one instance running
#   stopped  — replicas 0 (a deliberate parking spot: weights cached, no VRAM)
#   error    — an instance in error, GPUStack's message as detail
#   pending  — anything else (downloading, scheduled, starting, no instance yet)
# The router serves `ready` only; the console shows the rest with its reason.
_llmm_gpustack_models() {
    local base="http://127.0.0.1:${GPUSTACK_PORT:-9090}" models instances
    models=$(_llmm_gpustack_get "$base/v1/models") || return 1
    instances=$(_llmm_gpustack_get "$base/v1/model-instances") || return 1
    MODELS_JSON="$models" INSTANCES_JSON="$instances" python3 - <<'CATEOF'
import json, os, sys
def load(raw, what):
    try:
        return json.loads(raw)
    except Exception as exc:
        sys.stderr.write("unreadable %s answer (%s)\n" % (what, exc))
        raise SystemExit(1)
models = load(os.environ["MODELS_JSON"], "/v1/models").get("items", [])
instances = load(os.environ["INSTANCES_JSON"], "/v1/model-instances").get("items", [])
by_model = {}
for i in instances:
    if isinstance(i, dict) and i.get("model_name"):
        by_model.setdefault(i["model_name"], []).append(i)
for m in models:
    if not isinstance(m, dict) or not m.get("name"):
        continue
    name = m["name"]
    insts = by_model.get(name, [])
    states = [str(i.get("state") or "").lower() for i in insts]
    if "running" in states:
        status, detail = "ready", ""
    elif (m.get("replicas") or 0) < 1 and not insts:
        status, detail = "stopped", "0 replicas in GPUStack (parked: weights cached, no instance)"
    elif "error" in states:
        err = next(i for i in insts if str(i.get("state") or "").lower() == "error")
        status, detail = "error", "GPUStack instance error: %s" % (err.get("state_message") or "no message")
    elif insts:
        first = insts[0]
        status = "pending"
        detail = "GPUStack instance %s%s" % (states[0] or "unknown",
                                             (": " + first["state_message"]) if first.get("state_message") else "")
    else:
        status, detail = "pending", "replicas %s in GPUStack, no instance yet" % (m.get("replicas") or 0)
    print("%s\t%s\t%s" % (name, status, detail.replace("\t", " ").replace("\n", " ")))
CATEOF
}

# _llmm_federation_payload <models-one-per-line> → JSON on stdout. Task from the
# name shape (the manager's own #318 rule, _infer_task): rerank → /v1 (the manager
# appends /rerank), embed/chat → /v1-openai. GPUStack's key is the backend key.
_llmm_federation_payload() {
    local key="${GPUSTACK_API_KEY:-}"
    [ -n "$key" ] || key=$(read_env_value "${ENV_FILE:-.env}" GPUSTACK_API_KEY 2>/dev/null) || key=""
    # The model list travels in the environment: `python3 -` reads its script
    # from stdin, so a pipe into it would be swallowed by the heredoc.
    MODELS="$1" NAME="$LLMM_GPUSTACK_WORKER_NAME" HARDWARE="${HARDWARE:-amd}" KEY="$key" \
        OPENAI="$LLMM_GPUSTACK_OPENAI_BASE" RERANK="$LLMM_GPUSTACK_RERANK_BASE" python3 - <<'PAYEOF'
import json, os, sys
def task(n):
    n = n.lower()
    if "rerank" in n: return "rerank"
    if "embed" in n or n.endswith("-emb") or "bge" in n or "nomic" in n or "e5" in n: return "embed"
    return "chat"
# rev-D: a line is name<TAB>status<TAB>detail (every model GPUStack holds, with
# its state); a bare name still means ready, so a caller with the old shape works.
rows = []
for line in os.environ["MODELS"].splitlines():
    if not line.strip():
        continue
    parts = line.split("\t")
    name = parts[0].strip()
    status = (parts[1].strip() if len(parts) > 1 and parts[1].strip() else "ready")
    detail = (parts[2].strip() if len(parts) > 2 else "") or None
    rows.append((name, status, detail))
key = os.environ["KEY"] or None
print(json.dumps({
    "name": os.environ["NAME"], "address": os.environ["NAME"], "hardware": os.environ["HARDWARE"],
    "engine": "gpustack", "external": True, "role": "worker",
    "models": [{"model_name": m, "served_model": m, "task": task(m), "status": st, "detail": dt, "api_key": key,
                "endpoint": os.environ["RERANK"] if task(m) == "rerank" else os.environ["OPENAI"]}
               for m, st, dt in rows]}))
PAYEOF
}

# _llmm_deregister_gpustack — Blocker 1 (#1442 review). Clearing the marker and
# releasing the reservation is not enough: `register_worker` wrote the GPUStack
# models with `status: "ready"`, and `router_config.generate_from_db` filters
# only RELAY-routed workers on liveness — an external worker's instances stay
# serveable after its container is gone. Worse, once the manager deploys its own
# set again it hangs its instance on the SAME unique `model_name` row, so one
# model name carries two endpoints and LiteLLM round-robins half the traffic
# into a container that no longer exists.
#
# No admin transport needed: `register_worker` prunes authoritatively — a POST
# with an EMPTY model list removes every instance the worker no longer reports.
# Same command key the federation mints. Never fatal; the caller decides what to
# say when it fails.
_llmm_deregister_gpustack() {
    local ckey
    ckey=$(_llmm_mint_command_key "$LLMM_GPUSTACK_WORKER_NAME" 2>/dev/null) || ckey=""
    [ -n "$ckey" ] || return 1
    local payload
    payload=$(NAME="$LLMM_GPUSTACK_WORKER_NAME" HARDWARE="${HARDWARE:-amd}" python3 - <<'DEREGEOF'
import json, os
print(json.dumps({"name": os.environ["NAME"], "address": os.environ["NAME"],
                  "hardware": os.environ["HARDWARE"], "engine": "gpustack",
                  "external": True, "role": "worker", "models": []}))
DEREGEOF
)
    printf '%s' "$payload" | curl -sf --max-time 15 -X POST \
        "http://127.0.0.1:${LLM_MANAGER_PORT:-8091}/api/workers" \
        -H "Authorization: Bearer $ckey" -H "Content-Type: application/json" \
        -d @- >/dev/null 2>&1
}

_llmm_set_federation_marker() {
    # $1 = true|false — .env for the next run, export for THIS run (the
    # predicate reads the environment, like COMPOSE_PROFILES).
    update_env_value "${ENV_FILE:-.env}" "LLM_MANAGER_GPUSTACK_FEDERATED" "$1"
    export LLM_MANAGER_GPUSTACK_FEDERATED="$1"
}

llm_manager_federate_gpustack() {
    local env_file="${ENV_FILE:-.env}" was
    was=$(read_env_value "$env_file" LLM_MANAGER_GPUSTACK_FEDERATED 2>/dev/null) || was=""
    if ! _gpustack_profile_active; then
        # rev-B (review): clearing the marker alone left
        # LLM_MANAGER_VRAM_RESERVED_EXTERNAL_GB at the federation value, so the
        # admission budget stayed 0 and the manager could not place a single
        # engine of its own — while this line claimed the opposite. The
        # reservation is GPUStack's; it goes with GPUStack.
        # Blocker 1 (review): take the registration back BEFORE the marker
        # flips, while the manager is still reachable — otherwise the dead
        # GPUStack endpoints stay serveable in the router.
        if [ "$was" = "true" ] && _llm_manager_profile_active && _llm_manager_running; then
            if _llmm_deregister_gpustack; then
                print_substep "  external backend '${LLMM_GPUSTACK_WORKER_NAME}' deregistered — its endpoints are out of the router (#1442)."
            else
                print_warning "  could not deregister '${LLMM_GPUSTACK_WORKER_NAME}' — its endpoints may still be in the router config; remove the worker in the LLM Manager console (#1442)."
            fi
        elif [ "$was" = "true" ]; then
            print_warning "  llm-manager is not running — external backend '${LLMM_GPUSTACK_WORKER_NAME}' NOT deregistered; its endpoints stay in the router config until the next 'rzfz post-install --refresh' with the manager up (#1442)."
        fi
        local cur_res_off restore_to
        cur_res_off=$(read_env_value "$env_file" LLM_MANAGER_VRAM_RESERVED_EXTERNAL_GB 2>/dev/null) || cur_res_off=""
        if [ "$cur_res_off" = "$LLMM_GPUSTACK_RESERVED_GB" ]; then
            # Review blocker 2: the set branch used to overwrite an operator's
            # own coexistence value (.env.example names 38 for a resident
            # qwen3.6 Q8) and this branch wrote 0 — so the federation silently
            # ate a setting that was not its own. The pre-federation value is
            # parked and restored.
            restore_to=$(read_env_value "$env_file" LLM_MANAGER_VRAM_RESERVED_EXTERNAL_GB_PRE_FEDERATION 2>/dev/null) || restore_to=""
            [ -n "$restore_to" ] || restore_to="0"
            update_env_value "$env_file" "LLM_MANAGER_VRAM_RESERVED_EXTERNAL_GB" "$restore_to"
            update_env_value "$env_file" "LLM_MANAGER_VRAM_RESERVED_EXTERNAL_GB_PRE_FEDERATION" ""
            # rev-C (review N1): the WRITE is unconditional — the manager reads
            # the value at start, so a box whose manager is off picks it up on
            # its next start anyway. The RECREATE is not: `docker compose up -d
            # <service>` starts a named service even when its profile is off, so
            # doing it here would restart a container the operator just disabled
            # and then wait 90 s for a health endpoint that will never answer.
            if _llm_manager_profile_active && _llm_manager_running; then
                docker compose up -d --no-deps --force-recreate llm-manager >/dev/null 2>&1 || true
                _llmm_wait_manager_healthy 90 || print_warning "  llm-manager did not report healthy within 90 s after the recreate (#1442)."
            fi
        fi
        if [ "$was" = "true" ]; then
            _llmm_set_federation_marker false
            print_substep "GPUStack profile is off — federation marker cleared and its VRAM reservation released; the manager serves its own workers again (#1442)."
        fi
        return 0
    fi
    _llm_manager_profile_active || return 0
    if ! _llm_manager_running; then
        print_warning "llm-manager profile active but the container is not running — GPUStack NOT federated behind the manager; consumers stay on GPUStack. Re-run 'rzfz post-install --refresh' once it is up (#1442)."
        return 0
    fi
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gpustack; then
        print_warning "gpustack container is not running — nothing to federate (#1442)."
        return 0
    fi
    print_step "Federating GPUStack behind the LLM Manager (#1442)..."
    local models why rc=0 _err
    # stdout and stderr must be captured SEPARATELY: assigning inside a command
    # substitution would set the variable in the subshell and lose it here.
    _err=$(mktemp 2>/dev/null) || _err=""
    if [ -n "$_err" ]; then
        models=$(_llmm_gpustack_models 2>"$_err") || rc=$?
        why=$(cat "$_err" 2>/dev/null); rm -f "$_err"
    else
        models=$(_llmm_gpustack_models 2>/dev/null) || rc=$?
        why="no detail (could not capture stderr)"
    fi
    if [ "$rc" -ne 0 ]; then
        print_warning "  could not read GPUStack's model list: ${why:-no detail} — NOT federated (#1442). This is not 'no models yet'; check the key and that gpustack answers on ${GPUSTACK_PORT:-9090}."
        return 0
    fi
    if [ -z "$models" ]; then
        print_warning "  GPUStack answered with an EMPTY model list on /v1/models — not federated yet (deploy the standard set first, then re-run 'rzfz post-install --refresh')."
        return 0
    fi
    local ckey
    ckey=$(_llmm_mint_command_key "$LLMM_GPUSTACK_WORKER_NAME") || ckey=""
    if [ -z "$ckey" ]; then
        print_warning "  could not mint a command key for worker '${LLMM_GPUSTACK_WORKER_NAME}' — GPUStack not federated (#1442)."
        return 0
    fi
    local payload resp
    payload=$(_llmm_federation_payload "$models")
    resp=$(printf '%s' "$payload" | curl -sf --max-time 15 -X POST \
        "http://127.0.0.1:${LLM_MANAGER_PORT:-8091}/api/workers" \
        -H "Authorization: Bearer $ckey" -H "Content-Type: application/json" -d @- 2>/dev/null) || resp=""
    if [ -z "$resp" ]; then
        print_warning "  POST /api/workers for '${LLMM_GPUSTACK_WORKER_NAME}' failed — GPUStack not federated; consumers stay on GPUStack (#1442)."
        return 0
    fi
    # #330 stage 1: GPUStack holds the GPU — the embedded worker must never place
    # an own engine next to it. The manager reads this at start, so recreate it
    # only when the value actually changes.
    local cur_res
    cur_res=$(read_env_value "$env_file" LLM_MANAGER_VRAM_RESERVED_EXTERNAL_GB 2>/dev/null) || cur_res=""
    if [ "$cur_res" != "$LLMM_GPUSTACK_RESERVED_GB" ]; then
        # Park whatever the operator had, so switching the backend off gives it
        # back instead of writing 0 over it (review blocker 2).
        update_env_value "$env_file" "LLM_MANAGER_VRAM_RESERVED_EXTERNAL_GB_PRE_FEDERATION" "${cur_res:-0}"
        update_env_value "$env_file" "LLM_MANAGER_VRAM_RESERVED_EXTERNAL_GB" "$LLMM_GPUSTACK_RESERVED_GB"
        # --no-deps: llm-manager depends_on llm-manager-router, and recreating
        # the LiteLLM instance mid-post-install is not what this write needs
        # (review rev-A, #308's reload gap).
        docker compose up -d --no-deps --force-recreate llm-manager >/dev/null 2>&1 || true
        _llmm_wait_manager_healthy 90 || print_warning "  llm-manager did not report healthy within 90 s after the recreate (#1442) — the verify block will say so."
    fi
    _llmm_set_federation_marker true
    local n by_state
    n=$(printf '%s\n' "$models" | grep -c .)
    # rev-D: say how many of them can actually answer — a catalogue federated
    # with three ready and four not is not "seven models" to a consumer.
    by_state=$(printf '%s\n' "$models" | awk -F'\t' 'NF{c[$2==""?"ready":$2]++} END{o=""; for (k in c) o=o (o==""?"":", ") c[k] " " k; print o}')
    print_success "GPUStack federated behind the LLM Manager: ${n} model(s) registered as external backend '${LLMM_GPUSTACK_WORKER_NAME}' (${by_state}) — consumers wire to the manager; only ready models are served (#1442)."
    return 0
}

_llmm_wait_manager_healthy() {
    local max="${1:-60}" waited=0
    while [ "$waited" -lt "$max" ]; do
        curl -sf --max-time 3 "http://127.0.0.1:${LLM_MANAGER_PORT:-8091}/healthz" >/dev/null 2>&1 && return 0
        sleep 3; waited=$((waited + 3))
    done
    return 1
}

llm_manager_verify_manifest_drift() {
    local rc=0
    llm_manager_manifest_drift_report "standard" || rc=$?
    case "$rc" in
        0) verify_check "LLM Manager: deployments match core/llm/standard-models.yaml" "pass" ;;
        2) print_warning "  LLM Manager: manifest drift NOT checked (manager unreachable) - no verdict" ;;
        *) verify_check "LLM Manager: deployments match core/llm/standard-models.yaml" "fail" \
               "$LLMM_DRIFT_SUMMARY - apply with 'rzfz post-install --refresh --reconcile-models'; zombies: remove in the console" ;;
    esac
    return 0
}

llm_manager_verify_models() {
    local base="http://127.0.0.1:${LLM_MANAGER_PORT:-8091}"
    local key="${LLM_MANAGER_OWUI_KEY:-}"
    if [ -z "$key" ]; then
        key=$(read_env_value "${ENV_FILE:-.env}" LLM_MANAGER_OWUI_KEY 2>/dev/null) || key=""
    fi
    local chat_model embed_model rerank_model
    chat_model=$(_llmm_default_alias chat qwen3.6)
    embed_model=$(_llmm_default_alias embedding qwen3-embedding)
    rerank_model=$(_llmm_default_alias reranker qwen3-reranker)

    print_substep "Checking the LLM Manager..."
    if [ -z "$key" ]; then
        # Say it ONCE, as its own check, instead of four identical "no
        # response" lines that hide the single real cause.
        verify_check "LLM Manager: service key present" "fail" \
            "LLM_MANAGER_OWUI_KEY is empty in .env — re-run 'rzfz post-install --refresh' with llm-manager up to mint one (#1185)"
        return 0
    fi

    # 1. models listed on the metered /v1 surface
    local models
    models=$(curl -sf --max-time 10 -H "Authorization: Bearer $key" \
        "${base}/v1/models" 2>/dev/null | \
        python3 -c "import sys,json; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null) || models=""
    if [ -n "$models" ] && [ "$models" -gt 0 ] 2>/dev/null; then
        verify_check "LLM Manager: $models models available" "pass"
    else
        verify_check "LLM Manager: models available" "fail" "no models or unreachable"
    fi

    # 2. chat completion against the manifest's default chat model. qwen3.6
    # ships thinking-ON, so give reasoning AND the answer room (2048) and fall
    # back to reasoning_content for proof-of-life — same shape as the GPUStack
    # check this replaces.
    local chat_resp
    chat_resp=$(curl -sf --max-time 120 -H "Authorization: Bearer $key" \
        -H "Content-Type: application/json" \
        "${base}/v1/chat/completions" \
        -d "{\"model\":\"${chat_model}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}],\"max_tokens\":2048}" 2>/dev/null | \
        python3 -c "
import sys, json
try:
    msg = json.load(sys.stdin)['choices'][0]['message']
    content = msg.get('content', '') or msg.get('reasoning_content', '')
    print(content[:20] if content else 'empty')
except Exception: pass
" 2>/dev/null) || chat_resp=""
    if [ -n "$chat_resp" ]; then
        verify_check "LLM Manager: chat completion ($chat_model)" "pass"
    else
        verify_check "LLM Manager: chat completion ($chat_model)" "fail" "no response"
    fi

    # 3. embedding
    local embed_resp
    embed_resp=$(curl -sf --max-time 30 -H "Authorization: Bearer $key" \
        -H "Content-Type: application/json" \
        "${base}/v1/embeddings" \
        -d "{\"model\":\"${embed_model}\",\"input\":\"test\"}" 2>/dev/null | \
        python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data'][0]['embedding']))" 2>/dev/null) || embed_resp=""
    if [ -n "$embed_resp" ] && [ "$embed_resp" -gt 0 ] 2>/dev/null; then
        verify_check "LLM Manager: embedding ($embed_model, dim=$embed_resp)" "pass"
    else
        verify_check "LLM Manager: embedding ($embed_model)" "fail" "no response"
    fi

    # 4. rerank — the RAG path breaks silently without it (#908/#1058), so it
    # gets its own check rather than being assumed from the embedding one.
    local rerank_resp
    rerank_resp=$(curl -sf --max-time 30 -H "Authorization: Bearer $key" \
        -H "Content-Type: application/json" \
        "${base}/v1/rerank" \
        -d "{\"model\":\"${rerank_model}\",\"query\":\"test\",\"documents\":[\"a test document\",\"an unrelated one\"]}" 2>/dev/null | \
        python3 -c "import sys,json; print(len(json.load(sys.stdin).get('results',[])))" 2>/dev/null) || rerank_resp=""
    if [ -n "$rerank_resp" ] && [ "$rerank_resp" -gt 0 ] 2>/dev/null; then
        verify_check "LLM Manager: rerank ($rerank_model)" "pass"
    else
        verify_check "LLM Manager: rerank ($rerank_model)" "fail" "no response"
    fi
}
