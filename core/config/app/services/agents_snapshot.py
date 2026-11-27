# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The per-user agents a disable of the agents module stopped (#3077).

ApplyManager writes the names here before it stops the agents and starts
exactly these on the next enable, so the agent manager never finds them
stopped (see ApplyManager._stop_agent_containers for why that matters). The
file lives in the Portal's persistent apply-state directory. Kept out of
apply_manager.py, whose only file writes are the guarded `.env` writers.
"""
from __future__ import annotations

import json
import os
import time


def read(path: str | None) -> list:
    """The remembered container names; [] when there is no snapshot or it
    cannot be read."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        return [n for n in data.get('containers', []) if isinstance(n, str) and n]
    except (OSError, ValueError, AttributeError):
        return []


def write(path: str, names) -> list:
    """Remember ``names`` merged with what is already remembered; atomic.
    Returns the merged list. Raises OSError when it cannot be written."""
    keep = sorted(set(read(path)) | set(names))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'containers': keep, 'stopped_at': int(time.time())}, fh)
    os.replace(tmp, path)
    return keep


def clear(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
