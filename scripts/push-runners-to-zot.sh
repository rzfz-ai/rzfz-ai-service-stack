#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #549 R2 — publish the master's locally-built runner images into its own Zot
# registry, so nodes can pull them over the LAN (deploy_runner) instead of each
# box building its own.
#
#   ./scripts/push-runners-to-zot.sh [--registry host:port] [--dry-run]
#
# Pushes every LOCALLY PRESENT runner image under runners/<name>:<tag>, e.g.
#   llama-vulkan-runner:b8943  ->  llm-registry:5000/runners/llama-vulkan-runner:b8943
#
# The image list is NOT hard-coded here: it is derived from what init/upgrade
# actually build, read from cli/init.sh's _build_runner lines — the same
# authority tests/unit/llm-node-agent/test_engine_image_names.py compares the
# drivers against (#331). A runner renamed there is picked up here without a
# second edit, and a name invented here that init does not build cannot exist.
#
# Idempotent: pushing an already-present tag is a registry no-op. Absent local
# images are SKIPPED with a note, not an error — a CPU master legitimately never
# built the vulkan runner (#331's hardware gating).
#
# ⚠ UNVERIFIED AGAINST A LIVE ZOT (authored in a code-only environment, #549 R2).
# The docker tag/push mechanics are standard; what a live run must confirm is
# that this Zot accepts docker-daemon pushes on the loopback port and that a
# node-side deploy_runner pull round-trips. Run with --dry-run first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

REGISTRY="127.0.0.1:${LLM_REGISTRY_PORT:-8093}"
DRY_RUN=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --registry) REGISTRY="${2:?--registry needs host:port}"; shift 2 ;;
        --dry-run)  DRY_RUN=true; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# The runner list, from the shell that actually builds them (cli/init.sh).
# #1516 (E5): the list is DERIVED from modules/llm/runners/runners.yaml — the
# same manifest cli/init.sh, cli/upgrade.sh and cli/post-install.sh build from.
# It used to be grepped out of init.sh's `_build_runner` lines; those are gone,
# and a hard-coded list here would be the two-authorities bug (#331/#547) on its
# sixth surface. Legacy aliases ride along for one cycle so a pinned box still
# resolves.
# shellcheck source=/dev/null
. "$(dirname "$0")/lib.sh"
mapfile -t RUNNERS < <(razzfazz_runner_manifest_rows "$(dirname "$0")/.." \
    | awk -F'\t' 'NF{print $1; if ($4 != "") print $4}' | sort -u)
if [ "${#RUNNERS[@]}" -eq 0 ]; then
    echo "ERROR: modules/llm/runners/runners.yaml yielded no runner images — the authority moved (or PyYAML is missing)." >&2
    exit 1
fi

pushed=0; skipped=0
for img in "${RUNNERS[@]}"; do
    name="${img%%:*}"; tag="${img##*:}"
    target="${REGISTRY}/runners/${name}:${tag}"
    if ! docker image inspect "$img" >/dev/null 2>&1; then
        echo "  skip  ${img}  (not built on this box — hardware-gated, see #331)"
        skipped=$((skipped + 1)); continue
    fi
    if [ "$DRY_RUN" = true ]; then
        echo "  would push  ${img}  ->  ${target}"
    else
        docker tag "$img" "$target"
        docker push "$target"
        echo "  pushed  ${target}"
    fi
    pushed=$((pushed + 1))
done
echo "done: ${pushed} pushed/planned, ${skipped} skipped (not present locally)"
