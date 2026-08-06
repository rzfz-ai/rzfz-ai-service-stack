# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Caddy admin API client for per-user MCP-proxy subdomain routing (#36).

Mirrors agent-manager's caddy_client.py. Each provisioned per-user MCP proxy
gets its own subdomain:

    {mcp_id}-{token}.{mcp_domain} -> container:port

`token` is an 8-hex-char HMAC-derived value from the instance UUID. The
username is intentionally NOT part of the subdomain (exposing it would leak
fleet membership / org structure via DNS / TLS SNI). The mapping is recoverable
inside mcp-manager (instance_id -> user) but not externally.

Routes are SSO-gated by the *.{mcp_domain} Caddy block's forward_auth (same as
*.agents); see core/Caddy. Registered via the admin API at start, removed at
stop, reconciled on boot.
"""

import hashlib
import hmac
import logging
import os

from app.services.ssrf_guard import assert_safe_upstream

logger = logging.getLogger(__name__)


def instance_token(instance_id) -> str:
    """Derive a stable 8-hex-char DNS token from an instance UUID.

    HMAC-SHA256 keyed on MCP_DOMAIN_TOKEN_SECRET (falls back to
    MCP_MANAGER_SECRET_KEY, which mcp-manager always has). 32 bits is enough
    collision-resistance for the per-box instance counts while staying short
    enough to read in URLs.
    """
    secret = (os.environ.get("MCP_DOMAIN_TOKEN_SECRET")
              or os.environ.get("MCP_MANAGER_SECRET_KEY")
              or os.environ.get("WEBUI_SECRET_KEY", "mcp-fallback"))
    digest = hmac.new(secret.encode("utf-8"), str(instance_id).encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return digest[:8]


class CaddyClient:
    def __init__(self, admin_url: str, mcp_domain: str):
        # httpx is imported lazily so that modules which only need
        # instance_token() (e.g. agent_wiring, the api blueprint) don't pull in
        # the httpx dependency just to derive a token.
        import httpx  # noqa: F401  (validate availability at construction)
        self._admin_url = admin_url.rstrip("/")
        self._mcp_domain = mcp_domain

    def _instance_domain(self, mcp_id: str, instance_id) -> str:
        return f"{mcp_id}-{instance_token(instance_id)}.{self._mcp_domain}"

    def register_route(self, mcp_id: str, container_name: str,
                       container_port: int, instance_id,
                       route_secret: str | None = None,
                       rewrite_upstream_host: bool = False) -> bool:
        """Register the per-instance subdomain route. Idempotent (PUT then POST).

        CRITICAL-1 (#61): when ``route_secret`` is given, the route ENFORCES
        ``Authorization: Bearer <route_secret>`` at the Caddy layer — any request
        without the exact header gets a 401 and never reaches the proxy (which
        holds the user's live creds). This gates EVERY proxy image uniformly,
        including the external ``razzfazz-mcp-proxy`` we can't modify here; the
        reference test-echo proxy ALSO checks ``PROXY_AUTH_TOKEN`` itself
        (defense in depth). Without a secret (legacy callers) the route is the
        old reverse_proxy-only shape.

        ``rewrite_upstream_host`` (#36 follow-up): rewrite the upstream ``Host``
        header to ``container:port`` before proxying. Needed for MCP servers whose
        streamable-http transport has a DNS-rebinding guard that only accepts the
        internal host (cognee-mcp) — without it they reject the public
        opaque-subdomain Host with "Invalid Host header".
        """
        import httpx
        # #67 defense-in-depth: never install a route that dials an internal
        # infra service / metadata endpoint / private IP as the upstream. The
        # per-user proxy upstream is normally a derived container name
        # (mcp-<id>-<slug>); this refuses a poisoned catalog id / container name.
        assert_safe_upstream(container_name)
        route_id = f"mcp-{mcp_id}-{instance_token(instance_id)}"
        instance_domain = self._instance_domain(mcp_id, instance_id)
        upstream = f"{container_name}:{container_port}"

        proxy_handle = {
            "handler": "reverse_proxy",
            "upstreams": [{"dial": upstream}],
            "transport": {"protocol": "http"},
        }
        if rewrite_upstream_host:
            # cognee-mcp's transport guard only accepts localhost:<port> (its
            # MCP_ALLOWED_HOSTS `*` is not honoured; the container name / public
            # subdomain both 421/"Invalid Host header"). Rewriting to
            # localhost:<port> makes the guard accept the proxied request.
            proxy_handle["headers"] = {
                "request": {"set": {"Host": [f"localhost:{container_port}"]}}
            }
        if route_secret:
            # Sub-routes are evaluated in order: requests WITHOUT the exact
            # bearer match the first sub-route and get a 401; everything else
            # falls through to the reverse_proxy. `header` matches the exact
            # header value, so this is a constant-string equality gate.
            inner_routes = [
                {
                    "match": [{"not": [{"header": {
                        "Authorization": [f"Bearer {route_secret}"]}}]}],
                    "handle": [{
                        "handler": "static_response",
                        "status_code": 401,
                        "headers": {"Content-Type": ["application/json"]},
                        "body": "{\"error\":\"unauthorized\"}",
                    }],
                },
                {"handle": [proxy_handle]},
            ]
        else:
            inner_routes = [{"handle": [proxy_handle]}]

        route = {
            "@id": route_id,
            "match": [{"host": [instance_domain]}],
            "handle": [{
                "handler": "subroute",
                "routes": inner_routes,
            }],
            "terminal": True,
        }
        # Per-host internal-CA TLS automation policy (mirrors agent-manager's
        # caddy_client). The static *.{mcp_domain} block does NOT use on_demand
        # TLS (commit-review CRITICAL #1: it must not expose the manager), so
        # each per-proxy subdomain needs its cert provisioned here explicitly.
        tls_policy = {
            "@id": f"tls-{route_id}",
            "subjects": [instance_domain],
            "issuers": [{"module": "internal"}],
        }
        try:
            resp = httpx.put(f"{self._admin_url}/id/{route_id}", json=route, timeout=10)
            if resp.status_code not in (200, 201):
                resp = httpx.post(
                    f"{self._admin_url}/config/apps/http/servers/srv0/routes/0",
                    json=route, timeout=10,
                )
            if resp.status_code in (200, 201):
                logger.info("Registered MCP route %s: %s -> %s",
                            route_id, instance_domain, upstream)
                try:
                    httpx.put(f"{self._admin_url}/id/tls-{route_id}",
                              json=tls_policy, timeout=10)
                except httpx.HTTPError as e:
                    logger.warning("Could not install TLS policy for %s: %s", route_id, e)
                self._move_wildcard_to_end()
                return True
            logger.error("Failed to register MCP route %s: %s %s",
                         route_id, resp.status_code, resp.text)
            return False
        except httpx.HTTPError as e:
            logger.error("Caddy API error registering %s: %s", route_id, e)
            return False

    def _move_wildcard_to_end(self):
        """Keep ALL `*.` wildcard routes last so specific hosts win.

        The srv0 routes list carries multiple wildcards (e.g. *.{agents_domain}
        AND *.{mcp_domain}). Moving only the first one (the old behavior) left
        the other wildcard ahead of a freshly-registered specific per-proxy host
        — Caddy matches in order, so the `*.{mcp_domain}` 404 catch-all would
        shadow `test-echo-<token>.{mcp_domain}` and the agent got a 404. We now
        move EVERY wildcard route to the end (preserving their relative order),
        so all specific-host routes are evaluated first.
        """
        import httpx
        try:
            resp = httpx.get(
                f"{self._admin_url}/config/apps/http/servers/srv0/routes", timeout=10)
            if resp.status_code != 200:
                return
            routes = resp.json()

            def _is_wildcard(r):
                for m in r.get("match", []):
                    for h in m.get("host", []):
                        if h.startswith("*."):
                            return True
                return False

            specific = [r for r in routes if not _is_wildcard(r)]
            wildcards = [r for r in routes if _is_wildcard(r)]
            reordered = specific + wildcards
            if reordered != routes:
                httpx.patch(
                    f"{self._admin_url}/config/apps/http/servers/srv0/routes",
                    json=reordered, timeout=10)
        except Exception as e:
            logger.warning("Could not reorder wildcard routes: %s", e)

    def route_exists(self, mcp_id: str, instance_id) -> bool:
        import httpx
        route_id = f"mcp-{mcp_id}-{instance_token(instance_id)}"
        try:
            resp = httpx.get(f"{self._admin_url}/id/{route_id}", timeout=10)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def remove_route(self, mcp_id: str, instance_id) -> bool:
        import httpx
        route_id = f"mcp-{mcp_id}-{instance_token(instance_id)}"
        try:
            resp = httpx.delete(f"{self._admin_url}/id/{route_id}", timeout=10)
            if resp.status_code in (200, 404):
                logger.info("Removed MCP route %s (%s)", route_id, resp.status_code)
                return True
            logger.error("Failed to remove MCP route %s: %s", route_id, resp.status_code)
            return False
        except httpx.HTTPError as e:
            logger.error("Caddy API error removing %s: %s", route_id, e)
            return False

    def get_instance_url(self, mcp_id: str, instance_id) -> str:
        return f"https://{self._instance_domain(mcp_id, instance_id)}"

    def ping(self) -> bool:
        import httpx
        try:
            return httpx.get(f"{self._admin_url}/config/", timeout=5).status_code == 200
        except Exception:
            return False
