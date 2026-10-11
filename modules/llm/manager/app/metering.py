# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Usage parser + tokenizer backstop (M4) — TOKEN-ONLY.

Records, per request, three token counters — **input (prompt) · output
(completion) · cached** — into ``usage_events``. Two sources:

  1. the engine-reported ``usage`` object (OpenAI-compatible final chunk);
  2. a tokenizer BACKSTOP when the engine omits ``usage`` — a heuristic
     estimate of input+output (cached=0), with the row flagged
     ``estimated`` so it can be reconciled later (D6).

Everything is token counts. There is NO currency, NO cost — by design.

The default persist path writes to ``llm_manager_db`` via a fresh session;
tests inject a capturing ``persist`` so the whole function unit-tests
without a DB. Metering runs AFTER the response is streamed to the client
(see the proxy), so it never adds to client-visible latency.
"""
from __future__ import annotations

import math
import time
from typing import Callable, Optional


def parse_usage(usage: Optional[dict]) -> tuple[int, int, int]:
    """Return (prompt_tokens, completion_tokens, cached_tokens) from an
    OpenAI-style usage object. Cached tokens come from
    ``prompt_tokens_details.cached_tokens`` (OpenAI) or a top-level
    ``cached_tokens`` (some engines)."""
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cached = 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens") or 0)
    if not cached:
        cached = int(usage.get("cached_tokens") or 0)
    return prompt, completion, cached


def estimate_tokens(text: Optional[str]) -> int:
    """Offline-safe backstop estimate: ~4 chars/token (OpenAI rule of thumb),
    ceil, min 1 for non-empty text, 0 for empty.

    Deliberately dependency-free (no tiktoken/transformers) so it never
    reaches the network at import or runtime — a real air-gap requirement
    for this stack. A model-accurate tokenizer can be plugged in later
    behind this same signature.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def text_from_messages(messages) -> str:
    """Concatenate the text content of an OpenAI chat ``messages`` list
    (handles both plain-string and multimodal-part content)."""
    parts: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for seg in content:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(str(seg.get("text", "")))
    return "\n".join(parts)


def meter_usage(
    *,
    rec,
    model: str,
    usage: Optional[dict],
    request_id: Optional[str] = None,
    estimated: bool = False,
    cache=None,
    prompt_text: str = "",
    completion_text: str = "",
    persist: Optional[Callable[[dict], None]] = None,
    now: Optional[float] = None,
) -> dict:
    """Build + persist a TOKEN-ONLY usage_events row and update counters.

    Returns the row dict (handy for tests/logging).
    """
    now = time.time() if now is None else now
    usage = usage or {}
    if usage:
        prompt, completion, cached = parse_usage(usage)
        est = bool(estimated)
    else:
        # tokenizer backstop
        prompt = estimate_tokens(prompt_text)
        completion = estimate_tokens(completion_text)
        cached = 0
        est = True

    row = {
        "request_id": request_id,
        "api_key_id": rec.key_id,
        "cost_center_id": rec.cost_center_id,
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "estimated": est,
    }

    # business metrics (best-effort — never break the caller)
    try:
        from app import metrics

        metrics.add_tokens(
            rec.cost_center_id, rec.key_id, input=prompt, output=completion, cached=cached
        )
    except Exception:  # pragma: no cover - defensive
        pass

    # feed the tpm sliding window + the budget counter so subsequent requests
    # see this spend (rate limit + quota).
    if cache is not None:
        try:
            from app.enforce import record_budget_tokens, record_tokens

            record_tokens(cache, rec.key_id, prompt + completion, now)
            record_budget_tokens(
                cache, rec.key_id, prompt + completion,
                getattr(rec, "budget_duration_seconds", None),
            )
        except Exception:  # pragma: no cover - defensive
            pass

    (persist or _default_persist)(row)
    return row


def _default_persist(row: dict) -> None:
    import uuid

    from app.db import session_scope
    from app.models import UsageEvent

    with session_scope() as s:
        s.add(
            UsageEvent(
                request_id=row["request_id"],
                api_key_id=uuid.UUID(str(row["api_key_id"])),
                cost_center_id=uuid.UUID(str(row["cost_center_id"])),
                model=row["model"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                cached_tokens=row["cached_tokens"],
                estimated=row["estimated"],
            )
        )
