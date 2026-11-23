#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""rzfz model curation (#2392): pin a catalogue entry to the Hugging Face commit
it was approved at and the sha256 of every file the fleet may load.

    python3 scripts/curate-model.py qwen3-reranker        # one alias
    python3 scripts/curate-model.py --all                  # every entry with a source

The digests come from ONE tree listing at the RESOLVED commit, never `main`, so
a later re-publish of the repository cannot change what the fleet accepts.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
import urllib.request
from pathlib import Path

HF = "https://huggingface.co"
MANIFEST = Path(__file__).resolve().parents[1] / "core/llm/standard-models.yaml"


PAGE = 1000  # the tree endpoint's page size; a full page without a next link is unprovable
_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
_SHARD = re.compile(r"^(.*)-(\d{5})-of-(\d{5})(\.[^/]+)$")


def _get(url):
    """Return (json body, next-page url or None) following the Link header."""
    req = urllib.request.Request(url, headers={"User-Agent": "rzfz-curate-model"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        m = _NEXT.search(resp.headers.get("Link") or "")
        return json.load(resp), (m.group(1) if m else None)


def _page(fetch, url):
    r = fetch(url)
    return r if isinstance(r, tuple) else (r, None)


def _listing(fetch, url, repo):
    entries, seen = [], set()
    while url:
        if url in seen:
            raise SystemExit(f"{repo}: pagination loops at {url}")
        seen.add(url)
        page, url = _page(fetch, url)
        entries += page
        if url is None and len(page) >= PAGE:
            raise SystemExit(f"{repo}: full page of {len(page)} and no next link — "
                             "cannot prove the listing is complete")
    return entries


def check_shards(repo, pattern, paths):
    """Every -NNNNN-of-MMMMM group among `paths` must hold shards 1..MMMMM."""
    groups = {}
    for p in paths:
        m = _SHARD.match(p)
        if m:
            groups.setdefault((m[1], m[3], m[4]), set()).add(int(m[2]))
    for (prefix, total, _ext), have in groups.items():
        if have != set(range(1, int(total) + 1)):
            raise SystemExit(f"{repo}: {pattern!r} matched {len(have)} of {int(total)} "
                             f"shards of {prefix} — partial shard set refused")


def resolve(repo, patterns, *, fetch=_get):
    sha = _page(fetch, f"{HF}/api/models/{repo}/revision/main")[0]["sha"]
    tree = _listing(fetch, f"{HF}/api/models/{repo}/tree/{sha}?recursive=true", repo)
    by_path = {e["path"]: e for e in tree if e.get("type") == "file"}
    digests = {}
    for pat in patterns:
        hits = (sorted(p for p in by_path if fnmatch.fnmatchcase(p, pat))
                if any(c in pat for c in "*?[") else [pat])
        if not hits:
            raise SystemExit(f"{repo}: nothing matches {pat!r} at {sha}")
        check_shards(repo, pat, hits)
        for p in hits:
            oid = ((by_path.get(p) or {}).get("lfs") or {}).get("oid") or ""
            if not re.fullmatch(r"[0-9a-f]{64}", oid):
                raise SystemExit(f"{repo}/{p}: no LFS sha256 at {sha} — cannot pin it")
            digests[p] = oid
    return sha, digests


def rewrite_entry(text, alias, sha, digests):
    lines = text.splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.rstrip("\n") == f"  {alias}:")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^(  )?\S", lines[i])), len(lines))
    kept, skipping = [], False
    for l in lines[start:end]:
        if l.startswith("    huggingface_revision:"):
            continue
        if l.startswith("    sha256:"):
            skipping = True
            continue
        if skipping and l.startswith("      "):
            continue
        skipping = False
        kept.append(l)
    at = next(i for i, l in enumerate(kept) if l.startswith("    huggingface_filename:")) + 1
    kept[at:at] = ([f"    huggingface_revision: {sha}\n", "    sha256:\n"]
                   + [f"      {p}: {d}\n" for p, d in sorted(digests.items())])
    return "".join(lines[:start] + kept + lines[end:])


def main(argv=None) -> int:
    import yaml
    ap = argparse.ArgumentParser(prog="curate-model")
    ap.add_argument("aliases", nargs="*")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    text = MANIFEST.read_text(encoding="utf-8")
    models = yaml.safe_load(text)["models"]
    aliases = [a for a, m in models.items() if m.get("huggingface_repo_id")] if args.all else args.aliases
    if not aliases:
        ap.error("name an alias or pass --all")
    for alias in aliases:
        m = models[alias]
        pats = [m["huggingface_filename"]] + ([m["huggingface_mmproj_filename"]]
                                              if m.get("huggingface_mmproj_filename") else [])
        sha, digests = resolve(m["huggingface_repo_id"], pats)
        text = rewrite_entry(text, alias, sha, digests)
        print(f"{alias}: {m['huggingface_repo_id']}@{sha} — {len(digests)} file(s) pinned")
    MANIFEST.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
