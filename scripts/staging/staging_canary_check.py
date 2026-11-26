# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""scripts/staging/staging_canary_check.py — #392: after a staging refresh, check
that the canary group exists and holds at least the expected number of ACTIVE
members, plus an optional short spot-check list of usernames.
Runs inside authentik-worker:

    docker cp scripts/staging/staging_canary_check.py authentik-worker:/tmp/canary_check.py
    docker exec -i authentik-worker python /tmp/canary_check.py < canary.json
    # canary.json: {"group": "...", "min_members": 10, "members": ["username", ...]}

The canary group is the box's access group (RAZZFAZZ_ACCESS_GROUP, applied by
core/Authentik/apply-policy-bindings.py). Staging admits everyone who has chat on
the production box, so the group is "razzfazz.ai Chat Users": its membership is
maintained on the production box and rides every restore. Authentik's own
bootstrap superuser is not in it, by design. The check counts members rather
than naming every person in the repo; `members` stays an optional spot-check list.

Read-only.
Exit: 0 ok; 10 a spot-check member is missing (warning only);
      2 the group is missing, empty, or below min_members active members;
      3 the check itself failed (Django, the ORM, the payload): never a pass.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

RC_OK, RC_GROUP, RC_ERROR, RC_SPOT_CHECK = 0, 2, 3, 10


def check(members_in_group, expected, min_members=1):
    """members_in_group: the ACTIVE members' usernames, or None for no group."""
    if isinstance(min_members, bool) or not isinstance(min_members, int) or min_members < 1:
        return 2, [f"min_members must be a whole number >= 1, got {min_members!r} (canary_min_members in the staging config)"]
    if members_in_group is None:
        return 2, ["canary group not found — the base blueprints did not create it (check 02-groups.yaml and the init marker)"]
    if not members_in_group:
        return 2, ["canary group has no active member — nobody can use this box; check the group on the production box, then refresh"]
    n = len(members_in_group)
    if n < min_members:
        return 2, [f"canary group has {n} active member(s), expected at least {min_members} — "
                   "the restore did not bring production's chat users (wrong backup, or the group was emptied); "
                   "check the group on the production box, then refresh"]
    lines = [f"canary group: {n} active member(s) (at least {min_members} expected)"]
    missing = sorted(set(expected) - set(members_in_group))
    if missing:
        lines.append(f"expected but not in the group: {', '.join(missing)} — add them on the production box; "
                     "the next refresh carries the membership (a change made here is lost at the next restore)")
        return RC_SPOT_CHECK, lines
    return RC_OK, lines


def run(m, group_name, expected, min_members=1):
    group = m.Group.objects.filter(name=group_name).first()
    # only active accounts count: a deactivated member cannot sign in to staging
    members = None if group is None else sorted(u.username for u in group.users.all() if u.is_active)
    rc, lines = check(members, expected, min_members)
    return rc, [f"{group_name!r}: {line}" for line in lines]


def _main() -> int:
    import django
    sys.path.insert(0, "/")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
    django.setup()
    from types import SimpleNamespace
    from authentik.core.models import Group
    cfg = json.load(sys.stdin)
    rc, lines = run(SimpleNamespace(Group=Group), cfg["group"], cfg.get("members") or [],
                    cfg.get("min_members", 1))
    for line in lines:
        print(f"[canary-check] {line}")
    return rc


def main() -> int:
    """Any failure of the check itself is exit 3: a check that cannot look must
    never read as a pass or as a warning."""
    try:
        return _main()
    except BaseException as exc:   # noqa: BLE001 - SystemExit/KeyboardInterrupt included
        if isinstance(exc, SystemExit) and exc.code in (RC_OK, RC_GROUP, RC_SPOT_CHECK):
            raise
        print(f"[canary-check] ERROR: the check could not run: {type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stderr)
        return RC_ERROR


if __name__ == "__main__":
    sys.exit(main())
