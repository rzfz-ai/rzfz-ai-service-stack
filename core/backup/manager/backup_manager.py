# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
import io
import json
import os
import posixpath
import stat
import logging
import argparse
import re
import sys
import time
import tarfile
import tempfile
import docker
import shutil
import subprocess
import hashlib
from datetime import datetime

from razzfazz_common.env_mount import explain_stale, inspect_env_file, recreate_cmd

# Setup Logging
# Logs to the container's /var/log by default. The FileHandler is fail-safe and
# the path is overridable (BACKUP_MANAGER_LOG_FILE) so the module stays
# importable off-box — e.g. unit tests running as a non-root user where
# /var/log isn't writable. A hardcoded FileHandler there raises PermissionError
# at import time and breaks test collection (the CI runner surfaced this).
LOG_FILE = os.environ.get("BACKUP_MANAGER_LOG_FILE", "/var/log/backup_manager.log")
_log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _log_handlers.insert(0, logging.FileHandler(LOG_FILE))
except OSError:
    pass  # path not writable (unit-test / non-container import) — stdout only
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)

BACKUP_DIR = "/archive"

# rc6.7 #6: backup files come in two flavours since rc2 enabled GPG
# encryption (F-A2). Pre-rc2 boxes have plain `.tar.gz`; post-rc2 boxes
# have `.tar.gz.gpg`. Both can coexist on a box that upgraded across the
# encryption-enable boundary. Every listing / parsing site here recognises
# both via this canonical pattern.
BACKUP_SUFFIXES = ('.tar.gz.gpg', '.tar.gz')


def is_backup_file(filename):
    """Return True if `filename` looks like a backup archive (encrypted or not)."""
    return any(filename.endswith(sfx) for sfx in BACKUP_SUFFIXES)


def backup_basename(filename):
    """Strip the backup suffix, return the canonical base name. Used for
    parsing the embedded timestamp out of `backup-YYYY-MM-DD-HHMM.tar.gz[.gpg]`
    and similar."""
    for sfx in BACKUP_SUFFIXES:
        if filename.endswith(sfx):
            return filename[: -len(sfx)]
    return filename


def is_encrypted_backup(filename):
    """True for `.tar.gz.gpg` (F-A2 encrypted form)."""
    return filename.endswith('.tar.gz.gpg')


def _read_passphrase_from_env_file(env_path):
    """BSB-04 / R-DEF-04: re-read BACKUP_ENCRYPTION_PASSWORD from the
    bind-mounted host .env at every call. Returns the (possibly empty)
    passphrase string, or '' if the file is missing.

    Mirrors the parser in decrypt_backup_to_temp (and the shell wrapper
    razzfazz-backup-wrapper.sh): strips trailing ` # comment`, surrounding
    quotes, and leading/trailing whitespace. Doesn't `source` the file —
    operator-edited .env carries spaces and shell metachars (memory:
    feedback_dotenv_no_source.md). First-match wins.
    """
    import re
    if not env_path or not os.path.isfile(env_path):
        return ''
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith('BACKUP_ENCRYPTION_PASSWORD='):
                    raw = line.split('=', 1)[1]
                    raw = re.sub(r'\s+#.*$', '', raw).rstrip('\n').strip().strip('"').strip("'")
                    return raw
    except OSError:
        return ''
    return ''


def decrypt_backup_to_temp(encrypted_path, env_path='/stack/.env'):
    """Decrypt a `.tar.gz.gpg` backup to a tempfile and return its path.
    Caller is responsible for removing the temp file when done.

    rc6.7 #7: encrypted backups (F-A2, rc2+) cannot be opened with
    `tarfile.open(..., "r:gz")` directly — gzip can't read the GPG
    payload. We shell out to `gpg --batch --decrypt` with the passphrase
    from BACKUP_ENCRYPTION_PASSWORD in `.env`, write the decrypted
    .tar.gz to /tmp under a securely-named file, and return the path so
    the existing tar-handling code can keep using `tarfile.open(...,
    "r:gz")`.

    The temp file lives in /tmp (inside the container, not on the host
    bind-mount) so it doesn't accidentally end up persisted alongside
    the encrypted source. Caller must `os.remove(...)` after extraction
    (or wrap in try/finally — see restore_full).
    """
    import tempfile
    # Read BACKUP_ENCRYPTION_PASSWORD from the bind-mounted .env. The
    # container has /stack/.env mounted from the host. Don't `source`
    # the file (operator-edited; can carry shell metachars). Just grep.
    passphrase = ''
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith('BACKUP_ENCRYPTION_PASSWORD='):
                    raw = line.split('=', 1)[1]
                    # Mirror rc6.7 #3 read_env_value semantics: strip
                    # trailing ` # comment` and surrounding whitespace +
                    # quotes.
                    import re
                    raw = re.sub(r'\s+#.*$', '', raw).strip().strip('"').strip("'")
                    passphrase = raw
                    break
    except Exception as e:
        raise RuntimeError(f"Could not read BACKUP_ENCRYPTION_PASSWORD from {env_path}: {e}")
    if not passphrase:
        raise RuntimeError(
            f"BACKUP_ENCRYPTION_PASSWORD is empty in {env_path}; cannot "
            f"decrypt {os.path.basename(encrypted_path)}. The backup was "
            f"encrypted at create-time, but the passphrase needed to "
            f"decrypt it is no longer present in .env. Restore the "
            f"value (it should equal AUTHENTIK_BOOTSTRAP_PASSWORD on "
            f"a default install) and retry."
        )

    # Write to a tempfile that lives in /tmp (not bind-mounted to host).
    # delete=False because subprocess writes into the path; we close+pass
    # to gpg, then clean up explicitly in the caller's finally clause.
    fd, tmp_path = tempfile.mkstemp(prefix='razzfazz-restore-', suffix='.tar.gz')
    os.close(fd)

    logger.info(f"Decrypting {os.path.basename(encrypted_path)} → {tmp_path} ...")
    try:
        # --batch + --pinentry-mode loopback so gpg never asks an interactive
        # tty. --passphrase-fd reads from stdin so the passphrase never
        # appears in /proc/<pid>/cmdline.
        proc = subprocess.run(
            [
                'gpg', '--batch', '--yes', '--quiet',
                '--pinentry-mode', 'loopback',
                '--passphrase-fd', '0',
                '--decrypt', '--output', tmp_path,
                encrypted_path,
            ],
            input=passphrase.encode('utf-8'),
            capture_output=True,
            timeout=600,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode('utf-8', errors='replace')
            raise RuntimeError(
                f"gpg --decrypt failed (exit {proc.returncode}) on "
                f"{os.path.basename(encrypted_path)}: {stderr.strip()}"
            )
    except Exception:
        # On any failure clean up the temp file before propagating.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    logger.info(f"Decryption complete; temp tarball at {tmp_path} "
                f"({os.path.getsize(tmp_path) / (1024**3):.2f} GiB)")
    return tmp_path
RESTORE_ROOT = "/restore_targets"
# #2465: the full-restore extraction staging dir. Env-overridable like
# PARTIAL_RESTORE_DIR so the api-tier test can run restore_full end to end
# without touching the shared /tmp/restore_extract.
RESTORE_EXTRACT_DIR = os.environ.get("RESTORE_EXTRACT_DIR", "/tmp/restore_extract")
#: #1224 — see app.py ENV_MOUNT_SERVICE; the manager runs in the same container.
ENV_MOUNT_SERVICE = "razzfazz-backup-management"
# Partial-restore staging dir. Module-level + env-overridable (like RESTORE_ROOT)
# so unit tests can point it at an isolated tmp dir instead of the shared
# /tmp/partial_restore — a hardcoded shared path is fragile to leftovers from a
# crashed/other-user run (rmtree PermissionError; surfaced on the CI runner).
PARTIAL_RESTORE_DIR = os.environ.get("PARTIAL_RESTORE_DIR", "/tmp/partial_restore")
DOCKER_SOCK = os.environ.get('DOCKER_SOCK', 'unix://var/run/docker.sock')
# #2544 (fix round 1): the helper that writes an agent volume back runs GNU tar,
# the same as the one that took it (core/backup/pre-backup.sh §7): the archive
# may hold SPARSE members (`tar -S`), and only GNU tar restores those as sparse
# files — BusyBox tar (alpine) would write every hole out as zeros. The image
# is the one the box's `postgres` container runs (pgvector/pgvector, Debian,
# GNU tar), resolved at restore time, so it is present offline too and its tag
# comes from core/compose.yml, not from here. BACKUP_AGENT_TAR_IMAGE overrides.
AGENT_TAR_IMAGE_ENV = "BACKUP_AGENT_TAR_IMAGE"


def agent_tar_image():
    """The GNU-tar helper image for agent volumes, or None (with the reason
    logged). Checked: `tar --version` must say GNU tar."""
    img = os.environ.get(AGENT_TAR_IMAGE_ENV, "").strip()
    try:
        if not img:
            r = subprocess.run(["docker", "inspect", "--type", "container", "--format",
                                "{{.Config.Image}}", "postgres"],
                               capture_output=True, text=True, check=False)
            img = r.stdout.strip() if r.returncode == 0 else ""
        if not img:
            logger.error("#2544: could not read the postgres container's image — no GNU "
                         f"tar helper for the agent volumes (set {AGENT_TAR_IMAGE_ENV}).")
            return None
        v = subprocess.run(["docker", "run", "--rm", "--entrypoint", "tar", img, "--version"],
                           capture_output=True, text=True, check=False)
    except OSError as e:   # FileNotFoundError: no docker CLI — PARTIAL, not a crash
        logger.error(f"#2544: could not run docker to find the GNU tar helper: {e}")
        return None
    first = ((v.stdout or v.stderr or "").splitlines() or [""])[0]
    if v.returncode != 0 or "GNU tar" not in first:
        logger.error(f"#2544: the helper image {img} has no GNU tar ({first!r}) — a "
                     "sparse agent volume cannot be restored as sparse.")
        return None
    return img

# #279: sub-directory (inside databases/ in the archive, i.e. the db-dumps
# volume) holding one `pg_dump -Fc` per database, alongside the existing
# `pg_dumpall` cluster dumps (postgres_core.sql / postgres_komodo.sql).
# Written by BackupManager.dump_per_database_backups() before every full
# backup so restore_single_database() can pg_restore ONE database directly
# instead of replaying (or text-slicing) the whole cluster dump.
PER_DB_DUMP_SUBDIR = "per-db"

# #279: a database name that can't match this is refused outright — it is
# never interpolated into a shell string, but the check also fences off
# lookalike/ambiguous names before any I/O happens at all (fail-safe: this
# moves real data).
# \A…\Z, not ^…$: `$` also matches before a trailing newline, so
# "postgres\n" would pass (#2465 fix round 2).
_SAFE_DB_NAME_RE = re.compile(r'\A[A-Za-z0-9_]+\Z')

try:
    client = docker.DockerClient(base_url=DOCKER_SOCK)
except docker.errors.DockerException:
    # No reachable docker socket (e.g. unit-test import off-box / non-docker
    # user). The backup container always has it; tests mock `client`.
    client = None


def safe_extract_tar(tar, path):
    """Safely extract tar archive with comprehensive validation (CVE-2007-4559).

    F-051: Validates path traversal, rejects device files and out-of-scope symlinks,
    and checks available disk space before extraction.
    """
    abs_path = os.path.abspath(path)

    # F-051: Check archive size vs available disk space before extracting
    total_uncompressed = sum(m.size for m in tar.getmembers() if m.isfile())
    stat = os.statvfs(path)
    available_bytes = stat.f_bavail * stat.f_frsize
    # Require at least 10% headroom beyond the uncompressed size
    required = int(total_uncompressed * 1.1)
    if required > available_bytes:
        raise Exception(
            f"Insufficient disk space for extraction: need {required} bytes, "
            f"have {available_bytes} bytes available in {path}"
        )

    for member in tar.getmembers():
        # rc6.7 #6: the backup archive stores members under ABSOLUTE paths
        # (`/backup/<vol>/...`) — offen's backup tars `/backup` without
        # stripping the leading slash. os.path.join(path, "/backup/...")
        # discards `path` (POSIX join semantics for absolute second args),
        # so member_path would resolve to `/backup/...` and fail the
        # traversal check below, aborting EVERY restore. Normalise absolute
        # member (and link) names to be relative to the extraction root
        # BEFORE the traversal check AND before extractall. We mutate
        # member.name in place so tar.extractall() honours the sanitised,
        # relative name and lands content under <path>/backup/... (which is
        # exactly where restore_full's downstream source_root reads from).
        # This is still safe: stripping a leading separator can only move a
        # member INTO the extraction root, never out of it; the `..`,
        # device-file, and out-of-scope-symlink checks below remain intact
        # and now run against the normalised, relative paths (F-051).
        if os.path.isabs(member.name):
            member.name = member.name.lstrip("/")
        # #125 (symlink-target corruption): do NOT lstrip member.linkname for
        # SYMLINKS. linkname is the link's TARGET (data content), not an
        # extraction path. The authentik-media volume legitimately contains
        # `media -> /media` (an absolute, container-internal symlink resolved
        # at runtime inside authentik). Stripping the leading slash rewrote it
        # to `media -> media` — a self-referential loop — so the
        # authentik-media-migrator's `mkdir -p /data/media` died with
        # "Symbolic link loop", exit 1, blocking the post-restore
        # `compose up` (it's a service_completed_successfully dependency).
        # Symlink targets are preserved verbatim and validated below by the
        # F-051 escape check. Only HARD links (islnk) reference an in-archive
        # path that the leading-slash normalisation must track, so we strip
        # those.
        if member.linkname and os.path.isabs(member.linkname) and member.islnk():
            member.linkname = member.linkname.lstrip("/")

        member_path = os.path.abspath(os.path.join(path, member.name))

        # Reject path traversal
        if not member_path.startswith(abs_path + os.sep) and member_path != abs_path:
            raise Exception(f"Attempted path traversal in tar archive: {member.name}")

        # F-051: Reject device files (block, char, fifo)
        if member.isdev() or member.isblk() or member.ischr():
            raise Exception(f"Refusing to extract device file from archive: {member.name}")

        # F-051: Reject symlinks/hardlinks that point outside the extraction
        # directory. For an ABSOLUTE symlink target (e.g. `media -> /media`),
        # `os.path.join(dirname, "/media")` discards dirname and yields the
        # host-absolute `/media`, which would spuriously fail this check even
        # though the link is a legitimate container-internal target that is
        # never followed during extraction. tarfile creates the symlink as a
        # plain symlink node (it does not resolve/follow the target on
        # extract), so an absolute symlink target is safe to recreate
        # verbatim. We therefore only escape-check links whose target is
        # RELATIVE (those genuinely resolve within / could escape the
        # extraction tree). Hard links always reference an in-archive path
        # and are checked.
        if member.issym() or member.islnk():
            if not os.path.isabs(member.linkname) or member.islnk():
                link_target = os.path.abspath(os.path.join(os.path.dirname(member_path), member.linkname))
                if not link_target.startswith(abs_path + os.sep) and link_target != abs_path:
                    raise Exception(
                        f"Symlink escapes extraction directory: {member.name} -> {member.linkname}"
                    )

    # #2542: as root, tarfile restores each member's owner — by NAME unless
    # numeric_owner, i.e. through THIS image's /etc/passwd, which knows none
    # of the service users (cognee's 1000, a de-rooted service's uid). The
    # archive's numbers are the truth. `fully_trusted` is today's behaviour
    # stated explicitly (the vetting above is ours); a newer Python's default
    # `data` filter would drop the owners and refuse `media -> /media`.
    kw = {"numeric_owner": True}
    if hasattr(tarfile, "fully_trusted_filter"):
        kw["filter"] = "fully_trusted"
    tar.extractall(path, **kw)


def archive_owners(members, prefix):
    """#2542: {relative path: (uid, gid, mode, is_symlink)} for every member
    below `prefix/` — the ownership a restored volume must carry, read from the
    ARCHIVE (never from the staging copy or the target). Member names are the
    normalised, relative ones safe_extract_tar leaves behind. The entry for
    `prefix` itself (the volume root) is keyed "".
    """
    owners = {}
    pre = prefix.rstrip("/") + "/"
    for m in members:
        name = m.name.lstrip("/").rstrip("/")
        if name == pre[:-1]:
            rel = ""
        elif name.startswith(pre):
            rel = name[len(pre):]
        else:
            continue
        if m.isfile() or m.isdir() or m.issym() or m.islnk():
            owners[rel] = (m.uid, m.gid, m.mode & 0o7777, m.issym())
    return owners


def archive_owners_by_volume(members, root="backup"):
    """#2542 (fix round 1): archive_owners for EVERY volume in one pass —
    {volume: {relative path: (uid, gid, mode, is_symlink)}}. A full restore
    used to rescan all members once per volume (O(volumes x members)); a
    backup holds hundreds of thousands of members."""
    by_vol = {}
    pre = root.rstrip("/") + "/"
    for m in members:
        name = m.name.lstrip("/").rstrip("/")
        if not name.startswith(pre):
            continue
        vol, _, rel = name[len(pre):].partition("/")
        if vol and (m.isfile() or m.isdir() or m.issym() or m.islnk()):
            by_vol.setdefault(vol, {})[rel] = (m.uid, m.gid, m.mode & 0o7777, m.issym())
    return by_vol


def apply_archive_ownership(dst_root, owners, label):
    """#2542: give every restored entry below `dst_root` the uid/gid (and mode)
    it has in the archive.

    `shutil.copytree` copies with copy2: modes and times, never uid/gid. The
    manager runs as root, so every restored file of every volume came out
    root-owned, and a non-root service (cognee, uid 1000: "Cannot open file
    …/cognee_graph_kuzu: Permission denied") failed the bring-up.

    * `os.lchown`, so a symlink is changed itself and never followed;
    * the mode is set AFTER the chown (chown clears setuid/setgid), from the
      archive, for everything but symlinks;
    * the volume root (the mountpoint, key "") is left alone: docker created
      it from the image on this box, which is the owner the service expects.

    Raises RuntimeError naming the entries it could not set, so the caller
    counts the volume as failed — a restore that leaves files with the wrong
    owner is not a restore. The manager runs as root; a non-root process
    (a developer's test run) cannot chown at all, and is told so instead.
    """
    if os.geteuid() != 0:
        logger.warning(f"#2542: {label}: not running as root — the archive's "
                       "owners are NOT restored (the files belong to this user).")
        return
    failed = []
    for rel, (uid, gid, mode, is_link) in sorted(owners.items()):
        if not rel:
            continue
        path = os.path.join(dst_root, rel)
        if not os.path.lexists(path):
            continue
        try:
            os.lchown(path, uid, gid)
            if not is_link:
                os.chmod(path, mode)
        except OSError as e:
            failed.append(f"{rel} ({e.strerror})")
    if failed:
        raise RuntimeError(
            f"#2542: {label}: could not restore the archive's owner/mode of "
            f"{len(failed)} entr{'y' if len(failed) == 1 else 'ies'}: "
            + ", ".join(failed[:5]) + (" …" if len(failed) > 5 else ""))


# #2464 fix round 1: a full restore whose databases and .env came back but
# whose certs/ or box-local state did not. restore_full returns this (not
# True, not False); main() maps it to exit code 3 — NOT 2, which argparse and
# the interpreter use for usage errors (fix round 2). Compare with ==, never
# test its truth value.
RESTORE_PARTIAL = "partial"
EXIT_RESTORE_PARTIAL = 3


# #2464: the box-local archive (`core/backup/pre-backup.sh`: `tar czf - -C
# /scripts checksums.db overlay`) carries exactly these two names. They are
# the only things the restore writes; everything else in the archive is
# logged and left out.
BOX_LOCAL_DB_MEMBER = "checksums.db"
BOX_LOCAL_OVERLAY = "overlay"


class UnsafeArchiveError(Exception):
    """#2464: an archive member that must not be extracted anywhere."""


def _within(rel, root):
    """True if the normalised relative path `rel` is `root` or below it."""
    return rel == root or rel.startswith(root + "/")


def _box_local_members(tar):
    """#2464: vet every member of the box-local archive; return the ones to
    extract. Raises UnsafeArchiveError for the whole archive on the first
    member that is unsafe (absolute, `..`, a device/fifo, a link out of its
    scope) — a backup that carries one is not trusted for the rest either.
    Members outside `checksums.db` / `overlay/**` are logged and skipped."""
    keep = []
    links = set()
    for m in tar.getmembers():
        name = m.name
        if name.startswith("/") or os.path.isabs(name):
            raise UnsafeArchiveError(f"absolute path in box-local archive: {name!r}")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if ".." in parts:
            raise UnsafeArchiveError(f"'..' in box-local archive member: {name!r}")
        if not parts:
            continue  # the archive root itself (`.`)
        if not (m.isreg() or m.isdir() or m.issym() or m.islnk()):
            raise UnsafeArchiveError(
                f"device/fifo/special file in box-local archive: {name!r}")
        rel = "/".join(parts)
        # Nothing is extracted THROUGH a link: a member below a symlink member
        # would be written wherever that link points.
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in links:
                raise UnsafeArchiveError(
                    f"member below a symlink in box-local archive: {name!r}")
        if m.issym():
            target = m.linkname
            if not target or target.startswith("/"):
                raise UnsafeArchiveError(
                    f"symlink with an absolute target in box-local archive: "
                    f"{name!r} -> {target!r}")
            # Normalised (as the `data` filter does): `d/..` cannot smuggle a
            # climb past a sibling link. Every link's parent is a real
            # directory (rule above), so the lexical check below is the real
            # one; the realpath pass after extraction double-checks it.
            target = posixpath.normpath(target)
            m.linkname = target
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
            if resolved.startswith("../") or resolved == ".." or \
                    not _within(resolved, BOX_LOCAL_OVERLAY):
                raise UnsafeArchiveError(
                    f"symlink leaves overlay/ in box-local archive: {name!r} -> {target!r}")
            links.add(rel)
        if m.islnk():
            target = posixpath.normpath(m.linkname.lstrip("/")) if m.linkname else ""
            if not target or target.startswith("../") or target == ".." or \
                    m.linkname.startswith("/") or not _within(target, BOX_LOCAL_OVERLAY):
                raise UnsafeArchiveError(
                    f"hardlink leaves overlay/ in box-local archive: "
                    f"{name!r} -> {m.linkname!r}")
            m.linkname = target
        if rel == BOX_LOCAL_DB_MEMBER:
            if not m.isreg():
                raise UnsafeArchiveError(
                    f"{BOX_LOCAL_DB_MEMBER} in box-local archive is not a regular file")
        elif not _within(rel, BOX_LOCAL_OVERLAY):
            logger.warning("box-local archive: ignoring %r — only %s and %s/ are "
                           "restored (#2464)", name, BOX_LOCAL_DB_MEMBER, BOX_LOCAL_OVERLAY)
            continue
        m.name = rel
        # No setuid/setgid/sticky, filter or not (the `data` filter strips
        # them too; this layer must hold without it).
        if m.mode is not None:
            m.mode &= 0o777
        keep.append(m)
    return keep


def extract_box_local_archive(data, dest):
    """#2464: extract the decrypted box-local tar.gz bytes into `dest` (a fresh
    temporary directory — NEVER /stack). Returns the vetted member names.

    Belt and braces: the manual vetting above, then tarfile's `data` filter
    where this Python has it (3.11.4+, the container's python:3.11-alpine),
    then a realpath check of every extracted symlink — a chain like
    `d -> .`, `x -> d/..` passes a per-link lexical check and still leaves
    overlay/ once resolved."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = _box_local_members(tar)
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(dest, members=members, **kwargs)
    overlay_root = os.path.realpath(os.path.join(dest, BOX_LOCAL_OVERLAY))
    for dirpath, dirnames, filenames in os.walk(os.path.join(dest, BOX_LOCAL_OVERLAY)):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                real = os.path.realpath(p)
                if real != overlay_root and not real.startswith(overlay_root + os.sep):
                    raise UnsafeArchiveError(
                        f"symlink resolves outside overlay/: "
                        f"{os.path.relpath(p, dest)} -> {os.readlink(p)}")
    return [m.name for m in members]


SQLITE_HEADER = b"SQLite format 3\x00"


def _is_sqlite_file(path):
    """#2464: True if `path` starts with the SQLite 3 file header."""
    try:
        with open(path, "rb") as f:
            return f.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def _is_nonempty_file(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size > 0


def write_file_in_place(src, target):
    """#2464/#1224: put `src`'s bytes into the EXISTING file `target` without
    replacing it — open, truncate, write, fsync. `target` is a single-file bind
    mount (`../.checksums.db:/stack/.checksums.db:rw`); a rename or a new file
    would detach it from the host's file (or fail on the :ro parent). No
    O_CREAT: a missing target means the mount is missing, and that is an
    error, not something to paper over. O_NOFOLLOW: never write through a
    symlink."""
    fd = os.open(target, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{target} is not a regular file")
        os.ftruncate(fd, 0)
        with open(src, "rb") as fsrc:
            while True:
                chunk = fsrc.read(1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_entry(path):
    """Remove `path` without following it: a symlink or file is unlinked, a
    real directory removed with rmtree (which never follows links inside)."""
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def merge_tree(src, dst):
    """#2464 fix round 1: restore `src` (the backup's overlay/) INTO the existing
    directory `dst` (the rw mount /stack/overlay) with #1621's merge semantics:
    OVERWRITE-AND-KEEP-EXTRAS.

    * An entry the backup has is written. If the box has an entry of the same
      name that cannot simply be descended into (a file, a symlink — even one
      pointing at a directory — or a directory where the backup has a file), it
      is removed FIRST, without following it, then the backup's entry is
      written. A real directory on both sides is kept and merged into.
    * An entry only the box has STAYS. Every backup carries `overlay/` (the
      bind exists on every box and sync-enterprise-overlay.sh mkdir -p's it),
      so a backup taken before the Enterprise payload was staged would, under
      mirror semantics, wipe docs/, security-run/, sbom/ on an air-gapped box
      that cannot re-sync them. Staging is additive (sync-enterprise-
      overlay.sh:66-68); so is the restore.

    Ownership and modes: the `data` tar filter drops the archive's uid/gid, so
    the staged tree is owned by the restoring user (root in the container).
    Every entry this function CREATES is chowned (as root) to the pre-restore
    owner of `dst` — the operator who runs sync-enterprise-overlay.sh must be
    able to write it afterwards. `dst` itself, the mount root, is never
    chowned or chmod'ed; nor are box directories that are merged into. Modes
    are normalised: directories 0755, files 0755 if the backup's copy is
    executable else 0644; symlinks are recreated verbatim (vetted to stay
    inside overlay/)."""
    dst_st = os.lstat(dst)
    if not stat.S_ISDIR(dst_st.st_mode):
        raise OSError(f"{dst} is not a directory (is the rw mount missing?)")
    owner = (dst_st.st_uid, dst_st.st_gid)
    as_root = hasattr(os, "geteuid") and os.geteuid() == 0

    def _own(d):
        if as_root:
            os.lchown(d, *owner)

    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        out = dst if rel == "." else os.path.join(dst, rel)
        for n in list(dirnames) + filenames:
            s = os.path.join(dirpath, n)
            d = os.path.join(out, n)
            s_st = os.lstat(s)
            try:
                d_st = os.lstat(d)
            except FileNotFoundError:
                d_st = None
            if stat.S_ISDIR(s_st.st_mode):
                if d_st is not None and stat.S_ISDIR(d_st.st_mode):
                    continue  # a real box directory: merge into it
                if d_st is not None:
                    _remove_entry(d)
                os.mkdir(d)
                os.chmod(d, 0o755)
                _own(d)
                continue
            if d_st is not None:
                _remove_entry(d)
            if stat.S_ISLNK(s_st.st_mode):
                os.symlink(os.readlink(s), d)
            else:
                shutil.copyfile(s, d, follow_symlinks=False)
                os.chmod(d, 0o755 if s_st.st_mode & 0o111 else 0o644)
            _own(d)
        # os.walk (followlinks=False) lists a symlink-to-dir in dirnames but
        # never descends into it; and `out` is always a real directory (kept
        # because lstat said so, or made above), so nothing is written
        # through a link.


# #2465: section boundaries of a `pg_dumpall` plain-text dump, shared by the
# #279 single-DB slicer and the full-restore planner so both cut the dump
# the same way.
#   * `\connect <db>` — pg_dump's (or pg_dumpall's, for postgres/template1)
#     switch into a database.
#   * `-- Database "<db>" dump` — the header pg_dumpall (>= 11) writes before
#     EVERY database. The database's `CREATE DATABASE` sits between this
#     header and its `\connect`, i.e. in front of the section; cutting only
#     at `\connect` hands one database's section the NEXT database's
#     CREATE DATABASE.
# Lines inside a `COPY ... FROM stdin;` data block are row data, never a
# boundary (a text column may hold any line; COPY escapes backslashes, but
# not dashes).
#
# Known limit: the scanner does not parse SQL quoting. A multi-line quoted
# literal OUTSIDE a COPY block (a function body in $$…$$, a view or comment
# text, an --inserts dump's string) could hold a line that reads exactly
# like `\connect x` or `-- Database "x" dump`. That never cuts silently in
# the wrong place: plan_cluster_dump then sees a second \connect in one
# section, a header without its own \connect, or a database twice, and
# REFUSES the dump before anything is dropped; a boundary it cannot spot
# ends in psql's ON_ERROR_STOP failing the section loudly.
_CONNECT_LINE_RE = re.compile(r'^\\connect(?:\s|$)')
_CONNECT_NAME_RE = re.compile(r'^\\connect\s+"?([A-Za-z0-9_]+)"?\s*$')
_DUMPALL_DB_HEADER_RE = re.compile(r'^-- Database "(.*)" dump\s*$')
_COPY_FROM_STDIN_RE = re.compile(r'^COPY\s.*\sFROM stdin;\s*$')
_DUMPALL_TRAILER = '-- PostgreSQL database cluster dump complete'


def _cluster_dump_events(lines):
    """#2465: yield ``(index, offset, kind, name)`` for every section boundary
    in a pg_dumpall dump, outside COPY data blocks.

    ``lines`` is any iterable of lines (str, or bytes from a binary file),
    each with its line ending; ``offset`` is the boundary line's start in
    the units of ``len(line)`` (bytes for a binary file, so a caller can
    seek to it). ``kind`` is ``'connect'`` or ``'header'``; ``name`` is the
    database name, or None for a ``\\connect`` line whose target is not a
    plain name (a caller must refuse, never guess, on None).
    """
    in_copy = False
    offset = 0
    for index, raw in enumerate(lines):
        # Cheap pre-filter: every line that matters starts with `\`
        # (\connect, the COPY terminator), `-` (a header) or `C` (COPY);
        # the bulk of a dump — COPY rows — is skipped without decoding.
        first = raw[:1]
        if isinstance(raw, bytes):
            first = first.decode('latin-1')
        if first not in ('\\', '-', 'C'):
            offset += len(raw)
            continue
        line = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
        line = line.rstrip('\r\n')
        if in_copy:
            if line == '\\.':
                in_copy = False
        elif _COPY_FROM_STDIN_RE.match(line):
            in_copy = True
        elif _CONNECT_LINE_RE.match(line):
            m = _CONNECT_NAME_RE.match(line)
            yield index, offset, 'connect', (m.group(1) if m else None)
        else:
            m = _DUMPALL_DB_HEADER_RE.match(line)
            if m:
                yield index, offset, 'header', m.group(1)
        offset += len(raw)


def extract_single_database_dump(sql_text, db_name, lines=None):
    """#279: slice exactly ONE database's section out of a `pg_dumpall`
    cluster dump (postgres_core.sql / postgres_komodo.sql).

    A `pg_dumpall` dump is one text stream: cluster-wide role/tablespace/
    CREATE DATABASE statements, then one `\\connect <db>` section per
    database (schema + data for that db only, via COPY/INSERT). Replaying
    the WHOLE stream onto a live cluster is wrong for a single-DB restore:
    it touches every other database too. This returns just the target
    database's section: from its own `\\connect db_name` line (inclusive)
    up to (but excluding) the next section boundary — the next `\\connect`
    line or (#2465) the next `-- Database "..." dump` header, which carries
    the next database's CREATE DATABASE — or EOF.

    Matches an EXACT database-name token only — `\\connect openwebui_db`
    must never match a lookup for `openwebui_db2` (or vice-versa). A
    prefix/substring match here would let one database's restore silently
    pull in another's content, which is exactly the kind of "ambiguous
    target" a per-DB restore must refuse (#279 fail-safe).

    Returns the section text (starting at its own `\\connect` line), or
    None if `db_name` has no such section in this dump — the caller must
    treat that as "not in this dump", never restore anything.

    ``lines``: the dump already split with keepends (saves a second split
    of a large dump); ``sql_text`` is then ignored.
    """
    if lines is None:
        lines = sql_text.splitlines(keepends=True)
    start = None
    end = len(lines)
    for index, _offset, kind, name in _cluster_dump_events(lines):
        if start is not None:
            end = index
            break
        if kind == 'connect' and name == db_name:
            start = index
    if start is None:
        return None
    return ''.join(lines[start:end])


def _create_database_re(db_name):
    """`CREATE DATABASE <db_name>` at a line start, quoted or not (str)."""
    return re.compile(r'^CREATE DATABASE "?' + re.escape(db_name) + r'"?[\s;]', re.M)


def extract_database_create_section(sql_text, db_name, lines=None):
    """#279 / #2465: the part of a pg_dumpall dump that CREATES ``db_name``:
    from its `-- Database "db_name" dump` header up to (excluding) its own
    `\\connect db_name` — pg_dump --create's CREATE DATABASE (encoding,
    locale, owner) and ALTER DATABASE … OWNER, plus SET / \\restrict lines.
    None when the dump has no such part with a CREATE DATABASE for exactly
    this database (postgres/template1, a pre-11 dump, a synthetic one) —
    the caller then creates the database itself.

    ``lines``: the dump already split with keepends; ``sql_text`` is then
    ignored. The scan stops at the section's own \\connect.
    """
    if lines is None:
        lines = sql_text.splitlines(keepends=True)
    header_at = None
    for index, _offset, kind, name in _cluster_dump_events(lines):
        if kind == 'header':
            header_at = index if name == db_name else None
        elif header_at is not None:
            if name != db_name:
                return None
            part = ''.join(lines[header_at:index])
            return part if _create_database_re(db_name).search(part) else None
    return None


# #2465: databases a full restore replays IN PLACE instead of dropping and
# recreating: `postgres` is the maintenance database the restore itself is
# connected to (it can never be dropped from there), and template1 is the
# template every CREATE DATABASE without TEMPLATE copies. pg_dumpall writes
# neither a CREATE DATABASE for them.
# NOT idempotent: the in-place replay (ON_ERROR_STOP=1) fails on the first
# object that already exists there. The stack writes nothing into either
# database today, so on a real box their sections hold only SET lines; the
# day something does, a restore fails loudly here instead of merging.
_IN_PLACE_DATABASES = ('postgres', 'template1')
_MAINTENANCE_DB = 'postgres'

# #2465: psql `\set VERBOSITY verbose` prints every server message as
# `psql:<file>:<line>: <SEVERITY>:  <SQLSTATE>: <text>`. The SQLSTATE is
# never translated, the severity word is, so the checks key on the SQLSTATE.
_PSQL_MESSAGE_RE = re.compile(r'^psql:.*?:\d+: \S+:\s+([0-9A-Z]{5}): ')
# 00 = success, 01 = warning, 02 = no data: notices, not errors.
_NON_ERROR_SQLSTATE_CLASSES = ('00', '01', '02')
# 42710 duplicate_object: in the cluster-wide part of a dump this is only
# ever `role "…" already exists` (or the same for a tablespace) — the role
# init already created, including the connecting superuser. The ALTER ROLE
# pg_dumpall writes next to every CREATE ROLE still applies the backup's
# attributes and password.
_ROLE_EXISTS_SQLSTATE = '42710'


def _refuse_incomplete_dump(size, tail_text):
    """#2465: the refusals a pg_dumpall file must pass before ANY database is
    dropped, shared by the full restore (plan_cluster_dump) and the #279
    single-DB restore. ``tail_text`` is (at least) the file's last 4 KiB.

    Why the trailer matters: psql treats EOF inside `COPY … FROM stdin` as
    the end of the data and exits 0 (measured, PG 16.2, ON_ERROR_STOP=1) —
    a truncated dump replays "successfully" with the rows it happens to hold.
    """
    if size == 0:
        raise ValueError("the dump file is empty (0 bytes)")
    tail_lines = tail_text.splitlines()
    while tail_lines and tail_lines[-1].strip() in ('', '--'):
        tail_lines.pop()
    if not tail_lines or tail_lines[-1].strip() != _DUMPALL_TRAILER:
        raise ValueError(
            f"the dump does not end with pg_dumpall's '{_DUMPALL_TRAILER}' "
            f"line — it is truncated or not a pg_dumpall file")


_NO_DATABASE_MSG = ("the dump holds no database at all (no "
                    "'-- Database \"...\" dump' header)")


def refuse_incomplete_dump_lines(lines):
    """#279 / #2465 fix round 2: the same refusals for a dump already in
    memory as lines (the single-DB path): empty, truncated (no trailer),
    no database header. Raises ValueError."""
    tail = ''.join(lines[-64:])
    _refuse_incomplete_dump(sum(len(l) for l in lines), tail[-4096:])
    if not any(kind == 'header' for _i, _o, kind, _n in _cluster_dump_events(lines)):
        raise ValueError(_NO_DATABASE_MSG)


def plan_cluster_dump(dump_path):
    """#2465: cut a pg_dumpall file into its cluster-wide part and one
    section per database, as byte ranges, without reading it into memory
    (a customer dump can be many GB).

    Returns ``(header_end, databases)``: the cluster-wide part (roles,
    memberships, tablespaces) is ``[0, header_end)``; ``databases`` is a
    list of ``(name, start, connect_at, end)`` in dump order — the section
    ``[start, end)`` begins at the database's `-- Database "…" dump` header
    (so it carries its CREATE DATABASE) and ``connect_at`` is where its
    `\\connect` line starts.

    Raises ValueError for anything it cannot cut unambiguously: an empty
    file, a dump without pg_dumpall's closing `-- PostgreSQL database
    cluster dump complete` line (truncated — a disk-full or killed
    pg_dumpall), a dump without any database header, a `\\connect`
    outside a pg_dumpall database header, a header without exactly one
    matching `\\connect`, a database name that isn't a plain identifier,
    or a database that appears twice. A restore refuses such a dump before
    it touches anything.
    """
    size = os.path.getsize(dump_path)
    with open(dump_path, 'rb') as f:
        f.seek(max(0, size - 4096))
        tail = f.read().decode('utf-8', errors='replace')
    _refuse_incomplete_dump(size, tail)

    with open(dump_path, 'rb') as f:
        events = list(_cluster_dump_events(f))

    headers = [e for e in events if e[2] == 'header']
    if not headers:
        if any(e[2] == 'connect' for e in events):
            raise ValueError(
                "the dump has \\connect sections but no pg_dumpall "
                "'-- Database \"...\" dump' headers (not a pg_dumpall >= 11 "
                "dump) — refusing to guess where each database starts")
        raise ValueError(_NO_DATABASE_MSG)

    header_end = headers[0][1]
    stray = [e for e in events if e[1] < header_end]
    if stray:
        raise ValueError(
            f"\\connect before the first database header (line {stray[0][0] + 1})")

    databases = []
    seen = set()
    for i, (_index, start, _kind, name) in enumerate(headers):
        end = headers[i + 1][1] if i + 1 < len(headers) else size
        if not name or not _SAFE_DB_NAME_RE.match(name):
            raise ValueError(
                f"database name {name!r} is not a plain identifier "
                f"(letters, digits, underscore only) — the WHOLE full restore "
                f"is refused, nothing was dropped; rename that database on "
                f"the source box and take a new backup")
        if name in seen:
            raise ValueError(f"database {name!r} appears twice in the dump")
        seen.add(name)
        connects = [e for e in events if e[2] == 'connect' and start < e[1] < end]
        if len(connects) != 1 or connects[0][3] != name:
            raise ValueError(
                f"the section of database {name!r} does not hold exactly one "
                f"\\connect {name} (found {[c[3] for c in connects]})")
        databases.append((name, start, connects[0][1], end))
    return header_end, databases


def _stream_psql(argv, dump_path=None, ranges=(), prefix=b''):
    """#2465: run psql with ``prefix`` and then the byte ``ranges`` of
    ``dump_path`` on stdin, streamed in 1 MiB chunks. Returns
    ``(returncode, stderr_text)``; stdout (command tags) is discarded."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=err)
        try:
            if prefix:
                proc.stdin.write(prefix)
            if ranges:
                with open(dump_path, 'rb') as f:
                    for start, end in ranges:
                        f.seek(start)
                        remaining = end - start
                        while remaining > 0:
                            chunk = f.read(min(1 << 20, remaining))
                            if not chunk:
                                break
                            proc.stdin.write(chunk)
                            remaining -= len(chunk)
        except BrokenPipeError:
            pass  # psql stopped reading (ON_ERROR_STOP); rc + stderr say why
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        rc = proc.wait()
        err.seek(0)
        return rc, err.read().decode('utf-8', errors='replace')


def _tail(text, limit=800):
    text = (text or '').strip()
    return text if len(text) <= limit else '…' + text[-limit:]


def restore_cluster_dump(dump_path, psql_argv, label='postgres'):
    """#2465: replay a pg_dumpall file so that every database IN THE DUMP
    ends up holding exactly the dumped contents.

    The old replay piped the whole dump into one psql without
    ON_ERROR_STOP onto a live cluster (the DR path: `rzfz init`, then
    restore): every CREATE DATABASE failed, every COPY that hit a row init
    had already written failed as a whole, and psql still exited 0 — the
    restore reported success with a mix of init's and the backup's data.

    Order and rules:
      1. The cluster-wide part (roles, memberships, tablespaces) first,
         against the maintenance DB. `role … already exists` (SQLSTATE
         42710) is a no-op; any other error is fatal and the restore stops
         here, before a single database is dropped.
      2. Then each database section, in dump order:
         * `postgres` / `template1` are replayed in place (never dropped).
         * Every other database: its other connections are terminated and
           it is dropped (`DROP DATABASE … WITH (FORCE)`) and recreated by
           the dump's own CREATE DATABASE, then its section replays with
           ON_ERROR_STOP=1. A section without its CREATE DATABASE is
           refused before the drop. A failed drop aborts that database
           before anything is written to it.
         Databases on the live cluster that the dump does not contain are
         never touched.
      Any failure makes the result False; the remaining databases are
      still restored, and every failure is named in the returned list.

    ``psql_argv(db, on_error_stop)`` returns the argv of a psql that reads a
    script from stdin with ``-f -`` connected to ``db`` (docker exec on a box; a local psql
    in the api-tier test).

    Returns ``(ok, failures)``.
    """
    failures = []
    try:
        header_end, databases = plan_cluster_dump(dump_path)
    except (OSError, ValueError) as e:
        msg = f"{label}: cannot read the dump {dump_path} as a pg_dumpall file: {e}"
        logger.error(f"#2465: {msg} — nothing was restored.")
        return False, [msg]

    verbose = b'\\set VERBOSITY verbose\n'

    # 1. roles / tablespaces
    rc, err = _stream_psql(psql_argv(_MAINTENANCE_DB, False), dump_path,
                           [(0, header_end)], prefix=verbose)
    fatal = []
    exists = 0
    for line in err.splitlines():
        if not line.startswith('psql:'):
            continue  # DETAIL / HINT / LOCATION continuation lines
        m = _PSQL_MESSAGE_RE.match(line)
        if m and m.group(1)[:2] in _NON_ERROR_SQLSTATE_CLASSES:
            continue
        if m and m.group(1) == _ROLE_EXISTS_SQLSTATE:
            exists += 1
            continue
        fatal.append(line)
    if rc != 0 or fatal:
        msg = (f"{label}: replaying roles/tablespaces failed (psql rc={rc}): "
               f"{_tail(chr(10).join(fatal) or err)}")
        logger.error(f"#2465: {msg} — aborting before any database was dropped.")
        return False, [msg]
    logger.info(f"#2465: {label}: roles/tablespaces replayed "
                f"({exists} already existed — kept, attributes re-applied).")

    # 2. databases
    for name, start, connect_at, end in databases:
        if name in _IN_PLACE_DATABASES:
            rc, err = _stream_psql(psql_argv(name, True), dump_path,
                                   [(start, end)], prefix=verbose)
            if rc != 0:
                msg = f"{label}: database {name!r} (replayed in place) FAILED: {_tail(err)}"
                logger.error(f"#2465: {msg}")
                failures.append(msg)
            else:
                logger.info(f"#2465: {label}: database {name!r} replayed in place.")
            continue

        with open(dump_path, 'rb') as f:
            f.seek(start)
            create_part = f.read(connect_at - start)
        if not _create_database_re(name).search(
                create_part.decode('utf-8', errors='replace')):
            msg = (f"{label}: database {name!r} has no CREATE DATABASE in its "
                   f"dump section — not dropped, not restored")
            logger.error(f"#2465: {msg}")
            failures.append(msg)
            continue

        drop_sql = (
            verbose
            + (f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
               f"WHERE datname = '{name}' AND pid <> pg_backend_pid();\n"
               f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);\n').encode()
        )
        rc, err = _stream_psql(psql_argv(_MAINTENANCE_DB, True), prefix=drop_sql)
        if rc != 0:
            msg = (f"{label}: could not drop database {name!r} before restoring "
                   f"it — its restore was ABORTED, the live database is "
                   f"unchanged: {_tail(err)}")
            logger.error(f"#2465: {msg}")
            failures.append(msg)
            continue

        rc, err = _stream_psql(psql_argv(_MAINTENANCE_DB, True), dump_path,
                               [(start, end)], prefix=verbose)
        if rc != 0:
            msg = (f"{label}: database {name!r} was dropped and its replay "
                   f"FAILED — it is INCOMPLETE: {_tail(err)}")
            logger.error(f"#2465: {msg}")
            failures.append(msg)
        else:
            logger.info(f"#2465: {label}: database {name!r} dropped, recreated and restored.")

    return not failures, failures


class BackupManager:
    def __init__(self):
        pass

    def _generate_checksum(self, archive_path):
        """F-002: Generate a SHA-256 checksum file alongside a backup archive.

        Creates <archive_path>.sha256 in sha256sum-compatible format.
        """
        sha256 = hashlib.sha256()
        try:
            with open(archive_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    sha256.update(chunk)
            checksum = sha256.hexdigest()
            checksum_path = archive_path + '.sha256'
            filename = os.path.basename(archive_path)
            with open(checksum_path, 'w') as f:
                f.write(f"{checksum}  {filename}\n")
            logger.info(f"Checksum generated: {checksum_path}")
            return checksum_path
        except Exception as e:
            logger.error(f"Failed to generate checksum for {archive_path}: {e}")
            return None

    def _verify_checksum(self, archive_path):
        """F-002: Verify SHA-256 checksum of a backup archive before restore.

        Returns True if checksum matches, False otherwise.
        Logs a warning and returns True (allow restore) if no checksum file exists.
        """
        checksum_path = archive_path + '.sha256'
        if not os.path.exists(checksum_path):
            logger.warning(f"No checksum file found for {archive_path}. Skipping integrity check.")
            return True

        try:
            # Read expected checksum
            with open(checksum_path, 'r') as f:
                line = f.readline().strip()
            expected_hash = line.split()[0]

            # Compute actual checksum
            sha256 = hashlib.sha256()
            with open(archive_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    sha256.update(chunk)
            actual_hash = sha256.hexdigest()

            if actual_hash == expected_hash:
                logger.info(f"Checksum verified for {os.path.basename(archive_path)}")
                return True
            else:
                logger.error(
                    f"CHECKSUM MISMATCH for {os.path.basename(archive_path)}: "
                    f"expected {expected_hash}, got {actual_hash}"
                )
                return False
        except Exception as e:
            logger.error(f"Checksum verification failed for {archive_path}: {e}")
            return False

    def list_backups(self):
        """List all available backup files in the archive directory.
        rc6.7 #6: accepts both .tar.gz and .tar.gz.gpg via is_backup_file."""
        try:
            files = [f for f in os.listdir(BACKUP_DIR) if is_backup_file(f)]
            files.sort(reverse=True)
            return files
        except Exception as e:
            logger.error(f"Error listing backups: {e}")
            return []

    def delete_backup(self, filename):
        """Delete a specific backup file."""
        # Sanitize filename to prevent directory traversal
        filename = os.path.basename(filename)
        path = os.path.join(BACKUP_DIR, filename)
        
        if not os.path.exists(path):
            logger.error(f"Backup file not found: {path}")
            return False
            
        try:
            os.remove(path)
            logger.info(f"Deleted backup file: {filename}")
            return True
        except Exception as e:
            logger.error(f"Error deleting backup {filename}: {e}")
            return False

    def dump_per_database_backups(self):
        """#279: write one `pg_dump -Fc` per user database so the archive
        carries a directly pg_restore-able per-DB dump alongside the
        existing pg_dumpall cluster dump.

        Writes into RESTORE_ROOT/databases/per-db/<db>.dump. RESTORE_ROOT/
        databases is bind-mounted from the SAME `db-dumps` named volume
        that backup-service mounts at /backup/databases (core/compose.yml),
        so anything written here BEFORE trigger_full_backup() invokes the
        wrapper is already sitting in that volume when offen archives it —
        no changes to pre-backup.sh needed.

        Best-effort / non-critical: a failure here must not abort the full
        backup (the pg_dumpall cluster dump — the pre-existing full-restore
        path — is unaffected either way). Stale per-DB dumps for databases
        that no longer exist are cleared first so a dropped database's dump
        doesn't linger forever.
        """
        db_dump_dir = os.path.join(RESTORE_ROOT, "databases")
        per_db_dir = os.path.join(db_dump_dir, PER_DB_DUMP_SUBDIR)
        pg_user = os.environ.get('POSTGRES_USER', 'docker')
        try:
            os.makedirs(db_dump_dir, exist_ok=True)
            if os.path.exists(per_db_dir):
                shutil.rmtree(per_db_dir)
            os.makedirs(per_db_dir, exist_ok=True)

            list_result = subprocess.run(
                ['docker', 'exec', 'postgres', 'psql', '-U', pg_user, '-d', 'postgres',
                 '-tAc',
                 "SELECT datname FROM pg_database WHERE datistemplate = false "
                 "AND datname != 'postgres' ORDER BY datname;"],
                capture_output=True, text=True,
            )
            if list_result.returncode != 0:
                logger.error(
                    f"#279: could not list databases for per-DB dump: "
                    f"{list_result.stderr.strip()}"
                )
                return

            dbs = [d.strip() for d in list_result.stdout.splitlines() if d.strip()]
            written = 0
            failed = 0
            for db in dbs:
                dump_path = os.path.join(per_db_dir, f"{db}.dump")
                try:
                    with open(dump_path, 'wb') as f:
                        result = subprocess.run(
                            ['docker', 'exec', 'postgres', 'pg_dump',
                             '-U', pg_user, '-Fc', db],
                            stdout=f, stderr=subprocess.PIPE,
                        )
                    if result.returncode != 0:
                        stderr = result.stderr
                        stderr = stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else (stderr or '')
                        logger.error(f"#279: per-DB dump failed for {db!r}: {stderr.strip()}")
                        failed += 1
                        try:
                            os.remove(dump_path)
                        except OSError:
                            pass
                    else:
                        written += 1
                except Exception as e:
                    logger.error(f"#279: per-DB dump raised for {db!r}: {e}")
                    failed += 1
                    try:
                        os.remove(dump_path)
                    except OSError:
                        pass

            logger.info(
                f"#279: per-DB dump pass complete — {written} written, "
                f"{failed} failed, {len(dbs)} database(s) listed."
            )
        except Exception as e:
            logger.error(f"#279: per-DB dump step failed: {e}")

    def trigger_full_backup(self):
        """Trigger the existing backup-service container to run a backup.

        BSB-04 / R-DEF-04: invokes the `razzfazz-backup` wrapper instead
        of offen's bare `backup` binary. The wrapper re-reads BACKUP_
        ENCRYPTION_PASSWORD from /scripts/dot-env at every call, so a
        rotated passphrase takes effect immediately without needing a
        `compose up -d --force-recreate backup-service`.

        #279: refreshes the per-DB pg_dump -Fc dumps FIRST (best-effort —
        see dump_per_database_backups) so they land in the db-dumps volume
        before offen archives it.
        """
        try:
            self.dump_per_database_backups()
            container = client.containers.get("backup-service")
            logger.info("Triggering full backup via backup-service (razzfazz-backup wrapper)...")

            # BSB-04: invoke the wrapper (installed at
            # /usr/local/bin/razzfazz-backup by core/backup/Dockerfile).
            # Wrapper re-reads BACKUP_ENCRYPTION_PASSWORD per invocation,
            # then exec's offen's /usr/bin/backup. Falls back gracefully
            # if the wrapper is missing (older image): try plain `backup`.
            exec_log = container.exec_run("razzfazz-backup", stream=True)
            for line in exec_log.output:
                logger.info(f"Backup Service: {line.decode().strip()}")

            logger.info("Full backup triggered successfully.")
            return True
        except docker.errors.NotFound:
            logger.error("backup-service container not found.")
            return False
        except Exception as e:
            logger.error(f"Error triggering backup: {e}")
            return False

    def trigger_partial_backup(self, target_name, env_path='/stack/.env'):
        """
        Create a partial backup for a specific target (volume name in /backup inside backup-service).
        Since we can't easily tell the main backup service to only do one, we might have to do it ourselves
        if we have access to the volumes.

        However, this container (backup-manager) will mount the volumes at /restore_targets/...
        mirroring the backup service structure for consistency.

        #2452: a partial is encrypted exactly like a full backup. It was always
        written as plain gzip, world-readable — on 0.208 three partials of
        valkey-data held Authentik sessions and Dify's cached provider
        credentials, and since the full backup excludes valkey-data they were
        the box's only Valkey backups. With BACKUP_ENCRYPTION_PASSWORD set the
        archive is `partial-….tar.gz.gpg` (gpg symmetric AES256, passphrase on
        stdin, never on the command line) and no plaintext copy is left behind;
        if encryption fails the backup FAILS rather than falling back to
        plaintext. Every file written is 0600. A box without a passphrase keeps
        a plaintext archive (0600) and says so — the same rule as the full
        backup, which `rzfz status` already reports as F-A2.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filename = f"partial-{target_name}-{timestamp}.tar.gz"
        source_path = os.path.join(RESTORE_ROOT, target_name)
        dest_path = os.path.join(BACKUP_DIR, filename)

        if not os.path.exists(source_path):
            logger.error(f"Target volume path {source_path} does not exist.")
            return False

        passphrase = _read_passphrase_from_env_file(env_path) \
            or os.environ.get('BACKUP_ENCRYPTION_PASSWORD', '')
        logger.info(f"Starting partial backup of {target_name} to {filename}"
                    f"{'.gpg (encrypted)' if passphrase else ' (NOT encrypted: BACKUP_ENCRYPTION_PASSWORD is empty)'}...")
        old_umask = os.umask(0o077)
        try:
            with tarfile.open(dest_path, "w:gz") as tar:
                tar.add(source_path, arcname=target_name)
            os.chmod(dest_path, 0o600)
            if passphrase:
                enc_path = dest_path + '.gpg'
                try:
                    proc = subprocess.run(
                        ['gpg', '--batch', '--yes', '--quiet', '--pinentry-mode', 'loopback',
                         '--passphrase-fd', '0', '--symmetric', '--cipher-algo', 'AES256',
                         '--output', enc_path, dest_path],
                        input=passphrase.encode('utf-8'), capture_output=True, timeout=3600)
                    ok = proc.returncode == 0 and os.path.isfile(enc_path) and os.path.getsize(enc_path) > 0
                    why = proc.stderr.decode('utf-8', errors='replace').strip()
                except Exception as e:  # gpg missing, timeout
                    ok, why = False, str(e)
                finally:
                    # the plaintext archive never outlives this call on a box with a passphrase
                    try:
                        os.remove(dest_path)
                    except OSError:
                        pass
                if not ok:
                    try:
                        os.remove(enc_path)
                    except OSError:
                        pass
                    logger.error(f"Partial backup of {target_name} NOT written: encryption failed ({why}). "
                                 f"No plaintext copy was kept (#2452).")
                    return False
                os.chmod(enc_path, 0o600)
                dest_path = enc_path
                filename = os.path.basename(enc_path)
            else:
                logger.warning(f"Partial backup {filename} is NOT encrypted: BACKUP_ENCRYPTION_PASSWORD "
                               f"is empty on this box (F-A2). It is readable by its owner only.")
            logger.info(f"Partial backup created: {filename}")
            # F-002: Generate integrity checksum
            sidecar = self._generate_checksum(dest_path)
            if sidecar:
                os.chmod(sidecar, 0o600)
            return True
        except Exception as e:
            logger.error(f"Failed to create partial backup: {e}")
            for leftover in (dest_path, dest_path + '.gpg'):
                if leftover.endswith('.tar.gz') and os.path.exists(leftover) and passphrase:
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
            return False
        finally:
            os.umask(old_umask)

    def restore_full(self, backup_file):
        """
        Perform a full restore.
        1. Stop all containers (except this one and backup-service maybe).
        2. Extract archive.
        3. Restore DBs.
        4. Restart containers.

        Returns True (complete), RESTORE_PARTIAL (#2464: databases and .env
        restored, certs/, the box-local state or — #2531 — a per-user agent
        volume not) or False (failed).
        """
        backup_path = os.path.join(BACKUP_DIR, backup_file)
        if not os.path.exists(backup_path):
            logger.error(f"Backup file {backup_file} not found.")
            return False

        # F-002: Verify integrity before restoring
        if not self._verify_checksum(backup_path):
            logger.error("Aborting restore due to checksum mismatch.")
            return False

        logger.info("Initiating FULL RESTORE. Stopping stack...")
        self.stop_stack()

        # rc6.7 #7: encrypted backups (.tar.gz.gpg) need a decrypt step
        # before tarfile.open. We write the decrypted tarball to a /tmp
        # path; the finally clause below removes it after extraction.
        decrypted_tmp = None
        # #1633-Klasse, gefunden von agent-rzfz im Review zu #1621: der
        # `finally`-Block unten endete auf `return True`. Ein `return` im
        # `finally` ueberschreibt JEDES `return` aus dem `try` und verschluckt
        # ausserdem jede Ausnahme — die Ablehnung eines zu neuen Archivs meldete
        # deshalb Erfolg, und (seit jeher, aelter als #1621) meldete auch jeder
        # abgestuerzte Restore Erfolg. Das Ergebnis wird jetzt festgehalten,
        # `finally` raeumt nur noch auf, und die Antwort steht danach.
        outcome = False
        try:

            if is_encrypted_backup(backup_file):
                decrypted_tmp = decrypt_backup_to_temp(backup_path)
                archive_path = decrypted_tmp
            else:
                archive_path = backup_path

            logger.info(f"Extracting {backup_file}...")
            # We extract to root / which maps to the mounted volumes at /restore_targets due to how tar stores paths
            # The backup service stores them as backup/<volname>/...
            # We need to map backup/<volname> to /restore_targets/<volname>

            # Let's inspect the tar first
            # #1621: after the decrypt, before a single member is written. A
            # refusal here costs a restore that would have gone wrong; one step
            # later it costs the volumes that were already overwritten.
            _ok, _why = self.check_backup_version(archive_path)
            logger.info("Backup version check: %s", _why)
            if not _ok:
                logger.error(_why)
                return False

            with tarfile.open(archive_path, "r:gz") as tar:
                # The backup creates archives with structure: backup/volume-name/files...
                # Our restore targets are mounted at: /restore_targets/volume-name
                
                # So we need to strip 'backup/' prefix and extract to /restore_targets
                for member in tar.getmembers():
                    if member.name.startswith("backup/"):
                        # Remove 'backup/'
                        new_name = member.name[7:] # len("backup/") == 7
                        if not new_name: continue 
                        
                        target_path = os.path.join(RESTORE_ROOT, new_name)
                        
                        # We can't just change member.name and extractall easily if we want to be safe
                        # But simpler: extract to a temp dir, then rsync/copy
                        pass
                
                # Extraction Strategy:
                # Extract entire archive to a temp directory
                temp_extract_dir = RESTORE_EXTRACT_DIR
                if os.path.exists(temp_extract_dir):
                    shutil.rmtree(temp_extract_dir)
                os.makedirs(temp_extract_dir)
                
                # Safe extraction preventing path traversal
                safe_extract_tar(tar, temp_extract_dir)
                # #2542: the owners come from the archive's members (names are
                # normalised by safe_extract_tar above).
                archive_owner_map = archive_owners_by_volume(tar.getmembers())
                
                # Move files from /tmp/restore_extract/backup/* to /restore_targets/*
                #
                # #125 (CRITICAL): volumes whose target already has data (the
                # common DR path: fresh init populated postgres-data etc., then we
                # restore an OLDER backup over it) MUST be EMPTIED before the copy.
                # The old code used copytree(dirs_exist_ok=True), which MERGES the
                # backup over the existing contents. For PostgreSQL this is fatal:
                # the merged PGDATA keeps init #2's pg_authid / pg_control / WAL
                # alongside init #1's base files → an inconsistent cluster that
                # starts but rejects EVERY login with `password authentication
                # failed`. (This — not start-vs-recreate alone — is the deepest
                # cause of the post-restore crash loop: the postgres-data volume
                # was never actually replaced.) We therefore CLEAR each target
                # volume's contents in place (keeping the mountpoint dir itself,
                # which docker owns) and then copy the backup in cleanly.
                source_root = os.path.join(temp_extract_dir, "backup")
                # Volumes whose restore failure must FAIL the whole operation
                # (a "success" with a half-restored DB is worse than a loud error).
                critical_volumes = {"postgres-data", "databases"}
                vol_failures = []
                if os.path.exists(source_root):
                    for vol_name in sorted(os.listdir(source_root)):
                        src_vol = os.path.join(source_root, vol_name)
                        dst_vol = os.path.join(RESTORE_ROOT, vol_name)

                        if not os.path.exists(dst_vol):
                            logger.warning(f"Volume target {vol_name} exists in backup but not mounted in restoration container.")
                            continue

                        logger.info(f"Restoring volume: {vol_name} -> {dst_vol}")
                        try:
                            # #125: empty the destination IN PLACE first (true
                            # restore, not merge). Remove the mountpoint's children
                            # but never the mountpoint itself (docker owns it).
                            self._empty_dir_contents(dst_vol)
                            # rc6.7 #7: copy symlinks AS symlinks (symlinks=True)
                            # and tolerate dangling ones. Volume contents legit-
                            # imately contain symlinks pointing outside the backup
                            # tree (e.g. smtp-relay-data/etc/localtime ->
                            # /usr/share/zoneinfo/... , a container-local abs link).
                            # The default copytree FOLLOWS symlinks and aborts on
                            # the first dangling one. symlinks=True recreates the
                            # link verbatim; ignore_dangling_symlinks=True is belt-
                            # and-braces. dirs_exist_ok=True is still needed because
                            # the (now-empty) mountpoint dir itself already exists.
                            shutil.copytree(
                                src_vol, dst_vol,
                                symlinks=True,
                                ignore_dangling_symlinks=True,
                                dirs_exist_ok=True,
                            )
                            # #2542: copytree drops uid/gid; put the archive's back.
                            apply_archive_ownership(
                                dst_vol, archive_owner_map.get(vol_name, {}),
                                f"volume {vol_name}")
                        except Exception as ce:
                            # #125: SURFACE the failure — do not silently swallow.
                            # Non-critical volumes: log loudly and continue so the
                            # rest of the restore still runs. Critical volumes:
                            # record so we abort the whole restore below.
                            vol_failures.append((vol_name, str(ce)))
                            level = logger.critical if vol_name in critical_volumes else logger.error
                            level(
                                f"#125: volume restore FAILED for {vol_name}: {ce}"
                            )

                # Clean up
                shutil.rmtree(temp_extract_dir)

                # #125: a critical-volume failure means the DB was not restored —
                # abort loudly instead of marching on to report "Full Restore
                # Complete" on a broken cluster.
                critical_failed = [v for v, _ in vol_failures if v in critical_volumes]
                if critical_failed:
                    raise RuntimeError(
                        "Critical volume(s) failed to restore "
                        f"({', '.join(critical_failed)}) — aborting restore; the "
                        "database was NOT restored."
                    )
                if vol_failures:
                    logger.error(
                        "#125: restore completed with non-critical volume "
                        f"failures: {[v for v, _ in vol_failures]}"
                    )

            # Database Restore
            # Look for SQL dumps in /restore_targets/databases/ (which was just restored)
            # #2465: the result decides the outcome. The remaining steps
            # still run (the replayed roles carry the backup's passwords,
            # which only the restored .env matches), but a partial database
            # restore is never reported as complete.
            db_ok = self.restore_databases() is True

            # Restore encrypted .env files
            self.restore_env_files()

            # #1621: and the box's TLS material, which lives outside every
            # volume. Without it the box comes back on self-signed certs and
            # every vHost warns — a restore that "succeeded".
            # #2464 fix round 1: False = the backup carries it and it was NOT
            # restored (None = the backup has none). Either makes the restore
            # PARTIAL (exit 3), not failed: the databases and .env are back.
            certs_ok = self.restore_cert_files() is not False
            # #1621: and the rest of the box-local state, in the same breath.
            box_local_ok = self.restore_box_local_files() is not False

            # M030 S4: per-user agent volume restore. pre-backup.sh writes
            # one tarball per mounted volume into databases/agents/
            # <instance_id>/<volume>.tar with a manifest (#2531; 2026.09-ga.3 wrote
            # <slug>/<type>/<role>.tar). For each tarball, ensure the
            # target docker volume exists and extract the tarball into it.
            # The agent_instances rows (with their mounted_volumes) are
            # already back from the postgres restore — at next agent-manager
            # boot, the recreate mounts exactly these volumes.
            # #2531: False = an archive was NOT restored → PARTIAL (exit 3),
            # like certs/ and the box-local state (F2's post-DB rule).
            agents_ok = self.restore_agent_volumes() is not False

            not_restored = []
            if not certs_ok:
                not_restored.append(
                    "#1621: certs/ (the box's TLS material) was NOT restored "
                    "(see the certs/ restore error above)")
            if not box_local_ok:
                not_restored.append(
                    "#2464: the box-local state (.checksums.db, overlay/) was "
                    "NOT restored (see the box-local restore error above)")
            if not agents_ok:
                not_restored.append(
                    "#2531: per-user agent volumes were NOT all restored "
                    "(see the agent volume restore errors above)")
            if not db_ok:
                raise RuntimeError("; ".join(
                    ["#2465: the databases were NOT restored completely (see "
                     "the database restore errors above)"] + not_restored)
                    + " — FULL RESTORE FAILED")
            if not_restored:
                # #2464 fix round 1: the databases and .env are back, so the
                # stack must come up (cli/backup.sh recreates it on exit 3) —
                # but this is not "complete", and it says what is missing.
                logger.error("PARTIAL RESTORE: the databases and .env are "
                             "restored, but " + "; ".join(not_restored)
                             + " — put these back by hand.")
                outcome = RESTORE_PARTIAL
            else:
                logger.info("Full Restore Complete.")
                outcome = True

        except Exception as e:
            logger.error(f"Restore failed: {e}")
            # Try to restart anyway — but the caller hears that it failed.
            outcome = False
        finally:
            # rc6.7 #7: clean up the decrypted temp tarball so it doesn't
            # linger in /tmp after the restore (it contains the full
            # plaintext backup; treat as sensitive).
            if decrypted_tmp and os.path.exists(decrypted_tmp):
                try:
                    os.remove(decrypted_tmp)
                    logger.info(f"Removed decrypted temp tarball: {decrypted_tmp}")
                except OSError as e:
                    logger.warning(f"Could not remove decrypted temp tarball {decrypted_tmp}: {e}")
            logger.info("Restarting stack...")
            self.start_stack()
        return outcome

    def check_backup_version(self, archive_path, env_path='/stack/.env',
                             stack_dir='/stack'):
        """Is this archive safe to restore onto THIS code? (#1621)

        Returns (ok, message). The rule is deliberately ASYMMETRIC, because the
        two directions are not equally dangerous:

        * archive OLDER than the box — the normal recovery: restore last
          night's data onto today's code and let the migrations run forward.
          That is what a backup is for, and it must never be blocked.
        * archive NEWER than the box — a schema from the future onto code that
          does not know it. Nothing downgrades a database; the failure surfaces
          later, in some other service, as data that "went strange". This is
          the one we refuse.

        An archive with no version (made before this landed) and a version the
        shared parser cannot ORDER are both reported and allowed: refusing a
        recovery over a missing label would turn a safety check into the reason
        an operator cannot get his box back. #1334's lesson applies in the
        other direction too — say what you could not decide, never decide it
        silently.

        The ordering comes from `scripts/version_order.py`, the ONE parser
        (#1334), reachable here because the management container mounts the
        repo at /stack. If it cannot be imported the check reports that and
        allows: a backup manager that refuses to work because a helper moved is
        worse than one that restores an unusual archive.
        """
        # #1621 rev-B (review agent-rzfz): read the marker OUT OF THE ARCHIVE.
        #
        # The first version read RESTORE_ROOT/databases/stack-version.json —
        # which is the LIVE db-dumps volume of this box, and the check runs
        # before the archive is unpacked. So it answered with the version of the
        # last backup THIS BOX took, never the one being restored. Measured with
        # a real restore_full: archive ga.20, volume ga.9, box ga.15 → the log
        # said "Archive 2026.08-ga.9 <= box 2026.08-ga.15" and the restore went
        # ahead. On bare metal (empty volume) it read "no version marker" and
        # also went ahead. The one case the check exists for was waved through,
        # and the log asserted something false about exactly the question the
        # operator had asked.
        #
        # `archive_path` is the DECRYPTED tarball, so this runs after the
        # decrypt and before a single member is written.
        made_with = ""
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                member = None
                for name in ("backup/databases/stack-version.json",
                             "./backup/databases/stack-version.json",
                             "databases/stack-version.json"):
                    try:
                        member = tar.extractfile(name)
                    except KeyError:
                        member = None
                    if member is not None:
                        break
                if member is None:
                    return True, ("This archive carries no version marker (made "
                                  "before #1621). Restoring anyway — verify by "
                                  "hand that the code matches.")
                made_with = (json.loads(member.read().decode("utf-8"))
                             .get("version") or "").strip()
        except Exception as e:
            return True, f"Version marker unreadable ({e}); restoring anyway."
        if not made_with:
            return True, "Version marker is empty; restoring anyway."

        # The SHARED .env parser (razzfazz_common), not a fourth local one —
        # an operator's file carries quotes, inline comments and metachars, and
        # this module already refuses to `source` it for that reason.
        try:
            from razzfazz_common.env_utils import read_env_key
            here = (read_env_key(env_path, "RAZZFAZZ_VERSION") or "").strip()
        except Exception:
            here = ""
        if not here:
            return True, (f"Archive was made with {made_with}; this box does "
                          f"not say what it runs (RAZZFAZZ_VERSION in "
                          f"{env_path}). Restoring anyway.")
        try:
            sys.path.insert(0, os.path.join(stack_dir, "scripts"))
            import version_order
            if not (version_order.orderable(made_with) and version_order.orderable(here)):
                return True, (f"Cannot order {made_with!r} against {here!r}; "
                              "restoring anyway — check by hand.")
            newer = version_order.compare(made_with, here) > 0
        except Exception as e:
            return True, f"Version comparison unavailable ({e}); restoring anyway."

        if newer:
            if os.environ.get("RAZZFAZZ_RESTORE_ALLOW_NEWER") == "1":
                return True, (f"Archive is NEWER ({made_with}) than this box "
                              f"({here}) — proceeding because "
                              "RAZZFAZZ_RESTORE_ALLOW_NEWER=1 was set.")
            return False, (
                f"REFUSED: this archive was made with {made_with}, this box "
                f"runs {here}. Restoring a NEWER backup onto OLDER code puts a "
                "schema on disk that this code does not know, and nothing "
                "migrates a database backwards. Upgrade the box first, or set "
                "RAZZFAZZ_RESTORE_ALLOW_NEWER=1 if you know why you want this.")
        return True, f"Archive {made_with} <= box {here}; migrations run forward."

    def restore_cert_files(self, env_path='/stack/.env', stack_dir='/stack'):
        """Decrypt and restore certs/ from the backup (#1621).

        Shaped like `restore_env_files` on purpose — same passphrase source,
        same re-read-at-call-time rule (BSB-04), same "no password, say so and
        return" behaviour. One recipe, so an operator in an incident does not
        have to remember two.

        `stack_dir` is a parameter and not the literal "/stack" its sibling uses,
        so the round trip — encrypt with the shipped hook, restore with this —
        can be asserted without monkeypatching the filesystem out from under
        the code under test. The default is the container's path, so nothing
        changes in production.

        Extracted OVER the existing directory rather than replacing it: a box
        may hold a file the backup predates (a freshly renewed certificate),
        and losing that to a restore would be its own incident. tar without
        --overwrite-dir keeps existing entries that the archive does not name.
        """
        enc_path = os.path.join(RESTORE_ROOT, "databases", "certs-backup.tar.gz.enc")
        if not os.path.exists(enc_path):
            # #2464 fix round 1: None, like the box-local sibling — a backup
            # older than #1621 carries no certs; that is not a failed restore.
            logger.info("No encrypted certs archive in backup. Skipping.")
            return None

        encryption_pass = _read_passphrase_from_env_file(env_path) \
            or os.environ.get('BACKUP_ENCRYPTION_PASSWORD') \
            or os.environ.get('AUTHENTIK_BOOTSTRAP_PASSWORD', '')
        if not encryption_pass:
            logger.warning(
                "No BACKUP_ENCRYPTION_PASSWORD in %s (nor in the environment). "
                "Cannot restore certs/ — the box will come back on self-signed "
                "TLS. The passphrase is NOT in the backup: it has to come from "
                "outside it.", env_path)
            return False

        target = stack_dir
        try:
            dec = subprocess.run(
                ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
                 "-in", enc_path, "-pass", f"pass:{encryption_pass}"],
                capture_output=True, check=True)
            # #2464 fix round 1: name the member. /stack is :ro except the
            # `certs/` bind, and only `certs/` (and below) is extracted —
            # anything else the archive might carry is not.
            subprocess.run(["tar", "xzf", "-", "-C", target, "certs"],
                           input=dec.stdout, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            # Named loudly: a silent failure here is a box that looks restored
            # and serves the wrong certificate.
            logger.error(
                "certs/ restore FAILED (%s) — the box keeps whatever certs it "
                "has, which after a bare-metal restore is nothing. Its TLS will "
                "be self-signed until they are put back by hand.",
                (e.stderr or b"").decode("utf-8", "replace")[:300])
            return False
        logger.info("certs/ restored.")
        return True

    def restore_box_local_files(self, env_path='/stack/.env', stack_dir='/stack'):
        """Decrypt and restore the box-local state from the backup (#1621, #2464).

        The third sibling of `restore_env_files` and `restore_cert_files`: same
        passphrase source, same re-read at call time (BSB-04).

        WHAT IS IN IT, and why each is here rather than reproducible:

        * `.checksums.db` — the governance BASELINE. Its loss breaks nothing on
          the box; it erases the record of what that box once was, and
          `rzfz setup --checksum-take` cannot rebuild a history, only start a
          new one.
        * `overlay/` — the Enterprise-docs surface. `sync-enterprise-overlay.sh`
          CAN rebuild it, but only from a box that still reaches the source.
          After the outage a restore answers, that is the assumption one does
          not get to make.

        WHERE IT GOES (#2464). `stack_dir` is the `..:/stack:ro` mount (BSB-03):
        nothing may be written there except the two narrow rw binds. The old
        `tar -C /stack` died on `checksums.db` with "Read-only file system" on
        every box, and the full restore still said "Full Restore Complete.".
        So the archive is vetted and extracted into a TEMPORARY directory, then:

        * `checksums.db` -> `<stack_dir>/.checksums.db`, written IN PLACE (open,
          truncate, write): a single-file bind mount keeps its inode or it
          stops being the host's file (#1224).
          A backup `checksums.db` that is empty or not SQLite (every backup
          taken before the first --checksum-take: init.sh touches the file)
          never replaces a non-empty box baseline; that is logged, not failed.
        * `overlay/` -> `<stack_dir>/overlay/`, OVERWRITE-AND-KEEP-EXTRAS
          (#1621's merge semantics, `merge_tree`): what the backup has is
          written — a colliding box entry is removed first, never followed —
          and what only the box has stays. Staging the overlay is additive,
          and every backup carries an overlay/ (possibly empty), so a mirror
          would wipe Enterprise docs an air-gapped box cannot re-sync. New
          entries get the pre-restore owner of the mount root; the root itself
          is never chowned.

        A half the archive does not carry is left as it is on the box.

        Returns True when everything the archive carries is back, None when
        the backup carries no box-local archive (a backup older than #1621
        rev-D: nothing to restore, not a failure), and False when the archive
        is there and was NOT restored — that makes the full restore PARTIAL
        (exit 3).
        """
        enc_path = os.path.join(RESTORE_ROOT, "databases", "box-local-backup.tar.gz.enc")
        if not os.path.exists(enc_path):
            logger.info("No encrypted box-local archive in backup. Skipping.")
            return None

        lost = ("— .checksums.db (the governance baseline) and overlay/ are "
                "whatever this box already had. The checksum baseline cannot be "
                "rebuilt from anywhere else.")
        encryption_pass = _read_passphrase_from_env_file(env_path) \
            or os.environ.get('BACKUP_ENCRYPTION_PASSWORD') \
            or os.environ.get('AUTHENTIK_BOOTSTRAP_PASSWORD', '')
        if not encryption_pass:
            logger.error(
                "box-local restore FAILED: no encryption password available "
                "(the backup carries the box-local state, but it cannot be "
                "decrypted) %s", lost)
            return False

        try:
            dec = subprocess.run(
                ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
                 "-in", enc_path, "-pass", f"pass:{encryption_pass}"],
                capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            logger.error("box-local restore FAILED (decrypt: %s) %s",
                         (e.stderr or b"").decode("utf-8", "replace")[:300], lost)
            return False

        staging = tempfile.mkdtemp(prefix="box-local-restore-")
        try:
            try:
                names = extract_box_local_archive(dec.stdout, staging)
            except (UnsafeArchiveError, tarfile.TarError, OSError, EOFError, KeyError) as e:
                logger.error("box-local restore FAILED (archive refused: %s) %s", e, lost)
                return False

            failed = []
            if BOX_LOCAL_DB_MEMBER in names:
                target = os.path.join(stack_dir, ".checksums.db")
                staged_db = os.path.join(staging, BOX_LOCAL_DB_MEMBER)
                if not _is_sqlite_file(staged_db) and _is_nonempty_file(target):
                    # #2464 fix round 1: init.sh/upgrade.sh `touch` the file, so
                    # every backup taken before the first --checksum-take
                    # carries an EMPTY checksums.db. Writing it would truncate
                    # a real baseline to 0 bytes.
                    logger.warning(
                        "box-local: the backup's checksums.db is %s — keeping "
                        "this box's non-empty .checksums.db instead of "
                        "overwriting the baseline with it.",
                        "empty" if os.path.getsize(staged_db) == 0
                        else "not an SQLite database")
                else:
                    try:
                        write_file_in_place(staged_db, target)
                        logger.info("box-local: checksum baseline written in place to %s", target)
                    except OSError as e:
                        failed.append(f".checksums.db ({e})")
            else:
                logger.info("box-local: the backup carries no checksums.db; "
                            ".checksums.db left as it is.")

            src_overlay = os.path.join(staging, BOX_LOCAL_OVERLAY)
            if os.path.isdir(src_overlay) and not os.path.islink(src_overlay):
                target = os.path.join(stack_dir, BOX_LOCAL_OVERLAY)
                try:
                    merge_tree(src_overlay, target)
                    logger.info("box-local: overlay/ restored from the backup into %s "
                                "(overwrite, keep extras)", target)
                except (OSError, shutil.Error) as e:
                    failed.append(f"overlay/ ({e})")
            else:
                logger.info("box-local: the backup carries no overlay/; "
                            "overlay/ left as it is.")

            if failed:
                logger.error("box-local restore FAILED for %s %s",
                             ", ".join(failed), lost)
                return False
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        logger.info("box-local state restored.")
        return True

    def restore_partial(self, backup_file, target_item):
        """Restores a single volume or database from a full backup."""
        backup_path = os.path.join(BACKUP_DIR, backup_file)
        if not os.path.exists(backup_path):
            return False

        # F-002: Verify integrity before restoring
        if not self._verify_checksum(backup_path):
            logger.error("Aborting partial restore due to checksum mismatch.")
            return False

        logger.info(f"Restoring {target_item} from {backup_file}...")
        self.stop_stack() # Safer to stop everything to avoid inconsistencies, generally.

        # rc6.7 #7: same decrypt-then-extract dance as restore_full.
        decrypted_tmp = None
        outcome = True
        try:
            if is_encrypted_backup(backup_file):
                decrypted_tmp = decrypt_backup_to_temp(backup_path)
                archive_path = decrypted_tmp
            else:
                archive_path = backup_path

            with tarfile.open(archive_path, "r:gz") as tar:
                # We want to extract only 'backup/{target_item}'. rc6.7 #6:
                # archive members are stored as ABSOLUTE `/backup/...`, so
                # match against the leading-slash-stripped name (the same
                # normalisation applied below before extraction).
                member_prefix = f"backup/{target_item}"
                members = [m for m in tar.getmembers()
                           if m.name.lstrip("/").startswith(member_prefix)]

                if not members:
                    logger.error(f"Item {target_item} not found in backup.")
                    self.start_stack()
                    return False

                temp_dir = PARTIAL_RESTORE_DIR
                if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
                os.makedirs(temp_dir)

                # Safe partial extraction with path validation.
                # rc6.7 #6: same absolute-member-path normalisation as
                # safe_extract_tar — the archive stores `/backup/...`, so we
                # strip the leading slash in-place before the traversal check
                # and before extractall, otherwise os.path.join discards
                # temp_dir and the partial restore aborts on every backup.
                abs_temp = os.path.abspath(temp_dir)
                for member in members:
                    if os.path.isabs(member.name):
                        member.name = member.name.lstrip("/")
                    # #125: only normalise HARD-link targets (in-archive paths);
                    # preserve absolute SYMLINK targets verbatim. See the long
                    # note in safe_extract_tar — stripping `/media` → `media`
                    # turns `media -> /media` into a self-loop and breaks the
                    # authentik-media-migrator on restore.
                    if member.linkname and os.path.isabs(member.linkname) and member.islnk():
                        member.linkname = member.linkname.lstrip("/")
                    member_path = os.path.abspath(os.path.join(temp_dir, member.name))
                    if not member_path.startswith(abs_temp + os.sep) and member_path != abs_temp:
                        raise Exception(f"Path traversal attempt: {member.name}")
                tar.extractall(path=temp_dir, members=members)

                # Move
                src = os.path.join(temp_dir, "backup", target_item)
                dst = os.path.join(RESTORE_ROOT, target_item)

                if os.path.exists(dst) and os.path.exists(src):
                    # Symlinks as symlinks, like restore_full (rc6.7 #7): a
                    # container-absolute link (`localtime -> /usr/share/…`)
                    # must not be dereferenced on the manager's filesystem.
                    shutil.copytree(src, dst, symlinks=True,
                                    ignore_dangling_symlinks=True, dirs_exist_ok=True)
                    # #2542: copytree drops uid/gid; put the archive's back.
                    apply_archive_ownership(
                        dst, archive_owners(members, f"backup/{target_item}"),
                        f"volume {target_item}")

                # If it was a database dump folder, triggers DB restore
                if target_item == "databases":
                    if self.restore_databases() is not True:
                        # #2465: never report a partial DB replay as success.
                        raise RuntimeError(
                            "the databases were NOT restored completely")

        except Exception as e:
            logger.error(f"Partial restore failed: {e}")
            outcome = False
        finally:
            if decrypted_tmp and os.path.exists(decrypted_tmp):
                try:
                    os.remove(decrypted_tmp)
                    logger.info(f"Removed decrypted temp tarball: {decrypted_tmp}")
                except OSError as e:
                    logger.warning(f"Could not remove decrypted temp tarball {decrypted_tmp}: {e}")
            self.start_stack()
        return outcome

    @staticmethod
    def _find_tar_member(tar, name):
        """Find a tar member by exact, leading-slash-normalised name. Mirrors
        the `/backup/...`-vs-`backup/...` normalisation used elsewhere in
        this module (rc6.7 #6) — the archive stores members as absolute
        paths."""
        for m in tar.getmembers():
            if m.name.lstrip('/') == name:
                return m
        return None

    def _pre_restore_safety_dump(self, container_name, db_name, pg_user):
        """Pre-restore safety dump: capture db_name's CURRENT state before
        we touch it, so a bad restore is itself recoverable. Best effort — a
        fresh/nonexistent target db has nothing to dump."""
        safety_path = os.path.join(
            BACKUP_DIR,
            f"pre-restore-safety-{db_name}-"
            f"{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.dump",
        )
        try:
            with open(safety_path, 'wb') as sf:
                safety_result = subprocess.run(
                    ['docker', 'exec', container_name, 'pg_dump',
                     '-U', pg_user, '-Fc', db_name],
                    stdout=sf, stderr=subprocess.PIPE,
                )
            if safety_result.returncode != 0:
                stderr = safety_result.stderr
                stderr = stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else (stderr or '')
                logger.warning(
                    f"#279: pre-restore safety dump of {db_name!r} failed "
                    f"(db may not exist yet) — {stderr.strip()[:200]}"
                )
                try:
                    os.remove(safety_path)
                except OSError:
                    pass
            else:
                logger.info(f"#279: pre-restore safety dump written: {safety_path}")
        except Exception as e:
            logger.warning(f"#279: pre-restore safety dump step raised: {e}")

    def restore_single_database(self, backup_file, db_name):
        """#279: targeted single-database restore.

        Care Solutions incident (#278): a GPUStack version mishap corrupted
        ONLY gpustack_db, and there was no way to restore just that one
        database — the only options were a full-volume restore (reverts
        EVERYTHING, undoing unrelated work) or restore_databases() replaying
        the WHOLE pg_dumpall cluster dump. Neither is usable to fix one DB
        under incident pressure.

        Unlike restore_full/restore_partial, this deliberately does NOT call
        stop_stack()/start_stack() — the whole point is fixing ONE database
        without touching any other running service.

        Dispatch mirrors #188's per-cluster shape (postgres vs.
        postgres-komodo): prefer a real per-DB `pg_dump -Fc` dump if the
        archive carries one (dump_per_database_backups() writes these into
        databases/per-db/<db>.dump going forward) and pg_restore it; fall
        back to slicing the target database's `\\connect` section out of
        each pg_dumpall cluster dump in the archive (postgres_core.sql, then
        postgres_komodo.sql) for older archives, and psql-replay just that
        section.

        FAIL-SAFE (this moves real data): an invalid name, a target absent
        from every dump, or a target present in MORE than one dump
        (ambiguous — never guess) all abort BEFORE any docker exec runs —
        no DROP, no CREATE, no write, and the rest of the stack keeps
        running untouched.
        """
        if not db_name or not _SAFE_DB_NAME_RE.match(db_name):
            logger.error(f"#279: refusing to restore — invalid database name {db_name!r}.")
            return False
        if db_name in _IN_PLACE_DATABASES:
            # The restore drops and recreates its target from the maintenance
            # DB `postgres`, which cannot drop itself; template1 is the
            # template every CREATE DATABASE copies.
            logger.error(
                f"#279: refusing a single-database restore of {db_name!r} — it "
                f"cannot be dropped and recreated; use a full restore.")
            return False

        backup_path = os.path.join(BACKUP_DIR, backup_file)
        if not os.path.exists(backup_path):
            logger.error(f"Backup file {backup_file} not found.")
            return False

        if not self._verify_checksum(backup_path):
            logger.error("Aborting single-database restore due to checksum mismatch.")
            return False

        logger.info(
            f"#279: restoring database {db_name!r} from {backup_file} "
            f"(single-DB restore — the rest of the stack is left running)."
        )

        pg_user = os.environ.get('POSTGRES_USER', 'docker')
        decrypted_tmp = None
        try:
            if is_encrypted_backup(backup_file):
                decrypted_tmp = decrypt_backup_to_temp(backup_path)
                archive_path = decrypted_tmp
            else:
                archive_path = backup_path

            # Find the target — at most ONE dump may carry db_name.
            # (container_name, kind, payload_bytes, create_sql_or_None)
            candidates = []
            with tarfile.open(archive_path, "r:gz") as tar:
                per_db_name = f"backup/databases/{PER_DB_DUMP_SUBDIR}/{db_name}.dump"
                per_db_member = self._find_tar_member(tar, per_db_name)
                if per_db_member is not None:
                    fh = tar.extractfile(per_db_member)
                    candidates.append(("postgres", "pg_restore", fh.read(), None))
                else:
                    for dump_name, container_name in (
                        ("postgres_core.sql", "postgres"),
                        ("postgres_komodo.sql", "postgres-komodo"),
                    ):
                        member = self._find_tar_member(tar, f"backup/databases/{dump_name}")
                        if member is None:
                            continue
                        fh = tar.extractfile(member)
                        lines = fh.read().decode('utf-8', errors='replace') \
                            .splitlines(keepends=True)
                        # #2465 fix round 2: the full restore's refusals,
                        # before anything is dropped. A slice read to EOF of
                        # a truncated dump replays "successfully" (psql ends
                        # COPY at EOF with rc 0) with part of the rows.
                        try:
                            refuse_incomplete_dump_lines(lines)
                        except ValueError as e:
                            logger.error(
                                f"#279: {dump_name} in {backup_file} is not a "
                                f"complete pg_dumpall file ({e}) — aborting; "
                                f"nothing was dropped or restored.")
                            return False
                        section = extract_single_database_dump(None, db_name, lines=lines)
                        if section is not None:
                            candidates.append((
                                container_name, "psql", section.encode('utf-8'),
                                extract_database_create_section(None, db_name, lines=lines)))

            if not candidates:
                logger.error(
                    f"#279: database {db_name!r} not found in any dump in "
                    f"{backup_file} — aborting; nothing was restored."
                )
                return False
            if len(candidates) > 1:
                logger.error(
                    f"#279: database {db_name!r} found in MORE THAN ONE dump "
                    f"in {backup_file} (ambiguous target: "
                    f"{[c[0] for c in candidates]}) — aborting; nothing was "
                    f"restored. Refusing to guess which one is authoritative."
                )
                return False

            container_name, kind, payload, create_sql = candidates[0]

            try:
                target_ctr = client.containers.get(container_name)
            except docker.errors.NotFound:
                logger.error(
                    f"#279: target container {container_name!r} for database "
                    f"{db_name!r} not found on this box — aborting; nothing "
                    f"was restored."
                )
                return False

            if target_ctr.status != 'running':
                logger.info(f"#279: starting {container_name} for single-DB restore...")
                target_ctr.start()

            if not self._wait_for_postgres(container_name):
                logger.error(
                    f"#279: {container_name} did not become ready within 30 "
                    f"seconds — aborting; nothing was restored."
                )
                return False

            self._pre_restore_safety_dump(container_name, db_name, pg_user)

            # DROP, then CREATE, against the maintenance DB ('postgres') —
            # never against db_name itself (can't drop a database you're
            # connected to). Touches ONLY db_name; db_name is regex-validated
            # above (alnum + underscore only), so it cannot inject statements.
            # #2465 (fix round 1): these used to go as ONE `psql -c "DROP …;
            # CREATE …;"`, which PostgreSQL runs as a single implicit
            # transaction — DROP DATABASE refuses that ("cannot run inside a
            # transaction block"), so every single-DB restore aborted here.
            # They are separate statements on psql's stdin now, and the drop
            # first terminates the database's other sessions and uses
            # WITH (FORCE), exactly as the full restore does.
            verbose = b'\\set VERBOSITY verbose\n'
            drop_sql = verbose + (
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();\n"
                f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);\n').encode()
            rc, err = _stream_psql(self._psql_argv(container_name, _MAINTENANCE_DB, True),
                                   prefix=drop_sql)
            if rc != 0:
                logger.error(
                    f"#279: drop of {db_name!r} failed — aborting before any "
                    f"restore write; the database is unchanged: {_tail(err)}"
                )
                return False
            # The dump's own CREATE DATABASE keeps the backup's owner,
            # encoding and locale; without one (per-DB pg_dump -Fc, or a dump
            # that doesn't carry it) the database is created as before.
            if create_sql:
                create_bytes = verbose + create_sql.encode('utf-8')
            else:
                create_bytes = verbose + (
                    f'CREATE DATABASE "{db_name}" OWNER "{pg_user}";\n').encode()
            rc, err = _stream_psql(self._psql_argv(container_name, _MAINTENANCE_DB, True),
                                   prefix=create_bytes)
            if rc != 0:
                # #2465 fix round 2: the create part is CREATE DATABASE plus
                # ALTER DATABASE … OWNER TO <role>; the second fails when the
                # dump's role doesn't exist on this box, AFTER the database
                # was created. Ask the server which case this is.
                exists_rc, _ = _stream_psql(
                    self._psql_argv(container_name, _MAINTENANCE_DB, True),
                    prefix=(f"SELECT 1 / (SELECT count(*) FROM pg_database "
                            f"WHERE datname = '{db_name}')::int;\n").encode())
                if exists_rc == 0:
                    missing = sorted(set(re.findall(r'role "([^"]+)" does not exist', err)))
                    why = (f"role(s) {', '.join(missing)} do not exist on this box"
                           if missing else "a statement after CREATE DATABASE failed")
                    logger.error(
                        f"#279: {db_name!r} was recreated but is EMPTY and "
                        f"INCOMPLETE — its setup failed ({why}), so nothing "
                        f"was restored into it: {_tail(err)}"
                    )
                else:
                    logger.error(
                        f"#279: create of {db_name!r} failed after its drop — "
                        f"the database is GONE until a restore succeeds: "
                        f"{_tail(err)}"
                    )
                return False

            if kind == "pg_restore":
                # #279 hardening: --exit-on-error makes pg_restore return
                # non-zero on the FIRST statement failure instead of
                # continuing past it and exiting 0 — without this a
                # mid-stream error left the restore PARTIAL while still
                # being logged/reported as a success.
                restore_cmd = [
                    'docker', 'exec', '-i', container_name,
                    'pg_restore', '-U', pg_user, '-d', db_name,
                    '--no-owner', '--role', pg_user, '--exit-on-error',
                ]
            else:
                # #279 hardening: -v ON_ERROR_STOP=1 makes psql abort (and
                # return non-zero) on the first failing statement instead of
                # replaying the rest of the stream and exiting 0 regardless
                # — same partial-restore-reported-as-success risk as above.
                restore_cmd = self._psql_argv(container_name, db_name, True)

            restore_result = subprocess.run(restore_cmd, input=payload, capture_output=True)
            if restore_result.returncode != 0:
                stderr = restore_result.stderr
                stderr = stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else (stderr or '')
                logger.error(
                    f"#279: restore of {db_name!r} into {container_name} "
                    f"FAILED: {stderr.strip()[:500]}"
                )
                return False

            logger.info(
                f"#279: database {db_name!r} restored successfully from "
                f"{backup_file} into {container_name}."
            )
            return True

        except Exception as e:
            logger.error(f"#279: single-database restore of {db_name!r} failed: {e}")
            return False
        finally:
            if decrypted_tmp and os.path.exists(decrypted_tmp):
                try:
                    os.remove(decrypted_tmp)
                    logger.info(f"Removed decrypted temp tarball: {decrypted_tmp}")
                except OSError as e:
                    logger.warning(f"Could not remove decrypted temp tarball {decrypted_tmp}: {e}")

    def _psql_argv(self, container, db, on_error_stop):
        """#2465: psql inside ``container`` reading a script on stdin,
        connected to ``db``. -X: no psqlrc may change what the replay does.
        `-f -` (not a bare stdin redirect): only then does psql prefix every
        message with `psql:<stdin>:<line>:`, which is how
        restore_cluster_dump tells messages from continuation lines — a
        bare stdin prints `invalid command \\foo` with no prefix at all."""
        pg_user = os.environ.get('POSTGRES_USER', 'docker')
        return ['docker', 'exec', '-i', container, 'psql', '-X', '-U', pg_user,
                '-d', db, '-v', f"ON_ERROR_STOP={1 if on_error_stop else 0}",
                '-f', '-']

    def _wait_for_postgres(self, container, extra_args=()):
        """F-055: wait for readiness via pg_isready. True once ready."""
        pg_user = os.environ.get('POSTGRES_USER', 'docker')
        for attempt in range(30):
            result = subprocess.run(
                ['docker', 'exec', container, 'pg_isready', '-U', pg_user, *extra_args],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                logger.info(f"{container} is ready (attempt {attempt + 1}/30)")
                return True
            time.sleep(1)
        return False

    def restore_databases(self):
        """Finds restored SQL dumps and applies them.

        #2465: returns True only when every database in postgres_core.sql
        holds exactly the dumped contents (see restore_cluster_dump); False
        on any failure. The caller must not report success on False.
        """
        db_dump_dir = os.path.join(RESTORE_ROOT, "databases")
        if not os.path.exists(db_dump_dir):
            logger.warning("No database dumps found to restore.")
            return True

        ok = True
        # Start Postgres Container specifically for restore
        try:
            pg_user = os.environ.get('POSTGRES_USER', 'docker')

            # Postgres Core
            dump_file = os.path.join(db_dump_dir, "postgres_core.sql")
            if os.path.exists(dump_file):
                logger.info("Starting Postgres for DB restore...")
                client.containers.get("postgres").start()
                if not self._wait_for_postgres("postgres"):
                    logger.error(
                        "#2465: Postgres did not become ready within 30 seconds "
                        "— the databases were NOT restored.")
                    return False
                logger.info("Restoring Postgres Core (#2465: every database in "
                            "the dump is dropped and recreated from it)...")
                core_ok, failures = restore_cluster_dump(
                    dump_file,
                    lambda db, stop: self._psql_argv("postgres", db, stop),
                    label="postgres_core.sql",
                )
                if core_ok:
                    logger.info("Database restore completed successfully")
                else:
                    ok = False
                    logger.error(
                        f"#2465: Database restore FAILED — {len(failures)} "
                        f"problem(s); the databases do NOT hold the backup's "
                        f"contents completely: " + " | ".join(failures))
            else:
                logger.warning("#2465: no postgres_core.sql in the backup — "
                               "no core database was restored.")

            # Komodo (monitoring backend) — #188: the pre-backup hook dumps
            # postgres-komodo via `pg_dumpall` to postgres_komodo.sql and the
            # archive DOES carry it, but restore historically only replayed
            # postgres_core.sql, so on a full restore komodo came back empty
            # (its monitoring history/config was silently lost). Replay it
            # symmetrically into the postgres-komodo container.
            #
            # Guards:
            #  * only when the dump is actually in the archive (monitor profile
            #    was ON at backup time), and
            #  * only when a postgres-komodo container exists on THIS box
            #    (monitor profile ON here) — otherwise skip cleanly (NotFound),
            #    without tripping the outer error handler.
            komodo_dump = os.path.join(db_dump_dir, "postgres_komodo.sql")
            if os.path.exists(komodo_dump):
                try:
                    komodo_ctr = client.containers.get("postgres-komodo")
                except docker.errors.NotFound:
                    logger.info(
                        "postgres_komodo.sql present in backup but no "
                        "postgres-komodo container on this box (monitor profile "
                        "off) — skipping komodo DB restore."
                    )
                    komodo_ctr = None

                if komodo_ctr is not None:
                    logger.info("Starting postgres-komodo for DB restore...")
                    komodo_ctr.start()
                    # Wait for readiness (komodo's POSTGRES_DB is `postgres`).
                    komodo_ready = False
                    for attempt in range(30):
                        result = subprocess.run(
                            ['docker', 'exec', 'postgres-komodo',
                             'pg_isready', '-U', pg_user, '-d', 'postgres'],
                            capture_output=True, text=True
                        )
                        if result.returncode == 0:
                            komodo_ready = True
                            logger.info(
                                f"postgres-komodo is ready (attempt {attempt + 1}/30)"
                            )
                            break
                        time.sleep(1)
                    if not komodo_ready:
                        logger.error(
                            "postgres-komodo did not become ready within 30 "
                            "seconds. Proceeding anyway."
                        )

                    logger.info("Restoring Postgres Komodo...")
                    # postgres_komodo.sql is a `pg_dumpall` cluster dump (carries
                    # \connect + CREATE DATABASE/ROLE), so replay it against the
                    # default maintenance DB (`postgres`), mirroring the core path
                    # (local-socket trust — no PGPASSWORD needed).
                    with open(komodo_dump, 'r') as sql_file:
                        result = subprocess.run(
                            ['docker', 'exec', '-i', 'postgres-komodo',
                             'psql', '-U', pg_user, '-d', 'postgres'],
                            stdin=sql_file,
                            capture_output=True,
                            text=True
                        )
                        # #2465: psql without ON_ERROR_STOP exits 0 past
                        # failed statements, so rc 0 alone is not success.
                        # Komodo keeps its lenient replay (its data lives in
                        # the `postgres` DB next to FerretDB's own objects),
                        # but the errors are no longer reported as success.
                        komodo_errors = [
                            ln for ln in (result.stderr or '').splitlines()
                            if re.search(r':\s+ERROR:\s', ln)
                        ]
                        if result.returncode != 0:
                            logger.error(
                                f"Komodo database restore failed: {result.stderr}"
                            )
                        elif komodo_errors:
                            logger.error(
                                f"#2465: Komodo database restore applied with "
                                f"{len(komodo_errors)} error(s) — monitoring "
                                f"data may be incomplete: {komodo_errors[:5]}"
                            )
                        else:
                            logger.info(
                                "Komodo database restore completed successfully"
                            )

            logger.info("Database restore steps applied.")

        except Exception as e:
            logger.error(f"Database restore error: {e}")
            return False
        return ok

    def restore_env_files(self, env_path='/stack/.env'):
        """Decrypt and restore .env files from backup.

        BSB-04 / R-DEF-04: re-reads BACKUP_ENCRYPTION_PASSWORD from
        ``env_path`` (defaulting to /stack/.env, the bind-mounted host
        .env) at every call, NOT from os.environ — which is frozen at
        container start. Without this re-read, an operator who rotates
        the passphrase in .env and immediately attempts a restore would
        decrypt with the OLD container-start value silently. Mirrors the
        per-invocation .env read in `decrypt_backup_to_temp`.

        Falls back to os.environ['BACKUP_ENCRYPTION_PASSWORD'] (then
        AUTHENTIK_BOOTSTRAP_PASSWORD) if env_path is missing or empty,
        preserving the legacy behaviour for the "no-rotation" case and
        for test fixtures that don't ship a /stack/.env.
        """
        db_dump_dir = os.path.join(RESTORE_ROOT, "databases")
        stack_dir = "/stack"

        # Determine decryption password — prefer the .env file (re-read
        # at call time so a rotation just lands), fall back to
        # os.environ for backwards-compat.
        encryption_pass = _read_passphrase_from_env_file(env_path) \
            or os.environ.get('BACKUP_ENCRYPTION_PASSWORD') \
            or os.environ.get('AUTHENTIK_BOOTSTRAP_PASSWORD', '')
        if not encryption_pass:
            logger.warning("No encryption password available. Cannot restore .env files.")
            return

        env_files = [
            ("env-backup.enc", ".env"),
            ("env-dify-backup.enc", ".env.dify"),
        ]

        for enc_name, target_name in env_files:
            enc_path = os.path.join(db_dump_dir, enc_name)
            target_path = os.path.join(stack_dir, target_name)

            if not os.path.exists(enc_path):
                logger.info(f"No encrypted {target_name} found in backup. Skipping.")
                continue

            # #1224: {target_path} is a single-file bind (core/compose.yml) —
            # the same stale-inode class as the Portal's (#1189). `openssl
            # -out` truncates whatever inode the bind still points at: on a
            # stale bind that is the ORPHANED old file (the host never sees
            # the restored content) or EROFS. Check first; refuse with the
            # recovery instead of a misleading "password may have changed".
            state = inspect_env_file(target_path)
            if state['stale']:
                logger.error(
                    f"Not restoring {target_name}: {state['reason']}. Recreate this "
                    f"container once so it picks up the current host file — "
                    f"`{recreate_cmd(ENV_MOUNT_SERVICE)}` in the stack directory on "
                    f"the host — then run the restore again (#1224).")
                continue

            try:
                result = subprocess.run(
                    [
                        'openssl', 'enc', '-d', '-aes-256-cbc',
                        '-salt', '-pbkdf2', '-iter', '100000',
                        '-in', enc_path, '-out', target_path,
                        '-pass', f'pass:{encryption_pass}',
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    logger.info(f"Restored {target_name} from encrypted backup.")
                elif 'Read-only file system' in (result.stderr or ''):
                    # #1224: the dropped-bind variant surfaces here, not in
                    # the probe — say what it is, not "password changed".
                    logger.error(explain_stale(target_path, 'Read-only file system',
                                               ENV_MOUNT_SERVICE))
                else:
                    logger.warning(f"Failed to decrypt {target_name}: {result.stderr.strip()}. "
                                   "The encryption password may have changed since the backup was created.")
            except Exception as e:
                logger.warning(f"Error restoring {target_name}: {e}")


    #: 2026.09-ga.4 (#2531): the instance-keyed layout's per-instance file.
    AGENT_MANIFEST = "manifest.json"
    #: the only volumes a manifest may name: an AGENT volume (every name the
    #: provisioner creates starts `agent-`) or docker's 64-hex anonymous form,
    #: which E1 reuses (#2531 review: `postgres_data` must never pass).
    _AGENT_VOLUME_NAME_RE = re.compile(r"^(agent-[A-Za-z0-9_.-]+|[0-9a-f]{64})$")
    #: an instance directory is the instance id (a uuid) — it reaches a path
    #: and, through it, the helper's `sh -c`.
    _AGENT_INSTANCE_DIR_RE = re.compile(r"^[0-9a-f-]+$")
    #: what a tar path handed to the helper's `sh -c` may consist of.
    _AGENT_TAR_REL_RE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")

    def _agent_restore_plan_by_manifest(self, agent_root, manifests):
        """#2531: [(volume_name, tar_path)] from the per-instance manifests of
        a 2026.09-ga.4+ backup, plus the names of the entries that cannot be
        restored (invalid, skipped at backup time, or missing).

        Every archived volume goes back under the name it was TAKEN from (the
        manifest's `name`), never under a name rebuilt from slug + type + role:
        the container stem and the row's user_slug differ on real boxes, and
        a second instance of one type shares the first one's prefix."""
        plan, not_restored, seen = [], [], set()
        for manifest_path in manifests:
            inst_dir = os.path.dirname(manifest_path)
            inst = os.path.basename(inst_dir)
            if not self._AGENT_INSTANCE_DIR_RE.match(inst):
                logger.error(f"#2531: agent volume restore FAILED for agents/{inst!r}: "
                             "not an instance id — nothing restored from it.")
                not_restored.append(f"agents/{inst}")
                continue
            try:
                with open(manifest_path, encoding="utf-8") as fh:
                    manifest = json.load(fh)
                volumes = manifest.get("volumes")
                if manifest.get("instance_id") != inst or not isinstance(volumes, list):
                    raise ValueError("instance_id/volumes do not match the layout")
            except (OSError, ValueError, AttributeError) as e:
                logger.error(f"#2531: agent volume restore FAILED for {inst}: "
                             f"unreadable manifest ({e}).")
                not_restored.append(f"agents/{inst}")
                continue
            for vol in volumes:
                name = vol.get("name") if isinstance(vol, dict) else None
                if not isinstance(name, str) or not self._AGENT_VOLUME_NAME_RE.match(name):
                    logger.error(f"#2531: agent volume restore FAILED for {inst}: "
                                 f"{name!r} is not an agent volume name.")
                    not_restored.append(f"{inst}:{name!r}")
                    continue
                if vol.get("archived") is not True:
                    # Skipped when the backup was taken (disk ceiling, size
                    # cap, missing volume, snapshot failure): the instance's
                    # data is NOT all back — PARTIAL, by name (#2531 review).
                    logger.error(f"#2531: agent volume restore FAILED for {name} "
                                 f"({manifest.get('container_name')}): it is not in "
                                 "this backup — it was skipped when the backup was taken.")
                    not_restored.append(name)
                    continue
                tar_path = os.path.join(inst_dir, f"{name}.tar")
                if not os.path.isfile(tar_path):
                    logger.error(f"#2531: agent volume restore FAILED for {name}: "
                                 f"the manifest lists {tar_path}, which is missing.")
                    not_restored.append(name)
                    continue
                if name in seen:
                    logger.info(f"#2531: {name} is mounted by more than one "
                                f"instance; restored once (skipping {inst}'s copy).")
                    continue
                seen.add(name)
                plan.append((name, tar_path))
        # A directory without a manifest next to manifests is a leftover of an
        # older layout, not part of this backup: it is never restored over
        # the volumes the manifests name.
        for entry in sorted(os.listdir(agent_root)):
            d = os.path.join(agent_root, entry)
            if os.path.isdir(d) and not os.path.isfile(os.path.join(d, self.AGENT_MANIFEST)):
                logger.warning(f"#2531: ignoring agents/{entry}/ — no manifest in "
                               "an instance-keyed backup (a leftover of an older layout).")
        return plan, not_restored

    @staticmethod
    def _agent_restore_plan_legacy(agent_root):
        """M030 S4 (2026.09-ga.3 and older, no manifest): <slug>/<type>/<role>.tar →
        `agent-<type>-<slug>-<role>`, exactly as those releases restored it."""
        plan = []
        for slug in sorted(os.listdir(agent_root)):
            slug_dir = os.path.join(agent_root, slug)
            if not os.path.isdir(slug_dir):
                continue
            for agent_type in sorted(os.listdir(slug_dir)):
                type_dir = os.path.join(slug_dir, agent_type)
                if not os.path.isdir(type_dir):
                    continue
                for entry in sorted(os.listdir(type_dir)):
                    if not entry.endswith(".tar"):
                        continue
                    role = entry[:-4]
                    plan.append((f"agent-{agent_type}-{slug}-{role}",
                                 os.path.join(type_dir, entry)))
        return plan

    def restore_agent_volumes(self):
        """M030 S4: rehydrate per-user agent docker volumes from their tarballs.

        Source layouts (written by core/backup/pre-backup.sh §7):
          * 2026.09-ga.4+ (#2531), instance-keyed:
                /restore_targets/databases/agents/<instance_id>/manifest.json
                /restore_targets/databases/agents/<instance_id>/<volume>.tar
            Each archive is restored into the volume NAMED IN THE MANIFEST —
            the very volume the instance mounted — created with that exact
            name when the box does not have it (a fresh-init restore).
          * 2026.09-ga.3 and older (detected by the absence of any manifest):
                /restore_targets/databases/agents/<slug>/<type>/<role>.tar
            restored into `agent-<type>-<slug>-<role>` as before.

        rc6.7 #7: pre-backup.sh tars these into /backup/databases/agents/...
        (the `db-dumps` docker volume is MOUNTED at /backup/databases, so
        inside the archive — and therefore under /restore_targets after the
        volume copy — the directory is literally `databases/agents`, NOT
        `db-dumps/agents`). The old `db-dumps/agents` path never existed in
        the restored tree, so restore_agent_volumes() always reported "no
        agents/ subtree" and silently skipped EVERY per-user agent volume
        (hermes/moltis/coding-tools/...). Read from databases/agents.

        For each tarball: ensure the docker volume exists (create if not),
        then spawn an alpine container that mounts both source path and
        target volume, and `tar -xf` the contents in. Symmetric to the
        backup-side approach so the operation is doubly auditable. One
        failure does not abort the rest.

        Returns None (the backup carries no agent volumes), True (every
        archive restored) or False (at least one archive was NOT restored —
        #2531: that makes a full restore PARTIAL, exit 3, like certs/ and the
        box-local state; the databases and .env are back regardless).
        """
        agent_root = os.path.join(RESTORE_ROOT, "databases", "agents")
        if not os.path.isdir(agent_root):
            logger.info("M030 S4: no agents/ subtree in backup — skipping per-user agent restore.")
            return None

        manifests = sorted(
            os.path.join(agent_root, e, self.AGENT_MANIFEST)
            for e in os.listdir(agent_root)
            if os.path.isfile(os.path.join(agent_root, e, self.AGENT_MANIFEST)))
        if manifests:
            logger.info(f"#2531: instance-keyed agent backup — {len(manifests)} "
                        "instance manifest(s).")
            plan, not_restored = self._agent_restore_plan_by_manifest(agent_root, manifests)
        else:
            plan, not_restored = self._agent_restore_plan_legacy(agent_root), []
        if not plan and not not_restored:
            logger.info("M030 S4: no agent volume archives in backup — nothing to restore.")
            return None

        # rc6.7 #7: resolve the host-side docker volume that backs
        # /restore_targets/databases (the offen `db-dumps` volume). We must
        # mount THAT volume into the spawned alpine helper to reach the
        # tarballs — bind-mounting a /restore_targets/... path directly is
        # wrong: those paths exist only INSIDE this manager container, so the
        # daemon resolves them against the HOST fs (where they don't exist),
        # auto-creates an empty dir, and tar then fails with "invalid tar
        # magic". Find the volume name by inspecting our own mounts so we
        # don't hard-code the compose project prefix.
        dbdumps_volume = None
        try:
            inspect = subprocess.run(
                ["docker", "inspect", "-f",
                 '{{range .Mounts}}{{if eq .Destination "/restore_targets/databases"}}{{.Name}}{{end}}{{end}}',
                 os.environ.get("HOSTNAME", "razzfazz-backup-management")],
                capture_output=True, text=True, check=False,
            )
            dbdumps_volume = inspect.stdout.strip() or None
        except Exception as e:
            logger.warning(f"M030 S4: could not resolve db-dumps volume name: {e}")
        if not dbdumps_volume:
            logger.error(
                "M030 S4: agent volume restore FAILED — could not resolve the "
                "db-dumps host volume, so the tarballs are unreachable from a "
                f"spawned helper container ({len(plan)} volume(s) NOT restored).")
            return False

        tar_image = agent_tar_image()
        if not tar_image:
            logger.error(f"#2531: agent volume restore FAILED — no GNU tar helper "
                         f"({len(plan)} volume(s) NOT restored).")
            return False

        restored = 0
        for volume_name, tar_path in plan:
            # The tar path goes into the helper's `sh -c` inside quotes: only
            # plain path characters, no `..` (#2531 review; the legacy layout's
            # directory names are not validated anywhere else).
            rel_in_volume = os.path.relpath(tar_path, os.path.join(RESTORE_ROOT, "databases"))
            if (not self._AGENT_TAR_REL_RE.match(rel_in_volume)
                    or ".." in rel_in_volume.split("/")):
                logger.error(f"#2531: agent volume restore FAILED for {volume_name!r}: "
                             f"unsafe archive path {rel_in_volume!r}.")
                not_restored.append(volume_name)
                continue
            # Ensure the volume exists (idempotent). #2531: under the exact
            # name the archive was taken from.
            create = subprocess.run(
                ["docker", "volume", "create", "--name", volume_name],
                capture_output=True, text=True, check=False,
            )
            if create.returncode != 0:
                logger.warning(
                    f"M030 S4: failed to ensure volume {volume_name}: {create.stderr.strip()}"
                )
                not_restored.append(volume_name)
                continue

            # Extract the tarball into the target volume. rc6.7 #7:
            # mount the db-dumps host volume (read-only) at /dumps and
            # the target agent volume rw at /restore, then read the
            # tar by its path RELATIVE to the db-dumps volume root
            # (databases/agents/... is mounted at /restore_targets/
            # databases, i.e. <volume-root>/agents/...). This reaches
            # the real tar on the host instead of a non-existent
            # /restore_targets/... path. tar runs as root inside the
            # spawned container. --strip-components is not needed
            # because the tarball was created with `tar -C /data -cf
            # ... .`, so contents are flat under tar root.
            extract = subprocess.run([
                "docker", "run", "--rm",
                "-v", f"{dbdumps_volume}:/dumps:ro",
                "-v", f"{volume_name}:/restore",
                "--user", "0:0",
                "--entrypoint", "sh",
                tar_image,
                "-c", f"tar -C /restore --numeric-owner --sparse -xf '/dumps/{rel_in_volume}' 2>&1",
            ], capture_output=True, text=True, check=False)

            if extract.returncode == 0:
                restored += 1
                logger.info(f"M030 S4: restored {volume_name} from {tar_path}")
            else:
                not_restored.append(volume_name)
                logger.warning(
                    f"M030 S4: tar -x failed for {volume_name}: "
                    f"{(extract.stderr or extract.stdout or '').strip()[:200]}"
                )

        logger.info(
            f"M030 S4: per-user agent volume restore complete — "
            f"{restored} restored, {len(not_restored)} failure(s)."
        )
        if not_restored:
            logger.error(f"#2531: agent volume restore FAILED — NOT restored: "
                         f"{', '.join(not_restored)} (see the lines above).")
            return False
        return True


    @staticmethod
    def _empty_dir_contents(path):
        """#125: remove everything INSIDE `path` without removing `path` itself.

        Used to clear a restore-target volume before copying the backup in, so a
        restore REPLACES rather than MERGES over pre-existing data (the fresh-
        init-then-restore-older-backup DR path). We must not remove the mountpoint
        dir itself — docker owns it and removing it would detach the bind mount.
        """
        for entry in os.listdir(path):
            full = os.path.join(path, entry)
            try:
                if os.path.islink(full) or os.path.isfile(full):
                    os.unlink(full)
                elif os.path.isdir(full):
                    shutil.rmtree(full)
            except Exception as e:
                # Re-raise so the caller records this as a volume-restore failure
                # (clearing must succeed for a clean restore).
                raise RuntimeError(f"could not clear {full}: {e}") from e

    def stop_stack(self):
        """Stops all relevant containers."""
        # We should exclude ourselves ("razzfazz-backup-management") and "postgres" (if we need it for restore, although we start it manually)
        # Using a list of known services or just excluding self
        my_hostname = os.environ.get("HOSTNAME") 
        for container in client.containers.list():
            # Don't kill self (check name or hostname match)
            if container.name == "razzfazz-backup-management" or container.name == my_hostname: continue
            if container.name == "backup-service": continue
            # docker-socket-proxy is how this manager reaches the Docker API. Stopping it
            # severs our OWN connection mid-stop → the very next Docker call aborts with
            # RemoteDisconnected and the whole restore/backup fails (data never restored).
            # It holds no state; leave it running through the stop sweep.
            if container.name == "docker-socket-proxy": continue

            logger.info(f"Stopping {container.name}...")
            container.stop()

    def start_stack(self):
        """Restarts the stack.

        #125 (cross-secret-boundary DR): `container.start()` (== `docker start`)
        re-launches EXISTING containers with their PRE-restore baked environment
        — it does NOT re-read the freshly-restored `.env`. After a boundary-
        crossing restore (fresh init generated new DB secrets, then we restore an
        OLDER backup whose postgres volume carries the OLD password), every
        dependent container keeps the new init's password in its env while
        postgres serves the restored volume's old password → universal
        `password authentication failed` → permanent crash loop. The authoritative
        bring-up is therefore `docker compose up -d --force-recreate`, run HOST-SIDE
        by `razzfazz-backup.sh restore` AFTER this manager returns — recreate
        re-reads `.env`, start does not, and the host has the compose project +
        restored `.env` while this in-container manager does not.

        When the host wrapper is driving (it sets RAZZFAZZ_RESTORE_HOST_RECREATE=1
        on the `docker exec`), we SKIP this in-container start entirely so we don't
        race the host's force-recreate or briefly bring services up on the stale
        env. For the bare `docker exec ... backup_manager.py restore` path (no host
        wrapper), we fall back to the legacy `docker start` sweep so the operator
        is not left with a fully-stopped stack — but note this path canNOT apply a
        changed `.env` (documented limitation; prefer `razzfazz-backup.sh restore`).
        """
        if os.environ.get("RAZZFAZZ_RESTORE_HOST_RECREATE") == "1":
            logger.info(
                "start_stack: host wrapper will run "
                "`docker compose up -d --force-recreate` (re-reads restored .env, "
                "#125) — skipping in-container `docker start` bring-up."
            )
            return

        logger.warning(
            "start_stack: bringing the stack back with `docker start` (legacy "
            "in-container path). This re-uses each container's PRE-restore "
            "environment and will NOT apply a restored .env that changed secrets "
            "(#125). For cross-secret-boundary restores use "
            "`razzfazz-backup.sh restore`, which force-recreates host-side."
        )

        # This is tricky because 'docker start' only works on existing containers.
        # Docker Compose 'up' is better but we are in a container.
        # We will iterate over all *stopped* containers that belong to our project and start them.
        # Assuming project name 'razzfazz-service-stack' (or inferred)

        # Helper: Restart known core services first
        ordered = ["postgres", "valkey", "authentik-server", "authentik-worker", "caddy"]
        
        for name in ordered:
            try:
                c = client.containers.get(name)
                c.start()
                logger.info(f"Started {name}")
            except Exception:
                pass

        # Start others
        # Filter for containers that are part of the 'razzfazz-stack' compose project
        for container in client.containers.list(all=True):
            if container.status != 'running':
                # Check for Docker Compose project label
                project = container.labels.get('com.docker.compose.project', '')
                if project == 'razzfazz-stack':
                    try:
                        container.start()
                        logger.info(f"Started {container.name}")
                    except Exception as e:
                       logger.error(f"Failed to start {container.name}: {e}")

def main(argv=None):
    """CLI entry. #2465: returns the exit code — a failed restore exits
    non-zero, so cli/backup.sh and the config portal hear about it."""
    bm = BackupManager()
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['list', 'backup', 'restore', 'delete'])
    # #279: 'single' targets one database (--database <name>); it never
    # stops the rest of the stack. See restore_single_database().
    parser.add_argument('--type', choices=['full', 'partial', 'single'], default='full')
    parser.add_argument('--target', help='Specific volume or file to Restore/Backup')
    parser.add_argument('--file', help='Backup file to restore from or delete')
    parser.add_argument('--database', help='#279: database name for --type single restore')

    # #2464 fix round 2: argparse exits 2 on a usage error (`--file -h`,
    # `--type bogus`); 2 must never reach a caller that could read it as
    # anything but a failure. Every usage error — and --help — is exit 1.
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    if args.action == 'list':
        for f in bm.list_backups(): print(f)
    elif args.action == 'delete':
        if args.file:
            bm.delete_backup(args.file)
        else:
            print("Error: --file argument is required for delete action.")
    elif args.action == 'backup':
        if args.type == 'full':
            bm.trigger_full_backup()
        else:
            bm.trigger_partial_backup(args.target)
    elif args.action == 'restore':
        if args.type == 'full':
            # #2464 fix round 1/2: 0 complete, 3 PARTIAL (databases and .env
            # restored, certs/ or box-local state not — cli/backup.sh still
            # recreates the stack and warns), 1 failed.
            result = bm.restore_full(args.file)
            if result is True:
                return 0
            if result == RESTORE_PARTIAL:
                return EXIT_RESTORE_PARTIAL
            return 1
        elif args.type == 'single':
            if not args.database:
                print("Error: --database is required for --type single restore.")
                return 1
            if not args.file:
                print("Error: --file (the backup to restore from) is required for --type single restore.")
                return 1
            ok = bm.restore_single_database(args.file, args.database)
            return 0 if ok else 1
        else:
            return 0 if bm.restore_partial(args.file, args.target) is True else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
