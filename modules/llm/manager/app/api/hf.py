# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#295 HuggingFace model browser for the Deploy flow.

Search HuggingFace for GGUF model repos, inspect a repo's quant files (sizes +
multi-part shard grouping), read its README, and check whether each quant fits a
worker's memory budget — so an operator can pick + deploy a model without
leaving the console (GPUStack-style).

  GET /api/hf/search?q=<query>   GGUF repos matching the query (by downloads)
  GET /api/hf/repo/{repo_id}     files→quants (+ sizes/parts), README, fits-check

Admin-gated (require_admin, like the rest of /api/*). NETWORK-MODE GUARDED: HF is
only reached in online/proxied — in offline mode it returns 503 (the offline path
is the in-stack Zot mirror). In proxied mode httpx inherits HTTPS_PROXY via
trust_env, so the calls ride the corporate proxy (mirrors worker-agent hf_pull).

All HTTP is bounded + best-effort: a HF outage returns a clean 502/503, never a
hang or a 500 stack trace. The deploy itself is unchanged — the browser just
pre-fills POST /api/deployments (model_name + files + hf_repo), which already
fetches the weights on the worker (#287).
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.authz import Role, require_role
from app.db import session_scope

logger = logging.getLogger("orchestrator.hf")

_HF_API = "https://huggingface.co/api"
_HF_HOST = "https://huggingface.co"
_HTTP_TIMEOUT = 15.0
# #352 One budget for the WHOLE repo lookup, not five independent ones.
#
# `hf_repo` issues up to five sequential upstream calls, each bounded at
# _HTTP_TIMEOUT, with nothing bounding the sequence — so a slow-but-not-dead
# HuggingFace (or a corporate proxy adding latency in `proxied` mode) gives a
# worst case near 75 s for one console click. `hf_repo` is a plain `def`, so
# FastAPI runs it in the anyio worker threadpool: each in-flight lookup holds a
# pooled thread that the rest of the management API shares, and the console has no
# client-side timeout, so the operator just watches a spinner.
#
# Calls 2-5 are already best-effort — each degrades gracefully on failure — which
# is what makes this cheap: they can be time-bounded as a group without changing
# what a successful lookup returns. Returning the repo with `arch: null` after 25 s
# beats returning the complete answer after 75.
_LOOKUP_BUDGET = 25.0      # whole-handler ceiling
_ENRICH_TIMEOUT = 5.0      # per optional call, further capped by what is left
_MIN_CALL_SECONDS = 0.5    # below this, skip rather than start a doomed request
_README_MAX = 250000
# leave headroom over the raw weight size for the KV cache + engine runtime.
_FIT_SAFETY = 0.90

# quant label from a GGUF filename: ...-Q4_K_M.gguf / .IQ3_XXS. / -Q8_0- / f16
_QUANT_RE = re.compile(
    r"(?i)(?:^|[.\-_])((?:I?Q\d(?:_[0-9A-Za-z]+)*)|F16|FP16|BF16|F32)(?:[.\-_]|$)"
)
# multi-part shard suffix: model-00001-of-00003.gguf
_PART_RE = re.compile(r"(?i)-\d{5}-of-\d{5}\.gguf$")

# UI sort label → HF Hub server-side sort key.
_HF_SORT = {"trending": "trendingScore", "downloads": "downloads",
            "likes": "likes", "updated": "lastModified"}
# UI format → the HF query marker for a servable WORKER TYPE (repeated params).
_HF_FORMAT = {
    "gguf": [("filter", "gguf")],                      # llama.cpp (AMD / CPU)
    "safetensors": [("filter", "safetensors")],        # vLLM (NVIDIA)
    "vllm": [("filter", "safetensors")],
    "mlx": [("filter", "mlx")],                        # Mac gateway (verified:
    #   filter=mlx returns mlx-community repos; library=mlx does NOT filter)
}
# UI task → HF pipeline_tag (rerank has no reliable tag → left unfiltered).
_HF_TASK = {"chat": "text-generation", "embed": "sentence-similarity",
            "vision": "image-text-to-text"}

# Recommended sampling params to scrape from a model card ("recommended
# settings: temperature=0.7, top_p=0.8, …"). alias → canonical param name; the
# value is the first plausible number that follows the term within ~24 chars.
_REC_PARAMS = {
    "temperature": "temperature", "temp": "temperature",
    "top_p": "top_p", "top-p": "top_p",
    "top_k": "top_k", "top-k": "top_k",
    "min_p": "min_p", "min-p": "min_p",
    "presence_penalty": "presence_penalty", "presence penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty", "frequency penalty": "frequency_penalty",
    "repetition_penalty": "repeat_penalty", "repeat_penalty": "repeat_penalty",
    "repeat penalty": "repeat_penalty", "repetition penalty": "repeat_penalty",
}


class _Deadline:
    """#352 Remaining-time budget for a multi-call handler.

    ``timeout(cap)`` returns the timeout for the next OPTIONAL call, or None when
    too little budget is left to be worth starting one. Starting a request with
    0.1 s left just guarantees a timeout exception and burns the connection setup,
    so it is skipped instead.

    Injectable clock so the behaviour is testable without sleeping.
    """

    def __init__(self, budget: float, clock=None):
        self._clock = clock or time.monotonic
        self._end = self._clock() + budget

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def timeout(self, cap: float):
        left = self.remaining()
        if left < _MIN_CALL_SECONDS:
            return None
        return min(cap, left)

    def expired(self) -> bool:
        return self.remaining() < _MIN_CALL_SECONDS


def arch_from_config(cfg: dict) -> Optional[dict]:
    """Extract the dims the KV-cache estimate needs from a HF config.json:
    layers, kv-heads (GQA-aware), head-dim. None if the essentials are absent.
    Handles the nested ``text_config`` some multimodal configs use."""
    if not isinstance(cfg, dict):
        return None
    c = cfg.get("text_config") if isinstance(cfg.get("text_config"), dict) else cfg
    layers = c.get("num_hidden_layers") or c.get("n_layer")
    n_heads = c.get("num_attention_heads") or c.get("n_head")
    kv_heads = c.get("num_key_value_heads") or n_heads
    hidden = c.get("hidden_size") or c.get("n_embd")
    head_dim = c.get("head_dim")
    if head_dim is None and hidden and n_heads:
        try:
            head_dim = int(hidden) // int(n_heads)
        except (TypeError, ValueError, ZeroDivisionError):
            head_dim = None
    if not (layers and kv_heads and head_dim):
        return None
    try:
        return {"layers": int(layers), "kv_heads": int(kv_heads), "head_dim": int(head_dim)}
    except (TypeError, ValueError):
        return None


# GGUF/MLX repos rarely carry config.json — but their card frontmatter names a
# `base_model` (org/name) whose config we can read for the arch dims.
_BASE_MODEL_RE = re.compile(
    r"(?im)^\s*base_model:\s*(?:\n\s*-\s*)?([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)")

# #351: a repo id is exactly ``owner/name``. Both this and the card-supplied
# base_model are interpolated into upstream URLs, so validate the shape before use.
# The charset alone is not enough — ``.`` and ``..`` both match ``[A-Za-z0-9._-]+``,
# so a value like ``../..`` would pass the regex and then normalise away a URL
# segment. Reject dot segments explicitly.
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def valid_repo_id(repo_id: Optional[str]) -> bool:
    """True iff ``repo_id`` is a well-formed ``owner/name`` with no dot segments."""
    if not repo_id or not _REPO_ID_RE.match(repo_id):
        return False
    return all(seg not in (".", "..") for seg in repo_id.split("/"))


def base_model_from_readme(readme: str) -> Optional[str]:
    m = _BASE_MODEL_RE.search(readme or "")
    if not m:
        return None
    # the card is third-party content — never let it steer a URL (#351)
    return m.group(1) if valid_repo_id(m.group(1)) else None


def parse_recommended_params(readme: str) -> dict:
    """Best-effort scrape of a model card's recommended sampling settings, so the
    editor can pre-fill fields tagged 'from the model card'. Only clear
    ``<term> = <number>`` / ``<term>: <number>`` / ``<term> of <number>`` forms
    within a short window are taken — conservative, never guesses."""
    out: dict = {}
    if not readme:
        return out
    low = readme.lower()
    for alias, canon in _REC_PARAMS.items():
        if canon in out:
            continue
        m = re.search(re.escape(alias) + r"[^\n]{0,24}?(?:[=:]|\bof\b)\s*([0-9]+(?:\.[0-9]+)?)", low)
        if m:
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            # sanity clamp: reject obviously-not-a-param captures
            if canon in ("top_k",) and not (0 <= val <= 500):
                continue
            if canon not in ("top_k",) and not (0 <= val <= 10):
                continue
            out[canon] = int(val) if canon == "top_k" else val
    return out


def network_mode() -> str:
    return (os.environ.get("RAZZFAZZ_NETWORK_MODE") or "online").strip().lower()


def hf_allowed() -> bool:
    """HF is reachable in online/proxied, refused in offline (mirrors hf_pull)."""
    return network_mode() != "offline"


def quant_of(fname: str) -> str:
    m = _QUANT_RE.search(fname or "")
    return m.group(1).upper() if m else "other"


def part_group(fname: str) -> Optional[str]:
    """The group key shared by a multi-part quant's shards (filename with the
    ``-NNNNN-of-MMMMM`` suffix stripped), or None for a single-file quant."""
    return _PART_RE.sub(".gguf", fname) if _PART_RE.search(fname or "") else None


def build_quants(files: list, workers: list[dict]) -> list[dict]:
    """Group a repo's GGUF files into quants (single-file or multi-part shard
    sets), sum shard sizes, and compute a per-worker fits flag (total weight <
    budget × safety). Sorted smallest-first; unknown-size last."""
    groups: dict[str, dict] = {}
    for f in files or []:
        name = (f.get("path") or f.get("rfilename") or "") if isinstance(f, dict) else ""
        if not name.lower().endswith(".gguf"):
            continue
        size = 0
        if isinstance(f, dict):
            size = int(f.get("size") or (f.get("lfs") or {}).get("size") or 0)
        key = part_group(name) or name
        g = groups.setdefault(key, {"label": quant_of(name), "files": [], "bytes": 0, "parts": 0})
        g["files"].append(name)
        g["bytes"] += size
        g["parts"] += 1
    quants = []
    for g in groups.values():
        size_gb = round(g["bytes"] / 1e9, 2) if g["bytes"] else 0.0
        fits = [{"worker": w["name"], "mem_gb": w["mem_gb"], "source": w.get("source"),
                 "ok": bool(size_gb) and bool(w["mem_gb"]) and size_gb <= w["mem_gb"] * _FIT_SAFETY}
                for w in workers]
        quants.append({"label": g["label"], "files": sorted(g["files"]),
                       "size_gb": size_gb, "parts": g["parts"], "fits": fits})
    quants.sort(key=lambda q: (q["size_gb"] == 0, q["size_gb"]))
    return quants


def worker_budgets(session) -> list[dict]:
    """[{name, mem_gb, source}] for ready workers — the memory a model may use:
    VRAM (rocm/nvidia-smi, reported by the node) when known, else host RAM. On
    unified-memory boxes (Strix Halo) host /proc/meminfo under-reports because
    the BIOS pins VRAM out of system RAM, so VRAM is the honest ceiling."""
    from app.models import Worker

    out = []
    for w in (session.query(Worker).filter(Worker.status == "ready")
              .order_by(Worker.name).all()):
        labels = w.labels or {}
        vram = labels.get("vram_total_gb")
        ram = labels.get("mem_total_gb")
        # #2141: a CPU worker's budget is host RAM; a VRAM label it still reports
        # (older agent, host iGPU) is not its budget. Same rule as
        # inventory._capacity_source, so the fits-check and the gate agree.
        from app.api.inventory import _hardware_family
        if _hardware_family(labels.get("hardware")) == "cpu":
            vram = None
        budget, source = None, None
        for val, src in ((vram, "vram"), (ram, "ram")):
            if val:
                try:
                    budget, source = round(float(val), 1), src
                    break
                except (TypeError, ValueError):
                    continue
        if budget:
            out.append({"name": w.name, "mem_gb": budget, "source": source})
    return out


def _client():
    """The HuggingFace HTTP client. Module level since #1573 — `fetch_repo_facts`
    is callable outside a request, so its client has to be too.

    trust_env=True → inherits HTTPS_PROXY in proxied mode (mirrors hf_pull).
    """
    import httpx

    return httpx.Client(timeout=_HTTP_TIMEOUT, trust_env=True, follow_redirects=True,
                        headers={"user-agent": "razzfazz-llm-manager/hf-browser"})


def fetch_repo_facts(repo_id: str, *, budget_s: float = _LOOKUP_BUDGET) -> dict:
    """The HuggingFace half of a repo lookup, callable without a request (#1573).

    Returns ``{files, arch, readme, info, partial}`` — the repo's file tree WITH
    SIZES, the architecture from `config.json`, the model card, the repo info,
    and whether the enrichment budget ran out.

    Why it is a function and not the body of a route: `est_gb` is what makes the
    #227 admission gate work at all (`if not est_gb … return True`), and it is
    computed from a quant's SIZE. Those sizes already come down this wire — the
    console asks for them, uses them, and the deploy path never sees them. A
    deploy that names only a repo therefore had nothing to base a footprint on,
    which is why the door refuses it (#1529 review). One function, two callers,
    no second copy of the query: a console and a deploy path that disagree about
    what a repo contains is worse than either being wrong alone.

    `budget_s` is the whole-lookup ceiling. The console route keeps the full
    one; a caller for whom the answer is OPTIONAL should pass a small one, so
    that a courtesy lookup can never hold a request open for half a minute
    (#1648 — the deploy path derives an estimate this way, and an estimate is
    not worth blocking a deploy for).

    Raises HTTPException the same way the route did — 422 for a malformed id,
    503 offline, 404 unknown repo, 502 for an upstream failure. Offline is a
    REFUSAL, not an empty result: an estimate of nothing is exactly how the gate
    went inert in the first place.
    """
    import httpx

    if not valid_repo_id(repo_id):
        raise HTTPException(422, "repo_id must be 'owner/name'")
    if not hf_allowed():
        raise HTTPException(503, "HuggingFace lookup needs network access — this box "
                                 "is in offline mode")

    info: dict = {}
    readme = ""
    arch = None
    deadline = _Deadline(budget_s)
    try:
        with _client() as c:
            tree = c.get(f"{_HF_API}/models/{repo_id}/tree/main",
                         params={"recursive": "true"})
            if tree.status_code == 404:
                raise HTTPException(404, f"HuggingFace repo not found: {repo_id}")
            tree.raise_for_status()
            files = tree.json()

            def _enrich(url):
                """One optional call, or None when the budget is spent."""
                t = deadline.timeout(_ENRICH_TIMEOUT)
                if t is None:
                    return None
                return c.get(url, timeout=t)

            try:
                ir = _enrich(f"{_HF_API}/models/{repo_id}")
                if ir is not None and ir.status_code == 200:
                    info = ir.json() or {}
            except httpx.HTTPError:
                pass
            try:
                rr = _enrich(f"{_HF_HOST}/{repo_id}/raw/main/README.md")
                if rr is not None and rr.status_code == 200:
                    readme = (rr.text or "")[:_README_MAX]
            except httpx.HTTPError:
                pass
            try:
                cr = _enrich(f"{_HF_HOST}/{repo_id}/raw/main/config.json")
                if cr is not None and cr.status_code == 200:
                    arch = arch_from_config(cr.json())
            except (httpx.HTTPError, ValueError):
                pass
            # GGUF/MLX repos usually lack config.json — fall back to the
            # base_model's config (named in the card frontmatter).
            if arch is None:
                base = base_model_from_readme(readme)
                if base and base.lower() != repo_id.lower():
                    try:
                        br = _enrich(f"{_HF_HOST}/{base}/raw/main/config.json")
                        if br is not None and br.status_code == 200:
                            arch = arch_from_config(br.json())
                    except (httpx.HTTPError, ValueError):
                        pass
            if deadline.expired():
                logger.info("hf repo lookup for %s hit the %.0fs budget — "
                            "returning a partial result (#352)", repo_id, budget_s)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        raise HTTPException(502, f"HuggingFace lookup failed: {e}")

    return {"files": files if isinstance(files, list) else [],
            "arch": arch, "readme": readme, "info": info,
            "partial": deadline.expired()}


def register_hf_api(app) -> None:
    # #314: the HuggingFace model browser feeds the Deploy form → ADMIN tier.
    router = APIRouter(dependencies=[Depends(require_role(Role.ADMIN))])

    @router.get("/api/hf/search")
    def hf_search(
        q: str = Query("", max_length=200),   # empty = browse all (by sort/filters)
        limit: int = Query(30, ge=1, le=60),
        sort: str = Query("downloads"),   # trending | downloads | likes | updated
        format: str = Query("gguf"),      # gguf | safetensors | mlx  (→ worker type)
        task: Optional[str] = Query(None),  # chat | embed | rerank | vision
    ):
        if not hf_allowed():
            raise HTTPException(503, "HuggingFace search needs network access — this "
                                     "box is in offline mode (use the central registry "
                                     "mirror for offline model distribution)")
        import httpx

        # HF Hub params (a list → repeated keys where needed). sort is one of
        # HF's server-side sorts so "trending" / "most downloads" etc. are real.
        # An empty query omits `search` → HF returns the top models for the
        # chosen sort + filters, so an empty box browses instead of showing empty.
        params: list = [("limit", str(limit)), ("direction", "-1"),
                        ("sort", _HF_SORT.get(sort, "downloads"))]
        if q.strip():
            params.append(("search", q.strip()))
        # format → the marker that a WORKER TYPE can serve: GGUF (llama.cpp),
        # safetensors (vLLM), mlx (Mac gateway). So you can't pick a format
        # nothing in the fleet can run.
        params += _HF_FORMAT.get((format or "gguf").lower(), [("filter", "gguf")])
        if task and task in _HF_TASK:
            params.append(("pipeline_tag", _HF_TASK[task]))
        try:
            with _client() as c:
                r = c.get(f"{_HF_API}/models", params=params)
                r.raise_for_status()
                rows = r.json()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"HuggingFace search failed: {e}")
        results = []
        for m in rows or []:
            rid = m.get("id") or m.get("modelId")
            if rid:
                results.append({"id": rid, "downloads": m.get("downloads"),
                                "likes": m.get("likes"), "last_modified": m.get("lastModified"),
                                "gated": bool(m.get("gated")), "pipeline_tag": m.get("pipeline_tag"),
                                "library": m.get("library_name"),
                                "trending_score": m.get("trendingScore")})
        return {"query": q, "sort": sort, "format": (format or "gguf").lower(),
                "task": task, "results": results}

    @router.get("/api/hf/repo/{repo_id:path}")
    def hf_repo(repo_id: str):
        # #1573: the lookup itself lives in `fetch_repo_facts` so the deploy path
        # can ask the same question. This route only shapes the answer for the
        # console — validation, offline refusal and the enrichment budget all
        # happen in there, once.
        facts = fetch_repo_facts(repo_id)
        files = facts["files"]
        info = facts["info"]
        readme = facts["readme"]
        arch = facts["arch"]

        with session_scope() as s:
            workers = worker_budgets(s)
        quants = build_quants(files if isinstance(files, list) else [], workers)
        out = {"repo_id": repo_id, "downloads": info.get("downloads"),
               "likes": info.get("likes"), "gguf": bool(quants), "quants": quants,
               "workers": workers, "readme_md": readme, "arch": arch,
               "recommended_params": parse_recommended_params(readme),
               # #352 say so when the enrichment budget ran out. A missing `arch`
               # otherwise looks like "this repo has no config.json" — which is a
               # normal, permanent state for GGUF repos — rather than "we ran out
               # of time and did not look". The console can offer a retry for one
               # and should not for the other.
               "partial": facts["partial"]}
        if not quants:
            out["note"] = ("No GGUF files in this repo — the manager serves GGUF weights "
                           "(llama.cpp). Pick a *-GGUF repo.")
        return out

    app.include_router(router)
