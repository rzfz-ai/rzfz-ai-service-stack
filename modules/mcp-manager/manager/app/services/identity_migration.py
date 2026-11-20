# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Re-key pre-2026.11 rows by the OIDC sub, or quarantine them (#2472;
design record §2.3, controller rulings 1 and I1).

Migration 4 adds the sub keys and leaves every existing row unattributed
(NULL key: no gateway or portal read matches it). This module decides each
such row exactly once, on evidence only:

* **Instances** (`mcp_instances`): re-keyed to `user_id` only when `user_id`
  is the uid of an active internal Authentik user AND the row's `created_at`
  is not earlier than that user's `date_joined`. Everything else is
  quarantined (and its container removed: it holds decrypted credentials and
  nothing may reach it any more).
* **Bindings** (`mcp_type_bindings`): take the sub of the re-keyed instance
  with the same `(user_slug, mcp_id)`, only if their own `created_at` is not
  earlier than that owner's `date_joined`; a binding without such an instance
  is quarantined.
* **Credentials** (`mcp_user_secrets`): decided at the owner's next portal
  visit, re-keyed only when ALL hold — the anchored X-Authentik-Email
  resolves to exactly one active internal user, that user is the visitor; the
  visitor is the only active Authentik user whose username maps to the row's
  slug; the row is not older than the visitor's `date_joined`; and an
  instance with the same `(user_slug, mcp_id)` is re-keyed to the visitor, if
  there is one. A row whose instance was quarantined follows it. Anything
  else is quarantined.

Never inferred from the slug alone, a username or an unverified email. A
directory failure (or no read-only token) decides NOTHING: the rows stay
unattributed and the next run tries again.
"""
from __future__ import annotations

import logging
import time

from app.services.gateway_identity import AuthentikDirectory, DirectoryError, _parse_ts

logger = logging.getLogger(__name__)

#: Minimum seconds between two instance re-key attempts in one process
#: (each attempt lists every Authentik user).
RETRY_SECONDS = 60
#: Minimum seconds between two successful departed-person credential sweeps
#: in one process (#2677). The first runs at start, i.e. on every upgrade.
SWEEP_SECONDS = 3600


def _persons_by_uid(users) -> dict:
    return {str(u["uid"]): u for u in users if AuthentikDirectory._person(u)}


def _joined(user):
    """The user's date_joined, or raise: a listing without it cannot bound
    anything, so the whole run must decide nothing (never quarantine
    everyone because the token cannot see the field)."""
    ts = _parse_ts(user.get("date_joined"))
    if ts is None:
        raise DirectoryError("authentik listing carries no date_joined")
    return ts


def _not_before(created_at, joined) -> bool:
    return created_at is not None and joined is not None and created_at >= joined


def _listed(users, uid: str) -> bool:
    """#2677: is `uid` in the listing at all (deleted when it is not)?"""
    return any(str(u.get("uid")) == uid for u in users)


def _revoke_person_tokens(db, uid: str, why: str, cred_store=None, users=None) -> None:
    """A person the directory does not know as active keeps no gateway token
    (#2086 fix round 2): the same run that quarantines their rows revokes the
    tokens, so the very next gateway call is a 401. Their stored credentials
    follow retire_person (#2677): at once for a deleted account, after 30 days
    for a deactivated one."""
    from app.services.gateway_identity import retire_person
    uid = (uid or "").strip()
    if not uid or uid.startswith("~"):
        return
    try:
        res = retire_person(db, uid, None if users is None else _listed(users, uid),
                            cred_store=cred_store)
    except Exception:  # noqa: BLE001 — the gateway's own owner check still refuses
        logger.exception("could not revoke the gateway tokens of an inactive owner (%s)", why)
        return
    if res["tokens"] or res["rows"]:
        logger.info("revoked %d gateway token(s) and deleted %d stored credential(s) of an owner "
                    "who is not an active uid (%s)", res["tokens"], res["rows"], why)


def rekey_instances(db, directory, remove_container=None, cred_store=None) -> dict:
    """Decide every unattributed instance, then every unattributed binding.
    Returns a summary; {'error': …} and no decision on a directory failure."""
    out = {"rekeyed": 0, "quarantined": 0, "bindings_rekeyed": 0, "bindings_quarantined": 0}
    if not directory:
        return {**out, "error": "no directory"}
    try:
        # usable_listing: a listing with no active person at all decides
        # nothing (B8 fix round 3) — it would quarantine every row and
        # remove every container.
        users = directory.usable_listing()
        persons = _persons_by_uid(users)
        joined = {uid: _joined(u) for uid, u in persons.items()}
    except DirectoryError as e:
        logger.warning("instance re-key postponed: %s", e)
        return _record(db, {**out, "error": str(e)})

    # #2572: legacy cognee accounts FIRST, while every instance row is still
    # in mcp_instances (quarantining moves rows out, losing the evidence).
    out.update(decide_cognee_accounts(db, persons, joined, cred_store=cred_store, users=users))

    for row in db.pending_instances():
        uid = str(row.get("user_id") or "").strip()
        if uid in persons and _not_before(row.get("created_at"), joined[uid]):
            done = db.rekey_row("mcp_instances", row["id"], uid)
            if done:
                out["rekeyed"] += 1
            if done is not None:
                continue
            reason = "superseded"
        else:
            reason = ("created_before_owner_joined" if uid in persons
                      else "owner_not_an_active_uid")
        if reason == "owner_not_an_active_uid":
            _revoke_person_tokens(db, uid, "instance quarantined", cred_store, users)
        moved = db.quarantine_row("mcp_instances", row["id"], reason)
        if moved is None:
            continue
        out["quarantined"] += 1
        if remove_container and row.get("container_name"):
            try:
                remove_container(row["container_name"])
            except Exception:  # noqa: BLE001 — the row is already out of reach
                logger.warning("quarantine: could not remove container %s",
                               row["container_name"])

    for row in db.pending_bindings():
        inst = db.rekeyed_instance_for(row.get("user_slug"), row.get("mcp_id"))
        owner = inst["owner_sub"] if inst else None
        if owner and owner in joined and _not_before(row.get("created_at"), joined[owner]):
            done = db.rekey_row("mcp_type_bindings", row["id"], owner)
            if done:
                out["bindings_rekeyed"] += 1
            if done is not None:
                continue
            reason = "superseded"
        elif not inst and db.has_pending_instance_for(row.get("user_slug"), row.get("mcp_id")):
            continue  # its instance is still undecided (a concurrent run)
        else:
            reason = ("no_rekeyed_instance" if not inst else
                      "owner_not_an_active_uid" if owner not in joined else
                      "created_before_owner_joined")
        if db.quarantine_row("mcp_type_bindings", row["id"], reason) is not None:
            out["bindings_quarantined"] += 1

    if not db.pending_instances() and not db.pending_bindings() \
            and not db.pending_cognee_accounts():
        db.mark_instances_rekeyed()
    logger.info("identity re-key: %s", out)
    return _record(db, out)


def decide_cognee_accounts(db, persons: dict, joined: dict, cred_store=None,
                           users=None) -> dict:
    """#2572: a legacy cognee account (derived from a SLUG) is mapped to one
    sub only on the instance evidence: every cognee-tier instance ever
    provisioned under that slug (destroyed ones included) names the same
    `user_id`, that is an active internal uid, and the first provision is not
    older than the owner's `date_joined`. Anything else is quarantined: the
    account stays in cognee, and nobody is ever logged into it again."""
    res = {"cognee_attributed": 0, "cognee_quarantined": 0}
    for slug in db.pending_cognee_accounts():
        rows = db.cognee_evidence(slug)
        uids = {str(r.get("user_id") or "").strip() for r in rows}
        first = min((r["created_at"] for r in rows if r.get("created_at")), default=None)
        uid = next(iter(uids)) if len(uids) == 1 else ""
        if not rows:
            reason = "no_evidence"
        elif len(uids) != 1:
            reason = "several_owners"
        elif uid not in persons:
            reason = "owner_not_an_active_uid"
            _revoke_person_tokens(db, uid, "cognee account quarantined", cred_store, users)
        elif not _not_before(first, joined[uid]):
            reason = "created_before_owner_joined"
        else:
            done = db.decide_cognee_account(slug, uid)
            if done:
                res["cognee_attributed"] += 1
            if done is not None:
                continue
            reason = "superseded"  # this uid already has a mapped account
        if db.decide_cognee_account(slug, None, reason):
            res["cognee_quarantined"] += 1
    return res


def attribute_credentials(db, directory, *, slug: str, sub: str, email: str,
                          cred_store=None) -> dict:
    """Decide the visitor's unattributed credential rows under `slug`.
    `sub` and `email` come from the anchored X-Authentik-Uid/-Email only."""
    out = {"rekeyed": 0, "quarantined": 0, "waiting": 0}
    pending = db.pending_secrets_for_slug(slug)
    if not pending or not sub or not directory:
        return out
    try:
        users = directory.usable_listing()
        persons = _persons_by_uid(users)
        me = persons.get(sub)
        my_joined = _joined(me) if me else None
    except DirectoryError as e:
        logger.warning("credential re-key postponed for %s: %s", slug, e)
        return _record(db, {**out, "error": str(e)})

    from razzfazz_common.user_slug import make_user_slug
    want = (email or "").strip().lower()
    by_email = [u for u in persons.values()
                if want and str(u.get("email") or "").strip().lower() == want]
    email_ok = len(by_email) == 1 and str(by_email[0]["uid"]) == sub
    same_slug = [u for u in users if u.get("is_active")
                 and make_user_slug(str(u.get("username") or "")) == slug]
    slug_ok = len(same_slug) == 1 and str(same_slug[0].get("uid")) == sub

    for row in pending:
        mcp_id = row.get("mcp_id")
        if db.instance_was_quarantined(slug, mcp_id):
            reason = "instance_quarantined"
        elif db.has_pending_instance_for(slug, mcp_id):
            out["waiting"] += 1
            continue
        elif me is None:
            reason = "owner_not_an_active_uid"
            _revoke_person_tokens(db, sub, "credential quarantined", cred_store, users)
        elif not email_ok:
            reason = "email_not_verified"
        elif not slug_ok:
            reason = "slug_not_unique"
        elif not _not_before(row.get("created_at"), my_joined):
            reason = "created_before_owner_joined"
        else:
            inst = db.rekeyed_instance_for(slug, mcp_id)
            if inst and inst["owner_sub"] != sub:
                reason = "instance_owned_by_other"
            else:
                reason = None
        if reason is None:
            done = db.rekey_row("mcp_user_secrets", row["id"], sub)
            if done:
                out["rekeyed"] += 1
            if done is not None:
                continue
            reason = "superseded"  # the visitor already reconnected after the upgrade
        if db.quarantine_row("mcp_user_secrets", row["id"], reason) is not None:
            out["quarantined"] += 1
    logger.info("credential re-key for %s: %s", slug, out)
    return _record(db, out)


def ensure_instances_rekeyed(app):
    """Run the instance re-key if it has not completed, at most every
    RETRY_SECONDS per process. Returns None when nothing ran (done, throttled,
    or no directory), else the run's summary; a summary with 'error' is a
    failed attribution and is recorded for `rzfz status`."""
    if getattr(app, "_rzfz_rekey_done", False):
        return None
    now = time.monotonic()
    if now < getattr(app, "_rzfz_rekey_next", 0):
        return None
    app._rzfz_rekey_next = now + RETRY_SECONDS
    db = app.db
    # A cognee account registered in a later no-token window (B6 fix round 4)
    # reopens the run for that account only.
    if db.instances_rekey_done() and not db.pending_cognee_accounts():
        app._rzfz_rekey_done = True
        return None
    directory = getattr(app, "authentik_directory", None)
    if not directory:
        return None
    remove = getattr(getattr(app, "docker_client", None), "remove_container", None)
    res = rekey_instances(db, directory, remove_container=remove,
                          cred_store=getattr(app, "cred_store", None))
    if "error" not in res and db.instances_rekey_done():
        app._rzfz_rekey_done = True
    return res


def _record(db, res):
    """Record a run's outcome for `rzfz status` and return `res` unchanged."""
    try:
        db.record_attribution("error" not in res, res.get("error", ""))
    except Exception:  # noqa: BLE001 — the WARN log below still carries it
        logger.warning("could not record the attribution outcome")
    if "error" in res:
        logger.warning("MCP identity attribution failed: %s", res["error"])
    return res


def on_portal_visit(app, *, slug: str, sub: str, email: str, username: str) -> list:
    """record_sso_user's hand-off: finish the instance run first (a credential
    follows its instance), then decide this visitor's credential rows.
    Returns the attribution errors of this request (empty = none). The
    caller must treat a non-empty list as a failed attribution."""
    errors = []
    res = ensure_instances_rekeyed(app)
    if res and "error" in res:
        errors.append(res["error"])
    try:
        ensure_departed_swept(app)   # its own failure is not an attribution error
    except Exception:  # noqa: BLE001
        logger.exception("departed-person credential sweep failed; retried later")
    directory = getattr(app, "authentik_directory", None)
    if not directory or not app.db.pending_secrets_for_slug(slug):
        return errors
    res = attribute_credentials(app.db, directory, slug=slug, sub=sub, email=email,
                                cred_store=getattr(app, "cred_store", None))
    if "error" in res:
        errors.append(res["error"])
    return errors


# ---------------------------------------------------------------------------
# #2677: credentials of people who left
# ---------------------------------------------------------------------------

def sweep_departed_credentials(db, directory, cred_store=None) -> dict:
    """Retire every person who may still own a stored credential but is no
    longer an active internal Authentik user (#2677):
    a DELETED account (its uid in no listing record) loses its credentials at
    once; a DEACTIVATED one after 30 days continuously deactivated (the clock
    is kept in mcp_person_deactivated and reset when the person is active
    again). Gateway tokens are revoked either way (#2086).

    The candidates are read BEFORE the listing: a person who stores a
    credential while the listing is read is then judged by a listing that can
    already see them, never by one older than their row.

    Returns {"persons", "rows", "openbao", "deactivated_waiting"} (persons
    counted only when something of theirs was deleted); {"error": …} and
    NOTHING decided on a directory failure or an unusable listing. Technical
    users' and legacy (unattributed) rows are never touched. The outcome is
    recorded for `rzfz status`."""
    from app.services.gateway_identity import retire_person
    out = {"persons": 0, "rows": 0, "openbao": 0, "deactivated_waiting": 0}
    if not directory:
        return {**out, "error": "no directory"}
    owners = db.person_secret_owners()
    try:
        users = directory.usable_listing()
    except DirectoryError as e:
        logger.warning("departed-person credential sweep postponed: %s", e)
        return _record_sweep(db, {**out, "error": str(e)})
    active = set(_persons_by_uid(users))
    # Seen active again: the deactivation clock restarts from zero.
    back = [s for s in db.deactivated_persons() if s in active]
    if back:
        db.clear_person_deactivated(back)
    failed = 0
    for owner in owners:
        if owner in active:
            continue
        res = retire_person(db, owner, _listed(users, owner), cred_store=cred_store)
        if res.get("error"):
            failed += 1
            continue
        if res.get("waiting"):
            out["deactivated_waiting"] += 1
        if res["rows"] or res["openbao"]:
            out["persons"] += 1
            out["rows"] += res["rows"]
            out["openbao"] += res["openbao"]
    if out["persons"] or failed:
        logger.warning("deleted %d stored credential row(s) and %d OpenBao grant(s) of %d "
                       "person(s) no longer active in Authentik%s",
                       out["rows"], out["openbao"], out["persons"],
                       f"; {failed} could not be purged and are retried" if failed else "")
    if failed:
        out["error"] = f"{failed} person(s) could not be purged"
        out["partial"] = True
    return _record_sweep(db, out)


def _record_sweep(db, res):
    """Record a sweep's outcome for `rzfz status` and return `res` unchanged."""
    try:
        db.record_departed_sweep("error" not in res, res.get("error", ""))
    except Exception:  # noqa: BLE001 — the WARN log still carries it
        logger.warning("could not record the departed-person sweep outcome")
    return res


def ensure_departed_swept(app):
    """Run the sweep at start (every upgrade restarts mcp-manager) and then at
    most every SWEEP_SECONDS per process. A run that could not read the
    directory is retried after RETRY_SECONDS; a run that read it but could not
    purge everyone (a partial failure) keeps the hourly pace — retrying it on
    every portal visit would only repeat the same failure. Returns None when
    nothing ran, else the run's summary."""
    now = time.monotonic()
    if now < getattr(app, "_rzfz_sweep_next", 0):
        return None
    directory = getattr(app, "authentik_directory", None)
    if not directory:
        return None
    res = sweep_departed_credentials(app.db, directory, cred_store=getattr(app, "cred_store", None))
    retry_soon = "error" in res and not res.get("partial")
    app._rzfz_sweep_next = now + (RETRY_SECONDS if retry_soon else SWEEP_SECONDS)
    return res
