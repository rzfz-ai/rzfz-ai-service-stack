# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Hot-path streaming proxy (M3).

Request flow (spec §5.2): auth → enforce → force include_usage → forward to
the LiteLLM router (Bearer LITELLM_INTERNAL_KEY) → stream SSE back → meter
AFTER the stream (async, never blocks client latency).

The client key is NEVER forwarded upstream. SSE bytes are passed through
VERBATIM (identical framing); a side sniffer extracts the final ``usage``
chunk for metering without disturbing the stream.

Dependencies are resolved from ``app.state`` with production fallbacks so
the whole route unit/api-tests against fakes (cache, db-lookup, upstream
client factory, metering callback) with no real DB/Valkey/network.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.auth import AuthError, authenticate, extract_bearer, is_internal_service_key
from app.config import get_settings
from app.enforce import (
    EnforcementError,
    InFlightCounter,
    acquire_concurrency_slot,
    enforce,
    record_tokens,
    release_concurrency_slot,
)
from app.api.entitlement import entitlement_allows
from app import metrics

logger = logging.getLogger("orchestrator.proxy")

#: #1970 — the `response_format` strings that are complete on their own, so the
#: object form can be built without inventing anything. `json_schema` is
#: deliberately absent: its object form needs a schema the bare string does not
#: carry, and a guessed one would be worse than the upstream's own complaint.
_NORMALISABLE_RESPONSE_FORMATS = ("text", "json_object")

#: (model, value) pairs already reported. Bounded by the model list; see the
#: call site for why this is once-per-model rather than once-per-request.
_normalised_response_formats: set = set()


# --- SSE usage sniffer -------------------------------------------------------
class UsageSniffer:
    """Incrementally scans SSE text for the final ``usage`` object without
    buffering the whole response (keeps only a trailing partial event)."""

    def __init__(self) -> None:
        self._buf = ""
        self.usage: dict | None = None
        self._completion_parts: list[str] = []

    @property
    def completion_text(self) -> str:
        """Concatenated delta content — used only for the tokenizer backstop
        when the engine omits ``usage``."""
        return "".join(self._completion_parts)

    def feed(self, text: str) -> None:
        self._buf += text
        while "\n\n" in self._buf:
            event, self._buf = self._buf.split("\n\n", 1)
            self._scan_event(event)

    def _scan_event(self, event: str) -> None:
        for line in event.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("usage"):
                self.usage = obj["usage"]
            for choice in obj.get("choices") or []:
                if isinstance(choice, dict):
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str):
                        self._completion_parts.append(piece)


# --- dependency resolution ---------------------------------------------------
class _Deps:
    __slots__ = (
        "cache", "db_lookup", "http_client_factory", "metering", "meter_health",
        "concurrency_counter", "concurrency_limit",
    )


def resolve_deps(request: Request) -> _Deps:
    app = request.app
    d = _Deps()
    d.cache = getattr(app.state, "cache", None)
    if d.cache is None:
        from app.cache import get_cache

        d.cache = get_cache()
    d.db_lookup = getattr(app.state, "db_lookup", None)
    if d.db_lookup is None:
        d.db_lookup = _default_db_lookup
    d.http_client_factory = getattr(app.state, "http_client_factory", None)
    if d.http_client_factory is None:
        d.http_client_factory = _default_client_factory
    d.metering = getattr(app.state, "metering", None)
    if d.metering is None:
        d.metering = _default_metering
    d.meter_health = getattr(app.state, "meter_health", None)
    if d.meter_health is None:
        d.meter_health = _default_meter_health
    # #19 box-wide concurrency: one counter shared by every request this app
    # instance serves (a single uvicorn worker, #359 — no cross-process
    # coordination needed). Tests inject their own via app.state.
    d.concurrency_counter, d.concurrency_limit = concurrency_gate_for(app)
    return d


def concurrency_gate_for(app):
    """``(counter, limit)`` — THE box-wide in-flight gate (#19).

    Public because the /v1 verbs are no longer its only users: the console
    playground (LLMM-1) drives the same fleet through the internal router key
    and must contend for the same slots, or the cap it is supposed to be under
    is simply a different number of concurrent generations than the box can
    serve. One resolver, so a test that injects ``app.state.concurrency_limit``
    steers both paths and they can never disagree about the cap.
    """
    counter = getattr(app.state, "concurrency_counter", None)
    if counter is None:
        counter = _shared_concurrency_counter(app)
    limit = getattr(app.state, "concurrency_limit", None)
    if limit is None:
        limit = get_settings().max_concurrent_requests
    return counter, limit


def _shared_concurrency_counter(app) -> InFlightCounter:
    """Lazily create + cache one ``InFlightCounter`` per app instance on
    ``app.state`` — NOT a module global, so separate app instances (e.g. in
    tests) never share in-flight state."""
    counter = getattr(app.state, "_concurrency_counter", None)
    if counter is None:
        counter = InFlightCounter()
        app.state._concurrency_counter = counter
    return counter


def _default_db_lookup(key_hash: bytes):
    from app.auth import db_lookup_key_hash
    from app.db import session_scope

    with session_scope() as s:
        return db_lookup_key_hash(s, key_hash)


# #325: the upstream client used to run with `timeout=None` — no timeout at all — so
# a wedged engine (one that accepted the connection and then stopped producing tokens)
# held a manager connection forever, and with no concurrency cap on the proxy those
# accumulate until the manager runs out of connections. Bound every phase.
#
# READ is deliberately generous: on the NON-stream path it covers the whole
# generation, and on the stream path httpx applies it PER read, i.e. it is the
# maximum gap BETWEEN chunks — a model emitting tokens steadily keeps resetting it.
# So this bounds a hang without truncating a slow-but-live completion.
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 900.0
_WRITE_TIMEOUT = 60.0
_POOL_TIMEOUT = 10.0


def _upstream_timeout():
    import httpx

    return httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT,
                         write=_WRITE_TIMEOUT, pool=_POOL_TIMEOUT)


def _default_client_factory():
    import httpx

    return httpx.AsyncClient(base_url=get_settings().litellm_base_url, trust_env=False,  # in-network router — #1409
                             timeout=_upstream_timeout())


async def _hold_through_router_restart(attempt, *, model, what):
    """Run ``attempt()``; while the router refuses TCP connections, wait and retry.

    #1955. A router reload shuts :4000 for about eleven seconds — LiteLLM drains
    gracefully first (81 s measured on 0.91, the old process still serving), and
    the hole is between the old process letting go of the port and the new one
    taking it. Three of thirty-seven in-flight requests died there, all with the
    same shape:

        502 {"error":{"message":"router unreachable: All connection attempts
                                 failed","type":"orchestrator"}}

    ONLY connect-level failures are retried, and that restriction is the whole
    safety argument. ``ConnectError`` / ``ConnectTimeout`` mean the TCP handshake
    never completed, so the request was never delivered and a retry cannot make
    the model generate twice. A ``ReadTimeout``, or a ``RemoteProtocolError``
    mid-flight, means the router ACCEPTED the request — those are re-raised
    untouched, because there we cannot prove it did not run.

    The caller still answers 502 when the budget is spent; the hold turns a
    failed request into a slower one, not a silent one.

    ONE CONSEQUENCE, STATED RATHER THAN DISCOVERED: a held request keeps its
    concurrency slot (``LLM_MANAGER_MAX_CONCURRENT_REQUESTS``, default 24). So
    during an outage the slots fill with waiters, and arrivals past the cap get
    429 instead of 502. That is the better of the two answers — 429 is a
    retryable signal with defined semantics, 502 is a server error — and at the
    load this box sees (single digits parallel) the cap is never reached. But it
    IS a behaviour change under heavy load, and it belongs here rather than in
    someone's incident notes.
    """
    import httpx

    budget = get_settings().router_reconnect_seconds
    started = time.monotonic()
    deadline = started + budget
    delay = 0.25
    held = False
    while True:
        try:
            result = await attempt()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if budget <= 0 or time.monotonic() >= deadline:
                if held:
                    logger.warning(
                        "proxy %s: the router did not come back within %.1fs — "
                        "giving up for model=%s (#1955)", what, budget, model)
                raise
            if not held:
                held = True
                logger.warning(
                    "proxy %s: the router is refusing connections (%s) — holding "
                    "this request up to %.0fs rather than failing it; a reload "
                    "shuts the port for about that long (model=%s, #1955)",
                    what, exc, budget, model)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)
            continue
        if held:
            logger.info(
                "proxy %s: the router came back after %.1fs — request continues "
                "(model=%s, #1955)", what, time.monotonic() - started, model)
        return result


def _default_metering(**kwargs):
    from app.metering import meter_usage

    meter_usage(**kwargs)


# --- billing-meter CAP gate (metering_mode) ----------------------------------
_METER_HEALTH_CACHE = {"t": 0.0, "ok": True}
_METER_HEALTH_TTL = 2.0  # seconds — bound the hot-path DB probe cost


def _default_meter_health() -> bool:
    """Is the usage store (DB) reachable right now? Cheap `SELECT 1`, cached
    ~2s. Used ONLY in metering_mode=strict to decide whether to serve. Never
    raises (any failure → unhealthy). Self-recovering: once the DB is back the
    next probe (after TTL) flips it healthy again."""
    now = time.time()
    c = _METER_HEALTH_CACHE
    if now - c["t"] < _METER_HEALTH_TTL:
        return c["ok"]
    ok = True
    try:
        from sqlalchemy import text

        from app.db import session_scope

        with session_scope() as s:
            s.execute(text("SELECT 1"))
    except Exception:
        ok = False
    c["t"] = now
    c["ok"] = ok
    return ok


async def metering_cap_refuses(request: Request) -> bool:
    """Should THIS request be refused to protect billing consistency?

    Resolves the live metering mode + the meter-health DI seam off ``app.state``
    and applies ``metering_gate_rejects``. Public because the console playground
    (LLMM-1) consumes the same CAP as the /v1 verbs: it also spends real GPU
    time under the internal router key, so "the usage store is unreachable and
    the box is configured consistency-first" has to mean the same thing there.
    """
    health = getattr(request.app.state, "meter_health", None) or _default_meter_health
    return metering_gate_rejects(
        await _effective_metering_mode(request), await _offload(health)
    )


def metering_gate_rejects(mode: str, meter_healthy: bool) -> bool:
    """Pure decision: should the hot path REFUSE this request to protect
    billing consistency? Only in ``strict`` mode when the meter is unreachable.
    ``available`` never rejects (fail-open, best-effort metering)."""
    return mode == "strict" and not meter_healthy


# The effective metering mode can be flipped LIVE from the management UI
# (PATCH /api/settings → runtime_settings override), so the hot path can't just
# read the frozen env Settings. Resolve it per-request with a tiny per-app cache
# (bounds the DB read to ~once per TTL); a DI seam (app.state.metering_mode_
# resolver) keeps the gate tests DB-free. The cache lives on app.state, NOT a
# module global, so each app/test is isolated. settings_store.effective_
# metering_mode never raises (falls back to env on any DB error).
_METERING_MODE_TTL = 3.0  # seconds


def invalidate_metering_mode_cache(app) -> None:
    """Drop the cached effective mode so a PATCH /api/settings takes effect on
    the very next request (no wait for the TTL)."""
    try:
        app.state._metering_mode_cache = None
    except Exception:  # pragma: no cover - state always assignable
        pass


async def _effective_metering_mode(request: Request) -> str:
    app = request.app
    resolver = getattr(app.state, "metering_mode_resolver", None)
    if resolver is not None:  # test / DI hook — bypass the DB + cache
        return await _offload(resolver)
    cache = getattr(app.state, "_metering_mode_cache", None)
    now = time.time()
    if cache and (now - cache["t"] < _METERING_MODE_TTL):
        return cache["mode"]   # in-memory hit — no I/O, stays on the loop
    from app.settings_store import effective_metering_mode

    # #322: only the MISS touches Postgres, so only the miss is offloaded.
    mode = await _offload(effective_metering_mode)
    app.state._metering_mode_cache = {"t": now, "mode": mode}
    return mode


async def _offload(fn, *args, **kwargs):
    """Run a BLOCKING callable off the event loop (#322).

    Everything the hot path reaches for is blocking I/O: Valkey through redis-py's
    sync client (auth cache, rpm/tpm windows, budget counters) and Postgres through
    psycopg2 + sync SQLAlchemy (auth fallback, metering write, meter-health probe,
    settings override, entitlement verdict). Called straight from an ``async def``
    each one stalls the WHOLE event loop for its round trip — not just the request
    that made it, but every other in-flight completion on the manager. Uvicorn runs
    a single worker (see #359), so there is no second loop to absorb it.

    The DI seams here are injected by tests as plain sync callables, so the common
    path is a threadpool hand-off; an async fake is awaited directly rather than
    handed to a worker thread.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await run_in_threadpool(fn, *args, **kwargs)


def _err(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"message": detail, "type": "orchestrator"}})


def hybrid_bypass_hook(request: Request) -> bool:
    """D6 hybrid-availability hook — INTERFACE ONLY in Phase 1.

    When the manager degrades (e.g. datastore outage), a fully-built version
    fails OPEN to LiteLLM and flags the usage ``estimated`` for later
    reconciliation. Phase-1 always returns False (no bypass).
    """
    return False


def register_proxy(app) -> None:
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _proxy_openai(request, "/v1/chat/completions", enforce_rate=True)

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await _proxy_openai(request, "/v1/completions", enforce_rate=True)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        return await _proxy_openai(request, "/v1/embeddings", enforce_rate=True)

    # #976: reranking on the metered hot path. A consumer that needs reranking
    # (Open WebUI's external reranker, LightRAG) otherwise had no metered manager
    # endpoint and fell back to talking to GPUStack directly — which on an
    # llm-manager-only box isn't running. Rerank goes ENGINE-DIRECT, NOT through
    # the LiteLLM router: LiteLLM's rerank response parser assumes
    # `results[].document` is a plain string and wraps it as
    # `RerankResponseDocument(text=<document>)`, but vLLM's reranker returns
    # `document={"text":..., "multi_modal":...}` (its multimodal wrapper, emitted
    # even for text-only docs and NOT suppressible via return_documents), so the
    # router raises "3 validation errors for RerankResponse" and the call 500s.
    # The engine's own /v1/rerank returns the OpenAI-style
    # `{results:[{index, relevance_score, ...}]}` that OWUI/LightRAG parse
    # directly, so the gateway forwards straight to the resolved engine. Both the
    # versioned and bare paths are served (OpenAI-style /v1/rerank vs
    # Cohere-style /rerank).
    @app.post("/v1/rerank")
    async def rerank(request: Request):
        return await _proxy_rerank(request)

    @app.post("/rerank")
    async def rerank_bare(request: Request):
        return await _proxy_rerank(request)

    @app.get("/v1/models")
    async def list_models(request: Request):
        # #317: OpenAI clients (Open WebUI, SDKs) call GET /v1/models to enumerate
        # what they can use. Serve it on the key-auth hot path — rzfz-sk auth,
        # upstream carries the internal router key — so the external base URL is a
        # complete OpenAI surface, not just the POST inference verbs. Read-only:
        # authenticated but not rate-limited or metered.
        #
        # LLMM-9: `authenticate()` is BLOCKING (a synchronous redis-py GET plus,
        # on a miss, a synchronous psycopg2 lookup) and was called inline inside
        # this `async def`, stalling the single uvicorn event loop — and with it
        # every in-flight completion — for the round trip. Open WebUI polls this
        # route on a timer, so it is a hot path in practice. Offloaded like the
        # POST verbs and /v1/workers (#322 / EXO-10).
        settings = get_settings()
        deps = resolve_deps(request)
        try:
            token = extract_bearer(request.headers.get("authorization"))
            rec = await _offload(
                authenticate,
                token,
                cache=deps.cache,
                db_lookup=deps.db_lookup,
                key_prefix=settings.key_prefix,
                cache_ttl_seconds=settings.cache_ttl_seconds,
            )
        except AuthError as exc:
            return _err(exc.status_code, exc.detail)
        headers = {"authorization": f"Bearer {settings.litellm_internal_key}"}
        try:
            async with deps.http_client_factory() as client:
                resp = await client.get("/v1/models", headers=headers)
            body = resp.json()
        except Exception as exc:  # upstream router down / unreachable
            return _err(502, f"router unreachable: {exc}")
        # #329 A restricted key enumerated the WHOLE fleet. `check_model_allowed`
        # gates inference, so the key could not USE them — but the client was
        # handed a menu of models it will then be 403'd on, and the fleet's model
        # names leaked to every key holder. Filter with the same list that
        # enforces, so what a key can see is what a key can call.
        if resp.status_code == 200:
            body = _filter_models(body, getattr(rec, "allowed_models", None))
        return JSONResponse(status_code=resp.status_code, content=body)

    @app.get("/v1/workers")
    async def list_fleet_workers(request: Request):
        # Service-key-authenticated fleet GPU/worker summary for internal
        # dashboards (the Configuration Portal's GPU panel). The console SPA reads
        # GET /api/workers, but that route is ingress/SSO-gated and 403s any
        # container-to-container service-key call ("request did not originate from
        # the ingress proxy"). Expose the same live GPU/VRAM sample here on the
        # /v1 key-auth hot path (rzfz-sk), read-only: authenticated, not metered.
        #
        # EXO-3: this was gated by `authenticate()` ALONE — liveness, no role,
        # no scope — so ANY tenant/end-user rzfz-sk key could enumerate the
        # fleet's topology and live load, while the equivalent console read
        # (`GET /api/workers`) is admin-gated. Restrict it to the stack's own
        # INTERNAL service keys (`is_internal_service_key`, i.e. a `stack/*`
        # cost centre — the convention post-install already mints them under),
        # which is exactly the set of callers the route was added for. A
        # customer key now gets 403 instead of a fleet inventory.
        #
        # EXO-10: `authenticate()` is blocking (Valkey GET, Postgres fallback)
        # and was called STRAIGHT on the event loop here — stalling every other
        # in-flight completion for its round trip, the exact bug #322 fixed on
        # the POST verbs. Offloaded like every other hot-path auth.
        settings = get_settings()
        deps = resolve_deps(request)
        try:
            token = extract_bearer(request.headers.get("authorization"))
            rec = await _offload(
                authenticate,
                token,
                cache=deps.cache,
                db_lookup=deps.db_lookup,
                key_prefix=settings.key_prefix,
                cache_ttl_seconds=settings.cache_ttl_seconds,
            )
        except AuthError as exc:
            return _err(exc.status_code, exc.detail)
        if not is_internal_service_key(rec):
            logger.warning("/v1/workers refused for non-service key %s (cost centre %r)",
                           getattr(rec, "key_id", "?"), getattr(rec, "cost_center_name", None))
            return _err(403, "fleet topology is restricted to internal stack service keys")
        from app.api.inventory import fleet_gpu_summary
        rows = await run_in_threadpool(fleet_gpu_summary)
        return JSONResponse(status_code=200, content={"data": rows})


def _filter_models(body, allowed_models):
    """#329 Restrict an OpenAI /v1/models payload to a key's allow-list.

    An EMPTY or absent allow-list means unrestricted — the same convention
    `check_model_allowed` uses (`if allowed_models and ...`), so the two cannot
    disagree about what "no restriction" means.

    An unrecognised ENVELOPE is passed through untouched — this must never turn a
    router response we did not anticipate (an error body, say) into a broken or
    empty one. An unrecognised ENTRY is dropped: an allow-list has to fail closed
    at the item level, since keeping a model we cannot identify is the leak being
    fixed.
    """
    if not allowed_models:
        return body
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return body
    permitted = set(allowed_models)
    return {**body,
            "data": [m for m in body["data"]
                     if not isinstance(m, dict) or m.get("id") in permitted]}


# --- #976 rerank: engine-direct (bypass the LiteLLM router) -------------------
def _default_rerank_resolver(model: str):
    """Resolve a READY rerank engine endpoint for ``model``, reusing the SAME
    resolution the router config uses (``generate_from_db`` — co-located →
    container DNS; remote worker → the relay HTTP route).

    Returns ``(base_url, api_key, served_model)`` or ``None`` when no ready
    rerank engine serves it. The third element is the name the ENGINE knows
    (``Model.name``), which is not always the name the CLIENT used
    (``Deployment.model_name``) — ``DeployRequest.served_model`` exists to let
    them differ.

    #1535 revA finding 5: this is the one request path that reaches an engine
    WITHOUT going through LiteLLM, so it is the one path that forwards the
    client's name. Every other path sends ``litellm_params.model`` =
    ``"{provider}/{served}"``, and a relay-routed node matches the name in the
    body against the name it launched the engine under. Divergent names
    therefore worked co-located and failed on a remote worker with
    ``relay: no engine on this node serves model …`` — a rerank that used to
    work (by the old first-ready-engine fallback) stopping, for a reason no
    part of the deploy UI mentions."""
    from app.db import session_scope
    from app.router_config import _litellm_mode, generate_from_db

    with session_scope() as s:
        for dep in generate_from_db(s):
            if dep.get("model_name") == model and _litellm_mode(dep.get("task")) == "rerank":
                eps = dep.get("endpoints") or []
                if eps:
                    return (eps[0].get("endpoint"), eps[0].get("api_key"),
                            dep.get("served_model") or model)
    return None


async def _default_rerank_forward(base: str, api_key, body: dict):
    """POST the rerank body to the engine's ``/rerank`` and return
    ``(status_code, json_or_text)``. The stored endpoint already carries the
    engine's ``/v1`` suffix, so append ``/rerank``."""
    import httpx

    url = base.rstrip("/") + "/rerank"
    headers = {"content-type": "application/json"}
    if api_key and api_key != "none":
        headers["authorization"] = f"Bearer {api_key}"
    # #1409 (#276 reprise guard): the engine is an in-network container
    # (engine-<model>-<hex>). httpx honours HTTPS_PROXY from the environment
    # unless trust_env=False, and it matches NO_PROXY by host name only — never
    # by CIDR — so a corporate proxy overlay sent every rerank into the proxy
    # and the console showed them 'pending' forever. Every client in this app
    # that talks in-network says trust_env=False explicitly; only hf.py and the
    # #307 external-endpoint probe say True (guarded by test_1409).
    async with httpx.AsyncClient(timeout=_upstream_timeout(), trust_env=False) as client:
        resp = await client.post(url, json=body, headers=headers)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"error": {"message": (resp.text or "")[:500]
                                            or "rerank engine returned a non-JSON body",
                                            "type": "orchestrator"}}


def _rerank_prompt_text(body) -> str:
    """EXO-12 backstop input text for a rerank call: the query plus every
    candidate document, which is exactly what the engine tokenizes. Used only
    when the engine reports no ``usage`` — an engine that DOES report it always
    wins (``meter_usage`` prefers a real usage block over the estimate)."""
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    q = body.get("query")
    if isinstance(q, str):
        parts.append(q)
    docs = body.get("documents")
    if isinstance(docs, list):
        for d in docs:
            if isinstance(d, str):
                parts.append(d)
            elif isinstance(d, dict) and isinstance(d.get("text"), str):
                parts.append(d["text"])
    return "\n".join(parts)


async def _proxy_rerank(request: Request):
    """Metered, key-authed rerank that forwards ENGINE-DIRECT (see the route
    docstring in ``register_proxy`` for why the LiteLLM router is bypassed).

    LLMM-3: this verb used to run only the per-key ``enforce`` chain — it took
    NO box-wide concurrency slot, skipped the entitlement gate and skipped the
    strict-metering CAP, while occupying the same GPU as every other request
    for up to the same 900 s upstream read budget. It now runs the identical
    ``policy_gates`` chain as the OpenAI verbs, inside the same #19 slot.
    """
    t0 = time.perf_counter()
    settings = get_settings()
    deps = resolve_deps(request)
    app = request.app

    # 1. auth (same rzfz-sk hot-path auth as the OpenAI verbs)
    try:
        token = extract_bearer(request.headers.get("authorization"))
        rec = await _offload(
            authenticate, token, cache=deps.cache, db_lookup=deps.db_lookup,
            key_prefix=settings.key_prefix, cache_ttl_seconds=settings.cache_ttl_seconds,
        )
    except AuthError as exc:
        return _err(exc.status_code, exc.detail)

    try:
        body = await request.json()
    except Exception:
        return _err(400, "invalid JSON body")
    model = body.get("model") or ""

    # 2. #19 box-wide concurrency slot, acquired AFTER auth/body-parse and held
    # for the request's full lifetime — same contract as `_proxy_openai`. Rerank
    # is non-streaming, so the single `finally` below is the whole story: there
    # is no ownership hand-off to a streaming generator.
    try:
        acquire_concurrency_slot(deps.concurrency_counter, deps.concurrency_limit)
    except EnforcementError as exc:
        return _err(exc.status_code, exc.detail)

    try:
        # 3. the SAME rate/entitlement/metering-CAP chain the /v1 verbs run.
        refusal = await policy_gates(request, deps, rec, model, enforce_rate=True)
        if refusal is not None:
            return refusal

        # 4. resolve the rerank engine (DI seam for tests)
        resolver = getattr(app.state, "rerank_resolver", None) or _default_rerank_resolver
        upstream = await _offload(resolver, model)
        if not upstream:
            metrics.observe_proxy_latency(time.perf_counter() - t0, model=model)
            return _err(404, f"no ready rerank engine for model '{model}'")
        base, api_key = upstream[0], upstream[1]
        # #1535 revA finding 5: address the engine by the name it was launched
        # under. Read as an OPTIONAL third element (a length check, not an
        # exception) so a resolver installed as a test seam may still return a
        # pair — those keep forwarding the client's name, which is what they
        # asserted before. The client's own body is not mutated: the response
        # and the usage row stay keyed on the name the caller asked for.
        served = upstream[2] if len(upstream) > 2 else None
        forwarded = body
        if served and served != model:
            forwarded = dict(body, model=served)

        # 5. forward engine-direct (DI seam for tests)
        forward = getattr(app.state, "rerank_forwarder", None) or _default_rerank_forward
        metrics.incr_requests(model=model)
        try:
            status_code, data = await forward(base, api_key, forwarded)
        except Exception as exc:
            metrics.observe_proxy_latency(time.perf_counter() - t0, model=model)
            logger.warning("rerank engine unreachable model=%s: %s", model, exc)
            return _err(502, f"rerank engine unreachable: {exc}")

        # 6. meter. EXO-12: this passed a literal `{}` — every rerank request,
        # however large the document batch, wrote a ZERO-token usage row, so the
        # box's own billing source of truth (#254) under-reported reranking to
        # exactly nothing and the tpm/budget counters never saw it. Forward the
        # engine's usage block when it reports one (vLLM/llama.cpp rerank do,
        # as prompt_tokens/total_tokens); otherwise let `meter_usage`'s
        # tokenizer backstop estimate from query+documents and FLAG the row
        # estimated, which is what `estimated` exists to distinguish.
        usage = (data or {}).get("usage") or {} if isinstance(data, dict) else {}
        await record_usage(
            deps, rec, model, usage,
            request_id=(data or {}).get("id") if isinstance(data, dict) else None,
            estimated=False, prompt_text=_rerank_prompt_text(body),
            completion_text="")
        latency = time.perf_counter() - t0
        metrics.observe_proxy_latency(latency, model=model)
        logger.info("proxy rerank model=%s status=%s latency_ms=%.1f", model, status_code, latency * 1000)
        return JSONResponse(status_code=status_code, content=data)
    finally:
        release_concurrency_slot(deps.concurrency_counter, deps.concurrency_limit)


async def policy_gates(request: Request, deps, rec, model: str, *, enforce_rate: bool):
    """The per-request policy chain EVERY inference path runs, in one place.

    Returns an error ``JSONResponse`` when the request must be refused, else
    ``None``. Callers run it INSIDE the box-wide concurrency slot, so a refusal
    still gives the slot back through their own ``finally``.

    LLMM-3: this used to live inline in ``_proxy_openai`` only, and ``/v1/rerank``
    — added later, forwarding engine-direct — reproduced just the ``enforce``
    call. So a rerank consumer (Open WebUI's external reranker, LightRAG) skipped
    the entitlement gate and the strict-metering CAP entirely, on a verb that
    occupies the same GPU as everything else. Factoring the chain is what makes
    "the same gates" mechanically true instead of a copy that drifts.

    #1024 (LLMM-1) made it PUBLIC and gave it a second caller: the console
    playground drives the same fleet under the internal router key, and the
    operator decision of 2026-09-02 is that it takes the same gates — one code
    path, no special rules. Nothing here knows which surface called it.

    Order (unchanged from the pre-LLMM-3 hot path):
      1. rpm/tpm/budget + model allow-list (#19/M2) — offloaded (#322): 5+
         blocking Valkey round trips, the densest synchronous I/O on the path.
         Skipped when ``enforce_rate`` is false — the caller then has no
         ``KeyRecord`` whose limits could be read;
      2. subscription entitlement (#265 ENT1) — LOCAL + opt-in: only
         ``entitlement_mode=enforce`` blocks (402); the cached verdict fails
         CLOSED on a cache/DB blip. Moved OUT of the ``enforce_rate`` branch by
         #1024: entitlement is a contractual control on what the box may serve
         at all, so it cannot be something a caller opts out of along with its
         rate limit. No behaviour change for ``/v1`` — every verb there passes
         ``enforce_rate=True``, so the same two gates run in the same order;
      3. billing-meter CAP: in ``metering_mode=strict`` refuse (503) rather than
         serve an UNMETERED request when the usage store is unreachable
         (consistency-first). ``available`` (default) is a no-op here —
         availability-first, meter best-effort. Also outside the
         ``enforce_rate`` branch: a caller that opts out of rate limiting has
         not opted out of billing consistency.
    """
    if enforce_rate:
        try:
            await _offload(enforce, rec, model, cache=deps.cache, now=time.time())
        except EnforcementError as exc:
            return _err(exc.status_code, exc.detail)

    _es = get_settings()
    if _es.entitlement_mode == "enforce" and not await _offload(
        entitlement_allows, deps.cache, _es.subscription_number or None
    ):
        metrics.incr_entitlement_rejected()
        logger.warning("entitlement_mode=enforce + not entitled → refusing model=%s", model)
        return _err(402, "subscription not entitled (inactive or expired) — contact your razzfazz.ai support contact")

    if await metering_cap_refuses(request):
        metrics.incr_meter_rejected()
        logger.warning(
            "usage store unreachable + metering_mode=strict → refusing model=%s", model
        )
        return _err(503, "billing meter unavailable (metering_mode=strict) — refusing to serve an unmetered request")
    return None


async def _proxy_openai(request: Request, upstream_path: str, *, enforce_rate: bool):
    t0 = time.perf_counter()
    settings = get_settings()
    deps = resolve_deps(request)

    # 1. auth — offloaded whole (#322): authenticate() does a Valkey GET and, on a
    # miss, a Postgres lookup, both blocking.
    try:
        token = extract_bearer(request.headers.get("authorization"))
        rec = await _offload(
            authenticate,
            token,
            cache=deps.cache,
            db_lookup=deps.db_lookup,
            key_prefix=settings.key_prefix,
            cache_ttl_seconds=settings.cache_ttl_seconds,
        )
    except AuthError as exc:
        return _err(exc.status_code, exc.detail)

    # parse body
    try:
        body = await request.json()
    except Exception:
        return _err(400, "invalid JSON body")
    model = body.get("model") or ""

    # Engine-migration compatibility (llama.cpp → vLLM): the llama.cpp fleet
    # served chat requests that carried ONLY system messages (no user turn) —
    # common in extraction/vision workflows authored in Dify where the whole
    # instruction sits in the system prompt. vLLM's Qwen3 chat template rejects
    # those outright with HTTP 400 "No user query found in messages.", so the
    # SAME workflow that worked against a llama.cpp box breaks the moment the
    # model is served by vLLM. Keep the manager a drop-in across engines: if a
    # chat request has messages but none with role "user", append a minimal
    # empty user turn so the template renders. A well-formed request (any user
    # message, including a vision image-only user message) is left untouched.
    if upstream_path == "/v1/chat/completions":
        _msgs = body.get("messages")
        if (isinstance(_msgs, list) and _msgs
                and not any(isinstance(m, dict) and m.get("role") == "user" for m in _msgs)):
            _msgs.append({"role": "user", "content": ""})
            body["messages"] = _msgs

        # Qwen thinking toggle: clients (Dify's openai_api_compatible plugin, per
        # workflow node) pass `enable_thinking` as a TOP-LEVEL completion param,
        # but vLLM only honors it under `chat_template_kwargs`. Untranslated, a
        # node's `enable_thinking: false` is silently ignored — reasoning stays on,
        # which on extraction/RAG both slows the call AND lets the <think> block
        # eat the node's max_tokens (empty output). Map top-level → chat_template_
        # kwargs so per-node control actually takes effect on the vLLM path;
        # never clobber an explicit chat_template_kwargs the client already sent.
        _et = body.pop("enable_thinking", None)
        if _et is not None:
            _ctk = body.get("chat_template_kwargs")
            if not isinstance(_ctk, dict):
                _ctk = {}
            _ctk.setdefault("enable_thinking", _et)
            body["chat_template_kwargs"] = _ctk

        # #1970: `response_format` as a BARE STRING becomes the object OpenAI
        # defines. Same drop-in reasoning as the two shims above: a client sends
        # what an older or laxer backend accepted, and the strict one refuses the
        # whole request.
        #
        # What IS measured, on 0.91 (2026-09-12), same model, same router — a
        # deliberate hand probe, not traffic from a running workflow:
        #
        #   "response_format": "json_object"           -> HTTP 500
        #        OpenAIException - Unsupported response_format type - json_object
        #   "response_format": {"type": "json_object"}  -> 200, {"a":1}
        #
        # So the gateway's leniency is real, and so is the failure it prevents.
        #
        # What is NOT measured, and was believed here until a wire capture said
        # otherwise: that any client in THIS stack sends the bare form. The chain
        # "both shipped plugins omit the STRUCTURED_OUTPUT feature flag, so Dify
        # takes its prompt branch and writes the bare enum" is a plausible code
        # reading — and the capture of the real PSA workflow shows Dify sending
        # the OBJECT form. The only 500 anyone has seen came from the probe above.
        #
        # This is therefore a DEFENSIVE shim, not a corrective one. It is here
        # because the gateway should stay a drop-in across engines, and it is
        # kept by an explicit operator decision (2026-09-12) — not because it
        # repairs an observed break. The log line below is what would tell us a
        # real client had started sending it.
        #
        # ONLY the two self-contained types are translated. `json_schema` needs a
        # schema object that a bare string does not carry, so it is left alone:
        # the upstream then says what is missing, which is more useful than a
        # request we completed by guessing. An object-form `response_format` is
        # never touched.
        _rf = body.get("response_format")
        if isinstance(_rf, str) and _rf in _NORMALISABLE_RESPONSE_FORMATS:
            body["response_format"] = {"type": _rf}
            # ONCE per (model, value), not per request: a workflow that sends
            # this sends it on every call, and a per-request line would bury the
            # thing it is meant to surface. The set is bounded by the model list
            # times two, so it cannot grow without limit.
            _seen = (model or "?", _rf)
            if _seen not in _normalised_response_formats:
                _normalised_response_formats.add(_seen)
                logger.info(
                    "proxy: normalising a bare response_format=%r into "
                    "{'type': %r} for model %s — a client sent the pre-spec "
                    "form; no client in this stack was observed doing that, so "
                    "this line is worth chasing. Logged once per model (#1970)",
                    _rf, _rf, model or "?")

    # #1044: strip `dimensions` on /v1/embeddings. cognee (from its
    # EMBEDDING_DIMENSIONS config) and other clients send `dimensions`, but LiteLLM
    # classifies `openai/<embedding-model>` as an OpenAI text-embedding-3 model and
    # raises UnsupportedParamsError for `dimensions` BEFORE `drop_params: true` can
    # strip it (reproduced at BOTH the proxy and the LiteLLM router). The llama.cpp
    # embedding engine returns its native width regardless, so the param is a no-op
    # downstream — drop it so the request survives the router instead of erroring
    # (cognee cognify otherwise dies with `EmbeddingException` / HTTP 422). The
    # consumer's own vector-DB dimension is unaffected (it is driven client-side).
    if upstream_path == "/v1/embeddings":
        body.pop("dimensions", None)


    # #19 box-wide concurrency ("option C"): a single in-flight cap shared by
    # EVERY consumer (OWUI, Dify, agents, Cognee, …), acquired AFTER auth/body
    # parsing (a bad key or malformed JSON never reaches this far — no reason
    # to burn a shared slot on it) and held for the request's FULL lifetime,
    # including — for a streamed completion — until the stream itself
    # finishes. REJECTS with 429 at the cap; never queues (see enforce.py).
    #
    # `concurrency_owned` is the fail-safe: the `finally` below releases the
    # slot on every exit EXCEPT the one where a streamed response starts
    # successfully, at which point ownership explicitly transfers to
    # `_stream_upstream`'s own `finally` (same place its httpx client is
    # closed). So the slot comes back whether enforcement, entitlement, the
    # metering gate, an unreachable router, a non-2xx upstream, a non-JSON
    # body, or any other exception is what ends the request — a leaked slot
    # would slowly shrink the box's effective cap to zero.
    try:
        acquire_concurrency_slot(deps.concurrency_counter, deps.concurrency_limit)
    except EnforcementError as exc:
        return _err(exc.status_code, exc.detail)

    concurrency_owned = True
    try:
        # 2. per-key rate/budget + entitlement + billing-meter CAP — the one
        # gate chain every metered verb shares (see `policy_gates`).
        refusal = await policy_gates(request, deps, rec, model, enforce_rate=enforce_rate)
        if refusal is not None:
            return refusal

        stream = bool(body.get("stream"))
        if stream:
            # 3. force usage reporting on the final chunk
            so = body.get("stream_options") or {}
            so["include_usage"] = True
            body["stream_options"] = so

        headers = {
            "authorization": f"Bearer {settings.litellm_internal_key}",
            "content-type": "application/json",
        }
        metrics.incr_requests(model=model)

        if stream:
            # #323: open the upstream FIRST so its status can be inspected. Building the
            # StreamingResponse before the request was made meant a router/engine 4xx or
            # 5xx reached the client as a 200 text/event-stream whose body happened to
            # contain an error object — the "Invalid model name but it looks like it
            # worked" class of confusion (cf. #308) — and metering still recorded an
            # `estimated` row for a request that produced nothing.
            try:
                client, ctx, upstream = await _hold_through_router_restart(
                    lambda: _open_upstream_stream(deps, upstream_path, body, headers),
                    model=model, what="stream")
            except Exception as exc:
                metrics.observe_proxy_latency(time.perf_counter() - t0, model=model)
                logger.warning("proxy stream: upstream unreachable model=%s: %s", model, exc)
                return _err(502, f"router unreachable: {exc}")

            if upstream.status_code >= 400:
                data = await _drain_upstream_error(client, ctx, upstream)
                metrics.observe_proxy_latency(time.perf_counter() - t0, model=model)
                logger.warning("proxy stream: upstream %s model=%s", upstream.status_code, model)
                return JSONResponse(status_code=upstream.status_code, content=data)

            sniffer = UsageSniffer()
            # Success: the concurrency slot now lives for as long as the stream
            # does — _stream_upstream releases it in its own finally, not here.
            concurrency_owned = False
            return StreamingResponse(
                _stream_upstream(
                    client, ctx, upstream, sniffer,
                    deps.concurrency_counter, deps.concurrency_limit,
                ),
                media_type="text/event-stream",
                # metering runs after the response cycle, off the loop — see _meter_stream
                background=BackgroundTask(_meter_stream, deps, rec, model, body, sniffer, t0),
            )

        # non-stream
        client = deps.http_client_factory()
        try:
            try:
                resp = await _hold_through_router_restart(
                    lambda: client.post(upstream_path, json=body, headers=headers),
                    model=model, what="non-stream")
            except Exception as exc:
                # #323 (non-stream half): an unreachable or timed-out router used to
                # escape as a bare 500. Same envelope as the streaming path.
                metrics.observe_proxy_latency(time.perf_counter() - t0, model=model)
                logger.warning("proxy non-stream: upstream unreachable model=%s: %s", model, exc)
                return _err(502, f"router unreachable: {exc}")
            try:
                data = resp.json()
            except Exception:
                # a non-JSON body (proxy error page, empty response) must not 500 —
                # hand back the upstream status with the text we did get.
                text = (resp.text or "")[:500]
                metrics.observe_proxy_latency(time.perf_counter() - t0, model=model)
                logger.warning("proxy non-stream: non-JSON upstream body status=%s model=%s",
                               resp.status_code, model)
                return _err(resp.status_code if resp.status_code >= 400 else 502,
                            text or "upstream returned a non-JSON body")
        finally:
            await client.aclose()
        usage = (data or {}).get("usage") or {}
        await record_usage(
            deps, rec, model, usage,
            request_id=(data or {}).get("id"),
            estimated=False,
            prompt_text=_prompt_text(body),
            completion_text=_completion_text_from_response(data),
        )
        latency = time.perf_counter() - t0
        metrics.observe_proxy_latency(latency, model=model)
        logger.info("proxy non-stream model=%s status=%s latency_ms=%.1f", model, resp.status_code, latency * 1000)
        return JSONResponse(status_code=resp.status_code, content=data)
    finally:
        if concurrency_owned:
            release_concurrency_slot(deps.concurrency_counter, deps.concurrency_limit)


async def _open_upstream_stream(deps, upstream_path, body, headers):
    """Send the streaming request and return ``(client, ctx, response)`` with the
    response HEADERS read but the body untouched — so the caller can branch on
    ``status_code`` before committing to a 200 SSE passthrough (#323). The caller
    owns closing both ``ctx`` and ``client``."""
    client = deps.http_client_factory()
    ctx = client.stream("POST", upstream_path, json=body, headers=headers)
    try:
        upstream = await ctx.__aenter__()
    except BaseException:
        await client.aclose()
        raise
    return client, ctx, upstream


async def _drain_upstream_error(client, ctx, upstream) -> dict:
    """Read a non-2xx upstream body (small), close everything, and return a JSON
    object to hand back verbatim — falling back to the OpenAI-style error envelope
    when the body isn't JSON."""
    raw = b""
    try:
        raw = await upstream.aread()
    except Exception:  # pragma: no cover - defensive
        pass
    finally:
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:  # pragma: no cover - defensive
            pass
        await client.aclose()
    text = raw.decode("utf-8", "ignore") if raw else ""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        return data
    return {"error": {"message": text[:500] or "upstream error", "type": "orchestrator"}}


async def _stream_upstream(client, ctx, upstream, sniffer, concurrency_counter, concurrency_limit):
    """Pump an ALREADY-OPEN 2xx upstream stream through verbatim.

    Metering deliberately does NOT happen here (#322). It used to run in this
    generator's ``finally``, and once the DB write is offloaded that becomes an
    ``await`` in a ``finally`` — which raises ``RuntimeError: async generator
    ignored GeneratorExit`` when a client disconnects mid-stream and Starlette
    closes the generator. It runs as the response's BackgroundTask instead, which
    executes after the response cycle and can await freely. The caller shares the
    ``sniffer`` so the task can read the final usage chunk.

    #19: also owns releasing the box-wide concurrency slot ``_proxy_openai``
    acquired for this request — the slot must stay held for the whole stream,
    not just until the first byte, and this ``finally`` runs whether the stream
    completes normally, the client disconnects mid-stream (``GeneratorExit``),
    or the upstream read raises (a wedged/crashed engine) — the same
    unconditional cleanup already guaranteed for ``ctx``/``client`` below.
    """
    try:
        async for chunk in upstream.aiter_bytes():
            if chunk:
                try:
                    sniffer.feed(chunk.decode("utf-8", errors="ignore"))
                except Exception:  # never let the sniffer break passthrough
                    pass
                yield chunk  # verbatim — identical SSE framing
    finally:
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - defensive
            # #19: aclose() raising here (e.g. transport already torn down)
            # must NOT skip the release below — that would leak the
            # concurrency slot on every such failure and slowly shrink the
            # box's effective cap to zero.
            pass
        release_concurrency_slot(concurrency_counter, concurrency_limit)


async def _meter_stream(deps, rec, model, body, sniffer, t0):
    """Post-response metering for a streamed completion (#322 BackgroundTask).

    Runs after the client already has every byte, and off the event loop, so the
    per-request Postgres INSERT never delays another in-flight completion."""
    usage = sniffer.usage or {}
    await record_usage(
        deps, rec, model, usage,
        request_id=None,
        estimated=not bool(usage),
        prompt_text=_prompt_text(body),
        completion_text=sniffer.completion_text,
    )
    latency = time.perf_counter() - t0
    metrics.observe_proxy_latency(latency, model=model)
    logger.info("proxy stream model=%s latency_ms=%.1f usage=%s", model, latency * 1000, bool(usage))


async def record_usage(deps, rec, model, usage, *, request_id, estimated, prompt_text="", completion_text=""):
    """Hand usage to the metering layer + feed the tpm window. Best-effort:
    metering must never surface as a client error.

    #322: the default implementation opens a session and INSERTs a usage_events row
    — blocking psycopg2 — so it is offloaded. The module docstring's claim that
    metering "never adds to client-visible latency" was true only for the request
    that produced it; on the loop it delayed everyone else."""
    try:
        await _offload(
            deps.metering,
            rec=rec,
            model=model,
            usage=usage,
            request_id=request_id,
            estimated=estimated,
            cache=deps.cache,
            prompt_text=prompt_text,
            completion_text=completion_text,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("metering hook failed (non-fatal)")


def _prompt_text(body) -> str:
    """Best-effort input text for the tokenizer backstop (chat messages or a
    completion prompt)."""
    from app.metering import text_from_messages

    if not isinstance(body, dict):
        return ""
    if body.get("messages"):
        return text_from_messages(body.get("messages"))
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        return "\n".join(str(p) for p in prompt)
    return ""


def backstop_texts(upstream_path: str, body, data) -> tuple[str, str]:
    """``(prompt_text, completion_text)`` for the tokenizer backstop of a
    request/response pair — the values the /v1 verbs already pass to
    ``record_usage``, resolved by verb.

    #1024: the console playground meters through the same ``record_usage`` and
    therefore needs the same inputs, and the rerank verb derives its input text
    differently (query + candidate documents, which is what the engine
    tokenizes) than the OpenAI verbs. Composed from the SAME per-verb helpers
    the hot path uses, so a playground row and a /v1 row for the identical body
    estimate identically by construction rather than by two look-alike
    expressions.

    The backstop only ever decides what an ``estimated`` row says; an engine
    that reports ``usage`` always wins (see ``meter_usage``).
    """
    if upstream_path.endswith("/rerank"):
        return _rerank_prompt_text(body), ""
    return _prompt_text(body), _completion_text_from_response(data)


def _completion_text_from_response(data) -> str:
    if not isinstance(data, dict):
        return ""
    parts = []
    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message") or {}
        if isinstance(msg.get("content"), str):
            parts.append(msg["content"])
        elif isinstance(choice.get("text"), str):
            parts.append(choice["text"])
    return "".join(parts)
