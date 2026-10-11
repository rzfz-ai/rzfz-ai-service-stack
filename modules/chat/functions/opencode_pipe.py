"""
id: opencode
title: OpenCode Manifold Pipe
author: razzfazz-stack
version: 1.1.2
required_open_webui_version: 0.5.0
license: MIT
description: Per-user routing of chats to opencode serve running inside the calling user's coding-tools agent. Sessions per Open WebUI chat_id; SSE streaming via /event with title-generation contamination + async [DONE] workarounds.
requirements: aiohttp, requests, pydantic
"""

# M020 S05 — OpenCode pipe.
#
# Routes chats to opencode serve (port 4096) running inside the calling
# user's coding-tools agent container (provisioned via agent-manager). The
# user-facing surface for coding-tools is gsd --web on the catalog's primary
# `internal` port (8080) — covered by M020 S06; this pipe targets the
# `opencode_internal` extra port.
#
# Two documented workarounds (via groxaxo/open-webui-opencode-integration):
#  1. Title-generation contamination — when __task__ ∈ {title_generation,
#     tags_generation}, create a new throwaway opencode session instead of
#     reusing the chat's session, otherwise the title generation prompt
#     contaminates the main session's context window.
#  2. Async [DONE] sentinel — the OpenWebUI pipe runtime expects an OpenAI-
#     format `{choices:[{delta:{},finish_reason:"stop"}]}` final chunk to
#     mark stream completion; without it the UI hangs on streaming chats.

# @include _lib/per_user_routing.py
#
# Note: pipes that use `# @include` MUST NOT carry their own `from __future__`
# imports — see chat/seeder/seed.py expand_includes() for rationale.

import asyncio
import base64
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp
import requests
from pydantic import BaseModel, Field

log = logging.getLogger("opencode_pipe")

# ----------------------------------------------------------------------------
# OpenCode HTTP API shape — VERIFY against opencode.ai/docs/server during
# smoke test. Endpoint paths derived from the official opencode-ai/sdk
# OpenAPI spec at planning time.

_SESSION_CREATE = "/session"           # POST → {id, ...}
_SESSION_MESSAGE = "/session/{sid}/message"   # POST → enqueue user message
_EVENT_STREAM = "/event"               # GET SSE stream of all server events


# ----------------------------------------------------------------------------
# Per-user opencode session map. Keyed by (user_id, chat_id).

class _SessionStore:
    def __init__(self, path: str = "/app/backend/data/opencode_conv_map.json") -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("opencode session-map load failed: %s", e)
            self._data = {}

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            log.warning("opencode session-map flush failed: %s", e)

    @staticmethod
    def _key(user_id: str, chat_id: str) -> str:
        return f"{user_id}::{chat_id}"

    def get(self, user_id: str, chat_id: str) -> str:
        with self._lock:
            return self._data.get(self._key(user_id, chat_id), "")

    def set(self, user_id: str, chat_id: str, session_id: str) -> None:
        if not chat_id or not session_id:
            return
        with self._lock:
            self._data[self._key(user_id, chat_id)] = session_id
            self._flush()


_store = _SessionStore()


# ----------------------------------------------------------------------------
# Pipe

class Pipe:

    class Valves(BaseModel):
        REQUEST_TIMEOUT: int = Field(default=600)
        EMIT_TOOL_STATUS: bool = Field(default=True)
        DEBUG: bool = Field(default=False)
        # opencode-serve message schema requires explicit providerID/modelID.
        # Defaults match the GPUStack provider config baked into the
        # coding-tools agent image. Override per-pipe to swap models.
        PROVIDER_ID: str = Field(default="gpustack")
        MODEL_ID: str = Field(default="gemma4")

    def __init__(self) -> None:
        self.type = "manifold"
        self.id = "opencode"
        self.name = "OpenCode: "
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        return [{"id": "personal", "name": "Personal Workspace"}]

    # ------------------------------------------------------------------
    async def pipe(self, body: dict,
             __user__: Optional[dict] = None,
             __metadata__: Optional[dict] = None,
             __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
             __task__: Optional[str] = None,
             **kwargs: Any):
        __user__ = __user__ or {}
        __metadata__ = __metadata__ or {}
        chat_id = str(__metadata__.get("chat_id") or body.get("chat_id") or "")
        user_id = str(__user__.get("id") or __user__.get("username") or "anonymous")
        stream = bool(body.get("stream", False))

        # Route to the calling user's per-user opencode serve
        result = find("coding-tools", __user__, use_extra_port="opencode_internal")
        if isinstance(result, NotProvisioned):
            return result.msg
        if not result.internal_url:
            return ("⚠️ Coding-tools instance found but the catalog port mapping "
                    "is missing the `opencode_internal` slot (M020 S05 catalog change).")

        query = self._extract_query(body.get("messages") or [])
        if not query:
            return "⚠️ Empty user message — nothing to send to OpenCode."

        base_url = result.internal_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        # Workaround #1 from groxaxo: opencode HTTP server uses Basic auth via
        # OPENCODE_SERVER_PASSWORD; basic_auth_tuple defaults the username to
        # "admin" — adjust at the top of the file if upstream uses different.
        basic = basic_auth_tuple(result.auth)
        if basic:
            tok = base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
            headers["Authorization"] = f"Basic {tok}"

        # Workaround #2 from groxaxo: title/tag generation must not reuse the
        # chat's persistent session id, else the internal-task prompt
        # contaminates the conversation context. Mint a throwaway session.
        is_util = __task__ in {"title_generation", "tags_generation"}
        if is_util:
            session_id = self._create_session(base_url, headers)
            if not session_id:
                return f"⚠️ OpenCode session creation failed for {__task__}."
        else:
            session_id = _store.get(user_id, chat_id) if chat_id else ""
            if not session_id:
                session_id = self._create_session(base_url, headers)
                if session_id and chat_id:
                    _store.set(user_id, chat_id, session_id)

        if not session_id:
            return "⚠️ OpenCode session unavailable."

        msg_url = base_url + _SESSION_MESSAGE.format(sid=session_id)
        # opencode-serve v1.14+ requires the model fields nested under `model`,
        # NOT flat at the top level. The schema has additionalProperties: false,
        # so a flat {providerID, modelID, parts} body is silently rejected and
        # the request hangs with no HTTP response (reproduced 2026-05-12 on prod).
        # Default modelID = gemma4 because qwen3-coder-next needs a llama.cpp
        # build that knows the qwen3next architecture (>6152), which the
        # bundled GPUStack.app llama-box on Mac workers doesn't ship yet.
        msg_payload = {
            "model": {
                "providerID": self.valves.PROVIDER_ID,
                "modelID":    self.valves.MODEL_ID,
            },
            "parts": [{"type": "text", "text": query}],
        }

        # v1.1.2: opencode-serve v1.14 returns the FULL assistant response
        # synchronously in the POST body — {info: ..., parts: [...]} —
        # NOT via SSE events. Pre-v1.1.2 versions of this pipe subscribed
        # to /event for `message.part.updated` deltas which simply never
        # arrive for synchronous message posts in v1.14, leading to the
        # "(no content)" symptom reported on dev 2026-05-12.
        #
        # Both stream and non-stream paths use the same response-reading
        # logic now. For OWUI's stream=true case we yield text chunks
        # post-hoc (single full reply, not per-token) which is honest
        # behavior given opencode-serve doesn't expose per-token streams
        # at this endpoint. The /event-based per-token path may return
        # in a future opencode rev or via /prompt_async.
        if stream and not is_util:
            return self._stream_post_response(headers, msg_url, msg_payload, __event_emitter__)
        return await self._collect_post_response(headers, msg_url, msg_payload)

    # ------------------------------------------------------------------
    def _create_session(self, base_url: str, headers: dict) -> str:
        try:
            r = requests.post(base_url + _SESSION_CREATE, headers=headers,
                              timeout=self.valves.REQUEST_TIMEOUT, json={})
            r.raise_for_status()
            return str(r.json().get("id") or "")
        except (requests.RequestException, ValueError) as e:
            log.warning("opencode session create failed: %s", e)
            return ""

    # ------------------------------------------------------------------
    # M030 era: opencode v1.14 reads response from POST body, not SSE
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text_from_parts(parts: list) -> str:
        """Concatenate text content from opencode message parts.

        opencode v1.14 part shapes seen in the wild:
          {type: 'step-start', ...}        → ignore
          {type: 'text', text: '...'}      → yield text
          {type: 'reasoning', text: '...'} → reasoning trace, optional
          {type: 'tool-call', ...}         → tool invocation, surface separately
          {type: 'tool-result', ...}       → tool output
          {type: 'file', ...}              → file ref

        For the chat surface, the user-visible answer comes from 'text'
        parts. We surface reasoning only if EMIT_TOOL_STATUS-ish flag
        — for now keep simple and only emit text.
        """
        out = []
        for p in (parts or []):
            if not isinstance(p, dict):
                continue
            ptype = p.get("type", "")
            if ptype == "text":
                t = p.get("text") or p.get("content")
                if isinstance(t, str) and t:
                    out.append(t)
        return "".join(out)

    async def _collect_post_response(self, headers: dict, msg_url: str,
                                     msg_payload: dict) -> str:
        """Non-streaming: POST + read full response body, extract text parts."""
        timeout = aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(msg_url, headers=headers, json=msg_payload) as r:
                    r.raise_for_status()
                    data = await r.json()
            text = self._extract_text_from_parts(data.get("parts") or [])
            if text:
                return text
            # No text parts — check if it's an error info or just an empty reply
            info = data.get("info") or {}
            if info.get("error"):
                return f"⚠️ OpenCode error: {info.get('error')}"
            return "(no content)"
        except aiohttp.ClientResponseError as e:
            return f"⚠️ OpenCode HTTP {e.status}: {e.message}"
        except aiohttp.ClientError as e:
            return f"⚠️ OpenCode network error: {e}"
        except asyncio.TimeoutError:
            return "⚠️ OpenCode request timed out."

    async def _stream_post_response(self, headers: dict, msg_url: str,
                                    msg_payload: dict,
                                    event_emitter: Optional[Callable[[dict], Awaitable[None]]]):
        """Streaming-ish: POST + read full body, yield text parts.

        opencode-serve v1.14 doesn't expose per-token streaming on this
        endpoint. We yield the whole answer in one or more chunks (one
        per text-part) so OWUI's manifold-pipe runtime sees an async
        generator (avoiding the sync-buffering issue that caused
        "connection lost, retrying" on prod earlier today).
        """
        timeout = aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(msg_url, headers=headers, json=msg_payload) as r:
                    r.raise_for_status()
                    data = await r.json()
            for p in (data.get("parts") or []):
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    t = p.get("text") or p.get("content")
                    if isinstance(t, str) and t:
                        yield t
                elif p.get("type") in ("tool-call", "tool-result", "tool.call", "tool.result") \
                        and self.valves.EMIT_TOOL_STATUS and event_emitter:
                    tname = p.get("tool") or p.get("name") or p.get("type")
                    self._emit_safe(event_emitter, {
                        "type": "status",
                        "data": {"description": f"🔧 {p.get('type')}: {tname}",
                                 "done": p.get('type', '').endswith('result')},
                    })
            # Surface errors that came back in info.error
            info = data.get("info") or {}
            if info.get("error"):
                yield f"\n\n⚠️ OpenCode error: {info.get('error')}"
        except aiohttp.ClientResponseError as e:
            yield f"⚠️ OpenCode HTTP {e.status}: {e.message}"
        except aiohttp.ClientError as e:
            yield f"⚠️ OpenCode network error: {e}"
        except asyncio.TimeoutError:
            yield "⚠️ OpenCode request timed out."

    # ------------------------------------------------------------------
    # Legacy SSE-based methods — kept for reference / future opencode revs
    # that may re-add per-token streaming. Not called from pipe() anymore.
    # ------------------------------------------------------------------

    async def _stream_events(self, base_url: str, headers: dict, session_id: str,
                             msg_url: str, msg_payload: dict,
                             event_emitter: Optional[Callable[[dict], Awaitable[None]]]):
        """Async generator: yield string deltas as opencode events arrive.

        Critical: open the SSE GET *before* POSTing the message so no early
        `message.part.updated` events are lost in the gap between POST return
        and SSE subscription. Otherwise quick replies (or already-cached
        responses) can complete before we're listening, leaving the UI hung.

        Sync-generator variant (v1.0.x) caused intermittent "connection lost,
        retrying" in OpenWebUI — the runtime buffers sync yields and long
        quiet periods between events looked like a dropped connection.
        """
        timeout = aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 1) Open SSE first.
                sse_resp = await session.get(base_url + _EVENT_STREAM,
                                             headers=headers)
                sse_resp.raise_for_status()
                # 2) Now POST the user message — events from this turn will
                #    arrive on the already-open SSE stream.
                try:
                    async with session.post(msg_url, headers=headers,
                                            json=msg_payload) as post_resp:
                        post_resp.raise_for_status()
                except aiohttp.ClientError as e:
                    sse_resp.close()
                    yield f"\n\n⚠️ OpenCode network error queuing message: {e}"
                    return

                # 3) Drain SSE until session.idle / message.completed.
                buf = b""
                try:
                    async for chunk_bytes in sse_resp.content.iter_any():
                        buf += chunk_bytes
                        while b"\n" in buf:
                            line_b, _, buf = buf.partition(b"\n")
                            line = line_b.decode("utf-8", errors="replace").rstrip("\r")
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                evt = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            props = evt.get("properties") or {}
                            if props.get("sessionID") != session_id \
                               and evt.get("session_id") != session_id:
                                continue
                            etype = str(evt.get("type") or "")

                            if etype == "message.part.updated":
                                part = props.get("part") or {}
                                text = part.get("text") or part.get("content")
                                if isinstance(text, str) and text:
                                    yield text
                            elif etype in ("tool.call", "tool.result", "tool.update") \
                                    and self.valves.EMIT_TOOL_STATUS and event_emitter:
                                tname = props.get("tool", "tool")
                                self._emit_safe(event_emitter, {
                                    "type": "status",
                                    "data": {"description": f"🔧 {etype}: {tname}",
                                             "done": etype == "tool.result"},
                                })
                            elif etype in ("session.idle", "message.completed"):
                                return
                finally:
                    sse_resp.close()
        except aiohttp.ClientResponseError as e:
            yield f"\n\n⚠️ OpenCode HTTP {e.status}: {e.message}"
        except aiohttp.ClientError as e:
            yield f"\n\n⚠️ OpenCode stream error: {e}"
        except asyncio.TimeoutError:
            yield "\n\n⚠️ OpenCode stream timed out."

    async def _collect_events(self, base_url: str, headers: dict, session_id: str,
                              msg_url: str, msg_payload: dict) -> str:
        """Blocking (non-streaming) path. Open SSE first, then POST,
        then accumulate text parts until session done."""
        chunks: list[str] = []
        timeout = aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                sse_resp = await session.get(base_url + _EVENT_STREAM,
                                             headers=headers)
                sse_resp.raise_for_status()
                try:
                    async with session.post(msg_url, headers=headers,
                                            json=msg_payload) as post_resp:
                        post_resp.raise_for_status()
                except aiohttp.ClientError as e:
                    sse_resp.close()
                    return f"⚠️ OpenCode network error queuing message: {e}"

                buf = b""
                try:
                    async for chunk_bytes in sse_resp.content.iter_any():
                        buf += chunk_bytes
                        while b"\n" in buf:
                            line_b, _, buf = buf.partition(b"\n")
                            line = line_b.decode("utf-8", errors="replace").rstrip("\r")
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                evt = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            props = evt.get("properties") or {}
                            if props.get("sessionID") != session_id \
                               and evt.get("session_id") != session_id:
                                continue
                            etype = str(evt.get("type") or "")
                            if etype == "message.part.updated":
                                part = props.get("part") or {}
                                text = part.get("text") or part.get("content")
                                if isinstance(text, str) and text:
                                    chunks.append(text)
                            elif etype in ("session.idle", "message.completed"):
                                return "".join(chunks) or "(no content)"
                finally:
                    sse_resp.close()
        except aiohttp.ClientError as e:
            return f"⚠️ OpenCode collection error: {e}"
        except asyncio.TimeoutError:
            return "⚠️ OpenCode collection timed out."
        return "".join(chunks) or "(no content)"

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_query(messages: list[dict]) -> str:
        for m in reversed(messages or []):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
        return ""

    @staticmethod
    def _emit_safe(emitter, event):
        try:
            coro = emitter(event)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(coro)
                else:
                    loop.run_until_complete(coro)
            except RuntimeError:
                asyncio.run(coro)
        except Exception as e:
            log.debug("emit failed: %s", e)
