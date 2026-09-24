# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""node-side command channel (#261 C1).

The manager enqueues commands for this worker; we long-poll (outbound-only —
NAT-friendly), execute via the engine drivers, and report the result. Only the
enumerated kinds — never arbitrary shell. Pure/injectable so it unit-tests with
no docker/network.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("node_agent.commands")

# #364 Commands that are measured in minutes to tens of minutes and must NOT run
# on the node's single report loop. That loop also does supervision and the
# heartbeat, so an inline mirror stops engine restarts and freezes
# `last_heartbeat` — and the manager's _WORKER_STALE_SECONDS is 90, so after a
# minute and a half the worker reads as STALE and `_pick_worker` stops selecting
# it. Mirroring a 40 GB model made a perfectly healthy node look dead and
# unschedulable for the whole mirror.
#
# The asymmetry was the tell: pull-on-deploy already spawns a thread and reports
# `pulling %`, and auto-cache-on-deploy is documented as a background push. The
# one command an operator explicitly asks for was the one that ran inline.
#
# Deliberately NOT every kind. tail_logs / restart_engine / stop_engine /
# load_engine / unload_engine / list_disk_models are all bounded and fast
# (load_engine returns immediately — its weight fetch is already threaded), and
# backgrounding them would only add ordering surprises to commands an operator
# expects to have taken effect by the time the call returns.
# deploy_runner is here because an image pull is minutes on a cold node — the
# same reasoning as the two originals. remove_runner and list_runner_images are
# local docker calls and stay inline. delete_disk_model (#306) is a single
# os.remove() plus an in-memory in-use check — bounded and fast, stays inline.
ASYNC_COMMAND_KINDS = ("mirror_model", "pull_artifact", "deploy_runner")


class AsyncDispatchRejected(Exception):
    """The dispatcher DECLINED this command — do not run it inline (NODE-15).

    Distinct from a dispatcher that BROKE (any other exception), which still
    falls back to inline execution because losing an operator's mirror is worse
    than a slow cycle. A rejection is a deliberate answer — today: the node's
    background queue is at its cap — and the right response is to report the
    command failed so the manager can retry, not to run the very thing on the
    report loop that #364 moved off it.
    """


# #363: the container-targeting commands (tail_logs / restart_engine / stop_engine)
# reach docker through the SCOPED socket-proxy, which filters by API endpoint and NOT
# by container — so an unvalidated `args.container` would let the manager stop, remove
# or read the logs of ANY container on the box (gpustack, postgres, authentik-server,
# caddy). Every engine this node launches carries the rzfz.role label (set by each
# driver's LaunchSpec), so requiring it enforces the same "only touch what we own"
# invariant the supervisor already documents — and keeps a coexisting GPUStack, which
# the LLM Manager is explicitly designed to run alongside, off-limits.
ENGINE_ROLE_LABEL = "rzfz.role"
ENGINE_ROLE = "llm-engine"


def _owned_engine(docker, args: dict):
    """The container named by ``args['container']``, but ONLY if this node owns it
    (labelled ``rzfz.role=llm-engine``). Raises ValueError otherwise — the caller
    reports the command as failed with the reason."""
    name = (args or {}).get("container")
    if not name:
        raise ValueError("command args.container is required")
    try:
        container = docker.containers.get(name)
    except Exception as exc:
        raise ValueError(f"container {name!r} not found on this node") from exc
    labels = getattr(container, "labels", None) or {}
    if labels.get(ENGINE_ROLE_LABEL) != ENGINE_ROLE:
        raise ValueError(
            f"refusing to act on {name!r}: not an llm-engine container owned by this "
            f"node ({ENGINE_ROLE_LABEL}={labels.get(ENGINE_ROLE_LABEL)!r})"
        )
    return container


def execute_command(cmd: dict, *, docker, load_fn=None, unload_fn=None, mirror_fn=None,
                    disk_fn=None, pull_fn=None, deploy_runner_fn=None,
                    remove_runner_fn=None, list_runners_fn=None, evict_fn=None,
                    delete_disk_fn=None) -> dict:
    """Run one command; return a JSON-able result dict. Raises on failure (the
    caller records status=failed with the message)."""
    kind = cmd.get("kind")
    args = cmd.get("args") or {}

    if kind == "tail_logs":
        from app.drivers.base import engine_logs

        _owned_engine(docker, args)
        raw = engine_logs(args["container"], docker, tail=int(args.get("tail", 200)))
        text = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return {"container": args["container"], "logs": text[-20000:]}

    if kind == "restart_engine":
        container = _owned_engine(docker, args)
        container.restart(timeout=int(args.get("timeout", 20)))
        return {"restarted": args["container"]}

    if kind == "stop_engine":
        from app.drivers.base import stop_engine

        _owned_engine(docker, args)
        stop_engine(args["container"], docker)
        return {"stopped": args["container"]}

    if kind == "load_engine":
        if load_fn is None:
            raise ValueError("load handler not wired")
        return load_fn(args)

    if kind == "unload_engine":
        if unload_fn is None:
            raise ValueError("unload handler not wired")
        # NODE-1 / #363: unload force-removes its target (stop_engine ->
        # remove(force=True)) — the same blast radius stop_engine is guarded
        # against, but this branch skipped the check, so the manager (or anyone
        # holding the node key) could force-remove postgres / caddy /
        # authentik-server. Refuse a container that EXISTS on the box and is not
        # one of our engines. A name resolving to NO container is left to the
        # handler: stop_engine is a documented no-op on a missing container
        # ("safe to retry"), so unload's state-cleanup stays idempotent for an
        # engine that already vanished.
        name = args.get("instance_id") or args.get("container")
        if not name:
            raise ValueError("unload_engine requires args.instance_id or args.container")
        try:
            container = docker.containers.get(name)
        except Exception:
            container = None
        if container is not None:
            labels = getattr(container, "labels", None) or {}
            if labels.get(ENGINE_ROLE_LABEL) != ENGINE_ROLE:
                raise ValueError(
                    f"refusing to unload {name!r}: not an llm-engine container "
                    f"owned by this node ({ENGINE_ROLE_LABEL}="
                    f"{labels.get(ENGINE_ROLE_LABEL)!r})")
        return unload_fn(args)

    if kind == "mirror_model":
        # #307: fetch weights from HF (master) and push them into the in-stack
        # Zot registry — cache a model WITHOUT deploying it.
        if mirror_fn is None:
            raise ValueError("mirror handler not wired")
        return mirror_fn(args)

    if kind == "pull_artifact":
        # #353: pull a model's files BY DIGEST from the master's Zot into this
        # node's models mount. The mirror image of mirror_model: mirror pushes
        # HF -> registry on the master, this pulls registry -> node. Until this
        # existed, puller.py was correct, tested, and never called by anything.
        if pull_fn is None:
            raise ValueError("pull handler not wired")
        return pull_fn(args)

    if kind == "deploy_runner":
        # #549 R2: pull a runner image onto this node from the master's registry.
        # The handler enforces the registry allow-list — this node never pulls
        # from the public internet, whatever the channel says (#307 posture).
        if deploy_runner_fn is None:
            raise ValueError("deploy_runner handler not wired")
        return deploy_runner_fn(args)

    if kind == "remove_runner":
        # #549 R2: delete a runner image. The handler refuses while any container
        # uses it — a runner serving a deployment cannot be pulled out from
        # under it.
        if remove_runner_fn is None:
            raise ValueError("remove_runner handler not wired")
        return remove_runner_fn(args)

    if kind == "list_runner_images":
        # #549 R2: inventory the runner images on this node — the sibling of
        # list_disk_models (#306) for the image axis.
        if list_runners_fn is None:
            raise ValueError("list_runners handler not wired")
        return list_runners_fn(args)

    if kind == "list_disk_models":
        # #306: enumerate the weights this worker physically holds on disk
        # (the node's own model mount) — distinct from the master Zot cache.
        if disk_fn is None:
            raise ValueError("disk handler not wired")
        return disk_fn(args)

    if kind == "evict_weights":
        # #307 S3: delete a model's weight files off THIS worker's on-disk
        # cache — the per-worker leg of "remove from fleet". The manager
        # dispatches this only to workers its inventory (#306) already showed
        # as having the files; the handler itself is still idempotent about a
        # file that's already gone (a second evict, or a race with a manual
        # cleanup, is not an error).
        if evict_fn is None:
            raise ValueError("evict handler not wired")
        return evict_fn(args)

    if kind == "delete_disk_model":
        # #306 delete half: free disk by removing one cached weight file.
        # The handler enforces containment (never escapes the mount) AND the
        # in-use guard (refuses a file backing a currently-loaded deployment).
        if delete_disk_fn is None:
            raise ValueError("delete_disk handler not wired")
        return delete_disk_fn(args)

    raise ValueError(f"unknown command kind: {kind}")


def run_command_cycle(worker_id, *, manager_url, node_key, http_post, execute,
                      command_key=None, dispatch_async=None) -> list:
    """Claim this worker's pending commands, execute each, report the result.
    Best-effort: one command failing never aborts the cycle. Returns a list of
    (command_id, status).

    #207: present the per-worker command key when configured (scoped to this
    worker), else fall back to the shared node key (back-compat).

    #364: when ``dispatch_async`` is supplied, a command whose kind is in
    ASYNC_COMMAND_KINDS is handed to it and reported as ``"running"`` here; the
    dispatcher executes and reports it from another thread. Without a dispatcher
    (every existing caller, and the tests) behaviour is exactly as before — the
    command runs inline. A dispatcher that itself fails falls back to inline
    rather than dropping the command: blocking the loop is bad, losing an
    operator's mirror is worse."""
    base = manager_url.rstrip("/")
    hdr = {"authorization": f"Bearer {command_key or node_key}"}
    try:
        resp = http_post(f"{base}/api/workers/{worker_id}/commands/claim", json={}, headers=hdr)
    except Exception:  # pragma: no cover - defensive
        logger.exception("command claim failed")
        return []
    try:
        cmds = resp.json() if hasattr(resp, "json") else resp
    except ValueError:
        # #2447: a slow or restarting manager can answer the claim with an empty
        # or non-JSON body. That is "nothing claimed this cycle", not a crash —
        # the next cycle asks again. No traceback, the status says enough.
        logger.warning("command claim: the manager answered HTTP %s without a JSON body; "
                       "asking again next cycle", getattr(resp, "status_code", "?"))
        return []

    def _report(cid, status, result) -> None:
        try:
            http_post(f"{base}/api/commands/{cid}/result",
                      json={"status": status, "result": result}, headers=hdr)
        except Exception:  # pragma: no cover - defensive
            logger.exception("command result report failed for %s", cid)

    def _run_and_report(c) -> str:
        cid = c.get("id")
        try:
            result = execute(c)
            status = "done"
        except Exception as exc:  # noqa: BLE001 - report, never crash the loop
            result = {"error": str(exc)[:500]}
            status = "failed"
            logger.warning("command %s (%s) failed: %s", cid, c.get("kind"), exc)
        _report(cid, status, result)
        return status

    done = []
    for c in cmds or []:
        cid = c.get("id")
        if dispatch_async is not None and c.get("kind") in ASYNC_COMMAND_KINDS:
            try:
                dispatch_async(c, _run_and_report)
                done.append((cid, "running"))
                continue
            except AsyncDispatchRejected as exc:
                # NODE-15: a deliberate refusal (queue full) — report it and move
                # on. Running it inline would re-create the stall #364 removed.
                logger.warning("async dispatch REFUSED %s (%s): %s",
                               cid, c.get("kind"), exc)
                _report(cid, "failed", {"error": str(exc)[:500]})
                done.append((cid, "failed"))
                continue
            except Exception:  # pragma: no cover - defensive
                logger.exception("async dispatch failed for %s — running inline", cid)
        done.append((cid, _run_and_report(c)))
    return done
