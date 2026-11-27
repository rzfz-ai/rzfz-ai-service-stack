# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The brain overviews do bounded work per request and stop when the client leaves.

``GET /api/v1/visualize/brains`` reads the graph of every dataset the caller may read,
one after the other, on every call. ``/visualize/brains-summary`` and
``/datasets/graph-summary`` count the graph of every readable dataset whose count is not
cached yet. Each of those reads opens a graph worker. cognee runs one worker process, so
a user who can read hundreds of datasets kept it busy for minutes (74 s at 50 datasets,
more than 300 s at 500), and kept it busy after the client had given up, because nothing
cancels a request whose client is gone: every other user waited 15-20 s per request for
about 15 minutes.

This module, loaded at interpreter start by ``rzfz_brain_views.pth`` and patching cognee
the moment it imports the target modules, does this:

* ``/visualize/brains`` answers one page: at most ``max_datasets`` datasets (query
  parameter, default COGNEE_BRAINS_MAX_DATASETS=10, at most 50), from ``offset``
  (default 0), in a stable order (creation time, then id). The response carries
  ``X-Total-Count`` (readable datasets) and, when there are more, ``X-Next-Offset``.
  Authorization is unchanged: the page is cut from the caller's readable set.
  The page also ends early when the time budget (below) would not hold one more graph
  open: the answer is then a shorter page, still 200, whose ``X-Next-Offset`` names the
  first dataset not returned, so a client that follows it gets every dataset. (On a box
  where one open takes about 2 s, a page of 50 does not fit into 60 s.) Before the next
  open the page needs 1.5 times the slowest open so far plus a reserve of a twentieth
  of the budget to be left.
* The two count views open at most COGNEE_GRAPH_COUNTS_MAX_OPENS=10 graphs per request.
  A dataset beyond that comes back with zero counts and a null ``computedAt``, which
  these views already use for "not counted yet"; the next poll counts the next ones and
  every count is cached for good once taken.
* For these three views a request is cancelled when its client disconnects, and after
  COGNEE_VIEW_TIME_LIMIT_S=60 s it is cancelled and answered 503 with Retry-After, so
  no abandoned overview keeps the worker busy. For ``/visualize/brains`` the 503 comes
  only when not even one dataset could be read in time.

The image build asserts the seams on the real cognee (Dockerfile).
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import importlib.abc
import importlib.util
import logging
import os
import sys
from urllib.parse import parse_qs

CLIENT = "cognee.api.client"
VISUALIZE = "cognee.api.v1.visualize"
COUNTS = "cognee.modules.data.methods.get_datasets_graph_counts"
GUARDED_PATHS = ("/api/v1/visualize/brains", "/api/v1/visualize/brains-summary",
                 "/api/v1/datasets/graph-summary")
MAX_DATASETS_CEILING = 50
RETRY_AFTER_S = 10
OPEN_SAFETY = 1.5           # the next open may take this much longer than the slowest so far
RESERVE_SHARE = 0.05        # of the time budget, kept for the answer itself

log = logging.getLogger("rzfz.brain_views")
_VIEW: contextvars.ContextVar = contextvars.ContextVar("rzfz_brain_view", default=None)


def _env_number(name: str, default, cast=int):
    try:
        value = cast(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def page_size() -> int:
    return min(_env_number("COGNEE_BRAINS_MAX_DATASETS", 10), MAX_DATASETS_CEILING)


def opens_per_request() -> int:
    return _env_number("COGNEE_GRAPH_COUNTS_MAX_OPENS", 10)


def time_limit() -> float:
    return _env_number("COGNEE_VIEW_TIME_LIMIT_S", 60.0, float)


def _query_int(query: dict, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(query.get(name, [default])[0])))
    except (TypeError, ValueError):
        return default


def _sort_key(dataset):
    return (str(getattr(dataset, "created_at", "") or ""), str(dataset.id))


class ViewGuard:
    """ASGI middleware for the guarded views: request state, disconnect and time limit."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http" or scope.get("method") != "GET"
                or scope.get("path", "").rstrip("/") not in GUARDED_PATHS):
            return await self.app(scope, receive, send)
        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        state = {
            "path": scope["path"].rstrip("/"),
            "max_datasets": _query_int(query, "max_datasets", page_size(), 1, MAX_DATASETS_CEILING),
            "offset": _query_int(query, "offset", 0, 0, 1 << 31),
            "opens_left": opens_per_request(),
            "headers": [],
            "deadline": asyncio.get_running_loop().time() + time_limit(),
        }
        started = False

        async def send_with_headers(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = dict(message, headers=list(message.get("headers") or []) + state["headers"])
            await send(message)

        inbox: asyncio.Queue = asyncio.Queue()

        async def app_receive():
            return await inbox.get()

        token = _VIEW.set(state)
        try:
            work = asyncio.ensure_future(self.app(scope, app_receive, send_with_headers))
        finally:
            _VIEW.reset(token)
        left = asyncio.Event()

        async def watch():
            while True:
                message = await receive()
                await inbox.put(message)
                if message["type"] == "http.disconnect":
                    left.set()
                    return

        watcher = asyncio.ensure_future(watch())
        gone = asyncio.ensure_future(left.wait())
        try:
            done, _ = await asyncio.wait({work, gone}, timeout=time_limit(),
                                         return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return work.result()
            work.cancel()
            try:
                await work
            except (asyncio.CancelledError, Exception):
                pass
            if gone in done:
                log.warning("client left %s; its work was cancelled", state["path"])
                return
            log.warning("%s ran past %ss; cancelled, answering 503", state["path"], time_limit())
            if not started:
                body = b'{"error":"The overview took too long; try again."}'
                await send({"type": "http.response.start", "status": 503, "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(RETRY_AFTER_S).encode())]})
                await send({"type": "http.response.body", "body": body})
        finally:
            for task in (watcher, gone):
                task.cancel()


def patch_client(mod) -> None:
    app = getattr(mod, "app", None)
    if app is None or getattr(app, "__rzfz_brain_views__", False):
        return
    app.add_middleware(ViewGuard)
    app.__rzfz_brain_views__ = True


def patch_visualize(pkg) -> None:
    vis = sys.modules.get(VISUALIZE + ".visualize")
    orig = getattr(vis, "build_brains_payload", None)
    if vis is None or orig is None or getattr(orig, "__rzfz_brain_views__", False):
        return

    @functools.wraps(orig)
    async def build_brains_payload(user=None, max_nodes=vis.DEFAULT_MAX_NODES):
        state = _VIEW.get()
        if state is None:           # not an HTTP overview: cognee's own behaviour
            return await orig(user=user, max_nodes=max_nodes)
        if not user:
            user = await vis.get_default_user()
        datasets = sorted(await vis.get_all_user_permission_datasets(user, "read"), key=_sort_key)
        start, size = state["offset"], state["max_datasets"]
        page = datasets[start:start + size]
        loop = asyncio.get_running_loop()
        budget_end = state["deadline"] - RESERVE_SHARE * time_limit()
        payload: dict = {}
        slowest = 0.0
        for dataset in page:
            if payload and loop.time() + OPEN_SAFETY * slowest > budget_end:
                log.warning("brains overview: time budget allows %d of %d datasets; answering "
                            "a shorter page", len(payload), len(page))
                break
            began = loop.time()
            graph_data = await vis.fetch_dataset_graph_data(dataset, max_nodes=max_nodes)
            slowest = max(slowest, loop.time() - began)
            payload[str(dataset.id)] = vis.build_brain_summary_payload(dataset.name, graph_data)
        state["headers"].append((b"x-total-count", str(len(datasets)).encode()))
        following = start + len(payload)
        if following < len(datasets):
            state["headers"].append((b"x-next-offset", str(following).encode()))
        return payload

    build_brains_payload.__rzfz_brain_views__ = True
    vis.build_brains_payload = build_brains_payload
    pkg.build_brains_payload = build_brains_payload


def patch_counts(mod) -> None:
    orig = getattr(mod, "_count_and_cache", None)
    if orig is None or getattr(orig, "__rzfz_brain_views__", False):
        return

    @functools.wraps(orig)
    async def _count_and_cache(dataset, pipeline_run_id):
        state = _VIEW.get()
        if state is not None and pipeline_run_id not in mod._inflight_counts:
            if state["opens_left"] <= 0:
                return mod.DatasetGraphCounts(pipeline_run_id=pipeline_run_id)
            state["opens_left"] -= 1
        return await orig(dataset, pipeline_run_id)

    _count_and_cache.__rzfz_brain_views__ = True
    mod._count_and_cache = _count_and_cache


PATCHES = {CLIENT: patch_client, VISUALIZE: patch_visualize, COUNTS: patch_counts}


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name not in PATCHES:
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def exec_and_patch(module):
            exec_module(module)
            PATCHES[name](module)

        spec.loader.exec_module = exec_and_patch
        return spec


def install() -> None:
    for name, fn in PATCHES.items():
        if name in sys.modules:
            fn(sys.modules[name])
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())


install()
