#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# lint-blueprint-groups.sh — static group-binding lint (BSB-17).
# ==============================================================================
#
# Background
# ----------
# Commit 97f7e2c0 (2026-05-12) fixed 4 group-name typos in
# `core/start-portal/manifest.yaml`:
#   required_group: "razzfazz.ai Workflow Users"        # WRONG
#                  → "razzfazz.ai Workflow Automation Users"  # canonical
#   required_group: "razzfazz.ai Gitea Users"           # WRONG
#                  → "razzfazz.ai Git Users"            # canonical
#   required_group: "razzfazz.ai LLM Users"             # WRONG
#                  → "razzfazz.ai LLM Management Users" # canonical
# (and one more.) Each of those silently passed blueprint apply, portal
# load, and config render. The user only noticed when the SSO group
# binding refused to materialise (BSB-06's runtime gate caught it on
# the box that ran post-install — but only there).
#
# This linter closes the remaining gap by walking every reference
# site for an Authentik-group name and asserting that each name is
# defined by the blueprint set:
#
# Reference sites (DETECTED HERE, statically):
#   - core/start-portal/manifest.yaml          (`required_group:`)
#   - core/Authentik/apply-policy-bindings.py  (`ensure_binding(slug, name, …)`)
#   - core/config/profiles.yaml                (`user_group:` — null tolerated)
#
# Canonical group set (DEFINED BY):
#   - core/Authentik/blueprints/base/*.yaml    (model: authentik_core.group)
#
# This is the static counterpart to scripts/post-install-group-lint.sh
# (BSB-06), which checks the runtime Authentik DB after install.
# Static linting catches typos at commit time, before blueprint apply
# even runs.
#
# Wired into:
#   - scripts/prepare-release.sh (release gate, Check 15)
#   - operator may also install as a git pre-commit hook
#
# Usage:
#   bash scripts/lint-blueprint-groups.sh
#   bash scripts/lint-blueprint-groups.sh --help
#
# Exit codes:
#   0 — every reference resolves to a blueprint-defined group
#   1 — at least one reference is unknown (typo / missing definition)
#
# READ-ONLY: parses YAML/Python text, never modifies files, never
# touches a live Authentik instance.
# ==============================================================================

set -eo pipefail

usage() {
    cat <<'EOF'
lint-blueprint-groups.sh — static group-binding lint (BSB-17)

Walks every literal `razzfazz.ai *` group reference in
  core/start-portal/manifest.yaml          (required_group:)
  core/Authentik/apply-policy-bindings.py  (ensure_binding(...))
  core/config/profiles.yaml                (user_group:)
and asserts each one is defined in
  core/Authentik/blueprints/base/*.yaml    (model: authentik_core.group)

Catches the 97f7e2c0 typo class at commit time, before deploy.

Usage:
  scripts/lint-blueprint-groups.sh        # run the lint
  scripts/lint-blueprint-groups.sh --help

Exit codes:
  0   every reference resolves
  1   one or more references unknown
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "")        ;;
    *)         echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
esac

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

BLUEPRINT_DIR="core/Authentik/blueprints/base"
MANIFEST_FILE="core/start-portal/manifest.yaml"
BINDINGS_FILE="core/Authentik/apply-policy-bindings.py"
PROFILES_FILE="core/config/profiles.yaml"

# If none of these exist (extracted package without portal/blueprints,
# fresh tmp dir, …) the linter has nothing to check — pass.
if [ ! -d "$BLUEPRINT_DIR" ] && [ ! -f "$MANIFEST_FILE" ] \
   && [ ! -f "$BINDINGS_FILE" ] && [ ! -f "$PROFILES_FILE" ]; then
    echo "lint-blueprint-groups: no blueprint or reference files found — nothing to check"
    exit 0
fi

# Hand the heavy lifting to python — we need YAML walking for
# blueprints + manifest + profiles, and a regex for the python
# bindings file. Pure-bash would be ~3× the LOC and brittle (cf.
# BSB-01-BUG-01 / `tr` dropped chars).
python3 - "$BLUEPRINT_DIR" "$MANIFEST_FILE" "$BINDINGS_FILE" "$PROFILES_FILE" <<'PYEOF'
import re
import sys
from pathlib import Path

BLUEPRINT_DIR = Path(sys.argv[1])
MANIFEST_FILE = Path(sys.argv[2])
BINDINGS_FILE = Path(sys.argv[3])
PROFILES_FILE = Path(sys.argv[4])

# A "razzfazz.ai *" group is a name starting with `razzfazz.ai ` and a
# capital. We deliberately scope the lint to this prefix — the operator's
# convention. Other authentik groups (e.g. `authentik Admins`) are
# created by upstream and are not in scope.
GROUP_PREFIX_RE = re.compile(r'razzfazz\.ai [A-Z][^"\']*')


def _safe_yaml_load(path: Path):
    """Load YAML if possible. We deliberately avoid PyYAML (not always
    available) and fall back to text-walking. The references we care
    about are simple `key: "razzfazz.ai ..."` scalars — text is enough.
    Returns the file's text content."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---- 1. Build the canonical group set from blueprints --------------------
canonical: set[str] = set()
if BLUEPRINT_DIR.is_dir():
    # Walk every YAML in the blueprint dir. We treat any
    # `name: "razzfazz.ai ..."` line that appears under a `model:
    # authentik_core.group` block as a group definition. Simple
    # state-machine over the text — robust against arbitrary
    # YAML nesting in this repo's blueprint style.
    for bp in sorted(BLUEPRINT_DIR.glob("*.yaml")):
        text = _safe_yaml_load(bp)
        in_group_block = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- model:"):
                in_group_block = "authentik_core.group" in stripped
                continue
            # Top-level `model:` (no leading `- `) also opens a block.
            if stripped.startswith("model:"):
                in_group_block = "authentik_core.group" in stripped
                continue
            if in_group_block:
                # Match `name: "razzfazz.ai ..."` (quotes optional).
                m = re.match(r'name:\s*["\']?(razzfazz\.ai [^"\']+)["\']?\s*$',
                             stripped)
                if m:
                    canonical.add(m.group(1).strip())


# ---- 2. Walk reference sites ---------------------------------------------
# Each reference is a tuple (file, lineno, group_name, context).
references: list[tuple[str, int, str, str]] = []


def _scan_yaml_key(path: Path, key: str, context_label: str) -> None:
    """Find every `<key>: "razzfazz.ai ..."` in a YAML file. Tolerates
    `<key>: null`, `<key>: ~`, missing values, and quoted/unquoted vals."""
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    pat = re.compile(
        rf'^\s*{re.escape(key)}\s*:\s*["\']?(razzfazz\.ai [^"\'#\n]+?)["\']?\s*(?:#.*)?$'
    )
    for n, line in enumerate(text.splitlines(), 1):
        # Skip comments cleanly.
        if re.match(r'^\s*#', line):
            continue
        m = pat.match(line)
        if m:
            references.append(
                (str(path), n, m.group(1).strip(), context_label)
            )


_scan_yaml_key(MANIFEST_FILE, "required_group", "manifest.yaml required_group")
_scan_yaml_key(PROFILES_FILE, "user_group", "profiles.yaml user_group")

# Bindings file: regex for ensure_binding("slug", "razzfazz.ai ...", ...).
if BINDINGS_FILE.is_file():
    text = BINDINGS_FILE.read_text(encoding="utf-8")
    pat = re.compile(
        r'ensure_binding\(\s*"[^"]+"\s*,\s*"(razzfazz\.ai [^"]+)"'
    )
    for n, line in enumerate(text.splitlines(), 1):
        if re.match(r'^\s*#', line):
            continue
        for m in pat.finditer(line):
            references.append(
                (str(BINDINGS_FILE), n, m.group(1).strip(),
                 "apply-policy-bindings.py ensure_binding")
            )


# ---- 3. Compare ----------------------------------------------------------
unknown: list[tuple[str, int, str, str]] = []
for path, n, name, ctx in references:
    if name not in canonical:
        unknown.append((path, n, name, ctx))

# Diagnostics
total_refs = len(references)
total_groups = len(canonical)


def _short(p: str) -> str:
    """Render path relative to cwd if possible."""
    try:
        return str(Path(p).resolve().relative_to(Path.cwd()))
    except ValueError:
        return p


if unknown:
    print(
        f"lint-blueprint-groups: FAIL — {len(unknown)} unknown group "
        f"reference(s) across {total_refs} ref(s) checked against "
        f"{total_groups} blueprint-defined group(s):",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for path, n, name, ctx in unknown:
        print(f"  {_short(path)}:{n}: unknown group "
              f"\"{name}\" (in {ctx})", file=sys.stderr)
    print("", file=sys.stderr)
    print("Each of these references a group name that is NOT defined in",
          file=sys.stderr)
    print(f"  {BLUEPRINT_DIR}/*.yaml", file=sys.stderr)
    print("", file=sys.stderr)
    print("Either:", file=sys.stderr)
    print("  a) the reference is a typo — fix the spelling, or",
          file=sys.stderr)
    print("  b) the canonical group is genuinely missing — add it to",
          file=sys.stderr)
    print("     core/Authentik/blueprints/base/02-groups.yaml (or the",
          file=sys.stderr)
    print("     matching per-module blueprint, e.g. 19-paperless-ngx.yaml).",
          file=sys.stderr)
    print("", file=sys.stderr)
    print("This is the 97f7e2c0 regression class. BSB-06's runtime",
          file=sys.stderr)
    print("post-install gate would catch it after deploy; this static",
          file=sys.stderr)
    print("lint catches it at commit time.", file=sys.stderr)
    sys.exit(1)

print(f"lint-blueprint-groups: OK ({total_refs} reference(s) resolved "
      f"against {total_groups} blueprint-defined group(s))")
sys.exit(0)
PYEOF
