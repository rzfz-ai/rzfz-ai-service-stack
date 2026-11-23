# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Network Policy blueprint — read-only per-container network policy inspector.

Super-admin gated via the app-wide before_request middleware (app/auth.py); no
per-route decorator. GET-only — mutation + the 4th `userdefined` mode are a later
release. Data from services/network_policy.collect() (live docker via socket-proxy)."""

from flask import Blueprint, current_app, render_template

from app.services import network_policy

network_policy_bp = Blueprint('network_policy', __name__, url_prefix='/network-policy')


@network_policy_bp.route('/')
def index():
    model = network_policy.collect(current_app.config['STACK_ROOT'])
    return render_template('network_policy/index.html', model=model)
