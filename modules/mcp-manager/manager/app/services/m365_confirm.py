# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Per-call confirmations for Microsoft 365 send and delete in chat (#3433),
active while M365_MCP_CONFIRM_SEND_DELETE is true.

Why not the model's word: ms365-mcp has its own `confirm: true` argument
(MS365_MCP_REQUIRE_CONFIRM), but the model fills in the arguments, so it can
"confirm" by itself. MCP elicitation asks the CLIENT, and the client here is
Open WebUI, an agent or a workflow: whether a person or the client itself
answers is up to that client, so the gateway cannot tell a person's yes from
an automated one. A token handed back in the tool result would be read by the
model and replayed. So the confirmation travels on a channel the model has no
access to: the person's own browser session.

The flow:
  1. A send or delete in chat arrives at the gateway. Nothing is relayed: an
     approval is created (bound to the person's sub, the caller class, the
     tool and the sha256 of the exact arguments; for send-draft-message also
     to the sha256 of the draft's recipients, subject, body and attachments,
     read from Graph) and the tool result tells the model to show the person
     a link: https://mcp.<domain>/confirm/<id>.
  2. The person opens the link. mcp.<domain> sits behind Authentik
     forward-auth, and the page answers only the person the approval belongs
     to (X-Authentik-Uid, Caddy-anchored). It shows who the call reaches, in
     full and first, then the subject, the attachments, the full text, the
     message/event/file it acts on, and every argument; and a Confirm button
     (POST, a per-approval CSRF token, same origin only).
  3. The person (or the model, asked to) repeats the call with the SAME
     arguments. The gateway finds the approved approval for exactly that
     person, caller class, tool and argument hash, re-reads a draft and
     compares its content, consumes the approval and relays once. A changed
     argument or a changed draft needs a new confirmation.

Approvals live in this process only (mcp-manager runs ONE gunicorn process,
gunicorn.conf.py): never in the database or a log, so the preview (which
carries the message the person is about to send) is gone after use, expiry
or a restart. Pending and approved approvals expire after TTL_SECONDS.

`InFlight` closes the window between re-reading a draft and sending it: a
confirmed draft send runs only while no other Microsoft 365 change of the
same person is in flight through the gateway, and none starts until it ends.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field

TTL_SECONDS = 600
MAX_PER_PERSON = 20
MAX_TOTAL = 2000

OK, NONE, CHANGED = "ok", "none", "changed"


@dataclass
class Approval:
    id: str
    principal: str
    who: str
    tool: str
    tool_class: str
    call_hash: str
    preview: object
    content_digest: str | None = None
    created: float = field(default_factory=time.monotonic)
    approved: bool = False
    #: sha256 of the preview as created; the page's form token is bound to it.
    view_digest: str = ""


def view_digest(preview) -> str:
    """sha256 of everything the confirmation page shows: recipients,
    subject, attachments, text, the targets and every argument."""
    data = dataclasses.asdict(preview) if dataclasses.is_dataclass(preview) else preview
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


class Approvals:
    """Thread-safe, in-memory, bounded."""

    def __init__(self, ttl: float = TTL_SECONDS, clock=time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._by_id: dict[str, Approval] = {}

    def _expire(self):
        now = self._clock()
        for k in [k for k, a in self._by_id.items() if now - a.created > self._ttl]:
            del self._by_id[k]

    def request(self, principal: str, who: str, tool: str, tool_class: str, call_hash: str,
                preview, content_digest: str | None = None) -> str | None:
        """The id of the pending approval for this exact call and exactly
        this content and preview (an existing one is reused, so a model that
        retries does not pile them up), or None when the person or the
        gateway holds too many. An approval is never changed once created:
        when the draft, the target or anything else shown differs, a NEW
        approval is created and the old one stays bound to what its page
        showed (confirming it later finds a content mismatch: CHANGED)."""
        if not principal:
            return None
        shown = view_digest(preview)
        with self._lock:
            self._expire()
            for a in self._by_id.values():
                if (a.principal == principal and a.who == who and a.call_hash == call_hash
                        and not a.approved and a.content_digest == content_digest
                        and a.view_digest == shown):
                    return a.id
            mine = sum(1 for a in self._by_id.values() if a.principal == principal)
            if mine >= MAX_PER_PERSON or len(self._by_id) >= MAX_TOTAL:
                return None
            aid = secrets.token_urlsafe(24)
            self._by_id[aid] = Approval(aid, principal, who, tool, tool_class, call_hash,
                                        preview, content_digest, created=self._clock(),
                                        view_digest=shown)
            return aid

    def pending_for(self, aid: str, principal: str) -> Approval | None:
        """The approval `aid` when it belongs to `principal` and is still
        pending; None otherwise (unknown, expired, someone else's, done)."""
        with self._lock:
            self._expire()
            a = self._by_id.get(aid)
            if a is None or not principal or not hmac.compare_digest(a.principal, principal):
                return None
            return None if a.approved else a

    def approve(self, aid: str, principal: str) -> bool:
        with self._lock:
            self._expire()
            a = self._by_id.get(aid)
            if a is None or a.approved or not principal or not hmac.compare_digest(a.principal, principal):
                return False
            a.approved = True
            # The confirmation window starts when the person confirms.
            a.created = self._clock()
            return True

    def consume(self, principal: str, who: str, call_hash: str, content_digest: str | None = None) -> str:
        """OK once for an APPROVED approval of exactly this call (and, when
        it was bound to content, of exactly that content); it is gone
        afterwards (one confirmation, one relay). CHANGED when the content
        no longer matches (the approval is dropped: confirm again). NONE
        when there is no approved approval for this call."""
        if not principal:
            return NONE
        with self._lock:
            self._expire()
            stale = []
            for k, a in list(self._by_id.items()):
                if (a.approved and a.principal == principal and a.who == who
                        and hmac.compare_digest(a.call_hash, call_hash)):
                    if a.content_digest is not None and (
                            content_digest is None or not hmac.compare_digest(a.content_digest, content_digest)):
                        stale.append(k)
                        continue
                    del self._by_id[k]
                    for s in stale:
                        self._by_id.pop(s, None)
                    return OK
            for s in stale:
                del self._by_id[s]
            return CHANGED if stale else NONE


class InFlight:
    """Per person: Microsoft 365 changes in flight through the gateway, and
    whether one of them is a confirmed draft send that must run alone."""

    def __init__(self):
        self._lock = threading.Lock()
        self._count: dict[str, int] = {}
        self._alone: set[str] = set()

    def begin(self, principal: str, alone: bool = False):
        """A release callable, or None when the call must wait: another
        change runs while this one must run alone, or a lone one runs."""
        key = principal or ""
        with self._lock:
            if key in self._alone or (alone and self._count.get(key, 0)):
                return None
            self._count[key] = self._count.get(key, 0) + 1
            if alone:
                self._alone.add(key)
        done = threading.Event()

        def release():
            if done.is_set():
                return
            done.set()
            with self._lock:
                n = self._count.get(key, 1) - 1
                if n <= 0:
                    self._count.pop(key, None)
                else:
                    self._count[key] = n
                if alone:
                    self._alone.discard(key)
        return release


def csrf_token(secret: str, aid: str, principal: str, shown: str) -> str:
    """The confirm form's token: bound to the approval, the person and the
    digest of the preview the page showed (`view_digest`)."""
    key = hashlib.sha256(("m365-confirm\0" + (secret or "")).encode()).digest()
    return hmac.new(key, f"{aid}\0{principal}\0{shown}".encode(), hashlib.sha256).hexdigest()


_STORE_LOCK = threading.Lock()


def store(app) -> Approvals:
    """The app's approval store (one per app, so a test app gets its own)."""
    with _STORE_LOCK:
        st = app.extensions.get("rzfz_m365_approvals")
        if st is None:
            st = app.extensions["rzfz_m365_approvals"] = Approvals()
        return st


def in_flight(app) -> InFlight:
    with _STORE_LOCK:
        st = app.extensions.get("rzfz_m365_in_flight")
        if st is None:
            st = app.extensions["rzfz_m365_in_flight"] = InFlight()
        return st
