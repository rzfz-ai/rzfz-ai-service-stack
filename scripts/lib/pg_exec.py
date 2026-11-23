# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""SQL executors for box-side maintenance scripts (#2524, #2527).

A box has no psycopg2 on the host, but it always has the core `postgres`
container, so the box path is `docker exec -i <container> psql`. The tests
use SingleDsn against the api-tier Postgres. Both return rows as lists of
strings, split on the unit separator so a value with a `|` or a tab survives.
"""
from __future__ import annotations

import os
import subprocess

SEP = "\x1f"
READ_ONLY = "SET default_transaction_read_only = on;\n"


class Psql:
    def __init__(self, container: str, user: str) -> None:
        self.container, self.user = container, user

    def query(self, db: str, sql: str, read_only: bool = False) -> list[list[str]]:
        if read_only:                 # one psql process per query: the SET ends with it
            sql = READ_ONLY + sql
        cmd = ["docker", "exec", "-i", self.container, "psql", "-X", "-q", "-A", "-t",
               "-F", SEP, "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", db]
        r = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=1800,
                           env={**os.environ, "LC_ALL": "C"})
        if r.returncode != 0:
            raise RuntimeError(f"psql on {db} failed (rc={r.returncode}): {r.stderr.strip()[:800]}")
        return [line.split(SEP) for line in r.stdout.splitlines() if line != ""]


class SingleDsn:
    """Tests only: every `db` maps to the one database named by the DSN."""

    def __init__(self, dsn: str) -> None:
        import psycopg2  # imported here: the box path must not need it
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    def query(self, db: str, sql: str, read_only: bool = False) -> list[list[str]]:
        with self.conn.cursor() as cur:
            if read_only:
                cur.execute(READ_ONLY)
            try:
                cur.execute(sql)
                if cur.description is None:
                    return []
                return [["" if v is None else str(v) for v in row] for row in cur.fetchall()]
            finally:
                if read_only:          # the connection is shared: put it back
                    cur.execute("RESET default_transaction_read_only;")
