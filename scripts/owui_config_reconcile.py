#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1185 — JSON-aware reconcile of Open WebUI's persisted OpenAI connection
and RAG key family.

Open WebUI (0.11.x) stores its config as ONE ROW PER KEY in `config`
(`key TEXT PK, value JSON`) and reads the connection straight from the rows
`openai.api_base_urls` / `openai.api_keys` / `openai.api_configs`. The stack
used to touch those rows with an SQL text `replace()` over the serialised
JSON, which is how a list `[gpustack, manager]` became `[manager, manager]`
after the gpustack→manager repoint — the duplicate connection the operator
found on 0.91. `/api/v1/configs/import` with a NESTED `{"openai": {...}}`
payload is no better: on the per-key schema it writes a dead `openai` blob row
that OWUI never reads (the "legacy blob" from the same forensics).

This module does the reconcile on the parsed values instead:

* `upsert_connection` — ONE entry per base URL, never a duplicate; the target
  URL's key is set (or the URL appended when absent); every other backend the
  operator wired (mac gateway, …) is preserved verbatim, keys and configs
  re-indexed alongside.
* `reconcile_rows` — the whole key family (`openai.api_keys`,
  `rag.openai.api_key`, `rag.external_reranker_api_key`) written as ONE
  consistent set, the legacy `openai` blob mirrored when it exists (never
  created), and only rows whose value actually changes are emitted, so a
  second run is a no-op.
* `candidate_keys` — the keys the app currently holds for the target URL, so
  the caller can VALIDATE them against the manager before deciding which key
  wins (the .env key is not trusted blindly, see lib-owui.sh).

The pure functions carry no I/O. The CLI wraps them with a psql transport
(`docker exec -i postgres psql …`, overridable via RZFZ_OWUI_PSQL_CMD for the
test seam) and uses psql's `:'var'` client-side interpolation for every value
it writes — no SQL is ever assembled from a key or URL.

#2507 adds the `sso` subcommand (`sso_rows`): the persisted half of Open
WebUI's native-OIDC login. In 0.11.3 `ui.enable_login_form` is a DB row seeded
ONCE from env at first boot, so an env change never reaches an existing box —
the #2515 trap; the rows are written here instead.

#2519 adds the `chat-defaults` subcommand (`chat_default_rows`,
`chat_default_model_row`): default + pinned model = the box's chat default,
task-model thinking off, and the chat default's `model` row with three
builtin-tool groups (calendar, automations, notifications) off. `tasks` stays
on everywhere (operator decision 2026-09-28) and is never touched. The
`model` row goes through scripts/owui_model_rows.py's transport — that
module stays the one writer of the row shape.

Exit codes: 0 ok · 2 database/transport error · 3 legacy single-row schema
(pre-0.11 OWUI, nothing to reconcile until it migrates) OR, for
`chat-defaults`, NOOWNER (openwebui_db has no user row to own the chat
model's row yet; the config rows are written, the marker is not) · 4 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any

CONNECTION_KEYS = (
    "openai.enable",
    "openai.api_base_urls",
    "openai.api_keys",
    "openai.api_configs",
)
RAG_KEY_ROWS = ("rag.openai.api_key", "rag.external_reranker_api_key")
RAG_URL_ROWS = ("rag.openai.api_base_url", "rag.external_reranker_url")
#: Dead nested rows left behind by the old `/configs/import` payloads. Mirrored
#: when present so an operator reading `/configs/export` sees one truth; never
#: created.
LEGACY_BLOB_KEY = "openai"
ALL_KEYS = CONNECTION_KEYS + RAG_KEY_ROWS + RAG_URL_ROWS + (LEGACY_BLOB_KEY,)

#: The connection config OWUI's admin UI writes for a plain bearer-authed
#: OpenAI-compatible endpoint (the shape post-install always imported).
DEFAULT_API_CONFIG: dict[str, Any] = {
    "enable": True,
    "tags": [],
    "prefix_id": "",
    "model_ids": [],
    "connection_type": "external",
    "auth_type": "bearer",
}

PLACEHOLDER_KEY = "gpustack_CHANGEME_AFTER_FIRST_START"

#: #2507 — the rows behind Open WebUI's SSO-only login (0.11.3 config.py
#: DEFAULT_CONFIG / main.py get_app_config):
#:   ui.enable_login_form — PERSISTENT: the login page auto-redirects to the one
#:       OAuth provider only when this is false (and OAUTH_AUTO_REDIRECT, a plain
#:       env, is true);
#:   oauth.client_id / oauth.client_secret — read from env while
#:       ENABLE_OAUTH_PERSISTENT_CONFIG is off (the default, and this stack's
#:       setting); prod held them as "" rows left from an older release. Kept
#:       equal to the env so turning persistent OAuth config on can never
#:       silently switch the client to empty credentials.
SSO_KEYS = ("ui.enable_login_form", "oauth.client_id", "oauth.client_secret")

#: #2515 — the fleet-default embedding batch size + concurrent-requests rows
#: (0.11.3 config.py: `RAG_EMBEDDING_BATCH_SIZE`/`RAG_EMBEDDING_CONCURRENT_REQUESTS`
#: env vars feed `DEFAULT_CONFIG['rag.embedding_batch_size']` /
#: `DEFAULT_CONFIG['rag.embedding_concurrent_requests']`, seeded ONCE into the
#: per-key `config` table at first boot — same #2507 trap: prod held 1/1 with
#: no env able to move it). A third row, `rag.enable_async_embedding`
#: (`ENABLE_ASYNC_EMBEDDING`), is load-bearing alongside them:
#: retrieval/utils.py::get_embedding_function batches and parallelises ONLY
#: when it is true (`asyncio.gather` over a `Semaphore(concurrent_requests)`);
#: false — what every provisioned box has held since #976, never a deliberate
#: choice — walks the batches one at a time regardless of batch size or
#: concurrent-requests, so #2515 is a no-op without it.
#:
#: `owui_configure_embedding` (cli/post-install.sh) already writes all three
#: rows through OWUI's own admin API (`POST /api/v1/retrieval/embedding/update`,
#: which ends in `await config.save()`), on both the --preset and --refresh
#: paths — but that call needs OWUI answering and an admin session; this psql
#: write is the one that still lands when it is not (yet) reachable during a
#: --refresh, same reasoning as SSO_KEYS above.
EMBEDDING_KEYS = ("rag.embedding_batch_size", "rag.embedding_concurrent_requests",
                  "rag.enable_async_embedding")


#: #2519 — Open WebUI's chat defaults (operator decision 2026-09-26), all
#: PersistentConfig rows in 0.11.3 (config.py DEFAULT_CONFIG) seeded ONCE from
#: env at first boot, so env never reaches an existing box (the #2507/#2515
#: trap):
#:   ui.default_models / ui.default_pinned_models — `DEFAULT_MODELS` /
#:       `DEFAULT_PINNED_MODELS`, comma-separated STRINGS (routers/configs.py
#:       `DEFAULT_MODELS: str | None`; main.py splits on ','). Prod held
#:       `gemma4` / `gemma4,qwen3.5,magistral` — models the box does not serve.
#:   task.model.params — `TASK_MODEL_PARAMS` (0.11.1+), a params dict applied
#:       to every task call (title/tags/follow-ups/queries) by
#:       routers/tasks.py::apply_task_model_params → utils/payload.py::
#:       apply_params_to_form_data, which pops `custom_params`, JSON-parses each
#:       STRING value and deep-merges it into the request. Thinking on made the
#:       title task take 20.8 s for 1,000 tokens and an EMPTY title (#2519).
CHAT_DEFAULT_KEYS = ("ui.default_models", "ui.default_pinned_models", "task.model.params")

#: #2519 — the builtin-tool groups switched OFF on the chat default model. Keys
#: are Open WebUI 0.11.3's `meta.builtinTools` categories
#: (utils/tools.py::get_builtin_tools → `is_builtin_tool_enabled(category)`,
#: which reads `model['info']['meta']['builtinTools'][category]`, default True;
#: the model editor's BuiltinTools.svelte writes the same flat bool map).
#: calendar 1,193 + automations 1,153 + notifications 318 ≈ 2,660 prompt tokens
#: per chat (measured on prod). knowledge / notes / time / chats / user_input
#: stay as the operator has them — this module never writes those.
#:
#: Operator decision 2026-09-28: `tasks` (518 tokens) stays ON everywhere —
#: it is deliberately NOT in this tuple. This module never sets
#: `meta.builtinTools.tasks` at all, in either direction: an absent model row
#: is created with no `tasks` key (OWUI's own default is True), and a
#: `tasks` key an operator or an earlier box already wrote (on or off) is
#: left exactly as it is, on every run, marked or not. K1's apply-once
#: behaviour (the `rzfz.defaults.2519` marker) is unchanged by this — the
#: marker still gates default/pinned and the other three groups the same way
#: it always did; `tasks` was simply removed from the set it ever touches.
BUILTIN_TOOLS_OFF = ("calendar", "automations", "notifications")


def _positive_int(value: str) -> int:
    """argparse `type=` for --batch-size / --concurrent-requests (#2515 fix
    round 1). 0 or negative would write a value OWUI's embedding pipeline
    cannot act on (a batch size < 1 embeds nothing; a concurrency < 1 leaves
    no Semaphore slots for async_embedding_function to acquire) — refuse it
    here rather than write a row that quietly disables RAG."""
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


_KEY_RE = re.compile(r"^[a-z0-9_.]+$")


# ------------------------------------------------------------- pure logic

def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def upsert_connection(
    bases: Any,
    keys: Any,
    configs: Any,
    url: str,
    key: str,
    replace: dict[str, str] | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Return (bases, keys, configs) with exactly one entry per base URL and
    `url` carrying `key`.

    * `replace` maps old base URLs to new ones (the gpustack→manager
      transition) and is applied per entry BEFORE de-duplication, so the
      collapsed pair that produced #1185 folds into one entry.
    * First occurrence wins for the key/config of a duplicated URL, except
      that an empty key or missing config is filled from a later duplicate.
    * `configs` is index-keyed (`"0"`, `"1"`, …) exactly as OWUI keeps it and
      is re-indexed to the new positions; entries without a config stay
      without one (OWUI applies its defaults), the target gets
      DEFAULT_API_CONFIG when it is new.
    """
    replace = replace or {}
    bases = _as_list(bases)
    keys = _as_list(keys)
    configs = _as_dict(configs)
    keys = (keys + [""] * len(bases))[: len(bases)]

    order: list[str] = []
    seen: dict[str, tuple[str, Any]] = {}
    for i, raw in enumerate(bases):
        if not isinstance(raw, str):
            continue
        base = raw.strip()
        base = replace.get(base, base)
        if not base:
            continue
        k = keys[i] if isinstance(keys[i], str) else ""
        c = configs.get(str(i))
        if base in seen:
            prev_k, prev_c = seen[base]
            seen[base] = (prev_k or k, prev_c if isinstance(prev_c, dict) else c)
            continue
        seen[base] = (k, c)
        order.append(base)

    if url in seen:
        seen[url] = (key, seen[url][1])
    else:
        order.append(url)
        seen[url] = (key, dict(DEFAULT_API_CONFIG))

    new_bases = list(order)
    new_keys = [seen[b][0] for b in order]
    new_configs: dict[str, Any] = {}
    for i, b in enumerate(order):
        c = seen[b][1]
        if isinstance(c, dict):
            new_configs[str(i)] = c
    return new_bases, new_keys, new_configs


def candidate_keys(rows: dict[str, Any], url: str) -> list[str]:
    """Every distinct non-empty key the app holds that could be `url`'s key:
    the entries paired with `url` (granular rows and legacy blob) plus the two
    RAG key rows, which the stack always writes from the same family."""
    out: list[str] = []

    def add(k: Any) -> None:
        if isinstance(k, str) and k and k != PLACEHOLDER_KEY and k not in out:
            out.append(k)

    for source in (rows, _as_dict(rows.get(LEGACY_BLOB_KEY))):
        bases = _as_list(source.get("openai.api_base_urls" if source is rows else "api_base_urls"))
        keys = _as_list(source.get("openai.api_keys" if source is rows else "api_keys"))
        for i, b in enumerate(bases):
            if isinstance(b, str) and b.strip() == url and i < len(keys):
                add(keys[i])
    for k in RAG_KEY_ROWS:
        add(rows.get(k))
    return out


def connection_pair(rows: dict[str, Any], url: str) -> tuple[bool, str]:
    """`(url is listed, the key at its position)` from the GRANULAR rows only.

    Unlike `candidate_keys` this is a faithful read of what OWUI will actually
    use for `url` — no RAG fallbacks, no placeholder filtering — which is what
    the .env-vs-DB verify (#1252) has to compare against. A listed URL with no
    key yields `(True, "")`.
    """
    bases = _as_list(rows.get("openai.api_base_urls"))
    keys = _as_list(rows.get("openai.api_keys"))
    for i, b in enumerate(bases):
        if isinstance(b, str) and b.strip() == url:
            k = keys[i] if i < len(keys) else ""
            return True, k if isinstance(k, str) else ""
    return False, ""


def reconcile_rows(
    rows: dict[str, Any],
    url: str,
    key: str,
    rerank_url: str,
    replace_urls: dict[str, str] | None = None,
    replace_rerank: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compute the rows that must be written so the connection + RAG key
    family is consistent. Returns ONLY rows whose value differs from `rows`
    (a row absent from `rows` counts as different), so the caller writes
    nothing on an already-consistent config."""
    replace_urls = replace_urls or {}
    replace_rerank = replace_rerank or {}
    out: dict[str, Any] = {}

    def want(k: str, v: Any) -> None:
        if k not in rows or rows[k] != v:
            out[k] = v

    bases, keys, configs = upsert_connection(
        rows.get("openai.api_base_urls"),
        rows.get("openai.api_keys"),
        rows.get("openai.api_configs"),
        url, key, replace_urls,
    )
    want("openai.enable", True)
    want("openai.api_base_urls", bases)
    want("openai.api_keys", keys)
    want("openai.api_configs", configs)
    for k in RAG_KEY_ROWS:
        want(k, key)
    # The RAG endpoint rows are owned by the retrieval reconcile (post-install
    # / upgrade push them authoritatively); here they only follow a backend
    # transition, the same gpustack→manager move the connection entries make.
    for k, rmap in (("rag.openai.api_base_url", replace_urls),
                    ("rag.external_reranker_url", replace_rerank)):
        cur = rows.get(k)
        if isinstance(cur, str) and cur in rmap:
            want(k, rmap[cur])
    blob = rows.get(LEGACY_BLOB_KEY)
    if isinstance(blob, dict):
        mirrored = dict(blob)
        mirrored.update({
            "enable": True,
            "api_base_urls": bases,
            "api_keys": keys,
            "api_configs": configs,
        })
        want(LEGACY_BLOB_KEY, mirrored)
    return out


def sso_rows(
    rows: dict[str, Any],
    oidc_effective: bool,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """#2507 — the SSO login rows to write; only those whose value changes.

    `oidc_effective` is the caller's verdict that native OIDC is ON *and*
    working (switch true, client id + secret set, the Authentik provider
    answers its discovery URL). Only then is the login form hidden. Otherwise
    the form is (re)enabled: a box whose provider is missing or switched off
    must never be left with no way to log in — `/auth?form=1` is an escape
    hatch an operator has to know, not a login page.
    """
    out: dict[str, Any] = {}

    def want(k: str, v: Any) -> None:
        if k not in rows or rows[k] != v:
            out[k] = v

    if oidc_effective:
        want("ui.enable_login_form", False)
        want("oauth.client_id", client_id)
        want("oauth.client_secret", client_secret)
    else:
        want("ui.enable_login_form", True)
    return out


def embedding_rows(rows: dict[str, Any], batch_size: int, concurrent_requests: int) -> dict[str, Any]:
    """#2515 — the embedding batch-size/concurrent-requests rows to write,
    plus `rag.enable_async_embedding` (always wanted True — batching and
    concurrency are both inert while it is False); only rows whose value
    differs, so a box already at the fleet default (or hand-corrected to
    something higher) is left untouched on a re-run."""
    out: dict[str, Any] = {}

    def want(k: str, v: Any) -> None:
        if k not in rows or rows[k] != v:
            out[k] = v

    want("rag.embedding_batch_size", batch_size)
    want("rag.embedding_concurrent_requests", concurrent_requests)
    want("rag.enable_async_embedding", True)
    return out


def _json_obj(value: Any) -> dict | None:
    """A dict, or a JSON string holding one (the model editor's custom-param
    text box stores strings); None for anything else."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


#: #2519 fix round 1 (reviewer Minor 2) — Open WebUI 0.11.3 caps a title task
#: at the task model's `max_tokens` (default 1000) ONLY while
#: `task.model.params` is empty (routers/tasks.py:180 `task_model_params or
#: {'max_tokens': …}`). Any params we write switch that fallback off, so the
#: cap is written with them — or title/tags/follow-up calls run uncapped.
TASK_MAX_TOKENS = 1000

#: #2519 fix round 1 (controller ruling 1) — the "defaults applied once" marker:
#: a `config` row whose value is the stack VERSION that applied them. Chosen
#: over a table/file because Open WebUI 0.11.3 provably ignores it
#: (models/config.py): it reads config only by exact key (`get`/`get_many`
#: with its own DEFAULT_CONFIG names) or by its own namespaces
#: (`get_namespace`); `seed_defaults` only INSERTs missing known keys;
#: `repair_config_rows` touches only DICT_CONFIG_KEY_ALIASES prefixes and the
#: two default-model rows — nothing under `rzfz.`. `/configs/export` lists it
#: verbatim and `/configs/import` upserts it back (harmless, and correct: an
#: imported config carries its own "already applied" state). It lives WITH the
#: data it describes, so restoring an openwebui_db from before the first apply
#: also restores "not applied yet" — a host file would lie after that restore.
#: No DDL in a database whose migrations belong to Open WebUI.
MARKER_KEY = "rzfz.defaults.2519"


def task_model_params(current: Any) -> dict[str, Any]:
    """#2519 — `task.model.params` with `custom_params.chat_template_kwargs.
    enable_thinking` false, a `max_tokens` cap (TASK_MAX_TOKENS, unless the
    operator already set one) and EVERYTHING else the operator set kept.

    `chat_template_kwargs` is written as a JSON STRING when this module creates
    it — the form Open WebUI's own editor stores (a text input bound to
    `params.custom_params[key]`), which payload.py JSON-parses before merging;
    a dict would render as "[object Object]" in that editor and be saved back
    broken. An existing value keeps its form (string or dict) and its other
    keys. Already right → the input comes back unchanged (idempotent, no
    re-serialisation of an operator's string)."""
    params = dict(current) if isinstance(current, dict) else {}
    params.setdefault("max_tokens", TASK_MAX_TOKENS)
    custom = dict(params["custom_params"]) if isinstance(params.get("custom_params"), dict) else {}
    raw = custom.get("chat_template_kwargs")
    kwargs = _json_obj(raw)
    if kwargs is not None and kwargs.get("enable_thinking") is False:
        return params
    kwargs = kwargs or {}
    kwargs["enable_thinking"] = False
    custom["chat_template_kwargs"] = kwargs if isinstance(raw, dict) else \
        json.dumps(kwargs, separators=(",", ":"))
    params["custom_params"] = custom
    return params


def _model_list(value: Any) -> list[str]:
    """A default-models value as ids: the comma string OWUI 0.11.3 stores, or
    the legacy list its repair_config_rows still converts."""
    items = value if isinstance(value, list) else (value.split(",") if isinstance(value, str) else [])
    return [str(i).strip() for i in items if str(i).strip()]


def repaired_default(value: Any, chat_model: str, served: set[str] | None) -> str | None:
    """#2519 fix round 1 — the repair of ONE default-models row after the first
    apply; None = leave it (it is the admin's).

    * empty → the chat default;
    * the served set unknown (manager not asked / answered nothing) → leave it:
      "not served" cannot be told from "could not ask";
    * every id served → leave it (an admin's choice of a served model);
    * some ids unserved → drop those, keep the admin's served ones;
    * none served (a ga.3 box's gemma4 / qwen3.5 / magistral) → the chat default.
    """
    ids = _model_list(value)
    if not ids:
        return chat_model
    if not served:
        return None
    kept = [i for i in ids if i in served]
    if kept == ids:
        return None
    return ",".join(kept) if kept else chat_model


def chat_default_rows(
    rows: dict[str, Any],
    chat_model: str,
    served: set[str] | None = None,
) -> dict[str, Any]:
    """#2519 — the `config` rows to write; only those whose value changes.
    The marker row (MARKER_KEY) is not among them — the caller writes it once
    the model row is done too.

    FIRST apply (no marker in `rows` — a fresh box, or a ga.3 box upgrading):
    default AND pinned model = the box's chat default (the caller resolves it
    from the catalogue for this box's hardware — never a literal here), a
    ga.3 box's `gemma4` / `gemma4,qwen3.5,magistral` replaced; the task params
    merged (task_model_params).

    AFTER it (controller ruling 1, fix round 1): the admin owns these rows.
    Default/pinned are only REPAIRED (repaired_default: empty, or naming
    models the box does not serve); the task params are never touched again.

    No `function_calling` is written anywhere — the operator withdrew the
    legacy-function-calling default."""
    if not chat_model or "," in chat_model:
        raise ValueError(f"chat model must be one model id, got {chat_model!r}")
    out: dict[str, Any] = {}

    def want(k: str, v: Any) -> None:
        if k not in rows or rows[k] != v:
            out[k] = v

    if not rows.get(MARKER_KEY):
        want("ui.default_models", chat_model)
        want("ui.default_pinned_models", chat_model)
        want("task.model.params", task_model_params(rows.get("task.model.params")))
        return out
    for k in ("ui.default_models", "ui.default_pinned_models"):
        fixed = repaired_default(rows.get(k), chat_model, served)
        if fixed is not None:
            want(k, fixed)
    return out


def chat_default_model_row(
    existing: dict[str, Any] | None,
    chat_model: str,
    manifest_spec: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """#2519 — the chat default's `model` row with `meta.builtinTools`
    calendar/automations/notifications false; None when nothing changes.
    `tasks` is never asserted (operator decision 2026-09-28) — it stays
    exactly as it is, on or off, whatever an operator or an earlier box wrote.

    * PRESENT row: only those three keys are asserted. Every other meta key
      (capabilities, description, `tasks` and the other builtinTools groups,
      the #1305 `managed_by` stamp), `params` and `is_active` stay exactly as
      they are — the operator's per-model choices are not ours.
    * A user's custom model sharing the id (`base_model_id` set) is not the
      provider model's row and is left alone.
    * ABSENT row (fresh box, the model still downloading): created in the
      shape scripts/owui_model_rows.py writes for a chat model (same
      capabilities, manifest context budget) but WITHOUT its `managed_by`
      stamp — that writer deletes stamped rows the manager does not serve, and
      it stamps this row itself once the manager serves the model; stamping
      here would make the two writers pendulum on a box still pulling.
    """
    mr = _model_rows_module()
    if existing is not None:
        if (existing.get("base_model_id") or "") != "":
            return None
        meta = dict(existing["meta"]) if isinstance(existing.get("meta"), dict) else {}
        tools = dict(meta["builtinTools"]) if isinstance(meta.get("builtinTools"), dict) else {}
        if all(tools.get(k) is False for k in BUILTIN_TOOLS_OFF):
            return None
        for k in BUILTIN_TOOLS_OFF:
            tools[k] = False
        meta["builtinTools"] = tools
        return {"id": chat_model, "name": existing.get("name") or chat_model, "meta": meta,
                "params": existing.get("params") or {},
                "is_active": bool(existing.get("is_active")), "_new": False}
    spec = manifest_spec or {}
    meta = mr.merge_meta(None, mr.CHAT, "vision" in (spec.get("roles") or []))
    meta.pop(mr.MANAGED_BY_KEY, None)
    meta["builtinTools"] = {k: False for k in BUILTIN_TOOLS_OFF}
    return {"id": chat_model, "name": chat_model, "meta": meta,
            "params": mr.params_for(spec), "is_active": True, "_new": True}


def _model_rows_module():
    """scripts/owui_model_rows.py — the ONE writer of Open WebUI `model` rows
    (shape, owner, schema-aware upsert); reused, not re-implemented."""
    mod = sys.modules.get("owui_model_rows")
    if mod is None:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import owui_model_rows as mod  # noqa: PLC0415
    return mod


# ---------------------------------------------------------- psql transport

class SchemaError(RuntimeError):
    """The `config` table is not the per-key shape (pre-0.11 OWUI)."""


class DbError(RuntimeError):
    pass


def _psql_cmd(db: str, user: str) -> list[str]:
    override = os.environ.get("RZFZ_OWUI_PSQL_CMD", "").strip()
    if override:
        return shlex.split(override)
    return ["docker", "exec", "-i", "postgres", "psql", "-X",
            "-v", "ON_ERROR_STOP=1", "-tA", "-U", user, "-d", db]


def _run_sql(base: list[str], sql: str, params: dict[str, str] | None = None) -> str:
    cmd = list(base)
    for name, value in (params or {}).items():
        cmd += ["-v", f"{name}={value}"]
    try:
        proc = subprocess.run(cmd, input=sql, capture_output=True, text=True,
                              timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DbError(f"psql transport failed: {exc}") from exc
    if proc.returncode != 0:
        raise DbError((proc.stderr or proc.stdout).strip().splitlines()[0]
                      if (proc.stderr or proc.stdout).strip() else
                      f"psql exit {proc.returncode}")
    return proc.stdout


def value_column_type(base: list[str]) -> str:
    """'json' | 'jsonb' for the per-key schema; SchemaError otherwise."""
    out = _run_sql(base, "SELECT data_type FROM information_schema.columns "
                         "WHERE table_name='config' AND column_name='value';")
    kind = out.strip().lower()
    if kind in ("json", "jsonb"):
        return kind
    raise SchemaError("config.value is not a json column — legacy single-row schema")


def read_rows(base: list[str], keys: tuple[str, ...] = ALL_KEYS) -> dict[str, Any]:
    for k in keys:
        if not _KEY_RE.match(k):
            raise ValueError(f"refusing to inline config key {k!r}")
    in_list = ", ".join(f"'{k}'" for k in keys)
    out = _run_sql(base, "SELECT COALESCE(json_agg(json_build_object('key', key, "
                         f"'value', value)), '[]'::json)::text FROM config "
                         f"WHERE key IN ({in_list});")
    try:
        items = json.loads(out.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise DbError(f"unreadable config rows: {exc}") from exc
    return {it["key"]: it.get("value") for it in items
            if isinstance(it, dict) and isinstance(it.get("key"), str)}


def write_rows(base: list[str], rows: dict[str, Any], kind: str) -> None:
    """Write the whole key FAMILY in ONE transaction.

    rzfz review #1218 F3: one psql per row was one transaction per row — a
    failure after `openai.api_keys` and before `rag.openai.api_key` left exactly
    the half-written family this module exists to prevent. All statements go on
    a single stdin inside BEGIN/COMMIT; ON_ERROR_STOP aborts the lot cleanly."""
    if not rows:
        return
    sql, params = build_family_sql(rows, kind)
    _run_sql(list(base) + ["-v", "ON_ERROR_STOP=1"], sql, params)


def build_family_sql(rows: dict[str, Any], kind: str) -> tuple[str, dict[str, str]]:
    """Pure: the transactional statement block + its psql variables (testable)."""
    ts = str(int(time.time()))
    params: dict[str, str] = {"ts": ts}
    stmts = ["BEGIN;"]
    for i, (k, v) in enumerate(rows.items()):
        params[f"k{i}"] = k
        params[f"v{i}"] = json.dumps(v, separators=(",", ":"))
        stmts.append(
            "INSERT INTO config (key, value, updated_at) "
            f"VALUES (:'k{i}', :'v{i}'::{kind}, :'ts'::bigint) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = EXCLUDED.updated_at;")
    stmts.append("COMMIT;")
    return "\n".join(stmts), params


# -------------------------------------------------------------------- CLI

def _parse_pairs(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        old, sep, new = item.partition("=")
        if not sep or not old or not new:
            raise SystemExit(f"--replace expects OLD=NEW, got {item!r}")
        out[old] = new
    return out


def _stack_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _cmd_chat_defaults(base: list[str], chat_model: str, manifest: str,
                       served: set[str] | None, version: str) -> int:
    """#2519 — config rows first (one transaction); on the FIRST apply only,
    then the chat model's row (through scripts/owui_model_rows.py's upsert)
    and, once that row is handled, the MARKER_KEY row. Output: CHANGED=<n>,
    then one `ROW <key>` line per written row; `NOTE=` lines for a skip."""
    mr = _model_rows_module()
    yaml_error = getattr(getattr(mr, "yaml", None), "YAMLError", ValueError)
    written: list[str] = []
    try:
        kind = value_column_type(base)
        rows = read_rows(base, CHAT_DEFAULT_KEYS + (MARKER_KEY,))
        changes = chat_default_rows(rows, chat_model, served)
        write_rows(base, changes, kind)
        written += list(changes)
        if not rows.get(MARKER_KEY):
            types = mr.column_types(base)
            if "id" not in types or "meta" not in types:
                raise SchemaError("openwebui_db has no usable `model` table")
            existing = mr.read_existing(base).get(chat_model)
            spec = mr.load_manifest(manifest).get(chat_model)
            if existing is None and not spec:
                # Minor 3: a model the manifest does not know (an operator's
                # override naming a console deployment) gets no row from us —
                # core/llm/sync.py deletes unstamped rows that are not YAML
                # keys, and the two would create/delete on every run. Its row
                # comes from owui_model_rows.py once the manager serves it;
                # the next run (still unmarked) merges the tools into that.
                print(f"NOTE=model row for {chat_model} not created: not in the manifest "
                      "(it is written once the LLM Manager serves it); defaults stay unmarked")
            else:
                row = chat_default_model_row(existing, chat_model, spec)
                if row is not None:
                    uid = mr.owner_id(base) if row["_new"] else ""
                    mr.write_rows(base, {chat_model: row}, uid, types)
                    written.append(f"model:{chat_model}")
                write_rows(base, {MARKER_KEY: version}, kind)
                written.append(MARKER_KEY)
    except (SchemaError, mr.SchemaError) as exc:
        print(f"SCHEMA={exc}")
        return 3
    except mr.NoOwnerError as exc:
        print(f"NOOWNER={exc}")
        return 3
    except (DbError, mr.DbError, ValueError, yaml_error) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    print(f"CHANGED={len(written)}")
    for k in written:
        print(f"ROW {k}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    dbargs = argparse.ArgumentParser(add_help=False)
    dbargs.add_argument("--db", default=os.environ.get("OPENWEBUI_DB") or "openwebui_db")
    dbargs.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER") or "docker")
    common = argparse.ArgumentParser(add_help=False, parents=[dbargs])
    common.add_argument("--url", required=True, help="target OpenAI-compatible base URL")
    _sso_help = ("#2507: write the SSO login rows (client id/secret from "
                 "RZFZ_OWUI_OIDC_CLIENT_ID / RZFZ_OWUI_OIDC_CLIENT_SECRET, not this "
                 "helper's command line; like every write of this module they reach "
                 "psql as -v variables of the `docker exec postgres psql` call)")
    ap_sso = sub.add_parser("sso", parents=[dbargs], help=_sso_help, description=_sso_help)
    ap_sso.add_argument("--oidc-effective", required=True, choices=("true", "false"))

    _embedding_help = ("#2515: write the fleet-default embedding batch size + "
                       "concurrent-requests rows (rag.embedding_batch_size, "
                       "rag.embedding_concurrent_requests)")
    ap_embedding = sub.add_parser("embedding", parents=[dbargs],
                                  help=_embedding_help, description=_embedding_help)
    ap_embedding.add_argument("--batch-size", type=_positive_int, required=True)
    ap_embedding.add_argument("--concurrent-requests", type=_positive_int, required=True)

    _chat_help = ("#2519: write Open WebUI's chat defaults — ui.default_models / "
                  "ui.default_pinned_models = --chat-model, task.model.params with "
                  "thinking off, and the chat model's row with the calendar/automations/"
                  "notifications builtin tools off (tasks stays on, operator decision "
                  "2026-09-28; no legacy function calling)")
    ap_chat = sub.add_parser("chat-defaults", parents=[dbargs], help=_chat_help,
                             description=_chat_help)
    ap_chat.add_argument("--chat-model", required=True,
                         help="the box's chat default (resolved by the caller from the catalogue)")
    ap_chat.add_argument("--manifest", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "core", "llm", "standard-models.yaml"),
        help="standard-models.yaml — the context budget of a row created here")
    ap_chat.add_argument("--served", action="append", metavar="ID",
                         help="a model the box serves (repeatable). None given = unknown: "
                              "after the first apply, a default naming a served model is the "
                              "admin's and is left alone; with the set unknown nothing but an "
                              "EMPTY default is repaired")
    ap_chat.add_argument("--marker-version", default="",
                         help=f"value of the {MARKER_KEY} row (default: the repo's VERSION)")

    sub.add_parser("candidates", parents=[common],
                   help="print the keys the app holds for --url, one per line")
    sub.add_parser("pair", parents=[common],
                   help="print URL_PRESENT/KEY for --url (the .env-vs-DB verify)")
    ap_apply = sub.add_parser("apply", parents=[common],
                              help="write the connection + RAG key family")
    ap_apply.add_argument("--key", required=True)
    ap_apply.add_argument("--rerank-url", required=True)
    ap_apply.add_argument("--replace", action="append", metavar="OLD=NEW",
                          help="base-URL transition applied before dedupe")
    ap_apply.add_argument("--replace-rerank", action="append", metavar="OLD=NEW",
                          help="reranker-URL transition for rag.external_reranker_url")
    args = ap.parse_args(argv)

    base = _psql_cmd(args.db, args.pg_user)
    if args.cmd == "sso":
        try:
            kind = value_column_type(base)
            changes = sso_rows(read_rows(base, SSO_KEYS),
                               args.oidc_effective == "true",
                               os.environ.get("RZFZ_OWUI_OIDC_CLIENT_ID", ""),
                               os.environ.get("RZFZ_OWUI_OIDC_CLIENT_SECRET", ""))
            write_rows(base, changes, kind)
        except SchemaError as exc:
            print(f"SCHEMA={exc}")
            return 3
        except (DbError, ValueError) as exc:
            print(f"ERROR={exc}", file=sys.stderr)
            return 2
        print(f"CHANGED={len(changes)}")
        for k in changes:
            # names only — oauth.client_secret's value never reaches a log
            print(f"ROW {k}")
        return 0
    if args.cmd == "embedding":
        try:
            kind = value_column_type(base)
            changes = embedding_rows(read_rows(base, EMBEDDING_KEYS),
                                     args.batch_size, args.concurrent_requests)
            write_rows(base, changes, kind)
        except SchemaError as exc:
            print(f"SCHEMA={exc}")
            return 3
        except (DbError, ValueError) as exc:
            print(f"ERROR={exc}", file=sys.stderr)
            return 2
        print(f"CHANGED={len(changes)}")
        for k in changes:
            print(f"ROW {k}")
        return 0
    if args.cmd == "chat-defaults":
        served = {m.strip() for m in (args.served or []) if m.strip()} or None
        return _cmd_chat_defaults(base, args.chat_model.strip(), args.manifest, served,
                                  args.marker_version or _stack_version())
    try:
        kind = value_column_type(base)
        rows = read_rows(base)
        if args.cmd == "candidates":
            for k in candidate_keys(rows, args.url):
                print(k)
            return 0
        if args.cmd == "pair":
            present, key = connection_pair(rows, args.url)
            print(f"URL_PRESENT={1 if present else 0}")
            print(f"KEY={key}")
            return 0
        changes = reconcile_rows(rows, args.url, args.key, args.rerank_url,
                                 _parse_pairs(args.replace),
                                 _parse_pairs(args.replace_rerank))
        write_rows(base, changes, kind)
    except SchemaError as exc:
        print(f"SCHEMA={exc}")
        return 3
    except (DbError, ValueError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    print(f"CHANGED={len(changes)}")
    for k in changes:
        print(f"ROW {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
