# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Start-up back-fill for per-instance databases (#2529, #1039).

Runs once per agent-manager start, after the #2518 volume back-fill and before
the lifecycle scheduler — before anything can recreate an instance from its
row. A row without `_db_name`/`_db_user` gets the names its container really
connects with (its DATABASE_URL). Names it cannot READ are never persisted:
a row whose container env is unreadable (docker down, container gone, no
DATABASE_URL) is skipped with a warning and retried on the next start; until
then `db_identity_of` resolves it by the #1988 formula, as before. Names that
were read but are not a per-instance pair of the row's type, or that name a
database or role the cluster does not have (a URL a pre-#2529 recreate
recomputed from a rewritten slug), are skipped the same way; C2's repair owns
those rows. Never overwrites; never raises.

2. password (#1039): a row without `_db_password` gets one set on its role and
persisted — the password its running container already connects with when
there is one (so a healthy container is not locked out), else a minted one.
The role is the one the row's next recreate connects with (`db_identity_of`:
the recorded names, else the row's OWN #1988 formula with its instance_no —
never instance #1's for #N), and never a role another live row resolves to or
another container connects as. While docker cannot be read the row is skipped
and retried on the next start (upgrade() repairs again before any recreate).
"""
from __future__ import annotations

import json
import logging

from app.services.provisioner import db_identity_from_env, db_identity_of, instance_suffix

logger = logging.getLogger(__name__)


def _config(row) -> dict:
    cfg = row.get('config') or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (ValueError, TypeError):
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _as_created(ident: tuple[str, str], instance_no) -> tuple[str, str]:
    """The names instance #N's database was CREATED with, given the names its
    container connects with.

    Before #2529, `_resolve_env` dropped the #1988 suffix while
    `_create_instance_db` kept it, so the container of instance #N>1 carries
    instance #1's database and role (`agent_<type>_<launch slug>`), not the
    `…_N` pair that was created for it. Recording those would point the row
    - and its delete, which drops by the recorded names - at instance #1's
    database. The launch slug in the URL is still right (the row's may have
    been rewritten since), so the suffix is added back. Slugs never contain
    `_` (razzfazz_common.user_slug), so a trailing `_N` is always the suffix.
    """
    sfx = instance_suffix(instance_no).replace('-', '_')
    name, user = ident
    if not sfx or user.endswith(sfx):
        return ident
    logger.warning("#2529: instance #%s connects with instance #1's role %s; "
                   "recording its own %s%s", instance_no, user, user, sfx)
    return f"{user}{sfx}_db", f"{user}{sfx}"


def _unusable(db, row, ident) -> str | None:
    """Why `ident` must not be recorded for `row`, or None when it may.

    Delete drops whatever is recorded, and nothing overwrites it, so a pair is
    recorded only when it has the per-instance shape of the row's type and both
    the database and the role exist in the cluster."""
    name, user = ident
    prefix = f"agent_{row['agent_type']}_"
    if not user.startswith(prefix) or name != f"{user}_db":
        return f"that is not a per-instance {row['agent_type']} pair"
    try:
        has_db, has_role = db.per_instance_db_exists(name, user)
    except Exception:  # noqa: BLE001 — cannot confirm is "do not record"
        logger.warning("#2529: could not look up %s / %s", name, user, exc_info=True)
        return "the cluster could not be asked whether they exist"
    if not has_db:
        return "that database does not exist"
    if not has_role:
        return "that role does not exist"
    return None


def backfill_db_identity(db, rows, read_env) -> int:
    """Persist `_db_name`/`_db_user` on every row that lacks them. `rows` are
    updated in place (their `config` becomes the parsed, patched dict), so a
    later pass over the same rows sees the names. A row whose names cannot be
    read from its container is left untouched (retried on the next start) —
    a guess persisted here could never be corrected, since nothing
    overwrites. Returns the rows written."""
    written = 0
    for row in rows:
        cfg = _config(row)
        row['config'] = cfg
        if cfg.get('_db_name') and cfg.get('_db_user'):
            continue
        try:
            env = read_env(row['container_name'])
        except Exception:  # noqa: BLE001 — unreadable is "no answer"
            env = None
        ident = db_identity_from_env(env)
        if ident is None:
            logger.warning("#2529: instance %s (%s): no readable DATABASE_URL "
                           "(docker unreadable or container gone); DB names left "
                           "unset, retried on the next start",
                           row['id'], row['container_name'])
            continue
        ident = _as_created(ident, row.get('instance_no') or 1)
        why = _unusable(db, row, ident)
        if why:
            logger.warning("#2529: instance %s (%s): DATABASE_URL names %s / %s, but %s; "
                           "DB names left unset", row['id'], row['container_name'],
                           ident[0], ident[1], why)
            continue
        patch = {'_db_name': ident[0], '_db_user': ident[1]}
        db.merge_instance_config(row['id'], patch)
        cfg.update(patch)
        written += 1
    return written


def backfill_db_passwords(provisioner, rows) -> int:
    """#1039: repair every row that has no `_db_password`. Run after
    backfill_db_identity over the same rows, so a row it named is repaired
    under those names; a row it left unnamed is repaired under the role its
    recreate connects with — its own instance_no, never instance #1's.
    `ensure_db_password` reads what every agent container connects with: it
    keeps a running container's password, refuses a role another container
    uses, and skips (retried on the next start) while docker cannot be read.
    Returns the rows repaired."""
    repaired = 0
    for row in rows:
        cfg = _config(row)
        row['config'] = cfg
        if cfg.get('_db_password'):
            continue
        try:
            _, db_user = db_identity_of(cfg, row['agent_type'], row['user_slug'],
                                        row.get('instance_no') or 1)
            if provisioner.ensure_db_password(row['id'], cfg, db_user,
                                              row.get('container_name')):
                repaired += 1
        except Exception:  # noqa: BLE001 — one bad row must not stop the rest
            logger.warning("#1039: could not repair the DB password of %s",
                           row.get('container_name'), exc_info=True)
    return repaired


def run_startup_db_backfill(db, docker_client, provisioner) -> dict:
    """The once-per-start pass. Never raises."""
    try:
        rows = list(db.list_live_db_instance_rows())
        identity = backfill_db_identity(db, rows, docker_client.get_container_env)
        passwords = backfill_db_passwords(provisioner, rows)
        logger.info("per-instance DB back-fill: %d name record(s) written, "
                    "%d password(s) repaired", identity, passwords)
        return {'identity': identity, 'passwords': passwords}
    except Exception:  # noqa: BLE001
        logger.warning("per-instance DB back-fill failed; retried on the next start",
                       exc_info=True)
        return {'skipped': 'error'}
