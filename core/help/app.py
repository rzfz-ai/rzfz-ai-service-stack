# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""
razzfazz.ai Help Center — Local Documentation Hub

Serves cached offline copies of official app documentation,
filtered by user's Authentik groups. Includes admin cache management.
"""

from flask import (g, render_template, request, jsonify, send_from_directory,
                   abort, Response, url_for, has_app_context)
import os
import json
import re
import sys
import threading
from html import escape

from razzfazz_common.flask_app import create_base_app
from razzfazz_common.auth import SUPER_ADMINS_GROUP

from cache_manager import (MIRROR_SKIPPED_LOCAL, LOCAL_DOC_ROUTE, OWN_DOCS_DIR,
                           OWN_DOCS_SECTION_LABELS, OWN_DOCS_SECTIONS,
                           REFRESH_ALL_JOB_ID, STATE_FAILED, STATE_FALLBACK,
                           STATE_UNREADABLE, CacheManager)
from search_index import (MAX_QUERY_CHARS, build_index, collect_corpus)
import markdown_tree as _mtree  # #1196: tree asset dir name for the viewer

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

#: #1284 — how long to wait before each RE-attempt of the apps that are still
#: uncached or failed after the pass before it. One pass was not enough: a
#: capture that fails for a transient reason (an upstream rate-limit, a slow
#: site, `wget exit -7` mid-run) used to stay failed until somebody restarted
#: the container or clicked Refresh in the admin page — so a freshly installed
#: box shipped with one to three help sections permanently empty, DIFFERENT ones
#: on every install. Measured on 0.91: round 3 lost `komodo` + `gpustack-v0.7`,
#: round 4 `authentik` + `openwebui`, same box, same procedure. The delays grow
#: so a rate-limited source gets time to forgive, and the whole schedule is
#: bounded — a source that is genuinely broken ends up in `failed_modules` with
#: its reason, which is the honest outcome.
_AUTOWARM_RETRY_DEFAULT = (120, 300, 900)


def _parse_retry_delays(raw: str | None) -> tuple[int, ...]:
    """Seconds between autowarm passes, from `HELP_AUTOWARM_RETRY_DELAYS`.

    #1297 (rzfz review F1): this used to be a bare `int(x)` on module level.
    A single mistyped entry raised at IMPORT time, so the help container never
    produced an app object — no `/health`, no log beyond the traceback. And the
    likely typo is not exotic: every other duration in this stack carries a
    unit (`start_period: 120s`, `interval: 30s`), so `120,300s` is what a
    first-time reader writes. Help that warms late is a blemish; help that does
    not start takes away the surface the operator would read to find out what
    went wrong. Bad input therefore warns once and falls back.
    """
    if not (raw or "").strip():
        return _AUTOWARM_RETRY_DEFAULT
    try:
        parsed = tuple(int(x) for x in raw.split(",") if x.strip())
    except ValueError:
        print(f"[autowarm] HELP_AUTOWARM_RETRY_DELAYS={raw!r} is not a "
              f"comma-separated list of whole seconds (no unit suffix — write "
              f"120,300,900 not 120,300s); using the default "
              f"{','.join(str(s) for s in _AUTOWARM_RETRY_DEFAULT)}",
              flush=True)
        return _AUTOWARM_RETRY_DEFAULT
    if not parsed or any(s < 0 for s in parsed):
        print(f"[autowarm] HELP_AUTOWARM_RETRY_DELAYS={raw!r} yields no usable "
              f"delay; using the default "
              f"{','.join(str(s) for s in _AUTOWARM_RETRY_DEFAULT)}", flush=True)
        return _AUTOWARM_RETRY_DEFAULT
    return parsed


AUTOWARM_RETRY_DELAYS_S = _parse_retry_delays(
    os.environ.get('HELP_AUTOWARM_RETRY_DELAYS'))


def _autowarm_pass(reason: str) -> list:
    """Mirror every app that is uncached or failed. → ids still unhealthy.

    Idempotent: an app whose cache is present and error-free is skipped, so a
    later pass costs nothing for the apps that already succeeded.
    """
    remaining = []
    config = cm.get_config()
    for app_entry in config.get('apps', []):
        app_id = app_entry.get('id')
        if not app_id:
            continue
        # #149: box-local module pages are never mirrored — skip them so the
        # auto-warm loop doesn't keep retrying a wget for docs we ship locally.
        # #824/fix-round: a `capture_method: monolith` module still carries
        # `local_doc` as its OFFLINE FALLBACK, but it has a REAL capture path
        # (mirror_docs handles it same as any other mirrored app) — a bare
        # check here skipped it forever, so it was never (re-)warmed on
        # container start even when uncached or failed. #1196 generalises
        # that to every explicit capture_method (llms-txt, git-markdown,
        # wget): only a local_doc entry with NO capture method is skipped.
        if cm.is_local_only(app_entry):
            continue
        try:
            status = cm.get_app_cache_status(app_id)
        except Exception:
            continue
        # Re-mirror when the cache is empty OR the last mirror FAILED. A
        # failed mirror (wget exit 4/6, or exit 8 that failed the sanity
        # gate) leaves partial files on disk so `cached` is True — but it
        # must not be left cached-as-good; retry it. An accepted-partial
        # mirror (exit 8 that passed the gate) carries no `error`, so it is
        # left in place (it is usable).
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
            print(f'[autowarm] mirror_docs({app_id}) failed ({reason}): {e}', flush=True)
        try:
            after = cm.get_app_cache_status(app_id)
        except Exception:
            continue
        if not after.get('cached') or after.get('error'):
            remaining.append(app_id)
    return remaining


def _autowarm_uncached_caches():
    """Background fetch of uncached app docs. Runs once per container life."""
    try:
        time.sleep(30)
        remaining = _autowarm_pass('startup')
        # #982: warm the search index once autowarm has had a chance to
        # populate caches, so the first real /api/search isn't the one
        # paying for the cold build. Defensive: a warm failure here must
        # never take down the autowarm thread — it just means the index
        # builds lazily on first query instead, same as before this change.
        # Deliberately BEFORE the retries (#1284): the index must not wait
        # for a source that may take a quarter of an hour to come back.
        try:
            get_search_index()
            # HLP-2: same deal for the visibility snapshot /api/search filters
            # with — warm it here so the first query doesn't pay for the walk.
            get_visibility_snapshot()
        except Exception as e:
            print(f'[autowarm] search index warm failed: {e}', flush=True)
        # #1284: bounded retries for whatever is still unhealthy.
        for i, delay in enumerate(AUTOWARM_RETRY_DELAYS_S, 1):
            if not remaining:
                break
            print(f'[autowarm] {len(remaining)} mirror(s) still unhealthy '
                  f'({", ".join(sorted(remaining))}) — retry {i}/'
                  f'{len(AUTOWARM_RETRY_DELAYS_S)} in {delay}s', flush=True)
            time.sleep(delay)
            remaining = _autowarm_pass(f'retry {i}')
        if remaining:
            print(f'[autowarm] giving up on {", ".join(sorted(remaining))} after '
                  f'{len(AUTOWARM_RETRY_DELAYS_S)} retries — see /api/cache-status '
                  '(failed_modules) for the reason of each', flush=True)
    except Exception as e:
        print(f'[autowarm] thread aborted: {e}', flush=True)


# Spawn on import, not in __main__, so gunicorn workers also kick it off.
# Daemon=True so the thread doesn't block container shutdown.
#
# #1834 — but NOT under pytest, and the reason is a process abort, not tidiness.
#
# This thread starts at IMPORT time, sleeps 30 s, then prints. A test that
# imports this module therefore leaves a daemon thread running long after the
# test finished; the retry ladder (AUTOWARM_RETRY_DELAYS_S) keeps it alive for
# minutes. When the interpreter finalises while such a thread is inside a
# print, CPython aborts:
#
#     Fatal Python error: _enter_buffered_busy: could not acquire lock for
#       <_io.BufferedWriter name='<stdout>'> at interpreter shutdown,
#       possibly due to daemon threads
#     Python runtime state: finalizing
#     … Aborted (core dumped)
#
# Measured 2026-09-09 on a full `./rzfz test --unit --api all`: 11 727 passed,
# 0 failed — and rc **134** (SIGABRT), plus a 1.4 GB core file in the repo
# root. The pre-merge gate then printed "FAIL — do not merge" over a green
# test run, which is the expensive part: a verdict that contradicts its own
# summary teaches people to stop reading verdicts.
#
# The gate is on `pytest` being imported, because that is exactly the
# condition — the module is being imported by a test process that will
# finalise. A container never finalises, so this changes nothing in service.
# `HELP_AUTOWARM=0` additionally silences it anywhere (a test that WANTS the
# thread can set `HELP_AUTOWARM=1`).
_AUTOWARM_ENABLED = os.environ.get("HELP_AUTOWARM")
if _AUTOWARM_ENABLED is None:
    _AUTOWARM_ENABLED = "0" if "pytest" in sys.modules else "1"
if _AUTOWARM_ENABLED != "0":
    threading.Thread(target=_autowarm_uncached_caches, daemon=True).start()
else:
    print("[autowarm] disabled (HELP_AUTOWARM=0 or running under pytest) — "
          "caches populate on demand (#1834)", flush=True)

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


def _visible_ids() -> set[str]:
    """The doc ids the CURRENT user may open — the same gate `index()` and
    `view_docs` already enforce, factored out so `/api/search` (#982) filters
    results with the identical rule instead of re-implementing it.

    Applies exactly the rules `get_visible_apps()` + the own-doc AUDIENCE gate
    do (required_groups / super-admin / active-profile for mirrors; a non-admin
    only sees `audience: end-user` Guides), but reads its INPUTS from the
    cached visibility snapshot instead of calling `get_visible_apps()`.

    HLP-2: `get_visible_apps()` calls `get_app_cache_status()` per app, which
    `du`-walks that app's whole mirror cache tree (`_dir_size`), and
    `discover_own_docs()`, which walks + reads the entire own-docs tree. On the
    hub page that is once per page load; on `/api/search` — fired per keystroke
    behind a 200 ms debounce — it was a full recursive stat of every cache tree
    per request, i.e. a trivially triggerable I/O DoS, and it contradicted the
    route's own "no filesystem access on the request path" docstring. Cache
    sizes/timestamps have no bearing on WHO MAY SEE a doc, so none of that
    belongs on the search path.
    """
    admin = is_super_admin()
    groups = get_user_groups()
    active = _active_profiles()
    snapshot = get_visibility_snapshot()

    ids = set()
    for a in snapshot['apps']:
        required = a['required_groups']
        if not (not required or admin or any(g in groups for g in required)):
            continue
        if a['profiles'] and active and not (set(a['profiles']) & active):
            continue
        ids.add(a['id'])
    for own in snapshot['own']:
        if admin or own['audience'] == 'end-user':
            ids.add(own['id'])
    return ids


# ---------------------------------------------------------------------------
# Search index (#982) — lazy in-memory singleton over the local doc corpus.
# ---------------------------------------------------------------------------
# Built once (on first use) from collect_corpus(cm), which reads own docs,
# box-local module docs, and cached mirrors straight off disk — no network,
# air-gap safe. Role/profile filtering is NOT baked into the index; it is
# applied per-query via `allowed_ids=_visible_ids()` in /api/search, so one
# index serves every user and a query can never leak a doc id the caller
# isn't allowed to open. invalidate_search_index() clears the cached object
# so the NEXT get_search_index() call rebuilds from the current corpus
# (wired to the refresh routes / autowarm in a later step).
#
# HLP-4: a rebuild NEVER happens while a lock a searcher needs is held.
# `collect_corpus(cm)` reads and tag-strips up to 200 KB from every own doc,
# every local module doc and every cached mirror index.html — seconds of work
# on a full box — and it used to run *inside* `_search_index_lock`, so every
# concurrent /api/search blocked for the whole build. Since an admin working
# through the cache admin page invalidates on every refresh, that was easy to
# hit. Now: `_search_index_lock` guards only the (trivial) global reads and
# writes, the corpus build runs outside it under a separate build lock, and a
# reader that finds a STALE index while someone else rebuilds serves the stale
# one instead of waiting. `_search_generation` is what makes the swap safe —
# a build whose generation was invalidated mid-flight is not published.
_search_index = None
_search_index_gen = -1
_search_index_lock = threading.Lock()      # short critical sections only
_search_build_lock = threading.Lock()      # serialises corpus builds
_search_generation = 0

# HLP-2: the user-independent, FILESYSTEM-derived inputs of the visibility
# gate — the configured apps' required_groups/profiles and the discovered own
# docs' audience. Built at index time, on the same lifecycle as the search
# index (both are views of the same on-disk corpus, and a refresh invalidates
# both together), so `/api/search` applies the per-user gate against an
# in-memory structure instead of re-walking every cache tree per keystroke.
_visibility_snapshot = None
_visibility_snapshot_gen = -1


def _cached_build(kind):
    """Shared build-outside-the-lock/swap-under-it machinery (HLP-4).

    `kind` is 'index' or 'visibility'. Both are views of the same on-disk
    corpus, both are invalidated together by invalidate_search_index(), and
    both are expensive enough that building them under a lock a request path
    needs is the bug this exists to prevent. Returns the cached value,
    rebuilding it when the generation moved on.
    """
    global _search_index, _search_index_gen
    global _visibility_snapshot, _visibility_snapshot_gen

    def _read():
        with _search_index_lock:
            if kind == 'index':
                return _search_index, _search_index_gen, _search_generation
            return _visibility_snapshot, _visibility_snapshot_gen, _search_generation

    value, built_gen, gen = _read()
    if value is not None and built_gen == gen:
        return value

    # A stale-but-usable value means we do NOT have to wait for whoever is
    # already rebuilding — serve the previous corpus for one more request.
    if not _search_build_lock.acquire(blocking=value is None):
        return value
    try:
        value, built_gen, gen = _read()
        if value is not None and built_gen == gen:
            return value
        fresh = (build_index(collect_corpus(cm)) if kind == 'index'
                 else _build_visibility_snapshot())
        with _search_index_lock:
            # Discard the result if an invalidation landed mid-build: it was
            # computed from a corpus we have since been told is out of date.
            if _search_generation == gen:
                if kind == 'index':
                    _search_index, _search_index_gen = fresh, gen
                else:
                    _visibility_snapshot, _visibility_snapshot_gen = fresh, gen
        return fresh
    finally:
        _search_build_lock.release()


def get_visibility_snapshot():
    """Lazy singleton: {'apps': [...], 'own': [...]} — see _visible_ids()."""
    return _cached_build('visibility')


def _build_visibility_snapshot() -> dict:
    """Read the visibility-relevant metadata off disk exactly once.

    Deliberately keeps ONLY the fields the gate reads (id, required_groups,
    profiles, audience) — no cache status, no sizes, nothing that changes
    between refreshes without also invalidating this snapshot.
    """
    config = cm.get_config()
    return {
        'apps': [
            {
                'id': a['id'],
                'required_groups': list(a.get('required_groups') or []),
                'profiles': list(a.get('profiles') or []),
            }
            for a in config.get('apps', [])
        ],
        'own': [
            {'id': o['id'], 'audience': o.get('audience')}
            for o in cm.discover_own_docs()
        ],
    }


def get_search_index():
    """Lazy singleton: build once, reuse until invalidate_search_index().

    Rebuilds happen OUTSIDE `_search_index_lock` (HLP-4) — see _cached_build().
    """
    return _cached_build('index')


def invalidate_search_index():
    """Mark the cached index (and the visibility snapshot built alongside it)
    out of date, so the next get_search_index() / get_visibility_snapshot()
    rebuilds from the current corpus.

    HLP-4: this bumps a generation rather than nulling the cached objects, so a
    searcher arriving during the rebuild is served the previous index instead
    of blocking on the build. The stale window is one rebuild long and ends the
    moment the new index is published.
    """
    global _search_generation
    with _search_index_lock:
        _search_generation += 1


def _refresh_and_reindex(target, *args):
    """Run a mirror job to completion, THEN invalidate the search index so the
    next /api/search rebuilds from the freshly-mirrored content (#982).

    The mirror runs async in a daemon thread; invalidating BEFORE it finished
    (the first cut) rebuilt the index from pre-refresh disk and then never
    re-invalidated — so a refresh's new content stayed unsearchable until some
    later, unrelated invalidation. Invalidate in a `finally` so a failed mirror
    still clears the (now possibly partial) cache rather than pinning stale data.
    """
    try:
        target(*args)
    finally:
        invalidate_search_index()


# HLP-5: `/api/refresh/<app_id>` refuses a second run with 409 while a job for
# that app is active; `/api/refresh-all` had no equivalent, so N clicks spawned
# N threads running `cm.mirror_all()` over all 24 apps CONCURRENTLY, each
# wget/monolith capture writing into the same cache directories. The failure
# mode is a corrupted/partial mirror tree, not merely wasted work.
_refresh_all_lock = threading.Lock()


def _refresh_all_and_reindex():
    """mirror_all + reindex, holding the module-level refresh-all lock.

    The lock is acquired NON-BLOCKINGLY by the route (so a second POST is
    answered 409 rather than queued) and released here, in a finally, once the
    job has run to completion — including when it raises.

    #1055: the file-based marker is set/cleared alongside it. The in-process
    lock only guards THIS gunicorn worker and is invisible to the admin
    poller; the marker is what makes "a refresh-all is running" a fact every
    worker and the UI can read, for the whole run rather than only during the
    per-app windows `get_active_jobs()` happens to catch.
    """
    try:
        _refresh_and_reindex(cm.mirror_all)
    finally:
        cm.clear_refresh_all_lock()
        _refresh_all_lock.release()


# ---------------------------------------------------------------------------
# Routes — User-Facing
# ---------------------------------------------------------------------------
# /healthz is mounted by razzfazz_common.health.get_health_blueprint via
# create_base_app() above — returns {"status": "ok", "service": "razzfazz-help"}.

# #982 Task 4: stable per-section glyph for the two-level nav rail. Keyed by
# the own-doc `section` id — since #1055 that is the closed five-bucket
# taxonomy set (cache_manager.OWN_DOCS_SECTIONS), so every top-level topic has
# its own glyph; the default is a fallback that should never be reached.
# Nav icons are inline SVG (Lucide-style stroke icons), NOT emoji — rendered
# `| safe` in base.html and coloured light-grey via .topic .ic CSS. Each is a
# 24x24 viewBox, fill:none, stroke:currentColor so the rail's grey (and the
# active-topic accent) flow through with no per-icon colour baked in.
def _svg(body: str) -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            'aria-hidden="true">' + body + '</svg>')

# grid — Apps & Modules, and the fallback for anything unmapped
_DEFAULT_TOPIC_ICON = _svg('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>')

# #1055: one glyph per bucket of the CLOSED taxonomy set — the keys here are
# exactly cache_manager.OWN_DOCS_SECTIONS, and the nav guard test pins that.
_TOPIC_ICONS = {
    # rocket — Get started
    'get-started': _svg('<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>'),
    # wrench — Guides (the merged tutorials + how-to: doing something)
    'guides': _svg('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>'),
    # book — Reference
    'reference': _svg('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>'),
    # layers — Concepts (how it fits together)
    'concepts': _svg('<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>'),
    # grid — Apps & Modules
    'apps': _DEFAULT_TOPIC_ICON,
}
#: The one bucket that is not fed by the own-doc tree alone: the external app
#: mirrors and the module docs land here, so build_topics() always emits it.
APPS_SECTION = 'apps'

# The nav can only ever show these five buckets, so the glyph map must cover
# exactly them — a bucket added on one side and forgotten on the other is a
# taxonomy drift, and it fails here at import rather than shipping a rail with
# a mystery tab or a blank icon.
if set(_TOPIC_ICONS) != set(OWN_DOCS_SECTIONS):
    raise RuntimeError(
        '_TOPIC_ICONS must have exactly one glyph per taxonomy bucket '
        f'({sorted(OWN_DOCS_SECTIONS)}), got {sorted(_TOPIC_ICONS)}')


def _group_own_sections(apps: list[dict], is_admin: bool) -> list[dict]:
    """Group the auto-discovered own docs ("razzfazz.ai Guides") by section,
    preserving discover_own_docs()'s curated section order.

    Role-aware: the Guides are operating/administering the box — a STACK-ADMIN
    job — so non-admins only see pages explicitly marked end-user-facing
    (`<!-- audience: end-user -->`, e.g. the tutorials). A stack admin sees all.

    Shared by `index()` (legacy `own_sections` context var) and `build_topics()`
    (#982 two-level nav) so the role/audience gate lives in exactly one place.
    """
    own_sections = []  # list[ {section, label, apps[]} ] in display order
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
    return own_sections


def build_topics() -> list[dict]:
    """Build the two-level nav structure (#982): own-doc sections first (each
    a topic with its pages), then one final "Apps & Modules" topic carrying the
    external (non-own) visible mirrors plus any own-doc tagged `section: apps`.

    #1055: the top level is the CLOSED taxonomy set — every topic id here is
    one of cache_manager.OWN_DOCS_SECTIONS, because `section` is now a declared
    value from that set rather than whatever directory a doc happens to sit in.

    Reuses `get_visible_apps()` (role/profile gate for mirrors, unconditional
    inclusion of own docs) and `_group_own_sections()` (own-doc audience gate)
    rather than re-implementing either — same rule `index()` and `_visible_ids()`
    already enforce.

    Callable outside a request (e.g. `test_982_nav_structure.py` calls it
    bare, and the autowarm thread has no request either): `get_user_groups`/
    `is_super_admin` read `flask.g`, which needs a pushed app context or the
    `hasattr(g, ...)` check itself raises RuntimeError. Inside a real request
    an app context is already active (pushed alongside the request context,
    carrying the before_request-populated `g.user`) — reuse it as-is so the
    real role gate applies unchanged. Only when NO context exists do we push
    a bare one, which yields the same fail-safe "no g.user" defaults
    (anonymous / non-admin / no groups) `get_user_groups`/`is_super_admin`
    already fall back to.

    Returns: ordered list of
      {"id", "label", "icon", "pages": [{"id", "title", "cached", "is_own"}]}
    """
    if has_app_context():
        return _build_topics()
    with app.app_context():
        return _build_topics()


def _build_topics() -> list[dict]:
    apps = get_visible_apps()
    is_admin = is_super_admin()

    topics = []
    apps_topic_pages = []
    for section in _group_own_sections(apps, is_admin):
        pages = [
            {
                'id': a['id'],
                'title': a.get('title', a.get('name', a['id'])),
                'cached': a.get('cached'),
                'is_own': True,
            }
            for a in section['apps']
        ]
        # #1055: an own-doc that declares `section: apps` (per-app / module
        # help written by us) belongs in the SAME "Apps & Modules" topic as
        # the mirrors — never a second top-level tab with the same id.
        if section['section'] == APPS_SECTION:
            apps_topic_pages.extend(pages)
            continue
        topics.append({
            'id': section['section'],
            'label': section['label'],
            'icon': _TOPIC_ICONS.get(section['section'], _DEFAULT_TOPIC_ICON),
            'pages': pages,
        })

    apps_topic_pages.extend(
        {
            'id': a['id'],
            'title': a.get('title', a.get('name', a['id'])),
            'cached': a.get('cached'),
            'is_own': False,
        }
        for a in apps
        if not a.get('is_own')
    )
    topics.append({
        'id': APPS_SECTION,
        'label': OWN_DOCS_SECTION_LABELS[APPS_SECTION],
        'icon': _TOPIC_ICONS[APPS_SECTION],
        'pages': apps_topic_pages,
    })
    return topics


def _topic_for(app_id: str, topics: list[dict] | None = None) -> str | None:
    """The topic id whose `pages` list contains `app_id`, else the first topic.

    #982 Task 5: `index()` already knows which topic was clicked when it
    links to a page, but a doc reached directly (bookmark, deep link, or a
    search result) has no such context — `view_docs` uses this so the rail
    still highlights the right top-level topic and the sub-nav still shows
    the right siblings (with the current page marked active) even when the
    page was opened cold. `topics` may be passed in to avoid recomputing
    `build_topics()` twice per request; omitted, it computes its own.
    """
    topics = topics if topics is not None else build_topics()
    for t in topics:
        if any(p['id'] == app_id for p in t['pages']):
            return t['id']
    return topics[0]['id'] if topics else None


@app.route('/')
def index():
    """Hub page: cards for each available documentation section."""
    apps = get_visible_apps()
    username = get_username()
    is_admin = is_super_admin()
    own_sections = _group_own_sections(apps, is_admin)
    topics = build_topics()
    active_topic = topics[0]['id'] if topics else None
    return render_template('index.html', apps=apps, own_sections=own_sections,
                           topics=topics, active_topic=active_topic,
                           username=username, is_admin=is_admin)


# HLP-1 (#1020): mirrored/monolith upstream content is UNTRUSTED code that we
# serve SAME-ORIGIN on help.<domain>, inside the viewer's Authentik session.
# The Caddy help vhost imports only (security_headers), which sets no CSP, and
# the #525/#824 monolith capture inlines every upstream <script> into the saved
# file — so a captured (or hostile / tampered) doc page executed as the logged-in
# viewer and could read /api/search, fetch admin-only /docs/<slug>/ pages and
# POST /api/refresh-all with the browser's own cookies.
#
# Fix: every response that carries MIRRORED (non-own, upstream-derived) bytes
# gets a strict Content-Security-Policy. `script-src 'none'` kills both inline
# and remote script in the mirrored document; `default-src 'none'` + the narrow
# per-type allowances keep the page rendering (its own cached CSS/images/fonts
# are same-origin, and mirrored pages carry inline style attributes/blocks, as
# does our injected attribution bar) while `connect-src 'none'` stops any
# exfiltration channel and `form-action 'none'` stops credential-post tricks.
#
# Deliberately NOT applied site-wide: the Help Center's OWN pages (base.html /
# viewer.html / admin.html) legitimately run inline script, so the strict policy
# is scoped to the mirrored-doc responses that need it. Setting a site-wide CSP
# in the Caddy vhost would OVERRIDE this per-response header (Caddy `header`
# replaces) — if one is ever added there it must be at least as strict for
# /docs/* as this, see core/Caddy/Caddyfile §8.
MIRRORED_DOC_CSP = (
    "default-src 'none'; "
    "script-src 'none'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "media-src 'self' data:; "
    "connect-src 'none'; "
    "frame-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'self'"
)


def _harden_mirrored(response):
    """Stamp the strict mirrored-content CSP on a response (HLP-1 / #1020)."""
    response.headers['Content-Security-Policy'] = MIRRORED_DOC_CSP
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


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


def _resolves_in_cache(cache_dir: str, relpath: str) -> bool:
    """True if `relpath` names something view_docs could actually serve.

    Same resolution order view_docs uses (exact file / directory index /
    implicit .html), path-traversal-guarded. Used before redirecting to a
    mirror's configured entry_path (#1055) so a broken mirror does not send
    the reader one hop further into a 404.
    """
    root = os.path.realpath(cache_dir)
    target = os.path.realpath(os.path.join(cache_dir, relpath.strip('/')))
    if target != root and not target.startswith(root + os.sep):
        return False
    return (os.path.isfile(target)
            or os.path.isfile(os.path.join(target, 'index.html'))
            or os.path.isfile(target + '.html'))


def _entry_is_directory(cache_dir: str, relpath: str) -> bool:
    """True if the entry resolves to `<relpath>/index.html` (a directory the
    browser must treat as a folder), False when it is a plain file
    (`<relpath>` or `<relpath>.html`) — #1196, see view_docs."""
    root = os.path.realpath(cache_dir)
    target = os.path.realpath(os.path.join(cache_dir, relpath.strip('/')))
    if target != root and not target.startswith(root + os.sep):
        return False
    if os.path.isfile(target) or os.path.isfile(target + '.html'):
        return False
    return os.path.isfile(os.path.join(target, 'index.html'))


#: Extensions a mirrored tree serves as sub-RESOURCES rather than pages. A
#: request for one of these is never answered with the Unavailable HTML page
#: (#1229 review LOW 7) — a stylesheet request wants a stylesheet or a 404.
_PAGE_SUFFIXES = ('.html', '.htm')


def _looks_like_page_request(filepath: str | None) -> bool:
    """True when `filepath` addresses a documentation PAGE (an .html file or
    an extension-less route), False for an asset (.css/.js/.png/…)."""
    if not filepath:
        return True
    last = filepath.rstrip('/').rsplit('/', 1)[-1]
    if '.' not in last:
        return True
    return last.lower().endswith(_PAGE_SUFFIXES)


def _stale_capture_notice(status: dict) -> str:
    """The one-line "you are reading the last good copy" banner text (#1229
    review MEDIUM 4)."""
    when = status.get('last_updated')
    tail = f' (last attempt {str(when)[:16].replace("T", " ")} UTC)' if when else ''
    return ('⚠ The most recent refresh of these docs failed' + tail
            + ' — you are reading the last copy that captured cleanly.')


def _render_unavailable(app_id: str, app_entry: dict):
    """The honest "these docs are not readable offline right now" page (#1055).

    Replaces both the old fixed "Documentation not yet cached" text and — for
    an ENTRY request — the bare `abort(404)` a failed mirror produced. It says
    which of the two actually happened and, for an admin, prints the real
    error the capture recorded, so the operator is not left reading a 404 and
    guessing whether the module is missing, broken, or merely unwarmed.

    Rendered through viewer.html so the page keeps the nav rail: a reader who
    lands here can still reach every other doc, which a Flask 404 could not.
    """
    status = cm.get_app_cache_status(app_id)
    state = status.get('mirror_state')
    error = status.get('error')
    is_admin = is_super_admin()

    if state in (STATE_FAILED, STATE_UNREADABLE, STATE_FALLBACK):
        heading = 'This documentation did not mirror correctly'
        body = ('The last attempt to capture these docs failed, so there is no '
                'usable offline copy on this box.')
    else:
        heading = 'Documentation not yet cached'
        body = 'An administrator needs to refresh the documentation cache.'

    parts = [f'<div class="not-cached"><h2>{escape(heading)}</h2><p>{escape(body)}</p>']
    if is_admin:
        if error:
            parts.append(f'<p><strong>Reported error:</strong> '
                         f'<code>{escape(str(error))}</code></p>')
        parts.append('<p><a href="/admin/cache">Open cache administration</a> '
                     'to retry this module.</p>')
    else:
        parts.append('<p>Please ask a stack administrator to refresh the '
                     'documentation cache.</p>')
    upstream = app_entry.get('mirror_url')
    if upstream:
        parts.append(f'<p>Upstream documentation: '
                     f'<a href="{escape(str(upstream), quote=True)}" target="_blank" '
                     f'rel="noopener">{escape(str(upstream))}</a> '
                     '(needs internet access).</p>')
    parts.append('</div>')

    _topics = build_topics()
    return render_template('viewer.html',
                           content=''.join(parts),
                           app_entry=app_entry,
                           is_admin=is_admin,
                           topics=_topics,
                           active_topic=_topic_for(app_id, _topics))


def _serve_markdown_tree(app_id: str, app_entry: dict, filepath: str | None):
    """Serve a markdown-tree capture (#1196: llms-txt / git-markdown).

    Pages render through viewer.html — the same Help Center shell and
    theme the box's own docs use — from the cleaned markdown the capture
    stored; `_assets/*` (images pulled into the tree) are served raw under
    the mirrored-doc CSP. There is no entry redirect: the index route IS the
    table of contents (plus the tree's home page, when it names one).
    """
    cache_dir = cm.get_cache_path(app_id)
    rel = (filepath or '').strip('/')
    if cache_dir and rel.startswith(_mtree.ASSETS_DIR + '/'):
        name = rel[len(_mtree.ASSETS_DIR) + 1:]
        directory = os.path.realpath(os.path.join(cache_dir, _mtree.ASSETS_DIR))
        target = os.path.realpath(os.path.join(directory, name))
        if '/' in name or not name or not target.startswith(directory + os.sep) \
                or not os.path.isfile(target):
            abort(404)
        return _harden_mirrored(send_from_directory(directory, name))
    rendered = cm.render_tree_page(app_id, rel)
    if rendered is None:
        if not rel:
            return _render_unavailable(app_id, app_entry)
        abort(404)
    content, title = rendered
    _topics = build_topics()
    return render_template('viewer.html',
                           content=content,
                           app_entry={**app_entry, 'title': title},
                           is_admin=is_super_admin(),
                           topics=_topics,
                           active_topic=_topic_for(app_id, _topics))


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
        _topics = build_topics()
        return render_template('viewer.html',
                             content=content,
                             app_entry=app_entry,
                             is_admin=is_super_admin(),
                             topics=_topics,
                             active_topic=_topic_for(app_id, _topics))

    # Box-local module page (#149): a curated own-doc replaces the upstream
    # mirror for modules whose SPA/CDN docs can't be reliably wget-mirrored
    # (dify, cognee, openhands, gotenberg, komodo, lightrag). Rendered from
    # core/help/module_docs via the same markdown pipeline as the own-docs.
    # Available to every authenticated user (same visibility as a mirror — no
    # audience gate). Any filepath under the prefix renders the same page.
    #
    # #824: monolith-first, local_doc-fallback. Of these six, ONLY `lightrag`
    # carries `capture_method: monolith` today — the other five are
    # `llms-txt` (dify, cognee, openhands) and `wget` (gotenberg, komodo),
    # and `test_1196_wget_sweep.py::test_only_lightrag_still_uses_monolith`
    # pins exactly that. The sentence that used to stand here claimed all six
    # had moved; it described the plan, and the plan was overruled by #1196,
    # where an operator looked at the monolith capture of Dify's docs and got
    # a raw JS bundle. Rolling the other five over is a live question (#824),
    # not a done deal — and the thing that decides it is a look at a rendered
    # capture, not a byte count. A validated capture is ONE
    # self-contained file — monolith inlines every CSS/JS/image/font
    # reference, so unlike the wget tree below there is no entry_path /
    # sub-page routing to do — serve it directly here, for any filepath
    # under the prefix (there is nothing else to route to). local_doc is
    # retained as the OFFLINE FALLBACK: it is what's served when monolith is
    # unavailable (no binary, no WAN — #184 air-gapped boxes) or its capture
    # failed / was rejected by validate_capture_substance.
    #
    # #1197: the curated page is ALSO addressable next to a working capture
    # (`/docs/<app>/_local/`, linked from the mirror's attribution bar), so
    # box-specific notes — a known upstream issue, this box's ports/tokens —
    # are reachable without the mirror having to fail first.
    if filepath and filepath.strip('/') == LOCAL_DOC_ROUTE and app_entry.get('local_doc'):
        content = cm.render_local_doc(app_id)
        if content is None:
            abort(404)
        _topics = build_topics()
        return render_template('viewer.html',
                             content=content,
                             app_entry=app_entry,
                             is_admin=is_super_admin(),
                             topics=_topics,
                             active_topic=_topic_for(app_id, _topics))

    # #1196: a markdown-tree capture (llms-txt / git-markdown) outranks both
    # — it is the real upstream documentation, rendered in our theme.
    if cm.has_valid_markdown_tree(app_id):
        return _serve_markdown_tree(app_id, app_entry, filepath)

    if app_entry.get('local_doc'):
        if cm.has_valid_monolith_capture(app_id):
            cache_dir = cm.get_cache_path(app_id)
            html_path = os.path.join(cache_dir, 'index.html') if cache_dir else None
            if html_path and os.path.isfile(html_path):
                with open(html_path, 'r', encoding='utf-8', errors='replace') as f:
                    html_content = f.read()
                modified = cm.inject_attribution(html_content, app_entry, app_id, '')
                return _harden_mirrored(Response(modified, mimetype='text/html'))
        # #1196: a wget-first module (komodo/gotenberg: explicit
        # `capture_method: wget` + local_doc fallback) serves its tree when
        # the last capture passed the gate; otherwise the curated page.
        if not cm.has_valid_wget_tree(app_id):
            content = cm.render_local_doc(app_id)
            if content is None:
                abort(404)
            _topics = build_topics()
            return render_template('viewer.html',
                                 content=content,
                                 app_entry=app_entry,
                                 is_admin=is_super_admin(),
                                 topics=_topics,
                                 active_topic=_topic_for(app_id, _topics))

    # Cached external docs
    cache_dir = cm.get_cache_path(app_id)
    if not cache_dir or not os.path.isdir(cache_dir):
        return _render_unavailable(app_id, app_entry)

    # #1055: was this request the doc's ENTRY POINT (the link from the hub /
    # the nav rail / a bookmark of /docs/<app_id>/), or a sub-resource of a
    # page that is already rendering? An entry request that cannot be
    # resolved must never be a bare 404 — that is the operator's "offline help
    # won't load": the hub advertises the module (its cache dir is non-empty,
    # because a FAILED wget leaves files behind), the reader clicks, and gets
    # Flask's default Not Found with no nav, no explanation and no way back.
    # Sub-resource 404s stay 404s — a missing .css must not render a page.
    is_entry_request = filepath is None
    #: Set when the tree on disk is a LAST GOOD capture whose newest refresh
    #: failed — rendered into the attribution bar so the reader is told the
    #: page may be out of date instead of silently reading stale docs.
    stale_notice = None

    # #1196: a mirror whose LAST capture failed the exit-code or
    # content-quality gate leaves bytes on disk (a redirect stub, a naked
    # tree, …) — that is exactly what the #1055 state model says not to
    # trust.
    #
    # #1229 review MEDIUM 4 + LOW 7 — two corrections to that:
    #
    #  * A FAILED **refresh** is not the same as a broken **tree**.
    #    `_discard_failed_cache` deliberately keeps a previously-good tree
    #    until the next refresh starts, so "last night's refresh failed" was
    #    replacing working offline documentation with "not offline readable"
    #    — a regression against the pre-#1196 behaviour, on the one box shape
    #    (air-gapped, #184) where the cached copy is all there is. Ask whether
    #    the bytes on disk still render as docs; when they do, serve them and
    #    say in the attribution bar that the last refresh failed.
    #  * The verdict must be the SAME for the entry and for a deeplink into
    #    it. Gating only `is_entry_request` meant a bookmark of
    #    /docs/<app>/page.html kept serving the junk the entry refused to
    #    show. Page requests now get the honest page either way; a
    #    sub-RESOURCE (.css/.png/…) still 404s — answering an asset request
    #    with an HTML page helps nobody.
    _status = cm.get_app_cache_status(app_id)
    if _status.get('mirror_state') in (STATE_FAILED, STATE_UNREADABLE):
        if not cm.has_servable_tree(app_id):
            if is_entry_request or _looks_like_page_request(filepath):
                return _render_unavailable(app_id, app_entry)
            abort(404)
        stale_notice = _stale_capture_notice(_status)

    # If no filepath, redirect to the entry path (derived from mirror URL)
    if filepath is None:
        entry_path = cm.get_entry_path(app_id)
        # …but only when that entry path actually EXISTS in the cache. A
        # failed/truncated mirror leaves the directory without it, and the
        # redirect then lands on a 404 one hop away from the hub — the same
        # dead end, just harder to diagnose because the URL changed.
        if entry_path and _resolves_in_cache(cache_dir, entry_path):
            # Trailing slash is required so that browsers resolve relative links
            # (e.g., "overview/") correctly.  All wget-mirrored content uses
            # relative paths from within the entry directory, so the browser's
            # base URL must *include* that directory as a "folder".
            #
            # #1196: …unless the entry is a FILE (`docs/intro` saved by
            # --adjust-extension as `docs/intro.html`, the Docusaurus shape
            # of komodo/gotenberg). Its sibling links (`setup.html`) and
            # `../assets/…` are relative to `docs/`, so a trailing slash
            # would make the browser resolve them one level too deep.
            if _entry_is_directory(cache_dir, entry_path):
                return redirect(f'/docs/{app_id}/{entry_path}/')
            return redirect(f'/docs/{app_id}/{entry_path}')
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
            elif is_entry_request:
                return _render_unavailable(app_id, app_entry)
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
        elif is_entry_request:
            return _render_unavailable(app_id, app_entry)
        else:
            abort(404)

    # For HTML files: inject attribution bar + rewrite links, serve FULL page
    if full_path.endswith(('.html', '.htm')):
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            html_content = f.read()

        # #1196: hand the rewriter the FILE's path in the cache, not the
        # request path. A directory-index page (`docs/intro/` →
        # docs/intro/index.html) carries wget-converted refs relative to
        # docs/intro/ (`../../assets/css/…`); resolving them against
        # dirname('docs/intro') = docs/ escaped the tree and produced
        # `href="/docs/<app>/"` — a naked page. Measured live on openuem.
        file_rel = os.path.relpath(full_path, os.path.realpath(cache_dir)).replace(os.sep, '/')
        modified = cm.inject_attribution(html_content, app_entry, app_id, file_rel,
                                         notice=stale_notice)
        return _harden_mirrored(Response(modified, mimetype='text/html'))

    # For non-HTML assets (CSS, JS, images), serve directly. Same CSP: a
    # mirrored .js/.svg opened directly is upstream bytes on our origin too.
    directory = os.path.dirname(full_path)
    filename = os.path.basename(full_path)
    return _harden_mirrored(send_from_directory(directory, filename))




@app.route('/api/search')
def api_search():
    """One global full-text search over the local doc corpus (#982).

    Results are filtered by `_visible_ids()` — the same role/audience gate
    `index()`/`view_docs` enforce — so a query can never surface a doc a
    non-admin isn't allowed to open. Pure in-memory lookup: no network, no
    filesystem access on the request path (the index was built ahead of
    time by get_search_index()).
    """
    q = request.args.get('q', '')
    if not q.strip():
        return jsonify({'results': []})

    # HLP-3: bound the per-request cost. Query cost is linear in the term
    # count (one postings walk per term, plus one <mark> alternation branch
    # per unique term), and this route is unauthenticated beyond the Authentik
    # session and unthrottled — a 100 KB `q` was a CPU pin from a single
    # request. The index itself additionally truncates to MAX_QUERY_TERMS.
    if len(q) > MAX_QUERY_CHARS:
        return jsonify({
            'status': 'error',
            'message': f'Query too long (max {MAX_QUERY_CHARS} characters)',
        }), 400

    allowed = _visible_ids()
    hits = get_search_index().search(q, allowed_ids=allowed)

    results = [{
        'id': h['id'],
        'title': h['title'],
        'section': h['section'],
        'snippet': h['snippet'],
        'url': url_for('view_docs', app_id=h['id']),
    } for h in hits]

    return jsonify({'results': results})


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
        # #1196: the template keys the "Local page / Ships with the box"
        # treatment on this ONE flag instead of re-deriving it from
        # local_doc + capture_method (which grew a third and fourth method).
        statuses.append({**app_entry, **status, 'local_only': cm.is_local_only(app_entry)})

    active_jobs = cm.get_active_jobs()
    online = cm.check_internet()
    return render_template('admin.html',
                         apps=statuses,
                         active_jobs=active_jobs,
                         online=online,
                         is_admin=True,
                         # #1055: the true per-module state + the last full
                         # run's per-module record, so the page reports what
                         # actually happened instead of "✓ Cached" for every
                         # directory that merely has bytes in it.
                         refresh_all_active=cm.is_refresh_all_active(),
                         last_refresh_all=cm.get_last_refresh_all(),
                         failed_modules=cm.get_failed_modules(),
                         # #1229: the other half — served, but with a
                         # content-quality warning the operator should see.
                         degraded_modules=cm.get_degraded_modules(),
                         topics=build_topics())


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

    # #1055: refresh-all writes into EVERY app's cache directory, so a per-app
    # Refresh started mid-run is two writers on one tree — the same corruption
    # HLP-5 closed in the other direction (refresh-all during a per-app job).
    # The guard was one-way; the file-based registry makes it symmetric, and
    # works across gunicorn workers rather than only within one process.
    if cm.is_refresh_all_active():
        return jsonify({
            'status': 'error',
            'message': ('A refresh of all documentation is running — wait for '
                        'it to finish before refreshing a single module.'),
        }), 409

    # #525 Say so when there is nothing to refresh. A curated box-local page
    # with NO capture path (a plain wget mirror opted out entirely) makes
    # Refresh a genuine no-op — spawning a thread that returns immediately
    # would tell the operator "Refresh started" when nothing started.
    # Reporting a no-op as work in progress is what kept that gap invisible.
    #
    # #824: this no longer covers dify/cognee/openhands/gotenberg/komodo/
    # lightrag. Those six were the six with `local_doc` and no capture path —
    # #525 gave them `capture_method: monolith`, a REAL (if fallback-backed)
    # capture path, so their Refresh now legitimately starts one, same as any
    # other mirrored app; local_doc only backstops that capture at serve time
    # (see cache_manager.mirror_docs / has_valid_monolith_capture), not here.
    #
    # Not an error — there is no fault here — but not "started" either.
    # #1196: any explicit capture_method (llms-txt / git-markdown / wget) is a
    # real capture path too — see cache_manager.is_local_only.
    if cm.is_local_only(app_entry):
        return jsonify({
            'status': 'noop',
            'outcome': MIRROR_SKIPPED_LOCAL,
            'message': (f'{app_entry["name"]} ships a box-local documentation page — '
                        f'there is no upstream mirror to refresh.'),
        })

    # #982: reindex when the mirror COMPLETES (not at start) so the next
    # /api/search picks up the freshly-mirrored content — see _refresh_and_reindex.
    threading.Thread(target=_refresh_and_reindex, args=(cm.mirror_docs, app_id),
                     daemon=True).start()

    return jsonify({'status': 'success', 'message': f'Refresh started for {app_entry["name"]}'})


@app.route('/api/refresh-all', methods=['POST'])
def api_refresh_all():
    """Trigger cache refresh for all apps."""
    if not is_super_admin():
        return jsonify({'status': 'error', 'message': 'Forbidden'}), 403

    if not cm.check_internet():
        return jsonify({'status': 'error', 'message': 'No internet connection available'}), 503

    # HLP-5: one refresh-all at a time, and none while a single-app refresh is
    # mid-flight — both write into the same per-app cache directories. The
    # in-process lock covers concurrent clicks on this worker; the file-based
    # job registry (shared by all gunicorn workers) covers the rest.
    #
    # #1055: the registry now also carries the refresh-all marker itself, so a
    # run started on ANOTHER gunicorn worker is refused here too. Report it as
    # what it is rather than listing the synthetic job id among the app names.
    if cm.is_refresh_all_active():
        return jsonify({
            'status': 'error',
            'message': 'A refresh of all documentation is already running.',
        }), 409

    active = {k: v for k, v in cm.get_active_jobs().items()
              if k != REFRESH_ALL_JOB_ID}
    if active:
        return jsonify({
            'status': 'error',
            'message': ('A documentation refresh is already running '
                        f'({", ".join(sorted(active))}) — wait for it to finish.'),
        }), 409

    if not _refresh_all_lock.acquire(blocking=False):
        return jsonify({
            'status': 'error',
            'message': 'A refresh of all documentation is already running.',
        }), 409

    # Publish the marker BEFORE the thread starts, so there is no window in
    # which the route has already answered "started" while every reader still
    # sees an idle box.
    cm.set_refresh_all_lock()

    # #982: reindex when the mirror COMPLETES (not at start) — see _refresh_and_reindex.
    try:
        threading.Thread(target=_refresh_all_and_reindex, daemon=True).start()
    except BaseException:
        # The worker never started, so nothing will ever release the lock.
        cm.clear_refresh_all_lock()
        _refresh_all_lock.release()
        raise

    # #525 Name the split up front. "Refresh started for all apps" implied all
    # apps were being fetched; the ones with local_doc and no capture path
    # never were. #824: local_doc apps that carry `capture_method: monolith`
    # DO get a real refresh attempt now (mirror_all calls mirror_docs on
    # every app, including them) — only a local_doc app with no capture path
    # is genuinely "nothing to refresh".
    #
    # #1229 review: this hard-coded `!= monolith`, so #1196 silently broke it.
    # Five of the six moved to llms-txt / wget and were counted as "nothing to
    # refresh" again — the exact #525 dishonesty, re-introduced. `is_local_only`
    # is the ONE predicate for "has no capture path" (cache_manager), and the
    # per-app route above already uses it. The refresh-all tests could not
    # catch it because they were answered 409 first (BLOCKER 2).
    config = cm.get_config()
    local = [a for a in config.get('apps', []) if cm.is_local_only(a)]
    mirrored = len(config.get('apps', [])) - len(local)
    msg = f'Refresh started for {mirrored} mirrored apps.'
    if local:
        msg += (f' {len(local)} ship a box-local page and have no upstream mirror '
                f'({", ".join(sorted(a["id"] for a in local))}).')
    return jsonify({'status': 'success', 'message': msg,
                    'mirrored_apps': mirrored, 'local_doc_apps': len(local)})


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
        # #1055: the admin poller stopped on the first empty `active_jobs`,
        # which happens BETWEEN two per-app mirrors of a refresh-all — it then
        # reloaded and re-enabled every button mid-run. This is the flag that
        # answers "is a full refresh still going", independent of which single
        # app happens to hold a lock at this instant.
        'refresh_all_active': cm.is_refresh_all_active(),
        'last_refresh_all': cm.get_last_refresh_all(),
        # Per-module PASS/FAIL, so a client does not have to re-derive it from
        # the ambiguous `cached` flag (a FAILED capture leaves files behind).
        'failed_modules': cm.get_failed_modules(),
        # #1229: mirrors that ARE served but failed a cosmetic gate rule. Kept
        # separate from failed_modules because "needs attention" and "is
        # broken" are different operator actions — and so the day-1 acceptance
        # tier can go red on either without re-judging the tree itself.
        'degraded_modules': cm.get_degraded_modules(),
        'apps': statuses
    })


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
