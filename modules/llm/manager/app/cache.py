# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Valkey (redis-compatible) cache wrapper.

Thin, narrow surface used by the auth cache (M1), the rpm/tpm sliding
window (M2), and the per-key token counters (M4). ``redis`` is imported
LAZILY inside ``_client`` so importing this module never opens a socket —
the app must import cleanly off-box.

#2562 — a Valkey restart must not turn every ``/v1`` request into a 500.
Policy (controller ruling, 2026-09-29):

  * auth-cache reads degrade to a MISS, so ``authenticate()`` falls through
    to the Postgres lookup — authentication never fails open;
  * the rate/budget counters fail OPEN (``zcard → 0``, ``zrangebyscore → []``,
    ``incrby → amount``, writes are no-ops), so an outage does not take away
    all LLM use. Nothing is buffered: when Valkey returns, the counters
    resume from Valkey's own values — no replay, no double count;
  * the degraded state is visible: ONE warning per outage (not per request),
    one info line on recovery, ``/readyz`` names it and ``rzfz status`` WARNs.

While degraded, calls short-circuit to their fallback for
``_PROBE_INTERVAL_S`` before the next real attempt: a refused connection costs
~1.5 s per call with the retry below, and a request makes ~8 cache calls.
``ping()`` always probes for real (it is what readiness asks).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from app.config import get_settings

logger = logging.getLogger("llm_manager.cache")

#: #2562: the redis-py exception classes that mean "Valkey is not there right
#: now", matched by NAME so the unit tier needs no redis install. Anything
#: else (a wrong password, a WRONGTYPE, a bug) still raises.
_TRANSIENT = frozenset({"ConnectionError", "TimeoutError", "BusyLoadingError"})

#: While degraded, how long calls skip Valkey before the next real attempt.
_PROBE_INTERVAL_S = 5.0

#: Class names redis-py uses for a rejected password (NOAUTH / WRONGPASS).
_AUTH_ERRORS = frozenset({"AuthenticationError", "AuthenticationWrongNumberOfArgsError"})


class CacheUnavailable(RuntimeError):
    """A strict operation could not reach Valkey (down, or short-circuited)."""


def _is_auth_error(exc: BaseException) -> bool:
    if type(exc).__name__ in _AUTH_ERRORS:
        return True
    # Older servers/clients surface it as a plain ResponseError. The message
    # is only inspected here, never logged or returned.
    msg = str(exc).lstrip().upper()
    return msg.startswith("NOAUTH") or msg.startswith("WRONGPASS")


class ValkeyCache:
    """Process-wide lazy Valkey client wrapper.

    Only the operations the orchestrator actually needs are exposed, so a
    test fake has a small, obvious surface to implement.
    """

    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url or get_settings().valkey_url
        self._client = None
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._degraded = False
        self._retry_at = 0.0
        # While degraded, ONE thread probes per window; the rest take the
        # fallback instead of each stalling on a blackholed socket.
        self._probe_lock = threading.Lock()

    def _client_or_connect(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import redis  # lazy — keeps import-time side effects out
                    from redis.backoff import ExponentialBackoff
                    from redis.retry import Retry

                    self._client = redis.Redis.from_url(
                        self._url, decode_responses=True,
                        socket_connect_timeout=2, socket_timeout=2,
                        health_check_interval=15,
                        retry=Retry(ExponentialBackoff(cap=2.0, base=0.1), 3),
                    )
        return self._client

    # --- degraded-state bookkeeping (#2562) --------------------------------
    @property
    def degraded(self) -> bool:
        return self._degraded

    def _mark_down(self, exc: BaseException, op: str) -> None:
        with self._state_lock:
            self._retry_at = time.monotonic() + _PROBE_INTERVAL_S
            if self._degraded:
                return
            self._degraded = True
        # The exception CLASS only: a message can carry the connection URL,
        # and the URL can carry the Valkey password.
        logger.warning(
            "Valkey unavailable (%s during %s): API keys are checked against "
            "Postgres; rate and budget limits are NOT enforced until it returns",
            type(exc).__name__, op)

    def _mark_up(self) -> None:
        if not self._degraded:
            return
        with self._state_lock:
            if not self._degraded:
                return
            self._degraded = False
            self._retry_at = 0.0
        logger.info("Valkey reachable again: rate and budget limits are enforced")

    def _may_attempt(self) -> tuple[bool, bool]:
        """(attempt?, holds the probe lock?). Healthy: always attempt. Degraded:
        only after the probe interval, and only the one thread that wins the
        probe lock.

        The flags are read without the state lock on purpose: the worst race
        is a slow failure that started before a recovery re-marking the cache
        down for one more interval — bounded, and it never double-counts."""
        if not self._degraded:
            return True, False
        if time.monotonic() < self._retry_at:
            return False, False
        if not self._probe_lock.acquire(blocking=False):
            return False, False
        return True, True

    def _call(self, op: str, fallback, fn):
        attempt, probing = self._may_attempt()
        if not attempt:
            return fallback
        try:
            result = fn(self._client_or_connect())
        except Exception as exc:  # noqa: BLE001 — non-transient errors re-raise
            if type(exc).__name__ not in _TRANSIENT:
                raise
            self._mark_down(exc, op)
            return fallback
        finally:
            if probing:
                self._probe_lock.release()
        self._mark_up()
        return result

    def probe(self) -> str:
        """Readiness in one word, always a real round trip:
        ``ok`` · ``down`` (refused/timeout — the fail-open state) ·
        ``auth`` (password rejected — every /v1 request fails) ·
        ``misconfigured`` (any other error — every /v1 request fails)."""
        try:
            ok = bool(self._client_or_connect().ping())
        except Exception as exc:  # noqa: BLE001 — readiness never raises
            if type(exc).__name__ in _TRANSIENT:
                self._mark_down(exc, "ping")
                return "down"
            return "auth" if _is_auth_error(exc) else "misconfigured"
        if not ok:
            return "down"
        self._mark_up()
        return "ok"

    def ping(self) -> bool:
        """Can Valkey answer right now? Always a real round trip."""
        return self.probe() == "ok"

    # --- string ops (auth cache) -------------------------------------------
    def get(self, key: str) -> Optional[str]:
        # A miss, never a hit: auth then asks Postgres.
        return self._call("auth cache read", None, lambda c: c.get(key))

    def get_counter(self, key: str) -> Optional[str]:
        """``get`` for a budget counter — same fail-open miss, but the outage
        line names the budget, not the auth cache."""
        return self._call("budget counter read", None, lambda c: c.get(key))

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        self._call("auth cache write", None, lambda c: c.setex(key, ttl_seconds, value))

    def delete_or_raise(self, key: str) -> None:
        """STRICT delete — key invalidation (revoke/rotate/disable) must never
        fail open (#327): a swallowed delete leaves a revoked key's cached
        'active' verdict alive for the auth TTL.

        ALWAYS a real attempt, even inside the short-circuit window: after a
        single blip Valkey is usually back, and refusing there would 503
        revocations against a healthy cache. Revocations are rare, so one
        call that may stall on a dead socket is the right price. Raises
        ``CacheUnavailable`` on a transient failure and any other error as
        itself; the caller answers 503."""
        try:
            self._client_or_connect().delete(key)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ in _TRANSIENT:
                self._mark_down(exc, "cache invalidation")
                raise CacheUnavailable(
                    f"Valkey unavailable ({type(exc).__name__})") from exc
            raise
        self._mark_up()

    def delete(self, key: str) -> None:
        # Only invalidation deletes; it is strict. There is no swallowing delete.
        self.delete_or_raise(key)

    # --- sorted-set ops (sliding-window rate limit) ------------------------
    def zadd(self, key: str, mapping: dict) -> None:
        self._call("rate window write", None, lambda c: c.zadd(key, mapping))

    def zremrangebyscore(self, key: str, minv: float, maxv: float) -> int:
        return self._call("rate window trim", 0,
                          lambda c: c.zremrangebyscore(key, minv, maxv))

    def zcard(self, key: str) -> int:
        return self._call("rate window read", 0, lambda c: c.zcard(key))

    def zrangebyscore(self, key: str, minv: float, maxv: float) -> list:
        return self._call("rate window read", [],
                          lambda c: c.zrangebyscore(key, minv, maxv))

    def expire(self, key: str, ttl_seconds: int) -> None:
        self._call("counter expiry", None, lambda c: c.expire(key, ttl_seconds))

    # --- counter ops (token windows) ---------------------------------------
    def incrby(self, key: str, amount: int) -> int:
        return int(self._call("budget counter", int(amount),
                              lambda c: c.incrby(key, amount)))


_CACHE: Optional[ValkeyCache] = None
_CACHE_LOCK = threading.Lock()


def get_cache() -> ValkeyCache:
    global _CACHE
    if _CACHE is None:
        with _CACHE_LOCK:
            if _CACHE is None:
                _CACHE = ValkeyCache()
    return _CACHE
