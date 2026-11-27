# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2476 (A21) — the agents' only way to OpenBao: a read-only forwarder in
the Agent Manager.

The agents stay off openbao-net. They reach `http://agent-manager:5000/bao`
on the fence networks they already share with the manager, and the bao CLI
(through `rzfz-secret`) talks to it as if it were OpenBao. The manager
forwards a request with the AGENT's own token (X-Vault-Token), never its
own, and only when:

  * the path is one of
      GET  secret/data/users/<owner-sub>/...        (?version=N)
      GET  secret/metadata/users/<owner-sub>/...    (?list=true)
      GET  sys/internal/ui/mounts/secret/users/<owner-sub>/...  (the CLI's
           KV-version preflight)
      GET  auth/token/lookup-self
      POST/PUT auth/token/renew-self
  * the token's own lookup-self names this instance: its accessor is the one
    the manager delivered to `meta.agent_instance`, and that instance is
    running and its owner is <owner-sub>;
  * the request comes from that instance's container (its address on one of
    the shared networks) and was addressed to the manager by its internal
    name, not through Caddy.

Everything else is refused with OpenBao's own "permission denied" shape. A
token never reaches a log line.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid

log = logging.getLogger(__name__)

_SUB = r"[0-9a-f]{64}"
_REST = r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*"
_ROUTES = (
    # (methods, compiled path, allowed query keys, the group holding the sub)
    (("GET",), re.compile(rf"^secret/data/users/({_SUB})/({_REST})$"), {"version"}),
    (("GET",), re.compile(rf"^secret/metadata/users/({_SUB})(?:/({_REST}))?/?$"), {"list"}),
    (("GET",), re.compile(rf"^sys/internal/ui/mounts/secret/(?:data/|metadata/)?users/({_SUB})(?:/({_REST}))?/?$"), set()),
    (("GET",), re.compile(r"^auth/token/lookup-self$"), set()),
    (("POST", "PUT"), re.compile(r"^auth/token/renew-self$"), set()),
)
_TOKEN = re.compile(r"^[A-Za-z0-9._-]{8,512}$")
_QUERY_VALUES = {"version": re.compile(r"^[0-9]{1,6}$"), "list": re.compile(r"^true$")}
INTERNAL_HOSTS = frozenset({"agent-manager", "agent-manager:5000"})
DENIED = (403, {"errors": ["permission denied"]})
#: How long the set of running agents' addresses is trusted, and how often an
#: unknown address may trigger a re-read (a recreated container's new one).
SOURCES_TTL_S = 30.0
SOURCES_MIN_REFRESH_S = 5.0


class BaoProxy:
    def __init__(self, addr: str, db, container_ips, http=None, timeout: float = 10.0,
                 internal_hosts=INTERNAL_HOSTS):
        import httpx
        self._addr = addr.rstrip("/")
        self._hosts = frozenset(internal_hosts)
        self._sources, self._sources_at, self._refreshed_at = frozenset(), float("-inf"), float("-inf")
        self._sources_lock = threading.Lock()
        self._db = db
        self._ips = container_ips          # container name -> set of addresses
        self._http = http or httpx
        self._timeout = timeout

    # -- the decision ---------------------------------------------------------
    @staticmethod
    def route(method: str, path: str, query: dict):
        """(owner-sub or None, matched) for an allowed method/path/query."""
        if any(seg in (".", "..") for seg in path.split("/")):
            return None, False
        for methods, rx, keys in _ROUTES:
            m = rx.match(path)
            if not m or method not in methods:
                continue
            if set(query) - keys:
                return None, False
            if any(not _QUERY_VALUES[k].match(str(v)) for k, v in query.items()):
                return None, False
            return (m.group(1) if m.groups() else None), True
        return None, False

    def _refresh_sources(self) -> None:
        ips = set()
        try:
            for row in self._db.get_all_instances() or []:
                if row.get("state") == "running" and row.get("container_name"):
                    try:
                        ips |= set(self._ips(row["container_name"]) or ())
                    except Exception:  # noqa: BLE001 — a vanished container has no address
                        pass
        except Exception as e:  # noqa: BLE001 — unreadable: nobody is known
            log.warning("bao proxy: the running agents could not be listed: %s", type(e).__name__)
            ips = set()
        now = time.monotonic()
        self._sources, self._sources_at, self._refreshed_at = frozenset(ips), now, now

    def known_source(self, addr: str) -> bool:
        """Is `addr` some running agent container's address? Asked BEFORE the
        token goes to OpenBao, so the proxy validates no token for a caller
        that is not an agent at all."""
        with self._sources_lock:
            now = time.monotonic()
            if now - self._sources_at > SOURCES_TTL_S or (
                    addr not in self._sources and now - self._refreshed_at > SOURCES_MIN_REFRESH_S):
                self._refresh_sources()
            return bool(addr) and addr in self._sources

    def _caller(self, token: str):
        """The token's own lookup-self, or None."""
        r = self._http.get(f"{self._addr}/v1/auth/token/lookup-self",
                           headers={"X-Vault-Token": token}, timeout=self._timeout)
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data") or {}
        return data if isinstance(data, dict) else None

    def handle(self, method: str, path: str, query: dict, token: str, remote_addr: str,
               host: str, body=None):
        """(status, json body) for one proxied request."""
        method = method.upper()
        if host not in self._hosts:
            log.warning("bao proxy: refused a request not addressed to the internal name")
            return DENIED
        sub, ok = self.route(method, path, query)
        if not ok:
            # the path is not logged: a caller can put anything into it
            log.warning("bao proxy: refused %s on a path outside the allowed routes", method)
            return DENIED
        if not isinstance(token, str) or not _TOKEN.match(token):
            return DENIED
        if not self.known_source(remote_addr):
            log.warning("bao proxy: refused a caller that is no running agent's container")
            return DENIED
        try:
            data = self._caller(token)
        except Exception as e:  # noqa: BLE001
            log.warning("bao proxy: OpenBao lookup failed: %s", type(e).__name__)
            return 502, {"errors": ["OpenBao unreachable"]}
        if not data:
            return DENIED
        try:
            iid = uuid.UUID(str((data.get("meta") or {}).get("agent_instance")))
            row = self._db.get_instance(iid)
        except Exception:  # noqa: BLE001 — no instance named, no access
            row = None
        cfg = (row or {}).get("config") or {}
        if (not row or row.get("state") != "running" or not isinstance(cfg, dict)
                or not cfg.get("openbao_accessor") or cfg.get("openbao_accessor") != data.get("accessor")):
            log.warning("bao proxy: refused a token that is not the one delivered to its instance")
            return DENIED
        owner = row.get("user_id") or ""
        if not re.fullmatch(_SUB, owner) or (sub is not None and sub != owner):
            log.warning("bao proxy: refused instance %s a path outside its owner's", iid)
            return DENIED
        try:
            ips = set(self._ips(row["container_name"]) or ())
        except Exception:  # noqa: BLE001
            ips = set()
        if remote_addr not in ips:
            log.warning("bao proxy: refused instance %s's token from another address", iid)
            return DENIED
        payload = None
        if path == "auth/token/renew-self" and isinstance(body, dict) and "increment" in body:
            inc = body["increment"]
            if isinstance(inc, (int, str)) and re.fullmatch(r"[0-9]{1,6}[smh]?", str(inc)):
                payload = {"increment": inc}
        try:
            r = self._http.request(method, f"{self._addr}/v1/{path}", params=query or None,
                                   json=payload, headers={"X-Vault-Token": token},
                                   timeout=self._timeout)
        except Exception as e:  # noqa: BLE001
            log.warning("bao proxy: forwarding failed: %s", type(e).__name__)
            return 502, {"errors": ["OpenBao unreachable"]}
        try:
            out = r.json() if r.status_code != 204 and r.content else None
        except ValueError:
            out = None
        return r.status_code, out
