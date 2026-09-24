# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""LiteLLM router config generation (R1).

The manager GENERATES LiteLLM's config from its own state — LiteLLM is a
PURE ROUTER (load-balance + failover only). It holds NO end-customer keys
and NO spend/budget: the manager owns keys + TOKEN-ONLY metering. Multiple
ready instances of one deployment share a model_name → a LiteLLM
load-balance group; failover is num_retries/allowed_fails/cooldown_time.

Request is imported at module scope (FastAPI + `from __future__ import
annotations`).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, Request

from app.authz import Role, require_role
from app.config import get_settings

logger = logging.getLogger(__name__)

# Instance states whose endpoints are eligible to serve traffic.
_SERVEABLE_INSTANCE_STATUS = {"ready"}


# Advanced request-level params the manager forwards to the engine as a
# serving default. They CANNOT reach the backend on their own: LiteLLM runs with
# drop_params=True (strips anything outside the OpenAI schema), and llama.cpp /
# llama-box only honour reasoning/thinking controls NESTED under
# ``chat_template_kwargs`` — a top-level ``enable_thinking`` (which Dify's
# openai-compatible provider sends) is silently dropped. So the manager promotes
# a deployment's configured advanced params into ``litellm_params.extra_body``,
# which LiteLLM forwards verbatim regardless of drop_params. A client may still
# override per request via its own ``chat_template_kwargs``.
_TEMPLATE_KW_KEYS = ("enable_thinking",)  # top-level params promoted into chat_template_kwargs


def _extra_body(params: dict | None) -> dict:
    """Build LiteLLM ``extra_body`` from a deployment's advanced serving params.

    Merges an explicit ``chat_template_kwargs`` dict and promotes recognised
    top-level template controls (e.g. ``enable_thinking``) into it. Returns ``{}``
    when there is nothing to forward (keeps chat models' entries minimal)."""
    p = params or {}
    ctk = dict(p.get("chat_template_kwargs") or {})
    for k in _TEMPLATE_KW_KEYS:
        if k in p and k not in ctk:
            ctk[k] = p[k]
    return {"chat_template_kwargs": ctk} if ctk else {}


def _litellm_mode(task: str | None) -> str | None:
    """Map a deployment serve-task to the LiteLLM ``model_info.mode`` that makes
    ``/v1/embeddings`` + ``/v1/rerank`` route to this deployment. A chat model
    needs no mode (default), so return None to keep the entry minimal."""
    t = (task or "chat").strip().lower()
    if t in ("embed", "embedding", "embeddings"):
        return "embedding"
    if t in ("rerank", "reranker", "reranking", "score"):
        return "rerank"
    return None


def build_model_list(deployments: list[dict]) -> list[dict]:
    """One LiteLLM model_list entry per (deployment, endpoint). Two endpoints
    for the same model_name form a load-balance group.

    Each endpoint is either a bare URL string (no upstream key → api_key
    "none") or a ``{"endpoint": url, "api_key": key}`` dict (P2-B3: the manager
    injects the backend's own key router-side; the client never sees it).

    An embed/rerank deployment carries ``model_info.mode`` so LiteLLM routes
    ``/v1/embeddings`` + ``/v1/rerank`` to it (a chat model omits it)."""
    entries: list[dict] = []
    for dep in deployments:
        model_name = dep["model_name"]
        served = dep.get("served_model") or model_name
        mode = _litellm_mode(dep.get("task"))
        # LiteLLM's rerank API REJECTS the "openai" provider ("Unsupported
        # provider: openai"); a rerank deployment must use a rerank-capable
        # provider. Our engines (llama.cpp / llama-box) serve an OpenAI-style
        # /v1/rerank returning {results:[{index, relevance_score}]}, which is
        # exactly the shape LiteLLM's `infinity` provider posts to and parses.
        # chat + embeddings route fine through `openai`.
        provider = "infinity" if mode == "rerank" else "openai"
        for endpoint in dep.get("endpoints") or []:
            if isinstance(endpoint, dict):
                api_base = endpoint.get("endpoint")
                api_key = endpoint.get("api_key")
            else:
                api_base = endpoint
                api_key = None
            entry = {
                "model_name": model_name,
                "litellm_params": {
                    "model": f"{provider}/{served}",
                    "api_base": api_base,
                    "api_key": api_key or "none",
                },
            }
            eb = _extra_body(dep.get("params"))
            if eb:
                # advanced serving defaults (e.g. thinking-off) → forwarded to the
                # engine nested under chat_template_kwargs, surviving drop_params.
                entry["litellm_params"]["extra_body"] = eb
            if mode:
                entry["model_info"] = {"mode": mode}
            if mode == "embedding":
                # LiteLLM forwards `encoding_format: null` when the client omits
                # it; llama.cpp/llama-box's strict JSON parser then 500s
                # "[json.exception.type_error.302] type must be string, but is
                # null" (#310). Pin a valid string default so the engine is happy
                # (a client that sends its own value still overrides this).
                entry["litellm_params"]["encoding_format"] = "float"
            entries.append(entry)
    return entries


def render_router_config(
    deployments: list[dict],
    *,
    valkey_host: str = "valkey",
    valkey_port: int = 6379,
    otel_endpoint: str = "",
) -> dict:
    """Full LiteLLM config dict — pure router (no keys/spend).

    ``otel_endpoint`` set (observability module ON) → add LiteLLM's native
    ``otel`` callback so a span per request is exported to the otel-collector
    (LiteLLM reads OTEL_EXPORTER_OTLP_ENDPOINT/_PROTOCOL from the router's env).
    Empty → the config is byte-for-byte the pre-observability one (no callback,
    nothing emitted, no export errors when the collector isn't running)."""
    litellm_settings: dict = {"drop_params": True}
    if otel_endpoint:
        litellm_settings["callbacks"] = ["otel"]
    return {
        "model_list": build_model_list(deployments),
        "router_settings": {
            "routing_strategy": "latency-based-routing",
            "num_retries": 2,
            # #2026: `allowed_fails` is deliberately NOT set, and that absence is
            # load-bearing — it is what selects LiteLLM's CURRENT cooldown policy
            # over its legacy one.
            #
            # Setting it (to any value) makes `_is_allowed_fails_set_on_router()`
            # true, and `_should_cooldown_deployment` then takes the v1 branch —
            # "more than N fails a minute, cool the deployment down" — which has
            # no notion of how many deployments the model group has. The v2
            # branch, which runs only when this key is absent, opens with:
            #
            #     ## BASE CASE - single deployment
            #     if model_group is not None and len(model_group) == 1:
            #         is_single_deployment_model_group = True
            #     ...
            #     if exception_status_int == 429 and not is_single_deployment_model_group:
            #
            # A cooldown exists so the router can route AROUND a bad replica.
            # Almost every model on a razzfazz box is a single deployment, so
            # there is nothing to route to and the cooldown just converts a brief
            # relaunch into a total outage. Measured on 0.91 with
            # `allowed_fails: 2` (granite-docling, one replica, apply-params):
            # THREE 502s from the relaunch became 1917 requests answered 429 over
            # exactly `cooldown_time`, and under continuous load it did not
            # recover at all.
            #
            # The predicate is nastier than a threshold, which is why the key
            # cannot simply be given a "better" value (DevBox-Vuko):
            #
            #     if router.allowed_fails is None:                 return False
            #     if router.allowed_fails != litellm.allowed_fails: return True
            #     return False
            #
            # It treats "equal to the library default" as NOT SET. So the key
            # does not scale a threshold — the DIFFERENCE from the default picks
            # a different algorithm. `allowed_fails: 3` would have selected v2
            # and none of this would exist; `2` reads like a slightly stricter
            # `3` and is a different policy with the single-deployment
            # protection removed.
            #
            # Verified against the pinned `main-v1.83.7-stable`: `allowed_fails`
            # defaults to None on Router, `litellm.allowed_fails` is 3, and
            # `_should_retry(502)` is True — so the relaunch's own error does not
            # cool anything down under v2 either.
            #
            # ON A LITELLM BUMP, RE-MEASURE THIS — it is four lines, and it is
            # the only thing that answers the question:
            #
            #   1. pick a deployment with replicas=1 that is serving;
            #   2. drive it with a few concurrent clients (any load generator);
            #   3. POST /api/deployments/<id>/apply-params mid-flight;
            #   4. count 429s. Before the fix: 1917 of them over exactly
            #      `cooldown_time`, from three real 502s. After: zero.
            #
            # A 429 appearing again means the bump moved the branch above and
            # this key's absence no longer selects v2.
            "cooldown_time": 30,
            "redis_host": valkey_host,
            "redis_port": valkey_port,
            # The shared valkey runs with requirepass; LiteLLM resolves this
            # os.environ ref from the router container's VALKEY_PASSWORD env.
            # Without it the router's redis handshake fails (AuthenticationError)
            # and completions 500.
            "redis_password": "os.environ/VALKEY_PASSWORD",
        },
        "general_settings": {
            # known ONLY to the manager; resolved by LiteLLM from its env.
            "master_key": "os.environ/LITELLM_INTERNAL_KEY",
        },
        "litellm_settings": litellm_settings,
    }


def to_yaml(config: dict) -> str:
    return yaml.safe_dump(config, sort_keys=False)


#: What a redacted upstream credential reads as in the preview response.
REDACTED = "***"


def redact_config(config: dict) -> dict:
    """LLMM-7: a copy of ``config`` with every upstream credential removed.

    ``generate_from_db`` puts a REAL secret in ``litellm_params.api_key``: the
    manager↔router master key (``litellm_internal_key``) for a relay-routed
    instance — which is also the sole credential the relay HTTP route accepts
    (``app/relay_http.py``) — or the backend's own recoverable upstream key for
    a co-located one. ``write_router_config`` needs those values; the
    ``GET /api/router/config`` PREVIEW does not: it exists so an operator can
    see the routing shape, and returning the master key as JSON puts it in
    browser history, screenshots, HAR dumps and any log that captures the
    response body.

    So the preview is redacted here, matching the sibling settings API's stated
    contract ("Secrets are never returned or set here", ``app/api/settings.py``)
    while keeping the information an operator actually reads a preview for: each
    entry gains ``has_api_key`` so "is a credential attached to this endpoint?"
    is still answerable. ``general_settings.master_key`` is left alone — it is
    the literal string ``os.environ/LITELLM_INTERNAL_KEY``, a reference, not a
    secret. The unredacted config keeps flowing to ``write_router_config``,
    which is the only caller that needs the real values.
    """
    out = dict(config)
    entries = []
    for entry in config.get("model_list") or []:
        entry = dict(entry)
        params = dict(entry.get("litellm_params") or {})
        key = params.get("api_key")
        # "none" is the placeholder build_model_list writes when an endpoint has
        # no upstream key at all — not a secret, and reporting it as one would
        # make every preview look like it carries credentials.
        entry["has_api_key"] = bool(key) and key != "none"
        if entry["has_api_key"]:
            params["api_key"] = REDACTED
        entry["litellm_params"] = params
        entries.append(entry)
    out["model_list"] = entries
    return out


def _is_relay_routed(labels: Optional[dict]) -> bool:
    """#262: is this instance's owning worker a REMOTE worker whose traffic
    must go back through the master's WS relay?

    True iff labels are present, NOT external, and carry a non-empty
    ``advertise_addr`` — the EXACT condition ``_host_routable_endpoint`` uses to
    decide it rewrites the endpoint to the relay route. Kept as a shared helper
    so the endpoint choice and the api_key choice (generate_from_db) can never
    disagree about what "relay-routed" means (don't string-match the URL —
    fragile)."""
    if not labels or labels.get("external"):
        return False
    return bool(str(labels.get("advertise_addr") or "").strip())


def _relay_worker_is_live(worker, now: Optional[datetime] = None) -> bool:
    """#1422 follow-up: is the REMOTE worker behind a relay-routed instance
    still there? The relay route only works while the worker holds its WS
    connection, and a worker whose heartbeat has gone stale (or whose status
    is no longer ready) has no such connection — routing to it answers 502.
    After a reschedule the lost worker's instance rows stay ``ready`` in the
    table, so without this the router emitted BOTH the new engine and the dead
    relay under one model name and LiteLLM round-robined onto the 502.

    Deliberately RELAY-ONLY: a co-located instance on a briefly stale worker
    (every worker looks stale for up to the staleness window after a manager
    restart, #917) keeps serving from its engine container — dropping those
    would take every model off the router on each restart.

    A worker row without status/heartbeat attributes (older fixtures, partial
    stand-ins) counts as live: this filter removes only what it can prove dead."""
    status = getattr(worker, "status", None)
    if status is not None and status != "ready":
        return False
    hb = getattr(worker, "last_heartbeat", None)
    if hb is None:
        return True
    try:
        stale_s = float(os.environ.get("LLM_MANAGER_WORKER_STALE_SECONDS", "90"))
    except (TypeError, ValueError):
        stale_s = 90.0
    now = now or datetime.now(timezone.utc)
    if getattr(hb, "tzinfo", None) is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (now - hb).total_seconds() <= stale_s


_relay_worker_is_live_impl = _relay_worker_is_live


def _host_routable_endpoint(
    endpoint: str,
    labels: Optional[dict],
    *,
    worker_id: object = None,
    relay_internal_base: str = "",
) -> str:
    """#262 Task 4: choose the router-config-time endpoint for one deployment
    instance — either the direct container-DNS endpoint, or the manager's
    relay HTTP route for a REMOTE worker.

    ``labels`` is the owning ``Worker.labels`` (or None if the worker row is
    missing — a stale FK / race, handled the same as "no advertise_addr":
    leave the endpoint alone rather than raise).

    - No ``advertise_addr`` (a CO-LOCATED worker — shares the Docker network,
      container DNS already resolves the engine's container name) -> endpoint
      UNCHANGED. This is the #913 same-network behaviour; it must never break,
      which is why it is checked first.
    - An EXTERNAL backend (#307, e.g. a Mac running Ollama) -> NEVER rewritten.
      Its endpoint is already a real, pre-registered URL, not a container
      name, and must be left byte-for-byte alone even if it somehow also
      carried an advertise_addr.
    - ``advertise_addr`` PRESENT (and not external) -> REMOTE worker. #262
      Task 4 ruling (operator-confirmed uniform-443 model: "same for wan and
      lan, only port 443 from worker->master; if the worker is on the same
      machine the WS is not necessary"): the master no longer dials the
      worker's advertised address directly — it routes back through the
      worker's inbound WS relay, uniformly for LAN and WAN remote workers.
      This DELIBERATELY SUPERSEDES the former advertise_addr -> LAN-address
      rewrite. The composed URL is the manager app's own relay HTTP route
      (`GET/POST {relay_internal_base}/relay/{worker_id}/v1`,
      `app.relay_http.register_http_relay`); ``worker_id`` is the Worker
      row's uuid, matching what the relay WS endpoint parses on the inbound
      side (`app.relay.relay_ws` -> `parse_uuid(worker_id)`). The instance's
      own endpoint path is deliberately NOT preserved here (unlike the old
      LAN rewrite) — every relay-routed instance uses the fixed `/v1` suffix
      the relay route expects, per the #262 Task 4 spec.
    """
    if not _is_relay_routed(labels):
        return endpoint
    return f"{relay_internal_base}/relay/{worker_id}/v1"


def generate_from_db(session) -> list[dict]:
    """Build the deployment→endpoints list from llm_manager_db: each
    deployment with ≥1 READY instance that has an endpoint."""
    from app.models import Deployment, DeploymentInstance, Model, Worker

    # #262 Task 4: RELAY_INTERNAL_BASE is read once per call (get_settings()
    # is deliberately un-cached — see app.config — so tests that flip the env
    # var between cases see it take effect immediately).
    settings = get_settings()
    relay_internal_base = settings.relay_internal_base
    # #262 review round 2 (PR #933): a relay-routed instance's upstream key must
    # be the MASTER's internal key, because LiteLLM dials the relay HTTP route
    # (llm-manager:8080/relay/<id>/v1) — NOT the worker's engine directly — and
    # that route now authenticates the caller against litellm_internal_key
    # (app.relay_http._relay_http, FIX 1). Sending the worker's engine key there
    # would 403. A co-located instance keeps its engine key (it dials the engine
    # container directly, no relay hop).
    litellm_internal_key = settings.litellm_internal_key

    # #262: workers are looked up ONCE, keyed by id, purely to resolve each
    # instance's advertise_addr for the endpoint rewrite below. This is
    # additive to the #321 ordering guarantee, not a replacement for it — the
    # OUTPUT order is still governed solely by the Deployment/DeploymentInstance
    # ORDER BY beneath; a dict keyed by worker id contributes no order of its
    # own to the result.
    workers_by_id = {w.id: w for w in session.query(Worker).all()}

    # #321: ORDER BY is load-bearing, not cosmetic. `to_yaml` uses
    # `sort_keys=False`, so DB row order IS byte order — and the router restarts
    # LiteLLM whenever the file's md5 changes, killing every in-flight
    # completion. `rebuild_from_state()` runs on every worker registration
    # (~30s per node), and each one UPDATEs `deployment_instances`; in Postgres
    # an UPDATE writes a new tuple version, moving the row in heap order, so an
    # unordered SELECT legitimately returns a different sequence over time.
    #
    # Two rows swapping positions was therefore enough to drop every stream on
    # the box. Ordering by primary key makes the generated bytes a function of
    # the STATE rather than of the storage layout, which is what the router's
    # "steady-state rewrites of identical bytes cause no churn" assumption
    # (modules/llm/manager/compose.yml) has always required.
    # #929 M1: which model_names each relay-routed worker ends up serving. The
    # placement guard (`inventory._assert_relay_single_engine`) stops a SECOND
    # model reaching a remote worker from here on, but a box that was already in
    # that state before the guard existed keeps generating a config where two
    # model_names point at the SAME `/relay/{worker_id}/v1` endpoint — and the
    # node answers both from whichever engine is ready first. That is invisible
    # in the generated YAML (two perfectly well-formed entries), so it is said
    # out loud once per rebuild instead.
    relay_models: dict[str, set[str]] = {}
    dropped_dead_relay: list[tuple[str, object]] = []

    out: list[dict] = []
    for dep in session.query(Deployment).order_by(Deployment.id).all():
        endpoints = []
        for inst in (
            session.query(DeploymentInstance)
            .filter(DeploymentInstance.deployment_id == dep.id)
            .order_by(DeploymentInstance.id)
            .all()
        ):
            if inst.status not in _SERVEABLE_INSTANCE_STATUS or not inst.endpoint:
                continue
            worker = workers_by_id.get(getattr(inst, "worker_id", None))
            labels = worker.labels if worker is not None else None
            if _is_relay_routed(labels) and worker is not None and not _relay_worker_is_live(worker):
                # #1422 follow-up: a dead remote worker's relay answers 502 —
                # leave its instance out rather than round-robin onto it.
                dropped_dead_relay.append((dep.model_name, getattr(worker, "name", worker.id)))
                continue
            endpoint = _host_routable_endpoint(
                inst.endpoint,
                labels,
                worker_id=worker.id if worker is not None else None,
                relay_internal_base=relay_internal_base,
            )
            # Matched pair with app.relay_http._relay_http's auth: router sends
            # the internal key for a relay-routed instance ⇄ the relay route
            # requires exactly that key. Co-located instances keep the engine key.
            api_key = litellm_internal_key if _is_relay_routed(labels) else inst.api_key
            # #1535: only a worker that cannot pick its own engine is at risk
            # of answering one model's request from another's weights. A worker
            # that declares `relay_model_routing` serves several models by
            # design, so warning about it would train the operator to ignore
            # the line that still matters.
            if (_is_relay_routed(labels) and worker is not None
                    and not (labels or {}).get("relay_model_routing")):
                relay_models.setdefault(str(worker.id), set()).add(dep.model_name)
            endpoints.append({"endpoint": endpoint, "api_key": api_key})
        if not endpoints:
            continue
        model = session.get(Model, dep.model_id)
        out.append(
            {
                "model_name": dep.model_name,
                "served_model": model.name if model else dep.model_name,
                "task": getattr(dep, "task", "chat"),
                "params": dep.params or {},   # advanced serving params → extra_body
                "endpoints": endpoints,
            }
        )
    for worker_id, names in relay_models.items():
        if len(names) > 1:
            logger.warning(
                "router config (#929 M1): remote worker %s serves %d models "
                "(%s) through ONE relay endpoint — the MVP2 relay is "
                "single-engine per worker (its agent predates #1535 and does "
                "not route by model), so requests for any of them are "
                "answered by whichever engine is ready first. Upgrade the node "
                "agent, undeploy all but one, or move them to separate workers.",
                worker_id, len(names), ", ".join(sorted(names)),
            )
    if dropped_dead_relay:
        logger.info(
            "router config (#1422): left out %d relay-routed instance(s) on workers "
            "that are no longer live: %s",
            len(dropped_dead_relay),
            ", ".join(f"{m} on {w}" for m, w in dropped_dead_relay))
    return out


def _model_list_signature(config: dict) -> set:
    """(model_name, endpoint) for every entry — what the router actually routes.

    Deliberately NOT the whole document: api keys rotate and settings blocks
    move without changing where a request goes, and a delta that reports those
    would drown the one that matters.
    """
    out = set()
    for entry in (config.get("model_list") or []):
        params = entry.get("litellm_params") or {}
        out.add((str(entry.get("model_name")),
                 str(params.get("api_base") or params.get("base_url") or "")))
    return out


def write_router_config(path: str, config: dict) -> None:
    """Write the router config, and SAY what changed when it did.

    #1955: the router's entrypoint restarts LiteLLM whenever this file's content
    hash moves, and a restart costs the requests in flight — measured on 0.91
    during the 2026-09-12 PSA run: two reloads inside 38 minutes, 3 of 37
    requests died with `502 router unreachable`. Nothing anywhere said WHY the
    file had changed, so the investigation had to start from the timestamps.
    #321 made these bytes a function of the fleet's state precisely so that a
    steady state writes identical bytes and nothing restarts — which means every
    rewrite that DOES differ is a state change worth naming.

    Quiet by construction in the steady state: identical bytes log nothing, so
    this cannot become the noise it exists to prevent.
    """
    import os

    previous = None
    try:
        with open(path, encoding="utf-8") as fh:
            previous = fh.read()
    except OSError:
        pass

    rendered = to_yaml(config)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(rendered)

    if previous is None:
        logger.info("router config written for the first time at %s (%d model entries)",
                    path, len(config.get("model_list") or []))
        return
    if previous == rendered:
        return          # steady state — the restart watcher will not fire either

    try:
        before = _model_list_signature(yaml.safe_load(previous) or {})
    except Exception:
        before = None
    after = _model_list_signature(config)
    if before is None:
        logger.info("router config CHANGED — the previous file could not be parsed, "
                    "so the delta cannot be named; LiteLLM will restart (#1955)")
        return
    gained = sorted(after - before)
    lost = sorted(before - after)
    if not gained and not lost:
        # The routing targets are identical and something else moved: a rotated
        # key, an otel toggle, a settings change. Worth saying, because it still
        # costs a restart and is the case nobody expects.
        logger.info("router config CHANGED without any routing difference — same "
                    "%d model/endpoint pairs; a credential or setting moved. "
                    "LiteLLM restarts anyway and in-flight requests are dropped "
                    "(#1955)", len(after))
        return
    logger.info("router config CHANGED: +%d -%d routing targets; LiteLLM will "
                "restart and drop in-flight requests (#1955). gained=%s lost=%s",
                len(gained), len(lost), gained or "[]", lost or "[]")


#: #1955: the reload stamp lives NEXT TO the router config, in the same volume
#: the router entrypoint watches. Its only content is a reason and a timestamp;
#: nothing reads it but the hash in that entrypoint.
RELOAD_STAMP_NAME = "router-reload-stamp"


def reload_stamp_path(config_path: str) -> str:
    import os

    return os.path.join(os.path.dirname(config_path) or ".", RELOAD_STAMP_NAME)


def request_router_restart(reason: str, config_path: str | None = None) -> bool:
    """Ask the router process to restart even though the config is unchanged.

    #1955, measured by DevBox-Vuko on 0.91 (2026-09-11): after a blue-green
    runner switch under load, `granite-docling` — ONE replica — answered 429 for
    the rest of the run and two minutes past the end of it, while the upgrade
    said `state=done`, the deployment said `ready`, and the engine itself
    answered a direct request in 32 ms. The router was holding a LiteLLM
    cooldown in memory; recreating that one container cleared it instantly.

    Why the mechanism we already have cannot cover this. The router entrypoint
    (modules/llm/manager/compose.yml) restarts LiteLLM when the config's CONTENT
    HASH changes — deliberately, so steady-state rewrites of identical bytes
    cause no churn (#321: an unordered SELECT once dropped every stream on the
    box). But for a RELAY-routed worker the endpoint is
    `{relay_internal_base}/relay/{worker_id}/v1` — it names the worker, not the
    engine container — so an engine switch on such a worker can leave the
    rendered bytes identical, and a content-hash watcher cannot see a switch it
    produced no bytes for. The signal has to be explicit.

    Returns True when the stamp was written. A failure is NOT raised: this runs
    at the end of a cutover that has already succeeded, on the same "side
    errand" footing as the config write next to it (`_router_serves`), and a
    read-only /config must not turn a completed switch into an error. It is
    logged, because a router that silently keeps a cooldown is exactly the
    failure this exists to end.
    """
    import os
    import time

    # get_settings() only when we actually need it: an explicit path is the
    # caller telling us where the volume is, and asking the settings anyway
    # would make this fail in places that have no environment to read.
    if config_path is None:
        config_path = get_settings().router_config_path
    path = reload_stamp_path(config_path)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # The timestamp is what makes consecutive stamps differ; the reason is
        # for whoever reads the volume after an incident.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{time.time():.6f} {reason}\n")
        return True
    except OSError:
        logger.warning("router restart request (%s) could not be written to %s "
                       "— the router keeps its in-memory state, including any "
                       "LiteLLM cooldown (#1955)", reason, path, exc_info=True)
        return False


def rebuild_from_session(session) -> dict:
    """`rebuild_from_state`, but on a session the CALLER already holds.

    #266: the runner-upgrade state machine needs the config regenerated from
    state it has flushed but not yet committed. `rebuild_from_state` opens its
    own `session_scope`, i.e. a second connection, which cannot see that state —
    it would write a config describing the fleet as it was BEFORE the relaunch
    and then report the upgrade done against it.
    """
    settings = get_settings()
    config = render_router_config(
        generate_from_db(session),
        valkey_host=settings.valkey_host, valkey_port=settings.valkey_port,
        otel_endpoint=settings.otel_endpoint,
    )
    write_router_config(settings.router_config_path, config)
    return config


def rebuild_from_state() -> dict:
    """Regenerate the router config from DB state and write it to the shared
    volume. Returns the config dict."""
    from app.db import session_scope

    with session_scope() as s:
        return rebuild_from_session(s)


def register_router_admin(app) -> None:
    # #314: LiteLLM router config (rebuild + read) is global router config →
    # the SUPER-ADMIN tier.
    """Admin-gated router-config operations."""
    router = APIRouter(dependencies=[Depends(require_role(Role.SUPERADMIN))])

    @router.post("/api/router/rebuild")
    def rebuild(request: Request):
        config = rebuild_from_state()
        return {
            "written": get_settings().router_config_path,
            "model_list_size": len(config["model_list"]),
        }

    @router.get("/api/router/config")
    def preview(request: Request):
        """Render the config the router WOULD get from current DB state.

        LLMM-14: ``otel_endpoint`` is passed here exactly as ``rebuild_from_state``
        passes it — omitting it rendered ``litellm_settings`` without the
        ``callbacks: ["otel"]`` entry, so with the observability profile on, the
        preview an operator inspects was not the config the router is running.
        LLMM-7: the response is REDACTED — see ``redact_config``."""
        from app.db import session_scope

        settings = get_settings()
        with session_scope() as s:
            deployments = generate_from_db(s)
        return redact_config(render_router_config(
            deployments, valkey_host=settings.valkey_host,
            valkey_port=settings.valkey_port,
            otel_endpoint=settings.otel_endpoint,
        ))

    app.include_router(router)
