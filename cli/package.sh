#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz.ai Stack - Upgrade Package Creator
# ==============================================================================
# Creates an offline upgrade package from a specific git tag. The package
# contains the full repository working tree (without .git history), migration
# manifests, and optionally pre-built Docker images.
#
# Usage:
#   rzfz package v1.1.0                    # Create package for tag
#   rzfz package v1.1.0 --include-images   # Include Docker images
#   rzfz package --help
#
# This script runs on the DEVELOPER machine, not on the production box.
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# M026 / S02 #5: source the shared library for colors and print_* helpers.
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"
cd "$SCRIPT_DIR"

show_help() {
    echo "rzfz.ai Stack - Upgrade Package Creator"
    echo ""
    echo "Usage: $0 TAG [OPTIONS]"
    echo ""
    echo "Arguments:"
    echo "  TAG                          Git tag to package (e.g., v1.1.0)"
    echo ""
    echo "Options:"
    echo "  --include-images             Include Docker images in package (large!)"
    echo "  --include-models             Include the standard-model GGUFs (VERY large — tens of GB)"
    echo "  --models-from REF|PATH       (implies --include-models) bundle only the model GGUFs"
    echo "                               ADDED or CHANGED vs the standard-models.yaml at this git"
    echo "                               ref or path — an UPGRADE/DELTA package for a box that"
    echo "                               already has the base set"
    echo "  --community                  Base/community package — do NOT bundle the"
    echo "                               Enterprise overlay (default: overlay included)"
    echo "  --output DIR                 Output directory (default: current dir)"
    echo "  -h, --help                   Show this help"
    echo ""
    echo "By default (#125 P1.5) the package bundles the Enterprise overlay (gated docs +"
    echo "release security assessment + SBOM); the offline apply-path stages it into the"
    echo "box-local overlay/enterprise/ so an airgapped subscription box needs no fetch."
    echo ""
    echo "Examples:"
    echo "  $0 v1.1.0                                 # Subscription package (overlay bundled)"
    echo "  $0 v1.1.0 --community                     # Base/community package (no overlay)"
    echo "  $0 v1.1.0 --include-images                # With Docker images (~5GB+)"
    echo "  $0 v1.1.0 --include-images --include-models  # Fully self-contained (images + GGUFs)"
    echo "  $0 v1.1.0 --include-models --models-from v1.0.0  # Only models added/changed since v1.0.0"
    echo "  $0 v1.1.0 --output /tmp/packages          # Custom output dir"
    echo ""
    echo "The generated package can be used on the production box with:"
    echo "  rzfz upgrade --package razzfazz-v1.1.0.tar.gz"
    exit 0
}

# ==============================================================================
# Parse Arguments
# ==============================================================================
TAG=""
INCLUDE_IMAGES=false
# #184 P1 / WS7a — bundle the standard-model GGUFs (offline boxes register them
# via source=local_path, never huggingface.co). --models-from makes it a delta.
INCLUDE_MODELS=false
MODELS_FROM=""
# #125 P1.5: default = subscription package (bundle the Enterprise overlay). --community
# opts out for a base/community package.
INCLUDE_OVERLAY=true
OUTPUT_DIR="$SCRIPT_DIR"

while [[ $# -gt 0 ]]; do
    case $1 in
        --include-images) INCLUDE_IMAGES=true; shift ;;
        --include-models) INCLUDE_MODELS=true; shift ;;
        --models-from)    MODELS_FROM="$2"; INCLUDE_MODELS=true; shift 2 ;;
        --community)      INCLUDE_OVERLAY=false; shift ;;
        --output)         OUTPUT_DIR="$2"; shift 2 ;;
        -h|--help)        show_help ;;
        -*)               print_error "Unknown option: $1"; exit 1 ;;
        *)
            if [ -z "$TAG" ]; then
                TAG="$1"
            else
                print_error "Unexpected argument: $1"
                exit 1
            fi
            shift
            ;;
    esac
done

if [ -z "$TAG" ]; then
    print_error "Git tag is required."
    echo "Usage: $0 TAG [OPTIONS]"
    echo "Run $0 --help for more information."
    exit 1
fi

# ==============================================================================
# Validate Prerequisites
# ==============================================================================
print_step "Validating prerequisites..."

if [ ! -d ".git" ]; then
    print_error "Must be run from a git repository."
    exit 1
fi

if ! git rev-parse "$TAG" &>/dev/null; then
    print_error "Tag '${TAG}' not found in repository."
    print_substep "Available tags:"
    git tag -l | head -20
    exit 1
fi

if ! command -v tar &>/dev/null; then
    print_error "tar is required."
    exit 1
fi

if [ "$INCLUDE_IMAGES" = true ] && ! command -v docker &>/dev/null; then
    print_error "Docker is required for --include-images."
    exit 1
fi

if [ "$INCLUDE_MODELS" = true ] && ! command -v docker &>/dev/null; then
    print_error "Docker is required for --include-models (GGUFs are read from the gpustack-data volume)."
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Version from tag (strip v prefix)
VERSION="${TAG#v}"
PACKAGE_NAME="razzfazz-v${VERSION}"
# Stage on the SAME (large) filesystem as the output, NOT tmpfs /tmp: the
# all-profiles image set is ~140GB and would overflow a RAM-backed /tmp,
# silently truncating the offline package to whatever fit before ENOSPC (#184).
# Honor RAZZFAZZ_PACKAGE_STAGING for an explicit override.
STAGING_BASE="${RAZZFAZZ_PACKAGE_STAGING:-$OUTPUT_DIR}"
STAGING_DIR=$(mktemp -d "${STAGING_BASE}/.razzfazz-package-XXXXXX")

trap 'rm -rf "$STAGING_DIR"' EXIT

print_success "Prerequisites validated."

# ==============================================================================
# Export Repository at Tag
# ==============================================================================
print_step "Exporting repository at tag ${TAG}..."

git archive --format=tar --prefix="" "$TAG" | tar x -C "$STAGING_DIR"
print_substep "Repository exported ($(du -sh "$STAGING_DIR" | cut -f1))."

# Verify VERSION file in export
if [ -f "${STAGING_DIR}/VERSION" ]; then
    local_version=$(cat "${STAGING_DIR}/VERSION" | tr -d '[:space:]')
    print_substep "Package version: ${local_version}"
    if [ "$local_version" != "$VERSION" ]; then
        print_warning "VERSION file (${local_version}) does not match tag (${VERSION})."
    fi
else
    print_warning "No VERSION file in export. Creating one."
    echo "$VERSION" > "${STAGING_DIR}/VERSION"
fi

# Remove files not needed in upgrade package
rm -rf "${STAGING_DIR}/.git" \
       "${STAGING_DIR}/backups" \
       "${STAGING_DIR}/multipass" \
       "${STAGING_DIR}/tests" \
       "${STAGING_DIR}/.checksums.db" 2>/dev/null || true

print_success "Repository exported."

# ==============================================================================
# Bundle the Enterprise overlay (#125 P1.5)
# ==============================================================================
# "Everything we build is by definition a subscription box." Bundle the full Enterprise
# overlay (gated docs + release security assessment + SBOM) as a SELF-CONTAINED payload
# under enterprise-overlay/. The offline apply-path (cli/upgrade.sh --package →
# code_update_package) stages it into the box-local, gitignored overlay/enterprise/ via
# scripts/sync-enterprise-overlay.sh --from-payload — so an airgapped subscription box
# gets the overlay with no network fetch, and it survives Codeberg upgrades. Pass
# --community to skip and ship a base/community package.
if [ "$INCLUDE_OVERLAY" = true ]; then
    print_step "Bundling Enterprise overlay (docs + security assessment + SBOM)..."
    if [ -x "${SCRIPT_DIR}/scripts/assemble-enterprise-overlay-payload.sh" ]; then
        "${SCRIPT_DIR}/scripts/assemble-enterprise-overlay-payload.sh" \
            "${STAGING_DIR}/enterprise-overlay" --tag "$TAG" --sbom-mode reuse \
            2>&1 | sed 's/^/  /' \
          || print_warning "Overlay-payload assembly reported an issue — package may carry a partial overlay."
        print_success "Enterprise overlay bundled (enterprise-overlay/)."
    else
        print_warning "assemble-enterprise-overlay-payload.sh missing — package ships WITHOUT the Enterprise overlay."
    fi
else
    print_substep "--community: base/community package — Enterprise overlay NOT bundled."
fi

# ==============================================================================
# Include standard-model GGUFs (optional) — #184 P1 / WS7a
# ==============================================================================
# Bundle the standard-model GGUFs from the dev box's gpustack-data volume so an
# OFFLINE box can register them via source=local_path (WS7b) — never reaching
# huggingface.co. Staged FIRST (before the images block) so the image free-space
# preflight sees the reduced free space (both flags → both accounted for).
#
# FULL package (default): every standard model in the TARGET tag's
# standard-models.yaml. DELTA package (--models-from REF|PATH): only models
# ADDED or CHANGED vs that source YAML — for a box that already has the base set.
# A model not cached locally is warned + skipped (like a missing image).
if [ "$INCLUDE_MODELS" = true ]; then
    print_step "Bundling standard-model GGUFs (offline package — VERY large)..."

    mkdir -p "${STAGING_DIR}/models"

    EXP_MODELS="${SCRIPT_DIR}/core/llm/expected_models.py"
    # The tag's model declarations were exported into staging by `git archive`.
    TARGET_MODELS_YAML="${STAGING_DIR}/core/llm/standard-models.yaml"

    if [ ! -f "$EXP_MODELS" ] || [ ! -f "$TARGET_MODELS_YAML" ]; then
        print_warning "  Model enumerator or target standard-models.yaml missing — package ships WITHOUT model GGUFs."
    else
        # ---- Resolve the DELTA source YAML (optional) ----------------------
        DELTA_ARG=()
        if [ -n "$MODELS_FROM" ]; then
            SRC_MODELS_YAML="${STAGING_DIR}/.source-models.yaml"
            if [ -f "$MODELS_FROM" ]; then
                cp "$MODELS_FROM" "$SRC_MODELS_YAML"
            elif git cat-file -e "${MODELS_FROM}:core/llm/standard-models.yaml" 2>/dev/null; then
                git show "${MODELS_FROM}:core/llm/standard-models.yaml" > "$SRC_MODELS_YAML" 2>/dev/null
            else
                print_warning "  --models-from '${MODELS_FROM}' is neither a file nor a git ref carrying core/llm/standard-models.yaml — bundling the FULL model set instead."
                SRC_MODELS_YAML=""
            fi
            [ -n "$SRC_MODELS_YAML" ] && DELTA_ARG=(--delta-from "$SRC_MODELS_YAML") \
                && print_substep "  DELTA package: bundling models added/changed vs ${MODELS_FROM}."
        fi

        # ---- Enumerate the target model set (alias \t repo \t filename) ----
        model_rows=$(python3 "$EXP_MODELS" --stack-root "$SCRIPT_DIR" \
            --config "$TARGET_MODELS_YAML" --all-profiles --list "${DELTA_ARG[@]}" 2>/dev/null || true)

        if [ -z "$model_rows" ]; then
            print_warning "  No standard-model GGUFs to bundle (empty set / delta had no adds+changes)."
        else
            # sizes TSV: filename \t size — feeds expected-models.json below.
            SIZES_TSV="${STAGING_DIR}/.model-sizes.tsv"
            : > "$SIZES_TSV"
            # alpine copy script: find the model's GGUF(s) under local-models/ or
            # the HF cache in the volume, copy to /out preserving any sub-dir,
            # print total bytes. repo/filename are $1/$2 (never interpolated).
            copy_script='
set -e
repo="$1"; filename="$2"
sdir=$(dirname "$filename"); sbase=$(basename "$filename")
[ "$sdir" = "." ] && sdir=""
total=0
for base in "/data/local-models" "/data/cache/huggingface/$repo"; do
  srcdir="$base"; [ -n "$sdir" ] && srcdir="$base/$sdir"
  [ -d "$srcdir" ] || continue
  for f in "$srcdir"/$sbase; do
    [ -e "$f" ] || continue
    destdir="/out"; [ -n "$sdir" ] && destdir="/out/$sdir"
    mkdir -p "$destdir"
    cp "$f" "$destdir/$(basename "$f")"
    total=$((total + $(wc -c < "$f")))
  done
  [ "$total" -gt 0 ] && break
done
echo "$total"
'
            # Resolve the REAL gpustack-data volume. AUTHORITATIVE source: the
            # volume the running gpustack container actually mounts at
            # /var/lib/gpustack. compose prefixes it with the project name
            # (razzfazz-stack_gpustack-data); a BARE "gpustack-data" would mount
            # an empty, auto-created volume and bundle nothing (and a stray bare
            # volume can shadow the real one under a plain grep) — #184 P1 review.
            GPUSTACK_VOL="$(docker inspect gpustack --format '{{range .Mounts}}{{if eq .Destination "/var/lib/gpustack"}}{{.Name}}{{end}}{{end}}' 2>/dev/null)"
            [ -z "$GPUSTACK_VOL" ] && GPUSTACK_VOL="$(docker volume ls -q 2>/dev/null | grep -E '_gpustack-data$' | head -1)"
            [ -z "$GPUSTACK_VOL" ] && GPUSTACK_VOL="$(docker volume ls -q 2>/dev/null | grep -E '(^|_)gpustack-data$' | head -1)"
            [ -z "$GPUSTACK_VOL" ] && GPUSTACK_VOL="gpustack-data"
            print_substep "  Reading GGUFs from volume: ${GPUSTACK_VOL}"
            model_count=0
            model_missing=0
            models_total_bytes=0
            while IFS=$'\t' read -r m_alias m_repo m_filename; do
                [ -z "$m_alias" ] && continue
                sz=$(docker run --rm -v "$GPUSTACK_VOL":/data:ro \
                        -v "${STAGING_DIR}/models":/out alpine \
                        sh -c "$copy_script" _ "$m_repo" "$m_filename" 2>/dev/null | tail -n1)
                sz=${sz:-0}
                case "$sz" in ''|*[!0-9]*) sz=0 ;; esac
                if [ "$sz" -gt 0 ]; then
                    printf '%s\t%s\n' "$m_filename" "$sz" >> "$SIZES_TSV"
                    models_total_bytes=$((models_total_bytes + sz))
                    model_count=$((model_count + 1))
                    print_substep "  Bundled: ${m_alias} ($(( sz / 1024 / 1024 ))MB) — ${m_filename}"
                else
                    print_warning "  Not cached locally, skipped: ${m_alias} (${m_repo}/${m_filename})"
                    model_missing=$((model_missing + 1))
                fi
            done <<< "$model_rows"

            # ---- expected-models.json (mirror expected-images.json) --------
            MODELS_JSON="${STAGING_DIR}/expected-models.json" \
            TARGET_YAML="$TARGET_MODELS_YAML" \
            SIZES="$SIZES_TSV" \
            STACK_ROOT="$SCRIPT_DIR" \
            python3 - "$EXP_MODELS" <<'PYJSON'
import json, os, sys
sys.path.insert(0, os.path.dirname(sys.argv[1]))
import expected_models as em
sizes = {}
try:
    with open(os.environ["SIZES"], encoding="utf-8") as fh:
        for line in fh:
            if "\t" in line:
                fn, sz = line.rstrip("\n").split("\t", 1)
                sizes[fn] = int(sz or 0)
except OSError:
    pass
spec = em.load_spec(os.environ["TARGET_YAML"])
models = []
for alias, repo, filename, roles in em.iter_models(spec, all_profiles=True):
    models.append({"name": alias, "repo": repo, "filename": filename,
                   "roles": roles, "size": int(sizes.get(filename, 0))})
manifest = {
    "schema": "razzfazz.expected-models/v1",
    "stack_version": em._stack_version(os.environ["STACK_ROOT"]),
    "preset": "standard",
    "count": len(models),
    "bundled": sum(1 for m in models if m["size"] > 0),
    "models": models,
}
with open(os.environ["MODELS_JSON"], "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2)
PYJSON
            rm -f "$SIZES_TSV" "${STAGING_DIR}/.source-models.yaml" 2>/dev/null || true

            models_total_gb=$(( models_total_bytes / 1024 / 1024 / 1024 ))
            if [ "$model_missing" -gt 0 ]; then
                print_warning "Bundled ${model_count} model GGUFs (~${models_total_gb}GB); ${model_missing} expected model(s) were NOT cached locally. The offline model set is INCOMPLETE — download them on the dev box (rzfz post-install) and re-run."
            else
                print_warning "Bundled ${model_count} model GGUFs — total ~${models_total_gb}GB. This makes the package VERY large; ensure the transfer medium + target disk have room."
                print_success "Model GGUFs bundled (complete set for this package)."
            fi
        fi
    fi
fi

# ==============================================================================
# Include Docker Images (optional)
# ==============================================================================
if [ "$INCLUDE_IMAGES" = true ]; then
    # WS1 / #184: bundle the ALL-PROFILES image set, NOT just the dev box's
    # ACTIVE profiles. An offline box may enable ANY module later and the
    # runtime never builds/pulls (`pull_policy: never`), so the package must
    # carry every custom build (incl. disabled modules), every pinned pull, all
    # LLM-runtime variants, and the runtime-only per-user agent + gpustack
    # backend images. The expected set + the `expected-images.json` manifest are
    # computed by the SHARED source of truth
    # core/config/app/services/expected_images.py — the SAME enumeration
    # `rzfz verify-images` asserts against on the box.
    #
    # NB: no `local` here — this block runs at top-level (not in a function),
    # where `local` errors and, under `set -e`, would abort the whole save
    # (the pre-#184 bug that made `--include-images` a no-op).
    print_step "Saving Docker images (ALL profiles — offline package)..."

    mkdir -p "${STAGING_DIR}/images"

    # Checkout tag temporarily so the compose config + builds match the target.
    current_ref=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || git rev-parse HEAD)
    git checkout "$TAG" --quiet 2>/dev/null

    EXP_ENUM="${SCRIPT_DIR}/core/config/app/services/expected_images.py"
    BUILD_PREFLIGHT="${SCRIPT_DIR}/core/config/app/services/build_preflight.py"
    # All profiles EXCEPT the llm-runtime trio (which collide on container_name);
    # the trio is rendered one profile at a time below.
    ALL_PROFILES="$(python3 "$BUILD_PREFLIGHT" --stack-root "$SCRIPT_DIR" --build-profiles 2>/dev/null || true)"
    LLM_PROFILES="llm llm-legacy llm-cpu"

    # ---- Write the expected-images.json manifest into the package ----------
    print_substep "Writing expected-images.json manifest..."
    if python3 "$EXP_ENUM" --stack-root "$SCRIPT_DIR" --json \
            > "${STAGING_DIR}/expected-images.json" 2>/dev/null; then
        EXP_COUNT=$(python3 -c "import json;print(json.load(open('${STAGING_DIR}/expected-images.json'))['count'])" 2>/dev/null || echo '?')
        print_substep "  Expected image set: ${EXP_COUNT} images (all profiles)."
    else
        print_warning "  Could not enumerate the expected image set (docker/compose?)."
    fi

    # ---- Build custom images across ALL profiles ---------------------------
    print_substep "Building custom images (all profiles)..."
    # #184 WS2a: strip the no-build overlay from COMPOSE_FILE for the package
    # build — otherwise `build:` is neutralised and the package would ship no
    # custom images. This is the explicit install/package build path the overlay
    # is designed to leave intact.
    _BUILD_CF="$(compose_file_for_build)"
    if [ -n "$ALL_PROFILES" ]; then
        COMPOSE_FILE="$_BUILD_CF" COMPOSE_PROFILES="$ALL_PROFILES" docker compose build --parallel 2>&1 \
            || print_warning "Some builds failed (all-profiles pass)."
    fi
    for _p in $LLM_PROFILES; do
        COMPOSE_FILE="$_BUILD_CF" COMPOSE_PROFILES="$_p" docker compose build --parallel 2>&1 \
            || print_warning "Some builds failed (llm profile: ${_p})."
    done

    # ---- Pull remote images across ALL profiles ----------------------------
    print_substep "Pulling remote images (all profiles)..."
    if [ -n "$ALL_PROFILES" ]; then
        COMPOSE_PROFILES="$ALL_PROFILES" docker compose pull --ignore-pull-failures 2>&1 || true
    fi
    for _p in $LLM_PROFILES; do
        COMPOSE_PROFILES="$_p" docker compose pull --ignore-pull-failures 2>&1 || true
    done
    # Runtime-only images (openhands runtime sidecar, gpustack rocm-runner) are
    # NOT compose services — pull them explicitly.
    print_substep "Pulling runtime-only images..."
    python3 -c "import sys; sys.path.insert(0, '${SCRIPT_DIR}/core/config/app/services'); import expected_images as ei; print('\n'.join(sorted(ei.runtime_only_images('${SCRIPT_DIR}'))))" 2>/dev/null | \
    while IFS= read -r rt_img; do
        [ -z "$rt_img" ] && continue
        docker pull "$rt_img" 2>&1 | sed 's/^/    /' || print_warning "    Failed to pull runtime-only: ${rt_img}"
    done

    # ---- Save the full expected set ---------------------------------------
    print_substep "Exporting images..."
    image_list=$(python3 "$EXP_ENUM" --stack-root "$SCRIPT_DIR" --list 2>/dev/null | sort -u)

    # Free-space preflight (#184): refuse to start saving if the staging
    # filesystem can't hold the image set — otherwise a small/RAM-backed staging
    # dir silently truncates the package to whatever fit before ENOSPC, which
    # reads downstream as "all images bundled" when they were not. Summing
    # per-image sizes over-counts shared layers (each tar carries its own copy),
    # so it's a safe upper bound.
    est_kb=$(printf '%s\n' "$image_list" | while IFS= read -r _im; do
        [ -z "$_im" ] && continue
        docker image inspect "$_im" --format '{{.Size}}' 2>/dev/null
    done | awk '{s+=$1} END{printf "%d", s/1024}')
    avail_kb=$(df -Pk "$STAGING_DIR" | awk 'NR==2{print $4}')
    need_kb=$(( ${est_kb:-0} + ${est_kb:-0}/10 ))   # +10% headroom
    if [ "${est_kb:-0}" -gt 0 ] && [ "${avail_kb:-0}" -lt "$need_kb" ]; then
        print_error "Staging dir ${STAGING_DIR} has $((avail_kb/1024/1024))GB free but the image set needs ~$((need_kb/1024/1024))GB. Set RAZZFAZZ_PACKAGE_STAGING to a larger disk and re-run."
        exit 1
    fi
    print_substep "  Image set ~$(( ${est_kb:-0}/1024/1024 ))GB; staging has $(( ${avail_kb:-0}/1024/1024 ))GB free — OK."

    img_count=0
    missing_count=0
    while IFS= read -r image; do
        [ -z "$image" ] && continue
        safe_name=$(echo "$image" | tr '/:' '_')
        if docker save "$image" -o "${STAGING_DIR}/images/${safe_name}.tar" 2>/dev/null; then
            print_substep "  Saved: ${image}"
            img_count=$((img_count + 1))
        else
            print_warning "  Not present locally, skipped: ${image}"
            missing_count=$((missing_count + 1))
        fi
    done <<< "$image_list"

    # Return to previous ref
    git checkout "$current_ref" --quiet 2>/dev/null || true

    if [ "$missing_count" -gt 0 ]; then
        print_warning "Saved ${img_count} images; ${missing_count} expected image(s) were NOT present locally (build/pull failures?). The offline package is INCOMPLETE — inspect the warnings above and re-run on a box where every image builds/pulls."
    else
        print_success "Saved ${img_count} Docker images (complete all-profiles set)."
    fi
fi

# ==============================================================================
# Create Manifest
# ==============================================================================
print_step "Creating package manifest..."

# Exclude images/ and models/ (the multi-GB blobs) from the line-by-line
# checksum manifest — hashing tens of GB doubles the packaging IO for little
# benefit; expected-images.json / expected-models.json are the completeness
# check for those. Mirrors the images-path precedent.
(cd "$STAGING_DIR" && find . -type f -not -path './images/*' -not -path './models/*' -exec sha256sum {} \; | \
    sed 's|\./||' | sort > MANIFEST.sha256)

manifest_count=$(wc -l < "${STAGING_DIR}/MANIFEST.sha256")
print_substep "Manifest: ${manifest_count} files checksummed."

# Package metadata
cat > "${STAGING_DIR}/PACKAGE_INFO" <<EOF
package_version=${VERSION}
package_tag=${TAG}
package_date=$(date -Iseconds)
package_commit=$(git rev-parse "$TAG" 2>/dev/null || echo "unknown")
include_images=${INCLUDE_IMAGES}
include_models=${INCLUDE_MODELS}
created_by=$(whoami)@$(hostname)
EOF

print_success "Manifest created."

# ==============================================================================
# Create Archive
# ==============================================================================
print_step "Creating upgrade package..."

ARCHIVE_PATH="${OUTPUT_DIR}/${PACKAGE_NAME}.tar.gz"

tar czf "$ARCHIVE_PATH" -C "$STAGING_DIR" .

archive_size=$(du -sh "$ARCHIVE_PATH" | cut -f1)

print_success "Package created: ${ARCHIVE_PATH} (${archive_size})"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Package ready: ${PACKAGE_NAME}.tar.gz${NC}"
echo -e "${GREEN}║   Size: ${archive_size}${NC}"
echo -e "${GREEN}║                                                      ║${NC}"
echo -e "${GREEN}║   Transfer to production box and run:                ║${NC}"
echo -e "${GREEN}║   rzfz upgrade --package ${PACKAGE_NAME}.tar.gz${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
