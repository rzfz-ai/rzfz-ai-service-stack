"""Per-instance bearer auth-shim for cognee-mcp (#36 BLOCKER-1 fix).

cognee-mcp ignores the PROXY_AUTH_TOKEN the mcp-manager injects and its only
container-level guard (MCP_ALLOWED_HOSTS DNS-rebind check) is defeated by a
spoofed `Host: localhost:8000` header. That let ANY container on the shared
`coding-agents` bridge reach ANOTHER user's per-user cognee proxy by container
name and read/write their private memory.

This shim is the per-instance, HOST-INDEPENDENT bearer gate the coordinator
requires: it listens on :8000 (the port callers/Caddy hit), REQUIRES
`Authorization: Bearer <PROXY_AUTH_TOKEN>` on EVERY request, and only then
forwards to the real cognee-mcp on 127.0.0.1:8001 (loopback — not reachable off
the container). A request without the exact bearer gets 401, regardless of Host.

The owning user's agent already receives this bearer via agent_wiring
(HEADERS__AUTHORIZATION / .mcp.json headers), so the legit path keeps working; a
non-owner sandbox gets 401 even by container-name + Host-spoof.

Streaming-safe (SSE): responses are streamed through unbuffered so the MCP
streamable-http handshake and long tool calls work.
"""
from __future__ import annotations

import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

UPSTREAM = os.environ.get("SHIM_UPSTREAM", "http://127.0.0.1:8001")
TOKEN = os.environ.get("PROXY_AUTH_TOKEN", "")
LISTEN_PORT = int(os.environ.get("SHIM_LISTEN_PORT", "8000"))
# Health probe path served locally without a bearer (docker healthcheck).
HEALTH_PATH = "/shim-health"


def _bearer_ok(headers) -> bool:
    if not TOKEN:
        # Mis-provisioned (no token) -> fail CLOSED. A proxy with live user creds
        # must never be open.
        return False
    auth = headers.get("Authorization", "")
    expected = f"Bearer {TOKEN}"
    # constant-time compare
    return hmac.compare_digest(auth, expected)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence default logging (never log creds)
        pass

    def _deny(self):
        body = b'{"error":"unauthorized"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _health(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def _proxy(self, method: str):
        if self.path == HEALTH_PATH:
            return self._health()
        # HARD GATE — Host-independent. No valid per-instance bearer => 401.
        if not _bearer_ok(self.headers):
            return self._deny()

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        url = f"{UPSTREAM}{self.path}"
        # Forward headers except hop-by-hop; keep Authorization off the upstream
        # (cognee-mcp uses its own API_TOKEN to reach cognee; the shim bearer is
        # the client<->proxy secret, not for the backend).
        fwd = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection", "authorization",
                      "keep-alive", "proxy-authorization", "transfer-encoding",
                      "upgrade"):
                continue
            fwd[k] = v
        # Preserve a loopback Host so cognee-mcp's DNS-rebind guard is satisfied.
        fwd["Host"] = "localhost:8001"
        req = Request(url, data=body, method=method, headers=fwd)
        try:
            resp = urlopen(req, timeout=300)
        except HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except URLError as e:
            self._bad_gateway(str(e))
            return
        # Stream the upstream response through (SSE-safe).
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            lk = k.lower()
            if lk in ("connection", "keep-alive", "transfer-encoding"):
                continue
            self.send_header(k, v)
        # For streamable-http/SSE the upstream doesn't send Content-Length; use
        # chunked so we can stream without knowing the length.
        clen = resp.headers.get("Content-Length")
        if clen is None:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass
        else:
            self.end_headers()
            try:
                self.wfile.write(resp.read())
            except Exception:
                pass

    def _bad_gateway(self, msg: str):
        body = b'{"error":"bad_gateway"}'
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_PUT(self):
        self._proxy("PUT")


def main():
    if not TOKEN:
        print("[cognee-authshim] WARNING: PROXY_AUTH_TOKEN unset — failing CLOSED "
              "(all requests 401).", file=sys.stderr, flush=True)
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"[cognee-authshim] bearer-gated shim on :{LISTEN_PORT} -> {UPSTREAM}",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
