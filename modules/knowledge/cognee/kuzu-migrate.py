#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#186 — proactive ladybug on-disk graph-format migration + empty-graph guards.

Runs BEFORE the cognee server starts (see entrypoint-wrapper.sh). Turns the two
SILENT "empty knowledge-graph" failures that appear after a cognee ladybug/Kuzu
on-disk format upgrade (originally 0.16 -> 0.17; since the 2026.08 1.4.0 ->
1.5.3 bump, also 0.17 -> 0.19) into a safe, reversible migration (Defect 1) and
a loud, actionable warning (Defect 2). The detection/migration logic below is
generic (storage-format major.minor comparison + cognee's own official
ladybug_migration() API) and needed NO code change for the 0.17->0.19 crossing.

Root cause of the silence (cognee 1.2.2 / 1.4.0; same mechanism at 1.5.3):

Defect 1 — the GLOBAL graph store stays in the OLD on-disk format. cognee's
built-in auto-migration in the ladybug adapter NEVER fires for a 0.16 -> 0.17
crossing:
  * it is gated behind `except RuntimeError` when OPENING the DB fails, but a
    0.17 engine opens a 0.16-format store WITHOUT error (it just reads 0 nodes),
    so the handler is never entered; and
  * even if it were, upstream `needs_migration()` only returns True for on-disk
    versions < 0.15.0, so 0.16.0 is reported as "no migration needed".
The graph therefore silently reads empty (UI: "No graph data available")
despite the nodes/edges still being present on disk.

Defect 2 — with ENABLE_BACKEND_ACCESS_CONTROL=true (default) cognee scopes
graphs per user+dataset and the viz reads the per-dataset pickle store
(.cognee_system/databases/<user_id>/<dataset_id>.pkl), NOT the global Kuzu store.
On a box whose data pre-dates access control (or was migrated by Defect 1, which
fixes only the GLOBAL store) the per-dataset store is empty and cognee ships NO
API to rebuild it from the global graph — so the viz stays empty even after
Defect 1 is fixed. We do NOT auto-rebuild the per-dataset store (there is no safe
cognee API for it and it would touch on-disk graph data); instead we DETECT the
mismatch and LOG a clear, actionable warning + the documented recovery, so the
operator is no longer left staring at a silently-empty graph.

Safety properties of the Defect 1 migration (deliberately conservative — the
store holds the only copy of the user's knowledge graph):
  * STORAGE-FORMAT gated. Migration triggers ONLY when the on-disk storage FORMAT
    (major.minor) is OLDER than the engine's. A patch-level difference
    (0.17.0 vs 0.17.1) is storage-compatible -> NO migration (avoids pointless
    export/import churn — and its PyPI dependency — on every patch bump). A NEWER
    on-disk format (a downgrade) is NEVER auto-migrated: we warn and leave it.
  * BACKUP-OR-ABORT. Before migrating we take our OWN full copy of the store
    (`<store>_pre186bak_<ver>`). If that copy cannot be made (e.g. disk full) we
    ABORT the migration rather than risk an in-place transform without a
    recoverable copy. cognee's own migration ALSO keeps the original as
    `<store>_old_<ver>`, so a successful migration leaves TWO recoverable copies.
  * OFFICIAL API only. We call cognee's own `ladybug_migration()` (EXPORT under
    the old engine, IMPORT under the new engine) — never a hand-rolled rewrite.
  * OPT-OUT. `COGNEE_KUZU_AUTO_MIGRATE=false` disables the automatic migration:
    we still DETECT + warn loudly with the manual recovery command, but do not
    run it. Intended for air-gapped boxes and operators who prefer to migrate by
    hand under a maintenance window.
  * IDEMPOTENT + NON-FATAL. No-op when the store is absent, unreadable, current,
    or newer. Any failure is logged and swallowed (exit 0) so cognee still starts
    — the box is then no worse than before this fix.

OFFLINE CAVEAT (flagged in #186): cognee's `ladybug_migration()` creates
throwaway venvs and `pip install`s the old and new ladybug engines, so the
AUTOMATIC path needs PyPI reachability during the one-time migration. On an
air-gapped box it fails, logs a clear warning, and no-ops (the store is left
UNCHANGED and recoverable) — run the manual procedure there, or set
`COGNEE_KUZU_AUTO_MIGRATE=false` to skip the attempt entirely. The ladybug JSON
extension IS vendored offline for both engine versions (see Dockerfile) so the
export/import legs never need to download it.
"""
import os
import shutil
import sys

# The cognee-data volume is mounted at /app/cognee/.cognee_system (compose.yml),
# and with GRAPH_DATABASE_PROVIDER=kuzu the global graph store dir is
# <system_root>/databases/cognee_graph_kuzu.
DEFAULT_STORE = "/app/cognee/.cognee_system/databases/cognee_graph_kuzu"

# Defect 2 heuristic: only warn about the access-control per-dataset gap when the
# GLOBAL store holds a plausibly-real graph (avoids crying wolf on a brand-new
# box that simply hasn't cognified anything yet). A fresh, empty Kuzu store is a
# few tens of KiB of schema files; a store with real nodes/edges is well above.
_GLOBAL_STORE_MIN_BYTES = 256 * 1024


def _log(msg):
    print(f"[cognee-kuzu-migrate] {msg}", file=sys.stderr, flush=True)


def _env_flag(name, default):
    """Read a boolean-ish env var. Unset/blank -> default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_store_path():
    """Prefer cognee's own config resolution; fall back to the container default."""
    try:
        from cognee.infrastructure.databases.graph.config import get_graph_config

        path = get_graph_config().graph_file_path
        if path:
            return path
    except Exception as exc:  # noqa: BLE001 — config import is best-effort
        _log(f"could not resolve graph path via cognee config ({exc}); using default")
    return DEFAULT_STORE


def _parse_mm(version):
    """Parse the leading major.minor of a version string ('0.16.0' -> (0, 16)).

    Returns None for anything that does not start with two dotted integers, so an
    unrecognised/garbled version is treated as 'unknown' (never migrated)."""
    if not version:
        return None
    parts = str(version).strip().split(".")
    if len(parts) < 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except (TypeError, ValueError):
        return None


def _migration_decision(on_disk, engine):
    """Decide what to do given the on-disk storage version and the engine version.

    Returns one of: 'current' | 'migrate' | 'downgrade' | 'unknown'. The decision
    is made on the storage FORMAT (major.minor) so a patch-level difference is a
    no-op — the on-disk format only changes on a minor bump (0.16 -> 0.17)."""
    on_mm = _parse_mm(on_disk)
    eng_mm = _parse_mm(engine)
    if on_mm is None or eng_mm is None:
        return "unknown"
    if on_mm == eng_mm:
        return "current"
    if on_mm < eng_mm:
        return "migrate"
    return "downgrade"


def _import_migrate_api():
    """Import (read_ladybug_storage_version, ladybug_migration) from either
    the pure-stdlib worker package or the cognee re-export shim."""
    try:
        from cognee_db_workers.ladybug_migrate import (
            ladybug_migration,
            read_ladybug_storage_version,
        )

        return read_ladybug_storage_version, ladybug_migration
    except Exception:  # noqa: BLE001 — try the re-export path next
        from cognee.infrastructure.databases.graph.ladybug.ladybug_migrate import (
            ladybug_migration,
            read_ladybug_storage_version,
        )

        return read_ladybug_storage_version, ladybug_migration


def _backup_store(store, on_disk):
    """Take our OWN full copy of the store BEFORE migrating, so the migration is
    reversible even if cognee's internal `<store>_old_` rename never happens
    (e.g. the export leg dies mid-way). Returns the backup path.

    Raises on failure — the caller MUST treat a failed backup as a hard stop and
    NOT migrate: we never transform the only copy of the graph in place without a
    recoverable copy on disk."""
    safe_ver = str(on_disk).replace(os.sep, "_").replace("/", "_")
    backup = f"{store}_pre186bak_{safe_ver}"
    if os.path.exists(backup):
        # A prior interrupted attempt already made this backup. The store has NOT
        # been transformed yet on a failed attempt (migration leaves it intact),
        # so the existing copy is a valid backup of the same on-disk version —
        # reuse it rather than risk clobbering a good copy.
        _log(f"reusing existing pre-migration backup {backup}")
        return backup
    _log(f"backing up graph store -> {backup} before migrating")
    shutil.copytree(store, backup)
    return backup


def _run_format_migration(store, read_version, migrate, engine_version, on_disk):
    """Back up, then run cognee's official export/import migration and verify."""
    try:
        _backup_store(store, on_disk)
    except Exception as exc:  # noqa: BLE001
        _log(
            f"could NOT back up the graph store before migrating ({exc}); "
            "ABORTING migration to avoid an unrecoverable in-place transform. "
            "The store is UNCHANGED (still "
            f"{on_disk}); free disk space and restart, or migrate manually (#186)."
        )
        return

    mig = store + "_mig"
    # Clean any stale target from a previous interrupted attempt — upstream
    # ladybug_migration raises FileExistsError if new_db already exists.
    if os.path.exists(mig):
        _log(f"removing stale migration target {mig}")
        shutil.rmtree(mig, ignore_errors=True)
    if os.path.exists(mig + ".wal"):
        try:
            os.remove(mig + ".wal")
        except OSError:
            pass

    try:
        migrate(
            new_db=mig,
            old_db=store,
            new_version=engine_version,
            old_version=on_disk,
            overwrite=True,  # backs up old store as <name>_old_<ver>, swaps new in
        )
    except Exception as exc:  # noqa: BLE001
        _log(
            f"migration FAILED ({exc}). The graph store is UNCHANGED (still {on_disk}) "
            "and a pre-migration backup was kept; cognee will start but the "
            "knowledge-graph viz stays empty until migrated. On an air-gapped box the "
            "automatic migration cannot pip-install the ladybug engines — run the "
            "manual migration with PyPI access, or set COGNEE_KUZU_AUTO_MIGRATE=false "
            "and migrate under a maintenance window (see issue #186)."
        )
        return

    # Verify the swapped-in store now reads as the engine version.
    try:
        now = read_version(store)
        if _migration_decision(now, engine_version) == "current":
            _log(f"migration OK — store now {now}; original kept as *_old_ + *_pre186bak_ backups.")
        else:
            _log(f"migration ran but store reports {now} (expected {engine_version}); review #186.")
    except Exception as exc:  # noqa: BLE001
        _log(f"migration ran but post-check failed ({exc}); review #186.")


def _dir_total_size(path):
    """Best-effort recursive byte size of a directory (0 on any error)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def warn_access_control_graph_gap(store):
    """Defect 2 — turn the SILENT per-dataset empty graph into a loud warning.

    With access control ON the viz reads per-user/per-dataset pickle stores under
    <databases>/<user_id>/<dataset>.pkl, not the global Kuzu store. If the global
    store holds a real graph but NO per-dataset pickle stores exist, the viz will
    read empty. We only DETECT + warn (there is no safe cognee API to rebuild the
    per-dataset store from the global graph); the recovery is documented in the
    help docs and echoed here. Never fatal."""
    try:
        if not _env_flag("ENABLE_BACKEND_ACCESS_CONTROL", True):
            return  # access control off -> viz reads the global store; nothing to warn about
        databases_root = os.path.dirname(store)
        if not os.path.isdir(store):
            return  # no global graph yet -> nothing to be confused about
        if _dir_total_size(store) < _GLOBAL_STORE_MIN_BYTES:
            return  # global store looks empty/fresh -> don't cry wolf on a new box
        # Any per-dataset pickle graph present? (<databases>/<user_id>/<dataset>.pkl)
        has_per_dataset = False
        try:
            for entry in os.scandir(databases_root):
                if not entry.is_dir():
                    continue
                if os.path.abspath(entry.path) == os.path.abspath(store):
                    continue  # the global kuzu store dir itself
                for sub in os.scandir(entry.path):
                    if sub.is_file() and sub.name.endswith(".pkl"):
                        has_per_dataset = True
                        break
                if has_per_dataset:
                    break
        except OSError:
            return
        if has_per_dataset:
            return  # per-dataset stores exist -> viz has something to read
        _log(
            "WARNING (#186 Defect 2): access control is ON and the GLOBAL knowledge "
            "graph holds data, but NO per-user/per-dataset graph store exists — the "
            "knowledge-graph VISUALIZATION will read EMPTY. cognee has no API to "
            "rebuild the per-dataset store from the global graph. Recovery: either "
            "set COGNEE_ACCESS_CONTROL=false (single-tenant box — the viz then reads "
            "the global graph), or re-ingest under access control by adding the docs "
            "to a fresh dataset and re-running cognify (a re-cognify of an already-"
            "processed dataset idempotent-skips). See help -> Cognee troubleshooting."
        )
    except Exception as exc:  # noqa: BLE001 — detection must never block startup
        _log(f"access-control gap check skipped ({exc}).")


def main():
    store = _resolve_store_path()
    if not os.path.exists(store):
        _log(f"no graph store at {store}; nothing to migrate.")
        # Even with no store there is nothing to warn about; return early.
        return 0

    try:
        read_version, migrate = _import_migrate_api()
    except Exception as exc:  # noqa: BLE001
        _log(f"ladybug migrate API unavailable ({exc}); skipping format check.")
        warn_access_control_graph_gap(store)
        return 0

    try:
        import ladybug

        engine_version = ladybug.__version__
    except Exception as exc:  # noqa: BLE001
        _log(f"ladybug not importable ({exc}); skipping format check.")
        warn_access_control_graph_gap(store)
        return 0

    try:
        on_disk = read_version(store)
    except Exception as exc:  # noqa: BLE001
        # Fresh / unknown / unreadable store — leave it alone.
        _log(f"could not read on-disk storage version ({exc}); skipping format check.")
        warn_access_control_graph_gap(store)
        return 0

    decision = _migration_decision(on_disk, engine_version)
    if decision == "current":
        _log(f"store storage-format matches engine ({on_disk} ~ {engine_version}); no migration needed.")
    elif decision == "unknown":
        _log(
            f"could not compare storage format (on-disk={on_disk!r}, engine={engine_version!r}); "
            "leaving the store untouched."
        )
    elif decision == "downgrade":
        _log(
            f"WARNING: on-disk graph format {on_disk} is NEWER than the engine {engine_version} "
            "(a downgrade). NOT auto-migrating — downgrading graph data risks loss. Restore the "
            "matching cognee image or migrate manually (#186)."
        )
    else:  # "migrate"
        if not _env_flag("COGNEE_KUZU_AUTO_MIGRATE", True):
            _log(
                f"on-disk graph format {on_disk} is OLDER than engine {engine_version} and needs "
                "migration, but COGNEE_KUZU_AUTO_MIGRATE=false — SKIPPING the automatic migration. "
                "The knowledge-graph viz will read EMPTY until migrated. Run manually with PyPI "
                "access: ladybug_migrate --old-version "
                f"{on_disk} --new-version {engine_version} --old-db {store} --new-db {store}_mig "
                "--overwrite (see issue #186)."
            )
        else:
            _log(f"on-disk graph format {on_disk} is OLDER than engine {engine_version}; migrating…")
            _run_format_migration(store, read_version, migrate, engine_version, on_disk)

    # Independently of the format migration, surface the Defect 2 access-control
    # per-dataset gap (relevant even when the global store is already current).
    warn_access_control_graph_gap(store)
    return 0


if __name__ == "__main__":
    # Never fail container start on account of the migration.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        _log(f"unexpected error ({exc}); skipping migration.")
        sys.exit(0)
