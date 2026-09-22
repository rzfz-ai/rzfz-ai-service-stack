# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Source-IP anchor for the start-portal (#68).

The start-portal is the SSO-gated `start.<domain>` app; Caddy sits in front and
runs Authentik `forward_auth`, forwarding the caller's identity in the
`X-Authentik-*` request headers. Every route that trusts those headers (the
`/password` broker form, the admin reset path, the prefs/categories APIs) does
so on the ASSUMPTION that the request actually passed through Caddy — the only
component that validated the Authentik session.

That assumption was UN-ANCHORED before this fix. start-portal shares the
`_default` docker network with every other stack container; any peer on that
network could open a TCP connection straight to `razzfazz-start-portal:5000`
(bypassing Caddy) and forge `X-Authentik-Username: akadmin` +
`X-Authentik-Groups: authentik Admins`. The header-trusting code would then
treat that peer as an admin and — via the password broker — reset ARBITRARY
users' passwords across Authentik / Dify / Cognee.

This is the same auth-bypass class agent-manager's `_proxy_proof_ok`
(modules/agents/manager/app/blueprints/proxy.py) already closes: anchor on the
SOURCE IP. Every legitimate request arrives from Caddy (the sole ingress); a
peer dialling start-portal directly arrives from its OWN `_default` IP, which is
NOT Caddy's. A peer cannot spoof Caddy's source IP without NET_RAW (dropped on
the hardened containers). We resolve `caddy` via docker DNS at request time and
require `request.remote_addr` to be one of Caddy's IPs.

Design notes (mirrors agent-manager exactly):
  * NO X-Forwarded-For trust. `request.remote_addr` is the real TCP peer, and we
    deliberately do NOT install `ProxyFix` / read XFF — a forged XFF must never
    move the anchor.
  * FAIL-CLOSED: if `caddy` cannot be resolved (docker DNS hiccup), we deny.
    A momentary DNS failure that denied a real user is a self-healing 403 on
    retry; the alternative (fail-open) would re-open the bypass.
  * Cached ~30s so the per-request DNS lookup is cheap.
"""

from __future__ import annotations

import logging
import os
import socket
import time

from flask import request

logger = logging.getLogger(__name__)

# Container name Caddy is reachable at on the `_default` docker network. The
# `caddy` service name is stable across the stack; overridable for tests /
# alternate layouts.
CADDY_HOST = os.environ.get("CADDY_HOST", "caddy")

# Escape hatch for the in-process unit tests + local dev where there is no
# docker DNS to resolve `caddy` and the Flask test client's peer is 127.0.0.1.
# NOT set in any container env — production always enforces the anchor. When
# set to a truthy value, `came_through_caddy()` returns True unconditionally.
# (Same spirit as the agent-manager tests monkeypatching `_proxy_proof_ok` to
# `lambda: True`; here we expose an explicit, greppable env toggle so the
# acceptance/UI tests that DON'T monkeypatch internals can still run.)
_TRUST_ENV = "RZFZ_START_PORTAL_TRUST_ALL_PROXIES"


def _caddy_ips() -> set[str]:
    """Resolve `CADDY_HOST` → set of IPs, cached ~30s.

    Returns an empty set on any resolution failure; the caller treats an empty
    set as "cannot verify" → deny (fail-closed).
    """
    now = time.time()
    cached = getattr(_caddy_ips, "_cache", None)
    if cached and now - cached[0] < 30:
        return cached[1]
    ips: set[str] = set()
    try:
        for res in socket.getaddrinfo(CADDY_HOST, None):
            ips.add(res[4][0])
    except Exception:  # noqa: BLE001 — any DNS error → empty → fail-closed
        pass
    _caddy_ips._cache = (now, ips)  # type: ignore[attr-defined]
    return ips


def came_through_caddy() -> bool:
    """True iff the request's TCP peer is Caddy (the sole ingress).

    Fails CLOSED when `caddy` doesn't resolve. Mirrors agent-manager's
    `_proxy_proof_ok`. Does NOT trust X-Forwarded-For.
    """
    # Explicit test/dev escape hatch (never set in a container).
    if (os.environ.get(_TRUST_ENV) or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    remote = (request.remote_addr or "").strip()
    if not remote:
        return False
    ips = _caddy_ips()
    if not ips:
        logger.error(
            "Cannot resolve %r — refusing start-portal authenticated requests "
            "(fail-closed).", CADDY_HOST,
        )
        return False
    return remote in ips
