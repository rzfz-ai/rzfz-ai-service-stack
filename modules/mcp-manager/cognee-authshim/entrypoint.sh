#!/bin/bash
# Per-user cognee proxy = cognee-mcp on LOOPBACK :8001 + a bearer-gated shim on
# :8000 (#36 BLOCKER-1). The shim is the ONLY listener addressed off the
# container; a spoofed-Host container-name hit to :8000 gets the bearer gate.
set -e

# 1) Start the real cognee-mcp on :8001 (via HTTP_PORT). The proxy is addressed
#    externally on :8000 (the shim); nothing routes :8001 off-box, and the
#    dedicated cognee-backend net keeps non-owners off it entirely.
# cognee 1.4.0's mcp DEFAULTS to stdio and ignores HTTP_PORT unless TRANSPORT_MODE=http
# is set — stdio exits immediately with no stdin, so the wait-for-:8001 below fails and
# the shim exits 1 (#199-adjacent). Match the cognee-mcp compose (TRANSPORT_MODE=http).
export HTTP_PORT=8001
export TRANSPORT_MODE=http
/app/entrypoint.sh &
COGNEE_PID=$!

# 2) Wait for cognee-mcp to accept connections on 8001.
for i in $(seq 1 60); do
    if python3 -c "import socket;socket.create_connection(('127.0.0.1',8001),1)" 2>/dev/null; then
        break
    fi
    if ! kill -0 "$COGNEE_PID" 2>/dev/null; then
        echo "[cognee-authshim] cognee-mcp exited during startup" >&2
        exit 1
    fi
    sleep 2
done

# 3) Run the bearer-gated shim on :8000 in the foreground.
export SHIM_UPSTREAM="http://127.0.0.1:8001"
export SHIM_LISTEN_PORT=8000
exec python3 /app/shim.py
