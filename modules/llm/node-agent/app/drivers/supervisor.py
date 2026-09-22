# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Engine supervision with bounded backoff + circuit-break (#254 P2-A4).

A crashed engine is restarted with EXPONENTIAL BOUNDED BACKOFF, up to
``max_failures`` restarts; the NEXT consecutive failure CIRCUIT-BREAKS the
instance — so a genuinely broken model-load can't hot-loop and storm the node
(the lesson from failed post-install model deploys). A running-but-never-healthy
engine that blows its readiness grace counts as a failed load and goes through
the same bounded path.

#344 — the real restart bound, since "never restarted again" used to be stated
here and stopped being true when #316 landed: a circuit-broken instance is
half-opened after ``recover_after`` and may be actively restarted up to
``max_recover_attempts`` times, and each recovery attempt RESETS
``consecutive_failures``, so it re-earns the full ladder. The worst case is
therefore ``max_failures + max_recover_attempts * (1 + max_failures)`` engine
starts (23 at the defaults), not zero — still bounded and cooldown-spaced, but
an order of magnitude more than "never". Passive heal (an engine that came back
on its own) costs no restart at all.

Safety: the supervisor acts ONLY on instances explicitly handed to it via
``supervise`` — it never lists or touches other containers, so it can never
cascade-kill or "autoheal" a gpustack-class engine it doesn't own.

Deterministic + testable: the docker client, the wall clock (``now``) and the
health probe (``http_get``) are all injected; ``tick(now)`` is a pure step with
no real timers or daemon.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .base import LaunchSpec, probe_health, start_engine, stop_engine

logger = logging.getLogger("node_agent.supervisor")


#: How much of the engine's output to carry. Enough for a `missing tensor
#: blk.64.ssm_conv1d.weight` or an OOM to be readable in the console, small
#: enough that it can ride along in every registration payload without turning
#: the report into a log shipper.
ERROR_TAIL_LINES = 40
ERROR_TAIL_MAX_CHARS = 4000


def _tail_logs(name: str, docker) -> str:
    """The last lines the engine wrote, or "" if they cannot be read.

    Never raises: this runs inside the failure path, and a node that crashed
    while trying to explain a crash would replace a diagnosable problem with an
    undiagnosable one.
    """
    try:
        raw = docker.containers.get(name).logs(tail=ERROR_TAIL_LINES)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = str(raw).strip()
    if len(text) > ERROR_TAIL_MAX_CHARS:
        # Keep the END: the reason a process died is its last output, not its
        # banner.
        text = "…\n" + text[-ERROR_TAIL_MAX_CHARS:]
    return text


@dataclass
class _Inst:
    spec: LaunchSpec
    state: str = "loading"          # loading|backing_off|ready|failed
    consecutive_failures: int = 0
    backoff_until: float = 0.0
    started_at: float = 0.0
    no_restart: bool = False        # #293 re-adopted engine: health-monitor only
    failed_at: float = 0.0          # #316 when the circuit last opened (for recovery cooldown)
    last_error: str = ""            # #708 tail of the engine's own output at its last failure
    recover_attempts: int = 0       # #316 active recovery restarts issued so far


class EngineSupervisor:
    def __init__(
        self,
        docker,
        *,
        base_backoff: float = 5.0,
        max_backoff: float = 300.0,
        max_failures: int = 5,
        readiness_grace: float = 120.0,
        recover_after: float = 180.0,
        max_recover_attempts: int = 3,
    ):
        self._docker = docker
        self._insts: dict[str, _Inst] = {}
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.max_failures = max_failures
        self.readiness_grace = readiness_grace
        # #316 self-heal: a circuit that only opened because the HOST was
        # thrashing (unified-memory OOM, probe timeouts) shouldn't stay open
        # forever. After ``recover_after`` seconds we half-open it: passively
        # promote an engine that came back serving on its own (zero restart), or
        # — for engines we own — issue up to ``max_recover_attempts`` bounded
        # recovery restarts. Never a storm: passive heal is free, active heal is
        # capped and cooldown-spaced.
        self.recover_after = recover_after
        self.max_recover_attempts = max_recover_attempts

    # --- registry ----------------------------------------------------------
    def supervise(self, spec: LaunchSpec, *, now: float = 0.0) -> None:
        """Start supervising an already-launched instance."""
        self._insts[spec.name] = _Inst(spec=spec, state="loading", started_at=now)

    def readopt(self, spec: LaunchSpec, *, now: float = 0.0) -> None:
        """#293: health-monitor an already-running engine re-discovered on node
        startup. We don't have its full launch command, so on death it goes
        straight to 'failed' (no re-create) — the node restarted; the operator
        re-deploys. Keeps the engine VISIBLE + truthfully health-tracked."""
        self._insts[spec.name] = _Inst(spec=spec, state="loading", started_at=now, no_restart=True)

    def forget(self, name: str) -> None:
        """Stop supervising (e.g. on operator unload). Does NOT stop the
        container — the unload path handles that."""
        self._insts.pop(name, None)

    def supervised(self) -> list[str]:
        return sorted(self._insts)

    def last_errors(self) -> dict[str, str]:
        """#708 — {instance: last engine output} for instances that have one.

        Separate from ``states()`` on purpose: that mapping is consumed in
        several places that only want the phase, and widening its value type
        would touch all of them.
        """
        return {n: st.last_error for n, st in self._insts.items() if st.last_error}

    def states(self) -> dict[str, str]:
        """#286: the REAL per-instance state (loading|backing_off|ready|failed),
        so the report cycle can tell the manager the truth instead of assuming
        ready. Keyed by instance_id (== spec.name)."""
        return {name: st.state for name, st in self._insts.items()}

    # --- health/liveness ---------------------------------------------------
    def _backoff(self, failures: int) -> float:
        return min(self.max_backoff, self.base_backoff * (2 ** max(0, failures - 1)))

    def _is_down(self, name: str) -> bool:
        try:
            container = self._docker.containers.get(name)
        except Exception:
            return True  # gone == down
        return getattr(container, "status", "") not in ("running", "restarting")

    def _fail_and_maybe_restart(self, name: str, st: _Inst, now: float) -> str:
        """One failure event for ``name``. Respects the backoff window; counts
        the failure; circuit-breaks past ``max_failures``; else restarts."""
        if now < st.backoff_until:
            st.state = "backing_off"
            return "waiting"
        st.consecutive_failures += 1
        # #708 — read the engine's own last words BEFORE cleaning up. This
        # ordering is the whole fix: `stop_engine` stops AND REMOVES the
        # container, so once it has run the logs are gone for good. That is why
        # a circuit-broken instance could reach the console as a bare `failed`
        # with nothing attached — the operator saw a red state and had nothing
        # to grab, which is worse than the crash-loop it replaced.
        st.last_error = _tail_logs(name, self._docker) or st.last_error
        # Clean up the crashed/wedged container regardless of what we do next.
        stop_engine(name, self._docker)
        if st.consecutive_failures > self.max_failures:
            st.state = "failed"
            st.failed_at = now
            # #344: log the ACTUAL failure count. This previously printed
            # ``consecutive_failures - 1`` (the restart count) under a "failed N×"
            # label, so the number never matched max_failures and reading the logs
            # against the threshold was misleading.
            logger.error(
                "engine %s failed %d× consecutively (%d restarts, the max) — "
                "circuit-break; recovery half-opens after %.0fs",
                name, st.consecutive_failures, self.max_failures, self.recover_after,
            )
            return "circuit_break"
        start_engine(st.spec, self._docker)
        st.started_at = now
        st.state = "backing_off"
        st.backoff_until = now + self._backoff(st.consecutive_failures)
        # #344: phrase this as the restart budget being spent, not "failure N/N" —
        # the old wording printed "failure 3/3 — restarted", which reads as though
        # the cap had been reached and the breaker had failed to fire.
        logger.warning(
            "engine %s down — restart %d of %d, next backoff %.0fs",
            name, st.consecutive_failures, self.max_failures,
            st.backoff_until - now,
        )
        return "restarted"

    def _maybe_recover(self, name: str, st: _Inst, now: float, *, http_get) -> str:
        """#316 half-open a circuit-broken instance after ``recover_after``.

        A model that only tripped because the HOST was thrashing (unified-memory
        OOM, probe timeouts under load) should heal itself once the pressure
        passes — WITHOUT reintroducing the restart storm the break prevents:

          * Passive (always, zero-risk): the engine is up + serving again on its
            own (Docker restart policy / operator re-launch) → promote to ready.
            No restart is issued.
          * Active (only instances we own, hard-capped): still down after the
            cooldown → issue ONE recovery restart, up to ``max_recover_attempts``
            times total. Then it stays failed (passive heal still watches).
        """
        if now - st.failed_at < self.recover_after:
            return "failed"  # still cooling down — don't flap
        # passive: came back and serves → heal, no restart
        if not self._is_down(name) and probe_health(st.spec.health_url, http_get=http_get):
            st.state = "ready"
            st.consecutive_failures = 0
            st.backoff_until = 0.0
            st.recover_attempts = 0
            logger.info("engine %s self-healed (up + serving after circuit-break)", name)
            return "recovered"
        # active: we own it, it's down, attempts remain → one bounded restart
        if (not st.no_restart and self._is_down(name)
                and st.recover_attempts < self.max_recover_attempts):
            st.recover_attempts += 1
            st.consecutive_failures = 0
            st.state = "loading"
            st.started_at = now
            st.failed_at = 0.0
            start_engine(st.spec, self._docker)
            logger.info(
                "engine %s circuit half-open — recovery restart %d/%d",
                name, st.recover_attempts, self.max_recover_attempts,
            )
            return "recover_restart"
        # re-adopted+down, up-but-not-serving-yet, or attempts exhausted → re-arm
        # the cooldown and stay failed (still bounded, still no storm).
        st.failed_at = now
        return "failed"

    def tick(self, now: float, *, http_get) -> dict:
        """Advance supervision by one step. Returns {instance: action} for the
        instances it acted on. Actions: ready|loading|restarted|waiting|
        circuit_break|failed|recovered|recover_restart."""
        actions: dict[str, str] = {}
        for name, st in list(self._insts.items()):
            # #343: isolate each instance. A docker error on ONE engine (a vanished
            # container, a name conflict on re-launch) previously escaped the loop and
            # skipped supervision for every instance after it — and because a skipped
            # instance's ``started_at`` keeps ageing, it then tripped readiness_grace
            # and was counted as a failed load caused by someone else's error.
            try:
                actions[name] = self._tick_one(name, st, now, http_get=http_get)
            except Exception:
                actions[name] = "error"
                logger.exception("supervision step failed for %s (other engines unaffected)", name)
        return actions

    def _tick_one(self, name: str, st: _Inst, now: float, *, http_get) -> str:
        """Advance ONE instance. Returns its action; raises only on an unexpected
        docker/probe error, which ``tick`` isolates per instance."""
        if st.state == "failed":
            return self._maybe_recover(name, st, now, http_get=http_get)
        if self._is_down(name):
            if st.no_restart:
                # re-adopted engine we can't re-create → truthfully failed
                st.state = "failed"
                st.failed_at = now
                return "failed"
            return self._fail_and_maybe_restart(name, st, now)
        # container is running — is it actually serving?
        if probe_health(st.spec.health_url, http_get=http_get):
            st.state = "ready"
            st.consecutive_failures = 0
            st.backoff_until = 0.0
            return "ready"
        if now - st.started_at > self.readiness_grace:
            # up but never became ready within grace → failed load
            return self._fail_and_maybe_restart(name, st, now)
        st.state = "loading"
        return "loading"
