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

#3367: ``redact_log_secrets`` is what the collectors call. It applies the URL
rule and masks the one secret-bearing line a stack service is known to print:
komodo-core's ``Api key created … key="<KOMODO_API_KEY>"`` (an upstream INFO
line with no switch of its own). Same rule in shell (``redact_log_secrets``)
and in both OTel export overlays; tests/unit/common/
test_3367_komodo_api_key_is_redacted_on_export.py runs all of them.
"""
from __future__ import annotations

import re

_URL_CREDENTIALS = re.compile(r"""([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s'"<>]+@""")


def redact_url_credentials(text: str) -> str:
    """Return ``text`` with every ``scheme://userinfo@`` userinfo replaced by ``***``."""
    if not text or "@" not in text:
        return text
    return _URL_CREDENTIALS.sub(r"\1***@", text)


# komodo-core: `Api key created … key="<id>"`. The field name may carry ANSI
# colour codes (tracing's italic name, dimmed `=`), the value may be unquoted,
# and a re-logged (JSON-escaped) line quotes it as `key=\"<id>\"`.
#
# #3436 L-24: on a line that carries the marker, EVERY `key=` field is masked,
# in one linear pass. The rule used to be one regex `marker[^\n]*…key=…` per
# line: the greedy run backtracked once per marker (quadratic — a 104 KB line
# took seconds), only the LAST key of a line was masked, and the escaped quote
# left the value whole behind a `***`. Now the marker only SELECTS the line;
# the field rule has no unbounded prefix, so each position is tried once.
_MARKER = "Api key created"
_ANSI_ONE = r"\x1b\[[0-9;]*m"
_KEY_FIELD = re.compile(
    r"((?:\s|" + _ANSI_ONE + r")key(?:" + _ANSI_ONE + r")*=(?:" + _ANSI_ONE + r')*(?:\\?")?)'
    r'[^"\\\s\x1b]+')


def _mask_line(line: str) -> str:
    return _KEY_FIELD.sub(r"\1***", line) if _MARKER in line else line


def redact_log_secrets(text: str) -> str:
    """``redact_url_credentials`` plus the known secret-bearing service lines."""
    text = redact_url_credentials(text)
    if not text or _MARKER not in text:
        return text
    return "\n".join(_mask_line(line) for line in text.split("\n"))
