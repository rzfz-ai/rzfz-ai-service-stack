# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The gateway's Docling guard (#2086; design record
docs/superpowers/specs/2026-10-mcp-rework-design.md §7, rulings I2 and I3).

`docling-mcp-server` (docling-mcp 3.3.0 in the pinned docling-serve image) is
not multi-tenant on its own: one in-process document cache for every caller,
keyed by a hash of the source; every document-key tool answers an unknown key
with the list of ALL cached keys; it converts local paths and whole
directories inside its container, writes files to disk, and fetches any URL a
caller names. The gateway therefore admits it only behind this guard:

  * allow-list only — exactly two tools, `convert_document_into_docling_document`
    and `export_docling_document_to_markdown`, each with its own argument
    allow-list; every other tool (the key oracles `check_document_in_local_cache` (3.2.0:
    `is_document_in_local_cache`) and `list_cached_documents`, the directory reader,
    `save_docling_document`, `get_page_thumbnail` (3.2.0: `page_thumbnail`), every
    generation and manipulation tool) is refused before the upstream and
    filtered out of `tools/list`. Only initialize, ping, tools/list,
    tools/call and client notifications pass; `initialize` advertises tools
    only.
  * source guard — `source` must be an http(s) URL with a host; a local path,
    an object-storage URI or a quoted/padded value is refused before the
    upstream (the sidecar's deny-list then fences what an http(s) URL reaches).
  * key binding — each successful convert's `document_key` is recorded for
    the caller; an export whose key is not recorded for THAT caller is refused
    before the upstream. Two callers converting the same source share a key,
    and each is bound to it because it was returned to each.
  * error scrubbing — no upstream error text ever reaches a caller: every
    error becomes "conversion failed" or "document not found", so docling's
    `Existing document-keys are: …` list never leaves the gateway.

The response is read whole (bounded by MAX_RESPONSE_BYTES) and rebuilt: only
the answer to the caller's own request id is returned, as JSON; upstream
notifications and server-to-client requests are dropped."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

CONVERT = "convert_document_into_docling_document"
EXPORT = "export_docling_document_to_markdown"
#: tool -> the arguments it may carry (docling-mcp 3.3.0's schemas, unchanged from 3.2.0 for these two:
#: tools/conversion.py `source`; tools/generation.py `document_key`, `max_size`).
ALLOWED_TOOLS = {CONVERT: frozenset({"source"}),
                 EXPORT: frozenset({"document_key", "max_size"})}
ALLOWED_METHODS = frozenset({"initialize", "ping", "tools/list", "tools/call"})
CONVERSION_FAILED = "conversion failed"
DOCUMENT_NOT_FOUND = "document not found"
REQUEST_FAILED = "request failed"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_LEN = 4096
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def check_request(method, tool, args, *, bound) -> str | None:
    """A refusal message, or None when the request may be relayed.
    `bound(document_key) -> bool` says whether the key is recorded for the
    caller; it must fail closed (False) on any error."""
    if method is None:
        return "only requests and notifications are accepted"
    if method.startswith("notifications/"):
        return None
    if method not in ALLOWED_METHODS:
        return f"{method}: not available on this server"
    if method != "tools/call":
        return None
    allowed = ALLOWED_TOOLS.get(tool)
    if allowed is None:
        return f"{tool}: not available on this server"
    args = args if isinstance(args, dict) else {}
    extra = sorted(k for k in args if k not in allowed)
    if extra:
        return f"{tool}: unknown argument(s) {extra}"
    if tool == CONVERT:
        return _source_refusal(args.get("source"))
    key = args.get("document_key")
    if not isinstance(key, str) or not _KEY.match(key):
        return DOCUMENT_NOT_FOUND
    size = args.get("max_size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        return f"{tool}: max_size must be a whole number"
    if not bound(key):
        return DOCUMENT_NOT_FOUND
    return None


def _source_refusal(source) -> str | None:
    """`source` must be an http(s) URL, exactly as given: docling strips
    quotes and would read a local path, a directory or an object-storage URI."""
    if not isinstance(source, str) or not source or len(source) > MAX_SOURCE_LEN:
        return "source must be an http:// or https:// URL"
    if source != source.strip().strip("\"'") or any(c in source for c in "\r\n\t\\"):
        return "source must be an http:// or https:// URL"
    try:
        parts = urlsplit(source)
    except ValueError:
        return "source must be an http:// or https:// URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "source must be an http:// or https:// URL"
    return None


# ---------------------------------------------------------------------------
# The response
# ---------------------------------------------------------------------------

def _messages(content_type: str, body: bytes) -> list:
    """Every JSON-RPC message in a JSON or SSE body (unparseable parts dropped)."""
    text = body.decode("utf-8", errors="replace")
    out: list = []
    if "text/event-stream" in (content_type or "").lower():
        for event in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(line[5:].lstrip(" ") for line in re.split(r"\r?\n", event)
                             if line.startswith("data:"))
            if data.strip():
                out.extend(_loads(data))
        return out
    return _loads(text)


def _loads(text: str) -> list:
    try:
        v = json.loads(text)
    except ValueError:
        return []
    items = v if isinstance(v, list) else [v]
    return [m for m in items if isinstance(m, dict)]


def _tool_error(rid, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": text}], "isError": True}}


def _rpc_error(rid, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}


def _document_key(result: dict):
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and isinstance(sc.get("document_key"), str):
        key = sc["document_key"]
    else:
        key = None
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parsed = _loads(str(item.get("text") or ""))
                if parsed and isinstance(parsed[0].get("document_key"), str):
                    key = parsed[0]["document_key"]
                    break
    return key if isinstance(key, str) and _KEY.match(key) else None


def _clean_result(result: dict) -> dict:
    """A successful tool result: text content and structured content only."""
    out = {"content": [c for c in (result.get("content") or [])
                       if isinstance(c, dict) and c.get("type") == "text"],
           "isError": False}
    if isinstance(result.get("structuredContent"), dict):
        out["structuredContent"] = result["structuredContent"]
    return out


def sanitize(method, tool, rid, status: int, content_type: str, body: bytes,
             unavailable: str = "the document service is not available right now; try again later.",
             timed_out: str = "the document service timed out; try again."):
    """(status, JSON body or b"", document keys to bind) for the caller.
    Never passes an upstream error text through. A 502/503 (the relay could
    not reach docling-mcp) says plainly that the tool is down; a 504 (the
    relay, or the gateway's own read, timed out) says it timed out."""
    failed = CONVERSION_FAILED if tool == CONVERT else (
        DOCUMENT_NOT_FOUND if tool == EXPORT else REQUEST_FAILED)
    if method is None or method.startswith("notifications/"):
        # A notification is answered with 202 and no body; nothing else passes.
        return (202 if 200 <= status < 300 else 502), b"", []
    if status in (502, 503):
        return 503, _dump(_rpc_error(rid, -32603, unavailable)), []
    if status == 504:
        return 504, _dump(_rpc_error(rid, -32603, timed_out)), []
    if status == 404:
        # The upstream lost the session: the client re-initializes (MCP spec).
        return 404, _dump(_rpc_error(rid, -32001, "session not found")), []
    if not 200 <= status < 300:
        if method == "tools/call":
            return 200, _dump(_tool_error(rid, failed)), []
        if 400 <= status < 500 and status not in _OUR_AUTH:
            # #2680: a client protocol error (tools/list before initialize, a
            # bad Accept, ...) is the client's, not a server fault. Status
            # and the JSON-RPC error body pass through; a body that is not a
            # JSON-RPC error gets a generic one under the same status.
            return status, _client_error_body(rid, body), []
        return 502, _dump(_rpc_error(rid, -32603, REQUEST_FAILED)), []
    answer = next((m for m in _messages(content_type, body)
                   if m.get("id") == rid and ("result" in m or "error" in m)), None)
    if answer is None or "error" in answer or not isinstance(answer.get("result"), dict):
        if method == "tools/call":
            return 200, _dump(_tool_error(rid, failed)), []
        return 200, _dump(_rpc_error(rid, -32603, REQUEST_FAILED)), []
    result = answer["result"]
    if method == "tools/list":
        tools = [t for t in (result.get("tools") or [])
                 if isinstance(t, dict) and t.get("name") in ALLOWED_TOOLS]
        out = {"tools": tools}
        if isinstance(result.get("nextCursor"), str):
            out["nextCursor"] = result["nextCursor"]
        return 200, _dump({"jsonrpc": "2.0", "id": rid, "result": out}), []
    if method == "initialize":
        result = dict(result)
        caps = result.get("capabilities") if isinstance(result.get("capabilities"), dict) else {}
        result["capabilities"] = {"tools": caps.get("tools") or {}}
        return 200, _dump({"jsonrpc": "2.0", "id": rid, "result": result}), []
    if method != "tools/call":
        return 200, _dump({"jsonrpc": "2.0", "id": rid, "result": result}), []
    if result.get("isError"):
        return 200, _dump(_tool_error(rid, failed)), []
    keys = []
    if tool == CONVERT:
        key = _document_key(result)
        if key is None:
            return 200, _dump(_tool_error(rid, failed)), []
        keys = [key]
    return 200, _dump({"jsonrpc": "2.0", "id": rid, "result": _clean_result(result)}), keys


#: The relay is dialled with the gateway's own secret: an upstream 401/403/407
#: is OUR credential failing, a server fault, so it stays a 502.
_OUR_AUTH = (401, 403, 407)


def _client_error_body(rid, body: bytes) -> bytes:
    """The upstream's own JSON-RPC error message, unchanged, when that is
    what it sent; otherwise a generic one (no free-form upstream text)."""
    try:
        v = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        v = None
    if isinstance(v, dict) and v.get("jsonrpc") == "2.0" and isinstance(v.get("error"), dict):
        return body
    return _dump(_rpc_error(rid, -32600, REQUEST_FAILED))


def _dump(msg: dict) -> bytes:
    return json.dumps(msg).encode("utf-8")
