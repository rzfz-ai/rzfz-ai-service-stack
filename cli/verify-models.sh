#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz verify-models — offline model-readiness GGUF gate (#184 P1 / WS7c)
# ==============================================================================
# Asserts that every standard-model GGUF the enabled LLM profile needs is present
# in the gpustack-data volume (the local-models/ sideload dir OR the HuggingFace
# cache dir), and exits non-zero if any is missing. The offline deploy path
# (post-install / sync, source=local_path) registers from THESE files and never
# reaches huggingface.co — so a missing GGUF must fail CLEAR here rather than let
# GPUStack silently try the internet.
#
# The expected set is computed by the shared model source of truth
# core/llm/expected_models.py (the same enumeration `rzfz package
# --include-models` bundles), read from standard-models.yaml and filtered to the
# box's active profiles (requires_profile). In offline mode this NEVER falls back
# to huggingface.co.
#
# Usage:
#   rzfz verify-models                 # full report; non-zero exit on any gap
#   rzfz verify-models --quiet         # summary + missing only (for scripts)
#   rzfz verify-models --list          # print the expected model set
#   rzfz verify-models --json          # print the expected-models.json manifest
#
# Runs on the box (reads the gpustack-data volume via the running gpustack
# container). Mirrors cli/verify-images.sh.
# ==============================================================================
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

MODULE="core/llm/expected_models.py"
if [ ! -f "$MODULE" ]; then
    echo "rzfz verify-models: enumerator not found ($MODULE)" >&2
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
