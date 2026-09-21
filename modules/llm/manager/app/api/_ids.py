# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#329 — parse a caller-supplied UUID without turning a typo into a 500.

`uuid.UUID(worker_id)` on a path segment raises `ValueError`, which FastAPI does not
map to anything: it escapes as an unhandled **500**. A client that fat-fingers an id
therefore cannot tell "I sent a bad id" from "the manager is broken", the error lands
in the logs as a server fault, and any 5xx alerting fires on a client mistake.

Kept as a helper rather than re-typing the path params as `uuid.UUID`: that would make
FastAPI validate (good) but also change the in-body type from `str` to `UUID`, so every
`uuid.UUID(worker_id)` inside those handlers would become `uuid.UUID(UUID)` — a
TypeError, i.e. the same 500 by a different route. The helper converts 16 call sites
without touching a signature.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException


def parse_uuid(value, what: str = "id", *, status_code: int = 400) -> uuid.UUID:
    """Return ``value`` as a UUID, or raise HTTPException(400) naming the field.

    400 for path segments — the request line itself is malformed. Body fields pass
    ``status_code=422`` so they match what FastAPI would have produced had the field
    been typed `UUID` in the model, and clients that branch on 422-means-validation
    keep working.

    The message names the field but does NOT echo the value back, so a caller cannot
    use the error to reflect arbitrary content through the API.
    """
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=status_code,
                            detail=f"{what} is not a valid UUID") from None
