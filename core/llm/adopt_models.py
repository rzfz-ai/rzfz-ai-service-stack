#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Hand GPUStack's weights to the LLM Manager without downloading them again.

2026.09 moves the box from GPUStack to the LLM Manager. The GGUF files serve
both — they only live in a different volume and a different layout:

    GPUStack   /var/lib/gpustack/cache/huggingface/<repo>/<filename>
               /var/lib/gpustack/local-models/<repo>/<filename>

    Manager    /models/<basename(filename)>        FLAT, no repo directories

The flat layout is not a convention invented here. The node builds its
destination as ``os.path.join(models_dir, os.path.basename(filename))`` and
returns ``"cached"`` when that path exists (``hf_pull.ensure_file``), and
``missing_files()`` reads the same shape. A weight copied to the right name is
therefore never fetched.

On a 30–60 GB standard set that is the difference between a coffee and an
afternoon; on a metered or air-gapped line it is the difference between
possible and not.

This module PLANS and (optionally) executes. The plan is pure — given a listing
of what exists, it says what would be copied and why — so it can be tested
without Docker, without volumes and without weights.
"""
from __future__ import annotations

import argparse
import errno
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

#: Where GPUStack keeps weights inside its volume, in the order package.sh
#: already searches them: the HF cache first, then the sideload directory.
GPUSTACK_SUBDIRS = ("cache/huggingface", "local-models")


@dataclass
class Action:
    """One weight, and what is to happen to it."""
    name: str                    # the model key (qwen3.6, nomic-embed-text, …)
    filename: str                # as declared (may be a glob or carry a subdir)
    dest: str                    # absolute destination path
    source: Optional[str] = None # absolute source path, None when not found
    status: str = "missing"      # copy | present | missing | rejected | skipped (#2392)
    reason: str = ""
    sha256: Optional[str] = None # #2392: the catalogue pin the source matched
    hashed: Optional[list] = None # #2392: [dev, ino, size, mtime_ns, ctime_ns] of the source hashed

    @property
    def kind(self) -> str:
        return self.status


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    hashed_files: int = 0        # #2392: how much the pin check read
    hashed_bytes: int = 0

    @property
    def to_copy(self) -> list[Action]:
        return [a for a in self.actions if a.status == "copy"]

    @property
    def present(self) -> list[Action]:
        return [a for a in self.actions if a.status == "present"]

    @property
    def missing(self) -> list[Action]:
        return [a for a in self.actions if a.status == "missing"]

    @property
    def skipped(self) -> list[Action]:
        """#2392: pinned files that changed between their hash and their link —
        not placed (the bytes under the name are not the ones that were hashed)."""
        return [a for a in self.actions if a.status == "skipped"]

    @property
    def rejected(self) -> list[Action]:
        """#2392: files NOT adopted because their model's bytes are not the
        catalogue's — the node fetches the approved ones instead."""
        return [a for a in self.actions if a.status == "rejected"]

    def as_json(self) -> str:
        return json.dumps(
            {"copy": [a.__dict__ for a in self.to_copy],
             "present": [a.__dict__ for a in self.present],
             "missing": [a.__dict__ for a in self.missing],
             "rejected": [a.__dict__ for a in self.rejected]},
            indent=2, sort_keys=True)


def pin_matches(path: str, sha256: str | None) -> bool:
    """#2392: an unpinned file adopts as before; a pinned one only when its
    bytes are the approved ones (streamed hash, never read into RAM)."""
    if not sha256:
        return True
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest() == sha256


def _stable_hash(path: str) -> tuple:
    """``(sha256, [st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns])`` read
    through one fd; the identity is None when the file changed while it was
    hashed (the digest is then void, and the file is not adopted)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        b = os.fstat(fh.fileno())
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
        a = os.fstat(fh.fileno())
    stable = _identity(a) == _identity(b) and _identity(os.stat(path)) == _identity(b)
    return h.hexdigest(), (_identity(b) if stable else None)


def _identity(st) -> list:
    """The node's sidecar identity (hf_pull.file_identity), as a list."""
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


#: The node's sidecar (modules/llm/node-agent/app/hf_pull.py, SIDECAR_SUFFIX
#: and write_sidecar): "<sha256> <st_dev> <st_ino> <st_size> <st_mtime_ns>
#: <st_ctime_ns>". This file runs alone in a throwaway container, so it cannot
#: import the node's module; the format is duplicated on purpose and held
#: together by tests/unit/llm/test_2392_adoption_checks_the_pin.py, which has
#: the node's own sidecar_confirms accept what is written here.
NODE_SIDECAR_SUFFIX = ".rzfz-sha256"


def _write_node_sidecar(dest: str, sha256: str, st) -> None:
    side = dest + NODE_SIDECAR_SUFFIX
    tmp = f"{side}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="ascii") as fh:
        fh.write(" ".join([sha256, str(st.st_dev), str(st.st_ino), str(st.st_size),
                           str(st.st_mtime_ns), str(st.st_ctime_ns)]) + "\n")
    os.replace(tmp, side)


def _pin_for(m: dict, src: str) -> str | None:
    """The catalogue sha256 for the file at ``src`` (matched by basename — the
    pin map is keyed by repo path, the adopted layout is flat), or None."""
    by_base = {os.path.basename(str(p)): str(d) for p, d in (m.get("sha256") or {}).items()}
    return by_base.get(os.path.basename(src))


def _candidate_sources(src_root: str, repo: str, filename: str,
                       lister: Callable[[str], Iterable[str]]) -> list[str]:
    """Every path under `src_root` that could hold this weight.

    `filename` may carry a repo subfolder (`Qwen3-Coder-Next-Q4_K_M/…-*.gguf`)
    and may be a glob — both occur in standard-models.yaml — so the search is
    over the DIRECTORY the declaration implies, matched by the basename
    pattern. Matching the whole path would miss a sharded model whose parts sit
    in a subfolder, which is exactly the case #303 flattens.
    """
    sub = os.path.dirname(filename)
    pattern = os.path.basename(filename)
    out: list[str] = []
    for base in GPUSTACK_SUBDIRS:
        # `local-models` holds <repo>/<file>; so does the HF cache mirror that
        # package.sh reads. Search both with and without the repo segment: a
        # sideloaded file is sometimes dropped in flat by an operator.
        for d in (os.path.join(src_root, base, repo, sub) if sub else os.path.join(src_root, base, repo),
                  os.path.join(src_root, base, sub) if sub else os.path.join(src_root, base)):
            try:
                names = list(lister(d))
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue
            for n in sorted(names):
                if fnmatch.fnmatch(n, pattern):
                    out.append(os.path.join(d, n))
    # stable and duplicate-free: the same file can be reachable by two of the
    # paths above, and copying it twice is at best noise.
    seen, uniq = set(), []
    for p in out:
        b = os.path.basename(p)
        if b in seen:
            continue
        seen.add(b)
        uniq.append(p)
    return uniq


#: The two destination shapes weights are placed in, and they are NOT the same.
#:
#:   "manager"  — /models/<basename>                       (flat)
#:                /models/mmproj--<org>--<repo>--<basename> (a projector, #2431)
#:   "gpustack" — local-models/<declared filename>         (subdir preserved)
#:                local-models/<repo>/<mmproj>             (repo dir REQUIRED)
#:
#: Measured, not assumed: `core/llm/model_source.py::local_model_path` passes the
#: declared filename through verbatim — so the sharded `qwen3-coder-next` keeps
#: its subfolder — and `apply_mmproj` builds `local-models/<repo>/<mmproj>`.
#: `hf_pull.ensure_file` on the node, by contrast, only ever looks at
#: `models_dir/<hf_pull.store_name>` — the basename, or a projector's store name.
#:
#: A package bundled FLAT therefore serves the manager directly and the GPUStack
#: legacy path not at all, which is why the receiving side re-shapes rather than
#: the package carrying both.
LAYOUTS = ("manager", "gpustack")


def projector_store_name(filename: str, repo: str) -> str:
    """#2431: the name a projector gets in the manager's flat volume —
    ``mmproj--<org>--<repo>--<basename>``, because one projector basename
    ships in dozens of repositories. Must equal node-agent ``hf_pull.store_name`` (this
    script runs alone in a container and cannot import the node package) —
    pinned by tests/unit/llm-node-agent/test_2431_projectors_are_keyed_by_repository.py."""
    base = os.path.basename(filename or "")
    if repo and "mmproj" in base.lower():
        return "mmproj--" + repo.strip("/").replace("/", "--") + "--" + base
    return base


def dest_for(layout: str, *, filename: str, repo: str, is_mmproj: bool,
             src_basename: str, dst_dir: str) -> str:
    if layout == "manager":
        # #2431: a projector goes to its repository's store name
        return os.path.join(dst_dir, projector_store_name(src_basename, repo)
                            if is_mmproj else src_basename)
    if layout != "gpustack":
        raise ValueError(f"unknown layout {layout!r} (expected one of {LAYOUTS})")
    if is_mmproj:
        return os.path.join(dst_dir, repo, src_basename)
    sub = os.path.dirname(filename)
    return os.path.join(dst_dir, sub, src_basename) if sub else os.path.join(dst_dir, src_basename)


def build_plan(models: list[dict], *, src_root: str, dst_dir: str,
               layout: str = "manager",
               lister: Callable[[str], Iterable[str]] = None,
               exists: Callable[[str], bool] = None,
               progress: Callable[[str], None] = None,
               verify_pins: bool = True) -> Plan:
    """What to copy, what is already there, what cannot be found.

    #2392: every copy candidate with a catalogue pin is HASHED here, before
    anything is placed; ``progress`` hears one line per file first, because
    on a full GPUStack volume that is minutes of reading.

    `lister` and `exists` are injectable so the plan can be tested against a
    described filesystem rather than a real one.
    """
    lister = lister or (lambda d: os.listdir(d))
    exists = exists or os.path.exists

    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r} (expected one of {LAYOUTS})")
    plan = Plan()
    for m in models:
        first = len(plan.actions)
        name = m.get("name") or "<unnamed>"
        repo = m.get("repo", "")
        mmprojs = {v for k, v in m.items()
                   if k in ("mmproj", "mmproj_filename", "huggingface_mmproj_filename") and v}
        for filename in _declared_files(m):
            is_mmproj = filename in mmprojs
            found = _candidate_sources(src_root, repo, filename, lister)
            if not found:
                plan.actions.append(Action(
                    name=name, filename=filename,
                    dest=dest_for(layout, filename=filename, repo=repo,
                                  is_mmproj=is_mmproj,
                                  src_basename=os.path.basename(filename),
                                  dst_dir=dst_dir),
                    status="missing",
                    reason=("no file matching this declaration under "
                            f"{'/'.join(GPUSTACK_SUBDIRS)} — GPUStack never held it, "
                            "or it was pruned")))
                continue
            for src in found:
                dest = dest_for(layout, filename=filename, repo=repo,
                                is_mmproj=is_mmproj,
                                src_basename=os.path.basename(src),
                                dst_dir=dst_dir)
                if exists(dest):
                    plan.actions.append(Action(
                        name=name, filename=filename, dest=dest, source=src,
                        status="present",
                        reason="already in the manager's volume — left untouched"))
                    continue
                pin = _pin_for(m, src)
                got, hashed = None, None
                if pin and verify_pins:
                    size = os.path.getsize(src)
                    if progress:
                        progress(f"hashing {os.path.basename(src)} ({_gib(size)}) "
                                 "against its catalogue pin…")
                    got, hashed = _stable_hash(src)
                    plan.hashed_files += 1
                    plan.hashed_bytes += size
                if pin and verify_pins and (got != pin or hashed is None):
                    plan.actions.append(Action(
                        name=name, filename=filename, dest=dest, source=src,
                        status="rejected",
                        reason=("sha256 differs from the catalogue pin — not adopted (#2392)"
                                if got != pin else
                                "changed while it was hashed — not adopted (#2392)")))
                else:
                    plan.actions.append(Action(
                        name=name, filename=filename, dest=dest, source=src,
                        status="copy", reason="found in GPUStack's volume",
                        sha256=pin, hashed=hashed))
        # #2392: all-or-nothing per model. A shard set (or weight + projector)
        # with one file off its pin is not adopted at all: placing the good
        # files would leave the node reading them as present next to a hole.
        # Decided here, in the plan — before execute() links or copies a byte.
        mine = plan.actions[first:]
        bad = [a for a in mine if a.status == "rejected"]
        if bad:
            for a in mine:
                if a.status == "copy":
                    a.status = "rejected"
                    a.reason = (f"{os.path.basename(bad[0].source)} of the same model "
                                "differs from its catalogue pin — the set is not "
                                "adopted (#2392)")
    return plan


def _declared_files(m: dict) -> list[str]:
    """The weight and, when the model declares one, its vision projector.

    A multimodal model without its mmproj loads and then answers image prompts
    with nonsense — the projector is not optional decoration, and offline there
    is no auto-detect to fall back on (standard-models.yaml says so for
    qwen3.6 in as many words).

    NOTE the projector does NOT come from `expected_models.py --json`: that
    payload carries only name/repo/filename/roles/size. It has its own
    enumeration, `--list-mmproj`, and `merge_mmproj()` below folds it in. This
    was found while building this module — without the merge, three of the
    seven standard models would have been adopted WITHOUT their projector and
    nothing would have said so.
    """
    out = [m["filename"]] if m.get("filename") else []
    for key in ("mmproj", "mmproj_filename", "huggingface_mmproj_filename"):
        if m.get(key):
            out.append(m[key])
    return out


def merge_mmproj(models: list[dict], mmproj_lines: Iterable[str]) -> list[dict]:
    """Fold `expected_models.py --list-mmproj` (name<TAB>repo<TAB>file) in.

    Unknown model names are an error, not a shrug: the two enumerations come
    from the same YAML, so a name in one and not the other means the source of
    truth moved under us and the adoption would quietly skip a projector.
    """
    by_name = {m.get("name"): m for m in models}
    for raw in mmproj_lines:
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise ValueError(f"--list-mmproj line is not name<TAB>repo<TAB>file: {line!r}")
        name, _repo, mmproj = parts
        if name not in by_name:
            raise ValueError(
                f"--list-mmproj names {name!r}, which --json does not list. The "
                "two enumerations have drifted; adopting now would skip a "
                "projector without saying so.")
        by_name[name]["mmproj"] = mmproj
    return models


def execute(plan: Plan, *, dry_run: bool, copier=None, log=print,
            src_root: str = "", link_root: str | None = None) -> int:
    """Place what the plan says to place. Returns the number of files placed."""
    copier = copier or _place_atomic
    done, how_count = 0, {"linked": 0, "copied": 0}
    for a in plan.to_copy:
        if dry_run:
            log(f"  would adopt {os.path.basename(a.dest)}  ({a.name})")
            continue
        if a.hashed is not None and _identity(os.stat(a.source)) != list(a.hashed):
            # #2392 (D15 review round 2): the source is not the inode, in the
            # state, that the plan hashed — an in-place rewrite put back with
            # `touch -d` keeps name, size and mtime and changes only ctime.
            # Checked BEFORE the link, because the link itself moves ctime.
            a.status = "skipped"
            a.reason = "changed since it was hashed against its pin — not adopted (#2392)"
            log(f"  SKIPPED     {os.path.basename(a.dest)}  ({a.name}) — {a.reason}")
            continue
        log(f"  adopting    {os.path.basename(a.dest)}  ({a.name})")
        if copier is _place_atomic:
            how = _place_atomic(a.source, a.dest, link_src=_link_path(a.source, src_root, link_root))
            how_count[how] += 1
            if how == "linked" and a.sha256 and a.hashed:
                # #2392: the plan hashed an inode against its pin, and the
                # pre-link check above found it unchanged; if the name now
                # holds THAT inode (same ino, size, mtime — the link itself
                # moved its ctime), tell the node (hf_pull.sidecar_confirms)
                # so it does not hash it again. The sidecar binds to the
                # identity read AFTER the rename. A copy gets no sidecar: the
                # node checks the bytes that were written.
                st = os.stat(a.dest)
                if _identity(st)[1:4] == list(a.hashed)[1:4]:
                    _write_node_sidecar(a.dest, a.sha256, st)
        else:
            copier(a.source, a.dest)
        done += 1
    if not dry_run:
        log(f"adopted: {how_count['linked']} linked, {how_count['copied']} copied")
    return done


def _place_atomic(src: str, dest: str, *, link_src: str | None = None) -> str:
    """Put ``src`` at ``dest`` via a temporary name in the SAME directory, then
    rename — the destination name never exists half-written (the node reports
    an existing name as "cached", #1544).

    #2400: a hard link first. Two docker volumes share a filesystem by
    construction, and adopting 259 GB by copy needs 259 GB free on exactly the
    boxes that have the most weights. GGUF weights are immutable, so a shared
    inode is safe; if GPUStack's cache is pruned later, the manager's name is
    the last link. ``link_src`` is the same file seen through the mount that
    also holds ``dest`` (box mode: /vols); ``src`` is the read-only view the
    copy fallback reads. A copy happens only on EXDEV (a --from USB disk,
    another device, two separate mounts). Returns "linked" or "copied".
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".rzfz-partial"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    try:
        os.link(link_src or src, tmp)
        how = "linked"
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copyfile(src, tmp)
        how = "copied"
    os.replace(tmp, dest)
    return how


def _link_works(src: str, dst_dir: str) -> bool:
    """#2400: measure, do not guess — st_dev is equal for two bind mounts of
    one filesystem, yet link(2) across them answers EXDEV. A probe link says
    what the real placement will do."""
    os.makedirs(dst_dir, exist_ok=True)
    probe = os.path.join(dst_dir, ".rzfz-link-probe")
    if os.path.lexists(probe):
        os.unlink(probe)
    try:
        os.link(src, probe)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return False
        raise
    os.unlink(probe)
    return True


def _link_path(source: str, src_root: str, link_root: str | None) -> str | None:
    if not link_root or os.path.normpath(link_root) == os.path.normpath(src_root):
        return None
    return os.path.join(link_root, os.path.relpath(source, src_root))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="adopt_models")
    ap.add_argument("--expected", required=True,
                    help="expected-models JSON (core/llm/expected_models.py --json)")
    ap.add_argument("--mmproj",
                    help="expected_models.py --list-mmproj output; REQUIRED for a "
                         "complete adoption — the JSON above carries no projectors")
    ap.add_argument("--src", required=True, help="GPUStack data root (…/var/lib/gpustack)")
    ap.add_argument("--dst", required=True, help="the manager's models dir (…/models)")
    ap.add_argument("--layout", choices=LAYOUTS, default="manager",
                    help="destination shape: manager = flat under --dst; "
                         "gpustack = the legacy local-models shape (subdir for "
                         "shards, repo dir for projectors)")
    ap.add_argument("--link-src", default=None,
                    help="the --src tree seen through the mount that also holds --dst "
                         "(box mode: /vols/<gpustack-volume>/_data); links are made "
                         "from here, copies read --src")
    ap.add_argument("--max-copy-bytes", type=int, default=0,
                    help="refuse (exit 3) before writing when the plan would COPY more "
                         "than this many bytes; 0 = unlimited")
    ap.add_argument("--link-only", action="store_true",
                    help="never copy: refuse (exit 3) before placing anything when "
                         "any file cannot be hard-linked")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the plan and exit")
    args = ap.parse_args(argv)

    with open(args.expected, encoding="utf-8") as fh:
        models = json.load(fh).get("models", [])
    if args.mmproj:
        with open(args.mmproj, encoding="utf-8") as fh:
            models = merge_mmproj(models, fh)
    else:
        print("WARNING: --mmproj not given — vision projectors are NOT adopted, "
              "and a multimodal model without one answers image prompts with "
              "nonsense.", file=sys.stderr)

    if args.link_src:
        # #2400 review I4: the shell built this path from volume names; prove it
        # names the SAME directory as --src before a link is made through it.
        try:
            same = os.path.samefile(args.src, args.link_src)
        except OSError:
            same = False
        if not same:
            print(f"WARNING: --link-src {args.link_src} is not the same directory as "
                  f"--src {args.src} — adopting by copy.", file=sys.stderr)
            args.link_src = None

    # #2392 (D15 review I3): the pin check reads every candidate; say so on
    # stderr — also under --json, whose stdout stays the plan alone.
    def _progress(msg: str) -> None:
        print(f"  {msg}", file=sys.stderr, flush=True)

    # #2400: probe EVERY copy candidate (a link probe is one link + one
    # unlink) so the limit covers all the bytes that will really be copied.
    # #2392: BEFORE the pin hash — a probe link moves the source's ctime, and
    # execute() refuses a source whose identity is not the one it hashed.
    probe = (args.max_copy_bytes or args.link_only) and not args.dry_run and not args.json
    linkable: dict = {}
    if probe:
        for a in build_plan(models, src_root=args.src, dst_dir=args.dst,
                            layout=args.layout, verify_pins=False).to_copy:
            probe_src = _link_path(a.source, args.src, args.link_src) or a.source
            linkable[a.source] = _link_works(probe_src, os.path.dirname(a.dest))

    t0 = time.monotonic()
    plan = build_plan(models, src_root=args.src, dst_dir=args.dst, layout=args.layout,
                      progress=_progress)
    print(f"  hashed {plan.hashed_files} file(s), {_gib(plan.hashed_bytes)}, against "
          f"their catalogue pins in {time.monotonic() - t0:.1f} s", file=sys.stderr, flush=True)
    if args.json:
        print(plan.as_json())
        return 0

    print(f"Adopting weights for {len(models)} model(s):")
    print(f"  already present : {len(plan.present)}")
    print(f"  to copy         : {len(plan.to_copy)}")
    print(f"  not found       : {len(plan.missing)}")
    print(f"  rejected        : {len(plan.rejected)}")
    if probe and plan.to_copy:
        copying = [a for a in plan.to_copy if not linkable.get(a.source, False)]
        if copying:
            need = sum(os.path.getsize(a.source) for a in copying)
            if args.link_only:
                print(f"refusing: --link-only and {len(copying)} file(s) cannot be "
                      f"hard-linked; the adoption would COPY {need} bytes. Nothing was "
                      "placed.", file=sys.stderr)
                return 3
            if need > args.max_copy_bytes:
                print(f"refusing: the adoption would COPY {need} bytes and only "
                      f"{args.max_copy_bytes} are allowed (--max-copy-bytes); run it "
                      "with --from on the same filesystem, or free space",
                      file=sys.stderr)
                return 3
    execute(plan, dry_run=args.dry_run, src_root=args.src, link_root=args.link_src)
    for a in plan.missing:
        print(f"  NOT FOUND   {a.filename}  ({a.name}) — {a.reason}")
    for a in plan.skipped:
        print(f"  SKIPPED     {os.path.basename(a.source)}  ({a.name}) — {a.reason}")
    for a in plan.rejected:
        # #2392: the file in GPUStack's volume is left exactly as it is (the
        # source is read-only here); the node fetches the approved bytes.
        print(f"  REJECTED    {os.path.basename(a.source)}  ({a.name}) — {a.reason}")
    # A missing weight is not an error here: the manager will fetch it. It IS
    # reported, because on an offline box "the manager will fetch it" is not
    # true and the operator has to know before the deploy hangs.
    return 0


if __name__ == "__main__":
    sys.exit(main())
