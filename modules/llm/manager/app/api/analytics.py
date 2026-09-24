# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#991 — cost-control analytics over `usage_events` (ONE admin endpoint).

  GET /api/usage/analytics?days=&bucket=&cost_center=&model=&top=

The console's cost page needs six different cuts of the same window — headline
totals, the same totals for the PREVIOUS window (the "vs previous" delta), a
time-bucketed series, top-N models, the per-cost-centre and per-key split, and
a weekday x hour-of-day activity grid. Six round-trips would each re-scan the
same rows, so this is one endpoint that issues seven bounded GROUP BY
aggregates and returns the assembled payload.

TOKENS, NOT CURRENCY. `usage_events` deliberately has no cost column (see
`app/models.py::UsageEvent`) and this module does not invent one: "spend" here
is tokens and requests. Pricing stays in the operator's billing system.

Two properties this file is written to keep, both pinned by
tests/unit/llm-manager/test_991_cost_analytics.py:

  * **No full-table pull into Python.** Every query aggregates in SQL, carries
    the window's `ts` bounds, and the ranked ones carry a LIMIT. The row count
    that crosses the DB boundary is bounded by (points + top + 7*24), never by
    the size of `usage_events`.
  * **Shaping is pure.** Bucket fill, the weekday rotation, the share/delta
    percentages are module-level functions over plain data, so they are driven
    directly by tests instead of through a simulated database.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func

from app.api._ids import parse_uuid
from app.authz import Role, require_role
from app.db import session_scope
from app.models import ApiKey, CostCenter, UsageEvent

# Weekday labels for the heatmap rows, Monday-first (see `weekday_index`).
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HOURS = 24
# An `hour` bucket over a long window would return thousands of points that no
# 640px-wide chart can show; the series is what bounds this endpoint's response
# size, so the ceiling is enforced rather than silently truncated.
MAX_HOUR_DAYS = 14
_STEP = {"hour": _dt.timedelta(hours=1), "day": _dt.timedelta(days=1)}


# --------------------------------------------------------------------------- #
# pure shaping helpers (no session, no ORM)                                    #
# --------------------------------------------------------------------------- #

def truncate(ts: _dt.datetime, bucket: str) -> _dt.datetime:
    """Floor a timestamp to its bucket — the Python twin of the SQL
    `date_trunc(bucket, ts)` the aggregates group by, so a filled bucket key
    matches the key the database returns."""
    if bucket == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def window_bounds(days: int, bucket: str, now: _dt.datetime):
    """(start, end, points) for the requested window.

    `end` is the EXCLUSIVE upper edge one step past the bucket `now` falls in,
    so the partially-elapsed current hour/day is included — an operator looking
    at "today" expects today's tokens to be in it. `start` is `points` steps
    back from there, which makes the series exactly `points` long and the
    previous window (`start - points*step`, `start`) the same length.
    """
    step = _STEP[bucket]
    points = days * HOURS if bucket == "hour" else days
    end = truncate(now, bucket) + step
    return end - points * step, end, points


def weekday_index(pg_dow: int) -> int:
    """Postgres `extract(dow)` is 0=Sunday..6=Saturday; the grid is Monday-first
    (0=Mon..6=Sun) because that is the order `WEEKDAYS` renders and the order a
    working week reads in."""
    return (int(pg_dow) + 6) % 7


def _zero_point(bucket_iso: str) -> dict:
    return {
        "bucket": bucket_iso,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "events": 0,
    }


def fill_series(rows: list[dict], start: _dt.datetime, points: int, bucket: str) -> list[dict]:
    """Zero-fill the aggregate rows across the WHOLE window, ascending.

    A GROUP BY only returns buckets that have events, so an idle Sunday is
    simply absent — plotted raw, the x-axis silently compresses to whatever was
    busy (the same defect #290 hit on the dashboard, fixed client-side there).
    Filling here means every consumer of this endpoint gets the honest window.
    """
    step = _STEP[bucket]
    by = {r["bucket"]: r for r in rows}
    out = []
    for i in range(points):
        b = start + i * step
        row = by.get(b)
        if row is None:
            out.append(_zero_point(b.isoformat()))
        else:
            out.append(
                {
                    "bucket": b.isoformat(),
                    "input_tokens": int(row["input_tokens"]),
                    "output_tokens": int(row["output_tokens"]),
                    "cached_tokens": int(row["cached_tokens"]),
                    "total_tokens": int(row["input_tokens"]) + int(row["output_tokens"]),
                    "events": int(row["events"]),
                }
            )
    return out


def heatmap_grid(rows: list[tuple]) -> dict:
    """(dow, hour, tokens, events) rows → two 7x24 grids + their maxima.

    Both metrics are returned so the page's Tokens/Requests toggle switches
    without a refetch. Rows outside 0..23 / 0..6 cannot occur from
    `extract()`, so they are not silently dropped — an out-of-range hour would
    be a bug worth an IndexError in tests rather than a quietly wrong grid.
    """
    tokens = [[0] * HOURS for _ in range(len(WEEKDAYS))]
    events = [[0] * HOURS for _ in range(len(WEEKDAYS))]
    for dow, hour, tok, ev in rows:
        d = weekday_index(dow)
        h = int(hour)
        tokens[d][h] += int(tok)
        events[d][h] += int(ev)
    return {
        "weekdays": list(WEEKDAYS),
        "tokens": tokens,
        "events": events,
        "max_tokens": max((v for row in tokens for v in row), default=0),
        "max_events": max((v for row in events for v in row), default=0),
    }


def pct_delta(current: int, previous: int) -> Optional[float]:
    """Percent change vs the previous window, or None when there is no baseline.

    Growth from zero is NOT "+100%" and not "+inf" — it is undefined, and the
    console says "no baseline" rather than printing a number that reads like a
    measurement.
    """
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100.0, 1)


def _share(part: int, whole: int) -> float:
    return round(part / whole * 100.0, 1) if whole > 0 else 0.0


# --------------------------------------------------------------------------- #
# SQL aggregates                                                               #
# --------------------------------------------------------------------------- #

_TOKENS = func.coalesce(func.sum(UsageEvent.prompt_tokens), 0) + func.coalesce(
    func.sum(UsageEvent.completion_tokens), 0
)
# `ts` is timestamptz, and `date_trunc`/`extract` over a timestamptz answer in
# the SESSION's TimeZone — so the same row lands in a different bucket (and a
# different heatmap cell) depending on how the database happens to be
# configured. The window is documented as UTC, so the conversion is explicit
# here rather than inherited: `timezone('UTC', ts)` yields the UTC wall clock,
# and the bucket keys that come back are naive (normalised in `_series_rows`).
_TS_UTC = func.timezone("UTC", UsageEvent.ts)


def _conds(start, end, cost_center, model) -> list:
    conds = [UsageEvent.ts >= start, UsageEvent.ts < end]
    if cost_center is not None:
        conds.append(UsageEvent.cost_center_id == cost_center)
    if model:
        conds.append(UsageEvent.model == model)
    return conds


def _totals(session, conds) -> dict:
    row = (
        session.query(
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cached_tokens), 0),
            func.count(UsageEvent.id),
            func.coalesce(func.sum(case((UsageEvent.estimated.is_(True), 1), else_=0)), 0),
        )
        .filter(*conds)
        .one()
    )
    i, o, c, n, est = (int(x or 0) for x in row)
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cached_tokens": c,
        "total_tokens": i + o,
        "events": n,
        "estimated_events": est,
    }


def _as_utc(ts: _dt.datetime) -> _dt.datetime:
    """`timezone('UTC', ts)` comes back NAIVE (it is a wall clock, not an
    instant). The window keys are aware, so re-attach UTC — dropping this makes
    every bucket miss the fill map and the whole series read as zero."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=_dt.timezone.utc)


def _series_rows(session, conds, bucket: str) -> list[dict]:
    bcol = func.date_trunc(bucket, _TS_UTC).label("bucket")
    rows = (
        session.query(
            bcol,
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cached_tokens), 0),
            func.count(UsageEvent.id),
        )
        .filter(*conds)
        .group_by(bcol)
        .order_by(bcol)
        .all()
    )
    return [
        {"bucket": _as_utc(b), "input_tokens": i, "output_tokens": o, "cached_tokens": c, "events": n}
        for (b, i, o, c, n) in rows
    ]


def _top_models(session, conds, top: int, total_tokens: int) -> list[dict]:
    rows = (
        session.query(
            UsageEvent.model,
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.count(UsageEvent.id),
        )
        .filter(*conds)
        .group_by(UsageEvent.model)
        .order_by(_TOKENS.desc())
        .limit(top)
        .all()
    )
    return [
        {
            "model": m,
            "input_tokens": int(i),
            "output_tokens": int(o),
            "total_tokens": int(i) + int(o),
            "events": int(n),
            "share_pct": _share(int(i) + int(o), total_tokens),
        }
        for (m, i, o, n) in rows
    ]


def _by_cost_center(session, conds, top: int, total_tokens: int) -> list[dict]:
    rows = (
        session.query(
            UsageEvent.cost_center_id,
            CostCenter.name,
            CostCenter.team,
            _TOKENS,
            func.count(UsageEvent.id),
        )
        .outerjoin(CostCenter, CostCenter.id == UsageEvent.cost_center_id)
        .filter(*conds)
        .group_by(UsageEvent.cost_center_id, CostCenter.name, CostCenter.team)
        .order_by(_TOKENS.desc())
        .limit(top)
        .all()
    )
    return [
        {
            "cost_center_id": str(cc) if cc is not None else None,
            "name": name,
            "team": team,
            "total_tokens": int(tok),
            "events": int(n),
            "share_pct": _share(int(tok), total_tokens),
        }
        for (cc, name, team, tok, n) in rows
    ]


def _by_key(session, conds, top: int, total_tokens: int) -> list[dict]:
    rows = (
        session.query(
            UsageEvent.api_key_id,
            ApiKey.key_prefix,
            ApiKey.owner_username,
            CostCenter.name,
            _TOKENS,
            func.count(UsageEvent.id),
        )
        .outerjoin(ApiKey, ApiKey.id == UsageEvent.api_key_id)
        .outerjoin(CostCenter, CostCenter.id == UsageEvent.cost_center_id)
        .filter(*conds)
        .group_by(
            UsageEvent.api_key_id, ApiKey.key_prefix, ApiKey.owner_username, CostCenter.name
        )
        .order_by(_TOKENS.desc())
        .limit(top)
        .all()
    )
    return [
        {
            "api_key_id": str(k) if k is not None else None,
            "key_prefix": prefix,
            "owner_username": owner,
            "cost_center": cc_name,
            "total_tokens": int(tok),
            "events": int(n),
            "share_pct": _share(int(tok), total_tokens),
        }
        for (k, prefix, owner, cc_name, tok, n) in rows
    ]


def _heatmap(session, conds) -> dict:
    dow = func.extract("dow", _TS_UTC)
    hour = func.extract("hour", _TS_UTC)
    rows = (
        session.query(dow, hour, _TOKENS, func.count(UsageEvent.id))
        .filter(*conds)
        .group_by(dow, hour)
        .all()
    )
    return heatmap_grid(list(rows))


def usage_analytics(
    session,
    *,
    days: int = 30,
    bucket: str = "day",
    cost_center=None,
    model: Optional[str] = None,
    top: int = 5,
    now: Optional[_dt.datetime] = None,
) -> dict:
    """Assemble the cost-control payload for one window. `session` is injected
    (same seam as `entitlement.monthly_rollup`) so the shape can be driven by
    tests without a database."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    start, end, points = window_bounds(days, bucket, now)
    prev_start = start - (end - start)

    conds = _conds(start, end, cost_center, model)
    totals = _totals(session, conds)
    previous = _totals(session, _conds(prev_start, start, cost_center, model))
    tt = totals["total_tokens"]

    return {
        "window": {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "previous_from": prev_start.isoformat(),
            "days": days,
            "bucket": bucket,
            "points": points,
            "tz": "UTC",
        },
        "filters": {
            "cost_center": str(cost_center) if cost_center is not None else None,
            "model": model,
        },
        "totals": totals,
        "previous": previous,
        "delta_pct": {
            "total_tokens": pct_delta(tt, previous["total_tokens"]),
            "events": pct_delta(totals["events"], previous["events"]),
        },
        "series": fill_series(_series_rows(session, conds, bucket), start, points, bucket),
        "top_models": _top_models(session, conds, top, tt),
        "by_cost_center": _by_cost_center(session, conds, top, tt),
        "by_key": _by_key(session, conds, top, tt),
        "heatmap": _heatmap(session, conds),
    }


def register_analytics_api(app) -> None:
    # Same gate as the rest of the usage surface: "view ALL usage" is the ADMIN
    # tier in the #314 capability matrix (super-admin satisfies it by
    # hierarchy). This endpoint aggregates EVERY key on the box, so it must not
    # be reachable one tier lower than `GET /api/usage`.
    router = APIRouter(dependencies=[Depends(require_role(Role.ADMIN))])

    @router.get("/api/usage/analytics")
    def usage_analytics_report(
        days: int = Query(default=30, ge=1, le=365),
        bucket: str = Query(default="day", pattern="^(hour|day)$"),
        cost_center: Optional[str] = Query(default=None),
        model: Optional[str] = Query(default=None),
        top: int = Query(default=5, ge=1, le=25),
    ):
        if bucket == "hour" and days > MAX_HOUR_DAYS:
            raise HTTPException(
                status_code=422,
                detail=f"bucket=hour supports at most {MAX_HOUR_DAYS} days; use bucket=day",
            )
        cc = parse_uuid(cost_center, "cost_center", status_code=422) if cost_center else None
        with session_scope() as s:
            return usage_analytics(
                s, days=days, bucket=bucket, cost_center=cc, model=model, top=top
            )

    app.include_router(router)
