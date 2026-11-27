# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""PostgreSQL database layer for the personal MCP manager (#36).

Mirrors agent-manager's database.py: an ordered MIGRATIONS list driven by a
schema_version table, RealDictCursor, conn()/cursor() contextmanagers, and
psycopg2.extras.Json for JSONB columns.

Three tables:
  - mcp_instances     — one running per-user proxy per (user_slug, mcp_id)
  - mcp_user_secrets  — encrypted-at-rest credentials. The value column is
                        `encrypted_value` (AES-GCM token from crypto.py); the
                        manager NEVER stores plaintext here.
  - mcp_type_bindings — which of the user's agents (hermes/moltis/opencode) a
                        given (user_slug, mcp_id) is wired into.

SECURITY: every secret/instance read filters by user_slug (only-own). All
queries are parameterized (psycopg2 %s / %(name)s) — no user data is ever
shell/f-string interpolated into SQL.
"""

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.errors
import psycopg2.extras

logger = logging.getLogger(__name__)

psycopg2.extras.register_uuid()

MIGRATIONS = [
    # Version 1: initial schema.
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMPTZ DEFAULT NOW()
    );

    -- A provisioned per-user MCP proxy instance.
    CREATE TABLE IF NOT EXISTS mcp_instances (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        mcp_id VARCHAR(48) NOT NULL,
        user_id VARCHAR(128) NOT NULL,
        user_slug VARCHAR(24) NOT NULL,
        container_id VARCHAR(64),
        container_name VARCHAR(96) NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'provisioning',
        -- non-secret per-instance settings (e.g. subdomain, company_domain) +
        -- the proxy's generated route secret. NEVER stores credentials.
        config JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        stopped_at TIMESTAMPTZ,
        last_accessed TIMESTAMPTZ DEFAULT NOW(),
        error_message TEXT
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_instance_user
        ON mcp_instances(mcp_id, user_slug)
        WHERE state != 'destroyed';
    CREATE INDEX IF NOT EXISTS idx_mcp_instances_user ON mcp_instances(user_slug);
    CREATE INDEX IF NOT EXISTS idx_mcp_instances_state ON mcp_instances(state);

    -- Encrypted-at-rest credentials. encrypted_value is an AES-GCM token
    -- (crypto.CredentialCipher). cred_type: pat | api_key | email | oauth_access
    -- | oauth_refresh. (email/non-secret settings normally live in
    -- mcp_instances.config; cred_type is kept generic for flexibility.)
    CREATE TABLE IF NOT EXISTS mcp_user_secrets (
        id BIGSERIAL PRIMARY KEY,
        user_slug VARCHAR(24) NOT NULL,
        mcp_id VARCHAR(48) NOT NULL,
        cred_type VARCHAR(24) NOT NULL,
        encrypted_value TEXT NOT NULL,
        expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_secret_unique
        ON mcp_user_secrets(user_slug, mcp_id, cred_type);
    CREATE INDEX IF NOT EXISTS idx_mcp_secret_user ON mcp_user_secrets(user_slug);

    -- Which of the user's agents a given (user_slug, mcp_id) is wired into.
    CREATE TABLE IF NOT EXISTS mcp_type_bindings (
        id BIGSERIAL PRIMARY KEY,
        user_slug VARCHAR(24) NOT NULL,
        mcp_id VARCHAR(48) NOT NULL,
        consumer VARCHAR(24) NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_binding_unique
        ON mcp_type_bindings(user_slug, mcp_id, consumer);
    CREATE INDEX IF NOT EXISTS idx_mcp_binding_user ON mcp_type_bindings(user_slug);

    CREATE TABLE IF NOT EXISTS mcp_audit_log (
        id BIGSERIAL PRIMARY KEY,
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        user_slug VARCHAR(24),
        action VARCHAR(32) NOT NULL,
        mcp_id VARCHAR(48),
        details JSONB DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_mcp_audit_user ON mcp_audit_log(user_slug);

    INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
    """,
    # Version 2: per-integration governance (#36 follow-up). Config-UI writes
    # which catalog integrations are AVAILABLE and their minimum tier
    # (regular|power|admin). Absence of a row => catalog default (available,
    # min_tier=regular). DB-backed (NOT .env) — sidesteps the Config-UI
    # single-file-mount .env inode-rewrite bug (same rationale as agent-manager's
    # agent_settings table).
    """
    CREATE TABLE IF NOT EXISTS mcp_integration_governance (
        mcp_id VARCHAR(48) PRIMARY KEY,
        available BOOLEAN NOT NULL DEFAULT TRUE,
        min_tier VARCHAR(16) NOT NULL DEFAULT 'regular',
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    INSERT INTO schema_version (version) VALUES (2) ON CONFLICT DO NOTHING;
    """,
    # Version 3: per-integration governance CONFIG (#36 two-tier cognee). JSONB
    # blob for extra per-integration governance data — e.g. the company-brain
    # dataset name the cognee-company instances are scoped to.
    """
    ALTER TABLE mcp_integration_governance
        ADD COLUMN IF NOT EXISTS config JSONB DEFAULT '{}';
    INSERT INTO schema_version (version) VALUES (3) ON CONFLICT DO NOTHING;
    """,
    # Version 4 (#2472, B5 design record §2): the MCP gateway's identities.
    #
    # A person's rows are keyed by the OIDC `sub` (the Authentik `uid`), never
    # by `user_slug` (lossy: two people can share one, and a new hire can
    # inherit a deleted user's). `user_slug` stays as a display value and as
    # the name part of the per-user proxy containers.
    #   mcp_instances.owner_sub, mcp_type_bindings.owner_sub — the owner's sub;
    #   mcp_user_secrets.owner_key — a person's sub or "~svc-<name>".
    # NULL there means "a row from before this migration, not attributed
    # yet": no gateway or portal read ever matches it (they all filter by the
    # key). gateway_identity/identity_migration re-key such a row only on the
    # evidence the record names, and QUARANTINE everything else
    # (mcp_identity_quarantine, rows moved there verbatim, credentials still
    # AES-GCM ciphertext; purged only by an explicit, audited admin action).
    #
    #   mcp_users            — SSO users seen on the portal: slug, username,
    #                          email and the sub read from X-Authentik-Uid.
    #   mcp_technical_users  — MIRROR of A17's service accounts (A1 D7), owner
    #                          by sub; revoked, never deleted.
    #   mcp_gateway_tokens   — caller credentials, SHA-256 ONLY.
    #   mcp_identity_migration — when this migration ran (the 30-day clock
    #                          for "unattributed after 30 days").
    """
    CREATE TABLE IF NOT EXISTS mcp_users (
        user_slug VARCHAR(24) PRIMARY KEY,
        username VARCHAR(150) NOT NULL,
        email VARCHAR(255),
        sub VARCHAR(128),
        last_seen TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_mcp_users_email ON mcp_users (lower(email));
    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_users_sub ON mcp_users (sub) WHERE sub IS NOT NULL;

    CREATE TABLE IF NOT EXISTS mcp_technical_users (
        name VARCHAR(44) PRIMARY KEY,
        authentik_pk INTEGER,
        owner_sub VARCHAR(128) NOT NULL,
        owner_username VARCHAR(150) NOT NULL DEFAULT '',
        bindings JSONB NOT NULL DEFAULT '[]',
        synced_at TIMESTAMPTZ DEFAULT NOW(),
        revoked_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS mcp_gateway_tokens (
        id BIGSERIAL PRIMARY KEY,
        token_hash CHAR(64) NOT NULL UNIQUE,
        principal VARCHAR(128) NOT NULL,
        kind VARCHAR(12) NOT NULL CHECK (kind IN ('user', 'technical')),
        consumer VARCHAR(24) NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        last_used_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS idx_mcp_gw_tokens_principal ON mcp_gateway_tokens (principal);

    -- The sub keys. Existing rows stay NULL (unattributed) until re-keyed.
    ALTER TABLE mcp_instances ADD COLUMN IF NOT EXISTS owner_sub VARCHAR(128);
    ALTER TABLE mcp_type_bindings ADD COLUMN IF NOT EXISTS owner_sub VARCHAR(128);
    ALTER TABLE mcp_user_secrets ADD COLUMN IF NOT EXISTS owner_key VARCHAR(128);
    -- #2554: 'live' from the moment a Gitea service user is asked for (so a
    -- crash half-way is still revoked), 'owed' when its revoke is due while
    -- the row itself may be live (the name now belongs to another Authentik
    -- account, a token the gateway did not store), 'revoked' once Gitea
    -- confirmed its tokens deleted and its login prohibited. NULL: none was
    -- ever made. gitea_user_id + gitea_authentik_pk: the ONE Gitea user the
    -- portal made, and the Authentik account (pk) it was made for; the portal
    -- takes over no other Gitea user of that name.
    ALTER TABLE mcp_technical_users ADD COLUMN IF NOT EXISTS gitea_state VARCHAR(8);
    ALTER TABLE mcp_technical_users ADD COLUMN IF NOT EXISTS gitea_user_id BIGINT;
    ALTER TABLE mcp_technical_users ADD COLUMN IF NOT EXISTS gitea_authentik_pk INTEGER;
    -- user_slug is display-only from here on; a technical user's rows carry
    -- its "~svc-<name>" there, and an audit row carries a sub.
    ALTER TABLE mcp_user_secrets ALTER COLUMN user_slug TYPE VARCHAR(48);
    ALTER TABLE mcp_user_secrets ALTER COLUMN user_slug DROP NOT NULL;
    ALTER TABLE mcp_type_bindings ALTER COLUMN user_slug DROP NOT NULL;
    ALTER TABLE mcp_audit_log ALTER COLUMN user_slug TYPE VARCHAR(128);

    -- Uniqueness moves from the slug to the key. The instance slug index
    -- stays: the slug is the container name part, so two live instances of
    -- one integration under one slug would collide in docker.
    DROP INDEX IF EXISTS idx_mcp_secret_unique;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_secret_owner_unique
        ON mcp_user_secrets (owner_key, mcp_id, cred_type) WHERE owner_key IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_mcp_secret_owner ON mcp_user_secrets (owner_key);
    DROP INDEX IF EXISTS idx_mcp_binding_unique;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_binding_owner_unique
        ON mcp_type_bindings (owner_sub, mcp_id, consumer) WHERE owner_sub IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_instance_owner
        ON mcp_instances (mcp_id, owner_sub)
        WHERE state != 'destroyed' AND owner_sub IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_mcp_instances_owner ON mcp_instances (owner_sub);

    CREATE TABLE IF NOT EXISTS mcp_identity_quarantine (
        id BIGSERIAL PRIMARY KEY,
        source_table VARCHAR(32) NOT NULL,
        source_id TEXT NOT NULL,
        user_slug VARCHAR(48),
        mcp_id VARCHAR(48),
        reason VARCHAR(48) NOT NULL,
        -- the row as it was; a credential's encrypted_value stays AES-GCM
        -- ciphertext, an instance's route secret is dropped (its container
        -- is removed with it)
        row_data JSONB NOT NULL,
        quarantined_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (source_table, source_id)
    );
    CREATE INDEX IF NOT EXISTS idx_mcp_quarantine_slug ON mcp_identity_quarantine (user_slug);

    -- started_at: the schema upgrade. instances_done_at: the instance run
    -- completed, which is ALSO the start of the "unattributed after 30 days"
    -- clock (before it, no row can have been attributed, so none is listed,
    -- purgeable or counted). attribution_*: the last attribution attempt's
    -- outcome (error class only, never a value) for `rzfz status`.
    CREATE TABLE IF NOT EXISTS mcp_identity_migration (
        id SMALLINT PRIMARY KEY CHECK (id = 1),
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        instances_done_at TIMESTAMPTZ,
        attribution_ok_at TIMESTAMPTZ,
        attribution_error_at TIMESTAMPTZ,
        attribution_error VARCHAR(160)
    );
    INSERT INTO mcp_identity_migration (id) VALUES (1) ON CONFLICT DO NOTHING;

    -- The per-user cognee accounts made before 2026.11 were derived from the
    -- SLUG (email cognee-<slug>@MAIN_DOMAIN, password HMAC(master, slug)), so
    -- a reused username would log into a former employee's memory (#2572).
    -- From 2026.11 an account is derived from the sub. A legacy account is
    -- kept (cognee has no measured rename) and MAPPED to one sub only by the
    -- directory-confirmed instance run; undecidable ones are quarantined and
    -- never served. One row per slug that ever provisioned a cognee tier.
    CREATE TABLE IF NOT EXISTS mcp_cognee_accounts (
        legacy_slug VARCHAR(24) PRIMARY KEY,
        state VARCHAR(12) NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'attributed', 'quarantined')),
        owner_sub VARCHAR(128),
        reason VARCHAR(48),
        decided_at TIMESTAMPTZ
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_cognee_owner
        ON mcp_cognee_accounts (owner_sub) WHERE owner_sub IS NOT NULL;
    INSERT INTO mcp_cognee_accounts (legacy_slug)
        SELECT DISTINCT user_slug FROM mcp_instances
        WHERE mcp_id IN ('cognee-personal', 'cognee-company')
    ON CONFLICT DO NOTHING;

    INSERT INTO schema_version (version) VALUES (4) ON CONFLICT DO NOTHING;
    """,
    # Version 5 (#2086, design record §7 ruling I2): Docling document keys,
    # bound to the caller a convert returned them to. The gateway refuses an
    # export whose key is not bound to that caller (docling-mcp's cache is
    # shared by every caller and keyed by a hash of the source).
    # caller_key: a person's sub, "~svc-<name>", or "owui:<id>" for an
    # unresolved Open WebUI user (the gateway's slot/session key).
    """
    CREATE TABLE IF NOT EXISTS mcp_gateway_doc_keys (
        server_id VARCHAR(48) NOT NULL,
        caller_key VARCHAR(160) NOT NULL,
        doc_key VARCHAR(128) NOT NULL,
        bound_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (server_id, caller_key, doc_key)
    );
    CREATE INDEX IF NOT EXISTS idx_mcp_gw_doc_keys_bound_at ON mcp_gateway_doc_keys (bound_at);

    INSERT INTO schema_version (version) VALUES (5) ON CONFLICT DO NOTHING;
    """,
    # Version 6 (#2554): an OAuth grant the provider no longer accepts (the
    # user revoked it there, or it expired for good). The gateway sets the
    # mark when the upstream rejects the grant's token; the portal shows
    # "authorize again" and the gateway answers "connect it again" without
    # dialling. Storing a new token (a fresh authorization) clears it.
    """
    ALTER TABLE mcp_user_secrets ADD COLUMN IF NOT EXISTS reauth_required_at TIMESTAMPTZ;

    INSERT INTO schema_version (version) VALUES (6) ON CONFLICT DO NOTHING;
    """,
    # Version 7 (#2677): a deactivated person's stored credentials are deleted
    # only after 30 days CONTINUOUSLY deactivated (a deleted person's at once).
    # mcp_person_deactivated: when a person was first seen deactivated; the
    # row goes when they are seen active again (the clock restarts) or when
    # their credentials are purged. departed_sweep_*: the last sweep's outcome
    # (error class only, never a value) for `rzfz status`.
    """
    CREATE TABLE IF NOT EXISTS mcp_person_deactivated (
        sub VARCHAR(160) PRIMARY KEY,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    ALTER TABLE mcp_identity_migration ADD COLUMN IF NOT EXISTS departed_sweep_ok_at TIMESTAMPTZ;
    ALTER TABLE mcp_identity_migration ADD COLUMN IF NOT EXISTS departed_sweep_error_at TIMESTAMPTZ;
    ALTER TABLE mcp_identity_migration ADD COLUMN IF NOT EXISTS departed_sweep_error VARCHAR(160);

    INSERT INTO schema_version (version) VALUES (7) ON CONFLICT DO NOTHING;
    """,
    # Version 8 (#2574 item 2): a person with TWO own cognee memories. The one
    # bound to the CURRENT identity (sub-derived) wins; a legacy account the
    # run maps to the same sub later is kept, not served, until an admin
    # restores it. mcp_cognee_sub_used: subs whose sub-derived account was
    # provisioned. preferred: NULL (the current identity's memory serves) or
    # 'legacy' (an admin restored the legacy one; recorded and audited).
    """
    CREATE TABLE IF NOT EXISTS mcp_cognee_sub_used (
        sub VARCHAR(160) PRIMARY KEY,
        first_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    ALTER TABLE mcp_cognee_accounts ADD COLUMN IF NOT EXISTS preferred VARCHAR(8);

    INSERT INTO schema_version (version) VALUES (8) ON CONFLICT DO NOTHING;
    """,
    # Version 9 (#3429): remote MCP servers by URL with OAuth 2.1 dynamic
    # client registration (app/services/remote_mcp.py).
    # mcp_remote_clients: the box's registration at the provider, ONE per
    # catalogue entry (never per person). `endpoints` holds the discovered
    # endpoints exactly as validated (the only URLs ever dialled for it);
    # `client_secret_enc` is the AES-GCM token of a secret the provider
    # issued (NULL for a public client).
    # mcp_oauth_pending: one row per started authorization, consumed by its
    # callback (single use). `verifier_enc` is the PKCE code_verifier,
    # encrypted; it never leaves the server.
    """
    CREATE TABLE IF NOT EXISTS mcp_remote_clients (
        mcp_id VARCHAR(48) PRIMARY KEY,
        resource TEXT NOT NULL,
        issuer TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        endpoints JSONB NOT NULL,
        client_id VARCHAR(512) NOT NULL,
        client_secret_enc TEXT,
        auth_method VARCHAR(32) NOT NULL,
        scopes TEXT NOT NULL DEFAULT '',
        iss_supported BOOLEAN NOT NULL DEFAULT FALSE,
        registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS mcp_oauth_pending (
        nonce VARCHAR(64) PRIMARY KEY,
        owner_key VARCHAR(160) NOT NULL,
        mcp_id VARCHAR(48) NOT NULL,
        client_id VARCHAR(512) NOT NULL,
        verifier_enc TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_mcp_oauth_pending_expires ON mcp_oauth_pending (expires_at);

    INSERT INTO schema_version (version) VALUES (9) ON CONFLICT DO NOTHING;
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)

#: #2472 identity events in mcp_audit_log. STABLE names: the config portal's
#: D12 audit view reads them (A-lane follow-up); rename only with that reader.
AUDIT_IDENTITY_REKEY = "identity_rekey"
AUDIT_IDENTITY_QUARANTINE = "identity_quarantine"
AUDIT_IDENTITY_QUARANTINE_PURGE = "identity_quarantine_purge"
AUDIT_IDENTITY_UNATTRIBUTED_PURGE = "identity_unattributed_purge"
AUDIT_IDENTITY_COGNEE_ATTRIBUTED = "identity_cognee_attributed"
AUDIT_IDENTITY_COGNEE_RESTORED = "identity_cognee_restored"
AUDIT_IDENTITY_COGNEE_ERASED = "identity_cognee_erased"
#: #2677: a departed person's stored credentials were deleted.
AUDIT_PERSON_CREDENTIALS_PURGED = "person_credentials_purged"
#: #2677: deleting them failed (they stay; the next sweep retries).
AUDIT_PERSON_CREDENTIALS_PURGE_FAILED = "person_credentials_purge_failed"
#: #2677: days a person must stay deactivated before
#: their stored credentials are deleted. A deleted person's go at once.
DEACTIVATED_PURGE_DAYS = 30

#: The catalog ids whose proxies auto-provision a per-user cognee account
#: (`cognee_autoprovision`). Pinned against the catalog by a unit test.
COGNEE_MCP_IDS = ("cognee-personal", "cognee-company")


class Database:
    def __init__(self, database_url: str):
        self._dsn = database_url

    @contextmanager
    def conn(self):
        connection = psycopg2.connect(self._dsn)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def cursor(self):
        with self.conn() as connection:
            cur = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                yield cur
            finally:
                cur.close()

    def migrate(self):
        with self.conn() as conn:
            cur = conn.cursor()
            # #2538: resolve schema_version through THIS connection's
            # search_path, exactly as the unqualified SELECT below and the
            # migrations' CREATE TABLE do. information_schema.tables without a
            # schema filter spans every schema: a schema_version elsewhere made
            # a fresh schema look migrated and the SELECT raised UndefinedTable
            # (the Agent Manager's twin was fixed in 2026.09-ga.4, 92817bfc8).
            cur.execute("SELECT to_regclass('schema_version') IS NOT NULL")
            exists = cur.fetchone()[0]
            current_version = 0
            if exists:
                cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
                current_version = cur.fetchone()[0]
            for i, sql in enumerate(MIGRATIONS, 1):
                if i > current_version:
                    logger.info("Applying mcp-manager migration %d...", i)
                    cur.execute(sql)
                    conn.commit()
            cur.close()
        logger.info("mcp-manager DB at schema version %d", SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # Secrets — encrypted_value only; never plaintext. Keyed by `owner`: a
    # person's OIDC sub, or a technical user's "~svc-<name>" (#2472).
    #
    # LEGACY WINDOW (B6 fix round 1): a row from before migration 4 that is
    # not decided yet (owner_key NULL) is still served by its slug when the
    # caller passes `legacy_slug` — exactly the 2026.09 lookup — so nothing
    # disappears while the re-key waits for the read-only Authentik token.
    # A sub-keyed row always wins over a legacy row of the same cred_type,
    # and every WRITE is sub-keyed: no new slug-keyed row is ever created.
    # Once identity_migration decides a row (re-key or quarantine) the
    # legacy clause no longer matches it.
    # ------------------------------------------------------------------

    def upsert_secret(self, owner: str, mcp_id: str, cred_type: str,
                      encrypted_value: str, expires_at=None, user_slug=None):
        """Insert/replace an encrypted credential. `encrypted_value` MUST be the
        AES-GCM ciphertext token (the caller encrypts before calling this).
        `user_slug` is display only."""
        if not owner:
            raise ValueError("a credential needs an owner (sub or ~svc-<name>)")
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_user_secrets
                    (owner_key, user_slug, mcp_id, cred_type, encrypted_value, expires_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (owner_key, mcp_id, cred_type) WHERE owner_key IS NOT NULL
                DO UPDATE SET
                    encrypted_value = EXCLUDED.encrypted_value,
                    expires_at = EXCLUDED.expires_at,
                    user_slug = COALESCE(EXCLUDED.user_slug, mcp_user_secrets.user_slug),
                    reauth_required_at = NULL,
                    updated_at = NOW()
            """, (owner, user_slug, mcp_id, cred_type, encrypted_value, expires_at))
            if cred_type in ("oauth_access", "oauth_refresh"):
                # A new token is a new grant: the old grant's mark goes with it.
                cur.execute("""
                    UPDATE mcp_user_secrets SET reauth_required_at = NULL
                    WHERE owner_key = %s AND mcp_id = %s AND reauth_required_at IS NOT NULL
                """, (owner, mcp_id))

    def mark_reauth_required(self, owner: str, mcp_id: str) -> int:
        """#2554: the provider rejected this owner's OAuth grant for `mcp_id`.
        Marks the grant's rows (owner-keyed only); returns how many were
        newly marked. The rows stay: the portal shows "authorize again"."""
        if not owner:
            return 0
        with self.cursor() as cur:
            cur.execute("""
                UPDATE mcp_user_secrets SET reauth_required_at = NOW()
                WHERE owner_key = %s AND mcp_id = %s
                  AND cred_type IN ('oauth_access', 'oauth_refresh')
                  AND reauth_required_at IS NULL
            """, (owner, mcp_id))
            return cur.rowcount

    def get_secrets(self, owner: str, mcp_id: str, legacy_slug: str = None):
        """Return all encrypted secret rows for (owner, mcp), sub-keyed rows
        first, then undecided legacy rows under `legacy_slug`. only-own."""
        if not owner and not legacy_slug:
            return []
        with self.cursor() as cur:
            cur.execute("""
                SELECT cred_type, encrypted_value, expires_at, updated_at,
                       reauth_required_at, (owner_key IS NULL) AS legacy
                FROM mcp_user_secrets
                WHERE mcp_id = %s
                  AND (owner_key = %s
                       OR (%s::text IS NOT NULL AND owner_key IS NULL AND user_slug = %s))
                ORDER BY (owner_key IS NULL), id
            """, (mcp_id, owner or None, legacy_slug, legacy_slug))
            return cur.fetchall()

    def get_secret(self, owner: str, mcp_id: str, cred_type: str):
        if not owner:
            return None
        with self.cursor() as cur:
            cur.execute("""
                SELECT cred_type, encrypted_value, expires_at, reauth_required_at
                FROM mcp_user_secrets
                WHERE owner_key = %s AND mcp_id = %s AND cred_type = %s
            """, (owner, mcp_id, cred_type))
            return cur.fetchone()

    def delete_secret(self, owner: str, mcp_id: str, cred_type: str) -> None:
        """Drop ONE owner-keyed credential row (#2472: a technical user's
        cached client-credentials token when its client pair changes)."""
        if not owner:
            return
        with self.cursor() as cur:
            cur.execute("""
                DELETE FROM mcp_user_secrets
                WHERE owner_key = %s AND mcp_id = %s AND cred_type = %s
            """, (owner, mcp_id, cred_type))

    def delete_secrets(self, owner: str, mcp_id: str, legacy_slug: str = None):
        """Revoke: drop every credential for (owner, mcp), and the undecided
        legacy rows the same caller is served under `legacy_slug` (a revoke
        must remove what the user sees). only-own scoped."""
        if not owner and not legacy_slug:
            return
        with self.cursor() as cur:
            cur.execute("""
                DELETE FROM mcp_user_secrets
                WHERE mcp_id = %s
                  AND (owner_key = %s
                       OR (%s::text IS NOT NULL AND owner_key IS NULL AND user_slug = %s))
            """, (mcp_id, owner or None, legacy_slug, legacy_slug))

    def delete_person_secrets(self, owner: str) -> list:
        """#2677: drop EVERY credential row keyed to a person's sub (every
        server, every cred_type). Returns the (mcp_id, cred_type) pairs that
        went — never a value. A technical owner ("~svc-…") and legacy rows
        (owner_key NULL, B6's to decide) are never matched."""
        if not owner or str(owner).startswith("~"):
            raise ValueError("delete_person_secrets needs a person's sub")
        with self.cursor() as cur:
            cur.execute("""
                DELETE FROM mcp_user_secrets WHERE owner_key = %s
                RETURNING mcp_id, cred_type
            """, (owner,))
            return [(r["mcp_id"], r["cred_type"]) for r in cur.fetchall()]

    def person_secret_owners(self) -> list:
        """#2677: every person's sub that may still own a stored credential:
        the owners of credential rows, and every sub the portal remembered
        (mcp_users) — with the OpenBao half on, a person's OAuth grant has no
        Postgres row, and everyone who stored one came through the portal."""
        with self.cursor() as cur:
            cur.execute("""
                SELECT owner_key AS sub FROM mcp_user_secrets
                WHERE owner_key IS NOT NULL AND left(owner_key, 1) <> '~'
                UNION
                SELECT sub FROM mcp_users
                WHERE sub IS NOT NULL AND sub <> '' AND left(sub, 1) <> '~'
                ORDER BY sub
            """)
            return [r["sub"] for r in cur.fetchall()]

    def list_user_mcp_ids_with_secrets(self, owner: str, legacy_slug: str = None):
        if not owner and not legacy_slug:
            return []
        with self.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT mcp_id FROM mcp_user_secrets
                WHERE owner_key = %s
                   OR (%s::text IS NOT NULL AND owner_key IS NULL AND user_slug = %s)
                ORDER BY mcp_id
            """, (owner or None, legacy_slug, legacy_slug))
            return [r["mcp_id"] for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Instances. Owned by `owner_sub`; `user_slug` names the container.
    # ------------------------------------------------------------------

    def create_instance(self, mcp_id: str, user_id: str, user_slug: str,
                        container_name: str, config: dict = None) -> uuid.UUID:
        """`user_id` is the owner's sub (X-Authentik-Uid): it is stored as
        both the historical user_id and the owner_sub key."""
        if not user_id:
            raise ValueError("an instance needs its owner's sub")
        instance_id = uuid.uuid4()
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_instances
                    (id, mcp_id, user_id, owner_sub, user_slug, container_name, state, config)
                VALUES (%s, %s, %s, %s, %s, %s, 'provisioning', %s)
            """, (instance_id, mcp_id, user_id, user_id, user_slug, container_name,
                  psycopg2.extras.Json(config or {})))
        return instance_id

    def reset_instance_for_relaunch(self, instance_id: uuid.UUID,
                                    container_name: str, config: dict = None):
        """Re-arm an EXISTING instance row for a fresh launch (#66).

        Relaunching an integration whose instance was previously *stopped* (or
        left in *error* / *provisioning*) must REUSE its row, not INSERT a new
        one — a second INSERT for the same (mcp_id, user_slug) collides with the
        partial-unique index ``idx_mcp_instance_user`` (WHERE state !=
        'destroyed') and raised a 500. We flip the row back to 'provisioning',
        refresh the (possibly rotated) route secret carried in ``config``, and
        clear the prior container id / error so the launch path treats it as a
        clean start. Scoped by id (the caller has already checked only-own)."""
        with self.cursor() as cur:
            cur.execute("""
                UPDATE mcp_instances
                SET state = 'provisioning',
                    container_name = %s,
                    config = %s,
                    container_id = NULL,
                    error_message = NULL,
                    stopped_at = NULL
                WHERE id = %s
            """, (container_name, psycopg2.extras.Json(config or {}), instance_id))

    def set_instance_config(self, instance_id: uuid.UUID, config: dict):
        """Replace a live instance's config (the upgrade roll arms the cognee
        token renewal of an instance started before it existed)."""
        with self.cursor() as cur:
            cur.execute("UPDATE mcp_instances SET config = %s WHERE id = %s AND state != 'destroyed'",
                        (psycopg2.extras.Json(config or {}), instance_id))

    def get_instance(self, instance_id: uuid.UUID):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM mcp_instances WHERE id = %s", (instance_id,))
            return cur.fetchone()

    def get_instance_by_mcp_and_user(self, mcp_id: str, owner_sub: str,
                                     legacy_slug: str = None):
        """The caller's live instance: the sub-keyed one, else (legacy window)
        the undecided pre-migration one under `legacy_slug`."""
        if not owner_sub and not legacy_slug:
            return None
        with self.cursor() as cur:
            cur.execute("""
                SELECT * FROM mcp_instances
                WHERE mcp_id = %s AND state != 'destroyed'
                  AND (owner_sub = %s
                       OR (%s::text IS NOT NULL AND owner_sub IS NULL AND user_slug = %s))
                ORDER BY (owner_sub IS NULL) LIMIT 1
            """, (mcp_id, owner_sub or None, legacy_slug, legacy_slug))
            return cur.fetchone()

    def get_live_instance_by_slug(self, mcp_id: str, user_slug: str):
        """The live instance holding (mcp_id, user_slug) — the container name —
        whoever owns it, attributed or not. Used only to refuse a launch that
        would collide with someone else's container, never for ownership."""
        with self.cursor() as cur:
            cur.execute("""
                SELECT id, owner_sub, state FROM mcp_instances
                WHERE mcp_id = %s AND user_slug = %s AND state != 'destroyed'
            """, (mcp_id, user_slug))
            return cur.fetchone()

    def get_user_instances(self, owner_sub: str, legacy_slug: str = None):
        if not owner_sub and not legacy_slug:
            return []
        with self.cursor() as cur:
            cur.execute("""
                SELECT * FROM mcp_instances
                WHERE state != 'destroyed'
                  AND (owner_sub = %s
                       OR (%s::text IS NOT NULL AND owner_sub IS NULL AND user_slug = %s))
                ORDER BY created_at
            """, (owner_sub or None, legacy_slug, legacy_slug))
            return cur.fetchall()

    def owner_subs_for_slug(self, user_slug: str) -> list:
        """The distinct owners of the attributed live instances named by
        `user_slug` (the agent-wiring bridge until B8 keys it by sub)."""
        with self.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT owner_sub FROM mcp_instances
                WHERE user_slug = %s AND state != 'destroyed' AND owner_sub IS NOT NULL
            """, (user_slug,))
            return [r["owner_sub"] for r in cur.fetchall()]

    def get_all_instances(self):
        with self.cursor() as cur:
            cur.execute("""
                SELECT * FROM mcp_instances WHERE state != 'destroyed'
                ORDER BY user_slug, mcp_id
            """)
            return cur.fetchall()

    def list_active_instances_by_mcp(self, mcp_id: str):
        with self.cursor() as cur:
            cur.execute("""
                SELECT * FROM mcp_instances
                WHERE mcp_id = %s AND state != 'destroyed'
            """, (mcp_id,))
            return cur.fetchall()

    def list_active_instances_all(self):
        with self.cursor() as cur:
            cur.execute("""
                SELECT id, mcp_id FROM mcp_instances WHERE state != 'destroyed'
            """)
            return cur.fetchall()

    def update_instance_state(self, instance_id: uuid.UUID, state: str,
                              container_id: str = None, error_message: str = None):
        now = datetime.now(timezone.utc)
        with self.cursor() as cur:
            updates = ["state = %s"]
            params = [state]
            if container_id is not None:
                updates.append("container_id = %s")
                params.append(container_id)
            if error_message is not None:
                updates.append("error_message = %s")
                params.append(error_message)
            if state == 'running':
                updates.append("started_at = %s")
                params.append(now)
                updates.append("error_message = NULL")
            elif state == 'stopped':
                updates.append("stopped_at = %s")
                params.append(now)
            params.append(instance_id)
            cur.execute(
                f"UPDATE mcp_instances SET {', '.join(updates)} WHERE id = %s",
                params,
            )

    def update_last_accessed(self, instance_id: uuid.UUID):
        with self.cursor() as cur:
            cur.execute(
                "UPDATE mcp_instances SET last_accessed = NOW() WHERE id = %s",
                (instance_id,),
            )

    def delete_instance(self, instance_id: uuid.UUID):
        with self.cursor() as cur:
            cur.execute(
                "UPDATE mcp_instances SET state = 'destroyed' WHERE id = %s",
                (instance_id,),
            )

    # ------------------------------------------------------------------
    # Type bindings — keyed by the owner's sub.
    # ------------------------------------------------------------------

    def set_bindings(self, owner_sub: str, mcp_id: str, consumers: list,
                     user_slug: str = None):
        """Replace the consumer bindings for (owner, mcp)."""
        if not owner_sub:
            raise ValueError("a binding needs its owner's sub")
        with self.cursor() as cur:
            cur.execute(
                "DELETE FROM mcp_type_bindings WHERE owner_sub = %s AND mcp_id = %s",
                (owner_sub, mcp_id),
            )
            for consumer in consumers:
                cur.execute("""
                    INSERT INTO mcp_type_bindings (owner_sub, user_slug, mcp_id, consumer)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (owner_sub, mcp_id, consumer) WHERE owner_sub IS NOT NULL
                    DO NOTHING
                """, (owner_sub, user_slug, mcp_id, consumer))

    def get_bindings(self, owner_sub: str, legacy_slug: str = None):
        if not owner_sub and not legacy_slug:
            return []
        with self.cursor() as cur:
            cur.execute("""
                SELECT mcp_id, consumer FROM mcp_type_bindings
                WHERE owner_sub = %s
                   OR (%s::text IS NOT NULL AND owner_sub IS NULL AND user_slug = %s)
                ORDER BY (owner_sub IS NULL), id
            """, (owner_sub or None, legacy_slug, legacy_slug))
            return cur.fetchall()

    # ------------------------------------------------------------------
    # Integration governance (#36 follow-up) — Config-UI-managed availability +
    # per-integration tier gate. DB-backed (not .env).
    # ------------------------------------------------------------------

    def get_all_governance(self) -> dict:
        """Return {mcp_id: {available: bool, min_tier: str}} for every row set."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT mcp_id, available, min_tier FROM mcp_integration_governance"
            )
            return {r["mcp_id"]: {"available": bool(r["available"]),
                                  "min_tier": r["min_tier"]}
                    for r in cur.fetchall()}

    def get_governance(self, mcp_id: str):
        with self.cursor() as cur:
            cur.execute(
                "SELECT mcp_id, available, min_tier, config "
                "FROM mcp_integration_governance WHERE mcp_id = %s", (mcp_id,))
            return cur.fetchone()

    def set_governance_config(self, mcp_id: str, config: dict):
        """Merge extra per-integration governance config (e.g. company_dataset).
        Creates the row with defaults if absent."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_integration_governance (mcp_id, config, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (mcp_id) DO UPDATE SET
                    config = COALESCE(mcp_integration_governance.config, '{}'::jsonb)
                             || EXCLUDED.config,
                    updated_at = NOW()
            """, (mcp_id, psycopg2.extras.Json(config or {})))

    def set_governance(self, mcp_id: str, available: bool, min_tier: str):
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_integration_governance (mcp_id, available, min_tier, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (mcp_id) DO UPDATE SET
                    available = EXCLUDED.available,
                    min_tier = EXCLUDED.min_tier,
                    updated_at = NOW()
            """, (mcp_id, bool(available), min_tier))

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def log_audit(self, user_slug: str, action: str, mcp_id: str = None,
                  details: dict = None):
        """Audit log. Callers MUST NOT put plaintext credentials in `details`."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_audit_log (user_slug, action, mcp_id, details)
                VALUES (%s, %s, %s, %s)
            """, (user_slug, action, mcp_id, psycopg2.extras.Json(details or {})))

    def get_audit_log(self, limit=100, user_slug: str = None):
        with self.cursor() as cur:
            if user_slug:
                cur.execute("""
                    SELECT * FROM mcp_audit_log WHERE user_slug = %s
                    ORDER BY timestamp DESC LIMIT %s
                """, (user_slug, limit))
            else:
                cur.execute(
                    "SELECT * FROM mcp_audit_log ORDER BY timestamp DESC LIMIT %s",
                    (limit,),
                )
            return cur.fetchall()

    # ------------------------------------------------------------------
    # #2472 gateway identities: the SSO user directory
    # ------------------------------------------------------------------

    def upsert_user(self, user_slug: str, username: str, email, sub=None):
        """Remember an SSO user. A sub (X-Authentik-Uid, A1 D8) belongs to one
        slug: a renamed user's old slug gives it up, so a later lookup by sub
        finds the current one. A request without the header passes sub=None
        and keeps whatever sub the row already has."""
        sub = (sub or "").strip() or None
        with self.cursor() as cur:
            if sub:
                cur.execute("UPDATE mcp_users SET sub = NULL WHERE sub = %s AND user_slug <> %s",
                            (sub, user_slug))
            cur.execute("""
                INSERT INTO mcp_users (user_slug, username, email, sub, last_seen)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (user_slug) DO UPDATE SET
                    username = EXCLUDED.username, email = EXCLUDED.email,
                    sub = COALESCE(EXCLUDED.sub, mcp_users.sub), last_seen = NOW()
            """, (user_slug, username, (email or "").strip() or None, sub))

    def get_user_sub(self, user_slug: str):
        with self.cursor() as cur:
            cur.execute("SELECT sub FROM mcp_users WHERE user_slug = %s", (user_slug,))
            row = cur.fetchone()
        return (row or {}).get("sub") or None

    def get_user_slug_by_sub(self, sub: str):
        if not (sub or "").strip():
            return None
        with self.cursor() as cur:
            cur.execute("SELECT user_slug FROM mcp_users WHERE sub = %s", (sub.strip(),))
            row = cur.fetchone()
        return row["user_slug"] if row else None

    def get_user_slug_by_email(self, email: str):
        """The ONE slug with this email, or None (unknown OR ambiguous). Display
        only: no identity decision rests on it (the Open WebUI path asks
        Authentik, never this table — design record §2.3)."""
        if not (email or "").strip():
            return None
        with self.cursor() as cur:
            cur.execute("SELECT user_slug FROM mcp_users WHERE lower(email) = lower(%s) LIMIT 2",
                        (email.strip(),))
            rows = cur.fetchall()
        return rows[0]["user_slug"] if len(rows) == 1 else None

    # ------------------------------------------------------------------
    # #2472 technical users (the mirror of A17's service accounts)
    # ------------------------------------------------------------------

    def upsert_technical_user(self, name, owner_sub, authentik_pk, bindings, is_active,
                              owner_username="", active_since=None) -> dict:
        """Mirror one A17 service account; the owner is kept by sub (A17's
        rzfz_owner), owner_username is for display only. is_active=False
        revokes (tokens, vaulted upstream credentials); a later is_active=True
        does NOT un-revoke tokens — a re-activated technical user gets new ones.

        The revoked state wins (#2472 review): a revoked row becomes live again
        only when `active_since` (when Authentik made this account active: its
        creation, or a reassign that re-enabled it) is LATER than the
        revocation. A push built from a listing read before the revoke carries
        an older `active_since` (or none) and leaves the row revoked."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_technical_users (name, authentik_pk, owner_sub, owner_username, bindings, synced_at)
                VALUES (%(name)s, %(pk)s, %(sub)s, %(display)s, %(bindings)s, NOW())
                ON CONFLICT (name) DO UPDATE SET
                    authentik_pk = EXCLUDED.authentik_pk, owner_sub = EXCLUDED.owner_sub,
                    owner_username = EXCLUDED.owner_username,
                    bindings = EXCLUDED.bindings, synced_at = NOW(),
                    revoked_at = CASE
                        WHEN %(active)s AND mcp_technical_users.revoked_at IS NOT NULL
                             AND %(since)s::timestamptz IS NOT NULL
                             AND %(since)s::timestamptz > mcp_technical_users.revoked_at THEN NULL
                        ELSE mcp_technical_users.revoked_at END
                RETURNING *
            """, {"name": name, "pk": authentik_pk, "sub": owner_sub, "display": owner_username or "",
                  "bindings": psycopg2.extras.Json(list(bindings or [])), "active": bool(is_active),
                  "since": active_since})
            row = cur.fetchone()
        if not is_active:
            self.revoke_technical_user(name)
            row = self.get_technical_user(name)
        return row

    def get_technical_user(self, name: str):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM mcp_technical_users WHERE name = %s", (name,))
            return cur.fetchone()

    def list_technical_users(self) -> list:
        with self.cursor() as cur:
            cur.execute("""
                SELECT u.*, (SELECT max(t.last_used_at) FROM mcp_gateway_tokens t
                             WHERE t.principal = '~' || u.name) AS last_used_at
                FROM mcp_technical_users u ORDER BY u.name
            """)
            return cur.fetchall()

    def revoke_technical_user(self, name: str) -> bool:
        """Idempotent. True iff THIS call revoked it. A name the mirror never saw
        gets a revoked row, so no token can ever be resolved for it. Revoking
        also revokes its gateway tokens and deletes its vaulted upstream
        credentials, in the same transaction."""
        with self.cursor() as cur:
            cur.execute("SELECT revoked_at FROM mcp_technical_users WHERE name = %s FOR UPDATE",
                        (name,))
            row = cur.fetchone()
            if row is None:
                cur.execute("""INSERT INTO mcp_technical_users (name, owner_sub, revoked_at)
                               VALUES (%s, '', NOW())""", (name,))
                changed = True
            elif row["revoked_at"] is None:
                cur.execute("UPDATE mcp_technical_users SET revoked_at = NOW() WHERE name = %s", (name,))
                changed = True
            else:
                changed = False
            cur.execute("""UPDATE mcp_gateway_tokens SET revoked_at = NOW()
                           WHERE principal = %s AND revoked_at IS NULL""", ("~" + name,))
            cur.execute("DELETE FROM mcp_user_secrets WHERE owner_key = %s", ("~" + name,))
            return changed

    GITEA_STATES = ("live", "owed", "revoked", None)
    _KEEP = object()

    def set_gitea_state(self, name: str, state, gitea_user_id=_KEEP, authentik_pk=_KEEP) -> None:
        """#2554: 'live' (a Gitea service user may hold tokens), 'owed' (its
        revoke is due), 'revoked' (Gitea confirmed) or None; and, when given,
        the Gitea user id and the Authentik pk it was made for (None clears)."""
        if state not in self.GITEA_STATES:
            raise ValueError("gitea_state must be 'live', 'owed', 'revoked' or None")
        sets, args = ["gitea_state = %s"], [state]
        for col, val in (("gitea_user_id", gitea_user_id), ("gitea_authentik_pk", authentik_pk)):
            if val is not self._KEEP:
                if val is not None and (not isinstance(val, int) or isinstance(val, bool)):
                    raise ValueError(f"{col} must be an integer or None")
                sets.append(f"{col} = %s")
                args.append(val)
        with self.cursor() as cur:
            cur.execute(f"UPDATE mcp_technical_users SET {', '.join(sets)} WHERE name = %s", (*args, name))

    def drop_technical_credentials(self, name: str) -> None:
        """Revoke every gateway token and delete every vaulted upstream
        credential of a technical user, WITHOUT marking it revoked: the name
        now belongs to another Authentik account (a different pk), which must
        not inherit what the old one was given. A Gitea service user the old
        account had becomes a revoke OWED (#2554), in the same transaction:
        the new account's row is live, so 'live' alone would never be revoked."""
        with self.cursor() as cur:
            cur.execute("""UPDATE mcp_gateway_tokens SET revoked_at = NOW()
                           WHERE principal = %s AND revoked_at IS NULL""", ("~" + name,))
            cur.execute("DELETE FROM mcp_user_secrets WHERE owner_key = %s", ("~" + name,))
            cur.execute("""UPDATE mcp_technical_users SET gitea_state = 'owed'
                           WHERE name = %s AND gitea_state = 'live'""", (name,))

    def technical_bound(self, name: str, server_id: str) -> bool:
        """True iff the technical user is live and carries
        `mcp-integration:<server_id>` (A1 D7's binding kind)."""
        with self.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM mcp_technical_users
                WHERE name = %s AND revoked_at IS NULL AND bindings ? %s
            """, (name, f"mcp-integration:{server_id}"))
            return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # #2472 gateway caller tokens — SHA-256 only
    # ------------------------------------------------------------------

    def insert_gateway_token(self, token_hash, principal, kind, consumer):
        with self.cursor() as cur:
            cur.execute("""INSERT INTO mcp_gateway_tokens (token_hash, principal, kind, consumer)
                           VALUES (%s, %s, %s, %s)""", (token_hash, principal, kind, consumer))

    def get_gateway_token(self, token_hash: str):
        with self.cursor() as cur:
            cur.execute("""SELECT id, principal, kind, consumer FROM mcp_gateway_tokens
                           WHERE token_hash = %s AND revoked_at IS NULL""", (token_hash,))
            return cur.fetchone()

    def get_rejected_gateway_token(self, token_hash: str):
        """#2472: a token this gateway issued, looked up WITH its revocation
        (get_gateway_token sees live tokens only) — so a rejected call can be
        audited under its principal. Never returns the hash."""
        with self.cursor() as cur:
            cur.execute("""SELECT id, principal, kind, consumer, revoked_at FROM mcp_gateway_tokens
                           WHERE token_hash = %s""", (token_hash,))
            return cur.fetchone()

    def touch_gateway_token(self, token_id):
        with self.cursor() as cur:
            cur.execute("UPDATE mcp_gateway_tokens SET last_used_at = NOW() WHERE id = %s", (token_id,))

    def revoke_gateway_tokens(self, principal: str, consumer=None) -> int:
        with self.cursor() as cur:
            if consumer:
                cur.execute("""UPDATE mcp_gateway_tokens SET revoked_at = NOW()
                               WHERE principal = %s AND consumer = %s AND revoked_at IS NULL""",
                            (principal, consumer))
            else:
                cur.execute("""UPDATE mcp_gateway_tokens SET revoked_at = NOW()
                               WHERE principal = %s AND revoked_at IS NULL""", (principal,))
            return cur.rowcount

    # ------------------------------------------------------------------
    # #2086 Docling document keys, bound per caller (design record §7, I2)
    # ------------------------------------------------------------------

    def bind_doc_key(self, server_id: str, caller_key: str, doc_key: str) -> None:
        with self.cursor() as cur:
            cur.execute("""INSERT INTO mcp_gateway_doc_keys (server_id, caller_key, doc_key)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (server_id, caller_key, doc_key)
                           DO UPDATE SET bound_at = NOW()""",
                        (server_id, caller_key, doc_key))

    def doc_key_bound(self, server_id: str, caller_key: str, doc_key: str) -> bool:
        if not caller_key or not doc_key:
            return False
        with self.cursor() as cur:
            cur.execute("""SELECT 1 FROM mcp_gateway_doc_keys
                           WHERE server_id = %s AND caller_key = %s AND doc_key = %s""",
                        (server_id, caller_key, doc_key))
            return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # #2472 sub re-key of pre-migration rows, and their quarantine
    # (design record §2.3). Every write is conditional on the row still
    # being unattributed, so two workers never both decide one row.
    # ------------------------------------------------------------------

    _REKEY = {
        # table -> (key column, id column)
        "mcp_instances": ("owner_sub", "id"),
        "mcp_type_bindings": ("owner_sub", "id"),
        "mcp_user_secrets": ("owner_key", "id"),
    }

    def pending_instances(self) -> list:
        with self.cursor() as cur:
            cur.execute("""SELECT id, mcp_id, user_id, user_slug, container_name, created_at
                           FROM mcp_instances
                           WHERE owner_sub IS NULL AND state != 'destroyed'
                           ORDER BY created_at""")
            return cur.fetchall()

    def pending_bindings(self) -> list:
        with self.cursor() as cur:
            cur.execute("""SELECT id, user_slug, mcp_id, consumer, created_at
                           FROM mcp_type_bindings WHERE owner_sub IS NULL ORDER BY id""")
            return cur.fetchall()

    def pending_secrets_for_slug(self, user_slug: str) -> list:
        """Unattributed credential rows under a slug. NO value column."""
        with self.cursor() as cur:
            cur.execute("""SELECT id, user_slug, mcp_id, cred_type, created_at
                           FROM mcp_user_secrets
                           WHERE owner_key IS NULL AND user_slug = %s ORDER BY id""",
                        (user_slug,))
            return cur.fetchall()

    def rekeyed_instance_for(self, user_slug: str, mcp_id: str):
        """The attributed live instance under (user_slug, mcp_id), if any."""
        with self.cursor() as cur:
            cur.execute("""SELECT id, owner_sub FROM mcp_instances
                           WHERE user_slug = %s AND mcp_id = %s AND owner_sub IS NOT NULL
                             AND state != 'destroyed'""", (user_slug, mcp_id))
            return cur.fetchone()

    def has_pending_instance_for(self, user_slug: str, mcp_id: str) -> bool:
        with self.cursor() as cur:
            cur.execute("""SELECT 1 FROM mcp_instances
                           WHERE user_slug = %s AND mcp_id = %s AND owner_sub IS NULL
                             AND state != 'destroyed'""", (user_slug, mcp_id))
            return cur.fetchone() is not None

    def instance_was_quarantined(self, user_slug: str, mcp_id: str) -> bool:
        with self.cursor() as cur:
            cur.execute("""SELECT 1 FROM mcp_identity_quarantine
                           WHERE source_table = 'mcp_instances' AND user_slug = %s AND mcp_id = %s""",
                        (user_slug, mcp_id))
            return cur.fetchone() is not None

    def rekey_row(self, table: str, row_id, sub: str):
        """Attribute one unattributed row to `sub`. True when done; False when
        the row is gone or already decided (another worker was first); None
        when `sub` already holds a row with the same unique key (the owner
        reconnected after the upgrade — the old row is then superseded)."""
        col, idcol = self._REKEY[table]
        if not sub:
            raise ValueError("re-key needs a sub")
        try:
            return self._rekey_row(table, col, idcol, row_id, sub)
        except psycopg2.errors.UniqueViolation:
            return None

    def _rekey_row(self, table, col, idcol, row_id, sub) -> bool:
        with self.cursor() as cur:
            cur.execute(f"UPDATE {table} SET {col} = %s WHERE {idcol} = %s AND {col} IS NULL",
                        (sub, row_id))
            changed = cur.rowcount == 1
            if changed:
                cur.execute("""INSERT INTO mcp_audit_log (user_slug, action, mcp_id, details)
                               SELECT %s, %s, mcp_id, %s FROM """ + table +
                            f" WHERE {idcol} = %s",
                            (sub, AUDIT_IDENTITY_REKEY,
                             psycopg2.extras.Json({"table": table, "row": str(row_id)}), row_id))
            return changed

    def quarantine_row(self, table: str, row_id, reason: str):
        """Move one unattributed row out of `table` into the quarantine, in
        one transaction, and audit it. Returns the moved row (without the
        instance route secret) or None when it was already decided. A
        credential's encrypted_value moves as the ciphertext it is."""
        col, idcol = self._REKEY[table]
        with self.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE {idcol} = %s AND {col} IS NULL RETURNING *",
                        (row_id,))
            row = cur.fetchone()
            if row is None:
                return None
            data = {k: (v.isoformat() if hasattr(v, "isoformat") else
                        (str(v) if isinstance(v, uuid.UUID) else v))
                    for k, v in dict(row).items()}
            if isinstance(data.get("config"), dict):
                data["config"] = {k: v for k, v in data["config"].items()
                                  if k not in ("_route_secret", "_gateway_secret")}
            cur.execute("""
                INSERT INTO mcp_identity_quarantine
                    (source_table, source_id, user_slug, mcp_id, reason, row_data)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (table, str(row_id), data.get("user_slug"), data.get("mcp_id"), reason,
                  psycopg2.extras.Json(data)))
            cur.execute("""INSERT INTO mcp_audit_log (user_slug, action, mcp_id, details)
                           VALUES (%s, %s, %s, %s)""",
                        (data.get("user_slug"), AUDIT_IDENTITY_QUARANTINE, data.get("mcp_id"),
                         psycopg2.extras.Json({"table": table, "row": str(row_id), "reason": reason})))
            return data

    def mark_instances_rekeyed(self):
        with self.cursor() as cur:
            cur.execute("""UPDATE mcp_identity_migration SET instances_done_at = NOW()
                           WHERE id = 1 AND instances_done_at IS NULL""")

    def record_attribution(self, ok: bool, error: str = ""):
        """Remember the last attribution attempt's outcome for `rzfz status`.
        `error` is an error CLASS / short reason, never a value."""
        with self.cursor() as cur:
            if ok:
                cur.execute("UPDATE mcp_identity_migration SET attribution_ok_at = NOW() WHERE id = 1")
            else:
                cur.execute("""UPDATE mcp_identity_migration
                               SET attribution_error_at = NOW(), attribution_error = %s
                               WHERE id = 1""", ((error or "error")[:160],))

    def mark_person_deactivated(self, sub: str) -> bool:
        """#2677: start (or keep) a person's deactivation clock. Returns True
        once they have been deactivated for DEACTIVATED_PURGE_DAYS without a
        break; the first sighting is never overwritten while it lasts."""
        with self.cursor() as cur:
            cur.execute("""INSERT INTO mcp_person_deactivated (sub) VALUES (%s)
                           ON CONFLICT (sub) DO NOTHING""", (sub,))
            cur.execute("""SELECT first_seen_at <= NOW() - make_interval(days => %s) AS due
                           FROM mcp_person_deactivated WHERE sub = %s""",
                        (DEACTIVATED_PURGE_DAYS, sub))
            row = cur.fetchone()
        return bool(row and row["due"])

    def deactivated_persons(self) -> list:
        with self.cursor() as cur:
            cur.execute("SELECT sub FROM mcp_person_deactivated ORDER BY sub")
            return [r["sub"] for r in cur.fetchall()]

    def clear_person_deactivated(self, subs) -> int:
        """#2677: the clock stops — the person is active again, or their
        credentials are gone."""
        subs = [s for s in (subs or []) if s]
        if not subs:
            return 0
        with self.cursor() as cur:
            cur.execute("DELETE FROM mcp_person_deactivated WHERE sub = ANY(%s)", (subs,))
            return cur.rowcount

    def record_departed_sweep(self, ok: bool, error: str = ""):
        """#2677: the last departed-person sweep's outcome for `rzfz status`.
        `error` is a short reason, never a value."""
        with self.cursor() as cur:
            if ok:
                cur.execute("UPDATE mcp_identity_migration SET departed_sweep_ok_at = NOW() WHERE id = 1")
            else:
                cur.execute("""UPDATE mcp_identity_migration
                               SET departed_sweep_error_at = NOW(), departed_sweep_error = %s
                               WHERE id = 1""", ((error or "error")[:160],))

    def instances_rekey_done(self) -> bool:
        with self.cursor() as cur:
            cur.execute("SELECT instances_done_at FROM mcp_identity_migration WHERE id = 1")
            row = cur.fetchone()
        return bool(row and row["instances_done_at"])

    def quarantine_notices_for_slug(self, user_slug: str) -> list:
        """The integrations whose pre-upgrade rows under this slug sit in
        quarantine. Ids only: the notice shows no content and no other name."""
        with self.cursor() as cur:
            cur.execute("""SELECT DISTINCT mcp_id FROM mcp_identity_quarantine
                           WHERE user_slug = %s AND mcp_id IS NOT NULL ORDER BY mcp_id""",
                        (user_slug,))
            return [r["mcp_id"] for r in cur.fetchall()]

    def list_quarantine(self) -> list:
        """The admin's list. Metadata only — never row_data (which holds a
        credential's ciphertext)."""
        with self.cursor() as cur:
            cur.execute("""SELECT id, source_table, source_id, user_slug, mcp_id, reason,
                                  row_data->>'created_at' AS created_at,
                                  row_data->>'cred_type' AS cred_type, quarantined_at
                           FROM mcp_identity_quarantine ORDER BY quarantined_at, id""")
            return cur.fetchall()

    def list_unattributed_credentials(self, older_than_days: int = 30) -> list:
        """Credential rows still unattributed `older_than_days` after the
        instance run COMPLETED ("unattributed after 30 days"). Before it has
        completed nothing is listed: those rows are still in use through the
        legacy window, not abandoned. Metadata only."""
        with self.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.user_slug, s.mcp_id, s.cred_type, s.created_at
                FROM mcp_user_secrets s, mcp_identity_migration m
                WHERE s.owner_key IS NULL AND m.id = 1
                  AND m.instances_done_at IS NOT NULL
                  AND m.instances_done_at <= NOW() - make_interval(days => %s)
                ORDER BY s.id
            """, (int(older_than_days),))
            return cur.fetchall()

    def purge_quarantined(self, quarantine_id: int, admin: str) -> bool:
        """The ONLY way a quarantined row leaves: an explicit admin action,
        audited in the same transaction. Never called by a timer."""
        with self.cursor() as cur:
            cur.execute("""DELETE FROM mcp_identity_quarantine WHERE id = %s
                           RETURNING source_table, source_id, user_slug, mcp_id, reason""",
                        (quarantine_id,))
            row = cur.fetchone()
            if row is None:
                return False
            cur.execute("""INSERT INTO mcp_audit_log (user_slug, action, mcp_id, details)
                           VALUES (%s, %s, %s, %s)""",
                        (admin, AUDIT_IDENTITY_QUARANTINE_PURGE, row["mcp_id"], psycopg2.extras.Json(
                            {"table": row["source_table"], "row": row["source_id"],
                             "former_slug": row["user_slug"], "reason": row["reason"]})))
            return True

    def purge_unattributed_credential(self, secret_id: int, admin: str,
                                      older_than_days: int = 30) -> bool:
        """Admin purge of one credential row listed as unattributed after 30
        days. Refused (False) for a row that is attributed or not yet listed."""
        with self.cursor() as cur:
            cur.execute("""
                DELETE FROM mcp_user_secrets s USING mcp_identity_migration m
                WHERE s.id = %s AND s.owner_key IS NULL AND m.id = 1
                  AND m.instances_done_at IS NOT NULL
                  AND m.instances_done_at <= NOW() - make_interval(days => %s)
                RETURNING s.user_slug, s.mcp_id, s.cred_type
            """, (secret_id, int(older_than_days)))
            row = cur.fetchone()
            if row is None:
                return False
            cur.execute("""INSERT INTO mcp_audit_log (user_slug, action, mcp_id, details)
                           VALUES (%s, %s, %s, %s)""",
                        (admin, AUDIT_IDENTITY_UNATTRIBUTED_PURGE, row["mcp_id"], psycopg2.extras.Json(
                            {"row": str(secret_id), "former_slug": row["user_slug"],
                             "cred_type": row["cred_type"]})))
            return True


    # ------------------------------------------------------------------
    # #2572: legacy (slug-derived) cognee accounts -> one sub, or quarantine
    # ------------------------------------------------------------------

    def pending_cognee_accounts(self) -> list:
        with self.cursor() as cur:
            cur.execute("SELECT legacy_slug FROM mcp_cognee_accounts WHERE state = 'pending' "
                        "ORDER BY legacy_slug")
            return [r["legacy_slug"] for r in cur.fetchall()]

    def cognee_evidence(self, legacy_slug: str) -> list:
        """Every instance row (any state, destroyed included) of a cognee tier
        under the slug: who provisioned it (user_id) and when."""
        with self.cursor() as cur:
            cur.execute("""SELECT user_id, created_at FROM mcp_instances
                           WHERE user_slug = %s AND mcp_id = ANY(%s)""",
                        (legacy_slug, list(COGNEE_MCP_IDS)))
            return cur.fetchall()

    def decide_cognee_account(self, legacy_slug: str, sub, reason: str = ""):
        """Attribute (sub given) or quarantine (sub None) one pending legacy
        cognee account, audited, in one transaction. True when done, False
        when already decided (another worker was first), None when `sub`
        already holds a mapped account (unique owner)."""
        try:
            return self._decide_cognee_account(legacy_slug, sub, reason)
        except psycopg2.errors.UniqueViolation:
            return None

    def _decide_cognee_account(self, legacy_slug, sub, reason) -> bool:
        with self.cursor() as cur:
            cur.execute("""UPDATE mcp_cognee_accounts
                           SET state = %s, owner_sub = %s, reason = %s, decided_at = NOW()
                           WHERE legacy_slug = %s AND state = 'pending'""",
                        ("attributed" if sub else "quarantined", sub or None,
                         reason or None, legacy_slug))
            if cur.rowcount != 1:
                return False
            if sub:
                cur.execute("""INSERT INTO mcp_audit_log (user_slug, action, mcp_id, details)
                               VALUES (%s, %s, 'cognee-personal', %s)""",
                            (sub, AUDIT_IDENTITY_COGNEE_ATTRIBUTED,
                             psycopg2.extras.Json({"legacy_slug": legacy_slug})))
            else:
                data = {"legacy_slug": legacy_slug, "kind": "cognee account"}
                cur.execute("""
                    INSERT INTO mcp_identity_quarantine
                        (source_table, source_id, user_slug, mcp_id, reason, row_data)
                    VALUES ('mcp_cognee_accounts', %s, %s, 'cognee-personal', %s, %s)
                """, (legacy_slug, legacy_slug, reason, psycopg2.extras.Json(data)))
                cur.execute("""INSERT INTO mcp_audit_log (user_slug, action, mcp_id, details)
                               VALUES (%s, %s, 'cognee-personal', %s)""",
                            (legacy_slug, AUDIT_IDENTITY_QUARANTINE, psycopg2.extras.Json(
                                {"table": "mcp_cognee_accounts", "row": legacy_slug,
                                 "reason": reason})))
            return True

    def cognee_account_for_sub(self, sub: str):
        """The legacy slug whose cognee account SERVES `sub`, or None.

        A confirmed attribution maps a legacy account to `sub`, but when the
        person already has a memory under their current identity (sub-derived
        account provisioned, #2574 item 2) that one wins and the legacy
        account is kept, not served, unless an admin restored it."""
        if not sub:
            return None
        with self.cursor() as cur:
            cur.execute("""SELECT a.legacy_slug FROM mcp_cognee_accounts a
                           WHERE a.owner_sub = %s AND a.state = 'attributed'
                             AND (a.preferred = 'legacy' OR NOT EXISTS
                                  (SELECT 1 FROM mcp_cognee_sub_used u WHERE u.sub = a.owner_sub))""",
                        (sub,))
            row = cur.fetchone()
        return row["legacy_slug"] if row else None

    def note_cognee_sub_used(self, sub: str) -> None:
        """The person's sub-derived cognee account was provisioned."""
        if sub:
            with self.cursor() as cur:
                cur.execute("""INSERT INTO mcp_cognee_sub_used (sub) VALUES (%s)
                               ON CONFLICT DO NOTHING""", (sub,))

    def cognee_dual_memories(self) -> list:
        """People with two own memories: a legacy account mapped to a sub whose
        sub-derived account was also used. Metadata only. `serving` says which
        one the manager hands out now ('current' or 'legacy')."""
        with self.cursor() as cur:
            cur.execute("""SELECT a.legacy_slug, a.owner_sub, a.decided_at,
                                  u.first_used_at,
                                  CASE WHEN a.preferred = 'legacy' THEN 'legacy' ELSE 'current' END AS serving
                           FROM mcp_cognee_accounts a
                           JOIN mcp_cognee_sub_used u ON u.sub = a.owner_sub
                           WHERE a.state = 'attributed' ORDER BY a.legacy_slug""")
            return cur.fetchall()

    def restore_cognee_legacy(self, legacy_slug: str, admin: str) -> bool:
        """Admin: serve the legacy memory again instead of the current one.
        Nothing is deleted; the other memory stays in cognee. Audited in the
        same transaction. False when the slug is not a listed dual memory."""
        with self.cursor() as cur:
            cur.execute("""UPDATE mcp_cognee_accounts a SET preferred = 'legacy'
                           WHERE a.legacy_slug = %s AND a.state = 'attributed'
                             AND a.preferred IS DISTINCT FROM 'legacy'
                             AND EXISTS (SELECT 1 FROM mcp_cognee_sub_used u WHERE u.sub = a.owner_sub)
                           RETURNING a.owner_sub""", (legacy_slug,))
            row = cur.fetchone()
            if row is None:
                return False
            cur.execute("""INSERT INTO mcp_audit_log (user_slug, action, mcp_id, details)
                           VALUES (%s, %s, 'cognee-personal', %s)""",
                        (admin, AUDIT_IDENTITY_COGNEE_RESTORED, psycopg2.extras.Json(
                            {"legacy_slug": legacy_slug, "owner_sub": row["owner_sub"]})))
            return True

    def cognee_account_erased(self, legacy_slug: str) -> bool:
        with self.cursor() as cur:
            cur.execute("""SELECT 1 FROM mcp_cognee_accounts
                           WHERE legacy_slug = %s AND reason = 'erased'""", (legacy_slug,))
            return cur.fetchone() is not None

    def mark_cognee_erased(self, legacy_slug: str, admin: str, details: dict) -> bool:
        """Record that an admin erased a quarantined cognee account and its
        memory in cognee (#2573), with the audit line in the same transaction.
        The account row stays, marked 'erased', so the slug is never served
        again. False when the slug is not a quarantined account."""
        with self.cursor() as cur:
            cur.execute("""UPDATE mcp_cognee_accounts SET reason = 'erased'
                           WHERE legacy_slug = %s AND state = 'quarantined'""", (legacy_slug,))
            if cur.rowcount != 1:
                return False
            cur.execute("""INSERT INTO mcp_audit_log (user_slug, action, mcp_id, details)
                           VALUES (%s, %s, 'cognee-personal', %s)""",
                        (admin, AUDIT_IDENTITY_COGNEE_ERASED, psycopg2.extras.Json(
                            dict(details or {}, legacy_slug=legacy_slug))))
            return True

    def cognee_account_state(self, legacy_slug: str):
        """(state, owner_sub) of the legacy cognee account under the slug, or
        None when the slug has no registered account."""
        with self.cursor() as cur:
            cur.execute("""SELECT state, owner_sub FROM mcp_cognee_accounts
                           WHERE legacy_slug = %s""", (legacy_slug,))
            row = cur.fetchone()
        return (row["state"], row["owner_sub"]) if row else None

    def register_cognee_account(self, legacy_slug: str) -> None:
        """Register a slug-derived cognee account used in the no-token window
        (#2572, B6 fix round 4) exactly as migration 4 does, so the next
        directory-confirmed run decides it like any other."""
        with self.cursor() as cur:
            cur.execute("""INSERT INTO mcp_cognee_accounts (legacy_slug) VALUES (%s)
                           ON CONFLICT DO NOTHING""", (legacy_slug,))

    def cognee_account_pending(self, legacy_slug: str) -> bool:
        with self.cursor() as cur:
            cur.execute("""SELECT 1 FROM mcp_cognee_accounts
                           WHERE legacy_slug = %s AND state = 'pending'""", (legacy_slug,))
            return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # #3429: remote MCP servers (OAuth 2.1 dynamic client registration).
    # The registration is the box's, one per catalogue entry; a pending
    # authorization is one person's and is consumed by its callback.
    # ------------------------------------------------------------------

    def get_remote_client(self, mcp_id: str):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM mcp_remote_clients WHERE mcp_id = %s", (mcp_id,))
            return cur.fetchone()

    def upsert_remote_client(self, mcp_id: str, *, resource: str, issuer: str, redirect_uri: str,
                             endpoints: dict, client_id: str, client_secret_enc, auth_method: str,
                             scopes: str, iss_supported: bool = False) -> None:
        """`client_secret_enc` MUST be the AES-GCM token (or None)."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_remote_clients
                    (mcp_id, resource, issuer, redirect_uri, endpoints, client_id,
                     client_secret_enc, auth_method, scopes, iss_supported, registered_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (mcp_id) DO UPDATE SET
                    resource = EXCLUDED.resource, issuer = EXCLUDED.issuer,
                    redirect_uri = EXCLUDED.redirect_uri, endpoints = EXCLUDED.endpoints,
                    client_id = EXCLUDED.client_id, client_secret_enc = EXCLUDED.client_secret_enc,
                    auth_method = EXCLUDED.auth_method, scopes = EXCLUDED.scopes,
                    iss_supported = EXCLUDED.iss_supported, registered_at = NOW()
            """, (mcp_id, resource, issuer, redirect_uri, psycopg2.extras.Json(endpoints),
                  client_id, client_secret_enc, auth_method, scopes, bool(iss_supported)))

    def delete_remote_client(self, mcp_id: str, client_id: str | None = None) -> int:
        """Forget the registration (the provider rejected it). With
        `client_id`, only while it is still that registration: a newer one
        made meanwhile by another request is kept."""
        with self.cursor() as cur:
            if client_id is None:
                cur.execute("DELETE FROM mcp_remote_clients WHERE mcp_id = %s", (mcp_id,))
            else:
                cur.execute("DELETE FROM mcp_remote_clients WHERE mcp_id = %s AND client_id = %s",
                            (mcp_id, client_id))
            return cur.rowcount

    def insert_oauth_pending(self, nonce: str, owner: str, mcp_id: str, client_id: str,
                             verifier_enc: str, ttl_seconds: int) -> None:
        """`verifier_enc` MUST be the AES-GCM token. Expired rows go first."""
        with self.cursor() as cur:
            cur.execute("DELETE FROM mcp_oauth_pending WHERE expires_at < NOW()")
            cur.execute("""
                INSERT INTO mcp_oauth_pending (nonce, owner_key, mcp_id, client_id, verifier_enc, expires_at)
                VALUES (%s, %s, %s, %s, %s, NOW() + make_interval(secs => %s))
            """, (nonce, owner, mcp_id, client_id, verifier_enc, int(ttl_seconds)))

    def take_oauth_pending(self, nonce: str, owner: str, mcp_id: str):
        """The pending authorization, deleted as it is read (single use), or
        None when it is unknown, someone else's, another entry's or expired."""
        with self.cursor() as cur:
            cur.execute("""
                DELETE FROM mcp_oauth_pending
                WHERE nonce = %s AND owner_key = %s AND mcp_id = %s
                RETURNING client_id, verifier_enc, expires_at > NOW() AS live
            """, (nonce, owner, mcp_id))
            row = cur.fetchone()
        return row if row and row["live"] else None
