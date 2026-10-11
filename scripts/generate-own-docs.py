#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Generate baseline core/help/own_docs/<module>.md pages for modules missing one.

Reads core/config/profiles.yaml; for every profile with has_ui=true that does
NOT already have core/help/own_docs/<module>.md, emit a baseline page from
description + long_description + license + dependencies. Operator can extend
the file afterwards.

Usage:
    python3 scripts/generate-own-docs.py            # generate missing
    python3 scripts/generate-own-docs.py --check    # exit 1 if any are missing
    python3 scripts/generate-own-docs.py --force    # regenerate all
"""
import argparse
import os
import sys

import yaml

OWN_DOCS_DIR = "core/help/own_docs"
PROFILES_FILE = "core/config/profiles.yaml"


def render(module_id: str, profile: dict) -> str:
    name = profile.get("name", module_id)
    category = profile.get("category", "")
    maturity = profile.get("maturity", "")
    description = profile.get("description", "") or ""
    long_description = (profile.get("long_description") or "").rstrip()
    license_str = profile.get("license", "")
    github = profile.get("github", "")
    has_ui = profile.get("has_ui", False)
    deps = (profile.get("dependencies") or {}).get("required") or []
    rec_deps = (profile.get("dependencies") or {}).get("recommended") or []
    enable_impact = profile.get("enable_impact", "")
    disable_impact = profile.get("disable_impact", "")

    # URL section
    urls = profile.get("urls") or []
    url_lines = []
    for u in urls:
        sub = u.get("subdomain")
        label = u.get("label", "")
        if sub:
            url_lines.append(
                f"- **{label or sub}**: `https://{sub}.<your-domain>`"
            )
    if not url_lines and has_ui:
        url_lines.append(
            f"- Web UI: `https://{module_id}.<your-domain>` "
            f"(behind Authentik SSO)"
        )
    if not url_lines:
        url_lines.append(
            f"- API-only — reachable internally as `http://{module_id}` "
            f"from sibling containers."
        )

    # Header
    out = [f"# {name}", ""]
    if description:
        out.extend([description.strip(), ""])

    if maturity == "experimental":
        out.append(
            "> **Status: experimental.** Operator-facing surface may change "
            "between releases. Pin a tested version before relying on it in "
            "production."
        )
        out.append("")

    out.append("## URL")
    out.extend(url_lines)
    out.append("")

    if long_description:
        out.append("## What it does")
        out.append("")
        out.append(long_description)
        out.append("")

    out.append("## Enable / disable")
    out.append("")
    out.append(
        f"Add `{module_id}` to `COMPOSE_PROFILES` in `.env`, then bring "
        f"the stack up:"
    )
    out.append("")
    out.append("```sh")
    out.append(f"docker compose --profile {module_id} up -d")
    out.append("```")
    out.append("")
    out.append(f"To disable, remove `{module_id}` from `COMPOSE_PROFILES`:")
    out.append("")
    out.append("```sh")
    out.append(f"docker compose --profile {module_id} down")
    out.append("```")
    out.append("")
    if enable_impact:
        out.append(f"**Enable impact:** {enable_impact}")
        out.append("")
    if disable_impact:
        out.append(f"**Disable impact:** {disable_impact}")
        out.append("")

    if deps or rec_deps:
        out.append("## Dependencies")
        out.append("")
        if deps:
            out.append(
                f"- Required: {', '.join('`' + d + '`' for d in deps)}"
            )
        if rec_deps:
            out.append(
                f"- Recommended: {', '.join('`' + d + '`' for d in rec_deps)}"
            )
        out.append("")

    if github or license_str:
        out.append("## Upstream")
        out.append("")
        if github:
            out.append(f"- Source / releases: {github}")
        if license_str:
            out.append(f"- License: {license_str}")
        out.append("")

    out.append("## Operator notes")
    out.append("")
    out.append(
        "<!-- Extend this section with module-specific caveats, model "
        "recipes, smoke tests, troubleshooting tips, etc. The auto-generated "
        "content above can be overwritten by hand once you do; the generator "
        "treats hand-edited files as authoritative. -->"
    )
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any docs missing; do not write")
    ap.add_argument("--force", action="store_true",
                    help="regenerate even if file exists")
    ap.add_argument("--only", default="",
                    help="comma-separated module IDs to limit scope")
    args = ap.parse_args()

    if not os.path.exists(PROFILES_FILE):
        print(f"error: {PROFILES_FILE} not found", file=sys.stderr)
        return 2

    with open(PROFILES_FILE) as f:
        cfg = yaml.safe_load(f) or {}
    profiles = cfg.get("profiles") or {}

    only = {s.strip() for s in args.only.split(",") if s.strip()}

    # Skip non-module profiles (infrastructure-like, no description page)
    skip = {"core", "agents"}

    missing = []
    written = []
    for mid, p in sorted(profiles.items()):
        if mid in skip:
            continue
        if only and mid not in only:
            continue
        if not isinstance(p, dict):
            continue
        target = os.path.join(OWN_DOCS_DIR, f"{mid}.md")
        exists = os.path.exists(target)
        if exists and not args.force:
            continue
        if args.check:
            missing.append(mid)
            continue
        os.makedirs(OWN_DOCS_DIR, exist_ok=True)
        with open(target, "w") as f:
            f.write(render(mid, p))
        written.append(target)

    if args.check:
        if missing:
            print(f"missing own_docs for {len(missing)} modules:")
            for m in missing:
                print(f"  - {m}")
            return 1
        print("all module own_docs present")
        return 0

    if not written:
        print("nothing to do (use --force to regenerate)")
    else:
        print(f"wrote {len(written)} files:")
        for t in written:
            print(f"  - {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
