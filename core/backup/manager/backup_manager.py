# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
import errno
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
import zlib
import tarfile
import tempfile
import docker
import shutil
import subprocess
import hashlib
from datetime import datetime

from razzfazz_common.backup_keyring import KEYRING_KEYS, keyring_entries
from razzfazz_common import compose_local
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


#: files written next to an archive (#2713): removed together with it
BACKUP_SIDECAR_SUFFIXES = ('.sha256',)


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


def _read_env_keys(env_path, keys):
    """#2132: the BSB-04 parser of `_read_passphrase_from_env_file`, for several
    keys at once (first match per key; no `source`)."""
    keys = set(keys)
    out = {}
    if not env_path or not os.path.isfile(env_path):
        return out
    try:
        with open(env_path) as f:
            for line in f:
                k, sep, raw = line.partition('=')
                if sep and k in keys and k not in out:
                    out[k] = re.sub(r'\s+#.*$', '', raw).rstrip('\n').strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def keyring_from_env_file(env_path):
    """#2132: [(key, passphrase)], current first, then the previous generations."""
    return keyring_entries(_read_env_keys(env_path, KEYRING_KEYS))


def restore_passphrases(env_path, first=None):
    """#2132: every passphrase a restore may try, in order: the one that opened
    the archive, the current value through the BSB-04 reader (the seam the
    #1224/BSB-04 suites patch), the keyring in .env, then the process env
    (frozen at container start) as the last fallback, as before."""
    out = []
    for v in ([first] if first else []) \
            + [_read_passphrase_from_env_file(env_path)] \
            + [v for _, v in keyring_from_env_file(env_path)] \
            + [os.environ.get('BACKUP_ENCRYPTION_PASSWORD', ''),
               os.environ.get('AUTHENTIK_BOOTSTRAP_PASSWORD', '')]:
        if v and v not in out:
            out.append(v)
    return out


def _is_gzip(data):
    return data[:2] == b'\x1f\x8b'


def archive_key_entry(path, entries):
    """#2132: the (key, passphrase) keyring entry that opens `path`, or None.
    Reads two plaintext bytes (the gzip magic) and stops gpg."""
    for key, pw in entries:
        try:
            proc = subprocess.Popen(
                ['gpg', '--batch', '--quiet', '--pinentry-mode', 'loopback',
                 '--passphrase-fd', '0', '--decrypt', path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return None
        try:
            try:
                proc.stdin.write(pw.encode('utf-8'))
                proc.stdin.close()
            except BrokenPipeError:
                pass
            head = proc.stdout.read(2)
        finally:
            proc.kill()
            proc.wait()
        if _is_gzip(head):
            return key, pw
    return None


def openssl_decrypt_first(enc_path, passphrases, accept):
    """#2132: decrypt an `openssl enc` member with the first passphrase whose
    plaintext `accept` takes (a wrong one fails, or ~1/256 'succeeds' with
    garbage that `accept` rejects). Returns (plaintext, None) or (None, why)."""
    why = "no passphrase available"
    for pw in passphrases:
        try:
            dec = subprocess.run(
                ['openssl', 'enc', '-d', '-aes-256-cbc', '-salt', '-pbkdf2', '-iter', '100000',
                 '-in', enc_path, '-pass', 'stdin'],
                input=(pw + '\n').encode('utf-8'), capture_output=True)
        except OSError as e:
            return None, str(e)
        if dec.returncode == 0 and accept(dec.stdout):
            return dec.stdout, None
        why = (dec.stderr or b'').decode('utf-8', 'replace').strip()[:200] \
            or "decrypted, but not the expected content"
    return None, why


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

    #2132: see decrypt_backup_with_keyring.
    """
    return decrypt_backup_with_keyring(encrypted_path, env_path)[0]


def decrypt_backup_with_keyring(encrypted_path, env_path='/stack/.env'):
    """#2132: decrypt with the current passphrase, then each earlier generation
    (BACKUP_ENCRYPTION_PASSWORD_PREVIOUS_1..3). Returns (tmp_path, passphrase).
    gpg fails fast on a wrong passphrase, so a miss costs no archive read."""
    import tempfile
    name = os.path.basename(encrypted_path)
    if not os.path.isfile(env_path):
        raise RuntimeError(f"Could not read BACKUP_ENCRYPTION_PASSWORD from {env_path}: "
                           "no such file")
    entries = keyring_from_env_file(env_path)
    if not entries:
        raise RuntimeError(
            f"BACKUP_ENCRYPTION_PASSWORD is empty in {env_path}; cannot decrypt {name}. "
            f"The backup was encrypted at create-time, but the passphrase needed to "
            f"decrypt it is no longer present in .env. Restore the value (it should "
            f"equal AUTHENTIK_BOOTSTRAP_PASSWORD on a default install) and retry.")
    fd, tmp_path = tempfile.mkstemp(prefix='razzfazz-restore-', suffix='.tar.gz')
    os.close(fd)
    tried = []
    try:
        for i, (key, passphrase) in enumerate(entries):
            logger.info(f"Decrypting {name} with {key} → {tmp_path} ...")
            proc = subprocess.run(
                ['gpg', '--batch', '--yes', '--quiet', '--pinentry-mode', 'loopback',
                 '--passphrase-fd', '0', '--decrypt', '--output', tmp_path, encrypted_path],
                input=passphrase.encode('utf-8'), capture_output=True, timeout=600)
            if proc.returncode == 0:
                # Name the key that opened the archive, every time: an operator who
                # rotated passwords needs to see WHICH generation this archive wants.
                logger.info(f"{name} opened with {key} "
                            f"({'the current backup password' if not i else f'kept generation {i}'}).")
                if i:
                    logger.warning(f"{name} opened with {key}: it was sealed before "
                                   "the last passphrase rotation.")
                logger.info(f"Decryption complete; temp tarball at {tmp_path} "
                            f"({os.path.getsize(tmp_path) / (1024**3):.2f} GiB)")
                return tmp_path, passphrase
            tried.append(f"{key}: exit {proc.returncode}: "
                         f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
        raise RuntimeError(
            f"gpg --decrypt failed on {name} with every passphrase in the keyring "
            f"({'; '.join(tried)}). The passphrase that sealed it is not in .env any more"
            f": put it into BACKUP_ENCRYPTION_PASSWORD_PREVIOUS_1 and retry.")
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
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
            logger.error("could not read the postgres container's image — no GNU "
                         f"tar helper for the agent volumes (set {AGENT_TAR_IMAGE_ENV}).")
            return None
        v = subprocess.run(["docker", "run", "--rm", "--entrypoint", "tar", img, "--version"],
                           capture_output=True, text=True, check=False)
    except OSError as e:   # FileNotFoundError: no docker CLI — PARTIAL, not a crash
        logger.error(f"could not run docker to find the GNU tar helper: {e}")
        return None
    first = ((v.stdout or v.stderr or "").splitlines() or [""])[0]
    if v.returncode != 0 or "GNU tar" not in first:
        logger.error(f"the helper image {img} has no GNU tar ({first!r}) — a "
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

# A database name the restore accepts. PostgreSQL allows almost anything as
# a quoted identifier, and the stack has such names: Cognee, with per-dataset
# access control (ENABLE_BACKEND_ACCESS_CONTROL=true, VECTOR_DB_PROVIDER=
# pgvector), creates one pgvector database per dataset, named by the dataset's
# UUID (`abd872e5-41df-5f9a-9c24-ba879be5a837`) — one more with every new
# dataset. So any name is accepted except one holding a control character
# (NUL, a newline: it cannot sit on one line of a dump or a log), a double
# quote or a backslash (the quoting layers of psql's \connect and of a libpq
# connection string), or longer than PostgreSQL's 63 bytes. Every use of a
# name is quoted: quote_ident() in SQL, quote_literal() for a value,
# pg_dbname_arg() for a client's -d. A refused name skips only that database.
# \A…\Z, not ^…$: `$` also matches before a trailing newline, so
# "postgres\n" would pass (#2465 fix round 2).
_SAFE_DB_NAME_RE = re.compile(r'\A[^\x00-\x1f\x7f"\\]+\Z')
_PG_NAME_MAX_BYTES = 63
# Names a client takes verbatim as a database name: no quoting needed.
_PLAIN_DB_NAME_RE = re.compile(r'\A[A-Za-z0-9_]+\Z')


def db_name_problem(name):
    """Why ``name`` is refused as a database name, or None when it is
    accepted (see _SAFE_DB_NAME_RE)."""
    if not isinstance(name, str) or not name:
        return "the name is empty"
    if not _SAFE_DB_NAME_RE.match(name):
        return ("the name holds a control character, a double quote or a "
                "backslash")
    if len(name.encode('utf-8')) > _PG_NAME_MAX_BYTES:
        return f"the name is longer than {_PG_NAME_MAX_BYTES} bytes"
    return None


def quote_ident(name):
    """``name`` as an SQL identifier: always double-quoted, `"` doubled."""
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value):
    """``value`` as an SQL string literal (standard_conforming_strings):
    `'` doubled. Accepted names hold no backslash."""
    return "'" + value.replace("'", "''") + "'"


def pg_dbname_arg(name):
    """The value for a PostgreSQL client's ``-d``: libpq reads any ``-d``
    holding `=` (or starting `postgresql://`) as a connection string, so a
    name that isn't plain goes as ``dbname='…'`` with `\\` and `'` escaped."""
    if _PLAIN_DB_NAME_RE.match(name):
        return name
    return "dbname='" + name.replace('\\', '\\\\').replace("'", "\\'") + "'"


try:
    client = docker.DockerClient(base_url=DOCKER_SOCK)
except docker.errors.DockerException:
    # No reachable docker socket (e.g. unit-test import off-box / non-docker
    # user). The backup container always has it; tests mock `client`.
    client = None


_PER_DB_FILE_RE = re.compile(r'\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z')


def per_db_dump_filename(name):
    """``<name>.dump`` for the per-DB dump directory, or None when the name
    can't be a file name there (a `/`, a leading dot, anything beyond
    letters, digits, `_`, `.`, `-`). A UUID-named database is fine."""
    if not isinstance(name, str) or not _PER_DB_FILE_RE.match(name) or db_name_problem(name):
        return None
    return f"{name}.dump"


def _file_safe(name):
    """``name`` for use inside a file name: anything but [A-Za-z0-9_.-] as `_`."""
    return re.sub(r'[^A-Za-z0-9_.-]', '_', name).lstrip('.') or '_'


def _inside(root_real, p):
    return p == root_real or p.startswith(root_real + os.sep)


#: M-10 / I-08: a partial-restore target is ONE volume name — a single path
#: component, never `.`/`..`, never a path below a volume.
_RESTORE_TARGET_RE = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z')


class UnknownRestoreTargetError(ValueError):
    """M-10 / I-08: the partial-restore target is not a mounted volume."""


def mounted_restore_target(name):
    """M-10 / I-08: the path of the mounted restore target `name`, or
    UnknownRestoreTargetError. Only the volumes this container mounts under
    RESTORE_ROOT (core/compose.yml) qualify: `name` must be a single path
    component, and RESTORE_ROOT/<name> a real directory — not a symlink, not
    a path inside another volume (a service could plant a link there)."""
    if not isinstance(name, str) or not _RESTORE_TARGET_RE.match(name):
        raise UnknownRestoreTargetError(
            f"refusing the restore target {name!r}: not a volume name")
    path = os.path.join(RESTORE_ROOT, name)
    try:
        st = os.lstat(path)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode):
        raise UnknownRestoreTargetError(
            f"refusing the restore target {name!r}: not a volume mounted under "
            f"{RESTORE_ROOT}")
    return path


def normalise_member_names(members):
    """rc6.7 #6 / #125: the backup tars `/backup` with its leading slash, so a
    member is `/backup/<vol>/…`. Strip it from member names and from HARD-link
    targets (in-archive paths); a symlink's target is data and stays verbatim
    (`media -> /media`). Stripping can only move a member INTO the root.
    Idempotent: the dry pass and the extraction both call it."""
    for m in members:
        if os.path.isabs(m.name):
            m.name = m.name.lstrip("/")
        if m.islnk() and m.linkname and os.path.isabs(m.linkname):
            m.linkname = m.linkname.lstrip("/")
    return members


def _member_parts(name, what="member"):
    """#2678: the components of a normalised member path. An absolute path or
    a `..` component is refused outright."""
    if name.startswith("/"):
        raise UnsafeArchiveError(f"absolute {what} path: {name}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise UnsafeArchiveError(f"'..' in {what} path: {name}")
    return parts


def validate_archive_members(members):
    """#2678: the dry pass. Judges every member by the rules extract_confined
    enforces, from the member list alone, in archive order — so a restore can
    refuse an archive BEFORE it stops a single service or touches a volume.

    The rules (#2633's goal — nothing is written outside the destination —
    without its false refusals):
      * an absolute member path or a `..` component is refused;
      * a device or FIFO member is refused;
      * a SYMLINK is a leaf: it is extracted as a symlink, whatever its
        target (`.venv/bin/python -> /usr/bin/python3.12`, `python3 ->
        python`, `lib64 -> lib`, `media -> /media`). It is never followed
        during the extraction, so its target does not matter;
      * what is refused is a write THROUGH a link: a member below a path an
        earlier member made a symlink, or a non-symlink member over one;
      * a HARDLINK must name a file or symlink an earlier member extracted,
        by a path inside the root that passes through no symlink;
      * a non-directory over a directory, a directory over a file, and a
        member below a file are refused (the extraction would fail on them
        after the stack is down)."""
    kinds = {}
    for m in members:
        if m.isdev():  # character, block and FIFO
            raise UnsafeArchiveError(f"refusing a device/FIFO member: {m.name}")
        parts = tuple(_member_parts(m.name))
        if not parts:
            if m.isdir():
                continue
            raise UnsafeArchiveError(f"member names the destination itself: {m.name!r}")
        _refuse_a_lexical_link_on_the_way(kinds, parts, m.name)
        prior = kinds.get(parts)
        if prior == "sym" and not m.issym():
            raise UnsafeArchiveError(f"member is written over a symlink: {m.name}")
        if prior == "dir" and not m.isdir():
            raise UnsafeArchiveError(f"member replaces a directory: {m.name}")
        if prior == "file" and m.isdir():
            raise UnsafeArchiveError(f"directory member over a file: {m.name}")
        if m.islnk():
            try:
                target = tuple(_member_parts(m.linkname, "hardlink target"))
            except UnsafeArchiveError as e:
                raise UnsafeArchiveError(
                    f"hardlink escapes the destination: {m.name} -> {m.linkname} ({e})") from None
            _refuse_a_lexical_link_on_the_way(kinds, target, m.name)
            kind = kinds.get(target)
            if kind not in ("file", "sym"):
                raise UnsafeArchiveError(
                    f"hardlink escapes the destination: {m.name} -> {m.linkname} "
                    "(not a file an earlier member extracted)")
        else:
            kind = "sym" if m.issym() else "dir" if m.isdir() else "file"
        _refuse_a_bad_backup_layout(parts, kind, m.name)
        for i in range(1, len(parts)):
            kinds.setdefault(parts[:i], "dir")
        kinds[parts] = kind


#: #125: volumes whose restore failure fails the whole restore (a "success"
#: with a half-restored database is worse than a loud error).
CRITICAL_VOLUMES = frozenset({"postgres-data", "databases"})

#: #2632 review: a partial restore of one of these replaces what EVERY
#: service's database holds (the dumps are replayed, or a cluster's datadir
#: is swapped under its clients), so every running service is a consumer.
WHOLE_STACK_VOLUMES = CRITICAL_VOLUMES | {"postgres-komodo-data"}

#: #2712 review: volumes older backups archived that this version neither
#: archives nor restores — retired with the components that used them (the
#: hermes / moltis / coding-tools agents became per-user agents, falkordb and
#: yacht were removed). One in a backup is "not restored, volume retired": no
#: data the stack still uses is missing, so it is not PARTIAL.
RETIRED_VOLUMES = frozenset({
    "hermes-data", "moltis-config", "moltis-data", "coding-tools-workspace",
    "falkordb-data", "yacht-data",
})

#: #2712 review: volumes still in use that are deliberately NOT part of the
#: backup; an older backup that carried one is not restored over the live
#: one. name -> why, quoted in the restore's log line.
NOT_ARCHIVED_BY_DESIGN = {
    "razzfazz-config-data": ("not part of the backup by design (log snapshots, live "
                             "SQLite); the role ledger is restored from "
                             "razzfazz-rbac-data"),
}


#: #3450: volumes whose service keeps running through the backup are archived
#: from the pre-backup hook's copy (core/backup/stage-live-volumes.sh), never
#: live: a file growing under offen's tar aborts the whole archive.
#: volume -> where its copy sits below the archive's `backup/`. An archive made
#: before the copy existed holds the volume at `backup/<volume>/`, as before.
STAGED_VOLUMES = {
    "wazuh-manager-data": "databases/wazuh-manager-data",
}
_STAGED_IN_DATABASES = frozenset(
    rel.split("/", 1)[1] for rel in STAGED_VOLUMES.values() if rel.startswith("databases/"))

#: #3478: stage-live-volumes.sh writes `<copy>.staging-error` (reason + counts)
#: beside a copy it could not make completely, or in place of one it could not
#: make at all. A restore then keeps the live volume unless this is set to 1.
STAGING_ERROR_SUFFIX = ".staging-error"
ALLOW_INCOMPLETE_STAGED_ENV = "RAZZFAZZ_RESTORE_ALLOW_INCOMPLETE_STAGED"


def _staging_marker_reason(text):
    """The marker's `reason=` line (the whole first line if there is none)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for l in lines:
        if l.startswith("reason="):
            return l[len("reason="):][:600] or "no reason recorded"
    return (lines[0][:600] if lines else "no reason recorded")


def read_staging_marker_file(path):
    """#3478: the reason in an extracted marker, or None when there is none."""
    if not os.path.lexists(path):
        return None
    if os.path.islink(path) or not os.path.isfile(path):
        return "the staging marker in the backup is not a regular file"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return _staging_marker_reason(f.read(8192))
    except OSError as e:
        return f"the staging marker could not be read ({e.strerror})"


def incomplete_staged_verdict(vol_name, reason, copy_present):
    """#3478: what a restore does with a volume whose staged copy is marked.

    Returns None when the copy is restored anyway (the override is set; an
    ERROR is logged all the same), else the reason the volume is NOT restored
    (the caller leaves the live volume untouched). Never silent."""
    if not copy_present:
        logger.error(f"{vol_name}: NOT in this backup — copying it failed when the "
                     f"backup was made ({reason}). The live volume is left as it is.")
        return f"not in this backup: copying it failed when the backup was made ({reason})"
    if os.environ.get(ALLOW_INCOMPLETE_STAGED_ENV) == "1":
        logger.error(f"{vol_name}: restoring an INCOMPLETE copy because "
                     f"{ALLOW_INCOMPLETE_STAGED_ENV}=1 is set ({reason}). Files may be "
                     "missing or cut off; agents whose keys are missing must re-enrol.")
        return None
    logger.error(f"{vol_name}: the copy in this backup is INCOMPLETE ({reason}). The "
                 "live volume is NOT replaced with it. To restore the incomplete copy "
                 f"anyway, set {ALLOW_INCOMPLETE_STAGED_ENV}=1 for the restore.")
    return (f"its copy in the backup is incomplete ({reason}); not restored, the live "
            f"volume is kept ({ALLOW_INCOMPLETE_STAGED_ENV}=1 restores it anyway)")


def _refuse_a_bad_backup_layout(parts, kind, name):
    """#2678 review I-1 / M-2: the backup's own layout. `backup` must be a
    directory and no `backup/<volume>` a symlink — the full restore copies a
    volume with copytree, which follows a top-level link (`backup/vol ->
    <anywhere>` copied <anywhere> into the live volume; `backup ->
    /restore_targets` emptied every volume). A regular FILE at
    `backup/<name>` is not copied (the volume loop skips it), as before.
    And `backup/databases/` holds only what pre-backup.sh writes there —
    dumps, `.enc` files, agent tars, all regular files — which the restore
    later OPENS by name: a symlink there would be read through."""
    if parts[0] != "backup":
        return
    if (len(parts) == 1 and kind != "dir") or (len(parts) == 2 and kind == "sym"):
        raise UnsafeArchiveError(
            f"a volume in the backup is not a directory: {name}")
    # #2678 re-review: a file at a CRITICAL volume's name would be skipped by
    # the copy, and the restore would replay the box's own stale dumps.
    if len(parts) == 2 and parts[1] in CRITICAL_VOLUMES and kind != "dir":
        raise UnsafeArchiveError(
            f"a critical volume in the backup is not a directory: {name}")
    # #3450: a staged volume copy below databases/ is a volume's tree, which
    # the restore copies with symlinks kept as symlinks (never opened by name).
    if (len(parts) > 2 and parts[1] == "databases" and kind == "sym"
            and not (len(parts) > 3 and parts[2] in _STAGED_IN_DATABASES)):
        raise UnsafeArchiveError(
            f"symlink in the backup's databases volume: {name}")


def _refuse_a_lexical_link_on_the_way(kinds, parts, name):
    for i in range(1, len(parts)):
        k = kinds.get(parts[:i])
        if k == "sym":
            raise UnsafeArchiveError(
                f"member is written outside the destination through a link: {name}")
        if k == "file":
            raise UnsafeArchiveError(f"member below a file: {name}")


def _refuse_a_link_on_disk(root, parts, name):
    """#2678: every existing ancestor of `root/<parts>` must be a real
    directory (lstat), never a symlink — whoever put the link there."""
    p = root
    for part in parts[:-1]:
        p = os.path.join(p, part)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return  # the rest is created by tarfile as real directories
        if stat.S_ISLNK(st.st_mode):
            raise UnsafeArchiveError(
                f"member is written outside the destination through a link: {name}")


def extract_confined(tar, dest, members, numeric_owner=False):
    """#2542/#246, #2678: extract `members` into `dest`, ONE AT A TIME. Both
    restore paths use it (safe_extract_tar for the full restore,
    restore_partial).

    validate_archive_members runs first over the whole list (the rules are
    there). Then, per member, against what is ON DISK (a link the member list
    does not show — one already in `dest` — is caught here):
      * no existing ancestor of the member's path is a symlink;
      * a non-symlink member is never written over an existing symlink;
      * a hardlink's target is reached through no symlink; a target that is
        a symlink makes the member a symlink with the same target. Python
        3.14's tarfile links with os.link(follow_symlinks=True) — the member
        would become a second name of the file OUTSIDE. The manager image is
        python:3.11-alpine today, whose tarfile does not follow; the guard is
        for the image bump. The target text is copied verbatim, also when it
        is relative: a hardlinked symlink IS the same link, its text resolved
        from wherever the name sits — rewriting it would change that.
    A symlink member is created as it is and never followed.

    #2633 resolved paths with realpath and refused a relative symlink whose
    target resolved outside `dest`. realpath follows the links already
    extracted, so `python3 -> python` next to `python -> /usr/bin/python3.12`
    "escaped", and every Python venv in a volume failed the restore (#2678).

    `filter="fully_trusted"` where the keyword exists (3.12+, 3.11.4+): the
    checks are ours, and the `data` filter would refuse the absolute links and
    rewrite volume/PGDATA modes. Directory attributes are applied last, deepest
    first, as extractall does, so a read-only directory does not block its own
    contents — each one re-checked and set through an O_NOFOLLOW fd
    (`_set_dir_attrs_confined`)."""
    members = list(members)
    validate_archive_members(members)
    root = os.path.realpath(dest)
    kw = {"numeric_owner": numeric_owner}
    if hasattr(tarfile, "fully_trusted_filter"):
        kw["filter"] = "fully_trusted"
    dirs = []
    for m in members:
        parts = _member_parts(m.name)
        if parts:
            _refuse_a_link_on_disk(root, parts, m.name)
            target = os.path.join(root, *parts)
            if not m.issym() and os.path.islink(target):
                raise UnsafeArchiveError(f"member is written over a symlink: {m.name}")
        if m.islnk():
            lparts = _member_parts(m.linkname, "hardlink target")
            _refuse_a_link_on_disk(root, lparts, m.name)
            src = os.path.join(root, *lparts)
            try:
                st = os.lstat(src)
            except OSError:
                st = None
            if st is not None and stat.S_ISLNK(st.st_mode):
                if os.path.lexists(target):
                    os.unlink(target)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.symlink(os.readlink(src), target)
                continue
        if m.isdir():
            dirs.append(m)
            tar.extract(m, root, set_attrs=False, **kw)
        else:
            tar.extract(m, root, **kw)
    for d in sorted(dirs, key=lambda x: x.name, reverse=True):
        _set_dir_attrs_confined(tar, d, root, numeric_owner)


def _is_real_dir(path):
    """lstat: a directory, not a link to one."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _open_dir_confined(root, real):
    """An fd for the directory `real` (a resolved path inside `root`), opened
    component by component from `root` with O_NOFOLLOW|O_DIRECTORY: a symlink
    swapped into the path after the check cannot redirect it."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    rel = os.path.relpath(real, root)
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    # Defence in depth (review m1): the caller has checked `real`, but this
    # walk refuses on its own to climb out of `root` or to restart elsewhere.
    if os.path.isabs(rel) or ".." in parts:
        raise UnsafeArchiveError(f"directory path leaves the destination: {real}")
    fd = os.open(root, flags)
    try:
        for part in parts:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError:
        os.close(fd)
        raise
    return fd


def _set_dir_attrs_confined(tar, d, root, numeric_owner):
    """#246 review R1: the deferred pass of extract_confined. Between a
    directory's creation and this pass a LATER member may re-point a link in
    its path (`a -> sub`, `a/x`, `a -> /elsewhere`), so `root/a/x` is checked
    again here — resolved inside `root`, a real directory, not a symlink — and
    the attributes go through an fd, never through the path. Owner, mtime and
    mode as tarfile's chown/utime/chmod set them; a failed check refuses the
    restore."""
    target = os.path.join(root, d.name)
    real = os.path.realpath(target)
    try:
        st = os.lstat(target)
    except OSError as e:
        raise UnsafeArchiveError(f"directory vanished before its attributes: {d.name}") from e
    if not _inside(root, real) or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise UnsafeArchiveError(
            f"directory moved outside the destination before its attributes: {d.name}")
    try:
        fd = _open_dir_confined(root, real)
    except OSError as e:
        raise UnsafeArchiveError(f"directory path changed under the restore: {d.name}") from e
    try:
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                u, g = d.uid, d.gid
                if not numeric_owner:
                    try:
                        import grp
                        if d.gname:
                            g = grp.getgrnam(d.gname)[2]
                    except (ImportError, KeyError):
                        pass
                    try:
                        import pwd
                        if d.uname:
                            u = pwd.getpwnam(d.uname)[2]
                    except (ImportError, KeyError):
                        pass
                try:
                    os.fchown(fd, u, g)
                except OSError as e:
                    raise tarfile.ExtractError("could not change owner") from e
            if d.mtime is not None:
                try:
                    os.utime(fd, (d.mtime, d.mtime))
                except OSError as e:
                    raise tarfile.ExtractError("could not change modification time") from e
            if d.mode is not None:
                try:
                    os.fchmod(fd, d.mode)
                except OSError as e:
                    raise tarfile.ExtractError("could not change mode") from e
        except tarfile.ExtractError as e:
            # extractall's own rule: non-fatal unless errorlevel > 1
            if tar.errorlevel > 1:
                raise
            logger.debug("tarfile: %s", e)
    finally:
        os.close(fd)


class InsufficientSpaceError(Exception):
    """F-051: the staging directory cannot hold the extracted archive."""


def require_extract_space(members, path):
    """F-051: the archive's uncompressed size plus 10% must fit where `path`
    lives (the nearest existing directory, so the #2678 pre-stop phase can ask
    before the staging directory exists)."""
    total_uncompressed = sum(m.size for m in members if m.isfile())
    probe = os.path.abspath(path)
    while not os.path.isdir(probe) and probe != os.path.dirname(probe):
        probe = os.path.dirname(probe)
    st = os.statvfs(probe)
    available_bytes = st.f_bavail * st.f_frsize
    required = int(total_uncompressed * 1.1)
    if required > available_bytes:
        raise InsufficientSpaceError(
            f"Insufficient disk space for extraction: need {required} bytes, "
            f"have {available_bytes} bytes available in {probe}")


def safe_extract_tar(tar, path):
    """Safely extract tar archive with comprehensive validation (CVE-2007-4559).

    F-051: Validates path traversal, rejects device files and out-of-scope symlinks,
    and checks available disk space before extraction.
    """
    abs_path = os.path.abspath(path)

    # F-051: Check archive size vs available disk space before extracting
    require_extract_space(tar.getmembers(), path)

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

        # F-051: Reject HARD links that point outside the extraction
        # directory (a hardlink target is an in-archive path, relative to the
        # root). #2678: a SYMLINK's target is not checked at all — a symlink
        # is extracted as a leaf and never followed (`.venv/bin/python ->
        # /usr/bin/python3.12`, `lib64 -> lib`); extract_confined refuses
        # every write THROUGH one.
        if member.islnk():
            link_target = os.path.abspath(os.path.join(abs_path, member.linkname))
            if not link_target.startswith(abs_path + os.sep) and link_target != abs_path:
                raise Exception(
                    f"hardlink escapes the destination: {member.name} -> {member.linkname}"
                )

    # #2542: as root, tarfile restores each member's owner — by NAME unless
    # numeric_owner, i.e. through THIS image's /etc/passwd, which knows none
    # of the service users (cognee's 1000, a de-rooted service's uid). The
    # archive's numbers are the truth. The lexical checks above cannot see a
    # member written through an earlier symlink; extract_confined checks each
    # member against what is already on disk.
    extract_confined(tar, path, tar.getmembers(), numeric_owner=True)


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
        logger.warning(f"{label}: not running as root — the archive's "
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
            # M-10: os.chmod follows a link; never chmod through one, even
            # where the archive's member of that name was not a link.
            if not is_link and not os.path.islink(path):
                os.chmod(path, mode)
        except OSError as e:
            failed.append(f"{rel} ({e.strerror})")
    if failed:
        raise RuntimeError(
            f"{label}: could not restore the archive's owner/mode of "
            f"{len(failed)} entr{'y' if len(failed) == 1 else 'ies'}: "
            + ", ".join(failed[:5]) + (" …" if len(failed) > 5 else ""))


# #2464 fix round 1: a full restore whose databases and .env came back but
# whose certs/ or box-local state did not. restore_full returns this (not
# True, not False); main() maps it to exit code 3 — NOT 2, which argparse and
# the interpreter use for usage errors (fix round 2). Compare with ==, never
# test its truth value.
#: `restore --keep-env`: the backup's .env / .env.dify land here (in the archive
#: directory, the stack's backups/ on the host), mode 600, instead of over the
#: box's own. A staging refresh keeps its own .env and still needs the backup's.
RESTORED_ENV_SUBDIR = ".restored-env"

RESTORE_PARTIAL = "partial"
EXIT_RESTORE_PARTIAL = 3
# #2678: a restore that failed BEFORE it stopped anything (the archive was
# missing, did not verify or decrypt, was too new, or was refused by the dry
# pass or the space check). The stack is running and nothing was written;
# cli/backup.sh says so instead of "stack NOT recreated".
EXIT_RESTORE_REFUSED = 4

#: `restore --keep-env`: beside the handed-over .env, a machine-readable account
#: of what a full restore did NOT bring back (#2708). A caller that needs to tell
#: "skipped when the backup was taken" from every other cause reads this file,
#: never the log prose. Written fresh by every full restore that reaches a
#: verdict; removed first, so a failed run leaves none behind.
RESTORE_STATUS_FILE = "restore-status.json"
RESTORE_STATUS_FORMAT = 1
#: one log line per agent volume the backup skipped (manifest `archived` not true)
SKIPPED_AT_BACKUP_MARKER = "RESTORE-SKIPPED-AT-BACKUP"


def skip_note(detail):
    """The why of a volume the backup skipped, from its manifest entry's
    `skip_reason` / `size_mb` when the backup recorded them: "reason: size cap,
    5120 MB", or "reason unknown" (older manifests carry neither). Printable
    characters only, bounded: the text comes from the backup."""
    detail = detail if isinstance(detail, dict) else {}
    reason, size = detail.get("skip_reason"), detail.get("size_mb")
    reason = ("".join(c for c in reason if c.isprintable())[:200].strip()
              if isinstance(reason, str) else "")
    parts = [f"reason: {reason}" if reason else "reason unknown"]
    if isinstance(size, (int, float)) and not isinstance(size, bool) and size >= 0:
        parts.append(f"{size:g} MB")
    return ", ".join(parts)


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
                           "restored", name, BOX_LOCAL_DB_MEMBER, BOX_LOCAL_OVERLAY)
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


#: #3412: the box-local compose overlays (compose.local.<name>.yml in the stack
#: root): collected HERE for the backup (`compose-local-collect`, this container
#: mounts the stack root read-only), encrypted by pre-backup.sh with the .env's
#: passphrase and cipher, handed back to the host for the restore
#: (`compose-local-export`). Which files qualify is razzfazz_common.compose_local's
#: one rule, for both directions.
COMPOSE_LOCAL_ARCHIVE = "compose-local-backup.tar.gz.enc"
#: exit code of `compose-local-collect` when the box has no overlay to carry
EXIT_COMPOSE_LOCAL_NONE = 3


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


_ENV_LINE_RE = re.compile(rb'^[A-Za-z_][A-Za-z0-9_]*=', re.M)


def _looks_like_env(data):
    """#2532: a decrypted .env is UTF-8 text with at least one KEY= line. A
    wrong passphrase makes `openssl enc -d` fail, or (about once in 256 tries)
    'succeed' with garbage that fails this check."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(_ENV_LINE_RE.search(data))


def _write_env_in_place(target, data):
    """#2532/#1224: put `data` into `target` without replacing the file. A
    single-file bind mount keeps its inode. Created 0600 when absent;
    O_NOFOLLOW: never through a symlink."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.ftruncate(fd, 0)
        view = memoryview(data)
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
# like `\connect x`, or the whole three-line `--` / `-- Database "x" dump` /
# `--` block (a lone header line there is ignored: see _cluster_dump_events). That never cuts silently in
# the wrong place: plan_cluster_dump then sees a second \connect in one
# section, a header without its own \connect, or a database twice, and
# REFUSES the dump before anything is dropped; a boundary it cannot spot
# ends in psql's ON_ERROR_STOP failing the section loudly.
_CONNECT_LINE_RE = re.compile(r'^\\connect(?:\s|$)')
# The two shapes pg_dump writes (appendPsqlMetaConnect, PG 11-17):
#   \connect openwebui_db        a name of [A-Za-z0-9_.] only, quoted by fmtId
#   \connect "Mixed.Case"        when it is not a lowercase identifier
#   \connect -reuse-previous=on "dbname='abd872e5-41df-…'"
#                                any other name (a UUID's dashes): a libpq
#                                connection string inside an identifier
_CONNECT_ARGS_RE = re.compile(
    r'^\\connect\s+(?:-reuse-previous=on\s+)?'
    r'(?:"((?:[^"]|"")*)"|([^\s"]+))\s*$')
_CONNSTR_DBNAME_RE = re.compile(r"\Adbname=(?:'((?:[^'\\]|\\.)*)'|([^\s'\\]+))\Z", re.S)
_DUMPALL_DB_HEADER_RE = re.compile(r'^-- Database "(.*)" dump\s*$')


def parse_connect_target(line):
    """The database a pg_dump `\\connect` line switches to, or None when the
    line is not one of the shapes pg_dump writes (a caller refuses, never
    guesses, on None). The connection-string form is decoded the way libpq
    does: `\\x` is x."""
    m = _CONNECT_ARGS_RE.match(line.rstrip('\r\n'))
    if not m:
        return None
    reuse = line.split(None, 2)[1] == '-reuse-previous=on'
    quoted, bare = m.group(1), m.group(2)
    arg = quoted.replace('""', '"') if quoted is not None else bare
    if not reuse:
        # Without -reuse-previous psql still reads an argument holding `=` or a
        # URI prefix as a connection string (another host, another database);
        # pg_dump never writes that shape, so it is unreadable, never a name.
        if not arg or '=' in arg or arg.startswith(('postgresql://', 'postgres://')):
            return None
        return arg
    c = _CONNSTR_DBNAME_RE.match(arg)
    if not c:
        return None
    if c.group(1) is not None:
        return re.sub(r'\\(.)', r'\1', c.group(1), flags=re.S) or None
    return c.group(2)
_COPY_FROM_STDIN_RE = re.compile(r'^COPY\s.*\sFROM stdin;\s*$')
_DUMPALL_TRAILER = '-- PostgreSQL database cluster dump complete'


def _cluster_dump_events(lines):
    """#2465: yield ``(index, offset, kind, name)`` for every section boundary
    in a pg_dumpall dump, outside COPY data blocks.

    ``lines`` is any iterable of lines (str, or bytes from a binary file),
    each with its line ending; ``offset`` is the boundary line's start in
    the units of ``len(line)`` (bytes for a binary file, so a caller can
    seek to it). ``kind`` is ``'connect'`` or ``'header'``; ``name`` is the
    database name, or None for a ``\\connect`` line whose target cannot be
    read (a caller must refuse, never guess, on None).

    A `-- Database "…" dump` line is a header only inside the comment block
    pg_dumpall writes around it (pg_dumpall.c): a line that is exactly `--`
    before it AND after it. The same line alone — inside a function body, a
    view or a comment string — is not a boundary. pg_dumpall refuses a
    database name with a line break, so a real header is always one line.
    """
    in_copy = False
    offset = 0
    prev_dashes = False
    pending = None          # a header line that followed a `--` line
    for index, raw in enumerate(lines):
        # Cheap pre-filter: every line that matters starts with `\\`
        # (\connect, the COPY terminator), `-` (a header, `--`) or `C` (COPY);
        # the bulk of a dump — COPY rows — is skipped without decoding.
        first = raw[:1]
        if isinstance(raw, bytes):
            first = first.decode('latin-1')
        if first not in ('\\', '-', 'C'):
            pending = None
            prev_dashes = False
            offset += len(raw)
            continue
        line = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
        line = line.rstrip('\r\n')
        is_dashes = not in_copy and line == '--'
        if pending is not None:
            if is_dashes:
                yield pending
            pending = None
        if in_copy:
            if line == '\\.':
                in_copy = False
        elif _COPY_FROM_STDIN_RE.match(line):
            in_copy = True
        elif _CONNECT_LINE_RE.match(line):
            yield index, offset, 'connect', parse_connect_target(line)
        elif prev_dashes:
            m = _DUMPALL_DB_HEADER_RE.match(line)
            if m:
                pending = (index, offset, 'header', m.group(1))
        prev_dashes = is_dashes
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
    """`CREATE DATABASE <db_name>` at a line start (str): pg_dump writes the
    name quoted (`"` doubled) unless it is a plain lowercase identifier."""
    forms = [re.escape(quote_ident(db_name))]
    if _PLAIN_DB_NAME_RE.match(db_name):
        forms.append(re.escape(db_name))
    return re.compile(r'^CREATE DATABASE (?:' + '|'.join(forms) + r')[\s;]', re.M)


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

    Returns ``(header_end, databases, skipped)``: the cluster-wide part
    (roles, memberships, tablespaces) is ``[0, header_end)``; ``databases``
    is a list of ``(name, start, connect_at, end)`` in dump order — the
    section ``[start, end)`` begins at the database's `-- Database "…" dump`
    header (so it carries its CREATE DATABASE) and ``connect_at`` is where
    its `\\connect` line starts. ``skipped`` lists ``(name, reason)`` for
    each database whose name the restore refuses (see db_name_problem; the
    name is None when it cannot even be read): exactly that database is
    left out, every other one is still planned. A caller must report a
    restore with skipped databases as failed.

    Raises ValueError for anything it cannot cut unambiguously: an empty
    file, a dump without pg_dumpall's closing `-- PostgreSQL database
    cluster dump complete` line (truncated — a disk-full or killed
    pg_dumpall), a dump without any database header, a `\\connect`
    outside a pg_dumpall database header, a header without exactly one
    matching `\\connect`, or a database that appears twice. A restore
    refuses such a dump before it touches anything.
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
    skipped = []
    seen = set()
    for i, (_index, start, _kind, name) in enumerate(headers):
        end = headers[i + 1][1] if i + 1 < len(headers) else size
        if name is not None and name in seen:
            raise ValueError(f"database {name!r} appears twice in the dump")
        seen.add(name)
        connects = [e for e in events if e[2] == 'connect' and start < e[1] < end]
        # A section must hold exactly ONE \connect, to its own database —
        # for a refused name too (a name that cannot be read on either line
        # is None on both): anything else means the cut itself is in doubt,
        # and then no section of this dump is trusted.
        if len(connects) != 1 or connects[0][3] != name:
            raise ValueError(
                f"the section of database {name!r} does not hold exactly one "
                f"\\connect to it (found {[c[3] for c in connects]})")
        problem = ("its name cannot be read from the dump" if name is None
                   else db_name_problem(name))
        if problem:
            skipped.append((name, problem))
            continue
        databases.append((name, start, connects[0][1], end))
    return header_end, databases, skipped


def _drop_database_sql(name):
    """Terminate ``name``'s other sessions, then drop it (bytes for psql's
    stdin). The name only ever appears quoted."""
    return (f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = {quote_literal(name)} AND pid <> pg_backend_pid();\n"
            f"DROP DATABASE IF EXISTS {quote_ident(name)} WITH (FORCE);\n").encode('utf-8')


def _stream_psql(argv, dump_path=None, ranges=(), prefix=b'', suffix=b''):
    """#2465: run psql with ``prefix``, then the byte ``ranges`` of
    ``dump_path`` on stdin, streamed in 1 MiB chunks, then ``suffix``. Returns
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
            if suffix:
                proc.stdin.write(suffix)
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
      A database whose name the restore refuses (db_name_problem) is
      skipped — only that one; it is named in the failures and makes the
      result False.
      Any failure makes the result False; the remaining databases are
      still restored, and every failure is named in the returned list.

    ``psql_argv(db, on_error_stop)`` returns the argv of a psql that reads a
    script from stdin with ``-f -`` connected to ``db`` (docker exec on a box; a local psql
    in the api-tier test).

    Returns ``(ok, failures)``.
    """
    failures = []
    try:
        header_end, databases, skipped = plan_cluster_dump(dump_path)
    except (OSError, ValueError) as e:
        msg = f"{label}: cannot read the dump {dump_path} as a pg_dumpall file: {e}"
        logger.error(f"{msg} — nothing was restored.")
        return False, [msg]
    for name, problem in skipped:
        msg = (f"{label}: database {name!r} SKIPPED — {problem}; it was not "
               f"dropped and not restored")
        logger.error(msg)
        failures.append(msg)

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
        logger.error(f"{msg} — aborting before any database was dropped.")
        return False, [msg]
    logger.info(f"{label}: roles/tablespaces replayed "
                f"({exists} already existed — kept, attributes re-applied).")

    # 2. databases
    for name, start, connect_at, end in databases:
        if name in _IN_PLACE_DATABASES:
            rc, err = _stream_psql(psql_argv(name, True), dump_path,
                                   [(start, end)], prefix=verbose)
            if rc != 0:
                msg = f"{label}: database {name!r} (replayed in place) FAILED: {_tail(err)}"
                logger.error(f"{msg}")
                failures.append(msg)
            else:
                logger.info(f"{label}: database {name!r} replayed in place.")
            continue

        with open(dump_path, 'rb') as f:
            f.seek(start)
            create_part = f.read(connect_at - start)
        if not _create_database_re(name).search(
                create_part.decode('utf-8', errors='replace')):
            msg = (f"{label}: database {name!r} has no CREATE DATABASE in its "
                   f"dump section — not dropped, not restored")
            logger.error(f"{msg}")
            failures.append(msg)
            continue

        drop_sql = verbose + _drop_database_sql(name)
        rc, err = _stream_psql(psql_argv(_MAINTENANCE_DB, True), prefix=drop_sql)
        if rc != 0:
            msg = (f"{label}: could not drop database {name!r} before restoring "
                   f"it — its restore was ABORTED, the live database is "
                   f"unchanged: {_tail(err)}")
            logger.error(f"{msg}")
            failures.append(msg)
            continue

        rc, err = _stream_psql(psql_argv(_MAINTENANCE_DB, True), dump_path,
                               [(start, end)], prefix=verbose)
        if rc != 0:
            msg = (f"{label}: database {name!r} was dropped and its replay "
                   f"FAILED — it is INCOMPLETE: {_tail(err)}")
            logger.error(f"{msg}")
            failures.append(msg)
        else:
            logger.info(f"{label}: database {name!r} dropped, recreated and restored.")

    return not failures, failures


# #2524: Komodo's FerretDB stores its data in DocumentDB (an extension in
# postgres-komodo's `postgres` DB). The extension's catalogue — which
# collection is which documentdb_data.documents_<id> table, the index
# metadata, and both id sequences — belongs to the extension and is never
# marked for pg_dump, so pg_dumpall carries the documents tables without it.
# pre-backup.sh writes the catalogue next to the dump (KOMODO_CATALOG_FILE).
KOMODO_CATALOG_FILE = "postgres_komodo_catalog.sql"
KOMODO_CATALOG_TRAILER = "-- end of the razzfazz.ai DocumentDB catalogue"
KOMODO_DB = "postgres"

# Empties DocumentDB before the dump is replayed: every collection table in
# documentdb_data that is not part of the extension, and every catalogue row.
# On a fresh volume it is a no-op; on a box that ran Komodo before the restore
# it removes the collections the replay would otherwise collide with
# ("relation … already exists", COPY into a table that already has rows).
# Without DocumentDB (another Komodo backend) it does nothing.
DOCUMENTDB_CLEAR_SQL = b"""\\set ON_ERROR_STOP 1
DO $rzfz$
DECLARE t record;
BEGIN
  IF to_regnamespace('documentdb_data') IS NOT NULL THEN
    FOR t IN SELECT c.oid::regclass AS q FROM pg_class c
             WHERE c.relnamespace = 'documentdb_data'::regnamespace AND c.relkind IN ('r', 'p')
               AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                               AND d.objid = c.oid AND d.deptype = 'e')
    LOOP EXECUTE format('DROP TABLE IF EXISTS %s CASCADE', t.q); END LOOP;
  END IF;
  IF to_regclass('documentdb_api_catalog.collection_indexes') IS NOT NULL THEN
    DELETE FROM documentdb_api_catalog.collection_indexes;
  END IF;
  IF to_regclass('documentdb_api_catalog.collections') IS NOT NULL THEN
    DELETE FROM documentdb_api_catalog.collections;
  END IF;
END $rzfz$;
"""


# After the catalogue replay: a collection table no catalogue row names
# (a collection dropped between the dump and the catalogue copy) is unusable
# and would collide with the id it carries — drop it; and the id sequence
# must be past every id in use. Runs right after the catalogue, on the same
# psql session with ON_ERROR_STOP.
DOCUMENTDB_RECONCILE_SQL = b"""
DO $rzfz$
DECLARE t record; top bigint;
BEGIN
  FOR t IN SELECT c.oid::regclass AS q FROM pg_class c
           WHERE c.relnamespace = 'documentdb_data'::regnamespace AND c.relkind IN ('r', 'p')
             AND c.relname ~ '^documents_[0-9]+$'
             AND NOT EXISTS (SELECT 1 FROM documentdb_api_catalog.collections k
                             WHERE k.collection_id = substring(c.relname from 11)::bigint)
             AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                             AND d.objid = c.oid AND d.deptype = 'e')
  LOOP EXECUTE format('DROP TABLE %s CASCADE', t.q); END LOOP;
  SELECT max(collection_id) INTO top FROM documentdb_api_catalog.collections;
  IF top IS NOT NULL AND top >= (SELECT CASE WHEN is_called THEN last_value ELSE last_value - 1 END
                                   FROM documentdb_api_catalog.collections_collection_id_seq) THEN
    PERFORM setval('documentdb_api_catalog.collections_collection_id_seq', top, true);
  END IF;
  SELECT max(index_id) INTO top FROM documentdb_api_catalog.collection_indexes;
  IF top IS NOT NULL AND top >= (SELECT CASE WHEN is_called THEN last_value ELSE last_value - 1 END
                                   FROM documentdb_api_catalog.collection_indexes_index_id_seq) THEN
    PERFORM setval('documentdb_api_catalog.collection_indexes_index_id_seq', top, true);
  END IF;
END $rzfz$;
"""

def documentdb_catalog_problem(path):
    """#2524: why the catalogue file at ``path`` cannot be replayed, or None.
    A file without its trailer was cut off; replaying half a catalogue would
    link some collections and lose the rest without a word."""
    if not os.path.exists(path):
        return ("the backup carries no DocumentDB catalogue "
                f"({KOMODO_CATALOG_FILE}; backups made before this release "
                "do not)")
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode('utf-8', errors='replace').rstrip('\n')
    except OSError as e:
        return f"the DocumentDB catalogue {KOMODO_CATALOG_FILE} is unreadable: {e}"
    if tail.rsplit('\n', 1)[-1] != KOMODO_CATALOG_TRAILER:
        return (f"the DocumentDB catalogue {KOMODO_CATALOG_FILE} is truncated "
                f"(no '{KOMODO_CATALOG_TRAILER}' line at its end)")
    return None


class BackupManager:
    #: #2132: the generation that opened the archive; the inner members try it first
    #: (class attribute: suites build the manager with __new__)
    _archive_passphrase = None
    #: #2524: why Komodo's data did not come back (None: restored, or no dump)
    komodo_restore_problem = None

    def __init__(self):
        #: #2678: set by restore_full/restore_partial when they stop the stack
        self.stack_was_stopped = False
        #: #2632: the containers stop_stack stopped, in order — the only ones
        #: start_stack starts again
        self._stopped_containers = []

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
        """Delete a backup archive AND its sidecar files (#2713).

        True only when the named archive existed and every file belonging to
        it is gone. An unknown name, a name that is not a backup archive, or a
        file that could not be removed is False, and the log line names every
        file that was left.
        """
        # Sanitize filename to prevent directory traversal
        filename = os.path.basename(filename or '')
        if not filename or filename in ('.', '..') or not is_backup_file(filename):
            logger.error(f"Not a backup archive name: {filename!r}")
            return False
        path = os.path.join(BACKUP_DIR, filename)

        if not os.path.isfile(path):
            logger.error(f"Backup file not found: {path}")
            return False

        left = []
        for victim in (path,) + tuple(path + suffix for suffix in BACKUP_SIDECAR_SUFFIXES):
            try:
                os.remove(victim)
                logger.info(f"Deleted backup file: {os.path.basename(victim)}")
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.error(f"Error deleting {os.path.basename(victim)}: {e}")
                left.append(os.path.basename(victim))
        if left:
            logger.error(f"Backup {filename} NOT fully deleted; left behind: {', '.join(left)}")
            return False
        return True

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
                    f"could not list databases for per-DB dump: "
                    f"{list_result.stderr.strip()}"
                )
                return

            dbs = [d.strip() for d in list_result.stdout.splitlines() if d.strip()]
            written = 0
            failed = 0
            for db in dbs:
                # Every database is listed, whatever its name (a Cognee
                # dataset's UUID too); a name that can't be a file name here
                # has no per-DB dump, the cluster dump still carries it.
                filename = per_db_dump_filename(db)
                if filename is None:
                    logger.warning(f"no per-DB dump for {db!r}: its name is not "
                                   f"a safe file name (postgres_core.sql has it)")
                    continue
                dump_path = os.path.join(per_db_dir, filename)
                try:
                    with open(dump_path, 'wb') as f:
                        result = subprocess.run(
                            ['docker', 'exec', 'postgres', 'pg_dump',
                             '-U', pg_user, '-Fc', '-d', pg_dbname_arg(db)],
                            stdout=f, stderr=subprocess.PIPE,
                        )
                    if result.returncode != 0:
                        stderr = result.stderr
                        stderr = stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else (stderr or '')
                        logger.error(f"per-DB dump failed for {db!r}: {stderr.strip()}")
                        failed += 1
                        try:
                            os.remove(dump_path)
                        except OSError:
                            pass
                    else:
                        written += 1
                except Exception as e:
                    logger.error(f"per-DB dump raised for {db!r}: {e}")
                    failed += 1
                    try:
                        os.remove(dump_path)
                    except OSError:
                        pass

            logger.info(
                f"per-DB dump pass complete — {written} written, "
                f"{failed} failed, {len(dbs)} database(s) listed."
            )
        except Exception as e:
            logger.error(f"per-DB dump step failed: {e}")

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
            # #2549: the exit code, not a stream that always "succeeds". The
            # wrapper prints the pre-backup hook's report and the verdict
            # (OK / DEGRADED / FAILED) as its last lines.
            result = container.exec_run("razzfazz-backup", stream=False)
            for line in (result.output or b"").decode("utf-8", "replace").splitlines():
                logger.info(f"Backup Service: {line.strip()}")
            if result.exit_code == 3:
                logger.error("the backup is PARTIAL — agent volume(s) with data are "
                             "NOT in it (the WARN lines above name them and why).")
                return False
            if result.exit_code != 0:
                logger.error(f"the backup FAILED (razzfazz-backup exit "
                             f"{result.exit_code}); see the lines above.")
                return False
            logger.info("Full backup completed.")
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

        #2639: the target passes the restore side's check (I-08) — one
        volume name, a real directory under RESTORE_ROOT — and the archive
        has the full backup's layout (`backup/<volume>/…`), the one
        restore_partial reads: a partial backup round-trips through a
        partial restore.
        """
        try:
            source_path = mounted_restore_target(target_name)
        except UnknownRestoreTargetError as e:
            logger.error(f"Partial backup refused: {e}")
            return False
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filename = f"partial-{target_name}-{timestamp}.tar.gz"
        dest_path = os.path.join(BACKUP_DIR, filename)

        passphrase = _read_passphrase_from_env_file(env_path) \
            or os.environ.get('BACKUP_ENCRYPTION_PASSWORD', '')
        logger.info(f"Starting partial backup of {target_name} to {filename}"
                    f"{'.gpg (encrypted)' if passphrase else ' (NOT encrypted: BACKUP_ENCRYPTION_PASSWORD is empty)'}...")
        old_umask = os.umask(0o077)
        try:
            with tarfile.open(dest_path, "w:gz") as tar:
                tar.add(source_path, arcname=f"backup/{target_name}")
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
                                 f"No plaintext copy was kept.")
                    return False
                os.chmod(enc_path, 0o600)
                dest_path = enc_path
                filename = os.path.basename(enc_path)
            else:
                logger.warning(f"Partial backup {filename} is NOT encrypted: BACKUP_ENCRYPTION_PASSWORD "
                               f"is empty on this box (F-A2). It is readable by its owner only.")
            logger.info(f"Partial backup created: {filename}")
            self.last_archive = filename
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

    def restore_full(self, backup_file, keep_env=False):
        """
        Perform a full restore.
        1. Stop all containers (except this one and backup-service maybe).
        2. Extract archive.
        3. Restore DBs.
        4. Restart containers.

        ``keep_env``: the box's .env / .env.dify stay as they are; the
        backup's are written to BACKUP_DIR/RESTORED_ENV_SUBDIR (mode 600) for
        the caller (restore_env_files).

        Returns True (complete), RESTORE_PARTIAL (#2464: databases and .env
        restored, certs/, the box-local state, — #2531 — a per-user agent
        volume or — #2712 — a non-critical core volume not) or False (failed;
        a critical volume that did not come back is a failure). `stack_was_stopped` says whether a
        failure came before the stop (#2678: exit 4) or after it.
        """
        backup_path = os.path.join(BACKUP_DIR, backup_file)
        self.stack_was_stopped = False
        self.agent_restore_report = None
        if keep_env:
            # #2708: an earlier run's account must never describe this one.
            self._drop_restore_status()
        if not os.path.exists(backup_path):
            logger.error(f"Backup file {backup_file} not found.")
            return False
        if os.path.basename(backup_file).startswith("partial-"):
            # #2639: a partial archive holds one volume and no databases; a
            # full restore of it would replay the box's own database dumps.
            logger.error(f"Restore refused: {backup_file} is a partial backup (one "
                         "volume). Restore it with a partial restore of that volume; "
                         "nothing was stopped and no volume was touched.")
            return False

        # F-002: Verify integrity before restoring
        if not self._verify_checksum(backup_path):
            logger.error("Aborting restore due to checksum mismatch.")
            return False

        # #2678: the stack is stopped only once the archive has been decrypted,
        # its version accepted and every member judged by the extraction rules
        # (validate_archive_members). A refusal before that point leaves every
        # service running and every volume untouched — and is not followed by
        # a start sweep, because nothing was stopped.
        stopped = False

        # rc6.7 #7: encrypted backups (.tar.gz.gpg) need a decrypt step
        # before tarfile.open. We write the decrypted tarball to a /tmp
        # path; the finally clause below removes it after extraction.
        decrypted_tmp = None
        # #1633-Klasse, gefunden im Review zu #1621: der
        # `finally`-Block unten endete auf `return True`. Ein `return` im
        # `finally` ueberschreibt JEDES `return` aus dem `try` und verschluckt
        # ausserdem jede Ausnahme — die Ablehnung eines zu neuen Archivs meldete
        # deshalb Erfolg, und (seit jeher, aelter als #1621) meldete auch jeder
        # abgestuerzte Restore Erfolg. Das Ergebnis wird jetzt festgehalten,
        # `finally` raeumt nur noch auf, und die Antwort steht danach.
        outcome = False
        vol_failures = []
        retired = []
        by_design = []
        try:

            if is_encrypted_backup(backup_file):
                decrypted_tmp, self._archive_passphrase = decrypt_backup_with_keyring(backup_path)
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

            # #2678: the dry pass, before anything is stopped: the members by
            # the extraction rules, and the space the extraction needs (a
            # leftover staging directory of an earlier run is ours to clear).
            with tarfile.open(archive_path, "r:gz") as tar:
                try:
                    members = normalise_member_names(tar.getmembers())
                    validate_archive_members(members)
                    # #2639 review: a full backup always carries the db-dumps
                    # volume. One without it (a partial archive renamed, or
                    # some other tar) would replay the box's OWN dumps.
                    if not any(m.name.rstrip("/") == "backup/databases"
                               or m.name.startswith("backup/databases/")
                               for m in members):
                        raise UnsafeArchiveError(
                            "the archive carries no backup/databases/ — it is not a "
                            "full backup (a partial backup is restored with a "
                            "partial restore of its volume)")
                    if os.path.islink(RESTORE_EXTRACT_DIR):
                        raise UnsafeArchiveError(
                            f"the staging path is a link: {RESTORE_EXTRACT_DIR}")
                    if os.path.lexists(RESTORE_EXTRACT_DIR):
                        shutil.rmtree(RESTORE_EXTRACT_DIR)
                    require_extract_space(members, RESTORE_EXTRACT_DIR)
                except (UnsafeArchiveError, InsufficientSpaceError) as e:
                    logger.error(f"Restore refused: {e} — the archive was checked "
                                 "before anything was stopped; the stack keeps running "
                                 "and no volume was touched.")
                    return False

            logger.info("Initiating FULL RESTORE. Stopping stack...")
            stopped = self.stack_was_stopped = True
            self.stop_stack()

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
                # #2678 review I-1: copytree follows a top-level link. The dry
                # pass refuses one; this refuses one before any volume is
                # emptied, whoever put it there.
                if os.path.lexists(source_root) and not _is_real_dir(source_root):
                    raise UnsafeArchiveError(
                        f"the backup's root is not a directory: {source_root}")
                # Volumes whose restore failure must FAIL the whole operation
                # (a "success" with a half-restored DB is worse than a loud error).
                critical_volumes = CRITICAL_VOLUMES
                if os.path.exists(source_root):
                    vol_names = sorted(os.listdir(source_root))
                    for vol_name in vol_names:
                        if os.path.islink(os.path.join(source_root, vol_name)):
                            raise UnsafeArchiveError(
                                f"a volume in the backup is not a directory: {vol_name}")
                    for vol_name in vol_names:
                        src_vol = os.path.join(source_root, vol_name)
                        if not _is_real_dir(src_vol):
                            if vol_name in critical_volumes:
                                # #2678 re-review: never a silent skip.
                                vol_failures.append((vol_name, "not a directory in the backup"))
                                logger.critical(f"volume restore FAILED for {vol_name}: "
                                                "not a directory in the backup")
                            else:
                                # not a volume (a volume is a directory): no
                                # volume's data is left behind, so not PARTIAL
                                logger.warning(f"{vol_name} in the backup is not a volume "
                                               "directory; not restored.")
                            continue
                        dst_vol = os.path.join(RESTORE_ROOT, vol_name)

                        if not os.path.exists(dst_vol) and vol_name in RETIRED_VOLUMES:
                            retired.append(vol_name)
                            logger.warning(f"{vol_name}: not restored, volume retired "
                                           "(no component of this version uses it).")
                            continue
                        if not os.path.exists(dst_vol) and vol_name in NOT_ARCHIVED_BY_DESIGN:
                            by_design.append(vol_name)
                            logger.warning(f"{vol_name}: not restored — "
                                           f"{NOT_ARCHIVED_BY_DESIGN[vol_name]}.")
                            continue
                        if not os.path.exists(dst_vol):
                            # #2712: the backup holds this volume's data and the
                            # box does not get it back — that is not a success.
                            # Every volume the backup service archives has a
                            # restore target in core/compose.yml (pinned by
                            # test_2712_every_archived_volume_has_a_restore_target),
                            # so this is an older backup's volume the stack no
                            # longer has, or a mount that is missing: PARTIAL,
                            # by name (a critical volume fails the restore).
                            why = "no restore target is mounted for it"
                            vol_failures.append((vol_name, why))
                            level = logger.critical if vol_name in critical_volumes else logger.error
                            level(f"volume restore FAILED for {vol_name}: {why}")
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
                                f"volume restore FAILED for {vol_name}: {ce}"
                            )

                # #3450: a volume archived from the hook's copy comes back from
                # that copy (the live path is not in the archive).
                if os.path.exists(source_root):
                    for vol_name, why in self._restore_staged_volumes(
                            source_root, tar.getmembers()):
                        vol_failures.append((vol_name, why))
                        logger.error(f"volume restore FAILED for {vol_name}: {why}")

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
                        "restore completed with non-critical volume "
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
            # #2532: a .env the backup carries that did NOT come back fails the
            # restore. cli/backup.sh must not recreate the stack against the
            # box's own .env while the databases hold the backup's roles.
            env_ok = (self.restore_env_files(keep_env=True) if keep_env
                      else self.restore_env_files())
            if env_ok is False:
                raise RuntimeError(
                    "the backup's .env / .env.dify were NOT restored (see the "
                    ".env restore errors above). The volumes and databases already "
                    "hold the backup's data, so the stack is NOT recreated against "
                    "the box's own .env"
                    + ("" if db_ok else "; the databases were NOT restored "
                       "completely either")
                    + " — FULL RESTORE FAILED")

            # #1621: and the box's TLS material, which lives outside every
            # volume. Without it the box comes back on self-signed certs and
            # every vHost warns — a restore that "succeeded".
            # #2464 fix round 1: False = the backup carries it and it was NOT
            # restored (None = the backup has none). Either makes the restore
            # PARTIAL (exit 3), not failed: the databases and .env are back.
            certs_ok = self.restore_cert_files() is not False
            # #1621: and the rest of the box-local state, in the same breath.
            box_local_ok = self.restore_box_local_files() is not False
            # #3412: the box-local compose overlays. Written on the HOST by
            # cli/backup.sh (this mount is read-only); named here so the log
            # says what the backup carries.
            # #3436 L-20: an archive that cannot be read makes the restore
            # PARTIAL (named below) and never ends it: the agent volumes and
            # the stack start still follow.
            try:
                overlays, _skipped, why = self.compose_local_overlays()
            except Exception as e:  # noqa: BLE001 — must not end the restore
                overlays, why = None, f"unexpected error: {e!r}"
            overlays_ok = overlays is not None
            if overlays is None:
                logger.error("the backup's box-local compose overlays cannot be read "
                             "(%s); a COMPOSE_FILE entry naming one will be missing.", why)
            elif overlays:
                logger.info("the backup carries box-local compose overlays: %s "
                            "(put back on the host before compose runs)",
                            ", ".join(sorted(overlays)))

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
            if vol_failures:
                # #2712: a non-critical core volume that did not come back
                # makes the restore PARTIAL (exit 3), named — like certs/ and
                # the agent volumes. (A critical one raised above.)
                not_restored.append(
                    "core volume(s) NOT restored: "
                    + "; ".join(f"{v} ({why})" for v, why in vol_failures)
                    + " (see the volume restore errors above)")
            if not certs_ok:
                not_restored.append(
                    "certs/ (the box's TLS material) was NOT restored "
                    "(see the certs/ restore error above)")
            if not box_local_ok:
                not_restored.append(
                    "the box-local state (.checksums.db, overlay/) was "
                    "NOT restored (see the box-local restore error above)")
            if not agents_ok:
                not_restored.append(
                    "per-user agent volumes were NOT all restored "
                    "(see the agent volume restore errors above)")
            if not overlays_ok:
                not_restored.append(
                    "the box-local compose overlays (compose.local.*.yml) were NOT "
                    "restored: the backup's archive of them cannot be read "
                    "(see the compose overlay error above)")
            if not db_ok:
                raise RuntimeError("; ".join(
                    ["the databases were NOT restored completely (see "
                     "the database restore errors above)"] + not_restored)
                    + " — FULL RESTORE FAILED")
            # #2708: what is missing, split by cause. Only an agent volume the
            # backup skipped when it was taken is told apart; everything else
            # (certs/, the box-local state, an agent volume lost for any other
            # reason, a volume whose copy failed) is `other`. An agent failure
            # the report does not account for is `other` too: fail closed.
            report = self.agent_restore_report if not agents_ok else None
            skipped = list((report or {}).get("skipped_at_backup") or [])
            detail = dict((report or {}).get("skipped_at_backup_detail") or {})
            other = [f"volume {v}" for v, _ in vol_failures]
            other += [] if certs_ok else ["certs/"]
            other += [] if box_local_ok else ["box-local state (.checksums.db, overlay/)"]
            other += [] if overlays_ok else ["box-local compose overlays (compose.local.*.yml)"]
            if not agents_ok:
                agent_other = list((report or {}).get("not_restored_other") or [])
                other += agent_other
                if not report or not (skipped or agent_other):
                    other.append("per-user agent volumes (no account of which)")
            if not_restored:
                # #2464 fix round 1: the databases and .env are back, so the
                # stack must come up (cli/backup.sh recreates it on exit 3) —
                # but this is not "complete", and it says what is missing.
                what = ("the databases and .env are restored" if env_ok else
                        "the databases are restored (the backup carries no .env; "
                        "the box keeps its own)")
                logger.error("PARTIAL RESTORE: " + what + ", but "
                             + "; ".join(not_restored) + " — put these back by hand.")
                if skipped and not other:
                    logger.error("PARTIAL RESTORE: every item NOT restored is an agent "
                                 "volume the backup skipped when it was taken: "
                                 + ", ".join(f"{v} ({skip_note(detail.get(v))})"
                                             for v in skipped))
                outcome = RESTORE_PARTIAL
            else:
                logger.info("Full Restore Complete.")
                outcome = True
            if keep_env:
                self._write_restore_status({
                    "format": RESTORE_STATUS_FORMAT,
                    "backup": backup_file,
                    "outcome": "partial" if outcome == RESTORE_PARTIAL else "complete",
                    "skipped_at_backup": skipped,
                    # what the backup's manifest says about each (None when it
                    # recorded nothing) and the line a caller can quote
                    "skipped_at_backup_detail": {
                        v: {**(detail.get(v) or {"skip_reason": None, "size_mb": None}),
                            "note": skip_note(detail.get(v))} for v in skipped},
                    "not_restored_other": other,
                    "only_skipped_at_backup": (outcome == RESTORE_PARTIAL
                                               and bool(skipped) and not other),
                    # #2524: informational — Komodo (monitoring) never fails
                    # the restore; this says when it came back empty, and why
                    "komodo_not_restored": self.komodo_restore_problem,
                    # #2712 review: in the backup, not restored, not missing
                    "retired_not_restored": retired,
                    "not_restored_by_design": by_design,
                })

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
            if stopped:
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
        # #1621 rev-B (review): read the marker OUT OF THE ARCHIVE.
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
                                  "by a version before 2026.09-ga). Restoring anyway — verify by "
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

        passphrases = restore_passphrases(env_path, first=self._archive_passphrase)
        if not passphrases:
            logger.warning(
                "No BACKUP_ENCRYPTION_PASSWORD in %s (nor in the environment). "
                "Cannot restore certs/ — the box will come back on self-signed "
                "TLS. The passphrase is NOT in the backup: it has to come from "
                "outside it.", env_path)
            return False

        target = stack_dir
        try:
            plain, why = openssl_decrypt_first(enc_path, passphrases, _is_gzip)
            if plain is None:
                raise subprocess.CalledProcessError(1, "openssl", stderr=f"{why}".encode())
            # #2464 fix round 1: name the member. /stack is :ro except the
            # `certs/` bind, and only `certs/` (and below) is extracted —
            # anything else the archive might carry is not.
            subprocess.run(["tar", "xzf", "-", "-C", target, "certs"],
                           input=plain, check=True, capture_output=True)
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
        passphrases = restore_passphrases(env_path, first=self._archive_passphrase)
        if not passphrases:
            logger.error(
                "box-local restore FAILED: no encryption password available "
                "(the backup carries the box-local state, but it cannot be "
                "decrypted) %s", lost)
            return False

        plain, why = openssl_decrypt_first(enc_path, passphrases, _is_gzip)
        if plain is None:
            logger.error("box-local restore FAILED (decrypt: %s) %s", f"{why}", lost)
            return False

        staging = tempfile.mkdtemp(prefix="box-local-restore-")
        try:
            try:
                names = extract_box_local_archive(plain, staging)
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

    def compose_local_overlays(self, env_path='/stack/.env'):
        """#3412: the box-local compose overlays the restored backup carries.

        Returns (overlays, skipped, why): ({name: bytes}, [(name, why)], None)
        when the archive was read — an entry outside the shared rule is
        SKIPPED, the others still come back; ({}, [], None) when the backup
        carries none (it predates #3412, or the box had none); (None, [], why)
        when the archive cannot be decrypted or is malformed. The manager
        cannot write them — the stack root is mounted read-only here (BSB-03) —
        so cli/backup.sh asks for them with `compose-local-export` and writes
        them on the host BEFORE it runs compose on the restored .env."""
        enc_path = os.path.join(RESTORE_ROOT, "databases", COMPOSE_LOCAL_ARCHIVE)
        if not os.path.exists(enc_path):
            return {}, [], None
        passphrases = restore_passphrases(env_path, first=self._archive_passphrase)
        if not passphrases:
            return None, [], "no encryption password is available"
        plain, why = openssl_decrypt_first(enc_path, passphrases, _is_gzip)
        if plain is None:
            return None, [], f"decrypt: {why}"
        try:
            files, skipped = compose_local.read_tar(plain)
        except (tarfile.TarError, OSError, EOFError, zlib.error, ValueError) as e:
            # #3436 L-20: zlib.error too — it escaped here and ended a full
            # restore after the databases and .env, before the agent volumes.
            return None, [], f"archive malformed: {e}"
        return files, skipped, None

    def export_compose_local(self, out, env_path='/stack/.env'):
        """#3412: write the restorable overlays to `out` as a plain tar (mode
        600 members). Exit code: 0 written (possibly empty), 1 failed."""
        overlays, skipped, why = self.compose_local_overlays(env_path)
        if overlays is None:
            logger.error("compose overlay export FAILED (%s): the backup carries "
                         "box-local compose overlays that cannot be read.", why)
            return 1
        for name, reason in skipped:
            logger.warning("compose overlay export: SKIPPED %r from the backup: %s",
                           name, reason)
        out.write(compose_local.write_tar(overlays, gz=False))
        out.flush()
        logger.info("compose overlay export: %s",
                    ", ".join(sorted(overlays)) if overlays else "the backup carries none")
        return 0

    @staticmethod
    def collect_compose_local(out, stack_dir='/stack', err=None):
        """#3412: the BACKUP side. Write the stack root's box-local overlays
        that the shared rule accepts to `out` as a tar.gz; name every skipped
        file on `err` (one line each, `SKIPPED <name>: <why> — <remedy>`) and
        the carried ones as `CARRIED <names>`. Exit code: 0 carried at least
        one, EXIT_COMPOSE_LOCAL_NONE carried none (an empty tar.gz is still
        written), 1 the stack root cannot be read."""
        err = err or sys.stderr
        if not os.path.isdir(stack_dir):
            print(f"ERROR the stack root {stack_dir} is not mounted", file=err)
            return 1
        files, skipped = compose_local.collect(stack_dir)
        for name, why in skipped:
            print(f"SKIPPED {name}: {why} — {compose_local.REMEDY}", file=err)
        out.write(compose_local.write_tar(files, gz=True))
        out.flush()
        if files:
            print("CARRIED " + " ".join(sorted(files)), file=err)
            return 0
        return EXIT_COMPOSE_LOCAL_NONE

    def restore_partial(self, backup_file, target_item):
        """Restores a single volume or database from a full backup.

        M-10: the volume is REPLACED by the backup's copy of it, as in
        restore_full (#125): extract (confined) into staging, then empty the
        live volume in place (its mountpoint, owner and mode are kept) and
        copy. It used to MERGE into the live volume, and the copy wrote
        through any symlink already there — one a compromised service could
        plant in its own volume. Entries the live volume has and the backup
        does not are removed. I-08: `target_item` must be a mounted volume."""
        backup_path = os.path.join(BACKUP_DIR, backup_file)
        self.stack_was_stopped = False
        try:
            mounted_restore_target(target_item)
        except UnknownRestoreTargetError as e:
            logger.error(f"Partial restore refused: {e}")
            return False
        if not os.path.exists(backup_path):
            return False

        # F-002: Verify integrity before restoring
        if not self._verify_checksum(backup_path):
            logger.error("Aborting partial restore due to checksum mismatch.")
            return False

        logger.info(f"Restoring {target_item} from {backup_file}...")
        # #2678: stop the stack (safer, generally) only after the volume's
        # members have passed validate_archive_members — below.
        stopped = False

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
                # M-10: the volume's own members only — `backup/vol2/…` does
                # not belong to `vol`.
                # #2639: a partial backup has this layout since 2026.11; one
                # made before stored the volume as `<vol>/…` — read that too.
                all_members = tar.getmembers()

                def _of(prefix):
                    return [m for m in all_members
                            if m.name.lstrip("/").rstrip("/") == prefix
                            or m.name.lstrip("/").startswith(prefix + "/")]
                member_prefix = f"backup/{target_item}"
                members = _of(member_prefix)
                # #3450: a volume archived from the hook's copy is restored
                # from that copy; an older backup holds it at the live path.
                if target_item in STAGED_VOLUMES:
                    staged = _of(f"backup/{STAGED_VOLUMES[target_item]}")
                    # #3478: refused BEFORE anything is stopped when the copy
                    # is marked incomplete (or missing), unless overridden.
                    mname = f"backup/{STAGED_VOLUMES[target_item]}{STAGING_ERROR_SUFFIX}"
                    marker = next((m for m in all_members
                                   if m.name.lstrip("/").rstrip("/") == mname), None)
                    if marker is not None:
                        if not marker.isfile():
                            reason = "the staging marker in the backup is not a regular file"
                        else:
                            fh = tar.extractfile(marker)
                            reason = _staging_marker_reason(
                                fh.read(8192).decode("utf-8", "replace") if fh else "")
                        why = incomplete_staged_verdict(target_item, reason,
                                                        copy_present=bool(staged))
                        if why is not None:
                            logger.error(f"Partial restore refused: {target_item}: {why} — "
                                         "nothing was stopped; the stack keeps running.")
                            return False
                    if staged:
                        members = staged
                        member_prefix = f"backup/{STAGED_VOLUMES[target_item]}"
                        logger.info(f"{target_item} is restored from its copy "
                                    f"{STAGED_VOLUMES[target_item]} in the backup.")
                if not members and os.path.basename(backup_file).startswith("partial-"):
                    members = _of(target_item)
                    if members:
                        member_prefix = target_item
                        logger.info(f"{backup_file} has the older partial layout "
                                    f"({target_item}/…); restoring from it.")

                if not members:
                    logger.error(f"Item {target_item} not found in backup.")
                    return False

                # #2678: the dry pass, before anything is stopped — including
                # the check that the volume's own entry is a directory.
                try:
                    validate_archive_members(normalise_member_names(members))
                    if any(m.name.rstrip("/") == member_prefix and not m.isdir()
                           for m in members):
                        raise UnsafeArchiveError(
                            f"the backup holds no directory for {target_item}")
                except UnsafeArchiveError as e:
                    logger.error(f"Partial restore refused: {e} — checked before "
                                 "anything was stopped; the stack keeps running.")
                    return False
                # #2632: stop only the consumers of the volume, restart only
                # those. WHOLE_STACK_VOLUMES are the exception: `databases`'
                # dumps are replayed into every service's database, and a
                # postgres datadir is swapped under every client — so every
                # running service is a consumer (and only those restart).
                if target_item in WHOLE_STACK_VOLUMES:
                    consumers = None
                else:
                    try:
                        consumers = self.volume_consumers(target_item)
                    except Exception as e:
                        logger.error(f"Partial restore refused: could not determine "
                                     f"which containers use {target_item} ({e}) — "
                                     "nothing was stopped; the stack keeps running.")
                        return False
                    logger.info(f"Stopping the consumers of {target_item}: "
                                + (", ".join(consumers) or "none is running"))
                stopped = self.stack_was_stopped = True
                self.stop_stack(consumers)
                # A container started by a control-plane service while the
                # stop ran survives it. Look again, and refuse before the
                # volume is emptied while anything still mounts it.
                survivors = self._still_running_on(target_item, consumers)
                if survivors:
                    raise RuntimeError(
                        f"{', '.join(survivors)} still "
                        + ("runs" if len(survivors) == 1 else "run")
                        + (f" on {target_item}" if consumers is not None
                           else " after the stop")
                        + "; nothing was restored and the volume was not touched.")

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
                # The checks above are lexical only. extract_confined refuses
                # device/FIFO members, hardlink escapes and any write through
                # a link, and extracts every symlink as a leaf, whatever its
                # target (#2678; `localtime -> /usr/share/zoneinfo/…`, which
                # Python 3.14's default `data` filter refused).
                extract_confined(tar, temp_dir, members)

                # Move
                src = os.path.join(temp_dir, member_prefix)
                if os.path.islink(src) or not os.path.isdir(src):
                    raise UnsafeArchiveError(
                        f"the backup holds no directory for {target_item}")
                # I-08, checked again right before the write.
                dst = mounted_restore_target(target_item)

                # M-10: empty the live volume first, as restore_full does
                # (#125) — keeping the mountpoint itself. A link already
                # in the volume is removed, never followed: the copy below
                # then writes into an empty directory only.
                self._empty_dir_contents(dst)
                # Symlinks as symlinks, like restore_full (rc6.7 #7): a
                # container-absolute link (`localtime -> /usr/share/…`)
                # must not be dereferenced on the manager's filesystem.
                shutil.copytree(src, dst, symlinks=True,
                                ignore_dangling_symlinks=True, dirs_exist_ok=True)
                # #2542: copytree drops uid/gid; put the archive's back.
                apply_archive_ownership(
                    dst, archive_owners(members, member_prefix),
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
            if stopped:
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
            f"pre-restore-safety-{_file_safe(db_name)}-"
            f"{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.dump",
        )
        try:
            with open(safety_path, 'wb') as sf:
                safety_result = subprocess.run(
                    ['docker', 'exec', container_name, 'pg_dump',
                     '-U', pg_user, '-Fc', '-d', pg_dbname_arg(db_name)],
                    stdout=sf, stderr=subprocess.PIPE,
                )
            if safety_result.returncode != 0:
                stderr = safety_result.stderr
                stderr = stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else (stderr or '')
                logger.warning(
                    f"pre-restore safety dump of {db_name!r} failed "
                    f"(db may not exist yet) — {stderr.strip()[:200]}"
                )
                try:
                    os.remove(safety_path)
                except OSError:
                    pass
            else:
                logger.info(f"pre-restore safety dump written: {safety_path}")
        except Exception as e:
            logger.warning(f"pre-restore safety dump step raised: {e}")

    def restore_single_database(self, backup_file, db_name):
        """targeted single-database restore.

        Customer-box incident (#278): a GPUStack version mishap corrupted
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
        problem = db_name_problem(db_name)
        if problem:
            logger.error(f"refusing to restore — invalid database name "
                         f"{db_name!r} ({problem}).")
            return False
        if db_name in _IN_PLACE_DATABASES:
            # The restore drops and recreates its target from the maintenance
            # DB `postgres`, which cannot drop itself; template1 is the
            # template every CREATE DATABASE copies.
            logger.error(
                f"refusing a single-database restore of {db_name!r} — it "
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
            f"restoring database {db_name!r} from {backup_file} "
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
                per_db_file = per_db_dump_filename(db_name)
                per_db_member = None if per_db_file is None else self._find_tar_member(
                    tar, f"backup/databases/{PER_DB_DUMP_SUBDIR}/{per_db_file}")
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
                                f"{dump_name} in {backup_file} is not a "
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
                    f"database {db_name!r} not found in any dump in "
                    f"{backup_file} — aborting; nothing was restored."
                )
                return False
            if len(candidates) > 1:
                logger.error(
                    f"database {db_name!r} found in MORE THAN ONE dump "
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
                    f"target container {container_name!r} for database "
                    f"{db_name!r} not found on this box — aborting; nothing "
                    f"was restored."
                )
                return False

            if target_ctr.status != 'running':
                logger.info(f"starting {container_name} for single-DB restore...")
                target_ctr.start()

            if not self._wait_for_postgres(container_name):
                logger.error(
                    f"{container_name} did not become ready within 30 "
                    f"seconds — aborting; nothing was restored."
                )
                return False

            self._pre_restore_safety_dump(container_name, db_name, pg_user)

            # DROP, then CREATE, against the maintenance DB ('postgres') —
            # never against db_name itself (can't drop a database you're
            # connected to). Touches ONLY db_name; db_name is validated above
            # and only ever used quoted (quote_ident / quote_literal), so it
            # cannot inject statements.
            # #2465 (fix round 1): these used to go as ONE `psql -c "DROP …;
            # CREATE …;"`, which PostgreSQL runs as a single implicit
            # transaction — DROP DATABASE refuses that ("cannot run inside a
            # transaction block"), so every single-DB restore aborted here.
            # They are separate statements on psql's stdin now, and the drop
            # first terminates the database's other sessions and uses
            # WITH (FORCE), exactly as the full restore does.
            verbose = b'\\set VERBOSITY verbose\n'
            drop_sql = verbose + _drop_database_sql(db_name)
            rc, err = _stream_psql(self._psql_argv(container_name, _MAINTENANCE_DB, True),
                                   prefix=drop_sql)
            if rc != 0:
                logger.error(
                    f"drop of {db_name!r} failed — aborting before any "
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
                    f'CREATE DATABASE {quote_ident(db_name)} '
                    f'OWNER {quote_ident(pg_user)};\n').encode('utf-8')
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
                            f"WHERE datname = {quote_literal(db_name)})::int;\n"
                            ).encode('utf-8'))
                if exists_rc == 0:
                    missing = sorted(set(re.findall(r'role "([^"]+)" does not exist', err)))
                    why = (f"role(s) {', '.join(missing)} do not exist on this box"
                           if missing else "a statement after CREATE DATABASE failed")
                    logger.error(
                        f"{db_name!r} was recreated but is EMPTY and "
                        f"INCOMPLETE — its setup failed ({why}), so nothing "
                        f"was restored into it: {_tail(err)}"
                    )
                else:
                    logger.error(
                        f"create of {db_name!r} failed after its drop — "
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
                    'pg_restore', '-U', pg_user, '-d', pg_dbname_arg(db_name),
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
                    f"restore of {db_name!r} into {container_name} "
                    f"FAILED: {stderr.strip()[:500]}"
                )
                return False

            logger.info(
                f"database {db_name!r} restored successfully from "
                f"{backup_file} into {container_name}."
            )
            return True

        except Exception as e:
            logger.error(f"single-database restore of {db_name!r} failed: {e}")
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
                '-d', pg_dbname_arg(db), '-v', f"ON_ERROR_STOP={1 if on_error_stop else 0}",
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
                        "Postgres did not become ready within 30 seconds "
                        "— the databases were NOT restored.")
                    return False
                logger.info("Restoring Postgres Core (every database in "
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
                        f"Database restore FAILED — {len(failures)} "
                        f"problem(s); the databases do NOT hold the backup's "
                        f"contents completely: " + " | ".join(failures))
            else:
                logger.warning("no postgres_core.sql in the backup — "
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

                    self.restore_komodo(
                        komodo_dump,
                        os.path.join(db_dump_dir, KOMODO_CATALOG_FILE))

            logger.info("Database restore steps applied.")

        except Exception as e:
            logger.error(f"Database restore error: {e}")
            return False
        return ok

    def restore_komodo(self, komodo_dump, catalog_path, container="postgres-komodo"):
        """Replay postgres_komodo.sql into ``container`` and re-link
        DocumentDB's collections (#2524). Returns True when Komodo's data is
        back and usable; False otherwise (logged as an ERROR, and recorded in
        ``self.komodo_restore_problem``). Komodo is monitoring: a failure here
        does not fail the restore, but it is never reported as success.

          0. The catalogue is checked FIRST. Without a whole one (every backup
             made before it existed) nothing is changed: the box keeps the
             Komodo data it has — on a fresh volume that is an empty Komodo,
             on a running box its live monitoring data.
          1. DocumentDB is emptied (DOCUMENTDB_CLEAR_SQL).
          2. The cluster dump replays leniently, as before: its `postgres`
             section runs in place next to the extension's own objects, so
             `CREATE EXTENSION … already exists` is expected.
          3. The catalogue replays with ON_ERROR_STOP, in one transaction,
             then DOCUMENTDB_RECONCILE_SQL drops collection tables no
             catalogue row names and lifts the id sequence above every id in
             use, so a new collection never collides with a restored table.
             A failed catalogue replay leaves unlinkable tables behind; they
             are dropped again so Komodo starts empty instead of crash-looping.
        """
        self.komodo_restore_problem = None
        logger.info("Restoring Postgres Komodo...")
        problem = documentdb_catalog_problem(catalog_path)
        if problem is not None:
            self.komodo_restore_problem = problem
            logger.error(
                f"Komodo's monitoring data was NOT restored: {problem}. Without "
                f"it the backup's collection tables cannot be linked to their "
                f"collections, so nothing was replayed and this box's Komodo "
                f"database was left as it was.")
            return False

        rc, err = _stream_psql(self._psql_argv(container, KOMODO_DB, True),
                               prefix=DOCUMENTDB_CLEAR_SQL)
        if rc != 0:
            self.komodo_restore_problem = (
                f"could not empty DocumentDB before the replay: {_tail(err)}")
            logger.error(f"Komodo database restore NOT done — "
                         f"{self.komodo_restore_problem}")
            return False

        rc, err = _stream_psql(self._psql_argv(container, KOMODO_DB, False),
                               komodo_dump, [(0, os.path.getsize(komodo_dump))])
        # #2465: psql without ON_ERROR_STOP exits 0 past failed statements,
        # so rc 0 alone is not success.
        komodo_errors = [ln for ln in (err or '').splitlines()
                         if re.search(r':\s+ERROR:\s', ln)]
        if rc != 0:
            logger.error(f"Komodo database restore failed: {_tail(err)}")
        elif komodo_errors:
            logger.warning(
                f"Komodo database replay applied with {len(komodo_errors)} "
                f"error(s) (the extension's own objects already exist): "
                f"{komodo_errors[:5]}")

        rc, err = _stream_psql(self._psql_argv(container, KOMODO_DB, True),
                               catalog_path,
                               [(0, os.path.getsize(catalog_path))],
                               suffix=DOCUMENTDB_RECONCILE_SQL)
        if rc == 0:
            logger.info("Komodo database restore completed successfully "
                        "(DocumentDB catalogue re-linked).")
            return True

        problem = f"replaying the DocumentDB catalogue failed: {_tail(err)}"
        self.komodo_restore_problem = problem
        logger.error(
            f"Komodo's monitoring data was NOT restored: {problem}. Its "
            f"collection tables cannot be linked to their collections, so "
            f"they are removed and Komodo starts EMPTY (the backup archive "
            f"still holds them).")
        rc, err = _stream_psql(self._psql_argv(container, KOMODO_DB, True),
                               prefix=DOCUMENTDB_CLEAR_SQL)
        if rc != 0:
            logger.error(f"removing Komodo's unlinked collection tables "
                         f"failed — komodo-core will crash-loop on them: "
                         f"{_tail(err)}")
        return False

    def restore_env_files(self, env_path='/stack/.env', stack_dir='/stack', keep_env=False):
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

        #2532: returns True (every env file the backup carries is back), None
        (the backup carries none: the box keeps its own) or False (at least one
        was NOT restored, and that file on the box is untouched). The member is
        decrypted INTO MEMORY and checked before the live file is written:
        `openssl enc -d -out <live file>` truncated it first and left ciphertext
        garbage there on a wrong passphrase.

        ``keep_env``: the box's files are not written at all (so a stale
        single-file bind does not matter); each decrypted file goes to
        BACKUP_DIR/RESTORED_ENV_SUBDIR/<name>, a fresh 0700 directory, mode 600.
        """
        db_dump_dir = os.path.join(RESTORE_ROOT, "databases")
        env_files = [
            ("env-backup.enc", ".env"),
            ("env-dify-backup.enc", ".env.dify"),
        ]
        present = [(e, t) for e, t in env_files
                   if os.path.exists(os.path.join(db_dump_dir, e))]
        if not present:
            logger.warning("the backup carries no encrypted .env / .env.dify; "
                           "the box keeps its own.")
            return None

        passphrases = restore_passphrases(env_path, first=self._archive_passphrase)
        if not passphrases:
            logger.error(".env restore FAILED: no encryption password is available, "
                         "so the backup's .env cannot be decrypted. The passphrase is NOT "
                         "in the backup: it has to come from outside it.")
            return False

        ok = True
        out_dir = None
        if keep_env:
            out_dir = os.path.join(BACKUP_DIR, RESTORED_ENV_SUBDIR)
            try:
                if os.path.lexists(out_dir):
                    _remove_entry(out_dir)
                os.mkdir(out_dir, 0o700)
            except OSError as e:
                logger.error(f"cannot prepare {out_dir} for the backup's .env: {e}")
                return False
        for enc_name, target_name in present:
            enc_path = os.path.join(db_dump_dir, enc_name)
            target_path = os.path.join(out_dir or stack_dir, target_name)
            # #1224: a stale single-file bind: refuse with the recovery.
            state = {'stale': False} if keep_env else inspect_env_file(target_path)
            if state['stale']:
                logger.error(
                    f"Not restoring {target_name}: {state['reason']}. Recreate this "
                    f"container once so it picks up the current host file — "
                    f"`{recreate_cmd(ENV_MOUNT_SERVICE)}` in the stack directory on "
                    f"the host — then run the restore again.")
                ok = False
                continue
            plain, why = openssl_decrypt_first(enc_path, passphrases, _looks_like_env)
            if plain is None:
                logger.error(f"{target_name} restore FAILED: the backup's copy does "
                             f"not decrypt with any passphrase in the keyring ({why}). "
                             f"The box's {target_name} is untouched.")
                ok = False
                continue
            try:
                if keep_env:
                    fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'wb') as fh:
                        fh.write(plain)
                    logger.info(f"Kept the box's {target_name}; the backup's copy is in "
                                f"{target_path} (mode 600).")
                    continue
                _write_env_in_place(target_path, plain)
            except OSError as e:
                if e.errno == errno.EROFS:
                    # #1224: the dropped-bind variant.
                    logger.error(explain_stale(target_path, 'Read-only file system',
                                               ENV_MOUNT_SERVICE))
                else:
                    logger.error(f"{target_name} restore FAILED writing "
                                 f"{target_path}: {e}")
                ok = False
                continue
            logger.info(f"Restored {target_name} from encrypted backup.")
        return ok

    @staticmethod
    def _restore_status_path():
        return os.path.join(BACKUP_DIR, RESTORED_ENV_SUBDIR, RESTORE_STATUS_FILE)

    def _drop_restore_status(self):
        path = self._restore_status_path()
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError as e:
            logger.error(f"cannot remove the previous restore status {path}: {e}")

    def _write_restore_status(self, status):
        """#2708: the restore's own account (RESTORE_STATUS_FILE), mode 600,
        beside the handed-over .env. A write that fails is logged; the caller
        then finds no status and must treat the restore as unproven."""
        out_dir = os.path.join(BACKUP_DIR, RESTORED_ENV_SUBDIR)
        path = self._restore_status_path()
        try:
            if not os.path.lexists(out_dir):
                os.mkdir(out_dir, 0o700)
            if os.path.lexists(path):
                os.remove(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(status, fh, indent=1)
                fh.write("\n")
            logger.info(f"restore status written to {path}")
        except OSError as e:
            logger.error(f"cannot write the restore status {path}: {e}")

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
        a 2026.09-ga.4+ backup, the names of the entries that cannot be
        restored for any reason but one (an invalid instance directory, an
        unreadable manifest, a bad name, a listed archive that is missing), and
        — kept apart (#2708) — the volumes the backup skipped when it was taken
        (the manifest's `archived` is not true), with what the manifest says
        about why (`skip_reason`, `size_mb`; absent in older backups).

        Every archived volume goes back under the name it was TAKEN from (the
        manifest's `name`), never under a name rebuilt from slug + type + role:
        the container stem and the row's user_slug differ on real boxes, and
        a second instance of one type shares the first one's prefix."""
        plan, not_restored, skipped_at_backup, skip_detail, seen = [], [], [], {}, set()
        for manifest_path in manifests:
            inst_dir = os.path.dirname(manifest_path)
            inst = os.path.basename(inst_dir)
            if not self._AGENT_INSTANCE_DIR_RE.match(inst):
                logger.error(f"agent volume restore FAILED for agents/{inst!r}: "
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
                logger.error(f"agent volume restore FAILED for {inst}: "
                             f"unreadable manifest ({e}).")
                not_restored.append(f"agents/{inst}")
                continue
            for vol in volumes:
                name = vol.get("name") if isinstance(vol, dict) else None
                if not isinstance(name, str) or not self._AGENT_VOLUME_NAME_RE.match(name):
                    logger.error(f"agent volume restore FAILED for {inst}: "
                                 f"{name!r} is not an agent volume name.")
                    not_restored.append(f"{inst}:{name!r}")
                    continue
                if vol.get("archived") is not True:
                    # Skipped when the backup was taken (disk ceiling, size
                    # cap, missing volume, snapshot failure): the instance's
                    # data is NOT all back — PARTIAL, by name (#2531 review).
                    logger.error(f"agent volume restore FAILED for {name} "
                                 f"({manifest.get('container_name')}): it is not in "
                                 "this backup — it was skipped when the backup was taken.")
                    skip_detail[name] = {"skip_reason": vol.get("skip_reason"),
                                         "size_mb": vol.get("size_mb")}
                    logger.error(f"{SKIPPED_AT_BACKUP_MARKER}: {name} "
                                 f"({skip_note(skip_detail[name])})")
                    skipped_at_backup.append(name)
                    continue
                tar_path = os.path.join(inst_dir, f"{name}.tar")
                if not os.path.isfile(tar_path):
                    logger.error(f"agent volume restore FAILED for {name}: "
                                 f"the manifest lists {tar_path}, which is missing.")
                    not_restored.append(name)
                    continue
                if name in seen:
                    logger.info(f"{name} is mounted by more than one "
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
                logger.warning(f"ignoring agents/{entry}/ — no manifest in "
                               "an instance-keyed backup (a leftover of an older layout).")
        # A volume more than one instance mounts may be skipped (or missing)
        # under one instance and archived under another: it IS restored, from
        # the archive that exists, so it is not missing.
        planned = {name for name, _ in plan}
        for name in sorted(planned & (set(skipped_at_backup) | set(not_restored))):
            logger.info(f"{name} is restored from another instance's archive; "
                        "it is not missing.")
        skipped_at_backup = [n for n in skipped_at_backup if n not in planned]
        not_restored = [n for n in not_restored if n not in planned]
        skip_detail = {n: d for n, d in skip_detail.items() if n not in planned}
        return plan, not_restored, skipped_at_backup, skip_detail

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
        failure does not abort the rest. #2537: the target volume is emptied
        first (replace, not merge; the core volumes' #125 semantics), in the
        same GNU-tar helper run, and a volume a running container mounts is
        refused untouched.

        Returns None (the backup carries no agent volumes), True (every
        archive restored) or False (at least one archive was NOT restored —
        #2531: that makes a full restore PARTIAL, exit 3, like certs/ and the
        box-local state; the databases and .env are back regardless).

        #2708: `self.agent_restore_report` says what was not restored, in two
        lists: `skipped_at_backup` (the backup skipped them when it was taken)
        and `not_restored_other` (every other cause). It does not change the
        return value: a skipped volume is still not back.
        """
        skipped_at_backup, not_restored, skip_detail = [], [], {}
        self.agent_restore_report = {"skipped_at_backup": skipped_at_backup,
                                     "skipped_at_backup_detail": skip_detail,
                                     "not_restored_other": not_restored}
        agent_root = os.path.join(RESTORE_ROOT, "databases", "agents")
        if not os.path.isdir(agent_root):
            logger.info("M030 S4: no agents/ subtree in backup — skipping per-user agent restore.")
            return None

        manifests = sorted(
            os.path.join(agent_root, e, self.AGENT_MANIFEST)
            for e in os.listdir(agent_root)
            if os.path.isfile(os.path.join(agent_root, e, self.AGENT_MANIFEST)))
        if manifests:
            logger.info(f"instance-keyed agent backup — {len(manifests)} "
                        "instance manifest(s).")
            plan, other, skipped, detail = self._agent_restore_plan_by_manifest(
                agent_root, manifests)
            not_restored.extend(other)
            skipped_at_backup.extend(skipped)
            skip_detail.update(detail)
        else:
            plan = self._agent_restore_plan_legacy(agent_root)
        if not plan and not not_restored and not skipped_at_backup:
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
            not_restored.extend(name for name, _ in plan)
            return False

        tar_image = agent_tar_image()
        if not tar_image:
            logger.error(f"agent volume restore FAILED — no GNU tar helper "
                         f"({len(plan)} volume(s) NOT restored).")
            not_restored.extend(name for name, _ in plan)
            return False

        restored = 0
        for volume_name, tar_path in plan:
            # The tar path goes into the helper's `sh -c` inside quotes: only
            # plain path characters, no `..` (#2531 review; the legacy layout's
            # directory names are not validated anywhere else).
            rel_in_volume = os.path.relpath(tar_path, os.path.join(RESTORE_ROOT, "databases"))
            if (not self._AGENT_TAR_REL_RE.match(rel_in_volume)
                    or ".." in rel_in_volume.split("/")):
                logger.error(f"agent volume restore FAILED for {volume_name!r}: "
                             f"unsafe archive path {rel_in_volume!r}.")
                not_restored.append(volume_name)
                continue
            # #2537: the volume is REPLACED below. A container still running on
            # it would keep writing (a live SQLite WAL next to the restored db),
            # so refuse it untouched. A full restore has stopped the stack
            # (stop_stack); this catches what came back or was never stopped.
            busy = subprocess.run(
                ["docker", "ps", "-q", "--filter", f"volume={volume_name}"],
                capture_output=True, text=True, check=False)
            if busy.returncode != 0 or busy.stdout.strip():
                why = (f"a running container still mounts it "
                       f"({', '.join(busy.stdout.split())}); stop it first"
                       if busy.returncode == 0 else
                       f"cannot tell whether a running container mounts it "
                       f"({(busy.stderr or '').strip()[:200]})")
                logger.error(f"agent volume restore FAILED for {volume_name}: "
                             f"{why}. The volume was NOT touched.")
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
                "-c", ("find /restore -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + "
                       f"&& tar -C /restore --numeric-owner --sparse -xf '/dumps/{rel_in_volume}' 2>&1"),
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

        missing = skipped_at_backup + not_restored
        logger.info(
            f"M030 S4: per-user agent volume restore complete — "
            f"{restored} restored, {len(missing)} failure(s)."
        )
        if missing:
            logger.error(f"agent volume restore FAILED — NOT restored: "
                         f"{', '.join(missing)} (see the lines above).")
            return False
        return True


    def _restore_staged_volumes(self, source_root, members):
        """#3450: restore each STAGED_VOLUMES volume from its copy below
        `source_root` (the extracted `backup/`), replacing the live volume as
        the volume loop does (#125) and with the archive's owners (#2542).
        A backup without the copy (made before it existed) is left to the
        volume loop. Returns [(volume, why)] for every copy NOT restored."""
        failures = []
        for vol_name, rel in sorted(STAGED_VOLUMES.items(), key=lambda kv: kv[0]):
            src = os.path.join(source_root, rel)
            # #3478: the staging verdict travels with the archive.
            reason = read_staging_marker_file(src + STAGING_ERROR_SUFFIX)
            if reason is not None:
                why = incomplete_staged_verdict(vol_name, reason,
                                                copy_present=os.path.lexists(src))
                if why is not None:
                    failures.append((vol_name, why))
                    continue
            if not os.path.lexists(src):
                continue
            if not _is_real_dir(src):
                failures.append((vol_name, "its copy in the backup is not a directory"))
                continue
            dst = os.path.join(RESTORE_ROOT, vol_name)
            if not _is_real_dir(dst):
                failures.append((vol_name, "no restore target is mounted for it"))
                continue
            logger.info(f"Restoring volume: {vol_name} (from its copy {rel}) -> {dst}")
            try:
                self._empty_dir_contents(dst)
                shutil.copytree(src, dst, symlinks=True,
                                ignore_dangling_symlinks=True, dirs_exist_ok=True)
                apply_archive_ownership(dst, archive_owners(members, f"backup/{rel}"),
                                        f"volume {vol_name}")
            except Exception as e:
                failures.append((vol_name, str(e)))
        return failures

    @staticmethod
    def _empty_dir_contents(path):
        """#125: remove everything INSIDE `path` without removing `path` itself.

        Used to clear a restore-target volume before copying the backup in, so a
        restore REPLACES rather than MERGES over pre-existing data (the fresh-
        init-then-restore-older-backup DR path). We must not remove the mountpoint
        dir itself — docker owns it and removing it would detach the bind mount.

        I-09 (2026.09-ga.5): EVERY entry goes. Anything that is not a real
        directory (by lstat: a symlink, a regular file, a FIFO, a socket, a
        device node) is unlinked, never followed; a real directory is removed
        with rmtree. A FIFO or socket used to survive, and the copy then failed
        on it after the volume had already been emptied.
        """
        for entry in os.listdir(path):
            full = os.path.join(path, entry)
            try:
                if stat.S_ISDIR(os.lstat(full).st_mode):
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
            except Exception as e:
                # Re-raise so the caller records this as a volume-restore failure
                # (clearing must succeed for a clean restore).
                raise RuntimeError(f"could not clear {full}: {e}") from e

    @staticmethod
    def _is_plumbing(name):
        """The containers no restore stops: this manager, the backup service,
        and docker-socket-proxy — how this manager reaches the Docker API.
        Stopping the proxy severs our OWN connection mid-stop → the very next
        Docker call aborts with RemoteDisconnected and the whole restore fails
        (data never restored). It holds no state."""
        return name in ("razzfazz-backup-management", "backup-service",
                        "docker-socket-proxy", os.environ.get("HOSTNAME"))

    def volume_consumers(self, target):
        """#2632: the names of the RUNNING containers that mount the volume
        behind RESTORE_ROOT/<target> — the ones a restore of that volume must
        stop and restart. The volume's Docker name is read off this
        container's own mount (no compose project prefix is guessed). Raises
        when it cannot be determined: a restore must not write a volume a
        running service still has open."""
        me = client.containers.get(os.environ.get("HOSTNAME") or "razzfazz-backup-management")
        dest = os.path.join(RESTORE_ROOT, target)
        names = [m.get("Name") for m in (me.attrs.get("Mounts") or [])
                 if m.get("Destination") == dest and m.get("Name")]
        if not names:
            raise RuntimeError(f"no Docker volume is mounted at {dest}")
        volume = names[0]
        return [c.name for c in client.containers.list()
                if not self._is_plumbing(c.name)
                and any(m.get("Name") == volume for m in (c.attrs.get("Mounts") or []))]

    def _still_running_on(self, target, consumers):
        """The running containers a restore of `target` must not write under,
        listed again after the stop step: the volume's mounters, or (a
        whole-stack volume, `consumers is None`) every running container but
        the backup plumbing. A failed listing counts as "cannot tell" and
        raises, so the restore refuses."""
        if consumers is None:
            return [c.name for c in client.containers.list()
                    if not self._is_plumbing(c.name)]
        return self.volume_consumers(target)

    def stop_stack(self, names=None):
        """Stops the running containers — all of them except the backup
        plumbing, or (#2632) only `names`. Records what it stopped: that,
        and only that, is what start_stack starts again."""
        self._stopped_containers = []
        wanted = None if names is None else set(names)
        for container in client.containers.list():
            if self._is_plumbing(container.name):
                continue
            if wanted is not None and container.name not in wanted:
                continue
            logger.info(f"Stopping {container.name}...")
            container.stop()
            self._stopped_containers.append(container.name)

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
                "`docker compose up -d --force-recreate` (re-reads restored .env) "
                "— skipping in-container `docker start` bring-up."
            )
            return

        logger.warning(
            "start_stack: bringing the stack back with `docker start` (legacy "
            "in-container path). This re-uses each container's PRE-restore "
            "environment and will NOT apply a restored .env that changed secrets. "
            "For cross-secret-boundary restores use "
            "`razzfazz-backup.sh restore`, which force-recreates host-side."
        )

        # #2632: only what stop_stack stopped. The old sweep started EVERY
        # stopped container of the project — a disabled module's too (with
        # the matrix module off, synapse and element-web came back after a
        # restore). What ran before the restore is what COMPOSE_PROFILES
        # selected; that is what runs after it. Core services first.
        ordered = ["postgres", "valkey", "authentik-server", "authentik-worker", "caddy"]
        stopped = list(self._stopped_containers)
        for name in ([n for n in ordered if n in stopped]
                     + [n for n in stopped if n not in ordered]):
            try:
                client.containers.get(name).start()
                logger.info(f"Started {name}")
            except Exception as e:
                logger.error(f"Failed to start {name}: {e}")
        self._stopped_containers = []

def main(argv=None):
    """CLI entry. #2465: returns the exit code — a failed restore exits
    non-zero, so cli/backup.sh and the config portal hear about it."""
    bm = BackupManager()
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['list', 'backup', 'restore', 'delete',
                                           'compose-local-export', 'compose-local-collect'])
    # #279: 'single' targets one database (--database <name>); it never
    # stops the rest of the stack. See restore_single_database().
    parser.add_argument('--type', choices=['full', 'partial', 'single'], default='full')
    parser.add_argument('--target', help='Specific volume or file to Restore/Backup')
    parser.add_argument('--file', help='Backup file to restore from or delete')
    parser.add_argument('--database', help='database name for --type single restore')
    parser.add_argument('--check-keys', action='store_true',
                        help='name the keyring generation that opens each archive')
    parser.add_argument('--env', default='/stack/.env', help=argparse.SUPPRESS)
    # full restore only: leave the box's .env / .env.dify alone, hand the
    # backup's over in BACKUP_DIR/RESTORED_ENV_SUBDIR (cli/backup.sh --keep-env)
    parser.add_argument('--keep-env', action='store_true', help=argparse.SUPPRESS)

    # #2464 fix round 2: argparse exits 2 on a usage error (`--file -h`,
    # `--type bogus`); 2 must never reach a caller that could read it as
    # anything but a failure. Every usage error — and --help — is exit 1.
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    if args.action in ('compose-local-export', 'compose-local-collect'):
        # #3412: stdout carries the tar, so the log goes to stderr for these.
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler) and getattr(h, 'stream', None) is sys.stdout:
                h.setStream(sys.stderr)
        if args.action == 'compose-local-collect':
            return bm.collect_compose_local(sys.stdout.buffer)
        return bm.export_compose_local(sys.stdout.buffer, args.env)
    if args.action == 'list':
        entries = keyring_from_env_file(args.env) if args.check_keys else []
        for f in bm.list_backups():
            if not args.check_keys:
                print(f)
            elif not is_encrypted_backup(f):
                print(f"{f}\tplaintext (not encrypted)")
            else:
                hit = archive_key_entry(os.path.join(BACKUP_DIR, f), entries)
                print(f"{f}\tkey: {hit[0]}" if hit else
                      f"{f}\tkey: NOT HELD \u2014 the product cannot restore it")
    elif args.action == 'delete':
        if not args.file:
            print("Error: --file argument is required for delete action.")
            return 1
        # #2713: a no-op or a half-done delete is a failure, with the reason
        # in the log line; stdout carries it to the caller too.
        if not bm.delete_backup(args.file):
            print(f"Error: {os.path.basename(args.file)} was not (fully) deleted; see the log.")
            return 1
        print(f"Deleted {os.path.basename(args.file)}")
    elif args.action == 'backup':
        if args.type == 'full':
            before = set(bm.list_backups())
            bm.trigger_full_backup()
            # #2713: name the archive(s) this run added, so a caller can find its own
            for name in sorted(set(bm.list_backups()) - before):
                print(f"ARCHIVE: {name}")
        else:
            # #2639: a refused or failed partial backup is a failure
            ok = bm.trigger_partial_backup(args.target) is True
            if ok and getattr(bm, 'last_archive', None):
                print(f"ARCHIVE: {bm.last_archive}")
            return 0 if ok else 1
    elif args.action == 'restore':
        if args.type == 'full':
            # #2464 fix round 1/2: 0 complete, 3 PARTIAL (databases and .env
            # restored; certs/, box-local state, an agent volume or — #2712 —
            # a non-critical core volume not — cli/backup.sh still recreates
            # the stack and names what is missing), 1 failed.
            result = (bm.restore_full(args.file, keep_env=True) if args.keep_env
                      else bm.restore_full(args.file))
            if result is True:
                return 0
            if result == RESTORE_PARTIAL:
                return EXIT_RESTORE_PARTIAL
            # #2678: failed before anything was stopped: exit 4.
            return 1 if bm.stack_was_stopped else EXIT_RESTORE_REFUSED
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
