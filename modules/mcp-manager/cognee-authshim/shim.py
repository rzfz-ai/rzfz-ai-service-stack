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

The cognee side (the relay). cognee's JWT lives one hour, and cognee-mcp holds
the API_TOKEN it was started with, so an instance answered every call with
cognee's 401 an hour after its start, and returned that as an ordinary answer
(isError false). cognee-mcp therefore talks to cognee through a second,
loopback-only listener of this process (RELAY_HOST:RELAY_PORT; the entrypoint
points API_URL there). The relay puts the CURRENT token on every request,
renews it from mcp-manager shortly before it expires and once on a 401 (with
the renewal secret only this container holds; the account password stays in
the manager), and never follows a redirect off itself. A 401 it cannot
recover is counted, and a tools/call answered while one happened goes back to
the client as isError true with the reason, never as an answer.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from urllib.error import HTTPError, URLError

UPSTREAM = os.environ.get("SHIM_UPSTREAM", "http://127.0.0.1:8001")
TOKEN = os.environ.get("PROXY_AUTH_TOKEN", "")
LISTEN_PORT = int(os.environ.get("SHIM_LISTEN_PORT", "8000"))
#: The relay: loopback only. 127.0.0.2, not 127.0.0.1 or localhost: cognee-mcp's
#: entrypoint rewrites an API_URL naming those to the docker host's address.
RELAY_HOST = os.environ.get("SHIM_RELAY_HOST", "127.0.0.2")
RELAY_PORT = int(os.environ.get("SHIM_RELAY_PORT", "8002"))
COGNEE_UPSTREAM = os.environ.get("COGNEE_UPSTREAM", "").rstrip("/")
RENEW_URL = os.environ.get("RZFZ_COGNEE_RENEW_URL", "")
RENEW_SECRET = os.environ.get("RZFZ_COGNEE_RENEW_SECRET", "")
INSTANCE_ID = os.environ.get("RZFZ_INSTANCE_ID", "")
#: Renew this many seconds before the token's `exp`.
RENEW_BEFORE = int(os.environ.get("SHIM_RENEW_BEFORE", "300"))
AUTH_FAILURE_TEXT = (
    "Memory is unavailable: cognee refused this instance's sign-in (401 Unauthorized) "
    "and a new sign-in could not be obtained. Start the integration again in the MCP "
    "portal; if that does not help, ask an administrator.")
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


def jwt_exp(token: str):
    """The `exp` claim of a JWT (unverified, only to schedule a renewal), or None."""
    import base64
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part.encode("ascii"))).get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001
        return None


class CogneeToken:
    """The token the relay presents to cognee, renewed from mcp-manager."""

    def __init__(self, token, renew_url="", renew_secret="", instance_id=""):
        self.token = token or ""
        self.renew_url, self.renew_secret, self.instance_id = renew_url, renew_secret, instance_id
        self.lock = threading.Lock()
        self.last_try = 0.0
        self.failures = 0           # unrecovered 401s from cognee
        self.reason = ""

    @property
    def renewable(self) -> bool:
        return bool(self.renew_url and self.renew_secret and self.instance_id)

    def current(self) -> str:
        exp = jwt_exp(self.token)
        if self.renewable and (exp is None or exp - time.time() < RENEW_BEFORE):
            self.renew(min_gap=30)
        return self.token

    def renew(self, min_gap: float) -> bool:
        """Fetch a fresh token. At most one attempt per `min_gap` seconds."""
        if not self.renewable:
            return False
        with self.lock:
            if time.time() - self.last_try < min_gap:
                return False
            self.last_try = time.time()
            req = Request(self.renew_url, method="POST",
                          data=json.dumps({"instance": self.instance_id}).encode(),
                          headers={"Authorization": f"Bearer {self.renew_secret}",
                                   "Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=20) as r:
                    tok = (json.loads(r.read() or b"{}") or {}).get("token") or ""
            except HTTPError as e:
                print(f"[cognee-authshim] token renewal refused ({e.code})",
                      file=sys.stderr, flush=True)
                return False
            except (URLError, OSError, ValueError) as e:
                print(f"[cognee-authshim] token renewal failed ({type(e).__name__})",
                      file=sys.stderr, flush=True)
                return False
            if not tok:
                return False
            self.token = tok
            return True

    def after_unauthorized(self, sent: str) -> bool:
        """True when a different token is available to retry with."""
        if self.token != sent:
            return True
        return self.renew(min_gap=5) and self.token != sent

    def unrecovered(self, reason: str):
        with self.lock:
            self.failures += 1
            self.reason = reason


TOKENS = CogneeToken(os.environ.get("SHIM_COGNEE_TOKEN", ""), RENEW_URL, RENEW_SECRET,
                     INSTANCE_ID)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_RELAY_OPENER = build_opener(_NoRedirect)
_HOP = ("host", "content-length", "connection", "authorization", "x-api-key",
        "keep-alive", "proxy-authorization", "transfer-encoding", "upgrade", "cookie")


class RelayHandler(BaseHTTPRequestHandler):
    """cognee-mcp -> cognee, with the current per-user token (loopback only)."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _call(self, method, body, token):
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
        if token:
            fwd["Authorization"] = f"Bearer {token}"
        req = Request(f"{COGNEE_UPSTREAM}{self.path}", data=body, method=method, headers=fwd)
        try:
            return _RELAY_OPENER.open(req, timeout=600)
        except HTTPError as e:
            return e

    def _relay(self, method):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        token = TOKENS.current()
        try:
            resp = self._call(method, body, token)
            if resp.status == 401:
                if TOKENS.after_unauthorized(token):
                    resp.read()
                    resp = self._call(method, body, TOKENS.token)
                if resp.status == 401:
                    TOKENS.unrecovered("cognee answered 401")
        except (URLError, OSError) as e:
            data = json.dumps({"detail": f"cognee unreachable ({type(e).__name__})"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(resp.status)
        relay_base = f"http://{RELAY_HOST}:{self.server.server_address[1]}"
        for k, v in resp.headers.items():
            lk = k.lower()
            if lk in ("connection", "keep-alive", "transfer-encoding", "content-length"):
                continue
            if lk == "location" and COGNEE_UPSTREAM and v.startswith(COGNEE_UPSTREAM):
                v = relay_base + v[len(COGNEE_UPSTREAM):]      # stay on the relay
            elif lk == "location" and v.startswith("http"):
                from urllib.parse import urlsplit
                parts = urlsplit(v)
                v = relay_base + parts.path + (f"?{parts.query}" if parts.query else "")
            self.send_header(k, v)
        data = resp.read()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:  # noqa: BLE001
            pass

    def do_GET(self):
        self._relay("GET")

    def do_POST(self):
        self._relay("POST")

    def do_PUT(self):
        self._relay("PUT")

    def do_PATCH(self):
        self._relay("PATCH")

    def do_DELETE(self):
        self._relay("DELETE")


def tool_call_id(body):
    """The JSON-RPC id of a tools/call request body, or None for anything else."""
    try:
        msg = json.loads(body or b"")
    except ValueError:
        return None
    if isinstance(msg, dict) and msg.get("method") == "tools/call":
        return msg.get("id")
    return None


def _as_failure(msg, req_id, reason):
    if isinstance(msg, dict) and "result" in msg and msg.get("id") == req_id:
        res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
        content = [{"type": "text", "text": reason}]
        content += [c for c in (res.get("content") or []) if isinstance(c, dict)]
        msg = dict(msg, result={"content": content, "isError": True})
    return msg


def mark_failed(data: bytes, content_type: str, req_id, reason: str) -> bytes:
    """The tools/call answer `data` (JSON or SSE) turned into an MCP tool error."""
    if "text/event-stream" in (content_type or ""):
        out = []
        for line in data.decode("utf-8", "replace").split("\n"):
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                    line = "data: " + json.dumps(_as_failure(msg, req_id, reason))
                except ValueError:
                    pass
            out.append(line)
        return "\n".join(out).encode()
    try:
        return json.dumps(_as_failure(json.loads(data), req_id, reason)).encode()
    except ValueError:
        return data


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
        call_id = tool_call_id(body) if method == "POST" else None
        failures_before = TOKENS.failures
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
        if call_id is not None:
            # A tool call is answered whole (it ends with its result). When
            # cognee refused the sign-in during it and no renewal helped, the
            # answer is an MCP tool error carrying the reason.
            data = resp.read()
            if TOKENS.failures != failures_before:
                data = mark_failed(data, resp.headers.get("Content-Type", ""), call_id,
                                   AUTH_FAILURE_TEXT)
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() in ("connection", "keep-alive", "transfer-encoding",
                                 "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
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


def start_relay(host: str = None, port: int = None) -> ThreadingHTTPServer:
    """Serve the cognee relay in a daemon thread (loopback only)."""
    srv = ThreadingHTTPServer((host or RELAY_HOST, RELAY_PORT if port is None else port),
                              RelayHandler)
    threading.Thread(target=srv.serve_forever, name="cognee-relay", daemon=True).start()
    return srv


def main():
    if not TOKEN:
        print("[cognee-authshim] WARNING: PROXY_AUTH_TOKEN unset — failing CLOSED "
              "(all requests 401).", file=sys.stderr, flush=True)
    if COGNEE_UPSTREAM:
        relay = start_relay()
        print(f"[cognee-authshim] cognee relay on {RELAY_HOST}:{relay.server_address[1]} -> "
              f"{COGNEE_UPSTREAM} (token renewal "
              f"{'on' if TOKENS.renewable else 'OFF: relaunch the instance to get it'})",
              flush=True)
    for stream, line in describe_upstream_bind():
        print(line, file=stream, flush=True)

    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"[cognee-authshim] bearer-gated shim on :{LISTEN_PORT} -> {UPSTREAM}",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
