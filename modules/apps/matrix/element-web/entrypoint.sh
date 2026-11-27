#!/bin/sh
# Element Web entrypoint — inject MATRIX_SERVER_NAME + MATRIX_DOMAIN into config.json from template
set -e

ELEMENT_CONFIG="/app/config.json"
ELEMENT_TEMPLATE="/element-config.json.tpl"

if [ -f "$ELEMENT_TEMPLATE" ]; then
    envsubst '${MATRIX_SERVER_NAME} ${MATRIX_DOMAIN}' < "$ELEMENT_TEMPLATE" > "$ELEMENT_CONFIG"
    echo "[element-web] config.json generated (server_name: $MATRIX_SERVER_NAME, homeserver: $MATRIX_DOMAIN)"
fi

# Start nginx
exec nginx -g 'daemon off;'
