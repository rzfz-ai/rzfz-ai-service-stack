# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Mask credentials inside URLs before container output leaves the box (#2977).

``scheme://user:pass@host`` becomes ``scheme://***@host`` for any scheme
(redis, rediss, postgres, postgresql, amqp, https, ...). The userinfo runs up
to the first ``/`` or whitespace and may itself contain ``@``, so the LAST
``@`` of that run ends it: a password with an ``@`` in it is masked whole.

The same rule exists in shell as ``redact_url_credentials`` in scripts/lib.sh
(a sed), for the collectors that are bash. tests/unit/common/
test_2977_url_credentials_are_redacted.py runs both over one corpus.
"""
from __future__ import annotations

import re

_URL_CREDENTIALS = re.compile(r"""([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s'"<>]+@""")


def redact_url_credentials(text: str) -> str:
    """Return ``text`` with every ``scheme://userinfo@`` userinfo replaced by ``***``."""
    if not text or "@" not in text:
        return text
    return _URL_CREDENTIALS.sub(r"\1***@", text)
