#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# sync-enterprise-overlay.sh — populate the box-local Enterprise-docs overlay (#125)
# ------------------------------------------------------------------------------------
# The Help-UI bakes docs/community as its BASE own_docs and RUNTIME-MOUNTS a box-local,
# gitignored overlay (overlay/enterprise/docs -> /app/own_docs/enterprise) for the
# gated Enterprise docs (see core/help/Dockerfile + core/compose.yml razzfazz-help).
#
# TWO modes:
#
#   (default, no args) — P1 repo-mirror bridge:
#     - INTERNAL box (docs/enterprise/ present in the checkout): mirror docs/enterprise/
#       into overlay/enterprise/docs/ so the Help-UI serves the Enterprise docs.
#     - PUBLIC / Codeberg box (docs/enterprise/ absent): no content sync — the overlay is
#       left as-is (community-only unless a USB bake / offline package already populated
#       it — see --from-payload).
#
#   --from-payload <dir> — P1.5 self-contained payload stage (USB build + offline package):
#     Stage a self-contained Enterprise-overlay payload (assembled at build/package time —
#     NOT from the git tree) into the box-local overlay. The payload carries everything a
#     "subscription box" ships locally:
#         <dir>/docs/                    -> overlay/enterprise/docs/          (gated Enterprise docs)
#         <dir>/security-run/            -> overlay/enterprise/security-run/  (release security assessment)
#         <dir>/sbom/                    -> overlay/enterprise/sbom/          (release SBOM/CVE export)
#         <dir>/security-architecture.md -> overlay/enterprise/security-architecture.md (#171 — Config
#                                           Portal /security/ falls back to it on customer boxes)
#     This path does NOT depend on docs/enterprise/ being present in the git tree, so an
#     airgapped / Codeberg-origin subscription box gets the full overlay with no network
#     fetch, and it SURVIVES a later Codeberg upgrade (overlay/ is gitignored — a public
#     tag's `git checkout` never touches it).
#
# It ALWAYS creates the overlay directory (even empty) so the compose bind-mount source
# exists — otherwise Docker auto-creates it as a root-owned empty dir. An empty overlay
# is harmless: discover_own_docs() then finds no Enterprise docs and the hub is
# community-only (no crash).
#
# RETENTION BY CONSTRUCTION: overlay/ is gitignored, so `git checkout <codeberg-tag>`
# during an upgrade never touches it — the Enterprise content survives an upgrade to a
# public tag that has no docs/enterprise/ in the tree.
#
# Idempotent. Safe to run repeatedly (init, upgrade, first-boot, or by hand). Best-effort:
# a failure here must not abort an install/upgrade (callers wrap it accordingly).
set -euo pipefail

# Repo root = parent of this script's scripts/ directory.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DEST_ROOT="overlay/enterprise"
DOCS_SRC="docs/enterprise"
DOCS_DEST="${DEST_ROOT}/docs"

FROM_PAYLOAD=""
while [ $# -gt 0 ]; do
    case "$1" in
        --from-payload) FROM_PAYLOAD="$2"; shift 2 ;;
        -h|--help)
            sed -n '5,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "[sync-enterprise-overlay] unknown arg: $1" >&2; exit 2 ;;
    esac
done

# mirror <src-dir> <dest-dir>: copy src/ -> dest/ (rsync if available, else cp -a).
# `delete` (3rd arg = "delete") drops dest files removed upstream — used only for the
# authoritative repo-mirror docs case; payload staging is additive.
mirror() {
    local src="$1" dest="$2" delete="${3:-}"
    mkdir -p "$dest"
    if command -v rsync >/dev/null 2>&1; then
        if [ "$delete" = "delete" ]; then
            rsync -a --delete "$src"/ "$dest"/
        else
            rsync -a "$src"/ "$dest"/
        fi
    else
        [ "$delete" = "delete" ] && find "$dest" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
        cp -a "$src"/. "$dest"/
    fi
}

# Always ensure the overlay docs dir exists (compose bind-mount source).
mkdir -p "$DOCS_DEST"

# ---------- P1.5: self-contained payload stage --------------------------------------
if [ -n "$FROM_PAYLOAD" ]; then
    if [ ! -d "$FROM_PAYLOAD" ]; then
        echo "[sync-enterprise-overlay] payload dir '$FROM_PAYLOAD' not found — nothing staged." >&2
        exit 0
    fi
    staged=0
    for sub in docs security-run sbom; do
        if [ -d "$FROM_PAYLOAD/$sub" ] && [ -n "$(ls -A "$FROM_PAYLOAD/$sub" 2>/dev/null)" ]; then
            mirror "$FROM_PAYLOAD/$sub" "${DEST_ROOT}/$sub"
            echo "[sync-enterprise-overlay] staged ${sub}/ -> ${DEST_ROOT}/${sub}/"
            staged=$((staged + 1))
        fi
    done
    # Single-file component (#171): the CURRENT gated security-architecture doc.
    # Customer/Codeberg boxes render it via the Config Portal /security/ overlay
    # fallback — docs/security-architecture.md is stripped from the public export.
    if [ -f "$FROM_PAYLOAD/security-architecture.md" ]; then
        mkdir -p "$DEST_ROOT"
        cp -a "$FROM_PAYLOAD/security-architecture.md" "${DEST_ROOT}/security-architecture.md"
        echo "[sync-enterprise-overlay] staged security-architecture.md -> ${DEST_ROOT}/security-architecture.md"
        staged=$((staged + 1))
    fi
    if [ "$staged" -gt 0 ]; then
        echo "[sync-enterprise-overlay] Enterprise overlay staged from payload '${FROM_PAYLOAD}' (${staged} component(s)) — Help-UI serves the full gated docs."
    else
        echo "[sync-enterprise-overlay] payload '${FROM_PAYLOAD}' had no docs/security-run/sbom content — overlay left as-is."
    fi
    exit 0
fi

# ---------- P1: repo-mirror bridge (internal box) -----------------------------------
if [ -d "$DOCS_SRC" ]; then
    # docs/enterprise/ is the source of truth on internal boxes, so --delete drops
    # overlay files removed upstream. Only runs when DOCS_SRC exists (never on a public
    # box), so it can never wipe a payload-populated overlay.
    mirror "$DOCS_SRC" "$DOCS_DEST" delete
    echo "[sync-enterprise-overlay] Synced ${DOCS_SRC}/ -> ${DOCS_DEST}/ (internal box: Enterprise docs mounted into Help-UI)."
else
    echo "[sync-enterprise-overlay] No ${DOCS_SRC}/ in checkout (public/Codeberg box) — overlay left as-is (community-only)."
fi
