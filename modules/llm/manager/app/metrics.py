# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Native business metrics (U2) + the in-process recording state.

The recording helpers (``observe_proxy_latency``, ``add_tokens``,
``incr_requests``, ``incr_failover``) are written by the hot path (M3/M4);
the ``/metrics`` Prometheus endpoint (``register_metrics``, U2) renders a
snapshot. TOKEN-ONLY: the token series carry ``type=input|output|cached``
counts — never currency.

State is a plain module-level dict (no prometheus_client global registry) so
it stays import-safe and free of duplicate-timeseries headaches in tests.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict

# Imported at MODULE scope: with `from __future__ import annotations`, route
# annotations are strings resolved against module globals — a function-local
# `Request` import would leave the endpoint's `request: Request` unresolvable
# and FastAPI would treat `request` as a query param (spurious 422).
from fastapi import Request
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

# #324: `model` on the per-model series is CLIENT-SUPPLIED — the proxy reads it
# straight out of the request body and records it BEFORE any upstream call, so it
# need not even name a real deployment. Unbounded, an authenticated key holder could
# grow these maps (and the Prometheus TSDB scraping them) without limit until the
# manager hits its 1 GiB mem_limit. Track at most _MAX_MODEL_SERIES distinct names
# and fold the rest into a single `other` bucket, so the dimension stays useful for
# a real fleet (a handful of deployments) and bounded under abuse.
#
# The token series keys on (cost_center, api_key) — both DB-issued UUIDs an admin
# creates, so NOT client-controlled and deliberately left un-bucketed: those labels
# feed the observability dashboards and per-key billing display, and silently
# folding them into `other` would be worse than the (admin-bounded) growth.
_MAX_MODEL_SERIES = 100
_OTHER_MODEL = "other"

_LOCK = threading.Lock()
_STATE = {
    # (cost_center, api_key, type) -> total tokens
    "tokens": defaultdict(int),
    "proxy_latency_seconds_sum": 0.0,
    "proxy_latency_count": 0,
    "requests_total": 0,
    "failover_total": 0,
    "meter_rejected_total": 0,
    "entitlement_rejected_total": 0,
    # #311 per-model performance dimension (since process start)
    "requests_by_model": defaultdict(int),
    "latency_sum_by_model": defaultdict(float),
    "latency_count_by_model": defaultdict(int),
    # #350 playground consumption. Since migration 0016 the playground ALSO
    # writes a real usage_events row under the reserved operator-playground
    # identity — this counter stays as the cheap per-model live view and as
    # the reconciliation cross-check against those rows.
    "playground_tokens": defaultdict(int),     # (model, type) -> tokens
    "playground_requests_by_model": defaultdict(int),
}


def reset() -> None:
    """Test helper — clear all counters."""
    with _LOCK:
        _STATE["tokens"] = defaultdict(int)
        _STATE["proxy_latency_seconds_sum"] = 0.0
        _STATE["proxy_latency_count"] = 0
        _STATE["requests_total"] = 0
        _STATE["failover_total"] = 0
        _STATE["meter_rejected_total"] = 0
        _STATE["entitlement_rejected_total"] = 0
        _STATE["requests_by_model"] = defaultdict(int)
        _STATE["latency_sum_by_model"] = defaultdict(float)
        _STATE["latency_count_by_model"] = defaultdict(int)
        _STATE["playground_tokens"] = defaultdict(int)
        _STATE["playground_requests_by_model"] = defaultdict(int)


def _bounded_model(model: str) -> str:
    """#324 cardinality guard — caller MUST hold ``_LOCK``.

    ``requests_by_model`` is the authoritative set of tracked names: the proxy calls
    ``incr_requests`` before ``observe_proxy_latency`` for the same request, so both
    resolve a given model to the same bucket."""
    known = _STATE["requests_by_model"]
    if model in known or len(known) < _MAX_MODEL_SERIES:
        return model
    return _OTHER_MODEL


def observe_proxy_latency(seconds: float, model: str = "") -> None:
    with _LOCK:
        _STATE["proxy_latency_seconds_sum"] += float(seconds)
        _STATE["proxy_latency_count"] += 1
        if model:
            key = _bounded_model(model)
            _STATE["latency_sum_by_model"][key] += float(seconds)
            _STATE["latency_count_by_model"][key] += 1


def incr_requests(n: int = 1, model: str = "") -> None:
    with _LOCK:
        _STATE["requests_total"] += n
        if model:
            _STATE["requests_by_model"][_bounded_model(model)] += n


def incr_failover(n: int = 1) -> None:
    with _LOCK:
        _STATE["failover_total"] += n


def incr_meter_rejected(n: int = 1) -> None:
    """Requests refused by the metering_mode=strict billing-meter gate
    (usage store unreachable). 0 in metering_mode=available."""
    with _LOCK:
        _STATE["meter_rejected_total"] += n


def incr_entitlement_rejected(n: int = 1) -> None:
    """Requests refused by the entitlement_mode=enforce gate (subscription
    inactive/expired/absent). 0 in entitlement_mode=report (#265)."""
    with _LOCK:
        _STATE["entitlement_rejected_total"] += n


def add_playground_tokens(model: str, *, input: int = 0, output: int = 0,
                         cached: int = 0) -> None:
    """#350 Record playground consumption, broken down per MODEL.

    A different axis than `orchestrator_tokens_total`, which is keyed by
    cost-centre + key — this answers "which model is the console burning"
    without a DB query. #1024: since the playground meters through the same
    path as /v1, its tokens now also appear in the billing series, under the
    reserved `operator-playground` cost-centre; that label, not a separate
    series, is what keeps operator diagnostics from being read as customer
    traffic (playground.py, migration 0016).
    """
    with _LOCK:
        t = _STATE["playground_tokens"]
        _STATE["playground_requests_by_model"][model] += 1
        if input:
            t[(model, "input")] += int(input)
        if output:
            t[(model, "output")] += int(output)
        if cached:
            t[(model, "cached")] += int(cached)


def add_tokens(
    cost_center: str, api_key: str = "", *, input: int = 0, output: int = 0, cached: int = 0
) -> None:
    with _LOCK:
        t = _STATE["tokens"]
        if input:
            t[(cost_center, api_key, "input")] += int(input)
        if output:
            t[(cost_center, api_key, "output")] += int(output)
        if cached:
            t[(cost_center, api_key, "cached")] += int(cached)


def snapshot() -> dict:
    with _LOCK:
        return {
            "tokens": dict(_STATE["tokens"]),
            "proxy_latency_seconds_sum": _STATE["proxy_latency_seconds_sum"],
            "proxy_latency_count": _STATE["proxy_latency_count"],
            "requests_total": _STATE["requests_total"],
            "failover_total": _STATE["failover_total"],
            "meter_rejected_total": _STATE["meter_rejected_total"],
            "entitlement_rejected_total": _STATE["entitlement_rejected_total"],
            "requests_by_model": dict(_STATE["requests_by_model"]),
            "playground_tokens": dict(_STATE["playground_tokens"]),
            "playground_requests_by_model": dict(_STATE["playground_requests_by_model"]),
            "latency_sum_by_model": dict(_STATE["latency_sum_by_model"]),
            "latency_count_by_model": dict(_STATE["latency_count_by_model"]),
        }


def stats_json() -> dict:
    """#311 JSON perf snapshot for the console Dashboard (since process start).
    Global rollup + per-model requests + avg added-latency. Token totals stay in
    the durable usage store (per-model, 24h) — the dashboard merges the two."""
    snap = snapshot()
    cnt = snap["proxy_latency_count"]
    avg_ms = (snap["proxy_latency_seconds_sum"] / cnt * 1000.0) if cnt else 0.0
    models = []
    for m, req in snap["requests_by_model"].items():
        lc = snap["latency_count_by_model"].get(m, 0)
        la_ms = (snap["latency_sum_by_model"].get(m, 0.0) / lc * 1000.0) if lc else 0.0
        models.append({"model": m, "requests": req, "avg_latency_ms": round(la_ms, 1)})
    models.sort(key=lambda x: x["requests"], reverse=True)
    return {
        "requests_total": snap["requests_total"],
        "avg_latency_ms": round(avg_ms, 1),
        "failover_total": snap["failover_total"],
        "meter_rejected_total": snap["meter_rejected_total"],
        "entitlement_rejected_total": snap["entitlement_rejected_total"],
        "models": models,
    }


def _escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(number) -> str:
    # Integers without a trailing .0; floats plainly.
    if isinstance(number, int):
        return str(number)
    if float(number).is_integer():
        return str(int(number))
    return repr(float(number))


def _default_active_keys() -> int:
    """Count active api_keys. Best-effort: any datastore error → 0 so a
    metrics scrape never hangs or 500s on a datastore blip."""
    try:
        from sqlalchemy import func

        from app.db import session_scope
        from app.models import ApiKey

        with session_scope() as s:
            return int(
                s.query(func.count(ApiKey.id)).filter(ApiKey.status == "active").scalar() or 0
            )
    except Exception:  # pragma: no cover - defensive
        return 0


def render_exposition(active_keys: int, *, in_flight=None, in_flight_limit=None) -> str:
    snap = snapshot()
    lines: list[str] = []

    lines.append("# HELP orchestrator_tokens_total Tokens metered per API key (token-only chargeback).")
    lines.append("# TYPE orchestrator_tokens_total counter")
    for (cost_center, api_key, ttype), value in sorted(snap["tokens"].items()):
        labels = (
            f'cost_center="{_escape_label(cost_center)}",'
            f'api_key="{_escape_label(api_key)}",'
            f'type="{_escape_label(ttype)}"'
        )
        lines.append(f"orchestrator_tokens_total{{{labels}}} {_fmt(value)}")

    lines.append("# HELP orchestrator_proxy_latency_seconds Added proxy latency.")
    lines.append("# TYPE orchestrator_proxy_latency_seconds summary")
    lines.append(f"orchestrator_proxy_latency_seconds_sum {_fmt(snap['proxy_latency_seconds_sum'])}")
    lines.append(f"orchestrator_proxy_latency_seconds_count {_fmt(snap['proxy_latency_count'])}")

    lines.append("# HELP orchestrator_requests_total Proxied requests.")
    lines.append("# TYPE orchestrator_requests_total counter")
    lines.append(f"orchestrator_requests_total {_fmt(snap['requests_total'])}")

    # #311 per-model dimension: requests + added-latency summary per model.
    lines.append("# HELP orchestrator_model_requests_total Proxied requests per model.")
    lines.append("# TYPE orchestrator_model_requests_total counter")
    for model, value in sorted(snap["requests_by_model"].items()):
        lines.append(f'orchestrator_model_requests_total{{model="{_escape_label(model)}"}} {_fmt(value)}')
    lines.append("# HELP orchestrator_model_latency_seconds Added proxy latency per model.")
    lines.append("# TYPE orchestrator_model_latency_seconds summary")
    for model in sorted(snap["latency_count_by_model"]):
        ml = _escape_label(model)
        lines.append(f'orchestrator_model_latency_seconds_sum{{model="{ml}"}} {_fmt(snap["latency_sum_by_model"][model])}')
        lines.append(f'orchestrator_model_latency_seconds_count{{model="{ml}"}} {_fmt(snap["latency_count_by_model"][model])}')

    # #350 playground consumption, per MODEL. It began life as the stand-in for
    # a usage_events row the playground did not write; the row exists now
    # (migration 0016) and #1024 routes it through the same metering as /v1, so
    # this is the per-model VIEW of the same consumption rather than a gap.
    lines.append("# HELP orchestrator_playground_tokens_total Tokens consumed by the console playground (also in usage_events under the operator-playground cost-centre since 0016).")
    lines.append("# TYPE orchestrator_playground_tokens_total counter")
    for (model, ttype), value in sorted(snap["playground_tokens"].items()):
        labels = f'model="{_escape_label(model)}",type="{_escape_label(ttype)}"'
        lines.append(f"orchestrator_playground_tokens_total{{{labels}}} {_fmt(value)}")
    lines.append("# HELP orchestrator_playground_requests_total Playground requests per model (engine-reported usage only; the metered row is in usage_events).")
    lines.append("# TYPE orchestrator_playground_requests_total counter")
    for model, value in sorted(snap["playground_requests_by_model"].items()):
        lines.append(f'orchestrator_playground_requests_total{{model="{_escape_label(model)}"}} {_fmt(value)}')

    lines.append("# HELP orchestrator_failover_total Upstream failovers observed.")
    lines.append("# TYPE orchestrator_failover_total counter")
    lines.append(f"orchestrator_failover_total {_fmt(snap['failover_total'])}")

    lines.append("# HELP orchestrator_meter_rejected_total Requests refused by the strict billing-meter gate (usage store unreachable).")
    lines.append("# TYPE orchestrator_meter_rejected_total counter")
    lines.append(f"orchestrator_meter_rejected_total {_fmt(snap['meter_rejected_total'])}")
    lines.append("# HELP orchestrator_entitlement_rejected_total Requests refused by the enforce entitlement gate (subscription inactive/expired/absent).")
    lines.append("# TYPE orchestrator_entitlement_rejected_total counter")
    lines.append(f"orchestrator_entitlement_rejected_total {_fmt(snap['entitlement_rejected_total'])}")

    lines.append("# HELP orchestrator_active_api_keys Active API keys.")
    lines.append("# TYPE orchestrator_active_api_keys gauge")
    lines.append(f"orchestrator_active_api_keys {_fmt(active_keys)}")

    # #1060: the #19 box-wide in-flight gate. Unlike everything above, this is
    # not accumulated in this module's snapshot — it is LIVE state owned by
    # `proxy.concurrency_gate_for(app)`, so the caller passes it in rather than
    # this module keeping a second copy that could disagree with the counter
    # the proxy actually admits against.
    #
    # Both keywords are optional and both are omitted together when the gate
    # could not be read: a rendered 0 would read as "idle box", which is the
    # exact wrong conclusion when the truth is "we don't know". A gate that IS
    # readable and unbounded reports limit 0 — that is a fact, not an absence.
    if in_flight is not None and in_flight_limit is not None:
        lines.append("# HELP orchestrator_inflight_requests Completions currently holding a box-wide concurrency slot (#19).")
        lines.append("# TYPE orchestrator_inflight_requests gauge")
        lines.append(f"orchestrator_inflight_requests {_fmt(in_flight)}")
        lines.append("# HELP orchestrator_inflight_limit Configured box-wide concurrency cap (0 = unbounded).")
        lines.append("# TYPE orchestrator_inflight_limit gauge")
        lines.append(f"orchestrator_inflight_limit {_fmt(in_flight_limit)}")

    return "\n".join(lines) + "\n"


def _inflight_gauge(app):
    """``(in_flight, limit)`` off the LIVE #19 gate, or ``(None, None)``.

    Resolved through ``proxy.concurrency_gate_for`` — the single resolver the
    /v1 verbs and the console playground both admit against — so the gauge can
    never report a different gate from the one returning the 429s. Imported
    lazily: ``proxy`` imports this module, so a module-level import would be a
    cycle.

    Never raises. ``/metrics`` is what an operator reaches for WHILE the box is
    misbehaving; losing the whole scrape (tokens, latency, active keys) because
    the counter could not be read would take away the diagnosis at the one
    moment it is needed.
    """
    try:
        from app.proxy import concurrency_gate_for

        counter, limit = concurrency_gate_for(app)
        return int(counter.value), int(limit or 0)
    except Exception:  # pragma: no cover - defensive; exercised by the unit test
        logger.warning("in-flight gauge unavailable for this scrape", exc_info=True)
        return None, None


def register_metrics(app) -> None:
    """Register GET /metrics — Prometheus exposition of the manager's
    authoritative business metrics. TOKEN-ONLY: no currency anywhere.

    The active-keys count comes from ``app.state.active_keys_provider`` if
    set (tests inject one), else a best-effort DB count.
    """

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint(request: Request):
        provider = getattr(request.app.state, "active_keys_provider", None) or _default_active_keys
        try:
            active = int(provider())
        except Exception:  # pragma: no cover - defensive
            active = 0
        # #1060: the live #19 gate, read defensively — see `_inflight_gauge`.
        in_flight, in_flight_limit = _inflight_gauge(request.app)
        return PlainTextResponse(
            render_exposition(active, in_flight=in_flight,
                              in_flight_limit=in_flight_limit),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
