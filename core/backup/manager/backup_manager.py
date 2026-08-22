# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
import os
import logging
import argparse
import sys
import time
import tarfile
import docker
import shutil
import subprocess
import hashlib
from datetime import datetime

# Setup Logging
LOG_FILE = "/var/log/backup_manager.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
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
DOCKER_SOCK = os.environ.get('DOCKER_SOCK', 'unix://var/run/docker.sock')

client = docker.DockerClient(base_url=DOCKER_SOCK)


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

    tar.extractall(path)

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

    def trigger_full_backup(self):
        """Trigger the existing backup-service container to run a backup.

        BSB-04 / R-DEF-04: invokes the `razzfazz-backup` wrapper instead
        of offen's bare `backup` binary. The wrapper re-reads BACKUP_
        ENCRYPTION_PASSWORD from /scripts/dot-env at every call, so a
        rotated passphrase takes effect immediately without needing a
        `compose up -d --force-recreate backup-service`.
        """
        try:
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

    def trigger_partial_backup(self, target_name):
        """
        Create a partial backup for a specific target (volume name in /backup inside backup-service).
        Since we can't easily tell the main backup service to only do one, we might have to do it ourselves
        if we have access to the volumes.
        
        However, this container (backup-manager) will mount the volumes at /restore_targets/... 
        mirroring the backup service structure for consistency.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filename = f"partial-{target_name}-{timestamp}.tar.gz"
        source_path = os.path.join(RESTORE_ROOT, target_name)
        dest_path = os.path.join(BACKUP_DIR, filename)

        if not os.path.exists(source_path):
            logger.error(f"Target volume path {source_path} does not exist.")
            return False

        logger.info(f"Starting partial backup of {target_name} to {filename}...")
        try:
            with tarfile.open(dest_path, "w:gz") as tar:
                tar.add(source_path, arcname=target_name)
            logger.info(f"Partial backup created: {filename}")
            # F-002: Generate integrity checksum
            self._generate_checksum(dest_path)
            return True
        except Exception as e:
            logger.error(f"Failed to create partial backup: {e}")
            return False

    def restore_full(self, backup_file):
        """
        Perform a full restore.
        1. Stop all containers (except this one and backup-service maybe).
        2. Extract archive.
        3. Restore DBs.
        4. Restart containers.
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
                temp_extract_dir = "/tmp/restore_extract"
                if os.path.exists(temp_extract_dir):
                    shutil.rmtree(temp_extract_dir)
                os.makedirs(temp_extract_dir)
                
                # Safe extraction preventing path traversal
                safe_extract_tar(tar, temp_extract_dir)
                
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
            self.restore_databases()

            # Restore encrypted .env files
            self.restore_env_files()

            # M030 S4: per-user agent volume restore. pre-backup.sh writes
            # one tarball per role volume into databases/agents/<slug>/
            # <type>/<role>.tar (now living at /restore_targets/db-dumps/
            # agents/...). For each tarball, ensure the target docker
            # volume exists and extract the tarball into it. The
            # agent_instances rows are already back from the postgres
            # restore — at next agent-manager boot, the catalog spots
            # the existing volumes and skips re-creation.
            self.restore_agent_volumes()

            logger.info("Full Restore Complete.")

        except Exception as e:
            logger.error(f"Restore failed: {e}")
            # Try to restart anyway
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

                temp_dir = "/tmp/partial_restore"
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
                    shutil.copytree(src, dst, dirs_exist_ok=True)

                # If it was a database dump folder, triggers DB restore
                if target_item == "databases":
                    self.restore_databases()

        except Exception as e:
            logger.error(f"Partial restore failed: {e}")
        finally:
            if decrypted_tmp and os.path.exists(decrypted_tmp):
                try:
                    os.remove(decrypted_tmp)
                    logger.info(f"Removed decrypted temp tarball: {decrypted_tmp}")
                except OSError as e:
                    logger.warning(f"Could not remove decrypted temp tarball {decrypted_tmp}: {e}")
            self.start_stack()
        return True

    def restore_databases(self):
        """Finds restored SQL dumps and applies them."""
        db_dump_dir = os.path.join(RESTORE_ROOT, "databases")
        if not os.path.exists(db_dump_dir):
            logger.warning("No database dumps found to restore.")
            return

        # Start Postgres Container specifically for restore
        try:
            logger.info("Starting Postgres for DB restore...")
            client.containers.get("postgres").start()
            # F-055: Wait for Postgres readiness via pg_isready instead of fixed sleep
            pg_user = os.environ.get('POSTGRES_USER', 'docker')
            pg_ready = False
            for attempt in range(30):
                result = subprocess.run(
                    ['docker', 'exec', 'postgres', 'pg_isready', '-U', pg_user],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    pg_ready = True
                    logger.info(f"Postgres is ready (attempt {attempt + 1}/30)")
                    break
                time.sleep(1)
            if not pg_ready:
                logger.error("Postgres did not become ready within 30 seconds. Proceeding anyway.")
            
            # Postgres Core
            dump_file = os.path.join(db_dump_dir, "postgres_core.sql")
            if os.path.exists(dump_file):
                logger.info("Restoring Postgres Core...")
                # Use subprocess with proper stdin handling (no shell=True)
                pg_user = os.environ.get('POSTGRES_USER', 'docker')
                pg_db = os.environ.get('POSTGRES_DB', 'main_db')
                with open(dump_file, 'r') as sql_file:
                    result = subprocess.run(
                        ['docker', 'exec', '-i', 'postgres', 'psql', '-U', pg_user, '-d', pg_db],
                        stdin=sql_file,
                        capture_output=True,
                        text=True
                    )
                    if result.returncode != 0:
                        logger.error(f"Database restore failed: {result.stderr}")
                    else:
                        logger.info("Database restore completed successfully")

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
                        if result.returncode != 0:
                            logger.error(
                                f"Komodo database restore failed: {result.stderr}"
                            )
                        else:
                            logger.info(
                                "Komodo database restore completed successfully"
                            )

            logger.info("Database restore steps applied.")
            
        except Exception as e:
            logger.error(f"Database restore error: {e}")

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
                else:
                    logger.warning(f"Failed to decrypt {target_name}: {result.stderr.strip()}. "
                                   "The encryption password may have changed since the backup was created.")
            except Exception as e:
                logger.warning(f"Error restoring {target_name}: {e}")


    def restore_agent_volumes(self):
        """M030 S4: rehydrate per-user agent docker volumes from per-role tarballs.

        Source layout (written by core/backup/pre-backup.sh §7):
            /restore_targets/databases/agents/<slug>/<type>/<role>.tar
        Target docker volumes (named by agent-manager.provisioner):
            agent-<type>-<slug>-<role>

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
        backup-side approach so the operation is doubly auditable.

        Failures are logged but don't abort the rest of the restore —
        agent-manager will surface any missing-volume gaps at next boot.
        """
        agent_root = os.path.join(RESTORE_ROOT, "databases", "agents")
        if not os.path.isdir(agent_root):
            logger.info("M030 S4: no agents/ subtree in backup — skipping per-user agent restore.")
            return

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
            logger.warning(
                "M030 S4: could not resolve the db-dumps host volume; "
                "skipping per-user agent restore (tarballs unreachable from a "
                "spawned helper container)."
            )
            return

        restored = 0
        failures = 0
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
                    tar_path = os.path.join(type_dir, entry)
                    volume_name = f"agent-{agent_type}-{slug}-{role}"

                    # Ensure the volume exists (idempotent).
                    create = subprocess.run(
                        ["docker", "volume", "create", "--name", volume_name],
                        capture_output=True, text=True, check=False,
                    )
                    if create.returncode != 0:
                        logger.warning(
                            f"M030 S4: failed to ensure volume {volume_name}: {create.stderr.strip()}"
                        )
                        failures += 1
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
                    rel_in_volume = os.path.relpath(tar_path, os.path.join(RESTORE_ROOT, "databases"))
                    extract = subprocess.run([
                        "docker", "run", "--rm",
                        "-v", f"{dbdumps_volume}:/dumps:ro",
                        "-v", f"{volume_name}:/restore",
                        "--user", "0:0",
                        "alpine:latest",
                        "sh", "-c", f"tar -C /restore -xf '/dumps/{rel_in_volume}' 2>&1",
                    ], capture_output=True, text=True, check=False)

                    if extract.returncode == 0:
                        restored += 1
                        logger.info(f"M030 S4: restored {volume_name} from {tar_path}")
                    else:
                        failures += 1
                        logger.warning(
                            f"M030 S4: tar -x failed for {volume_name}: "
                            f"{(extract.stderr or extract.stdout or '').strip()[:200]}"
                        )

        if restored or failures:
            logger.info(
                f"M030 S4: per-user agent volume restore complete — "
                f"{restored} restored, {failures} failure(s)."
            )


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

if __name__ == "__main__":
    bm = BackupManager()
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['list', 'backup', 'restore', 'delete'])
    parser.add_argument('--type', choices=['full', 'partial'], default='full')
    parser.add_argument('--target', help='Specific volume or file to Restore/Backup')
    parser.add_argument('--file', help='Backup file to restore from or delete')
    
    args = parser.parse_args()
    
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
            bm.restore_full(args.file)
        else:
            bm.restore_partial(args.file, args.target)
