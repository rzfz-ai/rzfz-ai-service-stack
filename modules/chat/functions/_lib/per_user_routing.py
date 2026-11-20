"""Per-user routing helper for hermes / moltis / opencode pipes (M020 S01).

Source of truth lives here. Pipes inline this module via the seeder's
`# @include _lib/per_user_routing.py` directive — see chat/seeder/seed.py
Duty A. The directive is processed by the seeder before POSTing the pipe
to OpenWebUI's REST API, so the resulting Function source in the OpenWebUI
DB is fully self-contained — no relative-import dependency on Open WebUI's
Function sandbox supporting sibling-file imports (which it doesn't, by
empirical test in M020 S01's POC).

The module is also importable directly as `_lib.per_user_routing` for
local testing / pytest snapshots — the inlining is a transport concern,
not a runtime one.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("per_user_routing")

# -----------------------------------------------------------------------------
# Public types

@dataclass
class AgentAuth:
    method: str  # "bearer" | "basic" | "password" | "none"
    token: str = ""
    # rc6.7 #79: optional username for HTTP Basic auth — agent-manager
    # /api/find populates this per agent type (e.g. coding-tools/opencode
    # uses "opencode", which is opencode-serve's documented default
    # username). Empty string falls back to "admin" in basic_auth_tuple
    # for backward compat.
    user: str = ""


@dataclass
class AgentInstance:
    instance_id: str
    container_name: str
    internal_url: str
    state: str  # "running"
    auth: AgentAuth
    extra_ports: dict = field(default_factory=dict)


@dataclass
class NotProvisioned:
    agent_type: str
    launch_url: str
    msg: str  # pre-formatted Markdown for the pipe to yield directly


# -----------------------------------------------------------------------------
# Cache + lookup

_AGENT_MANAGER_URL = os.environ.get("AGENT_MANAGER_URL", "http://agent-manager:5000")
_CACHE_TTL_S = float(os.environ.get("PER_USER_ROUTING_CACHE_TTL_S", "30"))
_REQUEST_TIMEOUT_S = 5.0

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str], tuple[float, object]] = {}  # (user_id, agent_type) -> (expiry_ts, AgentInstance|NotProvisioned)


def _cache_get(key: tuple[str, str]):
    now = time.time()
    with _CACHE_LOCK:
        v = _CACHE.get(key)
        if v and v[0] > now:
            return v[1]
        if v:
            _CACHE.pop(key, None)
    return None


def _cache_put(key: tuple[str, str], value) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + _CACHE_TTL_S, value)


def _build_msg(agent_type: str, launch_url: str) -> str:
    label = agent_type.replace("-", " ").title()
    if launch_url:
        how = f"[➜ Provision one]({launch_url})"
    else:
        # #2109: no URL is better than a broken one — without a known domain the
        # user is told where the portal is instead of handed a dead link.
        how = "Open **Agents** from the start portal and provision one."
    return (
        f"You don't have a **{label}** agent yet.\n\n"
        f"{how}\n\n"
        f"Once provisioned, send another message and I'll route it to your instance."
    )


def _fallback_launch_url(agent_type: str) -> str:
    """The agent portal's launch link when agent-manager did not supply one.

    #2109: this used to be the LITERAL string `https://agents.<MAIN_DOMAIN>/…` —
    the placeholder was never substituted, so every degraded path (agent-manager
    unreachable, non-404 status, invalid JSON, a 404 without a URL) handed the
    user an unresolvable link, precisely when they most needed a working one.
    The Function runs inside the openwebui container, whose environment carries
    the stack's MAIN_DOMAIN (env_file ../../.env), so the host is known there;
    when it is not, return "" and let _build_msg say where the portal is.
    """
    domain = (os.environ.get("MAIN_DOMAIN") or "").strip().strip(".")
    if not domain or "<" in domain or "/" in domain:
        return ""
    return f"https://agents.{domain}/dashboard?launch={urllib.parse.quote(agent_type)}"


def find(
    agent_type: str,
    user: Optional[dict] = None,
    *,
    use_extra_port: Optional[str] = None,
    force_refresh: bool = False,
):
    """Look up the calling user's instance of `agent_type`.

    Returns AgentInstance on success or NotProvisioned with a clickable
    launch link if the user hasn't created one yet (or if it's stopped).

    `use_extra_port` selects a non-primary port from the catalog
    (e.g. hermes pipe passes `use_extra_port="agent_internal"` to talk to
    the agent gateway on :8642 instead of the workspace UI on :3000).

    Caches positive results for `_CACHE_TTL_S` seconds (default 30) per
    (user_id, agent_type) — keeps agent-manager out of the hot path on
    repeated chat turns. Force-refresh by passing `force_refresh=True`.
    """
    user = user or {}
    user_id = str(user.get("id") or user.get("username") or user.get("name") or "anonymous")
    cache_key = (user_id, agent_type)

    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return _apply_extra_port(cached, use_extra_port)

    # OpenWebUI's __user__ dict carries `name` (display name "Alexander Vukovic")
    # for SSO users, NOT `username`. agent-manager derives user_slug from
    # X-Authentik-Username via make_user_slug() (lowercase + non-alphanumeric →
    # hyphen), so passing the display name produces the same slug
    # agent-manager used at provisioning time. Without this we'd send the
    # OpenWebUI UUID and never find the running instance.
    forwarded_username = (
        user.get("username")           # If something already set it (Authentik direct), prefer
        or user.get("name")            # OpenWebUI's display-name field — what SSO writes
        or user_id
    )
    headers = {
        "X-Authentik-Username": str(forwarded_username),
        "X-Authentik-Uid": user_id,
        "X-Authentik-Email": str(user.get("email") or ""),
        "X-Authentik-Groups": "|".join(user.get("groups") or []),
    }

    url = f"{_AGENT_MANAGER_URL.rstrip('/')}/api/find/{urllib.parse.quote(agent_type)}"
    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {}
        if e.code == 404:
            launch_url = err_body.get("launch_url") or err_body.get("start_url") \
                or _fallback_launch_url(agent_type)
            np = NotProvisioned(agent_type=agent_type, launch_url=launch_url,
                                msg=_build_msg(agent_type, launch_url))
            _cache_put(cache_key, np)
            return np
        log.warning("per_user_routing: agent-manager %s returned HTTP %d", url, e.code)
        return _fallback_not_provisioned(agent_type)
    except (urllib.error.URLError, OSError) as e:
        log.warning("per_user_routing: agent-manager %s unreachable: %s", url, e)
        return _fallback_not_provisioned(agent_type)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("per_user_routing: agent-manager %s returned invalid JSON: %s", url, e)
        return _fallback_not_provisioned(agent_type)

    auth_payload = payload.get("auth") or {}
    inst = AgentInstance(
        instance_id=str(payload.get("instance_id", "")),
        container_name=str(payload.get("container_name", "")),
        internal_url=str(payload.get("internal_url", "")),
        state=str(payload.get("state", "running")),
        auth=AgentAuth(method=str(auth_payload.get("method", "none")),
                       token=str(auth_payload.get("token", "")),
                       user=str(auth_payload.get("user", ""))),
        extra_ports=dict(payload.get("extra_ports") or {}),
    )
    _cache_put(cache_key, inst)
    return _apply_extra_port(inst, use_extra_port)


def _fallback_not_provisioned(agent_type: str) -> NotProvisioned:
    launch_url = _fallback_launch_url(agent_type)
    return NotProvisioned(agent_type=agent_type, launch_url=launch_url,
                          msg=_build_msg(agent_type, launch_url))


def _apply_extra_port(value, use_extra_port: Optional[str]):
    if use_extra_port is None or not isinstance(value, AgentInstance):
        return value
    port = value.extra_ports.get(use_extra_port)
    if not port:
        return value
    # Build a new AgentInstance with the alt port substituted into internal_url.
    if "://" in value.internal_url:
        scheme, rest = value.internal_url.split("://", 1)
        host = rest.split(":", 1)[0]
        new_url = f"{scheme}://{host}:{port}"
    else:
        new_url = f"http://{value.container_name}:{port}"
    return AgentInstance(
        instance_id=value.instance_id,
        container_name=value.container_name,
        internal_url=new_url,
        state=value.state,
        auth=value.auth,
        extra_ports=value.extra_ports,
    )


# -----------------------------------------------------------------------------
# Pipe convenience: build common request kwargs from an AgentAuth

def auth_headers(auth: AgentAuth) -> dict:
    """Return HTTP headers a pipe should attach when calling its backend."""
    if auth.method == "bearer" and auth.token:
        return {"Authorization": f"Bearer {auth.token}"}
    return {}


def basic_auth_tuple(auth: AgentAuth) -> Optional[tuple[str, str]]:
    """Return (user, password) for HTTP Basic auth, or None if not applicable.

    Uses `auth.user` when set by agent-manager (e.g. coding-tools/opencode
    serve expects username "opencode" per upstream docs), else falls back
    to "admin" for backward compat with any non-listed basic-auth backend.
    """
    if auth.method == "basic" and auth.token:
        return (auth.user or "admin", auth.token)
    return None
