#!/bin/bash
# Pre-entrypoint patches for the upstream langgenius/dify-api image.
#
# Each block here is a surgical, idempotent fix for a known upstream bug
# that we can't yet drop because no fixed Dify release exists. The fix
# is applied to /app/api in the running container before the upstream
# /entrypoint.sh boots gunicorn/celery.
#
# Idempotency rule: every block must be safe to re-run on every container
# start. sed-based blocks use grep guards so they no-op when upstream has
# merged the fix.

set -e

#
# Patch 1 — _TokenData TypedDict is missing `phase`, breaks every password
# reset and change-email flow with 400 invalid_or_expired_token. Upstream
# PR #36117 (langgenius/dify) — open as of 2026-05-13, not yet merged.
#
# When upstream merges and we move to a Dify release that contains the
# fix, this block becomes a no-op (the grep already sees `phase: str`) and
# can be deleted in a later cleanup pass.
#
HELPER=/app/api/libs/helper.py
if [ -f "$HELPER" ] && ! grep -q '^    phase: str$' "$HELPER"; then
    echo "[dify-patch] _TokenData missing phase — applying PR #36117 fix"
    sed -i '/^class _TokenData(TypedDict/,/^$/ s|^    old_email: str$|    old_email: str\n    phase: str|' "$HELPER"
fi

exec /entrypoint.sh "$@"
