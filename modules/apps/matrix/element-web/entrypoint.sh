#!/bin/sh
# Element Web entrypoint — inject MAIN_DOMAIN + MATRIX_DOMAIN into config.json from template
set -e

ELEMENT_CONFIG="/app/config.json"
ELEMENT_TEMPLATE="/element-config.json.tpl"

if [ -f "$ELEMENT_TEMPLATE" ]; then
    envsubst '${MAIN_DOMAIN} ${MATRIX_DOMAIN}' < "$ELEMENT_TEMPLATE" > "$ELEMENT_CONFIG"
    echo "[element-web] config.json generated (server_name: $MAIN_DOMAIN, homeserver: $MATRIX_DOMAIN)"
fi

# Start nginx
exec nginx -g 'daemon off;'
