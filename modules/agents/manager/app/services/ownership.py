# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2596 — the ONE ownership test for agent instances, uid first.

Every route that asks "does this caller own this instance?" goes through
:func:`owns`: the row's recorded ``user_id`` against the caller's authenticated
``X-Authentik-Uid``; the legacy slug only for a row with no ``user_id``, and
never when that slug is shared by more than one user (the request is then
refused with a message, see :func:`refusal_message`)."""
from __future__ import annotations

import logging

from flask import current_app, g, request
from razzfazz_common.user_slug import AmbiguousLegacyOwner, owner_verdict

logger = logging.getLogger(__name__)


def authenticated_uid() -> str:
    """`X-Authentik-Uid` only; never the username fallback."""
    return request.headers.get('X-Authentik-Uid', '').strip()


def owns(instance, username: str, uid: str | None = None) -> bool:
    if uid is None:
        uid = authenticated_uid()
    holders = getattr(current_app.db, 'slug_holder_uids', None)
    try:
        return owner_verdict(instance, username, uid, holders)
    except AmbiguousLegacyOwner as exc:
        logger.warning("Ownership refused for user=%r on instance %s: %s",
                       username, instance.get('id'), exc)
        g.ownership_refusal = str(exc)
        return False


def refusal_message() -> str | None:
    """The message of the last ambiguous-legacy refusal in this request."""
    return g.get('ownership_refusal')
