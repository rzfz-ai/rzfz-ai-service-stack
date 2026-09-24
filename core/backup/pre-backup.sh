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
    echo "WARNING: root disk ${_root_used}% used (>= ${BACKUP_ROOT_DISK_WARN_PCT}%) — a nightly archive run on a nearly-full disk is how both prod outages started (#139)."
    echo "         Free space soon: 'docker builder prune -af', prune old backups, check /var/lib/docker/volumes. Threshold: BACKUP_ROOT_DISK_WARN_PCT."
fi

# F-052: Warn if no notification URLs are configured
if [ -z "${NOTIFICATION_URLS:-}" ]; then
    echo "WARNING: NOTIFICATION_URLS is not configured. Backup failures will not trigger external alerts."
    echo "Set BACKUP_NOTIFICATION_URLS in .env to enable (e.g. ntfy://topic, smtp://..., slack://...)."
fi

echo "Starting Pre-Backup Database Dump..."
mkdir -p /backup/databases

# Helper function for exact container name matching
container_running() {
    docker ps --format '{{.Names}}' | grep -q "^$1$"
}

# ------------------------------------------------------------------------------
# 1. Postgres Core
# ------------------------------------------------------------------------------
# #134: lightrag moved from Apache AGE to NetworkX storage. An orphaned `age`
# extension row left in lightrag_db (whose $libdir/age the image no longer ships)
# makes the next `pg_dumpall` ABORT → a silently incomplete, non-restorable
# backup. Drop it idempotently before the cluster dump (no-op when absent).
if container_running "postgres"; then
    docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres psql -U "${POSTGRES_USER}" \
        -d lightrag_db -c "DROP EXTENSION IF EXISTS age CASCADE;" >/dev/null 2>&1 || true
fi
echo "Backing up Postgres Core (Container: postgres)..."
if container_running "postgres"; then
    # Use docker exec with -e for PGPASSWORD (isolated in container environment)
    if docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres pg_dumpall -U "${POSTGRES_USER}" > /backup/databases/postgres_core.sql; then
        echo "Postgres Core backup successful."
    else
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
echo "Backing up Postgres Komodo (Container: postgres-komodo)..."
# Check if the monitoring profile is active (container exists)
if container_running "postgres-komodo"; then
    if docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres-komodo pg_dumpall -U "${POSTGRES_USER}" > /backup/databases/postgres_komodo.sql; then
        echo "Postgres Komodo backup successful."
    else
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
    # Use REDISCLI_AUTH environment variable instead of -a flag to avoid password in process list
    if docker exec -e REDISCLI_AUTH="${VALKEY_PASSWORD}" valkey valkey-cli SAVE; then
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
    # #137: only pass --password when CLICKHOUSE_PASSWORD is non-empty. The
    # ClickHouse default user has no password, in which case the env var is
    # empty; `clickhouse-client --password=` (empty after the `=`) is REJECTED
    # by the client with "the argument for option '--password' should follow
    # immediately after the equal sign", failing every backup on a
    # default-no-password deployment. Build the flag conditionally instead.
    CH_PW_ARG=""
    if [ -n "${CLICKHOUSE_PASSWORD:-}" ]; then
        CH_PW_ARG="--password=${CLICKHOUSE_PASSWORD}"
    fi
    # shellcheck disable=SC2086
    if docker exec -e CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-}" clickhouse \
        clickhouse-client --user="${CLICKHOUSE_USER:-default}" $CH_PW_ARG \
        --query "BACKUP DATABASE openlit TO Disk('backups', '${BACKUP_NAME}.zip')"; then
        echo "ClickHouse backup successful: ${BACKUP_NAME}.zip"
        # Keep only the 7 most recent daily backups inside the volume
        docker exec clickhouse sh -c "cd /var/lib/clickhouse/backups && ls -t daily-*.zip 2>/dev/null | tail -n +8 | xargs -r rm -f" 2>/dev/null || true
    else
        # #137: ClickHouse holds experimental, regenerable OpenLIT observability
        # telemetry — a failure here must NOT abort the whole backup (which
        # captures Postgres, .env, and per-user agent state). Log loudly but do
        # NOT increment CRITICAL_FAILURES.
        echo "WARNING: ClickHouse OpenLIT backup failed (non-critical — observability telemetry only; core data backup continues)."
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
    DROPPED=$(docker exec -e POSTGRES_USER="${POSTGRES_USER}" \
        -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
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
        if openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
            -in /scripts/dot-env -out /backup/databases/env-backup.enc \
            -pass "pass:${ENCRYPTION_PASS}" 2>/dev/null; then
            echo ".env encrypted successfully."
        else
            echo "ERROR: .env encryption failed."
        fi
    else
        echo "WARNING: /scripts/dot-env not mounted. Skipping .env encryption."
    fi

    # Encrypt .env.dify
    if [ -f /scripts/dot-env-dify ]; then
        if openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
            -in /scripts/dot-env-dify -out /backup/databases/env-dify-backup.enc \
            -pass "pass:${ENCRYPTION_PASS}" 2>/dev/null; then
            echo ".env.dify encrypted successfully."
        else
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
            > /backup/databases/stack-version.json
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
    if [ -d /scripts/certs ]; then
        if tar czf - --numeric-owner -C /scripts certs 2>/dev/null \
            | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
                -out /backup/databases/certs-backup.tar.gz.enc \
                -pass "pass:${ENCRYPTION_PASS}" 2>/dev/null; then
            echo "certs/ encrypted successfully."
        else
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
        if tar czf - --numeric-owner -C /scripts ${_bl_items} 2>/dev/null \
            | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
                -out /backup/databases/box-local-backup.tar.gz.enc \
                -pass "pass:${ENCRYPTION_PASS}" 2>/dev/null; then
            echo "box-local state encrypted successfully (${_bl_items# })."
        else
            echo "ERROR: box-local state encryption failed — this backup does NOT carry ${_bl_items# }."
        fi
    else
        echo "INFO: no box-local state mounted (.checksums.db / overlay). Skipping."
    fi

fi

# ------------------------------------------------------------------------------
# 7. M030-S4: per-user agent volume snapshots (default-include)
# ------------------------------------------------------------------------------
# Per-user agent volumes are named `agent-{type}-{slug}-{role}` (catalog
# `name_suffix` per S1). They're DYNAMIC — agent-manager creates them at
# provision time, so they can't be declared in compose.yml's static mount
# list like core volumes. Instead: walk agent_instances table → for each
# non-destroyed instance → docker run --rm an alpine container per volume
# to tar it into /backup/databases/agents/<slug>/<type>/<role>.tar.gz.
# offen/docker-volume-backup picks up /backup/databases (db-dumps volume)
# and includes our tarballs in the main backup archive.
#
# Layout inside the backup archive:
#   db-dumps/agents/instances.json            — snapshot of agent_instances
#   db-dumps/agents/<slug>/<type>/<role>.tar  — one tar per named volume
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
    #   * skip an individual volume whose apparent size exceeds this cap — a
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
    if docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres pg_dump \
        -U "${POSTGRES_USER}" -d agent_manager_db \
        --table=agent_instances --table=agent_types --data-only \
        > "$AGENT_DUMP_DIR/instances.sql" 2>/dev/null; then
        echo "  agent_instances + agent_types data dumped."
    else
        echo "  WARNING: agent_instances dump failed (continuing)."
    fi

    # For each running/stopped instance (skip destroyed), tar each named
    # volume. Volume names follow the convention from provisioner.py:
    # agent-{agent_type}-{user_slug}-{name_suffix}. We discover them by
    # listing the volumes that match `agent-` prefix, NOT by parsing the
    # catalog (catalog could be ahead of what's actually deployed).
    AGENT_INSTANCE_COUNT=0
    AGENT_VOLUME_COUNT=0
    AGENT_FAIL_COUNT=0

    # Resolve the host name of the db-dumps volume — needed by `docker run`
    # because path-mounts of /backup/databases would interpret as host
    # paths (which don't exist; they're inside this backup-service container).
    # Using the named volume directly works regardless of Docker rootfs layout.
    DB_DUMPS_VOL=$(docker inspect "$(hostname)" --format '{{range .Mounts}}{{if eq .Destination "/backup/databases"}}{{.Name}}{{end}}{{end}}' 2>/dev/null)
    if [ -z "$DB_DUMPS_VOL" ]; then
        echo "  WARNING: could not resolve db-dumps volume name; falling back to local tar (won't make the backup)."
        DB_DUMPS_VOL=""
    fi

    # Get rows: container_name|user_slug|agent_type from agent_instances.
    # CRITICAL: use container_name (pinned at provision time) to derive the
    # volume prefix, NOT a freshly-recomputed slug from user_slug. user_slug
    # in the DB is mutable in operator-edit cases (slug-stability bug Q3
    # in M030 ROADMAP — provisioner pins it on insert but post-insert SQL
    # UPDATEs are possible and have happened during pipe-debug sessions).
    # container_name = `agent-{type}-{slug-at-provision}` is the ground truth
    # because Docker won't let it change after creation. user_slug is kept
    # only for the backup PATH layout (slug/type/role) so backups are
    # human-readable; if it differs from the volume prefix, the backup
    # still captures the volumes correctly via container_name.
    INSTANCE_ROWS=$(docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres psql \
        -U "${POSTGRES_USER}" -d agent_manager_db -tAc \
        "SELECT container_name || '|' || user_slug || '|' || agent_type FROM agent_instances WHERE state != 'destroyed';" 2>/dev/null)

    if [ -z "$INSTANCE_ROWS" ]; then
        echo "  No active agent instances; nothing to back up."
    else
        for ROW in $INSTANCE_ROWS; do
            CONTAINER_NAME="${ROW%%|*}"
            REST="${ROW#*|}"
            SLUG="${REST%|*}"
            TYPE="${REST#*|}"
            AGENT_INSTANCE_COUNT=$((AGENT_INSTANCE_COUNT + 1))
            mkdir -p "$AGENT_DUMP_DIR/$SLUG/$TYPE"

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

            # Volume prefix = container_name + "-" (since provisioner names
            # volumes `agent-{type}-{slug-at-provision}-{role}` which equals
            # `{container_name}-{role}`). Decoupled from any post-provision
            # user_slug edits.
            VOL_PREFIX="${CONTAINER_NAME}-"
            for VOL in $(docker volume ls --format '{{.Name}}' --filter "name=^${VOL_PREFIX}"); do
                ROLE="${VOL#${VOL_PREFIX}}"
                # Use BusyBox alpine + tar in a spawned container.
                # Mount the agent volume read-only at /data and the
                # db-dumps named volume read-write at /dumps. Output path
                # inside /dumps mirrors the layout we expect:
                # /dumps/agents/<slug>/<type>/<role>.tar. No gzip — offen
                # recompresses the whole archive; double-gzip would just
                # bloat CPU.
                if [ -z "$DB_DUMPS_VOL" ]; then
                    echo "  SKIP: $VOL (no db-dumps volume to write into)"
                    AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                    continue
                fi
                # Guard 1: disk-usage ceiling (re-checked per volume so a run
                # that crosses the ceiling mid-loop stops adding to the disk).
                USED_PCT=$(disk_used_pct)
                if [ -n "$USED_PCT" ] && [ "$USED_PCT" -ge "$AGENT_BACKUP_MAX_USED_PCT" ]; then
                    echo "  SKIP: $VOL (disk ${USED_PCT}% used >= ${AGENT_BACKUP_MAX_USED_PCT}% ceiling — protecting host; raise BACKUP_AGENT_MAX_USED_PCT to override)"
                    AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                    continue
                fi
                # Guard 2: per-volume size cap. A coding-agent workspace bloated
                # by build caches / runaway files is not critical data and must
                # never be tarred whole into the shared backup volume (that is
                # exactly what filled the host disk in the 2026-08 incident).
                VOL_MB=$(docker run --rm -v "$VOL:/data:ro" --user 0:0 alpine:latest \
                    du -sm /data 2>/dev/null | awk '{print $1+0}')
                if [ -n "$VOL_MB" ] && [ "$VOL_MB" -gt "$AGENT_VOL_MAX_MB" ]; then
                    echo "  SKIP: $VOL (apparent ${VOL_MB} MiB > ${AGENT_VOL_MAX_MB} MiB cap — likely runaway workspace; not backing up to protect host disk. Raise BACKUP_AGENT_VOL_MAX_MB to override)"
                    AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                    continue
                fi
                # Guard 3: hard-cap the tar OUTPUT size via `ulimit -f`, so a
                # workspace that is small at check-time (Guard 2 passed) but
                # BALLOONS DURING archiving can't fill the host. This is the
                # real 2026-08 mechanism: a live coding agent's dev-server was
                # appending to files while busybox tar read them, so tar of a
                # ~2.5 GB workspace ran away to 636 GB. ulimit -f caps the file
                # the tar process may write; on overflow tar dies (SIGXFSZ),
                # the run exits non-zero, and the partial tar is removed below.
                # ulimit -f is in 512-byte blocks on busybox ash → MiB × 2048.
                # Cap = 2× the per-volume size cap (headroom for a legit
                # near-cap volume + tar overhead; still bounds any runaway).
                OUT_CAP_BLOCKS=$(( AGENT_VOL_MAX_MB * 2 * 2048 ))
                # `-S` (sparse): store sparse files compactly instead of
                # expanding holes to zero-bytes (another way a small volume
                # could balloon). Restore (`tar -x`) detects sparse members
                # transparently.
                if docker run --rm \
                    -v "$VOL:/data:ro" \
                    -v "$DB_DUMPS_VOL:/dumps" \
                    --user 0:0 \
                    alpine:latest \
                    sh -c "ulimit -f $OUT_CAP_BLOCKS; mkdir -p /dumps/agents/$SLUG/$TYPE && tar -C /data -Scf /dumps/agents/$SLUG/$TYPE/$ROLE.tar . 2>/dev/null" >/dev/null 2>&1; then
                    AGENT_VOLUME_COUNT=$((AGENT_VOLUME_COUNT + 1))
                else
                    # Remove any partial tar so a failed (e.g. disk-full) run
                    # doesn't leave a huge half-written file occupying the disk.
                    echo "  WARNING: failed to snapshot volume $VOL (removing any partial tar)"
                    rm -f "$AGENT_DUMP_DIR/$SLUG/$TYPE/$ROLE.tar" 2>/dev/null
                    AGENT_FAIL_COUNT=$((AGENT_FAIL_COUNT + 1))
                fi
            done
        done
        echo "  Per-user agents: $AGENT_INSTANCE_COUNT instance(s), $AGENT_VOLUME_COUNT volume(s) tarred, $AGENT_FAIL_COUNT failure(s)."
        # Snapshot OWUI user.settings (UserValves incl. moltis API_KEY)
        # so a restored agent can re-attach to its existing key without
        # the user having to re-paste. Encrypted same way as .env.
        # BSB-04: use ENCRYPTION_PASS (re-read from .env above) rather
        # than the stale BACKUP_ENCRYPTION_PASSWORD process env var.
        if container_running "openwebui" && [ -n "${ENCRYPTION_PASS:-}" ]; then
            docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres pg_dump \
                -U "${POSTGRES_USER}" -d openwebui_db \
                --table='"user"' --column-inserts --data-only \
                2>/dev/null | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
                    -out "$AGENT_DUMP_DIR/owui-users.sql.enc" \
                    -pass "pass:${ENCRYPTION_PASS}" 2>/dev/null \
                && echo "  OWUI user.settings (UserValves) snapshot encrypted." \
                || echo "  WARNING: OWUI user snapshot failed."
        fi
    fi
fi

# F-052: Exit non-zero if any critical failures occurred (triggers backup failure notification)
if [ "$CRITICAL_FAILURES" -gt 0 ]; then
    echo "ERROR: Pre-backup completed with $CRITICAL_FAILURES critical failure(s)."
    exit 1
fi

echo "Pre-Backup steps completed."
