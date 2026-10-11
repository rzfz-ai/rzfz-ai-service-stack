#!/usr/bin/env python3
"""Self-contained TEST/echo MCP server (#36 P2).

A minimal streamable-http MCP server used to prove the per-user provisioning
chain end-to-end on 0.91 WITHOUT any real third-party credentials:
  add cred via UI/API -> provision user's proxy -> Caddy SSO route live ->
  an agent (or curl through Caddy) reaches this endpoint -> it confirms the
  RIGHT per-user credential reached it.

It exposes:
  GET  /healthz           -> {"status":"ok"}
  POST /mcp               -> a tiny JSON-RPC-ish MCP surface. `initialize`
                             returns server info; `tools/list` advertises a
                             `whoami` tool; `tools/call` (whoami) returns the
                             injected ECHO_USER and a SHA256 FINGERPRINT of the
                             injected ECHO_TOKEN — NEVER the token plaintext.

Implemented with the stdlib http.server only (no third-party deps) so the test
image builds fast and has a tiny attack surface. This is a TEST aid, not a
production MCP server.
"""

import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ECHO_TOKEN = os.environ.get("ECHO_TOKEN", "")
ECHO_USER = os.environ.get("ECHO_USER", "")
# CRITICAL-1 (#61): the per-user proxy bearer. mcp-manager injects this at
# launch; the proxy REQUIRES `Authorization: Bearer <PROXY_AUTH_TOKEN>` on the
# MCP endpoint and 401s otherwise. This is the real access control on a proxy
# that holds a user's live credentials — the opaque subdomain is only defense
# in depth. /healthz stays open so Caddy/health probes work.
PROXY_AUTH_TOKEN = os.environ.get("PROXY_AUTH_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))


def token_fingerprint() -> str:
    """SHA256 hex prefix of the injected token — proves WHICH cred reached us
    without ever echoing the secret."""
    if not ECHO_TOKEN:
        return ""
    return hashlib.sha256(ECHO_TOKEN.encode("utf-8")).hexdigest()[:16]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not found"})

    def _authorized(self) -> bool:
        """Require `Authorization: Bearer <PROXY_AUTH_TOKEN>` on the MCP endpoint.

        Constant-time compare. If no token was injected (mis-provisioned) we
        FAIL CLOSED (deny) rather than serve an unauthenticated cred-holder.
        """
        if not PROXY_AUTH_TOKEN:
            return False
        presented = self.headers.get("Authorization", "")
        expected = f"Bearer {PROXY_AUTH_TOKEN}"
        return hmac.compare_digest(presented, expected)

    def do_POST(self):
        if self.path.rstrip("/") not in ("/mcp", ""):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})

        method = req.get("method", "")
        rid = req.get("id", 1)

        if method == "initialize":
            return self._send(200, {
                "jsonrpc": "2.0", "id": rid,
                "result": {"serverInfo": {"name": "test-echo-mcp", "version": "1.0"},
                           "capabilities": {"tools": {}}},
            })
        if method == "tools/list":
            return self._send(200, {
                "jsonrpc": "2.0", "id": rid,
                "result": {"tools": [{
                    "name": "whoami",
                    "description": "Echo the injected user + a token fingerprint.",
                    "inputSchema": {"type": "object", "properties": {}},
                }]},
            })
        if method == "tools/call":
            name = (req.get("params") or {}).get("name")
            if name == "whoami":
                return self._send(200, {
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": json.dumps({
                        "user": ECHO_USER,
                        "token_fingerprint": token_fingerprint(),
                        "has_token": bool(ECHO_TOKEN),
                    })}]},
                })
            return self._send(200, {"jsonrpc": "2.0", "id": rid,
                                    "error": {"code": -32601, "message": "unknown tool"}})
        # default
        self._send(200, {"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32601, "message": f"unknown method {method}"}})

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    print(f"test-echo-mcp listening on :{PORT} (user={ECHO_USER!r}, "
          f"token_fingerprint={token_fingerprint()!r})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
