# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2476 D7/D9 — the Configuration Portal's OpenBao half.

`OpenBaoAdmin`: the technical users' AppRoles (Settings → Technical users and
the secret_id rotator), through `docker exec -i openbao sh -c <script>`.
`OpenBaoHTTP`: the deleted-user purge's KV and identity calls over
http://openbao:8200 (the Portal sits on openbao-net).

Both log in with the rzfz-portal AppRole from the openbao container's
read-only /openbao/cred mount: the Portal's everyday role, which can manage
svc-* AppRoles with the one policy `rzfz-app` and the fixed limits, and do the
purge — never write a policy or touch an auth mount (openbao-init.sh). The
root-equivalent rzfz-config role stays with the host (`rzfz post-install
--refresh`, `rzfz openbao admin-shell`).

A technical user's access is the templated policy rzfz-app: its AppRole reads
secret/apps/<its own role name>/* and nothing else, so there is no policy per
technical user. The scripts carry names and limits only; a secret_id comes
back on stdout, is shown once by the page and is never logged. Names are
checked again here (defence in depth: they are interpolated into a script).
"""
from __future__ import annotations

import re
import subprocess

from app.services.technical_users import APPROLE_LIMITS, NAME_RE, RESERVED

#: The Portal's AppRole credential files inside the openbao container.
PORTAL_ROLE_ID = "/openbao/cred/portal.role_id"
PORTAL_SECRET_ID = "/openbao/cred/portal.secret_id"
#: The one policy every technical user's AppRole carries (openbao-init.sh).
APP_POLICY = "rzfz-app"
#: The technical users' own AppRole mount. The stack's roles are on approle/,
#: which the Portal's policy does not reach (role names are case-insensitive,
#: ACL paths are not: the boundary is the mount, openbao-init.sh).
APP_MOUNT = "auth/approle-apps"
#: The one address a technical user's AppRole may be bound to: Caddy's fixed
#: address on openbao-net (CADDY_STACK_IPV4). The Portal's policy pins it.
CADDY_CIDR = "10.213.76.2/32"
EXEC_TIMEOUT = 60

LOGIN = ("set -eu\nexport BAO_ADDR=http://127.0.0.1:8200\n"
         f"BAO_TOKEN=$(bao write -field=token auth/approle/login role_id=@{PORTAL_ROLE_ID} "
         f"secret_id=@{PORTAL_SECRET_ID}); export BAO_TOKEN\n"
         "trap 'bao token revoke -self >/dev/null 2>&1 || true' EXIT\n")
# One `write secret-id` call, both fields read from its JSON (the value and its
# accessor belong to the same secret_id).
MINT = ("printf 'ROLE_ID\\t%s\\n' \"$(bao read -field=role_id auth/approle-apps/role/{n}/role-id)\"\n"
        "out=$(bao write -f -format=json auth/approle-apps/role/{n}/secret-id)\n"
        "printf 'SECRET_ID\\t%s\\n' \"$(printf '%s' \"$out\" | sed -n 's/.*\"secret_id\": *\"\\([^\"]*\\)\".*/\\1/p')\"\n"
        "printf 'SECRET_ID_ACCESSOR\\t%s\\n' \"$(printf '%s' \"$out\" | sed -n 's/.*\"secret_id_accessor\": *\"\\([^\"]*\\)\".*/\\1/p')\"\n")
_CIDR = re.compile(r"^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)/(3[0-2]|[12]?\d)$")
_TTL = re.compile(r"^\d{1,6}[smh]$")


class OpenBaoAdminError(RuntimeError):
    pass


class OpenBaoAdmin:
    def __init__(self, run=subprocess.run):
        self._run = run

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED:
            raise OpenBaoAdminError(f"refusing AppRole name {name!r}")
        return name

    def _exec(self, body):
        try:
            p = self._run(["docker", "exec", "-i", "openbao", "sh", "-c", LOGIN + body],
                          capture_output=True, text=True, timeout=EXEC_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise OpenBaoAdminError(f"openbao exec timed out after {EXEC_TIMEOUT} s") from None
        if p.returncode != 0:
            # stderr is bao's own message (a permission denied, a missing role);
            # a secret_id only ever travels on stdout
            raise OpenBaoAdminError(f"openbao exec failed (rc={p.returncode}): {(p.stderr or '').strip()[:200]}")
        return p.stdout

    @staticmethod
    def _cidr(cidr):
        if not isinstance(cidr, str) or not _CIDR.match(cidr):
            raise OpenBaoAdminError(f"refusing CIDR {cidr!r}")
        return cidr

    @staticmethod
    def _limits(limits):
        for k in ("token_ttl", "token_max_ttl", "secret_id_ttl"):
            if not _TTL.match(str(limits[k])):
                raise OpenBaoAdminError(f"refusing {k} {limits[k]!r}")
        return int(limits["secret_id_num_uses"])

    @staticmethod
    def _parse(out, limits):
        kv = dict(l.split("\t", 1) for l in out.splitlines() if "\t" in l)
        if not kv.get("ROLE_ID") or not kv.get("SECRET_ID") or not kv.get("SECRET_ID_ACCESSOR"):
            raise OpenBaoAdminError("openbao returned no role_id/secret_id")
        return {"role_id": kv["ROLE_ID"], "secret_id": kv["SECRET_ID"], "secret_id_accessor": kv["SECRET_ID_ACCESSOR"],
                "secret_id_ttl": limits["secret_id_ttl"], "secret_id_num_uses": limits["secret_id_num_uses"]}

    def ensure_approle(self, name, policy_paths, limits):
        """D7: read-only on secret/apps/<name>/* (the templated rzfz-app
        policy); 5-minute tokens; a 24 h / 1000-use secret_id; token and
        secret_id bound to Caddy's address. Any other path is refused: the
        Portal cannot write a policy, so it cannot grant one."""
        n = self._name(name)
        if list(policy_paths or ()) != [f"secret/data/apps/{n}/*"]:
            raise OpenBaoAdminError(f"{n}: a technical user reads secret/data/apps/{n}/* only")
        cidr = self._cidr(limits["secret_id_bound_cidrs"])
        if self._cidr(limits["token_bound_cidrs"]) != cidr:
            raise OpenBaoAdminError(f"{n}: token and secret_id must be bound to the same address")
        return self._parse(self._exec(self._role_write(n, limits, cidr) + MINT.format(n=n)), limits)

    def _role_write(self, n, limits, cidr):
        """The role with EVERY limit: the Portal's policy requires them all on
        each write (a role is never recreated looser)."""
        uses = self._limits(limits)
        return (f"bao write {APP_MOUNT}/role/{n} token_policies={APP_POLICY} token_ttl={limits['token_ttl']} "
                f"token_max_ttl={limits['token_max_ttl']} secret_id_ttl={limits['secret_id_ttl']} "
                f"secret_id_num_uses={uses} "
                f"token_bound_cidrs={cidr} secret_id_bound_cidrs={cidr} >/dev/null\n")

    def rotate_secret_id(self, name):
        """One new secret_id. Older ones are NOT destroyed: they expire by their
        own 24 h TTL, so a Dify app the rotator skipped keeps working (D7).
        Revoking the technical user (delete_approle) removes them all."""
        n = self._name(name)
        return self._parse(self._exec(MINT.format(n=n)), APPROLE_LIMITS)

    def set_bound_cidrs(self, name, cidr):
        """Re-binds the role (with all its limits). OpenBao accepts only
        Caddy's fixed address from the Portal's role."""
        n, c = self._name(name), self._cidr(cidr)
        self._exec(self._role_write(n, APPROLE_LIMITS, c))

    def secret_id_uses_left(self, name, accessor):
        """Uses left of one secret_id; None for an unknown, expired or
        malformed accessor (advisory: never decides a rotation)."""
        n = self._name(name)
        if not isinstance(accessor, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", accessor):
            return None
        out = self._exec(f"bao write -field=secret_id_num_uses {APP_MOUNT}/role/{n}/secret-id-accessor/lookup "
                         f"secret_id_accessor={accessor} 2>/dev/null || true\n")
        return int(out.strip()) if out.strip().isdigit() else None

    def delete_approle(self, name):
        """The AppRole and with it every secret_id it issued (tokens already
        issued die within their 5 minutes)."""
        n = self._name(name)
        self._exec(f"bao delete {APP_MOUNT}/role/{n} >/dev/null\n")


# --- the purge reconcile's client (D9, ruling 12) -----------------------------

_SUB = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_STAMP = re.compile(r"^\d{8}T\d{6}Z$")
_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
#: The auth mounts whose alias names are people's OIDC subs (A21's token-role
#: tokens carry the sub as their token-mount alias).
PERSON_MOUNTS = ("auth/oidc/", "auth/token/")


def _token_via_exec(run=subprocess.run) -> str:
    """A short-lived rzfz-portal token; its credentials never leave the
    openbao container's read-only /openbao/cred mount."""
    try:
        p = run(["docker", "exec", "openbao", "sh", "-c",
                 "BAO_ADDR=http://127.0.0.1:8200 bao write -field=token auth/approle/login "
                 f"role_id=@{PORTAL_ROLE_ID} secret_id=@{PORTAL_SECRET_ID}"],
                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise OpenBaoAdminError("rzfz-portal login timed out") from None
    if p.returncode != 0 or not p.stdout.strip():
        raise OpenBaoAdminError("rzfz-portal login failed")
    return p.stdout.strip()


class OpenBaoHTTP:
    """KV v2 and identity calls for the purge reconcile (D9, ruling 12)."""

    def __init__(self, token_source=_token_via_exec, addr="http://openbao:8200", http=None):
        if http is None:
            import requests
            http = requests.Session()
            http.trust_env = False
        self._token_source, self._addr, self._http, self._token = token_source, addr.rstrip("/"), http, None

    def _send(self, method, path, body):
        return self._http.request(method, f"{self._addr}/v1/{path}", json=body, timeout=15,
                                  allow_redirects=False, headers={"X-Vault-Token": self._token})

    def _req(self, method, path, body=None, ok=(200, 204)):
        fresh = self._token is None
        if fresh:
            self._token = self._token_source()
        try:
            r = self._send(method, path, body)
            if r.status_code == 403 and not fresh:
                # the cached 15-minute token expired: log in again, once
                self._token = self._token_source()
                r = self._send(method, path, body)
        except OpenBaoAdminError:
            raise
        except Exception as e:  # noqa: BLE001 — requests' errors; never the token
            raise OpenBaoAdminError(f"openbao {method} {path.split('/', 1)[0]}/…: {type(e).__name__}") from None
        if r.status_code == 404 and method in ("LIST", "GET"):
            return {}
        if r.status_code not in ok:
            raise OpenBaoAdminError(f"openbao {method} {path.split('/', 1)[0]}/… refused ({r.status_code})")
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            raise OpenBaoAdminError(f"openbao {method} {path.split('/', 1)[0]}/…: not JSON") from None

    def _keys(self, prefix):
        out = []
        for k in self._req("LIST", f"secret/metadata/{prefix}").get("data", {}).get("keys", []):
            out += [k + sub for sub in self._keys(prefix + k)] if k.endswith("/") else [k]
        return out

    def person_aliases(self) -> dict:
        """{sub: {"entity_id", "pk"}} for every alias on the OIDC and token
        mounts (by the alias's own mount_path: the Portal cannot read
        sys/auth). `pk` is the Authentik pk the OIDC login copied into the
        alias metadata (ak_pk), or None: the purge confirms a deletion by
        looking that pk up, and never purges without it."""
        # #2476 A21: an agent token's alias (token mount, alias = the owner's
        # sub) belongs to an entity of its own next to the OIDC one, so a sub
        # can have several. All of them are collected (`entity_ids`); the
        # primary `entity_id` and the pk come from the OIDC alias when there
        # is one, whatever order the listing has.
        out, oidc = {}, set()
        for aid in self._req("LIST", "identity/entity-alias/id").get("data", {}).get("keys", []):
            a = self._req("GET", f"identity/entity-alias/id/{aid}").get("data") or {}
            if a.get("mount_path") in PERSON_MOUNTS and _SUB.match(a.get("name") or "") \
                    and _ID.match(a.get("canonical_id") or ""):
                sub, eid = a["name"], a["canonical_id"]
                pk = (a.get("metadata") or {}).get("ak_pk")
                rec = out.setdefault(sub, {"entity_id": eid, "entity_ids": [], "pk": None})
                if eid not in rec["entity_ids"]:
                    rec["entity_ids"].append(eid)
                if a.get("mount_path") == "auth/oidc/" and sub not in oidc:
                    oidc.add(sub)
                    rec["entity_id"] = eid
                if isinstance(pk, str) and pk.isdigit():
                    rec["pk"] = pk
        for rec in out.values():
            rec["entity_ids"] = [rec["entity_id"]] + sorted(e for e in rec["entity_ids"] if e != rec["entity_id"])
        return out

    def soft_delete_user(self, sub, entity_id, stamp, entity_ids=None) -> int:
        """Copy every secret to purged/ FIRST; then stop the user's tokens
        (every entity of the person disabled, their accessors revoked); only
        then delete the originals and the entities. A failed read or copy
        raises before anything is deleted. Returns the number of secrets
        moved. `entity_ids`: all of the person's entities (person_aliases);
        `entity_id` alone for a caller that knows one."""
        ids = list(dict.fromkeys([entity_id, *(entity_ids or [])]))
        if not _SUB.match(sub) or not _STAMP.match(stamp) or not all(_ID.match(e or "") for e in ids):
            raise OpenBaoAdminError("refusing an unsafe purge path")
        keys = self._keys(f"users/{sub}/")
        docs = {k: (self._req("GET", f"secret/data/users/{sub}/{k}").get("data") or {}).get("data") for k in keys}
        for k, doc in docs.items():
            if doc is None:
                continue      # 404: its current version was deleted in OpenBao; only metadata is left
            self._req("POST", f"secret/data/purged/{sub}/{stamp}/{k}", {"data": doc})
        for eid in ids:
            self._req("POST", f"identity/entity/id/{eid}", {"disabled": True})
        for acc in self._req("LIST", "auth/token/accessors").get("data", {}).get("keys", []):
            look = self._req("POST", "auth/token/lookup-accessor", {"accessor": acc}, ok=(200, 400, 403))
            if (look.get("data") or {}).get("entity_id") in ids:
                self._req("POST", "auth/token/revoke-accessor", {"accessor": acc})
        for k in keys:
            self._req("DELETE", f"secret/metadata/users/{sub}/{k}")
        for eid in ids:
            self._req("DELETE", f"identity/entity/id/{eid}")
        return sum(1 for doc in docs.values() if doc is not None)

    def list_purged(self):
        out = []
        for s in self._req("LIST", "secret/metadata/purged/").get("data", {}).get("keys", []):
            for st in self._req("LIST", f"secret/metadata/purged/{s}").get("data", {}).get("keys", []):
                if _SUB.match(s.rstrip("/")) and _STAMP.match(st.rstrip("/")):
                    out.append((s.rstrip("/"), st.rstrip("/")))
        return out

    def destroy_purged(self, sub, stamp) -> None:
        if not _SUB.match(sub) or not _STAMP.match(stamp):
            raise OpenBaoAdminError("refusing an unsafe purge path")
        for k in self._keys(f"purged/{sub}/{stamp}/"):
            self._req("DELETE", f"secret/metadata/purged/{sub}/{stamp}/{k}")
