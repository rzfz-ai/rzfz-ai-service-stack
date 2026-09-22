"""Per-instance bearer auth-shim for cognee-mcp (#36 BLOCKER-1 fix).

cognee-mcp ignores the PROXY_AUTH_TOKEN the mcp-manager injects and its only
container-level guard (MCP_ALLOWED_HOSTS DNS-rebind check) is defeated by a
spoofed `Host: localhost:8000` header. That let ANY container on the shared
`coding-agents` bridge reach ANOTHER user's per-user cognee proxy by container
name and read/write their private memory.

This shim is the per-instance, HOST-INDEPENDENT bearer gate the coordinator
requires: it listens on :8000 (the port callers/Caddy hit), REQUIRES
`Authorization: Bearer <PROXY_AUTH_TOKEN>` on EVERY request, and only then
forwards to the real cognee-mcp on 127.0.0.1:8001. A request without the exact
bearer gets 401, regardless of Host.

#94: that upstream used to be described here as "loopback — not reachable off
the container". It was not. cognee-mcp binds 0.0.0.0:8001 by default, so any
container sharing a network with this one could reach :8001 directly with a
spoofed `Host: localhost:8001` and skip the gate entirely — the shim was a
second layer that existed only in the comment. `upstream_bind_scope()` below
now MEASURES it at start-up and says so in the log, because a defence-in-depth
claim that nobody checks is worth less than no claim at all.

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
#: Host header forwarded upstream. Fixed, so MCP_ALLOWED_HOSTS can name exactly
#: this one value rather than `*` (#94).
UPSTREAM_HOST_HEADER = "localhost:8001"


def _decode_proc_addr(hexaddr: str):
    """A /proc/net/tcp{,6} local_address into an ip_address, or None."""
    import ipaddress
    try:
        if len(hexaddr) == 8:                       # IPv4, little-endian word
            return ipaddress.ip_address(bytes.fromhex(hexaddr)[::-1])
        if len(hexaddr) == 32:                      # IPv6, four LE words
            raw = b"".join(bytes.fromhex(hexaddr[i:i + 8])[::-1]
                           for i in range(0, 32, 8))
            return ipaddress.ip_address(raw)
    except ValueError:
        return None
    return None


def upstream_bind_scope(port: int, proc_root: str = "/proc") -> str:
    """'loopback', 'wildcard' or 'unknown' for the listeners on `port`.

    #94: the shim is only a real second layer if cognee-mcp is unreachable from
    other containers. Rather than assert that in a comment, read it: every
    LISTEN socket (state 0A) on `port` in /proc/net/tcp and tcp6, decoded and
    tested for loopback.

    'unknown' when nothing is listening yet or /proc is unreadable — it is not
    a synonym for 'fine', and the caller must not treat it as one.
    """
    import os as _os
    found = False
    all_loopback = True
    for name in ("tcp", "tcp6"):
        path = _os.path.join(proc_root, "net", name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) < 4 or cols[3] != "0A":     # LISTEN only
                continue
            local = cols[1]
            if ":" not in local:
                continue
            hexaddr, _, hexport = local.rpartition(":")
            try:
                if int(hexport, 16) != port:
                    continue
            except ValueError:
                continue
            addr = _decode_proc_addr(hexaddr)
            if addr is None:
                continue
            found = True
            if not addr.is_loopback:
                all_loopback = False
    if not found:
        return "unknown"
    return "loopback" if all_loopback else "wildcard"


def _bearer_ok(headers) -> bool:
    if not TOKEN:
        # Mis-provisioned (no token) -> fail CLOSED. A proxy with live user creds
        # must never be open.
        return False
    auth = headers.get("Authorization", "")
    expected = f"Bearer {TOKEN}"
    # Constant-time compare, on BYTES: `hmac.compare_digest` raises
    # "TypeError: comparing strings with non-ASCII characters is not supported"
    # for a header carrying a non-ASCII byte, and that exception escaping the
    # handler turns a 401 into an unhandled 500 on a route with no upstream
    # auth. Encoding both sides keeps the comparison constant-time and makes
    # every rejection identical.
    return hmac.compare_digest(auth.encode("latin-1", "ignore"),
                               expected.encode("latin-1", "ignore"))


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
        # This value is FIXED, which is what lets MCP_ALLOWED_HOSTS be a single
        # entry instead of `*` (#94) — the catalog and this line have to agree,
        # and tests/unit/mcp/test_94_authshim_loopback.py holds them together.
        fwd["Host"] = UPSTREAM_HOST_HEADER
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


def describe_upstream_bind(scope: str | None = None):
    """[(stream, line)] describing the measured upstream bind.

    #94: separated from main() so the decision is testable without starting a
    server. It WARNS rather than refusing to start: the primary control
    (sandboxes single-homed on `coding-agents`, proxies on `cognee-backend`)
    still holds, and turning a defence-in-depth gap into a failed launch would
    cost users their memory for a risk the topology already covers.
    """
    port = 8001
    try:
        port = int(UPSTREAM.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        pass
    if scope is None:
        scope = upstream_bind_scope(port)
    if scope == "loopback":
        return [(sys.stdout,
                 f"[cognee-authshim] upstream :{port} is loopback-bound — the "
                 "bearer gate is the only way in.")]
    return [(sys.stderr,
             f"[cognee-authshim] WARNING (#94): upstream :{port} bind scope is "
             f"{scope!r}, not loopback. Any container on a shared network can "
             "reach cognee-mcp directly and bypass this bearer gate. The "
             "sandbox bridge does not reach these proxies, so this is "
             "defence-in-depth, not an open door — but it is NOT the second "
             "layer it is meant to be.")]


def main():
    if not TOKEN:
        print("[cognee-authshim] WARNING: PROXY_AUTH_TOKEN unset — failing CLOSED "
              "(all requests 401).", file=sys.stderr, flush=True)
    for stream, line in describe_upstream_bind():
        print(line, file=stream, flush=True)

    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"[cognee-authshim] bearer-gated shim on :{LISTEN_PORT} -> {UPSTREAM}",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
