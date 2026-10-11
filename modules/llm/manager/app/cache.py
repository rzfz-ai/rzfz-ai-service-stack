# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Valkey (redis-compatible) cache wrapper.

Thin, narrow surface used by the auth cache (M1), the rpm/tpm sliding
window (M2), and the per-key token counters (M4). ``redis`` is imported
LAZILY inside ``_client`` so importing this module never opens a socket —
the app must import cleanly off-box.
"""
from __future__ import annotations

import threading
from typing import Optional

from app.config import get_settings


class ValkeyCache:
    """Process-wide lazy Valkey client wrapper.

    Only the operations the orchestrator actually needs are exposed, so a
    test fake has a small, obvious surface to implement.
    """

    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url or get_settings().valkey_url
        self._client = None
        self._lock = threading.Lock()

    def _client_or_connect(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import redis  # lazy — keeps import-time side effects out

                    self._client = redis.Redis.from_url(
                        self._url, decode_responses=True
                    )
        return self._client

    # --- string ops (auth cache) -------------------------------------------
    def get(self, key: str) -> Optional[str]:
        return self._client_or_connect().get(key)

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        self._client_or_connect().setex(key, ttl_seconds, value)

    def delete(self, key: str) -> None:
        self._client_or_connect().delete(key)

    # --- sorted-set ops (sliding-window rate limit) ------------------------
    def zadd(self, key: str, mapping: dict) -> None:
        self._client_or_connect().zadd(key, mapping)

    def zremrangebyscore(self, key: str, minv: float, maxv: float) -> int:
        return self._client_or_connect().zremrangebyscore(key, minv, maxv)

    def zcard(self, key: str) -> int:
        return self._client_or_connect().zcard(key)

    def zrangebyscore(self, key: str, minv: float, maxv: float) -> list:
        return self._client_or_connect().zrangebyscore(key, minv, maxv)

    def expire(self, key: str, ttl_seconds: int) -> None:
        self._client_or_connect().expire(key, ttl_seconds)

    # --- counter ops (token windows) ---------------------------------------
    def incrby(self, key: str, amount: int) -> int:
        return int(self._client_or_connect().incrby(key, amount))


_CACHE: Optional[ValkeyCache] = None
_CACHE_LOCK = threading.Lock()


def get_cache() -> ValkeyCache:
    global _CACHE
    if _CACHE is None:
        with _CACHE_LOCK:
            if _CACHE is None:
                _CACHE = ValkeyCache()
    return _CACHE
