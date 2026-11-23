"""Bearer -> X-Api-Key bridge for the shared cognee-mcp sidecar (#2750).

cognee-mcp's API-mode client sends `Authorization: Bearer <key>`, while the cognee API
accepts an API key only as `X-Api-Key`. This bridge listens on loopback inside the
cognee-mcp container, accepts either form of the SAME key (COGNEE_API_KEY), and forwards
to the cognee API with `X-Api-Key`. A Bearer that is not exactly that key is a 401 here,
compared in constant time; an X-Api-Key request is passed through and judged by cognee.
"""
from __future__ import annotations

import hmac
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UPSTREAM = os.environ.get("BRIDGE_UPSTREAM", "http://cognee:8000").rstrip("/")
KEY = os.environ.get("COGNEE_API_KEY", "")
# 127.0.0.2, not 127.0.0.1: the cognee-mcp image entrypoint rewrites any API_URL that
# contains the literal 127.0.0.1 / localhost to the docker gateway (#2750).
LISTEN_HOST = os.environ.get("BRIDGE_LISTEN_HOST", "127.0.0.2")
LISTEN_PORT = int(os.environ.get("BRIDGE_LISTEN_PORT", "8002"))
_DROP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
         "upgrade", "authorization", "proxy-authorization"}


def bearer_ok(authorization: str) -> bool:
    if not KEY:
        return False  # no key provisioned: fail closed
    # Bytes, so a non-ASCII header is a clean mismatch and not a TypeError (500).
    return hmac.compare_digest(authorization.encode("latin-1", "ignore"),
                               f"Bearer {KEY}".encode("latin-1", "ignore"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # never log credentials
        pass

    def _send(self, code, body, headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in _DROP}
        if self.headers.get("Authorization") is not None:
            if not bearer_ok(self.headers["Authorization"]):
                return self._send(401, b'{"detail":"unauthorized"}',
                                  [("Content-Type", "application/json")])
            fwd["X-Api-Key"] = KEY
        elif "X-Api-Key" not in self.headers:
            return self._send(401, b'{"detail":"unauthorized"}',
                              [("Content-Type", "application/json")])
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else None
        req = Request(f"{UPSTREAM}{self.path}", data=body, method=self.command, headers=fwd)
        try:
            resp = urlopen(req, timeout=300)
            code, data, hdrs = resp.status, resp.read(), resp.headers
        except HTTPError as e:
            code, data, hdrs = e.code, e.read(), e.headers
        except URLError:
            return self._send(502, b'{"error":"bad_gateway"}')
        out = [(k, v) for k, v in hdrs.items() if k.lower() not in _DROP]
        self._send(code, data, out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _proxy


if __name__ == "__main__":
    if not KEY:
        print("[apikey-bridge] COGNEE_API_KEY unset: Bearer requests fail closed", flush=True)
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
