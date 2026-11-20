# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Security blueprint — the box's security documentation in the settings portal.

Renders the security-architecture handover document and, since #2769, every
other security document the box carries: the release's security assessment,
the SBOM (CVE summary rendered; per-image CycloneDX files, the CVE baseline and
the manifest as downloads, the whole set as a ZIP) and the Enterprise security
how-tos — from the box-local Enterprise overlay when present, else from the
tracked tree (internal boxes). A document that describes another release than
the box runs is flagged (#2765: a ga.5 box showed the 2026.10-rc1 doc).
"""

import os
import re
import tempfile
import zipfile
from flask import Blueprint, current_app, render_template, abort, send_file

import bleach
import markdown

security_bp = Blueprint('security', __name__, url_prefix='/security')

DOC_RELATIVE = 'docs/security-architecture.md'
# #171: the tracked docs/security-architecture.md is STRIPPED from the public export
# (it is gated), so a customer/Codeberg box carries an absent-or-stale copy. The
# CURRENT gated doc is delivered box-locally via the Enterprise overlay
# (scripts/sync-enterprise-overlay.sh --from-payload, from a USB bake / offline
# package). Fall back to it when the tracked doc is missing OR empty, so those boxes
# render the current architecture without touching the git tree. Internal boxes keep
# rendering the tracked doc directly (primary wins). Matches the own_docs overlay
# resolver pattern in core/help.
DOC_OVERLAY_RELATIVE = 'overlay/enterprise/security-architecture.md'

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.S)

# #2564: Python-Markdown's fenced_code turns a ```mermaid fence into
# <pre><code class="language-mermaid">…</code></pre>. Rewrite it — AFTER bleach,
# so the sanitiser never has to allow a class on <pre> — into the
# <pre class="mermaid"> the vendored mermaid.min.js renders, the same element the
# Help service emits (core/help/cache_manager.py). The body is already
# HTML-escaped by markdown + bleach; mermaid reads textContent, which un-escapes it.
_MERMAID_FENCE_RE = re.compile(
    r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.S)


def _mermaid_fences_to_pre(html):
    return _MERMAID_FENCE_RE.sub(r'<pre class="mermaid">\1</pre>', html)

BLEACH_TAGS = bleach.ALLOWED_TAGS | {
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'pre', 'code', 'br', 'hr',
    'div', 'span', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'details', 'summary',
    'sup', 'sub', 'del', 'ins', 'img',
}
BLEACH_ATTRS = {
    **bleach.ALLOWED_ATTRIBUTES,
    'a': ['href', 'title', 'id'],
    'td': ['align'],
    'th': ['align'],
    'code': ['class'],
    'span': ['class'],
    'h1': ['id'], 'h2': ['id'], 'h3': ['id'], 'h4': ['id'], 'h5': ['id'], 'h6': ['id'],
    'img': ['src', 'alt', 'title'],
}


# #2769 — the other security documents. Overlay first (customer boxes), then
# the tracked tree (internal boxes). Fixed lists only: nothing user-supplied
# ever becomes a path.
ENTERPRISE_DOC_ROOTS = ('overlay/enterprise/docs', 'docs/enterprise')
SECURITY_DOCS = (
    ('security-model', 'Security model', 'explanation/security-model.md'),
    ('sso-login', 'Single sign-on', 'identity/sso-login.md'),
    ('mfa', 'Multi-factor authentication', 'identity/mfa.md'),
    ('app-access', 'App access and groups', 'identity/app-access.md'),
    ('authentik-users', 'User management (Authentik)', 'identity/authentik-users.md'),
    ('password-change', 'Changing passwords', 'how-to/password-change.md'),
    ('lost-admin-reset', 'Lost admin access', 'how-to/lost-admin-reset.md'),
    ('backup', 'Backup and encryption', 'how-to/backup.md'),
    ('cert-renewal', 'Certificates', 'how-to/cert-renewal.md'),
)
ASSESSMENT_DIRS = ('overlay/enterprise/security-run', 'security-run')
_ASSESSMENT_RE = re.compile(r'^razzfazz-ai-box-security-assessment-(v?\d{4}\.\d{2}[^/]*)\.md$')
SBOM_SUFFIXES = ('.json', '.tsv', '.md', '.txt')


def _stack_root():
    return current_app.config['STACK_ROOT']


def _norm_version(v):
    return (v or '').strip().strip('"').strip("'").lstrip('v')


def _box_version():
    """The release this box runs — the same order as the portal's footer
    (api._live_stack_version): RAZZFAZZ_VERSION in .env, then VERSION."""
    env_path = os.path.join(_stack_root(), '.env')
    if os.path.isfile(env_path):
        try:
            from app.services.env_utils import read_env_key
            v = read_env_key(env_path, 'RAZZFAZZ_VERSION')
            if v:
                return _norm_version(v)
        except Exception:
            pass
    path = os.path.join(_stack_root(), 'VERSION')
    if os.path.isfile(path):
        return _norm_version(open(path, encoding='utf-8').read())
    return ''


def _version_key(v):
    """Sort key for CalVer release names (2026.09-ga.5 > 2026.09-ga.4 > 2026.09-rc12)."""
    parts = re.split(r'[.\-]', _norm_version(v))
    return [(0, int(p), '') if p.isdigit() else (1, 0, p) for p in parts]


def _assessment():
    """(path, version) of the security assessment for this box's release — the
    exact release when present, else the newest release that has one."""
    root, box = _stack_root(), _box_version()
    found = {}
    for rel in ASSESSMENT_DIRS:
        d = os.path.join(root, rel)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            m = _ASSESSMENT_RE.match(name)
            if m and os.path.getsize(os.path.join(d, name)) > 0:
                found.setdefault(_norm_version(m.group(1)), os.path.join(d, name))
    if not found:
        return None, None
    if box in found:
        return found[box], box
    newest = max(found, key=_version_key)
    return found[newest], newest


def _sbom_dir():
    root, box = _stack_root(), _box_version()
    for rel in ('overlay/enterprise/sbom', f'releases/{box}/sbom', f'releases/v{box}/sbom'):
        d = os.path.join(root, rel)
        if os.path.isdir(d) and any(_sbom_files(d)):
            return d
    return None


def _sbom_files(d):
    """Downloadable SBOM files under d, relative, sorted (placeholders excluded)."""
    out = []
    for base, _dirs, files in os.walk(d):
        for f in files:
            if f.endswith(SBOM_SUFFIXES) and not f.startswith('TODO-'):
                out.append(os.path.relpath(os.path.join(base, f), d))
    return sorted(out)


def _enterprise_doc(slug):
    for s, title, rel in SECURITY_DOCS:
        if s != slug:
            continue
        for root_rel in ENTERPRISE_DOC_ROOTS:
            path = os.path.join(_stack_root(), root_rel, rel)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path, title
    return None, None


def _split_frontmatter(raw):
    meta, body = {}, raw
    m = _FRONTMATTER_RE.match(raw)
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                meta[k.strip()] = v.strip()
        body = raw[m.end():]
    return body, meta


def _render_markdown(body):
    html = markdown.markdown(body, extensions=['extra', 'tables', 'fenced_code', 'toc'])
    html = bleach.clean(html, tags=BLEACH_TAGS, attributes=BLEACH_ATTRS, strip=False)
    return _mermaid_fences_to_pre(html)


def _overview(doc_meta):
    """What the box carries, and which document describes another release."""
    box = _box_version()
    a_path, a_ver = _assessment()
    s_dir = _sbom_dir()
    s_files = _sbom_files(s_dir) if s_dir else []
    docs = [(slug, title) for slug, title, _rel in SECURITY_DOCS if _enterprise_doc(slug)[0]]
    mismatches = []
    doc_ver = _norm_version(doc_meta.get('stack_version', ''))
    if box and doc_ver and doc_ver != box:
        mismatches.append(f'The security architecture describes {doc_ver}; this box runs {box}.')
    if box and a_ver and a_ver != box:
        mismatches.append(f'The newest security assessment on this box is for {a_ver}; this box runs {box}.')
    return {
        'box_version': box,
        'assessment': {'version': a_ver} if a_path else None,
        'sbom': {'files': len(s_files), 'cyclonedx': sum(1 for f in s_files if f.endswith('.cdx.json'))} if s_files else None,
        'docs': docs,
        'enterprise_overlay': os.path.isdir(os.path.join(_stack_root(), 'overlay', 'enterprise')),
        'mismatches': mismatches,
    }


def _resolve_doc_path():
    """Return the security-architecture doc to render, or None.

    Prefer the tracked docs/security-architecture.md (internal boxes render it
    directly). Fall back to the box-local Enterprise overlay copy (#171) when the
    tracked doc is absent or empty — the path customer/Codeberg boxes take.
    """
    root = current_app.config['STACK_ROOT']
    for rel in (DOC_RELATIVE, DOC_OVERLAY_RELATIVE):
        path = os.path.join(root, rel)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def _read_document():
    path = _resolve_doc_path()
    if path is None:
        return None, {}
    return _split_frontmatter(open(path, encoding='utf-8').read())


@security_bp.route('/')
def index():
    body, meta = _read_document()
    overview = _overview(meta)
    if body is None and not (overview['assessment'] or overview['sbom'] or overview['docs']):
        abort(404, description=(
            "docs/security-architecture.md is missing. Run: "
            "python3 .claude/skills/security-documentation/scripts/generate-security-arch.py"
        ))
    return render_template(
        'security/index.html',
        doc_html=_render_markdown(body) if body is not None else None,
        doc_meta=meta,
        overview=overview,
    )


@security_bp.route('/assessment')
def assessment():
    path, version = _assessment()
    if path is None:
        abort(404, description='No security assessment is installed on this box.')
    body, meta = _split_frontmatter(open(path, encoding='utf-8').read())
    return render_template('security/page.html', page_title='Security assessment',
                           doc_html=_render_markdown(body), doc_meta={**meta, 'release': version},
                           overview=_overview({}))


@security_bp.route('/doc/<slug>')
def enterprise_doc(slug):
    path, title = _enterprise_doc(slug)
    if path is None:
        abort(404)
    body, meta = _split_frontmatter(open(path, encoding='utf-8').read())
    return render_template('security/page.html', page_title=title,
                           doc_html=_render_markdown(body), doc_meta=meta, overview=_overview({}))


@security_bp.route('/sbom')
def sbom():
    d = _sbom_dir()
    if d is None:
        abort(404, description='No SBOM is installed on this box.')
    files = _sbom_files(d)
    summary = os.path.join(d, 'cve-summary.md')
    html = None
    if os.path.isfile(summary):
        html = _render_markdown(_split_frontmatter(open(summary, encoding='utf-8').read())[0])
    return render_template('security/sbom.html', doc_html=html, doc_meta={},
                           files=[f for f in files if not f.endswith('.cdx.json')],
                           cyclonedx=[f for f in files if f.endswith('.cdx.json')],
                           overview=_overview({}))


@security_bp.route('/sbom/file/<path:rel>')
def sbom_file(rel):
    d = _sbom_dir()
    if d is None or rel not in _sbom_files(d):
        abort(404)
    return send_file(os.path.join(d, rel), as_attachment=True, download_name=os.path.basename(rel))


@security_bp.route('/sbom.zip')
def sbom_zip():
    d = _sbom_dir()
    if d is None:
        abort(404)
    tmp = tempfile.TemporaryFile()
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel in _sbom_files(d):
            z.write(os.path.join(d, rel), os.path.join('sbom', rel))
    tmp.seek(0)
    box = _box_version() or 'box'
    return send_file(tmp, mimetype='application/zip', as_attachment=True,
                     download_name=f'sbom-{box}.zip')
