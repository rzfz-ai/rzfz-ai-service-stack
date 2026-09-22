# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""razzfazz.ai Start Portal — M028.

Renders the post-login launcher tile grid for the calling user.
Visibility: (user has required group OR user is admin) AND module is active.
Live tiles: per-user agent instances from agent-manager /api/instances.
Prefs: pinned + sort_order persisted per-user in the agent-manager Postgres.
"""

import base64
import json
import logging
import os
import re
from datetime import date, datetime, timezone

import psycopg2
import psycopg2.extras
import requests
import yaml
from flask import Response, current_app, jsonify, render_template, request, redirect, url_for

from razzfazz_common.flask_app import create_base_app

# razzfazz-shortcuts (#185) — admin-curated redirect tiles. Pure/DB logic lives
# in shortcuts.py; this module wires table-init, tile-merge, and the routes.
import shortcuts

# #248 P3 — curated scenario-template gallery for the shortcuts editor
# (presentation data only; see scenario_templates.py).
import scenario_templates

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

ADMIN_GROUP = 'authentik Admins'
SUPER_ADMIN_GROUP = 'razzfazz.ai Super Admins'

#: #1157 — agent types whose live tiles belong in "5 Development"; every
#: other type is a personal agent and lands in "1 Workspace". Matches the
#: agent-manager catalog's coding family (catalog.py ids).
CODING_AGENT_TYPES = frozenset({
    'coding-tools', 'opencode', 'gsd-pi', 'codex', 'user-defined',
})

#: #1158 rev-B — v4 (and older) category names -> their v5 homes. This is the
#: FOURTH consumer of the taxonomy canon (manifest, Authentik blueprints, the
#: upgrade.sh Django migration — and now the portal's own stored user state:
#: per-user categories, per-tile category overrides). Kept byte-identical to
#: upgrade.sh's GROUP_RENAME; test_1157_taxonomy_v5.py pins the equality.
CATEGORY_RENAMES = {
    "1 Use":                       "1 Workspace",
    "1 Productivity":              "1 Workspace",
    "2 Agentic AI":                "6 Automation & Agents",
    "2 AI Assistants":             "6 Automation & Agents",
    "3 Development & APIs":        "5 Development",
    "3 Development":               "5 Development",
    "4 razzfazz.ai Admin & Tools": "8 Administration",
    "5 Administration":            "8 Administration",
    "4 Knowledge Engines":         "3 Knowledge & Search",
    "1 LLM Inference":             "7 LLM Infrastructure",
}
COMPOSE_PROFILES_PATH = os.environ.get('STACK_ENV_PATH', '/stack/.env')
MANIFEST_PATH = os.environ.get('MANIFEST_PATH', '/app/manifest.yaml')
AGENT_MANAGER_URL = os.environ.get('AGENT_MANAGER_URL', 'http://agent-manager:5000')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

# #299 — native "My Profile" dialog (portal-local display name + avatar).
# Raw upload cap BEFORE base64 encoding (~+33% on the wire/at rest).
# Server-side backstop only: the profile dialog downscales the avatar to a
# ~256px square client-side (start-portal-profile.js), so a saved avatar is
# normally tiny. This cap just bounds a JS-off / crafted POST — generous enough
# that a normal photo uploaded without the client resize (e.g. JS disabled)
# still fits.
MAX_AVATAR_BYTES = 512 * 1024   # 512 KB
MAX_DISPLAY_NAME_LEN = 80

# ── App ──────────────────────────────────────────────────────────────────────

app = create_base_app(__name__, service_name='razzfazz-start-portal',
                     require_auth=False)

# #54 / #388 — CSRF.
#
# This used to be `enable_csrf(app, verify_methods=())` — context processor only,
# NO before_request hook — because the JSON prefs API (/api/prefs/*,
# /api/categories/*) was called via fetch without a token, so a global hook would
# have broken it. The cost of that workaround was that only the three surfaces
# which remembered to call `verify_csrf_token()` by hand (the password broker and
# the shortcuts admin routes) were actually protected; the EIGHT prefs/categories
# routes were not, and nothing made that visible.
#
# #388 closes it the fail-closed way round: the client now sends the token
# (`<meta name="csrf-token">` in index.html, read by static/start-portal.js — the
# idiom static/admin-shortcuts.js already used), so the GLOBAL hook can be turned
# on. Every state-changing method is verified by default, including routes added
# later, instead of depending on each new route's author remembering.
#
# Every state-changing route here is browser-driven (prefs, categories,
# shortcuts, password form); nothing POSTs to start-portal service-to-service, so
# there is no server-side caller to exempt. /healthz and /static/* are exempt
# inside razzfazz_common.csrf.
#
# The explicit `verify_csrf_token()` calls in the shortcuts + broker handlers are
# deliberately KEPT: they are idempotent under the global hook and they keep
# those handlers protected on their own terms.
from razzfazz_common.csrf import enable_csrf, verify_csrf_token  # noqa: E402
from password_broker import password_broker  # noqa: E402

enable_csrf(app)  # global before_request hook: POST/PUT/PATCH/DELETE
app.register_blueprint(password_broker)

# CFG-13: a hard ceiling on the request body, enforced by Werkzeug BEFORE any
# handler (and before `request.get_json()` buffers it). Without it there was
# no body limit anywhere in the tree, so an authenticated user could POST a
# several-hundred-MB `avatar` to /api/portal/profile; with 2 gunicorn workers
# (core/start-portal/Dockerfile) two such requests OOM the portal. The
# per-field caps (MAX_AVATAR_BYTES, the shortcut icon cap) bound what is
# STORED; this bounds what is READ. 1 MB comfortably clears the largest legal
# payload — a 512 KB avatar is ~683 KB once base64-encoded.
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024


# ── #68 source-IP anchor ─────────────────────────────────────────────────────
# start-portal identifies the caller — and, in the password broker, AUTHORIZES
# admin password resets — STRICTLY from the X-Authentik-* forward-auth headers.
# Those headers are only trustworthy when the request actually passed through
# Caddy, the sole ingress that ran Authentik forward_auth. Because start-portal
# shares the `_default` docker network with every other container, a peer could
# otherwise open a direct TCP connection to razzfazz-start-portal:5000, forge
# `X-Authentik-Username: akadmin` + `X-Authentik-Groups: authentik Admins`, and
# reset ARBITRARY users' passwords across Authentik / Dify / Cognee.
#
# We close that bypass exactly the way agent-manager's `_proxy_proof_ok`
# (modules/agents/manager/app/blueprints/proxy.py) does: anchor on the SOURCE
# IP. Every legitimate request arrives from Caddy; a peer dialling start-portal
# directly arrives from its own `_default` IP, which is NOT Caddy's, and cannot
# spoof Caddy's source IP (no NET_RAW on the hardened containers). Enforced as a
# before_request over EVERY authenticated route — the broker `/password`
# GET+POST, the admin-capable prefs/categories APIs, and the index — so a forged
# identity is refused (403) before any header-trusting code runs. Fail-closed;
# NO X-Forwarded-For trust. See proxy_anchor.py for the full rationale.
from proxy_anchor import came_through_caddy  # noqa: E402

# Only the health probe and static assets stay reachable without the anchor:
# `/healthz` is polled by Docker / monitoring (not via Caddy) and carries no
# identity; `/static/` is public. Everything else consumes the forward-auth
# identity and MUST prove it came through Caddy.
_ANCHOR_EXEMPT_PREFIXES = ('/healthz', '/static/')


@app.before_request
def _enforce_caddy_source_ip():
    path = request.path
    if path.startswith(_ANCHOR_EXEMPT_PREFIXES):
        return None
    if not came_through_caddy():
        logger.warning(
            "start-portal: refusing %s %s from %s — not via Caddy "
            "(source-IP anchor, #68)", request.method, path, request.remote_addr,
        )
        return ('Forbidden', 403)
    return None


def _resolve_admin_email():
    """#54 — admin contact for the password-help mailto link.

    Resolution:
      1. ADMIN_CONTACT_EMAIL  — dedicated operator-configured contact (if set)
      2. razzfazz-ai-admin@MAIN_DOMAIN  — sensible default for the fleet

    An empty ADMIN_CONTACT_EMAIL is treated as unset. If MAIN_DOMAIN is also
    empty we fall back to `razzfazz-ai-admin@localhost` so the mailto is always
    a well-formed address and rendering never crashes.

    Exposed to ALL templates via the context processor below so both the
    in-page modal (index.html) and the /password no-JS fallback (password.html
    rendered by the broker) — both of which include _password_form.html — pick
    it up uniformly.
    """
    contact = (os.environ.get('ADMIN_CONTACT_EMAIL') or '').strip()
    if contact:
        return contact
    domain = (os.environ.get('MAIN_DOMAIN') or '').strip() or 'localhost'
    return f"razzfazz-ai-admin@{domain}"


@app.context_processor
def _inject_admin_email():
    return {'admin_email': _resolve_admin_email()}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_user():
    """Read the X-Authentik-* forward-auth headers."""
    username = request.headers.get('X-Authentik-Username', '')
    email = request.headers.get('X-Authentik-Email', '')
    uid = request.headers.get('X-Authentik-Uid', username)
    groups_raw = request.headers.get('X-Authentik-Groups', '')
    groups = [g.strip() for g in groups_raw.split('|') if g.strip()]
    is_admin = ADMIN_GROUP in groups or SUPER_ADMIN_GROUP in groups
    user_slug = (username or '').lower().replace(' ', '-').replace('_', '-')
    return {
        'username': username,
        'email': email,
        'uid': uid,
        'groups': groups,
        'is_admin': is_admin,
        'user_slug': user_slug,
    }


def _read_compose_profiles():
    """Read active COMPOSE_PROFILES.

    Prefers the live .env file at /stack/.env (so the operator toggling a
    profile via the Configuration Portal is reflected without a portal
    restart). Falls back to the container's own COMPOSE_PROFILES env if
    the file is unreadable (the .env file is mode 0600 on a hardened
    install — appuser inside the container can't read it). When falling
    back to env, profile toggles via the Configuration Portal don't
    propagate until the portal container is recreated by apply_manager;
    that's an acceptable degraded mode (apply_manager already recreates
    dependents on toggle — the portal will be on that list as a follow-up
    in M028 S05's scope).
    """
    if os.path.exists(COMPOSE_PROFILES_PATH):
        try:
            with open(COMPOSE_PROFILES_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('COMPOSE_PROFILES='):
                        value = line.split('=', 1)[1].strip()
                        if value and value[0] in ('"', "'") and value[-1] == value[0]:
                            value = value[1:-1]
                        return set(p.strip() for p in value.split(',') if p.strip())
        except (PermissionError, OSError) as e:
            logger.info(f"Cannot read {COMPOSE_PROFILES_PATH} live ({e}); "
                        f"falling back to container env.")
    env_value = os.environ.get('COMPOSE_PROFILES', '')
    return set(p.strip() for p in env_value.split(',') if p.strip())


def _load_manifest():
    """Load the tile manifest. Returns dict with `tiles` list."""
    if not os.path.exists(MANIFEST_PATH):
        logger.error(f"Manifest not found: {MANIFEST_PATH}")
        return {'tiles': []}
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f) or {'tiles': []}


def _expand_env_in_url(url):
    """Substitute ${VAR} from os.environ in a tile URL."""
    if not url:
        return url
    main_domain = os.environ.get('MAIN_DOMAIN', '')
    agents_domain = os.environ.get('AGENTS_DOMAIN', f'agents.{main_domain}')
    repls = {
        '${MAIN_DOMAIN}': main_domain,
        '${AGENTS_DOMAIN}': agents_domain,
    }
    # Generic env-substitution for other ${VAR} patterns.
    for k, v in os.environ.items():
        if not k.isupper():
            continue
        repls[f'${{{k}}}'] = v
    out = url
    for k, v in repls.items():
        out = out.replace(k, v)
    return out


def _filter_tile(tile, user, active_profiles):
    """Apply the visibility rules: (user has group OR is admin) AND profile active.

    The manifest's `profile:` field accepts:
      * a single profile name (e.g. "chat")
      * the literal "core" (always-on; no profile gate)
      * a comma- or pipe-separated list of profiles (e.g. "llm,llm-legacy,llm-cpu")
        — the tile is shown if ANY of the listed profiles is active.
    The list form lets one tile cover multiple interchangeable backends
    that share a single UI (added 2026-05-09 after the LLM Management tile
    was missing for llm-legacy + llm-cpu boxes; it had `profile: llm` and
    legacy boxes filtered it out even though llm.<domain> works on all
    three).
    """
    profile = tile.get('profile', 'core')
    if profile != 'core':
        # Split on , or |, accept either separator. Empty entries dropped.
        candidates = [p.strip() for p in profile.replace('|', ',').split(',') if p.strip()]
        if not any(p in active_profiles for p in candidates):
            return False
    # #1157: `admin_only: true` hides a tile from non-admins even when its
    # required_group would admit them — the v5 taxonomy hides builder/infra
    # tiles (Dify, GPUStack, Observability, …) from end users PORTAL-SIDE
    # only; Authentik policies are deliberately untouched. Must sit before
    # the admin short-circuit below.
    if tile.get('admin_only') and not user['is_admin']:
        return False
    # Group rule. Admins see everything regardless of group.
    if user['is_admin']:
        return True
    required = tile.get('required_group', 'any')
    if required == 'any':
        return True
    return required in user['groups']


# ── Database helpers (per-user prefs) ────────────────────────────────────────

def _db_conn():
    if not DATABASE_URL:
        return None
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        logger.error(f"DB connect failed: {e}")
        return None


def _ensure_prefs_table():
    """Create the start_portal prefs + categories tables.

    S11.6 adds:
      - `category` override column on start_portal_prefs (per-user move of
        a tile from its manifest category to a different one).
      - start_portal_categories table for per-user category metadata
        (name override + sort order). When this is empty for a user,
        manifest categories are used as-is (canonical).
    """
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS start_portal_prefs (
                    user_slug TEXT NOT NULL,
                    tile_id TEXT NOT NULL,
                    pinned BOOLEAN NOT NULL DEFAULT FALSE,
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    category TEXT,          -- S11.6: per-user category override; NULL = manifest's
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_slug, tile_id)
                )
            """)
            # Add the column if the table predates S11.6.
            cur.execute("""
                ALTER TABLE start_portal_prefs
                ADD COLUMN IF NOT EXISTS category TEXT
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS start_portal_categories (
                    user_slug TEXT NOT NULL,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_slug, name)
                )
            """)
            # #1158 rev-B: migrate stored user state off retired category
            # names. Without this, every user who ever created a category or
            # dragged a tile keeps dead v4 headings that sort ABOVE the v5
            # sections (user_cats ranks bucket 0), and tiles pinned to a
            # retired name stay stranded there. Idempotent: matches only the
            # retired names. Categories need collision handling — a user may
            # already own a category under the target name (PK user_slug+name),
            # in which case the old row is dropped instead of renamed.
            for old_name, new_name in CATEGORY_RENAMES.items():
                cur.execute(
                    "UPDATE start_portal_prefs SET category = %s "
                    "WHERE category = %s",
                    (new_name, old_name))
                cur.execute(
                    "DELETE FROM start_portal_categories old "
                    "WHERE old.name = %s AND EXISTS ("
                    "  SELECT 1 FROM start_portal_categories tgt "
                    "  WHERE tgt.user_slug = old.user_slug AND tgt.name = %s)",
                    (old_name, new_name))
                cur.execute(
                    "UPDATE start_portal_categories SET name = %s "
                    "WHERE name = %s",
                    (new_name, old_name))
    except Exception as e:
        logger.error(f"Could not ensure prefs/categories tables: {e}")
    finally:
        conn.close()


def _get_user_prefs(user_slug):
    """Returns dict {tile_id: {pinned: bool, sort_order: int, category: str|None}}."""
    conn = _db_conn()
    if not conn:
        return {}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT tile_id, pinned, sort_order, category FROM start_portal_prefs WHERE user_slug = %s",
                (user_slug,),
            )
            return {r['tile_id']: {'pinned': r['pinned'], 'sort_order': r['sort_order'],
                                   'category': r.get('category')}
                    for r in cur.fetchall()}
    except Exception as e:
        logger.error(f"Pref read failed: {e}")
        return {}
    finally:
        conn.close()


def _get_user_categories(user_slug):
    """Returns list of {name, sort_order} dicts for the user's categories.
    Empty list = no per-user overrides, use manifest categories.
    """
    conn = _db_conn()
    if not conn:
        return []
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT name, sort_order FROM start_portal_categories WHERE user_slug = %s ORDER BY sort_order, name",
                (user_slug,),
            )
            return [{'name': r['name'], 'sort_order': r['sort_order']} for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"Categories read failed: {e}")
        return []
    finally:
        conn.close()


def _set_tile_category(user_slug, tile_id, category):
    """S11.6: per-user override of a tile's category. category=None drops override."""
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO start_portal_prefs (user_slug, tile_id, category, pinned, sort_order)
                VALUES (%s, %s, %s, FALSE, 100)
                ON CONFLICT (user_slug, tile_id)
                DO UPDATE SET category = EXCLUDED.category, updated_at = NOW()
            """, (user_slug, tile_id, category))
        return True
    except Exception as e:
        logger.error(f"Category move write failed: {e}")
        return False
    finally:
        conn.close()


def _upsert_category(user_slug, name, sort_order=None):
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            if sort_order is None:
                cur.execute("""
                    INSERT INTO start_portal_categories (user_slug, name, sort_order)
                    VALUES (%s, %s, 100)
                    ON CONFLICT (user_slug, name) DO UPDATE SET updated_at = NOW()
                """, (user_slug, name))
            else:
                cur.execute("""
                    INSERT INTO start_portal_categories (user_slug, name, sort_order)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_slug, name)
                    DO UPDATE SET sort_order = EXCLUDED.sort_order, updated_at = NOW()
                """, (user_slug, name, int(sort_order)))
        return True
    except Exception as e:
        logger.error(f"Category upsert failed: {e}")
        return False
    finally:
        conn.close()


def _rename_category(user_slug, old_name, new_name):
    """S11.6: rename a category. Updates the row in categories AND every prefs.category override."""
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE start_portal_categories SET name = %s, updated_at = NOW()
                WHERE user_slug = %s AND name = %s
            """, (new_name, user_slug, old_name))
            cur.execute("""
                UPDATE start_portal_prefs SET category = %s, updated_at = NOW()
                WHERE user_slug = %s AND category = %s
            """, (new_name, user_slug, old_name))
        return True
    except Exception as e:
        logger.error(f"Category rename failed: {e}")
        return False
    finally:
        conn.close()


def _delete_category(user_slug, name):
    """S11.6: delete a category. Caller must verify it's empty (no tiles)."""
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM start_portal_categories WHERE user_slug = %s AND name = %s",
                (user_slug, name),
            )
        return True
    except Exception as e:
        logger.error(f"Category delete failed: {e}")
        return False
    finally:
        conn.close()


def _set_pinned(user_slug, tile_id, pinned):
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO start_portal_prefs (user_slug, tile_id, pinned, sort_order)
                VALUES (%s, %s, %s, 100)
                ON CONFLICT (user_slug, tile_id)
                DO UPDATE SET pinned = EXCLUDED.pinned, updated_at = NOW()
            """, (user_slug, tile_id, pinned))
        return True
    except Exception as e:
        logger.error(f"Pin write failed: {e}")
        return False
    finally:
        conn.close()


def _set_orders(user_slug, orders):
    """orders: dict {tile_id: int}. Partial updates only — never replaces full set."""
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            for tile_id, order in orders.items():
                cur.execute("""
                    INSERT INTO start_portal_prefs (user_slug, tile_id, sort_order, pinned)
                    VALUES (%s, %s, %s, FALSE)
                    ON CONFLICT (user_slug, tile_id)
                    DO UPDATE SET sort_order = EXCLUDED.sort_order, updated_at = NOW()
                """, (user_slug, tile_id, int(order)))
        return True
    except Exception as e:
        logger.error(f"Order write failed: {e}")
        return False
    finally:
        conn.close()


# ── #299 native "My Profile" store (portal-local display name + avatar) ────
#
# Portal-local, deliberately: this table personalizes how the START PORTAL
# renders a user's own greeting/avatar chip. It does NOT write back to
# Authentik/SSO identity — a user's real name, email, and login avatar (if
# Authentik ever grows one) are untouched. Same substrate + idiom as the
# existing prefs/shortcuts tables (`_db_conn()` + a best-effort
# `_ensure_*_table()` run at boot).
#
# The avatar is stored as a size-capped `data:` URI directly in the row
# (mirrors how small, per-user, low-traffic blobs already live inline in
# this service rather than behind a separate object store) — one row per
# user, not a new column bolted onto `start_portal_prefs` (that table is
# keyed per (user_slug, tile_id); a user-wide value doesn't fit its shape).

def _ensure_profile_table():
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS start_portal_profile (
                    user_slug TEXT PRIMARY KEY,
                    display_name TEXT,
                    avatar_data_uri TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
    except Exception as e:
        logger.error(f"Could not ensure profile table: {e}")
    finally:
        conn.close()


def _get_user_profile(user_slug):
    """Returns {'display_name': str|None, 'avatar_data_uri': str|None}.

    Empty dict-shaped default (both None) when no row exists or the DB is
    unavailable — callers fall back to the Authentik username/initial."""
    conn = _db_conn()
    if not conn:
        return {'display_name': None, 'avatar_data_uri': None}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT display_name, avatar_data_uri FROM start_portal_profile "
                "WHERE user_slug = %s",
                (user_slug,),
            )
            row = cur.fetchone()
            if not row:
                return {'display_name': None, 'avatar_data_uri': None}
            return {'display_name': row['display_name'],
                    'avatar_data_uri': row['avatar_data_uri']}
    except Exception as e:
        logger.error(f"Profile read failed: {e}")
        return {'display_name': None, 'avatar_data_uri': None}
    finally:
        conn.close()


def _set_display_name(user_slug, display_name):
    """Upsert just the display_name column. `display_name=None` clears it."""
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO start_portal_profile (user_slug, display_name)
                VALUES (%s, %s)
                ON CONFLICT (user_slug)
                DO UPDATE SET display_name = EXCLUDED.display_name, updated_at = NOW()
            """, (user_slug, display_name))
        return True
    except Exception as e:
        logger.error(f"Display-name write failed: {e}")
        return False
    finally:
        conn.close()


def _set_avatar(user_slug, avatar_data_uri):
    """Upsert just the avatar_data_uri column. `avatar_data_uri=None` clears it."""
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO start_portal_profile (user_slug, avatar_data_uri)
                VALUES (%s, %s)
                ON CONFLICT (user_slug)
                DO UPDATE SET avatar_data_uri = EXCLUDED.avatar_data_uri, updated_at = NOW()
            """, (user_slug, avatar_data_uri))
        return True
    except Exception as e:
        logger.error(f"Avatar write failed: {e}")
        return False
    finally:
        conn.close()


_DATA_URI_RE = re.compile(r'^data:([^;,]+);base64,(.+)$', re.S)

# Sniff the actual image type from magic bytes — NEVER trust the client's
# claimed `data:<mime>` prefix alone (that's client-controlled and trivially
# spoofed; a renamed .exe could claim `data:image/png`).
_IMAGE_MAGIC = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)


def _sniff_image_mime(raw: bytes):
    for magic, mime in _IMAGE_MAGIC:
        if raw.startswith(magic):
            return mime
    if len(raw) >= 12 and raw[0:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _validate_avatar_data_uri(data_uri: str):
    """Parse + validate a client-submitted `data:<mime>;base64,<data>` avatar.

    Returns a NORMALIZED data URI (mime rebuilt from the sniffed magic bytes,
    never the client-claimed one) on success, or None on any validation
    failure (malformed, oversize, or not a recognised image format) — the
    caller turns None into a 400.
    """
    m = _DATA_URI_RE.match((data_uri or '').strip())
    if not m:
        return None
    _claimed_mime, b64 = m.group(1), m.group(2)
    # CFG-13: reject on the ENCODED length first. `b64decode` on the whole
    # client string ran BEFORE the size test, so the cap bounded what was
    # stored, not what was processed — the opposite of what it is for. With
    # 2 gunicorn workers, two concurrent multi-hundred-MB avatars were enough
    # to OOM the portal. base64 expands by 4/3 (+ up to 4 bytes of padding),
    # so anything longer than this cannot decode within the cap.
    if len(b64) > (MAX_AVATAR_BYTES * 4) // 3 + 8:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > MAX_AVATAR_BYTES:
        return None
    sniffed = _sniff_image_mime(raw)
    if not sniffed:
        return None
    return f'data:{sniffed};base64,{b64}'


# ── Live agent tiles ─────────────────────────────────────────────────────────

def _fetch_running_agents(user):
    """Call agent-manager /api/instances forwarding user headers; filter to running."""
    if not user['username']:
        return []
    try:
        r = requests.get(
            f'{AGENT_MANAGER_URL}/api/instances',
            headers={
                'X-Authentik-Username': user['username'],
                'X-Authentik-Uid': user['uid'],
                'X-Authentik-Email': user['email'],
                'X-Authentik-Groups': '|'.join(user['groups']),
            },
            timeout=3,
        )
        if r.status_code != 200:
            return []
        instances = r.json().get('instances', [])
        # Only running; only the calling user (agent-manager already filters).
        return [i for i in instances if i.get('state') == 'running']
    except Exception as e:
        logger.warning(f"agent-manager fetch failed: {e}")
        return []


def _greeting_time_word(hour):
    """Map a 24-hour clock hour to a time-of-day greeting word.

    5:00-11:59 -> morning, 12:00-17:59 -> afternoon, everything else
    (18:00-4:59) -> evening. Every integer hour maps to exactly one of the
    three words, so this branch never needs a further fallback.
    """
    if 5 <= hour < 12:
        return 'morning'
    if 12 <= hour < 18:
        return 'afternoon'
    return 'evening'


def _portal_hero_greeting(display_name, now=None):
    """#299 tile-reskin-to-prototype — build the Start Portal hero greeting:
    "Good morning/afternoon/evening, <first name>", time-of-day based on the
    server's local clock. Falls back to a plain "Welcome, <name>" if the
    time-of-day can't be determined (defensive — the hero must never break
    the page render) or to a bare "Welcome"/"Good <time>" when there's no
    name to greet.

    `now` is injectable (a datetime, or anything with an `.hour` attribute)
    so tests can pin a specific time-of-day without depending on wall clock.
    """
    name = (display_name or '').strip()
    first = name.split()[0] if name else ''
    try:
        hour = (now if now is not None else datetime.now()).hour
        word = _greeting_time_word(hour)
        return f'Good {word}, {first}' if first else f'Good {word}'
    except Exception:  # noqa: BLE001 — the hero must never break the page
        return f'Welcome, {first}' if first else 'Welcome'


def _agent_to_tile(inst, agents_domain):
    """Convert an agent-manager instance dict to a tile dict.

    #1157 (v5 taxonomy): live agent tiles route by TYPE CLASS — coding agents
    (OpenCode, Codex, Coding Tools, user-defined) emit into "5 Development"
    next to Gitea/OpenHands, everything personal (Hermes, Moltis, Paperclip,
    …) into "1 Workspace" next to Chat. Supersedes the S11 #6 single-bucket
    "2 AI Assistants" section, which the v5 taxonomy retired.
    """
    agent_type = inst['agent_type']
    user_slug = inst['user_slug']
    icon_map = {
        'hermes': 'hermes', 'moltis': 'moltis', 'paperclip': 'paperclip',
        'openhands': 'openhands', 'coding-tools': 'coding',
    }
    icon = icon_map.get(agent_type, 'home')
    # #299 operator design feedback (2026-08-30) — every agent tile's default
    # name now reads "<Type> Agent" ("My Hermes" → "Hermes Agent") so the
    # grid reads consistently regardless of agent type. `user-defined` covers
    # a fully custom agent type with no fixed identity of its own.
    name_map = {
        'hermes': 'Hermes Agent', 'moltis': 'Moltis Agent',
        'paperclip': 'Paperclip Agent', 'openhands': 'OpenHands Agent',
        'coding-tools': 'Coding Agent', 'codex': 'Codex Agent',
        'opencode': 'OpenCode Agent', 'user-defined': 'Custom Agent',
    }
    # #233 — the user's own name wins when they set one. Only then: an agent
    # nobody has renamed keeps the friendlier "<Type> Agent" wording, which
    # reads better on a start page than the bare type name the API returns as
    # its default `display_name`.
    name = (inst.get('custom_name')
            or name_map.get(agent_type, inst.get('type_display_name', agent_type)))
    desc_map = {
        'hermes': 'Your personal Hermes agent — workspace UI, memory plugin, gateway.',
        'moltis': 'Your personal Moltis Rust agent server with Matrix/Telegram/Discord gateway.',
        'paperclip': 'Your personal Paperclip workspace — orchestrate AI agents.',
        'openhands': 'Your personal OpenHands instance for autonomous coding tasks.',
        'coding-tools': 'Your personal coding workspace — gsd-pi + opencode terminal UI.',
    }
    # Use the canonical URL that agent-manager returns — it is derived from the
    # opaque per-instance token ({type}-{token}.agents.<domain>) that Caddy
    # actually routes. We must NOT build the URL from user_slug: (1) it leaks the
    # username into the subdomain (DNS / TLS SNI — the token scheme exists to
    # prevent this), and (2) it points at a hostname Caddy doesn't serve, so the
    # tile link is dead. Fallback to the legacy user_slug form only if the API
    # didn't supply a url (older agent-manager); start-portal has no access to
    # AGENT_DOMAIN_TOKEN_SECRET, so it cannot derive the token itself.
    url = inst.get('url') or f'https://{agent_type}-{user_slug}.{agents_domain}'
    return {
        'id': f'agent-{agent_type}-{user_slug}',
        'name': name,
        'category': ('5 Development'
                     if agent_type in CODING_AGENT_TYPES
                     else '1 Workspace'),
        'icon': icon,
        'url': url,
        'description': desc_map.get(agent_type,
                                    'Running coding agent instance.'
                                    if agent_type in CODING_AGENT_TYPES
                                    else 'Running personal agent instance.'),
        'instance_id': inst['id'],
        'is_live_agent': True,
        # #299 Phase 2 — presentation-only: live agent instances are
        # razzfazz-built (Hermes/Moltis/OpenHands/OpenCode/Paperclip
        # supervision), so they carry the red rzfz.ai chip, not a vendor
        # chip. No route/query/auth change.
        'chip_kind': 'native',
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    user = _get_user()
    if not user['username']:
        # Forward-auth misconfigured or test access — show a minimal page.
        return render_template('index.html', user=user, sections=[],
                              favorites=[], live_agent_count=0,
                              display_name='', saved_display_name='',
                              avatar_data_uri='')

    manifest = _load_manifest()
    profiles = _read_compose_profiles()
    prefs = _get_user_prefs(user['user_slug'])
    user_cats = _get_user_categories(user['user_slug'])

    # Filter manifest tiles.
    visible = []
    for tile in manifest.get('tiles', []):
        if not _filter_tile(tile, user, profiles):
            continue
        # Apply per-user prefs.
        tile_prefs = prefs.get(tile['id'], {})
        tile = dict(tile)  # don't mutate manifest
        tile['url'] = _expand_env_in_url(tile.get('url', ''))
        tile['pinned'] = tile_prefs.get('pinned', tile.get('default_pinned', False))
        tile['sort_order'] = tile_prefs.get('sort_order', tile.get('order', 100))
        # S11.6 — per-user category override.
        if tile_prefs.get('category'):
            tile['category'] = tile_prefs['category']
        visible.append(tile)

    # Append live agent tiles (always rendered if running, regardless of manifest).
    agents_domain = os.environ.get('AGENTS_DOMAIN',
                                   f"agents.{os.environ.get('MAIN_DOMAIN', '')}")
    for inst in _fetch_running_agents(user):
        tile = _agent_to_tile(inst, agents_domain)
        tile_prefs = prefs.get(tile['id'], {})
        tile['pinned'] = tile_prefs.get('pinned', False)
        tile['sort_order'] = tile_prefs.get('sort_order', 50)
        if tile_prefs.get('category'):
            tile['category'] = tile_prefs['category']
        visible.append(tile)

    # #185 — append admin-curated shortcut tiles (redirect kinds in P1).
    # shortcuts_as_tiles has already access-filtered to what this user may see,
    # so the emitted tiles carry required_group="any"/profile="core" and flow
    # through the same per-user pinned/sort_order/category defaulting as the
    # manifest + agent tiles above.
    sc_ctx = _shortcut_ctx(user, '')
    sc_conn = _db_conn()
    if sc_conn:
        try:
            sc_tiles = shortcuts.shortcuts_as_tiles(
                shortcuts.list_all(sc_conn), user, sc_ctx)
        except Exception as e:  # noqa: BLE001 — a bad shortcut must not 500 the portal
            logger.error(f"Shortcut tile build failed: {e}")
            sc_tiles = []
        finally:
            sc_conn.close()
        for tile in sc_tiles:
            # #299 Phase 2 — presentation-only default: admin-curated
            # shortcut tiles point at arbitrary third-party targets, so
            # default them to a grey vendor chip unless the shortcut itself
            # already set one. No route/query/auth change.
            tile.setdefault('chip_kind', 'vendor')
            tile_prefs = prefs.get(tile['id'], {})
            tile['pinned'] = tile_prefs.get('pinned', tile.get('default_pinned', False))
            tile['sort_order'] = tile_prefs.get('sort_order', tile.get('order', 100))
            if tile_prefs.get('category'):
                tile['category'] = tile_prefs['category']
            visible.append(tile)

    # Split favorites and categorized.
    favorites = sorted([t for t in visible if t['pinned']],
                       key=lambda t: (t['sort_order'], t['id']))
    by_category = {}
    for t in visible:
        if t['pinned']:
            continue
        by_category.setdefault(t['category'], []).append(t)

    # S11 #13 — display_name strips the leading "<digit> " sort prefix from
    # the category name. The manifest still uses "1 Productivity" etc. as
    # the canonical sort key; the template renders just "Productivity".
    def _display_name(cat):
        return re.sub(r'^\d+\s+', '', cat)

    # S11.6 — sort order resolution. If the user has any rows in
    # start_portal_categories, those rows are authoritative for the order;
    # categories not in that list (newly visible because the user got
    # added to a group, etc.) fall back to manifest sort.
    user_cat_order = {c['name']: c['sort_order'] for c in user_cats}
    def _cat_sort_key(cat):
        # Prefer user override; otherwise sort by manifest's leading digit.
        if cat in user_cat_order:
            return (0, user_cat_order[cat], cat)
        return (1, 0, cat)

    # Union of categories that have tiles AND categories the user explicitly
    # created (which may be empty). Pre-hotfix iterated only `by_category` so
    # a freshly-created empty user category never appeared as a section
    # heading, even though the row was correctly persisted to
    # start_portal_categories — operator-reported as "new category does not
    # show up after creation" (v2026.05-ga.4 hotfix). Empty sections render
    # with zero tiles so the user can drag tiles into them.
    all_categories = sorted(
        set(by_category.keys()) | {c['name'] for c in user_cats},
        key=_cat_sort_key,
    )

    sections = []
    for cat in all_categories:
        items = sorted(by_category.get(cat, []), key=lambda t: (t['sort_order'], t['id']))
        sections.append({
            'name': cat,
            'display_name': _display_name(cat),
            'tiles': items,
        })

    # #54 Fix 2 — the in-page password modal includes _password_form.html, which
    # decides whether to show the "Current password" field from is_admin +
    # is_federated. The modal is always SELF-service (the caller's own
    # password), so probe federation on the caller's own email. _is_federated is
    # fail-safe (returns False/local on any probe error) so this never breaks the
    # page render.
    try:
        from password_broker import _is_federated as _pb_is_federated
        modal_is_federated = _pb_is_federated((user.get('email') or '').strip())
    except Exception:  # noqa: BLE001
        modal_is_federated = False

    # #299 — native "My Profile" dialog: the portal-local display name /
    # avatar the user saved themselves win over the Authentik identity;
    # falls back to the username / its first letter when nothing is saved
    # (or the DB is unavailable) — unchanged from the pre-#299 behaviour.
    user_profile = _get_user_profile(user['user_slug'])
    saved_display_name = (user_profile.get('display_name') or '').strip()
    display_name = saved_display_name or user['username']
    avatar_data_uri = user_profile.get('avatar_data_uri') or ''

    # #299 tile-reskin-to-prototype — hero greeting, replaces the bare
    # "My Applications" title.
    hero_greeting = _portal_hero_greeting(display_name)

    return render_template('index.html', user=user, sections=sections,
                          favorites=favorites,
                          all_categories=all_categories,
                          display_name=display_name,
                          saved_display_name=saved_display_name,
                          avatar_data_uri=avatar_data_uri,
                          hero_greeting=hero_greeting,
                          is_admin=user['is_admin'],
                          is_federated=modal_is_federated,
                          live_agent_count=sum(1 for t in visible if t.get('is_live_agent')))


# ── S11.6 API endpoints ──────────────────────────────────────────────────────

@app.route('/api/prefs/move', methods=['POST'])
def api_move_tile():
    """Set a tile's per-user category override."""
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    body = request.get_json(silent=True) or {}
    tile_id = body.get('tile_id')
    category = body.get('category')  # may be None to clear override
    if not tile_id:
        return jsonify({'error': 'tile_id required'}), 400
    ok = _set_tile_category(user['user_slug'], tile_id, category)
    # Auto-create the category in start_portal_categories if it doesn't
    # exist yet — so the user can move tiles to brand-new categories.
    if ok and category:
        _upsert_category(user['user_slug'], category)
    return jsonify({'ok': ok})


@app.route('/api/categories', methods=['POST'])
def api_create_category():
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    ok = _upsert_category(user['user_slug'], name)
    return jsonify({'ok': ok, 'name': name})


@app.route('/api/categories/order', methods=['POST'])
def api_categories_order():
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    body = request.get_json(silent=True) or {}
    orders = body.get('orders', {})
    if not isinstance(orders, dict):
        return jsonify({'error': 'orders must be a {name: int} object'}), 400
    ok = True
    for name, order in orders.items():
        ok = _upsert_category(user['user_slug'], name, sort_order=order) and ok
    return jsonify({'ok': ok})


@app.route('/api/categories/<path:name>', methods=['PATCH'])
def api_rename_category(name):
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    body = request.get_json(silent=True) or {}
    new_name = (body.get('name') or '').strip()
    if not new_name:
        return jsonify({'error': 'name required'}), 400
    ok = _rename_category(user['user_slug'], name, new_name)
    return jsonify({'ok': ok, 'name': new_name})


@app.route('/api/categories/<path:name>', methods=['DELETE'])
def api_delete_category(name):
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    # Guardrail: only delete if empty (no tiles).
    prefs = _get_user_prefs(user['user_slug'])
    has_user_tile = any(p.get('category') == name for p in prefs.values())
    if has_user_tile:
        return jsonify({'error': 'category is not empty'}), 409
    ok = _delete_category(user['user_slug'], name)
    return jsonify({'ok': ok})


@app.route('/api/prefs/pin/<tile_id>', methods=['POST'])
def api_pin(tile_id):
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    pinned = (request.get_json(silent=True) or {}).get('pinned', True)
    ok = _set_pinned(user['user_slug'], tile_id, bool(pinned))
    return jsonify({'ok': ok, 'pinned': bool(pinned)})


@app.route('/api/prefs/order', methods=['POST'])
def api_order():
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    body = request.get_json(silent=True) or {}
    orders = body.get('orders', {})
    if not isinstance(orders, dict):
        return jsonify({'error': 'orders must be an object'}), 400
    ok = _set_orders(user['user_slug'], orders)
    return jsonify({'ok': ok})


@app.route('/api/prefs/reset', methods=['POST'])
def api_reset_prefs():
    """S11.6 — reset the calling user's layout to manifest defaults.

    Wipes start_portal_prefs (pins, sort overrides, category overrides) and
    start_portal_categories (custom category names + ordering) for this user.
    The next page render falls back to the manifest as if the user had never
    customised anything.
    """
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    conn = _db_conn()
    if not conn:
        return jsonify({'error': 'db unavailable'}), 503
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM start_portal_prefs WHERE user_slug = %s",
                        (user['user_slug'],))
            cur.execute("DELETE FROM start_portal_categories WHERE user_slug = %s",
                        (user['user_slug'],))
        return jsonify({'ok': True})
    except Exception as e:
        logger.error(f"Reset prefs failed: {e}")
        return jsonify({'error': 'reset failed'}), 500
    finally:
        conn.close()


@app.route('/api/ready/<instance_id>', methods=['GET'])
def api_ready(instance_id):
    """Proxy the agent-manager readiness probe so live agent tiles can spin
    until the inner app is actually serving (rc6.7 #91 pattern reused)."""
    user = _get_user()
    if not user['username']:
        return jsonify({'ready': False}), 403
    try:
        r = requests.get(
            f'{AGENT_MANAGER_URL}/api/ready/{instance_id}',
            headers={
                'X-Authentik-Username': user['username'],
                'X-Authentik-Uid': user['uid'],
                'X-Authentik-Email': user['email'],
                'X-Authentik-Groups': '|'.join(user['groups']),
            },
            timeout=3,
        )
        return (r.text, r.status_code, {'Content-Type': 'application/json'})
    except Exception:
        return jsonify({'ready': False}), 200


# ── #299 native "My Profile" dialog API ─────────────────────────────────────

@app.route('/api/portal/profile', methods=['GET'])
def api_get_profile():
    """Return the caller's own saved display name + avatar (owner-scoped:
    identity comes strictly from the forward-auth headers, never a
    client-supplied user id — same pattern as every other /api/* route in
    this module)."""
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    profile = _get_user_profile(user['user_slug'])
    return jsonify({
        'display_name': profile.get('display_name') or '',
        'avatar': profile.get('avatar_data_uri') or '',
    })


@app.route('/api/portal/profile', methods=['POST'])
def api_save_profile():
    """Save the caller's own display name and/or avatar.

    Both fields are optional and independent: a body key that is ABSENT
    leaves that field untouched; `display_name: ""` or `avatar: ""` clears
    it. A present-but-invalid `avatar` (malformed data URI, oversize, or not
    a recognised image format after magic-byte sniffing) is rejected with
    400 and nothing is written — never a silent truncate/drop.
    """
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    body = request.get_json(silent=True) or {}
    ok = True

    if 'display_name' in body:
        display_name = (body.get('display_name') or '').strip()
        if len(display_name) > MAX_DISPLAY_NAME_LEN:
            return jsonify({
                'error': f'display name too long (max {MAX_DISPLAY_NAME_LEN} characters)'
            }), 400
        ok = _set_display_name(user['user_slug'], display_name or None) and ok

    if 'avatar' in body:
        avatar_raw = body.get('avatar') or ''
        if avatar_raw == '':
            ok = _set_avatar(user['user_slug'], None) and ok
        else:
            normalized = _validate_avatar_data_uri(avatar_raw)
            if normalized is None:
                return jsonify({
                    'error': f'invalid avatar — must be a PNG/JPEG/GIF/WEBP image '
                             f'no larger than {MAX_AVATAR_BYTES // 1024} KB'
                }), 400
            ok = _set_avatar(user['user_slug'], normalized) and ok

    return jsonify({'ok': ok})


# ── #185 razzfazz-shortcuts admin CRUD ───────────────────────────────────────
# P1 is redirect-only; the in-portal kinds (dify_workflow/owui_prompt/
# agent_message/cloud_api) are rejected here and 501'd on /run until P2/P3.
_P1_KINDS = shortcuts._REDIRECT_KINDS


@app.route('/admin/shortcuts')
def admin_shortcuts():
    """Admin-only CRUD UI for shortcuts. Super-Admin gate; the source-IP anchor
    (before_request) already blocked any non-Caddy caller."""
    user = _get_user()
    if not user['is_admin']:
        return ('Forbidden', 403)
    return render_template('admin_shortcuts.html', user=user,
                            templates=scenario_templates.SCENARIO_TEMPLATES)


def _may_author_shortcuts(user):
    """May ``user`` reach the shortcut WRITE routes at all? (CFG-12)

    Admins always may. A member of ``SHORTCUT_AUTHORS_GROUP`` may too — that
    group is the whole point of the #248 P2 publish control, which was dead
    code while every write route 403'd on `is_admin` first. Authoring rights
    are not admin rights: the caller still passes
    `user_can_publish_company_wide()` for a company-visibility body, and a
    non-admin author is confined to rows they own (see `api_shortcut`).

    Residual (deliberate, flagged): the `/admin/shortcuts` editor PAGE and the
    Authentik group/member lookups it uses stay admin-only, so an authors-group
    member publishes through the API rather than the admin UI. Widening those
    would hand out the full cross-tenant listing and the Authentik group
    directory, which this finding does not ask for.
    """
    return bool(user.get('is_admin')) or shortcuts.user_can_publish_company_wide(user)


@app.route('/api/shortcuts', methods=['GET', 'POST'])
def api_shortcuts():
    """GET: list every shortcut (admin). POST: create one (admin + CSRF).

    #248 P2 — two write-side governance checks run on the PATCH BODY, before
    any DB connection is opened (fail fast, nothing to roll back):
      1. visibility="company" needs shortcuts.user_can_publish_company_wide()
         (admin or the SHORTCUT_AUTHORS_GROUP) — 403 otherwise.
      2. a SHARED config (company/group) must be portable — 422 otherwise.
    `private` is exempt from both.

    CFG-12: the POST gate used to be `if not user['is_admin']: 403`, which
    ran BEFORE check 1 — and `user_can_publish_company_wide()` returns True
    unconditionally for an admin. So the check could never fail for any
    caller that reached it, and the SHORTCUT_AUTHORS_GROUP branch (the
    stated point of the control) was unreachable in production: the guard
    tests only passed because they monkeypatched the predicate to False
    while sending admin headers, a state the real app cannot produce. The
    POST gate is now `admin OR authors-group`; GET (the full cross-tenant
    listing) stays admin-only.
    """
    user = _get_user()
    if request.method == 'GET':
        if not user['is_admin']:
            return jsonify({'error': 'forbidden'}), 403
        conn = _db_conn()
        if not conn:
            return jsonify({'error': 'db unavailable'}), 503
        try:
            return jsonify(shortcuts.list_all(conn))
        finally:
            conn.close()
    if not _may_author_shortcuts(user):
        return jsonify({'error': 'forbidden'}), 403
    # POST — state-changing. The global CSRF hook (#388) already verified the
    # token; this explicit call is idempotent and keeps the handler protected
    # on its own terms (CFG-36: the old "global CSRF is intentionally off"
    # comment here described a state that #388 ended).
    verify_csrf_token()
    b = request.get_json(silent=True) or {}
    if not b.get('title') or not b.get('kind'):
        return jsonify({'error': 'title and kind are required'}), 400
    if b.get('kind') not in _P1_KINDS:
        return jsonify({'error': 'unsupported kind in P1'}), 400
    vis = b.get('visibility') or 'company'
    if vis == 'company' and not shortcuts.user_can_publish_company_wide(user):
        return jsonify({'error': 'only an admin or a shortcut-authors-group '
                                  'member may publish a company-wide shortcut'}), 403
    if vis in ('company', 'group'):
        ok, reason = shortcuts.shared_config_is_portable(b['kind'], b.get('config') or {})
        if not ok:
            return jsonify({'error': reason}), 422
    # #248 App Builder — icon picker's Custom-upload tab. Same shape/backstop
    # as the #299 avatar upload: size-capped + magic-byte sniffed server-side,
    # never the client-claimed mime. Also validates the curated:<name>
    # allowlist. Runs BEFORE any DB connection, same fail-fast pattern as the
    # two governance checks above.
    icon_ok, icon_norm, icon_err = shortcuts.validate_icon(b.get('icon', '✨'))
    if not icon_ok:
        return jsonify({'error': icon_err}), 400
    conn = _db_conn()
    if not conn:
        return jsonify({'error': 'db unavailable'}), 503
    try:
        row = shortcuts.create(
            conn, title=b['title'], kind=b['kind'], created_by=user['username'],
            description=b.get('description', ''), icon=icon_norm,
            category=b.get('category', '1 Workspace'),
            sort_order=int(b.get('sort_order', 100)),
            allowed_groups=b.get('allowed_groups') or [],
            config=b.get('config') or {},
            # #248 S1: admin may author any visibility; a private one is
            # owned by the named user (default: the authoring admin).
            # CFG-12: a NON-admin author may not name someone else as owner,
            # and their rows are always owner-stamped so the edit/delete
            # gate below can scope them to their own work.
            visibility=vis,
            owner_username=(
                user['username'] if not user['is_admin']
                else (b.get('owner_username')
                      or (user['username'] if (vis == 'private') else ''))))
        return jsonify(row)
    finally:
        conn.close()


@app.route('/api/shortcuts/groups', methods=['GET'])
def api_shortcut_groups():
    """Authentik group names for the admin access multi-select (admin;
    read-only, so the global CSRF hook does not apply — #388 verifies
    state-changing methods only). Static path; Werkzeug routes it ahead of
    /<sid>."""
    user = _get_user()
    if not user['is_admin']:
        return jsonify({'error': 'forbidden'}), 403
    try:
        return jsonify({'groups': shortcuts.fetch_groups()})
    except Exception as e:  # noqa: BLE001 — degrade to empty, never 500 the editor
        logger.warning(f"Authentik group fetch failed: {e}")
        return jsonify({'groups': [], 'error': 'authentik unavailable'}), 200


@app.route('/api/shortcuts/members', methods=['GET'])
def api_shortcut_members():
    """Resolved effective members of the selected groups (admin; read-only,
    so the global CSRF hook does not apply — see api_shortcut_groups).
    `groups` query is pipe-separated, matching the X-Authentik-Groups format."""
    user = _get_user()
    if not user['is_admin']:
        return jsonify({'error': 'forbidden'}), 403
    raw = request.args.get('groups', '')
    names = [g for g in raw.split('|') if g]
    try:
        return jsonify({'members': shortcuts.resolve_members(names)})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Authentik member resolve failed: {e}")
        return jsonify({'members': [], 'error': 'authentik unavailable'}), 200


@app.route('/api/shortcuts/who-can-use', methods=['GET'])
def api_shortcut_who_can_use():
    """#248 App Builder — Step 4 "who can use it" preview: resolves the
    selected Authentik groups (pipe-separated `groups` query, same format as
    /api/shortcuts/members) to a member count + a few initials for the
    avatar row. `any`/`all` entries are ignored here (that case is rendered
    entirely client-side as "everyone", same as the pre-existing
    refreshMembers() panel).

    Same admin gate + degrade-gracefully contract as the sibling
    /api/shortcuts/groups and /api/shortcuts/members endpoints: NEVER a 500
    — Authentik being unreachable must not break the editor. The caller
    hides the count/avatar row when `available` is false rather than
    showing a misleading zero.
    """
    user = _get_user()
    if not user['is_admin']:
        return jsonify({'error': 'forbidden'}), 403
    raw = request.args.get('groups', '')
    names = [g for g in raw.split('|') if g and g != 'any']
    if not names:
        return jsonify({'available': True, 'count': 0, 'initials': []})
    try:
        members = shortcuts.resolve_members(names)
        return jsonify({'available': True, 'count': len(members),
                         'initials': shortcuts.member_initials(members)})
    except Exception as e:  # noqa: BLE001 — degrade to unavailable, never 500
        logger.warning(f"who-can-use resolve failed: {e}")
        return jsonify({'available': False, 'count': None, 'initials': []}), 200


@app.route('/api/owui/models', methods=['GET'])
def api_owui_models():
    """#227 point 6 — normalized OWUI model list for the shortcuts editor's
    model picker (`owui_persona`/`owui_app` kinds). Admin-gated exactly like
    `/api/shortcuts/groups` — this is admin curation tooling, not a
    user-facing surface.

    Degrades to `{'models': [], 'available': False}` — NEVER a 500 — when
    OWUI is unreachable, answers non-200, or no OWUI_API_KEY is configured;
    the editor falls back to a free-text model-id box in that case. The
    <select> widget itself and live per-user OWUI-token resolution belong to
    the Start-Portal redesign (#299)."""
    user = _get_user()
    if not user['is_admin']:
        return jsonify({'error': 'forbidden'}), 403
    try:
        return jsonify({'models': shortcuts.fetch_owui_models(), 'available': True})
    except Exception as e:  # noqa: BLE001 — degrade to empty, never 500 the editor
        logger.warning(f"OWUI model fetch failed: {e}")
        # Match the sibling degrade handlers (groups/members): a STATIC reason
        # to the caller, the exception only in the server log — so a
        # connection failure never surfaces the internal docker DNS name
        # (openwebui:8080) in the JSON, even to an admin (agent-seqis #839 nit).
        return jsonify({'models': [], 'available': False, 'error': 'owui unavailable'}), 200


@app.route('/api/shortcuts/<sid>', methods=['POST', 'DELETE'])
def api_shortcut(sid):
    """POST: update (admin + CSRF). DELETE: remove (admin + CSRF).

    #248 P2 — the same two write-side governance checks as the create route
    (see api_shortcuts()), but evaluated against the EFFECTIVE end state
    (existing row merged with the patch) — a patch that OMITS `visibility`
    keeps the current one, and "keeping" company still needs authorization;
    #679 review already established this merge pattern for shortcuts.update()
    itself, this mirrors it for the two new checks.

    CFG-12: an authors-group member may edit and delete THEIR OWN rows (the
    ones `api_shortcuts` POST owner-stamped for them); everything else stays
    admin-only, so publishing rights never become editing rights over other
    people's shortcuts.
    """
    user = _get_user()
    if not _may_author_shortcuts(user):
        return jsonify({'error': 'forbidden'}), 403
    verify_csrf_token()
    conn = _db_conn()
    if not conn:
        return jsonify({'error': 'db unavailable'}), 503
    try:
        current = shortcuts.get(conn, sid) or {}
        if not user['is_admin'] and (
                (current.get('owner_username') or '') != user['username']):
            return jsonify({'error': 'forbidden'}), 403
        if request.method == 'DELETE':
            return jsonify({'ok': shortcuts.delete(conn, sid)})
        b = request.get_json(silent=True) or {}
        if 'kind' in b and b['kind'] not in _P1_KINDS:
            return jsonify({'error': 'unsupported kind in P1'}), 400
        vis = b.get('visibility', current.get('visibility') or 'company')
        if vis == 'company' and not shortcuts.user_can_publish_company_wide(user):
            return jsonify({'error': 'only an admin or a shortcut-authors-group '
                                      'member may publish a company-wide shortcut'}), 403
        if vis in ('company', 'group'):
            kind = b.get('kind', current.get('kind'))
            config = b.get('config', current.get('config') or {})
            ok, reason = shortcuts.shared_config_is_portable(kind, config)
            if not ok:
                return jsonify({'error': reason}), 422
        # #248 App Builder — only validate when the patch actually touches
        # `icon` (a partial update omitting it must not re-validate/rewrite
        # the existing stored value).
        if 'icon' in b:
            icon_ok, icon_norm, icon_err = shortcuts.validate_icon(b['icon'])
            if not icon_ok:
                return jsonify({'error': icon_err}), 400
            b['icon'] = icon_norm
        return jsonify(shortcuts.update(conn, sid, **b))
    finally:
        conn.close()




def _shortcut_ctx(user, input_text=''):
    """The whitelist-render context for a shortcut (#185) — built ONLY from the
    forward-auth identity plus the caller's one input box, never from any
    other client-supplied value. ``language``/``tz`` (#1192) feed OWUI's
    ``{{USER_LANGUAGE}}``/``{{CURRENT_TIMEZONE}}`` prompt variables when the
    portal creates the chat server-side. rzfz review #1208 F2: the language
    used to come from the Accept-Language header — a client-supplied value the
    docstring above promised never to use. Dropped: OWUI falls back to its own
    default, and nothing in the prompt whitelist ever read it."""
    return {
        'name': user['username'], 'email': user['email'],
        'username': user['username'], 'groups': user['groups'],
        'date': date.today().isoformat(),
        'domain': os.environ.get('MAIN_DOMAIN', ''), 'input': input_text or '',
        'language': 'en-US',
        'tz': os.environ.get('TZ', '') or 'UTC',
    }


def _load_shortcut_for(user, sid):
    """Shared gate for the run/open routes: (shortcut, None) when ``sid`` is an
    enabled shortcut this user may use, else (None, <error response>). Access
    is re-checked from the LIVE forward-auth identity on every call."""
    conn = _db_conn()
    if not conn:
        return None, (jsonify({'error': 'db unavailable'}), 503)
    try:
        sc = shortcuts.get(conn, sid)
    finally:
        conn.close()
    if not sc or not sc.get('enabled'):
        return None, (jsonify({'error': 'not found'}), 404)
    if not shortcuts.user_can_use(sc, user):
        return None, (jsonify({'error': 'forbidden'}), 403)
    return sc, None


def _shortcut_target(sc, user, ctx):
    """Resolve where a redirect shortcut sends ``user``.

    #1192: a prompt-bearing ``owui_app`` is created server-side through
    Open WebUI's API (``shortcuts.start_owui_app_chat``) and the target is
    the deterministic ``/c/<chat_id>`` — no ``?q=`` auto-submit, no
    double-mount race, one chat per click. When that path is unavailable
    (no shared secret, OWUI/DB unreachable, user has no OWUI account yet,
    OWUI refused) the legacy deep link is returned instead — degrade, not
    fail. Every other kind is the plain ``build_redirect_url`` as before.
    Raises ``ValueError``/``KeyError`` for a broken config like the builder.
    """
    kind = sc['kind']
    config = sc.get('config') or {}
    if kind == 'owui_app' and shortcuts.owui_app_uses_server_chat(config):
        url = shortcuts.start_owui_app_chat(config, ctx, shortcuts.get_owui_client())
        if url:
            return url
        logger.warning(f"shortcut {sc.get('id')}: server-side OWUI chat unavailable, "
                       f"falling back to the ?q= deep link (#1192)")
    return shortcuts.build_redirect_url(kind, config, ctx)


_NAV_ERROR_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>razzfazz.ai — shortcut</title>
<style>body{{font-family:system-ui,sans-serif;background:#f6f5f4;color:#222;display:grid;place-items:center;height:100vh;margin:0}}
.card{{background:#fff;border-left:4px solid #b3202c;padding:24px 28px;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:520px}}
a{{color:#b3202c}}</style></head><body><div class="card"><h2>{title}</h2><p>{detail}</p>
<p><a href="/">← Back to the start portal</a></p></div></body></html>"""


def _nav_error(status, title, detail):
    """rzfz review #1208 F3: /shortcuts/<sid>/open is a BROWSER NAVIGATION — an
    error must be a page with a way back, not a raw JSON blob in a new tab."""
    from markupsafe import escape
    return _NAV_ERROR_HTML.format(title=escape(title), detail=escape(detail)), status, {'Content-Type': 'text/html; charset=utf-8'}


def _is_first_party_navigation(req):
    """rzfz review #1208 F1: this GET creates a chat and fires a completion in
    the user's Open WebUI — it is state-changing, but a GET is invisible to the
    house CSRF gate (razzfazz_common.csrf verifies POST/PUT/PATCH/DELETE only).
    Without this, a cross-site top-level navigation (Authentik's cookie is Lax →
    sent) or a browser LINK PREFETCH would create ghost chats — the #1192
    symptom by another road. Allow only a same-origin (or address-bar) document
    navigation; refuse prefetch. Fetch-Metadata is sent by every current
    browser; a client that sends none (curl, very old UA) is refused too —
    the tile href is the only legitimate caller."""
    return _navigation_verdict(req) == 'ok'


def _navigation_verdict(req):
    """'ok' | 'same-site' | 'refuse'. rzfz re-review #1208 F-neu 1: after an
    SSO round-trip (portal session expired → forward_auth 302 → auth.<dom> →
    back to this route) the browser downgrades Sec-Fetch-Site over the WHOLE
    redirect chain to ``same-site``. That must NOT simply be allowed —
    ``*.<AGENTS_DOMAIN>`` coding-agent sandboxes are same-site too — so it is
    its own verdict: the route answers a signed-in same-site navigation with a
    one-click Continue page (same-origin click → 'ok') instead of a 403 dead
    end, and never creates anything on that answer."""
    site = (req.headers.get('Sec-Fetch-Site') or '').lower()
    mode = (req.headers.get('Sec-Fetch-Mode') or '').lower()
    dest = (req.headers.get('Sec-Fetch-Dest') or '').lower()
    purpose = (req.headers.get('Sec-Purpose') or req.headers.get('Purpose') or '').lower()
    if 'prefetch' in purpose or 'prerender' in purpose:
        return 'refuse'
    if mode != 'navigate':
        return 'refuse'
    if dest and dest != 'document':
        return 'refuse'
    if site in ('same-origin', 'none'):
        return 'ok'
    if site == 'same-site':
        return 'same-site'
    return 'refuse'


_NAV_CONTINUE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Continue to shortcut</title>
<style>body{{font-family:system-ui,sans-serif;background:#f6f6f6;color:#222;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0}}
.card{{background:#fff;border-radius:12px;padding:28px 32px;box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:520px}}
a.btn{{display:inline-block;margin-top:14px;padding:10px 18px;border-radius:8px;background:#b3202c;color:#fff;
text-decoration:none;font-weight:600}}</style></head><body><div class="card">
<h1 style="margin:0 0 8px;font-size:1.2rem">Your sign-in was refreshed</h1>
<p>Continue to open the shortcut. (Nothing happens until you click.)</p>
<a class="btn" href="/shortcuts/{sid}/open">Continue</a>
</div></body></html>"""


def _nav_continue(sid):
    """200 interstitial for a signed-in same-site navigation (post-SSO). The
    Continue link is a same-origin click, which the gate accepts; the page
    itself creates nothing, so a same-site page cannot drive it without the
    user's click."""
    from urllib.parse import quote
    return Response(_NAV_CONTINUE_HTML.format(sid=quote(str(sid), safe='')), status=200,
                    mimetype='text/html', headers={'Cache-Control': 'no-store'})


@app.route('/shortcuts/<sid>/open', methods=['GET'])
def shortcut_open(sid):
    """#1192 — the href a prompt-bearing ``owui_app`` TILE carries
    (``shortcuts.tile_url``). Resolves the target at click time — creating
    the OWUI chat server-side — and 302s there. Same identity/enabled/access
    gates as ``/run``. Deliberately takes NO query input: a GET carries no
    CSRF token, so accepting prompt text here would let a crafted link inject
    content into the victim's chat; ``{{input}}`` renders empty on this path
    (exactly what the pre-#1192 tile link did). State-changing GET → gated to
    first-party document navigations (``_is_first_party_navigation``); the
    deliberate exception is registered in tests/api/…/test_csrf.py (#388)."""
    verdict = _navigation_verdict(request)
    if verdict == 'refuse':
        return _nav_error(403, 'Shortcut not opened',
                          'This shortcut only opens from a click inside the start portal '
                          '(cross-site navigations and link prefetch are refused).')
    user = _get_user()
    if not user['username']:
        return _nav_error(403, 'Not signed in', 'Sign in to the start portal first.')
    if verdict == 'same-site':
        # post-SSO redirect chain (or a same-site page) — never act, offer a click
        return _nav_continue(sid)
    sc, err = _load_shortcut_for(user, sid)
    if err:
        status = err[1] if isinstance(err, tuple) and len(err) > 1 else 403
        return _nav_error(status, 'Shortcut unavailable',
                          'This shortcut is disabled, missing, or not allowed for your group.')
    if sc['kind'] not in shortcuts._REDIRECT_KINDS:
        return _nav_error(501, 'Not available', 'In-portal execution is not available for this shortcut kind yet.')
    try:
        url = _shortcut_target(sc, user, _shortcut_ctx(user, ''))
    except (ValueError, KeyError) as e:
        return _nav_error(400, 'Shortcut misconfigured', f'bad shortcut config: {e}')
    return redirect(url, code=302)


@app.route('/api/shortcuts/<sid>/run', methods=['POST'])
def api_shortcut_run(sid):
    """Run a shortcut. P1 = redirect kinds only: returns {"url": <target>} for
    the tile JS to open. Access is re-checked server-side from the live
    forward-auth identity on EVERY run (never a client-supplied value). Also
    powers the admin Test button (an admin passes user_can_use by bypass) —
    since #1192 a prompt-bearing owui_app Test really creates the chat in the
    admin's OWUI and links to it, i.e. it exercises the actual click path.
    In-portal kinds are 501 until P2/P3."""
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    verify_csrf_token()
    sc, err = _load_shortcut_for(user, sid)
    if err:
        return err
    b = request.get_json(silent=True) or {}
    ctx = _shortcut_ctx(user, b.get('input', ''))
    if sc['kind'] in shortcuts._REDIRECT_KINDS:
        try:
            url = _shortcut_target(sc, user, ctx)
        except (ValueError, KeyError) as e:
            return jsonify({'error': f'bad shortcut config: {e}'}), 400
        return jsonify({'url': url})
    # In-portal execution (dify_workflow/owui_prompt/agent_message/cloud_api)
    # is P2/P3; the admin form already rejects these kinds on create.
    return jsonify({'error': 'in-portal execution not available in P1'}), 501


# Initialize prefs table at boot — best-effort.
with app.app_context():
    _ensure_prefs_table()
    # #299 — native "My Profile" store (display name + avatar).
    _ensure_profile_table()
    # #185 — auto-migrate the shortcuts table the same best-effort way.
    _sc_boot_conn = _db_conn()
    if _sc_boot_conn:
        try:
            shortcuts.ensure_shortcuts_table(_sc_boot_conn)
        except Exception as e:  # noqa: BLE001 — never let a boot migration crash startup
            logger.error(f"Could not ensure shortcuts table: {e}")
        finally:
            _sc_boot_conn.close()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
