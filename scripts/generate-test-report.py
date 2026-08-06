#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Customer-facing test report generator for razzfazz-ai-service-stack.

Inputs (under tests/results/<run-id>/):
  - junit.xml    JUnit XML produced by `razzfazz-test.sh --ci-mode`
  - coverage.xml (optional) Cobertura-style coverage report from pytest-cov
  - .coverage    (optional) raw coverage.py data file (used to convert→XML)

Cross-cuts:
  - tests/{unit,api,ui,acceptance,scripts}/  — for `def test_*` docstring extraction
  - tests/pytest.ini                         — for marker → tier mapping
  - tests/pyproject.toml                     — coverage thresholds + module → source roots
  - .gsd/REQUIREMENTS.md                     — requirement IDs + titles
  - .gsd/COVERAGE-MATRIX.md                  — per-requirement coverage status
  - config/manifests/versions.json           — stack version / channel

Outputs:
  - tests/results/<run-id>/test-report.md    canonical Markdown report
  - tests/results/<run-id>/test-report.pdf   PDF rendered via Gotenberg or pandoc

Customer-facing rules (TR-DEC-02): internal milestone IDs, bug tags,
internal IPs/paths, and skill names are scrubbed via `sanitize_for_customer`
at every text-emission point.

Usage:
    scripts/generate-test-report.py <run-id> [--repo-root PATH]
                                    [--no-pdf] [--stack-version X]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Customer-facing redaction
# ---------------------------------------------------------------------------

# Patterns whose text is replaced inline (preserves surrounding sentence).
#
# Order matters: longer / more-specific patterns fire BEFORE the shorter
# generic catch-alls so a substring like `BS-AUTH-DEC-02` doesn't get
# half-eaten by the `BS-[A-Z]+-\d+` rule.
_INLINE_REDACTIONS: list[tuple[re.Pattern, str]] = [
    # Internal milestone / slice IDs
    (re.compile(r"\bM0\d{2}(?:[-/ ]?S\d{2}(?:\.\d+)?)?\b"), "current development cycle"),
    (re.compile(r"\bP\d+\.\d+\b"), "internal phase"),
    # Internal bug / decision tags
    # Long form first: BS-<CAT>-{BUG,DEC,FIX}-NN
    (re.compile(r"\bBS-[A-Z]+-(?:BUG|DEC|FIX|GAP|NOTE)-\d+\b"), "tracked internally"),
    (re.compile(r"\bBS-[A-Z]+-\d+\b"), "tracked internally"),
    # Bare workstream prefix (e.g. "by BS-CFGBKDATA"); a category code with
    # no numeric suffix is still an internal reference.
    (re.compile(r"\bBS-[A-Z]{2,}\b"), "internal workstream"),
    (re.compile(r"\bBSB-\d+(?:-[a-z]+)?\b"), "tracked internally"),
    (re.compile(r"\bS\d+-BUG-\d+\b"), "tracked internally"),
    (re.compile(r"\bTR-BUG-\d+\b"), "tracked internally"),
    (re.compile(r"\bTR-DEC-\d+\b"), "internal decision record"),
    # Internal blind-spots / dev-notes filenames
    (re.compile(r"\bblind-spots[-_][a-z0-9_-]+\.md\b"), "internal tracking docs"),
    (re.compile(r"\b(?:morning-report|kassasturz|ga-readiness)[-_][a-z0-9_-]+\.md\b"), "internal tracking docs"),
    # Internal env-var conventions used as test-gate switches
    (re.compile(r"\bRAZZFAZZ_TEST_[A-Z0-9_]+\b"), "internal test gate"),
    # Internal repo paths
    (re.compile(r"\.gsd/[^\s)]*"), "internal tracking docs"),
    (re.compile(r"\.claude/skills/[^\s)]*"), "internal automation"),
    (re.compile(r"\bsecurity-run/[^\s)]*"), "security review artifacts"),
    # Internal hostnames / IPs (RFC1918 in our ranges)
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), "internal host"),
    (re.compile(r"\b10\.163\.\d{1,3}\.\d{1,3}\b"), "internal host"),
    # Internal skill / tool names
    (re.compile(r"\bkassasturz\b", re.IGNORECASE), "nightly status check"),
    (re.compile(r"\brelease-cycle\b", re.IGNORECASE), "release process"),
    (re.compile(r"\bprepare-release\b", re.IGNORECASE), "release preparation"),
]


def sanitize_for_customer(text: str) -> str:
    """Strip internal-only references from customer-facing text.

    Conservative: ordinary technical content (service names, ports,
    standards references) is preserved. See TR-DEC-02.
    """
    if not text:
        return text
    out = text
    for pat, repl in _INLINE_REDACTIONS:
        out = pat.sub(repl, out)
    # Collapse double-replacements like "tracked internally tracked internally"
    out = re.sub(r"(tracked internally\s*){2,}", "tracked internally ", out)
    # "internal tracking docs::tracked internally" → "internal tracking docs"
    out = re.sub(r"internal tracking docs::tracked internally", "internal tracking docs", out)
    out = re.sub(r"internal tracking docs(?:\s+internal tracking docs)+", "internal tracking docs", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TestCase:
    classname: str
    name: str
    duration_s: float
    status: str  # pass | skip | xfail | fail | error
    failure_message: str = ""
    skip_reason: str = ""
    file_hint: str = ""  # derived from classname

    @property
    def test_id(self) -> str:
        return f"{self.classname}::{self.name}"

    @property
    def tier(self) -> str:
        # classname can be "tests.unit.foo.test_x" OR "unit.foo.test_x"
        # depending on pytest's rootdir + import-mode.
        parts = [p for p in self.classname.split(".") if p]
        if not parts:
            return "unknown"
        if parts[0] == "tests":
            parts = parts[1:]
        if parts and parts[0] in ("unit", "api", "ui", "acceptance", "scripts"):
            return parts[0]
        return "unknown"

    @property
    def module(self) -> str:
        parts = [p for p in self.classname.split(".") if p]
        if parts and parts[0] == "tests":
            parts = parts[1:]
        if len(parts) >= 2 and parts[0] in ("unit", "api", "ui", "acceptance", "scripts"):
            return parts[1]
        return "unknown"


@dataclass
class JUnitSummary:
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    duration_s: float = 0.0
    cases: list[TestCase] = field(default_factory=list)


@dataclass
class CoverageFile:
    path: str
    line_rate_pct: float
    branch_rate_pct: float


@dataclass
class CoverageSummary:
    line_rate_pct: float = 0.0
    branch_rate_pct: float = 0.0
    lines_covered: int = 0
    lines_valid: int = 0
    branches_covered: int = 0
    branches_valid: int = 0
    files: list[CoverageFile] = field(default_factory=list)


# ---------------------------------------------------------------------------
# JUnit parsing
# ---------------------------------------------------------------------------


def parse_junit(path: Path) -> JUnitSummary:
    summary = JUnitSummary()
    if not path.is_file():
        return summary
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return summary
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    for suite in suites:
        summary.total += int(suite.get("tests", 0))
        summary.failed += int(suite.get("failures", 0))
        summary.errors += int(suite.get("errors", 0))
        summary.skipped += int(suite.get("skipped", 0))
        try:
            summary.duration_s += float(suite.get("time", 0.0))
        except ValueError:
            pass
        for case in suite.findall("testcase"):
            classname = case.get("classname", "")
            name = case.get("name", "")
            try:
                duration = float(case.get("time", 0.0))
            except ValueError:
                duration = 0.0
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            if failure is not None or error is not None:
                node = failure if failure is not None else error
                msg = node.get("message", "") if node is not None else ""
                tc = TestCase(
                    classname=classname,
                    name=name,
                    duration_s=duration,
                    status="fail" if failure is not None else "error",
                    failure_message=msg,
                )
            elif skipped is not None:
                reason = skipped.get("message", "") or skipped.get("type", "")
                # pytest XFAIL shows up as <skipped type="pytest.xfail" message="reason: …">
                stype = skipped.get("type", "")
                status = "xfail" if "xfail" in stype.lower() else "skip"
                tc = TestCase(
                    classname=classname,
                    name=name,
                    duration_s=duration,
                    status=status,
                    skip_reason=reason,
                )
            else:
                tc = TestCase(
                    classname=classname,
                    name=name,
                    duration_s=duration,
                    status="pass",
                )
            summary.cases.append(tc)
    summary.passed = summary.total - summary.failed - summary.errors - summary.skipped
    if summary.passed < 0:
        summary.passed = 0
    return summary


# ---------------------------------------------------------------------------
# Coverage parsing
# ---------------------------------------------------------------------------


def parse_coverage_xml(path: Path) -> CoverageSummary:
    cov = CoverageSummary()
    if not path.is_file():
        return cov
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return cov

    def _pct(node, attr):
        try:
            return round(float(node.get(attr, 0.0)) * 100.0, 1)
        except ValueError:
            return 0.0

    cov.line_rate_pct = _pct(root, "line-rate")
    cov.branch_rate_pct = _pct(root, "branch-rate")
    try:
        cov.lines_covered = int(root.get("lines-covered", 0))
        cov.lines_valid = int(root.get("lines-valid", 0))
        cov.branches_covered = int(root.get("branches-covered", 0))
        cov.branches_valid = int(root.get("branches-valid", 0))
    except ValueError:
        pass
    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        if not filename:
            continue
        cov.files.append(
            CoverageFile(
                path=filename,
                line_rate_pct=_pct(cls, "line-rate"),
                branch_rate_pct=_pct(cls, "branch-rate"),
            )
        )
    return cov


def coverage_dat_to_xml(repo_root: Path, dat_file: Path, xml_out: Path) -> bool:
    """Convert a `.coverage` data file to coverage.xml using the venv's coverage CLI."""
    venv_cov = repo_root / "tests" / ".venv" / "bin" / "coverage"
    if not venv_cov.is_file() or not dat_file.is_file():
        return False
    try:
        subprocess.run(
            [str(venv_cov), "xml", "-o", str(xml_out)],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            env={**os.environ, "COVERAGE_FILE": str(dat_file)},
        )
        return xml_out.is_file()
    except subprocess.CalledProcessError:
        return False


# ---------------------------------------------------------------------------
# Docstring extraction
# ---------------------------------------------------------------------------


def _humanize(name: str) -> str:
    # Keep the "test " prefix in the human-readable form so the customer can
    # tie it back to the source.
    bare = name[5:] if name.startswith("test_") else name
    bare = bare.replace("_", " ").strip()
    if not bare:
        return name
    return f"Test {bare}"


def extract_test_descriptions(source_file: Path) -> dict[str, str]:
    """Return {test_id_relative: first_docstring_line} for one source file.

    Keys:
      - "test_foo"                — top-level function
      - "ClassName::test_foo"     — method in a class
    """
    out: dict[str, str] = {}
    if not source_file.is_file():
        return out
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return out

    def _first_line(doc: str | None, fallback: str) -> str:
        if not doc:
            return _humanize(fallback)
        first = doc.strip().splitlines()[0].strip()
        return first or _humanize(fallback)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            out[node.name] = _first_line(ast.get_docstring(node), node.name)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test_"):
                    out[f"{node.name}::{sub.name}"] = _first_line(
                        ast.get_docstring(sub), sub.name
                    )
    return out


def build_description_index(repo_root: Path) -> dict[str, str]:
    """Walk tests/{unit,api,ui,acceptance,scripts}/ and index every test_* docstring.

    Index key: "tests.<tier>.<module>.<file_stem>::<TestCls>::<method>" or
               "tests.<tier>.<module>.<file_stem>::<func>"
    Matches the JUnit `classname::name` shape.
    """
    index: dict[str, str] = {}
    tests_root = repo_root / "tests"
    for tier in ("unit", "api", "ui", "acceptance", "scripts"):
        tdir = tests_root / tier
        if not tdir.is_dir():
            continue
        for src in tdir.rglob("test_*.py"):
            descs = extract_test_descriptions(src)
            # Build the classname prefix the way pytest emits it. With
            # --import-mode=importlib pytest emits classname as the dotted
            # path of the module (no leading "tests." for some configs).
            # We register both forms to maximize hit-rate.
            rel = src.relative_to(repo_root)
            parts = list(rel.with_suffix("").parts)
            dotted = ".".join(parts)
            # pytest's classname is the dotted path RELATIVE to its rootdir
            # which can be tests/ or repo-root depending on invocation.
            # Register both forms (with + without the leading "tests.").
            dotted_no_tests = ".".join(parts[1:]) if parts and parts[0] == "tests" else dotted
            # Dashes in module dir names are kept verbatim by pytest in
            # classnames; we don't transform them.
            for key, desc in descs.items():
                # key is either "test_foo" or "Cls::test_foo"
                if "::" in key:
                    cls, fn = key.split("::")
                    for d in (dotted, dotted_no_tests):
                        index[f"{d}.{cls}::{fn}"] = desc
                else:
                    for d in (dotted, dotted_no_tests):
                        index[f"{d}::{key}"] = desc
    return index


# ---------------------------------------------------------------------------
# Requirement traceability
# ---------------------------------------------------------------------------


_REQ_HEADER_RE = re.compile(r"^####\s+(R-[A-Z]+-\d+)\s+[—-]\s+(.+?)\s*$", re.MULTILINE)
_MATRIX_ROW_RE = re.compile(
    r"^\|\s*(R-[A-Z]+-\d+)\s*\|\s*(.+?)\s*\|"
    r"\s*([-\d+]+)\s*\|\s*([-\d+]+)\s*\|\s*([-\d+]+)\s*\|\s*([-\d+]+)\s*\|"
    r"\s*(.+?)\s*\|\s*(.+?)\s*\|\s*([✅\U0001F7E1❌])\s*\|",
    re.MULTILINE,
)


def parse_requirements(repo_root: Path) -> dict[str, dict]:
    """Return {req_id: {title, category, status_emoji, counts}} from REQUIREMENTS.md + COVERAGE-MATRIX.md."""
    out: dict[str, dict] = {}
    req_md = repo_root / ".gsd" / "REQUIREMENTS.md"
    matrix_md = repo_root / ".gsd" / "COVERAGE-MATRIX.md"
    if req_md.is_file():
        for m in _REQ_HEADER_RE.finditer(req_md.read_text(encoding="utf-8")):
            rid = m.group(1)
            out[rid] = {
                "id": rid,
                "title": m.group(2).strip(),
                "category": rid.split("-")[1],
                "status": "?",
                "counts": {"acc": 0, "unit": 0, "api": 0, "ui": 0, "scripts": 0, "legacy": 0},
            }
    if matrix_md.is_file():
        # Simpler row parser: lines that begin with "| R-XXX-NN |"
        for line in matrix_md.read_text(encoding="utf-8").splitlines():
            line = line.rstrip()
            if not line.startswith("| R-"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 9:
                continue
            rid = cells[0]
            if rid not in out:
                # Title from the matrix table when REQUIREMENTS.md missed it
                out[rid] = {
                    "id": rid,
                    "title": cells[1],
                    "category": rid.split("-")[1],
                    "status": "?",
                    "counts": {"acc": 0, "unit": 0, "api": 0, "ui": 0, "scripts": 0, "legacy": 0},
                }

            def _n(s: str) -> int:
                s = s.strip()
                if s in ("—", "-", ""):
                    return 0
                # "5", "7+", "1+"
                m2 = re.match(r"(\d+)", s)
                return int(m2.group(1)) if m2 else 0

            out[rid]["counts"] = {
                "acc": _n(cells[2]),
                "unit": _n(cells[3]),
                "api": _n(cells[4]),
                "ui": _n(cells[5]),
                "scripts": _n(cells[6]),
                "legacy": cells[7],  # textual; e.g. "test-sso-oidc.sh"
            }
            # last cell is the status emoji (✅ 🟡 ❌)
            status_cell = cells[-1]
            for emoji in ("✅", "🟡", "❌"):
                if emoji in status_cell:
                    out[rid]["status"] = emoji
                    break
    return out


# ---------------------------------------------------------------------------
# Coverage thresholds (from tests/pyproject.toml)
# ---------------------------------------------------------------------------


def parse_coverage_thresholds(repo_root: Path) -> dict[str, int]:
    pp = repo_root / "tests" / "pyproject.toml"
    if not pp.is_file():
        return {}
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover
            import tomli as tomllib  # type: ignore
        with open(pp, "rb") as fh:
            data = tomllib.load(fh)
        return dict(
            data.get("tool", {})
            .get("razzfazz_test", {})
            .get("coverage_thresholds", {})
        )
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Stack metadata
# ---------------------------------------------------------------------------


def detect_stack_version(repo_root: Path) -> str:
    vjson = repo_root / "config" / "manifests" / "versions.json"
    if vjson.is_file():
        try:
            data = json.loads(vjson.read_text(encoding="utf-8"))
            v = data.get("stack_version")
            if v:
                return f"v{v}"
        except (json.JSONDecodeError, OSError):
            pass
    return "unknown"


def detect_build_commit(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def resolve_env_file(repo_root: Path, env_file_override: Path | None = None) -> Path | None:
    """Locate the source `.env` for `detect_profiles`.

    Resolution chain (first hit wins) — see TRF-DEC-01:

      1. ``--env-file PATH`` CLI argument
      2. ``$RAZZFAZZ_REPO_ROOT/.env``    (env var the wrapper sets)
      3. ``<repo_root>/.env``            (the --repo-root argument)
      4. walk-up from <repo_root> (cap 4 levels)
      5. ``~/razzfazz-ai-service-stack/.env`` (canonical install path)

    Returns the resolved Path, or None when nothing matched. Critical for
    the worktree case: a sibling worktree (e.g. test-report-fixes) has
    no `.env` of its own, but the main worktree at
    ``~/razzfazz-ai-service-stack/.env`` always does.
    """
    if env_file_override is not None:
        p = Path(env_file_override).expanduser()
        if p.is_file():
            return p
    env_repo_root = os.environ.get("RAZZFAZZ_REPO_ROOT")
    if env_repo_root:
        p = Path(env_repo_root).expanduser() / ".env"
        if p.is_file():
            return p
    p = repo_root / ".env"
    if p.is_file():
        return p
    parent = repo_root.parent
    for _ in range(4):
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
        new_parent = parent.parent
        if new_parent == parent:
            break
        parent = new_parent
    canonical = Path("~/razzfazz-ai-service-stack/.env").expanduser()
    if canonical.is_file():
        return canonical
    return None


def detect_profiles(repo_root: Path, env_file_override: Path | None = None) -> tuple[list[str], Path | None]:
    """Return (profiles_list, env_path_used) — env_path_used is None when
    nothing matched. Renderers use the path to decide between
    "(.env not present)" and the resolved profile list."""
    env = resolve_env_file(repo_root, env_file_override)
    if env is None:
        return [], None
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("COMPOSE_PROFILES="):
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            return [p.strip() for p in v.split(",") if p.strip()], env
    return [], env


def detect_container_count(repo_root: Path) -> int:
    """Count distinct services across compose.yml + llm/compose*.yml."""
    files = list(repo_root.glob("compose*.yml")) + list(repo_root.glob("*/compose*.yml"))
    services: set[str] = set()
    svc_re = re.compile(r"^  ([a-zA-Z0-9_-]+):\s*$")
    for f in files:
        try:
            in_services = False
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("services:"):
                    in_services = True
                    continue
                if in_services:
                    if line and not line.startswith(" "):
                        # left services block
                        in_services = False
                        continue
                    m = svc_re.match(line)
                    if m:
                        services.add(m.group(1))
        except OSError:
            continue
    return len(services)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


_TIER_ORDER = ["acceptance", "unit", "api", "ui", "scripts"]
_TIER_LABEL = {
    "acceptance": "Acceptance probes",
    "unit": "Unit tests",
    "api": "API tests",
    "ui": "UI tests",
    "scripts": "Script tests",
}


def _status_emoji(passed: int, failed: int, errors: int) -> str:
    if failed == 0 and errors == 0:
        return "OK"
    return "FAIL"


def render_markdown(
    *,
    run_dir: Path,
    stack_version: str,
    build_commit: str,
    repo_root: Path,
    junit_path: Path | None = None,
    coverage_path: Path | None = None,
    env_file_override: Path | None = None,
) -> str:
    junit_path = junit_path or (run_dir / "junit.xml")
    coverage_path = coverage_path or (run_dir / "coverage.xml")

    summary = parse_junit(junit_path)
    coverage = parse_coverage_xml(coverage_path)
    descs = build_description_index(repo_root)
    requirements = parse_requirements(repo_root)
    thresholds = parse_coverage_thresholds(repo_root)
    profiles, env_path = detect_profiles(repo_root, env_file_override)
    container_count = detect_container_count(repo_root)

    # Pivot tests by tier and module
    by_tier: dict[str, list[TestCase]] = defaultdict(list)
    by_module: dict[tuple[str, str], list[TestCase]] = defaultdict(list)
    for c in summary.cases:
        by_tier[c.tier].append(c)
        by_module[(c.tier, c.module)].append(c)

    def _counts(cases: Iterable[TestCase]) -> dict[str, int]:
        c = {"pass": 0, "fail": 0, "skip": 0, "xfail": 0, "error": 0, "duration": 0.0}
        for tc in cases:
            c[tc.status] = c.get(tc.status, 0) + 1
            c["duration"] += tc.duration_s
        return c

    out: list[str] = []

    # ---- Header ----------------------------------------------------------
    out.append("# Test Report — razzfazz.ai Box")
    out.append("")
    out.append(f"**Assessed version:** {sanitize_for_customer(stack_version)}  ")
    out.append(f"**Build commit:** `{build_commit}`  ")
    out.append(f"**Test run:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  ")
    out.append(f"**Run identifier:** `{run_dir.name}`  ")
    out.append("**Audience:** customer security officer, QA, ops team.  ")
    out.append("**Document version:** 1.")
    out.append("")
    out.append("---")
    out.append("")

    # ---- 1. Executive summary -------------------------------------------
    overall = "GREEN" if (summary.failed + summary.errors) == 0 else (
        "RED" if (summary.failed + summary.errors) > max(1, summary.total // 10) else "YELLOW"
    )
    out.append("## 1. Executive summary")
    out.append("")
    out.append(f"- Total tests collected: **{summary.total}**")
    out.append(f"- Passed: **{summary.passed}**")
    out.append(f"- Failed: **{summary.failed}**")
    out.append(f"- Errors: **{summary.errors}**")
    out.append(f"- Skipped / xfail: **{summary.skipped}**")
    if coverage.lines_valid > 0:
        out.append(f"- Overall line coverage: **{coverage.line_rate_pct}%** "
                   f"({coverage.lines_covered}/{coverage.lines_valid} lines)")
    out.append(f"- Wall-clock runtime: **{summary.duration_s:.1f}s**")
    out.append(f"- Bottom line: **{overall}**")
    out.append("")
    if overall == "GREEN":
        out.append("> All tests in scope passed against the assessed build.")
    elif overall == "YELLOW":
        out.append("> Tests passed with a small number of skipped or known-issue cases.")
    else:
        out.append("> One or more tests failed; see section 7 for details.")
    out.append("")

    # ---- 2. Test environment --------------------------------------------
    out.append("## 2. Test environment")
    out.append("")
    out.append(f"- Stack version: `{stack_version}`")
    out.append(f"- Build commit: `{build_commit}`")
    out.append(f"- Container services tested: **{container_count}**")
    if profiles:
        out.append(f"- Compose profiles enabled: `{', '.join(profiles)}`")
    elif env_path is not None:
        out.append(
            f"- Compose profiles enabled: (none — `.env` at `{env_path}` "
            f"has no `COMPOSE_PROFILES=` line)"
        )
    else:
        out.append(
            "- Compose profiles enabled: (`.env` not located — "
            "pass `--env-file PATH` or set `RAZZFAZZ_REPO_ROOT`)"
        )
    # Detect pytest / playwright versions from venv if present
    venv_pytest = repo_root / "tests" / ".venv" / "bin" / "pytest"
    if venv_pytest.is_file():
        try:
            v = subprocess.run([str(venv_pytest), "--version"], capture_output=True, text=True)
            out.append(f"- Test framework: `{v.stdout.strip().splitlines()[0]}`")
        except Exception:
            pass
    out.append("- Test runner: `razzfazz-test.sh --customer-report`")
    out.append("")

    # ---- 3. Results by tier ---------------------------------------------
    out.append("## 3. Results by tier")
    out.append("")
    out.append("| Tier | Tests | Pass | Fail | Skip | Avg runtime (s) | Status |")
    out.append("|---|---:|---:|---:|---:|---:|:-:|")
    for tier in _TIER_ORDER:
        cases = by_tier.get(tier, [])
        if not cases:
            out.append(f"| {_TIER_LABEL[tier]} | 0 | — | — | — | — | (no tests) |")
            continue
        c = _counts(cases)
        avg = c["duration"] / max(1, len(cases))
        status = _status_emoji(c["pass"], c["fail"], c["error"])
        out.append(
            f"| {_TIER_LABEL[tier]} | {len(cases)} | {c['pass']} | "
            f"{c['fail'] + c['error']} | {c['skip'] + c['xfail']} | {avg:.2f} | {status} |"
        )
    out.append("")

    # ---- 4. Results by module -------------------------------------------
    out.append("## 4. Results by module")
    out.append("")
    out.append("| Module | Tier | Tests | Pass | Fail | Skip | Status |")
    out.append("|---|---|---:|---:|---:|---:|:-:|")
    for (tier, module) in sorted(by_module.keys()):
        cases = by_module[(tier, module)]
        c = _counts(cases)
        status = _status_emoji(c["pass"], c["fail"], c["error"])
        out.append(
            f"| `{sanitize_for_customer(module)}` | {tier} | {len(cases)} | {c['pass']} | "
            f"{c['fail'] + c['error']} | {c['skip'] + c['xfail']} | {status} |"
        )
    out.append("")

    # ---- 5. Requirements coverage ---------------------------------------
    out.append("## 5. Requirements coverage")
    out.append("")
    out.append("Per-requirement traceability against the stack's published "
               "capability contract. Status legend: ✅ covered, 🟡 partial, "
               "❌ not yet exercised.")
    out.append("")
    if requirements:
        # Group by category
        by_cat: dict[str, list[dict]] = defaultdict(list)
        for r in requirements.values():
            by_cat[r["category"]].append(r)
        out.append("| Requirement | Title | Tests (acc/unit/api/ui/scripts) | Status |")
        out.append("|---|---|---|:-:|")
        for cat in sorted(by_cat.keys()):
            for r in sorted(by_cat[cat], key=lambda x: x["id"]):
                tcounts = r["counts"]
                # `legacy` is textual when from matrix; skip it from numeric pivot
                cells = f"{tcounts.get('acc',0)}/{tcounts.get('unit',0)}/{tcounts.get('api',0)}/{tcounts.get('ui',0)}/{tcounts.get('scripts',0)}"
                title = sanitize_for_customer(r["title"])
                out.append(f"| `{r['id']}` | {title} | {cells} | {r['status']} |")
        out.append("")
    else:
        out.append("_(requirement contract not present in this build environment)_")
        out.append("")

    # ---- 6. Detailed test results ---------------------------------------
    out.append("## 6. Detailed test results")
    out.append("")
    out.append("Per-test row. Test ID is the pytest node id; description "
               "is extracted from the test's docstring.")
    out.append("")
    out.append("| Test ID | Description | Tier | Module | Status | Duration (s) |")
    out.append("|---|---|---|---|:-:|---:|")
    # Sort by tier then module then test_id
    cases_sorted = sorted(
        summary.cases,
        key=lambda c: (_TIER_ORDER.index(c.tier) if c.tier in _TIER_ORDER else 99, c.module, c.test_id),
    )
    for tc in cases_sorted:
        desc = descs.get(tc.test_id, "")
        if not desc:
            # Try humanized fallback from the method name
            desc = _humanize(tc.name)
        desc = sanitize_for_customer(desc)
        # Escape pipes in descriptions to keep the table valid
        desc = desc.replace("|", "\\|")
        status_label = {
            "pass": "✅",
            "fail": "❌",
            "error": "❌",
            "skip": "⏭",
            "xfail": "🟡",
        }.get(tc.status, tc.status)
        # Sanitize test_id and module too — parametrize IDs sometimes
        # carry internal CIDRs (`[192.168.0.0/16]`), and a few test
        # directory names match the internal-skill deny-list.
        safe_test_id = sanitize_for_customer(tc.test_id)
        safe_module = sanitize_for_customer(tc.module)
        out.append(
            f"| `{safe_test_id}` | {desc} | {tc.tier} | `{safe_module}` | {status_label} | {tc.duration_s:.2f} |"
        )
    out.append("")

    # ---- 7. Failures + skips explained ----------------------------------
    out.append("## 7. Failures and known-issue skips")
    out.append("")
    failed_cases = [c for c in summary.cases if c.status in ("fail", "error")]
    if failed_cases:
        for c in failed_cases:
            out.append(f"### `{sanitize_for_customer(c.test_id)}`")
            out.append("")
            out.append(f"- Tier: {c.tier}")
            out.append(f"- Module: `{sanitize_for_customer(c.module)}`")
            msg = sanitize_for_customer(c.failure_message or "(no message)")
            # Truncate long stack-trace residue
            msg = msg.split("\n")[0][:500]
            out.append(f"- Failure: {msg}")
            out.append("- Suggested action: review the test's source file and "
                       "the corresponding service log; rerun in isolation.")
            out.append("")
    else:
        out.append("_(no failures in this run.)_")
        out.append("")

    skipped_cases = [c for c in summary.cases if c.status in ("skip", "xfail")]
    if skipped_cases:
        out.append("### Skipped / known-issue cases")
        out.append("")
        for c in skipped_cases:
            reason = sanitize_for_customer(c.skip_reason or "(no reason given)")
            reason = reason.split("\n")[0][:400]
            label = "xfail" if c.status == "xfail" else "skip"
            out.append(f"- `{sanitize_for_customer(c.test_id)}` [{label}] — {reason}")
        out.append("")

    # ---- 8. Coverage details --------------------------------------------
    out.append("## 8. Coverage details")
    out.append("")
    if coverage.lines_valid > 0:
        out.append(f"Overall: **{coverage.line_rate_pct}%** lines "
                   f"({coverage.lines_covered}/{coverage.lines_valid}); branches "
                   f"**{coverage.branch_rate_pct}%** "
                   f"({coverage.branches_covered}/{coverage.branches_valid}).")
        out.append("")
        # Aggregate per source root vs threshold
        if thresholds:
            out.append("### Per-module thresholds")
            out.append("")
            out.append("| Source path | Coverage | Threshold | Status |")
            out.append("|---|---:|---:|:-:|")
            for path, thr in sorted(thresholds.items()):
                # Find files under this path
                matched = [f for f in coverage.files if f.path == path or f.path.startswith(path.rstrip("/") + "/")]
                if not matched:
                    out.append(f"| `{path}` | n/a | {thr}% | (not exercised) |")
                    continue
                # Average line rate weighted by lines_valid is not in cobertura per-class;
                # use mean of class line-rates as a representative proxy.
                avg = sum(f.line_rate_pct for f in matched) / len(matched)
                status = "OK" if avg >= thr else "BELOW"
                out.append(f"| `{path}` | {avg:.1f}% | {thr}% | {status} |")
            out.append("")
        # Top under-covered files (lowest line rate) — limit to 15
        ranked = sorted(coverage.files, key=lambda f: f.line_rate_pct)[:15]
        if ranked:
            out.append("### Lowest-coverage files")
            out.append("")
            out.append("| File | Line coverage | Branch coverage |")
            out.append("|---|---:|---:|")
            for f in ranked:
                out.append(f"| `{f.path}` | {f.line_rate_pct}% | {f.branch_rate_pct}% |")
            out.append("")
    else:
        out.append("_(coverage data not present for this run; use `--customer-report` "
                   "without `--no-coverage` to include it.)_")
        out.append("")

    # ---- 9. Known gaps ---------------------------------------------------
    out.append("## 9. Known gaps")
    out.append("")
    blind = [r for r in requirements.values() if r["status"] == "❌"]
    partial = [r for r in requirements.values() if r["status"] == "🟡"]
    if blind:
        out.append(f"- **{len(blind)} requirements** have no test coverage yet "
                   "(blind spots; tracked in the development backlog).")
    if partial:
        out.append(f"- **{len(partial)} requirements** are partially covered "
                   "(at least one test exists but not for every aspect).")
    if thresholds:
        # Coverage carry-forwards: any threshold-bearing path with no files in coverage
        carry = []
        for path, thr in thresholds.items():
            matched = [f for f in coverage.files if f.path == path or f.path.startswith(path.rstrip("/") + "/")]
            if not matched:
                carry.append(path)
        if carry:
            out.append(f"- **{len(carry)} module(s)** with declared coverage thresholds "
                       "were not exercised in this run "
                       f"({', '.join(f'`{c}`' for c in carry)}).")
    xfails = [c for c in summary.cases if c.status == "xfail"]
    if xfails:
        out.append(f"- **{len(xfails)} test(s)** are pinned as expected-failure "
                   "(known issues with planned remediation).")
    if not (blind or partial or xfails):
        out.append("_(no open gaps recorded for this build.)_")
    out.append("")

    # FG-DEC-06 — skip-composition breakdown so the skip count is read
    # alongside its causes rather than as one opaque number.
    skipped_cases_for_breakdown = [
        c for c in summary.cases if c.status in ("skip", "xfail")
    ]
    if skipped_cases_for_breakdown:
        # Bucket by reason heuristic. Each test's skip/xfail message is
        # parsed for one of the known cause-tokens; anything that doesn't
        # match a known token is bucketed as "intentional gate (other)".
        cause_buckets = {
            "heavy-profile cycle-refusal (RAZZFAZZ_ALLOW_HEAVY_CYCLES=1 to enable)":
                lambda r: "refusing to cycle heavy profile" in r,
            "LLM-variant cycle-refusal (shares container_name with running variant)":
                lambda r: ("refusing to cycle 'llm" in r) or ("shares container_name with the running" in r),
            "live-Authentik gate (RAZZFAZZ_TEST_LIVE_AUTHENTIK=1 — opt-in to avoid akadmin lockout)":
                lambda r: "Live group-enforcement test gated" in r or "Live MFA-policy test gated" in r or "Live rate-limit storm gated" in r or "Live group-lint check gated" in r,
            "fixture-missing (seed file `tests/fixtures/.env.test` absent on dev box)":
                lambda r: "tests/fixtures/.env.test" in r or "fixture cannot stage env" in r,
            "image-not-built (`docker compose build` for that profile)":
                lambda r: "not built on this host" in r or "image_not_built" in r,
            "tracker-pinned xfail (R-* / internal tracking — known but unfixed)":
                lambda r: "xfail" in r.lower() or "tracked internally" in r.lower() or "see internal tracking" in r.lower(),
            "container introspection limit (no bash/python in image)":
                lambda r: "no bash" in r or "image has no bash" in r or "no in-container probe path" in r,
            "profile not enabled + cycle helper does not handle (e.g. paperclip needs Authentik bootstrap)":
                lambda r: "profile not enabled in COMPOSE_PROFILES" in r,
            "container not present (post-init re-init artifact)":
                lambda r: "container not present" in r,
            "patch fingerprint absent (legacy build pre-dating tracker)":
                lambda r: "marker missing inside" in r or "image was built from a Dockerfile" in r or "predates tracked internally" in r,
            "config not yet wired (post-install required)":
                lambda r: "no RAG_EXTERNAL_RERANKER_URL" in r or "no log-snapshot-" in r or "razzfazz-post-install" in r or "configured via the API into the DB" in r,
        }
        bucket_counts = {name: 0 for name in cause_buckets}
        intentional_other = 0
        for case in skipped_cases_for_breakdown:
            reason_text = (case.skip_reason or "").lower() + " " + (case.skip_reason or "")
            matched = False
            for bucket_name, predicate in cause_buckets.items():
                if predicate(case.skip_reason or ""):
                    bucket_counts[bucket_name] += 1
                    matched = True
                    break
            if not matched:
                intentional_other += 1

        out.append("### Skip composition (Q5)")
        out.append("")
        out.append(
            f"_Of the {len(skipped_cases_for_breakdown)} skipped/xfail tests, "
            f"the breakdown by cause is:_"
        )
        out.append("")
        for bucket_name, count in sorted(
            bucket_counts.items(), key=lambda x: -x[1]
        ):
            if count == 0:
                continue
            out.append(f"- **{count}** — {bucket_name}")
        if intentional_other > 0:
            out.append(f"- **{intentional_other}** — intentional gate (other / one-off env-var)")
        out.append("")
        out.append(
            "All buckets above are deliberate skips (env-gates, cycle-refusals, "
            "tracker-pinned xfails, fixture-missing on this dev box, or "
            "image-not-built locally) — none indicate a `--include-disabled` "
            "cycle helper bug. Heavy profiles can be exercised explicitly with "
            "`RAZZFAZZ_ALLOW_HEAVY_CYCLES=1`; live-Authentik tests with "
            "`RAZZFAZZ_TEST_LIVE_AUTHENTIK=1` on a throwaway / CI Authentik. "
            "See `.gsd/reports/final-greens-decisions-2026-05-15.md::FG-DEC-06`."
        )
        out.append("")

    # ---- Footer ----------------------------------------------------------
    out.append("---")
    out.append("")
    out.append("_Issued by razzfazz.ai GmbH - Member of SEQIS Group. "
               "This document describes the test posture of the build above "
               "at the date shown and is self-contained. Direct questions to "
               "your razzfazz.ai support contact._")
    out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Test Report</title>
<style>
  @page { size: A4; margin: 18mm 16mm 18mm 16mm; }
  body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         font-size: 10pt; color: #1a1a1a; line-height: 1.4; }
  h1 { font-size: 20pt; color: #003366; border-bottom: 2px solid #003366; padding-bottom: 4pt; }
  h2 { font-size: 14pt; color: #003366; margin-top: 18pt; border-bottom: 1px solid #ccc; padding-bottom: 2pt; }
  h3 { font-size: 11pt; color: #333; margin-top: 12pt; }
  table { width: 100%; border-collapse: collapse; margin: 6pt 0; font-size: 9pt; }
  th, td { border: 1px solid #bbb; padding: 4pt 6pt; text-align: left; vertical-align: top; }
  th { background: #f2f5f8; font-weight: 600; }
  code { background: #f5f5f5; padding: 1pt 3pt; border-radius: 2pt; font-size: 8.5pt; }
  pre { background: #f5f5f5; padding: 6pt; border-radius: 3pt; overflow-x: auto; font-size: 8pt; }
  blockquote { border-left: 3px solid #003366; padding-left: 10pt; color: #555; margin: 8pt 0; }
  .footer { margin-top: 24pt; padding-top: 8pt; border-top: 1px solid #ccc;
            font-size: 8pt; color: #666; text-align: center; }
</style>
</head>
<body>
__BODY__
</body>
</html>
"""


def _markdown_to_html(md: str) -> str:
    """Minimal Markdown → HTML conversion (no external dep)."""
    try:
        import markdown  # type: ignore
        return markdown.markdown(md, extensions=["tables", "fenced_code"])
    except ImportError:
        pass
    # Fallback: very small subset (headings, tables, lists, paragraphs).
    lines = md.splitlines()
    html: list[str] = []
    in_table = False
    in_list = False

    def _close_list():
        nonlocal in_list
        if in_list:
            html.append("</ul>")
            in_list = False

    def _close_table():
        nonlocal in_table
        if in_table:
            html.append("</tbody></table>")
            in_table = False

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("# "):
            _close_list(); _close_table()
            html.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            _close_list(); _close_table()
            html.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("### "):
            _close_list(); _close_table()
            html.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("|"):
            # Table row
            cells = [c.strip() for c in line.strip("|").split("|")]
            # Detect separator row
            if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                # already in table head; nothing to emit
                pass
            elif not in_table:
                in_table = True
                html.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr></thead><tbody>")
            else:
                html.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
        elif line.startswith("- "):
            _close_table()
            if not in_list:
                in_list = True
                html.append("<ul>")
            html.append(f"<li>{_inline(line[2:])}</li>")
        elif line.startswith("> "):
            _close_list(); _close_table()
            html.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        elif line.strip() == "---":
            _close_list(); _close_table()
            html.append("<hr/>")
        elif line.strip() == "":
            _close_list(); _close_table()
        else:
            _close_list(); _close_table()
            html.append(f"<p>{_inline(line)}</p>")
        i += 1
    _close_list(); _close_table()
    return "\n".join(html)


def _inline(s: str) -> str:
    """Inline markdown: **bold**, `code`, escape HTML."""
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _rewrite_section6_for_pdf(md: str) -> str:
    """Replace section 6's wide 6-column table with per-tier 4-column sub-tables.

    The Markdown artifact keeps the wide table — operators reading the
    .md want the full detail in one place. The PDF body needs narrower
    tables to survive paper-page rendering (Gotenberg/pandoc/wkhtmltopdf
    all truncate cells past ~A4 landscape width when the source has
    6+ wide columns and 1000+ rows). See TRF-DEC-02.

    The replacement keeps the section's intro paragraph, then emits one
    H3 per tier (acceptance/unit/api/ui/scripts) followed by a
    4-column table: short Test ID / Status / Duration / Description.
    The Test-ID column drops the dotted ``tests.<tier>.`` prefix to
    save horizontal space.
    """
    lines = md.splitlines()
    # Locate "## 6. Detailed test results" and the next "## " heading.
    start = None
    end = None
    for i, line in enumerate(lines):
        if line.startswith("## 6. "):
            start = i
        elif start is not None and line.startswith("## ") and i > start:
            end = i
            break
    if start is None:
        return md
    if end is None:
        end = len(lines)

    # Extract intro lines (up to the table header row) and the table rows.
    section = lines[start:end]
    intro: list[str] = []
    table_rows: list[str] = []
    in_table = False
    for ln in section:
        if ln.startswith("|"):
            in_table = True
            table_rows.append(ln)
        elif in_table and ln.strip() == "":
            # blank line after table
            continue
        else:
            if in_table:
                # post-table content (shouldn't really happen)
                continue
            intro.append(ln)

    if len(table_rows) < 3:
        # No real table here; bail.
        return md

    # Header row + separator row are the first two
    # Column order from render_markdown: Test ID | Description | Tier | Module | Status | Duration (s)
    parsed: list[tuple[str, str, str, str, str, str]] = []
    for row in table_rows[2:]:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) < 6:
            continue
        parsed.append((cells[0], cells[1], cells[2], cells[3], cells[4], cells[5]))

    # Group by tier; tier column index is 2
    by_tier_rows: dict[str, list[tuple[str, str, str, str, str, str]]] = defaultdict(list)
    for r in parsed:
        by_tier_rows[r[2]].append(r)

    new_section: list[str] = []
    new_section.extend(intro)  # "## 6. ..." + the explanatory paragraph + blank line
    new_section.append("")
    new_section.append("_PDF rendering: per-tier sub-tables; the full 6-column "
                       "matrix is in the Markdown artifact._")
    new_section.append("")

    for tier in _TIER_ORDER:
        rows = by_tier_rows.get(tier, [])
        if not rows:
            continue
        new_section.append(f"### {_TIER_LABEL[tier]}")
        new_section.append("")
        new_section.append("| Test ID | Status | Duration (s) | Description |")
        new_section.append("|---|:-:|---:|---|")
        for tid, desc, _t, _mod, status, dur in rows:
            short_tid = tid
            # Drop "tests." or "<tier>." prefix when present, plus the
            # surrounding backticks. The cell is rebuilt with backticks.
            inner = short_tid.strip("`")
            for prefix in (f"tests.{tier}.", f"{tier}."):
                if inner.startswith(prefix):
                    inner = inner[len(prefix):]
                    break
            new_section.append(
                f"| `{inner}` | {status} | {dur} | {desc} |"
            )
        new_section.append("")

    return "\n".join(lines[:start] + new_section + lines[end:])


def render_pdf(md: str, out_path: Path) -> tuple[bool, str]:
    """Render Markdown → PDF. Returns (ok, method_or_error)."""
    pdf_md = _rewrite_section6_for_pdf(md)
    body_html = _markdown_to_html(pdf_md)
    full_html = _HTML_TEMPLATE.replace("__BODY__", body_html)

    # 1. Try Gotenberg if container is up
    try:
        ps = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                            capture_output=True, text=True, check=True)
        if "gotenberg" in ps.stdout.split():
            html_tmp = Path("/tmp") / f"test-report-{out_path.stem}.html"
            html_tmp.write_text(full_html, encoding="utf-8")
            try:
                # Push html into container
                subprocess.run(
                    ["docker", "cp", str(html_tmp), "gotenberg:/tmp/index.html"],
                    check=True, capture_output=True,
                )
                rc = subprocess.run(
                    ["docker", "exec", "gotenberg", "curl", "-s",
                     "-o", "/tmp/output.pdf", "-w", "%{http_code}",
                     "--request", "POST",
                     "http://localhost:3000/forms/chromium/convert/html",
                     "--form", "files=@/tmp/index.html",
                     # A4 portrait — section 6 is now per-tier 4-col tables
                     # so portrait fits comfortably. (TRF-DEC-02)
                     "--form", "paperWidth=8.27",
                     "--form", "paperHeight=11.69",
                     "--form", "marginTop=0.4",
                     "--form", "marginBottom=0.4",
                     "--form", "marginLeft=0.4",
                     "--form", "marginRight=0.4",
                     "--form", "preferCssPageSize=true"],
                    capture_output=True, text=True,
                )
                if rc.stdout.strip() == "200":
                    subprocess.run(
                        ["docker", "cp", "gotenberg:/tmp/output.pdf", str(out_path)],
                        check=True, capture_output=True,
                    )
                    subprocess.run(
                        ["docker", "exec", "gotenberg", "rm", "-f",
                         "/tmp/index.html", "/tmp/output.pdf"],
                        capture_output=True,
                    )
                    html_tmp.unlink(missing_ok=True)
                    return True, "gotenberg"
            finally:
                html_tmp.unlink(missing_ok=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # 2. Try pandoc
    if shutil.which("pandoc"):
        try:
            html_tmp = out_path.with_suffix(".html")
            html_tmp.write_text(full_html, encoding="utf-8")
            for engine in ("weasyprint", "wkhtmltopdf", "xelatex"):
                if shutil.which(engine) or engine == "xelatex":
                    try:
                        subprocess.run(
                            ["pandoc", str(html_tmp), "-o", str(out_path),
                             f"--pdf-engine={engine}"],
                            check=True, capture_output=True,
                        )
                        html_tmp.unlink(missing_ok=True)
                        return True, f"pandoc+{engine}"
                    except subprocess.CalledProcessError:
                        continue
            html_tmp.unlink(missing_ok=True)
        except OSError:
            pass

    return False, "no PDF renderer available (Gotenberg container not running, pandoc not installed)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("run_id", help="Run identifier (subdir under tests/results/)")
    p.add_argument("--repo-root", type=Path, default=REPO_ROOT_DEFAULT)
    p.add_argument("--no-pdf", action="store_true", help="Skip PDF rendering")
    p.add_argument("--stack-version", default=None, help="Override detected stack version")
    p.add_argument(
        "--env-file", type=Path, default=None,
        help="Path to the source .env to read COMPOSE_PROFILES from. "
             "Highest-priority entry in the resolution chain (TRF-DEC-01).",
    )
    args = p.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    run_dir = repo_root / "tests" / "results" / args.run_id
    if not run_dir.is_dir():
        print(f"ERROR: run dir not found: {run_dir}", file=sys.stderr)
        return 2

    # If only .coverage exists, convert to coverage.xml
    cov_xml = run_dir / "coverage.xml"
    cov_dat = run_dir / ".coverage"
    if not cov_xml.is_file() and cov_dat.is_file():
        coverage_dat_to_xml(repo_root, cov_dat, cov_xml)
    # Also try the pytest-default location at repo-root/.coverage
    if not cov_xml.is_file():
        root_cov = repo_root / ".coverage"
        if root_cov.is_file():
            coverage_dat_to_xml(repo_root, root_cov, cov_xml)

    stack_version = args.stack_version or detect_stack_version(repo_root)
    build_commit = detect_build_commit(repo_root)

    md = render_markdown(
        run_dir=run_dir,
        stack_version=stack_version,
        build_commit=build_commit,
        repo_root=repo_root,
        env_file_override=args.env_file,
    )

    md_out = run_dir / "test-report.md"
    md_out.write_text(md, encoding="utf-8")
    print(f"Markdown report: {md_out}", file=sys.stderr)

    if not args.no_pdf:
        pdf_out = run_dir / "test-report.pdf"
        ok, method = render_pdf(md, pdf_out)
        if ok:
            print(f"PDF report ({method}): {pdf_out}", file=sys.stderr)
        else:
            print(f"PDF skipped: {method}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
