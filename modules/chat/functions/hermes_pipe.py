"""
id: hermes
title: Hermes Manifold Pipe
author: razzfazz-stack
version: 2.1.0
required_open_webui_version: 0.5.0
license: MIT
description: Per-user routing of chats to the calling user's personal Hermes agent (NousResearch). Pass-through to the agent's OpenAI-compatible /v1/chat/completions endpoint. Cold-start returns a clickable launch link if the user hasn't provisioned an agent yet.
requirements: aiohttp, pydantic
"""

# M020 S03 — Hermes pipe (v2).
#
# Routes chats to the calling user's per-user hermes-agent container,
# provisioned via agent-manager (per-user instance of the `hermes` agent
# type from M020 S02). hermes-agent exposes a standard OpenAI-compatible
# /v1/chat/completions endpoint at port 8642 ("agent_internal"). This pipe
# is a thin pass-through: forward OpenWebUI's body (messages array +
# stream flag) to the agent, return the OpenAI-format response.
#
# Empirically validated 2026-05-11 against the live hermes-agent on prod.
# Earlier v1 of this pipe targeted a hypothetical {message, session_id}
# schema at /v1/chat which doesn't exist — replaced wholesale.

# @include _lib/per_user_routing.py
#
# Note: pipes that use `# @include` MUST NOT carry their own `from __future__`
# imports — Python requires those at the very top of the file, which they
# can't be once the include block expands above them. The seeder strips
# `from __future__` lines from inlined helpers; pipes themselves should just
# avoid the directive.

import json
import logging
from typing import Any, Awaitable, Callable, Optional

import aiohttp
import requests
from pydantic import BaseModel, Field

log = logging.getLogger("hermes_pipe")

_CHAT_ENDPOINT = "/v1/chat/completions"
_HEALTH_ENDPOINT = "/health"
_MODEL_NAME = "hermes-agent"


class Pipe:
    class Valves(BaseModel):
        REQUEST_TIMEOUT: int = Field(default=300)
        DEBUG: bool = Field(default=False)

    def __init__(self) -> None:
        self.type = "manifold"
        self.id = "hermes"
        self.name = "Hermes: "
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

        # Route to the calling user's per-user hermes-agent gateway.
        # use_extra_port="agent_internal" picks the catalog's :8642 slot
        # (NOT the workspace-UI :3000 slot, which is the companion).
        result = find("hermes", __user__, use_extra_port="agent_internal")
        if isinstance(result, NotProvisioned):
            return result.msg
        if not result.internal_url:
            return ("⚠️ Hermes agent found but the catalog port mapping is missing "
                    "an `agent_internal` slot. Check M020 S02 catalog change.")

        messages = body.get("messages") or []
        if not messages:
            return "⚠️ Empty message list — nothing to send to Hermes."

        # Forward as standard OpenAI chat-completion request.
        url = result.internal_url.rstrip("/") + _CHAT_ENDPOINT
        headers = {**auth_headers(result.auth), "Content-Type": "application/json"}
        payload = {
            "model": _MODEL_NAME,
            "messages": messages,
            "stream": stream,
        }
        # Pass-through optional knobs that OpenWebUI may set.
        for k in ("temperature", "top_p", "max_tokens", "presence_penalty",
                  "frequency_penalty", "stop", "tools", "tool_choice"):
            if k in body and body[k] is not None:
                payload[k] = body[k]

        if self.valves.DEBUG:
            log.info("hermes_pipe → %s body=%s", url,
                     {k: v if k != "messages" else f"<{len(v)} msgs>"
                      for k, v in payload.items()})

        try:
            if stream:
                return self._stream(url, headers, payload)
            r = requests.post(url, headers=headers, json=payload,
                              timeout=self.valves.REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            return self._format_http_error(e)
        except requests.RequestException as e:
            return f"⚠️ Hermes network error: {e}"
        except Exception as e:
            log.exception("hermes_pipe unexpected error")
            return f"⚠️ Hermes pipe error: {e}"

    # ------------------------------------------------------------------
    async def _stream(self, url: str, headers: dict, payload: dict):
        """Stream OpenAI SSE chunks back to OpenWebUI as an async generator.

        OpenWebUI's manifold-pipe streaming contract for ASYNC pipes:
        yield STRING tokens (the content deltas) one at a time. Each
        yield is flushed to the client immediately. With a SYNC generator
        OpenWebUI batches yields and the response appears one-shot —
        async-yield is what triggers per-token rendering.

        Parse `data: {...}` lines, extract `choices[0].delta.content`,
        yield each non-empty delta. Stop on the `data: [DONE]` sentinel.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as r:
                    r.raise_for_status()
                    buf = b""
                    async for chunk_bytes in r.content.iter_any():
                        buf += chunk_bytes
                        # Process complete lines from the buffer.
                        while b"\n" in buf:
                            line_b, _, buf = buf.partition(b"\n")
                            line = line_b.decode("utf-8", errors="replace").rstrip("\r")
                            if not line or not line.startswith("data: "):
                                continue
                            data = line[6:].strip()
                            if data == "[DONE]":
                                return
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = (choices[0].get("delta") or {}).get("content") or ""
                            if delta:
                                yield delta
        except aiohttp.ClientResponseError as e:
            yield f"⚠️ Hermes HTTP {e.status}: {e.message}"
        except aiohttp.ClientError as e:
            yield f"⚠️ Hermes network error during stream: {e}"
        except Exception as e:
            log.exception("hermes_pipe stream error")
            yield f"⚠️ Hermes pipe stream error: {e}"

    # ------------------------------------------------------------------
    @staticmethod
    def _format_http_error(e: requests.HTTPError) -> str:
        try:
            body_text = e.response.text[:500] if e.response is not None else ""
        except Exception:
            body_text = ""
        return f"⚠️ Hermes HTTP {e.response.status_code if e.response else '?'}: {body_text}"
