# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""OAuth tokens in OpenBao (#2472, A1 D8; #2476).

With the openbao profile on, an OAuth grant for an integration lives in KV v2:
a PERSON's at secret/users/<oidc-sub>/oauth/<integration> (the immutable OIDC
sub, the identity alias OpenBao's own user policy keys on; never a username),
a TECHNICAL user's at secret/apps/<svc-name>/oauth/<integration>. mcp-manager
reads and writes them as the AppRole svc-mcp-manager, whose policy covers
those two patterns and nothing else; its role_id/secret_id are files on a
read-only volume, never environment. The person half is on only once
sub == X-Authentik-Uid is proven (OPENBAO_SUB_IS_UID=true, A19's gate).

The KV document: {"access", "refresh", "expires_at" (ISO 8601 or ""),
"reauth_required_at" (ISO 8601 or "")}.

Never logs a token; get() is the only method that returns one."""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_KEYS = {"users": re.compile(r"^[A-Za-z0-9_-]{1,128}$"),       # an OIDC sub (Authentik uid)
         "apps": re.compile(r"^svc-[a-z0-9-]{3,40}$")}         # A17's NAME_RE
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: Seconds per OpenBao request.
TIMEOUT = 5


class OpenBaoError(RuntimeError):
    """OpenBao unreachable or refusing, or mcp-manager holds no AppRole."""


def oauth_path(tree: str, key: str, provider: str) -> str:
    """<tree>/<key>/oauth/<provider>, tree in {users, apps}. Refuses anything
    that could name another tree or path segment."""
    rx = _KEYS.get(tree)
    if rx is None:
        raise OpenBaoError(f"OAuth tokens live under users/ or apps/ only, not {tree!r}")
    if not isinstance(key, str) or not rx.fullmatch(key):
        raise OpenBaoError(f"unsafe {tree} key for an OpenBao path: {key!r}")
    if not isinstance(provider, str) or not _PROVIDER.fullmatch(provider):
        raise OpenBaoError(f"unsafe integration id for an OpenBao path: {provider!r}")
    return f"{tree}/{key}/oauth/{provider}"


class OpenBaoOAuthBackend:
    def __init__(self, addr: str, cred_dir: str = "/run/rzfz-bao", http=None, mount: str = "secret",
                 persons: bool = False):
        self._addr = addr.rstrip("/")
        self._dir = Path(cred_dir)
        # trust_env off: the AppRole secret and the tokens never go to a proxy
        self._http = http or httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False)
        self._mount = mount
        self.persons = bool(persons)
        self._token = None
        self._lock = threading.Lock()

    def _login(self) -> None:
        try:
            role_id = (self._dir / "role_id").read_text(encoding="utf-8").strip()
            secret_id = (self._dir / "secret_id").read_text(encoding="utf-8").strip()
        except OSError as e:
            raise OpenBaoError("no svc-mcp-manager AppRole credentials in the credential volume") from e
        if not role_id or not secret_id:
            raise OpenBaoError("no svc-mcp-manager AppRole credentials in the credential volume")
        try:
            r = self._http.request("POST", f"{self._addr}/v1/auth/approle/login",
                                   json={"role_id": role_id, "secret_id": secret_id})
        except httpx.HTTPError as e:
            raise OpenBaoError("OpenBao unreachable") from e
        if r.status_code != 200:
            raise OpenBaoError(f"svc-mcp-manager AppRole login refused ({r.status_code})")
        try:
            self._token = r.json()["auth"]["client_token"]
        except (ValueError, KeyError, TypeError) as e:
            raise OpenBaoError("svc-mcp-manager AppRole login gave no token") from e

    def _call(self, method: str, api_path: str, body=None):
        with self._lock:
            for attempt in (0, 1):
                if self._token is None:
                    self._login()
                try:
                    r = self._http.request(method, f"{self._addr}/v1/{api_path}",
                                           headers={"X-Vault-Token": self._token}, json=body)
                except httpx.HTTPError as e:
                    raise OpenBaoError("OpenBao unreachable") from e
                if r.status_code == 403 and attempt == 0:
                    self._token = None  # expired or revoked token: one fresh login
                    continue
                return r
        raise OpenBaoError("OpenBao refused the AppRole token after a fresh login")

    def get(self, tree: str, key: str, provider: str):
        r = self._call("GET", f"{self._mount}/data/{oauth_path(tree, key, provider)}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise OpenBaoError(f"read refused ({r.status_code})")
        try:
            doc = r.json()["data"]["data"]
        except (ValueError, KeyError, TypeError) as e:
            raise OpenBaoError("unreadable KV answer") from e
        # a deleted latest version answers 200 with data null
        return dict(doc) if isinstance(doc, dict) else None

    def put(self, tree: str, key: str, provider: str, data: dict) -> None:
        r = self._call("POST", f"{self._mount}/data/{oauth_path(tree, key, provider)}", {"data": dict(data)})
        if r.status_code not in (200, 204):
            raise OpenBaoError(f"write refused ({r.status_code})")

    def delete(self, tree: str, key: str, provider: str) -> None:
        """Deletes the metadata: every version goes, not only the latest."""
        r = self._call("DELETE", f"{self._mount}/metadata/{oauth_path(tree, key, provider)}")
        if r.status_code not in (200, 204, 404):
            raise OpenBaoError(f"delete refused ({r.status_code})")

    def providers(self, tree: str, key: str) -> list:
        """The integration ids holding a grant under <tree>/<key>/oauth/."""
        prefix = oauth_path(tree, key, "x")[:-1]
        r = self._call("LIST", f"{self._mount}/metadata/{prefix}")
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise OpenBaoError(f"list refused ({r.status_code})")
        try:
            keys = r.json()["data"]["keys"]
        except (ValueError, KeyError, TypeError) as e:
            raise OpenBaoError("unreadable KV listing") from e
        return [k for k in keys if isinstance(k, str) and _PROVIDER.fullmatch(k)]


def build_oauth_backend(environ=None):
    """The backend iff the openbao profile is on; None keeps the Postgres
    store (the default). Its person half only with OPENBAO_SUB_IS_UID=true
    (A19's gate). Nothing is contacted here: OpenBao may still be starting,
    and every later call fails closed on its own."""
    env = os.environ if environ is None else environ
    profiles = {p.strip() for p in (env.get("COMPOSE_PROFILES") or "").split(",") if p.strip()}
    if "openbao" not in profiles:
        return None
    return OpenBaoOAuthBackend(env.get("OPENBAO_ADDR") or "http://openbao:8200",
                               env.get("OPENBAO_CRED_DIR") or "/run/rzfz-bao",
                               persons=str(env.get("OPENBAO_SUB_IS_UID", "")).strip().lower() == "true")
