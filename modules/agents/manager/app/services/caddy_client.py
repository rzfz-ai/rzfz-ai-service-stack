# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Instance subdomain naming for the *.agents wildcard (#606).

Each agent instance is addressed by its own subdomain:
  {type}-{token}.agents.<domain>

`token` is an 8-hex-char HMAC-derived value from the instance UUID
(see `instance_token`). The username is intentionally NOT part of the
subdomain — exposing it would leak fleet membership / org structure to
anyone who can resolve DNS or sniff TLS SNI. The mapping is recoverable
inside agent-manager (instance_id -> user) but not externally.

HISTORY (#606): this module used to register/remove per-instance routes
via the Caddy admin API and re-register them after every Caddy restart.
Those dynamic routes are gone — the static `*.agents` Caddyfile wildcard
forward-auths and reverse-proxies EVERY instance host to the manager
(WS-capable, live-verified 2026-08-23), so a route can no longer be
lost. What remains here is the naming: the HMAC token and the URL
builder the dashboard links with.
"""

import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)

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
        # #606: the admin API is no longer used (no dynamic routes); the
        # arg is kept so callers/constructor signatures stay stable.
        self._admin_url = admin_url.rstrip('/')
        self._agents_domain = agents_domain

    def _instance_domain(self, agent_type: str, instance_id) -> str:
        """Build the per-instance subdomain from agent type + opaque token."""
        return f"{agent_type}-{instance_token(instance_id)}.{self._agents_domain}"

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
