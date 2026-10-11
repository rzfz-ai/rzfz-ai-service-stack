# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Markdown-tree capture format (#1196).

Scraping a rendered Next.js/Mintlify app (dify, cognee, infisical,
openhands) yields a JS bundle as page text; scraping github.com yields
GitHub chrome instead of the Vaultwarden wiki; docs.paperless-ngx.com sits
behind a Cloudflare JS challenge. What all of those sources DO offer is the
documentation as markdown — `llms.txt` + `<page>.md` on every Mintlify site,
the wiki as a git repo, `docs/*.md` in the paperless repo.

So the Help Center captures the SOURCE and renders it through its own theme
at view time (the same pipeline the box's own docs use). One on-disk shape,
whichever producer built it:

    <cache>/<app_id>/
        _tree.json          index: title, nav, pages, home, counts
        _pages/<path>.md    cleaned markdown, links already localised
        _assets/<sha>.<ext> images, deduplicated by content hash

This module holds the PURE pieces — parsing, MDX cleanup, link/image
localisation, wiki syntax, tree I/O. The network/subprocess drivers that
feed it live in cache_manager (`_mirror_with_llms_txt`,
`_mirror_with_git_markdown`). Nothing here imports Flask or requests.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlsplit

TREE_FORMAT = 'markdown-tree'
TREE_INDEX = '_tree.json'
PAGES_DIR = '_pages'
ASSETS_DIR = '_assets'

_NON_LINK_SCHEMES = ('#', 'mailto:', 'tel:', 'data:', 'javascript:', 'blob:')


# ---------------------------------------------------------------------------
# llms.txt
# ---------------------------------------------------------------------------

@dataclass
class LlmsEntry:
    title: str
    url: str
    description: str = ''

    @property
    def is_subindex(self) -> bool:
        """Dify nests: llms.txt → _llms/en/cloud.md (265 pages) → pages.
        A sub-index is a link into `/_llms/` or one whose title carries the
        `(N pages)` marker Mintlify stamps on aggregate files."""
        if '/_llms/' in (urlsplit(self.url).path or ''):
            return True
        return bool(re.search(r'\(\d+\s+pages?\)', self.title))


@dataclass
class LlmsSection:
    title: str
    level: int
    entries: list[LlmsEntry] = field(default_factory=list)


@dataclass
class LlmsIndex:
    title: str
    description: str
    sections: list[LlmsSection] = field(default_factory=list)


_LLMS_ITEM_RE = re.compile(
    r'^\s*(?:[-*+]|\d+[.)])\s+\[([^\]]*)\]\(([^)\s]+)\)\s*:?\s*(.*?)\s*$')
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


def _is_page_url(url: str) -> bool:
    path = urlsplit(url).path or ''
    last = path.rstrip('/').rsplit('/', 1)[-1]
    if not last:
        return True
    if '.' not in last:
        return True
    return last.lower().endswith('.md')


def parse_llms_index(text: str, base_url: str) -> LlmsIndex:
    """Parse an llms.txt / llms sub-index into titled sections of page links.

    Non-page links (OpenAPI .json, images) are dropped; relative links are
    absolutised against `base_url`. Sections are the markdown headings in
    order, with their level kept for the TOC; a leading unnamed section is
    emitted only when it actually holds entries."""
    title, description = '', ''
    sections: list[LlmsSection] = []
    current = LlmsSection('', 1)
    seen_title = False
    desc_lines: list[str] = []
    in_desc = False

    for raw in (text or '').splitlines():
        line = raw.rstrip()
        h = _HEADING_RE.match(line)
        if h:
            level = len(h.group(1))
            if level == 1 and not seen_title:
                title = h.group(2).strip()
                seen_title = True
                continue
            if current.entries or current.title:
                sections.append(current)
            current = LlmsSection(h.group(2).strip(), level)
            in_desc = False
            continue
        m = _LLMS_ITEM_RE.match(line)
        if m:
            in_desc = False
            url = urljoin(base_url, m.group(2).strip())
            if not _is_page_url(url):
                continue
            current.entries.append(LlmsEntry(m.group(1).strip(), url, m.group(3).strip()))
            continue
        if line.startswith('>') and not sections and not current.entries and not description:
            desc_lines.append(line.lstrip('> ').strip())
            in_desc = True
            continue
        if in_desc and not line.strip():
            description = ' '.join(x for x in desc_lines if x).strip()
            in_desc = False
    if in_desc and not description:
        description = ' '.join(x for x in desc_lines if x).strip()
    if current.entries or current.title:
        sections.append(current)
    return LlmsIndex(title=title, description=description, sections=sections)


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------

def _scope_parts(scope_url: str) -> tuple[str, str]:
    """(origin, path-prefix-without-trailing-slash) of a mirror scope URL."""
    p = urlsplit(scope_url)
    origin = f'{p.scheme}://{p.netloc}'
    prefix = (p.path or '').rstrip('/')
    return origin, prefix


def _strip_page_suffix(path: str) -> str:
    path = path.strip('/')
    for suffix in ('/index.html', '/index.md', '.html', '.htm', '.md'):
        if path.lower().endswith(suffix):
            path = path[:-len(suffix)]
            break
    return path.strip('/')


def page_path_for(url: str, scope_url: str) -> str | None:
    """Scope-relative page path for `url`, or None when it is outside the
    mirrored scope (other host, or a path outside the scope prefix)."""
    origin, prefix = _scope_parts(scope_url)
    p = urlsplit(url)
    if f'{p.scheme}://{p.netloc}'.lower() != origin.lower():
        return None
    path = p.path or '/'
    if prefix and not (path == prefix or path.startswith(prefix + '/')):
        return None
    rel = path[len(prefix):] if prefix else path
    rel = _strip_page_suffix(rel)
    return rel or 'index'


def is_excluded(page_path: str, excludes) -> bool:
    rel = '/' + page_path.strip('/')
    for ex in excludes or []:
        ex = (ex or '').rstrip('/')
        if not ex:
            continue
        if not ex.startswith('/'):
            ex = '/' + ex
        if rel == ex or rel.startswith(ex + '/'):
            return True
    return False


# ---------------------------------------------------------------------------
# Fenced-code protection (shared by the cleanup passes)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r'^(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=fence)[ \t]*$',
                       re.MULTILINE | re.DOTALL)


def _protect_fences(md: str) -> tuple[str, list[str]]:
    stash: list[str] = []

    def _stash(m):
        stash.append(m.group(0))
        return f'\x00FENCE{len(stash) - 1}\x00'

    return _FENCE_RE.sub(_stash, md), stash


def _restore_fences(md: str, stash: list[str]) -> str:
    for i, block in enumerate(stash):
        md = md.replace(f'\x00FENCE{i}\x00', block)
    return md


_FRONTMATTER_RE = re.compile(r'\A\s*---[ \t]*\n.*?\n---[ \t]*\n', re.DOTALL)


def _strip_frontmatter(md: str) -> str:
    return _FRONTMATTER_RE.sub('', md, count=1)


# ---------------------------------------------------------------------------
# Mintlify MDX → markdown
# ---------------------------------------------------------------------------

_ATTR_RE = re.compile(
    r'''([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|\{([^}]*)\}|([^\s"'>]+))''')


def _jsx_attrs(tag: str) -> dict[str, str]:
    out = {}
    for m in _ATTR_RE.finditer(tag):
        val = next((g for g in m.groups()[1:] if g is not None), '')
        out[m.group(1)] = val.strip()
    return out


_CALLOUT_KINDS = ('Note', 'Tip', 'Info', 'Warning', 'Check', 'Danger', 'Caution')
_PREAMBLE_RE = re.compile(r'\A\s*>\s*#+\s*Documentation Index\s*\n(?:>[^\n]*\n?)*\s*', re.IGNORECASE)


def _paired(tag: str):
    return re.compile(rf'<{tag}\b([^>]*)>(.*?)</{tag}\s*>', re.DOTALL)


def _selfclosing(tag: str):
    return re.compile(rf'<{tag}\b([^>]*?)/>', re.DOTALL)


def _img_to_md(m) -> str:
    a = _jsx_attrs(m.group(1))
    cls = a.get('className', a.get('class', ''))
    if 'dark:block' in cls:
        return ''  # the dark-mode twin of the image right above it
    src = a.get('src', '')
    if not src:
        return ''
    return f'![{a.get("alt", "")}]({src})'


def _callout(kind: str, body: str) -> str:
    return (f'\n<div class="callout callout-{kind.lower()}" markdown="1">\n'
            f'**{kind}**\n\n{body.strip()}\n\n</div>\n')


def mintlify_to_markdown(md: str) -> str:
    """Turn a Mintlify page's `.md` (MDX with JSX components) into plain
    markdown our renderer (python-markdown + bleach) can show.

    Components are mapped to the closest plain structure — callouts to the
    theme's `.callout` box, Cards to linked callouts, Steps/Tabs to bold
    headings, Accordions to <details>, iframes to links; unknown components
    are unwrapped (their content kept), never left as raw tags. Fenced code
    is protected throughout so JSX in examples survives verbatim."""
    md = md or ''
    md, fences = _protect_fences(md)
    md = _strip_frontmatter(md)
    md = _PREAMBLE_RE.sub('', md, count=1)

    # MDX-only syntax
    md = re.sub(r'\{/\*.*?\*/\}', '', md, flags=re.DOTALL)
    md = re.sub(r'^import\s[^\n]*$', '', md, flags=re.MULTILINE)
    md = re.sub(r'^export\s[^\n]*$', '', md, flags=re.MULTILINE)
    md = re.sub(r'\{["\']\s*["\']\}', ' ', md)

    # images (JSX or HTML form), dropping dark-mode twins
    md = re.sub(r'<img\b([^>]*?)/?>(?:\s*</img>)?', _img_to_md, md, flags=re.DOTALL)

    # iframes → links
    def _iframe(m):
        a = _jsx_attrs(m.group(1))
        src = a.get('src', '')
        return f'[{a.get("title") or "Embedded video"}]({src})' if src else ''
    md = re.sub(r'<iframe\b([^>]*)>.*?</iframe\s*>', _iframe, md, flags=re.DOTALL)
    md = re.sub(r'<iframe\b([^>]*?)/>', _iframe, md, flags=re.DOTALL)

    # Cards → linked callouts (inner first, so CardGroup unwraps cleanly)
    def _card(m):
        a = _jsx_attrs(m.group(1))
        title, href = a.get('title', ''), a.get('href', '')
        head = f'**[{title}]({href})**' if href and title else (f'**{title}**' if title else '')
        body = (m.group(2) if m.lastindex and m.lastindex >= 2 else '') or ''
        return f'\n<div class="callout" markdown="1">\n{head}\n\n{body.strip()}\n\n</div>\n'
    md = _paired('Card').sub(_card, md)
    md = _selfclosing('Card').sub(_card, md)

    # Steps / Tabs / Updates / fields → bold headings
    def _titled(m):
        a = _jsx_attrs(m.group(1))
        label = a.get('title') or a.get('label') or a.get('name') or ''
        typ = a.get('type', '')
        head = f'**{label}**' if label else ''
        if typ and label:
            head += f' ({typ})'
        return f'\n{head}\n\n{m.group(2).strip()}\n'
    for tag in ('Step', 'Tab', 'Update', 'ResponseField', 'ParamField'):
        md = _paired(tag).sub(_titled, md)

    # Accordions / Expandables → details
    def _details(m):
        a = _jsx_attrs(m.group(1))
        return (f'\n<details markdown="1">\n<summary>{a.get("title", "Details")}</summary>\n\n'
                f'{m.group(2).strip()}\n\n</details>\n')
    for tag in ('Accordion', 'Expandable'):
        md = _paired(tag).sub(_details, md)

    # Callouts
    for kind in _CALLOUT_KINDS:
        md = _paired(kind).sub(lambda m, k=kind: _callout(k, m.group(2)), md)

    # Anything else Capitalised is a JSX component: unwrap it.
    md = re.sub(r'<[A-Z][A-Za-z0-9]*(?:\s[^<>]*?)?/>', '', md, flags=re.DOTALL)
    md = re.sub(r'</?[A-Z][A-Za-z0-9]*(?:\s[^<>]*?)?>', '', md, flags=re.DOTALL)

    md = re.sub(r'\n{3,}', '\n\n', md)
    return _restore_fences(md, fences).strip('\n') + '\n'


# ---------------------------------------------------------------------------
# Docs-directory (MkDocs sources) cleanup
# ---------------------------------------------------------------------------

def docs_dir_to_markdown(md: str) -> str:
    """MkDocs-source cleanup: drop frontmatter and the `#only-dark` image
    twins, strip `#only-light` markers so the light image renders once."""
    md, fences = _protect_fences(md or '')
    md = _strip_frontmatter(md)
    lines = []
    for line in md.splitlines():
        if re.search(r'!\[[^\]]*\]\([^)]*#only-dark\)', line):
            continue
        lines.append(re.sub(r'(!\[[^\]]*\]\([^)#]*)#only-light\)', r'\1)', line))
    md = '\n'.join(lines)
    return _restore_fences(md, fences).strip('\n') + '\n'


def _title_from_frontmatter(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ''
    t = re.search(r'^title:\s*(.+?)\s*$', m.group(0), re.MULTILINE)
    return t.group(1).strip().strip('"\'') if t else ''


def _title_from_h1(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('# '):
            return s[2:].strip()
    return ''


def docs_dir_title(md_path: str) -> str:
    """frontmatter `title:` → first H1 → humanised filename."""
    try:
        with open(md_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read(4000)
    except OSError:
        text = ''
    return (_title_from_frontmatter(text) or _title_from_h1(text)
            or _humanise(os.path.splitext(os.path.basename(md_path))[0]))


def _humanise(stem: str) -> str:
    words = stem.replace('_', ' ').replace('-', ' ').strip()
    return (words[:1].upper() + words[1:]) if words else stem


# ---------------------------------------------------------------------------
# GitHub wiki flavour
# ---------------------------------------------------------------------------

_WIKI_LINK_RE = re.compile(r'\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]')


def _wiki_page_name(target: str) -> str:
    return target.strip().replace(' ', '-')


def wiki_links_to_markdown(md: str, pages, app_id: str) -> str:
    """`[[Title|Page-Name]]` / `[[Page Name]]` → local route when the page
    was captured, plain text otherwise (a wiki link to a missing page)."""
    lower = {p.lower(): p for p in pages}

    def _sub(m):
        title = m.group(1).strip()
        target = _wiki_page_name(m.group(2) if m.group(2) else m.group(1))
        real = lower.get(target.lower())
        if real is None:
            return title
        return f'[{title}](/docs/{app_id}/{real}/)'

    md, fences = _protect_fences(md or '')
    md = _WIKI_LINK_RE.sub(_sub, md)
    return _restore_fences(md, fences)


def wiki_title_from_filename(filename: str) -> str:
    stem = filename[:-3] if filename.lower().endswith('.md') else filename
    return stem.replace('-', ' ').strip()


def parse_wiki_sidebar(sidebar: str, pages) -> list[dict]:
    """`_Sidebar.md` → nav sections `[{title, level, pages:[{path,title}]}]`,
    keeping only pages that exist in the tree."""
    lower = {p.lower(): p for p in pages}
    nav: list[dict] = []
    current = {'title': '', 'level': 1, 'pages': []}
    for raw in (sidebar or '').splitlines():
        line = raw.rstrip()
        h = _HEADING_RE.match(line)
        if h:
            if current['pages']:
                nav.append(current)
            current = {'title': h.group(2).strip(), 'level': len(h.group(1)), 'pages': []}
            continue
        for m in _WIKI_LINK_RE.finditer(line):
            title = m.group(1).strip()
            target = _wiki_page_name(m.group(2) if m.group(2) else m.group(1))
            real = lower.get(target.lower())
            if real is not None:
                current['pages'].append({'path': real, 'title': title})
        for m in re.finditer(r'\[([^\]]+)\]\(([^)\s]+)\)', line):
            target = _strip_page_suffix(m.group(2).split('#', 1)[0].rsplit('/', 1)[-1])
            real = lower.get(target.lower())
            if real is not None and not any(p['path'] == real for p in current['pages']):
                current['pages'].append({'path': real, 'title': m.group(1).strip()})
    if current['pages']:
        nav.append(current)
    return nav


# ---------------------------------------------------------------------------
# Link + image localisation
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r'(?<!!)\[([^\]]*)\]\(([^)\s]+)((?:\s+"[^"]*")?)\)')
_MD_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)((?:\s+"[^"]*")?)\)')
_HTML_HREF_RE = re.compile(r'(<a\b[^>]*?\bhref=")([^"]*)(")', re.IGNORECASE)
_HTML_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]*)(")', re.IGNORECASE)


def _split_frag(href: str) -> tuple[str, str]:
    for sep in ('#', '?'):
        if sep in href:
            i = href.index(sep)
            return href[:i], href[i:]
    return href, ''


def _resolve_page_target(href: str, page_path: str, pages, scope_url: str) -> tuple[str | None, str]:
    """(captured page path or None, upstream absolute URL) for a link."""
    origin, prefix = _scope_parts(scope_url)
    base, _frag = _split_frag(href)
    if base.lower().startswith(('http://', 'https://')):
        candidate = page_path_for(base, scope_url)
        return (candidate if candidate in pages else None), base
    if base.startswith('/'):
        candidate = page_path_for(origin + base, scope_url)
        if candidate in pages:
            return candidate, origin + base
        alt = _strip_page_suffix(base) or 'index'
        if alt in pages:
            return alt, origin + base
        return None, origin + base
    # relative to the page's directory
    base_dir = posixpath.dirname(page_path.strip('/')) if page_path else ''
    joined = posixpath.normpath(posixpath.join(base_dir, base)) if base else page_path
    if joined.startswith('..'):
        return None, urljoin(scope_url, base)
    candidate = _strip_page_suffix(joined) or 'index'
    upstream = origin + prefix + '/' + candidate
    return (candidate if candidate in pages else None), upstream


def localise_links(md: str, *, page_path: str, pages, app_id: str, scope_url: str) -> str:
    """Rewrite page links: captured pages → `/docs/<app>/<path>/`, anything
    else in the scope → absolute upstream URL (never a dangling relative
    href on our origin). Anchors, mailto:, data: and images are untouched."""
    pages = set(pages)

    local_prefix = f'/docs/{app_id}/'

    def _map(href: str) -> str:
        if not href or href.lower().startswith(_NON_LINK_SCHEMES):
            return href
        if href.startswith(local_prefix):
            return href  # already a route of ours (wiki links, a re-run)
        base, frag = _split_frag(href)
        local, upstream = _resolve_page_target(href, page_path, pages, scope_url)
        if local is not None:
            return f'/docs/{app_id}/{local}/{frag}'
        if base.lower().startswith(('http://', 'https://')):
            return href
        if frag and not upstream.endswith(frag):
            return upstream + frag
        return upstream

    md, fences = _protect_fences(md or '')
    md = _MD_LINK_RE.sub(lambda m: f'[{m.group(1)}]({_map(m.group(2))}{m.group(3)})', md)
    md = _HTML_HREF_RE.sub(lambda m: f'{m.group(1)}{_map(m.group(2))}{m.group(3)}', md)
    return _restore_fences(md, fences)


def localise_images(md: str, *, page_path: str, scope_url: str, store) -> str:
    """Resolve every image to an absolute upstream URL, hand it to
    `store(url) -> local route | None`, and rewrite the src to the route
    (or the absolute URL when the store declined)."""
    origin, prefix = _scope_parts(scope_url)

    def _absolute(src: str) -> str:
        if src.lower().startswith(('http://', 'https://')):
            return src
        if src.startswith('//'):
            return 'https:' + src
        if src.startswith('/'):
            return origin + src
        base_dir = posixpath.dirname(page_path.strip('/')) if page_path else ''
        joined = posixpath.normpath(posixpath.join(base_dir, src))
        if joined.startswith('..'):
            return urljoin(scope_url, src)
        return origin + prefix + '/' + joined

    def _map(src: str) -> str:
        if not src or src.lower().startswith(('data:', 'blob:')):
            return src
        url = _absolute(src)
        route = store(url)
        return route or url

    md, fences = _protect_fences(md or '')
    md = _MD_IMAGE_RE.sub(lambda m: f'![{m.group(1)}]({_map(m.group(2))}{m.group(3)})', md)
    md = _HTML_SRC_RE.sub(lambda m: f'{m.group(1)}{_map(m.group(2))}{m.group(3)}', md)
    return _restore_fences(md, fences)


# ---------------------------------------------------------------------------
# Tree I/O
# ---------------------------------------------------------------------------

_SAFE_SEGMENT_RE = re.compile(r'^[^/\\\x00]+$')


def _validate_page_path(path: str) -> str:
    path = (path or '').strip().strip('/')
    if not path:
        raise ValueError('empty page path')
    for seg in path.split('/'):
        if seg in ('.', '..') or not _SAFE_SEGMENT_RE.match(seg):
            raise ValueError(f'unsafe page path segment {seg!r}')
    return path


class TreeWriter:
    """Builds `<cache_dir>/{_tree.json,_pages/**,_assets/*}`."""

    def __init__(self, cache_dir: str, app_id: str):
        self.cache_dir = cache_dir
        self.app_id = app_id
        self.pages: dict[str, dict] = {}
        self._assets: dict[str, str] = {}
        self.asset_bytes = 0
        os.makedirs(os.path.join(cache_dir, PAGES_DIR), exist_ok=True)
        os.makedirs(os.path.join(cache_dir, ASSETS_DIR), exist_ok=True)

    @property
    def asset_count(self) -> int:
        return len(self._assets)

    def add_page(self, path: str, title: str, markdown_text: str) -> None:
        path = _validate_page_path(path)
        file_rel = os.path.join(PAGES_DIR, *path.split('/')) + '.md'
        full = os.path.join(self.cache_dir, file_rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as f:
            f.write(markdown_text)
        self.pages[path] = {'title': title or path, 'file': file_rel.replace(os.sep, '/')}

    def add_asset(self, data: bytes, ext: str) -> str:
        ext = re.sub(r'[^a-z0-9]', '', (ext or 'bin').lower())[:8] or 'bin'
        digest = hashlib.sha256(data).hexdigest()[:16]
        name = f'{digest}.{ext}'
        if name not in self._assets:
            with open(os.path.join(self.cache_dir, ASSETS_DIR, name), 'wb') as f:
                f.write(data)
            self._assets[name] = name
            self.asset_bytes += len(data)
        return f'/docs/{self.app_id}/{ASSETS_DIR}/{name}'

    def finish(self, *, title: str, description: str, source: str, nav: list[dict],
               home: str, scope_prefix: str = '', extra: dict | None = None) -> dict:
        tree = {
            'format': TREE_FORMAT,
            'app_id': self.app_id,
            'title': title,
            'description': description,
            'source': source,
            'captured_at': datetime.utcnow().isoformat() + 'Z',
            'home': home,
            'scope_prefix': scope_prefix.strip('/'),
            'nav': nav,
            'pages': self.pages,
            'page_count': len(self.pages),
            'asset_count': self.asset_count,
            'asset_bytes': self.asset_bytes,
        }
        if extra:
            tree.update(extra)
        tmp = os.path.join(self.cache_dir, TREE_INDEX + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(tree, f, indent=1)
        os.replace(tmp, os.path.join(self.cache_dir, TREE_INDEX))
        return tree


def read_tree(cache_dir: str) -> dict | None:
    path = os.path.join(cache_dir, TREE_INDEX)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            tree = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(tree, dict) or tree.get('format') != TREE_FORMAT:
        return None
    if not isinstance(tree.get('pages'), dict):
        return None
    return tree


def lookup_page(tree: dict, request_path: str | None) -> str | None:
    """Map a `/docs/<app>/<request_path>` to a page key, tolerating the
    `.html`/`.md`/`index.html` spellings a browser or the link rewriter
    (#1072) may produce, and the mirror's own path prefix."""
    pages = tree.get('pages', {})
    path = _strip_page_suffix(request_path or '')
    if not path:
        return tree.get('home') or ('index' if 'index' in pages else None)
    if path in pages:
        return path
    prefix = (tree.get('scope_prefix') or '').strip('/')
    if prefix and (path == prefix or path.startswith(prefix + '/')):
        stripped = path[len(prefix):].strip('/')
        if not stripped:
            return tree.get('home')
        if stripped in pages:
            return stripped
    return None


def read_page_markdown(cache_dir: str, tree: dict, page: str) -> str | None:
    info = tree.get('pages', {}).get(page)
    if not info:
        return None
    root = os.path.realpath(cache_dir)
    full = os.path.realpath(os.path.join(root, info.get('file', '')))
    if full != root and not full.startswith(root + os.sep):
        return None
    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def nav_neighbours(tree: dict, page: str) -> tuple[dict | None, dict | None]:
    """(prev, next) page dicts in nav order, for the page footer."""
    order: list[str] = []
    for section in tree.get('nav', []):
        for p in section.get('pages', []):
            if p.get('path') in tree.get('pages', {}) and p['path'] not in order:
                order.append(p['path'])
    for p in tree.get('pages', {}):
        if p not in order:
            order.append(p)
    if page not in order:
        return None, None
    i = order.index(page)
    pages = tree['pages']

    def _entry(k):
        return {'path': k, 'title': pages[k].get('title', k)}
    return (_entry(order[i - 1]) if i > 0 else None,
            _entry(order[i + 1]) if i + 1 < len(order) else None)
