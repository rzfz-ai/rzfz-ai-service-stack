#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The version pins a box's .env carries once it is upgraded to a release tree.

An upgrade moves every `*_VERSION` pin of the box's .env to the release's value in
two steps: the shipped `config/.env.example` (the pin sync inside migrate_env) and
then `config/manifests/versions.json` (the manifest reconcile), each one only for a
key the .env already holds with a value. Everything else in the .env (profiles,
hardware, network mode, the compose file chain) stays the box's own.

Two callers need that answer BEFORE the upgrade has run, and they must not
disagree with each other or with the upgrade:

  * the upgrade's offline image check applies it to its scratch copy of the .env;
  * the offline packager applies it to the .env it enumerates, builds, pulls and
    saves images from. With the box's own pins it saved the images of the release
    the box was leaving, and the upgrade it was built for stopped on missing
    images.

The stateful pins the upgrade never lowers (a database that migrated forward) are
the upgrade's own decision, made in cli/upgrade.sh; this module only says what the
release wants.

Commands:
  plan   --tree T --env E   one line per pin to move: KEY<TAB>HAVE<TAB>WANT
  check  --tree T --env E --images-dir D
         every image the release runs for the .env's enabled profiles whose tag
         is a version pin must be saved in D at the release's pin. Prints one
         line per gap (MISSING <ref> ...) and exits 1 when there is any.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

ENV_EXAMPLE = os.path.join("config", ".env.example")
VERSIONS_JSON = os.path.join("config", "manifests", "versions.json")

_LINE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_PIN_KEY = re.compile(r"^[A-Z0-9_]+_VERSION$")
# image: repo:${KEY} / repo:${KEY:-default} / repo:${KEY-default}, optionally @digest
_IMAGE_PIN = re.compile(r"^(?P<repo>[^\s:@${}]+(?::\d+/[^\s:@${}]+)?):\$\{(?P<key>[A-Z0-9_]+)(?::?-(?P<default>[^}]*))?\}(?P<rest>\S*)$")


def _value(raw: str) -> str:
    """The value as scripts/lib.sh read_env_value returns it."""
    raw = raw.rstrip("\r")
    m = re.match(r'^"(.*)"[ \t]*(#.*)?$', raw)
    if m:
        return m.group(1)
    m = re.match(r"^'(.*)'[ \t]*(#.*)?$", raw)
    if m:
        return m.group(1)
    return re.sub(r"[ \t]+$", "", re.sub(r"[ \t]+#.*$", "", raw))


def read_env(path: str) -> dict:
    """``{key: value}``, the FIRST assignment of each key (read_env_value's rule)."""
    out: dict = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _LINE.match(line.rstrip("\n"))
                if m and m.group(1) not in out:
                    out[m.group(1)] = _value(m.group(2))
    except OSError:
        pass
    return out


def release_pins(tree: str) -> dict:
    """``{key: value}`` the release sets: .env.example's non-empty `*_VERSION`
    values, then versions.json's `env_var`/`current` over them (the upgrade runs
    the manifest reconcile after the sync, so the manifest has the last word)."""
    pins = {k: v for k, v in read_env(os.path.join(tree, ENV_EXAMPLE)).items()
            if _PIN_KEY.match(k) and v}
    try:
        with open(os.path.join(tree, VERSIONS_JSON), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        manifest = {}
    seen = set()
    for section in ("images", "hardcoded"):
        for entry in (manifest.get(section) or {}).values():
            key, current = (entry or {}).get("env_var"), (entry or {}).get("current")
            if not key or not current or key in seen:
                continue
            seen.add(key)
            pins[key] = str(current)
    return pins


def plan(tree: str, env_path: str) -> list:
    """``[(key, have, want)]``: every pin the .env holds with a value that is not
    the release's. A key the .env does not hold is left alone, as the upgrade
    leaves it (an `add` rule or the compose default decides it)."""
    have = read_env(env_path)
    return [(k, have[k], want) for k, want in sorted(release_pins(tree).items(), key=lambda kv: kv[0])  # by key name; versions are never compared
            if have.get(k) and have[k] != want]


# ---- check -------------------------------------------------------------------

class _Reset:
    """A compose `!reset` value: the key is removed from the merged service."""

    def __repr__(self) -> str:
        return "!reset"


RESET = _Reset()


class _Override:
    """A compose `!override` value: replaces the earlier file's value, no merge."""

    def __init__(self, value) -> None:
        self.value = value


def _loader():
    """A safe YAML loader that knows compose's merge tags. Every installed box
    carries compose.no-build.yml (`build: !reset null`) in its COMPOSE_FILE, and a
    plain safe loader refuses the whole file on the first tag."""
    import yaml

    class ComposeLoader(yaml.SafeLoader):
        pass

    def _value(loader, node):
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_scalar(node)

    ComposeLoader.add_constructor("!reset", lambda _l, _n: RESET)
    ComposeLoader.add_constructor("!override", lambda l, n: _Override(_value(l, n)))
    return ComposeLoader


def _merge(later):
    """One key of a service after an overlay: `!reset` drops it, `!override` and
    a plain value replace it (the image and profiles are replaced either way)."""
    if later is RESET:
        return None
    if isinstance(later, _Override):
        return later.value
    return later


def _compose_files(tree: str, env: dict) -> list:
    """``[(rel, doc)]`` in merge order: compose.yml with everything it includes
    (depth first), then the .env's COMPOSE_FILE chain, each with its includes:
    the files a render of this .env reads."""
    import yaml  # the host CLI's own dependency; imported here so `plan` never needs it

    loader = _loader()
    seen, out = set(), []

    def expand(rel: str) -> None:
        rel = os.path.normpath(rel)
        path = os.path.join(tree, rel)
        if rel in seen or not os.path.isfile(path):
            return
        seen.add(rel)
        with open(path, encoding="utf-8") as fh:
            doc = yaml.load(fh, Loader=loader) or {}  # noqa: S506 - a SafeLoader subclass
        out.append((rel, doc))
        for inc in doc.get("include") or []:
            p = inc.get("path") if isinstance(inc, dict) else inc
            for one in (p if isinstance(p, list) else [p]):
                if one:
                    expand(os.path.join(os.path.dirname(rel), one))

    for rel in ["compose.yml"] + [p for p in (env.get("COMPOSE_FILE") or "").split(":") if p.strip()]:
        expand(rel)
    return out


def _services(tree: str, env: dict) -> dict:
    """``{service: (image, profiles)}`` after the merge: a later file's image or
    profiles replace an earlier one's, as compose merges an overlay. `!reset` on
    a key removes it, on a service removes the service; `!override` on a service
    replaces the whole definition."""
    merged: dict = {}
    for _rel, doc in _compose_files(tree, env):
        services = doc.get("services")
        if isinstance(services, _Override):
            merged, services = {}, services.value
        if services is RESET:
            merged, services = {}, None
        for name, svc in (services or {}).items():
            if svc is RESET:
                merged.pop(name, None)
                continue
            image, profiles = merged.get(name, (None, None))
            if isinstance(svc, _Override):
                svc, image, profiles = svc.value or {}, None, None
            svc = svc or {}
            if "image" in svc:
                image = _merge(svc["image"])
            if "profiles" in svc:
                profiles = _merge(svc["profiles"])
            merged[name] = (image, profiles)
    return merged


def _enabled(profiles, enabled: set) -> bool:
    return not profiles or bool(set(profiles) & enabled)


def _saved(images_dir: str, ref: str) -> bool:
    """A tar for ``ref`` (or for ``ref@<digest>``) in the package's images/ dir:
    the packager names each tar after its ref with `/` and `:` made `_`."""
    stem = ref.replace("/", "_").replace(":", "_")
    for path in glob.glob(glob.escape(os.path.join(images_dir, stem)) + "*.tar"):
        tail = os.path.basename(path)[len(stem):]
        if tail == ".tar" or tail.startswith("@"):
            return True
    return False


def needed(tree: str, env_path: str) -> list:
    """``[(ref, key, service)]``: the version-pinned images the release runs for
    the .env's enabled profiles, at the release's pin."""
    env = read_env(env_path)
    pins = release_pins(tree)
    enabled = {p.strip() for p in (env.get("COMPOSE_PROFILES") or "").split(",") if p.strip()}
    out = {}
    for name, (image, profiles) in sorted(_services(tree, env).items()):
        m = _IMAGE_PIN.match(str(image or "").strip())
        if not m or not _PIN_KEY.match(m.group("key")) or not _enabled(profiles, enabled):
            continue
        key = m.group("key")
        # The release's pin; for a pin the release does not set, what the
        # upgraded .env still says, else the compose default.
        want = pins.get(key) or env.get(key) or (m.group("default") or "")
        if not want:
            continue
        ref = f"{m.group('repo')}:{want}"
        out.setdefault(ref, (ref, key, name))
    return sorted(out.values())


def check(tree: str, env_path: str, images_dir: str) -> int:
    try:
        rows = needed(tree, env_path)
    except Exception as exc:   # an unreadable compose file is a gap, never a pass
        print(f"CANNOT CHECK the release's version-pinned images: {type(exc).__name__}: {exc}")
        return 1
    if not rows:
        print("CANNOT CHECK the release's version-pinned images: none found in the compose files")
        return 1
    gaps = [f"MISSING {ref} (the release's {key} for service {svc})"
            for ref, key, svc in rows if not _saved(images_dir, ref)]
    print("\n".join(gaps))
    return 1 if gaps else 0


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="release_pins", description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--tree", required=True)
    p.add_argument("--env", required=True)
    c = sub.add_parser("check")
    c.add_argument("--tree", required=True)
    c.add_argument("--env", required=True)
    c.add_argument("--images-dir", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        for key, have, want in plan(a.tree, a.env):
            print(f"{key}\t{have}\t{want}")
        return 0
    return check(a.tree, a.env, a.images_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
