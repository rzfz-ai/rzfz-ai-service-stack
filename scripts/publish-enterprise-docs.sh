#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# publish-enterprise-docs.sh — one-way, idempotent publisher for the gated
# Enterprise docs (docs/enterprise/) to a pluggable target (docs.rzfz.ai / #162).
#
# Source of truth is git; this NEVER reads back or authors in the target. The
# markdown render + page-tree mapping are target-agnostic; all Confluence-specific
# logic lives behind `--target confluence`. The documented exit path is
# `--target static` (P3). See .gsd/reports/2026.08-docs-rzfz-ai-portal-design.md.
#
# Sits beside scripts/publish-community-wiki.sh (Codeberg) and scripts/publish-wiki.sh
# (Gitea). Token from the environment, never hardcoded.
#
# Usage: scripts/publish-enterprise-docs.sh [--target confluence|static] [--dry-run] [--source DIR]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="confluence"; DRY=0; SRC="$REPO_ROOT/docs/enterprise"

usage(){ cat <<EOF
publish-enterprise-docs.sh — one-way, idempotent Enterprise-docs publisher (2026.08 #docs)

Usage: publish-enterprise-docs.sh [--target confluence|static] [--dry-run] [--source DIR]
  --target   render target adapter: confluence (default) | static (P3 stub)
  --dry-run  print the planned create/update actions and exit; makes NO network call
  --source   docs source dir (default: docs/enterprise)
  -h,--help  show this help

Env (confluence target): ATLASSIAN_SITE (e.g. rzfz.atlassian.net), ATLASSIAN_EMAIL,
  ATLASSIAN_API_TOKEN, CONFLUENCE_SPACE_KEY (e.g. RZFZDOCS). Never hardcode the token.
EOF
}

while [ $# -gt 0 ]; do case "$1" in
  --target)  TARGET="$2"; shift 2;;
  --dry-run) DRY=1; shift;;
  --reset)   RESET=1; shift;;
  --source)  SRC="$2"; shift 2;;
  -h|--help) usage; exit 0;;
  *) echo "unknown arg: $1" >&2; usage >&2; exit 2;;
esac; done

[ -d "$SRC" ] || { echo "source dir not found: $SRC" >&2; exit 1; }

ARGS=(--target "$TARGET" --source "$SRC")
[ "$DRY" = 1 ] && ARGS+=(--dry-run)
[ "${RESET:-0}" = 1 ] && ARGS+=(--reset)
exec python3 "$REPO_ROOT/scripts/lib/confluence_publish.py" "${ARGS[@]}"
