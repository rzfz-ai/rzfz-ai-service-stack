#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# razzfazz-help entrypoint
#
# The /data volume holds the cached doc mirrors + per-app refresh locks.
# When a fresh install creates the named volume, the Dockerfile build step
# chowns it to appuser. But on existing installs whose volume was
# initialized by an older help image that ran as root, the volume keeps
# root ownership and `cache_manager._set_lock` fails with PermissionError
# on every refresh attempt.
#
# Fix: ensure /data is owned by appuser on every startup, then drop to
# appuser via su-exec. Idempotent — chown is a no-op when ownership
# already matches.

set -e

if [ "$(id -u)" = "0" ]; then
    chown -R appuser:appuser /data 2>/dev/null || true
    exec su-exec appuser:appuser "$@"
fi

# Already non-root (e.g. operator overrode with --user) — just exec.
exec "$@"
