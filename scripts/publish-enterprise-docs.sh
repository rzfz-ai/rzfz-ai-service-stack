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

# ── #1844: say which interpreter is missing what, before running anything ────
#
# `confluence_publish.py` imports `markdown`, and `markdown` lives in
# tests/requirements.txt — i.e. in the test venv, not necessarily in whatever
# `python3` resolves to. That is not theory: the CI runner's scripts tier went
# red with `ModuleNotFoundError: No module named 'markdown'` because pytest runs
# as `tests/.venv/bin/python -m pytest` WITHOUT activating the venv, so a bare
# `python3` in this script was the system interpreter (#1840 fixed the test
# side by putting the venv's bin on PATH; this is the operator side).
#
# A raw traceback names the module but not WHICH python failed to find it —
# and on a box with several interpreters that is exactly the missing half.
# `$PYTHON` lets a caller pick one deliberately.
PY_BIN="${PYTHON:-python3}"
command -v "$PY_BIN" >/dev/null 2>&1 || {
    echo "publish-enterprise-docs: no usable interpreter: '$PY_BIN' is not on PATH." >&2
    echo "  Set PYTHON=/path/to/python to choose one." >&2
    exit 1
}
MISSING=$("$PY_BIN" - <<'PYEOF'
import importlib.util, sys
# Third-party imports of scripts/lib/confluence_publish.py. Kept in step with
# it by tests/unit/scripts/test_1844_the_publisher_checks_its_interpreter.py,
# which reads that file's imports rather than trusting this list.
for mod in ("markdown",):
    if importlib.util.find_spec(mod) is None:
        print(mod)
PYEOF
) || {
    echo "publish-enterprise-docs: '$PY_BIN' could not be asked about its modules." >&2
    exit 1
}
if [ -n "$MISSING" ]; then
    echo "publish-enterprise-docs: $PY_BIN is missing: $(echo "$MISSING" | tr '\n' ' ')" >&2
    echo "  interpreter: $("$PY_BIN" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "$PY_BIN")" >&2
    echo "  These come from tests/requirements.txt. Either" >&2
    echo "    PYTHON=$REPO_ROOT/tests/.venv/bin/python $0 $*" >&2
    echo "  or install them into this interpreter." >&2
    exit 1
fi

exec "$PY_BIN" "$REPO_ROOT/scripts/lib/confluence_publish.py" "${ARGS[@]}"
