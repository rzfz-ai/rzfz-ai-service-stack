# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Licenses blueprint — component license overview, proxied from licenses container."""

import requests
from flask import Blueprint, Response, render_template

licenses_bp = Blueprint('licenses', __name__, url_prefix='/licenses')

LICENSES_URL = 'http://razzfazz-licenses:5000'


@licenses_bp.route('/')
def index():
    """Proxy the licenses container index page."""
    try:
        resp = requests.get(LICENSES_URL, timeout=10)
        if resp.ok:
            html = resp.text
            html = html.replace('href="/', 'href="/licenses/')
            html = html.replace('src="/', 'src="/licenses/')
            return Response(html, content_type='text/html')
    except Exception:
        pass
    return render_template('docs/error.html', message='Licenses container is not running.')


@licenses_bp.route('/<path:path>')
def proxy(path):
    """Proxy static assets from licenses container."""
    try:
        resp = requests.get(f'{LICENSES_URL}/{path}', timeout=10)
        return Response(resp.content, status=resp.status_code,
                        content_type=resp.headers.get('Content-Type', 'application/octet-stream'))
    except Exception:
        return '', 404
