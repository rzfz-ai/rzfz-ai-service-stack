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
    echo "  --allow-partial-runners      Write the package even though a llama.cpp runner"
    echo "                               this host COULD have built is missing (#2154)."
    echo "                               Absent cross-architecture targets never block."
    echo "  --community                  Base/community package — do NOT bundle the"
    echo "                               Enterprise overlay (default: overlay included)"
    echo "  --sign-key PATH              (#781) Sign the finished archive with this"
    echo "                               private key — an openssl detached signature"
    echo "                               written BESIDE the package. The key comes from"
    echo "                               the SEQIS secrets vault; none is generated here."
    echo "                               Omit it and the package is unsigned, exactly as"
    echo "                               before (the out-of-band SHA-256 still applies)."
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
    echo "  $0 v1.1.0 --sign-key ~/vault/razzfazz-packages.key  # Signed package (#781)"
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
# #2154: refuse to write a package this host could have made complete.
ALLOW_PARTIAL_RUNNERS=false
OUTPUT_DIR="$SCRIPT_DIR"
# #781: signing is OPTIONAL and off by default. An empty SIGN_KEY reproduces
# the pre-#781 behaviour byte for byte — unsigned archive plus the out-of-band
# SHA-256 — because the fleet still receives packages built without a key, and
# a producer that silently changed shape would strand them.
SIGN_KEY=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --include-images) INCLUDE_IMAGES=true; shift ;;
        --include-models) INCLUDE_MODELS=true; shift ;;
        --models-from)    MODELS_FROM="$2"; INCLUDE_MODELS=true; shift 2 ;;
        --allow-partial-runners) ALLOW_PARTIAL_RUNNERS=true; shift ;;
        --community)      INCLUDE_OVERLAY=false; shift ;;
        --sign-key)       SIGN_KEY="$2"; shift 2 ;;
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

# #781: fail on a bad --sign-key HERE, before the multi-GB build. Discovering a
# missing vault key after `docker save` has run is a 20-minute mistake, and the
# tempting recovery ("ship it unsigned, sign it later") is the one that quietly
# puts an unsigned package on a stick.
if [ -n "$SIGN_KEY" ]; then
    if [ ! -r "$SIGN_KEY" ]; then
        print_error "--sign-key: no readable key at '${SIGN_KEY}'."
        print_substep "The package signing key lives in the SEQIS secrets vault (#781)."
        print_substep "None is generated here, and none belongs in this repository."
        exit 1
    fi
    if ! command -v openssl &>/dev/null; then
        print_error "openssl is required for --sign-key (#781)."
        exit 1
    fi
fi

if [ "$INCLUDE_IMAGES" = true ] && ! command -v docker &>/dev/null; then
    print_error "Docker is required for --include-images."
    exit 1
fi

if [ "$INCLUDE_MODELS" = true ] && ! command -v docker &>/dev/null; then
    print_error "Docker is required for --include-models (GGUFs are read from the LLM Manager worker volume and/or the gpustack-data volume)."
    exit 1
fi

# ---------------------------------------------------------------------------
# #2225: ONE helper image for every volume this script reads.
#
# The reads used to name a bare `alpine`, which Docker resolves to
# `alpine:latest` — a tag this stack ships nowhere. `core/compose.yml` runs
# `alpine:3.24` as a real service, so that tag is on every installed box and
# inside every `--include-images` package; measured on 0.91, a box installed
# for weeks, the bare name PULLED while `alpine:3.24` sat there.
#
# The pull is the small half. `docker run` resolves the image BEFORE it creates
# the container, so with no registry reachable the run dies without ever
# looking at the volume — and the model site swallowed stderr, so an empty
# `sz` became "Not cached locally, skipped" for every model in turn. A build
# box without WAN therefore produced a package with ZERO GGUFs and blamed the
# model volumes. `cli/upgrade.sh:7548` already wrote this lesson down after it
# blamed the wrong thing once ("a missing helper makes `docker run` fail BEFORE
# it ever looks at the volume"); this is that lesson applied here.
PACKAGE_HELPER_IMAGE="${RZFZ_PACKAGE_HELPER_IMAGE:-alpine:3.24}"

# Abort by NAME rather than walk a loop that can only report false absences.
# Called once, before the first read — never inside the per-model loop, where
# it would be a per-model failure that still looks like a missing model.
require_package_helper_image() {
    docker image inspect "$PACKAGE_HELPER_IMAGE" >/dev/null 2>&1 && return 0
    print_error "Helper image ${PACKAGE_HELPER_IMAGE} is not present on this box, so the model volumes cannot be read (#2225)."
    print_substep "  Every model would be reported as \"not cached locally\" and the package would ship with NO GGUFs."
    print_substep "  Pull it (docker pull ${PACKAGE_HELPER_IMAGE}), build on a box that runs the stack, or set RZFZ_PACKAGE_HELPER_IMAGE to an image that is present."
    exit 1
}

mkdir -p "$OUTPUT_DIR"

# Version from tag (strip v prefix)
VERSION="${TAG#v}"
PACKAGE_NAME="razzfazz-${VERSION}"
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

    # #2225: before the first volume read, not inside the loop.
    require_package_helper_image

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
            # helper copy script: find the model's GGUF(s) under local-models/ or
            # the HF cache in the volume, copy to /out preserving any sub-dir,
            # print total bytes. repo/filename are $1/$2 (never interpolated).
            copy_script='
set -e
umask 022
repo="$1"; filename="$2"
sdir=$(dirname "$filename"); sbase=$(basename "$filename")
[ "$sdir" = "." ] && sdir=""
total=0
# #2233: is the declared name a WILDCARD? `nomic-embed-text` carries
# `*f16*.gguf` because GPUStack stored the glob rather than the resolved name
# (standard-models.yaml, and #1786 chose to harden the readers rather than the
# manifest). A glob is attributable on a repo-scoped tree and NOT on the flat
# manager volume, where it matched the granite-docling vision projector —
# whose filename belongs to the manifest and nowhere else (#1256) — and
# bundled it, silently, as the embedding model.
is_glob=0
case "$sbase" in *"*"*|*"?"*|*"["*) is_glob=1 ;; esac
# #1544: /manager is the LLM Manager worker'"'"'s models volume, mounted when the box
# has one. It is FLAT — the node addresses a weight by its bare basename
# (hf_pull.ensure_file) — so it is searched with $sbase and no sub-dir. It comes
# FIRST because on a 2026.09 box that is where the standard set actually is; a
# GPUStack volume may still exist alongside it (adopt-models COPIES).
for base in "/manager" "/data/local-models" "/data/cache/huggingface/$repo"; do
  if [ "$base" = "/manager" ]; then
    # #2233: the flat volume has no repo namespace, so a wildcard match here
    # cannot be attributed to the declared repository. Treat it as unresolved —
    # the same verdict the LLM Manager reaches on the same manifest field
    # (#1786) — and tell the caller, so it is never silent.
    if [ "$is_glob" = 1 ]; then
      echo "GLOB_ON_FLAT $repo $sbase" >&2
      continue
    fi
    srcdir="$base"
  else
    srcdir="$base"; [ -n "$sdir" ] && srcdir="$base/$sdir"
  fi
  [ -d "$srcdir" ] || continue
  for f in "$srcdir"/$sbase; do
    [ -e "$f" ] || continue
    destdir="/out"
    # The package is written in MANAGER form (operator decision, #1544): flat,
    # the shape the node reads. A manifest sub-dir is preserved only for a
    # weight that came out of a GPUStack tree, where it is part of the name.
    [ "$base" != "/manager" ] && [ -n "$sdir" ] && destdir="/out/$sdir"
    mkdir -p "$destdir"
    cp "$f" "$destdir/$(basename "$f")"
    total=$((total + $(wc -c < "$f")))
  done
  [ "$total" -gt 0 ] && break
done
# #2241: this helper runs as ROOT, so everything it copied into the staging
# mount is root-owned — and on 0.91 mode 0600, which the invoking user cannot
# even read. `tar` and the cleanup trap both run as that user, so the models
# were counted as bundled and then silently omitted from the package.
#
# The hand-off happens HERE rather than via `docker run --user`, because
# --user requires the SOURCE volumes to be readable by that uid, which is a
# per-volume property of the box that nobody has measured. Running as root and
# handing over what we wrote works in both cases.
#
# Not on stdout: the caller reads the byte total with `tail -n1`.
if [ -n "${RZFZ_UID:-}" ]; then
  chown -R "${RZFZ_UID}:${RZFZ_GID:-$RZFZ_UID}" /out 2>/dev/null || true
fi
echo "$total"
'
            # Resolve the REAL gpustack-data volume. AUTHORITATIVE source: the
            # volume the running gpustack container actually mounts at
            # /var/lib/gpustack. compose prefixes it with the project name
            # (razzfazz-stack_gpustack-data); a BARE "gpustack-data" would mount
            # an empty, auto-created volume and bundle nothing (and a stray bare
            # volume can shadow the real one under a plain grep) — #184 P1 review.
            # #2230: `|| true`. This lookup is EXPECTED to fail on a 2026.09 box —
            # since #1443/C3 the Manager trio is the default and no container is
            # called `gpustack`. Bare, under this script's `set -eo pipefail`
            # (:19), the assignment's non-zero status ended the run with no error
            # text at all, and the EXIT trap removed the staging tree on the way
            # out. The three fallbacks below — one of which resolves
            # `razzfazz-stack_gpustack-data` correctly on exactly those boxes —
            # were one line too late to ever run.
            GPUSTACK_VOL="$(docker inspect gpustack --format '{{range .Mounts}}{{if eq .Destination "/var/lib/gpustack"}}{{.Name}}{{end}}{{end}}' 2>/dev/null || true)"
            [ -z "$GPUSTACK_VOL" ] && GPUSTACK_VOL="$(docker volume ls -q 2>/dev/null | grep -E '_gpustack-data$' | head -1)"
            [ -z "$GPUSTACK_VOL" ] && GPUSTACK_VOL="$(docker volume ls -q 2>/dev/null | grep -E '(^|_)gpustack-data$' | head -1)"
            [ -z "$GPUSTACK_VOL" ] && GPUSTACK_VOL="gpustack-data"
            # #1544: and the LLM Manager's worker volume, which is where the
            # standard set lives on a 2026.09 box. Without this, `rzfz package
            # --include-models` on a Manager-shaped box bundled NOTHING and said
            # only "not cached locally" per model — an offline package that
            # silently contains no models is the worst shape this command has.
            # Same variable and default as modules/llm/node-agent/compose.thin.yml.
            MANAGER_MODELS_VOL="${LLM_WORKER_MODELS_VOLUME:-razzfazz-stack_llm-node-models}"
            if ! docker volume inspect "$MANAGER_MODELS_VOL" >/dev/null 2>&1; then
                # #1544 review: NOT silently. Dropping the manager volume from
                # the mount list without a word puts the run back in exactly the
                # state this change removes — "not cached locally" per model and
                # a package with no weights — one box shape further along. The
                # project prefix is what distinguishes the candidates
                # (`razzfazz-stack_…` on a master, `rzfz-node_…` on a thin
                # node), and a box can carry several, so print them.
                MANAGER_MODELS_VOL=""
                # #2230: `|| true` — grep exits 1 when nothing matches, and under
                # `pipefail` that ends the run. This line is inside the branch that
                # exists to TELL the operator the volume is missing; bare, it killed
                # the build instead of printing the message.
                _mm_found="$(docker volume ls -q 2>/dev/null | grep -E 'llm-node-models$' | tr '\n' ' ' || true)"
                print_warning "  The LLM Manager models volume '${LLM_WORKER_MODELS_VOLUME:-razzfazz-stack_llm-node-models}' does not exist on this box — its weights will NOT be bundled."
                if [ -n "$_mm_found" ]; then
                    print_info "  Candidates present: ${_mm_found}"
                    print_info "  Set LLM_WORKER_MODELS_VOLUME=<name> in .env (or in the environment) and re-run."
                else
                    print_info "  No *llm-node-models volume on this box at all — if the models live in gpustack-data only, this is expected."
                fi
            fi
            print_substep "  Reading GGUFs from volume: ${GPUSTACK_VOL}${MANAGER_MODELS_VOL:+ + ${MANAGER_MODELS_VOL}}"
            model_count=0
            model_missing=0
            models_total_bytes=0
            while IFS=$'\t' read -r m_alias m_repo m_filename; do
                [ -z "$m_alias" ] && continue
                # #2233: stderr is captured, not discarded, so the flat-volume
                # wildcard refusal reaches the operator — the same shape the
                # mmproj site below already uses for its own flat-volume note.
                _mdl_err="${TMPDIR:-/tmp}/rzfz-model-$$.err"
                sz=$(docker run --rm -v "$GPUSTACK_VOL":/data:ro \
                        ${MANAGER_MODELS_VOL:+-v "$MANAGER_MODELS_VOL":/manager:ro} \
                        -v "${STAGING_DIR}/models":/out \
                        -e RZFZ_UID="$(id -u)" -e RZFZ_GID="$(id -g)" "$PACKAGE_HELPER_IMAGE" \
                        sh -c "$copy_script" _ "$m_repo" "$m_filename" 2>"$_mdl_err" | tail -n1)
                if grep -q "^GLOB_ON_FLAT " "$_mdl_err" 2>/dev/null; then
                    print_warning "  ${m_alias}: the manifest declares a WILDCARD weight name (${m_filename}), which cannot be attributed to a repository on the flat manager volume — not taken from there (#2233). It is bundled only from a repo-scoped cache."
                fi
                [ -s "$_mdl_err" ] && grep -v "^GLOB_ON_FLAT " "$_mdl_err" >&2 || true
                rm -f "$_mdl_err"
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

            # ---- #138: vision mmproj sidecars ------------------------------
            # A vision model's mmproj companion GGUF is NOT its
            # huggingface_filename, so the loop above never copies it — and an
            # offline box can't fetch it later (broken vision was the ga.8
            # symptom). Bundle each sidecar under models/<repo>/<file>:
            # repo-scoped because upstream sidecar names repeat across repos
            # (the mmproj-*.gguf convention). Offline registration points
            # --mmproj at local-models/<repo>/<file> (model_source.apply_mmproj).
            mmproj_copy_script='
set -e
repo="$1"; file="$2"
data="${RZFZ_DATA:-/data}"; out="${RZFZ_OUT:-/out}"
total=0
# #1544: the Manager worker volume is flat, so the sidecar is there under its
# bare name. Searched first for the same reason as the weights above. The
# DESTINATION stays repo-scoped either way: upstream sidecar names repeat
# across repos (the mmproj-*.gguf convention), and offline registration points
# --mmproj at local-models/<repo>/<file> (model_source.apply_mmproj).
# #1544 review, note 2: the repo-scoped paths come FIRST for the sidecars, the
# opposite of the weights above, and on purpose. The manager volume is flat, and
# upstream sidecar names repeat across repos (they follow one naming convention) — so
# a flat projector file there may belong to a DIFFERENT repo than the one being
# packed, and the destination here is repo-scoped, which would hide the mistake
# until load time. The repo-scoped source is unambiguous by construction; the
# flat one is the fallback, and it says so in the log when it wins.
for src in "$data/cache/huggingface/$repo/$file" "$data/local-models/$repo/$file" "/manager/$file"; do
  [ -f "$src" ] || continue
  mkdir -p "$out/$repo"
  cp "$src" "$out/$repo/$file"
  total=$(wc -c < "$src")
  case "$src" in
    /manager/*) echo "MMPROJ_FLAT_SOURCE $repo $file" >&2 ;;
  esac
  break
done
# #2241: this helper runs as ROOT, so everything it copied into the staging
# mount is root-owned — and on 0.91 mode 0600, which the invoking user cannot
# even read. `tar` and the cleanup trap both run as that user, so the models
# were counted as bundled and then silently omitted from the package.
#
# The hand-off happens HERE rather than via `docker run --user`, because
# --user requires the SOURCE volumes to be readable by that uid, which is a
# per-volume property of the box that nobody has measured. Running as root and
# handing over what we wrote works in both cases.
#
# Not on stdout: the caller reads the byte total with `tail -n1`.
if [ -n "${RZFZ_UID:-}" ]; then
  chown -R "${RZFZ_UID}:${RZFZ_GID:-$RZFZ_UID}" /out 2>/dev/null || true
fi
echo "$total"
'
            mmproj_rows=$(python3 "$EXP_MODELS" --stack-root "$SCRIPT_DIR" \
                --config "$TARGET_MODELS_YAML" --all-profiles --list-mmproj \
                "${DELTA_ARG[@]}" 2>/dev/null || true)
            while IFS=$'\t' read -r m_alias m_repo m_file; do
                [ -z "$m_alias" ] && continue
                # #1544 review, note 2: the copy script says on stderr when it
                # had to fall back to the FLAT manager volume, where a sidecar
                # name is ambiguous across repos. Swallowing stderr here would
                # make that note unreachable — which is the same silence this
                # PR removes elsewhere.
                _mmproj_err="${TMPDIR:-/tmp}/rzfz-mmproj-$$.err"
                sz=$(docker run --rm -v "$GPUSTACK_VOL":/data:ro \
                        ${MANAGER_MODELS_VOL:+-v "$MANAGER_MODELS_VOL":/manager:ro} \
                        -v "${STAGING_DIR}/models":/out \
                        -e RZFZ_UID="$(id -u)" -e RZFZ_GID="$(id -g)" "$PACKAGE_HELPER_IMAGE" \
                        sh -c "$mmproj_copy_script" _ "$m_repo" "$m_file" 2>"$_mmproj_err" | tail -n1)
                if grep -q "^MMPROJ_FLAT_SOURCE " "$_mmproj_err" 2>/dev/null; then
                    print_warning "  Vision sidecar ${m_file} taken from the FLAT manager volume — that name is not repo-scoped there, so verify it belongs to ${m_repo} before shipping this package (#1544)."
                fi
                rm -f "$_mmproj_err"
                sz=${sz:-0}
                case "$sz" in ''|*[!0-9]*) sz=0 ;; esac
                if [ "$sz" -gt 0 ]; then
                    printf '%s\t%s\n' "${m_repo}/${m_file}" "$sz" >> "$SIZES_TSV"
                    models_total_bytes=$((models_total_bytes + sz))
                    print_substep "  Bundled vision sidecar: ${m_alias} ($(( sz / 1024 / 1024 ))MB) — ${m_repo}/${m_file}"
                else
                    print_warning "  Vision sidecar not cached locally, skipped: ${m_alias} (${m_repo}/${m_file}) — vision for this model will NOT work offline."
                    model_missing=$((model_missing + 1))
                fi
            done <<< "$mmproj_rows"

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
# #138: vision sidecars, keyed by alias — recorded per model so verify/debug
# on the target box can tell "model bundled but its mmproj wasn't" apart.
mmproj = {alias: f"{repo}/{fn}"
          for alias, repo, fn in em.iter_mmproj(spec, all_profiles=True)}
models = []
for alias, repo, filename, roles in em.iter_models(spec, all_profiles=True):
    entry = {"name": alias, "repo": repo, "filename": filename,
             "roles": roles, "size": int(sizes.get(filename, 0))}
    if alias in mmproj:
        entry["mmproj"] = {"filename": mmproj[alias],
                           "size": int(sizes.get(mmproj[alias], 0))}
    models.append(entry)
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
            # #759: a declared model that cannot be resolved USED TO BE one
            # warning line in a build log thousands of lines long, and the
            # package was written anyway. That is how the 2026.08-ga.15 stick
            # shipped 78 GB without qwen3.6 — its main chat model — while
            # carrying six models nobody asked for. Nothing downstream notices:
            # the package is large, the install succeeds, and the gap surfaces
            # at the first chat attempt, offline, at the customer.
            #
            # So: fatal. The escape hatch follows the repo's existing
            # convention (RAZZFAZZ_ALLOW_* / RAZZFAZZ_SKIP_*) and is logged, for
            # the legitimate case of deliberately packaging a narrower set on a
            # box that does not hold every GGUF.
            if [ "$model_missing" -gt 0 ]; then
                print_warning "Bundled ${model_count} of $((model_count + model_missing)) declared model GGUFs (~${models_total_gb}GB); ${model_missing} could NOT be resolved in the local cache."
                if [ "${RAZZFAZZ_ALLOW_INCOMPLETE_PACKAGE:-0}" = "1" ]; then
                    print_warning "RAZZFAZZ_ALLOW_INCOMPLETE_PACKAGE=1 — continuing with an INCOMPLETE model set (operator override, #759)."
                else
                    print_error "Refusing to write an offline package whose declared models are missing (#759)."
                    print_error "Either download them on this box (rzfz post-install), narrow the set via --models-from,"
                    print_error "or re-run with RAZZFAZZ_ALLOW_INCOMPLETE_PACKAGE=1 if the gap is intended."
                    exit 1
                fi
                print_warning "The offline model set is INCOMPLETE — download the rest on the dev box (rzfz post-install) and re-run."
            else
                print_warning "Bundled ${model_count} model GGUFs — total ~${models_total_gb}GB. This makes the package VERY large; ensure the transfer medium + target disk have room."
                print_success "Model GGUFs bundled (complete set for this package)."
            fi
        fi
    fi
fi

# ==============================================================================
# #2272: build plugins/dify-wheelhouse/ from the staged .difypkg files, inside the
# daemon's own image (python 3.12 + pip 24 + uv 0.12.2; `uv pip download` does not
# exist, so `uv pip compile` per plugin → `python3 -m pip download`), with the
# packaging box's network; then verify offline resolution per plugin with uv
# itself. Two failure texts, and they mean different things (measured on 0.175):
#   "X was not found in the cache"                      -> index identity mismatch
#   "X==N needs to be downloaded from a registry"       -> that pin is missing
# Ownership handed to the invoking user (#2241). Returns 1 on any plugin that
# cannot resolve from the wheelhouse alone.
_difypkg_requirements() {
    if command -v unzip >/dev/null 2>&1; then
        unzip -p "$1" requirements.txt
    else
        python3 -c 'import sys,zipfile; sys.stdout.write(zipfile.ZipFile(sys.argv[1]).read("requirements.txt").decode())' "$1"
    fi
}

build_dify_wheelhouse() {
    local pkg_dir="$1" wh_dir="$2" reqs img rc=0 n=0 f name
    img="${DIFY_PLUGIN_DAEMON_IMAGE:-}"
    if [ -z "$img" ]; then
        local ver; ver="${DIFY_PLUGIN_VERSION:-$(read_env_value "${SCRIPT_DIR}/.env" DIFY_PLUGIN_VERSION 2>/dev/null || true)}"
        img="langgenius/dify-plugin-daemon:${ver:-0.6.10-local}"
    fi
    if ! docker image inspect "$img" >/dev/null 2>&1; then
        print_error "  The plugin daemon image ${img} is not present on this box — the wheelhouse is built inside it (#2272)."
        return 1
    fi
    reqs="$(mktemp -d "${TMPDIR:-/tmp}/rzfz-wh-reqs.XXXXXX")"
    # The daemon stores a plugin package as <vendor>/<name>:<version>@<sha256> with
    # NO extension; a side-loaded package is <name>.difypkg. A name filter is a
    # guess about a private on-disk convention — the zip magic (PK\003\004) is the
    # fact, so keep every regular file that is a zip, at any of the three depths.
    local bundled seen=0
    bundled="$(find "$pkg_dir" -type f 2>/dev/null | wc -l | tr -d ' ')"
    for f in "$pkg_dir"/* "$pkg_dir"/*/* "$pkg_dir"/*/*/*; do
        [ -f "$f" ] || continue
        [ "$(LC_ALL=C head -c 4 "$f" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "504b0304" ] || continue
        seen=$((seen + 1))
        name="${f#"$pkg_dir"/}"; name="${name%.difypkg}"; name="$(printf '%s' "$name" | tr '/:@' '___')"
        # requirements.txt at the zip root; unzip where the box has it, python's
        # zipfile where it does not (the sandbox), same result
        if ! _difypkg_requirements "$f" > "${reqs}/${name}.txt" 2>/dev/null || [ ! -s "${reqs}/${name}.txt" ]; then
            print_warning "  ${name}: no requirements.txt at the package root — nothing to resolve for it (#2272)."
            rm -f "${reqs}/${name}.txt"; continue
        fi
        n=$((n + 1))
    done
    if [ "$seen" -eq 0 ]; then
        rm -rf "$reqs"
        if [ "${bundled:-0}" -gt 0 ]; then
            # "no plugins" and "I could not see the plugins that are there" are
            # different outcomes; the second is a build failure by name.
            print_error "  ${bundled} file(s) bundled under plugins/dify but none is a zip — the packager cannot see the plugins that are there; NOT publishing (#2272)."
            find "$pkg_dir" -type f | head -5 | sed "s|^${pkg_dir}/|    |"
            return 1
        fi
        print_substep "  No plugin packages bundled — no wheelhouse to build (#2272)."
        return 0
    fi
    if [ "$n" -eq 0 ]; then
        # every plugin was seen, none declares Python requirements: record that
        # as a file so the verifier's wheelhouse row counts something real.
        mkdir -p "$wh_dir"
        { echo "# ${seen} plugin package(s) seen, none carries a requirements.txt (#2272)"; find "$pkg_dir" -type f | sed "s|^${pkg_dir}/||"; } > "${wh_dir}/NO-REQUIREMENTS"
        print_substep "  ${seen} plugin package(s) seen, none declares Python requirements — NO-REQUIREMENTS marker written (#2272)."
        rm -rf "$reqs"; return 0
    fi
    mkdir -p "$wh_dir"
    print_substep "  Building the Dify plugin wheelhouse for ${n} of ${seen} plugin package(s) inside ${img} (#2272)..."
    docker run --rm --network host \
        -v "${wh_dir}":/wh -v "${reqs}":/req:ro \
        -e RZFZ_UID="$(id -u)" -e RZFZ_GID="$(id -g)" \
        --entrypoint sh "$img" -c '
set -e
for r in /req/*.txt; do
  n=$(basename "$r" .txt)
  uv pip compile -q --python-version 3.12 "$r" -o "/tmp/${n}.lock" || { echo "COMPILE-FAILED $n"; exit 3; }
  python3 -m pip download -q -d /wh -r "/tmp/${n}.lock" || { echo "DOWNLOAD-FAILED $n"; exit 4; }
done
uv venv -q /tmp/vfy --python 3.12
for r in /req/*.txt; do
  n=$(basename "$r" .txt)
  if uv pip install --python /tmp/vfy/bin/python --dry-run --offline --no-index --find-links /wh -r "$r" >/tmp/vfy.log 2>&1; then
    echo "WHEELHOUSE-OK $n"
  else
    echo "WHEELHOUSE-UNRESOLVED $n"; tail -n 4 /tmp/vfy.log; exit 5
  fi
done
[ -n "${RZFZ_UID:-}" ] && chown -R "${RZFZ_UID}:${RZFZ_GID:-$RZFZ_UID}" /wh 2>/dev/null || true
' 2>&1 | sed 's/^/    /'
    rc=${PIPESTATUS[0]}
    rm -rf "$reqs"
    if [ "$rc" -ne 0 ]; then
        print_error "  The Dify plugin wheelhouse did not verify (exit ${rc}) — NOT publishing a package whose plugins cannot resolve offline (#2272)."
        return 1
    fi
    print_substep "  Wheelhouse: $(find "$wh_dir" -type f -name '*.whl' | wc -l | tr -d ' ') wheel(s), $(du -sh "$wh_dir" 2>/dev/null | cut -f1) — every plugin resolves from it offline (#2272)."
    return 0
}

# Include the Dify plugin packages (#2222) — 1.5 MB that decides whether an
# air-gapped box can serve models at all
# ==============================================================================
# Journey B on 0.175 (2026-09-16): a fresh offline install from an images
# package came up healthy, every belt armed, expected-image gate 91/91 — and
# Dify had no model provider (`provider_models` 0, post-install exit 1).
# Plugins come from the marketplace, which an air-gapped box cannot reach, and
# no command line could put them into a package. The plugin daemon's package
# cache on the building box holds exactly the installed set as files whose PATH
# IS the identifier (<vendor>/<name>:<version>@<checksum>) and agrees with
# `plugin_declarations` — measured on 0.91 — so the packager copies that tree
# verbatim into plugins/dify/ and post-install installs by identifier, with no
# mapping table. Bundled whenever docker can see the daemon's volume; a box
# without one gets a warning, not a silent omission.
if command -v docker >/dev/null 2>&1; then
    print_step "Bundling Dify plugin packages (offline package, #2222)..."
    # `|| true`: no match is a fact, not an error (grep's 1 under pipefail would
    # abort the whole build here, as #2139's regression class did in init).
    PLUGIN_VOL="$(docker volume ls -q 2>/dev/null | grep -E '(^|_)dify-plugin-daemon$' | head -1 || true)"
    # #2225: the helper image is the one this stack SHIPS (alpine:3.24,
    # core/compose.yml), never a bare `alpine` — that means alpine:latest, which
    # no installed box carries, so `docker run` would try to PULL it, and on a
    # build box without WAN it dies before ever looking at the volume. Checked
    # by name first: a missing helper aborts the build here rather than
    # producing an empty plugins/ subtree that says the daemon had nothing.
    # #2225: the same resolved name as the model reads — one default, one place.
    PLUGIN_HELPER_IMAGE="$PACKAGE_HELPER_IMAGE"
    if [ -z "$PLUGIN_VOL" ]; then
        print_warning "  No dify-plugin-daemon volume on this box — the package ships WITHOUT Dify plugins; an air-gapped box will have no model provider."
    elif ! docker image inspect "$PLUGIN_HELPER_IMAGE" >/dev/null 2>&1; then
        print_error "  Helper image ${PLUGIN_HELPER_IMAGE} is not present on this box — cannot read the plugin daemon's volume (#2225). Pull it, or build on a box that runs the stack. Refusing to write a package whose plugins/ would be empty."
        exit 1
    else
        mkdir -p "${STAGING_DIR}/plugins/dify"
        PLUGIN_CACHE_SUB="${DIFY_PLUGIN_PACKAGE_CACHE_PATH:-plugin_packages}"
        # stderr is NOT redirected: a missing directory, a docker error and a tar
        # failure must each arrive with their own words, not as one silent false.
        if docker run --rm -v "${PLUGIN_VOL}":/src:ro "$PLUGIN_HELPER_IMAGE" sh -c "cd /src/${PLUGIN_CACHE_SUB} && tar cf - ." \
             | tar -C "${STAGING_DIR}/plugins/dify" -xf -; then
            PLUGIN_COUNT=$(find "${STAGING_DIR}/plugins/dify" -type f | wc -l | tr -d ' ')
            if [ "${PLUGIN_COUNT:-0}" -gt 0 ]; then
                print_substep "  Bundled ${PLUGIN_COUNT} Dify plugin package(s) from ${PLUGIN_VOL}/${PLUGIN_CACHE_SUB}:"
                find "${STAGING_DIR}/plugins/dify" -type f | sed "s|^${STAGING_DIR}/plugins/dify/|    |"
                # The daemon builds each plugin's Python environment AT INSTALL
                # TIME with uv, fetching wheels from PyPI unless its cache holds
                # them — measured on 0.175 under a real air gap: without the cache
                # the install task dies ("failed to init environment"), with the
                # cache alone it succeeds in 8 s. One cache serves every plugin
                # (keyed by wheel); ~13 MB compressed. Built by the shipped daemon
                # image on the building box, so it is architecture-specific: an
                # arm64 box needs a cache built on arm64.
                UV_CACHE_SUB="${DIFY_PLUGIN_UV_CACHE_PATH:-cwd/.uv-cache}"
                mkdir -p "${STAGING_DIR}/plugins/dify-uv-cache"
                if docker run --rm -v "${PLUGIN_VOL}":/src:ro "$PLUGIN_HELPER_IMAGE" sh -c "cd /src/${UV_CACHE_SUB} && tar cf - ." \
                     | tar -C "${STAGING_DIR}/plugins/dify-uv-cache" -xf - \
                   && [ -n "$(ls -A "${STAGING_DIR}/plugins/dify-uv-cache" 2>/dev/null)" ]; then
                    print_substep "  Bundled the plugin daemon's uv cache ($(du -sh "${STAGING_DIR}/plugins/dify-uv-cache" 2>/dev/null | cut -f1)) — an air-gapped install builds the plugins' Python environments from it (#2222)."
                else
                    rmdir "${STAGING_DIR}/plugins/dify-uv-cache" 2>/dev/null || true
                    print_warning "  ${PLUGIN_VOL}/${UV_CACHE_SUB} is absent or empty — the plugins ship WITHOUT their dependency cache; an air-gapped install of them will FAIL at 'failed to init environment' (#2222)."
                fi

                # #2272: the daemon's uv cache is NOT a complete offline source — uv files
                # index metadata per index identity (the daemon resolved against an
                # auto-detected mirror, the cache was warmed for PyPI), and the cache holds
                # only what earlier installs happened to resolve (a fully pinned plugin's
                # exact versions were absent). Journey B on 2026.09-rc11 measured 0 of 3
                # plugins under a real egress cut. A WHEELHOUSE is every plugin's full
                # dependency closure as wheels, built INSIDE the daemon's own image so they
                # match its interpreter, per plugin from that plugin's own requirements.txt
                # (the .difypkg root). The bound uv.toml (#2261) names it with no-index, so
                # resolution never consults an index. Verified before publishing with the
                # consumer itself; a plugin that does not resolve fails the build by name.
                build_dify_wheelhouse "${STAGING_DIR}/plugins/dify" "${STAGING_DIR}/plugins/dify-wheelhouse" || exit 1
            else
                print_warning "  The plugin package cache in ${PLUGIN_VOL} is empty — the package ships WITHOUT Dify plugins."
                rmdir "${STAGING_DIR}/plugins/dify" "${STAGING_DIR}/plugins" 2>/dev/null || true
            fi
        else
            print_warning "  Could not read ${PLUGIN_VOL}/${PLUGIN_CACHE_SUB} — the package ships WITHOUT Dify plugins."
            rm -rf "${STAGING_DIR}/plugins" 2>/dev/null || true
        fi
    fi
else
    print_info "docker not available — Dify plugin packages not bundled (#2222)."
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

    # #758: the enumerator FAILS OPEN. `expected_images.py` derives the set from
    # `docker compose config`, and `docker compose config` aborts as soon as a
    # file referenced by `env_file` is missing. `.env.dify` is NOT versioned, so
    # in every fresh clone of a tag it is absent — the enumeration then falls
    # back to the handful of images it knows without compose, and the loop below
    # measures "missing" against THAT. Nothing is missing from an empty
    # expectation, so the run ends with "complete all-profiles set".
    #
    # Measured on 0.78, same clone, three states:
    #     no .env, no .env.dify   ->  3 images
    #     .env only               ->  3 images
    #     .env AND .env.dify      -> 79 images
    #
    # Fail-open is right for the VERIFIER on a box (it reports "cannot
    # determine"). The PACKAGER must not inherit it: an offline package is the
    # tool for the boxes that get no second try.
    #
    # This sits BEFORE expected-images.json is written into the package. That
    # manifest IS the box's own expectation afterwards — with a broken
    # enumeration the package would ship `count: 3`, and `rzfz verify-images`
    # on the air-gapped box would then confirm 3 of 3 present and report GREEN.
    # A wrong expectation does not just mislead the builder, it disables the
    # check that exists to catch exactly this.
    if [ ! -f "${SCRIPT_DIR}/.env" ] || [ ! -f "${SCRIPT_DIR}/.env.dify" ]; then
        print_error "--include-images needs BOTH ${SCRIPT_DIR}/.env and .env.dify."
        print_error "Without them 'docker compose config' aborts, the image enumeration"
        print_error "falls back to a handful of non-compose images, and the package would"
        print_error "be built — and reported complete — with the stack missing (#758)."
        print_error "On a fresh clone: cp config/.env.example .env && cp config/.env.dify.example .env.dify"
        exit 1
    fi

    image_expected=$(python3 "$EXP_ENUM" --stack-root "$SCRIPT_DIR" --list 2>/dev/null | sort -u | grep -c . || true)
    # Plausibility floor. The env-file check above catches the known trigger;
    # this catches the next one, whatever it turns out to be. The all-profiles
    # set is ~79 images — any single-digit result means the enumeration broke,
    # not that the stack shrank.
    if [ "${image_expected:-0}" -lt 20 ]; then
        print_error "Image enumeration returned only ${image_expected} image(s) — the all-profiles set is ~79."
        print_error "That is a broken enumeration, not a small stack. Refusing to build a package"
        print_error "that would report itself complete (#758). Check: docker compose config --images"
        exit 1
    fi
    # All profiles EXCEPT the llm-runtime trio (which collide on container_name);
    # the trio is rendered one profile at a time below.
    ALL_PROFILES="$(python3 "$BUILD_PREFLIGHT" --stack-root "$SCRIPT_DIR" --build-profiles 2>/dev/null || true)"
    # #1448 merged llm-cuda into llm-legacy; #1447 removed `llm` (2.x) and
    # folded `llm-cpu` in. ONE runtime profile is rendered per hardware line —
    # the images differ by DEVICE OVERLAY now, which is why the loop below
    # renders it once per HARDWARE rather than once per profile.
    LLM_PROFILES="llm-legacy"

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
        COMPOSE_FILE="$_BUILD_CF" COMPOSE_PROFILES="$ALL_PROFILES" COMPOSE_PARALLEL_LIMIT="$(razzfazz_build_parallelism)" docker compose build --parallel 2>&1 \
            || print_warning "Some builds failed (all-profiles pass)."
    fi
    for _p in $LLM_PROFILES; do
        COMPOSE_FILE="$_BUILD_CF" COMPOSE_PROFILES="$_p" COMPOSE_PARALLEL_LIMIT="$(razzfazz_build_parallelism)" docker compose build --parallel 2>&1 \
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

    # ---- Mirror EVERY runner target, not just this host's (#1309) -----------
    # The block above already states the intent: the package must carry "all
    # LLM-runtime variants". It did not. `expected_images.py::engine_runner_images()`
    # answers "which runner does THIS box launch" and returns exactly one BY
    # DESIGN, and the packager was asking it. Measured on the AMD dev box,
    # 2026-09-08, by running it against a stand-in `.env` per hardware:
    #
    #     HARDWARE=amd     ->  llama-runner:b9851-vulkan
    #     HARDWARE=cpu     ->  llama-runner:b10853-cpu
    #     HARDWARE=nvidia  ->  llama-runner:b9851-cuda12.8-sm120
    #
    # while modules/llm/runners/runners.yaml defines SIX targets. So a package
    # built here carried one of six: an NVIDIA target box unpacking it found no
    # runner at all, and `pull_policy: never` means it never gets one. Even
    # within its own class it carried one of three (the b8943 rollback and the
    # ROCm target were both missing).
    #
    # Verification on the box is deliberately NOT widened, because it asks the
    # other question: `cli/verify-images.sh` execs the enumerator with
    # --stack-root and re-enumerates LOCALLY — it does not read this package's
    # manifest — so it keeps demanding the one image that box launches. The
    # appliance loader gate (cli/init.sh) compares loaded images against
    # expected-images.json and reports only what is MISSING, so extra runner
    # variants in the archive do not disturb it either.
    print_substep "Mirroring llama.cpp runner variants (every target, #1309)..."
    runner_carried=0
    runner_absent=""
    # #2154: absences split in two. A target this host could have BUILT and did
    # not is an operator mistake and fatal; a cross-architecture target is a fact
    # about the host and only warns. Without the split the gate would fire on
    # every amd64 build (the GB10 runner is arm64), the override would become
    # routine, and that is the old silence wearing a flag.
    runner_absent_here=""
    case "$(uname -m)" in
        x86_64|amd64)  pkg_host_arch="amd64" ;;
        aarch64|arm64) pkg_host_arch="arm64" ;;
        *)             pkg_host_arch="$(uname -m)" ;;
    esac
    while IFS=$'\t' read -r plan_kind plan_ref plan_hw plan_arch plan_build; do
        case "$plan_kind" in
            carry)
                if printf '%s\n' "$image_list" | grep -Fxq "$plan_ref"; then
                    continue
                fi
                image_list="${image_list}
${plan_ref}"
                runner_carried=$((runner_carried + 1))
                ;;
            absent)
                runner_absent="${runner_absent}${plan_ref}  (${plan_hw}, ${plan_arch})
        ${plan_build}
"
                if [ "$plan_arch" = "$pkg_host_arch" ]; then
                    runner_absent_here="${runner_absent_here}${plan_ref}  (${plan_hw})
"
                fi
                ;;
        esac
    done <<< "$(razzfazz_runner_package_plan "$SCRIPT_DIR")"
    print_substep "  Carrying ${runner_carried} runner image(s) (incl. legacy aliases present locally)."
    if [ -n "$runner_absent" ]; then
        # Naming these IS the fix. Dropping them silently is what made a package
        # built on an AMD host useless to an NVIDIA box without anyone noticing.
        print_warning "  This host cannot carry every runner target — NOT in the package:"
        printf '%s' "$runner_absent" | sed 's/^/      /'
        print_warning "  A box of that class will find NO runner in this archive (pull_policy: never)."
        print_warning "  Build the targets above and re-run, or ship one package per hardware class."
        print_warning "  The arm64 GB10 target cannot be built on an amd64 host at all — that one needs a GB10-class builder."
    fi
    # The expected count was taken before these were added; the closing
    # "Saved X of Y expected" line compares against it, and leaving Y alone would
    # print "Saved 85 of 79". Absent targets are NOT folded into missing_count:
    # only images present locally ever enter image_list, so the save loop's
    # INCOMPLETE verdict keeps meaning "a build or pull failed", not "this host
    # is the wrong architecture for a runner it was never going to have".
    image_expected=$(( ${image_expected:-0} + runner_carried ))
    # #2154: #1309 made the packager NAME what it cannot carry, which fixed the
    # silence — and it still exited 0 and wrote the archive. So the operator got
    # a package that looks complete (large, successful, signed) whose target box
    # finds no runner and, with `pull_policy: never`, never gets one; the gap
    # surfaces at the first model launch, offline, at the customer.
    #
    # That is word for word the argument #759 already settled for the model
    # GGUFs a few hundred lines above, so this takes the same shape: fatal, with
    # a logged escape hatch for the operator who means it.
    #
    # Placed after the accounting line on purpose: the block ABOVE is lifted and
    # executed by #1309's guard, which asserts the report's contents and a clean
    # exit. Keeping the decision outside that range leaves its four assertions
    # measuring exactly what they were written to measure.
    if [ -n "$runner_absent_here" ]; then
        if [ "${ALLOW_PARTIAL_RUNNERS:-false}" = "true" ]; then
            print_warning "  --allow-partial-runners — writing a package without runner target(s) this host could have built (operator override, #2154)."
        else
            print_error "Refusing to write an offline package missing runner target(s) this host CAN build (#2154):"
            printf '%s' "$runner_absent_here" | sed 's/^/      /'
            print_error "  Build them on this box and re-run, or pass --allow-partial-runners if the gap is intended."
            print_error "  (Cross-architecture targets are listed above but never block — this host cannot build them.)"
            exit 1
        fi
    fi
    # ---- end runner step (#2154) ----


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

    # #271/#2120: record the exact image id `docker save` wrote for every saved
    # ref into expected-images.json (`image_ids`). A box that already holds an
    # image under that ref with the SAME id holds this archive's content — init
    # and the offline upgrade then skip extracting and loading the images/
    # subtree entirely (155 GB and 8+ minutes measured on 0.175 for zero loaded
    # images). A tag alone cannot say that (#2006/#2105); the id can.
    print_substep "Recording image ids into expected-images.json (#271/#2120)..."
    python3 - "${STAGING_DIR}/expected-images.json" <<'PYEOF' || print_warning "  Could not record image ids — a box loading this package will extract and load every image (no skip)."
import json, subprocess, sys
path = sys.argv[1]
m = json.load(open(path, encoding="utf-8"))
ids = {}
for ref in m.get("images") or []:
    r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        ids[ref] = r.stdout.strip()
m["image_ids"] = ids
json.dump(m, open(path, "w", encoding="utf-8"), indent=2, sort_keys=True)
print(f"  image_ids: {len(ids)} of {len(m.get('images') or [])} refs")
PYEOF

    # Return to previous ref
    git checkout "$current_ref" --quiet 2>/dev/null || true

    # #758: say the NUMBERS, not a verdict. "complete all-profiles set" is a
    # claim about a set the reader cannot see; "79 of 79 expected" is a claim
    # they can check. The old wording was true against an expectation of three.
    if [ "$missing_count" -gt 0 ]; then
        print_warning "Saved ${img_count} of ${image_expected} expected images; ${missing_count} were NOT present locally (build/pull failures?). The offline package is INCOMPLETE — inspect the warnings above and re-run on a box where every image builds/pulls."
    else
        print_success "Saved ${img_count} of ${image_expected} expected Docker images (all-profiles set)."
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
# The redirect creates MANIFEST.sha256 BEFORE find runs, so without the
# exclusion the manifest hashes its own half-written self — and every
# offline upgrade then reports a spurious "checksums did not match".
(cd "$STAGING_DIR" && find . -type f -not -path './images/*' -not -path './models/*' \
    -not -name 'MANIFEST.sha256' -exec sha256sum {} \; | \
    sed 's|\./||' | sort > MANIFEST.sha256)

manifest_count=$(wc -l < "${STAGING_DIR}/MANIFEST.sha256")
print_substep "Manifest: ${manifest_count} files checksummed."

# Package metadata
# #1554: which architecture the bundled images are for.
#
# `docker save` writes the LOCAL image, so everything in images/ is the
# packaging machine's architecture — there is no per-image choice and no
# multi-arch manifest in a save tarball. A GB10 box is arm64 while the rest of
# the fleet is amd64 (modules/llm/runners/runners.yaml: "the GB10 target is
# arm64, the rest amd64"), so an amd64 package on a GB10 loads without a murmur
# and every container then dies with "exec format error" — after the upgrade
# has already restarted the stack. The receiving side can only refuse that if
# the package says what it holds.
#
# Empty when no images are bundled: a code-only package is architecture-neutral
# and must keep installing anywhere.
IMAGE_ARCH=""
if [ "$INCLUDE_IMAGES" = "true" ]; then
    IMAGE_ARCH="$(docker version --format '{{.Server.Arch}}' 2>/dev/null || true)"
    [ -n "$IMAGE_ARCH" ] || IMAGE_ARCH="$(uname -m)"
    # ONE spelling on both sides: docker answers Go's namespace (amd64), uname
    # the kernel's (x86_64), and the receiving end may read either (#1570).
    IMAGE_ARCH="$(rzfz_normalize_arch "$IMAGE_ARCH")"
fi

cat > "${STAGING_DIR}/PACKAGE_INFO" <<EOF
package_version=${VERSION}
package_tag=${TAG}
package_date=$(date -Iseconds)
package_commit=$(git rev-parse "$TAG" 2>/dev/null || echo "unknown")
include_images=${INCLUDE_IMAGES}
include_models=${INCLUDE_MODELS}
created_by=$(whoami)@$(hostname)
image_arch=${IMAGE_ARCH}
EOF

print_success "Manifest created."

# ==============================================================================
# Create Archive
# ==============================================================================
print_step "Creating upgrade package..."


ARCHIVE_PATH="${OUTPUT_DIR}/${PACKAGE_NAME}.tar.gz"
# #2241: build under a .partial name and publish only after the archive has been
# read back. A failed `tar` used to leave 49 GB of plausible package sitting
# under its final name — the models unreadable and therefore absent, the size
# and the manifest both convincing. An artefact that exists is an artefact
# somebody ships.
ARCHIVE_PARTIAL="${ARCHIVE_PATH}.partial"
rm -f "$ARCHIVE_PARTIAL"

tar czf "$ARCHIVE_PARTIAL" -C "$STAGING_DIR" .

# Read it back with a DIFFERENT reader than the one that wrote it, and compare
# against the staging tree rather than against the packager's own tally — the
# tally is separately known to be wrong by construction (#2234).
print_substep "Verifying the archive carries what was staged (#2241)..."
if ! "${SCRIPT_DIR}/scripts/verify-package-archive.sh" "$ARCHIVE_PARTIAL" "$STAGING_DIR"; then
    print_error "The package does not contain what was staged — NOT publishing it (#2241)."
    print_error "  kept for inspection: ${ARCHIVE_PARTIAL}"
    exit 1
fi

mv -f "$ARCHIVE_PARTIAL" "$ARCHIVE_PATH"
archive_size=$(du -sh "$ARCHIVE_PATH" | cut -f1)

print_success "Package created: ${ARCHIVE_PATH} (${archive_size})"

# ==============================================================================
# #781: the authenticity value — produced AFTER the archive, deliberately
# ==============================================================================
# Everything written into $STAGING_DIR before the `tar czf` above becomes a
# MEMBER of the package. That is exactly what MANIFEST.sha256 is, and it is why
# MANIFEST.sha256 cannot answer "is this stick genuine": a prepared package
# carries its own matching manifest. So this hash is computed over the finished
# ARCHIVE and written BESIDE it, never into it.
#
# The sidecar file is a convenience for scripted delivery. It is NOT the
# authenticity check by itself — it travels on the same medium as the package,
# so anyone who can replace the stick can replace the sidecar. The value that
# does the work is the one PRINTED below and transported out of band: into the
# release notes, or the fleet channel, where the receiving operator reads it
# and passes it to `rzfz upgrade --expect-sha256 <hash>`.
#
# Signing is the other half (#781 point 1) and is done just below when
# --sign-key is given — same placement, same reason: after the tar, beside the
# archive, never a member of it.
archive_sha256="$(sha256sum -- "$ARCHIVE_PATH" | cut -d' ' -f1)"
printf '%s  %s\n' "$archive_sha256" "$(basename "$ARCHIVE_PATH")" \
    > "${ARCHIVE_PATH}.sha256"
print_substep "Archive SHA-256 written beside the package (NOT inside it):"
print_substep "  ${ARCHIVE_PATH}.sha256"

# ==============================================================================
# #781: the OPTIONAL signature (operator decision 2026-09-02 — openssl)
# ==============================================================================
# The private key is handed in by path and comes from the SEQIS secrets vault.
# It is never generated, never copied into the output directory, and never put
# into the archive. The PUBLIC half is NOT written beside the package either:
# a key travelling with the stick is the attacker's key as far as the receiving
# box is concerned, so it goes out over the fleet install channel instead
# (docs/enterprise/how-to/offline-install.md).
#
# Without --sign-key nothing below runs and the package is exactly what it was
# before: unsigned, with the out-of-band hash as its only authenticity value.
package_signature=""
if [ -n "$SIGN_KEY" ]; then
    print_step "Signing the package (#781, openssl detached signature)..."
    if ! razzfazz_sign_package "$ARCHIVE_PATH" "$SIGN_KEY"; then
        print_error "Package signing failed — refusing to hand out an archive whose"
        print_error "signature state is unclear. The package remains at:"
        print_substep "  ${ARCHIVE_PATH}"
        exit 1
    fi
    package_signature="${ARCHIVE_PATH}.openssl.sig"
    # The public-key fingerprint identifies WHICH vault key signed this build,
    # so the receiving side can tell "wrong key" from "tampered stick" without
    # either party having to send a key anywhere.
    #
    # Guarded on openssl SUCCEEDING first: piping a failed `openssl pkey`
    # straight into sha256sum yields the hash of an empty stream — a
    # perfectly plausible-looking fingerprint that identifies nothing.
    signing_key_fp=""
    signing_key_passin=()
    if [ -n "${RAZZFAZZ_PACKAGE_SIGN_PASSPHRASE:-}" ]; then
        signing_key_passin=(-passin env:RAZZFAZZ_PACKAGE_SIGN_PASSPHRASE)
    fi
    if openssl pkey -in "$SIGN_KEY" "${signing_key_passin[@]}" -pubout -outform DER \
            </dev/null >/dev/null 2>&1; then
        signing_key_fp="$(openssl pkey -in "$SIGN_KEY" "${signing_key_passin[@]}" \
            -pubout -outform DER </dev/null 2>/dev/null | sha256sum | cut -d' ' -f1)"
    fi
    print_substep "  ${package_signature}"
    if [ -n "$signing_key_fp" ]; then
        print_substep "  signing key (SHA-256 of the public key, DER): ${signing_key_fp}"
    fi
fi


echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Package ready: ${PACKAGE_NAME}.tar.gz${NC}"
echo -e "${GREEN}║   Size: ${archive_size}${NC}"
echo -e "${GREEN}║                                                      ║${NC}"
echo -e "${GREEN}║   Transfer to production box and run:                ║${NC}"
echo -e "${GREEN}║   rzfz upgrade --package ${PACKAGE_NAME}.tar.gz${NC}"
echo -e "${GREEN}║     --expect-sha256 ${archive_sha256}${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
if [ -n "$package_signature" ]; then
    # Both files travel with the stick; only the signature is checked against
    # something that did not (the fleet-distributed public key).
    echo -e "${GREEN}Signed (#781). Copy alongside the package:${NC}"
    echo "  $(basename "$package_signature")"
    echo "  The receiving box verifies it against the public key the fleet"
    echo "  install channel put at /etc/razzfazz/package-keys/razzfazz-packages.pem"
    echo "  (or \$RAZZFAZZ_PACKAGE_PUBKEY). No key travels on the stick."
    echo ""
else
    echo -e "${YELLOW}UNSIGNED package (#781). --sign-key <vault-key> signs it.${NC}"
    echo "  Until then the out-of-band SHA-256 below is its only authenticity value."
    echo ""
fi
# #781: transport this value OUT OF BAND — release notes or the fleet channel,
# not on the stick. It is the only statement about this package that a prepared
# medium cannot forge, because it never travels with the medium.
echo -e "${YELLOW}SHA-256 (publish this separately from the package):${NC}"
echo "  ${archive_sha256}"
echo ""
