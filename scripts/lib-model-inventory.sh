#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Deployed-model inventory for the operator-facing PDFs (#2317).
#
# Since #1443/#1445 the LLM Manager is the front end of every box and GPUStack
# (`llm-legacy`) is an optional backend. Both PDF generators used to read the
# inventory from the gpustack container only, so on the DEFAULT 2026.09 shape
# (no llm-legacy) they rendered a plausible two-page PDF with the model section
# silently missing, exit 0. This library reads the Manager first — the same
# wire cli/status.sh uses (/api/deployments through caddy with the forwarded
# admin identity) — and falls back to GPUStack only where that container runs.
#
#   razzfazz_model_inventory_load [ENV_FILE]      ← what the generators call
#       sets RAZZFAZZ_MODEL_INVENTORY_HTML (one <tr> per deployed model: Model · Type ·
#       Footprint · Backend · Status) and RAZZFAZZ_MODEL_INVENTORY_SOURCE ("manager" |
#       "gpustack" | "") IN THE CALLER'S SHELL; returns 0 with rows, 1 with nothing —
#       the CALLER must say so loudly.
#   razzfazz_model_inventory_rows [ENV_FILE]      prints the rows; the SOURCE variable it
#       sets is lost when called through $(...) — which is exactly how the generators
#       called it, so the stdout label read "Model inventory:  (4 models)" on a test box.
RAZZFAZZ_MODEL_INVENTORY_SOURCE=""
RAZZFAZZ_MODEL_INVENTORY_HTML=""
# #2546: the model manifest (types by role), relative to this library;
# RAZZFAZZ_MODEL_MANIFEST overrides it.
_RMI_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"

_rmi_env_value() {  # _rmi_env_value FILE KEY  (last assignment wins, quotes stripped)
    [ -f "$1" ] || return 1
    grep -E "^${2}=" "$1" | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

_rmi_manager_json() {
    local env_file="$1" user groups
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'llm-manager' || return 1
    user="$(_rmi_env_value "$env_file" LLM_MANAGER_ADMIN_USER)"
    [ -n "$user" ] || user="$(_rmi_env_value "$env_file" RAZZFAZZ_ADMIN_USERNAME)"
    [ -n "$user" ] || user="${RAZZFAZZ_ADMIN_USERNAME:-akadmin}"
    groups="$(_rmi_env_value "$env_file" LLM_MANAGER_ADMIN_GROUPS)"
    [ -n "$groups" ] || groups="razzfazz.ai Super Admins,authentik Admins"
    groups="$(printf '%s' "$groups" | tr ',' '|')"
    docker exec caddy wget -q -O - --timeout=15 \
        --header="X-Authentik-Username: $user" \
        --header="X-Authentik-Groups: $groups" \
        "http://llm-manager:8080/api/deployments" 2>/dev/null
}

_rmi_gpustack_json() {
    local env_file="$1" key
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'gpustack' || return 1
    key="$(_rmi_env_value "$env_file" GPUSTACK_API_KEY)"
    [ -n "$key" ] || return 1
    # The key travels in the exec environment by name and in a 0600 file inside
    # the container; no argv carries it (#2577).
    RZ_CURL_KEY="$key" docker exec -e RZ_CURL_KEY gpustack sh -c \
        'umask 077; cfg=$(mktemp) || exit 1; trap "rm -f \"$cfg\"" EXIT; printf "header = \"Authorization: Bearer %s\"\n" "$RZ_CURL_KEY" > "$cfg"; curl -s --max-time 15 -K "$cfg" http://localhost:9090/v1/models' </dev/null 2>/dev/null
}

razzfazz_model_inventory_rows() {
    local env_file="${1:-.env}" json rows
    RAZZFAZZ_MODEL_INVENTORY_SOURCE=""
    json="$(_rmi_manager_json "$env_file" || true)"
    if [ -n "$json" ]; then
        local manifest="${RAZZFAZZ_MODEL_MANIFEST:-${_RMI_LIB_DIR}/../core/llm/standard-models.yaml}"
        # #2546: without the manifest the types fall back to the serve task alone
        # (granite-docling then reads "Chat") — say so instead of degrading silently.
        if [ ! -r "$manifest" ]; then
            echo "[!] model manifest not readable ($manifest); model types fall back to the serve task" >&2
        fi
        rows="$(printf '%s' "$json" | RMI_MANIFEST="$manifest" python3 -c '
import json, sys, html
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
# #2546: the type comes from the serve task, refined by the model manifest roles
# for a chat-served model (granite-docling is served as chat but is the Docling
# document-conversion VLM). No task and no roles is "—", never "Chat".
import os, re
roles = {}
try:
    with open(os.environ.get("RMI_MANIFEST", ""), encoding="utf-8") as fh:
        text = fh.read()
    try:
        import yaml
        for alias, m in ((yaml.safe_load(text) or {}).get("models") or {}).items():
            if isinstance(m, dict):
                roles[str(alias)] = [str(x).lower() for x in (m.get("roles") or [])]
    except ImportError:
        # without PyYAML: "  <alias>:" under "models:", then "    roles: [a, b]"
        sect = alias = None
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            ind = len(line) - len(line.lstrip())
            if ind == 0:
                sect = line.split(":", 1)[0]
            elif sect == "models" and ind == 2:
                alias = line.strip().rstrip(":").strip("\"")
            elif sect == "models" and ind == 4 and alias and line.strip().startswith("roles:"):
                m = re.search(r"\[(.*)\]", line)
                roles[alias] = [x.strip().strip("\"").lower() for x in (m.group(1).split(",") if m else []) if x.strip()]
except Exception:
    roles = {}

def type_label(task, rls):
    def doc(x): return "document" in x or x in ("ocr", "vlm")
    def speech(x): return any(k in x for k in ("speech", "stt", "tts", "transcri", "whisper", "audio"))
    if task in ("embed", "embedding"):
        return "Embedding"
    if task in ("rerank", "reranker"):
        return "Reranker"
    if doc(task):
        return "Dokumenten-VLM"
    if speech(task):
        return "Sprache"
    if task in ("chat", ""):
        if any(doc(x) for x in rls):
            return "Dokumenten-VLM"
        if any(speech(x) for x in rls):
            return "Sprache"
        if "embedding" in rls:
            return "Embedding"
        if "reranker" in rls:
            return "Reranker"
        return "Chat" if (task or rls) else "—"
    return task

rows = d if isinstance(d, list) else (d.get("deployments") or d.get("items") or [])
out = []
for r in rows:
    if not isinstance(r, dict):
        continue
    name = r.get("model_name") or r.get("name") or ""
    if not name:
        continue
    # #2546: the manager row carries `task` (chat/embed/rerank), `health` (the
    # ACTUAL state, #286; `status` is the desired one, "active"), `est_gb`
    # (the footprint recorded at deploy time) and `size_gb` (the weight files
    # on the disk of a worker, for rows deployed before est_gb was derived). Older
    # shapes keep working.
    task = str(r.get("task") or r.get("kind") or r.get("model_type") or r.get("category") or "").lower()
    kind = type_label(task, roles.get(str(name)) or [])
    size = "—"
    # The footprint (est_gb) is what the column means; a file size stands in
    # only when no footprint is known, and is marked "(Datei)" so the two never
    # read as the same quantity.
    for key, scale, mark in (("est_gb", 1, ""), ("size_gb", 1, " (Datei)"),
                             ("size_bytes", 1e9, " (Datei)"), ("model_size_bytes", 1e9, " (Datei)")):
        try:
            v = float(r.get(key) or 0) / scale
        except (TypeError, ValueError):
            continue
        if v > 0:
            size = f"{v:.1f} GB{mark}"
            break
    engine = r.get("engine") or r.get("runtime") or ""
    backend = "LLM Manager" + (f" · {engine}" if engine else "")
    health = r.get("health")
    state = str(health or r.get("status") or r.get("state") or "?")
    ready = r.get("ready_instances") if r.get("ready_instances") is not None else 0
    total = r.get("replicas") if r.get("replicas") is not None else 1
    if health:
        mark = {"ready": "✅", "degraded": "◐", "failed": "❌"}.get(str(health).lower(), "⏳")
    else:
        # #2770: a manager without `health` reports only the DESIRED status
        # ("active"); its ready count is the actual state (a delivered box
        # printed "⏳ active 1/1" for four serving models).
        up = state.lower() in ("running", "ready", "healthy") or (total and ready >= total)
        mark = "✅" if up else "⏳"
    out.append(f"<tr><td><strong>{html.escape(str(name))}</strong></td><td>{html.escape(kind)}</td><td>{size}</td><td style=\"font-size:8pt;\">{html.escape(backend)}</td><td>{mark} {html.escape(state)} {ready}/{total}</td></tr>")
print("".join(out))
' 2>/dev/null)"
        if [ -n "$rows" ]; then
            RAZZFAZZ_MODEL_INVENTORY_SOURCE="manager"; printf '%s\n' "$rows"; return 0
        fi
    fi
    json="$(_rmi_gpustack_json "$env_file" || true)"
    if [ -n "$json" ]; then
        rows="$(printf '%s' "$json" | python3 -c '
import json, sys, html
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
out = []
for m in d.get("items", []):
    name = m.get("name", "?")
    cats = ", ".join(m.get("categories", [])) or "—"
    params_b = (m.get("meta", {}) or {}).get("n_params", 0) / 1e9
    ready = m.get("ready_replicas", 0); total = m.get("replicas", 0)
    mark = "✅" if ready >= total and total > 0 else "⏳"
    bp = {}
    for p in m.get("backend_parameters", []) or []:
        p = p.lstrip("-")
        if "=" in p:
            k, v = p.split("=", 1); bp[k] = v
    bp_str = "GPUStack" + (" · " + ", ".join(f"{k}={v}" for k, v in bp.items()) if bp else "")
    out.append(f"<tr><td><strong>{html.escape(str(name))}</strong></td><td>{html.escape(cats)}</td><td>{params_b:.1f}B</td><td style=\"font-size:8pt;\">{html.escape(bp_str)}</td><td>{mark} {ready}/{total}</td></tr>")
print("".join(out))
' 2>/dev/null)"
        if [ -n "$rows" ]; then
            RAZZFAZZ_MODEL_INVENTORY_SOURCE="gpustack"; printf '%s\n' "$rows"; return 0
        fi
    fi
    return 1
}

razzfazz_model_inventory_load() {
    local env_file="${1:-.env}" out
    RAZZFAZZ_MODEL_INVENTORY_HTML=""; RAZZFAZZ_MODEL_INVENTORY_SOURCE=""
    # The source label travels on the first line so it survives the subshell boundary.
    # stderr is NOT discarded: the rows function's own warnings (an unreadable
    # manifest) must reach the operator; its tool calls silence themselves.
    out="$( razzfazz_model_inventory_rows "$env_file" && printf 'SOURCE=%s\n' "$RAZZFAZZ_MODEL_INVENTORY_SOURCE" )" || return 1
    RAZZFAZZ_MODEL_INVENTORY_SOURCE="$(printf '%s\n' "$out" | sed -n 's/^SOURCE=//p' | tail -1)"
    RAZZFAZZ_MODEL_INVENTORY_HTML="$(printf '%s\n' "$out" | grep -v '^SOURCE=')"
    [ -n "$RAZZFAZZ_MODEL_INVENTORY_HTML" ]
}
