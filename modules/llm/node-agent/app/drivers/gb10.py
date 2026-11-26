# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""GB10 (Grace Blackwell / DGX-Spark class) engine driver — #1329, #1517, #1518.

HP ZGX Nano G1n, Dell Pro Max GB10, Lenovo ThinkStation PGX, ASUS Ascent GX10,
NVIDIA DGX Spark FE: NVIDIA ships the GB10 module, the OEMs build the case. All
of them are the same thing to us — an **inference worker**, never a master.

What this driver is
-------------------
Only the IMAGE differs from a plain CUDA box. The launch contract — nvidia
runtime, the llama.cpp tuning, the driver capabilities — is identical, so the
driver here is a thin subclass that changes nothing but which image it resolves.
Everything the CUDA path learned the hard way stays in one place.

The image differs because GB10 is **sm_121**, while the other runner is compiled
for sm_120 (RTX PRO 6000) against CUDA 12.8 — and CUDA 12.8's nvcc does not know
sm_121 at all. Measured on .191: the sm_120 binary dies with "no kernel image is
available for execution on the device"; the CUDA 13.0.2 / ``121a-real`` build
loads the model and serves. That measurement is why two CUDA tags exist rather
than one fat binary.

NOT a hardware class an operator types
--------------------------------------
This was a worker TYPE in #1329: an operator had to know that a GB10 enrols as
``cuda-gb10``, and getting it wrong was not a warning but a dead engine. #1517
retired that. Every NVIDIA dialect folds onto ``cuda``, and ``select_driver``
asks the GPU for its compute capability (``nvidia-smi --query-gpu=compute_cap``
→ 12.1 here, 12.0 on the RTX PRO 6000) — the same number the binary was compiled
for — to pick this subclass.

``hardware = "cuda-gb10"`` therefore survives for exactly two reasons, both
about compatibility rather than selection:

* it is how this driver RESOLVES its image (``ENV_VARS["cuda-gb10"]``,
  ``_DEFAULTS["cuda-gb10"]``), so a box that pinned the GB10-specific override
  keeps its pin — and, since #1517 rev-B, a box that pinned the ENROLLED class's
  override (``RAZZFAZZ_ENGINE_IMAGE_CUDA_LLAMACPP``) keeps that one too, through
  ``_PIN_FALLBACK_ENGINE_VARS``. Without that chain, being RECOGNISED as a GB10
  would silently disarm the operator's pin;
* the string still lands in the manager's ``nvidia`` family, because
  ``_hardware_family`` is substring-based — a bare ``gb10`` would form its own
  family and every ``nvidia`` placement selector would pass the box by. It is
  also what ``hardware_label`` writes into the ``rzfz.hardware`` container
  label, now including on a box that enrolled as ``nvidia``.

#1518 (E5): the vLLM sibling of this driver is gone. GGUF/llama.cpp is the one
model architecture the fleet serves, so the unified-memory
``--gpu-memory-utilization`` derivation (#1456) — a vLLM-only flag — went with
it. llama.cpp allocates per the model and the offload layers, not as a fraction
of a device-memory figure the GB10 does not even report.

The runner images themselves are built and published by the stack since #1516;
the tag names the toolkit and architecture, so two incompatible binaries can no
longer share one.
"""
from __future__ import annotations

from .cuda_llamacpp import CudaLlamaCppDriver


class Gb10LlamaCppDriver(CudaLlamaCppDriver):
    """llama.cpp (llama-server) on GB10, GGUF with CUDA offload.

    Same spec as :class:`CudaLlamaCppDriver` — including the explicit
    ``NVIDIA_DRIVER_CAPABILITIES`` the custom runner needs — with the
    GB10-specific image."""

    hardware = "cuda-gb10"
    hardware_label = "cuda-gb10"


__all__ = ["Gb10LlamaCppDriver"]
