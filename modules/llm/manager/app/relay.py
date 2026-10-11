# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Worker↔master WS inference relay — master side (#262, Tasks 2+3).

A remote worker (no shared network with the master's other services) opens
ONE long-lived WebSocket to the master and multiplexes every inference
request over it, framed via ``app.relay_protocol``. This module owns:

* ``RelayHub`` — the per-worker WS registry + demux read loop +
  ``open_request`` streaming generator, the piece the HTTP relay route
  (``app.relay_http``) drives to proxy a request through to a specific
  worker.
* ``relay_ws`` — the actual ``@app.websocket(...)`` handler function that
  authenticates the worker, registers it with the hub, and runs the demux
  read loop until the socket drops (Task 2).
* ``register_relay(app)`` (Task 3) — mounts BOTH the worker-facing WS
  ingress (``relay_ws``, directly) and the HTTP mux route (via a
  function-local import of ``app.relay_http.register_http_relay`` — see
  below and that module's docstring). Calling this from ``create_app()`` is
  Task 5's job; this module intentionally does not touch ``create_app()``
  itself.

``RelayHub`` is deliberately import-safe and testable WITHOUT FastAPI/
Starlette — it only needs an object with async ``send_text``/``receive_text``
methods (a real ``starlette.websockets.WebSocket`` satisfies that duck type,
and so does the tests' ``FakeWS``). All FastAPI/Starlette imports needed by
``relay_ws`` are local to that function, and the HTTP relay route's
FastAPI/Starlette imports live entirely in the SIBLING module
``app.relay_http`` (imported only inside ``register_relay``, function-
locally) — so a bare ``from app.relay import RelayHub`` never drags in the
ASGI stack. (Fix round 1, #262 review: the HTTP route originally lived in
THIS module with top-level fastapi imports, because its ``request: Request``
annotation must resolve against module globals under ``from __future__
import annotations`` — the module split is what lets that requirement live
somewhere other than here, restoring the invariant Task 2 established.)
"""
from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Optional

from app import relay_protocol as rp

if TYPE_CHECKING:  # names used only in annotations — NEVER imported at runtime
    # `relay_ws`'s `websocket: "WebSocket"` annotation references this. Guarded
    # under TYPE_CHECKING so a bare `from app.relay import RelayHub` (the
    # FastAPI-free unit tier) still drags in NO ASGI stack — the import-safe
    # invariant this module documents. FastAPI needs the name resolvable at
    # RUNTIME too (it introspects the annotation when mounting the route), which
    # is why `register_relay` also binds `WebSocket` into module globals there,
    # where FastAPI is by definition present. (This static import keeps the name
    # defined for linters/type-checkers; the runtime binding is what FastAPI
    # actually resolves against.)
    from starlette.websockets import WebSocket

logger = logging.getLogger("orchestrator.relay")

# Sentinel pushed into every in-flight request's queue on unregister/drop —
# distinct from any decoded frame dict, so `item is _DROP` can never
# false-positive on worker-supplied data.
_DROP = object()

# LLMM-10: BACKPRESSURE between a fast worker and a slow HTTP client.
#
# `_read_loop` reads frames off the worker WS as fast as they arrive; a request's
# consumer (`open_request`, driving a `StreamingResponse`) yields them only as
# fast as the downstream client reads. With an UNBOUNDED queue nothing connected
# the two, so a stalled or slow client made the manager buffer the entire
# response in memory — and with `relay_protocol.MAX_FRAME_SIZE` at 32 MiB and no
# cap on the frame COUNT, a handful of concurrent relayed requests could reach
# the container's memory limit.
#
# The queue is therefore bounded, and a full queue makes the read loop WAIT
# (which stops draining the socket and propagates TCP backpressure to the
# worker) — but only for a bounded grace period. An unbounded `await queue.put`
# would deadlock the whole worker's relay the moment ONE consumer went away for
# good: the read loop is shared by every in-flight request on that connection, so
# it would block forever on a queue nobody drains again, taking every OTHER
# request on that worker down with it. So the wait has a deadline, and a consumer
# that has not moved by then loses ITS request (drop + `cancel` to the worker)
# while the loop carries on serving the rest.
RESPONSE_QUEUE_MAXSIZE = 256
RESPONSE_QUEUE_PUT_TIMEOUT_S = 10.0


def _configured_idle_timeout() -> Optional[float]:
    """#928: the operator's relay idle timeout, or None when disabled.

    `app.config` is dependency-free (stdlib only), but the import stays
    FUNCTION-LOCAL here for the same reason `relay_http` is imported lazily in
    `register_relay`: a bare `from app.relay import RelayHub` — the fast,
    FastAPI-free unit tier — must keep pulling in as little of the app package
    as it does today. Settings are re-read per request (`get_settings()` is
    deliberately un-cached and cheap), so an operator's change takes effect on
    the next request rather than on the next process restart.
    """
    from app.config import DEFAULT_RELAY_IDLE_TIMEOUT_S, get_settings

    try:
        value = float(get_settings().relay_idle_timeout_s)
    except Exception:  # pragma: no cover - defensive: never fail a request on config
        value = DEFAULT_RELAY_IDLE_TIMEOUT_S
    return value if value > 0 else None


def _force_drop(queue: "asyncio.Queue") -> None:
    """Push `_DROP` into a request's queue even when it is FULL.

    The queue is bounded (see above), so the teardown path can no longer assume
    `put_nowait` succeeds — and teardown is synchronous, so it cannot await. A
    request being torn down is failing anyway (its consumer raises
    `RelayUnavailable` on `_DROP`), so making room by discarding one buffered
    frame is the right trade: never raise `QueueFull` out of `unregister`, and
    never leave a waiting consumer hanging. No `await` between the get and the
    put, so nothing can refill the slot in between."""
    try:
        queue.put_nowait(_DROP)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:  # pragma: no cover - defensive
        pass
    try:
        queue.put_nowait(_DROP)
    except asyncio.QueueFull:  # pragma: no cover - defensive
        logger.warning("relay: could not signal drop on a full response queue")


class RelayUnavailable(Exception):
    """Raised by ``RelayHub.open_request`` when the target worker has no live
    WS connection, or its WS drops (before or during the request)."""


class RelayResponse:
    """The head-of-response info `open_request` yields first: HTTP status +
    headers, exactly as the worker's local HTTP client observed them."""

    __slots__ = ("status", "headers")

    def __init__(self, status: int, headers: Dict[str, str]):
        self.status = status
        self.headers = headers

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"RelayResponse(status={self.status!r}, headers={self.headers!r})"


class RelayHub:
    """Per-worker WS registry + frame demuxer.

    One entry per connected worker (``worker_id -> ws``). Each worker also
    gets a ``req_id -> (owning_ws, asyncio.Queue)`` map for its currently
    in-flight relayed requests; ``_read_loop`` is the ONE reader of a given
    worker's WS (concurrent ``open_request`` calls over the same connection
    never read the socket directly — they only send, then wait on their own
    queue) so frames are demuxed to the right request purely by ``id``,
    with no read races between concurrent requests.

    Teardown is CONNECTION-SCOPED, not just worker-scoped (fix round 1,
    #262 review): every pending entry remembers which `ws` it was sent on,
    and `unregister(worker_id, ws)` only ever touches entries owned by
    THAT `ws`, and only pops the registry's live-`ws` slot if it still
    holds that exact `ws`. This matters because `relay_ws`'s `finally`
    calls `unregister` on ITS OWN connection's teardown — if a worker
    reconnects (a fresh `relay_ws` invocation calls `register` with a NEW
    ws) while the OLD `relay_ws` is still unwinding (e.g. blocked in
    `receive_text()` on an already-dead socket, or just slow to notice),
    the old teardown must never evict the new live connection or fail the
    new connection's in-flight requests — only the old connection's OWN
    requests are genuinely dead.
    """

    def __init__(self, request_idle_timeout_s: Optional[float] = None) -> None:
        # #928: per-request idle-between-frames deadline. None (the production
        # construction, `create_app()`) → read the operator's setting per
        # request; an explicit value pins it, which is what lets tests drive the
        # bound in milliseconds instead of waiting out a real one.
        self._request_idle_timeout_s = request_idle_timeout_s
        self._ws: Dict[str, Any] = {}
        self._pending: Dict[str, Dict[str, tuple]] = {}  # req_id -> (ws, queue)
        # Per-CONNECTION send lock (fix round 2, #262 review, FIX 2). Parallel
        # to `self._ws` and mutated in lockstep with it (set in `register`,
        # cleared in `unregister`) — every send on a worker's WS (the `req` in
        # `open_request`, `_read_loop`'s `pong` reply, and the best-effort
        # `cancel`) goes through this lock so concurrent senders never
        # interleave their writes and corrupt the JSON frame stream the node's
        # reader decodes. Mirrors the node's own per-connection `send_lock`
        # (`relay_client.run_client`). First-connection-wins (`register` below)
        # guarantees at most one live ws per worker_id, so keying the lock by
        # worker_id is effectively per-connection.
        self._locks: Dict[str, asyncio.Lock] = {}

    def _idle_timeout(self) -> Optional[float]:
        """Seconds of silence that fail ONE in-flight request, or None for
        unbounded (#928). An explicit constructor value wins over the setting."""
        if self._request_idle_timeout_s is not None:
            return (self._request_idle_timeout_s
                    if self._request_idle_timeout_s > 0 else None)
        return _configured_idle_timeout()

    # --- registry -----------------------------------------------------

    def register(self, worker_id: str, ws: Any) -> bool:
        """FIRST-CONNECTION-WINS (fix round 2, #262 review, FIX 3): record
        `ws` as the live connection for `worker_id` ONLY if no live
        connection already holds that slot. Returns ``True`` on success
        (and creates the connection's send lock), ``False`` WITHOUT touching
        any existing state when the slot is already live — the caller
        (`relay_ws`) then closes the duplicate with app code 4409.

        Rejecting a live-overwrite kills the eviction-hijack: under the
        default ``command_key_mode=allow`` the shared node key authenticates
        as ANY worker_id, so without this a second worker could dial another
        worker's id and silently steal its traffic by overwriting the live
        registration. First-wins removes the overwrite entirely.

        A genuine reconnect after the OLD socket died still works: the old
        connection's `relay_ws` `finally` calls `unregister(worker_id,
        old_ws)`, which frees the slot (see below); the redialing node backs
        off and retries, and the retry then finds the slot empty and
        succeeds. (A redial that races IN before the old teardown is refused
        with 4409 and simply retries — never permanently locked out.)"""
        if worker_id in self._ws:
            return False
        self._ws[worker_id] = ws
        self._locks[worker_id] = asyncio.Lock()
        self._pending.setdefault(worker_id, {})
        return True

    async def _send(self, ws: Any, lock: Any, frame: Dict[str, Any]) -> None:
        """Serialize one send on `ws` behind its per-connection `lock`
        (FIX 2). `lock` is looked up alongside `ws` by the caller so the two
        can never desync; a missing lock (a ws that isn't the registered one,
        e.g. a torn-down connection) falls back to a bare send rather than
        raising — the identity guards at the call sites already decide
        whether a send should happen at all."""
        await self._send_text(ws, lock, rp.encode(frame))

    async def _send_text(self, ws: Any, lock: Any, text: str) -> None:
        """The `_send` half that takes an ALREADY-ENCODED frame — so a caller
        that had to encode the frame anyway (to measure it against
        `rp.MAX_FRAME_SIZE`, see `open_request`) sends those exact bytes
        instead of paying for a second `rp.encode` of a possibly-32-MiB
        frame."""
        if lock is None:
            await ws.send_text(text)
            return
        async with lock:
            await ws.send_text(text)

    def unregister(self, worker_id: str, ws: Any) -> None:
        """Connection-scoped teardown: fail only the in-flight requests
        THIS `ws` owns, and only clear the registry's live-connection slot
        if it still points at THIS `ws` (see class docstring — a
        reconnect race is exactly why this takes `ws`, not just
        `worker_id`). Each owned pending queue gets the `_DROP` sentinel,
        which `open_request` turns into `RelayUnavailable` the next time
        it reads that queue (whether it's still waiting for the head, or
        mid-stream waiting for the next chunk) — never leaves a caller
        hanging."""
        if self._ws.get(worker_id) is ws:
            self._ws.pop(worker_id, None)
            # Clear the connection's send lock in lockstep with its ws (FIX 2),
            # and only when THIS ws still owns the slot — a stale/late
            # `unregister` for an already-reclaimed slot must not drop the new
            # connection's lock (same identity guard as the ws pop above).
            self._locks.pop(worker_id, None)
        owned = [
            (req_id, queue)
            for req_id, (owner_ws, queue) in self._pending.get(worker_id, {}).items()
            if owner_ws is ws
        ]
        for req_id, queue in owned:
            self._pending[worker_id].pop(req_id, None)
            _force_drop(queue)   # LLMM-10: the queue is bounded now
        # M3 (#929): drop the worker's now-EMPTY pending map instead of leaving
        # an empty `{}` behind for every worker uuid this manager has ever seen.
        # `register` creates it with `setdefault` and nothing removed it — a
        # slow leak, bounded by the number of DISTINCT worker ids (fleet size
        # plus every re-enrolled/retired node), so cosmetic rather than
        # dangerous, but there is no reason to keep it.
        #
        # Emptiness is the whole condition, and it is enough even under the
        # reconnect race this method exists to handle: a map that still holds
        # entries belongs to a connection with requests in flight (ours are
        # popped just above, so anything left is the NEW connection's) and is
        # kept; an empty one is recreated by the `setdefault` in `register` /
        # `open_request` the moment it is needed again.
        if not self._pending.get(worker_id, True):
            self._pending.pop(worker_id, None)

    def is_connected(self, worker_id: str) -> bool:
        return worker_id in self._ws

    # --- demux read loop ------------------------------------------------

    async def _read_loop(self, worker_id: str, ws: Any) -> None:
        """Read frames off `ws` until it closes/raises, routing each one to
        the matching in-flight request's queue by `id`. Runs for the
        lifetime of one worker connection — the WS endpoint (`relay_ws`)
        owns starting this (as a plain `await`, not a background task —
        the endpoint coroutine IS the read loop) and calling `unregister`
        in a `finally` once it returns/raises.

        Deliberately tolerant of a single malformed/unroutable frame: a
        `RelayProtocolError` or an id with no matching in-flight request
        (already timed out, cancelled, or a stray/late frame) is logged
        and skipped rather than tearing down the whole connection — one
        bad frame must not fail every OTHER in-flight request sharing this
        socket.
        """
        while True:
            text = await ws.receive_text()
            try:
                frame = rp.decode(text)
            except rp.RelayProtocolError:
                logger.warning("relay: dropping malformed frame from worker %s", worker_id)
                continue

            t = frame.get("t")
            if t == "pong":
                continue  # keepalive ack, nothing to route
            if t == "ping":
                # Keepalive from the worker — best-effort reply, never let a
                # failed pong tear down the read loop. Through the connection's
                # send lock (FIX 2) so it can't interleave with a concurrent
                # `open_request`/`cancel` send on the same ws.
                try:
                    await self._send(ws, self._locks.get(worker_id), rp.pong())
                except Exception:  # pragma: no cover - defensive
                    logger.warning("relay: pong reply to worker %s failed", worker_id, exc_info=True)
                continue

            req_id = frame.get("id")
            entry = self._pending.get(worker_id, {}).get(req_id)
            if entry is None:
                # No such in-flight request (never existed, already
                # finished, or this is a stray retransmit) — safe to drop.
                continue
            _owner_ws, queue = entry
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # LLMM-10: this request's consumer is behind. Wait (which stops
                # us draining the socket → TCP backpressure to the worker), but
                # only up to the deadline — see RESPONSE_QUEUE_MAXSIZE above for
                # why an unbounded wait would deadlock every other request on
                # this connection.
                try:
                    await asyncio.wait_for(queue.put(frame),
                                           timeout=RESPONSE_QUEUE_PUT_TIMEOUT_S)
                except asyncio.TimeoutError:
                    logger.warning(
                        "relay: response queue for request %s on worker %s stayed "
                        "full for %.0fs — dropping the request (slow or gone "
                        "client) rather than buffering without bound",
                        req_id, worker_id, RESPONSE_QUEUE_PUT_TIMEOUT_S)
                    self._pending.get(worker_id, {}).pop(req_id, None)
                    _force_drop(queue)
                    try:  # stop the worker burning GPU on a response nobody reads
                        await self._send(ws, self._locks.get(worker_id),
                                         rp.cancel(req_id))
                    except Exception:  # pragma: no cover - defensive
                        pass

    # --- request/response streaming --------------------------------------

    async def open_request(
        self,
        worker_id: str,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: str,
        *,
        b64: bool = False,
    ) -> AsyncIterator[Any]:
        """Relay one HTTP request to `worker_id` over its WS and stream the
        response back.

        Async-generator contract: the FIRST yielded item is a
        `RelayResponse(status, headers)` — status/headers as reported by the
        worker's local HTTP client, i.e. this call genuinely waits for the
        worker's `head` frame before yielding (matches what a caller
        building a `StreamingResponse` needs: a real status/headers BEFORE
        it can construct one). Every subsequent yielded item is a `bytes`
        chunk of the response body, until the worker's `end` frame closes
        the stream (plain `StopAsyncIteration`, no final sentinel object).

        Raises `RelayUnavailable`:
          * immediately, if `worker_id` has no live WS at all;
          * immediately, if the framed request would exceed
            `rp.MAX_FRAME_SIZE` (#929) — refused BEFORE it reaches the wire,
            because an oversize frame costs the whole multiplexed connection
            (1009 close → every in-flight request on this worker dropped),
            not just this one request;
          * while waiting for the head, or mid-stream, if the WS drops
            (`unregister` was called, e.g. from `relay_ws`'s disconnect
            handler) or the worker sends an `err` frame instead of the
            expected one;
          * when NO frame at all arrives for `RelayHub._idle_timeout()`
            seconds (#928) — the wedged-engine-on-a-live-socket case, which
            neither of the two paths above notices. The bound is between
            CONSECUTIVE frames, so a long streaming completion is unaffected;
            the `finally` below then cancels the request on the worker exactly
            as an abandon would.

        `req_id` is a fresh `uuid4().hex` per call, so concurrent
        `open_request`s against the SAME worker (SAME WS) are isolated —
        `_read_loop` demuxes by this id and nothing else ties them
        together.

        Cancel-on-abandon (#262 Task 3): if this generator is torn down
        WITHOUT having seen the worker's `end` frame — the caller stopped
        iterating early, a client disconnect drove Starlette to `aclose()`
        the `StreamingResponse` body generator (`GeneratorExit`), or any
        exception (including `RelayUnavailable`) propagated out — its
        `finally` sends a best-effort `cancel(req_id)` to the worker, so an
        abandoned request doesn't keep burning the worker's GPU for a
        response nobody will read. Best-effort only: never raises, and
        skipped entirely if the worker's connection already changed (a
        reconnect replaced `ws` with a new live connection) or was never
        actually live to begin with.
        """
        ws = self._ws.get(worker_id)
        if ws is None:
            raise RelayUnavailable(f"worker {worker_id!r} is not connected to the relay")
        # Capture the connection's send lock alongside `ws` (FIX 2) — looked up
        # together (no await between) so they're a consistent snapshot, and the
        # identity guards below (`self._ws.get(worker_id) is ws`) guarantee that
        # if `ws` is still the live connection then this is still its lock.
        lock = self._locks.get(worker_id)

        req_id = uuid.uuid4().hex
        # LLMM-10: BOUNDED — see RESPONSE_QUEUE_MAXSIZE. An unbounded queue let a
        # fast worker buffer a whole response in manager memory whenever the
        # downstream client read slower than the worker produced.
        queue: asyncio.Queue = asyncio.Queue(maxsize=RESPONSE_QUEUE_MAXSIZE)
        # Tag this entry with the SPECIFIC ws it's sent on (fix round 1):
        # `unregister(worker_id, ws)` is connection-scoped and only drops
        # entries owned by the ws it's tearing down, so a reconnect on the
        # SAME worker_id (a different ws) never DROPs this one by mistake.
        self._pending.setdefault(worker_id, {})[req_id] = (ws, queue)
        # Completes the Task-2 contract for Task 3 (#262 review): the HTTP
        # relay route (below) cannot send a keyed `cancel` frame itself — by
        # the time it would want to, `req_id` is a local here, never handed
        # back out. So THIS generator's own `finally` sends a best-effort
        # `cancel(req_id)` whenever it's tearing down WITHOUT having seen the
        # worker's `end` frame — a client disconnect (Starlette `aclose()`s
        # the StreamingResponse body generator -> `GeneratorExit` lands here),
        # a `RelayUnavailable` raise, or any other early exit. Tracked with a
        # plain flag rather than inferred from control flow, so it's correct
        # regardless of which of the several exit paths above is taken.
        saw_end = False
        # #928: read the bound ONCE per request so a settings change mid-stream
        # can't move this request's deadline under it.
        idle_timeout = self._idle_timeout()

        async def _next_frame(stage: str) -> Any:
            """`queue.get()` with the #928 idle bound applied.

            Every wait in this generator goes through here, so the deadline is
            between CONSECUTIVE frames rather than over the whole request: each
            arriving frame starts a fresh window, which is what lets a genuine
            multi-minute completion stream on while a wedged engine (accepted
            the request, emitted nothing, socket still healthy — the one case
            neither the WS-drop nor the client-disconnect recovery path covers)
            gives its slot back. `wait_for` cancels the pending `get()` on
            timeout; losing a frame that arrived in that exact instant is
            immaterial because this request is being failed anyway, and the
            `finally` below still pops the pending entry and sends the
            best-effort `cancel` that stops the worker generating.
            """
            if idle_timeout is None:
                return await queue.get()
            try:
                return await asyncio.wait_for(queue.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "relay: request %s on worker %s got no %s for %.0fs — failing it "
                    "(idle timeout) rather than pinning the slot indefinitely (#928)",
                    req_id, worker_id, stage, idle_timeout)
                raise RelayUnavailable(
                    f"worker {worker_id!r} sent no {stage} within {idle_timeout:g}s "
                    f"(relay idle timeout)"
                ) from None

        try:
            try:
                encoded_req = rp.encode(
                    rp.req(req_id, method, path, headers, body, b64=b64))
                if rp.exceeds_max_frame(encoded_req):
                    # #929 (M-oversize, from the #933 review): an over-cap frame
                    # is NOT a per-request problem once it is on the wire — the
                    # peer answers a >32 MiB frame with a 1009 close, and this
                    # connection is MULTIPLEXED, so `unregister` then `_DROP`s
                    # every OTHER in-flight request on this worker. Refusing to
                    # SEND it keeps the blast radius at the one request that is
                    # too big (the HTTP route turns this into its 502), which is
                    # the only request actually at fault.
                    raise RelayUnavailable(
                        f"request body is too large to relay to worker "
                        f"{worker_id!r} ({len(encoded_req.encode('utf-8'))} bytes "
                        f"framed, cap {rp.MAX_FRAME_SIZE}) — refusing rather than "
                        f"closing the shared relay connection")
                await self._send_text(ws, lock, encoded_req)
            except RelayUnavailable:
                raise
            except Exception as exc:
                # A drop between the connectivity check above and the actual
                # send (e.g. the socket died in between) — fail this request
                # the same way a post-send drop would, not with whatever raw
                # exception the transport happens to raise.
                raise RelayUnavailable(f"worker {worker_id!r} send failed: {exc}") from exc

            item = await _next_frame("response head")
            if item is _DROP:
                raise RelayUnavailable(f"worker {worker_id!r} disconnected before responding")
            if item["t"] == "err":
                raise RelayUnavailable(item.get("error") or "relay error")
            if item["t"] != "head":
                raise RelayUnavailable(
                    f"worker {worker_id!r} sent {item['t']!r} before a head frame")
            status = item.get("status")
            if status is None:
                raise RelayUnavailable(
                    f"worker {worker_id!r} sent a head frame with no status")
            yield RelayResponse(status, dict(item.get("headers") or {}))

            while True:
                item = await _next_frame("response data")
                if item is _DROP:
                    raise RelayUnavailable(f"worker {worker_id!r} disconnected mid-request")
                kind = item["t"]
                if kind == "chunk":
                    data = item.get("data", "")
                    if item.get("b64"):
                        yield base64.b64decode(data)
                    else:
                        yield data.encode("utf-8")
                elif kind == "end":
                    saw_end = True
                    return
                elif kind == "err":
                    raise RelayUnavailable(item.get("error") or "relay error")
                # Anything else (stray req/cancel/head) is a protocol
                # oddity, not a hard failure — ignore and keep streaming.
        finally:
            self._pending.get(worker_id, {}).pop(req_id, None)
            if not saw_end and self._ws.get(worker_id) is ws:
                # Best-effort only: guarded to the SAME identity check
                # `unregister` uses (a reconnect may already have replaced
                # `ws` with a new live connection — never send on behalf of
                # a request the CURRENT connection knows nothing about), and
                # wrapped so a dead/half-closed socket can never raise out of
                # a `finally` (that would shadow whatever real exception/
                # GeneratorExit is already propagating through this frame).
                try:
                    await self._send(ws, lock, rp.cancel(req_id))
                except Exception:  # pragma: no cover - defensive
                    pass


# --- WS endpoint -----------------------------------------------------------


async def relay_ws(websocket: "WebSocket", worker_id: str) -> None:
    """The worker-facing WS endpoint: `GET /api/workers/{worker_id}/relay`
    (mounted by Task 3/5's `register_relay`, not here).

    Auth reuses `authorize_command_node` — the SAME per-worker node-key
    Bearer check `/api/workers/{id}/commands/claim` uses (fail-closed: no
    node key configured -> 503-equivalent close, missing/invalid token ->
    close before ever registering). This is deliberately NOT a new auth
    mechanism: the relay is worker-federation control-plane traffic, same
    trust tier as the command channel.

    The hub is read off `websocket.app.state.relay_hub` (wired by Task 5's
    `create_app()`, one `RelayHub()` per manager process) rather than taken
    as a parameter, because FastAPI's `@app.websocket(...)` decorator calls
    endpoint functions with just the path params + injected dependencies —
    keeping the signature `(websocket, worker_id)` is what let Task 3 mount
    this directly.

    The `websocket` parameter is annotated `WebSocket` (fix round 2, #262
    review): FastAPI decides which param IS the socket by resolving the
    annotation to a `starlette.websockets.WebSocket` subclass at route-
    registration time. The previous `Any` annotation resolved to
    ``typing.Any``, so FastAPI treated `websocket` as a required QUERY
    param and rejected EVERY handshake with a 1008 validation close before
    this body ever ran — a latent break the (previously absent) handshake
    tests now cover. The name `WebSocket` is a bare string here under `from
    __future__ import annotations` and is injected into this module's globals
    lazily by `register_relay` (which runs only where FastAPI is present), so
    a bare ``from app.relay import RelayHub`` still drags in no ASGI stack.
    """
    from starlette.websockets import WebSocketDisconnect

    from app.api._ids import parse_uuid
    from app.api.workers import authorize_command_node
    from app.config import get_settings
    from app.db import session_scope
    from app.models import Worker

    authorization = websocket.headers.get("authorization")
    worker_name: Optional[str] = None
    try:
        with session_scope() as s:
            w = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            worker_name = w.name if w is not None else None
        authorize_command_node(authorization, worker_name, mode=get_settings().command_key_mode)
        # Resolve the hub BEFORE accept() (fix round 1, #262 review):
        # accepting the socket commits us to eventually calling
        # `unregister` in the `finally` below — if that lookup were done
        # AFTER accept() and it failed (e.g. a `create_app()` wiring bug
        # that left `app.state.relay_hub` unset), we'd have an accepted,
        # never-registered, never-unregistered socket. Doing it here means
        # any failure takes the SAME fail-closed close-before-accept path
        # as an auth failure.
        hub = websocket.app.state.relay_hub
    except Exception:
        # Fail-closed: never accept()/register() an unauthenticated peer
        # (or one we can't hand off to a hub). Close code 4401 is in the
        # app-defined range (4000-4999) — there is no standard WS close
        # code for "HTTP 401 equivalent".
        logger.warning("relay: rejecting WS for worker_id=%s (auth/setup failed)", worker_id)
        await websocket.close(code=4401)
        return

    # First-connection-wins (FIX 3): refuse a duplicate dial for a worker_id
    # that already has a LIVE connection, so a second worker (which, under the
    # default command_key_mode=allow, authenticates as ANY id with the shared
    # node key) cannot overwrite the live registration and steal its traffic.
    # Fast path: reject BEFORE accept() when we can already see the slot is
    # live — a clean handshake rejection. The authoritative guard is
    # `register()`'s return below, which also closes the accept-window race
    # (another dial registering during our `await accept()`).
    if hub.is_connected(worker_id):
        logger.warning(
            "relay: worker_id=%s already has a live connection — refusing duplicate dial",
            worker_id,
        )
        await websocket.close(code=4409)
        return

    await websocket.accept()
    if not hub.register(worker_id, websocket):
        # Lost the accept-window race: another dial registered this worker_id
        # while we were accepting. Refuse with the same 4409 and return BEFORE
        # the try/finally, so this never-registered socket's teardown doesn't
        # run `unregister` against the live connection's slot (the identity
        # guard in `unregister` would refuse it anyway, but not entering is
        # cleaner).
        logger.warning(
            "relay: worker_id=%s registered concurrently — refusing duplicate dial",
            worker_id,
        )
        await websocket.close(code=4409)
        return
    logger.info("relay: worker %s (%s) connected", worker_id, worker_name)
    try:
        await hub._read_loop(worker_id, websocket)
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - defensive: never let a read-loop
        # bug leave the worker registered with a dead socket.
        logger.warning("relay: read loop for worker %s ended abnormally", worker_id, exc_info=True)
    finally:
        # Connection-scoped teardown: pass OUR OWN websocket, not just the
        # worker_id, so a reconnect that happened while we were still
        # unwinding never gets evicted by us (see RelayHub.unregister).
        hub.unregister(worker_id, websocket)
        logger.info("relay: worker %s (%s) disconnected", worker_id, worker_name)


def register_relay(app) -> None:
    """Mount both relay routes on `app`: the worker-facing WS ingress
    (`relay_ws`, Task 2, directly) and the HTTP mux route (Task 3, via
    `app.relay_http.register_http_relay`).

    The HTTP half is wired via a FUNCTION-LOCAL import of `app.relay_http`
    (fix round 1, #262 review) — that sibling module carries the top-level
    `fastapi`/`starlette` imports the HTTP handler's `Request` annotation
    needs (see `app.relay_http`'s docstring for why it can't live here).
    Importing it only HERE, inside this function, means `from app.relay
    import RelayHub` (what the fast, FastAPI-free unit-test tier does) never
    imports `app.relay_http` as a side effect, and so never drags in the
    ASGI stack — this is what restores Task 2's invariant that a bare
    import of THIS module needs no fastapi/starlette at all.

    Deliberately NOT called from `create_app()` — Task 5 wires this in, once
    a `RelayHub()` instance exists on `app.state.relay_hub` for both routes
    to share. Calling this twice on the same `app` would double-register
    both routes; callers own not doing that (mirrors every other
    `register_*` helper in this package — none of them are idempotent
    either).
    """
    # Make `relay_ws`'s string annotation `websocket: "WebSocket"` resolvable
    # (fix round 2, #262 review): FastAPI resolves a handler's annotations
    # against the handler's OWN module globals at route-registration time, and
    # only recognises the injected socket when that annotation resolves to a
    # `WebSocket` subclass. We import it HERE (where FastAPI is present) and
    # bind it into this module's globals, so a bare `from app.relay import
    # RelayHub` in the FastAPI-free unit tier still imports no ASGI stack.
    from starlette.websockets import WebSocket

    globals()["WebSocket"] = WebSocket

    app.websocket("/api/workers/{worker_id}/relay")(relay_ws)

    from app.relay_http import register_http_relay

    register_http_relay(app)
