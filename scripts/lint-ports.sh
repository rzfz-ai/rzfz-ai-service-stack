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

# Scan each tracked compose.yml for loopback host-bound ports.
#
# Two host shapes are in the tree and both have to be seen (#1130 / WZ-14, refs
# #855). The old scan split on the literal string "127.0.0.1:" and then
# required the next field to be all digits, which was blind twice over:
#
#   - "${OPENUEM_NATS_HOST_BIND:-127.0.0.1}:4433:4433"   env-driven host — the
#         shape the add-module template emits, and the one Wazuh/OpenUEM use.
#         Contains no literal "127.0.0.1:" prefix, so it was never seen at all.
#   - "127.0.0.1:${GITEA_HTTP_PORT:-3000}:3000"          variable port — how
#         nearly every module writes its bind. The all-digits test threw it away.
#
# The result on main was "6 ports allocated" for a tree that declares an order
# of magnitude more, i.e. a collision check that examined almost nothing.
#
# CLAIM MODEL (same reasoning as tests/unit/consistency/test_port_allocations.py):
# a port collides when two DIFFERENT claimants want it. One variable used in
# several places is one setting deployed several ways — the four gpustack
# service variants are mutually exclusive profiles, and the stack and the thin
# node both bind $LLM_WORKER_AGENT_PORT. Literal ports are claimed per FILE for
# the same reason: `40000-40063` is repeated once per gpustack variant inside a
# single compose file. Consequence, deliberate and shared with the python
# guard: two different services in ONE file hardcoding the same literal port
# are not distinguished. Cross-file collisions — the new-module case this exists
# for — are.
#
# Non-loopback binds (Caddy's 0.0.0.0 ingress, gpustack's worker ports) are out
# of scope here; `test_every_bind_is_loopback_or_a_documented_exception` in
# tests/unit/consistency/test_port_allocations.py is where that is asked.
LOOPBACK_HOST='(\$\{[A-Za-z_][A-Za-z0-9_]*:-127\.0\.0\.1\}|127\.0\.0\.1)'
PORT_TOKEN='(\$\{([A-Za-z_][A-Za-z0-9_]*):-([0-9]+(-[0-9]+)?)\}|([0-9]+(-[0-9]+)?))'
BIND_RE="^[[:space:]]*-[[:space:]]*\"${LOOPBACK_HOST}:${PORT_TOKEN}:"

while IFS= read -r line; do
    file="${line%%:*}"
    rest="${line#*:}"          # strip "<file>:"
    content="${rest#*:}"       # strip "<lineno>:"

    [[ "$content" =~ $BIND_RE ]] || continue
    port_var="${BASH_REMATCH[3]}"
    port="${BASH_REMATCH[4]:-${BASH_REMATCH[6]}}"
    [[ -n "$port" ]] || continue

    # One setting = one claimant. A variable claims its port everywhere it is
    # used; a hardcoded port claims it once per file.
    if [[ -n "$port_var" ]]; then
        claim="\$$port_var"
    else
        claim="literal@$file"
    fi

    if [[ -z "${SEEN[$port]:-}" ]]; then
        SEEN[$port]="$claim"
        COUNT=$((COUNT + 1))
    elif [[ " ${SEEN[$port]} " != *" $claim "* ]]; then
        echo "ERROR: port $port collision: ${SEEN[$port]} <-> $claim ($file)" >&2
        SEEN[$port]="${SEEN[$port]} $claim"
        EXIT=1
    fi
done < <(
    if [ ${#tracked_composes[@]} -gt 0 ]; then
        # -H so the filename is present even when exactly one compose file is
        # scanned; without it the field split below eats the line number.
        grep -HEn "${BIND_RE}" "${tracked_composes[@]}" 2>/dev/null || true
    fi
)

if [[ $EXIT -eq 0 ]]; then
    echo "lint-ports: OK ($COUNT ports allocated, no collisions)"
else
    echo "lint-ports: FAIL — fix port collisions above" >&2
fi

exit $EXIT
