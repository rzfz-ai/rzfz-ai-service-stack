# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""In-process runtime state of the MCP gateway (#2472, fix round 1).

mcp-manager runs ONE gunicorn process with threads (gunicorn.conf.py), so
this state is process-wide and every structure here is guarded by a lock.

* `NoCookieJar`       — the pooled upstream client's jar: it never stores and
                        never sends a cookie, so no upstream session crosses
                        from one caller to the next (review C1).
* `Limiter`           — concurrent relays per caller and in total (review I2).
* `SessionBindings`   — an upstream `Mcp-Session-Id` belongs to the caller it
                        was issued to (review M1).
* `AnonymousLog`      — anonymous refusals are counted and logged at most once
                        per peer per minute, never written to the database
                        (a flooding vector; accepted deviation from B5 §7).
* `parse_message`     — the strict JSON-RPC envelope parser (review I1)."""
from __future__ import annotations

import http.cookiejar
import json
import logging
import math
import threading
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# C1: a cookie jar that is always empty
# ---------------------------------------------------------------------------

class NoCookieJar(http.cookiejar.CookieJar):
    """Stores nothing, sends nothing. Stateless by construction: there is no
    per-request clearing for 64 threads to race on."""

    def set_cookie(self, cookie):  # noqa: D102
        return None

    def set_cookie_if_ok(self, cookie, request):  # noqa: D102
        return None

    def extract_cookies(self, response, request):  # noqa: D102
        return None

    def make_cookies(self, response, request):  # noqa: D102
        return []

    def add_cookie_header(self, request):  # noqa: D102
        return None


# ---------------------------------------------------------------------------
# I1: the strict envelope parser
# ---------------------------------------------------------------------------

class BadMessage(ValueError):
    pass


#: The JSON-RPC / MCP field names the gateway reads. A key that folds to one of
#: these but is not spelled exactly so is refused: Go's encoding/json (gitea-mcp,
#: mcp-go) matches struct fields case-insensitively, with Unicode simple
#: folding (`ſ`→`s`, `K`→`k`), so it would read a field Python does not.
KNOWN_FIELDS = ("jsonrpc", "id", "method", "params", "name", "arguments", "result", "error", "_meta")
_FOLDED = {f.casefold(): f for f in KNOWN_FIELDS}
#: The only keys a message envelope may carry (a request, a notification, or a
#: client's response to a server request).
ENVELOPE_KEYS = frozenset({"jsonrpc", "id", "method", "params", "result", "error"})
#: The only keys `tools/call` params may carry (MCP CallToolRequest, including
#: the spec's task-augmented and multi-round-trip fields).
TOOLS_CALL_KEYS = frozenset({"name", "arguments", "_meta", "task", "inputResponses", "requestState"})


def _fold(key: str) -> str:
    # casefold() is a superset of Go's simple folding (it also folds ß→ss,
    # which Go would not): refusing more is the safe direction.
    return key.casefold()


def _pairs(pairs):
    """object_pairs_hook: no duplicate key, and no two keys that fold alike
    (Go would pick one of them, Python the other)."""
    seen = {}
    for k, _v in pairs:
        f = _fold(k)
        if f in seen:
            raise BadMessage(f"ambiguous keys {seen[f]!r} and {k!r}")
        seen[f] = k
    return dict(pairs)


def _reject_constant(name):
    raise BadMessage(f"non-JSON number {name}")


def _finite_float(text):
    """`1e400` parses to inf in Python and cannot be re-serialised as JSON."""
    value = float(text)
    if not math.isfinite(value):
        raise BadMessage(f"number out of range: {text[:32]}")
    return value


def _check_spelling(obj: dict, where: str):
    for k in obj:
        want = _FOLDED.get(_fold(k))
        if want is not None and k != want:
            raise BadMessage(f"{where}: key {k!r} is not spelled {want!r}")


def parse_message(body: bytes):
    """One JSON-RPC message, strictly. Returns (msg, method, tool, args).

    Refuses (BadMessage): invalid JSON, NaN/Infinity, a duplicate or
    case-folded-duplicate key at ANY depth, a batch, a key outside the
    envelope, a mis-cased known field in the envelope or in `params`, and
    `tools/call` params outside {name, arguments, _meta}. The caller forwards
    `canonical(msg)`, never the raw bytes, so the tool the gateway audits and
    guards is exactly the one the upstream reads."""
    try:
        msg = json.loads(body or b"null", object_pairs_hook=_pairs,
                         parse_constant=_reject_constant, parse_float=_finite_float)
    except BadMessage:
        raise
    except RecursionError:
        raise BadMessage("nested too deeply") from None
    except ValueError as e:
        raise BadMessage(f"not JSON: {e.__class__.__name__}") from None
    if isinstance(msg, list):
        raise BadMessage("one JSON-RPC message per request (no batches)")
    if not isinstance(msg, dict):
        raise BadMessage("one JSON-RPC message per request")
    _check_spelling(msg, "envelope")
    extra = set(msg) - ENVELOPE_KEYS
    if extra:
        raise BadMessage(f"envelope: unexpected keys {sorted(extra)}")
    method = msg.get("method")
    if method is not None and not isinstance(method, str):
        raise BadMessage("method must be a string")
    params = msg.get("params")
    if params is not None and not isinstance(params, (dict, list)):
        raise BadMessage("params must be an object or an array")
    tool, args = None, {}
    if isinstance(params, dict):
        _check_spelling(params, "params")
    if method == "tools/call":
        if not isinstance(params, dict):
            raise BadMessage("tools/call needs params")
        extra = set(params) - TOOLS_CALL_KEYS
        if extra:
            raise BadMessage(f"tools/call params: unexpected keys {sorted(extra)}")
        tool = params.get("name")
        if not isinstance(tool, str) or not tool:
            raise BadMessage("tools/call needs a tool name")
        args = params.get("arguments", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise BadMessage("tools/call arguments must be an object")
    return msg, (method or ""), tool, args


def canonical(msg) -> bytes:
    """The bytes the upstream receives: the parsed message, re-serialised.
    Strict JSON (no NaN/Infinity); anything that cannot be written back is a
    BadMessage, never a 500."""
    try:
        return json.dumps(msg, allow_nan=False).encode("utf-8")
    except RecursionError:
        raise BadMessage("nested too deeply") from None
    except (ValueError, TypeError) as e:
        raise BadMessage(f"cannot be re-serialised: {e.__class__.__name__}") from None


# ---------------------------------------------------------------------------
# I2: concurrency caps
# ---------------------------------------------------------------------------

class Lease:
    """One held relay slot. release() is idempotent."""

    def __init__(self, limiter, key):
        self._limiter, self._key, self._done = limiter, key, False
        self._lock = threading.Lock()

    def release(self):
        with self._lock:
            if self._done:
                return
            self._done = True
        self._limiter._release(self._key)


class Limiter:
    def __init__(self, per_caller: int, total: int):
        self.per_caller, self.total = int(per_caller), int(total)
        self._lock = threading.Lock()
        self._by_key: dict = {}
        self._held = 0
        self.refused = {"per_caller": 0, "total": 0}

    def acquire(self, key):
        """(Lease, None) or (None, "per_caller" | "total")."""
        with self._lock:
            if self._by_key.get(key, 0) >= self.per_caller:
                self.refused["per_caller"] += 1
                return None, "per_caller"
            if self._held >= self.total:
                self.refused["total"] += 1
                return None, "total"
            self._by_key[key] = self._by_key.get(key, 0) + 1
            self._held += 1
        return Lease(self, key), None

    def _release(self, key):
        with self._lock:
            n = self._by_key.get(key, 0) - 1
            if n > 0:
                self._by_key[key] = n
            else:
                self._by_key.pop(key, None)
            self._held = max(0, self._held - 1)

    def held(self) -> int:
        with self._lock:
            return self._held


# ---------------------------------------------------------------------------
# M1: Mcp-Session-Id belongs to the caller it was issued to
# ---------------------------------------------------------------------------

class SessionBindings:
    """(server id, session id) -> caller key, with a sliding TTL and a size
    bound. A session the gateway does not know (expired, evicted, or issued
    before a restart) is refused like a foreign one: MCP clients answer a 404
    on their session by initializing a new one."""

    def __init__(self, ttl: float = 12 * 3600, max_entries: int = 20000, clock=time.monotonic):
        self.ttl, self.max_entries, self._clock = ttl, max_entries, clock
        self._lock = threading.Lock()
        self._map: OrderedDict = OrderedDict()

    def bind(self, server_id, session_id, key):
        now = self._clock()
        with self._lock:
            self._map[(server_id, session_id)] = (key, now + self.ttl)
            self._map.move_to_end((server_id, session_id))
            while len(self._map) > self.max_entries:
                self._map.popitem(last=False)

    def owner_ok(self, server_id, session_id, key) -> bool:
        now = self._clock()
        with self._lock:
            hit = self._map.get((server_id, session_id))
            if not hit or hit[1] < now:
                self._map.pop((server_id, session_id), None)
                return False
            if hit[0] != key:
                return False
            self._map[(server_id, session_id)] = (key, now + self.ttl)
            self._map.move_to_end((server_id, session_id))
            return True

    def unbind(self, server_id, session_id, key):
        with self._lock:
            hit = self._map.get((server_id, session_id))
            if hit and hit[0] == key:
                del self._map[(server_id, session_id)]


# ---------------------------------------------------------------------------
# Anonymous refusals: counted, rate-limited log, never a DB row
# ---------------------------------------------------------------------------

class AnonymousLog:
    def __init__(self, window: float = 60.0, max_peers: int = 4096, clock=time.monotonic, log=None):
        self.window, self.max_peers, self._clock = window, max_peers, clock
        self._log = log or logger
        self._lock = threading.Lock()
        self._peers: OrderedDict = OrderedDict()   # peer -> [last_logged, suppressed]
        self.total = 0

    def refused(self, peer: str, reason: str):
        now = self._clock()
        line = None
        with self._lock:
            self.total += 1
            ent = self._peers.get(peer)
            if ent is None or now - ent[0] >= self.window:
                suppressed = ent[1] if ent else 0
                self._peers[peer] = [now, 0]
                line = (peer, reason, suppressed)
            else:
                ent[1] += 1
            self._peers.move_to_end(peer)
            while len(self._peers) > self.max_peers:
                self._peers.popitem(last=False)
        if line:
            self._log.warning("gateway: anonymous request refused from %s (%s); "
                              "%d more from this peer since its last line", *line)


# ---------------------------------------------------------------------------
# Rejections of KNOWN tokens: audited, at most one row per token per window
# ---------------------------------------------------------------------------

class RejectionThrottle:
    """#2472: a token this gateway issued that is presented after it was
    revoked (or after its technical user was) is a security event — a leaked
    or forgotten credential still in use — so it is audited under its
    principal. A client that retries in a loop (Dify does) must not turn that
    into a row per request: one row per token per `window` seconds, and the
    next row says how many were suppressed meanwhile. Unknown tokens never
    reach this (they name no principal; AnonymousLog counts them)."""

    def __init__(self, window: float = 60.0, max_tokens: int = 4096, clock=time.monotonic):
        self.window, self.max_tokens, self._clock = window, max_tokens, clock
        self._lock = threading.Lock()
        self._tokens: OrderedDict = OrderedDict()   # token id -> [last_recorded, suppressed]

    def admit(self, token_id):
        """The number of rejections suppressed since this token's last row
        when a row is due now, else None (count it, write nothing)."""
        now = self._clock()
        with self._lock:
            ent = self._tokens.get(token_id)
            if ent is None or now - ent[0] >= self.window:
                suppressed = ent[1] if ent else 0
                self._tokens[token_id] = [now, 0]
                due = suppressed
            else:
                ent[1] += 1
                due = None
            self._tokens.move_to_end(token_id)
            while len(self._tokens) > self.max_tokens:
                self._tokens.popitem(last=False)
        return due
