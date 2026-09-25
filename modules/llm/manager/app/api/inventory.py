# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Fleet + deployment READ + deploy/undeploy for the management console.

  GET    /api/workers          registered workers + what each serves
  GET    /api/deployments      client-facing deployments + per-worker instances
  POST   /api/deployments      DEPLOY a model (#263 SCH1): create desired-state +
                               place on a worker + enqueue load_engine (#261)
  DELETE /api/deployments/{id} UNDEPLOY: enqueue unload_engine per instance

Admin-gated (require_admin + Caddy source anchor). Deploy/undeploy drive the
node via the #261 command channel (pull) — no manager→node push, so it works
for NAT'd remote workers too.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

import logging

from app import metrics
from app.authz import Role, require_role
from app.api._ids import parse_uuid
from app.api.commands import enqueue_command
from app.api.entitlement import entitlement_allows
from app.catalog import complete_files
from app.config import get_settings
from app.db import session_scope
from app.footprint import footprint_gb
from app.models import Deployment, DeploymentInstance, Model, NodeCommand, Worker

logger = logging.getLogger(__name__)

# #298: a worker that hasn't reported within this window is "stale" — not a
# placement target and shown as stale, not a false "ready".
_WORKER_STALE_SECONDS = float(os.environ.get("LLM_MANAGER_WORKER_STALE_SECONDS", "90"))


def _cache(request: Request):
    """The Valkey cache off ``app.state`` (test-injected fake), else the
    process-wide singleton — same fallback ``keys.py::_cache`` uses."""
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        from app.cache import get_cache

        cache = get_cache()
    return cache


def _entitlement_gate(cache, action: str) -> None:
    """#265 ENT3: extend the subscription entitlement gate from the inference
    hot path (proxy.py, #265 ENT1) to the MUTATING orchestrator paths — deploy
    and catalog-pull/mirror — so a lapsed/expired/absent subscription cannot
    provision NEW capacity, not just cannot run inference.

    Same semantics as ENT1, exactly: default ``entitlement_mode=report`` never
    blocks (backward-compatible); only ``enforce`` gates, and — because
    ``entitlement_allows`` itself fails CLOSED on a cache/DB blip — a blip
    here refuses too, the same fail-closed posture as the hot path.

    LLMM-2: ``cache`` may be ``None`` for call sites that have no ``Request``
    to read ``app.state.cache`` from — notably ``_enqueue_engine``, the shared
    placement path. The process-wide cache is then resolved lazily, and ONLY in
    enforce mode, so the default (report) path still touches nothing.
    """
    settings = get_settings()
    if settings.entitlement_mode != "enforce":
        return
    if cache is None:
        from app.cache import get_cache

        cache = get_cache()
    if entitlement_allows(cache, settings.subscription_number or None):
        return
    metrics.incr_entitlement_rejected()
    logger.warning("entitlement_mode=enforce + not entitled → refusing to %s", action)
    raise HTTPException(status_code=402, detail=(
        f"subscription not entitled (inactive or expired) — cannot {action} "
        f"— contact your razzfazz.ai support contact"))


def _iso(v):
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else None


def _aware(dt):
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _is_fresh(worker, now) -> bool:
    """True iff the worker reported within the staleness window."""
    hb = worker.last_heartbeat
    return hb is not None and (now - _aware(hb)).total_seconds() <= _WORKER_STALE_SECONDS


def _display_status(worker, now) -> str:
    """A 'ready' worker that stopped reporting shows as 'stale', so a dead node
    can't read as available (and placement skips it). #307: external endpoint
    backends (e.g. a Mac/box running Ollama) don't run our worker-agent and don't
    heartbeat — they're never 'stale'."""
    if (worker.labels or {}).get("external"):
        return worker.status
    if worker.status == "ready" and not _is_fresh(worker, now):
        return "stale"
    return worker.status


def _hardware_family(value) -> str:
    """Canonical hardware family for placement + display (#588).

    Labels vary by how a node registered ("cuda" vs "nvidia", "amd" vs
    "amd-gfx1151", …) while the console selector speaks families — an exact
    string compare 409'd deploys where both sides were right. ONE
    normalization, shared by _pick_worker and _runtime_meta, so the two can
    never disagree again. Unknown values normalize to themselves (an exotic
    selector still requires an exact match)."""
    h = (value or "").lower()
    if ("amd" in h) or ("gfx" in h) or ("rocm" in h) or ("vulkan" in h):
        return "amd"
    if ("nvidia" in h) or ("cuda" in h):
        return "nvidia"
    if ("apple" in h) or ("metal" in h) or ("mlx" in h):
        return "apple"
    if h == "cpu" or h.endswith("-cpu") or "cpu" in h.split("-"):
        return "cpu"
    return h


def _placeability(w, now) -> tuple[bool, Optional[str]]:
    """(placeable, reason) for ONE worker — the single source of the placement
    rule, shared by `_assert_placeable` (the 409) and `GET /api/workers` (the
    row). #1264: the deploy library picked "the first `ready` worker" from the
    list while placement excluded external endpoint backends, so on a
    federated box — GPUStack behind the manager as an external backend, the
    operator decision of 2026-09-04 (#979) — the CLI named a target the
    manager would refuse. A list that does not say `placeable` cannot be used
    to choose a placement; now it does, from the same predicate the 409 uses."""
    if (w.labels or {}).get("external"):
        return False, (f"{w.name} is an external backend — it has no worker-agent to "
                       f"command, so a container deployment cannot be placed on it")
    if w.status != "ready" or not _is_fresh(w, now):
        return False, f"{w.name} is not a ready, reachable worker (status={w.status!r})"
    return True, None


def _assert_placeable(w, now) -> None:
    """LLMM-8: the placement preconditions, as a hard assertion on ONE worker.

    The auto-pick branch of ``_pick_worker`` below filters candidates on exactly
    three properties — ``status == "ready"``, a fresh heartbeat, and not an
    external endpoint backend — but the EXPLICIT-``worker_id`` branch used to
    skip every one of them, so ``POST /api/deployments {"worker_id": …}``
    happily placed an engine on a worker the #419 approval gate had not
    admitted, on a worker being drained (#261-C2), or on an external backend
    that runs no worker-agent (the ``load_engine`` command is never claimed and
    the deployment hangs in ``scheduled`` forever).

    Raises 409 with the same wording ``reassign_deployment`` already uses, so
    the two placement entry points can no longer disagree about what a
    placeable worker is."""
    ok, reason = _placeability(w, now)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)


def _pick_worker(s, now, *, worker_id=None, hardware=None, avoid=None, reject=None):
    """Placement: an explicit worker (must exist AND be placeable), else the
    first READY + FRESH worker (optionally hardware-matched), preferring one not
    in ``avoid``.

    ``avoid`` is only a PREFERENCE — when every candidate is in it we still fall
    back to ``fresh[0]`` (replicas may legitimately share a box). ``reject`` (#335)
    is a HARD exclusion, for workers that must never be chosen at all — a worker
    with no VRAM room is not a fallback."""
    if worker_id:
        w = s.get(Worker, parse_uuid(worker_id, "worker_id"))
        if w is None:
            raise HTTPException(status_code=404, detail="worker not found")
        # LLMM-8: an explicit id selects a worker, it does not exempt it from
        # the placement rules the auto-pick branch below applies.
        _assert_placeable(w, now)
        return w
    # #307: never place a container deploy onto an external endpoint backend
    # (it doesn't run our worker-agent — it only serves its pre-registered models).
    fresh = [w for w in s.query(Worker).filter(Worker.status == "ready").order_by(Worker.name).all()
             if _is_fresh(w, now) and not (w.labels or {}).get("external")]
    if hardware:
        # #588: family match, not exact string — a node registered as "cuda"
        # must satisfy the console selector "nvidia" (and "amd-gfx1151" must
        # satisfy "amd"). Exact equality still matches trivially (same family).
        want = _hardware_family(hardware)
        fresh = [w for w in fresh
                 if _hardware_family((w.labels or {}).get("hardware")) == want]
    if reject:
        fresh = [w for w in fresh if str(w.id) not in reject]
    if avoid:
        pref = [w for w in fresh if str(w.id) not in avoid]
        if pref:
            return pref[0]
    return fresh[0] if fresh else None


#: Instance statuses that do NOT occupy a worker: a failed placement is a
#: corpse, not an engine serving traffic. Every other status (scheduled,
#: loading, ready, …) means an engine is, or is about to be, running there —
#: stop/undeploy DELETE the rows outright, so there is no other terminal state
#: to enumerate.
_DEAD_INSTANCE_STATUS = frozenset({"failed"})


def _is_relay_routed_worker(worker) -> bool:
    """#929 M1: does this worker's traffic go back through the master's WS
    relay? Mirrors ``router_config._is_relay_routed`` exactly — not external,
    and carrying a non-empty ``advertise_addr`` — because the two must agree
    about which workers are subject to the relay's single-engine limit. Reads
    ``labels`` defensively so a caller passing a bare worker stand-in (tests,
    and the ``reassign``/``apply-params`` paths' already-loaded rows) can never
    turn this guard into an AttributeError on a placement that was fine."""
    labels = getattr(worker, "labels", None) or {}
    if labels.get("external"):
        return False
    return bool(str(labels.get("advertise_addr") or "").strip())


def _relay_routes_by_model(worker) -> bool:
    """#1535: does this worker's agent pick the engine by the model a relayed
    request NAMES? Declared by the node at registration
    (`relay_model_routing`), never inferred: the master must not assume a
    capability of a box it cannot see, and a rollback to an older agent has to
    take the capability away with it (the label is rewritten every
    registration, deliberately not sticky)."""
    labels = getattr(worker, "labels", None) or {}
    return bool(labels.get("relay_model_routing"))


def _relay_multi_model_conflict(s, dep, worker):
    """#929 M1 / #1535: the instance of ANOTHER deployment already occupying
    this REMOTE worker, or None — and only for a worker that cannot tell its
    engines apart.

    An agent that PREDATES #1535 is single-engine-per-worker by construction,
    and nothing enforced it. ``router_config`` emits ``…/relay/{worker_id}/v1``
    for EVERY deployment on a remote worker, and such a node answers all of
    them from ``runtime._primary_engine_base`` — the FIRST *ready* instance,
    whichever model that happens to be. So a second model deployed onto the
    same remote worker does not fail: it silently serves the FIRST model's
    weights under the second model's name, for both deployments, with whichever
    engine won the race. Wrong answers, no error, nothing in a log.

    #1535 lifted that for an agent that says it can disambiguate: it reads the
    model out of the request body and picks the engine serving it
    (``runtime.relay_engine_base_for``). The refusal therefore survives for
    exactly the nodes that still cannot — which is what ``relay_model_routing``
    is for, since a fleet upgrades one box at a time and the master cannot
    probe a box it only reaches through a WS that box dialled.

    Replicas of the SAME deployment were always fine — same weights, same
    params, so whichever engine answers is the right one.
    """
    if not _is_relay_routed_worker(worker):
        return None
    # #1535: the worker fans out to its own engines by the request's model, so
    # several models on one remote worker is the DESIGNED shape (#262: "the
    # master picks a worker, the worker picks a local engine"), not a hazard.
    # The refusal below survives only for an agent that has not declared it.
    if _relay_routes_by_model(worker):
        return None
    for di in (s.query(DeploymentInstance)
               .filter(DeploymentInstance.worker_id == worker.id).all()):
        if str(getattr(di, "deployment_id", "")) == str(dep.id):
            continue
        if getattr(di, "status", None) in _DEAD_INSTANCE_STATUS:
            continue
        return di
    return None


def _assert_relay_single_engine(s, dep, worker) -> None:
    """#929 M1: 409 rather than place a SECOND model on a remote worker that
    already hosts a different one AND cannot route by model (#1535) — see
    `_relay_multi_model_conflict` for which workers that still is."""
    occupant = _relay_multi_model_conflict(s, dep, worker)
    if occupant is None:
        return
    other = s.get(Deployment, occupant.deployment_id) if occupant.deployment_id else None
    other_name = other.model_name if other is not None else str(occupant.deployment_id)
    raise HTTPException(status_code=409, detail=(
        f"{getattr(worker, 'name', worker.id)} is a REMOTE (relay-routed) worker "
        f"already serving {other_name!r}, and its agent is too old to tell its "
        f"engines apart (it does not declare `relay_model_routing`, #1535 — "
        f"upgrade the node agent and it can serve several models at once). "
        f"Until then every deployment on this worker "
        f"is routed to the same relay endpoint and answered by whichever engine "
        f"is ready first, so a second model there would silently serve the wrong "
        f"weights. Undeploy {other_name!r} first, or place {dep.model_name!r} on "
        f"another worker."))


def weights_gate_applies(dep, files, *, weights_already_proven: bool = False) -> bool:
    """Does the #1372/#1494 weights check have anything to say about this
    placement? Two exemptions, both from the #1494 re-review, and both about
    the same thing: the check guards NEW residency for a row the manager has
    artifact knowledge about.

    * ``weights_already_proven`` — the caller knows this engine was RUNNING a
      moment ago (the runner upgrade captures it before the drain). It found
      its weights; the stored column is stale bookkeeping, not evidence. Gating
      it turns a maintenance operation into an outage: the worker stays
      drained, the model stays down, and the trigger is the upgrade rather than
      an operator's deploy.
    * the manager knows NOTHING about the artifacts — no ``source_files``, no
      ``hf_repo``. A deployment DISCOVERED from a node's registration report is
      exactly that shape (``app/api/workers.py`` creates it so; a node report
      has no files field at all), and it is by definition already serving.
      Refusing a scale-up on one helps nobody: the manager cannot name a file
      it does not have, and the caller has no request field to fix.

    In both cases the node keeps the last word: if the weights really are
    missing it refuses the load, the row goes ``failed`` with the #1376 reason,
    and the reconciler can act. That self-healing path is strictly better than
    refusing the operation outright, which is what this used to do.

    One predicate, used by ``_enqueue_engine`` and by the runner upgrade's
    all-or-nothing pre-check — two copies would answer differently the first
    time one of them learns something.
    """
    if weights_already_proven:
        return False
    if not (files or []) and not getattr(dep, "hf_repo", None):
        return False
    return True


def _enqueue_engine(s, dep, worker, served, files, hf_repo, params, task,
                    *, weights_already_proven: bool = False) -> str:
    # NB: the runner pin travels via dep.runner_image (persisted), not a
    # parameter — every caller already has the dep, and a parameter would let a
    # call site forget it, silently launching the default runner. #549 R1.
    """Place one engine instance: enqueue a load_engine command + create the
    optimistic 'scheduled' instance (#294b). Shared by deploy + the replica
    scheduler. Returns the instance_id.

    LLMM-2: the #265 ENT3 entitlement gate lives HERE, not only on ``deploy``
    and ``mirror_into_cache``. Those two were the only gated routes, so five
    other paths provisioned engine capacity ungated — ``start`` (resume),
    ``PATCH`` replicas scale-up, ``reassign``, ``apply-params`` and the
    reconciler's reschedule — every one of which launches a NEW engine on a
    worker, which is exactly what ENT3 exists to stop on a lapsed subscription.
    Putting it on the one function they all funnel through is what makes "every
    capacity route" true by construction instead of by five copies that a sixth
    route can forget. The generic ``POST /api/workers/{id}/commands`` with
    ``kind=load_engine`` is the one capacity path that does NOT come through
    here, and is gated at its own route (``app/api/commands.py``).

    Note the asymmetry, and it is deliberate: only PROVISIONING is gated.
    ``unload_engine``/``evict_weights``/stop/undeploy stay ungated — a lapsed
    box must always be able to FREE capacity.
    """
    _entitlement_gate(None, f"provision engine capacity for {dep.model_name}")
    # #929 M1: same "one funnel, not six call sites" argument as the gate above
    # — deploy, start, scale-up, reassign, apply-params and the #263 reconciler
    # all place engines through here, and every one of them could otherwise put
    # a second model on a remote worker whose relay can only serve one.
    _assert_relay_single_engine(s, dep, worker)
    # #1494 (review of #1372): and the same argument for the weights check. The
    # door (`POST /api/deployments`) validates the REQUEST; these five paths use
    # the PERSISTED dep.source_files, so an old row with `files: []` — a box
    # deployed before the door existed — could still send a llama.cpp load the
    # node can only reject. Checking the files that actually travel makes "every
    # capacity route" true by construction.
    # #1494 review, finding 5: the remedy named here must be one the CALLER
    # has. A PATCH/scale-up/reassign carries no `files[]` — it re-uses the
    # deployment's stored source_files — so pointing its operator at a request
    # field they cannot set sends them looking for something that is not there.
    # #1494 re-review, finding (b): the check gates NEW residency — deploy,
    # scale-up, resume, reassign, the reconciler's reschedule. It must NOT gate
    # restarting an instance that is ALREADY RUNNING under a new runner image:
    # such an engine has demonstrably found its weights, whatever the stored
    # `source_files` column says about them, and a pre-door row with `files: []`
    # would otherwise turn a maintenance operation into an outage — the worker
    # stays drained, the model stays down, and the trigger is not an operator's
    # deploy but the runner upgrade itself. Before this PR that case was ugly
    # but self-healing (the node refused the load, the row went `failed`, the
    # reconciler could re-place); breaking it one level higher is worse.
    # #1518 (E5), and the #1529 review asked exactly this: does relaunching an
    # EXISTING vllm deployment go through `_resolve_dep_engine`? It does not —
    # `start` (resume), `reassign`, `apply-params`, the PATCH scale-up and the
    # reconciler all read the PERSISTED `dep.engine` and come straight here.
    # Without this the row would be enqueued with `engine=vllm`, the node would
    # try to pull an image that no longer exists, and the operator would read a
    # pull failure instead of the reason. Refused here, in the one function
    # every relaunch path funnels through — the same argument #1494 made for the
    # weights check.
    if canonical_engine(getattr(dep, "engine", "") or "") == "vllm":
        raise HTTPException(status_code=422, detail=(
            f"deployment {dep.model_name} is recorded with engine=vllm, which the "
            "fleet removed in 2026.09 — there is no vLLM driver and no image to "
            "launch. Deploy a GGUF build of this model on llama.cpp instead; the "
            "CUDA runner is picked from the GPU's compute capability."))
    if weights_gate_applies(dep, files, weights_already_proven=weights_already_proven):
        _require_llamacpp_weights(
            dep.engine, files,
            where=(f"deployment {dep.id}'s stored source_files "
                   "(re-deploy the model to change them)"))
    iid = f"engine-{dep.model_name}-{uuid.uuid4().hex[:6]}".replace("/", "-").replace(".", "")
    enqueue_command(s, worker.id, "load_engine",
                    {"instance_id": iid, "model": served, "files": files or [],
                     "params": params or {}, "task": task or "chat", "hf_repo": hf_repo,
                     "runner_image": dep.runner_image,
                     # The engine this deployment runs. #1518 left llama.cpp as
                     # the only one, so this no longer selects between drivers;
                     # it is still sent because dep.engine is persisted
                     # desired-state and an older node reads it.
                     "engine": dep.engine,
                     # #307 S2.5: files already mirrored into the in-stack Zot
                     # registry, by digest — [] when the model isn't cached yet
                     # (the worker falls back to HF, unchanged).
                     "registry_files": _zot_repo_files(hf_repo, served)})
    s.add(DeploymentInstance(deployment_id=dep.id, worker_id=worker.id, instance_id=iid,
                             endpoint=f"http://{iid}:8080/v1", status="scheduled",
                             detail="scheduled — awaiting worker", started_at=datetime.now(timezone.utc)))
    s.flush()
    return iid


def _runtime_meta(engine, hardware) -> tuple[str, str]:
    """(device, arch) for display + grouping in the console.

    device ∈ {GPU, CPU, —}; arch is the runtime family the operator recognizes
    (Vulkan / CUDA / Ollama / CPU / vLLM). Derived from the engine kind + the
    worker's hardware class — the two facts we already track — so the console
    can show "GPU · Vulkan" vs "GPU · Ollama" vs "CPU · CPU" and group by it.
    """
    e = (engine or "").lower()
    # #588: the family logic lives in _hardware_family — the one source both
    # placement and display share.
    fam = _hardware_family(hardware)
    is_amd = fam == "amd"
    is_nv = fam == "nvidia"
    is_apple = fam == "apple"
    is_cpu = fam == "cpu"
    if e == "ollama":
        return ("GPU", "Ollama")            # Ollama picks its own device (Metal/CUDA/CPU)
    if e == "vllm":
        return ("GPU", "vLLM")
    if canonical_engine(e) == _CANONICAL_LLAMACPP:   # #1494: one normalisation
        if is_amd:
            return ("GPU", "Vulkan")
        if is_nv:
            return ("GPU", "CUDA")
        if is_apple:
            return ("GPU", "Metal")
        if is_cpu:
            return ("CPU", "CPU")
        return ("GPU", "llama.cpp")
    if is_amd:
        return ("GPU", "Vulkan")
    if is_nv:
        return ("GPU", "CUDA")
    if is_apple:
        return ("GPU", "Metal")
    if is_cpu:
        return ("CPU", "CPU")
    return ("—", engine or "—")


def _infer_task(model_name: str) -> str:
    """#318 best-effort serve-task from a model name, for external backends that
    don't tell us. 'embed' anywhere → embedding; 'rerank'/'reranker' → rerank;
    else chat. Operators can override per-model on registration."""
    n = (model_name or "").lower()
    if "rerank" in n:
        return "rerank"
    if "embed" in n or n.endswith("-emb") or "bge" in n or "nomic" in n or "e5" in n:
        return "embed"
    return "chat"


def _headroom_from_env(raw, default=0.9) -> float:
    """#319 Admission headroom fraction, tolerant of the ways an env var arrives
    wrong. `os.environ.get(K, "0.9")` returns "" for a variable that EXISTS but is
    empty — which is what `VAR: "${VAR:-}"` in a compose file produces — so the
    naive form raised ValueError at IMPORT and the manager never started. A
    mis-set tuning knob must not be able to do that; it falls back and the box
    runs with the documented default."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return value if 0 < value <= 1 else default


_VRAM_HEADROOM = _headroom_from_env(os.environ.get("LLM_MANAGER_VRAM_HEADROOM"))


def _reserved_external_gb(worker=None) -> float:
    """#330 stage 1: GB an EXTERNAL scheduler (GPUStack) holds on the same
    GPU. The two control planes book memory independently — until a live
    dynamic reading exists (stage 2, gated on the 0.91 coexistence
    measurements), the operator states the coexistence cost explicitly
    and the admission gate honours it. 0 (default) = no coexistence.
    Deliberately static + honest: no fake feedback loop from a value the
    gfx1151 UMA path may not even expose.

    #1442 review (blocker 2): the share is BOX-LOCAL. The external scheduler
    runs next to the manager, on the manager's own GPU — a REMOTE worker has
    its own hardware that GPUStack never sees. Subtracting the coexistence cost
    from every worker made a federated box refuse placements FLEET-wide:
    measured with this module, a remote worker with 96 GB went from a budget of
    86.4 to 0.0, and 0.0 is a KNOWN zero, which
    ``test_reserved_at_total_refuses_instead_of_going_inert`` pins as
    refuse-everything. So the reservation applies to the co-located workers
    only; ``_is_relay_routed_worker`` is the same distinction the router uses
    for remote traffic (#929 M1).

    Called without a worker (a caller that has no row at hand) it returns the
    configured value unchanged — the conservative answer.
    """
    if worker is not None and _is_relay_routed_worker(worker):
        return 0.0
    try:
        v = float(os.environ.get("LLM_MANAGER_VRAM_RESERVED_EXTERNAL_GB", "0"))
        return max(0.0, v)
    except (TypeError, ValueError):
        return 0.0


# #328 the capacity labels admission reads, in precedence order, with the name
# each one gives the resulting basis. Prefer the GPU VRAM carveout — the honest
# ceiling on unified-memory boxes (Strix Halo), where GPU allocations beyond it
# spill into host RAM via GTT and thrash the host — else host RAM.
_CAPACITY_LABELS = (("vram_total_gb", "vram"), ("mem_total_gb", "ram"))

#: #330 stage 2: how old (seconds) a reported vram_used_gb may be and still
#: steer admission. Older = the node stopped reporting = UNKNOWN, and unknown
#: falls back to the static stage-1 math — never to "whatever it last said".
_VRAM_USED_MAX_AGE_S = float(os.environ.get("LLM_MANAGER_VRAM_USED_MAX_AGE_S", "120"))


def _fresh_vram_used_gb(worker):
    """The worker's live VRAM usage (GB), or None unless it is fresh.

    The stamp is written with SERVER time at registration (api/workers.py), so
    this comparison never involves a node clock. Negative/garbage → None."""
    labels = worker.labels or {}
    used, at = labels.get("vram_used_gb"), labels.get("vram_used_at")
    if used is None or at is None:
        return None
    try:
        if (time.time() - float(at)) > _VRAM_USED_MAX_AGE_S:
            return None
        u = float(used)
        return u if u >= 0 else None
    except (TypeError, ValueError):
        return None


def _fresh_host_used_gb(worker):
    """The worker's live HOST memory usage (GB), or None unless it is fresh.

    #1947. The dashboard snapshot carries `mem_used_gb` with its own server-side
    stamp (`metrics_at`), written on the same registration as the VRAM figure.
    Reading it here gives the overlap check a SECOND, independently-based
    measurement to test its first one against.

    Why that is needed: on a unified-memory box the VRAM *carveout* and the
    operator's memory *budget* are different quantities, and nothing forces the
    node to report them on the same basis. Measured on box-175r (0.175,
    2026-09-11, DevBox-Vuko): `vram_total_gb` is 96.0 — the operator budget from
    `LLM_WORKER_MEM_BUDGET_GB` — while `vram_used_gb` is 2.0, the whole VRAM
    carveout the BIOS grants that platform, and it CANNOT rise above it because
    every model weight lives in GTT. Host memory told the truth the whole time:
    121.2 total, 65.5 used.
    """
    labels = worker.labels or {}
    metrics, at = labels.get("metrics"), labels.get("metrics_at")
    if not isinstance(metrics, dict) or at is None:
        return None
    used = metrics.get("mem_used_gb")
    if used is None:
        return None
    try:
        if (time.time() - float(at)) > _VRAM_USED_MAX_AGE_S:
            return None
        u = float(used)
        return u if u >= 0 else None
    except (TypeError, ValueError):
        return None


def _capacity_source(worker):
    """#328 (basis, total_gb) for a worker: which label admission is derived from
    and its raw value. ``("none", None)`` when neither is usable.

    ``_worker_budget_gb`` and the API's reported basis BOTH come from here, so the
    console can never say "vram" while the gate is quietly using host RAM — a
    divergence that would make an inert gate look like a working one.
    """
    labels = worker.labels or {}
    # #2141 (Jira RZFZAI-1950): a CPU worker is budgeted against host RAM, never
    # against a VRAM label. Older node-agents reported the HOST's amdgpu figures
    # under hardware=cpu — an iGPU's 0.5 GB carve-out with 2.2 GB "used" (QA's
    # seqitux-002: every deploy refused at "0.0 GB free"), the Strix UMA's
    # 103.1 GB on 0.91 (everything admitted). The agent no longer reports them
    # on a CPU worker; this is the manager's own half, so a mixed fleet with an
    # older agent gets the right basis too.
    keys = _CAPACITY_LABELS
    if _hardware_family(labels.get("hardware")) == "cpu":
        keys = tuple(kb for kb in _CAPACITY_LABELS if kb[1] == "ram")
    for key, basis in keys:
        v = labels.get(key)
        try:
            if v and float(v) > 0:
                return basis, float(v)
        except (TypeError, ValueError):
            pass
    return "none", None


def _worker_budget_gb(worker):
    """#227 usable placement budget (GB), or None when unknown → admission is NOT
    enforced (never block on a guess). A "none" basis therefore means admission
    control is INERT for this worker, which is why #328 makes it visible."""
    _basis, total = _capacity_source(worker)
    if total is None:
        return None
    # #330 stage 1: subtract the operator-declared external-scheduler
    # share BEFORE the headroom factor; never below 0.
    return max(0.0, (total - _reserved_external_gb(worker))) * _VRAM_HEADROOM


#: The one wording for "this deployment can never be placed as it stands".
#: `POST /api/deployments/{id}/start` has raised it as a 409 since #312; the
#: list endpoint answered `last_error: None` for the same row, so the reason
#: existed only for whoever happened to press start.
NO_WEIGHT_SOURCE_REASON = ("no weight source recorded — redeploy from the "
                           "catalog/HF instead")


#: The same sentence for a row that came from a node report. It is a DIFFERENT
#: conversation with the operator than the generic one: nobody deployed this
#: without a source — the manager created it because a node was already serving
#: the model, and that path has no weight source to record (#1760). Re-running
#: anything cannot help; the model has to be deployed from the catalog once.
DISCOVERED_NO_SOURCE_REASON = (
    "no weight source recorded — this deployment was DISCOVERED from a node's "
    "registration report (it was already being served) and never had one. It "
    "cannot be started again; deploy the model from the catalog/HF instead")


def unplaceable_reason(dep) -> str | None:
    """Why this deployment cannot be placed AS IT STANDS, or None (#1760).

    Not "why did the last attempt fail" — that is `_last_error_for`, and it
    reads an instance row. This answers the case where there is no instance row
    to read because none was ever created, which on a manager box is the shape
    that costs the most: measured on 0.79, three of four deployments sat
    `pending` with zero instances and an EMPTY `last_error`, `llm:8080/v1/models`
    served only the chat model, and every OWUI knowledge-base probe died on
    `POST /v1/embeddings -> 400 Invalid model name`. Nothing anywhere said why.

    One predicate, two callers: the 409 in `start_deployment` and the list
    endpoint. Two copies would drift the first time the rule learns something,
    and then the console and the API would disagree about the same row.
    """
    if not (getattr(dep, "source_files", None) or getattr(dep, "hf_repo", None)):
        # #1760: say WHICH of the two shapes this is. Both are unplaceable, but
        # only one of them is anybody's mistake.
        tags = getattr(dep, "tags", None) or []
        if "discovered-from-node" in tags:
            return DISCOVERED_NO_SOURCE_REASON
        return NO_WEIGHT_SOURCE_REASON
    return None


def _last_error_for(inst_list, health, dep=None) -> str | None:
    """#1372: the node's reason for the console — the detail of a `failed`
    instance row. None once the deployment is `ready`: a failed row can
    outlive a rescue onto another worker (the reconciler re-places, the old
    row on the rejecting worker is never reported again), and a ready
    deployment showing a stale error reads wrong (review rev-C).

    #1760: when there is NO instance row to read, fall back to why the
    deployment cannot be placed at all. A `failed` row always wins — it is the
    node's own account of a real attempt, and it is more specific than a
    precondition. The fallback only speaks where the field was empty before.
    """
    if health == "ready":
        return None
    detail = next((i.get("detail") for i in inst_list if i.get("status") == "failed"), None)
    if detail:
        return detail
    return unplaceable_reason(dep) if dep is not None else None


def _committed_gb(s, worker_id, exclude_dep_id=None) -> float:
    """Footprint (GB) already committed to a worker: sum est_gb over deployments
    that have a live (non-stopped) instance on it, times their replica count."""
    q = (s.query(Deployment)
         .join(DeploymentInstance, DeploymentInstance.deployment_id == Deployment.id)
         .filter(DeploymentInstance.worker_id == worker_id,
                 # #1372 rev-C: a `failed` row is kept for the console (the
                 # node's reason) but holds no VRAM — an engine that never
                 # loaded must not block the next placement on this worker.
                 DeploymentInstance.status.notin_(_DEAD_INSTANCE_STATUS),
                 Deployment.status != "stopped",
                 Deployment.est_gb.isnot(None)))
    if exclude_dep_id is not None:
        q = q.filter(Deployment.id != exclude_dep_id)
    total = 0.0
    for dep in q.distinct().all():
        total += float(dep.est_gb or 0) * max(1, dep.replicas or 1)
    return total


def _unknown_commitments(s, worker_id, exclude_dep_id=None) -> int:
    """How many live deployments on this worker have NO footprint estimate.

    #1422: `_committed_gb` filters `est_gb IS NOT NULL`, so these count as zero.
    Measured on the fleet: ALL SEVENTEEN deployments carried `est_gb = None` —
    every row the CLI deploy path ever wrote, because only the console sends the
    estimate. The admission math was therefore adding up nothing and comparing
    it against a budget, and the reconciler put eleven engines on one master
    before the box fell over (load average 187).
    """
    q = (s.query(Deployment)
         .join(DeploymentInstance, DeploymentInstance.deployment_id == Deployment.id)
         .filter(DeploymentInstance.worker_id == worker_id,
                 DeploymentInstance.status.notin_(_DEAD_INSTANCE_STATUS),
                 Deployment.status != "stopped",
                 Deployment.est_gb.is_(None)))
    if exclude_dep_id is not None:
        q = q.filter(Deployment.id != exclude_dep_id)
    return q.distinct().count()


def _placement_has_settled(worker) -> bool:
    """Has the last unaccounted placement on this worker shown up in its VRAM?

    #1422, the part no better estimate fixes on its own: `vram_used` rises only
    once the engine has actually loaded, which is seconds to minutes after the
    placement. `LLM_MANAGER_RECONCILE_MAX_PER_PASS` caps placements per PASS,
    not across passes — so the reconciler kept placing into a reading that had
    not moved yet, pass after pass, and the dynamic leg agreed every time.

    True when there is nothing outstanding, or when the freshness stamp is newer
    than the placement it has to account for. Unknown (no reading at all) counts
    as settled: this must not become a gate that blocks a fleet with no sysfs.
    """
    labels = worker.labels or {}
    placed_at = labels.get("unaccounted_placed_at")
    if placed_at is None:
        return True
    at = labels.get("vram_used_at")
    if at is None:
        return True
    try:
        return float(at) > float(placed_at)
    except (TypeError, ValueError):
        return True


def _mark_unaccounted_placement(worker) -> None:
    """Record that something with an UNKNOWN footprint just went onto this
    worker, so the next such placement waits for the reading to catch up."""
    labels = dict(worker.labels or {})
    labels["unaccounted_placed_at"] = time.time()
    worker.labels = labels


def _budget_check(s, worker, est_gb, *, exclude_dep_id=None, pending_gb=0.0):
    """#227/#335 admission math for placing ONE replica on ``worker``.

    Returns ``(fits, committed, projected, budget)``. ``fits`` is True whenever the
    footprint estimate or the worker budget is unknown — admission is never enforced
    on a guess, which is #227's original rule — and the three numbers are then None.

    ``pending_gb`` is footprint this request has already decided to put on this
    worker but has not yet committed to the DB. The replica loops accumulate it so
    iteration N sees iterations 1..N-1; without it every iteration re-reads the same
    committed total and places the whole fleet into the same free GB.

    Callers pass ``exclude_dep_id=dep.id`` and re-seed the deployment's own live
    instances through ``pending_gb`` instead, because ``_committed_gb`` multiplies by
    ``dep.replicas`` — a value the scale-up path has ALREADY raised, so counting it
    normally would charge the new replicas twice.
    """
    budget = _worker_budget_gb(worker)
    # #1422: an UNKNOWN footprint used to be an unconditional pass — the rule
    # was "admission is never enforced on a guess" (#227), and it is still
    # right that we do not invent a number. But "we cannot measure it" is not
    # the same as "it is free": with every deployment carrying est_gb = None,
    # the gate admitted without limit and the reconciler stacked eleven engines
    # onto one worker until the box went down.
    #
    # So the one thing that CAN be said without an estimate is said: a
    # placement whose size nobody knows has to become visible in the worker's
    # VRAM reading before the next one is allowed. That is not a budget, it is
    # a queue of one — and it is exactly the hole the measurement found, where
    # the per-pass cap did not stop placement across passes.
    if not _placement_has_settled(worker):
        return (False, None, None, None,
                {"settling": True,
                 "unknown": _unknown_commitments(s, worker.id, exclude_dep_id=exclude_dep_id)})
    # review #694: `is None`, not falsiness — 0.0 is a KNOWN budget of zero
    # (operator reserved the whole GPU for the external scheduler: intention
    # refuse-everything). Falsiness silently turned the gate INERT at exactly
    # that setting — the #328 inert-gate trap while the console shows 0.0.
    if not est_gb or budget is None:
        return True, None, None, None, None
    committed = _committed_gb(s, worker.id, exclude_dep_id=exclude_dep_id) + float(pending_gb)
    projected = committed + float(est_gb)
    fits = projected <= budget
    # #330 stage 2 — the DYNAMIC leg, grounded in the 0.91 measurement:
    # sysfs vram_used aggregates BOTH control planes and tracks load/unload
    # exactly, so with a FRESH reading the real question is "does this replica
    # fit into what is free RIGHT NOW". Notes on the math:
    #   * NO _reserved_external_gb() here — an external scheduler's resident
    #     models are already inside `used`; subtracting the static share too
    #     would double-count it. The static leg above keeps the reservation
    #     (it models the external scheduler's MAX footprint while our own
    #     committed-math cannot see it).
    #   * pending_gb stays: replicas admitted this request have not started,
    #     so `used` cannot see them yet.
    #   * The static committed-leg stays as the second net — it catches the
    #     admission race where several requests pass before `used` rises.
    #   * Only meaningful on a vram basis; a mem_total_gb (RAM) basis has no
    #     used-counterpart, and stale/missing readings fall back to static.
    dyn = None
    basis, total = _capacity_source(worker)
    if basis == "vram" and total is not None:
        used = _fresh_vram_used_gb(worker)
        if used is not None:
            free_now = max(0.0, (total - used)) * _VRAM_HEADROOM
            dyn = {"used": used, "free_now": free_now}
            if float(est_gb) + float(pending_gb) > free_now:
                fits = False
                dyn["refused"] = True
    return fits, committed, projected, budget, dyn


def _budget_noun(worker) -> str:
    """What the budget IS on this worker, for the operator's 409 text (#2141):
    "VRAM" on a GPU basis, "host RAM" on a CPU worker / RAM basis."""
    basis, _ = _capacity_source(worker)
    return "host RAM" if basis == "ram" else "VRAM"


def _over_subscription_detail(worker, est_gb, committed, projected, budget, remedy):
    """The 409 body for a refused placement — one wording for all four paths.
    #2141: the noun follows the basis — a CPU worker's refusal must not tell the
    operator to "free VRAM" on a box that has none."""
    noun = _budget_noun(worker)
    what = "VRAM carveout − headroom" if noun == "VRAM" else "host RAM − headroom"
    return (f"would over-subscribe {worker.name}: {float(est_gb):.1f} GB requested "
            f"+ {committed:.1f} GB already committed = {projected:.1f} GB > {budget:.1f} GB usable "
            f"({what}). {remedy.replace('free VRAM', f'free {noun}')}")


def _place_within_budget(s, now, *, dep, est_gb, hardware, avoid, pending, force=False,
                         worker_id=None):
    """#335 pick a READY worker that ALSO has VRAM room for one more replica.

    Returns ``(worker, reason)``. ``reason`` is None on success, ``"no_worker"`` when
    nothing ready matched the selector at all, and ``"no_room"`` when workers DID
    match but every one of them would be over-subscribed — the caller reports which,
    because "no GPU" and "no free VRAM" need different operator actions.

    #1542: ``worker_id`` names the target explicitly. It SELECTS a worker, it does
    not exempt it — `_pick_worker` still runs `_assert_placeable`, and the relay
    and VRAM checks below still apply. And it never falls back: if the named
    worker cannot take the replica the answer is that reason, not a different
    box. An operator who names a target on a heterogeneous fleet (#1542: prefill
    is 1.8x faster on one of ours, rerank 1.35x on another) means that box; a
    silent substitution would put the load exactly where they said not to.
    """
    reject: set[str] = set()
    while True:
        w = _pick_worker(s, now, worker_id=worker_id, hardware=hardware,
                         avoid=avoid, reject=reject)
        if w is None:
            return None, ("no_room" if reject else "no_worker")
        # #1422: a REMOTE (relay-routed) worker that already serves ANOTHER
        # model cannot take this one — unless its agent routes by model (#1535);
        # a node that predates that is single-engine per worker (#929 M1), and
        # `_relay_multi_model_conflict` is the one place that distinction lives.
        # Before this check the pick returned such a worker (they
        # sort by name, and `box-175b` < `master`), `_enqueue_engine` raised
        # the 409, and the reconciler retried the SAME worker every pass: a
        # lost deployment stayed lost while a free worker sat next to it. A
        # relay conflict is a hard reject, exactly like no VRAM room.
        if _relay_multi_model_conflict(s, dep, w) is not None:
            if worker_id:
                return None, "relay_conflict"
            reject.add(str(w.id))
            continue
        if force:
            return w, None
        fits, _c, _p, _b, _dyn = _budget_check(s, w, est_gb, exclude_dep_id=dep.id,
                                               pending_gb=pending.get(str(w.id), 0.0))
        if fits:
            # #1422: only an UNACCOUNTED placement starts the wait. A known
            # footprint is carried by `pending_gb` within this request and by
            # `_committed_gb` across requests, so it needs no queue of one —
            # and making it wait would serialise a legitimate replica loop.
            if not est_gb:
                _mark_unaccounted_placement(w)
            return w, None
        # #1542: with an explicit target there is nothing to fall back TO —
        # rejecting and looping would hand back the same worker forever.
        if worker_id:
            return None, "no_room"
        reject.add(str(w.id))


def _deployment_health(inst_list: list, ready: int, replicas: int) -> str:
    """#286 ACTUAL health from instance phases (vs the desired-state column).
    ready → all replicas serving · degraded → some · failed → engines died ·
    loading → coming up · pending → nothing placed."""
    if not inst_list:
        return "pending"
    statuses = [i.get("status") for i in inst_list]
    if ready >= max(1, replicas):
        return "ready"
    if ready > 0:
        return "degraded"
    if any(s == "failed" for s in statuses):
        return "failed"
    # surface the SPECIFIC transitional phase so the header mirrors the instance
    # (a "restarting" instance shouldn't read as "loading" up top).
    if any(s == "restarting" for s in statuses):
        return "restarting"
    if any(s == "pulling" for s in statuses):
        return "pulling"
    if any(s in ("scheduled", "loading", "pending", "starting") for s in statuses):
        return "loading"
    return "pending"


def _container(endpoint: Optional[str]) -> Optional[str]:
    """The engine container name = the host of its in-cluster endpoint
    (http://engine-x:8080/v1 → engine-x). Used as the target for restart/logs."""
    if not endpoint:
        return None
    try:
        return urlparse(endpoint).hostname
    except Exception:
        return None


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9._-]+", "-", (s or "").lower()).strip("-") or "model"


def _norm_tags(raw) -> list[str]:
    """Trim, drop blanks, de-dupe (case-insensitive, first spelling wins)."""
    out: list[str] = []
    seen: set[str] = set()
    for t in raw or []:
        s = str(t).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


# #549 R1: a conservative OCI image reference — [host[:port]/]name[:tag][@sha256:…].
# The value ends up in docker-py's containers.run(image=…) on a node, so this is
# defence in depth, not the security boundary (the channel is already node-key
# authed) — but a typo'd ref should fail HERE with a 422 naming the field, not on
# the node twenty seconds later as a pull error inside a command result.
#
# The node validates with the IDENTICAL pattern (drivers/images.py). The two
# services cannot share a module, so agreement is test-enforced, same as the
# command kinds (#547): tests/unit/llm-manager/test_runner_image.py fails if the
# two pattern strings diverge.
RUNNER_IMAGE_RE = re.compile(
    r"^(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?/)?"      # registry host[:port]/
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"                        # first path component
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"                  # further components
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?"              # :tag
    r"(?:@sha256:[a-f0-9]{64})?$"                          # @digest
)


def valid_runner_image(ref) -> bool:
    return isinstance(ref, str) and bool(RUNNER_IMAGE_RE.match(ref))


def registry_qualified_runner_image(ref) -> bool:
    """Well-formed AND registry-qualified (``host[:port]/path[:tag]``) — the
    #549 R2 rule ``deploy_runner`` (app/api/commands.py) applies to explicit
    pulls and, since #1187, the rule ``PATCH /api/deployments`` applies to a
    per-deployment runner pin.

    The node only launches an explicitly pinned runner from ITS allowed
    registry (``ref_from_allowed_registry`` in the node-agent) and refuses a
    bare name like ``llama-vulkan-runner:b9851`` outright — a bare ref would
    otherwise make docker default to docker.io, which an air-gapped box must
    never reach (#307). The first path component is a registry exactly when
    it contains ``.`` or ``:`` — docker's own rule. The manager cannot know
    each node's registry host, so it enforces the SHAPE here (fast 422 naming
    the field); the node stays the security boundary.
    """
    if not valid_runner_image(ref):
        return False
    first, sep, _rest = str(ref).partition("/")
    return bool(sep) and ("." in first or ":" in first)


def require_launchable_runner_image(ref) -> None:
    """The ONE runner-image gate for both `POST /api/deployments` and
    `PATCH /api/deployments/{id}` — LLMM-5: literally the same function, so
    the two routes cannot drift apart again.

    #1214: POST checked only the SHAPE (`valid_runner_image`) while PATCH also
    demanded a registry-qualified ref. A bare `llama-vulkan-runner:b9851`
    therefore passed the deploy edge, was persisted as the pin, and the node
    refused it at `load_engine` ("not from allowed registry", #549 R2) — the
    deployment sat `scheduled` forever with the error one hop away from the
    operator. Both refusals now happen here, at the edge, naming the field.
    """
    if not valid_runner_image(ref):
        raise HTTPException(status_code=422, detail=(
            f"runner_image {ref!r} is not a valid image "
            f"reference ([host[:port]/]name[:tag][@sha256:…], lowercase name)"))
    if not registry_qualified_runner_image(ref):
        raise HTTPException(status_code=422, detail=(
            f"runner_image {ref!r} is not registry-qualified — "
            f"the node only launches a pinned runner from its allowed registry "
            f"(host[:port]/path:tag, e.g. llm-registry:5000/runners/"
            f"llama-vulkan:b9851), never a bare name (#549 R2)"))


def _zot_repo_ref(hf_repo: str, name: Optional[str] = None, tag: Optional[str] = None) -> tuple[str, str]:
    """The Zot repo:tag a cached model resolves to — the SAME slugging
    ``mirror_into_cache`` already uses (#289/#307), factored out so the
    auto-mirror predicate (#307 S1) and the manual mirror route agree on the
    identical reference for the identical model."""
    repo = "models/" + _slug(name or hf_repo.split("/")[-1])
    return repo, _slug(tag or "latest")


def _zot_has_repo(base: str, repo: str) -> bool:
    """Is ``repo`` already a repository in the in-stack Zot registry? The SAME
    ``GET /v2/_catalog`` read ``registry.py`` uses (#289) — one Zot-catalog
    client/pattern, not a second one. Unreachable / any error -> False: the
    safe default costs one redundant (idempotent) mirror, never a silently
    skipped one."""
    try:
        import httpx
        # in-network (Zot) — #1409
        with httpx.Client(timeout=4, trust_env=False) as c:
            r = c.get(f"{base}/v2/_catalog")
            r.raise_for_status()
            repos = (r.json() or {}).get("repositories") or []
            return repo in repos
    except Exception:
        return False


#: #828: the tag the WORKER-AGENT's ``_auto_cache_deploy`` publishes a deployed
#: model's weights under (``_zot_push(..., _zot_repo(payload.model),
#: "deployed")``, ``modules/llm/node-agent/app/__init__.py``). The manager and
#: the worker-agent cannot share a module (same constraint ``RUNNER_IMAGE_RE``
#: and ``_network_allows_hf`` already live with), so the constant is mirrored
#: here and pinned against the worker-agent source by
#: ``test_307_s25_manager_registry_files.py``.
AUTO_CACHE_TAG = "deployed"


def _zot_repo_files(hf_repo: Optional[str], served: str) -> list[dict]:
    """#307 S2.5: the file->digest map (``[{"name","digest"}]``) ``load_engine``'s
    ``registry_files`` field needs — the exact shape ``puller.pull_artifact``
    (worker-agent, #307 S2) consumes, matched on basename + digest.

    Same repo:tag resolution ``_auto_mirror_if_absent`` uses (``_zot_repo_ref``)
    and the SAME manifest-read shape ``registry.py``'s inventory walk already
    reads (``layers`` + the ``org.opencontainers.image.title`` annotation) —
    one Zot client/pattern, not a second one. ``[]`` when the repo isn't in
    the registry yet (the worker falls back to HF, unchanged) or the registry
    is unreachable — never raises, same unknown-safe convention as
    ``_zot_has_repo``.

    #828 — BOTH tags the fleet actually writes, in preference order. Two
    independent paths mirror a model into Zot under DIFFERENT tags:
    the manager's S1 auto-mirror under ``_zot_repo_ref``'s tag (``latest``),
    and the worker-agent's ``_auto_cache_deploy`` under ``AUTO_CACHE_TAG``
    (``deployed``) — best-effort, background, on EVERY deploy, needing no HF
    reach. Reading only the first left the LAN pull dormant for every model
    cached by the second, which is not a corner case: ``_auto_mirror_if_absent``
    skips when ``_zot_has_repo`` (a TAG-BLIND ``/v2/_catalog`` read) already
    sees the repo, so once ``deployed`` exists ``latest`` is never created;
    and on an OFFLINE box the auto-mirror never runs at all, making
    ``deployed`` the only tag that can exist there — the deploy then failed
    with "weights not present and offline mode" while the blobs sat in the
    registry one LAN hop away. The mirror tag still WINS when both exist (it
    is the curated list the manager itself dispatched), and it costs no
    second round-trip when it answers.

    A manager->registry read only (the registry is in-stack), so this is safe
    to run offline and must NOT be gated on HF reachability, unlike the
    auto-mirror dispatch above."""
    base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
    repo, tag = _zot_repo_ref(hf_repo or "", served)
    tags = [tag] + ([AUTO_CACHE_TAG] if tag != AUTO_CACHE_TAG else [])
    try:
        import httpx

        from app.api.registry import MANIFEST_ACCEPT
        # in-network (Zot) — #1409
        with httpx.Client(timeout=4, trust_env=False) as c:
            for t in tags:
                r = c.get(f"{base}/v2/{repo}/manifests/{t}",
                          headers={"Accept": MANIFEST_ACCEPT})
                if r.status_code != 200:
                    continue
                man = r.json() or {}
                out: list[dict] = []
                for ly in (man.get("layers") or []):
                    name = (ly.get("annotations") or {}).get("org.opencontainers.image.title")
                    digest = ly.get("digest")
                    if name and digest:
                        out.append({"name": name, "digest": digest})
                # An EMPTY manifest is not an answer: NODE-8 documents that a
                # repo-dir deploy used to publish ``"layers": []`` (the #1001
                # state), which advertises a cached model containing nothing.
                # Such a tag must not shadow a sibling tag that really does
                # hold the blobs — so keep looking rather than reporting
                # "not cached" on a hollow hit.
                if out:
                    return out
            return []
    except Exception:
        return []


def _served_name(s, dep) -> str:
    """The engine's own name for ``dep`` — ``Model.name`` when a model row is
    linked, else the client-facing ``model_name`` (pre-model-row deployments).
    The SAME resolution every other route already repeats inline
    (``apply_params``, ``start_deployment``, ``patch_deployment``) — factored
    out here because #307 S3 needs it a THIRD time, for the Zot repo:tag a
    deployment resolves to."""
    model = s.get(Model, dep.model_id) if dep.model_id else None
    return model.name if model is not None else dep.model_name


def _latest_disk_report(s, worker_id) -> Optional[dict]:
    """#307 S3: this worker's most recently COMPLETED #306 on-disk report (a
    ``list_disk_models`` ``NodeCommand`` with ``status='done'``), or ``None``
    if it has never reported one.

    CONSUMES #306's existing channel rather than re-implementing a disk scan —
    the worker-agent's ``_disk`` handler (``runtime.py``) already walks its
    model mount and posts the result back over the command channel
    (``POST /api/commands/{id}/result``); this is a plain read of the latest
    row that landed there. The fleet inventory does not itself enqueue a
    fresh scan (that stays an explicit admin action via
    ``POST /api/workers/{id}/commands``, same as the console's per-worker
    panel), so a GET here is always fast and never blocks on a worker's poll
    cadence — the tradeoff is that "cached" reflects the worker's LAST
    reported disk state, not a live one.
    """
    row = (
        s.query(NodeCommand)
        .filter(NodeCommand.worker_id == worker_id,
                NodeCommand.kind == "list_disk_models",
                NodeCommand.status == "done")
        .order_by(NodeCommand.finished_at.desc())
        .first()
    )
    return (row.result or {}) if row is not None else None


def _worker_has_files(disk_report: Optional[dict], file_names: list) -> bool:
    """True iff EVERY basename in ``file_names`` is present in ``disk_report``
    (the #306 shape: ``{"files": [{"name": <relpath-under-mount>}, ...]}``).

    Matched on basename, the SAME axis ``puller.pull_artifact`` /
    ``safe_artifact_name`` flatten an artifact's name to before it ever lands
    on a mount (#355) — that flattening is why a bare basename compare is
    correct here rather than a false negative on a subfolder difference.
    No report yet, or no known files to match against (an external backend,
    or a deployment whose weight source was never recorded), reports NOT
    cached — an inventory must never claim a model is cached on a guess.
    """
    if not disk_report or not file_names:
        return False
    on_disk = {os.path.basename(f.get("name") or "")
               for f in (disk_report.get("files") or [])}
    return all(os.path.basename(fn) in on_disk for fn in file_names)


def _file_provenance_map(dep_served_pairs) -> dict:
    """#835: which deployment each cached weight FILE belongs to, keyed by
    basename — the SAME axis ``_worker_has_files``/``puller.pull_artifact``
    already use to decide a file is already on a worker's disk. Built ONLY
    from ``Deployment.source_files`` (``dep_served_pairs`` is a list of
    ``(Deployment, served_model)`` — the caller already computed ``served``
    via ``_served_name`` for its own per-model row, so this reuses it instead
    of a second Model lookup).

    ``ModelArtifact``/``ModelArtifactFile`` model the same "which repo/quant
    does this file come from" idea in the schema, but nothing in this
    codebase ever constructs a ``ModelArtifact`` row today (no deploy path
    writes one) — joining against a table nothing populates would only ever
    contribute zero matches, so this reads the column every deploy actually
    writes instead of a second, dead one.

    KEY FINDING (no filename-guess provenance): a weight file's basename is
    NOT globally unique — the same ``Q4_K_M.gguf`` leaf name can legitimately
    come from two unrelated HF repos. Two deployments sharing one physical
    file (the identical GGUF served under two ``model_name``s) is fine —
    SAME ``hf_repo`` agrees, and either is a valid redeploy target — but when
    two DIFFERENT ``hf_repo``s both claim the same basename there is no way
    to tell which repo the cached BYTES actually came from without re-hashing
    the file, and guessing one would be exactly the forbidden behaviour. Such
    a basename maps to ``{"ambiguous": True}`` ONLY — no model/repo leaks
    through, so the console can never offer a redeploy off a guess.
    """
    by_basename: dict[str, list[dict]] = {}
    for dep, served in dep_served_pairs:
        for f in dep.source_files or []:
            b = os.path.basename(f)
            if not b:
                continue
            by_basename.setdefault(b, []).append({
                "deployment_id": str(dep.id),
                "model_name": dep.model_name,
                "served_model": served,
                "hf_repo": dep.hf_repo,
            })
    out: dict[str, dict] = {}
    for b, matches in by_basename.items():
        repos = {m["hf_repo"] for m in matches}
        # A basename is unambiguous only when a SINGLE deployment claims it, or
        # every claimant agrees on the SAME NON-NULL hf_repo. A null/empty
        # hf_repo cannot establish provenance — we don't know which repo the
        # cached bytes came from — so two DIFFERENT deployments that both lack a
        # repo (repos == {None}) sharing a basename are ambiguous, never a
        # confirmed match. Keying on ``len(repos) > 1`` alone silently treated
        # {None} (length 1) as agreement and returned an arbitrary matches[0]
        # (whichever sorts first by model_name) — exactly the filename-guess
        # this map exists to forbid (#910).
        unambiguous = len(matches) == 1 or (len(repos) == 1 and next(iter(repos)))
        if not unambiguous:
            out[b] = {"ambiguous": True}
            continue
        out[b] = {"ambiguous": False, **matches[0]}
    return out


def _unreferenced_cached(workers, reports: dict, provenance: dict) -> list[dict]:
    """#837 item 3: per worker, the cached weight entries NO deployment claims.

    The fleet inventory is built from ``Deployment`` rows, so a model that was
    fully undeployed (``DELETE /api/deployments/{id}`` drops the row outright)
    or was only ever mirrored has no row to read a file list from — and its
    weights, often the largest thing on the volume, were invisible in
    ``GET /api/inventory``. That is arguably the PRIMARY "free disk space"
    case. Both halves of the answer were already being read here — the
    per-worker #306 disk reports and the basename->deployment map
    ``_file_provenance_map`` builds — nothing joined them.

    Pure (the caller passes the reports it already fetched), so the rule is
    testable without a DB. Matched on BASENAME, the same axis every other join
    in this module uses (#303 flattening). A basename mapped to
    ``{"ambiguous": True}`` counts as CLAIMED — two deployments do claim it,
    the manager just cannot say which; calling it free disk would be exactly
    the filename guess ``_file_provenance_map`` exists to refuse. A worker with
    no report at all contributes nothing (unknown-safe, same convention as
    ``_worker_has_files``), and a worker with nothing unreferenced is omitted
    rather than listed as an empty row.
    """
    out: list[dict] = []
    for w in workers:
        report = reports.get(str(w.id)) or {}
        entries = []
        for f in (report.get("files") or []):
            name = f.get("name")
            if not name or os.path.basename(name) in provenance:
                continue
            entries.append({"name": name, "kind": f.get("kind"),
                            "size_gb": f.get("size_gb")})
        if not entries:
            continue
        out.append({
            "worker_id": str(w.id), "worker": w.name,
            "mount": report.get("mount"),
            "files": entries, "count": len(entries),
            "total_gb": round(sum(e["size_gb"] or 0 for e in entries), 2),
        })
    return out


def _fleet_inventory(s) -> dict:
    """#307 S3: per model known to the fleet (every ``Deployment`` row,
    deployed or paused) → registry presence in the in-stack Zot cache
    (reusing #289/#307 S1's ``_zot_has_repo`` — the SAME ``GET /v2/_catalog``
    read, not a second client) + which workers physically hold its weight
    files on disk (reusing #306's per-worker report via
    ``_latest_disk_report`` above). Introduces no new discovery mechanism —
    both axes already existed for placement/auto-mirror; this just surfaces
    them to an operator.

    Scope note (#837 item 3, closed): a model that was fully undeployed
    (``DELETE /api/deployments/{id}`` deletes the row) or only ever mirrored
    via ``POST /api/registry/mirror`` without a deployment has no
    ``Deployment`` row, so it cannot appear in ``models`` — which used to make
    its weights invisible entirely. They now ride in the sibling
    ``unreferenced`` key (``_unreferenced_cached``), joined from the per-worker
    disk reports this function already reads; freeing them is the existing
    per-worker ``POST /api/workers/{id}/disk-models/delete`` (#306), which the
    console's cache table already offers per row.

    #835: each model row also carries ``files``/``task``/``params``/``tags``/
    ``est_gb``/``runner_image`` — everything a faithful "redeploy this cached
    model onto worker X" call needs — plus a top-level ``file_provenance``
    basename map (``_file_provenance_map``) the console uses for a dumb
    per-file lookup in the on-disk cache view. Both ride this ALREADY
    admin-gated, already-#314-matrix-covered endpoint rather than a new
    route.
    """
    base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
    workers = s.query(Worker).order_by(Worker.name).all()
    reports = {str(w.id): _latest_disk_report(s, w.id) for w in workers}
    out = []
    dep_served_pairs = []
    for dep in s.query(Deployment).order_by(Deployment.model_name).all():
        served = _served_name(s, dep)
        dep_served_pairs.append((dep, served))
        repo, tag = _zot_repo_ref(dep.hf_repo or "", served)
        files = dep.source_files or []
        out.append({
            "deployment_id": str(dep.id),
            "model_name": dep.model_name,
            "served_model": served,
            "hf_repo": dep.hf_repo,
            "files": files,                        # #835 redeploy-from-cache payload
            "task": dep.task,                       # #835
            "params": dep.params or {},             # #835
            "tags": dep.tags or [],                 # #835
            "est_gb": dep.est_gb,                   # #835 admission accuracy
            "runner_image": dep.runner_image,       # #835
            "registry_repo": repo,
            "registry_tag": tag,
            "in_registry": _zot_has_repo(base, repo),
            "workers": [
                {"worker_id": str(w.id), "worker": w.name,
                 "cached": _worker_has_files(reports.get(str(w.id)), files)}
                for w in workers
            ],
        })
    provenance = _file_provenance_map(dep_served_pairs)
    return {"models": out, "file_provenance": provenance,
            # #837 item 3: cached weights no deployment claims — the
            # undeployed-but-still-on-disk case the models list cannot show.
            "unreferenced": _unreferenced_cached(workers, reports, provenance)}


#: #837 item 2: instance statuses that mean "this engine will never load these
#: weights again". Everything else — scheduled/pulling/loading/ready/pending —
#: is exposed to an eviction, either right now or at its next restart. A
#: DENY-list, deliberately: an allow-list of "ready" would have missed all four
#: of the other live phases, and a new phase added later would silently stop
#: warning instead of loudly over-warning.
DEAD_INSTANCE_STATUSES = ("failed", "stopped")


def _live_instances(s, dep, worker_id) -> list[str]:
    """#837 item 2: ``dep``'s instance ids on ``worker_id`` that are not dead.

    Python-side filtering over an unfiltered ``.all()`` — the shape
    ``registry.py::_live_deployment_repos`` documents: a stray or mutated
    condition then shows up in this slice's own tests instead of hiding inside
    a SQLAlchemy expression a fake session cannot evaluate. A fleet is dozens
    of instances and this runs on one admin action, so the read is cheap.
    """
    out: list[str] = []
    for inst in s.query(DeploymentInstance).all():
        if str(inst.deployment_id) != str(dep.id):
            continue
        if str(inst.worker_id) != str(worker_id):
            continue
        if (inst.status or "") in DEAD_INSTANCE_STATUSES:
            continue
        out.append(inst.instance_id or str(inst.id))
    return out


def _evict_warnings(dispatched: list, *, offline: bool) -> list[str]:
    """#837 items 2+4: the soft warnings an evict result carries. Pure, so the
    RULE is testable without a DB or a registry.

    Never a block. Freeing capacity must stay possible on any box in any state
    — the same asymmetry ``_enqueue_engine`` documents for the entitlement gate
    (only PROVISIONING is gated; evict/unload/stop never are).
    """
    out: list[str] = []
    live = [d for d in dispatched if d.get("live_instances")]
    if live:
        where = ", ".join(f"{d['worker']} ({', '.join(d['live_instances'])})" for d in live)
        out.append(
            f"{len(live)} worker(s) are still running an engine for this model: "
            f"{where}. Removing the weights does not stop a running engine — it "
            f"fails on its NEXT start (crash-recovery, restart_engine or a "
            f"reboot) with the weight file gone. Undeploy first if you want the "
            f"model stopped now.")
    if offline:
        out.append(
            "This box is in offline network mode: there is no HuggingFace "
            "fallback. If this is the last cached copy, the model cannot be "
            "re-deployed until someone re-mirrors it from a box that has it.")
    return out


def _evict_from_fleet(s, dep, *, evict_registry: bool = False) -> dict:
    """#307 S3 "remove from fleet": evict ``dep``'s weight files from EVERY
    worker whose latest #306 disk report shows it cached — dispatching
    ``evict_weights`` over the SAME ``enqueue_command`` channel
    ``load_engine``/``unload_engine`` already use — and OPTIONALLY the
    registry repo too, reusing ``registry.evict_registry_repo`` (the exact
    delete-by-digest sequence ``DELETE /api/registry/models/{repo}`` already
    runs, not a second Zot-eviction client).

    Returns the per-target dispatch set — which workers actually got an
    evict command — because "evicted" silently meaning "asked zero workers"
    would be a false success. Workers with no known cache of this model (no
    #306 report yet, or a report that doesn't cover its files — this
    naturally excludes external backends, which never run ``list_disk_models``
    at all) are skipped, not force-dispatched.
    """
    workers = s.query(Worker).order_by(Worker.name).all()
    files = dep.source_files or []
    dispatched = []
    for w in workers:
        report = _latest_disk_report(s, w.id)
        if not _worker_has_files(report, files):
            continue
        cmd = enqueue_command(s, w.id, "evict_weights", {"files": files})
        s.flush()
        dispatched.append({"worker_id": str(w.id), "worker": w.name,
                           "command_id": str(cmd.id),
                           # #837 item 2: the engines that are exposed to this
                           # eviction — see _evict_warnings.
                           "live_instances": _live_instances(s, dep, w.id)})

    registry_result = None
    if evict_registry:
        from app.api.registry import evict_registry_repo

        served = _served_name(s, dep)
        repo, tag = _zot_repo_ref(dep.hf_repo or "", served)
        try:
            registry_result = evict_registry_repo(repo, tag)
        except HTTPException as exc:
            registry_result = {"status": "error", "detail": exc.detail}

    return {
        "model_name": dep.model_name,
        "worker_ids": [d["worker_id"] for d in dispatched],
        "dispatched": dispatched,
        "registry_evict_requested": evict_registry,
        "registry": registry_result,
        # #837 items 2+4: soft warnings, never a block.
        "warnings": _evict_warnings(dispatched, offline=not _network_allows_hf()),
        "status": "evicting",
    }


def _network_allows_hf() -> bool:
    """Manager-side mirror of worker-agent's ``hf_pull.network_allows_hf``
    semantics (the two services cannot share a module — same reasoning as
    ``RUNNER_IMAGE_RE`` above): HF is reachable in online/proxied, refused in
    offline. #307 S1 must not turn every offline deploy into a NEW automatic
    HF reach — before this slice an offline box never mirrored anything on
    deploy (nothing did it automatically), and this keeps that true."""
    mode = (os.environ.get("RAZZFAZZ_NETWORK_MODE") or "online").strip().lower()
    return mode != "offline"


def _auto_mirror_if_absent(s, worker, dep, *, hf_repo, files, served) -> bool:
    """#307 S1: make sure ``dep`` is already a cached repo in the in-stack Zot
    registry BEFORE its engine is placed on a worker — the "model is in the
    registry" predicate Slice 2 (worker pulls from the master instead of HF)
    will consume.

    Dispatches the EXISTING ``mirror_model`` command (the same machinery
    ``mirror_into_cache`` uses — worker-agent's ``perform_mirror``/``pusher.py``)
    exactly ONCE when the model is absent from the registry; a model already
    cached dispatches ZERO mirrors and the caller proceeds straight to
    placement. Sets ``dep.status = "mirroring"`` when it dispatches, so the
    deploy status surfaces the phase the same way an instance's
    ``status``/``detail`` surface "pulling NN%" (``_deployment_health``
    above). Returns True iff a mirror was dispatched.
    """
    if not hf_repo or not files:
        return False  # nothing to mirror without a weight source
    if not _network_allows_hf():
        return False  # offline: never a NEW HF reach (#307 S1 network-mode rule)
    base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
    repo, tag = _zot_repo_ref(hf_repo, served)
    if _zot_has_repo(base, repo):
        return False
    enqueue_command(s, worker.id, "mirror_model",
                    {"hf_repo": hf_repo, "files": files, "repo": repo, "tag": tag})
    dep.status = "mirroring"
    return True


#: LLMM-5: upper bound on a deployment's desired replica count. Every route that
#: can RAISE the count must apply it — the loop that places the missing
#: instances runs once per replica, doing several DB queries plus two INSERTs
#: each, and `_place_within_budget` cannot be relied on to end it early (`avoid`
#: is a preference, and `_budget_check` fits whenever `est_gb` is unset). No
#: fleet this stack targets runs more than a few dozen workers.
MAX_REPLICAS = 32


def _validate_replicas(n) -> int:
    """Shared bounds check for a desired replica count (LLMM-5).

    `POST /api/deployments` carried both bounds while `PATCH
    /api/deployments/{id}` checked only `>= 1`, so a `PATCH {"replicas":
    100000}` drove a 100k-iteration placement loop the sibling route rejected
    ten lines away. Both call THIS, so the two cannot diverge again."""
    if n < 1:
        raise HTTPException(status_code=422, detail="replicas must be >= 1")
    if n > MAX_REPLICAS:
        raise HTTPException(status_code=422,
                            detail=f"replicas must be <= {MAX_REPLICAS}")
    return int(n)


def _resolve_dep_engine(files, worker_engine, override=None) -> str:
    """Decide which engine a deployment runs, and refuse a certain mismatch.

    #361: the engine is decided by the ARTIFACT, not merely by the worker's
    default label. A GGUF is llama.cpp food on EVERY hardware class — AMD/CPU
    (Vulkan/CPU runners) and CUDA too (the llama-cuda-runner +
    CudaLlamaCppDriver) — so a cuda worker whose registration label is "vllm"
    can still serve a GGUF via llama.cpp. HF-format (a safetensors dir) stays on
    the worker's own engine.

    LLMM-6: the #575 "reject a GGUF resolved to vLLM loudly rather than
    crash-loop" guard used to be unreachable. It only fires on an explicit
    engine override, `DeployRequest` declared no `engine` field (pydantic drops
    unknown keys), and the GGUF auto-resolution ran FIRST and would have won
    anyway. The field now exists and the override is honoured BEFORE the
    auto-resolution, which is what makes the guard real.

    #1518 (E5): that guard now covers every artifact, not just GGUF. The fleet
    ships no vLLM engine any more — no driver, no image — so a deployment
    resolved to ``vllm`` can only end as a node-side pull failure or a
    crash-loop. It is refused here, where the operator sees why. The value can
    still arrive two ways: an explicit override, or a node that registered
    before the upgrade and still advertises ``engine=vllm``; the second is why
    the check sits after the resolution rather than only on ``override``."""
    all_gguf = bool(files) and all(str(f).lower().endswith(".gguf") for f in (files or []))
    if override:
        engine = canonical_engine(override)
    elif all_gguf:
        engine = _CANONICAL_LLAMACPP
    else:
        engine = canonical_engine(worker_engine) or "unknown"
    if engine == "vllm":
        raise HTTPException(status_code=422, detail=(
            "The vLLM engine was removed in 2026.09 — every model the fleet "
            "serves is GGUF on llama.cpp, including on NVIDIA (the CUDA runner "
            "is picked from the GPU's compute capability). "
            + ("Deploy these GGUF artifacts on the llama.cpp engine instead."
               if all_gguf else
               "Deploy a GGUF build of this model instead of the "
               "HF-format (safetensors) directory.")))
    return engine


#: every spelling of the llama.cpp engine an operator or a worker can produce.
#: `DeployRequest.engine` is free text and a worker's `labels.engine` is
#: self-reported, so neither is a closed set.
_LLAMACPP_ALIASES = frozenset({"llamacpp", "llama.cpp", "llama_cpp", "llama-cpp",
                               "llamabox", "llama-box", "llama_box"})

#: the ONE spelling the rest of the stack understands. The node's
#: `_ENGINE_REGISTRY` looks up `(hardware, engine)` with an EXACT match on
#: "llamacpp", so anything else falls through to the per-hardware default.
#:
#: That default used to be vLLM, and this comment said so until #1745b. It is
#: not any more: #1518 (E5) removed the vLLM engine from the fleet — there is
#: no driver (`node-agent/app/drivers/` ships amd, cpu, cuda_llamacpp, gb10)
#: and no image — and `_resolve_dep_engine` REFUSES a deployment recorded with
#: `engine=vllm` with a 422 rather than trying to launch one.
#:
#: agent-seqis found the stale line while measuring the GB10 (#1332), and it is
#: the expensive kind of wrong: a reader deciding what happens to an unpinned
#: deployment on cuda-gb10 would conclude "it lands on vLLM" and plan around a
#: path the product has removed. On that box the answer decides whether the
#: llama runner is optional or mandatory — and it is mandatory.
_CANONICAL_LLAMACPP = "llamacpp"


def canonical_engine(engine) -> str:
    """Normalise an engine name at the ONE place the value is decided.

    #1494 re-review, finding 5: the first fix taught `_require_llamacpp_weights`
    seven spellings. That made the manager ACCEPT `engine="llama.cpp"` with a
    .gguf — and the node then failed the exact-match lookup, fell through to
    the per-hardware default and started vLLM on GGUF artifacts: the #361/#575
    crash-loop, newly reachable. Widening a check without narrowing the value
    it checks moves the failure downstream.

    So the spellings collapse HERE, in `_resolve_dep_engine`, before anything
    is persisted; `dep.engine` only ever holds a canonical name, and the
    private alias lists that had started to diverge (`_runtime_meta` carried
    four of the seven, without the strip) all read this instead.
    """
    e = str(engine or "").strip().lower()
    return _CANONICAL_LLAMACPP if e in _LLAMACPP_ALIASES else e


def _require_llamacpp_weights(engine: str, files, *, where: str = "files[]") -> None:
    """#1372: llama.cpp needs a .gguf in files[]. The node enforces it (400 on
    load_engine), but by then the deployment row exists and nothing told it —
    `pending` forever, reason null.

    #1494: this sits in the FUNNEL (`_enqueue_engine`), not at the door — every
    way an engine gets enqueued passes it, including the paths that have no
    request body of their own (scale-up, reassign, the #263 reconciler, the
    #549 runner upgrade). `where` names the field the CALLER can actually
    change: a PATCH has no `files[]` to fix, so telling its operator to "pass
    hf_repo + files" sends them looking for a field that is not there.

    The exemption this used to promise — "a repo-directory deploy (#574,
    files == []) is untouched" — was only ever reachable through vLLM, the one
    engine that served a directory, and #1518 removed it. It is gone rather
    than left standing: an empty file list now means the manager knows no
    quantisation, and #1573 tracks resolving the repo up front instead of
    opening this door."""
    # normalised, not widened: a stored row from before this change may still
    # carry a variant spelling, and canonical_engine folds it the same way
    # _resolve_dep_engine now does at the door.
    if canonical_engine(engine) != _CANONICAL_LLAMACPP:
        return
    if any(str(f).lower().endswith(".gguf") for f in (files or [])):
        return
    raise HTTPException(status_code=422, detail=(
        f"llama.cpp deploy needs a .gguf weights file in {where} — pick a "
        "quantisation (the catalog's filename); "
        f"got {list(files or [])!r}"))


def _weights_gb(files: list, tree: list) -> float:
    """GB of weights this deploy will actually pull, from the HF tree's sizes.

    Matched on BASENAME: `files` carries filenames (that is what the catalog,
    the console's quant grouping and `complete_files` produce), while the tree
    carries repo-relative paths. A repo that keeps its GGUFs in a subdirectory
    would otherwise size every file at zero — and zero is the one answer that
    must never turn into a number (see `_derive_est_gb`).
    """
    sizes: dict = {}
    for f in tree or []:
        if not isinstance(f, dict):
            continue
        name = (f.get("path") or f.get("rfilename") or "").rsplit("/", 1)[-1]
        if not name:
            continue
        size = int(f.get("size") or (f.get("lfs") or {}).get("size") or 0)
        # A repo can carry the same basename in two directories. Keep the
        # larger: under-counting is precisely what makes the gate inert.
        sizes[name] = max(sizes.get(name, 0), size)
    total = sum(sizes.get(str(x).rsplit("/", 1)[-1], 0) for x in files or [])
    return round(total / 1e9, 2)


#: Seconds the deploy path will spend looking a repo up for an ESTIMATE (#1648).
#: Small on purpose: the estimate is optional, the deploy is not.
_ESTIMATE_BUDGET_S = 5.0


def _derive_est_gb(payload, dep_engine) -> Optional[float]:
    """The footprint of THIS deploy, when the caller did not send one (#1648).

    Measured on the fleet: all seventeen deployments carried `est_gb = None`,
    so `_committed_gb` summed nothing and the #227 gate compared that against a
    budget. The cause was a path difference, not neglect — the console computes
    the estimate before deploying, the CLI path does not, and the CLI path is
    how a box builds its standard set.

    Both halves already existed and nobody had joined them: `fetch_repo_facts`
    brings the file tree WITH SIZES (#1573), `footprint_gb` is the console's
    formula on this side (#1653). This is the join, and deliberately the SAME
    formula rather than a second one — two estimators for one quantity are two
    truths.

    Three refusals, each of which would otherwise produce a confident wrong
    number:

    * a caller-supplied estimate WINS and is never second-guessed;
    * an offline box refuses the lookup (`fetch_repo_facts` raises 503) and the
      deploy proceeds without an estimate exactly as it does today — the
      unaccounted-placement brake from #1646 is what carries there;
    * weights that size to zero yield None, not a number. An estimate built on
      zero weights passes the gate while claiming to have been measured, which
      is the #1422 failure with extra steps.
    """
    if payload.est_gb is not None:
        return payload.est_gb
    if not payload.hf_repo or not payload.files:
        return None
    # Imported at call time: app.api.hf pulls in httpx, and inventory is
    # imported by paths that must not need it.
    from app.api.hf import fetch_repo_facts
    try:
        # A courtesy lookup gets a courtesy budget. The console route may spend
        # 25 s enriching a page an operator is looking at; a deploy must not be
        # held open that long for a number it can also do without.
        facts = fetch_repo_facts(payload.hf_repo, budget_s=_ESTIMATE_BUDGET_S)
    except HTTPException as e:
        # 503 offline, 404 unknown repo, 502 upstream, 422 malformed id — every
        # one of these is a KNOWN outcome, and none is a reason to refuse a
        # deploy that works today. INFO, and the status is named: this line is
        # the operator's only evidence of why a deployment carries no estimate.
        logger.info("no footprint estimate for %s: repo lookup said %s (#1648)",
                    payload.hf_repo, getattr(e, "status_code", "?"))
        return None
    except Exception as e:
        # Anything else is a surprise, not a known outcome — same graceful
        # result, louder line. Collapsing the two would make an unexpected
        # failure read exactly like an offline box, which is how a real fault
        # spends months looking like a configuration choice.
        logger.warning("no footprint estimate for %s: unexpected %s: %s (#1648)",
                       payload.hf_repo, type(e).__name__, e)
        return None
    size_gb = _weights_gb(payload.files, facts.get("files") or [])
    est = footprint_gb(dep_engine, size_gb, facts.get("arch"), payload.params or {})
    if est is not None:
        logger.info("derived est_gb=%s for %s (%s GB weights, arch %s) — #1648",
                    est, payload.model_name, size_gb,
                    "known" if facts.get("arch") else "unknown, lower bound")
    return est


#: #1573: how the manager picks a quantisation when the operator named only a
#: repo. ONE named rule, so the answer is explainable and repeatable: the
#: LARGEST quantisation whose resident footprint still fits the target worker's
#: free budget. Largest, because quality rises with it; footprint rather than
#: weight size, because the weights are not what fills the card.
_QUANT_RULE = "largest quantisation whose footprint fits the worker's free budget"


def _resolve_quantisation(s, payload, worker, dep_engine) -> dict:
    """Turn `hf_repo` + no file selection into a concrete quantisation (#1573).

    The door used to be shut here, and shut for a good reason: `_budget_check`
    is INERT without `est_gb`, and `est_gb` is computed from the SIZE of a
    chosen quantisation. Without a choice the manager placed a residency whose
    size it did not know and learned it only once the node had downloaded it —
    the cost landing on the NEXT deploy, which is the damage #227 exists to
    prevent.

    So the resolution moves to the front instead of the door being opened: the
    manager picks, the answer is reported back, and the gate gets its number.

    Refusals, and each says what the operator can do about it:

    * no lookup (offline, unknown repo, upstream down) → 422. Guessing a
      quantisation is worse than saying "name the file yourself": one is a
      question, the other is a residency of unknown size.
    * no worker budget → 422. The rule IS the budget; without one there is
      nothing to be the largest that fits, and picking "the biggest" would be
      the unbounded placement in a new costume.
    * nothing fits → 409, naming the free budget and the smallest candidate, so
      the operator can see whether to free room or pick a smaller model.
    """
    from app.api.hf import build_quants, fetch_repo_facts

    try:
        facts = fetch_repo_facts(payload.hf_repo)
    except HTTPException as e:
        raise HTTPException(422, (
            f"cannot resolve a quantisation for {payload.hf_repo}: the repo "
            f"lookup failed ({getattr(e, 'detail', e)}). Name the weight file "
            "in files[] instead."))

    arch = facts.get("arch")
    params = payload.params or {}

    candidates = []
    for q in build_quants(facts.get("files") or [], workers=[]):
        # No separate size filter: `footprint_gb` already answers None for a
        # weight of None, 0 or 0.0 (measured), which is the #1648 rule — no
        # number beats a wrong one. A second check in front of it would be
        # code nothing executes, and a mutation of it would change no verdict.
        est = footprint_gb(dep_engine, q.get("size_gb"), arch, params)
        if est is None:
            continue
        candidates.append((est, q))
    if not candidates:
        raise HTTPException(422, (
            f"cannot resolve a quantisation for {payload.hf_repo}: the repo "
            "carries no sized GGUF quantisation. Name the weight file in "
            "files[] instead."))
    candidates.sort(key=lambda c: c[0])

    # Whether a candidate fits is asked of `_budget_check` and of nothing else
    # (#330 stage 2). Its own committed/budget arithmetic here would be a
    # SECOND admission authority — the exact shortcut review #709 removed from
    # the console deploy path — and it would silently skip the dynamic leg and
    # the #1422 settling brake, so the chooser could pick a quantisation the
    # gate then refuses two lines later.
    fitting, budget, committed = [], None, None
    for est, q in candidates:
        ok, committed_i, _projected, budget_i, dyn = _budget_check(s, worker, est)
        if dyn and dyn.get("settling"):
            raise HTTPException(409, (
                f"cannot resolve a quantisation on {worker.name} yet: an "
                "earlier placement has not shown up in its VRAM reading, so "
                "there is no honest free figure to choose against. Retry once "
                "the worker reports, or name the weight file in files[]."))
        if budget_i is None:
            raise HTTPException(422, (
                f"cannot resolve a quantisation: {worker.name} reports no VRAM "
                "budget, and the rule IS the budget — without one there is "
                "nothing to be the largest that fits. Name the weight file in "
                "files[]."))
        budget, committed = budget_i, committed_i
        if ok:
            fitting.append((est, q))
    free = budget - committed
    if not fitting:
        smallest_est, smallest = candidates[0]
        raise HTTPException(409, (
            f"no quantisation of {payload.hf_repo} fits {worker.name}: "
            f"{free:.1f} GB free of {budget:.1f} GB, and the smallest "
            f"({smallest['label']}, {smallest['size_gb']} GB weights) needs "
            f"{smallest_est:.1f} GB resident. Free room, pick another worker, "
            "or name a file in files[] with force=true."))

    est, chosen = fitting[-1]
    logger.info("#1573: picked %s (%s GB weights, %.1f GB resident) for %s on "
                "%s — %.1f GB free; rule: %s",
                chosen["label"], chosen["size_gb"], est, payload.model_name,
                worker.name, free, _QUANT_RULE)
    return {"label": chosen["label"], "files": list(chosen["files"]),
            "size_gb": chosen["size_gb"], "est_gb": est,
            "free_gb": round(free, 1), "rule": _QUANT_RULE}


class DeployRequest(BaseModel):
    model_name: str                      # client-facing (LiteLLM model_name)
    served_model: Optional[str] = None   # engine's own name; defaults model_name
    worker_id: Optional[str] = None      # explicit target; else first ready worker
    hardware: Optional[str] = None       # selector when auto-picking
    files: list[str] = []                # weight files (from catalog/registry)
    hf_repo: Optional[str] = None        # #287 HF source to fetch missing weights
    params: dict = {}
    task: str = "chat"                   # chat | embed | rerank (serve mode)
    tags: list = []                      # #296 operator tags (own column, not params)
    est_gb: Optional[float] = None       # #227 resident footprint estimate (weights+KV+compute) for admission control
    force: bool = False                  # #227 bypass the VRAM-budget admission gate
    # #549 R1: pin THIS deployment to a runner version. None = the node's default
    # for its hardware class (pre-R1 behaviour).
    runner_image: Optional[str] = None
    replicas: int = 1                    # #284 deploy N instances in one action
    # LLMM-6: explicit engine override. None = resolve from the artifact
    # format, else the worker's engine label. Declaring it is what makes the
    # #575 / #1518 vLLM guard reachable (see _resolve_dep_engine); "llamacpp"
    # is the only value a shipped fleet accepts.
    engine: Optional[str] = None


def _relaunch_live_instances(s, dep, served, instances) -> int:
    """#566 / #1263: relaunch every live, non-external instance of `dep` from
    the deployment row (the one params->CLI-flags authority). Surplus over
    `replicas` is reaped, not relaunched (#1045). Returns the relaunch count;
    the caller decides whether zero is an error (apply-params: 409) or simply
    "nothing running yet" (reconcile: params apply on the next deploy)."""
    target = max(1, dep.replicas or 1)
    ranked = sorted(
        instances,
        key=lambda di: (0 if di.status == "ready" else 1,
                        -(di.started_at.timestamp() if di.started_at else 0)))
    keep, surplus = ranked[:target], ranked[target:]
    for di in surplus:
        w = s.get(Worker, di.worker_id) if di.worker_id else None
        c = _container(di.endpoint)
        if w is not None and not (w.labels or {}).get("external") and c:
            enqueue_command(s, w.id, "unload_engine",
                            {"instance_id": c, "container": c})
        s.delete(di)                     # #1045: reap surplus, do NOT relaunch
        s.flush()
    relaunched = 0
    for di in keep:
        worker = s.get(Worker, di.worker_id) if di.worker_id else None
        if worker is None or (worker.labels or {}).get("external"):
            continue                     # #318: nothing to unload/launch
        container = _container(di.endpoint)
        # #2019: unload-first costs a single-replica deployment its whole
        # presence in the router. `generate_from_db` keeps only `ready`
        # instances and then drops a deployment with none of them from
        # `model_list` outright, so between the two statements below the model
        # is not slow — it is ABSENT, and the client gets
        # `404 no ready … engine` (measured on 0.79: 17.76 s, 1744 requests).
        # When the weights provably fit twice, start the new engine FIRST and
        # let the old one keep serving until it does; `retiring_since` is the
        # note that says so, and `api/workers.py` acts on it when a ready
        # sibling appears. The decision is #1867's, asked about THIS deployment
        # (#2019 scope) — every hard case (no fresh reading, unknown footprint,
        # does not fit twice, host memory too small) already has its answer
        # there, and all of them fall back to the unload-first path below,
        # which is exactly today's behaviour.
        if _overlap_relaunch(s, dep, worker, di, served):
            relaunched += 1
            continue
        if container:
            enqueue_command(s, worker.id, "unload_engine",
                            {"instance_id": container, "container": container})
        s.delete(di)
        s.flush()
        _enqueue_engine(s, dep, worker, served, dep.source_files,
                        dep.hf_repo, dep.params, dep.task)
        relaunched += 1
    return relaunched


def _overlap_relaunch(s, dep, worker, di, served) -> bool:
    """#2019: try the blue-green relaunch for ONE instance. True if taken.

    Blue-green here means: leave the old engine running and serving, start the
    new one alongside it, and mark the old row `retiring_since` so the report
    path retires it once the replacement is actually `ready`.

    Three reasons this stays out of `_budget_check` even though it doubles the
    footprint for a while — and the reason the ADMISSION_EXEMPT entry for
    `_relaunch_live_instances` needed rewriting rather than deleting:
    `switch_interruption` performs its own admission, it is STRICTER than the
    committed-budget check (it requires a fresh VRAM reading AND that the second
    copy fits in host memory too, #1947), and it fails closed — anything it
    cannot compute answers `interrupting`, which lands on the unload-first path.

    An instance already marked `retiring_since` is never overlapped again: that
    is an overlap still in flight, and starting a third engine for one
    deployment is how a box runs out of memory while trying not to.
    """
    if getattr(di, "retiring_since", None) is not None:
        return False
    try:
        from app.api.runner_upgrade import switch_interruption
        verdict = switch_interruption(s, worker, deployments=[dep])
    except Exception:  # noqa: BLE001 — the fallback IS the contract (#1913)
        logger.warning("overlap verdict unavailable for %s; taking the "
                       "unload-first relaunch (the safe direction, #2019)",
                       dep.model_name, exc_info=True)
        return False
    if verdict.get("interrupting", True):
        logger.info("apply-params on %s relaunches unload-first: %s (#2019)",
                    dep.model_name, verdict.get("why", "unspecified"))
        return False
    di.retiring_since = datetime.now(timezone.utc)
    s.flush()
    _enqueue_engine(s, dep, worker, served, dep.source_files,
                    dep.hf_repo, dep.params, dep.task)
    logger.info("apply-params on %s relaunches blue-green: %s (#2019)",
                dep.model_name, verdict.get("why", "fits-twice"))
    return True


class DeploymentReconcile(BaseModel):
    """#1263: the manifest's desired state for a standard-set deployment."""
    model_config = ConfigDict(extra="forbid")
    params: dict                         # full replace (same contract as PATCH)
    task: Optional[str] = None           # chat|embed|rerank; None = unchanged


#: #1263: the ownership marker. post-install stamps every standard-set deploy
#: with this tag (cli/lib-llm-manager-deploy.sh::_llmm_deploy_payload); an
#: operator who edits such a deployment in the console and wants the edit to
#: stick removes the tag — from then on the manifest reconcile leaves it alone.
MANIFEST_OWNED_TAG = "standard-set"


class DeploymentPatch(BaseModel):
    # #1187: an unknown key used to be DROPPED silently (pydantic default) —
    # `PATCH {"runner_image": …}` answered 200 and did nothing, so the caller
    # believed the pin was set. Unknown fields are a 422 naming the key.
    model_config = ConfigDict(extra="forbid")

    replicas: Optional[int] = None       # desired instance count
    task: Optional[str] = None           # chat|embed|rerank
    params: Optional[dict] = None        # full replace of backend params
    tags: Optional[list] = None          # #296 operator tags (own column)
    force: bool = False                  # #335 bypass the VRAM-budget gate on scale-up
    display_name: Optional[str] = None   # #284 console-only label; model_name unchanged
    # #1187 / #549 R1: pin THIS deployment to a runner image, applied by the
    # next (re)launch (apply-params, scale-up, reassign, reconciler — all of
    # which funnel through _enqueue_engine, which reads dep.runner_image).
    # Omitted → unchanged; explicit null → CLEAR the pin (node default again);
    # a string must be well-formed AND registry-qualified, see the handler.
    runner_image: Optional[str] = None
    # #1542: where the NEW replicas go. Omitted → the auto-placement this
    # endpoint has always done (prefer a worker that does not already host this
    # deployment, fall back to one that does). Given → every replica this call
    # adds is placed on THAT worker, or the call fails saying why; it is never
    # silently placed elsewhere. Ignored when `replicas` does not grow — there
    # is nothing to place — which is a 422 rather than a quiet no-op.
    worker_id: Optional[str] = None


class ReassignRequest(BaseModel):
    # #304: move a deployment's running instance onto a DIFFERENT worker,
    # post-deploy — today the worker is fixed at deploy time and the only way
    # to move a model is undeploy + redeploy (losing the desired-state row
    # while it is gone). Reuses DeploymentInstance.worker_id — no new column.
    worker_id: str                       # target worker
    instance_id: Optional[str] = None    # which instance moves; required when >1 live
    force: bool = False                  # #304 bypass the VRAM-budget gate, like deploy/patch


class MirrorRequest(BaseModel):
    hf_repo: str                         # HuggingFace repo to mirror from
    files: list[str] = []                # weight files (from the quant picker)
    name: Optional[str] = None           # cache repo name; defaults from hf_repo
    tag: Optional[str] = None            # quant label; defaults "latest"
    worker_id: Optional[str] = None      # which node runs the mirror (master)


class ExternalBackendRequest(BaseModel):
    name: str                            # display name, e.g. "smart-home-mac"
    endpoint: str                        # OpenAI-compatible base, e.g. http://192.0.2.10:11434/v1
    models: Optional[list[str]] = None   # explicit list; else discovered from {endpoint}/models
    hardware: str = "external"
    engine: str = "ollama"
    api_key: Optional[str] = None        # upstream key if the endpoint needs one
    tasks: Optional[dict[str, str]] = None  # #318 per-model serve-task override {model: chat|embed|rerank}; else auto-inferred


def fleet_gpu_summary() -> list[dict]:
    """Slim, service-key-safe fleet GPU/worker view for internal read consumers.

    Same underlying source as ``GET /api/workers`` (worker labels + the live
    ``metrics`` heartbeat sample), but only the non-sensitive capacity +
    utilization fields — no per-worker addresses, deployment instances, or
    admission internals. It exists because ``/api/*`` is ingress/SSO-gated
    (``require_role`` + the ingress-proxy origin check) and therefore rejects
    container-to-container service-key calls with 403 "request did not originate
    from the ingress proxy". Internal dashboards (the Configuration Portal's GPU
    panel) instead read this via ``GET /v1/workers`` on the key-auth hot path,
    the same tier ``GET /v1/models`` already uses. Pure DB read, no I/O beyond
    the session."""
    with session_scope() as s:
        now = datetime.now(timezone.utc)
        out: list[dict] = []
        for w in s.query(Worker).order_by(Worker.name).all():
            labels = w.labels or {}
            out.append(
                {
                    "name": w.display_name or w.name,
                    "hardware": labels.get("hardware"),
                    "status": _display_status(w, now),
                    "vram_total_gb": labels.get("vram_total_gb"),
                    "vram_used_gb": _fresh_vram_used_gb(w),
                    # live sample (gpu_util / vram_used_gb / mem_used_gb / load / ncpu)
                    "metrics": labels.get("metrics"),
                }
            )
        return out


def register_inventory_api(app) -> None:
    # #314: fleet read + deploy/configure/undeploy models is the ADMIN tier
    # (matrix: "Deploy / configure / undeploy models"). `/api/workers/external`
    # here folds a pre-existing OpenAI endpoint into the router — an admin
    # console action (self-described as such), NOT the node-key worker
    # federation lifecycle that lives at super-admin in workers.py/enroll.py.
    router = APIRouter(dependencies=[Depends(require_role(Role.ADMIN))])

    @router.get("/api/workers")
    def list_workers():
        with session_scope() as s:
            now = datetime.now(timezone.utc)
            rows = []
            for w in s.query(Worker).order_by(Worker.name).all():
                insts = (
                    s.query(DeploymentInstance, Deployment)
                    .join(Deployment, DeploymentInstance.deployment_id == Deployment.id)
                    .filter(DeploymentInstance.worker_id == w.id)
                    .all()
                )
                labels = w.labels or {}
                # #328 admission is silently INERT for a worker with no capacity
                # label, and nothing showed that anywhere. Report the basis and the
                # effective budget so an operator can see it rather than infer it
                # from a deploy that should have been refused and was not.
                _basis, _total = _capacity_source(w)
                # #330 stage 2: the same freshness + arithmetic the gate uses —
                # the console must never disagree with the admission math (#328).
                _used = _fresh_vram_used_gb(w) if _basis == "vram" else None
                rows.append(
                    {
                        "id": str(w.id),
                        "admission_basis": _basis,
                        "admission_budget_gb": (round(max(0.0, _total - _reserved_external_gb(w)) * _VRAM_HEADROOM, 1)
                                                if _total is not None else None),
                        "vram_used_gb": _used,
                        "admission_free_now_gb": (round(max(0.0, _total - _used) * _VRAM_HEADROOM, 1)
                                                  if _used is not None and _total is not None else None),
                        "name": w.name,
                        "display_name": w.display_name,  # #284 manager-owned rename
                        "address": w.address,
                        "hardware": labels.get("hardware"),
                        "engine": labels.get("engine"),
                        "stack_version": labels.get("stack_version"),
                        # #1951: never null. `None` was a FIFTH state the
                        # console had no name for — "this agent predates the
                        # field", which during a rollout is the COMMONEST state
                        # and the only one an operator fixes by updating the
                        # agent. Measured on box-175r (DevBox-Vuko, 2026-09-11):
                        # it reported a version and no source, and the console
                        # showed it identically to a node that looked and found
                        # nothing. Synthesised here rather than stored, because
                        # the node did not say it — the manager is stating the
                        # absence, not repeating a claim.
                        "stack_version_source": (labels.get("stack_version_source")
                                                 or "not-reported"),
                        "engine_version_why": (labels.get("engine_version_why")
                                               or "not-reported"),
                        "engine_version": labels.get("engine_version"),
                        "advertise_addr": labels.get("advertise_addr"),
                        "mem_total_gb": labels.get("mem_total_gb"),
                        "vram_total_gb": labels.get("vram_total_gb"),
                        # Live utilization sample (gpu_util / vram_used_gb / mem_used_gb
                        # / load / ncpu), written by the worker-agent into
                        # labels["metrics"] each heartbeat. The console dashboard's
                        # "Load & usage" graph reads w.metrics.*; without lifting it to
                        # the top level here it was always undefined and the chart sat
                        # at "Collecting live samples…" forever.
                        "metrics": labels.get("metrics"),
                        "status": _display_status(w, now),   # #298 stale if heartbeat old
                        # #1264: what placement will actually accept — the same
                        # predicate _assert_placeable raises 409 from. A caller
                        # choosing a deploy target must read `placeable`, never
                        # infer it from `status` (an external backend can be
                        # `ready` and still run no worker-agent).
                        "external": bool(labels.get("external")),
                        "placeable": _placeability(w, now)[0],
                        "placeable_reason": _placeability(w, now)[1],
                        "last_heartbeat": _iso(w.last_heartbeat),
                        "instances": [
                            {
                                "id": str(di.id),
                                "model_name": dep.model_name,
                                "endpoint": di.endpoint,
                                "container": _container(di.endpoint),
                                "status": di.status,
                                "detail": di.detail,
                            }
                            for di, dep in insts
                        ],
                    }
                )
            return rows

    @router.get("/api/workers/metrics")
    def worker_metrics():
        """#1598: the four dashboard numbers, their denominators, and WHEN they
        were taken. Nothing else.

        The console polls this once a second so a new sample is on the chart
        within a second of landing. `GET /api/workers` cannot be polled at that
        rate and the issue said so before this was written (#1598 decision (a)):
        it runs a per-worker instance JOIN, computes the admission basis, the
        budget and the placeability predicate twice, and returns the whole
        instance table — all of it right once every five seconds for the fleet
        view, all of it wasted for a chart that wants five floats.

        `at` is the SERVER stamp written beside `labels["metrics"]`, and it is
        the point of this route. The console appends a sample only when that
        stamp moves, which is what keeps the drawn line honest: a node that
        reports every 30 s produces ten points across five minutes rather than
        the same reading repeated at the poll rate. Without a stamp the console
        cannot tell a new measurement from a re-read of an old one, and a chart
        that cannot tell them apart will always choose to look busy.

        No freshness cut-off here on purpose: a stale sample is shown as what it
        is — a point that stops advancing — which reads as "this node went
        quiet". Dropping it would instead make the line jump to the remaining
        workers' average, and nothing on the screen would say why.
        """
        with session_scope() as s:
            out = []
            for w in s.query(Worker).order_by(Worker.name).all():
                labels = w.labels or {}
                m = labels.get("metrics") or {}
                if not m:
                    continue
                out.append({
                    "id": str(w.id),
                    "at": labels.get("metrics_at"),
                    "gpu_util": m.get("gpu_util"),
                    "vram_used_gb": m.get("vram_used_gb"),
                    "mem_used_gb": m.get("mem_used_gb"),
                    "load": m.get("load"),
                    "ncpu": m.get("ncpu"),
                    # the denominators, so the console turns the readings into
                    # percentages without a second round trip to /api/workers.
                    "vram_total_gb": labels.get("vram_total_gb"),
                    "mem_total_gb": labels.get("mem_total_gb"),
                })
            return out

    @router.get("/api/deployments")
    def list_deployments():
        with session_scope() as s:
            rows = []
            for d in s.query(Deployment).order_by(Deployment.model_name).all():
                insts = (
                    s.query(DeploymentInstance, Worker)
                    .outerjoin(Worker, DeploymentInstance.worker_id == Worker.id)
                    .filter(DeploymentInstance.deployment_id == d.id)
                    .all()
                )
                inst_list = []
                for di, w in insts:
                    hw = (w.labels or {}).get("hardware") if w is not None else None
                    dev, arch = _runtime_meta(d.engine, hw)
                    inst_list.append({
                        "id": str(di.id),
                        "worker": (w.name if w is not None else None),
                        "worker_id": str(di.worker_id) if di.worker_id else None,
                        "endpoint": di.endpoint,
                        "container": _container(di.endpoint),
                        "status": di.status,
                        "detail": di.detail,
                        "started_at": _iso(di.started_at),
                        "params_effective": di.params_effective or {},
                        "hardware": hw,
                        "device": dev,
                        "arch": arch,
                        # #318 external backend (no worker-agent / no container) —
                        # the UI hides container-lifecycle actions (logs/restart/
                        # unload can't work; they'd return null).
                        "external": bool((w.labels or {}).get("external")) if w is not None else False,
                    })
                ready = sum(1 for i in inst_list if i["status"] == "ready")
                # deployment-level rollup: the hardware of its first placed
                # instance (deployments are effectively 1:worker here), else the
                # engine kind alone drives the label.
                _dep_hw = next((i["hardware"] for i in inst_list if i["hardware"]), None)
                _dep_dev, _dep_arch = _runtime_meta(d.engine, _dep_hw)
                _health = _deployment_health(inst_list, ready, d.replicas)
                rows.append(
                    {
                        "id": str(d.id),
                        "model_name": d.model_name,
                        "display_name": d.display_name,  # #284 console-only label
                        "engine": d.engine,
                        "task": d.task,
                        "status": d.status,
                        # #286 ACTUAL health derived from instance phases (vs
                        # d.status = desired state). ready → all replicas serving;
                        # degraded → some; failed → engines died; loading → coming
                        # up; pending → nothing placed yet.
                        "health": _health,
                        # #1372: the node's reason, when an engine failed to load.
                        "last_error": _last_error_for(inst_list, _health, d),
                        "replicas": d.replicas,
                        "params": d.params or {},
                        # #549 R1: null = the node's default runner for its
                        # hardware class. The console's version picker (R4)
                        # reads this.
                        "runner_image": d.runner_image,
                        "tags": d.tags or [],
                        "device": _dep_dev,
                        "arch": _dep_arch,
                        "ready_instances": ready,
                        "instances": inst_list,
                    }
                )
            return rows

    @router.get("/api/stats")
    def stats():
        """#311 live performance snapshot (since process start) for the console
        Dashboard: global requests/latency/failover/rejects + per-model rollup."""
        from app.metrics import stats_json
        return stats_json()

    @router.get("/api/tags")
    def list_tags():
        """Central tag catalog = the distinct tags across all deployments
        (case-insensitive de-dupe, first spelling wins), sorted."""
        with session_scope() as s:
            seen: dict[str, str] = {}
            for (tags,) in s.query(Deployment.tags).all():
                for t in _norm_tags(tags):
                    seen.setdefault(t.lower(), t)
            return sorted(seen.values(), key=str.lower)

    @router.get("/api/inventory")
    def fleet_inventory():
        """#307 S3: per model known to the fleet — registry presence (Zot) +
        per-worker on-disk cache state. Reuses #289's catalog read
        (``_zot_has_repo``) and #306's per-worker disk-report channel
        (``_latest_disk_report``); see ``_fleet_inventory``'s docstring for
        the scope note on what isn't covered yet."""
        with session_scope() as s:
            return _fleet_inventory(s)

    @router.post("/api/deployments/{deployment_id}/evict")
    def evict_from_fleet(deployment_id: str, evict_registry: bool = False):
        """#307 S3 "remove from fleet": evict this deployment's weight files
        from every worker that has them cached, and — opt-in via
        ``?evict_registry=true``, the same query-param convention
        ``start_deployment``'s ``force`` uses — the central registry repo
        too. Keyed by ``deployment_id`` like its siblings
        (stop/start/reassign/apply-params above) — a `.get()` by primary key,
        not a second by-name lookup convention. See ``_evict_from_fleet``'s
        docstring for the dispatch rule."""
        with session_scope() as s:
            dep = s.get(Deployment, parse_uuid(deployment_id, "deployment_id"))
            if dep is None:
                raise HTTPException(status_code=404, detail="deployment not found")
            return _evict_from_fleet(s, dep, evict_registry=evict_registry)

    @router.post("/api/deployments")
    def deploy(payload: DeployRequest, request: Request):
        """#263 SCH1: create desired-state + place on a worker + enqueue a
        load_engine command. The worker's worker-agent picks it up, launches the
        engine, and self-registers the instance on its next report cycle."""
        # #265 ENT3: the SAME subscription entitlement gate as the inference
        # hot path (proxy.py ENT1), applied here so a lapsed/expired/absent
        # subscription cannot provision NEW capacity — checked before any
        # placement/enqueue work below (report mode is a no-op; enforce
        # 402s + counts the refusal).
        _entitlement_gate(_cache(request), "deploy new models")
        served = payload.served_model or payload.model_name
        # #1256: complete `files` with the model's vision projector BEFORE
        # anything reads it. Everything downstream consumes this list — the
        # engine resolution below, the desired-state row, the #307 registry
        # mirror and (via the node) the `--mmproj` flag — so completing it
        # here is what makes a deploy pull the model COMPLETE from every
        # caller: console one-click, console quant editor (whose file list
        # comes from the HF quant grouping and CANNOT know about a repo-level
        # companion), the CLI standard-set deploy, and a hand-rolled POST.
        # Idempotent, and a repo-DIRECTORY deploy (#574, files == []) is left
        # untouched. The filename itself is declared once, in the model
        # manifest — see app/model_manifest.py.
        payload.files = complete_files(
            payload.files, repo_id=payload.hf_repo, name=payload.model_name)
        # #1214: the same gate PATCH applies — shape AND registry-qualified.
        if payload.runner_image is not None:
            require_launchable_runner_image(payload.runner_image)
        # #284: deploy-time replica count — same bounds as PATCH's scale-up gate
        # (LLMM-5: literally the same function, so they cannot drift apart).
        _validate_replicas(payload.replicas)
        with session_scope() as s:
            now = datetime.now(timezone.utc)
            worker = _pick_worker(s, now, worker_id=payload.worker_id, hardware=payload.hardware)
            if worker is None:
                raise HTTPException(status_code=409, detail="no ready worker matches the placement")

            # #361: artifact format must match the target's engine, or the
            # deploy fails LATER as an opaque engine crash-loop. vLLM serves an
            # HF-format (safetensors) directory; every GGUF in the catalog and
            # HF browser is llama.cpp food. The fits-check compares sizes, not
            # formats — this is the missing hard rule (#334's "no compatible
            # artifact → reject"), enforced only when the mismatch is CERTAIN.
            # The resolved engine is what node-side select_driver keys on AND
            # what the guard inside `_resolve_dep_engine` checks (LLMM-6).
            # #1573: the operator named a repo and no file. Resolve the
            # quantisation FIRST, so that everything below — the engine
            # resolution, the door, the budget gate — judges the resolved
            # selection and cannot tell it from a hand-picked one.
            #
            # Before, not between: #1372 pins the door to sit directly after
            # the engine resolution, and that adjacency is worth keeping. It is
            # what makes "an empty file list never reaches the node" readable
            # in one glance.
            picked = None
            if payload.hf_repo and not payload.files:
                picked = _resolve_quantisation(
                    s, payload, worker,
                    _resolve_dep_engine([], (worker.labels or {}).get("engine"),
                                        payload.engine))
                payload.files = complete_files(
                    picked["files"], repo_id=payload.hf_repo,
                    name=payload.model_name)
                # The estimate belongs to the quantisation that was CHOSEN, so
                # it comes from the same computation rather than being
                # recomputed from the completed list — a projector file adds
                # weight the rule did not weigh, and re-deriving here would
                # quietly change the number the refusal messages were phrased
                # against.
                if payload.est_gb is None:
                    payload.est_gb = picked["est_gb"]

            dep_engine = _resolve_dep_engine(
                payload.files or [],
                (worker.labels or {}).get("engine"),
                payload.engine,
            )
            _require_llamacpp_weights(dep_engine, payload.files or [])

            # #1648: the admission gate below is inert without a number, and the
            # CLI path never sent one. Derive it here — AFTER the engine is
            # resolved, because the footprint depends on it, and BEFORE the gate,
            # because a number that arrives afterwards protects nothing.
            payload.est_gb = _derive_est_gb(payload, dep_engine)

            # #336: unique(models.name) makes the create race loud; the
            # caller-level retry pattern (workers.py) covers the report
            # path, and this deploy path is operator-serial in practice.
            model = s.query(Model).filter(Model.name == served).one_or_none()
            if model is None:
                model = Model(name=served)
                s.add(model); s.flush()
            dep = s.query(Deployment).filter(Deployment.model_name == payload.model_name).one_or_none()
            if dep is None:
                dep = Deployment(model_name=payload.model_name, model_id=model.id,
                                 engine=dep_engine,
                                 task=payload.task or "chat", params=payload.params or {},
                                 source_files=payload.files or [], hf_repo=payload.hf_repo,
                                 runner_image=payload.runner_image,
                                 replicas=max(1, payload.replicas),   # #284
                                 tags=_norm_tags(payload.tags), status="pending")
                s.add(dep); s.flush()
            else:
                dep.status = "pending"
                dep.task = payload.task or "chat"   # allow re-deploy to change serve mode
                dep.engine = dep_engine   # re-deploy may switch artifact type → engine
                # #298: remember the weight source so replicas can be scheduled.
                dep.source_files = payload.files or dep.source_files
                dep.hf_repo = payload.hf_repo or dep.hf_repo
                # #549 R1: re-deploy may repin the runner. `or` (not plain
                # assignment) matches hf_repo's convention: an omitted field
                # keeps the pin, it does not clear it — clearing is a PATCH
                # concern once the console grows the affordance.
                dep.runner_image = payload.runner_image or dep.runner_image
                dep.tags = _norm_tags(payload.tags)   # #296 re-deploy updates tags
                # #284: re-deploy may RAISE the desired count (never lower it here —
                # lowering is PATCH's job, same asymmetry as runner_image above).
                if payload.replicas:
                    dep.replicas = max(dep.replicas, max(1, payload.replicas))

            # #227 admission control: refuse a placement that would over-subscribe
            # the worker's VRAM budget (weights+KV spilling past the carveout into
            # host RAM is what thrashed the box). Enforced ONLY when both the
            # footprint estimate and the worker budget are known — never block on a
            # guess. force=true bypasses (logged).
            #
            # #330 stage 2 (review #709 should-fix): this used to be an INLINE
            # static committed-check that bypassed _budget_check — so the
            # dynamic used-based leg protected the auto-placement path but NOT
            # the explicit console deploy, which is exactly the 0.91 point-A
            # scenario (operator deploys 40 GB while GPUStack holds 58.7/96;
            # committed=0, static admits, double-booking). One gate, all paths.
            # The N-replica footprint rides in as est×N: for a single worker
            # that is equivalent to N pending-accumulated checks, and it keeps
            # the est_gb the 409 detail reports being the per-replica figure
            # times the count the operator asked for.
            if payload.est_gb and not payload.force:
                est_total = float(payload.est_gb) * max(1, dep.replicas or 1)
                fits, committed, projected, budget, dyn = _budget_check(
                    s, worker, est_total, exclude_dep_id=dep.id)
                if not fits:
                    if dyn and dyn.get("refused"):
                        # free_now is the honest remediation figure: the static
                        # budget can show plenty while the OTHER control plane
                        # holds the GPU right now.
                        detail = (
                            f"would over-subscribe {worker.name} RIGHT NOW: "
                            f"{est_total:.1f} GB requested > {dyn['free_now']:.1f} GB "
                            f"free (measured {dyn['used']:.1f} GB in use on the GPU "
                            f"across ALL schedulers, incl. e.g. GPUStack). Stop a "
                            f"model on either scheduler to free VRAM, pick another "
                            f"worker, or re-deploy with force enabled.")
                    else:
                        detail = _over_subscription_detail(
                            worker, est_total, committed, projected, budget,
                            "Stop a model to free VRAM, pick another worker, "
                            "or re-deploy with force enabled.")
                    raise HTTPException(status_code=409, detail=detail)
            if payload.est_gb is not None:
                dep.est_gb = float(payload.est_gb)  # remember for future admission math

            # #307 S1: mirror the model into the in-stack Zot registry BEFORE
            # placement when it isn't cached yet — exactly one mirror,
            # dispatched ahead of the engine so Slice 2's worker-side puller
            # has something to pull from the master instead of HF. A model
            # already cached dispatches zero mirrors and goes straight to
            # placement.
            mirrored = _auto_mirror_if_absent(s, worker, dep, hf_repo=dep.hf_repo,
                                              files=dep.source_files, served=served)

            # #294b: the helper creates the optimistic "scheduled" instance so the
            # deploy is visible from t+0 (see workers.py reconcile grace).
            #
            # #284: place the FIRST instance on the already-picked `worker`
            # (unchanged from pre-#284 behaviour — the explicit worker_id/hardware
            # selector always wins for replica 1), then place any remaining
            # replicas via the SAME budget-aware admission loop `start_deployment`
            # and `patch_deployment`'s scale-up already use (`_place_within_budget`)
            # — one placement/budget authority, not a second one reinvented here.
            now2 = datetime.now(timezone.utc)
            avoid: set[str] = {str(worker.id)}
            pending: dict[str, float] = {str(worker.id): float(payload.est_gb or 0)}
            instance_id = _enqueue_engine(s, dep, worker, served, payload.files,
                                          payload.hf_repo, payload.params, payload.task)
            scheduled = 1
            no_room = False
            for _ in range(max(0, max(1, payload.replicas) - 1)):
                w, reason = _place_within_budget(s, now2, dep=dep, est_gb=payload.est_gb,
                                                 hardware=payload.hardware, avoid=avoid,
                                                 pending=pending, force=payload.force)
                if w is None:
                    no_room = no_room or reason == "no_room"
                    break
                _enqueue_engine(s, dep, w, served, payload.files, payload.hf_repo,
                                payload.params, payload.task)
                avoid.add(str(w.id)); scheduled += 1
                pending[str(w.id)] = pending.get(str(w.id), 0.0) + float(payload.est_gb or 0)
            body = {"deployment_id": str(dep.id), "worker": worker.name,
                    "worker_id": str(worker.id), "instance_id": instance_id,
                    "scheduled_instances": scheduled, "admission_limited": no_room,
                    "status": "mirroring" if mirrored else "scheduled"}
            if picked is not None:
                # #1573: say WHAT was chosen and BY WHICH rule. A resolution the
                # operator cannot see is a decision taken behind their back —
                # and the whole point of resolving in the manager rather than on
                # the node was that the choice becomes visible before the
                # weights are 30 GB down the wire.
                body["resolved_quantisation"] = picked
            return body

    @router.post("/api/deployments/{deployment_id}/apply-params")
    def apply_params(deployment_id: str):
        """#566 (C3, operator decision: shape A): apply the CURRENT desired
        state (params / task / runner pin) to the RUNNING engines, now.

        PATCH edits desired state but the engines keep their launch-time flags
        until "the next (re)deploy" — this IS that redeploy, as orchestration
        over the existing command kinds: per live instance, unload the old
        engine and enqueue a fresh load built from the deployment row (the one
        params→CLI-flags authority; a node-side apply_params kind would be a
        second one, the two-authorities bug class). The claim endpoint orders
        by created_at, so each worker sees its unload before the new load.

        Brief downtime per instance is inherent and deliberate — same contract
        as the R3 relaunch. Instance rows are deleted here (the drain idiom);
        _enqueue_engine creates the fresh 'scheduled' rows and node reports
        take it from there. External backends have no container to relaunch.
        """
        # (#1187: no local `from app.models import Worker` here — `Worker` is
        # already imported at module scope; the redundant local import resolved
        # a RE-IMPORTED app.models under the test harness's module eviction and
        # made `s.get(Worker, …)` compare a different class object.)
        with session_scope() as s:
            dep = s.get(Deployment, parse_uuid(deployment_id, "deployment_id"))
            if dep is None:
                raise HTTPException(status_code=404, detail="deployment not found")
            _model = s.get(Model, dep.model_id) if dep.model_id else None
            served = _model.name if _model is not None else dep.model_name
            instances = (s.query(DeploymentInstance)
                         .filter(DeploymentInstance.deployment_id == dep.id).all())
            if not instances:
                raise HTTPException(status_code=409, detail=(
                    "no live instances — params apply on deploy; nothing to relaunch"))
            # #1045: honor `replicas` — relaunch at most `dep.replicas` instances
            # and DRAIN the surplus, instead of blindly relaunching every row. A
            # deployment can carry stale/extra instance rows beyond `replicas` (a
            # leftover failed placement); relaunching one that no longer fits the
            # worker's VRAM makes llama-server OOM (exit 134) and the
            # `unless-stopped` container crash-loops forever. Keep the healthiest
            # instances (ready first, then newest) up to the desired count; unload
            # + reap the rest so they don't get relaunched into a crash-loop.
            relaunched = _relaunch_live_instances(s, dep, served, instances)
            if relaunched == 0:
                raise HTTPException(status_code=409, detail=(
                    "only external instances — no containers to relaunch"))
            logger.info("apply-params: %s relaunching %d instance(s) (#566)",
                        dep.model_name, relaunched)
            return {"status": "applying", "deployment_id": str(dep.id),
                    "relaunched": relaunched}

    @router.post("/api/deployments/{deployment_id}/reconcile")
    def reconcile_deployment(deployment_id: str, body: DeploymentReconcile):
        """#1263: bring a MANIFEST-OWNED deployment back to the manifest.

        `post-install` deploys the standard set once and then skips every
        model that already has a deployment — right as a shield for console
        edits, but a manifest change (the #1058 batch sizes, a context size,
        a quantisation) never reached a box that had already deployed. PATCH
        would do it, except the only transport the admin tier trusts is the
        Caddy source-IP anchor, i.e. `docker exec caddy wget` — busybox, no
        PATCH. So this is the POST spelling of "PATCH params/task, then
        apply-params", with two differences that make it safe to run on
        every `--refresh --reconcile-models`:
        * it refuses deployments WITHOUT the `standard-set` tag (409) — the
          tag is the ownership marker; a console edit that should stick is
          made by removing the tag;
        * it is idempotent: when params and task already match, nothing is
          written and no engine is relaunched (`changed: false`).
        Zombies (tagged, no longer in the manifest) are reported by the CLI,
        never deleted here — VRAM is released by an operator decision.
        """
        with session_scope() as s:
            dep = s.get(Deployment, parse_uuid(deployment_id, "deployment_id"))
            if dep is None:
                raise HTTPException(status_code=404, detail="deployment not found")
            if MANIFEST_OWNED_TAG not in (dep.tags or []):
                raise HTTPException(status_code=409, detail=(
                    f"deployment {dep.model_name!r} is operator-owned (no "
                    f"{MANIFEST_OWNED_TAG!r} tag) — the manifest does not reconcile it"))
            new_params = dict(body.params or {})
            new_task = (body.task or dep.task or "chat")
            changed = (dict(dep.params or {}) != new_params) or ((dep.task or "chat") != new_task)
            if not changed:
                return {"status": "unchanged", "deployment_id": str(dep.id),
                        "task": dep.task, "params": dep.params or {},
                        "changed": False, "relaunched": 0}
            dep.params = new_params
            dep.task = new_task
            dep.updated_at = datetime.now(timezone.utc)
            s.flush()
            _model = s.get(Model, dep.model_id) if dep.model_id else None
            served = _model.name if _model is not None else dep.model_name
            instances = (s.query(DeploymentInstance)
                         .filter(DeploymentInstance.deployment_id == dep.id).all())
            relaunched = _relaunch_live_instances(s, dep, served, instances) if instances else 0
            logger.info("reconcile: %s params/task set from the manifest, relaunching %d instance(s) (#1263)",
                        dep.model_name, relaunched)
            return {"status": "reconciled", "deployment_id": str(dep.id),
                    "task": dep.task, "params": dep.params,
                    "changed": True, "relaunched": relaunched}

    @router.delete("/api/deployments/{deployment_id}")
    def undeploy(deployment_id: str):
        """Enqueue unload_engine to each instance's worker + mark the deployment
        removed. Instances disappear as the workers report post-unload."""
        with session_scope() as s:
            dep = s.get(Deployment, parse_uuid(deployment_id, "deployment_id"))
            if dep is None:
                raise HTTPException(status_code=404, detail="deployment not found")
            insts = s.query(DeploymentInstance).filter(
                DeploymentInstance.deployment_id == dep.id).all()
            enq = 0
            for di in insts:
                container = _container(di.endpoint)
                if di.worker_id and container:
                    enqueue_command(s, di.worker_id, "unload_engine",
                                    {"instance_id": container, "container": container})
                    enq += 1
            # #294: DELETE the deployment (cascades its instances) so it clears
            # from the console immediately — don't leave it stuck at "removing".
            # #298b: tombstone the name so the node's next registration (still
            # reporting this instance until it processes the unload) doesn't
            # RESURRECT the deployment — the "undeploy, still there" bug.
            from app.api.workers import tombstone_deployment
            model_name = dep.model_name
            s.delete(dep)
            tombstone_deployment(model_name)
            return {"deployment_id": deployment_id, "unload_commands": enq, "status": "removed"}

    @router.post("/api/deployments/{deployment_id}/stop")
    def stop_deployment(deployment_id: str):
        """#312: pause — unload the engines (free VRAM) but KEEP the deployment
        (config / params / tags / replica count) so it can be resumed without
        re-configuring. Instances are dropped; register_worker won't recreate
        them while status='stopped' (see workers.py)."""
        with session_scope() as s:
            dep = s.get(Deployment, parse_uuid(deployment_id, "deployment_id"))
            if dep is None:
                raise HTTPException(status_code=404, detail="deployment not found")
            enq = 0
            for di in s.query(DeploymentInstance).filter(
                    DeploymentInstance.deployment_id == dep.id).all():
                container = _container(di.endpoint)
                if di.worker_id and container:
                    enqueue_command(s, di.worker_id, "unload_engine",
                                    {"instance_id": container, "container": container})
                    enq += 1
                s.delete(di)   # drop the instance rows; the deployment stays
            dep.status = "stopped"
            dep.updated_at = datetime.now(timezone.utc)
            return {"deployment_id": deployment_id, "unload_commands": enq, "status": "stopped"}

    @router.post("/api/deployments/{deployment_id}/start")
    def start_deployment(deployment_id: str, force: bool = False):
        """#312: resume a stopped deployment — re-schedule its instances from the
        persisted weight source + params, no re-configuration needed."""
        with session_scope() as s:
            dep = s.get(Deployment, parse_uuid(deployment_id, "deployment_id"))
            if dep is None:
                raise HTTPException(status_code=404, detail="deployment not found")
            _blocked = unplaceable_reason(dep)
            if _blocked:
                raise HTTPException(status_code=409, detail=_blocked)
            now = datetime.now(timezone.utc)
            dep.status = "pending"
            served = (s.get(Model, dep.model_id).name if dep.model_id else dep.model_name)
            hw = (dep.worker_selector or {}).get("hardware")
            avoid: set[str] = set()
            # #335 resume was the largest hole: est_gb has been persisted since
            # migration 0011 precisely so the footprint is known here, but the gate
            # was never called — stop a model, let something else take the freed
            # VRAM, resume, and the box is over-subscribed with no refusal anywhere.
            pending: dict[str, float] = {}
            scheduled = 0
            no_room = False
            for _ in range(max(1, dep.replicas)):
                w, reason = _place_within_budget(s, now, dep=dep, est_gb=dep.est_gb,
                                                 hardware=hw, avoid=avoid,
                                                 pending=pending, force=force)
                if w is None:
                    no_room = no_room or reason == "no_room"
                    break
                _enqueue_engine(s, dep, w, served, dep.source_files, dep.hf_repo, dep.params, dep.task)
                avoid.add(str(w.id)); scheduled += 1
                pending[str(w.id)] = pending.get(str(w.id), 0.0) + float(dep.est_gb or 0)
            if scheduled == 0:
                if no_room:
                    raise HTTPException(status_code=409, detail=(
                        f"no worker has room to resume {dep.model_name} "
                        f"({float(dep.est_gb or 0):.1f} GB per replica): every ready worker would be "
                        f"over-subscribed. Stop a model to free VRAM, or resume with force=true."))
                raise HTTPException(status_code=409, detail="no ready worker to resume on")
            dep.updated_at = now
            return {"deployment_id": deployment_id, "scheduled_instances": scheduled,
                    "admission_limited": no_room, "status": "starting"}

    @router.post("/api/registry/mirror")
    def mirror_into_cache(payload: MirrorRequest, request: Request):
        """#307: cache a model in the master's Zot registry WITHOUT deploying it —
        enqueue a mirror_model command to a worker (the master), which fetches the
        weights from HF and pushes them into Zot. Workers later pull from there."""
        # #265 ENT3: same gate as deploy() — this route also PROVISIONS (pulls
        # weights + grows the shared registry cache), so it is the
        # "catalog-pull" half of ENT3, not read-only browse (#265).
        _entitlement_gate(_cache(request), "pull/mirror new models into the cache")
        if not payload.files:
            raise HTTPException(status_code=422, detail="files required — pick a quant to cache")
        with session_scope() as s:
            worker = _pick_worker(s, datetime.now(timezone.utc), worker_id=payload.worker_id)
            if worker is None:
                raise HTTPException(status_code=409, detail="no ready worker to run the mirror")
            repo = "models/" + _slug(payload.name or payload.hf_repo.split("/")[-1])
            tag = _slug(payload.tag or "latest")
            cmd = enqueue_command(s, worker.id, "mirror_model",
                                  {"hf_repo": payload.hf_repo, "files": payload.files,
                                   "repo": repo, "tag": tag})
            s.flush()
            return {"command_id": str(cmd.id), "worker": worker.name,
                    "repo": repo, "tag": tag, "status": "mirroring"}

    # #842 review (MED 2): SUPERADMIN, NOT the router's ADMIN default. This
    # route adds a SERVING fleet member (status "ready", bypassing the #419
    # external-approval gate) and folds an operator-named endpoint + api_key
    # into the LiteLLM router — a non-super ADMIN could point a "model" at
    # http://attacker/v1 and have user prompts routed/exfiltrated there. It is
    # federation-shaped (adds a fleet member), so it belongs with the
    # super-admin worker-lifecycle gate, not the model-lifecycle ADMIN tier.
    @router.post("/api/workers/external",
                 dependencies=[Depends(require_role(Role.SUPERADMIN))])
    def add_external_backend(payload: ExternalBackendRequest):
        """#307: register a PRE-EXISTING OpenAI-compatible endpoint (e.g. a Mac /
        box running Ollama) as an external backend — a SUPER-ADMIN console action
        (no node key; #842 keeps it at the worker-lifecycle tier). Discovers its
        models if not given; the manager folds it into the fleet router. No
        container is launched — the endpoint runs its own server, and we never
        touch it (its Ollama keeps serving whatever else)."""
        from app.api.workers import RegisteredModel, WorkerRegistration, register_worker
        ep = payload.endpoint.rstrip("/")
        models = [m for m in (payload.models or []) if m]
        if not models:
            try:
                import httpx
                # EXTERNAL endpoint (#307) — rides the operator's proxy policy, #1409
                with httpx.Client(timeout=6, trust_env=True) as c:
                    r = c.get(f"{ep}/models"); r.raise_for_status()
                    models = [m.get("id") for m in (r.json() or {}).get("data", []) if m.get("id")]
            except Exception as exc:
                raise HTTPException(status_code=502,
                                    detail=f"could not reach {ep}/models ({str(exc)[:120]}) — is it reachable from the manager? pass models explicitly to skip discovery")
        if not models:
            raise HTTPException(status_code=422, detail="no models found at the endpoint")
        import re as _re
        address = _re.sub(r"^https?://", "", ep).split("/")[0].split(":")[0]
        tasks = payload.tasks or {}
        reg = WorkerRegistration(
            name=payload.name, address=address, hardware=payload.hardware,
            engine=payload.engine, external=True,
            models=[RegisteredModel(model_name=m, served_model=m, endpoint=ep,
                                    status="ready", api_key=payload.api_key,
                                    task=(tasks.get(m) or _infer_task(m))) for m in models],
        )
        with session_scope() as s:
            result = register_worker(s, reg)
        try:  # route the new models immediately (best-effort; #308 reload picks it up)
            from app.router_config import rebuild_from_state
            rebuild_from_state()
        except Exception:
            pass
        return {"name": payload.name, "endpoint": ep, "models": models,
                "status": "registered", "worker_id": (result or {}).get("worker_id"),
                "instances": (result or {}).get("instances")}

    @router.patch("/api/deployments/{deployment_id}")
    def patch_deployment(deployment_id: str, patch: DeploymentPatch):
        """#284: edit a deployment's desired state — replicas (desired count),
        serve task, backend params, tags. These are NOT overwritten by node
        registration (unlike the client-facing model_name, whose rename needs a
        stable-id alias — tracked separately). params/task take effect on the next
        (re)deploy of the engines; replicas is the target the scheduler reconciles."""
        # #1187: validate the runner pin BEFORE touching the row, so a refused
        # patch is a pure 422 (nothing half-applied). Mirrors what the node will
        # enforce at launch (valid ref + allowed registry) so the operator gets
        # the failure here, naming the field, not in a command result later.
        # `model_fields_set` distinguishes an OMITTED key (leave the pin alone)
        # from an explicit `null` (clear it — back to the node's default).
        repin = "runner_image" in patch.model_fields_set
        if repin and patch.runner_image is not None:
            require_launchable_runner_image(patch.runner_image)   # #1214: one gate, both routes
        with session_scope() as s:
            dep = s.get(Deployment, parse_uuid(deployment_id, "deployment_id"))
            if dep is None:
                raise HTTPException(status_code=404, detail="deployment not found")
            if repin:
                # Persisted on the row; _enqueue_engine forwards dep.runner_image on
                # EVERY launch path, so the next (re)launch uses the new pin.
                dep.runner_image = patch.runner_image
            scheduled = 0
            no_room = False
            if patch.worker_id is not None:
                # A BODY field, so 422 — same convention as /reassign, and what
                # FastAPI would have produced had the field been typed `UUID`.
                # Checked HERE and not inside the placement loop: a malformed id
                # must not reach a state where earlier replicas are already
                # enqueued and the request then fails.
                parse_uuid(patch.worker_id, "worker_id", status_code=422)
            if patch.worker_id is not None and patch.replicas is None:
                # #1187's rule, applied to a field that would otherwise be
                # accepted and ignored: worker_id only means something while
                # replicas are being ADDED.
                raise HTTPException(status_code=422, detail=(
                    "worker_id only applies when replicas are raised — it names "
                    "where the NEW replicas go. Send it together with replicas, or "
                    "use POST /api/deployments/{id}/reassign to move a running one."))
            if patch.replicas is not None:
                # LLMM-5: the SAME bounds POST /api/deployments applies — the
                # upper cap was missing here, so a typo'd count drove an
                # unbounded placement loop through the identical code below.
                dep.replicas = _validate_replicas(patch.replicas)
                # #298: raising replicas SCHEDULES the missing instances (place +
                # enqueue load_engine), reusing the persisted weight source — it
                # was previously a no-op beyond the number, so a 2nd instance
                # never started. Count live (non-failed) instances as the current
                # count.
                now = datetime.now(timezone.utc)
                live = [i for i in s.query(DeploymentInstance)
                        .filter(DeploymentInstance.deployment_id == dep.id).all()
                        if i.status != "failed"]
                served = (s.get(Model, dep.model_id).name if dep.model_id else dep.model_name)
                hw = (dep.worker_selector or {}).get("hardware")
                # #1542: `avoid` stays as-is even with an explicit target —
                # `_pick_worker`'s worker_id branch never consults it, so a
                # second replica ON PURPOSE beside the first (isolation, a
                # rolling restart) is already reachable. Clearing it here would
                # be dead code that reads like a rule.
                avoid = {str(i.worker_id) for i in live if i.worker_id}
                # #335 dep.replicas has ALREADY been raised above, so _committed_gb
                # would charge the not-yet-placed replicas against every worker this
                # deployment already sits on. Exclude the deployment and re-seed its
                # real, currently-placed footprint per worker instead.
                pending: dict[str, float] = {}
                for i in live:
                    if i.worker_id:
                        pending[str(i.worker_id)] = (pending.get(str(i.worker_id), 0.0)
                                                     + float(dep.est_gb or 0))
                for _ in range(max(0, dep.replicas - len(live))):
                    w, reason = _place_within_budget(s, now, dep=dep, est_gb=dep.est_gb,
                                                     hardware=hw, avoid=avoid,
                                                     pending=pending, force=patch.force,
                                                     worker_id=patch.worker_id)
                    if w is None:
                        # #1542: a named target that cannot take the replica is an
                        # ERROR, not a partial scale-up. The operator asked for a
                        # specific box; reporting "placed 1 of 2" would hide that
                        # the one thing they specified did not happen.
                        if patch.worker_id:
                            detail = {
                                "no_room": (f"worker {patch.worker_id} has no VRAM room for "
                                            f"another replica of {dep.model_name!r} — free "
                                            f"capacity there, pick another worker, or re-send "
                                            f"with force"),
                                "relay_conflict": (f"worker {patch.worker_id} is a remote worker "
                                                   f"already serving a different model — the relay "
                                                   f"is single-engine per worker (#929 M1)"),
                            }.get(reason, f"worker {patch.worker_id} cannot take this replica "
                                          f"({reason})")
                            raise HTTPException(status_code=409, detail=detail)
                        no_room = no_room or reason == "no_room"
                        break
                    _enqueue_engine(s, dep, w, served, dep.source_files, dep.hf_repo, dep.params, dep.task)
                    avoid.add(str(w.id)); scheduled += 1
                    pending[str(w.id)] = pending.get(str(w.id), 0.0) + float(dep.est_gb or 0)
            if patch.task is not None:
                dep.task = (patch.task or "chat")
            # params = engine backend params (full replace); tags live in their OWN
            # column (never in params — a `--tags` CLI flag would crash the engine).
            if patch.params is not None:
                dep.params = dict(patch.params)
            if patch.tags is not None:
                dep.tags = _norm_tags(patch.tags)
            if patch.display_name is not None:
                # #284 review parity (defense-in-depth, mirrors the Phase-1
                # worker-rename #863 review): this label is serialized into the
                # UI and logs, so cap length and drop non-printable chars
                # before persisting — a log-injection / layout-break vector
                # even though it's ADMIN-only and React-escaped on render.
                dn = patch.display_name.strip()
                if len(dn) > 128:
                    raise HTTPException(status_code=422, detail="display_name too long (max 128)")
                dn = "".join(ch for ch in dn if ch.isprintable())
                dep.display_name = dn or None
            dep.updated_at = datetime.now(timezone.utc)
            return {"deployment_id": str(dep.id), "replicas": dep.replicas,
                    "task": dep.task, "params": dep.params,
                    "runner_image": dep.runner_image,
                    "tags": dep.tags or [], "scheduled_instances": scheduled,
                    "display_name": dep.display_name,
                    # #335 a scale-up that placed fewer replicas than asked because
                    # every worker was full must SAY so — the desired count is still
                    # recorded, so a silent 200 reads as "done".
                    "admission_limited": no_room, "status": "updated"}

    @router.post("/api/deployments/{deployment_id}/reassign")
    def reassign_deployment(deployment_id: str, patch: ReassignRequest):
        """#304: move a deployment's running instance to a different worker.

        Delete-worker (#594) already lets an operator remove a stale node; this
        is the other half — moving a LIVE deployment off its current worker
        without the undeploy/redeploy round-trip that drops the desired-state
        row for the duration.

        An instance is a container bound to a worker-agent, so "reassign" cannot
        be a bare foreign-key flip while the old container keeps running: it is
        unload-on-old + `_enqueue_engine`-on-new, the same idiom `apply_params`
        (#566) uses for an in-place relaunch on the SAME worker. The new
        instance row's `worker_id` is the moved column (#304 — no new schema);
        the old instance row is deleted, same as every other unload path here.
        """
        with session_scope() as s:
            dep = s.get(Deployment, parse_uuid(deployment_id, "deployment_id"))
            if dep is None:
                raise HTTPException(status_code=404, detail="deployment not found")
            target = s.get(Worker, parse_uuid(patch.worker_id, "worker_id", status_code=422))
            if target is None:
                raise HTTPException(status_code=404, detail="worker not found")
            if (target.labels or {}).get("external"):
                raise HTTPException(status_code=409, detail=(
                    f"{target.name} is an external backend — it has no worker-agent "
                    f"to command, so a container deployment cannot be reassigned onto it"))
            now = datetime.now(timezone.utc)
            if target.status != "ready" or not _is_fresh(target, now):
                raise HTTPException(status_code=409, detail=(
                    f"{target.name} is not a ready, reachable worker (status={target.status!r})"))

            live = [i for i in s.query(DeploymentInstance)
                    .filter(DeploymentInstance.deployment_id == dep.id).all()
                    if i.status != "failed"]
            if patch.instance_id:
                candidates = [i for i in live if i.instance_id == patch.instance_id]
                if not candidates:
                    raise HTTPException(status_code=404,
                                        detail="instance not found on this deployment")
                inst = candidates[0]
            elif len(live) == 1:
                inst = live[0]
            elif not live:
                raise HTTPException(status_code=409,
                                    detail="no live instance to reassign — deploy first")
            else:
                raise HTTPException(status_code=422, detail=(
                    f"{len(live)} live instances on {dep.model_name} — pass "
                    f"instance_id to say which one moves"))

            if str(inst.worker_id) == str(target.id):
                raise HTTPException(status_code=409,
                                    detail=f"already running on {target.name}")

            # #227/#335 admission gate: a target worker that cannot host this
            # deployment's footprint must refuse, exactly like deploy/patch/resume —
            # a reassign that over-subscribes the target is the same VRAM-thrash
            # bug in a new outfit.
            fits, committed, projected, budget, dyn = _budget_check(
                s, target, dep.est_gb, exclude_dep_id=dep.id)
            if not fits and not patch.force:
                if dyn and dyn.get("refused"):
                    detail = (
                        f"would over-subscribe {target.name} RIGHT NOW: "
                        f"{float(dep.est_gb or 0):.1f} GB requested > {dyn['free_now']:.1f} GB "
                        f"free (measured {dyn['used']:.1f} GB in use across ALL schedulers). "
                        f"Stop a model to free VRAM, pick another worker, or reassign "
                        f"with force=true.")
                else:
                    detail = _over_subscription_detail(
                        target, dep.est_gb or 0.0, committed, projected, budget,
                        "Stop a model to free VRAM, pick another worker, or reassign "
                        "with force=true.")
                raise HTTPException(status_code=409, detail=detail)

            served = (s.get(Model, dep.model_id).name if dep.model_id else dep.model_name)
            old_worker_id = inst.worker_id
            container = _container(inst.endpoint)
            if old_worker_id and container:
                enqueue_command(s, old_worker_id, "unload_engine",
                                {"instance_id": container, "container": container})
            s.delete(inst)
            s.flush()
            new_instance_id = _enqueue_engine(s, dep, target, served, dep.source_files,
                                              dep.hf_repo, dep.params, dep.task)
            logger.info("reassign: %s moved from worker %s to %s (#304)",
                        dep.model_name, old_worker_id, target.id)
            return {"deployment_id": str(dep.id), "instance_id": new_instance_id,
                    "worker_id": str(target.id), "worker": target.name,
                    "status": "reassigning"}

    app.include_router(router)
