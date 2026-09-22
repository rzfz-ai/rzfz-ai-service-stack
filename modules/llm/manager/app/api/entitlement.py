# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Entitlement + signed monthly rollup (#254 Phase-2 P2-E2).

The box is the BILLING SOURCE-OF-TRUTH (operator 2026-08-11): local
``usage_events`` are authoritative; the manager exposes a signed monthly TOKEN
rollup (input/output/cached per cost-center + key). No currency — token counts
only. An optional HMAC key (LLM_MANAGER_ROLLUP_KEY) signs the canonical rollup
so a downstream consumer can verify integrity; unset → returned unsigned.
Adding an export / phone-home later needs no change to where the SoT lives.

``installation_entitled`` is the subscription/installation check (stub →
enforced): active subscription, not past ``valid_until``. Admin-gated
(require_admin) like the rest of the management API.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import MAXYEAR, MINYEAR, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.authz import Role, require_role
from app.config import get_settings
from app.db import session_scope

logger = logging.getLogger("orchestrator.entitlement")


def _month_bounds(year: int, month: int):
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    return start, end


def monthly_rollup(session, year: int, month: int) -> dict:
    """Aggregate usage_events for the month → per (cost_center, api_key) TOKEN
    totals (input/output/cached). Deterministic ordering for a stable signature."""
    from sqlalchemy import func

    from app.models import UsageEvent

    start, end = _month_bounds(year, month)
    rows = (
        session.query(
            UsageEvent.cost_center_id,
            UsageEvent.api_key_id,
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cached_tokens), 0),
        )
        .filter(UsageEvent.ts >= start, UsageEvent.ts < end)
        .group_by(UsageEvent.cost_center_id, UsageEvent.api_key_id)
        .all()
    )
    totals = [
        {"cost_center": str(cc), "api_key": str(k),
         "input": int(i), "output": int(o), "cached": int(c)}
        for (cc, k, i, o, c) in rows
    ]
    totals.sort(key=lambda t: (t["cost_center"], t["api_key"]))
    return {"month": f"{year:04d}-{month:02d}", "totals": totals}


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def sign_rollup(payload: dict, key: str):
    """HMAC-SHA256 over the canonical rollup; None when no key is configured.

    #341: the signature must cover WHICH box and WHEN, not just the totals. The
    signed payload previously held only ``{month, totals}``, so a rollup from one
    installation was indistinguishable from another's whenever they shared
    LLM_MANAGER_ROLLUP_KEY, and any rollup could be replayed as a later
    submission. For an artifact whose stated role is "the box is the billing
    source of truth", neither is acceptable.

    The caller folds ``subscription_number`` and ``issued_at`` into the payload
    BEFORE signing, so the signature covers exactly the fields returned. That is
    the contract: signature = HMAC over the canonical JSON of every field except
    ``signed`` and ``signature``.
    """
    if not key:
        return None
    return hmac.new(key.encode(), _canonical(payload).encode(), hashlib.sha256).hexdigest()


#: Fields excluded from the signed payload — they carry the signature itself.
UNSIGNED_FIELDS = ("signed", "signature")


def installation_entitled(session, subscription_number: str, *, now=None) -> bool:
    """True iff the subscription exists, is active, and hasn't lapsed.

    #341: this was a SECOND, independently-implemented entitlement predicate that
    nothing called, and it disagreed with the live one — no single-local-row
    fallback, and a required number. Two predicates that can diverge is a
    liability in a licensing path, so this is now a thin wrapper over
    ``evaluate_entitlement`` and there is exactly one implementation.

    The required-number semantics are preserved deliberately: an empty number here
    must stay False rather than silently inheriting the single-row fallback, which
    is the divergence that made the two disagree in the first place.
    """
    if not subscription_number:
        return False
    return evaluate_entitlement(session, subscription_number, now=now)["entitled"]


def resolve_subscription(session, subscription_number: str | None):
    """The Subscription this box is governed by: the configured number if set,
    else the single local row (the common one-box case).

    Returns ``(subscription, problem)``. ``problem`` is None on success, ``"none"``
    when there is genuinely no row, and ``"ambiguous"`` (#341) when the fallback
    finds more than one and cannot choose.

    Ambiguity used to be folded into None, so a second Subscription row made the
    box report "no subscription on record" — and in entitlement_mode=enforce that
    is a 402 on every request, with an operator-facing reason that is not merely
    unhelpful but false: there are two subscriptions, not zero. The fix for
    "no subscription" is to provision one; the fix for this is to set
    LLM_MANAGER_SUBSCRIPTION_NUMBER. Reporting the wrong one costs an outage's
    worth of looking in the wrong place.
    """
    from app.models import Subscription

    if subscription_number:
        sub = (
            session.query(Subscription)
            .filter(Subscription.subscription_number == subscription_number)
            .one_or_none()
        )
        return (sub, None) if sub is not None else (None, "none")
    rows = (session.query(Subscription)
            .order_by(Subscription.subscription_number)   # deterministic listing
            .limit(3).all())
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, "none"
    names = ", ".join(r.subscription_number for r in rows[:3])
    logger.warning(
        "entitlement: %d subscriptions on record and LLM_MANAGER_SUBSCRIPTION_NUMBER "
        "is unset — cannot choose between: %s (#341)", len(rows), names)
    return None, "ambiguous"


def evaluate_entitlement(session, subscription_number: str | None = None, *, now=None) -> dict:
    """Rich LOCAL entitlement verdict for the console + the hot-path gate.

    state ∈ {active, expired, suspended, none, ambiguous}. ``entitled`` is the single
    boolean the gate keys on. No external call — reads the subscriptions table
    only (provisioned out-of-band)."""
    now = now or datetime.now(timezone.utc)
    sub, problem = resolve_subscription(session, subscription_number)
    if sub is None:
        # #341 `ambiguous` is NOT reported as entitled: the box genuinely cannot
        # tell which subscription governs it, and guessing in a licensing path is
        # worse than refusing. But it is its own state with its own reason, so the
        # operator is pointed at the setting rather than at provisioning.
        reason = ("more than one subscription on record and "
                  "LLM_MANAGER_SUBSCRIPTION_NUMBER is unset"
                  if problem == "ambiguous" else "no subscription on record")
        return {"state": problem or "none", "entitled": False, "reason": reason,
                "plan": None, "seats": None, "valid_until": None, "days_remaining": None,
                "subscription_number": subscription_number or None}
    expired = sub.valid_until is not None and sub.valid_until < now
    if sub.status != "active":
        state, entitled = "suspended", False
    elif expired:
        state, entitled = "expired", False
    else:
        state, entitled = "active", True
    days = None
    if sub.valid_until is not None:
        days = (sub.valid_until - now).days
    return {
        "state": state, "entitled": entitled,
        "reason": {"active": "ok", "expired": "past valid_until",
                   "suspended": f"status={sub.status}"}[state],
        "plan": sub.plan, "seats": sub.seats,
        "valid_until": sub.valid_until.isoformat() if sub.valid_until else None,
        "days_remaining": days,
        "subscription_number": sub.subscription_number,
    }


def _verdict_cache_key(sub_number: str | None) -> str:
    return f"entitlement:verdict:{sub_number or '_local'}"


def entitlement_allows(cache, subscription_number: str | None, *, now=None, ttl: int = 60) -> bool:
    """Cached boolean for the proxy hot path — DB read at most once per ``ttl``.
    Only consulted in enforce mode, so a cache/DB blip fails CLOSED (not
    entitled) rather than silently granting unlicensed serving."""
    key = _verdict_cache_key(subscription_number)
    if cache is not None:
        try:
            hit = cache.get(key)
            if hit is not None:
                return hit in ("1", b"1", 1, True)
        except Exception:  # pragma: no cover - cache blip
            pass
    try:
        with session_scope() as s:
            ok = evaluate_entitlement(s, subscription_number, now=now)["entitled"]
    except Exception:  # pragma: no cover - DB blip → fail closed in enforce mode
        ok = False
    if cache is not None:
        try:
            # ValkeyCache exposes setex(key, ttl, value) — there is no set(..., ex=).
            # The previous `cache.set(key, ..., ex=ttl)` raised AttributeError on every
            # call and was swallowed here, so the verdict was NEVER cached and every
            # enforce-mode request paid a full DB round-trip (#339).
            cache.setex(key, ttl, "1" if ok else "0")
        except Exception:  # pragma: no cover - cache blip must not fail the gate
            logger.warning("entitlement verdict cache write failed", exc_info=True)
    return ok


def register_entitlement_api(app) -> None:
    # #314: the subscription/entitlement rollup + status is box-level billing
    # posture, adjacent to global settings → the SUPER-ADMIN tier (conservative:
    # this is NOT loosened toward admin — a candidate for the admin "view all
    # usage" tier is noted in the follow-up).
    router = APIRouter(dependencies=[Depends(require_role(Role.SUPERADMIN))])

    @router.get("/api/entitlement/rollup")
    def rollup(month: str = Query(..., description="YYYY-MM")):
        try:
            y, m = month.split("-")
            year, mon = int(y), int(m)
            if not (1 <= mon <= 12):
                raise ValueError
            # #341 the month was range-checked and the year was not, so
            # ?month=0000-01 reached datetime(0, 1, 1) and raised
            # "year 0 is out of range" OUTSIDE this block — an unhandled 500 on a
            # malformed input, where every other malformed input here is a 422.
            # MINYEAR/MAXYEAR rather than a hand-picked window: the constraint is
            # what datetime can represent, and hard-coding a range would drift.
            #
            # Imported from the datetime MODULE, not read off the datetime CLASS.
            # `datetime.MINYEAR` is an AttributeError — and because this block
            # catches AttributeError, the guard would have turned EVERY month into
            # a 422, valid ones included. Caught by simulating the route before
            # merging; the first version of the test asserted the source text
            # rather than the behaviour and was happy with the broken form.
            if not (MINYEAR <= year <= MAXYEAR):
                raise ValueError
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail="month must be 'YYYY-MM'")
        settings = get_settings()
        with session_scope() as s:
            data = monthly_rollup(s, year, mon)
            # #341 bind the artifact to THIS installation. Resolved rather than
            # read straight from settings, so a box using the single-local-row
            # fallback still names the subscription it actually billed against.
            sub, _problem = resolve_subscription(s, settings.subscription_number or None)
            data["subscription_number"] = (
                sub.subscription_number if sub is not None
                else (settings.subscription_number or None))
        # ...and to a point in time, so a rollup cannot be replayed as a later
        # submission. This makes the response non-deterministic by design: two
        # calls for the same month differ, and that is what replay detection
        # needs. A verifier compares totals, not signatures.
        data["issued_at"] = datetime.now(timezone.utc).isoformat()
        sig = sign_rollup(data, settings.rollup_key)
        return {**data, "signed": sig is not None, "signature": sig}

    @router.get("/api/entitlement/status")
    def status():
        """#265: the box's LOCAL entitlement state + the active mode. In
        ``report`` the state is informational; in ``enforce`` an ``entitled:
        false`` state means the hot path returns 402."""
        settings = get_settings()
        with session_scope() as s:
            verdict = evaluate_entitlement(s, settings.subscription_number or None)
        return {**verdict, "mode": settings.entitlement_mode,
                "enforced": settings.entitlement_mode == "enforce"}

    app.include_router(router)
