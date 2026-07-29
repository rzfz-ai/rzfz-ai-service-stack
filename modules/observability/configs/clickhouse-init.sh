#!/bin/bash
# ==============================================================================
# OpenLIT ClickHouse schema init (razzfazz observability profile)
# ==============================================================================
# Runs once on first ClickHouse startup (CLICKHOUSE_ALWAYS_RUN_INITDB_SCRIPTS).
# Two responsibilities:
#   1. Vendor the upstream OpenLIT schema init from
#      https://github.com/openlit/openlit/blob/main/assets/clickhouse-init.sh
#      (TODO: drop the actual upstream script content above this line).
#   2. Apply a TTL to the OpenLIT span/trace tables so old data auto-prunes.
#      Default retention is 30 days, configurable via OBSERVABILITY_RETENTION_DAYS.
#
# This is currently a STUB — only the TTL section is razzfazz-specific. Vendor
# the real schema-init script before first deploy, or OpenLIT will fall back
# to whatever schema bootstrap it does internally on first connection (which
# may also work, depending on version).
# ==============================================================================

set -e

DAYS="${OBSERVABILITY_RETENTION_DAYS:-30}"

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

# Apply TTL to span/trace tables (idempotent; ignored if tables don't exist yet,
# in which case OpenLIT's own bootstrap creates them and the TTL is applied on
# the next ClickHouse restart when this script re-runs).
for tbl in otel_traces otel_logs otel_metrics; do
    clickhouse-client "${auth_args[@]}" \
        --query "ALTER TABLE openlit.${tbl} MODIFY TTL Timestamp + INTERVAL ${DAYS} DAY" \
        2>/dev/null || echo "razzfazz-init: openlit.${tbl} TTL set later (table not yet present)"
done

echo "razzfazz-init: ClickHouse OpenLIT init complete (retention=${DAYS}d)"
