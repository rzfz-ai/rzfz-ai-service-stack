#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""razzfazz-mcp-proxy, Google build (#2472, #2086; design record §3, rulings I4/R2-2).

The in-repo, pinned build of the per-user Google proxy. One container per
person, started by mcp-manager's provisioner with THAT person's own Google
access token (catalogue `google`, env GOOGLE_ACCESS_TOKEN from the person's
own vault row) and a per-instance bearer (PROXY_AUTH_TOKEN).

What it guarantees:
  * The ONLY Google credential it ever sends is GOOGLE_ACCESS_TOKEN from its
    own environment. Nothing in a request (headers, arguments) can name,
    replace or add a Google credential: tool arguments are schema-checked
    (unknown keys are refused), and the request's Authorization header is
    only compared with PROXY_AUTH_TOKEN, never forwarded.
  * No PROXY_AUTH_TOKEN, no service (401 on /mcp, fail closed).
  * Read-only: the four tools match the catalogue's read-only scopes
    (calendar.readonly, drive.readonly). Requests go to www.googleapis.com
    only, and a redirect is never followed (it would carry the token).
  * Standard library only: no third-party package enters the image, so the
    Dockerfile's digest-pinned base is the whole of its supply chain.

Transport: MCP streamable-http, JSON responses (no server-initiated stream):
POST /mcp answers `initialize`, `ping`, `tools/list`, `tools/call`;
`notifications/*` get 202; GET /mcp gets 405. GET /healthz is open.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET_KIND = "google"
GOOGLE_API = "https://www.googleapis.com"
PROTOCOL_VERSION = "2025-06-18"
MAX_BODY = 1024 * 1024          # a request body over 1 MiB is refused (413)
MAX_FILE_BYTES = 1024 * 1024    # drive_read_file returns at most 1 MiB of text
GOOGLE_TIMEOUT = 30
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# ---------------------------------------------------------------------------
# Google, with the container's own token only
# ---------------------------------------------------------------------------

class GoogleError(Exception):
    """A message safe to show the caller (no token, no upstream body)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise GoogleError("Google answered with a redirect; not followed.")


_OPENER = urllib.request.build_opener(_NoRedirect)


def google_get(path: str, params: dict | None = None, *, raw: bool = False, limit: int = MAX_FILE_BYTES):
    """GET GOOGLE_API + path with the container's own access token. `path` is
    built by this module from validated values, never taken from a caller."""
    token = _env("GOOGLE_ACCESS_TOKEN")
    if not token:
        raise GoogleError("Google is not connected for this proxy. Connect Google in the MCP "
                          "portal; that restarts this integration with your authorization.")
    url = GOOGLE_API + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=GOOGLE_TIMEOUT) as resp:
            data = resp.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # The recoveries named here work (B9 fix round 1): "Start" on a
            # running proxy whose token has expired restarts it with a renewed
            # token, and a reconnect restarts it with the new one.
            raise GoogleError("Google refused the request: the authorization has expired or "
                              "lacks this scope. Press Start on Google in the MCP portal (it "
                              "restarts the integration with a renewed authorization); if Google "
                              "still refuses, connect Google again.") from None
        if e.code == 404:
            raise GoogleError("Not found in Google.") from None
        raise GoogleError(f"Google answered HTTP {e.code}.") from None
    except GoogleError:
        raise
    except Exception as e:  # noqa: BLE001 — never echo an upstream body or URL
        raise GoogleError(f"Google could not be reached ({type(e).__name__}).") from None
    if raw:
        return data
    try:
        return json.loads(data or b"{}")
    except ValueError:
        raise GoogleError("Google answered with something that is not JSON.") from None


# ---------------------------------------------------------------------------
# Tools (read-only)
# ---------------------------------------------------------------------------

def _int(v, lo, hi, default):
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"must be a whole number in {lo}..{hi}")
    return v


def _str(v, name, max_len=500, default=""):
    if v is None:
        return default
    if not isinstance(v, str) or len(v) > max_len or any(c in v for c in "\r\n\x00"):
        raise ValueError(f"{name} must be a single-line string of at most {max_len} characters")
    return v


def calendar_list_calendars(args):
    data = google_get("/calendar/v3/users/me/calendarList", {"maxResults": 100})
    return [{"id": c.get("id"), "summary": c.get("summary"), "primary": bool(c.get("primary"))}
            for c in data.get("items", [])]


def calendar_list_events(args):
    cal = _str(args.get("calendar_id"), "calendar_id", 300, "primary") or "primary"
    params = {"singleEvents": "true", "orderBy": "startTime",
              "maxResults": _int(args.get("max_results"), 1, 50, 10),
              "timeMin": _str(args.get("time_min"), "time_min", 64),
              "timeMax": _str(args.get("time_max"), "time_max", 64),
              "q": _str(args.get("query"), "query", 200)}
    data = google_get(f"/calendar/v3/calendars/{urllib.parse.quote(cal, safe='')}/events", params)
    return [{"summary": ev.get("summary"), "start": ev.get("start"), "end": ev.get("end"),
             "location": ev.get("location"), "link": ev.get("htmlLink")}
            for ev in data.get("items", [])]


def _drive_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def drive_search_files(args):
    query = _str(args.get("query"), "query", 200)
    if not query:
        raise ValueError("query is required")
    data = google_get("/drive/v3/files", {
        "q": f"fullText contains {_drive_literal(query)} and trashed = false",
        "pageSize": _int(args.get("max_results"), 1, 50, 10),
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink)"})
    return data.get("files", [])


def drive_read_file(args):
    file_id = _str(args.get("file_id"), "file_id", 200)
    if not _FILE_ID.match(file_id or ""):
        raise ValueError("file_id must be a Google Drive file id")
    meta = google_get(f"/drive/v3/files/{file_id}", {"fields": "id,name,mimeType,size"})
    mime = str(meta.get("mimeType") or "")
    if mime in _EXPORTS:
        body = google_get(f"/drive/v3/files/{file_id}/export", {"mimeType": _EXPORTS[mime]}, raw=True)
    elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
        body = google_get(f"/drive/v3/files/{file_id}", {"alt": "media"}, raw=True)
    else:
        raise GoogleError(f"'{meta.get('name')}' is a {mime or 'binary'} file; only documents, "
                          "sheets, slides and text files can be read.")
    truncated = len(body) > MAX_FILE_BYTES
    return {"name": meta.get("name"), "mimeType": mime, "truncated": truncated,
            "text": body[:MAX_FILE_BYTES].decode("utf-8", errors="replace")}


def _schema(props: dict, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


TOOLS = {
    "calendar_list_calendars": (calendar_list_calendars, "List your Google calendars.",
                                _schema({})),
    "calendar_list_events": (calendar_list_events, "List events of one of your calendars.", _schema({
        "calendar_id": {"type": "string", "description": "default: primary"},
        "time_min": {"type": "string", "description": "RFC 3339 lower bound"},
        "time_max": {"type": "string", "description": "RFC 3339 upper bound"},
        "query": {"type": "string"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 50}})),
    "drive_search_files": (drive_search_files, "Search your Google Drive by full text.", _schema({
        "query": {"type": "string"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 50}}, ("query",))),
    "drive_read_file": (drive_read_file, "Read a Google Doc, Sheet, Slides deck or text file "
                        "from your Drive as text (at most 1 MiB).",
                        _schema({"file_id": {"type": "string"}}, ("file_id",))),
}


def call_tool(name, args):
    """(result, is_error). Unknown keys are refused, so no argument can carry
    a credential or steer the upstream."""
    if name not in TOOLS:
        return f"unknown tool {name!r}", True
    fn, _desc, schema = TOOLS[name]
    if not isinstance(args, dict):
        return "arguments must be an object", True
    extra = sorted(set(args) - set(schema["properties"]))
    if extra:
        return f"unknown argument(s): {', '.join(extra)}", True
    try:
        return fn(args), False
    except (ValueError, GoogleError) as e:
        return str(e), True


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "razzfazz-mcp-proxy"
    sys_version = ""

    def _send(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = _env("PROXY_AUTH_TOKEN")
        if not expected:
            return False
        return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {expected}")

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"status": "ok"})
        if self.path.split("?", 1)[0].rstrip("/") == "/mcp":
            return self._send(405, {"error": "no event stream on this server"})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        return self._send(405, {"error": "stateless server"})

    def do_POST(self):
        if self.path.split("?", 1)[0].rstrip("/") != "/mcp":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return self._send(400, {"error": "bad content-length"})
        if length > MAX_BODY:
            return self._send(413, {"error": "request too large"})
        try:
            req = json.loads(self.rfile.read(length) if length else b"")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if not isinstance(req, dict):
            return self._send(400, {"error": "one JSON-RPC message per request"})
        method, rid = str(req.get("method", "")), req.get("id")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        if method.startswith("notifications/"):
            return self._send(202)
        if method == "initialize":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "serverInfo": {"name": "razzfazz-mcp-proxy-google", "version": "1"},
                "capabilities": {"tools": {}}}})
        if method == "ping":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {}})
        if method == "tools/list":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": n, "description": d, "inputSchema": s} for n, (_f, d, s) in TOOLS.items()]}})
        if method == "tools/call":
            result, is_error = call_tool(params.get("name"), params.get("arguments") or {})
            text = result if isinstance(result, str) else json.dumps(result)
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}], "isError": is_error}})
        return self._send(200, {"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32601, "message": f"unknown method {method}"}})

    def log_message(self, *a):  # never log request lines (no URLs, no headers)
        pass


def main() -> int:
    kind = _env("MCP_TARGET_KIND")
    if kind != TARGET_KIND:
        print(f"razzfazz-mcp-proxy: this build serves MCP_TARGET_KIND={TARGET_KIND!r} only, "
              f"not {kind!r}", file=sys.stderr, flush=True)
        return 2
    if not _env("PROXY_AUTH_TOKEN"):
        print("razzfazz-mcp-proxy: PROXY_AUTH_TOKEN is not set; refusing to start",
              file=sys.stderr, flush=True)
        return 2
    port = int(_env("PORT") or "8080")
    print(f"razzfazz-mcp-proxy ({TARGET_KIND}) listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
