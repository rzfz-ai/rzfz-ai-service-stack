# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""razzfazz.ai Start Portal — M028.

Renders the post-login launcher tile grid for the calling user.
Visibility: (user has required group OR user is admin) AND module is active.
Live tiles: per-user agent instances from agent-manager /api/instances.
Prefs: pinned + sort_order persisted per-user in the agent-manager Postgres.
"""

import json
import logging
import os
from datetime import date, datetime, timezone

import psycopg2
import psycopg2.extras
import requests
import yaml
from flask import current_app, jsonify, render_template, request, redirect, url_for

from razzfazz_common.flask_app import create_base_app

# razzfazz-shortcuts (#185) — admin-curated redirect tiles. Pure/DB logic lives
# in shortcuts.py; this module wires table-init, tile-merge, and the routes.
import shortcuts

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

ADMIN_GROUP = 'authentik Admins'
SUPER_ADMIN_GROUP = 'razzfazz.ai Super Admins'
COMPOSE_PROFILES_PATH = os.environ.get('STACK_ENV_PATH', '/stack/.env')
MANIFEST_PATH = os.environ.get('MANIFEST_PATH', '/app/manifest.yaml')
AGENT_MANAGER_URL = os.environ.get('AGENT_MANAGER_URL', 'http://agent-manager:5000')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

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


def _agent_to_tile(inst, agents_domain):
    """Convert an agent-manager instance dict to a tile dict.

    S11 #6: live agent tiles emit into "2 AI Assistants" alongside the static
    `my-agents` tile. The previous "0 My Running Agents" section is gone —
    operator wanted personal agents listed inside AI Assistants.
    """
    agent_type = inst['agent_type']
    user_slug = inst['user_slug']
    icon_map = {
        'hermes': 'hermes', 'moltis': 'moltis', 'paperclip': 'paperclip',
        'openhands': 'openhands', 'coding-tools': 'coding',
    }
    icon = icon_map.get(agent_type, 'home')
    name_map = {
        'hermes': 'My Hermes', 'moltis': 'My Moltis',
        'paperclip': 'My Paperclip', 'openhands': 'My OpenHands',
        'coding-tools': 'My Coding Tools',
    }
    name = name_map.get(agent_type, inst.get('type_display_name', agent_type))
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
        'category': '2 AI Assistants',
        'icon': icon,
        'url': url,
        'description': desc_map.get(agent_type, 'Running personal agent instance.'),
        'instance_id': inst['id'],
        'is_live_agent': True,
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    user = _get_user()
    if not user['username']:
        # Forward-auth misconfigured or test access — show a minimal page.
        return render_template('index.html', user=user, sections=[],
                              favorites=[], live_agent_count=0)

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
    sc_ctx = {
        'name': user['username'], 'email': user['email'],
        'username': user['username'], 'groups': user['groups'],
        'date': date.today().isoformat(),
        'domain': os.environ.get('MAIN_DOMAIN', ''), 'input': '',
    }
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
    import re as _re
    def _display_name(cat):
        return _re.sub(r'^\d+\s+', '', cat)

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

    return render_template('index.html', user=user, sections=sections,
                          favorites=favorites,
                          all_categories=all_categories,
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
    return render_template('admin_shortcuts.html', user=user)


@app.route('/api/shortcuts', methods=['GET', 'POST'])
def api_shortcuts():
    """GET: list every shortcut (admin). POST: create one (admin + CSRF)."""
    user = _get_user()
    if not user['is_admin']:
        return jsonify({'error': 'forbidden'}), 403
    conn = _db_conn()
    if not conn:
        return jsonify({'error': 'db unavailable'}), 503
    try:
        if request.method == 'GET':
            return jsonify(shortcuts.list_all(conn))
        # POST — state-changing: verify CSRF (global CSRF is intentionally off).
        verify_csrf_token()
        b = request.get_json(silent=True) or {}
        if not b.get('title') or not b.get('kind'):
            return jsonify({'error': 'title and kind are required'}), 400
        if b.get('kind') not in _P1_KINDS:
            return jsonify({'error': 'unsupported kind in P1'}), 400
        row = shortcuts.create(
            conn, title=b['title'], kind=b['kind'], created_by=user['username'],
            description=b.get('description', ''), icon=b.get('icon', '✨'),
            category=b.get('category', 'Shortcuts'),
            sort_order=int(b.get('sort_order', 100)),
            allowed_groups=b.get('allowed_groups') or [],
            config=b.get('config') or {})
        return jsonify(row)
    finally:
        conn.close()


@app.route('/api/shortcuts/groups', methods=['GET'])
def api_shortcut_groups():
    """Authentik group names for the admin access multi-select (admin, no CSRF
    — read-only). Static path; Werkzeug routes it ahead of /<sid>."""
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
    """Resolved effective members of the selected groups (admin, no CSRF).
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


@app.route('/api/shortcuts/<sid>', methods=['POST', 'DELETE'])
def api_shortcut(sid):
    """POST: update (admin + CSRF). DELETE: remove (admin + CSRF)."""
    user = _get_user()
    if not user['is_admin']:
        return jsonify({'error': 'forbidden'}), 403
    verify_csrf_token()
    conn = _db_conn()
    if not conn:
        return jsonify({'error': 'db unavailable'}), 503
    try:
        if request.method == 'DELETE':
            return jsonify({'ok': shortcuts.delete(conn, sid)})
        b = request.get_json(silent=True) or {}
        if 'kind' in b and b['kind'] not in _P1_KINDS:
            return jsonify({'error': 'unsupported kind in P1'}), 400
        return jsonify(shortcuts.update(conn, sid, **b))
    finally:
        conn.close()


@app.route('/api/shortcuts/<sid>/run', methods=['POST'])
def api_shortcut_run(sid):
    """Run a shortcut. P1 = redirect kinds only: returns {"url": <target>} for
    the tile JS to open. Access is re-checked server-side from the live
    forward-auth identity on EVERY run (never a client-supplied value). Also
    powers the admin Test button (an admin passes user_can_use by bypass).
    In-portal kinds are 501 until P2/P3."""
    user = _get_user()
    if not user['username']:
        return jsonify({'error': 'Unauthorized'}), 403
    verify_csrf_token()
    conn = _db_conn()
    if not conn:
        return jsonify({'error': 'db unavailable'}), 503
    try:
        sc = shortcuts.get(conn, sid)
    finally:
        conn.close()
    if not sc or not sc.get('enabled'):
        return jsonify({'error': 'not found'}), 404
    if not shortcuts.user_can_use(sc, user):
        return jsonify({'error': 'forbidden'}), 403
    b = request.get_json(silent=True) or {}
    ctx = {
        'name': user['username'], 'email': user['email'],
        'username': user['username'], 'groups': user['groups'],
        'date': date.today().isoformat(),
        'domain': os.environ.get('MAIN_DOMAIN', ''), 'input': b.get('input', ''),
    }
    if sc['kind'] in shortcuts._REDIRECT_KINDS:
        try:
            url = shortcuts.build_redirect_url(sc['kind'], sc.get('config') or {}, ctx)
        except (ValueError, KeyError) as e:
            return jsonify({'error': f'bad shortcut config: {e}'}), 400
        return jsonify({'url': url})
    # In-portal execution (dify_workflow/owui_prompt/agent_message/cloud_api)
    # is P2/P3; the admin form already rejects these kinds on create.
    return jsonify({'error': 'in-portal execution not available in P1'}), 501


# Initialize prefs table at boot — best-effort.
with app.app_context():
    _ensure_prefs_table()
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
