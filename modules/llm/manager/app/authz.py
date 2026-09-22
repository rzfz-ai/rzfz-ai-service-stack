# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Admin authorization for the management API (/api/*, /ui/*).

The key/cost-center/usage routes MINT and manage API keys — the highest-
value target in the design. They MUST be admin-gated. We trust the Authentik
forward-auth identity the way the rest of the stack does: Caddy performs the
OIDC check and injects ``X-Authentik-Username`` / ``X-Authentik-Groups``
(pipe-delimited) upstream, stripping any client-supplied copies. This
``require_admin`` dependency FAILS CLOSED — a missing identity header or a
non-admin group → 403.

The forward-auth headers alone are NOT sufficient: the manager shares the
``default`` docker network with every other service, so a co-resident or
compromised peer (dify-api, agent-manager, a crawler, or an SSRF foothold)
could dial the manager directly with FORGED ``X-Authentik-Groups`` and mint
admin API keys — bypassing Authentik entirely (the fail-closed check only
covers an *absent* identity, never a *forged present* one). So every admin
route additionally requires a "came through Caddy" SOURCE-IP anchor
(``from_caddy`` below): the request's immediate peer must be Caddy's
docker-resolved IP. This is the manager-side twin of agent-manager's
``_proxy_proof_ok`` (C1 / PR #84). A header-injected proof was rejected there
because Caddy's per-instance ``forward_auth`` does not reliably propagate a
route-injected request header; a sandbox with ``cap_drop ALL`` (no NET_RAW)
cannot spoof a source IP.

The ``/v1/*`` OpenAI hot path is NOT gated here (it uses rzfz-sk key auth in
the proxy); ``/metrics`` is an internal Prometheus scrape.

Request is imported at MODULE scope on purpose: with `from __future__ import
annotations`, FastAPI resolves the dependency's ``request: Request``
annotation against module globals.
"""
from __future__ import annotations

import enum
import os
import socket
import time

from fastapi import HTTPException, Request

from app.config import get_settings

_CADDY_HOST = os.environ.get("CADDY_HOST", "caddy")
_caddy_ip_cache: dict = {"t": 0.0, "ips": frozenset()}


def _caddy_ips() -> frozenset:
    """Resolve ``CADDY_HOST`` → the set of its IPs, cached ~30s. Returns an
    empty set on resolution failure so the caller fails CLOSED."""
    now = time.monotonic()
    cached = _caddy_ip_cache["ips"]
    if cached and (now - _caddy_ip_cache["t"] < 30.0):
        return cached
    try:
        infos = socket.getaddrinfo(_CADDY_HOST, None)
        ips = frozenset(info[4][0] for info in infos)
    except OSError:
        ips = frozenset()
    if ips:
        _caddy_ip_cache["t"] = now
        _caddy_ip_cache["ips"] = ips
    return ips


def from_caddy(request: Request) -> bool:
    """True iff the request's immediate peer is Caddy (the sole ingress).

    Fails CLOSED when ``CADDY_HOST`` doesn't resolve. A sandbox / co-resident
    container cannot spoof Caddy's source IP (cap_drop ALL → no NET_RAW), so a
    direct in-network hit with forged forward-auth headers is rejected here.
    """
    remote = (request.client.host if request.client else "") or ""
    remote = remote.strip()
    if not remote:
        return False
    ips = _caddy_ips()
    if not ips:
        return False
    return remote in ips


def parse_groups(raw: str | None) -> list[str]:
    """Authentik forward-auth sends groups pipe-delimited."""
    if not raw:
        return []
    return [g.strip() for g in raw.split("|") if g.strip()]


def is_admin(username: str | None, groups: list[str], admin_groups) -> bool:
    if not username:
        return False
    return any(g in admin_groups for g in groups)


def require_admin(request: Request):
    """FastAPI dependency: require an Authentik admin identity that actually
    transited Caddy. Fails closed."""
    settings = get_settings()
    if not from_caddy(request):
        # Direct in-network hit — the forward-auth headers cannot be trusted
        # (they may be forged by a co-resident peer). Never mint/expose keys.
        raise HTTPException(
            status_code=403,
            detail="request did not originate from the ingress proxy",
        )
    username = request.headers.get(settings.admin_user_header)
    groups = parse_groups(request.headers.get(settings.admin_groups_header))
    if not is_admin(username, groups, settings.admin_groups):
        raise HTTPException(
            status_code=403,
            detail="admin authorization required (Authentik admin group)",
        )
    return {"username": username, "groups": groups}


# --- #314 three-tier RBAC ----------------------------------------------------
#
# `require_admin`/`is_admin` above are UNCHANGED — every existing management
# route (workers, settings, catalog, inventory, commands, hf, registry,
# runner_upgrade, enroll, entitlement, playground, router_config) keeps its
# exact current behaviour. What follows is a NEW, additive primitive: a
# three-tier decision (super-admin > admin > user > none) driven by
# Authentik groups, for routes that opt into finer-grained gating (starting
# with API-key self-service / owner attribution in `app/api/keys.py`).
#
# FAIL CLOSED is the whole point of a role hierarchy: a caller whose groups
# don't map to ANY known tier gets the lowest possible privilege (NONE), not
# the nearest one below what they typed. A security default that leaks under
# an unrecognised value is worse than one that over-denies — flagged
# explicitly for the release-cycle security review (issue #314).
class Role(str, enum.Enum):
    NONE = "none"
    USER = "user"
    ADMIN = "admin"
    SUPERADMIN = "superadmin"


# Total order used by `require_role`'s minimum-privilege check.
_ROLE_RANK: dict[Role, int] = {
    Role.NONE: 0,
    Role.USER: 1,
    Role.ADMIN: 2,
    Role.SUPERADMIN: 3,
}


def resolve_role(username: str | None, groups: list[str], settings) -> Role:
    """Map an Authentik identity onto the highest tier its groups satisfy.

    FAILS CLOSED: no username, or groups that match none of the three
    configured group-lists, resolve to ``Role.NONE`` — never a guess, never
    the nearest tier below an unrecognised group name.
    """
    if not username:
        return Role.NONE
    # `settings.admin_groups` is DELIBERATELY the super-admin tier here (see
    # the config.py comment) — this reuses the box's existing "admin" concept
    # rather than inventing a fourth group list.
    if any(g in settings.admin_groups for g in groups):
        return Role.SUPERADMIN
    if any(g in settings.llm_admin_groups for g in groups):
        return Role.ADMIN
    if any(g in settings.llm_user_groups for g in groups):
        return Role.USER
    return Role.NONE


def require_role(min_role: Role):
    """FastAPI dependency FACTORY: require at least ``min_role``.

    Same Caddy source-IP anchor as `require_admin` (a forged
    X-Authentik-Groups from a co-resident peer must not grant ANY tier, not
    just admin). Returns the resolved identity so a route can record who
    acted (e.g. API-key owner attribution).
    """

    def _dependency(request: Request):
        settings = get_settings()
        if not from_caddy(request):
            raise HTTPException(
                status_code=403,
                detail="request did not originate from the ingress proxy",
            )
        username = request.headers.get(settings.admin_user_header)
        groups = parse_groups(request.headers.get(settings.admin_groups_header))
        role = resolve_role(username, groups, settings)
        # Strict `<`, not `!=`/`not in {...}`: a role that fails to resolve
        # to anything recognised (Role.NONE, rank 0) is always below every
        # real minimum this dependency is ever configured with — fail closed
        # rather than accidentally passing an unranked value through.
        if _ROLE_RANK[role] < _ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"insufficient role (need >= {min_role.value}, have "
                    f"{role.value})"
                ),
            )
        return {"username": username, "groups": groups, "role": role}

    # Stamp the minimum tier on the returned dependency so the per-route
    # capability-matrix wiring is INTROSPECTABLE without standing up a server
    # (tests/unit/llm-manager/test_314_rbac.py walks `route.dependant` and reads
    # this): a mutation that loosens or over-tightens a single route's gate is
    # then caught by a pure, offline assertion rather than only by an api-tier
    # test that skips wherever docker is unavailable.
    _dependency.min_role = min_role
    return _dependency


def require_authenticated(request: Request):
    """FastAPI dependency: require an identity that transited Caddy, WITHOUT
    demanding any particular tier. Returns ``{username, groups, role}`` — the
    role may be ``Role.NONE`` for a signed-in but un-entitled user.

    This exists for ``/api/me`` (#314). The binary ``require_admin`` 403'd every
    non-admin, so a signed-in USER got a blank, silently-failing console instead
    of their identity + an explicit "you don't have access" answer. Still FAILS
    CLOSED on an absent identity or a request that did not come through Caddy —
    /api/me answers the AUTHENTICATED, never the anonymous, never a forger.
    """
    settings = get_settings()
    if not from_caddy(request):
        raise HTTPException(
            status_code=403,
            detail="request did not originate from the ingress proxy",
        )
    username = request.headers.get(settings.admin_user_header)
    if not username:
        raise HTTPException(status_code=403, detail="authentication required")
    groups = parse_groups(request.headers.get(settings.admin_groups_header))
    role = resolve_role(username, groups, settings)
    return {"username": username, "groups": groups, "role": role}


def capabilities_for(role: Role) -> dict:
    """What an identity of ``role`` may do, as the issue's capability matrix.

    Consumed by ``/api/me`` so the SPA gates its nav/actions off ONE
    server-authoritative answer instead of re-deriving the matrix client-side
    (where it could drift from the actual route gates). Monotonic in the tier
    ranking — a higher tier is a strict superset of a lower one.
    """
    rank = _ROLE_RANK[role]
    is_user = rank >= _ROLE_RANK[Role.USER]
    is_admin = rank >= _ROLE_RANK[Role.ADMIN]
    is_super = rank >= _ROLE_RANK[Role.SUPERADMIN]
    return {
        # admin tier — deploy/configure models, issue keys for anyone, all usage
        "deploy_models": is_admin,
        "issue_keys_for_anyone": is_admin,
        "view_all_usage": is_admin,
        # user tier — playground, self-issue own key, own usage
        "playground": is_user,
        "self_issue_key": is_user,
        "view_own_usage": is_user,
        # super-admin tier — workers/federation, global settings, grant policy
        "manage_workers": is_super,
        "global_settings": is_super,
        "grant_self_issue": is_super,
    }
