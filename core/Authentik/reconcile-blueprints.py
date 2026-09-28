# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1350/#1370 — re-apply failed blueprint instances SERIALLY, and leave a trace.

#1350: after a mass apply (every upgrade, every init with many modules) a
variable set of module blueprints sits in `error`. Measured on 0.79 across the
ga.15 → 2026.09 upgrade: 12 instances errored; re-queueing all 12 at once left
6; the remaining 4 applied one at a time, 20 s apart, ALL went green, with zero
deadlocks in the serial window. The cause is contention, not content — each of
those blueprints creates a forward-auth provider, every provider change fires
`outpost_send_update` on the ONE embedded-outpost row, and dozens of those at
once deadlock in postgres. Nothing retries them; they stay `error` until
authentik's own periodic apply happens by.

#1370: a failed instance is invisible. `BlueprintInstance` has no error column
(created, last_updated, managed, instance_uuid, name, metadata, path, context,
last_applied, last_applied_hash, status, enabled, managed_models, content), the
reason exists only in the worker log at apply time — where #1350's deadlock
lines bury it — and `post-install --verify` reports reachability, not state. So
this script writes what it saw to `/blueprints/.rzfz-blueprint-status.json`,
which outlives the run and the log, and which the verify step reads. That also
catches the TRANSIENT case (0.91, 2026-09-05: 34-hub.yaml was `error` at
00:52:42Z and `successful` at 00:57:51Z with nobody touching it — five minutes
of missing application and bindings that left no trace at all).

Never fails the caller: a box whose blueprints all applied is the normal case,
and a box where they did not is worse off if init aborts here.
"""
from __future__ import annotations

import json
import os
import sys
import time

import django

sys.path.append("/")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.blueprints.models import BlueprintInstance  # noqa: E402
from authentik.blueprints.v1.tasks import apply_blueprint  # noqa: E402

#: One at a time, with a gap — that is the whole finding. Overridable so an
#: operator on a slow box can widen it without editing the image.
ROUNDS = int(os.environ.get("RZFZ_BLUEPRINT_RETRY_ROUNDS", "3"))
GAP_SECONDS = float(os.environ.get("RZFZ_BLUEPRINT_RETRY_GAP", "20"))
POLL_SECONDS = float(os.environ.get("RZFZ_BLUEPRINT_RETRY_POLL", "30"))
STATUS_FILE = os.environ.get("RZFZ_BLUEPRINT_STATUS_FILE", "/blueprints/.rzfz-blueprint-status.json")

OK = "successful"


def _broken():
    """Instances that are not successful, oldest apply first.

    `enabled=False` instances are excluded: an operator who switched one off
    has said it should not apply, and reporting it as a failure every run is
    how a check gets ignored.
    """
    return list(BlueprintInstance.objects.exclude(status=OK)
                .filter(enabled=True).order_by("last_applied"))


def _apply_one(inst) -> str:
    """Queue ONE apply and wait for the row to settle. Returns the new status.

    `.send_with_options()`, not a direct call: apply_blueprint is a dramatiq
    actor, and calling it inline runs nothing and returns immediately — the
    silent no-op that made an earlier diagnosis round read as 'the retry did
    not help'.
    """
    apply_blueprint.send_with_options(args=(str(inst.pk),))
    deadline = time.monotonic() + POLL_SECONDS
    while time.monotonic() < deadline:
        time.sleep(1.0)
        inst.refresh_from_db()
        if inst.status == OK:
            return inst.status
    inst.refresh_from_db()
    return inst.status


def main() -> int:
    total = BlueprintInstance.objects.count()
    broken = _broken()
    seen = {str(b.pk): {"name": b.name, "path": b.path, "status_before": b.status}
            for b in broken}

    if not broken:
        print(f"Blueprints: all {total} instances successful.")
    else:
        print(f"Blueprints: {len(broken)} of {total} not successful — re-applying "
              f"one at a time ({ROUNDS} rounds, {GAP_SECONDS:g}s apart). #1350")
        for rnd in range(1, ROUNDS + 1):
            todo = _broken()
            if not todo:
                break
            for i, inst in enumerate(todo):
                status = _apply_one(inst)
                mark = "ok" if status == OK else status
                print(f"  round {rnd}: {inst.name} ({inst.path}) -> {mark}")
                seen.setdefault(str(inst.pk), {"name": inst.name, "path": inst.path,
                                               "status_before": inst.status})
                if i + 1 < len(todo):
                    time.sleep(GAP_SECONDS)

    remaining = _broken()
    for inst in remaining:
        seen.setdefault(str(inst.pk), {"name": inst.name, "path": inst.path,
                                       "status_before": inst.status})
    for pk, rec in seen.items():
        rec["status_after"] = OK
    for inst in remaining:
        seen[str(inst.pk)]["status_after"] = inst.status

    if remaining:
        print(f"Blueprints: {len(remaining)} still not successful after {ROUNDS} rounds:")
        for inst in remaining:
            print(f"  FAILED {inst.name} ({inst.path}) status={inst.status}")
        print("  The reason is only in the authentik-worker log at apply time — "
              "`docker logs authentik-worker | grep -v 'deadlock detected'` (#1350 "
              "buries everything else). Content errors survive a retry; "
              "contention does not.")
    elif seen:
        print(f"Blueprints: all {len(seen)} recovered.")

    payload = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total": total,
        "instances": list(seen.values()),
        "still_failing": len(remaining),
    }
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
    except OSError as exc:
        # The trace is a convenience; losing it must not cost the retry.
        print(f"  (could not write {STATUS_FILE}: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
