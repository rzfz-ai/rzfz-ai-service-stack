#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz verify-images — offline-readiness image gate (#184 WS3)
# ==============================================================================
# Asserts that EVERY image the stack needs across ALL profiles is present
# locally, and exits non-zero if any is missing. The runtime never builds or
# pulls (all `build:` services carry `pull_policy: never`), so a missing image
# must fail CLEAR here rather than trigger a silent build or a raw daemon 403.
#
# The verdict follows the box's RAZZFAZZ_NETWORK_MODE (read from .env, #2368):
# offline, any gap is a FAIL; online/proxied, an image the product builds only
# when its profile is enabled (the llm-legacy pair, #1447) is a WARN ("not
# offline-ready") while that profile is off, and a FAIL once it is on.
#
# The expected set is computed by the shared source of truth
# core/config/app/services/expected_images.py (the same enumeration
# `rzfz package --include-images` bundles), covering:
#   - every custom-build image (incl. disabled modules),
#   - every pinned pull across all profiles,
#   - the LLM runtime (llm-legacy; one profile per box since #1447),
#   - the runtime-only per-user agent + gpustack-backend images.
#
# Usage:
#   rzfz verify-images                 # full report; non-zero exit on any gap
#   rzfz verify-images --quiet         # summary + missing only (for scripts)
#   rzfz verify-images --list          # print the expected image set
#   rzfz verify-images --json          # print the expected-images.json manifest
#
# Called by the upgrade pre-restart gate (cli/upgrade.sh Step 7b: the full set
# on the offline `--package` path after loading images/*.tar, `--active` online),
# by `rzfz status` and by the Config Portal offline page (offline_status.py),
# which parse the summary line. Runs on the box.
# ==============================================================================
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

MODULE="core/config/app/services/expected_images.py"
if [ ! -f "$MODULE" ]; then
    echo "rzfz verify-images: enumerator not found ($MODULE)" >&2
    exit 1
fi

# --verify is the default mode; a caller may override with --list/--json.
has_mode=0
for arg in "$@"; do
    case "$arg" in
        --verify|--list|--json|-h|--help) has_mode=1 ;;
    esac
done
if [ "$has_mode" -eq 0 ]; then
    set -- --verify "$@"
fi

exec python3 "$MODULE" --stack-root "$SCRIPT_DIR" "$@"
