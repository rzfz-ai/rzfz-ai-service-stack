# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""A remote worker agent that runs another release line than the master.

A thin worker has no stack checkout, so `rzfz upgrade` on the master never
reaches it; a worker enrolled in September kept its September agent under a
November master and nothing said so (it lacked fixes the master relies on).
The agent already reports its version (stack_version); the master compares it
with its own release and the console and `rzfz status` show the difference.

"Beyond a patch" means a different release line (YYYY.MM): 2026.11-rc4 vs
2026.11-ga.1 is a patch step, 2026.09-rc1 vs 2026.11-rc4 is not. Unknown or
unparseable versions are never reported as skew.
"""
from __future__ import annotations

import re
from typing import Optional

_LINE = re.compile(r"^v?(\d{4})\.(\d{1,2})(?:[.\-+_]|$)")

#: `git describe` output: <last tag>-<commits since>-g<commit>. Its tag is the
#: last tag REACHABLE, not the release the tree belongs to (an interim 2026.11
#: build describes as `2026.09-ga.4-1773-g526a3987f`), so it says nothing about
#: the release line (#2897).
_DESCRIBE = re.compile(r"-\d+-g[0-9a-f]{7,40}$")

#: What the operator runs on the worker (it remembers its master).
UPDATE_COMMAND = "rzfz node-upgrade"


def release_line(version: Optional[str]) -> Optional[tuple[int, int]]:
    m = _LINE.match((version or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def agent_version_skew(worker_version: Optional[str], master_version: Optional[str]) -> Optional[dict]:
    """None when the lines agree or either side is unknown; else the facts.

    A `git describe` string (`<last tag>-<n>-g<commit>`) names the last tag
    reachable, not a release line, so it is unknown here and never skew — the
    master's own in-stack agent used to be flagged that way (#2897). Agents
    built from a release tree report `<VERSION>+g<commit>` instead, which is
    compared by line like any other version."""
    if _DESCRIBE.search((worker_version or "").strip()):
        return None
    w, m = release_line(worker_version), release_line(master_version)
    if w is None or m is None or w == m:
        return None
    return {
        "level": "warn",
        "worker": (worker_version or "").strip(),
        "master": (master_version or "").strip(),
        "update": UPDATE_COMMAND,
    }
