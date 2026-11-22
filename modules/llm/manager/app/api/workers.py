# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Node → manager worker registration (#254 Phase-2 P2-B1).

A fleet node (llm-worker-agent), after launching its engines, POSTs what it is
now serving to ``POST /api/workers``. The manager upserts the Worker and, per
served model, ensures a Model + Deployment and upserts a DeploymentInstance
(one per worker+deployment) with the live endpoint + status. That makes the
fleet SELF-REGISTERING — no more hand-inserted rows (the Phase-1 gap).

AUTH: node→manager is MACHINE auth, not the Authentik/Caddy admin path (nodes
are remote boxes on the LAN, not transiting THIS box's Caddy). A node presents
``Authorization: Bearer <LLM_MANAGER_NODE_KEY>``. Fail-closed: if the manager
has no node key configured, registration is DISABLED (503) — never open.

The generated router config is NOT rewritten here in P2-B1 (that's P2-B2 —
registration will trigger a rebuild); this slice only persists fleet state.
"""
from __future__ import annotations

import hmac
import logging
import re
import time
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from app.authz import Role, require_role
from pydantic import BaseModel

from app.config import get_settings
from app.db import session_scope
from app.footprint import footprint_gb

logger = logging.getLogger("orchestrator.workers")


#: A deployment the manager learned about from a node's registration report
#: rather than from a deploy request (#1760). Such a row is created without a
#: weight source; since #3106 it learns its engine's weight FILE when the node
#: reports one (a leaf name on that node's volume, no repository). The tag is
#: what lets `unplaceable_reason` tell "this was never
#: startable on its own" from "somebody deployed this without a source", which
#: are two different conversations with the operator.
DISCOVERED_TAG = "discovered-from-node"


class RegisteredModel(BaseModel):
    model_name: str                       # client-facing (LiteLLM model_name)
    served_model: Optional[str] = None    # engine's own name; defaults model_name
    endpoint: str                         # e.g. http://inst-1:8080/v1
    status: str = "ready"
    detail: Optional[str] = None          # #287 phase detail (pulling %, fail reason)
    api_key: Optional[str] = None         # per-backend upstream key (P2-B3)
    instance_id: Optional[str] = None     # engine instance/container (Q2 keying)
    # #2393: decimal GB of the weight files on the node's volume — the one
    # footprint fact a node always has. Lets the manager record `est_gb` for a
    # deployment created without one (offline install). Optional: old nodes omit.
    weights_gb: Optional[float] = None
    task: Optional[str] = None            # #318 serve mode: chat|embed|rerank (external backends set it explicitly)
    # #2868: the serve mode a node READ from an engine it did not launch for the
    # master (its argv). Used only to type a row the master discovers; unlike
    # `task` it never retypes a deployment someone chose. Unknown values are ignored.
    observed_task: Optional[str] = None
    # #3106: the weight files the engine was started with, as leaf names on the
    # node's models volume (the container's `rzfz.files` label, or its
    # `--model` path for an engine started by hand). Used only to give a row
    # the master DISCOVERED a weight source, so a runner upgrade can start it
    # again. Old nodes omit it.
    files: Optional[list] = None
    # The engine build this instance runs, read by the node from the running
    # engine (llama.cpp /props `build_info`). Absent from older agents.
    engine_version: Optional[str] = None
    # #3106: the image this engine's container runs (what it was started
    # from). A row with no runner pin runs the node's default, which moves
    # with the agent, so this is the only record of what a rollback must go
    # back to. Absent from older agents.
    runner_image: Optional[str] = None


def _instance_runner_image(m) -> Optional[str]:
    """The reported container image, if it is a well-formed image reference.
    Worker-controlled input that a rollback later sends back in a
    `load_engine`, so it is held to the same shape the upgrade API accepts."""
    v = getattr(m, "runner_image", None)
    if not isinstance(v, str) or not v.strip():
        return None
    from app.api.inventory import valid_runner_image
    v = v.strip()
    return v if valid_runner_image(v) else None


#: #3106: bounds on what a node may hand the master as an adopted engine's
#: weight files. Worker-controlled input that is later sent back in a
#: `load_engine`, so it is held to the shape the node itself writes: flat leaf
#: names on the models volume.
ADOPTED_FILES_CAP = 16
_ADOPTED_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")


def adoptable_weight_files(files) -> Optional[list]:
    """The node-reported files worth recording on a discovered row, or None.

    None unless at least one is a .gguf that is not a projector — that is
    what a relaunch needs (`runner_upgrade.relaunch_source_problem`). Entries
    that are not plain leaf names are dropped, never repaired."""
    if not isinstance(files, list):
        return None
    out = [f for f in files[:ADOPTED_FILES_CAP]
           if isinstance(f, str) and _ADOPTED_FILE_RE.match(f) and ".." not in f]
    if not any(f.lower().endswith(".gguf") and "mmproj" not in f.lower() for f in out):
        return None
    return out


#: An engine build string is short ("b12110-cd959bae9"); the cap bounds what a
#: misbehaving node can make the manager store per instance.
ENGINE_VERSION_MAX = 120


def _instance_engine_version(m) -> Optional[str]:
    """The reported per-instance engine build, printable and capped, or None."""
    v = getattr(m, "engine_version", None)
    if not isinstance(v, str):
        return None
    v = "".join(ch for ch in v.strip() if ch.isprintable())[:ENGINE_VERSION_MAX]
    return v or None


def _observed_task(m) -> Optional[str]:
    """#2868: the node's reading of an engine's serve mode, or None for anything
    that is not one of the three modes."""
    t = (getattr(m, "observed_task", None) or "").strip().lower()
    return t if t in ("chat", "embed", "rerank") else None


#: #2530: an image carries a handful of repo digests at most; the cap only
#: bounds what a misbehaving node can make the manager store per heartbeat.
AGENT_DIGESTS_CAP = 32

#: #2559: the Ollama release a Mac worker's running bundle pins, as the worker
#: reports it (`ollama_pinned`). Worker-controlled input that later becomes the
#: argument of the Mac's `ollama-switch.sh` and is shown in the console, so it
#: is stored only when it is exactly this shape — the SAME pattern the node
#: applies before it spawns the script (node-agent native_runtime.OLLAMA_VERSION;
#: tests/unit/llm-manager/test_2559_ollama_pin_pattern.py keeps the two equal).
OLLAMA_VERSION_PATTERN = r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:-rc[0-9]{1,3})?"
OLLAMA_VERSION = re.compile(OLLAMA_VERSION_PATTERN)


class WorkerRegistration(BaseModel):
    name: str
    address: str
    hardware: Optional[str] = None
    engine: Optional[str] = None
    # #254 D2: a node declares its role. Default "worker". A box that also runs
    # a control plane (manager/master) must NOT register as another manager's
    # worker (no nested/chained masters). Request-only — never persisted.
    role: Optional[str] = "worker"
    # Reported for the console's Fleet detail (Slice A): node's razzfazz.ai
    # stack version + the engine's llama.cpp build string. Stored in labels.
    stack_version: Optional[str] = None
    # #1932: the provenance of stack_version, from STACK_VERSION_SOURCE.
    stack_version_source: Optional[str] = None
    engine_version: Optional[str] = None
    # #1932: the reason an engine_version is absent, as a CODE from the
    # node's ENGINE_VERSION_WHY vocabulary. Accepted but never trusted as a
    # value to act on blindly — it is a report from the node, like the rest of
    # this payload.
    engine_version_why: Optional[str] = None
    # #2530: the hub manifest digest (sha256:…) of the worker-agent image the
    # node runs, derived by the agent from its own container. None = unknown.
    agent_image_digest: Optional[str] = None
    # ... and every hub digest of that image (one image id can carry several);
    # "current" means the hub's digest is among them. Absent from older agents.
    agent_image_digests: Optional[list[str]] = None
    # #262 host-routable addressing: the worker's own reachable base (e.g.
    # http://10.0.0.5:PORT). For a REMOTE worker its engine endpoints are only
    # container-DNS names on ITS docker network — unreachable from the master;
    # advertise_addr is where the master can actually reach that worker. Stored
    # in labels; the endpoint rewrite that consumes it lands with the live
    # node-runtime port-publishing (a later slice). None → same-box worker,
    # container DNS already routes.
    advertise_addr: Optional[str] = None
    # #1535: does this node's relay pick the engine by the model the request
    # names? Only then may the master place a SECOND model on it — see
    # inventory._assert_relay_single_engine. Absent/False on an agent older
    # than #1535, which still answers every relayed request from its first
    # ready engine.
    relay_model_routing: Optional[bool] = None
    # #2521 (fix round 1): does this node's relay serve
    # `/relay/{worker}/e/{engine}/v1` from exactly that engine? Only then does
    # the router config give each replica on this worker its own relay URL —
    # an older agent would append `/e/...` to the engine URL and 404.
    relay_engine_routing: Optional[bool] = None
    # #295 fits-check: memory a model may use on this worker. mem_total_gb = host
    # RAM; vram_total_gb = best-effort GPU VRAM (the honest ceiling on unified-
    # memory boxes). Stored in labels; consumed by the HF browser fits-check.
    mem_total_gb: Optional[float] = None
    vram_total_gb: Optional[float] = None
    # #330 stage 2: LIVE VRAM usage (GB) as read from sysfs at report time —
    # aggregates every control plane on the GPU, not just our own engines. Also
    # the console dashboard load graph's source (surfaced via labels["metrics"]).
    vram_used_gb: Optional[float] = None
    # live utilization for the dashboard load graph (sampled each report).
    load: Optional[float] = None
    gpu_util: Optional[float] = None
    mem_used_gb: Optional[float] = None
    ncpu: Optional[int] = None
    # #307: a PRE-EXISTING external endpoint (e.g. a Mac / box running Ollama).
    # It does NOT run our worker-agent and does NOT heartbeat, so it must not be
    # marked "stale", and placement must never schedule a container deploy onto
    # it (it only serves the models it was registered with).
    external: Optional[bool] = None
    # #1634/#1640: the external backend is being RETIRED (its profile was
    # switched off). Honoured only with external=True and an EMPTY model list:
    # the manager then deletes the worker row and every deployment row that
    # existed only because of it, and remembers the retirement so a later
    # registration of that backend is refused (409) until one arrives with
    # `retired: false` — the deliberate re-federation. Request-only.
    retired: Optional[bool] = None
    # #2512: the node asks to be held out of placement — a `fleet` master's
    # own worker (LLM_MASTER_ROLE=fleet: the master hosts NO models). A ready
    # worker reporting it is drained (drain_worker: engines unloaded, rows
    # gone); a new one is created draining. One-way by design: a report
    # WITHOUT it never undrains (#261-C2 — only POST .../undrain does).
    draining: Optional[bool] = None
    # This agent sends the liveness beat (app/liveness.py). A report that does
    # not say so ends any earlier judgement: its reports alone come far too
    # rarely to be judged by the beat's window.
    liveness_beat: Optional[bool] = None
    # #2559: the worker runs OUR agent in front of a loopback Ollama (the Mac
    # thin worker, node-agent native_runtime.py). Such a worker heartbeats,
    # claims commands and is placeable — it is NOT the #307 external backend
    # that a bare `engine: ollama` registration still means.
    agent_managed: Optional[bool] = None
    # #2559: the Ollama version the worker's running bundle pins (pins.json).
    # The only version `POST /api/workers/{id}/runtime-upgrade` will install.
    ollama_pinned: Optional[str] = None
    models: list[RegisteredModel] = []


class WorkerMetrics(BaseModel):
    """#1619: the four dashboard numbers and nothing else.

    A full `WorkerRegistration` carries the node's identity, its whole instance
    table and its image pins, and every POST of one rebuilds the LiteLLM router
    config. That is right once per report cycle and wrong several times a
    minute, which is what the console needs for a live chart — hence a body
    that is five optional floats.
    """
    vram_used_gb: Optional[float] = None
    load: Optional[float] = None
    gpu_util: Optional[float] = None
    mem_used_gb: Optional[float] = None
    ncpu: Optional[int] = None


class WorkerPatch(BaseModel):
    # #284: manager-owned display label. None or "" clears back to the
    # registered name.
    display_name: Optional[str] = None


# #254 D2 — roles that identify a control plane; rejected at worker registration
# (a manager/master may never enroll as a worker of another manager → no nested
# masters; keeps metering single-source + scheduling authoritative).
MANAGER_ROLES = frozenset({"manager", "master", "control-plane", "orchestrator"})

# #298b: undeploy tombstones. Self-registration re-creates a deployment for any
# model a node reports — great for fleet self-registration, but it RESURRECTS a
# model the operator just undeployed (the node keeps reporting the instance for a
# cycle or two until it processes the unload). A short tombstone makes
# registration SKIP a just-undeployed model_name so undeploy actually sticks; it
# expires so a genuine later re-deploy of the same name still registers.
_UNDEPLOY_TTL = 150.0
_undeploy_tombstones: dict[str, float] = {}


def _unloading_instance_ids(session, worker) -> set:
    """#2953: the instance ids with an unload_engine still pending/claimed on
    this worker."""
    from app.models import NodeCommand

    out: set = set()
    for c in (session.query(NodeCommand)
              .filter(NodeCommand.worker_id == worker.id,
                      NodeCommand.kind == "unload_engine",
                      NodeCommand.status.in_(("pending", "claimed"))).all()):
        a = c.args or {}
        for k in ("instance_id", "container"):
            if a.get(k):
                out.add(a[k])
    return out


def drain_worker(s, worker) -> int:
    """#261-C2 the drain operation itself: flip to draining, unload every engine,
    delete the instance rows. Module-level so the drain ROUTE and the runner-
    upgrade sequence (#549 R3) share one implementation — two drains that could
    diverge is the two-authorities bug on the operation axis.

    Caller has already validated the worker (exists, not pending, not external).
    Idempotent at the operation level: draining a draining worker enqueues
    nothing (its instance rows are already gone).
    """
    from app.api.commands import enqueue_command
    from app.api.inventory import _engine_name
    from app.models import DeploymentInstance

    worker.status = "draining"
    unloaded = 0
    for di in (s.query(DeploymentInstance)
               .filter(DeploymentInstance.worker_id == worker.id).all()):
        container = _engine_name(di)
        if container:
            enqueue_command(s, worker.id, "unload_engine",
                            {"instance_id": container, "container": container})
            unloaded += 1
        else:
            # #2953: an instance that cannot be named (no container, no reported
            # instance id) has had nothing unloaded; it is not counted as such.
            logger.warning("worker %s draining: instance %s (%s) has no identity "
                           "to unload by; not counted (#2953)", worker.name, di.id, di.endpoint)
        s.delete(di)
    logger.info("worker %s draining: %d engine unload(s) enqueued (#261-C2)",
                worker.name, unloaded)
    return unloaded


#: #2502 fix round 2 (finding C) — how long a retiring row may wait once its
#: NAMED replacement (`replaced_by_instance_id`) is stuck in a non-terminal
#: status (`scheduled`/`pulling`/…) AND that replacement's OWN worker has gone
#: stale or vanished, before the mark is abandoned and the source keeps
#: serving. Long and DELIBERATELY conditional on worker staleness, never a
#: fixed wall-clock alone: round 1's unconditional 900s clock could fire on a
#: legitimately slow-but-alive multi-GB pull (fix round 2, finding B — the
#: node reports `pulling` within seconds of claiming the command, so a
#: "still `scheduled`" check round 1 relied on never held for long enough to
#: matter). This clock exists only for finding C: a target worker that goes
#: offline (or is deleted) before ever reporting again would otherwise leave
#: the mark — and therefore the source instance — stuck forever, refusing
#: every later reassign with a 409. 0 disables it (never abandon).
_STALE_REPLACEMENT_ABANDON_SECONDS_DEFAULT = 6 * 3600.0


def _stale_replacement_abandon_seconds() -> float:
    import os
    raw = os.environ.get("LLM_MANAGER_STALE_REPLACEMENT_ABANDON_S")
    if raw is None or raw.strip() == "":
        return _STALE_REPLACEMENT_ABANDON_SECONDS_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("ignoring unparseable LLM_MANAGER_STALE_REPLACEMENT_ABANDON_S=%r", raw)
        return _STALE_REPLACEMENT_ABANDON_SECONDS_DEFAULT


def _replacement_worker_stale_or_gone(session, replacement, now) -> bool:
    """True when the worker hosting `replacement` is gone (row deleted) or has
    not heartbeated within the staleness window (`api/inventory.py::_is_fresh`
    — same predicate placement already uses to decide a worker is unreachable).
    The one signal `_stale_replacement_abandon_seconds`'s clock is gated on:
    a target that is merely SLOW (its worker still heartbeating) must never be
    abandoned, however long the pull takes (fix round 2, finding B)."""
    from app.api.inventory import _is_fresh
    from app.models import Worker

    worker_id = getattr(replacement, "worker_id", None)
    if worker_id is None:
        return True
    worker = session.get(Worker, worker_id)
    if worker is None:
        return True
    return not _is_fresh(worker, now)


def _replacement_stale_since(session, replacement):
    """#2536 item 6 - when the replacement's worker BECAME stale: its last
    heartbeat plus the staleness window (`_is_fresh`'s own bound). A worker
    row that is gone, or never heartbeated, leaves `started_at` as the only
    anchor. `started_at` alone measured the PULL: a worker quiet for ten
    seconds under a six-hour pull read as stale for six hours."""
    import datetime as _dt

    from app.api.inventory import _WORKER_STALE_SECONDS
    from app.models import Worker

    worker_id = getattr(replacement, "worker_id", None)
    worker = session.get(Worker, worker_id) if worker_id is not None else None
    hb = getattr(worker, "last_heartbeat", None) if worker is not None else None
    anchor = getattr(replacement, "started_at", None)
    anchor = _aware(anchor) if anchor is not None else None
    if hb is not None:
        since = _aware(hb) + _dt.timedelta(seconds=_WORKER_STALE_SECONDS)
        # a heartbeat older than the move itself must not backdate the clock
        # to before the move existed (#2536 M3)
        return max(since, anchor) if anchor is not None else since
    return anchor


def _cancel_paired_load(session, replacement, now, reason: str) -> int:
    """#2536 item 7 - the finding-C fallback frees the source; the target's
    `load_engine` must not stay `pending`, or a worker that comes back claims
    it and starts an engine nothing pairs any more. Only PENDING commands for
    exactly this instance_id: a CLAIMED one is #1633's expiry's to end."""
    from app.models import NodeCommand

    n = 0
    # skip_locked mirrors the claim path (commands.py): a row a concurrent
    # claim holds is not ours to overwrite.
    for c in (session.query(NodeCommand)
              .filter(NodeCommand.worker_id == replacement.worker_id,
                      NodeCommand.kind == "load_engine",
                      NodeCommand.status == "pending")
              .with_for_update(skip_locked=True).all()):
        if (c.args or {}).get("instance_id") == replacement.instance_id:
            c.status = "failed"
            c.finished_at = now
            c.result = {"error": f"cancelled: {reason}", "cancelled": True}
            n += 1
    claimed = any(
        (c.args or {}).get("instance_id") == replacement.instance_id
        for c in session.query(NodeCommand).filter(
            NodeCommand.worker_id == replacement.worker_id,
            NodeCommand.kind == "load_engine",
            NodeCommand.status == "claimed").all())
    replacement.status = "failed"
    replacement.detail = f"{_CANCELLED_REPLACEMENT_PREFIX}{reason}" + (
        " (a load already claimed by the worker may still finish)" if claimed else "")
    return n


#: #2617: what `_cancel_paired_load` writes into the replacement row's detail.
#: A row that carries it was failed BY the stale-replacement fallback, which
#: also freed the source's retiring mark: that engine was never meant to serve.
_CANCELLED_REPLACEMENT_PREFIX = "cancelled: "


def _is_cancelled_replacement(inst) -> bool:
    return (inst.status == "failed"
            and (inst.detail or "").startswith(_CANCELLED_REPLACEMENT_PREFIX))


def _unload_cancelled_replacement(session, worker, inst) -> bool:
    """#2617: a load the worker had already CLAIMED when the stale-replacement
    fallback cancelled the pair may finish after the worker comes back. Its
    engine then serves next to the source as an unpaired surplus, and observe
    mode (the shipped default) has no reaper. The worker's next report says it
    is running: unload it here, independent of ORCH_RECONCILE. The row stays
    `failed` (the audit trail) until the worker stops reporting it. One unload
    is enqueued per container, not one per heartbeat. True when one was enqueued.
    """
    from app.api.commands import enqueue_command
    from app.api.inventory import _engine_name
    from app.models import NodeCommand

    container = _engine_name(inst)
    if not container:
        return False
    for c in (session.query(NodeCommand)
              .filter(NodeCommand.worker_id == worker.id,
                      NodeCommand.kind == "unload_engine",
                      NodeCommand.status.in_(("pending", "claimed"))).all()):
        if (c.args or {}).get("container") == container:
            return False
    enqueue_command(session, worker.id, "unload_engine",
                    {"instance_id": container, "container": container})
    logger.warning("worker %s reports %s, a replacement cancelled by the "
                   "stale-replacement fallback; unloading it (#2617)",
                   worker.name, container)
    return True


#: Statuses that mean "this replacement is never going to become ready" —
#: `_retire_superseded_instances` clears the paired retiring row's mark the
#: instant it sees one of these, rather than waiting on any clock.
#:
#: #2502 fix round 3: `lost` deliberately does NOT belong here, even though it
#: reads like a dead end. `lost` is set only by `reconciler.py::_mark_lost`
#: (enforce mode), the instant a worker's reports lapse for 90s — and it is
#: REVIVABLE: `register_worker` flips a `lost` row straight back to whatever
#: status the next report says (`revived_at` records the transition, #2509 fix
#: round 2). A replacement that simply misses a couple of report cycles during
#: a long pull goes `lost` and would have been treated as "never coming back"
#: — clearing the source's mark on a status string, then the target quietly
#: revives and finishes loading, and the reconciler's own SURPLUS reap keeps
#: the OLDER (source, no longer marked) instance and unloads the NEWER
#: (target) one: finding B all over again, just reached through `lost`
#: instead of a wall-clock. `lost` therefore falls through to the "still in
#: flight" branch below, where ONLY the bounded, worker-staleness-gated clock
#: may abandon it — which is the right call anyway: a `lost` replacement's
#: worker is, by construction, the one that stopped reporting.
_REPLACEMENT_DEAD_STATUS = frozenset({"failed"})


def _clear_retiring_mark(session, di, *, message: str) -> None:
    """Un-mark `di` (it keeps serving) and, best-effort, record why on its
    deployment's `last_error`. The one place BOTH `_retire_superseded_instances`
    (registration-reported failures, a stale/gone replacement worker) and
    `commands.py::_mark_load_failed` (a command-reported failure) release a
    retiring row, so the two paths cannot drift on what they clear or how they
    word it (#2502 fix round 2, finding 5 + the registration out-of-scope note)."""
    from app.models import Deployment

    di.retiring_since = None
    di.replaced_by_instance_id = None
    dep = session.get(Deployment, di.deployment_id)
    if dep is not None:
        dep.last_error = message


def _retiring_sibling_paired_with(session, instance):
    """The retiring row (same deployment) whose EXPLICIT
    `replaced_by_instance_id` names `instance`, or ``None``.

    #2502 fix round 2: no recency/worker heuristics any more (round 1's
    `_reassign_pair_candidates`/the old version of this function) — a straight
    lookup on the column `_enqueue_engine` set when `instance`'s row was
    created to replace exactly this one. Used by `commands.py::_mark_load_failed`
    to clear the ONE retiring mark a failed target load actually corresponds
    to, never every retiring row the deployment happens to have.

    `getattr(..., None)` and `.all()` + a Python `next()` rather than `.first()`
    throughout: exercised by
    tests/unit/llm-manager/test_1372_node_rejection_reaches_the_deployment.py
    against a hand-built `SimpleNamespace`/fake-query double (only
    `filter().all()`, no `.first()`) that carries only
    `instance_id`/`deployment_id`/`worker_id`/`status`/`detail` — a plain
    failed deploy has nothing to do with a reassign/overlap, so "this test
    double doesn't even model instance_id" and "there is nothing to pair" are
    the same fact, not an error.
    """
    from app.models import DeploymentInstance

    iid = getattr(instance, "instance_id", None)
    if not iid:
        return None
    dep_id = getattr(instance, "deployment_id", None)
    candidates = (session.query(DeploymentInstance)
                 .filter(DeploymentInstance.deployment_id == dep_id).all())
    return next((r for r in candidates
                if getattr(r, "replaced_by_instance_id", None) == iid), None)


def _retire_superseded_instances(session, worker, now) -> int:
    """#2019 — the second half of the blue-green `apply-params` relaunch, and
    (#2502) the same half for a `reassign`'s make-before-break move.

    `_overlap_relaunch` (apply-params) and `reassign_deployment` (#2502) both
    mark an OLD instance `retiring_since`, record the NEW instance's id on
    `replaced_by_instance_id` (fix round 2 — see that column's comment in
    `models.py`), and start the new engine alongside the old one. The old one
    keeps serving, so the model never leaves `model_list` and no request sees
    `404 no ready … engine`. This runs on EVERY node report and decides each
    retiring row's fate from its NAMED replacement, and ONLY that row:

    * **`ready`** → retire the source (unload + delete): the replacement has
      proven itself.
    * **`failed`** → clear the mark (`_clear_retiring_mark`) and record why on
      `dep.last_error`, worded by which operation this was (same worker as
      the source = the #2019 apply-params overlap; a different worker = the
      #2502 reassign). This also covers a pull failure that reaches the
      manager through a node's ORDINARY registration report rather than a
      `POST /api/commands/.../result` — `register_worker` upserts
      `inst.status = m.status` earlier in the SAME request, so by the time
      this function runs the replacement row already reads `failed`.
    * **`lost` or anything else in flight** (`scheduled`, `pulling`,
      `loading`, …) → wait, UNLESS the replacement's own worker has gone
      stale or vanished (`_replacement_worker_stale_or_gone`) AND its age
      exceeds `_stale_replacement_abandon_seconds` (default 6h) — the bounded
      fallback for finding C: a target worker that goes offline before ever
      claiming its `load_engine` command would otherwise leave the row
      `scheduled` (no result, no #1633 claim to expire) forever, and the mark
      with it. #2502 fix round 3: `lost` is deliberately NOT treated as dead
      — `reconciler.py::_mark_lost` sets it after just 90s without a report,
      and it is REVIVABLE the instant the worker reports again. Clearing the
      mark on `lost` would let a target that merely missed a couple of report
      cycles during a long pull revive and finish loading with NO mark on the
      source — the reconciler's own SURPLUS reap then keeps the older
      (source) instance and unloads the newer (target) one: finding B again,
      reached through a status string instead of a wall-clock. A `lost`
      replacement's own worker is, by construction, the one that stopped
      reporting, so it is exactly the shape the bounded clock exists for.
    * **the replacement row is simply gone** (pruned, never existed) → same
      as `failed` — it is never coming back either.

    #2502 fix round 2 removed round 1's recency/worker-preference heuristics
    over "any ready, non-retiring sibling of the same deployment": they could
    not tell a PRE-EXISTING replica (replicas > 1) from the actual
    replacement, nor one concurrent overlap/reassign's replacement from
    another's on the same deployment — both mispaired and retired (or kept
    waiting on) the wrong row. Reading the EXPLICIT pointer instead makes
    "whose replacement is this" a fact recorded by the one party that knows
    it (the enqueuing code), not a guess reconstructed from timing.

    Never raises into the report path; the caller wraps it.
    """
    from app.api.commands import enqueue_command
    from app.api.inventory import _engine_name
    from app.models import DeploymentInstance

    retiring = (session.query(DeploymentInstance)
                .filter(DeploymentInstance.retiring_since.isnot(None)).all())
    if not retiring:
        return 0
    budget = _stale_replacement_abandon_seconds()
    retired = 0
    for di in retiring:
        replaced_by = di.replaced_by_instance_id
        replacement = None
        if replaced_by:
            replacement = (session.query(DeploymentInstance)
                          .filter(DeploymentInstance.deployment_id == di.deployment_id,
                                  DeploymentInstance.instance_id == replaced_by)
                          .first())
        if replacement is None:
            _clear_retiring_mark(
                session, di,
                message="target instance disappeared before completing the move")
            logger.warning(
                "overlap/move for deployment %s (worker %s) abandoned — its "
                "named replacement %r no longer exists (#2019/#2502)",
                di.deployment_id, di.worker_id, replaced_by)
            continue
        op = ("apply-params relaunch" if replacement.worker_id == di.worker_id
              else "reassign")
        if replacement.status == "ready":
            container = _engine_name(di)
            if container:
                enqueue_command(session, di.worker_id, "unload_engine",
                                {"instance_id": container, "container": container})
            session.delete(di)
            retired += 1
            logger.info("overlap/move complete for deployment %s: named "
                        "replacement %s is ready on worker %s, retiring the "
                        "superseded engine %s on worker %s (#2019/#2502)",
                        di.deployment_id, replacement.instance_id, replacement.worker_id,
                        container or di.id, di.worker_id)
            continue
        if replacement.status in _REPLACEMENT_DEAD_STATUS:
            reason = replacement.detail or f"target reported {replacement.status}"
            _clear_retiring_mark(session, di, message=f"{op} target load failed: {reason}")
            logger.warning(
                "%s for deployment %s (worker %s) abandoned — its named "
                "replacement %s is %s: %s (#2019/#2502)",
                op, di.deployment_id, di.worker_id, replacement.instance_id,
                replacement.status, reason)
            continue
        # still in flight (scheduled/pulling/loading/…): only a stale/gone
        # replacement WORKER starts the bounded fallback clock at all — see
        # `_stale_replacement_abandon_seconds`'s docstring for why a fixed
        # wall-clock alone (round 1) was wrong.
        if budget and _replacement_worker_stale_or_gone(session, replacement, now):
            # #2536 item 6: the clock runs from when that worker went stale,
            # not from started_at (the start of the pull).
            since = _replacement_stale_since(session, replacement)
            age = (now - since).total_seconds() if since is not None else None
            if age is not None and age > budget:
                reason = (f"{op} target worker went offline before "
                          f"completing the move (stale for {age:.0f}s)")
                _clear_retiring_mark(session, di, message=reason)
                cancelled = _cancel_paired_load(session, replacement, now, reason)
                logger.warning(
                    "%s for deployment %s (worker %s) abandoned after %.0fs - "
                    "its named replacement %s's worker went stale/gone; %d pending "
                    "load_engine cancelled (#2019/#2502/#2536)", op, di.deployment_id,
                    di.worker_id, age, replacement.instance_id, cancelled)
    session.flush()
    return retired


def tombstone_deployment(model_name: str) -> None:
    # #329 Entries were only ever removed LAZILY, when that exact model_name was
    # looked up again after expiry — so a model undeployed and never re-reported
    # stayed in the dict for the life of the process. Sweeping on insert bounds
    # the dict by the number of models undeployed within one TTL window, which is
    # what it was always meant to be.
    now = time.monotonic()
    for name in [n for n, exp in _undeploy_tombstones.items() if exp <= now]:
        _undeploy_tombstones.pop(name, None)
    _undeploy_tombstones[model_name] = now + _UNDEPLOY_TTL


def _tombstoned(model_name: str) -> bool:
    exp = _undeploy_tombstones.get(model_name)
    if exp is None:
        return False
    if time.monotonic() > exp:
        _undeploy_tombstones.pop(model_name, None)
        return False
    return True


def _retired_key(name: str) -> str:
    return f"retired_external:{name}"


def retire_external_worker(session, name: str) -> dict:
    """#1634/#1640: forget an external backend whose runtime left the box.

    An empty-model registration prunes the backend's INSTANCES; this removes
    what the prune leaves: the worker row (external rows never turn `stale`, so
    it would read `ready` for ever) and the deployment rows on the backend's
    engine that no instance holds any more. A row WITH a weight source is
    startable and is kept, moved to the engine this fleet has (the 0022 rule);
    a row without one can never start and is deleted — tagged or not, because
    the rows a fleet master carried came from a manager that predates the #1760 tag.

    The retirement is remembered (runtime_settings) so a stray registration of
    the same backend cannot bring the row back as `ready`.
    """
    from app.models import Deployment, DeploymentInstance, RunnerUpgrade, RuntimeSetting, Worker

    w = session.query(Worker).filter(Worker.name == name).one_or_none()
    if w is None or not (w.labels or {}).get("external"):
        return {"retired": False}
    engine = (w.labels or {}).get("engine")
    # Rows provably owned by this backend: those its instances hung on, plus
    # engine=gpustack rows (the retired backend type). The engine LABEL of the
    # request is never a sweep key — an external worker labelled llamacpp must
    # not touch other workers' llamacpp rows.
    owned = {i.deployment_id for i in session.query(DeploymentInstance).filter(
        DeploymentInstance.worker_id == w.id).all()}
    session.query(DeploymentInstance).filter(
        DeploymentInstance.worker_id == w.id).delete(synchronize_session=False)
    session.query(RunnerUpgrade).filter(
        RunnerUpgrade.worker_id == w.id).delete(synchronize_session=False)
    session.delete(w)
    session.flush()
    session.merge(RuntimeSetting(key=_retired_key(name), value=str(engine or "")))
    owns_type = Deployment.id.in_(owned)
    if engine == "gpustack":
        # only the retired backend's own type is swept engine-wide; any other
        # external worker (ollama, ...) must not touch a live GPUStack's rows
        owns_type = owns_type | (Deployment.engine == "gpustack")
    sweep = session.query(Deployment).filter(owns_type).all()
    removed, converted = [], []
    for dep in sweep:
        held = (session.query(DeploymentInstance)
                .filter(DeploymentInstance.deployment_id == dep.id).first())
        if held is not None or dep.status == "stopped":
            continue
        if dep.source_files or dep.hf_repo:
            dep.engine = "llamacpp"
            converted.append(dep.model_name)
        else:
            session.delete(dep)
            removed.append(dep.model_name)
    logger.info("external backend %s retired (#1634): %d row(s) removed, %d converted",
                name, len(removed), len(converted))
    return {"retired": True, "deployments_removed": sorted(removed),
            "deployments_converted": sorted(converted)}


def _withdraw_discovered_without_instances(session, dep) -> bool:
    """#1760 (operator decision E3): a DISCOVERED row goes when its last
    instance goes. Returns True when the row was removed.

    A discovered row exists because a node reported a model it was ALREADY
    serving — the manager created it from the report, and that path has no
    weight source to record. It is therefore not startable: `POST
    /api/deployments/<id>/start` answers 409, and no retry changes that
    (`DISCOVERED_NO_SOURCE_REASON`). Once its last instance is gone, the row
    can no longer become anything; standing at `pending` for ever is the only
    form in which it still acts — on the operator, who reads it as something
    that might yet start.

    Deliberately NARROW, because the other two shapes must survive:

    * a CATALOG-deployed row without instances stays `pending`. It has a weight
      source, it IS startable, and deleting it would be data loss — the
      operator asked for that model.
    * a discovered row whose instance moved to ANOTHER worker stays. The query
      below is fleet-wide, not scoped to the reporting worker: `pruned` above
      is per-worker on purpose, and reading only that would withdraw a model
      that is being served two metres to the left.

    Re-creation is not a worry here and not guarded against: the row is only
    removed once nobody reports the model any more, and the report is what
    creates it. A node that starts serving it again re-creates it, which is the
    correct outcome and the same path as the first time.
    """
    if DISCOVERED_TAG not in (getattr(dep, "tags", None) or []):
        return False
    if getattr(dep, "source_files", None) or getattr(dep, "hf_repo", None):
        # Startable after all — not the shape this is about.
        return False
    from app.models import DeploymentInstance      # local, like every caller here
    still_serving = (session.query(DeploymentInstance)
                     .filter(DeploymentInstance.deployment_id == dep.id)
                     .first() is not None)
    if still_serving:
        return False
    logger.info("withdrawing discovered deployment %s (%s): last instance gone",
                dep.id, dep.model_name)
    session.delete(dep)
    return True

# #294b: a deploy creates the instance row as "scheduled" so the pull is visible
# from t+0. The owning node needs up to ~2 report intervals to pick up the load
# command and first-report the engine; until then it registers WITHOUT this
# instance, which would otherwise trip the reconcile prune below. Protect a
# just-scheduled row for this grace window (default 2× the 30s report interval +
# slack) so it survives until the node adopts it in place.
_SCHEDULE_GRACE_SECONDS = 150.0


def _aware(dt):
    """Coerce a datetime to tz-aware UTC (SQLite hands back naive datetimes),
    so arithmetic against a tz-aware ``now`` never raises."""
    from datetime import timezone

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _bearer(authorization: Optional[str]) -> Optional[str]:
    """Extract the Bearer token, or None if absent/malformed."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


def require_node_key(authorization: Optional[str] = Header(default=None)) -> None:
    """Fail-closed Bearer check for node→manager registration."""
    settings = get_settings()
    if not settings.node_key:
        raise HTTPException(status_code=503, detail="node registration disabled (no node key configured)")
    tok = _bearer(authorization)
    if tok is None:
        raise HTTPException(status_code=401, detail="missing or malformed Authorization header (expected 'Bearer <key>')")
    if not hmac.compare_digest(tok, settings.node_key):
        raise HTTPException(status_code=401, detail="invalid node key")


def derive_command_key(node_key: str, worker_name: str, key_epoch: int = 0) -> str:
    """Per-worker command-channel credential (#207): HMAC(node_key, worker_name).

    Lets a worker present a key scoped to ITS OWN commands instead of the shared
    node key. The manager stores NOTHING: the key is recomputable from
    (node_key, worker_name), so validation is a constant-time HMAC compare.
    Handed to the worker at enrollment (#262).

    ISOLATION (#285): in command_key_mode=enforce the node is provisioned with
    ONLY this per-worker key — enrollment WITHHOLDS the shared node_key — and it
    authorizes BOTH registration and the command channel. So a node can act only
    as itself and cannot derive another worker's key (deriving needs node_key,
    which stays manager/admin-side). In mode=allow the shared node_key is still
    accepted for back-compat (no isolation guarantee then).

    REVOCATION (#340): ``key_epoch`` folds into the HMAC input. Bumping a
    worker's epoch (rotate-key) invalidates that ONE worker's credential
    instantly — no fleet-wide node_key rotation. Epoch 0 keeps the LEGACY
    input format byte-for-byte, so every key handed out before #340 stays
    valid without re-enrollment."""
    msg = (f"cmdkey:{worker_name}" if not key_epoch
           else f"cmdkey:{worker_name}:{int(key_epoch)}")
    return hmac.new(node_key.encode(), msg.encode(), "sha256").hexdigest()


def _worker_key_epoch(worker_name: str) -> int:
    """The worker's current key epoch, resolved in a short session of its own —
    for callers (registration) that authorize before holding the row. Unknown
    name → 0 (a first-time registration verifies against the epoch the enroll
    handed out, which is the worker's row epoch or 0 pre-creation)."""
    from app.db import session_scope
    from app.models import Worker
    try:
        with session_scope() as s:
            w = s.query(Worker).filter(Worker.name == worker_name).first()
            return int(w.key_epoch or 0) if w is not None else 0
    except Exception:
        return 0


def _keep_unreported_instance(di, reported_deps: set, now) -> bool:
    """Registration is the authoritative state of a worker; a row it does not
    report is pruned — with two exceptions:

    * #294b: a just-scheduled instance the node has not had a chance to report
      yet (deploy creates it so the pull is visible from t+0) — kept within the
      grace window; once the node reports it, status flips off "scheduled".
    * #1372: a FAILED engine has nothing to report, so "not reported" is its
      steady state. Pruning it threw away the only copy of the node's reason
      and the deployment read `pending` forever. Keep the row until this
      deployment actually has a reported instance on this worker.
    """
    if di.status == "scheduled" and di.started_at is not None \
            and (now - _aware(di.started_at)).total_seconds() < _SCHEDULE_GRACE_SECONDS:
        return True
    if di.status == "failed" and di.deployment_id not in reported_deps:
        return True
    return False


def authorize_command_node(
    authorization: Optional[str], worker_name: Optional[str], *, mode: str = "allow",
    key_epoch: Optional[int] = None,
) -> None:
    """Fail-closed auth for the command-channel claim/result (#207). Accepts the
    shared node key (mode=allow, back-compat) OR a per-worker key =
    derive_command_key(node_key, worker_name). mode=enforce REJECTS the bare
    shared key, so a party WITHOUT node_key must present a valid per-worker key.
    Used for BOTH registration and the command channel; in enforce mode the node
    holds only its per-worker key (enrollment withholds node_key), so this gives
    true cross-worker isolation (#285)."""
    settings = get_settings()
    if not settings.node_key:
        raise HTTPException(status_code=503, detail="node registration disabled (no node key configured)")
    tok = _bearer(authorization)
    if tok is None:
        raise HTTPException(status_code=401, detail="missing or malformed Authorization header")
    if worker_name:
        epoch = key_epoch if key_epoch is not None else _worker_key_epoch(worker_name)
        if hmac.compare_digest(tok, derive_command_key(settings.node_key, worker_name, epoch)):
            return  # correct per-worker key for the CURRENT epoch (#340)
    if mode != "enforce" and hmac.compare_digest(tok, settings.node_key):
        return  # shared node key (allowed unless enforce)
    raise HTTPException(status_code=401, detail="invalid command-channel credential for this worker")



def _known_key_names() -> list:
    """(name, key_epoch) for every identity a valid command key could have been
    minted for: the live worker rows AND the names recorded by past enrollments
    (``enroll_jti_spent``). The enroll exchange creates NO worker row, so on a
    box whose agent never managed to register the JTI table is the ONLY place
    that remembers which name the key in play was minted for — exactly the
    #1250 clean-install case. Best-effort: never raises."""
    out: list = []
    try:
        from app.db import session_scope
        from app.models import EnrollJtiSpent, Worker

        with session_scope() as s:
            for w in s.query(Worker).all():
                out.append((w.name, int(w.key_epoch or 0)))
            seen = {n for n, _ in out}
            for row in (s.query(EnrollJtiSpent)
                         .order_by(EnrollJtiSpent.used_at.desc()).limit(50).all()):
                if row.worker_name and row.worker_name not in seen:
                    seen.add(row.worker_name)
                    out.append((row.worker_name, 0))
    except Exception:  # pragma: no cover - diagnosis must never break a request
        return out
    return out


def diagnose_registration_401(reg_name, authorization, *, known=None) -> str:
    """#1250a: explain a refused registration in the manager log, WITHOUT ever
    logging key material.

    A 401 here is almost always a name/key mismatch — the command key is
    ``HMAC(node_key, worker_name)``, so an agent reporting a different name than
    the one it was enrolled under is rejected on every single report cycle, for
    ever, and the only visible symptom is an empty fleet. Recomputing the key
    for each name we already know about turns that into a one-line answer.

    ``known`` is an iterable of ``(worker_name, key_epoch)``; it is read from the
    database when not supplied (kept injectable so this is unit-testable without
    one)."""
    settings = get_settings()
    tok = _bearer(authorization)
    if tok is None:
        return "no usable Bearer credential was presented"
    node_key = settings.node_key or ""
    if not node_key:
        return "the manager has no node key configured, so nothing can validate"
    if hmac.compare_digest(tok, node_key):
        return ("the SHARED node key was presented while command_key_mode=enforce — "
                "this node must present its OWN per-worker key (re-enroll it)")
    for name, epoch in (known if known is not None else _known_key_names()):
        try:
            expected = derive_command_key(node_key, name, int(epoch or 0))
        except Exception:  # pragma: no cover - defensive
            continue
        if hmac.compare_digest(tok, expected):
            if name == reg_name:
                return (f"the key is correct for {name!r} but at a different key "
                        "epoch — the worker's key was rotated, so re-enroll it")
            return (f"the presented key was minted for the worker name {name!r}, but this "
                    f"registration reports the name {reg_name!r} — the agent's "
                    "LLM_WORKER_NAME does not match the name it was enrolled under (#1250)")
    return ("the presented key matches NO name this manager knows about at its current "
            "key epoch — it was minted for a different name, against a different node "
            "key, or the worker's key has been rotated")

# #328 Capacity labels are STICKY across re-registration; every other label is
# authoritative and reflects the CURRENT report.
#
# register_worker replaced `labels` wholesale and only set keys whose incoming
# value was truthy, so one heartbeat with `vram_total_gb=None` deleted the label
# — and `_worker_budget_gb` returns None without it, which means admission is not
# enforced AT ALL ("never block on a guess"). The node's VRAM probe is explicitly
# best-effort (rocm-smi/amd-smi are not even in the worker-agent image; the fallback
# is a /sys/class/drm glob), so a transient sysfs read or a restart before the
# probe settles silently disabled the gate for that worker. Nothing logged it and
# the console showed no difference.
#
# NOT a blanket `{**old, **new}` merge, which would trade this bug for a worse
# one: `external` (#307) and `hardware` could then never be UNSET. A box that
# stopped being an external backend would keep the flag, and placement never
# targets external workers — so it would become permanently unplaceable, with no
# way back short of editing the row. Descriptive labels must be able to go away;
# capacity labels must not, because their absence is a fail-OPEN.
STICKY_LABELS = ("vram_total_gb", "mem_total_gb")


def _log_admission_basis(name: str, previous: dict | None, resulting: dict) -> None:
    """#328 say it out loud when a worker's VRAM admission basis changes, and warn
    whenever the result is `none` — that state means the gate is INERT for this
    worker, and it was previously indistinguishable from a working one."""
    from app.api.inventory import _capacity_source   # local: avoid an import cycle

    before = _capacity_source(SimpleNamespace(labels=previous))[0] if previous is not None else None
    after = _capacity_source(SimpleNamespace(labels=resulting))[0]
    if after == "none":
        logger.warning(
            "worker %s has no usable capacity label (%s) — VRAM admission control "
            "is INERT for it; placements will not be refused (#227/#328)",
            name, "was: %s" % before if before else "newly registered")
    elif before is not None and after != before:
        logger.info("worker %s: VRAM admission basis %s -> %s (#328)", name, before, after)


#: #918: labels whose disappearance CHANGES ROUTING and therefore may not pass
#: unremarked, even though they stay reversible (i.e. deliberately NOT added to
#: STICKY_LABELS above).
#:
#: #1535 (revA finding 4) added `relay_model_routing`: it decides whether the
#: master may place a SECOND model on a remote worker, so a node-agent rolled
#: back to a build that never sends it silently un-does that permission. The
#: placements already made are NOT re-evaluated — that box keeps serving two
#: models through a relay that can no longer tell them apart, which is the #929
#: failure exactly. Whoever reads the log has to be told, because the only other
#: signal is `router_config`'s warning at the next rebuild.
ROUTING_LABELS = ("advertise_addr", "relay_model_routing", "relay_engine_routing")


#: What the operator loses when each routing label goes away. Per label, because
#: the two consequences are different and a generic line would be wrong for one
#: of them — #918's whole point was that nothing said why traffic broke.
_ROUTING_LABEL_LOSS = {
    "advertise_addr": ("this report carried none, so its instances revert from the WS "
                       "relay route to their direct container endpoint, which a remote "
                       "master cannot reach (#918/#913)"),
    "relay_model_routing": ("this agent build cannot pick an engine by the model a "
                            "request names, so any SECOND model already placed on this "
                            "worker is now served by whichever engine is ready first — "
                            "wrong weights under the right name (#1535/#929 M1). New "
                            "placements are refused again; the existing ones are not "
                            "moved. Re-upgrade the agent or unload the extra models"),
    "relay_engine_routing": ("this agent build cannot serve a relayed request from one "
                             "NAMED engine, so the router config falls back to one relay "
                             "URL per worker: replicas of one model on this worker are "
                             "again chosen by the node's round robin, and a chat pinned "
                             "to one of them alternates between them — prefix-cache "
                             "misses, no errors (#2521). Re-upgrade the agent"),
}


def _log_label_drops(name: str, previous: dict | None, resulting: dict) -> None:
    """#918 (#913/#262): say it out loud when a report changes or drops a label
    that decides where this worker's traffic is routed.

    ``advertise_addr`` stopped being descriptive when #913 made
    ``router_config.generate_from_db`` consume it: its PRESENCE is what routes a
    REMOTE worker's instances through the master's WS relay
    (``_is_relay_routed``). One report that omits it — an operator editing
    ``node.env``, a node-agent rolled back to a build that never sent the field —
    therefore reverts that worker's router endpoints to the raw
    ``http://engine-…:8080/v1`` container address the master cannot reach from
    outside its own docker network. Every completion on that worker then 502s,
    and until now NOTHING said why.

    Why this logs instead of making the label sticky (the issue's other
    suggestion): the NODE arms its own relay client on the SAME flag
    (``runtime.maybe_start_relay`` returns early without
    ``LLM_WORKER_ADVERTISE_ADDR``). A master holding a sticky ``advertise_addr``
    after the node stopped reporting one would route that worker through a relay
    nobody dials — a hard 100% failure, strictly worse than the direct-endpoint
    revert, which at least works when the worker genuinely became co-located.
    The master must mirror what the node reports; what it owes the operator is
    the signal. Same reasoning as the ``external`` label in the comment above
    STICKY_LABELS: a label that decides placement/routing has to stay
    un-settable, or there is no way back short of editing the row.

    Silent when nothing changed — this runs on EVERY heartbeat (~30s per node),
    and a line per report would bury the one that matters (the discipline
    ``_log_admission_basis`` already keeps).
    """
    previous = previous or {}
    for key in ROUTING_LABELS:
        before = str(previous.get(key) or "").strip()
        after = str(resulting.get(key) or "").strip()
        if before == after:
            continue
        if not after:
            logger.warning("worker %s: %s dropped (was %s) — %s",
                           name, key, before, _ROUTING_LABEL_LOSS[key])
        elif before:
            logger.info("worker %s: %s %s -> %s (#918)", name, key, before, after)


#: #2141 follow-on: labels that describe a GPU. On a cpu-family worker every
#: one of them is a phantom (the host's iGPU read through sysfs by an older
#: agent, #330's any-hardware fallback), so they are neither kept sticky nor
#: accepted from the report.
_GPU_ONLY_LABELS = ("vram_total_gb", "vram_used_gb", "vram_used_at")


def _merge_worker_labels(previous: dict | None, incoming: dict) -> dict:
    """Incoming labels win, except that a previously-known capacity label is never
    erased by a report that simply failed to measure it.

    #2141 follow-on, measured on 0.91: the fixed node-agent stopped sending
    ``vram_total_gb`` for ``hardware=cpu`` and the stored ``103.1`` survived
    every heartbeat — the stickiness below resurrected it. A CPU worker has no
    VRAM to protect a budget for, so for the cpu family the GPU labels are
    dropped instead of kept; GPU families keep the #328 stickiness unchanged."""
    from app.api.inventory import _hardware_family   # local: avoid an import cycle

    previous = previous or {}
    merged = dict(incoming)
    if _hardware_family(merged.get("hardware")) == "cpu":
        for key in _GPU_ONLY_LABELS:
            merged.pop(key, None)
        return merged
    for key in STICKY_LABELS:
        if not merged.get(key) and previous.get(key):
            merged[key] = previous[key]
    return merged


def register_worker(session, reg: WorkerRegistration) -> dict:
    """Upsert the worker + its served models/deployments/instances. Idempotent:
    re-registering the same worker updates in place (no duplicate rows)."""
    from datetime import datetime, timezone

    from app.models import Deployment, DeploymentInstance, Model, Worker

    # #254 D2 guard: reject nested masters. A control plane may never enroll as a
    # worker of another control plane (flat master → N workers only).
    #
    # #549 R5, SANCTIONED and load-bearing: a FULL BOX acting as a worker for a
    # remote master is allowed, and passes here precisely because its worker-agent
    # sends no role (defaults "worker"). The guard rejects what a registration
    # CLAIMS TO BE, not what else runs on the box — a box running its own
    # llm-manager whose worker-agent enrols elsewhere is a worker of that fleet,
    # full stop, because one box runs ONE worker-agent pointed at ONE master
    # (container_name is fixed), so engine ownership is never ambiguous.
    # Tightening this check to probe the box (port-scan for a manager, demand a
    # role field) would break R5 silently; the api tier pins the accepted cases.
    if (reg.role or "worker").strip().lower() in MANAGER_ROLES:
        raise HTTPException(
            status_code=409,
            detail=("a manager/master may not register as a worker of another "
                    "manager — nested masters are not supported (flat "
                    "master -> N workers only)"),
        )

    now = datetime.now(timezone.utc)
    labels = {}
    if reg.hardware:
        labels["hardware"] = reg.hardware
    if reg.engine:
        labels["engine"] = reg.engine
    if reg.stack_version:
        labels["stack_version"] = reg.stack_version
    if reg.stack_version_source:
        labels["stack_version_source"] = reg.stack_version_source
    if reg.engine_version:
        labels["engine_version"] = reg.engine_version
    # Stored even when engine_version IS set (`why: "probed"`), so a reader can
    # tell "this came from a successful probe" from "this label predates
    # #1932" — a missing key is itself an answer, and a different one.
    if reg.engine_version_why:
        labels["engine_version_why"] = reg.engine_version_why
    # #2530: not sticky — an agent rolled back to a build that does not report
    # it must read "unknown", not the digest of the build it replaced.
    if reg.agent_image_digest:
        labels["agent_image_digest"] = reg.agent_image_digest
    _agent_digests = sorted({d for d in (reg.agent_image_digests or [])
                             if isinstance(d, str) and d.startswith("sha256:")})[:AGENT_DIGESTS_CAP]
    if _agent_digests:
        labels["agent_image_digests"] = _agent_digests
    if reg.advertise_addr:
        labels["advertise_addr"] = reg.advertise_addr
    # #1535: stored as a plain bool so the placement guard can read it the same
    # way it reads advertise_addr. Written on every registration (not sticky):
    # the answer belongs to the agent build currently running, so a rollback to
    # an older agent must take the capability away with it.
    labels["relay_model_routing"] = bool(reg.relay_model_routing)
    # #2521: same footing — rewritten on every registration, so a rollback to
    # an older agent takes the per-engine relay URL away with it.
    labels["relay_engine_routing"] = bool(reg.relay_engine_routing)
    if reg.mem_total_gb:
        labels["mem_total_gb"] = reg.mem_total_gb
    if reg.vram_total_gb:
        labels["vram_total_gb"] = reg.vram_total_gb
    # #330 stage 2: deliberately NOT sticky (contrast STICKY_LABELS): a stale
    # usage figure is WORSE than none — the gate must fall back to the static
    # stage-1 math, not admit against last week's reading. The freshness stamp
    # is SERVER time so node clocks never matter.
    if reg.vram_used_gb is not None:
        labels["vram_used_gb"] = reg.vram_used_gb
        labels["vram_used_at"] = time.time()
    # live utilization snapshot for the dashboard load graph — refreshed every
    # report; keep only the metrics actually reported (None ones dropped).
    metrics = {k: v for k, v in (("vram_used_gb", reg.vram_used_gb),
                                 ("load", reg.load), ("gpu_util", reg.gpu_util),
                                 ("mem_used_gb", reg.mem_used_gb), ("ncpu", reg.ncpu))
               if v is not None}
    if metrics:
        labels["metrics"] = metrics
        # #1598: WHEN this sample was taken, server-side. The console plots the
        # live chart on a TIME axis and appends a point only when this stamp
        # moves, so a node reporting every 30 s draws ten points across five
        # minutes instead of the same reading repeated once per poll. Stamped
        # here as well as in the fast beat because a node without #1619 still
        # reports, and an unstamped sample would look frozen forever.
        labels["metrics_at"] = time.time()
    # #307: mark external endpoint backends (explicit flag or an ollama engine) so
    # the console never shows them "stale" and placement never targets them.
    # #2559: an Ollama run by OUR agent (the Mac thin worker) is a normal,
    # placeable worker. `labels` is built fresh from each report and the merge
    # below does not keep `external`, so a report saying so also clears an
    # external label left by an earlier hand registration.
    if reg.external or ((reg.engine or "").lower() == "ollama" and not reg.agent_managed):
        labels["external"] = True
    elif reg.agent_managed:
        labels["agent_managed"] = True
        pinned = reg.ollama_pinned
        if isinstance(pinned, str) and OLLAMA_VERSION.fullmatch(pinned):
            labels["ollama_pinned"] = pinned
        elif pinned is not None:
            # never stored: it would become an argv element on the Mac and
            # text in the console. %r and the cut keep the log line one line.
            logger.warning("worker %s reports ollama_pinned %r, not an X.Y.Z version — "
                           "ignored, so no Ollama upgrade is offered for it (#2559)",
                           reg.name, str(pinned)[:40])

    # #419 P0 Task 4: enrolling proves possession of a short-lived token; it does
    # NOT prove an operator meant to give this box production traffic. A newly
    # seen worker lands in `pending` and an admin approves it
    # (POST /api/workers/<id>/approve). Both placement filters already select
    # status == "ready" (api/inventory.py, api/hf.py), so `pending` is excluded
    # everywhere without a schema change — and every worker already on a live box
    # is already "ready", so an upgrade strands nobody.
    #
    # EXCEPTION — external backends (#307, the Mac Ollama gateway): an admin adds
    # those deliberately through the console, so there is no second human to wait
    # for and queueing them would break the existing add flow.
    externally_added = bool(labels.get("external"))
    gated = get_settings().worker_approval_mode == "manual" and not externally_added

    worker = session.query(Worker).filter(Worker.name == reg.name).one_or_none()
    if worker is None:
        worker = Worker(name=reg.name, address=reg.address, labels=labels,
                        status=("pending" if gated
                                else "draining" if reg.draining else "ready"),
                        last_heartbeat=now)
        session.add(worker)
        session.flush()
        _log_admission_basis(reg.name, None, labels)
    else:
        worker.address = reg.address
        merged = _merge_worker_labels(worker.labels, labels)
        _log_admission_basis(reg.name, worker.labels, merged)
        # #918: an advertise_addr that vanished re-routes this worker silently.
        _log_label_drops(reg.name, worker.labels, merged)
        worker.labels = merged
        # Re-registration must not decide approval in either direction: a
        # pending worker that restarts stays pending (otherwise a container
        # restart — which every node does on upgrade — walks straight through
        # the gate), and an approved one stays ready (otherwise every node
        # upgrade would dump the whole fleet back into the queue).
        #
        # #261-C2: "draining" survives re-registration for the same reason.
        # The node heartbeats every 30s, so without this a drain would hold for
        # AT MOST one report interval and then silently undo itself — a drain
        # that quietly un-drains is worse than none, because the operator has
        # been told the node is out of service.
        if worker.status not in ("pending", "draining"):
            worker.status = "ready"
        worker.last_heartbeat = now
        # #2512: a fleet master's worker that is (still, or again) `ready` —
        # an agent upgraded to LLM_MASTER_ROLE=fleet, or an operator undrain —
        # is drained here, so placement never picks it and its engines go.
        if reg.draining and worker.status == "ready":
            logger.info("worker %s asks to host no models (LLM_MASTER_ROLE=fleet, #2512) — draining", reg.name)
            drain_worker(session, worker)

    from app import liveness
    if reg.liveness_beat:
        liveness.note_beat(worker.id)
    else:
        liveness.forget(worker.id)

    instances = 0
    seen_keys: set = set()  # (deployment_id, instance_id) this worker reports NOW
    # #261-C2: a DRAINING worker's reports update the heartbeat and labels but
    # never re-create instances. The drain deleted this worker's instance rows
    # and enqueued the unloads; until the node processes them it keeps
    # reporting the models, and upserting them here would resurrect exactly
    # what the drain removed (#298b's resurrection, on the worker axis instead
    # of the model axis — tombstones are wrong here because they are keyed by
    # model_name and would deregister the SAME model's healthy instances on
    # other workers). With the loop skipped, seen_keys stays empty and the
    # authoritative-state prune below sweeps any stragglers — convergence and
    # cleanup point the same way.
    # #2953: an instance this worker was just told to unload and still reports
    # (the unload is queued or running) is on its way out — the report must not
    # re-create the row the drain/stop/reassign deleted as a fresh `ready` one.
    unloading = _unloading_instance_ids(session, worker)
    for m in (reg.models if worker.status != "draining" else []):
        if m.instance_id and m.instance_id in unloading:
            continue
        # #298b: don't resurrect a model the operator just undeployed — the node
        # is still reporting it until it processes the unload.
        if _tombstoned(m.model_name):
            continue
        served = m.served_model or m.model_name
        # ensure the logical Model
        model = session.query(Model).filter(Model.name == served).one_or_none()
        if model is None:
            model = Model(name=served)
            session.add(model)
            session.flush()
        # ensure the client-facing Deployment (model_name is unique)
        dep = session.query(Deployment).filter(
            Deployment.model_name == m.model_name
        ).one_or_none()
        if dep is None:
            # #1760: a row DISCOVERED from a node's report carries no weight
            # source, and cannot: `RegisteredModel` has no such field — a node
            # reports WHAT it serves, never WHERE the weights came from. That
            # is fine while the engine runs (the row exists because something is
            # already serving) and stops being fine the moment it stops: the row
            # falls to `pending`, `/start` answers 409 for ever, and nothing
            # says why. Measured on 0.79: three of four deployments in exactly
            # that state, and the whole #1507/#1446 chain hanging off it.
            #
            # The origin is therefore RECORDED here, where it is known, instead
            # of being guessed later from the absence of a field. Same lesson as
            # #1760 part 1: the manager knew the reason and never wrote it down.
            dep = Deployment(model_name=m.model_name, model_id=model.id,
                             engine=(reg.engine or "unknown"), params={},
                             task=(m.task or _observed_task(m) or "chat"), status="active",
                             tags=[DISCOVERED_TAG])
            session.add(dep)
            session.flush()
        # #312: the operator paused this deployment — ignore node reports for it
        # (the node keeps reporting until it processes the unload) so a stopped
        # model isn't resurrected as a live instance. Resume flips it back.
        elif dep.status == "stopped":
            continue
        # #318: an explicit serve-task (external backend typed its models embed/
        # rerank) updates the deployment so the router routes /v1/embeddings +
        # /v1/rerank to it. Node-agent reports omit task (None) → never clobbered.
        if m.task and dep.task != m.task:
            dep.task = m.task
        # #2868: a row the master itself discovered (adopted as chat before the
        # node could say better) is corrected once the node reports the real
        # serve mode. A deployment someone chose (no discovered tag) is not.
        elif (not m.task and dep.task == "chat" and DISCOVERED_TAG in (dep.tags or [])
              and _observed_task(m) in ("embed", "rerank")):
            dep.task = _observed_task(m)
        # #3106: a DISCOVERED row learns the weight file its engine runs on, so
        # the engine can be started again (a runner upgrade stops and restarts
        # every engine on the worker; without a file the node refuses the
        # restart). Only FILLS an empty source on a row the master created
        # itself: a deployment somebody made from the catalog keeps the source
        # it was given, whatever a node says.
        if (DISCOVERED_TAG in (dep.tags or []) and not (dep.source_files or [])
                and not dep.hf_repo):
            _adopted = adoptable_weight_files(m.files)
            if _adopted:
                dep.source_files = _adopted
                logger.info("recorded the weight file(s) %s for adopted deployment "
                            "%s from %s's report", _adopted, dep.model_name, worker.name)
        # #2393: a deployment that carries no footprint (created offline, where
        # no repo lookup is possible) learns it from the weights its node holds.
        # Only ever FILLS a NULL: a recorded estimate is never second-guessed.
        if dep.est_gb is None and m.weights_gb and m.weights_gb > 0:
            _est = footprint_gb(dep.engine, float(m.weights_gb), None, dep.params or {})
            if _est:
                dep.est_gb = float(_est)
                logger.info("recorded est_gb=%s for %s from %s GB of weights on %s (#2393)",
                            _est, dep.model_name, m.weights_gb, worker.name)
        # upsert the instance keyed by (deployment, worker, instance_id). An
        # EXPLICIT instance_id (the engine's container) distinguishes replicas +
        # same-model-per-worker. A node that omits it (old node / endpoint-only Mac
        # gateway) keeps the back-compat (deployment, worker) keying (NULL
        # instance_id, updated in place) — so an endpoint refresh isn't mistaken
        # for a new replica.
        inst_key = m.instance_id
        q = session.query(DeploymentInstance).filter(
            DeploymentInstance.deployment_id == dep.id,
            DeploymentInstance.worker_id == worker.id,
        )
        q = q.filter(DeploymentInstance.instance_id == inst_key) if inst_key \
            else q.filter(DeploymentInstance.instance_id.is_(None))
        inst = q.one_or_none()
        if inst is None:
            inst = DeploymentInstance(deployment_id=dep.id, worker_id=worker.id,
                                      instance_id=inst_key, endpoint=m.endpoint,
                                      api_key=m.api_key, status=m.status,
                                      detail=m.detail, started_at=now,
                                      engine_version=_instance_engine_version(m),
                                      runner_image=_instance_runner_image(m))
            session.add(inst)
        else:
            inst.instance_id = inst_key
            inst.endpoint = m.endpoint
            inst.api_key = m.api_key
            # #2509 fix round 2: mark the exact transition a row comes back
            # from `lost` (#1455 SCH3) — durable evidence a later SURPLUS
            # reap (reconciler.py's `_surplus_reap_key`) can lean on even
            # after `status`/`detail` below are overwritten by THIS report,
            # and by every report after it. Neither `status` (about to
            # become whatever the node says) nor `started_at` (untouched by
            # an outage the engine itself survived) can tell "this replica
            # was down and may already be covered by a rescue elsewhere"
            # once the row reads `ready` again — this can.
            if inst.status == "lost":
                inst.revived_at = now
            # #2617: a replacement the fallback cancelled that the worker now
            # reports as running is an unpaired surplus: unload it, keep the
            # row failed, do not let this report flip it back to ready.
            if _is_cancelled_replacement(inst) and m.status != "failed":
                _unload_cancelled_replacement(session, worker, inst)
                seen_keys.add((dep.id, inst_key))
                continue
            inst.status = m.status
            inst.detail = m.detail
            # Only a report that carries a build replaces it: the node reads it
            # once the engine is ready, so reports before that omit it.
            if _instance_engine_version(m):
                inst.engine_version = _instance_engine_version(m)
            # #3106: likewise the container image; an older agent omits it
            # and the last known value stays.
            if _instance_runner_image(m):
                inst.runner_image = _instance_runner_image(m)
        seen_keys.add((dep.id, inst_key))
        instances += 1

    # #286 reconcile: registration is the AUTHORITATIVE current state of this
    # worker. Any DeploymentInstance on this worker it no longer reports has been
    # unloaded / died / dropped — prune it so the console doesn't keep showing a
    # stale (often false-"ready") instance forever. Scoped to worker_id, so it
    # never touches another node's instances.
    pruned = 0
    # #678 review follow-up: a prune-deleted deployment is invisible to the
    # #677 reconcile below — not in seen_keys (no longer reported) and not in
    # the instance query (just deleted + flushed). Collect its dep id here or
    # it stays 'active' forever at zero instances, the mirror of the original
    # lie.
    pruned_dep_ids: set = set()
    reported_deps = {k[0] for k in seen_keys}
    for di in (session.query(DeploymentInstance)
               .filter(DeploymentInstance.worker_id == worker.id).all()):
        if (di.deployment_id, di.instance_id) not in seen_keys:
            if _keep_unreported_instance(di, reported_deps, now):
                continue
            session.delete(di)
            pruned_dep_ids.add(di.deployment_id)
            pruned += 1

    session.flush()

    # #677: Deployment.status never left 'pending' even while its engines
    # served (0.91: DISTINCT status == {pending} with ready instances) — a
    # standing lie to every consumer that reads the deployment status.
    # Reconcile it from the instances, FLEET-WIDE per touched deployment
    # (an instance on another worker keeps a deployment active when this
    # one drops its last). 'stopped' is the operator's pause (#312) and is
    # never overridden; _SERVEABLE_INSTANCE_STATUS stays the serving truth
    # (router_config) — this is display/consumer consistency.
    touched_dep_ids = {dep_id for dep_id, _ in seen_keys} | pruned_dep_ids | {
        di.deployment_id for di in (session.query(DeploymentInstance)
                                    .filter(DeploymentInstance.worker_id == worker.id).all())
    }
    for dep_id in touched_dep_ids:
        dep = session.get(Deployment, dep_id)
        if dep is None or dep.status == "stopped":
            continue
        if _withdraw_discovered_without_instances(session, dep):
            continue
        has_ready = (session.query(DeploymentInstance)
                     .filter(DeploymentInstance.deployment_id == dep_id,
                             DeploymentInstance.status == "ready")
                     .first() is not None)
        want = "active" if has_ready else "pending"
        if dep.status != want:
            dep.status = want

    session.flush()
    # #2019: a report is also how the manager learns an overlap's replacement is
    # ready. Same event-driven reasoning as the gate below — and wrapped the same
    # way, because a stuck overlap must never cost a worker its registration.
    try:
        _retire_superseded_instances(session, worker, now)
    except Exception:  # pragma: no cover - the report must still be accepted
        logger.exception("retiring superseded instances failed (report still accepted)")

    # #549 R3: a report is the health signal the upgrade's relaunch gate waits
    # for. Event-driven on purpose — no background thread to die silently.
    try:
        from app.api.runner_upgrade import advance_runner_upgrade
        advance_runner_upgrade(session, worker)
    except Exception:  # pragma: no cover - the gate must never fail a report
        logger.exception("runner-upgrade advancement failed (report still accepted)")
    return {"worker_id": str(worker.id), "models": len(reg.models),
            "instances": instances, "pruned": pruned}


def register_workers_api(app) -> None:
    # No router-level dependency: #285 authorizes registration per-worker,
    # against the SPECIFIC name in the body, so it needs the parsed request.
    router = APIRouter()

    @router.post("/api/workers")
    def register(reg: WorkerRegistration, authorization: Optional[str] = Header(default=None)):
        # #285 true per-worker isolation: registration is authorized with the
        # SAME per-worker credential the command channel uses (scoped to
        # reg.name), or the shared node key in mode=allow (back-compat). In
        # mode=enforce the bare shared key is refused, so a node that holds only
        # its own per-worker key (never node_key) can register as itself and
        # NOT as — or on behalf of — any other worker.
        # #340: ':' is the separator inside the epoch-aware command-key HMAC
        # ('cmdkey:<name>:<epoch>'). A worker literally NAMED 'node-a:1' at
        # epoch 0 would hold the same key as 'node-a' at epoch 1 — so colon
        # names are refused at both entry points (here and the token mint).
        if ":" in (reg.name or ""):
            raise HTTPException(status_code=422, detail=(
                "worker names must not contain ':' (reserved by the "
                "command-key derivation, #340)"))
        # #1250a: a refused registration used to leave NOTHING in the manager
        # log — an agent whose name did not match its key 401-looped invisibly
        # and the fleet just stayed empty. Say which identity the presented key
        # actually belongs to; never log key material.
        try:
            authorize_command_node(authorization, reg.name,
                                   mode=get_settings().command_key_mode)
        except HTTPException as exc:
            if exc.status_code == 401:
                logger.warning(
                    "worker registration REFUSED (401) for name %r: %s",
                    reg.name, diagnose_registration_401(reg.name, authorization))
            raise
        # #336: with the unique constraints in place a concurrent registration
        # race surfaces as IntegrityError instead of silent duplicate rows.
        # One retry suffices - the second pass finds the winner via
        # one_or_none() and updates it.
        from sqlalchemy.exc import IntegrityError
        if reg.external and reg.retired is not True:
            # #1634: a retired backend stays retired. The mark is keyed by name
            # and holds the engine; only a registration for exactly that pair is
            # refused. `retired: false` is the deliberate re-federation.
            from app.models import RuntimeSetting
            with session_scope() as s:
                mark = s.get(RuntimeSetting, _retired_key(reg.name))
                if mark is not None and mark.value == str(reg.engine or ""):
                    if reg.retired is False:
                        s.delete(mark)
                    else:
                        raise HTTPException(status_code=409, detail=(
                            f"external backend {reg.name!r} was retired; register it "
                            "with retired=false to bring it back (#1634)"))
        try:
            with session_scope() as s:
                result = register_worker(s, reg)
        except IntegrityError:
            logger.info("registration race for %s - retrying against the "
                        "winner's row (#336)", reg.name)
            with session_scope() as s:
                result = register_worker(s, reg)
        if reg.retired and reg.external and not reg.models:
            with session_scope() as s:
                result.update(retire_external_worker(s, reg.name))
        # P2-B2: a registration changes the live fleet → regenerate the LiteLLM
        # router config from DB state so new/updated endpoints route immediately.
        # Best-effort: a config-write failure must NOT fail the registration
        # (the node still registered; the config can be rebuilt via
        # POST /api/router/rebuild).
        rebuilt = False
        try:
            from app.router_config import rebuild_from_state

            config = rebuild_from_state()
            rebuilt = True
            result["model_list_size"] = len(config.get("model_list", []))
        except Exception:  # pragma: no cover - defensive
            logger.warning("router rebuild after registration failed (non-fatal)", exc_info=True)
        return {"status": "registered", "router_rebuilt": rebuilt, **result}

    @router.post("/api/workers/{worker_id}/metrics")
    def report_metrics(worker_id: str, m: WorkerMetrics,
                       authorization: Optional[str] = Header(default=None)):
        """#1619: the fast beat's landing place — utilisation only.

        WHAT IT DELIBERATELY DOES NOT DO, because each of these is why the full
        registration cannot run at this rate:

        * no router rebuild. `POST /api/workers` regenerates the LiteLLM config
          on every call; at a 3 s beat that is 20 rebuilds a minute per worker,
          for numbers no route depends on.
        * no label merge, no admission-basis logging, no approval-state change.
          The beat is not a registration and must never be able to act like one.
        * **it does not move `last_seen`.** That stamp answers "is this worker
          still reporting", and the full report is what earns it. A node whose
          registration has been failing for ten minutes must look stale even
          while its metrics keep arriving — otherwise the fleet view says
          healthy about a worker the manager can no longer place onto.

        Authorised with the per-worker command key, exactly like the command
        claim (`api/commands.py`): the node already holds it, and it is scoped
        to this one worker.
        """
        # local imports: this module's style (see `claim`/`approve` above) —
        # the model layer is not needed at import time.
        from app.api._ids import parse_uuid
        from app.models import Worker
        with session_scope() as s:
            w = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            authorize_command_node(authorization, w.name if w else None,
                                   mode=get_settings().command_key_mode)
            if not w:
                raise HTTPException(status_code=404, detail="unknown worker")
            labels = dict(w.labels or {})
            metrics = {k: v for k, v in (("vram_used_gb", m.vram_used_gb),
                                         ("load", m.load), ("gpu_util", m.gpu_util),
                                         ("mem_used_gb", m.mem_used_gb),
                                         ("ncpu", m.ncpu))
                       if v is not None}
            if not metrics:
                # Nothing readable on that node right now. Leaving the previous
                # snapshot in place is the honest answer: the alternative is a
                # dashboard that drops to zero whenever a sysfs read hiccups.
                return {"status": "empty"}
            labels["metrics"] = metrics
            labels["metrics_at"] = time.time()   # #1598: see the registration path
            # #330 stage 2 keeps the admission gate's VRAM reading beside the
            # display copy, with a SERVER-side freshness stamp — same write the
            # full report does, so the gate simply gets fresher numbers.
            if m.vram_used_gb is not None:
                labels["vram_used_gb"] = m.vram_used_gb
                labels["vram_used_at"] = time.time()
            w.labels = labels
        return {"status": "ok", "metrics": sorted(metrics)}

    @router.post("/api/workers/{worker_id}/beat")
    def liveness_beat(worker_id: str, authorization: Optional[str] = Header(default=None)):
        """The worker still answers (app/liveness.py). Writes nothing: this runs
        every two seconds per remote worker, for a fact that matters only while
        it keeps arriving. Same credential as the command claim."""
        from app import liveness
        from app.api._ids import parse_uuid
        from app.models import Worker
        with session_scope() as s:
            w = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            authorize_command_node(authorization, w.name if w else None,
                                   mode=get_settings().command_key_mode)
            if not w:
                raise HTTPException(status_code=404, detail="unknown worker")
            liveness.note_beat(w.id)
        return {"status": "ok"}

    # #419 P0 Task 4. Gated: approval IS the trust decision, so it must not be
    # reachable with the node's own credential — a node that could approve
    # itself would make the gate decorative. #314: worker lifecycle
    # (approve/drain/rotate-key/delete/undrain) is "add/remove workers,
    # per-worker keys, federation" → the SUPER-ADMIN tier, not admin.
    approve_router = APIRouter(dependencies=[Depends(require_role(Role.SUPERADMIN))])

    @approve_router.post("/api/workers/{worker_id}/approve")
    def approve(worker_id: str):
        from app.api._ids import parse_uuid
        from app.models import Worker  # local import: matches this module's style

        # LLMM-12: parse the id BEFORE it reaches the query, like every other
        # worker-lifecycle route here. On Postgres a non-UUID path segment made
        # `Worker.id == worker_id` raise DataError ("invalid input syntax for
        # type uuid") — an unhandled 500 for what is a client mistake, which is
        # exactly what app/api/_ids.py exists to prevent (400 instead, and no
        # 5xx alert on a typo'd URL). Parsed outside the session so a malformed
        # id costs no connection either.
        wid = parse_uuid(worker_id, "worker_id")
        with session_scope() as s:
            worker = s.get(Worker, wid)
            if worker is None:
                raise HTTPException(status_code=404, detail="worker not found")
            if worker.status == "pending":
                worker.status = "ready"
            return {"status": "approved", "worker": worker.name,
                    "worker_status": worker.status}

    @approve_router.post("/api/workers/{worker_id}/drain")
    def drain(worker_id: str):
        """#261-C2: take a worker out of service without touching the box.

        Manager-driven, deliberately — the #261 design sketched a `drain_node`
        command kind, but the manager already owns every piece: placement skips
        non-ready workers, unload_engine exists, and the instance rows are the
        manager's own. A node-side kind would add a second implementation of
        "unload everything" for the node to hold on its own docker socket, with
        nothing gained. The command channel carries the unloads; drain itself is
        state the manager flips.

        Idempotent: draining a draining worker reports the state and enqueues
        nothing new.
        """
        from app.api._ids import parse_uuid
        from app.models import Worker

        with session_scope() as s:
            worker = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            if worker is None:
                raise HTTPException(status_code=404, detail="worker not found")
            if worker.status == "pending":
                raise HTTPException(status_code=409, detail=(
                    "worker is pending approval — a worker that never served "
                    "has nothing to drain; reject it by leaving it unapproved"))
            if (worker.labels or {}).get("external"):
                raise HTTPException(status_code=409, detail=(
                    "external endpoint backends run no worker-agent — remove the "
                    "backend instead of draining it"))
            if worker.status == "draining":
                return {"status": "draining", "worker": worker.name,
                        "instances_unloaded": 0, "already": True}

            unloaded = drain_worker(s, worker)
            name = worker.name

        # #2521: the drained instances leave routing NOW, not at the next
        # worker registration — until then the router (and a chat pinned to one
        # of them, app/affinity.py) kept sending requests to engines that are
        # being unloaded. Best-effort, as after a worker removal.
        try:
            from app.router_config import rebuild_from_state

            rebuild_from_state()
        except Exception:  # pragma: no cover - defensive
            logger.warning("router rebuild after drain failed (non-fatal)", exc_info=True)
        return {"status": "draining", "worker": name,
                "instances_unloaded": unloaded}

    @approve_router.post("/api/workers/{worker_id}/rotate-key")
    def rotate_key(worker_id: str):
        """#340: revoke THIS worker's command-channel credential by bumping its
        key epoch — the old per-worker key stops validating on the next claim/
        report, without touching the shared node_key or any other worker.

        Returns the NEW key once, for the operator to place on the node
        (LLM_WORKER_COMMAND_KEY in .env.node / the stack .env, then restart the
        agent). Until then the node's reports 401 — that is the point.

        In command_key_mode=allow the SHARED node key still authorizes
        everything, so rotation alone does not lock a compromised node out —
        the response says so rather than implying containment that mode
        cannot give."""
        from app.api._ids import parse_uuid
        from app.config import get_settings as _gs
        from app.models import Worker

        with session_scope() as s:
            worker = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            if worker is None:
                raise HTTPException(status_code=404, detail="worker not found")
            if (worker.labels or {}).get("external"):
                raise HTTPException(status_code=409, detail=(
                    "external endpoint backends hold no command-channel "
                    "credential — nothing to rotate"))
            worker.key_epoch = int(worker.key_epoch or 0) + 1
            settings = _gs()
            new_key = derive_command_key(settings.node_key, worker.name,
                                         worker.key_epoch)
            logger.info("worker %s command key ROTATED to epoch %d (#340)",
                        worker.name, worker.key_epoch)
            return {
                "status": "rotated",
                "worker": worker.name,
                "key_epoch": worker.key_epoch,
                "command_key": new_key,
                "mode": settings.command_key_mode,
                "note": ("place this key on the node as LLM_WORKER_COMMAND_KEY and "
                         "restart its agent; the previous key is now refused"
                         + ("" if settings.command_key_mode == "enforce" else
                            ". WARNING: command_key_mode=allow — the SHARED node "
                            "key still authorizes; switch to enforce for the "
                            "rotation to actually contain a compromised node")),
            }

    @approve_router.patch("/api/workers/{worker_id}")
    def rename_worker(worker_id: str, patch: WorkerPatch):
        """#284: set a manager-owned display label. Node registration keys on
        `name` and never touches display_name, so the rename sticks."""
        from app.api._ids import parse_uuid
        from app.models import Worker
        with session_scope() as s:
            worker = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            if worker is None:
                raise HTTPException(status_code=404, detail="worker not found")
            # #284 review (agent-seqis, defense-in-depth): cap length and drop
            # non-printable chars before persisting — this label is serialized
            # into the UI and logs, so an unbounded / control-char value is a
            # log-injection / layout-break vector even though it's SUPERADMIN-only
            # and React-escaped on render.
            dn = (patch.display_name or "").strip()
            if len(dn) > 128:
                raise HTTPException(status_code=422, detail="display_name too long (max 128)")
            dn = "".join(ch for ch in dn if ch.isprintable())
            worker.display_name = dn or None
            return {"id": str(worker.id), "name": worker.name,
                    "display_name": worker.display_name}

    @approve_router.delete("/api/workers/{worker_id}")
    def remove_worker(worker_id: str):
        """#594: forget a worker that has left the fleet. The fleet was
        drain-but-never-forget — a retired machine sat listed as drained/stale
        forever. This is the delete path.

        Refused for an ACTIVELY SERVING worker (ready AND heartbeating): drain
        it first, so its deployments relocate deliberately instead of vanishing
        with the row. Everything else deletes — a draining worker, a 'ready'
        worker whose node died (stale heartbeat), a never-approved pending one,
        and external endpoint backends (which never heartbeat and cannot be
        drained — DELETE is their remove path).

        Cleanup: the worker's deployment_instances AND its runner_upgrades rows
        have a plain FK (no ON DELETE cascade), so the route clears BOTH or the
        delete would 500 on the constraint; node_commands cascade at the DB.
        LLMM-4: runner_upgrades was the one referencing table this cleanup
        forgot, and nothing else ever deletes a RunnerUpgrade row — so any
        worker that had ever been through POST /api/workers/{id}/upgrade-runner
        was permanently undeletable (IntegrityError → unhandled 500, worker
        still present). No unload commands are enqueued — a removed worker is
        gone (or being wiped), and for a dead node the queue would never drain
        anyway; drain-first is where a live node's engines get unloaded.
        """
        from datetime import datetime, timezone

        from app.api._ids import parse_uuid
        from app.api.inventory import _is_fresh
        from app.models import DeploymentInstance, RunnerUpgrade, Worker

        with session_scope() as s:
            worker = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            if worker is None:
                raise HTTPException(status_code=404, detail="worker not found")
            is_external = bool((worker.labels or {}).get("external"))
            if (worker.status == "ready" and not is_external
                    and _is_fresh(worker, datetime.now(timezone.utc))):
                raise HTTPException(status_code=409, detail=(
                    "worker is ready and still reporting — drain it first "
                    "(POST /api/workers/{id}/drain) so its deployments relocate, "
                    "then remove it"))
            name = worker.name
            from app import liveness
            liveness.forget(worker.id)
            removed = (s.query(DeploymentInstance)
                       .filter(DeploymentInstance.worker_id == worker.id)
                       .delete(synchronize_session=False))
            # LLMM-4: same reason, same transaction — runner_upgrades.worker_id
            # is a plain FK with no ON DELETE cascade and no route of its own
            # that removes rows, so the history of a node's runner upgrades
            # would otherwise pin the worker row forever.
            upgrades_removed = (s.query(RunnerUpgrade)
                                .filter(RunnerUpgrade.worker_id == worker.id)
                                .delete(synchronize_session=False))
            s.delete(worker)

        # the fleet changed → rebuild the LiteLLM router from DB state so a
        # removed worker's endpoints leave routing immediately. Best-effort:
        # the row is already gone; a config-write hiccup must not 500 the
        # delete (rebuild via POST /api/router/rebuild).
        rebuilt = False
        try:
            from app.router_config import rebuild_from_state

            rebuild_from_state()
            rebuilt = True
        except Exception:  # pragma: no cover - defensive
            logger.warning("router rebuild after worker removal failed (non-fatal)",
                           exc_info=True)
        logger.info("worker %s removed (#594): %d instance row(s), %d runner-upgrade "
                    "row(s) cleared", name, removed, upgrades_removed)
        return {"status": "removed", "worker": name,
                "instances_removed": removed,
                "runner_upgrades_removed": upgrades_removed,
                "router_rebuilt": rebuilt}

    @approve_router.post("/api/workers/{worker_id}/undrain")
    def undrain(worker_id: str):
        """Return a drained worker to service. Only valid FROM draining — undrain
        is not a synonym for approve, and making it one would let it walk a
        pending worker through the #419 admission gate."""
        from app.api._ids import parse_uuid
        from app.models import Worker

        with session_scope() as s:
            worker = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            if worker is None:
                raise HTTPException(status_code=404, detail="worker not found")
            if worker.status != "draining":
                raise HTTPException(status_code=409, detail=(
                    f"worker is {worker.status!r}, not draining — undrain only "
                    f"reverses a drain"))
            worker.status = "ready"
            s.flush()
            # #2781: drain unloaded this worker's engines and deleted their
            # instance rows; put the deployments back, as /start does. Not
            # gated on ORCH_RECONCILE (observe, the default, never places).
            from datetime import datetime, timezone
            from app.api.inventory import replace_after_undrain
            placed = replace_after_undrain(s, datetime.now(timezone.utc))
            logger.info("worker %s back in service (undrained); %d deployment(s) "
                        "scheduled again, %d not placeable", worker.name,
                        len(placed["scheduled"]), len(placed["skipped"]))
            return {"status": "ready", "worker": worker.name,
                    "scheduled_deployments": placed["scheduled"],
                    "not_scheduled": placed["skipped"]}

    app.include_router(router)
    app.include_router(approve_router)
