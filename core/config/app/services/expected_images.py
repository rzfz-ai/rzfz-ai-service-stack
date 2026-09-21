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
# #1448 (cutover C8): llm-cuda merged into llm-legacy (HARDWARE picks the overlay).
_LLM_RUNTIME_PROFILES = ('llm-legacy',)   # #1447: 2.x removed, llm-cpu folded in

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


_ENGINE_IMAGES_REL = os.path.join(
    'modules', 'llm', 'node-agent', 'app', 'drivers', 'images.py')

def _env_value(stack_root, key):
    """``key`` from the box's ``.env``. ``None`` if absent or unreadable.

    Read here rather than trusting ``os.environ``: ``verify-images`` runs from a
    shell that has not sourced anything, and the whole point is to describe THIS
    box. Never sources the file — operator-edited values carry spaces, quotes and
    shell metacharacters.
    """
    try:
        with open(os.path.join(stack_root, '.env')) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + '='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        return None
    return None


def _hardware(stack_root):
    """``HARDWARE`` from the box's ``.env``, lowercased. ``None`` if unreadable."""
    value = _env_value(stack_root, 'HARDWARE')
    return value.lower() if value else None


def engine_runner_images(stack_root, hardware=None):
    """(e) The LLM engine image THIS box would launch — exactly one.

    The worker-agent spawns engines through docker-py, so they appear in no
    ``compose config --images`` output and in no ``runtime_only`` manifest
    entry. That gap is why #331 — the AMD driver pointing at
    ``razzfazz-llama-vulkan-runner:latest``, a name nothing in the repo builds —
    passed every check until an AMD deploy failed on a customer box.

    HARDWARE-CONDITIONAL, and that is the whole design (agent-rzfz on #496):
    runners are built per hardware class, so a flat three-entry list would tell
    every correctly-provisioned box it is missing two images. An AMD box expects
    the AMD runner and nothing else.

    The ref comes from ``images.py::engine_image()`` — the SAME authority the
    launcher uses (#331/#489) — so a repin cannot leave the verifier checking
    for an image the box no longer launches. It is loaded by PATH because
    ``app/__init__.py`` imports fastapi, which is absent outside the worker-agent
    container; a plain import would fail on every box that runs this check.

    Fail-open, like every other source here: unknown hardware, a missing
    ``.env`` or an unloadable module contributes ``set()``. A verifier that
    crashes is worse than one that under-reports.
    """
    hw = (hardware or _hardware(stack_root) or '').strip().lower()
    if not hw:
        return set()
    path = os.path.join(stack_root, _ENGINE_IMAGES_REL)
    if not os.path.isfile(path):
        return set()
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            '_rzfz_engine_images', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # #1518 (E5) / #1546: speak the LAUNCHER's vocabulary. The fleet writes
        # `HARDWARE=nvidia` (the #1517 enrolment token, and what .env.example
        # ships) while the image map is keyed on the driver classes. Without
        # this, `ENV_VARS[hw]` finds no key, the `.env` override branch below is
        # skipped, and a deliberately repinned NVIDIA box gets the built-in
        # default packaged and verified instead of its pin. `engine_image()`
        # normalises internally too — this line is what makes the OVERRIDE
        # lookup agree with it, and `test_1546_…::test_an_env_override_reaches_
        # an_nvidia_box` is red without it.
        hw = mod.normalize_hardware(hw)
        # PR review R1: engine_image() resolves its RAZZFAZZ_ENGINE_IMAGE_*
        # override from os.environ — which on a box is the VERIFIER's shell,
        # never sourced from .env. #495 delivers the override to the
        # worker-agent CONTAINER via compose/.env, so the box's .env is the
        # authority here too (exactly like HARDWARE above). Inject it around
        # the call — and clear a shell-only value, which describes the
        # verifier's process, not the box — restoring the environment after,
        # so engine_image() stays the single precedence authority.
        key = getattr(mod, 'ENV_VARS', {}).get(hw)
        if not key:
            return {mod.engine_image(hw)}
        override = _env_value(stack_root, key)
        prev = os.environ.get(key)
        try:
            if override:
                os.environ[key] = override
            else:
                os.environ.pop(key, None)
            return {mod.engine_image(hw)}
        finally:
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
    except Exception:
        # ValueError for an unknown hardware class, anything else for a broken
        # module — both mean "cannot say", not "nothing is expected here".
        return set()


def llm_runtime_images(stack_root):
    """Union of image refs across the three LLM-runtime profiles (rendered apart)."""
    out = set()
    for p in _LLM_RUNTIME_PROFILES:
        out |= _compose_images(stack_root, p)
    # (e) #496: the worker-agent's own engine image, which renders in no profile.
    out |= engine_runner_images(stack_root)
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


def active_images(stack_root):
    """The image refs THIS box's ``docker compose up`` will need: the compose
    render scoped to the ``COMPOSE_PROFILES`` in the box's own ``.env``.

    #2035: the full :func:`expected_images` set is the OFFLINE contract — every
    image across every profile, because a package must carry all of them. An
    ONLINE box never pulls the profiles it does not run, so holding it to the
    full set would fail every upgrade for images of modules the operator never
    enabled. What an online upgrade must verify before it restarts is exactly
    the set `up` will try to create — this one. Sorted; empty on failure
    (fail-open, same as the full set).
    """
    profiles = _env_value(stack_root, 'COMPOSE_PROFILES') or ''
    return sorted(_compose_images(stack_root, profiles))


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
        # #2006 part 2: the build-context digest of every custom image the
        # packaging tree carries. A box that loads this package compares them
        # with ITS tree's digests: equal -> the loaded image is this tree's build
        # (`package-build`); different -> the package predates a change and the
        # image is `stale`, which blocks the zero-download skip instead of
        # letting an old package be judged COMPLETE by its own names.
        'custom_image_digests': _custom_image_digests(stack_root),
    }


def _custom_image_digests(stack_root):
    """``{image_ref: digest}`` for every custom image, or ``{}`` (fail-open)."""
    try:
        try:
            from app.services import image_provenance
        except Exception:  # pragma: no cover - stand-alone execution path
            import image_provenance  # type: ignore
        return {r['image']: r['digest'] for r in image_provenance.all_custom_images(stack_root)
                if r.get('digest')}
    except Exception:
        return {}


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
    parser.add_argument('--active', action='store_true',
                        help='scope --list/--verify to the images THIS box\'s '
                             'compose render needs (COMPOSE_PROFILES from .env) '
                             'instead of every image across every profile — '
                             'the online pre-restart gate (#2035)')
    args = parser.parse_args(argv)
    root = args.stack_root
    if args.json and args.active:
        # The package manifest is the FULL contract by definition; a scoped one
        # would let a package claim a completeness it does not have.
        parser.error('--active applies to --list/--verify, not --json')
    scope = 'active' if args.active else 'full'
    enumerate_ = active_images if args.active else expected_images

    if args.list:
        for ref in enumerate_(root):
            print(ref)
        return 0

    if args.json:
        print(json.dumps(build_manifest(root), indent=2))
        return 0

    # --verify
    expected = enumerate_(root)
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
    print(f'verify-images ({scope} set): {len(expected)} expected, '
          f'{len(present)} present, {len(missing)} missing.')
    if missing:
        if args.active:
            print('MISSING images (pre-restart gate FAILED, #2035):')
        else:
            print('MISSING images (offline-readiness gate FAILED):')
        for ref in missing:
            print(f'  - {ref}')
        if args.active:
            print('These are images THIS box\'s compose render needs and `docker '
                  'compose up` would fail to create. Either the pull failed '
                  '(registry unreachable, rate limit) or the pin names a tag '
                  'that does not exist — check the pull output above, fix the '
                  'pin or the network, then retry `rzfz upgrade` (#2035).')
        else:
            print('Build/pull them at install (online), or reload them from the '
                  'offline package (`rzfz upgrade --package …` loads images/*.tar '
                  'before restart). The runtime never builds — a missing image '
                  'fails clear here rather than triggering a silent build (#184).')
        return 1
    if args.active:
        print('All images this box runs are present locally. Safe to restart.')
        return 0
    # #2168: presence is not provenance. On 0.175 this said "Offline-ready" at
    # the same moment the installer judged two custom images stale and refused
    # the set. Ask the same helper the installer asks, and say what it will do.
    stale = _provenance_stale(root)
    if stale is None:
        print('All expected images are present locally. Offline-ready '
              '(provenance not checked: the provenance helper could not answer).')
        return 0
    if stale:
        print('All expected images are present locally — but NOT all as this tree\'s build:')
        for ref, why in stale:
            print(f'  [  stale] {ref}: {why}')
        print('The installer rebuilds these online and REFUSES the set offline (#2006 part 2, '
              '#2167). Rebuild the package from THIS tree, or run `rzfz post-install --refresh` '
              'online. Not offline-ready.')
        return 0
    print('All expected images are present locally, and every custom image is this '
          'tree\'s build or the package\'s. Offline-ready.')
    return 0


def _provenance_stale(root):
    """[(ref, why)] for custom images the provenance helper calls missing or
    stale across every profile; [] when none; None when it cannot answer."""
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location('image_provenance', os.path.join(here, 'image_provenance.py'))
        ip = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ip)
        vs = ip.verdicts(str(root), '', ip.default_state_path(), None, rows=ip.all_custom_images(str(root)))
    except Exception:
        return None
    return [(v['image'], v['detail']) for v in ip.not_from_this_tree(vs)]


if __name__ == '__main__':
    import sys

    sys.exit(_main(sys.argv[1:]))
