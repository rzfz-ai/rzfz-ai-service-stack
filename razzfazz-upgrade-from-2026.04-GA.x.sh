#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz.ai Stack — GA.1-era Upgrade Bootstrap
# ==============================================================================
# Solves the "reexec gap" that affects stacks still on v2026.04-GA.x.
#
# The 2026.04-GA.x upgrade entry point cannot re-exec itself mid-run: when it
# does `git checkout <target-tag>` it replaces the script on disk, but the
# bash process that launched keeps executing the OLD script from memory.
# Any function added in a newer release (e.g. a pre-`restart_stack` DB fixup,
# or a breaking env transform beyond what migrations/env-changes.json can
# express) is unreachable during the very upgrade that introduces it.
#
# This bootstrap does the swap BEFORE bash loads the target's upgrade code:
#   1. Fetch the target tag from origin (or use a --bundle for offline).
#   2. Seed the unified `rzfz` dispatcher + the cli/ tree (plus the
#      scripts/lib*.sh helpers they source) from the target tag onto the
#      working tree — the rest of the stack is checked out by the normal
#      flow later.
#   3. Exec `rzfz upgrade` with whatever args the caller passed; rzfz runs
#      the freshly-seeded cli/upgrade.sh, so every function in the target
#      tag's code is live from the first line.
#
# Post-reorg (#26/#34, 2026.07+) there is NO standalone root upgrade script —
# the upgrade entry point IS `rzfz upgrade` → cli/upgrade.sh (the former root
# upgrade script is now a thin backward-compat shim under legacy/). This
# bootstrap therefore targets reorg-era releases: it requires the target tag
# to ship `rzfz`. The 2026.05/06 gap is inert anyway — every upgrade-time
# action there lived in compose.yml (see docs/authentik-upgrade.md).
#
# SCOPE (operator decision B, 2026-07-05) — despite the "2026.04-GA.x" filename,
# this bootstrap is VERSION-GENERIC: it is the ONE supported path for EVERY
# pre-reorg release — 2026.04 AND 2026.05 AND 2026.06-ga.x — crossing INTO 2026.07.
# The plain `./razzfazz-upgrade.sh --target v2026.07-…` breaks across the reorg for
# all of them (the re-exec'd root script is gone → stale in-place paths → failure),
# so route every pre-reorg → 2026.07 upgrade through here. Post-2026.07 → newer is
# the plain `rzfz upgrade` again. (A rename to e.g.
# `razzfazz-upgrade-across-2026.07-reorg.sh` + a compat symlink would read clearer —
# operator call; see .gsd/reports/2026.07-ga-rollout-plan.md "decision B".)
#
# Usage:
#   ./razzfazz-upgrade-from-2026.04-GA.x.sh --target v2026.07-ga
#   ./razzfazz-upgrade-from-2026.04-GA.x.sh --target v2026.07-ga --skip-backup
#   ./razzfazz-upgrade-from-2026.04-GA.x.sh --target v2026.07-ga --bundle /tmp/upgrade.bundle
#
# All arguments except --bundle are forwarded verbatim to `rzfz upgrade`.
#
# IMPORTANT — run this from the ROOT of your checkout. The bootstrap acts on the
# directory it physically lives in (it needs compose.yml AND .git right beside
# it), NOT your current shell directory. Copy/run it inside the repo (typically
# ~/razzfazz-ai-service-stack) — never from your home directory (~/).
# ==============================================================================

set -eo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TARGET_TAG=""
BUNDLE_FILE=""
FORWARD_ARGS=()

# Extract --target for our own use, and --bundle (our extension).
# Everything else (including --target itself) gets forwarded to `rzfz upgrade`.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            TARGET_TAG="$2"
            FORWARD_ARGS+=("$1" "$2")
            shift 2
            ;;
        --bundle)
            BUNDLE_FILE="$2"
            shift 2
            ;;
        --help|-h)
            sed -n '2,45p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            FORWARD_ARGS+=("$1")
            shift
            ;;
    esac
done

if [ -z "$TARGET_TAG" ]; then
    echo -e "${RED}error:${NC} --target <tag> is required."
    echo "       e.g. $0 --target v2026.07-ga"
    exit 1
fi

if [ ! -f "compose.yml" ]; then
    echo -e "${RED}error:${NC} compose.yml not found in $SCRIPT_DIR."
    echo "       This bootstrap operates on its OWN directory ($SCRIPT_DIR), not \$PWD."
    echo "       Run it from the ROOT of your razzfazz-ai-service-stack checkout —"
    echo "       it must sit beside compose.yml + .git, NOT in your home dir (~/)."
    exit 1
fi

if [ ! -d ".git" ]; then
    echo -e "${RED}error:${NC} $SCRIPT_DIR is not a git repository."
    echo "       For offline upgrades use \`rzfz upgrade --package <file>\` directly;"
    echo "       the package bundles the new code, so the bootstrap is not needed."
    exit 1
fi

echo -e "${BLUE}[bootstrap] Target tag: ${TARGET_TAG}${NC}"

# Step 1: ensure the target tag exists locally. Prefer --bundle when supplied
# (air-gapped / no-credential environments), fall back to origin.
if [ -n "$BUNDLE_FILE" ]; then
    if [ ! -f "$BUNDLE_FILE" ]; then
        echo -e "${RED}error:${NC} bundle file not found: $BUNDLE_FILE"
        exit 1
    fi
    echo -e "${CYAN}[bootstrap] Seeding tag from bundle: $BUNDLE_FILE${NC}"
    # Annotated tags must be written under refs/tags/ — the unqualified
    # `tag:tag` form falls back to refs/heads/ and git refuses to write a
    # tag-object to a branch ref ("trying to write non-commit object").
    # If a stale local tag/branch with the same name exists, force-replace it.
    git tag -d "${TARGET_TAG}" 2>/dev/null || true
    git branch -D "${TARGET_TAG}" 2>/dev/null || true
    git fetch --force "$BUNDLE_FILE" "refs/tags/${TARGET_TAG}:refs/tags/${TARGET_TAG}" 2>&1 || {
        echo -e "${RED}error:${NC} could not read tag ${TARGET_TAG} from bundle."
        exit 1
    }
elif ! git rev-parse -q --verify "refs/tags/${TARGET_TAG}" >/dev/null 2>&1; then
    echo -e "${CYAN}[bootstrap] Fetching tag ${TARGET_TAG} from origin...${NC}"
    if ! git fetch --tags origin 2>&1; then
        echo -e "${RED}error:${NC} git fetch failed. Options:"
        echo "         - check the 'origin' remote URL (git remote -v)"
        echo "         - use --bundle <path> with a git bundle copied in out-of-band"
        echo "         - for offline upgrades, use \`rzfz upgrade --package <file>\`"
        exit 1
    fi
    git rev-parse -q --verify "refs/tags/${TARGET_TAG}" >/dev/null 2>&1 || {
        echo -e "${RED}error:${NC} tag ${TARGET_TAG} not present on origin after fetch."
        exit 1
    }
else
    echo -e "${CYAN}[bootstrap] Tag ${TARGET_TAG} already present locally.${NC}"
fi

# Step 2: seed the parts of the target tag the upgrade handoff chain needs
# BEFORE the target's own `git checkout <target-tag>` (code_update_git) lands
# the full tree. We deliberately do NOT checkout the full tree here — keeping
# the tree at the source version means backup/rollback still sees the
# pre-upgrade state if anything goes wrong before restart_stack.
#
# The handoff chain at bootstrap time (before that checkout) is:
#   rzfz upgrade  →  rzfz (dispatcher)  →  cli/upgrade.sh
# and cli/upgrade.sh at startup sources scripts/lib.sh + scripts/lib-journal.sh.
# So the seed set is: scripts/lib*.sh, rzfz, and the cli/ tree. env-snapshot.sh
# is sourced later (inside migrate_env, AFTER the checkout) so it needs no seed.

# rc6.7 #76 + follow-up: seed EVERY scripts/lib*.sh the target's cli/upgrade.sh
# sources at startup — not just lib.sh. The M026 refactor split the operator
# scripts' shared helpers across multiple lib files (e.g. lib.sh + lib-journal.sh,
# the upgrade-journal helpers); a 2026.04-ga checkout has none of them, so
# without this the handoff dies on the first `source scripts/lib-*.sh` line
# ("No such file or directory"). Enumerate them from the target tag so any
# future split is covered automatically. Idempotent; show-into-tempfile +
# bash-syntax-check before installing.
mkdir -p scripts
for _libpath in $(git ls-tree -r --name-only "${TARGET_TAG}" scripts/ 2>/dev/null | grep -E '^scripts/lib.*\.sh$'); do
    _base=$(basename "$_libpath")
    echo -e "${CYAN}[bootstrap] Seeding ${_libpath} from ${TARGET_TAG}...${NC}"
    if ! git show "${TARGET_TAG}:${_libpath}" > "scripts/${_base}.new"; then
        echo -e "${RED}error:${NC} could not read ${_libpath} at tag ${TARGET_TAG}."
        rm -f "scripts/${_base}.new"
        exit 1
    fi
    if ! bash -n "scripts/${_base}.new"; then
        echo -e "${RED}error:${NC} extracted ${_libpath} failed syntax check. Aborting."
        rm -f "scripts/${_base}.new"
        exit 1
    fi
    mv -f "scripts/${_base}.new" "scripts/${_base}"
done

# 2026.07 reorg (#26/#34): seed the unified `rzfz` dispatcher + the cli/ tree.
# Post-reorg, the upgrade entry point IS `rzfz upgrade` → cli/upgrade.sh; there
# is no standalone root upgrade script to seed anymore (the root shim moved to
# legacy/ and is not on the handoff path). A 2026.04-ga checkout has NEITHER
# `rzfz` NOR cli/, so we MUST seed them here or the `exec ./rzfz upgrade` handoff
# below dies instantly with ".../rzfz: No such file or directory" — a
# 2026.04-ga.x → 2026.07 big-bang BLOCKER (verified on 0.91).
#
# This seed set only has to carry the rzfz → cli/upgrade.sh chain as far as
# cli/upgrade.sh's own `git checkout <target-tag>` (code_update_git), after
# which the full target tree lands and supersedes every seed. code_update_git
# runs `git stash push -u` before its `git checkout`, which cleanly clears
# these untracked seeds, so seeding the tree is safe.
#
# BOTH rzfz and the seeded cli/*.sh MUST be chmod +x: the handoff `exec`s rzfz,
# and rzfz `exec`s its target after an `[[ -x "$target" ]]` gate. We enumerate
# the whole cli/ *.sh tree from the target tag — mirroring the lib*.sh loop's
# "cover any future split automatically" rationale — even though only
# cli/upgrade.sh is on the upgrade path today.
#
# This bootstrap requires a target that ships `rzfz` (reorg-era, 2026.07+). A
# pre-reorg target (v2026.05/06) has no rzfz/cli and cannot be handed off to via
# `rzfz upgrade`; fail early with an actionable message rather than dying on the
# exec below. In practice the only supported big-bang path is 2026.04-ga → the
# current GA, which is always reorg-era.
if ! git rev-parse -q --verify "${TARGET_TAG}:rzfz" >/dev/null 2>&1; then
    echo -e "${RED}error:${NC} target tag ${TARGET_TAG} does not ship the 'rzfz' CLI."
    echo "       This bootstrap hands off via 'rzfz upgrade' and therefore requires a"
    echo "       reorg-era target (2026.07+). Pick a current GA tag as --target."
    exit 1
fi

echo -e "${CYAN}[bootstrap] Seeding rzfz from ${TARGET_TAG}...${NC}"
if ! git show "${TARGET_TAG}:rzfz" > rzfz.new; then
    echo -e "${RED}error:${NC} could not read rzfz at tag ${TARGET_TAG}."
    rm -f rzfz.new
    exit 1
fi
if ! bash -n rzfz.new; then
    echo -e "${RED}error:${NC} extracted rzfz failed syntax check. Aborting."
    rm -f rzfz.new
    exit 1
fi
mv -f rzfz.new rzfz
chmod +x rzfz

for _clipath in $(git ls-tree -r --name-only "${TARGET_TAG}" cli/ 2>/dev/null | grep -E '^cli/.*\.sh$'); do
    mkdir -p "$(dirname "$_clipath")"
    echo -e "${CYAN}[bootstrap] Seeding ${_clipath} from ${TARGET_TAG}...${NC}"
    if ! git show "${TARGET_TAG}:${_clipath}" > "${_clipath}.new"; then
        echo -e "${RED}error:${NC} could not read ${_clipath} at tag ${TARGET_TAG}."
        rm -f "${_clipath}.new"
        exit 1
    fi
    if ! bash -n "${_clipath}.new"; then
        echo -e "${RED}error:${NC} extracted ${_clipath} failed syntax check. Aborting."
        rm -f "${_clipath}.new"
        exit 1
    fi
    mv -f "${_clipath}.new" "${_clipath}"
    chmod +x "${_clipath}"
done

echo -e "${GREEN}[bootstrap] rzfz + cli/ seeded from ${TARGET_TAG} — handing off.${NC}"
echo

# Step 3: remove the bootstrap from the working tree before handing off
# (rc6.3 fix). On a 2026.04-ga checkout the bootstrap was obtained out-of-band
# (e.g. `git show <tag>:razzfazz-upgrade-from-2026.04-GA.x.sh > ...`) and is
# untracked. The target tag HAS this file tracked, so the upcoming
# `git checkout <target-tag>` inside cli/upgrade.sh (reached via `rzfz upgrade`)
# would abort with "untracked working tree files would be overwritten by
# checkout". Bash has already loaded the bootstrap source into memory, so
# removing it from disk is harmless for this run; the checkout below will
# materialise the file back into the working tree from the target tag, ready
# for the next big-bang upgrade.
rm -f "${BASH_SOURCE[0]}"

# Step 4: hand off. exec replaces this process; rzfz runs the freshly-seeded
# cli/upgrade.sh with bash loading it fresh, so every function defined in the
# target tag's code is live from the first line.
exec ./rzfz upgrade "${FORWARD_ARGS[@]}"
