# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""
razzfazz.ai Help Center — Local Documentation Hub

Serves cached offline copies of official app documentation,
filtered by user's Authentik groups. Includes admin cache management.
"""

from flask import g, render_template, request, jsonify, send_from_directory, abort, Response
import os
import json
import re
import threading

from razzfazz_common.flask_app import create_base_app
from razzfazz_common.auth import SUPER_ADMINS_GROUP

from cache_manager import CacheManager, OWN_DOCS_DIR

MANIFEST_PATH = os.environ.get('RAZZFAZZ_VERSIONS_MANIFEST', '/app/manifests/versions.json')


def _load_manifest() -> dict:
    try:
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# Loaded at import time — manifest file is read-only and the help container
# restarts to pick up bumps (same deploy cadence as everything else).
_MANIFEST = _load_manifest()
_VERSION_RE = re.compile(r'(v?\d+\.\d+(?:\.\d+)?[\w.+-]*)')


def resolve_version(manifest_key):
    """Look up a display version for a mirror_config entry.

    manifest_key is a section-qualified ref (e.g. 'images.dify-api',
    'hardcoded.synapse', 'custom_built.razzfazz-cognee'). For custom_built
    entries the free-form upstream_pin is parsed for a version-like token.
    Returns None if the manifest isn't mounted or the key isn't found.
    """
    if not manifest_key or '.' not in manifest_key:
        return None
    section, name = manifest_key.split('.', 1)
    entry = _MANIFEST.get(section, {}).get(name)
    if not entry:
        return None
    if section in ('images', 'hardcoded'):
        return entry.get('current')
    if section == 'custom_built':
        pin = entry.get('upstream_pin', '')
        match = _VERSION_RE.search(pin)
        return match.group(1) if match else (pin or None)
    return None

# M026 S05 #2: shared base app provides /healthz, session config, and the
# {razzfazz_version, main_domain, brand_color} template context. Auth is
# enforced by Caddy `forward_auth` upstream of this container (see
# core/Caddy/Caddyfile §Help), so require_auth=False here matches the prior
# behaviour of the locally-defined Flask app. The before_request hook in
# create_base_app populates `flask.g.user` from the X-Authentik-* headers,
# so the helpers below read from `g.user` instead of parsing headers again.
app = create_base_app(__name__, service_name='razzfazz-help', require_auth=False)

cm = CacheManager()


# rc6.7 #14: auto-warm uncached apps on container start.
# Pre-rc6.7 the cache populated only on admin click in the help admin page,
# so apps added to mirror_config.json mid-cycle (e.g. OpenLIT, Crawl4AI in
# the 2026.05 cycle) silently appeared as "Not cached" until someone went
# to /admin and clicked Refresh. After every upgrade that added a new
# module, an operator manual-step was implicit and almost always missed.
#
# Fix: spawn a daemon thread on first import that — after a startup delay —
# walks mirror_config.json and triggers `mirror_docs(app_id)` for any app
# whose cache directory is empty. Idempotent: already-cached apps stay
# untouched (mirror_docs itself is a no-op when up-to-date). The 30-second
# delay lets the host's network/Authentik settle before the first wget
# burst, especially on first-boot of a freshly-installed box.
import time

def _autowarm_uncached_caches():
    """Background fetch of uncached app docs. Runs once per container life."""
    try:
        time.sleep(30)
        config = cm.get_config()
        for app_entry in config.get('apps', []):
            app_id = app_entry.get('id')
            if not app_id:
                continue
            # #149: box-local module pages are never mirrored — skip them so the
            # auto-warm loop doesn't keep retrying a wget for docs we ship locally.
            if app_entry.get('local_doc'):
                continue
            try:
                status = cm.get_app_cache_status(app_id)
            except Exception:
                continue
            # Re-mirror when the cache is empty OR the last mirror FAILED. A
            # failed mirror (wget exit 4/6, or exit 8 that failed the sanity
            # gate) leaves partial files on disk so `cached` is True — but it
            # must not be left cached-as-good; retry it on the next container
            # start. An accepted-partial mirror (exit 8 that passed the gate)
            # carries no `error`, so it is left in place (it is usable).
            if status.get('cached') and not status.get('error'):
                continue
            try:
                # mirror_docs is the same path the admin Refresh button uses.
                # On internet failure / 4xx it logs + bails; we don't surface
                # to UI here (the admin page already shows the error from the
                # most recent attempt).
                cm.mirror_docs(app_id)
            except Exception as e:
                # Don't crash the auto-warm thread on a single failure.
                print(f'[autowarm] mirror_docs({app_id}) failed: {e}', flush=True)
    except Exception as e:
        print(f'[autowarm] thread aborted: {e}', flush=True)


# Spawn on import, not in __main__, so gunicorn workers also kick it off.
# Daemon=True so the thread doesn't block container shutdown.
threading.Thread(target=_autowarm_uncached_caches, daemon=True).start()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Authentik header parsing + g.user population is provided by
# razzfazz_common.flask_app.create_base_app (before_request hook). These
# thin accessors keep the existing call-sites readable.

def get_user_groups() -> list[str]:
    return g.user.get('groups', []) if hasattr(g, 'user') and g.user else []


def get_username() -> str:
    if hasattr(g, 'user') and g.user and g.user.get('username'):
        return g.user['username']
    return 'anonymous'


def is_super_admin() -> bool:
    return SUPER_ADMINS_GROUP in get_user_groups()


def _active_profiles() -> set[str]:
    """Active Docker Compose profiles from COMPOSE_PROFILES (env_file: ../.env).

    Used to scope profile-specific doc entries — chiefly the two GPUStack doc
    variants (v0.7 docs for the llm-legacy/llm-cpu profiles, v2.1 docs for the
    llm profile). Returns an empty set when unknown, which the caller treats
    as fail-open (show all).
    """
    raw = os.environ.get('COMPOSE_PROFILES', '') or ''
    return {p.strip() for p in raw.split(',') if p.strip()}


def get_visible_apps() -> list[dict]:
    """Return the list of doc apps this user is allowed to see."""
    groups = get_user_groups()
    config = cm.get_config()
    active = _active_profiles()
    visible = []

    for app_entry in config['apps']:
        required = app_entry.get('required_groups', [])
        # Empty required_groups means all authenticated users can see it
        if not (not required or is_super_admin() or any(g in groups for g in required)):
            continue
        # Profile-scoped entries (e.g. the two GPUStack doc variants) only show
        # when their declared profile is active — otherwise an llm-legacy (v0.7.1)
        # box would surface the v2.1.x docs (and version tile) for a GPUStack it
        # isn't running (#171). Fail-open if COMPOSE_PROFILES is unknown.
        entry_profiles = app_entry.get('profiles')
        if entry_profiles and active and not (set(entry_profiles) & active):
            continue
        status = cm.get_app_cache_status(app_entry['id'])
        entry = {**app_entry, **status}
        entry['version'] = resolve_version(app_entry.get('manifest_key'))
        visible.append(entry)

    # Always include own docs — auto-discovered from the docs/enterprise tree
    # baked into the image at /app/own_docs (2026.07 docs rework). No manifest.
    for own_entry in cm.discover_own_docs():
        visible.append({**own_entry, 'cached': True, 'is_own': True})

    return visible


# ---------------------------------------------------------------------------
# Routes — User-Facing
# ---------------------------------------------------------------------------
# /healthz is mounted by razzfazz_common.health.get_health_blueprint via
# create_base_app() above — returns {"status": "ok", "service": "razzfazz-help"}.

@app.route('/')
def index():
    """Hub page: cards for each available documentation section."""
    apps = get_visible_apps()
    username = get_username()
    is_admin = is_super_admin()
    # Group the auto-discovered own docs ("razzfazz.ai Guides") by section,
    # preserving discover_own_docs()'s curated section order.
    #
    # Role-aware: the Guides are operating/administering the box — a STACK-ADMIN
    # job — so non-admins only see pages explicitly marked end-user-facing
    # (`<!-- audience: end-user -->`, e.g. the tutorials). A stack admin sees all.
    own_sections = []  # list[ {label, apps[]} ] in display order
    for a in apps:
        if not a.get('is_own'):
            continue
        if not is_admin and a.get('audience') != 'end-user':
            continue
        if own_sections and own_sections[-1]['section'] == a.get('section'):
            own_sections[-1]['apps'].append(a)
        else:
            own_sections.append({
                'section': a.get('section'),
                'label': a.get('section_label', 'Guides'),
                'apps': [a],
            })
    return render_template('index.html', apps=apps, own_sections=own_sections,
                           username=username, is_admin=is_admin)


def _resolve_mintlify_asset(cache_dir, filepath_clean, query_string=b''):
    """Resolve a Mintlify/Next.js asset (e.g. the cached Dify docs).

    Mintlify pages reference their CSS/JS with a page-path prefix
    (``.../en/use-dify/getting-started/mintlify-assets/...``) but wget cached
    the asset once at the cache root under ``mintlify-assets/`` — so the
    prefixed request 404s. wget also kept the ``?dpl=`` deploy-hash query in
    the saved filename, which Flask strips off the request path. Resolve from
    the cache root and match the query-suffixed filename. Returns the on-disk
    path or None.
    """
    if 'mintlify-assets/' not in filepath_clean:
        return None
    rel = 'mintlify-assets/' + filepath_clean.split('mintlify-assets/', 1)[1]
    base = os.path.realpath(os.path.join(cache_dir, rel))
    croot = os.path.realpath(cache_dir)
    if base != croot and not base.startswith(croot + os.sep):
        return None  # path-traversal guard
    if os.path.isfile(base):
        return base
    d, bn = os.path.dirname(base), os.path.basename(base)
    if os.path.isdir(d):
        if query_string:
            exact = os.path.join(d, bn + '?' + query_string.decode('utf-8', 'replace'))
            if os.path.isfile(exact):
                return exact
        for f in sorted(os.listdir(d)):
            if f.startswith(bn + '?'):
                return os.path.join(d, f)
    return None


@app.route('/docs/<app_id>/')
@app.route('/docs/<app_id>/<path:filepath>')
def view_docs(app_id, filepath=None):
    """Serve cached documentation — full original pages with link rewriting."""
    from flask import redirect
    # Check access
    groups = get_user_groups()
    app_entry = cm.get_app_entry(app_id)

    if not app_entry:
        abort(404)

    # Check group access (unless own docs or super admin)
    if not app_entry.get('is_own', False):
        required = app_entry.get('required_groups', [])
        if required and not is_super_admin() and not any(g in groups for g in required):
            abort(403)

    # Own docs are rendered from markdown — use viewer template
    if app_entry.get('is_own', False):
        # Role-aware access control AT THE SERVING ROUTE (not just the hub): the
        # Guides are a stack-admin job, so a non-admin must not be able to read an
        # admin-only Guide by guessing its /docs/<slug>/ URL. Fail closed —
        # anything not explicitly marked end-user is admin-only.
        if not is_super_admin() and app_entry.get('audience') != 'end-user':
            abort(403)
        content = cm.render_own_doc(app_id)
        if content is None:
            abort(404)
        return render_template('viewer.html',
                             content=content,
                             app_entry=app_entry,
                             is_admin=is_super_admin())

    # Box-local module page (#149): a curated own-doc replaces the upstream
    # mirror for modules whose SPA/CDN docs can't be reliably wget-mirrored
    # (dify, cognee, openhands, gotenberg, komodo, lightrag). Rendered from
    # core/help/module_docs via the same markdown pipeline as the own-docs.
    # Available to every authenticated user (same visibility as a mirror — no
    # audience gate). Any filepath under the prefix renders the same page.
    if app_entry.get('local_doc'):
        content = cm.render_local_doc(app_id)
        if content is None:
            abort(404)
        return render_template('viewer.html',
                             content=content,
                             app_entry=app_entry,
                             is_admin=is_super_admin())

    # Cached external docs
    cache_dir = cm.get_cache_path(app_id)
    if not cache_dir or not os.path.isdir(cache_dir):
        return render_template('viewer.html',
                             content='<div class="not-cached"><h2>Documentation not yet cached</h2>'
                                     '<p>An administrator needs to refresh the documentation cache.</p></div>',
                             app_entry=app_entry,
                             is_admin=is_super_admin())

    # If no filepath, redirect to the entry path (derived from mirror URL)
    if filepath is None:
        entry_path = cm.get_entry_path(app_id)
        if entry_path:
            # Trailing slash is required so that browsers resolve relative links
            # (e.g., "overview/") correctly.  All wget-mirrored content uses
            # relative paths from within the entry directory, so the browser's
            # base URL must *include* that directory as a "folder".
            return redirect(f'/docs/{app_id}/{entry_path}/')
        filepath = 'index.html'

    # Strip trailing slash for file resolution
    filepath_clean = filepath.rstrip('/')

    # Resolve the file in the cache
    full_path = os.path.realpath(os.path.join(cache_dir, filepath_clean))
    # Prevent path traversal — resolved path must stay within the cache dir
    if not full_path.startswith(os.path.realpath(cache_dir) + os.sep) and full_path != os.path.realpath(cache_dir):
        abort(403)

    # Resolution order:
    # 1. Exact match (file exists as-is)
    # 2. As directory with index.html inside
    # 3. With .html extension added (Docusaurus pattern: /docs/page → docs/page.html)
    if os.path.isfile(full_path):
        pass  # exact match
    elif os.path.isdir(full_path):
        idx = os.path.join(full_path, 'index.html')
        if os.path.isfile(idx):
            full_path = idx
        else:
            # directory exists but no index.html — try .html on the dir name
            html_alt = full_path.rstrip('/') + '.html'
            if os.path.isfile(html_alt):
                full_path = html_alt
            else:
                abort(404)
    elif os.path.isfile(full_path + '.html'):
        full_path = full_path + '.html'
    elif os.path.isfile(full_path + '/index.html'):
        full_path = full_path + '/index.html'
    else:
        # Mintlify/Next.js asset fallback (cached Dify docs): page-path-prefixed
        # asset refs + ?dpl= query kept in the wget filename → resolve from root.
        _mlf = _resolve_mintlify_asset(cache_dir, filepath_clean, request.query_string)
        if _mlf:
            full_path = _mlf
        else:
            abort(404)

    # For HTML files: inject attribution bar + rewrite links, serve FULL page
    if full_path.endswith(('.html', '.htm')):
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            html_content = f.read()

        modified = cm.inject_attribution(html_content, app_entry, app_id, filepath_clean)
        return Response(modified, mimetype='text/html')

    # For non-HTML assets (CSS, JS, images), serve directly
    directory = os.path.dirname(full_path)
    filename = os.path.basename(full_path)
    return send_from_directory(directory, filename)




@app.route('/docs-image/<path:imgpath>')
def docs_image(imgpath):
    """Serve relative image assets referenced by the own_docs markdown.

    Images live under an images/ directory in the own_docs tree — either the baked-in
    base (docs/community -> /app/own_docs/, currently image-less) or the runtime
    ENTERPRISE overlay (docs/enterprise -> /app/own_docs/enterprise, whose screenshots
    sit at /app/own_docs/enterprise/images/**; #125 P1). render_own_doc rewrites a
    doc's relative image links to /docs-image/<path-relative-to-own_docs-root>.
    Path-traversal-guarded to the own_docs root; scoped to paths under an images/ dir
    so raw markdown source is never served through this route.
    """
    # Scope to image assets only (base own_docs/images/ OR overlay
    # own_docs/enterprise/images/), never raw markdown source.
    if 'images' not in imgpath.split('/'):
        abort(404)
    base = os.path.realpath(OWN_DOCS_DIR)
    full = os.path.realpath(os.path.join(base, imgpath))
    if full != base and not full.startswith(base + os.sep):
        abort(403)
    if not os.path.isfile(full):
        abort(404)
    return send_from_directory(os.path.dirname(full), os.path.basename(full))


# ---------------------------------------------------------------------------
# Routes — Admin Cache Management
# ---------------------------------------------------------------------------

@app.route('/admin/cache')
def admin_cache():
    """Cache management page (Super Admins only)."""
    if not is_super_admin():
        abort(403)

    config = cm.get_config()
    statuses = []
    for app_entry in config['apps']:
        status = cm.get_app_cache_status(app_entry['id'])
        statuses.append({**app_entry, **status})

    active_jobs = cm.get_active_jobs()
    online = cm.check_internet()
    return render_template('admin.html',
                         apps=statuses,
                         active_jobs=active_jobs,
                         online=online,
                         is_admin=True)


@app.route('/api/refresh/<app_id>', methods=['POST'])
def api_refresh(app_id):
    """Trigger cache refresh for a single app."""
    if not is_super_admin():
        return jsonify({'status': 'error', 'message': 'Forbidden'}), 403

    if not cm.check_internet():
        return jsonify({'status': 'error', 'message': 'No internet connection available'}), 503

    app_entry = cm.get_app_entry(app_id)
    if not app_entry:
        return jsonify({'status': 'error', 'message': f'Unknown app: {app_id}'}), 404

    if cm.is_job_active(app_id):
        return jsonify({'status': 'error', 'message': f'{app_id} is already being refreshed'}), 409

    thread = threading.Thread(target=cm.mirror_docs, args=(app_id,), daemon=True)
    thread.start()

    return jsonify({'status': 'success', 'message': f'Refresh started for {app_entry["name"]}'})


@app.route('/api/refresh-all', methods=['POST'])
def api_refresh_all():
    """Trigger cache refresh for all apps."""
    if not is_super_admin():
        return jsonify({'status': 'error', 'message': 'Forbidden'}), 403

    if not cm.check_internet():
        return jsonify({'status': 'error', 'message': 'No internet connection available'}), 503

    thread = threading.Thread(target=cm.mirror_all, daemon=True)
    thread.start()

    return jsonify({'status': 'success', 'message': 'Refresh started for all apps'})


@app.route('/api/cache-status')
def api_cache_status():
    """Return cache status as JSON."""
    if not is_super_admin():
        return jsonify({'status': 'error', 'message': 'Forbidden'}), 403

    config = cm.get_config()
    statuses = {}
    for app_entry in config['apps']:
        statuses[app_entry['id']] = cm.get_app_cache_status(app_entry['id'])

    return jsonify({
        'status': 'success',
        'online': cm.check_internet(),
        'active_jobs': cm.get_active_jobs(),
        'apps': statuses
    })


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
