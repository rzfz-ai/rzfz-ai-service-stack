# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""HTTP half of the worker↔master WS inference relay — master side
(#262, Task 3, fix round 1: split out of ``app.relay``).

``app.relay`` is deliberately import-safe WITHOUT the ASGI stack (see its
own module docstring) — `RelayHub` only needs an object with async
``send_text``/``receive_text`` methods, never FastAPI/Starlette. The HTTP
relay route can't live there and preserve that: `app.relay` sets ``from
__future__ import annotations``, and FastAPI resolves a route handler's
string annotations against the handler's own *module* globals at
route-registration time — under that future import, a function-local
``from fastapi import Request`` inside `app.relay` would leave ``request:
Request`` unresolvable (the exact trap `proxy.py`'s own test suite
documents). So this sibling module carries the top-level `fastapi`/
`starlette` imports instead, mirroring `proxy.py`'s own house style (a
"hot-path HTTP handler" module, not a "testable-without-FastAPI" one).

``register_http_relay(app)`` is called by ``app.relay.register_relay(app)``
via a FUNCTION-LOCAL import there — so ``from app.relay import RelayHub``
(what the fast, FastAPI-free unit-test tier does) never imports this module,
and never drags fastapi/starlette in as a side effect.
"""
from __future__ import annotations

import hmac
from typing import Dict, Optional

from fastapi import Request
from starlette.responses import JSONResponse, StreamingResponse

from app.auth import AuthError, extract_bearer
from app.config import get_settings
from app.relay import RelayResponse, RelayUnavailable

# Headers stripped in BOTH directions before crossing the relay hop:
# `connection`/`keep-alive`/`transfer-encoding`/`upgrade`/`te`/`trailer`/any
# `proxy-*` are hop-by-hop framing headers for THIS hop, not the worker's;
# `host` names this hop's own host, not the worker's; `content-length` is
# dropped because the body is re-encoded as relay-protocol JSON frames (and,
# on the way back, streamed through `StreamingResponse` chunk-by-chunk) —
# forwarding a stale byte count risks a mismatch neither side can detect.
# `authorization` is stripped (fix round 2, #262 review, FIX 1): the caller at
# THIS hop is the internal LiteLLM router carrying the MASTER's internal key
# (`_relay_http` authenticates it below, constant-time, against
# `litellm_internal_key`). That key is a manager↔router secret and must NOT be
# tunnelled onward to the remote worker's engine — the engine needs no auth,
# and forwarding the master's key over the WAN to a worker box would leak it.
# Mirrors proxy.py, which sets its OWN upstream auth rather than forwarding the
# client's credential.
_HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "transfer-encoding", "upgrade",
    "te", "trailer", "host", "content-length", "authorization",
})


def _strip_hop_by_hop(headers: Dict[str, str]) -> Dict[str, str]:
    """Copy `headers`, dropping the hop-by-hop set above plus any
    `proxy-*` header, keeping whatever original casing the source used."""
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS and not k.lower().startswith("proxy-")
    }


def _auth_err(status_code: int, detail: str) -> JSONResponse:
    """The SAME JSON error envelope `proxy.py::_err` uses, so a caller that
    already handles the hot path's 401/403 shape handles the relay route's
    too (fix round 2, #262 review, FIX 1)."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": detail, "type": "orchestrator"}},
    )


def _header_ci(headers: Dict[str, str], name: str) -> Optional[str]:
    """Case-insensitive lookup into a plain `Dict[str, str]` — the worker's
    reported response headers arrive over the wire as whatever casing its
    local HTTP client used, not necessarily lower-cased."""
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


async def _relay_http(request: Request, worker_id: str, path: str):
    """`GET|POST /relay/{worker_id}/{path:path}` — the HTTP half of the relay
    mux (paired with `app.relay.relay_ws`'s WS half). Drives `RelayHub.
    open_request` for `worker_id` and streams the worker's response back
    verbatim.

    The hub is read off `request.app.state.relay_hub`, same convention as
    `relay_ws` reads `websocket.app.state.relay_hub` — both routes take the
    hub from ASGI app state rather than a parameter, since neither FastAPI's
    `@app.websocket(...)` nor `add_api_route(...)` inject anything beyond
    path params + declared dependencies.

    `open_request`'s async-generator contract (see its docstring) is:
    first yielded item is a `RelayResponse(status, headers)`, everything
    after is a `bytes` body chunk. We `__anext__()` once here to get real
    status/headers BEFORE constructing the `StreamingResponse` — building it
    first (with a placeholder status) would mean a worker-side failure that
    only surfaces at/after the head frame reaches the client as a 200 whose
    body happens to contain an error, the same class of bug `proxy.py`'s
    `_open_upstream_stream` (#323) exists to avoid. The SAME generator
    object is then handed to `StreamingResponse` to yield the remaining
    chunks — a partially-consumed async generator streams the rest exactly
    like a fresh one would.

    `RelayUnavailable` (worker not connected, WS dropped mid-request, or a
    worker `err` frame) maps to a 502 JSON error — there is no more specific
    HTTP status this layer can assign to "the thing on the other end of a
    private WS tunnel didn't answer".

    Caller auth (fix round 2, #262 review, FIX 1): this route reaches a
    worker's GPU, so it must NOT be open to any container on the shared
    docker network. The legit caller is the internal LiteLLM router, which
    dials relay endpoints with `api_key = litellm_internal_key`; authenticate
    exactly that, constant-time, the same shape `proxy.py` uses. Fail-closed:
    an empty/unset `litellm_internal_key` refuses everything (503) rather than
    silently allowing an unauthenticated call.
    """
    settings = get_settings()
    internal_key = settings.litellm_internal_key
    if not internal_key:
        # Fail-closed: no internal key configured → refuse, never allow.
        return _auth_err(503, "relay authentication is unavailable (no internal key configured)")
    try:
        token = extract_bearer(request.headers.get("authorization"))
    except AuthError as exc:
        # missing / malformed Authorization → 401 (proxy.py's shape).
        return _auth_err(exc.status_code, exc.detail)
    if not hmac.compare_digest(token, internal_key):
        # well-formed but not the internal key → 403.
        return _auth_err(403, "invalid relay credential")

    hub = request.app.state.relay_hub
    # Lossless request body (FIX 4): decode utf-8 for the common text path; a
    # non-utf-8 / binary body is preserved via surrogateescape + base64 (the
    # `req` frame carries `b64=True`, the node base64-decodes back to the exact
    # bytes). surrogateescape never raises, so there is no naked-500 decode
    # path here regardless of what bytes the body holds.
    raw = await request.body()
    try:
        body = raw.decode("utf-8")
        body_b64 = False
    except UnicodeDecodeError:
        body = raw.decode("utf-8", errors="surrogateescape")
        body_b64 = True
    headers = _strip_hop_by_hop(dict(request.headers))
    agen = hub.open_request(
        worker_id, request.method, f"/{path}", headers, body, b64=body_b64
    )
    try:
        head: RelayResponse = await agen.__anext__()
    except RelayUnavailable as exc:
        return JSONResponse(status_code=502, content={"error": str(exc)})

    return StreamingResponse(
        agen,
        status_code=head.status,
        headers=_strip_hop_by_hop(head.headers),
        media_type=_header_ci(head.headers, "content-type"),
    )


def register_http_relay(app) -> None:
    """Mount ONLY the HTTP relay route on `app`
    (`GET|POST /relay/{worker_id}/{path:path}`).

    Called by `app.relay.register_relay(app)` via a function-local import —
    see this module's docstring for why the split exists. Not meant to be
    called directly except by that one caller (and tests exercising this
    route in isolation)."""
    app.add_api_route(
        "/relay/{worker_id}/{path:path}",
        _relay_http,
        methods=["GET", "POST"],
    )
