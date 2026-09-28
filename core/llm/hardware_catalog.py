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
    cluster family when it set one, else — #2512 — on a `fleet` master the
    fleet's GPU family (fleet_family without a worker list: the master's own
    HARDWARE if it is a GPU class, else DEFAULT_FAMILY; never `cpu`), else the
    box's own HARDWARE."""
    import os
    forced = os.environ.get(CATALOG_HARDWARE_ENV)
    if forced:
        return forced
    hw = read_env_hardware(env_path)
    if master_role(env_path) == "fleet":
        return fleet_family(hw, [])
    return hw


def read_env_hardware(env_path) -> str:
    """HARDWARE= from a .env file, '' when absent — the box's own class."""
    return read_env_key(env_path, "HARDWARE")


def read_env_key(env_path, key) -> str:
    """KEY= from a .env file, '' when absent."""
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# ── #2512: the master's role ─────────────────────────────────────────────────
# Operator decision 2026-09-26. A CPU master has two jobs and the stack must
# tell them apart:
#   standalone — the master IS the LLM host (testvm-cpu; every GPU box). The
#                catalogue is evaluated for the master's own HARDWARE (#2158:
#                a CPU box gets qwen3-4b-instruct always-on).
#   fleet      — a master that serves GPU workers (master-cpu, prod 8.246). It
#                hosts NO models: its own worker registers as draining, the
#                catalogue is evaluated for the joined workers' family, and
#                the 4B is neither deployed nor offered.
# `cli/init.sh` writes it from the preset; the upgrade derives it once for an
# existing box (`derive_master_role`); an operator-set value is never touched.
MASTER_ROLE_KEY = "LLM_MASTER_ROLE"
MASTER_ROLES = ("standalone", "fleet")
#: What an absent/empty/unknown value means: the behaviour every box had
#: before the key existed.
DEFAULT_MASTER_ROLE = "standalone"
#: The families a fleet worker must belong to. `cpu` is not one — a CPU worker
#: is not what a fleet master hands its models to.
GPU_FAMILIES = ("amd", "nvidia", "apple")


def normalise_role(value) -> str:
    """'fleet' / 'standalone' for a recognised value (any case), else ''."""
    v = str(value or "").strip().lower()
    return v if v in MASTER_ROLES else ""


def master_role(env_path) -> str:
    """The box's role from .env; empty/unknown reads as standalone."""
    return normalise_role(read_env_key(env_path, MASTER_ROLE_KEY)) or DEFAULT_MASTER_ROLE


def fleet_gpu_workers(workers, master_name="", placeable_only=False) -> list:
    """The GPU workers JOINED to a fleet master.

    `workers` is the LLM Manager's GET /api/workers list (name, hardware,
    status, external). Counted: not an external endpoint backend (#307 — it
    runs no worker-agent, nothing is placed on it), a GPU family by the
    manager's own rule, and not the master's own worker — whatever its status
    (#2512 fix round 1): right after restart_stack every worker reads `stale`
    (no heartbeat for 90 s), a drained worker is still joined, and a
    derivation that counted only `ready` rows wrote `standalone` for good on
    exactly prod's shape. `placeable_only` narrows to `ready` for callers that
    pick a deploy target.

    EXCEPT `pending` (final wave): under manual approval a registration that
    was never approved stays `pending` — a restart keeps it there, and an
    approved worker never returns to it (workers.py register) — so it has
    never been joined. Counting it let one stray join request turn a CPU box
    into a fleet master that serves nothing, and the role is written once."""
    out = []
    for w in workers or []:
        if not isinstance(w, dict):
            continue
        status = str(w.get("status") or "").lower()
        if status == "pending":
            continue
        if placeable_only and status != "ready":
            continue
        if w.get("external"):
            continue
        if master_name and str(w.get("name") or "") == master_name:
            continue
        if hardware_family(w.get("hardware")) not in GPU_FAMILIES:
            continue
        out.append(w)
    return out


def fleet_family(env_hardware, workers, master_name="") -> str:
    """#2512: the family a `fleet` master's catalogue is evaluated for — the
    joined GPU workers' (the most common one, ties broken by name), NOT the
    master's HARDWARE (#2460/#2499: prod is HARDWARE=cpu with qwen3.6 on its
    AMD workers). No GPU worker known: the master's own HARDWARE when that is
    a GPU class, else DEFAULT_FAMILY. NEVER `cpu` — a fleet master's always-on
    set is the GPU set; the 4B (`hardware: [cpu]`) is not auto-started there
    even while its workers are still joining."""
    fams = [hardware_family(w.get("hardware")) for w in fleet_gpu_workers(workers, master_name)]
    if fams:
        return max(sorted(set(fams)), key=fams.count)
    if hardware_family(env_hardware) in GPU_FAMILIES:
        return hardware_family(env_hardware)
    return DEFAULT_FAMILY


def derive_master_role(env_hardware, workers, master_name="") -> str:
    """#2512 migration heuristic for a box that predates the key.

    `fleet` when the master itself is CPU-class AND at least one non-external
    GPU worker other than the master's own is joined (any status but a
    never-approved `pending` — see fleet_gpu_workers); else `standalone`. A GPU master is standalone by
    definition — it hosts its own models (the issue: "GPU presets ->
    standalone, the master has a GPU"); reading its own GPU worker as "a GPU
    worker is joined" would turn every GPU box into a master that serves
    nothing."""
    return derive_master_role_why(env_hardware, workers, master_name)[0]


def derive_master_role_why(env_hardware, workers, master_name=""):
    """(role, reason) — the reason is what `rzfz upgrade --check` prints."""
    if hardware_family(env_hardware) != "cpu":
        return "standalone", f"HARDWARE={env_hardware or '?'} is not CPU-class — this master hosts its own models"
    joined = fleet_gpu_workers(workers, master_name)
    if joined:
        names = ", ".join(sorted(str(w.get("name") or "?") for w in joined))
        return "fleet", f"a CPU master with GPU worker(s) joined: {names}"
    return "standalone", "a CPU master with no GPU worker joined"