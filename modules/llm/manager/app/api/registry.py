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
from app.db import session_scope
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
            dr = c.delete(f"{base}/v2/{repo}/manifests/{ref}")
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

def gc_eligible_repos(repos: list, referenced) -> list:
    """Pure set-difference: which Zot repos (``/v2/_catalog``, #289) have NO
    live deployment referencing them right now.

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
    return [r for r in repos if r not in ref]


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
    eligible = gc_eligible_repos(repos, referenced)
    return {"repositories_total": len(repos), "referenced_count": len(referenced),
            "eligible": eligible, "eligible_count": len(eligible)}


def register_registry_api(app) -> None:
    # #314: the model registry (catalog / cached weights / GC) backs the Deploy
    # workflow → ADMIN tier ("Deploy / configure / undeploy models").
    router = APIRouter(dependencies=[Depends(require_role(Role.ADMIN))])

    @router.get("/api/registry/catalog")
    def registry_catalog():
        base = os.environ.get("LLM_REGISTRY_URL", "http://llm-registry:5000").rstrip("/")
        out = {"base": base, "available": False, "repositories": []}
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
                return walk_registry_models(c, base)
        except Exception as exc:
            return {"base": base, "available": False, "models": [],
                    "total_bytes": 0, "total_bytes_tagged": 0, "error": str(exc)[:200]}

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
        warnings = _gc_warnings(out["eligible"])   # #837 item 4
        if not out["eligible"]:
            # Nothing to reclaim — skip opening a registry client entirely.
            return {"base": base, "eligible_count": 0, "removed": removed,
                    "failed": failed, "warnings": warnings}
        try:
            import httpx

            # in-network (Zot) — #1409
            with httpx.Client(timeout=10, trust_env=False) as c:
                for repo in out["eligible"]:
                    try:
                        tr = c.get(f"{base}/v2/{repo}/tags/list")
                        tags = ((tr.json() or {}).get("tags") or []) if tr.status_code == 200 else []
                        if not tags:
                            failed.append(repo)
                            continue
                        ok = True
                        for tag in tags:
                            try:
                                registry_evict(repo, tag)
                            except HTTPException:
                                ok = False
                        (removed if ok else failed).append(repo)
                    except Exception:
                        failed.append(repo)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"registry unreachable: {str(exc)[:150]}")
        return {"base": base, "eligible_count": out["eligible_count"],
                "removed": removed, "failed": failed, "warnings": warnings}

    app.include_router(router)
