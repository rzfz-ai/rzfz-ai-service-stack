#!/bin/bash
# ==============================================================================
# OpenLIT ClickHouse schema init (razzfazz observability profile)
# ==============================================================================
# Runs on EVERY ClickHouse startup (CLICKHOUSE_ALWAYS_RUN_INITDB_SCRIPTS=true).
# Three responsibilities:
#   1. Vendor the upstream OpenLIT schema init from
#      https://github.com/openlit/openlit/blob/main/assets/clickhouse-init.sh
#      (TODO: drop the actual upstream script content above this line).
#   2. Re-apply / converge the retention TTL on the OpenLIT span/trace tables.
#      Default retention is 7 days, configurable via OBSERVABILITY_RETENTION_DAYS.
#   3. Reclaim orphaned system-log generations that ClickHouse renamed aside
#      when a system log's schema changed (#2270).
#
# NB: this is the BELT, not the primary. The primary retention guard is the
# otel-collector's ClickHouse exporter `ttl:` (configs/otel-collector-config.yaml),
# which bakes a TTL into the CREATE TABLE at first write — race-free, no restart
# dependency. This script runs from docker-entrypoint-initdb.d at container
# start, i.e. BEFORE the exporter has created the tables on a fresh box, so its
# ALTER hits non-existent tables on that first boot (swallowed below). It only
# actually applies on a LATER restart once the tables exist. That single-shot,
# restart-gated behaviour is exactly why retention silently never took effect on
# a long-running box before the exporter-side fix (the store grew to ~130 GiB).
# Here it converges the TTL expression to the days form the acceptance test
# matches (toIntervalDay(N)) and MATERIALIZEs it to reclaim any rows written
# before a TTL existed.
#
# This is currently a STUB — only the TTL section is razzfazz-specific. Vendor
# the real schema-init script before first deploy, or OpenLIT will fall back
# to whatever schema bootstrap it does internally on first connection (which
# may also work, depending on version).
# ==============================================================================

set -e

DAYS="${OBSERVABILITY_RETENTION_DAYS:-7}"

# TODO: paste the upstream OpenLIT clickhouse-init.sh content here, BEFORE the
# TTL section. The upstream script creates the openlit database, the otel_*
# tables, the materialized views, etc.
#
# Until that's done, this stub only applies the TTL — assuming the openlit
# DB and tables already exist (created by OpenLIT itself on first connection).

# Build auth args; clickhouse-client rejects `--password=` with an empty value
# (it expects the value glued to the equals sign), so omit the flag entirely
# when CLICKHOUSE_PASSWORD is unset/empty.
auth_args=(--user="${CLICKHOUSE_USER:-default}")
if [ -n "${CLICKHOUSE_PASSWORD}" ]; then
    auth_args+=(--password="${CLICKHOUSE_PASSWORD}")
fi

clickhouse-client "${auth_args[@]}" \
    --query "CREATE DATABASE IF NOT EXISTS openlit"

# Converge TTL on span/trace tables (idempotent). On a fresh box the tables
# don't exist yet — the exporter's `ttl:` bakes retention in at create, so a
# miss here is harmless; the ALTER just re-asserts the days-form on a later
# restart. When the ALTER succeeds, MATERIALIZE TTL actively prunes rows that
# predate the TTL (MODIFY TTL alone only affects future merges), so a table
# that grew before retention was in force is reclaimed on the next restart.
for tbl in otel_traces otel_logs otel_metrics; do
    if clickhouse-client "${auth_args[@]}" \
        --query "ALTER TABLE openlit.${tbl} MODIFY TTL Timestamp + INTERVAL ${DAYS} DAY" \
        2>/dev/null; then
        clickhouse-client "${auth_args[@]}" \
            --query "ALTER TABLE openlit.${tbl} MATERIALIZE TTL" 2>/dev/null \
            || echo "razzfazz-init: openlit.${tbl} MATERIALIZE TTL deferred (runs on next merge)"
    else
        echo "razzfazz-init: openlit.${tbl} TTL deferred (table not present yet; exporter bakes TTL at create)"
    fi
done

# ---------------------------------------------------------------------------
# #2270: reclaim ORPHANED system-log generations.
#
# Whenever a system log's schema changes, ClickHouse does not migrate the table
# - it renames the existing one aside (system.text_log -> system.text_log_0,
# then _1, _2, ...) and starts a fresh one. Every razzfazz config change that
# touches a system log's TTL or partition key therefore leaves a generation
# behind. Nothing ever reads those tables and nothing ever reclaims them: they
# are not covered by the TTL (the TTL lives on the CURRENT table) and they are
# invisible to a size query that looks at `text_log` by name.
#
# Measured on the demo box 2026-09-17: FOUR generations present (text_log plus
# _0, _1, _2). Drop them on every start - this script runs on each container
# start via CLICKHOUSE_ALWAYS_RUN_INITDB_SCRIPTS.
#
# Deliberately narrow: only `system`, only names ending `_log_<digits>`, which
# is exactly ClickHouse's rename-aside form. A live system log never carries a
# numeric suffix, so the current tables can never match. Failures are swallowed
# - reclaiming disk must never keep the observability stack from starting.
# ---------------------------------------------------------------------------
orphans=$(clickhouse-client "${auth_args[@]}" --query \
    "SELECT name FROM system.tables
      WHERE database = 'system' AND match(name, '^[a-z_]+_log_[0-9]+$')
      ORDER BY name FORMAT TabSeparated" 2>/dev/null || true)

if [ -n "${orphans}" ]; then
    while read -r orphan; do
        [ -n "${orphan}" ] || continue
        if clickhouse-client "${auth_args[@]}" \
            --query "DROP TABLE IF EXISTS system.\`${orphan}\` SYNC" 2>/dev/null; then
            echo "razzfazz-init: dropped orphaned system-log generation system.${orphan}"
        else
            echo "razzfazz-init: could not drop system.${orphan} (left in place)"
        fi
    done <<< "${orphans}"
else
    echo "razzfazz-init: no orphaned system-log generations to reclaim"
fi

echo "razzfazz-init: ClickHouse OpenLIT init complete (retention=${DAYS}d)"
