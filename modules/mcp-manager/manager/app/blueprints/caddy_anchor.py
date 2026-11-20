# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#639 — Caddy source-IP anchor for header-authenticated surfaces.

Since #619 mcp-manager sits on mcp-network (its wildcard relay is the bridge
to the per-user proxies), so the old "manager not on the proxy network"
defense against X-Authentik header forgery is gone. Every surface that trusts
forward-auth headers must instead verify the TCP peer is Caddy (#397 pattern,
razzfazz_common.proxy_anchor) — a compromised proxy dialing
mcp-manager:5000 with forged identity headers is refused before any header
is read.

NOT anchored: health (liveness probes), internal (called by agent-manager
over `default`, gated by X-MCP-Internal-Token instead), and views marked
@anchor_exempt — today only /api/tls/ask: its real TCP peer is
agent-manager, NOT Caddy (Caddy's global on_demand ask points at
agent-manager, which DELEGATES the mcp-zone to us; PR #645 review, third
kill of that chain after #637). The route is by-design public and leaks
nothing (the instance token is HMAC-derived and unguessable).
"""
from __future__ import annotations

from flask import Response, current_app, request

from razzfazz_common.proxy_anchor import from_trusted_proxy

TRUST_ENV = "RZFZ_MCP_MANAGER_TRUST_ALL_PROXIES"


def anchor_exempt(view):
    """Mark a single view as NOT Caddy-anchored (see module docstring for
    the only legitimate use). The skip resolves via the request's endpoint
    on the registered app — never via path strings (#637 lesson)."""
    view._rzfz_anchor_exempt = True
    return view


def _caddy_anchor():
    view = current_app.view_functions.get(request.endpoint or "")
    if view is not None and getattr(view, "_rzfz_anchor_exempt", False):
        return None
    if not from_trusted_proxy(trust_env=TRUST_ENV):
        return Response("Forbidden: request did not arrive via the trusted "
                        "ingress proxy", status=403, mimetype="text/plain")
    return None


def install_caddy_anchor(*blueprints):
    """Attach the anchor as a before_request hook, idempotently.

    Idempotence matters: blueprints are module singletons, and Flask refuses
    setup calls on an already-registered blueprint — a second create_app()
    in the same process (debug reloader, tests) must not blow up.
    """
    for bp in blueprints:
        if getattr(bp, "_rzfz_caddy_anchor", False):
            continue
        bp.before_request(_caddy_anchor)
        bp._rzfz_caddy_anchor = True
