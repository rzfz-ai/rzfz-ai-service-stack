# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2476 D9 (ruling 12) — deleting an Authentik user purges secret/users/<sub>/.

Every 10 minutes: list the person aliases on OpenBao's OIDC and token mounts
(their names are the OIDC sub), list the uids of the Authentik users that
exist, and purge an alias whose user is gone. Fail-safes:
  * a listing only nominates: each candidate is purged only after a direct
    lookup of its Authentik pk (the alias metadata ak_pk, from the OIDC
    login) answers 404 — A17's owner rule. Offset paging can shift a live
    user out of a listing that passes every completeness check; no pk, or
    any other answer, decides nothing;
  * never purge on an errored, timed-out, empty or incompletely paginated
    Authentik listing (WARN, nothing purged);
  * at most max(1, 5 % of known aliases) per pass; above that purge NONE,
    FAIL in rzfz status, and wait for `rzfz openbao confirm-purge`;
  * a purge soft-deletes: the secrets move to secret/purged/<sub>/<stamp>/,
    the user's tokens are revoked, the entity and its aliases deleted; a
    purged tree is destroyed only after PURGE_GRACE (7 days);
  * off until A19's box gate proved sub == X-Authentik-Uid
    (OPENBAO_SUB_IS_UID=true); until then it purges nothing and says so.
Each purge is audited (category identity, D12). An Authentik deletion event is
not used as the hook: a missed notification would never be retried.

One run at a time per state directory (an flock), so the Portal's loop and
`rzfz openbao confirm-purge` never purge the same user twice."""
from __future__ import annotations

import fcntl
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_log = logging.getLogger(__name__)
PURGE_GRACE = timedelta(days=7)
STAMP = "%Y%m%dT%H%M%SZ"
PURGE_INTERVAL = 600.0


@dataclass(frozen=True)
class Listing:
    uids: frozenset
    count: int | None
    error: str | None = None


@dataclass(frozen=True)
class PurgePlan:
    action: str            # "none" | "purge" | "hold" | "refuse"
    subs: tuple = ()
    reason: str = ""


def _cap(n_aliases: int, cap_fraction: float) -> int:
    return max(1, math.floor(n_aliases * cap_fraction))


def confirm_plan(plan: PurgePlan, aliases: dict, deleted, cap_fraction: float = 0.05):
    """(plan, undecided): the candidates of `plan` that a direct pk lookup
    (`deleted(pk)` -> True only on 404) proved deleted, capped again; the
    others are left alone and named as undecided."""
    if plan.action not in ("purge", "hold"):
        return plan, ()
    gone, undecided = [], []
    for sub in plan.subs:
        pk = (aliases.get(sub) or {}).get("pk")
        verdict = deleted(pk) if pk else None
        if verdict is True:
            gone.append(sub)
        elif verdict is None:
            undecided.append(sub)
    if not gone:
        return PurgePlan("none"), tuple(undecided)
    cap = _cap(len(aliases), cap_fraction)
    if len(gone) > cap:
        return PurgePlan("hold", tuple(gone), f"{len(gone)} OpenBao users are deleted in Authentik, above the per-run "
                                              f"cap of {cap} - nothing purged; confirm with: rzfz openbao confirm-purge"), \
            tuple(undecided)
    return PurgePlan("purge", tuple(gone)), tuple(undecided)


def plan_purge(alias_subs, listing: Listing, cap_fraction: float = 0.05) -> PurgePlan:
    if listing.error:
        return PurgePlan("refuse", (), f"Authentik listing failed ({listing.error}) - nothing purged")
    if not listing.uids:
        return PurgePlan("refuse", (), "Authentik listing returned zero users - nothing purged")
    if listing.count is None or len(listing.uids) < listing.count:
        return PurgePlan("refuse", (), f"Authentik listing incomplete ({len(listing.uids)} of {listing.count}) "
                                       "- nothing purged")
    orphans = tuple(sorted(set(alias_subs) - set(listing.uids)))
    if not orphans:
        return PurgePlan("none")
    cap = _cap(len(alias_subs), cap_fraction)
    if len(orphans) > cap:
        return PurgePlan("hold", orphans, f"{len(orphans)} OpenBao users look deleted, above the per-run cap of "
                                          f"{cap} - nothing purged; confirm with: rzfz openbao confirm-purge")
    return PurgePlan("purge", orphans)


class PurgeReconciler:
    def __init__(self, bao, authentik, state_dir, *, enabled, audit=None, now=None):
        self._bao, self._ak, self._dir = bao, authentik, Path(state_dir)
        self._enabled, self._audit = bool(enabled), audit
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _status(self, result, text, pending=()):
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / "purge.status.tmp"
        text = " ".join(str(text).split())
        tmp.write_text(f"LAST_RUN\t{int(self._now().timestamp())}\nRESULT\t{result}\t{text}\n"
                       + "".join(f"PENDING\t{s}\n" for s in pending), encoding="utf-8")
        os.replace(tmp, self._dir / "purge.status")

    def run_once(self, confirm=frozenset()) -> PurgePlan:
        if not self._enabled:
            self._status("DISABLED", "off until sub == X-Authentik-Uid is proven on this Authentik "
                                     "(OPENBAO_SUB_IS_UID=false); deleted users' paths stay in place")
            return PurgePlan("refuse", (), "disabled")
        return self.locked(lambda: self._run_locked(frozenset(confirm)))

    def locked(self, fn):
        """`fn()` under the state directory's flock: the Portal's loop and the
        operator commands never purge at the same time."""
        self._dir.mkdir(parents=True, exist_ok=True)
        with open(self._dir / "purge.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return fn()

    def _run_locked(self, confirm) -> PurgePlan:
        try:
            aliases = self._bao.person_aliases()
        except Exception as e:  # noqa: BLE001
            self._status("WARN", f"OpenBao aliases not readable ({type(e).__name__}) - nothing purged")
            return PurgePlan("refuse", (), "openbao")
        plan, undecided = confirm_plan(plan_purge(aliases, self._ak.list_person_uids()), aliases,
                                       self._ak.user_deleted)
        if plan.action == "purge":
            subs = plan.subs
        elif plan.action == "hold":
            subs = tuple(s for s in plan.subs if s in confirm)
        else:
            subs = ()
        now = self._now()
        stamp = now.strftime(STAMP)
        failed, done = [], []
        for sub in subs:
            try:
                moved = self._bao.soft_delete_user(sub, aliases[sub]["entity_id"], stamp,
                                                   entity_ids=aliases[sub].get("entity_ids"))
            except Exception as e:  # noqa: BLE001 — one user never stops the others
                failed.append(f"{sub} ({type(e).__name__})")
                _log.warning("openbao purge: %s failed (%s)", sub, type(e).__name__)
                continue
            done.append(sub)
            if self._audit:
                self._audit(category="identity", action="openbao.user_purged", target=sub, user="purge-reconcile",
                            detail=f"{moved} secret(s) moved to secret/purged/{sub}/{stamp}/, tokens revoked")
        destroyed = 0
        try:
            for sub, st in self._bao.list_purged():
                if now - datetime.strptime(st, STAMP).replace(tzinfo=timezone.utc) >= PURGE_GRACE:
                    self._bao.destroy_purged(sub, st)
                    destroyed += 1
        except Exception as e:  # noqa: BLE001
            failed.append(f"destroying purged trees ({type(e).__name__})")
        pending = tuple(s for s in plan.subs if s not in done) if plan.action == "hold" else ()
        failed += [f"{s} (deletion not confirmed by a pk lookup; if the user is gone: rzfz openbao purge-user {s})"
                   for s in undecided]
        if plan.action == "refuse":
            self._status("WARN", plan.reason)
        elif pending:
            self._status("FAIL", plan.reason, pending)
        elif failed:
            self._status("WARN", f"{len(done)} purged; not purged: {', '.join(failed)}")
        else:
            self._status("OK", f"{len(done)} purged, {destroyed} destroyed after 7 days")
        return plan

    def start(self, interval: float = PURGE_INTERVAL) -> None:
        threading.Thread(target=self._loop, args=(interval,), daemon=True, name="openbao-purge").start()

    def _loop(self, interval: float) -> None:
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — logged; the status row reports a stale LAST_RUN
                _log.exception("openbao purge: the run failed")
            time.sleep(interval)


class AuthentikUids:
    """The Authentik half, built per call from the current .env (a bootstrap
    token rotation is picked up; a missing token is an errored listing)."""

    def __init__(self, env_source):
        self._env_source = env_source

    def list_person_uids(self) -> Listing:
        from app.services.technical_users import AuthentikAPI
        try:
            api = AuthentikAPI(self._env_source())
        except Exception as e:  # noqa: BLE001
            return Listing(frozenset(), None, type(e).__name__)
        return api.list_person_uids()

    def user_deleted(self, pk):
        from app.services.technical_users import AuthentikAPI
        try:
            api = AuthentikAPI(self._env_source())
        except Exception:  # noqa: BLE001
            return None
        return api.user_deleted(pk)


def sub_gate_proven(env) -> bool:
    """A19's box gate: sub == X-Authentik-Uid proven (OPENBAO_SUB_IS_UID=true)."""
    return str((env or {}).get("OPENBAO_SUB_IS_UID", "")).strip().lower() == "true"


def build_purge_reconciler(env, *, bao, authentik, state_dir, audit=None) -> PurgeReconciler:
    """Enabled only by the gate switch OPENBAO_SUB_IS_UID=true (A19 Step 6)."""
    return PurgeReconciler(bao, authentik, state_dir, audit=audit, enabled=sub_gate_proven(env))


def _standalone(stack_root):
    """The reconciler for the CLI, built from the stack .env, without the app
    (create_app would start the Portal's own loops in this process)."""
    from razzfazz_common.env_utils import read_env_file

    from app.services.openbao_admin import OpenBaoHTTP
    env_path = os.path.join(stack_root, ".env")

    def env():
        return read_env_file(env_path)

    audit = None
    try:
        from app.services.audit_logger import AuditLogger
        logger = AuditLogger(os.environ.get("AUDIT_LOG_PATH", "/var/log/razzfazz/config-audit.log"))

        def audit(*, category, action, target, user, detail="", outcome="success"):
            logger.log("config.change", user=user, category=category, action=action, target=target,
                       detail=detail, risk="caution", outcome=outcome)
    except Exception:  # noqa: BLE001 — a purge is still refused below without an audit trail
        audit = None
    return build_purge_reconciler(env(), bao=OpenBaoHTTP(), authentik=AuthentikUids(env),
                                  state_dir=os.path.join(stack_root, ".openbao-state"), audit=audit)


def main(stack_root=None, rec=None, ask=input) -> int:
    """`rzfz openbao confirm-purge`: show the held aliases; purge only on `PURGE <n>`."""
    stack_root = stack_root or os.environ.get("STACK_ROOT", "/stack")
    rec = rec or _standalone(stack_root)
    if not rec._enabled:
        print("the purge reconcile is off: OPENBAO_SUB_IS_UID is not true on this box")
        return 1
    if rec._audit is None:
        print("the audit log is not writable here; nothing purged")
        return 1
    aliases = rec._bao.person_aliases()
    plan, undecided = confirm_plan(plan_purge(aliases, rec._ak.list_person_uids()), aliases, rec._ak.user_deleted)
    for sub in undecided:
        print(f"  not confirmed deleted (left alone): {sub}")
    if plan.action != "hold":
        print(f"nothing is held ({plan.action}{': ' + plan.reason if plan.reason else ''})")
        return 0
    for sub in plan.subs:
        print(f"  {sub}")
    answer = ask(f"Type PURGE {len(plan.subs)} to soft-delete these users' OpenBao secrets "
                 f"(restorable from secret/purged/ for 7 days): ")
    if answer.strip() != f"PURGE {len(plan.subs)}":
        print("aborted - nothing purged")
        return 1
    after = rec.run_once(confirm=frozenset(plan.subs))
    print(f"done: {after.action}; see rzfz status (openbao purge)")
    return 0


def operator_name(raw=None) -> str:
    """The operator for the audit log: `rzfz openbao purge-user` passes the
    host login as RZFZ_BY; cleaned again here (a login name only)."""
    import re as _re
    raw = os.environ.get("RZFZ_BY", "") if raw is None else raw
    return _re.sub(r"[^A-Za-z0-9._-]", "", str(raw))[:64] or "unknown"


def purge_user(sub, stack_root=None, rec=None, ask=input, by=None) -> int:
    """`rzfz openbao purge-user <sub>`: the operator's path for ONE deleted
    user whose deletion the reconcile cannot confirm (no stored pk: no sign-in
    since the pk was recorded, or a token-mount alias). Refused unless the
    purge is enabled, the alias exists, a COMPLETE Authentik listing lacks
    this sub, and — when a pk is stored — its lookup does not find the user.
    Then a typed confirmation; a soft delete like the reconcile's (restorable
    for 7 days), audited as the operator's."""
    import re as _re
    if not isinstance(sub, str) or not _re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sub):
        print("refusing: not an OpenBao user id (the OIDC sub)")
        return 2
    stack_root = stack_root or os.environ.get("STACK_ROOT", "/stack")
    rec = rec or _standalone(stack_root)
    if not rec._enabled:
        print("the purge is off: OPENBAO_SUB_IS_UID is not true on this box")
        return 1
    if rec._audit is None:
        print("the audit log is not writable here; nothing purged")
        return 1
    aliases = rec._bao.person_aliases()
    if sub not in aliases:
        print(f"no OpenBao user {sub}")
        return 1
    listing = rec._ak.list_person_uids()
    plan = plan_purge(list(aliases), listing)
    if plan.action == "refuse":
        print(f"nothing purged: {plan.reason}")
        return 1
    if sub in listing.uids:
        print(f"nothing purged: {sub} is a user in Authentik")
        return 1
    pk = aliases[sub].get("pk")
    if pk and rec._ak.user_deleted(pk) is not True:
        print(f"nothing purged: Authentik does not answer 'not found' for this user's id ({pk})")
        return 1
    word = f"PURGE {sub[:12]}"
    answer = ask(f"{sub} is not in Authentik{'' if pk else ' (no user id stored to confirm it)'}. "
                 f"Type {word} to soft-delete their OpenBao secrets (restorable from secret/purged/ for 7 days): ")
    if answer.strip() != word:
        print("aborted - nothing purged")
        return 1

    who = operator_name(by)

    def go():
        # re-checked under the lock: the prompt was the window
        now_aliases = rec._bao.person_aliases()
        now_listing = rec._ak.list_person_uids()
        if sub not in now_aliases or plan_purge(list(now_aliases), now_listing).action == "refuse" \
                or sub in now_listing.uids:
            return None
        now = rec._now()
        stamp = now.strftime(STAMP)
        moved = rec._bao.soft_delete_user(sub, now_aliases[sub]["entity_id"], stamp,
                                          entity_ids=now_aliases[sub].get("entity_ids"))
        rec._audit(category="identity", action="openbao.user_purged", target=sub, user=f"operator {who} (purge-user)",
                   detail=f"{moved} secret(s) moved to secret/purged/{sub}/{stamp}/, tokens revoked; "
                          "confirmed by the operator, no Authentik pk lookup")
        return moved
    moved = rec.locked(go)
    if moved is None:
        print("nothing purged: the user or the Authentik listing changed while you answered")
        return 1
    print(f"done: {moved} secret(s) moved to secret/purged/{sub}/")
    return 0


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["confirm"]:
        raise SystemExit(main())
    if len(sys.argv) == 3 and sys.argv[1] == "purge-user":
        raise SystemExit(purge_user(sys.argv[2]))
    raise SystemExit("usage: python -m app.services.openbao_purge confirm | purge-user <sub>")
