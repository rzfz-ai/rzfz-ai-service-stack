"""
id: cognee
title: Cognee Memory Pipe
author: razzfazz-stack
version: 1.0.0
required_open_webui_version: 0.5.0
license: MIT
description: Use Cognee (GraphRAG knowledge base) as a long-term memory backend. Each turn ingests the message into the user's private Cognee dataset and answers from the accumulated knowledge graph (GRAPH_COMPLETION). Per-user isolation via dataset namespacing.
requirements: requests, pydantic
"""

# M033 S12 — Cognee consumer wiring (OpenWebUI).
#
# Cognee ships as an EXPERIMENTAL profile that builds a GraphRAG knowledge
# base, but until this pipe nothing in the stack actually *used* it. This
# pipe makes Cognee selectable as a chat "model" that, on every turn:
#   1. ingests the user's message into their private dataset  (POST /add)
#   2. folds it into the knowledge graph                       (POST /cognify, bg)
#   3. answers the query from the graph                        (POST /search,
#                                                                GRAPH_COMPLETION)
#
# Per-user isolation: the dataset name is derived from the calling user's
# id/email, so one user's memories never leak into another's retrieval.
#
# Auth: Cognee uses fastapi-users. We log in once with the admin credentials
# (COGNEE_ADMIN_EMAIL / COGNEE_ADMIN_PASSWORD, the fleet bootstrap password
# after M033 S17 alignment) and cache the bearer token, re-authenticating on
# 401. The OpenWebUI container receives these via env (see core/compose.yml /
# the openwebui service). Valves override env for ad-hoc testing.
#
# Validated end-to-end on the dev box 2026-05-24: ingest "the secret word is
# RAZZBERRY" -> cognify -> search "what is the secret word" -> ["RAZZBERRY"].
# The API recipe (multipart file 'data=@...;type=text/plain' on /add, JSON
# {"datasets":[name]} on /cognify, {"query","searchType","datasets"} on
# /search) was confirmed against cognee 1.x running in-stack.

import asyncio
import logging
import os
import re
import threading
import time
from typing import Any, Awaitable, Callable, List, Optional

import requests
from pydantic import BaseModel, Field

log = logging.getLogger("cognee_pipe")
log.setLevel(logging.INFO)

_LOGIN = "/api/v1/auth/login"
_ADD = "/api/v1/add"
_COGNIFY = "/api/v1/cognify"
_SEARCH = "/api/v1/search"


def _dataset_for_user(user: dict) -> str:
    """Stable, filesystem/identifier-safe per-user dataset name. Cognee
    partitions data by dataset, so this is our multi-tenancy boundary."""
    raw = (user or {}).get("id") or (user or {}).get("email") or "shared"
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", str(raw)).strip("_").lower()
    return f"owui_{slug or 'shared'}"


class _TokenCache:
    """Thread-safe bearer-token cache. Re-login is cheap and only happens on
    first use or after a 401, so no proactive expiry tracking is needed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token = ""

    def get(self) -> str:
        with self._lock:
            return self._token

    def set(self, token: str) -> None:
        with self._lock:
            self._token = token

    def clear(self) -> None:
        with self._lock:
            self._token = ""


_tokens = _TokenCache()


class Pipe:
    class Valves(BaseModel):
        COGNEE_BASE_URL: str = Field(
            default=os.getenv("COGNEE_BASE_URL", "http://cognee:8000"),
            description="Cognee API base URL (in-stack service name).",
        )
        COGNEE_ADMIN_EMAIL: str = Field(
            default=os.getenv("COGNEE_ADMIN_EMAIL", ""),
            description="Cognee admin email (fastapi-users login).",
        )
        COGNEE_ADMIN_PASSWORD: str = Field(
            default=os.getenv("COGNEE_ADMIN_PASSWORD", ""),
            description="Cognee admin password (fleet bootstrap password).",
        )
        SEARCH_TYPE: str = Field(
            default="GRAPH_COMPLETION",
            description="Cognee search type. GRAPH_COMPLETION answers in natural "
            "language from the graph; RAG_COMPLETION / CHUNKS are alternatives.",
        )
        TOP_K: int = Field(default=10, description="Max graph nodes to retrieve.")
        INGEST: bool = Field(
            default=True,
            description="Ingest each user turn into the graph (grows memory). "
            "Disable for read-only querying.",
        )
        PER_USER_DATASETS: bool = Field(
            default=True,
            description="Namespace each user's memory in their own dataset. "
            "Disable to share one collective memory across all users.",
        )
        REQUEST_TIMEOUT: int = Field(default=300)
        DEBUG: bool = Field(default=False)

    def __init__(self) -> None:
        self.type = "manifold"
        self.id = "cognee"
        self.name = "Cognee: "
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        return [{"id": "memory", "name": "Memory"}]

    # ------------------------------------------------------------------
    def _base(self) -> str:
        return self.valves.COGNEE_BASE_URL.rstrip("/")

    def _login(self) -> str:
        """Authenticate and return a bearer token, caching it."""
        resp = requests.post(
            f"{self._base()}{_LOGIN}",
            data={
                "username": self.valves.COGNEE_ADMIN_EMAIL,
                "password": self.valves.COGNEE_ADMIN_PASSWORD,
            },
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token", "")
        if not token:
            raise RuntimeError("cognee login returned no access_token")
        _tokens.set(token)
        return token

    def _auth_headers(self, force_login: bool = False) -> dict:
        token = "" if force_login else _tokens.get()
        if not token:
            token = self._login()
        return {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        """Do a request; on 401 re-login once and retry."""
        url = f"{self._base()}{path}"
        headers = kw.pop("headers", {})
        headers.update(self._auth_headers())
        resp = requests.request(method, url, headers=headers,
                                timeout=self.valves.REQUEST_TIMEOUT, **kw)
        if resp.status_code == 401:
            _tokens.clear()
            headers.update(self._auth_headers(force_login=True))
            resp = requests.request(method, url, headers=headers,
                                    timeout=self.valves.REQUEST_TIMEOUT, **kw)
        return resp

    def _ingest(self, text: str, dataset: str) -> None:
        """Add a turn to the user's dataset + fold it into the graph.
        Best-effort: ingestion failures must not break the chat answer."""
        try:
            files = {"data": ("turn.txt", text.encode("utf-8"), "text/plain")}
            r = self._request("POST", _ADD, files=files,
                              data={"datasetName": dataset})
            if not r.ok and self.valves.DEBUG:
                log.warning("cognee /add %s: %s", r.status_code, r.text[:200])
            # Cognify in the background so the chat turn doesn't wait on the
            # full graph build (which calls the LLM and can take minutes).
            self._request("POST", _COGNIFY,
                          json={"datasets": [dataset], "run_in_background": True})
        except Exception as e:  # noqa: BLE001 — never let ingest break the answer
            log.warning("cognee ingest failed (non-fatal): %s", e)

    def _search(self, query: str, dataset: str) -> str:
        r = self._request("POST", _SEARCH, json={
            "query": query,
            "searchType": self.valves.SEARCH_TYPE,
            "datasets": [dataset],
            "topK": self.valves.TOP_K,
        })
        # A 404 here means the dataset doesn't exist yet (cold start — the user
        # hasn't ingested anything into their memory namespace). Treat as empty
        # rather than an error so pipe() shows the friendly cold-start notice.
        if r.status_code == 404:
            return ""
        r.raise_for_status()
        out = r.json()
        # GRAPH_COMPLETION returns a list of answer strings; CHUNKS etc. may
        # return richer objects. Normalise to a readable string.
        if isinstance(out, list):
            parts = [o if isinstance(o, str) else str(o) for o in out]
            return "\n\n".join(p for p in parts if p).strip()
        return str(out).strip()

    # ------------------------------------------------------------------
    async def pipe(self, body: dict,
                   __user__: Optional[dict] = None,
                   __metadata__: Optional[dict] = None,
                   __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
                   __task__: Optional[str] = None,
                   **kwargs: Any):
        __user__ = __user__ or {}
        if not self.valves.COGNEE_ADMIN_PASSWORD:
            return ("⚠️ Cognee is not configured: COGNEE_ADMIN_PASSWORD is empty. "
                    "Set it in the pipe valves or the openwebui service env.")

        messages: List[dict] = body.get("messages", []) or []
        query = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                query = c if isinstance(c, str) else str(c)
                break
        if not query.strip():
            return "Ask a question and Cognee will answer from your memory graph."

        dataset = (_dataset_for_user(__user__)
                   if self.valves.PER_USER_DATASETS else "owui_shared")

        async def _emit(desc: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__({"type": "status",
                    "data": {"description": desc, "done": done}})

        try:
            if self.valves.INGEST and __task__ is None:
                await _emit("Cognee: updating memory…")
                await asyncio.to_thread(self._ingest, query, dataset)
            await _emit("Cognee: searching knowledge graph…")
            answer = await asyncio.to_thread(self._search, query, dataset)
            await _emit("", done=True)
        except requests.HTTPError as e:
            log.error("cognee request failed: %s", e)
            return f"⚠️ Cognee request failed: {e}"
        except Exception as e:  # noqa: BLE001
            log.error("cognee pipe error: %s", e)
            return f"⚠️ Cognee error: {e}"

        if not answer:
            return ("I don't have anything in memory for that yet. As you chat, "
                    "Cognee builds up a knowledge graph it can answer from.")
        return answer
