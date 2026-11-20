# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Scheduler placement + artifact resolution (Phase-2, pure logic).

Spec §5.3. A deployment references a LOGICAL model + a worker_selector; the
scheduler resolves candidate workers, then PER INSTANCE resolves the correct
artifact for that worker's engine/hardware (AMD and CUDA→gguf/llamacpp since
#1518, Mac→ollama/apple-silicon), checks VRAM fit, and places ``replicas``.

Validation (hard rule): if a selected worker has NO compatible artifact, the
placement is REJECTED — there is no silent CPU fallback.

Pure functions over lightweight dataclasses so this unit-tests without a DB
or a box; the manager builds WorkerCandidate/ArtifactSpec from ORM rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WorkerCandidate:
    id: str
    labels: dict = field(default_factory=dict)
    vram_free_gb: float = 0.0
    status: str = "ready"


def is_schedulable(worker: WorkerCandidate) -> bool:
    """Whether a worker may receive placements at all (#419 P0 Task 4).

    A worker awaiting admin approval is `pending` and must never be a placement
    target. Today the two live placement filters select `status == "ready"`
    directly (api/inventory.py, api/hf.py), so this module is not yet the
    enforcement point — it has no production caller. It is stated here so that
    when placement DOES move behind the scheduler, admission is part of
    candidate selection by construction rather than something to remember.

    Defaults to "ready" so a candidate built without a status behaves exactly as
    before, and nobody's fixture silently starts excluding workers.
    """
    return worker.status != "pending"


@dataclass
class ArtifactSpec:
    id: str
    hardware_class: str  # 'amd-gfx1151'|'cuda'|'apple-silicon'|'cpu'
    engine: str          # 'llamacpp'|'ollama' ('vllm' retired in #1518)
    format: str          # 'gguf'|'ollama' ('safetensors' retired with vLLM)
    size_bytes: int = 0


@dataclass
class Placement:
    worker: WorkerCandidate
    artifact: ArtifactSpec


class PlacementError(Exception):
    """Raised when a deployment cannot be placed (no compatible artifact or
    insufficient fitting capacity). Never falls back to CPU silently."""


def select_candidates(workers, selector: dict):
    """Workers whose labels satisfy every key/value in ``selector``.
    An empty selector matches all workers."""
    selector = selector or {}
    out = []
    for w in workers:
        if not is_schedulable(w):
            continue          # #419: unapproved workers are not candidates
        if all(w.labels.get(k) == v for k, v in selector.items()):
            out.append(w)
    return out


#: The label a worker's hardware class is actually stored under.
#:
#: #334: this module read `labels["gpu"]`, but nothing has ever written that
#: key — `api/workers.py::register` writes `labels["hardware"]` (amd/cuda/cpu/
#: mps). Since the documented rule here is "no compatible artifact → REJECT, no
#: silent CPU fallback", wiring the scheduler as written would have rejected
#: EVERY placement on every real worker. The module is not yet called by
#: anything (#263 is the epic that wires it), so this never fired in production
#: — it was a landmine for whoever wired it, not a live bug.
HARDWARE_LABEL = "hardware"
#: Tolerated legacy alias so a caller that still builds candidates with "gpu"
#: keeps working; new code should use HARDWARE_LABEL.
_LEGACY_HARDWARE_LABEL = "gpu"


def worker_hardware_class(worker: WorkerCandidate) -> str | None:
    """The worker's hardware class from its labels, or None.

    Reads the key registration actually writes, falling back to the legacy
    name so an existing caller is not broken by the correction.
    """
    return (worker.labels.get(HARDWARE_LABEL)
            or worker.labels.get(_LEGACY_HARDWARE_LABEL))


def artifact_for_worker(worker: WorkerCandidate, artifacts):
    """The artifact whose hardware_class matches the worker's hardware class, or
    None (→ the caller must reject; no CPU fallback)."""
    hw = worker_hardware_class(worker)
    if hw is None:
        return None
    for a in artifacts:
        if a.hardware_class == hw:
            return a
    return None


def artifact_vram_gb(artifact: ArtifactSpec) -> float:
    return (artifact.size_bytes or 0) / (1024 ** 3)


def fits_vram(worker: WorkerCandidate, artifact: ArtifactSpec) -> bool:
    return worker.vram_free_gb >= artifact_vram_gb(artifact)


def plan_placement(*, selector: dict, replicas: int, workers, artifacts) -> list[Placement]:
    """Resolve candidates → per-worker artifact → VRAM fit → place `replicas`.

    Raises PlacementError if a candidate has no compatible artifact, or if
    fewer than `replicas` workers can fit a resolved artifact.
    """
    candidates = select_candidates(workers, selector)
    if not candidates:
        raise PlacementError(f"no workers match selector {selector!r}")

    placements: list[Placement] = []
    for worker in candidates:
        if len(placements) >= replicas:
            break
        artifact = artifact_for_worker(worker, artifacts)
        if artifact is None:
            # Hard rule: no compatible artifact → reject (no silent CPU fallback).
            raise PlacementError(
                f"worker {worker.id!r} (hardware={worker_hardware_class(worker)!r}) has no "
                f"compatible artifact among {[a.id for a in artifacts]!r} — refusing "
                f"placement (no CPU fallback)"
            )
        if not fits_vram(worker, artifact):
            continue  # not enough VRAM on this worker; try the next candidate
        placements.append(Placement(worker=worker, artifact=artifact))

    if len(placements) < replicas:
        raise PlacementError(
            f"insufficient fitting capacity: needed {replicas}, placed "
            f"{len(placements)} (candidates={[w.id for w in candidates]!r})"
        )
    return placements
