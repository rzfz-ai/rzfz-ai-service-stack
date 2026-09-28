# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Expected local-GGUF set — the SHARED model source of truth for #184 offline
(WS7a bundling + WS7c verify).

`rzfz package --include-models` (WS7a) and `rzfz verify-models` (WS7c) must ask
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
``cli/verify-models.sh`` and ``cli/package.sh`` shell out to. stdlib + PyYAML.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess

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


def _find_in(container, roots, stack_root, *, prefix):
    """`find` inside a running container; paths relative to ``prefix``.

    Returns None when the container cannot be reached — the fail-open contract
    this module has had since WS7c: "cannot determine" beats a false gap.
    """
    find_cmd = 'find ' + ' '.join(roots) + ' -type f 2>/dev/null'
    try:
        out = subprocess.run(
            ['docker', 'exec', container, 'sh', '-c', find_cmd],
            cwd=stack_root, capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    if out.returncode != 0 and not out.stdout.strip():
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
                  config_path=None, network_mode=None):
    """Presence check, split by whether a gap actually blocks this box (#367).

    Returns ``(expected_rows, missing_rows, deferred_rows, listed_or_None)``,
    each row ``(alias, repo, filename)``:

      * ``missing``  — BLOCKING gaps. Always-on models in any mode, plus the
        ``auto_start: false`` spares when the box is **offline** (nothing can
        fetch them later, and locally-built/sideloaded weights are the only
        source) → non-zero exit.
      * ``deferred`` — absent ``auto_start: false`` spares on an **online** box.
        They download when first started, so flagging them as a failure on an
        otherwise-healthy install is a false alarm. Informational only.

    ``network_mode`` defaults to :func:`read_network_mode` for ``stack_root``.
    ``lister`` is injectable for tests.
    """
    spec = load_spec(config_path or default_config_path(stack_root))
    expected = [(a, r, f) for a, r, f, _ in
                iter_models(spec, preset=preset, profiles=profiles)]
    listed = (lister or _docker_volume_lister)(stack_root)
    if listed is None:
        return expected, [], [], None

    if network_mode is None:
        network_mode = read_network_mode(stack_root)
    # Offline: every absent GGUF blocks — a spare that never downloaded can
    # never start on an air-gapped box.
    spares = set() if network_mode == 'offline' else deferred_aliases(spec)

    missing, deferred = [], []
    for alias, repo, filename in expected:
        if model_present(repo, filename, listed):
            continue
        (deferred if alias in spares else missing).append((alias, repo, filename))
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
        })
    return {
        'schema': 'razzfazz.expected-models/v1',
        'stack_version': _stack_version(stack_root),
        'preset': preset,
        'count': len(models),
        'models': models,
    }


# --------------------------------------------------------------------------- #
# Stand-alone CLI — cli/verify-models.sh + cli/package.sh shell out to this.  #
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
                    'verify-models` (WS7c) and `rzfz package --include-models` '
                    '(WS7a). #184 offline / air-gap.')
    parser.add_argument('--stack-root', default=os.getcwd())
    parser.add_argument('--config', default=None,
                        help='path to the standard-models.yaml to read '
                             '(default: <stack-root>/core/llm/standard-models.yaml). '
                             'package.sh points this at the packaged TARGET-tag YAML.')
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
    parser.add_argument('--delta-from', default=None,
                        help='with --list/--json: restrict to models added or '
                             'changed vs the standard-models.yaml at this path '
                             '(UPGRADE/DELTA package)')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true',
                      help='TAB-separated "alias\\trepo\\tfilename" per model')
    mode.add_argument('--json', action='store_true',
                      help='the expected-models.json manifest')
    mode.add_argument('--verify', action='store_true',
                      help='assert every expected GGUF is present in the '
                           'gpustack-data volume; non-zero exit on any gap')
    mode.add_argument('--list-mmproj', action='store_true',
                      help='TAB-separated "alias\\trepo\\tmmproj_filename" per '
                           'vision model with a sidecar (#138)')
    parser.add_argument('--quiet', action='store_true')
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

    if args.list:
        for alias, repo, filename, _ in iter_models(
                spec, preset=args.preset, profiles=profiles,
                all_profiles=args.all_profiles or bool(args.delta_from)):
            if delta_aliases is not None and alias not in delta_aliases:
                continue
            print(f'{alias}\t{repo}\t{filename}')
        return 0

    if args.list_mmproj:
        for alias, repo, sidecar in iter_mmproj(
                spec, preset=args.preset, profiles=profiles,
                all_profiles=args.all_profiles or bool(args.delta_from)):
            if delta_aliases is not None and alias not in delta_aliases:
                continue
            print(f'{alias}\t{repo}\t{sidecar}')
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
        print('verify-models: could not list the gpustack-data volume (Docker '
              'unavailable or the gpustack container is not running) — skipping '
              'the presence gate.')
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
                mark = 'deferred'
            else:
                mark = 'ok'
            print(f'  [{mark:>8}] {alias}  ({filename})')
    # Keep the canonical "N expected, P present, M missing" shape — `rzfz
    # status` and the Config-UI offline panel parse exactly this line.
    summary = (f'verify-models: {len(expected)} expected, '
               f'{len(present)} present, {len(missing)} missing')
    if deferred:
        summary += f', {len(deferred)} deferred'
    print(summary + f'.  (network mode: {network_mode})')
    if missing:
        print('MISSING GGUFs (offline model-readiness gate FAILED):')
        for alias, repo, filename in missing:
            print(f'  - {alias}  {repo}  {filename}')
        print('Bundle them with `rzfz package --include-models` (dev box) and '
              'reload via `rzfz upgrade --package …`, or sideload via '
              'settings.<domain> → LLM/Models. Offline NEVER pulls from '
              'huggingface.co — a missing GGUF fails clear here (#184).')
        return 1
    if deferred:
        print('DEFERRED (on-demand spares — not a failure on an online box):')
        for alias, _repo, filename in deferred:
            print(f'  - {alias}  ({filename})')
        print('These are declared `auto_start: false`, so post-install registers '
              'them at 0 replicas and GPUStack downloads their weights the first '
              'time one is started. Nothing to do on an online box.')
        print('NOTE: this box is NOT yet offline-model-ready — before taking it '
              'offline, start each spare once, or bundle them with `rzfz package '
              '--include-models` and reload via `rzfz upgrade --package …`.')
        return 0
    print('All expected model GGUFs are present locally. Offline model-ready.')
    return 0


if __name__ == '__main__':
    import sys

    sys.exit(_main(sys.argv[1:]))
