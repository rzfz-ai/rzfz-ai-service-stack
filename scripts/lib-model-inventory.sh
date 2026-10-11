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
#       Size · Backend · Status) and RAZZFAZZ_MODEL_INVENTORY_SOURCE ("manager" |
#       "gpustack" | "") IN THE CALLER'S SHELL; returns 0 with rows, 1 with nothing —
#       the CALLER must say so loudly.
#   razzfazz_model_inventory_rows [ENV_FILE]      prints the rows; the SOURCE variable it
#       sets is lost when called through $(...) — which is exactly how the generators
#       called it, so the stdout label read "Model inventory:  (4 models)" on 0.91.
RAZZFAZZ_MODEL_INVENTORY_SOURCE=""
RAZZFAZZ_MODEL_INVENTORY_HTML=""

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
    docker exec gpustack curl -s --max-time 15 http://localhost:9090/v1/models \
        -H "Authorization: Bearer ${key}" 2>/dev/null
}

razzfazz_model_inventory_rows() {
    local env_file="${1:-.env}" json rows
    RAZZFAZZ_MODEL_INVENTORY_SOURCE=""
    json="$(_rmi_manager_json "$env_file" || true)"
    if [ -n "$json" ]; then
        rows="$(printf '%s' "$json" | python3 -c '
import json, sys, html
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rows = d if isinstance(d, list) else (d.get("deployments") or d.get("items") or [])
out = []
for r in rows:
    if not isinstance(r, dict):
        continue
    name = r.get("model_name") or r.get("name") or ""
    if not name:
        continue
    kind = r.get("kind") or r.get("model_type") or r.get("category") or "—"
    size = r.get("size_bytes") or r.get("model_size_bytes")
    size = f"{int(size)/1e9:.1f} GB" if size else "—"
    engine = r.get("engine") or r.get("runtime") or ""
    backend = "LLM Manager" + (f" · {engine}" if engine else "")
    state = str(r.get("status") or r.get("state") or "?")
    ready = r.get("ready_instances") if r.get("ready_instances") is not None else 0
    total = r.get("replicas") if r.get("replicas") is not None else 1
    mark = "✅" if str(state).lower() in ("running", "ready", "healthy") else "⏳"
    out.append(f"<tr><td><strong>{html.escape(str(name))}</strong></td><td>{html.escape(str(kind))}</td><td>{size}</td><td style=\"font-size:8pt;\">{html.escape(backend)}</td><td>{mark} {html.escape(state)} {ready}/{total}</td></tr>")
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
    out="$( { razzfazz_model_inventory_rows "$env_file" && printf 'SOURCE=%s\n' "$RAZZFAZZ_MODEL_INVENTORY_SOURCE"; } 2>/dev/null )" || return 1
    RAZZFAZZ_MODEL_INVENTORY_SOURCE="$(printf '%s\n' "$out" | sed -n 's/^SOURCE=//p' | tail -1)"
    RAZZFAZZ_MODEL_INVENTORY_HTML="$(printf '%s\n' "$out" | grep -v '^SOURCE=')"
    [ -n "$RAZZFAZZ_MODEL_INVENTORY_HTML" ]
}
