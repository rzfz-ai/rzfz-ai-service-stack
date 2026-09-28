# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Security blueprint — renders the security-architecture handover document."""

import os
import re
from flask import Blueprint, current_app, render_template, abort

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
    raw = open(path, encoding='utf-8').read()
    meta = {}
    m = _FRONTMATTER_RE.match(raw)
    body = raw
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                meta[k.strip()] = v.strip()
        body = raw[m.end():]
    return body, meta


@security_bp.route('/')
def index():
    body, meta = _read_document()
    if body is None:
        abort(404, description=(
            "docs/security-architecture.md is missing. Run: "
            "python3 .claude/skills/security-documentation/scripts/generate-security-arch.py"
        ))
    html = markdown.markdown(body, extensions=['extra', 'tables', 'fenced_code', 'toc'])
    html = bleach.clean(html, tags=BLEACH_TAGS, attributes=BLEACH_ATTRS, strip=False)
    return render_template(
        'security/index.html',
        doc_html=html,
        doc_meta=meta,
    )
