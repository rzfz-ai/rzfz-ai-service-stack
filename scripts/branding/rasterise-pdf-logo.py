#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2546/#2480: rasterise the single-source rzfz.ai logo SVG for the Day-1 PDFs.

Run in the development sandbox (cairosvg) whenever core/Authentik/media/rzfz-ai-logo.svg
changes; commit the PNG and its sidecar. Boxes never run this (no rasteriser there).
--check exits 1 when the sidecar does not match the SVG (needs no cairosvg)."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "core" / "Authentik" / "media"
SVG, PNG = MEDIA / "rzfz-ai-logo.svg", MEDIA / "rzfz-ai-logo-pdf.png"
SIDECAR = MEDIA / "rzfz-ai-logo-pdf.png.source-sha256"
#: 4x the SVG's 132x32 viewBox: sharp at the PDFs' 36 px header height in print.
WIDTH, HEIGHT = 528, 128


def _fc_match(family: str) -> str:
    out = subprocess.run(["fc-match", "-f", "%{family[0]}", family], capture_output=True,
                         text=True, timeout=10, check=False)
    return out.stdout.strip()


def resolve_font_family(svg: str) -> tuple[str, str]:
    """Pick the font a browser would: the first family in each `font-family`
    list that fontconfig really has (by name or by its metric alias, e.g.
    Helvetica/Arial -> Liberation Sans). cairosvg itself hands the whole list
    to fontconfig, which falls back to the system default, a monospace font in
    the sandbox. The SOURCE SVG is never changed; only the rasterised copy."""
    fallback = _fc_match("rzfz-no-such-font-family")
    chosen = ""

    def pick(m: re.Match) -> str:
        nonlocal chosen
        for fam in (f.strip().strip("'\"") for f in m.group(2).split(",")):
            got = _fc_match(fam) if fam else ""
            if got and got != fallback:
                chosen = chosen or got
                return f'{m.group(1)}"{got}"'
        return m.group(0)

    return re.sub(r'(font-family=)"([^"]*)"', pick, svg), chosen


def main(argv: list[str]) -> int:
    digest = hashlib.sha256(SVG.read_bytes()).hexdigest()
    if "--check" in argv:
        ok = (PNG.is_file() and SIDECAR.is_file()
              and (SIDECAR.read_text(encoding="utf-8").split() or [""])[0] == digest)
        print("OK" if ok else "STALE: re-run scripts/branding/rasterise-pdf-logo.py")
        return 0 if ok else 1
    import cairosvg  # noqa: PLC0415 — sandbox-only dependency
    svg, font = resolve_font_family(SVG.read_text(encoding="utf-8"))
    if not font:
        print("no installed font matches the wordmark's font-family list; install a sans font "
              "(e.g. Liberation Sans) first", file=sys.stderr)
        return 1
    cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=str(PNG),
                     output_width=WIDTH, output_height=HEIGHT)
    SIDECAR.write_text(f"{digest}  rzfz-ai-logo.svg\nfont: {font}\n", encoding="utf-8")
    print(f"wrote {PNG.name} ({WIDTH}x{HEIGHT}) from {SVG.name} {digest[:12]}, wordmark in {font}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
