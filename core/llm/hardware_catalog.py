# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2158 — the catalogue's hardware conditionality, in ONE place.

Journey A on 0.91 (2026-09-15, a fresh CPU box, 30 GB): `standard-models.yaml`
carried no hardware conditionality at all, its sole always-on chat entry was
pinned at ctx=1M par=4 (126.9 GB by the manager's own estimate), and a fresh
CPU tester VM installed clean with no chat model it could place. The upgraded
ga.15 CPU box next door chats fine on a 4B model at ~38 tok/s — the capability
was proven on the box class and unreachable from a clean install.

Two fields, both optional, both evaluated HERE by every consumer:

    models.<alias>.hardware: [cpu]        # families on which auto_start applies;
                                          # elsewhere the entry is an on-demand spare
    defaults_by_hardware:                 # role -> alias overrides per family
      cpu: {chat: qwen3-4b-instruct, …}

The family rule is the manager's own (`_hardware_family` in
modules/llm/manager/app/api/inventory.py); a guard runs both over the same
strings so they cannot drift. The deploy path passes the TARGET WORKER's
hardware as the manager reports it; the other consumers pass the box's
HARDWARE from .env.
"""
from __future__ import annotations

KNOWN_FAMILIES = ("amd", "nvidia", "apple", "cpu")
#: The class the catalogue was written for (unified-memory GPU boxes). An
#: UNKNOWN hardware — no HARDWARE in .env yet, no worker known, an exotic
#: string — resolves here, so the fleet default set is what such a box gets;
#: nothing hardware-specific (a cpu-only entry) turns on by accident, and the
#: fleet default never disappears because a value was missing.
DEFAULT_FAMILY = "amd"


def effective_family(hardware) -> str:
    fam = hardware_family(hardware)
    return fam if fam in KNOWN_FAMILIES else DEFAULT_FAMILY


def hardware_family(value) -> str:
    """Canonical family for a hardware string — byte-for-byte the manager's rule."""
    h = (value or "").lower()
    if ("amd" in h) or ("gfx" in h) or ("rocm" in h) or ("vulkan" in h):
        return "amd"
    if ("nvidia" in h) or ("cuda" in h):
        return "nvidia"
    if ("apple" in h) or ("metal" in h) or ("mlx" in h):
        return "apple"
    if h == "cpu" or h.endswith("-cpu") or "cpu" in h.split("-"):
        return "cpu"
    return h


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "no", "0", "")


def entry_families(entry) -> list | None:
    """The families an entry's auto_start applies to, normalised; None = all."""
    hw = (entry or {}).get("hardware")
    if hw is None:
        return None
    if isinstance(hw, str):
        hw = [hw]
    return [hardware_family(h) for h in hw]


def entry_auto_start(entry, hardware) -> bool:
    """Is this entry always-on for a worker/box of `hardware`?

    `auto_start` as declared, restricted to `hardware:` when the entry names
    one. An entry outside its families is an on-demand spare there (the same
    thing `auto_start: false` means): present in the catalogue, deployable
    from the console, never part of the unattended standard set."""
    base = _truthy((entry or {}).get("auto_start", True))
    fams = entry_families(entry)
    if fams is None:
        return base
    return base and effective_family(hardware) in fams


def defaults_for(spec, hardware) -> dict:
    """`defaults` with the family's `defaults_by_hardware` overrides applied."""
    base = dict((spec or {}).get("defaults") or {})
    by_hw = (spec or {}).get("defaults_by_hardware") or {}
    fam = effective_family(hardware)
    for key, overrides in by_hw.items():
        if hardware_family(key) == fam and isinstance(overrides, dict):
            base.update(overrides)
    return base


def cluster_family(env_hardware, workers) -> str:
    """#2460: the family the catalogue is evaluated for.

    A box's .env HARDWARE describes the box. On a GPUStack MASTER whose models
    are served by attached workers, that is the wrong input: prod (8.246) is
    HARDWARE=cpu with qwen3.6 running on a READY AMD worker, and the #2158 CPU
    catalogue would have scaled that model to 0 and swapped every consumer to
    the 4B. So: a GPU-class HARDWARE is returned as is; a CPU box with at least
    one READY worker carrying GPU devices is evaluated as the family of those
    devices (the most common one); everything else stays as declared.
    `workers` is GPUStack's /workers `items` (state + status.gpu_devices[].vendor)."""
    if hardware_family(env_hardware) != "cpu":
        return env_hardware
    fams = []
    for w in workers or []:
        if str((w or {}).get("state") or "").lower() != "ready":
            continue
        for dev in ((w.get("status") or {}).get("gpu_devices") or []):
            fam = effective_family((dev or {}).get("vendor") or (dev or {}).get("type") or "")
            if fam != "cpu":
                fams.append(fam)
    if not fams:
        return env_hardware
    return max(sorted(set(fams)), key=fams.count)


#: #2460: post-install resolves the cluster family once and hands it to the
#: Python consumers (core/llm/sync.py) through this variable.
CATALOG_HARDWARE_ENV = "RZFZ_CATALOG_HARDWARE"


def catalog_hardware(env_path) -> str:
    """The hardware the catalogue is evaluated for: post-install's resolved
    cluster family when it set one, else the box's own HARDWARE."""
    import os
    return os.environ.get(CATALOG_HARDWARE_ENV) or read_env_hardware(env_path)


def read_env_hardware(env_path) -> str:
    """HARDWARE= from a .env file, '' when absent — the box's own class."""
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("HARDWARE="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""
