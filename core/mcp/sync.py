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
# P1: moltis, hermes, opencode. P2: gsd. P3: openwebui. P4: dify.
ALL_CONSUMERS = ["moltis", "hermes", "opencode", "gsd", "openwebui", "dify"]


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


#: #2672 — Open WebUI 0.11.3 lists an MCP tool server only when its
#: `config.enable` is truthy (backend/open_webui/routers/tools.py:137), and a
#: non-admin sees it only with a read grant in `config.access_grants`
#: (routers/tools.py:154 + 178-189 → utils/access_control/__init__.py:108-145,
#: where None or [] means private). Public read is the `user` / `*` / `read`
#: grant (utils/access_control/__init__.py:123; the admin UI writes the same
#: `config: {enable, access_grants}` pair, AddToolServerModal.svelte:212-214).
#: Public is right for the registry's servers: per-user authorisation happens
#: behind them (the gateway resolves the chat user's own session).
OWUI_PUBLIC_READ = {"principal_type": "user", "principal_id": "*", "permission": "read"}


def owui_default_config() -> dict:
    """The `config` a registry connection is seeded with: on, readable by all."""
    return {"enable": True, "access_grants": [dict(OWUI_PUBLIC_READ)]}


OWUI_GATEWAY_URL_PREFIX = "http://mcp-manager:5000/gw/"


def merge_owui_connections(current: list, emitted: list) -> list:
    """#2672 — Open WebUI's tool-server list after seeding the registry's.

    * A connection whose URL the registry does not emit is not ours: kept
      exactly as it is, in place. The exception is a gateway URL
      (OWUI_GATEWAY_URL_PREFIX): the registry owns those, so one it no longer
      emits is dropped.
    * A registry URL not in the list yet is appended, enabled and public
      (`owui_default_config`, under whatever the emitter put in `config`).
    * A registry URL already in the list gets the registry's url/path/type/
      auth/headers/key/info, but its `config` keeps every decision already in
      it: an `enable` key present (an admin's off switch, or our own earlier
      seed) is never overwritten, nor are `access_grants` (or the legacy
      `access_control` OWUI migrates on boot). Only a key that is ABSENT — a
      connection seeded before 0.11 with `config: {}`, never touched since — is
      filled in. So the rule is the model rows' one (#2673): the platform makes
      each decision once, and never again over an admin's.
    """
    defaults = owui_default_config()
    emitted_by_url = {c["url"]: c for c in emitted if isinstance(c, dict) and c.get("url")}
    out: list = []
    seen: set = set()
    for cur in current or []:
        url = cur.get("url") if isinstance(cur, dict) else None
        if url not in emitted_by_url:
            # A gateway URL the registry no longer emits (the mcp module is off, or a
            # server's module is): the backend is gone, the entry could only fail.
            if isinstance(url, str) and url.startswith(OWUI_GATEWAY_URL_PREFIX):
                continue
            out.append(cur)
            continue
        if url in seen:      # a second copy of a registry URL: one is enough
            continue
        seen.add(url)
        new = dict(emitted_by_url[url])
        cfg = dict(cur.get("config") or {})
        for key, value in {**defaults, **(new.get("config") or {})}.items():
            if key == "access_grants" and "access_control" in cfg:
                continue
            cfg.setdefault(key, value)
        new["config"] = cfg
        out.append(new)
    for url, conn in emitted_by_url.items():
        if url in seen:
            continue
        new = dict(conn)
        new["config"] = {**defaults, **(conn.get("config") or {})}
        out.append(new)
    return out


def owui_system_oauth() -> bool:
    """#2472: Open WebUI signs in through Authentik — OWUI_OIDC_ACTIVE (derived
    from ENABLE_OPENWEBUI_OIDC) is true and the client id is set — so its
    gateway connections forward the user's Authentik token (system_oauth).
    The same rule as mcp-manager's owui_oidc.system_oauth_on, which reads the
    same two keys from its compose environment."""
    active = (load_env_value("OWUI_OIDC_ACTIVE") or "").strip().lower() == "true"
    return active and bool((load_env_value("OPENWEBUI_OIDC_CLIENT_ID") or "").strip())


def owui_connections(spec: dict, profiles: list[str],
                     person_resolvable: bool | None = None) -> list[dict]:
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
            "config": owui_default_config(),   # #2672
            "info": {"id": s["id"], "name": s.get("description", s["id"])[:60]},
        }
        # Header auth (e.g. cognee X-Api-Key): resolve the secret_ref from .env.
        auth = s.get("auth") or {}
        secret = resolve_secret(s)
        if auth.get("header") and secret:
            conn["auth_type"] = "bearer" if auth["header"].lower() == "authorization" else "none"
            conn["headers"] = {auth["header"]: secret}
        out.append(conn)
    # #2472: the MCP gateway, as the chat user. 'session' = Open WebUI sends the
    # chat user's own session JWT; mcp-manager asks Open WebUI whose it is and
    # injects THAT user's upstream credential. Needs the mcp profile (the
    # gateway lives in mcp-manager) and each server's own modules. Eligibility
    # is the gateway's own (gateway_registry.eligible: consumer + every
    # required profile). Governance (available / min_tier) is per-person
    # runtime state the gateway checks on each call; it is not knowable here.
    #
    # The no-token window (no MCP_MANAGER_AUTHENTIK_READONLY_TOKEN, or Open
    # WebUI signup open): the gateway cannot map the chat user to a person, so
    # every per-person credential is refused ("connect first"). Only a server
    # with `credential: none` (Docling) is still served, so only those are
    # emitted; a connection that can only fail would be a chat-UI regression.
    # A `shared_proxy` entry is the technical users' and is never offered.
    #
    # system_oauth (the 2026.11 direction of the design record): while Open
    # WebUI signs in through Authentik (OWUI_OIDC_ACTIVE, with its client id
    # set), the connections use 'system_oauth' instead: Open WebUI 0.12.4
    # sends the chat user's own Authentik access token (utils/tools.py
    # build_tool_server_headers), which the gateway checks with Authentik's
    # public keys; Open WebUI's user table (an email an Open WebUI admin can
    # rewrite) no longer decides who the caller is. The open-signup rule does
    # not apply then: a natively registered account holds no Authentik token.
    if "mcp" in profiles:
        # #2472: RAZZFAZZ_NETWORK_MODE as .env states it (never derived from a
        # package): an offline box gets no server that needs the internet.
        offline = (load_env_value("RAZZFAZZ_NETWORK_MODE") or "online").strip().lower() == "offline"
        system_oauth = owui_system_oauth()
        if person_resolvable is None:
            signup = (load_env_value("ENABLE_SIGNUP") or "").strip().lower() == "true"
            person_resolvable = bool((load_env_value("MCP_MANAGER_AUTHENTIK_READONLY_TOKEN") or "").strip()) \
                and (system_oauth or not signup)
        for g in spec.get("gateway_servers") or []:
            if "openwebui" not in (g.get("consumers") or []):
                continue
            if not all(p in profiles for p in (g.get("requires_profiles") or [])):
                continue
            if offline and g.get("requires_egress"):
                continue
            cred = str(g.get("credential") or "")
            if cred.startswith("shared_proxy:"):
                continue
            if not person_resolvable and cred != "none":
                continue
            out.append({
                "url": f"{OWUI_GATEWAY_URL_PREFIX}{g['id']}/mcp",
                "path": "", "type": "mcp",
                "auth_type": "system_oauth" if system_oauth else "session",
                "headers": None, "key": None,
                "config": owui_default_config(),   # #2672: same shape as the registry's
                "info": {"id": g["id"], "name": str(g.get("description", g["id"]))[:60]},
            })
    return _json_dumps_compat(out)


def strip_internal(spec: dict) -> dict:
    """#151/#2472: the public export drops gateway servers marked internal
    (`visibility: internal`, any case); `servers:` and the rest stay as they are."""
    out = dict(spec)
    if "gateway_servers" in spec:
        out["gateway_servers"] = [g for g in spec.get("gateway_servers") or []
                                  if str(g.get("visibility", "")).lower() != "internal"]
    return out


def internal_gateway_ids(spec: dict) -> list:
    """#2472: ids of gateway servers marked internal (the publish gate's check)."""
    return [str(g.get("id")) for g in spec.get("gateway_servers") or []
            if str(g.get("visibility", "")).lower() == "internal"]


def _json_dumps_compat(obj):
    # return the python object; CLI --emit serialises it. Kept as a hook so
    # callers can post-process before serialisation.
    return obj


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--check", action="store_true", help="validate only; exit 1 on errors")
    ap.add_argument("--target", default="all", help=f"consumer or 'all' ({ALL_CONSUMERS})")
    ap.add_argument("--emit", choices=["owui"], help="print a consumer's MCP config (owui=JSON connections)")
    ap.add_argument("--merge-current", action="store_true",
                    help="with --emit owui: read Open WebUI's GET /api/v1/configs/tool_servers "
                         "answer on stdin and print the POST body with the registry's "
                         "connections merged in (#2672; exit 2 when stdin is unreadable)")
    ap.add_argument("--strip-internal", metavar="PATH",
                    help="rewrite PATH without its visibility:internal gateway servers (public export)")
    ap.add_argument("--check-no-internal", metavar="PATH",
                    help="exit 1 and name them when PATH still holds a visibility:internal gateway server")
    args = ap.parse_args()

    # The export modes act on a staged copy, not the box's registry, and run
    # before validation: the gate must refuse an internal entry whatever else
    # the staged file holds.
    if args.strip_internal or args.check_no_internal:
        path = Path(args.strip_internal or args.check_no_internal)
        staged = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(staged, dict):
            print(f"{path}: not a registry mapping", file=sys.stderr)
            return 1
        gw = staged.get("gateway_servers")
        if gw is not None and (not isinstance(gw, list)
                               or not all(isinstance(g, dict) for g in gw)):
            print(f"{path}: gateway_servers is not a list of entries", file=sys.stderr)
            return 1
        if args.strip_internal:
            # Nothing internal: leave the file byte-identical (comments included).
            if not internal_gateway_ids(staged):
                return 0
            path.write_text(yaml.safe_dump(strip_internal(staged), sort_keys=False,
                                           allow_unicode=True), encoding="utf-8")
            return 0
        ids = internal_gateway_ids(staged)
        if ids:
            print(f"internal gateway servers staged: {', '.join(ids)}", file=sys.stderr)
            return 1
        return 0

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
        emitted = owui_connections(spec, active_profiles())
        if args.merge_current:
            # An unreadable answer is not an empty list: POSTing the registry's
            # connections alone would delete every server an admin added.
            try:
                cur = _json.loads(sys.stdin.read())
                # an error body ({"detail": ...}) has no list key at all;
                # a fresh box answers it with null
                assert isinstance(cur, dict) and "TOOL_SERVER_CONNECTIONS" in cur
                conns = cur["TOOL_SERVER_CONNECTIONS"] or []
                assert isinstance(conns, list)
            except (ValueError, AssertionError):
                print("error: Open WebUI's tool-server list is unreadable", file=sys.stderr)
                return 2
            print(_json.dumps({"TOOL_SERVER_CONNECTIONS": merge_owui_connections(conns, emitted)}))
            return 0
        print(_json.dumps(emitted))
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
