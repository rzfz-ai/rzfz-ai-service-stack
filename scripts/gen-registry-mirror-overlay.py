#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/gen-registry-mirror-overlay.py   (#184 P2 — air-gap registry mirror)
# =============================================================================
# Emit `compose.registry-mirror.yml` — the OPT-IN air-gap registry-mirror
# overlay. For a site that runs a LOCAL docker registry (a pull-through mirror)
# instead of the save/load offline package, it REWRITES each PULLED service's
# `image:` to the mirror-prefixed ref, so install / upgrade `docker compose pull`
# fetches every image from `${RAZZFAZZ_REGISTRY_MIRROR}/<normalized path>:<tag>`
# instead of the upstream registry (Docker Hub / ghcr.io / …).
#
# WHY an overlay (mirrors gen-offline-overlay.py):
#   - It is the visible, gitignored artifact that says "this box pulls from the
#     mirror", added to COMPOSE_FILE exactly like the offline overlay (#184), the
#     corporate-proxy overlay (#181) and the hardware device overlays.
#   - Rewriting `image:` in an overlay leaves the base compose files untouched and
#     is trivially reversible (drop the overlay / clear RAZZFAZZ_REGISTRY_MIRROR).
#
# ORTHOGONAL to the network mode. A box can be online+mirror, proxied+mirror or
# offline+mirror — the mirror only changes the PULL source at install/upgrade; it
# does not touch the online/proxied/offline egress axis. scripts/lib.sh reconciles
# it whenever RAZZFAZZ_REGISTRY_MIRROR is set, ALONGSIDE the mode overlays.
#
# BEST-EFFORT + no-build-safe:
#   - Locally-BUILT services (`build:`) are NEVER rewritten — their image is built
#     on the box, not pulled, so a mirror ref would be wrong. Only pinned-image
#     (pulled) services get the rewrite.
#   - `pull_policy` is left intact (this overlay carries ONLY `image:` overrides).
#     The universal no-build overlay + the offline overlay keep governing whether a
#     runtime `up` may pull at all — this overlay only redirects the pull SOURCE.
#
# Usage (normally invoked by scripts/lib.sh::ensure_registry_mirror_overlay):
#   gen-registry-mirror-overlay.py --mirror registry.local:5000 \
#                                  [--from-config compose-config.json | -] \
#                                  [--out compose.registry-mirror.yml]
# With no --from-config it asks `docker compose config --format json` (honours the
# box's real COMPOSE_PROFILES / COMPOSE_FILE, so only ACTIVE pulled services are
# rewritten — which is what we want to redirect).
#
# Pure stdlib (no PyYAML) — reads the compose-config JSON docker already emits and
# writes deterministic YAML by hand for a fixed shape.
# =============================================================================
import argparse
import json
import subprocess
import sys


# --------------------------------------------------------------------------- #
# Image-ref rewriting — the core, unit-tested logic.                          #
# --------------------------------------------------------------------------- #

def _is_registry(component):
    """True iff the first path component of an image ref is a REGISTRY host
    rather than a Docker-Hub namespace. Docker's own rule: a host has a `.` or a
    `:` (port), or is exactly `localhost`."""
    return '.' in component or ':' in component or component == 'localhost'


def _split_repo_ref(image):
    """Split ``image`` into ``(repo, sep, ref)`` where ``sep`` is ``':'`` (tag),
    ``'@'`` (digest) or ``''`` (no tag).

    A ``:`` is only a tag separator when it comes AFTER the last ``/`` — a ``:``
    before it is a registry port (``registry.local:5000/foo``), not a tag.
    """
    if '@' in image:
        repo, _, digest = image.partition('@')
        return repo, '@', digest
    last_slash = image.rfind('/')
    last_colon = image.rfind(':')
    if last_colon > last_slash:
        return image[:last_colon], ':', image[last_colon + 1:]
    return image, '', ''


def mirror_ref(image, mirror):
    """Rewrite ``image`` to be pulled from the mirror: ``<mirror>/<path><sep><ref>``.

    Normalisation makes the bare and fully-qualified Docker-Hub forms collapse to
    the SAME mirror path:
      * ``docker.io/library/postgres:17`` → ``<mirror>/library/postgres:17``
      * ``postgres:17``                   → ``<mirror>/library/postgres:17``
      * ``ghcr.io/x/y:z``                 → ``<mirror>/x/y:z``
      * ``eyalrot2/proxy:0.7.0``          → ``<mirror>/eyalrot2/proxy:0.7.0``
      * ``busybox``                       → ``<mirror>/library/busybox``
    Idempotent: an already-mirror-prefixed ref is returned unchanged.
    """
    image = (image or '').strip()
    mirror = (mirror or '').rstrip('/')
    if not image or not mirror:
        return image
    # Idempotence: already pointing at this mirror (host + optional path prefix).
    if image.startswith(mirror + '/'):
        return image

    repo, sep, ref = _split_repo_ref(image)
    first, slash, rest = repo.partition('/')
    if slash and _is_registry(first):
        # Fully-qualified: strip the upstream registry host, keep the path.
        path = rest
    else:
        # Docker Hub (implicit). A bare single-component name is an OFFICIAL image
        # → canonical path is `library/<name>` (so it matches the docker.io form).
        path = repo
        if '/' not in path:
            path = 'library/' + path
    return '%s/%s%s%s' % (mirror, path, sep, ref)


def rewrite_services(services, mirror):
    """``{service: new_image}`` for every PULLED (non-build) service.

    ``services`` maps ``service -> {'image': str, 'build': anything|None}`` (the
    shape ``docker compose config --format json`` emits). A service is skipped
    when it has a ``build:`` section (locally built — never mirror it) or has no
    ``image:``. The remaining services get their ``image:`` mirror-rewritten;
    ``pull_policy`` is deliberately NOT touched.
    """
    out = {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        if svc.get('build'):
            continue  # locally built — its image is not pulled, do not rewrite
        image = svc.get('image')
        if not image:
            continue
        out[name] = mirror_ref(image, mirror)
    return out


# --------------------------------------------------------------------------- #
# Service discovery + rendering.                                              #
# --------------------------------------------------------------------------- #

def _run(cmd):
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def discover_services(from_config):
    """``{service: {'image', 'build'}}`` for the CURRENT compose model.

    Prefers a caller-supplied compose-config JSON (``--from-config FILE`` or
    ``-`` for stdin — test-friendly, no docker); else asks ``docker compose
    config --format json`` (honours the box's real COMPOSE_PROFILES /
    COMPOSE_FILE, so only ACTIVE services are rewritten).
    """
    raw = None
    if from_config == '-':
        raw = sys.stdin.read()
    elif from_config:
        try:
            with open(from_config) as fh:
                raw = fh.read()
        except OSError:
            return {}
    else:
        raw = _run(['docker', 'compose', 'config', '--format', 'json'])
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return doc.get('services') or {}


def render(rewritten, mirror):
    lines = []
    lines.append("# SPDX-License-Identifier: BUSL-1.1")
    lines.append("# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.")
    lines.append("# ==============================================================================")
    lines.append("# compose.registry-mirror.yml  —  GENERATED, DO NOT EDIT BY HAND (#184 P2)")
    lines.append("# ==============================================================================")
    lines.append("# Regenerate with:  rzfz setup --network-mode --status  (reconciled on init/upgrade)")
    lines.append("# Added to COMPOSE_FILE like the hardware device overlays. Present only when")
    lines.append("# RAZZFAZZ_REGISTRY_MIRROR is set. Box-local — never committed (.gitignore).")
    lines.append("#")
    lines.append("# OPT-IN air-gap indirection: for a site that runs a LOCAL pull-through docker")
    lines.append("# registry instead of the save/load offline package. Every PULLED image below")
    lines.append("# is redirected to `%s/<path>:<tag>` so install / upgrade" % mirror)
    lines.append("# `docker compose pull` fetches from the mirror. Locally-BUILT (build:) services")
    lines.append("# are NOT rewritten (their image is built on the box, never pulled). pull_policy")
    lines.append("# is left intact — this overlay only redirects the pull SOURCE, orthogonal to the")
    lines.append("# online / proxied / offline network mode.")
    lines.append("# ==============================================================================")
    lines.append("")
    lines.append("services:")
    for svc in sorted(rewritten):
        lines.append("  %s:" % svc)
        lines.append("    image: %s" % rewritten[svc])
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate compose.registry-mirror.yml (#184 P2)")
    ap.add_argument("--mirror", required=True,
                    help="registry mirror host[:port][/path] "
                         "(e.g. registry.local:5000) — RAZZFAZZ_REGISTRY_MIRROR")
    ap.add_argument("--from-config", default="",
                    help="path to a `docker compose config --format json` dump "
                         "(or '-' for stdin); else docker is invoked")
    ap.add_argument("--out", default="compose.registry-mirror.yml")
    args = ap.parse_args(argv)

    mirror = args.mirror.strip().rstrip('/')
    if not mirror:
        print("gen-registry-mirror-overlay: empty --mirror", file=sys.stderr)
        return 2

    services = discover_services(args.from_config)
    if not services:
        print("gen-registry-mirror-overlay: no services discovered "
              "(pass --from-config or run where `docker compose config` works)",
              file=sys.stderr)
        return 3

    rewritten = rewrite_services(services, mirror)
    if not rewritten:
        print("gen-registry-mirror-overlay: no PULLED (non-build) services to "
              "rewrite — nothing to overlay", file=sys.stderr)
        return 4

    text = render(rewritten, mirror)

    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w") as f:
            f.write(text)
        print("Wrote %s (%d pulled service(s) → %s)"
              % (args.out, len(rewritten), mirror), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
