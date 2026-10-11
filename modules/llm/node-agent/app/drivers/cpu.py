# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""CPU llama.cpp engine driver (#254 P2-A3).

Same llama.cpp server as the AMD path but CPU-only: no GPU offload (`-ngl 0`),
no device passthrough, no nvidia runtime. Uses the official ggml-org CPU server
image. Carries the shared llama.cpp tuning + `--swa-full` ban via
``llamacpp_command``. This is the STABLE default for CPU boxes (mirrors the
llm-cpu profile intent).
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
from app.drivers.images import CPU_IMAGE, engine_image


# #331 the image this driver launches lives in app.drivers.images, the single
# authority cross-checked against what cli/init.sh and cli/upgrade.sh actually
# build. Kept as a module constant for back-compat; the env override
# (RAZZFAZZ_ENGINE_IMAGE_CPU) is resolved per CONSTRUCTION, not at import, so setting
# it after this module loads still takes effect.
DEFAULT_CPU_IMAGE = CPU_IMAGE


class CpuLlamaCppDriver(EngineDriver):
    engine = "llamacpp"

    def __init__(self, image: str | None = None):
        self.image = image or engine_image("cpu")

    def build_spec(self, req: LoadSpecInput) -> LaunchSpec:
        weight = pick_gguf_weight(req.files)
        if not weight:
            raise ValueError(
                "CPU llama.cpp load requires a .gguf weights file in files[]; "
                f"got {req.files!r}"
            )
        mmproj = pick_mmproj(req.files)
        cmd = llamacpp_command(
            f"{req.models_mount}/{weight}",
            ngl=0,   # CPU-only: no GPU offload
            mmproj=(f"{req.models_mount}/{mmproj}" if mmproj else None),
            params=req.params,
            task=req.task,
            # #2017: lead with the binary, like amd/cuda/gb10 already do. This
            # driver was the one caller of llamacpp_command that never passed it,
            # and the CPU runner image clears the entrypoint it inherited
            # (modules/llm/runners/llama-cpu/Dockerfile: `ENTRYPOINT []`), so the
            # container had NO program at all and docker exec'd `--model`:
            #
            #   OCI runtime create failed: exec: "--model": executable file not
            #   found in $PATH
            #
            # Measured on 0.79, a clean --hardware cpu install: all three
            # deployments stuck `pending`, zero engines, while init and
            # post-install both reported success.
            #
            # `or`, NOT a get() default — compose forwards the key as
            # "${RAZZFAZZ_LLAMACPP_BINARY:-}", which SETS it to empty, and a
            # get() default only fires when the key is ABSENT. That is the #1654
            # lesson, which fixed amd and left this caller behind.
            binary=os.environ.get("RAZZFAZZ_LLAMACPP_BINARY") or "llama-server",
        )
        return LaunchSpec(
            engine=self.engine,
            image=req.runner_image or self.image,
            name=req.instance_id,
            command=cmd,
            environment={},
            devices=[],          # no GPU
            group_add=[],
            volumes={req.models_volume: {"bind": req.models_mount, "mode": "ro"}},
            network=req.network,
            security_opt=["no-new-privileges:true"],
            labels={
                "rzfz.role": "llm-engine",
                "rzfz.instance": req.instance_id,
                "rzfz.engine": self.engine,
                "rzfz.hardware": "cpu",
                "rzfz.model": req.model,      # #293 re-adoption
                "rzfz.task": req.task,
                # NODE-9: weight leaves, for the post-restart in-use guard.
                "rzfz.files": files_label(req.files),
            },
            serve_url=f"http://{req.instance_id}:8080/v1",
            health_url=f"http://{req.instance_id}:8080/health",
        )
