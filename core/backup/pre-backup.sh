#!/bin/sh
set -eo pipefail

# ==============================================================================
# DATABASE BACKUP SCRIPT
# ==============================================================================
# This script is executed by the backup container before the volume backup.
# It creates SQL dumps for Postgres databases and triggers a save for Valkey.
# The dumps are written to /backup/databases, which is then backed up as a volume.
# ==============================================================================

# F-069: docker-cli and openssl are pre-installed in the custom Dockerfile.
# No runtime package installation needed.
if ! command -v docker >/dev/null 2>&1; then
    echo "CRITICAL: docker-cli not available. Rebuild the backup image."
    exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
    echo "CRITICAL: openssl not available. Rebuild the backup image."
    exit 1
fi

# F-052: Track critical failures to exit non-zero at the end
CRITICAL_FAILURES=0

# #139: shout BEFORE starting a nightly run on a nearly-full root disk — the
# exact prelude to both prod disk-full outages (offen additionally stages its
# archive on this filesystem unless BACKUP_TMP_DIR points elsewhere, #176).
# Warn-only by design: skipping the backup would trade a disk risk for a
# no-backup risk; the operator decides. /backup/databases sits on the root
# overlay, so this df measures the HOST root disk (same fact the agent guard
# below relies on). Lands in the backup log + NOTIFICATION_URLS consumers.
BACKUP_ROOT_DISK_WARN_PCT="${BACKUP_ROOT_DISK_WARN_PCT:-85}"
_root_used=$(df -P /backup/databases 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5+0}')
if [ "${_root_used:-0}" -ge "$BACKUP_ROOT_DISK_WARN_PCT" ] 2>/dev/null; then
    echo "WARNING: root disk ${_root_used}% used (>= ${BACKUP_ROOT_DISK_WARN_PCT}%) — a nightly archive run on a nearly-full disk can fail or truncate the backup; free space before the next run."
    echo "         Free space soon: 'docker builder prune -af', prune old backups, check /var/lib/docker/volumes. Threshold: BACKUP_ROOT_DISK_WARN_PCT."
fi

# F-052: Warn if no notification URLs are configured
if [ -z "${NOTIFICATION_URLS:-}" ]; then
    echo "WARNING: NOTIFICATION_URLS is not configured. Backup failures will not trigger external alerts."
    echo "Set BACKUP_NOTIFICATION_URLS in .env to enable (e.g. ntfy://topic, smtp://..., slack://...)."
fi

# A knowledge graph build or document ingest running now would be killed when
# the archive step stops cognee and lightrag: a scheduled run waits for it (with
# a bound) BEFORE the dumps, so dumps and archive hold the finished state.
# Never fails the backup; see the script.
KNOWLEDGE_WAIT="${RZFZ_KNOWLEDGE_WAIT:-/scripts/knowledge-wait.sh}"
if [ -f "$KNOWLEDGE_WAIT" ]; then sh "$KNOWLEDGE_WAIT" || true; fi

echo "Starting Pre-Backup Database Dump..."
mkdir -p /backup/databases

# #2584: EVERY file this hook writes under the backup source is written under
# STAGING_DIR first and moved into place only when it is complete, so a reader
# of /backup (offen's archive walk) never meets a file that is still growing —
# a production box lost a whole nightly to one ("archive/tar: write too long"). STAGING_DIR
# lies in the same volume, so the move is a rename. It is NOT part of the
# archive: core/compose.yml and the razzfazz-backup wrapper add
# `^/backup/(databases|clickhouse-backup)/\.staging(/|\z)` to BACKUP_EXCLUDE_REGEXP (a file that offen's
# walk lists and that is renamed away before it is read would fail the archive
# just the same). Cleared at every start — what an interrupted run left there
# is never finished work — except `runner/`, which belongs to
# pre-backup-run.sh and holds this very run's log.
STAGING_DIR=/backup/databases/.staging
if [ -L "$STAGING_DIR" ]; then rm -f "$STAGING_DIR"; fi
# #2759: what is staged here is not encrypted yet (an agent volume's tar holds
# its login tokens until offen encrypts the whole archive). The directory is
# owner-only (0700) and lies in the db-dumps volume, never on a shared tmpfs
# (/tmp, /dev/shm); the files in it are 0600 (the helper's umask, see §7).
(umask 077; mkdir -p "$STAGING_DIR")
chmod 0700 "$STAGING_DIR"
rzfz_staging_clear() {
    for _stale in "$STAGING_DIR"/*; do
        case "${_stale##*/}" in runner|'*') ;; *) rm -rf "$_stale" ;; esac
    done
}
rzfz_staging_clear
# ... and it is cleared again on EVERY way out of the hook: the normal end, an
# error exit (set -e), and the run being stopped (TERM, INT, HUP). A kill -9
# cannot be trapped: the next run clears what it left (the line above).
trap rzfz_staging_clear EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

# stage_path FINAL: where FINAL (an absolute path under /backup/databases) is
# written until it is complete. File names are unique across the hook.
stage_path() {
    printf '%s/%s' "$STAGING_DIR" "${1##*/}"
}
# stage_publish FINAL: move the finished file into place.
stage_publish() {
    mv -f "$(stage_path "$1")" "$1"
}
# stage_discard FINAL: a failed write leaves neither a partial file nor the
# previous run's copy, which the archive would carry as if it were current.
stage_discard() {
    rm -f "$(stage_path "$1")" "$1"
}

# Helper function for exact container name matching
container_running() {
    docker ps --format '{{.Names}}' | grep -q "^$1$"
}

# #2584: the ClickHouse backup disk is the clickhouse-backup volume, archived
# at /backup/clickhouse-backup. Its housekeeping runs FIRST, before any step
# that can be silent: a deletion cannot be staged (a walk that already listed
# the file fails at its Lstat whether the file is removed or renamed away), so
# it happens while the hook is still printing and the stream cannot have been
# cut yet. Keeps the 6 newest daily zips (§4 adds the 7th) and clears what an
# interrupted run left in the disk's staging dir.
CH_BACKUP_DIR=/var/lib/clickhouse/backups
if container_running "clickhouse"; then
    docker exec clickhouse sh -c "cd $CH_BACKUP_DIR && rm -rf .staging && ls -t daily-*.zip 2>/dev/null | tail -n +7 | xargs -r rm -f" 2>/dev/null || true
fi

# STAGE-LIVE-BEGIN
# #3450: volumes whose service keeps running through the backup
# (wazuh-manager, #855 DECISION-10) are archived from a copy, never live: a
# file that grows while offen's tar copies it aborts the WHOLE archive. The
# script copies each into STAGING_DIR and moves it to /backup/databases/<vol>;
# the live /backup/<vol> is excluded (core/compose.yml). It runs early, before
# the dumps, because it replaces the previous run's copy (a deletion cannot be
# staged, see the ClickHouse note above). A copy that is not complete is an
# ERROR line in this log; the archive still runs.
STAGE_LIVE="${RZFZ_STAGE_LIVE:-/scripts/stage-live-volumes.sh}"
if [ -f "$STAGE_LIVE" ]; then
    sh "$STAGE_LIVE" || echo "stage-live: ERROR: not every volume was staged completely (see the lines above); this backup is incomplete for it."
else
    echo "stage-live: ERROR: $STAGE_LIVE is not mounted — wazuh-manager-data is NOT in this backup (recreate backup-service)."
fi
# STAGE-LIVE-END

# ------------------------------------------------------------------------------
# 1. Postgres Core
# ------------------------------------------------------------------------------
# #134: lightrag moved from Apache AGE to NetworkX storage. An orphaned `age`
# extension row left in lightrag_db (whose $libdir/age the image no longer ships)
# makes the next `pg_dumpall` ABORT → a silently incomplete, non-restorable
# backup. Drop it idempotently before the cluster dump (no-op when absent).
if container_running "postgres"; then
    PGPASSWORD="${POSTGRES_PASSWORD}" docker exec -e PGPASSWORD postgres psql -U "${POSTGRES_USER}" \
        -d lightrag_db -c "DROP EXTENSION IF EXISTS age CASCADE;" >/dev/null 2>&1 || true
fi
echo "Backing up Postgres Core (Container: postgres)..."
if container_running "postgres"; then
    # Use docker exec with -e for PGPASSWORD (isolated in container environment)
    if PGPASSWORD="${POSTGRES_PASSWORD}" docker exec -e PGPASSWORD postgres pg_dumpall -U "${POSTGRES_USER}" > "$(stage_path /backup/databases/postgres_core.sql)" \
        && stage_publish /backup/databases/postgres_core.sql; then
        echo "Postgres Core backup successful."
    else
        stage_discard /backup/databases/postgres_core.sql
        echo "ERROR: Postgres Core backup failed."
        # F-052: Track critical failure (Postgres is a core service)
        CRITICAL_FAILURES=$((CRITICAL_FAILURES + 1))
    fi
else
    echo "WARNING: Container 'postgres' is not running."
    # F-052: Postgres not running is a critical failure for backup integrity
    CRITICAL_FAILURES=$((CRITICAL_FAILURES + 1))
fi

# ------------------------------------------------------------------------------
# 2. Postgres Komodo (FerretDB Backend)
# ------------------------------------------------------------------------------
# #2524: Komodo's data lives in FerretDB on DocumentDB (an extension in
# postgres-komodo's `postgres` DB). DocumentDB keeps its catalogue — which
# collection is which `documentdb_data.documents_<id>` table, its indexes, and
# the id sequences — in tables that BELONG to the extension and that it never
# marks for pg_dump. So pg_dumpall carries the documents tables and none of the
# catalogue: after a restore every collection's table exists, no collection is
# known, the id sequence starts again at 1, and komodo-core crash-loops with
# `relation "documents_<n>" already exists` (measured on a staging restore).
# This writes the catalogue next to the cluster dump, as a script the restore
# replays after the dump: the rows of both catalogue tables (named columns, so a
# newer DocumentDB fills its new columns with their defaults) and both sequence
# positions, in one transaction, ending in a trailer that proves it is whole.
KOMODO_CATALOG_TRAILER='-- end of the razzfazz.ai DocumentDB catalogue'
dump_documentdb_catalog() {   # $1 = file to write
    PGPASSWORD="${POSTGRES_PASSWORD}" docker exec -i -e PGPASSWORD postgres-komodo \
        psql -X -q -At -U "${POSTGRES_USER}" -d postgres -v ON_ERROR_STOP=1 -f - > "$1" <<'SQL'
SELECT '-- razzfazz.ai DocumentDB catalogue: replayed after postgres_komodo.sql';
SELECT 'BEGIN;';
SELECT 'COPY documentdb_api_catalog.collections (' || string_agg(quote_ident(attname), ', ' ORDER BY attnum) || ') FROM stdin;'
  FROM pg_attribute WHERE attrelid = 'documentdb_api_catalog.collections'::regclass AND attnum > 0 AND NOT attisdropped;
COPY documentdb_api_catalog.collections TO STDOUT;
SELECT '\.';
SELECT 'COPY documentdb_api_catalog.collection_indexes (' || string_agg(quote_ident(attname), ', ' ORDER BY attnum) || ') FROM stdin;'
  FROM pg_attribute WHERE attrelid = 'documentdb_api_catalog.collection_indexes'::regclass AND attnum > 0 AND NOT attisdropped;
COPY documentdb_api_catalog.collection_indexes TO STDOUT;
SELECT '\.';
SELECT format('SELECT pg_catalog.setval(%L, %s, %L::boolean);', 'documentdb_api_catalog.collections_collection_id_seq', last_value, is_called)
  FROM documentdb_api_catalog.collections_collection_id_seq;
SELECT format('SELECT pg_catalog.setval(%L, %s, %L::boolean);', 'documentdb_api_catalog.collection_indexes_index_id_seq', last_value, is_called)
  FROM documentdb_api_catalog.collection_indexes_index_id_seq;
SELECT 'COMMIT;';
SELECT '-- end of the razzfazz.ai DocumentDB catalogue';
SQL
}
echo "Backing up Postgres Komodo (Container: postgres-komodo)..."
# Check if the monitoring profile is active (container exists)
if container_running "postgres-komodo"; then
    if PGPASSWORD="${POSTGRES_PASSWORD}" docker exec -e PGPASSWORD postgres-komodo pg_dumpall -U "${POSTGRES_USER}" > "$(stage_path /backup/databases/postgres_komodo.sql)" \
        && stage_publish /backup/databases/postgres_komodo.sql; then
        echo "Postgres Komodo backup successful."
        # #2524: the DocumentDB catalogue the cluster dump cannot carry.
        if dump_documentdb_catalog "$(stage_path /backup/databases/postgres_komodo_catalog.sql)" \
            && [ "$(tail -n 1 "$(stage_path /backup/databases/postgres_komodo_catalog.sql)")" = "$KOMODO_CATALOG_TRAILER" ] \
            && stage_publish /backup/databases/postgres_komodo_catalog.sql; then
            echo "Postgres Komodo catalogue backup successful."
        else
            stage_discard /backup/databases/postgres_komodo_catalog.sql
            echo "ERROR: Postgres Komodo catalogue backup failed — a restore of this backup brings Komodo back EMPTY."
        fi
    else
        stage_discard /backup/databases/postgres_komodo.sql
        echo "ERROR: Postgres Komodo backup failed."
    fi
else
    echo "Info: Container 'postgres-komodo' is not running (Monitor profile might be off)."
fi

# ------------------------------------------------------------------------------
# 3. Valkey (Redis)
# ------------------------------------------------------------------------------
echo "Triggering Valkey Save (Container: valkey)..."
if container_running "valkey"; then
    # #2746: the password travels in the environment of the docker client and is
    # forwarded by NAME (`-e REDISCLI_AUTH`): never `-e NAME=value` on argv (`ps`).
    if REDISCLI_AUTH="${VALKEY_PASSWORD}" docker exec -e REDISCLI_AUTH valkey valkey-cli SAVE; then
        echo "Valkey save successful."
    else
        echo "ERROR: Valkey save failed."
    fi
else
    echo "WARNING: Container 'valkey' is not running."
fi

# ------------------------------------------------------------------------------
# 4. ClickHouse (OpenLIT span store — observability profile, M019)
# ------------------------------------------------------------------------------
# Native `BACKUP TO Disk('backups', ...)` writes a backup file into the
# clickhouse-backup named volume, which is mounted read-only into this backup
# container at /backup/clickhouse-backup and rolled into the tar.gz by
# docker-volume-backup.
#
# Pruning: keep the 7 most recent daily backups inside the volume; older ones
# are deleted after the snapshot completes (the tar.gz is the long-term store).
echo "Backing up ClickHouse OpenLIT database (Container: clickhouse)..."
if container_running "clickhouse"; then
    BACKUP_NAME="daily-$(date +%F-%H%M)"
    # #2746: no --password. clickhouse-client reads CLICKHOUSE_PASSWORD from
    # its environment, forwarded below by name; a `--password=<value>` would be
    # in the argv of both the docker client and the in-container client, which
    # every user of the host reads in `ps`. An empty value is the default
    # user's empty password (#137), which the env form accepts too.
    # #2584: ClickHouse writes the zip itself, into the disk's `.staging/`
    # (ClickHouse 25.8 creates the directory; measured with the pinned
    # binary), and it is renamed into place inside the container — same
    # volume, so the rename is atomic. `/backup/clickhouse-backup/.staging` is
    # excluded from the archive like /backup/databases/.staging. The prune of
    # older zips runs at the top of this hook (see CH_BACKUP_DIR).
    if CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-}" docker exec -e CLICKHOUSE_PASSWORD clickhouse \
        clickhouse-client --user="${CLICKHOUSE_USER:-default}" \
        --query "BACKUP DATABASE openlit TO Disk('backups', '.staging/${BACKUP_NAME}.zip')" \
        && docker exec clickhouse mv -f "$CH_BACKUP_DIR/.staging/${BACKUP_NAME}.zip" "$CH_BACKUP_DIR/${BACKUP_NAME}.zip"; then
        echo "ClickHouse backup successful: ${BACKUP_NAME}.zip"
    else
        docker exec clickhouse rm -f "$CH_BACKUP_DIR/.staging/${BACKUP_NAME}.zip" 2>/dev/null || true
        # #137: ClickHouse holds experimental, regenerable OpenLIT observability
        # telemetry — a failure here must NOT abort the whole backup (which
        # captures Postgres, .env, and per-user agent state). Log loudly but do
        # NOT increment CRITICAL_FAILURES.
        # The known cause first: a backup disk ClickHouse (uid 101, the exec's
        # default user) cannot write — docker creates the clickhouse-backup
        # volume root-owned. Name it and the repair, not just "failed".
        if ! docker exec clickhouse test -w "$CH_BACKUP_DIR" 2>/dev/null; then
            CH_OWNER=$(docker exec clickhouse stat -c '%u:%g' "$CH_BACKUP_DIR" 2>/dev/null || echo unknown)
            echo "WARNING: ClickHouse OpenLIT backup failed: ClickHouse (uid 101) cannot write its backup disk $CH_BACKUP_DIR — the clickhouse-backup volume is owned by ${CH_OWNER}, it must be 101:101. Repair: rzfz post-install --refresh (sets the owner). Non-critical — observability telemetry only; core data backup continues."
        else
            echo "WARNING: ClickHouse OpenLIT backup failed (non-critical — observability telemetry only; core data backup continues)."
        fi
    fi
else
    echo "Info: Container 'clickhouse' is not running (observability profile might be off)."
fi

# ------------------------------------------------------------------------------
# 5. Conversation-map cleanup (M019 S06)
# ------------------------------------------------------------------------------
# Per-pipe conversation-id maps in /app/backend/data/*_conv_map.json on the
# open-webui-data volume accumulate orphans when users delete OpenWebUI chats
# (the upstream WebUI has no undelete, so we don't keep a grace window).
#
# Runs from inside the openwebui container so it can both read the per-volume
# JSON files AND query Postgres for the live chat-id set, without needing
# write access to open-webui-data from this backup container.
#
# Currently cleans dify_conv_map.json (M019 S02). M020 adds hermes/moltis/
# opencode pipes — extend this loop when those land.
echo "Cleaning orphaned entries from conversation maps..."
if container_running "openwebui"; then
    DROPPED=$(POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" docker exec -e POSTGRES_USER="${POSTGRES_USER}" \
        -e POSTGRES_PASSWORD \
        -e OPENWEBUI_DB="${OPENWEBUI_DB:-openwebui_db}" \
        openwebui python3 - <<'PYEOF'
import json, os, urllib.parse
from pathlib import Path

# Read live chat-id set from openwebui DB (over docker DNS)
import sys
try:
    import psycopg2
except Exception:
    print("psycopg2 not available in openwebui image; skipping cleanup")
    sys.exit(0)

dsn = (f"host=postgres port=5432 dbname={os.environ['OPENWEBUI_DB']} "
       f"user={os.environ['POSTGRES_USER']} password={os.environ['POSTGRES_PASSWORD']}")
try:
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SELECT id FROM chat")
    live = {r[0] for r in cur.fetchall()}
    conn.close()
except Exception as e:
    print(f"DB query failed (skipping cleanup): {e}")
    sys.exit(0)

dropped = 0
for jf in Path("/app/backend/data").glob("*_conv_map.json"):
    try:
        data = json.loads(jf.read_text())
    except Exception:
        continue
    # Keys are "<pipe_id>::<chat_id>" — drop entries whose chat_id isn't in DB
    keep = {}
    for key, val in data.items():
        chat_id = key.split("::", 1)[-1] if "::" in key else key
        if chat_id in live:
            keep[key] = val
        else:
            dropped += 1
    if len(keep) != len(data):
        tmp = jf.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep))
        tmp.replace(jf)
print(dropped)
PYEOF
    2>/dev/null || echo "0")
    DROPPED="${DROPPED:-0}"
    echo "Conversation-map cleanup: ${DROPPED} orphan(s) dropped."
else
    echo "Info: Container 'openwebui' is not running; conversation-map cleanup skipped."
fi

# ------------------------------------------------------------------------------
# 6. Encrypted .env File Backup
# ------------------------------------------------------------------------------
echo "Creating encrypted .env backup..."

# BSB-04 / R-DEF-04: re-read BACKUP_ENCRYPTION_PASSWORD from /scripts/dot-env
# at every invocation. The bind-mounted .env IS the source of truth — the
# process env was frozen at container start and may be stale after a
# rotation. Wrapper /usr/local/bin/razzfazz-backup also exports the fresh
# value into our env (covers manually-triggered backups); this re-read is
# defense-in-depth + the only correctness path for offen-internal cron
# ticks (which don't go through the wrapper).
#
# Mirrors the parser in razzfazz-backup-wrapper.sh and
# backup_manager._read_passphrase_from_env_file: strip ` # comment`,
# strip surrounding quotes, strip whitespace, first-match wins, never
# `source` the file.
DOT_ENV_PATH="/scripts/dot-env"
ENCRYPTION_PASS=""
if [ -f "$DOT_ENV_PATH" ]; then
    RAW_LINE=$(grep -m1 '^BACKUP_ENCRYPTION_PASSWORD=' "$DOT_ENV_PATH" 2>/dev/null || true)
    if [ -n "$RAW_LINE" ]; then
        RAW_VALUE=${RAW_LINE#BACKUP_ENCRYPTION_PASSWORD=}
        # Strip trailing ` # comment`
        STRIPPED=$(printf '%s' "$RAW_VALUE" | sed 's/[[:space:]]\{1,\}#.*$//')
        # Strip surrounding double or single quotes
        case "$STRIPPED" in
            \"*\")
                STRIPPED=${STRIPPED#\"}
                STRIPPED=${STRIPPED%\"}
                ;;
            \'*\')
                STRIPPED=${STRIPPED#\'}
                STRIPPED=${STRIPPED%\'}
                ;;
        esac
        ENCRYPTION_PASS=$(printf '%s' "$STRIPPED" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    fi
fi
# Fall back to process env if the .env couldn't be read (e.g. the bind
# mount is missing on a misconfigured deployment).
ENCRYPTION_PASS="${ENCRYPTION_PASS:-${BACKUP_ENCRYPTION_PASSWORD:-}}"

if [ -z "$ENCRYPTION_PASS" ]; then
    echo "WARNING: No encryption password available. Skipping .env encryption."
else
    # Encrypt .env
    if [ -f /scripts/dot-env ]; then
        if RZFZ_OSSL_PASS="$ENCRYPTION_PASS" openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
            -in /scripts/dot-env -out "$(stage_path /backup/databases/env-backup.enc)" \
            -pass env:RZFZ_OSSL_PASS 2>/dev/null \
            && stage_publish /backup/databases/env-backup.enc; then
            echo ".env encrypted successfully."
        else
            stage_discard /backup/databases/env-backup.enc
            echo "ERROR: .env encryption failed."
        fi
    else
        echo "WARNING: /scripts/dot-env not mounted. Skipping .env encryption."
    fi

    # Encrypt .env.dify
    if [ -f /scripts/dot-env-dify ]; then
        if RZFZ_OSSL_PASS="$ENCRYPTION_PASS" openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
            -in /scripts/dot-env-dify -out "$(stage_path /backup/databases/env-dify-backup.enc)" \
            -pass env:RZFZ_OSSL_PASS 2>/dev/null \
            && stage_publish /backup/databases/env-dify-backup.enc; then
            echo ".env.dify encrypted successfully."
        else
            stage_discard /backup/databases/env-dify-backup.enc
            echo "ERROR: .env.dify encryption failed."
        fi
    else
        echo "INFO: /scripts/dot-env-dify not mounted. Skipping .env.dify encryption."
    fi
    # #1621: what CODE made this archive. Written unencrypted on purpose — it
    # is the one thing a restore must be able to read BEFORE it has a
    # passphrase, and it holds nothing secret. Without it a ga.15 backup can be
    # restored onto a ga.9 checkout and nothing checks: the volumes carry a
    # schema the code does not know, and the failure surfaces later, somewhere
    # else, as a broken service.
    if [ -f /scripts/dot-env ]; then
        _bv=$(grep -m1 '^RAZZFAZZ_VERSION=' /scripts/dot-env | cut -d= -f2- | tr -d '"'"'"'"' )
        _bc=$(grep -m1 '^RAZZFAZZ_COMMIT='  /scripts/dot-env | cut -d= -f2- | tr -d '"'"'"'"' )
        printf '{"version":"%s","commit":"%s","created_at":"%s"}\n' \
            "${_bv}" "${_bc}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            > "$(stage_path /backup/databases/stack-version.json)" \
            && stage_publish /backup/databases/stack-version.json
        echo "Stack version recorded: ${_bv:-unknown} (${_bc:-?})"
    fi

    # #1621: certs/ — the box's TLS material, and the only thing in a backup
    # whose loss is FINAL. A certificate can be re-issued; the private key
    # cannot be recovered, only replaced, and replacing it is a customer-facing
    # event. It lives outside every volume, so nothing carried it until now: a
    # restore on fresh hardware came up self-signed (#1590/#1621).
    #
    # Same passphrase and the same cipher as the .env above, deliberately — one
    # secret to hold, one recipe to remember in an incident. tar first because
    # it is a DIRECTORY, and `--numeric-owner` so a restore does not depend on
    # uids that only exist on the box that made the backup.
    # #2544: the tar's stderr is not discarded — when this fails, its own
    # message is the only record of why. This tar and the box-local one below
    # stay on this image's BusyBox tar: certs/, .checksums.db and overlay/ hold
    # no sparse files (the agent volumes, which can, use GNU tar -S in §7).
    if [ -d /scripts/certs ]; then
        if tar czf - --numeric-owner -C /scripts certs \
            | RZFZ_OSSL_PASS="$ENCRYPTION_PASS" openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
                -out "$(stage_path /backup/databases/certs-backup.tar.gz.enc)" \
                -pass env:RZFZ_OSSL_PASS 2>/dev/null \
            && stage_publish /backup/databases/certs-backup.tar.gz.enc; then
            echo "certs/ encrypted successfully."
        else
            stage_discard /backup/databases/certs-backup.tar.gz.enc
            # Loud, and not fatal: a backup without the certs is still worth
            # having, but the operator must know which one he is holding.
            echo "ERROR: certs/ encryption failed — this backup does NOT carry the box's TLS material."
        fi
    else
        echo "INFO: /scripts/certs not mounted. Skipping certs encryption."
    fi

    # #1621 rev-D: the REST of the box-local state — inventoried on 0.79 and
    # measured against this hook, where neither appeared.
    #
    #   .checksums.db is the governance baseline. Losing it does not break a
    #   box; it erases the record of what that box once was, and that record is
    #   not reconstructible by construction.
    #
    #   overlay/ holds the Enterprise docs. Rebuildable by
    #   sync-enterprise-overlay.sh — but only from a box that still reaches the
    #   source, which is precisely what a customer box after an outage cannot
    #   assume.
    #
    # ONE archive, same passphrase and cipher as certs/ above, for the reason
    # that block already states: one secret to hold, one recipe to remember in
    # an incident.
    #
    # HONEST LIMIT: `.checksums.db` is copied while the stack runs. It is a
    # SQLite file that is written only by `rzfz setup --checksum-take`, so a
    # torn copy is unlikely — but it is not impossible, and no journal is
    # captured with it. The alternative (stopping the config portal for the
    # backup) costs more than the case is worth; this note is here so the next
    # reader does not have to rediscover the trade.
    _bl_items=""
    [ -f /scripts/checksums.db ] && _bl_items="${_bl_items} checksums.db"
    [ -d /scripts/overlay ] && _bl_items="${_bl_items} overlay"
    if [ -n "${_bl_items}" ]; then
        # shellcheck disable=SC2086 — the list is built from fixed names above
        if tar czf - --numeric-owner -C /scripts ${_bl_items} \
            | RZFZ_OSSL_PASS="$ENCRYPTION_PASS" openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
                -out "$(stage_path /backup/databases/box-local-backup.tar.gz.enc)" \
                -pass env:RZFZ_OSSL_PASS 2>/dev/null \
            && stage_publish /backup/databases/box-local-backup.tar.gz.enc; then
            echo "box-local state encrypted successfully (${_bl_items# })."
        else
            stage_discard /backup/databases/box-local-backup.tar.gz.enc
            echo "ERROR: box-local state encryption failed — this backup does NOT carry ${_bl_items# }."
        fi
    else
        echo "INFO: no box-local state mounted (.checksums.db / overlay). Skipping."
    fi

    # #3412: box-local compose overlays — compose.local.<name>.yml in the stack
    # root. In no package and not in version control, so a backup is their only
    # copy; a restored .env whose COMPOSE_FILE names one that is not back makes
    # compose refuse the whole chain. The backup MANAGER collects them: it mounts
    # the stack root read-only already, and it applies the one rule its restore
    # applies (razzfazz_common.compose_local) — a file outside it is skipped and
    # named here, never archived for the restore to refuse. Encrypted here with
    # the .env's passphrase and cipher. No overlay: the previous run's archive is
    # removed, so a backup never carries overlays the box has since dropped.
    _cl_plain=$(mktemp /tmp/compose-local.XXXXXX)
    _cl_log="${_cl_plain}.log"
    _cl_rc=0
    if container_running "razzfazz-backup-management"; then
        docker exec razzfazz-backup-management python /app/backup_manager.py compose-local-collect \
            >"$_cl_plain" 2>"$_cl_log" || _cl_rc=$?
    else
        _cl_rc=255
        echo "the backup manager (razzfazz-backup-management) is not running" > "$_cl_log"
    fi
    sed -n 's/^SKIPPED /WARNING: box-local compose overlay NOT in this backup: /p' "$_cl_log" 2>/dev/null || true
    _cl_named=""
    if [ -f /scripts/dot-env ]; then
        _cl_cf=$(grep -m1 '^COMPOSE_FILE=' /scripts/dot-env 2>/dev/null | cut -d= -f2- | tr -d '"'"'" || true)
        case ":${_cl_cf}:" in
            *:compose.local.*|*:./compose.local.*) _cl_named=yes ;;
        esac
    fi
    case "$_cl_rc" in
        0)
            if RZFZ_OSSL_PASS="$ENCRYPTION_PASS" openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
                    -in "$_cl_plain" -out "$(stage_path /backup/databases/compose-local-backup.tar.gz.enc)" \
                    -pass env:RZFZ_OSSL_PASS 2>/dev/null \
                && stage_publish /backup/databases/compose-local-backup.tar.gz.enc; then
                echo "box-local compose overlays encrypted successfully ($(sed -n 's/^CARRIED //p' "$_cl_log"))."
            else
                stage_discard /backup/databases/compose-local-backup.tar.gz.enc
                echo "ERROR: compose overlay encryption failed — this backup does NOT carry the box-local compose overlays."
                if [ -n "$_cl_named" ]; then
                    CRITICAL_FAILURES=$((CRITICAL_FAILURES + 1))
                fi
            fi
            ;;
        3)
            stage_discard /backup/databases/compose-local-backup.tar.gz.enc
            echo "INFO: no box-local compose overlay to carry (compose.local.<name>.yml in the stack root)."
            ;;
        *)
            stage_discard /backup/databases/compose-local-backup.tar.gz.enc
            _cl_why=$(grep -v '^SKIPPED ' "$_cl_log" 2>/dev/null | tail -n 3 | tr '\n' ' ' || true)
            if [ -n "$_cl_named" ]; then
                # COMPOSE_FILE names an overlay this backup cannot carry: a
                # restore of it would not come up on this box's settings.
                echo "ERROR: the box-local compose overlays could not be collected (${_cl_why:-exit ${_cl_rc}}), but COMPOSE_FILE names one — this backup is INCOMPLETE for this box."
                CRITICAL_FAILURES=$((CRITICAL_FAILURES + 1))
            else
                echo "INFO: box-local compose overlays not collected (${_cl_why:-exit ${_cl_rc}}); COMPOSE_FILE names none, so this backup carries none."
            fi
            ;;
    esac
    rm -f "$_cl_plain" "$_cl_log"

fi

# ------------------------------------------------------------------------------
# 7. M030-S4: per-user agent volume snapshots (default-include)
# ------------------------------------------------------------------------------
# Per-user agent volumes are named `agent-{type}-{slug}-{role}` (catalog
# `name_suffix` per S1). They're DYNAMIC — agent-manager creates them at
# provision time, so they can't be declared in compose.yml's static mount
# list like core volumes. Instead: walk agent_instances table → for each
# non-destroyed instance → docker run --rm an alpine container per volume
# it mounts to tar it into /backup/databases/agents/<instance_id>/<volume>.tar.
# offen/docker-volume-backup picks up /backup/databases (db-dumps volume)
# and includes our tarballs in the main backup archive.
#
# Layout inside the backup archive (2026.09-ga.4, #2531):
#   databases/agents/instances.sql                    — agent_instances + agent_types rows
#   databases/agents/<instance_id>/manifest.json      — instance, slug, type, container, volumes
#   databases/agents/<instance_id>/<volume>.tar       — one tar per mounted volume
# (2026.09-ga.3 and older wrote databases/agents/<slug>/<type>/<role>.tar, no
# manifest; the restore still reads that layout.)
#
# Opt-out: set BACKUP_SKIP_AGENTS=true to skip this entire section
# (used by `razzfazz-backup.sh backup --skip-agents`). Skip is also
# automatic on dev boxes with no agent-manager / no agent_instances.
if [ "${BACKUP_SKIP_AGENTS:-false}" = "true" ]; then
    echo "Per-user agent backup skipped (BACKUP_SKIP_AGENTS=true)."
elif ! container_running "agent-manager"; then
    echo "Per-user agent backup skipped (agent-manager not running)."
else
    echo "Snapshotting per-user agent volumes..."
    AGENT_DUMP_DIR=/backup/databases/agents
    mkdir -p "$AGENT_DUMP_DIR"

    # ----------------------------------------------------------------------
    # Disk-full guards (2026-08 incident). A per-user coding agent transiently
    # ballooned its workspace to ~636 GB (runaway build artifact / log); this
    # loop tars whole agent volumes with no cap, so it faithfully wrote a
    # 636 GB tar into the db-dumps volume, filled the host root disk to 100%,
    # crash-looped postgres, and took the whole stack down. The agent backup
    # must NEVER be allowed to fill the host. Two thresholds, both overridable:
    #   * skip ALL agent-volume snapshots once the target fs is past this
    #     usage % (a backup should never be the thing that fills a tight disk);
    #   * skip an individual volume whose allocated size (du -sm) exceeds this cap — a
    #     coding workspace bloated by caches/runaway files is not critical data
    #     and its snapshot endangers the host. Both emit a loud WARNING so the
    #     operator sees the skip in the backup log (silence != success).
    AGENT_BACKUP_MAX_USED_PCT="${BACKUP_AGENT_MAX_USED_PCT:-85}"
    AGENT_VOL_MAX_MB="${BACKUP_AGENT_VOL_MAX_MB:-25600}"   # 25 GiB per-volume cap
    # Usage % of the fs backing /backup/databases (== host root fs where the
    # docker volumes live). Empty if df is unavailable → guard no-ops safely.
    disk_used_pct() { df -P /backup/databases 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5+0}'; }

    # Snapshot the agent_instances table (text-format; small, helpful for
    # restore tooling to know what to re-provision).
    if PGPASSWORD="${POSTGRES_PASSWORD}" docker exec -e PGPASSWORD postgres pg_dump \
        -U "${POSTGRES_USER}" -d agent_manager_db \
        --table=agent_instances --table=agent_types --data-only \
        > "$(stage_path "$AGENT_DUMP_DIR/instances.sql")" 2>/dev/null \
        && stage_publish "$AGENT_DUMP_DIR/instances.sql"; then
        echo "  agent_instances + agent_types data dumped."
    else
        stage_discard "$AGENT_DUMP_DIR/instances.sql"
        echo "  WARNING: agent_instances dump failed (continuing)."
    fi

    # 2026.09-ga.4 (#2531): per INSTANCE, exactly the volumes it MOUNTS.
    # Until 2026.09-ga.3 this listed `docker volume ls --filter name=^<container_name>-`
    # and wrote `<slug>/<type>/<role>.tar`. The prefix of instance 1
    # (`agent-<type>-<slug>-`) also matches instance 2's `agent-<type>-<slug>-2-*`,
    # and both instances wrote the same `<slug>/<type>/<role>.tar` path, so
    # whichever row came last (no ORDER BY) overwrote the other's archive.
    # Now:
    #   * the volume names come from the row's `mounted_volumes` (#2518,
    #     migration 14, {"primary": {name: mount}, "companion": {...}}); a row
    #     where it is NULL/unreadable falls back to the Mounts of the instance's
    #     containers (label razzfazz.agent.instance=<id>, exact container_name
    #     for containers that predate the label). NEVER a name prefix.
    #   * the archive is keyed on the instance: agents/<instance_id>/<volume>.tar,
    #     plus agents/<instance_id>/manifest.json (instance_id, user_slug,
    #     agent_type, container_name, and per volume its role and mount), which
    #     the restore reads to put every volume back under its OWN name.
    #   * instances in id order, volumes in name order (deterministic).
    # Source choice: this container already reads agent_manager_db through
    # `docker exec postgres psql` (the rows below) and already talks to docker
    # (socket proxy): no new access, no new credential. The JSON is unpacked
    # by Postgres itself (jsonb_each), because this Alpine image has no jq.
    AGENT_INSTANCE_COUNT=0
    AGENT_VOLUME_COUNT=0
    AGENT_FAIL_COUNT=0
    # #2544 (fix round 1): the helper that reads an agent volume runs GNU tar,
    # because only GNU tar can archive a SPARSE file as sparse (`-S`). BusyBox
    # tar (every alpine tag) has no -S and writes a file's holes as real zeros:
    # `du -sm` measures ALLOCATED blocks, so a sparse workspace passes the size
    # guard, then its tar runs into the `ulimit -f` cap (2 x the per-volume cap)
    # and dies of SIGXFSZ — on every run, and the volume is never backed up
    # (prod has a workspace of 1.233 PB apparent size). With -S the archive
    # tracks the allocated data, which is exactly what `du -sm` capped.
    #
    # The image is the stack's own data store, the `postgres` service's image
    # (pgvector/pgvector:<tag>, Debian-based: GNU tar is an Essential package
    # there). It is whatever the box RUNS — read off the postgres container, so
    # the tag comes from core/compose.yml via the box itself, never from a
    # literal here — and therefore present on every box, offline boxes and
    # offline packages included. `alpine:latest` (the old helper) was a pull.
    # BACKUP_AGENT_TAR_IMAGE overrides. The premise is CHECKED: an image whose
    # tar is not GNU tar is refused and every agent volume is a counted failure.
    #
    # #2785: every `docker run` of this image carries
    # `--tmpfs /var/lib/postgresql/data`. The postgres image declares
    # `VOLUME /var/lib/postgresql/data`, so without it each helper run created
    # and destroyed an anonymous volume — about 42 a night, each one a Wazuh
    # volume create/mount/unmount/destroy event. The helpers never write there
    # (they read /data and write /dumps); the tmpfs only takes the volume's
    # place. Guard: tests/unit/consistency/test_2785_backup_helper_runs_create_no_volume.py.
    AGENT_TAR_IMAGE="${BACKUP_AGENT_TAR_IMAGE:-}"
    if [ -z "$AGENT_TAR_IMAGE" ]; then
        AGENT_TAR_IMAGE=$(docker inspect --type container --format '{{.Config.Image}}' postgres 2>/dev/null || true)
    fi
    # #3164 (client approval): every helper run pins LC_ALL=C. This hook PARSES
    # the helper's output — 'GNU tar' from --version, the size check's fields,
    # and above all the snapshot's stderr (the rc-1 changed/vanished guard and
    # the WARNING's error line). A box's own locale translates tar's
    # diagnostics (de_DE.UTF-8: "Funktion open fehlgeschlagen: Keine
    # Berechtigung" for "Cannot open: Permission denied") and the parse then
    # misses them. AGENTS.md: assert on a tool's text only under LC_ALL=C.
    if [ -z "$AGENT_TAR_IMAGE" ]; then
        echo "  ERROR: could not read the postgres container's image — no GNU tar helper; agent volumes are NOT backed up (set BACKUP_AGENT_TAR_IMAGE)."
    elif ! _tar_version=$(docker run --rm --tmpfs /var/lib/postgresql/data -e LC_ALL=C --entrypoint tar "$AGENT_TAR_IMAGE" --version 2>&1) \
         || ! printf '%s\n' "$_tar_version" | head -n 1 | grep -q 'GNU tar'; then
        echo "  ERROR: the helper image $AGENT_TAR_IMAGE has no GNU tar ($(printf '%s\n' "$_tar_version" | head -n 1)) — sparse volumes cannot be archived; agent volumes are NOT backed up."
        AGENT_TAR_IMAGE=""
    fi
    AGENT_INSTANCE_LABEL="razzfazz.agent.instance"
    AGENT_COMPANION_LABEL="razzfazz.agent.companion"

    # #3060: core dumps are not user data. Agent containers wrote them into the
    # user's workspace (the host's core_pattern is a bare `core`, and the core
    # ulimit was unlimited): 1167 Chromium cores on one box, 33 GB allocated and
    # 732 TB apparent — which alone pushed that workspace over the per-volume
    # size cap, so none of it was backed up. The agent manager now creates
    # containers with core ulimit 0; the cores already written are left out
    # of both the size check and the archive here (the owner deletes them).
    # A core dump is a REGULAR file named `core` or `core.<digits>` whose ELF
    # header says ET_CORE — never a directory called core (a source tree has
    # those) and never an ordinary file that happens to have the name.
    # #3146: regenerable caches are left out the same way: `.cache` (XDG: pip,
    # Playwright browsers, build caches) and `.npm/_cacache` at the root of an
    # agent volume, which is where they are when the volume is the agent's home.
    # On prod they were 7.5 GB of one home volume that, with the rest, missed
    # the cap. Deliberately NOT node_modules or .venv: on an offline box those
    # can be the only copy of what a project needs. A symlink is not followed.
    # Shell text for the helper container's sh (it runs in the GNU tar image):
    # `core_dumps` prints the path of every core dump under /data, one a line;
    # `cache_dirs` the cache directories that exist.
    AGENT_CORE_DUMPS_SH='core_dumps() {
        find /data -xdev -type f \( -name core -o -name "core.[0-9]*" \) 2>/dev/null \
        | grep -E "/core(\.[0-9]+)?\$" \
        | while IFS= read -r f; do
            case "$(od -An -tx1 -N18 "$f" 2>/dev/null | tr -d " \n")" in
                7f454c46*0400|7f454c46*0004) printf "%s\n" "$f" ;;
            esac
        done
    }
    cache_dirs() {
        for d in /data/.cache /data/.npm/_cacache; do
            [ -d "$d" ] && [ ! -L "$d" ] && printf "%s\n" "$d"
        done
        return 0
    }
'

    # A volume name and an instance id go into paths and into `sh -c` below,
    # and a volume is read by this job — so only an AGENT volume is accepted:
    # `agent-[A-Za-z0-9_.-]+` (every name the provisioner creates) or docker's
    # 64-hex anonymous form (E1 reuses those). `postgres_data`, `../x` or a
    # quote are refused, whatever a row or a container claims (#2531 review).
    agent_volume_name_ok() {
        case "$1" in
            agent-*)
                case "${1#agent-}" in ''|*[!A-Za-z0-9_.-]*) return 1 ;; esac
                return 0 ;;
        esac
        case "$1" in *[!0-9a-f]*) return 1 ;; esac
        [ "${#1}" -eq 64 ]
    }
    agent_instance_id_ok() {
        case "$1" in ''|*[!0-9a-f-]*) return 1 ;; esac
        return 0
    }
    # A JSON string body: backslash, quote and every control character escaped
    # (\n \r \t, \u00XX for the rest). The trailing newline printf adds is
    # the record terminator of the last line, so a value's own newlines —
    # including a trailing one — survive as \n.
    json_str() {
        printf '%s\n' "$1" | LC_ALL=C awk '
            BEGIN { for (i = 1; i < 32; i++) m[sprintf("%c", i)] = sprintf("\\u%04x", i)
                    m["\r"] = "\\r"; m["\t"] = "\\t"; m["\\"] = "\\\\"; m["\""] = "\\\"" }
            { if (NR > 1) printf "\\n"
              n = length($0)
              for (i = 1; i <= n; i++) { c = substr($0, i, 1); printf "%s", ((c in m) ? m[c] : c) } }'
    }
    agent_psql() {
        PGPASSWORD="${POSTGRES_PASSWORD}" docker exec -e PGPASSWORD postgres psql \
            -U "${POSTGRES_USER}" -d agent_manager_db -tAc "$1"
    }
    # "group|volume|<mount as a JSON string>" per volume the row records
    # (#2518, migration 14). The mount comes JSON-encoded by Postgres, so a
    # newline in it can never split a line. Empty output = nothing recorded
    # (NULL); non-zero = unreadable.
    agent_db_volumes() {
        agent_psql "SELECT g.key || '|' || v.key || '|' || to_json(v.value)::text
            FROM agent_instances i
            CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(i.mounted_volumes) = 'object'
                                               THEN i.mounted_volumes ELSE '{}'::jsonb END) g
            CROSS JOIN LATERAL jsonb_each_text(CASE WHEN jsonb_typeof(g.value) = 'object'
                                                    THEN g.value ELSE '{}'::jsonb END) v
            WHERE i.id = '$1' AND g.key IN ('primary', 'companion')
              AND v.key <> '' AND COALESCE(v.value, '') <> ''
            ORDER BY g.key, v.key;"
    }
    # "group|volume|<mount as a JSON string>" per Type=volume mount of the
    # instance's containers.
    # Non-zero = docker cannot tell (never read as "mounts nothing").
    agent_docker_volumes() {
        _adv_names=$(docker ps -a --filter "label=${AGENT_INSTANCE_LABEL}=$1" --format '{{.Names}}') || return 1
        # exact-name fallback for a container that predates the label — unless
        # it is labelled for a DIFFERENT instance.
        if _adv_lbl=$(docker inspect --type container --format "{{index .Config.Labels \"${AGENT_INSTANCE_LABEL}\"}}" "$2" 2>/dev/null); then
            case "$_adv_lbl" in ''|'<no value>'|"$1") _adv_names="$_adv_names
$2" ;; esac
        fi
        for _adv_c in $(printf '%s\n' "$_adv_names" | LC_ALL=C sort -u); do
            _adv_comp=$(docker inspect --type container --format "{{index .Config.Labels \"${AGENT_COMPANION_LABEL}\"}}" "$_adv_c") || return 1
            _adv_grp=primary
            if [ "$_adv_comp" = "true" ]; then _adv_grp=companion; fi
            _adv_m=$(docker inspect --type container --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}|{{json .Destination}}{{"\n"}}{{end}}{{end}}' "$_adv_c") || return 1
            printf '%s\n' "$_adv_m" | sed -n "s/^\(..*|..*\)\$/${_adv_grp}|\1/p"
        done
    }

    # This run's archives replace the previous run's WHOLE agent tree (the
    # db-dumps volume persists between runs): a deleted instance's archive, or
    # a 2026.09-ga.3 `<slug>/<type>/` tree, must not ride along into this backup and
    # be restored as if it were current.
    # Never through a symlink: rm -rf would then empty whatever it points at.
    if [ -L "$AGENT_DUMP_DIR" ]; then
        echo "  WARNING: $AGENT_DUMP_DIR is a symlink — the previous agent tree is NOT cleared."
        AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
    else
        for _old in "$AGENT_DUMP_DIR"/*; do
            if [ -d "$_old" ] && [ ! -L "$_old" ]; then rm -rf "$_old"; fi
        done
    fi

    # Resolve the host name of the db-dumps volume — needed by `docker run`
    # because path-mounts of /backup/databases would interpret as host
    # paths (which don't exist; they're inside this backup-service container).
    # Using the named volume directly works regardless of Docker rootfs layout.
    DB_DUMPS_VOL=$(docker inspect "$(hostname)" --format '{{range .Mounts}}{{if eq .Destination "/backup/databases"}}{{.Name}}{{end}}{{end}}' 2>/dev/null)
    if [ -z "$DB_DUMPS_VOL" ]; then
        echo "  WARNING: could not resolve db-dumps volume name; falling back to local tar (won't make the backup)."
        DB_DUMPS_VOL=""
    fi

    # Rows: id|container_name|user_slug|agent_type, in id order.
    AGENT_ROWS_OK=true
    INSTANCE_ROWS=$(agent_psql "SELECT id::text || '|' || container_name || '|' || user_slug || '|' || agent_type FROM agent_instances WHERE state != 'destroyed' ORDER BY id;" 2>/dev/null) || AGENT_ROWS_OK=false

    if [ "$AGENT_ROWS_OK" != "true" ]; then
        # CRITICAL: agent-manager runs, so there are agents to lose — a backup
        # without them must fail loudly, not ship as a success (#2531 review).
        echo "ERROR: could not read agent_instances — per-user agent volumes NOT backed up."
        AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
        CRITICAL_FAILURES=$((CRITICAL_FAILURES + 1))
    elif [ -z "$INSTANCE_ROWS" ]; then
        echo "  No active agent instances; nothing to back up."
    else
        for ROW in $INSTANCE_ROWS; do
            INSTANCE_ID="${ROW%%|*}"
            REST="${ROW#*|}"
            CONTAINER_NAME="${REST%%|*}"
            REST="${REST#*|}"
            SLUG="${REST%|*}"
            TYPE="${REST#*|}"
            if ! agent_instance_id_ok "$INSTANCE_ID"; then
                echo "  SKIP: instance row with an unusable id '$INSTANCE_ID'"
                AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                continue
            fi
            AGENT_INSTANCE_COUNT=$((AGENT_INSTANCE_COUNT + 1))
            INSTANCE_DIR="$AGENT_DUMP_DIR/$INSTANCE_ID"
            mkdir -p "$INSTANCE_DIR"

            # Which volumes: the row's record, else the containers' Mounts.
            VOL_SOURCE=mounted_volumes
            if ! VOL_LINES=$(agent_db_volumes "$INSTANCE_ID" 2>/dev/null); then
                VOL_LINES=""
            fi
            if [ -z "$VOL_LINES" ]; then
                VOL_SOURCE=docker
                if ! VOL_LINES=$(agent_docker_volumes "$INSTANCE_ID" "$CONTAINER_NAME" 2>/dev/null); then
                    echo "  WARNING: $CONTAINER_NAME ($INSTANCE_ID): no mounted_volumes recorded and docker cannot list its mounts — NOT backed up (never guessed from a name prefix)."
                    AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                    VOL_LINES=""
                fi
            fi
            # One entry per volume name, in name order.
            VOL_LINES=$(printf '%s\n' "$VOL_LINES" | grep -v '^$' | LC_ALL=C sort -t '|' -k2,2 -u || true)
            if [ -z "$VOL_LINES" ]; then
                echo "  WARNING: $CONTAINER_NAME ($INSTANCE_ID): no mounted volumes known — nothing backed up for it."
                AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
            fi

            # M030 S4: WAL-checkpoint best-effort flush for SQLite-backed
            # agents (moltis) before snapshotting their volume. Without
            # this the tarball can land mid-transaction and the restore
            # then loses the last ~seconds of in-flight chat state. We
            # try the standard moltis db path; we ignore errors because
            # (a) the container may not have sqlite3, (b) the path may
            # differ on a future moltis version, (c) the file may not
            # exist yet on a fresh instance. In any of those cases the
            # subsequent tar still captures what's on disk.
            if container_running "$CONTAINER_NAME" && [ "$TYPE" = "moltis" ]; then
                docker exec "$CONTAINER_NAME" sh -c '
                    if command -v sqlite3 >/dev/null 2>&1; then
                        for DB in /home/moltis/.moltis/moltis.db /home/moltis/.moltis/instances/default/db.sqlite; do
                            if [ -f "$DB" ]; then
                                sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
                            fi
                        done
                    fi
                ' >/dev/null 2>&1 || true
            fi

            MANIFEST_VOLS=""
            # One line per volume; a mount path is never split or globbed.
            OLD_IFS="$IFS"
            IFS='
'
            set -f
            for VOL_LINE in $VOL_LINES; do
                IFS="$OLD_IFS"
                ROLE="${VOL_LINE%%|*}"
                VOL_REST="${VOL_LINE#*|}"
                VOL="${VOL_REST%%|*}"
                MOUNT="${VOL_REST#*|}"
                # Both sources hand the mount over JSON-encoded; anything else
                # is encoded here.
                case "$MOUNT" in
                    \"*\") MOUNT_JSON="$MOUNT" ;;
                    *) MOUNT_JSON="\"$(json_str "$MOUNT")\"" ;;
                esac
                ARCHIVED=false
                # #2709: why this volume is not archived, recorded in the manifest
                # (skip_reason, size_mb, cap_mb / used_pct + cap_pct) and in a
                # SKIP-REASON log line the pre-backup status carries to `rzfz backup status`
                SKIP_REASON=""; SKIP_CAP_MB=""; SKIP_USED_PCT=""; SKIP_CAP_PCT=""
                VOL_MB=""; VOL_CORES=""; VOL_CACHE_MB=""
                if ! agent_volume_name_ok "$VOL"; then
                    echo "  SKIP: '$VOL' of $CONTAINER_NAME is not an agent volume name"
                    AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                    continue
                fi
                # Use BusyBox alpine + tar in a spawned container.
                # Mount the agent volume read-only at /data and the
                # db-dumps named volume read-write at /dumps. Output path
                # inside /dumps: /dumps/agents/<instance_id>/<volume>.tar.
                # No gzip — offen recompresses the whole archive;
                # double-gzip would just bloat CPU.
                if [ -z "$DB_DUMPS_VOL" ]; then
                    echo "  SKIP: $VOL (no db-dumps volume to write into)"
                    SKIP_REASON=no_dump_volume
                    AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                elif ! docker volume inspect "$VOL" >/dev/null 2>&1; then
                    # BEFORE any probe: `docker run -v "$VOL:…"` would CREATE a
                    # missing volume, empty — archived as a success, and E1's
                    # "refusing to recreate onto empty volumes" guard defeated.
                    # The backup never changes volume state (#2531 review).
                    echo "  WARNING: $VOL ($CONTAINER_NAME) does not exist or cannot be inspected — NOT backed up, and not created."
                    SKIP_REASON=missing_volume
                    AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                else
                    # Guard 1: disk-usage ceiling (re-checked per volume so a run
                    # that crosses the ceiling mid-loop stops adding to the disk).
                    USED_PCT=$(disk_used_pct)
                    # Guard 2: per-volume size cap. A coding-agent workspace bloated
                    # by build caches / runaway files is not critical data and must
                    # never be tarred whole into the shared backup volume (that is
                    # exactly what filled the host disk in the 2026-08 incident).
                    if [ -n "$USED_PCT" ] && [ "$USED_PCT" -ge "$AGENT_BACKUP_MAX_USED_PCT" ]; then
                        echo "  SKIP: $VOL (disk ${USED_PCT}% used >= ${AGENT_BACKUP_MAX_USED_PCT}% ceiling — protecting host; raise BACKUP_AGENT_MAX_USED_PCT to override)"
                        SKIP_REASON=disk_ceiling; SKIP_USED_PCT="$USED_PCT"; SKIP_CAP_PCT="$AGENT_BACKUP_MAX_USED_PCT"
                        AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                    elif [ -z "$AGENT_TAR_IMAGE" ]; then
                        echo "  SKIP: $VOL (no GNU tar helper image — see the GNU tar ERROR above)"
                        SKIP_REASON=no_tar_helper
                        AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                    # The size check counts what will be archived: allocated
                    # MiB (du) less the core dumps the archive leaves out
                    # (#3060). The helper prints "<MiB> <cores> <core MiB>",
                    # and nothing when du fails (then the check is skipped, as
                    # before).
                     elif VOL_DU=$(docker run --rm --tmpfs /var/lib/postgresql/data -e LC_ALL=C -v "$VOL:/data:ro" --user 0:0 --entrypoint sh "$AGENT_TAR_IMAGE" \
                            -c "$AGENT_CORE_DUMPS_SH"'
                                t=$(du -sk /data | cut -f1) || exit 1
                                case "$t" in ""|*[!0-9]*) exit 1 ;; esac
                                l=$(mktemp) || exit 1
                                core_dumps > "$l"
                                n=$(wc -l < "$l")
                                c=0
                                if [ "$n" -gt 0 ]; then
                                    c=$(tr "\n" "\0" < "$l" | du -kc --files0-from=- | tail -n 1 | cut -f1)
                                fi
                                rm -f "$l"
                                case "$c" in ""|*[!0-9]*) c=0 ;; esac
                                k=$(cache_dirs | tr "\n" "\0" | du -skc --files0-from=- 2>/dev/null | tail -n 1 | cut -f1)
                                case "$k" in ""|*[!0-9]*) k=0 ;; esac
                                echo "$(( (t - c - k + 1023) / 1024 )) $n $(( (c + 1023) / 1024 )) $(( (k + 1023) / 1024 ))"' \
                            2>/dev/null | awk 'NF == 4 {print $1+0, $2+0, $3+0, $4+0}') \
                         && VOL_MB=${VOL_DU%% *} && VOL_CACHE_MB=${VOL_DU##* } \
                         && VOL_CORES=${VOL_DU#* } && VOL_CORES=${VOL_CORES% *} \
                         && [ -n "$VOL_MB" ] && [ "$VOL_MB" -gt "$AGENT_VOL_MAX_MB" ]; then
                        echo "  SKIP: $VOL (allocated ${VOL_MB} MiB on disk (du -sm) > ${AGENT_VOL_MAX_MB} MiB cap — likely runaway workspace; not backing up to protect host disk. Raise BACKUP_AGENT_VOL_MAX_MB to override)"
                        SKIP_REASON=size_cap; SKIP_CAP_MB="$AGENT_VOL_MAX_MB"
                        AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                    else
                        case "$VOL_CORES" in
                            [1-9]*\ *) echo "  NOTE: $VOL: ${VOL_CORES%% *} core dump file(s), ${VOL_CORES#* } MiB, are left out of the archive — crash dumps, not user data. The owner can delete them (files named core or core.<pid> in the workspace)." ;;
                        esac
                        case "$VOL_CACHE_MB" in
                            ""|0|*[!0-9]*) ;;
                            *) echo "  NOTE: $VOL: caches (.cache, .npm/_cacache), ${VOL_CACHE_MB} MiB, are left out of the archive — rebuilt by the tools that use them." ;;
                        esac
                        # Guard 3: hard-cap the tar OUTPUT size via `ulimit -f`, so a
                        # workspace that is small at check-time (Guard 2 passed) but
                        # BALLOONS DURING archiving can't fill the host. This is the
                        # real 2026-08 mechanism: a live coding agent's dev-server was
                        # appending to files while busybox tar read them, so tar of a
                        # ~2.5 GB workspace ran away to 636 GB. ulimit -f caps the file
                        # the tar process may write; on overflow tar dies (SIGXFSZ),
                        # the run exits non-zero, and the partial tar is removed below.
                        # ulimit -f is in 512-byte blocks in dash (the helper's
                        # /bin/sh) → MiB × 2048.
                        # Cap = 2× the per-volume size cap (headroom for a legit
                        # near-cap volume + tar overhead; still bounds any runaway).
                        OUT_CAP_BLOCKS=$(( AGENT_VOL_MAX_MB * 2 * 2048 ))
                        # #2544: GNU tar with -S (sparse; see AGENT_TAR_IMAGE above).
                        # --numeric-owner: the helper's passwd is not the agent's.
                        # The helper's stderr is KEPT: it is the only place the
                        # reason for a failed snapshot exists.
                        # #2584: tar writes into the staging dir (/dumps is the
                        # db-dumps volume, /dumps/.staging is STAGING_DIR) and the
                        # archive is renamed to agents/<id>/<vol>.tar only once tar
                        # is done with it — exit 0, or 1 (files changed, kept; see
                        # below). Anything else removes it. Exit 2 = setup failed.
                        if SNAP_ERR=$(docker run --rm \
                            --tmpfs /var/lib/postgresql/data \
                            -e LC_ALL=C \
                            -v "$VOL:/data:ro" \
                            -v "$DB_DUMPS_VOL:/dumps" \
                            --user 0:0 \
                            --entrypoint sh \
                            "$AGENT_TAR_IMAGE" \
                            -c "$AGENT_CORE_DUMPS_SH"'ulimit -f "$1"
                                umask 077
                                mkdir -p "/dumps/agents/$2" /dumps/.staging || exit 2
                                chmod 0700 /dumps/.staging "/dumps/agents/$2" || exit 2
                                t="/dumps/.staging/$2.$3.tar"
                                x=$(mktemp) || exit 2
                                { core_dumps; cache_dirs; } | while IFS= read -r f; do
                                    printf "./%s\n" "${f#/data/}"
                                done > "$x"
                                tar -C /data --numeric-owner -S --anchored --no-wildcards -X "$x" -cf "$t" .
                                rc=$?
                                rm -f "$x"
                                if [ "$rc" -le 1 ]; then
                                    mv -f "$t" "/dumps/agents/$2/$3.tar" || { rm -f "$t"; exit 2; }
                                else
                                    rm -f "$t"
                                fi
                                exit "$rc"' \
                            sh "$OUT_CAP_BLOCKS" "$INSTANCE_ID" "$VOL" 2>&1 >/dev/null); then
                            SNAP_RC=0
                        else
                            SNAP_RC=$?
                        fi
                        # #2544 fix round 2: GNU tar exits 1 on create when a file
                        # changed or vanished while it was read ("file changed as we
                        # read it") — agents are NOT paused for the snapshot, so a
                        # busy agent does exactly that (measured on prod's live home
                        # volume). The archive is complete but for those files: it is
                        # KEPT and counted, with a WARNING naming them. Exit 2 (fatal)
                        # and 129-159 (killed by a signal) stay failures. The mkdir
                        # above exits 2, never 1, so a 1 is always tar's.
                        # #3164: exit 1 alone does not mean "changed as we read it" —
                        # BusyBox tar (a host whose own /bin/tar is BusyBox) exits 1
                        # on ANY error, e.g. a permission-denied read where GNU tar
                        # exits 2. The helper's stderr is the only place the reason
                        # exists, so the keep-branch requires GNU's own changed /
                        # vanished wording; any other rc-1 reason falls through to
                        # the failure branch below and is counted with its error line.
                        # #3164 review P2: the wording must be tar's own DIAGNOSTIC —
                        # the trailing message of a `tar: <file>: <message>` line —
                        # never the same words inside a filename. An unreadable file
                        # named "file changed as we read it" prints
                        # `tar: ./file changed as we read it: Cannot open: Permission
                        # denied`: the phrase mid-line, the real diagnostic at the end.
                        # Only a line ENDING in the changed/vanished wording keeps the
                        # archive; any other rc-1 reason (permission denied, ...) falls
                        # through to the failure branch and is counted.
                        if [ "$SNAP_RC" -eq 1 ] && [ -f "$INSTANCE_DIR/$VOL.tar" ] \
                           && printf '%s\n' "$SNAP_ERR" | grep -Eq '(^|:[[:space:]]*)(file changed as we read it|file removed before we read it|file vanished)[[:space:]]*$'; then
                            echo "  WARNING: $VOL: files changed or vanished while it was archived (the agent is running) — archived, but these entries may be inconsistent:"
                            printf '%s\n' "$SNAP_ERR" | grep -v '^[[:space:]]*$' | head -n 10 | sed 's/^/    tar: /'
                            SNAP_RC=0
                        fi
                        if [ "$SNAP_RC" -eq 0 ]; then
                            AGENT_VOLUME_COUNT=$((AGENT_VOLUME_COUNT + 1))
                            ARCHIVED=true
                        else
                            # Remove any partial tar so a failed (e.g. disk-full) run
                            # doesn't leave a huge half-written file occupying the disk.
                            SNAP_ERR_LINE=$(printf '%s\n' "$SNAP_ERR" | grep -v '^[[:space:]]*$' | head -n 1)
                            if [ "$SNAP_RC" -gt 128 ] && [ "$SNAP_RC" -lt 160 ]; then
                                # a signal leaves at most the shell's own words ("File
                                # size limit exceeded"); SIGXFSZ (25) is the ulimit -f cap
                                SNAP_ERR_LINE="tar killed by signal $((SNAP_RC - 128)) — output cap of $((AGENT_VOL_MAX_MB * 2)) MiB reached? (exit $SNAP_RC)${SNAP_ERR_LINE:+; $SNAP_ERR_LINE}"
                            fi
                            echo "  WARNING: failed to snapshot volume $VOL: ${SNAP_ERR_LINE:-the helper exited $SNAP_RC with no message} (removing any partial tar)"
                            SKIP_REASON=snapshot_failed; SKIP_CAP_MB="$AGENT_VOL_MAX_MB"
                            if [ -n "$SNAP_ERR" ]; then
                                printf '%s\n' "$SNAP_ERR" | head -n 5 | sed 's/^/    tar: /'
                            fi
                            rm -f "$INSTANCE_DIR/$VOL.tar" 2>/dev/null
                            AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                        fi
                    fi
                fi
                if [ -n "$MANIFEST_VOLS" ]; then MANIFEST_VOLS="$MANIFEST_VOLS,"; fi
                SKIP_JSON=""
                if [ "$ARCHIVED" != true ] && [ -n "$SKIP_REASON" ]; then
                    SKIP_JSON=", \"skip_reason\": \"$SKIP_REASON\""
                    case "$VOL_MB" in ''|*[!0-9]*) ;; *) SKIP_JSON="$SKIP_JSON, \"size_mb\": $VOL_MB" ;; esac
                    case "$SKIP_CAP_MB" in ''|*[!0-9]*) ;; *) SKIP_JSON="$SKIP_JSON, \"cap_mb\": $SKIP_CAP_MB" ;; esac
                    case "$SKIP_USED_PCT" in ''|*[!0-9]*) ;; *) SKIP_JSON="$SKIP_JSON, \"used_pct\": $SKIP_USED_PCT" ;; esac
                    case "$SKIP_CAP_PCT" in ''|*[!0-9]*) ;; *) SKIP_JSON="$SKIP_JSON, \"cap_pct\": $SKIP_CAP_PCT" ;; esac
                    echo "  SKIP-REASON: vol=$VOL reason=$SKIP_REASON size_mb=${VOL_MB:--} cap_mb=${SKIP_CAP_MB:--} used_pct=${SKIP_USED_PCT:--} cap_pct=${SKIP_CAP_PCT:--}"
                fi
                MANIFEST_VOLS="$MANIFEST_VOLS
    {\"name\": \"$VOL\", \"role\": \"$(json_str "$ROLE")\", \"mount\": $MOUNT_JSON, \"archive\": \"$VOL.tar\", \"archived\": $ARCHIVED$SKIP_JSON}"
            done
            set +f
            IFS="$OLD_IFS"

            # The manifest is what makes this the instance-keyed layout: the
            # restore puts every archived volume back under the name it was
            # taken from. Written last, via a temp file, so a manifest never
            # names an archive this run did not finish.
            {
                printf '{\n  "format": 2,\n'
                printf '  "instance_id": "%s",\n' "$INSTANCE_ID"
                printf '  "user_slug": "%s",\n' "$(json_str "$SLUG")"
                printf '  "agent_type": "%s",\n' "$(json_str "$TYPE")"
                printf '  "container_name": "%s",\n' "$(json_str "$CONTAINER_NAME")"
                printf '  "source": "%s",\n' "$VOL_SOURCE"
                printf '  "volumes": [%s\n  ]\n}\n' "$MANIFEST_VOLS"
            } > "$STAGING_DIR/$INSTANCE_ID.manifest.json" && mv -f "$STAGING_DIR/$INSTANCE_ID.manifest.json" "$INSTANCE_DIR/manifest.json"
        done
        echo "  Per-user agents: $AGENT_INSTANCE_COUNT instance(s), $AGENT_VOLUME_COUNT volume(s) tarred, $AGENT_FAIL_COUNT failure(s)."
        if [ "$AGENT_FAIL_COUNT" -gt 0 ]; then
            # #2544: never "success" in silence. Not a CRITICAL failure: that
            # makes offen abort the WHOLE backup, trading the agent volumes
            # for the databases too. The restore reports these volumes as
            # NOT restored (PARTIAL, #2531).
            echo "  ERROR: $AGENT_FAIL_COUNT per-user agent volume(s) are NOT in this backup (see the WARNING/SKIP lines above) — a restore of it will be PARTIAL for them."
        fi
        # Snapshot OWUI user.settings (UserValves incl. moltis API_KEY)
        # so a restored agent can re-attach to its existing key without
        # the user having to re-paste. Encrypted same way as .env.
        # BSB-04: use ENCRYPTION_PASS (re-read from .env above) rather
        # than the stale BACKUP_ENCRYPTION_PASSWORD process env var.
        if container_running "openwebui" && [ -n "${ENCRYPTION_PASS:-}" ]; then
            PGPASSWORD="${POSTGRES_PASSWORD}" docker exec -e PGPASSWORD postgres pg_dump \
                -U "${POSTGRES_USER}" -d openwebui_db \
                --table='"user"' --column-inserts --data-only \
                2>/dev/null | RZFZ_OSSL_PASS="$ENCRYPTION_PASS" openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
                    -out "$(stage_path "$AGENT_DUMP_DIR/owui-users.sql.enc")" \
                    -pass env:RZFZ_OSSL_PASS 2>/dev/null \
                && stage_publish "$AGENT_DUMP_DIR/owui-users.sql.enc" \
                && echo "  OWUI user.settings (UserValves) snapshot encrypted." \
                || { stage_discard "$AGENT_DUMP_DIR/owui-users.sql.enc"; echo "  WARNING: OWUI user snapshot failed."; }
        fi
    fi
fi

# ------------------------------------------------------------------------------
# 8. OpenBao: raft snapshot + its key material, encrypted (#2476, D9)
# ------------------------------------------------------------------------------
# OPENBAO-BACKUP-BEGIN
# The snapshot's keyring is sealed by this box's static seal key, so a snapshot
# alone opens nothing: the key material travels beside it, encrypted with the
# backup passphrase (the "encrypted escrow" half of D9; the operator's copy of
# the recovery key is `rzfz openbao show-recovery-key`). keys.tar.enc plus
# raft.snap is the whole vault; the backup passphrase is what protects it off
# the box. Restore: scripts/openbao-restore.sh <backup-dir>.
#
#   databases/openbao/raft.snap     `bao operator raft snapshot save`, as the
#                                   rzfz-backup AppRole (read on the snapshot only)
#   databases/openbao/keys.tar.enc  a tar relative to /openbao of the files the
#                                   restore needs (OB_ESCROW_*), aes-256-cbc,
#                                   pbkdf2, the passphrase on the ENVIRONMENT
#                                   (-pass env:), never on argv
#
# Both are read from the `openbao` server (uid 100), which mounts the keys and
# credentials read-only. The tar names an ALLOWLIST, never a directory: after an
# interrupted first boot the keys volume can hold root-only leftovers
# (init-output.json*, bootstrap-root.token, *.tmp) that uid 100 cannot read
# and that must never enter an escrow (an init answer holds the root token).
# A required file that is missing or unreadable fails this section LOUDLY
# (ERROR:, which makes pre-backup.status DEGRADED), and no keys.tar.enc is
# written. Every stage is checked on its own (no pipeline status is trusted):
# tar's exit code travels through a file, openssl's is its own, and the result
# is decrypted and listed before it is kept. Not a CRITICAL failure: that would
# make offen drop the WHOLE backup, the databases with it (the §7 rule).
#
# The snapshot is taken as latest.snap.tmp and renamed to latest.snap only when
# both files are in this backup: `rzfz status` reads latest.snap's age as "the
# last backup that carries the secrets store".
OB_DIR=/backup/databases/openbao
# Required: the seal key, the recovery key (D9's escrow), and the stack's
# AppRole credentials (a restored store accepts only its own). Optional: the
# markers, and svc-mcp-manager's credentials once the identity reconcile has
# minted them. Kept equal to openbao-init.sh's files and the restore's list
# (test_2476_openbao_backup_section.py).
OB_ESCROW_REQUIRED="keys/seal.key keys/recovery.key cred/config.role_id cred/config.secret_id cred/backup.role_id cred/backup.secret_id cred/portal.role_id cred/portal.secret_id"
OB_ESCROW_OPTIONAL="keys/recovery.shown keys/setup.done mcp-cred/role_id mcp-cred/secret_id"
# Runs inside the openbao container. Exit 3 (no output) when a required file is
# missing, empty or unreadable; otherwise tar's own exit code.
OB_ESCROW_TAR='cd /openbao || exit 3
list=""
for f in '"$OB_ESCROW_REQUIRED"'; do
    if [ ! -s "$f" ] || [ ! -r "$f" ]; then echo "required file /openbao/$f is missing, empty or unreadable" >&2; exit 3; fi
    list="$list $f"
done
for f in '"$OB_ESCROW_OPTIONAL"'; do
    if [ -e "$f" ]; then list="$list $f"; fi
done
exec tar cf - $list'
OB_LOGIN='export BAO_ADDR=http://127.0.0.1:8200; BAO_TOKEN=$(bao write -field=token auth/approle/login role_id=@/openbao/cred/backup.role_id secret_id=@/openbao/cred/backup.secret_id) && [ -n "$BAO_TOKEN" ] && export BAO_TOKEN'
# A previous run's files never ride along into this backup as if current.
rm -rf "$OB_DIR"
if ! container_running "openbao"; then
    _ob_profiles=""
    if [ -f "${DOT_ENV_PATH:-/scripts/dot-env}" ]; then
        _ob_profiles=$(grep -m1 '^COMPOSE_PROFILES=' "${DOT_ENV_PATH:-/scripts/dot-env}" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"' ' || true)
    fi
    case ",${_ob_profiles}," in
        *,openbao,*) echo "ERROR: the openbao profile is on but OpenBao is not running — this backup does NOT carry the secrets store." ;;
        *) echo "OpenBao not running — skipped." ;;
    esac
else
    mkdir -p "$OB_DIR"
    _ob_snap_ok=false
    _ob_keys_ok=false
    # #2705: the failure's own words go on the ERROR line — the backup report
    # (and an upgrade stopped by it) shows only such lines, and "Code: 503"
    # (OpenBao sealed or not ready yet) is the cause an operator must read.
    _ob_snap_err=$(mktemp)
    if docker exec openbao sh -c "$OB_LOGIN"' || exit 1; rc=0; bao operator raft snapshot save /openbao/snapshots/latest.snap.tmp || rc=$?; bao token revoke -self >/dev/null 2>&1; exit $rc' 2>"$_ob_snap_err" \
       && docker exec openbao cat /openbao/snapshots/latest.snap.tmp 2>>"$_ob_snap_err" > "$OB_DIR/raft.snap.tmp" \
       && [ -s "$OB_DIR/raft.snap.tmp" ] && mv -f "$OB_DIR/raft.snap.tmp" "$OB_DIR/raft.snap"; then
        _ob_snap_ok=true
        echo "OpenBao raft snapshot saved."
    else
        rm -f "$OB_DIR/raft.snap.tmp"
        _ob_snap_why=$(grep -m1 -oE 'Code: [0-9]+' "$_ob_snap_err" 2>/dev/null || true)
        _ob_snap_msg=$(grep -m1 -E '^[[:space:]]*\* ' "$_ob_snap_err" 2>/dev/null | sed 's/^[[:space:]]*\* //' || true)
        if [ -z "$_ob_snap_why" ]; then
            _ob_snap_why=$(grep -v '^[[:space:]]*$' "$_ob_snap_err" 2>/dev/null | head -n 1 || true)
        elif [ -n "$_ob_snap_msg" ]; then
            _ob_snap_why="OpenBao answered ${_ob_snap_why#Code: }: ${_ob_snap_msg}"
        else
            _ob_snap_why="OpenBao answered ${_ob_snap_why#Code: }"
        fi
        echo "ERROR: OpenBao raft snapshot FAILED${_ob_snap_why:+ (${_ob_snap_why})} — this backup does NOT carry the secrets store."
    fi
    cat "$_ob_snap_err" >&2 2>/dev/null || true
    rm -f "$_ob_snap_err"
    if [ -z "${ENCRYPTION_PASS:-}" ]; then
        echo "ERROR: no encryption password — OpenBao keys NOT backed up (a restore cannot open this backup's OpenBao snapshot)."
    else
        _ob_rc=$(mktemp)
        _ob_err=$(mktemp)
        # tar's exit code goes through a file; openssl's is the `if`'s own.
        if { _r=0; docker exec openbao sh -c "$OB_ESCROW_TAR" 2>"$_ob_err" || _r=$?; echo "$_r" > "$_ob_rc"; } \
                | ENCRYPTION_PASS="$ENCRYPTION_PASS" openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
                    -out "$OB_DIR/keys.tar.enc.tmp" -pass env:ENCRYPTION_PASS 2>/dev/null; then
            _ob_ssl=0
        else
            _ob_ssl=1
        fi
        _ob_tar=$(cat "$_ob_rc" 2>/dev/null || echo 1)
        _ob_why=$(head -n 1 "$_ob_err" 2>/dev/null || true)
        rm -f "$_ob_rc" "$_ob_err"
        # The kept file must decrypt and name every required file.
        _ob_list=""
        _ob_missing=""
        if [ "$_ob_tar" = 0 ] && [ "$_ob_ssl" = 0 ]; then
            _ob_list=$(ENCRYPTION_PASS="$ENCRYPTION_PASS" openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
                -in "$OB_DIR/keys.tar.enc.tmp" -pass env:ENCRYPTION_PASS 2>/dev/null | tar tf - 2>/dev/null) || _ob_list=""
            for _f in $OB_ESCROW_REQUIRED; do
                printf '%s\n' "$_ob_list" | grep -qxF "$_f" || _ob_missing="$_ob_missing $_f"
            done
        fi
        if [ "$_ob_tar" = 0 ] && [ "$_ob_ssl" = 0 ] && [ -n "$_ob_list" ] && [ -z "$_ob_missing" ]; then
            mv -f "$OB_DIR/keys.tar.enc.tmp" "$OB_DIR/keys.tar.enc"
            _ob_keys_ok=true
            echo "OpenBao keys encrypted (seal key, recovery key, AppRole credentials)."
        else
            rm -f "$OB_DIR/keys.tar.enc.tmp"
            echo "ERROR: OpenBao keys NOT backed up (tar exit ${_ob_tar}, openssl exit ${_ob_ssl}${_ob_missing:+, missing:$_ob_missing}${_ob_why:+: $_ob_why}) — a restore cannot open this backup's OpenBao snapshot."
        fi
    fi
    if [ "$_ob_snap_ok" = true ] && [ "$_ob_keys_ok" = true ]; then
        docker exec openbao mv -f /openbao/snapshots/latest.snap.tmp /openbao/snapshots/latest.snap \
            || echo "WARNING: could not mark the OpenBao snapshot as backed up (rzfz status will show it as old)."
    fi
fi
# OPENBAO-BACKUP-END

# ------------------------------------------------------------------------------
# 9. Space preflight (#2833): the LAST step, so the sources are complete
# ------------------------------------------------------------------------------
# offen prunes only after a successful run, so a run that fails for space keeps
# every later one failing too (#2833). archive-guard.sh frees
# space first (unfinished archives, then archives past the retention) and stops
# the run here, before offen writes a partial archive, when it is still short.
ARCHIVE_GUARD="${RZFZ_ARCHIVE_GUARD:-/scripts/archive-guard.sh}"
if [ -f "$ARCHIVE_GUARD" ]; then
    if ! sh "$ARCHIVE_GUARD" preflight; then
        echo "CRITICAL: not enough space on the backup disk for this run (see the archive-guard lines above) — the run is stopped before it writes an archive."
        CRITICAL_FAILURES=$((CRITICAL_FAILURES + 1))
    fi
else
    # Not a WARNING (that would make every run DEGRADED until the recreate):
    # the container predates the mount, and the next recreate brings it.
    echo "archive-guard: $ARCHIVE_GUARD is not mounted — no space preflight for this run (recreate backup-service to get it)."
fi

# F-052: Exit non-zero if any critical failures occurred (triggers backup failure notification)
if [ "$CRITICAL_FAILURES" -gt 0 ]; then
    echo "ERROR: Pre-backup completed with $CRITICAL_FAILURES critical failure(s)."
    exit 1
fi

echo "Pre-Backup steps completed."
