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
import re
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


# --- model risk flag, risk families and excluded families (#2962, #3394) -----

#: The one value ``egress:`` takes today: the model may run only where egress is
#: denied (agents fenced by the allow-list proxy), never with open egress.
EGRESS_DENIED_ONLY = "denied-only"


def _family_pattern(family: str):
    """A family name as a pattern over model names, repository ids and file
    names. Case and separators do not matter: ``Qwen3.8-Flash-Next``,
    ``qwen3_8_flash_next`` and ``QWEN3.8 FLASH NEXT`` are one family.

    The family's letter and digit runs must appear in order with nothing but
    separators between them. After the match comes no letter or digit: a
    plain substring over the separator-stripped text cannot tell ``Qwen3.8``
    from ``Qwen3-8B`` (both read ``qwen38…``); the trailing boundary can —
    after ``qwen3.8`` comes a separator or the end, after ``qwen3-8`` comes
    the ``b`` of ``8b``. Before the match only a digit is refused, so a name
    glued to a prefix (``DeepQwen3.8-27B``, ``HuihuiQwen3.8-27B``) still
    belongs to the family. None for a family with no letter or digit.

    #3400: a name glued to a SUFFIX (``qwen3.8max``, ``qwen3.8it``,
    ``qwen3.8p8``) is matched through ``_FAMILY_SUFFIX`` — an allow-list, not
    a dropped boundary. ``b`` is deliberately not on it: ``qwen3-8b`` reads
    ``qwen38b`` like a glued ``qwen3.8b`` would, and only the ``b`` tells the
    Qwen3 8B model apart. Known false positive, accepted: ``Qwen3-8-bit``."""
    tokens = re.findall(r"[a-z]+|[0-9]+", str(family or "").lower())
    if not tokens:
        return None
    return re.compile(r"(?<![0-9])" + r"[^a-z0-9]*".join(map(re.escape, tokens))
                      + _FAMILY_SUFFIX + r"(?![a-z0-9])")


#: #3400: what may be glued straight onto a family name — the variant words
#: model names carry (``max``, ``instruct``, ``it``, …) and a ``p<digits>``
#: point-version. Never ``b``/``b<digits>`` (see ``_family_pattern``). Kept
#: identical to the agent manager's copy (tests/unit/consistency/
#: test_3394_one_risk_family_rule.py).
_FAMILY_SUFFIX = (r"(?:max|plus|pro|turbo|flash|next|mini|coder|instruct|it|chat"
                  r"|thinking|omni|vl|p[0-9]+)?")


def _family_entry(key: str, *, name=None, repo_id=None, files=None) -> Optional[dict]:
    """The first entry of the manifest list ``key`` (``excluded_models`` or
    ``risk_families``) whose family occurs in the model name, the repository id
    or any file name; None when none does or the manifest cannot be read."""
    entries = _load_spec().get(key) or []
    haystack = [str(x).lower() for x in [name, repo_id, *(files or [])] if x]
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        pat = _family_pattern(entry.get("family") or "")
        if pat is not None and any(pat.search(h) for h in haystack):
            return entry
    return None


def risk_family(*, name: Optional[str] = None, repo_id: Optional[str] = None,
                files=None) -> Optional[dict]:
    """#3394: the ``risk_families`` entry a model belongs to — ``{family,
    egress, reason}`` — matched against its name, repository id and file
    names; None when it belongs to none. A flagged family is not forbidden:
    the operator is warned before the deploy and acknowledges it."""
    return _family_entry("risk_families", name=name, repo_id=repo_id, files=files)


def egress_for(*, name: Optional[str] = None, repo_id: Optional[str] = None,
               files=None) -> Optional[str]:
    """The ``egress:`` flag of a model: its catalogue entry first (alias, then
    repository id), then the manifest's ``risk_families``.

    The repository decides for a model deployed by hand under a name of its
    own. Hugging Face resolves a repository id in any letter case, so a
    hand-typed ``unsloth/qwen3.8-27b-gguf`` is the flagged repository too. A
    model the catalogue does not carry is still flagged when its name,
    repository or a file name belongs to a risk family (#3394)."""
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
    fam = risk_family(name=name, repo_id=repo_id, files=files)
    if fam:
        v = str(fam.get("egress") or "").strip()
        if v:
            return v
    return None


#: #3403: the runner channels a catalogue entry may ask for (`runner:`). Absent
#: = stable, the node's default runner.
RUNNER_CHANNELS = ("stable", "experimental")


def runner_channel_for(*, name: Optional[str] = None,
                       repo_id: Optional[str] = None) -> Optional[str]:
    """#3403: the ``runner:`` channel the catalogue declares for a model —
    ``"experimental"`` for an architecture only the experimental llama.cpp
    build serves — or None (stable).

    The architecture is a property of the REPOSITORY, so a known repository
    decides alone (in any letter case): every quantisation of it needs the same
    runner, also one deployed by hand under a name of its own — and a catalogue
    ALIAS reused for another repository does NOT carry the channel over (#3403
    review: the name `inkling-2-mini` re-deployed with a Qwen repository is a
    Qwen model). Only without a repository is the alias asked. A copy of the
    model under another repository is not recognised (stable); the deploy
    editor says so. An unknown value in the manifest is ignored (None), never
    guessed into a channel; the manifest's consistency guard refuses it."""
    models = load_models()

    def _channel(model) -> Optional[str]:
        v = str((model or {}).get("runner") or "").strip().lower()
        return v if v in RUNNER_CHANNELS and v != "stable" else None

    repo = str(repo_id or "").strip().lower()
    if not repo:
        if name and isinstance(models.get(name), dict):
            return _channel(models[name])
        return None
    if repo:
        for model in models.values():
            if (isinstance(model, dict)
                    and str(model.get("huggingface_repo_id") or "").strip().lower() == repo):
                ch = _channel(model)
                if ch:
                    return ch
    return None


def runner_architecture_for(*, name: Optional[str] = None,
                            repo_id: Optional[str] = None) -> Optional[str]:
    """#3468: the GGUF architecture the catalogue records for a model that
    needs the experimental runner (``architecture:``), matched like
    ``runner_channel_for``; None when it records none."""
    models = load_models()
    repo = str(repo_id or "").strip().lower()
    for key, model in models.items():
        if not isinstance(model, dict):
            continue
        hit = (str(model.get("huggingface_repo_id") or "").strip().lower() == repo if repo
               else key == name)
        if hit and str(model.get("architecture") or "").strip():
            return str(model["architecture"]).strip()
    return None


def egress_risk_reason(*, name: Optional[str] = None, repo_id: Optional[str] = None,
                       files=None) -> Optional[str]:
    """#3394: what was found about a flagged model, for the notice an operator
    reads before deploying it — the ``reason`` of its risk family, else the
    catalogue's general sentence for a flagged entry; None when the model
    carries no flag."""
    if not egress_for(name=name, repo_id=repo_id, files=files):
        return None
    fam = risk_family(name=name, repo_id=repo_id, files=files)
    if fam and str(fam.get("reason") or "").strip():
        return str(fam.get("reason")).strip()
    return "the model catalogue flags this model for agent egress"


def excluded_reason(*, name: Optional[str] = None, repo_id: Optional[str] = None,
                    files=None) -> Optional[str]:
    """Why a model must not be deployed, or None. Matched against the
    manifest's ``excluded_models`` families like ``risk_families``: in the
    model name, the repository id or any file name. A manifest that cannot be
    read excludes nothing (it also cannot be read for anything else). The
    shipped list is empty since #3394 — a risky family is flagged and warned
    about before the deploy instead — but the mechanism stays for a family
    that must never be offered."""
    entry = _family_entry("excluded_models", name=name, repo_id=repo_id, files=files)
    if entry is None:
        return None
    return f"{entry.get('family')}: {entry.get('reason') or 'excluded from this stack'}"


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
