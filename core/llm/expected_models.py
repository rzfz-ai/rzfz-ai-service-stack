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

def _docker_volume_lister(stack_root):
    """List every GGUF-bearing file under the two volume roots, as paths
    RELATIVE to the volume root (e.g. ``local-models/foo.gguf``,
    ``cache/huggingface/org/repo/foo.gguf``).

    Runs INSIDE the running gpustack container (the volume is a docker named
    volume). Returns ``None`` when the container can't be reached (fail-open →
    the verifier reports "cannot determine" rather than a false gap).
    """
    find_cmd = (
        f'find {GPUSTACK_VOLUME_ROOT}/{LOCAL_MODELS_SUBDIR} '
        f'{GPUSTACK_VOLUME_ROOT}/{HF_CACHE_SUBDIR} -type f 2>/dev/null'
    )
    try:
        out = subprocess.run(
            ['docker', 'exec', 'gpustack', 'sh', '-c', find_cmd],
            cwd=stack_root, capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    if out.returncode != 0 and not out.stdout.strip():
        return None
    prefix = GPUSTACK_VOLUME_ROOT.rstrip('/') + '/'
    listed = set()
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            listed.add(line[len(prefix):])
    return listed


def model_present(repo, filename, listed):
    """True if any path in ``listed`` (relative to the volume root) matches this
    model's GGUF — under local-models/ (offline-sideloaded) OR the HF cache
    (pulled). ``filename`` may be a glob or carry a sub-dir; matched with
    ``fnmatch`` so ``*f16*.gguf`` and multi-part shards resolve."""
    patterns = (
        f'{LOCAL_MODELS_SUBDIR}/{filename}',
        f'{HF_CACHE_SUBDIR}/{repo}/{filename}',
    )
    for path in listed:
        for pat in patterns:
            if fnmatch.fnmatch(path, pat):
                return True
    return False


def verify(stack_root, preset='standard', profiles=None, lister=None,
           config_path=None):
    """Assert every expected GGUF is present in the volume.

    Returns ``(expected_rows, missing_rows, listed_or_None)`` where each row is
    ``(alias, repo, filename)``. ``listed_or_None`` is ``None`` when the volume
    couldn't be listed (caller fail-opens). ``lister`` is injectable for tests.
    """
    spec = load_spec(config_path or default_config_path(stack_root))
    expected = [(a, r, f) for a, r, f, _ in
                iter_models(spec, preset=preset, profiles=profiles)]
    listed = (lister or _docker_volume_lister)(stack_root)
    if listed is None:
        return expected, [], None
    missing = [(a, r, f) for a, r, f in expected if not model_present(r, f, listed)]
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
    expected, missing, listed = verify(root, preset=args.preset,
                                       profiles=profiles, config_path=config_path)
    if not expected:
        print('verify-models: no standard-model GGUFs expected for the active '
              'profiles — nothing to check.')
        return 0
    if listed is None:
        print('verify-models: could not list the gpustack-data volume (Docker '
              'unavailable or the gpustack container is not running) — skipping '
              'the presence gate.')
        return 0
    present = [(a, r, f) for a, r, f in expected if (a, r, f) not in set(missing)]
    if not args.quiet:
        for alias, repo, filename in expected:
            mark = 'MISSING' if (alias, repo, filename) in set(missing) else 'ok'
            print(f'  [{mark:>7}] {alias}  ({filename})')
    print(f'verify-models: {len(expected)} expected, '
          f'{len(present)} present, {len(missing)} missing.')
    if missing:
        print('MISSING GGUFs (offline model-readiness gate FAILED):')
        for alias, repo, filename in missing:
            print(f'  - {alias}  {repo}  {filename}')
        print('Bundle them with `rzfz package --include-models` (dev box) and '
              'reload via `rzfz upgrade --package …`, or sideload via '
              'config.<domain> → LLM/Models. Offline NEVER pulls from '
              'huggingface.co — a missing GGUF fails clear here (#184).')
        return 1
    print('All expected model GGUFs are present locally. Offline model-ready.')
    return 0


if __name__ == '__main__':
    import sys

    sys.exit(_main(sys.argv[1:]))
