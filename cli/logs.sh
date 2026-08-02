#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz.ai Stack - Log Snapshot Script
# ==============================================================================
# This script provides CLI access to the log snapshot system.
# It collects container logs, system information, and checksum history
# into compressed tar.gz archives for debugging and support.
#
# Usage:
#   rzfz logs take [reason]         # Create a log snapshot
#   rzfz logs list                  # List available snapshots
#
# Snapshots are stored in ./backups/logs/
#
# ==============================================================================

set -eo pipefail

# M026 / S02 #2: source the shared library for colors, print_*, check_container.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

# #22: the razzfazz-setup web container was removed; the log-snapshot
# backend (cli/setup_lib/) now runs on the HOST.
export RAZZFAZZ_STACK_ROOT="$SCRIPT_DIR"
run_setup_cli() {
    python3 "${SCRIPT_DIR}/cli/setup_lib/setup.py" "$@"
}

print_help() {
    echo "rzfz.ai Log Snapshot Tool"
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  take [reason]    Create a new log snapshot (container logs, system info, etc.)"
    echo "  list             List available log snapshots"
    echo ""
    echo "Snapshots are stored in ./backups/logs/"
    echo ""
    echo "Examples:"
    echo "  $0 take \"Before stack upgrade\""
    echo "  $0 take"
    echo "  $0 list"
    echo ""
}

# #141: setup-container-INDEPENDENT collection. Works when razzfazz-setup (or the
# whole stack) is down — exactly when you most need logs (e.g. Authentik 500s and
# the in-UI snapshot is unreachable). No dependency on any container or
# in-container script: pulls straight from `docker logs` / `docker ps`, plus the
# structured upgrade journal (#143) if present. Writes one tar.gz to a writable
# dir and prints its path.
collect_standalone() {
    local reason="${1:-standalone snapshot}"
    cd "$SCRIPT_DIR" || return 1
    local ts out d c
    ts="$(date +%Y%m%d-%H%M)"
    out="${SCRIPT_DIR}/backups/logs/standalone-logs-${ts}.tar.gz"
    if ! { : >> "${SCRIPT_DIR}/backups/logs/.wtest.$$"; } 2>/dev/null; then
        out="${HOME}/standalone-logs-${ts}.tar.gz"
    else
        rm -f "${SCRIPT_DIR}/backups/logs/.wtest.$$" 2>/dev/null || true
    fi
    d="$(mktemp -d)"
    echo "reason: ${reason}" > "$d/00-reason.txt"
    { docker compose ps -a 2>&1; echo; docker ps -a 2>&1; } > "$d/00-container-status.txt"
    { git describe --tags 2>&1; git rev-parse HEAD 2>&1; cat VERSION 2>&1; } > "$d/00-version.txt"
    { uname -a; echo; df -h; echo; free -h; echo; docker version; } > "$d/00-system.txt" 2>&1
    for c in $(docker ps -a --format '{{.Names}}'); do
        docker logs --tail 1000 "$c" > "$d/log-${c}.txt" 2>&1
    done
    cp -f .upgrade-journals/upgrade-journal-*.jsonl "$d/" 2>/dev/null || true
    cp -f backups/logs/upgrade-journal-*.jsonl "$d/" 2>/dev/null || true
    tar -czf "$out" -C "$d" . && rm -rf "$d"
    print_success "Standalone log snapshot: ${out} ($(du -h "$out" 2>/dev/null | cut -f1))"
    echo "$out"
}

# Main logic
case "${1:-}" in
    take)
        REASON="${2:-Manual snapshot via CLI}"
        [ "$REASON" = "--standalone" ] && REASON="${3:-standalone snapshot}"
        # #141 / #22: --standalone forces the container-independent path;
        # otherwise the host-side log-snapshot backend runs (the in-container
        # path is gone with razzfazz-setup). The backend itself needs docker
        # to read container logs, so fall back to standalone when the stack
        # isn't up.
        if [ "${2:-}" = "--standalone" ] || [ "${3:-}" = "--standalone" ]; then
            collect_standalone "$REASON"
        elif docker info >/dev/null 2>&1; then
            run_setup_cli --logs-take "$REASON"
        else
            print_warning "docker not reachable — falling back to standalone collection (#141)."
            collect_standalone "$REASON"
        fi
        ;;
    list)
        run_setup_cli --logs-list
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
