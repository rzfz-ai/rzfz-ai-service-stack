# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Source-IP anchor for the agent-manager's header-authenticated surfaces (#397).

Every blueprint in this service authorises off `X-Authentik-*` request headers.
Those headers are trustworthy ONLY because Caddy — the sole ingress, running
Authentik `forward_auth` — sets them. A request that did not transit Caddy
carries whatever headers its sender typed.

#390 anchored `api_bp`. Its siblings (`admin_bp`, `dashboard_bp`, and the
`terminal` WebSocket) were not covered, and chained they are worse: forged admin
groups enumerate every instance via `db.get_all_instances()`, and the terminal's
ownership check compares against a slug DERIVED FROM the forged username — so
claiming the victim's name earns a `docker exec` root shell inside their agent
container. The provisioned sandboxes sit on that same `_default` network (#256),
so the attacker is any agent.

This module is the one place that answers "did this request come from the
ingress?" for those three surfaces. `api_bp` keeps its own copy because it also
carries a narrow start-portal allow-list; consolidating the two is a follow-up.
"""
from __future__ import annotations

import logging

from razzfazz_common.proxy_anchor import DEFAULT_INGRESS_HOST
from razzfazz_common import proxy_anchor

logger = logging.getLogger(__name__)

# Test/local-dev escape hatch — the SAME variable `api_bp` uses, so one setting
# governs the whole service. Never set in a container.
TRUST_ENV = "RZFZ_AGENT_MANAGER_TRUST_ALL_PROXIES"

INGRESS_HOSTS = (DEFAULT_INGRESS_HOST,)


def ingress_ok() -> bool:
    """True iff the request's TCP peer is the ingress (Caddy).

    Fails CLOSED: when the ingress name cannot be resolved this denies rather
    than admits. Never consults `X-Forwarded-For` — a forged header must not be
    able to move the anchor.

    Resolved through the module (not a `from`-import) so tests can substitute
    `proxy_anchor.resolve_peer_ips`.
    """
    return proxy_anchor.from_trusted_proxy(INGRESS_HOSTS, trust_env=TRUST_ENV)


def log_refusal(what: str, method: str, path: str, remote: str | None) -> None:
    """One-line, greppable record of an anchor refusal."""
    logger.warning(
        "Refusing %s %s %s from %s — not the ingress (#397). Forged "
        "X-Authentik-* headers cannot authorise it.",
        what, method, path, remote or "<no peer>",
    )
