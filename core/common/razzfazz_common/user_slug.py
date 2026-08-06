# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Single source of truth for the per-user slug (#36 / #61 NEW-1).

Both agent-manager and mcp-manager derive a deterministic, DNS-/DB-safe slug
from an Authentik username. They MUST agree byte-for-byte: agent-manager calls
``/internal/agent-wiring/<slug>`` on mcp-manager, and mcp-manager stores its
per-user proxy instances under the SAME slug. If the two derivations diverge,
the wiring lookup returns empty, the per-proxy bearer never reaches the agent,
and the whole personal-MCP feature silently no-ops.

Historically each service had its own ``make_user_slug``:
  - agent-manager: ``slug[:20]`` (readable, but two long usernames sharing the
    first 20 chars collapse to the same slug — a cross-user isolation break)
  - mcp-manager (after #61 LOW-2): ``<=14>-<6hex>`` (collision-resistant)

They disagreed for ~every username. This module is the ONE implementation;
both services import it (mcp-manager's api blueprint re-exports it). The result
fits the ``user_slug VARCHAR(24)`` columns: ``base[:14]`` (<=14) + ``-`` (1) +
``sha256(username)[:6]`` (6) = at most 21 chars.
"""

from __future__ import annotations

import hashlib
import re


def _sanitized_base(username: str) -> str:
    """Lowercase, non-alphanumeric → hyphen, collapsed, stripped. Shared by
    both the current and legacy slug derivations so they never disagree on
    what the "readable part" of a username is — only on how it's truncated
    and whether a hash suffix is appended."""
    base = re.sub(r"[^a-z0-9]", "-", username.lower())
    return re.sub(r"-+", "-", base).strip("-")


def make_user_slug(username: str) -> str:
    """Deterministic per-user slug (fits ``user_slug VARCHAR(24)``).

    The readable part is truncated to 14 chars, then a 6-hex-char suffix derived
    from the FULL username is appended (``<=14>-<6hex>``, <=21 chars). The suffix
    is keyed on the stable identity string so DISTINCT usernames never collide,
    while the slug stays stable for a given user.
    """
    base = _sanitized_base(username)
    suffix = hashlib.sha256(username.encode("utf-8")).hexdigest()[:6]
    return f"{base[:14]}-{suffix}"


# ── Legacy (pre-#36/PR#61) slug — issue #192 ─────────────────────────────────
# Before the hashed <=14>-<6hex> scheme above, agent-manager derived a plain,
# un-hashed slug: same sanitization, truncated to 20 chars, no suffix. Agent
# instances provisioned before that change are still stored under THIS slug.
# A user whose Authentik session now resolves to the hashed make_user_slug
# would otherwise be denied ownership of (and orphaned from) their own
# pre-existing instances — the proxy's C1 ownership gate and the "My Agents"
# drawer's Open-vs-Launch decision both compare against instance['user_slug']
# verbatim. Kept as a named function (not inlined ad hoc at each call site)
# so every caller that needs to recognize a legacy-owned instance uses the
# IDENTICAL formula the old agent-manager actually used.
_LEGACY_SLUG_MAX_LEN = 20


def make_legacy_user_slug(username: str) -> str:
    """Pre-hash-suffix agent-manager slug (``base[:20]``, no suffix).

    Superseded by :func:`make_user_slug`; retained ONLY so ownership checks
    can recognize instances provisioned before the switch (#192).
    """
    return _sanitized_base(username)[:_LEGACY_SLUG_MAX_LEN]


def slug_candidates(username: str) -> tuple[str, ...]:
    """Ordered ownership-match candidates for ``username`` (#192).

    Returns ``(current,)`` or ``(current, legacy)`` when they differ (the
    common case — the legacy scheme never appends a hash suffix, so the two
    coincide only in the degenerate case of an empty sanitized base). Callers
    should treat ``candidates[0]`` (the current slug) as canonical — e.g. the
    slug to forward-migrate a legacy-matched instance to — and accept a match
    against ANY candidate as ownership.

    SECURITY: candidates MUST be derived from the REQUESTING user's own
    username only, never from the instance being checked, or cross-user
    isolation breaks (a different user must still be denied).
    """
    current = make_user_slug(username)
    legacy = make_legacy_user_slug(username)
    return (current,) if legacy == current else (current, legacy)
