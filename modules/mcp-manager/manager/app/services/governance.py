# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""MCP integration governance (#36 follow-up).

Combines three inputs to decide what a given user may SEE and PROVISION:

  1. the read-only catalog (which integrations exist),
  2. the Config-UI-managed governance rows (mcp_integration_governance:
     `available` + `min_tier`), and
  3. the caller's resolved tier (from their Authentik groups).

Defaults are permissive-but-safe: an integration with NO governance row is
available at `min_tier=regular` (today's behaviour — everything is offerable),
so adding the table never silently hides an integration. An operator opts an
integration OUT (available=False) or UP a tier via Config-UI.

Enforcement is SERVER-SIDE: both the /api/integrations listing and the
/api/provision route call this module, so a user can never provision an
integration that is unavailable or above their tier even by crafting the request
directly — the UI hiding is cosmetic only.
"""
from __future__ import annotations

import logging
import os

from app.services.tier import TIER_BASIC, VALID_MIN_TIERS, resolve_tier, tier_allows

logger = logging.getLogger(__name__)


def _cognee_access_control_ok(mcp_id: str, catalog) -> bool:
    """FAIL-CLOSED probe: True only when the cognee backend enforces access
    control, so a per-user cognee entry can actually deliver the isolation it
    claims. Any error / missing config -> False (refuse to provision rather than
    silently leak across users). #36 security-finding fix."""
    try:
        from app.services import cognee_identity
        base = os.environ.get("COGNEE_BASE_URL", "http://cognee:8000")
        admin_email = os.environ.get("COGNEE_ADMIN_EMAIL", "")
        admin_pw = os.environ.get("COGNEE_ADMIN_PASSWORD", "")
        if not admin_email or not admin_pw:
            logger.warning("cognee admin creds unset — failing closed on %s", mcp_id)
            return False
        return cognee_identity.backend_access_control_on(base, admin_email, admin_pw)
    except Exception:
        logger.exception("cognee access-control probe failed — failing closed on %s", mcp_id)
        return False


def effective_governance(db, mcp_id: str) -> dict:
    """The effective {available, min_tier} for one integration (row or default)."""
    row = None
    try:
        row = db.get_governance(mcp_id)
    except Exception:
        row = None
    if not row:
        return {"available": True, "min_tier": TIER_BASIC}
    min_tier = row.get("min_tier") if row.get("min_tier") in VALID_MIN_TIERS else TIER_BASIC
    return {"available": bool(row.get("available", True)), "min_tier": min_tier}


def user_can_provision(db, mcp_id: str, groups, catalog=None) -> tuple[bool, str]:
    """Return (allowed, reason). reason is '' when allowed.

    #36 security-finding fix: an entry that claims per-user isolation via cognee
    (``requires_backend_access_control: true``) is FAIL-CLOSED — it is only
    provisionable when the cognee backend actually enforces access control. This
    guarantees we never provision an integration that claims an isolation it
    can't deliver (no masquerade)."""
    gov = effective_governance(db, mcp_id)
    if not gov["available"]:
        return False, "This integration is not available on this box."
    user_tier = resolve_tier(groups)
    if not tier_allows(user_tier, gov["min_tier"]):
        return False, (f"This integration requires the '{gov['min_tier']}' tier; "
                       f"your tier is '{user_tier}'.")

    entry = catalog.get(mcp_id) if catalog else None
    if entry and entry.get("requires_backend_access_control") and \
            entry.get("isolation_backend") == "cognee":
        if not _cognee_access_control_ok(mcp_id, catalog):
            return False, (
                "Personal Cognee memory requires the Cognee backend to run with "
                "access control enabled (per-user isolation). It is currently OFF, "
                "so this private integration is disabled to avoid leaking memories "
                "across users. Ask an admin to enable Cognee backend access control."
            )
    return True, ""


def user_can_write_company(catalog, mcp_id: str, groups) -> tuple[bool, str]:
    """WRITE gate for a shared Company Brain: READ is open (anyone entitled may
    provision + query), but WRITE (remember/cognify) is reserved for the
    integration's ``min_write_tier`` (default power). Enforced at the tool/scoping
    layer; the mcp-manager runs a regular user's company instance read-only."""
    entry = catalog.get(mcp_id) if catalog else None
    if not entry or entry.get("cognee_autoprovision") != "company":
        return True, ""  # not a company brain — no write gate here
    min_write = entry.get("min_write_tier", "power")
    user_tier = resolve_tier(groups)
    if not tier_allows(user_tier, min_write):
        return False, (f"Writing to the company brain requires the "
                       f"'{min_write}' tier; your tier is '{user_tier}'. You can "
                       f"still read it.")
    return True, ""


def visible_integrations(db, catalog, groups) -> list[dict]:
    """The catalog entries this user may see, each annotated with its effective
    governance (`available` filtered out, `min_tier` + `user_tier` attached)."""
    user_tier = resolve_tier(groups)
    out = []
    for m in catalog.all():
        gov = effective_governance(db, m["id"])
        if not gov["available"]:
            continue
        if not tier_allows(user_tier, gov["min_tier"]):
            continue
        entry = dict(m)
        entry["min_tier"] = gov["min_tier"]
        entry["user_tier"] = user_tier
        out.append(entry)
    return out
