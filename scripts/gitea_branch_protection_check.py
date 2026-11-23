#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2554 — does every repo an agent can write to require a review on its default branch?

The MCP gateway's Gitea guard keeps agents off a repo's default branch: they
write to a branch and open a pull request. A person's agent may also MERGE
that pull request (the person is allowed to; the guard refuses a merge only
to technical users). What then stops an agent from merging its own change is
Gitea's branch protection on the default branch with at least one required
approval. This check lists the repos where that protection is missing.

Which repos: every organisation repository that is not archived, not a
mirror and not empty. A person's agent writes with that person's Gitea
rights, so any organisation member with write access makes a repo writable
by an agent; a personal repository belongs to its owner, who may commit to
it directly. A technical user is bound to organisation repos as well.

Calls (GET only, through Gitea's API as the stack's Gitea admin): one probe
(`/version`, short timeout: a Gitea that does not answer is skipped at once),
the organisation and repository listings (paged, 50 per page), then ONE call
per repository, `/repos/{owner}/{repo}/branches/{default branch}`, whose
`protected` and `required_approvals` are Gitea's own resolution of every
matching protection rule (glob rules included). At most MAX_REPOS repos and
DEADLINE seconds; a check cut short says how far it got.

Input: GITEA_URL (e.g. http://127.0.0.1:3000) in the environment; the admin
user name and password on stdin, one per line (never on a command line).
Output: lines `VERDICT|text`, VERDICT in PASS WARN SKIP INFO."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_REPOS = int(os.environ.get("MAX_REPOS", "200"))
DEADLINE = float(os.environ.get("DEADLINE", "30"))
PROBE_TIMEOUT = 3.0
CALL_TIMEOUT = 5.0
PAGE = 50
NAMES_SHOWN = 10


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get(base, auth, path, timeout):
    req = urllib.request.Request(base + "/api/v1" + path, method="GET",
                                 headers={"Authorization": auth, "Accept": "application/json"})
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read(8 * 1024 * 1024) or b"null")


def _paged(base, auth, path, stop_at):
    out, page = [], 1
    while time.monotonic() < stop_at:
        sep = "&" if "?" in path else "?"
        items = _get(base, auth, f"{path}{sep}limit={PAGE}&page={page}", CALL_TIMEOUT)
        if isinstance(items, dict) and "data" in items:   # /repos/search: {"ok", "data"}
            items = items["data"]
        if not isinstance(items, list):
            raise ValueError(f"{path} did not return a list")
        out.extend(items)
        if len(items) < PAGE:
            return out, True
        page += 1
    return out, False


def check(base, user, password, out=print) -> int:
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    stop_at = time.monotonic() + DEADLINE
    try:
        _get(base, auth, "/version", PROBE_TIMEOUT)
    except urllib.error.HTTPError as e:
        out(f"SKIP|Gitea review requirement: the admin login was refused (HTTP {e.code}); "
            "check GITEA_ADMIN_USER/GITEA_ADMIN_PASSWORD")
        return 0
    except Exception:  # noqa: BLE001 — not answering: skip at once
        out("SKIP|Gitea review requirement: Gitea is not answering")
        return 0
    try:
        orgs, _ = _paged(base, auth, "/admin/orgs", stop_at)
        org_names = {str(o.get("username") or o.get("name") or "").lower()
                     for o in orgs if isinstance(o, dict)}
        repos, complete = _paged(base, auth, "/repos/search?sort=alpha&order=asc", stop_at)
    except urllib.error.HTTPError as e:
        out(f"SKIP|Gitea review requirement: Gitea refused the repository listing (HTTP {e.code})")
        return 0
    except Exception as e:  # noqa: BLE001
        out(f"SKIP|Gitea review requirement: the repository listing failed ({type(e).__name__})")
        return 0
    targets = []
    for r in repos:
        if not isinstance(r, dict) or r.get("archived") or r.get("mirror") or r.get("empty"):
            continue
        owner = str((r.get("owner") or {}).get("login") or "")
        if owner.lower() not in org_names or not r.get("name") or not r.get("default_branch"):
            continue
        targets.append((owner, str(r["name"]), str(r["default_branch"])))
    missing, checked, unreadable = [], 0, 0
    for owner, name, branch in targets[:MAX_REPOS]:
        if time.monotonic() >= stop_at:
            complete = False
            break
        path = "/repos/{}/{}/branches/{}".format(*(urllib.parse.quote(x, safe="")
                                                     for x in (owner, name, branch)))
        try:
            b = _get(base, auth, path, CALL_TIMEOUT)
        except Exception:  # noqa: BLE001
            unreadable += 1
            continue
        checked += 1
        approvals = b.get("required_approvals") if isinstance(b, dict) else None
        if not (isinstance(b, dict) and b.get("protected") is True
                and isinstance(approvals, int) and approvals >= 1):
            why = "unprotected" if not (isinstance(b, dict) and b.get("protected")) else "0 approvals"
            missing.append(f"{owner}/{name} ({branch}: {why})")
    if len(targets) > MAX_REPOS:
        complete = False
    scope = f"{checked} of {len(targets)} organisation repos checked" if not complete or unreadable \
        else f"{checked} organisation repos"
    if missing:
        shown = ", ".join(missing[:NAMES_SHOWN])
        more = f" and {len(missing) - NAMES_SHOWN} more" if len(missing) > NAMES_SHOWN else ""
        out(f"WARN|Gitea: agents can merge without review in {len(missing)} repo(s): {shown}{more} "
            f"- protect the default branch with at least 1 required approval ({scope})")
    elif checked:
        out(f"PASS|Gitea: every default branch requires at least 1 approval ({scope})")
    elif targets:
        out(f"SKIP|Gitea review requirement: no repository could be read ({scope})")
    else:
        out("INFO|Gitea: no organisation repositories to check")
    if unreadable and missing == [] and checked:
        out(f"INFO|Gitea review requirement: {unreadable} repo(s) could not be read")
    return 0


def main() -> int:
    base = (os.environ.get("GITEA_URL") or "").rstrip("/")
    lines = sys.stdin.read().split("\n")
    user = lines[0] if lines else ""
    password = lines[1] if len(lines) > 1 else ""
    if not base or not user or not password:
        print("SKIP|Gitea review requirement: no Gitea admin login in .env")
        return 0
    return check(base, user, password)


if __name__ == "__main__":
    sys.exit(main())
