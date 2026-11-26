# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Post-wget asset completion (#1196) — the generic "naked HTML" fix.

Measured while fixing the sweep: `wget --mirror --page-requisites` does NOT
fetch a page requisite that an `--exclude-directories` (-X) or
`--include-directories` (-I) rule filters out. Eight MkDocs-class mirrors
exclude `/assets` — where MkDocs keeps its stylesheets — and the two
Docusaurus sites whose entry sits deep under `/docs/…` need `-I /docs` to
crawl the whole tree, which then drops `/assets/css/styles.<hash>.css`.
Either way the pages arrive without their CSS and render as naked HTML
(komodo, gotenberg — and docling/stirling/… were one click away from the
same finding).

This pass runs after wget over every saved HTML page: it collects the
stylesheet, icon, font-preload and image references from the page —
absolute same-host, root-relative, relative, hashed — resolves each to
its path in the cache tree, fetches the ones that are missing, and pulls
the fonts/images a fetched stylesheet references in turn. References that
wget left as absolute upstream URLs (it only relativises links to files it
downloaded) are rewritten to root-relative paths, and root-relative
`url(/…)` inside fetched CSS to relative ones, so the viewer's existing
rewrite_root_paths / relative resolution serve them under
`/docs/<app_id>/…` without reaching the internet.

Scripts are deliberately NOT completed: the mirrored-doc CSP is
`script-src 'none'` (HLP-1), so fetching a site's JS bundles would only cost
disk. Budgets bound the pass (per-file and per-mirror bytes, asset and page
counts); the fetcher is injected so tests run it without a network.
"""
from __future__ import annotations

import os
import posixpath
import re
from urllib.parse import urlsplit

from mirror_quality import is_local_ref, resolve_local_ref

MAX_PAGES = 2000
MAX_ASSETS = 600
MAX_ASSET_BYTES = 8 * 1024 * 1024
BUDGET_BYTES = 128 * 1024 * 1024

_LINK_TAG_RE = re.compile(r'<link\b[^>]*>', re.IGNORECASE)
_IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_ATTR_RE = re.compile(
    r'''([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))''')
_CSS_URL_RE = re.compile(r'''url\(\s*(["']?)([^"')\s]+)\1\s*\)''', re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r'''@import\s+(?:url\()?\s*(["']?)([^"')\s;]+)\1''', re.IGNORECASE)


def _attrs(tag: str) -> dict[str, str]:
    out = {}
    for m in _ATTR_RE.finditer(tag):
        val = next((g for g in m.groups()[1:] if g is not None), '')
        out[m.group(1).lower()] = val
    return out


def page_asset_refs(html_content: str) -> list[str]:
    """Stylesheet / icon / font-preload / image references in a page."""
    refs: list[str] = []
    for tag in _LINK_TAG_RE.findall(html_content):
        a = _attrs(tag)
        rel = a.get('rel', '').lower().split()
        href = a.get('href', '').strip()
        if not href:
            continue
        if 'stylesheet' in rel or 'icon' in rel or 'apple-touch-icon' in rel:
            refs.append(href)
        elif 'preload' in rel and a.get('as', '').lower() in ('style', 'font', 'image'):
            refs.append(href)
    for tag in _IMG_TAG_RE.findall(html_content):
        src = _attrs(tag).get('src', '').strip()
        if src:
            refs.append(src)
    return refs


def _split_query(cache_path: str) -> tuple[str, str]:
    if '?' in cache_path:
        i = cache_path.index('?')
        return cache_path[:i], cache_path[i:]
    return cache_path, ''


def _remote_url(origin: str, cache_path: str) -> str:
    path, query = _split_query(cache_path)
    return f'{origin}/{path}{query}'


def _safe_join(root: str, cache_path: str) -> str | None:
    target = os.path.realpath(os.path.join(root, cache_path))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def _relativise_css_urls(css: str, css_cache_path: str, host: str) -> str:
    """Root-absolute / absolute-same-host url() refs → relative to the CSS
    file, so the browser resolves them under any served prefix."""
    css_dir = posixpath.dirname(_split_query(css_cache_path)[0])

    def _sub(m):
        quote, ref = m.group(1), m.group(2)
        if not is_local_ref(ref, host) or ref.lower().startswith('data:'):
            return m.group(0)
        if not (ref.startswith('/') or ref.lower().startswith(('http://', 'https://'))):
            return m.group(0)
        target = resolve_local_ref(ref, css_cache_path, host)
        if not target:
            return m.group(0)
        rel = posixpath.relpath(_split_query(target)[0], css_dir or '.') + _split_query(target)[1]
        return f'url({quote}{rel}{quote})'
    return _CSS_URL_RE.sub(_sub, css)


def complete_page_assets(cache_dir: str, mirror_url: str, fetch, *, log=print,
                         max_pages: int = MAX_PAGES, max_assets: int = MAX_ASSETS,
                         budget_bytes: int = BUDGET_BYTES) -> dict:
    """Fetch the page assets wget left out; rewrite the pages' absolute
    same-host asset URLs to root-relative ones. Returns a stats dict."""
    root = os.path.realpath(cache_dir)
    parts = urlsplit(mirror_url)
    host = parts.netloc
    origin = f'{parts.scheme}://{parts.netloc}'
    stats = {'pages': 0, 'fetched': 0, 'failed': 0, 'bytes': 0, 'rewritten_pages': 0,
             'css_refs': 0, 'budget_hit': False}
    seen: set[str] = set()

    def _have(cache_path: str) -> bool:
        target = _safe_join(root, cache_path)
        return bool(target) and os.path.isfile(target)

    def _fetch_into(cache_path: str) -> bool:
        """Fetch origin/<cache_path> into the tree. True if present after."""
        if cache_path in seen:
            return _have(cache_path)
        seen.add(cache_path)
        if _have(cache_path):
            return True
        if stats['fetched'] + stats['failed'] >= max_assets or stats['bytes'] >= budget_bytes:
            stats['budget_hit'] = True
            return False
        target = _safe_join(root, cache_path)
        if not target:
            return False
        url = _remote_url(origin, cache_path)
        r = fetch.get(url)
        if r.status != 200 or not r.body or len(r.body) > MAX_ASSET_BYTES:
            stats['failed'] += 1
            return False
        body = r.body
        # count THIS fetch before descending into a stylesheet's own refs, so
        # the asset cap bounds the whole recursion, not just the top level
        stats['fetched'] += 1
        stats['bytes'] += len(body)
        ctype = (getattr(r, 'content_type', '') or '').split(';', 1)[0].strip().lower()
        is_css = ctype == 'text/css' or _split_query(cache_path)[0].lower().endswith('.css')
        if is_css:
            text = body.decode('utf-8', errors='replace')
            # fonts / images the stylesheet references, one level deep
            for m in list(_CSS_URL_RE.finditer(text)) + list(_CSS_IMPORT_RE.finditer(text)):
                ref = m.group(2)
                if ref.lower().startswith('data:') or not is_local_ref(ref, host):
                    continue
                sub = resolve_local_ref(ref, cache_path, host)
                if sub:
                    stats['css_refs'] += 1
                    _fetch_into(sub)
            body = _relativise_css_urls(text, cache_path, host).encode('utf-8')
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as f:
            f.write(body)
        return True

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        for fn in sorted(filenames):
            if not fn.lower().endswith(('.html', '.htm')):
                continue
            if stats['pages'] >= max_pages:
                break
            full = os.path.join(dirpath, fn)
            page_rel = os.path.relpath(full, root).replace(os.sep, '/')
            try:
                with open(full, 'r', encoding='utf-8', errors='replace') as f:
                    html_content = f.read()
            except OSError:
                continue
            stats['pages'] += 1
            rewrites: dict[str, str] = {}
            for ref in page_asset_refs(html_content):
                if not is_local_ref(ref, host):
                    continue
                cache_path = resolve_local_ref(ref, page_rel, host)
                if not cache_path:
                    continue
                if _fetch_into(cache_path) and ref.lower().startswith(('http://', 'https://')):
                    # wget left it absolute because it had not downloaded it;
                    # now it is local → serve it from the tree, not the internet
                    rewrites[ref] = '/' + cache_path
            if rewrites:
                for old, new in rewrites.items():
                    html_content = html_content.replace(old, new)
                try:
                    with open(full, 'w', encoding='utf-8') as f:
                        f.write(html_content)
                    stats['rewritten_pages'] += 1
                except OSError:
                    pass
    log(f"[help-cache] asset completion for {host}: {stats['pages']} pages, "
        f"{stats['fetched']} assets fetched ({stats['bytes']} bytes), "
        f"{stats['failed']} failed, {stats['rewritten_pages']} pages rewritten"
        + (' — budget hit' if stats['budget_hit'] else ''))
    return stats
