# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Caddy admin API client for dynamic per-agent subdomain routing.

Each agent instance gets its own subdomain:
  {type}-{token}.agents.<domain> → container:port

`token` is an 8-hex-char HMAC-derived value from the instance UUID
(see `instance_token`). The username is intentionally NOT part of
the subdomain — exposing it would leak fleet membership / org
structure to anyone who can resolve DNS or sniff TLS SNI. The mapping
is recoverable inside agent-manager (instance_id → user) but not
externally.

Routes are registered via Caddy's admin API at container start
and removed at stop. WebSocket upgrade is handled natively by
Caddy's reverse_proxy.
"""

import hashlib
import hmac
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# The Authentik identity headers copied upstream after a successful
# forward_auth — MUST match the `copy_headers` list in the
# (authentik_upstream_config) snippet in core/Caddy/Caddyfile so dynamically
# registered routes behave identically to the static forward_auth blocks.
_AUTHENTIK_COPY_HEADERS = [
    "X-Authentik-Username",
    "X-Authentik-Groups",
    "X-Authentik-Email",
    "X-Authentik-Uid",
    "X-Authentik-Jwt",
    "X-Authentik-Meta-Jwk",
    "X-Authentik-Meta-Outpost",
    "X-Authentik-Meta-Provider",
    "X-Authentik-Meta-App",
    "X-Authentik-Meta-Version",
]

# Auth server the forward_auth handler dials — same as the Caddyfile static
# blocks (`forward_auth ... authentik-server:9000`).
_AUTHENTIK_UPSTREAM = "authentik-server:9000"


def _forward_auth_handler() -> dict:
    """Build the JSON reverse_proxy handler that Caddy's `forward_auth`
    directive compiles to (C1 layer-1, PR #84 review).

    Reproduces exactly what `forward_auth authentik-server:9000 { import
    authentik_upstream_config }` expands to — verified against the live Caddy
    admin-API config on the reference box (byte-for-byte, modulo header order).
    Prepending this handler ahead of the per-instance reverse_proxy means the
    dynamically-registered route authenticates every request through Authentik
    (and copies the X-Authentik-* identity headers upstream) BEFORE it is
    proxied, just like the static `{$AGENTS_DOMAIN}` site. The manager then
    reads the (real, non-injected) X-Authentik-Username for its ownership check.

    NB (PR #84 re-review): the "came through Caddy" anchor is NOT a header here
    — Caddy's forward_auth does not reliably propagate an extra route-injected
    request header to the upstream (only its own copy_headers do), which 403'd
    the real owner. The manager instead anchors on the Caddy SOURCE IP
    (proxy.py `_proxy_proof_ok`), so nothing extra is injected in this handler.
    """
    copy_routes = [{"handle": [{"handler": "vars"}]}]
    for header in _AUTHENTIK_COPY_HEADERS:
        copy_routes.append({
            "handle": [{"handler": "headers",
                        "request": {"delete": [header]}}],
        })
        copy_routes.append({
            "handle": [{"handler": "headers",
                        "request": {"set": {
                            header: [f"{{http.reverse_proxy.header.{header}}}"]}}}],
            "match": [{"not": [{"vars": {
                f"{{http.reverse_proxy.header.{header}}}": [""]}}]}],
        })
    return {
        "handler": "reverse_proxy",
        "rewrite": {"method": "GET",
                    "uri": "/outpost.goauthentik.io/auth/caddy"},
        "headers": {"request": {"set": {
            "X-Forwarded-Method": ["{http.request.method}"],
            "X-Forwarded-Uri": ["{http.request.uri}"],
        }}},
        "upstreams": [{"dial": _AUTHENTIK_UPSTREAM}],
        "handle_response": [{
            "match": {"status_code": [2]},
            "routes": copy_routes,
        }],
    }


def instance_token(instance_id) -> str:
    """Derive a stable 8-hex-char DNS token from an instance UUID.

    HMAC-SHA256 keyed on AGENT_DOMAIN_TOKEN_SECRET (falls back to
    WEBUI_SECRET_KEY which agent-manager already requires). 32 bits is
    enough collision-resistance for <2^16 concurrent instances per box
    while staying short enough for users to read in URLs.
    """
    secret = os.environ.get('AGENT_DOMAIN_TOKEN_SECRET') or os.environ['WEBUI_SECRET_KEY']
    msg = str(instance_id).encode('utf-8')
    digest = hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()
    return digest[:8]


class CaddyClient:
    def __init__(self, admin_url: str, agents_domain: str):
        self._admin_url = admin_url.rstrip('/')
        self._agents_domain = agents_domain

    def _instance_domain(self, agent_type: str, instance_id) -> str:
        """Build the per-instance subdomain from agent type + opaque token."""
        return f"{agent_type}-{instance_token(instance_id)}.{self._agents_domain}"

    def register_route(self, agent_type: str, user_slug: str,
                       container_name: str, container_port: int,
                       username: str, instance_id=None):
        """Register a per-instance subdomain route via Caddy admin API.

        Creates: {type}-{token}.agents.<domain> → agent-manager:5000
        with TLS (internal in dev, on-demand LE in prod via wildcard
        block), WebSocket support, and Authentik forward-auth.

        NETWORK REACHABILITY (PR #84 review, item A): Caddy is on the stack
        `default` net but NOT on `coding-agents` (the egress-fenced sandbox
        net the coding-agent containers live on, ALONE). A route that dialled
        `agent-<type>-<slug>:<port>` directly therefore could not resolve the
        container → 502. So the per-instance route dials `agent-manager:5000`
        (which IS reachable on `default`, and IS on `coding-agents`, so it can
        reach the sandbox container by name). agent-manager's proxy blueprint
        (app.blueprints.proxy) resolves the subdomain → instance, enforces
        OWNERSHIP (C1 layer-2), and reverse-proxies to the container incl.
        WebSocket upgrades. Adding Caddy to `coding-agents` would breach the
        sandbox-isolation model, so we proxy via the manager instead.

        `instance_id` is required for the new opaque-token subdomain
        scheme; pass the UUID assigned by Provisioner.launch(). When
        omitted (legacy callers), we fall back to the user_slug-based
        subdomain so an upgrade doesn't immediately orphan running
        instances — Provisioner.start() then re-derives the token on
        the next launch/start.
        """
        if instance_id is not None:
            route_id = f"agent-{agent_type}-{instance_token(instance_id)}"
            instance_domain = self._instance_domain(agent_type, instance_id)
        else:
            route_id = f"agent-{agent_type}-{user_slug}"
            instance_domain = f"{agent_type}-{user_slug}.{self._agents_domain}"
        # Dial the manager, NOT the container — see docstring (item A). The
        # container_name/container_port args are retained for the openhands
        # sandbox-path subroute and API compatibility.
        upstream = "agent-manager:5000"

        # C1 layer-1 (PR #84 review): authenticate EVERY request through
        # Authentik before it is proxied. This dynamically registered
        # specific-host route wins over the static *.agents wildcard
        # (terminal:true), so without this handler the manager proxy path was
        # reached with NO edge auth. The forward_auth handler runs first; on a
        # non-2xx auth response Caddy short-circuits (redirect to login / 401)
        # and never proxies. On success it copies the X-Authentik-* identity
        # headers upstream, which the manager's ownership check (proxy.py,
        # C1 layer-2) and the container's own owner-identity gate (app.py
        # `_identity_ok`, C1 layer-3) then enforce.
        # C1 (PR #84 re-review): forward_auth authenticates + copies the
        # X-Authentik-* identity upstream; the "came through Caddy" anchor is a
        # SOURCE-IP check at the manager (proxy.py), not an injected header
        # (Caddy's forward_auth doesn't reliably propagate an extra route-set
        # header to the upstream — it 403'd the real owner).
        sub_routes = [
            {"handle": [_forward_auth_handler()]},
        ]
        # rc6.7 #89: per-user OpenHands needs a sandbox-path-proxy in
        # front of the catchall reverse_proxy. Browser-facing URLs for
        # OpenHands runtime sandboxes (oh-agent-server-* on the docker
        # default bridge with random host ports) are advertised as
        # `https://openhands-<slug>.agents.<domain>/sandbox/{port}/...`
        # via OH_SANDBOX_CONTAINER_URL_PATTERN; this subroute catches
        # them and proxies to host.docker.internal:<port>/<rest>
        # (host-gateway — reachable by Caddy). Only for agent_type='openhands'.
        if agent_type == 'openhands':
            sub_routes.append({
                "match": [{"path_regexp": {"name": "sb",
                                           "pattern": "^/sandbox/(\\d+)(/.*)?$"}}],
                "handle": [
                    {"handler": "rewrite", "uri": "{http.regexp.sb.2}"},
                    {"handler": "reverse_proxy",
                     "upstreams": [{"dial": "host.docker.internal:{http.regexp.sb.1}"}],
                     "transport": {"protocol": "http"}},
                ],
            })
        sub_routes.append({
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": upstream}],
                    "transport": {"protocol": "http"},
                },
            ],
        })

        route = {
            "@id": route_id,
            "match": [{"host": [instance_domain]}],
            "handle": [
                {"handler": "subroute", "routes": sub_routes},
            ],
            "terminal": True,
        }

        # Also ensure TLS automation for this domain (internal CA)
        tls_policy = {
            "@id": f"tls-{route_id}",
            "subjects": [instance_domain],
            "issuers": [{"module": "internal"}],
        }

        try:
            # Register the route — try update first, then insert at position 0
            resp = httpx.put(
                f"{self._admin_url}/id/{route_id}",
                json=route,
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                resp = httpx.post(
                    f"{self._admin_url}/config/apps/http/servers/srv0/routes/0",
                    json=route,
                    timeout=10,
                )

            if resp.status_code in (200, 201):
                logger.info(f"Registered route {route_id}: {instance_domain} → {upstream}")
                # Move the wildcard *.agents route to the end so specific hosts win
                self._move_wildcard_to_end()
                return True

            logger.error(f"Failed to register route {route_id}: {resp.status_code} {resp.text}")
            return False
        except httpx.HTTPError as e:
            logger.error(f"Caddy API error registering route {route_id}: {e}")
            return False

    def _move_wildcard_to_end(self):
        """Move the *.agents wildcard route to the end of the routes list.

        Caddy evaluates routes in order. The wildcard must come AFTER
        specific-host dynamic routes so they take priority.
        """
        try:
            resp = httpx.get(
                f"{self._admin_url}/config/apps/http/servers/srv0/routes",
                timeout=10,
            )
            if resp.status_code != 200:
                return

            routes = resp.json()
            wildcard_idx = None
            for i, r in enumerate(routes):
                hosts = r.get('match', [{}])[0].get('host', [])
                if hosts and hosts[0].startswith('*.'):
                    wildcard_idx = i
                    break

            if wildcard_idx is not None and wildcard_idx < len(routes) - 1:
                # Remove wildcard from current position
                wildcard = routes.pop(wildcard_idx)
                routes.append(wildcard)
                # Replace entire routes array
                httpx.patch(
                    f"{self._admin_url}/config/apps/http/servers/srv0/routes",
                    json=routes,
                    timeout=10,
                )
                logger.info(f"Moved wildcard route from index {wildcard_idx} to end")
        except Exception as e:
            logger.warning(f"Could not reorder wildcard route: {e}")

    def route_exists(self, agent_type: str, instance_id) -> bool:
        """Return True if the per-instance route is currently registered.

        Lets the boot-time reconcile (Provisioner.reconcile_routes) cheaply
        skip routes already present, so the periodic reconcile only does work
        when Caddy has actually lost the route (restart / box reboot). Uses
        the current opaque-token route_id scheme. A network/API error is
        treated as "not present" so the caller re-registers — safe, because
        register_route is idempotent (PUT-then-POST).
        """
        route_id = f"agent-{agent_type}-{instance_token(instance_id)}"
        try:
            resp = httpx.get(f"{self._admin_url}/id/{route_id}", timeout=10)
            return resp.status_code == 200
        except httpx.HTTPError as e:
            logger.warning(f"Caddy API error checking route {route_id}: {e}")
            return False

    def remove_route(self, agent_type: str, user_slug: str, instance_id=None):
        """Remove a per-instance subdomain route.

        Tries the new opaque-token route_id first (when instance_id is given),
        then falls back to the legacy user_slug-based id so we clean up
        routes registered by older releases too.
        """
        candidates = []
        if instance_id is not None:
            candidates.append(f"agent-{agent_type}-{instance_token(instance_id)}")
        candidates.append(f"agent-{agent_type}-{user_slug}")
        ok = True
        for route_id in candidates:
            try:
                resp = httpx.delete(
                    f"{self._admin_url}/id/{route_id}",
                    timeout=10,
                )
                if resp.status_code in (200, 404):
                    logger.info(f"Removed route {route_id} ({resp.status_code})")
                else:
                    logger.error(f"Failed to remove route {route_id}: {resp.status_code}")
                    ok = False
            except httpx.HTTPError as e:
                logger.error(f"Caddy API error removing route {route_id}: {e}")
                ok = False
        return ok

    def get_instance_url(self, agent_type: str, user_slug: str,
                         instance_id=None) -> str:
        """Get the full URL for an agent instance.

        With instance_id (new code path), returns the token-based URL.
        Without, falls back to the legacy user_slug URL so the dashboard
        can still link orphaned instances launched by older code.
        """
        if instance_id is not None:
            return f"https://{self._instance_domain(agent_type, instance_id)}"
        return f"https://{agent_type}-{user_slug}.{self._agents_domain}"

    def ping(self) -> bool:
        """Check Caddy admin API connectivity."""
        try:
            resp = httpx.get(f"{self._admin_url}/config/", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False
