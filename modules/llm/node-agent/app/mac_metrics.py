# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2559 — memory and Metal metrics on macOS, read without root.

Sources: `sysctl hw.memsize` / `iogpu.wired_limit_mb`, `vm_stat`, and
`ioreg -r -d 1 -c IOAccelerator` (its "PerformanceStatistics" dictionary).
Everything here is best-effort: a command that fails, or a key a macOS
release does not print, yields ``None`` for the fields that depend on it and
never an exception — the agent's report cycle must survive a missing metric.

Units follow the Linux agent (`runtime._live_metrics`): ``gpu_util`` is a
percentage 0-100, memory figures are GiB.

The command output formats were written from general platform knowledge;
D33 compares them with the real worker Mac.
"""
from __future__ import annotations

import re
import shutil
import subprocess

GIB = 2 ** 30


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=10,
                          env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}).stdout


def parse_vm_stat(text: str) -> dict:
    """Page counts from `vm_stat`. Missing page size -> ``page_size`` None;
    a missing counter -> 0."""
    m = re.search(r"page size of (\d+) bytes", text or "")

    def n(label):
        c = re.search(rf"^{re.escape(label)}:\s+(\d+)\.", text or "", re.M)
        return int(c.group(1)) if c else 0
    return {"page_size": int(m.group(1)) if m else None, "free": n("Pages free"),
            "active": n("Pages active"), "inactive": n("Pages inactive"),
            "wired": n("Pages wired down"), "compressed": n("Pages occupied by compressor")}


def parse_ioreg_accel(text: str) -> dict:
    """GPU load (percent) and GPU-held system memory (GiB) from the
    IOAccelerator "PerformanceStatistics" dictionary; None when a key is
    absent (the key names differ between macOS releases and GPU generations)."""
    utils = [int(x) for x in re.findall(r'"Device Utilization %"\s*=\s*(\d+)', text or "")]
    used = [int(x) for x in re.findall(r'"In use system memory"\s*=\s*(\d+)', text or "")]
    return {"gpu_util": float(max(utils)) if utils else None,
            "gpu_mem_used_gb": (max(used) / GIB) if used else None}


def gpu_budget_gb(memsize_bytes: int, wired_limit_mb: int) -> float:
    """The GPU working-set limit: `iogpu.wired_limit_mb` when set (> 0), else
    the macOS default share of unified memory — 2/3 up to 36 GiB, 3/4 above
    (the task brief's rule, general platform knowledge; `wired_limit_mb == 0`
    means "macOS default"). D33 measures it on the worker Mac."""
    if wired_limit_mb and wired_limit_mb > 0:
        return wired_limit_mb / 1024
    total = memsize_bytes / GIB
    return total * (2 / 3 if total <= 36 else 3 / 4)


def _try(run, argv):
    try:
        out = run(argv)
    except Exception:
        return None
    return out if isinstance(out, str) else None


def _int(text):
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def sample(run=None, disk_path="/opt/rzfz-worker") -> dict:
    run = run or _run
    memsize = _int(_try(run, ["sysctl", "-n", "hw.memsize"]))
    wired = _int(_try(run, ["sysctl", "-n", "iogpu.wired_limit_mb"])) or 0
    vm = parse_vm_stat(_try(run, ["vm_stat"]) or "")
    gpu = parse_ioreg_accel(_try(run, ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"]) or "")
    used = None
    # UNMEASURED until D33: mem_used = active + wired + compressor pages is
    # not yet compared with Activity Monitor's "Memory Used" on the worker Mac
    # ("active" includes some file-backed pages, so it may over-count).
    if vm["page_size"]:
        used = (vm["active"] + vm["wired"] + vm["compressed"]) * vm["page_size"] / GIB
    try:
        disk = shutil.disk_usage(disk_path).free / GIB
    except OSError:
        disk = None
    return {"mem_total_gb": memsize / GIB if memsize else None, "mem_used_gb": used,
            "vram_total_gb": gpu_budget_gb(memsize, wired) if memsize else None,
            "vram_used_gb": gpu["gpu_mem_used_gb"], "gpu_util": gpu["gpu_util"],
            "disk_free_gb": disk}
