#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #707 — remove the PREVIOUS job's leftovers from a shared runner workspace.
#
# The `razzfazz-ci` runner is an act_runner **hostexecutor** with a persistent
# working directory (`~/.cache/act/<hash>/hostexecutor`). Every job of every
# branch lands in the same tree. `actions/checkout` updates the files that the
# target ref tracks — it does not delete a file that the PREVIOUS ref tracked
# and this one does not. Such a file survives as UNTRACKED, and the next job
# imports it.
#
# That is not theory. Run 1775 / job 4275 (PR #704) collected the OLD test
# names from the checked-out branch while the imported module answered with the
# #703 branch's behaviour — the skip string `cli/test.sh not found at …` exists
# on #703 and nowhere else. Old tests against new code, in one job, green-ish
# and meaningless.
#
# The dangerous property is not the failure, it is the SHAPE of the failure:
# a mixed workspace does not error out, it silently answers a different
# question. The same class as a shallow clone (#474, #493) — tests stay green
# and stop checking what they claim.
#
# Kept on purpose:
#   tests/.venv  — the bootstrapped test virtualenv. Rebuilding it costs
#                  minutes per job and it is not branch state: `cli/test.sh`
#                  reconciles it against tests/requirements*.txt on every run.
#                  (It must NOT be a tracked symlink — see #761.)
#
# Everything else untracked goes, including __pycache__ and stray *.pyc, which
# `git clean -ffdx` already covers; the explicit sweep is there for the case
# where the tree is not a git repo at all (then clean cannot run and we still
# want the byte-code gone).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

#: Untracked paths that survive the sweep. Keep this list SHORT: every entry is
#: state that crosses job boundaries, which is exactly what #707 is about.
KEEP=(tests/.venv)

say() { printf '[ci-clean] %s\n' "$*"; }

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    exclude_args=()
    for k in "${KEEP[@]}"; do exclude_args+=(-e "$k"); done

    # Report before removing: on a runner this log line is the only evidence
    # that a previous job leaked into this one.
    leftovers="$(git clean -nffdx "${exclude_args[@]}" | head -40 || true)"
    if [ -n "$leftovers" ]; then
        say "removing leftovers from a previous job:"
        printf '%s\n' "$leftovers" | sed 's/^/    /'
    else
        say "workspace already clean."
    fi

    git clean -ffdx "${exclude_args[@]}" >/dev/null
else
    say "not a git work tree — skipping git clean, sweeping byte-code only."
fi

# Byte-code sweep. Redundant after `git clean -ffdx` in a git tree, and the
# whole hygiene otherwise in a non-git one.
find . -path ./tests/.venv -prune -o -type d -name __pycache__ -print0 2>/dev/null \
    | xargs -0 -r rm -rf
find . -path ./tests/.venv -prune -o -type f -name '*.pyc' -print0 2>/dev/null \
    | xargs -0 -r rm -f

say "done."
