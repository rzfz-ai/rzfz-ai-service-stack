# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""llm-worker-agent (Phase-2) — per-node worker agent (FastAPI SKELETON).

Spec §7. A lightweight service on each fleet node; the manager drives it
remotely. This is the endpoint skeleton (§7): the real engine drivers
(llama-swap/vLLM/Ollama), the atomic multi-file pull, and the per-vendor GPU
exporter are Phase-2 box work and are stubbed behind INJECTABLE providers so
the endpoint contract unit-tests off-box.

Endpoints:
  GET  /health          liveness + loaded instances
  GET  /gpu             GPU/VRAM (injectable probe; 'unknown' off-box)
  GET  /metrics         Prometheus (loaded-model gauge)
  POST /models/pull     atomic multi-file pull (weights + optional mmproj)
  POST /models/load     start an instance (AMD: auto --mmproj when present)
  POST /models/unload   stop an instance (drain / rolling update)

MUST be import-safe off-box: no docker client, no GPU probe, no FileHandler
at import; providers resolve lazily from app.state with safe defaults.
"""
from __future__ import annotations

from typing import Optional

import hmac
import os
import re
import time

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

__version__ = "0.1.0-phase2-skeleton"


class PullRequest(BaseModel):
    source: str
    repo_id: str
    artifact_id: str
    files: list[str] = []


class LoadRequest(BaseModel):
    instance_id: str
    model: str
    artifact_id: Optional[str] = None
    files: list[str] = []
    params: dict = {}
    task: str = "chat"                   # chat | embed | rerank (serve mode)
    hf_repo: Optional[str] = None        # #287: HF source to fetch missing weights from
    # #307 S2: files already mirrored into the in-stack Zot registry, by digest
    # — [{"name","digest"}], the exact shape pusher.push_artifact returns and
    # puller.pull_artifact consumes. A missing file covered here pulls from the
    # registry (LAN, offline-safe) INSTEAD of HuggingFace; hf_repo stays the
    # upstream of first resort for whatever this list doesn't cover.
    registry_files: list[dict] = []
    # #549 R1: the runner image this deployment is pinned to. None = this node's
    # default for its hardware class (drivers/images.py).
    runner_image: Optional[str] = None
    # #3403: the runner CHANNEL — None/"stable" = this node's default runner;
    # "experimental" = the experimental build of this node's hardware line
    # (drivers/images.py experimental_engine_image). An explicit runner_image
    # above always wins over the channel.
    runner_channel: Optional[str] = None
    # #3505: the master's opt-in for the experimental runner. A weight whose
    # architecture only that runner loads is routed to it when True, and
    # refused with the reason otherwise (None = an older manager: refused).
    experimental_runner_allowed: Optional[bool] = None
    # #2393: the admission charge (GB) the manager computed for this model — the
    # engine's container memory limit derives from it. None = not known / older manager.
    mem_budget_gb: Optional[float] = None
    # The engine this deployment wants. Meaningful only where a hardware class
    # runs more than one engine — a ``cuda`` worker can serve vLLM (default) or a
    # GGUF via llama.cpp. None (the manager omits it, or an older manager) keeps
    # the per-hardware default: vLLM on cuda, llama.cpp on amd/cpu. See
    # drivers.select_driver.
    engine: Optional[str] = None
    # #2392: the catalogue pins this deployment was approved with —
    # {"revision": <HF commit>, "sha256": {<basename>: <hex>}}. {} = not a
    # catalogue model (or an older manager): the pre-#2392 behaviour exactly.
    pins: dict = {}


def _pin_map(payload) -> dict:
    """#2392: ``{basename: sha256}`` from a LoadRequest's pins."""
    return dict(((getattr(payload, "pins", None) or {}).get("sha256") or {}))


class UnloadRequest(BaseModel):
    instance_id: str


def _now() -> float:
    """Monotonic wall clock for supervision timestamps (import-safe)."""
    import time

    return time.monotonic()


def _default_gpu_probe() -> dict:
    """Off-box safe default. A real node injects a vendor probe
    (NVIDIA: DCGM/nvidia-smi; AMD: amd-smi/rocm-smi; Mac: host)."""
    return {"vendor": "unknown", "available": False, "devices": []}


import logging as _logging

_load_logger = _logging.getLogger("node_agent.load")

# #574: engines that serve an unpacked HF repo DIRECTORY instead of a single
# GGUF file — `files == []` plus `hf_repo` is that deploy convention, and it
# changes both the pull (whole repo, summed-size preflight) and the presence
# probe. #1518 (E5) retired the only such engine (vLLM), so on a shipped box no
# driver produces a spec that lands here; the value is kept so a deployment
# record written before #1518 keeps its original semantics, and so the next
# directory-serving engine costs one entry rather than a re-derivation.
DIR_SERVING_ENGINES = frozenset({"vllm"})


#: #1633 — the byte stream's clock. It used to be `timeout=None`, which in httpx
#: switches off connect, read, write AND pool: a CDN connection that goes quiet
#: mid-transfer blocks the read for the life of the process. Measured on 0.79 —
#: `mirror_model` claimed for 1½ hours, two threads parked in `wait_woken`, zero
#: bytes written anywhere, the deployment stuck on `pulling` and never a word
#: about it. #325 removed the same `timeout=None` from the manager's proxy; the
#: one path that pulls gigabytes over somebody else's CDN kept it.
#:
#: READ is the one that matters and it is generous on purpose: it bounds the gap
#: BETWEEN chunks, not the transfer, so a 30 GB model over a slow link is fine as
#: long as bytes keep arriving. Five minutes of total silence is not a slow link,
#: it is a dead socket. Failing there is safe because the fetch resumes: files
#: are written atomically to a temp name and `ensure_file` skips a complete file,
#: so a re-issued command continues rather than starting over.
WEIGHT_FETCH_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0,
                                     write=300.0, pool=30.0)


#: mkstemp's name shape: the literal prefix plus exactly eight characters from
#: its own alphabet, and no suffix. Matching THIS rather than "starts with tmp"
#: is what keeps a weight called `tmp-model.gguf` — or a #574 repo directory —
#: out of the sweep's way.
_MKSTEMP_NAME = re.compile(r"^tmp[A-Za-z0-9_]{8}$")


def sweep_orphan_weight_temps(mount: str) -> int:
    """Remove half-written weights nobody owns any more. Returns bytes freed.

    #1633, measured on 0.79: after the stalled mirror, `/models` held 12.8 GB in
    `tmpfoz1qkiy` and `tmprohpeo7n` — the two files the transfer was streaming
    when it stopped. `download_stream` removes its temp file on any exception,
    but a HANG raises nothing and a hard kill (OOM, `docker stop`, reboot) never
    gives it the chance. The read timeout ends the hang; this ends the rest.

    Safe to run unattended at every start because of WHEN it runs: this agent is
    the only writer of this mount and it has just started, so any temp file here
    predates it and can belong to no transfer of its own. It never recurses and
    never touches a directory — a #574 repo-dir model IS a directory at the
    mount root.
    """
    freed = 0
    try:
        entries = os.listdir(mount)
    except OSError:
        return 0            # no mount yet: never a reason not to come up
    for name in entries:
        if not _MKSTEMP_NAME.match(name):
            continue
        path = os.path.join(mount, name)
        try:
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            size = os.path.getsize(path)
            os.unlink(path)
        except OSError as exc:
            _load_logger.warning("could not sweep orphan temp %s: %s", path, exc)
            continue
        freed += size
        _load_logger.warning(
            "swept orphaned weight temp %s (%.1f GB) — a transfer died before it "
            "could clean up (#1633)", path, size / 1e9)
    return freed


#: Statuses that mean "ask again", not "you asked wrong" (#2367).
_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})


def _http_stream(url, start: int = 0):
    """Stream an HTTP GET as (chunk, total_or_None). Proxy-aware (httpx honours
    HTTPS_PROXY, so this rides the corporate proxy in proxied mode).

    #2367: with ``start`` > 0 the request carries ``Range: bytes=<start>-`` and
    the yielded total is the REMAINDER; a host that answers 200 to a Range
    request gets ``RangeIgnored`` raised before the first chunk, so the caller
    drops its partial instead of appending a whole file to it. A 429/5xx raises
    ``TransientHTTPError`` (retried by the caller); any other error status is
    raised as before.

    #1409: an EXTERNAL fetch — weights come from Hugging Face or the operator's
    mirror — so trust_env stays on. It is spelled out rather than left to
    httpx's default, because a top-level `httpx.stream()` builds an implicit
    trust_env=True client and reads, on the page, like a decision nobody made.
    The in-network calls in runtime.py say False for the same reason."""
    from app.hf_pull import RangeIgnored, TransientHTTPError

    headers = {"Range": f"bytes={int(start)}-"} if start else None
    with httpx.Client(trust_env=True, follow_redirects=True,
                      timeout=WEIGHT_FETCH_TIMEOUT) as c:
        with c.stream("GET", url, headers=headers) as r:
            code = getattr(r, "status_code", None)
            if code in _TRANSIENT_HTTP:
                raise TransientHTTPError(f"HTTP {code} from {url}")
            if start and code == 200:
                raise RangeIgnored(f"{url}: Range ignored (200 instead of 206)")
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0) or None
            for chunk in r.iter_bytes(1024 * 1024):
                yield chunk, total


def _hold_pending(app, iid, detail) -> None:
    """#2393: the header check could not run (lock busy): the load stays
    pending with the reason. Never a `failed`, and never a record for an
    instance that was unloaded meanwhile."""
    rec = app.state.loaded.get(iid)
    if rec is not None:
        rec.update({"status": "pulling", "detail": detail})


class GGUFRefused(RuntimeError):
    """A GGUF of the load failed the structural check (#2393); ``str`` is the
    full operator-facing detail."""


class RunnerArchitectureRefused(Exception):
    """#3505: the weight's architecture is one only the experimental runner
    loads, and this load may not go there. ``str`` is the operator-facing
    reason (the deployment's last_error)."""


def _fit_runner_to_architecture(app, payload, spec, archs):
    """#3505: the runner a load gets follows the weight's GGUF architecture.

    The catalogue routes a model to the experimental runner by repository;
    a copy from another repository (unsloth's Inkling 2 Mini GGUFs) reached the
    stable runner, whose llama.cpp does not know `inkling2_loop`, and the engine
    crash-looped with nothing on the deployment. Called right after the header
    check at every launch site, so the decision rests on the file itself:

    * the architecture is not experimental-only, or the image already is the
      experimental runner (channel or pin): the spec as it is;
    * no explicit pin and the master allows the experimental runner: the same
      spec on this node's experimental image (the report carries it; the master
      records the channel from it);
    * otherwise: refused with the reason, before any container starts.
    """
    import dataclasses

    from app.drivers.images import (EXPERIMENTAL_ONLY_ARCHITECTURES,
                                    experimental_engine_image, is_experimental_image)
    needs = sorted({a for a in (archs or {}).values() if a in EXPERIMENTAL_ONLY_ARCHITECTURES})
    if not needs or is_experimental_image(getattr(spec, "image", None)):
        return spec
    arch = needs[0]
    if payload.runner_image:
        raise RunnerArchitectureRefused(
            f"refusing to load: the weight's architecture {arch} is not supported by the pinned "
            f"runner {payload.runner_image} — it needs the experimental runner. Pin an "
            f"experimental runner or clear the pin (#3505)")
    if not getattr(payload, "experimental_runner_allowed", None):
        raise RunnerArchitectureRefused(
            f"refusing to load: the weight's architecture {arch} needs the experimental runner, "
            f"which is off on this box — set RAZZFAZZ_EXPERIMENTAL_RUNNER=true and run "
            f"'rzfz post-install --refresh' on the master, or remove the deployment (#3505)")
    try:
        image = experimental_engine_image(app.state.hardware)
    except ValueError as exc:   # an unknown line, or a GPU no build is for (UnknownCudaCapability)
        raise RunnerArchitectureRefused(
            f"refusing to load: the weight's architecture {arch} needs the experimental runner, "
            f"and there is no experimental runner for this node's hardware "
            f"({app.state.hardware}): {exc} (#3505)") from None
    _load_logger.info("%s: architecture %s needs the experimental runner — launching %s (#3505)",
                      payload.instance_id, arch, image)
    return dataclasses.replace(spec, image=image)


def _assert_gguf_sound(models_mount, files) -> dict:
    """#2393: THE check, called immediately before every ``start_engine`` of a
    load (``_launch_now``, the pull thread, the #2367 late launch). Raises
    GGUFRefused for the first malformed ``.gguf`` (weight or projector, by
    store name). Header-only and bounded (``gguf_check``), never hashes, never
    deletes or moves the file — but it is NOT cheap enough for the report
    loop (seconds for a big weight): callers run it off-loop. A file that is
    absent or unreadable is skipped alone; the engine reports it."""
    from app.gguf_check import GGUFRejected, check_gguf

    archs: dict = {}
    for f in files or []:
        path = os.path.join(models_mount, f)
        if not f.lower().endswith(".gguf"):
            continue
        try:
            if os.path.isfile(path):
                archs[os.path.basename(f)] = check_gguf(path)
        except GGUFRejected as exc:
            raise GGUFRefused(f"refusing to load: {os.path.basename(f)}: {exc} (#2393)") from exc
        except OSError:
            continue
    # #3505: what each weight's header names as its architecture.
    return archs


def _fill_unknown_mem_limit(spec, models_mount, files) -> None:
    """#2393: an engine whose admission charge the manager never recorded
    (``mem_budget_gb`` None — a deployment created offline carries no estimate)
    must not run unlimited. Size the limit from the weight files on this node's
    own volume instead. ``LLM_ENGINE_MEM_LIMIT=off`` stays an explicit opt-out."""
    from app.drivers.base import engine_mem_limit, weights_budget_gb
    from app.registration import weights_gb

    if getattr(spec, "mem_limit", None):
        return
    if os.environ.get("LLM_ENGINE_MEM_LIMIT", "").strip().lower() == "off":
        return
    budget = weights_budget_gb(weights_gb(models_mount, files))
    if budget:
        # LaunchSpec is frozen; the SAME object is handed to the supervisor, so a
        # restart keeps the limit. Setting the one field in place is deliberate.
        object.__setattr__(spec, "mem_limit", engine_mem_limit(budget))


def _store_files(payload) -> list:
    from app.hf_pull import store_name
    return [store_name(f, payload.hf_repo) for f in payload.files or []]


# How long the report loop waits for the off-loop check+launch before it
# answers "pulling" (the instance then reads `pulling: verifying` until done).
_LAUNCH_GRACE_S = 0.05


def _launch_off_loop(app, payload, spec, docker, mmproj, base_rec):
    """#2393: the check + launch of an all-present load run in a thread, never
    on the report loop. A quick finish is reported as-is (same contract as
    ``_launch_now``, including its 502); a slow one answers `pulling`."""
    import threading

    iid = payload.instance_id
    app.state.loaded[iid] = {**base_rec, "status": "pulling", "detail": "verifying header"}
    box: dict = {}

    def run():
        try:
            box["out"] = _launch_now(app, payload, spec, docker, mmproj, base_rec, require_record=True)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller or recorded
            box["exc"] = exc
            rec = app.state.loaded.get(iid)
            if rec is not None and rec.get("status") == "pulling":
                rec.update({"status": "failed", "detail": f"engine launch failed: {str(exc)[:200]}"})

    t = threading.Thread(target=run, name=f"launch-{iid}", daemon=True)
    t.start()
    t.join(_LAUNCH_GRACE_S)
    if t.is_alive():
        return {"status": "pulling", "instance_id": iid, "endpoint": spec.serve_url, "mmproj": mmproj}
    if "exc" in box:
        raise box["exc"]
    return box["out"]


def _launch_now(app, payload, spec, docker, mmproj, base_rec, *, require_record=False):
    """Weights present → start the engine + supervise it (status flows via the
    supervisor: loading → ready / failed). Runs the #2393 GGUF check first —
    call it off the report loop (``_launch_off_loop``)."""
    from app import drivers
    from app.drivers.base import start_engine_retrying
    from app.gguf_check import GGUFBusy

    refusal = None
    try:
        _archs = _assert_gguf_sound(getattr(app.state, "models_mount", "/models"),
                                    base_rec.get("files"))
        _before = getattr(spec, "image", None)
        spec = _fit_runner_to_architecture(app, payload, spec, _archs)
        if getattr(spec, "image", None) != _before:
            base_rec = {**base_rec, "runner_image": spec.image, "runner_by_architecture": True}
        _fill_unknown_mem_limit(spec, getattr(app.state, "models_mount", "/models"),
                                base_rec.get("files"))
    except (GGUFRefused, RunnerArchitectureRefused) as exc:
        refusal = str(exc)
    except GGUFBusy as exc:
        _hold_pending(app, payload.instance_id, str(exc))
        return {"status": "pulling", "instance_id": payload.instance_id,
                "endpoint": spec.serve_url, "mmproj": mmproj, "detail": str(exc)}
    if require_record and payload.instance_id not in app.state.loaded:
        # unloaded while the header was being checked: neither a refusal record
        # nor an engine may resurrect the instance
        return {"status": "cancelled", "instance_id": payload.instance_id}
    if refusal:
        app.state.loaded[payload.instance_id] = {**base_rec, "status": "failed", "detail": refusal}
        return {"status": "failed", "instance_id": payload.instance_id, "detail": refusal}

    try:
        # #2447: a transport stall right after a large staging write is retried
        # here, where the spec and the client are at hand; a verdict is not.
        # `drivers.start_engine` is looked up now, so the seam tests patch stays.
        container = start_engine_retrying(spec, docker, start=drivers.start_engine)
    except Exception as exc:  # engine launch failed on the node
        app.state.loaded[payload.instance_id] = {
            **base_rec, "status": "failed", "detail": f"engine launch failed: {str(exc)[:200]}"}
        raise HTTPException(status_code=502, detail=f"engine launch failed: {exc}")
    app.state.loaded[payload.instance_id] = {
        **base_rec, "container_id": getattr(container, "id", None),
        "launched": True, "status": "loading", "detail": None}
    supervisor = getattr(app.state, "supervisor", None)
    if supervisor is not None:
        supervisor.supervise(spec, now=_now())
    _auto_cache_deploy(app, payload)   # #307 seed the master cache (background)
    return {"status": "loading", "instance_id": payload.instance_id,
            "endpoint": spec.serve_url, "mmproj": mmproj}


def _assert_no_flatten_collisions(files) -> None:
    """#303: weights are written FLAT into the models mount, so two entries
    whose basenames collide (same leaf in different repo subfolders) would
    overwrite each other and the engine would serve a mix. Refuse loudly —
    used by both write paths (deploy pull and the #307 mirror), because they
    share the flattening semantics (review #719).
    """
    import os as _os

    seen: dict = {}
    for f in files or []:
        seen.setdefault(_os.path.basename(f), []).append(f)
    clash = {k: v for k, v in seen.items() if len(v) > 1}
    if clash:
        raise ValueError(
            "#303: this file set flattens to colliding names in the models "
            f"volume: {clash} — refused instead of silently overwriting weights")


def _unconfirmed_pins(models_mount, payload) -> list:
    """#2392: the files of ``payload`` that are present and pinned but carry
    no sidecar vouching for the pin. Cheap (stat + a 65-byte read per file)
    — safe on the report loop; the HASH happens in the pull thread.
    #2431: looked up under the STORE name (``hf_pull.pending_verification``)."""
    from app.hf_pull import pending_verification
    return pending_verification(models_mount, payload.files, _pin_map(payload),
                                repo_id=payload.hf_repo)


def _missing_now(models_mount, files, repo_id=None):
    """The weights of ``files`` NOT on the volume at this moment (#2367),
    under their store names (#2431)."""
    from app.hf_pull import missing_files
    return missing_files(models_mount, files, repo_id=repo_id)


def _verify_present(app, payload, models_mount, unverified) -> list:
    """#2392: hash every present-but-unverified pinned file (in the pull thread
    — a 37 GB weight takes minutes, and the report loop that runs perform_load
    must keep beating). A match writes the sidecar; a mismatch is moved aside
    (``hf_pull.set_aside`` — never deleted) and returned, so the caller fetches
    the approved bytes or fails saying why."""
    import os as _os

    from app import hf_pull

    pins = _pin_map(payload)
    rejected = []
    for f in unverified:
        name = _os.path.basename(f)
        dest = _os.path.join(models_mount, hf_pull.store_name(f, payload.hf_repo))
        rec = app.state.loaded.get(payload.instance_id)
        if rec:
            rec["detail"] = f"verifying {name}"
        ok, aside = hf_pull.verify_or_set_aside(dest, pins[name])
        if not ok:
            rejected.append((f, _os.path.basename(aside)))
    return rejected


def _pull_then_launch(app, payload, spec, docker, mmproj, models_mount, miss,
                      *, registry_pull=None, unverified=(), adoptable=()):
    """Background: fetch the missing weights (status "pulling %"), then launch +
    supervise. On any failure the instance goes "failed" with the reason — never
    a silent crash-loop (#287).

    ``registry_pull`` (#307 S2, THE payoff): when the caller determined every
    missing file already has a digest in the master's Zot registry, this is
    ``{"repo","files":[{"name","digest"}]}`` and the fetch below pulls from
    there (LAN, offline-safe) instead of HuggingFace — None keeps the
    pre-#307-S2 HF-only behaviour exactly as it was.

    ``adoptable`` (#2431): pinned projectors whose store name is free while a
    legacy flat file of that basename exists. Each is hashed here and linked
    to its store name only when its bytes are the pin; otherwise it is
    fetched like any missing file, or — offline or without an hf_repo — the
    load fails naming it. The flat file is never moved.
    """
    import os as _os

    from app.hf_pull import bind_sidecar, ensure_file, ensure_repo_dir, list_repo_entries

    iid = payload.instance_id

    def _cancelled() -> bool:
        """#302: the instance record IS the interest signal. Unload marks it
        cancelled and deletes it, so a background pull nobody wants any more
        aborts at its next chunk instead of streaming a multi-part quant to
        completion (wasted bandwidth, filled volume)."""
        rec = app.state.loaded.get(iid)
        return rec is None or bool(rec.get("cancelled"))

    repo_dir_mode_flag = spec.engine in DIR_SERVING_ENGINES and not payload.files
    pins = _pin_map(payload)
    revision = (getattr(payload, "pins", None) or {}).get("revision") or "main"
    try:
        miss = list(miss)
        unverified = list(unverified)
        if adoptable:
            from app.hf_pull import adopt_flat_projector, network_allows_hf, store_name

            refused = []
            for f in adoptable:
                name = _os.path.basename(f)
                rec = app.state.loaded.get(iid)
                if rec:
                    rec["detail"] = f"verifying {name}"
                if adopt_flat_projector(models_mount, f, payload.hf_repo, pins.get(name)):
                    continue
                if _os.path.exists(_os.path.join(models_mount, store_name(f, payload.hf_repo))):
                    unverified.append(f)       # placed meanwhile: judge it on its bytes
                else:
                    refused.append(f)
            if refused and (not payload.hf_repo or not network_allows_hf(
                    _os.environ.get("RAZZFAZZ_NETWORK_MODE", "online"))):
                raise RuntimeError(
                    "the flat " + ", ".join(_os.path.basename(f) for f in refused)
                    + f" on this volume is not the catalogue's projector for {payload.hf_repo}, "
                    "and this node cannot fetch the approved one (offline or no hf_repo) (#2431)")
            miss.extend(refused)
        if unverified:
            rejected = _verify_present(app, payload, models_mount, unverified)
            if rejected:
                from app.hf_pull import network_allows_hf

                if not payload.hf_repo or not network_allows_hf(
                        _os.environ.get("RAZZFAZZ_NETWORK_MODE", "online")):
                    raise RuntimeError(
                        "set aside " + ", ".join(a for _f, a in rejected)
                        + ": sha256 differs from the catalogue pin, and this node "
                        "cannot fetch the approved bytes (offline or no hf_repo) (#2392)")
                miss.extend(f for f, _a in rejected)
        if spec.engine in DIR_SERVING_ENGINES and not payload.files:
            # self-contained invariant (#580 review): reachable only via
            # perform_load repo_dir_mode, which requires hf_repo - assert
            # here too so the guarantee survives a future second caller.
            assert payload.hf_repo, 'repo-dir pull without hf_repo'
            # #574 repo-dir pull: one listing drives file set + summed-size
            # preflight + per-file digests; progress is bytes across the whole
            # repo, not per file.
            def _dcb(got, total):
                rec = app.state.loaded.get(iid)
                if rec:
                    rec["status"] = "pulling"
                    rec["detail"] = (f"repo {int(got * 100 / total)}%" if total
                                     else f"repo {got // (1024 * 1024)}MiB")

            ensure_repo_dir(models_mount, payload.model, payload.hf_repo,
                            http_stream=_http_stream, progress_cb=_dcb,
                            list_entries=list_repo_entries,
                            cancel_cb=_cancelled)
            miss = []
        # #303: the LOCAL layout is flat (ensure_file writes the basename) but
        # the HF resolve URL needs the FULL repo path — repos keep multi-part
        # quants in subfolders (UD-Q1_0/...-00001-of-00010.gguf). Passing the
        # basename here built https://hf.co/<repo>/resolve/main/<leaf> → 404.
        # ensure_file flattens the destination itself; give it the real path.
        _assert_no_flatten_collisions(payload.files)
        if registry_pull is not None:
            # #307 S2 (THE CORE payoff): weights already mirrored into the
            # master's Zot registry by an earlier deploy — pull them over the
            # LAN instead of re-fetching from HuggingFace. Works offline too
            # (the registry IS the offline path, #353) — no network_allows_hf
            # check here, unlike the HF branch below.
            rec = app.state.loaded.get(iid)
            if rec:
                rec["status"] = "pulling"
                rec["detail"] = f"registry {registry_pull['repo']}"
            from app.hf_pull import store_name as _store_name

            placed: dict = {}
            pinned_pull = any(pins.get(e["name"]) for e in registry_pull["files"])
            _zot_pull(models_mount, registry_pull["files"], registry_pull["repo"],
                      identities=placed if pinned_pull else None)
            for entry in registry_pull["files"]:
                # #2392: the registry was used only because its digest IS the
                # pin, and pull_blob verified the bytes against that digest.
                # The sidecar binds to the inode pull_blob placed (or hashed),
                # never to whatever is under the name once all shards are in.
                n = entry["name"]
                path = _os.path.join(models_mount, _store_name(n, payload.hf_repo))
                if pins.get(n) and entry.get("digest") == "sha256:" + pins[n]:
                    bind_sidecar(path, pins[n], placed.get(path))
        from_registry = {e["name"] for e in (registry_pull or {}).get("files", [])}
        # #2392: whatever the registry did not supply — all of it without a
        # registry pull; a file set aside by the verification above with one.
        for f in [m for m in miss if _os.path.basename(m) not in from_registry]:
            name = _os.path.basename(f)

            def cb(got, total, _n=name):
                rec = app.state.loaded.get(iid)
                if not rec:
                    return
                rec["status"] = "pulling"
                rec["detail"] = (f"{_n} {int(got * 100 / total)}%" if total
                                 else f"{_n} {got // (1024 * 1024)}MiB")

            # #356: list_repo_entries supplies HF's published sha256 per file
            # — the pull dies on a digest mismatch instead of launching an
            # engine on corrupt weights.
            # #2392: the catalogue pin — the HF commit AND the file's
            # sha256 — overrides "main" and the hub listing's digest.
            ensure_file(models_mount, f, payload.hf_repo,
                        http_stream=_http_stream, progress_cb=cb,
                        list_entries=list_repo_entries, cancel_cb=_cancelled,
                        revision=revision, catalogue_sha256=pins.get(name),
                        catalogue_pins=pins or None)
        from app.drivers import start_engine

        _before = getattr(spec, "image", None)
        spec = _fit_runner_to_architecture(
            app, payload, spec, _assert_gguf_sound(models_mount, _store_files(payload)))
        _fill_unknown_mem_limit(spec, models_mount, _store_files(payload))
        if _cancelled():   # unloaded while the header was checked: start nothing
            return
        if getattr(spec, "image", None) != _before:
            (app.state.loaded.get(iid) or {}).update(
                {"runner_image": spec.image, "runner_by_architecture": True})
        container = start_engine(spec, docker)
        rec = app.state.loaded.get(iid) or {}
        rec.update({"container_id": getattr(container, "id", None),
                    "launched": True, "status": "loading", "detail": None})
        app.state.loaded[iid] = rec
        supervisor = getattr(app.state, "supervisor", None)
        if supervisor is not None:
            supervisor.supervise(spec, now=_now())
        _auto_cache_deploy(app, payload)   # #307 seed the master cache (background)
    except Exception as exc:  # noqa: BLE001 - report, never crash the node
        from app.hf_pull import PullCancelled

        if isinstance(exc, PullCancelled) or _cancelled():
            # #302: the deployment was withdrawn mid-pull. Writing a "failed"
            # record here would RESURRECT the instance the unload deleted — a
            # zombie the manager then reconciles over. Log and stay silent.
            _load_logger.info("pull cancelled for %s (instance unloaded): %s", iid, exc)
            return
        from app.gguf_check import GGUFBusy

        if isinstance(exc, GGUFBusy):
            _hold_pending(app, iid, str(exc))
            return
        if isinstance(exc, (GGUFRefused, RunnerArchitectureRefused)):
            rec = app.state.loaded.get(iid) or {}
            rec.update({"status": "failed", "detail": str(exc)})
            app.state.loaded[iid] = rec
            _load_logger.error("%s: %s", iid, exc)
            return
        # #2367 part 2, measured on 0.79 (2026-09-21): a weight file that
        # COMPLETED after the pull had been marked failed (another transfer of
        # the same file finished it) sat whole on the volume next to a failed
        # deployment, and nothing reconciled the two. If every weight is present
        # now, the pull's failure is history: launch instead of reporting it.
        if (not repo_dir_mode_flag and payload.files
                and not _missing_now(models_mount, payload.files, payload.hf_repo)
                # #2392: "present" is not "approved" — a pinned file whose
                # verification never finished must not launch this way.
                and not _unconfirmed_pins(models_mount, payload)):
            try:
                from app.drivers import start_engine as _start_engine_late

                _before = getattr(spec, "image", None)
                spec = _fit_runner_to_architecture(
                    app, payload, spec, _assert_gguf_sound(models_mount, _store_files(payload)))
                _fill_unknown_mem_limit(spec, models_mount, _store_files(payload))
                if _cancelled():
                    return
                if getattr(spec, "image", None) != _before:
                    (app.state.loaded.get(iid) or {}).update(
                        {"runner_image": spec.image, "runner_by_architecture": True})
                container = _start_engine_late(spec, docker)
                rec = app.state.loaded.get(iid) or {}
                rec.update({"container_id": getattr(container, "id", None),
                            "launched": True, "status": "loading", "detail": None})
                app.state.loaded[iid] = rec
                supervisor = getattr(app.state, "supervisor", None)
                if supervisor is not None:
                    supervisor.supervise(spec, now=_now())
                _load_logger.warning("pull for %s failed (%s) but every weight is present "
                                     "now — launched anyway (#2367)", iid, str(exc)[:120])
                return
            except GGUFBusy as busy:
                _hold_pending(app, iid, str(busy))
                return
            except (GGUFRefused, RunnerArchitectureRefused) as refused:
                if _cancelled():   # unloaded during the check: no zombie record
                    return
                rec = app.state.loaded.get(iid) or {}
                rec.update({"status": "failed", "detail": str(refused)})
                app.state.loaded[iid] = rec
                _load_logger.error("%s: %s", iid, refused)
                return
            except Exception as late_exc:  # noqa: BLE001 - fall through to the honest failure
                exc = late_exc
        if _cancelled():   # unloaded while the late launch ran: no zombie record
            return
        rec = app.state.loaded.get(iid) or {}
        rec.update({"status": "failed", "detail": f"pull/launch failed: {str(exc)[:200]}"})
        app.state.loaded[iid] = rec
        _load_logger.exception("pull-on-deploy failed for %s", iid)


def _zot_repo(name: str) -> str:
    """models/<slug> repo path in Zot from a model/served name."""
    import re as _re
    return "models/" + (_re.sub(r"[^a-z0-9._-]+", "-", (name or "").lower()).strip("-") or "model")


def _zot_push(models_mount: str, filenames: list, repo: str, tag: str,
              repo_id: str | None = None, cancel_cb=None) -> dict:
    """Push already-local files into the in-stack Zot registry (shared by the
    mirror command and auto-cache-on-deploy). Files must already be in
    ``models_mount``. HTTP via urllib → same contract pusher.push_artifact wants.

    NODE-7: address resolution + credential + redirect posture are now IDENTICAL
    to ``_zot_pull``'s. The push side used to read ``LLM_REGISTRY_URL`` raw, send
    no ``Authorization`` and follow redirects, so on a thin node — where
    ``.env.node`` points that variable at the hub EDGE — every auto-cache push
    401'd and was swallowed as "non-fatal", quietly voiding the "a deployed model
    is in the master cache" property for exactly the remote workers that need it.
    A LOCAL node now pins the in-network base regardless of a stray env value,
    the same hazard ``resolve_model_registry_base`` was written to remove.

    NODE-8: an EMPTY file list never reaches ``push_artifact``. A #574 repo-dir
    deploy carries ``files == []`` by contract, and pushing that published a
    manifest with ``"layers": []`` under tag ``deployed`` — the registry then
    advertised a cached model containing nothing (the #1001 empty-manifest state,
    produced deliberately). Repo-dir models have no mirror path yet; until they
    do, they are skipped explicitly instead of cached as an empty artifact.
    """
    import os as _os
    import urllib.error as _ue
    import urllib.request as _ur

    from app.drivers.images import (registry_basic_auth_header, registry_is_local,
                                    registry_opener, registry_write_auth_header,
                                    resolve_model_registry_base)
    from app.pusher import push_artifact

    from app.hf_pull import store_name

    reg = resolve_model_registry_base()
    # #2431: the blob is named by its basename in the registry, but read from
    # its STORE name — a flat projector of that basename may be another
    # repository's, and must never be cached under this model.
    local = [{"name": _os.path.basename(f),
              "path": _os.path.join(models_mount, store_name(f, repo_id))} for f in filenames]
    if not local:
        _load_logger.info(
            "skipping registry push of %s:%s — no files to cache (a repo-dir "
            "deploy has none by contract; an empty manifest would advertise a "
            "cached model that contains nothing, NODE-8)", repo, tag)
        return {"repo": repo, "tag": tag, "files": [], "pushed": 0, "cached": 0,
                "skipped": "no files"}

    class _Resp:
        def __init__(self, status, headers):
            self.status = status
            self.headers = headers

    def _req(method, url, data=None, filepath=None, size=None, headers=None):
        # STREAM a file blob from disk (never load a multi-GB weight into RAM —
        # the node has a tight mem_limit). http.client sends a file-like `data` in
        # blocks when Content-Length is set (pusher sets it).
        fh = None
        body = data
        if filepath is not None:
            fh = open(filepath, "rb")
            body = fh
        hdrs = dict(headers or {})
        # #307 S4 / #571: REMOTE attaches the host-scoped fleet Basic header
        # (the hub edge checks it). #3302: LOCAL attaches the registry's writer
        # identity, since the in-network registry serves anonymous READS only.
        # registry_opener() REFUSES redirects so neither credential can travel
        # to another host.
        if not registry_is_local():
            hdrs.update(registry_basic_auth_header(url, reg))
        else:
            hdrs.update(registry_write_auth_header(url, reg))
        try:
            r = _ur.Request(url, data=body, headers=hdrs, method=method)
            resp = registry_opener().open(r, timeout=1800)
            return _Resp(resp.status, dict(resp.headers))
        except _ue.HTTPError as e:  # 4xx/5xx carry usable status + headers
            return _Resp(e.code, dict(e.headers or {}))
        finally:
            if fh is not None:
                fh.close()

    if cancel_cb is None:
        return push_artifact(_req, reg, repo, tag, local)
    return push_artifact(_req, reg, repo, tag, local, cancel_cb=cancel_cb)


def _auto_cache_deploy(app, payload) -> None:
    """#307: best-effort BACKGROUND push of a deployed model's weights into Zot,
    so a deployed model is automatically in the master cache (offline-ready).
    Fire-and-forget — never blocks or fails the deploy.

    NODE-8: a deploy with NO files (the #574 repo-dir convention) is not cached
    at all — not even a thread is started. Pushing it published an empty
    manifest; see ``_zot_push``."""
    import threading

    if not (payload.files or []):
        _load_logger.info(
            "auto-cache skipped for %s: repo-dir deploy has no flat file list "
            "to mirror (NODE-8)", payload.instance_id)
        return

    def _run():
        try:
            models_mount = getattr(app.state, "models_mount", "/models")
            _zot_push(models_mount, payload.files or [], _zot_repo(payload.model), "deployed",
                      repo_id=getattr(payload, "hf_repo", None))
        except Exception:  # noqa: BLE001 - caching is best-effort
            _load_logger.warning("auto-cache to registry failed (non-fatal)", exc_info=True)

    threading.Thread(target=_run, name=f"autocache-{payload.instance_id}", daemon=True).start()


def _zot_pull(models_mount: str, files: list, repo: str, identities: dict | None = None) -> dict:
    """Pull artifact files BY DIGEST from the in-stack Zot into ``models_mount``.

    The exact counterpart of ``_zot_push``: same registry, same digest contract,
    opposite direction. STREAMS every blob — a model artifact is a single file of
    tens of GB (qwen3.6 Q8_0 = 36.9 GB) and the node runs under a tight
    mem_limit, so buffering one is an OOM, not a slow path (#354).
    """
    import urllib.request as _ur

    from app.puller import pull_artifact

    # #307 S4: LOCAL (co-located with the master) vs REMOTE (a routed thin
    # node, #549 R0) address resolution — see resolve_model_registry_base's
    # docstring. Was a bare LLM_REGISTRY_URL env read with no notion of which
    # kind of node this is; a LOCAL node now always gets the in-network
    # default regardless of that env value.
    from app.drivers.images import resolve_model_registry_base

    reg = resolve_model_registry_base()

    def _fetch_stream(url, chunk=8 * 1024 * 1024):
        # #571: hub-edge weight pulls are basic-authed; the helper scopes the
        # header to URLs under the configured registry base and to nothing
        # else, and the opener REFUSES redirects — urllib would otherwise
        # carry Authorization across a cross-host hop (#577 review).
        # #307 S4: LOCAL never attaches a credential, whatever
        # LLM_WORKER_REGISTRY_USER/_PASSWORD hold — the in-network default is
        # deliberately open (#559); only REMOTE reuses the #571 helper.
        from app.drivers.images import registry_basic_auth_header, registry_is_local, registry_opener
        headers = {} if registry_is_local() else registry_basic_auth_header(url, reg)
        req = _ur.Request(url, headers=headers)
        with registry_opener().open(req, timeout=1800) as resp:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    return
                yield buf

    return pull_artifact(reg, repo, files, models_mount, fetch_stream=_fetch_stream,
                         **({"identities": identities} if identities is not None else {}))


def perform_pull(app, args: dict):
    """#353: fetch a model's weights from the master's registry onto THIS node.

    Runs on a worker via the ``pull_artifact`` command. Together with
    ``perform_mirror`` (HF -> registry, on the master) this closes #307: the
    master caches once, every node pulls from the master, and an air-gapped box
    never reaches huggingface.co.
    """
    models_mount = getattr(app.state, "models_mount", "/models")
    repo = args["repo"]
    files = args.get("files") or []
    if not files:
        raise ValueError("pull requires a non-empty files list")
    if args.get("hf_repo"):
        # #2431: a projector lands under its repository's store name
        from app.hf_pull import store_name
        files = [{**f, "dest_name": f.get("dest_name") or store_name(f["name"], args["hf_repo"])}
                 for f in files]
    result = _zot_pull(models_mount, files, repo)
    return {"repo": repo, "pulled": result}


class MirrorCancelled(Exception):
    """The manager cancelled this mirror (its deployment was deleted)."""


def request_command_cancel(app, command_id) -> bool:
    """Mark a background command as cancelled (the ``cancel_command`` kind).
    The command itself notices at its next check; one that already finished
    or never ran here is unaffected. Returns False for an empty id."""
    if not command_id:
        return False
    cancelled = getattr(app.state, "cancelled_commands", None)
    if cancelled is None:
        cancelled = set()
        app.state.cancelled_commands = cancelled
    cancelled.add(str(command_id))
    return True


def command_cancelled(app, command_id) -> bool:
    return bool(command_id) and str(command_id) in (
        getattr(app.state, "cancelled_commands", None) or ())


def _drop_fetched_weights(app, models_mount: str, paths: list) -> list:
    """A cancelled mirror removes the weight files IT downloaded — never a file
    that was already on the volume, never one a loaded engine uses."""
    in_use = {os.path.basename(f)
              for rec in (getattr(app.state, "loaded", None) or {}).values()
              for f in ((rec or {}).get("files") or [])}
    removed = []
    for p in paths:
        if os.path.basename(p) in in_use:
            continue
        try:
            os.remove(p)
            removed.append(os.path.basename(p))
        except FileNotFoundError:
            pass
        except OSError:
            _load_logger.warning("cancelled mirror: could not remove %s", p, exc_info=True)
    return removed


def perform_mirror(app, args: dict, *, command_id=None):
    """#307: mirror a model HF → Zot WITHOUT deploying. Fetch each file from
    HuggingFace into the models mount (reusing the deploy fetch path, cached if
    present), then push them into Zot as an OCI artifact the puller consumes.
    Runs on the master via the ``mirror_model`` command.

    #307 S2: this IS the HF fetch — offline it cannot run (no registry
    fallback for the one path whose job is POPULATING the registry), gated
    the same way perform_load already gates its HF fallback.

    Cancellable (``command_id`` + ``cancel_command``): checked before the
    first file, inside every download, before each pushed blob and before the
    manifest. A cancelled mirror tags nothing in the registry, deletes the
    weights it downloaded itself, and raises ``MirrorCancelled``."""
    import os as _os

    from app.hf_pull import PullCancelled, ensure_file, is_glob, network_allows_hf, store_name

    models_mount = getattr(app.state, "models_mount", "/models")
    hf_repo = args["hf_repo"]
    files = args.get("files") or []
    repo = args["repo"]
    tag = args.get("tag") or "latest"
    if not files:
        raise ValueError("mirror requires a non-empty files list")
    net_mode = _os.environ.get("RAZZFAZZ_NETWORK_MODE", "online")
    if not network_allows_hf(net_mode):
        raise ValueError(f"mirror requires HuggingFace ({hf_repo}) but this "
                         f"node is offline")
    # review #719: the mirror writes flat too — same collision refusal as the
    # deploy pull, so a curated-but-colliding list cannot corrupt the cache.
    _assert_no_flatten_collisions(files)

    def _cancelled() -> bool:
        return command_cancelled(app, command_id)

    fetched: list = []

    def _abort(stage: str):
        removed = _drop_fetched_weights(app, models_mount, fetched)
        _load_logger.info("mirror %s:%s cancelled %s; removed %s", repo, tag, stage,
                          removed or "nothing")
        raise MirrorCancelled(f"mirror of {repo}:{tag} cancelled {stage}"
                              + (f"; removed {', '.join(removed)}" if removed else ""))

    try:
        for f in files:
            if _cancelled():
                _abort("before the download")
            from app.hf_pull import list_repo_entries as _lre  # #356 digest verify
            # #303: full repo path for the URL; ensure_file flattens the dest.
            got = ensure_file(models_mount, f, hf_repo,
                              http_stream=_http_stream, list_entries=_lre,
                              cancel_cb=_cancelled)
            if got == "pulled" and not is_glob(f):
                fetched.append(_os.path.join(models_mount, store_name(f, hf_repo)))
    except PullCancelled:
        _abort("during the download")
    if _cancelled():
        _abort("before the push")
    from app.pusher import PushCancelled
    try:
        result = _zot_push(models_mount, files, repo, tag, repo_id=hf_repo,
                           cancel_cb=_cancelled)
    except PushCancelled:
        _abort("during the push")
    return {"mirrored": f"{repo}:{tag}", **result}


#: #2280: written into the models volume by the host while it copies weights in
#: (scripts/lib.sh razzfazz_stage_package_models): one basename per line — the
#: files of THIS copy — refreshed before each file and every minute during one,
#: removed when the copy ends (also on INT/TERM).
STAGING_MARKER = ".rzfz-staging"
#: Seconds since the host last refreshed the marker after which the copy is
#: taken for dead: the marker is ignored and the load goes its normal way.
STAGING_MARKER_MAX_AGE_S = float(os.environ.get("NODE_STAGING_MARKER_MAX_AGE_S", "900") or 900)
STAGING_POLL_S = 5.0


def staging_names(models_mount: str) -> set[str]:
    """The basenames a LIVE host copy is bringing in; empty when there is no
    marker or it has not been refreshed within STAGING_MARKER_MAX_AGE_S."""
    path = os.path.join(models_mount, STAGING_MARKER)
    try:
        if time.time() - os.path.getmtime(path) >= STAGING_MARKER_MAX_AGE_S:
            return set()
        with open(path, encoding="utf-8", errors="replace") as fh:
            return {ln.strip() for ln in fh.read(1 << 20).splitlines() if ln.strip()}
    except OSError:
        return set()


def staging_in_progress(models_mount: str, miss) -> bool:
    """Is one of the missing files among those a live host copy is bringing in?
    Any other missing weight takes the normal online/offline path at once."""
    names = staging_names(models_mount)
    return bool(names) and any(os.path.basename(m or "") in names for m in (miss or []))


def _wait_for_staging_then_load(app, payload, spec, base_rec, models_mount, miss):
    """Answer `pulling` now; in a thread, wait until the host's copy has ended
    (the marker is gone or stale), then run the whole load again — present
    weights launch, still-absent ones meet the ordinary refusal."""
    import threading

    iid = payload.instance_id
    app.state.loaded[iid] = {**base_rec, "status": "pulling",
                             "detail": "waiting for the host to finish staging weights: " + ", ".join(miss)}

    def run():
        while staging_in_progress(models_mount, miss):
            time.sleep(STAGING_POLL_S)
        try:
            perform_load(app, payload)
        except BaseException as exc:  # noqa: BLE001 - recorded on the instance
            rec = app.state.loaded.get(iid)
            if rec is not None:
                rec.update({"status": "failed", "detail": f"engine launch failed: {str(exc)[:200]}"})

    threading.Thread(target=run, name=f"staging-wait-{iid}", daemon=True).start()
    return {"status": "pulling", "instance_id": iid, "endpoint": spec.serve_url}


def perform_load(app, payload: LoadRequest):
    """Start an engine instance on this node. Shared by the HTTP handler
    (/models/load) AND the #261 command channel (load_engine), so a deploy from
    the master runs the identical path whether pushed or pulled. Off-box / no
    docker → records the request only (skeleton)."""
    from app.hf_pull import store_name

    # #2431: the projector as it is named on the volume — keyed by repository
    from app.drivers.base import pick_mmproj

    mmproj = pick_mmproj(payload.files, payload.hf_repo)
    docker = getattr(app.state, "docker_client", None)

    if docker is not None:
        # Real path (P2-A1): the matching hardware driver builds a launch spec
        # and starts an engine container. The manager routes to spec.serve_url
        # once the engine's /health goes 200.
        from app.drivers import LoadSpecInput, select_driver, start_engine

        try:
            # engine refines the choice where the hardware runs more than one
            # (cuda → vLLM vs llama.cpp); None keeps the per-hardware default.
            driver = select_driver(app.state.hardware, engine=payload.engine)
        except ValueError as exc:
            # Unknown/unset hardware class — and, since #1517 rev-B, an NVIDIA
            # GPU whose compute capability no built runner matches
            # (UnknownCudaCapability is a ValueError precisely so its message
            # lands in this `detail` instead of a bare 500 in the node log).
            raise HTTPException(status_code=400, detail=str(exc))
        # #549 R1 defence in depth: the manager validates the pin with the
        # IDENTICAL pattern before enqueueing, but the node is the one that
        # actually runs the container, so it does not take the channel's word.
        from app.drivers.images import (allowed_runner_registry,
                                        ref_from_allowed_registry,
                                        valid_image_ref)
        if payload.runner_image is not None:
            if not valid_image_ref(payload.runner_image):
                raise HTTPException(status_code=400,
                                    detail=f"runner_image {payload.runner_image!r} is not "
                                           f"a valid image reference")
            # #549 R2: well-formed is NOT enough. docker-py's containers.run
            # implicitly PULLS an image the node does not hold, so a syntactically
            # valid `docker.io/attacker/x:1` pin would reach the public internet
            # and run with GPU access — the exact air-gap invariant _deploy_runner
            # enforces before its explicit pull. The launch path is the same
            # pull, just implicit, so it gets the same allow-list.
            if not ref_from_allowed_registry(payload.runner_image):
                raise HTTPException(
                    status_code=400,
                    detail=f"refusing runner_image {payload.runner_image!r}: runner "
                           f"images come from {allowed_runner_registry()!r} only — "
                           f"this node never pulls from a public registry (#549 R2)")
        # #3403: the runner CHANNEL, resolved on the node because only the node
        # knows its own line (a CUDA node's compute capability above all). An
        # explicit pin wins; the stable channel keeps the driver's default; the
        # experimental channel names a BAKED image (no registry host), which
        # `ensure_engine_image` pulls from the hub as `<registry>/runners/…`
        # when the node does not hold it — the same path as the stable default.
        launch_image = payload.runner_image
        if launch_image is None:
            from app.drivers.images import channel_image
            try:
                launch_image = channel_image(app.state.hardware, payload.runner_channel)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        req = LoadSpecInput(
            instance_id=payload.instance_id,
            model=payload.model,
            files=payload.files,
            models_volume=app.state.models_volume,
            models_mount=app.state.models_mount,
            network=app.state.engine_network,
            params=payload.params,
            task=payload.task,
            runner_image=launch_image,
            hf_repo=payload.hf_repo,
            mem_budget_gb=payload.mem_budget_gb,
        )
        try:
            spec = driver.build_spec(req)
        except ValueError as exc:  # e.g. no .gguf weight in files[]
            raise HTTPException(status_code=400, detail=str(exc))

        # #287 pull-on-deploy: if the weights aren't on this node yet, FETCH them
        # before launching (else the engine crash-loops on a missing GGUF — the
        # "stays pending/failed" deploy). #307 S2 (THE CORE payoff): a file
        # already mirrored into the master's Zot registry (payload.registry_files,
        # keyed by the flat basename pusher.push_artifact writes) pulls from
        # THERE — LAN, and the offline path too — instead of HuggingFace. HF
        # stays the upstream of first resort only for what the registry doesn't
        # have yet. Missing + nothing in the registry + no HF source / offline →
        # fail cleanly (never launch a doomed engine) — unchanged from before
        # #307 S2.
        import os as _os

        from app.hf_pull import (adopt_flat_projector, flat_projector_waiting,
                                 missing_files, network_allows_hf)

        models_mount = getattr(app.state, "models_mount", "/models")
        # #574: a directory-serving engine takes the WHOLE HF repo as
        # models_mount/<model>/ — file-list logic does not apply. files==[] +
        # hf_repo is the repo-dir deploy convention. WITHOUT hf_repo the old
        # contract holds unchanged: empty files = weights managed out-of-band,
        # launch immediately (pinned by test_models_load_with_docker_launches_
        # cuda_engine). Presence probe is cheap; the per-file completeness pass
        # lives in ensure_repo_dir.
        # See DIR_SERVING_ENGINES — no shipped driver matches it since #1518.
        repo_dir_mode = (spec.engine in DIR_SERVING_ENGINES and not payload.files
                         and bool(payload.hf_repo))
        if repo_dir_mode:
            from app.hf_pull import repo_dir_present
            miss = [] if repo_dir_present(models_mount, payload.model) else [payload.model]
        else:
            miss = missing_files(models_mount, payload.files, repo_id=payload.hf_repo)
        # #306 delete half: the flattened weight leaf names this deployment
        # actually reads (files are written FLAT into the mount, #303) — the
        # worker-agent's delete_disk_model in-use guard matches on this so a
        # cached blob backing a live/loading/pulling deployment is refused.
        base_rec = {"model": payload.model, "endpoint": spec.serve_url,
                    "command": spec.command, "engine": spec.engine,
                    "health_url": spec.health_url, "launched": False,
                    "files": [store_name(f, payload.hf_repo) for f in payload.files],
                    # #3106: the image this engine is started from, so the
                    # master can roll a runner upgrade back to it.
                    "runner_image": spec.image}
        # #2392: a present pinned file with no sidecar vouching for it must be
        # hashed before launch — in the background thread, never here (this
        # runs on the report loop; a 37 GB hash is minutes). It is NOT
        # "missing": hashing needs no network, so the offline refusal below
        # ignores it, and a match launches without a download.
        pins = _pin_map(payload)
        # #2431: a legacy flat projector is used only by its catalogue digest.
        # Linked here only when its own sidecar already proves the pin (a
        # stat and a tiny read); otherwise it is ADOPTABLE — hashed in the
        # pull thread like any unverified file, and not "missing" offline.
        adoptable = []
        for f in ([] if repo_dir_mode else miss):
            pin = pins.get(_os.path.basename(f))
            if store_name(f, payload.hf_repo) != _os.path.basename(f) and pin:
                if adopt_flat_projector(models_mount, f, payload.hf_repo, pin, may_hash=False):
                    continue
                if flat_projector_waiting(models_mount, f, payload.hf_repo):
                    adoptable.append(f)
        if not repo_dir_mode:
            miss = missing_files(models_mount, [m for m in miss if m not in adoptable],
                                 repo_id=payload.hf_repo)
        unverified = [] if repo_dir_mode else _unconfirmed_pins(models_mount, payload)
        if not miss and not unverified and not adoptable:
            return _launch_off_loop(app, payload, spec, docker, mmproj, base_rec)

        # #307 S2: every missing file already has a digest in the registry-
        # mirrored set → pull the lot from Zot instead of HF. The registry is
        # the offline path too (#353), so this is NOT gated on
        # network_allows_hf — a partially-covered set (some files mirrored,
        # some not) falls through to the HF-or-refuse decision below
        # unchanged, rather than splitting one deploy across two fetchers.
        registry_by_name = {f.get("name"): f.get("digest")
                            for f in (payload.registry_files or [])
                            if f.get("name") and f.get("digest")}
        # #2392: a registry blob is only as good as its digest — one that is
        # not the catalogue pin is not the approved bytes.
        off_pin = sorted(_os.path.basename(m) for m in miss
                         if pins.get(_os.path.basename(m))
                         and _os.path.basename(m) in registry_by_name
                         and registry_by_name[_os.path.basename(m)] != "sha256:" + pins[_os.path.basename(m)])
        use_registry = (bool(miss) and not repo_dir_mode and bool(registry_by_name)
                        and all(_os.path.basename(m) in registry_by_name for m in miss)
                        and not off_pin)

        net_mode = _os.environ.get("RAZZFAZZ_NETWORK_MODE", "online")
        # #2280: the host is copying weights into this volume right now (a
        # model bundle or a baked package; razzfazz_stage_package_models marks
        # it). A load that arrives in that window — the reconciler re-placing a
        # deployment after the restart, minutes into a 40 GB copy — would
        # otherwise fail for good with "weights not present" while the file is
        # one rename away. It waits for the copy to finish and loads again.
        if miss and not use_registry and staging_in_progress(models_mount, miss):
            return _wait_for_staging_then_load(app, payload, spec, base_rec, models_mount, miss)
        if miss and not use_registry and (not payload.hf_repo or not network_allows_hf(net_mode)):
            why = ("offline mode" if not network_allows_hf(net_mode)
                   else "no fetch source (hf_repo)")
            if off_pin:
                why += (f"; the registry holds {', '.join(off_pin)} under a digest that "
                        "is not the catalogue pin (#2392)")
            app.state.loaded[payload.instance_id] = {
                **base_rec, "status": "failed",
                "detail": f"weights not present and {why}: {', '.join(miss)}"}
            return {"status": "failed", "instance_id": payload.instance_id,
                    "detail": app.state.loaded[payload.instance_id]["detail"]}

        # background: pull the missing weights (registry first when it has
        # them all, HF otherwise), then launch.
        app.state.loaded[payload.instance_id] = {**base_rec, "status": "pulling", "detail": "queued"}
        import threading

        registry_pull = None
        if use_registry:
            names = {_os.path.basename(m) for m in miss}
            registry_pull = {"repo": _zot_repo(payload.model),
                             # #2431: a projector lands on its store name
                             "files": [{"name": n, "digest": registry_by_name[n],
                                        **({"dest_name": store_name(n, payload.hf_repo)}
                                           if store_name(n, payload.hf_repo) != n else {})}
                                       for n in names]}

        threading.Thread(
            target=_pull_then_launch,
            args=(app, payload, spec, docker, mmproj, models_mount, list(miss)),
            kwargs={"registry_pull": registry_pull, "unverified": unverified,
                    "adoptable": adoptable},
            name=f"pull-{payload.instance_id}", daemon=True,
        ).start()
        return {"status": "pulling", "instance_id": payload.instance_id,
                "endpoint": spec.serve_url, "mmproj": mmproj}

    # Skeleton fallback (off-box / no docker): record the request only.
    command = {"model": payload.model, "params": payload.params}
    if mmproj:
        command["mmproj"] = mmproj
    endpoint = f"http://{payload.instance_id}:8080/v1"
    app.state.loaded[payload.instance_id] = {
        "model": payload.model,
        "endpoint": endpoint,
        "command": command,
        "launched": False,
    }
    return {"status": "loading", "instance_id": payload.instance_id, "endpoint": endpoint,
            "mmproj": mmproj}


def _control_key() -> str:
    """The credential the mutating control endpoints demand (#2594): the node's
    per-worker command key, else the shared enrolment key. Read per request so
    a key rotated into the environment takes effect without a restart."""
    return (os.environ.get("LLM_WORKER_COMMAND_KEY")
            or os.environ.get("LLM_MANAGER_NODE_KEY") or "").strip()


def require_control_key(request: Request) -> None:
    """Refuse a mutating call that does not carry the node credential.

    Engines share the internal engines network with this API, so reachability
    is no authentication. With a key configured, every call needs
    ``Authorization: Bearer <key>``. A live node (LLM_WORKER_AGENT_RUNTIME=1)
    with NO key refuses everything (fail closed); the off-box skeleton used by
    unit tests, which has neither, stays open."""
    key = _control_key()
    if not key:
        if os.environ.get("LLM_WORKER_AGENT_RUNTIME") == "1":
            raise HTTPException(status_code=503,
                                detail="control API disabled: no node credential configured")
        return
    auth = request.headers.get("authorization", "")
    scheme, _, supplied = auth.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.strip().encode(), key.encode()):
        raise HTTPException(status_code=401, detail="unauthorized",
                            headers={"WWW-Authenticate": "Bearer"})


def _configure_app_logging() -> None:
    """#2924: the docker agent runs as plain `uvicorn app:create_app --factory`
    and uvicorn configures only its own loggers, so the agent's INFO records
    ("re-adopted N running engine(s)", the launch lines) were dropped; only
    WARNING and above reached stderr. Give the `node_agent` loggers an INFO
    handler on stderr. Idempotent, and left alone when the host process (the
    Mac native runtime's basicConfig, a test) already configured logging."""
    import logging
    import sys

    lg = logging.getLogger("node_agent")
    if lg.handlers or logging.getLogger().handlers:
        return
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False


def create_app() -> FastAPI:
    _configure_app_logging()
    app = FastAPI(title="rzfz.ai LLM worker-agent", version=__version__)
    app.state.loaded = {}  # instance_id -> {model, endpoint, command, launched}
    # Engine-launch wiring. Off-box / in unit tests docker_client stays None →
    # /models/load records the request only (skeleton behaviour). On a real
    # node the deploy injects a docker client + the node's hardware class, and
    # the matching driver launches an engine container (P2-A1).
    app.state.docker_client = None
    app.state.hardware = None                       # "amd" | "cuda" | "cpu"
    app.state.models_volume = "llm-models"
    app.state.models_mount = "/models"
    app.state.engine_network = "razzfazz-stack_default"
    # Optional EngineSupervisor (P2-A4). When set (real node wiring), a launched
    # instance is registered for bounded-backoff restart / circuit-break; the
    # periodic tick() is driven by the node runtime. None off-box → no-op.
    app.state.supervisor = None

    def _gpu_probe():
        return (getattr(app.state, "gpu_probe", None) or _default_gpu_probe)()

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "loaded": sorted(app.state.loaded.keys()),
        }

    @app.get("/gpu")
    def gpu():
        return _gpu_probe()

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        n = len(app.state.loaded)
        return PlainTextResponse(
            "# HELP node_agent_loaded_models Loaded model instances.\n"
            "# TYPE node_agent_loaded_models gauge\n"
            f"node_agent_loaded_models {n}\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.post("/models/pull", dependencies=[Depends(require_control_key)])
    def models_pull(payload: PullRequest):
        # Atomic multi-file pull is Phase-2 box work; the skeleton validates
        # the request contract and delegates to an injectable puller.
        if not payload.files:
            raise HTTPException(status_code=400, detail="files[] must be non-empty")
        puller = getattr(app.state, "puller", None)
        if puller is not None:
            puller(payload.model_dump())
        return {
            "status": "pulled",
            "artifact_id": payload.artifact_id,
            "files": len(payload.files),
        }

    @app.post("/models/load", dependencies=[Depends(require_control_key)])
    def models_load(payload: LoadRequest):
        return perform_load(app, payload)

    @app.on_event("startup")
    def _arm_runtime():
        # Live node wiring — no-op unless LLM_WORKER_AGENT_RUNTIME=1 (compose
        # sets it). Keeps TestClient/off-box import free of docker + threads.
        from app.runtime import configure_runtime

        # #1633: before anything else, drop half-written weights that no
        # transfer owns any more. This runs FIRST and deliberately: the sweep's
        # whole safety argument is that the only writer of this mount has just
        # started, so nothing here can belong to a live pull of ours.
        sweep_orphan_weight_temps(getattr(app.state, "models_mount", "/models"))
        # #2393: engines run as nobody; weights an older agent wrote 0600 are
        # opened up (pin sidecars re-bound) before any engine can be launched.
        from app.hf_pull import grant_engine_read

        grant_engine_read(getattr(app.state, "models_mount", "/models"))

        configure_runtime(app)

    @app.post("/models/unload", dependencies=[Depends(require_control_key)])
    def models_unload(payload: UnloadRequest):
        record = app.state.loaded.get(payload.instance_id)
        if record is None:
            raise HTTPException(status_code=404, detail="instance not loaded")
        supervisor = getattr(app.state, "supervisor", None)
        if supervisor is not None:
            supervisor.forget(payload.instance_id)
        # #302: signal an in-flight background pull BEFORE tearing the record
        # down. The puller checks this flag (and the record's absence) per
        # chunk, so a large multi-part download stops within one chunk instead
        # of running to completion for an instance nobody wants any more.
        record["cancelled"] = True
        docker = getattr(app.state, "docker_client", None)
        if docker is not None and record.get("launched"):
            from app.drivers import stop_engine

            stop_engine(payload.instance_id, docker)
        del app.state.loaded[payload.instance_id]
        return {"status": "unloaded", "instance_id": payload.instance_id}

    return app
