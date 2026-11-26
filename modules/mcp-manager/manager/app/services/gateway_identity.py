# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Who is calling the MCP gateway (#2472; design record
docs/superpowers/specs/2026-10-mcp-rework-design.md §2, A1 D7/D8).

Three caller shapes, one result (`Caller`):
  * agents     — a per-user gateway token `rzmcp_…` delivered in the agent
                 wiring (B8);
  * Dify       — a technical user's gateway token `rzmcp_…` (Dify 1.17.1 does
                 not pass its end user through);
  * Open WebUI — the chat user's own session bearer (`auth_type: session`),
                 which we never decode: Open WebUI says whose it is (an email),
                 and Authentik says which ONE person has that email (their uid).

A person is identified by the OIDC `sub` (the Authentik `uid`) and nothing
else: `Caller.principal` for a person IS the sub, and every credential key is
the sub. Never a username, never a slug, never an email turned into one. A
technical principal is `~svc-<name>`; a sub is hex and a slug never carries
`~`, so the three can never collide. Tokens are stored as SHA-256 only.

Authentik access (design record §7, ruling 2): mcp-manager reads Authentik
ONLY through a dedicated READ-ONLY token (A17 creates it), in
`MCP_MANAGER_AUTHENTIK_READONLY_TOKEN`, and only with
`GET /api/v3/core/users/`. It never uses `AUTHENTIK_BOOTSTRAP_TOKEN`, even
though the container's env_file hands it that key: with the read-only key
empty there is no directory at all (fail closed — no person resolves by
email, no pre-upgrade row is re-keyed or quarantined).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "rzmcp_"
TECH_PREFIX = "~"
CONSUMERS = ("agent", "openwebui", "dify")
#: A technical user's tokens are minted for these consumers only (§2.4).
TECH_CONSUMERS = ("agent", "dify")
GW_SECRET_ID = "_gateway"
#: The ONE env key the directory reads. Empty by default; A17 fills it.
READONLY_TOKEN_ENV = "MCP_MANAGER_AUTHENTIK_READONLY_TOKEN"
AUTHENTIK_BASE_DEFAULT = "http://authentik-server:9000"
#: A17's NAME_RE (core/config/app/services/technical_users.py), A1 D7.
_TECH_NAME = re.compile(r"^svc-[a-z0-9-]{3,40}$")
#: The names A19 reserves for the stack's own AppRoles (A1 D7).
RESERVED_TECH_NAMES = frozenset({"svc-agent-manager", "svc-mcp-manager"})
_USERS_PATH = "/api/v3/core/users/"
#: A1 D6: a knowledge scope is an Authentik group with
#: `attributes.rzfz_kb_scope: true` (by convention named `KB: <scope>`; the
#: name is not checked, the flag decides, as in the Open WebUI claim and the
#: Dify/cognee dataset reconcile); its cognee datasets are listed in
#: `attributes.rzfz_kb_targets.cognee_datasets` (A14).
#: The compact per-user field the listing keeps instead of `groups_obj`:
#: ((scope group name, frozenset of cognee datasets), ...) — KB scopes only,
#: so the cached listing stays bounded however many groups a user has.
KB_FIELD = "_rzfz_kb_scopes"
#: The user's tier-relevant group names (tier.TIER_GROUPS), kept for the
#: gateway's call-time min_tier check (B9 fix round 2).
TIER_FIELD = "_rzfz_tier_groups"
_MAX_KB_SCOPES_PER_USER = 64
_PAGE_SIZE = 100
_MAX_PAGES = 500


# ---------------------------------------------------------------------------
# Shared HTTP clients (B8 fix round 4)
# ---------------------------------------------------------------------------
# ONE pooled httpx.Client per target, built on first use, shared by every
# thread (httpx.Client is thread-safe). A client per call costs ~0.1 s of CPU
# for its TLS context, and those builds serialise on the GIL: 80 Open WebUI
# calls against a HEALTHY Authentik took 47-75 s each (B8 re-review 3), the
# same effect B7 measured for the relay client.
#
# Cookie jars (B7's rule): both clients get a NoCookieJar.
#   * "openwebui" NEEDS it: every request carries one user's Open WebUI
#     session bearer, and Open WebUI answers /api/v1/auths/ with a Set-Cookie
#     of that session token. A shared jar would send user A's session cookie
#     on user B's request.
#   * "authentik" carries only the service's read-only token, never user
#     traffic, so it does not strictly need it; it gets one anyway, because
#     nothing the directory asks depends on a cookie and a stored one could
#     only ever be a surprise.
# Redirects are never followed (a 3xx is a failure, not a new target).
_OPENWEBUI_TIMEOUT = 5.0
_CLIENT_SPECS = {
    # (timeout, connect, pool, max_connections, max_keepalive)
    # Open WebUI is asked before the relay slot, at most
    # OWUI_CONCURRENT_CHECKS at a time.
    "openwebui": (_OPENWEBUI_TIMEOUT, 3.0, 5.0, 16, 16),
    # Authentik is listed single-flight; B6's migration and the agent wiring
    # list on their own, so a few connections cover it. Each request still
    # passes the directory's PAGE_TIMEOUT.
    "authentik": (10.0, 5.0, 10.0, 8, 4),
}
_CLIENTS: dict = {}
_CLIENTS_LOCK = threading.Lock()

#: At most this many Open WebUI session checks run at once (B8 fix round 5).
#: The check runs BEFORE the relay slot (its answer is the slot's key), so
#: without a bound a hanging Open WebUI would park one worker thread per
#: call for the 5 s timeout; with it, the 17th concurrent call is refused at
#: once (OpenWebUIBusy -> the gateway's 503) and at most 16 threads wait.
#: The number is part of the worker's thread budget (services/thread_budget.py).
from app.services.thread_budget import OWUI_CONCURRENT_CHECKS  # noqa: E402
_OWUI_CHECKS = threading.BoundedSemaphore(OWUI_CONCURRENT_CHECKS)


class OpenWebUIBusy(Exception):
    """Too many Open WebUI session checks are already running: refused now,
    never queued."""


class _Flight:
    """One listing in progress: its pages read so far, its last progress,
    and (once done) its result. Waiters sleep on `cond`."""

    def __init__(self, now):
        self.cond = threading.Condition()
        self.started = self.progress = now
        self.pages = 0
        self.done = False
        self.users = None

    def page(self):
        with self.cond:
            self.pages += 1
            self.progress = time.monotonic()
            self.cond.notify_all()

    def finish(self, users):
        with self.cond:
            self.users = users
            self.done = True
            self.cond.notify_all()


def shared_client(target: str):
    """The process's one httpx.Client for `target` ("openwebui" | "authentik")."""
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(target)
        if client is None:
            import httpx
            from app.services.gateway_runtime import NoCookieJar
            read, connect, pool, conns, keep = _CLIENT_SPECS[target]
            client = _CLIENTS[target] = httpx.Client(
                follow_redirects=False, cookies=NoCookieJar(),
                timeout=httpx.Timeout(read, connect=connect, pool=pool),
                limits=httpx.Limits(max_connections=conns, max_keepalive_connections=keep))
        return client


@dataclass(frozen=True)
class Caller:
    principal: str   # a person's sub | "~svc-<name>" | "" (an unresolved Open WebUI user)
    kind: str        # "user" | "technical"
    consumer: str    # "agent" | "openwebui" | "dify"
    display: str     # for the audit log and the UI: slug, email or svc name
    sub: str = ""    # a person's sub (== principal); "" for technical or unresolved
    owui_id: str = ""  # the Open WebUI user id that resolved it (audit only, §7 D12)
    token_id: object = None  # the gateway token row of a person's rzmcp_ token (owner check)
    #: An Open WebUI caller whose email is still to be looked up in the
    #: directory (resolve_owui_session(defer_lookup=True)); resolve_owui_email
    #: does it, after the gateway holds a relay slot.
    email_pending: bool = False
    #: system_oauth (owui_oidc): the `sub` of a VERIFIED Authentik access
    #: token, still to be confirmed an active internal person by the
    #: directory (resolve_owui_email, with the relay slot held). Kept after
    #: that, resolved or not, so the audit says which path the caller came by.
    oidc_sub: str = ""
    #: The caller's e-mail address for the audit (owui_email) — Open WebUI's
    #: on the session path; the token's `email` claim or the directory's on
    #: system_oauth. Only ever an address (with "@"), never a username; ""
    #: when nobody said one. Audit only: the identity is the sub.
    email: str = ""


def _an_email(value) -> str:
    """`value` when it is an e-mail address, else "" (never a username)."""
    v = str(value or "").strip()
    return v[:254] if "@" in v and not v.startswith("@") and not v.endswith("@") else ""


class DirectoryError(RuntimeError):
    """The Authentik listing failed, was refused, malformed or incomplete.
    A caller that gets it decides NOTHING (no re-key, no quarantine)."""


def signup_open(environ=None) -> bool:
    """True when Open WebUI's native signup is on (§2.3 residual): anyone could
    then register the email of a person without an Open WebUI account, so the
    Open WebUI path gets no personal credential at all. Parsed exactly as
    Open WebUI parses it (`.lower() == "true"`); the compose default is false."""
    env = os.environ if environ is None else environ
    return str(env.get("ENABLE_SIGNUP") or "").strip().lower() == "true"


#: AuthentikDirectory.current_tier: the person is not an active internal user
#: in a listing young enough to say so (a refusal, 403; not "unavailable").
NOT_CURRENT = "not_current"


def _parse_ts(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _kb_scopes(user: dict) -> tuple:
    """The user's D6 knowledge scopes from Authentik's `groups_obj`: groups
    whose attributes say `rzfz_kb_scope: true` (the boolean; the group's name
    does not matter, like every other consumer of a scope), each with the
    cognee datasets it targets. Anything malformed counts as no scope."""
    out = []
    groups = user.get("groups_obj")
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        name, attrs = g.get("name"), g.get("attributes")
        if not (isinstance(name, str) and name and isinstance(attrs, dict)
                and attrs.get("rzfz_kb_scope") is True):
            continue
        targets = attrs.get("rzfz_kb_targets")
        ds = targets.get("cognee_datasets") if isinstance(targets, dict) else None
        datasets = frozenset(d for d in (ds if isinstance(ds, list) else []) if isinstance(d, str) and d)
        out.append((name, datasets))
        if len(out) >= _MAX_KB_SCOPES_PER_USER:
            break
    return tuple(out)


def _compact_user(user: dict) -> dict:
    """The listing keeps each user's knowledge scopes (KB_FIELD) and drops
    Authentik's full group objects (bounded memory in the cached listing)."""
    from app.services.tier import TIER_GROUPS
    u = {k: v for k, v in user.items() if k != "groups_obj"}
    u[KB_FIELD] = _kb_scopes(user)
    groups = user.get("groups_obj")
    u[TIER_FIELD] = tuple(sorted({g.get("name") for g in (groups if isinstance(groups, list) else [])
                                  if isinstance(g, dict) and g.get("name") in TIER_GROUPS}))
    return u


class AuthentikDirectory:
    """Read-only view of Authentik's users. GET only: the class calls no other
    verb, and it is built from the read-only token alone (`from_env`)."""

    def __init__(self, token: str, base: str = AUTHENTIK_BASE_DEFAULT, http=None):
        if not token:
            raise ValueError("AuthentikDirectory needs the read-only token")
        self._token, self._base, self._http = token, base.rstrip("/"), http
        self._cached = None      # (monotonic time, users) — owner_uid_state only
        self._cache_lock = threading.Lock()
        self._flight = None      # the _Flight of the listing in progress, if any
        self._unavailable_until = 0.0

    @classmethod
    def from_env(cls, environ=None):
        """The directory, or None when the read-only key is empty. Reads
        READONLY_TOKEN_ENV and nothing else — never AUTHENTIK_BOOTSTRAP_TOKEN."""
        env = os.environ if environ is None else environ
        tok = (env.get(READONLY_TOKEN_ENV) or "").strip()
        if not tok:
            return None
        base = (env.get("AUTHENTIK_URL") or "").strip() or AUTHENTIK_BASE_DEFAULT
        return cls(tok, base=base)

    # -- the one request -------------------------------------------------

    def list_users(self, on_page=None) -> list:
        """Every user, all pages. Raises DirectoryError on any failure and on
        an incomplete listing (fewer users read than the reported count), so
        no caller ever decides on a partial picture (A1 ruling 12's rule).
        `on_page()` is called after each page read (the single-flight leader
        reports its progress with it)."""
        users: list = []
        count = None
        page = 1
        http = self._http if self._http is not None else shared_client("authentik")
        for _ in range(_MAX_PAGES):
            try:
                r = http.get(f"{self._base}{_USERS_PATH}",
                                   params={"page": page, "page_size": _PAGE_SIZE,
                                           "include_groups": "true"},
                                   headers={"Authorization": f"Bearer {self._token}"},
                                   timeout=self.PAGE_TIMEOUT, follow_redirects=False)
            except Exception as e:  # noqa: BLE001 — any transport failure
                raise DirectoryError(f"authentik unreachable: {type(e).__name__}") from None
            if r.status_code != 200:
                raise DirectoryError(f"authentik answered {r.status_code}")
            try:
                body = r.json() or {}
            except Exception:  # noqa: BLE001
                raise DirectoryError("authentik answered no JSON") from None
            results = body.get("results")
            pag = body.get("pagination") or {}
            if not isinstance(results, list) or "count" not in pag:
                raise DirectoryError("authentik listing has no results/pagination")
            count = int(pag["count"])
            users.extend(_compact_user(u) for u in results if isinstance(u, dict))
            if on_page is not None:
                on_page()
            nxt = pag.get("next") or 0
            if not nxt:
                break
            page = int(nxt)
        else:
            raise DirectoryError("authentik listing did not end")
        if count is None or len(users) != count:
            raise DirectoryError(f"authentik listing incomplete ({len(users)} of {count})")
        return users

    # -- the questions ---------------------------------------------------

    @staticmethod
    def _person(u) -> bool:
        return bool(u.get("is_active")) and u.get("type") == "internal" and bool(u.get("uid"))

    @classmethod
    def _match_email(cls, users, want: str):
        hits = [u for u in users if cls._person(u)
                and str(u.get("email") or "").strip().lower() == want]
        return str(hits[0]["uid"]) if len(hits) == 1 else None

    def sub_for_email(self, email: str):
        """D8: the uid of the ONE active internal user whose stored email
        equals `email` (case-insensitive, exact otherwise), or None: no match,
        more than one, or any directory failure all REFUSE.

        B8 fix round 4: the SAME cached, single-flight listing as the owner
        check (never one listing per call). A listing younger than
        OWNER_CHECK_MAX_AGE answers a match; a miss is re-listed only when
        the cached listing is older than EMAIL_MISS_MAX_AGE (a person who
        just joined resolves within seconds, while a stream of calls from an
        email Authentik does not know costs at most one listing per
        EMAIL_MISS_MAX_AGE). While the directory is unavailable (a failed
        listing, then UNAVAILABLE_FOR seconds) the answer is None: the Open
        WebUI caller stays unresolved and gets no personal credential."""
        email = (email or "").strip()
        if not email or "@" not in email:
            return None
        want = email.lower()
        with self._cache_lock:
            cached = self._cached
        if cached is not None:
            age = time.monotonic() - cached[0]
            if age < self.OWNER_CHECK_MAX_AGE:
                sub = self._match_email(cached[1], want)
                if sub is not None or age < self.EMAIL_MISS_MAX_AGE:
                    return sub
        users = self._refresh(accept=lambda us: self._match_email(us, want) is not None)
        if users is None:
            logger.warning("email lookup refused: the directory is unavailable")
            return None
        return self._match_email(users, want)

    @classmethod
    def _in_scope_for(cls, users, uid: str, dataset: str) -> bool:
        hits = [u for u in users if cls._person(u) and str(u.get("uid")) == uid]
        if len(hits) != 1:
            return False
        return any(dataset in datasets for _name, datasets in hits[0].get(KB_FIELD) or ())

    def kb_dataset_state(self, uid: str, dataset: str) -> str:
        """A1 D6 at call time: may the active person `uid` read the cognee
        `dataset`? True only while they are a CURRENT member of a `KB: …`
        scope group (rzfz_kb_scope: true, whatever its name) that targets it.
        "member"      — they are, per a listing younger than
                        OWNER_CHECK_MAX_AGE (or, while a refresh that has
                        read a page runs, younger than stale_ok_for);
        "not_member"  — they are not: from a listing younger than
                        EMAIL_MISS_MAX_AGE, else from a fresh one (someone
                        just added is not refused from an old listing);
        "unavailable" — the directory could not be read: nothing is decided
                        (the gateway's 503, fail closed).
        The same cached single-flight listing as the owner check and the
        email lookup (B8), so a person removed from the scope is refused on
        the first call after the cached listing is refreshed."""
        uid, dataset = (uid or "").strip(), (dataset or "").strip()
        if not uid or not dataset:
            return "not_member"
        with self._cache_lock:
            cached = self._cached
        if cached is not None:
            age = time.monotonic() - cached[0]
            if age < self.OWNER_CHECK_MAX_AGE:
                if self._in_scope_for(cached[1], uid, dataset):
                    return "member"
                if age < self.EMAIL_MISS_MAX_AGE:
                    return "not_member"
        users = self._refresh(accept=lambda us: self._in_scope_for(us, uid, dataset))
        if users is None:
            return "unavailable"
        return "member" if self._in_scope_for(users, uid, dataset) else "not_member"

    def current_tier(self, uid: str) -> str:
        """B9 fix round 2: the active person's CURRENT tier (tier.resolve_tier
        over their tier groups). The same cached single-flight listing: a
        demotion counts after at most OWNER_CHECK_MAX_AGE (stale_ok_for while
        a healthy refresh runs).
        NOT_CURRENT   — the person is not in the listing (not an active
                        internal user): from a listing younger than
                        EMAIL_MISS_MAX_AGE, else from a fresh one, the same
                        age gate as kb_dataset_state (B9 fix round 4), so a
                        missing person does not force a re-list per request;
        "unavailable" — the directory could not be read (a 503)."""
        from app.services.tier import resolve_tier
        uid = (uid or "").strip()
        if not uid:
            return "unavailable"

        def person(users):
            hits = [u for u in users if self._person(u) and str(u.get("uid")) == uid]
            return hits[0] if len(hits) == 1 else None

        with self._cache_lock:
            cached = self._cached
        u = None
        if cached is not None:
            age = time.monotonic() - cached[0]
            if age < self.OWNER_CHECK_MAX_AGE:
                u = person(cached[1])
                if u is None and age < self.EMAIL_MISS_MAX_AGE:
                    return NOT_CURRENT
        if u is None:
            users = self._refresh(accept=lambda us: person(us) is not None)
            if users is None:
                return "unavailable"
            u = person(users)
            if u is None:
                return NOT_CURRENT
        return resolve_tier(list(u.get(TIER_FIELD) or ()))

    def active_user(self, uid: str, users=None):
        """The active internal user whose uid is exactly `uid`, or None.
        Raises DirectoryError (callers that must not decide on a failure use
        this; is_active_uid folds the failure into False)."""
        if not uid:
            return None
        users = self.usable_listing() if users is None else users
        hits = [u for u in users if self._person(u) and str(u.get("uid")) == uid]
        return hits[0] if len(hits) == 1 else None

    def email_of(self, uid: str) -> str:
        """The e-mail address the latest listing holds for `uid` ("" when
        none). Reads the listing the owner check just used, never a new one;
        for the audit only."""
        with self._cache_lock:
            cached = self._cached
        if cached is None or not uid:
            return ""
        hits = [u for u in cached[1] if str(u.get("uid")) == uid]
        return _an_email(hits[0].get("email")) if len(hits) == 1 else ""

    def listed_uid(self, uid: str):
        """#2677: is `uid` in the latest listing at all (any state)? True: the
        account exists (deactivated, when it is not an active person); False:
        it was deleted; None: no listing to tell from. Reads the listing the
        owner check just used (owner_uid_state), never a new one."""
        with self._cache_lock:
            cached = self._cached
        if cached is None or not uid:
            return None
        return any(str(u.get("uid")) == uid for u in cached[1])

    def is_active_uid(self, uid: str, username=None) -> bool:
        """Ruling I5: an owner id counts as an Authentik uid only if it differs
        from the owner's username (parse_authentik_headers falls back to the
        username when X-Authentik-Uid is missing) AND an active internal user
        has exactly that uid. Any directory failure is False."""
        uid = (uid or "").strip()
        if not uid:
            return False
        if username is not None and uid == str(username).strip():
            return False
        try:
            return self.active_user(uid) is not None
        except DirectoryError as e:
            logger.warning("is_active_uid refused: %s", e)
            return False


    #: Seconds per listing page. The Agent Manager's wiring client waits
    #: longer than one page (mcp_manager_client.WIRING_TIMEOUT).
    PAGE_TIMEOUT = 10

    #: How long owner_uid_state may reuse a listing (#2086 fix round 1): the
    #: agent-wiring route runs on every launch, recreate and post-install
    #: rewiring pass, and each check is a full paginated listing.
    OWNER_CHECK_MAX_AGE = 60.0
    #: How long an owner check answers "unavailable" after a failed listing,
    #: without asking again: an Authentik outage gives fast 503s, not threads
    #: blocked on one timed-out listing after another (B8 fix round 3).
    UNAVAILABLE_FOR = 5.0
    #: How old a cached listing may be to say an email is NOT known (a hit is
    #: taken from any listing younger than OWNER_CHECK_MAX_AGE).
    EMAIL_MISS_MAX_AGE = 10.0
    @property
    def refresh_wait(self) -> float:
        """How long a caller waits for another thread's listing to read its
        next page (one page timeout plus a margin); past it, the answer is
        "unavailable"."""
        return float(self.PAGE_TIMEOUT) + 2.0

    @property
    def stale_ok_for(self) -> float:
        """How old the previous good listing may be for a waiter to answer
        from it while a HEALTHY refresh runs (B8 fix round 5): the 60 s cache
        plus one refresh_wait, so a deactivation is never honoured for more
        than about OWNER_CHECK_MAX_AGE + refresh_wait."""
        return float(self.OWNER_CHECK_MAX_AGE) + self.refresh_wait

    def usable_listing(self, on_page=None):
        """The full listing, or DirectoryError. A listing with no active
        internal person at all is NOT evidence that everyone left: a wrong
        token scope, an empty tenant during a restore, a filter gone wrong —
        so it is refused like a failed one, and nothing is decided from it
        (no "not active", no revoke, no quarantine)."""
        users = self.list_users(on_page=on_page)
        if not any(self._person(u) for u in users):
            raise DirectoryError("authentik listing holds no active internal user")
        return users

    def _refresh(self, accept=None):
        """One listing for everyone who needs it (single-flight): the first
        caller lists, concurrent callers wait for its result. A failure is
        remembered for UNAVAILABLE_FOR seconds. Returns the users, or None
        for "unavailable".

        A waiter never answers "unavailable" while a healthy leader is still
        listing (B8 fix round 4), and never answers from an old listing while
        the leader hangs (fix round 5):
          (a) once the leader has read at least one page IN THIS FLIGHT (so
              Authentik is answering), a waiter may answer from the previous
              good listing, if it is younger than stale_ok_for and
              `accept(users)` says the answer it needs is in it — only an
              answer that decides nothing against anyone ("active", "this
              email is that person"); "not active" and "unknown email" wait
              for the fresh listing;
          (b) otherwise it waits for as long as the leader makes progress:
              it gives up ("unavailable") when the leader has read no page for
              refresh_wait — a leader that hangs, not one that is slowly
              reading page 6 of 6.
        A leader that hangs therefore never yields a positive answer from an
        old listing: its waiters get "unavailable" (the 503 path)."""
        with self._cache_lock:
            now = time.monotonic()
            if now < self._unavailable_until:
                return None
            flight = self._flight
            leader = flight is None
            if leader:
                flight = self._flight = _Flight(now)
            cached = self._cached
        if not leader:
            stale = (cached[1] if accept is not None and cached is not None and accept(cached[1])
                     else None)
            with flight.cond:
                while True:
                    if flight.done:
                        return flight.users
                    if (stale is not None and flight.pages > 0
                            and time.monotonic() - cached[0] < self.stale_ok_for):
                        return stale
                    left = flight.progress + self.refresh_wait - time.monotonic()
                    if left <= 0:
                        return None
                    flight.cond.wait(left)

        users = None
        try:
            users = self.usable_listing(on_page=flight.page)
        except DirectoryError as e:
            logger.warning("owner uid check: directory unavailable: %s", e)
        except Exception as e:  # noqa: BLE001 — any failure is "unavailable", never a guess
            logger.warning("owner uid check: directory unavailable: %s", type(e).__name__)
        finally:
            with self._cache_lock:
                if users is None:
                    self._unavailable_until = time.monotonic() + self.UNAVAILABLE_FOR
                else:
                    self._cached = (time.monotonic(), users)
                self._flight = None
            flight.finish(users)
        return users

    def owner_uid_state(self, uid: str, username=None, max_age=None) -> str:
        """Ruling I5 for the agent wiring and the gateway's person tokens,
        with the failure kept apart:
        "active"      — an active internal user has exactly this uid and it
                        is not the owner's username;
        "not_active"  — it is not (the username fallback, a deleted user, …);
        "unavailable" — the directory could not be read, or the listing is
                        not usable (no active person in it at all): nothing is
                        decided.

        When "active" may come from an older listing:
          * a listing younger than `max_age` (default OWNER_CHECK_MAX_AGE,
            60 s) says "active" without asking Authentik;
          * while another thread's refresh is running AND has read at least
            one page (Authentik answers), a listing younger than stale_ok_for
            (60 s + refresh_wait, 72 s at the defaults) may say "active".
        "Not active" is only ever said from a FRESH listing (a user made after
        the cached one was taken is not refused), and it revokes that
        person's tokens at the gateway. So, with Authentik reachable, a
        deactivated person's calls are refused within about 60 s (at most
        stale_ok_for). With Authentik unreachable, nothing older than
        max_age is used: the answer is "unavailable" (the gateway's 503,
        fail closed), remembered for UNAVAILABLE_FOR seconds. The fresh
        listing is single-flight."""
        uid = (uid or "").strip()
        if not uid or (username is not None and uid == str(username).strip()):
            return "not_active"
        age = self.OWNER_CHECK_MAX_AGE if max_age is None else max_age
        with self._cache_lock:
            cached = self._cached
        if cached is not None and time.monotonic() - cached[0] < age:
            if self.active_user(uid, users=cached[1]) is not None:
                return "active"
            # N4: "not active" from a cached listing may only mean the user
            # was created after it was taken — re-list once before answering.
        users = self._refresh(accept=lambda us: self.active_user(uid, users=us) is not None)
        if users is None:
            return "unavailable"
        return "active" if self.active_user(uid, users=users) is not None else "not_active"


# ---------------------------------------------------------------------------
# Principals and tokens
# ---------------------------------------------------------------------------

def technical_principal(name: str) -> str:
    if not isinstance(name, str) or not _TECH_NAME.match(name) or name in RESERVED_TECH_NAMES:
        raise ValueError(f"technical user name must match ^svc-[a-z0-9-]{{3,40}}$ and not be "
                         f"reserved (A1 D7): {name!r}")
    return TECH_PREFIX + name


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token(db, principal: str, kind: str, consumer: str) -> str:
    """Mint a caller token and store its SHA-256. A person's principal is their
    sub; a technical principal is `~svc-<name>` and gets `agent`/`dify` only."""
    if kind not in ("user", "technical") or consumer not in CONSUMERS or not principal:
        raise ValueError(f"cannot mint a {kind!r} token for {consumer!r}")
    if kind == "technical":
        technical_principal(principal[len(TECH_PREFIX):] if principal.startswith(TECH_PREFIX) else "")
        if consumer not in TECH_CONSUMERS:
            raise ValueError(f"a technical user's token is for {TECH_CONSUMERS}, not {consumer!r}")
    elif principal.startswith(TECH_PREFIX):
        raise ValueError("a person's token needs the person's sub, not a technical principal")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    db.insert_gateway_token(_hash(token), principal, kind, consumer)
    return token


class OwnerInactive(Exception):
    """A person's token whose owner is no longer an active Authentik user
    (the token is revoked on the spot). `caller` is who it was, for the audit."""

    def __init__(self, caller):
        super().__init__("the token's owner is not an active user")
        self.caller = caller


class OwnerCheckUnavailable(Exception):
    """A person's token whose owner could not be checked (no directory, or it
    failed): refused, never let through on a guess."""

    def __init__(self, caller):
        super().__init__("the token's owner could not be checked")
        self.caller = caller


#: Pass as `directory` to resolve_bearer to resolve the token WITHOUT the
#: owner check; the caller must then call check_owner itself (the gateway does,
#: after it holds a relay slot, so the caps bound a hanging directory).
DEFER_OWNER_CHECK = object()


def purge_person(db, sub: str, reason: str, cred_store=None) -> dict:
    """A person who is no longer an active internal Authentik user keeps
    nothing here: every gateway token of theirs is revoked (#2086) and every
    credential they stored is deleted (#2677), through the credential store so
    each of its backends is purged. `cred_store=None` uses a store over `db`
    alone. A failure of the credential half is logged, never raised: the
    caller's refusal stands, and the start-up / hourly sweep retries."""
    out = {"tokens": 0, "rows": 0, "openbao": 0}
    sub = (sub or "").strip()
    if not sub or sub.startswith(TECH_PREFIX):
        return out
    out["tokens"] = db.revoke_gateway_tokens(sub)
    if cred_store is None or not callable(getattr(cred_store, "purge_person", None)):
        from app.services.credential_store import CredentialStore
        cred_store = CredentialStore(db, None)
    try:
        res = cred_store.purge_person(sub, reason)
    except Exception:  # noqa: BLE001
        logger.exception("could not delete the stored credentials of a departed person")
        try:
            from app.services.database import AUDIT_PERSON_CREDENTIALS_PURGE_FAILED
            db.log_audit(sub, AUDIT_PERSON_CREDENTIALS_PURGE_FAILED, None,
                         {"reason": reason, "error": "purge_failed"})
        except Exception:  # noqa: BLE001 — the log line above still carries it
            pass
        return {**out, "error": "purge_failed"}
    if isinstance(res, dict):
        for k in ("rows", "openbao"):
            n = res.get(k)
            out[k] = n if isinstance(n, int) else 0
        if res.get("error"):
            out["error"] = res["error"]
    return out


def retire_person(db, sub: str, listed, cred_store=None) -> dict:
    """A person who is not an active internal Authentik user (#2677): their
    gateway tokens are revoked at once (#2086). Their
    stored credentials go
      * at once when the account was DELETED (`listed` is False: the uid is
        in no listing record);
      * when it is DEACTIVATED (`listed` True), only after
        DEACTIVATED_PURGE_DAYS continuously deactivated — the clock starts
        here and is reset when the person is seen active again;
      * never on a guess (`listed` None): only the tokens are revoked.
    Returns purge_person's summary (+ "waiting": True while the clock runs)."""
    sub = (sub or "").strip()
    out = {"tokens": 0, "rows": 0, "openbao": 0}
    if not sub or sub.startswith(TECH_PREFIX):
        return out
    if listed is False:
        res = purge_person(db, sub, "owner_deleted", cred_store=cred_store)
    elif listed is True and db.mark_person_deactivated(sub):
        res = purge_person(db, sub, "owner_deactivated_30_days", cred_store=cred_store)
    else:
        out["tokens"] = db.revoke_gateway_tokens(sub)
        return {**out, "waiting": listed is True}
    if not res.get("error"):
        db.clear_person_deactivated([sub])
    return res


def check_owner(db, caller, directory, token_id=None, cred_store=None):
    """The owner check of a person's token (see resolve_bearer). Returns the
    caller, or raises OwnerInactive (all of that person's tokens revoked; their
    stored credentials deleted at once if the account was deleted, after 30
    days of deactivation otherwise — retire_person, #2677) or
    OwnerCheckUnavailable.
    Only a person's `rzmcp_` TOKEN is checked
    (`token_id` set): a technical caller, and an Open WebUI caller (whose sub
    the directory itself just resolved, or who is unresolved), pass unchanged."""
    if caller.kind != "user" or token_id is None:
        return caller
    try:
        state = directory.owner_uid_state(caller.principal) if directory else "unavailable"
    except Exception:  # noqa: BLE001
        logger.exception("gateway: owner check failed")
        state = "unavailable"
    if state == "not_active":
        lookup = getattr(directory, "listed_uid", None)
        try:
            listed = lookup(caller.principal) if callable(lookup) else None
        except Exception:  # noqa: BLE001 — undecided: tokens only
            listed = None
        res = retire_person(db, caller.principal, listed, cred_store=cred_store)
        logger.warning("gateway: a token of an owner who is no longer active was presented; "
                       "revoked %d token(s), deleted %d stored credential(s)",
                       res["tokens"], res["rows"])
        raise OwnerInactive(caller)
    if state != "active":
        raise OwnerCheckUnavailable(caller)
    if token_id is not None:
        db.touch_gateway_token(token_id)
    return caller


def resolve_bearer(db, presented: str, *, directory, cred_store=None):
    """`rzmcp_…` -> Caller, or None (unknown, revoked, not ours, or a technical
    user that is no longer live in the mirror).

    A PERSON's token is honoured only while its owner is an active internal
    Authentik user (#2086 fix round 2; B6 revoked tokens only for technical
    users): `directory.owner_uid_state(sub)` — a 60 s cached listing, re-read
    before it says "not active". Not active: every token of that person is
    revoked and OwnerInactive is raised (401). No directory, or it failed:
    OwnerCheckUnavailable (503), fail closed. Technical tokens are checked
    against the mirror as before. `directory` is required on purpose: a caller
    cannot forget the check."""
    if not presented or not presented.startswith(TOKEN_PREFIX) or len(presented) > 128:
        return None
    row = db.get_gateway_token(_hash(presented))
    if not row:
        return None
    principal = row["principal"]
    if row["kind"] == "technical":
        if not principal.startswith(TECH_PREFIX) or row["consumer"] not in TECH_CONSUMERS:
            return None
        tu = db.get_technical_user(principal[len(TECH_PREFIX):])
        if not tu or tu.get("revoked_at"):
            return None
        db.touch_gateway_token(row["id"])
        return Caller(principal, "technical", row["consumer"], principal[len(TECH_PREFIX):], "")
    if principal.startswith(TECH_PREFIX):
        return None
    display = db.get_user_slug_by_sub(principal) or principal
    caller = Caller(principal, "user", row["consumer"], display, principal, token_id=row["id"])
    if directory is DEFER_OWNER_CHECK:
        return caller
    return check_owner(db, caller, directory, token_id=row["id"], cred_store=cred_store)


def rejected_token(db, presented: str):
    """#2472: why a token this gateway issued was rejected, or None when it
    is not one of ours (unknown tokens name no principal and are never
    audited per request). A dict with the token's row id, principal, kind,
    consumer, a display name and the reason — never the token or its hash:
      token_revoked           the token itself was revoked
      technical_user_revoked  its technical user was revoked (or deleted)
      technical_user_unknown  its technical user is not in the mirror
      token_not_accepted      any other refusal of a known token"""
    if not presented or not presented.startswith(TOKEN_PREFIX) or len(presented) > 128:
        return None
    ask = getattr(db, "get_rejected_gateway_token", None)
    if not callable(ask):
        return None
    row = ask(_hash(presented))
    if not isinstance(row, dict) or not isinstance(row.get("principal"), str) or not row["principal"]:
        return None
    principal, kind = row["principal"], row.get("kind")
    technical = principal.startswith(TECH_PREFIX)
    if row.get("revoked_at") is not None:
        reason = "token_revoked"
    elif kind == "technical" and technical:
        tu = db.get_technical_user(principal[len(TECH_PREFIX):])
        if not isinstance(tu, dict):
            reason = "technical_user_unknown"
        elif tu.get("revoked_at"):
            reason = "technical_user_revoked"
        else:
            reason = "token_not_accepted"
    else:
        reason = "token_not_accepted"
    if technical:
        display = principal[len(TECH_PREFIX):]
    else:
        slug = db.get_user_slug_by_sub(principal)
        display = slug if isinstance(slug, str) and slug else principal
    return {"token_id": row.get("id"), "principal": principal, "kind": kind,
            "consumer": row.get("consumer"), "display": display, "reason": reason}


def resolve_owui_session(db, token: str, http=None, base: str = "http://openwebui:8080",
                         directory=None, environ=None, defer_lookup=False):
    """An Open WebUI bearer -> Caller, or None when Open WebUI does not accept
    it. `directory=None` builds AuthentikDirectory.from_env(); `False` means
    none. The caller stays UNRESOLVED (principal "", no credential) when the
    signup is open, when there is no directory, or when Authentik does not map
    the email to exactly one active internal person — never a guess, and never
    a fallback to mcp-manager's own mcp_users table.

    `defer_lookup=True` (the gateway, B8 fix round 4): the directory is NOT
    asked here; the caller comes back unresolved with `email_pending` set,
    and resolve_owui_email asks it once the caller holds a relay slot, so a
    hanging Authentik ties up at most the caps' threads. Open WebUI itself is
    asked through the process's shared client (no per-call TLS context), and
    at most OWUI_CONCURRENT_CHECKS at a time: beyond that, OpenWebUIBusy."""
    if not token or token.startswith(TOKEN_PREFIX):
        return None
    if http is None:
        http = shared_client("openwebui")
    if not _OWUI_CHECKS.acquire(blocking=False):
        raise OpenWebUIBusy()
    try:
        r = http.get(f"{base}/api/v1/auths/", headers={"Authorization": f"Bearer {token}"},
                     timeout=_OPENWEBUI_TIMEOUT, follow_redirects=False)
    except Exception:  # noqa: BLE001
        return None
    finally:
        _OWUI_CHECKS.release()
    if r.status_code != 200:
        return None
    try:
        who = r.json() or {}
        email = str(who.get("email") or "").strip()
        owui_id = str(who.get("id") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not email:
        return None
    unresolved = Caller("", "user", "openwebui", email, "", owui_id, email=_an_email(email))
    if signup_open(environ):
        return unresolved
    # system_oauth: Open WebUI signs in through Authentik and its gateway
    # connections forward the user's Authentik token. A session bearer then
    # gets no personal credential: the email it carries comes from Open
    # WebUI's own user table, which an Open WebUI admin can rewrite (§2.3
    # residual). Stateless servers still work.
    from app.services.owui_oidc import system_oauth_on
    if system_oauth_on(environ):
        return unresolved
    if directory is None:
        directory = AuthentikDirectory.from_env(environ)
    if not directory:
        return unresolved
    if defer_lookup:
        return replace(unresolved, email_pending=True)
    return resolve_owui_email(replace(unresolved, email_pending=True), directory)


def resolve_owui_email(caller, directory):
    """Finish an Open WebUI caller whose lookup was deferred: the person with
    exactly that email, or the caller unresolved (no match, more than one,
    the directory unavailable, or any failure). A caller without
    `email_pending` comes back unchanged.

    A system_oauth caller (`oidc_sub` set, `email_pending` set) is the
    person with that uid only while the directory says the uid is an
    active internal user; deactivated, unknown or undecidable is the caller
    unresolved, exactly like an email without a match."""
    if not getattr(caller, "email_pending", False):
        return caller
    if getattr(caller, "oidc_sub", ""):
        unresolved = replace(caller, email_pending=False)
        if not directory:
            return unresolved
        try:
            state = directory.owner_uid_state(caller.oidc_sub)
        except Exception:  # noqa: BLE001 — undecidable is nobody, never a guess
            logger.exception("gateway: the active check of an Authentik token's sub failed")
            return unresolved
        if state != "active":
            return unresolved
        email = caller.email
        if not email:
            # A token without an `email` claim: the address the directory
            # holds for this (active) uid, from the listing just used.
            try:
                email = _an_email(getattr(directory, "email_of", lambda _u: "")(caller.oidc_sub))
            except Exception:  # noqa: BLE001 — audit only; no address is "", never a guess
                email = ""
        return replace(unresolved, principal=caller.oidc_sub, sub=caller.oidc_sub, email=email)
    unresolved = replace(caller, email_pending=False)
    if not directory:
        return unresolved
    try:
        sub = directory.sub_for_email(caller.display) or ""
    except Exception:  # noqa: BLE001 — a failed lookup is nobody, never a guess
        logger.exception("gateway: Open WebUI email lookup failed")
        return unresolved
    if not sub:
        return unresolved
    return replace(unresolved, principal=sub, sub=sub)


def resolve_owui_oidc(token: str, verifier):
    """system_oauth: an Authentik access token Open WebUI forwarded -> a
    Caller whose `oidc_sub` waits for the directory's active check
    (resolve_owui_email, slot held), or None when the token does not verify
    (owui_oidc.AuthentikTokenVerifier: signature, issuer, audience, expiry).
    `display` is the token's preferred_username or email, for the audit and
    the slot key only; the identity is the sub alone."""
    if verifier is None:
        return None
    try:
        claims = verifier.verify(token)
    except Exception:  # noqa: BLE001 — a failed check is nobody
        logger.exception("gateway: verifying an Authentik token failed")
        return None
    if not claims:
        return None
    sub = str(claims["sub"]).strip()
    shown = next((str(claims[k]).strip()[:128] for k in ("preferred_username", "email")
                  if isinstance(claims.get(k), str) and claims[k].strip()), "")
    return Caller("", "user", "openwebui", shown or f"authentik:{sub[:12]}", "",
                  email_pending=True, oidc_sub=sub, email=_an_email(claims.get("email")))


def agent_token_for(db, cred_store, owner_sub: str) -> str:
    """The person's agent gateway token: re-read from the vault while it is
    live, minted (and vaulted) otherwise. One per person, keyed by sub, so
    every agent of that person keeps working when a second agent launches.
    The caller (B8) checks is_active_uid first."""
    if not owner_sub or owner_sub.startswith(TECH_PREFIX):
        raise ValueError("an agent token needs the owner's sub")
    existing = cred_store.get_decrypted(owner_sub, GW_SECRET_ID).get("agent_token")
    if existing and db.get_gateway_token(_hash(existing)):
        return existing
    token = mint_token(db, owner_sub, "user", "agent")
    cred_store.store_pat(owner_sub, GW_SECRET_ID, {"agent_token": token})
    return token


# ---------------------------------------------------------------------------
# The portal side
# ---------------------------------------------------------------------------

def request_sub() -> str:
    """The caller's sub on a Caddy-anchored portal request: X-Authentik-Uid,
    read DIRECTLY. Never razzfazz_common.auth.parse_authentik_headers()["uid"],
    which falls back to the username when the header is missing (§2.3)."""
    from flask import request
    return (request.headers.get("X-Authentik-Uid") or "").strip()


def legacy_slug_for(app, user_slug):
    """The ga.4-parity window (B6 fix round 2, ruling I1: identity safety
    beats availability). Undecided pre-2026.11 rows are served by their slug
    ONLY while NO read-only token is set (no directory). Once a token is set
    the slug serves nothing: a row reaches its owner only after an
    attribution confirmed by the directory re-keyed it to their sub. Any
    directory failure therefore leaves the rows unserved (fail closed)."""
    if getattr(app, "authentik_directory", None) is None:
        return user_slug or None
    return None


def record_sso_user() -> None:
    """before_request on the Caddy-anchored SSO blueprints (installed after
    the anchor, so only Caddy-delivered headers reach here): remember (slug,
    username, email, sub) and, when the visitor has unattributed pre-upgrade
    credentials under their slug, decide them now (design record §2.3). A
    request without X-Authentik-Uid records no sub and decides nothing.

    A failed upsert or attribution is NOT a success path: it sets
    `g.rzfz_identity_error`, logs a WARN (no values) and is recorded for
    `rzfz status`. The request continues; with a token set it is never served
    undecided rows anyway (legacy_slug_for), so the user simply does not see
    them this time."""
    from flask import current_app, g, request
    from razzfazz_common.user_slug import make_user_slug
    g.rzfz_identity_error = None
    username = (request.headers.get("X-Authentik-Username") or "").strip()
    if not username:
        return None
    email = (request.headers.get("X-Authentik-Email") or "").strip()
    sub = request_sub()
    slug = make_user_slug(username)
    try:
        current_app.db.upsert_user(slug, username, email, sub or None)
    except Exception as e:  # noqa: BLE001
        _identity_failed(current_app, slug, f"user record failed: {type(e).__name__}")
        return None
    if not sub:
        return None
    try:
        from app.services import identity_migration
        errors = identity_migration.on_portal_visit(current_app, slug=slug, sub=sub,
                                                    email=email, username=username)
    except Exception as e:  # noqa: BLE001
        errors = [f"attribution raised: {type(e).__name__}"]
    if errors:
        _identity_failed(current_app, slug, "; ".join(errors))
    return None


def _identity_failed(app, slug, reason):
    from flask import g
    g.rzfz_identity_error = reason
    app.logger.warning("MCP identity attribution failed for %s: %s — undecided "
                       "pre-2026.11 rows are withheld for this request", slug, reason)
    try:
        app.db.record_attribution(False, reason)
    except Exception:  # noqa: BLE001 — the WARN above already carries it
        pass


def install_record_sso_user(*blueprints):
    """Attach record_sso_user as a before_request hook, idempotently (the
    blueprints are module singletons; a second create_app must not add it
    twice). Call it AFTER install_caddy_anchor: hooks run in order."""
    for bp in blueprints:
        if not getattr(bp, "_rzfz_caddy_anchor", False):
            raise RuntimeError(f"install the Caddy anchor on {bp.name} first")
        if getattr(bp, "_rzfz_record_sso_user", False):
            continue
        bp.before_request(record_sso_user)
        bp._rzfz_record_sso_user = True
