#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2393 — patch cadence for the pinned llama.cpp runner builds.

The runner is the process that parses untrusted GGUF files, so its build is a
security pin like any image digest. This compares every build named in
``modules/llm/runners/runners.yaml`` (``engine_build`` and each target's tag,
rollback targets included — they are published images too) with upstream.

Exit 0: every pinned build is the latest.
Exit 1: behind upstream, no advisory applies (a note).
Exit 2: a pinned build is older than a published security advisory's patched
        build — a mandatory bump (release-cycle Phase 2 stops on it).
Exit 3: upstream could not be asked — NOT a pass; fix the network and re-run.

``--json`` prints ``{"rc", "pinned", "latest", "advisories", "unmapped", "lines"}``.
``GITHUB_TOKEN``, when set, is sent as a bearer token (the anonymous API allows
60 requests an hour).
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
API = "https://api.github.com/repos/ggml-org/llama.cpp"


def _get(url):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "rzfz-llama-pins"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _build(s) -> int | None:
    m = re.search(r"\bb(\d{4,6})\b", str(s or ""))
    return int(m.group(1)) if m else None


def pinned_builds(runners_yaml) -> set[int]:
    spec = yaml.safe_load(Path(runners_yaml).read_text(encoding="utf-8"))
    out = {_build(spec.get("engine_build"))}
    for t in spec.get("targets") or []:
        out.add(_build(t.get("tag")))
    return {b for b in out if b}


def exempt_builds(runners_yaml) -> set[int]:
    """Builds a third-party image carries that cannot be bumped from here.

    A target opts in with BOTH ``measured_build`` (what ``llama-server
    --version`` reports inside the image) and ``advisories_fixed_in_commit``
    (the upstream commit that image contains, checked by hand against the
    advisories). Such a build is still reported as behind, but is not a
    mandatory bump. Nothing else is exempt."""
    spec = yaml.safe_load(Path(runners_yaml).read_text(encoding="utf-8"))
    return {int(t["measured_build"]) for t in spec.get("targets") or []
            if t.get("measured_build") and t.get("advisories_fixed_in_commit")}


def latest_build(fetch=_get) -> int | None:
    """The newest bNNNN build upstream published.

    Not ``/releases/latest``: measured 2026-09-30, upstream marks every bNNNN
    build a pre-release and ``latest`` is a semver release (``v0.5.0``) that
    names no build at all. The newest bNNNN tag among the recent releases is
    what the runners pin against."""
    builds = [_build(r.get("tag_name")) for r in (fetch(f"{API}/releases?per_page=100") or [])]
    builds = [b for b in builds if b]
    return max(builds) if builds else None


def _bounds(v) -> tuple[int, int | None]:
    """The affected builds of one vulnerability as ``[lower, upper)``.

    From ``vulnerable_version_range``: ``>= bX`` / ``> bX`` raise the lower
    bound, ``<= bX`` / ``< bX`` set the upper one, a bare ``bX`` is that build
    alone. ``patched_versions`` only narrows the upper bound: measured,
    GHSA-96jg-mvhq-q7q7 says ``< b7437`` and patched ``b7824``, so b7500 is
    outside the range and not affected. ``upper`` is None when neither field
    names a build (e.g. the fix is a commit)."""
    lower, upper = 0, None
    rng = str(v.get("vulnerable_version_range") or "")
    for op, n in re.findall(r"(>=|>|<=|<)?\s*\bb(\d{4,6})\b", rng):
        n = int(n)
        if op == ">=":
            lower = max(lower, n)
        elif op == ">":
            lower = max(lower, n + 1)
        elif op in ("<=", "<"):
            u = n + 1 if op == "<=" else n
            upper = u if upper is None else min(upper, u)
        else:  # a bare build: exactly that one
            lower = max(lower, n)
            upper = n + 1 if upper is None else min(upper, n + 1)
    patched = _build(v.get("patched_versions"))
    if patched:
        upper = patched if upper is None else min(upper, patched)
    return lower, upper


def _fixed_in(v) -> int | None:
    """The build that fixes it: ``patched_versions`` when it names one,
    otherwise the first build past the affected range (``<= b7991`` → 7992) —
    an advisory with no patch yet still marks its range as affected."""
    return _build(v.get("patched_versions")) or _bounds(v)[1]


def advisories(fetch=_get, unmapped=None) -> list[dict]:
    """Advisories whose affected range is expressed in builds. One that names
    only a commit (``26a48ad``) cannot be ordered against a build; its id goes
    to ``unmapped`` for a manual look."""
    out = []
    for a in fetch(f"{API}/security-advisories?per_page=100") or []:
        for v in a.get("vulnerabilities") or []:
            lower, upper = _bounds(v)
            if upper is None:
                if unmapped is not None:
                    unmapped.append(a.get("ghsa_id"))
                continue
            out.append({"ghsa_id": a.get("ghsa_id"), "patched_build": _fixed_in(v),
                        "lower": lower, "upper": upper, "summary": a.get("summary", "")})
    return out


def verdict(pinned, latest, advs, exempt=frozenset()):
    """A pin is MANDATORY when it lies in an advisory's affected range
    ``[lower, upper)`` (without bounds: below ``patched_build``)."""
    lines, rc = [], 0
    for b in sorted(pinned):
        for a in advs:
            lo, hi = a.get("lower") or 0, a.get("upper") or a["patched_build"]
            if lo <= b < hi and b not in exempt:
                lines.append(f"MANDATORY: b{b} is affected by {a['ghsa_id']} (fixed in b{a['patched_build']}) {a.get('summary','')}")
                rc = 2
        if latest and b < latest:
            lines.append(f"behind: b{b} (latest b{latest})")
            rc = max(rc, 1)
    return rc, lines


def main(argv=None, fetch=_get) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    pinned = pinned_builds(REPO / "modules/llm/runners/runners.yaml")
    try:
        latest = latest_build(fetch)
        if latest is None:
            raise ValueError("no upstream release names a bNNNN build")
        unmapped: list[str] = []
        advs = advisories(fetch, unmapped)
    except Exception as exc:  # network, HTTP, JSON: unknown is never "current"
        msg = f"could not check upstream ({type(exc).__name__}: {exc}) — this is not a pass"
        if as_json:
            print(json.dumps({"rc": 3, "pinned": sorted(pinned), "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 3
    rc, lines = verdict(pinned, latest, advs, exempt_builds(REPO / "modules/llm/runners/runners.yaml"))
    if as_json:
        print(json.dumps({"rc": rc, "pinned": sorted(pinned), "latest": latest,
                          "advisories": advs, "unmapped": unmapped, "lines": lines}))
    else:
        print("\n".join(lines) or f"current: {sorted(pinned)}")
        if unmapped:
            print(f"note: {len(unmapped)} advisory(ies) name a commit, not a build — "
                  f"check by hand: {', '.join(unmapped)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
