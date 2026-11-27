#!/usr/bin/env python3
"""Coding-agent → GPUStack local-model shim (razzfazz.ai #36 / PR #84).

One tiny stdlib-only sidecar, started per-container on 127.0.0.1, that makes the
bundled coding CLIs actually run against the LOCAL GPUStack model. It solves two
distinct breakages behind ONE endpoint:

A. codex (>= ~0.122, Feb 2026) HARD-REMOVED `wire_api = "chat"` — every provider
   must now speak the OpenAI *Responses* API (`/v1/responses`). But GPUStack
   serves *Chat-Completions only* and 404s `/responses`, and there is NO
   codex-side flag to force chat (pinning to the last chat-supporting build =
   shipping a stale CLI). So codex points here with `wire_api="responses"`; the
   shim (a) accepts `POST /v1/responses`, (b) translates it to a
   Chat-Completions request for GPUStack, (c) streams the Chat-Completions SSE
   back as a correct *Responses* SSE (response.created → output_item.added →
   output_text.delta* / function_call_arguments.delta* → output_item.done →
   response.completed with `usage.total_tokens` — the fields codex's Rust SSE
   parser requires), including tool/function calls. codex's FREEFORM tools
   (apply_patch, Responses `type: "custom"`) are offered to the model as a
   function with one string argument and returned as `custom_tool_call`
   items, with obvious patch-envelope errors repaired.

B. opencode + gsd-pi speak Chat-Completions directly, but qwen3.x is a THINKING
   model: left with thinking ON (the GPUStack default) it spends the whole token
   budget on `reasoning_content` and returns no usable content → the agent loop
   hangs. Neither CLI exposes a config path to add a request-body param. So they
   point their provider `baseURL` here too; the shim passes `/chat/completions`
   through to GPUStack but INJECTS `chat_template_kwargs.enable_thinking=false`
   (and forwards ALL request headers + streams — opencode's AI-SDK stalls
   otherwise).

The same injection also applies to codex's translated call (A) so codex doesn't
thinking-burn either. Self-contained, no new stack service, no LiteLLM, air-gap
safe.
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import urllib.request
import urllib.error
import urllib.parse
import uuid

# The OpenAI-compatible base the box's LLM plane serves. GPUStack serves it at
# `/v1-openai`; the LLM Manager (#612/#959) serves it at `/v1`. codex's
# Responses requests are translated to `${UPSTREAM}/chat/completions`.
UPSTREAM = os.environ.get("CODEX_SHIM_UPSTREAM", "http://llm:8080/v1")
# The BARE origin (scheme+host[:port], no path) and the upstream's BASE PATH,
# split apart so pass-through requests can be re-anchored on the upstream's own
# prefix instead of a hardcoded one. opencode/gsd/pi hit `/v1-openai/<x>` on the
# shim (that is what entrypoint.sh seeds as their baseURL) — forwarding that
# path verbatim under the bare origin is only correct while the upstream happens
# to BE a `/v1-openai` server. On an LLM-Manager box (#999) it produced
# `http://llm-manager:8080/v1-openai/…`, which the manager 404s: it serves
# `/v1/chat/completions`, `/v1/models`, `/v1/embeddings` and nothing under
# `/v1-openai`. Both are derived from UPSTREAM unless set explicitly.
_p = urllib.parse.urlsplit(UPSTREAM)
UPSTREAM_ORIGIN = os.environ.get(
    "CODEX_SHIM_ORIGIN", f"{_p.scheme}://{_p.netloc}")
UPSTREAM_PATH = os.environ.get(
    "CODEX_SHIM_UPSTREAM_PATH", _p.path).rstrip("/")
# The client-facing base paths this shim answers on, longest first so
# `/v1-openai` is never mis-split by the `/v1` rule. codex is pointed at `/v1`
# (config.toml), opencode/gsd/pi at `/v1-openai` (SHIM_URL) — both are just
# aliases for "the upstream's OpenAI base", and both re-anchor on UPSTREAM_PATH.
_CLIENT_BASE_PATHS = ("/v1-openai", "/v1")
LISTEN_PORT = int(os.environ.get("CODEX_SHIM_PORT", "8123"))
# When true, force the qwen thinking-disable body param on every upstream call.
DISABLE_THINKING = os.environ.get("CODEX_SHIM_DISABLE_THINKING", "1") != "0"


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _responses_to_chat(req: dict) -> dict:
    """Translate a codex Responses request → a Chat-Completions request body."""
    messages = []
    instructions = req.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    for item in req.get("input", []) or []:
        itype = item.get("type", "message")
        if itype == "message":
            role = item.get("role", "user")
            # Responses uses developer/user/assistant; chat has no "developer".
            if role == "developer":
                role = "system"
            parts = []
            for c in item.get("content", []) or []:
                ctype = c.get("type")
                if ctype in ("input_text", "output_text", "text"):
                    parts.append(c.get("text", ""))
            messages.append({"role": role, "content": "\n".join(parts)})
        elif itype == "function_call":
            # assistant asking to call a tool
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or "call_0",
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "") or "{}",
                    },
                }],
            })
        elif itype == "function_call_output":
            # the tool's result
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "call_0",
                "content": _stringify(item.get("output", "")),
            })
        elif itype == "custom_tool_call":
            # a FREEFORM tool call (codex's apply_patch) the model made earlier.
            # Chat knows only function calls, so it goes back the way the model
            # saw it: a function call carrying the raw text as its one argument.
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or "call_0",
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": json.dumps(
                            {CUSTOM_TOOL_ARG: item.get("input", "") or ""}),
                    },
                }],
            })
        elif itype == "custom_tool_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "call_0",
                "content": _output_text(item.get("output", "")),
            })

    chat: dict = {
        "model": req.get("model"),
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    # Responses `tools` are FLAT ({type:function, name, description, parameters});
    # Chat wants nested ({type:function, function:{name,description,parameters}}).
    tools = req.get("tools")
    if tools:
        chat_tools = []
        for t in tools:
            if t.get("type") == "function":
                chat_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                })
            elif t.get("type") == "custom":
                chat_tools.append(_custom_tool_as_function(t))
        if chat_tools:
            chat["tools"] = chat_tools
        tc = req.get("tool_choice")
        if tc:
            if isinstance(tc, dict) and tc.get("type") == "custom":
                tc = {"type": "function", "function": {"name": tc.get("name")}}
            chat["tool_choice"] = tc

    for k in ("temperature", "top_p", "max_output_tokens"):
        if k in req:
            chat["max_tokens" if k == "max_output_tokens" else k] = req[k]

    if DISABLE_THINKING:
        chat.setdefault("chat_template_kwargs", {})["enable_thinking"] = False

    return chat


def _stringify(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v)


# ── freeform ("custom") tools ────────────────────────────────────────────────
# codex offers apply_patch as a Responses CUSTOM tool: {type:"custom", name,
# description, format:{type:"grammar", syntax:"lark", definition}} whose call
# carries raw text (`custom_tool_call.input`), not JSON arguments. Chat
# Completions has no such tool kind, so the model sees a function with ONE
# string parameter, and its call is unwrapped back into a custom_tool_call.
# Dropping these tools (as before) left codex unable to edit files at all.
CUSTOM_TOOL_ARG = "input"


def _custom_tool_names(req: dict) -> set:
    """Names of the request's custom tools — how a returned call is routed."""
    return {t.get("name") for t in (req.get("tools") or [])
            if isinstance(t, dict) and t.get("type") == "custom" and t.get("name")}


#: The apply_patch format for a model that cannot be held to the grammar.
APPLY_PATCH_SPEC = """Write the patch as plain text in exactly this format and nothing else:

*** Begin Patch
*** Update File: path/to/file.py
@@ def main():
     total = 0
-    print("old")
+    print("new")
*** End Patch

Rules:
- After `@@` copy one line from the file that locates the change (for example the `def` or `class` line above it). Never put line numbers after `@@` (not `@@ -1,3 +1,3 @@`).
- Every line of a section starts with exactly one marker, in front of the line's own indentation: a space for unchanged context, `-` to remove, `+` to add. Never leave the marker out: the context line `    total = 0` is written with five leading spaces, as in the example. Context must match the file exactly.
- New file: `*** Add File: path` followed by the content, every line starting with `+`. Delete: `*** Delete File: path`.
- Several changes in one file: one `*** Update File:` block with several `@@` sections.
- Write `*** End of File` only after a section whose last line really is the last line of the file."""


def _custom_tool_as_function(t: dict) -> dict:
    desc = t.get("description", "") or ""
    fmt = t.get("format") or {}
    if t.get("name") == "apply_patch":
        # The box capture of the LLM Manager's /v1/responses showed the raw Lark
        # grammar backfiring: the model copied grammar tokens, wrote git-style
        # `@@ -1,3 +1,3 @@` headers and misplaced `*** End of File`. A short
        # plain-language spec with one example instead.
        desc += "\n\n" + APPLY_PATCH_SPEC
    elif fmt.get("definition"):
        desc += ("\n\nThe text must follow this "
                 f"{fmt.get('syntax') or fmt.get('type') or 'grammar'} grammar:\n"
                 f"{fmt['definition']}")
    desc += (f"\n\nPass the complete raw text as the `{CUSTOM_TOOL_ARG}` string "
             "argument, exactly as it should be applied.")
    return {
        "type": "function",
        "function": {
            "name": t.get("name"),
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {CUSTOM_TOOL_ARG: {
                    "type": "string",
                    "description": "The raw text input for this tool.",
                }},
                "required": [CUSTOM_TOOL_ARG],
            },
        },
    }


def _unwrap_custom_input(args: str) -> str:
    """The raw text inside a model's function arguments for a custom tool.

    Expected `{"input": "<text>"}`; local models also send a differently-named
    single key, a bare JSON string, literal newlines inside the string (not
    strict JSON), or the raw text itself. Each of those still means "this text".
    """
    try:
        v = json.loads(args, strict=False)
    except (ValueError, TypeError):
        return args
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        if isinstance(v.get(CUSTOM_TOOL_ARG), str):
            return v[CUSTOM_TOOL_ARG]
        strs = [x for x in v.values() if isinstance(x, str)]
        if len(v) == 1 and len(strs) == 1:
            return strs[0]
    return args


# apply_patch text a local model sends often has the right hunks in the wrong
# wrapper: no "*** Begin Patch" / "*** End Patch" lines, a Markdown code fence
# around it, CRLF line ends. codex rejects each of those and the model retries
# (up to 4 attempts per edit seen with qwen3.6). Those OBVIOUS wrapper errors are
# repaired here; no line between the envelope lines is ever changed (a wrong
# context line stays wrong — codex's own error tells the model).
APPLY_PATCH_TOOL = "apply_patch"
_PATCH_BEGIN = "*** Begin Patch"
_PATCH_END = "*** End Patch"
_PATCH_HUNKS = ("*** Add File: ", "*** Delete File: ", "*** Update File: ")


def _repair_patch(text: str) -> str:
    """Fix the envelope of an apply_patch text; a valid patch comes back as is."""
    if not isinstance(text, str):
        return text
    repaired = False
    t = text
    if "\r\n" in t:
        t = t.replace("\r\n", "\n")
        repaired = True
    lines = t.split("\n")

    def _trim() -> bool:
        """Drop blank lines around the text; True if more than the final
        newline went."""
        n = len(lines)
        lead = 0
        while lines and lines[0] == "":
            lines.pop(0)
            lead += 1
        while lines and lines[-1] == "":
            lines.pop()
        return lead > 0 or n - lead - len(lines) > 1

    if _trim():
        repaired = True
    if (len(lines) >= 2 and lines[0].strip().startswith("```")
            and lines[-1].strip() == "```"):
        del lines[0], lines[-1]
        _trim()
        repaired = True
    if not any(ln.startswith(_PATCH_HUNKS) for ln in lines):
        # not recognisably a patch: only the line ends are touched
        return t if repaired else text
    if lines[0].strip() != _PATCH_BEGIN:
        lines.insert(0, _PATCH_BEGIN)
        repaired = True
    if lines[-1].strip() != _PATCH_END:
        lines.append(_PATCH_END)
        repaired = True
    if not repaired:
        return text
    return "\n".join(lines) + "\n"


def _output_text(v) -> str:
    """A custom_tool_call_output's output: plain text, or content items."""
    if isinstance(v, list):
        texts = [c.get("text", "") for c in v
                 if isinstance(c, dict) and isinstance(c.get("text"), str)]
        if texts:
            return "\n".join(texts)
    return _stringify(v)


def _custom_item(st: dict) -> dict:
    return {"id": st["item_id"], "type": "custom_tool_call",
            "status": "completed", "call_id": st["id"], "name": st["name"],
            "input": st.get("input", "")}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        # codex may probe /models; proxy it straight through.
        if self.path.endswith("/models"):
            return self._passthrough_get()
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        if not self.path.endswith("/responses"):
            # anything else codex might POST — pass through unchanged
            return self._passthrough_post(raw)
        try:
            req = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            return self._send_json(400, {"error": f"bad json: {e}"})
        self._handle_responses(req)

    # ── the translation ──────────────────────────────────────────────────────
    def _handle_responses(self, req: dict):
        chat = _responses_to_chat(req)
        custom_names = _custom_tool_names(req)
        up_req = urllib.request.Request(
            f"{UPSTREAM}/chat/completions",
            data=json.dumps(chat).encode(),
            method="POST",
        )
        up_req.add_header("Content-Type", "application/json")
        auth = self.headers.get("Authorization")
        if auth:
            up_req.add_header("Authorization", auth)

        try:
            up = urllib.request.urlopen(up_req, timeout=600)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            return self._send_json(e.code, {"error": {"message": body}})
        except Exception as e:  # noqa: BLE001
            return self._send_json(502, {"error": {"message": str(e)}})

        # Start the Responses SSE stream to codex.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        resp_id = f"resp_{uuid.uuid4().hex}"
        self._write(_sse({"type": "response.created",
                          "response": {"id": resp_id, "status": "in_progress"}}))

        # State machine over the Chat-Completions SSE.
        text_open = False
        text_item_id = f"msg_{uuid.uuid4().hex}"
        # tool calls keyed by index → {id, name, args, item_id, opened}
        tools: dict[int, dict] = {}
        out_index = 0
        usage = None
        finish_reason = None
        text_accum = []

        for line in up:
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            delta = ch.get("delta") or {}
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

            # text content (ignore reasoning_content — codex wants final text)
            content = delta.get("content")
            if content:
                if not text_open:
                    self._write(_sse({
                        "type": "response.output_item.added",
                        "output_index": out_index,
                        "item": {"id": text_item_id, "type": "message",
                                 "role": "assistant",
                                 "content": [{"type": "output_text", "text": ""}]},
                    }))
                    text_open = True
                text_accum.append(content)
                self._write(_sse({
                    "type": "response.output_text.delta",
                    "item_id": text_item_id,
                    "output_index": out_index,
                    "content_index": 0,
                    "delta": content,
                }))

            # tool calls (streamed incrementally)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                st = tools.get(idx)
                if st is None:
                    st = {"id": tc.get("id") or f"call_{idx}",
                          "name": "", "args": "",
                          "item_id": f"fc_{uuid.uuid4().hex}", "opened": False}
                    tools[idx] = st
                fn = tc.get("function") or {}
                if tc.get("id"):
                    st["id"] = tc["id"]
                if fn.get("name"):
                    st["name"] += fn["name"]
                if not st["opened"] and st["name"]:
                    # close any open text item first
                    if text_open:
                        self._close_text(text_item_id, out_index, "".join(text_accum))
                        text_open = False
                        out_index += 1
                    st["out_index"] = out_index
                    st["custom"] = st["name"] in custom_names
                    if st["custom"]:
                        st["item_id"] = f"ctc_{uuid.uuid4().hex}"
                        added = {"id": st["item_id"], "type": "custom_tool_call",
                                 "status": "in_progress", "call_id": st["id"],
                                 "name": st["name"], "input": ""}
                    else:
                        added = {"id": st["item_id"], "type": "function_call",
                                 "call_id": st["id"], "name": st["name"],
                                 "arguments": ""}
                    self._write(_sse({
                        "type": "response.output_item.added",
                        "output_index": out_index,
                        "item": added,
                    }))
                    st["opened"] = True
                    out_index += 1
                if fn.get("arguments"):
                    st["args"] += fn["arguments"]
                    # A custom call's text is wrapped in JSON until the call is
                    # complete; it is unwrapped and sent in one piece at close.
                    if st["opened"] and not st.get("custom"):
                        self._write(_sse({
                            "type": "response.function_call_arguments.delta",
                            "item_id": st["item_id"],
                            "output_index": st.get("out_index", 0),
                            "delta": fn["arguments"],
                        }))

        # close open items
        if text_open:
            self._close_text(text_item_id, 0, "".join(text_accum))
        for st in tools.values():
            if st.get("opened") and st.get("custom"):
                st["input"] = _unwrap_custom_input(st["args"])
                if st["name"] == APPLY_PATCH_TOOL:
                    st["input"] = _repair_patch(st["input"])
                self._write(_sse({
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": st["item_id"], "call_id": st["id"],
                    "output_index": st.get("out_index", 0),
                    "delta": st["input"],
                }))
                self._write(_sse({
                    "type": "response.custom_tool_call_input.done",
                    "item_id": st["item_id"], "call_id": st["id"],
                    "output_index": st.get("out_index", 0),
                    "input": st["input"],
                }))
                self._write(_sse({
                    "type": "response.output_item.done",
                    "output_index": st.get("out_index", 0),
                    "item": _custom_item(st),
                }))
            elif st.get("opened"):
                self._write(_sse({
                    "type": "response.function_call_arguments.done",
                    "item_id": st["item_id"],
                    "output_index": st.get("out_index", 0),
                    "arguments": st["args"],
                }))
                self._write(_sse({
                    "type": "response.output_item.done",
                    "output_index": st.get("out_index", 0),
                    "item": {"id": st["item_id"], "type": "function_call",
                             "call_id": st["id"], "name": st["name"],
                             "arguments": st["args"], "status": "completed"},
                }))

        # Build the final output array + usage (codex REQUIRES total_tokens).
        output = []
        if text_accum:
            output.append({"id": text_item_id, "type": "message",
                           "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text",
                                        "text": "".join(text_accum)}]})
        for st in tools.values():
            if st.get("custom"):
                output.append(_custom_item(st))
                continue
            output.append({"id": st["item_id"], "type": "function_call",
                           "call_id": st["id"], "name": st["name"],
                           "arguments": st["args"], "status": "completed"})

        u_in = (usage or {}).get("prompt_tokens", 0)
        u_out = (usage or {}).get("completion_tokens", 0)
        u_total = (usage or {}).get("total_tokens", u_in + u_out)
        self._write(_sse({
            "type": "response.completed",
            "response": {
                "id": resp_id,
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": u_in, "output_tokens": u_out,
                          "total_tokens": u_total},
            },
        }))
        self._end_stream()

    def _close_text(self, item_id, out_index, full_text):
        self._write(_sse({
            "type": "response.output_text.done",
            "item_id": item_id, "output_index": out_index,
            "content_index": 0, "text": full_text,
        }))
        self._write(_sse({
            "type": "response.output_item.done",
            "output_index": out_index,
            "item": {"id": item_id, "type": "message", "role": "assistant",
                     "status": "completed",
                     "content": [{"type": "output_text", "text": full_text}]},
        }))

    # ── plumbing ──────────────────────────────────────────────────────────────
    def _write(self, b: bytes):
        try:
            self.wfile.write(b)
            self.wfile.flush()
        except Exception:  # noqa: BLE001
            pass

    def _end_stream(self):
        self._write(b"data: [DONE]\n\n")

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._write(data)

    def _fwd_headers(self):
        """All incoming request headers except hop-by-hop / length (which
        urllib recomputes). Forwarding the FULL set matters: opencode's AI-SDK
        stalls if Accept / others are dropped."""
        return {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "connection",
                                     "transfer-encoding")}

    def _passthrough_get(self):
        req = urllib.request.Request(self._upstream_url(), method="GET")
        for k, v in self._fwd_headers().items():
            req.add_header(k, v)
        try:
            up = urllib.request.urlopen(req, timeout=60)
            data = up.read()
            self.send_response(up.status)
            self.send_header("Content-Type", up.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._write(data)
        except Exception as e:  # noqa: BLE001
            self._send_json(502, {"error": str(e)})

    def _passthrough_post(self, raw):
        """opencode + gsd-pi speak Chat-Completions directly (not Responses), so
        they point their provider baseURL here and hit /chat/completions. We
        (1) inject enable_thinking=false so qwen3.x doesn't burn the budget on
        reasoning and hang the agent loop; (2) forward ALL request headers
        (opencode's AI-SDK stalls if Accept/etc are dropped); (3) STREAM the
        response (these clients send stream:true)."""
        url = self._upstream_url()
        if url.endswith("/chat/completions") and DISABLE_THINKING:
            try:
                body = json.loads(raw)
                if isinstance(body, dict):
                    body.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
                    raw = json.dumps(body).encode()
            except Exception:  # noqa: BLE001 — non-JSON: forward untouched
                pass
        req = urllib.request.Request(url, data=raw, method="POST")
        for k, v in self._fwd_headers().items():
            req.add_header(k, v)
        try:
            up = urllib.request.urlopen(req, timeout=600)
            self.send_response(up.status)
            self.send_header("Content-Type",
                             up.headers.get("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = up.read(1024)
                if not chunk:
                    break
                self._write(chunk)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type",
                             e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._write(data)
        except Exception as e:  # noqa: BLE001
            self._send_json(502, {"error": str(e)})

    def _upstream_url(self) -> str:
        """Map the incoming request path onto the UPSTREAM's own base path.

        Both client-facing bases (`/v1` for codex, `/v1-openai` for
        opencode/gsd/pi) mean the same thing — "the upstream's OpenAI-compatible
        root" — so the verb after the base is re-anchored on UPSTREAM_PATH:

          GPUStack box (UPSTREAM …/v1-openai, UPSTREAM_PATH=/v1-openai)
            `/v1/models`               → `${ORIGIN}/v1-openai/models`
            `/v1-openai/chat/completions` → `${ORIGIN}/v1-openai/chat/completions`
          LLM-Manager box (UPSTREAM …:8080/v1, UPSTREAM_PATH=/v1)   ← #999
            `/v1/models`               → `${ORIGIN}/v1/models`
            `/v1-openai/chat/completions` → `${ORIGIN}/v1/chat/completions`

        The GPUStack column is byte-identical to the pre-#999 behaviour; only a
        non-`/v1-openai` upstream changes, which is exactly the 404 case. A path
        carrying neither base is forwarded verbatim (nothing to re-anchor).
        """
        p = self.path
        for base in _CLIENT_BASE_PATHS:
            if p == base or p.startswith(base + "/"):
                return f"{UPSTREAM_ORIGIN}{UPSTREAM_PATH}{p[len(base):]}"
        return f"{UPSTREAM_ORIGIN}{p}"


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    ThreadingServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
