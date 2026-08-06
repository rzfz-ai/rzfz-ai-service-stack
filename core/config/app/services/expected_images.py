# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Expected local-image set — the SHARED source of truth for #184 offline (WS1/WS3).

`rzfz verify-images` (WS3) and `cli/package.sh --include-images` (WS1) must ask
the SAME question — *"which container images must exist locally on a fully
provisioned box, across ALL profiles?"* — and never disagree, or the package
saves an image the verifier then flags missing (or vice-versa). This module is
that one answer.

The expected set is the union of four sources:

  (a) **Custom-build images** — every ``build:`` service, from
      :func:`build_preflight.all_build_images` (the #174 source of truth the
      module-enable pre-flight also uses, so a missing custom image fails the
      same way in both places).
  (b) **Pulled + derived images across ALL profiles bar the llm trio** —
      ``docker compose config --images`` scoped to the all-profiles set. This
      already includes (a)'s refs (compose synthesises the ``<project>-<svc>``
      name for image-less build services), but (a) is unioned in explicitly so
      the build-set source of truth is represented even if a profile fails to
      render.
  (c) **LLM-runtime variants** — the ``llm`` / ``llm-legacy`` / ``llm-cpu``
      profiles share a ``container_name`` (``gpustack`` / ``model-sync``) so a
      naive all-profiles ``compose config`` ABORTS; they are rendered ONE AT A
      TIME (mirroring :func:`build_preflight._all_build_profiles`, which drops
      the trio) and unioned, capturing every gpustack image (v2.1.x /
      v0.7.1-cpu / the GitLab vulkan build), each model-sync build image, and
      the shared ollama-proxy.
  (d) **Runtime-only images** — the per-user agent images + the openhands
      runtime sidecar + the gpustack custom backend. These are provisioned
      OUTSIDE the compose graph (agent-manager spawns one container per
      Authentik user via the docker-socket-proxy; gpustack spawns its custom
      backend per model), so they never appear in ``compose config --images``.
      Source = ``config/manifests/versions.json`` ``runtime_only`` entries ∪ the
      ``agents`` catalog ``image``/``version`` pairs (see
      :func:`runtime_only_images` for the built-locally supersede rule).

Fail-open contract (mirrors build_preflight): if we cannot enumerate (no docker,
no ``compose.yml``, a parse error) the offending source contributes ``set()`` /
``[]`` rather than raising, so a verifier degrades to "cannot determine" instead
of a false hard-block.

Import-safe (``from app.services import expected_images``) AND runnable
stand-alone (``python3 expected_images.py --list|--json|--verify``) — the form
``cli/verify-images.sh`` and ``cli/package.sh`` shell out to. Imports only the
stdlib + the sibling ``build_preflight`` module.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

try:  # import-safe both as ``app.services.expected_images`` and stand-alone.
    from app.services import build_preflight
except Exception:  # pragma: no cover - stand-alone execution path (sys.path[0])
    import build_preflight  # type: ignore


# The three LLM-runtime profiles. Rendered individually (never together — a
# shared ``container_name`` makes a combined ``compose config`` abort); each
# yields its own gpustack image, model-sync build image, and the ollama-proxy.
_LLM_RUNTIME_PROFILES = ('llm', 'llm-legacy', 'llm-cpu')

# Relative paths (from stack_root) to the two runtime-only enumeration sources.
_VERSIONS_JSON = os.path.join('config', 'manifests', 'versions.json')
_AGENT_CATALOG = os.path.join(
    'modules', 'agents', 'manager', 'app', 'services', 'catalog.py')


def _compose_images(stack_root, profiles):
    """Image refs from ``docker compose config --images`` scoped to ``profiles``.

    ``profiles`` is written verbatim to ``COMPOSE_PROFILES``; ``COMPOSE_FILE``
    (which carries the ``modules/llm/compose.devices.<hw>.yml`` overlay) is left
    to be read from the box's ``.env`` so the enumeration matches what the box
    would actually run. Returns a ``set``; empty on any failure (fail-open).
    """
    if not os.path.isfile(os.path.join(stack_root, 'compose.yml')):
        return set()
    try:
        env = dict(os.environ)
        env['COMPOSE_PROFILES'] = profiles
        out = subprocess.run(
            ['docker', 'compose', 'config', '--images'],
            cwd=stack_root, env=env,
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            return set()
        return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}
    except Exception:
        return set()


def _catalog_image_versions(stack_root):
    """``[(image, version), …]`` from the agent-manager catalog.

    Read via regex — importing the module pulls Flask/psycopg2 deps the offline
    caller (bash / the dev box) may not have. Each catalog entry is a dict whose
    ``'image'`` precedes its ``'version'``; the search for the version (and any
    ``companion_version``) is BOUNDED by the NEXT entry's ``'image'`` so a large
    inter-key comment block (openhands' ``'version'`` sits ~1.5 kB after its
    ``'image'``) can't grab the wrong entry's pin. Includes disabled types —
    their images still ship in the offline package. Mirrors the enumeration in
    ``scripts/generate-sbom.sh`` so the two never drift.
    """
    path = os.path.join(stack_root, _AGENT_CATALOG)
    if not os.path.isfile(path):
        return []
    try:
        src = open(path, encoding='utf-8').read()
    except Exception:
        return []
    matches = list(re.finditer(r"'image':\s*'([^']+)'", src))
    pairs = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(src)
        block = src[m.start():end]
        vm = re.search(r"'(?:version|companion_version)':\s*'([^']+)'", block)
        if vm:
            pairs.append((m.group(1), vm.group(1)))
        cim = re.search(r"'companion_image':\s*'([^']+)'", block)
        cvm = re.search(r"'companion_version':\s*'([^']+)'", block)
        if cim and cvm:
            pairs.append((cim.group(1), cvm.group(1)))
    return pairs


def _manifest_runtime_only(stack_root):
    """``[(image, current), …]`` for versions.json ``hardcoded[]`` runtime_only."""
    path = os.path.join(stack_root, _VERSIONS_JSON)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding='utf-8') as fh:
            m = json.load(fh)
    except Exception:
        return []
    out = []
    for _, v in (m.get('hardcoded') or {}).items():
        if v.get('runtime_only') and v.get('image') and v.get('current'):
            out.append((v['image'], v['current']))
    return out


def _repo_basename(image):
    """Repo basename without registry path or tag (``ghcr.io/a/bar:1`` → ``bar``)."""
    repo = image.split(':', 1)[0]
    return repo.rsplit('/', 1)[-1]


def runtime_only_images(stack_root):
    """Per-user agent + gpustack-backend images provisioned OUTSIDE compose.

    Expected set = the ``agents`` catalog runtime pins ∪ the versions.json
    ``runtime_only`` entries, MINUS the upstream-source variants the stack
    **builds locally** rather than pulls. Two versions.json ``runtime_only``
    entries (``ghcr.io/moltis-org/moltis``, ``ghcr.io/nousresearch/hermes-agent``)
    are the audit-pin *source* for images the box actually runs as the locally
    built ``razzfazz-stack-moltis`` / ``razzfazz-stack-hermes-agent`` — requiring
    the upstream refs present would be a PERMANENT false "missing" (the box never
    fetches them; the moltis org slug is even 404 anonymously).

    Supersede rule: an upstream entry is dropped iff a ``razzfazz-*`` catalog
    local-build image ends with its repo basename (``moltis`` →
    ``razzfazz-stack-moltis``; ``hermes-agent`` → ``razzfazz-stack-hermes-agent``).
    The genuinely-pulled backends — ``ghcr.io/openhands/openhands`` + its runtime
    sidecar and the ``kyuz0`` rocm-runner — have no such local build and are kept.

    ``razzfazz-*`` catalog images are themselves left to the build set (they
    render as build services), so they are NOT duplicated here: this returns only
    the images that must be PULLED at install and reloaded from the offline
    package.
    """
    catalog = _catalog_image_versions(stack_root)
    manifest = _manifest_runtime_only(stack_root)
    local_builds = {img.split(':', 1)[0]
                    for img, _ in catalog if img.startswith('razzfazz')}

    def _superseded(image):
        base = _repo_basename(image)
        return any(name.endswith(base) for name in local_builds)

    out = set()
    for img, ver in catalog:
        if img.startswith('razzfazz'):
            continue  # a local build → covered by the build set, not pulled
        out.add(f'{img}:{ver}')
    for img, ver in manifest:
        if img.startswith('razzfazz'):
            continue
        if _superseded(img):
            continue
        out.add(f'{img}:{ver}')
    return out


def llm_runtime_images(stack_root):
    """Union of image refs across the three LLM-runtime profiles (rendered apart)."""
    out = set()
    for p in _LLM_RUNTIME_PROFILES:
        out |= _compose_images(stack_root, p)
    return out


def _build_set(stack_root):
    """(a) ∪ (b): custom-build refs + all-profiles-minus-trio compose images."""
    s = {img for _, img in build_preflight.all_build_images(stack_root) if img}
    s |= _compose_images(stack_root, build_preflight._all_build_profiles(stack_root))
    return s


def categorized(stack_root):
    """The expected set split into ``{stack, llm_runtime, runtime_only}``.

    ``stack`` = the all-profiles-minus-trio set (build + pull); the other two are
    the additions that set never renders (subtracted so each image lands in
    exactly one bucket — e.g. ``ghcr.io/openhands/openhands`` renders in ``stack``
    via the ``openhands`` profile and is therefore NOT re-listed under
    ``runtime_only``, which keeps only its out-of-compose runtime sidecar).
    """
    stack = _build_set(stack_root)
    llm = llm_runtime_images(stack_root) - stack
    runtime = runtime_only_images(stack_root) - stack - llm
    return {
        'stack': sorted(stack),
        'llm_runtime': sorted(llm),
        'runtime_only': sorted(runtime),
    }


def expected_images(stack_root):
    """The FULL sorted list of image refs that must exist locally on a box."""
    cats = categorized(stack_root)
    return sorted(set(cats['stack']) | set(cats['llm_runtime'])
                  | set(cats['runtime_only']))


def missing_images(stack_root, images=None):
    """Subset of the expected set whose ref is NOT present locally.

    ``images`` may be a pre-computed list (avoids a re-enumeration); otherwise
    the full :func:`expected_images` set is used.
    """
    imgs = images if images is not None else expected_images(stack_root)
    return [ref for ref in imgs if not build_preflight._image_exists(ref)]


def _stack_version(stack_root):
    try:
        with open(os.path.join(stack_root, 'VERSION'), encoding='utf-8') as fh:
            return fh.read().strip()
    except Exception:
        return ''


def build_manifest(stack_root):
    """The ``expected-images.json`` payload: flat list + per-category breakdown.

    Written into the offline package by ``cli/package.sh`` and consumed by the
    offline ``--package`` upgrade + ``rzfz verify-images`` as the single shared
    source of truth for what the package should contain.
    """
    cats = categorized(stack_root)
    images = expected_images(stack_root)
    return {
        'schema': 'razzfazz.expected-images/v1',
        'stack_version': _stack_version(stack_root),
        'count': len(images),
        'images': images,
        'categories': cats,
    }


# --------------------------------------------------------------------------- #
# Stand-alone CLI — cli/verify-images.sh and cli/package.sh shell out to this. #
# --------------------------------------------------------------------------- #

def _main(argv):
    import argparse

    parser = argparse.ArgumentParser(
        prog='expected_images',
        description='Shared expected-local-image enumeration for `rzfz '
                    'verify-images` (WS3) and `rzfz package --include-images` '
                    '(WS1). #184 offline / air-gap.')
    parser.add_argument('--stack-root', default=os.getcwd(),
                        help='stack checkout root (default: current directory)')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true',
                      help='print the expected image set, one ref per line')
    mode.add_argument('--json', action='store_true',
                      help='print the expected-images.json manifest (flat list '
                           '+ per-category breakdown + metadata)')
    mode.add_argument('--verify', action='store_true',
                      help='`docker image inspect` every expected image; report '
                           'present/missing and exit non-zero on any gap')
    parser.add_argument('--quiet', action='store_true',
                        help='with --verify: print only the summary + any '
                             'missing images (for programmatic callers)')
    args = parser.parse_args(argv)
    root = args.stack_root

    if args.list:
        for ref in expected_images(root):
            print(ref)
        return 0

    if args.json:
        print(json.dumps(build_manifest(root), indent=2))
        return 0

    # --verify
    expected = expected_images(root)
    if not expected:
        # Enumeration itself failed (no docker / compose parse error). Fail-open
        # so a broken tool never hard-blocks a box; make the reason visible.
        print('verify-images: could not enumerate the expected image set '
              '(docker/compose unavailable or unparseable) — skipping the '
              'presence gate.')
        return 0
    missing = missing_images(root, expected)
    present = [r for r in expected if r not in set(missing)]
    if not args.quiet:
        for ref in expected:
            mark = 'MISSING' if ref in set(missing) else 'ok'
            print(f'  [{mark:>7}] {ref}')
    print(f'verify-images: {len(expected)} expected, '
          f'{len(present)} present, {len(missing)} missing.')
    if missing:
        print('MISSING images (offline-readiness gate FAILED):')
        for ref in missing:
            print(f'  - {ref}')
        print('Build/pull them at install (online), or reload them from the '
              'offline package (`rzfz upgrade --package …` loads images/*.tar '
              'before restart). The runtime never builds — a missing image '
              'fails clear here rather than triggering a silent build (#184).')
        return 1
    print('All expected images are present locally. Offline-ready.')
    return 0


if __name__ == '__main__':
    import sys

    sys.exit(_main(sys.argv[1:]))
