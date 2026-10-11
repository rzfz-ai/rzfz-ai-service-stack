# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""BSB-16 — Post-toggle Tier-D acceptance probe runner.

Wired into ``apply_manager._execute_toggle`` so that when a profile is
ENABLED via the Configuration Portal, the relevant Tier-D acceptance
probes (one per container that has a ``tests/acceptance/<name>/``
subdir) are kicked off automatically. The result is reported back into
the apply-action's SSE line stream so the UI can render a green/red
toast and the audit log records the probe outcome alongside the
toggle.

DISABLE toggles do NOT trigger probes — there's nothing to verify.

Why this lives in a separate module
-----------------------------------
- Keeps ``apply_manager`` narrow (it's already 600+ lines).
- Lets the probe runner be unit-tested independently from the live
  threading + docker-compose machinery in ``_execute_toggle``.
- The "how does the container reach the host's test-runner?" question
  (see decisions log BSB-16-DEC-01) is contained here so any future
  swap (host-side queue, dedicated probe sidecar) only touches this
  one file.

Container-name → acceptance-dir mapping
---------------------------------------
``tests/acceptance/`` uses snake_case directory names. Profile
container names use kebab-case (``dify-api``, ``dify-web``). We
canonicalise via ``str.replace('-', '_')`` and check directory
existence; misses are silently ignored (not every container has a
probe yet — that's a known gap, not a hard error).

#1190 — wait for health first, and never call an infra problem a FAIL
---------------------------------------------------------------------
The operator saw ``gitea: FAIL (4 ms)`` straight after ``up -d``. Two
things were wrong with that line:

* The probe fired the instant compose returned. Nothing is healthy 4 ms
  after start — ``wait_for_containers_ready`` now polls the enabled
  services' container state (``docker compose ps``) with backoff, bounded
  by ``HEALTH_WAIT_TIMEOUT_SECONDS``, before the first probe runs.
  Services without a healthcheck count as ready once they have been
  ``running`` for ``HEALTH_WAIT_GRACE_SECONDS``; one-shots that exited 0
  are ready immediately.
* 4 ms is not a probe result. The Portal container mounts the repo
  read-only (BSB-03), so ``cli/test.sh`` dies at its first ``mkdir`` under
  ``tests/results/`` — and its stderr was dropped, so the operator saw a
  bare FAIL. ``probe_skip_reason`` now checks those preconditions up
  front and reports SKIPPED with the host command to run instead; the
  runner's stderr is merged into the captured tail; on a real FAIL a
  bounded tail is streamed into the action so the reason is visible where
  the operator is looking. There is no results file the container could
  write, so nothing points at ``tests/results/`` any more.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional


# Cap probe runtime so a hung container can't pin the apply-action
# forever. 5 minutes mirrors the M032-S02 PROFILE_UP_TIMEOUT_SECONDS
# upper bound — if the module's containers haven't passed their
# acceptance probe within 5 minutes of becoming Up, something is wrong.
PROBE_TIMEOUT_SECONDS = 5 * 60

# #1190 — readiness wait in front of the first probe. Bounded so a module
# that never becomes healthy costs the operator 90 s, not forever; the
# probe then runs against whatever state the module is in and its FAIL is
# a real signal ("still not healthy after 90 s"), not start-up noise.
HEALTH_WAIT_TIMEOUT_SECONDS = 90
# A service without a healthcheck cannot report readiness. Treat it as
# ready once it has stayed `running` this long — a crash-loop flips it to
# `restarting`/`exited` well inside the grace.
HEALTH_WAIT_GRACE_SECONDS = 5
HEALTH_WAIT_INITIAL_INTERVAL = 1.0
HEALTH_WAIT_MAX_INTERVAL = 10.0
HEALTH_WAIT_BACKOFF = 1.5
# `docker compose ps` is one CLI round-trip through the socket proxy —
# generous bound; a hang here must not eat the whole wait budget.
HEALTH_QUERY_TIMEOUT_SECONDS = 20

# On a real FAIL, this many trailing runner lines are streamed into the
# action so the operator sees WHY without leaving the Portal.
PROBE_FAIL_TAIL_LINES = 8
# Lines kept on the structured result (audit strips them to `last_line`).
PROBE_RESULT_TAIL_LINES = 20


def _container_to_acceptance_dirname(container_name: str) -> str:
    """``dify-api`` → ``dify_api``; ``openwebui`` → ``openwebui``."""
    return container_name.replace("-", "_")


def resolve_probe_targets(
    profile: dict,
    *,
    stack_root: Path,
) -> List[str]:
    """Map a profile dict's container list to acceptance probe targets.

    Returns a sorted list of acceptance-dir names (relative to
    ``tests/acceptance/``). Containers without a corresponding directory
    are silently skipped (they have no probe yet — that's not an error).
    """
    accept_root = Path(stack_root) / "tests" / "acceptance"
    if not accept_root.is_dir():
        return []
    targets: List[str] = []
    for container in profile.get("containers") or []:
        name = container.get("name") if isinstance(container, dict) else None
        if not name:
            continue
        dirname = _container_to_acceptance_dirname(name)
        if (accept_root / dirname).is_dir():
            targets.append(dirname)
    return sorted(set(targets))


# ── #1190: readiness wait ─────────────────────────────────────────────────────

def _parse_compose_ps(stdout: str) -> List[dict]:
    """`docker compose ps --format json` is NDJSON on compose ≥ 2.21 (the
    fleet standard, 2.40.x) and a JSON array on older builds. Accept both."""
    text = (stdout or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [d for d in data if isinstance(d, dict)]
    out: List[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def query_service_states(
    profile_id: str,
    services: Iterable[str],
    *,
    stack_root: Path,
    timeout: float = HEALTH_QUERY_TIMEOUT_SECONDS,
) -> Optional[Dict[str, dict]]:
    """One `docker compose ps -a` round-trip, scoped to `services`.

    Returns ``{service: {name, state, health, exit_code}}`` — a service
    with no container yet is simply absent. ``None`` when compose could
    not be asked (non-zero exit, no docker, timeout): the caller treats
    that as "readiness unknown" and does not block on it.

    Compose is asked with ``--profile`` explicitly, mirroring
    ``ApplyManager._compose_profile_services`` (#538), so the answer does
    not depend on the .env write having landed in compose's view yet.
    ``-a`` includes exited one-shots. ``--format json`` fields used:
    ``Service``, ``Name``, ``State``, ``Health`` ('' when the service has
    no healthcheck), ``ExitCode``.
    """
    services = [s for s in services if s]
    cmd = [
        "docker", "compose", "--profile", profile_id,
        "ps", "-a", "--format", "json", *services,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(stack_root),
            timeout=timeout, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    states: Dict[str, dict] = {}
    for entry in _parse_compose_ps(result.stdout):
        svc = entry.get("Service") or ""
        if not svc:
            continue
        try:
            exit_code = int(entry.get("ExitCode") or 0)
        except (TypeError, ValueError):
            exit_code = 0
        states[svc] = {
            "name": entry.get("Name") or svc,
            "state": (entry.get("State") or "").lower(),
            "health": (entry.get("Health") or "").lower(),
            "exit_code": exit_code,
        }
    return states


def _readiness(entry: Optional[dict], elapsed: float, grace_s: float):
    """Classify one service. Returns ``(ready, verdict, waiting_on_grace)``.

    * healthcheck present AND container running → only ``healthy`` is ready
      (``starting`` and ``unhealthy`` keep waiting; ``unhealthy`` can still
      recover inside the bound, and the timeout line names it if it does not).
      A health value on a container that is NOT running is stale (#1241) and
      never counts — the state rules below apply.
    * no healthcheck → ``running`` for ≥ grace is ready; ``exited`` with
      code 0 is a finished one-shot and ready at once; anything else
      (``created``, ``restarting``, ``exited`` non-zero, ``dead``) waits.
    * no container at all → waits.
    """
    if entry is None:
        return False, "missing", False
    state = entry.get("state") or ""
    health = entry.get("health") or ""
    # A finished one-shot is ready whatever its last probe said — `exited 0`
    # is the terminal success of a job that MAY have carried a healthcheck.
    # Provenance (DevBox box-verify on #1352, Docker 29.1.3): the daemon flips
    # Health.Status to `unhealthy` itself the moment a container leaves
    # `running` (exit 3 / kill / stop, with and without a restart policy), so
    # a stale `healthy` on a non-running container was NOT observed there.
    # The fixture below is SYNTHETIC — defence in depth for an older or a
    # different engine, and for a compose-ps/inspect race. Do not spend hours
    # trying to reproduce it on 29.x; measure the daemon you have instead.
    # This rule comes first on purpose: the stale-health rule below must not
    # turn a completed bootstrap into "not ready" (#1220 pin, #1241).
    if state == "exited" and entry.get("exit_code", 1) == 0:
        return True, "exited 0 (one-shot)", False
    # #1241: health is only meaningful for a RUNNING container. Docker keeps
    # the last probe result on the container object, so a container that
    # crashed after a green probe sits in `restarting` / `exited` non-zero /
    # `dead` with a stale `healthy` — and this used to call that READY. State
    # first; a health verdict on anything but `running` is history.
    if health and state != "running":
        return False, f"{state}, stale health={health}", False
    if health:
        if health == "healthy":
            return True, "healthy", False
        return False, f"{state}, health={health}", False
    if state == "running":
        if elapsed >= grace_s:
            return True, (f"running, no healthcheck — proceeding after "
                          f"{int(grace_s)} s grace"), False
        return False, "running, no healthcheck (grace)", True
    return False, (state or "unknown"), False


def wait_for_containers_ready(
    profile_id: str,
    services: Iterable[str],
    *,
    stack_root: Path,
    action=None,
    timeout_s: float = HEALTH_WAIT_TIMEOUT_SECONDS,
    grace_s: float = HEALTH_WAIT_GRACE_SECONDS,
    query: Optional[Callable[[str, List[str]], Optional[Dict[str, dict]]]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Block until every service in `services` is ready, or `timeout_s`.

    Polls `query(profile_id, services)` (default: ``query_service_states``
    against `stack_root`) starting at 1 s and backing off ×1.5 up to 10 s;
    never sleeps past the bound. Progress is streamed into `action`: each
    service is announced once when it becomes ready, then a summary line.

    Never raises. If the query fails or throws, readiness is UNKNOWN and
    the function returns immediately — a probe that cannot see the
    containers must not stall the enable on top of it.

    Returns::

        {'waited_ms': int, 'polls': int, 'timed_out': bool,
         'ready': [service, ...],
         'not_ready': [{'service', 'state', 'health'}, ...],
         'unknown_reason': Optional[str]}
    """
    services = [s for s in services if s]
    result = {
        "waited_ms": 0, "polls": 0, "timed_out": False,
        "ready": [], "not_ready": [], "unknown_reason": None,
    }
    if not services:
        result["unknown_reason"] = "no services to wait for"
        return result
    if query is None:
        def query(pid, svcs):  # noqa: E306 — default adapter
            return query_service_states(pid, svcs, stack_root=stack_root)

    def _line(text):
        if action is not None:
            action.add_line(text)

    _line(f"  [post-toggle probe] waiting for container health "
          f"(up to {int(timeout_s)} s): {', '.join(services)}")

    start = clock()
    interval = HEALTH_WAIT_INITIAL_INTERVAL
    announced: List[str] = []
    while True:
        result["polls"] += 1
        try:
            states = query(profile_id, list(services))
        except Exception as e:  # never crash the apply action
            states = None
            failure = f"{e!r}"
        else:
            failure = "docker compose ps failed"
        elapsed = clock() - start
        if states is None:
            result["unknown_reason"] = f"readiness query failed: {failure}"
            result["waited_ms"] = int(elapsed * 1000)
            _line(f"  [post-toggle probe] container health unknown "
                  f"({result['unknown_reason']}) — probing without waiting")
            return result

        pending: List[dict] = []
        grace_only = True
        for svc in services:
            entry = states.get(svc)
            ready, verdict, on_grace = _readiness(entry, elapsed, grace_s)
            if ready:
                if svc not in announced:
                    announced.append(svc)
                    _line(f"    {svc}: {verdict} after {int(elapsed)} s"
                          if verdict == "healthy" else f"    {svc}: {verdict}")
                continue
            grace_only = grace_only and on_grace
            pending.append({
                "service": svc,
                "state": (entry or {}).get("state") or "missing",
                "health": (entry or {}).get("health") or "",
            })

        if not pending:
            result["ready"] = list(announced)
            result["waited_ms"] = int(elapsed * 1000)
            _line(f"  [post-toggle probe] all {len(services)} container(s) "
                  f"ready after {int(elapsed)} s")
            return result

        if elapsed >= timeout_s:
            result["ready"] = list(announced)
            result["not_ready"] = pending
            result["timed_out"] = True
            result["waited_ms"] = int(elapsed * 1000)
            still = ", ".join(
                f"{p['service']} ({p['state']}"
                + (f", health={p['health']}" if p["health"] else "")
                + ")"
                for p in pending)
            _line(f"  [post-toggle probe] health wait timed out after "
                  f"{int(timeout_s)} s — still not ready: {still}")
            return result

        nap = min(interval, timeout_s - elapsed)
        if grace_only:
            # Everything left is a no-healthcheck service sitting out its
            # grace — no point sleeping longer than the grace itself.
            nap = min(nap, max(grace_s - elapsed, 0.1))
        sleep(nap)
        interval = min(interval * HEALTH_WAIT_BACKOFF, HEALTH_WAIT_MAX_INTERVAL)


# ── #1190: preconditions — infra problems are SKIP, never FAIL ───────────────

# ── #1223 (operator decision E4): the probe runs HOST-SIDE ───────────────────
# The Portal mounts the repo :ro (BSB-03), so `cli/test.sh` dies at its first
# mkdir and the probe was reported as SKIPPED on every hardened box — i.e. it
# never ran anywhere it mattered. Decision E4 (2026-09-05, #979): the Portal
# does not get a writable runner; it QUEUES the request and the host runs it
# (`rzfz probe --pending`, cli/probe.sh). The queue is one narrow rw mount, the
# same shape as the certs/mac-gateway mounts, and carries no secrets: a profile
# id, the acceptance targets, a timestamp.
PROBE_QUEUE_DIRNAME = ".probe-queue"
QUEUE_REQUEST_SUFFIX = ".request.json"
QUEUE_RESULT_SUFFIX = ".result.json"


def probe_queue_dir(stack_root: Path) -> Path:
    return Path(stack_root) / PROBE_QUEUE_DIRNAME


def _request_id(profile_id: str, now: float) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in profile_id)
    return f"{stamp}-{safe}"


def enqueue_probe_request(
    profile_id: str,
    targets: Iterable[str],
    *,
    stack_root: Path,
    services: Optional[Iterable[str]] = None,
    action=None,
    now: Optional[float] = None,
) -> dict:
    """Write ONE probe request for the host runner and report what happened.

    Never raises: a queue that cannot be written is a line in the action
    stream and a summary the audit entry records, never a failed toggle.
    """
    targets = [t for t in targets]
    if not targets:
        reason = "no acceptance probes found for this profile's containers"
        if action is not None:
            action.add_line(f"  [post-toggle probe] skipped: {reason}")
        return {"overall_pass": None, "targets": [], "queued": False,
                "skipped_reason": reason}

    now = time.time() if now is None else now
    qdir = probe_queue_dir(stack_root)
    rid = _request_id(profile_id, now)
    payload = {
        "id": rid,
        "profile": profile_id,
        "targets": targets,
        # #1223 review, finding 2: the readiness wait (#1190) was described as
        # "it happens in the host runner now" and was in fact deleted —
        # `wait_for_containers_ready` had no caller left, so `rzfz probe
        # --pending` measured against containers that were still coming up.
        # The runner cannot know which services belong to the profile, so the
        # request carries them. Absent (a request queued before this change) →
        # the runner skips the wait and says so, rather than guessing.
        "services": [s for s in (services or []) if s],
        "queued_at": int(now),
        "requested_by": "config-portal",
        "command": f"rzfz probe --pending",
    }
    try:
        qdir.mkdir(parents=True, exist_ok=True)
        dst = qdir / f"{rid}{QUEUE_REQUEST_SUFFIX}"
        tmp = qdir / f".{rid}{QUEUE_REQUEST_SUFFIX}.tmp"
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(dst))
    except Exception as exc:
        reason = (f"could not queue the probe in {qdir} ({exc!r}) — "
                  f"run `rzfz test {targets[0]} --acceptance` on the host")
        if action is not None:
            action.add_line(f"  [post-toggle probe] skipped: {reason}")
        return {"overall_pass": None, "targets": [], "queued": False,
                "skipped_reason": reason}

    # #1556 part c: this profile's PREVIOUS answered set is now superseded —
    # the toggle that just happened is the thing the next verdict is about.
    # After the write, so a queue that cannot be tidied still leaves a valid
    # request behind.
    dropped = prune_finished_requests(profile_id, stack_root=stack_root, keep_id=rid)
    if dropped and action is not None:
        action.add_line(
            f"  [post-toggle probe] superseded {len(dropped)} earlier "
            f"result(s) for {profile_id}")

    if action is not None:
        action.add_line(
            f"  [post-toggle probe] queued for the host: "
            f"{', '.join(targets)} (request {rid})")
        action.add_line(
            f"  [post-toggle probe] run `rzfz probe --pending` on the box to "
            f"execute it; the result lands in {PROBE_QUEUE_DIRNAME}/"
            f"{rid}{QUEUE_RESULT_SUFFIX}")
    return {"overall_pass": None, "targets": targets, "queued": True,
            "request_id": rid, "skipped_reason": None}


def _queue_sets(stack_root: Path) -> Dict[str, dict]:
    """Every request id in the queue with the files that belong to it.

    One id owns up to three files — request, result, marker — and they are
    only ever handled as a set. Anything else in the directory (the tracked
    README, a half-written .tmp) is not an id and is left alone.
    """
    qdir = probe_queue_dir(stack_root)
    sets: Dict[str, dict] = {}
    try:
        entries = list(qdir.iterdir())
    except Exception:
        return sets
    for path in entries:
        for suffix in (QUEUE_REQUEST_SUFFIX, QUEUE_RESULT_SUFFIX, QUEUE_AUDITED_SUFFIX):
            if path.name.endswith(suffix) and not path.name.startswith("."):
                rid = path.name[: -len(suffix)]
                sets.setdefault(rid, {})[suffix] = path
                break
    return sets


def _profile_of(rid: str, files: dict) -> Optional[str]:
    """Which profile a queue set belongs to.

    From the request file, and ONLY from it. The id embeds a sanitised profile
    name (`_request_id`), but a profile whose name needed sanitising would not
    map back — deleting somebody else's set on a name guessed from a filename
    is exactly the mistake this function exists to not make. No request file,
    no profile, no pruning.
    """
    req = files.get(QUEUE_REQUEST_SUFFIX)
    if req is None:
        return None
    try:
        payload = json.loads(req.read_text(encoding="utf-8"))
    except Exception:
        return None
    profile = payload.get("profile")
    return profile if isinstance(profile, str) and profile else None


def prune_finished_requests(profile_id: str, *, stack_root: Path,
                            keep_id: Optional[str] = None) -> List[str]:
    """Drop this profile's older ANSWERED probe sets. Returns the dropped ids.

    THE RULE, and what each half of it is for:

    * per profile, because the question the queue answers is per profile —
      "did *cognee* pass its acceptance after I enabled it?" A global
      keep-the-newest-N would let a busy profile push another profile's only
      verdict out, and nothing would say so.
    * only ANSWERED sets. A request without a result has not run yet; the host
      may be about to run it, and `rzfz probe --pending` finds work by exactly
      that absence. Deleting one would cancel a probe the operator asked for.
    * called at ENQUEUE time, so a verdict survives until the same profile is
      toggled again — until then it is the answer, and something has to still
      be there for anyone to read it (#1556 part b).
    * `keep_id` is the request being queued right now. It has no result yet, so
      the "answered" rule already spares it; naming it is belt and braces, and
      it says out loud that the new request is not a candidate.

    Never raises: a queue that cannot be tidied is not a failed toggle.
    """
    dropped: List[str] = []
    root = Path(stack_root)
    for rid, files in sorted(_queue_sets(root).items()):
        if rid == keep_id:
            continue
        if QUEUE_RESULT_SUFFIX not in files:
            continue                      # still pending — the host's work
        if _profile_of(rid, files) != profile_id:
            continue
        removed_any = False
        for path in files.values():
            try:
                path.unlink()
                removed_any = True
            except Exception:
                continue
        if removed_any:
            dropped.append(rid)
    return dropped


def read_probe_result(request_id: str, *, stack_root: Path) -> Optional[dict]:
    """The host runner's verdict for `request_id`, or None while it is
    still pending (or the file is unreadable)."""
    f = probe_queue_dir(stack_root) / f"{request_id}{QUEUE_RESULT_SUFFIX}"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def pending_requests(stack_root: Path) -> List[dict]:
    """Every queued request that has no result yet (oldest first)."""
    qdir = probe_queue_dir(stack_root)
    out: List[dict] = []
    try:
        names = sorted(p.name for p in qdir.iterdir()
                       if p.name.endswith(QUEUE_REQUEST_SUFFIX))
    except Exception:
        return out
    for name in names:
        rid = name[: -len(QUEUE_REQUEST_SUFFIX)]
        if (qdir / f"{rid}{QUEUE_RESULT_SUFFIX}").exists():
            continue
        try:
            out.append(json.loads((qdir / name).read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


# ── #1556: the verdict finds its way back into the audit trail ───────────────
#
# #1223/#1506 got the EXECUTION right — the Portal queues, the host runs, the
# result lands next to the request as JSON. What was missing is the return leg:
# `read_probe_result` and `pending_requests` had no production caller, the
# `profile.enable` entry recorded `overall_pass: null` and was never revisited,
# and the operator was left reading a file off the disk to learn whether the
# module he just enabled had passed its acceptance.
#
# WHO WRITES THE ENTRY. Not `rzfz probe`. The trail lives in a NAMED VOLUME
# (core/compose.yml: `razzfazz-config-audit:/var/log/razzfazz`), so there is no
# host path to append to, and the whole point of the host runner is that it
# works when nothing of the stack is up. So the Portal — the tree's only audit
# writer — carries over every verdict it has not carried over yet.
#
# A SEPARATE ENTRY, never an amendment. `profile.enable` said "queued" and that
# was true when it was written; an append-only log is not edited afterwards.
# `probe.result` is its own event and names the request it answers.
#
# EXACTLY ONCE, via a SIBLING file. The result file is written by the host; a
# field added to it by the Portal would make two parties owners of one file.
# `<id>.audited.json` is the Portal's own, and its absence is the whole
# question this reconciler asks.
#
# THE ORDER IS LOG-THEN-MARK, deliberately. A crash between the two repeats an
# entry on the next tick; the other order loses one. A duplicated audit line is
# a nuisance, a missing one is the thing this issue exists about.
QUEUE_AUDITED_SUFFIX = ".audited.json"

#: A target name the host runner would accept — the same rule cli/probe.sh
#: enforces before it execs. The result file is not attacker-controlled today
#: (the host writes it), but it is FILE input read by the Portal, and #1505
#: treats every Portal-reachable path as one; a name that could not have been
#: run has no business in the trail either.
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9_-]+$")


def _audited_marker(stack_root: Path, request_id: str) -> Path:
    return probe_queue_dir(stack_root) / f"{request_id}{QUEUE_AUDITED_SUFFIX}"


def unrecorded_results(stack_root: Path) -> List[dict]:
    """Every host verdict the trail has not been told about yet, oldest first.

    Returns one dict per request with the two halves side by side::

        {"id": str, "request": dict | None, "result": dict}

    `request` is None when the request file is gone (an operator tidying the
    queue, or a future cleanup rule): the verdict still gets its entry, just
    without the profile it belonged to. Losing the entry because the other
    file went missing would be the wrong trade.
    """
    qdir = probe_queue_dir(stack_root)
    out: List[dict] = []
    try:
        names = sorted(p.name for p in qdir.iterdir()
                       if p.name.endswith(QUEUE_RESULT_SUFFIX))
    except Exception:
        return out
    for name in names:
        rid = name[: -len(QUEUE_RESULT_SUFFIX)]
        if _audited_marker(stack_root, rid).exists():
            continue
        try:
            result = json.loads((qdir / name).read_text(encoding="utf-8"))
        except Exception:
            # Unreadable now (half-written, or corrupt): leave it unmarked and
            # look again next tick rather than burning the only chance to
            # record it.
            continue
        if not isinstance(result, dict):
            continue
        request = None
        try:
            request = json.loads(
                (qdir / f"{rid}{QUEUE_REQUEST_SUFFIX}").read_text(encoding="utf-8"))
        except Exception:
            request = None
        out.append({"id": rid, "request": request, "result": result})
    return out


def mark_result_recorded(request_id: str, *, stack_root: Path,
                         now: Optional[float] = None) -> bool:
    """Write the "this verdict is in the trail" marker. False if it could not
    be written — which means the entry will be repeated next tick."""
    now = time.time() if now is None else now
    marker = _audited_marker(Path(stack_root), request_id)
    payload = {"id": request_id, "audited_at": int(now),
               "audited_by": "config-portal"}
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(f".{marker.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(marker))
        return True
    except Exception:
        return False


def latest_verdict_for_profile(profile_id: str, *, stack_root: Path) -> Optional[dict]:
    """#1556 (Portal half) — what the operator gets to see on the module page.

    The execution path was finished in #1223/#1506 and the audit trail in the
    first half of this issue. The Portal itself still said nothing: it queued a
    request, printed "run `rzfz probe --pending` on the box", and then never
    mentioned the answer again. An operator who enabled a module had to read a
    JSON file off the disk to learn whether it had passed its own acceptance.

    Returns the NEWEST set for this profile as
    ``{"state", "request_id", "at", "detail"}`` — `state` is one of
    ``"pass" | "fail" | "refused" | "pending"`` — or None when this profile has
    never been probed. The wording comes from `verdict_audit_fields`, so the
    page and the trail cannot describe the same run differently.

    Newest by the request's own timestamp, not by filesystem mtime: a marker
    file written later must not reorder the runs it belongs to.
    """
    sets = _queue_sets(stack_root)
    best = None
    for rid, files in sets.items():
        if _profile_of(rid, files) != profile_id:
            continue
        req_path = files.get(QUEUE_REQUEST_SUFFIX)
        if req_path is None:
            continue
        try:
            request = json.loads(req_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        result = read_probe_result(rid, stack_root=stack_root)
        at = request.get("queued_at") or 0
        if best is not None and at <= best[0]:
            continue
        if result is None:
            entry = {"state": "pending", "request_id": rid, "at": at,
                     "detail": (f"acceptance probe {rid} is queued — run "
                                "`rzfz probe --pending` on the box to execute it")}
        else:
            fields = verdict_audit_fields({"id": rid, "request": request, "result": result})
            if result.get("refused_reason"):
                state = "refused"
            elif result.get("overall_pass") is True:
                state = "pass"
            else:
                state = "fail"
            entry = {"state": state, "request_id": rid, "at": at,
                     "detail": fields.get("detail") or ""}
        best = (at, entry)
    return best[1] if best else None


def verdict_audit_fields(entry: dict) -> dict:
    """The audit kwargs for one `{id, request, result}` pair.

    Split out from the recorder so the shape can be asserted without a logger,
    and so the one place that decides what an operator gets to read is one
    place.
    """
    rid = entry.get("id") or "?"
    request = entry.get("request") or {}
    result = entry.get("result") or {}
    rows = [r for r in (result.get("targets") or []) if isinstance(r, dict)]
    clean = [r for r in rows
             if isinstance(r.get("target"), str) and _SAFE_TARGET.match(r["target"])]
    overall = result.get("overall_pass")
    refused = result.get("refused_reason")
    duration = sum(int(r.get("duration_ms") or 0) for r in clean) or None
    profile = request.get("profile")
    failed = [r["target"] for r in clean if not r.get("pass")]

    if refused:
        detail = f"probe request {rid} was refused by the host runner: {refused}"
    elif overall is True:
        detail = (f"acceptance probes passed for {profile or 'a retired request'}: "
                  f"{', '.join(r['target'] for r in clean) or '(no targets)'}")
    elif overall is False:
        detail = (f"acceptance probes FAILED for {profile or 'a retired request'}: "
                  f"{', '.join(failed) or '(no target named)'}")
    else:
        detail = f"probe request {rid} returned no verdict"

    return {
        "category": "probe",
        "action": "post_toggle_probe",
        "target": profile or rid,
        "detail": detail,
        "outcome": "success" if overall is True else "failure",
        "duration_ms": duration,
        "risk": None if overall is True else "medium",
        # The SAME structured field the profile.enable entry carries, so the
        # two entries about one toggle read as one story rather than two
        # shapes (audit_log.py documents it for BSB-16).
        "post_toggle_probe": {
            "request_id": rid,
            "queued_at": request.get("queued_at"),
            "ran_at": result.get("ran_at"),
            "ran_by": result.get("ran_by"),
            "overall_pass": overall,
            "refused_reason": refused,
            "targets": [{"target": r["target"], "pass": bool(r.get("pass")),
                         "duration_ms": r.get("duration_ms")} for r in clean],
        },
    }


def record_pending_verdicts(audit_logger, *, stack_root: Path,
                            now: Optional[float] = None) -> List[str]:
    """Put every not-yet-recorded host verdict into the trail. Returns the ids.

    Never raises: this runs on a background tick inside the Portal, and a
    surprising file in the queue must not take the thread down — the next tick
    would then never come and the trail would go quiet for good.
    """
    recorded: List[str] = []
    try:
        entries = unrecorded_results(Path(stack_root))
    except Exception:
        return recorded
    for entry in entries:
        try:
            audit_logger.log("probe.result", **verdict_audit_fields(entry))
        except Exception:
            continue        # unmarked: try again next tick
        if mark_result_recorded(entry["id"], stack_root=Path(stack_root), now=now):
            recorded.append(entry["id"])
    return recorded


class ProbeVerdictRecorder:
    """The background tick that runs `record_pending_verdicts`.

    Same shape as `services/resource_monitor.py`'s poll loop, on purpose: a
    daemon thread started at app init. The alternative — reconciling when a
    page is rendered — makes the audit trail of a box nobody opens depend on
    somebody opening it, which is not an audit trail.
    """

    def __init__(self, audit_logger, stack_root):
        self._audit_logger = audit_logger
        self._stack_root = Path(stack_root)
        self._running = False
        self._thread = None

    def start(self, interval: float = 60.0):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, args=(interval,), daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self, interval: float):
        while self._running:
            try:
                record_pending_verdicts(
                    self._audit_logger, stack_root=self._stack_root)
            except Exception:
                pass
            time.sleep(interval)


def _runner_path(stack_root: Path) -> Path:
    # #471: razzfazz-test.sh moved to legacy/ in the #119 reorg — resolve the
    # real runner (the rzfz dispatcher maps `rzfz test` to cli/test.sh; same
    # CLI surface, so the probe arguments are unchanged).
    return Path(stack_root) / "cli" / "test.sh"


def _writable_or_creatable(path: Path) -> bool:
    """True when `path` can be written, or created by `mkdir -p`: walk up to
    the nearest existing ancestor and check that one for write access."""
    p = Path(path)
    while True:
        if p.exists():
            return p.is_dir() and os.access(str(p), os.W_OK)
        parent = p.parent
        if parent == p:
            return False
        p = parent


def _host_hint(targets: List[str]) -> str:
    first = targets[0] if targets else "<module>"
    return f"run `rzfz test {first} --acceptance` on the host instead"


def _probe_infra_problem(targets: List[str], stack_root: Path) -> Optional[str]:
    """What cli/test.sh will trip over before it runs a single probe.

    The Portal container mounts the repo :ro (BSB-03), so on a production
    box the runner's own bookkeeping — `mkdir tests/results/<run>` and, on
    first run, the venv bootstrap — fails at once. Both are host concerns,
    not probe verdicts.
    """
    tests_dir = Path(stack_root) / "tests"
    results_root = Path(os.environ.get("RAZZFAZZ_TEST_RESULTS_ROOT")
                        or (tests_dir / "results"))
    if not _writable_or_creatable(results_root):
        return (f"test results dir {results_root} is not writable from the "
                f"config-portal container (the repo is mounted read-only "
                f"here) — {_host_hint(targets)}")
    venv_dir = tests_dir / ".venv"
    if venv_dir.exists():
        # Path.exists() follows symlinks — a host venv whose bin/python
        # points at the HOST interpreter path is a dangling link in here.
        venv_py = venv_dir / "bin" / "python"
        if not venv_py.exists():
            return (f"tests/.venv interpreter {venv_py} does not resolve "
                    f"inside the config-portal container (host-side venv) "
                    f"— {_host_hint(targets)}")
    elif not _writable_or_creatable(venv_dir):
        return (f"tests/.venv is missing and {tests_dir} is not writable "
                f"from the config-portal container, so the runner cannot "
                f"bootstrap it — {_host_hint(targets)}")
    return None


def probe_skip_reason(targets: Iterable[str], *, stack_root: Path) -> Optional[str]:
    """Why `run_probes` would SKIP for `targets` — or ``None`` if it will run.

    Exposed so the apply flow can decide whether the readiness wait is
    worth the operator's time: waiting 90 s in front of a SKIP is not.
    """
    targets = list(targets)
    if not targets:
        return "no acceptance probes found for this profile's containers"
    runner = _runner_path(stack_root)
    if not runner.is_file():
        # The config-portal container in production may not have the test
        # infra mounted in (cli/test.sh + tests/.venv aren't part of
        # the runtime image). Skip cleanly with a clear reason rather than
        # crashing the toggle-action thread.
        return (f"cli/test.sh not found at {runner} — "
                f"the post-toggle probe needs the test infra mounted into the "
                f"config-portal container (see BSB-16-DEC-01)")
    return _probe_infra_problem(targets, Path(stack_root))


def run_probes(
    targets: Iterable[str],
    *,
    stack_root: Path,
    action=None,
) -> dict:
    """Run ``cli/test.sh <target> --acceptance --no-coverage`` for each
    target in sequence, capture the result, and return a structured summary.

    Returns
    -------
    dict with keys::

        {
            'overall_pass': True | False | None,   # None == skipped
            'targets': [
                {'target': str, 'pass': bool, 'duration_ms': int,
                 'stdout_tail': str},
                ...
            ],
            'skipped_reason': Optional[str],
        }

    The ``action`` argument, if given, is the ``ApplyAction`` whose
    ``add_line`` we stream progress into. The contract: the runner must
    NEVER raise — every error path returns a structured summary so the
    caller can log it deterministically.
    """
    targets = list(targets)
    runner = _runner_path(stack_root)

    skip = probe_skip_reason(targets, stack_root=stack_root)
    if skip is not None:
        if action is not None:
            action.add_line(f"  [post-toggle probe] skipped: {skip}")
        return {
            "overall_pass": None,
            "targets": [],
            "skipped_reason": skip,
        }

    if action is not None:
        action.add_line(
            f"  [post-toggle probe] running acceptance probes for: "
            f"{', '.join(targets)}"
        )

    target_results: List[dict] = []
    overall = True
    for target in targets:
        cmd = [
            str(runner), target,
            "--acceptance",
            "--no-coverage",
        ]
        start = time.monotonic()
        try:
            # stderr merged: cli/test.sh reports its own failures (venv,
            # results dir, unknown module) on stderr — dropping it is how
            # the 4 ms FAIL came without a reason (#1190).
            result = subprocess.run(
                cmd,
                cwd=str(stack_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
            )
            stdout = result.stdout or ""
            rc = result.returncode
        except subprocess.TimeoutExpired:
            stdout = f"timed out after {PROBE_TIMEOUT_SECONDS}s"
            rc = 124  # conventional timeout exit
        except Exception as e:  # never crash the apply action
            stdout = f"probe runner crashed: {e!r}"
            rc = 125
        duration_ms = int((time.monotonic() - start) * 1000)
        passed = (rc == 0)
        if not passed:
            overall = False
        # Last lines for the audit trail / the action stream. The container
        # cannot write a results file (repo :ro), so this tail IS the record.
        tail_lines = stdout.splitlines()[-PROBE_RESULT_TAIL_LINES:]
        tail = "\n".join(tail_lines)
        target_results.append({
            "target": target,
            "pass": passed,
            "duration_ms": duration_ms,
            "stdout_tail": tail,
        })
        if action is not None:
            verdict = "PASS" if passed else "FAIL"
            action.add_line(
                f"    [post-toggle probe] {target}: {verdict} "
                f"({duration_ms} ms, exit {rc})"
            )
            if not passed and tail_lines:
                shown = tail_lines[-PROBE_FAIL_TAIL_LINES:]
                action.add_line(
                    f"      tail: last {len(shown)} runner line(s)")
                for ln in shown:
                    action.add_line(f"        | {ln}")

    return {
        "overall_pass": overall,
        "targets": target_results,
        "skipped_reason": None,
    }


def summarize_for_audit(probe_result: dict) -> dict:
    """Strip the tail strings from the per-target results before audit-logging.

    Audit entries are JSON-lines and read by humans + log-aggregation
    tools; we keep the structure but drop the noisy stdout tails — only the
    runner's last line survives (bounded), which is where pytest and
    cli/test.sh put their verdict. The #1190 readiness summary rides along
    when the wait ran.
    """
    out = {
        "overall_pass": probe_result.get("overall_pass"),
        "skipped_reason": probe_result.get("skipped_reason"),
        # #1223 (E4): a queued request has no verdict yet — the audit entry
        # says WHICH request the host still has to run, so "no verdict" can
        # be told apart from "was never asked for".
        "queued": bool(probe_result.get("queued")),
        "request_id": probe_result.get("request_id"),
        "targets": [
            {"target": t, "pass": None, "duration_ms": None, "last_line": ""}
            if isinstance(t, str) else
            {
                "target": t.get("target"),
                "pass": t.get("pass"),
                "duration_ms": t.get("duration_ms"),
                "last_line": ((t.get("stdout_tail") or "").splitlines() or [""])[-1][:200],
            }
            for t in probe_result.get("targets") or []
        ],
    }
    readiness = probe_result.get("readiness")
    if isinstance(readiness, dict):
        out["readiness"] = {
            "waited_ms": readiness.get("waited_ms"),
            "polls": readiness.get("polls"),
            "timed_out": readiness.get("timed_out"),
            "ready": list(readiness.get("ready") or []),
            "not_ready": list(readiness.get("not_ready") or []),
            "unknown_reason": readiness.get("unknown_reason"),
        }
    return out
