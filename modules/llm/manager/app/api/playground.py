# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Playground endpoints for the management console (Phase-3 S-B).

A GPUStack-style "try it out" surface: chat / embeddings / rerank against the
fleet, driven from the SSO console. It forwards to the LiteLLM router with the
INTERNAL key injected server-side, so the browser never needs an rzfz-sk key —
and because of that it spends exactly the same fleet capacity as a paying
customer's ``/v1`` call.

**#1024 (audit LLMM-1), operator decision 2026-09-02 — binding, and it resolves
the #501 deferral the other way round: this path goes through the SAME gates as
/v1 traffic. One code path, no special rules.** Concretely, every request here
runs, in this order:

  * the #19 box-wide in-flight concurrency cap — the SAME counter/limit the /v1
    verbs contend for (``proxy.concurrency_gate_for``), so a console user cannot
    push the box past the cap every other consumer is held to;
  * ``proxy.policy_gates`` — the one chain that carries the per-key
    rpm/tpm/budget + model allow-list (#19/M2), the subscription ENTITLEMENT
    gate (#265: ``entitlement_mode=enforce`` + a lapsed subscription now answers
    402 HERE too, not only on /v1) and the strict-metering CAP (503);
  * ``proxy.record_usage`` → ``metering.meter_usage`` — the same function that
    writes a /v1 usage row, so the row carries the same fields, uses the same
    tokenizer backstop when an engine omits ``usage``, and feeds the same tpm /
    budget counters.

The per-key chain has something to enforce against because the reserved
accounting identity is a real ``api_keys`` row: cost-centre
``operator-playground``, key_prefix ``playground-internal`` (migration 0016).
Its rpm/tpm/budget columns are NULL as seeded — so nothing changes for an
operator who never configures it — but an operator who does set them gets them
enforced by ``enforce()`` itself, not by a second limiter that would drift. The
row can never authenticate on /v1: its key_hash is the digest of random bytes
discarded inside the migration, AND ``auth.py`` refuses any status != 'active'.

What that identity buys on the accounting side (#350, kept): playground
consumption lands in the authoritative ``usage_events`` table under a reserved
cost-centre, so chargeback can separate operator diagnostics from customer
traffic — and, since LLMM-1, the SSO identity that spent the tokens rides in the
row's ``request_id`` (``playground:<user>:<uuid>``), because "the console did
it" is not an audit trail when every USER-tier identity (#314) can drive this
path. ``orchestrator_playground_tokens_total`` /
``orchestrator_playground_requests_total`` remain as a per-model view of the
same consumption.

Superseded: the #350 note that the per-key gates "should not apply to an
operator test tool", and the #501 decision of 2026-08-22 (option "banner") that
kept this path serving on a lapsed subscription while /v1 returned 402. Both
rested on the console being an admin-only diagnostics tool; #314's three-tier
RBAC made it USER-tier, at which point "SSO-gated" stopped meaning "operator
only" and the bypass became an unbounded hole in every control /v1 enforces.
Changing this back is a product decision — reopen #1024, do not just delete the
gates.

The upstream client and the reserved key record are resolved from app.state with
production fallbacks so the routes unit-test against a fake ASGI upstream (no
real router, network or DB).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.authz import Role, require_role
from app.config import get_settings

logger = logging.getLogger("orchestrator.playground")

# Upstream budget for one NON-streaming playground call (embeddings, rerank, and
# a chat that did not ask to stream). Generous, but never unbounded — the proxy
# hot path's `timeout=None` is #325.
_TIMEOUT_SECONDS = 120.0

# #1184: the streamed chat path has NO total budget — a 8192-token answer at
# ~49 tok/s legitimately takes ~170 s and the old 120 s wall killed it at ~5900
# tokens while the engine was fine. What is bounded instead is the gap BETWEEN
# chunks (httpx applies `read` per read on a stream): a model emitting tokens
# keeps resetting it, a wedged engine trips it. Sized for a cold model load /
# long prefill before the first token, not for the whole generation.
_STREAM_IDLE_TIMEOUT_SECONDS = 300.0
_STREAM_CONNECT_TIMEOUT_SECONDS = 10.0


class ChatReq(BaseModel):
    model: str
    messages: list[dict]
    max_tokens: Optional[int] = 256
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    # #1184: opt-in SSE passthrough (the console sets it; a caller that omits
    # it gets the JSON answer it always got). Chat only — EmbedReq/RerankReq
    # deliberately do not carry it.
    stream: bool = False


class EmbedReq(BaseModel):
    model: str
    input: Any  # str | list[str]


class RerankReq(BaseModel):
    model: str
    query: str
    documents: list[str]
    top_n: Optional[int] = None


def _stream_client_timeout():
    """#1184: idle (per-read) timeout for the streamed chat path — see
    ``_STREAM_IDLE_TIMEOUT_SECONDS``. Same shape as ``proxy._upstream_timeout``."""
    import httpx

    return httpx.Timeout(connect=_STREAM_CONNECT_TIMEOUT_SECONDS,
                         read=_STREAM_IDLE_TIMEOUT_SECONDS,
                         write=60.0, pool=10.0)


def _client(request: Request, *, stream: bool = False):
    factory = getattr(request.app.state, "playground_client_factory", None)
    if factory is not None:
        return factory()
    import httpx

    return httpx.AsyncClient(base_url=get_settings().litellm_base_url, trust_env=False,  # in-network router — #1409
                             timeout=_stream_client_timeout() if stream else _TIMEOUT_SECONDS)


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": "playground"}})


def _caller(request: Request) -> str:
    """LLMM-1: WHO is driving this playground call.

    The router-level ``require_role(Role.USER)`` dependency has already proven
    the identity transited Caddy and resolves to at least USER, but a
    router-level dependency's return value never reaches the route — so read the
    same Authentik forward-auth header it validated. Only used for ATTRIBUTION
    (usage row + log line); it grants nothing, so an absent value degrades to
    "unknown" rather than failing a request the gate already allowed.
    """
    try:
        return (request.headers.get(get_settings().admin_user_header) or "").strip() or "unknown"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


class _SlotHandoff:
    """#1184: who releases the box-wide concurrency slot.

    ``_forward`` takes the slot and releases it in its ``finally`` — for every
    exit EXCEPT the one where a streamed response has been started: from then
    on the slot must live as long as the stream does, so ownership transfers
    to the pump generator (``_playground_stream``), whose own ``finally``
    releases it whether the stream completes, the console hits Stop
    (``GeneratorExit``) or the upstream read raises. The same explicit hand-off
    ``_proxy_openai`` does with its ``concurrency_owned`` flag.
    """
    __slots__ = ("transferred",)

    def __init__(self) -> None:
        self.transferred = False


async def _forward(request: Request, path: str, body: dict, *,
                   stream: bool = False):
    """#1024: admission, identical to a /v1 verb.

    The slot is taken FIRST and held for the request's whole lifetime, and
    everything that can refuse the request runs inside it — so a refusal still
    hands the slot back, exactly as ``_proxy_openai`` does. For a streamed
    chat (#1184) "whole lifetime" extends to the end of the stream: see
    ``_SlotHandoff``.
    """
    settings = get_settings()
    from app.enforce import (
        EnforcementError,
        acquire_concurrency_slot,
        release_concurrency_slot,
    )
    from app.proxy import resolve_deps

    # THE gate objects, resolved from the same place the /v1 verbs resolve
    # them: two counters would be no cap at all, and two caches would be two
    # different opinions about a key's rate limit.
    deps = resolve_deps(request)
    try:
        acquire_concurrency_slot(deps.concurrency_counter, deps.concurrency_limit)
    except EnforcementError as exc:
        return _err(exc.status_code, exc.detail)
    handoff = _SlotHandoff()
    try:
        return await _forward_gated(request, path, body, settings, deps,
                                    stream=stream, handoff=handoff)
    finally:
        if not handoff.transferred:
            release_concurrency_slot(deps.concurrency_counter, deps.concurrency_limit)


async def _forward_gated(request: Request, path: str, body: dict, settings,
                         deps, *, stream: bool = False,
                         handoff: Optional[_SlotHandoff] = None):
    """#1024: the shared policy chain, then the forward, then the shared meter."""
    from app.proxy import backstop_texts, policy_gates, record_usage

    model = body.get("model") or ""
    # The reserved KeyRecord (migration 0016), read off the event loop because
    # the lookup is blocking psycopg2 — the same rule as #322. Absent only when
    # the migration has not run: the per-key half of the chain then has nothing
    # to read and is skipped, while the entitlement gate and the metering CAP
    # still apply, because neither depends on a key.
    rec = await run_in_threadpool(_reserved_key_record, request)
    refusal = await policy_gates(request, deps, rec, model,
                                 enforce_rate=rec is not None)
    if refusal is not None:
        # Returned VERBATIM: same status and same message the /v1 caller would
        # get, in the `{"error": {"message": …}}` envelope the console renders
        # (manager-ui/src/api/client.ts reads `detail ?? error.message`). A
        # gated-out playground call must surface as a clear 402/429/503, never
        # as a hang or a bare 500.
        return refusal

    if stream:
        return await _forward_stream(request, path, body, settings, deps, rec,
                                     handoff or _SlotHandoff())

    client = _client(request)
    try:
        try:
            resp = await client.post(
                path,
                json=body,
                headers={
                    "authorization": f"Bearer {settings.litellm_internal_key}",
                    "content-type": "application/json",
                },
            )
        # #349: the response PARSE was guarded but the request itself was not, so the
        # exact conditions the playground exists to surface — a wedged engine, a router
        # that is down, a generation slower than the 120 s timeout — escaped as a bare
        # 500. Mirror the shape GET /v1/models already returns (app/proxy.py).
        except Exception as exc:
            import httpx

            if isinstance(exc, httpx.TimeoutException):
                logger.warning("playground upstream timed out on %s: %s", path, exc)
                return _err(504, f"router timed out after {_TIMEOUT_SECONDS:g}s — the model "
                                 f"may still be loading, or the generation is too long")
            logger.warning("playground upstream unreachable on %s: %s", path, exc)
            return _err(502, f"router unreachable: {exc}")
        try:
            data = resp.json()
        except Exception:
            data = {"error": {"message": resp.text[:500], "type": "playground"}}
        caller = _caller(request)
        _record_playground_usage(body.get("model", ""), data)
        if rec is not None:
            import uuid as _uuid

            prompt_text, completion_text = backstop_texts(path, body, data)
            usage = (data or {}).get("usage") if isinstance(data, dict) else None
            # The SAME call the /v1 verbs make. `estimated=False` mirrors the
            # non-stream hot path: `meter_usage` flags the row estimated itself
            # when the engine reported no usage and the backstop had to guess.
            await record_usage(
                deps, rec, model, usage if isinstance(usage, dict) else {},
                request_id=playground_request_id(caller, _uuid.uuid4().hex),
                estimated=False,
                prompt_text=prompt_text, completion_text=completion_text,
            )
            logger.info("playground: %s spent by %s (metered as operator-playground)",
                        model or "unknown", caller)
        return JSONResponse(status_code=resp.status_code, content=data)
    finally:
        await client.aclose()


def _sse_error_event(message: str) -> bytes:
    """One in-band SSE event carrying the console's error envelope — so a
    stream that dies mid-way ends with a reason, not a silently cut transcript."""
    import json as _json

    return ("data: " + _json.dumps({"error": {"message": message, "type": "playground"}})
            + "\n\n").encode("utf-8")


async def _forward_stream(request: Request, path: str, body: dict, settings, deps,
                          rec, handoff: _SlotHandoff):
    """#1184: proxy the router's SSE stream through.

    Mirrors ``_proxy_openai``'s streamed half, with the same three rules:

    * the upstream is opened FIRST and its status inspected (#323) — a router
      4xx/5xx is answered as a JSON error with that status, never as a 200
      ``text/event-stream`` whose body happens to contain an error;
    * ``stream_options.include_usage`` is forced so the final chunk carries
      usage and the metered row is not ``estimated``;
    * metering runs as the response's ``BackgroundTask`` (#322) — after the
      console has every byte, off the loop — through the SAME ``record_usage``
      the non-stream path and the /v1 verbs use.

    Until the stream is successfully started every exit leaves ``handoff``
    untouched, so ``_forward``'s ``finally`` returns the slot.
    """
    from app.proxy import UsageSniffer, _drain_upstream_error

    body["stream"] = True
    so = body.get("stream_options") or {}
    so["include_usage"] = True
    body["stream_options"] = so
    headers = {
        "authorization": f"Bearer {settings.litellm_internal_key}",
        "content-type": "application/json",
    }

    client = _client(request, stream=True)
    try:
        ctx = client.stream("POST", path, json=body, headers=headers)
        upstream = await ctx.__aenter__()
    except Exception as exc:
        import httpx

        await client.aclose()
        if isinstance(exc, httpx.TimeoutException):
            logger.warning("playground stream: router idle on %s: %s", path, exc)
            return _err(504, f"router produced nothing for {_STREAM_IDLE_TIMEOUT_SECONDS:g}s "
                             f"— the model may still be loading")
        logger.warning("playground stream: upstream unreachable on %s: %s", path, exc)
        return _err(502, f"router unreachable: {exc}")

    if upstream.status_code >= 400:
        data = await _drain_upstream_error(client, ctx, upstream)
        logger.warning("playground stream: upstream %s on %s", upstream.status_code, path)
        return JSONResponse(status_code=upstream.status_code, content=data)

    sniffer = UsageSniffer()
    model = body.get("model") or ""
    caller = _caller(request)
    # Success: from here the slot lives as long as the stream does.
    handoff.transferred = True
    return StreamingResponse(
        _playground_stream(client, ctx, upstream, sniffer,
                           deps.concurrency_counter, deps.concurrency_limit),
        media_type="text/event-stream",
        background=BackgroundTask(_meter_playground_stream, deps, rec, path, body,
                                  sniffer, caller),
    )


async def _playground_stream(client, ctx, upstream, sniffer, concurrency_counter,
                             concurrency_limit):
    """The pump. ONE pump: ``proxy._stream_upstream`` does the byte passthrough,
    the usage sniffing, closes ``ctx``/``client`` and releases the slot in its
    own ``finally`` — whether the stream completes, the console hits Stop
    (Starlette closes this generator → ``GeneratorExit`` → the inner one is
    closed here) or the upstream read raises. This wrapper adds the one thing
    the console needs that a /v1 API client does not: when the upstream dies
    mid-stream (a wedged engine tripping the idle timeout, a crashed runner),
    the reason is sent as a final in-band SSE error event instead of the
    transcript just stopping.
    """
    from app.proxy import _stream_upstream

    inner = _stream_upstream(client, ctx, upstream, sniffer,
                             concurrency_counter, concurrency_limit)
    try:
        async for chunk in inner:
            yield chunk
    except Exception as exc:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            logger.warning("playground stream: router idle for %gs mid-stream: %s",
                           _STREAM_IDLE_TIMEOUT_SECONDS, exc)
            yield _sse_error_event(
                f"router idle for {_STREAM_IDLE_TIMEOUT_SECONDS:g}s mid-stream — the "
                f"engine stopped producing tokens; the answer above is partial")
        else:
            logger.warning("playground stream: upstream failed mid-stream: %s", exc)
            yield _sse_error_event(f"stream interrupted: {str(exc)[:200] or type(exc).__name__}"
                                   " — the answer above is partial")
    finally:
        # Stop (GeneratorExit) arrives here with the inner generator suspended
        # at a yield — close it so ITS finally (upstream close + slot release)
        # runs now, not whenever the GC gets to it.
        await inner.aclose()


async def _meter_playground_stream(deps, rec, path, body, sniffer, caller) -> None:
    """Post-response metering for a streamed playground chat — the same two
    writers the non-stream path uses (the per-model Prometheus view and, when
    the reserved identity is seeded, the authoritative ``usage_events`` row
    through ``record_usage``), fed from the sniffed final usage chunk.
    ``estimated`` when the engine reported no usage, exactly like
    ``proxy._meter_stream``."""
    from app.proxy import backstop_texts, record_usage

    usage = sniffer.usage if isinstance(sniffer.usage, dict) else None
    model = body.get("model") or ""
    _record_playground_usage(model, {"usage": usage} if usage else {})
    if rec is None:
        return
    import uuid as _uuid

    prompt_text, _ = backstop_texts(path, body, None)
    await record_usage(
        deps, rec, model, usage or {},
        request_id=playground_request_id(caller, _uuid.uuid4().hex),
        estimated=not bool(usage),
        prompt_text=prompt_text, completion_text=sniffer.completion_text,
    )
    logger.info("playground: %s streamed by %s (metered as operator-playground, usage=%s)",
                model or "unknown", caller, bool(usage))


def _record_playground_usage(model: str, data) -> None:
    """#350 The per-model playground VIEW of what was consumed
    (``orchestrator_playground_tokens_total`` / ``…_requests_total``).

    The authoritative record is the ``usage_events`` row written by
    ``record_usage`` — this is the cheap per-model breakdown that answers "which
    model is the console burning" without a DB query. Best-effort: an accounting
    counter must never be able to fail an operator's request.

    #1024 removed the second half of this function (a private ``usage_events``
    writer). Two writers for one table is how the playground row and the /v1 row
    drift apart; there is now one.
    """
    try:
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return
        from app import metrics
        from app.metering import parse_usage

        prompt, completion, cached = parse_usage(usage)
        if prompt or completion or cached:
            metrics.add_playground_tokens(model or "unknown", input=prompt,
                                          output=completion, cached=cached)
    except Exception:  # pragma: no cover - defensive
        logger.debug("playground usage accounting failed", exc_info=True)


# The reserved KeyRecord is re-read at most once per TTL: the per-key limits on
# it are operator-editable through the console, so caching it for the process
# lifetime (what the pre-#1024 identity cache did) would mean a limit set on the
# playground key only took effect after a restart. Same shape as the metering-
# mode cache in proxy.py, and for the same reason.
_RESERVED_KEY_PREFIX = "playground-internal"
_RESERVED_REC_TTL = 30.0
_RESERVED_REC_CACHE: dict = {"t": 0.0, "rec": None}


def _reserved_key_record(request: Request):
    """The ``KeyRecord`` for the reserved accounting identity (migration 0016),
    or None when it has not been seeded yet.

    BLOCKING (psycopg2) — callers offload it. Never raises: a DB blip must not
    fail an operator's diagnostics call, it just means this one request carries
    no key (no per-key limits, no usage row) while the entitlement gate and the
    metering CAP still apply.
    """
    seam = getattr(request.app.state, "playground_key_record", None)
    if seam is not None:  # test / DI hook — no DB, no cache
        return seam()
    now = time.time()
    if _RESERVED_REC_CACHE["rec"] is not None and \
            now - _RESERVED_REC_CACHE["t"] < _RESERVED_REC_TTL:
        return _RESERVED_REC_CACHE["rec"]
    try:
        from app.auth import db_lookup_key_prefix
        from app.db import session_scope

        with session_scope() as s:
            rec = db_lookup_key_prefix(s, _RESERVED_KEY_PREFIX)
    except Exception:  # pragma: no cover - DB blip
        logger.debug("reserved playground identity lookup failed", exc_info=True)
        return None
    if rec is None:
        logger.debug("playground identity not seeded (migration 0016) — the call is "
                     "gated but not attributed to a usage row")
        return None
    _RESERVED_REC_CACHE.update(t=now, rec=rec)
    return rec


def playground_request_id(caller: str, uid: str) -> str:
    """LLMM-1: the ``usage_events.request_id`` for a playground row.

    ``usage_events`` has no user column and adding one is a migration that
    cannot be validated here without a live DB, so the caller rides in the
    free-form ``request_id`` text column that this path already fills with a
    throwaway UUID: ``playground:<user>:<uuid>``. Same table, same reserved
    cost-centre, but a chargeback/audit query can now answer "who ran this"
    instead of only "the console did". The user part is sanitised — it is
    Authentik-supplied but ends up in a text column read back into reports.
    """
    safe = "".join(ch for ch in (caller or "unknown") if ch.isalnum() or ch in "._-@")[:64]
    return f"playground:{safe or 'unknown'}:{uid}"


def register_playground_api(app) -> None:
    # #314: the Playground is the USER tier — every authenticated identity
    # (user/admin/super-admin) may use it. The router still injects the internal
    # router key server-side, so a user never sees or supplies an rzfz-sk key.
    router = APIRouter(dependencies=[Depends(require_role(Role.USER))])

    @router.post("/api/playground/chat")
    async def chat(req: ChatReq, request: Request):
        body: dict = {"model": req.model, "messages": req.messages, "max_tokens": req.max_tokens}
        # #254: forward every sampling param the console now sends so the
        # playground sliders actually take effect (previously only temperature
        # was forwarded — top_p was defined but dropped, penalties absent).
        for _p in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
            _v = getattr(req, _p, None)
            if _v is not None:
                body[_p] = _v
        # #1184: SSE passthrough when the console asks for it (see _forward_stream)
        return await _forward(request, "/v1/chat/completions", body, stream=req.stream)

    @router.post("/api/playground/embeddings")
    async def embeddings(req: EmbedReq, request: Request):
        return await _forward(request, "/v1/embeddings", {"model": req.model, "input": req.input})

    @router.post("/api/playground/rerank")
    async def rerank(req: RerankReq, request: Request):
        body = {"model": req.model, "query": req.query, "documents": req.documents}
        if req.top_n is not None:
            body["top_n"] = req.top_n
        return await _forward(request, "/v1/rerank", body)

    app.include_router(router)
