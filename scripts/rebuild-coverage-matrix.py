#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Rebuild .gsd/COVERAGE-MATRIX.md per-R counts from current test set.

Q6 / TRF-DEC-05: the matrix as committed at main@c83f6cb7 was generated
BEFORE Half-A and Half-B blind-spot work landed. Tests exist for many
requirements that the matrix still labels ❌. This script rescans the
test tree, updates the per-cell counts, recomputes the ✅/🟡/❌ status,
and refreshes the matrix's "Generated …" header + the per-category +
total summary blocks.

It does NOT touch the requirement definitions in REQUIREMENTS.md, the
blind-spot triage prose in BLIND-SPOTS.md, or the matrix's intro/legend
sections. REQUIREMENTS.md and BLIND-SPOTS.md only get their "Generated"
header refreshed.

Usage:
    scripts/rebuild-coverage-matrix.py [--dry-run]

The R-anchor pattern: any `R-CATEGORY-NN` substring (in the docstring
or anywhere in the file body) registers that test file under that
requirement, with its tier (acc/unit/api/ui/scripts) determined from
its path under tests/.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
GSD = REPO_ROOT / ".gsd"

R_ANCHOR_RE = re.compile(r"\bR-([A-Z]+)-(\d+)\b")
TIER_TO_COL = {
    "acceptance": "acc",
    "unit": "unit",
    "api": "api",
    "ui": "ui",
    "scripts": "scripts",
}


def scan_tests() -> dict[str, dict[str, int]]:
    """Return {req_id: {tier_col: test_count}}.

    "test_count" is the number of `def test_*` definitions in the file
    that mentions the R-id, summed across all files referencing that
    R-id under that tier.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for tier in TIER_TO_COL:
        tdir = TESTS_DIR / tier
        if not tdir.is_dir():
            continue
        for src in tdir.rglob("test_*.py"):
            try:
                body = src.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rids = {f"R-{cat}-{int(n):02d}" for cat, n in R_ANCHOR_RE.findall(body)}
            if not rids:
                continue
            n_tests = len(re.findall(r"^[ \t]*(?:async )?def test_", body, re.MULTILINE))
            if n_tests == 0:
                continue
            col = TIER_TO_COL[tier]
            for rid in rids:
                counts[rid][col] += n_tests
    return counts


_NUM_RE = re.compile(r"(\d+)")


def _parse_count_cell(cell: str) -> int:
    """Parse a count cell from the previous matrix. Cells can be "—", "5",
    "7+", or empty."""
    s = cell.strip()
    if s in ("—", "-", ""):
        return 0
    m = _NUM_RE.match(s)
    return int(m.group(1)) if m else 0


_STATUS_RANK = {"❌": 0, "🟡": 1, "✅": 2}


def parse_existing_matrix(path: Path) -> dict[str, dict]:
    """Parse the existing matrix to preserve title + legacy + prior counts.

    Returns {req_id: {title, legacy, category, prior_counts}} where
    prior_counts has keys acc/unit/api/ui/scripts. Prior counts are kept
    as a fallback when the rescan finds 0 R-anchored tests for a tier
    but the previous matrix recorded coverage there (legacy tests + tests
    written before the R-anchor convention).
    """
    out: dict[str, dict] = {}
    if not path.is_file():
        return out
    cur_cat = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m_cat = re.match(r"^### ([A-Z]+)\s*$", line)
        if m_cat:
            cur_cat = m_cat.group(1)
            continue
        if not line.startswith("| R-"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 9:
            continue
        rid = cells[0]
        # Last cell is the status emoji.
        status_cell = cells[-1]
        prior_status = "❌"
        for emoji in ("✅", "🟡", "❌"):
            if emoji in status_cell:
                prior_status = emoji
                break
        out[rid] = {
            "id": rid,
            "title": cells[1],
            "category": cur_cat or rid.split("-")[1],
            "prior_counts": {
                "acc": _parse_count_cell(cells[2]),
                "unit": _parse_count_cell(cells[3]),
                "api": _parse_count_cell(cells[4]),
                "ui": _parse_count_cell(cells[5]),
                "scripts": _parse_count_cell(cells[6]),
            },
            "legacy": cells[7],
            "prior_status": prior_status,
        }
    return out


def compute_status(counts: dict[str, int], legacy: str, prior_status: str = "❌") -> str:
    """Apply the matrix's ✅/🟡/❌ rule.

    Mechanical floor:
    - ❌ if no coverage at all
    - 🟡 if exactly one tier with coverage OR only legacy
    - ✅ if numeric coverage in 2+ tiers OR (1 tier AND legacy non-empty)

    The prior_status raises the floor: a previously-✅ requirement that
    still has at least one cell of coverage stays ✅ (the previous rating
    was operator-curated and considered all aspects covered). A
    previously-🟡 requirement stays at least 🟡 if any cell has coverage.
    The matrix never silently DOWN-grades a status without losing all
    coverage — that protects against false-negative regressions when
    a test gets refactored.
    """
    numeric_tiers = sum(1 for k in ("acc", "unit", "api", "ui", "scripts") if counts.get(k, 0) > 0)
    has_legacy = bool(legacy and legacy not in ("—", "-", ""))
    if numeric_tiers >= 2 or (numeric_tiers >= 1 and has_legacy):
        mechanical = "✅"
    elif numeric_tiers == 1 or has_legacy:
        mechanical = "🟡"
    else:
        mechanical = "❌"

    # Status floor from previous matrix: don't down-grade a prior ✅/🟡 to ❌
    # while we still have any coverage; don't down-grade ✅ to 🟡 either.
    if mechanical == "❌":
        return mechanical  # nothing to defend
    if _STATUS_RANK.get(prior_status, 0) > _STATUS_RANK.get(mechanical, 0):
        return prior_status
    return mechanical


def cell(n: int) -> str:
    return str(n) if n > 0 else "—"


def render_matrix(
    *,
    existing: dict[str, dict],
    new_counts: dict[str, dict[str, int]],
    head_short: str,
) -> str:
    """Render the full COVERAGE-MATRIX.md."""
    cats_order = [
        "AUTH", "NET", "DATA", "LLM", "CHAT", "WORKFLOW", "AGENT", "KNOW",
        "DOC", "STTS", "SEARCH", "COLLAB", "OBSV", "OPS", "CONFIG", "BACKUP",
        "SEC", "COMP", "DEF", "BUG",
    ]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for rid, meta in existing.items():
        by_cat[meta["category"]].append(meta)
    # Also surface any new R-id that appears in tests but NOT in the matrix
    # (e.g. R-DEF-* or R-BUG-* added after the matrix was generated). They
    # render under their category with a "(new)" title.
    for rid in sorted(new_counts.keys()):
        if rid not in existing:
            cat = rid.split("-")[1]
            by_cat[cat].append({
                "id": rid,
                "title": "(added since previous matrix; see test docstring)",
                "category": cat,
                "legacy": "—",
            })
            existing[rid] = by_cat[cat][-1]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out: list[str] = []
    out.append("# Coverage Matrix")
    out.append("")
    out.append(f"Generated {today} from `.gsd/REQUIREMENTS.md` and the M032 test suite at")
    out.append(f"main@`{head_short}` (Q6 rebuild — TRF-DEC-05).")
    out.append("")
    out.append("Legend:")
    out.append("- ✅ — at least one test exercises every aspect of the requirement")
    out.append("- 🟡 — partial: at least one test exists but not for every named aspect")
    out.append("- ❌ — no test exercises this requirement (BLIND SPOT — see `.gsd/BLIND-SPOTS.md`)")
    out.append("")
    out.append("Test-tier columns:")
    out.append("- **Acc** — `tests/acceptance/<module>/test_*.py` cases (Tier-D probe)")
    out.append("- **Unit** — `tests/unit/<module>/test_*.py` cases")
    out.append("- **API** — `tests/api/<module>/test_*.py` cases")
    out.append("- **UI** — `tests/ui/<module>/test_*.py` cases (Playwright)")
    out.append("- **Scripts** — `tests/scripts/<script>/test_*.py` cases")
    out.append("- **Legacy** — `tests/test-*.sh` (pre-M032 bash scripts)")
    out.append("")
    out.append("Counts are `def test_*` definitions in files that reference the")
    out.append("requirement's R-id (in docstrings, comments, or code). A \"—\" means")
    out.append("zero tests under that tier.")
    out.append("")
    out.append("---")
    out.append("")
    out.append("## Per-requirement coverage")
    out.append("")

    cat_totals: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "ok": 0, "partial": 0, "blind": 0})

    for cat in cats_order:
        rows = by_cat.get(cat, [])
        if not rows:
            continue
        out.append(f"### {cat}")
        out.append("")
        out.append("| Req | Title | Acc | Unit | API | UI | Scripts | Legacy | Coverage |")
        out.append("|---|---|---:|---:|---:|---:|---:|---|---|")
        for meta in sorted(rows, key=lambda r: r["id"]):
            rid = meta["id"]
            scanned = new_counts.get(rid, {})
            prior = meta.get("prior_counts", {})
            # Merge: take the MAX per cell. The rescan picks up new
            # R-anchored tests; the prior counts preserve unannotated
            # tests that were curated into the matrix before the
            # R-anchor convention. Either signal proves coverage.
            counts = {
                col: max(scanned.get(col, 0), prior.get(col, 0))
                for col in ("acc", "unit", "api", "ui", "scripts")
            }
            legacy = meta.get("legacy") or "—"
            status = compute_status(counts, legacy, meta.get("prior_status", "❌"))
            cat_totals[cat]["total"] += 1
            if status == "✅":
                cat_totals[cat]["ok"] += 1
            elif status == "🟡":
                cat_totals[cat]["partial"] += 1
            else:
                cat_totals[cat]["blind"] += 1
            out.append(
                f"| {rid} | {meta['title']} | "
                f"{cell(counts.get('acc', 0))} | "
                f"{cell(counts.get('unit', 0))} | "
                f"{cell(counts.get('api', 0))} | "
                f"{cell(counts.get('ui', 0))} | "
                f"{cell(counts.get('scripts', 0))} | "
                f"{legacy} | {status} |"
            )
        out.append("")

    out.append("---")
    out.append("")
    out.append("## Coverage by category")
    out.append("")
    out.append("| Category | Total reqs | ✅ Covered | 🟡 Partial | ❌ Blind |")
    out.append("|---|---:|---:|---:|---:|")
    grand = {"total": 0, "ok": 0, "partial": 0, "blind": 0}
    for cat in cats_order:
        if cat not in cat_totals:
            continue
        t = cat_totals[cat]
        out.append(f"| {cat} | {t['total']} | {t['ok']} | {t['partial']} | {t['blind']} |")
        for k in grand:
            grand[k] += t[k]
    out.append(f"| **TOTAL** | **{grand['total']}** | **{grand['ok']}** | "
               f"**{grand['partial']}** | **{grand['blind']}** |")
    out.append("")
    out.append("## Coverage summary")
    out.append("")
    pct = lambda n: f"{round(100.0 * n / grand['total'])} %" if grand['total'] else "0 %"
    out.append(f"- Total requirements: **{grand['total']}**")
    out.append(f"- Strictly covered: **{grand['ok']}** ({pct(grand['ok'])})")
    out.append(f"- Partial: **{grand['partial']}** ({pct(grand['partial'])})")
    out.append(f"- Blind spots: **{grand['blind']}** ({pct(grand['blind'])})")
    out.append("")
    out.append("Triage and prioritization in `.gsd/BLIND-SPOTS.md`.")
    out.append("")
    return "\n".join(out)


def refresh_header_dates(path: Path, head_short: str) -> bool:
    """Bump the 'Generated YYYY-MM-DD from main@`xxx`' line in REQUIREMENTS.md
    or BLIND-SPOTS.md to today + current HEAD. Returns True if the file
    was changed."""
    if not path.is_file():
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = path.read_text(encoding="utf-8")
    new = re.sub(
        r"Generated \d{4}-\d{2}-\d{2}(?: from )?",
        f"Generated {today} from ",
        body,
        count=1,
    )
    new = re.sub(
        r"main@`[a-f0-9]{6,}`",
        f"main@`{head_short}`",
        new,
        count=1,
    )
    if new != body:
        path.write_text(new, encoding="utf-8")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="Print the new matrix to stdout, do not write")
    args = p.parse_args(argv)

    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip() or "unknown"

    new_counts = scan_tests()
    print(f"Scanned tests; found R-anchors in {len(new_counts)} requirement(s).", file=sys.stderr)

    matrix_path = GSD / "COVERAGE-MATRIX.md"
    existing = parse_existing_matrix(matrix_path)
    print(f"Loaded {len(existing)} existing matrix entries.", file=sys.stderr)

    new_md = render_matrix(existing=existing, new_counts=new_counts, head_short=head)

    if args.dry_run:
        sys.stdout.write(new_md)
        return 0

    matrix_path.write_text(new_md, encoding="utf-8")
    print(f"Wrote {matrix_path}", file=sys.stderr)

    for fname in ("REQUIREMENTS.md", "BLIND-SPOTS.md"):
        path = GSD / fname
        if refresh_header_dates(path, head):
            print(f"Refreshed header date in {path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
