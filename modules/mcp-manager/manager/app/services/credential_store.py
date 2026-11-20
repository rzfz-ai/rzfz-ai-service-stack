# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Credential store — the encrypt-on-write / decrypt-on-use boundary (#36).

Sits between the API/UI and the DB. Every secret credential is encrypted with
the AES-GCM CredentialCipher BEFORE it touches the database; plaintext exists
only transiently in memory here and in the provisioner at launch time.

Cred types stored:
  pat | api_key | oauth_access | oauth_refresh

oauth_access/oauth_refresh live in OpenBao when the openbao profile is on: a
technical user's under secret/apps/<svc>/oauth/, a person's under
secret/users/<oidc-sub>/oauth/ once OPENBAO_SUB_IS_UID=true (#2472, B22).
There they never touch Postgres, and every OpenBao failure is closed: a write
or a revoke raises CredentialStoreError, a read serves no OAuth token.

KEY (#2472, design record ruling 1): every method takes `owner`, a person's
OIDC sub (the Authentik uid, read from X-Authentik-Uid) or a technical user's
"~svc-<name>" — never a user_slug, which is lossy and reusable. `user_slug`,
where passed, is stored for display only.

Legacy window (B6 fix round 1): reads also take `legacy_slug`, which serves a
pre-2026.11 row the migration has not decided yet by its slug, as 2026.09
did. A sub-keyed row of the same cred_type always wins. Writes are always
sub-keyed: a user who stores a credential before their old rows are
migrated gets a NEW sub-keyed row that shadows the old one; the migration
then quarantines the old one as "superseded".

NEVER log, echo, or return plaintext anywhere except the explicit
get_decrypted() call (used only by the provisioner when launching the user's
own proxy). No logging in this module emits credential values.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: #2677's audit action (app.services.database.AUDIT_PERSON_CREDENTIALS_PURGED).
_PURGED = "person_credentials_purged"
#: ... and when that failed (database.AUDIT_PERSON_CREDENTIALS_PURGE_FAILED).
_PURGE_FAILED = "person_credentials_purge_failed"

# cred_type values that are SECRET (encrypted at rest). Non-secret fields
# (email, subdomain, company_domain, ...) are stored as instance config, not here.
_SECRET_CRED_TYPES = {"pat", "api_key", "oauth_access", "oauth_refresh"}
_OAUTH_TYPES = ("oauth_access", "oauth_refresh")


class CredentialStoreError(RuntimeError):
    """A credential could not be stored or revoked where it must live."""


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _when(value):
    """An ISO 8601 string from the KV document as an aware datetime, or None."""
    from datetime import datetime, timezone
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


class CredentialStore:
    def __init__(self, db, cipher, oauth_backend=None):
        self._db = db
        self._cipher = cipher
        # #2472 / A1 D8: with the openbao profile on, OAuth tokens live in
        # OpenBao (app.services.openbao_oauth): a technical user's under
        # apps/<svc>/, a person's under users/<sub>/ once the sub gate is
        # proven (backend.persons). None = Postgres only.
        self._bao = oauth_backend

    # ------------------------------------------------------------------
    # #2472 B22: the OpenBao half
    # ------------------------------------------------------------------

    def _in_openbao(self, owner: str) -> bool:
        if self._bao is None or not owner:
            return False
        return True if str(owner).startswith("~") else bool(self._bao.persons)

    @staticmethod
    def _bao_key(owner: str):
        """(tree, key): ("apps", "<svc-name>") for a technical principal,
        ("users", "<oidc-sub>") for a person. The owner of a person's
        credential IS their sub (B6), never a slug or a username; the
        backend's path check refuses anything else."""
        if owner.startswith("~"):
            return "apps", owner[1:]
        return "users", owner

    def _bao_read(self, owner: str, mcp_id: str):
        """The OpenBao document, or None (absent, unusable owner, OpenBao down)."""
        from app.services.openbao_oauth import OpenBaoError
        try:
            return self._bao.get(*self._bao_key(owner), mcp_id)
        except OpenBaoError as e:
            logger.error("oauth tokens for %s/%s unavailable: %s", owner, mcp_id, e)
            return None

    def _bao_write(self, owner: str, mcp_id: str, doc: dict) -> None:
        from app.services.openbao_oauth import OpenBaoError
        try:
            self._bao.put(*self._bao_key(owner), mcp_id, doc)
        except OpenBaoError as e:
            raise CredentialStoreError(f"OAuth grant for {mcp_id} not stored: {e}") from e

    def _move_to_openbao(self, owner, mcp_id, legacy):
        """A grant stored before its OpenBao half was on: write it to OpenBao,
        then drop the Postgres rows. On any failure nothing is served."""
        row = self._db.get_secret(owner, mcp_id, "oauth_access") or {}
        try:
            self.store_oauth_tokens(owner, mcp_id, access=legacy["oauth_access"],
                                    refresh=legacy.get("oauth_refresh"),
                                    expires_at=row.get("expires_at"), _audit=False)
        except CredentialStoreError as e:
            logger.error("oauth move to OpenBao failed for %s/%s: %s", owner, mcp_id, e)
            return None
        if row.get("reauth_required_at"):
            self.mark_reauth_required(owner, mcp_id, "moved with the mark")
        self._db.log_audit(owner, "oauth_moved_to_openbao", mcp_id, {})
        return self._bao_read(owner, mcp_id)

    def reconnect_needed(self, owner: str, mcp_id: str, legacy_slug: str | None = None) -> bool:
        """#2472 B22 (decision of 2026-10-14): a pre-2026.11 OAuth grant not
        keyed by the person's sub is not migrated into OpenBao. True when such
        a row is all this person has for `mcp_id`: they reconnect once."""
        if not legacy_slug or str(owner).startswith("~") or not self._in_openbao(owner):
            return False
        try:
            rows = self._db.get_secrets(owner, mcp_id, legacy_slug=legacy_slug)
        except Exception:  # noqa: BLE001 — a hint must never break the page
            return False
        if not any(r.get("legacy") and r["cred_type"] == "oauth_access" for r in rows):
            return False
        return not (self._bao_read(owner, mcp_id) or {}).get("access")

    def oauth_expiry(self, owner: str, mcp_id: str):
        """When the stored access token expires (None = unknown/never)."""
        if not self._in_openbao(owner):
            row = self._db.get_secret(owner, mcp_id, "oauth_access") or {}
            return row.get("expires_at") if isinstance(row, dict) else None
        return _when((self._bao_read(owner, mcp_id) or {}).get("expires_at"))

    def drop_oauth(self, owner: str, mcp_id: str) -> None:
        """Forget the OAuth tokens of (owner, mcp) only (a cached machine
        token whose client pair changed). Raises when OpenBao cannot be
        reached: a dropped cache must never look dropped."""
        for t in _OAUTH_TYPES:
            self._db.delete_secret(owner, mcp_id, t)
        if self._in_openbao(owner):
            from app.services.openbao_oauth import OpenBaoError
            try:
                self._bao.delete(*self._bao_key(owner), mcp_id)
            except OpenBaoError as e:
                raise CredentialStoreError(f"OAuth grant for {mcp_id} not dropped in OpenBao: {e}") from e

    def purge_oauth(self, owner: str) -> int:
        """#2472: delete every OpenBao OAuth grant of a TECHNICAL owner (the
        technical user was revoked, or its name now belongs to another
        account). Postgres rows go with the caller's own DB call. Returns
        how many were deleted; raises when OpenBao cannot be reached."""
        if not str(owner).startswith("~") or not self._in_openbao(owner):
            return 0
        from app.services.openbao_oauth import OpenBaoError
        tree, key = self._bao_key(owner)
        try:
            providers = self._bao.providers(tree, key)
            for provider in providers:
                self._bao.delete(tree, key, provider)
        except OpenBaoError as e:
            raise CredentialStoreError(f"OAuth grants of {owner} not purged in OpenBao: {e}") from e
        return len(providers)

    def store_pat(self, owner: str, mcp_id: str, fields: dict,
                  secret_keys: set | None = None, user_slug: str | None = None):
        """Encrypt + persist PAT-style secret fields for (owner, mcp).

        `fields` maps cred field key -> value. By default every key is treated
        as a secret and stored under its own cred_type; pass `secret_keys` to
        restrict which keys are encrypted (the catalog marks non-secret fields
        like email/subdomain, which the caller stores in instance config
        instead). Existing values for the same cred_type are replaced.
        """
        for key, value in fields.items():
            if secret_keys is not None and key not in secret_keys:
                continue
            if value is None or value == "":
                continue
            enc = self._cipher.encrypt(str(value))
            self._db.upsert_secret(owner, mcp_id, cred_type=key,
                                   encrypted_value=enc, expires_at=None,
                                   user_slug=user_slug)
        # audit WITHOUT any value
        self._db.log_audit(owner, "store_credentials", mcp_id,
                           {"cred_types": sorted(fields.keys())})

    def store_oauth_tokens(self, owner: str, mcp_id: str, access: str,
                           refresh: str | None = None, expires_at=None,
                           user_slug: str | None = None, _audit: bool = True):
        """Encrypt + persist OAuth access (+ optional refresh) tokens. With
        the owner's OpenBao half on, they go to OpenBao only (a new grant
        clears a re-authorization mark); a failure raises
        CredentialStoreError and nothing lands in Postgres instead."""
        if self._in_openbao(owner):
            from app.services.openbao_oauth import OpenBaoError
            if not access:
                return
            try:
                prev = self._bao.get(*self._bao_key(owner), mcp_id) or {}
            except OpenBaoError as e:
                raise CredentialStoreError(f"OAuth grant for {mcp_id} not stored: {e}") from e
            self._bao_write(owner, mcp_id, {
                "access": access,
                "refresh": refresh or prev.get("refresh") or "",
                "expires_at": _iso(expires_at),
                "reauth_required_at": ""})
            for t in _OAUTH_TYPES:
                self._db.delete_secret(owner, mcp_id, t)
            if _audit:
                self._db.log_audit(owner, "store_oauth", mcp_id,
                                   {"has_refresh": bool(refresh), "store": "openbao"})
            return
        if access:
            self._db.upsert_secret(
                owner, mcp_id, cred_type="oauth_access",
                encrypted_value=self._cipher.encrypt(access), expires_at=expires_at,
                user_slug=user_slug,
            )
        if refresh:
            self._db.upsert_secret(
                owner, mcp_id, cred_type="oauth_refresh",
                encrypted_value=self._cipher.encrypt(refresh), expires_at=None,
                user_slug=user_slug,
            )
        self._db.log_audit(owner, "store_oauth", mcp_id,
                           {"has_refresh": bool(refresh)})

    def store_machine_fingerprint(self, owner: str, mcp_id: str, fingerprint: str):
        """#2472: which client pair the cached machine token (oauth_access)
        of a technical user was fetched with (a SHA-256, encrypted like every
        row). No audit row: store_oauth_tokens just wrote one."""
        self._db.upsert_secret(owner, mcp_id, cred_type="oauth_cc_fp",
                               encrypted_value=self._cipher.encrypt(fingerprint),
                               expires_at=None)

    def get_decrypted(self, owner: str, mcp_id: str, legacy_slug: str | None = None) -> dict:
        """Decrypt all stored credentials for (user, mcp). only-own scoped by
        the DB query. Returns {cred_type: plaintext}. Empty dict if none.

        CALLER CONTRACT: do not log / persist the returned values. Used by the
        provisioner to inject creds into the user's own proxy at launch.
        """
        out: dict[str, str] = {}
        for row in self._db.get_secrets(owner, mcp_id, legacy_slug=legacy_slug):
            if row["cred_type"] in out:
                continue  # a sub-keyed row (listed first) shadows a legacy one
            try:
                out[row["cred_type"]] = self._cipher.decrypt(row["encrypted_value"])
            except Exception:
                # Decryption failure (key rotation / tamper) — never expose the
                # ciphertext; log the cred_type only.
                logger.error("failed to decrypt %s for %s/%s (key rotated?)",
                             row["cred_type"], owner, mcp_id)
        if not self._in_openbao(owner):
            return out
        return self._with_openbao(owner, mcp_id, out, legacy_slug)

    def _with_openbao(self, owner, mcp_id, out, legacy_slug):
        """get_decrypted's OpenBao half: OAuth tokens from OpenBao only. An
        owner-keyed Postgres grant (stored before the half was on) moves on
        this first read; an undecided legacy row's OAuth token is not served
        (B6's migration decides whose it is, never this read)."""
        owned = {}
        if any(t in out for t in _OAUTH_TYPES):
            # owner-keyed rows only (no legacy_slug): they shadow a legacy row
            # of the same type in `out`, and a legacy-only token is dropped
            for row in self._db.get_secrets(owner, mcp_id):
                if row["cred_type"] in _OAUTH_TYPES and row["cred_type"] in out:
                    owned[row["cred_type"]] = out[row["cred_type"]]
            for t in _OAUTH_TYPES:
                out.pop(t, None)
        doc = self._bao_read(owner, mcp_id)
        if doc is None and owned.get("oauth_access"):
            doc = self._move_to_openbao(owner, mcp_id, owned)
        elif doc and owned:
            # OpenBao already holds the grant (a move whose Postgres delete
            # failed, or a grant stored again): the old rows go, never served
            for t in _OAUTH_TYPES:
                self._db.delete_secret(owner, mcp_id, t)
        if doc:
            if doc.get("access"):
                out["oauth_access"] = doc["access"]
            if doc.get("refresh"):
                out["oauth_refresh"] = doc["refresh"]
        return out

    def has_credentials(self, owner: str, mcp_id: str, legacy_slug: str | None = None) -> bool:
        rows = self._db.get_secrets(owner, mcp_id, legacy_slug=legacy_slug)
        if not self._in_openbao(owner):
            return bool(rows)
        if any(r["cred_type"] not in _OAUTH_TYPES for r in rows):
            return True
        return bool(self.get_decrypted(owner, mcp_id, legacy_slug=legacy_slug).get("oauth_access"))

    def credential_summary(self, owner: str, mcp_id: str,
                           legacy_slug: str | None = None) -> list[dict]:
        """Non-secret summary for the UI — cred_type + updated_at + expiry only,
        NEVER the value (not even masked plaintext)."""
        out, seen = [], set()
        bao = self._in_openbao(owner)
        for r in self._db.get_secrets(owner, mcp_id, legacy_slug=legacy_slug):
            if r["cred_type"] in seen or (bao and r["cred_type"] in _OAUTH_TYPES):
                continue
            seen.add(r["cred_type"])
            out.append({"cred_type": r["cred_type"],
                        "expires_at": r.get("expires_at"),
                        "updated_at": r.get("updated_at"),
                        "reauth_required": r.get("reauth_required_at") is not None})
        if bao:
            self.get_decrypted(owner, mcp_id, legacy_slug=legacy_slug)  # moves an old grant
            doc = self._bao_read(owner, mcp_id) or {}
            marked = bool(doc.get("reauth_required_at"))
            for t, k in (("oauth_access", "access"), ("oauth_refresh", "refresh")):
                if doc.get(k):
                    out.append({"cred_type": t,
                                "expires_at": _when(doc.get("expires_at")) if t == "oauth_access" else None,
                                "updated_at": None, "reauth_required": marked})
        return out

    def reauth_required(self, owner: str, mcp_id: str) -> bool:
        """#2554: whether the provider rejected this owner's OAuth grant
        (the gateway marked it). A lookup failure reads as "not marked": the
        gateway then dials, and the provider's own answer decides."""
        if self._in_openbao(owner):
            return bool((self._bao_read(owner, mcp_id) or {}).get("reauth_required_at"))
        try:
            row = self._db.get_secret(owner, mcp_id, "oauth_access")
        except Exception:  # noqa: BLE001
            return False
        return isinstance(row, dict) and row.get("reauth_required_at") is not None

    def mark_reauth_required(self, owner: str, mcp_id: str, reason: str) -> int:
        """#2554: the provider rejected this owner's OAuth grant. The grant is
        marked (not deleted: the portal shows "authorize again"), and the
        event is audited with its reason, never with a value."""
        if self._in_openbao(owner):
            n = self._mark_in_openbao(owner, mcp_id)
        else:
            n = self._db.mark_reauth_required(owner, mcp_id)
        self._db.log_audit(owner, "oauth_grant_rejected", mcp_id,
                           {"reason": reason, "newly_marked": n})
        return n

    def _mark_in_openbao(self, owner, mcp_id) -> int:
        from datetime import datetime, timezone
        doc = self._bao_read(owner, mcp_id)
        if not doc or doc.get("reauth_required_at"):
            return 0
        try:
            self._bao_write(owner, mcp_id, dict(doc, reauth_required_at=_iso(datetime.now(timezone.utc))))
        except CredentialStoreError as e:
            logger.error("re-authorization mark for %s/%s not stored: %s", owner, mcp_id, e)
            return 0
        return 1

    def purge_person(self, owner: str, reason: str) -> dict:
        """#2677: a person left (deleted or deactivated in Authentik) — every
        credential they stored goes, on every server, OAuth and PAT alike, in
        every backend this store writes to. Returns {"rows", "openbao",
        "mcp_ids"} (+ "error" when the OpenBao half could not be purged; the
        Postgres rows then STAY, so the next sweep still finds the person).
        Audited with counts and server ids, never a value.

        The OpenBao half is the person tree users/<sub>/oauth/<integration>
        (#2472 B22): purged whenever the store carries an OpenBao OAuth
        backend (`_bao`) whose person half is on."""
        if not owner or str(owner).startswith("~"):
            raise ValueError("purge_person needs a person's sub, not a technical owner")
        bao = getattr(self, "_bao", None)
        bao_ids: list = []
        if bao is not None and getattr(bao, "persons", False):
            try:
                bao_ids = list(bao.providers("users", owner))
                for provider in bao_ids:
                    bao.delete("users", owner, provider)
            except Exception as e:  # noqa: BLE001 — never a value in the message
                logger.error("purge of a departed person's OpenBao grants failed: %s",
                             type(e).__name__)
                self._db.log_audit(owner, _PURGE_FAILED, None,
                                   {"reason": reason, "error": "openbao_unavailable"})
                return {"rows": 0, "openbao": 0, "mcp_ids": [], "error": "openbao_unavailable"}
        gone = self._db.delete_person_secrets(owner)
        mcp_ids = sorted({m for m, _t in gone} | set(bao_ids))
        if gone or bao_ids:
            self._db.log_audit(owner, _PURGED, None, {
                "reason": reason, "rows": len(gone), "openbao": len(bao_ids),
                "mcp_ids": mcp_ids,
                "cred_types": sorted({t for _m, t in gone})})
        return {"rows": len(gone), "openbao": len(bao_ids), "mcp_ids": mcp_ids}

    def revoke(self, owner: str, mcp_id: str, legacy_slug: str | None = None):
        """Delete every credential for (owner, mcp) — and the undecided legacy
        rows served to this caller under `legacy_slug`. only-own scoped.
        With the owner's OpenBao half on, the grant's KV metadata goes too
        (every version); a revoke that cannot reach OpenBao raises, it must
        never look done."""
        self._db.delete_secrets(owner, mcp_id, legacy_slug=legacy_slug)
        if self._in_openbao(owner):
            from app.services.openbao_oauth import OpenBaoError
            try:
                self._bao.delete(*self._bao_key(owner), mcp_id)
            except OpenBaoError as e:
                raise CredentialStoreError(f"OAuth grant for {mcp_id} not revoked in OpenBao: {e}") from e
        self._db.log_audit(owner, "revoke_credentials", mcp_id, {})
