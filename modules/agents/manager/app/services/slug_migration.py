# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""E1 (#2518): back-fill the volume names an instance ACTUALLY mounts.

Why: recreate (upgrade, start's self-heal) and delete used to recompute every
volume name from `row.user_slug` + the current catalog. After the #192
legacy-slug forward migration the row slug no longer matched the names the
volumes were created with, so on 2026-09-26 a recreate handed the operator's
opencode a fresh, EMPTY set (the data sat detached under the old names), and a
delete left the real volumes behind.

Since migration 14 every launch and recreate records its volume names on the
row (`agent_instances.mounted_volumes`). Rows created before that carry NULL;
this module fills them ONCE from the running container's Mounts:

  * reads only Mounts of Type=volume (never a bind mount, never a copy);
  * never overwrites a persisted value (`WHERE mounted_volumes IS NULL`), so
    re-running it is a no-op;
  * never renames a volume and never touches `user_slug`;
  * runs at every agent-manager start (`run_startup_backfill`). Docker
    unreachable → skipped, logged, retried on the next start. Never fatal.

Rows it cannot fill (container gone) stay NULL; recreate then falls back to
the container_name pattern and REFUSES rather than create empties
(provisioner._pick_volumes).

CLI (read-only by default):
    python3 -m app.services.slug_migration            # dry run: list the plan
    python3 -m app.services.slug_migration --json     # the same, as JSON
    python3 -m app.services.slug_migration --apply    # write the back-fill
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import uuid

from app.services.provisioner import (instance_containers, instance_suffix,
                                      normalise_mounted_volumes)

logger = logging.getLogger(__name__)

#: `make_user_slug`'s shape: <=14 readable chars, '-', 6 hex. Anything else
#: in `user_slug` is a pre-hash legacy slug (#192).
_CANONICAL_SLUG_RE = re.compile(r'^[a-z0-9-]{0,14}-[0-9a-f]{6}$')


def slug_form(slug: str | None) -> str:
    return 'canonical' if _CANONICAL_SLUG_RE.match(slug or '') else 'legacy'


def split_mounts(row: dict, briefs: list) -> dict | None:
    """{"primary": {...}, "companion": {...}} from container briefs, None when
    no container mounts any volume."""
    primary, companion = {}, {}
    for b in briefs or []:
        mounts = b.get('mounts') if isinstance(b, dict) else None
        if not isinstance(mounts, dict):
            continue
        is_companion = bool(b.get('companion')) or b.get('name') != row.get('container_name')
        (companion if is_companion else primary).update(mounts)
    if not primary and not companion:
        return None
    return {'primary': primary, 'companion': companion}


def make_container_reader(docker_client, get_type):
    """row -> the instance's container briefs (label + exact-name lookup).
    `get_type(type_id)` gives the catalog row for the companion name."""
    def read(row):
        ti = get_type(row.get('agent_type')) or {}
        companion = (f"{row['container_name']}-{ti.get('companion_suffix') or 'workspace'}"
                     if ti.get('companion_image') else None)
        return instance_containers(docker_client, row, companion)
    return read


def build_backfill_plan(rows, read_containers, list_volumes=None,
                        known_suffixes=None) -> list[dict]:
    """The plan, one entry per live instance row. Pure apart from the two
    readers it is handed, so it is unit-testable without docker or a DB.

    action:
      keep     — names already persisted; never overwritten
      backfill — NULL on the row; the running container's Mounts will be written
      skip     — NULL and nothing to read (container gone / mounts no volume /
                 docker unreadable); recreate uses the container_name pattern
    """
    plan = []
    for row in rows or []:
        sfx = instance_suffix(row.get('instance_no'))
        cn = row.get('container_name') or ''
        entry = {
            'instance_id': str(row.get('id')),
            'agent_type': row.get('agent_type'),
            'user_slug': row.get('user_slug'),
            'slug_form': slug_form(row.get('user_slug')),
            'container_name': cn,
            # False = the row slug moved away from the stem the container and
            # its volumes were named with (the 2026-09-26 data-loss shape).
            'container_matches_slug': cn == f"agent-{row.get('agent_type')}-{row.get('user_slug')}{sfx}",
            'state': row.get('state'),
        }
        persisted = normalise_mounted_volumes(row.get('mounted_volumes'))
        if persisted:
            entry.update(action='keep', mounted=persisted,
                         reason='names already persisted; never overwritten')
        else:
            try:
                briefs = read_containers(row)
            except Exception as e:  # noqa: BLE001
                entry.update(action='skip', mounted=None,
                             reason=f'docker unreadable ({e}); retried next start')
                plan.append(entry)
                continue
            mounted = split_mounts(row, briefs)
            if mounted is None:
                entry.update(action='skip', mounted=None,
                             reason='no container mounts a volume; recreate falls back '
                                    'to the container_name pattern or refuses')
            else:
                entry.update(action='backfill', mounted=mounted,
                             reason='from the running container Mounts')
        plan.append(entry)

    if list_volumes is not None:
        _attach_unmounted_candidates(plan, list_volumes, known_suffixes or (lambda t: set()))
    return plan


#: Volume name suffixes a type USED to have (catalog renames), so the report
#: can still show their detached volumes. From #2518: Moltis before M030 had
#: `-config`/`-data`.
HISTORICAL_SUFFIXES = {'moltis': {'config', 'data'}}


def catalog_suffixes(get_type):
    """agent_type -> the catalog's name_suffixes (primary + companion) plus the
    historical ones above."""
    def known(agent_type):
        ti = get_type(agent_type) or {}
        out = set(HISTORICAL_SUFFIXES.get(agent_type, set()))
        for field in ('volumes', 'companion_volumes'):
            raw = ti.get(field) or '[]'
            try:
                specs = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                specs = []
            out |= {v['name_suffix'] for v in specs or []
                    if isinstance(v, dict) and v.get('name_suffix')}
        return out
    return known


def _attach_unmounted_candidates(plan, list_volumes, known_suffixes) -> None:
    """Report only: volumes named exactly `<stem>-<known suffix>` for an
    instance's stems (its container_name, and its user_slug stem when that
    differs) that no live instance mounts or records — detached data an
    operator may want to look at. Fix round 1: exact suffixes, not a bare
    `<stem>-` prefix (stem `agent-moltis-alexander` would otherwise list
    `agent-moltis-alexander-vukovic-*`, someone else's)."""
    claimed = set()
    for e in plan:
        m = e.get('mounted') or {}
        claimed |= set(m.get('primary') or {}) | set(m.get('companion') or {})
    for e in plan:
        stems = {e['container_name']}
        if not e['container_matches_slug']:
            stems.add(f"agent-{e['agent_type']}-{e['user_slug']}")
        suffixes = known_suffixes(e['agent_type']) or set()
        found = set()
        for stem in filter(None, stems):
            wanted = {f'{stem}-{sfx}' for sfx in suffixes}
            if not wanted:
                continue
            try:
                names = list_volumes(prefix=f'{stem}-')
            except Exception as ex:  # noqa: BLE001
                e['unmounted_candidates_error'] = str(ex)
                continue
            found |= {n for n in names if n in wanted and n not in claimed}
        e['unmounted_candidates'] = sorted(found)


def apply_backfill(db, plan) -> int:
    """Write every `backfill` entry (only where the row is still NULL).
    Returns how many rows were written."""
    written = 0
    for e in plan:
        if e.get('action') != 'backfill':
            continue
        if db.backfill_mounted_volumes(uuid.UUID(e['instance_id']), e['mounted']):
            written += 1
            logger.info("mounted_volumes back-filled for %s (%s): %s", e['instance_id'],
                        e['container_name'], sorted(e['mounted']['primary']))
    return written


def run_startup_backfill(db, docker_client, get_type) -> dict:
    """The once-per-start back-fill. Never raises."""
    try:
        reachable = bool(docker_client.ping())
    except Exception:  # noqa: BLE001
        reachable = False
    if not reachable:
        logger.warning("mounted_volumes back-fill (#2518) skipped: docker unreachable; "
                       "retried on the next agent-manager start")
        return {'skipped': 'docker unreachable'}
    try:
        rows = db.list_live_instance_volume_rows()
        plan = build_backfill_plan(rows, make_container_reader(docker_client, get_type))
        written = apply_backfill(db, plan)
        pending = [e for e in plan if e['action'] == 'skip']
        logger.info("mounted_volumes back-fill (#2518): %d written, %d already persisted, "
                    "%d without a readable container", written,
                    sum(1 for e in plan if e['action'] == 'keep'), len(pending))
        for e in pending:
            logger.warning("  not back-filled: %s (%s, slug %s): %s", e['instance_id'],
                           e['container_name'], e['user_slug'], e['reason'])
        return {'written': written, 'plan': plan}
    except Exception:  # noqa: BLE001
        logger.warning("mounted_volumes back-fill (#2518) failed; retried on the next "
                       "start", exc_info=True)
        return {'skipped': 'error'}


def format_plan(plan) -> str:
    lines = []
    for e in plan:
        flag = '' if e['container_matches_slug'] else '  [row slug != container stem]'
        lines.append(f"{e['action'].upper():8} {e['agent_type']:13} {e['container_name']} "
                     f"(id {e['instance_id']}, slug {e['user_slug']} [{e['slug_form']}], "
                     f"{e['state']}){flag}")
        m = e.get('mounted') or {}
        for role in ('primary', 'companion'):
            for name, dest in sorted((m.get(role) or {}).items()):
                lines.append(f"           {role:9} {name} -> {dest}")
        if e['action'] == 'skip':
            lines.append(f"           reason: {e['reason']}")
        for n in e.get('unmounted_candidates') or []:
            lines.append(f"           UNMOUNTED same-stem volume (review, untouched): {n}")
    counts = {a: sum(1 for e in plan if e['action'] == a) for a in ('keep', 'backfill', 'skip')}
    lines.append(f"\n{len(plan)} live instance(s): {counts['keep']} keep, "
                 f"{counts['backfill']} backfill, {counts['skip']} skip")
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog='python3 -m app.services.slug_migration',
        description='#2518: list (default) or write the mounted_volumes back-fill. '
                    'Read-only unless --apply. Never copies, renames or removes a volume.')
    ap.add_argument('--apply', action='store_true',
                    help='write the back-fill for rows whose mounted_volumes is NULL')
    ap.add_argument('--json', action='store_true', help='print the plan as JSON')
    args = ap.parse_args(argv)

    from app.services.database import Database
    from app.services.docker_client import AgentDockerClient
    db = Database(os.environ['AGENT_MANAGER_DATABASE_URL'])
    docker_client = AgentDockerClient.read_only(
        os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock'))
    rows = db.list_live_instance_volume_rows()
    plan = build_backfill_plan(rows, make_container_reader(docker_client, db.get_agent_type),
                               list_volumes=docker_client.list_volume_names,
                               known_suffixes=catalog_suffixes(db.get_agent_type))
    print(json.dumps(plan, indent=2, default=str) if args.json else format_plan(plan))
    if not args.apply:
        print('\nDRY RUN: nothing written. --apply writes the back-fill rows above.')
        return 0
    written = apply_backfill(db, plan)
    print(f'\nAPPLIED: {written} row(s) back-filled.')
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
