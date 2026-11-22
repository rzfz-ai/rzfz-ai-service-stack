# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Is a remote worker still answering? Seconds, not the 90 s staleness window.

An instance row says what the worker last reported, and a worker that is down
reports nothing, so the row keeps its last word. The staleness window
(``LLM_MANAGER_WORKER_STALE_SECONDS``, 90 s) is the right bound for moving a
workload and the wrong one for saying what serves now.

A remote worker therefore sends a small beat every ``BEAT_SECONDS``, on its own
thread, carrying nothing else: no sampling and no command work can delay it.
Silence for ``silence_seconds()`` means the worker does not answer:

* ``/api/deployments`` stops counting its instances as ready, and
* the proxy addresses the model's other instances (app/affinity.py).

Both are decided when asked and write nothing. That is what makes a false alarm
cheap: no instance row moves, the reconciler still counts the replica until the
staleness window says otherwise, the router config is not rewritten (a rewrite
would swap LiteLLM), and the next beat undoes it. Requests in flight are never
touched.

Only workers reached through the relay are judged. Traffic to a co-located
worker goes straight to the engine container, so its silent agent says nothing
about the engine, and the box that would have to go down takes the manager with
it. A worker that never sent a beat (an older agent, the beat switched off, a
Mac) keeps the staleness window.

The table is process memory, like the chat pins: the manager runs one uvicorn
worker. After a manager restart it is empty and nobody is judged until they
beat again, so a restart cannot make a healthy fleet look silent.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

from app.config import _normalize_seconds_or_off

logger = logging.getLogger(__name__)

#: What the agent sends (node-agent runtime LIVENESS_BEAT_SECONDS).
BEAT_SECONDS = 2.0
#: Silence that counts as "does not answer". Three beats may go missing; the
#: fourth would be due at the bound itself.
DEFAULT_SILENCE_SECONDS = 8.0
#: Below two whole beats, one late beat would drop a healthy worker.
MIN_SILENCE_SECONDS = 2 * BEAT_SECONDS + 1.0

_clock: Callable[[], float] = time.monotonic
_last_beat: dict[str, float] = {}


def silence_seconds() -> float:
    """``LLM_MANAGER_WORKER_SILENCE_SECONDS``; 0 switches the judgement off."""
    value = _normalize_seconds_or_off(
        os.environ.get("LLM_MANAGER_WORKER_SILENCE_SECONDS"), DEFAULT_SILENCE_SECONDS)
    return max(value, MIN_SILENCE_SECONDS) if value > 0 else 0.0


def note_beat(worker_id) -> None:
    _last_beat[str(worker_id)] = _clock()


def forget(worker_id) -> None:
    _last_beat.pop(str(worker_id), None)


def silent_for(worker_id) -> Optional[float]:
    """Seconds this worker has been silent past its last beat, or None when it
    is answering or is not judged at all."""
    limit = silence_seconds()
    last = _last_beat.get(str(worker_id))
    if not limit or last is None:
        return None
    quiet = _clock() - last
    return quiet if quiet > limit else None


def silent_worker_ids() -> set[str]:
    limit = silence_seconds()
    if not limit:
        return set()
    now = _clock()
    return {wid for wid, last in list(_last_beat.items()) if now - last > limit}


class _NoBeatAccessLines(logging.Filter):
    """A beat every two seconds per worker would be most of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3
                    and str(args[2]).endswith("/beat"))


def quiet_access_log() -> None:
    logging.getLogger("uvicorn.access").addFilter(_NoBeatAccessLines())
