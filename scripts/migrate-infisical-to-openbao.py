#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2476 — copy Infisical project secrets into OpenBao team paths.

Each Infisical project becomes one team tree, each environment one KV v2
document: `secret/teams/<project-slug>/<env-slug>`, holding that environment's
keys as fields. Nothing else is ever written: `secret/users/<sub>/` belongs to
one person and `secret/apps/<svc-name>/` to one technical user (A1 D7, D9). A
team path is readable by the members of the Authentik group whose slug is the
same and which carries `rzfz_bao_team`, and by admins.

The default is a dry run: it lists the paths and key NAMES it would write and
prints no value. `--apply` streams value by value (read from Infisical, write
to OpenBao, drop), never to a file, and never overwrites a key that already
exists in OpenBao unless `--overwrite` is given. Writes use KV v2
check-and-set, so a document changed by someone else in between is not
clobbered.

Tokens are read from stdin, never from the command line: line 1 is an Infisical
machine-identity access token, line 2 (only with `--apply`) an OpenBao token
whose policy may write `secret/data/teams/*`. Only the root secret path (`/`)
of each environment is copied; folders are listed by Infisical separately and
are not migrated.

    printf '%s\\n' "$INF_TOKEN" | migrate-infisical-to-openbao.py
    printf '%s\\n%s\\n' "$INF_TOKEN" "$BAO_TOKEN" | migrate-infisical-to-openbao.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TEAMS = "secret/teams/"


class MigrationError(Exception):
    """A refused or failed step. Its message never carries a secret value."""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def plan_migration(projects: list[dict]) -> list[dict]:
    """Paths and key NAMES only — the plan is printable and carries no value.

    Every path is secret/teams/<slug>/<env> (A1 D9); a project whose name slugs
    to nothing is refused rather than written one level up, and two projects
    (or two environments) that slug alike are refused rather than merged."""
    out, seen = [], {}
    for p in projects:
        slug = _slug(p["slug"])
        if not slug:
            raise ValueError(f"project name {p['slug']!r} gives no usable team path")
        if slug in seen:
            raise ValueError(f"projects {seen[slug]!r} and {p['slug']!r} both map to {TEAMS}{slug}/")
        seen[slug] = p["slug"]
        envs = {}
        for env in p["environments"]:
            e = _slug(env) or "default"
            if e in envs:
                raise ValueError(f"environments {envs[e]!r} and {env!r} of {p['slug']!r} both map to {e!r}")
            envs[e] = env
        for e in sorted(envs):
            out.append({"path": f"{TEAMS}{slug}/{e}", "keys": sorted(p["environments"][envs[e]])})
    return out


def migrate(infisical, openbao, plan, projects, overwrite=False):
    """Value by value, in memory: read -> write -> drop. Never a file.

    `projects` maps a plan slug to whatever `infisical.get` takes as its project
    argument. An existing OpenBao key is skipped before its value is read."""
    written = skipped = 0
    for item in plan:
        _, _, slug, env = item["path"].split("/", 3)
        for key in item["keys"]:
            if not overwrite and openbao.exists(item["path"], key):
                skipped += 1
                continue
            value = infisical.get(projects[slug], env, key)
            openbao.put(item["path"], key, value)
            del value
            written += 1
    return {"written": written, "skipped_existing": skipped}


# --- transports ----------------------------------------------------------------

class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """A token header must never follow a redirect to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None   # urllib then raises HTTPError with the 3xx code


#: No proxy from the environment (the tokens travel in headers) and no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect())


def _urllib(method, url, headers, body):
    req = urllib.request.Request(url, data=body.encode() if body is not None else None,
                                 headers=headers, method=method)
    where = f"{method} {url.split('?', 1)[0]}"
    try:
        with _OPENER.open(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise MigrationError(f"{where}: HTTP {e.code} redirect refused (a token never follows a redirect)") from None
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        raise MigrationError(f"{where}: {e.__class__.__name__}") from None


def _errors(doc) -> str:
    """Server error strings only; never a request body."""
    if not isinstance(doc, dict):
        return ""
    errs = doc.get("errors") or doc.get("message") or ""
    return "; ".join(errs) if isinstance(errs, list) else str(errs)


class InfisicalAPI:
    """Infisical REST, read only, with a machine-identity access token."""

    def __init__(self, url: str, token: str, http=_urllib):
        self.url, self._token, self._http = url.rstrip("/"), token, http
        self._env_map = {}

    def _get(self, path, **query):
        qs = urllib.parse.urlencode(query)
        status, doc = self._http("GET", f"{self.url}{path}{'?' + qs if qs else ''}",
                                 {"Authorization": f"Bearer {self._token}"}, None)
        if status != 200:
            raise MigrationError(f"Infisical GET {path}: HTTP {status} {_errors(doc)}".rstrip())
        return doc

    def projects(self) -> list[dict]:
        out = []
        for w in self._get("/api/v1/workspace").get("workspaces", []):
            envs = [e["slug"] for e in w.get("environments", [])]
            out.append({"id": w["id"], "name": w["name"], "environments": envs})
        return out

    def keys(self, workspace_id: str, env: str) -> list[str]:
        doc = self._get("/api/v3/secrets/raw", workspaceId=workspace_id, environment=env, secretPath="/")
        return sorted({s["secretKey"] for s in doc.get("secrets", [])})

    def get(self, project, env, key):
        workspace_id, env_map = project
        doc = self._get(f"/api/v3/secrets/raw/{urllib.parse.quote(key, safe='')}",
                        workspaceId=workspace_id, environment=env_map[env], secretPath="/")
        return doc["secret"]["secretValue"]


class OpenBaoKV:
    """OpenBao KV v2 on the `secret/` mount, limited to secret/teams/*.

    One document per path; a key is a field. A new document is created with
    cas=0 (fails if someone created it meanwhile); a field is added with a
    merge-patch at the version just read (fails if it changed meanwhile)."""

    def __init__(self, url: str, token: str, http=_urllib):
        self.url, self._token, self._http = url.rstrip("/"), token, http

    @staticmethod
    def _kv(path: str) -> str:
        parts = path.split("/")
        if (not path.startswith(TEAMS) or len(parts) != 4
                or any(p in ("", ".", "..") for p in parts) or any(_slug(p) != p for p in parts[2:])):
            raise MigrationError(f"refusing {path!r}: only {TEAMS}<team>/<env> is written")
        return path.removeprefix("secret/")

    def _call(self, method, path, body=None, content_type="application/json"):
        headers = {"X-Vault-Token": self._token}
        if body is not None:
            headers["Content-Type"] = content_type
        return self._http(method, f"{self.url}/v1/secret/data/{path}", headers,
                          json.dumps(body) if body is not None else None)

    def _read(self, path):
        status, doc = self._call("GET", self._kv(path))
        if status == 404:
            return 0, {}
        if status != 200:
            raise MigrationError(f"OpenBao read {path}: HTTP {status} {_errors(doc)}".rstrip())
        d = doc.get("data") or {}
        return (d.get("metadata") or {}).get("version", 0), (d.get("data") or {})

    def exists(self, path, key) -> bool:
        _, data = self._read(path)
        return key in data

    def put(self, path, key, value) -> None:
        version, _ = self._read(path)
        body = {"data": {key: value}, "options": {"cas": version}}
        if version == 0:
            status, doc = self._call("POST", self._kv(path), body)
        else:
            status, doc = self._call("PATCH", self._kv(path), body, "application/merge-patch+json")
        del body
        if status not in (200, 204):
            raise MigrationError(f"OpenBao write {path} key {key}: HTTP {status} {_errors(doc)}".rstrip())


# --- CLI -----------------------------------------------------------------------

def _read_tokens(n: int) -> list[str]:
    lines = sys.stdin.read().splitlines()
    tokens = [l.strip() for l in lines[:n]]
    if len(tokens) < n or not all(tokens):
        need = "an Infisical token" + (" and an OpenBao token, one per line," if n == 2 else "")
        raise MigrationError(f"stdin must carry {need} (never pass a token as an argument)")
    return tokens


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Copy Infisical project secrets into OpenBao team paths "
                                             "(dry run unless --apply). Tokens are read from stdin.")
    ap.add_argument("--infisical-url", default="http://infisical:8080")
    ap.add_argument("--openbao-url", default="http://openbao:8200")
    ap.add_argument("--apply", action="store_true", help="write to OpenBao (default: dry run)")
    ap.add_argument("--overwrite", action="store_true", help="also replace keys that already exist in OpenBao")
    args = ap.parse_args(argv)
    try:
        tokens = _read_tokens(2 if args.apply else 1)
        inf = InfisicalAPI(args.infisical_url, tokens[0])
        projects, refs = [], {}
        for p in inf.projects():
            envs = {env: dict.fromkeys(inf.keys(p["id"], env)) for env in p["environments"]}
            projects.append({"slug": p["name"], "environments": envs})
            refs[_slug(p["name"])] = (p["id"], {(_slug(e) or "default"): e for e in p["environments"]})
        plan = plan_migration(projects)
        if not args.apply:
            print("Dry run (nothing written). Would write:")
            for item in plan:
                print(f"  {item['path']}: {len(item['keys'])} key(s): {', '.join(item['keys'])}")
            print(json.dumps({"paths": len(plan), "keys": sum(len(i["keys"]) for i in plan)}))
            return 0
        bao = OpenBaoKV(args.openbao_url, tokens[1])
        del tokens
        print(json.dumps(migrate(inf, bao, plan, refs, overwrite=args.overwrite)))
        return 0
    except (MigrationError, ValueError) as e:
        print(f"migrate-infisical-to-openbao: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
