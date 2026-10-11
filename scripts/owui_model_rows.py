#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1266 — write Open WebUI's `model` rows for the models the LLM Manager serves.

On a GPUStack box the `model-sync` container INSERTed one row per backend model
into `openwebui_db.model`, and `cli/post-install.sh::owui_configure_models` then
patched `meta` on those rows (hide the embedder/reranker, vision on the chat
models). A Manager box runs no `model-sync` — nothing ever wrote the rows — so
after a clean install the table held ZERO rows:

* `qwen3-embedding` / `qwen3-reranker` / `granite-docling` showed up in the
  user's chat model picker as if they were chat models (a row is what carries
  `meta.hidden` / `is_active`);
* the chat models carried no capabilities at all (no vision, no tools);
* `core/llm/sync.py` could not apply `params` (num_ctx / max_tokens) because
  there was no row to apply them to, and reported
  "ADD <name> (row missing — sync after OWUI auto-discovery)" — an event that
  never comes on a Manager box.

This module is the writer. The model SET comes from the MANAGER (its `/v1/models`
plus the per-deployment `task`), never from the manifest's name list, so a model
an operator deployed from the console gets a row too. The CLASSIFICATION prefers
`core/llm/standard-models.yaml` when it knows the alias, because the manifest is
the only place that distinguishes a document-conversion model from a chat one:
`granite-docling`'s manager task is `chat` (it is neither an embedder nor a
reranker), yet it must never appear in the chat picker.

Row shape mirrors what `model-sync` wrote (id, user_id, base_model_id=NULL,
name, meta, params, is_active, created_at, updated_at) so the two paths cannot
diverge; optional columns (`access_control`) are included only when the live
schema has them. `meta` is MERGED on an existing row — an operator's
description / profile image / extra capabilities survive — and `params` is
written on INSERT only, because `core/llm/sync.py` owns it from then on.

Idempotent: only rows whose value actually changes are written, so a second run
reports `CHANGED=0`.

Exit codes: 0 ok · 2 database/transport error · 3 no user row to own the models
(OWUI has not been seeded yet) · 4 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - the CLI reports it, the pure logic works
    yaml = None

#: Serve tasks the manager reports (`GET /api/deployments` → `task`).
MANAGER_TASKS = ("chat", "embed", "rerank")

#: Model CLASSES this module decides visibility by. `doc` is the manifest-only
#: class (document-conversion vision models — granite-docling): the manager
#: serves them as `chat` because they are neither an embedder nor a reranker,
#: but they are consumed by the docling extractor, never by a person in the
#: chat picker.
CHAT, EMBED, RERANK, DOC = "chat", "embed", "rerank", "doc"

#: manifest role → class. Roles not listed here (chat / coding / general /
#: vision …) are chat-class models.
ROLE_CLASS = {
    "embedding": EMBED,
    "reranker": RERANK,
    "vision-document-conversion": DOC,
}

#: Last-resort classification for a model neither the manifest nor the manager
#: can place — substring on the model id, the same shape the pre-#1266
#: hardcoded `update_model "qwen3-embedding" "true"` calls encoded.
NAME_HINTS = (("embed", EMBED), ("rerank", RERANK), ("docling", DOC))

#: The capability set `model-sync` wrote for every row, with the two values
#: post-install then corrected per model (`vision` from the manifest,
#: `image_generation` off — the stack serves no image model). Keeping the same
#: key set means a Manager box's picker offers exactly what a GPUStack box's
#: does.
CHAT_CAPABILITIES: dict[str, Any] = {
    "vision": False,
    "file_upload": True,
    "file_context": True,
    "web_search": True,
    "code_interpreter": True,
    "citations": True,
    "status_updates": True,
    "builtin_tools": True,
    "image_generation": False,
}

DEFAULT_PROFILE_IMAGE = "/static/favicon.png"


# ------------------------------------------------------------- pure logic

def classify(alias: str, roles: list[str] | None, manager_task: str = "") -> str:
    """Which class `alias` belongs to.

    Precedence — manifest, then the manager, then the name:

    * the MANIFEST wins where it knows the alias: it is the only source that
      separates a document-conversion model from a chat model (see DOC above);
    * the MANAGER's `task` places a model the manifest has never heard of (an
      operator's console deploy);
    * the NAME is the last resort, for a served model that has neither.
    """
    for role in roles or []:
        if role in ROLE_CLASS:
            return ROLE_CLASS[role]
    if roles:
        return CHAT
    task = (manager_task or "").strip().lower()
    if task in ("embed", "embedding"):
        return EMBED
    if task in ("rerank", "reranker"):
        return RERANK
    if task == "chat":
        return CHAT
    low = alias.lower()
    for hint, kind in NAME_HINTS:
        if hint in low:
            return kind
    return CHAT


#: #1305 — provenance stamp on every row this module writes. core/llm/sync.py
#: deletes rows whose id is not a standard-models.yaml key; a row the MANAGER
#: serves (a console-deployed model) is exactly such a row, and the two writers
#: used to pendulum (sync deletes, this re-creates, CHANGED never reaches 0).
#: One owner per row: stamped rows are this module's — it creates AND removes
#: them (`orphaned_rows`) — and sync.py leaves them alone.
MANAGED_BY_KEY = "managed_by"
MANAGED_BY = "llm-manager"


def merge_meta(current: Any, kind: str, vision: bool) -> dict[str, Any]:
    """The row's `meta`, with only the fields this module owns asserted.

    Everything else an admin or an earlier version put there is preserved —
    that is the whole reason this merges instead of replacing (an admin's
    description or profile image must survive a `--refresh`).
    """
    meta = dict(current) if isinstance(current, dict) else {}
    meta.setdefault("profile_image_url", DEFAULT_PROFILE_IMAGE)
    meta.setdefault("description", None)
    meta[MANAGED_BY_KEY] = MANAGED_BY  # #1305
    caps = dict(meta.get("capabilities")) if isinstance(meta.get("capabilities"), dict) else {}
    if kind == CHAT:
        merged = dict(CHAT_CAPABILITIES)
        merged["vision"] = bool(vision)
        # An operator's extra capability keys stay; the ones we own are asserted.
        caps.update(merged)
        meta["capabilities"] = caps
        meta["hidden"] = False
    else:
        # Non-chat models are hidden from the picker. Their capabilities are not
        # ours to invent — an embedder has none that mean anything here.
        meta["capabilities"] = caps
        meta["hidden"] = True
    return meta


def params_for(spec: dict[str, Any] | None) -> dict[str, Any]:
    """`params` for a NEW row: the context budget the manifest declares.

    Same two keys `core/llm/sync.py::sync_openwebui` reconciles, written at
    INSERT time because on a Manager box that reconcile runs BEFORE the rows
    exist (post-install step 4b vs step 5) and would otherwise never apply
    them.
    """
    out: dict[str, Any] = {}
    spec = spec or {}
    if spec.get("per_slot_context"):
        out["num_ctx"] = int(spec["per_slot_context"])
    if spec.get("max_completion_tokens"):
        out["max_tokens"] = int(spec["max_completion_tokens"])
    return out


def desired_rows(
    models: list[tuple[str, str]],
    manifest: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """(alias, manager-task) pairs → the row each model must have.

    `manifest` is `standard-models.yaml`'s `models` mapping (may be empty: the
    manager's task then decides).
    """
    manifest = manifest or {}
    out: dict[str, dict[str, Any]] = {}
    for alias, task in models:
        alias = (alias or "").strip()
        if not alias or alias in out:
            continue
        spec = manifest.get(alias) or {}
        roles = list(spec.get("roles") or []) if spec else []
        kind = classify(alias, roles, task)
        out[alias] = {
            "id": alias,
            "name": alias,
            "kind": kind,
            "vision": "vision" in roles,
            "params": params_for(spec),
            "is_active": kind == CHAT,
        }
    return out


def changed_rows(
    existing: dict[str, dict[str, Any]],
    desired: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """The rows that must be written, keyed by id.

    Each value carries the FULL row to upsert plus `_new` (True when the row
    does not exist yet — the only case in which `params` is written).
    """
    out: dict[str, dict[str, Any]] = {}
    for alias, want in desired.items():
        cur = existing.get(alias)
        meta = merge_meta((cur or {}).get("meta"), want["kind"], want["vision"])
        active = bool(want["is_active"])
        if cur is None:
            out[alias] = {**want, "meta": meta, "_new": True}
            continue
        if (cur.get("base_model_id") or "") != "":
            # A user-created custom model that happens to share the alias — it
            # is not the backend model row, and it is not ours to rewrite.
            # Same carve-out core/llm/sync.py makes before deleting a row.
            continue
        if meta == (cur.get("meta") if isinstance(cur.get("meta"), dict) else None) \
                and bool(cur.get("is_active")) == active:
            continue
        out[alias] = {**want, "meta": meta, "params": cur.get("params") or {},
                      "name": cur.get("name") or want["name"], "_new": False}
    return out


def is_managed_row(row: dict[str, Any] | None) -> bool:
    """A row this module wrote (stamped) and that is not a user's custom model."""
    if not row or (row.get("base_model_id") or "") != "":
        return False
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    return meta.get(MANAGED_BY_KEY) == MANAGED_BY


def orphaned_rows(
    existing: dict[str, dict[str, Any]],
    desired: dict[str, dict[str, Any]],
) -> list[str]:
    """Rows this module owns that the manager no longer serves — a model
    undeployed from the console leaves its row behind otherwise, dead in the
    picker. Unstamped rows (model-sync's on a GPUStack box, anything older)
    are not ours to remove; sync.py's YAML-based DELETE still covers them."""
    return sorted(alias for alias, row in existing.items()
                  if alias not in desired and is_managed_row(row))


def findings(
    existing: dict[str, dict[str, Any]],
    desired: dict[str, dict[str, Any]],
) -> list[str]:
    """The `--verify` verdict: one line per defect, empty when the box is right.

    Two failure modes, reported apart because they have different causes:

    * `MISSING <id>` — a chat model the manager serves has no usable row, so it
      carries no capabilities and the day-1 model-count checks stay red;
    * `VISIBLE <id>` — an embedder / reranker / doc-conversion model IS offered
      in the chat picker, which is what a user sees as "why are there three
      models that cannot talk".
    """
    out: list[str] = []
    for alias, want in sorted(desired.items()):
        cur = existing.get(alias)
        if want["kind"] == CHAT:
            if cur is None:
                out.append(f"MISSING {alias}")
            elif not cur.get("is_active"):
                out.append(f"INACTIVE {alias}")
            continue
        if cur is None:
            out.append(f"VISIBLE {alias}")
            continue
        meta = cur.get("meta") if isinstance(cur.get("meta"), dict) else {}
        if cur.get("is_active") and not meta.get("hidden"):
            out.append(f"VISIBLE {alias}")
    for alias in orphaned_rows(existing, desired):
        # #1305: our row for a model the manager no longer serves.
        out.append(f"ORPHAN {alias}")
    return out


# ---------------------------------------------------------- psql transport

class SchemaError(RuntimeError):
    """The `model` table is not shaped the way any supported OWUI shapes it."""


class NoOwnerError(RuntimeError):
    """No `user` row to own the models (OWUI has not been seeded yet)."""


class DbError(RuntimeError):
    pass


#: Columns this module writes, in order. `access_control` is appended when the
#: live schema has it (OWUI ≤0.8.7; removed in 0.8.8 — see model-sync's note).
BASE_COLUMNS = ("id", "user_id", "base_model_id", "name", "meta", "params",
                "is_active", "created_at", "updated_at")


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
                              timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DbError(f"psql transport failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise DbError(detail.splitlines()[0] if detail else f"psql exit {proc.returncode}")
    return proc.stdout


def column_types(base: list[str]) -> dict[str, str]:
    """`{column: data_type}` for `model`. Empty means the table is absent."""
    out = _run_sql(base, "SELECT column_name || '=' || data_type FROM "
                         "information_schema.columns WHERE table_name='model';")
    types: dict[str, str] = {}
    for line in out.splitlines():
        name, _, kind = line.strip().partition("=")
        if name:
            types[name] = kind.strip().lower()
    return types


def owner_id(base: list[str]) -> str:
    """The user the rows belong to: an admin first, else the oldest account.

    `model-sync` took "the oldest user"; an admin is the better owner because
    the Workspace → Models editor is admin-gated, and on an SSO box the oldest
    account can be a plain user.
    """
    out = _run_sql(base, 'SELECT id FROM "user" WHERE role = \'admin\' '
                         'ORDER BY created_at ASC LIMIT 1;')
    uid = out.strip().splitlines()[0].strip() if out.strip() else ""
    if uid:
        return uid
    out = _run_sql(base, 'SELECT id FROM "user" ORDER BY created_at ASC LIMIT 1;')
    uid = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not uid:
        raise NoOwnerError("openwebui_db has no user row to own the models")
    return uid


def _json_value(raw: Any) -> Any:
    """A `meta` / `params` column value as python.

    OWUI's `JSONField` is a TypeDecorator over TEXT in some versions and a real
    json column in others, so the read can hand back either a parsed object or
    the serialised string.
    """
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def read_existing(base: list[str]) -> dict[str, dict[str, Any]]:
    out = _run_sql(base, "SELECT COALESCE(json_agg(json_build_object("
                         "'id', id, 'base_model_id', base_model_id, 'name', name, "
                         "'meta', meta, 'params', params, 'is_active', is_active)), "
                         "'[]'::json)::text FROM model;")
    try:
        items = json.loads(out.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise DbError(f"unreadable model rows: {exc}") from exc
    rows: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        rows[item["id"]] = {
            "id": item["id"],
            "base_model_id": item.get("base_model_id") or "",
            "name": item.get("name") or "",
            "meta": _json_value(item.get("meta")),
            "params": _json_value(item.get("params")),
            "is_active": bool(item.get("is_active")),
        }
    return rows


def _cast(kind: str) -> str:
    """The cast a json payload needs for a column of `kind` (json/jsonb/text)."""
    return f"::{kind}" if kind in ("json", "jsonb") else ""


def _ts_expr(var: str, kind: str) -> str:
    if kind.startswith("timestamp") or kind.startswith("date"):
        return f"to_timestamp(:'{var}'::bigint)"
    return f":'{var}'::bigint"


def build_rows_sql(
    rows: dict[str, dict[str, Any]],
    uid: str,
    types: dict[str, str],
) -> tuple[str, dict[str, str]]:
    """Pure: the transactional upsert block + its psql variables (testable).

    One transaction for the whole set — a half-written model list is the state
    #1266 exists to end, and `ON_ERROR_STOP` aborts the lot cleanly. Every value
    travels as a psql `:'var'`; no SQL is assembled from an id, a name or a JSON
    payload.
    """
    ts = str(int(time.time()))
    params: dict[str, str] = {"ts": ts, "uid": uid}
    meta_cast = _cast(types.get("meta", "text"))
    params_cast = _cast(types.get("params", "text"))
    created = _ts_expr("ts", types.get("created_at", "bigint"))
    updated = _ts_expr("ts", types.get("updated_at", "bigint"))
    has_ac = "access_control" in types
    cols = list(BASE_COLUMNS) + (["access_control"] if has_ac else [])
    stmts = ["BEGIN;"]
    for i, (alias, row) in enumerate(rows.items()):
        params[f"i{i}"] = alias
        params[f"n{i}"] = str(row.get("name") or alias)
        params[f"m{i}"] = json.dumps(row.get("meta") or {}, separators=(",", ":"))
        params[f"p{i}"] = json.dumps(row.get("params") or {}, separators=(",", ":"))
        params[f"a{i}"] = "true" if row.get("is_active") else "false"
        values = [f":'i{i}'", ":'uid'", "NULL", f":'n{i}'",
                  f":'m{i}'{meta_cast}", f":'p{i}'{params_cast}",
                  f":'a{i}'::boolean", created, updated]
        if has_ac:
            # NULL = public, the value OWUI's own "create model" path writes.
            values.append("NULL")
        stmts.append(
            f"INSERT INTO model ({', '.join(cols)}) "
            f"VALUES ({', '.join(values)}) "
            "ON CONFLICT (id) DO UPDATE SET "
            f"meta = EXCLUDED.meta, is_active = EXCLUDED.is_active, "
            f"updated_at = EXCLUDED.updated_at;")
    stmts.append("COMMIT;")
    return "\n".join(stmts), params


def write_rows(base: list[str], rows: dict[str, dict[str, Any]], uid: str,
               types: dict[str, str]) -> None:
    if not rows:
        return
    sql, params = build_rows_sql(rows, uid, types)
    _run_sql(list(base) + ["-v", "ON_ERROR_STOP=1"], sql, params)


# -------------------------------------------------------------------- CLI

def build_delete_sql(ids: list[str]) -> tuple[str, dict[str, str]]:
    """One transaction; ids bound as psql variables, never interpolated."""
    params = {f"d{i}": alias for i, alias in enumerate(ids)}
    stmts = ["BEGIN;"] + [f"DELETE FROM model WHERE id = :'d{i}' AND base_model_id IS NULL;"
                          for i in range(len(ids))] + ["COMMIT;"]
    return "\n".join(stmts), params


def delete_rows(base: list[str], ids: list[str]) -> None:
    if not ids:
        return
    sql, params = build_delete_sql(ids)
    _run_sql(list(base) + ["-v", "ON_ERROR_STOP=1"], sql, params)


def load_manifest(path: str) -> dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    if yaml is None:
        print("WARN=pyyaml not installed — classifying from the manager's task only",
              file=sys.stderr)
        return {}
    with open(path, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh) or {}
    models = spec.get("models")
    return models if isinstance(models, dict) else {}


def parse_models(items: list[str] | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in items or []:
        alias, _, task = item.partition("=")
        alias = alias.strip()
        if not alias:
            raise SystemExit(f"--model expects ID[=TASK], got {item!r}")
        out.append((alias, task.strip()))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", action="append", metavar="ID[=TASK]",
                        help="a model the backend serves and the task it serves it as")
    common.add_argument("--manifest", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "core", "llm", "standard-models.yaml"))
    common.add_argument("--db", default=os.environ.get("OPENWEBUI_DB") or "openwebui_db")
    common.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER") or "docker")
    sub.add_parser("apply", parents=[common], help="upsert the rows")
    sub.add_parser("verify", parents=[common],
                   help="report rows that are missing or wrongly visible")
    args = ap.parse_args(argv)

    models = parse_models(args.model)
    if not models:
        print("ERROR=no --model given", file=sys.stderr)
        return 4
    desired = desired_rows(models, load_manifest(args.manifest))
    base = _psql_cmd(args.db, args.pg_user)
    try:
        types = column_types(base)
        if "id" not in types or "meta" not in types:
            raise SchemaError("openwebui_db has no usable `model` table")
        existing = read_existing(base)
        if args.cmd == "verify":
            found = findings(existing, desired)
            print(f"CHECKED={len(desired)} FINDINGS={len(found)}")
            for line in found:
                print(line)
            return 0
        changes = changed_rows(existing, desired)
        write_rows(base, changes, owner_id(base), types)
        orphans = orphaned_rows(existing, desired)
        delete_rows(base, orphans)
    except SchemaError as exc:
        print(f"SCHEMA={exc}")
        return 3
    except NoOwnerError as exc:
        print(f"NOOWNER={exc}")
        return 3
    except (DbError, ValueError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    print(f"CHANGED={len(changes) + len(orphans)}")
    for alias, row in changes.items():
        print(f"ROW {alias} {row['kind']} "
              f"{'active' if row['is_active'] else 'hidden'} "
              f"{'new' if row.get('_new') else 'updated'}")
    for alias in orphans:
        print(f"ROW {alias} - - removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
