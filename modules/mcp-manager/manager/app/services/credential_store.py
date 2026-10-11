# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Credential store — the encrypt-on-write / decrypt-on-use boundary (#36).

Sits between the API/UI and the DB. Every secret credential is encrypted with
the AES-GCM CredentialCipher BEFORE it touches the database; plaintext exists
only transiently in memory here and in the provisioner at launch time.

Cred types stored:
  pat | api_key | oauth_access | oauth_refresh

NEVER log, echo, or return plaintext anywhere except the explicit
get_decrypted() call (used only by the provisioner when launching the user's
own proxy). No logging in this module emits credential values.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# cred_type values that are SECRET (encrypted at rest). Non-secret fields
# (email, subdomain, company_domain, ...) are stored as instance config, not here.
_SECRET_CRED_TYPES = {"pat", "api_key", "oauth_access", "oauth_refresh"}


class CredentialStore:
    def __init__(self, db, cipher):
        self._db = db
        self._cipher = cipher

    def store_pat(self, user_slug: str, mcp_id: str, fields: dict,
                  secret_keys: set | None = None):
        """Encrypt + persist PAT-style secret fields for (user, mcp).

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
            self._db.upsert_secret(user_slug, mcp_id, cred_type=key,
                                   encrypted_value=enc, expires_at=None)
        # audit WITHOUT any value
        self._db.log_audit(user_slug, "store_credentials", mcp_id,
                           {"cred_types": sorted(fields.keys())})

    def store_oauth_tokens(self, user_slug: str, mcp_id: str, access: str,
                           refresh: str | None = None, expires_at=None):
        """Encrypt + persist OAuth access (+ optional refresh) tokens."""
        if access:
            self._db.upsert_secret(
                user_slug, mcp_id, cred_type="oauth_access",
                encrypted_value=self._cipher.encrypt(access), expires_at=expires_at,
            )
        if refresh:
            self._db.upsert_secret(
                user_slug, mcp_id, cred_type="oauth_refresh",
                encrypted_value=self._cipher.encrypt(refresh), expires_at=None,
            )
        self._db.log_audit(user_slug, "store_oauth", mcp_id,
                           {"has_refresh": bool(refresh)})

    def get_decrypted(self, user_slug: str, mcp_id: str) -> dict:
        """Decrypt all stored credentials for (user, mcp). only-own scoped by
        the DB query. Returns {cred_type: plaintext}. Empty dict if none.

        CALLER CONTRACT: do not log / persist the returned values. Used by the
        provisioner to inject creds into the user's own proxy at launch.
        """
        out: dict[str, str] = {}
        for row in self._db.get_secrets(user_slug, mcp_id):
            try:
                out[row["cred_type"]] = self._cipher.decrypt(row["encrypted_value"])
            except Exception:
                # Decryption failure (key rotation / tamper) — never expose the
                # ciphertext; log the cred_type only.
                logger.error("failed to decrypt %s for %s/%s (key rotated?)",
                             row["cred_type"], user_slug, mcp_id)
        return out

    def has_credentials(self, user_slug: str, mcp_id: str) -> bool:
        return bool(self._db.get_secrets(user_slug, mcp_id))

    def credential_summary(self, user_slug: str, mcp_id: str) -> list[dict]:
        """Non-secret summary for the UI — cred_type + updated_at + expiry only,
        NEVER the value (not even masked plaintext)."""
        return [
            {"cred_type": r["cred_type"],
             "expires_at": r.get("expires_at"),
             "updated_at": r.get("updated_at")}
            for r in self._db.get_secrets(user_slug, mcp_id)
        ]

    def revoke(self, user_slug: str, mcp_id: str):
        """Delete every credential for (user, mcp). only-own scoped."""
        self._db.delete_secrets(user_slug, mcp_id)
        self._db.log_audit(user_slug, "revoke_credentials", mcp_id, {})
