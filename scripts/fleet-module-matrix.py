#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
"""Print the release x module version matrix for .gsd/FLEET-VERSIONS.md.

Reads config/manifests/versions.json AT EACH GIVEN TAG (git show), so the
matrix is what each release actually shipped, not what main says today.

    scripts/fleet-module-matrix.py v2026.08-ga.9 v2026.08-ga.15 2026.09-ga.5

Rows: every managed image (`images`), compose-pinned image (`hardcoded`) and
locally built image (`custom_built`, shown by its upstream pin). A cell is
`-` when the module is not in that release's manifest. Rows whose value
differs between the given tags are marked with `*` in the first column.
"""
import argparse
import json
import subprocess
import sys

MANIFEST = "config/manifests/versions.json"
SECTIONS = ("images", "hardcoded", "custom_built")


def load(tag):
    out = subprocess.run(
        ["git", "show", f"{tag}:{MANIFEST}"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        sys.exit(f"fleet-module-matrix: {MANIFEST} not readable at {tag}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def cell(section, entry):
    if section == "custom_built":
        pin = str(entry.get("upstream_pin") or "custom")
        # Upstream pins can carry a long provenance tail; the base is enough here.
        pin = pin.split(" + ")[0].split(" (")[0].split("; ")[-1]
        for prefix in ("FROM ", "ARG "):
            pin = pin.removeprefix(prefix)
        return pin if len(pin) <= 60 else pin[:57] + "..."
    return str(entry.get("current", "?"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tags", nargs="+", help="release tags, oldest first")
    args = ap.parse_args()

    manifests = {t: load(t) for t in args.tags}
    print("| | Module | Section | " + " | ".join(args.tags) + " |")
    print("|---|---|---|" + "---|" * len(args.tags))
    for section in SECTIONS:
        names = sorted({n for m in manifests.values() for n in m.get(section, {})})
        for name in names:
            vals = []
            for t in args.tags:
                entry = manifests[t].get(section, {}).get(name)
                vals.append(cell(section, entry) if entry else "-")
            mark = "*" if len(set(vals)) > 1 else ""
            vals = [v.replace("|", "\\|") for v in vals]
            print(f"| {mark} | {name} | {section} | " + " | ".join(vals) + " |")


if __name__ == "__main__":
    main()
