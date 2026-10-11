# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1189 / #1224 — detect and explain a stale single-file `.env` bind mount.

Two containers mount `.env` and `.env.dify` as single-file binds (rw) on top
of the read-only repo mount (BSB-03, core/compose.yml): `razzfazz-config`
(#1189) and `razzfazz-backup-management` (#1224). A file bind is pinned to
the file's INODE when the container is created. Any host-side tool that
rewrites the file atomically — `sed -i`, an editor with backup-write,
tmp+rename — produces a NEW inode, and from then on the container either
* still sees (and writes) the orphaned OLD inode through the bind, or
* — when the kernel dropped the bind — sees the read-only repo mount
  showing through at that path,
and every write fails with a bare `[Errno 30] Read-only file system` (or
EBUSY) — or, worse, SUCCEEDS into the orphan the host never sees. The
stack's own writers have been inode-preserving since 2026.08-ga.6, so this
is defence in depth against ad-hoc editors.

What a container CAN do without any mount change:
* tell the states apart — `inspect_env_file` compares the inode the
  directory entry reports (readdir → the host's CURRENT file, because the
  parent directory comes from the dir-bind) with `lstat()` through the
  mount table (→ the pinned old inode). Both sides have lstat semantics
  (`DirEntry.inode()` is readdir's `d_ino`), so a symlinked `.env` on a
  healthy box must not read as stale (rev-B, #1189). #1877 adds the probe
  that decides the OTHER variant: an actual `O_WRONLY|O_APPEND` open. On
  0.91 the rw bind was declared but absent from /proc/mounts, the inode
  matched the host's, `access(W_OK)` said writable — and every write got
  EROFS. `access()` answers a question about permission bits; a mount that
  is not there is not a permission bit, so the only honest check is the
  one the writer itself performs;
* turn the write failure into an actionable message naming the one-time
  recovery: recreate THAT container so the binds are re-established on the
  current inodes. `service` names it; the default is the Portal, so the
  #1189 call sites are unchanged.

Replacing the file binds with a rw directory bind of the repo root would
make both immune, but undoes BSB-03 (R-DEF-03 / R-COMP-13, the ISO 27001
A.8.4 conformance claim). That is an operator decision (BSB-03-DEC-01) —
not made here.
"""
from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from typing import Callable, Optional

ENV_FILES = ('.env', '.env.dify')

#: The container whose bind went stale — names the recovery. Default keeps the
#: #1189 (Portal) behaviour byte-for-byte.
DEFAULT_SERVICE = 'razzfazz-config'
SERVICE_LABELS = {
    'razzfazz-config': "the Configuration Portal",
    'razzfazz-backup-management': "the Backup Management UI",
}


def recreate_cmd(service: str = DEFAULT_SERVICE) -> str:
    """The one-time recovery. `restart` is NOT enough — binds are resolved
    at container CREATE time. Run in the stack directory on the host."""
    return f'docker compose up -d --force-recreate --no-deps {service}'


#: Back-compat for the Portal's call sites and tests.
RECREATE_CMD = recreate_cmd(DEFAULT_SERVICE)

_STALE_ERRNOS = (errno.EROFS, errno.EBUSY)


class StaleEnvMountError(OSError):
    """A write to `.env`/`.env.dify` failed because the container's single-file
    bind mount no longer matches the host file (#1189 Portal, #1224 backup
    management)."""

    def __init__(self, message: str, path: str, errno_: int):
        super().__init__(errno_, message, path)
        self.message = message

    def __str__(self) -> str:
        return self.message


def _explain(path: str, strerror: str, service: str = DEFAULT_SERVICE) -> str:
    name = os.path.basename(path) or path
    label = SERVICE_LABELS.get(service, service)
    return (
        f"Cannot write {name}: {label}'s private read-write bind for this file is "
        f"not in effect, so the read-only stack mount shows through ({strerror}). "
        f"Two causes produce this, and the recovery is the same for both: a "
        f"host-side tool replaced {name} with a new inode (the bind then points at "
        f"an orphan), or the bind was never established when the container was last "
        f"created — measured on a box in 2026.09 with the inode intact and the mount "
        f"simply absent from /proc/mounts (#1877). The stack itself is fine and the "
        f"host file is intact. Recreate the {service} container once so the bind is "
        f"resolved against the current file: `{recreate_cmd(service)}` (in the stack "
        f"directory on the host). See #1189."
    )


def explain_stale(path: str, strerror: str, service: str = DEFAULT_SERVICE) -> str:
    """Public spelling of `_explain` for callers that already know the write
    failed (a subprocess wrote the file — `openssl -out` — and reported EROFS)."""
    return _explain(path, strerror, service)


def explain_write_failure(path: str, exc: BaseException,
                          service: str = DEFAULT_SERVICE) -> Optional[str]:
    """The actionable message for an EROFS/EBUSY write failure on `path`;
    ``None`` for any other error (those are not this class)."""
    code = getattr(exc, 'errno', None)
    if code in _STALE_ERRNOS:
        return _explain(path, getattr(exc, 'strerror', None) or os.strerror(code), service)
    return None


@contextmanager
def env_write_guard(path: str, service: str = DEFAULT_SERVICE):
    """Wrap a truncate-in-place write of an env file: EROFS/EBUSY become a
    `StaleEnvMountError` carrying the recovery; everything else passes."""
    try:
        yield
    except OSError as exc:
        message = explain_write_failure(path, exc, service)
        if message is None:
            raise
        raise StaleEnvMountError(message, path, exc.errno) from exc


def _write_open_probe(path: str, open_: Callable = os.open):
    """Ask the question the WRITER asks: can this file be opened for writing?

    `O_WRONLY | O_APPEND`, closed immediately — no byte is written, nothing is
    truncated, and O_APPEND means even a concurrent writer is unaffected.

    This exists because the two cheap signals lie in the case that actually
    took a box down (#1877): on 0.91 the container's inode matched the host's
    and `access(W_OK)` reported writable, while `open(".env", "a")` returned
    EROFS — the rw bind was DECLARED (`docker inspect`) but absent from
    /proc/mounts, so the file resolved through the read-only stack mount.
    `access()` answers a question about permission bits; a mount that is not
    there is not a permission bit.

    Returns `(ok, errno)`: `(True, None)` writable, `(False, <errno>)` not,
    `(None, None)` when the probe itself could not decide.
    """
    try:
        fd = open_(path, os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        return False, exc.errno
    except Exception:
        return None, None
    try:
        os.close(fd)
    except OSError:
        pass
    return True, None


def inspect_env_file(
    path: str,
    *,
    stat: Callable = os.lstat,
    scandir: Callable = os.scandir,
    access: Callable = os.access,
    open_: Callable = os.open,
) -> dict:
    """Is the Portal's view of `path` the host's current file?

    Returns ``{path, name, exists, writable, inode_match, stale, reason}``.
    Never raises — a probe that cannot decide reports "not stale" and
    leaves the write guard to catch the real failure.

    `stat` defaults to `os.lstat`, NOT `os.stat`: the other side of the
    inode comparison, `DirEntry.inode()`, is readdir's `d_ino` and has
    lstat semantics (the entry itself, never a symlink's target). With
    `os.stat` a symlinked `.env` on a healthy box compares the target's
    inode with the link's and reads as stale — a false positive with a
    recreate advice that fixes nothing. At a real bind mount point the
    file is not a symlink, so lstat and stat agree and the stale
    detection is unchanged. `access(W_OK)` keeps following the link:
    what matters is whether the file we would write is writable."""
    name = os.path.basename(path)
    info = {'path': path, 'name': name, 'exists': False, 'writable': None,
            'write_open': None, 'write_errno': None,
            'inode_match': None, 'stale': False, 'reason': None}
    try:
        st = stat(path)
    except OSError:
        return info
    info['exists'] = True
    try:
        info['writable'] = bool(access(path, os.W_OK))
    except OSError:
        info['writable'] = None
    info['write_open'], info['write_errno'] = _write_open_probe(path, open_)
    dirent_ino = None
    try:
        for entry in scandir(os.path.dirname(path) or '.'):
            if entry.name == name:
                dirent_ino = entry.inode()
                break
    except OSError:
        dirent_ino = None
    info['inode_match'] = True if dirent_ino is None else (dirent_ino == st.st_ino)
    # Order matters. The inode comparison goes FIRST because it is the only
    # signal for the one failure the write probe cannot see: a bind pinned to
    # an orphaned inode is perfectly writable, and the write SUCCEEDS into a
    # file the host will never read again. A silent wrong write outranks a
    # loud refused one.
    if info['inode_match'] is False:
        info['stale'] = True
        info['reason'] = (
            f"the bind mount still points at the old inode ({st.st_ino}) while the "
            f"host directory now holds inode {dirent_ino} — a host-side tool replaced "
            f"{name}")
    elif info['write_open'] is False and info['write_errno'] in _STALE_ERRNOS:
        # #1877: the case both cheap signals missed. Authoritative, because it
        # is the same call the writer makes.
        info['stale'] = True
        info['reason'] = (
            f"{name} cannot be opened for writing ({os.strerror(info['write_errno'])}) "
            f"— the container's read-write bind for this file is not in effect and the "
            f"read-only stack mount shows through, even though the inode matches the "
            f"host's")
    elif info['writable'] is False:
        # Kept as a last resort for a box where the open probe could not run
        # (an injected/patched opener, an exotic filesystem). It is no longer
        # the primary signal: on 0.91 it reported writable while every write
        # failed.
        info['stale'] = True
        info['reason'] = (
            f"{name} is read-only inside the container — the single-file bind is "
            f"gone and the read-only repo mount shows through")
    return info


def inspect_env_mounts(stack_root: str, service: str = DEFAULT_SERVICE,
                       inspect_file: Optional[Callable] = None) -> dict:
    """Both env files at once, for the page banner and status surfaces.
    `inspect_file` is the per-file probe (default `inspect_env_file`) — an
    injectable seam like the probe's own `stat`/`scandir`/`access`."""
    probe = inspect_file or inspect_env_file
    files = [probe(os.path.join(stack_root, name)) for name in ENV_FILES]
    return {
        'stale': any(f['stale'] for f in files),
        'files': files,
        'service': service,
        'recreate': recreate_cmd(service),
    }
