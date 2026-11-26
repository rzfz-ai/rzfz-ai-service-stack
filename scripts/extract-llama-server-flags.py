#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The command-line flags a pinned ``llama-server`` accepts, read from upstream source.

Why: llama-server refuses an unknown flag at start ("error: invalid argument"),
and upstream removes flags between builds — `--no-mmap` went with v0.4.1
(replaced by `--load-mode`), while the model catalog still passed it, so the
model that carried it could not start on any build since. Nothing here could
notice, because no list of what the engine accepts existed off-box.

This reads ``common/arg.cpp`` of the pinned commit and writes the flags that
``llama-server`` takes: every ``add_opt(common_arg({...}, ...))`` whose examples
include SERVER or COMMON, not excluded for SERVER, and not one of the removed
stubs (``arg_removed(...)``). ``tests/fixtures/llama-server-flags.json`` is its
output for the fleet pin; the catalog guard compares against it. #3403: every
OTHER build a runners.yaml target ships (the experimental channel, the rollback,
the ROCm toolbox) has its own ``tests/fixtures/llama-server-flags-<build>.json``,
and tests/unit/llm-backends/test_3403_* checks the driver's argv per hardware
line and channel against the table of the build it reaches.

Refresh at every engine bump (the guard fails until it is refreshed):

    git clone --depth 1 --branch <build> https://github.com/ggml-org/llama.cpp /tmp/l
    python3 scripts/extract-llama-server-flags.py /tmp/l/common/arg.cpp \
        --build <build> --release <vX.Y.Z> --commit <sha> > tests/fixtures/llama-server-flags.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys

_OPT = "add_opt(common_arg("
_DASH_LIST = re.compile(r'\{\s*("-[^"]*"(?:\s*,\s*"-[^"]*")*)\s*\}')
_STR = re.compile(r'"(-[^"]*)"')
_SET = re.compile(r"\.set_(examples|excludes)\(\s*\{([^}]*)\}")


def server_flags(source: str) -> list[str]:
    flags: set[str] = set()
    parts = source.split(_OPT)[1:]
    for seg in parts:
        if "arg_removed(" in seg:
            continue
        head = seg.lstrip()
        lists = []
        # the leading brace lists are the args and, optionally, the negated args
        while True:
            m = _DASH_LIST.match(head)
            if not m:
                break
            lists.append(m.group(1))
            head = head[m.end():].lstrip()
            if head.startswith(","):
                head = head[1:].lstrip()
        if not lists:
            continue
        sets = {"examples": None, "excludes": ""}
        for kind, body in _SET.findall(seg):
            sets[kind] = body
        examples = sets["examples"]
        in_server = examples is None or "LLAMA_EXAMPLE_SERVER" in examples \
            or "LLAMA_EXAMPLE_COMMON" in examples
        if not in_server or "LLAMA_EXAMPLE_SERVER" in sets["excludes"]:
            continue
        for lst in lists:
            flags.update(_STR.findall(lst))
    return sorted(flags)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("arg_cpp")
    ap.add_argument("--build", required=True)
    ap.add_argument("--release", default="")
    ap.add_argument("--commit", required=True)
    a = ap.parse_args(argv)
    with open(a.arg_cpp, encoding="utf-8") as fh:
        flags = server_flags(fh.read())
    if not flags:
        print("no flags found — not an arg.cpp?", file=sys.stderr)
        return 1
    json.dump({"engine_build": a.build, "engine_release": a.release, "commit": a.commit,
               "flags": flags}, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
