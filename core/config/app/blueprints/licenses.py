# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Licenses blueprint — component license overview, proxied from licenses container."""

import re

import requests
from flask import Blueprint, Response, render_template

licenses_bp = Blueprint('licenses', __name__, url_prefix='/licenses')

LICENSES_URL = 'http://razzfazz-licenses:5000'


# #2766: /branding/* (the favicon and brand assets) is served by Caddy's
# branding_static on EVERY host, the settings host included. Prefixing it with
# /licenses/ sent the favicon through the proxy below to the licences app, which
# has no /branding route: 404, and the page showed no favicon.
_ROOT_LINK = re.compile(r'(href|src)="/(?!branding/)')


def rewrite_links(html):
    """Point the licences page's root-relative links at this blueprint's proxy,
    except the shared brand assets."""
    return _ROOT_LINK.sub(r'\1="/licenses/', html)


@licenses_bp.route('/')
def index():
    """Proxy the licenses container index page."""
    try:
        resp = requests.get(LICENSES_URL, timeout=10)
        if resp.ok:
            return Response(rewrite_links(resp.text), content_type='text/html')
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
