# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""CUDA / NVIDIA llama.cpp (llama-server) engine driver.

The SAME GGUF ``llama-server`` the AMD (Vulkan) and CPU drivers launch, but on
an NVIDIA box: full GPU offload (``-ngl 999``), the shared hard-won tuning (host
prompt-cache OFF, the ``--swa-full`` ban), and auto ``--mmproj`` for vision — all
carried by the shared ``llamacpp_command``. This lets the manager serve a GGUF on
an NVIDIA worker directly, with NO GPUStack and NO vLLM.

GPU access is granted the NVIDIA way — the nvidia container runtime
(``runtime=nvidia`` + ``NVIDIA_VISIBLE_DEVICES`` + ``NVIDIA_DRIVER_CAPABILITIES``),
NOT the AMD ``/dev/kfd`` + ``/dev/dri`` passthrough. Unlike the vLLM image (a CUDA
base image that bakes ``NVIDIA_DRIVER_CAPABILITIES`` in), the custom llama.cpp CUDA
runner needs the capability set stated explicitly, or the container starts without
the compute driver libraries mapped in.

The engine image is the locally-built CUDA runner (``llama-cuda-runner``,
overridable via ``RAZZFAZZ_ENGINE_IMAGE_CUDA_LLAMACPP`` at wiring time). Its
entrypoint is ``tini --``, so the launch command must LEAD with the
``llama-server`` binary (same contract as the AMD driver). The container joins the
stack bridge so the manager's router reaches it by name at
http://<instance>:8080/v1.
"""
from __future__ import annotations

import os

from .base import (
    EngineDriver,
    LaunchSpec,
    LoadSpecInput,
    files_label,
    llamacpp_command,
    pick_gguf_weight,
    pick_mmproj,
)
from app.drivers.images import CUDA_LLAMACPP_IMAGE, engine_image_for


# The image this driver launches lives in app.drivers.images (the single
# authority). Kept as a module constant for back-compat/discoverability; the env
# override (RAZZFAZZ_ENGINE_IMAGE_CUDA_LLAMACPP) is resolved per CONSTRUCTION, not
# at import, so setting it after this module loads still takes effect.
DEFAULT_CUDA_LLAMACPP_IMAGE = CUDA_LLAMACPP_IMAGE


class CudaLlamaCppDriver(EngineDriver):
    engine = "llamacpp"
    #: A subclass overrides this to serve another
    #: CUDA-class worker type from the same launch logic (#1329 GB10).
    hardware = "cuda"
    #: Pre-existing value, deliberately unchanged by #1329: this driver has
    #: always labelled "nvidia" where the retired vLLM driver labelled "cuda".
    #: Nothing in
    #: the product reads it.
    hardware_label = "nvidia"

    def __init__(self, image: str | None = None):
        self.image = image or engine_image_for(self.hardware, "llamacpp")

    def build_spec(self, req: LoadSpecInput) -> LaunchSpec:
        weight = pick_gguf_weight(req.files)
        if not weight:
            raise ValueError(
                "CUDA llama.cpp load requires a .gguf weights file in files[]; "
                f"got {req.files!r}"
            )
        mmproj = pick_mmproj(req.files)
        cmd = llamacpp_command(
            f"{req.models_mount}/{weight}",
            ngl=999,   # full offload to the NVIDIA GPU
            mmproj=(f"{req.models_mount}/{mmproj}" if mmproj else None),
            params=req.params,
            task=req.task,
            # runner image entrypoint is `tini --` → lead with the binary.
            # #1654: `or`, NOT a get() default. compose forwards this key as
            # "${RAZZFAZZ_LLAMACPP_BINARY:-}", which SETS it to empty — and a
            # get() default only fires when the key is ABSENT. The empty value
            # won, the argv lost its leading binary, and tini exec'd `--model`:
            # every engine on a recreated worker died with exit 127.
            binary=os.environ.get("RAZZFAZZ_LLAMACPP_BINARY") or "llama-server",
        )
        return LaunchSpec(
            engine=self.engine,
            image=req.runner_image or self.image,
            name=req.instance_id,
            command=cmd,
            # NVIDIA container runtime, NOT the AMD /dev/kfd path. The custom
            # llama.cpp CUDA runner (unlike vllm/vllm-openai) does not bake the
            # capability set into the image, so state it explicitly.
            environment={
                "NVIDIA_VISIBLE_DEVICES": "all",
                "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
            },
            devices=[],          # GPU via the nvidia runtime, not /dev passthrough
            group_add=[],        # no device GIDs needed on the nvidia-runtime path
            volumes={req.models_volume: {"bind": req.models_mount, "mode": "ro"}},
            network=req.network,
            runtime="nvidia",
            security_opt=["no-new-privileges:true"],
            labels={
                "rzfz.role": "llm-engine",
                "rzfz.instance": req.instance_id,
                "rzfz.engine": self.engine,
                "rzfz.hardware": self.hardware_label,
                # #293 re-adoption: recover model + serve mode after a node restart.
                "rzfz.model": req.model,
                "rzfz.task": req.task,
                # NODE-9: weight leaves, for the post-restart in-use guard.
                "rzfz.files": files_label(req.files),
            },
            serve_url=f"http://{req.instance_id}:8080/v1",
            health_url=f"http://{req.instance_id}:8080/health",
        )
