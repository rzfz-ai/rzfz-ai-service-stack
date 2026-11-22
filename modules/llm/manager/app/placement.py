# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Explainable and pinnable placement (#2980).

When several workers fit a deployment, the manager picks one by a rule
(``api/inventory.py::_rank_candidates``): a GPU worker before a CPU one, then a
worker that does not already host this deployment, then the most free memory,
then the name. Nothing said so: on a fleet with a Mac and a GB10 the Mac won
(it reports more free memory) and the console showed only the result.

This module holds the two halves of the fix that do not need the database:

* **The pin.** A deployment may name a worker in its ``worker_selector``
  column (no migration: the column has always been a free JSONB object whose
  only read key was ``hardware``)::

      {"pin": {"worker_id": "<uuid>", "mode": "pin" | "prefer"}}

  ``pin`` places every replica on that worker or nowhere, and says why not.
  ``prefer`` places a replica there when it can take it, and otherwise falls
  back to the rule, recording why the preferred worker could not.
  Start, resume, scale-up, the reschedule after a lost worker and the
  re-placement after an undrain all go through ``_place_within_budget``, which
  is where the pin is honoured, so they cannot disagree.

* **The record.** Every placement call records, per replica, the candidates
  with their free memory, the rule, the chosen worker, and for every other
  candidate why it was not chosen (ranked lower by which key, or rejected and
  why). The last record is kept on the deployment under
  ``worker_selector["last_placement"]`` and served by ``GET /api/deployments``
  as ``placement.last``. It is an observation, not a selector: no placement
  code reads it.

The default rule is unchanged. Whether a CUDA box should win over a Mac for
models both can serve is a throughput question that has to be measured on a
box first: the same model and quantisation on each worker, prefill and decode
tokens/s at one and at several parallel requests, at a short and a long
context. A rule that prefers one class is justified only if it wins on the
loads the fleet actually serves; until then the pin is the operator's tool.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

PIN_KEY = "pin"
RECORD_KEY = "last_placement"
MODES = ("pin", "prefer")

#: The ranking rule, in the words the console shows. Must describe
#: ``inventory._rank_key`` exactly; the tests pin the two together.
RULE = ("GPU before CPU, then a worker that does not already host this "
        "deployment, then the most free memory, then the name")

#: How a pin refusal ends; also how a stored ``last_error`` is recognised as
#: one, so the next successful placement clears it.
PIN_REFUSAL_TAIL = ("Not placed elsewhere; free capacity there, or change or "
                    "clear the pin")


def is_pin_refusal(text) -> bool:
    return isinstance(text, str) and PIN_REFUSAL_TAIL in text


#: Bound on the candidates one decision keeps, so a large fleet does not grow
#: the row without limit.
_MAX_CANDIDATES = 16
#: Bound on the decisions one record keeps (one per replica placed).
_MAX_DECISIONS = 8


def _selector(dep) -> dict:
    sel = getattr(dep, "worker_selector", None)
    return sel if isinstance(sel, dict) else {}


def read_pin(dep) -> Optional[dict]:
    """The deployment's pin as ``{"worker_id", "mode"}``, or None.

    A malformed entry (hand-edited row, unknown mode) reads as no pin rather
    than failing every placement of the deployment; ``GET /api/deployments``
    shows what is stored, so it stays visible."""
    raw = _selector(dep).get(PIN_KEY)
    if not isinstance(raw, dict):
        return None
    wid = raw.get("worker_id")
    mode = raw.get("mode") or "pin"
    if not isinstance(wid, str) or not wid.strip() or mode not in MODES:
        return None
    return {"worker_id": wid.strip(), "mode": mode}


def write_pin(dep, worker_id: Optional[str], mode: str = "pin") -> None:
    """Set (or, with ``worker_id=None``, clear) the pin.

    Assigns a NEW dict: an in-place change of a JSONB value is not seen by the
    ORM and would never be written."""
    sel = dict(_selector(dep))
    if worker_id is None:
        sel.pop(PIN_KEY, None)
    else:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        sel[PIN_KEY] = {"worker_id": str(worker_id), "mode": mode}
    dep.worker_selector = sel


def read_record(dep) -> Optional[dict]:
    rec = _selector(dep).get(RECORD_KEY)
    return rec if isinstance(rec, dict) else None


def deciding_key(winner_key, loser_key) -> str:
    """Which key of the rule put ``loser`` below ``winner``: ``gpu``,
    ``spread``, ``free`` or ``name``.

    Keys are ``inventory._rank_key`` tuples:
    ``(is_cpu, already_hosts, -free_gb, name)``."""
    if loser_key[0] and not winner_key[0]:
        return "gpu"
    if loser_key[1] and not winner_key[1]:
        return "spread"
    if loser_key[2] != winner_key[2]:
        return "free"
    return "name"


#: The deciding key, as the one-line summary says it.
_DECIDED_BY = {"gpu": "prefers a GPU worker",
               "spread": "prefers a worker not already hosting it",
               "free": "prefers most free memory",
               "name": "equal otherwise, first by name"}


def why_ranked_lower(winner, winner_key, loser_key) -> str:
    """Why ``loser`` ranked below ``winner``, in words."""
    key = deciding_key(winner_key, loser_key)
    if key == "gpu":
        return f"a CPU worker, and {winner} is a GPU worker"
    if key == "spread":
        return "already hosts a replica of this deployment"
    if key == "free":
        return (f"less free memory ({-loser_key[2]:.1f} GB, {winner} has "
                f"{-winner_key[2]:.1f} GB)")
    return f"equal on every key; {winner} sorts first by name"


class Decision:
    """Why ONE replica went where it went, or why it went nowhere."""

    def __init__(self, replica: int) -> None:
        self.replica = replica
        self.explicit: Optional[str] = None     # worker id the request named
        self.pin: Optional[dict] = None
        self.ranked: list = []                  # [(worker, key)] best first
        self.rejected: dict = {}                # worker id -> (name, reason)
        self.excluded: list = []                # [(name, reason)] never candidates
        self.chosen = None
        self.refusal: Optional[str] = None

    # -- recording -------------------------------------------------------
    def set_pin(self, pin: dict, worker_name: Optional[str]) -> None:
        self.pin = {**pin, "worker": worker_name, "outcome": None, "reason": None}

    def pin_outcome(self, outcome: str, reason: Optional[str] = None) -> None:
        if self.pin is not None:
            self.pin["outcome"] = outcome
            self.pin["reason"] = reason

    def reject(self, worker, reason: str) -> None:
        self.rejected[str(worker.id)] = (worker.name, reason)

    # -- reading ---------------------------------------------------------
    def _chosen_key(self):
        if self.chosen is None:
            return None
        for w, key in self.ranked:
            if str(w.id) == str(self.chosen.id):
                return key
        return None

    def _decided_by(self) -> str:
        """The key that separated the choice from the runner-up."""
        ckey = self._chosen_key()
        others = [k for w, k in self.ranked if str(w.id) != str(self.chosen.id)]
        if ckey is None or not others:
            return "the only worker that could take it"
        return _DECIDED_BY[deciding_key(ckey, others[0])]

    def candidates(self) -> list:
        out = []
        chosen_id = str(self.chosen.id) if self.chosen is not None else None
        ckey = self._chosen_key()
        seen = set()
        for w, key in self.ranked:
            wid = str(w.id)
            seen.add(wid)
            if wid == chosen_id:
                verdict = "chosen"
            elif wid in self.rejected:
                verdict = self.rejected[wid][1]
            elif ckey is not None:
                verdict = "ranked lower: " + why_ranked_lower(self.chosen.name, ckey, key)
            else:
                verdict = "not chosen"
            out.append({"worker_id": wid, "worker": w.name, "free_gb": round(-key[2], 1),
                        "gpu": not key[0], "hosts_replica": bool(key[1]),
                        "verdict": verdict})
        # Rejected workers drop out of the ranking on the next pick; keep them.
        for wid, (name, reason) in self.rejected.items():
            if wid not in seen:
                out.append({"worker_id": wid, "worker": name, "verdict": reason})
        return out[:_MAX_CANDIDATES]

    def summary(self) -> str:
        parts = []
        if self.pin is not None:
            mode = "pinned to" if self.pin["mode"] == "pin" else "prefers"
            who = self.pin.get("worker") or self.pin["worker_id"]
            outcome = self.pin.get("outcome")
            if outcome == "honoured":
                return f"{mode} {who}: placed there"
            if outcome == "refused":
                return (f"{mode} {who}, which cannot take it: {self.pin.get('reason')}. "
                        + PIN_REFUSAL_TAIL)
            if outcome in ("fell_back", "skipped"):
                parts.append(f"{mode} {who}, but {self.pin.get('reason')}; fell back to the rule")
        if self.explicit is not None and self.chosen is not None:
            return f"placed on {self.chosen.name}, the worker the request named"
        if self.explicit is not None:
            return f"the worker the request named cannot take it: {self.refusal}"
        # The preferred worker's refusal is already said above; not twice.
        skip = self.pin["worker_id"] if self.pin is not None and parts else None
        cands = [c for c in self.candidates()
                 if not (c["worker_id"] == skip and "free_gb" not in c)]
        listing = ", ".join(
            f"{c['worker']}: {c['free_gb']:.1f} GB free" if "free_gb" in c
            else f"{c['worker']}: {c['verdict']}"
            for c in cands)
        if self.chosen is not None:
            parts.append(f"{listing} · {self._decided_by()} → {self.chosen.name}"
                         if listing else f"placed on {self.chosen.name}")
        else:
            parts.append(f"no worker can take it ({self.refusal or 'none matched'})"
                         + (f": {listing}" if listing else ""))
        return " · ".join(parts)

    def as_dict(self) -> dict:
        return {
            "replica": self.replica,
            "outcome": "placed" if self.chosen is not None else "refused",
            "worker_id": str(self.chosen.id) if self.chosen is not None else None,
            "worker": self.chosen.name if self.chosen is not None else None,
            "explicit_worker_id": self.explicit,
            "pin": self.pin,
            "rule": RULE,
            "candidates": self.candidates(),
            "excluded": [{"worker": n, "reason": r} for n, r in self.excluded][:_MAX_CANDIDATES],
            "refusal": self.refusal,
            "summary": self.summary(),
        }


class PlacementLog:
    """The decisions of one placement request (start, scale-up, …)."""

    def __init__(self, trigger: str) -> None:
        self.trigger = trigger
        self.decisions: list[Decision] = []

    def new(self) -> Decision:
        d = Decision(len(self.decisions) + 1)
        self.decisions.append(d)
        return d

    def pin_refusal(self) -> Optional[str]:
        for d in self.decisions:
            if d.pin is not None and d.pin.get("outcome") == "refused":
                return d.summary()
        return None

    def summary(self) -> Optional[str]:
        if not self.decisions:
            return None
        texts = []
        for d in self.decisions:
            s = d.summary()
            if s not in texts:
                texts.append(s)
        return " | ".join(texts)

    def record(self, now: datetime) -> dict:
        return {"at": now.isoformat(), "trigger": self.trigger,
                "summary": self.summary(),
                "decisions": [d.as_dict() for d in self.decisions[:_MAX_DECISIONS]]}

    def save(self, dep, now: datetime) -> None:
        """Keep this request's record on the deployment.

        Skipped when nothing was decided, and when the record equals the one
        stored apart from its time: the reconciler retries a deployment that
        does not fit every pass, and an identical record is not news."""
        if not self.decisions:
            return
        rec = self.record(now)
        old = read_record(dep)
        if old is not None and {k: v for k, v in old.items() if k != "at"} == \
                {k: v for k, v in rec.items() if k != "at"}:
            return
        sel = dict(_selector(dep))
        sel[RECORD_KEY] = rec
        dep.worker_selector = sel


def view(dep, worker_names: dict) -> dict:
    """What ``GET /api/deployments`` serves as ``placement``."""
    pin = read_pin(dep)
    if pin is not None:
        pin = {**pin, "worker": worker_names.get(pin["worker_id"]),
               "worker_exists": pin["worker_id"] in worker_names}
    return {"pin": pin, "last": read_record(dep), "rule": RULE}
