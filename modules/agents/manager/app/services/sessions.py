# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3187 — in-process registry of live user attachments (portal PTY + proxied WS).

The agent-manager runs as ONE gunicorn worker (Dockerfile: `-w 1`), so a
module-level registry is authoritative for the whole process: an instance with
a registered attachment has a user actively attached and must never be
recreated under them by the idle auto-update pass (busy users get the badge
only).

Race safety: attachments are registered while holding the provisioner's
per-instance lock AND the shared cross-process advisory lock
(`Provisioner.activity_attach` is called under `_instance_lock` +
`_shared_instance_lock`), and `Provisioner._upgrade` re-checks under those
SAME locks immediately before it stops/removes containers. An attach that
begins between the auto-update snapshot and the recreate therefore either
registers before the check (the pass skips / the upgrade refuses) or blocks
until the recreate finishes and then attaches to the NEW container — a session
is never torn down mid-use. A process that cannot see this registry (the
`docker exec` CLI update) still sees the attachment: while attached,
`last_accessed` stays fresh in the shared DB, and
`Provisioner.session_active` treats a fresh row as attached.

#3187 review round 4: frame-driven `beat()` alone cannot keep a QUIET
attached PTY visible — the portal terminal only beats on inbound frames and
the container-output reader never beats, so >90 s without inbound input
would age the row out while the user is still attached. The serving
process therefore runs a SERVER-OWNED periodic renewal
(`heartbeat()`, scheduled by the LifecycleManager, serving process only)
that bumps `last_accessed` for every registered attachment for the FULL
attachment lifetime, independent of browser input/output. On
disconnect/crash the registry entry is gone, renewal stops, and the row
ages out within `Provisioner.SESSION_FRESHNESS_SECONDS` — bounded cleanup
is preserved.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

#: minimum seconds between two `last_accessed` DB writes for one attachment
DB_BUMP_INTERVAL = 30.0


class ActivityRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}   # key -> refcount (several tabs may attach)
        self._owners = {}     # key -> instance_id as passed to attach()
        self._db_bumps = {}   # key -> monotonic ts of the last DB write

    def attach(self, key: str, instance_id=None) -> None:
        with self._lock:
            self._sessions[key] = self._sessions.get(key, 0) + 1
            if instance_id is not None:
                self._owners.setdefault(key, instance_id)

    def detach(self, key: str) -> None:
        with self._lock:
            left = self._sessions.get(key, 0) - 1
            if left > 0:
                self._sessions[key] = left
            else:
                self._sessions.pop(key, None)
                self._owners.pop(key, None)
            self._db_bumps.pop(key, None)

    def is_active(self, key: str) -> bool:
        with self._lock:
            return self._sessions.get(key, 0) > 0

    def beat(self, key: str, db=None, instance_id=None) -> None:
        """Record activity; bump `last_accessed` in the DB at most every
        DB_BUMP_INTERVAL seconds while the attachment lives."""
        if db is None or instance_id is None:
            return
        now = time.monotonic()
        with self._lock:
            last = self._db_bumps.get(key, 0.0)
            if now - last < DB_BUMP_INTERVAL:
                return
            self._db_bumps[key] = now
        try:
            db.update_last_accessed(instance_id)
        except Exception:  # noqa: BLE001 — idle bookkeeping must not break a session
            logger.debug("sessions: update_last_accessed failed", exc_info=True)

    def heartbeat(self, db) -> int:
        """Server-owned periodic renewal: bump `last_accessed` in the shared
        DB for EVERY registered attachment — independent of inbound frames or
        container output, so a quiet attached PTY stays visible to other
        processes (the docker-exec CLI) for the full attachment lifetime.
        Runs only in the serving process (scheduled by the LifecycleManager);
        once the last tab detaches (or the process crashes) nothing renews
        and the row ages out within SESSION_FRESHNESS_SECONDS. Returns the
        number of renewed attachments."""
        if db is None:
            return 0
        with self._lock:
            items = [(k, self._owners.get(k)) for k in list(self._sessions)]
        now = time.monotonic()
        renewed = 0
        for key, owner in items:
            if owner is None:
                continue
            try:
                db.update_last_accessed(owner)
                renewed += 1
            except Exception:  # noqa: BLE001 — idle bookkeeping must not break a session
                logger.debug("sessions: heartbeat update_last_accessed failed",
                             exc_info=True)
            with self._lock:
                self._db_bumps[key] = now
        return renewed

    def reset(self) -> None:
        """Tests only."""
        with self._lock:
            self._sessions.clear()
            self._owners.clear()
            self._db_bumps.clear()


_REGISTRY = ActivityRegistry()


def activity_key(instance_id) -> str:
    """The registry key — the SAME normalisation `_instance_lock` uses, so the
    registry and the per-instance lock always address one instance."""
    try:
        return str(uuid.UUID(str(instance_id)))
    except ValueError:
        return str(instance_id)


def attach(instance_id) -> str:
    key = activity_key(instance_id)
    _REGISTRY.attach(key, instance_id)
    return key


def detach(key: str) -> None:
    _REGISTRY.detach(key)


def is_active(key: str) -> bool:
    return _REGISTRY.is_active(key)


def beat(key: str, db=None, instance_id=None) -> None:
    _REGISTRY.beat(key, db=db, instance_id=instance_id)


def heartbeat(db=None) -> int:
    return _REGISTRY.heartbeat(db)


def reset() -> None:
    _REGISTRY.reset()
