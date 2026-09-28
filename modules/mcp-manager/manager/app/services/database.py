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
]

SCHEMA_VERSION = len(MIGRATIONS)


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
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'schema_version'
                )
            """)
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
    # Secrets — encrypted_value only; never plaintext.
    # ------------------------------------------------------------------

    def upsert_secret(self, user_slug: str, mcp_id: str, cred_type: str,
                      encrypted_value: str, expires_at=None):
        """Insert/replace an encrypted credential. `encrypted_value` MUST be the
        AES-GCM ciphertext token (the caller encrypts before calling this)."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_user_secrets
                    (user_slug, mcp_id, cred_type, encrypted_value, expires_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_slug, mcp_id, cred_type) DO UPDATE SET
                    encrypted_value = EXCLUDED.encrypted_value,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
            """, (user_slug, mcp_id, cred_type, encrypted_value, expires_at))

    def get_secrets(self, user_slug: str, mcp_id: str):
        """Return all encrypted secret rows for (user, mcp). only-own scoped."""
        with self.cursor() as cur:
            cur.execute("""
                SELECT cred_type, encrypted_value, expires_at, updated_at
                FROM mcp_user_secrets
                WHERE user_slug = %s AND mcp_id = %s
            """, (user_slug, mcp_id))
            return cur.fetchall()

    def get_secret(self, user_slug: str, mcp_id: str, cred_type: str):
        with self.cursor() as cur:
            cur.execute("""
                SELECT cred_type, encrypted_value, expires_at
                FROM mcp_user_secrets
                WHERE user_slug = %s AND mcp_id = %s AND cred_type = %s
            """, (user_slug, mcp_id, cred_type))
            return cur.fetchone()

    def delete_secrets(self, user_slug: str, mcp_id: str):
        """Revoke: drop every credential for (user, mcp). only-own scoped."""
        with self.cursor() as cur:
            cur.execute("""
                DELETE FROM mcp_user_secrets
                WHERE user_slug = %s AND mcp_id = %s
            """, (user_slug, mcp_id))

    def list_user_mcp_ids_with_secrets(self, user_slug: str):
        with self.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT mcp_id FROM mcp_user_secrets
                WHERE user_slug = %s ORDER BY mcp_id
            """, (user_slug,))
            return [r["mcp_id"] for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Instances
    # ------------------------------------------------------------------

    def create_instance(self, mcp_id: str, user_id: str, user_slug: str,
                        container_name: str, config: dict = None) -> uuid.UUID:
        instance_id = uuid.uuid4()
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_instances
                    (id, mcp_id, user_id, user_slug, container_name, state, config)
                VALUES (%s, %s, %s, %s, %s, 'provisioning', %s)
            """, (instance_id, mcp_id, user_id, user_slug, container_name,
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

    def get_instance(self, instance_id: uuid.UUID):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM mcp_instances WHERE id = %s", (instance_id,))
            return cur.fetchone()

    def get_instance_by_mcp_and_user(self, mcp_id: str, user_slug: str):
        with self.cursor() as cur:
            cur.execute("""
                SELECT * FROM mcp_instances
                WHERE mcp_id = %s AND user_slug = %s AND state != 'destroyed'
            """, (mcp_id, user_slug))
            return cur.fetchone()

    def get_user_instances(self, user_slug: str):
        with self.cursor() as cur:
            cur.execute("""
                SELECT * FROM mcp_instances
                WHERE user_slug = %s AND state != 'destroyed'
                ORDER BY created_at
            """, (user_slug,))
            return cur.fetchall()

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
    # Type bindings
    # ------------------------------------------------------------------

    def set_bindings(self, user_slug: str, mcp_id: str, consumers: list):
        """Replace the consumer bindings for (user, mcp)."""
        with self.cursor() as cur:
            cur.execute(
                "DELETE FROM mcp_type_bindings WHERE user_slug = %s AND mcp_id = %s",
                (user_slug, mcp_id),
            )
            for consumer in consumers:
                cur.execute("""
                    INSERT INTO mcp_type_bindings (user_slug, mcp_id, consumer)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_slug, mcp_id, consumer) DO NOTHING
                """, (user_slug, mcp_id, consumer))

    def get_bindings(self, user_slug: str):
        with self.cursor() as cur:
            cur.execute(
                "SELECT mcp_id, consumer FROM mcp_type_bindings WHERE user_slug = %s",
                (user_slug,),
            )
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
