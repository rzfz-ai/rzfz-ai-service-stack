# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Custom-build image enumeration — the SHARED source of truth (#174 / #184).

Two consumers ask the SAME question — *"which custom-build (``build:``) images
must exist locally?"* — and they must never disagree:

  * **Config-Portal module-enable pre-flight** (``apply_manager``): before it
    ``docker compose up``'s a newly-enabled profile it asks
    :func:`missing_build_images` *"is this ONE profile's image already built?"*.
    The portal drives docker through the **docker-socket-proxy**, whose allowlist
    DENIES the image ``/build`` endpoint (``BUILD: 0`` in ``core/compose.yml`` —
    build is a large attack surface), so if the image is absent the ``up`` would
    fail with a raw, opaque ``error from daemon: 403 forbidden`` (#174). The
    pre-flight turns that into an actionable "pre-build first" message.

  * **post-install pre-build** (``razzfazz-post-install.sh`` /
    ``cli/post-install.sh``'s ``prebuild_all_custom_images``): after install it
    asks :func:`all_build_images` *"give me EVERY custom-build image across ALL
    profiles"* and builds them all — so that a later module-enable on an
    egress-restricted / air-gapped box never triggers a live build → 403
    (#184). If the pre-build set drifts from what the pre-flight demands, you get
    exactly that failure (an image the pre-flight requires but the pre-build
    skipped — the ga.1 ``razzfazz-mcp-manager`` / ``cognee`` regression).

Deriving both from this one module means the two sets can never drift. The
scope predicate (:func:`_is_build_service`) and the image-name resolution
(:func:`_resolved_image`) are defined **once** here.

Mechanics (all safe behind the socket-proxy):

  * ``docker compose config --format json`` / ``--profiles`` is a *client-side*
    compose-file parse — it does **not** hit the daemon, so it works even though
    the socket-proxy denies almost everything.
  * ``docker image inspect`` maps to the ``IMAGES`` endpoint, which the
    socket-proxy **allows** (``IMAGES: 1``).
  * ``gpustack*`` / ``model-sync*`` are excluded — those are HARDWARE-specific
    LLM-runtime images handled by the dedicated LLM-runtime toggle, out of scope
    for the enable-403 class (and their three profiles collide on a shared
    ``container_name`` if rendered together — see :func:`_all_build_profiles`).
  * A build service with no explicit ``image:`` (cognee, cognee-frontend,
    dify-web, caddy, the core UIs, …) is tagged by compose with the derived
    ``<project>-<service>`` name; :func:`_resolved_image` reproduces that so both
    the "does it exist" check and the "build set" carry the REAL image ref.

Fail-open contract: if we cannot enumerate (no docker, no ``compose.yml``,
parse error), return ``[]`` / ``''`` so the caller lets compose proceed. The
worst case then degrades to the pre-existing 403, never a false block.

This module is import-safe (``from app.services import build_preflight``) AND
runnable stand-alone (``python3 build_preflight.py --all-build-images`` — the
form ``prebuild_all_custom_images`` uses); it imports only the stdlib.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

try:  # import-safe both as ``app.services.build_preflight`` and stand-alone.
    from app.services import digest_pins
except Exception:  # pragma: no cover - stand-alone execution path (sys.path[0])
    try:
        import digest_pins  # type: ignore
    except Exception:
        digest_pins = None

try:  # #2816: the clean docker env (stand-alone: the caller's own environment).
    from app.services.compose_allowlist import compose_env
except Exception:  # pragma: no cover - stand-alone execution path
    def compose_env(stack_root=None, environ=None):
        return dict(os.environ if environ is None else environ)

# #184 WS2a — the universal no-runtime-build overlay. It sets ``build: !reset
# null`` on every custom-build service, so a ``docker compose config`` rendered
# WITH it in COMPOSE_FILE reports NO ``build:`` sections — and this enumerator,
# which keys on ``'build' in svc``, would then find zero build services (the
# post-install pre-build would build nothing, the enable pre-flight would never
# fire). So we strip it from COMPOSE_FILE for our config render: we need the
# TRUE build set, exactly as the install-time ``docker compose build`` sees it.
_NOBUILD_OVERLAY = 'compose.no-build.yml'


def _child_env(stack_root):
    """#2816: the clean docker env (never the Portal's boot-time MAIN_DOMAIN ...).
    An explicit COMPOSE_FILE in the caller's shell is kept: this module reasons
    about it itself (the host CLI uses it too)."""
    env = compose_env(stack_root)
    if os.environ.get('COMPOSE_FILE') is not None:
        env['COMPOSE_FILE'] = os.environ['COMPOSE_FILE']
    return env


def _base_compose_file(stack_root):
    """The box's COMPOSE_FILE with the no-build overlay stripped (or ''), so a
    ``docker compose config`` render still shows the ``build:`` contexts. Reads
    the env var if set, else the box's ``.env`` (targeted parse — never sources
    it). Returns '' when nothing is set (compose then uses its own default)."""
    cf = os.environ.get('COMPOSE_FILE') or ''
    if not cf:
        try:
            with open(os.path.join(stack_root, '.env')) as f:
                for line in f:
                    s = line.strip()
                    if s.startswith('COMPOSE_FILE='):
                        cf = s.split('=', 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            cf = ''
    if not cf:
        return ''
    sep = os.environ.get('COMPOSE_PATH_SEPARATOR', ':')
    parts = [p for p in cf.split(sep) if p and p != _NOBUILD_OVERLAY]
    return sep.join(parts)


# The three LLM-runtime profiles. Each defines a ``gpustack`` + ``model-sync``
# service that share the SAME ``container_name`` (``gpustack`` / ``model-sync``);
# activating more than one at once makes ``docker compose config`` ABORT with
# ``container name "…" is already in use by service`` → an empty enumeration.
# For the ALL-profiles pre-build set we drop all three: the only build services
# they contribute are gpustack*/model-sync*, which _is_build_service excludes
# anyway (built at init for the ACTIVE llm profile; the heavy inactive variants
# are out of scope for the enable-403 class).
# #1448 (cutover C8): llm-cuda merged into llm-legacy (HARDWARE picks the overlay).
# #1447 (cutover C7a): `llm` is REMOVED from the product and still listed here
# on purpose. This set filters an OPERATOR'S COMPOSE_PROFILES, and a box in
# the middle of an upgrade still carries the token — dropping it from the
# filter would send the build preflight after a service no compose file
# defines. It is a leftover-tolerant filter, not a statement about what
# exists.
_LLM_RUNTIME_PROFILES = frozenset({'llm', 'llm-legacy', 'llm-cpu'})


def _is_build_service(name, svc):
    """True iff ``svc`` is a custom-build service in scope for pre-build/pre-flight.

    In scope ⇔ it carries a ``build:`` section AND is not a
    ``gpustack*`` / ``model-sync*`` LLM-runtime image. This is the ONE predicate
    both consumers share — change the scope here and both stay aligned.
    """
    if not isinstance(svc, dict) or 'build' not in svc:
        return False
    if name.startswith('gpustack') or name.startswith('model-sync'):
        return False
    return True


def canonical_ref(ref):
    """One spelling per image (#2659): an implicit tag is ``:latest``.

    ``razzfazz-stack-paperclip`` and ``razzfazz-stack-paperclip:latest`` name
    the same local image, but used as dictionary keys they were two records
    with contradictory verdicts. Every place that writes or reads a verdict or
    a build record keys on this form. A digest ref (``name@sha256:...``) and
    the empty string are returned as they are; a registry port
    (``host:5000/img``) is not mistaken for a tag.
    """
    if not isinstance(ref, str) or not ref or '@' in ref:
        return ref
    return ref if ':' in ref.rsplit('/', 1)[-1] else ref + ':latest'


def _resolved_image(project, name, svc):
    """The image ref ``docker compose build`` will TAG this service with.

    An explicit ``image:`` wins; otherwise compose derives the default
    ``<project>-<service>`` for a build service (that is how cognee →
    ``razzfazz-stack-cognee``, cognee-frontend → ``razzfazz-stack-cognee-frontend``
    get their names). Returns ``''`` only when neither is known (e.g. a unit-test
    render with no top-level project ``name``).
    """
    image = svc.get('image') or ''
    if image:
        return image
    if project:
        return f'{project}-{name}'
    return ''


#: #2818: the compose variables that name the RELEASE the tree is, taken from the
#: tree's VERSION file. ``.env`` carries ``RAZZFAZZ_VERSION`` too, but an upgrade
#: writes it LAST, as its completion stamp (#1593), so while the image gate runs
#: ``.env`` still names the OLD release. The worker agent's ``RAZZFAZZ_IMAGE_VERSION``
#: build arg is interpolated from it: the box hashed the old version and called the
#: package's image, built with the new one, stale. Every other build arg is
#: either a literal or a pin that ``reconcile_version_pins_from_manifest`` has already
#: moved to the target release before the gate. The packager exports the same value
#: from the tag's VERSION (the Enterprise package tooling); shell builds use razzfazz_release_version.
#: It wins over the process environment as well, because post-install exports ``.env``.
def release_compose_env(stack_root) -> dict:
    """``{"RAZZFAZZ_VERSION": <tree VERSION>}``, or ``{}`` without a VERSION file."""
    try:
        with open(os.path.join(stack_root, 'VERSION'), encoding='utf-8') as fh:
            version = fh.read().strip()
    except OSError:
        return {}
    return {'RAZZFAZZ_VERSION': version} if version else {}


def _compose_config_json(stack_root, profiles):
    """Parsed ``docker compose config --format json`` scoped to ``profiles``.

    Returns the parsed dict, or ``None`` on any failure. ``profiles`` is written
    verbatim to ``COMPOSE_PROFILES`` — a single profile id (module-enable
    pre-flight) or a comma-joined list (the ALL-profiles pre-build set) both
    work. The always-on, profile-less core services (caddy, backup, config, …)
    render regardless; they are already built so they pass the presence check
    and don't false-positive.
    """
    if not os.path.isfile(os.path.join(stack_root, 'compose.yml')):
        # No stack checkout here (e.g. a bare unit-test tmpdir). Nothing to
        # enumerate — fail open.
        return None
    try:
        env = _child_env(stack_root)
        env['COMPOSE_PROFILES'] = profiles
        env.update(release_compose_env(stack_root))
        # #184 WS2a: render against the build compose set (no-build overlay
        # stripped) so `build:` sections are present for the enumeration.
        base_cf = _base_compose_file(stack_root)
        if base_cf:
            env['COMPOSE_FILE'] = base_cf
        out = subprocess.run(
            ['docker', 'compose', 'config', '--format', 'json'],
            cwd=stack_root, env=env,
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def _all_build_profiles(stack_root):
    """Comma-joined list of ALL compose profiles EXCEPT the llm-runtime trio.

    Used to render the FULL custom-build image set in one pass. The trio is
    dropped because rendering all three together aborts ``compose config`` on a
    shared ``container_name`` (see :data:`_LLM_RUNTIME_PROFILES`). Returns ``''``
    on any failure (fail-open).
    """
    if not os.path.isfile(os.path.join(stack_root, 'compose.yml')):
        return ''
    try:
        out = subprocess.run(
            ['docker', 'compose', 'config', '--profiles'],
            cwd=stack_root, env=_child_env(stack_root), capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return ''
        profiles = sorted(
            p.strip() for p in out.stdout.splitlines()
            if p.strip() and p.strip() not in _LLM_RUNTIME_PROFILES
        )
        return ','.join(profiles)
    except Exception:
        return ''


def _build_services(config):
    """``[(service_name, resolved_image), …]`` for every in-scope build service.

    Sorted by service name. Shared spine of :func:`missing_build_images` and
    :func:`all_build_images` — same predicate, same image resolution.
    """
    project = (config or {}).get('name') or ''
    out = []
    for name, svc in sorted(((config or {}).get('services') or {}).items()):
        if not _is_build_service(name, svc):
            continue
        out.append((name, _resolved_image(project, name, svc)))
    return out


def missing_build_images(stack_root, profile_id):
    """Build-required services of ``profile_id`` whose image is absent locally.

    Returns a sorted list of ``(service_name, resolved_image)`` tuples. An empty
    list means "nothing to pre-build" — either every custom image is present, or
    we could not enumerate (fail-open; see module docstring).
    """
    config = _compose_config_json(stack_root, profile_id)
    if not config:
        return []
    return [(name, image) for name, image in _build_services(config)
            if not _image_exists(image)]


def all_build_images(stack_root):
    """EVERY custom-build image across ALL profiles — the full pre-build set.

    Returns a sorted list of ``(service_name, resolved_image)`` — the images
    ``prebuild_all_custom_images`` must build so a later module-enable is
    offline-safe (#174/#184). Shares its scope predicate and image-name
    resolution with :func:`missing_build_images`, so the pre-build set and the
    enable-time pre-flight can never drift. ``gpustack*``/``model-sync*`` are
    excluded. Fail-open: returns ``[]`` when we cannot enumerate.
    """
    profiles = _all_build_profiles(stack_root)
    if not profiles:
        return []
    config = _compose_config_json(stack_root, profiles)
    if not config:
        return []
    return _build_services(config)


def _image_exists(image_ref):
    """True iff ``image_ref`` resolves to a local image via the daemon."""
    if not image_ref:
        return False
    try:
        r = subprocess.run(
            ['docker', 'image', 'inspect', image_ref],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return True
    except Exception:
        return False
    # #2778: ``tag@sha256:...`` never resolves on a box whose images came from a
    # package (docker load drops RepoDigests). The tag is the image when its id is
    # the one the package recorded for that digest (see digest_pins).
    if digest_pins is not None and digest_pins.split(image_ref):
        return digest_pins.verdict(image_ref) in digest_pins.ACCEPTED
    return False


#: #2659: the custom-image verdicts the host leaves beside the tree on every
#: build record (image_provenance.write_verdicts_snapshot). Same name there.
VERDICTS_SNAPSHOT = '.custom-image-verdicts.json'
_VERDICTS_OK = ('this-build', 'package-build', 'unknown')

#: The operator phrase per stale ``reason`` (image_provenance.STALE_REASONS).
#: Fixed text: the host's detail strings are for the host's logs, not the Portal.
_STALE_PHRASES = {
    'box-bound': 'the offline package carries a copy built for another box',
    'old-record': 'its build record is older than the record that says what a build built',
    'not-built': 'no build of this release produced it',
    'id-changed': 'it was replaced since it was built (loaded or rebuilt elsewhere)',
    'context-changed': "it was built from an earlier release's sources",
    'package-other-context': "the offline package built it from other sources than this release's",
    'package-carries-no-image': "the offline package names it but carries no image (a code-only package)",
    'no-record': 'nothing records that it was built from this release',
}
_STALE_GENERIC = "nothing shows that it was built from this release's sources"

_log = logging.getLogger(__name__)


def _image_id(image_ref):
    """The local image id of ``image_ref``, or ``None``."""
    try:
        r = subprocess.run(
            ['docker', 'image', 'inspect', '--format', '{{.Id}}', image_ref],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    out = (r.stdout or '').strip()
    return out if r.returncode == 0 and out else None


def _read(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return fh.read().strip()
    except OSError:
        return None


def _head_commit(stack_root):
    """The commit the checkout's HEAD names, read from the files under ``.git``
    (the Portal has the tree read-only and no usable git: the mount belongs to
    another uid). Handles a ``.git`` file (worktree), a symbolic HEAD, loose
    and packed refs. ``None`` when it cannot be read."""
    gitdir = os.path.join(stack_root, '.git')
    if os.path.isfile(gitdir):
        line = _read(gitdir) or ''
        if not line.startswith('gitdir:'):
            return None
        gitdir = line.split(':', 1)[1].strip()
        if not os.path.isabs(gitdir):
            gitdir = os.path.normpath(os.path.join(stack_root, gitdir))
    common = gitdir
    c = _read(os.path.join(gitdir, 'commondir'))
    if c:
        common = c if os.path.isabs(c) else os.path.normpath(os.path.join(gitdir, c))
    head = _read(os.path.join(gitdir, 'HEAD'))
    if not head:
        return None
    if not head.startswith('ref:'):
        return head
    ref = head.split(':', 1)[1].strip()
    for d in (gitdir, common):
        val = _read(os.path.join(d, ref))
        if val:
            return val
    packed = _read(os.path.join(common, 'packed-refs')) or ''
    for line in packed.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref:
            return parts[0]
    return None


def _canonical_snapshot(images):
    """The snapshot's ``images`` keyed by canonical ref. A file written before
    #2659's normalisation can hold one image under two spellings with
    contradictory verdicts; the entry that would REFUSE wins, so a stale
    verdict is never hidden behind a ``this-build`` twin."""
    out = {}
    if not isinstance(images, dict):
        return out
    for ref, ent in images.items():
        key = canonical_ref(ref)
        prev = out.get(key)
        if prev is None or (isinstance(ent, dict) and ent.get('verdict') not in _VERDICTS_OK
                            and not (isinstance(prev, dict) and prev.get('verdict') not in _VERDICTS_OK)):
            out[key] = ent
    return out


def not_this_tree_build_images(stack_root, profile_id):
    """Custom-build services of ``profile_id`` whose PRESENT image is not this
    tree's build (#2659).

    Returns a sorted list of ``[service, image, reason]``. The Portal cannot
    judge provenance itself (no build record in the container, the tree is
    read-only), so this reads the verdicts the host wrote on its last build
    record. The file counts only while it describes the checked-out commit
    (its ``tree_commit`` against ``.git/HEAD``), and a "stale" verdict only
    while the image it was made on is still the one present (same id).
    Missing images are :func:`missing_build_images`' business. Only the
    profile's OWN services count: the core services render with every
    profile and are running already. Fail-open like the rest of this module:
    no snapshot, another commit, no render, no docker -> ``[]``.
    """
    try:
        with open(os.path.join(stack_root, VERDICTS_SNAPSHOT), encoding='utf-8') as fh:
            data = json.load(fh) or {}
        snap = _canonical_snapshot(data.get('images'))
    except Exception:
        return []
    if not snap:
        return []
    judged, head = str(data.get('tree_commit') or ''), _head_commit(stack_root) or ''
    if not judged or not head or not head.startswith(judged):
        _log.warning('custom-image verdicts in %s describe commit %s, the checkout is at %s; '
                     'not used for the enable pre-flight', VERDICTS_SNAPSHOT, judged or '?', head or '?')
        return []
    config = _compose_config_json(stack_root, profile_id)
    if not config:
        return []
    services = config.get('services') or {}
    out = []
    for name, image in _build_services(config):
        if profile_id not in ((services.get(name) or {}).get('profiles') or []):
            continue
        ent = snap.get(canonical_ref(image))
        if not isinstance(ent, dict) or ent.get('verdict') in _VERDICTS_OK or ent.get('verdict') == 'missing':
            continue
        if not ent.get('image_id') or _image_id(image) != ent.get('image_id'):
            continue   # rebuilt or replaced since the host judged it: not this verdict's image
        out.append([name, image, str(ent.get('reason') or '')])
    return out


def format_not_this_tree_error(profile_id, rows):
    """Operator message for :func:`not_this_tree_build_images` (#2659). Each
    image gets the fixed phrase of its reason, never the host's detail text."""
    listed = '; '.join(f'{image} ({svc}): {_STALE_PHRASES.get(reason, _STALE_GENERIC)}'
                       for svc, image, reason in rows)
    plural = 'image is' if len(rows) == 1 else 'images are'
    return (
        f"Cannot enable '{profile_id}': {len(rows)} custom-build {plural} present but "
        f"NOT this release's build — {listed}. Starting it would run the old image "
        f"with this release's bind-mounted code, which can crash-loop it. The "
        f"Configuration Portal cannot build images (the "
        f"docker-socket-proxy denies /build). Run `rzfz post-install --refresh` on "
        f"the host — it rebuilds images that are not this release's — then enable "
        f"'{profile_id}' again."
    )


def format_missing_error(profile_id, missing):
    """Actionable operator message that replaces the raw daemon 403 (#174)."""
    services = ' '.join(s for s, _ in missing)
    plural = 'image' if len(missing) == 1 else 'images'
    return (
        f"Cannot enable '{profile_id}': {len(missing)} custom-build {plural} "
        f"not built on this box ({services}). The Configuration Portal cannot "
        f"build images itself — the docker-socket-proxy denies /build by "
        f"design — so enabling would fail with a raw 'error from daemon: 403 "
        f"forbidden'. Pre-build first: run `rzfz post-install --refresh` to "
        f"build ALL module images (it strips the no-build overlay for the "
        f"build). A bare `docker compose build` on an installed box builds "
        f"nothing — the universal no-build overlay (compose.no-build.yml) "
        f"neutralises every build context; use `rzfz post-install --refresh` "
        f"instead. Then re-enable '{profile_id}'."
    )


def _main(argv):
    """Stand-alone CLI so shell (``prebuild_all_custom_images``) can share this
    enumeration instead of re-implementing it in inline python.

    Emits ``service<TAB>image`` lines (one per image) so a bash ``while
    IFS=$'\\t' read`` loop consumes it directly.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog='build_preflight',
        description='Shared custom-build image enumeration for the post-install '
                    'pre-build and the Config-Portal module-enable pre-flight.')
    parser.add_argument('--stack-root', default=os.getcwd(),
                        help='stack checkout root (default: current directory)')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--all-build-images', action='store_true',
                      help='print `service<TAB>image` for EVERY custom-build '
                           'service across all profiles (the full pre-build '
                           'set); gpustack*/model-sync* excluded')
    mode.add_argument('--build-profiles', action='store_true',
                      help='print the comma-joined profile set used for the '
                           'full pre-build render (all profiles minus the '
                           'llm-runtime trio)')
    mode.add_argument('--missing', metavar='PROFILE',
                      help='print `service<TAB>image` for build services of '
                           'PROFILE whose image is absent locally')
    args = parser.parse_args(argv)

    if args.build_profiles:
        profiles = _all_build_profiles(args.stack_root)
        if profiles:
            print(profiles)
    elif args.all_build_images:
        for name, image in all_build_images(args.stack_root):
            print(f'{name}\t{image}')
    else:  # --missing PROFILE
        for name, image in missing_build_images(args.stack_root, args.missing):
            print(f'{name}\t{image}')
    return 0


if __name__ == '__main__':
    import sys

    sys.exit(_main(sys.argv[1:]))
