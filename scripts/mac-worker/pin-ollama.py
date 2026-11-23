#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2559 — pin the Mac worker's Ollama (and the uv that builds its Python):
download each official release asset once, compute its sha256 from the bytes,
and record version, URL and sha256 in modules/llm/mac-agent/pins.json.

The digest is ALWAYS computed here from the downloaded asset. When the release
also publishes a checksum (Ollama: `sha256sum.txt`; uv: `<asset>.sha256`), the
computed digest must match it, or nothing is written.

    scripts/mac-worker/pin-ollama.py <ollama-version> [--uv <uv-version>]

Without `--uv` the uv pin already in pins.json is kept. pins.json is written
only by this script (never by hand). The Mac shell side reads it with the
line-oriented reader in modules/llm/mac-agent/lib/pins.sh, so the file layout
(`json.dumps(indent=2)`) is part of the contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PINS = Path(__file__).resolve().parents[2] / "modules/llm/mac-agent/pins.json"

OLLAMA_URL = "https://github.com/ollama/ollama/releases/download/v{v}/ollama-darwin.tgz"
OLLAMA_SUMS = "https://github.com/ollama/ollama/releases/download/v{v}/sha256sum.txt"
UV_URL = "https://github.com/astral-sh/uv/releases/download/{v}/uv-aarch64-apple-darwin.tar.gz"


class ChecksumMismatch(SystemExit):
    pass


def _fetch(url):
    with urllib.request.urlopen(url, timeout=600) as resp:  # noqa: S310 - fixed https URLs
        return resp.read()


def _published(fetch, sums_url, asset):
    """The digest the release itself publishes for `asset`, or None when the
    release publishes no checksum file (or it does not list the asset)."""
    try:
        text = fetch(sums_url)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*").removeprefix("./") == asset:
            digest = parts[0].lower()
            if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                return digest
    return None


def _entry(fetch, version, url, sums_url, asset, sources, key):
    sha = hashlib.sha256(fetch(url)).hexdigest()
    published = _published(fetch, sums_url, asset)
    if published is not None and published != sha:
        raise ChecksumMismatch(f"{asset} {version}: downloaded sha256 {sha} does not match the "
                               f"published {published} ({sums_url}) — nothing written")
    sources[key] = (f"computed from the downloaded asset; matches {sums_url}" if published
                    else "computed from the downloaded asset; the release publishes no checksum for it")
    return {"version": version, "url": url, "sha256": sha}


def pin(version: str, *, uv: str | None = None, fetch=_fetch, pins_path=PINS, sources=None) -> dict:
    sources = {} if sources is None else sources
    data = {"ollama": _entry(fetch, version, OLLAMA_URL.format(v=version), OLLAMA_SUMS.format(v=version),
                             "ollama-darwin.tgz", sources, "ollama")}
    if uv:
        url = UV_URL.format(v=uv)
        data["uv"] = _entry(fetch, uv, url, url + ".sha256", "uv-aarch64-apple-darwin.tar.gz", sources, "uv")
    else:
        try:
            old = json.loads(Path(pins_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        if isinstance(old, dict) and isinstance(old.get("uv"), dict):
            data["uv"] = old["uv"]
    Path(pins_path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("version", help="the Ollama release, X.Y.Z (no leading v)")
    ap.add_argument("--uv", help="the uv release to pin, X.Y.Z")
    args = ap.parse_args(argv)
    sources: dict = {}
    data = pin(args.version.lstrip("v"), uv=args.uv, sources=sources)
    print(json.dumps(data, indent=2))
    for key, how in sources.items():
        print(f"{key}: {how}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
