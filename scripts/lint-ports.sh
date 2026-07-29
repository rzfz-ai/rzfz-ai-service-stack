#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# lint-ports.sh — fail if any */compose.yml host-bound port collides with another.
#
# Scans every `*/compose.yml` in the repo for `127.0.0.1:NNNN:` patterns,
# deduplicates by port, errors out on collisions.
#
# Phase-1 of M016 only catches collisions; phase-2 will also enforce the
# 8200-8299 range for newly-added modules (existing modules legitimately
# sit at 3000s/5000s/8000s/9000s/13xxx).
#
# Usage:
#   bash scripts/lint-ports.sh
#
# Exit codes:
#   0 — no collisions
#   1 — at least one port collision detected
#
# Wired into:
#   - scripts/prepare-release.sh (release gate)
#   - operator may also install as a git pre-commit hook:
#       ln -sf ../../scripts/lint-ports.sh .git/hooks/pre-commit

set -eo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

declare -A SEEN
EXIT=0
COUNT=0

# Only scan git-tracked compose files — stops scratch/backup copies in
# the working tree (e.g. a developer's local core/Caddy/compose.yml) from
# being treated as a second source of truth and flagged as collisions.
# Fall back to a filesystem scan when we're not running inside a git
# repo (e.g. in an extracted offline package).
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    mapfile -t tracked_composes < <(git ls-files '**/compose.yml' 'compose.yml' 2>/dev/null)
else
    mapfile -t tracked_composes < <(find . -name compose.yml \
        -not -path './.git/*' -not -path './.claude/*' \
        -not -path './node_modules/*' -not -path './backups/*' \
        -not -path './security-run/*' 2>/dev/null)
fi

# Scan each tracked compose.yml for host-bound ports.
# Format expected: "127.0.0.1:NNNN:..." inside a `ports:` list entry.
while IFS= read -r line; do
    file="${line%%:*}"
    rest="${line#*127.0.0.1:}"
    port="${rest%%:*}"
    if [[ ! "$port" =~ ^[0-9]+$ ]]; then continue; fi

    if [[ -n "${SEEN[$port]:-}" ]]; then
        echo "ERROR: port $port collision: ${SEEN[$port]} <-> $file" >&2
        EXIT=1
    else
        SEEN[$port]="$file"
        COUNT=$((COUNT + 1))
    fi
done < <(
    if [ ${#tracked_composes[@]} -gt 0 ]; then
        grep -En '127\.0\.0\.1:[0-9]+:' "${tracked_composes[@]}" 2>/dev/null || true
    fi
)

if [[ $EXIT -eq 0 ]]; then
    echo "lint-ports: OK ($COUNT ports allocated, no collisions)"
else
    echo "lint-ports: FAIL — fix port collisions above" >&2
fi

exit $EXIT
