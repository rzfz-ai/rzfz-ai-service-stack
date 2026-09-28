# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""JSON Lines audit logger (M033 B1) — shared across razzfazz UI containers.

Structured audit entries written as JSON Lines to a rotating log file.
Promoted verbatim from core/config/app/services/audit_logger.py."""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

# #1312 re-review, finding 3: `target`, `detail` and `error` carry caller input
# verbatim. Several routes put the raw form field there on purpose — that is
# what makes a refused path traversal or a refused mail address readable — and
# a caller who pastes a bearer token into one of those fields wrote it into the
# audit log, which `logs.send_support` then mails off the box as part of a
# support snapshot. There was no redaction and no length cap before this: the
# test that claimed one only ever fed inputs that held no secret.
#
# The other free-text-ish field, `changes`, is written in exactly two places
# (`apply_manager.py`), both with `{'var': 'COMPOSE_PROFILES', 'new': …}` — a
# profile list, no credential — so it is left structured and unredacted.
_REDACTED = '[redacted]'
_FIELD_LIMIT = 512

_SECRET_PATTERNS = (
    # An Authorization header pasted into a form field.
    re.compile(r"(?i)\bbearer\s+[^\s'\"]+"),
    # Key shapes with a recognisable prefix: this platform's own `rzfz-sk-…`,
    # plus the common vendor ones an operator may hold on the same box.
    re.compile(r"(?i)\b(?:rzfz-sk|sk-proj|sk-ant|sk|ghp|gho|ghu|ghs|ghr|"
               r"xox[abprs]|AKIA|glpat)[-_][A-Za-z0-9_\-]{8,}"),
    # `password=…`, `API_KEY: …`, `client_secret="…"` and their relatives.
    re.compile(r"(?i)\b[A-Za-z0-9_]*(?:password|passwd|secret|token|api[-_]?key|"
               r"credential)[A-Za-z0-9_]*\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|\S+)"),
    # A long opaque run: base64 or hex blobs. Deliberately last, so the named
    # forms above win and produce the more readable replacement.
    re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}\b"),
)


def redact(value):
    """Mask credential-shaped substrings and cap the length of a free-text
    audit field. Returns non-strings unchanged."""
    if not isinstance(value, str):
        return value
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    if len(value) > _FIELD_LIMIT:
        value = value[:_FIELD_LIMIT] + f'… [truncated, {len(value)} chars]'
    return value


class AuditLogger:
    def __init__(self, log_path):
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        self._logger = logging.getLogger('razzfazz.audit')
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        # Re-enable defensively: `logging.config.dictConfig()`/`fileConfig()`
        # default to `disable_existing_loggers=True`, which sets `.disabled`
        # on every logger not named in the new config. Frameworks that
        # reconfigure logging at import (uvicorn, gunicorn, LiteLLM — pulled
        # in by the LLM-orchestrator module) therefore silently disable
        # `razzfazz.audit` process-wide, so `log()` drops every audit record.
        # A disabled logger is invisible to handler resets, so re-assert
        # enabled state on every construction. No-op in production (nothing
        # disables it there); load-bearing under a full test run / any process
        # that reconfigures logging after this logger already exists.
        self._logger.disabled = False

        # `razzfazz.audit` is a PROCESS-GLOBAL logger. A naive "install once
        # if there are no handlers" guard freezes the audit file at the FIRST
        # path used in the process: any later AuditLogger built with a
        # different path silently keeps writing to the first file. Harmless in
        # production (one container = one app = one path), but wrong for any
        # multi-instance process — notably the test suite, where it made the
        # api/razzfazz-config audit reads see an empty file while an earlier
        # test's file held the entries. Enforce "exactly one handler, bound to
        # THIS log_path": keep a single existing handler already targeting this
        # file, drop every other (stale/foreign/duplicate) handler, and install
        # a fresh one only when none matched.
        want = os.path.abspath(log_path)
        matched = False
        for h in list(self._logger.handlers):
            base = getattr(h, 'baseFilename', None)
            if base is not None and os.path.abspath(base) == want and not matched:
                matched = True
                continue
            self._logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        if not matched:
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
            # #1285: profile.disable only — did the containers actually go down
            # after the stop, or did autoheal bring one back? Structure:
            # {verified: bool|None, still_running: [service], re_stops: int,
            #  samples: int, watched: bool}. verified=None means compose could
            # not be asked, which is NOT the same as verified.
            'stop_verification': kwargs.get('stop_verification'),
            # #1191 rev-B: governance.snapshot only — the commit the tracked
            # governance files were hashed against and the 12-hex id of the
            # .env key-level MAC key, so the audit log alone tells whether
            # key-level tracking was active for a checksum set.
            'git_head': kwargs.get('git_head'),
            'env_key_id': kwargs.get('env_key_id'),
        }
        # Remove None values for cleaner output
        entry = {k: v for k, v in entry.items() if v is not None}
        for field in ('target', 'detail', 'error'):
            if field in entry:
                entry[field] = redact(entry[field])
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
