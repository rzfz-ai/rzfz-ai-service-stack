"""
GPUStack 2.x runtime patches — fixes for Strix Halo (gfx1151) + custom GGUF
backends. Three independent patches, all delivered through this single
auto-loaded module:

  Patch 1: scheduler dispatch (is_gguf_model)
  Patch 2: dashboard model-weight sizing for GGUF (get_model_weight_size)
  Patch 3: GPU utilization sysfs fallback for gfx1151 (AMDDetector.detect)

This file is auto-loaded by Python before main() if it lives anywhere on
sys.path under the name `usercustomize.py` (PEP 370 / site.py). We mount
it at `/usr/local/lib/python3.11/dist-packages/usercustomize.py` inside
the `gpustack` container via `llm/compose.yml`.

To remove: delete the volume mount and restart gpustack. The patches are
a no-op when not present.

============================================================================
Patch 1 — Scheduler dispatch
============================================================================

GPUStack 2.1.x dispatches its placement selector by file extension:

    # gpustack/scheduler/scheduler.py L420
    if is_gguf_model(model):
        candidates_selector = GGUFResourceFitSelector(...)   # pessimistic
    elif model.backend == VLLM:
        candidates_selector = VLLMResourceFitSelector(...)
    else:
        candidates_selector = CustomBackendResourceFitSelector(...)  # optimistic

`is_gguf_model(model)` returns True iff the model filename ends in `.gguf`.
The GGUF selector reads `meta.n_ctx_train` from the file and refuses
placement when the worst-case KV-cache estimate exceeds free VRAM — *even
when the runtime `--ctx-size` argument shrinks the actual footprint by an
order of magnitude*.

For our `llama-box-custom` runner (kyuz0/amd-strix-halo-toolboxes:rocm-7.2.1)
and `llama-box-vulkan-custom` runner (our llama-vulkan-runner:b8943) this
means models like qwen3-coder-next (Q4_K_M, 80B-A3B, n_ctx_train=262144)
or gemma4 (Q8_0, 26B-A4B, n_ctx_train=262144) get blocked at scheduling
time even though the runtime config would fit comfortably. M018 documented
this; M022 stress testing confirmed it.

We override `is_gguf_model` to return False whenever the model's backend
is one of our trust-the-operator custom backends. The dispatch then falls
through to `CustomBackendResourceFitSelector`. Built-in backends (vLLM,
SGLang, MindIE, VoxBox) and their selectors are untouched.

============================================================================
Patch 2 — Dashboard model-weight sizing for GGUF
============================================================================

`gpustack.utils.hub.get_model_weight_size(model, token)` computes the
weight footprint that drives the `Allocated VRAM / RAM` column on the
dashboard, the deploy-modal compatibility check, and (post-Patch-1) the
CustomBackendResourceFitSelector's claim. The upstream implementation
hardcodes `weight_file_extensions = (".safetensors", ".bin", ".pt", ".pth")`
— `.gguf` is missing — and it ignores `model.huggingface_filename`,
which means even adding `.gguf` would sum every quant in a multi-quant
GGUF repo (e.g. unsloth/gemma-4-26B-A4B-it-GGUF ships ~20 quants).

For HF GGUF repos the function returns 0 → fallback formula
`0 * 1.2 + 2 GiB` for LLM, `0 * 1.2 + 512 MiB` for embedding/reranker.
Result: dashboard shows 2 GiB / 512 MiB for every GGUF model regardless
of actual size. Pre-Patch-1 this was hidden because GGUF models went
through the GGUF-specific selector which parses the file directly;
Patch 1 surfaced it.

We wrap the function: if `model.huggingface_filename` is set AND ends in
`.gguf` (or contains a glob), enumerate matching files in the HF repo
(via huggingface_hub.HfApi.model_info with files_metadata=True) and sum
their sizes. Falls through to original on any failure (network timeout,
non-HF source, no filename set, etc.).

============================================================================
Patch 3 — GPU utilization sysfs fallback for gfx1151
============================================================================

`gpustack_runtime.detector.amd.AMDDetector.detect()` reads core
utilization via amdsmi:

    dev_gpu_metrics_info = pyamdsmi.amdsmi_get_gpu_metrics_info(dev)
    dev_cores_util = dev_gpu_metrics_info.get("average_gfx_activity", 0)

The amdsmi version shipped with `gpustack/gpustack:v2.1.2` doesn't
understand gfx1151's gpu_metrics ABI — it raises
`AMDSMI_STATUS_UNEXPECTED_DATA (43)` on every call. The fallback path
calls `pyrocmsmi.rsmi_dev_busy_percent_get` but rocm-smi is not even
installed in the gpustack image, so that fails too. Result:
`dev_cores_util = 0` → dashboard shows 0% even at full GPU load.

The AMD kernel driver always exposes `/sys/class/drm/card{N}/device/
gpu_busy_percent` regardless of amdsmi/rocm-smi versions. We wrap
`AMDDetector.detect()`: after the original call returns, for any device
with `cores_utilization` == 0 (likely a detection failure), we read
gpu_busy_percent from sysfs and patch the value. Same pattern the 0.7.1
custom Vulkan build used via mock_rocm_smi.sh.

This is a no-op on hardware where amdsmi works (the original returns a
non-zero value and we skip the fallback). Strix Halo gets the sysfs
value; everything else stays unchanged.
"""

from __future__ import annotations

import sys

# rc6.7 #48's PR-#5255 leak-fix backport was imported here. It patched
# `gpustack.server.bus`, a module that exists only in GPUStack 2.x — removed
# from the product by #1447 (cutover C7a). On 0.7.1 the patch could only ever
# take its "not importable" branch and print that it was skipped, which is a
# line in every container start claiming a patch that has nothing to patch.

# ---------------------------------------------------------------------------
# Patch 1: scheduler dispatch
# ---------------------------------------------------------------------------

OPTIMISTIC_BACKENDS = {
    "llama-box-vulkan-custom",
    "llama-box-rocm-custom",
    "llama-box-cpu-custom",
}


def _install_scheduler_patch() -> None:
    try:
        from gpustack.schemas import models as _m
    except Exception as e:
        print(
            f"[usercustomize] Patch 1 skipped — gpustack not importable yet ({e!r})",
            file=sys.stderr,
        )
        return

    original_is_gguf_model = _m.is_gguf_model

    def patched_is_gguf_model(model):
        backend = getattr(model, "backend", None)
        if backend in OPTIMISTIC_BACKENDS:
            return False
        return original_is_gguf_model(model)

    _m.is_gguf_model = patched_is_gguf_model

    # Scheduler imports the symbol at module load, so rebind there too.
    try:
        from gpustack.scheduler import scheduler as _s
        if hasattr(_s, "is_gguf_model"):
            _s.is_gguf_model = patched_is_gguf_model
    except Exception:
        pass

    print(
        f"[usercustomize] Patch 1 active: is_gguf_model(model) returns False "
        f"for backends in {sorted(OPTIMISTIC_BACKENDS)} (scheduler routes them "
        f"through CustomBackendResourceFitSelector instead of "
        f"GGUFResourceFitSelector)",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Patch 2: GGUF weight-size resolver
# ---------------------------------------------------------------------------

def _install_weight_size_patch() -> None:
    try:
        from gpustack.utils import hub as _hub
        from gpustack.schemas.models import SourceEnum
    except Exception as e:
        print(
            f"[usercustomize] Patch 2 skipped — gpustack not importable yet ({e!r})",
            file=sys.stderr,
        )
        return

    import fnmatch
    import threading
    import time

    original_get_model_weight_size = _hub.get_model_weight_size

    # Process-lifetime memoization for the HF model_info lookup.
    #
    # Why: gpustack's scheduler reconciler re-evaluates unscheduled models on
    # every loop iteration. Each tick calls policies.utils.estimate_model_vram
    # → asyncio.to_thread(get_model_weight_size, ...). With Patch 1 forcing
    # custom-backend GGUFs through CustomBackendResourceFitSelector, an
    # un-placeable model (e.g. qwen3-coder-next blocked at first try) drives
    # repeated HF API calls. Without this cache, large multi-quant repos
    # (Unsloth ships ~30 quants × N shards) cause master RSS to balloon
    # until vm.panic_on_oom = 2 fires. See memory:
    # project_gpustack_estimator_patch_path.md.
    #
    # Strategy: dedupe via per-key lock. A second concurrent caller for the
    # same (repo_id, fn) blocks on the lock instead of starting another HF
    # round-trip. Successful results cached forever; failures cached for
    # NEGATIVE_TTL_SECS so the very next reconciler tick doesn't immediately
    # retry. Network-level failures will still re-attempt after the TTL.
    NEGATIVE_TTL_SECS = 60.0

    _weight_cache: dict = {}                     # key -> (timestamp, total_bytes_or_0)
    _weight_locks: dict = {}                     # key -> threading.Lock
    _weight_locks_mutex = threading.Lock()       # guards the locks dict itself

    def _key_lock(key):
        with _weight_locks_mutex:
            lk = _weight_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                _weight_locks[key] = lk
            return lk

    def _cache_lookup(key):
        entry = _weight_cache.get(key)
        if entry is None:
            return None
        ts, total = entry
        # successful result: cache forever
        if total > 0:
            return total
        # negative result: respect TTL
        if (time.time() - ts) < NEGATIVE_TTL_SECS:
            return 0
        return None

    def patched_get_model_weight_size(model, token=None):
        # Only intercept HF GGUF models — matches the original Patch 2 scope.
        fn = getattr(model, "huggingface_filename", None) or getattr(model, "model_scope_file_path", None) or ""
        if not fn or ".gguf" not in fn.lower():
            return original_get_model_weight_size(model, token)

        repo_id = getattr(model, "huggingface_repo_id", None) or ""
        cache_key = (repo_id, fn)

        # Fast path: lockless cache read
        cached = _cache_lookup(cache_key)
        if cached is not None:
            return cached if cached > 0 else original_get_model_weight_size(model, token)

        # Acquire per-key lock so concurrent callers for the same model
        # collapse to one HF API call. This is the actual leak fix —
        # asyncio.wait_for inside policies/utils.estimate_model_vram has a
        # 15-second timeout but the underlying to_thread worker keeps running
        # until the HF call returns. Without this lock, every reconciler tick
        # spawns another worker thread holding its own requests.Session +
        # partial JSON buffer.
        with _key_lock(cache_key):
            # Re-check cache after acquiring lock — a concurrent caller may have
            # populated it while we waited.
            cached = _cache_lookup(cache_key)
            if cached is not None:
                return cached if cached > 0 else original_get_model_weight_size(model, token)

            total = 0
            try:
                if model.source == SourceEnum.HUGGING_FACE:
                    from huggingface_hub import HfApi
                    api = HfApi(token=token)
                    info = api.model_info(model.huggingface_repo_id, files_metadata=True)
                    for sib in info.siblings or []:
                        rfn = getattr(sib, "rfilename", None) or ""
                        if fnmatch.fnmatch(rfn, fn):
                            total += getattr(sib, "size", 0) or 0
                # ModelScope path: total stays 0; falls through to original below.
            except Exception as e:
                print(
                    f"[usercustomize] Patch 2 fallback for {getattr(model, 'name', '?')}: "
                    f"GGUF size resolution failed ({e!r}); caching negative result for "
                    f"{NEGATIVE_TTL_SECS}s",
                    file=sys.stderr,
                )
                total = 0

            _weight_cache[cache_key] = (time.time(), total)
            return total if total > 0 else original_get_model_weight_size(model, token)

    _hub.get_model_weight_size = patched_get_model_weight_size

    # The function is also imported by the policies utils module that calls it
    # via asyncio.to_thread — rebind there too so the bound reference picks up
    # the patched version.
    try:
        from gpustack.policies import utils as _pu
        if hasattr(_pu, "get_model_weight_size"):
            _pu.get_model_weight_size = patched_get_model_weight_size
    except Exception:
        pass

    print(
        "[usercustomize] Patch 2 active: get_model_weight_size now resolves "
        "GGUF files via huggingface_hub.model_info (matches model.huggingface_filename "
        f"as glob), with per-(repo_id, filename) lock + cache (negative TTL "
        f"{NEGATIVE_TTL_SECS}s) to prevent reconciler-loop thread pile-up",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Patch 3: GPU utilization sysfs fallback (gfx1151)
# ---------------------------------------------------------------------------

def _read_sysfs_gpu_busy(card_id):
    """Read /sys/class/drm/card{card_id}/device/gpu_busy_percent (0-100)."""
    if card_id is None:
        return None
    try:
        path = f"/sys/class/drm/card{int(card_id)}/device/gpu_busy_percent"
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _read_sysfs_vram_used(card_id):
    """Read /sys/class/drm/card{card_id}/device/mem_info_vram_used (bytes)."""
    if card_id is None:
        return None
    try:
        path = f"/sys/class/drm/card{int(card_id)}/device/mem_info_vram_used"
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _read_sysfs_int(card_id, basename):
    """Read an integer-valued sysfs file under /sys/class/drm/card{N}/device/.
    Returns None on any I/O / parse failure."""
    if card_id is None:
        return None
    try:
        with open(f"/sys/class/drm/card{int(card_id)}/device/{basename}") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _read_sysfs_hex(card_id, basename):
    """Read a 0x… hex-valued sysfs file. Returns None on any failure."""
    if card_id is None:
        return None
    try:
        with open(f"/sys/class/drm/card{int(card_id)}/device/{basename}") as f:
            v = f.read().strip()
        return int(v, 16) if v.startswith("0x") else int(v)
    except Exception:
        return None


def _enumerate_amd_card_ids():
    """List /sys/class/drm/card{N} entries that have AMD vendor 0x1002.
    Used when amdsmi enumeration returns empty — we still want to find
    cards the kernel exposed."""
    import os
    found = []
    drm = "/sys/class/drm"
    if not os.path.isdir(drm):
        return found
    for entry in os.listdir(drm):
        # cardN where N is digits (skip cardN-HDMI-A-... output entries)
        if not entry.startswith("card") or "-" in entry:
            continue
        try:
            card_id = int(entry[4:])
        except ValueError:
            continue
        if _read_sysfs_hex(card_id, "vendor") == 0x1002:
            found.append(card_id)
    return sorted(found)


def _synthesize_amd_device_from_sysfs(card_id):
    """Construct a Device entry from sysfs when amdsmi returns nothing
    (rc6.7 #21: Strix Halo case — amdsmi can't even enumerate the GPU).
    Returns the Device or None if the card doesn't look AMD or sysfs is
    unreadable."""
    try:
        from gpustack_runtime.detector.__types__ import (
            Device,
            ManufacturerEnum,
            DeviceMemoryStatusEnum,
        )
    except Exception:
        return None

    if _read_sysfs_hex(card_id, "vendor") != 0x1002:
        return None

    vram_total = _read_sysfs_int(card_id, "mem_info_vram_total")
    if not vram_total or vram_total <= 0:
        return None

    vram_used = _read_sysfs_int(card_id, "mem_info_vram_used") or 0
    busy_pct = _read_sysfs_gpu_busy(card_id) or 0
    device_id = _read_sysfs_hex(card_id, "device")

    name_by_device_id = {
        0x1586: "AMD Radeon 8060S Graphics (Strix Halo, gfx1151)",
    }
    name = name_by_device_id.get(
        device_id,
        f"AMD GPU (device 0x{device_id:04x})" if device_id else "AMD GPU",
    )

    mem_mib = vram_total >> 20
    used_mib = vram_used >> 20

    manufacturer = (
        getattr(ManufacturerEnum, "AMD", None)
        or getattr(ManufacturerEnum, "ATI", None)
        or ManufacturerEnum.UNKNOWN
    )

    return Device(
        manufacturer=manufacturer,
        index=card_id,
        name=name,
        uuid=f"sysfs-card{card_id}",
        cores_utilization=busy_pct,
        memory=mem_mib,
        memory_used=used_mib,
        memory_utilization=(used_mib / mem_mib) if mem_mib > 0 else 0,
        memory_status=DeviceMemoryStatusEnum.UNKNOWN,
        appendix={"card_id": card_id, "synthesized_from_sysfs": True},
    )


def _install_gpu_util_patch() -> None:
    try:
        from gpustack_runtime.detector import amd as _amd
    except Exception as e:
        print(
            f"[usercustomize] Patch 3 skipped — gpustack_runtime not importable ({e!r})",
            file=sys.stderr,
        )
        return

    original_detect = _amd.AMDDetector.detect

    def patched_detect(self):
        devices = original_detect(self) or []

        # rc6.7 #21: amdsmi on Strix Halo (gfx1151) can return an EMPTY
        # device list — not just bad metric values. Synthesize a Device
        # from sysfs so the scheduler sees the GPU and gets a usable
        # VRAM-capacity number to compare against model RAM-claim.
        if not devices:
            for card_id in _enumerate_amd_card_ids():
                synth = _synthesize_amd_device_from_sysfs(card_id)
                if synth is not None:
                    devices.append(synth)
                    print(
                        f"[usercustomize] Patch 3: synthesized AMD device "
                        f"{synth.name} from sysfs (card {card_id}, "
                        f"{synth.memory >> 10} GiB VRAM); amdsmi returned "
                        f"no devices",
                        file=sys.stderr,
                    )
            if not devices:
                return devices

        for dev in devices:
            card_id = (dev.appendix or {}).get("card_id") if hasattr(dev, "appendix") else None
            # cores_utilization fallback
            cu = getattr(dev, "cores_utilization", None)
            if cu is None or cu == 0:
                sysfs_pct = _read_sysfs_gpu_busy(card_id)
                if sysfs_pct is not None:
                    dev.cores_utilization = sysfs_pct
            # memory_used fallback (amdsmi vram_usage may also fail)
            mu = getattr(dev, "memory_used", None)
            if mu is None or mu == 0:
                sysfs_vram = _read_sysfs_vram_used(card_id)
                if sysfs_vram is not None:
                    dev.memory_used = sysfs_vram >> 20
            # memory (capacity) fallback — synthesize when amdsmi returns 0
            mem = getattr(dev, "memory", None)
            if mem is None or mem == 0:
                sysfs_total = _read_sysfs_int(card_id, "mem_info_vram_total")
                if sysfs_total:
                    dev.memory = sysfs_total >> 20
        return devices

    _amd.AMDDetector.detect = patched_detect
    print(
        "[usercustomize] Patch 3 active: AMDDetector.detect falls back to "
        "/sys/class/drm/card{N}/device/{gpu_busy_percent,mem_info_vram_used,"
        "mem_info_vram_total} when amdsmi returns 0; SYNTHESIZES a Device "
        "from sysfs when amdsmi returns no devices at all (works around "
        "amdsmi ABI mismatch on gfx1151)",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Patch 4: estimate_model_vram → gguf-parser-aware (rc6.7 #49)
#
# Restores v0.7.1 behavior where backend_parameters (--ctx-size, --parallel,
# --cache-type-k|v, --gpu-layers) directly drive the scheduler's placement
# claim. v2.1.x's policies/utils.estimate_model_vram is a static heuristic:
#
#     vram_claim = weight_size * 1.2 + framework_overhead   # ignores ctx etc.
#
# Operators expecting v0.7.1 semantics (lower --ctx-size → smaller claim →
# more models fit) lose that lever entirely. Patch 4 wraps the function so
# that for HF/MS GGUF models on our OPTIMISTIC_BACKENDS it delegates to
# scheduler.calculator.calculate_gguf_model_resource_claim() — the same
# subprocess call to the bundled gguf-parser binary that the upstream GGUF
# selector uses, which DOES factor in backend_parameters.
#
# Process-lifetime cache + per-key asyncio.Lock prevent the subprocess from
# being re-spawned every reconciler tick. Negative cache TTL keeps a failed
# attempt from blocking subsequent ones for too long. Falls through to the
# original heuristic on any failure.
# ---------------------------------------------------------------------------


def _install_estimator_v07_compat_patch() -> None:
    try:
        from gpustack.policies import utils as _pu
        from gpustack.scheduler.calculator import (
            GGUFParserOutput,
            GGUFParserCommandMutableParameters,
        )
        from gpustack.schemas.models import SourceEnum
        from gpustack.utils.compat_importlib import pkg_resources
    except Exception as e:
        print(
            f"[usercustomize] Patch 4 skipped — imports failed ({e!r})",
            file=sys.stderr,
        )
        return

    import asyncio
    import os
    import platform
    import threading
    import time

    NEGATIVE_TTL_SECS = 60.0
    SUBPROCESS_TIMEOUT_SECS = 30.0
    _claim_cache: dict = {}                  # key -> (timestamp, vram_bytes_or_0)
    _claim_locks: dict = {}                  # key -> asyncio.Lock
    _claim_locks_mutex = threading.Lock()    # guards _claim_locks dict

    # Resolve the bundled gguf-parser binary path once, the same way
    # gpustack/scheduler/calculator.py:_gguf_parser_command does.
    try:
        _bin_path = str(
            pkg_resources.files("gpustack.third_party.bin.gguf-parser").joinpath(
                "gguf-parser"
                + (".exe" if platform.system().lower() == "windows" else "")
            )
        )
        if not os.path.exists(_bin_path):
            raise FileNotFoundError(_bin_path)
    except Exception as e:
        print(
            f"[usercustomize] Patch 4 skipped — gguf-parser binary not found ({e!r})",
            file=sys.stderr,
        )
        return

    def _cache_key(model):
        repo = getattr(model, "huggingface_repo_id", None) or ""
        fn = (
            getattr(model, "huggingface_filename", None)
            or getattr(model, "model_scope_file_path", None)
            or ""
        )
        ms_id = getattr(model, "model_scope_model_id", None) or ""
        bp = tuple(getattr(model, "backend_parameters", None) or [])
        bv = getattr(model, "backend_version", None) or ""
        return (repo, fn, ms_id, bv, bp)

    def _key_lock(key):
        with _claim_locks_mutex:
            lk = _claim_locks.get(key)
            if lk is None:
                lk = asyncio.Lock()
                _claim_locks[key] = lk
            return lk

    def _cache_lookup(key):
        entry = _claim_cache.get(key)
        if entry is None:
            return None
        ts, val = entry
        if val > 0:
            return val
        if (time.time() - ts) < NEGATIVE_TTL_SECS:
            return 0
        return None

    original_estimate = _pu.estimate_model_vram

    async def patched_estimate_model_vram(model, token=None, workers=None, session=None):
        # Honor the existing per-model env-var override. The original also
        # checks this, but checking here avoids the (cheap) cache lookup +
        # lock dance when the operator has already pinned a value.
        env_claim = _pu.get_vram_claim_from_model_env(model)
        if env_claim is not None:
            return env_claim

        # Only intercept HF/MS GGUF models on our optimistic backends.
        # Built-in backends (vLLM, SGLang, MindIE, VoxBox) keep their own
        # estimators; non-GGUF or non-HF/MS models fall through to original.
        backend = getattr(model, "backend", None)
        if backend not in OPTIMISTIC_BACKENDS:
            return await original_estimate(model, token, workers, session)

        fn = (
            getattr(model, "huggingface_filename", None)
            or getattr(model, "model_scope_file_path", None)
            or ""
        )
        if not fn or ".gguf" not in fn.lower():
            return await original_estimate(model, token, workers, session)

        if model.source not in (SourceEnum.HUGGING_FACE, SourceEnum.MODEL_SCOPE):
            return await original_estimate(model, token, workers, session)

        key = _cache_key(model)

        # Fast path: lockless read.
        cached = _cache_lookup(key)
        if cached is not None:
            return cached if cached > 0 else await original_estimate(
                model, token, workers, session
            )

        # Slow path: dedupe concurrent gguf-parser subprocess invocations
        # for the same (repo, file, params) key.
        lock = _key_lock(key)
        async with lock:
            cached = _cache_lookup(key)
            if cached is not None:
                return cached if cached > 0 else await original_estimate(
                    model, token, workers, session
                )

            try:
                # Build the gguf-parser command directly. We deliberately
                # bypass scheduler.calculator._gguf_parser_command_args_from_source
                # because it depends on global_config.huggingface_token and
                # list_repo() which fail on configs without an HF token. The
                # gguf-parser binary itself accepts --hf-repo/--hf-file and
                # the HF Hub anonymous download works for public repos.
                command = [
                    _bin_path,
                    "--skip-tokenizer",
                    "--skip-metadata",
                    "--json",
                    "--gpu-layers",
                    "-1",
                ]

                # HF token: honor it if set in env, but don't fail when it's
                # not (anonymous download works for public repos). Resolved
                # FIRST so it's available to the shard resolver below.
                hf_token = os.environ.get("HF_TOKEN") or os.environ.get(
                    "HUGGING_FACE_HUB_TOKEN"
                )

                # Source args. For HF/MS we may have a glob pattern (multi-shard
                # GGUFs use --hf-file 'Foo-Q4_K_M-*.gguf'). gguf-parser CLI does
                # NOT expand globs itself — the upstream gpustack wrapper does
                # this via list_repo() which is the path we bypass. We do our
                # own glob resolution via huggingface_hub.HfApi.model_info().
                # gguf-parser auto-discovers later shards from the first one.
                import fnmatch

                def _resolve_first_shard(repo_id: str, pattern: str, token: str) -> str:
                    if not pattern or ("*" not in pattern and "?" not in pattern and "[" not in pattern):
                        return pattern
                    try:
                        from huggingface_hub import HfApi
                        info = HfApi(token=token).model_info(
                            repo_id, files_metadata=False
                        )
                        sib_names = sorted(
                            (getattr(s, "rfilename", None) or "")
                            for s in (info.siblings or [])
                        )
                        matches = [n for n in sib_names if fnmatch.fnmatch(n, pattern)]
                        if matches:
                            print(
                                f"[usercustomize] Patch 4 shard-resolve: "
                                f"{repo_id}/{pattern} → {matches[0]} "
                                f"(picked first of {len(matches)} matches)",
                                file=sys.stderr,
                            )
                            return matches[0]
                    except Exception as e:
                        print(
                            f"[usercustomize] Patch 4 shard-resolve failed "
                            f"for {repo_id}/{pattern}: {e!r}; passing pattern "
                            f"as-is (likely will fail)",
                            file=sys.stderr,
                        )
                    return pattern

                if model.source == SourceEnum.HUGGING_FACE:
                    repo = getattr(model, "huggingface_repo_id", "") or ""
                    fn_resolved = _resolve_first_shard(
                        repo,
                        getattr(model, "huggingface_filename", "") or "",
                        hf_token,
                    )
                    command.extend(["--hf-repo", repo, "--hf-file", fn_resolved])
                else:  # MODEL_SCOPE
                    command.extend(
                        [
                            "--ms-repo",
                            getattr(model, "model_scope_model_id", "") or "",
                            "--ms-file",
                            getattr(model, "model_scope_file_path", "") or "",
                        ]
                    )

                if hf_token and model.source == SourceEnum.HUGGING_FACE:
                    command.extend(["--hf-token", hf_token])

                # Filter and apply the operator's backend_parameters via the
                # upstream parser class. It already knows how to translate
                # llama-server flag aliases (--ctx-size, --np, --ctk, --ctv,
                # --fa, --no-mmap, --batch-size, --ubatch-size, etc.) into
                # gguf-parser-compatible flags AND drops llama-server-only
                # flags gguf-parser doesn't understand (--temp, --top-p,
                # --top-k, --embeddings, etc.).
                params = GGUFParserCommandMutableParameters(
                    backend_version=getattr(model, "backend_version", None) or ""
                )
                params.from_args(getattr(model, "backend_parameters", None) or [])
                params.extend_command(command)

                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=SUBPROCESS_TIMEOUT_SECS
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    raise
                if process.returncode != 0:
                    err_tail = (stderr or b"")[-300:].decode("utf-8", "replace")
                    raise RuntimeError(
                        f"gguf-parser rc={process.returncode}: {err_tail}"
                    )

                claim_obj = GGUFParserOutput.from_json(stdout.decode())
                est = claim_obj.estimate
                if not est or not est.items:
                    raise ValueError("empty estimate.items from gguf-parser")
                item = est.items[0]
                # Sum nonuma VRAM across all GPU slots reported by the parser.
                # On Strix Halo gpustack reports is_unified_memory=False (the
                # 96 GiB is BIOS-pinned, distinct from host RAM); the rest of
                # v2.1.x consumes the nonuma branch for placement decisions.
                vram_total = sum(v.nonuma for v in (item.vrams or []))
                if vram_total <= 0:
                    raise ValueError(f"non-positive vram_total: {vram_total}")

                _claim_cache[key] = (time.time(), vram_total)
                bp_summary = " ".join(getattr(model, "backend_parameters", None) or []) or "<empty>"
                print(
                    f"[usercustomize] Patch 4: gguf-parser claim for "
                    f"{getattr(model, 'name', '?')}: "
                    f"VRAM={vram_total / (1024 ** 3):.2f} GiB "
                    f"(backend_parameters: {bp_summary})",
                    file=sys.stderr,
                )
                return vram_total
            except Exception as e:
                _claim_cache[key] = (time.time(), 0)
                print(
                    f"[usercustomize] Patch 4 fallback for "
                    f"{getattr(model, 'name', '?')}: gguf-parser failed "
                    f"({e!r}); using original heuristic, caching negative "
                    f"result for {NEGATIVE_TTL_SECS}s",
                    file=sys.stderr,
                )

        return await original_estimate(model, token, workers, session)

    _pu.estimate_model_vram = patched_estimate_model_vram

    # Rebind in candidate selector modules that imported the symbol at
    # module load time — without this, modules like
    # custom_backend_resource_fit_selector would still hold the original
    # binding and the patch would be a no-op for the very path we care about.
    for module_name in (
        "gpustack.policies.candidate_selectors.custom_backend_resource_fit_selector",
        "gpustack.policies.candidate_selectors.vllm_resource_fit_selector",
        "gpustack.policies.candidate_selectors.sglang_resource_fit_selector",
        "gpustack.policies.candidate_selectors.ascend_mindie_resource_fit_selector",
        "gpustack.policies.candidate_selectors.vox_box_resource_fit_selector",
    ):
        try:
            mod = sys.modules.get(module_name)
            if mod is None:
                __import__(module_name)
                mod = sys.modules.get(module_name)
            if mod and hasattr(mod, "estimate_model_vram"):
                mod.estimate_model_vram = patched_estimate_model_vram
        except Exception:
            pass

    print(
        f"[usercustomize] Patch 4 active: estimate_model_vram delegates to "
        f"gpustack's bundled gguf-parser for HF/MS GGUF models on "
        f"{sorted(OPTIMISTIC_BACKENDS)} — restores v0.7.1 behavior where "
        f"backend_parameters (--ctx-size, --parallel, --cache-type-k|v, "
        f"--gpu-layers) drive the scheduler placement claim. Process-"
        f"lifetime cache + per-key lock; negative TTL {NEGATIVE_TTL_SECS}s.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Install all patches
# ---------------------------------------------------------------------------

_install_scheduler_patch()
_install_weight_size_patch()
_install_gpu_util_patch()
_install_estimator_v07_compat_patch()
