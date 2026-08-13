#!/bin/sh
# Write opencode.json to the persisted volume at startup so model discovery
# picks up the GPUStack provider with credentials from env vars.
# This runs as root before gosu drops to node — hence we chown afterwards.

OPENCODE_CONF_DIR="${PAPERCLIP_HOME:-/paperclip}/.config/opencode"
OPENCODE_CONF="${OPENCODE_CONF_DIR}/opencode.json"

if [ -n "${GPUSTACK_API_KEY:-}" ] && [ -n "${GPUSTACK_BASE_URL:-}" ]; then
    mkdir -p "$OPENCODE_CONF_DIR"
    # rc6.7 #83: two fixes vs the previous version of this template.
    # (1) `$schema` was shell-expanded to empty inside the unquoted
    #     heredoc, so the resulting JSON had `"": "https://..."` and
    #     opencode rejected the whole file with "Configuration is
    #     invalid … Unrecognized key:". Escape with backslash; matches
    #     the same pattern in agents/coding-tools/entrypoint.sh.
    # (2) Add top-level `model` default so the paperclip
    #     opencode_local adapter (and `opencode run` direct
    #     invocations) pick up gpustack/qwen3-coder-next without
    #     the operator having to type the provider/model id at every
    #     agent-creation prompt. Pick qwen3-coder-next as the
    #     default — it's the coding-oriented model in our GPUStack
    #     catalog. gemma4 stays available in the picker for
    #     conversational work.
    cat > "$OPENCODE_CONF" << JSONEOF
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "gpustack": {
      "name": "GPUStack",
      "npm": "@ai-sdk/openai-compatible",
      "env": ["GPUSTACK_API_KEY"],
      "options": {
        "baseURL": "${GPUSTACK_BASE_URL}",
        "apiKey": "${GPUSTACK_API_KEY}"
      },
      "models": {
        "qwen3-coder-next": { "name": "Qwen3 Coder Next (GPUStack)" },
        "gemma4": { "name": "Gemma 4 (GPUStack)" }
      }
    }
  },
  "model": "gpustack/qwen3-coder-next"
}
JSONEOF
    chown -R node:node "${PAPERCLIP_HOME:-/paperclip}/.local" 2>/dev/null || true
    chown -R node:node "${PAPERCLIP_HOME:-/paperclip}/.config" 2>/dev/null || true
    echo "opencode: GPUStack provider configured (${GPUSTACK_BASE_URL})"
else
    echo "opencode: GPUSTACK_API_KEY not set, skipping GPUStack provider config"
fi
