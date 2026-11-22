# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Custom (freeform) tools on ``/v1/responses`` — translated for the engine (#3048).

The Responses API has two kinds of client tool: ``function`` tools, whose
arguments are a JSON object, and ``custom`` tools, whose input is free text,
optionally constrained by a grammar (``format: {type: "grammar", syntax:
"lark", definition: ...}``). Codex CLI's ``apply_patch`` is a custom tool.

The engine behind the router does not know custom tools. llama.cpp's
``/v1/responses`` (``tools/server/server-chat.cpp``,
``server_chat_convert_responses_to_chatcmpl``, b9851, b11778 and v0.9.0 alike) converts
the request to a chat completion and:

* SKIPS every tool whose type is not ``function`` ("unsupported Responses tool
  type ... skipped") — the model is never offered ``apply_patch`` at all;
* refuses an input item of type ``custom_tool_call`` / ``custom_tool_call_output``
  with "Cannot determine type of 'item'" — so the turn after a patch would 400;
* only ever answers with ``function_call`` items.

So the manager translates, on the way in and on the way back:

* a ``custom`` tool becomes a ``function`` tool of the same name taking
  ``{"input": "<the text>"}`` — the description says so and carries the
  grammar definition as text, because the engine cannot enforce it;
* ``custom_tool_call`` / ``custom_tool_call_output`` input items become
  ``function_call`` / ``function_call_output`` items;
* a ``function_call`` answer for one of those names becomes a
  ``custom_tool_call`` again, its ``input`` taken from the JSON arguments.

On the way back the ``apply_patch`` input gets the OBVIOUS envelope repairs
(``repair_apply_patch``): CRLF, a code fence around the patch, a missing
``*** Begin Patch`` / ``*** End Patch``. Content lines are never touched — a
wrong context line stays wrong, and the client's own error reaches the model.

Everything here is pure; ``proxy.py`` calls it only for ``/v1/responses``.
"""
from __future__ import annotations

import os
import codecs
import copy
import json

#: The custom tool whose input is repaired. Codex CLI's file-edit tool.
APPLY_PATCH = "apply_patch"

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_HUNK_HEADERS = ("*** Add File: ", "*** Delete File: ", "*** Update File: ")

_INPUT_NOTE = (
    "This tool is called with a JSON object whose only field, `input`, holds "
    "the tool's raw text input exactly as it would be written without JSON "
    "(newlines as \\n). Do not wrap it in code fences.")


# --- the apply_patch repair --------------------------------------------------

def _is_fence(line: str) -> bool:
    return line.strip().startswith("```")


def repair_apply_patch(text):
    """Repair the envelope of an ``apply_patch`` input; never its content.

    * CRLF line endings become LF;
    * a code fence wrapping the whole patch (```` ``` ````, ```` ```diff ````,
      ```` ```patch ```` …) is removed;
    * a missing ``*** Begin Patch`` first line / ``*** End Patch`` last line is
      added.

    Only text that IS a patch is repaired: it must start with a hunk header
    (``*** Add File:``, ``*** Update File:``, ``*** Delete File:``) or carry
    one of the two markers. Anything else comes back as it came (CRLF aside),
    and a valid patch comes back unchanged, character for character.
    """
    if not isinstance(text, str):
        return text
    out = text.replace("\r\n", "\n")
    lines = out.split("\n")
    # Only truly EMPTY lines are outside the patch: a line of spaces is a blank
    # CONTEXT line and belongs to the hunk (#3048 box capture: cutting at the last
    # non-blank line dropped it and broke the hunk).
    nonblank = [i for i, ln in enumerate(lines) if ln != ""]
    if not nonblank:
        return out
    first, last = nonblank[0], nonblank[-1]

    fenced = _is_fence(lines[first]) and last > first and lines[last].strip() == "```"
    if fenced:
        inner = lines[first + 1:last]
        inner_nonblank = [i for i, ln in enumerate(inner) if ln != ""]
        if not inner_nonblank:
            return out
        lines, first, last = inner, inner_nonblank[0], inner_nonblank[-1]

    head, tail = lines[first].rstrip(), lines[last].rstrip()
    if not (head == _BEGIN or tail == _END or lines[first].startswith(_HUNK_HEADERS)):
        return out
    if not fenced and head == _BEGIN and tail == _END:
        return out
    body = lines[first:last + 1]
    if head != _BEGIN:
        body.insert(0, _BEGIN)
    if tail != _END:
        body.append(_END)
    return "\n".join(body) + "\n"


# --- the request side --------------------------------------------------------

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


def _function_for_custom(tool: dict) -> dict:
    desc = tool.get("description") or ""
    fmt = tool.get("format")
    grammar = ""
    if tool.get("name") == APPLY_PATCH:
        # #3048 box capture: with the raw Lark grammar in the description the
        # model copied grammar tokens into its patch, wrote git-style
        # `@@ -1,3 +1,3 @@` headers and put `*** End of File` on non-final
        # hunks. A short plain-language spec with one example instead.
        grammar = "\n\n" + APPLY_PATCH_SPEC
    elif isinstance(fmt, dict) and fmt.get("type") == "grammar" and isinstance(fmt.get("definition"), str):
        grammar = (f"\n\nThe `input` text must match this {fmt.get('syntax') or 'grammar'} "
                   f"grammar:\n{fmt['definition']}")
    return {
        "type": "function",
        "name": tool.get("name"),
        "description": (desc + "\n\n" if desc else "") + _INPUT_NOTE + grammar,
        "parameters": {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
            "additionalProperties": False,
        },
    }


def translate_custom_tools(body: dict) -> frozenset:
    """Rewrite ``body`` in place so the engine sees only function tools.

    Returns the names of the custom tools translated (empty when there were
    none, in which case ``body`` is untouched and nothing needs to be mapped
    back)."""
    if not isinstance(body, dict):
        return frozenset()
    names: set = set()
    tools = body.get("tools")
    if isinstance(tools, list):
        new_tools = []
        for t in tools:
            if isinstance(t, dict) and t.get("type") == "custom" and isinstance(t.get("name"), str):
                names.add(t["name"])
                new_tools.append(_function_for_custom(t))
            else:
                new_tools.append(t)
        if names:
            body["tools"] = new_tools

    tc = body.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "custom":
        body["tool_choice"] = dict(tc, type="function")

    items = body.get("input")
    if isinstance(items, list):
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "custom_tool_call":
                if isinstance(item.get("name"), str):
                    names.add(item["name"])
                fc = {k: v for k, v in item.items() if k not in ("type", "input")}
                fc["type"] = "function_call"
                fc["arguments"] = json.dumps({"input": item.get("input") or ""})
                items[idx] = fc
            elif item.get("type") == "custom_tool_call_output":
                items[idx] = dict(item, type="function_call_output")
    return frozenset(names)


# --- the response side -------------------------------------------------------

def _custom_input(name: str, arguments) -> str:
    raw = arguments if isinstance(arguments, str) else ""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    text = parsed.get("input") if isinstance(parsed, dict) and isinstance(parsed.get("input"), str) else raw
    if name != APPLY_PATCH:
        return text
    # LLM_MANAGER_APPLY_PATCH_REPAIR=0 turns the envelope repair off, so a box
    # check can compare edit attempts with and without it (#3048 box check).
    if os.environ.get("LLM_MANAGER_APPLY_PATCH_REPAIR", "1").strip() == "0":
        return text
    return repair_apply_patch(text)


def custom_call_item(item, names: frozenset):
    """A ``function_call`` output item for a translated custom tool, as the
    ``custom_tool_call`` the client asked for; any other item unchanged."""
    if not (isinstance(item, dict) and item.get("type") == "function_call"
            and item.get("name") in names):
        return item
    out = {k: v for k, v in item.items() if k not in ("type", "arguments")}
    out["type"] = "custom_tool_call"
    out["input"] = _custom_input(item["name"], item.get("arguments"))
    return out


def restore_custom_calls(data, names: frozenset):
    """A non-streamed Responses answer with its custom tool calls restored."""
    if not names or not isinstance(data, dict) or not isinstance(data.get("output"), list):
        return data
    data = copy.copy(data)
    data["output"] = [custom_call_item(i, names) for i in data["output"]]
    return data


class ResponsesStreamRewriter:
    """Rewrites a Responses SSE stream so translated custom tool calls reach
    the client as ``custom_tool_call`` items.

    Only the events that describe such a call change: ``output_item.added`` /
    ``output_item.done`` carry the restored item, ``response.completed`` /
    ``response.incomplete`` carry the restored output list, and the
    ``function_call_arguments.delta`` / ``.done`` events for those items are
    dropped — they hold fragments of JSON, not of the tool's text, and the
    finished input arrives in ``output_item.done``. Every other event is
    passed through byte for byte."""

    _FINAL = ("response.completed", "response.incomplete")
    _ARGS = ("response.function_call_arguments.delta", "response.function_call_arguments.done")

    def __init__(self, names: frozenset):
        self.names = names
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._custom_ids: set = set()

    def feed(self, chunk: bytes) -> bytes:
        self._buf += self._decoder.decode(chunk)
        out: list[str] = []
        while "\n\n" in self._buf:
            event, self._buf = self._buf.split("\n\n", 1)
            rewritten = self._rewrite(event)
            if rewritten is not None:
                out.append(rewritten + "\n\n")
        return "".join(out).encode("utf-8")

    def flush(self) -> bytes:
        rest = self._buf + self._decoder.decode(b"", final=True)
        self._buf = ""
        return rest.encode("utf-8")

    def _rewrite(self, event: str):
        lines = event.split("\n")
        data_idx = [i for i, ln in enumerate(lines) if ln.startswith("data:")]
        if len(data_idx) != 1:
            return event
        try:
            obj = json.loads(lines[data_idx[0]][len("data:"):].strip())
        except ValueError:
            return event
        if not isinstance(obj, dict):
            return event
        etype = obj.get("type")
        if etype in ("response.output_item.added", "response.output_item.done"):
            item = obj.get("item")
            new = custom_call_item(item, self.names)
            if new is item:
                return event
            if isinstance(item, dict) and item.get("id"):
                self._custom_ids.add(item["id"])
            obj = dict(obj, item=new)
        elif etype in self._ARGS:
            return None if obj.get("item_id") in self._custom_ids else event
        elif etype in self._FINAL:
            resp = obj.get("response")
            if not isinstance(resp, dict):
                return event
            new = restore_custom_calls(resp, self.names)
            if new.get("output") == resp.get("output"):
                return event
            obj = dict(obj, response=new)
        else:
            return event
        lines[data_idx[0]] = "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        return "\n".join(lines)
