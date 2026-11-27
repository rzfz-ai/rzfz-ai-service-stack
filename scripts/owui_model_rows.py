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
diverge. `meta` is MERGED on an existing row — an operator's
description / profile image / extra capabilities survive — and `params` is
written on INSERT only, because `core/llm/sync.py` owns it from then on.

#2673 — VISIBILITY. Open WebUI 0.11 dropped the `access_control` column (NULL
meant public) for the `access_grant` table, and a model row with no grant is
private: only admins see it. So on a schema that has `access_grant`, a chat row
gets ONE public read grant (`user` / `*` / `read`), exactly once in its life —
see `access_plan` for the rule that keeps an admin's narrowing intact. Helper
rows (embedder, reranker, document conversion) never get one.

Idempotent: only rows whose value actually changes are written, so a second run
reports `CHANGED=0`.

Exit codes: 0 ok · 2 database/transport error · 3 no user row to own the models
(OWUI has not been seeded yet) · 4 usage.
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
import uuid
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


#: #2673 — Open WebUI 0.11.3 `models/access_grants.py:25-45`: table
#: `access_grant` (id, resource_type, resource_id, principal_type,
#: principal_id, permission, created_at), unique on the last five but
#: created_at. Public read is `principal_type='user'`, `principal_id='*'`,
#: `permission='read'` (access_grants.py:14-17, and what the 0.11 migration
#: f1e2d3c4b5a6 wrote for a NULL `access_control`). Models are
#: `resource_type='model'` (utils/models.py:514-516).
GRANT_TABLE = "access_grant"
MODEL_RESOURCE = "model"
PUBLIC_READ = {"principal_type": "user", "principal_id": "*", "permission": "read"}

#: #2673 — the stamp that says "the platform has made this row's one access
#: decision". Written in the same transaction as the grant (or alone, when the
#: row already had grants). From then on the row's grants are the admin's.
ACCESS_SEEDED_KEY = "access_seeded_by"


def access_plan(meta: Any, row_grants: list[dict[str, Any]] | None,
                grant_table: bool) -> tuple[bool, bool]:
    """`(stamp, grant)` for a platform-managed CHAT row.

    The rule ("platform-managed and untouched"):

    * no `access_grant` table (Open WebUI before 0.11) → nothing to do;
    * the row already carries the ACCESS_SEEDED_KEY stamp → the platform
      decided once; whatever grants it has now (none included: an admin who
      removed them made the model private on purpose) are left alone;
    * no stamp and NO grant at all → never decided by anyone: grant public
      read and stamp, in one transaction;
    * no stamp but SOME grant (an admin's group/user grant, or the public
      grant OWUI's own 0.11 migration wrote for a pre-0.11 row) → somebody
      decided; stamp only, never add or change a grant.
    """
    if not grant_table:
        return False, False
    m = meta if isinstance(meta, dict) else {}
    if m.get(ACCESS_SEEDED_KEY) == MANAGED_BY:
        return False, False
    return True, not row_grants


def has_read_grant(row_grants: list[dict[str, Any]] | None) -> bool:
    """Any read grant — public, a user's or a group's. A narrowed row is still
    readable by someone; only a row with none is invisible to every user."""
    return any(isinstance(g, dict) and g.get("permission") == "read"
               for g in row_grants or [])


#: #2954 — the stamp on a row whose model the manager does not serve right now
#: (its worker is stale, restarting or being upgraded). The row is HIDDEN
#: (is_active=false), never deleted on sight: deleting it lost the admin's
#: edits, and a recreate made a model the admin had made private public again.
#: The value is the epoch second the model was first seen unserved.
UNSERVED_KEY = "unserved_since"

#: #2954 — how long an unserved row of a model that still has a deployment is
#: kept hidden before it is deleted. A model with no deployment at all
#: (undeployed from the console) is deleted at once, as before.
DEFAULT_ORPHAN_GRACE_HOURS = 168


def merge_meta(current: Any, kind: str, vision: bool) -> dict[str, Any]:
    """The row's `meta`, with only the fields this module owns asserted.

    Everything else an admin or an earlier version put there is preserved —
    that is the whole reason this merges instead of replacing (an admin's
    description or profile image must survive a `--refresh`).
    """
    meta = dict(current) if isinstance(current, dict) else {}
    meta.pop(UNSERVED_KEY, None)   # #2954: served again — visible again
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
    grants: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """The rows that must be written, keyed by id.

    Each value carries the FULL row to upsert plus `_new` (True when the row
    does not exist yet — the only case in which `params` is written) and
    `_grant` (#2673: True when the public read grant is written with it).

    `grants` is the live `access_grant` rows per model id; None means the
    schema has no such table (Open WebUI before 0.11).
    """
    out: dict[str, dict[str, Any]] = {}
    grant_table = grants is not None
    for alias, want in desired.items():
        cur = existing.get(alias)
        if cur is not None and (cur.get("base_model_id") or "") != "":
            # A user-created custom model that happens to share the alias — it
            # is not the backend model row, and it is not ours to rewrite.
            # Same carve-out core/llm/sync.py makes before deleting a row.
            continue
        meta = merge_meta((cur or {}).get("meta"), want["kind"], want["vision"])
        grant = False
        if want["kind"] == CHAT:
            stamp, grant = access_plan((cur or {}).get("meta"), (grants or {}).get(alias),
                                       grant_table)
            if stamp:
                meta[ACCESS_SEEDED_KEY] = MANAGED_BY
        active = bool(want["is_active"])
        if cur is None:
            out[alias] = {**want, "meta": meta, "_new": True, "_grant": grant}
            continue
        if not grant and meta == (cur.get("meta") if isinstance(cur.get("meta"), dict) else None) \
                and bool(cur.get("is_active")) == active:
            continue   # a grant is written even when the row itself is unchanged
        out[alias] = {**want, "meta": meta, "params": cur.get("params") or {},
                      "name": cur.get("name") or want["name"], "_new": False,
                      "_grant": grant}
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


def _unserved_since(row: dict[str, Any]) -> int | None:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    try:
        since = int(meta.get(UNSERVED_KEY))
    except (TypeError, ValueError):
        return None
    return since if since > 0 else None


def unserved_plan(
    existing: dict[str, dict[str, Any]],
    desired: dict[str, dict[str, Any]],
    deployed: set[str] | None,
    now: int,
    grace_s: int,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """#2954 — what happens to our rows the manager does not serve right now.

    `(hide, delete)`:

    * the model has no deployment any more (`deployed` is known and lacks it)
      → delete: it was undeployed, its row is dead in the picker (#1305);
    * it still has a deployment, or the deployments could not be read
      (`deployed` is None) → its worker is stale, restarting or being upgraded:
      HIDE the row (is_active=false, stamped UNSERVED_KEY), keeping its meta,
      params and access grants for when the model comes back;
    * hidden for longer than `grace_s` → delete.

    A row that is already hidden and stamped is left as it is (idempotent).
    """
    hide: dict[str, dict[str, Any]] = {}
    delete: list[str] = []
    for alias in orphaned_rows(existing, desired):
        row = existing[alias]
        since = _unserved_since(row)
        if deployed is not None and alias not in deployed:
            delete.append(alias)
            continue
        if since is not None and now - since >= grace_s:
            delete.append(alias)
            continue
        if since is not None and not row.get("is_active"):
            continue
        meta = dict(row["meta"])
        meta[UNSERVED_KEY] = since or int(now)
        hide[alias] = {"id": alias, "name": row.get("name") or alias, "kind": "-",
                       "meta": meta, "params": row.get("params") or {},
                       "is_active": False, "_new": False, "_grant": False,
                       "_unserved": True}
    return hide, delete


def findings(
    existing: dict[str, dict[str, Any]],
    desired: dict[str, dict[str, Any]],
    grants: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    """The `--verify` verdict: one line per defect, empty when the box is right.

    Two failure modes, reported apart because they have different causes:

    * `MISSING <id>` — a chat model the manager serves has no usable row, so it
      carries no capabilities and the day-1 model-count checks stay red;
    * `VISIBLE <id>` — an embedder / reranker / doc-conversion model IS offered
      in the chat picker, which is what a user sees as "why are there three
      models that cannot talk";
    * `PRIVATE <id>` (#2673, Open WebUI 0.11+) — a chat model's row carries no
      read grant, so only admins see it.
    """
    out: list[str] = []
    for alias, want in sorted(desired.items()):
        cur = existing.get(alias)
        if want["kind"] == CHAT:
            if cur is None:
                out.append(f"MISSING {alias}")
            elif not cur.get("is_active"):
                out.append(f"INACTIVE {alias}")
            elif grants is not None and not has_read_grant(grants.get(alias)):
                out.append(f"PRIVATE {alias}")
            continue
        if cur is None:
            out.append(f"VISIBLE {alias}")
            continue
        meta = cur.get("meta") if isinstance(cur.get("meta"), dict) else {}
        if cur.get("is_active") and not meta.get("hidden"):
            out.append(f"VISIBLE {alias}")
    for alias in orphaned_rows(existing, desired):
        # #1305: our row for a model the manager no longer serves. #2954: a row
        # already hidden for its grace period is the intended state, not a defect.
        row = existing[alias]
        if _unserved_since(row) is not None and not row.get("is_active"):
            continue
        out.append(f"ORPHAN {alias}")
    return out


# ---------------------------------------------------------- psql transport

class SchemaError(RuntimeError):
    """The `model` table is not shaped the way any supported OWUI shapes it."""


class NoOwnerError(RuntimeError):
    """No `user` row to own the models (OWUI has not been seeded yet)."""


class DbError(RuntimeError):
    pass


#: Columns this module writes, in order. Visibility is not a column on Open
#: WebUI 0.11 (#2673): it is the `access_grant` row `build_rows_sql` writes.
BASE_COLUMNS = ("id", "user_id", "base_model_id", "name", "meta", "params",
                "is_active", "created_at", "updated_at")


def _psql_cmd(db: str, user: str) -> list[str]:
    override = os.environ.get("RZFZ_OWUI_PSQL_CMD", "").strip()
    if override:
        return shlex.split(override)
    return ["docker", "exec", "-i", "postgres", "psql", "-X",
            "-v", "ON_ERROR_STOP=1", "-tA", "-U", user, "-d", db]


def _psql_set_lines(params: dict[str, str] | None) -> str:
    r"""#2536 item 11 (#1185 transport): the values reach psql on STDIN as
    `\set` meta-commands, never as `-v name=value` argv — argv is world-readable
    in `ps` for the lifetime of the call, and these values include the OpenAI
    key and the OIDC client secret. psql's single-quoted meta-command argument:
    `''` is a quote; `\\`, `\n`, `\r`, `\t` are processed (psql docs,
    "Meta-Commands"). Twin: scripts/owui_config_reconcile.py::_psql_set_lines."""
    lines = []
    for name, value in (params or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"refusing psql variable name {name!r}")
        v = (str(value).replace("\\", "\\\\").replace("'", "''")
             .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        lines.append(f"\\set {name} '{v}'")
    return "".join(line + "\n" for line in lines)


def _run_sql(base: list[str], sql: str, params: dict[str, str] | None = None) -> str:
    try:
        proc = subprocess.run(list(base), input=_psql_set_lines(params) + sql,
                              capture_output=True, text=True, timeout=120, check=False)
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


def grant_table_exists(base: list[str]) -> bool:
    """#2673: does the schema have Open WebUI 0.11's `access_grant` table?"""
    out = _run_sql(base, "SELECT count(*) FROM information_schema.tables "
                         f"WHERE table_name='{GRANT_TABLE}' "
                         "AND table_schema = current_schema();")
    return out.strip().splitlines()[:1] not in ([], ["0"])


def read_model_grants(base: list[str]) -> dict[str, list[dict[str, Any]]]:
    """#2673: `{model id: [grant, ...]}` from `access_grant` (models only)."""
    out = _run_sql(base, "SELECT COALESCE(json_agg(json_build_object("
                         "'resource_id', resource_id, 'principal_type', principal_type, "
                         "'principal_id', principal_id, 'permission', permission)), "
                         f"'[]'::json)::text FROM {GRANT_TABLE} "
                         f"WHERE resource_type = '{MODEL_RESOURCE}';")
    try:
        items = json.loads(out.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise DbError(f"unreadable access grants: {exc}") from exc
    grants: dict[str, list[dict[str, Any]]] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("resource_id"), str):
            grants.setdefault(item["resource_id"], []).append(
                {k: item.get(k) for k in ("principal_type", "principal_id", "permission")})
    return grants


def read_grants_if_any(base: list[str]) -> dict[str, list[dict[str, Any]]] | None:
    """The model grants, or None on a schema without `access_grant`."""
    return read_model_grants(base) if grant_table_exists(base) else None


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

    #2673: a row flagged `_grant` also gets the public read grant, in the same
    transaction as the meta stamp that records it, and only while the row has
    no grant at all (the NOT EXISTS re-checks what `access_plan` decided, so a
    grant an admin adds in between is never doubled or overridden).
    """
    ts = str(int(time.time()))
    params: dict[str, str] = {"ts": ts, "uid": uid}
    meta_cast = _cast(types.get("meta", "text"))
    params_cast = _cast(types.get("params", "text"))
    created = _ts_expr("ts", types.get("created_at", "bigint"))
    updated = _ts_expr("ts", types.get("updated_at", "bigint"))
    cols = list(BASE_COLUMNS)
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
        stmts.append(
            f"INSERT INTO model ({', '.join(cols)}) "
            f"VALUES ({', '.join(values)}) "
            "ON CONFLICT (id) DO UPDATE SET "
            f"meta = EXCLUDED.meta, is_active = EXCLUDED.is_active, "
            f"updated_at = EXCLUDED.updated_at;")
        if row.get("_grant"):
            params[f"g{i}"] = str(uuid.uuid4())   # OWUI's own id shape
            stmts.append(
                f"INSERT INTO {GRANT_TABLE} (id, resource_type, resource_id, "
                "principal_type, principal_id, permission, created_at) "
                f"SELECT :'g{i}', '{MODEL_RESOURCE}', :'i{i}', "
                f"'{PUBLIC_READ['principal_type']}', '{PUBLIC_READ['principal_id']}', "
                f"'{PUBLIC_READ['permission']}', :'ts'::bigint "
                f"WHERE NOT EXISTS (SELECT 1 FROM {GRANT_TABLE} "
                f"WHERE resource_type = '{MODEL_RESOURCE}' AND resource_id = :'i{i}');")
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
    common.add_argument("--deployed", action="append", metavar="ID",
                        help="a model the manager has a deployment for, served or not "
                             "(without any, an unserved row is only hidden, never deleted early)")
    common.add_argument("--orphan-grace-hours", type=float, default=DEFAULT_ORPHAN_GRACE_HOURS,
                        help="how long an unserved row of a still deployed model stays hidden "
                             "before it is deleted")
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
        grants = read_grants_if_any(base)   # #2673
        if args.cmd == "verify":
            found = findings(existing, desired, grants)
            print(f"CHECKED={len(desired)} FINDINGS={len(found)}")
            for line in found:
                print(line)
            return 0
        changes = changed_rows(existing, desired, grants)
        deployed = ({d.strip() for d in args.deployed if d.strip()}
                    if args.deployed else None)
        hidden, orphans = unserved_plan(existing, desired, deployed, int(time.time()),
                                        int(args.orphan_grace_hours * 3600))
        write_rows(base, {**changes, **hidden}, owner_id(base), types)
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
    print(f"CHANGED={len(changes) + len(hidden) + len(orphans)}")
    for alias, row in changes.items():
        print(f"ROW {alias} {row['kind']} "
              f"{'active' if row['is_active'] else 'hidden'} "
              f"{'new' if row.get('_new') else 'updated'}"
              f"{' +public-read' if row.get('_grant') else ''}")
    for alias in hidden:
        print(f"ROW {alias} - hidden unserved")
    for alias in orphans:
        print(f"ROW {alias} - - removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
