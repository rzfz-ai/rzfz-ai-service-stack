# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2473 D7 — technical users (service accounts) for workflows and autonomous agents.

A technical user is an Authentik service account under path rzfz/technical,
named svc-<purpose>, owned by a named person (kept by the owner's OIDC sub,
shown by username), bound explicitly to the consumers that may use it. It
never receives a group implicitly (no knowledge scope widening). With OpenBao
enabled it also gets an AppRole of the same name reading only
secret/apps/<name>/*, with a 5-minute token and a 24 h / 1000-use secret_id,
both bound to Caddy's fixed stack-internal address. Every call refuses a name
that is not an existing technical user: the registry runs with the Authentik
bootstrap token and must never touch a person. Workstreams B (#2472, #2554)
and C bind to it; they do not create identities.

The owner rule (fail closed): a technical user whose owner is deactivated,
deleted, or no longer a person in Authentik is REVOKED by `enforce_owners`:
the account is deactivated, its Authentik tokens are deleted and its AppRole
is removed. The account and its attributes stay, so an administrator can
`reassign` it to a new owner, which re-enables it (credentials are issued
anew; nothing old comes back). A negative verdict needs a COMPLETE listing
(every page carries its pagination, the total does not move while paging) AND
a direct lookup of the owner by the pk stored next to the sub; anything short
of that decides nothing.

Revocation is crash-safe: the `rzfz_revoked` record is written FIRST with
state "revoking", then the account is deactivated, then its tokens and AppRole
go, and only then does the record say "revoked". A revocation that stopped
half-way is finished by the next sweep (and by the page's "Retry revoke").
`rzfz_revoked.kind` says who revoked: "owner" (the owner check) or "admin";
only an "owner" revocation is undone by a reassign.

The MCP gateway (#2472): with `mcp_mirror` set (mcp profile on), every change
is pushed to mcp-manager's mirror of the technical users, which holds no
Authentik token. A revocation is finished only when the gateway confirmed it
(its gateway tokens revoked and its vaulted upstream credentials deleted in
the same step): until then the record stays "revoking", the page offers
"Retry revoke" and every sweep retries it. An unbind the gateway did not hear
is an error too (it narrows access); a create or bind that did not reach it
is repaired by the reconcile on the next page load or sweep.
"""
from __future__ import annotations

import logging
import re

from app.services.gitea_admin import GiteaAdminError
import threading
import time
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^svc-[a-z0-9-]{3,40}$")
RESERVED = frozenset({"svc-agent-manager", "svc-mcp-manager"})   # A19's own AppRoles (D7)
BINDING_RE = re.compile(r"^(dify-app|agent-instance|mcp-integration|gitea-repo):[A-Za-z0-9._/-]{1,120}$")
PATH = "rzfz/technical"
APPROLE_LIMITS = {"token_ttl": "5m", "token_max_ttl": "5m", "secret_id_ttl": "24h", "secret_id_num_uses": 1000}
#: The actor the owner sweep writes into the audit trail and rzfz_revoked.by.
OWNER_SWEEP = "owner-check"
#: rzfz_revoked.kind — only KIND_OWNER is undone by a reassign.
KIND_OWNER, KIND_ADMIN = "owner", "admin"
REVOKING, REVOKED = "revoking", "revoked"
OWNER_SWEEP_INTERVAL = 300.0
#: The token scopes a Gitea service user may get (least privilege: never admin,
#: organisation, package or user writes). Enforced here, where the token is minted.
GITEA_SCOPES = frozenset({"read:repository", "write:repository", "read:issue", "write:issue", "read:user"})


class TechnicalUserError(ValueError):
    pass


class AuthentikUnavailable(RuntimeError):
    """Authentik did not answer usably. Nothing is decided on it."""


class AuthentikTransient(AuthentikUnavailable):
    """No answer at all, or a 5xx: says nothing about the request itself."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _active_since(attrs):
    """When Authentik made this account active: its creation, or the last
    reassign that re-enabled it (ISO-8601 with a zone), else None."""
    best = None
    for key in ("rzfz_created", "rzfz_reactivated"):
        v = attrs.get(key)
        if not isinstance(v, str):
            continue
        try:
            ts = datetime.fromisoformat(v)
        except ValueError:
            continue
        if ts.tzinfo is not None and (best is None or ts > best):
            best = ts
    return best.isoformat() if best else None


class _BadRecord(ValueError):
    """A technical user's Authentik record has a field of the wrong shape.
    The message names the field, never its value."""


def _read_row(u):
    """One listing row from an Authentik user record; _BadRecord (or any
    other exception) when the record is not what create/bind/revoke write."""
    name = u["username"]
    if not isinstance(name, str) or not name:
        raise _BadRecord("username is not a string")
    attrs = u.get("attributes") or {}
    if not isinstance(attrs, dict):
        raise _BadRecord("attributes is not a mapping")
    sub = attrs.get("rzfz_owner")
    if sub is not None and not isinstance(sub, str):
        raise _BadRecord("rzfz_owner is not a string")
    revoked = attrs.get("rzfz_revoked") or {}
    if not isinstance(revoked, dict):
        raise _BadRecord("rzfz_revoked is not a mapping")
    bindings = attrs.get("rzfz_bindings") or []
    if not isinstance(bindings, list) or not all(isinstance(b, str) for b in bindings):
        raise _BadRecord("rzfz_bindings is not a list of strings")
    return {"username": name, "owner_sub": sub, "bindings": list(bindings),
            "last_used": attrs.get("rzfz_last_used"), "is_active": bool(u.get("is_active")),
            "revoked_reason": revoked.get("reason") if revoked else None,
            "revoke_state": revoked.get("state") if revoked else None}


def _owner_verdict(p, sub):
    """None when `p` is the active person `sub`; else the revocation reason."""
    if p is None or (sub and p.get("uid") != sub):
        return "owner deleted"
    if not p.get("is_active"):
        return "owner deactivated"
    if p.get("type") != "internal":
        return "owner is no longer a person"
    return None


class TechnicalUserRegistry:
    def __init__(self, authentik, openbao=None, audit=None, bound_cidr=None, mcp_mirror=None, gitea=None):
        self._ak, self._bao, self._audit, self._cidr = authentik, openbao, audit, bound_cidr
        self._mcp = mcp_mirror
        self._gitea = gitea
        self.last_failures, self.last_unreadable = [], set()

    def _log(self, action, name, actor, detail="", outcome="success"):
        if self._audit:
            self._audit(category="identity", action=action, target=name, user=actor, detail=detail,
                        outcome=outcome)

    @staticmethod
    def _check_name(name):
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise TechnicalUserError(f"name must match {NAME_RE.pattern}")
        if name in RESERVED:
            raise TechnicalUserError(f"{name} is reserved for the stack's own AppRole")

    def _managed(self, name):
        """The user record, only if `name` is an existing technical user."""
        self._check_name(name)
        u = self._ak.get_user(name)
        if not u or u.get("path") != PATH or u.get("type") != "service_account":
            raise TechnicalUserError(f"{name} is not a technical user (path {PATH})")
        return u

    def _owner(self, owner):
        """D7: the owner is an existing, active person; kept by their sub (uid),
        with their pk for the owner check's direct lookup."""
        if not isinstance(owner, str) or not owner.strip():
            raise TechnicalUserError("a technical user needs a named human owner")
        p = self._ak.get_person(owner.strip())
        if not p or p.get("type") != "internal" or not p.get("is_active") or not p.get("uid"):
            raise TechnicalUserError(f"owner {owner!r} is not a person (an active internal Authentik user)")
        return p["uid"], p.get("pk")

    def _owner_sub(self, owner):
        return self._owner(owner)[0]

    def _bound_cidr(self):
        if self._cidr is None:
            raise TechnicalUserError("no CIDR bound for the AppRole: refusing an unbound secret_id")
        try:
            cidr = self._cidr()
        except ValueError as e:
            raise TechnicalUserError(f"no CIDR bound for the AppRole: {e}") from None
        if not cidr:
            raise TechnicalUserError("no CIDR bound for the AppRole: refusing an unbound secret_id")
        return cidr

    def _mint_approle(self, name, cidr):
        limits = dict(APPROLE_LIMITS, token_bound_cidrs=cidr, secret_id_bound_cidrs=cidr)
        return self._bao.ensure_approle(name, [f"secret/data/apps/{name}/*"], limits)

    def create(self, name, owner, bindings, actor):
        self._check_name(name)
        owner_sub, owner_pk = self._owner(owner)
        bindings = list(bindings or [])
        for b in bindings:
            if not isinstance(b, str) or not BINDING_RE.match(b):
                raise TechnicalUserError(f"binding {b!r} must match {BINDING_RE.pattern}")
        if self._ak.get_user(name):
            raise TechnicalUserError(f"{name} exists")
        cidr = self._bound_cidr() if self._bao is not None else None
        attrs = {"rzfz_owner": owner_sub, "rzfz_owner_pk": owner_pk, "rzfz_bindings": bindings,
                 "rzfz_created": _now_iso(), "rzfz_last_used": None}
        # The AppRole first: if it cannot be made, no Authentik account is left
        # behind (a retry would otherwise say "exists"); if the account then
        # fails, the AppRole goes again.
        approle = self._mint_approle(name, cidr) if self._bao is not None else None
        try:
            user, token_id = self._ak.create_service_account(name, PATH, attrs)
        except Exception:
            if self._bao is not None:
                self._bao.delete_approle(name)
            raise
        self._log("technical_user.create", name, actor, f"owner={owner.strip()}; bindings={bindings}")
        self._mcp_push(name)
        return {"username": name, "pk": user["pk"], "token_identifier": token_id,
                "owner_sub": owner_sub, "approle": approle}

    def _owner_username(self, sub):
        if not sub:
            return None
        return self._ak.username_for_uid(sub)

    def list(self):
        """Every technical user as a row. Each record is its own unit: a
        record an administrator broke by hand in Authentik (attributes not a
        mapping; rzfz_revoked, rzfz_owner or rzfz_bindings of the wrong shape)
        becomes a row carrying `error`, naming the account, with no bindings
        and not active, and is logged; every other row is listed as usual. So
        one bad record never stops the rotator, the page or a manual rotate.
        Only an unreadable LISTING (AuthentikUnavailable) raises."""
        out, names = [], {}
        for u in self._ak.list_users(PATH):
            try:
                if u.get("path") != PATH:
                    continue
                row = _read_row(u)
            except Exception as e:  # noqa: BLE001 — one odd record never hides the others
                name = u.get("username") if isinstance(u, dict) else None
                name = name if isinstance(name, str) and name else "(unreadable account)"
                detail = str(e) if isinstance(e, _BadRecord) else type(e).__name__
                _log.warning("technical users: the record of %s cannot be read (%s)", name, detail)
                out.append({"username": name, "owner_sub": None, "owner": "(unreadable)", "bindings": [],
                            "last_used": None, "is_active": False, "revoked_reason": None,
                            "revoke_state": None,
                            "error": f"its Authentik record cannot be read ({detail}); "
                                     "fix its attributes in Authentik"})
                continue
            sub = row["owner_sub"]
            if sub not in names:
                names[sub] = self._owner_username(sub)
            row["owner"] = names[sub] or "(deleted user)"
            out.append(row)
        return out

    def bind(self, name, binding, actor):
        u = self._managed(name)
        if not isinstance(binding, str) or not BINDING_RE.match(binding):
            raise TechnicalUserError(f"binding {binding!r} must match {BINDING_RE.pattern}")
        attrs = dict(u.get("attributes") or {})
        attrs["rzfz_bindings"] = sorted(set(attrs.get("rzfz_bindings") or []) | {binding})
        self._ak.update_attributes(name, attrs)
        self._log("technical_user.bind", name, actor, binding)
        self._mcp_push(name)

    def unbind(self, name, binding, actor):
        u = self._managed(name)
        attrs = dict(u.get("attributes") or {})
        attrs["rzfz_bindings"] = [b for b in attrs.get("rzfz_bindings") or [] if b != binding]
        self._ak.update_attributes(name, attrs)
        self._log("technical_user.unbind", name, actor, binding)
        if self._mcp is not None and not self._mcp_push(name):
            raise TechnicalUserError(f"{binding} is unbound in Authentik, but the MCP gateway was not reached; "
                                     "it still allows it until this page is loaded again with the gateway up")

    def rotate_secret_id(self, name, actor):
        u = self._managed(name)
        if self._bao is None:
            raise TechnicalUserError("OpenBao is not enabled")
        if not u.get("is_active") or (u.get("attributes") or {}).get("rzfz_revoked"):
            raise TechnicalUserError(f"{name} is revoked; reassign it first")
        out = self._bao.rotate_secret_id(name)
        self._log("technical_user.rotate_secret_id", name, actor)
        return out

    def _revoke(self, u, reason, kind, by):
        """Record first ("revoking"), then deactivate, then tokens and AppRole,
        then "revoked". A crash anywhere leaves a record the sweep finishes."""
        name = u["username"]
        attrs = dict(u.get("attributes") or {})
        attrs["rzfz_revoked"] = {"reason": reason, "kind": kind, "by": by, "at": _now_iso(),
                                 "state": REVOKING}
        self._ak.update_attributes(name, attrs)
        self._finish_revoke(name, attrs)

    def _finish_revoke(self, name, attrs):
        self._ak.deactivate(name)
        self._ak.delete_tokens(name)
        if self._bao is not None:
            self._bao.delete_approle(name)
        # The gateway last: its tokens and vaulted credentials go in one
        # transaction there. Not confirmed = not finished (stays "revoking").
        if self._mcp is not None and not self._mcp.revoke(name):
            raise TechnicalUserError(f"{name} is revoked in Authentik, but the MCP gateway was not "
                                     "reached — revoke again (its gateway token still works until then)")
        self._revoke_gitea(name)
        attrs = dict(attrs)
        attrs["rzfz_revoked"] = dict(attrs["rzfz_revoked"], state=REVOKED)
        self._ak.update_attributes(name, attrs)

    def _gitea_record(self, name):
        """mcp-manager's Gitea record of `name` (state, revoked, the Gitea user
        id and the Authentik pk it was made for), or None. An unreadable
        record is an error (undecided), never "nothing there"."""
        try:
            return self._mcp.gitea_records().get(name)
        except Exception as e:  # noqa: BLE001
            raise TechnicalUserError(f"{name}: its Gitea service user's record could not be read from the MCP "
                                     f"gateway ({type(e).__name__}) — try again") from None

    def _gitea_revoke_one(self, name, rec):
        """Revoke the Gitea user `rec` records. A token exists only for a
        recorded Gitea user id (the id is recorded before a token is
        minted), so a record without one owes nothing in Gitea. Raises when
        Gitea did not confirm."""
        if rec.get("gitea_user_id") is not None:
            self._gitea.revoke(name, expected_id=rec["gitea_user_id"])
        # Best effort: a state left "live" only makes the reconcile revoke again.
        self._mcp.set_gitea_state(name, "revoked")

    def _revoke_gitea(self, name):
        """#2554, part of every revocation: the Gitea service user's rzfz-gw-*
        tokens deleted and its login prohibited, confirmed by Gitea, or the
        revocation is not finished (it stays "revoking" and is retried). Only
        where a Gitea service user can exist (mcp and gitea on)."""
        if self._gitea is None or self._mcp is None:
            return
        try:
            rec = self._mcp.gitea_records().get(name)
        except Exception as e:  # noqa: BLE001
            raise TechnicalUserError(f"{name}: whether its Gitea service user holds a token could not be "
                                     f"read from the MCP gateway ({type(e).__name__}) — revoke again") from None
        if not rec or rec.get("state") not in ("live", "owed"):
            return
        try:
            self._gitea_revoke_one(name, rec)
        except Exception as e:  # noqa: BLE001 — GiteaAdminError names the call and status only
            detail = str(e) if isinstance(e, GiteaAdminError) else type(e).__name__
            raise TechnicalUserError(f"{name} is revoked in Authentik and on the MCP gateway, but Gitea did not "
                                     f"confirm that its Gitea token is deleted ({detail}) — revoke again") from None

    def revoke(self, name, actor):
        """An administrator's revoke. On a half-finished revocation it only
        finishes it (keeping who started it); otherwise it records kind=admin,
        which no reassign undoes."""
        u = self._managed(name)
        rec = (u.get("attributes") or {}).get("rzfz_revoked") or {}
        if rec.get("state") == REVOKING:
            self._finish_revoke(name, u.get("attributes") or {})
            self._log("technical_user.revoke", name, actor, "finished a half-done revocation")
            return
        self._revoke(u, f"revoked by {actor}", KIND_ADMIN, actor)
        self._log("technical_user.revoke", name, actor)

    def _confirm_owner(self, attrs):
        """The listing said the owner is gone or inactive: ask Authentik for
        that ONE user directly (by the pk stored at create/reassign) before
        anything is revoked. None = the owner is fine after all (or there is
        no pk to ask by, so nothing is decided)."""
        sub, pk = attrs.get("rzfz_owner"), attrs.get("rzfz_owner_pk")
        if not isinstance(pk, int) or isinstance(pk, bool):
            raise AuthentikUnavailable("no owner pk recorded: the owner cannot be confirmed; reassign to fix")
        return _owner_verdict(self._ak.person_by_pk(pk), sub)

    def enforce_owners(self, actor=OWNER_SWEEP):
        """The owner rule. Finishes every half-done revocation, and revokes
        every active technical user whose owner is no longer an active person
        — only when a complete listing AND a direct lookup of the owner agree.
        Returns [(name, reason)] of what it revoked or finished.

        Each account is its own unit: an error while confirming, revoking or
        finishing ONE account is audited, recorded in `last_failures` and the
        sweep goes on with the next (a revocation that keeps failing must not
        stop the owner rule for everyone after it). Only an unreadable
        listing (AuthentikUnavailable from list_users or person_for_uid)
        stops the sweep, having decided nothing further."""
        done = []
        self.last_failures = []
        self.last_unreadable = set()
        for u in self._ak.list_users(PATH):
            name = u.get("username")
            if (u.get("path") != PATH or u.get("type") != "service_account"
                    or not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED):
                continue
            try:
                attrs = u.get("attributes") or {}
                rec = attrs.get("rzfz_revoked") or {}
                if not isinstance(attrs, dict) or not isinstance(rec, dict):
                    raise ValueError("malformed attributes or rzfz_revoked record")
                sub = attrs.get("rzfz_owner")
                if sub is not None and not isinstance(sub, str):
                    raise ValueError("malformed rzfz_owner")
            except Exception as e:  # noqa: BLE001 — one odd record never stops the sweep
                self.last_failures.append((name, type(e).__name__))
                self.last_unreadable.add(name)
                self._log("technical_user.revoke", name, actor,
                          f"FAILED ({type(e).__name__}): the record cannot be read; fix it in Authentik",
                          outcome="failure")
                continue
            if rec.get("state") != REVOKING:
                if not u.get("is_active"):
                    continue
                # An unreadable LISTING (not one account) stops the sweep here.
                if _owner_verdict(self._ak.person_for_uid(sub) if sub else None, sub) is None:
                    continue
            try:
                if rec.get("state") == REVOKING:
                    self._finish_revoke(name, attrs)
                    reason = rec.get("reason")
                    self._log("technical_user.revoke", name, actor, f"finished: {reason}")
                else:
                    reason = self._confirm_owner(attrs)
                    if reason is None:
                        continue
                    self._revoke(u, reason, KIND_OWNER, actor)
                    self._log("technical_user.revoke", name, actor, reason)
            except Exception as e:  # noqa: BLE001 — one account never stops the sweep
                self.last_failures.append((name, type(e).__name__))
                self._log("technical_user.revoke", name, actor,
                          f"FAILED ({type(e).__name__}); the next sweep retries", outcome="failure")
                continue
            done.append((name, reason))
        return done

    def reassign(self, name, owner, actor):
        """A new owner. A user the owner check revoked is re-enabled (without
        its old credentials; a new AppRole is minted when OpenBao is on). A
        user an administrator revoked stays revoked; a half-done revocation
        must be finished first."""
        u = self._managed(name)
        owner_sub, owner_pk = self._owner(owner)
        attrs = dict(u.get("attributes") or {})
        rec = attrs.get("rzfz_revoked") or {}
        if rec.get("state") == REVOKING:
            raise TechnicalUserError(f"{name}: a revocation is unfinished; retry the revoke first")
        reactivate = bool(rec) and rec.get("kind") == KIND_OWNER and rec.get("state") == REVOKED
        cidr = self._bound_cidr() if (reactivate and self._bao is not None) else None
        attrs["rzfz_owner"], attrs["rzfz_owner_pk"] = owner_sub, owner_pk
        if reactivate:
            attrs.pop("rzfz_revoked", None)
            # The MCP gateway re-enables a revoked mirror row only for an
            # activation LATER than its revocation (a stale push never does).
            attrs["rzfz_reactivated"] = _now_iso()
        self._ak.update_attributes(name, attrs)
        approle = None
        if reactivate:
            if self._bao is not None:
                approle = self._mint_approle(name, cidr)
            self._ak.activate(name)
        self._log("technical_user.reassign", name, actor,
                  f"owner={owner.strip()}; reactivated={reactivate}")
        self._mcp_push(name)
        if self._gitea is not None and self._mcp is not None:
            try:
                rec = self._mcp.gitea_records().get(name) or {}
                gid = rec.get("gitea_user_id")
                if rec.get("state") == "live" and gid is not None and rec.get("authentik_pk") == u.get("pk"):
                    g = self._gitea.get_user(name)
                    if g is not None and g["id"] == gid:   # never another account of that name
                        self._gitea.describe(name, owner.strip())
            except Exception as e:  # noqa: BLE001 — the description is display only
                _log.warning("technical users: the Gitea description of %s names the old owner (%s)",
                             name, type(e).__name__)
        return {"owner_sub": owner_sub, "reactivated": reactivate, "approle": approle}

    # -- the MCP gateway's mirror (#2472) ----------------------------------

    def _mcp_record(self, u) -> dict:
        """The mirror's view of one Authentik record. Active only when
        Authentik says so AND no revocation is recorded (a crash between the
        "revoking" record and the deactivate leaves is_active true)."""
        row = _read_row(u)
        sub = row["owner_sub"] or ""
        return {"username": row["username"], "owner_sub": sub,
                "owner_username": (self._owner_username(sub) if sub else None) or "",
                "authentik_pk": u.get("pk"), "bindings": row["bindings"],
                "is_active": row["is_active"] and not row["revoke_state"],
                "active_since": _active_since(u.get("attributes") or {})}

    def _mcp_user(self, name: str) -> dict:
        return self._mcp_record(self._ak.get_user(name) or {"username": name})

    def _mcp_push(self, name: str) -> bool:
        """Best effort; False when the mirror did not confirm (logged there)."""
        if self._mcp is None:
            return True
        try:
            return self._mcp.push(self._mcp_user(name))
        except Exception as e:  # noqa: BLE001 — Authentik already holds the change
            _log.warning("technical users: %s not pushed to the MCP gateway (%s)", name, type(e).__name__)
            return False

    def reconcile_mcp(self) -> int:
        """Bring the MCP gateway in line with Authentik. Returns how many
        pushes the mirror confirmed.

        First, every technical user the gateway still holds live (and so may
        hold a gateway token or a vaulted credential for) is looked up
        DIRECTLY by the Authentik pk the mirror stored (GET /core/users/<pk>/,
        never a filtered listing): a 404 revokes it on the gateway, and so
        does a pk that now names another account, or one no longer a
        technical user (deleted and re-created under the same name, renamed,
        moved). Any other answer decides nothing. This does not depend on the
        listing: the last technical user deleted by hand is revoked too.

        Then every technical user in the COMPLETE listing is pushed, inactive
        ones too (which revokes them there). One unreadable record is skipped
        and decides nothing; an unreadable listing raises before anything is
        pushed; an unreadable mirror listing only skips the first step.

        Last (#2554, also after an unreadable listing: it rests on the
        mirror's state alone), every Gitea service user the mirror records as
        revoked but still "live" is revoked in Gitea."""
        if self._mcp is None:
            return 0
        self._mcp_revoke_gone()
        try:
            return self._push_all()
        finally:
            self._gitea_revoke_owed()

    def _gitea_revoke_owed(self):
        """#2554: every Gitea service user whose mirror row is revoked but
        whose Gitea revoke was never confirmed (an account deleted from
        Authentik by hand, a Gitea that was down or off, a push that revoked
        the row) is revoked in Gitea now. One failure is logged and the next
        reconcile retries it."""
        if self._gitea is None:
            return
        try:
            owed = [r for r in self._mcp.gitea_live() if r.get("revoked")]
        except Exception as e:  # noqa: BLE001 — no listing, no decision
            _log.warning("technical users: the Gitea service users owed a revoke could not be read (%s)",
                         type(e).__name__)
            return
        for rec in owed:
            name = rec.get("name")
            if not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED:
                continue
            try:
                self._gitea_revoke_one(name, rec)
            except Exception as e:  # noqa: BLE001 — retried on the next reconcile
                _log.warning("technical users: Gitea did not confirm the revoke of %s (%s); retried",
                             name, type(e).__name__)
                continue
            self._log("technical_user.revoke", name, OWNER_SWEEP, "its Gitea token is deleted, its login prohibited")

    def _push_all(self):
        ok = 0
        for u in self._ak.list_users(PATH):
            name = u.get("username") if isinstance(u, dict) else None
            try:
                if (u.get("path") != PATH or u.get("type") != "service_account"
                        or not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED):
                    continue
                user = self._mcp_record(u)
            except Exception as e:  # noqa: BLE001 — one odd record never stops the others
                detail = str(e) if isinstance(e, _BadRecord) else type(e).__name__
                _log.warning("technical users: %s not pushed to the MCP gateway, its record cannot be read (%s)",
                             name if isinstance(name, str) else "(unreadable account)", detail)
                continue
            if self._mcp.push(user):
                ok += 1
        return ok

    def _mcp_revoke_gone(self):
        try:
            rows = self._mcp.rows()
        except Exception as e:  # noqa: BLE001 — no mirror listing, no decision
            _log.warning("technical users: the MCP gateway's list could not be read (%s)", type(e).__name__)
            return
        for row in rows:
            name, pk = row.get("name"), row.get("authentik_pk")
            if not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED:
                continue
            if not isinstance(pk, int) or isinstance(pk, bool):
                _log.warning("technical users: %s has no Authentik pk on the MCP gateway; not checked", name)
                continue
            try:
                u = self._ak.user_by_pk(pk)
            except Exception as e:  # noqa: BLE001 — only a 404 is "deleted"
                _log.warning("technical users: %s could not be looked up by pk (%s)", name, type(e).__name__)
                continue
            if u is None:
                why = "deleted in Authentik"
            elif u.get("username") != name or u.get("path") != PATH or u.get("type") != "service_account":
                why = "its Authentik pk no longer names this technical user"
            else:
                continue
            if self._mcp.revoke(name):
                self._log("technical_user.revoke", name, OWNER_SWEEP,
                          f"{why}: its MCP gateway tokens and credentials are revoked")

    def _mcp_live(self, name):
        """The Authentik record, only for an active, unrevoked technical user
        whose current state the gateway has just confirmed. Authentik
        decides: a mirror that missed a revoke never lets a revoked account
        get a token or a credential."""
        if self._mcp is None:
            raise TechnicalUserError("the MCP gateway is not enabled on this box (the mcp module is off)")
        u = self._managed(name)
        if not u.get("is_active") or (u.get("attributes") or {}).get("rzfz_revoked"):
            raise TechnicalUserError(f"{name} is revoked; reassign it first")
        if not self._mcp.push(self._mcp_record(u)):
            raise TechnicalUserError(f"the MCP gateway was not reached; nothing was issued for {name}")
        return u

    def mcp_mint_token(self, name, consumer, actor):
        """A new MCP gateway token for `consumer` (dify or agent); the
        gateway revokes that consumer's earlier tokens. Returned once."""
        if consumer not in ("dify", "agent"):
            raise TechnicalUserError("an MCP gateway token is for dify or agent")
        self._mcp_live(name)
        try:
            token = self._mcp.mint_token(name, consumer)
        except Exception as e:  # noqa: BLE001 — requests' errors carry no secret, but say it plainly
            raise TechnicalUserError(f"the MCP gateway issued no token ({type(e).__name__})") from None
        self._log("technical_user.mcp_token", name, actor, f"consumer={consumer}")
        return token

    def mcp_store_credential(self, name, mcp_id, fields, actor):
        """Vault an upstream credential (a PAT, or client credentials) for a
        gateway server this technical user is bound to. The fields' values
        are never logged."""
        u = self._mcp_live(name)
        bindings = (u.get("attributes") or {}).get("rzfz_bindings") or []
        if f"mcp-integration:{mcp_id}" not in bindings:
            raise TechnicalUserError(f"{name} is not bound to mcp-integration:{mcp_id}")
        if not fields:
            raise TechnicalUserError("enter the credential")
        try:
            self._mcp.store_credential(name, mcp_id, dict(fields))
        except Exception as e:  # noqa: BLE001
            raise TechnicalUserError(f"the MCP gateway did not store the credential ({type(e).__name__})") from None
        self._log("technical_user.mcp_credential", name, actor, f"{mcp_id}: {sorted(fields)}")

    def mcp_company_brain(self, name, actor):
        """#2472: provision the company brain (cognee-company) for a technical
        user bound to mcp-integration:cognee-company-svc, so its Dify
        workflows read it through the gateway. Only for an active, unrevoked
        technical user whose state the gateway has just confirmed."""
        u = self._mcp_live(name)
        if "mcp-integration:cognee-company-svc" not in ((u.get("attributes") or {}).get("rzfz_bindings") or []):
            raise TechnicalUserError(f"{name} is not bound to mcp-integration:cognee-company-svc")
        try:
            iid = self._mcp.provision_company_brain(name)
        except Exception as e:  # noqa: BLE001
            raise TechnicalUserError(f"the MCP gateway did not provision the company brain ({type(e).__name__})") from None
        self._log("technical_user.company_brain", name, actor, f"instance={iid}")
        return iid

    def gitea_service_user(self, name, scopes, actor):
        """#2554: a Gitea service user named `name` — a restricted bot (no
        person's SSO login can be linked to it; see gitea_admin), its
        description naming the owner — with ONE token of the chosen scopes,
        named rzfz-gw-*, vaulted by mcp-manager for its `gitea` gateway server
        and never returned. Only for an active, unrevoked technical user bound
        to mcp-integration:gitea whose state the gateway has just confirmed.

        It is bound to the exact Authentik account (pk) it is made for:
        mcp-manager records "live" with that pk BEFORE Gitea is touched and
        the Gitea user id before any token is minted, so every later
        revocation, and the reconcile for an account deleted by hand, revokes
        exactly that Gitea user. A Gitea user recorded for an OLDER account of
        the same name is purged first (its grants never pass to the new
        owner); a Gitea user of that name the stack did not record is refused
        and never touched. Earlier rzfz-gw-* tokens are deleted."""
        scopes = sorted({s for s in scopes or [] if isinstance(s, str)})
        if not scopes or not set(scopes) <= GITEA_SCOPES:
            raise TechnicalUserError(f"choose Gitea token scopes from {sorted(GITEA_SCOPES)}")
        if self._gitea is None:
            raise TechnicalUserError("Gitea is not enabled on this box (the gitea module is off)")
        u = self._mcp_live(name)
        pk = u.get("pk")
        attrs = u.get("attributes") or {}
        if "mcp-integration:gitea" not in (attrs.get("rzfz_bindings") or []):
            raise TechnicalUserError(f"{name} is not bound to mcp-integration:gitea")
        owner = self._owner_username(attrs.get("rzfz_owner"))
        if not owner:
            raise TechnicalUserError(f"the owner of {name} is not an active person; reassign it first")
        rec = self._gitea_record(name) or {}
        rec_id, rec_pk = rec.get("gitea_user_id"), rec.get("authentik_pk")
        known_id = rec_id if (rec_id is not None and rec_pk == pk) else None
        try:
            if rec_id is not None and rec_pk != pk:
                # The Gitea user of an older Authentik account of this name.
                if not self._mcp.set_gitea_state(name, "owed"):
                    raise TechnicalUserError(f"the MCP gateway was not reached; nothing was changed for {name}")
                self._gitea.purge(name, expected_id=rec_id)
            if not self._mcp.set_gitea_state(name, "live", authentik_pk=pk, gitea_user_id=known_id):
                raise TechnicalUserError(f"the MCP gateway was not reached; no Gitea service user was made for {name}")
            gid = self._gitea.ensure_service_user(name, owner, known_id=known_id)
            if gid != known_id and not self._mcp.set_gitea_state(name, "live", authentik_pk=pk, gitea_user_id=gid):
                raise TechnicalUserError(f"the MCP gateway did not record the Gitea user of {name}; no token was "
                                         "made. Delete that Gitea user by hand before trying again")
            self._gitea.delete_tokens(name)
            token = self._gitea.mint_token(name, scopes)
        except TechnicalUserError:
            raise
        except Exception as e:  # noqa: BLE001 — GiteaAdminError names the call and status only
            detail = str(e) if isinstance(e, GiteaAdminError) else type(e).__name__
            raise TechnicalUserError(f"no Gitea service user for {name}: {detail}") from None
        try:
            self._mcp.store_credential(name, "gitea", {"api_key": token})
        except Exception as e:  # noqa: BLE001 — e.g. revoked meanwhile: the token must not outlive this
            # Owed first: if the revoke below fails too, the reconcile finishes it.
            self._mcp.set_gitea_state(name, "owed")
            try:
                self._gitea_revoke_one(name, {"gitea_user_id": gid})
            except Exception:  # noqa: BLE001 — still owed on the gateway: the reconcile revokes it
                pass
            raise TechnicalUserError(f"the MCP gateway did not store the Gitea token of {name} "
                                     f"({type(e).__name__}); the token is revoked again") from None
        self._log("technical_user.gitea_user", name, actor, f"scopes={scopes}")


class OwnerSweep:
    """Runs `enforce_owners` every OWNER_SWEEP_INTERVAL seconds, whatever the
    OpenBao profile says (a technical user's Authentik tokens exist without
    it). `registry_factory()` returns a registry, or None while Authentik is
    not configured (no bootstrap token): then the tick does nothing.

    With `status_path` every tick writes `LAST_RUN\t<epoch>` and one
    `WARN\t<text>` line per account that failed (and one when Authentik
    could not be read), which `rzfz status` shows."""

    def __init__(self, registry_factory, interval=OWNER_SWEEP_INTERVAL, first_delay=60.0, log=None,
                 status_path=None, clock=time.time):
        self._factory, self._interval, self._first = registry_factory, interval, first_delay
        self._log, self._status, self._clock = log, status_path, clock
        self.last_result = None

    def _write_status(self, warnings):
        if not self._status:
            return
        import os
        d = os.path.dirname(self._status)
        os.makedirs(d, exist_ok=True)
        tmp = self._status + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("LAST_RUN\t%d\n" % int(self._clock()))
            f.writelines("WARN\t%s\n" % " ".join(w.split()) for w in warnings)
        os.replace(tmp, self._status)

    def run_once(self):
        reg = self._factory()
        if reg is None:
            return []
        try:
            self.last_result = reg.enforce_owners()
        except (AuthentikUnavailable, OSError) as e:
            if self._log:
                self._log.warning("technical users: owner check skipped, Authentik unavailable (%s)",
                                  type(e).__name__)
            self._write_status([f"owner check skipped: Authentik could not be read ({type(e).__name__})"])
            return []
        mcp_warn = []
        try:
            if hasattr(reg, "reconcile_mcp"):
                reg.reconcile_mcp()
        except Exception as e:  # noqa: BLE001 — the owner check itself has run
            mcp_warn = [f"MCP gateway not reconciled ({type(e).__name__}); retried every sweep"]
        unreadable = getattr(reg, "last_unreadable", ())
        self._write_status(mcp_warn + [
            f"{n}: record unreadable ({err}) - the owner check skips it; fix its attributes "
            "in Authentik" if n in unreadable else
            f"{n}: revocation unfinished ({err}) - retried every sweep; "
            "Retry revoke on Settings > Technical users" for n, err in reg.last_failures])
        return self.last_result

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="technical-user-owner-check").start()

    def _loop(self):
        time.sleep(self._first)
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — the loop must survive; the next tick retries
                if self._log:
                    self._log.exception("technical users: owner check failed")
            time.sleep(self._interval)


# ---------------------------------------------------------------------------
# The real Authentik adapter
# ---------------------------------------------------------------------------

_USERS = "/api/v3/core/users/"
_TOKENS = "/api/v3/core/tokens/"
_PAGE = 200
_MAX_PAGES = 500


class AuthentikAPI:
    """Authentik REST with AUTHENTIK_BOOTSTRAP_TOKEN from the mounted .env
    (`env` is what config_manager.read_env() returns). Every error is raised
    as AuthentikUnavailable; no response body is ever put into a message
    (a token listing or a service-account answer carries secrets)."""

    def __init__(self, env: dict, base_url: str = "http://authentik-server:9000", session=None, token=None):
        self._token = token if token is not None else (env or {}).get("AUTHENTIK_BOOTSTRAP_TOKEN", "")
        if not self._token:
            raise AuthentikUnavailable("AUTHENTIK_BOOTSTRAP_TOKEN is empty")
        self._base = base_url.rstrip("/")
        if session is None:
            import requests
            session = requests.Session()
            session.trust_env = False
        self._s = session
        self._listing = None

    def _req(self, method, path, *, params=None, json=None, ok=(200, 201, 204), token=None, with_status=False):
        if method != "GET":
            self._listing = None
        try:
            r = self._s.request(method, self._base + path, params=params, json=json, timeout=15,
                                allow_redirects=False,
                                headers={"Authorization": f"Bearer {token or self._token}",
                                         "Accept": "application/json"})
        except Exception as e:  # noqa: BLE001 — requests' errors, and anything a session raises
            raise AuthentikTransient(f"{method} {path}: {type(e).__name__}") from None
        if r.status_code not in ok:
            cls = AuthentikTransient if r.status_code >= 500 else AuthentikUnavailable
            raise cls(f"{method} {path}: HTTP {r.status_code}")
        if r.status_code == 204 or not r.content:
            body = {}
        else:
            try:
                body = r.json()
            except ValueError:
                raise AuthentikUnavailable(f"{method} {path}: not JSON") from None
        return (r.status_code, body) if with_status else body

    def _pages(self, path, params):
        """Every page of a listing, or AuthentikUnavailable. COMPLETE or
        nothing (the owner check revokes on what this returns): every page
        must carry its pagination, the total must not move between the first
        and the last page (offset paging over a listing that changes shifts
        users across page borders), and the pages must add up to it."""
        out, total = [], None
        for page in range(1, _MAX_PAGES + 1):
            d = self._req("GET", path, params=dict(params, page=page, page_size=_PAGE))
            res, pag = d.get("results"), d.get("pagination")
            if not isinstance(res, list) or not isinstance(pag, dict) or not isinstance(pag.get("count"), int):
                raise AuthentikUnavailable(f"GET {path}: page {page} has no results or no pagination")
            if total is None:
                total = pag["count"]
            elif pag["count"] != total:
                raise AuthentikUnavailable(f"GET {path}: the listing changed while it was read")
            out.extend(x for x in res if isinstance(x, dict))
            if not pag.get("next"):
                if len(out) != total:
                    raise AuthentikUnavailable(f"GET {path}: {len(out)} of {total} rows read")
                return out
        raise AuthentikUnavailable(f"GET {path}: more than {_MAX_PAGES} pages")

    def _all_users(self):
        """The whole user listing, read once per adapter (one adapter per
        request or sweep tick); any write through the adapter drops it."""
        if self._listing is None:
            users = self._pages(_USERS, {"include_groups": "false"})
            if not any(u.get("is_active") and u.get("type") == "internal" for u in users):
                raise AuthentikUnavailable("the user listing holds no active person")
            self._listing = users
        return self._listing

    @staticmethod
    def _user(u):
        return {"pk": u.get("pk"), "uid": u.get("uid"), "username": u.get("username"),
                "path": u.get("path"), "type": u.get("type"), "is_active": bool(u.get("is_active")),
                "attributes": u.get("attributes") or {}}

    @staticmethod
    def _person(u):
        return {k: u[k] for k in ("pk", "uid", "username", "type", "is_active")}

    def get_user(self, username):
        d = self._req("GET", _USERS, params={"username": username, "include_groups": "false"})
        hits = [u for u in d.get("results") or [] if isinstance(u, dict) and u.get("username") == username]
        return self._user(hits[0]) if len(hits) == 1 else None

    def get_person(self, username):
        u = self.get_user(username)
        return None if u is None else self._person(u)

    def person_for_uid(self, uid):
        """The user with this uid, or None. The uid is computed (Authentik
        cannot filter on it), so the whole, complete listing is read."""
        hits = [u for u in self._all_users() if uid and u.get("uid") == uid]
        return self._person(self._user(hits[0])) if len(hits) == 1 else None

    def person_by_pk(self, pk):
        """ONE user by pk (GET /core/users/<pk>/). None ONLY when Authentik
        answers 404; any other answer that is not that user (no pk, another
        pk, an empty body) is AuthentikUnavailable — undecided, never
        "owner deleted"."""
        status, d = self._req("GET", f"{_USERS}{int(pk)}/", ok=(200, 404), with_status=True)
        if status == 404:
            return None
        if not isinstance(d, dict) or d.get("pk") != pk:
            raise AuthentikUnavailable(f"GET {_USERS}{pk}/: the answer is not that user")
        return self._person(self._user(d))

    def user_by_pk(self, pk):
        """ONE user record by pk (GET /core/users/<pk>/), any type. None ONLY
        when Authentik answers 404; any other answer that is not that user is
        AuthentikUnavailable (undecided)."""
        status, d = self._req("GET", f"{_USERS}{int(pk)}/", ok=(200, 404), with_status=True)
        if status == 404:
            return None
        if not isinstance(d, dict) or d.get("pk") != pk:
            raise AuthentikUnavailable(f"GET {_USERS}{pk}/: the answer is not that user")
        return self._user(d)

    def username_for_uid(self, uid):
        p = self.person_for_uid(uid)
        return p["username"] if p else None

    def list_person_uids(self):
        """#2476 D9: the uid of EVERY user that exists (any type, active or
        not: only a user who no longer exists is purged, a deactivated one is
        not), as the purge's Listing. COMPLETE or an errored Listing: any
        error, timeout, unparsable or incomplete page decides nothing."""
        from app.services.openbao_purge import Listing
        try:
            users = self._pages(_USERS, {"include_groups": "false"})
        except Exception as e:  # noqa: BLE001 — AuthentikUnavailable and anything a session raises
            return Listing(frozenset(), None, type(e).__name__)
        uids = frozenset(u["uid"] for u in users if isinstance(u.get("uid"), str) and u["uid"])
        # a row without a uid makes the listing short of its count: refused
        return Listing(uids, len(users))

    def user_deleted(self, pk):
        """#2476 D9, A17's owner rule: True ONLY when Authentik answers 404
        for this pk; False when the user exists; None (undecided) for any
        other answer, a malformed pk or an error."""
        try:
            pk = int(pk)
            return self.user_by_pk(pk) is None
        except Exception:  # noqa: BLE001 — undecided is never "deleted"
            return None

    def list_users(self, path):
        return [self._user(u) for u in self._pages(_USERS, {"path": path, "include_groups": "false"})
                if u.get("path") == path]

    def _pk(self, username):
        u = self.get_user(username)
        if not u:
            raise AuthentikUnavailable(f"user {username} vanished")
        return u["pk"]

    def create_service_account(self, username, path, attributes):
        d = self._req("POST", _USERS + "service_account/",
                      json={"name": username, "create_group": False, "expiring": False})
        pk = d.get("user_pk")
        if not isinstance(pk, int):
            raise AuthentikUnavailable("service account: no user_pk in the answer")
        try:
            self._req("PATCH", f"{_USERS}{pk}/", json={"path": path, "attributes": attributes})
        except AuthentikUnavailable:
            # Never leave an account outside rzfz/technical behind: _managed
            # would refuse to touch it and create() would say "exists".
            try:
                self._req("DELETE", f"{_USERS}{pk}/")
            except AuthentikUnavailable:
                pass
            raise
        toks = [t.get("identifier") for t in self._pages(_TOKENS, {"user__username": username})
                if (t.get("user_obj") or {}).get("username") in (None, username)]
        user = self.get_user(username) or {"pk": pk}
        return user, (toks[0] if toks else None)

    def update_attributes(self, username, attributes):
        self._req("PATCH", f"{_USERS}{self._pk(username)}/", json={"attributes": attributes})

    def deactivate(self, username):
        self._req("PATCH", f"{_USERS}{self._pk(username)}/", json={"is_active": False})

    def activate(self, username):
        self._req("PATCH", f"{_USERS}{self._pk(username)}/", json={"is_active": True})

    def delete_tokens(self, username):
        for t in self._pages(_TOKENS, {"user__username": username}):
            owner = (t.get("user_obj") or {}).get("username")
            ident = t.get("identifier")
            if owner == username and isinstance(ident, str) and ident:
                self._req("DELETE", f"{_TOKENS}{ident}/")

    def add_to_group(self, username, group):
        gs = [g for g in self._pages("/api/v3/core/groups/", {"name": group}) if g.get("name") == group]
        if len(gs) != 1:
            raise AuthentikUnavailable(f"group {group!r} not found")
        self._req("POST", f"/api/v3/core/groups/{gs[0]['pk']}/add_user/", json={"pk": self._pk(username)})
