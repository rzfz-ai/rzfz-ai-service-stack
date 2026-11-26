# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Read-only helper exposing the stack MCP registry to the agent catalog.

Mirrors llm_config.py: locate core/mcp/mcp-servers.yaml relative to the stack
root (same resolution llm_config uses for standard-models.yaml) and translate
registry entries into each agent's native MCP config shape. Per-user agents
(Moltis, Hermes) call these at catalog build time.

Path resolution
---------------
The YAML is read from ``/mcp-servers.yaml`` inside the container by default
(the path is overridable via ``MCP_SERVERS_YAML``). The agent-manager compose
entry bind-mounts ``core/mcp/mcp-servers.yaml`` to that target so operator
edits propagate on the next ``docker restart agent-manager`` — no image rebuild
required.

This matches how llm_config.py locates standard-models.yaml via the
``STANDARD_MODELS_YAML`` env var with a ``/standard-models.yaml`` container
default.

Resilience
----------
If the YAML is missing or malformed the helpers return empty structures so
agents simply get no MCP configuration — the manager keeps booting normally.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Mirrors llm_config.py's _YAML_PATH resolution: env var override with an
# in-container default. The compose bind-mount delivers the file there.
REGISTRY_PATH = Path(os.environ.get("MCP_SERVERS_YAML", "/mcp-servers.yaml"))


def _load() -> dict:
    """Read and return the registry YAML. Returns a safe empty structure on failure."""
    p = Path(REGISTRY_PATH)
    if not p.exists():
        logger.warning(
            "mcp-servers.yaml not at %s — no MCP servers will be configured; "
            "is the bind-mount missing from agents/compose.yml?",
            p,
        )
        return {"version": 1, "servers": []}

    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed in agent-manager — MCP registry unavailable.")
        return {"version": 1, "servers": []}

    try:
        return yaml.safe_load(p.read_text()) or {"version": 1, "servers": []}
    except Exception:
        logger.exception("mcp-servers.yaml unreadable — no MCP servers will be configured.")
        return {"version": 1, "servers": []}


def active_profiles_from_env() -> list[str]:
    """Active compose profiles, read from COMPOSE_PROFILES (set on the
    agent-manager container). The catalog is a static module-level structure
    built at import time, so it has no request context — env is the source."""
    raw = os.environ.get("COMPOSE_PROFILES", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _resolve_profiles(active_profiles: list[str] | None) -> list[str]:
    return active_profiles_from_env() if active_profiles is None else active_profiles


def _eligible(consumer: str, active_profiles: list[str]) -> list[dict]:
    """Return registry entries visible to ``consumer`` given ``active_profiles``."""
    out = []
    for s in _load().get("servers", []):
        if not s.get("enabled", False):
            continue
        prof = s.get("requires_profile")
        if prof and prof not in active_profiles:
            continue
        consumers = s.get("consumers", ["all"])
        if consumers != ["all"] and consumer not in consumers:
            continue
        out.append(s)
    return out


def moltis_mcp_env(active_profiles: list[str] | None = None) -> dict[str, str]:
    """Return the ``MOLTIS_MCP__SERVERS__<ID>__*`` env block.

    Double-underscore mapping translates to Moltis's TOML section
    ``[mcp.servers.<id>]``. Only ``sse`` and ``streamable-http`` transports
    are included — Moltis connects to remote MCP servers, not stdio processes.
    ``active_profiles`` defaults to COMPOSE_PROFILES (so the catalog can call
    this with no args, like llm_config).
    """
    profiles = _resolve_profiles(active_profiles)
    env: dict[str, str] = {}
    for s in _eligible("moltis", profiles):
        if s.get("transport") not in ("sse", "streamable-http"):
            continue
        key = s["id"].upper().replace("-", "_")
        env[f"MOLTIS_MCP__SERVERS__{key}__TRANSPORT"] = s["transport"]
        env[f"MOLTIS_MCP__SERVERS__{key}__URL"] = s["url"]
    return env


def hermes_mcp_add_commands(active_profiles: list[str] | None = None) -> list[str]:
    """Return ``hermes mcp add <id> --url <url>`` commands for the boot script.

    Only ``sse`` and ``streamable-http`` transports are included.
    Commands are fire-and-forget (``2>/dev/null || true``) so a missing
    Hermes binary or already-registered server won't abort provisioning.
    ``active_profiles`` defaults to COMPOSE_PROFILES.
    """
    cmds = []
    for s in _eligible("hermes", _resolve_profiles(active_profiles)):
        if s.get("transport") not in ("sse", "streamable-http"):
            continue
        cmds.append(
            f"/opt/hermes/.venv/bin/hermes mcp add {s['id']} --url {s['url']}"
            " 2>/dev/null || true"
        )
    return cmds


def opencode_mcp_block(active_profiles: list[str] | None = None) -> dict[str, dict]:
    """Return opencode's ``mcp`` config block: ``{<id>: {type, url, enabled}}``.

    OpenCode's config is assembled in catalog.py via
    ``llm_config.opencode_config_json(...)``; the catalog merges this block in
    under the ``mcp`` key. Only remote (sse/streamable-http) transports.
    ``active_profiles`` defaults to COMPOSE_PROFILES.
    """
    block: dict[str, dict] = {}
    for s in _eligible("opencode", _resolve_profiles(active_profiles)):
        if s.get("transport") not in ("sse", "streamable-http"):
            continue
        block[s["id"]] = {"type": "remote", "url": s["url"], "enabled": True}
    return block
