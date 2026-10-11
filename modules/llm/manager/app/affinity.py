# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2521 — a chat's turns stick to one replica.

The router balances every request over all ready instances of a model, so the
consecutive turns of one chat landed on different boxes and never found the
previous turn's prefix in an engine's cache (#2520). Open WebUI names the chat
in ``X-OpenWebUI-Chat-Id`` (with ``ENABLE_FORWARD_USER_INFO_HEADERS=true``,
modules/chat/compose.yml); this module turns that into a pin.

How a request is routed:

- **No chat id** (Dify, cognee, agents, any API client), or affinity switched
  OFF in the console's Settings (K4): nothing here runs and the request
  reaches the router exactly as before — no tag, normal routing.
- **A chat's first request**: normal routing (the router's latency-based pick).
  The router answers with ``x-litellm-model-id``, which is the INSTANCE id
  (router_config.build_model_list sets ``model_info.id``); that is the pin.
- **Later turns**: the request carries ``x-litellm-tags: rzfz-instance:<id>``,
  and the router's tag filter leaves exactly that deployment — the request
  still names the model, so nothing about the response changes.
- **The pinned instance is gone or draining**: it is no longer in the router
  config the manager wrote (the rebuild keeps only ``ready`` instances; a drain
  deletes the rows). The pin is dropped, the request goes through normal
  routing, and the chat is pinned again to wherever it lands.
- **The pinned instance fails** (still in the config, but an engine died or
  it answers 5xx/429): the SAME request is sent again tagged with every OTHER
  instance of the model — the router's own retries cannot do this, they would
  stay on the one tagged deployment, and untagged normal routing can pick the
  dead one again (measured against LiteLLM 1.83.7: five 500s in twenty
  untagged requests with one dead replica of two). If that also fails, one
  last untagged attempt. The chat is re-pinned to the instance that answered.

The pin table is process memory — the manager runs ONE uvicorn worker (see the
Dockerfile). It is bounded: an entry idles out after ``IDLE_TTL_SECONDS`` and
the table never holds more than ``MAX_CHATS``, least recently used first out.
A router blue-green swap (#2509) does not touch it, and instance ids survive
every config rewrite; a manager restart empties it, and each chat is simply
pinned again on its next turn.

Privacy: with the forward headers on, Open WebUI also sends the user's id,
name and e-mail. None of them is read, logged or forwarded here — the manager
builds the router request's headers from scratch — and the chat id itself is
only ever logged as its first 8 characters.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

import yaml

logger = logging.getLogger(__name__)

#: What Open WebUI sends (its FORWARD_SESSION_INFO_HEADER_CHAT_ID default).
CHAT_ID_HEADER = "x-openwebui-chat-id"
#: The router's request-tag header and the two response headers it sets.
TAGS_HEADER = "x-litellm-tags"
MODEL_ID_HEADER = "x-litellm-model-id"
API_BASE_HEADER = "x-litellm-model-api-base"

#: A chat idle this long loses its pin (the next turn routes normally).
IDLE_TTL_SECONDS = 3600.0
#: Hard cap on pinned chats; the least recently used goes first.
MAX_CHATS = 10_000

#: A chat id longer than this is not a chat id (OWUI's are UUIDs, or
#: ``local:<uuid>`` for a temporary chat) — ignore it rather than hash megabytes.
_MAX_CHAT_ID_LEN = 256

#: Upstream statuses that are the REQUEST's fault — another replica would
#: answer the same, so they are returned, not rerouted.
#:
#: 401 is deliberately NOT in this set. The manager authenticates to the router
#: with its own internal key, so a 401 from the router is never the client's
#: credential: LiteLLM 1.83.7 raises its tag-routing "no deployments with the
#: requested tags" error as a 401, and that is what a tagged attempt gets when
#: every deployment its tag matches is cooling down or has just left the
#: running router's model list (a swap in progress). Another tag set, or no
#: tag, can succeed — so it is rerouted like a 5xx.
_NOT_REROUTED = frozenset({400, 413, 422})


def chat_id_from(headers) -> Optional[str]:
    raw = headers.get(CHAT_ID_HEADER)
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw or len(raw) > _MAX_CHAT_ID_LEN:
        return None
    return raw


def chat_label(chat_id: str) -> str:
    """What a log line may say about a chat: its first 8 characters, no more."""
    return chat_id[:8]


def _digest(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode("utf-8")).hexdigest()


def reroutes(status: int) -> bool:
    return status >= 400 and status not in _NOT_REROUTED


class PinTable:
    """``(chat digest, model) -> instance id``, bounded by idle TTL and size."""

    def __init__(self, *, max_chats: int = MAX_CHATS,
                 idle_ttl_seconds: float = IDLE_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_chats = max(1, int(max_chats))
        self.idle_ttl = float(idle_ttl_seconds)
        self._clock = clock
        self._entries: "OrderedDict[tuple[str, str], tuple[str, float]]" = OrderedDict()

    def _prune(self, now: float) -> None:
        # Ordered by last use, so the expired ones are all at the front.
        while self._entries:
            _, (_, last) = next(iter(self._entries.items()))
            if now - last < self.idle_ttl:
                break
            self._entries.popitem(last=False)
        while len(self._entries) > self.max_chats:
            self._entries.popitem(last=False)

    def get(self, key) -> Optional[str]:
        now = self._clock()
        self._prune(now)
        hit = self._entries.get(key)
        if hit is None:
            return None
        self._entries[key] = (hit[0], now)
        self._entries.move_to_end(key)
        return hit[0]

    def put(self, key, instance_id: str) -> None:
        now = self._clock()
        self._entries[key] = (instance_id, now)
        self._entries.move_to_end(key)
        self._prune(now)

    def drop(self, key) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        self._prune(self._clock())
        return len(self._entries)


class ServedInstances:
    """The instances the router config serves, per model — re-read on change.

    The manager wrote that file itself from its instance state
    (router_config.generate_from_db: ``ready`` instances only, dead relay
    workers left out, drained workers' rows deleted), so "is the pinned
    instance still serving?" is answered by the same source the router's
    model list is built from. ``os.replace`` gives every rewrite a new inode,
    so a stat is enough to notice one.
    """

    def __init__(self, path_fn: Optional[Callable[[], str]] = None) -> None:
        self._path_fn = path_fn or _router_config_path
        self._stamp = None
        self._served: dict[str, list[str]] = {}

    def get(self) -> dict[str, list[str]]:
        from app.router_config import served_instances

        path = self._path_fn()
        try:
            st = os.stat(path)
        except OSError:
            self._stamp, self._served = None, {}
            return {}
        stamp = (path, st.st_ino, st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            try:
                with open(path, encoding="utf-8") as fh:
                    served = served_instances(yaml.safe_load(fh) or {})
            except Exception:  # unreadable or mid-edit by hand: pin nothing
                served = {}
            self._stamp, self._served = stamp, served
        return self._served


def _router_config_path() -> str:
    from app.config import get_settings

    return get_settings().router_config_path


@dataclass
class Route:
    """How ONE upstream attempt of a chat request is routed."""

    chat_id: str
    model: str
    key: tuple
    tags: Optional[list] = None          # None = untagged, i.e. normal routing
    pinned: Optional[str] = None         # the instance this attempt is pinned to
    avoided: Optional[str] = None        # the failed instance a reroute avoids
    note: str = "first pick"
    failed: list = field(default_factory=list)

    def headers(self, base: dict) -> dict:
        if not self.tags:
            return base
        return {**base, TAGS_HEADER: ",".join(self.tags)}


class ChatAffinity:
    def __init__(self, *, pins: Optional[PinTable] = None,
                 served: Optional[ServedInstances] = None) -> None:
        self.pins = pins if pins is not None else PinTable()
        self.served = served if served is not None else ServedInstances()

    def plan(self, chat_id: str, model: str) -> Route:
        from app.router_config import instance_tag

        key = (_digest(chat_id), model)
        pinned = self.pins.get(key)
        if pinned is None:
            return Route(chat_id, model, key)
        if pinned in self.served.get().get(model, {}):
            return Route(chat_id, model, key, tags=[instance_tag(pinned)],
                         pinned=pinned, note="kept")
        # Gone from the router config: not ready any more, drained, removed.
        self.pins.drop(key)
        return Route(chat_id, model, key, avoided=pinned,
                     note=f"instance {pinned} is no longer served, re-pinned")

    def next_route(self, route: Route, status: int) -> Optional[Route]:
        """The attempt after ``route`` failed with ``status`` — or None.

        Only a TAGGED attempt is retried: an untagged one already had the
        router's own retries over every replica, which is normal routing.
        """
        from app.router_config import instance_tag

        if not reroutes(status) or not route.tags:
            return None
        failed = route.failed + [status]
        if route.pinned is not None:
            self.pins.drop(route.key)
            others = [i for i in self.served.get().get(route.model, {})
                      if i != route.pinned]
            logger.warning(
                "chat affinity (#2521): chat=%s… model=%s — pinned instance %s "
                "answered %s; rerouting this request %s",
                chat_label(route.chat_id), route.model, route.pinned, status,
                f"to the other {len(others)} instance(s)" if others
                else "through normal routing (no other instance)")
            return Route(route.chat_id, route.model, route.key,
                         tags=[instance_tag(i) for i in others] or None,
                         avoided=route.pinned, failed=failed,
                         note=f"rerouted around {route.pinned} ({status})")
        # An exclusion attempt failed too: last try, untagged.
        return Route(route.chat_id, route.model, route.key, avoided=route.avoided,
                     failed=failed, note=route.note + f", then normal routing ({status})")

    def settle(self, route: Route, status: int, response_headers) -> None:
        """Record where the chat landed; one log line per chat request."""
        if status >= 400:
            return
        iid = response_headers.get(MODEL_ID_HEADER)
        if not iid:
            return
        iid = str(iid)
        from app.router_config import pin_scope

        served = self.served.get().get(route.model, {})
        note = route.note
        if iid not in served:
            # The router named a deployment the config does not know by an
            # instance id (a hand-written entry, or an id LiteLLM hashed
            # itself): there is nothing to pin to, so do not pin — pinning it
            # would make every later turn log "no longer served" (fix round 1).
            note = "not pinnable: the router entry carries no instance id"
        elif route.pinned is None or route.pinned != iid:
            self.pins.put(route.key, iid)
        # A legacy relay URL (/relay/<worker>/v1) pins the WORKER: its agent
        # picks among its own engines, so say so rather than claim an instance.
        scope, target = pin_scope(served.get(iid) or response_headers.get(API_BASE_HEADER), iid)
        logger.info(
            "chat affinity (#2521): chat=%s… model=%s %s=%s at %s (%s)",
            chat_label(route.chat_id), route.model, scope, target,
            response_headers.get(API_BASE_HEADER) or "?", note)
