#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz adopt-models — hand GPUStack's weights to the LLM Manager, no re-download
# ==============================================================================
# 2026.09 moves the box from GPUStack to the LLM Manager. The models do NOT have
# to be fetched again: the same GGUF files serve both, they only live in
# different volumes and in a different layout.
#
#   GPUStack  gpustack-data:/var/lib/gpustack
#               cache/huggingface/<repo_id>/<filename>
#               local-models/<repo_id>/<filename>        (sideloaded / offline)
#
#   Manager   llm-node-models:/models
#               <basename(filename)>                      FLAT — no repo dirs
#
# The flat layout is not a simplification made here: it is what the node already
# assumes. `hf_pull.ensure_file()` builds its destination as
# `os.path.join(models_dir, os.path.basename(filename))` and returns "cached"
# when that file exists — so a weight copied to the right name is simply never
# downloaded. `missing_files()` reads the same shape.
#
# On a 30–60 GB standard set that is the difference between a coffee and an
# afternoon, and on a metered or air-gapped line it is the difference between
# possible and not.
#
# Usage:
#   rzfz adopt-models                  # copy what is missing, report the rest
#   rzfz adopt-models --dry-run        # say what WOULD be copied, touch nothing
#   rzfz adopt-models --from DIR       # adopt from a directory instead of the
#                                      # gpustack-data volume (offline package,
#                                      # restored backup, USB disk)
#   rzfz adopt-models --max-copy-bytes N  # refuse (exit 3) a COPY larger than N
#   rzfz adopt-models --link-only         # never copy; refuse if a file cannot be linked
#   rzfz adopt-models --list           # what the manager expects, and where it is
#   rzfz adopt-models --layout gpustack --from DIR
#                                      # the OTHER direction: place a flat set
#                                      # into GPUStack's local-models shape
#                                      # (subdir for shards, repo dir for the
#                                      # projectors — see core/llm/model_source.py)
#
# Idempotent: a weight already present in llm-node-models is left alone, never
# overwritten and never re-hashed.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# House rule (#382 / cli-entrypoints): `rzfz` execs this from wherever the
# operator stands, and core/llm/expected_models.py resolves `.env` and the
# model manifest relative to the stack root.
cd "$SCRIPT_DIR"
# shellcheck source=scripts/lib.sh
[ -f "$SCRIPT_DIR/scripts/lib.sh" ] && . "$SCRIPT_DIR/scripts/lib.sh"

# Empty means "not a dry run". This is NOT cosmetic: the two planner calls pass
# the flag as `${DRY_RUN:+--dry-run}`, which expands whenever the variable is
# non-empty — with `DRY_RUN=false` that expanded to `--dry-run` on EVERY run,
# so the adoption printed its plan and copied nothing, on the box and on the
# host. Found by the #1544 review's point that the call line had no guard;
# `test_1544_adopt_models_wrapper.py` now runs the wrapper with a docker stub
# and reads the argv.
DRY_RUN=""
FROM_DIR=""
LIST_ONLY=false
LAYOUT=manager
MAX_COPY_BYTES=""
LINK_ONLY=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --from)    FROM_DIR="${2:-}"; shift 2 ;;
        --list)    LIST_ONLY=true; shift ;;
        --layout)  LAYOUT="${2:-manager}"; shift 2 ;;
        --max-copy-bytes) MAX_COPY_BYTES="${2:-}"; shift 2 ;;
        --link-only) LINK_ONLY=1; shift ;;
        -h|--help) sed -n '5,41p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "adopt-models: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

# ── the expected set ─────────────────────────────────────────────────────────
# Same source of truth as `rzfz verify-models` and an offline package (Enterprise): core/llm/expected_models.py, filtered to this box's active
# profiles. Deliberately NOT a second list — a copy would drift, and a weight
# this script does not know about is a weight the manager waits for for ever.
adopt_expected_models() {
    python3 "$SCRIPT_DIR/core/llm/expected_models.py" --json 2>/dev/null
}

# ── where the manager wants them ─────────────────────────────────────────────
adopt_models_volume() {
    # The worker agent's compose pins the FULL volume name (project-scoped) —
    # a bare "llm-node-models" resolves to a different, empty volume. Read the
    # same variable the compose file reads, with the same default.
    printf '%s' "${LLM_WORKER_MODELS_VOLUME:-razzfazz-stack_llm-node-models}"
}

# ── run ──────────────────────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

python3 "$SCRIPT_DIR/core/llm/expected_models.py" --json          > "$TMP/expected.json"
python3 "$SCRIPT_DIR/core/llm/expected_models.py" --list-mmproj   > "$TMP/mmproj.tsv"

PLANNER="$SCRIPT_DIR/core/llm/adopt_models.py"

if [ "$LIST_ONLY" = true ]; then
    python3 "$PLANNER" --expected "$TMP/expected.json" --mmproj "$TMP/mmproj.tsv" \
        --src "${FROM_DIR:-/var/lib/gpustack}" --dst /models --layout "$LAYOUT" --json
    exit 0
fi

# Host mode: --from points at a directory this shell can read (an unpacked
# offline package, a restored backup, a USB disk). No Docker, no volumes.
if [ -n "$FROM_DIR" ]; then
    [ -d "$FROM_DIR" ] || { echo "adopt-models: --from '$FROM_DIR' is not a directory" >&2; exit 2; }
    DST_DIR="${RZFZ_ADOPT_DEST:-}"
    if [ -z "$DST_DIR" ]; then
        echo "adopt-models: --from needs RZFZ_ADOPT_DEST (the models directory to fill)." >&2
        echo "  On a box that is usually the llm-node-models volume; use the container form instead." >&2
        exit 2
    fi
    exec python3 "$PLANNER" --expected "$TMP/expected.json" --mmproj "$TMP/mmproj.tsv" \
        --src "$FROM_DIR" --dst "$DST_DIR" --layout "$LAYOUT" \
        ${MAX_COPY_BYTES:+--max-copy-bytes "$MAX_COPY_BYTES"} ${LINK_ONLY:+--link-only} ${DRY_RUN:+--dry-run}
fi

# Box mode: one throwaway container. GPUStack's volume is mounted READ-ONLY —
# this reads weights, it never changes the backend we are migrating away from, and a mistake here would cost the fallback.
command -v docker >/dev/null 2>&1 || { echo "adopt-models: docker is required on a box (use --from for a directory)." >&2; exit 2; }

GPUSTACK_VOL="$(docker volume ls -q 2>/dev/null | grep -E '_gpustack-data$' | head -1)"
[ -n "$GPUSTACK_VOL" ] || { echo "adopt-models: no gpustack-data volume on this box — nothing to adopt." >&2; exit 0; }
MODELS_VOL="$(adopt_models_volume)"
docker volume inspect "$MODELS_VOL" >/dev/null 2>&1 || {
    echo "adopt-models: the manager's models volume '$MODELS_VOL' does not exist yet." >&2
    echo "  Start the LLM Manager trio once (rzfz start), then re-run." >&2
    exit 2
}

echo "Adopting from volume '$GPUSTACK_VOL' → '$MODELS_VOL'"
[ -n "$DRY_RUN" ] && echo "(dry run — nothing is written)"

# #2400: link(2) answers EXDEV across two mount points even on one filesystem,
# so the two-mount form always copied. When both volumes live under one
# parent, that parent is mounted ONCE more at /vols and used ONLY as the link
# path; the planner still reads and copies through GPUSTACK_VOL:/src:ro, the
# fallback protection while the migration is unproven.
SRC_MP="$(docker volume inspect -f '{{.Mountpoint}}' "$GPUSTACK_VOL" 2>/dev/null || true)"
DST_MP="$(docker volume inspect -f '{{.Mountpoint}}' "$MODELS_VOL" 2>/dev/null || true)"
VOL_ROOT=""
if [ -n "$SRC_MP" ] && [ -n "$DST_MP" ]; then
    _root="$(dirname "$(dirname "$SRC_MP")")"
    # The /vols paths below are BUILT from the volume names, so the mountpoints
    # must have exactly that shape; anything else adopts by copy.
    if [ "$SRC_MP" = "${_root}/${GPUSTACK_VOL}/_data" ] && [ "$DST_MP" = "${_root}/${MODELS_VOL}/_data" ]; then
        VOL_ROOT="$_root"
    fi
fi
MOUNTS=(-v "${GPUSTACK_VOL}:/src:ro")
if [ -n "$VOL_ROOT" ]; then
    MOUNTS+=(-v "${VOL_ROOT}:/vols")
    TARGET=(--dst "/vols/${MODELS_VOL}/_data" --link-src "/vols/${GPUSTACK_VOL}/_data")
else
    echo "adopt-models: the two volumes do not share a parent ('${SRC_MP:-?}', '${DST_MP:-?}') — adopting by copy." >&2
    MOUNTS+=(-v "${MODELS_VOL}:/dst")
    TARGET=(--dst /dst)
fi
# #2400: never pull on an air-gapped box — the worker-agent image carries
# python3 and is on every manager box.
ADOPT_IMAGE="${RZFZ_ADOPT_IMAGE:-razzfazz-llm-worker-agent:latest}"
docker run --rm --pull never --entrypoint python3 \
    "${MOUNTS[@]}" \
    -v "${TMP}:/plan:ro" \
    -v "${PLANNER}:/adopt_models.py:ro" \
    "$ADOPT_IMAGE" \
    /adopt_models.py --expected /plan/expected.json --mmproj /plan/mmproj.tsv \
        --src /src "${TARGET[@]}" \
        --layout "$LAYOUT" ${MAX_COPY_BYTES:+--max-copy-bytes "$MAX_COPY_BYTES"} ${LINK_ONLY:+--link-only} ${DRY_RUN:+--dry-run}
