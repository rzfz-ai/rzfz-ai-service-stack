"""
id: openlit_filter
title: OpenLIT OTLP Filter
author: razzfazz-stack
version: 1.0.0
required_open_webui_version: 0.5.0
license: MIT
description: Global outlet filter — emits one OTLP/HTTP span per chat to the OTel collector (http://otel-collector:4318/v1/traces) which writes it to ClickHouse for OpenLIT (openlit ships no OTLP receiver — #193), with model, user, chat_id, latency, token usage. Optional PII scrubbing of prompts/completions via OBSERVABILITY_PII_FILTER=true. Fire-and-forget; fail-open if the collector is down.
requirements: pydantic
"""

# This is a Pipelines Filter (lives in the openwebui-pipelines container,
# distributed via the openwebui-seed sidecar's Duty B). It runs against EVERY
# model in the picker via `pipelines = ["*"]`, capturing chats from Dify,
# GPUStack, hermes, moltis, opencode — anything that flows through OpenWebUI.
#
# Direct backend → GPUStack calls (autonomous agent turns, etc.) bypass the
# pipe layer entirely and are NOT captured here. See M019-SPEC § "Decisions
# resolved" for the per-backend SDK-instrumentation follow-up plan.
#
# Implementation notes:
# - Uses urllib.request (stdlib) instead of httpx/requests so the pipelines
#   container needs no extra `pip install` step beyond pydantic.
# - inlet captures start time keyed on (chat_id, model) into a thread-safe
#   dict; outlet retrieves it and emits the span. Loose entries time out
#   after 5 min to bound memory.
# - All network I/O happens inside a daemon thread with a 1s timeout —
#   the chat completes regardless of OpenLIT availability.
# - PII scrubbing is regex-based: email, long digit sequences, common
#   API-key shapes. Off by default; on for customer deployments via env.

from __future__ import annotations
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.request
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("openlit_filter")
log.setLevel(logging.INFO)


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int valve default from the environment.

    A malformed or negative value keeps the default and says so: this filter
    sits on the chat hot path, and an operator typo in .env must not stop
    Open WebUI's pipelines container from starting.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning("openlit_filter: %s=%r is not an integer — using %d",
                    name, raw, default)
        return default
    if value < 0:
        log.warning("openlit_filter: %s=%d is negative — using %d",
                    name, value, default)
        return default
    return value

# ----------------------------------------------------------------------------
# PII scrubbers — order matters (longest match first wins)

_PII_PATTERNS = [
    (re.compile(r"\b(?:sk|pk|app)-[A-Za-z0-9_-]{16,}", re.IGNORECASE), "[api_key]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE), "Bearer [token]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    (re.compile(r"\b(?:\+\d{1,3}[\s-]?)?\(?\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}\b"), "[phone]"),
    (re.compile(r"\b\d{13,19}\b"), "[long_number]"),
]


def _scrub(text: str) -> str:
    for pat, repl in _PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _apply_content_policy(
    prompt: str, completion: str, *, max_chars: int, pii_filter: bool,
) -> tuple[str, str]:
    """Apply the excerpt-length + PII governance policy to prompt/completion text.

    `max_chars <= 0` means "capture nothing" — the documented way to disable
    prompt/completion content capture entirely for compliance-sensitive
    deployments (metrics-only: model, tokens, latency, user/chat id still
    flow). Previously `max_chars == 0` was falsy in a bare `if cap:` guard
    and skipped truncation altogether, which sent the FULL untruncated (and
    unscrubbed-unless-the-PII-filter-was-separately-on) text — the opposite
    of "disabled". #245.
    """
    cap = max(0, int(max_chars))
    prompt = prompt[:cap]
    completion = completion[:cap]
    if pii_filter:
        prompt = _scrub(prompt)
        completion = _scrub(completion)
    return prompt, completion


# ----------------------------------------------------------------------------
# Reading what a backend reported: usage blocks and message timestamps

#: The field-name families a usage block can come in. First match wins, and
#: every family is tried on every candidate block before moving to the next
#: block. OpenAI-shaped backends use the first, Ollama-shaped ones the second.
_USAGE_FIELDS = (
    ("prompt_tokens", "completion_tokens"),
    ("prompt_eval_count", "eval_count"),
)

#: Above this, a timestamp cannot be epoch seconds (it would be the year
#: 5138), so it is milliseconds.
_MS_THRESHOLD = 1e11

#: A chat turn longer than this is a clock artefact, not a latency.
_MAX_PLAUSIBLE_LATENCY_MS = 24 * 60 * 60 * 1000


def _read_usage(block: Any) -> Optional[tuple[int, int]]:
    """(in, out) from one usage block, or None if it carries no numbers.

    A block that yields (0, 0) is reported as None on purpose: it is the empty
    `usage` the streaming path leaves on the body, and the caller must be free
    to keep looking for the block that has the numbers.
    """
    if not isinstance(block, dict):
        return None
    for in_field, out_field in _USAGE_FIELDS:
        if in_field in block or out_field in block:
            try:
                in_tok = int(block.get(in_field) or 0)
                out_tok = int(block.get(out_field) or 0)
            except (TypeError, ValueError):
                continue
            if in_tok or out_tok:
                return in_tok, out_tok
    return None


def _usage_blocks(body: dict) -> list[Any]:
    """Every place a usage block is known to sit, most authoritative first."""
    blocks: list[Any] = [body.get("usage")]
    messages = body.get("messages") or []
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("usage") is not None:
            blocks.append(m.get("usage"))
    return blocks


def _epoch_ms(value: Any) -> Optional[float]:
    """A message timestamp in milliseconds, whatever unit it arrived in."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return ts if ts > _MS_THRESHOLD else ts * 1000.0


# ----------------------------------------------------------------------------
# In-memory inlet→outlet timing store

_T0_LOCK = threading.Lock()
_T0: dict[str, float] = {}
_T0_MAX_AGE = 300.0  # 5 min — drop unmatched inlet entries beyond this


def _inlet_remember(key: str, ts: float) -> None:
    # ONE clock, and it is the caller's: `inlet` stores `time.monotonic()`, so
    # the staleness comparison must use `time.monotonic()` too. It used to read
    # `time.time()`, and the two are not the same magnitude — epoch seconds
    # against seconds since boot. `now - t` was therefore ~1.8e9 for the entry
    # written on the line above, every entry counted as stale, and the store
    # was wiped on every single inlet. `_outlet_recall` never found anything,
    # so EVERY span carried `Duration=0` — for every chat, on every box, since
    # this filter shipped (#1403).
    now = time.monotonic()
    with _T0_LOCK:
        _T0[key] = ts
        # Opportunistic cleanup of stale entries
        stale = [k for k, t in _T0.items() if now - t > _T0_MAX_AGE]
        for k in stale:
            _T0.pop(k, None)


def _outlet_recall(key: str) -> Optional[float]:
    with _T0_LOCK:
        return _T0.pop(key, None)


# ----------------------------------------------------------------------------
# OTLP span emit (fire-and-forget)

OTLP_TIMEOUT_S = 1.0
EMITTER_THREADS = threading.BoundedSemaphore(value=8)


def _emit_span_async(endpoint: str, payload: dict) -> None:
    def _worker() -> None:
        if not EMITTER_THREADS.acquire(blocking=False):
            return  # too many in flight — drop rather than block the chat
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=OTLP_TIMEOUT_S).read()
        except Exception as e:
            log.debug("openlit_filter: span emit failed (fail-open): %s", e)
        finally:
            EMITTER_THREADS.release()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _attr_str(k: str, v: Any) -> dict:
    return {"key": k, "value": {"stringValue": str(v)}}


def _attr_int(k: str, v: int) -> dict:
    return {"key": k, "value": {"intValue": str(v)}}


def _build_span(
    *,
    model: str,
    chat_id: str,
    user_id: str,
    user_name: str,
    backend: str,
    latency_ms: int,
    in_tokens: int,
    out_tokens: int,
    error_class: Optional[str],
    prompt_excerpt: Optional[str],
    completion_excerpt: Optional[str],
) -> dict:
    now_ns = time.time_ns()
    start_ns = now_ns - latency_ms * 1_000_000
    span_id = secrets.token_hex(8)
    trace_id = secrets.token_hex(16)

    attrs = [
        _attr_str("gen_ai.system", backend),
        _attr_str("gen_ai.request.model", model),
        _attr_int("gen_ai.usage.input_tokens", in_tokens),
        _attr_int("gen_ai.usage.output_tokens", out_tokens),
        _attr_int("latency.ms", latency_ms),
        _attr_str("user.id", user_id),
        _attr_str("user.name", user_name),
        _attr_str("chat.id", chat_id),
    ]
    if error_class:
        attrs.append(_attr_str("error.class", error_class))

    events: list[dict] = []
    if prompt_excerpt:
        events.append({
            "timeUnixNano": str(start_ns),
            "name": "gen_ai.content.prompt",
            "attributes": [_attr_str("gen_ai.prompt", prompt_excerpt)],
        })
    if completion_excerpt:
        events.append({
            "timeUnixNano": str(now_ns),
            "name": "gen_ai.content.completion",
            "attributes": [_attr_str("gen_ai.completion", completion_excerpt)],
        })

    return {
        "resourceSpans": [{
            "resource": {"attributes": [
                _attr_str("service.name", "razzfazz-openwebui-chat"),
                _attr_str("service.version", "1.0.0"),
            ]},
            "scopeSpans": [{
                "scope": {"name": "razzfazz.openlit_filter", "version": "1.0.0"},
                "spans": [{
                    "traceId": trace_id,
                    "spanId": span_id,
                    "name": f"chat.{model}",
                    "kind": 3,  # CLIENT
                    "startTimeUnixNano": str(start_ns),
                    "endTimeUnixNano": str(now_ns),
                    "attributes": attrs,
                    "events": events,
                    "status": {"code": 2 if error_class else 1},
                }],
            }],
        }]
    }


# ----------------------------------------------------------------------------
# Pipelines Filter contract

class Pipeline:

    class Valves(BaseModel):
        pipelines: list[str] = Field(default_factory=lambda: ["*"])
        priority: int = Field(default=0)
        OPENLIT_OTLP_ENDPOINT: str = Field(default="http://otel-collector:4318/v1/traces")
        OBSERVABILITY_PII_FILTER: bool = Field(default=False)
        EXCERPT_MAX_CHARS: int = Field(
            default=2000,
            description=(
                "Max chars of prompt/completion text sent to OpenLIT. Set to "
                "0 to disable prompt/completion content capture entirely "
                "(governed toggle for privacy/compliance — model, token "
                "counts, latency, and user/chat id still flow either way)."
            ),
        )
        DEBUG: bool = Field(default=False)

    def __init__(self) -> None:
        self.type = "filter"
        self.id = "openlit_filter"
        self.name = "OpenLIT OTLP Filter"
        # Pull defaults from environment so the Pipelines admin UI shows them
        # but operator-set values still win.
        self.valves = self.Valves(
            OPENLIT_OTLP_ENDPOINT=os.environ.get(
                "OPENLIT_OTLP_ENDPOINT", "http://otel-collector:4318/v1/traces"
            ),
            OBSERVABILITY_PII_FILTER=os.environ.get(
                "OBSERVABILITY_PII_FILTER", "false"
            ).strip().lower() == "true",
            # Read from the environment like its sibling valve. Without this the
            # "governed toggle" was reachable ONLY through the Pipelines admin
            # UI — no .env key, nothing in compose — while the shipped default
            # is capture-ON at 2000 chars with PII scrubbing off, and that
            # content lands in ClickHouse for 7 days. 0 disables content
            # capture entirely. A malformed value keeps the default rather than
            # crashing the filter on startup.
            EXCERPT_MAX_CHARS=_env_int(
                "OBSERVABILITY_EXCERPT_MAX_CHARS", default=2000),
        )

    # ------------------------------------------------------------------
    async def on_startup(self) -> None:
        log.info(
            "openlit_filter: starting; endpoint=%s pii_filter=%s",
            self.valves.OPENLIT_OTLP_ENDPOINT, self.valves.OBSERVABILITY_PII_FILTER,
        )

    async def on_shutdown(self) -> None:
        pass

    # ------------------------------------------------------------------
    @staticmethod
    def _key(body: dict, user: Optional[dict]) -> str:
        chat_id = (body.get("metadata") or {}).get("chat_id") or body.get("chat_id") or ""
        model = body.get("model") or ""
        uid = (user or {}).get("id") or "anonymous"
        return f"{uid}::{chat_id}::{model}"

    @staticmethod
    def _backend_from_model(model: str) -> str:
        # "dify.allgemein-chat" → "dify"
        # "openlit.openlit"       → "openlit"
        # plain "qwen3-coder-next" → "gpustack"
        return model.split(".", 1)[0] if "." in model else "gpustack"

    @staticmethod
    def _last_user_message(messages: list[dict]) -> str:
        for m in reversed(messages or []):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return " ".join(part.get("text", "") for part in c if part.get("type") == "text")
        return ""

    @staticmethod
    def _completion_text(body: dict) -> str:
        # Open WebUI's outlet body convention varies; try a few common shapes.
        for k in ("completion", "response", "answer"):
            v = body.get(k)
            if isinstance(v, str):
                return v
        msgs = body.get("messages") or []
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                c = m.get("content")
                if isinstance(c, str):
                    return c
        return ""

    @staticmethod
    def _usage(body: dict) -> tuple[int, int]:
        """Token counts out of an Open WebUI outlet body.

        Two things vary, and this used to assume one value for each.

        WHERE: the block is on the outlet body only for some backends. Open
        WebUI 0.11 attaches it to the ASSISTANT MESSAGE instead
        (`messages[-1]["usage"]`), which is why a real chat over the manager
        router produced `input_tokens=0 output_tokens=0` on a span that
        otherwise carried the full attribution (#1403, measured on 0.91).

        WHAT IT IS CALLED: OpenAI-shaped backends say
        `prompt_tokens`/`completion_tokens`, Ollama-shaped ones say
        `prompt_eval_count`/`eval_count`. Both reach Open WebUI, and Open WebUI
        passes on what it got.

        So: look in both places, accept either family, and keep looking past a
        block that answers (0, 0) — an empty `usage` dict on the body is
        exactly the shape the streaming path leaves behind, and letting it
        shadow the real numbers on the message would reproduce the defect.
        Adding a third naming family is one tuple in `_USAGE_FIELDS`.

        The stream itself is not this function's problem: the manager proxy
        forces `stream_options.include_usage` on every streamed request
        through `/v1` (`app/proxy.py`, and `api/playground.py` for #1184), so
        the numbers do arrive — see the guard in the #1403 test.
        """
        for candidate in _usage_blocks(body):
            got = _read_usage(candidate)
            if got is not None:
                return got
        return 0, 0

    @staticmethod
    def _latency_from_messages(messages: list[dict]) -> int:
        """Fallback latency in ms, from the outlet body's own timestamps.

        `outlet` normally subtracts the `t0` that `inlet` stored. It cannot
        when no `inlet` ran for this turn under the same key — the backend
        flow (task fan-out, a `session_id` instead of a `chat_id`) is the
        reported case (#1403). The messages carry their own clock: the last
        user turn and the assistant turn that answered it.

        Open WebUI writes those timestamps in seconds; other producers write
        milliseconds. Both are accepted — a value past `_MS_THRESHOLD` is too
        large to be epoch SECONDS for any date this software will see, so it
        is milliseconds. A delta that is negative or absurd (a clock step, a
        replayed history) yields 0 rather than a fabricated duration: an
        obviously-missing number is better than a plausible wrong one.
        """
        user_ts = assistant_ts = None
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            ts = _epoch_ms(m.get("timestamp"))
            if ts is None:
                continue
            role = m.get("role")
            if role == "user":
                user_ts = ts
            elif role == "assistant" and user_ts is not None:
                assistant_ts = ts
        if user_ts is None or assistant_ts is None:
            return 0
        delta = assistant_ts - user_ts
        if delta <= 0 or delta > _MAX_PLAUSIBLE_LATENCY_MS:
            return 0
        return int(delta)

    # ------------------------------------------------------------------
    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        _inlet_remember(self._key(body, user), time.monotonic())
        return body

    async def outlet(self, body: dict, user: Optional[dict] = None) -> dict:
        try:
            t0 = _outlet_recall(self._key(body, user))
            latency_ms = int((time.monotonic() - t0) * 1000) if t0 else 0
            if not latency_ms:
                # No matching inlet for this turn (the backend flow keys on a
                # session, not a chat) — ask the body's own timestamps (#1403).
                latency_ms = self._latency_from_messages(body.get("messages") or [])
            model = body.get("model") or "unknown"
            chat_id = (body.get("metadata") or {}).get("chat_id") or body.get("chat_id") or ""
            uid = (user or {}).get("id") or "anonymous"
            uname = (user or {}).get("name") or uid
            in_tok, out_tok = self._usage(body)
            err = body.get("error")
            err_class = type(err).__name__ if isinstance(err, BaseException) else (
                str(err.get("type")) if isinstance(err, dict) and err.get("type") else None
            )

            prompt = self._last_user_message(body.get("messages") or [])
            completion = self._completion_text(body)
            prompt, completion = _apply_content_policy(
                prompt, completion,
                max_chars=self.valves.EXCERPT_MAX_CHARS,
                pii_filter=self.valves.OBSERVABILITY_PII_FILTER,
            )

            payload = _build_span(
                model=model, chat_id=chat_id, user_id=uid, user_name=uname,
                backend=self._backend_from_model(model),
                latency_ms=latency_ms,
                in_tokens=in_tok, out_tokens=out_tok,
                error_class=err_class,
                prompt_excerpt=prompt or None,
                completion_excerpt=completion or None,
            )
            _emit_span_async(self.valves.OPENLIT_OTLP_ENDPOINT, payload)
            if self.valves.DEBUG:
                log.info("openlit_filter: emitted span model=%s latency=%dms", model, latency_ms)
        except Exception as e:
            # Filter must never break a chat completion — log and move on.
            log.warning("openlit_filter outlet error (fail-open): %s", e)
        return body
