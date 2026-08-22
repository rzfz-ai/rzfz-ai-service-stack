#!/bin/sh
set -e

LLAMA_BOX_DIR="/usr/local/lib/python3.10/dist-packages/gpustack/third_party/bin/llama-box"
DEFAULT_DIR="$LLAMA_BOX_DIR/llama-box-default"

# Räume altes llama-box-default auf (falls aus vorherigem Build vorhanden)
rm -rf "$DEFAULT_DIR" 2>/dev/null || true

# Hintergrund-Watcher: Wartet bis GPUStack llama-box-default erstellt hat,
# dann ersetzt die Binary mit unserer Vulkan-Version.
(
  echo "[vulkan-injector] Waiting for llama-box-default to appear..."
  TIMEOUT=300
  ELAPSED=0
  while [ ! -e "$DEFAULT_DIR/llama-box" ] && [ $ELAPSED -lt $TIMEOUT ]; do
    sleep 2
    ELAPSED=$((ELAPSED + 2))
  done

  if [ -e "$DEFAULT_DIR/llama-box" ]; then
    echo "[vulkan-injector] llama-box-default found, injecting Vulkan build..."
    chattr -i "$DEFAULT_DIR/llama-box" 2>/dev/null || true
    cp -a /tmp/vulkan_release/. "$DEFAULT_DIR/"
    chattr +i "$DEFAULT_DIR/llama-box"
    echo "[vulkan-injector] Vulkan llama-box v0.0.171 injection complete."
  else
    echo "[vulkan-injector] WARNING: Timeout waiting for llama-box-default!"
  fi
) &

echo "Starting gpustack..."
exec tini -- gpustack start "$@"
