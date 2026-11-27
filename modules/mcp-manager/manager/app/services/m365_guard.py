# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The gateway's Microsoft 365 guard (#3433): what ms365-mcp may do, for whom.

Settings (the stack .env, read per call so a recreate is all an apply needs).
The defaults leave Microsoft 365 unrestricted; each key restricts it:

  M365_MCP_MODE                   read-write (default) | read-only
  M365_MCP_ALLOW_SEND             true (default) | false
  M365_MCP_SEND_DELETE_AGENTS     true (default) | false — a person's agents
  M365_MCP_SEND_DELETE_WORKFLOWS  true (default) | false — Dify, technical users
  M365_MCP_CONFIRM_SEND_DELETE    false (default) | true — chat send/delete
                                  only after the person confirms each call

An absent or empty key is its default; any other spelling is the strictest
value (read-only, false, and true for the confirmation). Every call that is
not a read is audited whatever the settings say.

Every tool of the pinned ms-365-mcp-server has a class, DERIVED from its own
tool list (tests/fixtures/ms365-mcp/tools-<version>.json, measured from the
pinned image; `derive_classes` turns it into m365_tool_classes.json):

  read    the server marks it read-only (readOnlyHint: GET, or a POST it
          declares read-only, or a utility that only reads)
  send    something reaches other people by itself or leaves the person's control
          (a write into a place that is already shared is still a write):
          every tool that needs a *.Send scope, plus SEND_EXTRA (sharing a
          drive item, a sharing link or an embeddable preview, sharing the
          calendar, mail rules and mailbox settings that forward or answer
          mail, group and team membership, change subscriptions, forwarding
          or cancelling an event, ...)
  delete  an HTTP DELETE, plus clearing or deleting a worksheet range
  write   everything else the server can do (drafts, events, folders,
          uploads, edits)

A tool that is not in the table is a `delete` — the strictest class. Some
tools are classed per call, from their arguments (and, where the arguments
cannot tell, from the object itself, read by the gateway with the caller's
own token):

  * a calendar event with attendees is a `send` (Microsoft mails the
    invitations): created with a non-empty attendee list, updated with any
    attendee list or on an event that already has attendees, deleted while
    it has attendees (they get a cancellation); a response (accept, decline,
    tentative) is a `send` unless it says sendResponse: false. Whatever
    cannot be told is a `send`;
  * copying or moving a drive item into ANOTHER drive is a `send`;
  * moving a message into Deleted Items (or the recoverable-items folders)
    by its well-known name is a `delete`; a move by the folder's own id is a
    `write` (the gateway does not look folders up; the message stays
    recoverable there);
  * `graph-batch` is a `read` only when every sub-request is a GET; any
    other batch is refused outright (a batch would carry a send past
    M365_MCP_ALLOW_SEND).

Decision per call:

  * read-only mode: only `read`; everything else is refused (the server does
    not list them then either: it runs with --read-only).
  * read and write: allowed.
  * send with M365_MCP_ALLOW_SEND off: refused for everyone.
  * send and delete in chat: allowed, or — with M365_MCP_CONFIRM_SEND_DELETE
    on — only after the person confirms THIS call on the confirmation page
    (m365_confirm.py). The model cannot confirm: the page is behind the
    person's own Authentik session, and the approval is bound to the
    person, the tool, the exact arguments and, for a draft, its content.
  * send and delete for an unattended caller (an agent, a Dify workflow, a
    technical user): allowed while that caller class is enabled (the
    default), refused otherwise; never a confirmation (no person is there).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

READ, WRITE, SEND, DELETE = "read", "write", "send", "delete"
CLASSES = (READ, WRITE, SEND, DELETE)
#: What an unknown tool is: the strictest class.
STRICTEST = DELETE

MODE_READ_ONLY, MODE_READ_WRITE = "read-only", "read-write"

#: Caller classes.
CHAT, AGENT, WORKFLOW, TECHNICAL = "chat", "agent", "workflow", "technical"

CLASSES_FILE = Path(__file__).with_name("m365_tool_classes.json")

#: send: tools that need no *.Send scope but still hand something to other
#: people, expose it to them, or send it to an address the caller names.
SEND_EXTRA = frozenset({
    # mail and events that reach others
    "forward-calendar-event", "cancel-calendar-event", "reply-to-group-thread",
    "create-planner-task-message",
    # sharing and exposing content
    "share-drive-item", "create-drive-item-share-link", "create-drive-item-preview",
    "create-my-calendar-permission", "update-my-calendar-permission",
    # rules and settings that forward or answer mail on their own
    "create-mail-rule", "update-mail-rule", "update-mailbox-settings",
    # membership: other people gain access
    "add-group-member", "add-group-owner", "add-team-member", "create-chat",
    # change notifications to a URL the caller picks
    "create-subscription", "update-subscription", "reauthorize-subscription",
})
#: delete: tools that are not an HTTP DELETE but remove data.
DELETE_EXTRA = frozenset({"clear-excel-range", "delete-excel-range"})
#: Classed per call (see the module docstring).
BATCH_TOOL = "graph-batch"
MOVE_TOOLS = frozenset({"move-mail-message", "move-shared-mailbox-message"})
DELETED_FOLDERS = frozenset({"deleteditems", "recoverableitemsdeletions", "recoverableitemspurges",
                             "recoverableitemsroot", "recoverableitemsversions"})
EVENT_CREATE = frozenset({"create-calendar-event", "create-specific-calendar-event"})
EVENT_UPDATE = frozenset({"update-calendar-event", "update-specific-calendar-event"})
EVENT_DELETE = frozenset({"delete-calendar-event", "delete-specific-calendar-event"})
EVENT_RESPONSE = frozenset({"accept-calendar-event", "decline-calendar-event",
                            "tentatively-accept-calendar-event"})
COPY_TOOL = "copy-drive-item"
#: Copy and move take the same "another drive" rule (#3477, A2-02).
DRIVE_TARGET_TOOLS = frozenset({COPY_TOOL, "move-rename-onedrive-item"})
#: Mail send tools: they need Mail.Send in the person's grant.
MAIL_SEND_SCOPES = frozenset({"Mail.Send", "Mail.Send.Shared"})
#: The mail send tools of the pinned server (they need Mail.Send).
MAIL_SEND_TOOLS = frozenset({
    "send-mail", "send-draft-message", "reply-mail-message", "reply-all-mail-message",
    "forward-mail-message", "send-shared-mailbox-mail", "send-shared-mailbox-draft",
    "reply-shared-mailbox-mail", "reply-all-shared-mailbox-mail", "forward-shared-mailbox-mail",
})
#: Sends a stored draft: a confirmation is bound to the draft's content.
DRAFT_SEND_TOOL = "send-draft-message"


def derive_classes(tools: dict) -> dict:
    """{tool: class} from the measured tool list (`tools` of the fixture:
    name -> {method, read_only_hint, scopes})."""
    out = {}
    for name, t in tools.items():
        scopes = set(t.get("scopes") or ())
        if t.get("read_only_hint") is True:
            cls = READ
        elif name in SEND_EXTRA or any(".Send" in s for s in scopes):
            cls = SEND
        elif name in DELETE_EXTRA or t.get("method") == "DELETE":
            cls = DELETE
        elif name == BATCH_TOOL:
            cls = STRICTEST
        else:
            cls = WRITE
        out[name] = cls
    return out


def _load_classes() -> tuple[str, dict]:
    try:
        spec = json.loads(CLASSES_FILE.read_text(encoding="utf-8"))
        classes = {str(k): str(v) for k, v in (spec.get("classes") or {}).items() if v in CLASSES}
        return str(spec.get("version", "")), classes
    except Exception:  # noqa: BLE001 — no table: every tool is the strictest class
        return "", {}


VERSION, TOOL_CLASSES = _load_classes()


# ---------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    mode: str = MODE_READ_WRITE
    allow_send: bool = True
    agents: bool = True
    workflows: bool = True
    confirm: bool = False
    #: Keys whose value was not a valid spelling (read as the strictest).
    invalid: tuple = ()

    @property
    def read_write(self) -> bool:
        return self.mode == MODE_READ_WRITE


#: key -> (default, strictest) of the true/false keys.
FLAGS = {
    "M365_MCP_ALLOW_SEND": (True, False),
    "M365_MCP_SEND_DELETE_AGENTS": (True, False),
    "M365_MCP_SEND_DELETE_WORKFLOWS": (True, False),
    "M365_MCP_CONFIRM_SEND_DELETE": (False, True),
}
MODE_DEFAULT = MODE_READ_WRITE


def _flag(env, key, invalid) -> bool:
    default, strictest = FLAGS[key]
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    invalid.append(key)
    return strictest


def settings(env=None) -> Settings:
    env = os.environ if env is None else env
    invalid: list = []
    raw_mode = env.get("M365_MCP_MODE")
    if raw_mode in (None, ""):
        mode = MODE_DEFAULT
    elif raw_mode in (MODE_READ_ONLY, MODE_READ_WRITE):
        mode = raw_mode
    else:
        mode = MODE_READ_ONLY
        invalid.append("M365_MCP_MODE")
    allow_send = _flag(env, "M365_MCP_ALLOW_SEND", invalid)
    agents = _flag(env, "M365_MCP_SEND_DELETE_AGENTS", invalid)
    workflows = _flag(env, "M365_MCP_SEND_DELETE_WORKFLOWS", invalid)
    confirm = _flag(env, "M365_MCP_CONFIRM_SEND_DELETE", invalid)
    return Settings(mode, allow_send and mode == MODE_READ_WRITE, agents, workflows, confirm,
                    tuple(invalid))


# ---------------------------------------------------------------- classes
def _norm(key: str) -> str:
    return key.replace("-", "").replace("_", "").lower()


def _ci_get(d: dict, key: str):
    """The upstream reads keys leniently (camelCase, kebab-case, a body field
    given at the top level): look a key up case-insensitively, ignoring '-'
    and '_'."""
    want = _norm(key)
    return [v for k, v in d.items() if isinstance(k, str) and _norm(k) == want]


def _bodies(args: dict):
    """(dict bodies, True when a body is there that is not a mapping)."""
    out, odd = [], False
    for b in _ci_get(args, "body"):
        if isinstance(b, dict):
            out.append(b)
        else:
            odd = True
    return out, odd


def _field(args: dict, key: str):
    """Every value the upstream could read for `key`: at the top level
    (merged into the body) or in the body."""
    bodies, _ = _bodies(args)
    vals = list(_ci_get(args, key))
    for b in bodies:
        vals.extend(_ci_get(b, key))
    return vals


def _batch_class(args: dict) -> str:
    """`read` only when every sub-request of every request list the upstream
    could read is a GET; anything unexpected is the strictest class."""
    bodies, odd = _bodies(args)
    if odd:
        return STRICTEST
    lists = []
    for body in bodies:
        lists.extend(_ci_get(body, "requests"))
    lists.extend(_ci_get(args, "requests"))
    if not lists:
        return STRICTEST
    for reqs in lists:
        if not isinstance(reqs, list) or not reqs:
            return STRICTEST
        for r in reqs:
            if not isinstance(r, dict):
                return STRICTEST
            methods = _ci_get(r, "method")
            if not methods or any(not isinstance(m, str) or m.strip().upper() != "GET" for m in methods):
                return STRICTEST
    return READ


def _move_class(args: dict) -> str:
    if _bodies(args)[1]:
        # A body that is not a mapping cannot be read here (#3477, A2-06).
        return DELETE
    for t in _field(args, "destinationId"):
        if not isinstance(t, str) or t.strip().lower() in DELETED_FOLDERS:
            return DELETE
    return WRITE


def _attendees(args: dict):
    """None: no attendee list anywhere; True: a non-empty (or unreadable)
    one; False: only empty lists."""
    if _bodies(args)[1]:
        return True
    vals = _field(args, "attendees")
    if not vals:
        return None
    return any(not isinstance(v, list) or v for v in vals)


def _event_class(tool: str, args: dict, cls: str) -> str:
    if tool in EVENT_CREATE:
        return SEND if _attendees(args) else WRITE
    if tool in EVENT_UPDATE:
        # any attendee list (also an emptied one: the removed get a
        # cancellation) is a send; none at all is settled by the event
        return SEND if _attendees(args) is not None else WRITE
    if tool in EVENT_RESPONSE:
        if _bodies(args)[1]:
            return SEND
        flags = _field(args, "sendResponse")
        return WRITE if flags and all(v is False for v in flags) else SEND
    return cls


def _copy_class(args: dict) -> str:
    src = [v for v in _ci_get(args, "driveId") if isinstance(v, str)]
    if _bodies(args)[1]:
        return SEND
    for ref in _field(args, "parentReference"):
        if not isinstance(ref, dict):
            return SEND
        for d in _ci_get(ref, "driveId"):
            if not isinstance(d, str) or not src or any(d != s for s in src):
                return SEND
    return WRITE


def classify(tool, args) -> str:
    """The class from the tool and its arguments. Event updates and deletes
    may still be raised to `send` by settle_event (the stored event)."""
    args = args if isinstance(args, dict) else {}
    cls = TOOL_CLASSES.get(tool) if isinstance(tool, str) else None
    if cls is None:
        return STRICTEST
    if tool == BATCH_TOOL:
        return _batch_class(args)
    if tool in MOVE_TOOLS and cls == WRITE:
        return _move_class(args)
    if tool in EVENT_CREATE | EVENT_UPDATE | EVENT_RESPONSE:
        return _event_class(tool, args, cls)
    if tool in DRIVE_TARGET_TOOLS and cls == WRITE:
        return _copy_class(args)
    return cls


def event_lookup(tool: str, args, cls: str):
    """The Graph path of the event whose attendees settle this call's class,
    "" when the call needs one but names no usable event (it is then a
    send), or None when no lookup is needed."""
    if not (tool in EVENT_UPDATE and cls == WRITE) and tool not in EVENT_DELETE:
        return None
    ids = [v for v in _ci_get(args if isinstance(args, dict) else {}, "eventId") if isinstance(v, str)]
    if len(ids) != 1 or not ids[0]:
        return ""
    return f"/me/events/{quote(ids[0], safe='')}?$select=attendees"


def settle_event(cls: str, status: int, data) -> str:
    """Raise to `send` when the stored event has attendees, or when it could
    not be read (fail closed)."""
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("attendees"), list):
        return SEND
    return SEND if data["attendees"] else cls


def caller_class(caller) -> str:
    """chat = a person in Open WebUI; everything else runs with nobody
    watching each call."""
    if getattr(caller, "kind", "") == "technical":
        return TECHNICAL
    consumer = getattr(caller, "consumer", "")
    if consumer == "openwebui":
        return CHAT
    if consumer == "agent":
        return AGENT
    return WORKFLOW


ALLOW, CONFIRM, REFUSE = "allow", "confirm", "refuse"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str = ""


def decide(s: Settings, tool_class: str, who: str, tool: str = "") -> Decision:
    if tool_class == READ:
        return Decision(ALLOW)
    if tool == BATCH_TOOL:
        return Decision(REFUSE, "Only batches of reads (every sub-request a GET) are allowed for "
                                "Microsoft 365; send each change as its own tool call.")
    if not s.read_write:
        return Decision(REFUSE, "Microsoft 365 is read-only on this box.")
    if tool_class == WRITE:
        return Decision(ALLOW)
    if tool_class == SEND and not s.allow_send:
        return Decision(REFUSE, "Sending is switched off for Microsoft 365 on this box.")
    # send or delete (or an unknown tool, which is a delete)
    if who == CHAT:
        return Decision(CONFIRM if s.confirm else ALLOW)
    if who == AGENT and s.agents:
        return Decision(ALLOW)
    if who in (WORKFLOW, TECHNICAL) and s.workflows:
        return Decision(ALLOW)
    return Decision(REFUSE, f"Microsoft 365 {tool_class} tools are not available to {who} callers "
                            "on this box.")


# ---------------------------------------------------------------- the grant
def granted_scopes(headers) -> frozenset | None:
    """The delegated scopes (`scp`) of the injected Entra access token, or
    None when they cannot be read. Only a hint for a clear message: Graph
    itself refuses a call the grant does not cover."""
    auth = (headers or {}).get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    parts = auth[len("Bearer "):].strip().split(".")
    if len(parts) != 3:
        return None
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        scp = json.loads(raw).get("scp")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(scp, str):
        return None
    return frozenset(scp.split())


def grant_missing(tool: str, tool_class: str, scopes) -> bool:
    """True when the person's grant visibly lacks what this call needs: a
    read-only grant has no write scope at all; a mail send needs Mail.Send.
    Unknown scopes (None) never refuse."""
    if scopes is None or tool_class == READ:
        return False
    if tool in MAIL_SEND_TOOLS:
        return not (set(scopes) & MAIL_SEND_SCOPES)
    return not any("ReadWrite" in s for s in scopes)


RECONNECT_WRITE = ("Your Microsoft 365 connection is read-only. Reconnect Microsoft 365 at "
                   "https://{domain}/ to grant write access, then try again.")
RECONNECT_SEND = ("Your Microsoft 365 connection does not allow sending mail. Reconnect Microsoft 365 "
                  "at https://{domain}/ to grant it, then try again.")


def needs_write_reconnect(scopes, s: Settings) -> bool:
    """For the portal card: read-write is on and the person's grant has no
    write scope (or no Mail.Send while sending is on)."""
    if not s.read_write or scopes is None:
        return False
    if not any("ReadWrite" in x for x in scopes):
        return True
    return s.allow_send and not (set(scopes) & MAIL_SEND_SCOPES)


# ---------------------------------------------------------------- confirmation
def call_hash(principal: str, who: str, tool: str, args) -> str:
    """What an approval is bound to: the person, the caller class, the tool
    and the exact arguments (canonical JSON). Any change is a new call."""
    canon = json.dumps(args if isinstance(args, dict) else {}, sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256("\0".join((principal, who, tool, canon)).encode("utf-8")).hexdigest()


def _canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


#: What a draft is, for its confirmation: who it goes to, what it says and
#: what it carries.
DRAFT_FIELDS = ("subject", "toRecipients", "ccRecipients", "bccRecipients", "replyTo", "body")
DRAFT_SELECT = "subject,toRecipients,ccRecipients,bccRecipients,replyTo,body,from"
ATTACHMENT_SELECT = "id,name,size,contentType,isInline,lastModifiedDateTime"


def draft_paths(args):
    """(message path, attachments path) of the draft a send-draft-message
    names, or None when it names none usable."""
    ids = [v for v in _ci_get(args if isinstance(args, dict) else {}, "messageId") if isinstance(v, str)]
    if len(ids) != 1 or not ids[0]:
        return None
    mid = quote(ids[0], safe="")
    return (f"/me/messages/{mid}?$select={DRAFT_SELECT}",
            f"/me/messages/{mid}/attachments?$select={ATTACHMENT_SELECT}&$top=50")


def draft_digest(msg, atts):
    """sha256 of the draft's recipients, subject, body and attachments, or
    None when the answers are not a readable draft (a confirmation then
    cannot be bound, and the send is refused)."""
    if not isinstance(msg, dict) or not isinstance(atts, dict) or not isinstance(atts.get("value"), list):
        return None
    if atts.get("@odata.nextLink"):
        return None
    att = sorted(([a.get(k) for k in ("id", "name", "size", "contentType", "isInline", "lastModifiedDateTime")]
                  for a in atts["value"] if isinstance(a, dict)), key=_canon)
    return hashlib.sha256(_canon({"m": {k: msg.get(k) for k in DRAFT_FIELDS}, "a": att}).encode()).hexdigest()


def target_lookups(tool: str, args):
    """[(label, Graph path)] of the objects a call acts on, for the person to
    see on the confirmation page (the message replied to, forwarded or
    deleted; the event; the drive item)."""
    args = args if isinstance(args, dict) else {}
    out = []
    mids = [v for v in _ci_get(args, "messageId") if isinstance(v, str) and v]
    if len(mids) == 1 and tool != DRAFT_SEND_TOOL:
        out.append(("message", f"/me/messages/{quote(mids[0], safe='')}"
                               "?$select=subject,from,toRecipients,ccRecipients,bccRecipients,hasAttachments"))
    eids = [v for v in _ci_get(args, "eventId") if isinstance(v, str) and v]
    if len(eids) == 1:
        out.append(("event", f"/me/events/{quote(eids[0], safe='')}?$select=subject,organizer,attendees,start"))
    drives = [v for v in _ci_get(args, "driveId") if isinstance(v, str) and v]
    items = [v for v in _ci_get(args, "driveItemId") if isinstance(v, str) and v]
    if len(drives) == 1 and len(items) == 1:
        out.append(("file", f"/drives/{quote(drives[0], safe='')}/items/{quote(items[0], safe='')}"
                            "?$select=name,size,webUrl"))
    return out


# ---------------------------------------------------------------- addresses
def addresses(node, out=None, depth=0) -> list:
    """Every e-mail address in `node`, in order, once: emailAddress.address
    (recipients, attendees, permissions), `email` (sharing invitations) and
    plain strings in a *Recipients list."""
    out = [] if out is None else out
    if depth > 10:
        return out

    def add(a):
        if isinstance(a, str) and "@" in a and a not in out:
            out.append(a)
    if isinstance(node, dict):
        ea = node.get("emailAddress")
        if isinstance(ea, dict):
            add(ea.get("address"))
        add(node.get("email"))
        for k, v in node.items():
            if isinstance(k, str) and k.lower().endswith("recipients") and isinstance(v, list):
                for r in v:
                    add(r)
            if isinstance(v, (dict, list)):
                addresses(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            addresses(v, out, depth + 1)
    return out


def _count_recipients(node, depth=0) -> int:
    """How many recipient entries (not distinct addresses) a call names."""
    n = 0
    if depth > 10:
        return 0
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and (k.lower().endswith("recipients") or k.lower() == "attendees") \
                    and isinstance(v, list):
                n += len(v)
            elif isinstance(v, (dict, list)):
                n += _count_recipients(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            n += _count_recipients(v, depth + 1)
    return n


# ---------------------------------------------------------------- audit
_MAX_ID = 256


def audit_tool_name(tool) -> str:
    """The tool as the audit records it: a tool of the pinned server by its
    name; anything else (the caller chose it: an address, free text) only as
    a hash (#3477, A2-04)."""
    if isinstance(tool, str) and tool in TOOL_CLASSES:
        return tool
    raw = tool if isinstance(tool, str) else repr(tool)
    return "unknown-tool:" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]
#: What an object id may look like to be recorded: Graph ids are base64 or
#: base64url with a few separators. Anything else (the model chose it) is
#: recorded as NOT_AN_ID, so no free text reaches the audit.
_ID_VALUE = re.compile(r"[A-Za-z0-9_=!.,+/:-]{1,256}")
NOT_AN_ID = "(not an id)"


def audit_fields(tool: str, args) -> dict:
    """What the audit records about a call besides who and which tool: the
    ids of the objects it acts on (opaque Graph ids, top-level arguments
    named *Id / *-id), the number of recipients and their DOMAINS only, and
    for a batch the sub-request methods. Never a subject, a body, a file's
    content or an address."""
    args = args if isinstance(args, dict) else {}
    ids = {}
    for k, v in args.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        norm = _norm(k)
        if norm.endswith("id") and len(norm) > 2 and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", k):
            ids[k] = v if _ID_VALUE.fullmatch(v) else NOT_AN_ID
    out = {"target_ids": ids}
    found = addresses(args)
    count = max(_count_recipients(args), len(found))
    if count:
        out["recipient_count"] = count
        out["recipient_domains"] = sorted({a.rsplit("@", 1)[1].lower()[:253] for a in found})[:20]
    if tool == BATCH_TOOL:
        methods = []
        for body in _bodies(args)[0]:
            for reqs in _ci_get(body, "requests"):
                if isinstance(reqs, list):
                    methods += [str(m)[:8].upper() for r in reqs if isinstance(r, dict)
                                for m in _ci_get(r, "method")]
        out["batch_methods"] = methods[:20]
    return out


@dataclass
class Preview:
    """What the confirmation page shows, in this order."""
    recipients: list = field(default_factory=list)
    subject: str | None = None
    attachments: list = field(default_factory=list)   # [(name, size in bytes or None)]
    text: str | None = None
    targets: list = field(default_factory=list)        # [(label, [(field, value)])]
    details: str = ""


#: Characters a person cannot see that change what they read: controls
#: (other than newline and tab), bidirectional overrides and isolates, and
#: zero-width marks. Shown as ⟦U+XXXX⟧ on the confirmation page.
_INVISIBLE = {0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x061C, 0x180E}
_INVISIBLE |= set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


def visible(text):
    """`text` with every invisible or direction-changing character made
    visible; anything that is not a string is returned unchanged."""
    if not isinstance(text, str):
        return text
    out = []
    for ch in text:
        cp = ord(ch)
        if ch in "\n\t":
            out.append(ch)
        elif cp in _INVISIBLE or unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs"):
            out.append(f"⟦U+{cp:04X}⟧")
        else:
            out.append(ch)
    return "".join(out)


def _attachments_of(args) -> list:
    out = []
    for atts in _field(args, "attachments") + [m.get("attachments") for m in _field(args, "message")
                                              if isinstance(m, dict) and "attachments" in m]:
        if isinstance(atts, list):
            for a in atts:
                if isinstance(a, dict):
                    cb = a.get("contentBytes")
                    size = a.get("size") if isinstance(a.get("size"), int) else (
                        len(cb) * 3 // 4 if isinstance(cb, str) else None)
                    out.append((str(a.get("name") or "(no name)"), size))
    return out


def _details(args) -> str:
    """The full arguments; only an attachment's base64 content is replaced,
    by a marker that says so and gives its size."""
    def cut(v, key=""):
        if key.lower() == "contentbytes" and isinstance(v, str):
            return f"[attachment content not shown: {len(v) * 3 // 4} bytes]"
        if isinstance(v, dict):
            return {k: cut(x, str(k)) for k, x in v.items()}
        if isinstance(v, list):
            return [cut(x) for x in v]
        return v
    return json.dumps(cut(args if isinstance(args, dict) else {}), indent=2, ensure_ascii=False,
                      sort_keys=True)


def _text_of(args):
    for m in _field(args, "message") + [args]:
        if isinstance(m, dict):
            for b in _ci_get(m, "body"):
                if isinstance(b, dict) and isinstance(b.get("content"), str):
                    return b["content"]
    for c in _field(args, "comment"):
        if isinstance(c, str):
            return c
    return None


def _subject_of(args):
    for m in _field(args, "message") + [args] + _bodies(args)[0]:
        if isinstance(m, dict):
            for s in _ci_get(m, "subject"):
                if isinstance(s, str):
                    return s
    return None


def _target_fields(label: str, data) -> list:
    if not isinstance(data, dict):
        return [("", "could not be read")]
    if label == "message":
        return [("Subject", str(data.get("subject") or "")),
                ("From", ", ".join(addresses(data.get("from")))),
                ("To", ", ".join(addresses(data.get("toRecipients")))),
                ("Cc", ", ".join(addresses(data.get("ccRecipients")))),
                ("Bcc", ", ".join(addresses(data.get("bccRecipients")))),
                ("Attachments", "yes" if data.get("hasAttachments") else "no")]
    if label == "event":
        start = data.get("start") if isinstance(data.get("start"), dict) else {}
        return [("Subject", str(data.get("subject") or "")),
                ("Organizer", ", ".join(addresses(data.get("organizer")))),
                ("Attendees", ", ".join(addresses(data.get("attendees"))) or "none"),
                ("Starts", str(start.get("dateTime") or ""))]
    return [("Name", str(data.get("name") or "")), ("Size", str(data.get("size") or "")),
            ("Link", str(data.get("webUrl") or ""))]


def preview(tool: str, args, targets=(), draft=None, draft_atts=None) -> Preview:
    """The page's content. `targets`: [(label, status, json)] of the objects
    the call acts on; `draft`/`draft_atts`: the draft a send-draft-message
    sends (its recipients and content are what will go out)."""
    args = args if isinstance(args, dict) else {}
    p = Preview(details=_details(args))
    if draft is not None and isinstance(draft, dict):
        p.recipients = addresses({k: draft.get(k) for k in ("toRecipients", "ccRecipients", "bccRecipients")})
        p.subject = str(draft.get("subject") or "")
        body = draft.get("body") if isinstance(draft.get("body"), dict) else {}
        p.text = body.get("content") if isinstance(body.get("content"), str) else None
        if isinstance(draft_atts, dict) and isinstance(draft_atts.get("value"), list):
            p.attachments = [(str(a.get("name") or "(no name)"), a.get("size") if isinstance(a.get("size"), int)
                              else None) for a in draft_atts["value"] if isinstance(a, dict)]
    else:
        p.recipients = addresses(args)
        p.subject = _subject_of(args)
        p.attachments = _attachments_of(args)
        p.text = _text_of(args)
    for label, status, data in targets:
        p.targets.append((label, _target_fields(label, data if status == 200 else None)))
    # What the person reads is what is there: no hidden or reordering character.
    p.recipients = [visible(r) for r in p.recipients]
    p.subject = visible(p.subject)
    p.text = visible(p.text)
    p.details = visible(p.details)
    p.attachments = [(visible(n), sz) for n, sz in p.attachments]
    p.targets = [(label, [(k, visible(v)) for k, v in fields]) for label, fields in p.targets]
    return p
