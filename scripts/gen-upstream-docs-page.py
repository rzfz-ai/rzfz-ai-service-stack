#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/gen-upstream-docs-page.py   (2026.08 docs #162)
# =============================================================================
# Generate docs/enterprise/reference/upstream-docs.md from the SINGLE source of
# truth for what the box mirrors — core/help/mirror_config.json. The page is an
# index of the third-party component docs (name, what it is, upstream link,
# licence + attribution) so the docs.rzfz.ai portal has searchable "external
# resources" links that never drift from what the box actually mirrors.
#
# Regenerate (also run by the drift-check in the docs-structure test):
#   scripts/gen-upstream-docs-page.py
#   scripts/gen-upstream-docs-page.py --check   # non-zero if the page is stale
# =============================================================================
import argparse
import json
import os
import sys
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIRROR_CONFIG = os.path.join(REPO, "core", "help", "mirror_config.json")
OUT = os.path.join(REPO, "docs", "enterprise", "reference", "upstream-docs.md")


def _cell(s: str) -> str:
    """Make a string safe for a Markdown table cell."""
    return (s or "").strip().replace("|", "\\|").replace("\n", " ")


def _label(url: str) -> str:
    """Short, readable link label — the host (+ a hint for generic hosts)."""
    host = urllib.parse.urlparse(url).netloc or url
    return host


def render(apps: list) -> str:
    rows = sorted(apps, key=lambda a: a.get("name", "").lower())
    L = []
    L.append("# Upstream module documentation")
    L.append("")
    L.append("Official documentation for the third-party components in the rzfz.ai stack. "
             "Your box **mirrors these locally** in the in-product Help (`help.<domain>`); "
             "the links here point to each project's **upstream source**. Each component "
             "keeps its own licence and attribution, listed below.")
    L.append("")
    L.append("<!-- Reference (Diátaxis): index of upstream component docs. GENERATED from "
             "core/help/mirror_config.json by scripts/gen-upstream-docs-page.py — "
             "do not edit by hand. -->")
    L.append("")
    L.append("## Components")
    L.append("")
    L.append("| Component | What it is | Upstream docs | Licence |")
    L.append("|---|---|---|---|")
    for a in rows:
        name = _cell(a.get("name", ""))
        desc = _cell(a.get("description", "")).rstrip(".")   # tidy trailing period on prose only
        url = (a.get("mirror_url") or "").strip()
        lic = _cell(a.get("license", ""))
        cop = _cell(a.get("copyright", ""))
        licence = f"{lic} © {cop}" if lic and cop else (lic or cop or "—")
        link = f"[{_label(url)}]({url})" if url else "—"
        L.append(f"| **{name}** | {desc} | {link} | {licence} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Attribution")
    L.append("")
    L.append("These are independent open-source projects, each under its own licence "
             "(linked above). rzfz.ai mirrors their documentation for offline / in-product "
             "convenience; copyright remains with the respective projects. razzfazz.ai GmbH "
             "– Member of SEQIS Group is not affiliated with or endorsed by them.")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the upstream-docs reference page")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the generated page is stale (no write)")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)

    with open(MIRROR_CONFIG, encoding="utf-8") as f:
        apps = json.load(f).get("apps", [])
    text = render(apps)

    if a.check:
        current = ""
        if os.path.isfile(a.out):
            with open(a.out, encoding="utf-8") as f:
                current = f.read()
        if current != text:
            print("upstream-docs.md is STALE — run scripts/gen-upstream-docs-page.py",
                  file=sys.stderr)
            return 1
        print(f"upstream-docs.md is in sync ({len(apps)} components)")
        return 0

    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Wrote {a.out} ({len(apps)} components)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
