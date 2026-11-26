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
import re
import subprocess
import sys

try:  # import-safe both as ``app.services.image_provenance`` and stand-alone.
    from app.services import build_preflight
except Exception:  # pragma: no cover - stand-alone execution path (sys.path[0])
    import build_preflight  # type: ignore

SCHEMA = "razzfazz.custom-image-builds/v1"
VERDICTS_OK = ("this-build", "package-build")

#: #2659: the record ``source`` of a present custom image that a build run did
#: NOT build and that had no record yet. It is never judged this tree's build.
NOT_BUILT_SOURCE = "present-not-built"

#: #2659: the verdicts of every custom image, written beside the tree by each
#: ``--record`` so the Configuration Portal (which mounts the tree read-only and
#: cannot read the build record in the operator's home) can refuse to start an
#: image that is not this tree's build. See build_preflight.not_this_tree_build_images.
VERDICTS_SNAPSHOT = ".custom-image-verdicts.json"

#: Every ``reason`` a ``stale`` verdict carries. The Portal maps each to a fixed
#: operator phrase (build_preflight._STALE_PHRASES); a test keeps the two aligned.
STALE_REASONS = ("box-bound", "old-record", "not-built", "id-changed", "context-changed",
                 "package-other-context", "package-carries-no-image", "no-record")
VERDICTS_SNAPSHOT_SCHEMA = "razzfazz.custom-image-verdicts/v1"


#: #2659: one spelling per image, with an implicit ``:latest`` (defined once in
#: build_preflight, which the Portal also reads the snapshot with).
canonical_ref = build_preflight.canonical_ref


def _canonical_keys(d) -> dict:
    """``d`` re-keyed by canonical ref (an empty dict for anything else)."""
    return {canonical_ref(k): v for k, v in d.items()} if isinstance(d, dict) else {}


_STATE_REL = os.path.join(".razzfazz", "state", "custom-image-builds.json")


def default_state_path(stack_root: str | None = None) -> str:
    """The box's build record: ``$RAZZFAZZ_IMAGE_STATE``; else ``$HOME``'s
    ``~/.razzfazz/state/custom-image-builds.json`` when it exists.

    #3205: the record lives with the user who installed the stack. Under sudo
    ``$HOME`` is /root, and ``sudo rzfz status`` called every custom image of a
    fresh stick install STALE ("no build record"). So when ``$HOME`` has none:
    under sudo the invoking user's record ($SUDO_USER), and as root the record
    of the stack directory's owner, whichever exists first. Nothing found =
    ``$HOME``'s path, as before (a first build writes it there).
    """
    env = os.environ.get("RAZZFAZZ_IMAGE_STATE")
    if env:
        return env
    own = os.path.join(os.path.expanduser("~"), _STATE_REL)
    if os.path.exists(own):
        return own
    import pwd
    homes = []
    sudo_user = os.environ.get("SUDO_USER", "")
    if sudo_user and sudo_user != "root":
        try:
            homes.append(pwd.getpwnam(sudo_user).pw_dir)
        except (KeyError, OSError):
            pass
    if stack_root and os.geteuid() == 0:
        try:
            owner = os.stat(stack_root).st_uid
            if owner != 0:
                homes.append(pwd.getpwuid(owner).pw_dir)
        except (KeyError, OSError):
            pass
    for home in homes:
        cand = os.path.join(home, _STATE_REL)
        if os.path.exists(cand):
            return cand
    return own


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


def _dockerignore_rules(lines) -> list:
    """`.dockerignore` lines -> [(negated, regex)], Docker's rules: a pattern is
    anchored at the context root (`*.pyc` is the root only, `**/*.pyc` any depth),
    `*` and `?` stay within one path segment, `**` spans segments, a leading `/` is
    the root, `!` re-includes, and the last matching line wins."""
    rules = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        if neg:
            line = line[1:].strip()
        line = os.path.normpath(line.lstrip("/")).replace(os.sep, "/")
        if line in (".", ""):
            continue
        rx, i = "", 0
        while i < len(line):
            c = line[i]
            if line.startswith("**/", i):
                rx += "(?:.*/)?"; i += 3; continue
            if line.startswith("**", i):
                rx += ".*"; i += 2; continue
            if c == "*":
                rx += "[^/]*"
            elif c == "?":
                rx += "[^/]"
            elif c == "[":
                j = line.find("]", i + 1)
                if j < 0:
                    rx += re.escape(c)
                else:
                    rx += "[" + line[i + 1:j].replace("\\", "\\\\") + "]"; i = j
            else:
                rx += re.escape(c)
            i += 1
        rules.append((neg, re.compile(rx + "$")))
    return rules


def _dockerignored(rules, rel_path: str) -> bool:
    """Is `rel_path` (relative to the context, `/`-separated) left out of the build?
    A pattern that matches a parent directory takes the whole directory."""
    parts = rel_path.split("/")
    prefixes = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    excluded = False
    for neg, rx in rules:
        if any(rx.match(p) for p in prefixes):
            excluded = not neg
    return excluded


def _context_dockerignore(ctx_abs: str, df_abs: str | None):
    """The ignore file `docker build` applies: `<Dockerfile>.dockerignore` next to the
    Dockerfile (BuildKit) wins over `<context>/.dockerignore`; none -> no rules."""
    for cand in ([df_abs + ".dockerignore"] if df_abs else []) + [os.path.join(ctx_abs, ".dockerignore")]:
        if os.path.isfile(cand):
            with open(cand, encoding="utf-8", errors="replace") as fh:
                return _dockerignore_rules(fh.read().splitlines())
    return []


def _export_ignored(stack_root: str, paths) -> set | None:
    """#3397: the subset of ``paths`` (repo-relative files) that ``git archive``
    leaves out of a package: the file itself, or any directory above it, carries
    the ``export-ignore`` attribute. ``None`` when git cannot answer.

    git decides, not a parser of our own: ``git check-attr`` applies every
    ``.gitattributes`` of the tree with git's own pattern rules. A directory
    pattern (``tests/``) matches only the directory path spelled with a trailing
    slash, and a pattern without an inner slash matches at ANY depth, so
    ``tests/`` also takes ``core/common/tests/`` out of a package. Hence every
    parent directory is asked as ``<dir>/``, the way ``git archive`` walks the
    tree. The user's and the system's attribute files are switched off: a
    package's content must not depend on the host that measures it.
    """
    queries, parents = [], {}
    for p in paths:
        parts = p.split("/")
        dirs = ["/".join(parts[:n]) + "/" for n in range(1, len(parts))]
        parents[p] = dirs
        queries.append(p)
        queries.extend(dirs)
    if not queries:
        return set()
    uniq = sorted(set(queries))
    try:
        r = subprocess.run(
            ["git", "-C", stack_root, "-c", f"core.attributesFile={os.devnull}",
             "check-attr", "-z", "--stdin", "export-ignore"],
            input="\0".join(uniq) + "\0", capture_output=True, text=True, timeout=120,
            env={**os.environ, "GIT_ATTR_NOSYSTEM": "1"})
    except Exception:
        return None
    if r.returncode != 0:
        return None
    fields = r.stdout.split("\0")
    ignored = {fields[i] for i in range(0, len(fields) - 2, 3)
               if fields[i + 1] == "export-ignore" and fields[i + 2] == "set"}
    return {p for p in paths if p in ignored or any(d in ignored for d in parents[p])}


#: Build args that bound HOW an image is built, never WHAT it contains (#2560).
_NON_OUTPUT_BUILD_ARGS = frozenset({"RAZZFAZZ_BUILD_JOBS"})


#: #2818: bytecode a python run writes ON THE BOX next to the sources. Never an
#: input of our builds and never shipped by a package — and one
#: `images.cpython-314.pyc` turned the worker agent stale.
_NEVER_HASHED = re.compile(r"(?:^|/)__pycache__(?:/|$)|\.py[co]$")


def _context_spec(stack_root: str, build):
    """``(root_abs, ctx_abs, rel, df_rel, build)`` of a compose ``build:`` mapping
    (a bare string is a context path), or None when the context lies outside the
    repository."""
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
    dockerfile = build.get("dockerfile") or "Dockerfile"
    df_rel = os.path.normpath(os.path.join(rel, dockerfile)) if not os.path.isabs(dockerfile) else \
        os.path.relpath(os.path.realpath(dockerfile), root_abs)
    return root_abs, ctx_abs, rel, df_rel, build


def context_files(stack_root: str, build) -> list | None:
    """The files of THIS tree that :func:`context_digest` hashes for one service,
    in hashing order (repo-relative, present on disk). ``None`` = not computable.

    #2818: the packager records this list per image in the package manifest
    (``custom_image_context_files``); a box holding that manifest hashes exactly
    these paths, so a file the release deleted but a package upgrade left on
    disk, or one the box itself wrote, cannot make the release's image stale.
    """
    spec = _context_spec(stack_root, build)
    if spec is None:
        return None
    root_abs, ctx_abs, rel, df_rel, build = spec
    # #2757: the file set must not depend on the box's git INDEX. A box installed
    # from a package never moves git's HEAD: files the package added since then are
    # untracked there (left out of a tracked-only listing) and files it removed are
    # still indexed. The packager hashes a clean tree at the tag, where "tracked" and
    # "present and not ignored" are the same set, so: indexed OR untracked-not-ignored
    # (the shipped .gitignore decides), and only what exists on disk.
    files = _git_lines(stack_root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                       "--", rel if rel != "." else ".")
    if files is None:
        return None
    paths = sorted(p for p in "".join(files).split("\0") if p and not _NEVER_HASHED.search(p))
    # Only what `docker build` is SENT. The repo-root
    # contexts (help, start portal) otherwise hash .gsd/, .claude/, security-run/:
    # export-ignored, never shipped by a package, so a package box keeps its first
    # checkout's copies and every package read those images as stale.
    rules = _context_dockerignore(ctx_abs, None if build.get("dockerfile_inline")
                                  else os.path.join(root_abs, df_rel))
    if rules:
        ctx_prefix = "" if rel == "." else rel.rstrip("/") + "/"
        paths = [p for p in paths
                 if not (p.startswith(ctx_prefix) and _dockerignored(rules, p[len(ctx_prefix):]))]
    # #3397: nor what no package can deliver. A path `git archive` export-ignores
    # is never in a package, so the box that stages box tree + package keeps its
    # OWN copy (an older `.gitattributes`, an earlier checkout's
    # `core/common/tests/`) or has none at all (a package or stick install). If
    # the packager listed it, that copy alone read the repo-root images as built
    # from another tree. This tree's own attributes decide — they are the ones
    # `git archive` applies when this tree is packaged — so the packager's list
    # is exactly what its package carries. The box does not re-filter a
    # package's list (see context_digest): the package's digest was taken over
    # that list, and only the same list can reproduce it.
    dropped = _export_ignored(stack_root, paths)
    if dropped is None:
        return None
    if dropped:
        paths = [p for p in paths if p not in dropped]
    if df_rel not in paths and not df_rel.startswith("..") and not build.get("dockerfile_inline"):
        paths.append(df_rel)
    # Indexed but gone from disk (removed by a newer package): not in the build
    # context, and not in the packager's clean tree either.
    return [p for p in paths if os.path.isfile(os.path.join(root_abs, p))]


def _listed_paths(files) -> list | None:
    """A manifest's file list, or None unless it is a list of plain relative paths."""
    if not isinstance(files, list):
        return None
    out = []
    for p in files:
        if not isinstance(p, str) or not p or os.path.isabs(p) or ".." in p.split("/"):
            return None
        if not _NEVER_HASHED.search(p):
            out.append(p)
    return out


def context_digest(stack_root: str, build, files=None) -> str | None:
    """sha256 over what ``docker build`` is sent from THIS tree for one service.

    ``build`` is the compose-normalised ``build:`` mapping (``context``,
    ``dockerfile``, ``dockerfile_inline``, ``args``, ``target``); a bare string
    is a context path. The digest covers the CONTENT of every git-tracked file
    under the context (working tree, so an uncommitted edit counts), the
    Dockerfile when it lies outside the context, and the build args/target.
    ``None`` when the context is outside the repository or git
    cannot list it; callers must treat ``None`` as "unknown", never as a match.

    ``files`` (#2818): the release's own list for this image, from the package
    manifest. Then exactly those paths are hashed, in that order: a listed file
    missing or changed here is a difference; a file here that the list does not
    name is not the release's (a leftover of the old release, or something the
    box wrote) and does not count. The list is taken as it is, never re-filtered
    by the box's own rules (#3397): the box's ``.gitattributes`` is not the
    release's (a package never carries that file), and a digest taken over the
    packager's list is reproduced only by hashing that same list.
    """
    spec = _context_spec(stack_root, build)
    if spec is None:
        return None
    root_abs, _ctx_abs, rel, _df_rel, build = spec
    if files is not None:
        paths = _listed_paths(files)
    else:
        paths = context_files(stack_root, build)
    if paths is None:
        return None
    h = hashlib.sha256()
    h.update(rel.encode())
    for p in paths:
        full = os.path.join(root_abs, p)
        if os.path.isfile(full):
            h.update(b"F" + p.encode() + b"\0" + _file_sha(full).encode() + b"\n")
        # else: only a listed file can be absent here; the packager listed only
        # files it hashed, so its absence alone already makes the digest differ.
    dockerfile = build.get("dockerfile") or "Dockerfile"
    args = build.get("args") or {}
    if isinstance(args, dict):
        # #2560: a resource bound (how many compiler jobs), not an input of the
        # image; a value set for one run must not make the next run's verdict
        # "stale" and rebuild Moltis.
        args = {k: v for k, v in args.items() if k not in _NON_OUTPUT_BUILD_ARGS}
    h.update(json.dumps({
        "args": args,
        "target": build.get("target") or "",
        "dockerfile": dockerfile,
        "dockerfile_inline": build.get("dockerfile_inline") or "",
    }, sort_keys=True).encode())
    return h.hexdigest()


def _tree_version(stack_root: str) -> str:
    try:
        with open(os.path.join(stack_root, "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def release_context_files(stack_root: str, package_manifest: str | None = None) -> dict:
    """#2818: ``{image_ref: [paths]}`` the release's package recorded, or {}.

    Read from ``package_manifest`` or, without one, the on-box copy the last
    package load kept beside the build record — and only when that manifest is
    THIS tree's release (its ``stack_version`` equals the tree's VERSION). A
    manifest outlives its package: a tree moved on by git, or by a code-only
    package, must not be judged by an older release's file list.
    """
    path = package_manifest
    if not path:
        path = default_package_manifest_path()
        if not os.path.isfile(path):
            return {}
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh) or {}
    except Exception:
        return {}
    files = m.get("custom_image_context_files") if isinstance(m, dict) else None
    ver = str(m.get("stack_version") or "").strip() if isinstance(m, dict) else ""
    if not isinstance(files, dict) or not ver or ver != _tree_version(stack_root):
        return {}
    return _canonical_keys(files)


# --------------------------------------------------------------------------- #
# what the tree demands                                                         #
# --------------------------------------------------------------------------- #

def images_from_config(stack_root: str, config, context_files_by_ref: dict | None = None,
                       with_files: bool = False) -> list[dict]:
    """``[{service, image, custom, digest}]`` for EVERY service of a compose render.

    Unlike build_preflight's pre-build scope this does NOT drop ``gpustack*`` /
    ``model-sync*``: the missing `razzfazz-gpustack:vulkan` was the very image
    that failed scenario 03. ``custom`` ⇔ the service carries ``build:``.

    ``context_files_by_ref`` (#2818, :func:`release_context_files`): a custom
    image the release's manifest lists files for is hashed over exactly those.
    ``with_files``: also return each custom image's own ``files`` (the packager).
    """
    project = (config or {}).get("name") or ""
    listed = context_files_by_ref or {}
    out = []
    for name, svc in sorted(((config or {}).get("services") or {}).items()):
        if not isinstance(svc, dict):
            continue
        image = canonical_ref(build_preflight._resolved_image(project, name, svc))
        if not image:
            continue
        custom = "build" in svc
        row = {
            "service": name,
            "image": image,
            "custom": custom,
            "digest": context_digest(stack_root, svc["build"], listed.get(image)) if custom else None,
        }
        if with_files and custom:
            row["files"] = context_files(stack_root, svc["build"])
        out.append(row)
    return out


def demanded_images(stack_root: str, profiles: str, context_files_by_ref: dict | None = None,
                    with_files: bool = False) -> list[dict]:
    """What ``docker compose up`` will demand for ``profiles`` (comma-joined)."""
    config = build_preflight._compose_config_json(stack_root, profiles)
    if not config:
        return []
    return images_from_config(stack_root, config, context_files_by_ref, with_files)


def all_custom_images(stack_root: str, context_files_by_ref: dict | None = None,
                      with_files: bool = False) -> list[dict]:
    """Every custom image of the tree across ALL profiles (the packager's set)."""
    seen = {}
    profiles = build_preflight._all_build_profiles(stack_root)
    renders = [profiles] if profiles else []
    renders += [p for p in getattr(build_preflight, "_LLM_RUNTIME_PROFILES", ()) if p == "llm-legacy"]
    for p in renders:
        for row in demanded_images(stack_root, p, context_files_by_ref, with_files):
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


def compose_profiles(stack_root: str) -> str:
    """The COMPOSE_PROFILES a bare ``docker compose build`` in ``stack_root`` uses:
    the environment first, then the stack's ``.env`` (targeted parse, never sourced)."""
    if os.environ.get("COMPOSE_PROFILES"):
        return os.environ["COMPOSE_PROFILES"]
    try:
        with open(os.path.join(stack_root, ".env"), encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("COMPOSE_PROFILES="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict) and isinstance(st.get("images"), dict):
            # #2659: a record written under a tagless ref and one under :latest
            # are the same image; the later record wins.
            imgs: dict = {}
            for ref, rec in st["images"].items():
                key = canonical_ref(ref)
                old = imgs.get(key)
                if old is None or str((rec or {}).get("recorded_at", "")) >= str((old or {}).get("recorded_at", "")):
                    imgs[key] = rec
            st["images"] = imgs
            return st
    except Exception:
        pass
    return {"schema": SCHEMA, "images": {}}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    # #3205: root (sudo) may now write the installing user's record; keep its owner.
    if os.geteuid() == 0:
        try:
            st = os.stat(path) if os.path.exists(path) else os.stat(os.path.dirname(path) or ".")
            if st.st_uid != 0:
                os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
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
    return BOX_BOUND_IMAGES.get(ref) or _canonical_keys(BOX_BOUND_IMAGES).get(canonical_ref(ref))


def package_digests(package_manifest) -> dict:
    """``custom_image_digests`` of an offline package's expected-images.json, or {}."""
    if not package_manifest:
        return {}
    try:
        with open(package_manifest, encoding="utf-8") as fh:
            return _canonical_keys((json.load(fh) or {}).get("custom_image_digests") or {})
    except Exception:
        return {}


def package_image_ids(package_manifest) -> dict:
    """``image_ids`` of an offline package's expected-images.json, or {}."""
    if not package_manifest:
        return {}
    try:
        with open(package_manifest, encoding="utf-8") as fh:
            ids = (json.load(fh) or {}).get("image_ids") or {}
    except Exception:
        return {}
    return _canonical_keys(ids)


def record_builds(stack_root: str, state_path: str, rows=None, source: str = "built",
                  only_ids: dict | None = None, built=None, only_digests: dict | None = None) -> list[str]:
    """After a build: remember ``image id + context digest`` for the custom
    images of ``rows`` (default: all custom images of the tree) that this run
    BUILT. Returns the refs recorded.

    ``built`` (#2659) is the set of refs the run's build covered. ``None``
    means every row was built (a caller that passes exactly the build's scope).
    A present image of ``rows`` outside ``built`` keeps its record untouched;
    when it has none it gets a ``present-not-built`` record without a digest,
    which no verdict ever reads as this tree's build. The old rule stamped
    every PRESENT image, so an upgrade with a profile off stamped that
    profile's old image with the new tree and #2006 trusted it from then on
    (#2659: agent-manager crash-looped on ImportError voice_config).

    A build that was fully cached keeps its id, and recording it is correct:
    the build ran over this tree's context and produced the same content.

    #2168: ``only_ids`` (a package's ``image_ids``) restricts the recording to
    refs whose present id IS the package's — what a load from the stick just
    put there. A record from a previous install on the same box (the file
    lives outside the stack dir, no flatten touches it) is thereby replaced
    by what the package holds, and cannot outrank the stick on the next run.
    """
    rows = rows if rows is not None else all_custom_images(stack_root, release_context_files(stack_root))
    built = {canonical_ref(r) for r in built} if built is not None else None
    only_ids = _canonical_keys(only_ids) if only_ids is not None else None
    only_digests = _canonical_keys(only_digests) if only_digests else {}
    state = load_state(state_path)
    commit = _tree_commit(stack_root)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    done = []
    changed = False
    for row in rows:
        if not row.get("custom"):
            continue
        ref = canonical_ref(row["image"])
        img_id = _inspect_id(ref)
        if not img_id:
            continue
        if only_ids is not None and only_ids.get(ref) != img_id:
            continue
        # #2659/#2757: the package's id match says the image is the package's, not
        # that the package was made from THIS tree. A manifest digest of another
        # tree must not be stamped with today's digest.
        if only_ids is not None and only_digests.get(ref) and only_digests[ref] != row.get("digest"):
            continue
        if built is not None and ref not in built:
            if source == "built" and ref not in state["images"]:
                state["images"][ref] = {
                    "image_id": img_id,
                    "context_digest": None,
                    "source": NOT_BUILT_SOURCE,
                    "tree_commit": commit,
                    "recorded_at": now,
                }
                changed = True
            continue
        rec = {
            "image_id": img_id,
            "context_digest": row.get("digest"),
            "source": source,
            "tree_commit": commit,
            "recorded_at": now,
        }
        if source == "built":
            rec["built_in_run"] = True   # #2659: written by a record that knows what it built
        state["images"][ref] = rec
        done.append(ref)
        changed = True
    if changed:
        save_state(state_path, state)
    return done


def _stamped_not_built(rec: dict) -> str | None:
    """Why a ``built`` record from the old record rule cannot be trusted, or None.

    The old rule stamped every present custom image with the current tree,
    built or not, so a record without the ``built_in_run`` marker says nothing
    about what was built. No date comparison can tell a stamped image from a
    built one (an image built from an older tree after the newer commit was
    made looks the same), so every such record is distrusted. The next
    post-install rebuilds the image once (a cache hit when it was in fact
    built from this tree) and records it with the marker.
    """
    if rec.get("built_in_run") or rec.get("source", "built") != "built":
        return None
    return "the build record predates the rule that records only what a build built; rebuild to confirm"


def verdicts(stack_root: str, profiles: str, state_path: str, package_manifest: str | None = None,
             rows=None) -> list[dict]:
    """One verdict per image ``profiles`` demand — see the module docstring."""
    # #2441: no package at hand (rzfz verify-images) — ask the on-box copy the
    # last package load left beside the record, so verify judges the way init did.
    if not package_manifest and os.path.isfile(default_package_manifest_path()):
        package_manifest = default_package_manifest_path()
    if rows is None:
        rows = demanded_images(stack_root, profiles, release_context_files(stack_root, package_manifest))
    state = load_state(state_path)
    pkg = {}
    if package_manifest:
        try:
            with open(package_manifest, encoding="utf-8") as fh:
                pkg = _canonical_keys((json.load(fh) or {}).get("custom_image_digests") or {})
        except Exception:
            pkg = {}
    pkg_ids = package_image_ids(package_manifest)
    out = []
    ids: dict = {}
    for row in rows:
        ref = canonical_ref(row["image"])
        img_id = ids[ref] = _inspect_id(ref)
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
        # #2757: ... but only for the tree the manifest was made from. The on-box
        # manifest outlives the package that wrote it (a code-only package ships
        # none), so its ids keep matching the old images after the tree moved on.
        # When the manifest records a digest for the ref it must be THIS tree's.
        pkg_digest = pkg.get(ref)
        if pkg_ids and pkg_ids.get(ref) == img_id and not box_bound(ref) and (not pkg_digest or pkg_digest == digest):
            out.append({"image": ref, "kind": "custom", "verdict": "package-build",
                        "detail": "the offline package carries this exact image (id match)"})
            continue
        rec = state["images"].get(ref) or {}
        suspect = None
        if rec.get("image_id") == img_id and rec.get("context_digest") == digest:
            suspect = _stamped_not_built(rec)
            if not suspect:
                out.append({"image": ref, "kind": "custom", "verdict": "this-build",
                            "detail": f"recorded {rec.get('source', 'built')} at {rec.get('recorded_at', '?')} from {rec.get('tree_commit') or '?'}"})
                continue
        # #2757: the manifest's digest says what the packager BUILT FROM, not what
        # this box holds. A code-only package (include_images=false) names every
        # custom image and its digest yet carries none; certifying the present
        # image on that digest kept a pre-CVE-fix Paperclip "the package's build".
        # Only an image the package ships (id match above) is the package's.
        if pkg_ids and pkg_ids.get(ref) == img_id and box_bound(ref):
            reason = "box-bound"
            why = f"the offline package carries this exact image, but {box_bound(ref)} — built for the packager's box, not this one"
        elif pkg_ids and pkg_ids.get(ref) == img_id and pkg_digest and pkg_digest != digest:
            reason = "package-other-context"
            why = "the offline package that holds this image was built from an earlier tree than this one"
        elif suspect:
            reason, why = "old-record", suspect
        elif rec.get("source") == NOT_BUILT_SOURCE and rec.get("image_id") == img_id:
            reason = "not-built"
            why = (f"present, but no build of this tree produced it (seen unbuilt beside the build "
                   f"of {rec.get('tree_commit') or '?'} at {rec.get('recorded_at', '?')})")
        elif rec and rec.get("image_id") != img_id:
            reason, why = "id-changed", "image id changed since it was recorded (loaded or rebuilt elsewhere)"
        elif rec:
            reason, why = "context-changed", "build context changed since it was built"
        elif pkg.get(ref) == digest:
            reason = "package-carries-no-image"
            why = "the offline package names this image but carries no image (code-only) — the present one is not its build"
        elif pkg.get(ref):
            reason, why = "package-other-context", "the offline package built it from a different context"
        else:
            reason, why = "no-record", "no build record — adopted from the docker cache"
        out.append({"image": ref, "kind": "custom", "verdict": "stale", "detail": why, "reason": reason})
    for v in out:
        v["image_id"] = ids.get(v["image"])   # the id each verdict was made on (#2659 snapshot)
    return out


def write_verdicts_snapshot(stack_root: str, state_path: str, rows, package_manifest: str | None = None):
    """#2659: write every custom image's verdict to ``<stack_root>/.custom-image-verdicts.json``.

    The Configuration Portal runs in a container with the tree mounted
    read-only and without the build record (it lives in the operator's home),
    so it cannot judge provenance itself. Each ``--record`` (init, upgrade,
    rollback, post-install, a package load) leaves this snapshot behind; the
    Portal's enable pre-flight reads it and refuses an image the host judged
    not this tree's build while that very image id is still the one present.
    Returns the path written, or None when it could not be written.
    """
    vs = verdicts(stack_root, "", state_path, package_manifest, rows=[r for r in rows if r.get("custom")])
    path = os.path.join(stack_root, VERDICTS_SNAPSHOT)
    data = {
        "schema": VERDICTS_SNAPSHOT_SCHEMA,
        "tree_commit": _tree_commit(stack_root),
        "written_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "images": {v["image"]: {"verdict": v["verdict"], "reason": v.get("reason", ""),
                                "detail": v["detail"], "image_id": v.get("image_id")}
                   for v in vs},
    }
    try:
        save_state(path, data)
    except OSError as exc:
        print(f"provenance snapshot not written ({path}): {exc}", file=sys.stderr)
        return None
    return path


def _union_rows(*row_lists) -> list[dict]:
    seen: dict = {}
    for rows in row_lists:
        for r in rows or []:
            if r.get("custom"):
                seen.setdefault(canonical_ref(r["image"]), r)
    return [seen[k] for k in sorted(seen)]


def trusted_image_id(state_path: str, ref: str, image_id: str, package_manifest: str | None = None) -> bool:
    """#2566: may the local image ``image_id`` carry the name ``ref`` again?

    Only when it IS the image this box's build record holds for ``ref`` (#2006),
    or the image the offline package that last loaded here carries under
    ``ref`` (its ``image_ids``, #2168). A hub-tagged copy of any other build —
    pulled from a hub that has moved on, or pushed by another tree — is not
    adopted under the local name: an id match is the pin, a tag is not.
    """
    if not image_id:
        return False
    rec = (load_state(state_path).get("images") or {}).get(ref) or {}
    if rec.get("image_id") == image_id:
        return True
    if not package_manifest and os.path.isfile(default_package_manifest_path()):
        package_manifest = default_package_manifest_path()
    return package_image_ids(package_manifest).get(ref) == image_id


def _current_digest(stack_root: str, ref: str, profiles: str = "") -> str | None:
    """This tree's build-context digest for ``ref`` NOW (args, uncommitted
    edits included), from the same render the verdicts use; None = unknown."""
    listed = release_context_files(stack_root)   # #2818: the same file set the record was made over
    rows = demanded_images(stack_root, profiles, listed) if profiles else all_custom_images(stack_root, listed)
    for row in rows:
        if row.get("custom") and row.get("image") == ref:
            return row.get("digest")
    return None


def running_is_this_build(stack_root: str, state_path: str, ref: str, running_id: str,
                          profiles: str = "") -> bool:
    """#2566: True when the container of ``ref``'s service runs exactly the image
    the build record holds for ``ref``, that record was written from this tree's
    commit, AND the record's build-context digest is this tree's digest now.
    A refresh must not rebuild then: the container already runs this tree's
    build, and a rebuild only mints an unused image (measured on a fleet
    master). The digest is what a commit does not show — a changed build arg
    (OPENCODE_VERSION, DIFY_VERSION, RAZZFAZZ_VERSION, …) or an uncommitted edit
    in the context at the same commit — and either one must still rebuild. A
    digest that cannot be computed is never a match."""
    rec = (load_state(state_path).get("images") or {}).get(ref) or {}
    commit = _tree_commit(stack_root)
    if not (running_id and commit and rec.get("image_id") == running_id
            and rec.get("tree_commit") == commit):
        return False
    digest = _current_digest(stack_root, ref, profiles)
    return bool(digest and rec.get("context_digest") == digest)


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
    dp = getattr(build_preflight, "digest_pins", None)
    rows = []
    for ref, want in sorted(ids.items()):
        have = _inspect_id(ref)
        # #2829: ``docker load`` drops the digest, so a ``repo:tag@sha256:`` ref
        # never inspects on a package box. Same rule as digest_pins.verdict: the
        # tag's image is the pinned image when its id is the one recorded here.
        if not have and dp is not None and dp.split(ref):
            have = _inspect_id(dp.split(ref)[0])
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
    ap.add_argument("--built", default=None,
                    help="--record: comma-joined image refs this run built (default: every custom image "
                         "of --profiles, or of the COMPOSE_PROFILES the build used) (#2659)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--digests", action="store_true", help="JSON {image: digest} over all custom images")
    mode.add_argument("--record", action="store_true",
                      help="record image id + digest for the custom images this run built (#2659)")
    mode.add_argument("--verdicts", action="store_true", help="TSV image<TAB>kind<TAB>verdict<TAB>detail")
    mode.add_argument("--blocking", action="store_true", help="TSV of custom images that are missing or stale (exit 1 if any)")
    mode.add_argument("--needs-loading", action="store_true",
                      help="TSV image<TAB>reason of package images absent or present under another id; exit 0 none, 1 some, 2 undecidable (no image_ids)")
    mode.add_argument("--trusted-id", metavar="REF",
                      help="exit 0 when --image-id is the recorded (or package) image of REF (#2566)")
    mode.add_argument("--verdict-of", metavar="REF",
                      help="the verdicts TSV row of REF only; exit 1 when REF is not in the render (#2566)")
    mode.add_argument("--running-is-this-build", metavar="REF",
                      help="exit 0 when --running-id is this tree's recorded build of REF (#2566)")
    mode.add_argument("--state-path", action="store_true",
                    help="print the build record this box reads (#3205: under sudo the installing user's)")
    ap.add_argument("--image-id", default="")
    ap.add_argument("--running-id", default="")
    a = ap.parse_args(argv)
    if a.state_path:
        print(default_state_path(a.stack_root))
        return 0
    state = a.state or default_state_path(a.stack_root)
    if a.trusted_id:
        return 0 if trusted_image_id(state, a.trusted_id, a.image_id, a.package_manifest) else 1
    if a.running_is_this_build:
        return 0 if running_is_this_build(a.stack_root, state, a.running_is_this_build, a.running_id,
                                          a.profiles) else 1
    if a.verdict_of:
        # #2566: one ref's verdict (post-install re-checks a re-tagged image).
        for v in verdicts(a.stack_root, a.profiles, state, a.package_manifest):
            if v["image"] == a.verdict_of:
                print(f"{v['image']}\t{v['kind']}\t{v['verdict']}\t{v['detail']}")
                return 0
        return 1
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
        # #2818: record over the same file set every later verdict hashes —
        # the release's list when this tree is that release, else the tree's.
        listed = release_context_files(a.stack_root, a.package_manifest)
        if a.source == "package":
            rows = demanded_images(a.stack_root, a.profiles, listed) if a.profiles \
                else all_custom_images(a.stack_root, listed)
            only = package_image_ids(a.package_manifest) if a.package_manifest else None
            if a.package_manifest:
                persist_package_manifest(a.package_manifest)   # #2441: verify-images reads it later
            done = record_builds(a.stack_root, state, rows=rows, source=a.source, only_ids=only,
                                 only_digests=package_digests(a.package_manifest))
            snap_rows = rows
        else:
            # #2659: record what THIS run built, never every present image. The
            # build's scope is --profiles (default: the COMPOSE_PROFILES a bare
            # `docker compose build` here used), narrowed by --built when the
            # caller built single services. Every other present custom image
            # keeps its record (or gets a present-not-built one).
            scope = [r for r in demanded_images(a.stack_root, a.profiles or compose_profiles(a.stack_root),
                                                listed)
                     if r["custom"]]
            built = {r["image"] for r in scope} if a.built is None else \
                {x for x in a.built.split(",") if x}
            snap_rows = _union_rows(all_custom_images(a.stack_root, listed), scope)
            done = record_builds(a.stack_root, state, rows=snap_rows, source=a.source, built=built)
        for ref in done:
            print(ref)
        write_verdicts_snapshot(a.stack_root, state, snap_rows,
                                a.package_manifest if a.source == "package" else None)
        return 0
    vs = verdicts(a.stack_root, a.profiles, state, a.package_manifest)
    if a.blocking:
        vs = not_from_this_tree(vs)
    for v in vs:
        print(f"{v['image']}\t{v['kind']}\t{v['verdict']}\t{v['detail']}")
    return 1 if (a.blocking and vs) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
