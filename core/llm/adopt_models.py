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
import fnmatch
import json
import os
import shutil
import sys
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
    status: str = "missing"      # copy | present | missing
    reason: str = ""

    @property
    def kind(self) -> str:
        return self.status


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)

    @property
    def to_copy(self) -> list[Action]:
        return [a for a in self.actions if a.status == "copy"]

    @property
    def present(self) -> list[Action]:
        return [a for a in self.actions if a.status == "present"]

    @property
    def missing(self) -> list[Action]:
        return [a for a in self.actions if a.status == "missing"]

    def as_json(self) -> str:
        return json.dumps(
            {"copy": [a.__dict__ for a in self.to_copy],
             "present": [a.__dict__ for a in self.present],
             "missing": [a.__dict__ for a in self.missing]},
            indent=2, sort_keys=True)


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
#:   "gpustack" — local-models/<declared filename>         (subdir preserved)
#:                local-models/<repo>/<mmproj>             (repo dir REQUIRED)
#:
#: Measured, not assumed: `core/llm/model_source.py::local_model_path` passes the
#: declared filename through verbatim — so the sharded `qwen3-coder-next` keeps
#: its subfolder — and `apply_mmproj` builds `local-models/<repo>/<mmproj>`.
#: `hf_pull.ensure_file` on the node, by contrast, only ever looks at
#: `models_dir/<basename>`.
#:
#: A package bundled FLAT therefore serves the manager directly and the GPUStack
#: legacy path not at all, which is why the receiving side re-shapes rather than
#: the package carrying both.
LAYOUTS = ("manager", "gpustack")


def dest_for(layout: str, *, filename: str, repo: str, is_mmproj: bool,
             src_basename: str, dst_dir: str) -> str:
    if layout == "manager":
        return os.path.join(dst_dir, src_basename)
    if layout != "gpustack":
        raise ValueError(f"unknown layout {layout!r} (expected one of {LAYOUTS})")
    if is_mmproj:
        return os.path.join(dst_dir, repo, src_basename)
    sub = os.path.dirname(filename)
    return os.path.join(dst_dir, sub, src_basename) if sub else os.path.join(dst_dir, src_basename)


def build_plan(models: list[dict], *, src_root: str, dst_dir: str,
               layout: str = "manager",
               lister: Callable[[str], Iterable[str]] = None,
               exists: Callable[[str], bool] = None) -> Plan:
    """What to copy, what is already there, what cannot be found.

    `lister` and `exists` are injectable so the plan can be tested against a
    described filesystem rather than a real one.
    """
    lister = lister or (lambda d: os.listdir(d))
    exists = exists or os.path.exists

    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r} (expected one of {LAYOUTS})")
    plan = Plan()
    for m in models:
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
                else:
                    plan.actions.append(Action(
                        name=name, filename=filename, dest=dest, source=src,
                        status="copy", reason="found in GPUStack's volume"))
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


def execute(plan: Plan, *, dry_run: bool, copier=None, log=print) -> int:
    """Copy what the plan says to copy. Returns the number of files copied."""
    copier = copier or _copy_atomic
    done = 0
    for a in plan.to_copy:
        if dry_run:
            log(f"  would copy  {os.path.basename(a.dest)}  ({a.name})")
            continue
        log(f"  copying     {os.path.basename(a.dest)}  ({a.name})")
        copier(a.source, a.dest)
        done += 1
    return done


def _copy_atomic(src: str, dest: str) -> None:
    """Copy via a temporary name in the SAME directory, then rename.

    A weight is large; an interrupted copy leaves a truncated file, and the
    node's `ensure_file` only checks that the path EXISTS — it would then
    report "cached" and launch an engine on a half-written GGUF. The rename is
    atomic within a filesystem, so the destination name never exists in a
    partial state.
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".rzfz-partial"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


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

    plan = build_plan(models, src_root=args.src, dst_dir=args.dst, layout=args.layout)
    if args.json:
        print(plan.as_json())
        return 0

    print(f"Adopting weights for {len(models)} model(s):")
    print(f"  already present : {len(plan.present)}")
    print(f"  to copy         : {len(plan.to_copy)}")
    print(f"  not found       : {len(plan.missing)}")
    execute(plan, dry_run=args.dry_run)
    for a in plan.missing:
        print(f"  NOT FOUND   {a.filename}  ({a.name}) — {a.reason}")
    # A missing weight is not an error here: the manager will fetch it. It IS
    # reported, because on an offline box "the manager will fetch it" is not
    # true and the operator has to know before the deploy hangs.
    return 0


if __name__ == "__main__":
    sys.exit(main())
