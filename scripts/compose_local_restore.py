#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3412: put a backup's box-local compose overlays back into the stack root.

`rzfz backup restore` runs this on the HOST (the backup manager mounts the stack
root read-only) with the tar the manager's `compose-local-export` wrote, BEFORE
compose runs on the restored .env. Usage:

    compose_local_restore.py <tar> <stack root>

Which entries qualify is razzfazz_common.compose_local's rule — the same one
the backup applied. An entry outside it is SKIPPED (named on stderr), the
others are still written; only a malformed tar writes nothing. Each file is
written atomically (temp file + rename, mode 600, the same as the restored
.env), replacing the box's copy of the same name; other files are left alone.
Prints the names written, space-separated; exit 0 ok, 1 malformed or failed.
"""
from __future__ import annotations

import os
import sys
import tarfile
import zlib
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "core", "common"))
from razzfazz_common import compose_local  # noqa: E402


def write_atomic(root: str, name: str, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=root)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(root, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 1
    tar_path, root = argv[1], argv[2]
    try:
        with open(tar_path, "rb") as fh:
            files, skipped = compose_local.read_tar(fh.read())
        for name, why in skipped:
            print(f"compose overlay {name!r} from the backup NOT restored: {why}", file=sys.stderr)
        for name in sorted(files):
            write_atomic(root, name, files[name])
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:  # #3436 L-20
        print(f"compose overlay restore failed: {e}", file=sys.stderr)
        return 1
    print(" ".join(sorted(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
