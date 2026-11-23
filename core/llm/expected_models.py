# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Expected local-GGUF set — the SHARED model source of truth for #184 offline
(WS7a bundling + WS7c verify).

an offline package (Enterprise) (WS7a) and `rzfz verify-models` (WS7c) must ask
the SAME question — *"which standard-model GGUFs must exist locally on a box?"* —
and never disagree, or the package bundles a GGUF the verifier flags missing (or
vice-versa). This module is that one answer, the model-side twin of
``expected_images.py``.

It reads ``core/llm/standard-models.yaml`` (the model declarations `sync.py` and
`post-install.sh` already consume) and exposes:

  * :func:`iter_models`   — the (alias, repo, filename, roles) rows for a preset,
    optionally filtered by a module profile (``requires_profile``);
  * :func:`models_map` / :func:`compute_delta` — the UPGRADE/DELTA bundling
    helper (which GGUFs were added / changed / removed between two tags' YAMLs);
  * :func:`verify` + a stand-alone ``--verify`` CLI — assert every expected GGUF
    is present in the gpustack-data volume (the local-models dir OR the HF cache
    dir), non-zero exit on any gap. NEVER falls back to huggingface.co.

Fail-open contract (mirrors ``expected_images.py``): if the volume can't be
listed (no Docker / gpustack down) the verifier reports "cannot determine" and
exits 0 rather than a false hard-block; an actually-provisioned box lists fine.

Import-safe (``from core.llm import expected_models``) AND runnable stand-alone
(``python3 expected_models.py --list|--json|--verify``) — the form
``cli/verify-models.sh`` and `the Enterprise package tooling` shell out to. stdlib + PyYAML.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is present on every box + dev
    yaml = None


# The two roots inside the gpustack-data volume (mounted at /var/lib/gpustack in
# the gpustack container) where a GGUF may live: offline-sideloaded models under
# local-models/ (mirrors scripts/lib.sh::RAZZFAZZ_LOCAL_MODELS_DIR /
# model_source.LOCAL_MODELS_DIR) and HuggingFace-pulled models under the HF cache.
LOCAL_MODELS_SUBDIR = 'local-models'
HF_CACHE_SUBDIR = 'cache/huggingface'

GPUSTACK_VOLUME_ROOT = '/var/lib/gpustack'


# --------------------------------------------------------------------------- #
# YAML → model rows                                                           #
# --------------------------------------------------------------------------- #

def default_config_path(stack_root):
    return os.path.join(stack_root, 'core', 'llm', 'standard-models.yaml')


def load_spec(config_path):
    """Parse a standard-models.yaml. Returns {} on any failure (fail-open)."""
    if yaml is None or not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, encoding='utf-8') as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def iter_models(spec, preset='standard', profiles=None, all_profiles=False):
    """Yield ``(alias, repo, filename, roles)`` for every deployable standard
    model in ``spec`` under ``preset``.

    Filtering mirrors ``post-install.sh::_model_rows_for_preset`` (S19):
      * a model ships under ``preset`` iff ``preset`` is in its ``presets``
        (default ``[standard, developer]``);
      * a model with ``requires_profile: X`` ships only when ``X`` is in the
        active ``profiles`` — UNLESS ``all_profiles`` (packaging bundles every
        standard model, incl. disabled modules', so enabling one later works);
      * a model with no ``huggingface_repo_id``/``huggingface_filename`` is a
        placeholder and skipped.
    """
    profiles = set(profiles or ())
    for alias, m in (spec.get('models') or {}).items():
        presets = m.get('presets') or ['standard', 'developer']
        if preset not in presets:
            continue
        needs = m.get('requires_profile')
        if needs and not all_profiles and needs not in profiles:
            continue
        repo = m.get('huggingface_repo_id', '')
        filename = m.get('huggingface_filename', '')
        if not (repo and filename):
            continue
        yield alias, repo, filename, list(m.get('roles') or [])


def mmproj_filename(m):
    """The vision mmproj sidecar filename for one model declaration, or ''.

    Declared ``huggingface_mmproj_filename`` wins; otherwise the basename of an
    explicit ``--mmproj=<path>`` backend parameter (granite-docling pins the HF
    cache path there). Models without either have no vision sidecar. #138.

    #1256: that declared field is now the SINGLE source for the whole stack —
    the LLM-Manager catalog/console (``modules/llm/manager/app/
    model_manifest.py``) and the standard-set deploy
    (``cli/lib-llm-manager-deploy.sh``) resolve the same field with the same
    two-step rule, and no consumer keeps a copy of the filename. Resolution
    reads the DECLARATION, never the HF repo listing: a repo may carry a
    projector the fleet deliberately does not deploy (qwen3.8-27b's does).
    The shipped manifest declares the field for every vision model, so the
    ``--mmproj=`` leg is a fallback for a hand-edited manifest.
    """
    declared = str(m.get('huggingface_mmproj_filename') or '').strip()
    if declared:
        return declared
    for p in (m.get('backend_parameters') or []):
        p = str(p)
        if p.startswith('--mmproj='):
            return os.path.basename(p.split('=', 1)[1])
    return ''


def iter_mmproj(spec, preset='standard', profiles=None, all_profiles=False):
    """Yield ``(alias, repo, mmproj_filename)`` for every model that
    :func:`iter_models` would ship AND that has a vision sidecar (#138).

    The offline package bundles these under ``models/<repo>/<file>`` —
    repo-scoped, because sidecar names repeat across repos (``mmproj-*.gguf``
    is the upstream convention) — and offline registration rewrites
    ``--mmproj`` to the sideloaded copy (see ``model_source.apply_mmproj``).
    """
    models = spec.get('models') or {}
    for alias, repo, _filename, _roles in iter_models(
            spec, preset=preset, profiles=profiles, all_profiles=all_profiles):
        sidecar = mmproj_filename(models.get(alias) or {})
        if sidecar:
            yield alias, repo, sidecar


def _hardware_catalog():
    """core/llm/hardware_catalog.py, importable both as a package module and
    stand-alone (the Enterprise package tooling and scripts/ run this file by path)."""
    try:
        from core.llm import hardware_catalog as hc  # type: ignore
        return hc
    except Exception:
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hardware_catalog.py')
        s = importlib.util.spec_from_file_location('hardware_catalog', p)
        hc = importlib.util.module_from_spec(s)
        s.loader.exec_module(hc)
        return hc


def classify_models(spec, hardware, preset='standard', profiles=None, all_profiles=True):
    """``[(alias, repo, filename, klass)]`` for ONE hardware class (#2280, #2426).

    The ONE classifier the bundle packager, the cache filler and the box
    verifier all call (decision record §4), so they cannot disagree about a
    class. ``hardware`` is a class or a raw HARDWARE value: it resolves through
    ``hardware_catalog.effective_family`` (unknown/empty -> amd).

    klass ``default``: always-on for this class (`auto_start` restricted to the
    entry's `hardware:` families, hardware_catalog.entry_auto_start) -- a box of
    this class must hold it. ``on-demand``: declared, deployable from the
    console, not always-on here (`auto_start: false`, or a `hardware:` list that
    excludes this class). A model the preset excludes is not listed at all. A
    projector (iter_mmproj) travels with its weight, so it has its alias's class."""
    hc = _hardware_catalog()
    family = hc.effective_family(hardware)
    models = spec.get('models') or {}
    return [(alias, repo, filename,
             'default' if hc.entry_auto_start(models.get(alias) or {}, family) else 'on-demand')
            for alias, repo, filename, _roles in iter_models(
                spec, preset=preset, profiles=profiles, all_profiles=all_profiles)]


def _deferred_label(spec, alias):
    """#2280 box run: an always-on model of ANOTHER class (``hardware:``
    restricts its auto_start, e.g. qwen3-4b-instruct for cpu) is on-demand on
    this box by the decision record's rule (§3/§4), so it is listed — but says
    whose default it is, so it does not read like a missing piece of this
    class's set."""
    entry = (spec.get('models') or {}).get(alias) or {}
    fams = _hardware_catalog().entry_families(entry)
    auto = entry.get('auto_start', True)
    always = not (auto is False or str(auto).strip().lower() in ('false', 'no', '0'))
    if fams and always:
        return f"{alias} (the {'/'.join(fams)} default)"
    return alias


def deferred_aliases(spec):
    """Aliases declared ``auto_start: false`` — the on-demand *spares*.

    ``post-install.sh`` registers these in GPUStack at **0 replicas** (see
    ``deploy_all_models``: the always-on set is deployed first, the spares are
    only registered), and GPUStack downloads a GGUF **when an instance starts**.
    So on a box that has never started one, its weights are legitimately absent.

    That is deliberate: pre-fetching the ~27 GB spare ahead of the default chat
    model used to starve the deploy loop so the actual default was not up yet.
    """
    out = set()
    for alias, m in (spec.get('models') or {}).items():
        auto = m.get('auto_start', True)
        if auto is False or str(auto).strip().lower() in ('false', 'no', '0'):
            out.add(alias)
    return out


def models_map(spec):
    """``{alias: {'repo': str, 'filename': str}}`` for EVERY declared model with
    a repo+filename (unfiltered — the delta base compares full declarations)."""
    out = {}
    for alias, m in (spec.get('models') or {}).items():
        repo = m.get('huggingface_repo_id', '')
        filename = m.get('huggingface_filename', '')
        if repo and filename:
            out[alias] = {'repo': repo, 'filename': filename}
    return out


# --------------------------------------------------------------------------- #
# Delta (WS7a UPGRADE/DELTA package)                                          #
# --------------------------------------------------------------------------- #

def compute_delta(old_map, new_map):
    """Classify every alias across two ``models_map`` results.

    Returns ``{'added', 'changed', 'unchanged', 'removed'}`` — sorted alias
    lists. ``changed`` = present in both but the repo OR filename differs (a new
    GGUF to bundle). ``removed`` = gone from ``new`` (nothing to bundle). An
    UPGRADE/DELTA package bundles ``added ∪ changed``.
    """
    old_keys = set(old_map)
    new_keys = set(new_map)
    added = new_keys - old_keys
    removed = old_keys - new_keys
    changed, unchanged = set(), set()
    for alias in new_keys & old_keys:
        if old_map[alias] != new_map[alias]:
            changed.add(alias)
        else:
            unchanged.add(alias)
    return {
        'added': sorted(added),
        'changed': sorted(changed),
        'unchanged': sorted(unchanged),
        'removed': sorted(removed),
    }


def delta_bundle_aliases(old_map, new_map):
    """The aliases an UPGRADE/DELTA package must bundle = added ∪ changed."""
    d = compute_delta(old_map, new_map)
    return sorted(set(d['added']) | set(d['changed']))


# --------------------------------------------------------------------------- #
# Presence in the gpustack-data volume (WS7c verify)                          #
# --------------------------------------------------------------------------- #

#: Where the LLM Manager's worker keeps weights, and the container that can see
#: it. #1544: the node addresses a weight by its BARE BASENAME in this one flat
#: directory (``hf_pull.ensure_file`` builds ``os.path.join(models_dir,
#: os.path.basename(filename))``), so a listing from here has no repo dirs.
MANAGER_MODELS_ROOT = '/models'
MANAGER_MODELS_CONTAINER = 'llm-worker-agent'


MANAGER_MODELS_VOLUME_DEFAULT = 'razzfazz-stack_llm-node-models'   # modules/llm/node-agent/compose.yml
MANAGER_MODELS_VOLUME_SUFFIX = 'llm-node-models'
GPUSTACK_VOLUME_SUFFIX = 'gpustack-data'
HELPER_IMAGE_DEFAULT = 'alpine:3.24'   # the helper the stack ships (core/compose.yml), #2225


def _env_key(stack_root, key):
    """``key`` from the stack's ``.env`` (None when absent or unreadable)."""
    try:
        with open(os.path.join(stack_root, '.env'), encoding='utf-8') as fh:
            for line in fh:
                if line.startswith(key + '='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _model_volumes(stack_root):
    """``(manager_volumes, gpustack_volumes)``: the model volumes that EXIST on
    this box, project prefix and all (lists, possibly empty). None when
    ``docker volume ls`` itself could not answer: whether a volume exists is
    then UNKNOWN, which is not the same as "there is none" (#2428)."""
    try:
        out = subprocess.run(['docker', 'volume', 'ls', '-q'], cwd=stack_root,
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    names = [n.strip() for n in out.stdout.splitlines() if n.strip()]
    want = _env_key(stack_root, 'LLM_WORKER_MODELS_VOLUME') or MANAGER_MODELS_VOLUME_DEFAULT

    def _named(suffix):
        return [n for n in names if n == suffix or n.endswith('_' + suffix)]

    mgr = [want] if want in names else _named(MANAGER_MODELS_VOLUME_SUFFIX)
    return mgr, _named(GPUSTACK_VOLUME_SUFFIX)


def _find_in_volume(volume, subdirs, stack_root):
    """`find` over a VOLUME mounted read-only: the packager's own method, which
    needs no running container (#2428). Paths relative to the volume root.

    None when the volume could not be read: docker failed, the helper image is
    missing, the run timed out, OR the find itself failed part-way (an
    unreadable directory). A root that does not exist is not a failure (an
    optional sub-directory); a failed find is.
    """
    image = os.environ.get('RZFZ_PACKAGE_HELPER_IMAGE', HELPER_IMAGE_DEFAULT)
    script = _find_script(f'/v/{s}' if s else '/v' for s in subdirs)
    try:
        out = subprocess.run(['docker', 'run', '--rm', '--pull=never', '--network', 'none',
                              '-v', f'{volume}:/v:ro', image, 'sh', '-c', script],
                             cwd=stack_root, capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return {ln.strip()[len('/v/'):] for ln in out.stdout.splitlines() if ln.strip().startswith('/v/')}


def _find_script(roots):
    """sh script listing every file under ``roots``. A root that does not exist
    is skipped (an optional sub-directory); a `find` that FAILS (an unreadable
    directory, a read error) makes the script exit non-zero, so the caller can
    tell partial sight from a complete listing (#2428)."""
    return ''.join(f'if [ -d {r} ]; then find {r} -type f || exit 1; fi; ' for r in roots) + 'exit 0'


def _find_in(container, roots, stack_root, *, prefix):
    """`find` inside a running container; paths relative to ``prefix``.

    Returns None when the container cannot be reached OR the listing is not
    complete: "cannot determine" beats a false gap (the fail-open contract this
    module has had since WS7c), and a partial set is a false gap (#2428).
    """
    try:
        out = subprocess.run(
            ['docker', 'exec', container, 'sh', '-c', _find_script(roots)],
            cwd=stack_root, capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    pre = prefix.rstrip('/') + '/'
    listed = set()
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith(pre):
            listed.add(line[len(pre):])
    return listed


def _docker_volume_lister(stack_root):
    """List every GGUF-bearing file this box holds, as paths RELATIVE to the
    volume root (``local-models/foo.gguf``, ``cache/huggingface/org/repo/foo.gguf``,
    or — on a Manager-shaped box — the bare ``foo.gguf``).

    #1544: this used to ask the `gpustack` container and nothing else. From
    2026.09 the standard set lives in the LLM Manager's worker volume, and a
    Manager-shaped box runs no GPUStack at all — so `rzfz verify-models`, the
    OFFLINE model-readiness gate, hit its fail-open path on every such box and
    reported "cannot determine". A gate that cannot see the models it guards is
    not a gate; on an air-gapped install it is the difference between "this box
    can serve" and finding out at the first request.

    Both are asked, and the results are merged: a box may hold weights in both
    places during the migration (`rzfz adopt-models` COPIES, it does not move),
    and a weight found anywhere is present. Neither container reachable →
    None, i.e. the unchanged fail-open contract.
    """
    manager = _find_in(MANAGER_MODELS_CONTAINER, [MANAGER_MODELS_ROOT],
                       stack_root, prefix=MANAGER_MODELS_ROOT)
    gpustack = _find_in('gpustack',
                        [f'{GPUSTACK_VOLUME_ROOT}/{LOCAL_MODELS_SUBDIR}',
                         f'{GPUSTACK_VOLUME_ROOT}/{HF_CACHE_SUBDIR}'],
                        stack_root, prefix=GPUSTACK_VOLUME_ROOT)
    # #2428: a store whose container is not running is still a store. Read its
    # VOLUME (the packager's method). A volume that exists but cannot be read
    # makes the answer "cannot determine", never a partial listing that calls
    # everything outside it MISSING and sends an air-gapped operator to fetch
    # weights the box already has. Three states: present (listed), absent (no
    # such volume: that store simply does not exist here), unknown (None).
    if manager is None or gpustack is None:
        vols = _model_volumes(stack_root)
        if vols is None:
            return None
        mgr_vols, gp_vols = vols
        if manager is None and mgr_vols:
            parts = [_find_in_volume(v, [''], stack_root) for v in mgr_vols]
            if any(p is None for p in parts):
                return None
            manager = set().union(*parts)
        if gpustack is None and gp_vols:
            parts = [_find_in_volume(v, [LOCAL_MODELS_SUBDIR, HF_CACHE_SUBDIR], stack_root)
                     for v in gp_vols]
            if any(p is None for p in parts):
                return None
            gpustack = set().union(*parts)
    if manager is None and gpustack is None:
        return None
    return (manager or set()) | (gpustack or set())


def model_present(repo, filename, listed):
    """True if any path in ``listed`` (relative to the volume root) matches this
    model's GGUF — under local-models/ (offline-sideloaded) OR the HF cache
    (pulled). ``filename`` may be a glob or carry a sub-dir; matched with
    ``fnmatch`` so ``*f16*.gguf`` and multi-part shards resolve."""
    # #640 review (agent-seqis, empirically on 0.208/ga.14): GPUStack caches
    # in the OFFICIAL huggingface_hub layout -- models--{org}--{repo}/
    # snapshots/<rev>/<file> -- not the flat org/repo/file form this matcher
    # assumed. Both stay matched: flat (historical/other downloaders) AND hub
    # (the layout hf_hub actually writes, which the prefetch now uses too).
    hub_repo = 'models--' + repo.replace('/', '--')
    patterns = (
        f'{LOCAL_MODELS_SUBDIR}/{filename}',
        f'{HF_CACHE_SUBDIR}/{repo}/{filename}',
        f'{HF_CACHE_SUBDIR}/{hub_repo}/snapshots/*/{filename}',
        # #1544: the Manager's worker volume is FLAT — the node addresses a
        # weight by `os.path.basename(filename)` and by nothing else. A model
        # whose manifest filename carries a sub-dir (multi-part shards) is
        # therefore matched on its basename here, and only here.
        os.path.basename(filename),
    )
    for path in listed:
        for pat in patterns:
            if fnmatch.fnmatch(path, pat):
                return True
    return False


def read_network_mode(stack_root):
    """``'offline'`` or ``'online'`` for this box, from ``<stack_root>/.env``.

    Reads ``RAZZFAZZ_NETWORK_MODE`` (``offline`` wins) and the legacy
    ``RAZZFAZZ_OFFLINE=1`` flag. Greps the two keys — never sources ``.env``
    (values contain spaces/metacharacters). Defaults to ``'online'`` when
    there's no ``.env`` or neither key is set: we never invent an offline
    hard-gate for a box that hasn't asked for one.
    """
    path = os.path.join(stack_root, '.env')
    mode, offline_flag = '', ''
    if os.path.isfile(path):
        try:
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    if line.startswith('RAZZFAZZ_NETWORK_MODE='):
                        mode = line.split('=', 1)[1].strip().strip('"').strip("'")
                    elif line.startswith('RAZZFAZZ_OFFLINE='):
                        offline_flag = line.split('=', 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    if mode.lower() == 'offline' or offline_flag.strip() in ('1', 'true', 'yes'):
        return 'offline'
    return 'online'


def verify_detail(stack_root, preset='standard', profiles=None, lister=None,
                  config_path=None, network_mode=None, hardware=None):
    """Presence check, split by whether a gap actually blocks this box (#367, #2426).

    Returns ``(expected_rows, missing_rows, deferred_rows, listed_or_None)``,
    each row ``(alias, repo, filename)``. The box's hardware CLASS decides, via
    :func:`classify_models` (the one classifier the packager and the cache
    filler also use) -- not the network mode:

      * ``missing``  -- BLOCKING gaps: an absent ``default`` model for this class
        (always-on here), in either mode -> non-zero exit.
      * ``deferred`` -- an absent ``on-demand`` model for this class
        (``auto_start: false``, or a ``hardware:`` list that excludes the
        class). Online it downloads when first started; offline it is
        UNAVAILABLE (nothing can fetch it) and is named as such. Never a blocker:
        a box whose default set is complete is not a refused install.

    An unreadable volume listing (``listed is None``) is "unknown": no row is
    judged, so it can never become absent or unavailable (G13).

    ``network_mode`` defaults to :func:`read_network_mode` for ``stack_root``;
    ``hardware`` to ``HARDWARE`` from its ``.env`` (unknown/empty -> amd, as
    everywhere). ``lister`` is injectable for tests.
    """
    spec = load_spec(config_path or default_config_path(stack_root))
    expected = [(a, r, f) for a, r, f, _ in
                iter_models(spec, preset=preset, profiles=profiles)]
    listed = (lister or _docker_volume_lister)(stack_root)
    if listed is None:
        return expected, [], [], None

    if network_mode is None:
        network_mode = read_network_mode(stack_root)
    if hardware is None:
        hardware = _env_key(stack_root, 'HARDWARE') or ''
    klass = {a: k for a, _r, _f, k in classify_models(
        spec, hardware, preset=preset, profiles=profiles, all_profiles=False)}
    missing, deferred = [], []
    for alias, repo, filename in expected:
        if model_present(repo, filename, listed):
            continue
        (deferred if klass.get(alias) == 'on-demand' else missing).append((alias, repo, filename))
    return expected, missing, deferred, listed


def verify(stack_root, preset='standard', profiles=None, lister=None,
           config_path=None, network_mode=None):
    """Back-compatible 3-tuple wrapper over :func:`verify_detail`.

    Returns ``(expected_rows, missing_rows, listed_or_None)`` where ``missing``
    is the BLOCKING set only (deferred on-demand spares are excluded on an
    online box). Existing callers keep their shape and their fail-open contract.
    """
    expected, missing, _deferred, listed = verify_detail(
        stack_root, preset=preset, profiles=profiles, lister=lister,
        config_path=config_path, network_mode=network_mode)
    return expected, missing, listed


# --------------------------------------------------------------------------- #
# Manifest                                                                    #
# --------------------------------------------------------------------------- #

def _stack_version(stack_root):
    try:
        with open(os.path.join(stack_root, 'VERSION'), encoding='utf-8') as fh:
            return fh.read().strip()
    except Exception:
        return ''


def pins_for(spec, alias):
    """#2392: ``{"revision", "sha256": {repo path: hex}}`` for one catalogue
    entry, or None when it is not pinned. The manager's
    ``model_manifest.pins_for`` answers the same question by repository."""
    m = (spec.get('models') or {}).get(alias) or {}
    rev, digests = m.get('huggingface_revision'), m.get('sha256') or {}
    if not rev or not isinstance(digests, dict) or not digests:
        return None
    return {'revision': str(rev), 'sha256': {str(p): str(d) for p, d in digests.items()}}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def digest_mismatches(spec, rows, models_dir):
    """#2392 ask 4: every PRESENT pinned file under ``models_dir`` (flat, by
    basename — the package and the manager volume layout) whose bytes are not
    the approved ones, as ``(alias, local path, got, want)``. An absent file is
    the presence gate's business."""
    out = []
    for alias, _repo, _filename in rows:
        for path, want in ((pins_for(spec, alias) or {}).get('sha256') or {}).items():
            local = os.path.join(models_dir, os.path.basename(path))
            if os.path.isfile(local):
                got = _sha256_file(local)
                if got != want:
                    out.append((alias, local, got, want))
    return out


def build_manifest(stack_root, preset='standard', profiles=None,
                   all_profiles=True, sizes=None, config_path=None):
    """The ``expected-models.json`` payload: flat list of model entries.

    ``sizes`` (optional) maps ``filename -> bytes`` (the package fills this in
    after copying the GGUFs); absent → size 0.
    """
    spec = load_spec(config_path or default_config_path(stack_root))
    sizes = sizes or {}
    models = []
    for alias, repo, filename, roles in iter_models(
            spec, preset=preset, profiles=profiles, all_profiles=all_profiles):
        models.append({
            'name': alias,
            'repo': repo,
            'filename': filename,
            'roles': roles,
            'size': int(sizes.get(filename, 0)),
            # #2392: the catalogue's per-file sha256 pins ({} = unpinned);
            # adopt_models refuses a file whose bytes differ.
            'sha256': (pins_for(spec, alias) or {}).get('sha256', {}),
        })
    return {
        'schema': 'razzfazz.expected-models/v1',
        'stack_version': _stack_version(stack_root),
        'preset': preset,
        'count': len(models),
        'models': models,
    }


# --------------------------------------------------------------------------- #
# Stand-alone CLI — cli/verify-models.sh + the Enterprise package tooling shell out to this.  #
# --------------------------------------------------------------------------- #

def _read_compose_profiles(stack_root):
    """COMPOSE_PROFILES from <stack_root>/.env (no source; grep the one key)."""
    path = os.path.join(stack_root, '.env')
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('COMPOSE_PROFILES='):
                    raw = line.split('=', 1)[1].strip().strip('"').strip("'")
                    return [p for p in raw.split(',') if p]
    except OSError:
        pass
    return []


def _main(argv):
    import argparse

    parser = argparse.ArgumentParser(
        prog='expected_models',
        description='Shared expected-local-GGUF enumeration for `rzfz '
                    'verify-models` (WS7c) and the offline model bundle '
                    '(WS7a). #184 offline / air-gap.')
    parser.add_argument('--stack-root', default=os.getcwd())
    parser.add_argument('--config', default=None,
                        help='path to the standard-models.yaml to read '
                             '(default: <stack-root>/core/llm/standard-models.yaml). '
                             'offline packages point this at the packaged TARGET-tag YAML.')
    parser.add_argument('--preset', default='standard')
    parser.add_argument('--profiles', default=None,
                        help='comma-separated active profiles (default: read '
                             'COMPOSE_PROFILES from <stack-root>/.env)')
    parser.add_argument('--all-profiles', action='store_true',
                        help='ignore requires_profile (bundle every standard '
                             'model — the packaging default)')
    parser.add_argument('--network-mode', default=None,
                        choices=('online', 'offline'),
                        help='with --verify: override the box network mode '
                             '(default: RAZZFAZZ_NETWORK_MODE / '
                             'RAZZFAZZ_OFFLINE from <stack-root>/.env). Offline '
                             'makes an absent on-demand spare a hard failure.')
    parser.add_argument('--hardware-class', default=None, metavar='CLASS',
                        help='with --list/--list-mmproj: append the class column '
                             '(default | on-demand) for this hardware class -- '
                             'cpu, amd or nvidia, or a raw HARDWARE value (resolved '
                             'by hardware_catalog.effective_family). apple has no '
                             'bundle (#2280)')
    parser.add_argument('--delta-from', default=None,
                        help=argparse.SUPPRESS)  # internal packager flag (#2406)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true',
                      help='TAB-separated "alias\\trepo\\tfilename" per model')
    mode.add_argument('--json', action='store_true',
                      help='the expected-models.json manifest')
    mode.add_argument('--verify', action='store_true',
                      help='assert every expected GGUF is present in the '
                           'manager worker volume or the legacy gpustack-data volume; '
                           'non-zero exit on any gap')
    mode.add_argument('--list-mmproj', action='store_true',
                      help='TAB-separated "alias\\trepo\\tmmproj_filename" per '
                           'vision model with a sidecar (#138)')
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--digests', action='store_true',
                        help='with --verify: hash present files against the '
                             'catalogue sha256 pins (#2392). Digests ONLY — an '
                             'absent file passes; run --verify without it for '
                             'presence')
    parser.add_argument('--models-dir', default=None,
                        help='with --digests: the directory holding the weights')
    args = parser.parse_args(argv)
    root = args.stack_root
    profiles = (args.profiles.split(',') if args.profiles is not None
                else _read_compose_profiles(root))

    config_path = args.config or default_config_path(root)
    spec = load_spec(config_path)

    # DELTA filter (list/json): keep only added+changed aliases vs --delta-from.
    delta_aliases = None
    if args.delta_from:
        delta_aliases = set(delta_bundle_aliases(
            models_map(load_spec(args.delta_from)), models_map(spec)))

    klass = None
    if args.hardware_class is not None:
        if not (args.list or args.list_mmproj):
            parser.error('--hardware-class applies to --list and --list-mmproj')
        family = _hardware_catalog().effective_family(args.hardware_class)
        if family == 'apple':
            print('expected_models: hardware class apple has no bundle (the Mac is a '
                  'native thin worker, not a stack box) (#2280)', file=sys.stderr)
            return 2
        klass = {a: k for a, _r, _f, k in classify_models(
            spec, family, preset=args.preset, profiles=profiles,
            all_profiles=args.all_profiles or bool(args.delta_from))}

    def _suffix(alias):
        return '' if klass is None else '\t' + klass[alias]

    if args.list:
        for alias, repo, filename, _ in iter_models(
                spec, preset=args.preset, profiles=profiles,
                all_profiles=args.all_profiles or bool(args.delta_from)):
            if delta_aliases is not None and alias not in delta_aliases:
                continue
            print(f'{alias}\t{repo}\t{filename}{_suffix(alias)}')
        return 0

    if args.list_mmproj:
        for alias, repo, sidecar in iter_mmproj(
                spec, preset=args.preset, profiles=profiles,
                all_profiles=args.all_profiles or bool(args.delta_from)):
            if delta_aliases is not None and alias not in delta_aliases:
                continue
            print(f'{alias}\t{repo}\t{sidecar}{_suffix(alias)}')
        return 0

    if args.json:
        man = build_manifest(root, preset=args.preset, profiles=profiles,
                             all_profiles=args.all_profiles or bool(args.delta_from),
                             config_path=config_path)
        if delta_aliases is not None:
            man['models'] = [m for m in man['models'] if m['name'] in delta_aliases]
            man['count'] = len(man['models'])
        print(json.dumps(man, indent=2))
        return 0

    if args.verify and args.digests:
        if not args.models_dir:
            parser.error('--digests needs --models-dir (the directory the weights are in)')
        rows = [(a, r, f) for a, r, f, _ in iter_models(
            spec, preset=args.preset, profiles=profiles, all_profiles=args.all_profiles)]
        bad = digest_mismatches(spec, rows, args.models_dir)
        for alias, local, got, want in bad:
            print(f'  sha256 mismatch: {os.path.basename(local)} ({alias}) — '
                  f'{got[:12]}… on disk, the catalogue approved {want[:12]}…')
        print(f'verify-models: {len(bad)} sha256 mismatch(es) under {args.models_dir}')
        return 1 if bad else 0

    # --verify
    network_mode = args.network_mode or read_network_mode(root)
    expected, missing, deferred, listed = verify_detail(
        root, preset=args.preset, profiles=profiles,
        config_path=config_path, network_mode=network_mode)
    if not expected:
        print('verify-models: no standard-model GGUFs expected for the active '
              'profiles — nothing to check.')
        return 0
    if listed is None:
        print('verify-models: could not list the model volumes (the manager worker '
              'volume and the legacy gpustack-data volume; Docker unavailable or '
              'no container to ask) — skipping the presence gate. The check did '
              'NOT run.')
        return 0
    missing_set, deferred_set = set(missing), set(deferred)
    present = [row for row in expected
               if row not in missing_set and row not in deferred_set]
    if not args.quiet:
        for row in expected:
            alias, _repo, filename = row
            if row in missing_set:
                mark = 'MISSING'
            elif row in deferred_set:
                mark = 'unavail' if network_mode == 'offline' else 'deferred'
            else:
                mark = 'ok'
            print(f'  [{mark:>8}] {alias}  ({filename})')
    # Keep the canonical "N expected, P present, M missing" shape — `rzfz
    # status` and the Config-UI offline panel parse exactly this line.
    summary = (f'verify-models: {len(expected)} expected, '
               f'{len(present)} present, {len(missing)} missing')
    if deferred:
        summary += (f', {len(deferred)} unavailable offline' if network_mode == 'offline'
                    else f', {len(deferred)} deferred')
    print(summary + f'.  (network mode: {network_mode})')
    if missing:
        print('MISSING GGUFs (offline model-readiness gate FAILED):')
        for alias, repo, filename in missing:
            print(f'  - {alias}  {repo}  {filename}')
        print('Obtain an offline package with the models from your razzfazz.ai contact (Enterprise) and '
              'reload via `rzfz upgrade --package …`, or sideload via '
              'settings.<domain> → LLM/Models. Offline NEVER pulls from '
              'huggingface.co — a missing GGUF fails clear here (#184).')
        return 1
    if deferred:
        if network_mode == 'offline':
            names = ', '.join(_deferred_label(spec, a) for a, _r, _f in deferred)
            print(f'UNAVAILABLE OFFLINE: {names}')
            print("On-demand for this box's hardware class and not on the box: they cannot be "
                  'deployed until their weights arrive in a model bundle or package (#2426). '
                  'The default set is complete.')
            return 0
        print('DEFERRED (on-demand spares — not a failure on an online box):')
        for alias, _repo, filename in deferred:
            print(f'  - {alias}  ({filename})')
        print('These are declared `auto_start: false`, so post-install registers '
              'them at 0 replicas and GPUStack downloads their weights the first '
              'time one is started. Nothing to do on an online box.')
        print('NOTE: this box is NOT yet offline-model-ready — before taking it '
              'offline, start each spare once, or obtain an offline package with the models from your razzfazz.ai contact (Enterprise) '
              'and reload via `rzfz upgrade --package …`.')
        return 0
    print('All expected model GGUFs are present locally. Offline model-ready.')
    return 0


if __name__ == '__main__':
    import sys

    sys.exit(_main(sys.argv[1:]))
