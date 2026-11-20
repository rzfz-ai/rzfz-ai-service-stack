# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The gateway's server registry (#2472/#2086): `gateway_servers:` in
core/mcp/mcp-servers.yaml. Existing readers of that file (agent-manager's
mcp_config, core/mcp/sync.py) read `servers:` and never see these entries.

credential:
  none                    stateless upstream (Docling): no user credential
  catalog:<mcp_id>        the caller's credential for personal-catalog entry
                          <mcp_id> (OAuth or PAT, from the vault), injected
                          per request as `auth_header`
  per_user_proxy:<mcp_id> relay to the caller's OWN running per-user proxy
                          (cognee, google, …) with its route secret; never
                          for a technical caller
  shared_proxy:<mcp_id>   (#2472, B9) relay a TECHNICAL caller to the instance
                          an admin provisioned for that `~svc-<name>` of a
                          catalogue entry whose tenancy is `shared` (the
                          cognee company brain); requires `technical: true`,
                          never serves a person

guard (optional): the name of a per-server request/response guard the relay
applies (`docling`: app/services/docling_guard.py — a two-tool allow-list,
http(s)-only sources, per-caller document keys, scrubbed errors; relayed
buffered, never streamed; `gitea` (#2554): app/services/gitea_guard.py — only
the pinned gitea-mcp's own tools, arguments spelled exactly as it reads them,
per-tool target-branch rules for every branch-content write, and the §9
extension's persons-only write tools; a `gitea` entry's `write_guard` must name every tool gitea_guard.WRITE_RULES covers,
or the registry does not load).

relay_secret_env (optional, credential `none` only): the env key of a relay
secret the gateway sends as HTTP Basic `mcp-gateway:<value>` (Docling's
egress sidecar admits only that credential, checked against a bcrypt hash). Must end in `_RELAY_SECRET`; an empty value
refuses the call (503) rather than dialling without it.

timeout (optional, seconds, default DEFAULT_TIMEOUT): the longest a single
POST to this upstream may take. The relay uses it as the read timeout, and
gunicorn.conf.py sets the worker timeout from the largest one (B4 §3(g)).

An invalid registry loads as EMPTY: a gateway that cannot read its registry
offers nothing rather than guessing."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path(os.environ.get("MCP_SERVERS_YAML", "/mcp-servers.yaml"))
CONSUMERS = {"agent", "openwebui", "dify"}
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600
_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GUARDS = {"docling", "gitea"}
_RELAY_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,63}_RELAY_SECRET$")
_CRED = re.compile(r"^(none|(catalog|per_user_proxy|shared_proxy):[a-z0-9]+(?:-[a-z0-9]+)*)$")


@dataclass(frozen=True)
class GatewayServer:
    id: str
    description: str
    url: str
    credential: str
    auth_header: str
    consumers: frozenset
    technical: bool
    requires_profiles: tuple
    requires_egress: bool
    write_guard: frozenset
    visibility: str = ""
    timeout: int = DEFAULT_TIMEOUT
    guard: str = ""
    relay_secret_env: str = ""

    @property
    def credential_kind(self) -> str:
        return self.credential.split(":", 1)[0]

    @property
    def credential_ref(self) -> str:
        return self.credential.split(":", 1)[1] if ":" in self.credential else ""


def validate_gateway_servers(entries) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    if entries is not None and not isinstance(entries, list):
        return [f"gateway_servers must be a list, not {type(entries).__name__}"]
    for e in entries or []:
        if not isinstance(e, dict):
            errors.append(f"entry is not a mapping: {e!r}")
            continue
        sid = e.get("id")
        if not isinstance(sid, str) or not _ID.match(sid) or len(sid) > 48:
            errors.append(f"invalid id {sid!r}")
            continue
        if sid in seen:
            errors.append(f"{sid}: duplicate id")
        seen.add(sid)
        cred = str(e.get("credential", ""))
        if not _CRED.match(cred):
            errors.append(f"{sid}: invalid credential {cred!r}")
        if cred.startswith("per_user_proxy:"):
            if e.get("technical"):
                errors.append(f"{sid}: a per-user proxy is never lent to a technical user")
        elif cred.startswith("shared_proxy:"):
            if e.get("technical") is not True:
                errors.append(f"{sid}: shared_proxy serves technical users only (technical: true)")
        elif not str(e.get("url", "")).startswith(("http://", "https://")):
            # (a proxy entry has no url: its instance is looked up per caller)
            errors.append(f"{sid}: 'url' (http/https) is required")
        if cred.startswith("catalog:") and "{token}" not in str(e.get("auth_header", "Bearer {token}")):
            errors.append(f"{sid}: auth_header must contain {{token}}")
        cons = e.get("consumers") or []
        if not isinstance(cons, list):
            cons = [cons]
        bad = [c for c in cons if c not in CONSUMERS]
        if not cons or bad:
            errors.append(f"{sid}: consumers must be a non-empty subset of {sorted(CONSUMERS)} (bad: {bad})")
        if not isinstance(e.get("requires_egress"), bool):
            errors.append(f"{sid}: requires_egress must be true or false")
        for key in ("requires_profiles", "write_guard"):
            val = e.get(key)
            if val is not None and (not isinstance(val, list)
                                    or not all(isinstance(v, str) and v for v in val)):
                errors.append(f"{sid}: {key} must be a list of names")
        if "timeout" in e:
            t = e.get("timeout")
            if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= MAX_TIMEOUT:
                errors.append(f"{sid}: timeout must be whole seconds in 1..{MAX_TIMEOUT}")
        if str(e.get("visibility", "")) not in ("", "internal"):
            errors.append(f"{sid}: visibility must be empty or 'internal'")
        if "guard" in e and e.get("guard") not in GUARDS:
            errors.append(f"{sid}: guard must be one of {sorted(GUARDS)}")
        if e.get("guard") == "gitea":
            from app.services.gitea_guard import WRITE_RULES
            listed = e.get("write_guard") if isinstance(e.get("write_guard"), list) else []
            missing = sorted(set(WRITE_RULES) - set(listed))
            if missing:
                errors.append(f"{sid}: a gitea entry's write_guard must list {missing} "
                              "(every write of the pinned gitea-mcp that has a rule)")
        if "relay_secret_env" in e:
            env = e.get("relay_secret_env")
            if not isinstance(env, str) or not _RELAY_ENV.match(env):
                errors.append(f"{sid}: relay_secret_env must be an env key ending in _RELAY_SECRET")
            elif cred != "none":
                errors.append(f"{sid}: relay_secret_env is only for credential 'none'")
    return errors


def load_gateway_servers(path=None) -> dict:
    import yaml
    p = Path(path) if path else REGISTRY_PATH
    if not p.exists():
        logger.warning("gateway registry not found at %s: the gateway offers no server", p)
        return {}
    try:
        spec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.exception("gateway registry unreadable: %s", p)
        return {}
    if not isinstance(spec, dict):
        logger.error("gateway registry: %s is not a mapping", p)
        return {}
    entries = spec.get("gateway_servers") or []
    errors = validate_gateway_servers(entries)
    if errors:
        for err in errors:
            logger.error("gateway registry: %s", err)
        return {}
    return {e["id"]: GatewayServer(
        id=e["id"], description=str(e.get("description", "")), url=str(e.get("url", "")),
        credential=str(e["credential"]), auth_header=str(e.get("auth_header", "Bearer {token}")),
        consumers=frozenset(e["consumers"]), technical=bool(e.get("technical", False)),
        requires_profiles=tuple(e.get("requires_profiles") or ()),
        requires_egress=bool(e["requires_egress"]),
        write_guard=frozenset(e.get("write_guard") or ()),
        visibility=str(e.get("visibility", "")),
        timeout=int(e.get("timeout", DEFAULT_TIMEOUT)),
        guard=str(e.get("guard", "")),
        relay_secret_env=str(e.get("relay_secret_env", ""))) for e in entries}


@dataclass(frozen=True)
class GatewayLimits:
    """`gateway_limits:` in the registry file (#2472 fix round 1, I2).

    per_caller           concurrent relays one caller (principal) may hold; 429 beyond
    total                concurrent relays the whole gateway may hold; 503 beyond, so
                         the rest of the worker's threads (gunicorn.conf.py) stay for
                         the portal, /healthz and /api/tls/ask
    stream_idle_timeout  seconds a GET stream may stay silent before the relay ends
                         it (clients resume with Last-Event-ID)
    max_body_bytes       the largest request body the gateway reads; 413 beyond"""
    per_caller: int = 8
    total: int = 48
    stream_idle_timeout: int = 300
    max_body_bytes: int = 4 * 1024 * 1024


_LIMIT_RANGES = {"per_caller": (1, 64), "total": (1, 56), "stream_idle_timeout": (1, MAX_TIMEOUT),
                 "max_body_bytes": (1024, 64 * 1024 * 1024)}


def validate_gateway_limits(spec) -> list[str]:
    if spec is None:
        return []
    if not isinstance(spec, dict):
        return ["gateway_limits must be a mapping"]
    errors = [f"gateway_limits: unknown key {k!r}" for k in spec if k not in _LIMIT_RANGES]
    for key, (lo, hi) in _LIMIT_RANGES.items():
        if key in spec:
            v = spec[key]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                errors.append(f"gateway_limits.{key} must be a whole number in {lo}..{hi}")
    if not errors and spec.get("per_caller", GatewayLimits.per_caller) > spec.get("total", GatewayLimits.total):
        errors.append("gateway_limits.per_caller must not exceed gateway_limits.total")
    return errors


def load_gateway_limits(path=None) -> GatewayLimits:
    """The limits, or the DEFAULTS when the block is absent or invalid: the
    defaults are the safe caps, so an unreadable block never lifts them."""
    import yaml
    p = Path(path) if path else REGISTRY_PATH
    try:
        spec = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    except Exception:
        logger.exception("gateway registry unreadable: %s", p)
        return GatewayLimits()
    block = spec.get("gateway_limits") if isinstance(spec, dict) else None
    errors = validate_gateway_limits(block)
    if errors:
        for err in errors:
            logger.error("gateway registry: %s (using the default limits)", err)
        return GatewayLimits()
    return GatewayLimits(**(block or {}))


def longest_timeout(servers) -> int:
    """The largest per-server timeout (DEFAULT_TIMEOUT when there is none)."""
    return max([DEFAULT_TIMEOUT] + [int(s.timeout) for s in (servers or {}).values()])


def eligible(server: GatewayServer, consumer: str, active_profiles, network_mode: str = "online") -> bool:
    """`network_mode` is RAZZFAZZ_NETWORK_MODE as the box states it (read,
    never derived): an offline box offers no server that needs the internet
    (#2472), and the gateway refuses it like an unknown one."""
    if server.requires_egress and (network_mode or "online").strip().lower() == "offline":
        return False
    return (consumer in server.consumers
            and all(p in active_profiles for p in server.requires_profiles))
