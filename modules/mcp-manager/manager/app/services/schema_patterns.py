# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3560 — tool lists leave the gateway without JSON-schema `pattern`s.

llama.cpp compiles every offered tool's input schema into a grammar for
native tool calls and refuses a `pattern` that is not anchored `^…$`
("Pattern must start with '^' and end with '$'"). JSON Schema patterns are
unanchored by definition, so upstream servers emit them legitimately (e.g.
Microsoft 365's `copilot_retrieve.queryString`, zod `.regex(/\\S/)`). One
such tool makes a local model fail the whole chat.

The `pattern` keyword is dropped, not rewritten: llama.cpp's grammar
converter supports only a subset of regex, so an anchored rewrite can fail
the same way. Nothing is lost on the call path: the gateway forwards a
tools/call's arguments unchanged, and the upstream server validates them
against its own (unstripped) schema.

Only the `pattern` KEYWORD of a schema goes. The walk follows the schema
structure, so a parameter that is NAMED "pattern" (a key of a `properties`
map) stays, and so does anything inside `enum`/`const`/`default`/`examples`
(data, not schema). Bodies with nothing to drop are returned unchanged,
byte for byte."""
from __future__ import annotations

import json
import re

#: Keywords whose value is one subschema.
_ONE = frozenset({"items", "additionalItems", "additionalProperties", "contains",
                  "propertyNames", "not", "if", "then", "else",
                  "unevaluatedItems", "unevaluatedProperties"})
#: Keywords whose value is a list of subschemas.
_LIST = frozenset({"anyOf", "oneOf", "allOf", "prefixItems", "items"})
#: Keywords whose value maps names to subschemas (the names are data).
_MAP = frozenset({"properties", "patternProperties", "$defs", "definitions",
                  "dependentSchemas"})

#: A tools/list body larger than this is relayed unchanged (not buffered).
MAX_TOOLS_LIST_BYTES = 32 * 1024 * 1024


def strip_patterns(schema):
    """`schema` without any `pattern` keyword, at any depth; a new object,
    the input is not modified."""
    if isinstance(schema, list):
        return [strip_patterns(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key == "pattern" and isinstance(value, str):
            continue
        if key in _MAP and isinstance(value, dict):
            out[key] = {name: strip_patterns(sub) for name, sub in value.items()}
        elif key in _ONE or key in _LIST:
            out[key] = strip_patterns(value) if isinstance(value, (dict, list)) else value
        else:
            out[key] = value
    return out


def _strip_message(msg) -> bool:
    """Strip the input schemas of a tools/list result in place; True when
    anything was dropped."""
    result = msg.get("result") if isinstance(msg, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return False
    changed = False
    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("inputSchema"), (dict, list)):
            stripped = strip_patterns(tool["inputSchema"])
            if stripped != tool["inputSchema"]:
                tool["inputSchema"] = stripped
                changed = True
    return changed


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _strip_json(text: str):
    obj = json.loads(text)
    msgs = obj if isinstance(obj, list) else [obj]
    changed = False
    for m in msgs:
        changed = _strip_message(m) or changed
    return _dump(obj) if changed else None


_EVENT_SEP = re.compile(r"(\r\n\r\n|\n\n|\r\r)")


def _strip_sse(text: str):
    parts = _EVENT_SEP.split(text)
    changed = False
    for i in range(0, len(parts), 2):
        block = parts[i]
        lines = block.splitlines()
        data = [ln[5:][1:] if ln[5:6] == " " else ln[5:] for ln in lines if ln.startswith("data:")]
        if not data:
            continue
        try:
            new = _strip_json("\n".join(data))
        except (ValueError, RecursionError):
            continue
        if new is None:
            continue
        nl = "\r\n" if "\r\n" in block else "\n"
        rebuilt, placed = [], False
        for ln in lines:
            if ln.startswith("data:"):
                if not placed:
                    rebuilt.append("data: " + new)
                    placed = True
            else:
                rebuilt.append(ln)
        parts[i] = nl.join(rebuilt)
        changed = True
    return "".join(parts) if changed else None


def strip_tools_list_body(content_type: str, body: bytes) -> bytes:
    """A tools/list response body (application/json or text/event-stream)
    with the `pattern` keywords dropped from every tool's inputSchema. A body
    that cannot be read, or has nothing to drop, comes back unchanged."""
    try:
        text = body.decode("utf-8")
        if "text/event-stream" in (content_type or "").lower():
            new = _strip_sse(text)
        else:
            new = _strip_json(text)
    except (ValueError, RecursionError):
        return body
    return body if new is None else new.encode("utf-8")


def strip_tools_list_stream(chunks, content_type: str, limit: int = MAX_TOOLS_LIST_BYTES):
    """Relay a tools/list response with the patterns dropped. JSON is read
    whole (up to `limit`) and yielded once; an event stream is yielded event
    by event, as each one completes, so a stream the upstream keeps open still
    delivers its answer. Past `limit` (one body or one event), what was read
    and the rest pass through unchanged. Transport errors propagate."""
    sse = "text/event-stream" in (content_type or "").lower()
    buf = b""
    it = iter(chunks)
    for chunk in it:
        buf += chunk
        if sse:
            cut = max(buf.rfind(sep) + len(sep) if sep in buf else -1
                      for sep in (b"\n\n", b"\r\r", b"\r\n\r\n"))
            if cut >= 0:
                done, buf = buf[:cut], buf[cut:]
                yield strip_tools_list_body(content_type, done)
        if len(buf) > limit:
            yield buf
            yield from it
            return
    if buf:
        yield strip_tools_list_body(content_type, buf)
