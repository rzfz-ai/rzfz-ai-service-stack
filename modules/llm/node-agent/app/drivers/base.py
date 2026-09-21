# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Engine-driver layer (#254 Phase-2 P2-A1).

A *driver* turns a model-load request into a hardware-correct docker
**LaunchSpec** (`build_spec`). A tiny *runtime* layer (`start_engine` /
`stop_engine` / `engine_logs` / `probe_health`) runs that spec against an
INJECTED docker client, so the whole thing unit-tests off-box with a fake —
no real docker daemon, no GPU, no network.

Import-safety: this module imports ONLY the stdlib. The real docker SDK is
never imported here — the caller injects a client (created lazily at wiring
time). That keeps `import app` clean off-box (import-side-effect guard).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("node_agent.drivers")


@dataclass(frozen=True)
class LoadSpecInput:
    """Normalised input to a driver's ``build_spec``."""
    instance_id: str
    model: str
    files: list[str]
    models_volume: str = "llm-models"          # docker volume (or host dir)
    models_mount: str = "/models"              # in-container mount point
    network: str = "razzfazz-stack_default"    # engine joins the stack bridge
    params: dict = field(default_factory=dict)
    task: str = "chat"                         # chat | embed | rerank (serve mode)
    # #549 R1: deployment-pinned runner image. None = the driver's default
    # (constructor arg → env override → built-in, see drivers/images.py). Lets
    # two deployments on one node run DIFFERENT engine builds side by side.
    runner_image: str | None = None


@dataclass(frozen=True)
class LaunchSpec:
    """A docker container launch, hardware+engine specific. Field names map
    onto docker-py ``containers.run`` kwargs in ``start_engine``."""
    engine: str
    image: str
    name: str
    command: list[str]
    environment: dict = field(default_factory=dict)
    devices: list[str] = field(default_factory=list)
    group_add: list[str] = field(default_factory=list)
    volumes: dict = field(default_factory=dict)
    network: str | None = None
    runtime: str | None = None   # e.g. "nvidia" for CUDA GPU passthrough
    security_opt: list[str] = field(default_factory=list)
    shm_size: str | None = None
    mem_limit: str | None = None
    labels: dict = field(default_factory=dict)
    serve_url: str = ""    # OpenAI-compatible endpoint the manager routes to
    health_url: str = ""   # readiness probe URL (200 == ready)


class EngineDriver:
    """Builds a LaunchSpec for one engine on one hardware class. Subclasses
    implement ``build_spec``; the runtime funcs below are engine-agnostic."""

    engine: str = "base"

    def build_spec(self, req: LoadSpecInput) -> LaunchSpec:  # pragma: no cover
        raise NotImplementedError


def pick_gguf_weight(files: list[str]) -> str | None:
    """First .gguf that is NOT an mmproj projector, as its LOCAL leaf name.

    #303: ``files`` carries HuggingFace repo paths, which may include a
    subfolder (``UD-Q1_0/model-00001-of-00010.gguf``). The puller writes every
    weight FLAT into the models mount, so the engine command must reference
    the basename — handing the driver the unflattened path produced
    ``--model /models/UD-Q1_0/...gguf``, a file that does not exist.
    """
    import os as _os
    return next(
        (_os.path.basename(f) for f in files
         if f.lower().endswith(".gguf") and "mmproj" not in f.lower()),
        None,
    )


def pick_mmproj(files: list[str]) -> str | None:
    """The multimodal projector file (local leaf name — see pick_gguf_weight)."""
    import os as _os
    return next((_os.path.basename(f) for f in files if "mmproj" in f.lower()), None)


#: Container label carrying the flattened weight leaf names an engine reads.
#: NODE-9: ``delete_disk_model``'s in-use guard matches a deletion candidate
#: against ``loaded[iid]["files"]`` — but ``_readopt_engines`` rebuilds those
#: records from container LABELS, and no label carried the weight list. So after
#: any node or daemon restart (the exact scenario #293 exists for) the guard read
#: an empty list for every running engine and happily deleted a GGUF a live
#: engine had mmap'd. Persisting the list on the container closes it: the label
#: survives the restart because the container does.
ENGINE_FILES_LABEL = "rzfz.files"


def files_label(files) -> str:
    """The ``rzfz.files`` label value for a launch: the flattened leaf names
    this engine actually reads, comma-joined (NODE-9).

    Leaf names, because weights are written FLAT into the models mount (#303)
    and the in-use guard compares leaves. Empty string for a repo-dir (vLLM)
    deploy, which reads a DIRECTORY and has no flat file list — that case is
    covered by the model-name guard in ``delete_cached_weight`` instead.
    """
    import os as _os
    return ",".join(_os.path.basename(f) for f in (files or []) if f)


def files_from_label(value) -> list[str]:
    """Inverse of ``files_label`` — tolerant of an absent/empty label (an engine
    launched by a pre-NODE-9 agent carries none, and must simply read as
    'unknown file set' rather than breaking re-adoption)."""
    return [p for p in str(value or "").split(",") if p]


def normalize_flag(key: str) -> str:
    """Canonical CLI-flag form: strip leading dashes, underscores → dashes,
    lowercase. ``max_model_len`` / ``--max-model-len`` → ``max-model-len``."""
    return str(key).lstrip("-").replace("_", "-").lower()


# #345: driver-owned contract, NOT operator-tunable — and ENGINE-INDEPENDENT, so
# it lives in its own set that every driver bans. Every driver hardcodes
# `--host 0.0.0.0 --port 8080` and pins serve_url/health_url to :8080, and
# params_to_cli_flags appends AFTER those (last-wins in llama.cpp AND in vLLM's
# argparse), so a params-supplied `port` makes the engine serve on an address the
# health probe never checks — it comes up healthy, fails readiness, and gets
# circuit-broken 120 s later while its logs look perfect. `host` (e.g. 127.0.0.1)
# makes it unreachable from the bridge; `model` would point the engine at a
# different weight than was deployed.
ENGINE_CONTRACT_FLAGS = frozenset({"host", "port", "model"})

# Flags that must never reach a llama.cpp engine, whatever was requested.
# Normalised form (see ``normalize_flag``). The contract set above, plus
#   swa-full — the ~256× SWA-KV host-OOM trap.
LLAMACPP_BANNED_FLAGS = ENGINE_CONTRACT_FLAGS | {"swa-full"}


def normalize_task(task: str | None) -> str:
    """Canonical serve-task: ``chat`` (default) | ``embed`` | ``rerank``.
    Accepts the common aliases (``embedding(s)``, ``reranker``/``reranking``/
    ``score``) so a catalog/preset/UI value maps cleanly."""
    t = (task or "chat").strip().lower()
    if t in ("embed", "embedding", "embeddings"):
        return "embed"
    if t in ("rerank", "reranker", "reranking", "score"):
        return "rerank"
    return "chat"


# #1518 (E5) / #1546: the fleet-token → driver-class mapping and the CUDA class
# set live in `images.py` and are imported here, NOT defined twice. They have to
# sit in a module that loads STANDALONE (by path, without the `app` package):
# the config portal's `expected_images.engine_runner_images()` — the source of
# the offline package's image list and of `rzfz verify-images` — loads
# `images.py` that way, and `base.py` cannot be loaded like that at all
# (measured: `spec.loader.exec_module` raises AttributeError). Before the move
# the verifier had no way to learn that `nvidia` means `cuda`, failed open, and
# an NVIDIA box's package carried no runner image.
from .images import (CUDA_CLASSES, UMA_CUDA_DIALECTS,  # noqa: F401  (re-exports)
                     _HARDWARE_ALIASES, is_unified_memory, normalize_hardware)

def is_cuda_class(hardware: str | None) -> bool:
    """True for every NVIDIA/CUDA hardware class, normalizing first.

    Use this instead of ``== "cuda"`` anywhere a branch means "this box has an
    NVIDIA GPU". An exact compare was the #969 defect (a box registered as
    ``nvidia`` never took the CUDA branch); adding a second CUDA class without
    this helper would reintroduce it for GB10."""
    return normalize_hardware(hardware) in CUDA_CLASSES


# Physical-batch ceiling for pooling (embed/rerank) serve modes. Covers every
# realistic RAG/rerank chunk (cognee/OWUI chunk in the low thousands of tokens)
# while bounding the non-causal compute buffer so a deployment that pins a large
# ctx (e.g. 32768) does not blow VRAM/GTT on a memory-constrained box. An
# operator who needs to embed longer single inputs raises `ubatch-size`/
# `batch-size` explicitly — which is honoured (see `llamacpp_task_flags`).
POOLING_BATCH_CAP = 8192
#: llama.cpp's own default n_ctx, used when the deployment pins no ctx.
_LLAMACPP_DEFAULT_CTX = 4096


def _pooling_batch_target(params: dict | None) -> int:
    """Physical/logical batch size for a pooling engine: cover the deployment's
    context so any in-context input embeds in ONE forward pass, capped at
    ``POOLING_BATCH_CAP``. Falls back to llama.cpp's default n_ctx when the
    deployment pins no ``ctx-size``."""
    ctx = None
    for k, v in (params or {}).items():
        if normalize_flag(k) in ("ctx-size", "c"):
            try:
                ctx = int(v)
            except (TypeError, ValueError):
                ctx = None
            break
    if not ctx or ctx <= 0:
        ctx = _LLAMACPP_DEFAULT_CTX
    return min(ctx, POOLING_BATCH_CAP)


def llamacpp_task_flags(task: str | None, params: dict | None = None) -> list[str]:
    """Engine flags that make a llama.cpp ``llama-server`` serve a NON-chat
    task. A plain chat deploy adds nothing (back-compat).

      embed  → ``--embeddings`` (+ ``--pooling mean``)
      rerank → ``--reranking``  (+ ``--pooling rank``)

    Without ``--embeddings`` the server answers ``/v1/embeddings`` with
    "this server does not support embeddings" — the exact 0.91 playground gap.

    Pooling models are NON-CAUSAL: the whole input is embedded in a SINGLE
    forward pass, so any single input longer than the physical batch
    (``-ub``/``--ubatch-size``) is REJECTED — ``input (N tokens) is too large to
    process. increase the physical batch size``. llama.cpp defaults that batch
    to 512, which silently fails every realistic RAG/rerank chunk; downstream
    the LiteLLM router then cools the deployment down and the whole embed path
    returns 429. So for embed/rerank we default ``--batch-size`` and
    ``--ubatch-size`` to cover the deployment's context (see
    ``_pooling_batch_target``). Chat is left alone: it is causal/incremental and
    the 512 default is correct there.

    Explicit params win: a flag the operator already set in ``params`` is not
    re-added here (so an override pooling / manual ``--embeddings`` /
    hand-tuned batch sizes all stand)."""
    t = normalize_task(task)
    if t == "chat":
        return []
    have = {normalize_flag(k) for k in (params or {})}
    flags: list[str] = []
    if t == "embed":
        if "embeddings" not in have:
            flags.append("--embeddings")
        if "pooling" not in have:
            flags += ["--pooling", "mean"]
    else:  # rerank
        if "reranking" not in have:
            flags.append("--reranking")
        if "pooling" not in have:
            flags += ["--pooling", "rank"]
    # Physical/logical batch must cover the input for these non-causal modes.
    # Only fill in a size the operator did not pin (long or short flag form).
    target = _pooling_batch_target(params)
    if not (have & {"batch-size", "b"}):
        flags += ["--batch-size", str(target)]
    if not (have & {"ubatch-size", "ub"}):
        flags += ["--ubatch-size", str(target)]
    return flags


def llamacpp_command(
    model_path: str,
    *,
    ngl: int,
    mmproj: str | None = None,
    params: dict | None = None,
    binary: str | None = None,
    task: str = "chat",
) -> list[str]:
    """Assemble the shared llama.cpp-server argv used by both the AMD (GPU,
    ``ngl=999``) and CPU (``ngl=0``) drivers. Carries the hard-won tuning:
    host prompt-cache OFF (``--cache-ram 0 --ctx-checkpoints 0``) and the
    ``--swa-full`` ban. ``--mmproj`` is appended for vision models; ``task``
    (chat|embed|rerank) adds the serve-mode flags (see ``llamacpp_task_flags``).

    ``binary`` leads the argv when set — the razzfazz llama.cpp runner images
    use a ``tini --`` entrypoint, so the server binary (``llama-server``) must
    be the first arg or tini tries to exec ``--model`` (exit 127). Omit for
    images whose entrypoint is already the binary."""
    cmd = ([binary] if binary else []) + [
        "--model", model_path,
        "--host", "0.0.0.0",
        "--port", "8080",
        "-ngl", str(ngl),
        "--cache-ram", "0",       # host prompt-cache OFF (RAM-exhaustion trap)
        "--ctx-checkpoints", "0",
        "--jinja",                # use the model's chat template
    ]
    if mmproj:
        cmd += ["--mmproj", mmproj]
    cmd += llamacpp_task_flags(task, params)
    cmd += params_to_cli_flags(params or {}, banned=LLAMACPP_BANNED_FLAGS)
    return cmd


# llama.cpp flag quirks the structured editor's param keys must map onto:
#  - alias: our key → the real llama.cpp flag (n_parallel→parallel, temperature→temp).
#  - VALUED bool: --flash-attn takes on|off, NOT a bare presence flag (a bare
#    "--flash-attn" makes llama.cpp consume the NEXT arg as its value → crash).
_FLAG_ALIASES = {"n-parallel": "parallel", "temperature": "temp"}
_VALUED_BOOL_FLAGS = frozenset({"flash-attn"})


def params_to_cli_flags(params: dict, *, banned: frozenset = frozenset()) -> list[str]:
    """Translate a param dict into CLI flags shared by all engines. A VALUED
    bool (e.g. flash-attn) becomes ``--flag on|off``; a plain bool a bare flag
    (only when true); everything else ``--flag value``. Keys are normalised +
    aliased to the real engine flag; ``banned`` flags are dropped with a warning
    (e.g. the AMD/Strix ``--swa-full`` host-OOM trap)."""
    flags: list[str] = []
    for raw_key, value in (params or {}).items():
        key = _FLAG_ALIASES.get(normalize_flag(raw_key), normalize_flag(raw_key))
        if key in banned:
            logger.warning("dropping banned engine flag %r (unsafe on this hardware)", raw_key)
            continue
        if key in _VALUED_BOOL_FLAGS:
            flags += [f"--{key}", ("on" if value else "off") if isinstance(value, bool) else str(value)]
        elif isinstance(value, bool):
            if value:
                flags.append(f"--{key}")
        elif isinstance(value, (dict, list)):
            # A structured value (e.g. vLLM --override-generation-config /
            # --hf-overrides) MUST be serialised as JSON, not Python repr —
            # str({"a": 1}) yields `{'a': 1}` (single quotes), which the engine's
            # json.loads rejects ("cannot be converted"). json.dumps gives valid
            # JSON the flag parser accepts (also valid for the flags that use
            # ast.literal_eval, since double-quoted JSON is a valid Python literal).
            flags += [f"--{key}", json.dumps(value, separators=(",", ":"))]
        else:
            flags += [f"--{key}", str(value)]
    return flags


# --- runtime layer (operates on an injected docker client) ------------------
#: Restart policy for socket-launched engines (#342).
#:
#: Every long-lived service in the stack sets `restart: unless-stopped` in its
#: compose file. Engines are launched through the docker socket, not compose, so
#: they inherited NO policy — a daemon restart or host reboot left them stopped,
#: the node re-adopted nothing, and the fleet came back empty with no error
#: anywhere. `unless-stopped` (not `always`) so a deliberate `stop_engine` stays
#: stopped rather than being resurrected by the daemon.
ENGINE_RESTART_POLICY = {"Name": "unless-stopped"}


#: CA-trust env vars an engine needs to trust the corporate TLS-intercept CA.
#: Same six as scripts/gen-corporate-proxy-overlay.py::CA_ENV_VARS and
#: modules/agents/manager/app/services/docker_client.py's #281 fix — one
#: constant set, never a locally-invented subset.
CORPORATE_CA_ENV_KEYS = (
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO", "PIP_CERT", "NODE_EXTRA_CA_CERTS",
)


def _inject_corporate_proxy(environment: dict, volumes: dict) -> tuple[dict, dict]:
    """#283 — mirror #281 (agent-manager's socket-spawned per-user agents):
    an engine launched here via the docker socket is not a compose SERVICE,
    so the compose corporate-proxy overlay (Level C) never reaches it. The
    SPAWNER (worker-agent) must instead forward the SAME proxy routing + CA
    trust it received itself (worker-agent is expected to sit in the overlay's
    egress allow-list, exactly as agent-manager does) onto every engine it
    launches. No-op when the box is not proxied.

    CA env and CA MOUNT travel together (the empty-dir x509 trap: Docker
    creates a directory at a missing bind target and the TLS stack fails on
    "not a file"), and the bind SOURCE is a HOST path — the daemon behind
    docker-socket-proxy resolves it on the host filesystem, not inside the
    worker-agent container. Driver-provided ``environment`` (the LaunchSpec an
    engine driver already built) wins over these box-wide defaults.
    """
    if os.environ.get("RAZZFAZZ_CORPORATE_PROXY") != "1":
        return environment, volumes

    inject = {
        "HTTP_PROXY": os.environ.get("HTTP_PROXY", ""),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),
        "http_proxy": os.environ.get("http_proxy", os.environ.get("HTTP_PROXY", "")),
        "https_proxy": os.environ.get("https_proxy", os.environ.get("HTTPS_PROXY", "")),
        "NO_PROXY": os.environ.get("NO_PROXY", ""),
        "no_proxy": os.environ.get("no_proxy", os.environ.get("NO_PROXY", "")),
    }
    volumes = dict(volumes)
    stack_host = os.environ.get("STACK_HOST_PATH", "")
    if stack_host:
        ca_in = os.environ.get("SSL_CERT_FILE") or "/certs/caddy-ca.pem"
        volumes["%s/certs/caddy-ca.pem" % stack_host] = {
            "bind": ca_in, "mode": "ro",
        }
        for key in CORPORATE_CA_ENV_KEYS:
            inject[key] = ca_in
    else:
        logger.warning(
            "RAZZFAZZ_CORPORATE_PROXY=1 but STACK_HOST_PATH unset — "
            "skipping CA bind for engine (proxy env still injected)")

    # Driver-provided env wins; only fill in vars we actually have a value for.
    environment = {**{k: v for k, v in inject.items() if v}, **environment}
    return environment, volumes


def ensure_engine_image(image: str, docker) -> str:
    """#1472: make sure ``image`` (a bare class image, e.g.
    ``llama-vulkan-runner:b9851``) exists on this node BEFORE ``containers.run``
    — docker-py's ``run()`` pulls a missing image itself, from docker.io, which
    (a) 404s for our runners and (b) is the public-registry reach #307 forbids.
    Measured on 0.175 (2026-09-05): `images/create?fromImage=llama-vulkan-runner`
    → 404, engine launch failed, deployment stuck.

    Missing → pull ``<allowed registry>/runners/<image>`` with the node's
    registry credential (#571/#1408) and tag it back to the bare name the
    drivers use. A pull that fails is a hard, explained failure — never a
    fall-through to docker's own resolution. Returns "present" | "pulled".

    Fakes without an ``images`` attribute (older driver tests) are treated as
    present: the real docker-py client always has it."""
    images = getattr(docker, "images", None)
    if images is None:
        return "present"
    try:
        images.get(image)
        return "present"
    except Exception:
        pass
    from .images import registry_ref_for, runner_pull_auth_config
    ref = registry_ref_for(image)
    repo, _, tag = ref.rpartition(":")
    if "/" in tag or not repo:
        repo, tag = ref, None
    try:
        pulled = images.pull(repo, tag=tag, auth_config=runner_pull_auth_config())
        local_repo, _, local_tag = image.rpartition(":")
        pulled.tag(local_repo or image, local_tag or None)
    except Exception as exc:  # noqa: BLE001 — explained, then fatal
        raise RuntimeError(
            f"engine image {image!r} is not on this node and could not be pulled from "
            f"{ref!r}: {exc}. The master publishes its runner images into the hub at "
            f"post-install (runners/…, #1472); this node never pulls from a public "
            f"registry (#307).") from exc
    logger.info("engine image %s pulled from %s and tagged locally", image, ref)
    return "pulled"


def start_engine(spec: LaunchSpec, docker):
    """Launch the engine container from ``spec``. ``docker`` is a docker-py
    (or fake) client; returns the container handle."""
    ensure_engine_image(spec.image, docker)
    environment, volumes = _inject_corporate_proxy(spec.environment, spec.volumes)
    return docker.containers.run(
        spec.image,
        restart_policy=ENGINE_RESTART_POLICY,
        command=spec.command,
        name=spec.name,
        detach=True,
        environment=environment,
        devices=spec.devices,
        group_add=spec.group_add,
        volumes=volumes,
        network=spec.network,
        runtime=spec.runtime,
        security_opt=spec.security_opt,
        shm_size=spec.shm_size,
        mem_limit=spec.mem_limit,
        # #674 review: engines carry razzfazz.managed=true like every other
        # socket-provisioned container — the rzfz lifecycle verbs stop/
        # revive them and the #670 orphan guard protects them. Central here
        # (not per driver) so no hardware class can forget it. Pre-existing
        # engines gain the label on their next (re)launch.
        labels={**(spec.labels or {}), "razzfazz.managed": "true"},
    )


def stop_engine(name: str, docker, *, timeout: int = 20) -> bool:
    """Stop + remove the engine container. Idempotent: a missing container is
    a no-op returning False (never raises), so unload/drain is safe to retry."""
    try:
        container = docker.containers.get(name)
    except Exception:
        return False
    try:
        container.stop(timeout=timeout)
    except Exception:  # already stopped / gone — still try to remove
        logger.warning("stop_engine: stop(%s) failed; removing anyway", name)
    try:
        container.remove(force=True)
    except Exception:
        # #343: remove() was previously unguarded, so a container that vanished
        # between get() and remove() (operator unload racing the supervisor, or
        # docker reaping it) raised straight out of stop_engine — breaking the
        # documented "never raises" contract and aborting the supervisor tick for
        # every instance after this one.
        logger.warning("stop_engine: remove(%s) failed (already gone?)", name, exc_info=True)
        return False
    return True


def engine_logs(name: str, docker, *, tail: int = 200):
    return docker.containers.get(name).logs(tail=tail)


#: Why an engine version is missing. A CODE, not a sentence (#1932): the
#: console and any later check can branch on it, and a wording change cannot
#: silently break a reader. Same convention as `switch_interruption`'s `why`
#: (#1926), for the same reason — five different situations used to come out of
#: this probe as one empty cell, and each of them asks for a different action:
#:
#:   probed          a version was read
#:   not-applicable  this engine has no such string (vLLM); nothing to fetch
#:   no-image        the driver named no image; a config question, not a pull
#:   image-absent    the image is not on this node — PULL IT. The likely state
#:                   of a freshly enrolled worker, and the one an operator can
#:                   act on immediately.
#:   probe-failed    the one-shot run failed for another reason (docker socket
#:                   unreachable, binary missing in the image) — fix the node,
#:                   not the image.
#:   no-version-line the run SUCCEEDED and printed nothing recognisable —
#:                   upstream changed its output format. Distinct from every
#:                   failure above: nothing is broken on this box.
ENGINE_VERSION_WHY = (
    "probed", "not-applicable", "no-image", "image-absent", "probe-failed",
    "no-version-line",
)


def probe_engine_version_detail(image: str, docker, *, binary: str = "llama-server"):
    """→ ``{"version": str|None, "why": <one of ENGINE_VERSION_WHY>}``.

    Runs ``<binary> --version`` in a one-shot ``--remove`` container; the image
    entrypoint (``tini --``) execs the leading binary, so the command is
    ``[binary, '--version']``. NEVER raises — a probe failure yields a `None`
    version, and the node still registers.

    WHAT CHANGED AND WHY (#1932). This used to return the string or `None`, and
    `None` meant any of five things. Measured on 0.91 (2026-09-10): the thin
    node `gb10-191` reported `ready` with an EMPTY engine_version, and from the
    manager that is indistinguishable from a broken probe — so the question
    "must I raise this worker for fix X to apply?" was unanswerable for exactly
    the node shape the 2026.09 cutover targets (#266 E7).

    `image-absent` is split out from `probe-failed` deliberately, because it is
    the difference between "pull the image" and "fix the node". It is detected
    on the exception's CLASS NAME rather than by importing docker's error
    types: this module is imported off-box, where the docker SDK need not be
    installed, and the existing code in this tree lazy-imports it for that
    reason. A name check costs nothing and adds no import.
    """
    try:
        out = docker.containers.run(
            image, [binary, "--version"], remove=True, stdout=True, stderr=True
        )
    except Exception as exc:                                  # noqa: BLE001
        absent = type(exc).__name__ in ("ImageNotFound", "NotFound")
        return {"version": None,
                "why": "image-absent" if absent else "probe-failed"}
    try:
        text = out.decode("utf-8", "ignore") if isinstance(out, (bytes, bytearray)) else str(out)
        for line in text.splitlines():
            if "version" in line.lower():
                return {"version": line.strip()[:120], "why": "probed"}
        text = text.strip()
        if text:
            return {"version": text[:120], "why": "probed"}
        return {"version": None, "why": "no-version-line"}
    except Exception:                                         # noqa: BLE001
        # Decoding an unexpected payload must not cost the node its
        # registration — the whole point of a best-effort probe.
        return {"version": None, "why": "probe-failed"}


def probe_engine_version(image: str, docker, *, binary: str = "llama-server"):
    """The version alone, for callers that do not care why it is missing."""
    return probe_engine_version_detail(image, docker, binary=binary)["version"]


def probe_health(health_url: str, *, http_get) -> bool:
    """Real readiness probe: ready ONLY when the engine's health endpoint
    returns HTTP 200. llama.cpp's /health is 503 while the model is still
    loading and 200 once it can serve — so this is true readiness, not just
    'container is up'. Any transport error (not up yet) → not ready."""
    try:
        return int(http_get(health_url)) == 200
    except Exception:
        return False
