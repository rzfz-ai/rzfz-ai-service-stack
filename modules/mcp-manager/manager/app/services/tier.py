# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""User-tier resolution for MCP integration governance (#36 follow-up).

The stack's agent tiers are Authentik groups mapped to a priority in
agent-manager's ``quota_tiers`` table (agent-manager/.../services/database.py):

    agent-basic  -> priority 0    ("regular")
    agent-power  -> priority 10   ("power")
    agent-admin  -> priority 99   ("admin")
    <super-admin Authentik groups> -> unlimited (priority 1000)

mcp-manager gates *which* integrations a user may see/provision by the SAME tier
model, but it must NOT reach across into agent-manager's DB (loose coupling, and
agent-manager may be off). So we mirror the group→priority map here as constants
and resolve the caller's tier purely from the Authentik ``groups`` header the
forward-auth already delivers. This is intentionally a SMALL, read-only mirror —
the authoritative quota enforcement still lives in agent-manager; here we only
decide MCP visibility/provisioning.

Server-side: the API filters the catalog and gates /provision by this tier, so a
user can never see or provision an integration above their tier even by crafting
requests — the UI hiding is cosmetic only.
"""
from __future__ import annotations

# Ordered low→high. `name` is the governance min_tier keyword; `groups` are the
# Authentik group ids that grant it; `priority` mirrors agent-manager's tiers.
TIER_BASIC = "regular"
TIER_POWER = "power"
TIER_ADMIN = "admin"

VALID_MIN_TIERS = (TIER_BASIC, TIER_POWER, TIER_ADMIN)

_TIER_PRIORITY = {TIER_BASIC: 0, TIER_POWER: 10, TIER_ADMIN: 99}

# Authentik group → tier. Mirrors agent-manager SEED_TIERS ids + the super-admin
# short-circuit groups (database._UNLIMITED_ADMIN_GROUPS).
_GROUP_TO_TIER = {
    "agent-basic": TIER_BASIC,
    "agent-power": TIER_POWER,
    "agent-admin": TIER_ADMIN,
}
_SUPER_ADMIN_GROUPS = {"razzfazz.ai Super Admins", "authentik Admins"}


def resolve_tier(groups) -> str:
    """Resolve the caller's MCP tier from their Authentik groups.

    Returns the HIGHEST-priority tier any of the user's groups grants. A user
    with no recognised group is treated as `regular` (the safe floor) so a
    freshly-created user still gets the base, publicly-available integrations.
    Super-admin groups map to `admin`.
    """
    groups = groups or []
    if any(g in _SUPER_ADMIN_GROUPS for g in groups):
        return TIER_ADMIN
    best = TIER_BASIC
    best_prio = _TIER_PRIORITY[TIER_BASIC]
    for g in groups:
        t = _GROUP_TO_TIER.get(g)
        if t and _TIER_PRIORITY[t] > best_prio:
            best, best_prio = t, _TIER_PRIORITY[t]
    return best


def tier_priority(tier: str) -> int:
    return _TIER_PRIORITY.get(tier, 0)


def tier_allows(user_tier: str, min_tier: str) -> bool:
    """True iff a user of `user_tier` may access something gated at `min_tier`."""
    return tier_priority(user_tier) >= tier_priority(min_tier or TIER_BASIC)
