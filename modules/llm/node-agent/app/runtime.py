# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Live node runtime wiring (#254 node-runtime-wiring track).

Turns the endpoint skeleton into a running node: builds a real docker client
(through the stack's SCOPED docker-socket-proxy, never a raw socket), sets the
hardware class + GPU probe, stands up the EngineSupervisor, and runs a
background loop that (a) ticks the supervisor (bounded-backoff restarts) and
(b) periodically reports its live instances to the manager (self-registration).

FAIL-SAFE + TEST-SAFE: the whole runtime is gated behind
``LLM_WORKER_AGENT_RUNTIME=1`` (set only in the compose), so importing the app or
building it under TestClient NEVER connects docker or spawns a thread. All
heavy imports (docker, httpx) are lazy — ``import app`` stays clean off-box.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from typing import Optional

logger = logging.getLogger("node_agent.runtime")

# hardware class → the engine a driver launches there.
# #1518 (E5): llama.cpp everywhere — GGUF is the fleet's one model
# architecture, and a CUDA box runs the same llama-server as an AMD one, only
# compiled for its GPU (#1516/#1517).
# #1517 rev-B: no `cuda-gb10` row. `engine_for` normalizes first, so every
# NVIDIA dialect folds onto `cuda` before the lookup — the entry was
# unreachable, and an unreachable row is where a future divergence hides.
_ENGINE_BY_HARDWARE = {"amd": "llamacpp", "cuda": "llamacpp", "cpu": "llamacpp"}


def engine_for(hardware: str | None) -> str:
    # normalize_hardware maps the fleet-facing ``nvidia`` token onto the ``cuda``
    # driver class this map is keyed on (see drivers/base.py), so an NVIDIA box
    # (HARDWARE=nvidia) reports its real engine instead of "unknown".
    from app.drivers.base import normalize_hardware
    return _ENGINE_BY_HARDWARE.get(normalize_hardware(hardware), "unknown")


def node_engine(hardware: str | None) -> str:
    """The engine this node ADVERTISES + runs, used for the worker registration
    label, the version probe, and driver selection.

    Every class serves GGUF through llama.cpp since #1518, so the per-hardware
    default (``engine_for``) is the answer on a shipped box.
    ``RAZZFAZZ_NODE_ENGINE`` stays as the escape hatch for a box running
    something we do not ship, and it is what a pre-#1518 NVIDIA node used to
    declare ``llamacpp`` with. Empty/unset → the per-hardware default."""
    override = os.environ.get("RAZZFAZZ_NODE_ENGINE", "").strip().lower()
    return override or engine_for(hardware)


def _env(name: str, default=None):
    return os.environ.get(name, default)


#: Per-instance engine /health probe timeout, seconds (NODE-16).
#:
#: ``supervisor.tick`` probes each supervised instance IN TURN, on the node's one
#: report loop, before the registration POST runs. At the old fixed 5 s, N
#: instances wedged in a state where the probe TIMES OUT (rather than refusing
#: fast) cost 5·N seconds of a cycle — against the manager's 90 s staleness
#: window that eats the margin the whole #364 background-runner work exists to
#: protect. 2 s is still an eternity for a local-bridge HTTP GET to an engine
#: that is up, and it quarters the worst case.
DEFAULT_HEALTH_PROBE_TIMEOUT = 2.0


def _positive_env_number(name: str, default, cast=float):
    """A positive numeric env knob, or ``default``.

    Empty/whitespace, non-numeric and non-positive all fall back. The EMPTY case
    is not hypothetical politeness: the compose ``environment:`` block is how
    these reach the process, `VAR: "${VAR:-}"` sets the EMPTY STRING, and
    ``float("")`` raises — so an empty-defaulted pass-through would crash
    ``configure_runtime`` at startup on every node (the trap documented in
    tests/unit/consistency/test_llm_env_documented.py). Non-positive falls back
    too: 0 does not mean "no limit" for any of these windows, it means the
    behaviour they exist to prevent."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = cast(raw)
    except ValueError:
        return default
    return val if val > 0 else default


def health_probe_timeout() -> float:
    """The engine health-probe timeout, env-tunable (``RZFZ_ENGINE_PROBE_TIMEOUT``).
    A non-numeric or non-positive value falls back to the default rather than
    disabling the timeout — an unbounded probe would hang the report loop."""
    return _positive_env_number("RZFZ_ENGINE_PROBE_TIMEOUT", DEFAULT_HEALTH_PROBE_TIMEOUT)


#: EngineSupervisor first-boot readiness window, seconds (#971).
#:
#: How long an engine may take to answer /health after launch before the
#: supervisor calls the load failed and restarts it. The old hardcoded 120 s was
#: sized for a small llama.cpp engine. A large model on the CUDA/vLLM path loads
#: its weights in seconds and then spends MINUTES in vLLM's vision-encoder +
#: KV-cache profiling and (non-eager) CUDA-graph capture before /health ever
#: answers — so 120 s killed a 35B multimodal FP8 engine mid-profiling and
#: looped forever. Not a crash: exit 0, no OOM, no traceback, just a
#: premature-kill loop in which the model never serves. 300 s clears that boot
#: with margin; a box with an even larger model raises
#: ``RZFZ_ENGINE_READINESS_GRACE``.
DEFAULT_ENGINE_READINESS_GRACE = 300.0
#: #316 self-heal cooldown before a circuit-broken engine is half-opened.
DEFAULT_ENGINE_RECOVER_AFTER = 180.0
#: #316 bound on those self-heal attempts.
DEFAULT_ENGINE_MAX_RECOVER_ATTEMPTS = 3


def engine_readiness_grace() -> float:
    """First-boot readiness window in seconds (``RZFZ_ENGINE_READINESS_GRACE``).

    Zero/negative falls back rather than being honoured: a zero grace window
    reinstates exactly the premature-restart loop this knob exists to end."""
    return _positive_env_number("RZFZ_ENGINE_READINESS_GRACE",
                                DEFAULT_ENGINE_READINESS_GRACE)


def engine_recover_after() -> float:
    """Self-heal cooldown in seconds (``RZFZ_ENGINE_RECOVER_AFTER``)."""
    return _positive_env_number("RZFZ_ENGINE_RECOVER_AFTER",
                                DEFAULT_ENGINE_RECOVER_AFTER)


def engine_max_recover_attempts() -> int:
    """Bound on self-heal attempts (``RZFZ_ENGINE_MAX_RECOVER_ATTEMPTS``)."""
    return _positive_env_number("RZFZ_ENGINE_MAX_RECOVER_ATTEMPTS",
                                DEFAULT_ENGINE_MAX_RECOVER_ATTEMPTS, cast=int)


def amd_gpu_probe() -> dict:
    """Best-effort AMD probe. available=True when the ROCm/Vulkan device nodes
    are present; enriched with rocm-smi VRAM if the tool is available. Never
    raises (a probe failure must not take the endpoint down)."""
    info: dict = {"vendor": "amd", "available": False, "devices": []}
    try:
        info["available"] = os.path.exists("/dev/kfd") and os.path.isdir("/dev/dri")
    except OSError:  # pragma: no cover - defensive
        return info
    try:
        import json
        import shutil
        import subprocess

        smi = shutil.which("rocm-smi") or shutil.which("amd-smi")
        if smi:
            out = subprocess.run(
                [smi, "--showmeminfo", "vram", "--json"],
                capture_output=True, text=True, timeout=8,
            )
            if out.returncode == 0 and out.stdout.strip():
                info["devices"] = list(json.loads(out.stdout).keys())
    except Exception:  # pragma: no cover - best-effort enrichment only
        pass
    return info


#: /proc/meminfo, as a module constant so the probes can be pinned by tests.
MEMINFO_PATH = "/proc/meminfo"


def _live_metrics() -> dict:
    """Best-effort LIVE utilization for the console dashboard (never raises).
    The console load graph plots four series as %: GPU load (``gpu_util``),
    VRAM (``vram_used_gb``/vram_total), CPU RAM (``mem_used_gb``/mem_total), and
    CPU load (``load``/``ncpu``). All sysfs/proc reads, so they work in-container
    without rocm-smi; any that can't be read are simply omitted."""
    out: dict = {}
    # #2141: on a CPU worker the GPU series are not this worker's. amdgpu sysfs
    # belongs to the host's iGPU (or the UMA carve-out), and reporting it under
    # hardware=cpu fed the admission gate a phantom "used" figure (2.2 GB on a
    # 0.5 GB "total", QA's seqitux-002). RAM and load are still reported below.
    from app.drivers.base import normalize_hardware
    cpu_worker = normalize_hardware(os.environ.get("HARDWARE")) == "cpu"
    try:
        # #1952: one reader, not two. This block used to glob mem_info_vram_used
        # itself, so the carve-out-only bug had to be fixed in two places or it
        # crawled back in through whichever reader was missed — and the report
        # lets THIS one win over _vram_used_gb().
        used = None if cpu_worker else _vram_used_gb()
        if used is not None:
            out["vram_used_gb"] = used
    except Exception:  # pragma: no cover - best-effort
        pass
    try:
        import glob
        # amdgpu exposes engine busy % per card; take the busiest card as GPU load.
        busiest = None
        for f in ([] if cpu_worker else glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
            try:
                with open(f) as fh:
                    pct = int(fh.read().strip())
                busiest = pct if busiest is None else max(busiest, pct)
            except (OSError, ValueError):  # pragma: no cover - per-card best-effort
                pass
        if busiest is not None:
            out["gpu_util"] = busiest
    except Exception:  # pragma: no cover - best-effort
        pass
    # NVIDIA: amdgpu sysfs (mem_info_vram_used / gpu_busy_percent) does not exist
    # on the CUDA path, so the two blocks above yield nothing. nvidia-smi is the
    # portable source for GPU load + VRAM there; it is present in-container when
    # the worker-agent runs with the nvidia runtime (compose.devices.nvidia.yml).
    # Only queried to fill gaps sysfs left, so an AMD box never pays for it.
    # #1456: GB10 / DGX-Spark class boxes answer memory.used with "[N/A]" —
    # unified memory, no VRAM accounting. Remembered here so the meminfo block
    # below can supply the figure instead.
    # #1990: ask the PLATFORM, not the toolbox. This used to be set only inside
    # the nvidia-smi branch below, from an "[N/A]" answer — so on a node without
    # nvidia-smi the condition could never be observed, and the worker-agent runs
    # without the NVIDIA runtime by design (the engines get it; the reporter does
    # not need GPU access to read /proc/meminfo). Measured on .191: the manager
    # saw vram_used_gb None and admission_free_now_gb None on a healthy worker.
    # The nvidia-smi path still sets it too — that answer is authoritative where
    # the tool exists, and this is the same conclusion reached without it.
    from app.drivers.base import is_unified_memory
    uma_memory = is_unified_memory(os.environ.get("HARDWARE"))
    if not cpu_worker and ("gpu_util" not in out or "vram_used_gb" not in out):
        try:
            import shutil
            import subprocess
            smi = shutil.which("nvidia-smi")
            if smi:
                r = subprocess.run(
                    [smi, "--query-gpu=utilization.gpu,memory.used",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=8,
                )
                if r.returncode == 0 and r.stdout.strip():
                    utils, used_mib = [], 0
                    for line in r.stdout.strip().splitlines():
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) < 2:
                            continue
                        try:
                            utils.append(int(float(parts[0])))
                        except ValueError:
                            pass
                        try:
                            used_mib += int(float(parts[1]))
                        except ValueError:
                            if "N/A" in parts[1].upper():
                                uma_memory = True
                    if utils and "gpu_util" not in out:
                        out["gpu_util"] = max(utils)  # busiest GPU, matches amdgpu
                    if used_mib and "vram_used_gb" not in out:
                        out["vram_used_gb"] = round(used_mib / 1024, 1)
        except Exception:  # pragma: no cover - best-effort
            pass
    try:
        with open("/proc/loadavg") as fh:
            out["load"] = round(float(fh.read().split()[0]), 2)
    except Exception:  # pragma: no cover - best-effort
        pass
    try:
        meminfo: dict = {}
        with open(MEMINFO_PATH) as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                meminfo[k] = int(rest.split()[0])  # kB
        total_kb = meminfo.get("MemTotal")
        avail_kb = meminfo.get("MemAvailable")
        if total_kb and avail_kb is not None:
            out["mem_used_gb"] = round((total_kb - avail_kb) / 1024 / 1024, 1)
            if uma_memory and "vram_used_gb" not in out:
                # #1456: on unified memory the model memory IS host memory.
                # MemTotal − MemAvailable is where vLLM's pre-allocation shows
                # up (GB10 zgx-684f: 51.8 GB used under a 0.35 reservation of
                # 121.6 GB, container RSS only 3.6 GiB). Reported as
                # vram_used_gb so the #227 admission — on its vram basis via
                # LLM_WORKER_MEM_BUDGET_GB — finally sees usage instead of None.
                out["vram_used_gb"] = out["mem_used_gb"]
    except Exception:  # pragma: no cover - best-effort
        pass
    try:
        out["ncpu"] = os.cpu_count() or 1
    except Exception:  # pragma: no cover - best-effort
        pass
    return out


def _host_mem_gb():
    """Total host RAM in GB from /proc/meminfo (best-effort, None on failure).
    For CPU workers this IS the fit budget; for GPU workers it's the fallback
    when a VRAM probe isn't available. Never raises."""
    try:
        with open(MEMINFO_PATH) as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)  # kB → GB
    except Exception:  # pragma: no cover - best-effort
        return None
    return None


def _sysfs_vram_bytes(name: str):
    """Sum ``/sys/class/drm/card*/device/<name>`` across cards; None when no
    card exposes it (CUDA/CPU boxes — the amdgpu driver owns these files)."""
    import glob
    total, found = 0, False
    for f in glob.glob(f"/sys/class/drm/card*/device/{name}"):
        try:
            total += int(open(f).read().strip())
            found = True
        except (OSError, ValueError):
            continue
    return total if found else None


def _vram_used_gb():
    """#330 stage 2: CURRENT GPU-held memory across all cards, read fresh on
    every report (never cached — the whole point is a live figure). Measured on
    gfx1151 (0.91, 2026-08-24): aggregates BOTH control planes (GPUStack +
    our engines), tracks load/unload exactly (2.2 -> 58.7 -> 75.2 -> 2.2 GiB)
    and is stable under inference (KV cache pre-allocated). None = unknown
    (no amdgpu sysfs); the manager treats that as "no dynamic signal", never
    as zero.

    #1952: the carve-out is only half the story. amdgpu splits GPU memory into
    the BIOS-pinned frame buffer (``mem_info_vram_used``) and GTT, pages lent
    from host RAM (``mem_info_gtt_used``); a buffer object lives in exactly one
    of them, so GPU-held memory is the SUM. Reading the carve-out alone made
    this figure a CONSTANT on a small-carve-out box — 0.175 (Minisforum MS-S1
    MAX, 2.0 GB carve-out / 123.5 GB GTT) reported 1.9 GB while holding 55.9 GB
    of weights, and the admission gate subtracted that frozen number forever.
    The sum is right on both fleet layouts and additionally catches the spill
    past a large carve-out, which was invisible before."""
    carveout = _sysfs_vram_bytes("mem_info_vram_used")
    gtt = _sysfs_vram_bytes("mem_info_gtt_used")
    if carveout is None and gtt is None:
        return None        # not an amdgpu box: no signal, and 0.0 would lie
    return round(((carveout or 0) + (gtt or 0)) / 1e9, 1)


def _vram_total_gb(hardware: str | None):
    """Best-effort total VRAM in GB for the fits-check budget. AMD → rocm/amd-smi;
    CUDA → nvidia-smi. None when no tool / not a GPU box (caller falls back to
    host RAM). On unified-memory boxes (Strix Halo) this is the HONEST ceiling —
    host /proc/meminfo under-reports because the BIOS pins VRAM out of RAM.
    Never raises."""
    # Operator override: on unified-memory boxes (Strix Halo) the auto-probe is
    # unreliable and the node container often lacks rocm-smi — let the box declare
    # its real model-memory budget via LLM_WORKER_MEM_BUDGET_GB.
    override = os.environ.get("LLM_WORKER_MEM_BUDGET_GB")
    if override:
        try:
            return round(float(override), 1)
        except ValueError:
            pass
    # #969: normalize, do NOT just lowercase. The fleet sets HARDWARE=nvidia on
    # a real NVIDIA box (it also selects llm/compose.devices.nvidia.yml), while
    # this probe's branch — like the driver registry and the engine map — is
    # keyed on the runtime class ``cuda``. Comparing the raw string meant the
    # nvidia-smi branch never ran on any actual NVIDIA box: the function fell
    # through to the amdgpu sysfs glob, which a CUDA box does not have, and
    # returned None. None is not a harmless "unknown" here — it is the #227/#295
    # admission budget going inert, so the fits-check silently stops refusing
    # placements it cannot honour. Same shape as the #330 stage-2 finding
    # ("hardware string did not equal amd → gate silently inert"), on the CUDA
    # path this time. normalize_hardware also strips/lowercases, so every token
    # that reached the sysfs fallback before still does (see #330's rule below).
    from app.drivers.base import is_cuda_class, normalize_hardware
    hw = normalize_hardware(hardware)
    # #2141 (Jira RZFZAI-1950): a CPU worker has NO VRAM budget — its engines
    # run on host RAM (mem_total_gb is the manager's basis). The "try sysfs for
    # ANY hardware string" fallback below made a CPU-classified box report
    # whatever amdgpu the HOST has: an iGPU's 0.5 GB BIOS carve-out on QA's
    # seqitux-002 (with 2.2 GB "used" via GTT — used > total), the Strix UMA's
    # 103.1 GB on 0.91. The admission gate then refused every model on the
    # first box and admitted everything on the second. Same defect, two faces.
    if hw == "cpu":
        return None
    try:
        import json
        import shutil
        import subprocess

        if hw == "amd":
            smi = shutil.which("rocm-smi") or shutil.which("amd-smi")
            if smi:
                out = subprocess.run([smi, "--showmeminfo", "vram", "--json"],
                                     capture_output=True, text=True, timeout=8)
                if out.returncode == 0 and out.stdout.strip():
                    data = json.loads(out.stdout)
                    total = 0
                    for card in data.values():
                        for k, v in (card or {}).items():
                            if "VRAM Total Memory" in k:
                                total += int(str(v).strip())
                    if total:
                        return round(total / 1e9, 1)
            # sysfs fallback — works WITHOUT rocm-smi (the node container usually
            # lacks it). On Strix Halo this reports the BIOS-pinned GPU VRAM
            # (e.g. 96 GB), the honest fit budget that /proc/meminfo can't see.
            b = _sysfs_vram_bytes("mem_info_vram_total")
            if b:
                return round(b / 1e9, 1)
        if is_cuda_class(hw):
            smi = shutil.which("nvidia-smi")
            if not smi:
                return None
            out = subprocess.run(
                [smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=8)
            if out.returncode != 0 or not out.stdout.strip():
                return None
            mib = sum(int(x) for x in out.stdout.split() if x.strip().isdigit())
            return round(mib / 1024, 1) if mib else None
        # #330 stage-2 side-finding: on 0.91 ALL THREE workers reported
        # vram_total_gb=None — the hardware string did not equal "amd", so the
        # probe never reached the sysfs glob even though sysfs knew the 96-GiB
        # carveout, and the admission gate was silently inert (budget=None).
        # Try sysfs for ANY hardware string: only amdgpu exposes these files,
        # so CUDA/CPU boxes fall through to None exactly as before.
        b = _sysfs_vram_bytes("mem_info_vram_total")
        if b:
            return round(b / 1e9, 1)
    except Exception:  # pragma: no cover - best-effort
        return None
    return None


def _git_describe(stack_root: str | os.PathLike | None) -> str | None:
    """Best-effort ``git describe --tags --always`` inside ``stack_root``.
    Returns None (never raises) when there is no repo mounted there (e.g. a
    thin remote node, or a master node without the optional STACK_ROOT
    bind-mount) — the caller falls back to the plain env var in that case.

    ``-c safe.directory=<root>`` (#817): the repo arrives as a read-only bind
    from the host, owned by the operator's uid, while this process runs as root
    inside the container. Git >= 2.35.2 then refuses with "detected dubious
    ownership" and exits 128, which this function reads as "no repo" — the
    stamp would silently keep showing the stale env version on every properly
    mounted box. The command-line config scope is *protected*, so a repo-local
    ``.git/config`` cannot forge this; it only says "I mounted this on purpose".
    """
    if not stack_root:
        return None
    try:
        import subprocess

        out = subprocess.run(
            ["git", "-c", f"safe.directory={stack_root}",
             "describe", "--tags", "--always"],
            cwd=str(stack_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:  # pragma: no cover - best-effort
        return None
    return None


def resolve_stack_version(env_version: str | None, stack_root=None, *, describe_fn=None) -> str | None:
    """#291: the worker-detail "Stack version" the console shows must reflect
    what is ACTUALLY running, not a stale ``.env`` value. ``RAZZFAZZ_VERSION``
    is only rewritten by the install/upgrade scripts — on a long-lived dev
    branch (e.g. working #254 off the last GA tag) it silently keeps showing
    the last release tag while HEAD has moved on for weeks.

    ``git describe --tags`` tells the two cases apart:
      * HEAD sits exactly on a tag  -> describe == the tag itself (a release
        build) -> keep the env value (the GA string; describe would say the
        same thing, but the env value is the one already threaded through
        registration/labels).
      * HEAD sits N commits past the last tag (or there's no reachable tag at
        all, e.g. a shallow clone) -> describe carries a ``-N-g<hash>`` suffix
        (or is a bare short hash) -> a DEV/unreleased build; show that instead
        of the stale GA string so the console reflects the real commit.

    ``describe_fn`` is injectable for tests (stub it instead of touching a
    real git checkout); production wiring passes the default, which no-ops
    (returns None, falling back to ``env_version`` unchanged) when no repo is
    mounted at ``stack_root`` — never raises, never blocks registration.
    """
    return resolve_stack_version_detail(
        env_version, stack_root, describe_fn=describe_fn)["version"]


#: Where a reported version came from. A CODE, like `ENGINE_VERSION_WHY`
#: (#1932) and `switch_interruption`'s `why` (#1926), and for the same reason:
#: the value alone cannot say whether it is trustworthy.
#:
#:   git-describe  read from the mounted repo — what is ACTUALLY running
#:   env           RAZZFAZZ_VERSION, on a release build where describe agrees
#:   image         stamped into this container at BUILD time. The thin-node
#:                 answer: no repo to read, so the image says what it is.
#:   unknown       nothing could be established. Distinct from every value
#:                 above, and the state #1932 was filed about.
STACK_VERSION_SOURCE = ("git-describe", "env", "image", "unknown")


def _image_version() -> str | None:
    """The version stamped into this image at build time (#1932).

    `unknown` is treated as no answer: the Dockerfile uses it as the default
    when the builder passed nothing, and a literal "unknown" in the console
    would be a value pretending to be one.
    """
    val = (os.environ.get("RAZZFAZZ_IMAGE_VERSION") or "").strip()
    return val or None if val != "unknown" else None


def resolve_stack_version_detail(env_version: str | None, stack_root=None, *,
                                 describe_fn=None, image_version_fn=None) -> dict:
    """→ ``{"version": str|None, "source": <one of STACK_VERSION_SOURCE>}``.

    #1932, operator decision 2026-09-10: a node WITHOUT a repo reports its own
    IMAGE version rather than a stack version it cannot see. That was chosen
    over adding `RAZZFAZZ_VERSION` to `config/node.env.example` because an
    `.env` number goes stale on the next upgrade — and the question this field
    exists to answer is "is the fix on this node?". A value that becomes wrong
    at exactly the moment the question is asked is worse than none.

    The ORDER is deliberate and unchanged where a repo exists: a mounted repo
    still wins, because `git describe` describes what is running, while the
    image version describes what was shipped. On a full node those differ the
    moment someone works on a branch — the whole point of #291.
    """
    describe = (describe_fn or _git_describe)(stack_root)
    if describe:
        # exact tag match ("2026.08-ga.11") has no "-N-g<hash>" dev suffix; a
        # dev build does (git describe's own format), or --always fell back to a
        # bare short hash with no tag reachable at all (also treat as dev).
        import re

        is_dev = bool(re.search(r"-\d+-g[0-9a-f]+$", describe)) or bool(
            re.fullmatch(r"[0-9a-f]{7,40}", describe)
        )
        if is_dev:
            return {"version": describe, "source": "git-describe"}
        # HEAD sits exactly on a tag: describe would say the same thing as the
        # env value, and the env value is the one already threaded through
        # registration/labels. Reported as `env` because that is the string
        # being sent — not as `git-describe`, which would overstate it.
        if env_version:
            return {"version": env_version, "source": "env"}
        return {"version": describe, "source": "git-describe"}
    if env_version:
        return {"version": env_version, "source": "env"}
    # No repo and no env value — the thin node. THIS is the case #1932 measured.
    img = (image_version_fn or _image_version)()
    if img:
        return {"version": img, "source": "image"}
    return {"version": None, "source": "unknown"}


def _probe_engine_version(hardware: str | None, docker_client):
    """→ ``{"version": str|None, "why": <code>}`` for this node's engine image.

    Only llamacpp engines (amd/cpu) expose ``llama-server --version``; others
    (vLLM) have no such string. Never raises.

    #1932: the answer now carries WHY it is missing. It used to be the string
    or `None`, and `None` was the answer for "this engine has no version
    string", "no image configured", "the image is not here", "docker is
    unreachable" and "upstream changed its output" alike — five states, one
    empty cell, five different actions.
    """
    try:
        if node_engine(hardware) != "llamacpp":
            return {"version": None, "why": "not-applicable"}
        from app.drivers import select_driver
        from app.drivers.base import probe_engine_version_detail

        image = getattr(select_driver(hardware, engine=node_engine(hardware)), "image", None)
        if not image:
            return {"version": None, "why": "no-image"}
        # #1654: `or`, not a get() default — compose sets this key to EMPTY
        # ("${VAR:-}"), and an empty value beats a get() default.
        binary = os.environ.get("RAZZFAZZ_LLAMACPP_BINARY") or "llama-server"
        return probe_engine_version_detail(image, docker_client, binary=binary)
    except Exception:  # pragma: no cover - best-effort
        # An unexpected failure in the SELECTION (not in the probe) is still a
        # failed probe from the manager's point of view, and it must not be
        # reported as "this engine has no version".
        return {"version": None, "why": "probe-failed"}


def make_docker_client(docker_host: str | None):
    """Real docker client via DOCKER_HOST (the scoped socket-proxy). Lazy
    import so `import app` doesn't need the docker SDK off-box."""
    import docker

    if docker_host:
        return docker.DockerClient(base_url=docker_host)
    return docker.from_env()


def _worker_id_from(resp):
    """Pull worker_id out of a registration response (httpx response or a plain
    dict from a test fake). None if absent."""
    try:
        if hasattr(resp, "json"):
            return (resp.json() or {}).get("worker_id")
        if isinstance(resp, dict):
            return resp.get("worker_id")
    except Exception:  # pragma: no cover - defensive
        return None
    return None


# #286: supervisor state → the phase we report to the manager. A just-launched
# instance is "loading", NOT "ready"; a circuit-broken one is "failed".
_PHASE_BY_STATE = {"loading": "loading", "backing_off": "restarting",
                   "ready": "ready", "failed": "failed"}


def _readopt_engines(app, docker, supervisor) -> int:
    """#293: re-discover engine containers this node created (label
    rzfz.role=llm-engine) and re-populate `loaded` + health-monitor them, so a
    node restart never orphans a running engine or lets the manager prune its
    instance. Returns the count re-adopted."""
    from app.drivers.base import LaunchSpec, files_from_label

    try:
        # #342: `all=True`. docker-py's containers.list() defaults to
        # running-only, so after a reboot the engine containers exist but are
        # exited and re-adopt found ZERO — the fleet came back empty and silent.
        # With the restart policy (drivers/base.ENGINE_RESTART_POLICY) they
        # should be running again by now; listing all of them means a container
        # the daemon could NOT restart is still seen and reported as failed
        # rather than vanishing. A visible failure beats a silent loss.
        conts = docker.containers.list(all=True,
                                       filters={"label": "rzfz.role=llm-engine"})
    except Exception:  # pragma: no cover - defensive (socket-proxy may lack list)
        logger.warning("re-adopt: cannot list engine containers", exc_info=True)
        return 0
    loaded = app.state.loaded
    n = 0
    for c in conts or []:
        lbl = getattr(c, "labels", {}) or {}
        name = getattr(c, "name", None) or lbl.get("rzfz.instance")
        if not name or name in loaded:
            continue
        serve = f"http://{name}:8080/v1"
        health = f"http://{name}:8080/health"
        engine = lbl.get("rzfz.engine", "llamacpp")
        # NB an exited container is adopted here too, and that is DELIBERATE
        # (#293): it is handed to the supervisor, whose first tick marks it
        # `failed` without attempting a re-create. Special-casing it here would
        # duplicate that decision in a second place and break the single path
        # the existing tests pin.
        loaded[name] = {
            "model": lbl.get("rzfz.model", name), "endpoint": serve,
            "health_url": health, "engine": engine,
            "task": lbl.get("rzfz.task", "chat"),
            # NODE-9: restore the weight list the launch stamped on the
            # container, so delete_disk_model's in-use guard is not blind for a
            # re-adopted engine. A container launched by a pre-NODE-9 agent has
            # no such label → [] (unchanged, still-blind behaviour for THAT
            # container only, until it is next redeployed).
            "files": files_from_label(lbl.get("rzfz.files")),
            "launched": True, "status": "loading", "readopted": True,
        }
        try:
            supervisor.readopt(
                LaunchSpec(engine=engine, image="", name=name, command=[],
                           serve_url=serve, health_url=health),
                now=_now_runtime())
        except Exception:  # pragma: no cover - defensive
            logger.exception("re-adopt supervise failed for %s", name)
        n += 1
    if n:
        logger.info("re-adopted %d running engine(s) after restart", n)
    return n


def _now_runtime() -> float:
    import time

    return time.monotonic()


def _sync_instance_status(app, supervisor) -> None:
    """Stamp each loaded instance with the supervisor's REAL state so
    build_registration reports the truth instead of defaulting to ready (#286)."""
    loaded = getattr(app.state, "loaded", None)
    if not loaded or supervisor is None:
        return
    try:
        states = supervisor.states()
    except Exception:  # pragma: no cover - defensive
        return
    # #708 — the REASON, alongside the state. `failed` on its own tells an
    # operator that something is wrong and nothing about what; the engine's own
    # last output usually says it outright (`missing tensor
    # blk.64.ssm_conv1d.weight`, an OOM kill, a bad --ctx-size). Best-effort:
    # an older supervisor without this method must not break the report cycle.
    try:
        errors = supervisor.last_errors()
    except AttributeError:
        errors = {}
    except Exception:  # pragma: no cover - defensive
        errors = {}

    for name, rec in loaded.items():
        # Only sync instances the supervisor OWNS. A pre-launch instance (still
        # "pulling" weights, or "failed" with no source) isn't supervised yet —
        # leave its status alone so the pull thread's "pulling %" / "failed"
        # isn't clobbered back to "loading" (#287 × #286 interaction).
        if isinstance(rec, dict) and name in states:
            rec["status"] = _PHASE_BY_STATE.get(states[name], "loading")
            # Attach only where it means something. A `ready` instance carrying
            # the text of a failure it recovered from reads as broken.
            if rec["status"] in ("failed", "restarting") and errors.get(name):
                rec["last_error"] = errors[name]
            else:
                rec.pop("last_error", None)



class CommandQueueFull(Exception):
    """The background command queue is at its cap (NODE-15)."""


class AsyncCommandRunner:
    """#364 a single background consumer for long-running node commands.

    ONE consumer, not a thread per command, and that is deliberate: two 40 GB
    mirrors running at once would thrash the same disk and the same uplink and
    finish later than if they had queued. Serialising them costs nothing —
    nobody is waiting on the loop any more — and keeps the node's I/O
    predictable.

    The thread is started lazily on first use and then lives for the process. An
    idle-exit-with-timeout variant races with `submit` (the thread can decide to
    exit between the liveness check and the put), and the fix for that race is a
    lock held across the put — at which point keeping one parked daemon thread
    blocked on `Queue.get()` is simpler and cheaper than the machinery to avoid
    it.

    NODE-15: the SERIALISATION is deliberate; the old absence of a CAP was not.
    An unbounded queue lets a manager enqueue mirrors faster than the node can
    drain them — memory grows without limit and every one of them is reported
    back as `"running"`, so nothing anywhere shows the backlog. A bounded queue
    turns that into an immediate, legible `failed` for the command that does not
    fit, which the manager can retry, instead of an accepted command that sits
    behind a 40 GB mirror for an hour.
    """

    #: Default depth. Generous next to any realistic operator batch (a fleet-wide
    #: mirror of every catalog model is single digits per node) and small enough
    #: that a runaway producer is refused within a cycle. Env-tunable like every
    #: other node knob.
    DEFAULT_MAXSIZE = 32

    def __init__(self, name: str = "worker-agent-commands", maxsize: int | None = None) -> None:
        if maxsize is None:
            try:
                maxsize = int(os.environ.get("RZFZ_NODE_COMMAND_QUEUE_MAX", "")
                              or self.DEFAULT_MAXSIZE)
            except ValueError:
                maxsize = self.DEFAULT_MAXSIZE
        self._maxsize = max(1, int(maxsize))
        self._q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        self._name = name
        self._lock = threading.Lock()
        self._started = False

    def submit(self, job) -> None:
        """Enqueue ``job`` for the background consumer.

        Raises ``CommandQueueFull`` when the queue is at its cap — the caller
        turns that into a reported failure rather than blocking the report loop
        (blocking is the very thing #364 removed)."""
        with self._lock:
            if not self._started:
                threading.Thread(target=self._drain, name=self._name,
                                 daemon=True).start()
                self._started = True
        try:
            self._q.put_nowait(job)
        except queue.Full:
            raise CommandQueueFull(
                f"node command queue is full ({self._maxsize} queued long "
                f"commands) — retry once the backlog drains") from None

    def maxsize(self) -> int:
        """The configured cap, for the report payload and for tests."""
        return self._maxsize

    def _drain(self) -> None:
        while True:
            job = self._q.get()
            try:
                job()
            except Exception:  # pragma: no cover - defensive
                logger.exception("background command failed")
            finally:
                self._q.task_done()

    def pending(self) -> int:
        """Queue depth, for the report payload and for tests."""
        return self._q.qsize()


#: #1250a — how often the registration-rejection warning may repeat (seconds).
REGISTRATION_REJECT_WARN_INTERVAL = 60.0


def warn_on_registration_rejection(app, resp, *, now, node_name, manager_url) -> bool:
    """#1250a: a 401 on POST /api/workers used to be entirely SILENT.

    ``register_with_manager`` returns the response and the caller only mines it
    for a worker id, so a rejected registration looked exactly like an accepted
    one: the agent kept reporting into a manager that refused it on every cycle,
    the fleet stayed empty, no model could be placed, and the agent log said
    nothing at all. Measured on 0.91 (#1250): the agent ran as ``master`` while
    its key had been minted for the box hostname.

    The manager validates the presented credential as
    ``HMAC(node_key, worker_name)``, so a rejection is overwhelmingly a
    name/key mismatch — which means the NAME is the diagnostic. Log it together
    with the manager URL, never the key, and at most once per minute so a 30 s
    report loop cannot flood the log.

    Returns True when a warning was emitted (for tests)."""
    status = getattr(resp, "status_code", None)
    if status not in (401, 403):
        return False
    last = getattr(app.state, "_registration_reject_warned_at", None)
    if last is not None and (now - last) < REGISTRATION_REJECT_WARN_INTERVAL:
        return False
    app.state._registration_reject_warned_at = now
    logger.warning(
        "registration REJECTED by the manager (HTTP %s): manager=%s worker name=%r. "
        "The per-worker command key is HMAC(node_key, worker name), so this name "
        "almost certainly differs from the name the key was enrolled for — compare "
        "LLM_WORKER_NAME in this container with LLM_WORKER_NAME in the stack .env "
        "and re-run `rzfz post-install --refresh` (#1250).",
        status, manager_url, node_name)
    return True


def run_report_cycle(app, supervisor, *, http_get, http_post, now,
                     manager_url, node_key, hardware, node_name, node_addr) -> dict:
    """One supervision-tick + registration-report + command-poll cycle. Pure
    enough to unit test with fakes. Each part is best-effort: a failure in one
    never blocks the others, and none ever raises out of here."""
    result = {"ticked": False, "reported": False}
    try:
        supervisor.tick(now, http_get=http_get)
        result["ticked"] = True
        _sync_instance_status(app, supervisor)
    except Exception:  # pragma: no cover - defensive
        logger.exception("supervisor tick failed")

    if manager_url and node_key:
        try:
            from app.registration import build_registration, register_with_manager

            worker = {"name": node_name, "address": node_addr,
                      "hardware": hardware, "engine": node_engine(hardware),
                      # version fields computed once at arm-time (configure_runtime)
                      # and stashed on app.state — reading them here keeps this
                      # function pure/test-safe (absent in tests → None).
                      "stack_version": getattr(app.state, "stack_version", None),
                      "stack_version_source": getattr(app.state, "stack_version_source", None),
                      "engine_version": getattr(app.state, "engine_version", None),
                      "engine_version_why": getattr(app.state, "engine_version_why", None),
                      # #262: host-routable base for a remote master to reach this
                      # worker's engines (LLM_WORKER_ADVERTISE_ADDR). Unset → same-box.
                      "advertise_addr": _env("LLM_WORKER_ADVERTISE_ADDR") or None,
                      # #295 fits-check budget (host RAM + best-effort VRAM).
                      "mem_total_gb": getattr(app.state, "mem_total_gb", None),
                      "vram_total_gb": getattr(app.state, "vram_total_gb", None),
                      # #330 stage 2: live reading, fresh EVERY report (both
                      # control planes aggregate here — the gfx1151 measurement).
                      "vram_used_gb": _vram_used_gb()}
            # live utilization (load, + amdgpu vram_used_gb where present) for the
            # console dashboard load graph — sampled fresh every report cycle;
            # best-effort, never blocks. Does not clobber the #330 admission
            # vram_used_gb above unless it has a fresher reading to offer.
            worker.update(_live_metrics())
            payload = build_registration(worker, dict(getattr(app.state, "loaded", {})))
            resp = register_with_manager(manager_url, node_key, payload, http_post=http_post)
            result["reported"] = True
            # #1250a: a refused registration must not look like an accepted
            # one. Not folded into `result` — callers compare that dict by
            # equality — the signal is the log line.
            warn_on_registration_rejection(
                app, resp, now=now, node_name=node_name, manager_url=manager_url)
            wid = getattr(app.state, "worker_id", None) or _worker_id_from(resp)
            if wid:
                app.state.worker_id = wid
                # #262 Task 8: arm the relay client (remote workers only) now
                # that worker_id is known. Idempotent — a no-op every cycle
                # after the first successful start.
                maybe_start_relay(app, manager_url=manager_url, node_key=node_key)
        except Exception:  # pragma: no cover - defensive
            logger.exception("registration report failed")

    # #261 control channel: claim + execute this worker's pending commands.
    # Runtime-only (needs a live docker client + known worker_id) → a no-op in
    # unit tests (SimpleNamespace app.state has neither).
    wid = getattr(app.state, "worker_id", None)
    docker = getattr(app.state, "docker_client", None)
    if wid and docker is not None and manager_url and node_key:
        try:
            _poll_commands(app, wid, docker, manager_url, node_key, http_post)
        except Exception:  # pragma: no cover - defensive
            logger.exception("command poll failed")
    return result


def fast_beat_once(app, *, manager_url, node_key, http_post) -> dict:
    """#1619: ONE lean beat — report utilisation, then claim what is queued.

    Deliberately shaped like `run_report_cycle`: "pure enough to unit test with
    fakes", never raises, and each half independent of the other. A manager that
    refuses the metrics must not stop the command claim — they fail for
    different reasons and only one of them is about the dashboard.

    WHY TWO CLOCKS MAY BOTH CLAIM COMMANDS, and this is the line that carries
    the safety of the whole split: the report cycle calls `_poll_commands` too,
    so from now on two threads can claim concurrently. That is safe because the
    CLAIM IS ATOMIC ON THE MANAGER — `api/commands.py::claim` selects
    `FOR UPDATE SKIP LOCKED` and flips `status` to `claimed` inside the same
    transaction, so a second claimer SKIPS the locked rows instead of getting
    them a second time. This is not incidental: #326 fixed exactly this bug
    (plain SELECT then UPDATE, two overlapping claims both returning the same
    row, `load_engine` executed twice and two engines racing for the same
    VRAM). Should that primitive ever be relaxed, this beat becomes a way to
    trigger it on every quiet node — an in-flight flag around `_poll_commands`
    would then be the smallest fix.

    Returns what it managed to do, so a test can assert the halves separately
    instead of inferring them from the fakes.
    """
    result = {"reported": False, "polled": False}
    wid = getattr(app.state, "worker_id", None)
    if not (wid and manager_url and node_key):
        # Not enrolled yet. Enrolment is the FULL report's job; a beat that
        # tried to register would be the very coupling this split removes.
        return result
    try:
        metrics = _live_metrics()
        if metrics:
            http_post(f"{manager_url.rstrip('/')}/api/workers/{wid}/metrics",
                      json=metrics,
                      headers={"Authorization": f"Bearer {node_key}"})
            result["reported"] = True
    except Exception:  # pragma: no cover - best-effort
        logger.debug("fast beat: metrics report failed", exc_info=True)
    docker = getattr(app.state, "docker_client", None)
    if docker is None:
        return result
    try:
        _poll_commands(app, wid, docker, manager_url, node_key, http_post)
        result["polled"] = True
    except Exception:  # pragma: no cover - best-effort
        logger.debug("fast beat: command poll failed", exc_info=True)
    return result


def _relay_url(manager_url: str, worker_id: str) -> str:
    """#262 Task 8: turn the manager's HTTP(S) base into the relay WS URL —
    `http(s)://host[:port]` -> `ws(s)://host[:port]/api/workers/<id>/relay`.
    A `manager_url` that already uses a `ws(s)://` scheme (or anything else
    unrecognised) is passed through unchanged, on the assumption the caller
    already gave us what it wanted.

    #918: the scheme match is CASE-INSENSITIVE. `LLM_WORKER_AGENT_MANAGER_URL` is
    operator-typed and a URI scheme is case-insensitive (RFC 3986 §3.1), so a
    lowercase-only `startswith` let `HTTPS://llm-manager.example` fall through
    to the pass-through branch and produced `HTTPS://…/api/workers/<id>/relay` —
    not a WS URL at all, so the dial fails and `run_client` retries forever
    while this worker never serves relayed inference. Only the SCHEME is
    normalised; host, path and worker id are left byte-for-byte alone (a host
    can matter to an exact-Host-matching proxy, and the id is a uuid the master
    parses back)."""
    lowered = manager_url.lower()
    if lowered.startswith("https://"):
        base = "wss://" + manager_url[len("https://"):]
    elif lowered.startswith("http://"):
        base = "ws://" + manager_url[len("http://"):]
    else:
        base = manager_url
    return f"{base.rstrip('/')}/api/workers/{worker_id}/relay"


def _engine_records(app) -> list:
    """Loaded instances that carry an endpoint, ready ones first."""
    loaded = getattr(app, "state", app)
    loaded = getattr(loaded, "loaded", None) or {}
    candidates = [rec for rec in loaded.values()
                  if isinstance(rec, dict) and rec.get("endpoint")]
    return ([rec for rec in candidates if rec.get("status") == "ready"]
            + [rec for rec in candidates if rec.get("status") != "ready"])


def _base_of(endpoint: str) -> str:
    """An engine endpoint (`http://inst-1:8080/v1`) as a ROOT base."""
    if endpoint.endswith("/v1"):
        endpoint = endpoint[: -len("/v1")]
    return endpoint.rstrip("/")


def model_from_request_body(body) -> Optional[str]:
    """The model an OpenAI-style request names, or ``None``.

    #1535: this is how a worker tells its own engines apart. Every request the
    master relays here was generated by LiteLLM from a router entry whose
    ``litellm_params.model`` is ``"{provider}/{served}"``, so the upstream body
    carries ``"model": "<served>"`` — chat, embeddings and rerank alike. Reading
    it costs one JSON parse of a body we already hold.

    Deliberately total: a body that is absent, binary, not JSON, or has no
    string ``model`` yields ``None`` and the caller falls back. Nothing here may
    raise — a malformed body must not take the relay down."""
    if not body:
        return None
    try:
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "replace")
        if not isinstance(body, str) or not body.lstrip().startswith("{"):
            return None
        data = json.loads(body)
    except Exception:
        return None
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) and model.strip() else None


def engine_bases_for_model(app, model: str, *, ready_only: bool = False) -> list:
    """Every engine on THIS node serving ``model`` — ready ones first.

    More than one is the REPLICA case: `PATCH replicas` places a second engine
    for the same deployment, and `_pick_worker` falls back to the same box when
    no other worker fits ("replicas may legitimately share a box"). Each gets
    its own container (`engine-<model>-<uuid6>`), and the master's router gets
    one entry per endpoint under ONE model name — but for a relay-routed worker
    both entries rewrite to the SAME `/relay/{worker_id}/v1`, so the master
    cannot tell them apart and the choice lands here.

    ``ready_only`` is what the rotation actually uses (revA finding 1): ready
    FIRST is only a tie-break, and a caller that rotates walks straight past it
    into the loading replica on the very next request."""
    recs = [rec for rec in _engine_records(app) if rec.get("model") == model]
    if ready_only:
        recs = [rec for rec in recs if rec.get("status") == "ready"]
    return [_base_of(rec["endpoint"]) for rec in recs]


def engine_base_for_model(app, model: str) -> Optional[str]:
    """The base URL of an engine serving ``model`` on THIS node, or None.

    Round-robins across the READY replicas of the same model. Returning the
    FIRST match always would leave a second replica running, holding memory,
    and never answering anything — the operator asked for two and would get the
    capacity of one, with nothing to show why.

    The rotation runs over the ready set, not over "ready ones first" (revA
    finding 1). Ordering only decides the FIRST call; from the second the
    counter walks into whatever follows, so a `loading` replica took every
    second request — minutes to hours of 502s for half the traffic of that
    model while a 35-B replica pulls its weights, and permanently after one
    died. Ordering cannot express "never", only "not first".

    A non-ready base is used ONLY when the model has no ready engine at all:
    the caller's alternative there is a refusal, and an engine that is coming
    up answers sooner than that.

    The counter lives on `app.state`; the relay serves its requests from one
    asyncio loop, so a plain int needs no lock."""
    bases = (engine_bases_for_model(app, model, ready_only=True)
             or engine_bases_for_model(app, model))
    if not bases:
        return None
    if len(bases) == 1:
        return bases[0]
    state = getattr(app, "state", app)
    counters = getattr(state, "relay_rr", None)
    if counters is None:
        counters = {}
        try:
            state.relay_rr = counters
        except Exception:      # a mapping-only stand-in: rotate statelessly
            return bases[0]
    n = counters.get(model, 0)
    counters[model] = (n + 1) % len(bases)
    return bases[n % len(bases)]


def relay_engine_base_for(app, path, body=None):  # noqa: ARG001 - path is the URL, the model is in the body
    """#1535 — THE resolver the relay installs: the worker fans out to its OWN
    engines.

    #262's decided architecture is two-tier: "the master picks a worker, the
    worker picks a local engine". The master half shipped; this is the worker
    half, and until now it was a single-engine stand-in
    (`_primary_engine_base`) with the model-in-body disambiguation marked
    DEFERRED. A remote worker was therefore capped at one model — enforced by a
    409 in the manager (#929 M1), which is the symptom, not the cause.

    A request that NAMES a model is served by that model's engine or by
    nothing. Falling back to the primary here is exactly the failure #929
    describes: the right model name, the wrong weights, and no error anywhere.
    A request that names no model (`GET /v1/models`, a probe) keeps the
    pre-#1535 behaviour.

    Module level, not a closure inside `maybe_start_relay`, so a test can reach
    the code the node actually runs — the first cut hid it in the closure and
    two mutations (fall back to primary for a named model; stop reading the
    body) stayed green against stand-ins.
    """
    model = model_from_request_body(body)
    if model:
        base = engine_base_for_model(app, model)
        if base:
            return base
        raise RuntimeError(
            f"relay: no engine on this node serves model {model!r} "
            f"(loaded: {sorted(str(r.get('model')) for r in _engine_records(app))})")
    base = _primary_engine_base(app)
    if not base:
        raise RuntimeError("relay: no ready engine to forward requests to")
    return base


def _primary_engine_base(app):
    """The node's first running engine's root base URL (e.g.
    `http://inst-1:8080`, no `/v1`). Prefers a supervisor-confirmed `ready`
    instance; falls back to any loaded instance that at least has an
    `endpoint` (a node with nothing loaded/ready yet returns None — the caller
    turns that into a per-request `err` frame rather than failing to start the
    relay itself).

    #1535: this is no longer how a relayed REQUEST is routed — it is the
    fallback for a request that names no model at all (`GET /v1/models`, a
    health probe). Picking the first ready engine for a request that DOES name
    a model is what made a second model on a remote worker answer with the
    first one's weights."""
    loaded = getattr(app.state, "loaded", None) or {}
    candidates = [rec for rec in loaded.values()
                  if isinstance(rec, dict) and rec.get("endpoint")]
    if not candidates:
        return None
    ready = [rec for rec in candidates if rec.get("status") == "ready"]
    endpoint = (ready or candidates)[0]["endpoint"]
    if endpoint.endswith("/v1"):
        endpoint = endpoint[: -len("/v1")]
    return endpoint.rstrip("/")


def maybe_start_relay(app, *, manager_url, node_key) -> None:
    """#262 Task 8: arm the ONE outbound worker->master WS inference relay —
    but ONLY for a REMOTE worker (`LLM_WORKER_ADVERTISE_ADDR` set). A
    co-located worker reaches its own engines directly (same docker network)
    and has no use for the relay hop.

    Called from `run_report_cycle` on every cycle once `worker_id` is known
    (registration may take a cycle or two) — deliberately idempotent, so
    every call after the first successful start is a no-op:
    `app.state.relay_thread` being set at all is enough to skip re-arming
    (mirrors the co-located marker check — a set-but-dead thread would only
    happen on interpreter-level chaos this process can't recover from
    anyway, and a background reconnect loop is `run_client`'s own job, not
    this function's).

    Starts exactly one daemon thread (mirroring `configure_runtime`'s own
    `worker-agent-runtime` thread, just above) running an asyncio event loop
    that drives `relay_client.run_client` against the manager's relay
    endpoint, using the node's single primary engine
    (`_primary_engine_base`) to serve requests. The default `connect` (real
    WS dial, added in Task 9) and a long-lived `httpx.AsyncClient` (no read
    timeout — relay responses can stream for as long as the model takes) are
    used.

    Guarded end-to-end: a relay-start failure must NEVER break the
    report/command cycle that calls this, so every failure path here is
    caught and logged rather than raised."""
    try:
        if not _env("LLM_WORKER_ADVERTISE_ADDR"):
            return  # co-located: no relay hop needed

        worker_id = getattr(app.state, "worker_id", None)
        if not worker_id or not manager_url or not node_key:
            return

        if getattr(app.state, "relay_thread", None) is not None:
            return  # already armed (idempotent)

        relay_url = _relay_url(manager_url, worker_id)
        stop_flag = threading.Event()

        def _engine_base_for(path, body=None):
            return relay_engine_base_for(app, path, body)

        def _run_relay() -> None:
            import asyncio

            import httpx

            from app.relay_client import run_client

            async def _main() -> None:
                loop_stop = asyncio.Event()

                async def _watch_stop_flag() -> None:
                    # Bridges the process-wide threading.Event (settable from
                    # any thread, e.g. a future shutdown hook) into the
                    # asyncio.Event run_client actually watches (must live on
                    # THIS thread's loop).
                    while not stop_flag.is_set():
                        await asyncio.sleep(0.5)
                    loop_stop.set()

                watcher = asyncio.ensure_future(_watch_stop_flag())
                try:
                    # No read timeout: a relay response streams for as long
                    # as the engine takes to generate it.
                    timeout = httpx.Timeout(10.0, read=None)
                    # #1064: this client forwards the manager's requests to the
                    # LOCAL engine (relay_client appends the path to the engine
                    # base). trust_env=False — on a proxied box (#283 puts the
                    # agent on the egress list) it must never send engine traffic
                    # to the corporate proxy; NO_PROXY cannot name engine-* (#276).
                    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                        await run_client(
                            worker_id,
                            url=relay_url,
                            node_key=node_key,
                            http_client=client,
                            engine_base_for=_engine_base_for,
                            stop=loop_stop,
                        )
                finally:
                    watcher.cancel()

            try:
                asyncio.run(_main())
            except Exception:  # pragma: no cover - defensive
                logger.exception("relay client loop exited unexpectedly")

        thread = threading.Thread(target=_run_relay, name="worker-agent-relay", daemon=True)
        thread.start()
        app.state.relay_thread = thread
        app.state.relay_stop = stop_flag
        logger.info("relay client armed: worker_id=%s url=%s", worker_id, relay_url)
    except Exception:  # pragma: no cover - defensive
        logger.exception("relay start failed (non-fatal, will retry next cycle)")


def delete_cached_weight(mount: str, name: str, loaded: dict) -> dict:
    """#306 delete half: remove one cached weight file from this worker's model
    mount, freeing disk. A pure function (no docker, no app.state) so it
    unit-tests directly against a tmp_path — the same posture as
    ``_disk``'s enumeration, applied to deletion.

    FAIL-SAFE: refuses when ``name`` backs any deployment CURRENTLY present in
    ``loaded`` (``app.state.loaded`` — ready, loading, or pulling; only
    ``_unload`` ever pops an entry) — freeing weights out from under a
    live/starting engine is data-loss-adjacent, and the whole point of the
    cache is redeploying WITHOUT a re-download.

    ``name`` MUST resolve to a path INSIDE ``mount`` — the same containment
    posture as the runner-image allow-list (#549 R2), applied to a filesystem
    path instead of an image reference. A caller-chosen ``../../etc/passwd``
    is refused before any filesystem call, never merely rejected by chance.

    NODE-10: a DIRECTORY is removable too. A #574 vLLM deployment serves
    ``models_mount/<model>/`` — tens of GB of safetensors — and the old
    ``os.path.isfile`` gate meant the operator could neither see it (``_disk``
    was ``.gguf``-only) nor free it through the fleet console: the models volume
    filled with weights that had no removal path at all. Containment and the
    in-use guard apply identically; only the removal call differs (``rmtree``).
    """
    import os
    import shutil

    if not name:
        raise ValueError("delete_disk_model requires args.name")
    mount_abs = os.path.normpath(mount)
    target = os.path.normpath(os.path.join(mount_abs, name))
    if target != mount_abs and not target.startswith(mount_abs + os.sep):
        raise ValueError(f"refusing to delete {name!r}: escapes the models mount")
    leaf = os.path.basename(name)
    rel = str(name).strip("/")
    for iid, rec in (loaded or {}).items():
        rec = rec or {}
        if leaf in (rec.get("files") or []):
            raise ValueError(
                f"refusing to delete {name!r}: in use by deployment {iid!r} "
                f"(status={rec.get('status')!r}) — unload it first")
        # NODE-9/NODE-10: a repo-dir deployment has NO flat file list by
        # contract — its weights are the directory named by the model. Match on
        # that too, else the one deployment kind whose weights are only
        # removable as a directory is also the one with no in-use guard.
        model = str(rec.get("model") or "").strip("/")
        if model and rel in (model, os.path.basename(model)):
            raise ValueError(
                f"refusing to delete {name!r}: in use by deployment {iid!r} "
                f"(status={rec.get('status')!r}, serving directory {model!r}) "
                f"— unload it first")
    if os.path.isdir(target) and not os.path.islink(target):
        size = _tree_bytes(target)
        shutil.rmtree(target)
        return {"deleted": name, "kind": "dir", "freed_bytes": size,
                "freed_gb": round(size / 1e9, 2)}
    if not os.path.isfile(target):
        raise ValueError(f"{name!r} not found on this node's model mount")
    size = os.path.getsize(target)
    os.remove(target)
    return {"deleted": name, "kind": "file", "freed_bytes": size,
            "freed_gb": round(size / 1e9, 2)}


def _tree_bytes(path: str) -> int:
    """Recursive on-disk size of a directory (symlinks not followed)."""
    total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            p = os.path.join(root, n)
            try:
                if not os.path.islink(p):
                    total += os.path.getsize(p)
            except OSError:  # pragma: no cover - raced deletion
                continue
    return total


#: Weight file extensions the disk inventory reports as standalone entries.
#: ``.gguf`` alone made every vLLM/#574 repo directory invisible AND uncounted
#: in ``total_gb`` (NODE-10) — the operator saw a half-empty volume that was
#: actually full. Repo directories are reported as single ``dir`` entries by
#: ``disk_inventory``; these are the flat single-file weights.
DISK_WEIGHT_SUFFIXES = (".gguf", ".safetensors", ".bin", ".pt", ".pth", ".onnx")


def disk_inventory(mount: str) -> dict:
    """#306 + NODE-10: what this worker physically holds on its models mount.

    Two entry kinds, both counted into ``total_gb``:

    * ``file`` — a flat weight (llama.cpp GGUF and friends) at the mount root,
      which is where the puller writes every weight (#303 flattening).
    * ``dir`` — a top-level directory, i.e. a #574 repo-dir model a vLLM
      deployment serves whole. Size is the recursive sum; the console shows one
      row for the model rather than nothing at all.

    Files nested INSIDE a reported directory are not listed separately — their
    bytes are already in the directory's total, and listing them twice would
    double-count the volume. ``name`` is therefore the mount-root entry name,
    which for a flat weight is the same string the pre-NODE-10 relpath produced;
    the manager's ``_worker_has_files`` matches on basename either way.

    Pure (no docker, no app.state) so it unit-tests against a tmp_path, the same
    posture as ``delete_cached_weight``.
    """
    files, total = [], 0
    try:
        with os.scandir(mount) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as exc:
        return {"mount": mount, "files": [], "count": 0, "total_gb": 0,
                "error": str(exc)[:200]}
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                sz = _tree_bytes(e.path)
                kind = "dir"
            elif e.is_file(follow_symlinks=False):
                if not e.name.lower().endswith(DISK_WEIGHT_SUFFIXES):
                    continue
                sz = e.stat().st_size
                kind = "file"
            else:
                continue
        except OSError:  # pragma: no cover - raced deletion
            continue
        files.append({"name": e.name, "kind": kind, "size_bytes": sz,
                      "size_gb": round(sz / 1e9, 2)})
        total += sz
    files.sort(key=lambda f: f["size_bytes"], reverse=True)
    return {"mount": mount, "files": files, "count": len(files),
            "total_gb": round(total / 1e9, 2)}


def evict_weights(mount: str, names, loaded: dict | None = None) -> dict:
    """#307 S3: delete a model's weight files off this worker's models mount.

    ``names`` come from the manager over the command channel — the same origin
    as ``pull_artifact``'s — so each gets the same leaf-only safety reduction
    (``safe_artifact_name``) before touching a path.

    NODE-9: and the same IN-USE guard ``delete_cached_weight`` has always had.
    Evict had none, so the fleet-wide "remove from everywhere" leg could pull a
    weight out from under a live engine on a worker that happened to still serve
    it — the exact data-loss-adjacent case the delete half refuses. In-use names
    are reported in ``in_use`` and left on disk; the rest of the batch still
    proceeds (evict is a best-effort sweep, not a transaction).

    #837: two honesty fixes the focused test surfaced.

    * A repo DIRECTORY is removed, exactly as the #306 delete half already
      removes it (NODE-10, ``shutil.rmtree``). ``os.remove`` raises
      ``IsADirectoryError`` on a #574 vLLM model's ``models_mount/<model>/``,
      which was swallowed into ``missing`` — so the fleet-wide evict reported
      the model as ALREADY GONE while every one of its tens of GB was still on
      the volume.
    * A removal that genuinely fails lands in ``failed``, not ``missing``.
      ``missing`` means "already gone — idempotent, not an error"; a
      permission error is neither, and folding it in told the operator disk
      had been freed when it had not.
    """
    import shutil

    from app.puller import UnsafeArtifactName, safe_artifact_name

    in_use_leaves = set()
    for rec in (loaded or {}).values():
        for f in ((rec or {}).get("files") or []):
            in_use_leaves.add(os.path.basename(f))

    deleted, missing, refused, in_use, failed = [], [], [], [], []
    for raw in names or []:
        try:
            leaf = safe_artifact_name(raw)
        except UnsafeArtifactName:
            refused.append(raw)
            continue
        if leaf in in_use_leaves:
            in_use.append(leaf)
            continue
        path = os.path.join(mount, leaf)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            deleted.append(leaf)
        except FileNotFoundError:
            missing.append(leaf)  # already gone — idempotent, not an error
        except OSError as exc:
            logger.warning("evict_weights: could not remove %s: %s", path, exc)
            failed.append(leaf)
    return {"mount": mount, "deleted": deleted, "missing": missing,
            "refused": refused, "in_use": in_use, "failed": failed}


def _poll_commands(app, worker_id, docker, manager_url, node_key, http_post) -> None:
    from app import LoadRequest, perform_load
    from app.commands import execute_command, run_command_cycle
    from app.drivers.base import stop_engine

    def _load(args):
        return perform_load(app, LoadRequest(**args))

    def _unload(args):
        iid = args.get("instance_id") or args.get("container")
        # MUST forget from the supervisor first, else its bounded-backoff tick
        # resurrects the container we're about to stop (the orphan-engine trap).
        sup = getattr(app.state, "supervisor", None)
        if sup is not None:
            try:
                sup.forget(iid)
            except Exception:  # pragma: no cover
                pass
        stop_engine(iid, docker)
        try:
            app.state.loaded.pop(iid, None)
        except Exception:  # pragma: no cover
            pass
        return {"unloaded": iid}

    def _mirror(args):
        from app import perform_mirror
        return perform_mirror(app, args)

    def _disk(args):
        # #306 + NODE-10: what weights this worker physically holds — flat files
        # AND repo directories. The body is a pure module-level function so it
        # unit-tests against a tmp_path (same posture as delete_cached_weight).
        return disk_inventory(getattr(app.state, "models_mount", "/models"))

    def _evict(args):
        # #307 S3 + NODE-9: per-worker leg of "remove from fleet", with the same
        # in-use guard the delete half has. Body is module-level for the same
        # testability reason as _disk.
        return evict_weights(getattr(app.state, "models_mount", "/models"),
                             (args or {}).get("files"),
                             getattr(app.state, "loaded", {}))

    def _delete_disk_model(args):
        # #306 delete half: sibling of _disk() above — the same models_mount,
        # plus app.state.loaded so the in-use guard has ground truth on what
        # this node currently has deployed.
        mount = getattr(app.state, "models_mount", "/models")
        loaded = getattr(app.state, "loaded", {})
        return delete_cached_weight(mount, (args or {}).get("name"), loaded)

    def _pull(args):
        from app import perform_pull
        return perform_pull(app, args)

    # ── #549 R2: runner-image lifecycle handlers ────────────────────────────
    def _deploy_runner(args):
        """Pull a runner image from the master's registry onto this node.

        The allow-list is enforced HERE, not trusted from the channel: this node
        never pulls from docker.io or any registry other than its configured one
        (#307's air-gap invariant, applied to images). The manager validated the
        ref too, but the node is the one running `docker pull`, so it does not
        take the channel's word."""
        from app.drivers.images import (allowed_runner_registry,
                                        ref_from_allowed_registry,
                                        runner_pull_auth_config)

        image = (args or {}).get("image") or ""
        if not ref_from_allowed_registry(image):
            raise ValueError(
                f"refusing to pull {image!r}: runner images come from "
                f"{allowed_runner_registry()!r} only — this node never pulls from "
                f"a public registry (#549 R2)")
        if "@" in image:
            # digest ref — docker-py takes the whole reference as repository;
            # splitting on ':' would cut inside the sha256 digest.
            repo, tag = image, None
        else:
            repo, _, tag = image.rpartition(":")
            # rpartition mis-splits when the ONLY colon is the registry port
            # (host:5000/name → tag "5000/name"); a tag never contains '/'.
            if "/" in tag or not repo:
                repo, tag = image, None
        # #571: the hub's /v2 is basic-authed at the edge; the credential is
        # attached ONLY to refs that passed the allow-list check above.
        pulled = docker.images.pull(repo, tag=tag,
                                    auth_config=runner_pull_auth_config())
        ident = getattr(pulled, "id", None) or str(pulled)
        logger.info("runner image %s pulled (%s)", image, ident)
        return {"image": image, "id": ident, "status": "pulled"}

    def _remove_runner(args):
        """Delete a runner image — ONLY one from the allowed registry (this
        channel removes what it deployed; the init-built local runners belong to
        init/upgrade, not to remote management), and never one in use.

        No force=True: docker refuses to delete an image a container uses, and
        that refusal IS the safety property — a runner serving a deployment must
        not be pulled out from under it. The error is reported verbatim so the
        operator sees which container holds it; drain first (#261-C2)."""
        from app.drivers.images import allowed_runner_registry, ref_from_allowed_registry

        image = (args or {}).get("image") or ""
        if not ref_from_allowed_registry(image):
            raise ValueError(
                f"refusing to remove {image!r}: this channel manages images from "
                f"{allowed_runner_registry()!r} only — locally-built runners are "
                f"owned by init/upgrade (#549 R2)")
        docker.images.remove(image)          # raises if in use → reported as failed
        logger.info("runner image %s removed", image)
        return {"image": image, "status": "removed"}

    def _list_runners(args):
        """Inventory the runner images this node holds — both the allowed
        registry's (deployed via this channel) and the local init-built runners,
        labelled by origin so the console can tell them apart."""
        from app.drivers.images import (AMD_IMAGE, CPU_IMAGE,
                                        allowed_runner_registry,
                                        allowed_runner_registry_source)

        registry = allowed_runner_registry()
        local_names = {AMD_IMAGE.split(":")[0], CPU_IMAGE.split(":")[0],
                       "llama-rocm-runner"}
        out = []
        for img in docker.images.list():
            for ref in (getattr(img, "tags", None) or []):
                first, sep, _ = ref.partition("/")
                if sep and first == registry:
                    origin = "registry"
                elif ref.split(":")[0] in local_names:
                    origin = "local-build"
                else:
                    continue
                size = (getattr(img, "attrs", {}) or {}).get("Size", 0)
                out.append({"image": ref, "id": getattr(img, "id", ""),
                            "origin": origin, "size_bytes": size,
                            "size_gb": round(size / 1e9, 2)})
        out.sort(key=lambda r: r["image"])
        # #1860: the console cannot judge the registry by its value alone —
        # `llm-registry:5000` is correct when the operator set it and a dead end
        # when the node fell back to it. Report WHICH source answered.
        return {"registry": registry,
                "registry_source": allowed_runner_registry_source(),
                "runners": out, "count": len(out)}

    def _execute(cmd):
        return execute_command(cmd, docker=docker, load_fn=_load, unload_fn=_unload,
                               mirror_fn=_mirror, disk_fn=_disk, pull_fn=_pull,
                               deploy_runner_fn=_deploy_runner,
                               remove_runner_fn=_remove_runner,
                               list_runners_fn=_list_runners, evict_fn=_evict,
                               delete_disk_fn=_delete_disk_model)

    # #207: present the per-worker command key if the box handed us one at
    # enrollment (LLM_WORKER_COMMAND_KEY); else fall back to the shared node key.
    command_key = _env("LLM_WORKER_COMMAND_KEY") or None

    # #364 long commands (mirror_model, pull_artifact) run on a background
    # consumer so they never delay supervision or the heartbeat. The runner is
    # kept on app.state so successive report cycles reuse the same queue and
    # thread rather than spawning one per cycle.
    runner = getattr(app.state, "command_runner", None)
    if runner is None:
        runner = AsyncCommandRunner()
        app.state.command_runner = runner

    def _dispatch_async(cmd, run_and_report):
        from app.commands import AsyncDispatchRejected

        logger.info("command %s (%s) dispatched to the background runner "
                    "(queue depth %d/%d) — the report loop keeps heartbeating (#364)",
                    cmd.get("id"), cmd.get("kind"), runner.pending(), runner.maxsize())
        try:
            runner.submit(lambda: run_and_report(cmd))
        except CommandQueueFull as exc:
            # NODE-15: REJECT, do not fall back to inline. Running it inline is
            # exactly the report-loop stall #364 removed, and silently dropping
            # it is worse. A clean `failed` lets the manager retry.
            raise AsyncDispatchRejected(str(exc)) from exc

    run_command_cycle(worker_id, manager_url=manager_url, node_key=node_key,
                      http_post=http_post, execute=_execute, command_key=command_key,
                      dispatch_async=_dispatch_async)


def configure_runtime(app) -> None:
    """Wire the live runtime from env — ONLY when LLM_WORKER_AGENT_RUNTIME=1.
    Off-box / in tests the flag is unset → complete no-op (the endpoint
    skeleton + injected-state tests are unaffected)."""
    if _env("LLM_WORKER_AGENT_RUNTIME") != "1":
        return

    hardware = _env("HARDWARE", "amd")
    app.state.hardware = hardware
    if (hardware or "").lower() == "amd":
        app.state.gpu_probe = amd_gpu_probe

    app.state.models_volume = _env("LLM_WORKER_MODELS_VOLUME", app.state.models_volume)
    app.state.engine_network = _env("LLM_WORKER_ENGINE_NETWORK", app.state.engine_network)

    docker_host = _env("DOCKER_HOST")
    try:
        client = make_docker_client(docker_host)
    except Exception:
        logger.exception("node runtime: docker client init failed — engine spawn disabled")
        return
    app.state.docker_client = client

    from app.drivers import EngineSupervisor

    supervisor = EngineSupervisor(
        client,
        # First-boot readiness window. A large model's FIRST boot on CUDA/vLLM can
        # far exceed the old 120s default: weight load is quick, but a big
        # multimodal + hybrid (vision-encoder + mamba) model spends minutes in
        # vLLM's KV-cache/encoder profiling and (non-eager) CUDA-graph capture
        # before /health ever comes up. At 120s the supervisor killed it
        # mid-profiling and looped forever (never a crash — no OOM, exit 0 — just
        # a premature-kill loop). Bump the default to 300s and make it env-tunable
        # (same pattern as recover_after) so an operator with an even larger model
        # can extend it. #969-adjacent, found bringing up qwen3.6 FP8 on Blackwell.
        #
        # #971: resolved through engine_readiness_grace() rather than a bare
        # os.environ.get + float(). The compose environment: block is how this
        # reaches the process at all, and a `${VAR:-}` pass-through hands the
        # process "" — which float() rejects, at startup, on every node.
        readiness_grace=engine_readiness_grace(),
        # #316 self-heal: half-open a circuit-broken engine after a cooldown so a
        # transient HOST-pressure failure (unified-memory OOM/thrash) recovers on
        # its own instead of showing false-"failed" forever. Bounded + tunable.
        recover_after=engine_recover_after(),
        max_recover_attempts=engine_max_recover_attempts(),
    )
    app.state.supervisor = supervisor

    # #293: re-adopt engines this node already runs, so a node RESTART doesn't
    # orphan them (or let the manager prune their instances). Best-effort.
    app.state.loaded = getattr(app.state, "loaded", {})
    if client is not None:
        try:
            _readopt_engines(app, client, supervisor)
        except Exception:  # pragma: no cover - defensive
            logger.exception("engine re-adoption failed (non-fatal)")

    # Version reporting (Slice A): the node's stack version (from .env) + the
    # engine's llama.cpp build (one-shot `--version` probe of the engine image).
    # Computed ONCE here (runtime-only, never in tests) + stashed on app.state;
    # run_report_cycle just reads them. Best-effort — None on any failure.
    _sv = resolve_stack_version_detail(_env("RAZZFAZZ_VERSION") or None,
                                       _env("STACK_ROOT")) or {}
    app.state.stack_version = _sv.get("version")
    app.state.stack_version_source = _sv.get("source")
    # `or {}`: this is the STARTUP path, and a best-effort probe must not be
    # able to stop a node from coming up. A patched or future implementation
    # that answers None then yields an unknown version with an unknown reason —
    # which is the honest reading of "no answer" — instead of an AttributeError
    # three frames into configure_runtime (#1932). The same reasoning applies to
    # the `or {}` on the stack-version detail three lines up.
    _ev = _probe_engine_version(hardware, client) or {}
    app.state.engine_version = _ev.get("version")
    app.state.engine_version_why = _ev.get("why")
    # #295 fits-check budget: host RAM + best-effort VRAM (VRAM is the honest
    # ceiling on unified-memory boxes). Computed once here; reported each cycle.
    app.state.mem_total_gb = _host_mem_gb()
    app.state.vram_total_gb = _vram_total_gb(hardware)

    manager_url = _env("LLM_MANAGER_URL")
    # #285: the credential the node authenticates with. In enforce mode the box
    # is provisioned with only its per-worker key (LLM_WORKER_COMMAND_KEY) and no
    # shared node_key — use it for registration too; fall back to the shared
    # node_key (allow mode / back-compat). The command cycle re-reads
    # LLM_WORKER_COMMAND_KEY itself, so both paths use the same key.
    node_key = _env("LLM_WORKER_COMMAND_KEY") or _env("LLM_MANAGER_NODE_KEY", "")
    node_name = _env("LLM_WORKER_NAME") or _env("HOSTNAME") or "node"
    node_addr = _env("LLM_WORKER_ADDRESS") or node_name
    interval = float(_env("LLM_WORKER_REPORT_INTERVAL", "30"))

    def _http_get(url):
        import httpx

        # #1064: engine health probes (http://engine-<id>:8080/health) are
        # in-network by container name — never via the corporate proxy.
        with httpx.Client(timeout=health_probe_timeout(), trust_env=False) as c:
            return c.get(url).status_code

    def _http_post(url, *, json, headers):
        import httpx

        # #1064: registration/report goes to the MASTER by DNS name; the proxy
        # environment (and its NO_PROXY for the site's domain) applies as-is.
        with httpx.Client(timeout=10, trust_env=True) as c:
            return c.post(url, json=json, headers=headers)

    def _monotonic():
        import time

        return time.monotonic()

    stop = threading.Event()
    app.state._runtime_stop = stop
    fast_interval = float(_env("LLM_WORKER_REPORT_INTERVAL_FAST", "4"))

    def _transitional() -> bool:
        # #296: while any instance is pulling/loading, report FAST so the console
        # shows live download-% progress instead of 30s-granular jumps.
        loaded = getattr(app.state, "loaded", {}) or {}
        return any(isinstance(r, dict) and r.get("status") in
                   ("pulling", "loading", "restarting", "starting", "scheduled")
                   for r in loaded.values())

    # #1619: a SECOND, lean beat beside the full report.
    #
    # `run_report_cycle` is, by its own docstring, "supervision-tick +
    # registration-report + command-poll" — three jobs with very different time
    # constants on one 30 s clock. Two operator wishes came down to that clock:
    # a live chart that moves (the numbers only leave here in that cycle) and a
    # model log that appears (the node CLAIMS its commands in that cycle, so a
    # `tail_logs` sat in the queue up to 30 s before anyone looked at it).
    #
    # The full report stays where it is. It carries the whole registration —
    # identity, instance table, image pins — and every POST of it rebuilds the
    # LiteLLM router config; that belongs on a slow clock. This beat carries
    # five numbers and a claim, and nothing else.
    #
    # Set LLM_WORKER_FAST_BEAT_INTERVAL=0 to switch it off: the box then behaves
    # exactly as before, which is what makes this reversible on a customer box
    # without a rollback.
    fast_beat_interval = float(_env("LLM_WORKER_FAST_BEAT_INTERVAL", "3"))

    def _fast_loop():
        while not stop.wait(fast_beat_interval):
            fast_beat_once(app, manager_url=manager_url, node_key=node_key,
                           http_post=_http_post)

    def _loop():
        wait = interval
        while not stop.wait(wait):
            run_report_cycle(
                app, supervisor, http_get=_http_get, http_post=_http_post,
                now=_monotonic(), manager_url=manager_url, node_key=node_key,
                hardware=hardware, node_name=node_name, node_addr=node_addr,
            )
            wait = fast_interval if _transitional() else interval

    t = threading.Thread(target=_loop, name="worker-agent-runtime", daemon=True)
    t.start()
    app.state._runtime_thread = t
    if fast_beat_interval > 0:
        ft = threading.Thread(target=_fast_loop, name="worker-agent-fast-beat",
                              daemon=True)
        ft.start()
        app.state._fast_beat_thread = ft
    logger.info("node runtime armed: hardware=%s manager=%s interval=%ss "
                "fast_beat=%ss", hardware, manager_url, interval,
                fast_beat_interval if fast_beat_interval > 0 else "off")
