#!/bin/bash
# revert-fix.sh — restore original usercustomize.py + restart gpustack.
set -eu
cd "$HOME/razzfazz-ai-service-stack"

ORIG=modules/llm/gpustack/usercustomize.py
BACKUP=modules/llm/gpustack/usercustomize.py.original

if [[ ! -f "$BACKUP" ]]; then
  echo "→ no backup found; restoring from git HEAD"
  git checkout HEAD -- "$ORIG"
else
  cp "$BACKUP" "$ORIG"
fi
echo "→ restored (sha256=$(sha256sum "$ORIG" | awk '{print $1}'))"

echo "→ restarting gpustack"
docker compose restart gpustack 2>&1 | tail -5
echo "→ revert-fix.sh done"
