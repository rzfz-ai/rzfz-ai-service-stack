# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""
Cache Manager — handles wget mirroring, status tracking, link rewriting
and attribution injection for cached documentation.
"""

import json
import os
import subprocess
import time
import re
from datetime import datetime
from urllib.parse import urlparse

import bleach
import markdown
import requests

BLEACH_ALLOWED_TAGS = bleach.ALLOWED_TAGS | {
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'pre', 'code', 'br', 'hr',
    'div', 'span', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'img', 'details', 'summary',
    'sup', 'sub', 'del', 'ins',
}
BLEACH_ALLOWED_ATTRS = {
    **bleach.ALLOWED_ATTRIBUTES,
    'img': ['src', 'alt', 'title', 'width', 'height'],
    'a': ['href', 'title', 'id', 'name'],
    'td': ['align', 'colspan', 'rowspan'],
    'th': ['align', 'colspan', 'rowspan'],
    'code': ['class'],
    'div': ['class', 'id'],
    'span': ['class', 'id'],
    'h1': ['id'], 'h2': ['id'], 'h3': ['id'], 'h4': ['id'], 'h5': ['id'], 'h6': ['id'],
}

DATA_DIR = os.environ.get('HELP_DATA_DIR', '/data')
CACHE_DIR = os.path.join(DATA_DIR, 'cache')
META_DIR = os.path.join(DATA_DIR, 'meta')
LOCK_DIR = os.path.join(DATA_DIR, 'locks')
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mirror_config.json')

# The own_docs section of the Help-UI is AUTO-DISCOVERED by walking OWN_DOCS_DIR —
# there is no hand-maintained manifest of own_docs. The tree has two layers:
#   - BASE (#125 P1): the COMMUNITY docs (docs/community/**.md) baked into the image
#     at /app/own_docs by the Dockerfile (`COPY docs/community/ own_docs/`). Present
#     on every box, incl. public/Codeberg.
#   - OVERLAY (#125 P1): the gated ENTERPRISE docs, RUNTIME-MOUNTED read-only at
#     /app/own_docs/enterprise (box-local, gitignored — see core/compose.yml +
#     scripts/sync-enterprise-overlay.sh). Absent → community-only.
# Both layers are walked uniformly by discover_own_docs(); the `enterprise/` overlay
# prefix is stripped only when deriving the hub SECTION so Enterprise docs keep their
# Diátaxis grouping. Override the path with HELP_OWN_DOCS_DIR (e.g. to bind-mount a
# doc tree live during authoring).
OWN_DOCS_DIR = os.environ.get(
    'HELP_OWN_DOCS_DIR',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'own_docs'),
)
# The overlay mount prefix (relative to OWN_DOCS_DIR) under which the gated
# Enterprise docs are runtime-mounted. Stripped for SECTION derivation only.
OWN_DOCS_OVERLAY_PREFIX = 'enterprise'
# Images referenced by the docs are copied alongside the markdown under an images/
# directory — either the base tree (own_docs/images/, currently image-less) or the
# Enterprise overlay (own_docs/enterprise/images/**). The /docs-image route serves
# from the OWN_DOCS_DIR root scoped to images/ dirs, so it resolves both. This
# constant names the base-tree images root (kept for reference / external callers).
OWN_DOCS_IMAGES_DIR = os.path.join(OWN_DOCS_DIR, 'images')

# Box-local MODULE help pages (#149). A handful of modules ship upstream docs
# that cannot be reliably wget-mirrored — cross-host CDN assets (broken
# images/CSS on dify/cognee/openhands), SPA sites that yield 0 usable HTML
# (broken subpages on gotenberg/komodo), or an auth/redirect wall (lightrag).
# For those we STOP mirroring and instead serve a curated, box-local markdown
# page authored in-repo at core/help/module_docs/<app_id>.md and baked into the
# image at /app/module_docs. An app opts in with a ``"local_doc": "<file>.md"``
# field on its mirror_config.json entry; view_docs then renders the local page
# (render_local_doc) instead of a mirror. This directory is DELIBERATELY NOT
# /app/own_docs — that path is the auto-discovered customer-doc tree
# (discover_own_docs); mixing module pages in there would surface them as
# "rzfz.ai Guides" cards. Override with HELP_LOCAL_DOCS_DIR for authoring.
LOCAL_DOCS_DIR = os.environ.get(
    'HELP_LOCAL_DOCS_DIR',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'module_docs'),
)

# Pretty labels for the top-level docs/enterprise/<section>/ directories, used to
# group the auto-discovered own-doc cards on the hub page. Unknown directories
# fall back to a title-cased name, so adding a new section needs no code change.
OWN_DOCS_SECTION_LABELS = {
    '': 'Start Here',
    'get-started': 'Get Started',
    'tutorials': 'Tutorials',
    'how-to': 'How-To Guides',
    'identity': 'Identity & SSO',
    'reference': 'Reference',
    'explanation': 'How It Fits Together',
    'troubleshoot': 'Troubleshooting',
}
# Stable ordering of sections on the hub (anything unlisted sorts last, alpha).
OWN_DOCS_SECTION_ORDER = [
    '', 'get-started', 'tutorials', 'how-to', 'identity', 'reference',
    'explanation', 'troubleshoot',
]

# Ensure directories exist
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(META_DIR, exist_ok=True)
os.makedirs(LOCK_DIR, exist_ok=True)

# Sanity gate for accepting a "partial" mirror. wget exit 8 means the server
# issued an error response for SOME URL in the crawl (e.g. a single upstream
# page legitimately 404s within an otherwise-complete doc tree). That is common
# and should NOT be treated as a hard failure — but it must not wave through a
# tree that mirrored the index page only (or nothing). So exit 8 is accepted
# ONLY when the resulting tree still looks like a real doc set: more than the
# index page alone AND above a byte floor. Everything below the gate is a
# genuine partial/broken mirror and is recorded as such (never "Success").
MIRROR_MIN_PAGES = 2          # > index-only: real subpages must exist
MIRROR_MIN_BYTES = 51_200     # 50 KiB floor — a title-page-only tree is smaller


def _slug_from_relpath(relpath: str) -> str:
    """docs/enterprise relative path → URL-safe own-doc id.

    'get-started/install.md' → 'get-started__install'
    'index.md'               → 'index'
    The route /docs/<app_id>/ uses a non-path converter, so the id must not
    contain a slash; we encode directory separators as '__'.
    """
    rel = relpath[:-3] if relpath.endswith('.md') else relpath
    return rel.replace(os.sep, '__').replace('/', '__')


def _relpath_from_slug(slug: str) -> str:
    """Inverse of _slug_from_relpath → on-disk relative .md path."""
    return slug.replace('__', os.sep) + '.md'


def _extract_h1(md_path: str, fallback: str) -> str:
    """First ATX H1 ('# Title') in a markdown file, else fallback."""
    try:
        with open(md_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                s = line.strip()
                if s.startswith('# '):
                    return s[2:].strip()
                # stop scanning after a reasonable header window
        return fallback
    except OSError:
        return fallback


def _extract_audience(md_path: str) -> str:
    """Audience marker for a customer doc — drives role-aware hub visibility.

    Customer docs are **admin** by default (operating/administering the box is a
    stack-admin job). A genuinely end-user-facing page (the tutorials, the odd
    how-to) opts in with an HTML comment near the top so NON-admins also see its
    tile on the hub:  `<!-- audience: end-user -->`
    Accepted values (case-insensitive): end-user / enduser / user / everyone / all.
    """
    try:
        with open(md_path, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(1500)
    except OSError:
        return 'admin'
    m = re.search(r'<!--\s*audience:\s*([a-z\- ]+?)\s*-->', head, re.IGNORECASE)
    if m and m.group(1).strip().lower() in (
            'end-user', 'enduser', 'user', 'everyone', 'all'):
        return 'end-user'
    return 'admin'


class CacheManager:
    def __init__(self):
        self._config = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        """Load mirror_config.json."""
        if self._config is None:
            with open(CONFIG_FILE, 'r') as f:
                self._config = json.load(f)
        return self._config

    def discover_own_docs(self) -> list[dict]:
        """Auto-discover the customer doc tree under OWN_DOCS_DIR.

        Walks the whole /app/own_docs tree — the baked-in COMMUNITY base
        (docs/community/**.md) AND the runtime-mounted ENTERPRISE overlay at
        /app/own_docs/enterprise (#125 P1) — and builds one own-doc entry per
        markdown file. The card title is the file's H1; the `section` is the
        top-level directory (used to group cards on the hub), except that the
        `enterprise/` overlay prefix is stripped for section derivation so the
        gated Enterprise docs keep their Diátaxis grouping (get-started/how-to/…)
        exactly as they did when they were baked in at the own_docs root. The slug
        and on-disk path keep the real relpath (incl. the enterprise/ prefix). No
        hand-maintained manifest — drop a .md in the tree and it appears after the
        next image build / container restart / overlay refresh.

        The `images/` directory is NOT a doc section — it holds the relative
        image assets and is skipped here (served via the /docs-image route). When
        the Enterprise overlay is empty/absent, only the community base is walked.
        """
        if not os.path.isdir(OWN_DOCS_DIR):
            return []

        overlay_prefix = OWN_DOCS_OVERLAY_PREFIX + os.sep
        entries: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(OWN_DOCS_DIR):
            # Never descend into the image asset tree (base OR overlay images/).
            dirnames[:] = [d for d in sorted(dirnames) if d != 'images']
            for fn in sorted(filenames):
                if not fn.endswith('.md'):
                    continue
                full = os.path.join(dirpath, fn)
                relpath = os.path.relpath(full, OWN_DOCS_DIR)
                # Strip the runtime `enterprise/` overlay prefix for SECTION
                # derivation only (slug + on-disk path keep the real relpath).
                section_relpath = (
                    relpath[len(overlay_prefix):]
                    if relpath.startswith(overlay_prefix) else relpath
                )
                # MANIFEST.md under images/ would already be skipped; also skip
                # any stray non-content markdown at the image root just in case.
                section = os.path.dirname(section_relpath).split(os.sep)[0] if os.sep in section_relpath else ''
                if section == 'images':
                    continue
                slug = _slug_from_relpath(relpath)
                title = _extract_h1(full, fallback=os.path.splitext(fn)[0].replace('-', ' ').title())
                entries.append({
                    'id': slug,
                    'name': title,
                    'section': section,
                    'section_label': OWN_DOCS_SECTION_LABELS.get(
                        section, section.replace('-', ' ').title() or 'Start Here'),
                    'relpath': relpath,
                    'description': '',
                    'license': 'Proprietary',
                    'copyright': 'razzfazz.ai',
                    'required_groups': [],
                    'is_own': True,
                    'audience': _extract_audience(full),
                })

        # Order: by section (per OWN_DOCS_SECTION_ORDER, unknown last alpha),
        # then index.md first within a section, then by title.
        def _sort_key(e: dict):
            sec = e['section']
            try:
                sec_rank = OWN_DOCS_SECTION_ORDER.index(sec)
            except ValueError:
                sec_rank = len(OWN_DOCS_SECTION_ORDER)
            is_index = 0 if e['relpath'].rsplit(os.sep, 1)[-1] == 'index.md' else 1
            return (sec_rank, sec, is_index, e['name'].lower())

        entries.sort(key=_sort_key)
        return entries

    def get_app_entry(self, app_id: str) -> dict | None:
        """Look up an app by id (covers both external apps and own_docs)."""
        config = self.get_config()
        for entry in config.get('apps', []):
            if entry['id'] == app_id:
                return entry
        # own_docs are auto-discovered from the docs/enterprise tree.
        for entry in self.discover_own_docs():
            if entry['id'] == app_id:
                return entry
        return None

    # ------------------------------------------------------------------
    # Cache Paths
    # ------------------------------------------------------------------

    def get_cache_path(self, app_id: str) -> str | None:
        """Return the root filesystem path for a cached app's content."""
        app_cache = os.path.join(CACHE_DIR, app_id)
        if not os.path.isdir(app_cache):
            return None
        return app_cache

    def get_entry_path(self, app_id: str) -> str:
        """Return the relative entry path within the cache (from the mirror URL).
        Respects an optional 'entry_path' override in the config."""
        app_entry = self.get_app_entry(app_id)
        if not app_entry:
            return ''
        # Explicit override takes precedence
        if app_entry.get('entry_path'):
            return app_entry['entry_path']
        mirror_url = app_entry.get('mirror_url', '')
        if not mirror_url:
            return ''
        parsed = urlparse(mirror_url)
        # Strip leading/trailing slashes, e.g. /docs/ → docs
        return parsed.path.strip('/')

    def get_domain_map(self) -> dict[str, str]:
        """Build a mapping from original base URLs to local /docs/<app_id> paths."""
        config = self.get_config()
        mapping = {}
        for entry in config.get('apps', []):
            mirror_url = entry.get('mirror_url', '')
            if mirror_url:
                parsed = urlparse(mirror_url)
                base_url = f'{parsed.scheme}://{parsed.netloc}'
                mapping[base_url] = f'/docs/{entry["id"]}'
        return mapping

    # ------------------------------------------------------------------
    # Cache Status
    # ------------------------------------------------------------------

    def get_app_cache_status(self, app_id: str) -> dict:
        """Return cache status for an app."""
        # Box-local module pages (#149) are never wget-mirrored — they render
        # from core/help/module_docs and are always available offline. Report a
        # synthetic "available, local page" status so the hub shows them as
        # ready (not "⚠ Not cached") and the admin page doesn't imply a stale
        # mirror. `local_doc` flags them for a distinct admin badge.
        app_entry = self.get_app_entry(app_id)
        if app_entry and app_entry.get('local_doc'):
            return {
                'cached': True,
                'last_updated': None,
                'size_mb': 0,
                'refreshing': False,
                'error': None,
                'partial': False,
                'exit_code': None,
                'page_count': None,
                'local_doc': True,
            }

        meta_file = os.path.join(META_DIR, f'{app_id}.json')
        app_cache = os.path.join(CACHE_DIR, app_id)

        result = {
            'cached': os.path.isdir(app_cache) and bool(os.listdir(app_cache)),
            'last_updated': None,
            'size_mb': 0,
            'refreshing': self.is_job_active(app_id),
            'error': None,
            # Mirror-hygiene fields (surfaced in the admin cache page): a mirror
            # that failed the sanity gate is `partial` (and carries an `error`)
            # so a broken/title-page-only tree is visible instead of "Success".
            'partial': False,
            'exit_code': None,
            'page_count': None,
        }

        if os.path.isfile(meta_file):
            with open(meta_file, 'r') as f:
                meta = json.load(f)
            result['last_updated'] = meta.get('last_updated')
            result['error'] = meta.get('error')
            result['partial'] = meta.get('partial', False)
            result['exit_code'] = meta.get('exit_code')
            result['page_count'] = meta.get('page_count')

        if result['cached']:
            result['size_mb'] = round(self._dir_size(app_cache) / (1024 * 1024), 1)

        return result

    def _dir_size(self, path: str) -> int:
        """Calculate total size of files in a directory tree."""
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total

    def _count_html_pages(self, path: str) -> int:
        """Count mirrored HTML pages under a cache dir.

        Used as the sanity-gate signal for accepting a wget-exit-8 mirror: a
        real doc tree has more than the single index page, so a count of <= 1
        means the mirror is title-page-only / broken and must not be served as
        good. Only .html/.htm are counted (assets like CSS/JS/images don't
        prove the doc navigation mirrored)."""
        n = 0
        for _dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                if f.lower().endswith(('.html', '.htm')):
                    n += 1
        return n

    # ------------------------------------------------------------------
    # Internet Check
    # ------------------------------------------------------------------

    def check_internet(self) -> bool:
        """Quick internet connectivity check via HTTP."""
        try:
            r = requests.get('http://clients3.google.com/generate_204', timeout=5)
            return r.status_code == 204
        except Exception:
            try:
                r = requests.get('https://httpbin.org/ip', timeout=5)
                return r.status_code == 200
            except Exception:
                return False

    # ------------------------------------------------------------------
    # Job Management (file-based for multi-worker gunicorn)
    # ------------------------------------------------------------------

    def is_job_active(self, app_id: str) -> bool:
        lock = os.path.join(LOCK_DIR, f'{app_id}.lock')
        if not os.path.isfile(lock):
            return False
        # Stale lock protection: if lock older than 35 minutes, ignore it
        try:
            age = time.time() - os.path.getmtime(lock)
            return age < 2100
        except OSError:
            return False

    def get_active_jobs(self) -> dict:
        jobs = {}
        try:
            for f in os.listdir(LOCK_DIR):
                if f.endswith('.lock'):
                    app_id = f[:-5]
                    if self.is_job_active(app_id):
                        jobs[app_id] = 'mirroring'
        except OSError:
            pass
        return jobs

    def _set_lock(self, app_id: str):
        lock = os.path.join(LOCK_DIR, f'{app_id}.lock')
        with open(lock, 'w') as f:
            f.write(str(time.time()))

    def _clear_lock(self, app_id: str):
        lock = os.path.join(LOCK_DIR, f'{app_id}.lock')
        try:
            os.remove(lock)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Mirroring
    # ------------------------------------------------------------------

    def mirror_docs(self, app_id: str) -> bool:
        """Mirror documentation for a single app using wget."""
        app_entry = self.get_app_entry(app_id)
        if not app_entry:
            return False

        # Box-local module page (#149): nothing to mirror. The upstream docs of
        # these modules can't be reliably wget-mirrored, so we ship a curated
        # local page instead (served by render_local_doc). Treat as a success
        # no-op so autowarm / the admin Refresh button don't keep re-fetching a
        # broken tree. NOTE: mirror_url is intentionally KEPT on the entry — it
        # is the "Full upstream documentation" link, not a fetch target.
        if app_entry.get('local_doc'):
            return True

        mirror_url = app_entry.get('mirror_url', '')
        if not mirror_url:
            return False

        self._set_lock(app_id)
        meta_file = os.path.join(META_DIR, f'{app_id}.json')
        app_cache = os.path.join(CACHE_DIR, app_id)

        try:
            # Ensure output directory exists
            os.makedirs(app_cache, exist_ok=True)

            # Build wget command
            cmd = [
                'wget',
                '--mirror',
                '--convert-links',
                '--adjust-extension',
                '--page-requisites',
                '--no-parent',
                '--no-host-directories',
                '--directory-prefix', app_cache,
                '--timeout=30',
                '--tries=3',
                '--wait=0.5',
                '--random-wait',
                '--user-agent', 'Mozilla/5.0 (compatible; razzfazz-help-cache/1.0)',
                '--reject', 'robots.txt',
                '-e', 'robots=off',
                '--no-verbose',
            ]

            # Add depth limit if configured
            depth = app_entry.get('depth')
            if depth:
                cmd.extend(['--level', str(depth)])

            # Exclude directories
            exclude_dirs = app_entry.get('exclude_directories', [])
            for d in exclude_dirs:
                cmd.extend(['--exclude-directories', d])

            # Reject patterns
            reject_patterns = app_entry.get('reject_patterns', [])
            if reject_patterns:
                cmd.extend(['--reject-regex', '|'.join(reject_patterns)])

            cmd.append(mirror_url)

            print(f"[help-cache] Mirroring {app_id}: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

            # wget exit codes (see man wget "Exit Status"):
            #   0 = clean success — every requested URL fetched OK.
            #   4 = network failure — the mirror is very likely truncated.
            #   6 = username/password authentication failure — what got written
            #       is the basic-auth challenge, NOT the docs (the LightRAG
            #       "username/password" prompt symptom).
            #   8 = the server returned an error response for SOME URL (a 404/500
            #       chain). Common and often benign (one upstream page 404s inside
            #       an otherwise-complete tree) — but it also produces the
            #       title-page-only mirrors (Komodo / Gotenberg / Dify) when the
            #       crawl fell over early.
            # Historically ALL of {0,4,6,8} were recorded as "Success", so partial
            # / auth-prompt / truncated trees were served as if complete. Now:
            #   * 0            → clean success (partial=False).
            #   * 8            → accepted ONLY if the tree passes the sanity gate
            #                    (real subpages + above the byte floor); flagged
            #                    partial=True either way so it's visible.
            #   * 4, 6, other  → hard failure (partial, retried on next refresh).
            returncode = result.returncode
            page_count = self._count_html_pages(app_cache)
            size_bytes = self._dir_size(app_cache)
            partial = returncode != 0
            fail_reason = None

            if returncode == 0:
                success = True
            elif returncode == 8:
                if page_count >= MIRROR_MIN_PAGES and size_bytes >= MIRROR_MIN_BYTES:
                    # Some URL 404'd, but the tree still looks like real docs.
                    success = True
                else:
                    success = False
                    fail_reason = (
                        f'wget exit 8 (server error) and the mirror failed the '
                        f'sanity gate: {page_count} HTML page(s), {size_bytes} '
                        f'bytes (need >= {MIRROR_MIN_PAGES} pages and '
                        f'>= {MIRROR_MIN_BYTES} bytes) — likely title-page-only'
                    )
            elif returncode == 6:
                success = False
                fail_reason = ('wget exit 6 (authentication failure) — the mirror '
                               'is a basic-auth challenge, not the documentation')
            elif returncode == 4:
                success = False
                fail_reason = ('wget exit 4 (network failure) — the mirror is '
                               'likely truncated')
            else:
                success = False
                fail_reason = f'wget exit {returncode}'

            # Human-visible error (surfaced in the admin cache page + container
            # logs). Accepted mirrors (incl. accepted-partial exit 8) carry no
            # error; only genuine failures do.
            if success:
                error = None
            else:
                error = fail_reason or (
                    result.stderr[-500:] if result.stderr else 'Unknown error')
                if fail_reason and result.stderr:
                    error = f'{fail_reason} | {result.stderr[-300:]}'

            meta = {
                'last_updated': datetime.utcnow().isoformat() + 'Z',
                'mirror_url': mirror_url,
                'exit_code': returncode,
                'page_count': page_count,
                'size_bytes': size_bytes,
                'partial': partial,
                'success': success,
                'error': error,
            }

            with open(meta_file, 'w') as f:
                json.dump(meta, f, indent=2)

            status_word = 'Success' if success else ('Partial' if partial else 'Failed')
            print(f"[help-cache] {status_word} mirroring {app_id} "
                  f"(exit {returncode}, {page_count} HTML pages, {size_bytes} bytes, "
                  f"partial={partial})")
            return success

        except subprocess.TimeoutExpired:
            meta = {
                'last_updated': datetime.utcnow().isoformat() + 'Z',
                'mirror_url': mirror_url,
                'exit_code': None,
                'page_count': self._count_html_pages(app_cache),
                'partial': True,
                'success': False,
                'error': 'Mirror timed out after 30 minutes',
            }
            with open(meta_file, 'w') as f:
                json.dump(meta, f, indent=2)
            print(f"[help-cache] Timeout mirroring {app_id}")
            return False

        except Exception as e:
            meta = {
                'last_updated': datetime.utcnow().isoformat() + 'Z',
                'mirror_url': mirror_url,
                'exit_code': None,
                'page_count': self._count_html_pages(app_cache),
                'partial': True,
                'success': False,
                'error': str(e),
            }
            with open(meta_file, 'w') as f:
                json.dump(meta, f, indent=2)
            print(f"[help-cache] Error mirroring {app_id}: {e}")
            return False

        finally:
            self._clear_lock(app_id)

    def mirror_all(self) -> dict:
        """Mirror all configured apps. Returns status dict."""
        config = self.get_config()
        results = {}
        for app_entry in config.get('apps', []):
            app_id = app_entry['id']
            results[app_id] = self.mirror_docs(app_id)
        return results

    # ------------------------------------------------------------------
    # Own Docs (Markdown)
    # ------------------------------------------------------------------

    def render_own_doc(self, doc_id: str) -> str | None:
        """Render a customer markdown doc (docs/enterprise/**.md) to safe HTML.

        `doc_id` is the auto-discovery slug (see _slug_from_relpath). Supports:
          - ```mermaid fenced blocks → <pre class="mermaid"> so the vendored,
            offline mermaid.min.js (loaded by viewer.html) renders them. The
            fence content is preserved verbatim and survives bleach.
          - relative image links (../images/<topic>/x.png, images/x.png) →
            rewritten to the /docs-image/<path> route that serves the baked-in
            docs/enterprise/images tree.
        """
        # Resolve slug → on-disk path, guarding against traversal.
        relpath = _relpath_from_slug(doc_id)
        md_path = os.path.realpath(os.path.join(OWN_DOCS_DIR, relpath))
        root = os.path.realpath(OWN_DOCS_DIR)
        if md_path != root and not md_path.startswith(root + os.sep):
            return None
        if not os.path.isfile(md_path):
            return None

        with open(md_path, 'r', encoding='utf-8') as f:
            md_content = f.read()

        return self._render_markdown(md_content, os.path.dirname(relpath))

    def render_local_doc(self, app_id: str) -> str | None:
        """Render a box-local MODULE help page (#149) to safe HTML.

        These replace the upstream doc mirror for modules whose docs can't be
        reliably wget-mirrored (dify, cognee, openhands, gotenberg, komodo,
        lightrag). The app opts in via ``"local_doc": "<file>.md"`` on its
        mirror_config.json entry; the markdown lives under LOCAL_DOCS_DIR
        (core/help/module_docs, baked to /app/module_docs). Rendered through the
        same markdown/bleach pipeline as the customer own-docs. Returns None if
        the app has no local_doc or the file is missing / outside the tree.
        """
        entry = self.get_app_entry(app_id)
        if not entry or not entry.get('local_doc'):
            return None
        fname = entry['local_doc']
        # Resolve the configured filename under LOCAL_DOCS_DIR, guarding traversal.
        md_path = os.path.realpath(os.path.join(LOCAL_DOCS_DIR, fname))
        root = os.path.realpath(LOCAL_DOCS_DIR)
        if md_path != root and not md_path.startswith(root + os.sep):
            return None
        if not os.path.isfile(md_path):
            return None
        with open(md_path, 'r', encoding='utf-8') as f:
            md_content = f.read()
        # These curated pages use only ABSOLUTE links (the box app URL + the
        # upstream docs URL) and no relative image assets, so doc_dir=''.
        return self._render_markdown(md_content, '')

    def _render_markdown(self, md_content: str, doc_dir: str) -> str:
        """Shared markdown → safe-HTML core for own-docs AND module pages.

        `doc_dir` is the on-disk directory of the source .md relative to its
        docs root — used ONLY to resolve RELATIVE image (`../images/…`) and
        inter-doc (`foo.md`) links to the /docs-image and /docs/<slug>/ routes.
        Pass '' for pages that use only absolute links (module pages). Handles
        <domain>/<server-ip> placeholders, ```mermaid fences, and bleaches.
        """
        # Replace domain placeholders with the real configured domain
        main_domain = os.environ.get('MAIN_DOMAIN', '')
        if main_domain:
            md_content = md_content.replace('<your-domain>', main_domain)
            md_content = md_content.replace('<domain>', main_domain)

        # Replace IP placeholders with the real host IP
        host_ip = os.environ.get('HOST_IP', '')
        if host_ip:
            md_content = md_content.replace('<server-ip>', host_ip)
            md_content = md_content.replace('192.168.1.100', host_ip)

        # Pull ```mermaid fenced blocks out BEFORE the markdown pass so the
        # graph DSL is never HTML-escaped or code-highlighted, then re-insert
        # as <pre class="mermaid"> placeholders that bleach is told to keep.
        mermaid_blocks: list[str] = []

        def _stash_mermaid(m):
            mermaid_blocks.append(m.group(1))
            return f'\n\nMERMAIDPLACEHOLDER{len(mermaid_blocks) - 1}ENDPLACEHOLDER\n\n'

        md_content = re.sub(
            r'```mermaid[ \t]*\n(.*?)\n```',
            _stash_mermaid, md_content, flags=re.DOTALL,
        )

        html = markdown.markdown(
            md_content, extensions=['tables', 'fenced_code', 'toc', 'attr_list'])

        # Rewrite relative image sources to the served image route. The doc lives
        # at docs/enterprise/<relpath>; images live at docs/enterprise/images/. A link
        # like ../images/foo/bar.png from how-to/x.md resolves to images/foo/bar.png.
        def _rewrite_img(m):
            attr, src = m.group(1), m.group(2)
            if src.startswith(('http://', 'https://', '/docs-image/', 'data:')):
                return m.group(0)
            resolved = os.path.normpath(os.path.join(doc_dir, src))
            resolved = resolved.replace(os.sep, '/')
            if resolved.startswith('..'):
                return m.group(0)  # outside the tree — leave as-is
            # The /docs-image route serves from the OWN_DOCS_DIR root (scoped to
            # images/ dirs), so emit the path relative to own_docs UNCHANGED. This
            # resolves both the baked base tree (images/…) and the runtime
            # Enterprise overlay (enterprise/images/…, #125 P1).
            return f'{attr}/docs-image/{resolved}'

        html = re.sub(r'(<img[^>]*\bsrc=["\'])([^"\']+)', _rewrite_img, html, flags=re.IGNORECASE)

        # Rewrite relative inter-doc links (`get-started/requirements.md`,
        # `../how-to/backup.md`, `./install.md#step`) to the /docs/<slug>/ route.
        # Without this, a relative .md href resolves against the current page URL
        # (e.g. /docs/index/get-started/requirements.md) and navigates back to the
        # same page instead of the target doc.
        def _rewrite_link(m):
            attr, href = m.group(1), m.group(2)
            if href.startswith(('http://', 'https://', '/', '#', 'mailto:', 'tel:', 'data:')):
                return m.group(0)
            base, frag = href, ''
            for sep in ('#', '?'):
                if sep in base:
                    i = base.index(sep)
                    frag = base[i:] + frag
                    base = base[:i]
            if not base.endswith('.md'):
                return m.group(0)
            resolved = os.path.normpath(os.path.join(doc_dir, base)).replace(os.sep, '/')
            if resolved.startswith('..'):
                return m.group(0)  # outside the own_docs tree — leave as-is
            return f'{attr}/docs/{_slug_from_relpath(resolved)}/{frag}'

        html = re.sub(r'(<a[^>]*\bhref=["\'])([^"\']+)', _rewrite_link, html, flags=re.IGNORECASE)

        clean = bleach.clean(html, tags=BLEACH_ALLOWED_TAGS, attributes=BLEACH_ALLOWED_ATTRS)

        # Re-insert mermaid blocks as <pre class="mermaid"> (bleach already
        # allows <pre> + the class attr). The DSL text is HTML-escaped so it is
        # safe, and mermaid reads textContent which un-escapes it at render time.
        import html as _htmlmod
        for i, block in enumerate(mermaid_blocks):
            placeholder = f'MERMAIDPLACEHOLDER{i}ENDPLACEHOLDER'
            pre = f'<pre class="mermaid">{_htmlmod.escape(block)}</pre>'
            # The placeholder may have been wrapped in <p>…</p> by markdown.
            clean = clean.replace(f'<p>{placeholder}</p>', pre).replace(placeholder, pre)
        return clean

    # ------------------------------------------------------------------
    # Link Rewriting
    # ------------------------------------------------------------------

    def rewrite_links(self, html_content: str, app_id: str) -> str:
        """Rewrite external doc domain URLs to local /docs/<app_id> paths."""
        domain_map = self.get_domain_map()
        for base_url, local_path in domain_map.items():
            # Replace absolute URLs: https://docs.example.com/page → /docs/appid/page
            html_content = html_content.replace(base_url + '/', local_path + '/')
            html_content = html_content.replace(base_url + '"', local_path + '/"')
            html_content = html_content.replace(base_url + "'", local_path + "/'")
        return html_content

    def rewrite_root_paths(self, html_content: str, app_id: str) -> str:
        """
        Rewrite root-absolute paths (/assets/..., /img/...) to /docs/<app_id>/...
        so they resolve correctly when served under our /docs/<app_id>/ prefix.
        Handles both quoted and unquoted HTML attribute values.
        """
        prefix = f'/docs/{app_id}'

        def _replacer(match):
            pre = match.group(1)
            path = match.group(2)
            # Don't rewrite if already has our prefix, or is /static/ (Flask)
            if path.startswith('/docs/') or path.startswith('/static/'):
                return match.group(0)
            return f'{pre}{prefix}{path}'

        # Quoted: href="/path" or src='/path' (not href="//cdn...")
        html_content = re.sub(
            r'((?:href|src|action|poster)\s*=\s*["\'])(/(?!/)[^"\'>]*)',
            _replacer, html_content, flags=re.IGNORECASE
        )
        # Unquoted: href=/path or src=/path
        html_content = re.sub(
            r'((?:href|src|action|poster)\s*=\s*)(/(?!/|["\'])[^\s>]*)',
            _replacer, html_content, flags=re.IGNORECASE
        )
        # CSS url() with root-absolute paths: url(/img/logo.png)
        html_content = re.sub(
            r'(url\s*\(\s*["\']?)(/(?!/)[^"\')\s]*)',
            _replacer, html_content, flags=re.IGNORECASE
        )
        return html_content

    def suppress_docusaurus_banner(self, html_content: str) -> str:
        """
        Inject a script to suppress the Docusaurus 'baseUrl mismatch' error banner.
        Docusaurus checks: void 0 === window.docusaurus && insertBanner()
        Setting window.docusaurus to a truthy value prevents the banner.
        """
        marker = '<script>window.docusaurus={}</script>'
        if 'docusaurus' not in html_content.lower():
            return html_content
        # Inject right after <head> or at the very start
        head_idx = html_content.lower().find('<head')
        if head_idx != -1:
            close_idx = html_content.find('>', head_idx)
            if close_idx != -1:
                pos = close_idx + 1
                return html_content[:pos] + marker + html_content[pos:]
        return html_content

    def suppress_readthedocs_redirect(self, html_content: str) -> str:
        """
        Neutralise the Read-the-Docs canonical-redirect + analytics/flyout JS so a
        mirrored RTD page doesn't bounce the offline copy back to the cloud.

        Read-the-Docs-hosted docs (e.g. paperless-ngx) ship several things that
        break an offline mirror served under our /docs/<app_id>/ prefix:
          * a ``<link rel="canonical" ...>`` — on a modern RTD build this is
            usually a LOCAL ``href="faq.html"`` (per-page), so removing it does
            NOT by itself disarm the bounce; we still drop it for hygiene;
          * the RTD **Addons loader** ``<script async src="…/readthedocs-addons.js">``
            plus the inline ``<script id="READTHEDOCS_DATA">{…}</script>`` config
            blob — together these drive the version flyout AND the cloud redirect;
          * the ``readthedocs-doc-embed`` / analytics ``<meta>`` + script (phones home);
          * an inline ``window.location.replace(...)`` / ``window.location.href = …``
            redirect that fires from a plain ``<script>`` which need NOT itself
            mention "readthedocs" — so the readthedocs-script regex misses it and
            the page still bounces. THIS is the paperless "every page redirects to
            the cloud" symptom (#149).

        We STRIP the RTD elements and, belt-and-suspenders, DEFUSE any
        ``window.location`` navigation inside the remaining ``<script>`` blocks —
        the same HTML-rewrite intent as ``suppress_docusaurus_banner``. Scoped:
        a no-op unless the page carries RTD markers; the doc body is untouched.
        Applied in the same rewrite path as the Docusaurus banner (see
        ``inject_attribution``)."""
        low = html_content.lower()
        if 'readthedocs' not in low and 'rtd-' not in low:
            return html_content

        # 1. Drop <link rel="canonical" ...> (any attribute order). Hygiene: on a
        #    modern RTD build this is a LOCAL per-page href, so it isn't the bounce
        #    hook — step 4 handles the actual redirect — but a cloud canonical on
        #    older builds IS, so we still remove it.
        html_content = re.sub(
            r'<link\b[^>]*\brel\s*=\s*["\']?canonical["\']?[^>]*>',
            '', html_content, flags=re.IGNORECASE,
        )
        # 2. Drop the readthedocs-analytics <meta> tag.
        html_content = re.sub(
            r'<meta\b[^>]*readthedocs-analytics[^>]*>',
            '', html_content, flags=re.IGNORECASE,
        )
        # 3. Drop any <script> whose opening tag OR body references readthedocs:
        #    the external embed/flyout/analytics loaders (readthedocs-doc-embed.js,
        #    assets.readthedocs.org, …), the Addons loader (readthedocs-addons.js),
        #    AND the inline config/redirect blobs (id="READTHEDOCS_DATA",
        #    readthedocs-addons-data, inline snippets that perform the canonical
        #    bounce). Tempered dot so a single match never swallows past the first
        #    </script> into unrelated markup.
        html_content = re.sub(
            r'<script\b(?:(?!</script>).)*?readthedocs(?:(?!</script>).)*?</script>',
            '', html_content, flags=re.IGNORECASE | re.DOTALL,
        )
        # 3b. Explicit belt-and-suspenders drop of the RTD Addons loader by src,
        #     covering both the paired <script…></script> form and a rare
        #     self-closing tag — even if step 3's tempered-dot ever missed it.
        html_content = re.sub(
            r'<script\b[^>]*\breadthedocs-addons\.js[^>]*>(?:\s*</script>)?',
            '', html_content, flags=re.IGNORECASE,
        )
        # 4. DEFUSE any window.location navigation inside the SURVIVING <script>
        #    blocks. The real paperless redirect is an inline
        #    `window.location.replace(<cloud-url>)` (or `window.location.href = …`)
        #    in a plain <script> that carries no "readthedocs" token, so steps 1–3
        #    leave it intact and the offline copy still bounces. We rewrite the
        #    navigation call/assignment to a harmless no-op — only within <script>
        #    bodies and only on RTD-marked pages, so document prose that merely
        #    mentions the string is never touched. The `=(?!=)` guards keep JS
        #    comparisons (`==`, `===`) working.
        def _defuse_location(m):
            block = m.group(0)
            block = re.sub(r'window\.location\.(?:replace|assign)\s*\(',
                           'void (', block, flags=re.IGNORECASE)
            block = re.sub(r'window\.location\.href\s*=(?!=)',
                           'window.__rtdBlockedHref =', block, flags=re.IGNORECASE)
            block = re.sub(r'window\.location\s*=(?!=)',
                           'window.__rtdBlockedLoc =', block, flags=re.IGNORECASE)
            return block

        html_content = re.sub(
            r'<script\b[^>]*>.*?</script>',
            _defuse_location, html_content, flags=re.IGNORECASE | re.DOTALL,
        )
        return html_content

    # ------------------------------------------------------------------
    # Full-Page Injection (preserves original layout + CSS/JS)
    # ------------------------------------------------------------------

    def rewrite_relative_paths(self, html_content: str, app_id: str, filepath: str) -> str:
        """
        Fix relative paths that break due to the /docs/<app_id>/ URL prefix.

        wget --convert-links produces relative paths (e.g. ../assets/...) that
        resolve correctly on the filesystem but not in the browser when pages
        are served under /docs/<app_id>/<entry_path>/.  This method converts
        relative ../  paths to absolute /docs/<app_id>/ paths so the browser
        can resolve them correctly.
        """
        if not filepath:
            return html_content

        # Determine the directory of the currently served file within the cache
        # e.g. filepath="docs/intro" → file_dir="docs"
        file_dir = os.path.dirname(filepath.rstrip('/'))

        prefix = f'/docs/{app_id}'

        def _resolve_relative(match):
            attr_prefix = match.group(1)  # e.g. href="  or src="
            rel_path = match.group(2)     # e.g. ../assets/css/styles.css

            # Resolve the relative path against the file's directory in the cache
            resolved = os.path.normpath(os.path.join(file_dir, rel_path))
            # Avoid path traversal out of cache root
            if resolved.startswith('..'):
                resolved = ''
            return f'{attr_prefix}{prefix}/{resolved}'

        # Match href/src with relative paths starting with ../ or ./
        html_content = re.sub(
            r'((?:href|src|action|poster)\s*=\s*["\'])(\.\./[^"\'>]*)',
            _resolve_relative, html_content, flags=re.IGNORECASE
        )
        # CSS url() with relative paths
        html_content = re.sub(
            r'(url\s*\(\s*["\']?)(\.\./[^"\')\s]*)',
            _resolve_relative, html_content, flags=re.IGNORECASE
        )
        return html_content

    def inject_attribution(self, html_content: str, app_entry: dict, app_id: str, filepath: str = '') -> str:
        """
        Inject a small floating attribution bar into the cached HTML page.
        Preserves the original page layout, CSS, and JavaScript.
        Also rewrites links to point to local cache.
        """
        # Rewrite external links to local paths
        html_content = self.rewrite_links(html_content, app_id)
        # Rewrite root-absolute paths (/assets/...) to work under /docs/<app_id>/
        html_content = self.rewrite_root_paths(html_content, app_id)
        # Fix relative paths that break due to URL prefix depth mismatch
        html_content = self.rewrite_relative_paths(html_content, app_id, filepath)
        # Suppress Docusaurus baseUrl error banner
        html_content = self.suppress_docusaurus_banner(html_content)
        # Strip Read-the-Docs canonical-redirect + analytics/flyout JS so a
        # mirrored RTD page (paperless-ngx, …) doesn't bounce to the cloud.
        html_content = self.suppress_readthedocs_redirect(html_content)

        license_name = app_entry.get('license', 'Unknown')
        mirror_url = app_entry.get('mirror_url', '#')
        app_name = app_entry.get('name', 'Unknown')

        attribution_html = f'''
<div id="razzfazz-help-bar" style="
    position:fixed; bottom:0; left:0; right:0; z-index:999999;
    background:linear-gradient(135deg,#f0f0f0,#e8e8e8);
    border-top:3px solid #CD1719;
    padding:6px 20px;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    font-size:12px; color:#333;
    display:flex; align-items:center; justify-content:space-between;
">
    <span>
        📖 Cached from <a href="{mirror_url}" target="_blank" rel="noopener"
            style="color:#CD1719;text-decoration:none;">{app_name}</a>
        · License: {license_name}
        · <a href="/" style="color:#CD1719;text-decoration:none;">← Help Center</a>
    </span>
    <button onclick="this.parentElement.style.display='none'"
        style="background:none;border:none;cursor:pointer;font-size:16px;color:#999;">✕</button>
</div>
<style>#razzfazz-help-bar ~ * {{ padding-bottom: 35px; }} body {{ padding-bottom: 40px !important; }}</style>
'''

        # Inject before </body>
        if '</body>' in html_content.lower():
            idx = html_content.lower().rfind('</body>')
            return html_content[:idx] + attribution_html + html_content[idx:]
        else:
            return html_content + attribution_html
