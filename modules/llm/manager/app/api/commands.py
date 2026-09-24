# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""manager→node command channel (#261 control plane, C1).

The manager ENQUEUES a command for a worker (admin); the worker's worker-agent
LONG-POLLS its own pending commands (node-key, outbound-only — NAT-friendly),
executes via its drivers, and POSTs the result. The management UI enqueues +
polls the result (e.g. tail_logs → read result).

Auth: enqueue / history / read = require_admin (+ Caddy source anchor);
claim / result = require_node_key (fail-closed). Only enumerated `kind`s — the
node never runs arbitrary shell.
"""
from __future__ import annotations

import datetime as _dt
import os
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.authz import Role, require_role
from app.api.workers import authorize_command_node
from app.config import get_settings
from app.api._ids import parse_uuid
from app.db import session_scope

logger = logging.getLogger("orchestrator.commands")
from app.models import DeploymentInstance, NodeCommand, Worker

# ─── Command-kind authority (#547) ───────────────────────────────────────────
#
# COMMAND_KINDS is THE manager-side list of everything the worker-agent knows how
# to execute (never shell). The node's dispatch in
# modules/llm/node-agent/app/commands.py is a separate deployable and cannot
# import this, so agreement is enforced by
# tests/unit/llm-manager/test_command_kinds_authority.py, which parses the node
# file — the two diverging is a red test, not a silent no-op on the node.
#
# Before #547 this list existed twice and already disagreed: 5 kinds gated the
# generic admin endpoint while 8 reached the node, because inventory.py
# constructed NodeCommand rows directly at four sites. Adding a kind now means:
#   1. add it here,
#   2. add the node-side handler,
#   3. decide whether it belongs in ADMIN_ENQUEUEABLE_KINDS (usually NOT — see
#      below).
# Forgetting (2) fails the authority test; forgetting (1) makes enqueue_command
# raise at the first internal call site.
COMMAND_KINDS = frozenset({
    # interactive engine ops (admin-enqueueable)
    "restart_engine", "stop_engine", "load_engine", "unload_engine", "tail_logs",
    # long-running distribution ops — enqueued ONLY by their dedicated routes,
    # which validate the payload (#307 mirror, #353 pull, #306 disk inventory)
    "mirror_model", "pull_artifact", "list_disk_models",
    # #307 S3: delete a model's weight files off ONE worker's on-disk cache —
    # the "remove from fleet" eviction's per-worker leg. Dedicated route only
    # (POST /api/inventory/{model}/evict), same posture as mirror/pull above:
    # the file list comes off the persisted deployment row, not a caller-typed
    # arg the generic admin endpoint would let through unvalidated.
    "evict_weights",
    # #549 R2: runner-image lifecycle on a node. Same posture as the ops above —
    # dedicated, payload-validating routes only, never the generic endpoint:
    # deploy_runner pulls an image onto a node and remove_runner deletes one,
    # and both take an image reference the routes must validate first.
    "deploy_runner", "remove_runner", "list_runner_images",
    # #306 delete half: free disk by removing one cached weight file from a
    # node's model mount. `name` is a caller-chosen path — same posture as
    # deploy_runner/remove_runner's image ref, so this is dedicated-route-only,
    # never the generic admin endpoint (see ADMIN_ENQUEUEABLE_KINDS below).
    "delete_disk_model",
})

# The deliberate SUBSET the generic admin endpoint accepts. Narrower than
# COMMAND_KINDS on purpose: raw enqueue of e.g. pull_artifact with
# caller-chosen args would bypass the dedicated routes' payload validation, and
# pull_artifact writes files onto a node's model mount. Widening this is a
# security decision, not a convenience.
#
# #671: list_disk_models belongs here — it is READ-ONLY and takes no args, so
# the bypass rationale above does not apply, and the console's #306 cache view
# calls it through this endpoint. The #547 narrowing dropped it and the view
# errored on every worker panel until the UI-contract test below the fold
# pinned the set.
ADMIN_ENQUEUEABLE_KINDS = frozenset({
    "restart_engine", "stop_engine", "load_engine", "unload_engine", "tail_logs",
    "list_disk_models",
})

# Back-compat alias (pre-#547 name); same object as the admin subset.
ALLOWED_KINDS = ADMIN_ENQUEUEABLE_KINDS

# LLMM-2: the kinds that PROVISION capacity on a worker — the ones the #265 ENT3
# subscription gate applies to. `load_engine` launches an engine; `mirror_model`
# and `pull_artifact` fetch weights and grow the shared cache. Everything else in
# COMMAND_KINDS either frees capacity (unload/stop/evict/delete_disk_model) or
# only reads (tail_logs/list_*) and must keep working on a lapsed box, so a
# customer can always shut things down and diagnose. Kept next to the kind lists
# it partitions, not next to the gate, so adding a kind forces the "does this
# provision?" question in the same place as "does the node handle it?".
CAPACITY_PROVISIONING_KINDS = frozenset({
    "load_engine", "mirror_model", "pull_artifact",
})

# #1183: the READ-ONLY kinds the console may ask to "revalidate" — answer at
# once with the last completed result for the same (worker, kind, args) and
# enqueue ONE refresh (reusing a refresh already in flight). Only inventories
# and logs: an action (restart/stop/load/unload/delete/…) must never be served
# "stale" nor have two operator requests coalesced into one execution.
REVALIDATE_KINDS = frozenset({"list_disk_models", "list_runner_images", "tail_logs"})


def enqueue_command(s, worker_id, kind: str, args: dict) -> "NodeCommand":
    """The ONE way manager code enqueues a command for a node (#547).

    Validates the kind against COMMAND_KINDS, so an internal call site cannot
    ship a string the node has no handler for — before this, four sites in
    inventory.py constructed NodeCommand directly and nothing checked, meaning a
    typo'd kind would sit `pending` forever and its deployment would hang in
    `scheduled` with no error anywhere.

    Deliberately does NOT check ADMIN_ENQUEUEABLE_KINDS: internal callers are
    the dedicated, payload-validating routes, and restricting them to the
    interactive subset would break mirror/pull.
    """
    if kind not in COMMAND_KINDS:
        raise ValueError(
            f"unknown command kind {kind!r} — the worker-agent has no handler for "
            f"it (have: {sorted(COMMAND_KINDS)}). Add it to COMMAND_KINDS and "
            f"the node dispatch together (#547).")
    # #569 review: created_at is stamped IN PYTHON, not left to the column's
    # server default — Postgres now() is transaction-constant, so two commands
    # enqueued in one transaction (apply-params' unload+load, drain's batch)
    # would tie and the claim's ORDER BY created_at could invert them. A
    # python-side clock gives distinct microsecond stamps per call, which is
    # what makes "unload before load" actually hold on the wire.
    import datetime as _dt
    cmd = NodeCommand(worker_id=worker_id, kind=kind, args=args or {},
                      created_at=_dt.datetime.now(_dt.timezone.utc))
    s.add(cmd)
    return cmd


class CommandCreate(BaseModel):
    kind: str
    args: dict = {}
    # #1183 stale-while-revalidate (REVALIDATE_KINDS only; ignored otherwise).
    revalidate: bool = False


class RevalidateOpts(BaseModel):
    """#1183: optional body for the dedicated read-only routes (runners/list)."""
    revalidate: bool = False


def _mark_load_failed(s, cmd, result: dict):
    """#1372: carry a node's load_engine rejection onto the instance row it was
    scheduled for, so `_deployment_health` reads `failed` and the console shows
    the node's own sentence (`detail`) instead of `pending` forever.

    One failed row per deployment on this worker — the latest: the reconciler
    reschedules after its cooldown, and every attempt would otherwise leave a
    row behind. Returns the instance row, or None when nothing matched (a
    command from before the row existed, or an already-pruned instance).
    """
    iid = (cmd.args or {}).get("instance_id")
    if not iid:
        return None
    err = str(result.get("error") or result.get("detail")
              or "node reported failure without a reason")[:500]
    rows = (s.query(DeploymentInstance)
            .filter(DeploymentInstance.worker_id == cmd.worker_id).all())
    target = next((di for di in rows if di.instance_id == iid), None)
    if target is None:
        return None
    for di in rows:
        if di is not target and di.deployment_id == target.deployment_id and di.status == "failed":
            s.delete(di)
    target.status = "failed"
    target.detail = f"node rejected load_engine: {err}"
    s.flush()
    return target

#: #1633 — how long a CLAIMED command may stay unfinished before the manager
#: stops believing in it. Deliberately generous: with no progress reporting from
#: the node (there is none today), this cap cannot tell a slow 30 GB transfer
#: from a wedged one, so it is set where no honest transfer lives and a wedged
#: box still reports the same day. The sharp instrument is the node's own read
#: timeout (#1633 first half) — this is the backstop for the case that timeout
#: cannot see: the node died mid-command and forgot it ever ran.
STALE_CLAIM_SECONDS = int(os.environ.get("LLM_MANAGER_STALE_CLAIM_SECONDS") or 6 * 3600)


def expire_stale_claims(s, worker_id, now, *, max_age_s: int = None) -> list:
    """Fail this worker's claims that nobody ever finished. Returns the rows.

    A command sits at `claimed` with `finished_at` NULL only while the node is
    working on it. When the node restarts, that row becomes a promise nobody
    remembers making — and nothing used to notice. On 0.79 the console read
    `pulling` for hours off exactly such a row (#1633).

    Scoped to ONE worker on purpose: this runs inside that worker's own claim,
    so a busy node can only ever expire its own leftovers. A node that never
    comes back has an offline worker row, which is the honest signal there.

    The instance the command was scheduled for is carried along by the same
    helper the node's own `failed` result uses, so an expiry and a rejection
    leave the deployment in the same readable state instead of two.
    """
    cap = STALE_CLAIM_SECONDS if max_age_s is None else max_age_s
    cutoff = now - _dt.timedelta(seconds=cap)
    stale = (
        s.query(NodeCommand)
        .filter(NodeCommand.worker_id == worker_id,
                NodeCommand.status == "claimed",
                NodeCommand.finished_at.is_(None),
                NodeCommand.claimed_at.isnot(None),
                NodeCommand.claimed_at < cutoff)
        .all()
    )
    for c in stale:
        age = int((now - c.claimed_at).total_seconds())
        c.status = "failed"
        c.finished_at = now
        c.result = {"error": (f"expired: claimed {age}s ago and never finished "
                              f"(cap {cap}s). The node was restarted or stopped "
                              f"mid-command."),
                    "expired": True}
        if c.kind == "load_engine":
            _mark_load_failed(s, c, c.result)
        logger.warning("expired stale %s command %s on worker %s after %ss",
                       c.kind, c.id, worker_id, age)
    return stale

class CommandResult(BaseModel):
    status: str  # done | failed
    result: dict = {}


# #1195: these two request models MUST live at module scope. This module uses
# `from __future__ import annotations`, so a handler's `payload: X` is a STRING
# FastAPI resolves against the module globals — a class defined inside
# `register_commands_api()` is invisible there, pydantic keeps an unresolved
# ForwardRef, and `GET /openapi.json` (hence /docs) 500s with
# "`RunnerImageRequest` is not fully defined". Request validation itself
# limped along, so nothing else noticed.
class RunnerImageRequest(BaseModel):
    """#549 R2 runner-image lifecycle payload (deploy/remove)."""
    image: str


class DiskModelDeleteRequest(BaseModel):
    """#306 cached-weight delete payload — `name` is a caller-chosen path."""
    name: str


def _iso(v):
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else None


def _row(c: NodeCommand) -> dict:
    return {
        "id": str(c.id),
        "worker_id": str(c.worker_id),
        "kind": c.kind,
        "args": c.args or {},
        "status": c.status,
        "result": c.result,
        "created_at": _iso(c.created_at),
        "claimed_at": _iso(c.claimed_at),
        "finished_at": _iso(c.finished_at),
    }


# ─── #1183 stale-while-revalidate ───────────────────────────────────────────
#
# The manager already holds every answer a node ever gave (node_commands rows;
# inventory.py::_latest_disk_report reads exactly that for the fleet view), so
# the drawer's first paint does not have to wait a full claim round-trip: serve
# the last completed result immediately, marked stale, and refresh behind it.
# The selection is pure (plain rows in, row out) so the rule is unit-testable
# without a DB; the DB read is one bounded query of recent same-kind rows.

def _same_args(row, args: dict) -> bool:
    return (row.args or {}) == (args or {})


def latest_done(rows, args: dict):
    """The newest COMPLETED row (status done — a failed answer is not a
    stale inventory, it is no inventory) whose args equal ``args``. ``rows``
    may be in any order; ties broken by finished_at then created_at."""
    best = None
    for r in rows:
        if r.status != "done" or not _same_args(r, args):
            continue
        # None-safe + tz-safe like in_flight_refresh: a row with neither timestamp
        # (or a naive/aware mix) must not raise TypeError mid-request.
        key = (r.finished_at or r.created_at or T_MIN, r.created_at or T_MIN)
        if best is None or key > (best.finished_at or best.created_at or T_MIN, best.created_at or T_MIN):
            best = r
    return best


def in_flight_refresh(rows, args: dict):
    """A row for the same args the node has not answered yet (pending or
    claimed) — the refresh to reuse instead of stacking a second one."""
    newest = None
    for r in rows:
        if r.status not in ("pending", "claimed") or not _same_args(r, args):
            continue
        if newest is None or (r.created_at or T_MIN) > (newest.created_at or T_MIN):
            newest = r
    return newest


T_MIN = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
RECENT_SAME_KIND_LIMIT = 50


def _recent_same_kind(s, worker_id, kind: str):
    """The bounded DB read behind ``revalidate_command``: the newest rows of
    this kind for this worker (tail_logs live-polls create one per answer, so
    a bound is needed; 50 is plenty for 'the last done one + anything in
    flight')."""
    return (
        s.query(NodeCommand)
        .filter(NodeCommand.worker_id == worker_id, NodeCommand.kind == kind)
        .order_by(NodeCommand.created_at.desc())
        .limit(RECENT_SAME_KIND_LIMIT)
        .all()
    )


def revalidate_command(s, worker_id, kind: str, args: dict, *, recent=None):
    """Stale-while-revalidate for one (worker, kind, args) (#1183).

    Returns ``(command, extra)``: ``command`` is the refresh the console should
    poll — a refresh already pending/claimed for the same args (``coalesced``)
    or a freshly enqueued one — and ``extra`` carries the last completed
    result (``stale_result`` + ``stale_finished_at``) or ``None`` when the node
    never answered this before. Exactly one refresh is ever in flight per
    (worker, kind, args) through this path.
    """
    rows = recent if recent is not None else _recent_same_kind(s, worker_id, kind)
    stale = latest_done(rows, args)
    running = in_flight_refresh(rows, args)
    cmd = running if running is not None else enqueue_command(s, worker_id, kind, args)
    extra = {
        "stale": stale is not None,
        "stale_result": (stale.result or {}) if stale is not None else None,
        "stale_finished_at": _iso(stale.finished_at) if stale is not None else None,
        "coalesced": running is not None,
    }
    return cmd, extra


def valid_disk_model_name(name) -> bool:
    """#306 delete half: is `name` a safe relative path into a node's model
    mount?

    `name` is caller-chosen (an admin picks a row from the #306 list view) and
    travels to the node as a filesystem path to `os.remove` — the same trust
    boundary as `_validated_runner_ref`'s image reference below, applied to a
    path instead of an image ref. Refuses:
      * empty/blank — nothing to delete;
      * an absolute path — must resolve INSIDE the node's mount, not to it;
      * any path that normalises to `.` or starts with `..` — the traversal
        shape (`../../etc/passwd`, `..`) that would escape the mount.
    The node re-validates AND enforces its own containment check
    (`delete_cached_weight`) — this is the fast 422 naming the field, not the
    security boundary (#549 R2's posture, applied here).
    """
    import os

    if not isinstance(name, str) or not name.strip():
        return False
    if os.path.isabs(name):
        return False
    normalized = os.path.normpath(name)
    if normalized == "." or normalized == ".." or normalized.startswith(".." + os.sep):
        return False
    return True


def register_commands_api(app) -> None:
    # #314: the manager→node command channel (per-worker runner/disk-model ops)
    # is worker-federation control-plane → the SUPER-ADMIN tier. Node-side
    # claim/result stays on per-worker node-key auth (unchanged).
    admin = APIRouter(dependencies=[Depends(require_role(Role.SUPERADMIN))])
    node = APIRouter()  # per-endpoint node auth (#207 per-worker command keys)

    # --- admin: enqueue / history / read -----------------------------------
    @admin.post("/api/workers/{worker_id}/commands")
    def enqueue(worker_id: str, payload: CommandCreate, request: Request):
        if payload.kind not in ADMIN_ENQUEUEABLE_KINDS:
            raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(ADMIN_ENQUEUEABLE_KINDS)}")
        # LLMM-2: this route can enqueue a raw `load_engine` — engine capacity,
        # provisioned without going through inventory's `_enqueue_engine` (where
        # the #265 ENT3 gate now lives), so it was the one capacity path the
        # shared placement gate cannot cover. Gate it here, on the SAME helper,
        # so the two cannot drift. Imported at call time on purpose: inventory
        # imports `enqueue_command` from this module at import time, so a
        # module-level import back would be a cycle. Only PROVISIONING kinds are
        # gated — restart/stop/unload/tail_logs/list_disk_models must keep
        # working on a lapsed box (they free or inspect, never provision).
        if payload.kind in CAPACITY_PROVISIONING_KINDS:
            from app.api.inventory import _cache, _entitlement_gate

            _entitlement_gate(_cache(request), f"enqueue {payload.kind}")
        with session_scope() as s:
            if s.get(Worker, parse_uuid(worker_id, "worker_id")) is None:
                raise HTTPException(status_code=404, detail="worker not found")
            # #1183: read-only kinds may be served stale-while-revalidate —
            # last answer now, one refresh behind it. Actions never are.
            if payload.revalidate and payload.kind in REVALIDATE_KINDS:
                cmd, extra = revalidate_command(s, parse_uuid(worker_id, "worker_id"),
                                                payload.kind, payload.args)
                s.flush()
                return {**_row(cmd), **extra}
            cmd = enqueue_command(s, parse_uuid(worker_id, "worker_id"),
                                  payload.kind, payload.args)
            s.flush()
            return _row(cmd)

    @admin.get("/api/workers/{worker_id}/commands")
    def history(worker_id: str, limit: int = 25):
        with session_scope() as s:
            q = (
                s.query(NodeCommand)
                .filter(NodeCommand.worker_id == parse_uuid(worker_id, "worker_id"))
                .order_by(NodeCommand.created_at.desc())
                .limit(min(limit, 200))
            )
            return [_row(c) for c in q.all()]

    @admin.get("/api/commands/{command_id}")
    def read(command_id: str):
        with session_scope() as s:
            c = s.get(NodeCommand, parse_uuid(command_id, "command_id"))
            if c is None:
                raise HTTPException(status_code=404, detail="command not found")
            return _row(c)

    # --- node auth: claim / result (#207 per-worker keys) -------------------
    # No router-level dependency: each endpoint authorizes against the SPECIFIC
    # worker's per-worker key (or the shared node key in mode=allow), so a node
    # can't claim/complete another worker's commands.
    @node.post("/api/workers/{worker_id}/commands/claim")
    def claim(worker_id: str, authorization: Optional[str] = Header(default=None)):
        """Return this worker's pending commands and mark them claimed.

        #326: the claim is now genuinely atomic. It used to be a plain SELECT
        followed by an UPDATE — a check-then-act race despite the docstring
        saying otherwise. Two overlapping claims (the node retrying, a slow
        response, two report cycles overlapping) both saw the same `pending`
        rows and both returned them, so the command executed TWICE. For
        `load_engine` that means two engines racing for the same VRAM.

        `FOR UPDATE SKIP LOCKED` is the standard queue primitive: the first
        claimer locks the rows it is taking, and a concurrent claimer SKIPS them
        rather than blocking or duplicating. Requires PostgreSQL, which is what
        this schema targets (JSONB columns throughout).
        """
        now = _dt.datetime.now(_dt.timezone.utc)
        with session_scope() as s:
            w = s.get(Worker, parse_uuid(worker_id, "worker_id"))
            authorize_command_node(authorization, w.name if w else None,
                                   mode=get_settings().command_key_mode)
            # #1633: before handing out new work, let go of work this worker
            # claimed and never finished. A node that restarts claims again
            # within seconds, and that claim is the one moment its own orphans
            # can be recognised without a scheduler.
            expire_stale_claims(s, parse_uuid(worker_id, "worker_id"), now)
            pend = (
                s.query(NodeCommand)
                .filter(NodeCommand.worker_id == parse_uuid(worker_id, "worker_id"),
                        NodeCommand.status == "pending")
                .order_by(NodeCommand.created_at)
                .with_for_update(skip_locked=True)
                .all()
            )
            out = []
            for c in pend:
                c.status = "claimed"
                c.claimed_at = now
                out.append(_row(c))
            return out

    @node.post("/api/commands/{command_id}/result")
    def result(command_id: str, payload: CommandResult,
               authorization: Optional[str] = Header(default=None)):
        status = payload.status if payload.status in ("done", "failed") else "failed"
        with session_scope() as s:
            c = s.get(NodeCommand, parse_uuid(command_id, "command_id"))
            if c is None:
                # Authorize as a bare node before disclosing existence (no worker
                # context yet) so a random command_id probe can't bypass auth.
                authorize_command_node(authorization, None, mode=get_settings().command_key_mode)
                raise HTTPException(status_code=404, detail="command not found")
            w = s.get(Worker, c.worker_id)
            authorize_command_node(authorization, w.name if w else None,
                                   mode=get_settings().command_key_mode)
            c.status = status
            c.result = payload.result or {}
            c.finished_at = _dt.datetime.now(_dt.timezone.utc)
            # #1372: a node's `failed` for a load_engine was the ONLY place the
            # rejection existed. The optimistic 'scheduled' instance row sat
            # until the registration reconcile pruned it, and the deployment
            # read `pending` with reason null — for thirty minutes on 0.91.
            if c.kind == "load_engine" and status == "failed":
                _mark_load_failed(s, c, payload.result or {})
            # #549 R3: a deploy_runner result is the event the upgrade's
            # deploying state waits for. Best-effort — a result post must never
            # fail because the upgrade bookkeeping did.
            if w is not None:
                try:
                    from app.api.runner_upgrade import advance_runner_upgrade
                    advance_runner_upgrade(s, w)
                except Exception:  # pragma: no cover
                    logger.warning("runner-upgrade advancement failed", exc_info=True)
            return {"id": command_id, "status": status}

    # ── #549 R2: runner-image lifecycle routes ──────────────────────────────
    # Dedicated + payload-validating, per the ADMIN_ENQUEUEABLE_KINDS decision:
    # these kinds never ride the generic endpoint, because their args are an
    # image reference that must be checked before a node acts on it. The node
    # re-validates AND enforces its registry allow-list — the manager's check is
    # the fast 422 naming the field, not the security boundary.
    # (RunnerImageRequest is a MODULE-level class — see the #1195 note there.)

    def _validated_runner_ref(image: str) -> str:
        from app.api.inventory import registry_qualified_runner_image, valid_runner_image

        if not valid_runner_image(image):
            raise HTTPException(status_code=422, detail=(
                f"image {image!r} is not a valid image reference"))
        # deploy/remove refs must be REGISTRY-QUALIFIED (host[:port]/path:tag):
        # a bare name would make the node's docker pull default to docker.io,
        # which an air-gapped box must never attempt (#307). The first path
        # component is a registry exactly when it contains '.' or ':' — the
        # same rule docker itself uses. ONE predicate, shared with the #1187
        # per-deployment runner pin (PATCH /api/deployments), so they cannot
        # drift apart.
        if not registry_qualified_runner_image(image):
            raise HTTPException(status_code=422, detail=(
                f"image {image!r} is not registry-qualified — runner images are "
                f"distributed from the master's registry (e.g. "
                f"llm-registry:5000/runners/llama-vulkan:b9100), never from "
                f"docker.io (#549 R2)"))
        return image

    def _worker_or_404(s, worker_id: str):
        w = s.get(Worker, parse_uuid(worker_id, "worker_id"))
        if w is None:
            raise HTTPException(status_code=404, detail="worker not found")
        return w

    @admin.post("/api/workers/{worker_id}/runners")
    def deploy_runner(worker_id: str, payload: RunnerImageRequest):
        """Enqueue a pull of a runner image onto the node (async on the node —
        an image pull is minutes, and #364 keeps it off the report loop)."""
        image = _validated_runner_ref(payload.image)
        with session_scope() as s:
            _worker_or_404(s, worker_id)
            cmd = enqueue_command(s, parse_uuid(worker_id, "worker_id"),
                                  "deploy_runner", {"image": image})
            s.flush()
            return _row(cmd)

    @admin.post("/api/workers/{worker_id}/runners/remove")
    def remove_runner(worker_id: str, payload: RunnerImageRequest):
        """Enqueue removal of a runner image from the node. The node refuses if
        any container still uses it — a runner serving a deployment cannot be
        pulled out from under it; drain first (#261-C2)."""
        image = _validated_runner_ref(payload.image)
        with session_scope() as s:
            _worker_or_404(s, worker_id)
            cmd = enqueue_command(s, parse_uuid(worker_id, "worker_id"),
                                  "remove_runner", {"image": image})
            s.flush()
            return _row(cmd)

    @admin.post("/api/workers/{worker_id}/runners/list")
    def list_runner_images(worker_id: str, payload: Optional[RevalidateOpts] = None):
        """Enqueue an inventory of runner images present on the node. POST, not
        GET: the answer arrives via the command channel (poll the returned
        command id), the same shape as list_disk_models (#306). With a
        ``{"revalidate": true}`` body the last completed inventory rides along
        as ``stale_result`` and an in-flight refresh is reused (#1183)."""
        with session_scope() as s:
            _worker_or_404(s, worker_id)
            if payload is not None and payload.revalidate:
                cmd, extra = revalidate_command(s, parse_uuid(worker_id, "worker_id"),
                                                "list_runner_images", {})
                s.flush()
                return {**_row(cmd), **extra}
            cmd = enqueue_command(s, parse_uuid(worker_id, "worker_id"),
                                  "list_runner_images", {})
            s.flush()
            return _row(cmd)

    # ── #306 delete half: free disk by removing one cached weight file ──────
    # Dedicated + payload-validating, same posture as the runner-image routes
    # above: `name` is a caller-chosen path that must be checked before a node
    # acts on it, so this kind never rides the generic admin endpoint (kept
    # OUT of ADMIN_ENQUEUEABLE_KINDS on purpose).
    # (DiskModelDeleteRequest is a MODULE-level class — see the #1195 note there.)

    @admin.post("/api/workers/{worker_id}/disk-models/delete")
    def delete_disk_model(worker_id: str, payload: DiskModelDeleteRequest):
        """Enqueue deletion of one cached weight file from the node's model
        mount. The node is the security boundary: it re-checks containment AND
        refuses when the file backs a currently-loaded deployment (ready,
        loading, or pulling) — freeing weights out from under a live/starting
        engine is data-loss-adjacent, and the whole point of the cache is
        redeploying WITHOUT a re-download."""
        if not valid_disk_model_name(payload.name):
            raise HTTPException(status_code=422, detail=(
                f"name {payload.name!r} is not a valid cached-weight path"))
        with session_scope() as s:
            _worker_or_404(s, worker_id)
            cmd = enqueue_command(s, parse_uuid(worker_id, "worker_id"),
                                  "delete_disk_model", {"name": payload.name})
            s.flush()
            return _row(cmd)

    app.include_router(admin)
    app.include_router(node)
