# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""AMD Strix Halo (gfx1151) llama.cpp Vulkan engine driver (#254 P2-A1).

Carries the hard-won Strix tuning so the manager can't foot-gun a node (all in
the shared ``llamacpp_command``): full GPU offload (`-ngl 999` — the iGPU shares
host RAM, BIOS-pinned VRAM), host prompt-cache OFF (`--cache-ram 0
--ctx-checkpoints 0`), auto `--mmproj` for vision, and NEVER `--swa-full` (the
~256× SWA-KV host-OOM trap, dropped even if requested).

The engine image is the locally-built Vulkan runner (overridable via env at
wiring time); the container joins the stack bridge so the manager's router
reaches it by name at http://<instance>:8080/v1.
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
from app.drivers.images import AMD_IMAGE, engine_image


# #331 the image this driver launches lives in app.drivers.images, the single
# authority cross-checked against what cli/init.sh and cli/upgrade.sh actually
# build. Kept as a module constant for back-compat; the env override
# (RAZZFAZZ_ENGINE_IMAGE_AMD) is resolved per CONSTRUCTION, not at import, so setting
# it after this module loads still takes effect.
DEFAULT_AMD_IMAGE = AMD_IMAGE


def _gpu_group_add() -> list[str]:
    """Supplementary groups for GPU device access, as NUMERIC host GIDs from
    RAZZFAZZ_GPU_GROUP_GIDS (comma-separated). Default empty: a root engine +
    /dev/kfd + /dev/dri passthrough already has access, and group *names*
    (video/render) can't be used — docker resolves them against the engine
    image's /etc/group, which minimal runner images don't define (that made
    container start fail). Set the host's video/render GIDs here only when the
    engine runs non-root."""
    raw = os.environ.get("RAZZFAZZ_GPU_GROUP_GIDS", "")
    return [g.strip() for g in raw.split(",") if g.strip()]


class AmdLlamaCppDriver(EngineDriver):
    engine = "llamacpp"

    def __init__(self, image: str | None = None):
        self.image = image or engine_image("amd")

    def build_spec(self, req: LoadSpecInput) -> LaunchSpec:
        weight = pick_gguf_weight(req.files)
        if not weight:
            raise ValueError(
                "AMD llama.cpp load requires a .gguf weights file in files[]; "
                f"got {req.files!r}"
            )
        mmproj = pick_mmproj(req.files)
        cmd = llamacpp_command(
            f"{req.models_mount}/{weight}",
            ngl=999,   # full offload to the iGPU
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
            environment={"GGML_VK_VISIBLE_DEVICES": "0"},
            devices=["/dev/kfd", "/dev/dri"],   # ROCm/Vulkan passthrough
            group_add=_gpu_group_add(),          # numeric host GIDs (env); see helper
            volumes={req.models_volume: {"bind": req.models_mount, "mode": "ro"}},
            network=req.network,
            security_opt=["no-new-privileges:true"],
            labels={
                "rzfz.role": "llm-engine",
                "rzfz.instance": req.instance_id,
                "rzfz.engine": self.engine,
                "rzfz.hardware": "amd",
                # #293 re-adoption: recover model + serve mode after a node restart.
                "rzfz.model": req.model,
                "rzfz.task": req.task,
                # NODE-9: the flat weight leaves this engine reads. Labels are
                # ALL _readopt_engines has to rebuild `loaded` from, so without
                # this the delete-in-use guard is blind after a node restart.
                "rzfz.files": files_label(req.files),
            },
            serve_url=f"http://{req.instance_id}:8080/v1",
            health_url=f"http://{req.instance_id}:8080/health",
        )
