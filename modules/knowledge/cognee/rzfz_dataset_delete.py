# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Deleting a dataset stops its running knowledge graph build, and the delete answers soon.

cognee 1.6.5 serialises ``DELETE /api/v1/datasets/{id}`` with the dataset's pipeline runs
through one per-dataset lock: a delete during a running cognify waits until the cognify has
finished (29 minutes, measured), the client gives up long before, and the build that the
user wanted gone runs to its end and writes the data first. On an earlier release, work of
a deleted dataset was even written again after the delete had returned.

This module, loaded at interpreter start by ``rzfz_dataset_delete.pth`` and patching cognee
the moment it imports the target modules, does this:

* ``datasets.empty_dataset`` (the delete behind the route) authorizes exactly as before.
  Only once the caller may delete the dataset is it marked as being deleted, then the
  delete runs in its own task. The request waits up to DELETE_WAIT_S for it: done, the
  answer is the usual one; still waiting for the build to stop, the answer is
  202 Accepted and the delete finishes in the background (the dataset leaves
  ``GET /api/v1/datasets`` when it is gone; another DELETE meanwhile answers 202 again).
* The build is stopped: every task of the dataset that is in an LLM or embedding call (the
  run's item tasks and what they gather, never the task driving the run) is cancelled at once, and any later LLM or embedding call of the dataset
  is refused before it is sent. The pipeline's own error path then rolls the run back,
  records it as errored and releases the lock, and the delete proceeds. Nothing of the
  dataset is written after the delete.
* A cognify call over several datasets goes on with the others: the run of the deleted
  dataset ends as errored instead of cancelling the whole background run.

* The dataset's session cache is purged after cognee's delete: every cached question and
  answer, agent trace and session-context entry of a session named ``*_<dataset id>`` (the
  dataset's default session, for every user). cognee removes those only for sessions it
  has a session record for, and that record is written best-effort after the answer is
  cached, so an answer could survive the delete for the cache's 7-day lifetime and, as a
  same-owner re-create gets the same dataset id, be served again.

The mark is lifted when the delete has finished, so a dataset created again under the same
name can be built. The image build asserts the seams on the real cognee (Dockerfile).
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
import time
import weakref

RATE_LIMITING = "cognee.shared.rate_limiting"
PIPELINE = "cognee.modules.pipelines.operations.pipeline"
DATASETS_API = "cognee.api.v1.datasets.datasets"
CONTEXT = "cognee.context_global_variables"

log = logging.getLogger("rzfz.dataset_delete")
DELETING: dict = {}                     # dataset id (str) -> the delete task
_WORKERS: dict = {}                     # dataset id (str) -> WeakSet of tasks in LLM/embedding calls
_DRIVERS: "weakref.WeakSet" = weakref.WeakSet()   # tasks that drive a pipeline run (never cancelled)
_DELETER: contextvars.ContextVar = contextvars.ContextVar("rzfz_dataset_deleter", default=False)
_accepted_class = None


def delete_wait_s() -> float:
    try:
        value = float(os.environ.get("COGNEE_DELETE_WAIT_S", "") or 20)
    except ValueError:
        return 20.0
    return value if value >= 0 else 20.0


class DatasetBeingDeleted(asyncio.CancelledError):
    """Raised instead of an LLM or embedding call of a dataset that is being deleted.

    A CancelledError, so cognee's LLM retry policy does not retry it and a pipeline
    treats it like the cancellation it is (roll back, record as errored).
    """


def _current_dataset():
    mod = sys.modules.get(CONTEXT)
    var = getattr(mod, "current_dataset_id", None)
    value = var.get() if var is not None else None
    return str(value) if value is not None else None


def _guard_call():
    """Refuse an LLM or embedding call of a dataset being deleted; note who makes it."""
    dataset = _current_dataset()
    if dataset is None or _DELETER.get():
        return
    if dataset in DELETING:
        raise DatasetBeingDeleted("dataset %s is being deleted" % dataset)
    task = asyncio.current_task()
    if task is not None:
        _WORKERS.setdefault(dataset, weakref.WeakSet()).add(task)


def _stop_items(dataset: str) -> int:
    """Cancel the dataset's tasks that are in (or waiting for) an LLM or embedding call.

    These are the run's item tasks and the sub-tasks they gather; the task that drives
    the run is left alone, so the run's own error path records and releases it."""
    stopped = 0
    me = asyncio.current_task()
    for task in list(_WORKERS.get(dataset, ())):
        if task is not me and task not in _DRIVERS and not task.done():
            task.cancel()
            stopped += 1
    return stopped


def patch_rate_limiting(mod) -> None:
    for name in ("llm_rate_limiter_context_manager", "embedding_rate_limiter_context_manager"):
        orig = getattr(mod, name, None)
        if orig is None or getattr(orig, "__rzfz_dataset_delete__", False):
            continue

        def guarded(*args, __orig=orig, **kwargs):
            _guard_call()
            return __orig(*args, **kwargs)

        functools.update_wrapper(guarded, orig)
        guarded.__rzfz_dataset_delete__ = True
        setattr(mod, name, guarded)


def patch_pipeline(mod) -> None:
    orig = getattr(mod, "run_tasks", None)
    if orig is None or getattr(orig, "__rzfz_dataset_delete__", False):
        return

    @functools.wraps(orig)
    async def run_tasks(tasks, dataset_id, *args, **kwargs):
        source = orig(tasks, dataset_id, *args, **kwargs)
        try:
            while True:
                _DRIVERS.add(asyncio.current_task())    # priming and driving may be two tasks
                try:
                    info = await source.__anext__()
                except StopAsyncIteration:
                    return
                yield info
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if str(dataset_id) in DELETING and task is not None and not task.cancelling():
                # The run of the dataset being deleted has rolled back and been
                # recorded as errored; let the caller go on with its other datasets.
                log.warning("stopped the knowledge graph build of dataset %s: it is being deleted",
                            dataset_id)
                return
            raise
        finally:
            await source.aclose()

    run_tasks.__rzfz_dataset_delete__ = True
    mod.run_tasks = run_tasks


async def purge_session_cache(dataset_id) -> int:
    """Delete the SQL session cache rows of the dataset's sessions; the number removed.

    Matches the session id suffix ``_<dataset id>`` across all users, independent of
    cognee's session records. A non-SQL cache backend is left to cognee's own purge."""
    from cognee.infrastructure.databases.cache import get_cache_engine
    from sqlalchemy import delete

    engine = get_cache_engine()
    if engine is None or not hasattr(engine, "sessionmaker"):
        return 0
    from cognee.infrastructure.databases.cache.sql import tables

    await engine._ensure_initialized()
    suffix = "_%s" % dataset_id
    removed = 0
    async with engine.sessionmaker() as session, session.begin():
        for table in (tables.cache_qa_entries, tables.cache_trace_entries,
                      tables.cache_session_context):
            result = await session.execute(delete(table).where(table.c.session_id.endswith(suffix, autoescape=True)))
            removed += result.rowcount or 0
    return removed


async def _delete_then_purge(orig, dataset_id, user):
    result = await orig(dataset_id, user)
    try:
        removed = await purge_session_cache(dataset_id)
        if removed:
            log.warning("dataset %s: removed %d cached session row(s) cognee's delete left",
                        dataset_id, removed)
    except Exception as error:      # never fails a delete that has happened
        log.error("dataset %s: purging its session cache failed: %s", dataset_id, error)
    return result


def deletion_accepted() -> Exception:
    """HTTP 202: the delete goes on in the background."""
    global _accepted_class
    if _accepted_class is None:
        from starlette.exceptions import HTTPException

        class DeletionAccepted(HTTPException):
            def __init__(self):
                super().__init__(status_code=202, detail=(
                    "The dataset is being deleted. Its running knowledge graph build is being "
                    "stopped; the dataset disappears from the dataset list when it is gone."))

        _accepted_class = DeletionAccepted
    return _accepted_class()


def patch_datasets_api(mod) -> None:
    cls = getattr(mod, "datasets", None)
    orig = getattr(cls, "empty_dataset", None)
    if orig is None or getattr(orig, "__rzfz_dataset_delete__", False):
        return

    @functools.wraps(orig)
    async def empty_dataset(dataset_id, user=None):
        key = str(dataset_id)
        running = DELETING.get(key)
        if running is not None and not running.done():
            raise deletion_accepted()
        try:
            who = user or await mod.get_default_user()
            allowed = await mod.get_authorized_dataset(who, dataset_id, "delete")
        except Exception:
            allowed = None
        if not allowed:     # cognee's own answer (401/403/404), nothing is stopped
            return await orig(dataset_id, user)

        token = _DELETER.set(True)
        try:
            task = asyncio.ensure_future(_delete_then_purge(orig, dataset_id, user))
        finally:
            _DELETER.reset(token)
        DELETING[key] = task
        started = time.monotonic()

        def finished(t, key=key):
            if DELETING.get(key) is t:
                DELETING.pop(key, None)
                _WORKERS.pop(key, None)
            if not t.cancelled() and t.exception() is not None:
                log.error("deleting dataset %s failed: %s", key, t.exception())
            else:
                log.info("dataset %s deleted after %.1f s", key, time.monotonic() - started)

        task.add_done_callback(finished)
        stopped = _stop_items(key)
        if stopped:
            log.warning("deleting dataset %s: stopped %d running build step(s)", key, stopped)
        done, _ = await asyncio.wait({task}, timeout=delete_wait_s())
        if task in done:
            return task.result()
        raise deletion_accepted()

    empty_dataset.__rzfz_dataset_delete__ = True
    cls.empty_dataset = staticmethod(empty_dataset)


PATCHES = {RATE_LIMITING: patch_rate_limiting, PIPELINE: patch_pipeline,
           DATASETS_API: patch_datasets_api}


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
