# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""BSB-16 — Post-toggle Tier-D acceptance probe runner.

Wired into ``apply_manager._execute_toggle`` so that when a profile is
ENABLED via the Configuration Portal, the relevant Tier-D acceptance
probes (one per container that has a ``tests/acceptance/<name>/``
subdir) are kicked off automatically. The result is reported back into
the apply-action's SSE line stream so the UI can render a green/red
toast and the audit log records the probe outcome alongside the
toggle.

DISABLE toggles do NOT trigger probes — there's nothing to verify.

Why this lives in a separate module
-----------------------------------
- Keeps ``apply_manager`` narrow (it's already 600+ lines).
- Lets the probe runner be unit-tested independently from the live
  threading + docker-compose machinery in ``_execute_toggle``.
- The "how does the container reach the host's test-runner?" question
  (see decisions log BSB-16-DEC-01) is contained here so any future
  swap (host-side queue, dedicated probe sidecar) only touches this
  one file.

Container-name → acceptance-dir mapping
---------------------------------------
``tests/acceptance/`` uses snake_case directory names. Profile
container names use kebab-case (``dify-api``, ``dify-web``). We
canonicalise via ``str.replace('-', '_')`` and check directory
existence; misses are silently ignored (not every container has a
probe yet — that's a known gap, not a hard error).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional


# Cap probe runtime so a hung container can't pin the apply-action
# forever. 5 minutes mirrors the M032-S02 PROFILE_UP_TIMEOUT_SECONDS
# upper bound — if the module's containers haven't passed their
# acceptance probe within 5 minutes of becoming Up, something is wrong.
PROBE_TIMEOUT_SECONDS = 5 * 60


def _container_to_acceptance_dirname(container_name: str) -> str:
    """``dify-api`` → ``dify_api``; ``openwebui`` → ``openwebui``."""
    return container_name.replace("-", "_")


def resolve_probe_targets(
    profile: dict,
    *,
    stack_root: Path,
) -> List[str]:
    """Map a profile dict's container list to acceptance probe targets.

    Returns a sorted list of acceptance-dir names (relative to
    ``tests/acceptance/``). Containers without a corresponding directory
    are silently skipped (they have no probe yet — that's not an error).
    """
    accept_root = Path(stack_root) / "tests" / "acceptance"
    if not accept_root.is_dir():
        return []
    targets: List[str] = []
    for container in profile.get("containers") or []:
        name = container.get("name") if isinstance(container, dict) else None
        if not name:
            continue
        dirname = _container_to_acceptance_dirname(name)
        if (accept_root / dirname).is_dir():
            targets.append(dirname)
    return sorted(set(targets))


def run_probes(
    targets: Iterable[str],
    *,
    stack_root: Path,
    action=None,
) -> dict:
    """Run ``razzfazz-test.sh <target> --acceptance --no-coverage`` for each
    target in sequence, capture the result, and return a structured summary.

    Returns
    -------
    dict with keys::

        {
            'overall_pass': True | False | None,   # None == skipped
            'targets': [
                {'target': str, 'pass': bool, 'duration_ms': int,
                 'stdout_tail': str},
                ...
            ],
            'skipped_reason': Optional[str],
        }

    The ``action`` argument, if given, is the ``ApplyAction`` whose
    ``add_line`` we stream progress into. The contract: the runner must
    NEVER raise — every error path returns a structured summary so the
    caller can log it deterministically.
    """
    targets = list(targets)
    runner = Path(stack_root) / "razzfazz-test.sh"

    if not targets:
        msg = "no acceptance probes found for this profile's containers"
        if action is not None:
            action.add_line(f"  [post-toggle probe] skipped: {msg}")
        return {
            "overall_pass": None,
            "targets": [],
            "skipped_reason": msg,
        }
    if not runner.is_file():
        # The config-portal container in production may not have the test
        # infra mounted in (razzfazz-test.sh + tests/.venv aren't part of
        # the runtime image). Skip cleanly with a clear reason rather than
        # crashing the toggle-action thread.
        msg = (
            f"razzfazz-test.sh not found at {runner} — "
            f"the post-toggle probe needs the test infra mounted into the "
            f"config-portal container (see BSB-16-DEC-01)"
        )
        if action is not None:
            action.add_line(f"  [post-toggle probe] skipped: {msg}")
        return {
            "overall_pass": None,
            "targets": [],
            "skipped_reason": msg,
        }

    if action is not None:
        action.add_line(
            f"  [post-toggle probe] running acceptance probes for: "
            f"{', '.join(targets)}"
        )

    target_results: List[dict] = []
    overall = True
    for target in targets:
        cmd = [
            str(runner), target,
            "--acceptance",
            "--no-coverage",
        ]
        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                cwd=str(stack_root),
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
            )
            stdout = result.stdout or ""
            rc = result.returncode
        except subprocess.TimeoutExpired:
            stdout = f"timed out after {PROBE_TIMEOUT_SECONDS}s"
            rc = 124  # conventional timeout exit
        except Exception as e:  # never crash the apply action
            stdout = f"probe runner crashed: {e!r}"
            rc = 125
        duration_ms = int((time.monotonic() - start) * 1000)
        passed = (rc == 0)
        if not passed:
            overall = False
        # Last 20 lines for the audit trail; full output is in the
        # tests/results/<run-id>/ that razzfazz-test.sh wrote.
        tail = "\n".join(stdout.splitlines()[-20:])
        target_results.append({
            "target": target,
            "pass": passed,
            "duration_ms": duration_ms,
            "stdout_tail": tail,
        })
        if action is not None:
            verdict = "PASS" if passed else "FAIL"
            action.add_line(
                f"    [post-toggle probe] {target}: {verdict} "
                f"({duration_ms} ms)"
            )
            if not passed and tail:
                action.add_line(
                    f"      tail: {tail.splitlines()[-1] if tail else ''}"
                )

    return {
        "overall_pass": overall,
        "targets": target_results,
        "skipped_reason": None,
    }


def summarize_for_audit(probe_result: dict) -> dict:
    """Strip the tail strings from the per-target results before audit-logging.

    Audit entries are JSON-lines and read by humans + log-aggregation
    tools; we keep the structure but drop the noisy stdout tails (those
    live in tests/results/ already).
    """
    return {
        "overall_pass": probe_result.get("overall_pass"),
        "skipped_reason": probe_result.get("skipped_reason"),
        "targets": [
            {
                "target": t.get("target"),
                "pass": t.get("pass"),
                "duration_ms": t.get("duration_ms"),
            }
            for t in probe_result.get("targets") or []
        ],
    }
