# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Client for pulling a user's personal MCP-proxy wiring (#36 gap 2).

At agent launch/relaunch time the provisioner asks the mcp-manager service for
THAT user's running MCP proxies and injects them into the user's
hermes/moltis/opencode instances — the per-user analog of the stack-wide
registry that mcp_config.py bakes into the catalog.

PULL-AT-LAUNCH (vs push-on-connect): the agent-manager already owns the launch
path and the per-user identity (user_slug); pulling at launch keeps the two
services loosely coupled (no callback/inbound auth from mcp-manager into
agent-manager) and is naturally idempotent — every (re)launch re-reads the
current set of running proxies. Connecting a new MCP simply takes effect on the
agent's next relaunch.

Fail-safe: ANY error (mcp profile inactive so MCP_MANAGER_URL unset, service
down, malformed response) yields the empty wiring so agent provisioning is
never blocked by MCP.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

#: Seconds to wait for the wiring. Longer than one page of mcp-manager's
#: Authentik listing (gateway_identity.AuthentikDirectory.PAGE_TIMEOUT, 10 s),
#: which the owner check on a cold cache may need (#2086): a shorter wait
#: dropped the whole wiring, the user's own proxies included.
WIRING_TIMEOUT = 20

_EMPTY = {"moltis_env": {}, "hermes_specs": [], "opencode_block": {},
          # #36 follow-up: coding-agent shapes (Claude Code + Codex).
          "claude_mcp": {"mcpServers": {}}, "codex_mcp": {}, "proxies": []}


def fetch_user_wiring(user_slug: str, owner_id: str | None = None,
                      username: str | None = None) -> dict:
    """Return {moltis_env, hermes_specs, opencode_block, proxies} for the user.

    Calls GET {MCP_MANAGER_URL}/internal/agent-wiring/<user_slug>. Returns the
    empty wiring (never raises) when MCP_MANAGER_URL is unset or the call fails.

    #2086: `owner_id` (the instance owner's stored id) and, when known, the
    owner's `username` go along as `?sub=&username=`. mcp-manager wires the
    stack's gateway servers only when Authentik confirms that id is the
    owner's uid (ruling I5); without them the wiring is the 2026.09 one.
    """
    base = os.environ.get("MCP_MANAGER_URL", "").strip()
    if not base:
        # mcp profile not active / not wired — nothing to inject.
        return dict(_EMPTY)
    url = f"{base.rstrip('/')}/internal/agent-wiring/{user_slug}"
    # HIGH-1 (#61): present the shared internal token. mcp-manager's /internal/*
    # routes fail CLOSED without it (401), so an attacker who reaches the
    # endpoint without this secret learns nothing.
    headers = {}
    internal_token = os.environ.get("MCP_INTERNAL_TOKEN", "").strip()
    if internal_token:
        headers["X-MCP-Internal-Token"] = internal_token
    try:
        params = {}
        if owner_id:
            params["sub"] = str(owner_id)
            if username:
                params["username"] = str(username)
        kwargs = {"params": params} if params else {}
        resp = httpx.get(url, timeout=WIRING_TIMEOUT, headers=headers, **kwargs)
        if resp.status_code != 200:
            logger.warning("mcp-manager wiring %s -> %s", user_slug, resp.status_code)
            return dict(_EMPTY)
        data = resp.json() or {}
        # Normalize to the full shape so callers can index safely.
        claude_mcp = data.get("claude_mcp") or {}
        if not isinstance(claude_mcp.get("mcpServers"), dict):
            claude_mcp = {"mcpServers": {}}
        return {
            "moltis_env": data.get("moltis_env", {}) or {},
            "hermes_specs": data.get("hermes_specs", []) or [],
            "opencode_block": data.get("opencode_block", {}) or {},
            # #36 follow-up: coding-agent shapes.
            "claude_mcp": claude_mcp,
            "codex_mcp": data.get("codex_mcp", {}) or {},
            "proxies": data.get("proxies", []) or [],
        }
    except httpx.HTTPError as e:
        logger.warning("mcp-manager wiring fetch failed for %s: %s", user_slug, e)
        return dict(_EMPTY)
    except Exception:
        logger.exception("mcp-manager wiring fetch error for %s", user_slug)
        return dict(_EMPTY)


#: #2554: seconds to wait for a technical user's git credentials (one DB read
#: and one decrypt in mcp-manager; no directory lookup).
TECHNICAL_GIT_TIMEOUT = 5


def fetch_technical_git(name: str) -> dict:
    """#2554: {token, name, email} of a technical user's Gitea service user
    for an agent launched in technical mode, or {} when mcp-manager refuses
    (not live, not bound to gitea, no service user yet) or cannot be asked.
    The caller refuses the launch on {}; it never falls back to the owner."""
    base = os.environ.get("MCP_MANAGER_URL", "").strip()
    tok = os.environ.get("MCP_INTERNAL_TOKEN", "").strip()
    if not base or not tok:
        return {}
    from urllib.parse import quote
    url = f"{base.rstrip('/')}/internal/technical-users/{quote(name, safe='')}/git-credentials"
    try:
        r = httpx.get(url, params={"consumer": "agent"}, headers={"X-MCP-Internal-Token": tok},
                      timeout=TECHNICAL_GIT_TIMEOUT)
        if r.status_code != 200:
            logger.warning("mcp-manager git credentials for %s -> %s", name, r.status_code)
            return {}
        data = r.json()
    except Exception as e:  # noqa: BLE001 — refused, never raised
        logger.warning("mcp-manager git credentials for %s failed: %s", name, type(e).__name__)
        return {}
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data.get(k)
                                             for k in ("token", "name", "email")):
        return {}
    return {"token": data["token"], "name": data["name"], "email": data["email"]}


#: #2554: the binding a technical user needs for an agent to run as its Gitea
#: service user (mcp-manager's git-credentials route checks it again).
TECHNICAL_GIT_BINDING = "mcp-integration:gitea"


def usable_technical_users(users) -> list:
    """#2554: the live technical users bound to Gitea, from a listing."""
    return [u for u in users or []
            if isinstance(u, dict) and not u.get("revoked_at")
            and TECHNICAL_GIT_BINDING in (u.get("bindings") or [])]


def fetch_technical_users() -> list | None:
    """#2554: mcp-manager's mirror of the technical users (name, owner_sub,
    the display-only owner_username, bindings, revoked_at; never a token).
    None when mcp-manager is not configured, not reachable or answers
    anything but the listing: the caller refuses, it never guesses."""
    base = os.environ.get("MCP_MANAGER_URL", "").strip()
    tok = os.environ.get("MCP_INTERNAL_TOKEN", "").strip()
    if not base or not tok:
        return None
    try:
        r = httpx.get(f"{base.rstrip('/')}/internal/technical-users",
                      headers={"X-MCP-Internal-Token": tok}, timeout=TECHNICAL_GIT_TIMEOUT)
        if r.status_code != 200:
            logger.warning("mcp-manager technical users -> %s", r.status_code)
            return None
        data = r.json()
    except Exception as e:  # noqa: BLE001 — refused, never raised
        logger.warning("mcp-manager technical users failed: %s", type(e).__name__)
        return None
    users = data.get("technical_users") if isinstance(data, dict) else None
    if not isinstance(users, list):
        return None
    return [u for u in users if isinstance(u, dict) and isinstance(u.get("name"), str)]
