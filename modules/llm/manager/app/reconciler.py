# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#263 SCH2 — desired-state reconcile loop + reschedule-on-node-loss.

SCH1 (already wired, `api/inventory.py::deploy`/`patch_deployment`/
`start_deployment`) places engines ONLY at deploy time. Nothing watches for a
worker dying afterwards — `_is_fresh`/`_display_status` mark a dead worker
"stale" for display, but its deployments' instance rows just sit there
forever, silently under-replicated. This module is the autonomous fix: a
background pass that compares desired replicas to what is ACTUALLY live on
FRESH, non-draining workers, and fills the gap.

Deliberately reuses the SCH1 admission + launch path wholesale — no new
placement or launch logic:

* ``app.api.inventory._place_within_budget`` — the SAME VRAM-admission gate
  (``_budget_check``) deploy/patch/start already enforce. A reschedule that
  would over-subscribe a target is refused exactly like a fresh deploy would
  be; it is never force-placed.
* ``app.api.inventory._enqueue_engine`` — the SAME ``load_engine`` command
  (#261) + optimistic 'scheduled' instance row every other placement path
  creates.

Safety (this is a workload-mover, so it is opt-in):

* ``ORCH_RECONCILE`` env — ``"enforce"`` actually reschedules; anything else
  (including unset — the default) is ``"observe"``: the decision logic still
  runs and is reported, but NOTHING is enqueued. A fresh install therefore
  never autonomously relaunches a workload until an operator opts in — the
  same shape as ``LLM_MANAGER_ENTITLEMENT_MODE`` (config.py) defaulting to
  the non-enforcing mode.
* A per-deployment cooldown (``LLM_MANAGER_RECONCILE_COOLDOWN_S``) so a
  flapping worker (heartbeat blinking in and out around the staleness
  window) cannot turn into a reschedule storm — a deployment that was just
  rescheduled is skipped for the cooldown window even if it is still short.

#917 — the four hardenings that must exist BEFORE ``ORCH_RECONCILE=enforce``
is switched on anywhere (the flip itself stays an operator act on a box; this
module cannot and does not perform it):

* **Startup grace** (``LLM_MANAGER_RECONCILE_STARTUP_GRACE_S``). Liveness is
  judged purely on heartbeat AGE, and the manager's own ``entrypoint.sh`` can
  take longer to come up (up to 60×2s waiting for Postgres, plus ``alembic
  upgrade head``) than the staleness window it then judges workers by —
  during which no worker CAN heartbeat, because the endpoint they post to is
  not serving. Without a grace, the first enforce pass after any manager
  restart sees the ENTIRE fleet as stale and reschedules every deployment off
  workers that are perfectly healthy and about to check back in. So no
  enforce ACTION happens until the manager has been up for longer than the
  staleness window (see ``_startup_grace_s``); the pass still runs and still
  reports, exactly as ``observe`` does.
* **Scale-down.** ``missing`` was clamped at zero, so a SURPLUS — the residue
  of a false-positive or self-healed reschedule — was never reclaimed and the
  deployment silently ran at 2× VRAM until someone audited by hand. A surplus
  now gets ``unload_engine`` for the extra instances.
* **Fleet-wide cap** (``LLM_MANAGER_RECONCILE_MAX_PER_PASS``). The
  per-deployment cooldown does nothing about a genuine multi-node outage,
  where ONE pass would enqueue every affected deployment's ``load_engine`` at
  once and stampede cold-loads onto the survivors. The pass now places at
  most N engines and lets the next pass continue.
* **Per-deployment isolation.** One deployment raising (a malformed
  ``worker_selector``, an entitlement/placement refusal) used to abort the
  whole pass — and, since the pass ran in ONE transaction, roll back every
  rescue decided before it, over and over. Each deployment is now wrapped and
  committed on its own.

Out of scope for this slice (tracked as #263's SCH3 follow-on): rolling
update / drain-then-start when a runner or artifact changes. This module
only restores REPLICA COUNT after a node loss — it never touches a healthy
placement.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_RECONCILE_MODES = ("observe", "enforce")

#: How often the background loop runs a pass. Independent of the worker
#: staleness window (LLM_MANAGER_WORKER_STALE_SECONDS, default 90s) — this
#: only needs to be frequent enough that a lost node's replacement lands in a
#: reasonable time, not tied to detection latency.
_RECONCILE_INTERVAL_S = float(os.environ.get("LLM_MANAGER_RECONCILE_INTERVAL_S", "30"))

#: Per-deployment cooldown: once a deployment has been (successfully)
#: rescheduled, skip it again for this many seconds even if it is still
#: short — gives a flapping node's heartbeat time to settle rather than
#: firing a fresh load_engine every pass.
_COOLDOWN_S = float(os.environ.get("LLM_MANAGER_RECONCILE_COOLDOWN_S", "60"))

def _max_per_pass_from_env(raw, default=2):
    """#917: parse `LLM_MANAGER_RECONCILE_MAX_PER_PASS`, tolerantly.

    #319's lesson, on a new knob: `os.environ.get(K, "2")` returns "" for a
    variable that EXISTS but is EMPTY — which is exactly what `VAR: "${VAR:-}"`
    in a compose file produces — and the naive `int()` on that raises at
    IMPORT, so the manager never starts. A mis-set tuning knob must cost the
    tuning, not the service.
    """
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError, AttributeError):
        return default


#: #917: the most engines ONE pass may place across the WHOLE fleet, 0 (or
#: below) = uncapped. The per-deployment cooldown is per-deployment by
#: construction and says nothing about a multi-node outage, where every
#: affected deployment is short at the same time and a single pass would fire
#: all their cold-loads at once — onto exactly the survivors that are already
#: carrying the load. Two per pass (one pass every `_RECONCILE_INTERVAL_S`)
#: restores a fleet steadily instead of stampeding it; an operator who wants
#: the old behaviour sets it high.
_MAX_PER_PASS = _max_per_pass_from_env(
    os.environ.get("LLM_MANAGER_RECONCILE_MAX_PER_PASS", "2"))

#: #917: when THIS manager process started. The startup grace is measured
#: against it — see `_startup_grace_s` and `reconcile_once`. Module import
#: happens during app startup, after `entrypoint.sh`'s Postgres wait and
#: migrations, so this is "when the manager began serving" to within a second.
_MANAGER_START = datetime.now(timezone.utc)

#: dep_id (str) -> the `now` of its last successful reschedule. Process-local,
#: like playground.py's identity cache — a manager restart just re-evaluates
#: from scratch, which is safe (idempotent).
_last_reschedule: dict[str, datetime] = {}


def _reconcile_mode() -> str:
    v = (os.environ.get("ORCH_RECONCILE") or "observe").strip().lower()
    return v if v in _RECONCILE_MODES else "observe"


def _worker_stale_seconds() -> float:
    """The staleness window placement judges workers by — read from the SAME
    env var `api/inventory.py` reads (`_WORKER_STALE_SECONDS`), not imported
    from it, so this stays free of the import cycle the rest of this module
    avoids by importing inventory lazily inside `reconcile_once`."""
    try:
        return float(os.environ.get("LLM_MANAGER_WORKER_STALE_SECONDS", "90"))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 90.0


def _startup_grace_s() -> float:
    """#917 [HIGH]: how long after manager start an enforce pass stays
    hands-off.

    Must be AT LEAST the staleness window, because that is exactly how long a
    healthy fleet can look dead: heartbeats stop landing while the manager is
    restarting (`entrypoint.sh` can spend 120s on the Postgres wait plus
    `alembic upgrade head` before anything serves), so at the first pass every
    worker's `last_heartbeat` is older than the window, through no fault of
    theirs. The default adds one reconcile interval on top, so a worker gets a
    full heartbeat round after the manager is back before anything is judged.

    An explicit `LLM_MANAGER_RECONCILE_STARTUP_GRACE_S` wins (0 disables the
    grace — a deliberate operator act, e.g. for a box where the manager and
    the workers are known to come up together).
    """
    raw = os.environ.get("LLM_MANAGER_RECONCILE_STARTUP_GRACE_S")
    if raw not in (None, ""):
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            logger.warning("ignoring unparseable LLM_MANAGER_RECONCILE_STARTUP_GRACE_S=%r", raw)
    return _worker_stale_seconds() + _RECONCILE_INTERVAL_S


def _in_cooldown(dep_id: str, now: datetime) -> bool:
    last = _last_reschedule.get(dep_id)
    return last is not None and (now - last).total_seconds() < _COOLDOWN_S


def _commit(session) -> None:
    """#917 [LOW]: commit what THIS deployment decided.

    The pass used to run as one transaction, so an exception on deployment #7
    rolled back the six rescues decided before it — every pass, for as long as
    #7 kept failing. Committing per deployment makes one bad deployment cost
    only itself. Guarded because the fake sessions the unit tier drives this
    with have no transaction to commit, and because a commit failure must not
    be more fatal than the exception handling around it already is."""
    commit = getattr(session, "commit", None)
    if commit is None:
        return
    try:
        commit()
    except Exception:  # noqa: BLE001 - never let a commit failure kill the pass
        logger.warning("reconcile: commit failed (non-fatal)", exc_info=True)


def _unload_surplus(session, dep, surplus) -> int:
    """#917 [MEDIUM]: give back the instances a deployment has BEYOND its
    replica count.

    Uses the same `unload_engine` command + row-deletion shape #1045's
    apply-params surplus path uses (`api/inventory.py`), so a reclaimed engine
    is torn down exactly the way every other surplus in this codebase is.
    External backends are skipped: they run no worker-agent, so there is no
    command channel to unload them through."""
    from app.api.commands import enqueue_command
    from app.api.inventory import _container
    from app.models import Worker

    unloaded = 0
    for inst in surplus:
        worker = session.get(Worker, inst.worker_id) if inst.worker_id else None
        container = _container(inst.endpoint)
        if worker is None or (worker.labels or {}).get("external") or not container:
            continue
        enqueue_command(session, worker.id, "unload_engine",
                        {"instance_id": container, "container": container})
        session.delete(inst)
        unloaded += 1
    return unloaded


def _is_externally_served(session, dep, instances) -> bool:
    """#1422: True when the deployment is an external-backend entry (#307):
    every instance it has sits on a worker labelled ``external`` (no agent to
    command), or it has no instances at all AND no weight source to launch
    from — the same "nothing to relaunch" the `start` route refuses with 409."""
    from app.models import Worker
    ext_seen = False
    for inst in instances:
        worker = session.get(Worker, inst.worker_id) if inst.worker_id else None
        if worker is None:
            continue
        if (getattr(worker, "labels", None) or {}).get("external"):
            ext_seen = True
        else:
            return False
    if ext_seen:
        return True
    return not instances and not (dep.source_files or dep.hf_repo)


#: The last observe-mode picture, so a permanently short deployment is logged
#: on every CHANGE of the picture, not every 30 s (#1422: 40 minutes of a
#: stale worker's deployments reading active/ready left no trace at all).
_last_observed_key: str | None = None


def _log_observed(summary: dict) -> None:
    global _last_observed_key
    key = json.dumps(sorted(
        (str(o.get("model_name")), o.get("missing", 0), o.get("surplus", 0))
        for o in summary.get("observed", [])))
    if key == _last_observed_key:
        return
    _last_observed_key = key
    if summary.get("observed"):
        logger.info(
            "reconcile (observe, #263 SCH2): %d deployment(s) out of sync and NOT "
            "acted on (ORCH_RECONCILE=%s): %s",
            len(summary["observed"]), summary.get("mode"),
            ", ".join(f"{o.get('model_name')} (missing={o.get('missing', 0)}, "
                      f"surplus={o.get('surplus', 0)})" for o in summary["observed"]))
    else:
        logger.info("reconcile (observe, #263 SCH2): fleet in sync again")


#: #1455: instance statuses a ghost row is NOT re-marked from — `failed` keeps
#: its failure reason, `lost` is already said. Neither counts as a replica.
_GHOST_TERMINAL_STATUS = frozenset({"failed", "lost"})


def _mark_lost(session, dep, ghosts, now: datetime) -> list[str]:
    """#1455 SCH3: flip the instance rows of a non-live worker to `lost` and
    say why in `detail`. Returns the instance ids marked (for the summary)."""
    marked: list[str] = []
    for inst, worker in ghosts:
        hb = getattr(worker, "last_heartbeat", None)
        if hb is not None:
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            age = f"{max(0, int((now - hb).total_seconds()))}s ago"
        else:
            age = "never"
        why = (f"worker {worker.name} is {worker.status}" if worker.status != "ready"
               else f"worker {worker.name} unreachable (last heartbeat {age})")
        inst.status = "lost"
        inst.detail = (f"{why} — not counted as a replica; the worker's next "
                       f"report restores or prunes this row (#1455 SCH3)")
        marked.append(str(inst.instance_id or inst.id))
        logger.info("reconcile (#1455 SCH3): %s instance %s on %s marked lost (%s)",
                    dep.model_name, inst.instance_id, worker.name, why)
    return marked


def reconcile_once(session, now: datetime, *, mode: str | None = None,
                   started_at: datetime | None = None) -> dict:
    """One reconcile pass. Pure(ish): takes the session and clock, mutates
    nothing but what it decides to reschedule or reclaim (and does that ONLY
    in "enforce" mode, and only past the #917 startup grace). Returns a
    summary for logging/testing:

    ``{"mode", "deployments_checked", "rescheduled": [...],
       "insufficient_capacity": [...], "skipped_cooldown": [...],
       "observed": [...], "scaled_down": [...], "deferred_cap": [...],
       "skipped_startup_grace": [...], "errors": [...],
       "startup_grace": bool}``

    ``rescheduled``/``insufficient_capacity``/``scaled_down`` are only
    populated in "enforce" mode; "observe" mode reports what it WOULD have
    done in ``observed`` instead, and never calls the placement/launch path
    at all.

    ``started_at`` is when the manager process began (default: this module's
    import time) and exists so the #917 startup grace can be driven by a test
    without waiting one out. During the grace an ``enforce`` pass behaves
    exactly like ``observe`` — it decides and reports, and touches nothing.
    """
    from app.api.inventory import (
        _enqueue_engine,
        _is_fresh,
        _place_within_budget,
        _served_name,
    )
    from app.models import Deployment, DeploymentInstance, Worker

    mode = mode if mode in _RECONCILE_MODES else _reconcile_mode()
    started_at = started_at if started_at is not None else _MANAGER_START
    grace_s = _startup_grace_s()
    # #917 [HIGH]: a `now` that is somehow BEFORE the manager's start (a
    # backwards clock step during startup) is inside the grace too — the safe
    # direction is always "wait and look again", never "act on a fleet whose
    # liveness we cannot judge yet".
    in_startup_grace = (now - started_at).total_seconds() < grace_s
    summary: dict = {
        "mode": mode,
        "deployments_checked": 0,
        "rescheduled": [],
        "insufficient_capacity": [],
        "skipped_cooldown": [],
        "observed": [],
        "scaled_down": [],
        "deferred_cap": [],
        "skipped_startup_grace": [],
        "errors": [],
        "skipped_external": [],
        "marked_lost": [],
        "startup_grace": bool(mode == "enforce" and in_startup_grace),
    }
    # Everything below treats "enforce but still warming up" as observe.
    acting = mode == "enforce" and not in_startup_grace
    placed_this_pass = 0

    deployments = (
        session.query(Deployment).filter(Deployment.status != "stopped").all()
    )
    for dep in deployments:
        summary["deployments_checked"] += 1
        dep_id = str(dep.id)
        ghost_marked = False   # #1455: rows flipped to `lost` this pass
        committed = False      # a rescue/scale-down commit already carried them
        # #917 [LOW]: ONE deployment's failure is one deployment's failure.
        try:
            instances = (
                session.query(DeploymentInstance)
                .filter(DeploymentInstance.deployment_id == dep.id)
                .all()
            )
            # #1422: a deployment served by an EXTERNAL backend (#307 — e.g. a
            # Mac running Ollama, registered with `external=True`) has no
            # container engine to relaunch: its instances live on a worker that
            # runs no agent, and it carries no weight source. Counting it as
            # "missing" made every enforce pass try to place a container engine
            # for it — and fail, or worse, succeed on a real worker.
            if _is_externally_served(session, dep, instances):
                summary["skipped_external"].append(dep_id)
                continue
            live = []
            ghosts = []   # #1455 SCH3: rows on a worker that is not live
            hosts: set[str] = set()
            for inst in instances:
                if inst.worker_id:
                    hosts.add(str(inst.worker_id))
                worker = session.get(Worker, inst.worker_id) if inst.worker_id else None
                if worker is None:
                    continue
                # Stale (dead heartbeat) or draining (or any non-"ready" status)
                # workers do NOT count — a dead node's instance row is a ghost,
                # not a live replica.
                if worker.status != "ready" or not _is_fresh(worker, now):
                    if inst.status not in _GHOST_TERMINAL_STATUS:
                        ghosts.append((inst, worker))
                    continue
                # #1455: a `lost` row on a worker that IS live again means the
                # worker has not (re-)reported that engine — register_worker
                # flips a reported one back to its reported status and prunes
                # an unreported one; until then it is not a replica either.
                if inst.status in _GHOST_TERMINAL_STATUS:
                    continue
                live.append(inst)

            desired = max(1, dep.replicas or 1)

            # --- GHOSTS (#1455 SCH3) --------------------------------------
            # SCH2 moved the PLACEMENT of a lost worker's replica; the STATE
            # stayed: the dead worker's instance row kept saying `ready` in
            # /api/deployments and the console (0.91 ↔ 0.175, 2026-09-05: two
            # `ready` rows per deployment, one on a worker with no heartbeat
            # for minutes). Say what the manager knows: the row is `lost`, with
            # the worker and the heartbeat age in `detail`. Not deleted — the
            # row is the audit trail, and the worker's next report is the
            # authority that either restores it (reported → its reported
            # status) or prunes it (unreported). Independent of whether a
            # replacement fits anywhere: the state is wrong either way.
            # Enforce-and-acting only, like every other mutation in this pass.
            if acting and ghosts:
                marked = _mark_lost(session, dep, ghosts, now)
                if marked:
                    ghost_marked = True
                    summary["marked_lost"].append(
                        {"deployment_id": dep_id, "model_name": dep.model_name,
                         "instances": marked})

            # --- SURPLUS (#917 [MEDIUM]) ---------------------------------
            # `missing = max(0, ...)` meant a deployment running MORE replicas
            # than it asks for was simply never looked at again: a
            # false-positive reschedule that then self-healed left the engine
            # it launched running, double-booking VRAM/compute silently until
            # someone noticed by hand.
            # #2019: a blue-green `apply-params` deliberately runs two engines
            # for one replica — the old one serving while the new one loads.
            # That is the surplus case below, and its ranking (ready first, then
            # newest) would keep the OLD instance and unload the NEW one, which
            # kills the overlap and leaves the params unapplied. The instance on
            # its way out is the one marked `retiring_since`; it is retired by
            # `api/workers.py::_retire_superseded_instances` the moment the
            # replacement reports ready, so it is not a replica to be counted
            # here — excluded for the SURPLUS question only, never for
            # `missing`, because an abandoned overlap whose old engine is still
            # serving must not read as a deployment with nothing running.
            overlapping = [di for di in live
                           if getattr(di, "retiring_since", None) is not None]
            surplus_live = [di for di in live if di not in overlapping]
            if overlapping and len(surplus_live) <= desired:
                if not acting:
                    summary["observed"].append(
                        {"deployment_id": dep_id, "model_name": dep.model_name,
                         "overlap_in_flight": len(overlapping)})
                continue

            if len(live) > desired:
                extra = len(live) - desired
                if not acting:
                    summary["observed"].append(
                        {"deployment_id": dep_id, "model_name": dep.model_name,
                         "surplus": extra})
                    if mode == "enforce":
                        summary["skipped_startup_grace"].append(dep_id)
                    continue
                # Same ranking #1045's apply-params surplus path uses: keep the
                # healthiest (ready first, then newest), give back the rest.
                ranked = sorted(
                    live,
                    key=lambda di: (0 if di.status == "ready" else 1,
                                    -(di.started_at.timestamp() if di.started_at else 0)))
                unloaded = _unload_surplus(session, dep, ranked[desired:])
                if unloaded:
                    summary["scaled_down"].append(
                        {"deployment_id": dep_id, "model_name": dep.model_name,
                         "unloaded": unloaded})
                    logger.info(
                        "reconcile (#263 SCH2 / #917): %s runs %d live replica(s) "
                        "for replicas=%d — unloading %d surplus instance(s)",
                        dep.model_name, len(live), desired, unloaded)
                    _commit(session)
                    committed = True
                continue

            missing = desired - len(live)
            if missing == 0:
                continue

            if not acting:
                summary["observed"].append(
                    {"deployment_id": dep_id, "model_name": dep.model_name,
                     "missing": missing}
                )
                if mode == "enforce":
                    # enforce was ASKED for; the startup grace is why nothing
                    # happened. Named separately from `observed` so an operator
                    # reading the log can tell "warming up" from "not enabled".
                    summary["skipped_startup_grace"].append(dep_id)
                continue

            if _in_cooldown(dep_id, now):
                summary["skipped_cooldown"].append(dep_id)
                continue

            # --- FLEET-WIDE CAP (#917 [MEDIUM]) ---------------------------
            if _MAX_PER_PASS > 0 and placed_this_pass >= _MAX_PER_PASS:
                summary["deferred_cap"].append(
                    {"deployment_id": dep_id, "model_name": dep.model_name,
                     "missing": missing})
                continue

            served = _served_name(session, dep)
            hardware = (dep.worker_selector or {}).get("hardware")
            avoid: set[str] = set(hosts)
            pending: dict[str, float] = {}
            for inst in live:
                if inst.worker_id:
                    pending[str(inst.worker_id)] = (
                        pending.get(str(inst.worker_id), 0.0) + float(dep.est_gb or 0)
                    )

            scheduled = 0
            no_room = False
            for _ in range(missing):
                if _MAX_PER_PASS > 0 and placed_this_pass >= _MAX_PER_PASS:
                    # The cap is FLEET-wide, so it also bounds how many
                    # replicas of ONE deployment a single pass cold-loads.
                    break
                worker, reason = _place_within_budget(
                    session, now, dep=dep, est_gb=dep.est_gb, hardware=hardware,
                    avoid=avoid, pending=pending, force=False,
                )
                if worker is None:
                    no_room = no_room or (reason == "no_room")
                    break
                _enqueue_engine(session, dep, worker, served, dep.source_files,
                                dep.hf_repo, dep.params, dep.task)
                avoid.add(str(worker.id))
                scheduled += 1
                placed_this_pass += 1
                pending[str(worker.id)] = (
                    pending.get(str(worker.id), 0.0) + float(dep.est_gb or 0)
                )

            if scheduled:
                _last_reschedule[dep_id] = now
                summary["rescheduled"].append(
                    {"deployment_id": dep_id, "model_name": dep.model_name,
                     "scheduled": scheduled}
                )
                logger.info(
                    "reconcile (#263 SCH2): %s short %d replica(s), rescheduled %d "
                    "(no_room=%s)", dep.model_name, missing, scheduled, no_room,
                )
                _commit(session)
                committed = True
            else:
                summary["insufficient_capacity"].append(
                    {"deployment_id": dep_id, "model_name": dep.model_name,
                     "missing": missing, "no_room": no_room}
                )
                logger.warning(
                    "reconcile (#263 SCH2): %s short %d replica(s), no fitting "
                    "worker available (no_room=%s) — will retry next pass",
                    dep.model_name, missing, no_room,
                )
        except Exception as exc:  # noqa: BLE001 - see the try's comment
            summary["errors"].append({"deployment_id": dep_id,
                                      "model_name": getattr(dep, "model_name", None),
                                      "error": str(exc)})
            logger.warning(
                "reconcile (#263 SCH2 / #917): deployment %s (%s) failed this pass — "
                "carrying on with the rest", dep_id, getattr(dep, "model_name", None),
                exc_info=True)
        finally:
            # #1455: a marking no rescue/scale-down commit carried (no room,
            # cooldown, cap, or a later exception) is committed on its own —
            # "one commit per deployment" (#917) stays the rule either way.
            if ghost_marked and not committed:
                _commit(session)

    if summary["startup_grace"] and (summary["skipped_startup_grace"]
                                     or summary["observed"]):
        logger.info(
            "reconcile (#917): ORCH_RECONCILE=enforce, but the manager has been up "
            "for %.0fs of a %.0fs startup grace — deciding and reporting only. A "
            "fleet cannot heartbeat while this process is restarting, so acting "
            "now would reschedule healthy workers.",
            (now - started_at).total_seconds(), grace_s)

    _log_observed(summary)
    return summary


def _reconcile_pass() -> dict:
    """Owns its own session (mirrors retention.py's `prune_once`) so the
    daemon loop below never touches the DB directly — patching this one
    function is enough to test the loop's failure-survival without a live
    database."""
    from app.db import session_scope

    with session_scope() as s:
        return reconcile_once(s, datetime.now(timezone.utc))


def start_reconcile_loop() -> threading.Thread:
    """Background reconcile daemon; first pass runs immediately (mirrors
    retention.py's `start_retention_loop`). Always started — the ORCH_RECONCILE
    gate lives INSIDE `reconcile_once` (default "observe": decide + log,
    never enqueue), so an operator sees reconcile activity in the logs before
    ever opting into it moving anything.

    #917: the immediate first pass is deliberately KEPT — it is the startup
    GRACE inside `reconcile_once`, not a delayed first pass, that makes an
    enforce-mode manager safe across its own restart. Reporting from the first
    second is useful; acting in the first two minutes is what was dangerous."""
    def _loop():
        while True:
            try:
                summary = _reconcile_pass()
                if summary and (summary.get("rescheduled") or summary.get("observed")
                                or summary.get("insufficient_capacity")):
                    logger.info("reconcile pass (#263 SCH2): %s", summary)
            except Exception:  # noqa: BLE001 — reconcile must never kill the app
                logger.warning("reconcile pass failed (non-fatal)", exc_info=True)
            _stop.wait(_RECONCILE_INTERVAL_S)
            if _stop.is_set():
                return

    _stop = threading.Event()
    t = threading.Thread(target=_loop, name="reconcile-loop", daemon=True)
    t._stop_event = _stop  # tests can stop the loop
    t.start()
    return t
