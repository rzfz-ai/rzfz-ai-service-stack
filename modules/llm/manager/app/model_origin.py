# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3267: the exact origin of a model's weights, for the console.

One block, built here and only here, for the Deployments table, the deployment
detail and the Model cache overview: the HF repository, its files (shards and the
mmproj projector apart), the quantisation, the size on disk, and links to
huggingface.co. The console renders it and never derives any of it from a model
name.

Sources, all already in the manager:
- the repository and the file list a deployment was made from
  (``Deployment.hf_repo`` / ``Deployment.source_files``);
- sizes from the workers' disk reports (or the registry's layer sizes for a
  cached model);
- the basename provenance map (``inventory._file_provenance_map``): a file that two
  different repositories claim is **ambiguous**. The block then names no
  repository, because the bytes cannot be attributed to one.

States: ``known``, ``unknown`` (no repository recorded, an external endpoint such
as Ollama, or no files) and ``ambiguous``. Nothing is fetched to build the block;
the links are plain information, which matters on air-gapped boxes.

CUI-12: a repository id is a third-party string. Links are built only from a
repository id of the HF ``owner/name`` shape, with every path segment
URL-encoded. Any other id is still shown as text, with no link.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional
from urllib.parse import quote

from app.api.hf import quant_of
from app.model_manifest import node_store_name

HF_BASE = "https://huggingface.co"
# HF's own shape: owner/name, letters, digits, '.', '_', '-'; no '..' segment.
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def repo_url(repo_id: Optional[str]) -> Optional[str]:
    """``https://huggingface.co/<owner>/<name>`` for a well-formed repo id, else None."""
    if not repo_id or not _REPO_ID_RE.match(repo_id) or ".." in repo_id:
        return None
    return HF_BASE + "/" + "/".join(quote(seg, safe="") for seg in repo_id.split("/"))


def file_url(repo_id: Optional[str], path: str) -> Optional[str]:
    """The file's blob page in the repository (default branch), or None."""
    base = repo_url(repo_id)
    segs = [s for s in (path or "").split("/") if s]
    if base is None or not segs or any(s in (".", "..") for s in segs):
        return None
    return base + "/blob/main/" + "/".join(quote(s, safe="") for s in segs)


def _is_projector(path: str, repo_id: Optional[str]) -> bool:
    return node_store_name(path, repo_id) != os.path.basename(path or "")


def _empty(state: str, reason: str) -> dict:
    return {"state": state, "reason": reason, "repo_id": None, "repo_url": None,
            "file_url": None, "files": [], "shards": 0, "quant": None,
            "size_gb": None, "mmproj": None}


def disk_sizes(disk_reports: Iterable[Optional[dict]]) -> dict:
    """basename -> size_gb from the workers' disk reports (first report wins)."""
    out: dict = {}
    for report in disk_reports or []:
        for f in ((report or {}).get("files") or []):
            name = os.path.basename(f.get("name") or "")
            size = f.get("size_gb")
            if name and isinstance(size, (int, float)) and size > 0:
                out.setdefault(name, float(size))
    return out


def model_origin(repo_id: Optional[str], files: Optional[list], *,
                 sizes: Optional[dict] = None, provenance: Optional[dict] = None,
                 external: bool = False) -> dict:
    """The origin block for one model.

    ``files``: the repository paths (``Deployment.source_files``).
    ``sizes``: basename (or node store name) -> GB, e.g. ``disk_sizes(reports)``.
    ``provenance``: ``inventory._file_provenance_map`` output; a file it marks
    ambiguous makes the whole block ambiguous.
    ``external``: an endpoint the manager does not provision (Ollama, a remote
    OpenAI-compatible server): its weights' origin is not ours to state.
    """
    files = [f for f in (files or []) if isinstance(f, str) and f.strip()]
    if external:
        return _empty("unknown", "external endpoint")
    if not repo_id:
        return _empty("unknown", "no repository recorded")
    if not files:
        return _empty("unknown", "no files recorded")
    for f in files:
        claim = (provenance or {}).get(node_store_name(f, repo_id))
        if claim and claim.get("ambiguous"):
            return _empty("ambiguous", "two repositories claim the same file name")
    sizes = sizes or {}
    entries, total, complete = [], 0.0, True
    for f in files:
        store = node_store_name(f, repo_id)
        size = sizes.get(store)
        if size is None and store != os.path.basename(f):
            size = sizes.get(os.path.basename(f))   # #2431 legacy flat projector name
        if isinstance(size, (int, float)) and size > 0:
            total += float(size)
            size = round(float(size), 2)
        else:
            size, complete = None, False
        entries.append({"name": f, "size_gb": size, "url": file_url(repo_id, f),
                        "mmproj": _is_projector(f, repo_id)})
    weights = [e for e in entries if not e["mmproj"]]
    labels = sorted({quant_of(os.path.basename(e["name"])) for e in weights})
    quant = labels[0] if len(labels) == 1 else ("mixed" if labels else None)
    mmproj = next((os.path.basename(e["name"]) for e in entries if e["mmproj"]), None)
    return {
        "state": "known", "reason": None,
        "repo_id": repo_id, "repo_url": repo_url(repo_id),
        "file_url": weights[0]["url"] if len(weights) == 1 else None,
        "files": entries, "shards": len(weights), "quant": quant,
        "size_gb": round(total, 2) if complete else None,
        "mmproj": mmproj,
    }
