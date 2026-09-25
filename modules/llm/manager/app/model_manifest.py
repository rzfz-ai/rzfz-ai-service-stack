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


def load_models() -> dict:
    """The manifest's ``models:`` mapping, or ``{}`` when it cannot be read."""
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
    except Exception as exc:      # a malformed manifest must not 500 the API
        if path not in _WARNED:
            _WARNED.add(path)
            log.warning("model manifest %s unparsable (%s) — no projectors "
                        "resolved (#1256)", path, exc)
        return {}
    _CACHE.clear()               # only ever one manifest generation is useful
    _CACHE[key] = models
    return models


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
