# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Gitea #3187 — image-based `update_available` detection, the idle auto-update
decision, and the `rzfz agents` CLI entrypoint (runs in-container via
`docker exec agent-manager python3 -c '... cli_main()'`).

Measured on 0.91: after `rzfz upgrade` pulls/rebuilds an agent image, an
instance is recreated only when its baked settings change — not when its image
does. The M030-S2 badge compared `image_version` strings only, so a rebuilt
mutable tag (`:latest` — the coding agents, paperclip) showed no badge at all.

The reconciled rule (ONE signal, one badge — the version badge and this one are
not shipped side by side): an instance has an update available when

  * the recorded `image_version` differs from the catalog version (tag bump),
    OR
  * the running container's image ID differs from the ID the catalog image
    ref resolves to locally — primary container AND companion container.

The image-ID compare is the one that catches the post-upgrade case: same tag,
new digest. When docker cannot answer (tag not pulled locally, container gone)
that signal is "cannot tell" and only the version compare decides — the same
"never force on unknown" rule the `_upgrade` no-op guard uses.

The idle window for the automatic update is NOT guessed: it defaults to the
instance's own `idle_timeout` (catalog) with the existing
AGENT_IDLE_TIMEOUT_LIGHTWEIGHT/HEAVY tier fallbacks — the same "idle" the
lifecycle reaper uses.

Configuration path (supported, live): the two knobs are stored in
`agent_settings` (`auto_update_enabled`, `update_idle_window_seconds`) and are
exposed + writable through the agent-manager admin `/api/config` endpoint (the
Config UI's path — the #36/PR#84 live pattern that dodges the .env inode bug).
The container env vars AGENT_AUTO_UPDATE_ENABLED /
AGENT_UPDATE_IDLE_WINDOW_SECONDS remain as the container-level fallback when no
DB value is set. Precedence: agent_settings DB value > env > derived default.

Safety: the pass never recreates an instance with an attached terminal/session
(`app.services.sessions` registry, re-checked inside `_upgrade` under the
per-instance lock), and the `rzfz agents` CLI entrypoint builds the app with
`create_app(background_jobs=False)` so list / --dry-run never start a second
mutating scheduler.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def catalog_image_refs(type_info) -> dict:
    """The catalog's current image refs for a type: primary + companion."""
    ti = type_info or {}
    refs = {}
    if ti.get('image') and ti.get('version'):
        refs['primary'] = f"{ti['image']}:{ti['version']}"
    if ti.get('companion_image'):
        refs['companion'] = f"{ti['companion_image']}:{ti.get('companion_version') or 'latest'}"
    return refs


def companion_container_name(instance, type_info) -> str | None:
    """`<primary>-<suffix>` — the same formula Provisioner._companion_name uses."""
    ti = type_info or {}
    if not ti.get('companion_image'):
        return None
    suffix = ti.get('companion_suffix') or 'workspace'
    return f"{instance['container_name']}-{suffix}"


def image_drift(docker, container_name, image_ref) -> bool | None:
    """True/False when both image IDs are known; None = "cannot tell".

    A rebuilt mutable tag moves `image:tag` to a NEW image ID while the
    running container keeps the OLD one — that is the drift we detect.
    """
    if not container_name or not image_ref:
        return None
    running_id = docker.get_container_image_id(container_name)
    tagged_id = docker.get_image_id(image_ref)
    if not running_id or not tagged_id:
        return None
    return running_id != tagged_id


def version_drift(instance, type_info) -> bool:
    ti = type_info or {}
    return bool(instance.get('image_version') and ti.get('version')
                and instance['image_version'] != ti['version'])


def update_available(docker, instance, type_info) -> bool:
    """The reconciled per-instance update signal (see module docstring)."""
    if version_drift(instance, type_info):
        return True
    refs = catalog_image_refs(type_info)
    if refs.get('primary'):
        if image_drift(docker, instance.get('container_name'), refs['primary']):
            return True
    companion = companion_container_name(instance, type_info)
    if companion and refs.get('companion'):
        if image_drift(docker, companion, refs['companion']):
            return True
    return False


def update_available_for(provisioner, instance, type_info=None, use_cache=True) -> bool:
    """Read-path helper: ask the provisioner (image-aware when it can be);
    fall back to the version-string compare when it cannot (older callers,
    test doubles). Never raises."""
    meth = getattr(provisioner, 'update_available', None)
    if callable(meth):
        try:
            val = meth(instance, type_info, use_cache=use_cache)
            if isinstance(val, bool):
                return val
        except TypeError:
            try:
                val = meth(instance, type_info)
                if isinstance(val, bool):
                    return val
            except Exception:  # noqa: BLE001 — fall through to the version compare
                logger.warning("update_available probe failed for %s", instance.get('id'),
                               exc_info=True)
        except Exception:  # noqa: BLE001
            logger.warning("update_available probe failed for %s", instance.get('id'),
                           exc_info=True)
    return version_drift(instance, type_info)


def _db_setting(db, key):
    """Read an agent_settings value defensively — never raises, a missing or
    broken DB read just means "not set" (the env / derived fallback applies)."""
    if db is None:
        return None
    get = getattr(db, 'get_agent_setting', None)
    if not callable(get):
        return None
    try:
        raw = get(key)
    except Exception:  # noqa: BLE001
        logger.debug("agent_settings read failed for %s", key, exc_info=True)
        return None
    if raw is None or str(raw).strip() == '':
        return None
    return str(raw).strip()


def auto_update_enabled(db, config) -> bool:
    """Whether the idle auto-update pass runs. Precedence: the live
    agent_settings value (`auto_update_enabled`, set via the admin
    /api/config endpoint) > the AGENT_AUTO_UPDATE_ENABLED env > default on.
    Resolved per pass, so an operator opt-out applies without a restart."""
    raw = _db_setting(db, 'auto_update_enabled')
    if raw is not None:
        return raw.lower() in ('true', '1', 'yes', 'on')
    return bool(config.get('AGENT_AUTO_UPDATE_ENABLED', True))


def explicit_update_window(db, config) -> int | None:
    """The explicit idle-window override only (agent_settings
    `update_idle_window_seconds` > AGENT_UPDATE_IDLE_WINDOW_SECONDS env).
    None = not overridden — the window is derived per instance instead."""
    raw = _db_setting(db, 'update_idle_window_seconds')
    if raw is None:
        raw = config.get('AGENT_UPDATE_IDLE_WINDOW_SECONDS')
        if raw is not None:
            raw = str(raw).strip() or None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def update_window_seconds(config, inst, db=None) -> int | None:
    """The idle window for auto-updating `inst`.

    Explicit override (agent_settings DB value > AGENT_UPDATE_IDLE_WINDOW_SECONDS
    env) wins; otherwise the default is derived from existing config — the
    instance's own `idle_timeout`, else the tier fallback (lightweight/heavy)
    exactly like LifecycleManager._check_idle.
    Returns None when nothing can be derived (caller: do not auto-update).
    """
    explicit = explicit_update_window(db, config)
    if explicit is not None:
        return explicit
    timeout = inst.get('idle_timeout')
    if timeout is None:
        timeout = (config.get('AGENT_IDLE_TIMEOUT_LIGHTWEIGHT', 1800)
                   if inst.get('tier') == 'lightweight'
                   else config.get('AGENT_IDLE_TIMEOUT_HEAVY', 7200))
    try:
        return int(timeout)
    except (TypeError, ValueError):
        return None


def is_idle(inst, now=None, window=None) -> bool:
    """True when the instance has been idle at least `window` seconds.

    `last_accessed` is bumped on every portal/proxy access, so "idle" covers
    both "no running session" and "not attached to a terminal". An instance
    never accessed counts as idle. window=None means "cannot tell" → False
    (never auto-update on an underivable window). window<=0 → always idle.
    """
    if window is None:
        return False
    if window <= 0:
        return True
    last = inst.get('last_accessed')
    if not last:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= window


# ── `rzfz agents` CLI (runs inside the agent-manager container) ─────────────

_EXEC_SNIPPET = 'from app.services.agent_updates import cli_main; raise SystemExit(cli_main())'


def cli_main(app=None, cmd=None, instance_id=None, dry_run=False, out=print) -> int:
    """Entrypoint for `rzfz agents list|update` (via docker exec).

    `app=None` builds the real app (in-container) with background_jobs=False —
    a read-only init: NO LifecycleManager/scheduler, no schema migrations, no
    mutating startup backfills in this second process, so `list`, `--dry-run`
    and the upgrade-summary probe can never race a background recreate.
    Tests inject a fake app. Prints human lines plus a final `AGENTS_JSON:{...}`
    line the host shell parses without jq. Dry-run never calls upgrade().
    """
    cmd = (cmd or os.environ.get('RZFZ_AGENTS_CMD') or 'list').strip().lower()
    if instance_id is None:
        instance_id = (os.environ.get('RZFZ_AGENTS_INSTANCE') or '').strip()
    if not dry_run:
        dry_run = os.environ.get('RZFZ_AGENTS_DRY_RUN', '').strip().lower() in ('1', 'true', 'yes', 'on')
    if app is None:
        from app import create_app
        app = create_app(background_jobs=False)
    with app.app_context():
        prov = app.provisioner
        entries = []
        for row in app.db.get_all_instances():
            if row.get('state') == 'destroyed':
                continue
            ti = app.catalog.get_type(row['agent_type']) or {}
            ua = update_available_for(prov, row, ti, use_cache=False)
            entries.append({
                'id': str(row['id']),
                'container_name': row.get('container_name'),
                'agent_type': row.get('agent_type'),
                'user_slug': row.get('user_slug'),
                'state': row.get('state'),
                'image_version': row.get('image_version'),
                'update_available': ua,
                'update_target_version': ti.get('version') if ua else None,
            })
        behind_all = [e for e in entries if e['update_available']]
        behind = behind_all
        if instance_id:
            behind = [e for e in behind_all
                      if e['id'] == instance_id or e['container_name'] == instance_id]
        if cmd == 'list':
            for e in entries:
                if e['update_available']:
                    flag = f"update available (-> {e['update_target_version'] or 'current image'})"
                else:
                    flag = 'up to date'
                out(f"{e['container_name']}  [{e['agent_type']}]  {e['state']}  "
                    f"image_version={e['image_version'] or '?'}  {flag}")
            out(f"{len(entries)} instance(s), {len(behind_all)} with an update available.")
        elif cmd == 'update':
            if dry_run:
                out('DRY RUN — no changes made.')
            updated, failed = [], []
            for e in behind:
                if dry_run:
                    out(f"would update {e['container_name']} "
                        f"({e['image_version'] or '?'} -> "
                        f"{e['update_target_version'] or 'current image'})")
                    continue
                # the owner as the acting name — the same convention the
                # post-install re-key and the #2589 revive use
                who = e['user_slug'] or os.environ.get('RAZZFAZZ_ADMIN_USERNAME') or 'akadmin'
                try:
                    iid, msg = prov.upgrade(e['id'], who)
                    out(f"{'updated' if iid else 'FAILED'} {e['container_name']}: {msg}")
                    (updated if iid else failed).append(e['id'])
                except Exception as exc:  # noqa: BLE001 — one bad instance must not stop the rest
                    out(f"FAILED {e['container_name']}: {exc!r}")
                    failed.append(e['id'])
            if not behind:
                out('Nothing to update — every instance is on its current image.')
            out(f"{len(behind)} instance(s) behind"
                + (' (dry run)' if dry_run else f': {len(updated)} updated, {len(failed)} failed')
                + '.')
        else:
            out(f"unknown command {cmd!r} (expected: list | update)")
            return 2
        out('AGENTS_JSON:' + json.dumps({
            'cmd': cmd,
            'dry_run': bool(dry_run),
            'behind': len(behind_all),
            'would_update': [e['id'] for e in behind] if dry_run else [],
            'updated': updated if cmd == 'update' and not dry_run else [],
            'failed': failed if cmd == 'update' and not dry_run else [],
            'instances': entries,
        }))
    return 0
