#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz.ai Stack - SBOM + CVE generation per release tag
# ==============================================================================
# Implements R-DEF-01 (OWASP-LLM-03 supply-chain validation): generates a
# CycloneDX 1.5 JSON SBOM for every container image used by the stack at
# the given release tag, runs Grype against each SBOM, and writes the
# results into ``releases/<tag>/sbom/`` for the audit trail.
#
# Image enumeration (the "konfig ∪ ist" union from security-review):
#   1. ``docker compose config --images`` — what compose files declare.
#   1b. ``config/manifests/versions.json`` runtime_only entries — per-user agents.
#   1c. ``modules/agents/manager/app/services/catalog.py`` image:version pairs — same agents from live catalog.
#   2. ``docker ps --format '{{.Image}}'`` — what's actually running.
#   3. or ``--images-from <file>`` — explicit list, one per line.
#
# Toolchain:
#   - Syft (CycloneDX SBOM generator) — host binary if present, else
#     ``anchore/syft:latest`` containerized fallback.
#   - Grype (CVE scanner) — host binary if present, else
#     ``anchore/grype:latest`` containerized fallback.
#
# Diff mode:
#   --diff-previous <tag>  produces ``cve-diff-vs-<tag>.md`` flagging
#                          NEW CRITICAL/HIGH findings vs that release's
#                          image set. Exit 2 if NEW criticals/highs
#                          exist (kassasturz gate).
#
#   To isolate REAL package/version deltas from Grype-vuln-DB growth (#194),
#   the previous tag's images (from its committed manifest.json) are RE-SCANNED
#   with the CURRENT Grype DB before diffing — so a CVE that is merely newly
#   *disclosed* since the last cut (added to the DB, same unchanged image) no
#   longer shows up as NEW and no longer mechanically trips the exit-2 gate.
#   Unchanged images are reused from the current run (no double scan); images
#   that can't be re-pulled fall back to the committed baseline (per-image).
#   Use --no-rescan-previous for the old fast path (offline / images
#   unavailable) — the diff header then flags the DB-growth caveat.
#
# Usage:
#   scripts/generate-sbom.sh <tag> [options]
#
# Options:
#   --output-dir <dir>       Override base output dir (default: releases/)
#   --images-from <file>     Read image list from file (one per line)
#   --diff-previous <tag>    Compare against a previous release tag
#   --no-rescan-previous     Diff against the previous committed baseline as-is
#                            (skip the current-DB re-scan; faster, but counts
#                            may include Grype-DB-growth false positives)
#   --syft-bin <path>        Override syft binary (default: auto-detect)
#   --grype-bin <path>       Override grype binary (default: auto-detect)
#   -h, --help               Show this help
# ==============================================================================

set -eo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

print_step()    { echo -e "${BLUE}[STEP] $1${NC}"; }
print_substep() { echo -e "${CYAN}  → $1${NC}"; }
print_success() { echo -e "${GREEN}[OK] $1${NC}"; }
print_warning() { echo -e "${YELLOW}[!] $1${NC}"; }
print_error()   { echo -e "${RED}[X] $1${NC}" >&2; }
print_info()    { echo -e "[i] $1"; }

show_help() {
    sed -n '2,53p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------- arg parsing -------------------------------------------------- #

TAG=""
OUTPUT_BASE=""
IMAGES_FROM=""
DIFF_PREVIOUS=""
RESCAN_PREVIOUS=1        # #194: re-scan the prev tag's images with the current DB before diffing
SYFT_BIN=""
GRYPE_BIN=""

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) show_help; exit 0 ;;
        --output-dir)    OUTPUT_BASE="$2"; shift 2 ;;
        --images-from)   IMAGES_FROM="$2"; shift 2 ;;
        --diff-previous) DIFF_PREVIOUS="$2"; shift 2 ;;
        --rescan-previous)    RESCAN_PREVIOUS=1; shift ;;
        --no-rescan-previous) RESCAN_PREVIOUS=0; shift ;;
        --syft-bin)      SYFT_BIN="$2"; shift 2 ;;
        --grype-bin)     GRYPE_BIN="$2"; shift 2 ;;
        --) shift; break ;;
        -*) print_error "Unknown option: $1"; show_help; exit 2 ;;
        *)
            if [ -z "$TAG" ]; then
                TAG="$1"
            else
                print_error "Unexpected positional arg: $1"
                exit 2
            fi
            shift
            ;;
    esac
done

if [ -z "$TAG" ]; then
    print_error "Missing required <tag> argument."
    echo
    show_help
    exit 2
fi

# Default output base = repo's releases/ dir.
if [ -z "$OUTPUT_BASE" ]; then
    OUTPUT_BASE="$STACK_DIR/releases"
fi

OUT_DIR="$OUTPUT_BASE/$TAG/sbom"
IMAGES_DIR="$OUT_DIR/images"
mkdir -p "$IMAGES_DIR"

# ---------- toolchain detection ----------------------------------------- #

detect_syft() {
    if [ -n "$SYFT_BIN" ] && [ -x "$SYFT_BIN" ]; then
        echo "$SYFT_BIN"
        return
    fi
    if command -v syft >/dev/null 2>&1; then
        command -v syft
        return
    fi
    # Containerized fallback wrapper script (lazy-written into TMP).
    echo ""
}

detect_grype() {
    if [ -n "$GRYPE_BIN" ] && [ -x "$GRYPE_BIN" ]; then
        echo "$GRYPE_BIN"
        return
    fi
    if command -v grype >/dev/null 2>&1; then
        command -v grype
        return
    fi
    echo ""
}

write_docker_wrapper() {
    # $1 = wrapper path; $2 = upstream image (anchore/syft:latest or anchore/grype:latest)
    local wrapper="$1"
    local upstream="$2"
    cat > "$wrapper" <<WRAPPER
#!/bin/bash
# Auto-generated thin wrapper that runs $upstream via docker.
# Mounts /var/run/docker.sock so the tool can introspect images by name.
# The working directory is mounted at its OWN absolute path (not /work) so that
# absolute output paths the caller passes (e.g. the per-image .cdx.json under
# releases/<tag>/sbom/images/) resolve identically inside the container — a
# /work-only mount silently discarded those writes, leaving empty SBOM/grype
# files and a useless CVE diff.
set -eo pipefail
exec docker run --rm \\
    -v /var/run/docker.sock:/var/run/docker.sock \\
    -v "\$(pwd)":"\$(pwd)" -w "\$(pwd)" \\
    "$upstream" "\$@"
WRAPPER
    chmod +x "$wrapper"
}

TMP_WRAPPERS="$(mktemp -d -t razzfazz-sbom-XXXXXX)"
trap 'rm -rf "$TMP_WRAPPERS"' EXIT

SYFT_PATH="$(detect_syft)"
if [ -z "$SYFT_PATH" ]; then
    SYFT_PATH="$TMP_WRAPPERS/syft"
    write_docker_wrapper "$SYFT_PATH" "anchore/syft:latest"
    print_info "syft not on PATH — using containerized anchore/syft:latest fallback."
fi

GRYPE_PATH="$(detect_grype)"
if [ -z "$GRYPE_PATH" ]; then
    GRYPE_PATH="$TMP_WRAPPERS/grype"
    write_docker_wrapper "$GRYPE_PATH" "anchore/grype:latest"
    print_info "grype not on PATH — using containerized anchore/grype:latest fallback."
fi

SYFT_VERSION="$("$SYFT_PATH" --version 2>&1 | head -n1 || echo unknown)"
GRYPE_VERSION="$("$GRYPE_PATH" --version 2>&1 | head -n1 || echo unknown)"
# Grype vuln-DB build stamp — recorded so a later cut can SEE the DB drift that
# would otherwise silently inflate the cve-diff (#194). Best-effort; older grype
# prints "Built:" under `db status`.
GRYPE_DB_STATUS="$("$GRYPE_PATH" db status 2>/dev/null | grep -iE 'built|schema|checksum|location' | tr '\n' ';' | sed 's/  */ /g; s/;$//' || true)"
[ -z "$GRYPE_DB_STATUS" ] && GRYPE_DB_STATUS="unknown"

# ---------- image enumeration ------------------------------------------- #

enumerate_images() {
    if [ -n "$IMAGES_FROM" ]; then
        if [ ! -f "$IMAGES_FROM" ]; then
            print_error "--images-from file not found: $IMAGES_FROM"
            exit 2
        fi
        # Strip blanks + comments.
        grep -vE '^\s*(#|$)' "$IMAGES_FROM"
        return
    fi
    # Live mode: union of FOUR sources:
    #   (a) compose-declared image: lines        — what the stack runs in standard profiles
    #   (b) currently-running container images   — what the box currently has up
    #   (c) config/manifests/versions.json runtime_only — per-user agent images that ship
    #       OUTSIDE the compose graph (provisioned by agent-manager per Authentik
    #       user via the 'My Agents' drawer; never appear in compose config --images
    #       and only appear in docker ps if a user has provisioned them — at
    #       whatever version they were last provisioned at, NOT the catalog pin).
    #   (d) agent-manager catalog.py 'image:version' pairs — same per-user agents
    #       as (c) but read from the live catalog (source of truth for what gets
    #       provisioned today). Sources (c) and (d) are deliberately both queried
    #       — the manifest entry is the audit pin; catalog.py is the runtime pin;
    #       they SHOULD match (drift between them is a manifest-staleness bug).
    # All four are unioned to ensure per-user agents are covered even on a box
    # where no user has provisioned them yet (R-DEF-01 / OWASP-LLM-03 supply-chain
    # validation must include the FULL per-platform-customer-can-reach surface,
    # not just what happens to be `docker ps`-visible at SBOM time).
    local compose_imgs="" running_imgs="" runtime_only_imgs="" catalog_imgs=""
    if command -v docker >/dev/null 2>&1; then
        if [ -f "$STACK_DIR/compose.yml" ]; then
            compose_imgs="$(cd "$STACK_DIR" && docker compose config --images 2>/dev/null || true)"
        fi
        running_imgs="$(docker ps --format '{{.Image}}' 2>/dev/null || true)"
    else
        print_error "docker not on PATH and --images-from not given; cannot enumerate."
        exit 2
    fi
    # (c) config/manifests/versions.json runtime_only — per-user agent images
    if [ -f "$STACK_DIR/config/manifests/versions.json" ] && command -v python3 >/dev/null 2>&1; then
        runtime_only_imgs="$(python3 -c "
import json, sys
try:
    m = json.load(open('$STACK_DIR/config/manifests/versions.json'))
    for _, v in m.get('hardcoded', {}).items():
        if v.get('runtime_only') and v.get('image') and v.get('current'):
            print(f\"{v['image']}:{v['current']}\")
except Exception as e:
    sys.stderr.write(f'runtime_only enumeration failed: {e}\n')
" 2>/dev/null || true)"
    fi
    # (d) agent-manager catalog.py 'image:version' pairs (skips ':latest' mutable tags)
    if [ -f "$STACK_DIR/modules/agents/manager/app/services/catalog.py" ] && command -v python3 >/dev/null 2>&1; then
        catalog_imgs="$(python3 -c "
import re, sys
try:
    src = open('$STACK_DIR/modules/agents/manager/app/services/catalog.py').read()
    # Each agent entry is a dict; pick up 'image':...'version':... pairs in order
    # via simple regex (avoids importing the module which pulls Flask deps).
    images = re.findall(r\"'image':\s*'([^']+)'\", src)
    versions = re.findall(r\"'(?:version|companion_version)':\s*'([^']+)'\", src)
    # Map each image to subsequent versions — versions appear right after
    # image, sometimes plus a companion_version. We rely on catalog ordering:
    # for each image, emit image:next_version_in_list (which is the per-user pin).
    # Skip ':latest' — those are operator-side custom builds, not auditable pins.
    for img in images:
        # Find first version following this image's position
        idx = src.index(f\"'image': '{img}'\")
        rest = src[idx:idx+800]  # look ahead in the same dict block
        m = re.search(r\"'(?:version|companion_version)':\s*'([^']+)'\", rest)
        if m and m.group(1) != 'latest':
            print(f\"{img}:{m.group(1)}\")
        # Also pick up companion_image / companion_version pairs
        cm_img = re.search(r\"'companion_image':\s*'([^']+)'\", rest)
        cm_ver = re.search(r\"'companion_version':\s*'([^']+)'\", rest)
        if cm_img and cm_ver and cm_ver.group(1) != 'latest':
            print(f\"{cm_img.group(1)}:{cm_ver.group(1)}\")
except Exception as e:
    sys.stderr.write(f'catalog enumeration failed: {e}\n')
" 2>/dev/null || true)"
    fi
    printf '%s\n%s\n%s\n%s\n' "$compose_imgs" "$running_imgs" "$runtime_only_imgs" "$catalog_imgs" | sed '/^\s*$/d' | sort -u
}

mapfile -t IMAGES < <(enumerate_images)
if [ "${#IMAGES[@]}" -eq 0 ]; then
    print_error "No images to scan."
    exit 2
fi

print_step "Generating SBOMs for $TAG (${#IMAGES[@]} image(s))..."
print_substep "Syft:  $SYFT_VERSION"
print_substep "Grype: $GRYPE_VERSION"
print_substep "Output: $OUT_DIR"

# ---------- safe-image-name (matches security-review scheme) ------------ #

safe_name() {
    # Replace '/' and ':' with '__' — same as security-review SKILL.md.
    # NB: `tr '/:' '__'` doesn't expand a 1-char source to a 2-char target;
    # use sed for the actual `__` doubling.
    echo "$1" | sed -e 's|/|__|g' -e 's|:|__|g' -e 's| ||g'
}

# ---------- per-image scan (reusable) ----------------------------------- #

scan_one_image() {
    # $1 = image ref, $2 = output images dir.
    # Writes <safe>.cdx.json + <safe>.grype.json into $2.
    # Returns 0 iff a grype.json was successfully produced.
    local img="$1" out_dir="$2"
    local safe sbom_path grype_path
    safe="$(safe_name "$img")"
    sbom_path="$out_dir/${safe}.cdx.json"
    grype_path="$out_dir/${safe}.grype.json"

    # Syft: produce CycloneDX 1.5 JSON SBOM. Pin the format explicitly
    # via the --output flag (real syft accepts --output cyclonedx-json=<path>).
    if ! "$SYFT_PATH" "$img" --output "cyclonedx-json=$sbom_path" >/dev/null 2>&1; then
        # Some syft versions want the path arg separately; fall back.
        if ! "$SYFT_PATH" "$img" -o "cyclonedx-json=$sbom_path" >/dev/null 2>&1; then
            print_error "    syft failed for $img"
            return 1
        fi
    fi

    # Grype: scan the SBOM, write JSON.
    if ! "$GRYPE_PATH" "sbom:$sbom_path" -o json > "$grype_path" 2>/dev/null; then
        # Stub-friendly fallback — call with the SBOM path as positional.
        if ! "$GRYPE_PATH" "$sbom_path" -o json > "$grype_path" 2>/dev/null; then
            print_warning "    grype failed for $img"
            return 1
        fi
    fi
    return 0
}

# ---------- per-image scan loop (current tag) --------------------------- #

for img in "${IMAGES[@]}"; do
    print_substep "$img"
    scan_one_image "$img" "$IMAGES_DIR" || true
done

# Guard: a working syft produces one SBOM per image. If NONE were produced,
# the (known-unreliable) containerized anchore/syft fallback silently wrote
# nothing — fail loudly instead of emitting an empty, misleading SBOM/diff.
# Fix is to install the syft + grype BINARIES on the scan host. Also fail if
# the live stack starved the daemon so badly that almost nothing scanned
# (run image scans against an idle daemon: `docker compose stop` first).
CDX_COUNT="$(find "$IMAGES_DIR" -maxdepth 1 -name '*.cdx.json' 2>/dev/null | wc -l)"
TOTAL_IMAGES="${#IMAGES[@]}"
if [ "$CDX_COUNT" -eq 0 ]; then
    print_error "0 SBOMs produced for $TOTAL_IMAGES image(s) — syft is not working."
    print_error "  The containerized anchore/syft fallback is known-unreliable; install the"
    print_error "  syft + grype BINARIES on this host (then re-run). Aborting to avoid an"
    print_error "  empty, misleading SBOM/CVE-diff."
    exit 1
fi
if [ "$CDX_COUNT" -lt "$(( TOTAL_IMAGES / 2 ))" ]; then
    print_warning "Only $CDX_COUNT/$TOTAL_IMAGES images scanned — likely daemon contention from a"
    print_warning "  running stack. For a complete inventory, stop the stack (idle daemon) and re-run."
fi

# ---------- manifest.json ----------------------------------------------- #

GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
MANIFEST="$OUT_DIR/manifest.json"

# Use python for JSON to avoid jq dependency.
python3 - "$MANIFEST" "$TAG" "$GENERATED_AT" "$SYFT_VERSION" "$GRYPE_VERSION" "$GRYPE_DB_STATUS" "${IMAGES[@]}" <<'PY'
import json, sys
manifest, tag, gen, syft_v, grype_v, grype_db = sys.argv[1:7]
images = sys.argv[7:]
doc = {
    "tag": tag,
    "generated_at": gen,
    "syft_version": syft_v,
    "grype_version": grype_v,
    # #194: record the Grype vuln-DB build stamp so a later --diff-previous can
    # detect DB drift vs this baseline (the driver of the false-"NEW" inflation).
    "grype_db": grype_db,
    "images": images,
}
with open(manifest, "w") as f:
    json.dump(doc, f, indent=2)
    f.write("\n")
PY

# ---------- cve-summary.md ---------------------------------------------- #

SUMMARY="$OUT_DIR/cve-summary.md"

python3 - "$SUMMARY" "$TAG" "$IMAGES_DIR" "${IMAGES[@]}" <<'PY'
import json, os, sys
from pathlib import Path

summary_path, tag, images_dir = sys.argv[1:4]
images = sys.argv[4:]

def safe(name: str) -> str:
    return name.replace("/", "__").replace(":", "__").replace(" ", "")

SEVS = ("Critical", "High", "Medium", "Low", "Negligible", "Unknown")

lines = [f"# SBOM CVE summary — {tag}", ""]
lines.append("| Image | Critical | High | Medium | Low | Total |")
lines.append("|---|---:|---:|---:|---:|---:|")

totals = {s: 0 for s in SEVS}
for img in images:
    grype_file = Path(images_dir) / f"{safe(img)}.grype.json"
    counts = {s: 0 for s in SEVS}
    if grype_file.is_file():
        try:
            doc = json.loads(grype_file.read_text() or "{}")
            for m in doc.get("matches", []):
                sev = m.get("vulnerability", {}).get("severity", "Unknown")
                counts[sev] = counts.get(sev, 0) + 1
        except json.JSONDecodeError:
            pass
    for s in SEVS:
        totals[s] = totals.get(s, 0) + counts.get(s, 0)
    total = sum(counts.values())
    lines.append(
        f"| `{img}` | {counts['Critical']} | {counts['High']} "
        f"| {counts['Medium']} | {counts['Low']} | {total} |"
    )

grand = sum(totals.values())
lines.append(
    f"| **TOTAL** | **{totals['Critical']}** | **{totals['High']}** "
    f"| **{totals['Medium']}** | **{totals['Low']}** | **{grand}** |"
)
lines.append("")
lines.append(f"_Generated by `scripts/generate-sbom.sh {tag}`._")
Path(summary_path).write_text("\n".join(lines) + "\n")
PY

print_success "SBOMs written to $OUT_DIR"
print_info "Summary: $SUMMARY"

# ---------- compact CVE baseline (committable) -------------------------- #
# The per-image *.grype.json / *.cdx.json artifacts are large (hundreds of MB
# for a full stack) and are NOT committed (see .gitignore). To still give the
# NEXT release a real diff baseline, emit a compact tab-separated set of
# (image, cve, severity) tuples. This file is small enough to commit and is
# what `--diff-previous` prefers when present.
BASELINE="$OUT_DIR/cve-baseline.tsv"
python3 - "$BASELINE" "$IMAGES_DIR" <<'PY'
import json, sys
from pathlib import Path
baseline_path, images_dir = sys.argv[1:3]
rows = set()
for gf in Path(images_dir).glob("*.grype.json"):
    img = gf.stem.replace(".grype", "")
    try:
        doc = json.loads(gf.read_text() or "{}")
    except json.JSONDecodeError:
        continue
    for m in doc.get("matches", []):
        v = m.get("vulnerability", {})
        cve = v.get("id", "")
        sev = v.get("severity", "Unknown")
        if cve:
            rows.add((img, cve, sev))
with open(baseline_path, "w") as fh:
    for img, cve, sev in sorted(rows):
        fh.write(f"{img}\t{cve}\t{sev}\n")
print(f"compact baseline: {len(rows)} (image, cve, severity) rows")
PY
print_info "Compact baseline: $BASELINE"

# ---------- diff vs previous tag (optional) ----------------------------- #

EXIT_RC=0

if [ -n "$DIFF_PREVIOUS" ]; then
    # release dirs under releases/ are named WITHOUT the leading "v" (e.g.
    # releases/2026.05-ga.7), but callers naturally pass a git tag WITH the "v"
    # (e.g. v2026.05-ga.7 from `git describe`). Resolve the prev SBOM base dir by
    # trying the arg verbatim first, then with a stripped leading "v".
    PREV_BASE="$OUTPUT_BASE/$DIFF_PREVIOUS/sbom"
    if [ ! -d "$PREV_BASE" ] && [ -d "$OUTPUT_BASE/${DIFF_PREVIOUS#v}/sbom" ]; then
        PREV_BASE="$OUTPUT_BASE/${DIFF_PREVIOUS#v}/sbom"
    fi
    DIFF_PATH="$OUT_DIR/cve-diff-vs-${DIFF_PREVIOUS}.md"

    # The committed compact baseline (cve-baseline.tsv, scanned with the PREVIOUS
    # release's Grype DB) is both the fast-path source and the per-image gap-fill
    # source for images that can't be re-scanned now.
    PREV_COMMITTED=""
    if [ -f "$PREV_BASE/cve-baseline.tsv" ]; then
        PREV_COMMITTED="$PREV_BASE/cve-baseline.tsv"
    fi

    PREV_SRC=""
    DIFF_METHOD=""

    # --------------------------------------------------------------------- #
    # #194 fix — re-scan the PREVIOUS tag's image set with the CURRENT Grype
    # DB so the diff isolates real package/version deltas from vuln-DB growth.
    #
    # Root cause of the old false-"NEW" inflation: the previous baseline was
    # scanned with an OLDER Grype DB. Between cuts the DB gains newly-*disclosed*
    # CVEs; those land on UNCHANGED images (postgres, valkey, gitea, authentik,
    # …) and showed up as "NEW" purely because the previous baseline's DB didn't
    # know them yet — mechanically tripping the exit-2 gate on every cut. Scanning
    # the prev images with the SAME (current) DB makes such a CVE appear on BOTH
    # sides, so it cancels and is no longer counted NEW. What remains is the real
    # signal: CVEs on packages/versions we actually introduced this cut.
    #
    # Cost control: images already scanned in THIS run (unchanged, identical tag)
    # are reused — no second pull. Locally-built razzfazz-* images at the prev tag
    # can't be re-pulled, so they gap-fill from the committed baseline (a tiny,
    # every-cut-churning surface — acceptable residual DB-growth for those only).
    # --------------------------------------------------------------------- #
    if [ "$RESCAN_PREVIOUS" -eq 1 ] && [ -f "$PREV_BASE/manifest.json" ] && command -v python3 >/dev/null 2>&1; then
        print_step "Re-scanning $DIFF_PREVIOUS image set with the CURRENT Grype DB (#194 — isolates DB-growth)..."
        PREV_RESCAN_DIR="$TMP_WRAPPERS/prev-rescan"
        mkdir -p "$PREV_RESCAN_DIR"
        mapfile -t PREV_IMAGES < <(python3 -c "
import json, sys
try:
    d = json.load(open('$PREV_BASE/manifest.json'))
    for i in d.get('images', []):
        print(i)
except Exception as e:
    sys.stderr.write('prev manifest read failed: %s\n' % e)
")
        rescanned=0; reused=0; gapfilled=0
        for pimg in "${PREV_IMAGES[@]}"; do
            [ -z "$pimg" ] && continue
            psafe="$(safe_name "$pimg")"
            # (1) Reuse this run's scan when the exact image was already scanned
            #     with the current DB (unchanged image → identical, DB-consistent).
            if [ -f "$IMAGES_DIR/${psafe}.grype.json" ]; then
                cp "$IMAGES_DIR/${psafe}.grype.json" "$PREV_RESCAN_DIR/${psafe}.grype.json"
                reused=$((reused+1)); continue
            fi
            # (2) Locally-built / private images (razzfazz-*, gitlab-registry
            #     gpustack build) can't be re-pulled at the prev tag → gap-fill.
            case "$pimg" in
                *razzfazz*)
                    if [ -n "$PREV_COMMITTED" ]; then
                        awk -F'\t' -v i="$psafe" '$1==i' "$PREV_COMMITTED" > "$PREV_RESCAN_DIR/${psafe}.gapfill.tsv" 2>/dev/null || true
                    fi
                    gapfilled=$((gapfilled+1)); continue ;;
            esac
            # (3) Prev-only upstream image (e.g. one removed this cut): re-scan it
            #     now with the current DB so its CVEs are keyed consistently.
            print_substep "prev: $pimg"
            if scan_one_image "$pimg" "$PREV_RESCAN_DIR"; then
                rescanned=$((rescanned+1))
            else
                if [ -n "$PREV_COMMITTED" ]; then
                    awk -F'\t' -v i="$psafe" '$1==i' "$PREV_COMMITTED" > "$PREV_RESCAN_DIR/${psafe}.gapfill.tsv" 2>/dev/null || true
                fi
                gapfilled=$((gapfilled+1))
            fi
        done
        # Build a fresh compact baseline from the (current-DB) rescanned grype
        # JSONs + any committed-baseline gap-fill rows.
        PREV_RESCAN_TSV="$TMP_WRAPPERS/prev-rescan-baseline.tsv"
        python3 - "$PREV_RESCAN_TSV" "$PREV_RESCAN_DIR" <<'PY'
import json, sys, glob, os
out_path, d = sys.argv[1:3]
rows = set()
for gf in glob.glob(os.path.join(d, "*.grype.json")):
    img = os.path.basename(gf)[:-len(".grype.json")]
    try:
        doc = json.loads(open(gf).read() or "{}")
    except Exception:
        continue
    for m in doc.get("matches", []):
        v = m.get("vulnerability", {})
        cve = v.get("id", "")
        sev = v.get("severity", "Unknown")
        if cve:
            rows.add((img, cve, sev))
for tf in glob.glob(os.path.join(d, "*.gapfill.tsv")):
    for line in open(tf):
        p = line.rstrip("\n").split("\t")
        if len(p) == 3 and p[1]:
            rows.add((p[0], p[1], p[2]))
with open(out_path, "w") as fh:
    for r in sorted(rows):
        fh.write("\t".join(r) + "\n")
PY
        PREV_SRC="$PREV_RESCAN_TSV"
        DIFF_METHOD="rescan-current-db (DB-growth isolated) — prev images: ${rescanned} rescanned, ${reused} reused-from-current-run, ${gapfilled} committed-baseline-gapfilled"
        print_success "Prev-set rescan complete: ${rescanned} rescanned, ${reused} reused, ${gapfilled} gap-filled"
    fi

    # Fast/fallback path: rescan disabled (--no-rescan-previous), no prev
    # manifest.json, or python missing. Diff against the committed baseline as-is
    # (may include DB-growth false positives — flagged in the diff header).
    if [ -z "$PREV_SRC" ]; then
        if [ -n "$PREV_COMMITTED" ]; then
            PREV_SRC="$PREV_COMMITTED"
        elif [ -d "$PREV_BASE/images" ]; then
            PREV_SRC="$PREV_BASE/images"
        fi
        DIFF_METHOD="committed-baseline (previous Grype DB) — CAVEAT: NEW counts may include Grype-DB-growth false positives; the default current-DB rescan needs ${PREV_BASE}/manifest.json present"
    fi

    if [ -z "$PREV_SRC" ]; then
        print_warning "Previous-release CVE baseline not found under $PREV_BASE — skipping diff."
        # Write a stub diff file noting the skip so the audit trail
        # records WHY the diff is missing.
        {
            echo "# CVE diff vs $DIFF_PREVIOUS — SKIPPED"
            echo
            echo "No \`cve-baseline.tsv\` / \`manifest.json\` / \`images/\` under \`$PREV_BASE\`."
            echo "Run \`scripts/generate-sbom.sh $DIFF_PREVIOUS\` first to populate it,"
            echo "then re-run with \`--diff-previous $DIFF_PREVIOUS\`."
        } > "$DIFF_PATH"
    else
        print_step "Diffing CVE inventory vs $DIFF_PREVIOUS [${DIFF_METHOD%% —*}]..."
        python3 - "$DIFF_PATH" "$IMAGES_DIR" "$PREV_SRC" "$TAG" "$DIFF_PREVIOUS" "$DIFF_METHOD" <<'PY' || EXIT_RC=$?
import json, os, sys
from pathlib import Path

diff_path, cur_src, prev_src, tag, prev_tag = sys.argv[1:6]
method = sys.argv[6] if len(sys.argv) > 6 else ""

def collect(src):
    """Return (keys, attribution).

    keys: set of (cve_id, severity).
    attribution: {(cve_id, severity): set(image-identifier)} — best-effort.

    The diff is keyed on (cve, severity), NOT (image, cve, severity), on
    purpose. The per-image identifier is unstable across runs: a
    locally-built image gets a fresh image-id on every rebuild, and a
    baseline generated on a different host can key the same image
    differently (image-SHA vs name:tag). Including it in the diff key made
    every rebuilt/relabelled image churn as NEW+GONE — thousands of false
    positives that buried the real signal. Keying on the CVE answers the
    actual release question ("did we introduce new vulnerabilities?")
    independent of image-identity noise. Per-image attribution is kept for
    the NEW table (from the current scan only).

    `src` is either a compact baseline .tsv (image<TAB>cve<TAB>severity per
    line) or a directory of per-image *.grype.json files.
    """
    keys = set()
    attr = {}
    p = Path(src)
    if p.is_file() and p.suffix == ".tsv":
        for line in p.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[1]:
                img, cve, sev = parts
                keys.add((cve, sev))
                attr.setdefault((cve, sev), set()).add(img)
        return keys, attr
    for gf in p.glob("*.grype.json"):
        img = gf.stem.replace(".grype", "")
        try:
            doc = json.loads(gf.read_text() or "{}")
        except json.JSONDecodeError:
            continue
        for m in doc.get("matches", []):
            v = m.get("vulnerability", {})
            cve = v.get("id", "")
            sev = v.get("severity", "Unknown")
            if cve:
                keys.add((cve, sev))
                attr.setdefault((cve, sev), set()).add(img)
    return keys, attr

cur, cur_attr = collect(cur_src)
prev, _ = collect(prev_src)

new = sorted(cur - prev)
gone = sorted(prev - cur)
persistent = sorted(cur & prev)

new_critical = [x for x in new if x[1] == "Critical"]
new_high = [x for x in new if x[1] == "High"]

def imgs_for(key):
    return ", ".join(sorted(cur_attr.get(key, set()))) or "—"

lines = [
    f"# CVE diff: {tag} vs {prev_tag}",
    "",
    "_Keyed on (CVE, severity) across the full image set — robust to "
    "image-rebuild / host-keying churn. Per-image counts: `cve-summary.md`._",
    "",
    f"_Baseline method: {method or 'committed-baseline'}._",
    "",
    f"- NEW findings: **{len(new)}** (Critical: {len(new_critical)}, High: {len(new_high)})",
    f"- GONE since previous: {len(gone)}",
    f"- PERSISTENT (already accepted at {prev_tag}): {len(persistent)}",
    "",
]

if new:
    lines.append("## NEW findings")
    lines.append("")
    lines.append("| CVE | Severity | Image(s) |")
    lines.append("|---|---|---|")
    for cve, sev in new:
        lines.append(f"| {cve} | {sev} | `{imgs_for((cve, sev))}` |")
    lines.append("")

if gone:
    lines.append("## GONE (informational)")
    lines.append("")
    lines.append("| CVE | Severity |")
    lines.append("|---|---|")
    for cve, sev in gone:
        lines.append(f"| {cve} | {sev} |")
    lines.append("")

Path(diff_path).write_text("\n".join(lines) + "\n")

# Exit 2 to signal a release-gate trip if any NEW Critical or High.
if new_critical or new_high:
    sys.exit(2)
PY
        if [ "$EXIT_RC" -eq 2 ]; then
            print_warning "NEW CRITICAL/HIGH findings detected — see $DIFF_PATH"
        else
            print_success "No NEW CRITICAL/HIGH findings — see $DIFF_PATH"
        fi
    fi
fi

exit "$EXIT_RC"
