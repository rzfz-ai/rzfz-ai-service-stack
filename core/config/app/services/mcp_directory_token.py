# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2472 / A1 D7 — mcp-manager's dedicated READ-ONLY Authentik token
(MCP_MANAGER_AUTHENTIK_READONLY_TOKEN).

The MCP gateway resolves Open WebUI callers and owners through
`GET /api/v3/core/users/` only, and must never hold the bootstrap token. This
module gives it a token that can do exactly that and nothing else:

  * role  "razzfazz.ai MCP Directory Reader" — the single global permission
    authentik_core.view_user;
  * group "razzfazz.ai MCP Directory Readers" — that role, not a superuser
    group;
  * user  "rzfz-mcp-directory" — a service account under path rzfz/system
    (never rzfz/technical: it is not a technical user an admin can bind),
    member of that group and of no other;
  * token "rzfz-mcp-directory-readonly" — intent api, not expiring.

Before a key is handed out it is PROVEN from outside, with the key itself,
against the permission rules of the pinned Authentik 2026.5.7:
  * it reads: `GET /core/users/` lists at least one user other than its own
    account, and `GET /core/users/me/` says it IS `rzfz-mcp-directory`;
  * it cannot write: each of these must be refused with 403 — nothing else
    (a 400, a 2xx, a 404 or anything unexpected is "not proven"):
      POST  /core/users/            {}   needs authentik_core.add_user
      POST  /core/groups/           {}   needs authentik_core.add_group
      POST  /rbac/roles/            {}   needs authentik_rbac.add_role
      PATCH /core/users/<other pk>/ {}   needs change_user on that user
    Every probe is harmless even if it were allowed: an empty POST body fails
    validation (nothing is created), an empty PATCH changes nothing.
  Source (goauthentik/authentik, tag version/2026.5.7): the default permission
  class is authentik.rbac.permissions.ObjectPermissions
  (authentik/root/settings.py:231). Its has_permission (rbac/permissions.py
  L18-36) refuses a collection POST without the model's add permission unless
  the view sets rbac_allow_create_without_perm; UserViewSet (core/api/users.py
  L596), GroupViewSet (core/api/groups.py L380) and RoleViewSet
  (rbac/api/roles.py L127) do not. A detail PATCH passes has_permission and is
  decided by has_object_permission (L38-49): global change permission, else
  the view's owner_field, else an object permission — UserViewSet has no
  owner_field, so PATCHing another user needs change_user.
  NOT probed: /core/tokens/. TokenViewSet sets owner_field = "user" and
  rbac_allow_create_without_perm = True (core/api/tokens.py L145-146): EVERY
  authenticated user may create tokens for itself and change or delete its
  own. That is the inherent residual of any Authentik API token: the account
  can manage its own tokens, which grants it nothing it does not have.

A key that fails the proof is never handed out: a freshly minted one is
deleted afterwards (a key that may write must not exist); a kept one is
replaced. An empty key is fail closed (Open WebUI callers get no personal
credentials), a writing key is not.

Idempotent and quiet: a key already in .env that passes the proof is kept
without a token lookup or a view_key (each view_key is a SECRET_VIEW event in
Authentik), and the role, group and account are only written when they
differ from what they must be.

Run by cli/post-install.sh (install and `rzfz upgrade`'s --refresh) as
`docker exec -i razzfazz-config python -m app.services.mcp_directory_token`.
STDIN: the key currently in .env (may be empty). STDOUT: `STATUS kept` or
`STATUS minted`, then `KEY <key>`. Nothing else, and never the bootstrap
token. Exit 0 ok; 2 Authentik unusable — including a timeout or a 5xx during
a proof, which proves nothing either way, so the key in .env is neither kept
as proven nor replaced (nothing changed); 3 the new key failed its proof (it
was deleted).
"""
from __future__ import annotations

import hmac
import sys

from app.services.technical_users import AuthentikAPI, AuthentikTransient, AuthentikUnavailable

ROLE = "razzfazz.ai MCP Directory Reader"
GROUP = "razzfazz.ai MCP Directory Readers"
USERNAME = "rzfz-mcp-directory"
USER_PATH = "rzfz/system"
TOKEN_ID = "rzfz-mcp-directory-readonly"
PERMISSION = "authentik_core.view_user"
#: (method, path, what) — each must be refused with 403 and nothing else.
#: "{other}" is the pk of a user other than the directory account.
WRITE_PROBES = (("POST", "/api/v3/core/users/", "create a user"),
                ("POST", "/api/v3/core/groups/", "create a group"),
                ("POST", "/api/v3/rbac/roles/", "create a role"),
                ("PATCH", "/api/v3/core/users/{other}/", "change another user"))


class DirectoryTokenError(RuntimeError):
    """The key failed its proof; the token has been deleted."""


class DirectoryTokenProvisioner:
    def __init__(self, api: AuthentikAPI, bootstrap_token: str):
        self._api = api
        self._bootstrap = bootstrap_token or ""

    # --- the objects -------------------------------------------------------

    def _one(self, path, params, key, value):
        hits = [x for x in self._api._pages(path, params) if x.get(key) == value]
        if len(hits) > 1:
            raise AuthentikUnavailable(f"{value!r} exists more than once")
        return hits[0] if hits else None

    def ensure_role(self) -> str:
        role = self._one("/api/v3/rbac/roles/", {"name": ROLE}, "name", ROLE)
        if role is None:
            role = self._api._req("POST", "/api/v3/rbac/roles/", json={"name": ROLE})
        uuid = role.get("pk")
        if not isinstance(uuid, str) or not uuid:
            raise AuthentikUnavailable("role: no pk")
        # A global assignment (no model, no object): view any user.
        self._api._req("POST", f"/api/v3/rbac/permissions/assigned_by_roles/{uuid}/assign/",
                       json={"permissions": [PERMISSION]})
        return uuid

    def ensure_group(self, role_uuid: str):
        g = self._one("/api/v3/core/groups/", {"name": GROUP}, "name", GROUP)
        body = {"name": GROUP, "is_superuser": False, "roles": [role_uuid],
                "attributes": {"rzfz_system": "mcp-manager read-only directory"}}
        if g is None:
            g = self._api._req("POST", "/api/v3/core/groups/", json=body)
        elif g.get("is_superuser") is not False or list(g.get("roles") or []) != [role_uuid]:
            self._api._req("PATCH", f"/api/v3/core/groups/{g['pk']}/", json=body)
        if not g.get("pk"):
            raise AuthentikUnavailable("group: no pk")
        return g["pk"]

    def ensure_user(self, group_pk) -> int:
        u = self._one("/api/v3/core/users/", {"username": USERNAME, "include_groups": "false"},
                      "username", USERNAME)
        body = {"username": USERNAME, "name": "MCP gateway directory (read-only)",
                "type": "service_account", "path": USER_PATH, "is_active": True,
                "groups": [group_pk], "attributes": {"rzfz_system": "mcp-manager"}}
        if u is None:
            u = self._api._req("POST", "/api/v3/core/users/", json=body)
        elif (u.get("type"), u.get("path"), u.get("is_active"), list(u.get("groups") or [])) != \
                ("service_account", USER_PATH, True, [group_pk]):
            # Exactly this group: an account someone added to an admin group
            # is put back (and the write proof below would refuse it anyway).
            self._api._req("PATCH", f"/api/v3/core/users/{u['pk']}/", json=body)
        if not isinstance(u.get("pk"), int):
            raise AuthentikUnavailable("user: no pk")
        return u["pk"]

    # --- the token -------------------------------------------------------

    def _token_exists(self) -> bool:
        return self._one("/api/v3/core/tokens/", {"identifier": TOKEN_ID}, "identifier", TOKEN_ID) is not None

    def _view_key(self) -> str:
        d = self._api._req("GET", f"/api/v3/core/tokens/{TOKEN_ID}/view_key/")
        key = d.get("key")
        if not isinstance(key, str) or len(key) < 32:
            raise AuthentikUnavailable("token: no usable key")
        return key

    def _delete_token(self):
        try:
            self._api._req("DELETE", f"/api/v3/core/tokens/{TOKEN_ID}/", ok=(204, 404))
        except AuthentikUnavailable:
            pass

    def _mint(self, user_pk: int) -> str:
        if self._token_exists():
            self._api._req("DELETE", f"/api/v3/core/tokens/{TOKEN_ID}/")
        self._api._req("POST", "/api/v3/core/tokens/", json={
            "identifier": TOKEN_ID, "user": user_pk, "intent": "api", "expiring": False,
            "description": "mcp-manager: read-only user directory (view users only)"})
        return self._view_key()

    def prove(self, key: str) -> None:
        """Read yes, write no — with the key itself. Raises DirectoryTokenError.
        Never deletes or changes anything, whatever Authentik answers."""
        if not key or hmac.compare_digest(key, self._bootstrap):
            raise DirectoryTokenError("the key is empty or the bootstrap token")
        try:
            d = self._api._req("GET", "/api/v3/core/users/", params={"page_size": 2}, token=key)
            me = self._api._req("GET", "/api/v3/core/users/me/", token=key)
        except AuthentikTransient:
            raise              # no answer / 5xx: undecided, never "not proven"
        except AuthentikUnavailable as e:
            raise DirectoryTokenError(f"the key cannot read users ({e})") from None
        if ((me.get("user") or {}).get("username")) != USERNAME:
            raise DirectoryTokenError(f"the key does not belong to {USERNAME}")
        others = [u.get("pk") for u in d.get("results") or []
                  if isinstance(u, dict) and u.get("username") != USERNAME and isinstance(u.get("pk"), int)]
        if not others:
            raise DirectoryTokenError("the key lists no user besides its own account")
        for method, path, what in WRITE_PROBES:
            try:
                self._api._req(method, path.format(other=others[0]), json={}, token=key, ok=(403,))
            except AuthentikTransient:
                raise
            except AuthentikUnavailable as e:
                raise DirectoryTokenError(f"the key was not refused to {what} ({e})") from None

    def run(self, current: str):
        """-> ("kept" | "minted", key). Raises DirectoryTokenError (the new
        token deleted) or AuthentikUnavailable (nothing proven, nothing
        handed out)."""
        user_pk = self.ensure_user(self.ensure_group(self.ensure_role()))
        current = (current or "").strip()
        if current:
            try:
                self.prove(current)
                return "kept", current
            except DirectoryTokenError:
                pass
        key = self._mint(user_pk)
        try:
            self.prove(key)
        except (DirectoryTokenError, AuthentikTransient):
            # Not proven (or not provable right now): a key that might write
            # must not exist. A transient failure exits 2, a refusal 3.
            self._delete_token()
            raise
        return "minted", key


def main(stdin=None, stdout=None, stderr=None, env=None) -> int:
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    if env is None:
        import os
        from app.services.config_manager import ConfigManager
        env = ConfigManager(os.environ.get("STACK_ROOT", "/stack")).read_env()
    current = stdin.read().strip()
    bootstrap = env.get("AUTHENTIK_BOOTSTRAP_TOKEN", "")
    try:
        api = AuthentikAPI(env)
        status, key = DirectoryTokenProvisioner(api, bootstrap).run(current)
    except DirectoryTokenError as e:
        stderr.write(f"mcp directory token REFUSED: {e}; the token was deleted\n")
        return 3
    except AuthentikUnavailable as e:
        stderr.write(f"mcp directory token not provisioned: Authentik unusable ({e})\n")
        return 2
    stdout.write(f"STATUS {status}\nKEY {key}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
