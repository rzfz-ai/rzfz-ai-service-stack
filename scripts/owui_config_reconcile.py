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

Exit codes: 0 ok · 2 database/transport error · 3 legacy single-row schema
(pre-0.11 OWUI, nothing to reconcile until it migrates) · 4 usage.
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=os.environ.get("OPENWEBUI_DB") or "openwebui_db")
    common.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER") or "docker")
    common.add_argument("--url", required=True, help="target OpenAI-compatible base URL")

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
