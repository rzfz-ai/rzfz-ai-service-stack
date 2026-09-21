#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# Guard: a release-notes file that becomes a PUBLIC artifact — the GitHub Release
# body (fed verbatim to `gh release create --notes-file` by publish-public.sh) or a
# file shipped in the public mirror — MUST NOT reference internal-only artifacts.
#
# The public export strips the internal trees (security-run/, .gsd/, .claude/,
# docs/enterprise/) and the public channel has no access to git.razzfazz.ai, so any
# reference to those is a DANGLING pointer on the public side, plus an internal-path
# / internal-tracker leak. This is the exact defect that shipped in the
# v2026.08-ga.12 Release body (Security section cited security-run/…md + "the release
# checklist", neither present on GitHub).
#
# Kept as one file so the author-time lint gate (lint-release-docs.sh) and the
# publish-time gate (publish-public.sh) share ONE source of truth for the markers.
#
# Usage:  check-public-notes-safe.sh <notes-file> [<notes-file> ...]
# Exit:   0  clean (or no existing files given)
#         1  one or more internal markers found

set -uo pipefail

# Internal markers that must never appear in a customer-facing release note.
#   security-run/ .gsd/ .claude/ docs/enterprise/  — internal trees stripped from the export
#   release checklist / RELEASE_CHECKLIST          — internal release artifact
#   git.razzfazz.ai                                — internal Gitea host (public has no access)
#   M0NN (e.g. M029, M032)                         — internal milestone IDs
#   192.168.x / 172.16-31.x                        — private/fleet IPs
PATTERNS='security-run/|\.gsd/|\.claude/|docs/enterprise/|[Rr]elease [Cc]hecklist|RELEASE_CHECKLIST|git\.razzfazz\.ai|\bM0[0-9][0-9]\b|\b192\.168\.[0-9]|\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]|enterprise-only'

rc=0
for f in "$@"; do
    [ -f "$f" ] || continue
    if grep -qE "$PATTERNS" "$f" 2>/dev/null; then
        echo "[!] internal markers in $f (would leak into / dangle on the public release body):"
        grep -nE "$PATTERNS" "$f" | sed 's/^/    /' | head -40
        rc=1
    fi
done
exit $rc
