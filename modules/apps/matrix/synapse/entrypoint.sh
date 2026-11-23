#!/bin/sh
# Synapse entrypoint: render homeserver.yaml template with envsubst, then start Synapse
set -e

CONFIG_TPL="/synapse-config/homeserver.yaml"
CONFIG_OUT="/data/homeserver.yaml"
LOG_CONFIG_TPL="/synapse-config/log.config"
LOG_CONFIG_OUT="/data/log.config"

# Render homeserver.yaml from template (expands ${VAR} env vars)
if [ -f "$CONFIG_TPL" ]; then
    envsubst < "$CONFIG_TPL" > "$CONFIG_OUT"
    echo "[synapse-entrypoint] homeserver.yaml rendered (server_name: ${SYNAPSE_SERVER_NAME:-MISSING})"
fi

# Copy log config
if [ -f "$LOG_CONFIG_TPL" ]; then
    cp "$LOG_CONFIG_TPL" "$LOG_CONFIG_OUT"
fi

# Fix ownership (Synapse expects /data owned by synapse user 991)
chown -R 991:991 /data 2>/dev/null || true

# Start Synapse via the upstream entrypoint
exec gosu 991:991 python -m synapse.app.homeserver \
    --config-path /data/homeserver.yaml \
    "$@"
