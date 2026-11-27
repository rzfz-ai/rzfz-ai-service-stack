#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
set -euo pipefail
# scripts/wazuh-fleet-groups.sh — #2526: the fleet groups on a running manager.
#
# Usage:
#   wazuh-fleet-groups.sh [--ensure]              every shipped group exists (creates missing ones)
#   wazuh-fleet-groups.sh --assign AGENT GROUP    move an enrolled agent into GROUP
#                                                 (GROUP becomes its ONLY group)
#   wazuh-fleet-groups.sh -h | --help             this text
#
# The shipped groups are the directories under
# modules/apps/wazuh/configs/wazuh_manager/shared/ (one agent.conf each; the
# securityconfig one-shot copies them to the manager). Run --ensure before the
# first agent enrols with --group: authd refuses an unknown group.
#
# Exit: 0 ok; 1 the manager is unreachable, a group is still missing after
# --ensure, or the agent/group is unknown; 2 usage.
#
# Every list is read ONCE into a variable and then matched exactly. A
# `list | grep -q` under pipefail can report "missing" for a group that is
# there (grep exits on the first match, the writer gets SIGPIPE), and a sed
# pattern built from a name treats its dots as wildcards.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED="${RZFZ_WAZUH_SHARED_DIR:-$ROOT/modules/apps/wazuh/configs/wazuh_manager/shared}"
MANAGER="${WAZUH_MANAGER_CONTAINER:-wazuh-manager}"
NAME_RE='^[A-Za-z0-9_.-]{1,64}$'

usage() { sed -n '/^# Usage:/,/^#   wazuh-fleet-groups.sh -h/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

agent_groups() { docker exec "$MANAGER" /var/ossec/bin/agent_groups "$@"; }

# `agent_groups -l` prints "  <name> (<agent count>)" per group (Wazuh 4.14).
group_list() {
    local out
    out="$(agent_groups -l 2>&1)" || {
        echo "[fleet-groups] cannot list groups on $MANAGER: $out" >&2
        return 1
    }
    printf '%s\n' "$out" | awk '/^  [^ ]+ \([0-9]+\)$/ { print $1 }'
}

has_line() {  # $1 = list, $2 = exact line
    local line
    while IFS= read -r line; do [ "$line" = "$2" ] && return 0; done <<<"$1"
    return 1
}

case "${1:---ensure}" in
    -h|--help) usage; exit 0 ;;
    --ensure)
        [ $# -le 1 ] || { usage >&2; exit 2; }
        shipped=()
        for d in "$SHARED"/*/; do
            [ -d "$d" ] && shipped+=("$(basename "$d")")
        done
        [ "${#shipped[@]}" -gt 0 ] || { echo "[fleet-groups] no group directories under $SHARED" >&2; exit 1; }
        have="$(group_list)" || exit 1
        missing=0
        for g in "${shipped[@]}"; do
            if has_line "$have" "$g"; then echo "[fleet-groups] $g: present"; continue; fi
            if ! agent_groups -a -g "$g" -q >/dev/null 2>&1; then
                echo "[fleet-groups] $g: create FAILED" >&2
            fi
            have="$(group_list)" || exit 1
            if has_line "$have" "$g"; then
                echo "[fleet-groups] $g: created"
            else
                echo "[fleet-groups] $g: STILL MISSING" >&2
                missing=1
            fi
        done
        exit "$missing" ;;
    --assign)
        [ $# -eq 3 ] || { echo "usage: $0 --assign AGENT GROUP" >&2; exit 2; }
        agent="$2"; group="$3"
        [[ "$group" =~ $NAME_RE ]] && [ "$group" != . ] && [ "$group" != .. ] \
            || { echo "[fleet-groups] invalid group '$group'" >&2; exit 2; }
        have="$(group_list)" || exit 1
        has_line "$have" "$group" || { echo "[fleet-groups] no group '$group' on the manager (run --ensure)" >&2; exit 1; }
        # `agent_control -l`: "   ID: 001, Name: host-1, IP: any, Active". Exact
        # name compare in awk, never a regex built from the name.
        agents="$(docker exec "$MANAGER" /var/ossec/bin/agent_control -l 2>&1)" \
            || { echo "[fleet-groups] cannot list agents on $MANAGER: $agents" >&2; exit 1; }
        id="$(printf '%s\n' "$agents" | awk -F', ' -v want="Name: $agent" '
            $2 == want { sub(/^ *ID: */, "", $1); print $1; exit }')"
        [ -n "$id" ] || { echo "[fleet-groups] no agent named '$agent'" >&2; exit 1; }
        # `-a -i -g` alone ADDS a group (Wazuh 4.x multi-group): the agent would
        # keep `default` or its old group and that group's policy with it. `-f`
        # replaces the assignment with exactly this one group.
        agent_groups -a -f -i "$id" -g "$group" -q >/dev/null
        # Read back, never assume: "... belongs to groups: a, b." (4.14 -s).
        shown="$(agent_groups -s -i "$id" 2>&1)" \
            || { echo "[fleet-groups] cannot read the groups of agent $id: $shown" >&2; exit 1; }
        now="$(printf '%s\n' "$shown" | sed -n 's/.*belongs to groups*: *//p' | head -n 1)"
        now="${now%.}"
        if [ "$now" != "$group" ]; then
            echo "[fleet-groups] agent $agent ($id) is in '${now:-?}' after the move, expected only '$group'" >&2
            exit 1
        fi
        echo "[fleet-groups] agent $agent ($id) -> $group (only group)" ;;
    *) usage >&2; exit 2 ;;
esac
