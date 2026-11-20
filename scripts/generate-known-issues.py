#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Generate the per-release Known Issues pages of the enterprise documentation.

Source of truth: releases/known-issues.yaml (one entry per
defect). From it this script writes

  * docs/enterprise/known-issues/index.md           ("Which version am I on?", overview)
  * docs/enterprise/known-issues/<line>.md          one page per release line (2026.08, ...)
  * a marked callout block in every page an entry links to:
        <!-- known-issues:begin --> ... <!-- known-issues:end -->
    (HTML comments are dropped by the Help Center renderer, so the markers never show.)

An entry appears on every release line it affects; it is written once.

Never rendered: the internal fields `verified`, `draft_workaround` and `sources`.
Publication rule: the workaround steps are published only for `verified: yes`;
for `partial` / `no` the workaround field carries the fixed support text.

Usage:
    python3 scripts/generate-known-issues.py            # write the pages
    python3 scripts/generate-known-issues.py --check    # exit 1 if any page is stale or the source is invalid
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs" / "enterprise"
KI_DIR = DOCS / "known-issues"
SOURCE = REPO / "releases" / "known-issues.yaml"  # outside docs/: its internal fields must not ship with the docs
VERSION_FILE = REPO / "VERSION"

#: The first release line that has a page. Older lines are not documented here.
FIRST_LINE = "2026.08"
BEGIN = "<!-- known-issues:begin -->"
END = "<!-- known-issues:end -->"
SUPPORT_PREFIX = "Contact support for a verified workaround"
SEVERITIES = ("critical", "high", "medium", "low")
VERIFIED = ("yes", "partial", "no")
CUSTOMER_FIELDS = ("id", "title", "severity", "security", "affected_from", "affected_to",
                   "fixed_in", "symptom", "impact", "workaround", "links")
INTERNAL_FIELDS = ("verified", "draft_workaround", "sources")
REQUIRED = CUSTOMER_FIELDS + ("verified",)

_VER = re.compile(r"^v?(\d{4})\.(\d{2})-ga(?:\.(\d+))?$|^(\d{4})\.(\d{2})$|^(\d{4})\.(\d{2})-rc(\d{1,2})$")
_ID = re.compile(r"^KI-(\d{4}\.\d{2})-(\d{2,3})$")


# Pages written whole by another generator: a callout there would be wiped on the
# next run of that generator, so the known-issues generator refuses to link them.
GENERATED_PAGES = frozenset({"docs/enterprise/reference/commands.md"})


def support_text(fixed_in: str) -> str:
    if fixed_in == "open":
        return f"{SUPPORT_PREFIX} – no fixed version is available yet."
    return f"{SUPPORT_PREFIX} – or upgrade to {fixed_in}."


def parse_version(v) -> tuple[int, int, int] | None:
    """(year, month, patch) of a tag; `2026.11` (a release not tagged yet) is its GA, `2026.11-rcN` sorts before it. None if malformed."""
    if not isinstance(v, str):
        return None
    m = _VER.match(v)
    if not m:
        return None
    if m.group(1):
        return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if m.group(6):
        # a release candidate sorts before the GA of its line (and after the previous line)
        return int(m.group(6)), int(m.group(7)), int(m.group(8)) - 100
    return int(m.group(4)), int(m.group(5)), 0


def line_of(v: str) -> str:
    p = parse_version(v)
    return f"{p[0]}.{p[1]:02d}"


def current_line(entries: list[dict]) -> str:
    """The release line being prepared: the VERSION file, or the newest line named in the data."""
    lines = []
    if VERSION_FILE.is_file():
        m = re.match(r"^v?(\d{4}\.\d{2})", VERSION_FILE.read_text(encoding="utf-8").strip())
        if m:
            lines.append(m.group(1))
    for e in entries:
        for k in ("affected_from", "affected_to", "fixed_in"):
            if parse_version(e.get(k)):
                lines.append(line_of(e[k]))
    return max(lines + [FIRST_LINE])


def is_release_line(y: int, m: int) -> bool:
    """Releases were monthly up to 2026.09; from 2026.11 on they come every second month (odd months)."""
    return (y, m) <= (2026, 9) or m % 2 == 1


def release_lines(entries: list[dict]) -> list[str]:
    cur = current_line(entries)
    y, m = map(int, FIRST_LINE.split("."))
    out = []
    while f"{y}.{m:02d}" <= cur:
        if is_release_line(y, m):
            out.append(f"{y}.{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def affects(e: dict, line: str, cur: str) -> bool:
    to = cur if e["affected_to"] == "open" else line_of(e["affected_to"])
    return line_of(e["affected_from"]) <= line <= to


# ----------------------------------------------------------------------------- validation
def validate(entries, root: Path = REPO) -> list[str]:
    """Every problem in the source, as readable strings (empty = valid)."""
    errs: list[str] = []
    if not isinstance(entries, list) or not entries:
        return ["the source must be a non-empty list of entries"]
    seen: set[str] = set()
    for n, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            errs.append(f"entry #{n}: not a mapping")
            continue
        i = e.get("id", f"#{n}")
        miss = [f for f in REQUIRED if f not in e]
        if miss:
            errs.append(f"{i}: missing field(s) {', '.join(miss)}")
            continue
        unknown = sorted(set(e) - set(CUSTOMER_FIELDS) - set(INTERNAL_FIELDS))
        if unknown:
            errs.append(f"{i}: unknown field(s) {', '.join(unknown)}")
        if not isinstance(i, str) or not _ID.match(i):
            errs.append(f"{i}: id must look like KI-2026.08-01")
        if i in seen:
            errs.append(f"{i}: duplicate id")
        seen.add(i)
        for link in e.get("links") or []:
            if str(link) in GENERATED_PAGES:
                errs.append(f"{i}: links {link}, a page another generator rewrites; "
                            "link the guide that explains the task instead")
        for f in ("title", "symptom", "impact", "workaround"):
            if not isinstance(e[f], str) or not e[f].strip():
                errs.append(f"{i}: {f} is empty")
        if e["severity"] not in SEVERITIES:
            errs.append(f"{i}: severity must be one of {', '.join(SEVERITIES)}")
        if not isinstance(e["security"], bool):
            errs.append(f"{i}: security must be true or false")
        if e["verified"] not in VERIFIED:
            errs.append(f"{i}: verified must be one of {', '.join(VERIFIED)} (quote it: yes alone is a boolean)")
        # version range: affected_from <= affected_to < fixed_in
        a = parse_version(e["affected_from"])
        t = None if e["affected_to"] == "open" else parse_version(e["affected_to"])
        f_ = None if e["fixed_in"] == "open" else parse_version(e["fixed_in"])
        if a is None:
            errs.append(f"{i}: affected_from {e['affected_from']!r} is not a version")
        if e["affected_to"] != "open" and t is None:
            errs.append(f"{i}: affected_to {e['affected_to']!r} is not a version or open")
        if e["fixed_in"] != "open" and f_ is None:
            errs.append(f"{i}: fixed_in {e['fixed_in']!r} is not a version or open")
        if a and t and a > t:
            errs.append(f"{i}: affected_from is after affected_to")
        if t and f_ and f_ <= t:
            errs.append(f"{i}: fixed_in must be after affected_to")
        if a and f_ and f_ <= a:
            errs.append(f"{i}: fixed_in must be after affected_from")
        if e["fixed_in"] == "open" and e["affected_to"] != "open":
            errs.append(f"{i}: an open issue must have affected_to: open")
        if e["affected_to"] == "open" and e["fixed_in"] != "open":
            errs.append(f"{i}: affected_to: open needs fixed_in: open")
        # workaround publication rule
        if e["verified"] == "yes":
            if isinstance(e["workaround"], str) and e["workaround"].lstrip().startswith(SUPPORT_PREFIX):
                errs.append(f"{i}: verified: yes but the workaround is the support text")
            if "draft_workaround" in e:
                errs.append(f"{i}: verified: yes entries carry no draft_workaround")
        elif e["workaround"] != support_text(e["fixed_in"]):
            errs.append(f"{i}: verified: {e['verified']} - the workaround must read exactly: {support_text(e['fixed_in'])!r}")
        # links
        if not isinstance(e["links"], list) or not e["links"]:
            errs.append(f"{i}: links must be a non-empty list")
        else:
            for l in e["links"]:
                if not (isinstance(l, str) and l.startswith("docs/enterprise/") and l.endswith(".md")):
                    errs.append(f"{i}: link {l!r} must be a docs/enterprise/*.md path")
                elif l.startswith("docs/enterprise/known-issues/"):
                    errs.append(f"{i}: link {l!r} points into the generated pages")
                elif not (root / l).is_file():
                    errs.append(f"{i}: link {l} does not exist")
    return errs


# ----------------------------------------------------------------------------- rendering
def anchor(i: str) -> str:
    return i.lower().replace(".", "-")


def doc_title(rel: str) -> str:
    for ln in (REPO / rel).read_text(encoding="utf-8").splitlines():
        if ln.startswith("# "):
            return ln[2:].strip()
    return Path(rel).stem


def relpath(frm: Path, to: Path) -> str:
    """Relative link from the directory of `frm` to `to` (both under docs/enterprise)."""
    a = frm.parent.relative_to(DOCS).parts if frm.parent != DOCS else ()
    b = to.relative_to(DOCS).parts
    k = 0
    while k < len(a) and k < len(b) - 1 and a[k] == b[k]:
        k += 1
    return "/".join([".."] * (len(a) - k) + list(b[k:]))


def affected_text(e: dict) -> str:
    if e["affected_to"] == e["affected_from"]:
        return f"{e['affected_from']} only"
    to = "the current release" if e["affected_to"] == "open" else e["affected_to"]
    return f"{e['affected_from']} to {to}"


def fixed_text(e: dict) -> str:
    return "Not fixed yet" if e["fixed_in"] == "open" else f"{e['fixed_in']} (upgrade)"


def first_page(e: dict, lines: list[str], cur: str) -> str:
    for ln in lines:
        if affects(e, ln, cur):
            return ln
    return lines[0]


def render_entry(e: dict, page: Path) -> str:
    sev = e["severity"].capitalize()
    rows = [f"| Severity | {sev}{' – security issue' if e['security'] else ''} |",
            f"| Affected versions | {affected_text(e)} |",
            f"| Fixed in | {fixed_text(e)} |"]
    related = "\n".join(f"- [{doc_title(l)}]({relpath(page, REPO / l)})" for l in e["links"])
    return (f'<a id="{anchor(e["id"])}"></a>\n\n### {e["id"]} – {e["title"]}\n\n'
            "| | |\n|---|---|\n" + "\n".join(rows) + "\n\n"
            f"**What you notice**\n\n{e['symptom'].strip()}\n\n"
            f"**Impact**\n\n{e['impact'].strip()}\n\n"
            f"**Workaround**\n\n{e['workaround'].strip()}\n\n"
            f"**Related pages**\n\n{related}\n")


def order(entries: list[dict]) -> list[dict]:
    rank = {s: n for n, s in enumerate(SEVERITIES)}
    return sorted(entries, key=lambda e: (e["fixed_in"] != "open", rank[e["severity"]], e["id"]))


def render_line_page(line: str, entries: list[dict], lines: list[str], cur: str) -> str:
    page = KI_DIR / f"{line}.md"
    mine = order([e for e in entries if affects(e, line, cur)])
    open_ = [e for e in mine if e["fixed_in"] == "open"]
    out = [f"<!-- section: guides -->\n# Known issues in {line}\n",
           f"Known defects that affect a box on release line {line}, with the workaround and the release that fixes them. "
           "Not sure which release you run? See [Which version am I on?](index.md#which-version-am-i-on). "
           "An issue that spans several releases is listed on each of them.\n",
           f"**{len(mine)} issues listed, {len(open_)} not fixed yet.** "
           "Every issue marked \"upgrade\" is fixed by moving to the named release or later "
           "([Upgrade a box](../how-to/upgrade.md)).\n"]
    if line >= "2026.11":  # the pages of older lines are frozen history
        out.append("Run the `docker compose` commands in the workarounds in the "
                   "[stack directory](../admin-guide.md#where-your-stack-lives), unless the entry names another directory.\n")
    if not mine:
        out.append("No known issues are listed for this release line.\n")
    else:
        out.append("| ID | Issue | Severity | Fixed in |\n|---|---|---|---|")
        for e in mine:
            out.append(f"| [{e['id']}](#{anchor(e['id'])}) | {e['title']} | {e['severity'].capitalize()} | "
                       f"{'not fixed yet' if e['fixed_in'] == 'open' else e['fixed_in']} |")
        out.append("")
        for e in mine:
            out.append(render_entry(e, page))
    out.append("Do you see something that is not listed? [Report a problem](../how-to/report-a-problem.md) "
               "or [get support](../how-to/get-support.md).\n")
    return "\n".join(out)


def render_index(entries: list[dict], lines: list[str], cur: str) -> str:
    out = ["<!-- section: guides -->\n# Known issues and workarounds\n",
           "Known defects of the releases from 2026.08 onwards, what they mean for your box, how to work around them "
           "and which release fixes them. Pick the page of your release line; an issue that spans several releases "
           "appears on each of them. The related how-to pages carry a short note that points back here.\n",
           "## Which version am I on?\n",
           "Any one of these shows the release your box runs:\n",
           "- Command line: `rzfz status` prints it as `VERSION file` in its \"CODE STATE\" section.\n"
           "- In the [stack directory](../admin-guide.md#where-your-stack-lives): `cat VERSION`.\n"
           "- Settings portal (`https://settings.<domain>`): the version is shown at the top of the left navigation, under the rzfz.ai name.\n",
           "A version such as `2026.09-ga.4` belongs to the release line **2026.09**. Open that page and read the "
           "entries whose *affected versions* include yours.\n",
           "## Pages by release line\n",
           "| Release line | Issues listed | Not fixed yet |\n|---|---|---|"]
    for ln in reversed(lines):
        mine = [e for e in entries if affects(e, ln, cur)]
        out.append(f"| [{ln}]({ln}.md) | {len(mine)} | {sum(1 for e in mine if e['fixed_in'] == 'open')} |")
    out.append("")
    open_ = order([e for e in entries if e["fixed_in"] == "open"])
    out.append("## Not fixed yet\n")
    if open_:
        out.append("| ID | Issue | Severity | Page |\n|---|---|---|---|")
        for e in open_:
            ln = first_page(e, lines, cur)
            out.append(f"| {e['id']} | {e['title']} | {e['severity'].capitalize()} | [{ln}]({ln}.md#{anchor(e['id'])}) |")
        out.append("")
    else:
        out.append("No open issues are listed.\n")
    out += ["## How to read an entry\n",
            "- **Affected versions** run from the first to the last release that has the defect. `the current release` means it is still present.\n"
            "- **Fixed in** names the release that removes it. Upgrade with `rzfz upgrade --target <version>` "
            "([Upgrade a box](../how-to/upgrade.md)); take a backup first.\n"
            "- **Workaround** is a set of steps that was checked against the affected version. Where it reads "
            "\"Contact support for a verified workaround\", no checked steps are published yet: ask support, or upgrade to the "
            "release that fixes the issue.\n"
            "- **Severity**: *critical* means data loss or a box that stops working, *high* a main function or a security "
            "weakness, *medium* a function with a workaround, *low* a minor or cosmetic effect.\n"
            "- Security issues are listed like every other issue: what is affected, the impact, the workaround and the fixing release.\n",
            "## Need help?\n",
            "[Get support](../how-to/get-support.md) · [Report a problem](../how-to/report-a-problem.md) · "
            "[Upgrade a box](../how-to/upgrade.md)\n"]
    return "\n".join(out)


def callout(page: Path, es: list[dict], lines: list[str], cur: str) -> str:
    idx = relpath(page, KI_DIR / "index.md")
    items = []
    for e in sorted(es, key=lambda x: x["id"]):
        ln = first_page(e, lines, cur)
        link = relpath(page, KI_DIR / f"{ln}.md") + "#" + anchor(e["id"])
        fx = "not fixed yet" if e["fixed_in"] == "open" else f"fixed in {e['fixed_in']}"
        items.append(f"> - [{e['id']}]({link}) – {e['title']} (affects {affected_text(e)}; {fx})")
    return "\n".join([BEGIN, f"> **Known issues related to this page.** Workarounds and fixing releases are on the "
                      f"[Known issues pages]({idx}).", ">", *items, END])


_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)


def apply_callout(text: str, block: str | None) -> str:
    """Insert, refresh or remove the marked block. It sits right below the page's first heading."""
    if block is None and BEGIN not in text:
        return text
    text = _BLOCK.sub("", text)
    if block is None:
        return re.sub(r"\n{3,}", "\n\n", text)
    src = text.split("\n")
    at = next((n + 1 for n, ln in enumerate(src) if ln.startswith("# ")), 0)
    head = "\n".join(src[:at]).rstrip("\n")
    tail = "\n".join(src[at:]).lstrip("\n")
    return (head + "\n\n" if head else "") + block + "\n\n" + tail


def build(root: Path = REPO) -> dict[Path, str]:
    """Every file this generator owns, as {path: content}."""
    entries = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    errs = validate(entries, root)
    if errs:
        raise SystemExit("known-issues.yaml is invalid:\n  " + "\n  ".join(errs))
    cur = current_line(entries)
    lines = release_lines(entries)
    files: dict[Path, str] = {KI_DIR / "index.md": render_index(entries, lines, cur)}
    for ln in lines:
        files[KI_DIR / f"{ln}.md"] = render_line_page(ln, entries, lines, cur)
    by_page: dict[str, list[dict]] = {}
    for e in entries:
        for l in e["links"]:
            by_page.setdefault(l, []).append(e)
    for md in sorted(DOCS.rglob("*.md")):
        if KI_DIR in md.parents:
            continue
        rel = md.relative_to(REPO).as_posix()
        old = md.read_text(encoding="utf-8")
        new = apply_callout(old, callout(md, by_page[rel], lines, cur) if rel in by_page else None)
        if new != old:
            files[md] = new
    return files


def main(argv: list[str]) -> int:
    check = "--check" in argv
    files = build()
    stale = [p for p, c in files.items() if not p.is_file() or p.read_text(encoding="utf-8") != c]
    known = {p.name for p in files}
    stray = [p for p in KI_DIR.glob("*.md") if p.name not in known]  # a release line that no longer exists
    if check:
        for p in stale + stray:
            print(f"STALE: {p.relative_to(REPO)} - run scripts/generate-known-issues.py", file=sys.stderr)
        return 1 if (stale or stray) else 0
    for p in stale:
        p.write_text(files[p], encoding="utf-8")
        print(f"wrote {p.relative_to(REPO)}")
    for p in stray:
        p.unlink()
        print(f"removed {p.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
