# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2501 — give Authentik's built-in OAuth2 scope mappings back their `managed` key.

Authentik's own system blueprint `system/providers-oauth2.yaml` ("System -
OAuth2 Provider - Scopes") identifies each built-in scope mapping by its
`managed` key and sets its `name` in attrs. Measured on prod 8.246
(2026-09-26, agent-devbox, dry-run `Importer.validate()`): the row
"authentik default OAuth Mapping: OpenID 'email'" carries `managed = NULL`.
The importer's lookup by `managed=goauthentik.io/providers/oauth2/scope-email`
misses, it tries to CREATE a second mapping with the same name, and
`PropertyMapping.name` is unique -> "Property Mapping with this name already
exists". The whole system blueprint has sat in `error` since 2026-08-06, so no
Authentik update to any of its mappings lands.

This is a DATA defect. We do not ship that blueprint and do not edit it; we
put the key back on the existing row, so the importer's lookup resolves it and
updates it in place. Idempotent and conservative:

  * a row already holds the key under the expected name   -> no-op ("ok")
  * a DIFFERENT row already holds the key                 -> no change, WARN
    (`managed` is UNIQUE; the system blueprint would rename that row onto the
    taken name — an operator has to look)
  * the named row is a scope mapping for the right scope with managed NULL
                                                          -> set it ("CHANGED")
  * the named row carries some OTHER managed key          -> no change, WARN
  * no such row at all (fresh box)                        -> no-op; Authentik's
    system blueprint creates it

The table below is the six scope mappings of system/providers-oauth2.yaml at
Authentik version/2026.5.7 (the pinned AUTHENTIK_VERSION in core/compose.yml).
Only the email one was found broken; the others get the same guard because the
failure mode is identical and the guard is a no-op on a healthy row.

Runs inside authentik-worker (Django + the Authentik DB connection), like
reconcile-blueprints.py. Never fails the caller: exit 0 always; a problem is a
line starting with WARN(#2501).
"""
from __future__ import annotations

import hashlib
import os
import sys

#: managed key -> (name, scope_name), verbatim from Authentik's
#: blueprints/system/providers-oauth2.yaml at version/2026.5.7.
SYSTEM_SCOPE_MAPPINGS: dict[str, tuple[str, str]] = {
    "goauthentik.io/providers/oauth2/scope-openid":
        ("authentik default OAuth Mapping: OpenID 'openid'", "openid"),
    "goauthentik.io/providers/oauth2/scope-email":
        ("authentik default OAuth Mapping: OpenID 'email'", "email"),
    "goauthentik.io/providers/oauth2/scope-profile":
        ("authentik default OAuth Mapping: OpenID 'profile'", "profile"),
    "goauthentik.io/providers/oauth2/scope-entitlements":
        ("authentik default OAuth Mapping: Application Entitlements", "entitlements"),
    "goauthentik.io/providers/oauth2/scope-offline_access":
        ("authentik default OAuth Mapping: OpenID 'offline_access'", "offline_access"),
    "goauthentik.io/providers/oauth2/scope-authentik_api":
        ("authentik default OAuth Mapping: authentik API access", "goauthentik.io/api"),
}

# Authentik's tables: PropertyMapping (authentik_core, name UNIQUE, managed
# UNIQUE NULL) and its multi-table child ScopeMapping (authentik_providers_oauth2).
SQL_HOLDER = ("SELECT pm_uuid, name FROM authentik_core_propertymapping "
              "WHERE managed = %s")
SQL_NAMED = ("SELECT pm.pm_uuid, pm.managed, sm.scope_name "
             "FROM authentik_core_propertymapping pm "
             "LEFT JOIN authentik_providers_oauth2_scopemapping sm "
             "ON sm.propertymapping_ptr_id = pm.pm_uuid "
             "WHERE pm.name = %s")
SQL_SET = ("UPDATE authentik_core_propertymapping SET managed = %s "
           "WHERE pm_uuid = %s AND managed IS NULL")
SQL_EXPR = "SELECT expression FROM authentik_core_propertymapping WHERE pm_uuid = %s"


def repair(cursor, out=print) -> dict[str, str]:
    """Apply the table above through a DB-API cursor (%s placeholders).

    Returns {managed_key: outcome} with outcome in
    ok | changed | absent | conflict-holder | conflict-named | not-a-scope-mapping | race.
    """
    result: dict[str, str] = {}
    for key, (name, scope) in SYSTEM_SCOPE_MAPPINGS.items():
        cursor.execute(SQL_HOLDER, (key,))
        holder = cursor.fetchone()
        if holder is not None:
            if holder[1] == name:
                result[key] = "ok"
            else:
                result[key] = "conflict-holder"
                out(f"WARN(#2501): managed={key} is held by '{holder[1]}' ({holder[0]}), "
                    f"not by '{name}' — left unchanged; Authentik's system blueprint "
                    f"would rename it onto that name. Resolve by hand.")
            continue
        cursor.execute(SQL_NAMED, (name,))
        named = cursor.fetchone()
        if named is None:
            result[key] = "absent"
            continue
        pm_uuid, managed, scope_name = named
        if managed is not None:
            result[key] = "conflict-named"
            out(f"WARN(#2501): '{name}' carries managed={managed}, not {key} — left unchanged.")
            continue
        if scope_name != scope:
            result[key] = "not-a-scope-mapping"
            out(f"WARN(#2501): '{name}' ({pm_uuid}) is not a scope mapping for scope "
                f"'{scope}' (scope_name={scope_name!r}) — left unchanged.")
            continue
        # #2536 item 15: Authentik's system blueprint rewrites this row's
        # expression once the key resolves it. Record what it was (hash only).
        cursor.execute(SQL_EXPR, (pm_uuid,))
        row = cursor.fetchone()
        old_hash = hashlib.sha256(((row[0] if row else "") or "").encode("utf-8")).hexdigest()[:12]
        cursor.execute(SQL_SET, (key, pm_uuid))
        if cursor.rowcount == 1:
            result[key] = "changed"
            out(f"CHANGED(#2501): '{name}' ({pm_uuid}) managed NULL -> {key}; "
                f"old expression sha256 {old_hash} (the system blueprint now rewrites it; "
                f"compare if the row was customised, #2536)")
        else:
            result[key] = "race"
            out(f"WARN(#2501): '{name}' ({pm_uuid}) changed under us — left as it is now.")
    return result


def main() -> int:
    sys.path.append("/")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
    import django
    django.setup()
    from django.db import connection, transaction

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                result = repair(cursor)
    except Exception as exc:  # noqa: BLE001 — never fail init; say so instead
        print(f"WARN(#2501): scope-mapping repair rolled back: {exc}")
        return 0
    changed = sorted(k for k, v in result.items() if v == "changed")
    if changed:
        print(f"Scope mappings: set `managed` on {len(changed)} built-in row(s); "
              "Authentik's system blueprint now updates them in place.")
    else:
        print("Scope mappings: nothing to repair "
              + ", ".join(f"{k.rsplit('/', 1)[-1]}={v}" for k, v in result.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
