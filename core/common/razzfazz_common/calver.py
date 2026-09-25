# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""One CalVer comparator for the whole stack (#524).

Release identifiers are `YYYY.MM[-stage[.patch]]` — `2026.08-ga.12`,
`2026.08-rc5`, `2026.08-ga`, and the bare cycle `2026.08`. **String order is not
release order** for any of them:

    sorted(["2026.08-ga.9", "2026.08-ga.12"])[-1]  ->  "2026.08-ga.9"    WRONG
    sorted(["2026.9", "2026.10"])[-1]              ->  "2026.9"          WRONG
    "2026.08-rc5" > "2026.08-ga.1"                 ->  True              WRONG

Three independent places had reached for a plain sort and got it wrong:

* `core/licenses/app.py` — picked the "current version" for a **customer-facing
  licence page**, so a `ga.12` box was told it runs `ga.9` and given that
  release's BSL Change Date. An understated conversion date is a licensing
  statement, not a display nit (#524).
* `core/config/app/blueprints/api.py` — ordered the What's New sections, with a
  comment conceding "may need a fix at rc10+". The stack is at ga.12.
* `tests/unit/migrations/` — compared cycles as strings to decide "released"
  (caught in review of PR #508, fixed there).

`sort -V` is not a substitute either: it treats `-rc5` and `-ga.1` as opaque
suffixes and gets the stage order wrong.

Unparseable input returns **None** rather than a sentinel that sorts somewhere.
A value that quietly sorts newest is how a bad tag becomes "the current release";
callers must decide explicitly what to do with what they cannot read.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

#: `2026.08-ga.12`, `2026.08-rc5`, `2026.08-ga`, `2026.08`, `2026.08-ga.12-usbfix`
_CALVER = re.compile(
    r"^v?(?P<year>\d{4})\.(?P<month>\d{1,2})"
    # `\.?` because the tag scheme is INCONSISTENT: `ga.12` has a dot, `rc5`
    # does not. Requiring the dot made `-rc5` fall through to `extra`, which
    # ranks ABOVE `ga` — so a release candidate outranked the release. Found by
    # running the comparator, not by reading it.
    r"(?:-(?P<stage>[A-Za-z]+)\.?(?P<patch>\d+)?)?"
    r"(?P<extra>-.+)?$"
)

#: Pre-release stages sort BELOW the general release of the same cycle.
#: Anything unrecognised sorts below every known stage rather than above, so a
#: novel stage name cannot leapfrog `ga` and become "the newest release".
_STAGE_RANK = {"dev": -30, "alpha": -20, "beta": -15, "rc": -10, "ga": 0}
_UNKNOWN_STAGE = -100

#: A cycle with no stage (`2026.08`) means the cycle itself. It ranks above every
#: release inside it so `latest()` over a mixed list is still deterministic, and
#: `cycle_of()` exists for callers that only care about the cycle.
_NO_STAGE = 1


def calver_key(version: str) -> Optional[Tuple]:
    """Sortable key for a release identifier, or None if it is not one.

    The key orders by (year, month, stage, patch, suffix). Compare keys, never
    strings::

        max(tags, key=calver_key)          # only if every tag parses
        latest(tags)                       # skips what does not parse
    """
    if not isinstance(version, str):
        return None
    m = _CALVER.match(version.strip())
    if not m:
        return None
    stage = (m.group("stage") or "").lower()
    if not stage:
        rank = _NO_STAGE
    else:
        rank = _STAGE_RANK.get(stage, _UNKNOWN_STAGE)
    return (
        int(m.group("year")),
        int(m.group("month")),
        rank,
        int(m.group("patch") or 0),
        # A trailing variant (`-usbfix`) is a respin OF that release, so it sorts
        # just after it. "" sorts before any non-empty string, which is correct.
        m.group("extra") or "",
    )


def is_calver(version: str) -> bool:
    return calver_key(version) is not None


def cycle_of(version: str) -> Optional[Tuple[int, int]]:
    """`(year, month)` for a release or a bare cycle; None if unparseable."""
    key = calver_key(version)
    return None if key is None else (key[0], key[1])


def latest(versions: Iterable[str]) -> Optional[str]:
    """The newest parseable version, or None when none of them parse.

    Unparseable entries are SKIPPED, not ranked. Returning None on an
    all-unparseable list forces the caller to say what that means rather than
    inheriting an arbitrary answer.
    """
    parsed = [(calver_key(v), v) for v in versions or []]
    usable = [(k, v) for k, v in parsed if k is not None]
    if not usable:
        return None
    return max(usable)[1]


def sort_versions(versions: Iterable[str], *, reverse: bool = False) -> list:
    """Release order. Unparseable entries are kept, ordered after everything that
    parses (or before, when reversed), so nothing silently disappears from a
    listing — the caller sees them, just not in a position that implies a date."""
    items = list(versions or [])
    parseable = sorted((v for v in items if is_calver(v)), key=calver_key)
    rest = sorted(v for v in items if not is_calver(v))
    return (list(reversed(parseable)) + rest) if reverse else (parseable + rest)
