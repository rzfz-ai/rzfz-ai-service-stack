# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#289 — read the central model registry (Zot) contents for the console.

Surfaces what's mirrored in the in-stack Zot registry (llm-registry:5000) so the
Catalog page can show the three tiers: HuggingFace source → central registry
(offline/fleet distribution) → per-node volume. Read-only + admin-gated; the
registry being down/absent is reported gracefully (available=false), never a 500.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.authz import Role, require_role
# #307 S5: module-level (NOT deferred) on purpose — a function-local
# `from app.models import ...` re-resolves against whatever `sys.modules`
# holds at CALL time, and this suite's `app`-package isolation hook
# (tests/conftest.py::pytest_runtest_setup) evicts+reloads `app.*` between
# module collection and the first test executing. A deferred import of an
# ORM class would then bind to a DIFFERENT (freshly reloaded) class object
# than the one a test file captured at its own module level, breaking every
# `cls is Deployment` identity check a fake session relies on. Importing
# here, at module load (the same collection-time window every test file's
# own top-level imports run in), keeps identity consistent — the same
# convention `inventory.py` already uses for these exact classes.
from app.api.inventory import _network_allows_hf, _zot_repo_ref
from app.model_manifest import node_store_name
from app.model_origin import model_origin
from app.db import session_scope
from app.runner_targets import RUNNER_REPO_PREFIX, declared_targets, target_rows
from app.models import Deployment, Model


MANIFEST_ACCEPT = ("application/vnd.oci.image.manifest.v1+json,"
                   "application/vnd.docker.distribution.manifest.v2+json")

#: Whole-walk budget in seconds (#362). The per-call `timeout=4` bounds ONE
#: request; the walk is catalog → tags-per-repo → manifest-per-tag, which at the
#: 500-repo cap is ~1500 sequential requests, so the total was unbounded in
#: practice while the console refetched it every 5 s. Each concurrent viewer
#: holds an anyio worker thread for the whole walk (the handler is a plain
#: `def`), so an unbounded walk is a thread-pool problem, not just a slow page.
#:
#: Overridable so an operator with a genuinely large store can raise it rather
#: than being stuck with a permanently truncated view.
REGISTRY_INVENTORY_BUDGET_S = float(
    os.environ.get("LLM_REGISTRY_INVENTORY_BUDGET_S", "15") or 15
)
REGISTRY_REPO_CAP = 500

#: #2530: the hub repository of the worker-agent image (post-install pushes it
#: as ${RAZZFAZZ_NODE_REPO_PREFIX}/razzfazz-llm-worker-agent).
AGENT_IMAGE_HUB_REPO = "node/razzfazz-llm-worker-agent"
#: Every manifest media type, the index types included: a node's RepoDigests
#: records the digest of whatever it pulled, and a multi-platform push is an
#: index. Asking for a single manifest only would answer a different digest.
AGENT_MANIFEST_ACCEPT = ("application/vnd.oci.image.index.v1+json, "
                         "application/vnd.docker.distribution.manifest.list.v2+json, "
                         "application/vnd.oci.image.manifest.v1+json, "
                         "application/vnd.docker.distribution.manifest.v2+json")
HUB_DIGEST_TTL_S = 60
_HUB_DIGEST_CACHE: dict[str, tuple[float, Optional[str]]] = {}
#: tag → the platform digests of the hub's manifest list under it, filled by
#: the same lookup as _HUB_DIGEST_CACHE (empty for a single manifest).
_HUB_PLATFORM_CACHE: dict[str, frozenset] = {}


def hub_agent_digest(tag: str = "latest") -> Optional[str]:
    """#2530: the hub's manifest digest of node/razzfazz-llm-worker-agent:<tag>,
    or None when the registry does not answer. Cached 60 s — GET /api/workers
    is polled by the console every few seconds."""
    now = time.monotonic()
    hit = _HUB_DIGEST_CACHE.get(tag)
    if hit and now - hit[0] < HUB_DIGEST_TTL_S:
        return hit[1]
    base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
    digest: Optional[str] = None
    platforms: frozenset = frozenset()
    try:
        import httpx

        url = f"{base}/v2/{AGENT_IMAGE_HUB_REPO}/manifests/{tag}"
        # in-network (Zot) — #1409
        with httpx.Client(timeout=5, trust_env=False) as c:
            r = c.head(url, headers={"Accept": AGENT_MANIFEST_ACCEPT})
            if r.status_code == 200:
                digest = r.headers.get("Docker-Content-Digest") or None
                ctype = (r.headers.get("Content-Type") or "").lower()
                if digest and ("index" in ctype or "manifest.list" in ctype):
                    platforms = _list_platform_digests(c, url)
    except Exception:  # noqa: BLE001 — unknown is an answer, not an error
        digest = None
    _HUB_DIGEST_CACHE[tag] = (now, digest)
    _HUB_PLATFORM_CACHE[tag] = platforms
    return digest


def _list_platform_digests(client, url: str) -> frozenset:
    """The per-platform manifest digests of a manifest list / OCI index. An
    unreadable body is no halves, never a failed lookup: the list's own digest
    still stands."""
    try:
        body = client.get(url, headers={"Accept": AGENT_MANIFEST_ACCEPT}).json()
        return frozenset(m["digest"] for m in (body.get("manifests") or [])
                         if isinstance(m, dict) and isinstance(m.get("digest"), str)
                         and m["digest"].startswith("sha256:"))
    except Exception:  # noqa: BLE001
        return frozenset()


def hub_agent_platform_digests(tag: str = "latest") -> frozenset:
    """#2530 (box acceptance 2026-11-16): the platform halves of the hub's
    multi-arch :<tag>. A node that runs one of them runs the hub's image even
    though its RepoDigests carry only the platform digest — the master's own
    agent, pushed per platform, is exactly that. Filled by the same lookup
    as hub_agent_digest(), so ask that first: this only reads its result."""
    return _HUB_PLATFORM_CACHE.get(tag, frozenset())


def walk_registry_models(client, base: str, *, budget_s: float | None = None,
                         clock=None) -> dict:
    """Walk the Zot catalog within a TOTAL time budget (#362).

    Returns the inventory dict. When the budget runs out the walk stops and the
    result carries ``truncated: True`` plus counts, so the console can say
    "showing N of M" instead of presenting a partial store as the whole store —
    which is the part that actually misleads: a truncated inventory looks
    exactly like a smaller cache.

    ``client`` and ``clock`` are injected so this is testable without a registry.

    Sizes (#1179): Zot is content-addressed and dedupes blobs GLOBALLY — a
    model mirrored under both ``latest`` (manager auto-mirror, #828) and
    ``deployed`` (worker-agent ``_auto_cache_deploy``) is on disk ONCE. So
    ``total_bytes`` is the sum over the set of UNIQUE blob digests (config +
    layers) seen across the whole walk, which is what the store physically
    holds. Each row keeps its own ``size_bytes`` (what that one tag weighs)
    plus ``config_digest``/``config_size`` so the console can dedupe per repo
    from the same digests, and the naive per-tag sum is reported as
    ``total_bytes_tagged`` for transparency. A blob without a digest cannot be
    deduped and is counted — undercounting is the worse error for capacity
    planning.
    """
    import time as _time

    now = clock or _time.monotonic
    budget = REGISTRY_INVENTORY_BUDGET_S if budget_s is None else budget_s
    deadline = now() + budget

    out: dict = {"base": base, "available": False, "models": [], "total_bytes": 0,
                 "total_bytes_tagged": 0, "truncated": False}
    seen_digests: set = set()

    def _count_unique(digest, size: int) -> None:
        # No digest → cannot dedupe → count it (see docstring).
        if digest and digest in seen_digests:
            return
        if digest:
            seen_digests.add(digest)
        out["total_bytes"] += size
    r = client.get(f"{base}/v2/_catalog")
    r.raise_for_status()
    out["available"] = True
    repos = ((r.json() or {}).get("repositories") or [])
    out["repositories_total"] = len(repos)
    if len(repos) > REGISTRY_REPO_CAP:
        out["truncated"] = True
    repos = repos[:REGISTRY_REPO_CAP]

    scanned = 0
    for repo in repos:
        if now() >= deadline:
            out["truncated"] = True
            break
        tr = client.get(f"{base}/v2/{repo}/tags/list")
        tags = ((tr.json() or {}).get("tags") or []) if tr.status_code == 200 else []
        for tag in tags:
            if now() >= deadline:
                out["truncated"] = True
                break
            mr = client.get(f"{base}/v2/{repo}/manifests/{tag}",
                            headers={"Accept": MANIFEST_ACCEPT})
            if mr.status_code != 200:
                continue
            man = mr.json() or {}
            layers = man.get("layers") or []
            config = man.get("config") or {}
            config_digest = config.get("digest") or None
            config_size = int(config.get("size") or 0)
            size = config_size + sum(int(ly.get("size") or 0) for ly in layers)
            files = [{"name": (ly.get("annotations") or {}).get(
                          "org.opencontainers.image.title") or (ly.get("digest") or "")[:19],
                      "digest": ly.get("digest"), "size": int(ly.get("size") or 0)}
                     for ly in layers]
            out["models"].append({"repository": repo, "tag": tag, "size_bytes": size,
                                  "config_digest": config_digest,
                                  "config_size": config_size,
                                  "files": files,
                                  "digest": mr.headers.get("Docker-Content-Digest")})
            out["total_bytes_tagged"] += size
            _count_unique(config_digest, config_size)
            for f in files:
                _count_unique(f["digest"], f["size"])
        scanned += 1
        if out["truncated"] and now() >= deadline:
            break
    out["repositories_scanned"] = scanned
    if out["truncated"]:
        out["truncated_reason"] = (
            f"inventory exceeded its {budget:g}s budget after {scanned} of "
            f"{out['repositories_total']} repositories — showing a partial view"
        )
    return out


#: #3302: the model registry's only writer identity (modules/llm/registry/config.json).
REGISTRY_WRITER = "llm-manager"


def registry_write_auth(base: str) -> Optional[tuple[str, str]]:
    """The registry's writer identity for ``base``, or None (#3302).

    The in-stack registry serves anonymous READS only; a manifest DELETE needs
    the ``llm-manager`` identity (``LLM_REGISTRY_WRITE_PASSWORD``). Sent only to
    the configured registry (``LLM_REGISTRY_URL``, default the in-network
    ``llm-registry:5000``), never to another base a caller passes in."""
    pw = (os.environ.get("LLM_REGISTRY_WRITE_PASSWORD") or "").strip()
    own = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
    if not pw or base.rstrip("/") != own:
        return None
    return (REGISTRY_WRITER, pw)


def evict_registry_repo(repo: str, tag: str = "latest", *, base: Optional[str] = None) -> dict:
    """Evict a model:tag from the in-stack Zot cache — delete its manifest (Zot
    GCs the now-unreferenced blobs). ``repo`` is the full path (e.g.
    ``models/qwen3.6``).

    Factored out of the ``DELETE /api/registry/models/{repo}`` route (#289) so
    #307 S3's fleet-wide "remove from fleet" can call the EXACT same
    delete-by-digest sequence — resolve the tag to its digest, delete by
    digest — instead of growing a second Zot-eviction client that could drift
    from this one. Raises the same ``HTTPException``s the route always raised
    (404 absent, 502 registry trouble); callers that want a soft failure catch
    it themselves.
    """
    if base is None:
        base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
    try:
        import httpx

        # in-network (Zot) — #1409
        with httpx.Client(timeout=10, trust_env=False) as c:
            # OCI manifest delete is by digest → resolve the tag's digest first.
            mr = c.get(f"{base}/v2/{repo}/manifests/{tag}", headers={"Accept": MANIFEST_ACCEPT})
            if mr.status_code == 404:
                raise HTTPException(status_code=404, detail=f"{repo}:{tag} not in cache")
            digest = mr.headers.get("Docker-Content-Digest")
            ref = digest or tag
            # #3302: reads are anonymous, the delete is the writer's
            dr = c.delete(f"{base}/v2/{repo}/manifests/{ref}", auth=registry_write_auth(base))
            if dr.status_code not in (200, 202):
                raise HTTPException(status_code=502,
                                    detail=f"registry delete failed: HTTP {dr.status_code} (is delete enabled?)")
        return {"evicted": f"{repo}:{tag}", "status": "removed"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"registry unreachable: {str(exc)[:150]}")

# ---------------------------------------------------------------------------
# #307 S5 — registry blob retention: GC-eligibility computation + a MANUAL
# Zot GC trigger (v1). LRU/automatic scheduling deferred per the scope doc;
# this NEVER runs on a timer — only when an operator hits the endpoint.
# ---------------------------------------------------------------------------

#: The ONE repository namespace registry GC may ever touch: model weights the
#: manager mirrors (``_zot_repo_ref``) and the worker-agent caches
#: (``_zot_repo``), both ``models/<slug>``. An ALLOW-list on purpose: the hub
#: registry also carries what the fleet pulls to run at all (the worker-agent
#: image under ``node/``, runner images under ``runners/``), and no deployment
#: row ever references those, so a deny-list would turn every infrastructure
#: repository added later into GC fodder the day it is pushed.
GC_MANAGED_REPO_PREFIX = "models/"


def gc_may_touch(repo) -> bool:
    """True only for a single-level ``models/<slug>`` repository — the model
    weights namespace. Anything else (``node/…``, ``runners/…``, a nested or
    odd path) is infrastructure or unknown and is never collected."""
    if not isinstance(repo, str) or not repo.startswith(GC_MANAGED_REPO_PREFIX):
        return False
    slug = repo[len(GC_MANAGED_REPO_PREFIX):]
    return bool(slug) and "/" not in slug and slug not in (".", "..")


def gc_eligible_repos(repos: list, referenced) -> list:
    """Pure set-difference: which Zot repos (``/v2/_catalog``, #289) have NO
    live deployment referencing them right now — restricted to the model
    namespace ``gc_may_touch`` allows (#3096: the worker-agent and runner
    images were listed as eligible because no deployment references them).

    ``repos`` — the registry catalog. ``referenced`` — the Zot repo names a
    live deployment resolves to (``_live_deployment_repos`` below, sharing
    ``inventory.py``'s ``_zot_repo_ref`` naming — #307 S1/S2.5).

    Kept pure (no I/O) so the eligibility RULE is unit-testable without a
    registry or a DB, and so the read-only report and the manual trigger
    below share exactly ONE definition of "eligible". Deliberately
    ``repos - referenced`` (repo present in the catalog, absent from the
    referenced set) — inverting that (``referenced - repos``, or `&` instead
    of the asymmetric subtraction) would make an in-use repo look eligible,
    which is the mutation this slice's test pins against.
    """
    ref = set(referenced)
    return [r for r in repos if gc_may_touch(r) and r not in ref]


def _live_deployment_repos(s) -> set:
    """The Zot repo names backing a LIVE deployment (#307 S5).

    'Live' reuses this manager's existing precedent for the word
    (``inventory.py::_committed_gb`` already filters admission-control
    accounting on ``Deployment.status != "stopped"``): a stopped deployment
    keeps its DB row so ``deploy()`` can resume it later, but it is not
    currently serving anything — it is the correct candidate to reclaim the
    shared registry store for. Resuming a deployment GC'd while stopped just
    re-mirrors from HF once (``_auto_mirror_if_absent``, S1); nothing breaks,
    it merely stops being an instant resume.

    The exclusion is a plain Python ``continue`` (not a SQL filter clause) on
    a full, unfiltered ``.all()`` — same shape ``inventory.py::list_deployments``
    already uses for the same table — so a stray/mutated condition here shows
    up directly in this slice's own tests instead of hiding inside a
    SQLAlchemy expression a fake session can't evaluate.
    """
    out: set = set()
    for dep in s.query(Deployment).all():
        if dep.status == "stopped":
            continue
        served = (s.get(Model, dep.model_id).name if dep.model_id else dep.model_name)
        repo, _tag = _zot_repo_ref(dep.hf_repo or "", served)
        out.add(repo)
    return out


def layer_sizes_from(models: list) -> dict:
    """#3267: layer title -> GB over the registry rows. A title that two layers of
    DIFFERENT size carry (the same file name from two repositories) is dropped:
    no size is better than the wrong one."""
    out: dict = {}
    clash: set = set()
    for m in models or []:
        for f in m.get("files") or []:
            name, size = f.get("name"), f.get("size")
            if not name or not isinstance(size, int) or size <= 0:
                continue
            gb = size / 1e9
            if name in out and abs(out[name] - gb) > 1e-9:
                clash.add(name)
            out.setdefault(name, gb)
    return {k: v for k, v in out.items() if k not in clash}


_LAYER_SIZES: dict = {"at": None, "sizes": {}}
LAYER_SIZES_MAX_AGE_S = 60.0


def registry_layer_sizes(*, now=time.monotonic) -> dict:
    """#3267: the cache's layer sizes for the origin block, when no worker disk
    report holds a file (a model cached in the hub but on no worker yet). The
    deployments list is polled, so the registry walk runs at most once a minute;
    an unreachable registry gives {} (size stays unknown, never a guess)."""
    t = now()
    if _LAYER_SIZES["at"] is not None and t - _LAYER_SIZES["at"] < LAYER_SIZES_MAX_AGE_S:
        return _LAYER_SIZES["sizes"]
    base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
    sizes: dict = {}
    try:
        import httpx

        with httpx.Client(timeout=4, trust_env=False) as c:   # in-network (Zot) — #1409
            sizes = layer_sizes_from((walk_registry_models(c, base) or {}).get("models") or [])
    except Exception:
        sizes = {}
    _LAYER_SIZES.update(at=t, sizes=sizes)
    return sizes


def registry_origins(deps: list, models: list) -> dict:
    """#3267: repository -> origin block for the Model cache rows.

    ``deps``: ``(Deployment, served_name)`` pairs; ``models``: the
    ``walk_registry_models`` rows. A cached repository is attributed to a
    repository on HF only through the deployment record that mirrored it (the
    same ``_zot_repo_ref`` slug), and only when the cached layers are that
    record's files. Otherwise it is ``unknown`` (cached without a deployment
    record, or other files) or ``ambiguous`` (two deployments with different
    repositories share the slug). Never a guess from the name."""
    by_repo: dict = {}
    for dep, served in deps:
        repo, _tag = _zot_repo_ref(dep.hf_repo or "", served)
        by_repo.setdefault(repo, []).append(dep)
    layers: dict = {}
    for m in models:
        for f in m.get("files") or []:
            if f.get("name"):
                layers.setdefault(m["repository"], {})[f["name"]] = (f.get("size") or 0) / 1e9
    out: dict = {}
    for repo in {m["repository"] for m in models}:
        claim = by_repo.get(repo) or []
        hf = {d.hf_repo for d in claim}
        sizes = layers.get(repo, {})
        if not claim:
            out[repo] = model_origin(None, [])
            out[repo]["reason"] = "cached without a deployment record"
        elif len(hf) > 1:
            o = model_origin(None, [])
            out[repo] = {**o, "state": "ambiguous", "reason": "deployments from different repositories share this cache entry"}
        else:
            dep = claim[0]
            files = dep.source_files or []
            # A layer is named by the file's flat basename (the pusher's title); a
            # projector's node store name (#2431) is accepted too.
            if files and not all(node_store_name(f, dep.hf_repo) in sizes or os.path.basename(f) in sizes
                                 for f in files):
                out[repo] = {**model_origin(None, []), "reason": "the cached files are not the deployment's files"}
            else:
                out[repo] = model_origin(dep.hf_repo, files, sizes=sizes)
    return out


def _registry_repos(base: str) -> list:
    """The Zot catalog's repo list (#289) — same ``GET /v2/_catalog`` read
    ``registry_catalog``/``walk_registry_models`` above already use, isolated
    here so both GC routes share ONE call shape. Unreachable/error -> []
    (unknown-safe, matches ``_zot_has_repo``'s convention in inventory.py) —
    never raises into a GC report/trigger."""
    try:
        import httpx

        # in-network (Zot) — #1409
        with httpx.Client(timeout=4, trust_env=False) as c:
            r = c.get(f"{base}/v2/_catalog")
            r.raise_for_status()
            return (r.json() or {}).get("repositories") or []
    except Exception:
        return []


def _repo_tags(c, base: str, repo: str):
    """A repo's tags from ``/v2/<repo>/tags/list``. ``[]`` when the registry
    holds the repo without tags or no longer knows it (404): Zot keeps a repo in
    ``_catalog`` after its last manifest is deleted and lists no tags for it, so
    such a repo is already evicted (#3113). ``None`` for any other answer: the
    registry could not say."""
    tr = c.get(f"{base}/v2/{repo}/tags/list")
    if tr.status_code == 200:
        return list((tr.json() or {}).get("tags") or [])
    if tr.status_code == 404:
        return []
    return None


def _split_empty(base: str, eligible: list) -> tuple[list, list]:
    """``(eligible, empty)``: the eligible repos that still hold tags, and those
    that hold none (nothing to do, #3113: an emptied repo stayed "eligible"
    forever). A repo whose tags cannot be read stays eligible, so the POST
    reports it rather than this hiding it."""
    if not eligible:
        return [], []
    try:
        import httpx

        # in-network (Zot) — #1409
        with httpx.Client(timeout=4, trust_env=False) as c:
            keep, empty = [], []
            for repo in eligible:
                try:
                    tags = _repo_tags(c, base, repo)
                except Exception:
                    tags = None
                (empty if tags == [] else keep).append(repo)
            return keep, empty
    except Exception:
        return list(eligible), []


def _gc_warnings(eligible: list) -> list[str]:
    """#837 item 4: the air-gap caveat on reclaiming registry blobs.

    A stopped deployment keeps its DB row (so `deploy()` can resume it) but is
    not "live", which is exactly what makes its cached repo GC-eligible —
    ``_live_deployment_repos`` documents that resuming it just re-mirrors from
    HuggingFace once. On an OFFLINE box that sentence is false: there is no HF
    to re-mirror from, so reclaiming the only copy strands the deployment with
    no way back. Predates S5 (the same hazard has always existed via a manual
    ``registry_evict``), so it is surfaced rather than blocked — but the
    operator has to be told BEFORE the blobs are gone.

    Only when something is actually eligible: a standing "you are offline"
    banner on an empty report is noise, and noise is how the real warning gets
    clicked through. Same reasoning as ``inventory._evict_warnings``, which
    carries the sibling warning for the per-worker evict path.
    """
    if not eligible or _network_allows_hf():
        return []
    return [
        "This box is in offline network mode: reclaimed blobs cannot be "
        "re-mirrored from HuggingFace. A stopped deployment whose only cached "
        "copy is reclaimed here cannot be resumed until someone re-mirrors it "
        "from a box that still has it."
    ]


def _gc_eligibility_report(base: str) -> dict:
    """Compose the catalog read + the live-deployment query into ONE
    eligibility snapshot — shared by the read-only report and the manual
    trigger so both act on the identical computation."""
    repos = _registry_repos(base)
    with session_scope() as s:
        referenced = _live_deployment_repos(s)
    eligible, empty = _split_empty(base, gc_eligible_repos(repos, referenced))
    return {"repositories_total": len(repos), "referenced_count": len(referenced),
            "eligible": eligible, "eligible_count": len(eligible),
            # Catalog entries with no tags left: already evicted, nothing to do.
            "empty": empty,
            # What GC never touches, whatever references it: the fleet's own
            # images and anything outside the model namespace.
            "protected": [r for r in repos if not gc_may_touch(r)]}


def register_registry_api(app) -> None:
    # #314: the model registry (catalog / cached weights / GC) backs the Deploy
    # workflow → ADMIN tier ("Deploy / configure / undeploy models").
    router = APIRouter(dependencies=[Depends(require_role(Role.ADMIN))])

    @router.get("/api/registry/catalog")
    def registry_catalog():
        base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
        out = {"base": base, "available": False, "repositories": []}
        # #2429: one row per declared runner target with its presence in the
        # hub — all absent while the registry is unreachable, never a 500.
        declared = declared_targets()
        out["runner_targets"] = target_rows(declared, [])
        try:
            import httpx

            # in-network (Zot) — #1409
            with httpx.Client(timeout=4, trust_env=False) as c:
                r = c.get(f"{base}/v2/_catalog")
                r.raise_for_status()
                repos = (r.json() or {}).get("repositories", []) or []
                out["available"] = True
                items = []
                for repo in repos[:200]:
                    tags = []
                    try:
                        tr = c.get(f"{base}/v2/{repo}/tags/list")
                        if tr.status_code == 200:
                            tags = (tr.json() or {}).get("tags") or []
                    except Exception:  # pragma: no cover - per-repo best-effort
                        pass
                    items.append({"repository": repo, "tags": tags})
                out["repositories"] = items
                # The listing above stops at 200 repositories and "runners/"
                # sorts after every "models/" path, so a large cache would read
                # every runner as absent: ask for the runner repos by name.
                seen = {i["repository"] for i in items}
                runner_items = [i for i in items if i["repository"].startswith(f"{RUNNER_REPO_PREFIX}/")]
                for repo in sorted({d["repository"] for d in declared} - seen):
                    try:
                        tr = c.get(f"{base}/v2/{repo}/tags/list")
                        if tr.status_code == 200:
                            runner_items.append({"repository": repo, "tags": (tr.json() or {}).get("tags") or []})
                    except Exception:  # pragma: no cover - per-repo best-effort
                        pass
                out["runner_targets"] = target_rows(declared, runner_items)
        except Exception as exc:  # registry down / not deployed → report, don't 500
            out["error"] = str(exc)[:200]
        return out

    @router.get("/api/registry/models")
    def registry_models():
        """#307 model-cache inventory: every model:tag in Zot with its on-registry
        size (config + layers), plus the store total — ``total_bytes`` is over
        UNIQUE blobs (#1179; Zot dedupes), ``total_bytes_tagged`` the naive
        per-tag sum. Graceful when Zot is down."""
        base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
        try:
            import httpx

            # snappy per-call timeout so the inventory never hangs the tab when
            # the store is busy (e.g. a large mirror streaming in) — degrade to a
            # partial/again rather than a long spinner.
            # in-network (Zot) — #1409
            with httpx.Client(timeout=4, trust_env=False) as c:
                out = walk_registry_models(c, base)
        except Exception as exc:
            return {"base": base, "available": False, "models": [],
                    "total_bytes": 0, "total_bytes_tagged": 0, "error": str(exc)[:200]}
        # #3267: each row's exact origin, from the deployment record that mirrored it.
        # Best-effort: the cache view never fails over it (no block = the console
        # shows "origin unknown").
        try:
            with session_scope() as s:
                deps = [(d, (s.get(Model, d.model_id).name if d.model_id else d.model_name))
                        for d in s.query(Deployment).all()]
                origins = registry_origins(deps, out.get("models") or [])
        except Exception:
            origins = {}
        for m in out.get("models") or []:
            m["origin"] = origins.get(m["repository"])
        return out

    @router.delete("/api/registry/models/{repo:path}")
    def registry_evict(repo: str, tag: str = "latest"):
        """Evict a model:tag from the cache — delete its manifest (Zot GCs the
        now-unreferenced blobs). ``repo`` is the full path (e.g. models/qwen3.6).
        Thin wrapper over ``evict_registry_repo`` — see its docstring (#307 S3
        factored this out so the fleet-wide evict route reuses it)."""
        return evict_registry_repo(repo, tag)

    @router.get("/api/registry/gc")
    def gc_report():
        """#307 S5 — read-only GC-eligibility report: which cached repos have
        NO live deployment referencing them right now. Deletes nothing; POST
        this same path to actually reclaim them (manual only, v1 — never a
        timer)."""
        base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
        out = _gc_eligibility_report(base)
        out["base"] = base
        out["warnings"] = _gc_warnings(out["eligible"])   # #837 item 4
        return out

    @router.post("/api/registry/gc")
    def registry_gc():
        """#307 S5 v1 — MANUAL Zot GC trigger (no timer, no LRU — both
        deferred per the scope doc). Recomputes eligibility fresh (the exact
        rule ``gc_report`` above reports), then evicts every tag of each
        eligible repo through the EXISTING ``registry_evict`` above — one
        delete machinery, not two. Untagging the manifest is the actionable
        trigger available to the manager; Zot's already-enabled ``gc: true``
        (``modules/llm/registry/config.json``) reclaims the now-orphaned
        blobs on its own schedule. Best-effort per repo: one failing evict
        does not abort the rest — it's reported in ``failed``, never raised."""
        base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
        out = _gc_eligibility_report(base)
        removed: list = []
        failed: list = []
        empty: list = list(out.get("empty") or [])
        protected: list = list(out.get("protected") or [])
        warnings = _gc_warnings(out["eligible"])   # #837 item 4
        if not out["eligible"]:
            # Nothing to reclaim — skip opening a registry client entirely.
            return {"base": base, "eligible_count": 0, "removed": removed,
                    "failed": failed, "empty": empty, "protected": protected,
                    "warnings": warnings}
        try:
            import httpx

            # in-network (Zot) — #1409
            with httpx.Client(timeout=10, trust_env=False) as c:
                for repo in out["eligible"]:
                    if not gc_may_touch(repo):
                        # The allow-list again, next to the delete: nothing
                        # outside models/ is evicted, whatever named it.
                        protected.append(repo)
                        continue
                    try:
                        tags = _repo_tags(c, base, repo)
                        if tags is None:
                            failed.append(repo)
                            continue
                        if not tags:
                            # Emptied between the snapshot and now: nothing
                            # to do, not a failure (#3113).
                            empty.append(repo)
                            continue
                        ok = True
                        for tag in tags:
                            try:
                                registry_evict(repo, tag)
                            except HTTPException as exc:
                                # 404: the tag is already gone. Two tags on one
                                # manifest go with the first delete, and the
                                # second tag's lookup then finds nothing (#3113).
                                if exc.status_code != 404:
                                    ok = False
                        (removed if ok else failed).append(repo)
                    except Exception:
                        failed.append(repo)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"registry unreachable: {str(exc)[:150]}")
        return {"base": base, "eligible_count": out["eligible_count"],
                "removed": removed, "failed": failed, "empty": empty,
                "protected": protected, "warnings": warnings}

    app.include_router(router)
