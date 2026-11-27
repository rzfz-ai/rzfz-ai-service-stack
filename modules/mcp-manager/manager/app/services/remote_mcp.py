# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Official remote MCP servers by URL, with OAuth 2.1 and dynamic client
registration (#3429).

A catalogue entry with a `remote_mcp:` block (e.g. Atlassian's
https://mcp.atlassian.com/v2/mcp) needs no app registered by an operator:

  1. discovery: the server's protected-resource metadata (RFC 9728, at
     /.well-known/oauth-protected-resource<path>) names its authorization
     server; that server's metadata (RFC 8414, path-inserted well-known,
     then the OpenID forms) names the endpoints. The metadata's `resource`
     must be the entry's URL and its `issuer` the authorization server
     named, character for character;
  2. registration (RFC 7591), once per box and entry, on first use: a
     public client (token_endpoint_auth_method `none`) when the server
     offers it, else a confidential one whose secret is stored encrypted.
     The registration is stored with the endpoints exactly as validated;
     those are the only URLs ever dialled for the entry. A provider that
     rejects the registration later (invalid_client) gets a new one on the
     next connect;
  3. per person: authorization code with PKCE (S256 only; the verifier is
     kept server-side, encrypted, single use) and the RFC 8707 `resource`
     parameter; tokens are stored and renewed like every other OAuth grant
     (CredentialStore: Postgres or OpenBao);
  4. the gateway relays the person's calls to the entry's URL with their
     bearer token (gateway_credentials, a `catalog:<id>` gateway server).

Egress (SSRF): every URL dialled — the entry's own, the metadata documents,
every discovered endpoint, the browser's authorize redirect — must be https,
on one of the entry's `allowed_hosts` (an exact host list in the catalogue,
never widened by metadata), port 443, carry no user-info, fragment, control
character or whitespace, not be an internal IP in any encoding, and resolve
only to public addresses. Behind an egress proxy that applies to the URL
(HTTPS_PROXY without a NO_PROXY match, resolved as httpx resolves it) the
proxy resolves the name, so the LOCAL resolution check is skipped and every
other check stays; the proxy's CA comes from the corporate-proxy overlay
(SSL_CERT_FILE, which httpx reads). Redirects are never followed, answers
are read streamed and cut at MAX_ANSWER_BYTES, and nothing a provider says
is logged with a token or secret in it.

Only an explicit JSON `{"error": "invalid_client"}` from the token endpoint
drops the box-wide registration; any other refusal (a bare 401, an HTML
page from a proxy, unauthorized_client) concerns this person's grant only.
The authorization response's `iss` (RFC 9207) must name the issuer when
present, and is required when the server advertises it. Revoking in the
portal also revokes at the provider (RFC 7009) when it names a revocation
endpoint — best effort, never blocking the local revoke.

Errors reach the person as RemoteMcpError.message, in plain words; `.code`
is for the audit row."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import re
import secrets
import socket
import threading
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from app.services.ssrf_guard import SSRFError, assert_safe_url

logger = logging.getLogger(__name__)

#: Bytes a metadata document or a token/registration answer may have.
MAX_ANSWER_BYTES = 64 * 1024
#: Seconds for one discovery, registration or token request.
TIMEOUT = 15
#: Seconds a started authorization stays redeemable (as the signed state).
PENDING_TTL = 600
#: The grants this client registers for.
GRANT_TYPES = ["authorization_code", "refresh_token"]
#: Token endpoint authentication methods this client can do, best first.
AUTH_METHODS = ("none", "client_secret_basic", "client_secret_post")
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_ENDPOINTS = ("authorization_endpoint", "token_endpoint", "registration_endpoint")
_OPTIONAL_ENDPOINTS = ("revocation_endpoint",)


class RemoteMcpError(Exception):
    """`code` for the audit; `message` for the person, in plain words."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RemoteSpec:
    mcp_id: str
    display: str
    url: str
    allowed_hosts: frozenset
    scopes: tuple
    client_name: str


def validate_entry(m: dict) -> list[str]:
    """Catalogue checks for an entry's `remote_mcp:` block (catalog.py)."""
    mid = m.get("id")
    block = m.get("remote_mcp")
    if block is None:
        return []
    errors = []
    if not isinstance(block, dict):
        return [f"{mid}: remote_mcp must be a mapping"]
    if m.get("runtime") != "gateway" or not str(m.get("cred_model", "")).startswith("oauth:"):
        errors.append(f"{mid}: remote_mcp needs runtime 'gateway' and an oauth:<name> cred_model")
    hosts = block.get("allowed_hosts")
    if not isinstance(hosts, list) or not hosts or not all(
            isinstance(h, str) and _HOST.match(h) for h in hosts):
        errors.append(f"{mid}: remote_mcp.allowed_hosts must be a non-empty list of host names")
        hosts = []
    url = block.get("url")
    parts = urlsplit(url) if isinstance(url, str) else None
    if not parts or parts.scheme != "https" or parts.username or parts.password \
            or parts.query or parts.fragment or (parts.hostname or "") not in hosts:
        errors.append(f"{mid}: remote_mcp.url must be an https URL on one of its allowed_hosts")
    unknown = set(block) - {"url", "allowed_hosts", "client_name"}
    if unknown:
        errors.append(f"{mid}: remote_mcp has unknown keys {sorted(unknown)}")
    return errors


def spec_for(integ) -> RemoteSpec | None:
    """The entry's remote spec, or None for any other entry."""
    if not isinstance(integ, dict) or not isinstance(integ.get("remote_mcp"), dict):
        return None
    block = integ["remote_mcp"]
    return RemoteSpec(
        mcp_id=str(integ.get("id", "")), display=str(integ.get("display_name") or integ.get("id")),
        url=str(block.get("url", "")),
        allowed_hosts=frozenset(h.lower() for h in block.get("allowed_hosts") or ()),
        scopes=tuple(str(s) for s in integ.get("scopes") or ()),
        client_name=str(block.get("client_name") or "razzfazz.ai MCP gateway"))


# ---- egress --------------------------------------------------------------------


def _resolve(host: str) -> list[str]:
    """The addresses `host` resolves to (a seam for the tests)."""
    return [ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def _resolves_public(host: str) -> bool:
    try:
        addrs = _resolve(host)
    except OSError:
        return False
    if not addrs:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def check_url(url, spec: RemoteSpec, what: str, *, resolve: bool = True) -> str:
    """`url` if it may be dialled (or shown to the browser) for `spec`;
    RemoteMcpError otherwise."""
    try:
        if not isinstance(url, str) or len(url) > 2048:
            raise SSRFError(f"{what} is not a URL")
        if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in url):
            # urlsplit silently drops CR/LF/TAB; the raw string must not reach
            # a request line or a redirect.
            raise SSRFError(f"{what} contains whitespace or control characters")
        assert_safe_url(url, set(spec.allowed_hosts), what=what)
        parts = urlsplit(url)
        if parts.username or parts.password or parts.fragment:
            raise SSRFError(f"{what} carries user info or a fragment")
        if parts.port not in (None, 443):
            raise SSRFError(f"{what} names a port other than 443")
        if resolve and not proxied(url) and not _resolves_public(parts.hostname or ""):
            raise SSRFError(f"{what} does not resolve to public addresses only")
    except (SSRFError, ValueError) as e:
        logger.warning("remote MCP %s: refused %s: %s", spec.mcp_id, what, e)
        raise RemoteMcpError("endpoint_refused", (
            f"{spec.display} could not be connected: its sign-in service points to an address "
            "this box does not allow. Ask your administrator.")) from e
    return url


def proxied(url: str) -> bool:
    """Whether httpx (trust_env, the default here) sends `url` through an
    egress proxy from the environment: its own mount rules —
    HTTP(S)_PROXY/ALL_PROXY, minus NO_PROXY, most specific pattern first."""
    try:
        import httpx
        from httpx._utils import URLPattern, get_environment_proxies
        mounts = sorted(((URLPattern(k), v) for k, v in get_environment_proxies().items()),
                        key=lambda kv: kv[0])
        target = httpx.URL(url)
        for pattern, proxy in mounts:
            if pattern.matches(target):
                return proxy is not None
    except Exception:  # noqa: BLE001 — undecidable: resolve locally (fail closed)
        logger.warning("remote MCP: the egress proxy settings could not be read")
    return False


def _http():
    import httpx
    return httpx


def _request(http, method, url, spec, what, **kw):
    """(status, dict | None): one request, never redirected, its answer read
    streamed and abandoned past MAX_ANSWER_BYTES. Status 0 = no answer."""
    try:
        with http.stream(method, url, timeout=TIMEOUT, follow_redirects=False, **kw) as resp:
            status = int(resp.status_code)
            if 300 <= status < 400:
                logger.warning("remote MCP %s: %s answered a redirect; not followed",
                               spec.mcp_id, what)
                return status, None
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf += chunk
                if len(buf) > MAX_ANSWER_BYTES:
                    logger.warning("remote MCP %s: %s answer over %d bytes; not read",
                                   spec.mcp_id, what, MAX_ANSWER_BYTES)
                    return status, None
    except Exception as e:  # noqa: BLE001
        logger.warning("remote MCP %s: %s unreachable: %s", spec.mcp_id, what, type(e).__name__)
        return 0, None
    try:
        body = json.loads(bytes(buf) or b"null")
    except ValueError:
        return status, None
    return status, body if isinstance(body, dict) else None


def _get(http, url, spec, what):
    check_url(url, spec, what)
    return _request(http, "GET", url, spec, what, headers={"Accept": "application/json"})


def _unreachable(spec):
    return RemoteMcpError("discovery_failed", (
        f"{spec.display} could not be reached to set up the connection. Try again later; "
        "if it keeps failing, ask your administrator."))


def _well_known(url: str, suffix: str, *, insert: bool) -> str:
    """RFC 8414 / RFC 9728 well-known URL: inserted between host and path
    (`insert`), or appended to the path (the OpenID form)."""
    p = urlsplit(url)
    path = p.path.rstrip("/")
    new = f"/.well-known/{suffix}{path}" if insert else f"{path}/.well-known/{suffix}"
    return urlunsplit(("https", p.netloc, new, "", ""))


@dataclass(frozen=True)
class Discovery:
    resource: str
    issuer: str
    endpoints: dict
    auth_methods: tuple
    scopes: tuple
    iss_supported: bool = False


def discover(spec: RemoteSpec, http=None) -> Discovery:
    http = http or _http()
    check_url(spec.url, spec, "the MCP server URL")
    prm = None
    root = urlunsplit(("https", urlsplit(spec.url).netloc, "", "", ""))
    for url in (_well_known(spec.url, "oauth-protected-resource", insert=True),
                _well_known(root, "oauth-protected-resource", insert=True)):
        status, body = _get(http, url, spec, "the protected-resource metadata")
        if status == 200 and body:
            prm = body
            break
    if not prm:
        raise _unreachable(spec)
    if str(prm.get("resource", "")).rstrip("/") != spec.url.rstrip("/"):
        logger.warning("remote MCP %s: metadata names another resource", spec.mcp_id)
        raise RemoteMcpError("metadata_mismatch", (
            f"{spec.display} answered for a different server than the one configured; "
            "nothing was connected. Ask your administrator."))
    servers = prm.get("authorization_servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
        raise _unreachable(spec)
    issuer = check_url(servers[0], spec, "the authorization server")
    asm = None
    for url in (_well_known(issuer, "oauth-authorization-server", insert=True),
                _well_known(issuer, "openid-configuration", insert=True),
                _well_known(issuer, "openid-configuration", insert=False)):
        status, body = _get(http, url, spec, "the authorization-server metadata")
        if status == 200 and body:
            asm = body
            break
    if not asm:
        raise _unreachable(spec)
    if asm.get("issuer") != issuer:
        logger.warning("remote MCP %s: authorization-server issuer mismatch", spec.mcp_id)
        raise RemoteMcpError("metadata_mismatch", (
            f"{spec.display}'s sign-in service did not identify itself as expected; "
            "nothing was connected. Ask your administrator."))
    endpoints = {}
    for key in _ENDPOINTS:
        if not asm.get(key):
            raise RemoteMcpError("registration_unsupported", (
                f"{spec.display} does not offer automatic registration of MCP clients; "
                "it cannot be connected without an app registered by an administrator."))
        endpoints[key] = check_url(asm[key], spec, key.replace("_", " "))
    for key in _OPTIONAL_ENDPOINTS:
        if asm.get(key):
            endpoints[key] = check_url(asm[key], spec, key.replace("_", " "))
    if "S256" not in (asm.get("code_challenge_methods_supported") or []):
        raise RemoteMcpError("pkce_unsupported", (
            f"{spec.display}'s sign-in service does not support the required protection (PKCE); "
            "nothing was connected."))
    methods = asm.get("token_endpoint_auth_methods_supported")
    methods = tuple(methods) if isinstance(methods, list) else ("client_secret_basic",)
    supported = prm.get("scopes_supported")
    scopes = spec.scopes
    if isinstance(supported, list) and supported:
        scopes = tuple(s for s in spec.scopes if s in supported)
    return Discovery(resource=spec.url, issuer=issuer, endpoints=endpoints,
                     auth_methods=methods, scopes=scopes,
                     iss_supported=asm.get("authorization_response_iss_parameter_supported") is True)


# ---- the box's registration ---------------------------------------------------------


@dataclass(frozen=True)
class Registration:
    client_id: str
    client_secret: str
    auth_method: str
    endpoints: dict
    scopes: tuple
    redirect_uri: str
    issuer: str = ""
    iss_supported: bool = False


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_nonce() -> str:
    """The key of one pending authorization (also carried in the signed state)."""
    return _b64u(secrets.token_bytes(24))


class RemoteMcp:
    """The registration, the PKCE flow and the token requests for every
    remote entry. `db` holds the registrations and pending authorizations,
    `cipher` encrypts their secrets."""

    def __init__(self, db, cipher, http=None):
        self._db = db
        self._cipher = cipher
        self._http = http
        self._locks: dict = {}
        self._guard = threading.Lock()

    def _client(self):
        return self._http or _http()

    def _lock(self, mcp_id):
        with self._guard:
            return self._locks.setdefault(mcp_id, threading.Lock())

    # -- registration --

    def _stored(self, spec: RemoteSpec, redirect_uri: str) -> Registration | None:
        """The stored registration, only while it was made for this URL and
        redirect and every endpoint still passes today's allow-list."""
        row = self._db.get_remote_client(spec.mcp_id)
        if not row or row.get("resource") != spec.url or row.get("redirect_uri") != redirect_uri:
            return None
        endpoints = row.get("endpoints") or {}
        if isinstance(endpoints, str):
            endpoints = json.loads(endpoints)
        for key in _ENDPOINTS:
            if key not in endpoints:
                return None
        for key, url in endpoints.items():
            check_url(url, spec, key.replace("_", " "))
        secret = ""
        if row.get("client_secret_enc"):
            secret = self._cipher.decrypt(row["client_secret_enc"])
        return Registration(row["client_id"], secret, row.get("auth_method") or "none", endpoints,
                            tuple((row.get("scopes") or "").split()), redirect_uri,
                            str(row.get("issuer") or ""), bool(row.get("iss_supported")))

    def registration(self, spec: RemoteSpec, redirect_uri: str) -> Registration:
        """The box's registration for `spec`: stored, or discovered and
        registered now (one at a time per entry)."""
        reg = self._stored(spec, redirect_uri)
        if reg:
            return reg
        with self._lock(spec.mcp_id):
            reg = self._stored(spec, redirect_uri)
            if reg:
                return reg
            disc = discover(spec, self._client())
            return self._register(spec, disc, redirect_uri)

    def _register(self, spec: RemoteSpec, disc: Discovery, redirect_uri: str) -> Registration:
        method = next((m for m in AUTH_METHODS if m in disc.auth_methods), None)
        if method is None:
            raise RemoteMcpError("registration_unsupported", (
                f"{spec.display} offers no client authentication this box supports; "
                "nothing was connected."))
        body = {"client_name": spec.client_name, "redirect_uris": [redirect_uri],
                "grant_types": GRANT_TYPES, "response_types": ["code"],
                "token_endpoint_auth_method": method}
        if disc.scopes:
            body["scope"] = " ".join(disc.scopes)
        url = check_url(disc.endpoints["registration_endpoint"], spec, "registration endpoint")
        status, ans = _request(self._client(), "POST", url, spec, "the registration", json=body,
                               headers={"Accept": "application/json"})
        if status == 0:
            raise _unreachable(spec)
        if status not in (200, 201) or not ans or not isinstance(ans.get("client_id"), str) \
                or not ans["client_id"] or len(ans["client_id"]) > 512:
            logger.warning("remote MCP %s: registration refused (HTTP %s, %s)", spec.mcp_id, status,
                           (ans or {}).get("error", "no error code"))
            raise RemoteMcpError("registration_refused", (
                f"{spec.display} refused to register this box as an MCP client. An administrator "
                f"of your {spec.display} site may need to allow this box for MCP clients first; "
                "nothing was connected."))
        uris = ans.get("redirect_uris")
        if isinstance(uris, list) and redirect_uri not in uris:
            raise RemoteMcpError("registration_refused", (
                f"{spec.display} registered this box with a different return address; "
                "nothing was connected. Ask your administrator."))
        secret = ans.get("client_secret") if isinstance(ans.get("client_secret"), str) else ""
        granted = ans.get("token_endpoint_auth_method") or method
        if granted not in AUTH_METHODS or (granted != "none" and not secret):
            raise RemoteMcpError("registration_refused", (
                f"{spec.display} registered this box in a way this box cannot use; "
                "nothing was connected."))
        self._db.upsert_remote_client(
            spec.mcp_id, resource=disc.resource, issuer=disc.issuer, redirect_uri=redirect_uri,
            endpoints=disc.endpoints, client_id=ans["client_id"],
            client_secret_enc=self._cipher.encrypt(secret) if secret else None,
            auth_method=granted, scopes=" ".join(disc.scopes), iss_supported=disc.iss_supported)
        logger.info("remote MCP %s: registered this box at the provider (auth method %s)",
                    spec.mcp_id, granted)
        return Registration(ans["client_id"], secret, granted, dict(disc.endpoints), disc.scopes,
                            redirect_uri, disc.issuer, disc.iss_supported)

    def forget(self, spec: RemoteSpec, client_id: str) -> None:
        """The provider rejected registration `client_id`: the next connect
        registers again."""
        n = self._db.delete_remote_client(spec.mcp_id, client_id)
        if n:
            logger.warning("remote MCP %s: the provider rejected this box's registration; "
                           "it will register again on the next connect", spec.mcp_id)

    # -- the person's authorization --

    def authorize_url(self, spec: RemoteSpec, owner: str, nonce: str, state: str,
                      redirect_uri: str) -> str:
        reg = self.registration(spec, redirect_uri)
        verifier = _b64u(secrets.token_bytes(48))
        challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
        self._db.insert_oauth_pending(nonce, owner, spec.mcp_id, reg.client_id,
                                      self._cipher.encrypt(verifier), PENDING_TTL)
        params = {"response_type": "code", "client_id": reg.client_id,
                  "redirect_uri": redirect_uri, "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256",
                  "resource": spec.url}
        if reg.scopes:
            params["scope"] = " ".join(reg.scopes)
        base = check_url(reg.endpoints["authorization_endpoint"], spec, "authorization endpoint")
        return f"{base}{'&' if urlsplit(base).query else '?'}{urlencode(params)}"

    def exchange(self, spec: RemoteSpec, owner: str, nonce: str, code: str,
                 redirect_uri: str, iss: str | None = None) -> dict:
        pending = self._db.take_oauth_pending(nonce, owner, spec.mcp_id)
        if not pending:
            raise RemoteMcpError("state_unknown", (
                "This sign-in link was already used or has expired. Connect again."))
        reg = self._stored(spec, redirect_uri)
        if not reg or reg.client_id != pending["client_id"]:
            raise RemoteMcpError("registration_changed", (
                f"The connection to {spec.display} was reset while you were signing in. "
                "Connect again."))
        if (iss is not None and iss != reg.issuer) or (iss is None and reg.iss_supported):
            # RFC 9207: the answer must come from the issuer this box
            # registered with (a mix-up defence); its code is never redeemed.
            logger.warning("remote MCP %s: authorization response issuer mismatch", spec.mcp_id)
            raise RemoteMcpError("issuer_mismatch", (
                f"The sign-in answer did not come from {spec.display}'s own sign-in service; "
                "nothing was connected. Connect again."))
        verifier = self._cipher.decrypt(pending["verifier_enc"])
        return self._token(spec, reg, {"grant_type": "authorization_code", "code": code,
                                       "redirect_uri": redirect_uri, "code_verifier": verifier,
                                       "resource": spec.url})

    def discard_pending(self, spec: RemoteSpec, owner: str, nonce: str) -> None:
        """The provider sent the person back without a code: the pending
        authorization is spent."""
        if nonce:
            self._db.take_oauth_pending(nonce, owner, spec.mcp_id)

    def refresh(self, spec: RemoteSpec, refresh_token: str, redirect_uri: str) -> dict:
        reg = self._stored(spec, redirect_uri)
        if not reg:
            raise RemoteMcpError("client_rejected", (
                f"The connection to {spec.display} has to be set up again. Connect again."))
        return self._token(spec, reg, {"grant_type": "refresh_token",
                                       "refresh_token": refresh_token, "resource": spec.url})

    @staticmethod
    def _client_auth(reg: Registration, data: dict):
        """(form data, headers) carrying the client's own authentication."""
        headers = {"Accept": "application/json"}
        data = dict(data, client_id=reg.client_id)
        if reg.auth_method == "client_secret_basic":
            pair = f"{quote(reg.client_id, safe='')}:{quote(reg.client_secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        elif reg.auth_method == "client_secret_post":
            data["client_secret"] = reg.client_secret
        return data, headers

    def revoke_at_provider(self, spec: RemoteSpec, tokens: dict, redirect_uri: str) -> bool:
        """RFC 7009: revoke the person's refresh and access tokens at the
        provider when its metadata named a revocation endpoint. Best effort:
        True only when every token was accepted; never raises."""
        try:
            reg = self._stored(spec, redirect_uri)
            url = (reg.endpoints.get("revocation_endpoint") if reg else None)
            if not url:
                return False
            check_url(url, spec, "revocation endpoint")
            ok, sent = True, 0
            for key, hint in (("oauth_refresh", "refresh_token"), ("oauth_access", "access_token")):
                if not tokens.get(key):
                    continue
                data, headers = self._client_auth(reg, {"token": tokens[key], "token_type_hint": hint})
                status, _ans = _request(self._client(), "POST", url, spec, "the revocation",
                                        data=data, headers=headers)
                sent += 1
                ok = ok and status == 200
            if sent:
                logger.info("remote MCP %s: revocation at the provider %s", spec.mcp_id,
                            "done" if ok else "not confirmed")
            return ok and sent > 0
        except Exception as e:  # noqa: BLE001 — the local revoke goes on regardless
            logger.warning("remote MCP %s: revocation at the provider failed: %s", spec.mcp_id,
                           type(e).__name__)
            return False

    def _token(self, spec: RemoteSpec, reg: Registration, data: dict) -> dict:
        url = check_url(reg.endpoints["token_endpoint"], spec, "token endpoint")
        data, headers = self._client_auth(reg, data)
        status, ans = _request(self._client(), "POST", url, spec, "the token answer",
                               data=data, headers=headers)
        if status == 0:
            raise _unreachable(spec)
        ans = ans or {}
        token_type = str(ans.get("token_type") or "bearer").lower()
        if status == 200 and isinstance(ans.get("access_token"), str) and ans["access_token"] \
                and token_type == "bearer":
            return ans
        err = ans.get("error") if isinstance(ans.get("error"), str) else ""
        logger.warning("remote MCP %s: token request (%s) refused: HTTP %s %s", spec.mcp_id,
                       data["grant_type"], status, err or "no error code")
        if err == "invalid_client":
            # Only the provider's explicit JSON word drops the box-wide
            # registration: a bare 401 or an HTML page may be a proxy or a
            # firewall, and everyone would have to connect again.
            self.forget(spec, reg.client_id)
            raise RemoteMcpError("client_rejected", (
                f"{spec.display} no longer accepts this box's registration. It has been reset: "
                "connect again."))
        if err in ("invalid_grant", "unauthorized_client") or status == 401:
            raise RemoteMcpError("grant_rejected", (
                f"Your {spec.display} authorization expired or was revoked. Connect again."))
        raise RemoteMcpError("token_failed", (
            f"{spec.display} did not complete the sign-in. Try connecting again."))


def callback_error_message(spec_display: str, error: str) -> str:
    """What the provider's `error` on the browser redirect means, in plain words."""
    if error == "access_denied":
        return (f"You did not allow access at {spec_display}, so nothing was connected. "
                "You can connect again at any time.")
    if error in ("invalid_client", "unauthorized_client", "invalid_request"):
        return (f"{spec_display} did not accept this box's sign-in request. "
                "Connect again; if it keeps failing, ask your administrator.")
    return f"{spec_display} did not complete the sign-in. Connect again."
