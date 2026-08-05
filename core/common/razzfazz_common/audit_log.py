# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""JSON Lines audit logger (M033 B1) — shared across razzfazz UI containers.

Structured audit entries written as JSON Lines to a rotating log file.
Promoted verbatim from core/config/app/services/audit_logger.py."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler


class AuditLogger:
    def __init__(self, log_path):
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        self._logger = logging.getLogger('razzfazz.audit')
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

        if not self._logger.handlers:
            handler = TimedRotatingFileHandler(
                log_path, when='D', interval=1, backupCount=90
            )
            handler.setFormatter(logging.Formatter('%(message)s'))
            self._logger.addHandler(handler)

    def log(self, event, **kwargs):
        entry = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': kwargs.get('level', 'INFO'),
            'event': event,
            'user': kwargs.get('user'),
            'source_ip': kwargs.get('source_ip'),
            'auth_method': kwargs.get('auth_method'),
            'category': kwargs.get('category'),
            'action': kwargs.get('action'),
            'target': kwargs.get('target'),
            'detail': kwargs.get('detail'),
            'risk': kwargs.get('risk'),
            'changes': kwargs.get('changes'),
            'containers_affected': kwargs.get('containers_affected'),
            'outcome': kwargs.get('outcome', 'success'),
            'duration_ms': kwargs.get('duration_ms'),
            'error': kwargs.get('error'),
            # BSB-16: post-toggle Tier-D probe outcome (only present for
            # profile.enable events when the probe ran). Structure:
            # {overall_pass: bool|None, skipped_reason: str|None,
            #  targets: [{target, pass, duration_ms}]}
            'post_toggle_probe': kwargs.get('post_toggle_probe'),
        }
        # Remove None values for cleaner output
        entry = {k: v for k, v in entry.items() if v is not None}
        self._logger.info(json.dumps(entry, default=str))

    def log_auth(self, user, source_ip, success):
        self.log(
            'auth.login' if success else 'auth.login_failed',
            user=user,
            source_ip=source_ip,
            auth_method='authentik+password',
            category='auth',
            action='login' if success else 'login_failed',
            outcome='success' if success else 'failure',
        )
