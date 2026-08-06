# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""razzfazz-shortcuts domain logic (2026.08 — issue #185).

Pure functions + psycopg2 DB access, mirroring the idioms in
``core/start-portal/app.py`` (``_db_conn`` / ``_ensure_prefs_table`` /
``_set_pinned``). No Flask routing lives here — ``app.py`` wires the routes.

A *shortcut* is an admin-defined, DB-backed tile in the start portal. P1 ships
the **redirect** kinds only (``owui_persona``, ``dify_chat``, ``cloud_link``):
the portal builds a pre-configured target URL server-side and the tile opens it.
In-portal execution (``dify_workflow``/``owui_prompt``/...), stored secrets, and
the ``cloud_api``/``agent_message`` kinds are P2/P3 and deliberately absent.

Variable injection is a **fixed whitelist**, never a template engine — user data
is never eval'd. In URL context every substituted value is URL-encoded so
``{{input}}`` cannot break out of the query string.
"""
from __future__ import annotations

import json
import os
import uuid
from urllib.parse import quote

import psycopg2
import psycopg2.extras
import requests

# ── Variable whitelist ───────────────────────────────────────────────────────
# The ONLY placeholders that resolve. Anything else is left literal. `ctx` is
# built server-side from the forward-auth identity + the user's one input box.
_WHITELIST = {
    "{{user.name}}":     lambda c: c.get("name", "") or "",
    "{{user.email}}":    lambda c: c.get("email", "") or "",
    "{{user.username}}": lambda c: c.get("username", "") or "",
    "{{user.groups}}":   lambda c: ",".join(c.get("groups", []) or []),
    "{{date}}":          lambda c: c.get("date", "") or "",
    "{{domain}}":        lambda c: c.get("domain", "") or "",
    "{{input}}":         lambda c: c.get("input", "") or "",
}


def render_vars(template: str, ctx: dict, mode: str = "text") -> str:
    """Substitute whitelisted ``{{placeholders}}`` in ``template`` from ``ctx``.

    ``mode="url"`` URL-encodes each substituted value (``safe=""`` — encode
    everything, including ``/`` and ``&``) so it is safe inside a query string.
    ``mode="text"`` substitutes raw. Unknown placeholders are left literal.
    """
    out = template or ""
    for token, getter in _WHITELIST.items():
        if token in out:
            val = getter(ctx)
            out = out.replace(token, quote(val, safe="") if mode == "url" else val)
    return out


# ── Redirect-URL builders (P1 kinds) ─────────────────────────────────────────
# Best-effort prompt-prefill bases for the cloud deep-link kind. If a provider
# drops its prefill param, the shortcut still opens the chat (degrade, not fail).
_CLOUD_PREFILL = {
    "claude":  "https://claude.ai/new?q=",
    "chatgpt": "https://chatgpt.com/?q=",
    "gemini":  "https://gemini.google.com/app?q=",
}

# The set of kinds P1 ships. Used both here and by the tile/route layer so the
# "redirect only in P1" gate lives in one place.
_REDIRECT_KINDS = frozenset({"owui_persona", "dify_chat", "cloud_link"})


def build_redirect_url(kind: str, config: dict, ctx: dict) -> str:
    """Build the target URL for a redirect shortcut. ``ctx["domain"]`` is the box.

    Raises ``ValueError`` for a non-redirect kind or an unknown cloud provider,
    ``KeyError`` when a required config field is missing.
    """
    dom = ctx["domain"]
    if kind == "owui_persona":
        return f"https://chat.{dom}/?models={quote(config['model_id'], safe='')}"
    if kind == "dify_chat":
        path = (config.get("app_path") or "").lstrip("/")
        return f"https://dify.{dom}/{path}"
    if kind == "cloud_link":
        base = _CLOUD_PREFILL.get(config.get("provider"))
        if not base:
            raise ValueError(f"unknown cloud provider: {config.get('provider')}")
        # The rendered prompt is a SINGLE query-param value, so encode it whole:
        # substitute the whitelist raw (text mode), then percent-encode the
        # entire result. (render_vars' own mode="url" only encodes substituted
        # values — correct when the template itself carries URL syntax, wrong
        # here where the literal prompt text is also part of the value.)
        rendered = render_vars(config.get("prompt_tmpl", ""), ctx, mode="text")
        return base + quote(rendered, safe="")
    raise ValueError(f"not a redirect kind: {kind}")


# ── Access resolution ────────────────────────────────────────────────────────

def user_can_use(shortcut: dict, user: dict) -> bool:
    """May ``user`` run ``shortcut``? Admin bypass, ``any`` group, or overlap.

    ``user`` is the ``_get_user()`` dict (``is_admin`` bool + ``groups`` list);
    ``shortcut["allowed_groups"]`` is the Authentik group-name list. This is the
    SAME rule the portal's ``_filter_tile`` uses for tile visibility, so a tile a
    user can see is exactly a shortcut they can ``/run``.
    """
    if user.get("is_admin"):
        return True
    allowed = set(shortcut.get("allowed_groups") or [])
    if "any" in allowed:
        return True
    return bool(allowed & set(user.get("groups") or []))


# ── Table + CRUD (psycopg2, mirrors app.py's _db_conn idioms) ────────────────
# Columns SELECTed back (secret_enc / secret_ref exist in the schema for P2 but
# are never read into the tile/admin surface in P1 — no secrets stored yet).
_COLS = ["id", "title", "description", "icon", "category", "sort_order",
         "allowed_groups", "kind", "config", "enabled", "created_by",
         "created_at", "updated_at"]

# Fields an update() may set. `kind` is included so the admin editor can retype
# a shortcut; the route layer still rejects non-P1 kinds before calling update.
_MUTABLE = {"title", "description", "icon", "category", "sort_order",
            "allowed_groups", "kind", "config", "enabled"}


def ensure_shortcuts_table(conn):
    """Auto-migrate the one shortcuts table at boot — same mechanism as
    ``_ensure_prefs_table`` (``CREATE TABLE IF NOT EXISTS``; no init-db.sh
    change). ``id`` is a Python ``uuid4`` string (TEXT), so no pgcrypto dep.
    ``secret_enc``/``secret_ref`` are declared now (unused in P1) so the P2
    secret store needs no migration."""
    with conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS start_portal_shortcuts (
                id             TEXT PRIMARY KEY,
                title          TEXT NOT NULL,
                description    TEXT NOT NULL DEFAULT '',
                icon           TEXT NOT NULL DEFAULT '✨',
                category       TEXT NOT NULL DEFAULT 'Shortcuts',
                sort_order     INTEGER NOT NULL DEFAULT 100,
                allowed_groups TEXT[] NOT NULL DEFAULT '{}',
                kind           TEXT NOT NULL,
                config         JSONB NOT NULL DEFAULT '{}',
                secret_enc     BYTEA,
                secret_ref     TEXT,
                enabled        BOOLEAN NOT NULL DEFAULT TRUE,
                created_by     TEXT NOT NULL,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


def _row(r):
    d = dict(r)
    # psycopg2 decodes JSONB to a dict already; be defensive if a driver hands
    # back the raw string.
    if isinstance(d.get("config"), str):
        d["config"] = json.loads(d["config"])
    # text[] -> list; psycopg2 already does this, but normalise None -> [].
    if d.get("allowed_groups") is None:
        d["allowed_groups"] = []
    return d


def list_all(conn):
    """Every shortcut, ordered by ``sort_order``. Defensive: returns [] if the
    table doesn't exist yet (mirrors the prefs reader's fail-soft behaviour)."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"SELECT {', '.join(_COLS)} FROM start_portal_shortcuts "
                f"ORDER BY sort_order, title")
            return [_row(r) for r in cur.fetchall()]
    except Exception:
        conn.rollback()
        return []


def get(conn, sid):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            f"SELECT {', '.join(_COLS)} FROM start_portal_shortcuts WHERE id = %s",
            (sid,))
        r = cur.fetchone()
        return _row(r) if r else None


def create(conn, *, title, kind, created_by, description="", icon="✨",
           category="Shortcuts", sort_order=100, allowed_groups=None, config=None):
    sid = str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO start_portal_shortcuts
              (id, title, description, icon, category, sort_order,
               allowed_groups, kind, config, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (sid, title, description, icon, category, sort_order,
              allowed_groups or [], kind, psycopg2.extras.Json(config or {}),
              created_by))
    return get(conn, sid)


def update(conn, sid, **fields):
    """Partial update of the mutable columns. Unknown fields are ignored."""
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _MUTABLE:
            continue
        sets.append(f"{k} = %s")
        vals.append(psycopg2.extras.Json(v) if k == "config" else v)
    if not sets:
        return get(conn, sid)
    sets.append("updated_at = NOW()")
    vals.append(sid)
    with conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE start_portal_shortcuts SET {', '.join(sets)} WHERE id = %s",
            vals)
    return get(conn, sid)


def delete(conn, sid):
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM start_portal_shortcuts WHERE id = %s", (sid,))
        return cur.rowcount > 0


# ── Shortcut rows -> portal tiles ────────────────────────────────────────────

def shortcuts_as_tiles(rows, user, ctx):
    """Convert shortcut rows into portal tile dicts, filtered to what ``user``
    may see. Drops disabled rows, non-redirect (P2/P3) kinds, and rows the user
    can't use; any row whose URL can't be built (bad config) is skipped rather
    than blowing up the whole page.

    Emitted tiles use ``icon_emoji`` (so the emoji branch in ``_tile.html``
    renders the emoji, not a PNG lookup) and ``required_group="any"`` /
    ``profile="core"`` — the access check already happened here, so the tiles
    pass the portal's ``_filter_tile`` gate unchanged and flow into the normal
    favourites/sections split.
    """
    tiles = []
    for r in rows:
        if not r.get("enabled"):
            continue
        if r.get("kind") not in _REDIRECT_KINDS:      # P1: redirect only
            continue
        if not user_can_use(r, user):
            continue
        try:
            url = build_redirect_url(r["kind"], r.get("config") or {}, ctx)
        except (ValueError, KeyError):
            continue
        tiles.append({
            "id": f"shortcut-{r['id']}",
            "name": r["title"],
            "category": r.get("category", "Shortcuts"),
            "icon_emoji": r.get("icon", "✨"),
            "url": url,
            "description": r.get("description", ""),
            "required_group": "any",
            "profile": "core",
            "order": r.get("sort_order", 100),
            "default_pinned": False,
        })
    return tiles


# ── Authentik groups + member resolution (admin UI) ──────────────────────────
# Same base + bootstrap token the password broker already uses server-side
# (password_broker.py): RZFZ_AUTHENTIK_BASE + AUTHENTIK_BOOTSTRAP_TOKEN, both
# injected into the portal container via env_file: ../.env.
_AK_BASE = os.environ.get("RZFZ_AUTHENTIK_BASE", "http://authentik-server:9000").rstrip("/")


def _authentik_get(path: str) -> dict:
    """GET the Authentik REST API with the bootstrap token. Raises on error."""
    token = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN", "")
    r = requests.get(f"{_AK_BASE}{path}",
                     headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/json"},
                     timeout=10)
    r.raise_for_status()
    return r.json() or {}


def fetch_groups() -> list[str]:
    """Every Authentik group name, for the admin's access multi-select."""
    data = _authentik_get("/api/v3/core/groups/?include_users=false")
    return [g["name"] for g in data.get("results", [])]


def resolve_members(group_names) -> list[str]:
    """Resolve group names to their effective member usernames — so the admin
    *sees which users* a shortcut's access grants (the operator's requirement).
    """
    wanted = set(group_names or [])
    if not wanted:
        return []
    data = _authentik_get("/api/v3/core/groups/?include_users=true")
    out = []
    for g in data.get("results", []):
        if g.get("name") in wanted:
            out += [u.get("username") for u in g.get("users_obj", []) if u.get("username")]
    return sorted(set(out))
