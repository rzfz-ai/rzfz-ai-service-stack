"""
id: moltis
title: Moltis Manifold Pipe
author: razzfazz-stack
version: 2.1.0
required_open_webui_version: 0.5.0
license: MIT
description: Per-user routing of chats to moltis 20260429+ via its WebSocket /ws/chat protocol. API key + model + thinking-emit are PER-USER (UserValves) since each user has their own moltis instance. Streams response deltas from the chat events stream filtered by runId.
requirements: aiohttp, pydantic
"""

# M020 S04 — Moltis pipe (v2 for moltis 20260429+).
#
# Replaces the v1 JSON-RPC `/rpc chat.send` pipe (v1 was written against an
# earlier moltis-org/moltis API that no longer exists). The current moltis is
# a session/cookie/WS webapp; this pipe uses its operator API:
#
#   * Auth:    Bearer <api_key>  on every request, including the WS upgrade.
#              The key MUST have `operator.read + operator.write` scopes —
#              moltis "Full Access" UI label does NOT attach scopes (verified
#              the hard way 2026-05-12); pick the checkboxes explicitly.
#   * WS path: /ws/chat
#   * Frame:   {"type":"req","id":<uuid-string>,"method":"...","params":{...}}
#              Server replies with {"type":"res","id":<echo>,"ok":bool,"payload"|"error":...}
#              Server pushes events as {"type":"event","event":"chat","payload":{...}}
#   * IDs:     MUST be strings (moltis Rust deserializer rejects ints with
#              "invalid type: integer 1, expected a string").
#   * Flow:    1) connect handshake (req method=connect with protocol min/max + client info)
#              2) subscribe to events (req method=subscribe params.events=["chat"])
#              3) send the message (req method=chat.send params.message + params.model)
#              4) drain "event"/"chat" frames; filter by payload.runId == our runId;
#                 yield payload.text when state=="delta"; stop on completion
#                 metrics frame (no `state`, has `durationMs`).
#
# API key is sourced from the pipe Valves (operator-set in OWUI Admin). Long-term
# this should move into agent-manager so /api/find/moltis returns auth.token =
# api_key, but tonight's pragmatic approach is a Valve.

# @include _lib/per_user_routing.py
#
# Note: pipes that use `# @include` MUST NOT carry their own `from __future__`
# imports — see chat/seeder/seed.py expand_includes() for rationale.

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from pydantic import BaseModel, Field

log = logging.getLogger("moltis_pipe")

_WS_PATH = "/ws/chat"


class Pipe:

    class Valves(BaseModel):
        # Admin-set, applies to all users. Technical knobs only — credentials
        # and model choice live in UserValves below because every user has
        # their own moltis instance with its own API key.
        REQUEST_TIMEOUT: int = Field(default=300)
        DEBUG: bool = Field(default=False)

    class UserValves(BaseModel):
        # Per-user. Set in OWUI chat → user menu → "Valves" panel for the
        # Moltis function (each user must paste their OWN moltis API key —
        # one key only authenticates against ONE per-user moltis instance).
        API_KEY: str = Field(
            default="",
            description="Moltis API key with operator.read + operator.write scopes. "
                        "Generate in your moltis web UI → Settings → Security → API "
                        "Keys; tick BOTH scope checkboxes (the 'Full Access' label "
                        "does NOT attach scopes — verified the hard way).",
        )
        # Moltis uses "<provider>::<model>" format; the provider must be
        # configured in your moltis web UI → Settings → Providers.
        MODEL: str = Field(
            default="openai::qwen3.6",
            description='Default model, format "<provider>::<model>". Must exist in your moltis providers config.',
        )
        # Surface moltis' incremental reasoning trace. Disabled by default
        # because thinking_text events are CUMULATIVE (not deltas) — naive
        # streaming would re-render the entire reasoning on every event;
        # we emit diffs but it's still verbose.
        EMIT_THINKING: bool = Field(
            default=False,
            description="Stream the model's reasoning text alongside the answer. Verbose; off by default.",
        )

    def __init__(self) -> None:
        self.type = "manifold"
        self.id = "moltis"
        self.name = "Moltis: "
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        return [{"id": "personal", "name": "Personal"}]

    # ------------------------------------------------------------------
    async def pipe(self, body: dict,
             __user__: Optional[dict] = None,
             __metadata__: Optional[dict] = None,
             __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
             __task__: Optional[str] = None,
             **kwargs: Any):
        __user__ = __user__ or {}
        stream = bool(body.get("stream", False))

        # Per-user settings: __user__['valves'] is a UserValves Pydantic
        # instance populated by OWUI from user.settings.functions.valves.<id>.
        # If the user hasn't set anything, OWUI passes UserValves() with
        # defaults — so this attribute access never KeyErrors.
        uv = __user__.get("valves")
        api_key = (uv.API_KEY.strip() if uv else "")
        model = (uv.MODEL if uv and uv.MODEL else "openai::qwen3.6")
        emit_thinking = bool(uv.EMIT_THINKING if uv else False)

        # 1) Find the user's moltis instance via agent-manager
        result = find("moltis", __user__)
        if isinstance(result, NotProvisioned):
            return result.msg
        if not result.internal_url:
            return ("⚠️ Moltis instance found but the catalog port mapping is missing "
                    "the primary `internal` slot.")

        # 2) Fallback: agent-manager could one day hand out api_key tokens
        #    (auth.token starting with `mk_`). Use that if no UserValve set.
        if not api_key and getattr(result.auth, "token", "") and result.auth.token.startswith("mk_"):
            api_key = result.auth.token
        if not api_key:
            return ("⚠️ **Your Moltis API key isn't set.** Each user has their "
                    "own moltis instance, so you need to paste your OWN key "
                    "(other users' keys won't work):\n\n"
                    "1. Open your moltis web UI (Agents dashboard → Moltis → Open).\n"
                    "2. Settings → Security → API Keys → New key.\n"
                    "3. **Explicitly tick** `operator.read` + `operator.write` "
                    "scopes (the 'Full Access' label does not attach scopes).\n"
                    "4. Copy the key.\n"
                    "5. In this OWUI chat: click your name (bottom-left) → "
                    "Settings → Valves → Moltis → paste into `API_KEY`.")

        # 3) Build WS URL (http→ws / https→wss)
        base = result.internal_url.rstrip("/")
        ws_url = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + _WS_PATH

        query = self._extract_query(body.get("messages") or [])
        if not query:
            return "⚠️ Empty user message — nothing to send to Moltis."

        if stream:
            return self._stream(ws_url, api_key, query, model, emit_thinking)
        return await self._collect(ws_url, api_key, query, model, emit_thinking)

    # ------------------------------------------------------------------
    async def _stream(self, ws_url: str, api_key: str, query: str,
                      model: str, emit_thinking: bool):
        """Async generator: yield content deltas for our runId."""
        timeout = aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT)
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(ws_url, headers=headers) as ws:
                    # 1. connect handshake
                    err = await self._handshake(ws)
                    if err:
                        yield err
                        return
                    # 2. subscribe to chat events (must be done BEFORE chat.send
                    #    so we don't miss the early `queued` / `thinking` frames)
                    err = await self._subscribe(ws, ["chat"])
                    if err:
                        yield err
                        return
                    # 3. fire chat.send and capture runId
                    run_id, err = await self._send_chat(ws, query, model)
                    if err:
                        yield err
                        return
                    if not run_id:
                        # Some moltis versions return runId only via events
                        # (the `chat.send` response has ok:true but no runId).
                        # We then accept ANY runId until completion. Best-effort.
                        if self.valves.DEBUG:
                            log.info("moltis chat.send returned no runId; "
                                     "matching all events until completion")

                    # 4. drain events until completion
                    last_thinking = ""
                    seen_completion = False
                    async for raw in ws:
                        if raw.type != aiohttp.WSMsgType.TEXT:
                            if raw.type in (aiohttp.WSMsgType.CLOSED,
                                             aiohttp.WSMsgType.ERROR,
                                             aiohttp.WSMsgType.CLOSE):
                                break
                            continue
                        try:
                            frame = json.loads(raw.data)
                        except json.JSONDecodeError:
                            continue
                        if frame.get("type") != "event" or frame.get("event") != "chat":
                            continue
                        payload = frame.get("payload") or {}
                        # Filter by runId (when known)
                        if run_id and payload.get("runId") not in (run_id, None):
                            continue
                        state = payload.get("state")
                        if state == "delta":
                            text = payload.get("text") or ""
                            if text:
                                yield text
                        elif state == "thinking_text" and emit_thinking:
                            # Cumulative — emit only the diff vs. last seen
                            full = payload.get("text") or ""
                            if full.startswith(last_thinking):
                                inc = full[len(last_thinking):]
                                if inc:
                                    yield inc
                                last_thinking = full
                            else:
                                # Thinking restarted (new iteration); emit full
                                if full and full != last_thinking:
                                    yield full
                                last_thinking = full
                        elif state == "thinking_done":
                            # End of reasoning — model now writes the answer
                            last_thinking = ""
                        elif state in ("done", "complete", "final"):
                            seen_completion = True
                            break
                        elif state is None and payload.get("durationMs") is not None:
                            # Metrics frame — last event of the run
                            seen_completion = True
                            break
                        elif state == "error":
                            err_msg = payload.get("error") or payload.get("message") or "moltis run error"
                            yield f"\n\n⚠️ Moltis: {err_msg}"
                            break
                    if not seen_completion and self.valves.DEBUG:
                        log.info("moltis stream ended without completion event")
        except aiohttp.WSServerHandshakeError as e:
            yield f"⚠️ Moltis WS upgrade failed (HTTP {e.status}). Verify API_KEY scopes include operator.read + operator.write."
        except aiohttp.ClientError as e:
            yield f"⚠️ Moltis network error: {e}"
        except asyncio.TimeoutError:
            yield "⚠️ Moltis request timed out."
        except Exception as e:
            log.exception("moltis_pipe stream error")
            yield f"⚠️ Moltis pipe error: {e}"

    # ------------------------------------------------------------------
    async def _collect(self, ws_url: str, api_key: str, query: str,
                       model: str, emit_thinking: bool) -> str:
        """Non-streaming path: accumulate deltas, return as one string."""
        chunks: list[str] = []
        async for piece in self._stream(ws_url, api_key, query, model, emit_thinking):
            chunks.append(piece)
        return "".join(chunks) or "(no content)"

    # ------------------------------------------------------------------
    @staticmethod
    async def _send_req(ws, method: str, params: dict) -> tuple[str, dict]:
        """Send a req frame, return (id, response payload-or-error dict).

        Note `id` MUST be a string — moltis rejects integer ids with
        'invalid type: integer 1, expected a string'.
        """
        rid = str(uuid.uuid4())
        await ws.send_json({"type": "req", "id": rid, "method": method, "params": params})
        # Wait for the matching response (skipping interleaved events)
        while True:
            msg = await ws.receive()
            if msg.type != aiohttp.WSMsgType.TEXT:
                return rid, {"_ws": msg.type}
            try:
                frame = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "res" and frame.get("id") == rid:
                return rid, frame
            # Interleaved event — for handshake/subscribe phase we drop these.
            # The caller is responsible for re-subscribing if it needs them.

    async def _handshake(self, ws) -> Optional[str]:
        _, frame = await self._send_req(ws, "connect", {
            "protocol": {"min": 3, "max": 4},
            "client": {"id": "owui-moltis-pipe", "version": "2.0.0",
                       "platform": "server", "mode": "operator"},
            "locale": "en",
            "timezone": "UTC",
        })
        if not frame.get("ok"):
            err = frame.get("error") or {}
            return f"⚠️ Moltis connect failed: {err.get('code','?')} {err.get('message','')}"
        return None

    async def _subscribe(self, ws, events: list[str]) -> Optional[str]:
        _, frame = await self._send_req(ws, "subscribe", {"events": events})
        if not frame.get("ok"):
            err = frame.get("error") or {}
            return f"⚠️ Moltis subscribe failed: {err.get('code','?')} {err.get('message','')}"
        return None

    async def _send_chat(self, ws, query: str, model: str) -> tuple[Optional[str], Optional[str]]:
        _, frame = await self._send_req(ws, "chat.send", {
            "message": query,
            "model": model,
        })
        if not frame.get("ok"):
            err = frame.get("error") or {}
            return None, f"⚠️ Moltis chat.send failed: {err.get('code','?')} {err.get('message','')}"
        run_id = (frame.get("payload") or {}).get("runId")
        return run_id, None

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
