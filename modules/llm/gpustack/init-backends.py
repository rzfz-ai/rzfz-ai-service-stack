#!/usr/bin/env python3
"""
Idempotent registration of custom GPUStack backends.

Run after `gpustack` (the v2.1.x server container) is healthy. Re-running is
safe — the script GETs the existing backend list and only POSTs missing
entries, or PUTs entries whose config has drifted.

Invocation points (M018, Phase 6 / S06.4):
- razzfazz-init.sh: post stack-up, after gpustack healthcheck OK
- razzfazz-upgrade.sh: between data-migrate and verify_upgrade
- core/config/app/services/apply_manager.py::_execute_image_update: post
  docker compose recreate when env_var == GPUSTACK_VERSION

Why this exists:
- GPUStack 2.x ships built-in backends (vLLM, SGLang, MindIE, VoxBox).
- vLLM covers NVIDIA and most AMD GPUs out of the box, but on Strix Halo
  it can't address the BIOS-pinned VRAM (PyTorch HIP only sees the GTT
  region). M022 evaluation concluded vLLM not viable for our workload.
- For AMD Strix Halo (gfx1151) we register two llama.cpp custom backends:
  * `llama-box-vulkan-custom` (preferred) — local image built from
    `llm/runners/llama-vulkan/Dockerfile`, llama.cpp b8943 + Mesa RADV.
    M022 stress comparison favored this path on chat/code latency.
  * `llama-box-rocm-custom` (alternative) — kyuz0/amd-strix-halo-toolboxes:
    rocm-7.2.1, kept as A/B baseline and backup.
- For CPU-only inference, GPUStack 2.x ships no runner at all — we register
  ghcr.io/ggml-org/llama.cpp:server as `llama-cpp-cpu-custom`.

We register all three regardless of detected hardware. GPUStack scheduler
will only place models on a backend whose runner can actually run on the
attached devices, so unused backends sit harmless. This makes the stack
rebootable onto different hardware without re-running registration.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

GPUSTACK_API = os.environ.get("GPUSTACK_API", "http://gpustack:9090")
GPUSTACK_API_KEY = os.environ.get("GPUSTACK_API_KEY", "")
HEALTH_TIMEOUT = int(os.environ.get("GPUSTACK_HEALTH_TIMEOUT", "120"))
HEALTH_INTERVAL = 3

BACKENDS = [
    {
        "backend_name": "llama-box-vulkan-custom",
        "default_version": "b8943",
        "default_run_command": (
            "llama-server -m {{model_path}} --host 0.0.0.0 "
            "--port {{port}} -fa 1 --no-mmap -ngl 999"
        ),
        "default_entrypoint": "",
        "default_backend_param": [],
        "is_built_in": False,
        "backend_source": "custom",
        "enabled": True,
        "description": (
            "llama.cpp llama-server (b8943) with Vulkan backend on Mesa "
            "RADV (Ubuntu 24.04). For AMD Strix Halo / gfx1151. M022 "
            "head-to-head established this as faster than the ROCm runner "
            "on chat/code; same llama.cpp tree, different GPU compute path. "
            "Built locally from llm/runners/llama-vulkan/Dockerfile."
        ),
        "version_configs": {
            "b8943": {
                "image_name": "llama-vulkan-runner:b8943",
                "run_command": (
                    "llama-server -m {{model_path}} --host 0.0.0.0 "
                    "--port {{port}} -fa 1 --no-mmap -ngl 999"
                ),
                "entrypoint": "",
                "custom_framework": "rocm",
                "env": {},
            }
        },
    },
    {
        "backend_name": "llama-box-rocm-custom",
        "default_version": "rocm-7.2.1",
        "default_run_command": (
            "llama-server -m {{model_path}} --host 0.0.0.0 "
            "--port {{port}} -fa 1 --no-mmap -ngl 999"
        ),
        "default_entrypoint": "",
        "default_backend_param": [],
        "is_built_in": False,
        "backend_source": "custom",
        "enabled": True,
        "description": (
            "llama.cpp llama-server in kyuz0/amd-strix-halo-toolboxes "
            "(ROCm 7.2.1, gfx1151). Mandatory on Strix Halo: -fa 1 "
            "--no-mmap. -ngl 999 keeps everything in VRAM (BIOS-pinned). "
            "Kept as alternative to llama-box-vulkan (the M022-preferred "
            "path) for A/B and as backup if Vulkan ever regresses."
        ),
        "version_configs": {
            "rocm-7.2.1": {
                # Local wrapper image around kyuz0/amd-strix-halo-toolboxes:
                # rocm-7.2.1, built from llm/runners/llama-rocm/Dockerfile.
                # Wrapper drops the llama-server-shim onto PATH so GPUStack-
                # style `--flag=value` backend_parameters are accepted (the
                # upstream binary requires `--flag value`). Same llama.cpp
                # binary as kyuz0 — only the argv translator differs.
                "image_name": "llama-rocm-runner:rocm-7.2.1",
                "run_command": (
                    "llama-server -m {{model_path}} --host 0.0.0.0 "
                    "--port {{port}} -fa 1 --no-mmap -ngl 999"
                ),
                "entrypoint": "",
                "custom_framework": "rocm",
                "env": {},
            }
        },
    },
    {
        "backend_name": "llama-box-cpu-custom",
        "default_version": "b8000",
        "default_run_command": (
            "llama-server -m {{model_path}} --host 0.0.0.0 --port {{port}}"
        ),
        "default_entrypoint": "",
        "default_backend_param": [],
        "is_built_in": False,
        "backend_source": "custom",
        "enabled": True,
        "description": (
            "llama.cpp CPU-only runner (local wrapper around "
            "ghcr.io/ggml-org/llama.cpp:server). GPUStack 2.x does not ship "
            "a stock CPU backend; this fills the gap for testvm-cpu and "
            "HARDWARE=cpu deployments."
        ),
        "version_configs": {
            "b8000": {
                # Local wrapper image around ghcr.io/ggml-org/llama.cpp:server,
                # built from llm/runners/llama-cpu/Dockerfile. Wrapper drops
                # the llama-server-shim onto PATH so GPUStack-style
                # `--flag=value` backend_parameters are accepted.
                "image_name": "llama-cpu-runner:b8000",
                "run_command": (
                    "llama-server -m {{model_path}} --host 0.0.0.0 "
                    "--port {{port}}"
                ),
                "entrypoint": "",
                "custom_framework": "cpu",
                "env": {},
            }
        },
    },
]

# Migration: rename legacy backends to the uniform `llama-box-{compute}-custom`
# convention so the GPUStack UI (which strips the mandatory `-custom` suffix
# for display) shows clean names like `llama-box-vulkan` / `llama-box-rocm` /
# `llama-box-cpu`. The `-custom` suffix itself is enforced server-side by
# GPUStack's name validator — we can't drop it from the wire name.
#
# Models referencing the old names are reassigned to the new name; the old
# backend records are deleted. `vllm-custom` is dropped entirely — it was a
# leftover from the M022 vLLM evaluation and duplicated the built-in `vLLM`.
#
# The bottom three intermediate-state entries cover an interim broken state
# from a transitional commit on the test box that registered names without
# the `-custom` suffix; the validator rejected the registrations but the
# model.backend reassignments had already happened. Idempotent on a clean
# install (no-op if no models reference these intermediate names).
BACKEND_RENAMES = {
    # Legacy → new canonical
    "llama-box-custom":        "llama-box-rocm-custom",
    "llama-cpp-cpu-custom":    "llama-box-cpu-custom",
    # Intermediate broken state recovery (one-shot during M022 cleanup)
    "llama-box-vulkan":        "llama-box-vulkan-custom",
    "llama-box-rocm":          "llama-box-rocm-custom",
    "llama-box-cpu":           "llama-box-cpu-custom",
}
BACKEND_DELETES = {"vllm-custom"}

# rc6.7 #12: bare `llama-box` is the legacy v0.7.1 backend name. Pre-rc6.7
# razzfazz-post-install.sh hardcoded it on every model deploy; v2.x has
# nothing under that name and the model fails to initialise with
# "Inference backend llama-box not specified or not found". The right
# replacement depends on the host's HARDWARE because the v2.x runners
# are split per-hardware. Resolved at run-time by `_pick_target_for_legacy_llamabox`.
LEGACY_AMBIGUOUS_BACKENDS = {"llama-box"}


def _pick_target_for_legacy_llamabox() -> str:
    """Return the per-HARDWARE replacement for a bare `llama-box`.
    Mirrors the same logic in razzfazz-post-install.sh::_pick_backend_name.
    GPUSTACK_BACKEND env override wins if set."""
    override = os.environ.get("GPUSTACK_BACKEND", "").strip()
    if override:
        return override
    hw = os.environ.get("HARDWARE", "amd").strip().lower()
    if hw == "cpu":
        return "llama-box-cpu-custom"
    if hw == "nvidia":
        return "vllm"
    # AMD or unknown → Vulkan runner (M022 preferred path)
    return "llama-box-vulkan-custom"


def _api_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    """Make an authenticated GPUStack v2 API call. Returns (status, parsed_body)."""
    url = f"{GPUSTACK_API.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if GPUSTACK_API_KEY:
        headers["X-API-Key"] = GPUSTACK_API_KEY
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            parsed = json.loads(raw) if raw else None
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read())
        except Exception:
            parsed = None
        return e.code, parsed


def wait_for_gpustack_healthy() -> bool:
    """Poll /healthz until 200 or timeout. Returns True on success."""
    deadline = time.time() + HEALTH_TIMEOUT
    last_err = None
    while time.time() < deadline:
        try:
            status, _ = _api_request("GET", "/healthz")
            if status == 200:
                return True
            last_err = f"HTTP {status}"
        except Exception as e:
            last_err = str(e)
        time.sleep(HEALTH_INTERVAL)
    print(f"[init-backends] gpustack /healthz did not return 200 within "
          f"{HEALTH_TIMEOUT}s (last: {last_err})", file=sys.stderr)
    return False


def get_existing_backend(name: str) -> dict | None:
    """Return the existing backend dict by name, or None."""
    status, body = _api_request("GET", f"/v2/inference-backends/backend_name/{name}")
    if status == 200 and body:
        return body
    return None


_VERSION_CONFIG_MANAGED_KEYS = (
    "image_name",
    "run_command",
    "entrypoint",
    "custom_framework",
    "env",
)


def _backend_config_matches(existing: dict, target: dict) -> bool:
    """Return True if the existing backend's config matches what we'd register.

    Only compares fields the script manages — GPUStack adds server-side
    fields like `built_in_frameworks`, `id`, `created_at`, `health_check_path`
    that we don't write, and we don't want those triggering spurious updates.
    """
    for k in ("default_version", "default_run_command", "default_entrypoint", "enabled"):
        if existing.get(k) != target.get(k):
            return False
    existing_vc = existing.get("version_configs") or {}
    target_vc = target.get("version_configs") or {}
    if set(existing_vc.keys()) != set(target_vc.keys()):
        return False
    for ver, target_cfg in target_vc.items():
        existing_cfg = existing_vc.get(ver, {})
        for k in _VERSION_CONFIG_MANAGED_KEYS:
            if (existing_cfg.get(k) or None) != (target_cfg.get(k) or None):
                return False
    return True


def register_backend(target: dict) -> str:
    """Register or update one backend. Returns 'created' / 'updated' / 'unchanged'."""
    name = target["backend_name"]
    existing = get_existing_backend(name)
    if existing is None:
        status, body = _api_request("POST", "/v2/inference-backends", target)
        if status not in (200, 201):
            raise RuntimeError(
                f"POST /v2/inference-backends failed for {name}: "
                f"HTTP {status} body={body}"
            )
        return "created"
    if _backend_config_matches(existing, target):
        return "unchanged"
    backend_id = existing.get("id")
    if not backend_id:
        raise RuntimeError(f"existing backend {name} has no id")
    status, body = _api_request("PUT", f"/v2/inference-backends/{backend_id}", target)
    if status not in (200, 204):
        raise RuntimeError(
            f"PUT /v2/inference-backends/{backend_id} failed for {name}: "
            f"HTTP {status} body={body}"
        )
    return "updated"


def _list_models() -> list[dict]:
    status, body = _api_request("GET", "/v2/models")
    if status != 200 or not isinstance(body, dict):
        return []
    return body.get("items", []) or []


def _patch_model_backend(model: dict, new_backend: str) -> bool:
    """PUT /v2/models/{id} to update only the `backend` field. Returns True on success."""
    mid = model.get("id")
    name = model.get("name", f"#{mid}")
    body = dict(model)
    for k in ("id", "created_at", "updated_at", "ready_replicas", "deleted_at"):
        body.pop(k, None)
    body["backend"] = new_backend
    status, resp = _api_request("PUT", f"/v2/models/{mid}", body)
    if status not in (200, 204):
        print(
            f"[init-backends] migrate model {name} backend "
            f"-> {new_backend}: HTTP {status} body={resp}",
            file=sys.stderr,
        )
        return False
    return True


def migrate_old_backend_names() -> tuple[int, int, int]:
    """One-shot rename of legacy `*-custom` backend names + delete deprecated.

    Returns (models_updated, backends_renamed_or_deleted, failures).
    Idempotent: a no-op once all models reference the new names and the
    old backend records are gone.
    """
    models_updated = 0
    backends_changed = 0
    failures = 0

    # 1. Reassign any model whose backend is an old name → new name.
    models = _list_models()
    for model in models:
        old = model.get("backend")
        if old in BACKEND_RENAMES:
            new = BACKEND_RENAMES[old]
            if _patch_model_backend(model, new):
                models_updated += 1
                print(
                    f"[init-backends] migrated model {model.get('name')} "
                    f"backend {old} -> {new}"
                )
            else:
                failures += 1
        elif old in LEGACY_AMBIGUOUS_BACKENDS:
            # rc6.7 #12: bare `llama-box` from pre-rc6.7 razzfazz-post-install.sh
            # — pick the per-HARDWARE replacement at run time.
            new = _pick_target_for_legacy_llamabox()
            if _patch_model_backend(model, new):
                models_updated += 1
                print(
                    f"[init-backends] migrated model {model.get('name')} "
                    f"backend {old} -> {new} (legacy v0.7.1 name; resolved "
                    f"via HARDWARE={os.environ.get('HARDWARE', 'amd')})"
                )
            else:
                failures += 1
        elif old in BACKEND_DELETES:
            # Model references a backend we're dropping. Don't auto-reassign
            # (no obvious target). Warn loudly so the operator handles it.
            print(
                f"[init-backends] WARNING: model {model.get('name')} "
                f"references deprecated backend {old} — set replicas=0 and "
                f"reassign manually before that backend is deleted",
                file=sys.stderr,
            )

    # 2. Delete the old backend records.
    for old_name in list(BACKEND_RENAMES.keys()) + list(BACKEND_DELETES):
        existing = get_existing_backend(old_name)
        if existing is None:
            continue
        bid = existing.get("id")
        status, resp = _api_request("DELETE", f"/v2/inference-backends/{bid}")
        if status in (200, 204):
            backends_changed += 1
            print(f"[init-backends] deleted legacy backend {old_name} (id={bid})")
        else:
            print(
                f"[init-backends] failed to delete {old_name} (id={bid}): "
                f"HTTP {status} body={resp}",
                file=sys.stderr,
            )
            failures += 1

    return models_updated, backends_changed, failures


def main() -> int:
    if not GPUSTACK_API_KEY:
        print("[init-backends] GPUSTACK_API_KEY is not set — refusing to "
              "run unauthenticated", file=sys.stderr)
        return 2

    print(f"[init-backends] target: {GPUSTACK_API}")
    if not wait_for_gpustack_healthy():
        return 3

    failures = 0

    # Step 1: register the canonical backends.
    for target in BACKENDS:
        name = target["backend_name"]
        try:
            outcome = register_backend(target)
            print(f"[init-backends] {name}: {outcome}")
        except Exception as e:
            print(f"[init-backends] {name}: FAILED — {e}", file=sys.stderr)
            failures += 1

    # Step 2: migrate legacy `*-custom` names if present (idempotent).
    try:
        models_updated, backends_changed, mig_fails = migrate_old_backend_names()
        if models_updated or backends_changed:
            print(
                f"[init-backends] migration: {models_updated} model(s) "
                f"reassigned, {backends_changed} legacy backend(s) removed"
            )
        failures += mig_fails
    except Exception as e:
        print(f"[init-backends] migration FAILED — {e}", file=sys.stderr)
        failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
