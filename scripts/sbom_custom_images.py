#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Absolute CVE counts for the images we build, and a Critical-must-not-rise gate.

Our images cannot be diffed by CVE: the previous tag's local build is gone, so the
rescan of the previous release reuses THIS run's scan and every finding reads
PERSISTENT. Report them absolutely per cut, keyed by repository (a tag change keeps
the row), and compare Critical counts against the previous release's committed
cve-baseline.tsv.

Exit codes: 0 = no rise (or no baseline to compare against, which is reported),
2 = Critical rose on at least one image we build.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SEVS = ("Critical", "High")


def is_ours(safe: str) -> bool:
    return safe.startswith("razzfazz-") or safe.startswith("llama-runner__")


def repo_key(safe: str) -> str:
    return safe.split("__", 1)[0]


def _row(acc: dict, key: str) -> dict:
    return acc.setdefault(key, {s: 0 for s in SEVS})


def counts_from_grype_dir(d: Path) -> dict:
    """Distinct (cve, severity) per repository, the same unit the baseline TSV counts:
    one CVE on two packages, or on two tags of one repository, is one finding."""
    seen: dict = {}
    for gf in sorted(Path(d).glob("*.grype.json")):
        safe = gf.name[: -len(".grype.json")]
        if not is_ours(safe):
            continue
        rows = seen.setdefault(repo_key(safe), set())
        try:
            doc = json.loads(gf.read_text() or "{}")
        except json.JSONDecodeError:
            continue
        for m in doc.get("matches", []):
            v = m.get("vulnerability") or {}
            if v.get("id") and v.get("severity") in SEVS:
                rows.add((v["id"], v["severity"]))
    acc: dict = {}
    for key, rows in seen.items():
        row = _row(acc, key)
        for _cve, sev in rows:
            row[sev] += 1
    return acc


def counts_from_tsv(p: Path) -> dict:
    """Distinct (cve, severity) per repository. Raises OSError/UnicodeError if unreadable."""
    seen: dict = {}
    for line in Path(p).read_text().splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not is_ours(parts[0]):
            continue
        seen.setdefault(repo_key(parts[0]), set()).add((parts[1], parts[2]))
    acc: dict = {}
    for key, rows in seen.items():
        row = _row(acc, key)
        for _cve, sev in rows:
            if sev in SEVS:
                row[sev] += 1
    return acc


def repos_from_manifest(p: Path) -> set:
    """Repositories of our images listed in a release manifest.json. Raises on an unreadable file."""
    doc = json.loads(Path(p).read_text())
    images = doc["images"]
    if not isinstance(images, list):
        raise ValueError("images is not a list")
    out = set()
    for ref in images:
        safe = str(ref).replace("/", "__").replace(":", "__").replace(" ", "")
        if is_ours(safe):
            out.add(repo_key(safe))
    return out


def with_zero_finding_images(prev: dict, listed: set) -> dict:
    """An image the previous release listed but the baseline has no rows for had 0 findings."""
    merged = {k: dict(v) for k, v in prev.items()}
    for k in listed:
        merged.setdefault(k, {s: 0 for s in SEVS})
    return merged


def regressions(cur: dict, prev: dict) -> list:
    return [(k, prev[k]["Critical"], cur[k]["Critical"]) for k in sorted(cur)
            if k in prev and cur[k]["Critical"] > prev[k]["Critical"]]


def render(cur: dict, prev: dict | None, limited: bool = False) -> str:
    lines = ["## Images we build: absolute CVE counts", "",
             "_Not diffable by CVE (the previous tag's local build is gone); reported absolutely per cut._", "",
             "| Image | Critical | High | Critical at previous release |", "|---|---:|---:|---:|"]
    for k in sorted(cur):
        if prev is None:
            was = "n/a"
        elif k in prev:
            was = str(prev[k]["Critical"])
        else:
            was = "new"
        lines.append(f"| {k} | {cur[k]['Critical']} | {cur[k]['High']} | {was} |")
    if prev is None:
        lines += ["", "no baseline: gate not evaluated"]
        return "\n".join(lines) + "\n"
    if limited:
        lines += ["", "previous manifest not readable: gate limited to images with prior findings"]
    gone = sorted(k for k in prev if k not in cur)
    if gone:
        lines += ["", "### No longer built or scanned", ""]
        lines += [f"- {k} (Critical {prev[k]['Critical']}, High {prev[k]['High']} at previous release)" for k in gone]
    regs = regressions(cur, prev)
    if regs:
        lines += ["", "### Critical rose: release gate", ""]
        lines += [f"- {k}: Critical {a} → {b}" for k, a, b in regs]
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sbom_custom_images")
    ap.add_argument("--images-dir", required=True)
    ap.add_argument("--prev-baseline")
    ap.add_argument("--prev-manifest")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    cur = counts_from_grype_dir(Path(a.images_dir))
    prev = None
    if a.prev_baseline:
        try:
            prev = counts_from_tsv(Path(a.prev_baseline))
        except (OSError, UnicodeError) as exc:
            print(f"no baseline: gate not evaluated ({a.prev_baseline}: {exc})", file=sys.stderr)
    limited = False
    if prev is not None:
        try:
            if not a.prev_manifest:
                raise ValueError("no previous manifest given")
            prev = with_zero_finding_images(prev, repos_from_manifest(Path(a.prev_manifest)))
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            limited = True
            print(f"previous manifest not readable: gate limited to images with prior findings ({exc})",
                  file=sys.stderr)
    _write_atomic(Path(a.out), render(cur, prev, limited))
    return 2 if prev is not None and regressions(cur, prev) else 0


if __name__ == "__main__":
    sys.exit(main())
