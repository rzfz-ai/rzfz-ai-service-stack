# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#264 — the deployable-model catalog.

A curated list of models the console can deploy without the operator hand-typing
a HuggingFace repo, filename, serve task, and params. Mirrors the stack's
standard model set (``core/llm/standard-models.yaml``) but is bundled INTO the
manager image so it's available regardless of what's mounted — the manager
serves many hardware classes, not one box's provisioned set.

ONE exception, added by #1256: the vision ``mmproj`` projector FILENAME is not
carried here. It is declared once in the model manifest and resolved through
``app/model_manifest.py`` (read-only mount). The entries below still say WHICH
models are multimodal (``modalities``); only the filename lives elsewhere,
because a hand-typed second copy of it is precisely what left a clean
LLM-Manager box serving the vision default text-only. A missing mount degrades
to "no projector known", never to a wrong one.

Each entry is engine-agnostic metadata; the node driver turns ``task`` into the
right engine flags (embeddings / reranking / chat — the serve-task work). The
console's "Deploy from catalog" picker reads GET /api/catalog and pre-fills the
deploy form; the operator can still override anything before deploying.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from app.authz import Role, require_role
from app.model_manifest import egress_for, mmproj_for, runner_channel_for
from app.runner_targets import EXPERIMENTAL_RUNNER_OFF_NOTICE, experimental_runner_enabled
from app.model_manifest import reset_cache as reset_manifest_cache

# task: chat | embed | rerank — drives the engine's serve mode (serve-task work).
# hardware: which HARDWARE classes the gguf is sensible on (informational filter).
CATALOG: list[dict] = [
    {
        "name": "qwen3.6",
        "display": "Qwen3.6 35B-A3B (Q8)",
        "description": "General chat + tool use + vision. MoE, large context. The stack default.",
        "task": "chat",
        "repo_id": "unsloth/Qwen3.6-35B-A3B-GGUF",
        "filename": "Qwen3.6-35B-A3B-Q8_0.gguf",
        # Multimodal MoE (image-text-to-text). The vision projector is a
        # repo-LEVEL companion the weight-quant listing doesn't include, so a
        # deploy has to name it explicitly or vision is silently OFF (root
        # cause of a customer's scan-extraction failures). #1256: the filename is
        # NOT repeated here — it is declared once in the model manifest and
        # resolved by `companion_files` (a hand-typed copy in this file spent
        # eight days disagreeing with the manifest, which is why an
        # LLM-Manager box came up text-only).
        "modalities": ["text", "image"],
        "engine": "llamacpp",
        # #1518 (E5): "nvidia" is BACK. EXO-4 kept it out because nothing in the
        # stack built the CUDA llama.cpp runner — #1516 builds and publishes
        # both (sm_120 and sm_121a) and #1517 lets the GPU pick between them, so
        # an NVIDIA deploy now lands on an image that exists. That was exactly
        # the condition test_catalog_runner_wiring.py demanded before lifting
        # the restriction, which is why it retires in this change.
        "hardware": ["amd", "cpu", "nvidia"],
        "recommended": True,
        # curated house params (shown tagged HOUSE in the deploy editor)
        # #1538: ctx_size is the TOTAL KV pool and llama.cpp DIVIDES it across
        # `parallel` slots — n_ctx_slot = ctx_size / parallel. 1048576 / 4 is
        # what gives each concurrent request the 262144 the fleet advertises;
        # 32768 gave ONE slot of 32k. `core/llm/standard-models.yaml` has
        # launched this model at `--ctx-size=1048576 --parallel=4` all along
        # ("we can run par=4 with full 262K per slot"), so the two deploy paths
        # disagreed 8-fold on the SAME model file — and since #1292 the manager
        # path is the default on every preset, so a fresh box got the 32k one.
        #
        # Measured on 0.91 / .175 / .191, 2026-09-06. Before:
        #   srv load_model: n_slots = 4, n_ctx_slot = 65536     (ctx_size 262144)
        #   a 69 333-token request → 400 "exceeds the available context size"
        # After:
        #   srv load_model: n_slots = 4, n_ctx_slot = 262144
        #   a 99 681-token request serves on all three boxes.
        # Affordable because qwen3.6 uses sliding-window attention: the KV pool
        # for 1M total measures ~20 GB, so weights + KV is ~57 GB, well inside
        # a 104 GB worker budget.
        #
        # #2520: context checkpoints ON (the node driver's default is 0) — same
        # value, same reason as the manifest's qwen3.6 entry
        # (core/llm/standard-models.yaml): a hybrid model with 0 checkpoints
        # reuses no prefix (12.8 s vs 0.68 s for the 2nd request of an
        # 11.6k-token prefix). Admission charges the +1.7 GiB host RSS
        # (app/footprint.py). batch_size 2048 is the measured -b (and the
        # console schema's default anyway).
        # #2536 item 33: `ubatch_size` 1024 is the measured -ub (#2520). It has
        # no console schema field, so the console's `deployParams` carries it
        # from here for a catalogue deploy.
        "params": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
                   "repeat_penalty": 1.05, "ctx_size": 1048576, "n_parallel": 4,
                   "ctx_checkpoints": 8, "batch_size": 2048, "ubatch_size": 1024},
    },
    {
        "name": "gemma4",
        "display": "Gemma 4 26B-A4B-it (Q8)",
        "description": "Vision-capable chat. Strong multimodal + summarisation.",
        "task": "chat",
        "repo_id": "unsloth/gemma-4-26B-A4B-it-GGUF",
        "filename": "gemma-4-26B-A4B-it-Q8_0.gguf",
        "modalities": ["text", "image"],       # projector: see the manifest (#1256)
        "engine": "llamacpp",
        "hardware": ["amd", "nvidia"],
        "recommended": False,
        "params": {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "min_p": 0.0,
                   "ctx_size": 8192},
    },
    {
        "name": "qwen3-coder-next",
        "display": "Qwen3 Coder Next (Q4_K_M)",
        "description": "Coding-tuned model for the developer preset + coding agents.",
        "task": "chat",
        "repo_id": "Qwen/Qwen3-Coder-Next-GGUF",
        "filename": "Qwen3-Coder-Next-Q4_K_M/Qwen3-Coder-Next-Q4_K_M-*.gguf",
        "engine": "llamacpp",
        "hardware": ["amd", "cpu", "nvidia"],
        "recommended": False,
        "params": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
                   "repeat_penalty": 1.05, "ctx_size": 32768},
    },
    {
        # #3403: served by the EXPERIMENTAL runner channel (its architecture,
        # inkling2_loop, is unknown to the stable llama.cpp build). The channel
        # is the manifest's `runner: experimental`; the deploy resolves it, so a
        # one-click deploy from this entry lands on the experimental runner.
        "name": "inkling-2-mini",
        "display": "Inkling 2 Mini (IQ4_NL) - experimental runner",
        "description": ("Looped MoE chat + vision + tool use (26B, 3.4B active). Runs on "
                        "the experimental llama.cpp runner: no stability promise."),
        "task": "chat",
        "repo_id": "ggml-org/Inkling-2-Mini-GGUF",
        "filename": "Inkling-2-Mini-IQ4_NL.gguf",
        "modalities": ["text", "image"],       # projector: see the manifest (#1256)
        "engine": "llamacpp",
        "hardware": ["amd", "nvidia"],
        "recommended": False,
        "params": {"temperature": 1.0, "top_p": 1.0, "top_k": 0, "min_p": 0.0,
                   "ctx_size": 65536, "n_parallel": 2},
    },
    {
        "name": "granite-docling",
        "display": "Granite Docling 258M (BF16)",
        "description": "Small vision model backing the Docling document-conversion VLM.",
        "task": "chat",
        "repo_id": "ibm-granite/granite-docling-258M-GGUF",
        "filename": "granite-docling-258M-BF16.gguf",
        "modalities": ["text", "image"],       # projector: see the manifest (#1256)
        "engine": "llamacpp",
        "hardware": ["amd", "cpu", "nvidia"],
        "recommended": False,
    },
    {
        "name": "qwen3-embedding",
        "display": "Qwen3 Embedding 8B (Q4_K_M)",
        "description": "Default embedding model on GPU boxes. 4096-dim, 32K native context.",
        "task": "embed",
        "repo_id": "Qwen/Qwen3-Embedding-8B-GGUF",
        "filename": "Qwen3-Embedding-8B-Q4_K_M.gguf",
        "engine": "llamacpp",
        # #1518 (E5): "nvidia" is BACK here too — same reason as the qwen3.6
        # entry above (the CUDA llama.cpp runner is built and published by
        # #1516, the GPU picks the variant in #1517). The comment that stood
        # here said the opposite of the line below it and pointed at "the vLLM
        # embedding entry", which E5 deleted: an NVIDIA box has no separate
        # embedding path any more, it serves this one.
        "hardware": ["amd", "cpu", "nvidia"],
        "recommended": False,
    },
    {
        # #2727: the CPU class's embedder (the 8B never fits a 16 GB box)
        "name": "qwen3-embedding-0.6b",
        "display": "Qwen3 Embedding 0.6B (Q8_0)",
        "description": "Default embedding model on CPU boxes. 1024-dim, 32K native context, 0.64 GB.",
        "task": "embed",
        "repo_id": "Qwen/Qwen3-Embedding-0.6B-GGUF",
        "filename": "Qwen3-Embedding-0.6B-Q8_0.gguf",
        "engine": "llamacpp",
        # A 0.6B embedder runs on any class; it is the standard set's pick on CPU
        # only (core/llm/standard-models.yaml: hardware [cpu], defaults_by_hardware).
        # Listed for amd/nvidia too so every llama.cpp entry serves NVIDIA
        # (test_1567); not starred, the 8B stays the GPU boxes' embedder.
        "hardware": ["amd", "cpu", "nvidia"],
        "recommended": False,
    },
    {
        "name": "nomic-embed-text",
        "display": "Nomic Embed Text v1.5 (F16)",
        "description": "Lightweight 768-dim embedding model. Lower RAM footprint.",
        "task": "embed",
        "repo_id": "nomic-ai/nomic-embed-text-v1.5-GGUF",
        "filename": "*f16*.gguf",
        "engine": "llamacpp",
        "hardware": ["amd", "cpu", "nvidia"],
        "recommended": False,
    },
    {
        "name": "qwen3-reranker",
        "display": "Qwen3 Reranker 0.6B (Q8)",
        "description": "Default reranker for RAG pipelines. Small + fast.",
        "task": "rerank",
        "repo_id": "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF",
        "filename": "qwen3-reranker-0.6b-q8_0.gguf",
        "engine": "llamacpp",
        # #1518 (E5): "nvidia" is BACK here too — see the qwen3.6 entry above.
        # The EXO-4 exclusion that used to be described here is gone from the
        # list; this comment was left behind claiming it.
        "hardware": ["amd", "cpu", "nvidia"],
        "recommended": True,
    },
    # #1518 (E5): the two vLLM entries lived here. GGUF is the only model
    # architecture now — an NVIDIA box serves the same catalog as every other,
    # through the CUDA llama.cpp runner its GPU selects (#1516/#1517). The
    # repo-DIR convention they documented (filename None, the node pulls a whole
    # HF repo) went with them; every entry is a GGUF file again.
]


def companion_files(*, repo_id: Optional[str] = None, name: Optional[str] = None) -> list[str]:
    """Extra files (beyond the chosen weight quant) a model needs to serve ALL
    its use cases — today the vision ``mmproj`` projector. Returned so the
    deploy + cache paths can guarantee a model is pulled COMPLETE (weights +
    projector), the way GPUStack's whole-repo snapshot did implicitly. Naming
    the projector is required because it's a repo-LEVEL companion, absent from
    the per-quant file listing.

    #1256: the filename comes from the model manifest — the single declaration
    for the whole stack — NOT from this module and NOT from the repo's HF
    listing. See ``app/model_manifest.py`` for why both alternatives are wrong.
    Offline-safe: one local YAML read, never HF. Matched by name first (exact
    identity), then by repo_id (survives an alias rename; the projector is a
    property of the repo).
    """
    hit = mmproj_for(repo_id=repo_id, name=name)
    return [hit] if hit else []


def complete_files(files, *, repo_id: Optional[str] = None,
                   name: Optional[str] = None) -> list[str]:
    """``files`` guaranteed to carry the model's vision projector — the ONE
    choke point every deploy passes through (#1256).

    Callers that build a ``files`` list from a weight-quant listing (the
    console's quant editor gets it from the HF quant grouping, which has no
    repo-level companion in it) cannot know about the projector, and a new
    caller would forget it again. Completing it server-side in
    ``POST /api/deployments`` means the node pulls weights AND projector, the
    #307 registry mirror caches the model COMPLETE, and the node driver's
    ``pick_mmproj`` finds something to hand ``--mmproj``.

    Two deliberate non-actions:

    * an EMPTY ``files`` list is left empty — that is the #574 repo-DIRECTORY
      deploy (vLLM), where the node pulls the whole repo and adding one
      filename would narrow it to a single-file pull;
    * a list that already carries a projector is returned unchanged, so the
      CLI's explicit send and this completion cannot double it.
    """
    out = list(files or [])
    if not out:
        return out
    if any("mmproj" in str(f).lower() for f in out):
        return out
    return out + companion_files(repo_id=repo_id, name=name)


#: #1518 (E5): the ONE hardware-family rule for the catalog view. It has to
#: agree with the manager's placement (`api/inventory.py::_hardware_family`),
#: which is substring-based for exactly this reason: labels vary by how a node
#: registered (`cuda` vs `nvidia`, `amd` vs `amd-gfx1151`) and an exact compare
#: 409'd deploys where both sides were right. The console filtered the catalog
#: on the literal label, so a GB10 (`cuda-gb10`) matched nothing.
def hardware_family(value: Optional[str]) -> str:
    h = (value or "").lower()
    if ("amd" in h) or ("gfx" in h) or ("rocm" in h) or ("vulkan" in h):
        return "amd"
    if ("nvidia" in h) or ("cuda" in h):
        return "nvidia"
    if ("apple" in h) or ("metal" in h) or ("mlx" in h):
        return "apple"
    if h == "cpu" or h.endswith("-cpu") or "cpu" in h.split("-"):
        return "cpu"
    return h


def list_catalog(task: Optional[str] = None, hardware: Optional[str] = None) -> list[dict]:
    """The catalog, optionally filtered by serve task and/or hardware class.

    #1518 (E5): the ``engine == "vllm"`` view gate (#1182,
    ``LLM_MANAGER_ENABLE_VLLM``) is gone with the entries it hid — GGUF is the
    only model architecture. #1256's VIEW-level field stays: the console needs
    the resolved ``mmproj``, and the ``CATALOG`` constant must not hold it
    (the #985 tool-calling guard reads the constant directly).

    ``hardware`` is matched on the FAMILY, not the literal label: a worker
    registers ``cuda`` (or, until it re-registers, ``cuda-gb10``), while an
    entry lists ``nvidia``. Comparing the strings meant a GB10 matched no entry
    at all and the console showed it an empty catalog.
    """
    out = list(CATALOG)
    if task:
        out = [e for e in out if e["task"] == task]
    if hardware:
        want = hardware_family(hardware)
        out = [e for e in out
               if any(hardware_family(h) == want for h in e.get("hardware", []))]
    # #1256: hand the console the RESOLVED projector so it never carries a
    # filename of its own. A VIEW field — CATALOG itself stays projector-free.
    return [dict(e, mmproj=(mmproj_for(repo_id=e.get("repo_id"),
                                       name=e.get("name")) or None),
                 egress=egress_for(name=e.get("name"), repo_id=e.get("repo_id"),
                                   files=[e["filename"]] if e.get("filename") else None),
                 # #3403: "experimental" when the manifest sends this model to
                 # the experimental runner channel; None = the stable runner.
                 runner=runner_channel_for(name=e.get("name"), repo_id=e.get("repo_id")),
                 # #3403: False when the model needs the experimental runner and
                 # this box has not opted in — the console shows the entry as
                 # "experimental runner — off on this box" with the notice, and
                 # the deploy would be refused (422 experimental_runner_off).
                 runner_available=(runner_channel_for(name=e.get("name"), repo_id=e.get("repo_id"))
                                   != "experimental" or experimental_runner_enabled()),
                 runner_notice=(None if (runner_channel_for(name=e.get("name"), repo_id=e.get("repo_id"))
                                         != "experimental" or experimental_runner_enabled())
                                else EXPERIMENTAL_RUNNER_OFF_NOTICE))
            for e in out]


#: #2394: console catalogue entries that are NOT pinned in the model manifest,
#: on purpose, each with the reason. Pins live only in
#: core/llm/standard-models.yaml, and every entry there is part of the standard
#: set the CLI deploys, so an entry that is not a standard model cannot be
#: pinned without becoming one. A one-click deploy of an entry listed here goes
#: through the unverified-model notice like any other unverified model.
#: tests/unit/llm-manager/test_2394_every_catalogue_entry_is_verified_or_listed.py
#: holds every other entry to "pinned".
UNVERIFIED_BY_DESIGN: dict[str, str] = {
    "nomic-embed-text": (
        "Removed from the standard set (#2236); its weight name is a wildcard "
        "(*f16*.gguf), so the manifest has no entry to pin. Pinning it means "
        "re-adding it to the standard set, which the operator decided against."),
}


def _entry_files(row: dict) -> list[str]:
    """The files a deploy of catalogue ``row`` loads: the weight (a sharded
    entry is a glob) and its projector. A glob is expanded to the shards the
    manifest pins (#2392), so "held" means every shard, not one of them."""
    from app.model_manifest import pins_for
    files = [f for f in (row.get("filename"), row.get("mmproj")) if f]
    pins = pins_for(row.get("repo_id"), files)
    if pins and pins.get("sha256"):
        return sorted(pins["sha256"])
    return files


def _report_holds(report: Optional[dict], files: list[str], repo_id: Optional[str]) -> bool:
    """``_worker_has_files`` for exact names; an unpinned glob is held when at
    least one file on the worker's disk matches it."""
    import fnmatch
    import os
    from app.api.inventory import _names_on_disk, _worker_has_files
    exact = [f for f in files if not any(c in f for c in "*?[")]
    globs = [os.path.basename(f) for f in files if f not in exact]
    if not report or not files:
        return False
    if exact and not _worker_has_files(report, exact, repo_id):
        return False
    on_disk = _names_on_disk(report)
    return all(any(fnmatch.fnmatchcase(n, g) for n in on_disk) for g in globs)


def annotate_offline(rows: list[dict]) -> list[dict]:
    """#2426 ask 3: on an OFFLINE box an entry is deployable only when some
    worker already holds its files (weight + projector) — nothing can fetch
    them. That is the console form of G17's verdict ("on-demand for this
    class and absent → unavailable offline"). Judged on each worker's LAST
    disk report (the #306 channel the Fleet cache view already uses); online
    the answer is None, the fetch will happen."""
    from app.api.hf import hf_allowed
    if hf_allowed():
        return [dict(r, offline_available=None) for r in rows]
    from app.api.inventory import _latest_disk_report
    from app.db import session_scope
    from app.models import Worker
    with session_scope() as s:
        reports = [_latest_disk_report(s, w.id) for w in s.query(Worker).all()]
    out = []
    for r in rows:
        files = _entry_files(r)
        out.append(dict(r, offline_available=any(
            _report_holds(rep, files, r.get("repo_id")) for rep in reports)))
    return out


def register_catalog_api(app) -> None:
    # #314: the deployable-model catalog pre-fills the Deploy form → ADMIN tier.
    router = APIRouter(dependencies=[Depends(require_role(Role.ADMIN))])

    @router.get("/api/catalog")
    def get_catalog(task: Optional[str] = None, hardware: Optional[str] = None):
        # #2426: the rows carry `offline_available`; list_catalog stays DB-free.
        return annotate_offline(list_catalog(task=task, hardware=hardware))

    app.include_router(router)
