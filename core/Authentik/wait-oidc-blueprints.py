# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2507 — wait until the native-OIDC provider blueprints have ACTUALLY applied.

init-authentik.sh only writes the templated blueprint files into /blueprints.
Authentik discovers and applies them ASYNCHRONOUSLY in the worker: on prod
(2026-09-26) both OIDC providers appeared a few seconds AFTER authentik-init
had finished — a discovery check right at init's end still answered 404, then
200. Everything downstream of init (the policy-binding pass, Gitea's add-oauth
discovery fetch, OWUI's login-form reconcile) must therefore wait for the
applied state, not for the file.

"Applied" here means, per expected application slug:
  * the Application exists and has a provider (what the discovery URL serves);
  * its BlueprintInstance, when one is registered, is `successful`;
  * when the caller says it just CHANGED that file (a new or rotated secret on
    the upgrade path), the instance has been applied after this script started
    — an older successful apply of the previous content does not count.

While waiting it nudges once: a discovery run when no instance has appeared
yet, and a serial re-apply (the #1350 way — one `send_with_options`, never an
inline call, which runs nothing) for an instance that is not successful or has
not picked up a changed file. Exit 0 when every slug is applied, 3 otherwise —
the caller prints that loudly but never aborts init on it.

Env (all optional except RZFZ_OIDC_APPS):
  RZFZ_OIDC_APPS       space-separated application slugs to wait for
  RZFZ_OIDC_CHANGED    subset whose blueprint file was (re)written this run
  RZFZ_OIDC_TIMEOUT    seconds to wait in total (default 180)
  RZFZ_OIDC_POLL       seconds between polls (default 3)
  RZFZ_OIDC_KICK_AFTER seconds before the first nudge (default 20)
"""
from __future__ import annotations

import os
import sys
import time

import django

sys.path.append("/")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.blueprints.models import BlueprintInstance  # noqa: E402
from authentik.blueprints.v1 import tasks as bp_tasks  # noqa: E402
from authentik.core.models import Application  # noqa: E402

OK = "successful"


def _instance(slug):
    """The instance init-authentik.sh's `<slug>-<file>.yaml` target produces."""
    return (BlueprintInstance.objects.filter(path__startswith=f"{slug}-")
            .order_by("-last_applied").first())


def _app_ready(slug) -> bool:
    return Application.objects.filter(slug=slug, provider__isnull=False).exists()


def verdict(app_ready: bool, inst, changed: bool, started_at) -> str:
    """Pure: 'applied' | the reason it is not (yet)."""
    if inst is None:
        if changed:
            return "no blueprint instance yet (discovery pending)"
        return "applied" if app_ready else "no blueprint instance yet (discovery pending)"
    if inst.status != OK:
        return f"instance status={inst.status}"
    if changed and (inst.last_applied is None or inst.last_applied < started_at):
        return "instance not yet re-applied with the new file"
    if not app_ready:
        return "instance applied but no application/provider"
    return "applied"


def _nudge(slug, inst) -> None:
    try:
        if inst is None:
            discovery = getattr(bp_tasks, "blueprints_discovery", None)
            if discovery is not None:
                discovery.send()
                print(f"  {slug}: no instance yet — queued a blueprint discovery")
        else:
            bp_tasks.apply_blueprint.send_with_options(args=(str(inst.pk),))
            print(f"  {slug}: queued a re-apply of {inst.path} (status={inst.status})")
    except Exception as exc:  # a nudge is a convenience; the wait is the contract
        print(f"  {slug}: could not queue a nudge ({exc})")


def main() -> int:
    slugs = [s for s in os.environ.get("RZFZ_OIDC_APPS", "").split() if s]
    if not slugs:
        return 0
    changed = set(os.environ.get("RZFZ_OIDC_CHANGED", "").split())
    timeout = float(os.environ.get("RZFZ_OIDC_TIMEOUT", "180"))
    poll = float(os.environ.get("RZFZ_OIDC_POLL", "3"))
    kick_after = float(os.environ.get("RZFZ_OIDC_KICK_AFTER", "20"))

    from django.utils import timezone
    started_at = timezone.now()
    t0 = time.monotonic()
    nudged = set()
    pending = {s: "not checked" for s in slugs}
    while True:
        for slug in list(pending):
            inst = _instance(slug)
            why = verdict(_app_ready(slug), inst, slug in changed, started_at)
            if why == "applied":
                del pending[slug]
                print(f"OIDC blueprint {slug}: applied "
                      f"({time.monotonic() - t0:.0f}s after init wrote it)")
                continue
            pending[slug] = why
            if slug not in nudged and time.monotonic() - t0 >= kick_after:
                _nudge(slug, inst)
                nudged.add(slug)
        if not pending or time.monotonic() - t0 >= timeout:
            break
        time.sleep(poll)

    for slug, why in pending.items():
        print(f"ERROR(#2507): OIDC blueprint {slug} NOT applied after {timeout:.0f}s — {why}. "
              f"/application/o/{slug}/ keeps answering 404 until it is. The reason is in "
              "the authentik-worker log: docker logs authentik-worker | grep -i blueprint")
    return 3 if pending else 0


if __name__ == "__main__":
    sys.exit(main())
