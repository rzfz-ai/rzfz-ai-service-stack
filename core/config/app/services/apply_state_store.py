# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2794 — apply-action state that every worker (and a restarted portal) can read.

ApplyManager used to keep its actions in a dict of the current process, so
/api/apply/<id> answered 404 for an action started before a worker restart or
by another worker. The portal has no database; the state goes to one small JSON
file per action in a directory on the persistent audit volume, written
atomically (temp file + os.replace) and swept after a TTL.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

TTL_SECONDS = 24 * 3600
MAX_LINES = 5000


class StoredAction:
    """A read-only view of a persisted action, shaped like ApplyAction."""

    STALE_SECONDS = 600

    def __init__(self, data, store=None, clock=time.time):
        self._store = store
        self._clock = clock
        self._load(data)

    def _load(self, data):
        self.action_id = data["action_id"]
        self.description = data.get("description", "")
        self.lines = list(data.get("lines", ()))
        self.done = bool(data.get("done"))
        self.success = bool(data.get("success"))
        self.error = data.get("error")
        self.duration_ms = int(data.get("duration_ms") or 0)
        self.start_time = data.get("start_time", 0)
        self._status = dict(data.get("status") or {})
        # A run that stopped writing without finishing was cut off (the portal
        # restarted under it): say so instead of "running" forever.
        if not self.done and self._clock() - float(data.get("updated") or 0) > self.STALE_SECONDS:
            self.done, self.success = True, False
            self.error = "the apply was interrupted (the portal restarted before it finished)"

    def status(self):
        """The persisted job status (#2993). A stored action is never
        cancellable from here: its thread is not in this process."""
        st = dict(self._status)
        if self.done:
            st["state"] = st.get("state") if st.get("state") in ("succeeded", "failed", "cancelled") \
                else ("succeeded" if self.success else "failed")
            st["waiting_for"], st["cancel_note"] = [], ""
        st.setdefault("state", "running")
        st["cancellable"] = False
        return st

    def request_cancel(self):
        return False, ("This action is not running in this portal process (it was started "
                       "before a restart); it cannot be cancelled from here.")

    def get_lines_from(self, index):
        if not self.done and self._store is not None:
            fresh = self._store._read(self.action_id)
            if fresh:
                self._load(fresh)
        return list(self.lines[index:]), self.done, self.success, self.error, self.duration_ms


class ApplyStateStore:
    def __init__(self, directory, ttl=TTL_SECONDS, clock=time.time):
        self.dir = directory
        self.ttl = ttl
        self._clock = clock

    def _path(self, action_id):
        safe = "".join(c for c in str(action_id) if c.isalnum() or c in "-_")
        return os.path.join(self.dir, f"{safe}.json") if safe else None

    def save(self, action):
        """Persist the action's current state; never raises (best effort)."""
        path = self._path(action.action_id)
        if not path:
            return False
        try:
            lines, done, success, error, duration_ms = action.get_lines_from(0)
            data = {
                "action_id": action.action_id, "description": action.description,
                "lines": lines[-MAX_LINES:], "done": bool(done), "success": bool(success),
                "error": error, "duration_ms": duration_ms,
                "start_time": action.start_time, "updated": self._clock(),
            }
            # #2993: the job status (step, what it waits for, cancel state).
            status = getattr(action, "status", None)
            if callable(status):
                data["status"] = status()
            os.makedirs(self.dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _read(self, action_id):
        path = self._path(action_id)
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def load(self, action_id):
        data = self._read(action_id)
        try:
            return StoredAction(data, self, self._clock) if data else None
        except (KeyError, TypeError, ValueError):
            return None

    def cleanup(self):
        """Delete states older than the TTL. Returns how many."""
        n = 0
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0
        cutoff = self._clock() - self.ttl
        for name in names:
            if not name.endswith(".json"):
                continue
            p = os.path.join(self.dir, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
                    n += 1
            except OSError:
                pass
        return n
