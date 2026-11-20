#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Redact secret values from earlier razzfazz run logs (#2697).

Before #2697, `rzfz init` printed a generated admin password and the run-log
tee kept it in razzfazz-init-*.log (0644). `rzfz upgrade` calls this once per
run: every razzfazz-*.log holding one of the values gets `[redacted]` in its
place, and every run log ends up 0600.

The values come on stdin, one per line (never argv, never printed). Values
shorter than 8 characters are ignored. Output is one line: "<redacted>
<tightened>" (counts only).

    scrub_run_logs.py [--dry-run] [--skip PATH] [--own DIR]... [--sudo-own DIR]... [--shared DIR]...

--own   a log directory that belongs to the operator (RZFZ_LOG_DIR,
        ~/.razzfazz/logs, the sudo user's): tightened to 0700. It must be a
        real directory (not a symlink) owned by the runner or SUDO_UID, else
        it is skipped.
--sudo-own the sudo user's log directory: as --own, but it must resolve to
        a directory owned by SUDO_UID itself (not root), so a ~/.razzfazz
        symlink into a root-owned directory is skipped.
--shared a directory others write too (TMPDIR, /tmp): its mode is never
        touched, and only files this user owns are read or changed.
A world-writable --own directory is treated as shared.

Safety: a log is opened O_NOFOLLOW|O_NONBLOCK and must be the same regular file
lstat() saw (device, inode), so a symlink or a swapped file is skipped. A
rewrite goes through a mkstemp file in the same directory, keeps the owner and
the timestamps (retention sorts by mtime), and replaces the log atomically. A
file that fails is skipped; the rest still run.
"""
from __future__ import annotations

import argparse
import glob
import os
import stat
import sys
import tempfile

REDACTED = b"[redacted]"
MIN_LEN = 8


def may_touch(shared: bool, file_uid: int, euid: int) -> bool:
    """In a shared directory only this user's own files are touched."""
    return not shared or file_uid == euid


def is_shared(dir_mode: int, declared_shared: bool) -> bool:
    return declared_shared or bool(dir_mode & stat.S_IWOTH)


def allowed_owners(euid: int) -> set:
    """Who may own an operator log dir: the runner, and under sudo the user."""
    owners = {euid}
    sudo_uid = os.environ.get("SUDO_UID", "")
    if sudo_uid.isdigit():
        owners.add(int(sudo_uid))
    return owners


def _tighten_dir(path: str, allowed: set) -> None:
    """chmod 0700 through a descriptor opened without following a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        fst = os.fstat(fd)
        if stat.S_ISDIR(fst.st_mode) and fst.st_uid in allowed:
            os.fchmod(fd, 0o700)
    except OSError:
        pass
    finally:
        os.close(fd)


def open_checked(path: str, lst: os.stat_result):
    """Open `path` read-only without following a symlink; return the fd only
    when it is still the regular file `lst` described, else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        fst = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if (not stat.S_ISREG(fst.st_mode) or fst.st_dev != lst.st_dev
            or fst.st_ino != lst.st_ino):
        os.close(fd)
        return None
    return fd


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def _rewrite(rd: str, path: str, fst: os.stat_result, data: bytes, euid: int) -> None:
    tfd, tmp = tempfile.mkstemp(dir=rd, prefix=".razzfazz-scrub-")
    try:
        with os.fdopen(tfd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fchmod(fh.fileno(), 0o600)
            if euid == 0:
                os.fchown(fh.fileno(), fst.st_uid, fst.st_gid)
            os.utime(fh.fileno(), ns=(fst.st_atime_ns, fst.st_mtime_ns))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def scrub(values, own, shared, skip="", dry=False, euid=None, sudo_own=()):
    euid = os.geteuid() if euid is None else euid
    vals = sorted({v for v in values if len(v) >= MIN_LEN}, key=len, reverse=True)
    skip_real = os.path.realpath(skip) if skip else None
    seen, redacted, tightened = set(), 0, 0
    allowed = allowed_owners(euid)
    sudo_uid = os.environ.get("SUDO_UID", "")
    sudo_only = {int(sudo_uid)} if sudo_uid.isdigit() else set()
    dirs = ([(x, False, sudo_only) for x in sudo_own] + [(x, False, allowed) for x in own]
            + [(x, True, None) for x in shared])
    for d, declared_shared, owners in dirs:
        try:
            if not declared_shared:
                # An operator log dir is used only as itself: a symlink (under
                # sudo, a user could point it at /var/log) or a directory of
                # another owner is skipped, never followed or chmod'ed. lstat
                # resolves the parents, so this owner is the RESOLVED dir's: for
                # the sudo user's dir (owners = SUDO_UID only) a ~/.razzfazz
                # symlink into a root-owned directory fails here.
                lst = os.lstat(d)
                if not stat.S_ISDIR(lst.st_mode) or lst.st_uid not in owners:
                    continue
            rd = os.path.realpath(d)
            dst = os.stat(rd)
        except OSError:
            continue
        if rd in seen or not stat.S_ISDIR(dst.st_mode):
            continue
        seen.add(rd)
        shared_dir = is_shared(dst.st_mode, declared_shared)
        if not shared_dir and not dry and stat.S_IMODE(dst.st_mode) & 0o077:
            _tighten_dir(d, owners)
        for f in sorted(glob.glob(os.path.join(glob.escape(rd), "razzfazz-*.log"))):
            try:
                lst = os.lstat(f)
                if not stat.S_ISREG(lst.st_mode):
                    continue
                if skip_real and os.path.realpath(f) == skip_real:
                    continue
                if not may_touch(shared_dir, lst.st_uid, euid):
                    continue
                fd = open_checked(f, lst)
                if fd is None:
                    continue
                try:
                    fst = os.fstat(fd)
                    data = _read_all(fd)
                    new = data
                    for v in vals:
                        new = new.replace(v.encode(), REDACTED)
                    if new != data:
                        redacted += 1
                        if not dry:
                            _rewrite(rd, f, fst, new, euid)
                    elif not dry and stat.S_IMODE(fst.st_mode) & 0o077:
                        os.fchmod(fd, 0o600)
                        tightened += 1
                finally:
                    os.close(fd)
            except OSError:
                continue
    return redacted, tightened


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip", default="")
    ap.add_argument("--own", action="append", default=[])
    ap.add_argument("--shared", action="append", default=[])
    ap.add_argument("--sudo-own", action="append", default=[])
    a = ap.parse_args(argv)
    values = sys.stdin.read().split("\n")
    r, t = scrub(values, a.own, a.shared, a.skip, a.dry_run, sudo_own=a.sudo_own)
    print(r, t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
