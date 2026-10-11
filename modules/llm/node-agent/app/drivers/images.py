# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#331 — one authority for the engine image each hardware class launches.

The AMD driver defaulted to `razzfazz-llama-vulkan-runner:latest`, a name **nothing
in the repo builds or tags**. What `cli/init.sh` and `cli/upgrade.sh` actually build
is `llama-vulkan-runner:<LLAMA_CPP_TAG>` (today b9851, plus b8943 as the rollback
image). Since `select_driver` is called with no `image=` override and
`LoadSpecInput` carries no image field, the driver default is what really runs — so
on a clean AMD box the engine launch failed with image-not-found, surfacing as a
deploy stuck in `pending`/`failed`.

#1188 (2026-09): the AMD default moved b8943 → b9851. On b8943 a multi-turn
agentic chat with thinking streams the raw Qwen-XML `<tool_call>` block as text
after `</think>` (tool-call/reasoning-parser fixes landed upstream in between);
b9851 is the #609 research-anchored tag (MTP gate passed text-only) and was
already what init/upgrade/post-install build first. b8943 stays built as the
rollback: `RAZZFAZZ_ENGINE_IMAGE_AMD=llama-runner:b8943-vulkan` repins a box.

On an installed box `compose.no-build.yml` (#184 WS2a) means nothing builds at
runtime, so there is no self-repair: the image is simply absent.

The CPU driver had the same shape for a different reason: it pointed at the
upstream `ghcr.io/ggml-org/llama.cpp:server` rather than the `llama-cpu-runner:b10853`
that init/upgrade build from `modules/llm/runners/llama-cpu/Dockerfile`. That works
on a box with egress and fails on an air-gapped one — worse than failing outright,
because it fails only for the customers least able to debug it.

#1518 (E5): every image named here is BUILT by this repo — the one upstream
reference (`vllm/vllm-openai`, pulled from Docker Hub) went with the vLLM path.
An air-gapped box can therefore serve on any hardware class from the images the
stack ships.

Names must agree in FOUR places — this module, `cli/init.sh`, `cli/upgrade.sh` and
the cleanup filters in `scripts/lib.sh`. `tests/unit/llm-node-agent/test_engine_image_names.py`
cross-checks them against the shell sources rather than trusting a comment.
"""
from __future__ import annotations

import os
import re

# The locally-built runners, exactly as `_build_runner` / `_build_runner_img` /
# `_build_one` (post-install) tag them. The AMD tag must equal the runner
# Dockerfile's `ARG LLAMA_CPP_TAG` default and be built WITH that build-arg in
# all three scripts — tests/unit/llm-node-agent/test_1188_amd_runner_default.py.
# #1516 (E5): these tags are the node-side copy of
# `modules/llm/runners/runners.yaml`, which is the source of truth for what the
# stack BUILDS and publishes. A thin node has no repo checkout, so the constants
# must be baked in — a consistency guard pins them to the manifest instead.
#
# The tag names the toolkit and GPU architecture the binary was compiled for,
# because the previous scheme could not tell two incompatible binaries apart:
# `llama-cuda-runner:b9851` (CUDA 12.8, sm_120) and `llama-cuda-gb10-runner:b9851`
# (CUDA 13.0.2, sm_121a) shared a tag and differed only by repository. The wrong
# one dies with "no kernel image is available for execution on the device".
AMD_IMAGE = "llama-runner:b9851-vulkan"
CPU_IMAGE = "llama-runner:b10853-cpu"
# The locally-built CUDA llama.cpp (llama-server) runner — same GGUF
# llama-server as the AMD/CPU path but with CUDA offload. #1518 (E5): this is
# now the ONLY thing a CUDA worker launches. The upstream `vllm/vllm-openai`
# image it used to default to is gone with the vLLM path: GGUF is the fleet's
# one model architecture, so every hardware class runs llama.cpp and only the
# BINARY differs (Vulkan / CUDA sm_120 / CUDA sm_121a / CPU).
CUDA_LLAMACPP_IMAGE = "llama-runner:b9851-cuda12.8-sm120"

# ── GB10 (Grace Blackwell, DGX Spark class) ──────────────────────────────────
# #1329: a SEPARATE worker type, not a variant of `cuda`, because the images
# differ at the CUDA level and nothing else does. GB10 is sm_121; the runner
# above is compiled for sm_120 (RTX PRO 6000) against CUDA 12.8, and CUDA 12.8's
# nvcc does not know sm_121 at all — so one fat binary would have meant dragging
# the RTX 6000 build onto CUDA 13 and re-verifying a production hardware class
# for no gain. Operator decision 2026-09-04: two tags, the sm_120 build stays
# byte-identical.
#
# The llama.cpp runner is built from a GB10-specific Dockerfile (CUDA 13 base,
# `-DCMAKE_CUDA_ARCHITECTURES=121a-real` — the `a` suffix is what enables native
# NVFP4, which is the whole performance argument for the box). That Dockerfile
# and its build wiring are NOT in this change: they need the hardware to verify
# and are handed to the box owner together with #1309.
#
# Measured on the first GB10 box (HP ZGX Nano G1n, DGX OS, driver 580.173.02,
# 2026-09-05, #1332/#1436/#1517): the sm_120 runner dies there with "no kernel
# image is available for execution on the device", while this build — CUDA
# 13.0.2, `121a-real` — loads the model and serves. That measurement is the
# whole reason two CUDA tags exist.
GB10_LLAMACPP_IMAGE = "llama-runner:b9851-cuda13.0-sm121a"

# ── #1517 (E5): the GPU picks its own runner ─────────────────────────────────
# A CUDA llama.cpp binary is compiled FOR a compute capability, and
# `nvidia-smi --query-gpu=compute_cap` reports exactly that number — so the
# capability, not a hand-typed class name, is the honest selector. Measured:
# GB10 (.191) reports 12.1, the RTX PRO 6000 reports 12.0, which is why its
# runner is built with -DCMAKE_CUDA_ARCHITECTURES=120.
#
# Before this, `HARDWARE` was typed at enrolment (`--hardware amd|cpu|nvidia`,
# default amd) and an operator had to know that a GB10 is called `cuda-gb10` —
# vocabulary only we know. Getting it wrong is not a warning: the wrong binary
# dies with "no kernel image is available for execution on the device".
#
# Mirrors `compute_cap` in modules/llm/runners/runners.yaml; a consistency guard
# pins the two together (a thin node has no checkout, so this must be baked in).
CUDA_CAPABILITY_IMAGES = {
    "12.0": CUDA_LLAMACPP_IMAGE,        # RTX PRO 6000 Blackwell, sm_120
    "12.1": GB10_LLAMACPP_IMAGE,        # GB10 / DGX-Spark class, sm_121a
}

#: Box override, for a GPU we have not mapped yet or a box that misreports.
CUDA_CAPABILITY_ENV = "RAZZFAZZ_CUDA_COMPUTE_CAP"


def detect_cuda_capability(_run=None) -> str | None:
    """The GPU's compute capability as `nvidia-smi` reports it, e.g. ``12.1``.

    ``None`` when there is no NVIDIA GPU or no tool — the caller then falls back
    to its per-hardware default rather than guessing. An explicit
    ``RAZZFAZZ_CUDA_COMPUTE_CAP`` always wins, so a box can be corrected without
    a code change.
    """
    override = (os.environ.get(CUDA_CAPABILITY_ENV) or "").strip()
    if override:
        return override
    import shutil
    import subprocess
    run = _run or subprocess.run
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = run([smi, "--query-gpu=compute_cap", "--format=csv,noheader"],
                  capture_output=True, text=True, timeout=8)
    except Exception:      # pragma: no cover - a probe must never raise
        return None
    if getattr(out, "returncode", 1) != 0:
        return None
    for line in (getattr(out, "stdout", "") or "").splitlines():
        cap = line.strip()
        # Multi-GPU boxes list one line per card; they must not be mixed, so the
        # FIRST card decides and a mismatch is the operator's to resolve via the
        # override. (Not a case the fleet has today.)
        if cap and cap.upper() != "N/A":
            return cap
    return None


class UnknownCudaCapability(ValueError):
    """Raised instead of guessing an image for an unmapped GPU.

    #1517 rev-B: a ``ValueError``, not a bare ``RuntimeError``. The one call
    site that turns a driver choice into an operator-visible answer —
    ``/models/load`` in app/__init__.py — catches ``ValueError`` and renders it
    as a 400 with the message in ``detail``. As a RuntimeError this careful
    text (the capability, the runners that ARE built, the override to set)
    reached the node log only, and the manager saw a bare 500. The message and
    the escape hatch it names are the whole point of raising instead of
    guessing, so it has to arrive where the operator is looking."""


def cuda_llamacpp_image(capability: str | None = None) -> str:
    """The llama.cpp runner for this box's NVIDIA GPU.

    Unmapped capability raises: falling back to any of the built images means
    "no kernel image is available for execution on the device" at engine start,
    with nothing in the console explaining it.
    """
    cap = capability if capability is not None else detect_cuda_capability()
    if cap is None:
        # No GPU visible (or no nvidia-smi): keep the historical default rather
        # than fail — the image may still be right, and the engine will say so.
        return CUDA_LLAMACPP_IMAGE
    try:
        return CUDA_CAPABILITY_IMAGES[cap]
    except KeyError:
        raise UnknownCudaCapability(
            f"this GPU reports CUDA compute capability {cap!r}, which no runner "
            f"is built for (have: {', '.join(sorted(CUDA_CAPABILITY_IMAGES))}). "
            f"A llama.cpp binary is compiled for a specific architecture, so "
            f"launching another one fails with 'no kernel image is available "
            f"for execution on the device'. Add the target to "
            f"modules/llm/runners/runners.yaml, or set "
            f"{CUDA_CAPABILITY_ENV}=<capability> to pin an existing one."
        ) from None

# Env overrides, so an image can be repinned on a box without a code change — the
# thing the issue asked for. Empty/whitespace is treated as unset: an env var that
# exists but is blank is a misconfiguration, and honouring it would launch `""`.
ENV_VARS = {
    "amd": "RAZZFAZZ_ENGINE_IMAGE_AMD",
    "cpu": "RAZZFAZZ_ENGINE_IMAGE_CPU",
    "cuda": "RAZZFAZZ_ENGINE_IMAGE_CUDA",
    "cuda-gb10": "RAZZFAZZ_ENGINE_IMAGE_CUDA_GB10",
}

# #1518 (E5): `None` = "ask the GPU". Every NVIDIA class serves GGUF through
# llama.cpp now, and WHICH llama.cpp binary is a property of the card's compute
# capability (#1517), not of a class name an operator typed at enrolment.
_DEFAULTS = {
    "amd": AMD_IMAGE,
    "cpu": CPU_IMAGE,
    "cuda": None,
    "cuda-gb10": None,
}

# (hardware, engine) → (default image, env-override var). Since #1518 removed
# the vLLM path there is no longer a per-hardware default this DIFFERS from —
# what the table still buys is the engine-scoped env override, which lets a box
# repin the CUDA runner without a code change. `None` means "ask the GPU"
# (#1517). The `cuda-gb10` row stays as a DEPRECATED alias so a node still
# carrying `HARDWARE=cuda-gb10` keeps working for one cycle.
_ENGINE_IMAGE_OVERRIDES = {
    ("cuda", "llamacpp"): (None, "RAZZFAZZ_ENGINE_IMAGE_CUDA_LLAMACPP"),
    ("cuda-gb10", "llamacpp"): (None, "RAZZFAZZ_ENGINE_IMAGE_CUDA_GB10_LLAMACPP"),
}

# #1517 rev-B — the pin must survive the box being RECOGNISED.
#
# A capability variant resolves its image under a class name of its own
# (`cuda-gb10`), but the operator's pin is written for the class the node was
# ENROLLED as (`cuda`). Before this PR a GB10 registered as `nvidia` was a plain
# `cuda` box and its RAZZFAZZ_ENGINE_IMAGE_CUDA_LLAMACPP pin WORKED; after it,
# the same box resolves as `cuda-gb10` and the pin silently stopped applying —
# on exactly the boxes #1517 exists for. So a class-specific pin is consulted
# first and the enrolled-class pin is the fallback.
#
# Deliberately llama.cpp ONLY, and not symmetric with the vLLM side: both CUDA
# llama.cpp runners are images this repo builds, so inheriting the generic pin
# is at worst the operator's own build. `RAZZFAZZ_ENGINE_IMAGE_CUDA` names the
# Docker-Hub `vllm/vllm-openai`, which was MEASURED dead on sm_121 (no kernel
# image, no PTX — #1332/#1436); letting a fleet-wide vLLM pin leak onto a GB10
# would brick it, so that one stays class-scoped.
_PIN_FALLBACK_ENGINE_VARS = {
    ("cuda-gb10", "llamacpp"): "RAZZFAZZ_ENGINE_IMAGE_CUDA_LLAMACPP",
}


# Fleet-facing hardware token → worker-agent driver class. The rest of the stack
# (the global ``HARDWARE`` env that picks ``llm/compose.devices.<HARDWARE>.yml``,
# the deploy catalog's ``hardware: ["nvidia"]``, the manager's DeployRequest) all
# speak ``nvidia`` for an NVIDIA/CUDA box, but this worker-agent's driver registry
# and engine map are keyed on the CUDA *runtime* class ``cuda``. A real NVIDIA box
# MUST set ``HARDWARE=nvidia`` for the rest of the stack to compose correctly, so
# the worker-agent has to accept ``nvidia`` and resolve it to its ``cuda`` driver —
# otherwise the FIRST real CUDA deploy fails with "no engine driver for
# hardware='nvidia'". The worker still REGISTERS as ``nvidia`` (so the manager /
# catalog / auto-pick stay consistent); only the internal driver/engine lookup is
# normalized.
#: Fleet-facing tokens → the runtime class the driver registry is keyed on.
#: `gb10`/`nvidia-gb10` fold onto `cuda-gb10` (#1329) so an operator can type the
#: short form at enrolment. The canonical form deliberately KEEPS the `cuda-`
#: prefix: the manager's `_hardware_family` is substring-based, so `cuda-gb10`
#: lands in the `nvidia` family for free — exactly as `amd-gfx1151` lands in
#: `amd` — while a bare `gb10` would form its own family and every `nvidia`
#: placement selector would silently pass the box by.
# #1517 (E5): there is ONE NVIDIA class again. Which llama.cpp binary a box gets
# is decided by its CUDA compute capability (drivers/images.py), not by a second
# hardware class an operator has to know the name of — the GB10 reports 12.1,
# the RTX PRO 6000 reports 12.0, and that number is what the binary is compiled
# for. `cuda-gb10` stays here as an alias because nodes in the field carry it in
# `.env.node`; it normalizes onto `cuda` and needs no re-enrolment.
_HARDWARE_ALIASES = {
    "nvidia": "cuda",
    "gb10": "cuda",
    "nvidia-gb10": "cuda",
    "cuda-gb10": "cuda",
}

#: Every hardware class served by the NVIDIA/CUDA stack. ONE definition, because
#: the two places that branch on it must never disagree: driver selection and
#: the nvidia-smi memory-budget probe (`runtime.py`). Getting the second one
#: wrong is not a loud failure — it drops through to the amdgpu sysfs glob,
#: returns None, and the #227/#295 admission budget goes inert, which is the
#: #969/#330 defect this repo has already paid for twice. A new CUDA-class
#: worker type MUST be added here; `test_1329_gb10_worker_type.py` enforces it
#: against the driver registry.
# #1517: one class, but the legacy value is kept in the set so a box that has
# not re-registered yet still takes every CUDA branch (the #969/#330 failure was
# exactly a CUDA box missing its branch).
CUDA_CLASSES = frozenset({"cuda", "cuda-gb10"})


#: NVIDIA classes whose GPU memory IS host memory (Grace Blackwell / DGX Spark).
#: Keyed on the RAW declaration on purpose: normalize_hardware() folds every
#: GB10 dialect onto plain `cuda` (#1517) because they share an image, but they
#: do NOT share a memory layout — after normalization a GB10 is indistinguishable
#: from a box with a discrete card, and on that box host RAM is not VRAM.
UMA_CUDA_DIALECTS = frozenset({"cuda-gb10", "gb10", "nvidia-gb10", "dgx-spark"})


def is_unified_memory(hardware: str | None) -> bool:
    """True when the box has no separate VRAM to account for.

    #1990: the #1456 fallback used to infer this from nvidia-smi answering
    ``[N/A]``, which made it unreachable on a node that has no nvidia-smi — and
    the worker-agent deliberately runs WITHOUT the NVIDIA runtime, so it never
    has one. Ask what the platform is instead of what is installed on it.
    """
    return (hardware or "").strip().lower() in UMA_CUDA_DIALECTS


def normalize_hardware(hardware: str | None) -> str:
    """Canonical worker-agent hardware class: ``amd`` | ``cuda`` | ``cpu``.

    #1517: every NVIDIA dialect the fleet writes — ``nvidia``, ``gb10``,
    ``nvidia-gb10`` and the retired ``cuda-gb10`` — maps onto ``cuda``. Any other
    value passes through lowercased/stripped so an unknown class still fails
    loud in ``select_driver``."""
    hw = (hardware or "").strip().lower()
    return _HARDWARE_ALIASES.get(hw, hw)


def engine_image(hardware: str) -> str:
    """The image to launch for ``hardware``, honouring the env override.

    Per-hardware default. A CUDA class resolves through the GPU's compute
    capability (``None`` in ``_DEFAULTS``) because both CUDA runners are
    llama.cpp and only the architecture differs. Engine-aware selection — i.e.
    the engine-scoped env override — lives in ``engine_image_for``.

    #1518 (E5, from the review of #1546): the class is NORMALIZED first. The
    fleet writes ``HARDWARE=nvidia`` (the enrolment token of #1517, and what
    ``config/.env.example`` ships) while ``_DEFAULTS`` is keyed on the driver
    classes, so this raised ValueError for the one spelling every box actually
    carries. It was not loud: the config portal's
    ``expected_images.engine_runner_images()`` catches it and fails open, so an
    NVIDIA box's offline package carried NO runner image and
    ``rzfz verify-images`` reported nothing missing. Measured before the fix —
    ``engine_runner_images`` for HARDWARE=nvidia: ``set()``; for amd:
    ``{llama-runner:b9851-vulkan}``."""
    hw = normalize_hardware(hardware)
    try:
        default = _DEFAULTS[hw]
    except KeyError:
        raise ValueError(f"no engine image for hardware={hardware!r} "
                         f"(have: {sorted(_DEFAULTS)})")
    override = (os.environ.get(ENV_VARS[hw]) or "").strip()
    if override:
        return override
    return default if default is not None else cuda_llamacpp_image()


def engine_image_for(hardware: str, engine: str | None = None) -> str:
    """The image to launch for (``hardware``, ``engine``), honouring env overrides.

    Any (hardware, engine) pair without an explicit entry in
    ``_ENGINE_IMAGE_OVERRIDES`` falls through to ``engine_image(hardware)``.
    Since #1518 both routes end at the same llama.cpp runner for a CUDA box;
    the pair-scoped entry exists for its env override. Empty/whitespace env
    override is treated as unset (same rule as ``engine_image``)."""
    hw = (hardware or "").strip().lower()
    eng = (engine or "").strip().lower()
    override_entry = _ENGINE_IMAGE_OVERRIDES.get((hw, eng))
    if override_entry is not None:
        default, env_var = override_entry
        pinned = (os.environ.get(env_var) or "").strip()
        if not pinned:
            fallback_var = _PIN_FALLBACK_ENGINE_VARS.get((hw, eng))
            if fallback_var:
                pinned = (os.environ.get(fallback_var) or "").strip()
        if pinned:
            return pinned
        # #1517: `None` means "ask the GPU" — the capability decides which of
        # the built CUDA runners this box can actually execute.
        return default if default is not None else cuda_llamacpp_image()
    return engine_image(hw)

# #549 R1: a conservative OCI image reference — [host[:port]/]name[:tag][@sha256:…].
#
# BYTE-IDENTICAL to RUNNER_IMAGE_RE in the manager's api/inventory.py. The two
# services cannot share a module, so agreement is test-enforced
# (tests/unit/llm-manager/test_runner_image.py compares the pattern strings),
# same discipline as the command kinds (#547). Change one, the test names the
# other.
RUNNER_IMAGE_RE = re.compile(
    r"^(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?/)?"      # registry host[:port]/
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"                        # first path component
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"                  # further components
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?"              # :tag
    r"(?:@sha256:[a-f0-9]{64})?$"                          # @digest
)


def valid_image_ref(ref) -> bool:
    """True iff ``ref`` is a well-formed image reference this node may launch."""
    return isinstance(ref, str) and bool(RUNNER_IMAGE_RE.match(ref))

# #549 R2: the ONE registry this node may pull runner images from — the master's
# Zot by default. Enforced in ref_from_allowed_registry(), which deploy_runner
# uses before any docker pull: a node must never reach docker.io or any other
# public registry for a runner, whatever the command channel says. That is the
# same air-gap invariant as model weights (#307), applied to images.
#
# Overridable for split deployments (a worker that reaches the master's registry
# on a routed address), empty-tolerant like every #319 knob: an env var that
# exists but is blank falls back to the default rather than allow-listing "".
#: Last resort only. `llm-registry:5000` is a COMPOSE-NETWORK name, and a runner
#: pull is not made by this process: `docker-py` hands the reference to the
#: HOST's docker daemon, which resolves it in the host resolver. Measured on
#: 0.91 (2026-09-08): `llm-registry` does not resolve there, while the hub domain
#: does — so the shipped default produced
#:
#:     images/create ?fromImage=llm-registry%3A5000%2Frunners%2F… ->
#:     dial tcp: lookup llm-registry on 127.0.0.53:53: server misbehaving
#:
#: on every box, master included (#1677). This is the difference from
#: DEFAULT_MODEL_REGISTRY_URL below, which THIS process fetches over HTTP and
#: where the compose name is right.
DEFAULT_RUNNER_REGISTRY = "llm-registry:5000"


def node_registry_credentials() -> tuple[str, str] | None:
    """The node's upstream-registry credential pair, or None (#571).

    One pair for BOTH consumers — SDK runner pulls and HTTP weight-blob pulls —
    because the node has one upstream registry (the master's hub). In-network
    consumers (llm-registry:5000 default) run unauthenticated and leave these
    empty. docker-py inside the agent container never sees the host's
    `docker login` (that's the daemon-CLI's ~/.docker/config.json, not ours),
    which is why the credential rides the env at all.
    """
    user = os.environ.get("LLM_WORKER_REGISTRY_USER", "").strip()
    pw = os.environ.get("LLM_WORKER_REGISTRY_PASSWORD", "").strip()
    return (user, pw) if user and pw else None


def runner_pull_auth_config() -> dict | None:
    """docker-py ``auth_config`` for a runner pull, or None. The CALLER must
    only attach this to refs that passed ref_from_allowed_registry — the
    credential is for that one host and must never travel elsewhere."""
    creds = node_registry_credentials()
    if creds is None:
        return None
    return {"username": creds[0], "password": creds[1]}


class _NoRedirect(__import__("urllib.request", fromlist=["HTTPRedirectHandler"]).HTTPRedirectHandler):
    """#577 review: urllib's default handler FOLLOWS redirects and keeps the
    request headers — Authorization included — across a cross-host hop, so the
    prefix scoping below would only protect the INITIAL url. Behind our own
    Caddy edge a redirect on a blob fetch is unexpected, so the safe contract
    is: refuse the hop, fail loud (HTTPError), never travel with the credential.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def registry_opener():
    """urlopen-compatible opener for registry fetches: redirects REFUSED."""
    import urllib.request
    return urllib.request.build_opener(_NoRedirect())


def registry_basic_auth_header(url: str, registry_base: str) -> dict:
    """``{"Authorization": "Basic …"}`` iff ``url`` is under ``registry_base``
    AND credentials are configured — else {}. The prefix check is the security
    property: the node's registry credential must never be sent to any other
    host (a crafted repo name; redirects are refused outright by
    registry_opener — see _NoRedirect). Comparison is byte-wise and fails SAFE:
    a case-different host or an explicit :443 against an implicit base yields
    NO header (→ a clear 401), never a leak — configure the base exactly as
    the node will request it.
    """
    creds = node_registry_credentials()
    if creds is None or not registry_base:
        return {}
    if not url.startswith(registry_base.rstrip("/") + "/"):
        return {}
    import base64
    tok = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


# #307 S4 — LOCAL vs REMOTE address for a worker's WEIGHT-BLOB pull
# (puller.pull_artifact / _zot_pull), as distinct from #549 R2's
# allowed_runner_registry() above (RUNNER IMAGE pulls via the docker SDK).
#
# LOCAL default carries the scheme because it feeds straight into
# puller.blob_url() (an HTTP base), unlike DEFAULT_RUNNER_REGISTRY above
# (a bare docker image-ref host).
DEFAULT_MODEL_REGISTRY_URL = "http://llm-registry:5000"


def registry_is_local() -> bool:
    """True iff this node is co-located with the master (#307 S4).

    Reuses the #262 ``LLM_WORKER_ADVERTISE_ADDR`` signal — registration.py
    already documents an unset value as "None on a same-box node" (the
    master needs no routed address to dial a co-located worker's engines).
    The mirror-image question for registry reachability holds the same
    answer: a co-located node's Docker DNS resolves ``llm-registry``
    directly; a routed thin node (#549 R0) does not, and needs the master's
    advertised registry address instead (``resolve_model_registry_base``).
    """
    return not (os.environ.get("LLM_WORKER_ADVERTISE_ADDR") or "").strip()


def resolve_model_registry_base() -> str:
    """The base URL this node pulls model-weight blobs from (#307 S4).

    LOCAL: always the in-network Zot default, whatever ``LLM_REGISTRY_URL``
    holds — matching the #559 hub design ("in-network consumers use
    llm-registry:5000 directly and are unaffected by the edge auth"). A
    co-located node's Docker DNS answer for ``llm-registry`` never changes,
    so there is nothing to override and no reason to let a stray env value
    redirect it.

    REMOTE: ``LLM_REGISTRY_URL`` — the operator-configured routed address for
    this worker (#571 / ``rzfz node-init --registry``). Empty-tolerant like
    every #319 knob: falls back to the same in-network default if unset,
    unchanged pre-#307-S4 behaviour for a misconfigured remote node (the
    ``rzfz node-init`` output already warns the operator about this case).
    """
    if registry_is_local():
        return DEFAULT_MODEL_REGISTRY_URL
    return (os.environ.get("LLM_REGISTRY_URL") or "").strip() or DEFAULT_MODEL_REGISTRY_URL


def allowed_runner_registry() -> str:
    """The ONE registry host this node may pull runner images from.

    Order: the explicit knob, then the hub domain, then the compose name.

    The hub domain is in the middle because it is the only coordinate that is
    the same for a co-located and a remote node, and because the host daemon —
    the process that actually performs the pull — can resolve it. #1677: the
    old two-step ended at the compose name, which the daemon cannot resolve on
    any box; `.env.example` even documented the hub domain as the empty-value
    meaning while the code never implemented it.

    The compose name is kept as the last resort rather than removed, and the
    reason is an OUTAGE, not a security hole. An earlier version of this
    docstring said an empty registry would make `ref_from_allowed_registry`
    "match every ref"; agent-rzfz measured the opposite and I confirmed it —
    the comparison is `first == allowed_runner_registry()` on the segment before
    the first `/`, so against `""` it matches NOTHING, and the one shape that
    could (a leading slash) is refused by `valid_image_ref` first:

        llm-registry:5000/llama-runner:b1 -> False   docker.io/library/alpine:3 -> False
        hub.example.com/x:1               -> False   /leading-slash/x:1         -> False

    So an empty registry closes the door on every runner pull: a box with an old
    `.env` and no hub domain could place no model at all. The fallback keeps such
    a box working exactly as before. (Recording the correction rather than
    quietly rewriting it: a wrong rationale next to a security comparison is read
    as the rule, and the next reader defends against a danger that does not
    exist while missing the real one.)
    """
    explicit = (os.environ.get("LLM_WORKER_RUNNER_REGISTRY") or "").strip()
    if explicit:
        return explicit
    hub = (os.environ.get("LLM_HUB_DOMAIN") or "").strip()
    if hub:
        return hub
    return DEFAULT_RUNNER_REGISTRY


def allowed_runner_registry_source() -> str:
    """WHICH of the three sources answered — `explicit`, `hub-domain`, `fallback`.

    #1860: the value alone cannot be judged. `llm-registry:5000` is a legitimate
    answer when the operator set it, and a silent dead end when the node fell
    back to it: the pull is performed by the HOST's docker daemon, which is not
    on the compose network and cannot resolve a compose service name. Measured
    on the remote worker box-175r during the SCH3 run — the node reported
    `ready`, served its models, and every runner pull died at

        lookup llm-registry on 127.0.0.53:53: server misbehaving

    Health was green over a dead capability, and nothing said so until an
    operator triggered a switch.

    Kept beside `allowed_runner_registry` and reading the same two variables in
    the same order, because the one thing that must never drift is which branch
    the two functions agree was taken.
    """
    if (os.environ.get("LLM_WORKER_RUNNER_REGISTRY") or "").strip():
        return "explicit"
    if (os.environ.get("LLM_HUB_DOMAIN") or "").strip():
        return "hub-domain"
    return "fallback"


#: #1472: where the master publishes its init-built runner images inside the
#: registry (post-install `push_runner_images_to_registry` pushes
#: `<registry>/runners/<local-name>:<tag>`; a consistency test holds both sides
#: to this literal). A node that lacks a class image pulls exactly that ref.
RUNNER_REPO_PREFIX = "runners"


def registry_ref_for(image: str) -> str:
    """The allowed-registry reference for a bare class image such as
    ``llama-vulkan-runner:b9851`` → ``<registry>/runners/llama-vulkan-runner:b9851``.
    Measured before #1472 (0.175 → 0.91): a node without the image let docker
    resolve the bare name against docker.io — 404, and a public-registry reach
    the #307 air-gap rule forbids."""
    return f"{allowed_runner_registry()}/{RUNNER_REPO_PREFIX}/{image}"


def ref_from_allowed_registry(ref) -> bool:
    """True iff ``ref`` is a valid image reference hosted on the allowed registry.

    Registry comparison is on the exact host[:port] prefix followed by '/'. A
    prefix check without the '/' would let `llm-registry:5000evil.example/x`
    through; splitting on the first '/' does not.
    """
    if not valid_image_ref(ref):
        return False
    first, sep, _rest = str(ref).partition("/")
    return bool(sep) and first == allowed_runner_registry()

