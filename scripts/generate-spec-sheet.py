#!/usr/bin/env python3
"""Generate the customer-facing STACK SPEC SHEET for one release tag.

    scripts/generate-spec-sheet.py --tag v2026.08-ga.15
    scripts/generate-spec-sheet.py --tag 2026.09-ga --out docs/enterprise/reference/stack-spec-sheet-2026.09-ga.md

Everything in the sheet is read from the tagged tree (``git show <tag>:<path>``), never
from the working copy, so a sheet for an older release is exact for that release:

    VERSION                                 the version string the box stamps
    config/manifests/versions.json          every image pin (third-party, hardcoded, our own builds)
    config/manifests/license-dates.json     the release date
    stack.yaml                              modules: name, category, tier, licence, edition, entry points, containers
    wiki/System-Requirements-Sizing.md      deployment variants, OS, disk, ports, support matrix

The sheet is a data sheet, not release notes: what the release IS (versions, modules,
platform, limits), nothing about what changed. Default output:
``docs/enterprise/reference/stack-spec-sheet-<tag>.md``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent


def show(tag: str, path: str) -> str:
    r = subprocess.run(["git", "-C", str(REPO), "show", f"{tag}:{path}"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"cannot read {path} at {tag}: {r.stderr.strip()}")
    return r.stdout


def commit_of(tag: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", f"{tag}^{{commit}}"],
                          capture_output=True, text=True, check=True).stdout.strip()


def sizing_section(md: str, number: int) -> list[str]:
    """Section ``## <number>.`` of the sizing page as whole blocks: table rows, bullets and
    paragraphs, with wrapped lines joined; code fences and HTML comments dropped; wiki-relative
    links reduced to their text (they do not resolve outside the wiki)."""
    m = re.search(rf"^## {number}\. .*?$(.*?)(?=^## \d+\. |\Z)", md, re.S | re.M)
    if not m:
        return []
    out: list[str] = []
    fence = False
    for ln in m.group(1).splitlines():
        if ln.startswith("```"):
            fence = not fence
            continue
        if fence or ln.startswith("<!--") or ln.startswith("#"):
            continue
        if not ln.strip():
            continue
        if ln.startswith("|") or ln.startswith("- ") or not out or out[-1].startswith("|"):
            out.append(ln.rstrip())
        else:
            out[-1] = out[-1] + " " + ln.strip()
    return [re.sub(r"\[([^\]]+)\]\((?!http)[^)]+\)", r"\1", ln) for ln in out]


PUBLIC_REGISTRIES = ("ghcr.io", "docker.io", "quay.io", "gcr.io", "public.ecr.aws", "mcr.microsoft.com", "registry.k8s.io")


def public_image(image: str) -> str:
    """An older tag may pin our own builds to a retired private registry, and nothing
    shipped may name that host (operator ruling 2026-09-04). An image whose first path
    segment is a host that is not one of the public registries we pull from is named by
    its last path segment as a build of ours; the version column identifies it."""
    head = image.split("/", 1)[0]
    if "/" in image and ("." in head or ":" in head) and head not in PUBLIC_REGISTRIES:
        return f"rzfz.ai build: {image.rsplit('/', 1)[-1]}"
    return image


def table_rows(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("|") and not re.match(r"^\|\s*-", ln)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out")
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args()
    tag = a.tag

    version = show(tag, "VERSION").strip()
    manifest = json.loads(show(tag, "config/manifests/versions.json"))
    stack = yaml.safe_load(show(tag, "stack.yaml"))
    sizing = show(tag, "wiki/System-Requirements-Sizing.md")
    try:
        dates = json.loads(show(tag, "config/manifests/license-dates.json"))
    except SystemExit:
        dates = {}
    dates = dates.get("releases", dates)
    if version not in dates and f"v{version}" not in dates:
        # the row for a tag is written at the cut; an older tree may predate its own row
        head = json.loads((REPO / "config" / "manifests" / "license-dates.json").read_text())
        dates = head.get("releases", head)
    released = (dates.get(version) or dates.get(f"v{version}") or {}).get("released", "—")
    sha = commit_of(tag)

    images = manifest.get("images", {})
    hard = manifest.get("hardcoded", {})
    own = manifest.get("custom_built", {})
    by_profile: dict[str, list[str]] = {}
    for name, e in list(images.items()) + list(hard.items()):
        by_profile.setdefault(e.get("profile", "—"), []).append(f"{name} {e.get('current', '')}".strip())

    mods = stack.get("modules", {})
    lines: list[str] = []
    w = lines.append
    w("<!-- section: reference -->")
    w(f"# rzfz.ai Stack — Spec Sheet {version}")
    w("")
    w(f"| | |")
    w(f"|---|---|")
    w(f"| **Release** | `{version}` (tag `{tag}`, commit `{sha}`) |")
    w(f"| **Released** | {released} |")
    w(f"| **Channel** | {manifest.get('channel', '—')} |")
    w(f"| **Modules** | {len(mods)} (see §3) |")
    w(f"| **Container images** | {len(images) + len(hard)} third-party pins, {len(own)} built by rzfz.ai (see §4) |")
    w(f"| **Licensing** | Enterprise code BSL 1.1 (source-available), Community code Apache-2.0, bundled upstream software keeps its own licence — the box's Licenses page lists every component |")
    w(f"| **Maintainer** | {stack.get('maintainer', 'razzfazz.ai')} |")
    w("")
    w("This sheet describes what the release **is**: platform, modules, pinned versions and limits. What changed in it is in the release notes for the same tag.")
    w("")
    w("## 1. Deployment variants")
    w("")
    for ln in table_rows(sizing_section(sizing, 1)):
        w(ln)
    w("")
    w("## 2. Platform")
    w("")
    w("**Operating system**")
    w("")
    for ln in sizing_section(sizing, 2):
        w(ln)
    w("")
    w("**Disk**")
    w("")
    for ln in sizing_section(sizing, 8):
        w(ln)
    w("")
    w("**Network ports**")
    w("")
    for ln in sizing_section(sizing, 9):
        w(ln)
    w("")
    w("## 3. Modules")
    w("")
    w("| Module | Category | Tier | Edition / licence | Entry points | Containers | Pinned components |")
    w("|---|---|---|---|---|---|---|")
    for mid, m in mods.items():
        subs = ", ".join(f"`{s}.<domain>`" for s in (m.get("subdomains") or [])) or "—"
        comps = ", ".join(by_profile.get(mid, [])) or "—"
        w(f"| **{m.get('name', mid)}** (`{mid}`) | {m.get('category', '—')} | {m.get('tier', '—')} | {m.get('edition', '—')} / {m.get('license', '—')} | {subs} | {len(m.get('containers') or [])} | {comps} |")
    w("")
    w("Tier: *core* is always on; *production* is supported and validated; *experimental* is opt-in and evolving.")
    w("")
    w("## 4. Pinned component versions")
    w("")
    w("| Component | Image | Version | Profile | Compatibility |")
    w("|---|---|---|---|---|")
    for name, e in sorted(list(images.items()) + list(hard.items()), key=lambda kv: kv[0]):
        w(f"| {name} | `{public_image(e.get('image', '—'))}` | `{e.get('current', '—')}` | {e.get('profile', '—')} | {e.get('compatibility', '—')} |")
    w("")
    w(f"Images built by rzfz.ai from this tree ({len(own)}): " + ", ".join(f"`{n}`" for n in sorted(own)) + ".")
    w("")
    w("## 5. Support matrix and known limitations")
    w("")
    for ln in sizing_section(sizing, 10):
        w(ln)
        w("")
    w("")
    w("## 6. Delivery and upgrade")
    w("")
    w("- Online install: the bootstrap script clones this tag and provisions the box (`rzfz init`).")
    w("- Offline install and upgrade: a self-contained package built from this tag (`cli/package.sh`) carries images, models and wheels; the box needs no internet.")
    w("- Upgrade: `rzfz upgrade` to a newer tag of the same cycle or the next cycle's GA; a backup is taken first unless the operator opts out.")
    w("- Security fixes: the current GA cycle receives them; earlier cycles do not (see `SECURITY.md` at this tag).")
    w("")
    w(f"*Generated by `scripts/generate-spec-sheet.py --tag {tag}` on {dt.date.today().isoformat()} from the tagged tree only.*")
    out = "\n".join(lines) + "\n"
    if a.stdout:
        sys.stdout.write(out)
        return 0
    dest = pathlib.Path(a.out) if a.out else REPO / "docs" / "enterprise" / "reference" / f"stack-spec-sheet-{tag}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out, encoding="utf-8")
    print(f"wrote {dest.relative_to(REPO)} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
