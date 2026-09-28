# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""
Cache Manager — handles wget mirroring, status tracking, link rewriting
and attribution injection for cached documentation.
"""

import html
import json
import os
import shutil
import subprocess
import tempfile
import types
import time
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlsplit

import bleach
import markdown
import requests

# #1196 — sibling modules of the help app (all live in /app in the image and
# in core/help/ in the repo). gunicorn runs with /app on sys.path; a test
# that loads this file standalone via importlib (test_1072 does) has not put
# core/help there, so fall back to this file's own directory.
try:
    import markdown_tree as mtree
    import mirror_assets
    import mirror_quality
    from mirror_fetch import HttpFetcher
except ModuleNotFoundError:  # pragma: no cover — standalone import only
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import markdown_tree as mtree
    import mirror_assets
    import mirror_quality
    from mirror_fetch import HttpFetcher

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
    # #1196: `admonition` titles (`<p class="admonition-title">`) and
    # `<details open>` from the MDX cleanup need these to keep their styling.
    'p': ['class'],
    'details': ['class', 'open'],
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

# HLP-11: the only schemes an attribution/upstream link may carry. Anything
# else — `javascript:`, `data:`, a relative path — collapses to '#', a dead
# anchor. Mirrored pages are third-party HTML served on the Help Center's own
# origin, so a live `javascript:` href there would run in the authenticated
# session (the CSP added for HLP-1 blocks it, but the link has no business
# existing either way).
SAFE_URL_SCHEMES = ('http', 'https')


def _safe_http_url(url) -> str:
    """Return `url` if it is an absolute http(s) URL, else '#'."""
    if not url or not isinstance(url, str):
        return '#'
    try:
        parsed = urlparse(url)
    except ValueError:
        return '#'
    if parsed.scheme.lower() not in SAFE_URL_SCHEMES or not parsed.netloc:
        return '#'
    return url

# ---------------------------------------------------------------------------
# #1055 — the Help Center taxonomy is a CLOSED SET of five reader-intent
# buckets. Before this, the top level was whatever directory names happened to
# exist under docs/enterprise/, which is how the redundant "Tutorials" +
# "How-To Guides" pair (and five more near-synonym tabs) grew unnoticed.
#
# The rule now: a doc is placed by ONE declared `section:` value, and the only
# legal values are the ids below. Nothing else can become a top-level tab —
# `tests/unit/razzfazz-help/test_982_nav_structure.py` fails the build if any
# own-doc declares a section outside this set, or if the nav grows a top-level
# entry that is not one of these buckets.
#
#   get-started  the single onboarding / first-run path
#   guides       every goal-driven, step-by-step page (the old tutorials +
#                how-to + identity + troubleshoot, merged)
#   reference    lookup material (config, env, APIs, limits)
#   apps         per-app help + the module docs, one entry per enabled module
#   concepts     architecture / explanation ("how it fits together")
OWN_DOCS_SECTION_LABELS = {
    'get-started': 'Get started',
    'guides': 'Guides',
    'reference': 'Reference',
    'concepts': 'Concepts',
    'apps': 'Apps & Modules',
}
# Stable top-level ordering. `apps` sits last: it is where the external app
# mirrors land, and build_topics() appends that topic after the own-doc ones.
OWN_DOCS_SECTION_ORDER = [
    'get-started', 'guides', 'reference', 'concepts', 'apps',
]
#: The closed set itself — the single source of truth for "is this a legal
#: bucket?" (used by discover_own_docs(), app.build_topics() and the guard).
OWN_DOCS_SECTIONS = frozenset(OWN_DOCS_SECTION_ORDER)
#: Where an own-doc lands when it declares nothing legal. `guides` is the
#: catch-all for goal-driven pages, and it keeps a stray doc INSIDE the closed
#: set — the UI can never sprout a rogue tab; the guard test is what surfaces
#: the mis-tagged file.
OWN_DOCS_DEFAULT_SECTION = 'guides'
#: Historical on-disk directory → bucket. The doc tree keeps its original paths
#: (slugs, deep links and cross-doc links stay valid — this pass is IA-only, it
#: does not move or rewrite content), so the directory is only a FALLBACK for a
#: file that carries no `<!-- section: … -->` marker, e.g. an enterprise overlay
#: mounted from an older release.
OWN_DOCS_LEGACY_SECTION_DIRS = {
    '': 'get-started',
    'get-started': 'get-started',
    'tutorials': 'guides',
    'how-to': 'guides',
    'howtos': 'guides',
    'identity': 'guides',
    'troubleshoot': 'guides',
    'reference': 'reference',
    'explanation': 'concepts',
}

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


def _extract_section(md_path: str, section_relpath: str) -> str:
    """Taxonomy bucket for a customer doc — drives the top-level nav (#1055).

    Placement is DECLARED, once, near the top of the file:
      `<!-- section: guides -->`
    and the only legal values are the OWN_DOCS_SECTIONS bucket ids. A value
    outside the closed set is ignored here (the nav must not grow a tab from a
    typo) and caught by the guard test in test_982_nav_structure.py.

    A file with no marker falls back to its historical top-level directory via
    OWN_DOCS_LEGACY_SECTION_DIRS, then to OWN_DOCS_DEFAULT_SECTION — so an
    older enterprise overlay still groups sensibly and always legally.
    """
    try:
        with open(md_path, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(1500)
    except OSError:
        head = ''
    m = re.search(r'<!--\s*section:\s*([a-z0-9\-]+)\s*-->', head, re.IGNORECASE)
    if m and m.group(1).strip().lower() in OWN_DOCS_SECTIONS:
        return m.group(1).strip().lower()
    directory = (section_relpath.split(os.sep)[0]
                 if os.sep in section_relpath else '')
    return OWN_DOCS_LEGACY_SECTION_DIRS.get(
        directory, OWN_DOCS_DEFAULT_SECTION)


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


#: #525 mirror_docs outcomes. A bool cannot distinguish "fetched the docs" from
#: "there is nothing here to fetch", and the admin Refresh button reported the
#: second as the first — so six modules looked refreshed while having no mirror
#: at all. Strings rather than a bool because the operator-facing message needs
#: to say WHICH.
MIRROR_MIRRORED = 'mirrored'
MIRROR_SKIPPED_LOCAL = 'skipped-local-doc'
MIRROR_FAILED = 'failed'
MIRROR_NO_URL = 'no-mirror-url'
MIRROR_UNKNOWN_APP = 'unknown-app'
MIRROR_OUTCOMES = (MIRROR_MIRRORED, MIRROR_SKIPPED_LOCAL, MIRROR_FAILED,
                   MIRROR_NO_URL, MIRROR_UNKNOWN_APP)

#: #1055 — the OPERATOR-FACING per-module mirror state, derived from the meta
#: record `mirror_docs`/`_mirror_with_monolith` write. `MIRROR_OUTCOMES` above
#: describes what ONE call did and is thrown away the moment it returns;
#: this is the durable "what is on disk right now, and is it any good"
#: answer the cache-admin table needs.
#:
#: The distinction that matters: `cached` (a non-empty directory) was the ONLY
#: thing the admin badge keyed on, and a FAILED wget/monolith run leaves files
#: behind — a truncated tree, a basic-auth challenge page, a rejected SPA
#: shell — so a module whose docs are broken rendered as "✓ Cached". Every
#: state below except OK/PARTIAL means "do not trust what is on disk".
STATE_OK = 'ok'                 # last capture succeeded cleanly
STATE_PARTIAL = 'partial'       # accepted, but wget flagged it partial
STATE_FAILED = 'failed'         # last capture FAILED — on-disk content is junk
STATE_FALLBACK = 'fallback'     # capture failed, curated local_doc is served
STATE_LOCAL = 'local'           # box-local page, nothing to mirror (by design)
STATE_NEVER = 'never'           # never attempted / no record at all
STATE_UNREADABLE = 'unreadable'  # meta record exists but could not be parsed
STATE_REFRESHING = 'refreshing'  # a job is mid-flight right now
MIRROR_STATES = (STATE_OK, STATE_PARTIAL, STATE_FAILED, STATE_FALLBACK,
                 STATE_LOCAL, STATE_NEVER, STATE_UNREADABLE, STATE_REFRESHING)

#: The states an operator must act on. Used by the admin page to sort/flag and
#: by `get_failed_modules()` to answer "which module docs silently failed".
MIRROR_BAD_STATES = (STATE_FAILED, STATE_FALLBACK, STATE_UNREADABLE)

#: Synthetic job id under which a `/api/refresh-all` run registers itself in
#: the file-based job registry (LOCK_DIR). The registry is the only piece of
#: refresh state SHARED ACROSS GUNICORN WORKERS — an in-process
#: `threading.Lock` cannot tell worker 2 that worker 1 is mid-refresh-all, and
#: between two per-app mirrors `get_active_jobs()` was momentarily EMPTY, so
#: the admin poller concluded "done", reloaded, and re-enabled every button
#: while `mirror_all` was still writing into those same cache directories.
REFRESH_ALL_JOB_ID = '__refresh_all__'

#: Where `mirror_all` records its per-module PASS/FAIL run summary. Lives in
#: META_DIR beside the per-app meta files; the leading underscore keeps it out
#: of the `<app_id>.json` namespace.
REFRESH_ALL_META = '_refresh_all.json'


# ----------------------------------------------------------------------
# #525 — JS-rendering capture path (a second option beside wget --mirror)
# ----------------------------------------------------------------------
#
# Operator decision 2026-08-26: REPAIR the mirror for the six SPA/CDN modules
# (dify, cognee, openhands, gotenberg, komodo, lightrag) rather than keep
# linking out to the public upstream docs (#149's fallback) or dropping the
# feature. `wget --mirror` cannot capture these: it does not execute JS, so
# a JS-rendered SPA (gotenberg/komodo) yields an empty shell, and it cannot
# follow assets that live on a different host (dify/cognee/openhands' CDN
# trees). `monolith` (https://github.com/Y2Z/monolith, a static Rust
# binary) is a different tool for a different job: it fetches ONE page —
# after JS has run, in whatever headless-capable form the caller feeds it —
# and inlines every CSS/JS/image/font it references into a single
# self-contained HTML file, so cross-host assets are moot (they end up
# embedded, not linked).
#
# Selected per-app via `"capture_method": "monolith"` on the app's
# mirror_config.json entry; `"wget"` (or the field absent) keeps the
# existing tree-mirror path unchanged. The binary need NOT be present in
# every environment running this code (it is absent in this sandbox) —
# `monolith_available()` makes presence/absence a first-class, testable
# fact, and `_mirror_with_monolith` degrades to MIRROR_FAILED with a clear
# reason instead of crashing when it is missing.
CAPTURE_WGET = 'wget'
CAPTURE_MONOLITH = 'monolith'
#: #1196 — capture the SOURCE markdown instead of scraping a rendered app.
#: `llms-txt`: Mintlify sites (dify, cognee, infisical, openhands) publish
#: llms.txt + `<page>.md`; `git-markdown`: a git repo holding markdown (the
#: Vaultwarden GitHub wiki, paperless' docs/ dir). Both build the
#: markdown-tree shape in markdown_tree.py, rendered through the Help
#: Center's own theme at view time.
CAPTURE_LLMS_TXT = 'llms-txt'
CAPTURE_GIT_MARKDOWN = 'git-markdown'
CAPTURE_METHODS = (CAPTURE_WGET, CAPTURE_MONOLITH, CAPTURE_LLMS_TXT, CAPTURE_GIT_MARKDOWN)
#: The methods whose on-disk result is a markdown tree (see markdown_tree).
CAPTURE_TREE_METHODS = (CAPTURE_LLMS_TXT, CAPTURE_GIT_MARKDOWN)

#: #1197 — the curated box-local page of a capture-first module stays
#: reachable NEXT TO its working mirror at `/docs/<app_id>/_local/` (linked
#: from the mirrored pages' attribution bar and a tree's source line). It is
#: where box-specific notes live — a known upstream issue, this box's
#: ports/tokens — which a reader must find without the mirror having to fail.
LOCAL_DOC_ROUTE = '_local'

#: Override to point at a non-PATH monolith binary (or a test double).
MONOLITH_BIN = os.environ.get('HELP_MONOLITH_BIN', 'monolith')
#: Same for git (#1196 git-markdown capture).
GIT_BIN = os.environ.get('HELP_GIT_BIN', 'git')
GIT_CLONE_TIMEOUT = 600

#: llms-txt capture bounds. A Mintlify index can list well over a thousand
#: pages (Dify: ~540 English pages across nested sub-indexes); the cap keeps
#: one module from monopolising a refresh-all, and the time budget keeps the
#: per-app lock (35 min staleness, see is_job_active) honest.
LLMS_MAX_PAGES_DEFAULT = 800
LLMS_MAX_SUBINDEX_DEPTH = 3
LLMS_TIME_BUDGET_SECONDS = 20 * 60
LLMS_FETCH_DELAY = 0.1
#: Image assets pulled into a markdown tree: per-file and per-module caps.
TREE_ASSET_MAX_BYTES = 4 * 1024 * 1024
TREE_ASSET_BUDGET_BYTES = 128 * 1024 * 1024
#: A markdown tree counts as captured once it has a page and some prose.
TREE_MIN_PAGES = 1
TREE_MIN_BYTES = 1024
#: Extra markdown extensions for tree pages (MkDocs `!!! note` admonitions,
#: `<div markdown="1">` callouts emitted by the MDX cleanup).
TREE_MARKDOWN_EXTENSIONS = ('admonition', 'md_in_html')

_IMAGE_EXTS = ('png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'avif', 'ico', 'bmp')
_CTYPE_EXT = {
    'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/svg+xml': 'svg',
    'image/webp': 'webp', 'image/avif': 'avif', 'image/x-icon': 'ico', 'image/bmp': 'bmp',
}


def git_available() -> bool:
    """True if the `git` binary is runnable (#1196 git-markdown capture).
    Same contract as monolith_available(): absence is a reportable fact,
    not a traceback."""
    return shutil.which(GIT_BIN) is not None


def new_fetcher(**kwargs) -> HttpFetcher:
    """Factory for the bounded HTTP fetcher; tests replace it with an
    in-memory stand-in via this module's globals."""
    return HttpFetcher(**kwargs)


def is_local_only(app_entry: dict | None) -> bool:
    """A `local_doc` entry with NO explicit capture_method ships its curated
    page and nothing else (#149). An explicit capture_method — any of
    CAPTURE_METHODS, `wget` included — is an opt-in to a real capture with
    the curated page retained as the offline FALLBACK (#824, #1196)."""
    if not app_entry:
        return False
    return bool(app_entry.get('local_doc')) and not app_entry.get('capture_method')


def _image_ext_for(url: str, content_type: str) -> str | None:
    path = urlsplit(url).path or ''
    ext = path.rsplit('.', 1)[-1].lower() if '.' in path.rsplit('/', 1)[-1] else ''
    if ext in _IMAGE_EXTS:
        return 'jpg' if ext == 'jpeg' else ext
    ctype = (content_type or '').split(';', 1)[0].strip().lower()
    return _CTYPE_EXT.get(ctype)


def monolith_available() -> bool:
    """True if the `monolith` JS-rendering archiver binary is runnable.

    Presence/absence must be a fact the caller can check and branch on —
    the whole point of this function existing separately from just trying
    the subprocess call and catching FileNotFoundError is that the capture
    path can then log a clear, specific reason ("binary not installed") and
    degrade to MIRROR_FAILED instead of an opaque traceback. #525 does not
    require the binary to be present in every environment this module runs
    in (it is not present in this sandbox); it requires that its absence is
    detected and handled cleanly.
    """
    return shutil.which(MONOLITH_BIN) is not None


#: #525's actual guard: the failure #149 named ("success reported, empty
#: shell delivered" — a wget mirror recorded as Success while it was really
#: a title-page-only tree) has an exact equivalent for a single-file
#: capture. `monolith` can exit 0 having captured a bare SPA root div
#: (`<div id="root"></div>`) because it never executes the app's JS to
#: render real content into it — the file is well-formed HTML, syntactically
#: a "success", and functionally useless. wget's tree-shaped sanity gate
#: (MIRROR_MIN_PAGES / MIRROR_MIN_BYTES, above) cannot apply here — a
#: capture is one file, not a page tree — so this is the single-file
#: equivalent: is there real, rendered, visible TEXT in the file, not just
#: markup waiting for JS that will never run once served offline from a
#: static cache.
CAPTURE_MIN_BYTES = 4096        # a bare SPA shell is a few hundred bytes
CAPTURE_MIN_TEXT_CHARS = 400    # visible text floor after stripping tags

#: Phrases a JS-rendered SPA leaves behind verbatim in its unrendered shell
#: (the <noscript> fallback, a loading spinner's only text, …). Matched only
#: as a tie-breaker alongside a thin text length — a legitimate page is
#: allowed to mention "loading" in prose.
_CAPTURE_SHELL_MARKERS = (
    'you need to enable javascript to run this app',
    'please enable javascript',
    'javascript is required',
)


def validate_capture_substance(html_content: str) -> tuple[bool, str | None]:
    """Judge a captured page on SUBSTANCE, not on subprocess exit code.

    Returns (True, None) for a page with real, resolved content; otherwise
    (False, reason) naming why it was rejected. This is the function a
    capture-path caller MUST run before ever recording `success=True` —
    reporting success on an empty shell is the exact 0.208 failure #525 (and
    its sibling #149) is about, just one file instead of one tree.

    Checks, cheapest-first:
      1. non-empty at all;
      2. above a byte floor (an unrendered SPA shell is a few hundred bytes
         even with a full <head> of inlined boilerplate CSS);
      3. above a VISIBLE-TEXT floor after stripping tags/script/style — this
         is what actually distinguishes "the docs got captured" from "an
         empty <div id='root'> got captured"; wget's page-count gate has no
         equivalent for a single inlined file, so this floor does that job;
      4. no verbatim SPA-shell placeholder phrase sitting inside otherwise
         thin text (a real page merely mentioning "enable JavaScript" in a
         troubleshooting paragraph, surrounded by substantial other text,
         is NOT rejected — only thin text carrying the exact placeholder is).
    """
    if not html_content or not html_content.strip():
        return False, 'empty capture — zero bytes'

    size = len(html_content.encode('utf-8', errors='ignore'))
    if size < CAPTURE_MIN_BYTES:
        return False, (
            f'capture too small ({size} bytes, need >= {CAPTURE_MIN_BYTES}) '
            '— likely an unrendered shell, not the rendered page')

    # Strip <script>/<style> bodies first so their contents never count as
    # "visible text", then strip all remaining tags.
    text_only = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', ' ', html_content,
                        flags=re.IGNORECASE | re.DOTALL)
    text_only = re.sub(r'<[^>]+>', ' ', text_only)
    text_only = re.sub(r'&nbsp;|&#160;', ' ', text_only, flags=re.IGNORECASE)
    text_only = re.sub(r'\s+', ' ', text_only).strip()

    if len(text_only) < CAPTURE_MIN_TEXT_CHARS:
        low = html_content.lower()
        for marker in _CAPTURE_SHELL_MARKERS:
            if marker in low:
                return False, (
                    f'SPA-shell placeholder text found ({marker!r}) with only '
                    f'{len(text_only)} chars of visible text — the page was '
                    'captured before its JS rendered real content')
        return False, (
            f'visible text too short ({len(text_only)} chars, need >= '
            f'{CAPTURE_MIN_TEXT_CHARS}) — looks like an unrendered SPA shell, '
            'not a real documentation page')

    return True, None


#: Seconds a `check_internet()` result is reused for (#1055 — see the method).
INTERNET_CHECK_TTL = 30.0


class CacheManager:
    def __init__(self):
        self._config = None
        #: (timestamp, online) of the last real connectivity probe, or None.
        self._internet_check: tuple[float, bool] | None = None

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
        bucket the file DECLARES (`<!-- section: guides -->`, one of the closed
        OWN_DOCS_SECTIONS set — #1055), falling back to its top-level directory
        via OWN_DOCS_LEGACY_SECTION_DIRS. The `enterprise/` overlay prefix is
        stripped for that fallback so a marker-less Enterprise overlay from an
        older release still groups as it did when baked in at the own_docs root.
        The slug and on-disk path keep the real relpath (incl. the enterprise/
        prefix). No hand-maintained manifest — drop a .md in the tree and it
        appears after the next image build / container restart / overlay refresh.

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
                directory = os.path.dirname(section_relpath).split(os.sep)[0] if os.sep in section_relpath else ''
                if directory == 'images':
                    continue
                # #1055: the bucket is DECLARED by the file, not inferred from
                # its directory (the directory is only the legacy fallback).
                section = _extract_section(full, section_relpath)
                slug = _slug_from_relpath(relpath)
                title = _extract_h1(full, fallback=os.path.splitext(fn)[0].replace('-', ' ').title())
                entries.append({
                    'id': slug,
                    'name': title,
                    'section': section,
                    'section_label': OWN_DOCS_SECTION_LABELS.get(
                        section, section.replace('-', ' ').title()),
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

    def is_local_only(self, app_entry: dict | None) -> bool:
        """See the module-level is_local_only (#149/#824/#1196)."""
        return is_local_only(app_entry)

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
    # Meta records (#1055)
    # ------------------------------------------------------------------
    # Every capture attempt writes ONE json record per app. Two rules, both
    # learned from cache-admin falling over:
    #
    #   * WRITE ATOMICALLY. The meta file was written in place, so a worker
    #     that died (or a container restart) mid-`json.dump` left truncated
    #     JSON on disk.
    #   * NEVER let a bad record raise. `get_app_cache_status()` used a bare
    #     `json.load`, and it is called for EVERY app by both `/admin/cache`
    #     and the hub's `get_visible_apps()` — so one corrupt byte took out
    #     the entire Help Center with a 500, not just that module's row.
    #     A record we cannot parse is a per-module FAILURE to report, never
    #     an exception to propagate.

    def _meta_path(self, app_id: str) -> str:
        return os.path.join(META_DIR, f'{app_id}.json')

    def _write_meta(self, meta_file: str, meta: dict) -> None:
        """Atomically replace `meta_file` with `meta` (never a partial file)."""
        tmp = f'{meta_file}.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp, meta_file)
        except OSError as e:
            print(f'[help-cache] could not write meta {meta_file}: {e}')
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _read_meta(self, meta_file: str) -> tuple[dict | None, str | None]:
        """Return (meta, error). `meta` is None when there is nothing to read;
        `error` is set (and meta None) when a record exists but is unusable."""
        if not os.path.isfile(meta_file):
            return None, None
        try:
            with open(meta_file, 'r') as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            return None, f'cache metadata unreadable ({e.__class__.__name__}: {e})'
        if not isinstance(meta, dict):
            return None, 'cache metadata unreadable (not a JSON object)'
        return meta, None

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
        #
        # #824/fix-round: a `capture_method: monolith` module also carries
        # `local_doc` (its OFFLINE FALLBACK), but it has a REAL capture path —
        # short-circuiting here BEFORE the meta file is ever read reported the
        # synthetic always-available dict (last_updated=None, no error) even
        # when a real capture had run and failed. Fall through to the
        # meta-based status below for monolith entries so the admin page shows
        # the actual last_updated/error/exit_code/page_count.
        app_entry = self.get_app_entry(app_id)
        if is_local_only(app_entry):
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
                'mirror_state': STATE_LOCAL,
                'mirror_ok': True,
                'outcome': MIRROR_SKIPPED_LOCAL,
            }

        meta_file = self._meta_path(app_id)
        app_cache = os.path.join(CACHE_DIR, app_id)

        try:
            has_files = os.path.isdir(app_cache) and bool(os.listdir(app_cache))
        except OSError:
            has_files = False

        result = {
            'cached': has_files,
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
            # #1055: the true per-module state (see MIRROR_STATES). `cached`
            # alone cannot express it — a FAILED run leaves files on disk, so
            # `cached` is True for a module whose docs are junk.
            'mirror_state': STATE_NEVER,
            'mirror_ok': False,
            'outcome': None,
            # #1229: a capture that IS served but is cosmetically wrong (see
            # mirror_quality's degraded half). `error` stays reserved for "the
            # reader gets nothing"; this is the warning beside a served mirror.
            'warning': None,
            'quality': None,
            'quality_severity': None,
        }

        meta, meta_error = self._read_meta(meta_file)
        if meta_error:
            # Fail LOUD: an unreadable record is a per-module problem the
            # operator can act on (click Refresh), not a 500 for everyone.
            result['error'] = meta_error
            result['mirror_state'] = STATE_UNREADABLE
        elif meta is not None:
            result['last_updated'] = meta.get('last_updated')
            result['error'] = meta.get('error')
            result['partial'] = meta.get('partial', False)
            result['exit_code'] = meta.get('exit_code')
            result['page_count'] = meta.get('page_count')
            result['outcome'] = meta.get('outcome')
            result['quality'] = meta.get('quality')
            result['quality_severity'] = meta.get('quality_severity')
            if meta.get('success'):
                result['warning'] = meta.get('quality_note')
                result['mirror_state'] = (STATE_PARTIAL if result['partial']
                                          else STATE_OK)
                result['mirror_ok'] = True
            elif meta.get('fell_back_to_local_doc'):
                # The capture genuinely failed; the curated box-local page is
                # what the reader gets. Docs ARE available — but the operator
                # must still see that the upstream capture is broken, which is
                # exactly what "📄 Local page" used to hide.
                result['mirror_state'] = STATE_FALLBACK
            else:
                result['mirror_state'] = STATE_FAILED

        if result['refreshing']:
            result['mirror_state'] = STATE_REFRESHING

        if has_files:
            result['size_mb'] = round(self._dir_size(app_cache) / (1024 * 1024), 1)

        return result

    def get_failed_modules(self) -> list[dict]:
        """Every configured module whose docs are NOT in a trustworthy state.

        The "which module docs succeeded, which silently failed" answer, read
        off the durable per-module records rather than from whatever the last
        `mirror_all()` happened to print to the container log.
        """
        failed = []
        for app_entry in self.get_config().get('apps', []):
            app_id = app_entry.get('id')
            if not app_id:
                continue
            status = self.get_app_cache_status(app_id)
            if status.get('mirror_state') in MIRROR_BAD_STATES:
                failed.append({
                    'id': app_id,
                    'name': app_entry.get('name', app_id),
                    'mirror_state': status['mirror_state'],
                    'error': status.get('error'),
                })
        return failed

    def get_degraded_modules(self) -> list[dict]:
        """Every module whose docs ARE served but failed a cosmetic gate rule.

        The other half of `get_failed_modules` (#1229 review BLOCKER 3): a
        mirror that reports `quality_severity: degraded` is readable — no
        Unavailable page, no fallback — but something the operator should see
        is wrong with it (unstyled pages, a stylesheet the completion pass
        could not re-fetch). Kept OUT of `get_failed_modules` because "needs
        attention" and "is broken" are different operator actions; surfaced
        alongside it so the admin page and the day-1 acceptance tier can go
        red on either without re-judging the tree.
        """
        degraded = []
        for app_entry in self.get_config().get('apps', []):
            app_id = app_entry.get('id')
            if not app_id:
                continue
            status = self.get_app_cache_status(app_id)
            if status.get('quality_severity') != mirror_quality.SEVERITY_DEGRADED:
                continue
            if status.get('mirror_state') in MIRROR_BAD_STATES:
                continue  # already reported by get_failed_modules
            degraded.append({
                'id': app_id,
                'name': app_entry.get('name', app_id),
                'mirror_state': status.get('mirror_state'),
                'quality': status.get('quality'),
                'warning': status.get('warning'),
            })
        return degraded

    def has_servable_tree(self, app_id: str) -> bool:
        """True when the bytes on disk still render as documentation (#1229
        review MEDIUM 4) — i.e. there is a LAST GOOD tree to keep serving even
        though the latest refresh failed.

        `_discard_failed_cache` deliberately leaves a previously-good tree in
        place until the next refresh actually starts, and before #1229 the
        viewer threw it away anyway: one failed overnight refresh replaced
        working offline docs with "not offline readable". Rather than
        bookkeeping "was the PREVIOUS capture good" across runs, this asks the
        only question that matters at serve time — does the entry page on disk
        pass the FATAL half of the content-quality gate right now. A degraded
        (unstyled) tree counts as servable; that is the whole point of the
        severity split.
        """
        app_cache = os.path.join(CACHE_DIR, app_id)
        if not os.path.isdir(app_cache):
            return False
        app_entry = self.get_app_entry(app_id) or {}
        host = urlsplit(app_entry.get('mirror_url') or '').netloc or None
        try:
            rep = mirror_quality.assess_cache_page(
                app_cache, self.get_entry_path(app_id), page_host=host)
        except OSError:
            return False
        return rep.usable

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
        """Quick internet connectivity check via HTTP, memoised briefly.

        #1055: on an air-gapped box (#184) BOTH probes below run to their
        5-second timeout, so one call costs ~10 s of a gunicorn worker. It is
        made on every `/admin/cache` render AND on every `/api/cache-status`
        poll — which the admin page fires every 3 s while a refresh runs. The
        result was a cache-admin page that hung, timed out, and looked broken
        precisely on the boxes that most need offline docs. Connectivity does
        not change second-to-second; a short TTL removes the pile-up while
        still letting an operator who just plugged in the network retry
        within half a minute.
        """
        now = time.time()
        cached = self._internet_check
        if cached is not None and (now - cached[0]) < INTERNET_CHECK_TTL:
            return cached[1]

        try:
            r = requests.get('http://clients3.google.com/generate_204', timeout=5)
            online = r.status_code == 204
        except Exception:
            try:
                r = requests.get('https://httpbin.org/ip', timeout=5)
                online = r.status_code == 200
            except Exception:
                online = False

        self._internet_check = (time.time(), online)
        return online

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

    # #1055: refresh-all registers in the SAME file-based registry as a
    # per-app job, under the synthetic REFRESH_ALL_JOB_ID. Two things follow,
    # both of which the in-process `threading.Lock` in app.py cannot give:
    #   * every gunicorn worker sees the run — a per-app Refresh on worker 2
    #     can be refused while worker 1 is mid-`mirror_all()`;
    #   * `get_active_jobs()` stays non-empty for the WHOLE run, so the admin
    #     poller no longer sees the gap BETWEEN two per-app mirrors, declare
    #     the job finished, reload the page and re-enable every button while
    #     the refresh is still writing into those cache directories.
    # The 35-minute staleness rule in `is_job_active` applies here too, so a
    # crashed worker cannot park the button forever.

    def set_refresh_all_lock(self):
        self._set_lock(REFRESH_ALL_JOB_ID)

    def clear_refresh_all_lock(self):
        self._clear_lock(REFRESH_ALL_JOB_ID)

    def is_refresh_all_active(self) -> bool:
        return self.is_job_active(REFRESH_ALL_JOB_ID)

    # ------------------------------------------------------------------
    # Mirroring
    # ------------------------------------------------------------------

    def _discard_failed_cache(self, app_id: str, app_cache: str,
                              meta_file: str) -> bool:
        """Delete the cache tree of a module whose LAST capture failed (#1055).

        Returns True if anything was removed. A record we cannot parse counts
        as failed — we have no evidence the tree is good, and the whole point
        of a refresh is to get back to a known state.
        """
        if not os.path.isdir(app_cache):
            return False
        meta, meta_error = self._read_meta(meta_file)
        if meta_error is None and (meta is None or meta.get('success')):
            return False  # never attempted, or last attempt was good — keep it
        try:
            shutil.rmtree(app_cache)
        except OSError as e:
            print(f'[help-cache] could not clear failed cache for {app_id}: {e}')
            return False
        print(f'[help-cache] cleared the failed cache tree for {app_id} '
              'so the refresh re-fetches instead of timestamp-skipping')
        return True

    def _mark_fallback(self, meta_file: str) -> None:
        """Flag the meta record written by a failed capture as 'the curated
        local_doc page is what gets served' (#1055) — see mirror_docs."""
        meta, meta_error = self._read_meta(meta_file)
        if meta_error is not None or meta is None:
            return
        meta['fell_back_to_local_doc'] = True
        meta['outcome'] = MIRROR_SKIPPED_LOCAL
        self._write_meta(meta_file, meta)

    def mirror_docs(self, app_id: str) -> str:
        """Mirror documentation for a single app using wget.

        Returns one of MIRROR_OUTCOMES, not a bool (#525).

        It used to return **True** for a box-local page — "success" for a call
        that fetched nothing — so the admin Refresh button reported success while
        doing no work, on the six modules that have no mirror at all. Not
        re-fetching a tree that cannot be mirrored is right; calling it a success
        is what made the gap invisible. `skipped-local-doc` says both.

        Callers must branch on the OUTCOME. A bool cannot express three states,
        and the missing third is exactly the one an operator needs to see.
        """
        app_entry = self.get_app_entry(app_id)
        if not app_entry:
            return MIRROR_UNKNOWN_APP

        # #525: a per-app opt-in to the JS-rendering capture path. Default
        # stays wget (unchanged behaviour for the other mirrors).
        capture_method = app_entry.get('capture_method', CAPTURE_WGET)
        has_local_doc = bool(app_entry.get('local_doc'))

        # #824: monolith-first, local_doc-fallback. A plain wget-mirrored app
        # with a local_doc box-local page (#149) still short-circuits here —
        # nothing to mirror, so skip straight to the curated page. But an app
        # opted into `capture_method: monolith` gets a REAL capture attempt
        # first; local_doc on that entry is retained as the OFFLINE FALLBACK
        # (monolith needs the binary + WAN egress, neither guaranteed on an
        # air-gapped box, #184), not an unconditional skip. NOTE: mirror_url
        # is intentionally KEPT on every local_doc entry either way — it is
        # the "Full upstream documentation" link, not (necessarily) a fetch
        # target.
        if is_local_only(app_entry):
            return MIRROR_SKIPPED_LOCAL

        meta_file = self._meta_path(app_id)
        mirror_url = app_entry.get('mirror_url', '')
        if not mirror_url:
            # #1055: this used to be a SILENT skip — no meta record, nothing
            # printed, and the admin page showed a bare "⚠ Not cached" with no
            # reason. A module configured with no capture target is a config
            # fault an operator can fix; say so, durably.
            self._write_meta(meta_file, {
                'last_updated': datetime.utcnow().isoformat() + 'Z',
                'mirror_url': None,
                'exit_code': None,
                'page_count': 0,
                'size_bytes': 0,
                'partial': True,
                'success': False,
                'outcome': MIRROR_NO_URL,
                'error': (f'{app_id} has no mirror_url configured in '
                          'mirror_config.json — there is no documentation '
                          'source to capture'),
            })
            print(f'[help-cache] {app_id} has no mirror_url — nothing to capture')
            return MIRROR_NO_URL

        self._set_lock(app_id)
        app_cache = os.path.join(CACHE_DIR, app_id)

        try:
            # #1055 — a refresh must actually RE-ATTEMPT a failed module.
            # `wget --mirror` implies `-N` (timestamping): against a tree left
            # behind by a FAILED run it re-fetches nothing the server calls
            # unchanged, exits 0, and the meta is rewritten as a clean success
            # — so the operator is told the module was repaired while the same
            # broken bytes are still on disk. Discard a known-bad tree first so
            # the retry is a genuine cold fetch. A tree from a SUCCESSFUL (or
            # accepted-partial) run is never touched.
            self._discard_failed_cache(app_id, app_cache, meta_file)

            # Ensure output directory exists
            os.makedirs(app_cache, exist_ok=True)

            if capture_method in (CAPTURE_MONOLITH, CAPTURE_LLMS_TXT, CAPTURE_GIT_MARKDOWN):
                if capture_method == CAPTURE_MONOLITH:
                    outcome = self._mirror_with_monolith(
                        app_id, mirror_url, app_cache, meta_file)
                elif capture_method == CAPTURE_LLMS_TXT:
                    outcome = self._mirror_with_llms_txt(
                        app_id, app_entry, mirror_url, app_cache, meta_file)
                else:
                    outcome = self._mirror_with_git_markdown(
                        app_id, app_entry, mirror_url, app_cache, meta_file)
                # Capture tool unavailable, or its capture failed / was
                # rejected by the substance / content-quality gate: fall
                # back to the curated local_doc one-pager rather than
                # surfacing a hard failure — only when this entry actually
                # carries a fallback.
                if outcome != MIRROR_MIRRORED and has_local_doc:
                    # #1055: the RETURN value stays MIRROR_SKIPPED_LOCAL (#824
                    # pins it — the reader does get docs, so this is not a hard
                    # failure for the caller). But "skipped" is not what
                    # happened, and reporting only that is how a permanently
                    # broken capture stayed invisible behind a "📄 Local page"
                    # badge. Flag the fallback IN THE META so cache-admin can
                    # show the real state and the real error.
                    self._mark_fallback(meta_file)
                    return MIRROR_SKIPPED_LOCAL
                return outcome

            # Build wget command
            cmd = [
                'wget',
                '--mirror',
                '--convert-links',
                '--adjust-extension',
                '--page-requisites',
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

            # #1196: `crawl_scope` — a site whose entry page sits DEEP in the
            # tree (komodo: /docs/intro, gotenberg: /docs/getting-started/
            # introduction) needs the crawl bounded by the docs ROOT, not by
            # the entry page's own directory as --no-parent would do (for
            # gotenberg that meant one subtree). -I bounds it explicitly; the
            # page requisites it also filters are recovered by the asset
            # completion pass below.
            crawl_scope = (app_entry.get('crawl_scope') or '').strip()
            if crawl_scope:
                cmd.extend(['--include-directories', crawl_scope])
            else:
                cmd.append('--no-parent')

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
            # #1284: a crawl that outlives the 30-minute budget is NOT the same
            # thing as a crawl that failed. onyx (0.91, 2026-09-05) had 134
            # real pages on disk with an `ok` entry page and was recorded as
            # `failed` — nothing served, the day-1 probe red, the reader sent
            # to a "timed out" badge instead of 134 pages of documentation.
            # Treat the timeout like a truncated-but-present tree: run the
            # same sanity + content-quality gates the exit-8 path runs, serve
            # what passed as PARTIAL with the reason in front of the operator,
            # and fail only when the tree is a title page.
            timed_out = False
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            except subprocess.TimeoutExpired:
                timed_out = True
                result = types.SimpleNamespace(returncode=None, stdout='', stderr='')

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

            # #1196: recover the page requisites wget's -X/-I rules dropped
            # (stylesheets, icons, fonts, images) so the tree renders styled
            # offline — see mirror_assets. Never fatal: a failed completion
            # is reported through the quality gate below, not as a crash.
            asset_stats = None
            if page_count and (returncode in (0, 8) or timed_out):
                try:
                    asset_stats = mirror_assets.complete_page_assets(
                        app_cache, mirror_url, new_fetcher(delay=0.05))
                except Exception as e:  # noqa: BLE001 — must not abort the capture
                    print(f'[help-cache] asset completion for {app_id} raised: {e}')

            size_bytes = self._dir_size(app_cache)
            partial = returncode != 0
            fail_reason = None
            timeout_note = None

            if timed_out:
                if page_count >= MIRROR_MIN_PAGES and size_bytes >= MIRROR_MIN_BYTES:
                    success = True
                    timeout_note = (f'Mirror timed out after 30 minutes — serving the '
                                    f'{page_count} pages captured (partial)')
                else:
                    success = False
                    fail_reason = (
                        f'Mirror timed out after 30 minutes and the tree failed the '
                        f'sanity gate: {page_count} HTML page(s), {size_bytes} bytes '
                        f'(need >= {MIRROR_MIN_PAGES} pages and >= {MIRROR_MIN_BYTES} '
                        f'bytes) — likely title-page-only')
            elif returncode == 0:
                if page_count >= 1:
                    success = True
                else:
                    # exit 0 with nothing saved is the #149 "success reported,
                    # empty shell delivered" shape, not a success.
                    success = False
                    fail_reason = 'wget exit 0 but no HTML page was saved — nothing to serve'
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

            # #1196: the content-quality gate. A tree that passed wget's exit
            # code and page-count checks can still be unusable — the entry
            # page a meta-refresh stub to another host (presidio), a
            # "permanently moved" tombstone (paperless), a directory listing
            # (openuem), or naked HTML whose stylesheet never arrived. Judge
            # the ENTRY page the reader lands on; record the verdict either
            # way so the admin page can show WHAT is wrong.
            #
            # #1229 review BLOCKER 3: the verdict has two severities. A FATAL
            # problem (listing / foreign redirect / tombstone / raw JS /
            # unrendered shell) means the reader gets no documentation — fail
            # closed, as before. A DEGRADED one ("this page has no stylesheet"
            # over 40 000 characters of real prose — tika.apache.org ships it
            # that way, and every MkDocs mirror with `-X /assets` is one
            # re-fetch failure away from the same verdict) is cosmetic: serve
            # the capture, mark it PARTIAL, and put the reason in front of the
            # operator. Switching a working mirror off over missing CSS was
            # strictly worse than showing unstyled docs.
            quality = None
            quality_note = None
            if success:
                entry_rel = self.get_entry_path(app_id)
                quality = mirror_quality.assess_cache_page(
                    app_cache, entry_rel, page_host=urlsplit(mirror_url).netloc)
                if quality.fatal:
                    success = False
                    partial = True
                    fail_reason = quality.summary()
                elif quality.degraded:
                    partial = True
                    quality_note = quality.summary()
            if success and timeout_note:
                # #1284: the timeout is the operator-facing reason, next to any
                # content-quality note; `partial` already says "not clean".
                quality_note = '; '.join(x for x in (timeout_note, quality_note) if x)

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
                'outcome': MIRROR_MIRRORED if success else MIRROR_FAILED,
                'error': error,
                'quality': (quality.problems if quality is not None else None),
                # #1229: the durable record of WHICH half fired. `partial` says
                # "not clean"; only these say whether the mirror is readable —
                # the admin badge, /api/cache-status and the day-1 probe all
                # read them rather than re-judging the tree.
                'quality_severity': (quality.severity if quality is not None else None),
                'quality_degraded': (quality.degraded if quality is not None else None),
                'quality_note': quality_note,
                'assets_completed': (asset_stats.get('fetched') if asset_stats else None),
            }

            self._write_meta(meta_file, meta)

            status_word = ('Degraded' if (success and quality_note) else
                           'Success' if success else
                           ('Partial' if partial else 'Failed'))
            if quality_note:
                print(f'[help-cache] {app_id}: {quality_note}')
            print(f"[help-cache] {status_word} mirroring {app_id} "
                  f"(exit {returncode}, {page_count} HTML pages, {size_bytes} bytes, "
                  f"partial={partial})")
            if not success and has_local_doc:
                # #1196: a wget-first module (komodo/gotenberg) keeps its
                # curated page as the offline fallback, same as the other
                # capture methods — visible as STATE_FALLBACK, never silent.
                self._mark_fallback(meta_file)
                return MIRROR_SKIPPED_LOCAL
            return MIRROR_MIRRORED if success else MIRROR_FAILED


        except Exception as e:
            meta = {
                'last_updated': datetime.utcnow().isoformat() + 'Z',
                'mirror_url': mirror_url,
                'exit_code': None,
                'page_count': self._count_html_pages(app_cache),
                'partial': True,
                'success': False,
                'outcome': MIRROR_FAILED,
                'error': str(e),
            }
            self._write_meta(meta_file, meta)
            print(f"[help-cache] Error mirroring {app_id}: {e}")
            return MIRROR_FAILED

        finally:
            self._clear_lock(app_id)

    def _mirror_with_monolith(self, app_id: str, mirror_url: str,
                               app_cache: str, meta_file: str) -> str:
        """JS-rendering single-file capture path (#525), an alternative to
        `wget --mirror` selected per-app via `"capture_method": "monolith"`.

        Unlike wget's tree crawl, monolith fetches one page and inlines every
        asset it references (CSS/JS/images/fonts) into a single
        self-contained HTML file — the shape of tool the six SPA/CDN modules
        (dify, cognee, openhands, gotenberg, komodo, lightrag) need, since
        their upstream docs either never finish rendering under wget (no JS
        execution) or reference assets on a different host wget won't follow.

        Caller (`mirror_docs`) already holds the per-app lock and created
        `app_cache`; this method owns writing `meta_file` and returning a
        MIRROR_OUTCOMES value, mirroring the wget path's contract.

        THE GUARD: exit code 0 from monolith is NOT enough to record success
        — see `validate_capture_substance`. A capture that exits clean but
        renders to an empty shell is rejected exactly like #149's
        title-page-only wget trees were, just judged on visible text instead
        of page count.

        REMAINING SCOPE (not this slice, reported honestly per #525's
        instructions): this method is a real, runnable seam — the subprocess
        invocation and the meta bookkeeping are complete — but it has not
        been run against a live monolith binary (none is installed in this
        sandbox), and no app in mirror_config.json has been switched onto
        `capture_method: monolith` yet. Doing that per-app rollout requires
        capturing each of the six sites for real and eyeballing the result,
        which is exactly the kind of "looks fine, ships empty" step this
        issue exists to stop being silent about — so it is left for a
        follow-up with the binary actually available to verify against.
        """
        out_file = os.path.join(app_cache, 'index.html')

        if not monolith_available():
            meta = {
                'last_updated': datetime.utcnow().isoformat() + 'Z',
                'mirror_url': mirror_url,
                'capture_method': CAPTURE_MONOLITH,
                'exit_code': None,
                'page_count': 0,
                'size_bytes': 0,
                'partial': True,
                'success': False,
                'outcome': MIRROR_FAILED,
                'error': (
                    f'monolith binary not found on PATH ({MONOLITH_BIN}) — '
                    f'{app_id} is configured for the JS-rendering capture '
                    'path but the archiver is not installed in this '
                    'environment'),
            }
            self._write_meta(meta_file, meta)
            print(f"[help-cache] monolith not available, cannot capture {app_id}")
            return MIRROR_FAILED

        # #824: every option here must exist in the monolith build we ship,
        # and none may suppress the diagnosis this method depends on.
        #
        #  * monolith is a clap CLI — an argument it does not know is a hard
        #    parse error (usage to stderr, exit code 2), NOT a warning, so one
        #    stale flag makes EVERY capture on every box fail. This list was
        #    written against monolith <= 2.6.2, which had `-s/--silent`;
        #    2.7.0 renamed it to `-q/--quiet` (upstream src/main.rs:
        #    `options.silent = cli.quiet`) and Alpine packages 2.10.1 — so
        #    `--silent` was rejected by every monolith we could install, and
        #    because all six monolith modules carry a local_doc fallback the
        #    capture would have failed invisibly behind a "📄 Local page"
        #    badge for ever.
        #  * The replacement is NOT `--quiet`: upstream gates its own error
        #    message on `if !silent { print_error_message(...) }`, so quieting
        #    monolith empties `result.stderr` and the `monolith exit N |
        #    <reason>` meta below degrades to a bare exit code with no
        #    diagnosis. We already capture stderr instead of inheriting it,
        #    so there is nothing to be quiet for — verbosity costs a few KB
        #    we discard on success and buys the operator the actual reason on
        #    failure.
        #
        # Flags verified against upstream src/main.rs at v2.10.1 and master;
        # tests/unit/razzfazz-help/test_824_monolith_subprocess_seam.py pins
        # the whole argv against that surface through a real subprocess.
        cmd = [
            MONOLITH_BIN, mirror_url,
            '--output', out_file,
            '--isolate',
            '--no-audio',
            '--no-video',
        ]

        try:
            print(f"[help-cache] Capturing {app_id} via monolith: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            returncode = result.returncode

            html_content = ''
            if returncode == 0 and os.path.isfile(out_file):
                with open(out_file, 'r', encoding='utf-8', errors='replace') as f:
                    html_content = f.read()

            substantive, reason = validate_capture_substance(html_content)
            size_bytes = len(html_content.encode('utf-8', errors='ignore'))
            success = (returncode == 0) and substantive

            if success:
                error = None
            elif returncode != 0:
                error = f'monolith exit {returncode}'
                if result.stderr:
                    error = f'{error} | {result.stderr[-300:]}'
            else:
                error = f'capture rejected: {reason}'

            meta = {
                'last_updated': datetime.utcnow().isoformat() + 'Z',
                'mirror_url': mirror_url,
                'capture_method': CAPTURE_MONOLITH,
                'exit_code': returncode,
                'page_count': 1 if success else 0,
                'size_bytes': size_bytes,
                'partial': not success,
                'success': success,
                'outcome': MIRROR_MIRRORED if success else MIRROR_FAILED,
                'error': error,
            }
            self._write_meta(meta_file, meta)

            status_word = 'Success' if success else 'Failed'
            print(f"[help-cache] {status_word} capturing {app_id} via monolith "
                  f"(exit {returncode}, {size_bytes} bytes, substantive={substantive})")
            return MIRROR_MIRRORED if success else MIRROR_FAILED

        except subprocess.TimeoutExpired:
            meta = {
                'last_updated': datetime.utcnow().isoformat() + 'Z',
                'mirror_url': mirror_url,
                'capture_method': CAPTURE_MONOLITH,
                'exit_code': None,
                'page_count': 0,
                'size_bytes': 0,
                'partial': True,
                'success': False,
                'outcome': MIRROR_FAILED,
                'error': 'monolith capture timed out after 3 minutes',
            }
            self._write_meta(meta_file, meta)
            print(f"[help-cache] Timeout capturing {app_id} via monolith")
            return MIRROR_FAILED

        except Exception as e:
            meta = {
                'last_updated': datetime.utcnow().isoformat() + 'Z',
                'mirror_url': mirror_url,
                'capture_method': CAPTURE_MONOLITH,
                'exit_code': None,
                'page_count': 0,
                'size_bytes': 0,
                'partial': True,
                'success': False,
                'outcome': MIRROR_FAILED,
                'error': str(e),
            }
            self._write_meta(meta_file, meta)
            print(f"[help-cache] Error capturing {app_id} via monolith: {e}")
            return MIRROR_FAILED

    def has_valid_monolith_capture(self, app_id: str) -> bool:
        """True if a validated monolith capture (#525/#824) is on disk for app_id.

        Reads the same meta file `_mirror_with_monolith` writes rather than
        just checking `index.html` exists — a rejected capture (binary
        missing, non-zero exit, or `validate_capture_substance` rejecting a
        thin SPA shell) can still leave a file on disk, and `success: False`
        is exactly what must NOT be served. This is the fact the viewer
        (app.py::view_docs) branches on to pick monolith-first vs. the
        local_doc fallback.
        """
        meta, meta_error = self._read_meta(self._meta_path(app_id))
        if meta_error is not None or meta is None:
            return False
        if meta.get('capture_method') != CAPTURE_MONOLITH or not meta.get('success'):
            return False
        return os.path.isfile(os.path.join(CACHE_DIR, app_id, 'index.html'))

    # ------------------------------------------------------------------
    # #1196 — markdown-tree captures (llms-txt / git-markdown)
    # ------------------------------------------------------------------
    # Both drivers build into a sibling `.<app_id>.building` directory and
    # swap it into place only when the build passed the gate, so a refresh
    # that fails halfway never destroys the last good tree — the reader
    # keeps the previous capture, and the admin page shows the failed
    # refresh (has_valid_markdown_tree keys on the swapped-in _tree.json,
    # which only a completed build writes).

    def _tree_build_dir(self, app_id: str) -> str:
        return os.path.join(CACHE_DIR, f'.{app_id}.building')

    def _tree_meta(self, mirror_url: str, method: str, **fields) -> dict:
        meta = {
            'last_updated': datetime.utcnow().isoformat() + 'Z',
            'mirror_url': mirror_url,
            'capture_method': method,
            'tree_format': mtree.TREE_FORMAT,
            'exit_code': None,
            'page_count': 0,
            'size_bytes': 0,
            'partial': True,
            'success': False,
            'outcome': MIRROR_FAILED,
            'error': None,
        }
        meta.update(fields)
        return meta

    def _tree_fail(self, app_id: str, meta_file: str, mirror_url: str, method: str,
                   reason: str, **fields) -> str:
        shutil.rmtree(self._tree_build_dir(app_id), ignore_errors=True)
        if mtree.read_tree(os.path.join(CACHE_DIR, app_id)) is not None:
            reason = f'{reason} (the previous capture stays served)'
        self._write_meta(meta_file, self._tree_meta(mirror_url, method, error=reason, **fields))
        print(f'[help-cache] Failed capturing {app_id} via {method}: {reason}')
        return MIRROR_FAILED

    def _tree_finish(self, app_id: str, meta_file: str, mirror_url: str, method: str,
                     writer: 'mtree.TreeWriter', tree: dict, *, partial: bool,
                     note: str | None, **fields) -> str:
        """Gate, swap the build into place, write the meta record."""
        build_dir = self._tree_build_dir(app_id)
        app_cache = os.path.join(CACHE_DIR, app_id)
        text_bytes = sum(
            os.path.getsize(os.path.join(build_dir, p['file']))
            for p in tree['pages'].values()
            if os.path.isfile(os.path.join(build_dir, p['file'])))
        if tree['page_count'] < TREE_MIN_PAGES or text_bytes < TREE_MIN_BYTES:
            return self._tree_fail(
                app_id, meta_file, mirror_url, method,
                f'capture rejected: {tree["page_count"]} page(s), {text_bytes} bytes of '
                f'markdown (need >= {TREE_MIN_PAGES} page and >= {TREE_MIN_BYTES} bytes)',
                **fields)
        shutil.rmtree(app_cache, ignore_errors=True)
        os.replace(build_dir, app_cache)
        size_bytes = self._dir_size(app_cache)
        self._write_meta(meta_file, self._tree_meta(
            mirror_url, method, page_count=tree['page_count'], size_bytes=size_bytes,
            asset_count=tree['asset_count'], partial=partial, success=True,
            outcome=MIRROR_MIRRORED, error=None, note=note, **fields))
        print(f"[help-cache] Success capturing {app_id} via {method} "
              f"({tree['page_count']} pages, {tree['asset_count']} assets, "
              f"{size_bytes} bytes, partial={partial})")
        return MIRROR_MIRRORED

    def _http_asset_store(self, fetch, writer: 'mtree.TreeWriter', budget: int,
                          *, host: str | None = None, extra_hosts=()):
        """`store(url)` for markdown_tree.localise_images: fetch an image
        within the per-file/per-module budget into the tree's _assets.

        #1229 review LOW 5 — bounded to a CLOSED host set. Unlike
        `mirror_assets`, which cannot leave the origin because it BUILDS every
        URL from it (`_remote_url(origin, cache_path)`), this store is handed
        whatever the upstream markdown referenced: an `<img>` on a third-party
        CDN, a tracking pixel, an intranet host. Following those turns a
        documentation capture into an outbound GET whose destination is chosen
        by remote content.

        The mirror's own origin is always allowed. `extra_hosts` comes from
        the entry's `asset_hosts` in mirror_config.json and is the ONLY way to
        reach anything else — a repo-controlled, reviewed decision rather than
        one the fetched markdown makes. Mintlify sites (dify, cognee,
        infisical, openhands) need exactly one such host: every image in their
        markdown lives on `mintcdn.com`, so origin-only would have shipped
        those four mirrors with no images at all. An unlisted host keeps its
        absolute URL in the rendered page (it will not load offline), which is
        the honest outcome.
        """
        allowed = [h for h in ([host] + [x for x in extra_hosts if x]) if h]

        def store(url: str) -> str | None:
            if writer.asset_bytes >= budget:
                return None
            if not any(mirror_quality.is_same_host(url, h) for h in allowed):
                return None
            r = fetch.get(url)
            if r.status != 200 or not r.body or len(r.body) > TREE_ASSET_MAX_BYTES:
                return None
            ext = _image_ext_for(url, r.content_type)
            if not ext:
                return None
            return writer.add_asset(r.body, ext)
        return store

    def _mirror_with_llms_txt(self, app_id: str, app_entry: dict, mirror_url: str,
                              app_cache: str, meta_file: str) -> str:
        """Capture a Mintlify site from its llms.txt index + `<page>.md`
        sources (#1196). Caller holds the per-app lock; this writes the
        meta record and returns a MIRROR_OUTCOMES value."""
        method = CAPTURE_LLMS_TXT
        scope_url = mirror_url if mirror_url.endswith('/') else mirror_url + '/'
        llms_url = app_entry.get('llms_url') or urljoin(scope_url, 'llms.txt')
        excludes = app_entry.get('exclude_directories') or []
        max_pages = int(app_entry.get('max_pages') or LLMS_MAX_PAGES_DEFAULT)
        fetch = new_fetcher(delay=LLMS_FETCH_DELAY)
        started = time.monotonic()

        print(f'[help-cache] Capturing {app_id} via llms.txt: {llms_url}')
        status, text = fetch.text(llms_url)
        if status != 200:
            return self._tree_fail(app_id, meta_file, mirror_url, method,
                                   f'llms.txt not available at {llms_url}: HTTP {status}')
        index = mtree.parse_llms_index(text, llms_url)

        nav: list[dict] = []
        planned: dict[str, tuple[str, str]] = {}   # path -> (title, url)

        def _expand(idx: 'mtree.LlmsIndex', offset: int, depth: int, parent_title: str = ''):
            for sec in idx.sections:
                level = sec.level + offset
                if sec.title and sec.title.strip().lower() == parent_title.strip().lower():
                    node = nav[-1] if nav else None
                else:
                    node = {'title': sec.title, 'level': level, 'pages': []}
                    nav.append(node)
                for e in sec.entries:
                    if e.is_subindex:
                        if depth >= LLMS_MAX_SUBINDEX_DEPTH:
                            continue
                        st, body = fetch.text(e.url)
                        if st != 200:
                            print(f'[help-cache] {app_id}: sub-index {e.url} → HTTP {st}, skipped')
                            continue
                        title = re.sub(r'\s*\(\d+\s+pages?\)\s*', '', e.title).strip()
                        sub_level = (level + 1) if sec.title else max(level, 2)
                        nav.append({'title': title, 'level': sub_level, 'pages': []})
                        _expand(mtree.parse_llms_index(body, e.url), sub_level - 1, depth + 1, title)
                        continue
                    path = mtree.page_path_for(e.url, scope_url)
                    if path is None or mtree.is_excluded(path, excludes) or path in planned:
                        continue
                    planned[path] = (e.title, e.url)
                    (node if node is not None else nav[-1])['pages'].append(
                        {'path': path, 'title': e.title})

        _expand(index, 0, 0)

        note = None
        partial = False
        if len(planned) > max_pages:
            keep = list(planned)[:max_pages]
            note = f'max_pages={max_pages} reached: {len(planned) - max_pages} listed pages not captured'
            planned = {k: planned[k] for k in keep}
            partial = True
        if not planned:
            return self._tree_fail(app_id, meta_file, mirror_url, method,
                                   f'{llms_url} lists no capturable pages inside {scope_url}')

        build_dir = self._tree_build_dir(app_id)
        shutil.rmtree(build_dir, ignore_errors=True)
        writer = mtree.TreeWriter(build_dir, app_id)
        store = self._http_asset_store(fetch, writer, TREE_ASSET_BUDGET_BYTES,
                                       host=urlsplit(scope_url).netloc,
                                       extra_hosts=app_entry.get('asset_hosts') or ())
        page_set = set(planned)
        failed: list[str] = []
        for path, (title, url) in planned.items():
            if time.monotonic() - started > LLMS_TIME_BUDGET_SECONDS:
                note = (note + '; ' if note else '') + 'time budget exhausted before every page was fetched'
                partial = True
                break
            md_url = url if url.lower().endswith('.md') else url + '.md'
            st, body = fetch.text(md_url)
            if st != 200 or not body.strip():
                failed.append(path)
                continue
            md = mtree.mintlify_to_markdown(body)
            md = mtree.localise_links(md, page_path=path, pages=page_set,
                                      app_id=app_id, scope_url=scope_url)
            md = mtree.localise_images(md, page_path=path, scope_url=scope_url, store=store)
            writer.add_page(path, title or mtree._title_from_h1(md) or path, md)

        if failed:
            partial = True
            note = (note + '; ' if note else '') + f'{len(failed)} listed page(s) could not be fetched'
        captured = set(writer.pages)
        for node in nav:
            node['pages'] = [p for p in node['pages'] if p['path'] in captured]
        nav = [n for n in nav if n['pages'] or n['title']]
        _origin, prefix = mtree._scope_parts(scope_url)
        tree = writer.finish(title=index.title or app_entry.get('name', app_id),
                             description=index.description, source=llms_url, nav=nav,
                             home='', scope_prefix=prefix)
        return self._tree_finish(app_id, meta_file, mirror_url, method, writer, tree,
                                 partial=partial, note=note,
                                 failed_pages=failed[:20], listed_pages=len(planned))

    def _mirror_with_git_markdown(self, app_id: str, app_entry: dict, mirror_url: str,
                                  app_cache: str, meta_file: str) -> str:
        """Capture markdown from a git repository (#1196): the GitHub wiki
        (`git_flavor: github-wiki`) or a docs directory (`docs-dir`, with
        `git_subdir` sparse-checked-out). Caller holds the per-app lock."""
        method = CAPTURE_GIT_MARKDOWN
        git_url = app_entry.get('git_url')
        if not git_url:
            return self._tree_fail(app_id, meta_file, mirror_url, method,
                                   f'{app_id} has no git_url configured in mirror_config.json')
        if not git_available():
            return self._tree_fail(
                app_id, meta_file, mirror_url, method,
                f'git binary not found on PATH ({GIT_BIN}) — {app_id} is configured for the '
                'git-markdown capture path but git is not installed in this environment')

        ref = app_entry.get('git_ref')
        subdir = (app_entry.get('git_subdir') or '').strip('/')
        tmp_root = os.path.join(DATA_DIR, 'tmp')
        os.makedirs(tmp_root, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f'{app_id}-git-', dir=tmp_root)
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        try:
            cmd = [GIT_BIN, 'clone', '--quiet', '--depth', '1']
            if ref:
                cmd += ['--branch', str(ref)]
            if subdir:
                cmd += ['--filter=blob:none', '--sparse']
            cmd += [git_url, tmp]
            print(f"[help-cache] Capturing {app_id} via git: {' '.join(cmd)}")
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=GIT_CLONE_TIMEOUT, env=env)
            if r.returncode != 0:
                return self._tree_fail(app_id, meta_file, mirror_url, method,
                                       f'git clone exit {r.returncode} | {(r.stderr or "")[-300:]}')
            if subdir:
                r = subprocess.run([GIT_BIN, '-C', tmp, 'sparse-checkout', 'set', subdir],
                                   capture_output=True, text=True, timeout=GIT_CLONE_TIMEOUT, env=env)
                if r.returncode != 0:
                    return self._tree_fail(app_id, meta_file, mirror_url, method,
                                           f'git sparse-checkout exit {r.returncode} | {(r.stderr or "")[-300:]}')
            rev = subprocess.run([GIT_BIN, '-C', tmp, 'rev-parse', '--short', 'HEAD'],
                                 capture_output=True, text=True, timeout=60, env=env)
            commit = (rev.stdout or '').strip() if rev.returncode == 0 else ''
            src_dir = os.path.join(tmp, subdir) if subdir else tmp
            if not os.path.isdir(src_dir):
                return self._tree_fail(app_id, meta_file, mirror_url, method,
                                       f'{subdir or "/"} not found in the clone of {git_url}')
            return self._build_tree_from_dir(app_id, app_entry, mirror_url, meta_file,
                                             src_dir, git_url, commit)
        except subprocess.TimeoutExpired:
            return self._tree_fail(app_id, meta_file, mirror_url, method,
                                   f'git clone timed out after {GIT_CLONE_TIMEOUT} seconds')
        except Exception as e:
            return self._tree_fail(app_id, meta_file, mirror_url, method,
                                   f'{e.__class__.__name__}: {e}')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _build_tree_from_dir(self, app_id: str, app_entry: dict, mirror_url: str,
                             meta_file: str, src_dir: str, source: str, commit: str) -> str:
        method = CAPTURE_GIT_MARKDOWN
        flavor = app_entry.get('git_flavor') or 'docs-dir'
        is_wiki = flavor == 'github-wiki'
        excludes = app_entry.get('exclude_directories') or []
        exclude_files = {f.strip('/') for f in (app_entry.get('exclude_files') or [])}
        scope_url = mirror_url if mirror_url.endswith('/') else mirror_url + '/'
        origin, prefix = mtree._scope_parts(scope_url)
        src_root = os.path.realpath(src_dir)

        # 1. the page set
        files: dict[str, str] = {}   # page key -> absolute .md path
        if is_wiki:
            for fn in sorted(os.listdir(src_root)):
                if not fn.lower().endswith('.md') or fn.startswith('_'):
                    continue
                if fn in exclude_files:
                    continue
                files[fn[:-3]] = os.path.join(src_root, fn)
        else:
            for dirpath, dirnames, filenames in os.walk(src_root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
                for fn in sorted(filenames):
                    if not fn.lower().endswith('.md'):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, src_root).replace(os.sep, '/')
                    if rel in exclude_files or fn in exclude_files:
                        continue
                    key = rel[:-3]
                    if mtree.is_excluded(key, excludes):
                        continue
                    files[key] = full
        if not files:
            return self._tree_fail(app_id, meta_file, mirror_url, method,
                                   f'no markdown pages found in {source}')
        pages = set(files)

        # 2. build
        build_dir = self._tree_build_dir(app_id)
        shutil.rmtree(build_dir, ignore_errors=True)
        writer = mtree.TreeWriter(build_dir, app_id)
        fetch = new_fetcher(delay=LLMS_FETCH_DELAY)
        http_store = self._http_asset_store(fetch, writer, TREE_ASSET_BUDGET_BYTES,
                                            host=urlsplit(origin).netloc,
                                            extra_hosts=app_entry.get('asset_hosts') or ())
        base = origin + prefix + '/'

        def store(url: str) -> str | None:
            # an image referenced relative to the docs root lives in the clone
            if url.startswith(base):
                rel = url[len(base):].split('#', 1)[0].split('?', 1)[0]
                local = os.path.realpath(os.path.join(src_root, rel))
                if local.startswith(src_root + os.sep) and os.path.isfile(local):
                    ext = _image_ext_for(local, '')
                    try:
                        size = os.path.getsize(local)
                        if ext and size <= TREE_ASSET_MAX_BYTES \
                                and writer.asset_bytes < TREE_ASSET_BUDGET_BYTES:
                            with open(local, 'rb') as f:
                                return writer.add_asset(f.read(), ext)
                    except OSError:
                        return None
                    return None
            return http_store(url)

        for key, path in files.items():
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError:
                continue
            if is_wiki:
                title = mtree._title_from_h1(text) or mtree.wiki_title_from_filename(os.path.basename(path))
                md = mtree.wiki_links_to_markdown(text, pages, app_id)
            else:
                title = mtree.docs_dir_title(path)
                md = mtree.docs_dir_to_markdown(text)
            md = mtree.localise_links(md, page_path=key, pages=pages, app_id=app_id,
                                      scope_url=scope_url)
            md = mtree.localise_images(md, page_path=key, scope_url=scope_url, store=store)
            writer.add_page(key, title, md)

        # 3. nav + home
        captured = set(writer.pages)
        if is_wiki:
            nav = []
            sidebar = os.path.join(src_root, '_Sidebar.md')
            if os.path.isfile(sidebar):
                with open(sidebar, 'r', encoding='utf-8', errors='replace') as f:
                    nav = mtree.parse_wiki_sidebar(f.read(), captured)
            home = 'Home' if 'Home' in captured else ''
        else:
            ordered = []
            for fn in app_entry.get('git_nav') or []:
                key = fn[:-3] if fn.lower().endswith('.md') else fn
                if key in captured and key not in ordered:
                    ordered.append(key)
            rest = sorted(k for k in captured if k not in ordered)
            if 'index' in rest:
                rest.remove('index')
                rest.insert(0, 'index')
            nav = [{'title': '', 'level': 1,
                    'pages': [{'path': k, 'title': writer.pages[k]['title']} for k in ordered + rest]}]
            home = 'index' if 'index' in captured else ''
        # pages the sidebar / nav list does not mention are still reachable:
        # list them at the end (the home page is the index route itself).
        listed = {p['path'] for n in nav for p in n['pages']} | {home}
        leftovers = [k for k in captured if k not in listed]
        if leftovers and nav and nav[0]['pages']:
            nav.append({'title': 'Other pages', 'level': 2,
                        'pages': [{'path': k, 'title': writer.pages[k]['title']} for k in leftovers]})

        tree = writer.finish(title=app_entry.get('name', app_id), description='',
                             source=source, nav=nav, home=home, scope_prefix=prefix,
                             extra={'git_commit': commit, 'git_flavor': flavor})
        return self._tree_finish(app_id, meta_file, mirror_url, method, writer, tree,
                                 partial=False, note=None, git_commit=commit)

    def has_valid_wget_tree(self, app_id: str) -> bool:
        """True if the last wget capture of app_id passed its gates and left
        files on disk — what a wget-first local_doc module (#1196:
        komodo/gotenberg) branches on to serve the tree over the curated
        fallback page."""
        meta, meta_error = self._read_meta(self._meta_path(app_id))
        if meta_error is not None or meta is None or not meta.get('success'):
            return False
        if meta.get('capture_method') not in (None, CAPTURE_WGET):
            return False
        app_cache = os.path.join(CACHE_DIR, app_id)
        try:
            return os.path.isdir(app_cache) and bool(os.listdir(app_cache))
        except OSError:
            return False

    def has_valid_markdown_tree(self, app_id: str) -> bool:
        """True if a completed markdown-tree capture is on disk for app_id.
        `_tree.json` is written only by a build that passed the gate and was
        swapped into place, so its presence IS the validity signal — a later
        failed refresh leaves it (and is reported by the meta record)."""
        cache_dir = os.path.join(CACHE_DIR, app_id)
        return mtree.read_tree(cache_dir) is not None

    def _tree_toc_html(self, app_id: str, tree: dict) -> str:
        pages = tree.get('pages', {})
        parts = ['<nav class="mtree-toc">']
        any_pages = False
        for node in tree.get('nav', []):
            links = [p for p in node.get('pages', []) if p.get('path') in pages]
            if not links and not node.get('title'):
                continue
            if node.get('title'):
                level = int(node.get('level') or 2)
                tag = 'h2' if level <= 2 else ('h3' if level == 3 else 'h4')
                parts.append(f'<{tag}>{html.escape(str(node["title"]))}</{tag}>')
            if links:
                any_pages = True
                parts.append('<ul>')
                for p in links:
                    parts.append(f'<li><a href="/docs/{app_id}/{html.escape(p["path"], quote=True)}/">'
                                 f'{html.escape(str(p.get("title") or p["path"]))}</a></li>')
                parts.append('</ul>')
        if not any_pages:
            parts.append('<ul>')
            for key, info in pages.items():
                parts.append(f'<li><a href="/docs/{app_id}/{html.escape(key, quote=True)}/">'
                             f'{html.escape(str(info.get("title") or key))}</a></li>')
            parts.append('</ul>')
        parts.append('</nav>')
        return ''.join(parts)

    def _tree_source_line(self, app_entry: dict, tree: dict) -> str:
        upstream = html.escape(_safe_http_url(app_entry.get('mirror_url')), quote=True)
        name = html.escape(str(app_entry.get('name', tree.get('title', ''))))
        lic = html.escape(str(app_entry.get('license', '')))
        when = html.escape(str(tree.get('captured_at', ''))[:10])
        local_link = ''
        if app_entry.get('local_doc') and app_entry.get('id'):
            local_link = (f' · <a href="/docs/{html.escape(str(app_entry["id"]), quote=True)}/'
                          f'{LOCAL_DOC_ROUTE}/">Notes for this box</a>')
        return (f'<p class="mtree-source">Offline copy of the <a href="{upstream}" target="_blank" '
                f'rel="noopener">{name} documentation</a>'
                + (f' · License: {lic}' if lic else '')
                + (f' · captured {when}' if when else '') + local_link + '</p>')

    def _strip_leading_h1(self, md: str) -> tuple[str, str]:
        lines = md.splitlines()
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i < len(lines) and lines[i].startswith('# '):
            return lines[i][2:].strip(), '\n'.join(lines[i + 1:])
        return '', md

    def render_tree_page(self, app_id: str, request_path: str | None) -> tuple[str, str] | None:
        """(content_html, page_title) for a markdown-tree page, or None when
        the tree has no such page. An empty request path renders the index:
        the home page (if the tree names one) followed by the table of
        contents built from the capture's nav."""
        cache_dir = os.path.join(CACHE_DIR, app_id)
        tree = mtree.read_tree(cache_dir)
        if tree is None:
            return None
        app_entry = self.get_app_entry(app_id) or {}
        rel = (request_path or '').strip('/')
        if not rel:
            parts = []
            if tree.get('description'):
                parts.append(f'<p class="mtree-desc">{html.escape(str(tree["description"]))}</p>')
            home = tree.get('home')
            if home and home in tree.get('pages', {}):
                md = mtree.read_page_markdown(cache_dir, tree, home) or ''
                _title, body = self._strip_leading_h1(md)
                parts.append(self._render_markdown(body, '', extensions=TREE_MARKDOWN_EXTENSIONS))
            parts.append(self._tree_toc_html(app_id, tree))
            parts.append(self._tree_source_line(app_entry, tree))
            return ''.join(parts), str(tree.get('title') or app_entry.get('name') or app_id)

        page = mtree.lookup_page(tree, rel)
        if page is None:
            return None
        if page == tree.get('home'):
            # the home page IS the index route; keep one canonical rendering
            return self.render_tree_page(app_id, '')
        md = mtree.read_page_markdown(cache_dir, tree, page)
        if md is None:
            return None
        h1, body = self._strip_leading_h1(md)
        title = h1 or tree['pages'][page].get('title') or page
        section = ''
        for node in tree.get('nav', []):
            if any(p.get('path') == page for p in node.get('pages', [])):
                section = str(node.get('title') or '')
                break
        crumb = (f'<p class="mtree-crumb"><a href="/docs/{app_id}/">'
                 f'{html.escape(str(tree.get("title") or app_entry.get("name") or app_id))}</a>'
                 + (f' › {html.escape(section)}' if section else '') + '</p>')
        rendered = self._render_markdown(body, '', extensions=TREE_MARKDOWN_EXTENSIONS)
        prev_p, next_p = mtree.nav_neighbours(tree, page)
        footer = ['<div class="mtree-pn">']
        if prev_p:
            footer.append(f'<a class="prev" href="/docs/{app_id}/{html.escape(prev_p["path"], quote=True)}/">'
                          f'← {html.escape(str(prev_p["title"]))}</a>')
        if next_p:
            footer.append(f'<a class="next" href="/docs/{app_id}/{html.escape(next_p["path"], quote=True)}/">'
                          f'{html.escape(str(next_p["title"]))} →</a>')
        footer.append('</div>')
        return crumb + rendered + ''.join(footer) + self._tree_source_line(app_entry, tree), title

    def tree_corpus_text(self, app_id: str, limit: int = 200_000) -> str:
        """Plain text of a markdown tree for the search index (#982): the
        title/description plus every page's markdown, up to `limit`."""
        cache_dir = os.path.join(CACHE_DIR, app_id)
        tree = mtree.read_tree(cache_dir)
        if tree is None:
            return ''
        chunks = [str(tree.get('title') or ''), str(tree.get('description') or '')]
        total = sum(len(c) for c in chunks)
        for key, info in tree.get('pages', {}).items():
            if total >= limit:
                break
            md = mtree.read_page_markdown(cache_dir, tree, key) or ''
            chunk = f"{info.get('title') or key}\n{md}"
            chunks.append(chunk)
            total += len(chunk)
        return '\n'.join(chunks)[:limit]

    # ------------------------------------------------------------------
    # refresh-all run record (#1055)
    # ------------------------------------------------------------------

    def get_last_refresh_all(self) -> dict | None:
        """The per-module PASS/FAIL summary of the last `mirror_all()` run.

        `mirror_all` built exactly this and then `print`-ed a one-line count to
        the container log before dropping it — so the answer to "which modules
        did that refresh actually fix, and which failed again" lived only in
        `docker logs`, and only until the log rotated. Persisted now, and
        surfaced by /api/cache-status + the admin page.
        """
        meta, meta_error = self._read_meta(os.path.join(META_DIR, REFRESH_ALL_META))
        if meta_error is not None:
            return None
        return meta

    def mirror_all(self) -> dict:
        """Mirror all configured apps. Returns {app_id: outcome}.

        #525: the values are MIRROR_OUTCOMES strings, not bools, so a caller can
        tell "18 mirrored" from "18 mirrored, 6 had nothing to mirror". The
        second number is the one that was invisible.

        #1055: a per-module exception no longer aborts the whole run. One
        module raising (an unreadable meta, a permission error under
        CACHE_DIR, …) used to take the remaining modules with it — the
        operator saw a refresh that stopped partway with no record of where,
        which is the "refresh-all is unreliable" report. Each module's outcome
        is recorded independently, and the run summary is persisted.
        """
        config = self.get_config()
        results = {}
        errors = {}
        started = datetime.utcnow().isoformat() + 'Z'
        for app_entry in config.get('apps', []):
            app_id = app_entry['id']
            try:
                results[app_id] = self.mirror_docs(app_id)
            except Exception as e:  # never let one module end the run
                results[app_id] = MIRROR_FAILED
                errors[app_id] = f'{e.__class__.__name__}: {e}'
                print(f'[help-cache] refresh-all: {app_id} raised — {e}')
        counts = {}
        for outcome in results.values():
            counts[outcome] = counts.get(outcome, 0) + 1
        print(f"[help-cache] refresh-all complete: "
              + ", ".join(f"{n} {o}" for o, n in sorted(counts.items())))

        self._write_meta(os.path.join(META_DIR, REFRESH_ALL_META), {
            'started': started,
            'finished': datetime.utcnow().isoformat() + 'Z',
            'results': results,
            'counts': counts,
            'exceptions': errors,
            # The list an operator actually wants: modules whose docs are NOT
            # in a trustworthy state after this run.
            'failed': [f['id'] for f in self.get_failed_modules()],
        })
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

    def _render_markdown(self, md_content: str, doc_dir: str, extensions=()) -> str:
        """Shared markdown → safe-HTML core for own-docs AND module pages.

        `doc_dir` is the on-disk directory of the source .md relative to its
        docs root — used ONLY to resolve RELATIVE image (`../images/…`) and
        inter-doc (`foo.md`) links to the /docs-image and /docs/<slug>/ routes.
        Pass '' for pages that use only absolute links (module pages). Handles
        <domain>/<server-ip> placeholders, ```mermaid fences, and bleaches.
        `extensions` adds python-markdown extensions for a caller whose
        source needs them (#1196 tree pages: admonition, md_in_html); own
        docs and module pages render exactly as before.
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
            md_content,
            extensions=['tables', 'fenced_code', 'toc', 'attr_list', *extensions])

        # Rewrite relative image sources to the served image route. The doc lives
        # at docs/enterprise/<relpath>; images live at docs/enterprise/images/. A link
        # like ../images/foo/bar.png from how-to/x.md resolves to images/foo/bar.png.
        # A root-absolute src (`/docs/<app>/_assets/…`, #1196 tree assets) is
        # already a served route and is left alone.
        def _rewrite_img(m):
            attr, src = m.group(1), m.group(2)
            if src.startswith(('http://', 'https://', '/', 'data:')):
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

    def _link_scopes(self) -> list[tuple[str, str, str, tuple]]:
        """(netloc, path_prefix, app_id, exclude_dirs) per mirrored app,
        longest path-prefix first (#1072).

        The prefix is the mirror_url's PATH — the part of the remote site that
        was actually mirrored. It is what lets rewrite_links tell "this URL is
        inside a mirrored doc tree" from "this URL merely shares the host":
        github.com hosts the Vaultwarden wiki mirror, but only
        /dani-garcia/vaultwarden/wiki/** of github.com exists locally; and
        docs.gpustack.ai carries TWO apps (/0.7/ vs /2.1/) whose dict-keyed
        predecessor collapsed into whichever entry was iterated last.
        Longest-prefix-first makes the same-host discrimination deterministic.
        """
        scopes = []
        for entry in self.get_config().get('apps', []):
            mirror_url = entry.get('mirror_url', '')
            if not mirror_url:
                continue
            parsed = urlsplit(mirror_url)
            # #1196: a `crawl_scope` (komodo/gotenberg: "/docs") is the part
            # of the site that was actually mirrored — wider than the entry
            # page's own path, which is what mirror_url names for them.
            prefix = (entry.get('crawl_scope') or parsed.path).rstrip('/')
            excludes = tuple(entry.get('exclude_directories') or [])
            scopes.append((parsed.netloc, prefix, entry['id'], excludes))
        scopes.sort(key=lambda s: len(s[1]), reverse=True)
        return scopes

    _ABS_URL_RE = re.compile(r'https?://[^\s"\'<>\\)]+')

    def rewrite_links(self, html_content: str, app_id: str) -> str:
        """Localise absolute URLs that point INSIDE a mirrored doc scope;
        leave every other absolute URL external (#1072).

        The previous implementation replaced `scheme://netloc` per app across
        the whole page — host-keyed, path-blind. Measured fallout (0.91,
        2026-09-01, 1200-page crawl → 269 broken links): every github.com link
        in ANY mirror pointed into the Vaultwarden wiki tree (161×), the
        gpustack v0.7 pages linked into the v2.1 tree (70×), out-of-scope
        marketing links (blog/careers) were localised to 404s (15×), and
        excluded directories were still localised (7×). Scope-aware mapping
        kills all four families in one move; the local path keeps the FULL
        remote path (the on-disk cache stores it that way), so existing cache
        trees serve unchanged.
        """
        scopes = self._link_scopes()

        def _map_abs(m):
            url = m.group(0)
            # urlSPLIT, not urlparse: urlparse additionally splits the last path
            # segment on `;` into path + params (RFC 2396), and the local URL is
            # rebuilt from `.path` alone — so every href carrying an HTML entity
            # (`&amp;`, the mandatory spelling of `&` in an href) lost its `;`
            # AND its tail: `.../System&amp;Global-Settings` came out as
            # `/docs/stirling-pdf/Configuration/System&amp`, i.e. the measured
            # `…/System&` 404s (6× in the crawl). Browsers (WHATWG URL) do no
            # params splitting either, so urlsplit is also the faithful model.
            parsed = urlsplit(url)
            for netloc, prefix, aid, excludes in scopes:
                if parsed.netloc != netloc:
                    continue
                path = parsed.path or '/'
                if prefix and not (path == prefix or path.startswith(prefix + '/')):
                    continue  # same host, outside the mirrored subtree → external
                # excludes are scope-relative ("/releases" of the mirrored root)
                rel = path[len(prefix):] or '/'
                if any(rel == ex.rstrip('/') or rel.startswith(ex.rstrip('/') + '/')
                       for ex in excludes if ex):
                    return url  # mirrored host but excluded dir → keep external
                local = f'/docs/{aid}{path}'
                if parsed.query:
                    local += '?' + parsed.query
                if parsed.fragment:
                    local += '#' + parsed.fragment
                return local
            return url  # no mirrored scope claims it → stays external

        return self._ABS_URL_RE.sub(_map_abs, html_content)

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

    def inject_attribution(self, html_content: str, app_entry: dict, app_id: str,
                           filepath: str = '', notice: str | None = None) -> str:
        """
        Inject a small floating attribution bar into the cached HTML page.
        Preserves the original page layout, CSS, and JavaScript.
        Also rewrites links to point to local cache.

        `notice` adds one line to the bar (#1229 review MEDIUM 4): the viewer
        passes it when the tree being served is the LAST GOOD capture and the
        newest refresh failed, so "this may be out of date" is visible on the
        page instead of only in the admin table.
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

        # HLP-11: these three come out of mirror_config.json and are
        # f-string-interpolated straight into markup below. Repo-controlled
        # today, but a single `"` in a name silently breaks the attribution
        # bar's markup, and a `javascript:` mirror_url would be an executable
        # link inside a mirrored page. Escape all three, and let only
        # http(s) through as an href — anything else degrades to a dead
        # anchor rather than a live one.
        license_name = html.escape(str(app_entry.get('license', 'Unknown')))
        app_name = html.escape(str(app_entry.get('name', 'Unknown')))
        mirror_url = html.escape(_safe_http_url(app_entry.get('mirror_url')), quote=True)
        # Same escaping discipline as the three above (HLP-11): the notice is
        # built from a meta-record timestamp, but it lands inside markup.
        notice_html = (
            f'<span style="color:#8a5a00;font-weight:600;">{html.escape(str(notice))}</span> · '
            if notice else '')
        # #1197: link the box-local notes page when the module ships one.
        local_link = ''
        if app_entry.get('local_doc'):
            local_link = (f' · <a href="/docs/{html.escape(str(app_id), quote=True)}/{LOCAL_DOC_ROUTE}/" '
                          'style="color:#CD1719;text-decoration:none;">Notes for this box</a>')

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
        {notice_html}📖 Cached from <a href="{mirror_url}" target="_blank" rel="noopener"
            style="color:#CD1719;text-decoration:none;">{app_name}</a>
        · License: {license_name}{local_link}
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
