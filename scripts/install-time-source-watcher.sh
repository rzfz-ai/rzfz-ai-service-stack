#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# install-time-source-watcher.sh — install the systemd path unit that applies a
# time source change queued by the Settings portal (scripts/time-source-watcher.sh)
# =============================================================================
#   sudo scripts/install-time-source-watcher.sh            # install / refresh, idempotent
#   sudo scripts/install-time-source-watcher.sh --remove   # stop and delete both units
# Run by `rzfz init` and `rzfz upgrade` like the other host files they install.
#
# Failure policy: set -e is on; a tolerated failure carries an explicit `|| warn`.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
UNIT_DIR="${RAZZFAZZ_TIME_UNIT_DIR:-/etc/systemd/system}"
SKIP_ROOT="${RAZZFAZZ_TIME_SKIP_ROOT_CHECK:-0}"
UNITS=(razzfazz-time-source.path razzfazz-time-source.service)

warn() { echo "install-time-source-watcher: WARN $*" >&2; }
die()  { echo "install-time-source-watcher: $*" >&2; exit 1; }

if [ "$SKIP_ROOT" != "1" ] && [ "$(id -u)" -ne 0 ]; then die "run as root (sudo)"; fi

if [ "${1:-}" = "--remove" ]; then
    systemctl disable --now razzfazz-time-source.path || warn "could not stop the path unit"
    for u in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$u" || warn "could not remove $UNIT_DIR/$u"; done
    systemctl daemon-reload || warn "daemon-reload failed"
    exit 0
fi

case "$SCRIPT_DIR" in *[[:space:]]*|*[@%\\]*) die "the stack path '$SCRIPT_DIR' has a character a unit file cannot carry" ;; esac
mkdir -p "$UNIT_DIR"
changed=false
for u in "${UNITS[@]}"; do
    tmp="$(mktemp)"
    sed "s|@STACK_DIR@|${SCRIPT_DIR}|g" "$SCRIPT_DIR/scripts/systemd/$u.in" > "$tmp"
    if ! cmp -s "$tmp" "$UNIT_DIR/$u" 2>/dev/null; then
        install -m 0644 "$tmp" "$UNIT_DIR/$u"
        changed=true
    fi
    rm -f "$tmp"
done
if $changed; then systemctl daemon-reload || die "systemctl daemon-reload failed"; fi
systemctl enable --now razzfazz-time-source.path || die "could not enable razzfazz-time-source.path"
echo "install-time-source-watcher: razzfazz-time-source.path is active ($UNIT_DIR)"
