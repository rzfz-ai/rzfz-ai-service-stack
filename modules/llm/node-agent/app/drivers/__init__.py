# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Engine-driver registry (#254 Phase-2).

`select_driver(hardware, engine=…)` returns the driver for a hardware class,
refined by the GPU where the class alone cannot decide.

#1518 (E5): every driver here launches ``llama-server`` on a GGUF weight —
that is the fleet's one model architecture. What differs between them is the
BINARY: Vulkan on AMD, CPU, and two CUDA builds whose GPU architectures are
mutually unusable (sm_120 for the RTX PRO 6000, sm_121a for the GB10). The
compute capability the card reports picks between the last two, so nothing
depends on an operator typing the right hardware name.

The runtime helpers + spec types are re-exported so callers import everything
from ``app.drivers``.
"""
from __future__ import annotations

from .amd import AmdLlamaCppDriver
from .base import (
    EngineDriver,
    LaunchSpec,
    LoadSpecInput,
    engine_logs,
    probe_health,
    start_engine,
    stop_engine,
)
from .base import normalize_hardware
from .cpu import CpuLlamaCppDriver
from .cuda_llamacpp import CudaLlamaCppDriver
from .gb10 import Gb10LlamaCppDriver
from .images import detect_cuda_capability
from .supervisor import EngineSupervisor

# hardware class -> driver class. One entry per class; #1518 left exactly one
# engine, so the deployment's `engine` no longer changes the answer.
_REGISTRY: dict[str, type[EngineDriver]] = {
    # #1517 (E5): no `cuda-gb10` entry — normalize_hardware folds every NVIDIA
    # dialect onto `cuda`, and the GPU's compute capability picks the binary.
    "amd": AmdLlamaCppDriver,
    "cuda": CudaLlamaCppDriver,
    "cpu": CpuLlamaCppDriver,
}

# (hardware, engine) → driver, for the pairs where the deployment's engine picks
# something OTHER than the hardware default above. EMPTY since #1518 removed the
# vLLM path: llama.cpp is the only engine, so every class has exactly one driver
# and `engine` cannot select a second. Kept — rather than deleted along with its
# lookup — because a hard-coded `if hw == "cuda" and eng == …` is what #1329
# showed to be the fragile form: a table cannot forget a class the way a chained
# comparison can, and the next engine we add lands here as one row.
_ENGINE_REGISTRY: dict[tuple[str, str], type[EngineDriver]] = {}


# (driver, compute capability) → the variant that GPU needs. Empty for every
# capability we have nothing special to say about, which is the common case.
_CAPABILITY_REGISTRY: dict[tuple[type[EngineDriver], str], type[EngineDriver]] = {
    (CudaLlamaCppDriver, "12.1"): Gb10LlamaCppDriver,
}


def select_driver(hardware: str, engine: str | None = None, **kwargs) -> EngineDriver:
    """Return an engine driver for ``hardware`` (amd|cuda|cpu),
    refined by the
    deployment's ``engine`` where the hardware supports more than one.

    Rule: a (hardware, engine) pair listed in ``_ENGINE_REGISTRY`` wins; anything
    else falls through to the per-hardware default in ``_REGISTRY``. Since #1518
    that table is empty — every class is llama.cpp-only — so ``engine`` is
    accepted and ignored, including the ``vllm`` a pre-#1518 deployment record
    may still carry.

    Raises ValueError for an unknown hardware class so a mis-provisioned node
    fails loud rather than silently launching nothing. Extra kwargs (e.g.
    ``image=``) pass through to the driver constructor."""
    hw = normalize_hardware(hardware)
    try:
        cls = _REGISTRY[hw]
    except KeyError:
        raise ValueError(
            f"no engine driver for hardware={hardware!r} "
            f"(have: {sorted(_REGISTRY)}; nvidia→cuda)"
        )
    eng = (engine or "").strip().lower()
    cls = _ENGINE_REGISTRY.get((hw, eng), cls)
    # #1517 (E5): the GPU refines what the class cannot. A GB10 is a `cuda` box
    # like any other, but its llama.cpp binary is a different architecture
    # (sm_121a vs sm_120) — a property of the GPU, so the compute capability
    # selects it, not a hardware class an operator has to type correctly.
    # #1517 rev-B: probe the GPU ONLY for a class that has a capability variant.
    # `detect_cuda_capability` shells out to nvidia-smi with an 8 s timeout, and
    # the unconditional call ran that subprocess on every AMD and CPU engine
    # start too — for a result that was then discarded.
    if any(key[0] is cls for key in _CAPABILITY_REGISTRY):
        cls = _CAPABILITY_REGISTRY.get((cls, detect_cuda_capability()), cls)
    return cls(**kwargs)


__all__ = [
    "select_driver",
    "normalize_hardware",
    "EngineDriver",
    "LaunchSpec",
    "LoadSpecInput",
    "AmdLlamaCppDriver",
    "CudaLlamaCppDriver",
    "Gb10LlamaCppDriver",
    "CpuLlamaCppDriver",
    "EngineSupervisor",
    "start_engine",
    "stop_engine",
    "engine_logs",
    "probe_health",
]
