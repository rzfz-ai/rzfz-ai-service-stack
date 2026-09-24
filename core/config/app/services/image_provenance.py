# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Custom-image provenance — "present" must mean "built from THIS tree" (#2006 part 2).

Three measured failures on 2026-09-14 (0.175, rc2 sweep) share one cause: every
decision about a custom image asked only whether an image with the right NAME
exists locally.

  * `rzfz init` reused a cognee image nine days older than the Dockerfile fix
    under acceptance; the failure was indistinguishable from the defect (#2105).
  * The appliance loader judged the loaded set COMPLETE against the PACKAGE's
    own manifest (ga.15 names) and skipped the build; compose then demanded the
    renamed `razzfazz-gpustack:vulkan` and 27 containers stayed in `created`.
  * The agents' images were present at the tags of the previous release
    (`v2026.8.3` where the tree pins `v2026.8.27`); a bare `docker compose
    build` under a narrow `COMPOSE_PROFILES` built nothing for them and exited 0.

This module gives every custom image a **build-context digest** — a hash of the
tracked files of its build context plus its Dockerfile and build arguments, i.e.
of what `docker build` would be sent from this tree — and keeps a small state
file of what was built (image id + digest). From these two facts a **verdict**
per image the tree demands:

  this-build     present, recorded here as built from a context with this digest
  package-build  present, the offline package that carried it records this digest
  stale          present, but nothing says it came from this tree's context
  missing        not present at all
  unknown        present, and this tree's digest cannot be computed (no git)
  present        a pulled (non-custom) image that is there
  (missing)      a pulled image that is not

`rzfz init` decides the appliance skip from these verdicts, `post-install`'s
pre-build rebuilds what is stale instead of skipping what is present, and the
packager writes the digests into `expected-images.json` so a package carries
proof of what it contains.

Fail-open like its siblings: when docker or compose cannot answer, callers get
empty lists, never an exception — and a digest that cannot be computed yields
`unknown`, never `this-build`.

Import-safe as ``app.services.image_provenance`` and runnable stand-alone
(``python3 image_provenance.py --stack-root X --verdicts --profiles a,b``).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import sys

try:  # import-safe both as ``app.services.image_provenance`` and stand-alone.
    from app.services import build_preflight
except Exception:  # pragma: no cover - stand-alone execution path (sys.path[0])
    import build_preflight  # type: ignore

SCHEMA = "razzfazz.custom-image-builds/v1"
VERDICTS_OK = ("this-build", "package-build")


def default_state_path() -> str:
    """``$RAZZFAZZ_IMAGE_STATE`` or ``~/.razzfazz/state/custom-image-builds.json``."""
    return os.environ.get("RAZZFAZZ_IMAGE_STATE") or os.path.join(
        os.path.expanduser("~"), ".razzfazz", "state", "custom-image-builds.json")


def default_package_manifest_path() -> str:
    """The on-box copy of the offline package's ``expected-images.json`` that
    last loaded images here — beside the build record, outside the stack dir.

    #2441: ``rzfz verify-images`` has no package at hand, so without this copy
    it falls through to the build record alone, and a record written before
    ``.env`` carried ``RAZZFAZZ_VERSION`` (a build arg) calls a byte-identical
    package image "stale" while init, which had the manifest, passed it.
    """
    return os.path.join(os.path.dirname(default_state_path()), "package-manifest.json")


def persist_package_manifest(package_manifest: str) -> str | None:
    """Keep a copy of the package manifest beside the build record; returns the
    path written, or None when there was nothing readable to copy."""
    try:
        with open(package_manifest, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    dest = default_package_manifest_path()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    return dest


# --------------------------------------------------------------------------- #
# build-context digest                                                          #
# --------------------------------------------------------------------------- #

def _git_lines(stack_root, *args):
    try:
        r = subprocess.run(["git", "-C", stack_root, *args],
                           capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return [l for l in r.stdout.splitlines() if l]


def _file_sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def context_digest(stack_root: str, build) -> str | None:
    """sha256 over what ``docker build`` is sent from THIS tree for one service.

    ``build`` is the compose-normalised ``build:`` mapping (``context``,
    ``dockerfile``, ``dockerfile_inline``, ``args``, ``target``); a bare string
    is a context path. The digest covers the CONTENT of every git-tracked file
    under the context (working tree, so an uncommitted edit counts), the
    Dockerfile when it lies outside the context, and the build args/target.
    Untracked files are not part of a release tree and are ignored — stated,
    not hidden. ``None`` when the context is outside the repository or git
    cannot list it; callers must treat ``None`` as "unknown", never as a match.
    """
    if isinstance(build, str):
        build = {"context": build}
    if not isinstance(build, dict):
        return None
    ctx = build.get("context") or "."
    ctx_abs = ctx if os.path.isabs(ctx) else os.path.normpath(os.path.join(stack_root, ctx))
    root_abs = os.path.realpath(stack_root)
    rel = os.path.relpath(os.path.realpath(ctx_abs), root_abs)
    if rel.startswith(".."):
        return None
    files = _git_lines(stack_root, "ls-files", "-z", "--", rel if rel != "." else ".")
    if files is None:
        return None
    paths = sorted(p for p in "".join(files).split("\0") if p)
    dockerfile = build.get("dockerfile") or "Dockerfile"
    df_rel = os.path.normpath(os.path.join(rel, dockerfile)) if not os.path.isabs(dockerfile) else \
        os.path.relpath(os.path.realpath(dockerfile), root_abs)
    if df_rel not in paths and not df_rel.startswith("..") and not build.get("dockerfile_inline"):
        paths.append(df_rel)
    h = hashlib.sha256()
    h.update(rel.encode())
    for p in paths:
        full = os.path.join(root_abs, p)
        if os.path.isfile(full):
            h.update(b"F" + p.encode() + b"\0" + _file_sha(full).encode() + b"\n")
        else:
            h.update(b"D" + p.encode() + b"\n")  # tracked but deleted in the working tree
    h.update(json.dumps({
        "args": build.get("args") or {},
        "target": build.get("target") or "",
        "dockerfile": dockerfile,
        "dockerfile_inline": build.get("dockerfile_inline") or "",
    }, sort_keys=True).encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# what the tree demands                                                         #
# --------------------------------------------------------------------------- #

def images_from_config(stack_root: str, config) -> list[dict]:
    """``[{service, image, custom, digest}]`` for EVERY service of a compose render.

    Unlike build_preflight's pre-build scope this does NOT drop ``gpustack*`` /
    ``model-sync*``: the missing `razzfazz-gpustack:vulkan` was the very image
    that failed scenario 03. ``custom`` ⇔ the service carries ``build:``.
    """
    project = (config or {}).get("name") or ""
    out = []
    for name, svc in sorted(((config or {}).get("services") or {}).items()):
        if not isinstance(svc, dict):
            continue
        image = build_preflight._resolved_image(project, name, svc)
        if not image:
            continue
        custom = "build" in svc
        out.append({
            "service": name,
            "image": image,
            "custom": custom,
            "digest": context_digest(stack_root, svc["build"]) if custom else None,
        })
    return out


def demanded_images(stack_root: str, profiles: str) -> list[dict]:
    """What ``docker compose up`` will demand for ``profiles`` (comma-joined)."""
    config = build_preflight._compose_config_json(stack_root, profiles)
    if not config:
        return []
    return images_from_config(stack_root, config)


def all_custom_images(stack_root: str) -> list[dict]:
    """Every custom image of the tree across ALL profiles (the packager's set)."""
    seen = {}
    profiles = build_preflight._all_build_profiles(stack_root)
    renders = [profiles] if profiles else []
    renders += [p for p in getattr(build_preflight, "_LLM_RUNTIME_PROFILES", ()) if p == "llm-legacy"]
    for p in renders:
        for row in demanded_images(stack_root, p):
            if row["custom"]:
                seen[row["image"]] = row
    return [seen[k] for k in sorted(seen)]


# --------------------------------------------------------------------------- #
# docker + state                                                                #
# --------------------------------------------------------------------------- #

def _inspect_id(image_ref: str) -> str | None:
    """The local image id for ``image_ref``; ``None`` when absent. Patched in tests."""
    try:
        r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image_ref],
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict) and isinstance(st.get("images"), dict):
            return st
    except Exception:
        pass
    return {"schema": SCHEMA, "images": {}}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _tree_commit(stack_root: str) -> str:
    lines = _git_lines(stack_root, "rev-parse", "--short=12", "HEAD")
    return lines[0] if lines else ""


#: #2174: images whose BUILD compiles the box's own configuration in. For
#: these "the package carries this exact image" is not "this image is right for
#: this box": dify-web's Next.js bundle bakes NEXT_PUBLIC_*_URL = the packager's
#: domain, and no runtime variable moves it, so a generic stick carries a copy
#: that is wrong for every box on another domain. They stay judged by digest
#: and record (the digest folds the build args, so the domain difference shows
#: as stale) and are never accepted by image id alone. The consistency guard
#: tests/unit/razzfazz-config/test_2006_present_means_built_from_this_tree.py
#: scans every compose build's args for domain interpolation, so a new one
#: must be declared here or the gate goes red.
BOX_BOUND_IMAGES: dict = {
    # empty since #2174's fix: dify-web no longer takes the domain at build.
    # Declare an image here the day a build compiles box configuration in again;
    # the consistency guard scans every compose build's args for it.
}


def box_bound(ref: str) -> str | None:
    """The reason an image may not be accepted from a package by id, or None."""
    return BOX_BOUND_IMAGES.get(ref)


def package_image_ids(package_manifest) -> dict:
    """``image_ids`` of an offline package's expected-images.json, or {}."""
    if not package_manifest:
        return {}
    try:
        with open(package_manifest, encoding="utf-8") as fh:
            ids = (json.load(fh) or {}).get("image_ids") or {}
    except Exception:
        return {}
    return ids if isinstance(ids, dict) else {}


def record_builds(stack_root: str, state_path: str, rows=None, source: str = "built",
                  only_ids: dict | None = None) -> list[str]:
    """After a build: remember ``image id + context digest`` for every custom
    image of ``rows`` (default: all custom images of the tree) that is present.

    Recording asserts nothing about the build's success — it states which image
    id stands behind the tag NOW and which tree digest was current; a build that
    was fully cached keeps its id, and that is correct: same content, same
    digest. Returns the refs recorded.

    #2168: ``only_ids`` (a package's ``image_ids``) restricts the recording to
    refs whose present id IS the package's — what a load from the stick just
    put there. A record from a previous install on the same box (the file
    lives outside the stack dir, no flatten touches it) is thereby replaced
    by what the package holds, and cannot outrank the stick on the next run.
    """
    rows = rows if rows is not None else all_custom_images(stack_root)
    state = load_state(state_path)
    commit = _tree_commit(stack_root)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    done = []
    for row in rows:
        if not row.get("custom"):
            continue
        img_id = _inspect_id(row["image"])
        if not img_id:
            continue
        if only_ids is not None and only_ids.get(row["image"]) != img_id:
            continue
        state["images"][row["image"]] = {
            "image_id": img_id,
            "context_digest": row.get("digest"),
            "source": source,
            "tree_commit": commit,
            "recorded_at": now,
        }
        done.append(row["image"])
    if done:
        save_state(state_path, state)
    return done


def verdicts(stack_root: str, profiles: str, state_path: str, package_manifest: str | None = None,
             rows=None) -> list[dict]:
    """One verdict per image ``profiles`` demand — see the module docstring."""
    rows = rows if rows is not None else demanded_images(stack_root, profiles)
    state = load_state(state_path)
    pkg = {}
    # #2441: no package at hand (rzfz verify-images) — ask the on-box copy the
    # last package load left beside the record, so verify judges the way init did.
    if not package_manifest and os.path.isfile(default_package_manifest_path()):
        package_manifest = default_package_manifest_path()
    if package_manifest:
        try:
            with open(package_manifest, encoding="utf-8") as fh:
                pkg = (json.load(fh) or {}).get("custom_image_digests") or {}
        except Exception:
            pkg = {}
    pkg_ids = package_image_ids(package_manifest)
    out = []
    for row in rows:
        ref = row["image"]
        img_id = _inspect_id(ref)
        if not row.get("custom"):
            out.append({"image": ref, "kind": "pulled",
                        "verdict": "present" if img_id else "missing", "detail": ""})
            continue
        digest = row.get("digest")
        if not img_id:
            out.append({"image": ref, "kind": "custom", "verdict": "missing", "detail": "no local image"})
            continue
        if not digest:
            out.append({"image": ref, "kind": "custom", "verdict": "unknown",
                        "detail": "this tree's build-context digest is not computable (no git?) — presence only"})
            continue
        # #2168 (journey B, 0.175): the package's images ARE the package's build,
        # by construction — the packager saved them from its tree and recorded
        # each exact id. A present image whose id is the package's id is
        # package-build, whatever a leftover record from a previous install on
        # this box says and whatever a build-arg-dependent digest computes. Two
        # correct images were judged "stale" because a record from the box's
        # earlier install disagreed with the stick, and a correct 52 GB package
        # was refused as INCOMPLETE with zero load failures.
        if pkg_ids and pkg_ids.get(ref) == img_id and not box_bound(ref):
            out.append({"image": ref, "kind": "custom", "verdict": "package-build",
                        "detail": "the offline package carries this exact image (id match)"})
            continue
        rec = state["images"].get(ref) or {}
        if rec.get("image_id") == img_id and rec.get("context_digest") == digest:
            out.append({"image": ref, "kind": "custom", "verdict": "this-build",
                        "detail": f"recorded {rec.get('source', 'built')} at {rec.get('recorded_at', '?')} from {rec.get('tree_commit') or '?'}"})
            continue
        if pkg.get(ref) == digest:
            out.append({"image": ref, "kind": "custom", "verdict": "package-build",
                        "detail": "the offline package records this tree's context digest for it"})
            continue
        if pkg_ids and pkg_ids.get(ref) == img_id and box_bound(ref):
            why = f"the offline package carries this exact image, but {box_bound(ref)} — built for the packager's box, not this one"
        elif rec:
            why = "image id changed since it was recorded (loaded or rebuilt elsewhere)" \
                if rec.get("image_id") != img_id else "build context changed since it was built"
        elif pkg.get(ref):
            why = "the offline package built it from a different context"
        else:
            why = "no build record — adopted from the docker cache"
        out.append({"image": ref, "kind": "custom", "verdict": "stale", "detail": why})
    return out


def package_needs_loading(manifest_path: str) -> tuple[list[dict], bool]:
    """Does an offline package's image payload need loading at all? (#271, #2120)

    The package's ``expected-images.json`` carries ``image_ids`` — the exact
    image id (``sha256:…``) ``docker save`` wrote for every ref (written by the
    packager). A box whose local image under that ref has the SAME id holds
    that archive's content already; extracting and loading it is pure cost
    (155 GB and 8+ minutes measured on 0.175 for zero loaded images).

    Returns ``(rows, decidable)``: ``rows`` names every ref that is absent or
    present under a DIFFERENT id (``docker load`` would change it), ``decidable``
    is False when the manifest carries no ``image_ids`` (a package built before
    this field) — then nothing can be skipped and the caller must extract as
    before. A tag-only comparison is deliberately NOT used: a tag says which
    name is present, not which build (#2006, #2105).
    """
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            m = json.load(fh) or {}
    except Exception:
        return [], False
    ids = m.get("image_ids") or {}
    if not isinstance(ids, dict) or not ids:
        return [], False
    rows = []
    for ref, want in sorted(ids.items()):
        have = _inspect_id(ref)
        if not have:
            rows.append({"image": ref, "reason": "absent"})
        elif have != want:
            rows.append({"image": ref, "reason": f"present under a different image id ({have[:19]}… vs package {str(want)[:19]}…)"})
    return rows, True


def not_from_this_tree(vs) -> list[dict]:
    """The verdicts that must block a skip: custom images missing or stale."""
    return [v for v in vs if v["kind"] == "custom" and v["verdict"] in ("missing", "stale")]


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="image_provenance",
                                 description="custom-image provenance: digests, build records, verdicts (#2006)")
    ap.add_argument("--stack-root", default=os.getcwd())
    ap.add_argument("--profiles", default="", help="comma-joined COMPOSE_PROFILES the render is scoped to")
    ap.add_argument("--state", default=None, help="state file (default: $RAZZFAZZ_IMAGE_STATE or ~/.razzfazz/state/custom-image-builds.json)")
    ap.add_argument("--package-manifest", default=None, help="an offline package's expected-images.json")
    ap.add_argument("--source", default="built", help="record source label (built|package)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--digests", action="store_true", help="JSON {image: digest} over all custom images")
    mode.add_argument("--record", action="store_true", help="record image id + digest for present custom images")
    mode.add_argument("--verdicts", action="store_true", help="TSV image<TAB>kind<TAB>verdict<TAB>detail")
    mode.add_argument("--blocking", action="store_true", help="TSV of custom images that are missing or stale (exit 1 if any)")
    mode.add_argument("--needs-loading", action="store_true",
                      help="TSV image<TAB>reason of package images absent or present under another id; exit 0 none, 1 some, 2 undecidable (no image_ids)")
    a = ap.parse_args(argv)
    state = a.state or default_state_path()
    if a.digests:
        print(json.dumps({r["image"]: r["digest"] for r in all_custom_images(a.stack_root)}, indent=2, sort_keys=True))
        return 0
    if a.needs_loading:
        if not a.package_manifest:
            print("--needs-loading requires --package-manifest", file=sys.stderr)
            return 2
        rows, decidable = package_needs_loading(a.package_manifest)
        if not decidable:
            print("(package manifest carries no image_ids — undecidable, extract as before)", file=sys.stderr)
            return 2
        for r in rows:
            print(f"{r['image']}\t{r['reason']}")
        return 1 if rows else 0
    if a.record:
        rows = demanded_images(a.stack_root, a.profiles) if a.profiles else None
        only = package_image_ids(a.package_manifest) if (a.source == "package" and a.package_manifest) else None
        if a.source == "package" and a.package_manifest:
            persist_package_manifest(a.package_manifest)   # #2441: verify-images reads it later
        for ref in record_builds(a.stack_root, state, rows=rows, source=a.source, only_ids=only):
            print(ref)
        return 0
    vs = verdicts(a.stack_root, a.profiles, state, a.package_manifest)
    if a.blocking:
        vs = not_from_this_tree(vs)
    for v in vs:
        print(f"{v['image']}\t{v['kind']}\t{v['verdict']}\t{v['detail']}")
    return 1 if (a.blocking and vs) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
