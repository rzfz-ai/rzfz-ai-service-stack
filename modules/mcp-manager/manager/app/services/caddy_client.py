# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Per-user MCP-proxy subdomain NAMING (#619 — routes retired).

Each provisioned per-user MCP proxy is addressed by its own subdomain:

    {mcp_id}-{token}.{mcp_domain}

`token` is an 8-hex-char HMAC-derived value from the instance UUID. The
username is intentionally NOT part of the subdomain (it would leak fleet
membership via DNS / TLS SNI); the mapping is recoverable only manager-side.

HISTORY (#619, mirrors agent-manager's #606/#610): this module used to
register/remove per-instance routes via the Caddy admin API (bearer gate +
host-rewrite + per-route TLS) and reconcile them on boot. Those dynamic
routes are gone — the static `*.{mcp_domain}` Caddyfile wildcard proxies
every instance host to the manager's bearer proxy
(app/blueprints/wildcard_proxy.py), which re-creates the same gates per
request from the DB row. What remains here is the naming.
"""

import hashlib
import hmac
import logging
import os


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
        # #619: the admin API is no longer used (no dynamic routes); the arg
        # stays so constructor signatures remain stable.
        self._admin_url = admin_url.rstrip("/")
        self._mcp_domain = mcp_domain

    def _instance_domain(self, mcp_id: str, instance_id) -> str:
        return f"{mcp_id}-{instance_token(instance_id)}.{self._mcp_domain}"
