#!/bin/bash
# apply-fix.sh — swap in the memoised Patch 2 + restart gpustack only.
#
# Reversible via revert-fix.sh.
# Does NOT touch git (the diff is committed via separate commit-fix.sh once verified).
set -eu
cd "$HOME/razzfazz-ai-service-stack"

ORIG=modules/llm/gpustack/usercustomize.py
FIXED=modules/llm/gpustack/usercustomize.py.fixed
BACKUP=modules/llm/gpustack/usercustomize.py.original

if [[ ! -f "$FIXED" ]]; then
  echo "✗ $FIXED not found — nothing to apply" >&2
  exit 1
fi

# Backup the current (in-tree) version once
if [[ ! -f "$BACKUP" ]]; then
  cp -p "$ORIG" "$BACKUP"
  echo "→ backup of original saved at $BACKUP (sha256=$(sha256sum "$BACKUP" | awk '{print $1}'))"
fi

cp "$FIXED" "$ORIG"
echo "→ swapped in fixed version (sha256=$(sha256sum "$ORIG" | awk '{print $1}'))"

echo "→ restarting gpustack to pick up the new patch"
docker compose restart gpustack 2>&1 | tail -5

echo "→ tailing gpustack logs for 10s to confirm Patch 2 active line"
timeout 12 docker compose logs --tail=0 -f gpustack 2>&1 | head -50 &
TAIL_PID=$!
sleep 12
kill "$TAIL_PID" 2>/dev/null || true
echo "→ apply-fix.sh done. Check for: '[usercustomize] Patch 2 active: ... per-(repo_id, filename) lock + cache'"
