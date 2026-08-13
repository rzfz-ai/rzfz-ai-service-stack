# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Reverse proxy for per-user agent instances.

Routes /i/{type}/* to the user's agent container.
Caddy forwards these requests after Authentik SSO — the X-Authentik-Username
header identifies the user and maps to their container.
"""

import hmac
import json
import logging
import os
import re
import threading

import httpx
from flask import Blueprint, Response, current_app, request, stream_with_context

from app.services.provisioner import _is_sandboxed, slug_candidates

logger = logging.getLogger(__name__)

proxy_bp = Blueprint('proxy', __name__)

# ── Forwarded-port PREVIEW (#36 / PR #84 — FEATURE 1) ────────────────────────
# Codespaces-style: a dev server the user starts INSIDE their sandbox (e.g.
# `npm run dev` on :3000) is reachable from the browser through the SAME
# authenticated per-instance path the terminal uses, via a `/__port/<port>/…`
# path prefix on the per-instance subdomain. Caddy proxies the whole subdomain
# 1:1 to agent-manager (forward_auth in front), so a `/__port/3000/foo` request
# arrives here ALREADY authenticated + about to be ownership-checked; we parse
# the target port, strip the prefix, and reverse-proxy to
# http://<agent-container>:<port>/foo. No new Caddy route, no new Authentik
# provider, no relaxed auth — the owner-only + source-IP gates are unchanged.
#
# The port is validated to a real TCP port range; anything else is treated as a
# normal path (falls through to the container's primary UI port), so a repo file
# literally named `__port` can't be mistaken for a preview.
_PORT_PREFIX_RE = re.compile(r'^__port/(\d{1,5})(/.*)?$')


def _parse_port_preview(path: str):
    """If `path` is a `/__port/<port>/rest` preview path, return (port, rest);
    else (None, None). `path` is the proxy's captured path WITHOUT a leading
    slash (Flask's `<path:path>`). rest keeps its leading slash (or '' → '/')."""
    m = _PORT_PREFIX_RE.match(path or '')
    if not m:
        return None, None
    try:
        port = int(m.group(1))
    except (TypeError, ValueError):
        return None, None
    if not (1 <= port <= 65535):
        return None, None
    rest = m.group(2) or '/'
    return port, rest

# C1 bypass fix (PR #84 re-review): "came through Caddy" — SOURCE-IP anchor.
# The sandbox coding-agent containers share the `coding-agents` docker network
# with agent-manager, so a malicious user who controls their own sandbox could
# otherwise reach agent-manager:5000 DIRECTLY (bypassing Caddy — the only
# component that validates the Authentik session) and forge Host +
# X-Authentik-Username to hijack a victim's shell.
#
# A prior version required a Caddy-injected proof HEADER, but Caddy's
# `forward_auth` on the per-instance routes does not reliably propagate a
# route-injected request header to the upstream (the X-Authentik-* copy_headers
# arrive, but an extra header set in/after forward_auth does not), so that gate
# 403'd the real owner. Instead we anchor on the SOURCE IP: every legitimate
# request arrives from Caddy (the sole ingress); a sandbox dialing
# agent-manager:5000 directly arrives from its own `coding-agents` IP, which is
# NOT Caddy's. A sandbox cannot spoof Caddy's source IP (cap_drop ALL → no
# NET_RAW). We resolve `caddy` via docker DNS at request time and require
# request.remote_addr to match.
_PROXY_PROOF_HEADER = 'X-Razzfazz-Proxy-Proof'  # stripped from upstream (never forwarded)
_CADDY_HOST = os.environ.get('CADDY_HOST', 'caddy')
import socket as _socket  # noqa: E402


def _caddy_ips():
    """Resolve `caddy` → set of IPs, cached ~30s. Fail-open to empty (caller
    then denies) rather than crash if DNS momentarily hiccups."""
    import time as _t
    now = _t.time()
    cached = getattr(_caddy_ips, "_cache", None)
    if cached and now - cached[0] < 30:
        return cached[1]
    ips = set()
    try:
        for res in _socket.getaddrinfo(_CADDY_HOST, None):
            ips.add(res[4][0])
    except Exception:  # noqa: BLE001
        pass
    _caddy_ips._cache = (now, ips)
    return ips


def _proxy_proof_ok() -> bool:
    """True iff the request's peer address is Caddy (the sole ingress).

    Fails CLOSED when `caddy` doesn't resolve. This is the manager-side twin of
    the container's source-IP anchor (coding-agent-web/app.py `_from_manager`).
    """
    remote = (request.remote_addr or '').strip()
    if not remote:
        return False
    ips = _caddy_ips()
    if not ips:
        logger.error("Cannot resolve %r — refusing agent proxy requests "
                     "(fail-closed).", _CADDY_HOST)
        return False
    return remote in ips


def _ui_container_name(instance, type_info) -> str:
    """The container serving the user-facing UI for this instance.

    Companion-UI agents (hermes) expose their UI on the `-<suffix>` companion
    (default 'workspace'); everything else uses the primary. Mirrors
    caddy_client.register_route's route_target so the manager proxy dials the
    same container the Caddy route was built for.
    """
    if type_info and type_info.get('companion_image'):
        suffix = type_info.get('companion_suffix') or 'workspace'
        return f"{instance['container_name']}-{suffix}"
    return instance['container_name']


def _header_safe(value: str) -> str:
    """Return a header value safe to put on the wire (PR #84 Unicode fix).

    Forwarded Authentik headers can carry non-ASCII — an `X-Authentik-Name`
    display name or an `X-Authentik-Groups` group with an umlaut (e.g.
    "Grüße Müller", "Geschäftsführung"). httpx 0.28 normalizes str header
    values with ``.encode("ascii")`` and raises UnicodeEncodeError → the proxy
    502s and the agent never opens. HTTP/1.1 header values are latin-1 (RFC
    7230 §3.2.4), so we transmit the UTF-8 bytes reinterpreted as latin-1: the
    result is a latin-1-encodable str, and re-encoding it as latin-1 yields the
    original UTF-8 bytes — a lossless round-trip the upstream can UTF-8-decode.
    Pure-ASCII values (the common case, incl. the X-Authentik-Username slug the
    container's owner-check reads) pass through unchanged.
    """
    if value is None:
        return value
    try:
        value.encode("ascii")
        return value  # already ASCII — no-op
    except UnicodeEncodeError:
        return value.encode("utf-8").decode("latin-1")


def init_proxy_ws(app):
    """Register the per-instance WebSocket proxy route (item A, PR #84).

    httpx cannot upgrade a WebSocket, so ANY agent WS needs a real bridge:
    browser ⇄ agent-manager ⇄ container. The coding-agent terminal uses
    `/terminal`, but other agents use different WS paths (moltis serves its
    LLM-list / chat over `/ws/chat`; hermes has its own). #36 fix: register a
    GENERIC `<path:path>` route so the bridge proxies WHATEVER path the browser
    opens the WS on — the handler dials the upstream at the request's actual
    path. We reuse the SAME flask-sock instance the terminal blueprint owns (a
    second `Sock()` would collide on the internal '__flask_sock' blueprint name).

    `/terminal` is also registered explicitly: flask-sock/Werkzeug's
    `<path:path>` converter does NOT match an empty or single-segment root the
    same way, and the terminal is the most-used WS — an exact route keeps its
    dispatch unambiguous. Both land on the same `ws_terminal` handler, which
    reads `request.path` to pick the upstream path, so behaviour is identical.
    """
    from app.blueprints.terminal import sock as _sock
    # Distinct Flask endpoints — both point at ws_terminal, but Flask derives
    # the endpoint name from the view's __name__, so registering the same
    # function twice collides ("overwriting an existing endpoint"). Give the
    # generic route an explicit, distinct endpoint.
    _sock.route('/terminal', endpoint='ws_terminal')(ws_terminal)
    _sock.route('/<path:ws_path>', endpoint='ws_terminal_generic')(ws_terminal)


def _resolve_instance_for_host(host):
    """Resolve a per-instance subdomain host → (agent_type, instance) or
    (agent_type|None, None). Shared by the HTTP proxy and the WS proxy.

    Does NOT enforce ownership — the caller must (see _owns()).
    """
    from app.services.caddy_client import instance_token

    agents_domain = current_app.config.get('AGENTS_DOMAIN', '')
    if not agents_domain or not host.endswith(f'.{agents_domain}'):
        return None, None

    instance_prefix = host.replace(f'.{agents_domain}', '')
    parts = instance_prefix.rsplit('-', 1)
    if len(parts) != 2:
        for i in range(len(instance_prefix) - 1, 0, -1):
            if instance_prefix[i] == '-':
                agent_type = instance_prefix[:i]
                suffix = instance_prefix[i + 1:]
                break
        else:
            return None, None
    else:
        agent_type, suffix = parts

    instance = None
    for cand in current_app.db.list_active_instances_by_type(agent_type):
        if instance_token(cand['id']) == suffix:
            instance = cand
            break
    if not instance:
        instance = current_app.db.get_instance_by_type_and_user(agent_type, suffix)
    return agent_type, instance


def _owns(instance, forwarded_user):
    """True iff the forwarded Authentik user owns the instance (C1 layer-2).

    #192: accepts a match against EITHER the current make_user_slug OR the
    legacy pre-hash-suffix slug (razzfazz_common.user_slug.slug_candidates) —
    instances provisioned before #36/PR#61 introduced the hash suffix are
    still stored under the legacy slug, and a same-user session now resolving
    to the hashed slug would otherwise be denied ownership of their own
    pre-existing instance. Candidates are derived ONLY from `forwarded_user`
    (the caller's own identity), never from `instance`, so a DIFFERENT user's
    legacy slug can never satisfy this match — cross-user isolation holds.
    """
    if not instance or not forwarded_user:
        return False
    return instance['user_slug'] in slug_candidates(forwarded_user)


@proxy_bp.route('/', defaults={'path': ''})
@proxy_bp.route('/<path:path>')
def proxy_to_agent(path):
    """Proxy request to the user's agent container based on subdomain.

    Subdomain format (current): {type}-{token}.agents.<domain>
    Subdomain format (legacy):  {type}-{user_slug}.agents.<domain>

    The token is HMAC-derived from the instance UUID — see
    app.services.caddy_client.instance_token. We try the new lookup
    first, then fall back to the legacy user_slug lookup so instances
    registered before the upgrade keep working until they're recreated.
    """
    from app.services.caddy_client import instance_token

    host = request.host.split(':')[0]  # strip port
    agents_domain = current_app.config.get('AGENTS_DOMAIN', '')

    # Check if this is a per-instance subdomain request
    if not agents_domain or not host.endswith(f'.{agents_domain}'):
        return Response('Not an agent subdomain', status=404)

    # C1 bypass fix: require the Caddy↔manager proof. A sandbox dialling
    # agent-manager:5000 directly (same coding-agents net) with a forged Host +
    # X-Authentik-Username cannot produce this header → 403. Enforced BEFORE any
    # ownership logic so a forged identity never gets that far.
    if not _proxy_proof_ok():
        logger.warning("Proxy-proof missing/invalid on %r %s from %s — refusing",
                       host, request.path, request.remote_addr)
        return Response('Forbidden', status=403)

    # Extract {type}-{suffix} from subdomain
    instance_prefix = host.replace(f'.{agents_domain}', '')
    parts = instance_prefix.rsplit('-', 1)
    if len(parts) != 2:
        # Multi-hyphen agent type like "coding-tools-{suffix}"
        for i in range(len(instance_prefix) - 1, 0, -1):
            if instance_prefix[i] == '-':
                agent_type = instance_prefix[:i]
                suffix = instance_prefix[i+1:]
                break
        else:
            return Response('Invalid agent subdomain', status=404)
    else:
        agent_type, suffix = parts

    # Token-based lookup (current scheme): iterate active instances of
    # this type and match the derived token. O(N) but N is small (per-type
    # instances) and proxied requests are cached in Caddy after route
    # registration anyway — this only fires for the cold/lazy-start path.
    instance = None
    for cand in current_app.db.list_active_instances_by_type(agent_type):
        if instance_token(cand['id']) == suffix:
            instance = cand
            break

    # Fall back to legacy user_slug lookup so we don't 404 on instances
    # registered before this upgrade (their subdomain still has the
    # username in it until they're stopped + relaunched).
    if not instance:
        instance = current_app.db.get_instance_by_type_and_user(agent_type, suffix)
        if instance:
            user_slug = suffix  # noqa: F841 — keeps the local readable

    # C1 layer-2 (PR #84 review): enforce OWNERSHIP. This Flask proxy path is
    # reached for the lazy-start / cold path (the hot path is Caddy's dynamic
    # admin-API route straight to the container). Caddy's forward_auth already
    # authenticated the caller and forwarded X-Authentik-Username; require the
    # caller's slug to equal the instance owner's. A DIFFERENT logged-in user
    # hitting someone else's subdomain → 403 (not their shell). Mirrors the
    # check in api.py / terminal.py. Applied once the instance is resolved
    # (below, after the not-provisioned bounce) so the "launch one" page still
    # works for callers with no instance of their own.
    if instance:
        forwarded_user = request.headers.get('X-Authentik-Username', '')
        # #192: accept the current OR legacy (pre-hash-suffix) slug — see
        # _owns()/slug_candidates() above for why. Candidates come from
        # `forwarded_user` only, so a different user is still denied.
        if not forwarded_user or not _owns(instance, forwarded_user):
            logger.warning(
                "Ownership denied: user=%r slug_candidates=%r != instance owner=%r on host=%r",
                forwarded_user, slug_candidates(forwarded_user) if forwarded_user else (),
                instance['user_slug'], host,
            )
            return Response('Forbidden', status=403)

    if not instance:
        # Truly not provisioned yet — bounce to the dashboard so the user
        # can launch one. /dashboard lives on the agents-MANAGER subdomain
        # (NOT this per-instance subdomain), so use an absolute URL.
        main_domain = current_app.config.get('MAIN_DOMAIN', '')
        dashboard_url = f"https://agents.{main_domain}/dashboard?launch={agent_type}" if main_domain else "/"
        return Response(
            f'<h1>Agent not provisioned</h1>'
            f'<p>{agent_type} hasn\'t been launched yet. '
            f'<a href="{dashboard_url}">Open the AI Agents dashboard</a> to start it.</p>',
            status=503, content_type='text/html'
        )

    if instance['state'] != 'running':
        # rc6.7 #59: lazy-start on access. The pre-fix behaviour was to
        # send the user to "/" of THIS subdomain, which the proxy then
        # returned 503 for again — an infinite "Agent not running, go to
        # dashboard" loop because the dashboard link pointed back at the
        # broken subdomain. Now: if the instance exists but is stopped
        # (idle-shutdown timer fired), auto-start it and serve a small
        # 503 page that auto-refreshes after 5s. Subsequent requests
        # find it `running` and proxy normally.
        try:
            current_app.provisioner.start(str(instance['id']), 'lazy-access')
            logger.info(f"Lazy-started {instance['container_name']} on access from {host}")
        except Exception as e:
            logger.warning(f"Lazy-start failed for {instance['container_name']}: {e}")
        return Response(
            '<!doctype html><html><head>'
            '<meta http-equiv="refresh" content="5">'
            '<title>Starting agent…</title>'
            '<style>body{font-family:sans-serif;margin:3rem;}</style>'
            '</head><body>'
            f'<h1>Starting {agent_type}…</h1>'
            '<p>This page will refresh in 5 seconds. '
            'If the agent does not load after 30 seconds, '
            f'<a href="https://agents.{current_app.config.get("MAIN_DOMAIN","")}/dashboard">'
            'check the dashboard</a>.</p>'
            '</body></html>',
            status=503, content_type='text/html',
            headers={'Retry-After': '5'},
        )

    # Update last accessed for idle tracking
    current_app.db.update_last_accessed(instance['id'])

    # Resolve container port + target. For companion-UI agents (e.g. hermes,
    # whose user-facing UI is the `-workspace` companion) the proxy MUST target
    # the companion — mirroring caddy_client.register_route's route_target. The
    # primary container (hermes-agent) serves the gateway, not the UI, so
    # dialling it 502s. (PR #84 re-review sweep — real hermes owner-open 502.)
    type_info = current_app.catalog.get_type(agent_type)
    ports = json.loads(type_info['ports']) if isinstance(type_info['ports'], str) else type_info['ports']
    container_port = ports['internal']
    container_name = _ui_container_name(instance, type_info)

    # FEATURE 1 — forwarded-port preview: `/__port/<port>/rest` reverse-proxies
    # to the dev server the user started INSIDE their sandbox on <port>, at
    # `rest`. Only for sandboxed coding agents (the ones that run user dev
    # servers); on other agent types the prefix is treated as a normal path.
    preview_port, preview_rest = _parse_port_preview(path)
    if preview_port is not None and _is_sandboxed(agent_type, type_info):
        container_port = preview_port
        # The container name for a preview is ALWAYS the sandbox's own container
        # (not a companion) — the user's dev server runs in the primary sandbox.
        container_name = instance['container_name']
        path = preview_rest.lstrip('/')
    upstream = f"http://{container_name}:{container_port}"

    # Build target URL
    target_url = f"{upstream}/{path}"
    if request.query_string:
        target_url += f"?{request.query_string.decode()}"

    # Forward the request
    try:
        # Filter hop-by-hop headers. Also STRIP the Caddy↔manager proof header
        # (C1 bypass fix): it's a secret shared only between Caddy and the
        # manager and MUST NOT leak into a sandbox container (which the user
        # controls) — the container trusts source-IP, not this header.
        # PR #84 Unicode fix: header values are made header-safe and passed to
        # httpx as latin-1 BYTES so a non-ASCII display name / group
        # (X-Authentik-Name/-Groups) doesn't crash httpx's ascii normalization.
        # PR #84 fix (browser gate — "garbage/mojibake body"): STRIP the
        # client's `accept-encoding`. The browser offers `br`/`zstd`; some
        # upstreams (moltis serves `/login` as zstd, others use brotli)
        # honour it, but this manager's httpx has NO brotli/zstd decoder
        # installed, so `resp.content` would stay COMPRESSED. We then strip
        # `content-encoding` below and hand the still-compressed bytes to the
        # browser with no encoding header → it renders raw compressed bytes as
        # a page of garbage. By dropping accept-encoding, httpx falls back to
        # its own default (`gzip, deflate`) which it CAN decode, so
        # `resp.content` is always real, decompressed bytes that match the
        # identity response we emit. (A plain httpx health-check masked this:
        # httpx's default AE never offers br/zstd, so the upstream never chose
        # an encoding the manager couldn't decode.)
        fwd_headers = {
            k: _header_safe(v).encode("latin-1")
            for k, v in request.headers
            if k.lower() not in ('host', 'transfer-encoding', 'connection',
                                 'accept-encoding',
                                 _PROXY_PROOF_HEADER.lower())
        }

        resp = httpx.request(
            method=request.method,
            url=target_url,
            headers=fwd_headers,
            content=request.get_data(),
            timeout=30,
            follow_redirects=False,
        )

        # Build response, excluding hop-by-hop headers. `content-encoding` and
        # `content-length` are dropped because `resp.content` is the DECODED
        # body (httpx transparently decompressed gzip/deflate) — emitting the
        # upstream's encoding/length would mismatch the identity bytes we send.
        excluded = {'transfer-encoding', 'connection', 'keep-alive', 'content-encoding', 'content-length'}
        response_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in excluded
        }

        return Response(
            resp.content,
            status=resp.status_code,
            headers=response_headers,
        )

    except httpx.ConnectError:
        return Response(
            f'<h1>Agent starting...</h1><p>{agent_type} container is not ready yet. '
            f'Try refreshing in a few seconds.</p>',
            status=502, content_type='text/html'
        )
    except Exception as e:
        logger.exception(f"Proxy error for {agent_type} on {host}")
        return Response(f'Proxy error: {e}', status=502)


# ── WebSocket proxy (item A, PR #84 / #36) ───────────────────────────────────
# An agent may serve ANY WebSocket path — the coding-agent terminal is
# `/terminal`, but moltis loads its LLM list / chat over `/ws/chat`, hermes has
# its own, and user-defined agents can pick anything. httpx (used by the HTTP
# proxy above) cannot upgrade a WS, and Caddy can't reach the sandbox container
# directly, so the browser's wss://…agents.<domain>/<any-path> lands on
# agent-manager and MUST be bridged: browser ⇄ agent-manager ⇄ container.
#
# This one handler serves BOTH the exact `/terminal` route and the generic
# `<path:ws_path>` route (see init_proxy_ws); it dials the upstream at the
# request's ACTUAL path (request.path) — no hardcoded `/terminal` — so it works
# for every agent's WS regardless of path.
#
# Same auth model as the HTTP path — Caddy forward_auth (layer 1) authenticated
# the upgrade request and forwarded X-Authentik-Username; here we re-check
# OWNERSHIP (layer 2) before opening the upstream socket, and the container
# enforces its own owner gate (layer 3). The security gates are IDENTICAL to the
# `/terminal`-only version — generalizing the path does not relax them.
def ws_terminal(ws, ws_path=None):
    """WebSocket bridge: browser ⇄ agent-manager ⇄ container, any WS path.

    Registered on the terminal blueprint's Sock via init_proxy_ws — both as the
    exact `/terminal` route (ws_path=None) and as the generic `<path:ws_path>`
    route. Either way the upstream path is taken from `request.path`, so the
    `ws_path` arg is only the URL-converter capture and isn't used directly.
    """
    import simple_websocket

    host = request.host.split(':')[0]

    # C1 bypass fix: the WS upgrade must also carry the Caddy↔manager proof, so a
    # sandbox forging `ws://agent-manager:5000/<path>` directly is refused
    # before any ownership check.
    if not _proxy_proof_ok():
        logger.warning("WS proxy-proof missing/invalid on %r from %s — refusing",
                       host, request.remote_addr)
        try:
            ws.send('\r\n\x1b[31mForbidden\x1b[0m\r\n')
        except Exception:
            pass
        return

    forwarded_user = request.headers.get('X-Authentik-Username', '')
    agent_type, instance = _resolve_instance_for_host(host)

    if not instance or not _owns(instance, forwarded_user):
        logger.warning("WS ownership denied: user=%r host=%r", forwarded_user, host)
        try:
            ws.send('\r\n\x1b[31mForbidden\x1b[0m\r\n')
        except Exception:
            pass
        return

    if instance['state'] != 'running':
        try:
            current_app.provisioner.start(str(instance['id']), 'lazy-access')
        except Exception as e:
            logger.warning("WS lazy-start failed for %s: %s", instance['container_name'], e)
        try:
            ws.send('\r\n\x1b[33mAgent is starting — reload in a few seconds.\x1b[0m\r\n')
        except Exception:
            pass
        return

    type_info = current_app.catalog.get_type(agent_type)
    ports = json.loads(type_info['ports']) if isinstance(type_info['ports'], str) else type_info['ports']
    container_port = ports['internal']
    container_name = _ui_container_name(instance, type_info)

    # FEATURE 1 — forwarded-port preview over WebSocket (dev-server HMR runs on
    # a WS). `/__port/<port>/rest` bridges the browser WS to the dev server the
    # user started on <port> inside the sandbox, at `rest`. request.path has a
    # leading slash; _parse_port_preview wants it stripped. Only for sandboxed
    # coding agents; otherwise the prefix is a normal WS path.
    _ws_req_path = request.path if request.path else '/terminal'
    _preview_port, _preview_rest = _parse_port_preview(_ws_req_path.lstrip('/'))
    if _preview_port is not None and _is_sandboxed(agent_type, type_info):
        container_port = _preview_port
        container_name = instance['container_name']
        _ws_req_path = _preview_rest

    # Dial the upstream at the request's ACTUAL path (#36) — NOT a hardcoded
    # `/terminal`. The terminal's ?session=… query (and any other agent's WS
    # query string, e.g. moltis auth params) is preserved so the container sees
    # the exact URL the browser opened. request.path is the same path Caddy
    # forwarded (Caddy proxies the per-instance subdomain 1:1 to agent-manager),
    # so a `/ws/chat` in the browser reaches the container as `/ws/chat`.
    ws_path = _ws_req_path if _ws_req_path else '/terminal'
    qs = request.query_string.decode() if request.query_string else ''
    upstream_url = f"ws://{container_name}:{container_port}{ws_path}"
    if qs:
        upstream_url += f"?{qs}"

    # Forward the caller's auth material so the upstream container's own gate
    # (layer-3) authenticates the WS handshake — for EVERY agent's auth style:
    #
    #  • coding-agents (coding-agent-web `_identity_ok`) read the ASCII
    #    `X-Authentik-Username` slug.
    #  • moltis authenticates its `/ws/chat` upgrade by SESSION COOKIE
    #    (auth_middleware: `has_session_cookie`) — set on the earlier HTTP
    #    login that flows through this same proxy. Without the browser's
    #    `Cookie` header the upstream returns 401 (`has_bearer=false
    #    has_session_cookie=false`) → the "Loading LLMs…" hang / reconnect
    #    loop (#36). So we forward the browser's Cookie verbatim.
    #  • hermes / user-defined agents: forwarding the standard Authentik
    #    identity set covers header-based gates too.
    #
    # ASCII safety (PR #84): `simple_websocket.Client`'s header encoder is
    # ASCII-only — a non-ASCII `X-Authentik-Name`/`-Groups` (umlaut display
    # name "Müller", group "Geschäftsführung") crashes the upstream WS connect
    # with UnicodeEncodeError, leaving the WS permanently "unavailable". Unlike
    # the HTTP path (httpx accepts latin-1 BYTES), the WS client re-encodes str
    # as ASCII with no round-trip escape, so we simply DROP any non-ASCII value
    # (rather than crash). The two headers that actually gate auth —
    # `X-Authentik-Username` (an [a-z0-9-] slug) and `Cookie` (ASCII per RFC
    # 6265) — are always ASCII, so this never drops anything load-bearing;
    # only cosmetic display headers can be dropped, and no upstream gate needs
    # those. We do NOT forward the Caddy↔manager proof header (source-IP based,
    # must never leak into a user-controlled container) nor hop-by-hop headers.
    _FWD_WS_HEADERS = (
        'X-Authentik-Username',
        'X-Authentik-Groups',
        'X-Authentik-Email',
        'X-Authentik-Name',
        'X-Authentik-Uid',
        'Cookie',
    )
    up_headers = {}
    for _h in _FWD_WS_HEADERS:
        _v = request.headers.get(_h)
        if _v is None:
            continue
        _v = _v.strip()
        if not _v:
            continue
        try:
            _v.encode('ascii')
        except UnicodeEncodeError:
            logger.debug("WS: dropping non-ASCII header %s for upstream", _h)
            continue
        up_headers[_h] = _v

    try:
        upstream = simple_websocket.Client(upstream_url, headers=up_headers)
    except Exception as e:
        logger.warning("WS upstream connect failed %s: %s", upstream_url, e)
        try:
            ws.send(f'\r\n\x1b[31mAgent terminal unavailable: {e}\x1b[0m\r\n')
        except Exception:
            pass
        return

    current_app.db.update_last_accessed(instance['id'])
    stop = threading.Event()

    def _pump_up_to_client():
        try:
            while not stop.is_set():
                data = upstream.receive(timeout=1)
                if data is None:
                    continue
                ws.send(data)
        except Exception:
            pass
        finally:
            stop.set()

    t = threading.Thread(target=_pump_up_to_client, daemon=True)
    t.start()

    try:
        while not stop.is_set():
            msg = ws.receive(timeout=1)
            if msg is None:
                continue
            upstream.send(msg)
    except Exception:
        pass
    finally:
        stop.set()
        try:
            upstream.close()
        except Exception:
            pass
