# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Auth middleware (M1) — ``rzfz-sk-…`` bearer → key_hash → Valkey cache →
Postgres fallback → request context.

The core ``authenticate()`` is DEPENDENCY-INJECTED (cache + db-lookup
callable) so it unit-tests without redis/postgres. The manager owns keys +
metering; the key is never forwarded to LiteLLM.

Hashing: SHA-256 of the plaintext key. The DB stores the raw digest bytes
(``api_keys.key_hash``); the cache is keyed by the hex digest.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Callable, Optional

# Number of random bytes in a freshly-minted key body.
_KEY_ENTROPY_BYTES = 32
# How many chars of the plaintext to keep as a human-facing prefix label.
_DISPLAY_PREFIX_LEN = 12


class AuthError(Exception):
    """Raised on any authentication/authorisation failure.

    ``status_code`` is 401 (bad/unknown/malformed credential) or 403
    (known key but disabled/revoked/expired).
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class KeyRecord:
    """Authenticated request context for a single API key."""

    key_id: str
    cost_center_id: str
    allowed_models: list[str]
    rpm_limit: Optional[int]
    tpm_limit: Optional[int]
    max_budget_tokens: Optional[int]
    status: str
    expires_at: Optional[str]  # ISO-8601 or None
    # Window (seconds) the token budget resets over; None = all-time cap.
    # Defaulted so older cached blobs (pre-P2-E1) still deserialize.
    budget_duration_seconds: Optional[int] = None
    # EXO-3: the key's cost-centre NAME (``cost_centers.name``), carried so a
    # route can tell an INTERNAL stack service key from a tenant/end-user key
    # without a second DB read on the hot path. Cost-centre names are already
    # the stack's service-key convention (``cli/post-install.sh`` mints the
    # OWUI/Dify keys under ``stack/openwebui`` / ``stack/dify``, team
    # ``stack``), so this reads an EXISTING column — no schema migration.
    # Defaulted to None so a cached blob written before this field still
    # deserializes; the effect of an old blob is that the key reads as
    # non-service until its cache entry expires, i.e. it fails CLOSED.
    cost_center_name: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "KeyRecord":
        return cls(**json.loads(blob))


# --- key material helpers ----------------------------------------------------
def hash_key(plaintext: str) -> bytes:
    """SHA-256 digest bytes of a plaintext key (stored in api_keys.key_hash)."""
    return hashlib.sha256(plaintext.encode("utf-8")).digest()


def cache_key_for(key_hash: bytes) -> str:
    return "authkey:" + key_hash.hex()


def generate_api_key(prefix: str) -> tuple[str, bytes, str]:
    """Mint a new key. Returns (plaintext, key_hash_bytes, display_prefix).

    The plaintext is shown to the operator ONCE; only the hash is stored.
    """
    body = secrets.token_urlsafe(_KEY_ENTROPY_BYTES)
    plaintext = f"{prefix}{body}"
    return plaintext, hash_key(plaintext), plaintext[:_DISPLAY_PREFIX_LEN]


def extract_bearer(authorization_header: Optional[str]) -> str:
    """Pull the raw token out of an ``Authorization: Bearer <token>`` header."""
    if not authorization_header:
        raise AuthError(401, "missing Authorization header")
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError(401, "malformed Authorization header (expected 'Bearer <key>')")
    return parts[1].strip()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_live(rec: KeyRecord) -> None:
    """Apply active/expiry gates. 403 for a known-but-unusable key."""
    if rec.status != "active":
        raise AuthError(403, f"key is not active (status={rec.status})")
    if rec.expires_at:
        try:
            exp = datetime.fromisoformat(rec.expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        except ValueError:
            exp = None
        if exp is not None and exp < _now():
            raise AuthError(403, "key has expired")


def authenticate(
    token: str,
    *,
    cache,
    db_lookup: Callable[[bytes], Optional[KeyRecord]],
    key_prefix: str,
    cache_ttl_seconds: int = 300,
) -> KeyRecord:
    """Resolve a plaintext bearer ``token`` to a live ``KeyRecord``.

    Path: prefix fast-reject → cache hit → (miss) DB lookup + cache populate.
    Active/expiry gates run on the record from EITHER source, so a key that
    expires mid-TTL is still caught from cache. A status *change* (disable)
    must invalidate the cache entry (see keys API).
    """
    if not token or not token.startswith(key_prefix):
        # Don't leak whether the prefix was the problem; generic 401.
        raise AuthError(401, "invalid API key")

    key_hash = hash_key(token)
    ckey = cache_key_for(key_hash)

    cached = cache.get(ckey)
    if cached is not None:
        rec = KeyRecord.from_json(cached)
        _check_live(rec)
        return rec

    rec = db_lookup(key_hash)
    if rec is None:
        raise AuthError(401, "invalid API key")

    # Populate the cache BEFORE the live-gate so a disabled/expired key is
    # still cached (its gate re-runs each request) — matches the cache-hit path.
    cache.setex(ckey, cache_ttl_seconds, rec.to_json())
    _check_live(rec)
    return rec


# --- Postgres-backed lookup (used by the FastAPI dependency) ----------------
def db_lookup_key_hash(session, key_hash: bytes) -> Optional[KeyRecord]:
    """Load a KeyRecord from llm_manager_db by raw key_hash bytes."""
    from app.models import ApiKey  # local import keeps this module light

    row = session.query(ApiKey).filter(ApiKey.key_hash == key_hash).one_or_none()
    return _key_record_from_row(row)


def db_lookup_key_prefix(session, key_prefix: str) -> Optional[KeyRecord]:
    """Load a KeyRecord by its DISPLAY prefix rather than by key material.

    #1024: the console playground has no key to present — its identity is the
    reserved ``playground-internal`` row (migration 0016) — but it now runs the
    same ``enforce``/metering chain as ``/v1``, which is expressed entirely in
    terms of a ``KeyRecord``. Building that record HERE, with the same mapping
    ``db_lookup_key_hash`` uses, is what keeps the two surfaces from disagreeing
    about what a key's limits mean.

    This is a lookup, NOT an authentication: it deliberately takes no secret and
    proves nothing. ``authenticate()`` remains the only way a request acquires a
    record from key material, and it still refuses the reserved row on status
    (``internal`` != ``active``) even if its hash were somehow guessed.
    """
    from app.models import ApiKey

    row = (session.query(ApiKey)
           .filter(ApiKey.key_prefix == key_prefix)
           .one_or_none())
    return _key_record_from_row(row)


def _key_record_from_row(row) -> Optional[KeyRecord]:
    if row is None:
        return None
    # EXO-3: resolve the cost-centre NAME while the session is still open (the
    # relationship lazy-loads). Best-effort — a key whose cost-centre row is
    # missing simply reads as a non-service key (fail closed), it never fails
    # the whole authentication.
    try:
        cc_name = row.cost_center.name if row.cost_center is not None else None
    except Exception:  # pragma: no cover - defensive
        cc_name = None
    return KeyRecord(
        key_id=str(row.id),
        cost_center_id=str(row.cost_center_id),
        allowed_models=list(row.allowed_models or []),
        rpm_limit=row.rpm_limit,
        tpm_limit=row.tpm_limit,
        max_budget_tokens=row.max_budget_tokens,
        status=row.status,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
        budget_duration_seconds=(
            int(row.budget_duration.total_seconds()) if row.budget_duration else None
        ),
        cost_center_name=cc_name,
    )


# --- EXO-3: internal service key vs tenant/end-user key ----------------------
#: Cost-centre name prefix the stack's own service keys are minted under
#: (``cli/post-install.sh::_llm_manager_mint_service_key`` → ``stack/openwebui``,
#: ``stack/dify``; the Configuration Portal reuses one of those keys). Customer /
#: per-user cost centres are operator-named through the console and are NOT
#: expected to sit under this prefix.
SERVICE_COST_CENTER_PREFIX = "stack/"


def is_internal_service_key(rec) -> bool:
    """Is this key one of the STACK's own in-cluster service keys?

    The narrowest check available without a schema change: the key's
    cost-centre name is under ``stack/``. FAILS CLOSED — an unknown, absent or
    stale-cached cost-centre name is not a service key.

    Known limitation (stated rather than papered over): cost-centre names are
    operator-writable through the console, so an admin who names a customer
    cost-centre ``stack/…`` grants that key this tier. Closing that properly
    wants a dedicated ``kind``/``scope`` column on ``api_keys`` and a
    migration, which cannot be validated here without a live DB — tracked in
    the EXO-3 report rather than invented blind.
    """
    name = (getattr(rec, "cost_center_name", None) or "").strip().lower()
    return name.startswith(SERVICE_COST_CENTER_PREFIX)
