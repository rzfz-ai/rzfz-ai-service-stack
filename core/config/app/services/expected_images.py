# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Expected local-image set — the SHARED source of truth for #184 offline (WS1/WS3).

`rzfz verify-images` (WS3) and the Enterprise package tooling (WS1) must ask
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
  (d) **Runtime-only images** — the per-user agent images + the gpustack
      custom backend. These are provisioned
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

Verdict contract (#2368): the expected SET is the same in every network mode —
a package must carry every image — but the VERDICT follows the box's
``RAZZFAZZ_NETWORK_MODE``. Offline, any gap is a FAIL (exit 1): the box can never
fetch it. Online (or proxied), an image the product builds only when its profile
is ENABLED (:func:`built_on_enable_images` — the llm-legacy pair, one LLM profile
per box since #1447; ``rzfz init`` and the Settings toggle build them on enable)
is a WARN ("not offline-ready") while the profile is off, and a FAIL once it is
on. Every other gap stays a FAIL in every mode.

Import-safe (``from app.services import expected_images``) AND runnable
stand-alone (``python3 expected_images.py --list|--json|--verify``) — the form
``cli/verify-images.sh`` and `the Enterprise package tooling` shell out to. Imports only the
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

# #3215: fail-open like every other source here: a stand-alone copy of this
# module without hardware_lines.py (tests build such trees) filters no line.
try:
    from app.services import hardware_lines
except Exception:  # pragma: no cover - stand-alone execution path (sys.path[0])
    try:
        import hardware_lines  # type: ignore
    except Exception:
        hardware_lines = None

compose_env = build_preflight.compose_env   # #2816: one helper, one fallback


# The three LLM-runtime profiles. Rendered individually (never together — a
# shared ``container_name`` makes a combined ``compose config`` abort); each
# yields its own gpustack image, model-sync build image, and the ollama-proxy.
# #1448 (cutover C8): llm-cuda merged into llm-legacy (HARDWARE picks the overlay).
_LLM_RUNTIME_PROFILES = ('llm-legacy',)   # #1447: 2.x removed, llm-cpu folded in

# Relative paths (from stack_root) to the two runtime-only enumeration sources.
_VERSIONS_JSON = os.path.join('config', 'manifests', 'versions.json')
_AGENT_CATALOG = os.path.join(
    'modules', 'agents', 'manager', 'app', 'services', 'catalog.py')


_OFFLINE_OVERLAY = 'compose.offline.yml'


def _child_env(stack_root):
    """#2816: the clean docker env (never the Portal's boot-time MAIN_DOMAIN ...).
    An explicit COMPOSE_FILE in the caller's shell is kept: this module reasons
    about it itself (the host CLI uses it too)."""
    env = compose_env(stack_root)
    if os.environ.get('COMPOSE_FILE') is not None:
        env['COMPOSE_FILE'] = os.environ['COMPOSE_FILE']
    return env


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
        env = _child_env(stack_root)
        env['COMPOSE_PROFILES'] = profiles
        # #2778: the offline overlay rewrites digest-pinned refs to their tag so
        # `up` can find a loaded image. The expected set is the tree's own pin,
        # which verify judges (tag + the package's recorded image id), so
        # enumerate without that overlay.
        cf = env.get('COMPOSE_FILE') or _env_value(stack_root, 'COMPOSE_FILE') or ''
        if _OFFLINE_OVERLAY in cf.split(':'):
            env['COMPOSE_FILE'] = ':'.join(e for e in cf.split(':') if e != _OFFLINE_OVERLAY)
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
    inter-key comment block between an entry's ``'image'`` and its
    ``'version'`` can't grab the wrong entry's pin. Includes disabled types —
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


def _manifest_runtime_only_meta(stack_root):
    """``{image:current → {'profile': str, 'hardware': [str, …]}}`` for the
    versions.json ``runtime_only`` entries (#2811). ``profile`` is the compose
    profile whose users need the image; ``hardware`` (optional, lowercased) the
    hardware classes it applies to — absent means every class."""
    path = os.path.join(stack_root, _VERSIONS_JSON)
    try:
        with open(path, encoding='utf-8') as fh:
            m = json.load(fh)
    except Exception:
        return {}
    out = {}
    for _, v in (m.get('hardcoded') or {}).items():
        if v.get('runtime_only') and v.get('image') and v.get('current'):
            out[f"{v['image']}:{v['current']}"] = {
                'profile': (v.get('profile') or '').strip(),
                'hardware': [str(h).strip().lower() for h in (v.get('hardware') or [])],
                'on_demand': bool(v.get('pulled_on_demand')),
            }
    return out


def runtime_only_on_demand(stack_root):
    """Runtime-only one-shot helpers the stack pulls itself, online, at the
    moment it needs them (versions.json ``pulled_on_demand``): the Vaultwarden
    SQLite-to-Postgres probe and copy images. A box that is online and already
    past that step never needs them, so on such a box their absence is a WARN,
    not a gap. Offline nothing can pull them, and a package must carry them
    (``--strict``), so there they stay required."""
    return {ref for ref, meta in _manifest_runtime_only_meta(stack_root).items()
            if meta.get('on_demand')}


def wrong_hardware_images(stack_root, hardware=None):
    """Runtime-only images that belong to ANOTHER hardware class than this box's.

    #2811: ``llama-rocm-runner-base`` is the base of the ROCm runner; on
    ``HARDWARE=cpu`` the worker launches the CPU runner, so an AMD base image
    can never be a requirement there. Fail-open like every source here: unknown
    hardware excludes nothing."""
    hw = (hardware or _hardware(stack_root) or '').strip().lower()
    if not hw:
        return set()
    return {ref for ref, meta in _manifest_runtime_only_meta(stack_root).items()
            if meta['hardware'] and hw not in meta['hardware']}


def runtime_only_off_profile(stack_root):
    """``{image_ref: profile}`` — runtime-only images whose profile is not
    enabled on this box (#2811: a runtime image is needed only where its
    profile runs)."""
    enabled = enabled_profiles(stack_root)
    return {ref: meta['profile']
            for ref, meta in _manifest_runtime_only_meta(stack_root).items()
            if meta['profile'] and meta['profile'] not in enabled}


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
    The genuinely-pulled backends — e.g. the ``kyuz0`` rocm-runner — have no such
    local build and are kept.

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


def network_mode(stack_root):
    """Effective ``RAZZFAZZ_NETWORK_MODE`` of THIS box: online|proxied|offline.

    Read from the box's ``.env`` like every other value here, through the SAME
    resolver the Config Portal offline page uses (``offline_status``, which
    mirrors ``scripts/lib.sh::razzfazz_network_mode``: explicit value, else the
    legacy booleans). ``'online'`` when it cannot be read — that is the
    ``.env.example`` default and what the CLI assumes without a value.
    """
    try:
        try:
            from app.services import offline_status
        except Exception:  # pragma: no cover - stand-alone execution path
            import offline_status  # type: ignore
        return offline_status.resolve_network_mode(
            os.path.join(stack_root, '.env'))
    except Exception:
        return 'online'


def enabled_profiles(stack_root):
    """``COMPOSE_PROFILES`` from the box's ``.env`` as a set of profile ids."""
    raw = _env_value(stack_root, 'COMPOSE_PROFILES') or ''
    return {p.strip() for p in raw.split(',') if p.strip()}


def built_on_enable_images(stack_root):
    """``{image_ref: profile}`` — custom images the product builds ONLY when
    their profile is enabled, keyed by that profile.

    #2368: ``rzfz init`` builds the LLM-runtime images for the ACTIVE llm profile
    only (one LLM profile per box, #1447); enabling the profile later builds
    them (the Settings toggle "repairs what it needs"). So on a correct ONLINE
    box without ``llm-legacy`` they are legitimately absent, and holding the box
    to them turned the readiness gate red on a fresh install from the public
    mirror. This is the enumeration's own marking of that class — not a name
    list: a ``build:`` service whose ``profiles`` names an LLM-runtime profile,
    resolved to the ref ``docker compose build`` tags it with (the same
    resolution :func:`build_preflight._resolved_image` gives the pre-build
    set). The profile-less core services that render alongside (their
    ``profiles`` is empty) are NOT in this class — they are built at install for
    every box. Rendered ONE profile at a time, like :func:`llm_runtime_images`.
    Fail-open: ``{}`` when a profile cannot be rendered.
    """
    out = {}
    for profile in _LLM_RUNTIME_PROFILES:
        config = build_preflight._compose_config_json(stack_root, profile)
        if not config:
            continue
        project = config.get('name') or ''
        for name, svc in sorted((config.get('services') or {}).items()):
            if not isinstance(svc, dict) or 'build' not in svc:
                continue
            if profile not in (svc.get('profiles') or []):
                continue
            ref = build_preflight._resolved_image(project, name, svc)
            if ref:
                out[ref] = profile
    return out


def pulled_on_enable_images(stack_root):
    """``{image_ref: profile}`` — pulled images that only a DISABLED profile renders
    (services with a ``build:`` section are excluded).

    ``rzfz init`` pulls the enabled profiles only; enabling a profile later runs
    ``docker compose up -d``, which pulls them. So on a correct ONLINE box a
    disabled profile's pulled images are legitimately absent. Rendered as the
    box's own COMPOSE_PROFILES plus ONE disabled profile at a time; the delta is
    that profile's. Fail-CLOSED: ``{}`` when the box's own render fails, so a
    broken enumeration never turns a real gap into a WARN.
    """
    enabled = enabled_profiles(stack_root)
    base = _compose_images(stack_root, ','.join(sorted(enabled)))
    if not base:
        return {}
    out = {}
    all_profiles = [p for p in build_preflight._all_build_profiles(stack_root).split(',') if p]
    for profile in all_profiles:
        if profile in enabled:
            continue
        with_it = ','.join(sorted(enabled | {profile}))
        # Only what `up -d` can PULL: a service with a ``build:`` section is
        # built, never pulled (and the no-build overlay stops `up -d` from
        # building it), so its ref is not this class. Judged on the same
        # render the verdict uses; fail-closed per profile when it will not
        # render as JSON.
        config = build_preflight._compose_config_json(stack_root, with_it)
        if not config:
            continue
        project = config.get('name') or ''
        built = {build_preflight._resolved_image(project, name, svc)
                 for name, svc in (config.get('services') or {}).items()
                 if isinstance(svc, dict) and 'build' in svc}
        for ref in sorted(_compose_images(stack_root, with_it) - base - built):
            out.setdefault(ref, profile)
    return out


def hardware_line_images(stack_root):
    """``{line: set(refs)}``: the LLM runtime rendered once per device overlay
    (the renders an offline package (Enterprise) takes per line, #2387). Empty when the box's
    COMPOSE_FILE cannot be read or nothing renders (fail-open)."""
    cf = os.environ.get('COMPOSE_FILE') or _env_value(stack_root, 'COMPOSE_FILE') or ''
    base = hardware_lines.without_overlay(cf)
    if not base:
        return {}
    out = {}
    saved = os.environ.get('COMPOSE_FILE')
    try:
        for line in hardware_lines.LINES:
            os.environ['COMPOSE_FILE'] = ':'.join(base + [hardware_lines.OVERLAY.format(line)])
            refs = set()
            for p in _LLM_RUNTIME_PROFILES:
                refs |= _compose_images(stack_root, p)
            if refs:
                out[line] = refs
    finally:
        if saved is None:
            os.environ.pop('COMPOSE_FILE', None)
        else:
            os.environ['COMPOSE_FILE'] = saved
    return out


def other_hardware_line_images(stack_root, hardware=None):
    """#3215: the images of the other hardware lines this box's line does not
    use (razzfazz-gpustack:vulkan on a CPU box) — the rule the stick loader
    skips them by (hardware_lines.skipped), fed by this box's own renders."""
    if hardware_lines is None:
        return set()
    hw = (hardware or _hardware(stack_root) or '').strip().lower()
    if hw not in hardware_lines.LINES:
        return set()
    lines = hardware_line_images(stack_root)
    if len(lines) != len(hardware_lines.LINES):
        return set()   # a line did not render: no verdict, never guess an image away
    return hardware_lines.skipped(lines, hw)


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


def _canonical(ref):
    """``repo`` and ``repo:latest`` are ONE image to Docker (#2221)."""
    if '@' in ref:
        return ref
    return ref if ':' in ref.rsplit('/', 1)[-1] else f'{ref}:latest'


def _dedupe(refs):
    """One spelling per image. The BARE spelling wins over its ``:latest``
    twin: every other custom image is written bare, and the spelling is what
    the archive name, ``image_ids`` and the provenance digests are keyed by —
    changing it would make an older package's records miss (#2221)."""
    chosen = {}
    for ref in sorted(refs, key=lambda r: (_canonical(r), ':' in r.rsplit('/', 1)[-1])):
        chosen.setdefault(_canonical(ref), ref)
    return set(chosen.values())


def categorized(stack_root):
    """The expected set split into ``{stack, llm_runtime, runtime_only}``.

    ``stack`` = the all-profiles-minus-trio set (build + pull); the other two are
    the additions that set never renders (subtracted so each image lands in
    exactly one bucket — an image a profile renders lands in ``stack`` and is
    therefore NOT re-listed under ``runtime_only``).
    """
    stack = _dedupe(_build_set(stack_root))
    seen = {_canonical(r) for r in stack}
    llm = {r for r in _dedupe(llm_runtime_images(stack_root)) if _canonical(r) not in seen}
    seen |= {_canonical(r) for r in llm}
    runtime = {r for r in _dedupe(runtime_only_images(stack_root)) if _canonical(r) not in seen}
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


def built_images(stack_root, active=True):
    """Refs of the services this stack BUILDS locally (a ``build:`` section).

    They cannot be pulled: a bare ``docker pull`` of ``razzfazz-mcp-manager:latest``
    resolves to docker.io and fails. ``active`` scopes the render to the box's
    ``COMPOSE_PROFILES`` (like :func:`active_images`); otherwise every profile.
    Empty on any failure (fail-open).
    """
    if active:
        profiles = _env_value(stack_root, 'COMPOSE_PROFILES') or ''
    else:
        profiles = build_preflight._all_build_profiles(stack_root)
    config = build_preflight._compose_config_json(stack_root, profiles)
    if not config:
        return set()
    project = config.get('name') or ''
    return {build_preflight._resolved_image(project, name, svc)
            for name, svc in (config.get('services') or {}).items()
            if isinstance(svc, dict) and svc.get('build')}


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

    Written into the offline package by `the Enterprise package tooling` and consumed by the
    offline ``--package`` upgrade + ``rzfz verify-images`` as the single shared
    source of truth for what the package should contain.
    """
    cats = categorized(stack_root)
    images = expected_images(stack_root)
    digests, context_files = _custom_image_provenance(stack_root)
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
        'custom_image_digests': digests,
        # #2818: the exact files each digest above was taken over. A box whose
        # tree IS this release hashes these and only these, so a file the
        # release deleted but the package upgrade left on disk, or a
        # __pycache__ a python run wrote there, cannot make the image stale.
        'custom_image_context_files': context_files,
    }


def _export_image_state(stack_root):
    """#3205: `sudo rzfz verify-images` reads the installing user's build record,
    not /root's: export the resolved path so every reader (the provenance and
    digest-pin helpers, in-process or not) uses the same one."""
    if os.environ.get('RAZZFAZZ_IMAGE_STATE'):
        return
    try:
        try:
            from app.services import image_provenance
        except Exception:  # pragma: no cover - stand-alone execution path
            import image_provenance  # type: ignore
        path = image_provenance.default_state_path(stack_root)
    except Exception:
        return
    if path != os.path.join(os.path.expanduser('~'), image_provenance._STATE_REL):
        os.environ['RAZZFAZZ_IMAGE_STATE'] = path


def _custom_image_provenance(stack_root):
    """``({image_ref: digest}, {image_ref: [files]})`` for every custom image,
    taken over THIS tree (never a package's list), or ``({}, {})`` (fail-open)."""
    try:
        try:
            from app.services import image_provenance
        except Exception:  # pragma: no cover - stand-alone execution path
            import image_provenance  # type: ignore
        rows = [r for r in image_provenance.all_custom_images(stack_root, with_files=True) if r.get('digest')]
    except Exception:
        return {}, {}
    return ({r['image']: r['digest'] for r in rows},
            {r['image']: r['files'] for r in rows if isinstance(r.get('files'), list)})


def _custom_image_digests(stack_root):
    """``{image_ref: digest}`` for every custom image, or ``{}`` (fail-open)."""
    return _custom_image_provenance(stack_root)[0]


# --------------------------------------------------------------------------- #
# Stand-alone CLI — cli/verify-images.sh and the Enterprise package tooling shell out to this. #
# --------------------------------------------------------------------------- #

#: #2757: every image is present, but a custom image is not this tree's build.
#: Distinct from 1 (missing) so the upgrade can say which of the two stopped it.
EXIT_STALE = 3


def _main(argv):
    import argparse

    parser = argparse.ArgumentParser(
        prog='expected_images',
        description='Shared expected-local-image enumeration for `rzfz '
                    'verify-images` (WS3) and the offline image bundle '
                    '(WS1). #184 offline / air-gap.')
    parser.add_argument('--stack-root', default=os.getcwd(),
                        help='stack checkout root (default: current directory)')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true',
                      help='print the expected image set, one ref per line')
    mode.add_argument('--json', action='store_true',
                      help='print the expected-images.json manifest (flat list '
                           '+ per-category breakdown + metadata)')
    mode.add_argument('--missing', action='store_true',
                      help='print the refs of the chosen scope (--active or '
                           'full) that are NOT present locally, one per line; '
                           'exit 0')
    mode.add_argument('--verify', action='store_true',
                      help='`docker image inspect` every expected image; report '
                           'present/missing and exit non-zero on any gap')
    parser.add_argument('--quiet', action='store_true',
                        help='with --verify: print only the summary + any '
                             'missing images (for programmatic callers)')
    parser.add_argument('--built', action='store_true',
                        help='with --missing: only the missing refs the stack '
                             'BUILDS locally (they cannot be pulled)')
    parser.add_argument('--strict', action='store_true',
                        help='with --verify: judge the set as a package\'s '
                             'completeness — the images a disabled profile '
                             'PULLS on enable are required, not a WARN (the '
                             'network mode is not consulted)')
    parser.add_argument('--active', action='store_true',
                        help='scope --list/--verify/--missing to the images THIS box\'s '
                             'compose render needs (COMPOSE_PROFILES from .env) '
                             'instead of every image across every profile — '
                             'the online pre-restart gate (#2035)')
    args = parser.parse_args(argv)
    root = args.stack_root
    _export_image_state(root)
    if args.json and args.active:
        # The package manifest is the FULL contract by definition; a scoped one
        # would let a package claim a completeness it does not have.
        parser.error('--active applies to --list/--verify/--missing, not --json')
    if args.strict and not args.verify:
        parser.error('--strict applies to --verify only')
    if args.built and not args.missing:
        parser.error('--built applies to --missing only')
    scope = 'active' if args.active else 'full'
    enumerate_ = active_images if args.active else expected_images

    if args.list:
        for ref in enumerate_(root):
            print(ref)
        return 0

    if args.missing:
        absent = missing_images(root, enumerate_(root))
        if args.built:
            built = built_images(root, active=args.active)
            absent = [ref for ref in absent if ref in built]
        for ref in absent:
            print(ref)
        return 0

    if args.json:
        print(json.dumps(build_manifest(root), indent=2))
        return 0

    # --verify
    expected = enumerate_(root)
    # #2811: an image that belongs to another hardware class is not a gap on
    # THIS box in any network mode. A package's completeness (--strict) still
    # carries every class.
    if not args.strict and not args.active:
        not_here = wrong_hardware_images(root)
        # #3215: and the other hardware lines' LLM runtime (the stick loads
        # only this box's line, #3201).
        not_here = set(not_here) | other_hardware_line_images(root)
        expected = [r for r in expected if r not in not_here]
    if not expected:
        # Enumeration itself failed (no docker / compose parse error). Fail-open
        # so a broken tool never hard-blocks a box; make the reason visible.
        print('verify-images: could not enumerate the expected image set '
              '(docker/compose unavailable or unparseable) — skipping the '
              'presence gate.')
        return 0
    missing = missing_images(root, expected)
    missing_set = set(missing)
    present = [r for r in expected if r not in missing_set]
    # #2368: the SET is mode-independent (a package must carry everything; the
    # summary line below keeps counting these as missing so `rzfz status` and
    # the Config Portal offline page, which parse it, keep saying "not
    # offline-ready"). The VERDICT is not: online, an image the product builds
    # on enable is a WARN while its profile is off. Offline it stays a FAIL —
    # nothing on that box can build it. The --active scope is untouched: it is
    # exactly what `up` needs, so a gap there is always a FAIL.
    mode = network_mode(root)
    deferred = {}
    pulled_deferred = {}
    if missing and not args.active and mode != 'offline':
        enabled = enabled_profiles(root)
        on_enable = built_on_enable_images(root)
        deferred = {ref: on_enable[ref] for ref in missing
                    if ref in on_enable and on_enable[ref] not in enabled}
        on_pull = {} if args.strict else pulled_on_enable_images(root)
        if not args.strict:
            # #2811: runtime-only images (outside compose) of a profile that is off
            for ref, prof in runtime_only_off_profile(root).items():
                on_pull.setdefault(ref, prof)
        pulled_deferred = {ref: on_pull[ref] for ref in missing
                           if ref not in deferred and ref in on_pull}
    on_demand = []
    if missing and not args.active and not args.strict and mode != 'offline':
        helpers = runtime_only_on_demand(root)
        on_demand = [ref for ref in missing
                     if ref in helpers and ref not in deferred and ref not in pulled_deferred]
    hard_missing = [ref for ref in missing
                    if ref not in deferred and ref not in pulled_deferred and ref not in on_demand]
    if not args.quiet:
        for ref in expected:
            if ref in deferred or ref in pulled_deferred or ref in on_demand:
                mark = 'WARN'
            else:
                mark = 'MISSING' if ref in missing_set else 'ok'
            print(f'  [{mark:>7}] {ref}')
    print(f'verify-images ({scope} set): {len(expected)} expected, '
          f'{len(present)} present, {len(missing)} missing.')
    if deferred:
        print('WARN: not offline-ready (built when the profile is enabled):')
        for ref, profile in deferred.items():
            print(f'  - {ref}  ({profile} is not in COMPOSE_PROFILES)')
        print(f'This box is {mode} (RAZZFAZZ_NETWORK_MODE): the product builds '
              'these when the profile is enabled (`rzfz init` / the Settings '
              'toggle, #1447), so they are not required now. An offline package '
              'must still carry them — with RAZZFAZZ_NETWORK_MODE=offline this '
              'gap is a FAIL (#2368).')
    if pulled_deferred:
        print('WARN: not offline-ready (pulled when the profile is enabled):')
        for ref, profile in pulled_deferred.items():
            print(f'  - {ref}  ({profile} is not in COMPOSE_PROFILES)')
        print(f'This box is {mode}: enabling the profile pulls these (`docker compose '
              'up -d` from the Settings toggle or `rzfz post-install --refresh`). '
              'An offline package must still carry them.')
    if on_demand:
        print('WARN: not offline-ready (one-time helpers, pulled only when needed):')
        for ref in on_demand:
            print(f'  - {ref}')
        print(f'This box is {mode}: the update pulls these itself on the one run that needs '
              'them (moving an existing Vaultwarden database to Postgres); a box past that '
              'step never uses them. An offline package must still carry them.')
    if hard_missing:
        if args.active:
            print('MISSING images (pre-restart gate FAILED, #2035):')
        else:
            print('MISSING images (offline-readiness gate FAILED):')
        for ref in hard_missing:
            print(f'  - {ref}')
        for ref in hard_missing:
            if _digest_mismatch(ref):
                print(f'NOTE: {ref} — the local {_digest_mismatch(ref)} image is NOT the image '
                      'the package recorded for that digest. Reload it from the package (#2778).')
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
    if deferred or pulled_deferred or on_demand:
        # The gate holds for THIS box; the set is still incomplete for a
        # package, so "Offline-ready" is withheld (and provenance is moot).
        print('Every image this box requires is present locally. Not offline-ready.')
        return 0
    # #2168: presence is not provenance. On 0.175 this said "Offline-ready" at
    # the same moment the installer judged two custom images stale and refused
    # the set. Ask the same helper the installer asks, and say what it will do.
    stale = _provenance_stale(root)
    if stale is None:
        print('All expected images are present locally. Offline-ready '
              '(provenance not checked: the provenance helper could not answer).')
        return 0
    # #3003: a stale image of a module that is OFF is not restarted onto — the
    # restart creates only what the enabled profiles demand. It is the same class
    # as a missing image of a disabled profile (a WARN), not a refusal: on a box
    # installed from a stick, `rzfz init` reloaded the stick's older builds of
    # disabled modules, and the next code-only package upgrade then refused
    # although nothing it restarts was stale. Fail closed: when the active set
    # cannot be rendered, every stale image counts.
    stale_off = []
    if stale:
        active = _active_custom_refs(root)
        if active is not None:
            stale_off = [(ref, why) for ref, why in stale if ref not in active]
            stale = [(ref, why) for ref, why in stale if ref in active]
        if stale_off:
            print('Custom images of modules that are OFF are not this tree\'s build (nothing restarts '
                  'onto them; enabling the module needs a package that carries them or an online rebuild):')
            for ref, why in stale_off:
                print(f'  [   WARN] {ref}: {why}')
    if stale:
        print('All expected images are present locally — but NOT all as this tree\'s build:')
        for ref, why in stale:
            print(f'  [  stale] {ref}: {why}')
        print('The installer rebuilds these online and REFUSES the set offline (#2006 part 2, '
              '#2167). Rebuild the package from THIS tree, or run `rzfz post-install --refresh` '
              'online. Not offline-ready.')
        # #2757: a stale custom image BLOCKS wherever nothing can rebuild it: an
        # offline box, or a package's completeness (--strict). The upgrade used to
        # warn here and restart the stack on the old builds (a fixed CVE kept).
        # Online and not strict the installer rebuilds them, so the exit stays 0.
        if mode == 'offline' or args.strict:
            print(f'STALE custom images ({len(stale)}): the stack must not be restarted onto '
                  'them. This needs a package that carries these images '
                  '(an offline package (Enterprise)) or an online rebuild '
                  '(`rzfz post-install --refresh`).')
            return EXIT_STALE
        return 0
    if stale_off:
        print('All expected images are present locally, and every custom image the enabled '
              'modules run is this tree\'s build or the package\'s. Safe to restart. Not offline-ready.')
        return 0
    print('All expected images are present locally, and every custom image is this '
          'tree\'s build or the package\'s. Offline-ready.')
    return 0


def _digest_mismatch(ref):
    """The tag of a digest-pinned ``ref`` whose local image is another image, else ''."""
    dp = getattr(build_preflight, 'digest_pins', None)
    if dp is None or not dp.split(ref):
        return ''
    return dp.split(ref)[0] if dp.verdict(ref) == 'mismatch' else ''


def _active_custom_refs(root):
    """#3003: the custom image refs the box's ENABLED profiles demand (what the
    restart creates); None when that cannot be rendered — callers then fail closed."""
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location('image_provenance', os.path.join(here, 'image_provenance.py'))
        ip = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ip)
        rows = ip.demanded_images(str(root), ip.compose_profiles(str(root)))
    except Exception:
        return None
    refs = {r['image'] for r in rows if r.get('custom')}
    # the profile-less core services always build custom images: an empty set
    # means the render failed, not that nothing is active
    return refs or None


def _provenance_stale(root):
    """[(ref, why)] for custom images the provenance helper calls missing or
    stale across every profile; [] when none; None when it cannot answer."""
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location('image_provenance', os.path.join(here, 'image_provenance.py'))
        ip = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ip)
        vs = ip.verdicts(str(root), '', ip.default_state_path(str(root)), None,
                         rows=ip.all_custom_images(str(root), ip.release_context_files(str(root))))
    except Exception:
        return None
    return [(v['image'], v['detail']) for v in ip.not_from_this_tree(vs)]


if __name__ == '__main__':
    import sys

    sys.exit(_main(sys.argv[1:]))
