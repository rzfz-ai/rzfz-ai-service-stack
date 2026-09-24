# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#337 — lifecycle for the manager's two highest-volume tables.

``usage_events`` (one row per proxied request; the chargeback source of
truth) and ``node_commands`` (one permanent row per node command, with
``tail_logs`` results holding captured log output) had NO retention at all —
nothing ever deleted from either, and ``/ui/usage``'s unfiltered scan got
slower every day the box ran.

Policy:

* ``usage_events``: pruned after ``LLM_MANAGER_USAGE_RETENTION_DAYS``
  (default 730 — two full years, comfortably covering the monthly
  entitlement rollup + any yearly chargeback question). ``0`` disables
  pruning entirely for operators who archive elsewhere.
* ``node_commands``: pruned after ``LLM_MANAGER_COMMANDS_RETENTION_DAYS``
  (default 30) — commands are operational breadcrumbs, not billing data,
  and the ``tail_logs`` results are the unbounded part.

Runs at startup and then daily from a daemon thread (registered in
``create_app``'s startup hook, so tests that build the app never spawn it).
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import threading

logger = logging.getLogger(__name__)

_PRUNE_INTERVAL_S = 24 * 3600


def _retention_days(env_key: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(env_key, str(default))))
    except ValueError:
        return default


def prune_once() -> dict:
    """One pruning pass; returns per-table deleted counts."""
    from app.db import session_scope
    from app.models import NodeCommand, UsageEvent

    usage_days = _retention_days("LLM_MANAGER_USAGE_RETENTION_DAYS", 730)
    cmd_days = _retention_days("LLM_MANAGER_COMMANDS_RETENTION_DAYS", 30)
    now = _dt.datetime.now(_dt.timezone.utc)
    deleted = {"usage_events": 0, "node_commands": 0}
    with session_scope() as s:
        if usage_days:
            deleted["usage_events"] = (
                s.query(UsageEvent)
                .filter(UsageEvent.ts < now - _dt.timedelta(days=usage_days))
                .delete(synchronize_session=False))
        if cmd_days:
            deleted["node_commands"] = (
                s.query(NodeCommand)
                .filter(NodeCommand.created_at < now - _dt.timedelta(days=cmd_days))
                .delete(synchronize_session=False))
    if any(deleted.values()):
        logger.info("retention prune (#337): %s", deleted)
    return deleted


def start_retention_loop() -> threading.Thread:
    """Daily pruning daemon; first pass runs immediately."""
    def _loop():
        while True:
            try:
                prune_once()
            except Exception:  # noqa: BLE001 — pruning must never kill the app
                logger.warning("retention prune failed (non-fatal)", exc_info=True)
            _stop.wait(_PRUNE_INTERVAL_S)
            if _stop.is_set():
                return

    _stop = threading.Event()
    t = threading.Thread(target=_loop, name="retention-prune", daemon=True)
    t._stop_event = _stop  # tests can stop the loop
    t.start()
    return t
