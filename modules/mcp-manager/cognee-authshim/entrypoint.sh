#!/bin/bash
# Per-user cognee proxy = cognee-mcp on :8001 + a bearer-gated shim on :8000
# (#36 BLOCKER-1). The shim is the listener CALLERS address; a spoofed-Host
# container-name hit to :8000 gets the bearer gate.
#
# #94: this header used to say cognee-mcp was on LOOPBACK :8001 and that
# "nothing routes :8001 off-box". Neither was arranged anywhere — cognee-mcp
# binds 0.0.0.0 by default, so any container on a shared network could reach
# :8001 directly, spoof `Host: localhost:8001`, and skip the gate. The comment
# was the only thing making the shim look like a second layer.
set -e

# 1) Start the real cognee-mcp on :8001 (via HTTP_PORT), asking it to bind
#    loopback. The dedicated cognee-backend net keeps non-owners off the proxy;
#    a loopback bind is the second layer behind that.
# cognee-mcp's mcp DEFAULTS to stdio and ignores HTTP_PORT unless TRANSPORT_MODE=http
# is set — stdio exits immediately with no stdin, so the wait-for-:8001 below fails and
# the shim exits 1 (#199-adjacent). Match the cognee-mcp compose (TRANSPORT_MODE=http).
# Confirmed unchanged behavior at cognee-mcp 1.5.3 (2026.08 bump).
export HTTP_PORT=8001
export TRANSPORT_MODE=http
# Ask for a loopback bind. cognee-mcp takes its port from HTTP_PORT, so
# HTTP_HOST is the symmetric name; FASTMCP_HOST covers the underlying FastMCP
# server if it reads its own. WHICH of these cognee-mcp actually honours could
# not be established from source here — so the shim MEASURES the resulting bind
# at start-up (shim.py::upstream_bind_scope) and says plainly in the log whether
# it worked. An unverified export plus an honest check beats an unverified
# export plus a confident comment, which is what #94 was.
export HTTP_HOST=127.0.0.1
export FASTMCP_HOST=127.0.0.1
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
