#!/bin/bash
# ==============================================================================
# OpenLIT ClickHouse schema init (razzfazz observability profile)
# ==============================================================================
# Runs once on first ClickHouse startup (CLICKHOUSE_ALWAYS_RUN_INITDB_SCRIPTS).
# Two responsibilities:
#   1. Vendor the upstream OpenLIT schema init from
#      https://github.com/openlit/openlit/blob/main/assets/clickhouse-init.sh
#      (TODO: drop the actual upstream script content above this line).
#   2. Re-apply / converge the retention TTL on the OpenLIT span/trace tables.
#      Default retention is 7 days, configurable via OBSERVABILITY_RETENTION_DAYS.
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

echo "razzfazz-init: ClickHouse OpenLIT init complete (retention=${DAYS}d)"
