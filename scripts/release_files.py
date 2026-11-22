#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The release's own file list, and pruning what a newer release deleted (#2818).

An offline package upgrade copies the new tree over the old one and removed
nothing: on a test box 104 files the new release had deleted were still on disk
after the upgrade, two of them inside repo-root build contexts, and six custom
images read ``stale`` against the very package that carried them.

``record TREE OUT``
    The packager writes ``.release-files`` into the package: one line per file
    of the exported release, ``<sha256>  <path>`` (sha256sum format). The
    upgrade copies it into the tree with everything else, so the NEXT upgrade
    knows exactly which files this release put there.

``plan TREE NEW``
    Which files to remove before NEW (the incoming package's ``.release-files``)
    replaces the tree: the ones the OLD release shipped and NEW does not. One
    path per line on stdout, everything else on stderr. The old release's list
    is the tree's ``.release-files``; a box installed by git (no such file yet)
    uses its git index, minus what ``git archive`` never exports. With neither,
    nothing is pruned and the plan says why. A file is only planned when it is
    byte-for-byte what the old release shipped — an operator's edit is kept,
    and so is anything outside the release set (.env, data, certs, backups,
    volumes, overlays, the test tier, which has its own apply step).

``apply TREE PLAN``
    Remove the planned files (and directories this leaves empty). Each path is
    logged. ``plan`` alone is the dry run.

Exit status: 0 always for ``plan`` (an empty plan is a valid answer), 0/1 for
``record`` and ``apply``.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

LIST_NAME = ".release-files"

#: Never pruned, whatever a list says: what the package never copies
#: (cli/upgrade.sh PACKAGE_RSYNC_EXCLUDES) and what lives beside the release.
_PROTECTED_TOP = ("tests", ".git", "backups", "images", "models", "enterprise-overlay",
                  "overlay", "certs", "data", "volumes",
                  "multipass")   # tracked, but the Enterprise package tooling strips it from every package
_PROTECTED_NAMES = (LIST_NAME, ".checksums.db", ".upgrade.log")


def protected(path: str) -> bool:
    parts = path.split("/")
    if parts[0] in _PROTECTED_TOP or path in _PROTECTED_NAMES:
        return True
    return any(p.startswith(".env") for p in parts)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _blob_sha1(path: str) -> str:
    """git's blob id of a file's content (what the index records)."""
    with open(path, "rb") as fh:
        data = fh.read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _link_sha(path: str) -> str:
    return "link:" + hashlib.sha256(os.readlink(path).encode()).hexdigest()


def record(tree: str, out: str) -> int:
    lines = []
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, tree).replace(os.sep, "/")
            if rel == LIST_NAME:
                continue
            if os.path.islink(full):
                lines.append(f"{_link_sha(full)}  {rel}")
            elif os.path.isfile(full):
                lines.append(f"{_sha256(full)}  {rel}")
    if not lines:
        print(f"release_files: {tree} holds no files", file=sys.stderr)
        return 1
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sorted(lines, key=lambda l: l.split("  ", 1)[1])) + "\n")
    return 0


def read_list(path: str) -> dict | None:
    """``{path: sha}`` of a ``.release-files``, or None when unreadable or empty."""
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [l.rstrip("\n").split("  ", 1) for l in fh if l.strip()]
    except OSError:
        return None
    out = {r[1]: r[0] for r in rows if len(r) == 2 and r[1]}
    return out or None


def _git(tree: str, *args, stdin: str | None = None):
    try:
        r = subprocess.run(["git", "-C", tree, *args], input=stdin, capture_output=True,
                           text=True, timeout=120)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def _old_from_git(tree: str) -> dict | None:
    """``{path: blob-sha1}`` of the tree's git index, minus export-ignored paths."""
    if not os.path.exists(os.path.join(tree, ".git")):   # a dir, or a worktree's file
        return None
    out = _git(tree, "ls-files", "-s", "-z")
    if out is None:
        return None
    old = {}
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) >= 2 and path and fields[0] != "160000":   # a submodule is no file
            old[path] = fields[1]
    if not old:
        return None
    # `.gsd/ export-ignore` names the DIRECTORY: git archive drops everything
    # below it, but check-attr on a file inside answers "unspecified". Ask for
    # every parent directory too, spelled with its trailing slash (only then
    # does a `dir/` pattern match).
    queries = set(old)
    for path in old:
        parts = path.split("/")
        queries.update("/".join(parts[:n]) + "/" for n in range(1, len(parts)))
    attrs = _git(tree, "check-attr", "-z", "--stdin", "export-ignore", stdin="\0".join(sorted(queries)) + "\0")
    if attrs is None:
        return None
    f = attrs.split("\0")
    ignored = {f[i] for i in range(0, len(f) - 2, 3) if f[i + 2] == "set"}
    for path in list(old):
        parts = path.split("/")
        if path in ignored or any("/".join(parts[:n]) + "/" in ignored for n in range(1, len(parts))):
            del old[path]
    return old


def plan(tree: str, new_list: str) -> list[str]:
    new = read_list(new_list)
    if new is None:
        print(f"release_files: the package carries no {LIST_NAME} — nothing is pruned", file=sys.stderr)
        return []
    old = read_list(os.path.join(tree, LIST_NAME))
    by_git = old is None
    if by_git:
        old = _old_from_git(tree)
    if old is None:
        print(f"release_files: the installed release's file list is unknown (no {LIST_NAME}, no git index) "
              "— nothing is pruned", file=sys.stderr)
        return []
    out = []
    for path in sorted(set(old) - set(new)):
        if protected(path):
            continue
        full = os.path.join(tree, path)
        if os.path.islink(full):
            target = os.readlink(full).encode()
            now = hashlib.sha1(b"blob %d\0" % len(target) + target).hexdigest() if by_git else _link_sha(full)
        elif os.path.isfile(full):
            now = _blob_sha1(full) if by_git else _sha256(full)
        else:
            continue
        if now != old[path]:
            print(f"release_files: kept {path} (changed on this box since the release shipped it)",
                  file=sys.stderr)
            continue
        out.append(path)
    return out


def apply(tree: str, plan_file: str) -> int:
    try:
        with open(plan_file, encoding="utf-8") as fh:
            paths = [l.rstrip("\n") for l in fh if l.strip()]
    except OSError as e:
        print(f"release_files: cannot read the plan: {e}", file=sys.stderr)
        return 1
    root = os.path.realpath(tree)
    rc = 0
    for path in paths:
        full = os.path.join(root, path)
        parent = os.path.realpath(os.path.dirname(full))
        if protected(path) or ".." in path.split("/") or os.path.isabs(path) \
                or not (parent == root or parent.startswith(root + os.sep)):
            print(f"release_files: refused {path}", file=sys.stderr)
            rc = 1
            continue
        try:
            if os.path.islink(full) or os.path.isfile(full):
                os.remove(full)
                print(f"pruned {path}")
        except OSError as e:
            print(f"release_files: could not remove {path}: {e}", file=sys.stderr)
            rc = 1
            continue
        d = os.path.dirname(full)
        while d.startswith(root + os.sep) and d != root:
            try:
                os.rmdir(d)   # only when empty
            except OSError:
                break
            d = os.path.dirname(d)
    return rc


def main(argv) -> int:
    if len(argv) == 3 and argv[0] == "record":
        return record(argv[1], argv[2])
    if len(argv) == 3 and argv[0] == "plan":
        for p in plan(argv[1], argv[2]):
            print(p)
        return 0
    if len(argv) == 3 and argv[0] == "apply":
        return apply(argv[1], argv[2])
    print("usage: release_files.py record TREE OUT | plan TREE NEW_LIST | apply TREE PLAN", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
