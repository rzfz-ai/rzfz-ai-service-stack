# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2472: push A17's technical users (Authentik service accounts, A1 D7) to
mcp-manager, the MCP gateway's decision point. mcp-manager holds no Authentik
write token, so the portal tells it: every create, bind, unbind, reassign and
revoke, and a full reconcile on every page load and owner-check tick.

A push or revoke counts as confirmed only on HTTP 200 WITH the JSON answer
mcp-manager gives (a 401 from a stale internal token, a 5xx, or a body that is
not that answer is unconfirmed: the revocation then stays unfinished). A push
that fails is reported (False) and logged by name only; a minted token and a
credential's fields are never logged. The internal token travels in a
header only, and the session never uses a proxy from the environment (the
internal token must not leave the stack)."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

BASE = "http://mcp-manager:5000"
TIMEOUT = 10


def mcp_enabled(profiles) -> bool:
    return "mcp" in set(profiles or ())


class MCPMirror:
    def __init__(self, base=BASE, token=None, http=None):
        self._base = base.rstrip("/")
        self._token = token if token is not None else os.environ.get("MCP_INTERNAL_TOKEN", "")
        if http is None:
            import requests
            http = requests.Session()
            http.trust_env = False
        self._http = http

    def _call(self, method, path, **kw):
        return self._http.request(method, f"{self._base}{path}", timeout=TIMEOUT, allow_redirects=False,
                                  headers={"X-MCP-Internal-Token": self._token}, **kw)

    @staticmethod
    def _answer(r):
        """The JSON object of a 200 answer, else None."""
        if r.status_code != 200:
            return None
        try:
            body = r.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def push(self, user: dict) -> bool:
        try:
            r = self._call("PUT", f"/internal/technical-users/{user['username']}", json={
                "owner_sub": user.get("owner_sub") or "", "owner_username": user.get("owner_username") or "",
                "authentik_pk": user.get("authentik_pk"), "active_since": user.get("active_since"),
                "bindings": list(user.get("bindings") or []), "is_active": user.get("is_active") is True})
            body = self._answer(r)
            ok = body is not None and body.get("name") == user["username"]
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP mirror push for %s failed (%s)", user.get("username"), type(e).__name__)
            return False
        if not ok:
            logger.warning("MCP mirror push for %s answered HTTP %s", user.get("username"), r.status_code)
        return ok

    def revoke(self, name: str) -> bool:
        try:
            r = self._call("DELETE", f"/internal/technical-users/{name}")
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP mirror revoke for %s failed (%s)", name, type(e).__name__)
            return False
        body = self._answer(r)
        if body is None or not isinstance(body.get("revoked"), bool):
            logger.warning("MCP mirror revoke for %s is unconfirmed (HTTP %s)", name, r.status_code)
            return False
        return True

    def rows(self) -> list:
        """The mirror's live technical users as {"name", "authentik_pk"}.
        Raises on any failure (the caller then decides nothing)."""
        r = self._call("GET", "/internal/technical-users")
        body = self._answer(r)
        rows = body.get("technical_users") if body is not None else None
        if not isinstance(rows, list):
            raise ValueError(f"the mirror listing is not usable (HTTP {r.status_code})")
        return [{"name": u["name"], "authentik_pk": u.get("authentik_pk")} for u in rows
                if isinstance(u, dict) and isinstance(u.get("name"), str) and not u.get("revoked_at")]

    def last_calls(self) -> dict:
        """{name: "YYYY-MM-DD HH:MM UTC"} of each technical user's latest MCP
        gateway call (the newest use of any of its gateway tokens), for the
        Settings page. A user that never called is absent. Raises on any
        failure (the page then shows no time, never a wrong one)."""
        from datetime import datetime, timezone
        r = self._call("GET", "/internal/technical-users")
        body = self._answer(r)
        rows = body.get("technical_users") if body is not None else None
        if not isinstance(rows, list):
            raise ValueError(f"the mirror listing is not usable (HTTP {r.status_code})")
        out = {}
        for u in rows:
            if not (isinstance(u, dict) and isinstance(u.get("name"), str)
                    and isinstance(u.get("last_used_at"), str)):
                continue
            try:
                when = datetime.fromisoformat(u["last_used_at"])
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            out[u["name"]] = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return out

    def mint_token(self, name: str, consumer: str) -> str:
        r = self._call("POST", f"/internal/technical-users/{name}/tokens", json={"consumer": consumer})
        r.raise_for_status()
        return r.json()["token"]

    def provision_company_brain(self, name: str) -> str:
        """#2472: the company-memory proxy the gateway's cognee-company-svc
        entry relays to for this technical user; its instance id."""
        r = self._call("POST", f"/internal/technical-users/{name}/instances/cognee-company")
        r.raise_for_status()
        return str((r.json() or {}).get("instance_id") or "")

    def store_credential(self, name: str, mcp_id: str, fields: dict) -> None:
        self._call("PUT", f"/internal/technical-users/{name}/credentials/{mcp_id}",
                   json={"fields": fields}).raise_for_status()

    _UNSET = object()

    def set_gitea_state(self, name: str, state: str, authentik_pk=_UNSET, gitea_user_id=_UNSET) -> bool:
        """#2554: record what the portal owes Gitea for this technical user
        ("live" before a Gitea user or token is made, with the Authentik pk it
        is made for and, once known, the Gitea user id; "owed"; "revoked"
        once Gitea confirmed). True only on mcp-manager's own 200 answer
        echoing exactly what was recorded."""
        body = {"state": state}
        if authentik_pk is not self._UNSET:
            body["authentik_pk"] = authentik_pk
        if gitea_user_id is not self._UNSET:
            body["gitea_user_id"] = gitea_user_id
        try:
            r = self._call("PUT", f"/internal/technical-users/{name}/gitea", json=body)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP mirror: Gitea state of %s not recorded (%s)", name, type(e).__name__)
            return False
        answer = self._answer(r)
        if (answer is None or answer.get("name") != name or answer.get("gitea_state") != state
                or any(answer.get(k) != v for k, v in body.items() if k != "state")):
            logger.warning("MCP mirror: Gitea state of %s is unconfirmed (HTTP %s)", name, r.status_code)
            return False
        return True

    def gitea_records(self) -> dict:
        """#2554: {name: {"state", "revoked", "gitea_user_id", "authentik_pk"}}
        for every technical user with a Gitea record ("revoked": the mirror
        row is revoked). Raises on any failure (the caller decides nothing)."""
        r = self._call("GET", "/internal/technical-users")
        body = self._answer(r)
        rows = body.get("technical_users") if body is not None else None
        if not isinstance(rows, list):
            raise ValueError(f"the mirror listing is not usable (HTTP {r.status_code})")
        out = {}
        for u in rows:
            if not isinstance(u, dict) or not isinstance(u.get("name"), str):
                continue
            if u.get("gitea_state") is None and u.get("gitea_user_id") is None:
                continue
            gid, pk = u.get("gitea_user_id"), u.get("gitea_authentik_pk")
            out[u["name"]] = {"state": u.get("gitea_state"), "revoked": bool(u.get("revoked_at")),
                              "gitea_user_id": gid if isinstance(gid, int) and not isinstance(gid, bool) else None,
                              "authentik_pk": pk if isinstance(pk, int) and not isinstance(pk, bool) else None}
        return out

    def gitea_live(self) -> list:
        """#2554: the technical users whose Gitea service user may still hold
        a token (state "live" or "owed"), as {"name", "revoked",
        "gitea_user_id"}; "revoked" is true when the revoke is due (the
        mirror row is revoked, or the state says owed). Raises on failure."""
        return [{"name": n, "revoked": rec["revoked"] or rec["state"] == "owed",
                 "gitea_user_id": rec["gitea_user_id"]}
                for n, rec in self.gitea_records().items() if rec["state"] in ("live", "owed")]

    def reconcile(self, users: list) -> int:
        return sum(1 for u in users if self.push(u))
