#!/bin/bash
# cognee-mcp wrapper (#2750): start the Bearer -> X-Api-Key bridge on 127.0.0.2 (the image entrypoint rewrites 127.0.0.1/localhost API_URLs), then hand
# over to the image's own entrypoint unchanged (no secret on the argv, #2749).
python3 /opt/rzfz/apikey-bridge.py &
exec /app/entrypoint.sh "$@"
