#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Will the pre-restart image check pass on the release this upgrade installs?

An offline box cannot build or pull, so the upgrade's pre-restart check refuses
when a custom image is not the new tree's build. That check needs the NEW tree,
which used to be on disk only after the code switch, the .env migration and the
database and secret reconcile. This helper asks the same question before any of
that, without touching the box:

  stage   builds what the box's tree WILL be in a scratch directory: the box's own
          files overlaid with the package (same excludes, same release prune) and
          the box's git index, or a git target's tree and its index. Plus the box's
          .env, .env.dify and the ignored compose overlays .env's COMPOSE_FILE
          names, which `docker compose config` needs.
  judge   replays what the package arm does after its copy, on a copy of the build
          record (record the package's images, or drop the on-box manifest of a
          package that ships none), then runs the TARGET's own verifier over the
          scratch tree. Its verdict cannot drift from the late check's: it is the
          late check's code. docker reaches it read-only, and `image inspect`
          answers for the images the package carries as if they were loaded.

Where this can differ from the late check: .env here has the release's version
pins applied (the caller does that) but not the rest of the migration (renamed
keys, new defaults, COMPOSE_PROFILES and COMPOSE_FILE rewrites), a `docker load`
that would fail is not foreseen, and files a container writes in between are not
seen. So the late check stays, as the backstop.

"Cannot tell" is never a refusal: an older package may not have the verifier or
its options, and an older carrying package may not say what it carries.

Output of `judge`, one record per line (TAB separated):
  VERDICT covered|refuse|unknown
  KIND    stale|missing          (refuse only)
  ITEM    <image ref>  <why>     (refuse only)
  WHY     <text>                 (unknown only)
"""
from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import shutil
import subprocess
import sys
import tarfile

VERIFIER = os.path.join("core", "config", "app", "services", "expected_images.py")
PROVENANCE = os.path.join("core", "config", "app", "services", "image_provenance.py")
DIGEST_PINS = os.path.join("core", "config", "app", "services", "digest_pins.py")
VERIFY_TIMEOUT = 900

#: The late check's marker lines. A verifier that crashed exits 1 as well, so an
#: exit code without its line is not a verdict.
STALE_LINE = "STALE custom images ("
MISSING_LINE = "MISSING images ("


def _is_excluded(rel: str, excludes) -> bool:
    """rsync's reading of the package excludes: `name` and `name/` match a path
    component at any depth, `/name/` only at the root."""
    parts = rel.split("/")
    for raw in excludes:
        name = raw.strip("/")
        if raw.startswith("/") and not raw.endswith("/"):
            # a root-anchored file glob, e.g. `/compose.local.*.yml` (#3412)
            if len(parts) == 1 and fnmatch.fnmatchcase(parts[0], name):
                return True
            continue
        if raw.startswith("/"):
            if parts[0] == name:
                return True
        elif raw.endswith("/"):
            if name in parts[:-1]:
                return True
        elif name in parts:
            return True
    return False


def _copy_entry(src: str, dest: str) -> None:
    """One file or link, replacing whatever is at dest. Never follows a link at
    the destination; an unreadable file of the box is skipped by the caller."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.lexists(dest):
        if os.path.isdir(dest) and not os.path.islink(dest):
            shutil.rmtree(dest)
        else:
            os.unlink(dest)
    if os.path.islink(src):
        os.symlink(os.readlink(src), dest)
    else:
        shutil.copy2(src, dest)


def _git(box: str, *args, env=None):
    return subprocess.run(["git", "-C", box, *args], capture_output=True, timeout=300,
                          env={**os.environ, **(env or {})})


def _env_value(path: str, key: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith(key + "="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _copy_box_config(box: str, stage: str) -> None:
    """What a compose render reads and a tree copy leaves out: gitignored."""
    wanted = set(glob.glob(os.path.join(glob.escape(box), ".env")))
    wanted |= set(glob.glob(os.path.join(glob.escape(box), ".env.dify")))
    for entry in _env_value(os.path.join(box, ".env"), "COMPOSE_FILE").split(":"):
        if entry and not os.path.isabs(entry) and ".." not in entry.split("/"):
            wanted.add(os.path.join(box, entry))
    for src in sorted(wanted):
        dest = os.path.join(stage, os.path.relpath(src, box))
        if os.path.isfile(src) and not os.path.lexists(dest):
            _copy_entry(src, dest)


def _init_git(stage: str, index: str | None, box: str | None) -> None:
    """The scratch tree's git state: an index and nothing else, which is all the
    context listing reads. No template, so no hook is ever installed."""
    subprocess.run(["git", "init", "-q", "--template=", stage], check=True, capture_output=True, timeout=60)
    if index:
        shutil.copy2(index, os.path.join(stage, ".git", "index"))
    if box:
        for part in glob.glob(glob.escape(os.path.dirname(index or "")) + "/sharedindex.*"):
            shutil.copy2(part, os.path.join(stage, ".git", os.path.basename(part)))
        exclude = _git(box, "rev-parse", "--git-path", "info/exclude").stdout.decode().strip()
        exclude = exclude if os.path.isabs(exclude) else os.path.join(box, exclude)
        if os.path.isfile(exclude):
            os.makedirs(os.path.join(stage, ".git", "info"), exist_ok=True)
            shutil.copy2(exclude, os.path.join(stage, ".git", "info", "exclude"))


def stage_package(box: str, stage: str, pkg: str, excludes) -> int:
    """The tree after the package's copy: rsync never deletes, so it is the box's
    own listed files with the package on top, minus the release prune."""
    os.makedirs(stage)
    has_git = os.path.exists(os.path.join(box, ".git"))
    if has_git:
        listed = _git(box, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        if listed.returncode != 0:
            print("git cannot list this tree", file=sys.stderr)
            return 1
        for rel in sorted(set(listed.stdout.decode("utf-8", "surrogateescape").split("\0"))):
            if not rel or rel.startswith(".upgrade-pkg-") or rel.split("/")[0] in ("backups", ".git"):
                continue
            src = os.path.join(box, rel)
            if not os.path.lexists(src) or (os.path.isdir(src) and not os.path.islink(src)):
                continue
            try:
                _copy_entry(src, os.path.join(stage, rel))
            except OSError:
                continue   # a file a container wrote as root: the late check is the backstop
    # The installed release's file list: the prune plan compares against it.
    old_list = os.path.join(box, ".release-files")
    if os.path.isfile(old_list) and not os.path.lexists(os.path.join(stage, ".release-files")):
        _copy_entry(old_list, os.path.join(stage, ".release-files"))
    planner = os.path.join(pkg, "scripts", "release_files.py")
    new_list = os.path.join(pkg, ".release-files")
    plan_file = stage.rstrip("/") + ".prune-plan"
    planned = False
    if os.path.isfile(planner) and os.path.isfile(new_list):
        r = subprocess.run([sys.executable, planner, "plan", stage, new_list], capture_output=True, timeout=300)
        if r.returncode == 0:
            with open(plan_file, "wb") as fh:
                fh.write(r.stdout)
            planned = bool(r.stdout.strip())
    for dirpath, dirnames, filenames in os.walk(pkg):
        rel_dir = os.path.relpath(dirpath, pkg)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(dirpath, d))
                       and not _is_excluded(f"{rel_dir}/{d}/x".lstrip("/"), excludes)]
        names = filenames + [d for d in os.listdir(dirpath)
                             if os.path.islink(os.path.join(dirpath, d)) and d not in filenames]
        for name in names:
            rel = f"{rel_dir}/{name}".lstrip("/")
            if not _is_excluded(rel, excludes):
                _copy_entry(os.path.join(dirpath, name), os.path.join(stage, rel))
    if planned:
        subprocess.run([sys.executable, planner, "apply", stage, plan_file], capture_output=True, timeout=300)
    if os.path.exists(plan_file):
        os.unlink(plan_file)
    _copy_box_config(box, stage)
    if has_git:
        idx = _git(box, "rev-parse", "--git-path", "index").stdout.decode().strip()
        idx = idx if os.path.isabs(idx) else os.path.join(box, idx)
        if not os.path.isfile(idx):
            print("this tree has no git index", file=sys.stderr)
            return 1
        _init_git(stage, idx, box)
    return 0


def stage_git(box: str, stage: str, ref: str) -> int:
    """The tree after `git checkout <ref>`: the ref's files and its index. The
    upgrade stashes untracked files first, so none of the box's join them. The
    box's repository is only read: both steps work on an index outside it."""
    os.makedirs(stage)
    index = stage.rstrip("/") + ".index"
    env = {"GIT_INDEX_FILE": index}
    try:
        if _git(box, "read-tree", ref, env=env).returncode != 0:
            print(f"git cannot read {ref}", file=sys.stderr)
            return 1
        if _git(box, "checkout-index", "-a", "-f", f"--prefix={stage.rstrip('/')}/", env=env).returncode != 0:
            print(f"git cannot write the tree of {ref}", file=sys.stderr)
            return 1
        _copy_box_config(box, stage)
        _init_git(stage, index, box)
    finally:
        if os.path.exists(index):
            os.unlink(index)
    return 0


def _archive_refs(path: str) -> dict | None:
    """``{ref: image id}`` from a docker-save archive's own index; None unreadable."""
    try:
        with tarfile.open(path) as t:
            fh = t.extractfile("manifest.json")
            rows = json.load(fh) if fh else None
    except (OSError, KeyError, tarfile.TarError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    out = {}
    for row in rows:
        cfg = os.path.basename(str((row or {}).get("Config") or ""))
        cfg = cfg[:-5] if cfg.endswith(".json") else cfg
        for tag in (row or {}).get("RepoTags") or []:
            if cfg:
                out[tag] = "sha256:" + cfg
    return out


def carried_images(pkg: str, images_skipped: bool):
    """``(map, why_unknown)``: what the box will hold under each ref once the
    package's images are loaded. Empty for a code-only package. A carrying package
    that does not say what it carries cannot be judged."""
    tars = sorted(glob.glob(glob.escape(pkg) + "/images/*.tar"))
    if not tars and not images_skipped:
        return {}, ""
    try:
        with open(os.path.join(pkg, "expected-images.json"), encoding="utf-8") as fh:
            ids = (json.load(fh) or {}).get("image_ids") or {}
    except (OSError, ValueError):
        ids = {}
    if not isinstance(ids, dict) or not ids:
        return {}, "the package carries images but records no image ids (an older package)"
    carried = dict(ids)
    for tar in tars:
        for ref, image_id in (_archive_refs(tar) or {}).items():
            carried.setdefault(ref, image_id)
    return carried, ""


def _shim(bin_dir: str, guard: str, carried: dict) -> None:
    """docker for the verifier: the carried images answer `image inspect`,
    everything else goes through the read-only guard."""
    os.makedirs(bin_dir, exist_ok=True)
    table = os.path.join(bin_dir, "carried.tsv")
    with open(table, "w", encoding="utf-8") as fh:
        for ref, image_id in sorted(carried.items()):
            fh.write(f"{_canonical(ref)}\t{image_id}\n")
    script = os.path.join(bin_dir, "docker")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(f"""#!/usr/bin/env bash
if [ "${{1:-}}" = image ] && [ "${{2:-}}" = inspect ]; then
    ref="${{*: -1}}"
    case "$ref" in
        *@*) ;;
        *) case "${{ref##*/}}" in *:*) ;; *) ref="$ref:latest" ;; esac ;;
    esac
    id="$(awk -F'\\t' -v r="$ref" '$1 == r {{ print $2; exit }}' '{table}')"
    if [ -n "$id" ]; then
        case " $* " in
            *" --format "*|*" -f "*) printf '%s\\n' "$id" ;;
            *) printf '[{{"Id":"%s"}}]\\n' "$id" ;;
        esac
        exit 0
    fi
fi
exec '{guard}/docker' "$@"
""")
    os.chmod(script, 0o755)


def _canonical(ref: str) -> str:
    if "@" in ref or ":" in ref.rsplit("/", 1)[-1]:
        return ref
    return ref + ":latest"


def _state_dir() -> str:
    state = os.environ.get("RAZZFAZZ_IMAGE_STATE") or os.path.join(
        os.path.expanduser("~"), ".razzfazz", "state", "custom-image-builds.json")
    return os.path.dirname(state)


def _say(*fields) -> None:
    print("\t".join(fields))


def _unknown(why: str) -> int:
    _say("VERDICT", "unknown")
    _say("WHY", why.replace("\n", " ").replace("\t", " "))
    return 0


def judge(stage: str, work: str, guard: str, pkg: str | None, images_skipped: bool, strict: bool) -> int:
    verifier = os.path.join(stage, VERIFIER)
    if not os.path.isfile(verifier):
        return _unknown("the target release has no image verifier")
    # The verifier must be the target's. In the scratch tree an older package that
    # has none would leave the installed one standing, whose verdict is not its own.
    if pkg and not os.path.isfile(os.path.join(pkg, VERIFIER)):
        return _unknown("the package has no image verifier (an older package)")
    carried, why = carried_images(pkg, images_skipped) if pkg else ({}, "")
    if why:
        return _unknown(why)
    state_dir = os.path.join(work, "state")
    os.makedirs(state_dir, exist_ok=True)
    source = _state_dir()
    if os.path.isdir(source):
        for name in os.listdir(source):
            path = os.path.join(source, name)
            if os.path.isfile(path) and not os.path.islink(path):
                shutil.copy2(path, os.path.join(state_dir, name))
    bin_dir = os.path.join(work, "bin")
    _shim(bin_dir, guard, carried)
    env = {**os.environ,
           "PATH": f"{bin_dir}:{guard}:{os.environ.get('PATH', '')}",
           "RAZZFAZZ_IMAGE_STATE": os.path.join(state_dir, "custom-image-builds.json"),
           "GIT_CEILING_DIRECTORIES": os.path.dirname(os.path.abspath(stage)),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("COMPOSE_FILE", "COMPOSE_PROFILES", "RAZZFAZZ_ENV_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    # The package arm's step after its copy, on the copy of the record.
    if pkg is not None:
        # The package's own manifest, not the tree's: a copy never deletes, so an
        # earlier package's file can still lie in the tree.
        manifest = os.path.join(pkg, "expected-images.json")
        kept = os.path.join(state_dir, "package-manifest.json")
        if os.path.isfile(manifest) and os.path.getsize(manifest) > 0:
            for tool, args in ((PROVENANCE, ["--stack-root", stage, "--record", "--source", "package",
                                             "--package-manifest", manifest]),
                               (DIGEST_PINS, ["--record", manifest])):
                path = os.path.join(stage, tool)
                if os.path.isfile(path):
                    subprocess.run([sys.executable, path, *args], cwd=stage, env=env,
                                   capture_output=True, timeout=VERIFY_TIMEOUT)
        elif os.path.exists(kept):
            os.unlink(kept)
    argv = [sys.executable, verifier, "--stack-root", stage, "--verify", "--quiet"] + (["--strict"] if strict else [])
    try:
        r = subprocess.run(argv, cwd=stage, env=env, capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _unknown("the target release's image verifier timed out")
    out = r.stdout or ""
    lines = out.splitlines()
    if r.returncode == 3 and any(l.startswith(STALE_LINE) for l in lines):
        _say("VERDICT", "refuse")
        _say("KIND", "stale")
        for line in lines:
            mark = "  [  stale] "
            if line.startswith(mark):
                ref, _, detail = line[len(mark):].partition(": ")
                _say("ITEM", ref, detail)
        return 0
    if r.returncode == 1 and any(l.startswith(MISSING_LINE) for l in lines):
        _say("VERDICT", "refuse")
        _say("KIND", "missing")
        at = next(i for i, l in enumerate(lines) if l.startswith(MISSING_LINE))
        for line in lines[at + 1:]:
            if not line.startswith("  - "):
                break
            _say("ITEM", line[4:], "not on this box")
        return 0
    if r.returncode != 0:
        tail = (r.stderr or out).strip().splitlines()[-1:] or [""]
        return _unknown(f"the target release's image verifier exited {r.returncode}: {tail[0][:200]}")
    if "could not enumerate" in out or "provenance not checked" in out:
        return _unknown("the image set of the target release could not be worked out here")
    _say("VERDICT", "covered")
    return 0


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="upgrade_image_preflight", description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stage")
    st.add_argument("--box", required=True)
    st.add_argument("--stage", required=True)
    st.add_argument("--package-dir")
    st.add_argument("--git-ref")
    st.add_argument("--exclude", action="append", default=[])
    ju = sub.add_parser("judge")
    ju.add_argument("--stage", required=True)
    ju.add_argument("--work", required=True)
    ju.add_argument("--guard", required=True)
    ju.add_argument("--package-dir")
    ju.add_argument("--images-skipped", action="store_true")
    ju.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "stage":
        if bool(a.package_dir) == bool(a.git_ref):
            ap.error("stage takes exactly one of --package-dir and --git-ref")
        if a.package_dir:
            return stage_package(a.box, a.stage, a.package_dir, a.exclude)
        return stage_git(a.box, a.stage, a.git_ref)
    return judge(a.stage, a.work, a.guard, a.package_dir, a.images_skipped, a.strict)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
