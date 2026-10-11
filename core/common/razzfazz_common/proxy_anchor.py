# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Shared source-IP anchor for services that trust Authentik forward-auth headers.

Caddy is the sole ingress and runs Authentik `forward_auth`, injecting the
caller's identity as `X-Authentik-*` request headers. Every route that trusts
those headers does so on the ASSUMPTION that the request actually passed through
Caddy — the only component that validated the Authentik session.

Headers alone do not carry that assumption. Every stack service shares the
`_default` docker network, so a peer can open a TCP connection straight to a
service's container port and forge `X-Authentik-Username` +
`X-Authentik-Groups`. The header-trusting code then treats that peer as whoever
it claims to be. Anchoring on the SOURCE IP closes it: a legitimate request
arrives from Caddy; a peer dialling directly arrives from its own `_default` IP,
and cannot spoof Caddy's without NET_RAW (dropped on the hardened containers).

**Why this module exists.** The same anchor had been written three times —
`modules/agents/manager/app/blueprints/proxy.py::_proxy_proof_ok` (C1 / PR #84),
`core/start-portal/proxy_anchor.py` (#68), and
`modules/llm/manager/app/authz.py::from_caddy` — while
`razzfazz_common.auth` offered only the header parsing. Predictably, two
consumers ended up with none: the Configuration Portal (#377) and
agent-manager's `/api/*` blueprint (#390). Putting it here once is the
structural fix; the three existing copies can collapse onto it, and
`require_authentik_auth` can adopt it, without changing their behaviour.

Design (kept identical to the three implementations it generalises):
  * **NO X-Forwarded-For trust.** `request.remote_addr` is the real TCP peer, and
    we deliberately do not install `ProxyFix` or read XFF — a forged header must
    never be able to move the anchor.
  * **FAIL-CLOSED.** If no trusted hostname resolves (docker DNS hiccup), deny.
    A momentary DNS failure is a self-healing 403 on retry; fail-open would
    re-open the bypass.
  * **Cached ~30 s** per hostname set, so the per-request lookup is cheap.
  * **Explicit env escape hatch** per service, for in-process tests and local dev
    where there is no docker DNS and the test client's peer is 127.0.0.1. Never
    set in a container.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Iterable

from flask import request

logger = logging.getLogger(__name__)

#: The stack's sole ingress. Overridable for tests / alternate layouts.
DEFAULT_INGRESS_HOST = os.environ.get("CADDY_HOST", "caddy")

_CACHE_TTL_SECONDS = 30.0
_cache: dict[tuple[str, ...], tuple[float, frozenset[str]]] = {}

_TRUTHY = ("1", "true", "yes", "on")


def resolve_peer_ips(hosts: Iterable[str]) -> frozenset[str]:
    """Resolve ``hosts`` → the set of their IPs, cached ~30 s.

    Returns an EMPTY set when nothing resolves, which callers must treat as
    "cannot verify" → deny. A partial resolution (one of two hosts up) returns
    what it found: a service whose optional upstream is absent should still
    accept its primary ingress.
    """
    key = tuple(hosts)
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    ips: set[str] = set()
    for host in key:
        if not host:
            continue
        try:
            for res in socket.getaddrinfo(host, None):
                ips.add(res[4][0])
        except Exception:  # noqa: BLE001 — any DNS error contributes nothing
            continue
    frozen = frozenset(ips)
    _cache[key] = (now, frozen)
    return frozen


def from_trusted_proxy(hosts: Iterable[str] | None = None, *,
                       trust_env: str | None = None) -> bool:
    """True iff the request's TCP peer is one of ``hosts`` (default: Caddy).

    Fails closed when none of ``hosts`` resolves. Does NOT consult
    X-Forwarded-For. ``trust_env`` names an environment variable that, when
    truthy, bypasses the check — for tests and local dev only.
    """
    if trust_env and (os.environ.get(trust_env) or "").strip().lower() in _TRUTHY:
        return True
    remote = (request.remote_addr or "").strip()
    if not remote:
        return False
    candidates = tuple(hosts) if hosts is not None else (DEFAULT_INGRESS_HOST,)
    ips = resolve_peer_ips(candidates)
    if not ips:
        logger.error(
            "Cannot resolve any of %r — refusing header-authenticated requests "
            "(fail-closed).", candidates,
        )
        return False
    return remote in ips


def reset_cache() -> None:
    """Drop the resolution cache (tests; also useful after a topology change)."""
    _cache.clear()
