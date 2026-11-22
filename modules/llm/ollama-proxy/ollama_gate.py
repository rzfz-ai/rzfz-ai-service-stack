# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Bearer gate in front of the Ollama-protocol proxy (#3301).

The upstream proxy (`eyalrot2/ollama-openai-proxy`) translates Ollama calls to
the LLM Manager's OpenAI API using ITS OWN manager key, and has no inbound
authentication. Without this gate every container on the stack network got
model access under that key, past per-user keys and metering.

This module is mounted read-only into the proxy container as
`/app/ollama_gate.py` and served instead of the upstream app:

    python -m uvicorn ollama_gate:app --host 0.0.0.0 --port 11434

A request passes only with `Authorization: Bearer <OLLAMA_PROXY_CLIENT_KEY>`.
Every path is gated, the root probe included. An empty or unset key refuses
every caller (fail closed): an unconfigured gate must never be an open one.

Stdlib only — the upstream image ships no extra packages for us.
"""
from __future__ import annotations

import hmac
import importlib
import json
import logging
import os

log = logging.getLogger("ollama_gate")

_KEY_ENV = "OLLAMA_PROXY_CLIENT_KEY"
_UPSTREAM = "src.main"   # the upstream image's ASGI module (its CMD serves src.main:app)


def _refuse(reason: bytes):
    body = json.dumps({"error": "unauthorized: send Authorization: Bearer <key>"}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"www-authenticate", b'Bearer realm="ollama-proxy", error="' + reason + b'"'),
    ]

    async def send_401(send):
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return send_401


def _presented_key(scope) -> bytes | None:
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization":
            scheme, _, token = value.partition(b" ")
            if scheme.lower() != b"bearer":
                return None
            token = token.strip()
            return token or None
    return None


def bearer_gate(inner, key):
    """Wrap the ASGI app ``inner``: only a request bearing ``key`` reaches it."""
    expected = (key or "").strip().encode()
    if not expected:
        log.error("%s is empty: the Ollama proxy refuses every request until it is set", _KEY_ENV)

    async def app(scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            return await inner(scope, receive, send)   # lifespan: start the proxy normally
        got = _presented_key(scope)
        if expected and got is not None and hmac.compare_digest(got, expected):
            return await inner(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await _refuse(b"invalid_token" if got else b"missing_token")(send)

    return app


def __getattr__(name):
    # `ollama_gate:app` is resolved by uvicorn when it serves; importing this
    # module (e.g. in a test) does not import the upstream proxy.
    if name == "app":
        inner = importlib.import_module(_UPSTREAM).app
        return bearer_gate(inner, os.environ.get(_KEY_ENV, ""))
    raise AttributeError(name)
