# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The addresses of the per-user cognee accounts that mcp-manager owns.

mcp-manager mints one cognee account per person for the personal memory MCP
(`cognee_login` in its provisioner). Those accounts are never members of an
Authentik group: their lifecycle (identity migration, quarantine, departed
people) belongs to mcp-manager. The role reconcile (razzfazz-config's
downstream_roles planner) uses `is_mcp_account` to leave them alone. Both
sides import this module, so the shapes cannot drift apart.

Two shapes, both under the box's MAIN_DOMAIN:

* `cognee-<sha256(sub)[:16]>@<domain>` — the account derived from the
  person's Authentik uid (the current form);
* `cognee-<user slug>@<domain>` — the 2026.09 slug-derived account, where the
  slug is `make_user_slug` (`<at most 14 of [a-z0-9-]>-<6 hex>`).
"""
from __future__ import annotations

import hashlib
import re

_LOCAL = re.compile(r"cognee-(?:[0-9a-f]{16}|[a-z0-9-]{0,14}-[0-9a-f]{6})")


def sub_account_email(sub: str, domain: str) -> str:
    """The sub-derived account of the person with Authentik uid `sub`."""
    tag = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:16]
    return f"cognee-{tag}@{domain}"


def slug_account_email(slug: str, domain: str) -> str:
    """The 2026.09 slug-derived account (legacy window and mapped accounts)."""
    return f"cognee-{slug}@{domain}"


def is_mcp_account(email: str, domain: str) -> bool:
    """True when `email` has exactly one of the two shapes above under
    `domain`. No domain, no match: the caller then treats every account as a
    person's (fail towards the reconcile's normal rules)."""
    if not isinstance(email, str) or not isinstance(domain, str) or not domain.strip():
        return False
    local, sep, dom = email.strip().lower().rpartition("@")
    if not sep or dom != domain.strip().lower():
        return False
    return _LOCAL.fullmatch(local) is not None
