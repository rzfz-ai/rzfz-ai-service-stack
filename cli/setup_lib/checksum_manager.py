# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""
Checksum Governance Manager for razzfazz.ai Stack

Tracks SHA256 checksums of all governance-relevant files (scripts, configs,
compose files, Dockerfiles, blueprints) in a SQLite database. Supports
historised checksum sets with timestamps and comments, detail views, and
diffs between sets.

Storage: .checksums.db in stack root (/stack/.checksums.db in container)
"""

import hashlib
import os
import glob
import sqlite3
import json
from datetime import datetime, timezone

# #22: configurable so the CLI backend runs on the host (default /stack
# for legacy in-container invocation).
STACK_ROOT = os.environ.get("RAZZFAZZ_STACK_ROOT", "/stack")
DB_PATH = os.path.join(STACK_ROOT, ".checksums.db")

# Files and patterns to checksum (relative to stack root)
# Covers: scripts, compose files, Dockerfiles, configs, blueprints, Python apps
GOVERNANCE_PATTERNS = [
    # Root scripts
    "razzfazz-init.sh",
    "razzfazz-setup.sh",
    "razzfazz-backup.sh",
    "razzfazz-checksum.sh",
    "razzfazz-logs.sh",
    "razzfazz-upgrade.sh",
    "razzfazz-package.sh",
    # Root config
    "compose.yml",
    "VERSION",
    # Migrations (2026.07 Layout-A reorg: moved under config/)
    "config/migrations/env-changes.json",
    "scripts/prepare-release.sh",
    ".env",
    ".env.dify",
    "config/.env.example",
    "config/.env.dify.example",
    # Core
    "core/compose.yml",
    "core/init-authentik.sh",
    "core/init-db.sh",
    "core/Caddy/Caddyfile",
    "core/Caddy/Dockerfile",
    "core/Caddy/entrypoint.sh",
    "core/help/Dockerfile",
    "core/help/app.py",
    "core/help/cache_manager.py",
    "core/help/mirror_config.json",
    "core/licenses/Dockerfile",
    "core/licenses/app.py",
    # #22: razzfazz-setup web container removed; CLI backend relocated to
    # cli/setup_lib/ (still governance-tracked).
    "cli/setup_lib/setup.py",
    "cli/setup_lib/config_manager.py",
    "cli/setup_lib/checksum_manager.py",
    "cli/setup_lib/log_manager.py",
    "core/backup/pre-backup.sh",
    "core/backup/manager/Dockerfile",
    "core/backup/manager/app.py",
    "core/backup/manager/backup_manager.py",
    # Authentik blueprints
    "core/Authentik/blueprints/base/*.yaml",
    "core/Authentik/blueprints/google/*.yaml",
    "core/Authentik/blueprints/entra/*.yaml",
    # Module compose files (2026.07 Layout-A reorg: moved under modules/)
    "modules/chat/compose.yml",
    "modules/dify/compose.yml",
    "modules/dify/Dockerfile",
    "modules/llm/compose.yml",
    "modules/monitor/compose.yml",
    "modules/search/searxng/compose.yml",
    "modules/search/searxng/settings.yml",
    "modules/stts/compose.yml",
    "modules/doc-processing/gotenberg/compose.yml",
    "modules/gitea/compose.yml",
    "modules/gitea/init-gitea.sh",
    # Multipass
    "scripts/multipass/razzfazz-multipass.sh",
    "scripts/multipass/razzfazz-multipass.ps1",
]


def _resolve_patterns():
    """Resolve glob patterns to actual file paths, return sorted list of relative paths."""
    files = set()
    for pattern in GOVERNANCE_PATTERNS:
        full_pattern = os.path.join(STACK_ROOT, pattern)
        matches = glob.glob(full_pattern, recursive=False)
        if matches:
            for m in matches:
                rel = os.path.relpath(m, STACK_ROOT)
                if os.path.isfile(m):
                    files.add(rel)
        else:
            # Exact file reference — include even if not found (will get "MISSING" hash)
            rel = pattern
            files.add(rel)
    return sorted(files)


def _hash_file(filepath):
    """Compute SHA256 hash of a file. Returns 'MISSING' if file doesn't exist."""
    full = os.path.join(STACK_ROOT, filepath)
    if not os.path.isfile(full):
        return "MISSING", 0
    sha = hashlib.sha256()
    try:
        size = os.path.getsize(full)
        with open(full, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest(), size
    except (OSError, PermissionError):
        return "ERROR", 0


def _compute_overall(file_hashes):
    """Compute an overall SHA256 from sorted file hashes."""
    sha = hashlib.sha256()
    for filepath, filehash, _ in sorted(file_hashes, key=lambda x: x[0]):
        sha.update(f"{filepath}:{filehash}\n".encode("utf-8"))
    return sha.hexdigest()


class ChecksumManager:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        # journal_mode=MEMORY: the stack dir is bind-mounted read-only
        # (BSB-03), so sqlite cannot create its rollback journal
        # (.checksums.db-journal) alongside the DB file. Keeping the journal
        # in RAM avoids the "sqlite3.OperationalError: unable to open
        # database file" that put razzfazz-config / razzfazz-setup into a
        # gunicorn-worker-exit-3 restart-loop on first CREATE TABLE against an
        # empty/new .checksums.db (M033 S27, root-caused on 0.91 2026-05-24).
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=MEMORY")
        return conn

    def _init_db(self):
        """Create tables if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checksum_sets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    source TEXT NOT NULL,
                    overall_sha256 TEXT NOT NULL,
                    file_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checksum_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    set_id INTEGER NOT NULL,
                    filepath TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (set_id) REFERENCES checksum_sets(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_checksum_files_set_id
                ON checksum_files(set_id)
            """)
            conn.commit()

    def take_checksum(self, comment, source="manual"):
        """
        Take a new checksum snapshot of all governance files.
        
        Args:
            comment: Human-readable description of why this checksum was taken
            source: Origin of the take (init, setup-wizard, setup-cli, manual, pre-upgrade)
        
        Returns:
            dict with set info including id, timestamp, overall_sha256, file_count
        """
        files = _resolve_patterns()
        file_hashes = []
        for fp in files:
            h, size = _hash_file(fp)
            file_hashes.append((fp, h, size))

        overall = _compute_overall(file_hashes)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO checksum_sets (timestamp, comment, source, overall_sha256, file_count) VALUES (?, ?, ?, ?, ?)",
                (ts, comment, source, overall, len(file_hashes)),
            )
            set_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO checksum_files (set_id, filepath, sha256, file_size) VALUES (?, ?, ?, ?)",
                [(set_id, fp, h, size) for fp, h, size in file_hashes],
            )
            conn.commit()

        return {
            "set_id": set_id,
            "timestamp": ts,
            "comment": comment,
            "source": source,
            "overall_sha256": overall,
            "file_count": len(file_hashes),
        }

    def get_history(self, limit=50):
        """
        Get the checksum set history, most recent first.
        
        Returns:
            list of dicts with id, timestamp, comment, source, overall_sha256, file_count
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, timestamp, comment, source, overall_sha256, file_count "
                "FROM checksum_sets ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_set_detail(self, set_id):
        """
        Get a single checksum set with all file hashes.
        
        Returns:
            dict with set info + 'files' list of (filepath, sha256, file_size)
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            set_row = conn.execute(
                "SELECT id, timestamp, comment, source, overall_sha256, file_count "
                "FROM checksum_sets WHERE id = ?",
                (set_id,),
            ).fetchone()
            if not set_row:
                return None
            file_rows = conn.execute(
                "SELECT filepath, sha256, file_size FROM checksum_files WHERE set_id = ? ORDER BY filepath",
                (set_id,),
            ).fetchall()
        result = dict(set_row)
        result["set_id"] = result.pop("id")
        result["files"] = [dict(r) for r in file_rows]
        return result

    def get_diff(self, id_a, id_b):
        """
        Compare two checksum sets and return differences.
        
        Returns:
            dict with 'set_a', 'set_b' info and 'changes' list of
            {filepath, status (changed/added/removed), hash_a, hash_b, size_a, size_b}
        """
        a = self.get_set_detail(id_a)
        b = self.get_set_detail(id_b)
        if not a or not b:
            return None

        files_a = {f["filepath"]: f for f in a["files"]}
        files_b = {f["filepath"]: f for f in b["files"]}
        all_files = sorted(set(files_a.keys()) | set(files_b.keys()))

        changes = []
        for fp in all_files:
            fa = files_a.get(fp)
            fb = files_b.get(fp)
            if fa and fb:
                if fa["sha256"] != fb["sha256"]:
                    changes.append({
                        "filepath": fp,
                        "status": "changed",
                        "hash_a": fa["sha256"],
                        "hash_b": fb["sha256"],
                        "size_a": fa["file_size"],
                        "size_b": fb["file_size"],
                    })
            elif fa and not fb:
                changes.append({
                    "filepath": fp,
                    "status": "removed",
                    "hash_a": fa["sha256"],
                    "hash_b": None,
                    "size_a": fa["file_size"],
                    "size_b": 0,
                })
            elif fb and not fa:
                changes.append({
                    "filepath": fp,
                    "status": "added",
                    "hash_a": None,
                    "hash_b": fb["sha256"],
                    "size_a": 0,
                    "size_b": fb["file_size"],
                })

        return {
            "set_a": {k: v for k, v in a.items() if k != "files"},
            "set_b": {k: v for k, v in b.items() if k != "files"},
            "changes": changes,
            "total_changes": len(changes),
            "overall_changed": a["overall_sha256"] != b["overall_sha256"],
        }

    def get_current_overall(self):
        """Compute current overall checksum without saving (for live comparison)."""
        files = _resolve_patterns()
        file_hashes = []
        for fp in files:
            h, size = _hash_file(fp)
            file_hashes.append((fp, h, size))
        return _compute_overall(file_hashes)

    def get_governance_file_list(self):
        """Return the list of governance file patterns and resolved files."""
        return {
            "patterns": GOVERNANCE_PATTERNS,
            "resolved": _resolve_patterns(),
        }
