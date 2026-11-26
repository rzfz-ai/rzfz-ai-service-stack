# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The MCP gateway (#2472): one entry point for Open WebUI, Dify and the agents.

POST|GET|DELETE /gw/<server_id>/mcp — streamable-http, relayed.

Order (every step fails closed): caller resolved from the credential alone
(else 401, upstream never dialled, and BEFORE the server id is looked at, so
an anonymous request cannot probe which servers exist) -> a relay slot within
the per-caller and total caps, taken before the body is read (else 429 / 503)
-> the directory, asked only with the slot held (B8 fix rounds 3/4): an Open
WebUI caller's email becomes a sub (or the caller stays unresolved), and a
person's token is honoured only while its owner is an active Authentik user
(else 401, audited; 503 when that cannot be checked) -> a body within
`max_body_bytes` (else 413) -> known server (else 404) -> server
eligible for that consumer and the active profiles (else 404) -> technical
caller bound to this server (else 403) -> one JSON-RPC message, parsed
strictly (else 400; the upstream receives its canonical re-serialisation,
never the client's bytes) -> a presented Mcp-Session-Id must be bound to this
caller (else 404) -> the caller's own upstream credential (else 403 "connect
first") -> the Gitea guard on `guard: gitea` (only the pinned gitea-mcp's
tools, arguments spelled as it reads them; #2554) -> write guard (a tool on
the server's `write_guard` list is checked by its per-tool rule, with the
caller's own credential; without a rule it is refused) -> the Microsoft 365
guard on `guard: m365` (#3433: tool class x caller class; send and delete in
chat only after the person's confirmation on mcp.<domain>/confirm, refused for
unattended callers unless enabled) -> audit (a request that cannot be audited
is not relayed) -> relay. Every refusal after the caller is known is audited; an anonymous
refusal is counted and rate-limit-logged, never written to the database.

A token this gateway issued that is presented after its revocation (or its
technical user's) is still refused with 401 at the first step, but audited
under its principal (`auth_rejected`, with consumer, caller and reason; at
most one row per token per minute, RejectionThrottle). A token that is not
ours names nobody: a row for it would record only what the sender chose and
would let any peer write to the database per request, so it stays counted
and rate-limit-logged only (#2472).

An OAuth grant the provider no longer accepts (#2554): an upstream 401/403
on a call made with the caller's OAuth access token, or — for gitea-mcp,
which wraps Gitea's 401 in a tool result — a tool error after which Gitea
itself refuses that credential, marks the grant (the portal says "authorize
again") and answers "connect it again" instead of the provider's raw text.
A PAT, an API key or a relay secret is never mapped: its 401 stays as is.

Headers: only an allow-list of MCP transport headers reaches the upstream.
The caller's Authorization and cookies, Origin, X-Authentik-*, X-Rzfz-*,
X-Dify-SSO-Token and X-MCP-Internal-Token never do; the upstream sees only
the credential we inject. A redirect from the upstream is never followed
and never relayed (Dify's MCP client follows redirects).

Not Caddy-anchored on purpose: it trusts no identity header, only a
credential it can verify (a hashed gateway token, an Open WebUI session
that Open WebUI itself vouches for, or — `system_oauth`, while Open WebUI
signs in through Authentik — the chat user's own Authentik access token,
checked with Authentik's public keys in owui_oidc; with that on, a session
bearer gets no personal credential). Either Open WebUI credential is accepted ONLY
when the TCP peer is Open WebUI's own container (design record §2.2): Caddy
(the public gw.<MCP_DOMAIN> route, Dify's SSRF proxy), the per-user proxies
and every other peer are resolved by `rzmcp_` tokens alone, so the gateway
never becomes an internet-facing check for Open WebUI tokens. Reached
in-network by Open WebUI / Dify (http://mcp-manager:5000/gw/…) and by agents
via https://gw.<MCP_DOMAIN>/gw/…"""
from __future__ import annotations

import itertools
import json
import logging
import os
import threading
import time

import httpx
# Bound at import: tests replace the module's `httpx` with a double, and an
# `except` needs the real classes.
from httpx import ConnectError as _ConnectError
from httpx import ConnectTimeout as _ConnectTimeout
from httpx import TimeoutException as _TimeoutException
from flask import Blueprint, Response, current_app, g, request, stream_with_context

from app.services import (docling_guard, gateway_credentials, gateway_identity, gateway_registry,
                          gateway_runtime, gitea_guard, m365_audit, m365_confirm, m365_guard, owui_oidc)

logger = logging.getLogger(__name__)
gateway_bp = Blueprint("gateway", __name__, url_prefix="/gw")

#: The container whose peer address may present an Open WebUI session.
OPENWEBUI_HOST = "openwebui"

# Allow-lists, not deny-lists: a header we did not name never crosses.
_FWD_REQ = {"content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id"}
_FWD_RESP = {"content-type", "mcp-session-id", "mcp-protocol-version", "cache-control"}

# ONE pooled client for every relay. httpx.Client is thread-safe, and building
# one costs ~0.1 s of CPU (its TLS context): a client per request serialised
# concurrent gateway calls on the GIL — measured, 20 parallel calls took up to
# 8 s (test_2472_gateway_worker_does_not_block.py). Its cookie jar is a
# NoCookieJar that never stores or sends a cookie (review C1: the default jar
# carried caller A's upstream session cookie on caller B's request), so the
# only credential on a relayed request is the header we inject. Set-Cookie
# never reaches the client either: it is not on _FWD_RESP.
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(follow_redirects=False, cookies=gateway_runtime.NoCookieJar(),
                                   limits=httpx.Limits(max_connections=256,
                                                       max_keepalive_connections=64))
        return _CLIENT


class _Runtime:
    """The per-app gateway state: caps, session bindings, anonymous counter."""

    def __init__(self, limits):
        self.limits = limits
        self.limiter = gateway_runtime.Limiter(limits.per_caller, limits.total)
        self.sessions = gateway_runtime.SessionBindings()
        self.anonymous = gateway_runtime.AnonymousLog(log=logger)
        self.rejections = gateway_runtime.RejectionThrottle()


_RUNTIME_LOCK = threading.Lock()


def runtime(app=None) -> _Runtime:
    app = app or current_app
    with _RUNTIME_LOCK:
        rt = app.extensions.get("rzfz_gateway")
        if rt is None:
            limits = getattr(app, "gateway_limits", None) or gateway_registry.GatewayLimits()
            rt = app.extensions["rzfz_gateway"] = _Runtime(limits)
        return rt


def _caller_key(caller) -> str:
    """Who holds a slot or a session: the principal, or for an unresolved Open
    WebUI user their Open WebUI identity (they have no principal), or the
    sub of their verified Authentik token (system_oauth)."""
    if caller.principal:
        return caller.principal
    if getattr(caller, "oidc_sub", ""):
        return f"owui-oidc:{caller.oidc_sub}"
    return f"owui:{getattr(caller, 'owui_id', '') or caller.display}"


#: Per-guard, per-tool write rules: `guard -> {tool -> rule(caller, args,
#: fetch) -> refusal | None}`, where `fetch(path) -> (status, json)` reads the
#: upstream's own API with the CALLER's injected credential (_lookup).
#: Design record §9 (ruling C1): gitea-mcp's per-tool target-branch rules
#: (#2554, app/services/gitea_guard.py). A tool on a server's `write_guard`
#: list with NO rule for that server's guard is refused — the guard fails
#: closed until its rule exists.
WRITE_RULES: dict = {"gitea": gitea_guard.WRITE_RULES}


def _err(status: int, code: int, message: str, rid=None) -> Response:
    return Response(json.dumps({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": code, "message": message}}),
                    status=status, mimetype="application/json")


def _tool_error(rid, text: str) -> Response:
    """A tool result the model reads (HTTP 200, `isError: true`)."""
    return Response(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
        "content": [{"type": "text", "text": text}], "isError": True}}),
        status=200, mimetype="application/json")


def _reconnect(method, rid, message: str) -> Response:
    """#2554: "connect it again" — as a tool result on a tool call, so the
    chat model can tell the user; as a JSON-RPC error on anything else."""
    if method == "tools/call":
        return _tool_error(rid, message)
    return _err(403, -32002, message, rid)


def _active_profiles() -> list:
    return [p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p.strip()]


def _from_openwebui() -> bool:
    """True iff the TCP peer is Open WebUI's own container. No env bypass."""
    from razzfazz_common.proxy_anchor import from_trusted_proxy
    return from_trusted_proxy(hosts=(OPENWEBUI_HOST,))


def _resolve_caller():
    """The caller, from the credential alone; the server id plays no part.
    An rzmcp_ token is ours; any other bearer can only be an Open WebUI
    session, which Open WebUI itself must vouch for, and only when Open WebUI
    is the peer. Such a caller has consumer 'openwebui', so `eligible`
    refuses it on a server that does not list Open WebUI."""
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    if token.startswith(gateway_identity.TOKEN_PREFIX):
        # The owner check (a directory listing) runs in relay() AFTER the
        # caller holds a relay slot, so the caps bound it (B8 fix round 3).
        return gateway_identity.resolve_bearer(
            current_app.db, token, directory=gateway_identity.DEFER_OWNER_CHECK)
    if not _from_openwebui():
        return None
    # system_oauth: the chat user's own Authentik access token, checked
    # against Authentik's public keys (owui_oidc). Only when Open WebUI signs
    # in through Authentik; a token that fails any check is nobody. The
    # active-person check runs in relay() with the slot held.
    verifier = getattr(current_app, "owui_oidc", None)
    if verifier is not None and owui_oidc.looks_like_authentik_token(token):
        return gateway_identity.resolve_owui_oidc(token, verifier)
    # Open WebUI says whose session it is (an email); the directory lookup
    # that turns it into a sub runs in relay() AFTER the caller holds a relay
    # slot (B8 fix round 4), like the owner check of a person's token.
    return gateway_identity.resolve_owui_session(
        current_app.db, token, directory=getattr(current_app, "authentik_directory", None) or False,
        defer_lookup=True)


#: Who an Open WebUI call is, for its audit row, before Open WebUI said so
#: (the session check was refused as busy).
_OWUI_UNIDENTIFIED = gateway_identity.Caller("", "user", "openwebui", "", "")


def _audit_principal(caller):
    """(principal column, what it is). A person's sub or `~svc-<name>` as
    is. An Open WebUI caller whose sub is not (yet) known — unresolved, or
    refused before the lookup (429/503 over a cap) — is recorded under its
    Open WebUI identity, labelled as such and never empty: `owui:<id>`,
    `owui-email:<email>` when Open WebUI gave no id, `owui:unidentified`
    when Open WebUI was not asked at all (B8 fix round 5)."""
    if caller.principal:
        return caller.principal, "sub" if caller.kind == "user" else "technical"
    oidc_sub = (getattr(caller, "oidc_sub", "") or "").strip()
    if oidc_sub:
        # A verified Authentik token whose sub is not an active person.
        return f"owui-oidc:{oidc_sub}"[:128], "authentik_sub_not_active"
    owui_id = (getattr(caller, "owui_id", "") or "").strip()
    if owui_id:
        return f"owui:{owui_id}"[:128], "openwebui_user_id"
    if caller.display:
        return f"owui-email:{caller.display}"[:128], "openwebui_email"
    return "owui:unidentified", "openwebui_unidentified"


def _presented_token() -> str:
    auth = request.headers.get("Authorization") or ""
    return auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""


def _audit_rejection(rt, server_id) -> None:
    """#2472: a refused `rzmcp_` token that this gateway issued is audited
    under its principal — consumer, caller, reason, the token's row id —
    never the token or its hash. Throttled per token (RejectionThrottle).
    Never raises: the answer stays 401 whatever happens here."""
    try:
        rej = gateway_identity.rejected_token(current_app.db, _presented_token())
        if rej is None:
            rt.anonymous.refused(request.remote_addr or "?", "no valid credential")
            return
        suppressed = rt.rejections.admit(rej["token_id"])
        if suppressed is None:
            return
        current_app.db.log_audit(rej["principal"], "gw_request", str(server_id)[:48], {
            "method": request.method, "consumer": rej["consumer"], "kind": rej["kind"],
            "caller": rej["display"], "outcome": "auth_rejected", "reason": rej["reason"],
            "token_id": rej["token_id"], "suppressed_since_last": suppressed})
    except Exception:  # noqa: BLE001
        logger.exception("gateway: auditing a rejected token failed")


def _audit(caller, server_id, method, tool, outcome, extra=None) -> bool:
    """One mcp_audit_log row (design record §7, D12): caller kind, principal,
    consumer, server, tool (or method) and outcome — never a credential and
    never the tool's arguments (`extra`: a guard's own fields, #3433 — object
    ids and classes, never content). False when the row could not be written."""
    srv = (getattr(current_app, "gateway_servers", None) or {}).get(server_id)
    if tool and getattr(srv, "guard", "") == "m365":
        # #3477 (A2-04): an unknown tool name is the caller's free text.
        tool = m365_guard.audit_tool_name(tool)
    details = {"tool": tool} if tool else {"method": method or request.method}
    details.update(extra or {})
    principal, principal_is = _audit_principal(caller)
    details.update({"consumer": caller.consumer, "kind": caller.kind,
                    "caller": caller.display, "outcome": outcome})
    if caller.consumer == "openwebui":
        # The user id: Open WebUI's own on the session path; on system_oauth
        # Open WebUI is not asked, and the id is the verified token's sub
        # (Open WebUI keys an Authentik account by it). The e-mail is an
        # address or "", never the username `caller` carries.
        owui_id = getattr(caller, "owui_id", "") or ""
        oidc_sub = getattr(caller, "oidc_sub", "") or ""
        details.update({"owui_user_id": owui_id or oidc_sub,
                        "owui_user_id_is": ("openwebui_id" if owui_id
                                            else "authentik_sub" if oidc_sub else ""),
                        "owui_email": getattr(caller, "email", "") or "",
                        "principal_is": principal_is,
                        "owui_auth": "system_oauth" if oidc_sub else "session"})
    try:
        current_app.db.log_audit(principal,
                                 "gw_tool_call" if tool else "gw_request", str(server_id)[:48], details)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("gateway: audit write failed for %s", server_id)
        return False


def _write_refusal(server, caller, tool, args, up):
    if not tool or tool not in server.write_guard:
        return None
    rule = WRITE_RULES.get(server.guard, {}).get(tool)
    if rule is None:
        return f"{tool}: writes through the gateway are not enabled for this tool"
    return rule(caller, args if isinstance(args, dict) else {}, _lookup(up))


#: #3433: Microsoft Graph, for the guard's reads of the object a call acts on
#: (never configurable; the caller's own token, like the call itself).
GRAPH_API = "https://graph.microsoft.com/v1.0"
GRAPH_LOOKUP_BYTES = 2 * 1024 * 1024


def _m365_gate(caller, server, rid, tool, args, up):  # noqa: C901 — one linear decision
    """#3433: the Microsoft 365 guard (app/services/m365_guard.py). None =
    go on (a read, a write, an allowed or a confirmed send or delete);
    otherwise the answer, as a tool result the model can relay. A call that
    is not a read is audited twice, whatever the settings: the mcp_audit_log
    row (the gate: no row, no relay) and the Wazuh-collected audit file. A
    relayed change holds an InFlight slot until its response closes
    (`g.rzfz_m365_release`, run by relay())."""
    settings = m365_guard.settings()
    cls = m365_guard.classify(tool, args)
    fetch = None
    path = m365_guard.event_lookup(tool, args, cls)
    if path is not None:
        fetch = fetch or _lookup(up, base=GRAPH_API, limit=GRAPH_LOOKUP_BYTES)
        status, data = fetch(path) if path else (0, None)
        cls = m365_guard.settle_event(cls, status, data)
    if cls == m365_guard.READ:
        return None
    who = m365_guard.caller_class(caller)
    fields = m365_guard.audit_fields(tool, args)

    def audited(decision):
        extra = {"tool_class": cls, "caller_class": who, "m365_mode": settings.mode}
        extra.update(fields)
        ok = _audit(caller, server.id, "tools/call", tool, "m365_" + decision, extra)
        m365_audit.write(caller.principal or _audit_principal(caller)[0], m365_guard.audit_tool_name(tool),
                         cls, who, decision, dict(fields, consumer=caller.consumer))
        return ok

    decision = m365_guard.decide(settings, cls, who, tool)
    if decision.action == m365_guard.REFUSE:
        audited("refused")
        return _tool_error(rid, decision.reason)
    domain = current_app.config.get("MCP_DOMAIN", "")
    if m365_guard.grant_missing(tool, cls, m365_guard.granted_scopes(up.headers)):
        audited("reconnect_required")
        text = (m365_guard.RECONNECT_SEND if tool in m365_guard.MAIL_SEND_TOOLS
                else m365_guard.RECONNECT_WRITE)
        return _tool_error(rid, text.format(domain=domain))

    draft = draft_atts = content = None
    alone = False
    if decision.action == m365_guard.CONFIRM and tool == m365_guard.DRAFT_SEND_TOOL:
        # The draft is what goes out: its confirmation is bound to its content.
        paths = m365_guard.draft_paths(args)
        fetch = fetch or _lookup(up, base=GRAPH_API, limit=GRAPH_LOOKUP_BYTES)
        if paths:
            st1, draft = fetch(paths[0])
            st2, draft_atts = fetch(paths[1])
            if st1 == 200 and st2 == 200:
                content = m365_guard.draft_digest(draft, draft_atts)
        if content is None:
            audited("draft_unreadable")
            return _tool_error(rid, "The draft could not be read, so it cannot be confirmed; nothing was "
                                    "sent. Check that it exists, or send the mail with send-mail instead.")
        alone = True

    release = m365_confirm.in_flight(current_app).begin(caller.principal or _caller_key(caller), alone=alone)
    if release is None:
        audited("busy")
        return _tool_error(rid, "Another Microsoft 365 change of this person is still running; nothing "
                                "was done. Try again in a moment.")
    try:
        if decision.action == m365_guard.CONFIRM:
            approvals = m365_confirm.store(current_app)
            digest = m365_guard.call_hash(caller.principal, who, tool, args)
            got = approvals.consume(caller.principal, who, digest, content)
            if got == m365_confirm.OK:
                if not audited("confirmed"):
                    release()
                    return _err(503, -32603, "audit unavailable; nothing was relayed", rid)
                g.rzfz_m365_release = release
                return None
            release()
            if fetch is None:
                fetch = _lookup(up, base=GRAPH_API, limit=GRAPH_LOOKUP_BYTES)
            targets = [(label, *fetch(p)) for label, p in m365_guard.target_lookups(tool, args)]
            view = m365_guard.preview(tool, args, targets, draft=draft, draft_atts=draft_atts)
            aid = approvals.request(caller.principal, who, tool, cls, digest, view, content)
            if aid is None:
                audited("confirmation_limit")
                return _tool_error(rid, "Too many Microsoft 365 confirmations are waiting for this person; "
                                        "confirm or let them expire (10 minutes), then try again.")
            audited("confirmation_changed" if got == m365_confirm.CHANGED else "confirmation_required")
            verb = "send or share" if cls == m365_guard.SEND else "delete"
            first = ("The draft changed after it was confirmed, so nothing was sent. "
                     if got == m365_confirm.CHANGED else "Nothing was done yet: ")
            return _tool_error(rid, (
                f"{first}{tool} would {verb} data in the person's Microsoft 365 account, "
                f"and that needs the person's own confirmation. Show the person this link: "
                f"https://{domain}/confirm/{aid} — they check the details there and press Confirm. "
                f"Then call {tool} again with exactly the same arguments (within 10 minutes). "
                f"Changed arguments need a new confirmation."))
        if not audited("allowed"):
            release()
            return _err(503, -32603, "audit unavailable; nothing was relayed", rid)
        g.rzfz_m365_release = release
        return None
    except BaseException:
        release()
        raise


def _lookup(up, base=None, limit=None):
    """`fetch(path) -> (status, json | None)` against the in-stack Gitea API
    (gitea_guard.GITEA_API) or, for the Microsoft 365 guard, Microsoft Graph
    (GRAPH_API) — fixed bases, never configurable — with the credential injected
    for THIS caller — the caller's own grant, never a server token. Bounded
    like any upstream call (timeouts, MAX_LOOKUP_BYTES), never redirected,
    and run inside the caller's relay slot, so it needs no cap of its own
    (thread_budget.py). A transport failure, an oversized or unreadable
    answer is status 0, which every rule refuses (fail closed)."""
    headers = {"Accept": "application/json"}
    auth = (up.headers or {}).get("Authorization")
    if auth:
        headers["Authorization"] = auth

    def fetch(path):
        try:
            with _client().stream("GET", (base or gitea_guard.GITEA_API) + path, headers=headers,
                                  timeout=httpx.Timeout(10, connect=5, write=10, pool=10),
                                  follow_redirects=False) as resp:
                status = int(resp.status_code)
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > (limit or gitea_guard.MAX_LOOKUP_BYTES):
                        return 0, None
        except Exception as e:  # noqa: BLE001 — no lookup, no write
            logger.info("gateway: lookup failed: %s", type(e).__name__)
            return 0, None
        if status != 200:
            return status, None
        try:
            return status, json.loads(bytes(buf) or b"null")
        except ValueError:
            return status, None
    return fetch


@gateway_bp.route("/<server_id>/mcp", methods=["GET", "POST", "DELETE"])
def relay(server_id):  # noqa: C901 — one linear gate sequence, kept together
    rt = runtime()
    try:
        caller = _resolve_caller()
    except gateway_identity.OpenWebUIBusy:
        # Open WebUI's own peer, and already OWUI_CONCURRENT_CHECKS session
        # checks running (Open WebUI slow or hanging): refused at once, so
        # the checks can never hold more than that many worker threads.
        _audit(_OWUI_UNIDENTIFIED, server_id, None, None, "openwebui_check_busy")
        return _err(503, -32005, "the gateway is busy; retry shortly")
    except Exception:  # noqa: BLE001 — a failed lookup is no caller, never a guess
        logger.exception("gateway: caller resolution failed")
        return _err(503, -32603, "gateway unavailable")
    if caller is None:
        # No database row per anonymous request (a flooding vector): a counter
        # and at most one log line per peer per minute (accepted deviation
        # from design record §7, fix round 1). A token we issued and then
        # revoked is not anonymous: _audit_rejection records it (throttled).
        _audit_rejection(rt, server_id)
        return _err(401, -32001, "unauthorized")
    key = _caller_key(caller)

    # The relay slot is taken BEFORE anything else — before the body is read
    # and before any lookup (review I2, fix round 2): a caller trickling
    # request bodies holds at most `per_caller` threads, and the gateway at
    # most `total`, whatever the request turns out to be.
    lease, over = rt.limiter.acquire(key)
    if lease is None:
        if over == "per_caller":
            _audit(caller, server_id, None, None, "over_caller_limit")
            return _err(429, -32005, "too many concurrent requests for this caller")
        _audit(caller, server_id, None, None, "over_gateway_limit")
        return _err(503, -32005, "the gateway is busy; retry shortly")
    try:
        # Everything that asks the directory runs here, with the slot held.
        # An Open WebUI caller holds its slot under its Open WebUI identity
        # (the sub is not known yet); once resolved, sessions and document
        # keys are bound to the sub, as before.
        caller = gateway_identity.resolve_owui_email(
            caller, getattr(current_app, "authentik_directory", None))
        key = _caller_key(caller)
        refused = _owner_refusal(caller, server_id)
        if refused is not None:
            lease.release()
            return refused
        response = _admitted(rt, caller, key, lease, server_id)
    except BaseException:
        lease.release()
        _m365_release_now()
        raise
    if not getattr(response, "_rzfz_holds_lease", False):
        lease.release()
    m365_release = g.pop("rzfz_m365_release", None)
    if m365_release is not None:
        # #3433: a relayed Microsoft 365 change is in flight until its
        # response is closed (streamed to its end, or abandoned).
        response.call_on_close(m365_release)
    return response


def _m365_release_now():
    release = g.pop("rzfz_m365_release", None)
    if release is not None:
        release()


def _owner_refusal(caller, server_id):
    """A person's token is honoured only while its owner is an active
    Authentik user. Checked here, with the relay slot held: a directory that
    hangs then ties up at most the caps' threads, never the worker."""
    try:
        gateway_identity.check_owner(current_app.db, caller,
                                     getattr(current_app, "authentik_directory", None),
                                     token_id=getattr(caller, "token_id", None),
                                     cred_store=getattr(current_app, "cred_store", None))
    except gateway_identity.OwnerInactive as e:
        # A real token whose owner left: refused and audited (the tokens are
        # already revoked, so this is the last such row).
        _audit(e.caller, server_id, None, None, "owner_inactive")
        return _err(401, -32001, "unauthorized")
    except gateway_identity.OwnerCheckUnavailable as e:
        _audit(e.caller, server_id, None, None, "owner_check_unavailable")
        return _err(503, -32603, "the gateway cannot check the caller right now; try again shortly")
    except Exception:  # noqa: BLE001 — e.g. the database failed revoking or touching the token
        # Never a raw 500 and never let through: refused like an unavailable
        # check, and audited (if the audit can be written at all).
        logger.exception("gateway: the owner check failed")
        _audit(caller, server_id, None, None, "owner_check_failed")
        return _err(503, -32603, "the gateway cannot check the caller right now; try again shortly")
    return None


def _read_body(limit: int):
    """The request body, or None when it is over `limit`. An announced
    Content-Length over the limit is refused unread; a body without one
    (chunked) is read in pieces and refused as soon as it passes the limit —
    never silently truncated into something that might still parse."""
    if request.content_length is not None:
        if request.content_length > limit:
            return None
        return request.get_data()
    buf = bytearray()
    while True:
        chunk = request.stream.read(65536)
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            return None


def _admitted(rt, caller, key, lease, server_id):  # noqa: C901 — one linear gate sequence
    """Everything after the caller holds a slot. A response that streams
    from the upstream carries the lease (`_rzfz_holds_lease`) and returns it
    when it closes; every other response returns it in relay()."""
    rid, method, tool, args, content, bad = None, None, None, {}, None, None
    if request.method == "POST":
        body = _read_body(rt.limits.max_body_bytes)
        if body is None:
            _audit(caller, server_id, None, None, "too_large")
            return _err(413, -32600, f"request body over {rt.limits.max_body_bytes} bytes")
        try:
            msg, method, tool, args = gateway_runtime.parse_message(body)
            rid = msg.get("id")
            content = gateway_runtime.canonical(msg)
        except gateway_runtime.BadMessage as e:
            bad = str(e)

    server = current_app.gateway_servers.get(server_id)
    if server is None or not gateway_registry.eligible(
            server, caller.consumer, _active_profiles(),
            os.environ.get("RAZZFAZZ_NETWORK_MODE", "online")):
        _audit(caller, server_id, method, tool, "unknown_server")
        return _err(404, -32004, "unknown MCP server")
    if caller.kind == "technical" and not current_app.db.technical_bound(
            caller.principal[len(gateway_identity.TECH_PREFIX):], server.id):
        _audit(caller, server.id, method, tool, "not_bound")
        return _err(403, -32003, f"{caller.display} is not bound to {server.id}")
    if bad is not None:
        _audit(caller, server.id, method, tool, "bad_request")
        return _err(400, -32600, f"invalid JSON-RPC message: {bad}")
    if server.guard == "docling" and request.method == "GET":
        # A guarded server answers only the caller's own requests, read whole
        # and rebuilt: there is no server-initiated stream to relay (405 is
        # the MCP spec's "no SSE stream offered here").
        _audit(caller, server.id, method, tool, "method_not_allowed")
        resp = _err(405, -32600, "no event stream on this server")
        resp.headers["Allow"] = "POST, DELETE"
        return resp

    session_id = request.headers.get("Mcp-Session-Id")
    if session_id and not rt.sessions.owner_ok(server.id, session_id, key):
        _audit(caller, server.id, method, tool, "session_refused")
        return _err(404, -32001, "session not found", rid)

    try:
        up = gateway_credentials.upstream_for(
            server, caller, db=current_app.db, cred_store=current_app.cred_store,
            catalog=current_app.catalog, oauth_flow=getattr(current_app, "oauth_flow", None),
            mcp_domain=current_app.config.get("MCP_DOMAIN", ""),
            directory=getattr(current_app, "authentik_directory", None))
    except gateway_credentials.GrantRejected as e:
        # #2554: the provider rejected this grant before; nothing is dialled
        # until the user authorizes again.
        _audit(caller, server.id, method, tool, "reauth_required")
        return _reconnect(method, rid, str(e))
    except gateway_credentials.Unavailable as e:
        # B9: the directory (D6 knowledge scope) or the governance row could
        # not be read: nothing is decided, nothing is dialled (fail closed).
        _audit(caller, server.id, method, tool, "access_check_unavailable")
        return _err(503, -32603, str(e), rid)
    except gateway_credentials.GovernanceRefused as e:
        # #3477: the Settings page's availability / minimum tier stops this
        # entry for this caller. Nothing is decrypted or dialled.
        _audit(caller, server.id, method, tool, "governance_refused")
        return _err(403, -32003, str(e), rid)
    except gateway_credentials.NotConnected as e:
        _audit(caller, server.id, method, tool, "not_connected")
        return _err(403, -32002, str(e), rid)
    except gateway_credentials.NotConfigured as e:
        _audit(caller, server.id, method, tool, "upstream_not_configured")
        return _err(503, -32603, str(e), rid)

    if server.guard == "gitea" and method == "tools/call":
        # #2554: only the pinned gitea-mcp's own tools, arguments spelled
        # exactly as it reads them (gitea_guard.check_call).
        refusal = gitea_guard.check_call(tool, args)
        if refusal:
            _audit(caller, server.id, method, tool, "tool_refused")
            return _err(200, -32602, refusal, rid)
    refusal = _write_refusal(server, caller, tool, args, up)
    if refusal:
        _audit(caller, server.id, method, tool, "write_refused")
        return _err(200, -32602, refusal, rid)
    if server.guard == "m365" and method == "tools/call":
        gated = _m365_gate(caller, server, rid, tool, args, up)
        if gated is not None:
            return gated
    if server.guard == "docling" and request.method == "POST":
        refusal = docling_guard.check_request(
            method, tool, args, bound=lambda k: _doc_key_bound(server.id, key, k))
        if refusal:
            _audit(caller, server.id, method, tool, "tool_refused")
            return _err(200, -32602, refusal, rid)

    upstream = None
    try:
        if not _audit(caller, server.id, method, tool, "relayed"):
            return _err(503, -32603, "audit unavailable; nothing was relayed", rid)

        headers = {k: v for k, v in request.headers if k.lower() in _FWD_REQ}
        headers.update(up.headers)
        if up.host:
            headers["Host"] = up.host
        url = up.url + (("?" + request.query_string.decode()) if request.query_string else "")
        # A GET is the long-lived SSE stream: it may stay open, but not silent
        # for longer than the idle timeout (clients resume with Last-Event-ID).
        # Everything else is bounded by the server's registry timeout.
        read = rt.limits.stream_idle_timeout if request.method == "GET" else server.timeout
        try:
            upstream = _client().stream(request.method, url, headers=headers, content=content,
                                        timeout=httpx.Timeout(read, connect=10, write=60, pool=60),
                                        follow_redirects=False)
            resp = upstream.__enter__()
        except Exception as e:  # noqa: BLE001
            upstream = None
            lease.release()
            logger.warning("gateway: upstream %s failed: %s", server.id, type(e).__name__)
            if isinstance(e, (_ConnectError, _ConnectTimeout)):
                # Nothing answered the dial (for Docling: the docling-egress
                # sidecar is not running): say so plainly.
                _audit(caller, server.id, method, tool, "upstream_unavailable")
                return _err(503, -32603, unavailable_message(server), rid)
            if isinstance(e, _TimeoutException):
                # It answered the dial but not in time: that is a timeout, not
                # a service that is down.
                _audit(caller, server.id, method, tool, "upstream_timeout")
                return _err(504, -32603, timeout_message(server), rid)
            _audit(caller, server.id, method, tool, "upstream_unavailable")
            return _err(502, -32603, "upstream unavailable", rid)

        if 300 <= int(resp.status_code) < 400:
            _close(upstream, lease)
            _audit(caller, server.id, method, tool, "upstream_redirect_refused")
            return _err(502, -32603, "upstream answered a redirect; not followed", rid)

        chunks = resp.iter_bytes()
        head = []
        if up.oauth_ref:
            dead, head = _grant_dead(server, up, method, resp, chunks)
            if dead:
                _close(upstream, lease)
                return _grant_rejected(caller, server, up, method, tool, rid, dead)

        issued = resp.headers.get("mcp-session-id")
        if issued and 200 <= int(resp.status_code) < 300:
            rt.sessions.bind(server.id, issued, key)
        if request.method == "DELETE" and session_id and 200 <= int(resp.status_code) < 300:
            rt.sessions.unbind(server.id, session_id, key)
        if server.guard == "docling":
            return _guarded(caller, server, key, method, tool, rid, resp, upstream, lease)

        still_allowed = _stream_revocation_check(caller, server)

        def _relay():
            try:
                for chunk in itertools.chain(head, chunks):
                    if not still_allowed():
                        logger.info("gateway: stream from %s ended: %s was revoked or unbound",
                                    server.id, caller.display)
                        return
                    yield chunk
            except httpx.HTTPError as e:
                # An idle stream past its timeout, or an upstream that went
                # away: end this response; the client reconnects.
                logger.info("gateway: stream from %s ended: %s", server.id, type(e).__name__)
            finally:
                _close(upstream, lease)

        out = [(k, v) for k, v in resp.headers.multi_items() if k.lower() in _FWD_RESP]
        response = Response(stream_with_context(_relay()), status=resp.status_code, headers=out)
        # A response that is never iterated still returns its slot and connection.
        response.call_on_close(lambda: _close(upstream, lease))
        response._rzfz_holds_lease = True
        return response
    except Exception:
        _close(upstream, lease)
        raise


def _peek(chunks, limit: int):
    """(chunks read, complete): read until the body ends or passes `limit`.
    A transport error ends the peek as incomplete (the relay then ends)."""
    head, n = [], 0
    try:
        for chunk in chunks:
            head.append(chunk)
            n += len(chunk)
            if n > limit:
                return head, False
    except httpx.HTTPError:
        return head, False
    return head, True


def _grant_dead(server, up, method, resp, chunks):
    """(reason or "", chunks already read). Only for a call made with the
    caller's OAuth access token (`up.oauth_ref`, #2554):
      * the upstream answered 401/403 -> the grant is dead;
      * gitea-mcp answered a tools/call with a tool error -> Gitea is asked
        who this credential is (gitea_guard.GRANT_PROBE_PATH, inside the
        caller's relay slot); its 401/403 -> the grant is dead. Any other
        answer, or no answer, leaves the error as gitea-mcp wrote it."""
    status = int(resp.status_code)
    if status in (401, 403):
        return f"upstream_http_{status}", []
    if server.guard != "gitea" or method != "tools/call" or not 200 <= status < 300:
        return "", []
    head, complete = _peek(chunks, gitea_guard.GRANT_PEEK_BYTES)
    if complete and gitea_guard.is_tool_error(resp.headers.get("content-type", ""), b"".join(head)):
        probe, _ = _lookup(up)(gitea_guard.GRANT_PROBE_PATH)
        if probe in (401, 403):
            return f"gitea_credential_{probe}", head
    return "", head


def _grant_rejected(caller, server, up, method, tool, rid, reason):
    """Mark the caller's grant (the portal then says "authorize again"),
    audit, and answer "connect it again" — never the provider's raw text."""
    try:
        current_app.cred_store.mark_reauth_required(caller.principal, up.oauth_ref, reason)
    except Exception:  # noqa: BLE001 — the answer is the same; the next call re-checks
        logger.exception("gateway: marking the %s grant failed", up.oauth_ref)
    _audit(caller, server.id, method, tool, "grant_rejected")
    return _reconnect(method, rid, gateway_credentials.reconnect_message(
        up.oauth_ref, current_app.config.get("MCP_DOMAIN", "")))


#: How often an open stream of a technical caller re-reads the mirror (#2472
#: review): a revoke or an unbind ends the stream at its next chunk after this.
STREAM_REVOCATION_CHECK_SECONDS = 5.0


def _stream_revocation_check(caller, server):
    """A callable that says whether a relayed stream may go on. Every POST is
    resolved anew, so only a long-lived stream needs this: a technical
    caller's stream ends once its mirror row is revoked or no longer bound to
    the server (checked at most every STREAM_REVOCATION_CHECK_SECONDS, as
    chunks arrive; a silent stream ends at the idle timeout). A read that
    fails ends the stream too (fail closed; the client reconnects)."""
    if caller.kind != "technical":
        return lambda: True
    db = current_app.db
    name = caller.principal[len(gateway_identity.TECH_PREFIX):]
    state = {"at": time.monotonic()}

    def check():
        now = time.monotonic()
        if now - state["at"] < STREAM_REVOCATION_CHECK_SECONDS:
            return True
        state["at"] = now
        try:
            return bool(db.technical_bound(name, server.id))
        except Exception:  # noqa: BLE001
            logger.warning("gateway: revocation check for a stream failed; ending it")
            return False
    return check


def unavailable_message(server) -> str:
    return (f"{server.description or server.id} is not available right now "
            "(its service on this box is not running); try again later.")


def timeout_message(server) -> str:
    return (f"{server.description or server.id} timed out: it did not answer in time; "
            "try again, or with a smaller document.")


def _doc_key_bound(server_id, caller_key, doc_key) -> bool:
    """Whether the Docling key was returned to this caller; any failure is
    False (the export is refused, never relayed on a guess)."""
    try:
        return bool(current_app.db.doc_key_bound(server_id, caller_key, doc_key))
    except Exception:  # noqa: BLE001
        logger.exception("gateway: document-key lookup failed for %s", server_id)
        return False


def _guarded(caller, server, key, method, tool, rid, resp, upstream, lease):
    """A guarded upstream's answer, read whole (bounded), rebuilt by its guard
    and returned as one JSON message; the slot and connection are returned
    here. Nothing the upstream wrote reaches the caller unchecked."""
    buf, status = bytearray(), int(resp.status_code)
    try:
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > docling_guard.MAX_RESPONSE_BYTES:
                status = 500   # a generic failure, not "the service is down"
                break
    except _TimeoutException as e:
        logger.info("gateway: guarded read from %s timed out: %s", server.id, type(e).__name__)
        status = 504
    except httpx.HTTPError as e:
        logger.info("gateway: guarded read from %s failed: %s", server.id, type(e).__name__)
        status = 500   # a generic failure: the service answered, then broke off
    finally:
        _close(upstream, lease)
    keep = [(k, v) for k, v in resp.headers.multi_items()
            if k.lower() in ("mcp-session-id", "mcp-protocol-version")]
    if request.method == "DELETE":
        return Response(b"", status=status if 200 <= status < 300 or status == 404 else 502,
                        headers=keep)
    out_status, body, keys = docling_guard.sanitize(
        method, tool, rid, status, resp.headers.get("content-type", ""), bytes(buf),
        unavailable=unavailable_message(server), timed_out=timeout_message(server))
    # The `relayed` row was written before the dial; a call that then timed
    # out or found the service down gets its own row saying so.
    if out_status == 504:
        _audit(caller, server.id, method, tool, "upstream_timeout")
    elif out_status == 503:
        _audit(caller, server.id, method, tool, "upstream_unavailable")
    for doc_key in keys:
        try:
            current_app.db.bind_doc_key(server.id, key, doc_key)
        except Exception:  # noqa: BLE001 — the export is then refused, never leaked
            logger.exception("gateway: binding a document key failed for %s", server.id)
    if not body:
        return Response(b"", status=out_status, headers=keep)
    return Response(body, status=out_status, headers=keep, mimetype="application/json")


def _close(upstream, lease):
    """Return the upstream connection and the relay slot; safe to call twice."""
    try:
        if upstream is not None and not getattr(upstream, "_rzfz_closed", False):
            upstream._rzfz_closed = True
            upstream.__exit__(None, None, None)
    except Exception:  # noqa: BLE001
        logger.debug("gateway: closing the upstream stream failed", exc_info=True)
    finally:
        lease.release()
