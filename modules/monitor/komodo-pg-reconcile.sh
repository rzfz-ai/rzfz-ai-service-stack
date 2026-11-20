#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #3309: Komodo's own PostgreSQL cluster (postgres-komodo) gets its own password.
#
# The cluster was initialised with the stack's main superuser password, and
# ferretdb and komodo-core connect with it, so both held the password of the
# MAIN cluster's superuser. The image applies POSTGRES_PASSWORD only when it
# initialises an empty data directory, so an existing cluster keeps the old
# one; this one-shot moves the role to KOMODO_PG_PASSWORD on every start:
#   * the Komodo password already logs in: nothing to do (every start on a
#     migrated box);
#   * otherwise it logs in with the old password (the main superuser's) and
#     sets the new one: an upgraded box, monitor enabled later, or a restored
#     older archive whose role dump carries the old password;
#   * then the new password is probed over the network (pg_hba enforces
#     scram-sha-256 there; a local socket would accept anything) and a failure
#     exits non-zero, so ferretdb never starts against a wrong password.
# The password reaches psql through the environment only: PGPASSWORD for the
# login, `\getenv` for the ALTER, never argv; statement logging is off for the
# session that sets it.
set -euo pipefail

host="${KOMODO_PG_HOST:-postgres-komodo}"
user="${POSTGRES_USER:-docker}"
: "${KOMODO_PG_PASSWORD:?KOMODO_PG_PASSWORD is empty: rzfz init / upgrade mints it}"

logs_in() {
    PGPASSWORD="$1" psql -X -h "$host" -U "$user" -d postgres -tAc 'SELECT 1' 2>/dev/null | grep -qx 1
}

if logs_in "$KOMODO_PG_PASSWORD"; then
    echo "komodo-pg-reconcile: the Komodo cluster already uses its own password — ok."
    exit 0
fi

if [ -z "${POSTGRES_PASSWORD:-}" ] || ! logs_in "$POSTGRES_PASSWORD"; then
    echo "komodo-pg-reconcile: neither the Komodo password nor the previous one logs in as ${user} on ${host}." >&2
    echo "komodo-pg-reconcile: ferretdb and Komodo stay down until the role's password is repaired." >&2
    exit 1
fi

echo "komodo-pg-reconcile: moving the Komodo cluster off the shared password..."
PGPASSWORD="$POSTGRES_PASSWORD" psql -X -q -v ON_ERROR_STOP=1 -h "$host" -U "$user" -d postgres <<'SQL'
SET log_statement = 'none';
SET log_min_duration_statement = -1;
\getenv komodo_pw KOMODO_PG_PASSWORD
SELECT format('ALTER ROLE CURRENT_USER PASSWORD %L', :'komodo_pw') \gexec
SQL

if ! logs_in "$KOMODO_PG_PASSWORD"; then
    echo "komodo-pg-reconcile: the new password was set but does not log in — refusing to let ferretdb start." >&2
    exit 1
fi
echo "komodo-pg-reconcile: done — the Komodo cluster has its own password."
