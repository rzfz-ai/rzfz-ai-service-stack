# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Runtime settings overrides (Phase-3 S5).

The env-derived :class:`app.config.Settings` is immutable and read fresh on
each ``get_settings()`` call, so the management UI can't change a knob without
a container restart. This module backs a tiny ``runtime_settings`` key/value
table with the ONE knob the operator flips live today — the billing-meter CAP
mode. The env value is the DEFAULT; a row here overrides it.

Import-safe off-box: DB access is lazy (``session_scope`` opens no connection
at import). Every reader is best-effort — a DB blip must never take the hot
path down, so :func:`effective_metering_mode` falls back to the env value on
any error.
"""
from __future__ import annotations

from app.config import _normalize_metering_mode, get_settings

METERING_MODE_KEY = "metering_mode"
# Only these keys may be set via the API (allow-list — never let the UI write
# arbitrary rows). Value validators keep a typo from disabling serving.
_ALLOWED = {METERING_MODE_KEY}


def get_override(key: str) -> str | None:
    """Return the stored override for ``key`` or None. Raises on DB error
    (callers that must not fail wrap this — see effective_metering_mode)."""
    from app.db import session_scope
    from app.models import RuntimeSetting

    with session_scope() as s:
        row = s.get(RuntimeSetting, key)
        return row.value if row is not None else None


def set_override(key: str, value: str) -> None:
    """Upsert an override. Rejects unknown keys (allow-list)."""
    if key not in _ALLOWED:
        raise ValueError(f"unknown runtime setting: {key}")
    from app.db import session_scope
    from app.models import RuntimeSetting

    with session_scope() as s:
        row = s.get(RuntimeSetting, key)
        if row is None:
            s.add(RuntimeSetting(key=key, value=value))
        else:
            row.value = value


def clear_override(key: str) -> bool:
    """Delete a stored override so the env value takes effect again (#358).

    Returns True if a row was removed, False if there was none — so a caller can
    distinguish "reverted" from "there was nothing to revert", and a repeated
    DELETE stays idempotent rather than erroring.

    Without this, `set_override` was a one-way door: once the operator flipped
    the metering mode in the UI, the `.env` value was permanently shadowed and
    the only way back was a hand-written DELETE against the database. An
    override you cannot remove is not an override, it is a migration.

    Allow-listed on the same set as `set_override` — a caller must not be able
    to delete arbitrary rows by naming them.
    """
    if key not in _ALLOWED:
        raise ValueError(f"unknown runtime setting: {key}")
    from app.db import session_scope
    from app.models import RuntimeSetting

    with session_scope() as s:
        row = s.get(RuntimeSetting, key)
        if row is None:
            return False
        s.delete(row)
        return True


def list_overrides() -> dict[str, str]:
    """All stored overrides as a plain dict (best-effort: {} on DB error)."""
    from app.db import session_scope
    from app.models import RuntimeSetting

    try:
        with session_scope() as s:
            return {r.key: r.value for r in s.query(RuntimeSetting).all()}
    except Exception:
        return {}


def effective_metering_mode() -> str:
    """The metering mode in effect right now: a valid ``runtime_settings``
    override wins, else the env default. NEVER raises — any DB error falls
    back to the env value so the hot path stays up during a datastore blip."""
    env_mode = get_settings().metering_mode
    try:
        override = get_override(METERING_MODE_KEY)
    except Exception:
        return env_mode
    if override is None:
        return env_mode
    return _normalize_metering_mode(override)
