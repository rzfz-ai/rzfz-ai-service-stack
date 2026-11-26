# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#549 R3 — the per-node runner-upgrade sequence.

Two shapes, chosen per switch by whether the weights fit into the worker TWICE
(#1867). The overlap is the default when it is provably possible:

    BLUE-GREEN (weights fit twice — no outage)
    deploy_runner (pull) → capture → relaunch ALONGSIDE → health-gate
    → router-gate → retire the old container → done

    TEAR-DOWN (footprint unknown, or too big — an outage the length of a load)
    deploy_runner (pull) → capture → drain → undrain + repin + relaunch
    → health-gate → router-gate → done

The tear-down shape was the only one, and it was measured on 0.91 under
sustained load: **159 of 290 requests failed (54.8 %)**. The pull phase was
already drain-free — 0 failures while a multi-GB image came down — which is
what shows the tear-down is the outage, not the upgrade. Blue-green is a
REORDERING of the same steps; nothing in it is a new mechanism:

* container names are minted per instance (`engine-<model>-<uuid6>`), so the
  old and the new never collide;
* two engines of ONE deployment on one worker is the shipped replica path
  (node-agent's `engine_bases_for_model` documents it);
* "do the weights fit twice" is `switch_interruption`, on the dynamic VRAM leg
  only and only with a known footprint plus a fresh reading.

Which shape ran is on the status as `overlapped`, because the two mean
different things for the models on that worker.

One node per call, on purpose: the single-node sequence is the composable
primitive, and the fleet-wide rolling upgrade is the console (R4) or the
operator calling it node by node — each with its own health gate, which is what
"rolling" means. Baking the fleet loop into the manager first would have hidden
the hard part (failure-midway semantics) inside a bigger loop.

Ordering that took deciding, recorded here rather than rediscovered:

* **The PULL happens before anything on the node is touched** (#1677 (a),
  operator decision 2026-09-08). This reverses the original order,
  and the original reasoning is kept here because it was not wrong — it was
  weighed differently. Draining first protected the pull: no new placement can
  land on the node while a multi-GB image comes down, so the upgrade cannot
  miss one. The price of that case is a deployment that restarts once, minutes
  after it started — visible, annoying, self-repairing.

  The price of draining first when the pull FAILS is a node that is drained,
  its engines stopped, serving nothing, until a human notices. And the pull
  fails on every box with the shipped values (part 1 of #1677), so the
  expensive case was not the edge — it was the normal one. A pull does not
  touch the running engine; only the relaunch does, and the drain belongs
  immediately before THAT.

  The original objection is not argued away, it is relocated: a deployment
  placed on the node during the pull is picked up by the relaunch, because the
  capture happens after the pull and immediately before the drain — the upgrade
  acts on what is on the node NOW, not on a list from before the download.

* **Undrain precedes the RELAUNCH.** A draining worker's registration reports
  never re-create instances (the drain invariant, #261-C2) — so relaunching
  while drained would leave the health gate blind: the new engines would come
  up and their self-registration would be skipped forever.
* **Repin happens only after the pull succeeds.** A failed pull leaves every
  deployment pinned exactly as it was — rollback from `deploying` is
  "relaunch what was captured", nothing more.
* **On the BLUE-GREEN path, failure costs nothing** (#1867). Nothing is torn
  down until the cutover, so a rejected relaunch, a timeout or a manager
  restart leaves the worker serving on the OLD runner. The worker is therefore
  NOT drained on that path — draining there would manufacture the outage the
  overlap exists to avoid. The paragraph below is the tear-down path.

* **A failed upgrade leaves the worker SERVING** (#3106, superseding the
  earlier "failure leaves the worker drained"). That rule kept traffic off a
  node whose runner state was unknown — but after #3106 the state IS known:
  every engine that fails, or is refused, is put back on the runner it ran
  before, and the node's report says what serves. Leaving it drained only
  produced a node serving nothing and a status FAIL nothing cleared (box pass,
  2026-11-08). A failure BEFORE the drain — a failed pull, a refused
  pre-flight — leaves the node untouched and serving.

* **An engine is never torn down unless it can be started again** (#3106).
  Before the drain, every captured deployment must carry a weight file the
  node can load (`relaunch_source_problem`); otherwise the upgrade is refused
  and the node is untouched. Refused, not narrowed: the drain is worker-wide.
  And a relaunch that fails anyway — the node refuses the load, the engine
  dies, or it is not ready by the deadline — is ROLLED BACK for that engine:
  previous pin, previous settings, half-started container removed; the
  upgrade ends `failed` and says what was rolled back. A relaunch the
  manager's own funnel refuses (relay, entitlement) is rolled back the same
  way. "Previous runner" is the image the engine container REALLY ran, as the
  node reports it — a row with no pin runs the node's default, which moves
  with the agent.

* **Rollback drains only when something was captured.** Draining a node in
  order to relaunch nothing is never the right action, and after #1677 (a) it
  is reachable: a failed pull captures nothing, so an operator reaching for
  rollback would have had every running engine unloaded and nothing put back —
  the rollback would CREATE the outage it exists to undo.

Advancement is EVENT-driven: command results and registration reports call
``advance_runner_upgrade``. No background thread — nothing to die silently
(#364's lesson from the node applies to the manager too). Timeouts are checked
lazily at the same points and on reads.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.authz import Role, require_role
from app.db import session_scope

logger = logging.getLogger("orchestrator.runner_upgrade")

#: Generous ceilings; a lazy timeout is a diagnosis aid, not a scheduler.
DEPLOY_TIMEOUT = timedelta(minutes=30)      # a cold multi-GB image pull
RELAUNCH_TIMEOUT = timedelta(minutes=15)    # engines up + first ready report

ACTIVE_STATES = ("deploying", "relaunching")


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class UpgradeRequest(BaseModel):
    image: str


def _active_for(s, worker_id):
    from app.models import RunnerUpgrade

    return (s.query(RunnerUpgrade)
            .filter(RunnerUpgrade.worker_id == worker_id,
                    RunnerUpgrade.state.in_(ACTIVE_STATES))
            .one_or_none())


def _fail(up, reason: str, worker=None) -> None:
    """#1677 (a): say what state the NODE is actually in.

    Before the pull moved to the front, every failure was a failure after the
    drain, so "worker stays drained" was always true. It is not any more — a
    failed pull leaves the node serving — and a log line that claims a drained
    node when the node is fine sends an operator looking for an outage that
    does not exist. So the line reports the status instead of asserting it.
    """
    up.state = "failed"
    up.error = reason
    up.updated_at = _now()
    status = getattr(worker, "status", None)
    where = ("worker stays drained — operator decision: rollback or investigate"
             if status == "draining" else
             f"worker status {status!r} — nothing on the node was changed"
             if status is not None else "worker state not inspected here")
    logger.error("runner upgrade %s FAILED: %s (%s)", up.id, reason, where)


def _container_of(endpoint):
    """The engine container name behind an instance endpoint.

    Thin re-export of `inventory._container` so `_capture` does not grow an
    import of its own — and so the two places that turn an endpoint into a
    container name (the drain, the capture) demonstrably use ONE rule. Two
    spellings of "which container is this" is the two-authorities bug on the
    identity axis, and the cutover unloads by that name.
    """
    from app.api.inventory import _container
    return _container(endpoint)


#: Where post-install publishes the init-built runner images inside the hub
#: (`<registry>/runners/<local-name>:<tag>`); the node-agent's
#: `RUNNER_REPO_PREFIX` names the same literal.
_RUNNER_REPO_PREFIX = "runners"


def _image_leaf(ref: str) -> str:
    """``name:tag`` of an image reference without registry host, repository
    path and digest (the node-agent's `drivers.base._image_leaf`, which the
    version probe uses to find the same build under another name)."""
    ref = str(ref or "").split("@", 1)[0]
    leaf = ref.rsplit("/", 1)[-1]
    return leaf if ":" in leaf else f"{leaf}:latest"


def launchable_prior_image(prior, upgrade_image):
    """The runner reference a rollback may pin, given what the engine ran
    before (``prior``) and the image this upgrade pulled (``upgrade_image``);
    None means "no pin" — the node starts its own default runner (#3112).

    A node launches a pinned runner only from its ONE allowed registry and
    refuses everything else at `load_engine` ("runner images come from … only").
    What an engine container reports running is often the LOCAL, un-prefixed
    tag (`llama-runner:b12110-cuda13.0-sm121a`): init-built runners and adopted,
    hand-started engines look like that. Measured on the GB10: three of four
    captures were such a ref, the rollback pinned one, the node refused it, and
    the pin stayed on the row — every later start of that deployment hit the
    same refusal.

    The upgrade image is the reliable coordinate: the node pulled it a moment
    ago (the capture runs only after the pull succeeded), so its registry host
    IS the node's allowed registry. A prior on that host is kept. Anything else
    — a bare local tag, or a ref on a registry the node no longer allows — is
    mapped to its hub twin `<host>/runners/<name>:<tag>`, the place post-install
    publishes the same build under the same tag. When no twin can be formed,
    None: an unpinned relaunch the node accepts beats a pin it refuses.
    """
    if not prior:
        return None
    from app.api.inventory import registry_qualified_runner_image, valid_runner_image

    if not registry_qualified_runner_image(upgrade_image or ""):
        return None
    host = str(upgrade_image).partition("/")[0]
    prior = str(prior)
    if registry_qualified_runner_image(prior) and prior.partition("/")[0] == host:
        return prior
    leaf = _image_leaf(prior)
    if leaf.split(":", 1)[0] in ("", "sha256"):      # an image id, not a name
        return None
    twin = f"{host}/{_RUNNER_REPO_PREFIX}/{leaf}"
    return twin if registry_qualified_runner_image(twin) and valid_runner_image(twin) else None


def _rollback_pin(entry, up, dep=None):
    """The pin a rollback restores for one capture entry — always one the node
    accepts, or None (#3112). Applied again at rollback time, not only at
    capture, so an upgrade captured before this rule existed is safe too.

    #3403: a deployment on the EXPERIMENTAL CHANNEL without a pin of its own
    gets None back — never the image its engine reported. That image is the
    channel's build of today; written as a pin it would outlive the next bump
    of the channel (the pin wins over the channel, and the old tag is no longer
    an experimental one, so a later upgrade repins it to the stable runner,
    where its model does not load). Decided from the capture's record
    (`prior_image_source: channel`) and, for a capture taken before this rule,
    from the row itself."""
    if entry.get("prior_image_source") == "channel":
        return None
    if (dep is not None and getattr(dep, "runner_channel", None) == "experimental"
            and entry.get("prior_image_source") != "pin"):
        return None
    return launchable_prior_image(entry.get("prior_runner_image"),
                                  getattr(up, "image", None))


def _capture(s, worker, upgrade_image=None) -> list:
    """What this worker is serving, and what each deployment is pinned to.

    Taken immediately BEFORE the drain, which deletes the instance rows — after
    it, nothing can be asked any more. #1677 (a) moved the call site from the
    start endpoint to the moment the pull succeeds, and that is not only a
    reordering: a deployment placed on the node WHILE the image was coming down
    is now in the capture, instead of being taken down by the drain and never
    relaunched.
    """
    from app.models import Deployment, DeploymentInstance

    rows = (s.query(DeploymentInstance)
            .filter(DeploymentInstance.worker_id == worker.id).all())
    # #1867: EVERY container serving each deployment on this worker, not just
    # the first row's. The entry list is deduped per deployment (the relaunch
    # is per deployment, once), but the CONTAINER set must be complete: a
    # deployment with two replicas on this box has two, and a blue-green gate
    # that only knew the first would count the SECOND EXISTING replica as "the
    # new engine" and pass before anything had loaded. That is the same trap
    # the container-keyed gate exists to close, one level in.
    by_dep = {}
    for di in rows:
        name = _container_of(di.endpoint)
        if name:
            by_dep.setdefault(str(di.deployment_id), []).append(name)
    # #3106: the image each deployment's engine container REALLY runs, as the
    # node reports it. A row without a pin runs the node's default runner, and
    # that default moves with the agent: on the box both rows were empty, the
    # capture said "previous image: none", and the node's default was already
    # the new build — a rollback had nothing to go back to. A ready instance's
    # report is preferred over a loading one's.
    ran = {}
    for di in sorted(rows, key=lambda r: r.status != "ready"):
        img = getattr(di, "runner_image", None)
        if img:
            ran.setdefault(str(di.deployment_id), img)

    captured, seen = [], set()
    for di in rows:
        if di.deployment_id in seen:
            continue
        seen.add(di.deployment_id)
        dep = s.get(Deployment, di.deployment_id)
        pin = dep.runner_image if dep else None
        reported = pin or ran.get(str(di.deployment_id))
        # #3403: a deployment on the experimental channel follows the channel,
        # not the image it happens to run — it is recorded as such, so no
        # rollback turns it into a pin, and the stable switch does not decide
        # on it (see `advance_runner_upgrade`).
        channel = getattr(dep, "runner_channel", None) if dep else None
        from app.api.inventory import runner_is_experimental
        experimental = bool(dep) and runner_is_experimental(
            channel=channel, runner_image=pin, tags=_EXP_TAGS())
        # #3112: recorded as the reference the node will ACCEPT as a pin (the
        # hub twin of a local tag), or None when there is none. The raw value
        # stays beside it for the record.
        prior = (launchable_prior_image(reported, upgrade_image)
                 if upgrade_image else reported)
        on_channel = channel == "experimental" and not pin
        captured.append({"deployment_id": str(di.deployment_id),
                         "prior_runner_image": None if on_channel else prior,
                         "prior_image_reported": reported,
                         # where `prior_runner_image` came from: the row's pin,
                         # the engine's own report, or nowhere (an agent too old
                         # to report it — the rollback then gets the node default)
                         # #3403: `channel` — no pin, the experimental channel.
                         "prior_image_source": ("channel" if on_channel else "pin" if pin
                                                else "engine" if reported else "unknown"),
                         "experimental": experimental,
                         # #1867: the container serving this deployment RIGHT
                         # NOW. The drain path never needed it (it unloads
                         # before this record is used again), but a blue-green
                         # switch does: the cutover has to retire exactly the
                         # OLD container, and after the relaunch there are two
                         # of them. Recorded at capture time for the same
                         # reason `was_running` is — after the intervention
                         # nothing can be asked any more.
                         "containers": sorted(
                             set(by_dep.get(str(di.deployment_id)) or [])),
                         # #1494 re-review, finding (b): an instance that was
                         # READY here has demonstrably found its weights,
                         # whatever the stored source_files column says.
                         # Recorded at capture time, because after the drain
                         # nothing can be asked any more. Absent on an upgrade
                         # captured before this change → treated as not proven,
                         # i.e. the strict path.
                         "was_running": di.status == "ready"})
    return captured


def _EXP_TAGS():
    from app.api.inventory import experimental_runner_tags
    return experimental_runner_tags()


def _decides(e) -> bool:
    """#3403: does this capture entry decide the switch's outcome? An engine on
    the experimental runner does not: the switch moves the STABLE runner, and
    "experimental" carries no stability promise. Its trouble is reported as a
    warning naming the deployment and its rollback (`warnings` on the status)."""
    return not e.get("experimental")


def _retiring(up) -> set:
    """The OLD containers an overlap cutover retires — never an experimental
    engine's: on the overlap path it is not relaunched, so its old container is
    the one that keeps serving (#3403)."""
    return {name for e in (up.captured or []) if e.get("overlap") and _decides(e)
            for name in (e.get("containers") or [])}


def _experimental_warning(s, e, why) -> str:
    from app.models import Deployment
    dep = s.get(Deployment, e.get("deployment_id"))
    name = dep.model_name if dep is not None else e.get("deployment_id")
    return (f"{name} runs on the experimental runner and did not come up again after "
            f"the stable runner switch ({why}). It does not fail the switch. Start it "
            f"again (Models -> {name} -> Start), or move that one deployment back to the "
            f"stable runner (Models -> {name} -> Back to stable runner).")


def _note_experimental(s, up, entries_whys) -> None:
    """Record a warning on each experimental capture entry (re-assigned: the
    column is plain JSONB). Logged too."""
    whys = {e.get("deployment_id"): why for e, why in entries_whys}
    if not whys:
        return
    out = []
    for e in up.captured or []:
        if e.get("deployment_id") in whys and not e.get("warning"):
            w = _experimental_warning(s, e, whys[e.get("deployment_id")])
            logger.warning("runner upgrade %s: %s", up.id, w)
            e = dict(e, warning=w)
        out.append(e)
    up.captured = out


#: The tag a deployment created from a node's registration report carries
#: (`workers.DISCOVERED_TAG`). Spelled here rather than imported so this module
#: keeps importing nothing from `workers` at load time.
_DISCOVERED_TAG = "discovered-from-node"


def relaunch_source_problem(dep) -> str | None:
    """Why the node could NOT start this deployment's engine again from what
    the manager knows about it, or None when it can.

    A runner upgrade stops every engine on the worker and starts it again
    (#3106). The start is a `load_engine` built from the deployment ROW, and
    every llama.cpp driver on the node refuses a load without a `.gguf` in
    `files[]` ("requires a .gguf weights file in files[]; got []"). The engine
    having run a moment ago proves nothing about that: the node needs the
    file NAME, and a row that never had one cannot hand it over. Measured on
    the GB10: an adopted embedding engine was torn down, its relaunch was
    refused, and the upgrade sat in `relaunching` until its deadline with the
    old container gone.

    This is why the #1494 (b) `was_running` exemption is not a weight source:
    it skipped the MANAGER's check and handed the node the same empty list the
    node always refuses. The check here is what the node will do, asked before
    anything is torn down.

    Only llama.cpp needs the file, and an engine the node reported without
    naming one (`unknown`, or empty) is treated as llama.cpp — runner upgrades
    exist only for llama.cpp runners, so that is what it is.
    """
    from app.api.inventory import canonical_engine

    engine = canonical_engine(getattr(dep, "engine", "") or "")
    if engine not in ("llamacpp", "", "unknown"):
        return None
    files = [str(f) for f in (getattr(dep, "source_files", None) or [])]
    if any(f.lower().endswith(".gguf")
           and "mmproj" not in f.rsplit("/", 1)[-1].lower() for f in files):
        return None
    name = getattr(dep, "model_name", None) or str(dep.id)
    adopted = _DISCOVERED_TAG in (getattr(dep, "tags", None) or [])
    if adopted:
        origin = ("it was adopted from what a node reported it was already "
                  "serving, so the manager has no .gguf weights file for it")
    else:
        origin = f"its stored source_files name no .gguf weights file (got {files!r})"
    return (f"deployment {dep.id} ({name}) cannot be relaunched on a new runner: "
            f"{origin}. A runner upgrade stops the engine and starts it again, "
            f"and the node cannot start it without that file. Redeploy {name} "
            f"by repository from the catalog first, then upgrade the runner."
            + (" On the same worker, the adopted engine keeps serving until the "
               "redeployed one is ready and is then stopped." if adopted else ""))


def _relaunch_blockers(s, deployment_ids) -> list:
    """`relaunch_source_problem` for each of these deployments; the ones that
    have a problem, as sentences. Deployments that no longer exist are fine —
    there is nothing to relaunch."""
    from app.models import Deployment

    out, seen = [], set()
    for dep_id in deployment_ids:
        if str(dep_id) in seen:
            continue
        seen.add(str(dep_id))
        dep = s.get(Deployment, dep_id)
        if dep is None:
            continue
        problem = relaunch_source_problem(dep)
        if problem:
            out.append(problem)
    return out


def _relaunch_runner(dep, pin_image, exp_tags=None):
    """The runner pin ``dep`` would carry when relaunched by ``_relaunch`` with
    ``pin_image`` (None: kept as it is): an engine on the experimental runner
    keeps its own, every other one is repinned (#3403)."""
    from app.api.inventory import runner_is_experimental
    if pin_image is None or runner_is_experimental(
            channel=getattr(dep, "runner_channel", None),
            runner_image=getattr(dep, "runner_image", None), tags=exp_tags):
        return getattr(dep, "runner_image", None)
    return pin_image


def _runner_refusal(worker, dep, runner_image) -> str | None:
    """#3436 L-21: the two runner-channel gates of the engine funnel
    (`_enqueue_engine`), asked without writing: does this worker's agent serve
    the channel the relaunch needs, and has the box opted in to the
    experimental runner it may run on. The reason, or None."""
    from app.api.inventory import (_channel_needed, _channel_supported,
                                   _channel_unsupported_detail,
                                   require_experimental_runner_enabled)
    need = _channel_needed(channel=getattr(dep, "runner_channel", None),
                           runner_image=runner_image)
    if not _channel_supported(worker, need):
        return _channel_unsupported_detail(worker, need)
    try:
        require_experimental_runner_enabled(channel=getattr(dep, "runner_channel", None),
                                            runner_image=runner_image,
                                            model=getattr(dep, "model_name", None))
    except HTTPException as exc:
        detail = exc.detail
        return (detail.get("notice") if isinstance(detail, dict) else None) or str(detail)
    return None


def _channel_blockers(s, worker, deployment_ids, pin_image) -> list:
    """#3436 L-21: for the WHOLE set, before anything is stopped — the switch
    image itself on a box that has not opted in to the experimental runner,
    and every deployment the relaunch would refuse on its runner channel.
    Sentences naming each, like `_relaunch_blockers`."""
    from app.api.inventory import experimental_runner_tags, require_experimental_runner_enabled
    from app.models import Deployment

    out = []
    try:
        require_experimental_runner_enabled(channel=None, runner_image=pin_image)
    except HTTPException as exc:
        notice = exc.detail.get("notice") if isinstance(exc.detail, dict) else exc.detail
        out.append(f"the switch image {pin_image} is the experimental runner: {notice}")
        return out
    exp_tags = experimental_runner_tags()
    seen = set()
    for dep_id in deployment_ids:
        if str(dep_id) in seen:
            continue
        seen.add(str(dep_id))
        dep = s.get(Deployment, dep_id)
        if dep is None:
            continue
        why = _runner_refusal(worker, dep, _relaunch_runner(dep, pin_image, exp_tags))
        if why:
            out.append(f"deployment {dep.id} ({dep.model_name}) could not be started "
                       f"again: {why}")
    return out


def _relaunch(s, worker, captured, *, pin_image=None, launched_ids=None) -> int:
    """Re-launch the captured deployments on this worker. ``pin_image`` set →
    repin each first (the upgrade path); None → keep whatever pin each carries
    (the rollback path restores priors before calling this).

    #1494 review, finding 1: `_enqueue_engine` REJECTS a deployment whose
    artifacts cannot run on its engine (422). This function is the sixth way
    into that funnel, and it is the only one that is not a request handler —
    so the rejection is checked for the WHOLE set before anything is enqueued.
    Two reasons:

    * all-or-nothing. Rejecting halfway used to leave the earlier deployments
      enqueued and the later ones not, on a worker that had already been
      undrained. Nothing rolls that back: `session_scope` only unwinds when
      the exception escapes the request, and on the upgrade path it does not
      (see `advance_runner_upgrade`, which turns it into a failed upgrade).
    * the reason names the deployment. "one of the captured deployments" is
      not something an operator can act on.
    """
    from app.api.inventory import (_assert_relay_single_engine, _entitlement_gate,
                                   _enqueue_engine)
    from app.models import Deployment, Model

    rows = []
    for entry in captured or []:
        dep = s.get(Deployment, entry["deployment_id"])
        if dep is None:                        # undeployed mid-upgrade — fine
            continue
        served = (s.get(Model, dep.model_id).name if dep.model_id else dep.model_name)
        # #1494 re-review, finding (b): a deployment we CAPTURED off this worker
        # was running here a moment ago — the drain took it down. Its weights
        # are proven by the fact that it ran, regardless of what the stored
        # `source_files` column says (a row deployed before the door existed
        # carries `[]`). So the weights gate is skipped for the relaunch, and
        # only for the relaunch.
        rows.append((dep, served, bool(entry.get("was_running"))))

    # Every rejection `_enqueue_engine` can raise, asked for the WHOLE set
    # before the first write. #1494 re-review, finding 2: checking only the
    # weights made "all or nothing" true for one reason out of three — measured
    # against the real funnel, a relay conflict on the second deployment still
    # left one command enqueued, one instance row created and BOTH deployments
    # repinned before the 409. These are the same three gates, in the same
    # order, and they are pure: none of them writes.
    for dep, served, was_running in rows:
        try:
            _entitlement_gate(None, f"relaunch engine capacity for {dep.model_name}")
            _assert_relay_single_engine(s, dep, worker)
        except HTTPException as exc:
            raise HTTPException(status_code=exc.status_code, detail=(
                f"deployment {dep.id} ({served}) cannot be relaunched: "
                f"{exc.detail}")) from exc
        # #3106: what the NODE will need to start it, whether or not it was
        # running. `was_running` used to skip this (#1494 (b)), and the node
        # then refused the empty file list anyway — after the drain.
        problem = relaunch_source_problem(dep)
        if problem:
            raise HTTPException(status_code=422, detail=problem)

    from app.api.inventory import experimental_runner_tags, runner_is_experimental

    exp_tags = experimental_runner_tags() if pin_image is not None else set()
    # #3436 L-21: and the two runner-channel gates, for the whole set and on
    # the runner each engine WILL run (after the repin below): an agent that
    # lost the channel, or an experimental runner the box has not opted in to,
    # used to be refused by the funnel halfway — after the first engines were
    # enqueued and repinned.
    for dep, served, _was_running in rows:
        why = _runner_refusal(worker, dep, _relaunch_runner(dep, pin_image, exp_tags))
        if why:
            raise HTTPException(status_code=409, detail=(
                f"deployment {dep.id} ({served}) cannot be relaunched: {why}"))
    launched = 0
    for dep, served, was_running in rows:
        # #3403: a runner upgrade moves the node's STABLE runner. A deployment
        # on the experimental runner — by its channel, or pinned to an
        # experimental image — is relaunched where it is, not re-pinned onto the
        # upgrade image: that build may not know its architecture at all
        # (Inkling 2 Mini on b12110 does not load).
        if pin_image is not None and runner_is_experimental(
                channel=getattr(dep, "runner_channel", None),
                runner_image=dep.runner_image, tags=exp_tags):
            pass
        elif pin_image is not None:
            dep.runner_image = pin_image
        iid = _enqueue_engine(s, dep, worker, served, dep.source_files, dep.hf_repo,
                              dep.params, dep.task, weights_already_proven=was_running)
        if launched_ids is not None:
            launched_ids.setdefault(str(dep.id), []).append(iid)
        launched += 1
    return launched



def _router_serves(s, deployment_ids) -> bool:
    """Would the router config, rendered from THIS session, carry every one of
    these deployments? (#266)

    Two things are deliberately kept apart here.

    **The gate is the CONTENT.** `generate_from_db` applies the routing rules —
    only `ready` instances, no endpointless rows, no dead relay workers — so
    asking it is asking "is this deployment reachable through the gateway",
    which is what an operator means by "the switch is over". Rendering needs no
    I/O and cannot fail for environmental reasons.

    **The write is a SIDE ERRAND, and non-fatal**, exactly as it already is
    after a registration (`workers.py`: "router rebuild after registration
    failed (non-fatal)"). The fleet changed, so the file should be refreshed
    here too — but a manager whose router path is not writable must not sit in
    `relaunching` for ever over it. The registration path rewrites it on the
    next report anyway.

    What this does NOT cover, said out loud: LiteLLM re-reads the file on its
    own schedule (#308). A residual lag between a correct config and a serving
    router remains, and closing it needs a signal from the router, not another
    check here.
    """
    from app.models import Deployment

    names = []
    for dep_id in deployment_ids:
        dep = s.get(Deployment, dep_id)
        if dep is not None:
            names.append(dep.model_name)
    if not names:
        return True

    from app.config import get_settings
    from app.router_config import render_from_session

    settings = get_settings()
    # The shared render path: the same config (operator's algorithm included)
    # a rebuild would write, so this errand never flips it (#2521 follow-up).
    config = render_from_session(s)
    served = {e.get("model_name") for e in (config.get("model_list") or [])
              if (e.get("litellm_params") or {}).get("api_base")}
    missing = sorted(n for n in names if n not in served)
    if missing:
        logger.info("runner upgrade: the router config would not carry %s yet "
                    "— staying in relaunching", missing)
        return False

    # The errand: refresh the file the router reads. Never fatal.
    try:
        from app.router_config import write_router_config
        write_router_config(settings.router_config_path, config)
    except Exception:
        logger.warning("runner upgrade: router config rewrite failed "
                       "(non-fatal) — the next registration rewrites it",
                       exc_info=True)
    return True


def switch_interruption(s, worker, deployments=None) -> dict:
    """Will this runner switch INTERRUPT serving, and why? (#1867)

    The switch tears the engine down and starts a new one on the new runner, so
    a single-replica model is unserved for the length of a model load. Measured
    under sustained /v1 load on 0.91: 159 of 290 requests failed (54.8%). The
    pull phase before it is already drain-free — the old instance keeps serving
    while the image is fetched — which is what shows the tear-down is the
    outage, not the upgrade as such.

    A blue-green relaunch (start the new engine ALONGSIDE the old, cut over
    when it is routable) is the fix, and it is only possible when the weights
    fit into the worker TWICE. Two things had to be read before that could be
    claimed, and both answers are here:

    * `_committed_gb` joins Deployment→DeploymentInstance and takes `.distinct()`
      on the DEPLOYMENT, times `dep.replicas`. A SECOND instance of the same
      deployment on the same worker therefore adds NOTHING to it. The static
      leg of the admission math cannot see an overlap at all.
    * `_budget_check`'s DYNAMIC leg can: it reads the worker's real VRAM use,
      and the old engine's weights are resident in that number. "Does another
      `est_gb` fit into what is free right now" is exactly the overlap question.

    So the overlap is judged on the dynamic leg only, and it REQUIRES both a
    known `est_gb` and a fresh reading. That inverts `_budget_check`'s own rule
    on purpose: there, an unknown footprint means "admit, we do not enforce on a
    guess" (#227). Here an unknown footprint means "do NOT attempt the overlap"
    — guessing wrong the other way trades a known outage for an OOM, and an OOM
    takes the OTHER models on that worker down with it.

    Returns ``{"interrupting": bool, "reason": str, "deployments": [...]}``.

    ``deployments`` (#2019) narrows the question to a given set instead of every
    deployment on the worker. The runner switch relaunches all of them and so
    omits it; ``apply-params`` relaunches exactly ONE, and asking "do all the
    models on this worker fit twice" on its behalf answers `does-not-fit-twice`
    on any well-used box — a fix that reports success and changes nothing. An
    empty list is a legitimate question with the `nothing-serving` answer. Only
    WHICH deployments are counted changes; `unknown`, both budget legs and all
    four `why` values are the ones #1867 and #1947 decided.

    **Never raises, and now actually.** The promise was in this docstring from
    the start (#1913) and the body had no `try` in it — measured when #1867
    began calling this from `advance_runner_upgrade` and fifteen unit tests
    went red with `AttributeError: '_Q' object has no attribute 'join'`. The
    thin session double in those tests cannot join, and a docstring is not an
    implementation.

    The direction of the fallback is the same one the whole function argues
    for: anything unreadable means **interrupting**, i.e. take the tear-down.
    Guessing the other way trades a known outage for an OOM. So an unexpected
    failure here costs a switch its overlap, never a worker its other models.
    """
    try:
        verdict = _switch_interruption(s, worker, deployments)
        verdict.setdefault("determined", True)
        return verdict
    except Exception:  # noqa: BLE001 — the fallback IS the contract
        logger.warning("switch_interruption could not be computed; treating "
                       "the switch as interrupting (the safe direction)",
                       exc_info=True)
        return {"interrupting": True, "deployments": [],
                # `determined` is the machine-readable half of the difference
                # (#1867, asked for in review): a REFUSAL by the admission
                # arithmetic and a FAILURE to compute one both come out
                # `interrupting: True`, so on a box both look like the same
                # tear-down and the same ~54.8 % failure window. Measuring the
                # rate then says nothing about the cause, and a repetition of
                # the SCH3 proof would report "blue-green does not engage here"
                # for a defect one level down. A boolean is checkable; a
                # sentence in a reason string is not.
                "determined": False, "why": "verdict-failed",
                "reason": ("this switch will interrupt serving for the length "
                           "of a model load: whether the weights fit twice "
                           "could not be determined")}


def _switch_interruption(s, worker, deployments=None) -> dict:
    """The real computation. Wrapped by `switch_interruption`, which is the
    one callers use — see its "never raises" note.

    ``deployments`` is the #2019 scope: ``None`` means "every deployment on this
    worker" (the runner switch's question, and the query below), a list means
    exactly those rows. The distinction is `is None`, not truthiness — an empty
    list asks about nothing and must answer `nothing-serving`, not silently
    widen back to the whole worker."""
    from app.api.inventory import (_capacity_source, _dep_charge_gb, _fresh_host_used_gb,
                                   _fresh_vram_used_gb, _VRAM_HEADROOM)
    from app.models import Deployment, DeploymentInstance

    if deployments is None:
        rows = (s.query(Deployment)
                .join(DeploymentInstance,
                      DeploymentInstance.deployment_id == Deployment.id)
                .filter(DeploymentInstance.worker_id == worker.id,
                        Deployment.status != "stopped")
                .distinct().all())
    else:
        rows = list(deployments)
    names = [d.model_name for d in rows]
    if not rows:
        return {"interrupting": False, "why": "nothing-serving",
                "reason": "nothing is serving on this worker — the switch "
                          "interrupts nothing",
                "deployments": names}

    unknown = [d.model_name for d in rows if not d.est_gb]
    if unknown:
        return {"interrupting": True, "deployments": names,
                "why": "unknown-footprint",
                "reason": ("this switch will interrupt serving for the length of "
                           "a model load: the footprint of "
                           f"{', '.join(sorted(unknown))} is unknown, so it "
                           "cannot be shown that a second copy would fit, and "
                           "starting one on a guess risks taking the whole "
                           "worker down with an out-of-memory")}

    basis, total = _capacity_source(worker)
    used = _fresh_vram_used_gb(worker) if basis == "vram" else None
    if basis != "vram" or total is None or used is None:
        return {"interrupting": True, "deployments": names,
                "why": "no-fresh-vram-reading",
                "reason": ("this switch will interrupt serving for the length of "
                           "a model load: this worker reports no fresh VRAM "
                           "reading, and without one it cannot be shown that a "
                           "second copy of the weights would fit")}

    free_now = max(0.0, total - used) * _VRAM_HEADROOM
    # #2520: a second copy costs what the first does — est_gb plus the host RSS
    # of llama.cpp context checkpoints when the params turn them on.
    need = sum(float(_dep_charge_gb(d)) * max(1, d.replicas or 1) for d in rows)
    if need > free_now:
        return {"interrupting": True, "deployments": names,
                "why": "does-not-fit-twice",
                "reason": ("this switch will interrupt serving for the length of "
                           f"a model load: a second copy needs {need:.1f} GB and "
                           f"{free_now:.1f} GB is free — the weights do not fit "
                           "twice on this worker")}

    # #1947 — THE SECOND MEASUREMENT, and the reason it exists.
    #
    # `free_now` above is computed from ONE basis, and nothing forces the node
    # to report `total` and `used` on the same one. Measured on box-175r (0.175,
    # 2026-09-11):
    #
    #     vram_total_gb : 96.0   <- the operator budget (LLM_WORKER_MEM_BUDGET_GB)
    #     vram_used_gb  :  2.0   <- the ENTIRE VRAM carveout the BIOS grants
    #     mem_total_gb  : 121.2      this platform; weights live in GTT, so this
    #     mem_used_gb   :  65.5      figure CANNOT rise above 2.0, ever
    #
    #     free_now = (96.0 - 2.0) * 0.9 = 84.6 GB   believed, on an empty box
    #                                                AND on a full one
    #     really free ~ 121.2 - 65.5    = 55.7 GB
    #
    # So on that platform the dynamic half of admission has no signal at all:
    # 84.6 GB is a CONSTANT. The overlap would be taken because of a number that
    # never moves — which is precisely the direction this function argues
    # against in its own docstring, "guessing wrong the other way trades a known
    # outage for an OOM, and an OOM takes the OTHER models on that worker down
    # with it". It was invisible on 0.91 because the two Strix boxes are not the
    # same machine: 0.91 has a large carveout and its VRAM figure tracks
    # load/unload exactly. The measurement was right; generalising it to "Strix"
    # was not.
    #
    # The fix is not to guess which basis a node meant. It is to require the
    # overlap to fit in HOST memory as well, whenever host memory is known. A
    # second copy of the weights has to live in real memory on this machine
    # regardless of which accounting names it, so a claim that cannot survive
    # both readings is not a claim we act on.
    #
    # Deliberately NOT done here: having the node report its basis
    # (`vram_used_basis`). That is the more complete answer and it needs a fleet
    # rollout — during which a mixed fleet reports no basis at all, i.e. exactly
    # the boxes at risk would be the ones without the new field. This check
    # needs nothing from the node and protects the fleet as it is today.
    host_used = _fresh_host_used_gb(worker)
    host_total = None
    try:
        host_total = float((worker.labels or {}).get("mem_total_gb") or 0) or None
    except (TypeError, ValueError):
        host_total = None
    if host_total is None or host_used is None:
        # No second reading, so the first one cannot be checked against
        # anything. Its own code, not `no-fresh-vram-reading`: a box run has to
        # be able to tell "the node went quiet" from "the node reports VRAM but
        # no host memory", because those are different faults on the node.
        return {"interrupting": True, "deployments": names,
                "why": "host-memory-unknown",
                "reason": ("this switch will interrupt serving for the length of "
                           "a model load: this worker reports no fresh host "
                           "memory reading, so its VRAM figure cannot be checked "
                           "against a second, independently-based one and a "
                           "second copy cannot be shown to fit")}
    free_host = max(0.0, host_total - host_used) * _VRAM_HEADROOM
    if need > free_host:
        return {"interrupting": True, "deployments": names,
                "why": "does-not-fit-in-host-memory",
                "reason": ("this switch will interrupt serving for the length of "
                           f"a model load: a second copy needs {need:.1f} GB and "
                           f"the VRAM budget reports {free_now:.1f} GB free, but "
                           f"host memory has only {free_host:.1f} GB — on a "
                           "unified-memory worker the weights come out of host "
                           "memory whichever budget names them")}
    return {"interrupting": False, "deployments": names, "why": "fits-twice",
            "reason": (f"a second copy needs {need:.1f} GB, {free_now:.1f} GB is "
                       f"free on the VRAM budget and {free_host:.1f} GB in host "
                       "memory, so the new engine can be started alongside the "
                       "old one")}


def _retire(s, worker, containers) -> int:
    """Unload the named engine containers on ``worker`` and drop their rows.

    The cutover half of a blue-green switch (#1867). Shaped like
    ``drain_worker``'s inner loop on purpose — same command, same row deletion —
    but scoped to NAMED containers instead of "everything on this worker", and
    without the `draining` flip: the worker keeps taking traffic throughout,
    because the new engines are already serving it.

    Returns how many were retired. A container that is already gone (the node
    lost it, an operator removed it) is not an error: the desired end state is
    "this container is not serving", and it is not.
    """
    from app.api.commands import enqueue_command
    from app.models import DeploymentInstance

    retired = 0
    for di in (s.query(DeploymentInstance)
               .filter(DeploymentInstance.worker_id == worker.id).all()):
        name = _container_of(di.endpoint)
        if name not in containers:
            continue
        enqueue_command(s, worker.id, "unload_engine",
                        {"instance_id": name, "container": name})
        s.delete(di)
        retired += 1
    return retired


def _ready_deployments(s, worker, up) -> set:
    """Deployment ids of the capture that the relaunch has brought up — the
    health gate's half that the node reports. On the overlap path only an
    instance that is NOT a captured (old) container counts (#1867)."""
    from app.models import DeploymentInstance

    rows = (s.query(DeploymentInstance)
            .filter(DeploymentInstance.worker_id == worker.id,
                    DeploymentInstance.status == "ready").all())
    retiring = _retiring(up)
    return {str(di.deployment_id) for di in rows
            if not retiring or _container_of(di.endpoint) not in retiring}


def _failed_relaunches(s, worker, up) -> list:
    """``[(entry, why)]`` for every captured deployment whose relaunched engine
    the node has given up on (#3106).

    Keyed on the instance ids this upgrade MINTED (recorded on the capture at
    relaunch time), not on "any failed row of the deployment": a failed row
    left over from an earlier, unrelated placement is not this upgrade's
    failure, and rolling back on it would undo a relaunch that is fine.
    """
    from app.models import DeploymentInstance

    by_iid = {getattr(di, "instance_id", None): di for di in
              (s.query(DeploymentInstance)
               .filter(DeploymentInstance.worker_id == worker.id).all())}
    out = []
    for e in up.captured or []:
        for iid in e.get("relaunched") or []:
            di = by_iid.get(iid)
            if di is not None and di.status == "failed":
                out.append((e, di.detail or "the node reported the engine failed"))
                break
    return out


def _roll_back_relaunch(s, up, worker, bad, reason: str) -> None:
    """A relaunch failed (or never came up): put the affected engines back on
    the runner they were on, and end the upgrade `failed`, saying so (#3106).

    ``bad`` is ``[(entry, why)]``. Two shapes, as everywhere in this module:

    * OVERLAP — nothing was torn down; the old engines still serve on the
      prior image. Rolling back is removing everything this upgrade started
      and restoring every pin. Per-engine would leave the engines that DID come
      up running next to their old copies, on a worker chosen because the
      weights fit exactly twice.
    * TEAR-DOWN — the old containers are gone. Each failed engine gets its
      half-started container removed, its prior pin back, and a relaunch on
      that prior image with its stored settings (files, params, task: the row
      the upgrade did not change). Engines that came up on the new runner keep
      it — rolling THEM back would be a second outage for a model that works.

    Never raises: this runs on the status-read and report paths. A rollback
    relaunch the funnel refuses is named in the error instead.
    """
    from app.models import Deployment

    overlap = any(e.get("overlap") for e in (up.captured or []))
    names = {}
    for e, _why in bad:
        dep = s.get(Deployment, e.get("deployment_id"))
        names[e.get("deployment_id")] = (dep.model_name if dep is not None
                                         else e.get("deployment_id"))
    rolled, not_restored = [], []
    if overlap:
        for e in up.captured or []:
            dep = s.get(Deployment, e.get("deployment_id"))
            if dep is not None:
                dep.runner_image = _rollback_pin(e, up, dep)
        started = {iid for e in (up.captured or [])
                   for iid in (e.get("relaunched") or [])}
        _retire(s, worker, started)
        rolled = sorted(names.values())
        how = ("nothing had been torn down: the previous engines kept serving on "
               "their previous runner, and the engines this upgrade started were "
               "removed")
    else:
        bad_ids = {e.get("deployment_id") for e, _ in bad}
        new_entries = []
        for e in up.captured or []:
            if e.get("deployment_id") not in bad_ids:
                new_entries.append(e)
                continue
            dep = s.get(Deployment, e.get("deployment_id"))
            if dep is None:
                new_entries.append(e)
                continue
            dep.runner_image = _rollback_pin(e, up, dep)
            _retire(s, worker, set(e.get("relaunched") or []))
            ids = {}
            try:
                _relaunch(s, worker, [e], pin_image=None, launched_ids=ids)
                rolled.append(dep.model_name)
                new_entries.append(dict(e, rolled_back=True,
                                        rollback_instances=ids.get(str(dep.id), [])))
            except Exception as exc:  # noqa: BLE001 - named in the error instead
                detail = getattr(exc, "detail", None) or str(exc)
                not_restored.append(f"{dep.model_name}: {detail}")
                new_entries.append(dict(e, rolled_back=False))
        up.captured = new_entries
        how = ("each was started again on its previous runner "
               f"({', '.join(sorted({str(_rollback_pin(e, up) or 'the node default') for e, _ in bad}))}) "
               "with its previous settings")
    whys = "; ".join(f"{names[e.get('deployment_id')]}: {why}" for e, why in bad)
    msg = (f"{reason} for {', '.join(sorted(names.values()))} ({whys}). "
           f"Rolled back {', '.join(rolled) or 'nothing'} — {how}.")
    if not_restored:
        msg += (" NOT restored, start these by hand: " + "; ".join(not_restored)
                + ".")
    up.state = "failed"
    up.error = msg
    up.updated_at = _now()
    logger.error("runner upgrade %s FAILED and rolled back on %s: %s",
                 up.id, getattr(worker, "name", worker.id), msg)


def _stable_gate(s, worker, up) -> bool:
    """The ONE place a switch becomes `done`: every deployment that decides
    it (not the experimental runner's, #3403) has a ready NEW engine, the
    router carries them (#266), then the overlap cutover runs (#1867).
    True when it passed (state `done`); False when not yet. Shared by the
    report path and the deadline, so experimental lateness can only ever
    add a warning — never stand in for this gate or skip the cutover
    (#3403 review).
    """
    # the health gate: every captured deployment has a READY instance on
    # this worker, as reported by the node itself.
    # #3403: the experimental runner's engines do not decide the switch.
    wanted = {e["deployment_id"] for e in (up.captured or []) if _decides(e)}
    # #1867: on the overlap path the OLD instance is still here and still
    # `ready`, so "this deployment has a ready instance" is true the moment
    # the relaunch is enqueued — and `_router_serves` is true as well, via
    # the old endpoint. A gate keyed on either would report `done` before
    # the new engine had loaded and then retire the only one that works.
    # So the overlap gate keys on the CONTAINER: a deployment counts as
    # ready only through an instance that is NOT the one we captured.
    retiring = _retiring(up)
    ready = _ready_deployments(s, worker, up)
    if wanted <= ready:
        # #266: "ready on the node" is not "reachable through the gateway".
        # Measured on 0.91 during the SCH3 box proof: the upgrade reported
        # DONE and requests kept failing for another ~72 s, because the
        # ROUTER still carried the pre-relaunch endpoint set. `done` is what
        # an operator reads as "the switch is over"; it must not be the
        # moment the node is happy, it must be the moment the fleet serves.
        #
        # So: regenerate the router config from THIS session (the instance
        # rows are flushed here, not committed — a second connection would
        # render the fleet as it was), then require every captured
        # deployment to actually appear in it. If it does not, stay in
        # `relaunching`; the deadline above still governs, so this can wait
        # but never hang.
        if not _router_serves(s, wanted):
            return False
        if retiring:
            # #1867 cutover. The new engines are ready and the fleet serves
            # them; NOW the old ones are retired. Deliberately after both
            # gates and never before: until this line runs, a failure or a
            # timeout leaves the worker serving on the old runner, which is
            # the whole point of the overlap.
            #
            # Only the rows we captured are removed. A row this upgrade did
            # not capture is somebody else's replica (`PATCH replicas`
            # places a second engine for the same deployment and it may
            # legitimately share the box) — retiring it would take down a
            # replica nobody asked us to touch.
            retired = _retire(s, worker, retiring)
            logger.info("runner upgrade %s: cut over to %s, retired %d old "
                        "engine(s) %s", up.id, up.image, retired,
                        sorted(retiring))
            # #1955: the retired rows are gone from THIS session, so the
            # router config rendered from it no longer carries them. Write
            # it here rather than leaving it to the next registration —
            # until then the config describes engines that are being
            # unloaded.
            #
            # And then ask the router to restart regardless of what those
            # bytes turned out to be. Measured on 0.91: a switch under load
            # leaves LiteLLM holding a cooldown for a single-replica model,
            # which refuses every request while every status says ready.
            # The entrypoint's content-hash watcher cannot catch this on a
            # relay-routed worker, because the endpoint names the WORKER,
            # not the engine container, and an engine switch there can
            # render byte-identical. Both calls are side errands: a cutover
            # that has already happened must not fail over a file.
            try:
                from app.router_config import (rebuild_from_session,
                                               request_router_restart)
                rebuild_from_session(s)
                request_router_restart(f"cutover:{up.id}")
            except Exception:  # pragma: no cover - defensive
                logger.warning("runner upgrade %s: router refresh after the "
                               "cutover failed (non-fatal) — the next "
                               "registration rebuilds the config, but a "
                               "LiteLLM cooldown would survive it (#1955)",
                               up.id, exc_info=True)
        # #3403: experimental engines that are still not up at this point
        # are named as a warning; they do not hold the switch.
        _note_experimental(s, up, [
            (e, "not ready when the stable engines were") for e in (up.captured or [])
            if not _decides(e) and e.get("relaunched")
            and e.get("deployment_id") not in ready])
        up.state = "done"
        up.updated_at = _now()
        logger.info("runner upgrade %s DONE: %d deployment(s) ready on %s "
                    "with %s, and the router config carries them",
                    up.id, len(wanted), worker.name, up.image)
        return True
    return False


def advance_runner_upgrade(s, worker) -> None:
    """Move the worker's active upgrade forward if its awaited event arrived.

    Called from the command-result endpoint and from registration. Idempotent
    and cheap when nothing is active — the common case on every heartbeat.
    """
    from app.models import DeploymentInstance, NodeCommand

    up = _active_for(s, worker.id)
    if up is None:
        return

    if up.deadline is not None and _now() > _aware(up.deadline):
        timed_out = (f"timed out in state {up.state!r} "
                     f"(deadline {(_aware(up.deadline)).isoformat()})")
        if up.state == "relaunching":
            # #3106: past this deadline an engine that has not come up is not
            # coming up, and on the tear-down path its old container is gone.
            # Failing here and walking away is what left the adopted engine
            # dead on the GB10; the ones that did not make it go back.
            ready = _ready_deployments(s, worker, up)
            late_all = [(e, "it did not report ready before the deadline")
                        for e in (up.captured or [])
                        if e.get("deployment_id") not in ready
                        and (e.get("relaunched") or _decides(e))]
            _note_experimental(s, up, [x for x in late_all if not _decides(x[0])])
            late = [x for x in late_all if _decides(x[0])]
            if not late:
                # #3403 review: the stable engines are ready, but that is not
                # the gate — it also needs the router (#266) and, on the
                # overlap path, the cutover. Only the gate may say `done`;
                # otherwise this times out exactly as it would without an
                # experimental engine.
                if _stable_gate(s, worker, up):
                    return
                _fail(up, timed_out, worker)
                return
            if late:
                _roll_back_relaunch(s, up, worker, late,
                                    f"the relaunch {timed_out}")
                return
        _fail(up, timed_out, worker)
        return

    if up.state == "deploying":
        cmd = s.get(NodeCommand, up.deploy_command_id) if up.deploy_command_id else None
        if cmd is None or cmd.status in ("pending", "claimed", "running"):
            return
        if cmd.status == "failed":
            # #1677 (a): the node was never touched — no capture, no drain. It
            # is still serving whatever it served, which is the whole point of
            # pulling first. `_fail` reports the real status rather than the
            # old blanket "worker stays drained".
            _fail(up, f"deploy_runner failed on the node: "
                      f"{(cmd.result or {}).get('error', 'no detail')}", worker)
            return
        # The image is present, so NOW the node is touched — capture what is
        # serving (the drain is about to delete those rows), drain, and undrain
        # again before the relaunch (see the module docstring: a draining
        # worker's reports would skip the relaunched engines' self-registration
        # and the health gate would wait forever).
        from app.api.workers import drain_worker

        # #1867 blue-green. `switch_interruption` answers "do the weights fit
        # into this worker TWICE" on the dynamic VRAM leg, and only on a known
        # footprint plus a fresh reading. When they do, the new engine starts
        # ALONGSIDE the old one and nothing is torn down until it serves — the
        # 54.8 % outage measured on 0.91 was the tear-down, not the upgrade.
        # When they do not, this is the old sequence, unchanged: an unknown or
        # too-large footprint means "do NOT attempt the overlap", because
        # guessing wrong trades a known outage for an OOM that takes the OTHER
        # models on the worker down with it.
        verdict = switch_interruption(s, worker)
        overlap = not verdict["interrupting"]
        # The decision is LOGGED with three distinguishable wordings, because
        # the box cannot otherwise tell them apart (#1867). Grep-stable:
        #   "blue-green"                — the overlap was taken
        #   "tear-down (admission)"     — the arithmetic refused it, correctly
        #   "tear-down (verdict failed)"— the arithmetic could not be computed
        # The middle and the last produce the SAME outage; only this line and
        # `interruption_determined` on the status separate them.
        # #1867: the decision is LOGGED with a machine-readable code, because
        # the box cannot otherwise tell the causes apart — and three of them
        # produce the SAME outage. `tear-down (admission)` alone was not
        # enough: the arithmetic refuses for THREE different reasons, and
        # under sustained load the likely one is not the interesting one.
        #
        #   blue-green  why=fits-twice           the overlap was taken
        #   tear-down   why=does-not-fit-twice   refused, correctly
        #   tear-down   why=no-fresh-vram-reading the node stopped reporting —
        #               often a symptom of the load itself, and it would read
        #               as "does not fit" if the code were not named
        #   tear-down   why=unknown-footprint    the deployment has no est_gb
        #   tear-down   why=does-not-fit-in-host-memory (#1947) the VRAM budget
        #               said yes and host memory said no — on a unified-memory
        #               worker that is the reading to believe, and seeing this
        #               code is how you learn the box is one
        #   tear-down   why=host-memory-unknown  (#1947) the VRAM figure could
        #               not be checked against a second, independently-based
        #               reading. Distinct from no-fresh-vram-reading: there the
        #               node went quiet, here it reports VRAM but not host memory
        #   tear-down   why=verdict-failed       the computation raised
        _why = verdict.get("why", "unspecified")
        _how = "blue-green" if overlap else "tear-down"
        logger.info("runner upgrade %s on %s: %s why=%s — %s",
                    up.id, worker.name, _how, _why, verdict.get("reason", ""))

        # Written ONCE, at the first crossing of the intervention boundary.
        # `advance_runner_upgrade` is called from the command-result endpoint,
        # from registration AND from status reads; two of them can reach this
        # branch before either commits. The second would capture AFTER the
        # first's drain deleted the instance rows — i.e. capture nothing — and
        # overwrite the good record, leaving rollback with an empty list and no
        # way back. Re-capturing an empty capture is harmless; overwriting a
        # full one is not.
        if not up.captured:
            up.captured = _capture(s, worker, up.image)
        # #3106 pre-flight: every engine about to be stopped must be one the
        # node can start again. Checked on the capture — what is on the node
        # NOW, including anything placed during the pull — and before the
        # drain, so a refusal costs nothing: the node keeps serving, untouched.
        #
        # REFUSE the whole upgrade rather than exclude the engine. The drain is
        # worker-wide (it unloads every engine and deletes every row), so
        # "upgrade the others and leave this one running" is not a thing the
        # tear-down path can do without a second, partial drain. And a node
        # left half on the old runner is a state nobody asked for and nothing
        # reports. The remedy is cheap and named in the message.
        blockers = _relaunch_blockers(s, [e["deployment_id"]
                                          for e in (up.captured or [])])
        # #3436 L-21: and the runner-channel gates for every engine this switch
        # will start again — the agent may have re-registered without the
        # channel while the image came down. Same refusal, same cost: none.
        from app.runner_targets import experimental_runner_enabled as _exp_on
        blockers += _channel_blockers(
            s, worker,
            [e["deployment_id"] for e in (up.captured or [])
             if _decides(e) or (not overlap and _exp_on())],
            up.image)
        if blockers:
            # Nothing was touched, so nothing is recorded: an empty capture is
            # what tells the rollback endpoint to keep its hands off the node
            # (#1677 (a)). Leaving it would let a later rollback DRAIN a node
            # this upgrade never changed.
            up.captured = []
            _fail(up, "refused before anything was stopped — "
                      + " ".join(blockers), worker)
            return
        if overlap:
            # No drain: the old engines keep serving, and their instance rows
            # must survive — the cutover below needs them, and a draining
            # worker's reports never re-create instances (#261-C2), which would
            # leave the health gate blind to the engine we are about to start.
            #
            # The mark goes on the capture (JSONB) rather than on a new column:
            # it is per-deployment state about THIS upgrade, and the row already
            # carries the list. Re-assigned rather than mutated in place —
            # `captured` is a plain JSONB column, so SQLAlchemy does not track
            # an in-place edit and the mark would be lost on commit.
            up.captured = [dict(e, overlap=True,
                                overlap_why=verdict.get("why", "unspecified"))
                           for e in (up.captured or [])]
        else:
            # Recorded on the TEAR-DOWN path too, so a status read months later
            # can still say WHY this switch did not overlap. `overlap: False`
            # plus `overlap_determined` is the pair that distinguishes "the
            # weights do not fit twice" from "we could not work it out".
            up.captured = [dict(e, overlap=False,
                                overlap_determined=bool(verdict.get("determined", True)),
                                overlap_why=verdict.get("why", "unspecified"))
                           for e in (up.captured or [])]
            drain_worker(s, worker)
            worker.status = "ready"
        try:
            minted = {}
            # #3403: on the overlap path nothing is torn down, so an engine on
            # the experimental runner simply keeps serving — the stable switch
            # has nothing to change on it. On the tear-down path the drain
            # stopped it too, so it is started again (on its channel).
            to_launch = [e for e in (up.captured or [])
                         if not (overlap and not _decides(e))]
            # #3403 review: with the box's experimental runner switched off,
            # an engine on it that the drain stopped cannot be started again
            # (the funnel refuses it). Named, not failed — it never decides the
            # stable switch.
            from app.runner_targets import experimental_runner_enabled
            if not experimental_runner_enabled():
                held = [e for e in to_launch if not _decides(e)]
                to_launch = [e for e in to_launch if _decides(e)]
                _note_experimental(s, up, [
                    (e, "the experimental runner is off on this box "
                        "(RAZZFAZZ_EXPERIMENTAL_RUNNER), so it was not started again")
                    for e in held])
            launched = _relaunch(s, worker, to_launch, pin_image=up.image,
                                 launched_ids=minted)
            # #3106: the instance ids this relaunch minted, so a failure of
            # exactly THESE engines can be told from an unrelated failed row.
            # Re-assigned, not mutated in place (plain JSONB column).
            up.captured = [dict(e, relaunched=minted.get(str(e["deployment_id"]), []))
                           for e in (up.captured or [])]
        except HTTPException as exc:
            # #3436 L-21: what was enqueued before the refusal is recorded
            # first, so the rollback below retires exactly those engines
            # instead of starting a second copy next to them.
            up.captured = [dict(e, relaunched=minted.get(str(e["deployment_id"]), []))
                           for e in (up.captured or [])]
            # #1494 review, finding 1: this function is called from the
            # command-result endpoint, from registration AND from
            # `_status_payload` — i.e. from `GET /api/runner-upgrades/{id}`.
            # Letting the 422 out turned a STATUS READ into an error response,
            # and on the command-result path it was swallowed by the result
            # handler: undrain and a partial relaunch stayed committed while
            # the upgrade sat in `deploying` until the 30-minute timeout, then
            # blamed the timeout. The rejection IS the failure, so say so.
            # #1494 re-review, finding 1: `worker.status = "ready"` above has
            # already undrained the node. `_fail` does not undo it, so a failed
            # upgrade left the worker taking traffic with nothing relaunched on
            # it — the exact opposite of what this module's docstring promises
            # ("failure leaves the worker DRAINED — operator decision") and of
            # what the deploy-failure path does two branches up.
            #
            # #1867: on the OVERLAP path there is nothing to drain for. Nothing
            # was torn down, the old engines are still serving, and draining
            # here would MANUFACTURE the outage this path exists to avoid. The
            # rejection is still a failure — it is just a failure that costs
            # nothing, like a failed pull.
            #
            # #3106 box pass, superseding the "stays drained" half: a failed
            # upgrade must not leave the node drained and serving nothing (the
            # status report then showed a FAIL nothing cleared). The relaunch
            # is all-or-nothing, so NOTHING was enqueued and nothing repinned:
            # the engines the drain stopped go back on their previous runner
            # (the same per-engine rollback as a failed load), the one the
            # funnel refuses is named in the error, and the worker stays
            # undrained. On the overlap path nothing was stopped at all.
            if not overlap and up.captured:
                worker.status = "ready"
                _roll_back_relaunch(
                    s, up, worker,
                    [(e, "not started on the new runner") for e in up.captured],
                    f"the relaunch was refused ({exc.detail})")
                return
            _fail(up, f"relaunch rejected: {exc.detail}", worker)
            return
        if launched == 0:
            # nothing was serving — the upgrade is just the new image present.
            up.state = "done"
            up.updated_at = _now()
            logger.info("runner upgrade %s done (no deployments to relaunch)", up.id)
            return
        up.state = "relaunching"
        up.deadline = _now() + RELAUNCH_TIMEOUT
        up.updated_at = _now()
        logger.info("runner upgrade %s: %s pulled, %d deployment(s) repinned + "
                    "relaunched — health gate armed", up.id, up.image, launched)
        return

    if up.state == "relaunching":
        # #3106: a relaunched engine the node gave up on (load refused, or the
        # container died) is not waited out to the deadline — on the tear-down
        # path its old container is already gone, so every minute spent here
        # is a minute the model is down. It goes back to its previous runner.
        failed_all = _failed_relaunches(s, worker, up)
        _note_experimental(s, up, [x for x in failed_all if not _decides(x[0])])
        failed = [x for x in failed_all if _decides(x[0])]
        if failed:
            _roll_back_relaunch(s, up, worker, failed, "the relaunch failed")
            return
        _stable_gate(s, worker, up)


def register_runner_upgrade_api(app) -> None:
    # #314: per-node runner-backend upgrades are worker/federation infrastructure
    # → the SUPER-ADMIN tier.
    router = APIRouter(dependencies=[Depends(require_role(Role.SUPERADMIN))])

    @router.post("/api/workers/{worker_id}/upgrade-runner")
    def start(worker_id: str, payload: UpgradeRequest):
        from app.api._ids import parse_uuid
        from app.api.commands import enqueue_command
        from app.api.inventory import valid_runner_image
        from app.models import RunnerUpgrade, Worker

        if not valid_runner_image(payload.image):
            raise HTTPException(status_code=422, detail=(
                f"image {payload.image!r} is not a valid image reference"))
        first = payload.image.split("/", 1)[0]
        if "/" not in payload.image or not ("." in first or ":" in first):
            raise HTTPException(status_code=422, detail=(
                f"image {payload.image!r} is not registry-qualified — upgrades "
                f"deploy from the master's registry (#549 R2)"))

        with session_scope() as s:
            worker = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            if worker is None:
                raise HTTPException(status_code=404, detail="worker not found")
            if worker.status == "pending":
                raise HTTPException(status_code=409, detail="approve the worker first")
            if (worker.labels or {}).get("external"):
                raise HTTPException(status_code=409,
                                    detail="external backends run no worker-agent")
            if _active_for(s, worker.id) is not None:
                raise HTTPException(status_code=409, detail=(
                    "an upgrade is already active on this worker — one at a "
                    "time; roll it back or let it finish"))
            # #3106: say it NOW, before the pull, if an engine on this worker
            # could not be started again. The same check runs once more on the
            # capture after the pull (a deployment placed meanwhile), and that
            # one is what guarantees nothing is torn down; this one spares the
            # operator a multi-GB pull that ends in the same refusal.
            from app.models import DeploymentInstance
            on_worker = [di.deployment_id for di in
                         (s.query(DeploymentInstance)
                          .filter(DeploymentInstance.worker_id == worker.id).all())]
            blockers = _relaunch_blockers(s, on_worker)
            # #3436 L-21: and the runner-channel gates, for the whole set, at
            # ACCEPT time: a switch onto the experimental runner on a box that
            # has not opted in, or an engine whose channel this worker's agent
            # does not serve, was accepted (200) and refused only after the
            # engines were stopped. An engine on the experimental channel that
            # the box has switched off is not a blocker: the switch names it
            # and leaves it stopped (#3403 review), as before.
            from app.api.inventory import (require_experimental_runner_enabled,
                                           runner_is_experimental)
            from app.models import Deployment as _Dep
            from app.runner_targets import experimental_runner_enabled as _exp_on
            require_experimental_runner_enabled(channel=None, runner_image=payload.image)

            def _held(dep_id) -> bool:
                d = s.get(_Dep, dep_id)
                return d is not None and runner_is_experimental(
                    channel=d.runner_channel, runner_image=d.runner_image, tags=_EXP_TAGS())
            blockers += _channel_blockers(
                s, worker, [d for d in on_worker if _exp_on() or not _held(d)], payload.image)
            if blockers:
                raise HTTPException(status_code=409, detail=(
                    "runner upgrade refused, nothing was changed: "
                    + " ".join(blockers)))

            # #1677 (a): this endpoint now only starts the PULL. Nothing on the
            # node is captured, drained or otherwise touched until the image is
            # actually present — see the ordering block in the module docstring.
            cmd = enqueue_command(s, worker.id, "deploy_runner",
                                  {"image": payload.image})
            s.flush()
            up = RunnerUpgrade(worker_id=worker.id, image=payload.image,
                               state="deploying", captured=[],
                               deploy_command_id=cmd.id,
                               deadline=_now() + DEPLOY_TIMEOUT)
            s.add(up)
            s.flush()
            # #1867: say BEFORE the operator commits whether this switch will
            # interrupt serving. Silence here is what turned a 54.8% failure
            # rate into something an operator found out from a log afterwards.
            interruption = switch_interruption(s, worker)
            logger.info("runner upgrade %s started on %s -> %s (pull enqueued; "
                        "the node is untouched until it lands) — %s",
                        up.id, worker.name, payload.image, interruption["reason"])
            # `captured_deployments` stays in the payload — it is the caller's
            # handle on "how much is this upgrade about to move" — but it is 0
            # here now, and only meaningful once the pull has landed. The status
            # endpoint carries the real list.
            return {"upgrade_id": str(up.id), "worker": worker.name,
                    "image": payload.image, "state": up.state,
                    "captured_deployments": len(up.captured or []),
                    "interruption": interruption}

    def _status_payload(s, up) -> dict:
        """Serialize an upgrade row, first running the lazy timeout check so a
        stuck upgrade cannot look "deploying" forever to an operator staring
        at it (shared by the by-id and by-worker reads)."""
        from app.models import Worker

        if up.state in ACTIVE_STATES:
            w = s.get(Worker, up.worker_id)
            if w is not None:
                advance_runner_upgrade(s, w)
        return {"upgrade_id": str(up.id), "worker_id": str(up.worker_id),
                "image": up.image, "state": up.state, "error": up.error,
                "captured": up.captured or [],
                # #3403: engines on the experimental runner that did not come
                # up — they do not decide the switch, so they are named here.
                "warnings": [e["warning"] for e in (up.captured or []) if e.get("warning")],
                # #1867: which path the switch took. An operator watching one
                # needs to tell an overlap from a tear-down — they mean
                # different things for the models on that worker, and only one
                # of them has an outage in it.
                "overlapped": any(e.get("overlap")
                                  for e in (up.captured or [])),
                # #1867: `overlapped: false` has two causes with one symptom.
                # False here means the verdict could not be computed at all —
                # the switch tore down because of a defect, not because the
                # arithmetic said so. Without this an operator (or a box
                # measurement) reads the same outage either way.
                # #1867: WHICH of the five verdicts this switch got. The three
                # refusing ones produce the same outage, so a rate alone cannot
                # attribute it.
                "interruption_why": next(
                    (e.get("overlap_why") for e in (up.captured or [])
                     if e.get("overlap_why")), None),
                "interruption_determined": all(
                    e.get("overlap_determined", True)
                    for e in (up.captured or [])),
                "deadline": up.deadline.isoformat() if up.deadline else None}

    @router.get("/api/runner-upgrades/{upgrade_id}")
    def status(upgrade_id: str):
        from app.api._ids import parse_uuid
        from app.models import RunnerUpgrade

        with session_scope() as s:
            up = s.get(RunnerUpgrade, parse_uuid(upgrade_id, "upgrade_id"))
            if up is None:
                raise HTTPException(status_code=404, detail="upgrade not found")
            return _status_payload(s, up)

    @router.get("/api/workers/{worker_id}/upgrade-runner")
    def latest_for_worker(worker_id: str):
        """The worker's most recent upgrade, any state — the console reopens a
        drawer mid-upgrade without holding the id (R4), and a failed upgrade
        must stay visible (failure leaves the worker drained) so the operator
        finds the rollback affordance, not just a mysteriously drained node."""
        from app.api._ids import parse_uuid
        from app.models import RunnerUpgrade

        with session_scope() as s:
            up = (s.query(RunnerUpgrade)
                  .filter(RunnerUpgrade.worker_id == parse_uuid(worker_id, "worker_id"))
                  .order_by(RunnerUpgrade.created_at.desc())
                  .first())
            if up is None:
                raise HTTPException(status_code=404, detail="no upgrades for this worker")
            return _status_payload(s, up)

    @router.post("/api/runner-upgrades/{upgrade_id}/rollback")
    def rollback(upgrade_id: str):
        """Restore the prior pins, relaunch what was serving, return the worker
        to service. Valid from any non-done state — uniform, because a partial
        forward state is exactly when an operator reaches for this."""
        from app.api._ids import parse_uuid
        from app.models import (Deployment, DeploymentInstance, RunnerUpgrade,
                                Worker)
        from app.api.workers import drain_worker

        with session_scope() as s:
            up = s.get(RunnerUpgrade, parse_uuid(upgrade_id, "upgrade_id"))
            if up is None:
                raise HTTPException(status_code=404, detail="upgrade not found")
            if up.state == "done":
                raise HTTPException(status_code=409, detail=(
                    "upgrade already completed — roll forward with a new upgrade "
                    "to the previous image instead"))
            if up.state == "rolled_back":
                return {"upgrade_id": str(up.id), "state": "rolled_back",
                        "already": True}
            worker = s.get(Worker, up.worker_id)
            if worker is None:
                raise HTTPException(status_code=409, detail="worker no longer exists")

            # restore the pins captured before anything changed
            for entry in (up.captured or []):
                dep = s.get(Deployment, entry["deployment_id"])
                if dep is not None:
                    dep.runner_image = _rollback_pin(entry, up, dep)
            # Clear whatever half-state is on the worker, then relaunch the
            # captured set with their restored pins and put it back in service.
            #
            # #1677 (a): ONLY when there is something to bring back. An empty
            # capture became reachable when the pull moved in front of the
            # drain — a failed pull touches nothing, so it records nothing. On
            # such an upgrade the unconditional drain below would unload every
            # engine the node is happily serving and relaunch none of them: the
            # rollback would create the outage it exists to undo. Draining in
            # order to relaunch nothing is never the right action; before the
            # reorder it simply could not happen.
            # #1867: on the OVERLAP path the old engines were never touched —
            # they are still serving, on the prior image, which is exactly the
            # state a rollback wants to reach. Draining here would unload them
            # to relaunch the same thing: the rollback would create an outage
            # it does not need to create, the same shape as the empty-capture
            # case below. What DOES have to go is whatever this upgrade started
            # alongside them.
            overlap_names = {name for e in (up.captured or [])
                             if e.get("overlap")
                             for name in (e.get("containers") or [])}
            if overlap_names:
                # The old engines are ALREADY the rolled-back state: still up,
                # still on the prior image (restored two lines above). So this
                # path neither drains nor relaunches — it only removes what the
                # upgrade started alongside them, and returns.
                #
                # Relaunching here would be actively wrong, not merely
                # redundant: `_relaunch` mints a THIRD engine for a deployment
                # that already has a working one, and on a worker that was
                # chosen because the weights fit exactly twice, a third copy is
                # the OOM that `switch_interruption` refuses to risk.
                started = {_container_of(di.endpoint) for di in
                           (s.query(DeploymentInstance)
                            .filter(DeploymentInstance.worker_id == worker.id)
                            .all())} - overlap_names
                started.discard(None)
                retired = _retire(s, worker, started) if started else 0
                up.state = "rolled_back"
                up.updated_at = _now()
                logger.info("runner upgrade %s rolled back on %s (overlap): "
                            "pins restored, the old engines were never torn "
                            "down and keep serving, %d half-started engine(s) "
                            "retired %s", up.id, worker.name, retired,
                            sorted(started))
                return {"upgrade_id": str(up.id), "state": "rolled_back",
                        "relaunched": 0, "skipped": [],
                        # Named rather than implied: an operator who rolls back
                        # a switch wants to know whether anything went down.
                        "retired": sorted(started), "overlapped": True}
            if up.captured:
                drain_worker(s, worker)
            else:
                logger.info("runner upgrade %s rollback: nothing was captured, "
                            "so the node was never changed — leaving %s alone",
                            up.id, worker.name)
            # #1494 re-review, finding 3: rollback is the EMERGENCY route. One
            # deployment the funnel refuses (a GGUF-less llama.cpp row, a relay
            # conflict) used to abort the whole restore through session_scope,
            # leaving every OTHER deployment on the box unrecoverable — and the
            # operator with a 422 instead of a node. It skips and reports now;
            # the refused ones are named so they can be re-deployed by hand.
            skipped = []
            launched = 0
            for entry in (up.captured or []):
                try:
                    launched += _relaunch(s, worker, [entry], pin_image=None)
                except HTTPException as exc:
                    skipped.append({"deployment_id": entry.get("deployment_id"),
                                    "reason": str(exc.detail)})
                    logger.warning("runner upgrade %s rollback: skipping "
                                   "deployment %s — %s", up.id,
                                   entry.get("deployment_id"), exc.detail)
            # Same reasoning: only put the node back in service if this
            # rollback took it out. Forcing `ready` on a node that was draining
            # for an unrelated reason would silently undo that drain.
            if up.captured:
                worker.status = "ready"
            up.state = "rolled_back"
            if skipped:
                up.error = ("rolled back, but %d deployment(s) could not be "
                            "relaunched: %s" % (len(skipped), skipped))
            up.updated_at = _now()
            logger.info("runner upgrade %s rolled back on %s: pins restored, "
                        "%d deployment(s) relaunched, %d skipped",
                        up.id, worker.name, launched, len(skipped))
            return {"upgrade_id": str(up.id), "state": "rolled_back",
                    "relaunched": launched, "skipped": skipped}

    app.include_router(router)
