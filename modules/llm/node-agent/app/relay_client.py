# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Worker↔master WS inference relay — node side (#262 Task 7).

Counterpart to `app.relay` on the manager (master) side. A remote worker
holds ONE long-lived outbound WebSocket to the master; the master sends
`req` frames over it (framed via `app.relay_protocol`) and this module
drives the node's LOCAL engine (llama.cpp/vLLM container, addressed via
`engine_base_for`) to serve them, streaming the response back as
`head`/`chunk`/`end` frames — `err` only for a genuine connect/transport
failure, never for an HTTP-level 5xx from the engine (that's just a normal
response to relay through).

Two pieces:

* `handle_frame` — given ONE `req` frame, makes the local engine call and
  drives `send` through head/chunk*/end (or `err` on failure). Pure
  request/response logic, no WS knowledge — takes `send` as an injected
  async callable so it has no idea whether frames are going out over a
  real WS, a fake queue in a test, or anywhere else.
* `run_client` — the dial loop: connects, reads frames off the WS,
  dispatches each `req` as its OWN `asyncio.Task` (keyed by request id) so
  a slow/streaming request never blocks the read loop or a concurrent
  request, honours `cancel` frames by cancelling the matching task,
  answers `ping` with `pong`, and redials with a backoff on drop until
  `stop` (an `asyncio.Event`-like) is set.

Both `connect` and `http_client` are injected — this module is unit-tested
entirely with fakes, no real sockets and no real HTTP (see
`tests/unit/llm-node-agent/test_262_relay_client.py`). `run_client`'s
DEFAULT `connect` lazily imports a real WS client library INSIDE the
function body (never at module import time — importing this module must
stay side-effect-free in the unit tier, which has no WS library
installed). As of Task 7, neither `websockets` nor `httpx-ws` is in
`modules/llm/node-agent/requirements.txt` — Task 9 adds the actual
dependency; until then the default `connect` raises a clear `RuntimeError`
if the optimistic `websockets` import fails, which is fine because every
unit test injects its own fake `connect` and never reaches it.

Task 8 wires this into the node's live runtime (spawning `run_client` as a
background task with the real manager URL / node key / `httpx.AsyncClient`
/ engine map) — this module intentionally does not touch `runtime.py` or
`commands.py`.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from app import relay_protocol as rp

logger = logging.getLogger("node_agent.relay_client")

# Sentinel returned by `_first_of` when `stop` won the race instead of the
# awaited work — distinct from any real frame/None a coroutine could return.
_STOPPED = object()

# Sentinel returned by `_first_of` when the connection's own keepalive declared
# the socket dead (see `run_client`'s heartbeat) instead of `stop` firing or a
# frame arriving. Distinct from `_STOPPED` because the two mean opposite things
# to the dial loop: `stop` -> return, dead -> redial.
_DEAD = object()

# #929 M5: app-level keepalive on the node's outbound connection.
#
# `ping`/`pong` were DEFINED by the codec and HANDLED by both ends from the
# start, but nobody ever SENT one — the keepalive the protocol implied did not
# exist. What did exist is the transport-level ping the WS libraries run on
# their own (uvicorn's websockets impl on the master, the `websockets` client
# here — both default to 20s/20s, now pinned explicitly in `_default_connect`
# rather than inherited), which catches a black-holed TCP path (NAT drop, no
# FIN) but says nothing about the master's APPLICATION being alive: a wedged
# `RelayHub._read_loop` still answers transport pings from inside the library,
# while every request this node serves goes unanswered.
#
# So the node originates an app-level `ping` on an idle connection and treats
# prolonged silence — no frame of ANY kind, including the `pong` the master's
# read loop sends back — as a dead connection, dropping it so the dial loop
# redials. The node is the right end to own this: it holds the outbound socket
# and is the side that can re-establish it. (The master needs no equivalent: a
# request wedged on a live-but-useless socket is already bounded by #928's
# per-request idle timeout, and it answers pings rather than sending them.)
#
# Interval < timeout by a wide margin, so a single lost/late pong can never
# be enough to tear down a healthy connection.
DEFAULT_HEARTBEAT_INTERVAL_S = 20.0
DEFAULT_HEARTBEAT_TIMEOUT_S = 60.0

# #929 (M-oversize, from the #933 review): the master's WS accepts frames up to
# `rp.MAX_FRAME_SIZE`; a bigger one is answered with a 1009 close that takes the
# WHOLE multiplexed connection — and with it every OTHER request this node is
# streaming — not just the oversize response. So a large response body is SPLIT
# across several `chunk` frames here instead. Splitting is lossless: the master
# concatenates chunk payloads in order, and a split that lands mid-UTF-8-
# sequence just makes that piece take the base64 path, which round-trips the
# exact bytes (see the `b64` handling below).
_MAX_CHUNK_BYTES = rp.MAX_CHUNK_PAYLOAD_BYTES


def _safe_relay_path(path: Any) -> bool:
    """Is ``path`` a request-target this node may append to its engine base?

    NODE-14. ``url = f"{base}{path}"`` is string concatenation, so the shape of
    ``path`` decides which HOST is dialled:

    * ``"/v1/chat/completions"`` — the only legitimate form, appended to the base.
    * ``"//evil.example/x"``     — a protocol-relative URL: httpx resolves it
      against the base's SCHEME, so the request goes to evil.example.
    * ``"@evil.example/x"``      — turns ``http://engine:8080`` into a userinfo
      prefix, so the request goes to evil.example.
    * anything with a scheme, or a bare relative path, is likewise not something
      the master is supposed to be able to make this node dial.

    Requiring exactly one leading slash covers all of them. Kept as a plain
    predicate (no exception) so ``handle_frame`` answers with an ``err`` frame
    for that one request instead of tearing down the relay.
    """
    return (isinstance(path, str) and path.startswith("/")
            and not path.startswith("//") and "\\" not in path)


def _split_piece(piece: Any) -> list:
    """#929: one response piece, cut into segments that each fit a `chunk`
    frame (`_MAX_CHUNK_BYTES`).

    Returns ``[piece]`` unchanged for anything within the budget — the
    overwhelming majority (a streaming SSE piece is a few hundred bytes), so
    the hot path pays one `len()` and nothing else. `bytes` are cut on BYTE
    boundaries (byte-lossless, see the `b64` handling in `handle_frame`); a
    `str` piece is cut on CHARACTER boundaries against the same byte budget,
    which is conservative (a character is ≥1 byte) and never splits a
    codepoint.
    """
    if isinstance(piece, bytes):
        if len(piece) <= _MAX_CHUNK_BYTES:
            return [piece]
        return [piece[i:i + _MAX_CHUNK_BYTES]
                for i in range(0, len(piece), _MAX_CHUNK_BYTES)]
    if isinstance(piece, str):
        if len(piece.encode("utf-8", errors="surrogateescape")) <= _MAX_CHUNK_BYTES:
            return [piece]
        return [piece[i:i + _MAX_CHUNK_BYTES]
                for i in range(0, len(piece), _MAX_CHUNK_BYTES)]
    return [piece]  # pragma: no cover - defensive: an exotic piece type


async def handle_frame(
    frame: Dict[str, Any],
    *,
    send: Callable[[Dict[str, Any]], Awaitable[None]],
    http_client: Any,
    engine_base_for: Callable[[str, Optional[Any]], str],
) -> None:
    """Serve ONE `req` frame against the local engine, streaming the
    response back through `send`.

    `http_client` is an `httpx.AsyncClient`-like object: `.stream(method,
    url, headers=, content=)` used as an async context manager, yielding a
    response with `.status_code`, `.headers`, and `.aiter_bytes()` (falls
    back to `.aiter_raw()` if that's what the fake/client offers).
    `engine_base_for(path, body)` resolves the engine's ROOT base URL (e.g.
    `http://inst-1:8080`, no `/v1`) for this request; `path` (e.g.
    `/v1/chat/completions`) is concatenated onto it directly.

    #1535: the BODY is passed as well because that is where the target model
    is named — a worker runs many engines and picks the one this request is
    for (#262's "the worker picks a local engine"). ONE calling convention:
    the resolver is always called `(path, body)`, and whatever it raises
    reaches the client as an `err` frame. The first cut wrapped the call in
    `except TypeError: engine_base_for(path)` as compatibility with a 1-argument
    resolver; since the installed resolver takes two, that retry re-entered the
    SAME function with `body=None` and degraded any internal TypeError into
    "first ready engine" — the wrong weights under the right name, which is the
    failure this change exists to remove (revA finding 2).

    Contract (both branches always send SOMETHING for `id`, so the master
    never hangs waiting on a request it sent):

    * Engine responds (any status, including 5xx — that is a normal HTTP
      response, not a failure of the relay itself): `head(id, status,
      headers)`, then one `chunk(id, data)` per streamed piece (decoded
      utf-8; base64-flagged via the codec's own `b64` handling when a
      piece isn't valid utf-8), then `end(id)`.
    * The engine call/stream itself raises (connect refused, DNS failure,
      transport error mid-stream, …): `err(id, message)` instead. This is
      the ONLY case that produces `err`.
    * `asyncio.CancelledError` (the request's task was cancelled — see
      `run_client`'s handling of `cancel` frames) is never turned into an
      `err`/`end` — it propagates so the caller's task shows as cancelled
      and the master, having already sent `cancel`, is not sent a
      confusing `end`/`err` for a request it gave up on.

    Non-`req` frames are a caller error (run_client dispatches
    `cancel`/`ping`/`pong` itself and only ever calls this for `req`) —
    handled as a silent no-op here rather than raising, so a stray call
    can never crash the per-request task.
    """
    if frame.get("t") != "req":
        return

    req_id = frame["id"]
    method = frame.get("method", "GET")
    path = frame.get("path", "/")
    headers = frame.get("headers") or {}
    body = frame.get("body")
    # Lossless binary request bodies (#262 review, FIX 4): the master
    # base64-encodes a non-utf-8 request body and flags `b64: true` on the
    # `req` frame (mirroring the response direction's `chunk(b64=...)`).
    # Decode back to the EXACT raw bytes here before handing it to the
    # engine; a plain utf-8 body stays a `str` (httpx encodes it) exactly as
    # before, so the common path is unchanged.
    if frame.get("b64") and isinstance(body, str):
        body = base64.b64decode(body)

    if not _safe_relay_path(path):
        # NODE-14: `path` is concatenated onto the engine base verbatim. Today's
        # only producer (manager/app/api/relay_http.py) always sends f"/{path}",
        # so this is safe by the master's FORMATTING — not by anything the node
        # checks. A producer change (or a compromised master) sending
        # "@evil.example/x" or "//evil.example/x" would re-target this node's
        # HTTP call at another host WITH the caller's headers attached. The
        # node's trust boundary must not depend on the master's string handling.
        logger.warning("relay: refusing request %s with unsafe path %r", req_id, path)
        await send(rp.err(req_id, f"refusing unsafe relay path {path!r}"))
        return

    try:
        # #1535: the resolver gets the body too — the model it names is what
        # selects the engine. ONE calling convention, no arity guessing: revA
        # finding 2 showed the `except TypeError` fallback here re-called the
        # SAME two-argument resolver with body=None, so any TypeError raised
        # INSIDE it degraded silently to "first ready engine" — the right model
        # name served by the wrong weights, which is the #929 failure this
        # whole change exists to remove. A resolver that raises must reach the
        # caller as an `err` frame.
        base = engine_base_for(path, body)
        url = f"{base.rstrip('/')}{path}"
        stream_cm = http_client.stream(method, url, headers=headers, content=body)
        async with stream_cm as resp:
            await send(rp.head(req_id, resp.status_code, dict(resp.headers)))

            aiter = resp.aiter_bytes if hasattr(resp, "aiter_bytes") else resp.aiter_raw
            async for piece in aiter():
                if not piece:
                    continue
                # #929: never build a frame the master would have to answer
                # with a 1009 close — see `_MAX_CHUNK_BYTES`. A piece within
                # the budget (every streaming SSE chunk, in practice) takes
                # exactly the same single-frame path as before.
                for segment in _split_piece(piece):
                    if isinstance(segment, bytes):
                        try:
                            text = segment.decode("utf-8")
                            b64 = False
                        except UnicodeDecodeError:
                            # Preserve exact bytes via surrogateescape — the
                            # codec's chunk() builder re-encodes the same way
                            # before base64'ing, so this round-trips exactly.
                            # (This is also what makes splitting mid-sequence
                            # lossless: an incomplete UTF-8 sequence at a
                            # segment boundary simply takes this branch.)
                            text = segment.decode("utf-8", errors="surrogateescape")
                            b64 = True
                    else:
                        text = segment
                        b64 = False
                    await send(rp.chunk(req_id, text, b64=b64))

            await send(rp.end(req_id))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - genuinely any engine/transport failure
        logger.warning("relay: request %s failed", req_id, exc_info=True)
        await send(rp.err(req_id, str(exc)))


async def _first_of(coro: Awaitable[Any], stop: "asyncio.Event",
                    dead: Optional["asyncio.Event"] = None) -> Any:
    """Await `coro`, but bail out with `_STOPPED` the instant `stop` is set
    instead of only noticing it on the next loop iteration.

    This is what lets `run_client` react to `stop` promptly even while
    blocked on a WS read (or a backoff sleep) that would otherwise never
    return on its own — a real WS's `receive_text()` blocks until the next
    frame or a drop, neither of which `stop` being set implies. Whichever
    side doesn't win the race is cancelled and awaited (swallowing its
    `CancelledError`) so nothing is left as an orphaned, never-retrieved
    task.

    `dead` (#929 M5) is the same mechanism for the connection's heartbeat: a
    black-holed socket delivers no frame AND no drop, so `receive_text()` waits
    forever on a connection that will never answer again. The keepalive sets
    that event and this returns `_DEAD`, which the dial loop turns into a
    redial. `stop` WINS a simultaneous race — shutting down beats reconnecting.
    """
    work = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(stop.wait())
    dead_waiter = asyncio.ensure_future(dead.wait()) if dead is not None else None
    watchers = [w for w in (work, waiter, dead_waiter) if w is not None]
    try:
        done, _pending = await asyncio.wait(set(watchers),
                                            return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        if waiter in done:
            return _STOPPED
        return _DEAD
    finally:
        for task in watchers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)


async def _default_connect(url: str, *, headers: Dict[str, str]) -> Any:
    """Real WS dial, used only when `run_client` isn't given a `connect=`
    override (i.e. never, in the unit tier — every test injects a fake).

    Lazily imports `websockets` so a bare `import app.relay_client` stays
    side-effect-free and dependency-free; that import will fail until
    Task 9 adds a WS client library to
    `modules/llm/node-agent/requirements.txt` (and the Dockerfile's
    `pip install -r requirements.txt` picks it up) — until then this
    raises a clear, actionable error instead of a bare `ImportError`.
    """
    try:
        # Use the NEW asyncio client explicitly. On websockets 13.x the
        # top-level `websockets.connect` is still the LEGACY client, whose
        # header kwarg is `extra_headers`; passing `additional_headers` to it
        # falls through to asyncio's create_connection and raises
        # "unexpected keyword argument 'additional_headers'". Only the asyncio
        # client accepts `additional_headers` on 13.x (it becomes the top-level
        # default in 14.x). Importing it directly works on both.
        from websockets.asyncio.client import connect as _ws_connect  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "relay_client._default_connect: no WS client library installed — "
            "the worker-agent needs `websockets` (>=13) in "
            "modules/llm/node-agent/requirements.txt + Dockerfile. Tests "
            "should inject their own `connect=` fake instead of hitting this."
        ) from exc

    # `max_size` governs what THIS SIDE is willing to RECEIVE. The `req`
    # frame the master sends over this connection carries the entire
    # request body (a RAG/long-context/embedding call can easily exceed a
    # few hundred KiB), so this must agree with the codec's own cap
    # (`relay_protocol.MAX_FRAME_SIZE`, 32 MiB) — `websockets` defaults
    # `max_size` to 1 MiB, which would otherwise close the connection (and
    # 502 the request) the first time a body crosses that default (#262
    # pre-ship review, I1).
    # #929 M5: PIN the transport-level keepalive rather than inheriting it.
    # `websockets` currently defaults to 20s/20s, which is what a black-holed
    # WAN path (NAT timeout, no FIN) relies on today — an unstated default in a
    # third-party library is not where the liveness guarantee for a worker's one
    # inference connection should live, and it is exactly the kind of default a
    # major version changes silently. Stated here, alongside the app-level
    # heartbeat `run_client` runs on top of it (which covers the case a
    # transport ping cannot: a master whose library still answers pings while
    # its relay read loop is wedged).
    conn = await _ws_connect(
        url, additional_headers=headers, max_size=rp.MAX_FRAME_SIZE,
        ping_interval=DEFAULT_HEARTBEAT_INTERVAL_S,
        ping_timeout=DEFAULT_HEARTBEAT_INTERVAL_S,
    )
    return _WebsocketsAdapter(conn)


class _WebsocketsAdapter:
    """Adapts a `websockets` client connection (`.send`/`.recv`/`.close`)
    to the `send_text`/`receive_text`/`close` duck type `run_client` uses
    (matching the master side's `RelayHub`, which speaks the same
    `send_text`/`receive_text` vocabulary against a Starlette
    `WebSocket`) — so `run_client` itself stays agnostic to which library
    Task 9 ultimately picks. If Task 9 chooses a library that already
    speaks this vocabulary natively (e.g. `httpx-ws`), this adapter can be
    dropped in `_default_connect` without touching `run_client`."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def send_text(self, text: str) -> None:
        await self._conn.send(text)

    async def receive_text(self) -> str:
        message = await self._conn.recv()
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        return message

    async def close(self) -> None:
        await self._conn.close()


async def run_client(
    worker_id: str,
    *,
    url: str,
    node_key: str,
    connect: Optional[Callable[..., Awaitable[Any]]] = None,
    http_client: Any,
    engine_base_for: Callable[[str, Optional[Any]], str],
    stop: "asyncio.Event",
    backoff: float = 2.0,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
    clock: Optional[Callable[[], float]] = None,
) -> None:
    """Dial `url` and serve inference requests over it until `stop` is set.

    Auth mirrors the command channel (`app.commands`'s
    `authorization: Bearer <node_key>`): `connect(url, headers={
    "authorization": f"Bearer {node_key}"})`. `worker_id` isn't used for
    anything else here (Task 8 may fold it into `url`/logging) — kept as a
    parameter because callers (and logs) need it to identify which
    worker's relay loop this is.

    Dial loop, one iteration per connection attempt:

    1. `connect(...)`. On failure: log, sleep `backoff` (racing `stop` —
       see `_first_of`), retry — unless `stop` won the race, in which case
       return immediately without another dial.
    2. Read frames off the connected `ws` (also racing `stop`, so a `stop`
       set while blocked on a read is noticed immediately, not just on the
       next frame): a `req` frame starts `handle_frame` as its OWN task,
       keyed by the frame's `id`, so concurrent/slow requests never block
       this read loop or each other; `cancel` cancels the matching
       in-flight task (if any — already-finished or unknown ids are a
       no-op); `ping` replies `pong`; `pong` and anything else are
       ignored. Finished tasks are reaped opportunistically before each
       new dispatch so the tracking dict doesn't grow unbounded over a
       long-lived connection. Every write over `ws` — the `pong` reply
       here, and every `head`/`chunk`/`end`/`err` a concurrently-running
       `handle_frame` task sends — goes through the SAME per-connection
       `send_lock`-guarded `_send`, so concurrent senders never interleave
       their writes on the wire.
    3. On WS drop/exception, or `stop` firing mid-read: every still-running
       per-request task for THIS connection is cancelled and awaited (best
       effort — a task's own cleanup, if any, gets to run via
       `CancelledError` same as any other cancellation), then the `ws` is
       closed (best effort). If `stop` is set, return; otherwise sleep
       `backoff` (again racing `stop`) and go back to step 1.

    `backoff` is a plain float (seconds) precisely so tests can pass
    something tiny (e.g. `0.001`) instead of waiting out a real backoff
    interval — production callers (Task 8) should pass something
    meaningful (with jitter, if desired) rather than relying on this
    default.

    KEEPALIVE (#929 M5). Every `heartbeat_interval` seconds of an otherwise
    idle connection this loop sends an app-level `ping`, which the master's
    read loop answers with a `pong`. If NO frame of any kind — pong, response,
    or new request — has arrived for `heartbeat_timeout` seconds, the
    connection is declared dead: it is closed and redialled, which is the only
    thing that recovers a black-holed socket (no frames, no drop, so the read
    would otherwise wait forever while this worker serves nothing). Both are
    seconds, and either being `<= 0` disables the heartbeat entirely — which is
    what the tests that predate it, and any caller that wants only the
    transport-level ping, get by passing 0. `clock` (default
    `time.monotonic`) is injectable purely so a test can drive the staleness
    decision without sleeping out a real timeout.
    """
    if connect is None:
        connect = _default_connect
    _clock = clock or time.monotonic

    while not stop.is_set():
        try:
            ws = await connect(url, headers={"authorization": f"Bearer {node_key}"})
        except Exception:
            logger.warning("relay: connect to %s failed", url, exc_info=True)
            await _first_of(asyncio.sleep(backoff), stop)
            continue

        tasks: Dict[str, "asyncio.Task[None]"] = {}
        # ONE lock per connection (fresh on every reconnect, since it's
        # created inside this `while` iteration, scoped to THIS `ws`).
        # Multiple `handle_frame` tasks run concurrently over the SAME ws
        # (that's the whole point of per-request tasks), and this loop
        # itself writes `pong` replies through the same `_send` — without
        # serializing those writes, two frames "sent at the same time" can
        # have their writes interleaved by the event loop, corrupting the
        # JSON frame stream the master's `_read_loop` decodes. This module
        # is deliberately library-agnostic (see `_WebsocketsAdapter`'s
        # docstring) and must not assume any particular ws client's
        # `send_text` is atomic against concurrent callers — this lock is
        # what actually guarantees it, regardless of which library Task 9
        # picks (#262 review round 1, FIX 1).
        send_lock = asyncio.Lock()

        async def _send(out_frame: Dict[str, Any], _ws: Any = ws) -> None:
            async with send_lock:
                await _ws.send_text(rp.encode(out_frame))

        # --- per-connection app-level keepalive (#929 M5) ------------------
        # `dead` is this connection's own "give up and redial" signal; the read
        # below races it exactly as it races `stop`, because a black-holed
        # socket produces neither a frame nor a drop to wake that read.
        dead = asyncio.Event()
        last_rx = _clock()
        heartbeat: Optional["asyncio.Task[None]"] = None

        async def _keepalive(_ws: Any = ws) -> None:
            while True:
                await asyncio.sleep(heartbeat_interval)
                if _clock() - last_rx >= heartbeat_timeout:
                    logger.warning(
                        "relay: no frame from %s for %.0fs (app-level keepalive "
                        "unanswered) — dropping the connection and redialling",
                        url, _clock() - last_rx)
                    dead.set()
                    try:  # best effort: unblock a read that a real socket
                        await _ws.close()  # would still be waiting on
                    except Exception:  # pragma: no cover - defensive
                        pass
                    return
                try:
                    await _send(rp.ping())
                except Exception:
                    # The send failed — the connection is going away on its
                    # own; the read loop's own error path handles it.
                    return

        if heartbeat_interval > 0 and heartbeat_timeout > 0:
            heartbeat = asyncio.ensure_future(_keepalive())

        try:
            while True:
                raw = await _first_of(ws.receive_text(), stop, dead)
                if raw is _STOPPED:
                    return
                if raw is _DEAD:
                    # #929 M5: the keepalive declared this connection dead.
                    # Leave the try via the same path a drop takes, so the
                    # `finally` tears the connection down and the dial loop
                    # backs off and redials.
                    raise ConnectionError(
                        f"relay: connection to {url} stopped answering "
                        f"(app-level keepalive)")
                last_rx = _clock()

                try:
                    frame = rp.decode(raw)
                except rp.RelayProtocolError:
                    logger.warning("relay: dropping malformed frame from master")
                    continue

                # Reap finished per-request tasks opportunistically. Consume
                # each one's exception (if any) so a `handle_frame` task
                # that died on a genuinely unexpected bug (it already turns
                # ordinary engine/transport failures into an `err` frame
                # itself, so this is only a safety net) doesn't trip
                # asyncio's "exception was never retrieved" warning.
                # `.exception()` raises on a cancelled task, hence the guard
                # (`cancel` frames pop+cancel their task directly, above,
                # and never leave it for this reap to find in the first
                # place — but a task can also self-cancel, so stay defensive).
                for done_id in [rid for rid, t in tasks.items() if t.done()]:
                    finished = tasks.pop(done_id)
                    if not finished.cancelled():
                        finished.exception()

                frame_type = frame.get("t")
                if frame_type == "req":
                    req_id = frame.get("id")
                    tasks[req_id] = asyncio.create_task(
                        handle_frame(
                            frame,
                            send=_send,
                            http_client=http_client,
                            engine_base_for=engine_base_for,
                        )
                    )
                elif frame_type == "cancel":
                    task = tasks.pop(frame.get("id"), None)
                    if task is not None:
                        task.cancel()
                elif frame_type == "ping":
                    await _send(rp.pong())
                # "pong" (keepalive ack) and anything else: nothing to do.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("relay: WS to %s dropped", url, exc_info=True)
        finally:
            if heartbeat is not None:
                # Connection-scoped, like `send_lock` and `tasks`: a keepalive
                # left running would ping a socket this loop has already given
                # up on (and, on the next iteration, a socket it no longer owns).
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            for task in tasks.values():
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)
            try:
                await ws.close()
            except Exception:  # pragma: no cover - defensive, best-effort
                pass

        if stop.is_set():
            return
        await _first_of(asyncio.sleep(backoff), stop)
