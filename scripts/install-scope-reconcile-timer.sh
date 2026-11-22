#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# install-scope-reconcile-timer.sh — install the systemd timer that runs the
# knowledge-scope and role reconcile every hour (scripts/scope-reconcile-run.sh)
# =============================================================================
#   sudo scripts/install-scope-reconcile-timer.sh            # install / refresh, idempotent
#   sudo scripts/install-scope-reconcile-timer.sh --remove   # stop and delete both units
# Run by `rzfz init` and `rzfz upgrade` like the other host units they install.
# The service runs as the owner of the stack's .env (who runs `rzfz`, with
# access to Docker), never as root unless root owns the stack. A box without a
# .env (a worker node) gets nothing.
#
# Failure policy: set -e is on; a tolerated failure carries an explicit `|| warn`.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
UNIT_DIR="${RAZZFAZZ_SCOPE_UNIT_DIR:-/etc/systemd/system}"
SKIP_ROOT="${RAZZFAZZ_SCOPE_SKIP_ROOT_CHECK:-0}"
UNITS=(razzfazz-scope-reconcile.service razzfazz-scope-reconcile.timer)

warn() { echo "install-scope-reconcile-timer: WARN $*" >&2; }
die()  { echo "install-scope-reconcile-timer: $*" >&2; exit 1; }

if [ "$SKIP_ROOT" != "1" ] && [ "$(id -u)" -ne 0 ]; then die "run as root (sudo)"; fi

if [ "${1:-}" = "--remove" ]; then
    systemctl disable --now razzfazz-scope-reconcile.timer || warn "could not stop the timer"
    for u in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$u" || warn "could not remove $UNIT_DIR/$u"; done
    systemctl daemon-reload || warn "daemon-reload failed"
    exit 0
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "install-scope-reconcile-timer: no .env in $SCRIPT_DIR (not a master): nothing installed"
    exit 0
fi
case "$SCRIPT_DIR" in *[[:space:]]*|*[@%\\]*) die "the stack path '$SCRIPT_DIR' has a character a unit file cannot carry" ;; esac
STACK_USER="${RAZZFAZZ_SCOPE_USER:-$(stat -c %U "$SCRIPT_DIR/.env")}"
[[ "$STACK_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "the owner of $SCRIPT_DIR/.env ('$STACK_USER') is no usable user name"

mkdir -p "$UNIT_DIR"
changed=false
for u in "${UNITS[@]}"; do
    tmp="$(mktemp)"
    sed -e "s|@STACK_DIR@|${SCRIPT_DIR}|g" -e "s|@STACK_USER@|${STACK_USER}|g" "$SCRIPT_DIR/scripts/systemd/$u.in" > "$tmp"
    if ! cmp -s "$tmp" "$UNIT_DIR/$u" 2>/dev/null; then
        install -m 0644 "$tmp" "$UNIT_DIR/$u"
        changed=true
    fi
    rm -f "$tmp"
done
if $changed; then systemctl daemon-reload || die "systemctl daemon-reload failed"; fi
systemctl enable --now razzfazz-scope-reconcile.timer || die "could not enable razzfazz-scope-reconcile.timer"
echo "install-scope-reconcile-timer: razzfazz-scope-reconcile.timer is active, runs as $STACK_USER ($UNIT_DIR)"
