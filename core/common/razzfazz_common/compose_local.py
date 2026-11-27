# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3412: THE rule for a box-local compose overlay, shared by backup and restore.

A box-local overlay is ``compose.local.<name>.yml`` in the stack root. One rule
decides which of them a backup carries and which a restore writes back, so the
two can never disagree (a name the backup archived and the restore refused
used to lose every overlay of the box):

* the name matches :data:`NAME_ERE` — the characters the Settings portal's
  COMPOSE_FILE allow-list accepts in a compose file name (#1226), nothing else;
* it is a regular file (a symlink is never followed) of at most
  :data:`MAX_BYTES` (an overlay is a few lines of YAML).

The shell side (scripts/lib.sh, cli/status.sh) cannot import this module; it
carries :data:`NAME_ERE` as the same literal string, and a test pins that.

Used by the backup manager (``compose-local-collect`` for the backup,
``compose-local-export`` for the restore) and by
scripts/compose_local_restore.py on the host. A file outside the rule is
SKIPPED with its reason, never a reason to drop the others.
"""
from __future__ import annotations

import gzip
import io
import os
import re
import stat
import tarfile
import zlib

#: POSIX ERE, identical in bash (`[[ $n =~ $ERE ]]`) and Python (`re.match`).
NAME_ERE = r"^compose\.local\.[A-Za-z0-9._-]+\.yml$"
_NAME_RE = re.compile(NAME_ERE)
MAX_BYTES = 1024 * 1024
#: what a skip message tells the operator to do
REMEDY = ("rename it to compose.local.<name>.yml with only letters, digits, '.', '_' "
          "and '-' in <name>, at most 1 MiB")


def name_problem(name: str) -> str | None:
    """Why `name` is not a box-local overlay name, or None."""
    if "\n" in name or not _NAME_RE.match(name):
        return "not a compose.local.<name>.yml name the stack accepts"
    return None


def candidates(root: str) -> list[str]:
    """Every entry of `root` that looks like a box-local overlay
    (``compose.local.*.yml``), valid or not — the set a skip is reported for."""
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(n for n in names if n.startswith("compose.local.") and n.endswith(".yml"))


def collect(root: str):
    """The overlays of the stack root `root` a backup carries.

    Returns ``(files, skipped)``: ``{name: bytes}`` and ``[(name, why)]``."""
    files, skipped = {}, []
    for name in candidates(root):
        why = name_problem(name)
        if why is None:
            path = os.path.join(root, name)
            try:
                st = os.lstat(path)
            except OSError as e:
                why = f"cannot be read ({e.strerror})"
            else:
                if stat.S_ISLNK(st.st_mode):
                    why = "a symlink (never followed)"
                elif not stat.S_ISREG(st.st_mode):
                    why = "not a regular file"
                elif st.st_size > MAX_BYTES:
                    why = f"larger than {MAX_BYTES} bytes ({st.st_size})"
                else:
                    try:
                        with open(path, "rb") as fh:
                            files[name] = fh.read(MAX_BYTES + 1)
                    except OSError as e:
                        why = f"cannot be read ({e.strerror})"
                    else:
                        if len(files[name]) > MAX_BYTES:
                            del files[name]
                            why = f"larger than {MAX_BYTES} bytes"
        if why is not None:
            skipped.append((name, why))
    return files, skipped


def write_tar(files: dict, *, gz: bool) -> bytes:
    """`files` as a tar (gzip when `gz`), members mode 600, sorted."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as tar:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(files[name])
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(files[name]))
    return buf.getvalue()


class MalformedArchive(tarfile.TarError):
    """#3436 L-20: the archive cannot be read as a whole — a damaged gzip
    stream (bad checksum, cut short, invalid deflate data) or a damaged tar.
    A ``tarfile.TarError`` so every caller that refuses a malformed tar
    refuses this too."""


def _gunzip(data: bytes) -> bytes:
    """`data` decompressed IN FULL, so the gzip checksum and length trailer
    are checked; plain tar data is returned as it is. tarfile alone stops at
    the end of the tar records and never reads the trailer: an archive with a
    flipped bit or cut by a few bytes was accepted with altered content, and a
    damaged deflate stream raised ``zlib.error``, which no caller handles."""
    if data[:2] != b"\x1f\x8b":
        return data
    try:
        return gzip.decompress(data)
    except (zlib.error, OSError, EOFError) as e:
        raise MalformedArchive(f"damaged gzip stream ({e})") from e


def read_tar(data: bytes):
    """The overlays of a tar (plain or gzip) a restore may write.

    Returns ``(files, skipped)``. Only an entry outside the rule is skipped (a
    path, a link, a bad name, an oversized file); a MALFORMED archive raises
    :class:`MalformedArchive` (a ``tarfile.TarError``) — nothing in it can be
    trusted. A gzip archive is decompressed whole first, so its checksum is
    verified before any entry is read (#3436 L-20)."""
    plain = _gunzip(data)
    try:
        return _read_plain_tar(plain)
    except MalformedArchive:
        raise
    except (tarfile.TarError, zlib.error, OSError, EOFError, ValueError) as e:
        raise MalformedArchive(f"damaged tar ({e})") from e


def _read_plain_tar(data: bytes):
    files, skipped = {}, []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for m in tar.getmembers():
            name = m.name[2:] if m.name.startswith("./") else m.name
            if name in ("", "."):
                continue
            why = None
            if "/" in name:
                why = "a path, not a file of the stack root"
            elif not m.isreg():
                why = "not a regular file"
            else:
                why = name_problem(name)
            if why is None and m.size > MAX_BYTES:
                why = f"larger than {MAX_BYTES} bytes ({m.size})"
            if why is not None:
                skipped.append((m.name, why))
                continue
            fh = tar.extractfile(m)
            files[name] = fh.read() if fh is not None else b""
    return files, skipped
