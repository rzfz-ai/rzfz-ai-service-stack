# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Settings → Knowledge access (#2473 D6): who can read each knowledge scope, and why.

A knowledge scope is an Authentik group with attributes.rzfz_kb_scope: true.
The role reconcile (`rzfz provision-sso-users --reconcile-roles`, on every
`rzfz post-install --refresh`) restricts the Dify and cognee datasets of each
scope to the scope's members plus each dataset's owner, and writes what it
found to the report this page reads (app/services/downstream_roles.py
`knowledge-report`). Read-only; the portal's own gate makes it admin-only.
"""
from __future__ import annotations

import json

from flask import Blueprint, render_template

from app.services.downstream_roles import knowledge_report_path

knowledge_access_bp = Blueprint('knowledge_access', __name__, url_prefix='/settings/knowledge-access')


def _load():
    """(report, error): report None when there is none yet or it is unreadable."""
    try:
        with open(knowledge_report_path(), encoding='utf-8') as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError):
        return None, 'The last report could not be read; it is rewritten by the next role reconcile.'
    if not isinstance(doc, dict) or not isinstance(doc.get('scopes'), dict):
        return None, 'The last report could not be read; it is rewritten by the next role reconcile.'
    return doc, None


@knowledge_access_bp.route('')
@knowledge_access_bp.route('/')
def index():
    report, error = _load()
    scopes = sorted((report or {}).get('scopes', {}).items())
    return render_template('settings/knowledge_access.html', report=report, scopes=scopes, error=error)
