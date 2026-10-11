# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Enforcement (M2) — model allow-list + rpm/tpm Valkey sliding windows.

Pure functions over an injected cache so they unit-test without redis. The
``active``/expiry gate lives in ``app.auth`` (KeyRecord live-check); this
module handles per-request policy:

  * model-allowed  → 403 when the requested model isn't in a non-empty
    ``allowed_models`` (empty list = unrestricted, documented default);
  * rpm            → 429 via a 60s sliding-window request log;
  * tpm            → 429 via a 60s sliding-window token log (tokens are fed
    in AFTER metering by ``record_tokens`` — see M4).
"""
from __future__ import annotations

import secrets

RPM_WINDOW_SECONDS = 60
TPM_WINDOW_SECONDS = 60


class EnforcementError(Exception):
    """Per-request policy violation. status_code 403 (model) or 429 (rate)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _rpm_key(key_id: str) -> str:
    return f"rl:rpm:{key_id}"


def _tpm_key(key_id: str) -> str:
    return f"rl:tpm:{key_id}"


def _unique_member(now: float) -> str:
    # Monotonic-ish + random suffix so concurrent same-instant events don't
    # collide in the sorted set.
    return f"{int(now * 1_000_000)}-{secrets.token_hex(4)}"


def check_model_allowed(allowed_models, model: str) -> None:
    if allowed_models and model not in allowed_models:
        raise EnforcementError(403, f"model '{model}' not permitted for this key")


def enforce_rpm(cache, key_id: str, rpm_limit, now: float) -> None:
    if not rpm_limit:
        return
    wkey = _rpm_key(key_id)
    cache.zremrangebyscore(wkey, 0, now - RPM_WINDOW_SECONDS)
    if cache.zcard(wkey) >= rpm_limit:
        raise EnforcementError(429, "rate limit exceeded (requests per minute)")
    cache.zadd(wkey, {_unique_member(now): now})
    cache.expire(wkey, RPM_WINDOW_SECONDS)


def current_tpm(cache, key_id: str, now: float) -> int:
    """Sum of tokens recorded in the trailing 60s window."""
    wkey = _tpm_key(key_id)
    cache.zremrangebyscore(wkey, 0, now - TPM_WINDOW_SECONDS)
    members = cache.zrangebyscore(wkey, now - TPM_WINDOW_SECONDS, now)
    total = 0
    for m in members:
        # member format: "<micros>-<rand>:<tokens>"
        try:
            total += int(str(m).rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
    return total


def enforce_tpm(cache, key_id: str, tpm_limit, now: float) -> None:
    if not tpm_limit:
        return
    if current_tpm(cache, key_id, now) >= tpm_limit:
        raise EnforcementError(429, "rate limit exceeded (tokens per minute)")


def record_tokens(cache, key_id: str, tokens: int, now: float) -> None:
    """Feed consumed tokens into the tpm sliding window (called post-metering)."""
    if tokens <= 0:
        return
    wkey = _tpm_key(key_id)
    cache.zadd(wkey, {f"{_unique_member(now)}:{tokens}": now})
    cache.expire(wkey, TPM_WINDOW_SECONDS)


# --- token BUDGET (P2-E1): a fixed-window quota, distinct from the 60s tpm ---
# rate limit. A key may spend up to ``max_budget_tokens`` per
# ``budget_duration`` (a Valkey counter with TTL = the window; None = all-time).
# TOKEN-ONLY — never money. Enforced BEFORE serving; fed AFTER metering.
def _budget_key(key_id: str) -> str:
    return f"budget:{key_id}"


def current_budget(cache, key_id: str) -> int:
    """Tokens spent in the current budget window (0 if none / unreadable)."""
    try:
        return int(cache.get(_budget_key(key_id)) or 0)
    except (TypeError, ValueError):
        return 0


def enforce_budget(cache, key_id: str, max_budget_tokens) -> None:
    """429 when the key has already spent its token budget this window."""
    if not max_budget_tokens:
        return
    if current_budget(cache, key_id) >= max_budget_tokens:
        raise EnforcementError(429, "token budget exceeded")


def record_budget_tokens(cache, key_id: str, tokens: int, window_seconds=None) -> None:
    """Add consumed tokens to the budget counter (called post-metering). Sets
    the window TTL on the first increment; None → all-time (no reset)."""
    if tokens <= 0:
        return
    bkey = _budget_key(key_id)
    new_total = cache.incrby(bkey, tokens)
    if window_seconds and new_total == tokens:
        cache.expire(bkey, int(window_seconds))


def enforce(rec, model: str, *, cache, now: float) -> None:
    """Run all per-request gates for an authenticated ``KeyRecord``.

    Order: model allow-list (403) → budget (429) → tpm (429) → rpm (429). The
    rpm gate records the request on success; token accounting for tpm + budget
    happens after the response via ``record_tokens`` / ``record_budget_tokens``.

    Box-wide concurrency (#19) is NOT part of this per-key chain — it is a
    single shared gate across every key/consumer, applied once around the
    whole request in the proxy (see ``acquire_concurrency_slot`` below).
    """
    check_model_allowed(rec.allowed_models, model)
    enforce_budget(cache, rec.key_id, rec.max_budget_tokens)
    enforce_tpm(cache, rec.key_id, rec.tpm_limit, now)
    enforce_rpm(cache, rec.key_id, rec.rpm_limit, now)


# --- box-wide in-flight concurrency limiter (#19, "option C") ---------------
# Per-app throttling (Dify's max_active_requests, #30) only protects Dify's own
# queue — Open WebUI, the agents, Cognee, and everything else hitting this
# manager can still pile unbounded concurrent completions onto the shared GPU.
# Option C, per the issue: a single box-wide in-flight cap in front of
# `llm.<domain>`/gpustack so ALL consumers are throttled together. On admission
# it REJECTS with 429 once the cap is hit — it does NOT queue. Queueing would
# just relocate the wedge from "GPU overloaded" to "manager holding N blocked
# connections open", which is exactly what #325 already fixed on the upstream
# leg (see the httpx-timeout comment in proxy.py).
#
# The counter is a plain int wrapper, not a semaphore/lock: the manager runs a
# single uvicorn worker on a single asyncio event loop (#359), and
# acquire/release never `await` between reading and mutating ``.value`` — so
# nothing else on the loop can interleave between the check and the increment.
class InFlightCounter:
    """Process-wide in-flight request counter (DI seam — tests inject their own
    instance; production shares one across all requests via app.state)."""

    def __init__(self) -> None:
        self.value = 0


def acquire_concurrency_slot(counter: "InFlightCounter", limit) -> None:
    """429 when the box is already serving ``limit`` concurrent completions.

    ``limit`` falsy (0/None) disables the gate entirely (unbounded, the
    pre-#19 behaviour). On success the slot is already counted — the caller
    MUST call ``release_concurrency_slot`` exactly once for every successful
    ``acquire_concurrency_slot``, including on error paths (try/finally), or
    the slot leaks and the box's effective cap silently shrinks over time
    until nothing is admitted.
    """
    if not limit:
        return
    if counter.value >= limit:
        raise EnforcementError(429, "box-wide LLM concurrency limit exceeded")
    counter.value += 1


def release_concurrency_slot(counter: "InFlightCounter", limit) -> None:
    """Give back a slot acquired by ``acquire_concurrency_slot``.

    Symmetric no-op when the gate is disabled. Defensive against going
    negative (a stray/double release must not hand out extra capacity).
    """
    if not limit:
        return
    if counter.value > 0:
        counter.value -= 1
