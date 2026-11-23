#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""scripts/openuem-device-export.py — #2527: the OpenUEM device list as CSV
(onboarding/offboarding, and the server part of the fleet catalogue).

  --stale-days N   only devices whose last contact is older than N days
The schema is OpenUEM's: the required columns are checked first and a
mismatch is NAMED, never guessed. Every query runs read-only. A cell that a
spreadsheet would run as a formula (a reported hostname starting with = + - @)
is prefixed with a quote. Exit: 0 ok; 1 schema mismatch; 2 usage or psql error.
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

REQUIRED = ["hostname", "os", "last_contact"]
OPTIONAL = ["first_contact", "agent_status", "nickname", "description", "ip"]
IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
#: Leading characters a spreadsheet treats as a formula (CSV injection).
FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


class SchemaMismatch(RuntimeError):
    pass


def _check_table(table: str) -> None:
    if not isinstance(table, str) or not IDENT.match(table):
        raise ValueError(f"not a plain table name: {table!r}")


def columns(exe, db: str, table: str) -> set:
    _check_table(table)
    rows = exe.query(db, "SELECT column_name FROM information_schema.columns "
                         f"WHERE table_schema = 'public' AND table_name = '{table}';",
                     read_only=True)
    return {r[0] for r in rows}


def export_columns(exe, db: str, table: str) -> list:
    """The columns the export reads, in output order; SchemaMismatch names the gap."""
    have = columns(exe, db, table)
    missing = [c for c in REQUIRED if c not in have]
    if missing:
        raise SchemaMismatch(f"{db}.{table} lacks required column(s): {', '.join(sorted(missing))}")
    return REQUIRED + [c for c in OPTIONAL if c in have]


def export_rows(exe, db: str, table: str, stale_days):
    _check_table(table)
    if stale_days is not None and (isinstance(stale_days, bool) or not isinstance(stale_days, int)
                                   or stale_days < 0):
        raise ValueError(f"--stale-days must be a non-negative integer, got {stale_days!r}")
    cols = export_columns(exe, db, table)
    where = f" WHERE last_contact < now() - interval '{stale_days} days'" if stale_days is not None else ""
    sql = f"SELECT {', '.join(cols)} FROM {table}{where} ORDER BY hostname;"
    return [dict(zip(cols, r)) for r in exe.query(db, sql, read_only=True)]


def _cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_LEAD) else text


def write_csv(rows: list, out, fieldnames=None) -> None:
    """Header first (also for an empty result when fieldnames is given)."""
    names = list(fieldnames) if fieldnames else (list(rows[0]) if rows else [])
    if not names:
        return
    w = csv.DictWriter(out, fieldnames=names)
    w.writeheader()
    w.writerows({k: _cell(r.get(k)) for k in names} for r in rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="openuem_db")
    ap.add_argument("--table", default="agents")
    ap.add_argument("--container", default="postgres")
    ap.add_argument("--pg-user", default="docker")
    ap.add_argument("--stale-days", type=int)
    a = ap.parse_args(argv)
    from pg_exec import Psql
    exe = Psql(a.container, a.pg_user)
    try:
        rows = export_rows(exe, a.db, a.table, a.stale_days)
        cols = export_columns(exe, a.db, a.table)
    except SchemaMismatch as e:
        print(f"[openuem-export] {e}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as e:
        print(f"[openuem-export] ERROR: {e}", file=sys.stderr)
        return 2
    except (OSError, subprocess.TimeoutExpired) as e:
        # No docker on PATH, or psql hung: not a schema mismatch (exit 1).
        print(f"[openuem-export] ERROR: could not run psql in {a.container!r}: {e}", file=sys.stderr)
        return 2
    write_csv(rows, sys.stdout, cols)
    return 0


if __name__ == "__main__":
    sys.exit(main())
