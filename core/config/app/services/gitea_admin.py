# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Gitea service users for technical identities (#2554, A1 D7 technical users).

Runs in the config portal, which holds the stack .env anyway; never in
mcp-manager (GITEA_ADMIN_PASSWORD is, on a normal box, the Authentik admin's
password too, and mcp-manager must not hold it). mcp-manager only vaults the
minted token for its gitea gateway server.

A service user is a Gitea BOT user (`gitea admin user create --user-type
bot`, run in the gitea container through the socket proxy: the REST API
cannot create one). Gitea 1.27.3 (unchanged up to 28.4.0) auto-links a first-time SSO login to an
existing account only through GetIndividualUserByName and
GetIndividualUserByPrimaryEmail (routers/web/auth/auth.go, the
ACCOUNT_LINKING=auto branch of createUserInContext), and both filter on
`type = UserTypeIndividual` (models/user/user.go). A bot is never found by
either, so no person can log in as it, whatever e-mail or name they hold in
Authentik. 28.3.0 and later additionally link only when the SSO identity's e-mail or
derived name matches the found account (oauth2IdentityMatchesUser). A bot has no password (the CLI refuses one), so it has no web
login either; its API tokens work like any user's.

Everything else goes through the API with the admin's Basic auth (Gitea's
token endpoints require it — reqBasicOrRevProxyAuth, routers/api/v1/api.go).
The user is restricted (it sees only repos it is explicitly added to), no
site admin, no git hooks, no local imports, no repository or organisation of
its own. Its description names its human owner. Its tokens are scoped and
named `rzfz-gw-*`; a revoke deletes exactly those and prohibits the login.

Ownership: the portal takes over ONLY the Gitea user it recorded (its id,
kept by mcp-manager with the Authentik pk it was made for). Any other Gitea
user of that name — a person's account, a site admin — is refused
(GiteaForeignUser) and never touched; one recorded for an older Authentik
account of the same name is purged (DELETE /admin/users/<name>?purge=true:
repositories, collaborations, team memberships, tokens, keys) before a fresh
one is made.

No token, password or response body is ever put into a message or a log:
every error names the call and its HTTP status only."""
from __future__ import annotations

import os
import secrets
import subprocess

TOKEN_PREFIX = "rzfz-gw-"
#: The largest page Gitea serves (setting.API.MaxResponseItems default).
_PAGE = 50
_MAX_PAGES = 200


class GiteaAdminError(RuntimeError):
    """Gitea did not do (or did not confirm) what was asked. The message
    names the call and its status, never a body."""


class GiteaForeignUser(GiteaAdminError):
    """A Gitea account of this name exists that the stack did not make for
    this technical user. It is never taken over."""

    def __init__(self, name):
        super().__init__(f"a Gitea account named {name} exists that the stack did not create for this "
                         "technical user; it is never taken over. Rename or delete that account in Gitea "
                         "(Site administration > User accounts), or give the technical user another name")


#: The gitea container (modules/gitea/compose.yml container_name).
GITEA_CONTAINER = "gitea"


def _docker_exec(argv):
    """`gitea admin user create ...` in the gitea container, through the
    socket proxy (DOCKER_HOST). The arguments carry no secret."""
    return subprocess.run(["docker", "exec", GITEA_CONTAINER, "su-exec", "git", "gitea", *argv],
                          capture_output=True, text=True, timeout=60, env=dict(os.environ, LC_ALL="C"))


class GiteaAdmin:
    def __init__(self, base="http://gitea:3000", admin_user=None, admin_password=None,
                 http=None, mail_domain=None, runner=None):
        self._base = base.rstrip("/")
        # None falls back to the environment; a value given (even "") is used as is.
        self._auth = (admin_user if admin_user is not None else os.environ.get("GITEA_ADMIN_USER", ""),
                      admin_password if admin_password is not None else os.environ.get("GITEA_ADMIN_PASSWORD", ""))
        self._mail = mail_domain or os.environ.get("MAIN_DOMAIN", "localhost")
        if http is None:
            import requests
            http = requests.Session()
            http.trust_env = False   # the admin password never goes to a proxy
        self._http = http
        self._run = runner or _docker_exec

    def _req(self, method, path, **kw):
        try:
            return self._http.request(method, f"{self._base}{path}", auth=self._auth,
                                      timeout=10, allow_redirects=False, **kw)
        except Exception as e:  # noqa: BLE001 — a transport error carries no secret, but say it plainly
            raise GiteaAdminError(f"{method} {path}: {type(e).__name__}") from None

    @staticmethod
    def _ok(r, method, path, ok):
        if r.status_code not in ok:
            raise GiteaAdminError(f"{method} {path}: HTTP {r.status_code}")
        return r

    def _patch_user(self, name, **fields):
        path = f"/api/v1/admin/users/{name}"
        # login_name and source_id are required by EditUserOption (a local user: source 0).
        return self._ok(self._req("PATCH", path, json={"login_name": name, "source_id": 0, **fields}),
                        "PATCH", path, (200,))

    def get_user(self, name: str):
        """The Gitea user of this name, or None ONLY on a 404."""
        path = f"/api/v1/users/{name}"
        r = self._ok(self._req("GET", path), "GET", path, (200, 404))
        if r.status_code == 404:
            return None
        try:
            u = r.json()
        except Exception:  # noqa: BLE001
            u = None
        if not isinstance(u, dict) or not isinstance(u.get("id"), int) or isinstance(u.get("id"), bool):
            raise GiteaAdminError(f"GET {path}: the answer is not a user")
        if str(u.get("login", name)).lower() != name.lower():
            raise GiteaAdminError(f"GET {path}: the answer is another user")
        return u

    def create_bot(self, name: str) -> None:
        r = self._run(["admin", "user", "create", "--username", name, "--email", f"{name}@noreply.{self._mail}",
                       "--user-type", "bot", "--restricted", "--fullname", f"{name} (technical user)"])
        if getattr(r, "returncode", 1) != 0:
            raise GiteaAdminError(f"gitea admin user create {name}: exit {getattr(r, 'returncode', '?')}")

    def ensure_service_user(self, name: str, owner: str, known_id=None) -> int:
        """The Gitea user id of `name`'s service user, made as a restricted
        bot if Gitea has no user of that name. An existing one is taken over
        ONLY when it is the user recorded for this Authentik account
        (`known_id`); any other raises GiteaForeignUser and is not touched.
        Then (also for the recorded one) every privilege is reset explicitly,
        the owner named and the login allowed again (a reassign re-enables
        the technical user)."""
        u = self.get_user(name)
        if u is None:
            self.create_bot(name)
            u = self.get_user(name)
            if u is None:
                raise GiteaAdminError(f"gitea admin user create {name}: the user is not there afterwards")
        elif known_id is None or u["id"] != known_id:
            raise GiteaForeignUser(name)
        r = self._patch_user(name, admin=False, restricted=True, prohibit_login=False, max_repo_creation=0,
                             allow_create_organization=False, allow_git_hook=False, allow_import_local=False,
                             visibility="private",
                             description=f"Technical user of the razzfazz.ai stack. Owner: {owner}")
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = None
        if isinstance(body, dict) and (body.get("is_admin") is True or body.get("restricted") is False):
            raise GiteaAdminError(f"PATCH /api/v1/admin/users/{name}: the privileges were not reset")
        return u["id"]

    def purge(self, name: str, expected_id: int) -> None:
        """Delete the Gitea user `expected_id` (an older Authentik account's
        service user) with everything it owns or was granted. A Gitea user of
        that name with another id is not ours and is left alone."""
        u = self.get_user(name)
        if u is None or u["id"] != expected_id:
            return
        path = f"/api/v1/admin/users/{name}"
        self._ok(self._req("DELETE", path, params={"purge": "true"}), "DELETE", path, (204, 404))
        if self.get_user(name) is not None:
            raise GiteaAdminError(f"DELETE {path}: the user is still there")

    def describe(self, name: str, owner: str) -> None:
        """Name a new owner (a reassign), changing nothing else."""
        self._patch_user(name, description=f"Technical user of the razzfazz.ai stack. Owner: {owner}")

    def mint_token(self, name: str, scopes: list) -> str:
        path = f"/api/v1/users/{name}/tokens"
        r = self._ok(self._req("POST", path, json={"name": f"{TOKEN_PREFIX}{secrets.token_hex(4)}",
                                                   "scopes": list(scopes)}), "POST", path, (201,))
        try:
            token = r.json()["sha1"]
        except Exception:  # noqa: BLE001 — never echo the body
            raise GiteaAdminError(f"POST {path}: no token in the answer") from None
        if not isinstance(token, str) or not token:
            raise GiteaAdminError(f"POST {path}: no token in the answer")
        return token

    def _our_tokens(self, name):
        """Every `rzfz-gw-*` token of `name`, from a COMPLETE listing; None
        when Gitea has no such user (404). Anything short of a complete
        listing raises: a revoke never decides on part of it."""
        path = f"/api/v1/users/{name}/tokens"
        out, seen, total = [], 0, None
        for page in range(1, _MAX_PAGES + 1):
            r = self._ok(self._req("GET", path, params={"page": page, "limit": _PAGE}), "GET", path, (200, 404))
            if r.status_code == 404:
                if page == 1:
                    return None
                raise GiteaAdminError(f"GET {path}: the user vanished while its tokens were read")
            try:
                rows = r.json()
            except Exception:  # noqa: BLE001
                rows = None
            if not isinstance(rows, list):
                raise GiteaAdminError(f"GET {path}: page {page} is not a token list")
            count = (getattr(r, "headers", None) or {}).get("X-Total-Count")
            if count is not None:
                if total is None:
                    total = int(count)
                elif int(count) != total:
                    raise GiteaAdminError(f"GET {path}: the token list changed while it was read")
            seen += len(rows)
            out.extend(t for t in rows if isinstance(t, dict) and str(t.get("name", "")).startswith(TOKEN_PREFIX))
            if len(rows) < _PAGE:
                if total is not None and seen != total:
                    raise GiteaAdminError(f"GET {path}: {seen} of {total} tokens read")
                return out
        raise GiteaAdminError(f"GET {path}: more than {_MAX_PAGES} pages")

    def delete_tokens(self, name: str) -> bool:
        """Delete every `rzfz-gw-*` token of `name`, each deletion confirmed
        by Gitea (204; 404 = already gone). False when Gitea has no such user."""
        tokens = self._our_tokens(name)
        if tokens is None:
            return False
        for t in tokens:
            tid = t.get("id")
            if not isinstance(tid, int) or isinstance(tid, bool):
                raise GiteaAdminError(f"GET /api/v1/users/{name}/tokens: a token without an id")
            path = f"/api/v1/users/{name}/tokens/{tid}"
            self._ok(self._req("DELETE", path), "DELETE", path, (204, 404))
        return True

    def revoke(self, name: str, expected_id=None) -> None:
        """Delete our tokens, then prohibit the login. Returns only when
        Gitea confirmed both (or has no such user at all); raises
        GiteaAdminError otherwise, so the caller keeps the revocation open.
        With `expected_id`, a Gitea user of that name with another id is not
        ours: it is left alone (a person's login is never prohibited), and
        our own user is gone, which is revoked enough."""
        if expected_id is not None:
            u = self.get_user(name)
            if u is None or u["id"] != expected_id:
                return
        if not self.delete_tokens(name):
            return
        path = f"/api/v1/admin/users/{name}"
        r = self._ok(self._req("PATCH", path, json={"login_name": name, "source_id": 0, "prohibit_login": True}),
                     "PATCH", path, (200, 404))
        if r.status_code == 200:
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = None
            if isinstance(body, dict) and body.get("prohibit_login") is False:
                raise GiteaAdminError(f"PATCH {path}: the login is still allowed")


def gitea_admin_for(env_value, profiles):
    """The portal's GiteaAdmin, or None where no Gitea service user can exist
    (a service user is made for the MCP gateway: mcp AND gitea must be on).
    `env_value(key)` reads the stack .env (never os.environ: the portal's
    own environment does not carry the Gitea admin)."""
    on = {p.strip() for p in profiles or () if isinstance(p, str)}
    if not {"gitea", "mcp"} <= on:
        return None
    return GiteaAdmin(admin_user=env_value("GITEA_ADMIN_USER") or "admin",
                      admin_password=env_value("GITEA_ADMIN_PASSWORD") or "",
                      mail_domain=env_value("MAIN_DOMAIN") or None)
