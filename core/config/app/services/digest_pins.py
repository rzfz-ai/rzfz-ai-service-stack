# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Digest-pinned image refs on a box whose images came from a package (#2778).

``docker save`` / ``docker load`` on the classic image store (overlay2) carries
the tag and the image id but NOT ``RepoDigests``. A ref pinned as
``repo:tag@sha256:<manifest digest>`` therefore never resolves on a package box:
``docker image inspect`` says "no such image" and compose, which may not pull
offline, cannot start the service.

The split of trust:

* the PACKAGER pulled the image by that digest, so its ``RepoDigests`` held the
  digest (`the Enterprise package tooling` records the image id only then) and wrote
  ``image_ids[<digest ref>] = <image id>`` into ``expected-images.json``;
* the BOX cannot learn the digest from ``docker load``. It accepts the pin when
  the local ``repo:tag`` image has exactly the id the package recorded for that
  digest (the id is a hash of the image config: same id, same image), and
  rewrites the ref to ``repo:tag`` for ``compose up`` (``compose.offline.yml``).

``record`` keeps those attestations in ``digest-pins.json`` beside the build
record, so a later code-only package (which supersedes the package manifest,
#2757) does not lose them.

Verdicts of :func:`verdict`:

``resolved``    the digest ref itself resolves (an online pull; RepoDigests).
``attested``    the tag's image id equals the id recorded for the digest.
``unattested``  the tag is present, nothing recorded an id for this digest
                (a package built before the pins existed): accepted, because
                refusing would strand a box that has no other way to run it.
``mismatch``    the tag is present but is NOT the image recorded for the digest.
``absent``      neither the digest ref nor the tag is present.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_DIGEST_REF = re.compile(r'^(?P<tag>[^@\s]+)@(?P<digest>sha256:[0-9a-f]{64})$')
PINS_FILE = 'digest-pins.json'
MANIFEST_FILE = 'package-manifest.json'

ACCEPTED = ('resolved', 'attested', 'unattested')


def split(ref):
    """``(tag_ref, digest)`` for ``repo:tag@sha256:...``; ``None`` otherwise, and
    ``None`` for a digest ref with no tag (nothing to fall back to)."""
    m = _DIGEST_REF.match(ref or '')
    if not m:
        return None
    tag = m.group('tag')
    if ':' not in tag.rsplit('/', 1)[-1]:
        return None
    return tag, m.group('digest')


def state_dir():
    st = os.environ.get('RAZZFAZZ_IMAGE_STATE') or os.path.join(
        os.path.expanduser('~'), '.razzfazz', 'state', 'custom-image-builds.json')
    return os.path.dirname(st)


def _load(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def trusted_ids(sdir=None):
    """``{digest_ref: image_id}``: the package manifest's ``image_ids`` over the
    kept ``digest-pins.json`` (the newer package wins)."""
    sdir = sdir or state_dir()
    out = dict((_load(os.path.join(sdir, PINS_FILE)).get('pins') or {}))
    ids = _load(os.path.join(sdir, MANIFEST_FILE)).get('image_ids') or {}
    out.update({k: v for k, v in ids.items() if split(k) and v})
    return out


def _inspect_id(ref):
    try:
        r = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    out = (r.stdout or '').strip()
    return out if r.returncode == 0 and out else None


def verdict(ref, inspect_id=_inspect_id, trusted=None):
    """How a digest-pinned ``ref`` stands on this box (see the module docstring)."""
    parts = split(ref)
    if parts is None:
        return 'resolved' if inspect_id(ref) else 'absent'
    if inspect_id(ref):
        return 'resolved'
    tag_ref, _digest = parts
    have = inspect_id(tag_ref)
    if not have:
        return 'absent'
    want = (trusted if trusted is not None else trusted_ids()).get(ref)
    if not want:
        return 'unattested'
    return 'attested' if want == have else 'mismatch'


def record(manifest_path, sdir=None):
    """Merge the digest refs of a package manifest's ``image_ids`` into
    ``digest-pins.json``. Returns how many pins the manifest carried."""
    sdir = sdir or state_dir()
    ids = _load(manifest_path).get('image_ids') or {}
    new = {k: v for k, v in ids.items() if split(k) and v}
    if not new:
        return 0
    path = os.path.join(sdir, PINS_FILE)
    pins = dict(_load(path).get('pins') or {})
    pins.update(new)
    os.makedirs(sdir, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'pins': pins}, fh, indent=2, sort_keys=True)
    return len(new)


def retag(manifest_path, inspect_id=_inspect_id, tag_image=None):
    """Give a loaded-but-untagged package image its ``repo:tag`` (#2778).

    A tar saved from a digest ref (older packages) has no RepoTags, so ``docker
    load`` leaves the image untagged and the tag-vs-id attestation sees "absent".
    For each digest ref in the manifest's ``image_ids``: when the recorded id is
    present locally and the ``repo:tag`` is absent or another id, tag the recorded
    id as ``repo:tag``. The id is the image-config hash, the same trust as
    :func:`verdict`. A tag that points elsewhere is overwritten only when the
    recorded id is here, and the line says so. Where the recorded id is not here,
    nothing is touched (a tag with another image stays a ``mismatch``).
    Returns the human lines, one per re-tag."""
    if tag_image is None:
        def tag_image(src, dst):
            r = subprocess.run(['docker', 'tag', src, dst], capture_output=True, text=True, timeout=60)
            return r.returncode == 0
    lines = []
    ids = _load(manifest_path).get('image_ids') or {}
    for ref, want in sorted(ids.items()):
        parts = split(ref)
        if parts is None or not want:
            continue
        tag_ref = parts[0]
        if inspect_id(ref):
            continue
        if inspect_id(want) != want:
            continue
        have = inspect_id(tag_ref)
        if have == want:
            continue
        if tag_image(want, tag_ref):
            if have:
                lines.append(f'  re-tagged {tag_ref} -> {want} (was {have}; the package recorded {want} for the pin)')
            else:
                lines.append(f'  re-tagged {tag_ref} -> {want} (the package image arrived untagged)')
        else:
            lines.append(f'  could not tag {want} as {tag_ref}')
    return lines


def _list_tag_refs():
    try:
        r = subprocess.run(['docker', 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}'],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    return set((r.stdout or '').split()) if r.returncode == 0 else None


def missing(manifest_path, inspect_id=_inspect_id, list_refs=_list_tag_refs, trusted=None):
    """The images a package manifest expects that this box does not hold (#2829).

    The completeness gate of a fresh offline/appliance install. A plain ref is
    present when ``docker image ls`` lists it (a tag-less ref means ``:latest``,
    #1481). A digest-pinned ``repo:tag@sha256:`` ref is judged by :func:`verdict`,
    the resolver verify-images and the upgrade use: ``docker load`` never brings
    the digest, so a plain string compare could never match it. The trust is
    this manifest's own ``image_ids`` over the kept pins (the manifest is the one
    being installed). Returns ``(ref, reason)`` pairs; raises when the manifest
    or the image list cannot be read, so the caller fails closed."""
    with open(manifest_path, encoding='utf-8') as fh:
        m = json.load(fh) or {}
    imgs = m.get('images') or m.get('expected') or []
    have = list_refs()
    if have is None:
        raise RuntimeError('docker image ls failed')
    pins = dict(trusted if trusted is not None else trusted_ids())
    pins.update({k: v for k, v in (m.get('image_ids') or {}).items() if split(k) and v})
    out = []
    for ref in imgs:
        if split(ref):
            v = verdict(ref, inspect_id=inspect_id, trusted=pins)
            if v == 'mismatch':
                out.append((ref, f'{split(ref)[0]} is another image than the package recorded for the pin'))
            elif v not in ACCEPTED:
                out.append((ref, v))
            continue
        if '@' in ref:
            # a digest ref without a tag: only the digest itself can answer
            if not inspect_id(ref):
                out.append((ref, 'absent'))
            continue
        tail = ref.rsplit('/', 1)[-1]
        key = ref if (':' in tail or '@' in tail) else ref + ':latest'
        if key not in have:
            out.append((ref, 'absent'))
    return out


def main(argv):
    if len(argv) == 2 and argv[0] == '--missing':
        try:
            rows = missing(argv[1])
        except Exception as exc:
            print(f'cannot judge {argv[1]}: {exc}', file=sys.stderr)
            return 2
        for ref, why in rows:
            print(ref if why == 'absent' else f'{ref} ({why})')
        return 0
    if len(argv) == 2 and argv[0] == '--record':
        n = record(argv[1])
        print(f'digest pins recorded: {n}')
        return 0
    if len(argv) == 2 and argv[0] == '--retag':
        for line in retag(argv[1]):
            print(line)
        return 0
    print('usage: digest_pins.py --record|--retag|--missing <expected-images.json>', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
