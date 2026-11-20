# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1256 — read the stack's model manifest (``core/llm/standard-models.yaml``).

The manifest is the SINGLE place a model's vision projector (``mmproj``) is
named. It reaches this container as a read-only mount — the same
one-artifact-two-readers pattern #1059 uses for ``install-worker.sh.tmpl``, and
for the same reason: a second, hand-typed copy is exactly what went wrong.

For eight days ``app/catalog.py`` named a DIFFERENT projector precision for
qwen3.6 than the manifest did (F16 here, F32 there). Both of those files really
exist in ``unsloth/Qwen3.6-35B-A3B-GGUF``, so neither was a 404 — but because
the two disagreed, the #1250b standard-set deploy shipped NO projector
at all rather than gamble on a name, and a clean LLM-Manager box came up serving
qwen3.6 **text-only** while the GPUStack-path boxes had vision. The manifest's
value is the one with a box behind it (#138 copied it out of a serving box's HF
cache and byte-compared it), so the manifest wins and nobody else keeps a copy.

**Resolution reads the DECLARATION, never the repo listing.** A repo can carry a
projector the fleet deliberately does not want: ``unsloth/Qwen3.8-27B-GGUF``
carries one and the manifest names none for ``qwen3.8-27b``. "Attach whatever
the repo has" would deploy one nobody asked for.

Offline-safe: this module reads one local YAML file and never touches the
network. A missing or unparsable manifest is survivable — it degrades to "no
projector known", logged once per path, and the CLI standard-set deploy still
carries the sidecar because it reads the manifest off the host filesystem.
"""
from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import PurePosixPath
from typing import Optional

import yaml

log = logging.getLogger("llm-manager.model-manifest")

#: Mount target inside the container (see modules/llm/manager/compose.yml).
DEFAULT_MANIFEST_PATH = "/srv/standard-models.yaml"

#: Parsed-manifest cache keyed by (path, mtime_ns, size) so an operator editing
#: the mounted manifest takes effect without a container restart, while the
#: common case costs one ``stat``.
_CACHE: dict[tuple, dict] = {}
#: Paths already reported as unreadable — logged once, not once per request.
_WARNED: set[str] = set()


def manifest_path() -> str:
    """The manifest to read. ``LLM_MANAGER_MODEL_MANIFEST`` overrides the mount
    target (tests point it at the repo copy)."""
    return os.environ.get("LLM_MANAGER_MODEL_MANIFEST") or DEFAULT_MANIFEST_PATH


def reset_cache() -> None:
    """Drop the parsed-manifest cache (tests relocate the manifest)."""
    _CACHE.clear()
    _WARNED.clear()


def _load_spec() -> dict:
    """The parsed manifest (``models:`` validated as a mapping), or ``{}``."""
    path = manifest_path()
    try:
        st = os.stat(path)
    except OSError as exc:
        if path not in _WARNED:
            _WARNED.add(path)
            log.warning(
                "model manifest %s unreadable (%s) — vision projectors cannot "
                "be resolved, catalog deploys will carry weights only (#1256)",
                path, exc)
        return {}
    key = (path, st.st_mtime_ns, st.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
        models = spec.get("models") or {}
        if not isinstance(models, dict):
            raise ValueError("`models:` is not a mapping")
        spec["models"] = models
    except Exception as exc:      # a malformed manifest must not 500 the API
        if path not in _WARNED:
            _WARNED.add(path)
            log.warning("model manifest %s unparsable (%s) — no projectors "
                        "resolved (#1256)", path, exc)
        return {}
    _CACHE.clear()               # only ever one manifest generation is useful
    _CACHE[key] = spec
    return spec


def load_models() -> dict:
    """The manifest's ``models:`` mapping, or ``{}`` when it cannot be read."""
    return _load_spec().get("models") or {}


# --- model risk flag and excluded families (#2962) ---------------------------

#: The one value ``egress:`` takes today: the model may run only where egress is
#: denied (agents fenced by the allow-list proxy), never with open egress.
EGRESS_DENIED_ONLY = "denied-only"


def egress_for(*, name: Optional[str] = None, repo_id: Optional[str] = None) -> Optional[str]:
    """The ``egress:`` flag declared for a model (alias first, then repo id).

    The repository decides for a model deployed by hand under a name of its
    own. Hugging Face resolves a repository id in any letter case, so a
    hand-typed ``unsloth/qwen3.8-27b-gguf`` is the flagged repository too."""
    models = load_models()
    if name and isinstance(models.get(name), dict):
        v = str(models[name].get("egress") or "").strip()
        if v:
            return v
    repo = str(repo_id or "").strip().lower()
    if repo:
        for model in models.values():
            if (isinstance(model, dict)
                    and str(model.get("huggingface_repo_id") or "").strip().lower() == repo):
                v = str(model.get("egress") or "").strip()
                if v:
                    return v
    return None


def _family_key(text: str) -> str:
    """Case and separators do not matter: ``Qwen3.8-Flash-Next``,
    ``qwen3_8_flash_next`` and ``QWEN3.8 FLASH NEXT`` are one family."""
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def excluded_reason(*, name: Optional[str] = None, repo_id: Optional[str] = None,
                    files=None) -> Optional[str]:
    """Why a model must not be deployed, or None. Matched against the
    manifest's ``excluded_models`` families: a family is excluded when its key
    occurs in the model name, the repository id or any file name. A manifest
    that cannot be read excludes nothing (it also cannot be read for anything
    else); the shipped manifest always carries the list."""
    entries = _load_spec().get("excluded_models") or []
    haystack = [_family_key(x) for x in [name, repo_id, *(files or [])] if x]
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        fam = _family_key(entry.get("family") or "")
        if fam and any(fam in h for h in haystack):
            return f"{entry.get('family')}: {entry.get('reason') or 'excluded from this stack'}"
    return None


def node_store_name(filename: str, repo_id: Optional[str] = None) -> str:
    """#2431: the name a node stores ``filename`` under in its flat models
    volume — what its disk report lists and what ``evict_weights`` must name.
    Weights keep their basename; a projector is keyed by its repository
    (``mmproj--<org>--<repo>--<basename>``), because the same projector
    basename ships in dozens of repositories. Must equal node-agent
    ``hf_pull.store_name`` — pinned by
    tests/unit/consistency/test_2431_one_store_name_rule.py."""
    base = os.path.basename(filename or "")
    if repo_id and "mmproj" in base.lower():
        return "mmproj--" + repo_id.strip("/").replace("/", "--") + "--" + base
    return base


def mmproj_filename_for(model: dict) -> str:
    """The vision projector filename declared for one model entry, or ``''``.

    Same two-step rule as ``core/llm/expected_models.py::mmproj_filename`` (the
    host-side reader used by offline packaging, verify and sync — the two are
    compared by tests/unit/consistency/test_1256_mmproj_single_source.py):
    the declared ``huggingface_mmproj_filename`` wins, else the basename of an
    explicit ``--mmproj=<path>`` backend parameter. The second leg keeps a
    hand-edited or pre-#1256 manifest working; the shipped manifest declares
    the field for every vision model.
    """
    declared = str((model or {}).get("huggingface_mmproj_filename") or "").strip()
    if declared:
        return declared
    for param in (model or {}).get("backend_parameters") or []:
        param = str(param)
        if param.startswith("--mmproj="):
            return PurePosixPath(param.split("=", 1)[1]).name
    return ""


def mmproj_for(*, repo_id: Optional[str] = None,
               name: Optional[str] = None) -> str:
    """The projector declared for a model, matched by manifest alias first, then
    by ``huggingface_repo_id``. ``''`` when none is declared.

    Alias first because it is the exact identity; repo second because the
    projector is a repo-LEVEL companion, so ANY quant of that repo needs the
    same file (and a renamed alias still resolves).
    """
    models = load_models()
    if name:
        hit = mmproj_filename_for(models.get(name) or {})
        if hit:
            return hit
    if repo_id:
        for model in models.values():
            if (model or {}).get("huggingface_repo_id") == repo_id:
                hit = mmproj_filename_for(model or {})
                if hit:
                    return hit
    return ""


def _entry_covers(model: dict, files) -> bool:
    """Does a deploy of ``files`` load THIS entry's weights? The entry's
    ``huggingface_filename`` (a glob for a sharded model) must be among them,
    literally or by pattern — by full path or by basename, the same two
    spellings ``pins_for`` accepts (a node stores weights flat, by basename)."""
    want = str((model or {}).get("huggingface_filename") or "").strip()
    if not want:
        return False
    want_base = os.path.basename(want)
    return any(isinstance(f, str) and (
        f == want or fnmatch.fnmatchcase(f, want)
        or os.path.basename(f) == want_base
        or fnmatch.fnmatchcase(os.path.basename(f), want_base))
        for f in (files or []))


def measured_footprint_gb(repo_id: Optional[str], files) -> Optional[float]:
    """The largest ``measured_footprint_gb`` the manifest records for the entry
    whose weights a deploy of ``files`` from ``repo_id`` loads, or None.

    The values are resident memory measured on a box, per hardware family
    (``{nvidia: 54.4, amd: 51.3}``). Admission uses them as a FLOOR under the
    formula estimate (footprint.py), which undercounted the engines' compute
    buffers. One number per deployment, the largest family's: the admission
    charge is a property of the deployment row, not of the worker it is asked
    about, and the families differ by a few GB at most, so the largest is the
    safe side. Matched like ``ollama_tag_for``: only the entry's own file
    counts, another quant of the same repository inherits nothing."""
    if not repo_id or not files:
        return None
    best = None
    for model in load_models().values():
        if not isinstance(model, dict) or model.get("huggingface_repo_id") != repo_id:
            continue
        measured = model.get("measured_footprint_gb")
        if not isinstance(measured, dict) or not _entry_covers(model, files):
            continue
        for value in measured.values():
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if v > 0 and (best is None or v > best):
                best = v
    return best


def ollama_tag_for(*, repo_id: Optional[str] = None, name: Optional[str] = None,
                   files=None) -> Optional[str]:
    """#2559 finding 18: the catalogue's Ollama reference override
    (``ollama_tag``) for a deploy, or None.

    Looked up like ``mmproj_for`` — by manifest alias first, then by
    ``huggingface_repo_id`` — with one difference that matters: a projector
    is a repo-level companion, a tag names ONE quantisation. So an entry
    answers only when the deploy loads that entry's own file; a second quant
    of the same repository (or a renamed alias pointing at other weights)
    must never inherit it, or the Mac would serve other weights under the
    deployment's name (#929)."""
    models = load_models()
    candidates = []
    if name and isinstance(models.get(name), dict):
        candidates.append(models[name])
    if repo_id:
        candidates += [m for m in models.values()
                       if isinstance(m, dict) and m.get("huggingface_repo_id") == repo_id]
    for model in candidates:
        if repo_id and model.get("huggingface_repo_id") not in (None, repo_id):
            continue
        tag = str(model.get("ollama_tag") or "").strip()
        if tag and _entry_covers(model, files):
            return tag
    return None


def pins_for(repo_id: Optional[str], files) -> Optional[dict]:
    """#2392: the catalogue pins for a deploy of ``files`` from ``repo_id`` —
    ``{"revision", "sha256": {basename: hex}}`` — or None unless EVERY file is
    pinned by ONE catalogue entry of that repository.

    A glob (the sharded ``qwen3-coder-next`` is deployed by its pattern) is
    covered by every pinned shard it matches. Entries of the same repository
    are tried in turn, so a second quant of a catalogued repo is judged by its
    own entry, not the first one listed."""
    if not repo_id or not files:
        return None
    for m in load_models().values():
        m = m or {}
        if m.get("huggingface_repo_id") != repo_id:
            continue
        rev, digests = m.get("huggingface_revision"), m.get("sha256") or {}
        if not rev or not isinstance(digests, dict) or not digests:
            continue
        digests = {str(p): str(d) for p, d in digests.items()}
        by_base = {os.path.basename(p): d for p, d in digests.items()}
        out: dict = {}
        covered = True
        for f in files:
            f = str(f)
            if any(c in f for c in "*?["):
                hits = {os.path.basename(p): d for p, d in digests.items()
                        if fnmatch.fnmatchcase(p, f)
                        or fnmatch.fnmatchcase(os.path.basename(p), os.path.basename(f))}
                if not hits:
                    covered = False
                    break
                out.update(hits)
                continue
            d = digests.get(f) or by_base.get(os.path.basename(f))
            if not d:
                covered = False
                break
            out[os.path.basename(f)] = d
        if covered:
            return {"revision": str(rev), "sha256": out}
    return None
