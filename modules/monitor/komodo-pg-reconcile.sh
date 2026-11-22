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
#   * the Komodo password already logs in over the network: nothing to do
#     (every start on a migrated box);
#   * otherwise it sets the new one as the cluster's superuser over the
#     cluster's UNIX SOCKET, shared with postgres-komodo through a volume.
#     Local socket logins are trusted in that image (backup and restore use
#     them through `docker exec`), so #3337: this container never receives
#     the old password (the main cluster superuser's) at all. That covers an
#     upgraded box, monitor enabled later and a restored older archive alike;
#   * then the new password is probed over the network (pg_hba enforces
#     scram-sha-256 there; the socket would accept anything) and a failure
#     exits non-zero, so ferretdb never starts against a wrong password.
# The password reaches psql through the environment only: PGPASSWORD for the
# probe, `\getenv` for the ALTER, never argv; statement logging is off for the
# session that sets it.
set -euo pipefail

host="${KOMODO_PG_HOST:-postgres-komodo}"
sock="${KOMODO_PG_SOCKET_DIR:-/var/run/postgresql}"
user="${POSTGRES_USER:-docker}"
: "${KOMODO_PG_PASSWORD:?KOMODO_PG_PASSWORD is empty: rzfz init / upgrade mints it}"

logs_in() {
    PGPASSWORD="$1" psql -X -h "$host" -U "$user" -d postgres -tAc 'SELECT 1' 2>/dev/null | grep -qx 1
}

if logs_in "$KOMODO_PG_PASSWORD"; then
    echo "komodo-pg-reconcile: the Komodo cluster already uses its own password — ok."
    exit 0
fi

if ! psql -X -h "$sock" -U "$user" -d postgres -tAc 'SELECT 1' 2>/dev/null | grep -qx 1; then
    echo "komodo-pg-reconcile: the Komodo password does not log in as ${user} on ${host}, and the cluster's local socket (${sock}) does not answer." >&2
    echo "komodo-pg-reconcile: ferretdb and Komodo stay down until the role's password is repaired." >&2
    exit 1
fi

echo "komodo-pg-reconcile: moving the Komodo cluster to its own password..."
psql -X -q -v ON_ERROR_STOP=1 -h "$sock" -U "$user" -d postgres <<'SQL'
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
