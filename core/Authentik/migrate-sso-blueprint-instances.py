# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2522 — move the Google/Entra SSO blueprint instances onto their new file names.

Until 2026.09-ga.4, init-authentik.sh copied core/Authentik/blueprints/google/*
and entra/* into ONE flat /blueprints directory under their bare names
(10-property-mappings.yaml … 13-source.yaml). With both providers enabled the
Entra files overwrote the Google ones. Authentik names an instance once, at
discovery, from the file's metadata, and later applies only whatever the path
holds — so prod 8.246 has a "Google - Flows" instance at `12-flows.yaml`
applying Entra content, and no Entra instance at all (agent-devbox, #2501).

init-authentik.sh now deploys `google-<file>` and `entra-<file>`. Authentik
(version/2026.5.7, authentik/blueprints) then behaves like this:

  * discovery (`check_blueprint_v1_file`) finds an instance by `path` only; for
    a new path it CREATES one named after metadata.name;
  * `BlueprintInstance.name` is UNIQUE — so creating "Google - Flows" for
    google-12-flows.yaml while the old "Google - Flows" still sits at
    12-flows.yaml raises IntegrityError inside the discovery loop;
  * `clear_failed_blueprints` (hourly) DELETES an instance whose file is gone,
    and with it (GenericRelation) its task history.

So this runs on the full path after the old files are removed and BEFORE the
renamed files are written: every instance at an old bare SSO path whose name is
the metadata name of an ENABLED provider's file is repointed to that provider's
new path. That keeps the instance's identity (pk), its `context`, its `enabled`
switch and its task history. `last_applied_hash` is cleared so discovery
re-applies the instance's OWN file once — the prod "Google - Flows" instance
last applied Entra content. The provider left without an instance (Entra on
prod) gets a fresh one from discovery, and no name collides.

Conservative and idempotent:

  * no instance at the old path                   -> no-op ("absent")
  * name is an enabled provider's file name       -> repoint ("CHANGED")
  * ... but the new path already has an instance  -> no change, WARN
  * name is a DISABLED provider's file name       -> no change ("orphan"): its
    file is gone and never comes back; Authentik's own clear_failed_blueprints
    retires it, exactly as it did before for a switched-off provider
  * any other name (operator-renamed)             -> no change, WARN

Nothing is deleted here. Runs inside authentik-worker (Django + the Authentik DB
connection), like repair-managed-scope-mappings.py. Never fails the caller:
exit 0 always; a problem is a line starting with WARN(#2522).

Env: RZFZ_SSO_PROVIDERS — space-separated providers init deploys this run
(`google`, `entra`).
"""
from __future__ import annotations

import os
import sys

#: provider -> {source basename: metadata.name}, verbatim from
#: core/Authentik/blueprints/<provider>/*.yaml (a consistency test holds them equal).
SSO_BLUEPRINTS: dict[str, dict[str, str]] = {
    "google": {
        "10-property-mappings.yaml": "Google - Property Mappings",
        "11-policies.yaml": "Google - Policies",
        "12-flows.yaml": "Google - Flows",
        "13-source.yaml": "Google - SSO Source",
    },
    "entra": {
        "10-property-mappings.yaml": "Entra - Property Mappings",
        "11-policies.yaml": "Entra - Policies",
        "12-flows.yaml": "Entra - Flows",
        "13-source.yaml": "Entra - SSO Source",
    },
}

# Authentik's table: BlueprintInstance (authentik_blueprints), name UNIQUE,
# unique_together (name, path); path is NOT unique on its own.
SQL_AT_PATH = ("SELECT instance_uuid, name FROM authentik_blueprints_blueprintinstance "
               "WHERE path = %s ORDER BY name")
SQL_REPOINT = ("UPDATE authentik_blueprints_blueprintinstance "
               "SET path = %s, last_applied_hash = '' "
               "WHERE instance_uuid = %s AND path = %s")


def new_path(provider: str, basename: str) -> str:
    """The target name init-authentik.sh's copy_sso_blueprints writes."""
    return f"{provider}-{basename}"


def migrate(cursor, providers, out=print) -> dict[str, str]:
    """Repoint old bare-path SSO instances through a DB-API cursor (%s placeholders).

    Returns {"<old path>|<name>": outcome} (or {"<old path>": "absent"}), with
    outcome in changed | absent | orphan | unknown-name | conflict-new-path | race.
    """
    enabled = [p for p in SSO_BLUEPRINTS if p in set(providers)]
    owner_of: dict[tuple[str, str], str] = {}
    for provider, files in SSO_BLUEPRINTS.items():
        for basename, name in files.items():
            owner_of[(basename, name)] = provider
    basenames = sorted({b for files in SSO_BLUEPRINTS.values() for b in files})

    result: dict[str, str] = {}
    for basename in basenames:
        cursor.execute(SQL_AT_PATH, (basename,))
        rows = list(cursor.fetchall())
        if not rows:
            result[basename] = "absent"
            continue
        for pk, name in rows:
            key = f"{basename}|{name}"
            owner = owner_of.get((basename, name))
            if owner is None:
                result[key] = "unknown-name"
                out(f"WARN(#2522): instance '{name}' ({pk}) at {basename} is not one of "
                    f"ours by name — left unchanged; Authentik removes it once its file is gone.")
                continue
            if owner not in enabled:
                result[key] = "orphan"
                out(f"SSO blueprints: '{name}' ({pk}) at {basename} belongs to {owner}, which "
                    f"is not deployed — left for Authentik's clear_failed_blueprints (#2522).")
                continue
            target = new_path(owner, basename)
            cursor.execute(SQL_AT_PATH, (target,))
            taken = list(cursor.fetchall())
            if taken:
                result[key] = "conflict-new-path"
                out(f"WARN(#2522): '{name}' ({pk}) at {basename} not moved — {target} already "
                    f"has instance(s) {', '.join(repr(t[1]) for t in taken)}; left for "
                    f"Authentik's clear_failed_blueprints.")
                continue
            cursor.execute(SQL_REPOINT, (target, pk, basename))
            if cursor.rowcount == 1:
                result[key] = "changed"
                out(f"CHANGED(#2522): '{name}' ({pk}) path {basename} -> {target} "
                    f"(identity, context and history kept; re-applies its own file)")
            else:
                result[key] = "race"
                out(f"WARN(#2522): '{name}' ({pk}) changed under us — left as it is now.")
    return result


def main() -> int:
    providers = os.environ.get("RZFZ_SSO_PROVIDERS", "").split()
    sys.path.append("/")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
    import django
    django.setup()
    from django.db import connection, transaction

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                result = migrate(cursor, providers)
    except Exception as exc:  # noqa: BLE001 — never fail init; say so instead
        print(f"WARN(#2522): SSO blueprint instance migration rolled back: {exc}")
        return 0
    changed = sorted(k for k, v in result.items() if v == "changed")
    if changed:
        print(f"SSO blueprints: moved {len(changed)} instance(s) to the per-provider file names.")
    else:
        print("SSO blueprints: no instance to move (providers: "
              f"{' '.join(providers) or 'none'}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
