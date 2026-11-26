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
handshake needs the operator's real client credentials on a test box.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from urllib.parse import urlencode

from app.services.ssrf_guard import (
    SSRFError,
    assert_safe_url,
    derive_allowed_hosts,
)

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 600  # 10 minutes for a state to be redeemed

#: #2554: the ONLY in-stack token endpoint each provider may name as
#: `token_url_internal` — exact URLs, bound to their provider, never expanded
#: from the environment. http is allowed only here: the exchange never
#: leaves the stack's own network (mcp-manager has no route to the public
#: git.<domain> and does not trust the box CA). Everything else goes through
#: assert_safe_url (https, derived host allow-list).
INTERNAL_TOKEN_URLS = {"gitea": "http://gitea:3000/login/oauth/access_token"}
#: `${VAR}` in a provider's auth_url/token_url, expanded from the environment
#: at load. Only *_DOMAIN variables: a URL can never carry a secret.
_VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_EXPANDABLE = re.compile(r"^[A-Z][A-Z0-9_]*_DOMAIN$")


#: #2472: variables that may fill one PATH segment of a provider URL (never
#: its host), each with the shape its value must have. M365_TENANT_ID is the
#: box's own Entra tenant: a tenant id (GUID) or a tenant domain. The
#: multi-tenant authorities (common, organizations, consumers) have neither
#: shape, so a box can never sign in people from any tenant.
_PATH_VARS = {
    "M365_TENANT_ID": re.compile(
        r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        r"|[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)$"),
}
#: Tenant ids that are not one organisation's tenant: the Microsoft
#: personal-account (MSA, "consumers") tenant and the first-party services
#: tenant. Refused like the named multi-tenant authorities.
_REFUSED_TENANTS = frozenset({
    "9188040d-6c67-4c5b-b112-36a304b66dad",
    "f8cdef31-a31e-4b4a-93e4-5f571e91255a",
})
_SCHEME_HOST = re.compile(r"^https://[^/]*")


def _expand(value: str) -> str:
    """${VAR} from the environment. An unset variable, or one that is not a
    *_DOMAIN, expands to '' so the URL fails the https/host SSRF check
    (fail closed) instead of guessing."""
    def sub(m):
        name = m.group(1)
        return os.environ.get(name, "") if _EXPANDABLE.match(name) else ""
    return _VAR.sub(sub, value)


def _expand_path(value: str) -> str:
    """#2472: a _PATH_VARS variable in the PATH of an https URL whose host is
    literal. Returns the URL with it filled in, or '' (fail closed: the SSRF
    check refuses an empty URL) when the variable sits in the scheme or host,
    is unset, or does not have its shape. Other variables are left for
    _expand."""
    head = _SCHEME_HOST.match(value)
    if not head or "${" in head.group(0):
        return ""
    failed = False

    def sub(m):
        nonlocal failed
        name = m.group(1)
        if name not in _PATH_VARS:
            return m.group(0)
        got = os.environ.get(name, "").strip()
        if not _PATH_VARS[name].fullmatch(got) or got.lower() in _REFUSED_TENANTS:
            failed = True
        return got
    out = head.group(0) + _VAR.sub(sub, value[head.end():])
    return "" if failed else out


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
        #: #3429: remote MCP servers by URL (app.services.remote_mcp.RemoteMcp),
        #: set by create_app; None = none offered.
        self.remote = None

    # ---- signed + expiring state ------------------------------------------

    def make_state(self, user_slug: str, mcp_id: str, provider: str,
                   ttl: int = _DEFAULT_TTL, nonce: str | None = None) -> str:
        """#3429: `nonce` names a server-side pending authorization (the
        remote MCP flow's PKCE verifier); random otherwise."""
        payload = {
            "user_slug": user_slug,
            "mcp_id": mcp_id,
            "provider": provider,
            "exp": int(time.time()) + int(ttl),
            "nonce": nonce or _b64u(os.urandom(9)),
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

    def setup_gaps(self, provider: str) -> list[str]:
        """#3424: what this box still lacks before `provider` can be
        connected — the .env keys that are empty, and `auth_url` when the
        sign-in address could not be built. [] means set up. An unknown
        provider is one gap, never "set up"."""
        p = self._providers.get(provider)
        if not isinstance(p, dict):
            return [f"provider {provider!r}"]
        gaps = []
        if not p.get("client_id"):
            gaps.append(client_id_key(provider, p))
        if not self._client_secret(provider):
            gaps.append(client_secret_key(provider, p))
        if not p.get("auth_url"):
            gaps.append("auth_url")
        return gaps

    def settings_for(self, provider: str) -> dict:
        """#3424: the names an administrator needs to set `provider` up —
        the two .env keys and the variables its sign-in address is built
        from. Names only, never a value."""
        p = self._providers.get(provider)
        p = p if isinstance(p, dict) else {}
        return {"client_id_key": client_id_key(provider, p),
                "client_secret_key": client_secret_key(provider, p),
                "url_vars": list(p.get("_url_vars") or [])}

    def _safe_endpoint(self, provider: str, key: str) -> str:
        """Return the provider's `key` endpoint (auth_url/token_url) only after
        the https-only + host-allowlist SSRF checks pass (#67). Raises
        SSRFError (a ValueError) otherwise — fail closed, never fetched."""
        p = self._provider(provider)
        return assert_safe_url(str(p.get(key, "")), self._allowed_hosts,
                               what=f"{provider} {key}")

    def _token_endpoint(self, provider: str) -> str:
        """Where the code/refresh exchange goes. A provider that names a
        `token_url_internal` gets exactly its entry in INTERNAL_TOKEN_URLS or
        nothing (SSRFError). A provider whose `token_url` was expanded from
        the environment MUST name one: an expanded URL is never a token
        endpoint, so no environment value can point the exchange elsewhere."""
        p = self._provider(provider)
        internal = p.get("token_url_internal")
        if internal is not None or p.get("_token_url_expanded"):
            if not isinstance(internal, str) or INTERNAL_TOKEN_URLS.get(provider) != internal:
                raise SSRFError(f"{provider} token_url_internal is not the allowed in-stack endpoint")
            return internal
        return self._safe_endpoint(provider, "token_url")

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
        # #2472: provider-specific authorize parameters (Atlassian 3LO needs
        # audience=api.atlassian.com and prompt=consent). They only ADD keys:
        # a provider entry can never replace client_id, redirect_uri, state
        # or the scopes.
        for k, v in _extra_auth_params(p).items():
            params.setdefault(k, v)
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
        token_url = self._token_endpoint(provider)
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
        resp = http.post(token_url, headers={"Accept": "application/json"}, timeout=15,
                         follow_redirects=False, **_token_body(p, data))
        resp.raise_for_status()
        return resp.json()

    def refresh(self, provider: str, refresh_token: str, http=None) -> dict:
        p = self._provider(provider)
        if http is None:
            import httpx
            http = httpx
        token_url = self._token_endpoint(provider)
        data = {
            "grant_type": "refresh_token",
            "client_id": p["client_id"],
            "client_secret": self._client_secret(provider),
            "refresh_token": refresh_token,
        }
        # #67 (review follow-up): follow_redirects=False pinned explicitly — see
        # exchange_code above (prevents a 3xx-into-internal-host SSRF via a
        # future refactor / swapped client).
        resp = http.post(token_url, headers={"Accept": "application/json"}, timeout=15,
                         follow_redirects=False, **_token_body(p, data))
        resp.raise_for_status()
        return resp.json()

    def client_credentials(self, provider: str, client_id: str, client_secret: str,
                           http=None) -> dict:
        """#2472: a machine token for a technical user (an Atlassian service
        account): the client-credentials grant at the provider's declared
        `client_credentials_url`, after the same https + derived-host SSRF
        checks as every other OAuth fetch. The pair is the technical user's
        own (vaulted per `~svc-<name>`), never the box's OAuth app. The
        request is form-encoded (RFC 6749 4.4.2; Atlassian documents the
        service-account grant that way) whatever `token_request` says: that
        field is for the code and refresh exchanges."""
        self._provider(provider)  # an unknown provider raises ValueError
        if http is None:
            import httpx
            http = httpx
        url = self._safe_endpoint(provider, "client_credentials_url")
        data = {"grant_type": "client_credentials", "client_id": client_id,
                "client_secret": client_secret}
        resp = http.post(url, data=data, headers={"Accept": "application/json"}, timeout=15,
                         follow_redirects=False)
        resp.raise_for_status()
        return resp.json()


def _extra_auth_params(p: dict) -> dict:
    """#2472: the provider's `extra_auth_params`, as strings. Anything but a
    mapping of scalars is ignored (fail safe: the standard request)."""
    extra = p.get("extra_auth_params") or {}
    if not isinstance(extra, dict):
        return {}
    return {str(k): str(v) for k, v in extra.items()
            if isinstance(v, (str, int, float)) and not isinstance(v, bool)}


def _token_body(p: dict, data: dict) -> dict:
    """#2472: how the token request carries its fields — a form (RFC 6749,
    the default) or JSON for a provider that declares `token_request: json`
    (Atlassian)."""
    return {"json": data} if p.get("token_request") == "json" else {"data": data}


def client_id_key(provider: str, p: dict) -> str:
    """The .env key that carries `provider`'s client id."""
    return str(p.get("client_id_env") or f"OAUTH_{provider.upper()}_CLIENT_ID")


def client_secret_key(provider: str, p: dict) -> str:
    """The .env key that carries `provider`'s client secret."""
    return str(p.get("client_secret_env") or f"OAUTH_{provider.upper()}_CLIENT_SECRET")


def _mode_scopes(p: dict) -> None:
    """#3433: a provider with `scopes_read_write` (Microsoft 365) asks for
    those instead of `scopes` while M365_MCP_MODE is read-write, plus
    `scopes_send` while M365_MCP_ALLOW_SEND is true (m365_guard.settings: any
    other spelling is read-only / false). A malformed list leaves the read
    scopes (fail closed)."""
    rw = p.get("scopes_read_write")
    if rw is None:
        return
    from app.services import m365_guard
    s = m365_guard.settings()
    send = p.get("scopes_send") or []
    if not s.read_write or not isinstance(rw, list) or not all(isinstance(x, str) for x in rw):
        return
    scopes = list(rw)
    if s.allow_send and isinstance(send, list):
        scopes += [x for x in send if isinstance(x, str) and x not in scopes]
    p["scopes"] = scopes


def load_providers(path: str | None = None) -> dict:
    """Load the OAuth provider registry from core/mcp/oauth-providers.yaml.

    Client secrets are NOT in this file — only client_id, auth/token URLs,
    scopes, and the env var name that carries the secret. Returns {} on
    missing/malformed (fail-safe).

    #2554: `${VAR}` (a *_DOMAIN variable) in `auth_url`/`token_url` is
    expanded from the environment here (#2472: a _PATH_VARS variable only in
    the URL path, after its literal host); `client_id_env` names the env key
    that carries the client id (post-install registers Gitea's app and writes
    it to .env). `token_url_internal` is never expanded (_token_endpoint)."""
    p = path or os.environ.get("OAUTH_PROVIDERS_YAML", "/oauth-providers.yaml")
    from pathlib import Path
    fp = Path(p)
    if not fp.exists():
        return {}
    try:
        import yaml
        spec = yaml.safe_load(fp.read_text()) or {}
        providers = spec.get("providers", {}) or {}
        for name, p in providers.items():
            if not isinstance(p, dict):
                continue
            # #3424: remembered before expansion, so "not set up" can name
            # the variables an incomplete sign-in address is built from.
            p["_url_vars"] = sorted({v for k in ("auth_url", "token_url")
                                     if isinstance(p.get(k), str)
                                     for v in _VAR.findall(p[k])})
            for k in ("auth_url", "token_url"):
                if isinstance(p.get(k), str) and any(
                        f"${{{v}}}" in p[k] for v in _PATH_VARS):
                    # #2472: a path-only variable never moves the host, so
                    # the exchange stays at the declared https endpoint.
                    p[k] = _expand_path(p[k])
                if isinstance(p.get(k), str) and "${" in p[k]:
                    p[k] = _expand(p[k])
                    if k == "token_url":
                        p["_token_url_expanded"] = True
            # #3424: the client id comes from .env (`client_id_env`). A
            # non-empty literal `client_id` in the file is the deprecated
            # fallback for boxes that edited the tracked file.
            from_env = os.environ.get(str(p["client_id_env"]), "") if p.get("client_id_env") else ""
            if from_env:
                p["client_id"] = from_env
            elif p.get("client_id"):
                logger.warning("oauth provider %s: client_id read from oauth-providers.yaml; "
                               "that is deprecated, set %s in .env instead",
                               name, client_id_key(name, p))
            else:
                p["client_id"] = ""
            _mode_scopes(p)
        return providers
    except Exception:
        logger.exception("oauth-providers.yaml unreadable")
        return {}
