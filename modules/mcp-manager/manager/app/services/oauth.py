# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""OAuth authorization-code flow for personal MCP integrations (#36, P3).

Browser-mediated code flow, behind Authentik forward-auth. The `state`
parameter is HMAC-signed and time-limited, and BOUND to the authenticated user
+ mcp_id + provider — so a forged, expired, or cross-user callback is rejected
(CSRF + state-fixation defense). Client SECRETS live server-side in `.env`
(OAUTH_<PROVIDER>_CLIENT_SECRET); they are sent only to the provider token
endpoint and never logged or returned.

The token exchange / refresh take an injected http client so they can be
unit/mock-verified without hitting a real provider. A real external-provider
handshake needs the operator's real client credentials on 0.91.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import urlencode

from app.services.ssrf_guard import (
    SSRFError,
    assert_safe_url,
    derive_allowed_hosts,
)

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 600  # 10 minutes for a state to be redeemed


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class OAuthFlow:
    def __init__(self, secret_key: str, providers: dict):
        if not secret_key:
            raise ValueError("OAuthFlow requires a non-empty signing secret")
        self._secret = secret_key.encode("utf-8")
        self._providers = providers or {}
        # #67: SSRF allowlist, DERIVED from the configured providers' own
        # auth_url/token_url hosts (not hardcoded). Every outbound OAuth fetch
        # below is validated against it + https-only, so a crafted/misconfigured
        # provider URL can't steer the token exchange at an internal host.
        self._allowed_hosts = derive_allowed_hosts(self._providers)

    # ---- signed + expiring state ------------------------------------------

    def make_state(self, user_slug: str, mcp_id: str, provider: str,
                   ttl: int = _DEFAULT_TTL) -> str:
        payload = {
            "user_slug": user_slug,
            "mcp_id": mcp_id,
            "provider": provider,
            "exp": int(time.time()) + int(ttl),
            "nonce": _b64u(os.urandom(9)),
        }
        body = _b64u(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64u(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def verify_state(self, state: str, expected_user_slug: str) -> dict:
        """Validate signature, expiry, and user binding. Raises ValueError on
        any mismatch (fail-closed)."""
        try:
            body, sig = state.split(".", 1)
        except ValueError:
            raise ValueError("malformed state")
        expected_sig = _b64u(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected_sig):
            raise ValueError("state signature mismatch (forged or tampered)")
        try:
            payload = json.loads(_b64u_decode(body))
        except Exception:
            raise ValueError("undecodable state payload")
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("state expired")
        if payload.get("user_slug") != expected_user_slug:
            raise ValueError("state bound to a different user")
        return payload

    # ---- provider URLs / token endpoints ----------------------------------

    def _provider(self, provider: str) -> dict:
        p = self._providers.get(provider)
        if not p:
            raise ValueError(f"unknown OAuth provider: {provider}")
        return p

    def _client_secret(self, provider: str) -> str:
        p = self._provider(provider)
        env_name = p.get("client_secret_env", f"OAUTH_{provider.upper()}_CLIENT_SECRET")
        return os.environ.get(env_name, "")

    def _safe_endpoint(self, provider: str, key: str) -> str:
        """Return the provider's `key` endpoint (auth_url/token_url) only after
        the https-only + host-allowlist SSRF checks pass (#67). Raises
        SSRFError (a ValueError) otherwise — fail closed, never fetched."""
        p = self._provider(provider)
        return assert_safe_url(str(p.get(key, "")), self._allowed_hosts,
                               what=f"{provider} {key}")

    def authorize_url(self, provider: str, mcp_id: str, state: str,
                      redirect_uri: str) -> str:
        p = self._provider(provider)
        auth_url = self._safe_endpoint(provider, "auth_url")
        scopes = p.get("scopes", [])
        params = {
            "client_id": p["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "scope": " ".join(scopes),
        }
        return f"{auth_url}?{urlencode(params)}"

    def exchange_code(self, provider: str, code: str, redirect_uri: str,
                      http=None) -> dict:
        """Exchange an authorization code for tokens at the provider token
        endpoint. `http` is an injected client with .post(url, data, headers,
        timeout) returning a response with .json() / .raise_for_status()."""
        p = self._provider(provider)
        if http is None:
            import httpx
            http = httpx
        token_url = self._safe_endpoint(provider, "token_url")
        data = {
            "grant_type": "authorization_code",
            "client_id": p["client_id"],
            "client_secret": self._client_secret(provider),
            "code": code,
            "redirect_uri": redirect_uri,
        }
        # #67 (review follow-up): pin follow_redirects=False EXPLICITLY. It is
        # httpx's current default, but making it explicit stops a future
        # refactor / swapped client from silently following a 3xx into an
        # internal host (an SSRF that would bypass the pre-fetch URL allowlist).
        resp = http.post(token_url, data=data,
                         headers={"Accept": "application/json"}, timeout=15,
                         follow_redirects=False)
        resp.raise_for_status()
        return resp.json()

    def refresh(self, provider: str, refresh_token: str, http=None) -> dict:
        p = self._provider(provider)
        if http is None:
            import httpx
            http = httpx
        token_url = self._safe_endpoint(provider, "token_url")
        data = {
            "grant_type": "refresh_token",
            "client_id": p["client_id"],
            "client_secret": self._client_secret(provider),
            "refresh_token": refresh_token,
        }
        # #67 (review follow-up): follow_redirects=False pinned explicitly — see
        # exchange_code above (prevents a 3xx-into-internal-host SSRF via a
        # future refactor / swapped client).
        resp = http.post(token_url, data=data,
                         headers={"Accept": "application/json"}, timeout=15,
                         follow_redirects=False)
        resp.raise_for_status()
        return resp.json()


def load_providers(path: str | None = None) -> dict:
    """Load the OAuth provider registry from core/mcp/oauth-providers.yaml.

    Client secrets are NOT in this file — only client_id, auth/token URLs,
    scopes, and the env var name that carries the secret. Returns {} on
    missing/malformed (fail-safe)."""
    p = path or os.environ.get("OAUTH_PROVIDERS_YAML", "/oauth-providers.yaml")
    from pathlib import Path
    fp = Path(p)
    if not fp.exists():
        return {}
    try:
        import yaml
        spec = yaml.safe_load(fp.read_text()) or {}
        return spec.get("providers", {}) or {}
    except Exception:
        logger.exception("oauth-providers.yaml unreadable")
        return {}
