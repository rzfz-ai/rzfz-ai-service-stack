# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""A user's MCP recall reaches every dataset they may read, and a failure is an MCP error.

cognee-mcp 1.5.3.dev1 (``src/cognee_client.py``) sends dataset NAMES to cognee's
``/api/v1/recall`` and ``/api/v1/search``: the names the caller passed, or, without any,
the names of every dataset ``GET /api/v1/datasets/`` lists. cognee resolves names only
among the datasets the caller OWNS. One dataset shared with the user (a knowledge group
grant, the company brain) turns the whole request into a 404, so the user's own datasets
are lost as well. cognee-mcp then answers the tool call with that error as ordinary text
(``isError: false``), which an MCP client or a model reads as an answer.

This module, loaded at interpreter start by ``rzfz_mcp_recall.pth`` and patching
cognee-mcp the moment it imports the target modules, does two things:

* ``CogneeClient.recall`` and ``CogneeClient.search`` (API mode) send ``dataset_ids``:
  the ids of every listed dataset whose name or id the caller gave, or of every listed
  dataset when the caller gave none. The list is the user's own view (cognee answers it
  with the user's read grants), so nothing the user may not read is ever addressed. A
  name the user cannot see fails with a clear message instead of a 404. A recall bound
  to a session and no dataset stays as it is: it reads the session cache first.
* An instance with ``RZFZ_COGNEE_DATASET_SCOPE`` set (the company brain) sends only the
  ids of the listed datasets inside that scope, refuses a dataset outside it and never
  falls back to "every readable dataset", so the caller's own memory cannot answer there.
* Every tool registered through cognee-mcp's ``ToolRegistry`` raises an MCP tool error
  when it answers with cognee-mcp's failure convention (one text item starting with
  ``Error:``), so the client gets ``isError: true`` with the same text.

The image build asserts both on the real cognee-mcp and FastMCP (Dockerfile).
"""
from __future__ import annotations

import functools
import importlib.abc
import importlib.util
import sys
import uuid

CLIENT_MODULES = ("src.cognee_client", "cognee_client")
REGISTRY_MODULES = ("src.tool_registry", "tool_registry")
FAILURE_PREFIX = "Error:"


class DatasetNotVisible(LookupError):
    """A dataset the caller named is not among the datasets they may read."""


def _as_uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def resolve_dataset_ids(requested, visible) -> list:
    """The ids of the visible datasets ``requested`` names (names or ids), in order.

    ``requested`` empty: every visible dataset. A name that several visible datasets
    carry (an own one and a shared one) selects all of them. A name or id the caller
    cannot see raises DatasetNotVisible: searching without it would quietly answer
    from fewer datasets than asked for.
    """
    rows = [(str(d.get("id")), d.get("name")) for d in visible or () if d.get("id")]
    if not requested:
        return list(dict.fromkeys(i for i, _ in rows))
    ids, missing = [], []
    for item in requested:
        wanted = str(item).strip()
        as_id = _as_uuid(wanted)
        hits = [i for i, n in rows if n == wanted or (as_id is not None and _as_uuid(i) == as_id)]
        if hits:
            ids.extend(hits)
        else:
            missing.append(wanted)
    if missing:
        raise DatasetNotVisible("Dataset(s) not found among the datasets you can read: %s"
                                % ", ".join(repr(m) for m in missing))
    return list(dict.fromkeys(ids))


#: The datasets an instance is confined to (names or ids, comma-separated). The company
#: brain instance runs as the caller's OWN cognee account, which can also read the
#: caller's personal and shared datasets; without this, a bare recall there answered
#: from all of them and personal content came back labelled as company knowledge.
#: Unset (the personal instance): every dataset the caller may read. Set but empty: none.
SCOPE_ENV = "RZFZ_COGNEE_DATASET_SCOPE"


def dataset_scope():
    """None when the instance is not confined, else the tuple of names/ids (maybe empty)."""
    import os
    raw = os.environ.get(SCOPE_ENV)
    if raw is None:
        return None
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def scoped(visible, scope):
    """The listed datasets inside `scope` (all of them when scope is None). The list is
    the caller's own view, so the result never holds a dataset they may not read."""
    if scope is None:
        return list(visible or ())
    names = set(scope)
    ids = {_as_uuid(s) for s in scope} - {None}
    return [d for d in visible or () if d.get("name") in names or _as_uuid(d.get("id")) in ids]


async def _scoped_ids(client, requested, scope):
    """The dataset ids a recall/search sends. Confined: only ids inside the scope, an
    explicit dataset outside it is refused, and an empty result is refused rather than
    left to cognee's "everything readable" default."""
    visible = scoped(await client.list_datasets(), scope)
    try:
        ids = resolve_dataset_ids(requested, visible)
    except DatasetNotVisible as exc:
        if scope is None:
            raise
        raise DatasetNotVisible("This memory only reads %s; %s"
                                % (", ".join(scope) or "no dataset", exc)) from None
    if not ids and scope is not None:
        raise DatasetNotVisible("None of the datasets this memory reads (%s) is readable "
                                "for you" % (", ".join(scope) or "none configured"))
    return ids


def _post_json(client, path: str, payload: dict):
    async def run():
        response = await client.client.post(f"{client.api_url}{path}", json=payload,
                                             headers=client._get_headers())
        response.raise_for_status()
        return response.json()
    return run()


def patch_client(mod) -> None:
    cls = getattr(mod, "CogneeClient", None)
    if cls is None or getattr(cls.recall, "__rzfz_dataset_ids__", False):
        return
    orig_recall, orig_search = cls.recall, cls.search
    default_prompt = getattr(mod, "_default_recall_system_prompt", lambda: None)

    @functools.wraps(orig_recall)
    async def recall(self, query_text, search_type=None, datasets=None, session_id=None,
                     system_prompt=None, top_k=15):
        scope = dataset_scope()
        if not self.use_api or (session_id and not datasets and scope is None):
            return await orig_recall(self, query_text, search_type=search_type, datasets=datasets,
                                     session_id=session_id, system_prompt=system_prompt, top_k=top_k)
        ids = await _scoped_ids(self, datasets, scope)
        if not ids:     # the user can read nothing: cognee's own answer, unchanged
            return await orig_recall(self, query_text, search_type=search_type, datasets=None,
                                     session_id=session_id, system_prompt=system_prompt, top_k=top_k)
        payload = {"query": query_text, "top_k": top_k, "search_type": None, "dataset_ids": ids}
        if search_type:
            payload["search_type"] = search_type.upper()
        if session_id:
            payload["session_id"] = session_id
        system_prompt = system_prompt or default_prompt()
        if system_prompt:
            payload["system_prompt"] = system_prompt
        return await _post_json(self, "/api/v1/recall", payload)

    @functools.wraps(orig_search)
    async def search(self, query_text, query_type, datasets=None, system_prompt=None, top_k=15):
        if not self.use_api:
            return await orig_search(self, query_text, query_type, datasets=datasets,
                                     system_prompt=system_prompt, top_k=top_k)
        ids = await _scoped_ids(self, datasets, dataset_scope())
        if not ids:
            return await orig_search(self, query_text, query_type, datasets=None,
                                     system_prompt=system_prompt, top_k=top_k)
        payload = {"query": query_text, "search_type": query_type.upper(), "top_k": top_k,
                   "dataset_ids": ids}
        if system_prompt:
            payload["system_prompt"] = system_prompt
        return await _post_json(self, "/api/v1/search", payload)

    recall.__rzfz_dataset_ids__ = search.__rzfz_dataset_ids__ = True
    cls.recall, cls.search = recall, search


def failure_text(result):
    """The text of a cognee-mcp failure answer, or None for anything else."""
    if isinstance(result, (list, tuple)) and len(result) == 1:
        text = getattr(result[0], "text", None)
        if isinstance(text, str) and text.startswith(FAILURE_PREFIX):
            return text
    return None


def _tool_error(text: str) -> Exception:
    try:
        from fastmcp.exceptions import ToolError
    except ImportError:     # pragma: no cover - cognee-mcp always ships FastMCP
        return RuntimeError(text)
    return ToolError(text)


def strict(fn):
    """``fn`` with a failure answer raised as an MCP tool error (isError: true)."""
    if getattr(fn, "__rzfz_strict__", False):
        return fn

    @functools.wraps(fn)
    async def tool(*args, **kwargs):
        result = await fn(*args, **kwargs)
        text = failure_text(result)
        if text is not None:
            raise _tool_error(text)
        return result

    tool.__rzfz_strict__ = True
    return tool


def patch_registry(mod) -> None:
    cls = getattr(mod, "ToolRegistry", None)
    if cls is None or getattr(cls.tool, "__rzfz_strict__", False):
        return
    orig_tool = cls.tool

    @functools.wraps(orig_tool)
    def tool(self, *args, **kwargs):
        decorator = orig_tool(self, *args, **kwargs)

        def register(fn):
            return decorator(strict(fn))
        return register

    tool.__rzfz_strict__ = True
    cls.tool = tool


def self_check() -> None:
    """The image build's check, on the real cognee-mcp and FastMCP (run from /app).

    The client sends ids for a shared dataset given by name, and a tool registered
    through cognee-mcp's registry that answers ``Error: ...`` reaches an MCP client as
    ``isError: true``.
    """
    import asyncio

    import mcp.types as mt
    from fastmcp import Client, FastMCP
    from src.cognee_client import CogneeClient
    from src.tool_registry import ToolRegistry

    assert getattr(CogneeClient.recall, "__rzfz_dataset_ids__", False), "recall not patched"
    assert getattr(CogneeClient.search, "__rzfz_dataset_ids__", False), "search not patched"
    assert getattr(ToolRegistry.tool, "__rzfz_strict__", False), "tool registry not patched"
    shared = {"id": "22222222-2222-2222-2222-222222222222", "name": "shared"}
    assert resolve_dataset_ids(["shared"], [shared]) == [shared["id"]]

    app = FastMCP("rzfz-self-check")

    @ToolRegistry(app).tool(tags={"default"})
    async def probe(query: str) -> list:
        return [mt.TextContent(type="text", text=f"{FAILURE_PREFIX} probe failed")]

    async def call():
        async with Client(app) as client:
            return await client.call_tool("probe", {"query": "q"}, raise_on_error=False)

    result = asyncio.run(call())
    assert result.is_error and "probe failed" in result.content[0].text, result


PATCHES = {**{m: patch_client for m in CLIENT_MODULES},
           **{m: patch_registry for m in REGISTRY_MODULES}}


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
