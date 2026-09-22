#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The ONE ordering for razzfazz.ai version strings (#1334).

`cli/upgrade.sh` carried three copies of this parser: the shell-facing
`compare_versions`, the comparator inside `migrate_env`'s Python heredoc — the
one that actually SELECTS the manifest blocks — and that heredoc's `sort_key`,
whose own comment already declared it a copy. Three copies drift, and this one
had drifted in a way nobody could see from any single call site.

## What was wrong

Both regexes only knew `YYYY.MM-rcN[.M]` and `YYYY.MM-ga[.N]`. Everything else
fell into a "split on dots and strip non-digits" fallback, which turns the six
milestone-era entries in `config/migrations/env-changes.json` into this::

    2026.04-m004             -> (2026, 4004)
    2026.04-M010             -> (2026, 4010)
    2026.04-M011             -> (2026, 4011)
    2026-04.M008             -> (202604, 8)
    2026.04-version-updates  -> (2026, 4)
    2026.09-rc1              -> (2026, 9, 0, 1, 0)

`4011 > 9` at the second position, so `2026.04-M011` sorts ABOVE every real
release. As a *target* that is merely dead. As the *installed* value it turns
everything off: measured against the real 81-block manifest with target
``2026.09-rc1``, an installed version of ``2026.04-M011`` selects **0 blocks**
while ``2026.04-GA`` selects 68. Such a box gets "No .env migrations needed"
and RC 0 on every upgrade, for ever — the #1310 signature from the other side.

## What this module does instead

A 5-tuple ``(year, month, rank, n1, n2)`` with an explicit rank so pre-release
forms cannot collide::

    -1  milestone   YYYY.MM-M<N>     (the 2026.04 development cycle)
     0  rc          YYYY.MM-rcN[.M]
     1  ga          YYYY.MM-ga[.N]

and it **raises** on anything it cannot order. Guessing is what produced the
defect; a comparator that answers confidently about a string it does not
understand is worse than one that says so.

The single legacy name without a number is mapped explicitly rather than
guessed — see ``LEGACY_ORDER``.
"""
from __future__ import annotations

import re
import sys

#: rank component of the 5-tuple: milestones precede rc, rc precedes ga.
RANK_MILESTONE, RANK_RC, RANK_GA = -1, 0, 1

_RC = re.compile(r'^(\d{4})[.\-](\d{2})[.\-]rc(\d+)(?:\.(\d+))?$')
_GA = re.compile(r'^(\d{4})[.\-](\d{2})[.\-][Gg][Aa](?:\.(\d+))?$')
#: M023-era milestones: `2026.04-M011`, `2026-04.M008`, lower case too.
_MILESTONE = re.compile(r'^(\d{4})[.\-](\d{2})[.\-][Mm](\d+)$')

#: Named historical entries that carry no number at all. There is exactly ONE
#: in the manifest, dated 2026-04-10 — the same day as `2026.04-m004` — so it
#: is ordered immediately after it. Written down rather than guessed: a new
#: entry in this shape must be a decision, not a fallback.
LEGACY_ORDER = {
    "2026.04-version-updates": (2026, 4, RANK_MILESTONE, 4, 1),
}


class UnorderableVersion(ValueError):
    """Raised for a version string this module refuses to guess about."""


def parse(version: str) -> tuple:
    """Order key for `version`. Raises `UnorderableVersion` if unknown.

    Deliberately NOT a fallback: the split-on-dots guess is what made
    `2026.04-M011` newer than `2026.09-rc1`.
    """
    v = str(version).strip().lstrip("v")
    if v in LEGACY_ORDER:
        return LEGACY_ORDER[v]
    m = _RC.match(v)
    if m:
        return (int(m.group(1)), int(m.group(2)), RANK_RC,
                int(m.group(3)), int(m.group(4) or 0))
    m = _GA.match(v)
    if m:
        return (int(m.group(1)), int(m.group(2)), RANK_GA,
                int(m.group(3) or 0), 0)
    m = _MILESTONE.match(v)
    if m:
        return (int(m.group(1)), int(m.group(2)), RANK_MILESTONE,
                int(m.group(3)), 0)
    # Semver X.Y.Z — still ordered, because the shipped VERSION file has used
    # it historically and a tag may. Anything else is refused.
    if re.match(r'^\d+(\.\d+){1,3}$', v):
        parts = [int(p) for p in v.split(".")]
        return tuple(parts + [0] * (5 - len(parts)))[:5]
    raise UnorderableVersion(
        f"cannot order the version {version!r} — it is neither CalVer "
        "(YYYY.MM-rcN[.M] / -ga[.N] / -M<N>) nor semver, and guessing is how "
        "2026.04-M011 came to sort above 2026.09-rc1 (#1334)")


def compare(v1: str, v2: str) -> int:
    """-1 / 0 / 1. Raises `UnorderableVersion` like `parse`."""
    p1, p2 = parse(v1), parse(v2)
    return -1 if p1 < p2 else (0 if p1 == p2 else 1)


def orderable(version: str) -> bool:
    try:
        parse(version)
    except UnorderableVersion:
        return False
    return True


def _main(argv) -> int:
    """Shell contract, unchanged from cli/upgrade.sh::compare_versions:
    exit 0 = v1 < v2, 1 = equal, 2 = v1 > v2. NEW: 3 = cannot order, with the
    reason on stderr — the old code silently guessed instead."""
    if len(argv) != 2:
        print("usage: version_order.py <v1> <v2>", file=sys.stderr)
        return 64
    try:
        c = compare(argv[0], argv[1])
    except UnorderableVersion as exc:
        print(f"version_order: {exc}", file=sys.stderr)
        return 3
    return {-1: 0, 0: 1, 1: 2}[c]


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
