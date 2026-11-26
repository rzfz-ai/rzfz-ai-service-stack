# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3022 — a CONCURRENCY bound for cognee's LLM calls.

cognee 1.6.5 only paces its LLM calls by a rate (LLM_RATE_LIMIT_REQUESTS per
LLM_RATE_LIMIT_INTERVAL). One graph-extraction call runs ~40 s on a box's local
model, so at 1 request per second the calls in flight climb past the LLM
Manager's box-wide cap within seconds; the manager answers 429 (it rejects by
design, #19), cognee's retries run out and a 50-document dataset ends ERRORED.

This module wraps cognee's single dispatch seam,
``cognee.shared.rate_limiting._governed_llm_dispatch`` (every adapter enters it
for every attempt, retries included), in a semaphore of
COGNEE_LLM_MAX_CONCURRENT slots. It is loaded at interpreter start by
``rzfz_llm_bound.pth`` and patches the module the moment cognee imports it, so
nothing is imported early. 0 disables the bound. The image build asserts the
patch takes on the real cognee (Dockerfile), so a cognee upgrade that moves
the seam fails the build instead of silently losing the bound.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.abc
import importlib.util
import os
import sys

TARGET = "cognee.shared.rate_limiting"


def limit() -> int:
    try:
        return max(0, int(os.environ.get("COGNEE_LLM_MAX_CONCURRENT", "2") or 0))
    except ValueError:
        return 2


def patch(mod) -> None:
    """Wrap ``mod._governed_llm_dispatch`` in a per-event-loop semaphore."""
    orig = getattr(mod, "_governed_llm_dispatch", None)
    if orig is None or getattr(orig, "__rzfz_bound__", False):
        return
    n = limit()
    if n <= 0:
        return
    sems: dict[int, asyncio.Semaphore] = {}

    @contextlib.asynccontextmanager
    async def bounded():
        loop = asyncio.get_running_loop()
        sem = sems.get(id(loop))
        if sem is None:
            sem = sems[id(loop)] = asyncio.Semaphore(n)
        async with sem:
            async with orig():
                yield

    bounded.__rzfz_bound__ = True
    bounded.__rzfz_limit__ = n
    mod._governed_llm_dispatch = bounded


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != TARGET:
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
            patch(module)

        spec.loader.exec_module = exec_and_patch
        return spec


def install() -> None:
    if TARGET in sys.modules:
        patch(sys.modules[TARGET])
    elif not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())


install()
