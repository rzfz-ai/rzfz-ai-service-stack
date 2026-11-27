#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""razzfazz-mcp-proxy, Google build (#2472, #2086, #3453; design record §3, rulings I4/R2-2).

The in-repo, pinned build of the per-user Google Workspace proxy. One
container per person, started by mcp-manager's provisioner with THAT person's
own Google access token (catalogue `google`, env GOOGLE_ACCESS_TOKEN from the
person's own vault row) and two per-instance bearers: PROXY_AUTH_TOKEN (the
route secret the person's agents present) and PROXY_GATEWAY_TOKEN (presented
only by the MCP gateway, which relays the person's chats).

What it guarantees:
  * The ONLY Google credential it ever sends is GOOGLE_ACCESS_TOKEN from its
    own environment. Nothing in a request (headers, arguments) can name,
    replace or add a Google credential: tool arguments are schema-checked
    (unknown keys are refused), and the request's Authorization header is
    only compared with the two bearers, never forwarded.
  * No PROXY_AUTH_TOKEN, no service (401 on /mcp, fail closed).
  * Requests go to www.googleapis.com only (Drive, Calendar and Gmail are all
    served there), and a redirect is never followed (it would carry the token).
  * Standard library only: no third-party package enters the image, so the
    Dockerfile's digest-pinned base is the whole of its supply chain.

What it may do (#3453) — every tool has a class:

  read    search and read Drive files, calendars, events and mail
  write   create, update, rename or move Drive files in folders nobody else
          sees; create or change an event nobody else takes part in;
          create a mail draft
  send    anything that reaches other people: sending, replying to or
          forwarding mail, sharing a Drive file, creating or moving a file
          into a shared folder or a shared drive, creating or changing an
          event with attendees (passed, or already on the stored event) or
          with notify_attendees, deleting an event with notify_attendees
  delete  moving a Drive file to the trash, deleting an event (no mail)

A tool that is not in the table is a `delete` (the strictest class). Where
the arguments alone cannot tell, the stored object is read with the person's
own token first (the event's attendees, the target folder's `shared` and
`driveId`); one that cannot be read counts as send.

The box's settings (the stack .env, handed in at start by the provisioner):

  GOOGLE_MCP_MODE                read-write (default) | read-only
  GOOGLE_MCP_ALLOW_SEND          true (default) | false — the send class, for everyone
  GOOGLE_MCP_SEND_DELETE_AGENTS  true (default) | false — send and delete by agents

Unset or empty is the default; any other spelling is the strictest value
(read-only, false), so a typo never opens anything. The decision is made
here, where the tools are: read-only mode refuses every write, send and
delete even when the token would allow it. A refused tool is also left out
of tools/list for that caller.

Who calls: a request presenting PROXY_GATEWAY_TOKEN comes through the
gateway (a chat); one presenting PROXY_AUTH_TOKEN is an agent.

Audit: every call that is not a read — allowed, refused or failed — writes
JSON lines to stdout (`event: mcp.tool_call`, the fields of the stack's
AuditLogger entry, `target: google`): `attempted` BEFORE the first request
that changes something (no line written, no request sent), then one outcome
line (`allowed`, `upstream_refused`, `outcome_unknown` when no answer came,
a refusal, ...). Each names the person (MCP_AUDIT_PRINCIPAL, their sub), the
tool, its class, the caller class, the decision, the ids of the objects
acted on, and the number of recipients with their DOMAINS only. Never a
subject, a body, a file's content or an address. The lines live in this
container's log only: they are gone when the container is recreated.

A person whose grant predates write access (calendar.readonly and
drive.readonly only) is told to reconnect Google Workspace when Google
refuses a call for a missing scope.

Transport: MCP streamable-http, JSON responses (no server-initiated stream):
POST /mcp answers `initialize`, `ping`, `tools/list`, `tools/call`;
`notifications/*` get 202; GET /mcp gets 405. GET /healthz is open.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET_KIND = "google"
GOOGLE_API = "https://www.googleapis.com"
PROTOCOL_VERSION = "2025-06-18"
MAX_BODY = 1024 * 1024          # a request body over 1 MiB is refused (413)
MAX_FILE_BYTES = 1024 * 1024    # drive_read_file / gmail_read_message return at most 1 MiB of text
MAX_CONTENT = 900 * 1024        # text written to Drive or mail, in UTF-8 bytes
MAX_RECIPIENTS = 100
GOOGLE_TIMEOUT = 30
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9_@.-]{1,1024}$")
_MSG_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_EMAIL = re.compile(r"^[^@\s<>()\[\],;:\"\\]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+)$")
_DOMAIN = re.compile(r"^[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})$")
_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
#: What drive_create_file may convert a text upload into.
_CONVERT = {"document": "application/vnd.google-apps.document",
            "spreadsheet": "application/vnd.google-apps.spreadsheet"}
#: The text types a Drive upload may carry.
_UPLOAD_TYPES = ("text/plain", "text/csv", "text/markdown", "text/html", "application/json")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# ---------------------------------------------------------------------------
# Classes, settings and the decision
# ---------------------------------------------------------------------------

READ, WRITE, SEND, DELETE = "read", "write", "send", "delete"
CLASSES = (READ, WRITE, SEND, DELETE)
STRICTEST = DELETE
CHAT, AGENT = "chat", "agent"
MODE_READ_WRITE, MODE_READ_ONLY = "read-write", "read-only"


def settings(env=None) -> dict:
    """The box's settings. Unset/empty = the default (read-write, true);
    any other spelling than the exact one = the strictest (read-only, false)."""
    env = os.environ if env is None else env
    invalid = []
    raw = (env.get("GOOGLE_MCP_MODE") or "").strip()
    if raw in ("", MODE_READ_WRITE):
        mode = MODE_READ_WRITE
    elif raw == MODE_READ_ONLY:
        mode = MODE_READ_ONLY
    else:
        mode = MODE_READ_ONLY
        invalid.append("GOOGLE_MCP_MODE")

    def flag(key):
        v = (env.get(key) or "").strip()
        if v in ("", "true"):
            return True
        if v != "false":
            invalid.append(key)
        return False

    return {"mode": mode, "allow_send": flag("GOOGLE_MCP_ALLOW_SEND"),
            "agents": flag("GOOGLE_MCP_SEND_DELETE_AGENTS"), "invalid": tuple(invalid)}


def decide(tool_class: str, caller: str, s: dict) -> str:
    """'' when the call may go ahead, else the refusal decision."""
    if tool_class == READ:
        return ""
    if tool_class not in CLASSES:
        tool_class = STRICTEST
    if s["mode"] != MODE_READ_WRITE:
        return "refused_read_only"
    if tool_class == SEND and not s["allow_send"]:
        return "refused_send_disabled"
    if tool_class in (SEND, DELETE) and caller != CHAT and not s["agents"]:
        return "refused_agents"
    return ""


REFUSALS = {
    "refused_read_only": "Google Workspace is read-only on this box: '{tool}' is not available. "
                         "Ask your administrator.",
    "refused_send_disabled": "Sending and sharing through Google Workspace is turned off on this "
                             "box: '{tool}' is not available. Ask your administrator.",
    "refused_agents": "Agents may not send, share or delete through Google Workspace on this box: "
                      "'{tool}' is not available to an agent. Ask your administrator.",
}


def _reconnect(write: bool) -> str:
    domain = _env("MCP_DOMAIN")
    where = f"at https://{domain}/" if domain else "in the MCP portal"
    if write:
        return (f"Your Google Workspace connection does not allow this yet (it was granted "
                f"read-only). Reconnect Google Workspace {where} to grant write access, then "
                f"try again.")
    return (f"Your Google Workspace connection does not cover this yet. Reconnect Google "
            f"Workspace {where} to grant it, then try again.")


# ---------------------------------------------------------------------------
# Audit (stdout JSON lines, the AuditLogger entry shape)
# ---------------------------------------------------------------------------

_AUDIT_LOCK = threading.Lock()
_RISK = {WRITE: "caution", SEND: "danger", DELETE: "danger"}
#: The outcome field of each decision. `attempted` is written BEFORE the
#: first request that changes something; `outcome_unknown` after a change
#: request whose answer never came (a timeout, a dropped connection, a 5xx):
#: Google may have done it.
_OUTCOME = {"allowed": "success", "attempted": "attempted", "outcome_unknown": "unknown"}


def audit_entry(tool: str, tool_class: str, caller: str, decision: str, fields: dict) -> dict:
    ctx = {"caller_class": caller, "tool_class": tool_class, "decision": decision}
    for k in ("target_ids", "recipient_count", "recipient_domains"):
        if k in (fields or {}):
            ctx[k] = fields[k]
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "level": "INFO",
            "event": "mcp.tool_call", "user": _env("MCP_AUDIT_PRINCIPAL") or "unknown",
            "category": "mcp", "action": str(tool)[:128], "target": "google",
            "detail": decision, "risk": _RISK.get(tool_class, "danger"),
            "outcome": _OUTCOME.get(decision, "failure"), "context": ctx}


def audit(tool, tool_class, caller, decision, fields) -> None:
    line = json.dumps(audit_entry(tool, tool_class, caller, decision, fields), default=str)
    with _AUDIT_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


#: The call this thread is serving (one request per thread): google_request
#: writes its `attempted` line before the first change request and records
#: how that request ended, so call_tool can tell done, refused and unknown.
_CALL = threading.local()


def _begin_call(tool, tool_class, caller, fields) -> None:
    _CALL.ctx = {"tool": tool, "class": tool_class, "caller": caller, "fields": fields,
                 "attempted": False, "status": None}


def _call_ctx():
    return getattr(_CALL, "ctx", None)


def _before_change() -> None:
    """Before a request that changes something: one `attempted` line per call.
    A line that cannot be written stops the request (raises)."""
    ctx = _call_ctx()
    if ctx is not None and not ctx["attempted"]:
        audit(ctx["tool"], ctx["class"], ctx["caller"], "attempted", ctx["fields"])
        ctx["attempted"] = True


# ---------------------------------------------------------------------------
# Google, with the container's own token only
# ---------------------------------------------------------------------------

class GoogleError(Exception):
    """A message safe to show the caller (no token, no upstream body)."""


class ScopeMissing(GoogleError):
    """Google refused the call because the grant lacks its scope."""


class OutcomeUnknown(GoogleError):
    """A change request got no usable answer: Google may have done it."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise GoogleError("Google answered with a redirect; not followed.")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _scope_missing(err: urllib.error.HTTPError) -> bool:
    """Google's answer for a token without the call's scope: 403 with
    `insufficient_scope` in WWW-Authenticate, or the error reason
    ACCESS_TOKEN_SCOPE_INSUFFICIENT / insufficientPermissions. (A file the
    person may not touch is `insufficientFilePermissions`: not a scope.)"""
    if err.code != 403:
        return False
    if "insufficient_scope" in (err.headers.get("WWW-Authenticate", "") if err.headers else ""):
        return True
    try:
        body = json.loads(err.read(65536) or b"{}")
    except Exception:  # noqa: BLE001
        return False
    e = body.get("error") if isinstance(body, dict) else None
    if not isinstance(e, dict):
        return False
    reasons = {str(x.get("reason")) for x in (e.get("errors") or []) if isinstance(x, dict)}
    reasons |= {str(x.get("reason")) for x in (e.get("details") or []) if isinstance(x, dict)}
    return bool(reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})


def google_request(method: str, path: str, params: dict | None = None, *, body=None,
                   content_type: str = "application/json", raw: bool = False,
                   limit: int = MAX_FILE_BYTES):
    """`method` GOOGLE_API + path with the container's own access token.
    `path` is built by this module from validated values, never taken from a
    caller. `body`: a dict (sent as JSON) or bytes (sent as `content_type`)."""
    token = _env("GOOGLE_ACCESS_TOKEN")
    if not token:
        raise GoogleError("Google is not connected for this proxy. Connect Google in the MCP "
                          "portal; that restarts this integration with your authorization.")
    url = GOOGLE_API + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")}, doseq=True)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode() if isinstance(body, dict) else bytes(body)
        headers["Content-Type"] = "application/json" if isinstance(body, dict) else content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    change = method != "GET"
    if change:
        _before_change()
    ctx = _call_ctx()
    try:
        with _OPENER.open(req, timeout=GOOGLE_TIMEOUT) as resp:
            if change and ctx is not None:
                ctx["status"] = resp.status
            out = resp.read(limit + 1)
    except urllib.error.HTTPError as e:
        if change and ctx is not None:
            ctx["status"] = e.code
        if change and e.code >= 500:
            raise OutcomeUnknown(f"Google answered HTTP {e.code}.") from None
        if _scope_missing(e):
            raise ScopeMissing("scope missing") from None
        if e.code in (401, 403):
            # The recoveries named here work (B9 fix round 1): "Start" on a
            # running proxy whose token has expired restarts it with a renewed
            # token, and a reconnect restarts it with the new one.
            raise GoogleError("Google refused the request: the authorization has expired or "
                              "lacks this scope, or you may not do this with this item. Press "
                              "Start on Google in the MCP portal (it restarts the integration "
                              "with a renewed authorization); if Google still refuses, connect "
                              "Google again.") from None
        if e.code == 404:
            raise GoogleError("Not found in Google.") from None
        raise GoogleError(f"Google answered HTTP {e.code}.") from None
    except GoogleError:
        raise
    except Exception as e:  # noqa: BLE001 — never echo an upstream body or URL
        if change:
            raise OutcomeUnknown(f"no answer from Google ({type(e).__name__})") from None
        raise GoogleError(f"Google could not be reached ({type(e).__name__}).") from None
    if raw:
        return out
    try:
        got = json.loads(out or b"{}")
    except ValueError:
        got = None
    if not isinstance(got, dict):
        if change:
            # The change was accepted (2xx); its answer is not an object.
            raise _UnreadableAnswer()
        raise GoogleError("Google answered with something that is not a JSON object.")
    return got


class _UnreadableAnswer(Exception):
    """A change request answered 2xx with a body that is not a JSON object."""


def google_get(path: str, params: dict | None = None, *, raw: bool = False, limit: int = MAX_FILE_BYTES):
    return google_request("GET", path, params, raw=raw, limit=limit)


# ---------------------------------------------------------------------------
# Argument checks
# ---------------------------------------------------------------------------

def _int(v, lo, hi, default):
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"must be a whole number in {lo}..{hi}")
    return v


def _str(v, name, max_len=500, default=""):
    if v is None:
        return default
    if not isinstance(v, str) or len(v) > max_len or any(c in v for c in "\r\n\x00"):
        raise ValueError(f"{name} must be a single-line string of at most {max_len} characters")
    return v


def _text(v, name, default=""):
    """Multi-line text (a file's content, a mail or event body)."""
    if v is None:
        return default
    if not isinstance(v, str) or "\x00" in v or len(v.encode("utf-8")) > MAX_CONTENT:
        raise ValueError(f"{name} must be text of at most {MAX_CONTENT} bytes")
    return v


def _bool(v, name, default=False):
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ValueError(f"{name} must be true or false")
    return v


def _id(v, name, pattern=_FILE_ID, required=True):
    if v is None and not required:
        return ""
    s = _str(v, name, 1024)
    if not pattern.fullmatch(s or "") or _dots_only(s):
        raise ValueError(f"{name} must be a Google id")
    return s


def _dots_only(s: str) -> bool:
    """`.` / `..` would become a path segment of their own (review 282)."""
    return bool(s) and set(s) <= {"."}


def _emails(v, name):
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > MAX_RECIPIENTS:
        raise ValueError(f"{name} must be a list of at most {MAX_RECIPIENTS} email addresses")
    out = []
    for a in v:
        if not isinstance(a, str) or not _EMAIL.fullmatch(a):
            raise ValueError(f"{name} must hold plain email addresses (name@example.com)")
        out.append(a)
    return out


def _when(v, name, required=True):
    if v is None and not required:
        return None
    s = _str(v, name, 64)
    if _DATE.fullmatch(s or ""):
        return {"date": s}
    if _DATETIME.fullmatch(s or ""):
        return {"dateTime": s}
    raise ValueError(f"{name} must be an RFC 3339 date-time (2026-11-25T10:00:00+01:00) or a "
                     "date (2026-11-25) for an all-day event")


def _domains(addresses) -> list:
    return sorted({a.rsplit("@", 1)[1].lower()[:253] for a in addresses if "@" in a})[:20]


def _note_recipients(audit_fields: dict, addresses) -> None:
    addresses = [a for a in addresses if isinstance(a, str)]
    audit_fields["recipient_count"] = len(addresses)
    audit_fields["recipient_domains"] = _domains(addresses)


#: What an object id looks like to be recorded (#3477, A2-04): Google ids
#: are base64url-ish. A calendar id that is an address (a primary calendar),
#: or any free text the caller passed, is recorded as NOT_AN_ID.
_ID_VALUE = re.compile(r"[A-Za-z0-9_=!.,+/:-]{1,256}")
NOT_AN_ID = "(not an id)"


def _note_ids(audit_fields: dict, **ids) -> None:
    t = audit_fields.setdefault("target_ids", {})
    for k, v in ids.items():
        if isinstance(v, str) and v:
            t[k] = v if _ID_VALUE.fullmatch(v) else NOT_AN_ID


def audit_tool_name(name) -> str:
    """A tool of this server by its name; anything else only as a hash (the
    caller chose it: an address, free text) (#3477, A2-04)."""
    if isinstance(name, str) and name in TOOLS:
        return name
    raw = name if isinstance(name, str) else repr(name)
    return "unknown-tool:" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def calendar_list_calendars(args, _a):
    data = google_get("/calendar/v3/users/me/calendarList", {"maxResults": 100})
    return [{"id": c.get("id"), "summary": c.get("summary"), "primary": bool(c.get("primary"))}
            for c in data.get("items", [])]


def _cal(args):
    cal = _str(args.get("calendar_id"), "calendar_id", 300, "primary") or "primary"
    if _dots_only(cal):
        raise ValueError("calendar_id must be a calendar id")
    return cal


def _cal_path(cal):
    return f"/calendar/v3/calendars/{urllib.parse.quote(cal, safe='')}/events"


def calendar_list_events(args, _a):
    params = {"singleEvents": "true", "orderBy": "startTime",
              "maxResults": _int(args.get("max_results"), 1, 50, 10),
              "timeMin": _str(args.get("time_min"), "time_min", 64),
              "timeMax": _str(args.get("time_max"), "time_max", 64),
              "q": _str(args.get("query"), "query", 200)}
    data = google_get(_cal_path(_cal(args)), params)
    return [{"id": ev.get("id"), "summary": ev.get("summary"), "start": ev.get("start"),
             "end": ev.get("end"), "location": ev.get("location"), "link": ev.get("htmlLink"),
             "attendees": len(ev.get("attendees") or [])}
            for ev in data.get("items", [])]


def _drive_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def drive_search_files(args, _a):
    query = _str(args.get("query"), "query", 200)
    if not query:
        raise ValueError("query is required")
    data = google_get("/drive/v3/files", {
        "q": f"fullText contains {_drive_literal(query)} and trashed = false",
        "pageSize": _int(args.get("max_results"), 1, 50, 10),
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink,parents)"})
    return data.get("files", [])


def drive_read_file(args, _a):
    file_id = _id(args.get("file_id"), "file_id")
    meta = google_get(f"/drive/v3/files/{file_id}", {"fields": "id,name,mimeType,size"})
    mime = str(meta.get("mimeType") or "")
    if mime in _EXPORTS:
        body = google_get(f"/drive/v3/files/{file_id}/export", {"mimeType": _EXPORTS[mime]}, raw=True)
    elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
        body = google_get(f"/drive/v3/files/{file_id}", {"alt": "media"}, raw=True)
    else:
        raise GoogleError(f"'{meta.get('name')}' is a {mime or 'binary'} file; only documents, "
                          "sheets, slides and text files can be read.")
    truncated = len(body) > MAX_FILE_BYTES
    return {"name": meta.get("name"), "mimeType": mime, "truncated": truncated,
            "text": body[:MAX_FILE_BYTES].decode("utf-8", errors="replace")}


_GMAIL = "/gmail/v1/users/me"


def _headers(msg: dict) -> dict:
    return {str(h.get("name", "")).lower(): str(h.get("value", ""))
            for h in ((msg.get("payload") or {}).get("headers") or []) if isinstance(h, dict)}


def gmail_search_messages(args, _a):
    query = _str(args.get("query"), "query", 500)
    data = google_get(f"{_GMAIL}/messages", {"q": query,
                                             "maxResults": _int(args.get("max_results"), 1, 25, 10)})
    out = []
    for m in data.get("messages", []) or []:
        mid = str(m.get("id") or "")
        if not _MSG_ID.fullmatch(mid):
            continue
        meta = google_get(f"{_GMAIL}/messages/{mid}", {
            "format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
        meta2 = meta if isinstance(meta, dict) else {}
        h = _headers(meta2)
        out.append({"id": mid, "threadId": meta2.get("threadId"), "from": h.get("from"),
                    "subject": h.get("subject"), "date": h.get("date"),
                    "snippet": meta2.get("snippet")})
    return out


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _plain_text(payload: dict) -> str:
    """The first text/plain part of a Gmail message (else text/html)."""
    found = {"text/plain": None, "text/html": None}

    def walk(p, depth=0):
        if not isinstance(p, dict) or depth > 20:
            return
        mime = str(p.get("mimeType") or "")
        data = (p.get("body") or {}).get("data")
        if mime in found and found[mime] is None and isinstance(data, str):
            found[mime] = _b64d(data).decode("utf-8", errors="replace")
        for c in p.get("parts") or []:
            walk(c, depth + 1)
    walk(payload)
    return found["text/plain"] or found["text/html"] or ""


def _get_message(mid: str) -> dict:
    return google_get(f"{_GMAIL}/messages/{mid}", {"format": "full"})


def gmail_read_message(args, _a):
    mid = _id(args.get("message_id"), "message_id", _MSG_ID)
    msg = _get_message(mid)
    h = _headers(msg)
    text = _plain_text(msg.get("payload") or {})
    raw = text.encode("utf-8")
    return {"id": mid, "threadId": msg.get("threadId"), "from": h.get("from"), "to": h.get("to"),
            "cc": h.get("cc"), "subject": h.get("subject"), "date": h.get("date"),
            "labels": msg.get("labelIds"), "truncated": len(raw) > MAX_FILE_BYTES,
            "text": raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")}


# ---------------------------------------------------------------------------
# Drive: write, send (share), delete (trash)
# ---------------------------------------------------------------------------

def _upload_type(args):
    mime = _str(args.get("mime_type"), "mime_type", 100, "text/plain") or "text/plain"
    if mime not in _UPLOAD_TYPES:
        raise ValueError(f"mime_type must be one of {', '.join(_UPLOAD_TYPES)}")
    return mime


def drive_create_file(args, a):
    name = _str(args.get("name"), "name", 300)
    if not name:
        raise ValueError("name is required")
    content = _text(args.get("content"), "content")
    mime = _upload_type(args)
    convert = _str(args.get("convert_to"), "convert_to", 20)
    if convert and convert not in _CONVERT:
        raise ValueError("convert_to must be 'document' or 'spreadsheet'")
    folder = _id(args.get("folder_id"), "folder_id", required=False)
    meta = {"name": name, "mimeType": _CONVERT[convert] if convert else mime}
    if folder:
        meta["parents"] = [folder]
    _note_ids(a, folder_id=folder)
    boundary = "rzfz-" + base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: {mime}; charset=UTF-8\r\n\r\n"
            ).encode() + content.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode()
    out = google_request("POST", "/upload/drive/v3/files",
                         {"uploadType": "multipart", "fields": "id,name,mimeType,webViewLink"},
                         body=body, content_type=f"multipart/related; boundary={boundary}")
    _note_ids(a, created_id=str(out.get("id") or ""))
    return out


def drive_update_file(args, a):
    file_id = _id(args.get("file_id"), "file_id")
    _note_ids(a, file_id=file_id)
    content = _text(args.get("content"), "content")
    if args.get("content") is None:
        raise ValueError("content is required")
    mime = _upload_type(args)
    return google_request("PATCH", f"/upload/drive/v3/files/{file_id}",
                          {"uploadType": "media", "fields": "id,name,mimeType,modifiedTime"},
                          body=content.encode("utf-8"), content_type=f"{mime}; charset=UTF-8")


def drive_rename_move(args, a):
    file_id = _id(args.get("file_id"), "file_id")
    new_name = _str(args.get("new_name"), "new_name", 300)
    add = _id(args.get("add_parent_id"), "add_parent_id", required=False)
    remove = _id(args.get("remove_parent_id"), "remove_parent_id", required=False)
    if not (new_name or add or remove):
        raise ValueError("give new_name, add_parent_id or remove_parent_id")
    _note_ids(a, file_id=file_id, add_parent_id=add, remove_parent_id=remove)
    return google_request("PATCH", f"/drive/v3/files/{file_id}",
                          {"addParents": add, "removeParents": remove,
                           "fields": "id,name,parents,webViewLink"},
                          body={"name": new_name} if new_name else {})


def drive_delete_file(args, a):
    file_id = _id(args.get("file_id"), "file_id")
    _note_ids(a, file_id=file_id)
    out = google_request("PATCH", f"/drive/v3/files/{file_id}", {"fields": "id,name,trashed"},
                         body={"trashed": True})
    return {"id": out.get("id"), "name": out.get("name"), "trashed": bool(out.get("trashed")),
            "note": "moved to the Drive trash (restorable there for 30 days)"}


_ROLES = ("reader", "commenter", "writer")


def drive_share_file(args, a):
    file_id = _id(args.get("file_id"), "file_id")
    _note_ids(a, file_id=file_id)
    role = _str(args.get("role"), "role", 20, "reader") or "reader"
    if role not in _ROLES:
        raise ValueError("role must be reader, commenter or writer")
    kind = _str(args.get("type"), "type", 20, "user") or "user"
    perm = {"role": role, "type": kind}
    if kind in ("user", "group"):
        perm["emailAddress"] = _emails([args.get("email")], "email")[0]
        _note_recipients(a, [perm["emailAddress"]])
    elif kind == "domain":
        dom = _str(args.get("domain"), "domain", 253)
        if not _DOMAIN.fullmatch(dom or ""):
            raise ValueError("domain must be a domain name (example.com)")
        perm["domain"] = dom
        a["recipient_count"] = 0
        a["recipient_domains"] = [dom.lower()]
    elif kind == "anyone":
        a["recipient_count"] = 0
        a["recipient_domains"] = ["anyone-with-the-link"]
    else:
        raise ValueError("type must be user, group, domain or anyone")
    if kind not in ("user", "group") and (args.get("email") is not None):
        raise ValueError("email is only for type user or group")
    notify = _bool(args.get("notify"), "notify", True)
    params = {"fields": "id,role,type"}
    if kind in ("user", "group"):
        params["sendNotificationEmail"] = "true" if notify else "false"
        msg = _str(args.get("message"), "message", 1000)
        if msg:
            params["emailMessage"] = msg
    out = google_request("POST", f"/drive/v3/files/{file_id}/permissions", params, body=perm)
    _note_ids(a, permission_id=str(out.get("id") or ""))
    return out


# ---------------------------------------------------------------------------
# Calendar: write (no attendees) / send (attendees) / delete
# ---------------------------------------------------------------------------

def _event_class(args) -> str:
    if not isinstance(args, dict):
        return SEND
    if args.get("attendees") or args.get("notify_attendees") is True:
        return SEND
    return WRITE


def _delete_event_class(args) -> str:
    """Deleting with notify_attendees mails every attendee: send (review 282)."""
    if isinstance(args, dict) and args.get("notify_attendees") in (None, False):
        return DELETE
    return SEND


def _event_body(args, required: bool) -> dict:
    ev = {}
    summary = _str(args.get("summary"), "summary", 500)
    if summary:
        ev["summary"] = summary
    elif required:
        raise ValueError("summary is required")
    for key in ("description",):
        if args.get(key) is not None:
            ev[key] = _text(args.get(key), key)
    if args.get("location") is not None:
        ev["location"] = _str(args.get("location"), "location", 500)
    tz = _str(args.get("time_zone"), "time_zone", 64)
    for key in ("start", "end"):
        when = _when(args.get(key), key, required=required)
        if when is not None:
            if tz and "dateTime" in when:
                when["timeZone"] = tz
            ev[key] = when
    if args.get("attendees") is not None:
        ev["attendees"] = [{"email": e} for e in _emails(args.get("attendees"), "attendees")]
    return ev


def _send_updates(args, a) -> str:
    notify = _bool(args.get("notify_attendees"), "notify_attendees", bool(args.get("attendees")))
    return "all" if notify else "none"


def calendar_create_event(args, a):
    cal = _cal(args)
    ev = _event_body(args, required=True)
    _note_ids(a, calendar_id=cal)
    _note_recipients(a, [x["email"] for x in ev.get("attendees", [])])
    out = google_request("POST", _cal_path(cal), {"sendUpdates": _send_updates(args, a)}, body=ev)
    _note_ids(a, created_id=str(out.get("id") or ""))
    return {"id": out.get("id"), "link": out.get("htmlLink"), "start": out.get("start"),
            "end": out.get("end"), "attendees": len(out.get("attendees") or [])}


def calendar_update_event(args, a):
    cal = _cal(args)
    eid = _id(args.get("event_id"), "event_id", _EVENT_ID)
    ev = _event_body(args, required=False)
    if not ev:
        raise ValueError("give at least one field to change")
    _note_ids(a, calendar_id=cal, event_id=eid)
    if "attendees" in ev:
        _note_recipients(a, [x["email"] for x in ev["attendees"]])
    out = google_request("PATCH", f"{_cal_path(cal)}/{urllib.parse.quote(eid, safe='')}",
                         {"sendUpdates": _send_updates(args, a)}, body=ev)
    return {"id": out.get("id"), "link": out.get("htmlLink"), "start": out.get("start"),
            "end": out.get("end"), "attendees": len(out.get("attendees") or [])}


def calendar_delete_event(args, a):
    cal = _cal(args)
    eid = _id(args.get("event_id"), "event_id", _EVENT_ID)
    _note_ids(a, calendar_id=cal, event_id=eid)
    notify = _bool(args.get("notify_attendees"), "notify_attendees", False)
    google_request("DELETE", f"{_cal_path(cal)}/{urllib.parse.quote(eid, safe='')}",
                   {"sendUpdates": "all" if notify else "none"}, raw=True)
    return {"id": eid, "deleted": True}


# ---------------------------------------------------------------------------
# Gmail: draft (write), send / reply / forward (send)
# ---------------------------------------------------------------------------

def _compose(args, a, *, need_to: bool) -> dict:
    """The Gmail API body {raw, threadId?} of a new message, a reply or a
    forward. Addresses are checked (plain name@domain), headers are
    single-line, so nothing in the arguments can add a header."""
    to = _emails(args.get("to"), "to")
    cc = _emails(args.get("cc"), "cc")
    bcc = _emails(args.get("bcc"), "bcc")
    subject = _str(args.get("subject"), "subject", 998, None)
    body = _text(args.get("body"), "body")
    reply_to = _id(args.get("reply_to_message_id"), "reply_to_message_id", _MSG_ID, required=False)
    forward = _id(args.get("forward_message_id"), "forward_message_id", _MSG_ID, required=False)
    if reply_to and forward:
        raise ValueError("give reply_to_message_id or forward_message_id, not both")
    thread_id = None
    msg = EmailMessage()
    if reply_to:
        _note_ids(a, reply_to_message_id=reply_to)
        orig = google_get(f"{_GMAIL}/messages/{reply_to}", {
            "format": "metadata",
            "metadataHeaders": ["From", "Subject", "Message-ID", "References"]})
        h = _headers(orig)
        thread_id = orig.get("threadId")
        if not to:
            # The sender (From), never a Reply-To the sender picked; the result
            # names who it went to (review 282).
            to = [addr for _n, addr in getaddresses([h.get("from") or ""])
                  if _EMAIL.fullmatch(addr or "")]
        if subject is None:
            s = h.get("subject", "")
            subject = s if s.lower().startswith("re:") else f"Re: {s}".strip()
        mid = h.get("message-id", "")
        if mid and "\n" not in mid and "\r" not in mid:
            msg["In-Reply-To"] = mid
            refs = (h.get("references", "") + " " + mid).strip()
            msg["References"] = refs.replace("\r", " ").replace("\n", " ")[:4000]
    if forward:
        _note_ids(a, forward_message_id=forward)
        orig = _get_message(forward)
        h = _headers(orig)
        if subject is None:
            s = h.get("subject", "")
            subject = s if s.lower().startswith("fwd:") else f"Fwd: {s}".strip()
        quoted = _plain_text(orig.get("payload") or {})
        body = (f"{body}\n\n---------- Forwarded message ---------\nFrom: {h.get('from', '')}\n"
                f"Date: {h.get('date', '')}\nSubject: {h.get('subject', '')}\nTo: {h.get('to', '')}"
                f"\n\n{quoted}")
        if len(body.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("the forwarded message is too long to forward as text")
    if need_to and not (to or cc or bcc):
        raise ValueError("give at least one recipient (to, cc or bcc)")
    if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
        raise ValueError(f"at most {MAX_RECIPIENTS} recipients")
    _note_recipients(a, to + cc + bcc)
    a["_recipients"] = to + cc + bcc
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = (subject or "").replace("\r", " ").replace("\n", " ")
    msg.set_content(body)
    out = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")}
    if thread_id and _MSG_ID.fullmatch(str(thread_id)):
        out["threadId"] = thread_id
    return out


def gmail_create_draft(args, a):
    message = _compose(args, a, need_to=False)
    out = google_request("POST", f"{_GMAIL}/drafts", body={"message": message})
    _note_ids(a, created_id=str(out.get("id") or ""))
    return {"draft_id": out.get("id"), "message_id": (out.get("message") or {}).get("id"),
            "recipients": a.get("_recipients", [])}


def gmail_send_message(args, a):
    message = _compose(args, a, need_to=True)
    out = google_request("POST", f"{_GMAIL}/messages/send", body=message)
    _note_ids(a, created_id=str(out.get("id") or ""))
    return {"message_id": out.get("id"), "threadId": out.get("threadId"), "sent": True,
            "recipients": a.get("_recipients", [])}


# ---------------------------------------------------------------------------
# The tool table
# ---------------------------------------------------------------------------

def _schema(props: dict, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


_S = {"type": "string"}
_EMAILS = {"type": "array", "items": {"type": "string"}, "maxItems": MAX_RECIPIENTS}
_MAIL = {
    "to": _EMAILS, "cc": _EMAILS, "bcc": _EMAILS, "subject": _S,
    "body": {"type": "string", "description": "plain text"},
    "reply_to_message_id": {"type": "string", "description": "reply to this message (same thread; "
                            "default recipient: its sender)"},
    "forward_message_id": {"type": "string", "description": "forward this message's text"}}
_EVENT = {
    "calendar_id": {"type": "string", "description": "default: primary"},
    "summary": _S, "description": _S, "location": _S,
    "start": {"type": "string", "description": "RFC 3339 date-time, or YYYY-MM-DD for all day"},
    "end": {"type": "string", "description": "RFC 3339 date-time, or YYYY-MM-DD for all day"},
    "time_zone": {"type": "string", "description": "IANA time zone, e.g. Europe/Vienna"},
    "attendees": {**_EMAILS, "description": "email addresses to invite (they are notified)"},
    "notify_attendees": {"type": "boolean", "description": "email the attendees (default: when "
                         "attendees are given)"}}

#: name -> (function, class, description, input schema). A class may be a
#: function of the arguments (events: send when they invite someone).
TOOLS = {
    "calendar_list_calendars": (calendar_list_calendars, READ, "List your Google calendars.",
                                _schema({})),
    "calendar_list_events": (calendar_list_events, READ, "List events of one of your calendars.",
                             _schema({
                                 "calendar_id": {"type": "string", "description": "default: primary"},
                                 "time_min": {"type": "string", "description": "RFC 3339 lower bound"},
                                 "time_max": {"type": "string", "description": "RFC 3339 upper bound"},
                                 "query": _S,
                                 "max_results": {"type": "integer", "minimum": 1, "maximum": 50}})),
    "drive_search_files": (drive_search_files, READ, "Search your Google Drive by full text.", _schema({
        "query": _S, "max_results": {"type": "integer", "minimum": 1, "maximum": 50}}, ("query",))),
    "drive_read_file": (drive_read_file, READ, "Read a Google Doc, Sheet, Slides deck or text file "
                        "from your Drive as text (at most 1 MiB).",
                        _schema({"file_id": _S}, ("file_id",))),
    "gmail_search_messages": (gmail_search_messages, READ, "Search your Gmail (Gmail search syntax, "
                              "e.g. 'from:anna is:unread').", _schema({
                                  "query": _S,
                                  "max_results": {"type": "integer", "minimum": 1, "maximum": 25}})),
    "gmail_read_message": (gmail_read_message, READ, "Read one of your Gmail messages as text "
                           "(at most 1 MiB).", _schema({"message_id": _S}, ("message_id",))),
    "drive_create_file": (drive_create_file, WRITE, "Create a file in your Drive from text; with "
                          "convert_to 'document' or 'spreadsheet' (from CSV) it becomes a Google "
                          "Doc or Sheet.", _schema({
                              "name": _S, "content": _S,
                              "mime_type": {"type": "string", "enum": list(_UPLOAD_TYPES)},
                              "convert_to": {"type": "string", "enum": list(_CONVERT)},
                              "folder_id": _S}, ("name", "content"))),
    "drive_update_file": (drive_update_file, WRITE, "Replace the content of a file in your Drive "
                          "with text.", _schema({
                              "file_id": _S, "content": _S,
                              "mime_type": {"type": "string", "enum": list(_UPLOAD_TYPES)}},
                              ("file_id", "content"))),
    "drive_rename_move": (drive_rename_move, WRITE, "Rename a Drive file or move it to another "
                          "folder.", _schema({"file_id": _S, "new_name": _S, "add_parent_id": _S,
                                              "remove_parent_id": _S}, ("file_id",))),
    "drive_delete_file": (drive_delete_file, DELETE, "Move a Drive file to the trash.",
                          _schema({"file_id": _S}, ("file_id",))),
    "drive_share_file": (drive_share_file, SEND, "Share a Drive file with a person, a group, a "
                         "domain or anyone with the link.", _schema({
                             "file_id": _S,
                             "type": {"type": "string", "enum": ["user", "group", "domain", "anyone"]},
                             "email": _S, "domain": _S,
                             "role": {"type": "string", "enum": list(_ROLES)},
                             "notify": {"type": "boolean", "description": "email the person "
                                        "(default true)"},
                             "message": _S}, ("file_id",))),
    "calendar_create_event": (calendar_create_event, _event_class, "Create an event in one of your "
                              "calendars; attendees are invited.", _schema(_EVENT,
                                                                           ("summary", "start", "end"))),
    "calendar_update_event": (calendar_update_event, _event_class, "Change an event (only the "
                              "fields given; attendees replaces the list).",
                              _schema({**_EVENT, "event_id": _S}, ("event_id",))),
    "calendar_delete_event": (calendar_delete_event, _delete_event_class, "Delete an event.", _schema({
        "calendar_id": {"type": "string", "description": "default: primary"}, "event_id": _S,
        "notify_attendees": {"type": "boolean", "description": "email the attendees a "
                             "cancellation (default false)"}}, ("event_id",))),
    "gmail_create_draft": (gmail_create_draft, WRITE, "Create a Gmail draft (a new message, a "
                           "reply or a forward); nothing is sent.", _schema(_MAIL)),
    "gmail_send_message": (gmail_send_message, SEND, "Send an email from your Gmail: a new message, "
                           "a reply (reply_to_message_id) or a forward (forward_message_id).",
                           _schema(_MAIL)),
}


def tool_class(name: str, args=None) -> str:
    """The class of a call. Unknown tool = the strictest class."""
    entry = TOOLS.get(name)
    if entry is None:
        return STRICTEST
    cls = entry[1]
    return cls(args if isinstance(args, dict) else {}) if callable(cls) else cls


def _event_has_guests(args) -> bool:
    """The stored event, read with the person's own token: does anyone but
    the person take part? Unreadable = yes (fail closed). An answer that does
    not name the requested event (`{}` included) is not that event: yes (#3466)."""
    cal = _cal(args)
    eid = _id(args.get("event_id"), "event_id", _EVENT_ID)
    try:
        ev = google_get(f"{_cal_path(cal)}/{urllib.parse.quote(eid, safe='')}",
                        {"fields": "id,attendees(email,self)"})
        if ev.get("id") != eid:
            return True
        people = ev.get("attendees")
        if people is None:
            return False
        return any(not (isinstance(x, dict) and x.get("self") is True) for x in people)
    except Exception:  # noqa: BLE001 — cannot tell: treat as having guests
        return True


def _folder_is_shared(folder: str) -> bool:
    """A target folder that anyone else can see (shared, or on a shared
    drive): putting a file there shares it. Unreadable/unknown = shared."""
    try:
        f = google_get(f"/drive/v3/files/{folder}",
                       {"fields": "id,shared,driveId", "supportsAllDrives": "true"})
        if f.get("driveId"):
            return True
        return f.get("shared") is not False
    except Exception:  # noqa: BLE001 — cannot tell: treat as shared
        return True


def call_class(name: str, args: dict) -> str:
    """The class of THIS call, reading the stored object where the arguments
    alone cannot tell (review 282): changing an event that has guests, and
    putting a file into a folder others can see, reach other people (send).
    Raises ValueError for an argument the tool would refuse anyway, before
    anything is read."""
    cls = tool_class(name, args)
    if cls != WRITE:
        return cls
    if name == "calendar_update_event":
        if not _event_body(args, required=False):
            raise ValueError("give at least one field to change")
        return SEND if _event_has_guests(args) else WRITE
    if name in ("drive_create_file", "drive_rename_move"):
        key = "folder_id" if name == "drive_create_file" else "add_parent_id"
        folder = _id(args.get(key), key, required=False)
        if folder:
            return SEND if _folder_is_shared(folder) else WRITE
    return cls


def listed_class(name: str) -> str:
    """The class a tool is listed under: its class with no arguments (events
    without attendees: write; deleting an event without notifying: delete)."""
    return tool_class(name, {})


def visible_tools(caller: str, s: dict | None = None) -> list:
    s = settings() if s is None else s
    return [n for n in TOOLS if not decide(listed_class(n), caller, s)]


UNKNOWN_TEXT = ("No answer from Google for '{tool}': it may or may not have been done. Check in "
                "Google before trying again.")
UNREADABLE_TEXT = ("Google accepted '{tool}', but its answer could not be read. Check the result "
                   "in Google.")


def call_tool(name, args, caller: str = AGENT):
    """(result, is_error). Unknown keys are refused, so no argument can carry
    a credential or steer the upstream. Every call that is not a read is
    audited, whatever happens: an `attempted` line before the first request
    that changes something (google_request; no line, no request), and one
    outcome line after it. Nothing raised in a tool escapes."""
    try:
        return _call_tool(name, args, caller)
    except Exception:  # noqa: BLE001 — e.g. an outcome line that cannot be written
        return "The call failed inside the proxy; check in Google before trying again.", True
    finally:
        _CALL.ctx = None


def _call_tool(name, args, caller):
    if name not in TOOLS:
        if isinstance(name, str):
            audit(audit_tool_name(name), STRICTEST, caller, "unknown_tool", {})
        return f"unknown tool {name!r}", True
    fn, _cls, _desc, schema = TOOLS[name]
    fields: dict = {"target_ids": {}}
    if not isinstance(args, dict):
        cls = tool_class(name, {})
        if cls != READ:
            audit(name, cls, caller, "invalid_arguments", fields)
        return "arguments must be an object", True
    cls = tool_class(name, args)
    extra = sorted(set(args) - set(schema["properties"]))
    missing = [k for k in schema["required"] if args.get(k) is None]
    if extra or missing:
        if cls != READ:
            audit(name, cls, caller, "invalid_arguments", fields)
        if extra:
            return f"unknown argument(s): {', '.join(extra)}", True
        return f"missing argument(s): {', '.join(missing)}", True
    # A refusal by the arguments alone needs no read: reading the stored
    # object can only make a call stricter (write -> send).
    refusal = decide(cls, caller, settings())
    if refusal:
        audit(name, cls, caller, refusal, fields)
        return REFUSALS[refusal].format(tool=name), True
    try:
        cls = call_class(name, args)
    except ValueError as e:
        audit(name, cls, caller, "invalid_arguments", fields)
        return str(e), True
    except Exception:  # noqa: BLE001 — cannot classify: the strictest class
        cls = STRICTEST
    refusal = decide(cls, caller, settings())
    if refusal:
        audit(name, cls, caller, refusal, fields)
        return REFUSALS[refusal].format(tool=name), True
    _begin_call(name, cls, caller, fields)
    ctx = _call_ctx()
    try:
        result = fn(args, fields)
        decision, out = "allowed", (result, False)
    except ScopeMissing:
        decision, out = "reconnect_required", (_reconnect(cls != READ), True)
    except OutcomeUnknown:
        decision, out = "outcome_unknown", (UNKNOWN_TEXT.format(tool=name), True)
    except ValueError as e:
        decision, out = "invalid_arguments", (str(e), True)
    except GoogleError as e:
        decision = "upstream_refused" if ctx["attempted"] else "upstream_error"
        out = (str(e), True)
    except Exception as e:  # noqa: BLE001 — never escapes, never echoed
        if ctx["attempted"] and isinstance(ctx["status"], int) and 200 <= ctx["status"] < 300:
            # Google did it (2xx); only its answer was not what we expected.
            decision, out = "allowed", (UNREADABLE_TEXT.format(tool=name), True)
        elif ctx["attempted"]:
            decision, out = "outcome_unknown", (UNKNOWN_TEXT.format(tool=name), True)
        else:
            decision = "internal_error"
            out = (f"'{name}' failed inside the proxy; nothing was changed in Google.", True)
    if ctx["attempted"] and decision == "invalid_arguments":
        decision = "upstream_error"
    if cls != READ or ctx["attempted"]:
        fields.pop("_recipients", None)
        audit(name, cls, caller, decision, fields)
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "razzfazz-mcp-proxy"
    sys_version = ""

    def _send(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _caller(self) -> str | None:
        """AGENT for the route secret, CHAT for the gateway's own bearer, None
        otherwise. Without PROXY_AUTH_TOKEN nobody is admitted."""
        expected = _env("PROXY_AUTH_TOKEN")
        if not expected:
            return None
        presented = self.headers.get("Authorization", "").encode("latin-1", "ignore")
        if hmac.compare_digest(presented, f"Bearer {expected}".encode("latin-1", "ignore")):
            return AGENT
        gateway = _env("PROXY_GATEWAY_TOKEN")
        if gateway and hmac.compare_digest(presented, f"Bearer {gateway}".encode("latin-1", "ignore")):
            return CHAT
        return None

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"status": "ok"})
        if self.path.split("?", 1)[0].rstrip("/") == "/mcp":
            return self._send(405, {"error": "no event stream on this server"})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        return self._send(405, {"error": "stateless server"})

    def do_POST(self):
        if self.path.split("?", 1)[0].rstrip("/") != "/mcp":
            return self._send(404, {"error": "not found"})
        caller = self._caller()
        if caller is None:
            return self._send(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return self._send(400, {"error": "bad content-length"})
        if length > MAX_BODY:
            return self._send(413, {"error": "request too large"})
        try:
            req = json.loads(self.rfile.read(length) if length else b"")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if not isinstance(req, dict):
            return self._send(400, {"error": "one JSON-RPC message per request"})
        method, rid = str(req.get("method", "")), req.get("id")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        if method.startswith("notifications/"):
            return self._send(202)
        if method == "initialize":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "serverInfo": {"name": "razzfazz-mcp-proxy-google", "version": "2"},
                "capabilities": {"tools": {}}}})
        if method == "ping":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {}})
        if method == "tools/list":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": n, "description": TOOLS[n][2], "inputSchema": TOOLS[n][3]}
                for n in visible_tools(caller)]}})
        if method == "tools/call":
            result, is_error = call_tool(params.get("name"), params.get("arguments") or {}, caller)
            text = result if isinstance(result, str) else json.dumps(result)
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}], "isError": is_error}})
        return self._send(200, {"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32601, "message": f"unknown method {method}"}})

    def log_message(self, *a):  # never log request lines (no URLs, no headers)
        pass


def main() -> int:
    kind = _env("MCP_TARGET_KIND")
    if kind != TARGET_KIND:
        print(f"razzfazz-mcp-proxy: this build serves MCP_TARGET_KIND={TARGET_KIND!r} only, "
              f"not {kind!r}", file=sys.stderr, flush=True)
        return 2
    if not _env("PROXY_AUTH_TOKEN"):
        print("razzfazz-mcp-proxy: PROXY_AUTH_TOKEN is not set; refusing to start",
              file=sys.stderr, flush=True)
        return 2
    s = settings()
    for key in s["invalid"]:
        print(f"razzfazz-mcp-proxy: {key} has an invalid value; read as the strictest "
              f"(read-only / false)", file=sys.stderr, flush=True)
    port = int(_env("PORT") or "8080")
    print(f"razzfazz-mcp-proxy ({TARGET_KIND}) listening on :{port}; mode {s['mode']}, send "
          f"{'on' if s['allow_send'] else 'off'}, send/delete by agents "
          f"{'on' if s['agents'] else 'off'}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
