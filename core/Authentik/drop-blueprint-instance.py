# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2536 item 15 — delete the BlueprintInstance of a blueprint FILE this stack
gated out (core/init-authentik.sh::gate_synapse_blueprint, #2501).

Removing the file left the instance to Authentik's hourly clear_failed_blueprints,
so "Module - Synapse Matrix OIDC" sat in `error` in the meantime. Deleting the
instance never deletes the objects it created. Runs inside authentik-worker
(Django + the Authentik DB connection), like repair-managed-scope-mappings.py.
Never fails the caller: exit 0 always; a problem is a WARN(#2536) line.
Usage: python drop_blueprint_instance.py <path relative to /blueprints>
"""
from __future__ import annotations

import os
import sys

SQL_FIND = ("SELECT instance_uuid, name, status FROM authentik_blueprints_blueprintinstance "
            "WHERE path = %s")
SQL_DROP = "DELETE FROM authentik_blueprints_blueprintinstance WHERE instance_uuid = %s"


def drop(cursor, path: str, out=print) -> int:
    """Delete every instance whose `path` is `path`; return how many."""
    cursor.execute(SQL_FIND, (path,))
    rows = cursor.fetchall()
    for instance_uuid, name, status in rows:
        cursor.execute(SQL_DROP, (instance_uuid,))
        out(f"DROPPED(#2536): blueprint instance '{name}' ({instance_uuid}, status {status}) "
            f"— its file {path} is not deployed on this box")
    return len(rows)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("WARN(#2536): usage: drop_blueprint_instance.py <path>")
        return 0
    sys.path.append("/")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
    import django
    django.setup()
    from django.db import connection, transaction
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                n = drop(cursor, argv[1])
    except Exception as exc:  # noqa: BLE001 — never fail init; say so instead
        print(f"WARN(#2536): dropping the blueprint instance for {argv[1]} rolled back: {exc}")
        return 0
    if not n:
        print(f"Blueprint instance for {argv[1]}: none (nothing to drop).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
