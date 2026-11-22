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

from collections.abc import Set as AbstractSet

from razzfazz_common.rbac import CatalogueError
from razzfazz_common.rbac_guard import allows, groups_for

# Ordered low→high. `name` is the governance min_tier keyword; `groups` are the
# Authentik group ids that grant it; `priority` mirrors agent-manager's tiers.
TIER_BASIC = "regular"
TIER_POWER = "power"
TIER_ADMIN = "admin"

VALID_MIN_TIERS = (TIER_BASIC, TIER_POWER, TIER_ADMIN)

_TIER_PRIORITY = {TIER_BASIC: 0, TIER_POWER: 10, TIER_ADMIN: 99}

# Authentik group → tier. Mirrors agent-manager SEED_TIERS ids; the admin
# short-circuit comes from the role catalogue (mcp-manager/admin, #2473).
_GROUP_TO_TIER = {
    "agent-basic": TIER_BASIC,
    "agent-power": TIER_POWER,
    "agent-admin": TIER_ADMIN,
}


class _CatalogueAdminGroups(AbstractSet):
    """The groups the role catalogue lists under mcp-manager/admin, as a set-like
    that is resolved on every use. `g in _SUPER_ADMIN_GROUPS` keeps working for
    code that reads the name as a module global. Unusable catalogue: empty (fail
    closed, CRITICAL once per rbac_guard.FAILURE_TTL_S)."""

    @classmethod
    def _from_iterable(cls, it):
        return frozenset(it)

    @staticmethod
    def _groups() -> frozenset:
        try:
            return groups_for("mcp-manager", "admin")
        except CatalogueError:
            return frozenset()

    def __contains__(self, item) -> bool:
        return item in self._groups()

    def __iter__(self):
        return iter(self._groups())

    def __len__(self) -> int:
        return len(self._groups())

    def __repr__(self) -> str:
        return f"_SUPER_ADMIN_GROUPS({sorted(self._groups())})"


_SUPER_ADMIN_GROUPS = _CatalogueAdminGroups()


def resolve_tier(groups) -> str:
    """Resolve the caller's MCP tier from their Authentik groups.

    Returns the HIGHEST-priority tier any of the user's groups grants. A user
    with no recognised group is treated as `regular` (the safe floor) so a
    freshly-created user still gets the base, publicly-available integrations.
    Super-admin groups map to `admin`.
    """
    groups = groups or []
    if allows("mcp-manager", "admin", groups):
        return TIER_ADMIN
    best = TIER_BASIC
    best_prio = _TIER_PRIORITY[TIER_BASIC]
    for g in groups:
        t = _GROUP_TO_TIER.get(g)
        if t and _TIER_PRIORITY[t] > best_prio:
            best, best_prio = t, _TIER_PRIORITY[t]
    return best


#: Every Authentik group name that can change a tier (the directory keeps only
#: these of a user's groups, so its cached listing stays bounded).
TIER_GROUPS = frozenset(_GROUP_TO_TIER) | frozenset(_SUPER_ADMIN_GROUPS)


def tier_priority(tier: str) -> int:
    return _TIER_PRIORITY.get(tier, 0)


def tier_allows(user_tier: str, min_tier: str) -> bool:
    """True iff a user of `user_tier` may access something gated at `min_tier`."""
    return tier_priority(user_tier) >= tier_priority(min_tier or TIER_BASIC)


def is_mcp_admin(groups) -> bool:
    """mcp-manager's admin: platform-admin (`razzfazz.ai Super Admins`) or
    authentik-admin (`authentik Admins`), A1 D3 — NOT the `agent-admin` tier
    group, which only raises the integration tier."""
    return any(g in _SUPER_ADMIN_GROUPS for g in (groups or []))
