# Per-user cognee proxy with a HOST-INDEPENDENT per-instance bearer gate (#36
# BLOCKER-1). Wraps the stock cognee-mcp image: cognee-mcp runs on :8001 (loopback-bind requested; verified at
# runtime by shim.py, see #94)
# :8001, a tiny bearer-checking shim listens on :8000 (the only externally
# addressed port). A request without the exact PROXY_AUTH_TOKEN bearer gets 401
# even when reached by container-name with a spoofed Host header.
#
# Pinned to the SAME tag as the shared cognee-mcp sidecar (bump in lockstep).
FROM cognee/cognee-mcp:1.5.3.dev1

# The upstream cognee-mcp image runs as a non-root user, so a build-time chmod
# is not permitted — invoke the scripts via their interpreter instead (no exec
# bit needed).
COPY shim.py /app/shim.py
COPY entrypoint.sh /app/authshim-entrypoint.sh

EXPOSE 8000
# The shim answers /shim-health locally without a bearer (creds never exposed).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=60s \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/shim-health',timeout=4)" || exit 1

ENTRYPOINT ["bash", "/app/authshim-entrypoint.sh"]
