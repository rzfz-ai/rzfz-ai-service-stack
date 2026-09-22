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

_EMPTY = {"moltis_env": {}, "hermes_specs": [], "opencode_block": {},
          # #36 follow-up: coding-agent shapes (Claude Code + Codex).
          "claude_mcp": {"mcpServers": {}}, "codex_mcp": {}, "proxies": []}


def fetch_user_wiring(user_slug: str) -> dict:
    """Return {moltis_env, hermes_specs, opencode_block, proxies} for the user.

    Calls GET {MCP_MANAGER_URL}/internal/agent-wiring/<user_slug>. Returns the
    empty wiring (never raises) when MCP_MANAGER_URL is unset or the call fails.
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
        resp = httpx.get(url, timeout=5, headers=headers)
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
