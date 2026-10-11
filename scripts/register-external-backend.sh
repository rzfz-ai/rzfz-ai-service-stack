#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# register-external-backend.sh  (#254 P2-C1 / #307)
# -----------------------------------------------------------------------------
# Register a PRE-EXISTING OpenAI-compatible endpoint — e.g. a Mac running Ollama
# — as an external backend in the LLM Manager. No engine container is launched
# (the Mac runs Ollama itself); we PUBLISH its endpoint so the manager folds it
# into the fleet router. One endpoint serves many models (Ollama routes by
# model name), so every model points at the same /v1 endpoint.
#
# HARD REQUIREMENT: the manager AND its LiteLLM router must be able to REACH the
# endpoint over the network. Run this from a box that can reach BOTH the manager
# and the Mac (e.g. prod, or a dev box on the OpenVPN that routes to the Mac's
# subnet). A dev box without that VPN cannot route to a Mac on the worker subnet.
#
# Usage:
#   register-external-backend.sh --name mac-studio \
#       --endpoint http://192.0.2.10:11434/v1 \
#       [--manager http://127.0.0.1:8091] [--node-key KEY] \
#       [--models qwen3:8b,nomic-embed-text] \
#       [--hardware apple-silicon] [--engine ollama] [--api-key KEY] [--dry-run]
#
# --models omitted  → auto-discovered from GET <endpoint>/models.
# --node-key omitted → read LLM_MANAGER_NODE_KEY from ./.env.
# --dry-run          → print the registration payload, POST nothing (works even
#                      when the Mac isn't reachable, if you pass --models).
set -euo pipefail

NAME="" ENDPOINT="" MANAGER="http://127.0.0.1:8091" NODE_KEY="" MODELS=""
HARDWARE="apple-silicon" ENGINE="ollama" API_KEY="" DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --endpoint) ENDPOINT="$2"; shift 2;;
    --manager) MANAGER="${2%/}"; shift 2;;
    --node-key) NODE_KEY="$2"; shift 2;;
    --models) MODELS="$2"; shift 2;;
    --hardware) HARDWARE="$2"; shift 2;;
    --engine) ENGINE="$2"; shift 2;;
    --api-key) API_KEY="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$NAME" ] && [ -n "$ENDPOINT" ] || { echo "ERROR: --name and --endpoint are required" >&2; exit 2; }
ENDPOINT="${ENDPOINT%/}"

# node key: explicit, else from .env next to this repo root
if [ -z "$NODE_KEY" ] && [ -f .env ]; then
  NODE_KEY="$(grep -m1 '^LLM_MANAGER_NODE_KEY=' .env | cut -d= -f2- | tr -d '"' || true)"
fi
[ -n "$NODE_KEY" ] || { echo "ERROR: no node key (--node-key or LLM_MANAGER_NODE_KEY in .env)" >&2; exit 2; }

# host part of the endpoint → worker 'address'
ADDRESS="$(printf '%s' "$ENDPOINT" | sed -E 's#^https?://##; s#[:/].*$##')"

# discover models if not supplied
if [ -z "$MODELS" ]; then
  echo "[*] discovering models at $ENDPOINT/models ..." >&2
  MODELS="$(curl -fsS -m 10 "$ENDPOINT/models" \
    | python3 -c 'import sys,json; print(",".join(m["id"] for m in json.load(sys.stdin).get("data",[])))')"
  [ -n "$MODELS" ] || { echo "ERROR: no models found at $ENDPOINT/models (reachable? try --models)" >&2; exit 1; }
fi
echo "[*] models: $MODELS" >&2

PAYLOAD="$(NAME="$NAME" ADDRESS="$ADDRESS" ENDPOINT="$ENDPOINT" HARDWARE="$HARDWARE" \
  ENGINE="$ENGINE" API_KEY="$API_KEY" MODELS="$MODELS" python3 - <<'PY'
import json, os
models = [m.strip() for m in os.environ["MODELS"].split(",") if m.strip()]
api_key = os.environ["API_KEY"] or None
print(json.dumps({
    "name": os.environ["NAME"],
    "address": os.environ["ADDRESS"],
    "hardware": os.environ["HARDWARE"],
    "engine": os.environ["ENGINE"],
    # An endpoint registered through THIS script is external by definition
    # (no worker-agent, no heartbeat, nothing to place onto). The manager
    # infers the label only from `engine == ollama` (#307); `--engine vllm`
    # produced a worker that placement then targeted (#1348 box run, 0.91).
    "external": True,
    "role": "worker",
    "models": [
        {"model_name": m, "served_model": m, "endpoint": os.environ["ENDPOINT"],
         "status": "ready", "api_key": api_key}
        for m in models
    ],
}))
PY
)"

if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] would POST to $MANAGER/api/workers:"; echo "$PAYLOAD" | python3 -m json.tool
  exit 0
fi

echo "[*] registering '$NAME' ($ENGINE @ $ENDPOINT) with the manager ..." >&2
RESP="$(curl -fsS -m 15 -X POST "$MANAGER/api/workers" \
  -H "Authorization: Bearer $NODE_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")"
echo "$RESP"
echo "[✓] registered. Check Deployments — the Mac's models should appear and answer in the Playground." >&2
