# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Keys + cost-centers CRUD + TOKEN-ONLY usage report (U1).

Endpoints (JSON API + a minimal HTML table):

  POST /api/cost-centers        create a cost-center (grouping label)
  GET  /api/cost-centers        list cost-centers
  POST /api/keys                mint a key (plaintext returned ONCE; only the
                                SHA-256 hash is stored)
  GET  /api/keys                list keys (never leaks plaintext)
  POST /api/keys/{id}/rotate    new plaintext + hash; invalidate the cache
  POST /api/keys/{id}/disable   status=revoked; invalidate the cache
  GET  /api/usage               per-key SUM(input/output/cached) tokens,
                                optional ?since_days=N and ?bucket=day|month
  GET  /ui/usage                the same report as a minimal HTML table

Everything is token counts — NO currency, NO cost (operator 2026-08-10).
"""
from __future__ import annotations

import logging

import datetime as _dt
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func

from app.auth import cache_key_for, generate_api_key

logger = logging.getLogger(__name__)
from app.authz import Role, require_role
from app.config import get_settings
from app.api._ids import parse_uuid
from app.db import session_scope
from app.models import ApiKey, CostCenter, UsageEvent


class CostCenterCreate(BaseModel):
    name: str
    team: Optional[str] = None


class KeyCreate(BaseModel):
    cost_center_id: str
    allowed_models: list[str] = []
    rpm_limit: Optional[int] = None
    tpm_limit: Optional[int] = None
    max_budget_tokens: Optional[int] = None
    # Rolling window the token budget resets over, in seconds (e.g. 2_592_000
    # = 30d). NULL = the budget never resets (lifetime cap).
    budget_duration_seconds: Optional[int] = None
    expires_at: Optional[str] = None
    # #314 owner attribution. Mint is admin-gated only (this route's caller is
    # always at least an admin — see the capability matrix: "issue keys for
    # anyone" is an admin+ power), so an admin may explicitly mint FOR someone
    # else. Absent/blank → defaults to the CALLER's own identity (self-mint),
    # never left empty — `_owner_for_mint` is the single place this decision
    # is made.
    owner_username: Optional[str] = None



def _cache(request: Request):
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        from app.cache import get_cache

        cache = get_cache()
    return cache


class CacheInvalidationError(RuntimeError):
    """The auth cache could not be invalidated, so a revoked/rotated key may
    still authenticate until its cached verdict expires (#327)."""


def _invalidate(cache, key_hash: bytes) -> None:
    """Drop the cached auth verdict for a key. RAISES on failure (#327).

    This used to swallow every exception as "cache best-effort". For a read
    path that is right; for REVOCATION it is not. `auth` caches verdicts for
    `cache_ttl_seconds` (default 300), so a swallowed delete meant a revoked key
    kept authenticating for up to five minutes — while the endpoint returned
    `{"status": "revoked"}` and the operator believed it was done.

    Revocation is usually taken BECAUSE a key leaked, which is exactly when a
    silent five-minute grace period is least acceptable. The caller now converts
    this into a 503 that says the row is revoked but the cache is not clear, so
    the operator knows to wait out the TTL or fix the cache — rather than
    learning it from a bill.
    """
    try:
        cache.delete(cache_key_for(key_hash))
    except Exception as exc:
        raise CacheInvalidationError(str(exc)) from exc


def _invalidate_or_503(cache, key_hash: bytes, key_id: str, action: str) -> None:
    """Invalidate, or fail the request loudly (#327).

    The database row is already committed at this point, so the state is not
    lost — what is lost is the guarantee the caller just asked for. A 200 here
    would be the API asserting something untrue.
    """
    try:
        _invalidate(cache, key_hash)
    except CacheInvalidationError as exc:
        logger.error("key %s %s but cache invalidation FAILED: %s", key_id, action, exc)
        raise HTTPException(
            status_code=503,
            detail=(
                f"key {key_id} is marked {action} in the database, but the auth "
                f"cache could not be invalidated ({exc}). The old key may keep "
                f"authenticating until its cached verdict expires. Retry once the "
                f"cache is reachable, or wait out the auth cache TTL."
            ),
        )


def _owner_for_mint(payload_owner: Optional[str], identity: dict) -> str:
    """#314: resolve who a minted key is attributed to.

    An explicit ``owner_username`` in the payload wins (an admin minting a
    key for someone else); otherwise the key is attributed to the CALLER
    (self-mint). Never returns empty — a key with no owner would silently
    reopen the exact "who does this key belong to" gap the issue names, and
    ``identity["username"]`` cannot be missing here: this is only reached
    after ``require_admin`` has already fail-closed on an absent identity.
    """
    owner = (payload_owner or "").strip()
    if owner:
        return owner
    return identity["username"]


def register_keys_api(app) -> None:
    # #314: the admin key-management surface (mint FOR ANYONE + view ALL usage)
    # is the ADMIN tier per the capability matrix. One shared dependency object
    # so FastAPI dedups the router-level gate and the per-route `identity`
    # injection into a single evaluation. Super-admin satisfies it by hierarchy.
    _admin = require_role(Role.ADMIN)
    router = APIRouter(dependencies=[Depends(_admin)])

    # --- cost centers ------------------------------------------------------
    @router.post("/api/cost-centers")
    def create_cost_center(payload: CostCenterCreate):
        with session_scope() as s:
            cc = CostCenter(name=payload.name, team=payload.team)
            s.add(cc)
            s.flush()
            return {"id": str(cc.id), "name": cc.name, "team": cc.team}

    @router.get("/api/cost-centers")
    def list_cost_centers():
        with session_scope() as s:
            return [
                {"id": str(c.id), "name": c.name, "team": c.team}
                for c in s.query(CostCenter).order_by(CostCenter.created_at).all()
            ]

    # --- keys --------------------------------------------------------------
    @router.post("/api/keys")
    def create_key(payload: KeyCreate, identity: dict = Depends(_admin)):
        settings = get_settings()
        plaintext, key_hash, display = generate_api_key(settings.key_prefix)
        expires = _parse_ts(payload.expires_at)
        budget_duration = (
            _dt.timedelta(seconds=payload.budget_duration_seconds)
            if payload.budget_duration_seconds
            else None
        )
        owner_username = _owner_for_mint(payload.owner_username, identity)
        with session_scope() as s:
            if s.get(CostCenter, parse_uuid(payload.cost_center_id, "cost_center_id", status_code=422)) is None:
                raise HTTPException(status_code=404, detail="cost_center not found")
            key = ApiKey(
                key_hash=key_hash,
                key_prefix=display,
                cost_center_id=parse_uuid(payload.cost_center_id, "cost_center_id", status_code=422),
                allowed_models=list(payload.allowed_models or []),
                rpm_limit=payload.rpm_limit,
                tpm_limit=payload.tpm_limit,
                max_budget_tokens=payload.max_budget_tokens,
                budget_duration=budget_duration,
                expires_at=expires,
                owner_username=owner_username,
            )
            s.add(key)
            s.flush()
            return {
                "id": str(key.id),
                "key": plaintext,  # shown ONCE
                "key_prefix": key.key_prefix,
                "cost_center_id": str(key.cost_center_id),
                "status": key.status,
                "owner_username": key.owner_username,
            }

    @router.get("/api/keys")
    def list_keys():
        with session_scope() as s:
            return [
                {
                    "id": str(k.id),
                    "key_prefix": k.key_prefix,
                    "cost_center_id": str(k.cost_center_id),
                    "status": k.status,
                    "owner_username": k.owner_username,
                    "allowed_models": list(k.allowed_models or []),
                    "rpm_limit": k.rpm_limit,
                    "tpm_limit": k.tpm_limit,
                    "max_budget_tokens": k.max_budget_tokens,
                    "budget_duration_seconds": (
                        int(k.budget_duration.total_seconds())
                        if k.budget_duration is not None
                        else None
                    ),
                    "created_at": k.created_at.isoformat() if k.created_at else None,
                    "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                }
                for k in s.query(ApiKey).order_by(ApiKey.created_at).all()
            ]

    @router.post("/api/keys/{key_id}/rotate")
    def rotate_key(key_id: str, request: Request):
        settings = get_settings()
        plaintext, new_hash, display = generate_api_key(settings.key_prefix)
        with session_scope() as s:
            key = s.get(ApiKey, parse_uuid(key_id, "key_id"))
            if key is None:
                raise HTTPException(status_code=404, detail="key not found")
            old_hash = bytes(key.key_hash)
            key.key_hash = new_hash
            key.key_prefix = display
        _invalidate_or_503(_cache(request), old_hash, key_id, "rotated")
        return {"id": key_id, "key": plaintext, "key_prefix": display}

    @router.post("/api/keys/{key_id}/disable")
    def disable_key(key_id: str, request: Request):
        with session_scope() as s:
            key = s.get(ApiKey, parse_uuid(key_id, "key_id"))
            if key is None:
                raise HTTPException(status_code=404, detail="key not found")
            key.status = "revoked"
            key_hash = bytes(key.key_hash)
        _invalidate_or_503(_cache(request), key_hash, key_id, "revoked")
        return {"id": key_id, "status": "revoked"}

    # --- token-usage report ------------------------------------------------
    @router.get("/api/usage")
    def usage_report(
        since_days: Optional[int] = Query(default=None, ge=0),
        bucket: Optional[str] = Query(default=None, pattern="^(day|month)$"),
        group: str = Query(default="key", pattern="^(key|model)$"),
    ):
        return _usage_rows(since_days, bucket, group)

    @router.get("/api/usage/series")
    def usage_series(
        frm: Optional[str] = Query(default=None, alias="from"),
        to: Optional[str] = Query(default=None),
        bucket: str = Query(default="hour", pattern="^(hour|day)$"),
        model: Optional[str] = Query(default=None),
    ):
        """Token/request usage bucketed over time for the dashboard usage graph.
        Ordered ascending by bucket; filterable by model + [from,to) window."""
        return _usage_series(frm, to, bucket, model)

    @router.get("/ui/usage", response_class=HTMLResponse)
    def usage_ui(
        since_days: Optional[int] = Query(default=None, ge=0),
        bucket: Optional[str] = Query(default=None, pattern="^(day|month)$"),
    ):
        rows = _usage_rows(since_days, bucket)
        return HTMLResponse(_render_usage_html(rows, bucket))

    # --- USER-tier self-service mint: DEFERRED to #265 --------------------
    # A `POST /api/me/keys` at the USER tier was drafted here, but agent-seqis's
    # #842 review (MED 1) showed it cannot be shipped SAFELY before #265: a
    # self-issued key needs a spend CAP and correct cost-center ATTRIBUTION, and
    # both are #265's entitlement machinery. Without it the endpoint would mint
    # an uncapped, durable key attributable to ANY team's cost-centre — a real
    # billing-misattribution + no-spend-cap hole. Picking a default budget
    # magnitude here would be inventing policy that belongs to #265/the
    # operator. So the mint waits for #265 (tracked in the #314 follow-up); the
    # USER tier still has the playground, and `/api/me` already answers a
    # non-admin identity. `capabilities_for` keeps `self_issue_key` in the tier
    # matrix as the documented future capability.
    app.include_router(router)


def _parse_ts(value: Optional[str]):
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="expires_at must be ISO-8601")


def _usage_rows(
    since_days: Optional[int], bucket: Optional[str], group: str = "key"
) -> list[dict]:
    # #337: an omitted since_days used to scan the ENTIRE table - /ui/usage
    # got slower every day the box ran. Default window 90 days; an explicit
    # since_days=0 still means "everything" for a deliberate full report.
    if since_days is None:
        since_days = 90
    by_model = group == "model"
    key_col = UsageEvent.model if by_model else UsageEvent.api_key_id
    with session_scope() as s:
        cols = [
            key_col.label("gkey"),
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageEvent.cached_tokens), 0).label("cached_tokens"),
            func.count(UsageEvent.id).label("events"),
        ]
        grouping = [key_col]
        if bucket:
            # #1163: the same shape as `/api/usage/series` (#1153, `_TS_UTC`
            # below): `ts` is timestamptz and `date_trunc` over it answers in
            # the DATABASE SESSION's TimeZone, so a row near midnight landed in
            # a different day bucket depending on the session — and the label
            # went out with that session's offset. Truncate the UTC wall clock.
            bucket_col = func.date_trunc(bucket, _TS_UTC).label("bucket")
            cols.append(bucket_col)
            grouping.append(bucket_col)

        q = s.query(*cols)
        if since_days:
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days)
            q = q.filter(UsageEvent.ts >= cutoff)
        q = q.group_by(*grouping)

        # map key_id -> prefix for display (key grouping only)
        prefixes = {str(k.id): k.key_prefix for k in s.query(ApiKey).all()}

        out = []
        for row in q.all():
            if by_model:
                entry = {"model": row.gkey}
            else:
                entry = {
                    "api_key_id": str(row.gkey),
                    "key_prefix": prefixes.get(str(row.gkey)),
                }
            entry.update(
                {
                    "input_tokens": int(row.input_tokens),
                    "output_tokens": int(row.output_tokens),
                    "cached_tokens": int(row.cached_tokens),
                    "events": int(row.events),
                }
            )
            if bucket:
                # `timezone('UTC', ts)` comes back NAIVE — re-mark as UTC on the
                # way out (#1163, same as `/api/usage/series`).
                entry["bucket"] = _bucket_iso(row.bucket)
            out.append(entry)
        return out


# #1153: `ts` is `timestamptz`, and `date_trunc` over a timestamptz answers in
# the DATABASE SESSION's TimeZone — so the same row lands in a different bucket
# depending on how postgres happens to be configured, and a box running
# `TimeZone=Europe/Vienna` reports a different day than the documented UTC
# window. This is the same latency #991 fixed in the analytics endpoint and
# used to skip; #1163 gave it the same shape. `timezone('UTC',
# ts)` yields the UTC wall clock — the bucket keys then come back NAIVE, so
# `_bucket_iso` re-marks them (see there: the wire format must keep its
# `+00:00`).
_TS_UTC = func.timezone("UTC", UsageEvent.ts)


def _as_utc(value: Optional[_dt.datetime]) -> Optional[_dt.datetime]:
    """Read a naive window bound as UTC.

    Second half of the same bug: a naive `?from=`/`?to=` (which is what the
    dashboard and the documented examples send) is handed to postgres as a
    `timestamp` and cast against `timestamptz` using the SESSION TimeZone. With
    the buckets pinned to UTC but the bounds still floating, the window edges
    would silently slide by the box's UTC offset.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=_dt.timezone.utc)


def _bucket_iso(value) -> str:
    """`timezone('UTC', ts)` comes back NAIVE — it is a wall clock, not an
    instant. `/api/usage/series` has always answered with an explicit `+00:00`
    and the dashboard reads the offset, so re-attach UTC before serialising;
    dropping this turns every bucket key into an ambiguous local-looking
    timestamp for the client."""
    if not hasattr(value, "isoformat"):
        return str(value)
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.isoformat()


def _usage_series(
    frm: Optional[str], to: Optional[str], bucket: str, model: Optional[str]
) -> list[dict]:
    """Time-bucketed token/request totals (ascending). [from,to) window +
    optional model filter. Buckets with no events are simply absent — the
    client fills gaps for a continuous line.

    Buckets and window bounds are UTC regardless of the database's TimeZone
    (#1153)."""
    f = _as_utc(_parse_ts(frm))
    t = _as_utc(_parse_ts(to))
    with session_scope() as s:
        bcol = func.date_trunc(bucket, _TS_UTC).label("bucket")
        q = s.query(
            bcol,
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageEvent.cached_tokens), 0).label("cached_tokens"),
            func.count(UsageEvent.id).label("events"),
        )
        if f:
            q = q.filter(UsageEvent.ts >= f)
        if t:
            q = q.filter(UsageEvent.ts < t)
        if model:
            q = q.filter(UsageEvent.model == model)
        q = q.group_by(bcol).order_by(bcol)
        return [
            {
                "bucket": _bucket_iso(r.bucket),
                "input_tokens": int(r.input_tokens),
                "output_tokens": int(r.output_tokens),
                "cached_tokens": int(r.cached_tokens),
                "total_tokens": int(r.input_tokens) + int(r.output_tokens),
                "events": int(r.events),
            }
            for r in q.all()
        ]


def _render_usage_html(rows: list[dict], bucket: Optional[str]) -> str:
    head = "<th>Key</th><th>Input</th><th>Output</th><th>Cached</th><th>Events</th>"
    if bucket:
        head = "<th>Bucket</th>" + head
    body_rows = []
    for r in rows:
        cells = ""
        if bucket:
            cells += f"<td>{r.get('bucket','')}</td>"
        cells += (
            f"<td>{r['key_prefix'] or r['api_key_id']}</td>"
            f"<td>{r['input_tokens']}</td>"
            f"<td>{r['output_tokens']}</td>"
            f"<td>{r['cached_tokens']}</td>"
            f"<td>{r['events']}</td>"
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Token usage — rzfz.ai LLM Manager</title></head><body>"
        "<h1>Token usage by API key</h1>"
        "<p>Token counts only (input / output / cached). No currency.</p>"
        f"<table border='1'><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
        "</body></html>"
    )
