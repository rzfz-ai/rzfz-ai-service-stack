# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Node → manager registration client (#254 Phase-2 P2-B1).

After the node launches its engines, it POSTs what it is now serving to the
manager's ``/api/workers`` so the fleet self-registers. This module builds the
payload from the node identity + the agent's loaded-instance table and does the
authenticated POST. The HTTP call is injected (``http_post``) so it unit-tests
with no network; the periodic reporter loop that drives it on the running node
is runtime wiring (deferred with the docker-socket mount, like the supervisor
tick).

Import-safe: stdlib only; no httpx at import (the real client is passed in).
"""
from __future__ import annotations


def build_registration(worker: dict, loaded: dict) -> dict:
    """Serialize {node identity + loaded instances} into the manager's
    WorkerRegistration shape. ``worker`` = {name, address, hardware, engine};
    ``loaded`` = the agent's instance_id -> {model, endpoint, ...} table."""
    models = []
    for _instance_id, rec in (loaded or {}).items():
        models.append(
            {
                "model_name": rec.get("model"),
                "endpoint": rec.get("endpoint"),
                # #286: default to loading — a launched engine is NOT ready until
                # the supervisor's health probe says so (_sync_instance_status).
                "status": rec.get("status", "loading"),
                # #287: phase detail — "pulling 42%" during a weight fetch, or the
                # failure reason. Surfaced in the console next to the status.
                "detail": rec.get("detail"),
                "api_key": rec.get("api_key"),
                # Q2 keying: the engine instance so replicas + same-model-per-worker
                # register as distinct instances instead of collapsing into one.
                "instance_id": _instance_id,
            }
        )
    return {
        "name": worker["name"],
        "address": worker.get("address", ""),
        "hardware": worker.get("hardware"),
        "engine": worker.get("engine"),
        # Slice A: node's razzfazz.ai stack version + engine (llama.cpp) build,
        # surfaced in the console's Fleet detail. Optional — None if unknown.
        "stack_version": worker.get("stack_version"),
        # #1932: WHERE that version came from — a mounted repo, the .env, or
        # the image itself. On a thin node the last one is the only possible
        # answer, and a reader has to be able to tell which they got.
        "stack_version_source": worker.get("stack_version_source"),
        "engine_version": worker.get("engine_version"),
        # #1932: WHY the version is missing, when it is. An empty cell used to
        # mean five different things (not applicable / no image / image absent
        # / probe failed / upstream changed its output), and each asks for a
        # different action.
        "engine_version_why": worker.get("engine_version_why"),
        # #262 host-routable addressing: the worker's own reachable base, so a
        # remote master can dial this worker's engines. None on a same-box node.
        "advertise_addr": worker.get("advertise_addr"),
        # #1535: this node routes a relayed request to the engine serving the
        # model the request NAMES, so it can hold several models at once
        # (#262's "the worker picks a local engine"). Declared, not assumed:
        # a master that allows a second model on a worker whose agent still
        # forwards to its first ready engine would get the #929 failure —
        # right model name, wrong weights, no error. An older agent omits the
        # field, and the master keeps refusing the second placement for it.
        "relay_model_routing": True,
        # #295 fits-check budget: host RAM + best-effort VRAM. Optional.
        "mem_total_gb": worker.get("mem_total_gb"),
        "vram_total_gb": worker.get("vram_total_gb"),
        # live utilization for the console dashboard load graph. Optional —
        # absent off-box / in tests. See runtime._live_metrics.
        "vram_used_gb": worker.get("vram_used_gb"),
        "load": worker.get("load"),
        "gpu_util": worker.get("gpu_util"),
        "mem_used_gb": worker.get("mem_used_gb"),
        "ncpu": worker.get("ncpu"),
        "models": models,
    }


def build_external_backend_registration(
    name: str,
    address: str,
    endpoint: str,
    model_names: list[str],
    *,
    hardware: str = "apple-silicon",
    engine: str = "ollama",
    api_key: str | None = None,
) -> dict:
    """Registration for a PRE-EXISTING external OpenAI-compatible endpoint —
    e.g. a Mac running Ollama (#254 P2-C1, absorbing the standalone mac-gateway).

    One endpoint serves many models (Ollama routes by model name), so every
    model points at the same ``endpoint``. No engine container is launched
    (the Mac runs Ollama itself); the worker-agent / an operator helper just
    PUBLISHES the endpoint so the manager folds the Mac into the fleet router
    (with the Mac's own upstream key via P2-B3). GPUStack is untouched."""
    return {
        "name": name,
        "address": address,
        "hardware": hardware,
        "engine": engine,
        "models": [
            {"model_name": mn, "endpoint": endpoint, "status": "ready", "api_key": api_key}
            for mn in model_names
        ],
    }


def register_with_manager(manager_url: str, node_key: str, payload: dict, *, http_post):
    """POST the registration to the manager with the node Bearer key. Returns
    whatever ``http_post`` returns (a response object). ``http_post`` has the
    httpx signature ``(url, *, json, headers)``."""
    url = manager_url.rstrip("/") + "/api/workers"
    return http_post(
        url,
        json=payload,
        headers={"authorization": f"Bearer {node_key}"},
    )
