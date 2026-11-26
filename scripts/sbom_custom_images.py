#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Absolute CVE counts for the images we build, and a Critical-must-not-rise gate.

Our images cannot be diffed by CVE: the previous tag's local build is gone, so the
rescan of the previous release reuses THIS run's scan and every finding reads
PERSISTENT. Report them absolutely per cut, keyed by repository (a tag change keeps
the row), and compare Critical counts against the previous release's committed
cve-baseline.tsv.

An image the previous manifest lists but could not scan (`failed_images`) or never
built on that host (`not_built_on_this_host`) has no rows in the baseline, exactly like
a clean scan. Unless the baseline does hold rows for its repository, it has NO baseline:
its row says so, it is named as "gate not evaluated", and it cannot trip the gate.

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


def _our_repos(refs) -> set:
    out = set()
    for ref in refs:
        safe = str(ref).replace("/", "__").replace(":", "__").replace(" ", "")
        if is_ours(safe):
            out.add(repo_key(safe))
    return out


def repos_from_manifest(p: Path) -> set:
    """Repositories of our images listed in a release manifest.json. Raises on an unreadable file."""
    doc = json.loads(Path(p).read_text())
    images = doc["images"]
    if not isinstance(images, list):
        raise ValueError("images is not a list")
    return _our_repos(images)


UNSCANNED = (("failed_images", "previous scan failed"),
             ("not_built_on_this_host", "not built at the previous release"))


def unscanned_from_manifest(p: Path) -> dict:
    """Repositories of our images the previous release could not scan, with the reason.
    Both fields are optional (older manifests lack them). Raises on an unreadable file."""
    doc = json.loads(Path(p).read_text())
    out: dict = {}
    for field, why in UNSCANNED:
        refs = doc.get(field) or []
        if not isinstance(refs, list):
            raise ValueError(f"{field} is not a list")
        for k in _our_repos(refs):
            out.setdefault(k, why)
    return out


def with_zero_finding_images(prev: dict, listed: set, unscanned: dict | None = None) -> dict:
    """An image the previous release listed AND scanned, but the baseline has no rows for,
    had 0 findings. One it could not scan has no baseline: it stays out."""
    merged = {k: dict(v) for k, v in prev.items()}
    for k in listed:
        if k in (unscanned or {}):
            continue
        merged.setdefault(k, {s: 0 for s in SEVS})
    return merged


def regressions(cur: dict, prev: dict) -> list:
    return [(k, prev[k]["Critical"], cur[k]["Critical"]) for k in sorted(cur)
            if k in prev and cur[k]["Critical"] > prev[k]["Critical"]]


def render(cur: dict, prev: dict | None, limited: bool = False,
           unmeasured: dict | None = None) -> str:
    unmeasured = unmeasured or {}
    lines = ["## Images we build: absolute CVE counts", "",
             "_Not diffable by CVE (the previous tag's local build is gone); reported absolutely per cut._", "",
             "| Image | Critical | High | Critical at previous release |", "|---|---:|---:|---:|"]
    for k in sorted(cur):
        if prev is None:
            was = "n/a"
        elif k in unmeasured:
            was = "no baseline"
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
    skipped = sorted(k for k in cur if k in unmeasured)
    if skipped:
        lines += ["", "### No baseline at the previous release", ""]
        lines += [f"- {k}: no baseline: gate not evaluated ({unmeasured[k]})" for k in skipped]
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
    unmeasured: dict = {}
    if prev is not None:
        try:
            if not a.prev_manifest:
                raise ValueError("no previous manifest given")
            listed = repos_from_manifest(Path(a.prev_manifest))
            # A repository the baseline holds rows for was measured; the rest of
            # what the previous release could not scan has no baseline at all.
            unmeasured = {k: why for k, why in unscanned_from_manifest(Path(a.prev_manifest)).items()
                          if k not in prev}
            prev = with_zero_finding_images(prev, listed, unmeasured)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            limited = True
            print(f"previous manifest not readable: gate limited to images with prior findings ({exc})",
                  file=sys.stderr)
    _write_atomic(Path(a.out), render(cur, prev, limited, unmeasured))
    return 2 if prev is not None and regressions(cur, prev) else 0


if __name__ == "__main__":
    sys.exit(main())
