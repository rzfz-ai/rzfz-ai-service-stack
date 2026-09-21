# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Per-user agent wiring for personal MCP proxies (#36).

Produces the SAME native-config shapes as agent-manager's mcp_config.py
(moltis env block / hermes `mcp add` commands / opencode block), but for a
user's OWN running MCP proxies — each URL points at that user's opaque-token
proxy subdomain. The agent-manager merges these per-user blocks into the user's
hermes/moltis/opencode instances at provision time (the same way it merges the
stack-wide registry).

A `proxies` list element:
  {mcp_id, instance_id, transport, mcp_path, consumers}

Consumer filtering: a proxy is wired into agent X only if X is in its
`consumers` (or consumers == ['all']). Only sse / streamable-http transports
are wired (agents connect to remote MCP endpoints, not stdio).

URLs are derived from the SAME opaque-token scheme caddy_client uses, so the
username never appears in the endpoint.
"""

from __future__ import annotations

import re

# DNS-/shell-safe MCP id: lowercase alnum + single dashes (NOT leading/trailing).
# This is the SAME constraint the catalog enforces on `id` (DNS-safe slug used
# in container/route names). Re-validated here as defense-in-depth because the
# wiring crosses into the agent boot path — an id is NEVER allowed to carry
# shell metachars. (commit-review CRITICAL #2)
_MCP_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def valid_mcp_id(mcp_id) -> bool:
    return isinstance(mcp_id, str) and bool(_MCP_ID_RE.match(mcp_id)) and len(mcp_id) <= 48


def proxy_url(mcp_id: str, instance_id, mcp_domain: str, mcp_path: str = "/mcp") -> str:
    """Build the per-user proxy's MCP endpoint URL (opaque-token subdomain)."""
    from app.services.caddy_client import instance_token
    path = mcp_path if mcp_path.startswith("/") else f"/{mcp_path}"
    return f"https://{mcp_id}-{instance_token(instance_id)}.{mcp_domain}{path}"


def _for_consumer(proxies: list[dict], consumer: str) -> list[dict]:
    out = []
    for p in proxies:
        # Hard reject any proxy whose id isn't a strict DNS-safe slug — it must
        # never reach moltis env keys / hermes argv / opencode config as data
        # that could be re-interpreted as a shell token downstream.
        if not valid_mcp_id(p.get("mcp_id")):
            continue
        if p.get("transport") not in ("sse", "streamable-http"):
            continue
        consumers = p.get("consumers", ["all"])
        if consumers != ["all"] and consumer not in consumers:
            continue
        out.append(p)
    return out


def _bearer(p: dict) -> str:
    """The per-proxy bearer secret (the REAL auth, CRITICAL-1 #61). Empty for a
    legacy proxy with no stored secret — the consumer just sends no header."""
    return str(p.get("route_secret") or "")


def moltis_env_for(proxies: list[dict], mcp_domain: str) -> dict:
    """MOLTIS_MCP__SERVERS__<ID>__{TRANSPORT,URL,HEADERS__AUTHORIZATION} env block.

    CRITICAL-1 (#61): each proxy URL is gated by a bearer; moltis sends it via
    the per-server Authorization header so only the owning user's agent reaches
    the proxy.
    """
    env: dict[str, str] = {}
    for p in _for_consumer(proxies, "moltis"):
        key = p["mcp_id"].upper().replace("-", "_")
        env[f"MOLTIS_MCP__SERVERS__{key}__TRANSPORT"] = p["transport"]
        env[f"MOLTIS_MCP__SERVERS__{key}__URL"] = proxy_url(
            p["mcp_id"], p["instance_id"], mcp_domain, p.get("mcp_path", "/mcp"))
        bearer = _bearer(p)
        if bearer:
            env[f"MOLTIS_MCP__SERVERS__{key}__HEADERS__AUTHORIZATION"] = \
                f"Bearer {bearer}"
    return env


def hermes_add_specs_for(proxies: list[dict], mcp_domain: str) -> list[dict]:
    """STRUCTURED hermes wiring: a list of {id, url, headers} dicts (NOT shell).

    commit-review CRITICAL #2: previously this emitted `hermes mcp add ... ||
    true` strings that the agent boot script `eval`'d — a command-injection sink
    for cross-service/user-influenced data. We now hand the agent-manager
    structured specs; the hermes boot script runs `hermes mcp add` with a quoted
    argv list (no shell, no eval), so the id (already strict-validated) and url
    are inert data.

    CRITICAL-1 (#61): each spec carries the proxy bearer in `headers` so only the
    owning user's hermes can reach the proxy (the boot script passes it as
    `--header "Authorization: Bearer <secret>"` in the quoted argv).
    """
    specs = []
    for p in _for_consumer(proxies, "hermes"):
        spec = {
            "id": p["mcp_id"],
            "url": proxy_url(p["mcp_id"], p["instance_id"], mcp_domain,
                             p.get("mcp_path", "/mcp")),
        }
        bearer = _bearer(p)
        if bearer:
            spec["headers"] = {"Authorization": f"Bearer {bearer}"}
        specs.append(spec)
    return specs


def opencode_block_for(proxies: list[dict], mcp_domain: str) -> dict:
    """opencode `mcp` config block: {<id>: {type, url, enabled, headers}}.

    CRITICAL-1 (#61): the bearer is carried in opencode's per-server `headers`
    so the agent presents it on every call to the gated proxy.
    """
    block: dict[str, dict] = {}
    for p in _for_consumer(proxies, "opencode"):
        entry = {
            "type": "remote",
            "url": proxy_url(p["mcp_id"], p["instance_id"], mcp_domain, p.get("mcp_path", "/mcp")),
            "enabled": True,
        }
        bearer = _bearer(p)
        if bearer:
            entry["headers"] = {"Authorization": f"Bearer {bearer}"}
        block[p["mcp_id"]] = entry
    return block


def claude_mcp_json_for(proxies: list[dict], mcp_domain: str) -> dict:
    """Claude Code `.mcp.json` / `~/.claude.json` `mcpServers` shape (#36).

    Claude Code reads remote MCP servers from a ``mcpServers`` map, each entry:
      {"type": "http"|"sse", "url": ..., "headers": {"Authorization": "Bearer …"}}

    The agent-manager writes this into a MANAGED block of the coding agent's
    ~/.claude.json (never clobbering the user's manual `claude mcp add` entries).
    Only sse / streamable-http remotes are wired; the per-proxy bearer is carried
    in ``headers`` so only the owning user's agent reaches the gated proxy.
    """
    servers: dict[str, dict] = {}
    for p in _for_consumer(proxies, "claude"):
        # Claude Code's remote transport keyword is "http" for streamable-http.
        ctype = "sse" if p["transport"] == "sse" else "http"
        entry = {
            "type": ctype,
            "url": proxy_url(p["mcp_id"], p["instance_id"], mcp_domain,
                             p.get("mcp_path", "/mcp")),
        }
        bearer = _bearer(p)
        if bearer:
            entry["headers"] = {"Authorization": f"Bearer {bearer}"}
        servers[p["mcp_id"]] = entry
    return {"mcpServers": servers}


def codex_mcp_block_for(proxies: list[dict], mcp_domain: str) -> dict:
    """Codex `~/.codex/config.toml` [mcp_servers.<id>] shape (#36).

    Codex's streamable-http remote MCP form is a table per server with ``url``
    (and, for a bearer-gated endpoint, ``bearer_token``). The entrypoint renders
    this dict into TOML in a MANAGED block. Only sse / streamable-http remotes
    are wired.
    """
    block: dict[str, dict] = {}
    for p in _for_consumer(proxies, "codex"):
        entry = {
            "url": proxy_url(p["mcp_id"], p["instance_id"], mcp_domain,
                             p.get("mcp_path", "/mcp")),
        }
        bearer = _bearer(p)
        if bearer:
            entry["bearer_token"] = bearer
        block[p["mcp_id"]] = entry
    return block


def build_user_proxies(db, catalog, user_slug: str) -> list[dict]:
    """Assemble the agent_wiring `proxies` list for ONE user's RUNNING proxies.

    Joins each running mcp_instances row with its catalog entry (transport,
    mcp_path) and the consumer set. Per-user `mcp_type_bindings` (if the user
    pinned which agents an MCP is wired into) take precedence over the catalog's
    default `consumers`; otherwise the catalog default applies.

    Strictly scoped to ``user_slug`` (the DB query filters on it) — this is the
    only-own boundary for the service-to-service wiring path.
    """
    # Per-user binding overrides: {mcp_id: [consumer, ...]}.
    bindings: dict[str, list] = {}
    try:
        for row in (db.get_bindings(user_slug) or []):
            bindings.setdefault(row["mcp_id"], []).append(row["consumer"])
    except Exception:
        bindings = {}

    proxies: list[dict] = []
    for inst in (db.get_user_instances(user_slug) or []):
        if inst.get("state") != "running":
            continue
        mcp_id = inst.get("mcp_id")
        # Hard reject non-DNS-safe ids before they enter the wiring (defense in
        # depth — the catalog already constrains ids, but never trust the row).
        if not valid_mcp_id(mcp_id):
            continue
        integ = catalog.get(mcp_id) if catalog else None
        if not integ:
            continue
        consumers = bindings.get(mcp_id) or integ.get("consumers", ["all"])
        # CRITICAL-1 (#61): carry the per-proxy bearer so the consumer can
        # present `Authorization: Bearer <secret>`. The secret lives in the
        # instance config (never returned to the user-facing API).
        cfg = inst.get("config") or {}
        route_secret = cfg.get("_route_secret") if isinstance(cfg, dict) else None
        proxies.append({
            "mcp_id": mcp_id,
            "instance_id": inst.get("id"),
            "transport": integ.get("transport"),
            "mcp_path": integ.get("mcp_path", "/mcp"),
            "consumers": consumers,
            "route_secret": route_secret,
        })
    return proxies


def wiring_for_user(db, catalog, user_slug: str, mcp_domain: str) -> dict:
    """Full per-user wiring payload for the internal agent-wiring endpoint.

    Returns the three native-config shapes (moltis env / hermes commands /
    opencode block) plus the raw `proxies` list, all for ``user_slug`` only.
    """
    proxies = build_user_proxies(db, catalog, user_slug)
    return {
        "moltis_env": moltis_env_for(proxies, mcp_domain),
        # STRUCTURED specs (commit-review CRITICAL #2) — no shell strings.
        "hermes_specs": hermes_add_specs_for(proxies, mcp_domain),
        "opencode_block": opencode_block_for(proxies, mcp_domain),
        # #36 follow-up: coding-agent shapes (Claude Code + Codex).
        "claude_mcp": claude_mcp_json_for(proxies, mcp_domain),
        "codex_mcp": codex_mcp_block_for(proxies, mcp_domain),
        "proxies": [{"mcp_id": p["mcp_id"]} for p in proxies],
    }
