# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Whether a card on the MCP Integrations page can be connected on THIS box
(#3424).

Two things make a card a dead end, and both are the administrator's to fix,
not the person's:

* its ENGINE is off — a `runtime: gateway` card is served by a shared MCP
  server that a module of its own runs (the catalogue's `engine_profile`:
  gitea, mcp-atlassian, mcp-m365). Without that module the card reads "not
  enabled on this box" and connecting is refused.
* its OAuth provider is NOT SET UP — the client id or secret is empty in the
  manager's environment, or the sign-in address could not be built.

The page shows the reason instead of a connect button, and /oauth/start and
/oauth/callback refuse with the same answer, so the server decides, not the
page. Active profiles are read from COMPOSE_PROFILES in the manager's own
environment, as the gateway and /api/stack-tools read them: a module toggle
recreates the manager with the new list."""
from __future__ import annotations

import os

from app.services import remote_mcp

ENGINE_OFF = "Not enabled on this box — ask your admin."
#: #3427: the catalogue marks the entry unusable (no image ships for it).
NOT_AVAILABLE = "Not available on this box: {reason}."
NOT_SET_UP = "Not set up on this box — ask your admin."


def active_profiles(environ=None) -> set[str]:
    env = os.environ if environ is None else environ
    return {p.strip() for p in (env.get("COMPOSE_PROFILES") or "").split(",") if p.strip()}


def not_available(catalog, mcp_id: str) -> str:
    """#3427: the person-facing text for an entry the catalogue marks
    unusable, or ''."""
    ask = getattr(catalog, "unavailable_reason", None)
    reason = ask(mcp_id) if callable(ask) else ""
    if not isinstance(reason, str) or not reason:
        return ""
    return NOT_AVAILABLE.format(reason=reason.rstrip("."))


def engine_off(catalog, mcp_id: str, profiles=None) -> bool:
    """True iff the entry names an engine profile and that profile is off."""
    engine = catalog.engine_profile(mcp_id)
    if not engine:
        return False
    return engine not in (active_profiles() if profiles is None else set(profiles))


def oauth_gaps(catalog, flow, mcp_id: str) -> list[str]:
    """What the entry's OAuth provider still lacks ([] = set up, or no OAuth)."""
    if catalog.cred_model(mcp_id) != "oauth":
        return []
    provider = catalog.oauth_provider(mcp_id)
    if flow is None:
        return [f"provider {provider!r}"]
    if remote_mcp.spec_for(catalog.get(mcp_id)) is not None:
        # #3429: a remote MCP server registers the box itself on first use —
        # no app, no .env key; set up whenever the manager can register.
        return [] if getattr(flow, "remote", None) is not None else ["remote registration"]
    return flow.setup_gaps(provider)


def unavailable_reason(catalog, flow, mcp_id: str, profiles=None) -> str:
    """The person-facing reason a card cannot be connected, or ''. An engine
    that is off wins: setting up OAuth would not help. An entry the catalogue
    marks unusable wins over both (#3427)."""
    gone = not_available(catalog, mcp_id)
    if gone:
        return gone
    if engine_off(catalog, mcp_id, profiles):
        return ENGINE_OFF
    if oauth_gaps(catalog, flow, mcp_id):
        return NOT_SET_UP
    return ""


def admin_hint(catalog, flow, mcp_id: str, redirect_uri: str) -> str:
    """What an administrator does about it. Names .env keys, a profile and
    the redirect URI; never a value."""
    integ = catalog.get(mcp_id) or {}
    name = integ.get("display_name") or mcp_id
    if engine_off(catalog, mcp_id):
        return (f"{name} is not enabled on this box. An administrator enables the "
                f"'{catalog.engine_profile(mcp_id)}' module in the Settings portal "
                f"(Modules); then each person connects their own account here.")
    provider = catalog.oauth_provider(mcp_id)
    if remote_mcp.spec_for(integ) is not None:
        # #3429: nothing to register on the box; the provider's own admin may
        # have to allow this box's address for MCP clients.
        return (f"{name} could not be connected from this box. It needs no app on the box; "
                f"an administrator of the provider may have to allow {redirect_uri.rsplit('/oauth/', 1)[0]} "
                "for MCP clients, and the MCP manager must be running with its encryption key.")
    s = flow.settings_for(provider) if flow is not None else {
        "client_id_key": f"OAUTH_{str(provider).upper()}_CLIENT_ID",
        "client_secret_key": f"OAUTH_{str(provider).upper()}_CLIENT_SECRET", "url_vars": []}
    msg = (f"Connecting {name} is not set up on this box. An administrator registers an "
           f"OAuth app with the provider, with the redirect URI {redirect_uri}, and sets "
           f"{s['client_id_key']} and {s['client_secret_key']} in the box's .env")
    gaps = flow.setup_gaps(provider) if flow is not None else []
    if "auth_url" in gaps and s["url_vars"]:
        msg += f" (and {', '.join(s['url_vars'])}, which the sign-in address is built from)"
    return msg + ("; then the MCP manager is recreated so it reads them "
                  "(rzfz post-install --refresh).")
