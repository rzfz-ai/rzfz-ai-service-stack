# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2473 D5 — plan app-role changes for the apps without claim mapping
(Dify, cognee, Synapse), and keep the ledger of what the reconcile did.

`plan_report` is pure: members (Authentik group -> account keys) + the app's
current accounts + the ledger -> actions. The account key is the e-mail for
Dify and cognee, and the OIDC `sub` (Synapse's `user_external_ids`) for
Synapse. Called by cli/provision-sso-users.sh --reconcile-roles through
`python -m app.services.downstream_roles` (JSON on stdin, JSON on stdout).

The privilege rules (A12, review round 1):

* **Never widen past 2026.09-ga.4.** A role is only ever RAISED, and only to
  the role the catalogue explicitly maps the user to.
* **Access removal follows the module group (D5).** An account whose owner
  left the module-access group is disabled (Dify banned, cognee inactive,
  Synapse admin dropped) whatever the ledger says.
* **Role lowering only for what the reconcile set.** Lowering or a lateral
  change of a role happens only when the ledger records that the reconcile set
  exactly that role. A hand-granted role is HELD and reported as a warning.
* **Re-enable only what the reconcile disabled.** A ban set by hand is never
  lifted (HELD, reported).
* **Never the last admin.** A removal that would leave the app without an
  active admin is REFUSED and reported. The stack's own identity counts as an
  admin: it is the box's break-glass account (its password is in .env).
* **Protected accounts** (ownership, the stack identities) are never changed.
* **mcp-manager's cognee accounts** (the per-user memory accounts, see
  razzfazz_common.cognee_accounts) are never members of an Authentik group and
  belong to mcp-manager: never disabled, never re-roled, never counted. The
  one exception repairs earlier releases, which disabled them: one the ledger
  records as disabled by the reconcile is enabled again (and its ledger entry
  goes). One disabled by hand or by anything else stays disabled.
* **Dify accounts that are pending, uninitialized or closed** are not the
  reconcile's business: no action, no drift.
* An unusable catalogue raises CatalogueError (exit 3), an unusable ledger is
  exit 4; the caller then changes nothing.

Dify limitation: a role is written to every non-owner workspace join of the
account, and drift is read from the account's highest join. Dify CE runs one
workspace, so both are the same join there.
"""
from __future__ import annotations

import json
import os
import sys

from razzfazz_common.cognee_accounts import is_mcp_account
from razzfazz_common.rbac import Catalogue, CatalogueError

#: roles an app treats as ownership — never changed by a reconcile
PROTECTED = {"owner"}

#: Per app: the rank of every app role this planner may read or write (higher
#: is more privilege; equal ranks are lateral), the admin roles (for the
#: last-admin guard), and whether "disable" is a real account state or only
#: dropping to the default role (Synapse: `deactivated` is never used, D5).
APPS = {
    "dify": {"rank": {"normal": 0, "dataset_operator": 0, "editor": 1, "admin": 2, "owner": 3},
             "admin": {"admin", "owner"}, "disable_is_role_drop": False},
    "cognee": {"rank": {"user": 0, "superuser": 1}, "admin": {"superuser"}, "disable_is_role_drop": False},
    "synapse": {"rank": {"user": 0, "admin": 1}, "admin": {"admin"}, "disable_is_role_drop": True},
}

#: where the ledger lives: razzfazz-config's data volume, which the backup archives
LEDGER_ENV = "RZFZ_DOWNSTREAM_LEDGER"
LEDGER_DEFAULT = "/data/rbac/downstream-roles.json"


class LedgerError(RuntimeError):
    """The ledger exists but cannot be read or is malformed."""


def _spec(app: str, cat: Catalogue) -> dict:
    """The app's table, checked against the app's role vocabulary. Anything
    the planner cannot read with certainty is a CatalogueError (fail closed)."""
    if app not in APPS:
        raise CatalogueError(f"no downstream planner for app {app!r}")
    if app not in cat.downstream or app not in cat.module_access:
        raise CatalogueError(f"the role catalogue declares no downstream roles for {app!r}")
    spec = APPS[app]
    table = cat.downstream[app]
    for role, app_role in table.items():
        if app_role not in spec["rank"]:
            raise CatalogueError(f"downstream.{app}.{role}: {app_role!r} is not a {app} role "
                                 f"(known: {sorted(spec['rank'])})")
        if app_role in PROTECTED:
            raise CatalogueError(f"downstream.{app}.{role}: {app_role!r} is ownership, never granted by a reconcile")
        if role == "*" and app_role in spec["admin"]:
            raise CatalogueError(f"downstream.{app}.\"*\": the default role may not be an admin role ({app_role!r})")
        if role != "*":
            cat.role_groups(role)  # unknown role -> CatalogueError
    return spec


def needed_groups(app: str, cat: Catalogue) -> list[str]:
    """Every Authentik group whose members the plan reads, sorted."""
    _spec(app, cat)
    out = {cat.module_access[app]} | set(cat.role_groups("platform-admin"))
    for role in cat.downstream[app]:
        if role != "*":
            out |= cat.role_groups(role)
    return sorted(out)


def _norm(key) -> str:
    return str(key).strip().lower()


def _lower_members(members: dict) -> dict[str, set[str]]:
    return {g: {_norm(e) for e in v if isinstance(e, str) and e.strip()} for g, v in members.items()}


def _wanted_role(key_l: str, members: dict, cat: Catalogue, app: str) -> str | None:
    table = cat.downstream[app]
    for role, app_role in table.items():
        if role == "*":
            continue
        if any(key_l in members.get(g, set()) for g in cat.role_groups(role)):
            return app_role
    return table.get("*")


def _state(acc: dict) -> str:
    """active | disabled | other. Dify passes its real status; anything but
    active/banned (pending, uninitialized, closed) is `other`: not ours."""
    if "status" in acc:
        return {"active": "active", "banned": "disabled"}.get(acc.get("status"), "other")
    return "active" if acc.get("active", True) else "disabled"


def _ledger_entry(ledger: dict, key_l: str) -> dict:
    v = ledger.get(key_l)
    if isinstance(v, str):          # the first A12 format: {key: role}
        return {"role": v}
    return v if isinstance(v, dict) else {}


def plan_report(app: str, members: dict, current: dict, cat: Catalogue, *,
                managed: dict | None = None, service_accounts=(), ignored_accounts=(),
                service_localparts=(), conflicts=(), mcp_domain: str = "") -> dict:
    """The full plan: `actions` to apply, `held` differences left alone
    (kind `role`: a hand-granted role; kind `ban`: a hand ban; kind
    `conflict`: one app account linked to several identities) and `refused`
    removals (last admin). `managed` is the app's ledger:
    {key: {"role": set-by-reconcile, "disabled": true}}. `service_accounts`
    (keys) and `service_localparts` (Synapse: the account's user id
    `@<localpart>:<server>`) are never touched but count as admins;
    `ignored_accounts` are never touched and never count. `conflicts` are
    accounts the planner must not touch at all (Synapse: one user id linked to
    several OIDC subs — which one is current is the operator's call); they are
    HELD and still count for the last-admin guard. `mcp_domain` (cognee: the
    box's MAIN_DOMAIN) names where mcp-manager's own cognee accounts live;
    those are left to mcp-manager (see the module docstring).

    The last-admin guard counts ACCOUNTS: an entry's `user` (Synapse's
    users.name) when present, else its key."""
    spec = _spec(app, cat)
    rank = spec["rank"]
    default = cat.downstream[app].get("*")
    members = _lower_members(members)
    access = members.get(cat.module_access[app], set())
    admins = set()
    for g in cat.role_groups("platform-admin"):
        admins |= members.get(g, set())
    ledger = {_norm(k): v for k, v in (managed or {}).items()}
    skip = {_norm(s) for s in service_accounts}
    ignored = {_norm(s) for s in ignored_accounts}
    svc_lp = {"@" + _norm(lp) for lp in service_localparts if str(lp).strip()}

    def mcp_owned(key: str) -> bool:
        return app == "cognee" and is_mcp_account(key, mcp_domain)

    def account_of(key: str, acc: dict) -> str:
        return _norm(acc.get("user") or key)

    def is_service(key_l: str, acc: dict) -> bool:
        return key_l in skip or (bool(svc_lp) and _norm(acc.get("user") or "").split(":")[0] in svc_lp)

    # One account linked under several keys (Synapse: the same users.name under
    # two subs) is a conflict whatever the caller already collapsed: the
    # planner never acts on either key.
    by_user: dict = {}
    for k, a in current.items():
        if a.get("user"):
            by_user.setdefault(_norm(a["user"]), []).append(k)
    conflicts = list(conflicts)
    for u, keys in by_user.items():
        if len(keys) > 1:
            a = current[keys[0]]
            conflicts.append({"user": current[keys[0]]["user"], "subs": sorted(keys), "role": a.get("role"),
                              "active": a.get("active", True)})
    conflict_users = {_norm(c.get("user")) for c in conflicts}
    current = {k: a for k, a in current.items() if _norm(a.get("user") or "\0") not in conflict_users}

    # Active admins now, one per account: the last-admin guard counts down as
    # removals are planned.
    live_admins = {account_of(k, a) for k, a in current.items()
                   if a.get("role") in spec["admin"] and _state(a) == "active" and _norm(k) not in ignored
                   and not mcp_owned(k)}
    live_admins |= {_norm(c.get("user")) for c in conflicts
                    if c.get("role") in spec["admin"] and _state(c) == "active"}

    acts: list[dict] = []
    held: list[dict] = []
    refused: list[dict] = []

    for c in conflicts:
        held.append({"email": c.get("user"), "role": c.get("role"), "kind": "conflict",
                     "reason": f"linked to {len(c.get('subs') or [])} Authentik identities "
                               f"({', '.join(c.get('subs') or [])}) — not changed; remove the stale link"})

    def removal(key: str, acct: str, action: dict, reason: str) -> None:
        """A step that takes a role away (or the account): the last-admin guard."""
        if acct in live_admins and len(live_admins) <= 1:
            refused.append({**action, "reason": f"would remove the last active {app} admin ({reason})"})
            return
        live_admins.discard(acct)
        acts.append(action)

    for key in sorted(current):
        acc = current[key]
        key_l = _norm(key)
        role = acc.get("role")
        state = _state(acc)
        acct = account_of(key, acc)
        if role in PROTECTED or key_l in ignored or is_service(key_l, acc):
            continue
        if mcp_owned(key_l):
            # Undo only this reconcile's own disable (releases before this
            # fix disabled these accounts as "left the module group").
            if state == "disabled" and _ledger_entry(ledger, key_l).get("disabled"):
                acts.append({"op": "enable", "email": key, "role": None})
            continue
        if state == "other":
            continue
        if role is not None and role not in rank:
            held.append({"email": key, "role": role, "kind": "role", "reason": f"unknown {app} role {role!r} — left as it is"})
            continue
        entry = _ledger_entry(ledger, key_l)
        elevated = role is not None and role != default

        if key_l not in access and key_l not in admins:
            # Left the module group (D5): disabled, never deleted, whatever the ledger says.
            if spec["disable_is_role_drop"]:
                if elevated:
                    removal(key, acct, {"op": "disable", "email": key, "role": None}, "left the module group")
                continue
            if state == "active":
                removal(key, acct, {"op": "disable", "email": key, "role": None}, "left the module group")
            continue

        if state == "disabled" and not spec["disable_is_role_drop"]:
            if not entry.get("disabled"):
                held.append({"email": key, "role": role, "kind": "ban", "reason": "disabled by hand — never lifted by the reconcile"})
                continue
            acts.append({"op": "enable", "email": key, "role": None})
            if role in spec["admin"]:
                live_admins.add(acct)
        want = _wanted_role(key_l, members, cat, app)
        if not want or want == role:
            continue
        if role is None or rank[want] > rank[role]:
            # Raised to exactly what the catalogue maps this user to.
            acts.append({"op": "set_role", "email": key, "role": want})
            if want in spec["admin"]:
                live_admins.add(acct)
            continue
        # Lowered or lateral: only a role this reconcile set itself.
        if entry.get("role") != role:
            held.append({"email": key, "role": role, "kind": "role",
                         "reason": f"hand-granted role; the catalogue maps {want!r}"})
            continue
        action = {"op": "set_role", "email": key, "role": want}
        if role in spec["admin"] and want not in spec["admin"]:
            removal(key, acct, action, f"{role} -> {want}")
        else:
            acts.append(action)
    return {"app": app, "default_role": default, "actions": acts, "held": held, "refused": refused,
            "matched": len(current) + len(conflicts)}


def plan(app: str, members: dict, current: dict, cat: Catalogue, **kw) -> list[dict]:
    """The actions only: `{"op": "set_role"|"disable"|"enable", "email", "role"}`."""
    return plan_report(app, members, current, cat, **kw)["actions"]


# ── knowledge scopes: datasets (#2473 D6) ───────────────────────────────────
#
# A knowledge scope is an Authentik group with attributes.rzfz_kb_scope: true.
# A Dify dataset joins a scope by carrying a knowledge tag named like the group;
# a cognee dataset joins by NAME, listed in the group's
# attributes.rzfz_kb_targets.cognee_datasets. A dataset in a scope is readable
# by exactly the scope's members plus its owner (Dify: `datasets.maintainer`,
# cognee: `datasets.owner_id`). A dataset in no scope is never touched here.

#: where the last reconcile writes "who can read which scope" (the
#: Settings -> Knowledge access page reads it)
KNOWLEDGE_REPORT_ENV = "RZFZ_KNOWLEDGE_REPORT"
KNOWLEDGE_REPORT_DEFAULT = "/data/rbac-reconcile.json"


def plan_dataset_acl(system: str, scopes: dict, datasets: list[dict], accounts: dict) -> list[dict]:
    """Restrict every dataset linked to a scope to the scope's members + its owner.

    `scopes`: group -> member e-mails; `accounts`: e-mail -> the app's account id;
    each dataset: `id`, `tags` (scope names), `creator` (the owner's account id),
    `members` (account ids that read it now) and, for Dify, `permission`.
    Returns `{"op": "restrict", "dataset", "members"}` for each dataset that
    differs; a converged dataset plans nothing. A dataset without an owner is
    never restricted (nobody would be left who manages it)."""
    acc = {_norm(k): v for k, v in accounts.items()}
    lower = _lower_members(scopes)
    acts = []
    for ds in datasets:
        linked = [t for t in ds.get("tags") or [] if t in lower]
        if not linked or not ds.get("creator"):
            continue
        members = {acc[e] for t in linked for e in lower[t] if e in acc}
        members.add(ds["creator"])
        want = sorted(members)
        converged = sorted(set(ds.get("members") or [])) == want
        if system == "dify" and ds.get("permission") != "partial_members":
            converged = False
        if not converged:
            acts.append({"op": "restrict", "dataset": ds["id"], "members": want})
    return acts


def link_cognee_targets(targets: dict, datasets: list[dict]) -> tuple[list[dict], dict]:
    """Fill each cognee dataset's `tags` from the scopes' target NAMES.

    cognee dataset names are unique per owner only; a name that matches more
    than one dataset is AMBIGUOUS and links nothing (it would give the scope
    other owners' data). Returns (datasets with tags, {scope: [ambiguous names]})."""
    by_name: dict[str, list[dict]] = {}
    for ds in datasets:
        by_name.setdefault(ds.get("name"), []).append(ds)
    out = [dict(ds, tags=[]) for ds in datasets]
    idx = {id(ds): o for ds, o in zip(datasets, out)}
    ambiguous: dict[str, list[str]] = {}
    for scope, names in sorted(targets.items()):
        for name in names or []:
            hits = by_name.get(name, [])
            if len(hits) > 1:
                ambiguous.setdefault(scope, []).append(name)
            elif hits:
                idx[id(hits[0])]["tags"].append(scope)
    return out, ambiguous


def accounts_map(accounts) -> tuple[dict, list[str]]:
    """e-mail (lower-cased) -> account id, deterministically. `accounts` is a
    dict or a list of [email, id] pairs (the SQL sends pairs, ordered). An
    e-mail two accounts share maps to NEITHER (fail closed: nobody gets a
    dataset through an ambiguous address); the duplicates are returned."""
    pairs = accounts.items() if isinstance(accounts, dict) else [tuple(p) for p in accounts or []]
    seen: dict[str, set] = {}
    for email, aid in pairs:
        if isinstance(email, str) and email.strip() and aid:
            seen.setdefault(_norm(email), set()).add(aid)
    dup = sorted(e for e, ids in seen.items() if len(ids) > 1)
    return {e: next(iter(ids)) for e, ids in seen.items() if len(ids) == 1}, dup


def owner_less(scopes: dict, datasets: list[dict]) -> list[str]:
    """Datasets linked to a scope that carry no owner: never restricted, an ERROR."""
    # key=str: dataset ids, not versions (the calver guard reads "tags" as one)
    return sorted((ds["id"] for ds in datasets
                   if any(t in scopes for t in ds.get("tags") or []) and not ds.get("creator")), key=str)


def plan_unscope(scopes: dict, datasets: list[dict], restricted: list[str]) -> list[dict]:
    """A dataset this reconcile restricted earlier (`restricted`, the scoped
    ledger) that is in no scope any more goes back to owner-only:
    `{"op": "unscope", "dataset", "members": [owner]}`. Without this, removing
    the tag kept the last member list forever. A dataset that no longer exists
    plans nothing; one without an owner is never touched."""
    linked = {ds["id"] for ds in datasets if any(t in scopes for t in ds.get("tags") or [])}
    by_id = {ds["id"]: ds for ds in datasets}
    return [{"op": "unscope", "dataset": i, "members": [by_id[i]["creator"]]}
            for i in sorted(set(restricted)) if i in by_id and i not in linked and by_id[i].get("creator")]


def widened_datasets(system: str, datasets: list[dict], actions: list[dict]) -> list[str]:
    """The planned datasets that someone OUTSIDE the plan reads right now: a
    leak, not just drift. `actions` are this run's restrict/unscope actions;
    each names exactly who should read the dataset. A current reader the action
    does not name widens it. Dify: a team-wide dataset is read by everyone in
    the workspace; an `only_me` dataset ignores its member rows (Dify 1.18's
    dataset access check), so stale rows there widen nothing."""
    by_id = {ds["id"]: ds for ds in datasets}
    out = []
    for a in actions:
        ds = by_id.get(a.get("dataset"))
        if ds is None:
            continue
        extra = set(ds.get("members") or []) - set(a.get("members") or [])
        if system == "dify":
            perm = ds.get("permission")
            wide = perm == "all_team_members" or (perm == "partial_members" and bool(extra))
        else:
            wide = bool(extra)
        if wide:
            out.append(ds["id"])
    # key=str: dataset ids, not versions (the calver guard reads "tags" as one)
    return sorted(set(out), key=str)


def knowledge_report(scopes: dict, systems: dict, generated: str, unreadable=()) -> dict:
    """{"generated", "unreadable", "scopes": {group: {"members", <system>...,
    "cognee_ambiguous", "openwebui"}}}: who reads each scope (its members) and
    which datasets it covers, each with its owner (who also reads it). A
    system whose datasets could not be read is NOT in the scope entries (an
    empty list would read as "this scope has no datasets there"); it is named
    in `unreadable`."""
    out = {}
    for scope in sorted(scopes):
        entry = {"members": sorted(_lower_members({scope: scopes[scope]})[scope]),
                 "openwebui": "group-managed"}
        for system in ("dify", "cognee"):
            if system not in systems:
                continue
            sysd = systems[system]
            acc, _dup = accounts_map(sysd.get("accounts") or {})
            rev = {v: k for k, v in acc.items()}
            entry[system] = [{"id": ds["id"], "name": ds.get("name") or ds["id"],
                              "owner": rev.get(ds.get("creator"), ds.get("creator"))}
                             for ds in sysd.get("datasets") or [] if scope in (ds.get("tags") or [])]
        if "cognee" in systems:
            entry["cognee_ambiguous"] = sorted((systems["cognee"].get("ambiguous") or {}).get(scope, []))
        out[scope] = entry
    return {"generated": generated, "unreadable": sorted(unreadable), "scopes": out}


def knowledge_report_path() -> str:
    return os.environ.get(KNOWLEDGE_REPORT_ENV) or KNOWLEDGE_REPORT_DEFAULT


def _atomic_json(path: str, doc: dict, mode: int) -> None:
    import tempfile
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_knowledge_report(doc: dict) -> None:
    """Atomic write (temp file + rename) next to the target."""
    _atomic_json(knowledge_report_path(), doc, 0o640)


#: which datasets the reconcile restricted, per system — next to the role ledger
SCOPED_LEDGER_ENV = "RZFZ_KNOWLEDGE_SCOPED_LEDGER"


def scoped_ledger_path() -> str:
    return os.environ.get(SCOPED_LEDGER_ENV) or os.path.join(
        os.path.dirname(ledger_path()) or ".", "knowledge-scoped.json")


def load_scoped_ledger() -> dict:
    """{system: [dataset ids]}; absent = nothing restricted yet. A corrupt
    file raises LedgerError (the caller then unscopes nothing)."""
    path = scoped_ledger_path()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise LedgerError(f"knowledge-scope ledger {path} unreadable: {e}") from e
    if not isinstance(doc, dict) or not all(isinstance(v, list) for v in doc.values()):
        raise LedgerError(f"knowledge-scope ledger {path} malformed")
    return doc


def scoped_ledger_next(old: list, req: dict) -> list:
    """The system's restricted set after an apply: what was recorded and still
    exists, plus the linked datasets that are restricted now (converged, or a
    restrict that SUCCEEDED), minus a successful unscope. A failed restrict of
    a new dataset is not recorded; a failed unscope keeps the id (retried)."""
    present = set(req.get("present") or [])
    planned = set(req.get("planned_restrict") or [])
    keep = (set(old) & present) | (set(req.get("scoped") or []) - planned) | set(req.get("restrict_ok") or [])
    return sorted(keep - set(req.get("unscope_ok") or []))


def scoped_ledger_update(req: dict) -> list:
    import fcntl
    path = scoped_ledger_path()
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, ".knowledge-scoped.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        doc = load_scoped_ledger()
        doc[req["system"]] = scoped_ledger_next(doc.get(req["system"]) or [], req)
        _atomic_json(path, doc, 0o600)
        return doc[req["system"]]


def _datasets_mode(req: dict) -> dict:
    """`datasets` mode: {"scopes": {group: {"members": [...], "cognee_targets": [...]}},
    "systems": {"dify"|"cognee": {"datasets": [...], "accounts": ...}},
    "unreadable": [systems]} -> {"actions": {system: [...]}, "tagged",
    "scoped", "present", "errors", "duplicates", "widened", "ambiguous", "report"}."""
    import datetime
    scopes = {g: set(v.get("members") or []) for g, v in (req.get("scopes") or {}).items()}
    systems = {k: dict(v) for k, v in (req.get("systems") or {}).items()}
    if "cognee" in systems:
        targets = {g: list(v.get("cognee_targets") or []) for g, v in (req.get("scopes") or {}).items()}
        systems["cognee"]["datasets"], systems["cognee"]["ambiguous"] = link_cognee_targets(
            targets, systems["cognee"].get("datasets") or [])
    try:
        restricted = load_scoped_ledger()
        ledger_error = None
    except LedgerError as e:
        restricted, ledger_error = {}, str(e)
    actions, tagged, scoped, present, errors, duplicates, widened = {}, {}, {}, {}, {}, {}, {}
    for system, sysd in systems.items():
        ds = sysd.get("datasets") or []
        acc, duplicates[system] = accounts_map(sysd.get("accounts") or {})
        actions[system] = plan_dataset_acl(system, scopes, ds, acc) + (
            [] if ledger_error else plan_unscope(scopes, ds, restricted.get(system) or []))
        widened[system] = widened_datasets(system, ds, actions[system])
        # key=str: dataset ids, not versions (the calver guard reads "tags" as one)
        tagged[system] = sorted((d["id"] for d in ds if any(t in scopes for t in d.get("tags") or [])),
                                key=str)
        scoped[system] = sorted((d["id"] for d in ds if any(t in scopes for t in d.get("tags") or [])
                                 and d.get("creator")), key=str)
        present[system] = sorted(d["id"] for d in ds)
        errors[system] = [f"dataset {i} is in a knowledge scope but has no owner; not restricted"
                          for i in owner_less(scopes, ds)]
        if ledger_error:
            errors[system].append(ledger_error + "; no dataset is taken out of a scope")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"actions": actions, "tagged": tagged, "scoped": scoped, "present": present,
            "errors": errors, "duplicates": duplicates, "widened": widened,
            "report": knowledge_report(scopes, systems, now, req.get("unreadable") or ()),
            "ambiguous": (systems.get("cognee") or {}).get("ambiguous") or {}}


# ── the ledger ──────────────────────────────────────────────────────────────

def ledger_path() -> str:
    return os.environ.get(LEDGER_ENV) or LEDGER_DEFAULT


def _read_ledger(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise LedgerError(f"ledger {path} unreadable: {e}") from e
    if not isinstance(doc, dict) or not all(isinstance(v, dict) for v in doc.values()):
        raise LedgerError(f"ledger {path} malformed: not {{app: {{account: entry}}}}")
    return doc


def load_ledger(app: str) -> dict:
    return _read_ledger(ledger_path()).get(app, {})


def fold_applied(entries: dict, app: str, default: str | None, applied: list[dict]) -> dict:
    """Pure: the ledger for one app after the applied actions (successes only)."""
    led = {k: (dict(v) if isinstance(v, dict) else {"role": v}) for k, v in entries.items()}
    for a in applied:
        if a.get("outcome") != "success":
            continue
        key = _norm(a["email"])
        e = led.setdefault(key, {})
        if a["op"] == "set_role":
            if a.get("role") == default:
                e.pop("role", None)
            else:
                e["role"] = a["role"]
        elif a["op"] == "disable":
            if APPS[app]["disable_is_role_drop"]:
                e.pop("role", None)      # Synapse: the admin flag dropped
            else:
                e["disabled"] = True
        elif a["op"] == "enable":
            e.pop("disabled", None)
        if not e:
            led.pop(key, None)
    return led


def _locked_update(mutate) -> None:
    """Read-modify-write the ledger under an exclusive flock, written through a
    unique temp file in the same directory, fsynced, then atomically renamed.
    A corrupt ledger is never overwritten (LedgerError)."""
    import fcntl
    import tempfile
    path = ledger_path()
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, ".downstream-roles.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        doc = _read_ledger(path)
        new = mutate(doc)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".downstream-roles.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(new, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def ledger_update(app: str, default: str | None, applied: list[dict]) -> None:
    if app not in APPS:
        raise LedgerError(f"no ledger for app {app!r}")

    def mutate(doc):
        doc[app] = fold_applied(doc.get(app, {}), app, default, applied)
        return doc
    _locked_update(mutate)


def ledger_import(old: dict) -> dict:
    """Merge the first A12 host-side ledger ({app: {email: role}}) once: an
    entry the volume ledger already has wins. Synapse entries keyed by an
    e-mail are dropped: Synapse is keyed by the OIDC sub now, so they could
    never match. Returns {"added": n, "dropped": m}."""
    if not isinstance(old, dict):
        raise LedgerError("the ledger to import is not a mapping")
    count = {"added": 0, "dropped": 0}

    def mutate(doc):
        for app, entries in old.items():
            if app not in APPS or not isinstance(entries, dict):
                continue
            cur = doc.setdefault(app, {})
            for k, v in entries.items():
                if app == "synapse" and "@" in str(k):
                    count["dropped"] += 1
                    continue
                if _norm(k) not in cur and isinstance(v, (str, dict)):
                    cur[_norm(k)] = {"role": v} if isinstance(v, str) else v
                    count["added"] += 1
        return doc
    _locked_update(mutate)
    return count


#: who ran the reconcile, in the audit log's `user` (a request may name the
#: hourly run instead; anything else falls back to this)
AUDIT_ACTOR = "rzfz provision-sso-users --reconcile-roles"


def _actor(req: dict) -> str:
    a = req.get("actor")
    return a.strip()[:120] if isinstance(a, str) and a.strip() else AUDIT_ACTOR


def _audit_log():
    from app.services.audit_logger import AuditLogger
    return AuditLogger(os.environ.get("AUDIT_LOG_PATH", "/var/log/razzfazz/config-audit.log"))


def _audit(req: dict) -> int:
    """Write applied reconcile actions to the portal audit log (category identity, D12)."""
    log = _audit_log()
    for a in req.get("applied", []):
        ok = a.get("outcome") == "success"
        log.log("identity.downstream_role", category="identity", action=a.get("op"),
                user=_actor(req),
                target=f"{req.get('app')}:{a.get('email')}", detail=a.get("role"),
                outcome="success" if ok else "failure", level="INFO" if ok else "WARN",
                error=None if ok else a.get("outcome"))
    return 0


def _audit_datasets(req: dict) -> int:
    """`audit-datasets` mode: one audit entry per knowledge-scope dataset change
    the reconcile applied (or failed to apply). Request: {"system", "actor",
    "applied": [{"op": "restrict"|"unscope", "dataset", "readers": n,
    "outcome": "ok"|"failed"}]}. The readers are counted, never listed."""
    log = _audit_log()
    system = req.get("system")
    for a in req.get("applied", []):
        ok = a.get("outcome") == "ok"
        op = a.get("op")
        n = a.get("readers")
        detail = ("readable by its owner only" if op == "unscope"
                  else f"readable by {n} account(s): the scope's members and its owner")
        log.log("identity.knowledge_scope", category="identity", action=op, user=_actor(req),
                target=f"{system}:dataset:{a.get('dataset')}", detail=detail,
                outcome="success" if ok else "failure", level="INFO" if ok else "WARN",
                error=None if ok else "the change was not applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    from razzfazz_common.rbac import catalogue
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "plan"
    try:
        if mode == "groups":
            if len(argv) != 2:
                print("usage: groups <app>", file=sys.stderr)
                return 2
            json.dump(needed_groups(argv[1], catalogue()), sys.stdout)
            return 0
        if mode == "audit":
            return _audit(json.load(sys.stdin))
        if mode == "audit-datasets":
            return _audit_datasets(json.load(sys.stdin))
        if mode == "ledger-update":
            req = json.load(sys.stdin)
            ledger_update(req["app"], req.get("default_role"), req.get("applied") or [])
            return 0
        if mode == "datasets":
            json.dump(_datasets_mode(json.load(sys.stdin)), sys.stdout)
            return 0
        if mode == "scoped-update":
            json.dump(scoped_ledger_update(json.load(sys.stdin)), sys.stdout)
            return 0
        if mode == "knowledge-report":
            write_knowledge_report(json.load(sys.stdin))
            return 0
        if mode == "ledger-import":
            json.dump(ledger_import(json.load(sys.stdin)), sys.stdout)
            return 0
        if mode != "plan":
            print(f"unknown mode {mode!r} (plan | groups <app> | datasets | scoped-update | knowledge-report | audit | audit-datasets | ledger-update | ledger-import)", file=sys.stderr)
            return 2
        req = json.load(sys.stdin)
        cat = catalogue()
        managed = load_ledger(req["app"])
        json.dump(plan_report(req["app"], {g: set(v) for g, v in req["members"].items()}, req["current"],
                              cat, managed=managed,
                              service_accounts=req.get("service_accounts") or (),
                              ignored_accounts=req.get("ignored_accounts") or (),
                              service_localparts=req.get("service_localparts") or (),
                              conflicts=req.get("conflicts") or (),
                              mcp_domain=req.get("mcp_domain") or ""), sys.stdout)
        return 0
    except CatalogueError as e:
        print(f"role catalogue unusable — nothing reconciled: {e}", file=sys.stderr)
        return 3
    except LedgerError as e:
        print(f"{e} — nothing reconciled for this app (no removals)", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
