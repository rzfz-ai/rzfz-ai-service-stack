#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Render stack.json from stack.yaml (the stack SSOT).

stack.json is what ships: the licences container reads it (bind-mounted,
core/compose.yml), and the public mirror carries it (ALLOW_FILES). It is the
JSON rendering of stack.yaml and nothing else — it had no generator in the
tree and lagged stack.yaml by two modules (openuem, wazuh) for seven weeks
without anything noticing (#1056 follow-up, same class as #1354).

    python3 scripts/generate-stack-json.py          # write stack.json
    python3 scripts/generate-stack-json.py --check  # non-zero if stale

Run after any stack.yaml change. tests/unit/consistency/
test_1056_stack_json_is_rendered_from_stack_yaml.py runs --check in CI.
"""
from __future__ import annotations

import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "stack.yaml")
DST = os.path.join(ROOT, "stack.json")


def render() -> str:
    with open(SRC, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    # Byte-for-byte the layout the file has carried since 2026.07: two-space
    # indent, UTF-8 verbatim, trailing newline. Key order is stack.yaml's.
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    out = render()
    if "--check" in sys.argv:
        cur = open(DST, encoding="utf-8").read() if os.path.exists(DST) else ""
        if cur != out:
            print("stale (run scripts/generate-stack-json.py): stack.json", file=sys.stderr)
            return 1
        print("stack.json up to date.")
        return 0
    with open(DST, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"wrote {DST} ({len(out.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
