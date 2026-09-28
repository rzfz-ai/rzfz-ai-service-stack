# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#619 — the *.mcp wildcard bearer-proxy (Option A-light, operator-approved).

Replaces the dynamic per-instance Caddy admin-API routes (the last user of
the pattern #606/#610 retired in agent-manager). The static ``*.{MCP_DOMAIN}``
Caddyfile block now reverse-proxies every instance host to THIS handler,
which re-creates the exact security properties the dynamic routes carried:

* **Bearer gate FIRST** (CRITICAL-1, #61): the per-instance ``_route_secret``
  is compared constant-time before any other work — a request without the
  exact bearer never reaches the per-user proxy container (which holds the
  user's live credentials). 401 on mismatch, like the Caddy-layer gate did.
* **Host rewrite** for DNS-rebinding-guarded MCP servers (cognee-mcp): the
  upstream ``Host`` becomes ``localhost:<port>`` when the catalog says so.
* **Manager never exposed on the wildcard** (commit-review CRITICAL #1):
  this runs as a ``before_request`` hook that FULLY handles any request whose
  Host is an instance subdomain — the manager's UI/API routes are unreachable
  on those hosts, and unknown subdomains get the same 404 the static
  catch-all used to serve. The exact-host {MCP_DOMAIN} block (SSO) is
  untouched.
* **Caddy anchor**: the TCP peer must be Caddy (razzfazz_common.proxy_anchor)
  — an in-network peer dialing mcp-manager:5000 with a forged Host cannot
  reach a proxy container through here.

SSE note (#619 precondition check): MCP streamable-http is SSE over plain
HTTP — the httpx streaming relay below carries it; no WebSocket upgrade is
part of the MCP proxy protocol.
"""
from __future__ import annotations

import hmac
import json
import logging
import re

from flask import Response, current_app, request, stream_with_context

from razzfazz_common.proxy_anchor import from_trusted_proxy

logger = logging.getLogger(__name__)

# Hop-by-hop + headers we must not blindly relay.
_SKIP_REQ_HEADERS = {"host", "connection", "keep-alive", "transfer-encoding",
                     "te", "upgrade", "proxy-authorization", "proxy-connection"}
_SKIP_RESP_HEADERS = {"connection", "keep-alive", "transfer-encoding",
                      "content-encoding", "content-length"}

_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)-([0-9a-f]{8})$")


def _resolve_instance(mcp_prefix: str):
    """(instance, integ) for a `{mcp_id}-{token}` host prefix, or (None, None).

    Token-anchored like agent-manager's resolver: iterate the instances of the
    parsed mcp_id and match the HMAC token — the mapping stays recoverable
    only manager-side (the subdomain never leaks user identity)."""
    from app.services.caddy_client import instance_token
    m = _HOST_RE.match(mcp_prefix)
    if not m:
        return None, None
    for inst in current_app.db.get_all_instances():
        if not inst.get("id"):
            continue
        tok = instance_token(inst["id"])
        candidate = f"{inst['mcp_id']}-{tok}"
        if hmac.compare_digest(candidate, mcp_prefix):
            integ = current_app.catalog.get(inst["mcp_id"])
            return inst, integ
    return None, None


def init_wildcard_proxy(app):
    mcp_domain = (app.config.get("MCP_DOMAIN") or "").lower()

    @app.before_request
    def _wildcard_proxy():  # noqa: C901 — one linear gate sequence, kept together
        host = (request.host or "").split(":")[0].lower()
        # Not an instance subdomain -> fall through to the normal app
        # (exact-host UI/API, healthz, internal endpoints).
        if not mcp_domain or host == mcp_domain or not host.endswith("." + mcp_domain):
            return None

        # From here on the request is FULLY handled — instance hosts can
        # never reach the manager's own routes (CRITICAL #1).
        if not from_trusted_proxy(trust_env='RZFZ_MCP_MANAGER_TRUST_ALL_PROXIES'):
            logger.warning("wildcard proxy: peer is not Caddy (%s) — refusing",
                           request.remote_addr)
            return Response("Forbidden", status=403)

        prefix = host[: -(len(mcp_domain) + 1)]
        inst, integ = _resolve_instance(prefix)
        if not inst or not integ:
            # same contract as the retired static catch-all: unknown host = 404
            return Response("Not found", status=404)

        # CRITICAL-1 (#61): bearer gate BEFORE anything else touches the
        # per-user proxy. Constant-time; missing and wrong are the same 401.
        cfg = inst.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                cfg = {}
        secret = cfg.get("_route_secret") or ""
        presented = request.headers.get("Authorization") or ""
        # Compare on BYTES. Werkzeug decodes headers as latin-1, so an
        # `Authorization: Bearer <0xE9>` yields a str with non-ASCII characters
        # and `hmac.compare_digest` raises
        # "TypeError: comparing strings with non-ASCII characters is not
        # supported". That exception escapes this before_request hook, so an
        # unauthenticated caller on the wildcard host — a route that has no
        # forward_auth by design — got a 500 with a logged traceback instead of
        # a 401. Encoding both sides keeps the comparison constant-time and
        # makes every rejection identical.
        expected = f"Bearer {secret}".encode("latin-1", "ignore")
        if not secret or not hmac.compare_digest(
                presented.encode("latin-1", "ignore"), expected):
            return Response("Unauthorized", status=401)

        if inst.get("state") != "running":
            return Response("Proxy not running", status=503)

        import httpx
        port = int(integ["container_port"])
        upstream_host = (f"localhost:{port}"
                         if integ.get("rewrite_upstream_host")
                         else f"{inst['container_name']}:{port}")
        url = f"http://{inst['container_name']}:{port}{request.path}"
        if request.query_string:
            url += "?" + request.query_string.decode()

        up_headers = {k: v for k, v in request.headers
                      if k.lower() not in _SKIP_REQ_HEADERS}
        up_headers["Host"] = upstream_host

        try:
            # #629 review: read=None — long-lived idle SSE sessions (MCP
            # streamable-http keeps quiet streams open well beyond 5 min)
            # must not be cut by the relay; the old Caddy route never did.
            # connect stays bounded; write/pool inherit the default.
            client = httpx.Client(
                timeout=httpx.Timeout(None, connect=10, write=60, pool=60))
            upstream = client.stream(
                request.method, url, headers=up_headers,
                content=request.get_data())
            resp = upstream.__enter__()
        except Exception:
            logger.exception("wildcard proxy: upstream dial failed for %s", host)
            return Response("Bad gateway", status=502)

        def _relay():
            try:
                # iter_bytes(), NOT iter_raw(): _SKIP_RESP_HEADERS drops
                # `content-encoding`, and `accept-encoding` is NOT in
                # _SKIP_REQ_HEADERS, so the client's value is forwarded
                # upstream. Any MCP server that honours it returned compressed
                # bytes that iter_raw() relayed verbatim while the client was
                # told the body was identity-encoded — a garbled body with no
                # error. iter_bytes() decodes, which is what the stripped
                # header then correctly describes.
                for chunk in resp.iter_bytes():
                    yield chunk
            finally:
                try:
                    upstream.__exit__(None, None, None)
                finally:
                    client.close()

        # #1909: `multi_items()`, not `items()`. httpx comma-joins whatever
        # appeared more than once, and that rule does NOT hold for `Set-Cookie`
        # (RFC 6265: one header per cookie) — a browser handed
        # `a=1; Path=/, b=2; Path=/` reads ONE cookie and drops the other,
        # silently. Measured in the agent-manager proxy, which had the same
        # line; an MCP server that sets a session and a CSRF cookie together
        # lost one of them here too.
        headers = [(k, v) for k, v in resp.headers.multi_items()
                   if k.lower() not in _SKIP_RESP_HEADERS]
        return Response(stream_with_context(_relay()),
                        status=resp.status_code, headers=headers)
