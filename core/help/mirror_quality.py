# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Mirror content-quality probe (#1196).

The 2026-09-03 operator sweep of the Help Center found ten upstream doc
mirrors that were unusable while every one of them answered HTTP 200 and
wore a green "✓ Cached" badge. The failure shapes repeat across sources:

  raw-js              the visible text IS a minified JS bundle (a Next.js /
                      Mintlify hydration payload captured as page text);
  no-stylesheet       the page references no stylesheet and carries no
                      inline <style> — nothing can style it;
  stylesheet-missing  a stylesheet is referenced but none of the referenced
                      local stylesheets exists (wget's -X/-I also filter
                      page requisites, so "/assets" excludes strip the CSS);
  directory-listing   an Apache/nginx autoindex ("Index of /docs") was
                      captured as the landing page;
  external-redirect   a <meta http-equiv=refresh> or an inline
                      window.location bounce to a DIFFERENT host — an
                      air-gapped box would have no docs at all;
  tombstone           a thin "this page has permanently moved" stub;
  thin-text           an unrendered SPA shell (mirrors
                      cache_manager.validate_capture_substance's floor).

Not every shape is equally fatal (#1229 review, BLOCKER 3). A directory
listing, a foreign redirect, a tombstone, a raw-JS bundle or an unrendered
shell means the reader gets NO documentation — those fail closed. "The page
has no stylesheet" does not: measured on tika.apache.org, 40 398 characters
of real prose arrived with no stylesheet at all, because that is how the
UPSTREAM ships it — and a rule that cannot tell "the capture lost the CSS"
from "there is no CSS to lose" was switching a working mirror off. So a
stylesheet complaint over a page that carries substantial prose is
**degraded**, not fatal: the capture is served, flagged partial, and shown
to the operator — see `FATAL_PROBLEMS` / `DEGRADABLE_PROBLEMS` below.

This module is deliberately **stdlib-only and Flask-free** so the same
verdict can be reached in three places: at capture time over the cache tree
(cache_manager), in unit tests over fixtures and mirror output, and from the
day-1 acceptance tier (#1201) over a live `help.<domain>/docs/<app>/`
response. Presence of assets is delegated to an `asset_exists(path)`
callback so the caller decides whether "present" means "on disk in the cache
tree" or "answers a HEAD request on the box".

CLI (for day-1 / ad-hoc use):
    python3 mirror_quality.py <file.html | https://…> [--host <page-host>]
exits 0 when the page passes, 1 when it does not, and prints the report.
"""
from __future__ import annotations

import html as _html
import os
import posixpath
import re
import sys
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

PROBLEM_RAW_JS = 'raw-js'
PROBLEM_NO_STYLESHEET = 'no-stylesheet'
PROBLEM_STYLESHEET_MISSING = 'stylesheet-missing'
PROBLEM_DIRECTORY_LISTING = 'directory-listing'
PROBLEM_EXTERNAL_REDIRECT = 'external-redirect'
PROBLEM_TOMBSTONE = 'tombstone'
PROBLEM_THIN_TEXT = 'thin-text'
PROBLEM_NO_PAGE = 'no-page'

ALL_PROBLEMS = (
    PROBLEM_RAW_JS, PROBLEM_NO_STYLESHEET, PROBLEM_STYLESHEET_MISSING,
    PROBLEM_DIRECTORY_LISTING, PROBLEM_EXTERNAL_REDIRECT, PROBLEM_TOMBSTONE,
    PROBLEM_THIN_TEXT, PROBLEM_NO_PAGE,
)

#: Verdict severities. `ok` = nothing found; `degraded` = the capture is
#: readable but cosmetically wrong (serve it, flag it); `fatal` = the reader
#: would get junk (fail closed, keep the last good tree / the local_doc).
SEVERITY_OK = 'ok'
SEVERITY_DEGRADED = 'degraded'
SEVERITY_FATAL = 'fatal'

#: The two rules that can DEGRADE instead of failing — and only when the page
#: carries substantial prose. On a thin page a missing stylesheet is a symptom
#: of a broken capture; on 40 000 characters of documentation it is a fact
#: about the upstream (tika.apache.org ships no CSS at all), and the same
#: verdict would have switched off the eight MkDocs mirrors whose `-X /assets`
#: rule drops the stylesheet the completion pass could not re-fetch.
DEGRADABLE_PROBLEMS = (PROBLEM_NO_STYLESHEET, PROBLEM_STYLESHEET_MISSING)

#: Everything else means "there is no readable documentation here".
FATAL_PROBLEMS = tuple(p for p in ALL_PROBLEMS if p not in DEGRADABLE_PROBLEMS)


def split_severity(problems, *, text_chars: int,
                   min_text_chars: int = None) -> tuple[list[str], list[str]]:
    """Split `problems` into (fatal, degraded).

    A DEGRADABLE problem degrades only over a page with at least
    `min_text_chars` of visible text — the same floor `PROBLEM_THIN_TEXT`
    uses, so "substantial prose" means one thing in this module. Below it the
    page is thin anyway (PROBLEM_THIN_TEXT has already fired, and that is
    fatal), and a naked thin page is exactly the broken-capture shape the
    gate exists for."""
    if min_text_chars is None:
        min_text_chars = MIN_TEXT_CHARS
    substantial = text_chars >= min_text_chars
    fatal, degraded = [], []
    for p in problems:
        if p in DEGRADABLE_PROBLEMS and substantial:
            degraded.append(p)
        else:
            fatal.append(p)
    return fatal, degraded

#: Visible-text floor. Same number as cache_manager.CAPTURE_MIN_TEXT_CHARS so
#: a page judged "substantive" by the monolith path is judged the same here.
MIN_TEXT_CHARS = 400
#: An inline <style> budget below this is a reset snippet, not a theme.
INLINE_STYLE_MIN_CHARS = 200
#: Tombstone phrases only count on a page THIS thin — a migration guide that
#: says "has permanently moved" in the middle of real prose is documentation.
TOMBSTONE_MAX_TEXT_CHARS = 600
#: How much of the visible text is inspected for the raw-JS shape.
RAW_JS_WINDOW = 400

_TOMBSTONE_PHRASES = (
    'permanently moved',
    'page has moved',
    'documentation has moved',
    'has moved to',
    'will be redirected',
    'redirecting you to',
    'redirecting to',
)

_RAW_JS_MARKERS = (
    'self.__next_f.push', '__next_data__', '__webpack', 'webpackchunk',
    '!function(', '(function(', '"use strict"', "'use strict'",
)
_RAW_JS_TOKENS = ('function', 'return', '=>', 'var ', 'void 0', '!0', '!1',
                  '.push(', 'exports')

_TAG_RE = re.compile(r'<[^>]+>')
#: #1229 review LOW 6 — a documentation page may legitimately OPEN with a
#: JavaScript example, and a fenced example is dense in `{}();=` exactly like
#: a minified bundle. Code the author marked up AS code is never the raw-JS
#: shape, so it is removed before the window is measured. `<script>` is NOT
#: stripped here: raw-js means JS text rendered as page content, which
#: `visible_text` already drops when it sits inside a real <script> element.
_PRE_CODE_RE = re.compile(r'<(pre|code)\b[^>]*>.*?</\1\s*>',
                          re.IGNORECASE | re.DOTALL)
_STRIP_BLOCKS_RE = re.compile(
    r'<(script|style|noscript|template|head|svg)\b[^>]*>.*?</\1\s*>',
    re.IGNORECASE | re.DOTALL)
_LINK_TAG_RE = re.compile(r'<link\b[^>]*>', re.IGNORECASE)
_STYLE_BLOCK_RE = re.compile(r'<style\b[^>]*>(.*?)</style\s*>', re.IGNORECASE | re.DOTALL)
_SCRIPT_BLOCK_RE = re.compile(r'<script\b[^>]*>(.*?)</script\s*>', re.IGNORECASE | re.DOTALL)
_META_TAG_RE = re.compile(r'<meta\b[^>]*>', re.IGNORECASE)
_TITLE_RE = re.compile(r'<title\b[^>]*>(.*?)</title\s*>', re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r'<h1\b[^>]*>(.*?)</h1\s*>', re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r'''([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))''')
_JS_LOCATION_RE = re.compile(
    r'''(?:window\.|document\.|top\.|self\.)?location(?:\.href)?\s*=\s*["'](https?://[^"']+)["']'''
    r'''|location\.(?:replace|assign)\s*\(\s*["'](https?://[^"']+)["']''',
    re.IGNORECASE)


def _attrs(tag: str) -> dict[str, str]:
    out = {}
    for m in _ATTR_RE.finditer(tag):
        name = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4))
        out[name] = _html.unescape(val or '')
    return out


def visible_text(html_content: str) -> str:
    """Whitespace-collapsed text a reader would actually see.

    Drops <script>/<style>/<noscript>/<template>/<head>/<svg> bodies before
    stripping tags — a <noscript> "enable JavaScript" hint and an inline
    hydration payload are not content."""
    if not html_content:
        return ''
    text = _STRIP_BLOCKS_RE.sub(' ', html_content)
    text = _TAG_RE.sub(' ', text)
    text = _html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def visible_prose(html_content: str) -> str:
    """`visible_text` with author-marked code (`<pre>`, `<code>`) removed.

    The text a RAW-JS verdict is allowed to look at: a page whose first
    paragraph is a `<pre><code>` JavaScript sample is documentation about
    JavaScript, not a hydration payload captured as page text."""
    return visible_text(_PRE_CODE_RE.sub(' ', html_content or ''))


def is_same_host(url: str, host: str | None) -> bool:
    """True when `url` is an absolute http(s) URL on `host`.

    The origin bound the markdown-tree image store needs (#1229 review LOW 5):
    `mirror_assets` never leaves its own origin because it BUILDS every URL
    from it, while `markdown_tree.localise_images` hands over whatever the
    upstream markdown referenced — including third-party hosts."""
    if not url or not host:
        return False
    parts = urlsplit(url)
    return parts.scheme in ('http', 'https') and parts.netloc.lower() == host.lower()


def _looks_like_raw_js(text: str) -> bool:
    window = text[:RAW_JS_WINDOW]
    if len(window) < 120:
        return False
    low = window.lower()
    if any(marker in low for marker in _RAW_JS_MARKERS):
        return True
    symbols = sum(1 for ch in window if ch in '{}();=[]')
    density = symbols / max(1, len(window))
    if density >= 0.12 and any(tok in window for tok in _RAW_JS_TOKENS):
        return True
    return False


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).netloc or '').lower()
    except ValueError:
        return ''


def is_local_ref(ref: str, page_host: str | None) -> bool:
    """True if `ref` names something the mirror itself can serve: a relative
    or root-relative path, or an absolute URL on the page's own host."""
    if not ref:
        return False
    r = ref.strip()
    low = r.lower()
    if low.startswith(('data:', 'javascript:', 'mailto:', 'blob:', 'about:')):
        return False
    if low.startswith('//'):
        return bool(page_host) and _host_of('https:' + r) == page_host.lower()
    if re.match(r'^[a-z][a-z0-9+.-]*:', low):
        if not low.startswith(('http:', 'https:')):
            return False
        return bool(page_host) and _host_of(r) == page_host.lower()
    return True


def resolve_local_ref(ref: str, page_relpath: str = '', page_host: str | None = None) -> str | None:
    """Map a local asset reference to its path inside the cache tree.

    Absolute same-host URLs and root-relative paths map from the tree root;
    relative refs resolve against the referencing page's directory. The
    query string is KEPT (wget keeps it in the saved filename); the fragment
    is dropped. Returns None for a non-local ref or one escaping the root."""
    if not is_local_ref(ref, page_host):
        return None
    r = ref.strip()
    if r.lower().startswith('//'):
        r = 'https:' + r
    parts = urlsplit(r)
    path = parts.path
    query = ('?' + parts.query) if parts.query else ''
    if parts.scheme or r.startswith('/'):
        rel = path.lstrip('/')
    else:
        base_dir = posixpath.dirname(page_relpath.replace(os.sep, '/')) if page_relpath else ''
        rel = posixpath.normpath(posixpath.join(base_dir, path)) if path else base_dir
    rel = posixpath.normpath(rel) if rel else ''
    if rel in ('', '.'):
        return None
    if rel.startswith('../') or rel == '..' or rel.startswith('/'):
        return None
    return rel + query


def _stylesheet_refs(html_content: str) -> list[str]:
    refs = []
    for tag in _LINK_TAG_RE.findall(html_content):
        a = _attrs(tag)
        rel = a.get('rel', '').lower().split()
        href = a.get('href', '').strip()
        if not href:
            continue
        if 'stylesheet' in rel or ('preload' in rel and a.get('as', '').lower() == 'style'):
            refs.append(href)
    return refs


def _meta_refresh_target(html_content: str) -> str | None:
    for tag in _META_TAG_RE.findall(html_content):
        a = _attrs(tag)
        if a.get('http-equiv', '').lower() != 'refresh':
            continue
        content = a.get('content', '')
        m = re.search(r'url\s*=\s*["\']?([^"\';\s]+)', content, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _script_redirect_target(html_content: str) -> str | None:
    for body in _SCRIPT_BLOCK_RE.findall(html_content):
        m = _JS_LOCATION_RE.search(body)
        if m:
            return m.group(1) or m.group(2)
    return None


def _is_external(target: str, page_host: str | None) -> bool:
    host = _host_of(target)
    if not host:
        return False  # relative / same-origin routing
    if not page_host:
        return True   # served on our own origin, any absolute host is foreign
    return host != page_host.lower()


@dataclass
class QualityReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    #: The problems that make the capture unservable. Empty on a page that is
    #: merely degraded — that one is SERVED.
    fatal: list[str] = field(default_factory=list)
    #: Cosmetic problems worth reporting over a capture that is still served.
    degraded: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str:
        if self.fatal:
            return SEVERITY_FATAL
        if self.degraded:
            return SEVERITY_DEGRADED
        return SEVERITY_OK

    @property
    def usable(self) -> bool:
        """True when the capture may be served. Wider than `ok`: a page whose
        only complaint is a missing stylesheet is readable documentation."""
        return not self.fatal

    def summary(self) -> str:
        """One line for meta records / container logs / day-1 output."""
        if self.ok:
            return 'content-quality: ok'
        hints = []
        if PROBLEM_DIRECTORY_LISTING in self.problems and self.details.get('title'):
            hints.append(f'title {self.details["title"]!r}')
        if PROBLEM_EXTERNAL_REDIRECT in self.problems and self.details.get('redirect_target'):
            hints.append(f'redirects to {self.details["redirect_target"]}')
        if PROBLEM_STYLESHEET_MISSING in self.problems:
            hints.append('missing ' + ', '.join(self.details.get('missing_stylesheets', [])[:3]))
        if PROBLEM_THIN_TEXT in self.problems:
            hints.append(f'{self.details.get("text_chars", 0)} chars of visible text')
        if PROBLEM_TOMBSTONE in self.problems and self.details.get('tombstone_phrase'):
            hints.append(f'says {self.details["tombstone_phrase"]!r}')
        tail = f' ({"; ".join(hints)})' if hints else ''
        prefix = 'content-quality'
        if self.severity == SEVERITY_DEGRADED:
            # Say WHY it is still served, so an operator reading the admin
            # row does not read a warning as an outage.
            prefix = 'content-quality (degraded — served)'
        return prefix + ': ' + ', '.join(self.problems) + tail


def assess_page(html_content: str, *,
                asset_exists: Callable[[str], bool] | None = None,
                page_relpath: str = '',
                page_host: str | None = None,
                min_text_chars: int = MIN_TEXT_CHARS) -> QualityReport:
    """Judge one captured/served HTML page on the failure shapes above.

    `asset_exists(cache_path)` decides whether a referenced local stylesheet
    is actually available; pass None when only the HTML is known (a live
    probe) and presence is not judged. `page_relpath` is the page's path in
    the tree (for relative refs); `page_host` the upstream host (so an
    absolute same-host ref counts as local and a foreign redirect is
    recognised)."""
    problems: list[str] = []
    details: dict = {}
    html_content = html_content or ''

    title_m = _TITLE_RE.search(html_content)
    title = visible_text(title_m.group(1)) if title_m else ''
    details['title'] = title

    text = visible_text(html_content)
    details['text_chars'] = len(text)

    # directory listing — the openuem class
    h1_m = _H1_RE.search(html_content)
    h1 = visible_text(h1_m.group(1)) if h1_m else ''
    if title.lower().startswith('index of ') or h1.lower().startswith('index of '):
        problems.append(PROBLEM_DIRECTORY_LISTING)

    # external redirect — the presidio (meta refresh) and RTD (inline JS) classes
    target = _meta_refresh_target(html_content)
    if target and _is_external(target, page_host):
        problems.append(PROBLEM_EXTERNAL_REDIRECT)
        details['redirect_target'] = target
    else:
        target = _script_redirect_target(html_content)
        if target and _is_external(target, page_host):
            problems.append(PROBLEM_EXTERNAL_REDIRECT)
            details['redirect_target'] = target

    # tombstone — thin page whose text says it moved
    if len(text) < TOMBSTONE_MAX_TEXT_CHARS:
        low = text.lower()
        for phrase in _TOMBSTONE_PHRASES:
            if phrase in low:
                problems.append(PROBLEM_TOMBSTONE)
                details['tombstone_phrase'] = phrase
                break

    # raw JS as text — the Mintlify class. Judged over the prose only: a
    # page opening with a <pre><code> JavaScript sample is not a bundle.
    if _looks_like_raw_js(visible_prose(html_content)):
        problems.append(PROBLEM_RAW_JS)

    # thin text — the unrendered SPA shell
    if len(text) < min_text_chars:
        problems.append(PROBLEM_THIN_TEXT)

    # stylesheets
    refs = _stylesheet_refs(html_content)
    local_refs = [r for r in refs if is_local_ref(r, page_host)]
    inline_css = sum(len(b.strip()) for b in _STYLE_BLOCK_RE.findall(html_content))
    details['stylesheets'] = local_refs
    details['inline_style_chars'] = inline_css
    if not local_refs and inline_css < INLINE_STYLE_MIN_CHARS:
        problems.append(PROBLEM_NO_STYLESHEET)
    elif local_refs and asset_exists is not None:
        present, missing = [], []
        for ref in local_refs:
            path = resolve_local_ref(ref, page_relpath, page_host)
            if path is None:
                continue
            (present if asset_exists(path) else missing).append(path)
        details['missing_stylesheets'] = missing
        if missing and not present and inline_css < INLINE_STYLE_MIN_CHARS:
            problems.append(PROBLEM_STYLESHEET_MISSING)

    fatal, degraded = split_severity(problems, text_chars=len(text),
                                     min_text_chars=min_text_chars)
    details['severity'] = (SEVERITY_FATAL if fatal else
                           SEVERITY_DEGRADED if degraded else SEVERITY_OK)
    return QualityReport(ok=not problems, problems=problems, details=details,
                         fatal=fatal, degraded=degraded)


def _resolve_page_file(cache_dir: str, relpath: str) -> str | None:
    """Same resolution order app.py::view_docs uses — exact file, directory
    index.html, implicit .html — traversal-guarded."""
    root = os.path.realpath(cache_dir)
    rel = (relpath or '').strip('/')
    candidate = os.path.realpath(os.path.join(root, rel)) if rel else root
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    if os.path.isfile(candidate):
        return candidate
    for alt in (os.path.join(candidate, 'index.html'), candidate + '.html'):
        if os.path.isfile(alt):
            return alt
    return None


def assess_cache_page(cache_dir: str, entry_relpath: str, *,
                      page_host: str | None = None,
                      min_text_chars: int = MIN_TEXT_CHARS) -> QualityReport:
    """Capture-time entry point: judge the page `entry_relpath` resolves to
    inside `cache_dir`, with asset presence checked on disk."""
    page_file = _resolve_page_file(cache_dir, entry_relpath)
    if page_file is None:
        return QualityReport(ok=False, problems=[PROBLEM_NO_PAGE],
                             details={'entry': entry_relpath,
                                      'severity': SEVERITY_FATAL},
                             fatal=[PROBLEM_NO_PAGE])
    root = os.path.realpath(cache_dir)
    try:
        with open(page_file, 'r', encoding='utf-8', errors='replace') as f:
            html_content = f.read()
    except OSError as e:
        return QualityReport(ok=False, problems=[PROBLEM_NO_PAGE],
                             details={'entry': entry_relpath, 'error': str(e),
                                      'severity': SEVERITY_FATAL},
                             fatal=[PROBLEM_NO_PAGE])

    def _exists(path: str) -> bool:
        target = os.path.realpath(os.path.join(root, path))
        if target != root and not target.startswith(root + os.sep):
            return False
        return os.path.isfile(target)

    rep = assess_page(html_content, asset_exists=_exists,
                      page_relpath=os.path.relpath(page_file, root),
                      page_host=page_host, min_text_chars=min_text_chars)
    rep.details['page_file'] = page_file
    return rep


def _main(argv: list[str]) -> int:  # pragma: no cover — CLI convenience
    import argparse
    ap = argparse.ArgumentParser(description='Judge a mirrored doc page (#1196).')
    ap.add_argument('target', help='HTML file or http(s) URL')
    ap.add_argument('--host', default=None, help='upstream page host for same-host checks')
    ap.add_argument('--min-text', type=int, default=MIN_TEXT_CHARS)
    ap.add_argument('--strict', action='store_true',
                    help='exit 2 on a degraded (still served) page as well')
    ns = ap.parse_args(argv)
    if ns.target.startswith(('http://', 'https://')):
        import urllib.request
        req = urllib.request.Request(ns.target, headers={'User-Agent': 'razzfazz-help-quality/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — operator-supplied URL
            body = resp.read().decode('utf-8', errors='replace')
        host = ns.host or urlsplit(ns.target).netloc
        rep = assess_page(body, page_host=host, min_text_chars=ns.min_text)
    else:
        with open(ns.target, 'r', encoding='utf-8', errors='replace') as f:
            body = f.read()
        rep = assess_page(body, page_host=ns.host, min_text_chars=ns.min_text)
    print(rep.summary())
    for k in ('title', 'text_chars', 'severity', 'stylesheets',
              'missing_stylesheets', 'redirect_target'):
        if k in rep.details:
            print(f'  {k}: {rep.details[k]}')
    # A degraded page IS served on the box, so the CLI (day-1 / ad-hoc) must
    # not report it as an outage. `--strict` is the "warnings are errors" knob
    # for a day-1 tier that wants degraded red as well.
    if rep.fatal:
        return 1
    return 2 if (rep.degraded and ns.strict) else 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
