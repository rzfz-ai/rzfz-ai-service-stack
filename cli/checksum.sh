#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz.ai Stack - Checksum Governance Script
# ==============================================================================
# This script provides CLI access to the checksum governance system.
# It tracks SHA256 checksums of all governance-relevant files (scripts, configs,
# compose files, Dockerfiles, blueprints) for change detection and auditing.
#
# Usage:
#   rzfz checksum take [comment]       # Take a checksum snapshot
#   rzfz checksum history               # Show checksum history
#   rzfz checksum detail <set_id>       # Show details of a set
#   rzfz checksum diff <id_a> <id_b>    # Compare two sets
#   rzfz checksum status                # Show current fingerprint
#
# ==============================================================================

set -eo pipefail

# M026 / S02 #1: source the shared library for colors, print_*, check_container.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

# #22: the razzfazz-setup web container was removed; the checksum backend
# (cli/setup_lib/) now runs on the HOST. RAZZFAZZ_STACK_ROOT points the
# governance DB + tracked-file scan at this repo's root.
export RAZZFAZZ_STACK_ROOT="$SCRIPT_DIR"
run_setup_cli() {
    python3 "${SCRIPT_DIR}/cli/setup_lib/setup.py" "$@"
}

print_help() {
    echo "rzfz.ai Checksum Governance"
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  take [comment]       Take a checksum snapshot of all governance files"
    echo "  history              Show checksum history (last 50 snapshots)"
    echo "  detail <set_id>      Show detailed file list for a checksum set"
    echo "  diff <id_a> <id_b>   Show diff between two checksum sets"
    echo "  status               Show current governance fingerprint"
    echo ""
    echo "Examples:"
    echo "  $0 take \"After upgrading Caddy\""
    echo "  $0 history"
    echo "  $0 detail 3"
    echo "  $0 diff 2 3"
    echo "  $0 status"
    echo ""
}

# Main logic
case "${1:-}" in
    take)
        COMMENT="${2:-Manual snapshot via CLI}"
        run_setup_cli --checksum-take "$COMMENT"
        ;;
    history)
        run_setup_cli --checksum-history
        ;;
    detail)
        if [ -z "${2:-}" ]; then
            print_error "Please specify a set ID."
            echo "Usage: $0 detail <set_id>"
            exit 1
        fi
        run_setup_cli --checksum-detail "$2"
        ;;
    diff)
        if [ -z "${2:-}" ] || [ -z "${3:-}" ]; then
            print_error "Please specify two set IDs."
            echo "Usage: $0 diff <id_a> <id_b>"
            exit 1
        fi
        run_setup_cli --checksum-diff "$2" "$3"
        ;;
    status)
        run_setup_cli --checksum-status
        ;;
    help|--help|-h)
        print_help
        ;;
    "")
        print_help
        ;;
    *)
        print_error "Unknown command: $1"
        print_help
        exit 1
        ;;
esac
