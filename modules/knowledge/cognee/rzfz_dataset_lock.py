# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""A busy dataset graph ends in a bounded wait and a 503, never a long stall or a 500.

With backend access control every dataset has its own graph file
(``databases/<owner>/<dataset>.pkl``). cognee 1.6.5 opens it in a dedicated worker
process, which holds an exclusive file lock for as long as it lives, and keeps at
most DATABASE_MAX_LRU_CACHE_SIZE (default 6) of those workers. A search over more
datasets evicts and reopens graphs all the time. The eviction closes a worker at
once unless something still holds its engine; then the close is deferred and is not
registered anywhere (get_graph_engine.py, ``_GraphEngineHandle``, "Known limitation").
The next open of that dataset spawns a second worker, which blocks the event loop
while it retries and then fails with "Could not set lock on file : <path>".

This module, loaded at interpreter start by ``rzfz_dataset_lock.pth`` and patching
cognee the moment it imports the target modules (nothing is imported early), does this:

* ``get_graph_engine.acreate_graph_engine``, the one path every graph open of a
  ``get_graph_engine()`` caller takes, passes a gate. A graph already open is used as
  it is, so readers of one dataset share a worker and run in parallel, and nothing is
  held across a slow retrieval or LLM call. Otherwise the gate closes an evicted worker
  of this dataset that only an idle holder kept alive (the holder reopens on next use,
  as after cognee's own idle sweep), then waits on the event loop, without spawning
  anything, until the close has finished and the file lock is free.
* The wait is bounded by BUSY_WAIT_S per open. When the bound passes, or a lock error
  comes back anyway, the caller gets GraphBusy: HTTP 503 with Retry-After and no path.
  Once one graph of a search has timed out, the other graphs of that search stop
  waiting, so one search waits at most BUSY_WAIT_S in total for busy graphs (plus one
  open that lost a race, which cognee's worker retries for a few seconds).
* ``cognee.api.v1.search.search``, the search the API calls, runs once. A lock error
  that reaches it becomes GraphBusy. It is never run again as a whole: it may take
  minutes over many datasets, so a retry there is what turned one lock error into a
  stall of several times the search. Any other failure is re-raised without file system
  paths (the original exception stays the cause, so cognee's log keeps it).

The gate bounds waiting for a busy graph only, never the search itself. Which datasets
a search may read is decided before any of this, and each search keeps its own database
context. COGNEE_SEARCH_DATASET_LOCK=0 turns the gate off (not the 503 or the path
scrub). The image build asserts the patch on the real cognee (Dockerfile).
"""
from __future__ import annotations

import asyncio
import contextvars
import fcntl
import functools
import importlib.abc
import importlib.util
import logging
import os
import re
import struct
import sys
import time

GRAPH_ENGINE = "cognee.infrastructure.databases.graph.get_graph_engine"
SEARCH_API = "cognee.api.v1.search.search"
TARGETS = (GRAPH_ENGINE, SEARCH_API)

LOCK_ERROR = "Could not set lock on file"
BUSY_MESSAGE = "The dataset's knowledge graph is busy; try the search again."
BUSY_WAIT_S = 10.0      # the longest one open waits for a busy graph
RETRY_AFTER_S = 5       # what the 503 tells the client to wait
POLL_S = 0.1            # a normal worker close takes about 1 s
GATED_PROVIDERS = ("kuzu", "ladybug")
_PATH = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.@+-]+){2,}/?")
_FLOCK = "hhqqi"        # struct flock on 64-bit Linux

log = logging.getLogger("rzfz.dataset_graph")
_REQUEST: contextvars.ContextVar = contextvars.ContextVar("rzfz_graph_busy_request", default=None)
_busy_class = None


def enabled() -> bool:
    return (os.environ.get("COGNEE_SEARCH_DATASET_LOCK", "1") or "1").strip() != "0"


def _is_lock_error(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if LOCK_ERROR in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def scrub(text: str) -> str:
    """The message with every file system path replaced by <path>."""
    return _PATH.sub("<path>", text)


class SearchFailed(RuntimeError):
    """A search error whose message carries no server path."""


def graph_busy() -> Exception:
    """An HTTP 503 with Retry-After that the search route lets through.

    The route re-raises CogneeApiError and turns everything else into a 500, so this is
    one. HTTPException comes first in the bases: the app's handlers are looked up along
    the class hierarchy, and only the HTTPException handler sends the headers.
    """
    global _busy_class
    if _busy_class is None:
        from cognee.exceptions import CogneeApiError
        from starlette.exceptions import HTTPException

        class GraphBusy(HTTPException, CogneeApiError):
            def __init__(self):
                HTTPException.__init__(self, status_code=503, detail=BUSY_MESSAGE,
                                       headers={"Retry-After": str(RETRY_AFTER_S)})
                self.message, self.name, self.remediation = BUSY_MESSAGE, "GraphBusy", None

        _busy_class = GraphBusy
    return _busy_class()


def lock_holder(path: str):
    """The pid holding the graph file's lock, or None. Asks the kernel; takes no lock.

    Only for graphs opened in worker processes: closing any descriptor of a file drops
    the POSIX locks THIS process holds on it, so the caller checks the mode first.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None                 # a new dataset: there is no file yet
    try:
        probe = struct.pack(_FLOCK, fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
        result = struct.unpack(_FLOCK, fcntl.fcntl(fd, fcntl.F_GETLK, probe))
    except OSError:
        return None
    finally:
        os.close(fd)
    return None if result[0] == fcntl.F_UNLCK else result[4]


class _DeferredCloses:
    """Evicted graph workers whose close waits for an idle holder to let go."""

    def __init__(self, cache):
        self._cache = cache
        self._entries: dict = {}    # cache key -> [entries]
        detach = cache._detach_entry

        @functools.wraps(detach)
        def detach_and_note(entry):
            detach(entry)
            if getattr(entry.value, "_subprocess_mode", False) and not entry.closed:
                kept = [e for e in self._entries.get(entry.key, []) if not e.closed]
                self._entries[entry.key] = kept + [entry]

        cache._detach_entry = detach_and_note

    def close_now(self, key) -> int:
        """cognee's evict_and_close for an entry that already left the cache."""
        closed = 0
        for entry in self._entries.pop(key, []):
            value = None
            with entry._lock:
                entry.close_requested = True
                proxy, entry.proxy = entry.proxy, None
                if not entry.closed:
                    entry.closed, value = True, entry.value
            if value is not None:
                self._cache._track_close(entry.key, value)
                closed += 1
            del proxy               # outside the lock: it may run the proxy's finalizer
        return closed


def patch_engine(mod) -> None:
    orig = getattr(mod, "acreate_graph_engine", None)
    if orig is None or getattr(orig, "__rzfz_dataset_lock__", False) or not enabled():
        return
    cache = mod._create_graph_engine.cache_info.__self__
    deferred = _DeferredCloses(cache)

    @functools.wraps(orig)
    async def acreate_graph_engine(**kwargs):
        if (str(kwargs.get("graph_database_provider")).lower() not in GATED_PROVIDERS
                or not kwargs.get("graph_database_subprocess_enabled")
                or not kwargs.get("graph_file_path")):
            return await orig(**kwargs)
        key = mod._resolve_graph_engine_args(kwargs)
        path = kwargs["graph_file_path"]
        deadline = time.monotonic() + BUSY_WAIT_S
        request = _REQUEST.get()
        last_error, holder, closing = None, None, ()
        while True:
            if cache.contains(key):
                return await orig(**kwargs)
            if deferred.close_now(key):
                log.warning("closed an idle graph worker of dataset %s so that it can reopen",
                            os.path.basename(path))
            with cache._lock:
                closing = cache._snapshot_pending_closes(key)
            holder = None if closing else lock_holder(path)
            if not closing and holder is None:
                try:
                    return await orig(**kwargs)
                except Exception as exc:
                    if not _is_lock_error(exc):
                        raise
                    last_error = exc
            already_busy = request is not None and request["busy"]
            if already_busy or time.monotonic() >= deadline:
                if request is not None:
                    request["busy"] = True
                log.warning("graph of dataset %s stayed busy (closing=%d, lock holder pid=%s); "
                            "answering 503", os.path.basename(path), len(closing), holder)
                raise graph_busy() from last_error
            await asyncio.sleep(POLL_S)

    acreate_graph_engine.__rzfz_dataset_lock__ = True
    mod.acreate_graph_engine = acreate_graph_engine


def patch_api(mod) -> None:
    orig = getattr(mod, "search", None)
    if orig is None or getattr(orig, "__rzfz_dataset_lock__", False):
        return
    try:   # cognee's own errors carry their status and message; never touched
        from cognee.exceptions import CogneeApiError
    except Exception:   # pragma: no cover - a cognee without it
        CogneeApiError = ()

    @functools.wraps(orig)
    async def search(*args, **kwargs):
        token = _REQUEST.set({"busy": False})
        try:
            return await orig(*args, **kwargs)
        except CogneeApiError:
            raise
        except Exception as exc:
            if _is_lock_error(exc):
                log.warning("a search met a graph file lock; answering 503")
                raise graph_busy() from exc
            raise SearchFailed(scrub(str(exc)) or type(exc).__name__) from exc
        finally:
            _REQUEST.reset(token)

    search.__rzfz_dataset_lock__ = True
    mod.search = search


PATCHES = {GRAPH_ENGINE: patch_engine, SEARCH_API: patch_api}


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
