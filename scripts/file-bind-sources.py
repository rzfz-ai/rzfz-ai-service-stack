#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1595 — which bind-mount sources of this project have to be FILES.

WHY THIS EXISTS. A missing bind source is not an error to Docker: it creates it,
as an empty DIRECTORY. Every check of the form "does the path exist?" is happy
afterwards, and the failure surfaces one layer later somewhere unrelated —

    ssl.create_default_context(cafile=cafile)
    IsADirectoryError: [Errno 21] Is a directory

— which cost an hour on 2026-09-07, in three places at once. `lib.sh`'s
`ensure_oidc_ca_superset` already knows the repair for ONE file (#1080); this
lists the whole set so the repair can be applied to all of them before
`compose up`, and so a review can see which of them nothing creates.

A source is FILE-SHAPED when the container-side target has a filename with a
suffix, or the source itself does. That is deliberately syntactic: the compose
files are the only thing available before the stack runs, and a rule that
needed the running stack could not be used before `up` — which is the one
moment it has to work.

Prints one path per line, resolved against the project root, deduplicated and
sorted. Exit code is always 0: a caller before `compose up` must not be stopped
by this enumerator failing to parse something.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: a bind line in a compose file: `- ./src:/dst` or `- ../src:/dst:ro`
_BIND = re.compile(r"^\s*-\s+(?P<src>[.~/][^:\s]*):(?P<dst>/[^:\s]*)(?::(?P<mode>[a-z,]+))?\s*$")
#: a path that names a file rather than a directory
_FILE_SHAPED = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def file_shaped(src: str, dst: str) -> bool:
    """Is this bind a FILE bind?

    The container side decides first — it is the side that says what the
    process will open. `../.env:/scripts/dot-env` has no suffix on either side
    and is still a file, so a known set of extensionless targets is named
    explicitly rather than guessed: guessing wrong in this direction would put
    a directory on the list and make the repair delete an operator's folder.
    """
    base = os.path.basename(dst)
    if base.startswith("dot-env") or base in {".env", ".env.dify"}:
        return True
    if _FILE_SHAPED.search(base):
        return True
    return bool(_FILE_SHAPED.search(os.path.basename(src)))


def sources(compose_files, root: Path) -> list[str]:
    out: set[str] = set()
    for f in compose_files:
        p = Path(f)
        if not p.is_absolute():
            p = root / p
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            m = _BIND.match(line)
            if not m:
                continue
            src, dst = m.group("src"), m.group("dst")
            if not file_shaped(src, dst):
                continue
            # compose resolves a relative source against the directory of the
            # file that names it (#1595 case 3 / the overlay trap in #1447).
            resolved = os.path.normpath(str((p.parent / src)))
            # Only paths INSIDE the project. `/var/run/docker.sock` is a socket
            # the host owns; a repair that removed it would be a far worse
            # defect than the one this list exists to prevent.
            try:
                Path(resolved).resolve().relative_to(root.resolve())
            except ValueError:
                continue
            out.add(resolved)
    return sorted(out)


def _default_compose_files(root: Path) -> list[str]:
    env = os.environ.get("COMPOSE_FILE")
    if env:
        sep = os.environ.get("COMPOSE_PATH_SEPARATOR", ":")
        return [x for x in env.split(sep) if x]
    return [str(root / "core" / "compose.yml")]


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    for path in sources(_default_compose_files(root), root):
        print(path)
    return 0


if __name__ == "__main__":       # pragma: no cover - CLI
    try:
        sys.exit(main(sys.argv))
    except Exception:            # never stop a caller that runs before compose up
        sys.exit(0)
