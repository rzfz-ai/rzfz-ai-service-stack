# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Direction rule for version pins of services that migrate their data.

Python port of the CLI's ``pin_sync_decision`` (cli/upgrade.sh): a pin of a
service whose database a newer version has migrated is never LOWERED, and a
value that cannot be ordered against the current one is not applied either.
Every other pin moves in either direction, as before.

``STATEFUL_VERSION_PINS`` must list the same variables as the shell's
``STATEFUL_VERSION_PINS``; a unit test compares the two.
"""
from __future__ import annotations

import re

STATEFUL_VERSION_PINS = frozenset({
    'AUTHENTIK_VERSION', 'KOMODO_DB_VERSION', 'DIFY_VERSION', 'GITEA_VERSION',
    'OPENWEBUI_VERSION', 'VALKEY_VERSION', 'WAZUH_VERSION',
    'CLICKHOUSE_VERSION', 'OBSERVABILITY_VERSION',
})


def pin_token(value):
    """The orderable part of a tag (``v1.5.7``->(1,5,7,0), ``pg17``->(17,0)),
    or None when the value has no leading number. Same shapes as the CLI's
    ``_pin_version_token``: strip v/pg, dashes count as dots, at most 4 parts."""
    s = re.sub(r'^(v|pg)', '', value or '').replace('-', '.')
    m = re.match(r'^[0-9]+(\.[0-9]+)*', s)
    if not m:
        return None
    parts = [int(p) for p in m.group(0).split('.')[:4]]
    if len(parts) == 1:
        parts.append(0)
    return tuple(parts)


def pin_order(have, want):
    """'lt' | 'eq' | 'gt' | 'unknown' — ``have`` against ``want``."""
    a, b = pin_token(have), pin_token(want)
    if a is None or b is None:
        return 'unknown'
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return 'lt' if a < b else 'gt' if a > b else 'eq'


def pin_sync_decision(pin, have, want):
    """Return ``(True, '')`` to write ``pin := want`` over ``have``, or
    ``(False, message)`` to keep ``have``. No current value (``have`` empty)
    means nothing to protect."""
    if pin not in STATEFUL_VERSION_PINS or not have:
        return True, ''
    order = pin_order(have, want)
    if order == 'gt':
        return False, (f'{pin}: NOT lowered {have} → {want} (running: unknown). '
                       f'Downgrading a migrated database is not supported — keeping {have}. '
                       'To lower it deliberately, edit .env by hand, at your own risk.')
    if order == 'unknown':
        return False, (f'{pin}: cannot order {have} against {want} — not changed, because a '
                       'downgrade of a migrated database is not supported. '
                       f'Set it by hand if {want} is newer.')
    return True, ''
