# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Credential encryption for the personal MCP manager (#36).

Every user's external credentials (PATs, OAuth access/refresh tokens, API
keys) are stored ENCRYPTED-AT-REST in `mcp_manager_db`. This module is the
only place plaintext credentials are turned into ciphertext and back.

Design
------
- AES-256-GCM (authenticated encryption — confidentiality + integrity) via the
  `cryptography` library's `AESGCM` primitive.
- Master key from `MCP_MANAGER_SECRET_KEY` (base64-encoded 32 bytes), minted by
  init/post-install like every other stack secret and injected as an env var on
  the mcp-manager container. The key NEVER touches the DB.
- A fresh random 96-bit nonce per `encrypt()` call (GCM's nonce-reuse-is-fatal
  requirement). Stored token layout: ``base64( nonce(12) || ciphertext || tag(16) )``
  — `AESGCM.encrypt` already appends the tag to the ciphertext, so we only
  prepend the nonce.

Security invariants (enforced by tests/unit/mcp-manager/test_crypto.py):
  - ciphertext != plaintext and plaintext bytes never appear in the token
  - same plaintext encrypts to different tokens each call (random nonce)
  - any tamper (incl. cross-key) fails the GCM tag → InvalidTag, never a
    silently-wrong plaintext
  - a missing / too-short master key is rejected loudly (no weak-key fallback)

This module deliberately has NO logging — it must never emit plaintext OR
ciphertext to logs. Callers are responsible for never logging the values
they pass in or get back.
"""

from __future__ import annotations

import base64
import binascii

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-256 key length (bytes) and GCM nonce length (bytes).
_KEY_LEN = 32
_NONCE_LEN = 12


def _decode_key(master_key_b64: str) -> bytes:
    """Decode + validate the base64 master key. Raises ValueError on any
    problem — we fail closed rather than derive a weak key."""
    if not master_key_b64 or not master_key_b64.strip():
        raise ValueError(
            "MCP_MANAGER_SECRET_KEY is empty — refusing to operate without an "
            "encryption key. Mint one via init/post-install."
        )
    try:
        raw = base64.b64decode(master_key_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"MCP_MANAGER_SECRET_KEY is not valid base64: {e}") from e
    if len(raw) < _KEY_LEN:
        raise ValueError(
            f"MCP_MANAGER_SECRET_KEY decodes to {len(raw)} bytes; need >= {_KEY_LEN} "
            "(AES-256). Mint a 32-byte key."
        )
    # If the operator supplied a longer key, take the first 32 bytes
    # deterministically (so the same .env value always yields the same key).
    return raw[:_KEY_LEN]


class CredentialCipher:
    """AES-256-GCM encrypt/decrypt of short credential strings.

    Construct once per request/use with the master key from the environment;
    cheap to build. Holds the key in memory only.
    """

    def __init__(self, master_key_b64: str):
        self._key = _decode_key(master_key_b64)
        self._aead = AESGCM(self._key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext credential. Returns a base64 token string."""
        import os

        nonce = os.urandom(_NONCE_LEN)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Decrypt a token produced by `encrypt`. Raises InvalidTag on any
        tamper / wrong key (authenticated decryption)."""
        raw = base64.b64decode(token)
        if len(raw) < _NONCE_LEN + 16:
            raise ValueError("ciphertext token too short to contain nonce + tag")
        nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        pt = self._aead.decrypt(nonce, ct, None)
        return pt.decode("utf-8")
