#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# Diff the live fleet against source HEAD for un-committed hot-patches.
#
# A "hot-patch" here is anything that mutates a running container's
# behaviour outside what `docker compose up -d` from source produces:
#
#   - `docker exec <c> sed -i ...` on a container's filesystem
#   - bind-mounted `*-patches/` dirs at /<something>-patches in a container
#     that aren't reflected in source-tree compose files
#   - `entrypoint:` overrides in a host-side compose that wrap an
#     upstream image's entrypoint with a wrapper script
#
# The 2026-05-13 Dify `_TokenData phase` patch is the canonical example
# of why this matters: an in-container sed lived for hours on prod 8.246
# without anyone tracking it, and would have been wiped by the next
# `compose --force-recreate dify-api`.
#
# Boxes are read from a small allowlist (set in $RAZZFAZZ_FLEET below or
# via the RAZZFAZZ_FLEET env var). Each is reached via SSH (preferably
# with ProxyJump / control-master already configured in ~/.ssh/config).
#
# Exit codes:
#   0  clean — every box's compose entrypoint overrides + patch dirs
#      match what source HEAD says they should be
#   1  drift detected — one or more boxes have something the source
#      doesn't carry yet (or carries differently)
#   2  setup error (SSH unreachable, missing tools, etc.)

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$STACK_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Allowlist of boxes to check. Override via env var if a different set
# is in play (e.g. customer fleet pre-release).
FLEET="${RAZZFAZZ_FLEET:-prod-vm}"

ISSUES=0

# What HEAD knows about: compose files declaring entrypoint overrides
# referencing in-tree wrapper scripts, and any *-patches/ dirs that are
# bind-mounted into containers.
echo "[check-hot-patches] Enumerating source-HEAD compose overrides + patch dirs..."
SOURCE_ENTRYPOINTS=$(grep -rnE '^\s*entrypoint:' --include='compose*.yml' . 2>/dev/null | grep -v '\.git/' || true)
SOURCE_PATCH_DIRS=$(find . -type d \( -name 'patches' -o -name '*-patches' -o -name '*patches' \) -not -path './.git/*' -not -path './node_modules/*' -not -path './dify/dify_orig/*' 2>/dev/null || true)
# Counter helper: empty string → 0, otherwise number of non-blank lines.
count_lines() { if [ -z "$1" ]; then echo 0; else printf '%s\n' "$1" | grep -c .; fi; }
echo -e "${CYAN}  HEAD declares $(count_lines "$SOURCE_ENTRYPOINTS") entrypoint override(s), $(count_lines "$SOURCE_PATCH_DIRS") patch dir(s).${NC}"

for box in $FLEET; do
    echo
    echo "[check-hot-patches] === $box ==="

    if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$box" 'true' 2>/dev/null; then
        echo -e "${YELLOW}  [!] $box unreachable via SSH — skipping (treat as gap).${NC}"
        ISSUES=$((ISSUES + 1))
        continue
    fi

    # Pull the live entrypoint set + bind-mounts.
    BOX_DATA=$(ssh "$box" 'set -e
        cd ~/razzfazz-ai-service-stack 2>/dev/null || { echo "NO_STACK_DIR"; exit 0; }
        echo "=== git_head ==="
        git rev-parse HEAD
        echo "=== entrypoints_override ==="
        # Live containers whose ContainerConfig.Entrypoint differs from
        # the image default. (Bash, awk, jq are stack dependencies.)
        for c in $(docker ps --format "{{.Names}}"); do
            ep=$(docker inspect "$c" --format "{{json .Config.Entrypoint}}" 2>/dev/null || echo "null")
            img_ep=$(docker inspect "$(docker inspect "$c" --format "{{.Image}}")" --format "{{json .Config.Entrypoint}}" 2>/dev/null || echo "null")
            if [ "$ep" != "$img_ep" ] && [ "$ep" != "null" ]; then
                echo "$c|$ep|$img_ep"
            fi
        done
        echo "=== patch_dir_mounts ==="
        for c in $(docker ps --format "{{.Names}}"); do
            docker inspect "$c" --format "{{range .Mounts}}{{if .Source}}{{.Source}} -> {{.Destination}}{{println}}{{end}}{{end}}" 2>/dev/null | grep -E "/(patches|patched)" || true
        done
    ')

    if echo "$BOX_DATA" | grep -q "^NO_STACK_DIR$"; then
        echo -e "${YELLOW}  [!] $box has no ~/razzfazz-ai-service-stack — skipping.${NC}"
        continue
    fi

    BOX_HEAD=$(echo "$BOX_DATA" | awk '/^=== git_head ===$/{getline; print}')
    BOX_ENTRYPOINTS=$(echo "$BOX_DATA" | awk '/^=== entrypoints_override ===$/,/^=== /' | grep -v '^=== ' || true)
    BOX_PATCH_MOUNTS=$(echo "$BOX_DATA" | awk '/^=== patch_dir_mounts ===$/,/^=== /' | grep -v '^=== ' || true)

    echo "  HEAD on box: $BOX_HEAD"
    echo "  source HEAD: $(git rev-parse HEAD)"
    if [ "$BOX_HEAD" != "$(git rev-parse HEAD)" ]; then
        # Older box is fine for this check, but flag the version-gap.
        echo -e "${CYAN}  (box is on a different commit than source HEAD — expected during rollout)${NC}"
    fi

    # For each live entrypoint override on the box: does our source carry it?
    if [ -n "$BOX_ENTRYPOINTS" ]; then
        echo "  Live entrypoint overrides:"
        echo "$BOX_ENTRYPOINTS" | while IFS='|' read -r container live_ep image_ep; do
            [ -z "$container" ] && continue
            # Extract the script path out of the JSON array.
            script=$(echo "$live_ep" | grep -oE '/[a-zA-Z0-9_/.-]+\.sh' | head -1)
            if [ -n "$script" ]; then
                # Is the script committed in source? Try to find it under
                # any */patches/ dir we know about.
                basename=$(basename "$script")
                if echo "$SOURCE_PATCH_DIRS" | xargs -I{} find {} -name "$basename" 2>/dev/null | grep -q .; then
                    echo "    [ok] $container: $script (found in source patches)"
                else
                    echo -e "${RED}    [DRIFT] $container: $script not found under source */patches/${NC}"
                    ISSUES=$((ISSUES + 1))
                fi
            fi
        done
    fi

    # For each live bind-mount into /*-patches: does the source have it?
    if [ -n "$BOX_PATCH_MOUNTS" ]; then
        echo "  Live patch-dir bind-mounts:"
        echo "$BOX_PATCH_MOUNTS" | while read -r line; do
            [ -z "$line" ] && continue
            src=$(echo "$line" | awk '{print $1}')
            dst=$(echo "$line" | awk '{print $3}')
            # If src is under the stack dir, check git has it. Else flag.
            rel=$(echo "$src" | sed 's|^/home/[^/]*/razzfazz-ai-service-stack/||')
            if git ls-files --error-unmatch "$rel" >/dev/null 2>&1; then
                echo "    [ok] $src → $dst (tracked in source)"
            else
                echo -e "${RED}    [DRIFT] $src → $dst (NOT in source HEAD)${NC}"
                ISSUES=$((ISSUES + 1))
            fi
        done
    fi
done

echo
if [ "$ISSUES" -eq 0 ]; then
    echo -e "${GREEN}[✓] Hot-patch reconciliation clean across fleet.${NC}"
    exit 0
fi

echo -e "${RED}[✗] Hot-patch reconciliation: $ISSUES drift(s) detected.${NC}"
echo -e "${YELLOW}    Either commit the patches to source or remove them from the affected box before tagging.${NC}"
exit 1
