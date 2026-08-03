#!/bin/sh
# coding-tools entrypoint
# Configures git identity, writes gsd + opencode provider config,
# starts the web UI, then keeps the container alive.

set -e

HOME_DIR="${HOME:-/home/agent}"
GPUSTACK_KEY="${GPUSTACK_API_KEY:-}"
GPUSTACK_BASE_URL="${OPENAI_BASE_URL:-http://gpustack:9090/v1}"

# ── Git identity ──────────────────────────────────────────────────────────────
if [ -n "${GIT_AUTHOR_NAME}" ]; then
    git config --global user.name "${GIT_AUTHOR_NAME}"
    git config --global user.email "${GIT_AUTHOR_EMAIL:-agent@localhost}"
fi

# ── Gitea git credentials ─────────────────────────────────────────────────────
if [ -n "${GITEA_API_TOKEN}" ]; then
    git config --global credential.helper store
    GITEA_HOST=$(echo "${GITEA_INTERNAL_URL:-http://gitea:3000}" | sed 's|http[s]*://||' | cut -d/ -f1)
    echo "http://agent:${GITEA_API_TOKEN}@${GITEA_HOST}" > "${HOME_DIR}/.git-credentials"
    chmod 600 "${HOME_DIR}/.git-credentials"
fi

# ── gsd: models.json — GPUStack as a named custom provider ───────────────────
# ~/.gsd/agent/models.json is the official custom-provider config for gsd/pi.
# Docs: gsd-build/GSD-2/docs/user-docs/custom-models.md
#
# M031 S2: content comes from $GSD_MODELS_JSON set by agent-manager from
# core/llm/standard-models.yaml — no hardcoded models or context numbers.
# Fresh provisions get the current YAML; existing provisions keep their
# on-disk file (idempotent — only write if absent, so operator edits in
# the gsd UI are preserved across container recreates).
GSD_AGENT_DIR="${HOME_DIR}/.gsd/agent"
mkdir -p "${GSD_AGENT_DIR}"
MODELS_JSON="${GSD_AGENT_DIR}/models.json"
if [ ! -f "${MODELS_JSON}" ] && [ -n "${GSD_MODELS_JSON}" ]; then
    printf '%s\n' "${GSD_MODELS_JSON}" > "${MODELS_JSON}"
    echo "[coding-tools] gsd models.json written from \$GSD_MODELS_JSON (standard-models.yaml)"
fi

# ── gsd: preferences.md — pin gpustack/$LLM_MODEL for all phases ─────────────
# M031 S2: model identifier read from $LLM_MODEL (resolved by agent-manager
# from standard-models.yaml's defaults.coding). Operator can switch a fresh
# agent's preferred model by editing the YAML's defaults.coding.
PREFS="${HOME_DIR}/.gsd/PREFERENCES.md"
LLM_MODEL_FOR_PREFS="${LLM_MODEL:-qwen3.6}"
if [ ! -f "${PREFS}" ]; then
    cat > "${PREFS}" << EOF
---
version: 1
models:
  research: gpustack/${LLM_MODEL_FOR_PREFS}
  planning: gpustack/${LLM_MODEL_FOR_PREFS}
  execution: gpustack/${LLM_MODEL_FOR_PREFS}
  completion: gpustack/${LLM_MODEL_FOR_PREFS}
  validation: gpustack/${LLM_MODEL_FOR_PREFS}
---
EOF
    echo "[coding-tools] gsd PREFERENCES.md written (model=${LLM_MODEL_FOR_PREFS})"
fi

# ── opencode: config.json — GPUStack via @ai-sdk/openai-compatible ───────────
# Uses npm provider loading — same pattern as other openai-compat endpoints.
#
# M031 S2: content comes from $OPENCODE_CONFIG_JSON set by agent-manager
# from core/llm/standard-models.yaml — no hardcoded models, context windows,
# or default-model choice. Same idempotency policy as gsd above.
OC_CONFIG_DIR="${HOME_DIR}/.config/opencode"
mkdir -p "${OC_CONFIG_DIR}"
OC_CONFIG="${OC_CONFIG_DIR}/config.json"
if [ ! -f "${OC_CONFIG}" ] && [ -n "${OPENCODE_CONFIG_JSON}" ]; then
    printf '%s\n' "${OPENCODE_CONFIG_JSON}" > "${OC_CONFIG}"
    echo "[coding-tools] opencode config.json written from \$OPENCODE_CONFIG_JSON (standard-models.yaml)"
fi

# ── Start web UI ──────────────────────────────────────────────────────────────
echo "[coding-tools] gsd $(gsd --version 2>/dev/null || echo 'not found')"
echo "[coding-tools] Starting web UI on port ${CODING_TOOLS_WEB_PORT:-3004}..."
CODING_TOOLS_WEB_PORT=${CODING_TOOLS_WEB_PORT:-3004} \
HOME="${HOME_DIR}" \
    /opt/webui-venv/bin/python3 /opt/coding-tools-web/app.py \
    >> /tmp/coding-tools-web.log 2>&1 &

# ── M020 S05 — opencode serve (background daemon, optional) ───────────────────
# Started when OPENCODE_SERVE_AT_BOOT=true (set by the per-user agent-manager
# catalog entry). Listens on :4096 internal-only — the M020 opencode pipe
# routes per-user via agent-manager's Caddy registration. Authenticated via
# OPENCODE_SERVER_PASSWORD (Basic auth, password from instance.config
# _generated_secret per M020 S02).
#
# Failures here do NOT kill the container — the keep-alive `tail -f` at the
# bottom keeps the container up so the operator can `docker exec` in to
# debug.
if [ "${OPENCODE_SERVE_AT_BOOT:-false}" = "true" ]; then
    echo "[coding-tools] Starting opencode serve on :4096..."
    OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-}" \
    HOME="${HOME_DIR}" \
        opencode serve --port 4096 --hostname 0.0.0.0 \
        >> /tmp/opencode-serve.log 2>&1 &
fi

# ── M020 S06 — gsd --web (background daemon, optional) ────────────────────────
# Started when GSD_WEB_AT_BOOT=true. Listens on :8080 internal-only — Caddy
# routes per-user at agents.<domain>/<slug>/ via agent-manager registration.
# gsd state persists at /workspace/.gsd/ (the workspace volume).
if [ "${GSD_WEB_AT_BOOT:-false}" = "true" ]; then
    echo "[coding-tools] Starting gsd --web on :8080..."
    HOME="${HOME_DIR}" \
        gsd --web --port 8080 --hostname 0.0.0.0 \
        >> /tmp/gsd-web.log 2>&1 &
fi

echo "[coding-tools] Container ready (user=$(id -un), uid=$(id -u))."
echo "[coding-tools]   Web UI:   http://localhost:${CODING_TOOLS_WEB_PORT:-3004}"
echo "[coding-tools]   gsd:      docker exec -it coding-tools gsd"
echo "[coding-tools]   opencode: docker exec -it coding-tools opencode"
[ "${OPENCODE_SERVE_AT_BOOT:-false}" = "true" ] && echo "[coding-tools]   opencode serve: http://localhost:4096"
[ "${GSD_WEB_AT_BOOT:-false}" = "true" ] && echo "[coding-tools]   gsd --web:      http://localhost:8080"

exec tail -f /dev/null
