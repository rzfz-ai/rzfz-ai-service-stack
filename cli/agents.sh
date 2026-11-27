#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz agents — per-user agent instances: list + update (Gitea #3187)
#
#   rzfz agents list [--behind-count]        list instances + update status
#   rzfz agents update [--all|--instance <id>] [--dry-run]
#                                            recreate every instance that is
#                                            behind on its image (update_available)
#                                            onto the current image; volumes kept
#
# The work happens INSIDE the agent-manager container (it owns the docker
# socket and the instance DB), via `docker exec ... python3 -c cli_main()` —
# the same pattern cli/post-install.sh's coding-agent re-key uses. This script
# is the host-side shell: check the manager runs, forward the flags, pass the
# output through. `--behind-count` prints just the number (for the upgrade
# summary; no jq on a box).
# ==============================================================================
set -euo pipefail

RZFZ_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
MANAGER_CONTAINER="${AGENT_MANAGER_CONTAINER:-agent-manager}"

usage() {
    cat <<EOF
rzfz agents — list and update per-user agent instances

Usage:
  rzfz agents list [--behind-count]     list instances and their update status
                                        (--behind-count: print only how many are
                                         behind, for scripts)
  rzfz agents update [--all]            recreate every instance with an update
                                        available onto the current image
                                        (chats/skills/files/config are kept)
  rzfz agents update --instance <id>    update just that instance
  rzfz agents update --dry-run          show what would be updated, change nothing

Runs against the agent-manager container; the agents module must be on.
EOF
}

cmd="list"
instance=""
dry_run=0
behind_count=0
while [ $# -gt 0 ]; do
    case "$1" in
        list|update)   cmd="$1" ;;
        --all)         : ;;                       # default: update all behind
        --instance)    shift; instance="${1:?--instance needs an instance id or container name}" ;;
        --dry-run)     dry_run=1 ;;
        --behind-count) behind_count=1 ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "rzfz agents: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

command -v docker >/dev/null 2>&1 || { echo "rzfz agents: docker is not available on this box" >&2; exit 1; }
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$MANAGER_CONTAINER"; then
    echo "rzfz agents: the agent-manager is not running (agents module off or stack down) — nothing to do" >&2
    exit 1
fi

out="$(docker exec \
    -e RZFZ_AGENTS_CMD="$cmd" \
    -e RZFZ_AGENTS_INSTANCE="$instance" \
    -e RZFZ_AGENTS_DRY_RUN="$dry_run" \
    -e RAZZFAZZ_ADMIN_USERNAME="${RAZZFAZZ_ADMIN_USERNAME:-}" \
    "$MANAGER_CONTAINER" python3 -c \
    'from app.services.agent_updates import cli_main; raise SystemExit(cli_main())' 2>&1)" || {
    printf '%s\n' "$out" >&2
    echo "rzfz agents: the in-container pass failed — if the agent-manager image predates this command, recreate it first: docker compose up -d --no-deps --force-recreate $MANAGER_CONTAINER" >&2
    exit 1
}

if [ "$behind_count" = 1 ]; then
    # last AGENTS_JSON line, "behind":N — extracted without jq
    behind="$(printf '%s\n' "$out" | sed -n 's/.*AGENTS_JSON:.*"behind":[ ]*\([0-9][0-9]*\).*/\1/p' | tail -n 1)"
    printf '%s\n' "${behind:-0}"
else
    printf '%s\n' "$out"
fi
