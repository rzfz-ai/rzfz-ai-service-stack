#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""scripts/wazuh-oidc-token-check.py — #1940: check an id_token the way the
Wazuh indexer's openid authenticator needs it.

  --code CODE --redirect-uri URI   do the token exchange (the secret comes from
                                   WAZUH_OIDC_CLIENT_SECRET or --secret-file)
  --id-token-file PATH             or check a token captured earlier
Prints one PASS/FAIL line per check and the non-secret claim names; never the
token, never the secret. Exit: 0 all pass; 1 a check failed; 2 usage/network.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    _CRYPTO_ERROR = None
except ImportError as _e:   # reported by main() as a usage error, exit 2
    _CRYPTO_ERROR = str(_e)

SKEW = 30


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _ctx(cafile):
    return ssl.create_default_context(cafile=cafile) if cafile else None


def _get_json(url: str, cafile=None) -> dict:
    with urllib.request.urlopen(url, timeout=20, context=_ctx(cafile)) as r:
        return json.loads(r.read().decode())


def exchange_code(token_endpoint, code, redirect_uri, client_id, secret, cafile=None) -> dict:
    data = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                   "redirect_uri": redirect_uri}).encode()
    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    req = urllib.request.Request(token_endpoint, data=data,
                                 headers={"Authorization": f"Basic {auth}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20, context=_ctx(cafile)) as r:
        return json.loads(r.read().decode())


def check_token(id_token, discovery, jwks, client_id, subject_key, roles_key, now) -> list:
    res = []
    try:
        h64, p64, s64 = id_token.split(".")
        header, claims = json.loads(_b64d(h64)), json.loads(_b64d(p64))
    except ValueError as e:
        return [("format", False, f"not a JWS compact token: {e}")]
    keys = jwks.get("keys", [])
    key = next((k for k in keys if k.get("kid") == header.get("kid")), None) if header.get("kid") else (keys[0] if len(keys) == 1 else None)
    res.append(("kid", key is not None, f"header kid={header.get('kid')!r}, jwks kids={[k.get('kid') for k in keys]}"))
    res.append(("alg", header.get("alg") == "RS256", f"alg={header.get('alg')!r} (the indexer path verified here is RS256)"))
    sig_ok = False
    if key is not None and key.get("kty") == "RSA":
        pub = rsa.RSAPublicNumbers(int.from_bytes(_b64d(key["e"]), "big"),
                                   int.from_bytes(_b64d(key["n"]), "big")).public_key()
        try:
            pub.verify(_b64d(s64), f"{h64}.{p64}".encode(), padding.PKCS1v15(), hashes.SHA256())
            sig_ok = True
        except InvalidSignature:
            sig_ok = False
    if key is not None:   # no key found: `kid` already failed; the signature check cannot run
        res.append(("signature", sig_ok, "RS256 over header.payload with the JWKS key"))
    res.append(("iss", claims.get("iss") == discovery.get("issuer"),
                f"iss={claims.get('iss')!r} discovery issuer={discovery.get('issuer')!r}"))
    aud = claims.get("aud")
    aud_ok = client_id in (aud if isinstance(aud, list) else [aud])
    res.append(("aud", aud_ok, f"aud={aud!r} client_id={client_id!r}"))
    exp = claims.get("exp")
    res.append(("exp", isinstance(exp, int) and exp + SKEW > now, f"exp={exp} now={now}"))
    sub = claims.get(subject_key)
    res.append(("subject_key", isinstance(sub, str) and bool(sub), f"{subject_key} present={sub is not None}"))
    roles = claims.get(roles_key)
    roles_ok = isinstance(roles, list) and all(isinstance(r, str) for r in roles)
    res.append(("roles_key", roles_ok, f"{roles_key}={roles!r}" if roles_ok else f"{roles_key} is {type(roles).__name__}"))
    res.append(("claims", True, "claim names: " + ", ".join(sorted(claims))))
    return res


def _refusal(e: urllib.error.HTTPError) -> str:
    """The token endpoint's own reason: status plus the OAuth `error` and
    `error_description` fields only (never the raw body, never the request)."""
    try:
        body = json.loads(e.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return (f"token endpoint answered HTTP {e.code}: error={body.get('error')!r} "
            f"error_description={str(body.get('error_description', ''))[:300]!r}")


def _save(path: str, token: str) -> None:
    # O_NOFOLLOW: never write through a symlink; fchmod: a file that already
    # existed (with any mode) ends up 600 too.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, token.encode())
    finally:
        os.close(fd)


def _save_path_problem(path: str):
    """Why --save-token PATH cannot be written, or None. Checked BEFORE the code
    exchange: the authorization code is single-use."""
    if os.path.islink(path):
        return "is a symlink (never written through)"
    if os.path.lexists(path) and not os.path.isfile(path):
        return "exists and is not a regular file"
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        return f"its directory {parent} does not exist"
    if not os.access(parent, os.W_OK | os.X_OK):
        return f"its directory {parent} is not writable"
    return None


def main(argv=None) -> int:
    if _CRYPTO_ERROR is not None:
        print(f"[token-check] ERROR: the Python package 'cryptography' is required ({_CRYPTO_ERROR}); "
              "install python3-cryptography", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discovery", required=True)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--code")
    ap.add_argument("--redirect-uri")
    ap.add_argument("--id-token-file")
    ap.add_argument("--secret-file")
    ap.add_argument("--ca-file")
    ap.add_argument("--save-token", help="write the exchanged id_token to this path (mode 600) for a follow-up check")
    ap.add_argument("--subject-key", default="preferred_username")
    ap.add_argument("--roles-key", default="groups")
    a = ap.parse_args(argv)
    try:
        disc = _get_json(a.discovery, a.ca_file)
        jwks = _get_json(disc["jwks_uri"], a.ca_file)
        if a.id_token_file:
            token = open(a.id_token_file, encoding="utf-8").read().strip()
        elif a.code and a.redirect_uri:
            if a.save_token and (why := _save_path_problem(a.save_token)):
                print(f"[token-check] ERROR: --save-token {a.save_token} {why}; nothing exchanged, "
                      "the code is still unused", file=sys.stderr)
                return 2
            secret = (open(a.secret_file, encoding="utf-8").read().strip() if a.secret_file
                      else os.environ.get("WAZUH_OIDC_CLIENT_SECRET", ""))
            if not secret:
                print("[token-check] ERROR: no client secret (WAZUH_OIDC_CLIENT_SECRET or --secret-file)", file=sys.stderr)
                return 2
            try:
                resp = exchange_code(disc["token_endpoint"], a.code, a.redirect_uri, a.client_id, secret, a.ca_file)
            except urllib.error.HTTPError as e:
                print(f"[token-check] ERROR: {_refusal(e)}", file=sys.stderr)
                return 2
            print(f"[token-check] token endpoint answered with keys: {sorted(resp)}")
            token = resp.get("id_token", "")
            if not token:
                print("[token-check] FAIL id_token: the token response carries no id_token")
                return 1
            if a.save_token:
                _save(a.save_token, token)
        else:
            ap.error("give --id-token-file, or --code with --redirect-uri")
    except (OSError, KeyError, ValueError) as e:
        print(f"[token-check] ERROR: {e}", file=sys.stderr)
        return 2
    results = check_token(token, disc, jwks, a.client_id, a.subject_key, a.roles_key, int(time.time()))
    for name, ok, detail in results:
        print(f"[token-check] {'PASS' if ok else 'FAIL'} {name}: {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
