# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Offline / Network blueprint — read-only offline-readiness status (#184 P2).

Renders the box's network mode (online / proxied / offline) + registry mirror,
which overlays are composed, and the ``rzfz verify-images`` / ``rzfz
verify-models`` present/missing summaries. Super-admin gated via the app-wide
before_request middleware (app/auth.py) — no per-route decorator needed. No
mutations: it only reads ``.env`` and shells out to the two verify CLIs (which
themselves only inspect local images / the gpustack volume).
"""

from flask import Blueprint, current_app, render_template

from app.services import offline_status

offline_bp = Blueprint('offline', __name__, url_prefix='/offline')


@offline_bp.route('/')
def index():
    stack_root = current_app.config['STACK_ROOT']
    # collect() shells out to cli/verify-images.sh + cli/verify-models.sh (same
    # subprocess pattern the rest of the portal uses for docker work) and parses
    # their summary lines; it degrades to `unknown` if docker/exec is unavailable.
    status = offline_status.collect(stack_root)
    return render_template('offline/index.html', status=status)
