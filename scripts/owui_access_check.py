#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2673 / #2672 — can a non-admin Open WebUI user see what the platform serves?

Open WebUI 0.11 treats a resource without a read grant as private: only admins
see it. Two things the platform writes are affected, and an admin never
notices, because admins bypass the check:

* the chat model rows scripts/owui_model_rows.py writes (`model` table, read
  grants in `access_grant`) — #2673;
* the registry's MCP tool servers (`config` row `tool_server.connections`,
  each with `config.enable` and `config.access_grants`) — #2672.

Read-only. One `VERDICT|text` line per finding (PASS / WARN / INFO), the shape
cli/status.sh reads. Prints nothing on a schema without `access_grant` (Open
WebUI before 0.11: visibility was a column then, and NULL meant public).

Exit codes: 0 ok · 2 database/transport error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import owui_model_rows as mr  # noqa: E402

REPO = HERE.parent


def invisible_chat_models(existing: dict[str, dict[str, Any]],
                          grants: dict[str, list[dict[str, Any]]]) -> tuple[list[str], list[str]]:
    """`(never_granted, made_private)`: active, picker-visible chat rows the
    platform wrote (#1305 `managed_by` stamp) that carry no read grant.

    `made_private` are the rows the platform already granted once (the #2673
    ACCESS_SEEDED_KEY stamp) whose grants an admin has since removed — a
    refresh will not undo that, so they are named apart."""
    never, private = [], []
    for mid, row in sorted(existing.items()):
        if not mr.is_managed_row(row) or not row.get("is_active"):
            continue
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        if meta.get("hidden"):
            continue      # embedder / reranker / doc conversion: hidden on purpose
        if mr.has_read_grant(grants.get(mid)):
            continue
        (private if meta.get(mr.ACCESS_SEEDED_KEY) == mr.MANAGED_BY else never).append(mid)
    return never, private


def mcp_findings(current: list, emitted: list) -> dict[str, list[str]]:
    """The registry's connections, judged as Open WebUI 0.11 judges them
    (routers/tools.py:137 `config.enable`, :178-189 a read grant)."""
    by_url = {c.get("url"): c for c in current or [] if isinstance(c, dict)}
    out: dict[str, list[str]] = {"missing": [], "disabled": [], "private": [], "ok": []}
    for conn in emitted:
        name = (conn.get("info") or {}).get("id") or conn.get("url")
        cur = by_url.get(conn.get("url"))
        if cur is None:
            out["missing"].append(name)
            continue
        cfg = cur.get("config") or {}
        if not cfg.get("enable"):
            out["disabled"].append(name)
        elif not mr.has_read_grant(cfg.get("access_grants")):
            out["private"].append(name)
        else:
            out["ok"].append(name)
    return out


def read_tool_servers(base: list[str]) -> list:
    out = mr._run_sql(base, "SELECT COALESCE((SELECT value::text FROM config "
                            "WHERE key = 'tool_server.connections'), '[]');")
    try:
        value = json.loads(out.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise mr.DbError(f"unreadable tool_server.connections: {exc}") from exc
    return value if isinstance(value, list) else []


def registry_connections() -> list:
    sys.path.insert(0, str(REPO / "core" / "mcp"))
    import sync  # noqa: PLC0415
    return sync.owui_connections(sync.load_registry(), sync.active_profiles())


def report(existing, grants, current, emitted) -> list[str]:
    lines: list[str] = []
    never, private = invisible_chat_models(existing, grants)
    if never or private:
        parts = []
        if never:
            parts.append(f"{', '.join(never)} — fix: rzfz post-install --refresh")
        if private:
            parts.append(f"{', '.join(private)} — its access was removed in Open WebUI "
                         "(Workspace → Models → Access); grant it there if that was not meant")
        lines.append(f"WARN|Open WebUI: {len(never) + len(private)} chat model(s) invisible "
                     f"to non-admin users (no read grant, #2673): {'; '.join(parts)}")
    else:
        managed = sum(1 for r in existing.values() if mr.is_managed_row(r) and r.get("is_active")
                      and not (r.get("meta") or {}).get("hidden"))
        if managed:
            lines.append(f"PASS|Open WebUI: all {managed} platform chat model(s) readable by users")
    if emitted:
        f = mcp_findings(current, emitted)
        if f["missing"]:
            lines.append(f"WARN|Open WebUI: MCP tool server(s) not seeded (#2672): "
                         f"{', '.join(f['missing'])} — fix: rzfz post-install --refresh")
        if f["private"]:
            lines.append(f"WARN|Open WebUI: MCP tool server(s) invisible to non-admin users "
                         f"(no read grant, #2672): {', '.join(f['private'])} — grant access in "
                         "Admin → Settings → Integrations")
        if f["disabled"]:
            lines.append(f"INFO|Open WebUI: MCP tool server(s) switched off by an admin: "
                         f"{', '.join(f['disabled'])}")
        if f["ok"] and not (f["missing"] or f["private"]):
            lines.append(f"PASS|Open WebUI: {len(f['ok'])} registry MCP tool server(s) "
                         "enabled and readable by users")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("OPENWEBUI_DB") or "openwebui_db")
    ap.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER") or "docker")
    args = ap.parse_args(argv)
    base = mr._psql_cmd(args.db, args.pg_user)
    try:
        grants = mr.read_grants_if_any(base)
        if grants is None:
            return 0
        existing = mr.read_existing(base)
        current = read_tool_servers(base)
    except mr.DbError as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    try:
        emitted = registry_connections()
    except (SystemExit, Exception) as exc:  # must not hide the model verdict
        emitted = None
        print(f"WARN|Open WebUI: the MCP registry is unreadable ({exc}) — "
              "tool-server check skipped (#2672)")
    for line in report(existing, grants, current, emitted or []):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
