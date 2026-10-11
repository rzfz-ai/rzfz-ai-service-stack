# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""
Checksum Governance Manager for razzfazz.ai Stack

Tracks SHA256 checksums of all governance-relevant files (scripts, configs,
compose files, Dockerfiles, blueprints) in a SQLite database. Supports
historised checksum sets with timestamps and comments, detail views, and
diffs between sets.

Storage: .checksums.db in stack root (/stack/.checksums.db in container)

#1191 — a diff that shows WHAT changed, without persisting secrets
------------------------------------------------------------------
Hash-only rows can only say *that* a file changed. Two extra columns per
file row (both NULLable, added in place by ``_ensure_extended_schema``)
make a real diff possible:

* ``env_keys`` — for ``.env`` / ``.env.dify`` (untracked, secret-bearing):
  a JSON map ``{KEY: HMAC-SHA256(k, file \0 KEY \0 value)}``. The MAC key
  ``k`` is derived (domain-separated, ``_derive_env_hmac_key``) from
  **WEBUI_SECRET_KEY** — chosen because ``rzfz init`` generates it on every
  box unconditionally (cli/init.sh, "Generating WEBUI_SECRET_KEY") and this
  container already carries it as ``CONFIG_SECRET_KEY`` (core/compose.yml).
  Binding file + key name into the MAC means two keys holding the same
  value do not produce the same MAC. The diff lists keys added / removed /
  changed; values are never stored and never rendered. A 12-hex ``key id``
  is stored per set so a rotated secret (``rzfz setup
  --regenerate-secrets``) is detected and the diff degrades to hash-only
  with that reason instead of showing every key as "changed".
* ``git_blob`` — for git-tracked files: the blob id of the content at
  snapshot time (``git hash-object --stdin-paths``; computes, never
  writes). At diff time both sides are fetched from the object store
  (``git cat-file blob``) — or from the working tree when its SHA256 still
  matches the row — and rendered as a unified diff. Content in neither
  place (an uncommitted hot-patch, since overwritten) says so.

Rows written before this change, or by the CLI writer
(cli/setup_lib/checksum_manager.py, which inserts an explicit column list
and therefore keeps working against the migrated schema), carry NULLs and
show the previous hash-only view with an explicit reason.

rev-B (review of #1221) — two invariants the diff page relies on
-----------------------------------------------------------------
* **Full content is rendered only for git-tracked files.** ``ENV_KEY_FILES``
  is a hand-maintained allow-list; nothing couples it to
  ``GOVERNANCE_PATTERNS``. A later secret-bearing untracked pattern
  (``certs/*``, a module ``.env``, ``config/.env.local``) must therefore
  not fall through to the working-tree read in ``_content_for``. The belt:
  ``_content_detail`` renders content only when the file is git-tracked
  (a ``git_blob`` was recorded at snapshot time — recorded ONLY when
  ``git ls-files`` succeeded, so a blob implies "tracked" — or ``git
  ls-files`` reports it now) and never for an ``ENV_KEY_FILES`` entry;
  everything else is ``CONTENT_WITHHELD``. The guard
  ``tests/unit/consistency/test_1191_governance_files_tracked_or_keylevel.py``
  keeps the two lists consistent at CI time.
* **"No key data" has three distinct causes**, each with its own reason,
  because only one of them is fixed by taking a new snapshot: a row that
  predates key-level tracking (NULL ``env_keys``); a snapshot taken by this
  code but with no resolvable secret (``env_keys`` holds the state marker
  ``ENV_KEYS_STATE_NO_KEY``); an env file that read as ``ERROR`` at
  snapshot time (``sha256 = 'ERROR'``).
"""

import difflib
import hashlib
import hmac
import os
import glob
import sqlite3
import subprocess
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# rc6.7 #53: read STACK_ROOT from env (matches the pattern used everywhere
# else in the config UI). Pre-rc5 the volume mount was at /stack inside the
# container, but rc5+ mounts the repo at the HOST path so docker-compose.yml
# paths resolve when the daemon executes them on the host. Hardcoding /stack
# here meant the ChecksumManager opened (and silently created) an empty DB at
# /stack/.checksums.db inside the container's writable layer — masking the
# real on-host DB and showing "no snapshots" in Governance & Audit even
# though razzfazz-checksum.sh had been writing rows for weeks.
def _default_db_path():
    """Resolve the checksum DB path from STACK_ROOT at CALL time, not import
    time. Freezing it at import bound the value to whatever STACK_ROOT was on
    first import; in the unit suite that's before the harness patches STACK_ROOT
    to a tmp dir, so a stale `/stack` made ChecksumManager() call
    makedirs('/stack') and error (import-order-dependent — only surfaced under
    the full parallel run). Lazy resolution removes the fragility; prod is
    unchanged (STACK_ROOT is set to the real stack root in the container)."""
    return os.path.join(os.environ.get("STACK_ROOT", "/stack"), ".checksums.db")


STACK_ROOT = os.environ.get("STACK_ROOT", "/stack")
DB_PATH = _default_db_path()  # back-compat for callers importing the constant

# Files and patterns to checksum (relative to stack root)
# Covers: scripts, compose files, Dockerfiles, configs, blueprints, Python apps
GOVERNANCE_PATTERNS = [
    # Operator surface (#1238).
    #
    # Until 2026.09 this block listed seven root `razzfazz-*.sh` wrappers. #119
    # moved them under `legacy/` on 2026-07-05 and the list was never followed,
    # so every box recorded seven MISSING rows — noise that masks real drift.
    # Worse, and the actual reason this could not just be deleted: NOTHING
    # replaced them. `rzfz` — the single entry point they were consolidated
    # into — and all 24 `cli/*.sh` bodies were tracked by no pattern at all,
    # while R-OPS-10 classes this snapshot as *compliance*. The snapshot was
    # following seven files that do not exist and missing the 25 that actually
    # drive the stack.
    #
    # Both halves land together on purpose: the pattern list feeds the overall
    # snapshot checksum (see `_snapshot_payload`, "patterns": GOVERNANCE_PATTERNS
    # below), so ANY edit here shows a one-time drift on every fleet box. Doing
    # it in one change costs one such diff instead of two.
    #
    # RELEASE OBLIGATION: the 2026.09 release notes must announce this one-time
    # governance diff, or the first Alt<->Neu comparison on every box looks like
    # an incident. Operator decision 2026-09-04, tracked in #1238.
    "rzfz",
    "cli/*.sh",
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


# #1191 — the two governance files that carry secrets. They are untracked
# (gitignored) and are diffed at KEY level only; git never sees them here.
ENV_KEY_FILES = (".env", ".env.dify")
# rev-B: stored (JSON string) in checksum_files.env_keys for an ENV_KEY_FILES
# row when the snapshot ran with key-level code but WITHOUT a usable secret.
# NULL keeps meaning "row predates key-level tracking" (pre-#1191 / CLI
# writer); a JSON object is the {KEY: mac} map.
ENV_KEYS_STATE_NO_KEY = "no-key"
# rev-B: reason prefix for content the diff page deliberately does not
# render — an untracked file that is not one of the key-level env files, or
# an env file routed to the content path. Asserted verbatim by the tests.
CONTENT_WITHHELD = "content withheld (untracked, not key-level tracked)"
# Domain-separation label for the per-box HMAC key (see module docstring).
ENV_HMAC_LABEL = b"razzfazz-config governance env-key hmac v1"
# Render bounds for the content diff — governance files are small text
# files; anything beyond this is not something a diff page should carry.
MAX_DIFF_CONTENT_BYTES = 1024 * 1024
MAX_DIFF_LINES = 3000
GIT_TIMEOUT_SECONDS = 20


def _resolve_patterns(root=None):
    """Resolve glob patterns to actual file paths, return sorted list of relative paths."""
    root = STACK_ROOT if root is None else root
    files = set()
    for pattern in GOVERNANCE_PATTERNS:
        full_pattern = os.path.join(root, pattern)
        matches = glob.glob(full_pattern, recursive=False)
        if matches:
            for m in matches:
                rel = os.path.relpath(m, root)
                if os.path.isfile(m):
                    files.add(rel)
        else:
            # Exact file reference — include even if not found (will get "MISSING" hash)
            rel = pattern
            files.add(rel)
    return sorted(files)


def _hash_file(filepath, root=None):
    """Compute SHA256 hash of a file. Returns 'MISSING' if file doesn't exist."""
    root = STACK_ROOT if root is None else root
    full = os.path.join(root, filepath)
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


# ── #1191: key material for the .env key-level diff ──────────────────────────

def _derive_env_hmac_key(secret) -> Optional[bytes]:
    """HMAC key for the per-key value MACs, derived from the box secret.

    Domain-separated so the Flask session secret itself is never used as a
    MAC key directly. ``None`` when there is no secret — the snapshot then
    stores no key data and the diff stays hash-only (and says so)."""
    if not secret:
        return None
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, ENV_HMAC_LABEL, hashlib.sha256).digest()


def _env_key_id(hmac_key: bytes) -> str:
    """Short, non-reversible identifier of the MAC key — stored per set so a
    secret rotation between two snapshots is detected, not misread as
    'every key changed'."""
    return hashlib.sha256(b"key-id\0" + hmac_key).hexdigest()[:12]


def _env_key_hmacs(path, filepath_label: str, hmac_key: Optional[bytes]) -> Dict[str, str]:
    """``{KEY: hex MAC}`` for one env file; ``{}`` when it is absent or
    unreadable. Values are parsed with the stack's canonical parser (same
    view the services get) and bound to file + key name inside the MAC."""
    if not hmac_key or not os.path.isfile(path):
        return {}
    try:
        from .env_utils import read_env_file
        values = read_env_file(path, expand=False)
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for key, value in values.items():
        msg = (filepath_label.encode("utf-8") + b"\0" + key.encode("utf-8") + b"\0"
               + (value or "").encode("utf-8", "surrogateescape"))
        out[key] = hmac.new(hmac_key, msg, hashlib.sha256).hexdigest()
    return out


def default_secret_provider(stack_root):
    """The per-box secret the MAC key is derived from (see module docstring).

    Order: WEBUI_SECRET_KEY from the .env FILE (always current, even right
    after a rotation), then the container's CONFIG_SECRET_KEY (the same
    value as injected at container create time), then AUTHENTIK_SECRET_KEY
    as a last resort. ``None`` → no key-level data for this snapshot."""
    env_path = os.path.join(stack_root, ".env")

    def _read(key):
        try:
            from .env_utils import read_env_key
            return read_env_key(env_path, key, "") or None
        except Exception:
            return None

    def provider():
        return (_read("WEBUI_SECRET_KEY")
                or os.environ.get("CONFIG_SECRET_KEY")
                or _read("AUTHENTIK_SECRET_KEY")
                or None)
    return provider


# ── #1191: git plumbing ───────────────────────────────────────────────────────

def _default_git_runner(args, cwd, stdin=None) -> Tuple[int, bytes]:
    """Run one git plumbing command in `cwd`; ``(rc, stdout_bytes)``.

    ``-c safe.directory=*`` — the container runs as root while the checkout
    is owned by the installing user (same reasoning as image_checker). Only
    read-only plumbing is ever issued here: ls-files, rev-parse,
    hash-object (without -w), cat-file."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd, input=stdin, capture_output=True, env=env,
        timeout=GIT_TIMEOUT_SECONDS, check=False,
    )
    return result.returncode, result.stdout


def _classify_diff_line(line: str) -> str:
    if line.startswith("+++") or line.startswith("---"):
        return "meta"
    if line.startswith("@@"):
        return "hunk"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    return "ctx"


def _compute_overall(file_hashes):
    """Compute an overall SHA256 from sorted file hashes."""
    sha = hashlib.sha256()
    for filepath, filehash, _ in sorted(file_hashes, key=lambda x: x[0]):
        sha.update(f"{filepath}:{filehash}\n".encode("utf-8"))
    return sha.hexdigest()


class ChecksumManager:
    def __init__(self, db_path=None, *, stack_root=None, secret_provider=None,
                 git_runner=None):
        # Resolve lazily (see _default_db_path) so create_app() picks up the
        # CURRENT STACK_ROOT rather than the import-time value.
        self.db_path = db_path if db_path is not None else _default_db_path()
        # #1191: the root the snapshot reads from. When not given, the
        # module-level STACK_ROOT is read at CALL time (see the property) —
        # callers that reload the module or patch that global must keep
        # steering this instance, exactly as they steered _hash_file before.
        self._stack_root = stack_root
        self._secret_provider = secret_provider
        self.git_runner = git_runner if git_runner is not None else _default_git_runner
        # Set by _init_db: False when the #1191 columns could not be added
        # (e.g. sqlite opened read-only) — the manager then behaves exactly
        # like the pre-#1191 one.
        self.extended = False
        self._init_db()

    @property
    def stack_root(self):
        return self._stack_root if self._stack_root is not None else STACK_ROOT

    @property
    def secret_provider(self):
        if self._secret_provider is not None:
            return self._secret_provider
        return default_secret_provider(self.stack_root)

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
        self.extended = self._ensure_extended_schema()

    # #1191 — (table, column) pairs added in place. All NULLable TEXT, so
    # rows from before the change and from the CLI writer stay valid.
    _EXTENDED_COLUMNS = (
        ("checksum_sets", "git_head"),
        ("checksum_sets", "env_key_id"),
        ("checksum_files", "git_blob"),
        ("checksum_files", "env_keys"),
    )

    def _ensure_extended_schema(self):
        """Add the #1191 columns if missing. Returns True when they are all
        present afterwards, False when the DB could not be altered — the
        caller then stays on the base feature set rather than failing."""
        try:
            with self._connect() as conn:
                for table, col in self._EXTENDED_COLUMNS:
                    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if col not in cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                conn.commit()
            return True
        except sqlite3.Error:
            return False

    # ── #1191 helpers ──────────────────────────────────────────────────────

    def _git(self, args, stdin=None):
        """``(rc, stdout)`` or ``(None, b'')`` — git failing is never fatal."""
        try:
            rc, out = self.git_runner(list(args), self.stack_root, stdin)
            return rc, (out or b"")
        except Exception:
            return None, b""

    def _tracked_now(self):
        """Paths git tracks in the checkout right now, or ``None`` when git
        could not answer (not a repository, binary missing, timeout)."""
        rc, out = self._git(["ls-files", "-z"])
        if rc != 0:
            return None
        return {p for p in out.decode("utf-8", "replace").split("\0") if p}

    def _collect_extended(self, file_hashes):
        """The #1191 columns for one snapshot.

        Returns ``(git_head, blobs, env_key_id, env_keys)`` where ``blobs``
        maps filepath → blob id (tracked, present files only — never the
        ENV_KEY_FILES; and only when ``git ls-files`` answered, so a stored
        blob always implies "git-tracked at snapshot time" — the diff-time
        belt relies on that) and ``env_keys`` maps filepath → ``{KEY: mac}``
        for the env files that exist (``{}`` for an env file with no keys),
        or → ``ENV_KEYS_STATE_NO_KEY`` when there was no secret to key with."""
        # Key-level data for the env files.
        try:
            secret = self.secret_provider()
        except Exception:
            secret = None
        hmac_key = _derive_env_hmac_key(secret)
        env_key_id = _env_key_id(hmac_key) if hmac_key else None
        env_keys = {}
        for fp, h, _size in file_hashes:
            if fp in ENV_KEY_FILES and h not in ("MISSING", "ERROR"):
                if hmac_key:
                    env_keys[fp] = _env_key_hmacs(
                        os.path.join(self.stack_root, fp), fp, hmac_key)
                else:
                    env_keys[fp] = ENV_KEYS_STATE_NO_KEY

        # Git facts for everything else.
        git_head = None
        rc, out = self._git(["rev-parse", "HEAD"])
        if rc == 0 and out.strip():
            git_head = out.decode("utf-8", "replace").strip()
        # No ls-files answer → no blobs at all: an untracked file must never
        # end up with a blob id, or the diff-time belt would trust it.
        tracked = self._tracked_now() or set()
        candidates = [
            fp for fp, h, _size in file_hashes
            if fp not in ENV_KEY_FILES and h not in ("MISSING", "ERROR")
            and fp in tracked
        ]
        blobs = {}
        if candidates:
            rc, out = self._git(["hash-object", "--stdin-paths"],
                                stdin=("\n".join(candidates) + "\n").encode("utf-8"))
            ids = out.decode("utf-8", "replace").split() if rc == 0 else []
            if len(ids) == len(candidates):
                blobs = dict(zip(candidates, ids))
        return git_head, blobs, env_key_id, env_keys

    def take_checksum(self, comment, source="manual"):
        """
        Take a new checksum snapshot of all governance files.
        
        Args:
            comment: Human-readable description of why this checksum was taken
            source: Origin of the take (init, setup-wizard, setup-cli, manual, pre-upgrade)
        
        Returns:
            dict with set info including id, timestamp, overall_sha256, file_count
        """
        files = _resolve_patterns(self.stack_root)
        file_hashes = []
        for fp in files:
            h, size = _hash_file(fp, self.stack_root)
            file_hashes.append((fp, h, size))

        overall = _compute_overall(file_hashes)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        git_head = env_key_id = None
        blobs, env_keys = {}, {}
        if self.extended:
            try:
                git_head, blobs, env_key_id, env_keys = self._collect_extended(file_hashes)
            except Exception:
                # The hash snapshot is the tamper-evidence contract; the
                # #1191 extras must never prevent it from being taken.
                git_head = env_key_id = None
                blobs, env_keys = {}, {}

        with self._connect() as conn:
            if self.extended:
                cursor = conn.execute(
                    "INSERT INTO checksum_sets (timestamp, comment, source, overall_sha256, "
                    "file_count, git_head, env_key_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ts, comment, source, overall, len(file_hashes), git_head, env_key_id),
                )
                set_id = cursor.lastrowid
                conn.executemany(
                    "INSERT INTO checksum_files (set_id, filepath, sha256, file_size, "
                    "git_blob, env_keys) VALUES (?, ?, ?, ?, ?, ?)",
                    [(set_id, fp, h, size, blobs.get(fp),
                      json.dumps(env_keys[fp], sort_keys=True) if fp in env_keys else None)
                     for fp, h, size in file_hashes],
                )
            else:
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
            "git_head": git_head,
            "env_key_id": env_key_id,
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
        ext_sets = ", git_head, env_key_id" if self.extended else ""
        ext_files = ", git_blob, env_keys" if self.extended else ""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            set_row = conn.execute(
                "SELECT id, timestamp, comment, source, overall_sha256, file_count"
                f"{ext_sets} FROM checksum_sets WHERE id = ?",
                (set_id,),
            ).fetchone()
            if not set_row:
                return None
            file_rows = conn.execute(
                f"SELECT filepath, sha256, file_size{ext_files} FROM checksum_files "
                "WHERE set_id = ? ORDER BY filepath",
                (set_id,),
            ).fetchall()
        result = dict(set_row)
        result["set_id"] = result.pop("id")
        result.setdefault("git_head", None)
        result.setdefault("env_key_id", None)
        files = []
        for r in file_rows:
            f = dict(r)
            f.setdefault("git_blob", None)
            raw = f.get("env_keys")
            # env_keys: dict {KEY: mac} or None; env_key_state: "keys" when a
            # map is present, ENV_KEYS_STATE_NO_KEY when the snapshot had no
            # secret, None when the row predates key-level tracking.
            f["env_keys"] = None
            f["env_key_state"] = None
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    f["env_keys"] = parsed
                    f["env_key_state"] = "keys"
                elif isinstance(parsed, str) and parsed:
                    f["env_key_state"] = parsed
            files.append(f)
        result["files"] = files
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

        # #1191: what actually changed, per file. Never lets a detail
        # failure take the hash view down with it. The tracked set is
        # resolved once per diff (rev-B belt input, see _content_detail).
        tracked_now = self._tracked_now() if changes else None
        for c in changes:
            try:
                c["detail"] = self._change_detail(
                    c["filepath"], files_a.get(c["filepath"]), files_b.get(c["filepath"]),
                    a, b, tracked_now)
            except Exception as exc:
                c["detail"] = {"kind": "unavailable",
                               "reason": f"detail could not be computed ({exc!r}) — hash only"}

        return {
            "set_a": {k: v for k, v in a.items() if k != "files"},
            "set_b": {k: v for k, v in b.items() if k != "files"},
            "changes": changes,
            "total_changes": len(changes),
            "overall_changed": a["overall_sha256"] != b["overall_sha256"],
        }

    # ── #1191: per-file diff detail ────────────────────────────────────────

    def _change_detail(self, filepath, fa, fb, set_a, set_b, tracked_now=None):
        if filepath in ENV_KEY_FILES:
            return self._env_detail(filepath, fa, fb, set_a, set_b)
        return self._content_detail(filepath, fa, fb, set_a, set_b, tracked_now)

    @staticmethod
    def _env_side(f):
        """``(keys, present)`` for one side: ``({}, False)`` when the file was
        absent, ``(None, True)`` when the row carries no key data."""
        if f is None or f.get("sha256") == "MISSING":
            return {}, False
        keys = f.get("env_keys")
        if keys is None:
            return None, True
        return keys, True

    def _env_detail(self, filepath, fa, fb, set_a, set_b):
        ida, idb = set_a["set_id"], set_b["set_id"]
        sides = ((ida, fa), (idb, fb))
        # rev-B: three causes for "no key data", three reasons — only the
        # last one is fixed by taking a new snapshot.
        # (1) the env file could not be read when the snapshot was taken.
        unreadable = [f"#{sid}" for sid, f in sides
                      if f is not None and f.get("sha256") == "ERROR"]
        if unreadable:
            return {
                "kind": "unavailable",
                "reason": (f"{filepath} could not be read when snapshot "
                           f"{' and '.join(unreadable)} was taken: env file unreadable "
                           f"at snapshot time — see snapshot detail (the file row shows "
                           f"ERROR instead of a hash); hash only"),
            }
        # (2) key-level code ran but had no secret to key the MACs with.
        no_key = [f"#{sid}" for sid, f in sides
                  if f is not None and f.get("sha256") != "MISSING"
                  and f.get("env_key_state") == ENV_KEYS_STATE_NO_KEY]
        if no_key:
            return {
                "kind": "unavailable",
                "reason": (f"snapshot {' and '.join(no_key)} holds no key data for "
                           f"{filepath}: env key unavailable — key-level diff disabled "
                           f"on this box (no WEBUI_SECRET_KEY / CONFIG_SECRET_KEY could "
                           f"be resolved when it was taken; another snapshot does not "
                           f"help until the secret is available); hash only"),
            }
        ka, present_a = self._env_side(fa)
        kb, present_b = self._env_side(fb)
        # (3) the row predates key-level tracking (pre-#1191 or CLI writer).
        old = [f"#{sid}" for sid, k in ((ida, ka), (idb, kb)) if k is None]
        if old:
            return {
                "kind": "unavailable",
                "reason": (f"snapshot {' and '.join(old)} predates key-level "
                           f"tracking of {filepath} (hash only) — take a new snapshot; "
                           f"diffs between snapshots taken from now on list the keys"),
            }
        if present_a and present_b and set_a.get("env_key_id") != set_b.get("env_key_id"):
            return {
                "kind": "unavailable",
                "reason": (f"snapshots #{ida} and #{idb} were keyed with a different "
                           f"secret (WEBUI_SECRET_KEY rotated between them) — key-level "
                           f"comparison is not possible, hash only"),
            }
        common = set(ka) & set(kb)
        changed = sorted(k for k in common if ka[k] != kb[k])
        return {
            "kind": "env-keys",
            "added": sorted(set(kb) - set(ka)),
            "removed": sorted(set(ka) - set(kb)),
            "changed": changed,
            "unchanged": len(common) - len(changed),
        }

    def _content_for(self, filepath, f, tracked=True):
        """``(bytes, source)`` for one side; source ∈ git | working-tree |
        missing | unavailable. The working-tree read is the only path that
        can surface content git has never seen — it is taken only for a
        ``tracked`` file (rev-B belt; ``_content_detail`` decides)."""
        if f is None or f.get("sha256") == "MISSING":
            return b"", "missing"
        if f.get("sha256") == "ERROR":
            return None, "unavailable"
        blob = f.get("git_blob")
        if blob:
            rc, out = self._git(["cat-file", "blob", blob])
            if rc == 0:
                return out, "git"
        full = os.path.join(self.stack_root, filepath)
        if tracked and os.path.isfile(full):
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                data = None
            if data is not None and hashlib.sha256(data).hexdigest() == f.get("sha256"):
                return data, "working-tree"
        return None, "unavailable"

    @staticmethod
    def _tracked_at_snapshot(fa, fb):
        """A recorded blob id means git tracked the file when that snapshot
        was taken (blobs are computed for ls-files-listed paths only)."""
        return any(f is not None and f.get("git_blob") for f in (fa, fb))

    def _content_detail(self, filepath, fa, fb, set_a, set_b, tracked_now=None):
        ida, idb = set_a["set_id"], set_b["set_id"]
        # rev-B belt: full content only for git-tracked files, never for the
        # key-level env files — whatever GOVERNANCE_PATTERNS grows to match.
        # rev-C (rzfz re-review, LOW): decide PER SIDE, not per file. A blob
        # recorded at snapshot A proves git tracked the file THEN; it says
        # nothing about snapshot B. Tracked-at-A + untracked-at-B must not let
        # side B render from the working tree (measured: a probe secret written
        # into a no-longer-tracked governance file rendered as '+' line).
        now = tracked_now is not None and filepath in tracked_now
        tracked_a = bool(fa is not None and fa.get("git_blob")) or now
        tracked_b = bool(fb is not None and fb.get("git_blob")) or now
        tracked = tracked_a or tracked_b
        if filepath in ENV_KEY_FILES or not tracked:
            why = ("it is a secret-bearing env file and is compared at key level only"
                   if filepath in ENV_KEY_FILES else
                   "it is not git-tracked and not one of the key-level env files, so "
                   "its working-tree content is never rendered here")
            return {
                "kind": "unavailable",
                "reason": f"{CONTENT_WITHHELD} — {filepath}: {why}; hash only",
            }
        content_a, src_a = self._content_for(filepath, fa, tracked_a)
        content_b, src_b = self._content_for(filepath, fb, tracked_b)
        lost = [f"#{sid}" for sid, src in ((ida, src_a), (idb, src_b)) if src == "unavailable"]
        if lost:
            return {
                "kind": "unavailable",
                "reason": (f"content of {filepath} at snapshot {' and '.join(lost)} is "
                           f"not in the git object store (uncommitted change at snapshot "
                           f"time) and no longer on disk — hash only"),
            }
        biggest = max(len(content_a), len(content_b))
        if biggest > MAX_DIFF_CONTENT_BYTES:
            return {
                "kind": "unavailable",
                "reason": (f"{filepath} is too large to render "
                           f"({biggest // 1024} KB > {MAX_DIFF_CONTENT_BYTES // 1024} KB) — hash only"),
            }
        try:
            text_a = content_a.decode("utf-8")
            text_b = content_b.decode("utf-8")
        except UnicodeDecodeError:
            return {"kind": "unavailable",
                    "reason": f"{filepath} holds binary or non-UTF-8 content — not rendered, hash only"}
        raw = difflib.unified_diff(
            text_a.splitlines(), text_b.splitlines(),
            fromfile=f"a/{filepath} (#{ida}{', missing' if src_a == 'missing' else ''})",
            tofile=f"b/{filepath} (#{idb}{', missing' if src_b == 'missing' else ''})",
            lineterm="",
        )
        lines = []
        truncated = False
        for line in raw:
            if len(lines) >= MAX_DIFF_LINES:
                truncated = True
                lines.append({"cls": "meta",
                              "text": f"… diff truncated after {MAX_DIFF_LINES} lines"})
                break
            lines.append({"cls": _classify_diff_line(line), "text": line})
        return {
            "kind": "content",
            "lines": lines,
            "truncated": truncated,
            "source_a": src_a,
            "source_b": src_b,
        }

    def get_current_overall(self):
        """Compute current overall checksum without saving (for live comparison)."""
        files = _resolve_patterns(self.stack_root)
        file_hashes = []
        for fp in files:
            h, size = _hash_file(fp, self.stack_root)
            file_hashes.append((fp, h, size))
        return _compute_overall(file_hashes)

    def get_governance_file_list(self):
        """Return the list of governance file patterns and resolved files."""
        return {
            "patterns": GOVERNANCE_PATTERNS,
            "resolved": _resolve_patterns(self.stack_root),
        }
