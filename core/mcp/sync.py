#!/usr/bin/env python3
"""Stack-wide MCP registry sync engine (M035).

Reads core/mcp/mcp-servers.yaml (single source of truth), validates it, filters
servers by enabled / requires_profile / consumers, resolves auth secrets from
.env, and emits each consumer's native MCP config.

Modelled on core/llm/sync.py. Same exit-code contract:
    0 — synced (or --check found no drift)
    1 — invalid registry / missing secret / --check found drift

Usage:
    python3 core/mcp/sync.py                 # sync all eligible consumers
    python3 core/mcp/sync.py --check         # read-only: report config drift
    python3 core/mcp/sync.py --target moltis # sync one consumer
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("error: pyyaml not installed. Install with: pip3 install pyyaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO_ROOT / "core" / "mcp" / "mcp-servers.yaml"
ENV_FILE = REPO_ROOT / ".env"

VALID_TRANSPORTS = {"stdio", "sse", "streamable-http"}
VALID_SCOPES = {"internal", "external"}
# P1: moltis, hermes, opencode. P2: openhands, gsd. P3: openwebui. P4: dify.
ALL_CONSUMERS = ["moltis", "hermes", "opencode", "openhands", "gsd", "openwebui", "dify"]


def load_registry(path: str = str(DEFAULT_REGISTRY)) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"registry not found: {path}")
    return yaml.safe_load(p.read_text()) or {}


def validate(spec: dict) -> list[str]:
    """Return a list of human-readable validation errors ([] if valid)."""
    errors: list[str] = []
    if spec.get("version") != 1:
        errors.append(f"unsupported registry version: {spec.get('version')}")
    seen: set[str] = set()
    for s in spec.get("servers", []):
        sid = s.get("id")
        if not sid:
            errors.append("server entry missing 'id'")
            continue
        if sid in seen:
            errors.append(f"{sid}: duplicate id")
        seen.add(sid)
        transport = s.get("transport")
        if transport not in VALID_TRANSPORTS:
            errors.append(f"{sid}: invalid transport {transport!r} (allowed: {sorted(VALID_TRANSPORTS)})")
        if s.get("scope") not in VALID_SCOPES:
            errors.append(f"{sid}: invalid scope {s.get('scope')!r} (allowed: {sorted(VALID_SCOPES)})")
        if transport == "stdio":
            if not s.get("command"):
                errors.append(f"{sid}: stdio transport requires 'command'")
        elif transport in ("sse", "streamable-http"):
            if not s.get("url"):
                errors.append(f"{sid}: {transport} transport requires 'url'")
        auth = s.get("auth")
        if auth is not None:
            if not auth.get("secret_ref"):
                errors.append(f"{sid}: auth present but missing 'secret_ref'")
        consumers = s.get("consumers")
        if consumers != ["all"] and consumers is not None:
            bad = [c for c in consumers if c not in ALL_CONSUMERS]
            if bad:
                errors.append(f"{sid}: unknown consumer(s) {bad}; valid: {ALL_CONSUMERS} or 'all'")
    return errors


def load_env_value(key: str) -> str | None:
    """Targeted grep of .env (never `source` — operator-edited .env may carry
    spaces/metachars; project rule)."""
    if not ENV_FILE.exists():
        return None
    pat = re.compile(rf"^{re.escape(key)}=(.*)$")
    for line in ENV_FILE.read_text().splitlines():
        m = pat.match(line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def servers_for(consumer: str, spec: dict, active_profiles: list[str]) -> list[dict]:
    """Servers from the registry that should be wired into `consumer`, after
    enabled / requires_profile / consumers filtering."""
    out = []
    for s in spec.get("servers", []):
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


def resolve_secret(server: dict) -> str | None:
    """Resolve a server's auth secret from .env via its secret_ref, or None."""
    auth = server.get("auth")
    if not auth or not auth.get("secret_ref"):
        return None
    return load_env_value(auth["secret_ref"])


def active_profiles() -> list[str]:
    val = load_env_value("COMPOSE_PROFILES") or ""
    return [p.strip() for p in val.split(",") if p.strip()]


def owui_connections(spec: dict, profiles: list[str]) -> list[dict]:
    """Emit Open WebUI native-MCP tool-server connections (P3) for eligible
    servers. OWUI 0.9.x supports type='mcp' streamable-http connections via
    POST /api/v1/configs/tool_servers (the ToolServerConnection model). Only
    sse/streamable-http servers apply (OWUI native MCP is HTTP-only)."""
    import json as _json
    out = []
    for s in servers_for("openwebui", spec, profiles):
        if s.get("transport") not in ("sse", "streamable-http"):
            continue
        # OWUI's ToolServerConnection (Pydantic v2) requires url/path/auth_type/
        # key/config to be present (Optional[...] without a default = nullable
        # but required). Include all of them or the POST 422s.
        conn = {
            "url": s["url"],
            "path": "",
            "type": "mcp",
            "auth_type": "none",
            "headers": None,
            "key": None,
            "config": {},
            "info": {"id": s["id"], "name": s.get("description", s["id"])[:60]},
        }
        # Header auth (e.g. cognee X-Api-Key): resolve the secret_ref from .env.
        auth = s.get("auth") or {}
        secret = resolve_secret(s)
        if auth.get("header") and secret:
            conn["auth_type"] = "bearer" if auth["header"].lower() == "authorization" else "none"
            conn["headers"] = {auth["header"]: secret}
        out.append(conn)
    return _json_dumps_compat(out)


def _json_dumps_compat(obj):
    # return the python object; CLI --emit serialises it. Kept as a hook so
    # callers can post-process before serialisation.
    return obj


def openhands_config_toml(spec: dict, profiles: list[str]) -> str:
    """Emit an OpenHands `[mcp]` config.toml block (P2). OpenHands 1.6.0 reads
    /app/config.toml: streamable-http → shttp_servers, sse → sse_servers, each
    {url, api_key?}. api_key resolved from the server's secret_ref. Returns the
    TOML text (empty string if no eligible servers)."""
    shttp, sse = [], []
    for s in servers_for("openhands", spec, profiles):
        t = s.get("transport")
        if t not in ("sse", "streamable-http"):
            continue
        entry = {"url": s["url"]}
        secret = resolve_secret(s)
        if secret:
            entry["api_key"] = secret
        (shttp if t == "streamable-http" else sse).append(entry)
    if not shttp and not sse:
        return ""

    def _fmt(entries):
        rows = []
        for e in entries:
            kv = ", ".join(f'{k} = "{v}"' for k, v in e.items())
            rows.append("    {" + kv + "}")
        return "[\n" + ",\n".join(rows) + "\n]"

    out = ["# Generated by core/mcp/sync.py --emit openhands-toml (M035 P2). Do not hand-edit.", "[mcp]"]
    if shttp:
        out.append(f"shttp_servers = {_fmt(shttp)}")
    if sse:
        out.append(f"sse_servers = {_fmt(sse)}")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--check", action="store_true", help="validate only; exit 1 on errors")
    ap.add_argument("--target", default="all", help=f"consumer or 'all' ({ALL_CONSUMERS})")
    ap.add_argument("--emit", choices=["owui", "openhands-toml"], help="print a consumer's MCP config (owui=JSON connections, openhands-toml=config.toml [mcp] block)")
    args = ap.parse_args()

    spec = load_registry(args.config)
    errors = validate(spec)
    if errors:
        for e in errors:
            print(f"validation: {e}", file=sys.stderr)
        return 1
    if args.check:
        print("mcp registry: valid")
        return 0

    if args.emit == "owui":
        import json as _json
        print(_json.dumps(owui_connections(spec, active_profiles())))
        return 0
    if args.emit == "openhands-toml":
        print(openhands_config_toml(spec, active_profiles()), end="")
        return 0

    if args.target != "all" and args.target not in ALL_CONSUMERS:
        print(f"unknown target {args.target!r}; valid: {ALL_CONSUMERS} or 'all'", file=sys.stderr)
        return 1
    # P1 consumers (moltis/hermes) are wired at agent provision time via
    # mcp_config (the catalog reads the registry directly), and opencode at
    # container start. So sync.py's P1 job is the validate gate above plus this
    # eligibility summary; later phases add active push (e.g. dify plugin config).
    targets = ALL_CONSUMERS if args.target == "all" else [args.target]
    profs = active_profiles()
    for t in targets:
        n = len(servers_for(t, spec, profs))
        print(f"{t}: {n} MCP server(s) eligible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
