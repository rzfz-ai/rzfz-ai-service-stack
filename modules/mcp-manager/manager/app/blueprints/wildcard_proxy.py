# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#619 — the *.mcp wildcard bearer-proxy (Option A-light, approved).

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
import threading

from flask import Response, current_app, request, stream_with_context

from razzfazz_common.proxy_anchor import from_trusted_proxy

from app.services import gateway_credentials as gc
from app.services import schema_patterns, thread_budget
from app.services.gateway_runtime import Limiter

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


#: B9 fix round 4: the agents' directory checks have a limiter of their own,
#: apart from the gateway's B7 relay limiter (whose slots the gateway's
#: long-lived streams hold for up to stream_idle_timeout): per instance owner
#: and in total, taken without waiting (like B8's Open WebUI check bound).
#: Both numbers are part of the worker's thread budget (services/thread_budget.py).
CHECKS_PER_OWNER = thread_budget.AGENT_CHECKS_PER_OWNER
CHECKS_TOTAL = thread_budget.AGENT_CHECKS_TOTAL
_CHECKS_GUARD = threading.Lock()


def check_limiter(app) -> Limiter:
    """The app's limiter for the agents' directory checks (one per app)."""
    with _CHECKS_GUARD:
        lim = app.extensions.get("rzfz_wildcard_checks")
        if lim is None:
            lim = app.extensions["rzfz_wildcard_checks"] = Limiter(CHECKS_PER_OWNER, CHECKS_TOTAL)
        return lim


class _OverCheckLimit(Exception):
    def __init__(self, which):
        super().__init__(which)
        self.which = which


class _SlottedDirectory:
    """The read-only directory as the agents' checks see it. The FIRST
    question asked of it takes a slot of the check limiter (no wait; over the
    caps raises _OverCheckLimit) and holds it until release(). A check that
    never asks the directory (the no-token window, a base-tier entry, a
    technical owner's grant) takes no slot at all."""

    def __init__(self, directory, limiter, key):
        self._directory, self._limiter, self._key = directory, limiter, key
        self._lease = None

    def _slot(self):
        if self._lease is None:
            lease, over = self._limiter.acquire(self._key)
            if lease is None:
                raise _OverCheckLimit(over)
            self._lease = lease

    def current_tier(self, uid):
        self._slot()
        return self._directory.current_tier(uid)

    def kb_dataset_state(self, uid, dataset):
        self._slot()
        return self._directory.kb_dataset_state(uid, dataset)

    def release(self):
        if self._lease is not None:
            self._lease.release()
            self._lease = None


def _access_gate(inst, integ, cfg):
    """None to relay, else the refusal. The call-time checks of the gateway
    (gateway_credentials.instance_refusal: governance, the person's tier, the
    company brain's D6 scope or technical grant).

    B9 fix round 4: a request takes a slot ONLY when its check actually asks
    the read-only directory (a token is set, and the owner is a person whose
    entry has a min_tier above the base tier, or whose instance is a company
    brain), and that slot comes from the agents' own check limiter
    (check_limiter: CHECKS_PER_OWNER per instance owner, CHECKS_TOTAL in all,
    no wait), never from the gateway's B7 relay limiter: the gateway's
    streams cannot refuse agents, and a hanging Authentik can park at most
    CHECKS_TOTAL requests, never the worker. The slot is released before the
    dial. 403 refused, 503 undecidable or over the total check cap, 429 over
    the owner's check cap; each audited under the owner, never a credential."""
    owner = inst.get("owner_sub") or ""
    key = owner or f"slug:{inst.get('user_slug') or '?'}"
    directory = getattr(current_app, "authentik_directory", None)
    slotted = (None if directory is None
               else _SlottedDirectory(directory, check_limiter(current_app), key))
    reason = ""
    try:
        integ = dict(integ, id=integ.get("id") or inst["mcp_id"])
        reason = gc.instance_refusal(current_app.db, slotted, integ, owner, cfg)
        status, outcome = (403, "refused") if reason else (None, None)
    except _OverCheckLimit as e:
        status, outcome = ((429, "over_owner_check_limit") if e.which == "per_caller"
                           else (503, "over_check_limit"))
    except gc.Unavailable:
        status, outcome = 503, "check_unavailable"
    except Exception:  # noqa: BLE001 — a failed check is a refusal, never a pass
        logger.exception("wildcard proxy: access check failed")
        status, outcome = 503, "check_unavailable"
    finally:
        if slotted is not None:
            slotted.release()
    if status is None:
        return None
    try:
        current_app.db.log_audit(owner or inst.get("user_slug") or "?", "wildcard_refused",
                                 inst["mcp_id"], {"outcome": outcome})
    except Exception:  # noqa: BLE001 — the refusal stands without its row
        logger.warning("wildcard proxy: refusal audit failed", exc_info=True)
    text = {403: "Forbidden", 429: "Too many requests"}.get(status, "Access check unavailable")
    if status == 403 and reason:
        # #3477: the reason is ours (governance, tier, company-brain scope).
        text = f"Forbidden: {reason}"
    return Response(text, status=status)


def _is_tools_list(content: bytes) -> bool:
    """Whether a request body is a JSON-RPC tools/list (alone or in a batch)."""
    try:
        msg = json.loads(content or b"")
    except (ValueError, RecursionError):
        return False
    msgs = msg if isinstance(msg, list) else [msg]
    return any(isinstance(m, dict) and m.get("method") == "tools/list" for m in msgs)


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
        # #2472: gw.<MCP_DOMAIN> is the agents' way to the gateway. Only /gw/*
        # is served there; the manager's own routes stay unreachable on any
        # non-exact host (CRITICAL #1).
        if prefix == "gw":
            return None if request.path.startswith("/gw/") else Response("Not found", status=404)
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

        # B9 fix rounds 2-4: the agents' path follows the SAME call-time
        # rules as the gateway (governance, the person's tier, the company
        # brain's D6 scope or technical grant; cognee's ACL is not relied
        # on); a check that asks the directory holds a slot of the agents'
        # own check limiter. In B6's no-token window the directory checks
        # behave as 2026.09-ga.4 did.
        refused = _access_gate(inst, integ, cfg)
        if refused is not None:
            return refused

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

        content = request.get_data()
        try:
            # #629 review: read=None — long-lived idle SSE sessions (MCP
            # streamable-http keeps quiet streams open well beyond 5 min)
            # must not be cut by the relay; the old Caddy route never did.
            # connect stays bounded; write/pool inherit the default.
            client = httpx.Client(
                timeout=httpx.Timeout(None, connect=10, write=60, pool=60))
            upstream = client.stream(
                request.method, url, headers=up_headers,
                content=content)
            resp = upstream.__enter__()
        except Exception:
            logger.exception("wildcard proxy: upstream dial failed for %s", host)
            return Response("Bad gateway", status=502)

        body = resp.iter_bytes()
        if (request.method == "POST" and 200 <= int(resp.status_code) < 300
                and _is_tools_list(content)):
            # #3560: the same tool-list normalisation as the gateway relay
            # (app/services/schema_patterns.py): llama.cpp refuses unanchored
            # `pattern`s under native tool calling. Calls pass unchanged.
            body = schema_patterns.strip_tools_list_stream(body, resp.headers.get("content-type", ""))

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
                for chunk in body:
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
