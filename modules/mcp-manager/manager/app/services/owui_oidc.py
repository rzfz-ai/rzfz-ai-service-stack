# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Open WebUI `system_oauth`: the chat user's own Authentik access token
(#2472; design record docs/superpowers/specs/2026-10-mcp-rework-design.md
§2.2/§2.3, "the 2026.11 direction").

With single sign-on through Authentik on (`OWUI_OIDC_ACTIVE=true`, Open WebUI
signs in at the `openwebui-oidc` provider), Open WebUI's gateway connections
use `auth_type: system_oauth`. Open WebUI 0.12.4 then sends the access token
of the chat user's own OAuth session (`utils/tools.py`
build_tool_server_headers -> `utils/oauth.py` get_system_oauth_token), and
renews it five minutes before it expires.

That token is a JWT Authentik signed for the `openwebui-oidc` client. The
gateway checks it here, with Authentik's public keys alone:

  * the signature, with a key from the provider's JWKS
    (`/application/o/openwebui-oidc/jwks/`, fetched in-stack and cached);
    asymmetric algorithms only — never `none`, never HS* (Open WebUI's own
    session JWT is HS256 and keeps going to the session path);
  * `iss` is exactly the provider's issuer, `https://<AUTHENTIK_DOMAIN>/
    application/o/openwebui-oidc/`;
  * `aud` is the Open WebUI client id (`OPENWEBUI_OIDC_CLIENT_ID`); a token
    Authentik issued to any other application is refused;
  * `exp` has not passed (`nbf` / `iat` not in the future), within LEEWAY;
  * `sub` is set. The provider's `sub_mode` is `hashed_user_id` (pinned in
    its blueprint), which is the Authentik user's `uid`: the key every
    person's credential is stored under.

The sub alone does not make a person: the gateway then asks the read-only
directory whether that uid is an active internal user (the same check as an
agent's token), with its relay slot held. Nothing of Open WebUI's user table
is trusted on this path: an Open WebUI admin who rewrites a user's email
changes nothing here.

No env bypass, no client secret: the check needs only public keys. Every
failure — unreachable JWKS, an unknown key, a malformed token — is
"nobody" (the gateway's 401), never a guess."""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

PROVIDER_SLUG = "openwebui-oidc"
AUTHENTIK_BASE_DEFAULT = "http://authentik-server:9000"
#: Seconds of clock difference tolerated on exp / nbf / iat (Authentik and
#: mcp-manager share the host clock; Open WebUI renews 5 minutes early).
LEEWAY = 30
#: How long a fetched key set is used before it is fetched again.
JWKS_MAX_AGE = 600.0
#: An unknown `kid` refetches the key set at most this often (a key rotation
#: is picked up at once; a stream of forged kids costs one fetch per window).
JWKS_REFETCH_MIN = 30.0
#: After a failed fetch, the key set is not asked again for this long.
JWKS_FAILED_FOR = 5.0
JWKS_TIMEOUT = 5.0
MAX_TOKEN_LEN = 8192

_RSA = {"RS256": "SHA256", "RS384": "SHA384", "RS512": "SHA512"}
_EC = {"ES256": ("SHA256", "P-256", 32), "ES384": ("SHA384", "P-384", 48),
       "ES512": ("SHA512", "P-521", 66)}
ALGORITHMS = frozenset(_RSA) | frozenset(_EC)


def _b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _segments(token: str):
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LEN:
        return None
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    try:
        header = json.loads(_b64d(parts[0]))
    except Exception:  # noqa: BLE001
        return None
    return (header, parts) if isinstance(header, dict) else None


def looks_like_authentik_token(token: str) -> bool:
    """True for a compact JWS signed with an asymmetric algorithm — the shape
    of an Authentik access token. Open WebUI's own session JWT (HS256), an
    `sk-…` API key and anything else are not, and keep going to the session
    path. Says nothing about validity."""
    seg = _segments(token)
    return seg is not None and seg[0].get("alg") in ALGORITHMS


def system_oauth_on(environ=None) -> bool:
    """Open WebUI signs in through Authentik (`OWUI_OIDC_ACTIVE`, derived from
    ENABLE_OPENWEBUI_OIDC by setup, the Settings portal and every upgrade)
    and its client id is known: the gateway connections use system_oauth."""
    env = os.environ if environ is None else environ
    return (str(env.get("OWUI_OIDC_ACTIVE") or "").strip().lower() == "true"
            and bool(str(env.get("OPENWEBUI_OIDC_CLIENT_ID") or "").strip()))


def _public_key(jwk: dict):
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    kty = jwk.get("kty")
    if kty == "RSA":
        n = int.from_bytes(_b64d(jwk["n"]), "big")
        e = int.from_bytes(_b64d(jwk["e"]), "big")
        return rsa.RSAPublicNumbers(e, n).public_key()
    if kty == "EC":
        curve = {"P-256": ec.SECP256R1, "P-384": ec.SECP384R1, "P-521": ec.SECP521R1}[jwk["crv"]]
        x = int.from_bytes(_b64d(jwk["x"]), "big")
        y = int.from_bytes(_b64d(jwk["y"]), "big")
        return ec.EllipticCurvePublicNumbers(x, y, curve()).public_key()
    raise ValueError(f"unsupported key type {kty!r}")


def _verify_signature(alg: str, key, signing_input: bytes, sig: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    try:
        if alg in _RSA:
            if not isinstance(key, rsa.RSAPublicKey):
                return False
            key.verify(sig, signing_input, padding.PKCS1v15(), getattr(hashes, _RSA[alg])())
            return True
        if alg in _EC:
            hname, crv, size = _EC[alg]
            if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != {
                    "P-256": "secp256r1", "P-384": "secp384r1", "P-521": "secp521r1"}[crv]:
                return False
            if len(sig) != 2 * size:
                return False
            der = encode_dss_signature(int.from_bytes(sig[:size], "big"),
                                       int.from_bytes(sig[size:], "big"))
            key.verify(der, signing_input, ec.ECDSA(getattr(hashes, hname)()))
            return True
    except InvalidSignature:
        return False
    return False


class AuthentikTokenVerifier:
    """Checks an Authentik access token issued to Open WebUI (module doc)."""

    def __init__(self, issuer: str, audience: str, jwks_url: str, http=None, clock=time.time,
                 monotonic=time.monotonic):
        if not issuer or not audience or not jwks_url:
            raise ValueError("the verifier needs an issuer, an audience and a JWKS URL")
        self.issuer, self.audience, self.jwks_url = issuer, audience, jwks_url
        self._http, self._clock, self._mono = http, clock, monotonic
        self._lock = threading.Lock()
        self._keys: dict = {}          # kid -> (alg hint, public key)
        self._fetched_at = None        # monotonic time of the last good fetch
        self._failed_until = 0.0

    @classmethod
    def from_env(cls, environ=None):
        """The verifier, or None while Open WebUI does not sign in through
        Authentik (system_oauth_on false): the path is then off."""
        env = os.environ if environ is None else environ
        if not system_oauth_on(env):
            return None
        domain = str(env.get("AUTHENTIK_DOMAIN") or "").strip() or "auth.localhost"
        base = str(env.get("AUTHENTIK_URL") or "").strip().rstrip("/") or AUTHENTIK_BASE_DEFAULT
        return cls(issuer=f"https://{domain}/application/o/{PROVIDER_SLUG}/",
                   audience=str(env["OPENWEBUI_OIDC_CLIENT_ID"]).strip(),
                   jwks_url=f"{base}/application/o/{PROVIDER_SLUG}/jwks/")

    # -- the key set -------------------------------------------------------

    def _fetch(self) -> bool:
        """Fetch the key set (caller holds the lock). False on any failure."""
        http = self._http
        if http is None:
            from app.services.gateway_identity import shared_client
            http = shared_client("authentik")
        try:
            r = http.get(self.jwks_url, timeout=JWKS_TIMEOUT, follow_redirects=False)
            if r.status_code != 200:
                raise ValueError(f"answered {r.status_code}")
            body = r.json()
            keys = {}
            for jwk in (body.get("keys") if isinstance(body, dict) else None) or []:
                if not isinstance(jwk, dict) or jwk.get("use") not in (None, "sig"):
                    continue
                try:
                    keys[str(jwk.get("kid") or "")] = (jwk.get("alg"), _public_key(jwk))
                except Exception:  # noqa: BLE001 — a key we cannot read is no key
                    continue
            if not keys:
                raise ValueError("no usable signing key")
        except Exception as e:  # noqa: BLE001
            logger.warning("Authentik key set for Open WebUI tokens unavailable: %s",
                           type(e).__name__ if not isinstance(e, ValueError) else e)
            self._failed_until = self._mono() + JWKS_FAILED_FOR
            return False
        self._keys, self._fetched_at = keys, self._mono()
        return True

    def _key(self, kid: str):
        """The key for `kid`. At most ONE thread fetches; while it does, the
        others answer from the keys they have (or None) instead of waiting,
        so a hanging Authentik ties up one gateway thread, not every Open
        WebUI call (the check runs before the relay slot)."""
        keys, fetched_at, now = self._keys, self._fetched_at, self._mono()
        fresh = fetched_at is not None and now - fetched_at < JWKS_MAX_AGE
        if fresh and kid in keys:
            return keys[kid]
        if not self._lock.acquire(blocking=False):
            return keys.get(kid) if fresh else None
        try:
            now = self._mono()
            fetched_at = self._fetched_at
            fresh = fetched_at is not None and now - fetched_at < JWKS_MAX_AGE
            if fresh and kid in self._keys:
                return self._keys[kid]
            if now < self._failed_until:
                return None
            # Stale, never fetched, or an unknown kid (a rotation): fetch,
            # but for an unknown kid at most once per JWKS_REFETCH_MIN.
            if fresh and now - fetched_at < JWKS_REFETCH_MIN:
                return None
            if not self._fetch():
                return None
            return self._keys.get(kid)
        finally:
            self._lock.release()

    def keys_reachable(self) -> int:
        """For the box check: how many signing keys the in-stack JWKS serves
        right now (0 when unreachable). Forces a fetch."""
        with self._lock:
            self._failed_until = 0.0
            return len(self._keys) if self._fetch() else 0

    # -- the token ---------------------------------------------------------

    def verify(self, token: str):
        """The token's claims when every check of the module doc holds, else
        None."""
        seg = _segments(token)
        if seg is None:
            return None
        header, parts = seg
        alg = header.get("alg")
        if alg not in ALGORITHMS or header.get("crit"):
            return None
        hit = self._key(str(header.get("kid") or ""))
        if hit is None:
            return None
        alg_hint, key = hit
        if alg_hint and alg_hint != alg:
            return None
        try:
            sig = _b64d(parts[2])
            claims = json.loads(_b64d(parts[1]))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(claims, dict):
            return None
        if not _verify_signature(alg, key, f"{parts[0]}.{parts[1]}".encode("ascii"), sig):
            return None
        if claims.get("iss") != self.issuer:
            return None
        aud = claims.get("aud")
        if isinstance(aud, str):
            if aud != self.audience:
                return None
        elif isinstance(aud, list):
            # Several audiences: ours among them AND the token was issued to us.
            if self.audience not in aud or claims.get("azp") != self.audience:
                return None
        else:
            return None
        now = self._clock()
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp + LEEWAY <= now:
            return None
        for k in ("nbf", "iat"):
            v = claims.get(k)
            if v is not None and (not isinstance(v, (int, float)) or v - LEEWAY > now):
                return None
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub.strip() or len(sub) > 256:
            return None
        return claims
