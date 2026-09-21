#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#278 ask 1 — refuse to start GPUStack against a database of another major.

The incident on a Care Solutions box: `docker compose up -d gpustack` naming
the SERVICE `gpustack` started the **llm / 2.1.x** service on an **llm-legacy /
0.7.1** box, because Compose v2 starts an explicitly named service regardless
of its profile. 2.1.x pointed at the existing 0.7.1 `gpustack_db`, alembic
migrated it, and the hard-hold was violated on production data. 0.7.1 then
crash-looped on the migrated schema.

The profile assertion in the compose entrypoints (ask 2) already refuses that
exact invocation. This guard is the layer *below* it: it holds no matter how
the container was launched — `docker run`, an unset `COMPOSE_PROFILES`, a
hand-written unit — because it asks the only question that actually decides
whether damage happens: **does this database belong to my major?**

How it knows, without a hardcoded version table
-----------------------------------------------
Every GPUStack image ships its own alembic migrations, so the image already
carries the answer:

* the revisions THIS image knows  = its ``migrations/versions/*.py``
* the revision the DATABASE is at = ``alembic_version.version_num``

Measured on the pinned images (0.78, 2026-08-26): 0.7.1 ships 10 revisions,
2.1.2 ships 25, and the 0.7.1 set is a strict SUBSET of the 2.x set. So set
membership alone cannot tell a legitimate upgrade from the accident — the
0.7.1 head is a perfectly ordinary ancestor from 2.x's point of view. What
separates them is the **major boundary migration**, which upstream names for
what it is: ``v2_0_database_migration``. Two positive, unambiguous cases:

1. The DB is at a revision this image has never heard of → it was migrated by
   a NEWER GPUStack. Starting is the crash-loop. **Refuse.**
2. The DB is at a revision that lies BEFORE a major boundary this image would
   cross → starting runs that boundary migration on another major's database.
   **Refuse**, unless the operator explicitly opted in.

Everything else — including an ordinary within-major upgrade — passes.

Fail-open, deliberately
-----------------------
This runs before every GPUStack start on every box in the fleet. A guard that
is wrong about a box it cannot read would be a worse outage than the one it
prevents, so it refuses ONLY on a positive, unambiguous mismatch. No database
URL, no ``alembic_version`` table, an unreadable migrations directory, a
driver import that fails, a connection that times out — all of those log a
line and exit 0. The cost of failing open here is that the accident stays as
likely as it is today; the cost of failing closed on a false reading is a
fleet that will not start.
"""
from __future__ import annotations

import os
import re
import sys

#: Upstream names its major-crossing migration for what it does, e.g.
#: `2025_09_08_1454-924c9a0b4c13_v2_0_database_migration.py`. Deriving the
#: boundary from the shipped files (rather than pinning 924c9a0b4c13) is what
#: keeps this correct across the next major without an edit.
_BOUNDARY_RE = re.compile(r"_v(\d+)_0_database_migration", re.I)
_REVISION_RE = re.compile(r"^revision(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", re.M)
_DOWN_RE = re.compile(
    r"^down_revision(?::\s*[^=]+)?\s*=\s*(?:['\"]([^'\"]+)['\"]|None)", re.M)

OPT_IN = "RAZZFAZZ_ALLOW_GPUSTACK_MAJOR_MIGRATION"


def _say(msg: str) -> None:
    print(f"[gpustack-db-guard] {msg}", file=sys.stderr)


def versions_dir() -> str | None:
    """Where THIS image keeps its migrations. Located via the installed
    package so it follows the image's own python version and layout."""
    try:
        import gpustack  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        _say(f"cannot import gpustack ({exc}) — skipping the check")
        return None
    base = os.path.dirname(os.path.abspath(gpustack.__file__))
    path = os.path.join(base, "migrations", "versions")
    return path if os.path.isdir(path) else None


def load_revisions(path: str) -> tuple[dict[str, str], dict[str, str | None]]:
    """Return (revision -> filename, revision -> down_revision).

    Parsed rather than imported: importing a migration module pulls in the
    whole alembic/gpustack runtime for no benefit, and a syntax-level read
    cannot execute anything.
    """
    known: dict[str, str] = {}
    parents: dict[str, str | None] = {}
    for name in sorted(os.listdir(path)):
        if not name.endswith(".py") or name.startswith("__"):
            continue
        try:
            with open(os.path.join(path, name), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        m = _REVISION_RE.search(text)
        if not m:
            continue
        known[m.group(1)] = name
        d = _DOWN_RE.search(text)
        parents[m.group(1)] = d.group(1) if d and d.group(1) else None
    return known, parents


def ancestry(rev: str, parents: dict[str, str | None]) -> list[str]:
    """rev and everything it descends from. Cycle-safe (a malformed chain must
    not hang a container start)."""
    chain, seen = [], set()
    cur: str | None = rev
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = parents.get(cur)
    return chain


def db_revision(url: str) -> str | None:
    """The database's current alembic head, or None when there is none."""
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    engine = create_engine(url, pool_pre_ping=False,
                           connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT version_num FROM alembic_version")).fetchall()
    finally:
        engine.dispose()
    return rows[0][0] if rows else None


def verdict(my_revs: dict[str, str], parents: dict[str, str | None],
            db_rev: str | None) -> tuple[bool, str]:
    """(ok, message). The whole decision, with no I/O — so it is testable."""
    if db_rev is None:
        return True, "database has no alembic_version yet — fresh install"

    if db_rev not in my_revs:
        return False, (
            f"the database is at alembic revision {db_rev}, which this "
            f"GPUStack image does not know. It was migrated by a NEWER "
            f"GPUStack. Starting this one would crash-loop on a schema it "
            f"cannot read (#278).\n"
            f"  Start the GPUStack variant that matches this database, or "
            f"restore gpustack_db from a backup taken before the migration.")

    # Which major boundaries would running alembic cross from here?
    head_candidates = [r for r in my_revs if r not in set(parents.values())]
    to_head: set[str] = set()
    for head in head_candidates:
        to_head.update(ancestry(head, parents))
    already = set(ancestry(db_rev, parents))
    pending = to_head - already

    crossings = sorted(
        (int(m.group(1)), r) for r in pending
        if (m := _BOUNDARY_RE.search(my_revs[r])))
    if not crossings:
        return True, (
            f"database at {db_rev} ({my_revs[db_rev]}) — same major, "
            f"{len(pending)} pending revision(s)")

    major, rev = crossings[0]
    if os.environ.get(OPT_IN) == "1":
        return True, (
            f"{OPT_IN}=1 — proceeding across the GPUStack {major}.0 boundary "
            f"({rev}). This migrates the database IRREVERSIBLY (#278).")
    return False, (
        f"the database is at alembic revision {db_rev} "
        f"({my_revs[db_rev]}), which predates the GPUStack {major}.0 "
        f"migration ({rev}). Starting this image would run that migration and "
        f"convert a database from the PREVIOUS major in place — the GPUStack "
        f"that owns it can never read it again (#278).\n"
        f"  On an llm-legacy box: start `gpustack-legacy`, not `gpustack`.\n"
        f"  Use `docker compose up -d` (profile-scoped) rather than naming the "
        f"service — all three variants share `container_name: gpustack`.\n"
        f"  To migrate deliberately, back up gpustack_db and set "
        f"{OPT_IN}=1.")


def main() -> int:
    url = os.environ.get("GPUSTACK_DATABASE_URL", "").strip()
    if not url:
        _say("GPUSTACK_DATABASE_URL is unset — skipping the check")
        return 0
    if not url.startswith(("postgresql", "postgres")):
        _say(f"non-postgres database URL ({url.split(':', 1)[0]}) — skipping")
        return 0

    path = versions_dir()
    if not path:
        _say("no migrations/versions directory found — skipping the check")
        return 0

    try:
        my_revs, parents = load_revisions(path)
        if not my_revs:
            _say("no revisions parsed — skipping the check")
            return 0
        rev = db_revision(url)
    except Exception as exc:  # noqa: BLE001
        # Fail OPEN. This guard runs before every start on every box; being
        # wrong about a box it cannot read would be the worse outage.
        _say(f"could not read the database ({type(exc).__name__}: {exc}) — "
             f"skipping the check")
        return 0

    ok, msg = verdict(my_revs, parents, rev)
    if ok:
        _say(msg)
        return 0
    _say("REFUSING TO START.")
    for line in msg.splitlines():
        _say(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
