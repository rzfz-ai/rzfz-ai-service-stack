# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The hardware lines (amd, cpu, nvidia) and which of their images a box skips.

#3201/#3215: ONE rule for what a box of one hardware line does not use, shared
by the stick loader (scripts/lib.sh appliance_hardware_filter, fed by the
package's ``hardware_images``) and by ``rzfz verify-images`` (fed by one
LLM-runtime render per device overlay). Before #3215 the loader skipped
razzfazz-gpustack:vulkan on a CPU box and verify-images still expected it.

No imports beyond the standard library: the loader runs this on a fresh box.
"""
from __future__ import annotations

LINES = ("amd", "cpu", "nvidia")

#: The device overlay that selects a line (an entry in COMPOSE_FILE).
OVERLAY = "modules/llm/compose.devices.{}.yml"


def skipped(lines: dict, hardware: str) -> set:
    """The images of the OTHER lines that ``hardware``'s line does not use.

    ``lines`` maps a line to its images. An image every line uses (the common
    services) is never skipped. A hardware the lines do not name, or no lines,
    skips nothing: never guess away an image."""
    if not lines or hardware not in lines:
        return set()
    mine = set(lines[hardware] or ())
    return {ref for line, refs in lines.items() if line != hardware for ref in (refs or ())} - mine


def without_overlay(compose_file: str) -> list:
    """COMPOSE_FILE's entries without any device overlay (spelled with or
    without the ``modules/`` prefix, as scripts/lib.sh accepts both)."""
    overlays = {f"compose.devices.{hw}.yml" for hw in LINES}
    return [e for e in (compose_file or "").split(":")
            if e and not (e.rsplit("/", 1)[-1] in overlays and "llm/" in e)]
