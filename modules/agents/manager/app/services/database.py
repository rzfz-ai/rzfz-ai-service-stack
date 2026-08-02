# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""PostgreSQL database layer for the Agent Manager."""

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Register UUID adapter
psycopg2.extras.register_uuid()

SCHEMA_VERSION = 8


def parse_mem_to_mb(value) -> int:
    """Parse a docker-style memory string ('2g', '512m', '2048', 2147483648)
    to an integer number of MB. Bare numbers are treated as BYTES (docker's
    convention). Unknown/empty → 0. Shared so the DB sum + provisioner clamp
    agree on the unit."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value / (1024 * 1024))  # bytes → MB
    s = str(value).strip().lower()
    if not s:
        return 0
    mult = 1  # bytes
    if s.endswith('g') or s.endswith('gb'):
        mult = 1024 * 1024 * 1024
        s = s.rstrip('b').rstrip('g')
    elif s.endswith('m') or s.endswith('mb'):
        mult = 1024 * 1024
        s = s.rstrip('b').rstrip('m')
    elif s.endswith('k') or s.endswith('kb'):
        mult = 1024
        s = s.rstrip('b').rstrip('k')
    try:
        num = float(s)
    except ValueError:
        return 0
    return int(num * mult / (1024 * 1024))

MIGRATIONS = [
    # Version 1: initial schema
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS agent_types (
        id VARCHAR(32) PRIMARY KEY,
        display_name VARCHAR(100) NOT NULL,
        tier VARCHAR(16) NOT NULL DEFAULT 'lightweight',
        image VARCHAR(255) NOT NULL,
        version VARCHAR(64) NOT NULL DEFAULT 'latest',
        ports JSONB DEFAULT '{}',
        volumes JSONB DEFAULT '[]',
        env_template JSONB DEFAULT '{}',
        requires_db BOOLEAN DEFAULT FALSE,
        requires_docker_socket BOOLEAN DEFAULT FALSE,
        mem_limit VARCHAR(16) DEFAULT '256m',
        cpu_limit FLOAT DEFAULT 1.0,
        idle_timeout INTEGER DEFAULT 1800,
        enabled BOOLEAN DEFAULT TRUE,
        description TEXT DEFAULT '',
        icon_url VARCHAR(255) DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS agent_instances (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        agent_type VARCHAR(32) NOT NULL REFERENCES agent_types(id),
        user_id VARCHAR(64) NOT NULL,
        user_slug VARCHAR(24) NOT NULL,
        container_id VARCHAR(64),
        container_name VARCHAR(80) NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'provisioning',
        config JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        stopped_at TIMESTAMPTZ,
        last_accessed TIMESTAMPTZ DEFAULT NOW(),
        error_message TEXT
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_instance_type_user
        ON agent_instances(agent_type, user_slug)
        WHERE state != 'destroyed';

    CREATE TABLE IF NOT EXISTS quota_tiers (
        id VARCHAR(32) PRIMARY KEY,
        display_name VARCHAR(100) NOT NULL,
        max_per_type INTEGER DEFAULT 1,
        max_heavy INTEGER DEFAULT 0,
        allowed_types JSONB,
        priority INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id BIGSERIAL PRIMARY KEY,
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        user_id VARCHAR(64),
        action VARCHAR(32) NOT NULL,
        agent_type VARCHAR(32),
        instance_id UUID,
        details JSONB DEFAULT '{}'
    );

    CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
    CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_instances_user ON agent_instances(user_slug);
    CREATE INDEX IF NOT EXISTS idx_instances_state ON agent_instances(state);

    INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
    """,

    # Version 2 (M020 S02): companion-container support — agent types may
    # bundle a second container (e.g. hermes pairs with hermes-workspace).
    # The provisioner launches both for one instance, with the companion
    # named `<primary>-<companion_suffix>` (default suffix: 'workspace').
    """
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS companion_image VARCHAR(255);
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS companion_version VARCHAR(64);
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS companion_suffix VARCHAR(32) DEFAULT 'workspace';
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS companion_volumes JSONB DEFAULT '[]';
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS companion_env_template JSONB DEFAULT '{}';

    INSERT INTO schema_version (version) VALUES (2) ON CONFLICT DO NOTHING;
    """,

    # Version 3 (rc6.7 #50): per-agent ENTRYPOINT + CMD overrides. Lets the
    # catalog wrap an upstream image's entrypoint with init shims (the
    # canonical case is OpenHands' chown-then-exec pattern that the shared
    # compose uses for /.openhands volume permissions; without an
    # entrypoint override the per-user OpenHands instance dies on
    # PermissionError writing .jwt_secret.tmp).
    """
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS entrypoint JSONB;
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS command JSONB;

    INSERT INTO schema_version (version) VALUES (3) ON CONFLICT DO NOTHING;
    """,

    # Version 4 (rc6.7 #50 followup): some dev installs already had a
    # `command TEXT DEFAULT ''` column from a prior iteration, so the v3
    # `ADD COLUMN IF NOT EXISTS command JSONB` was a no-op for them and the
    # column stayed text. That made list-form commands round-trip as
    # JSON-encoded strings (docker-py then split them on whitespace,
    # producing `[uvicorn,` / `openhands.server.listen:app,` etc).
    # Force the column type to JSONB. Cast existing values via JSON parser;
    # bare strings (like `''`) become JSONB null on cast failure.
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'agent_types'
            AND column_name = 'command'
            AND data_type = 'text'
        ) THEN
            ALTER TABLE agent_types
                ALTER COLUMN command DROP DEFAULT,
                ALTER COLUMN command TYPE JSONB
                USING (CASE WHEN command IS NULL OR command = '' THEN NULL
                            ELSE command::JSONB END);
        END IF;
    END
    $$;

    INSERT INTO schema_version (version) VALUES (4) ON CONFLICT DO NOTHING;
    """,

    # Version 5 (rc6.7 #56): hermes + moltis images moved from inaccessible
    # GHCR refs (ghcr.io/nousresearch/hermes-agent — private 403,
    # ghcr.io/moltis-org/moltis — non-existent 404, and the
    # ghcr.io/outsourc-e/hermes-workspace companion) to locally-built
    # images cloned from public source. The catalog seed (catalog.py) is
    # always-upsert and will rewrite image fields on the next agent-manager
    # boot; this migration is the explicit safety net for installs where
    # the seed didn't run yet (e.g. between code deploy and first restart).
    # It rewrites only the matching legacy rows — silent no-op if the
    # column already points at the new image.
    """
    UPDATE agent_types
       SET image = 'razzfazz-stack-hermes-agent'
     WHERE id = 'hermes' AND image = 'ghcr.io/nousresearch/hermes-agent';

    UPDATE agent_types
       SET companion_image = 'razzfazz-stack-hermes-workspace'
     WHERE id = 'hermes' AND companion_image = 'ghcr.io/outsourc-e/hermes-workspace';

    UPDATE agent_types
       SET image = 'razzfazz-stack-moltis'
     WHERE id = 'moltis' AND image = 'ghcr.io/moltis-org/moltis';

    INSERT INTO schema_version (version) VALUES (5) ON CONFLICT DO NOTHING;
    """,

    # Version 6 (M030-S2): track which image:version each instance is
    # actually running, plus a last_upgraded_at timestamp. The dashboard
    # reads `instance.image_version` vs `catalog.get_type(...)['version']`
    # to decide whether to surface "Update available" badges + buttons.
    # Backfill: existing rows pre-S2 have NULL image_version — treated
    # as "version unknown, hide update badge until first upgrade-or-relaunch
    # populates it" (deliberately not auto-backfilled with the catalog
    # version because catalog might already be ahead of what's actually
    # running). Once S2's upgrade() runs, image_version gets accurate.
    #
    # Also adds optional per-agent pre_stop_command + pre_stop_timeout to
    # agent_types (Q6 decision). Agents without explicit clean-shutdown
    # needs leave both NULL; provisioner.upgrade()/stop() skip the hook
    # when NULL. Honored by docker exec inside the container before
    # `docker stop`. Canonical use: moltis SQLite WAL flush.
    """
    ALTER TABLE agent_instances ADD COLUMN IF NOT EXISTS image_version VARCHAR(64);
    ALTER TABLE agent_instances ADD COLUMN IF NOT EXISTS last_upgraded_at TIMESTAMPTZ;

    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS pre_stop_command JSONB;
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS pre_stop_timeout INTEGER;

    INSERT INTO schema_version (version) VALUES (6) ON CONFLICT DO NOTHING;
    """,
    # Version 7 (#36): optional per-agent COMPANION command override. The
    # provisioner already accepts a `command` for the PRIMARY container; the
    # companion (e.g. hermes-workspace) had no such hook and always ran the
    # image's default CMD. hermes-workspace needs to seed its own
    # ~/.hermes/config.yaml (model.default) before starting its server so the
    # workspace's first-run onboarding gate is satisfied — otherwise it parks
    # on "Connect Backend · Choose a model" which reads as "agent not
    # connected". JSONB (exec-form list), NULL for every other agent type so
    # they keep using the image default.
    """
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS companion_command JSONB;

    INSERT INTO schema_version (version) VALUES (7) ON CONFLICT DO NOTHING;
    """,
    # Version 8 (#36 / PR #84 — agent memory governance): a small key/value
    # settings store, owned by agent-manager, for the operator-set global
    # agents-memory budget and the per-user memory cap. DB-backed (not .env) on
    # purpose: (1) it applies LIVE — a Config-UI change reaches the running
    # provisioner on the next launch with no `agent-manager` recreate; (2) it
    # sidesteps the known Config-UI single-file-mount `.env` inode-rewrite bug
    # (the config-ui-env-write memory) — the Config UI writes it via the SAME
    # admin-API + config-ui-admin trust path already used for quota_tiers /
    # agent_types, which persist here. Values are stored as text; callers cast.
    """
    CREATE TABLE IF NOT EXISTS agent_settings (
        key   VARCHAR(64) PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    INSERT INTO schema_version (version) VALUES (8) ON CONFLICT DO NOTHING;
    """,
]


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
        """Run pending schema migrations."""
        with self.conn() as conn:
            cur = conn.cursor()
            # Check if schema_version table exists
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
                    logger.info(f"Applying migration {i}...")
                    cur.execute(sql)
                    conn.commit()

            cur.close()
        logger.info(f"Database at schema version {SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # Agent Types
    # ------------------------------------------------------------------

    def get_agent_types(self, enabled_only=True):
        with self.cursor() as cur:
            if enabled_only:
                cur.execute("SELECT * FROM agent_types WHERE enabled = TRUE ORDER BY display_name")
            else:
                cur.execute("SELECT * FROM agent_types ORDER BY display_name")
            return cur.fetchall()

    def get_agent_type(self, type_id: str):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM agent_types WHERE id = %s", (type_id,))
            return cur.fetchone()

    def upsert_agent_type(self, type_data: dict):
        # M020 S02: companion fields default to empty so legacy SEED entries
        # that don't supply them still upsert cleanly.
        # rc6.7 #50: entrypoint + command default to NULL so legacy SEED
        # entries that don't supply them still upsert cleanly.
        # M030-S2: pre_stop_command + pre_stop_timeout default to NULL so
        # agents without explicit clean-shutdown needs still upsert cleanly.
        td = {
            'companion_image': None,
            'companion_version': None,
            'companion_suffix': 'workspace',
            'companion_volumes': '[]',
            'companion_env_template': '{}',
            'companion_command': None,   # #36: companion CMD override (JSONB)
            'entrypoint': None,
            'command': None,
            'pre_stop_command': None,
            'pre_stop_timeout': None,
            **type_data,
        }
        # entrypoint / command / pre_stop_command may be a list (exec form)
        # or a string (shell form). For the JSONB columns, psycopg2.extras.Json
        # is the safe adapter — passing a raw list to a JSONB column lets
        # psycopg2 interpret it as a Postgres array (text[]) which then
        # comes back as a list of single-quoted strings on read. Json()
        # forces the JSON adapter so the list survives the round-trip as
        # a list.
        # Every JSONB column must be str-or-Json() before the upsert. A round-trip
        # via get_agent_type() (SELECT *) returns JSONB columns as PARSED Python
        # dicts/lists; passing one raw to psycopg2 raises "can't adapt type 'dict'".
        # This bit any agent-type save (e.g. changing mem_limit) because
        # companion_env_template ({} → dict) and companion_volumes ([] → list) were
        # not covered here — only entrypoint/command were.
        for key in ('ports', 'volumes', 'env_template',
                    'companion_volumes', 'companion_env_template', 'companion_command',
                    'entrypoint', 'command', 'pre_stop_command'):
            v = td.get(key)
            if v is not None and not isinstance(v, str):
                td[key] = psycopg2.extras.Json(v)
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO agent_types (
                    id, display_name, tier, image, version, ports, volumes,
                    env_template, requires_db, requires_docker_socket,
                    mem_limit, cpu_limit, idle_timeout, enabled, description, icon_url,
                    companion_image, companion_version, companion_suffix,
                    companion_volumes, companion_env_template, companion_command,
                    entrypoint, command, pre_stop_command, pre_stop_timeout
                ) VALUES (
                    %(id)s, %(display_name)s, %(tier)s, %(image)s, %(version)s,
                    %(ports)s, %(volumes)s, %(env_template)s, %(requires_db)s,
                    %(requires_docker_socket)s, %(mem_limit)s, %(cpu_limit)s,
                    %(idle_timeout)s, %(enabled)s, %(description)s, %(icon_url)s,
                    %(companion_image)s, %(companion_version)s, %(companion_suffix)s,
                    %(companion_volumes)s, %(companion_env_template)s, %(companion_command)s,
                    %(entrypoint)s, %(command)s,
                    %(pre_stop_command)s, %(pre_stop_timeout)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    tier = EXCLUDED.tier,
                    image = EXCLUDED.image,
                    version = EXCLUDED.version,
                    ports = EXCLUDED.ports,
                    volumes = EXCLUDED.volumes,
                    env_template = EXCLUDED.env_template,
                    requires_db = EXCLUDED.requires_db,
                    requires_docker_socket = EXCLUDED.requires_docker_socket,
                    mem_limit = EXCLUDED.mem_limit,
                    cpu_limit = EXCLUDED.cpu_limit,
                    idle_timeout = EXCLUDED.idle_timeout,
                    enabled = EXCLUDED.enabled,
                    description = EXCLUDED.description,
                    icon_url = EXCLUDED.icon_url,
                    companion_image = EXCLUDED.companion_image,
                    companion_version = EXCLUDED.companion_version,
                    companion_suffix = EXCLUDED.companion_suffix,
                    companion_volumes = EXCLUDED.companion_volumes,
                    companion_env_template = EXCLUDED.companion_env_template,
                    companion_command = EXCLUDED.companion_command,
                    entrypoint = EXCLUDED.entrypoint,
                    command = EXCLUDED.command,
                    pre_stop_command = EXCLUDED.pre_stop_command,
                    pre_stop_timeout = EXCLUDED.pre_stop_timeout
            """, td)

    # ------------------------------------------------------------------
    # Agent Instances
    # ------------------------------------------------------------------

    def get_user_instances(self, user_slug):
        """`user_slug` may be a single slug (str) or an iterable of candidate
        slugs (#192 — e.g. `[current, legacy]` from
        `razzfazz_common.user_slug.slug_candidates`) so a caller can list a
        user's instances even when some were provisioned before the
        make_user_slug hash-suffix change and are still stored under the
        legacy slug."""
        slugs = [user_slug] if isinstance(user_slug, str) else list(user_slug)
        with self.cursor() as cur:
            cur.execute("""
                SELECT i.*, t.display_name as type_display_name, t.tier, t.icon_url
                FROM agent_instances i
                JOIN agent_types t ON i.agent_type = t.id
                WHERE i.user_slug = ANY(%s) AND i.state != 'destroyed'
                ORDER BY i.created_at
            """, (slugs,))
            return cur.fetchall()

    def get_all_instances(self):
        with self.cursor() as cur:
            # rc6.7 #78: include t.idle_timeout in the SELECT so the
            # lifecycle manager's `_check_idle` actually sees the catalog
            # override. Before this fix, `inst.get('idle_timeout')` always
            # returned None (column wasn't in the result set), so the
            # lifecycle code fell through to AGENT_IDLE_TIMEOUT_LIGHTWEIGHT
            # (1800s = 30 min) for every lightweight agent. Hermes /
            # Moltis / coding-tools / etc. were silently killed after 30
            # minutes regardless of their catalog `idle_timeout` (e.g.
            # hermes defines 14400 = 4h, paperclip 7200 = 2h). Reproduces
            # 1:1: hermes dies via SIGTERM ~31 minutes after start, the
            # gateway logs the "Shutdown diagnostic — other hermes
            # processes" message, container exits with code 1.
            cur.execute("""
                SELECT i.*,
                       t.display_name as type_display_name,
                       t.tier,
                       t.icon_url,
                       t.idle_timeout
                FROM agent_instances i
                JOIN agent_types t ON i.agent_type = t.id
                WHERE i.state != 'destroyed'
                ORDER BY i.user_slug, i.agent_type
            """)
            return cur.fetchall()

    def get_instance(self, instance_id: uuid.UUID):
        with self.cursor() as cur:
            cur.execute("""
                SELECT i.*, t.display_name as type_display_name, t.tier, t.icon_url,
                       t.image, t.version, t.ports, t.env_template, t.mem_limit,
                       t.cpu_limit, t.requires_db, t.requires_docker_socket, t.volumes as type_volumes
                FROM agent_instances i
                JOIN agent_types t ON i.agent_type = t.id
                WHERE i.id = %s
            """, (instance_id,))
            return cur.fetchone()

    def get_instance_by_type_and_user(self, agent_type: str, user_slug):
        """`user_slug` may be a single slug (str) or an iterable of candidate
        slugs (#192 — see get_user_instances)."""
        slugs = [user_slug] if isinstance(user_slug, str) else list(user_slug)
        with self.cursor() as cur:
            cur.execute("""
                SELECT i.*, t.display_name as type_display_name, t.tier, t.icon_url, t.ports
                FROM agent_instances i
                JOIN agent_types t ON i.agent_type = t.id
                WHERE i.agent_type = %s AND i.user_slug = ANY(%s) AND i.state != 'destroyed'
            """, (agent_type, slugs))
            return cur.fetchone()

    def list_active_instances_by_type(self, agent_type: str):
        """All non-destroyed instances of a given agent type.

        Used by the per-instance subdomain proxy to find the matching
        instance by deriving its opaque token.
        """
        with self.cursor() as cur:
            cur.execute("""
                SELECT i.*, t.display_name as type_display_name, t.tier, t.icon_url, t.ports
                FROM agent_instances i
                JOIN agent_types t ON i.agent_type = t.id
                WHERE i.agent_type = %s AND i.state != 'destroyed'
            """, (agent_type,))
            return cur.fetchall()

    def list_active_instances_all(self):
        """All non-destroyed instances across all agent types.

        Used by the on-demand-TLS ask endpoint to validate that an
        incoming hostname maps to a real registered instance before
        Caddy issues a Let's Encrypt cert for it.
        """
        with self.cursor() as cur:
            cur.execute("""
                SELECT id, agent_type
                FROM agent_instances
                WHERE state != 'destroyed'
            """)
            return cur.fetchall()

    def create_instance(self, agent_type: str, user_id: str, user_slug: str,
                        container_name: str, config: dict = None) -> uuid.UUID:
        instance_id = uuid.uuid4()
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO agent_instances
                    (id, agent_type, user_id, user_slug, container_name, state, config)
                VALUES (%s, %s, %s, %s, %s, 'provisioning', %s)
            """, (instance_id, agent_type, user_id, user_slug, container_name,
                  psycopg2.extras.Json(config or {})))
        return instance_id

    def update_instance_state(self, instance_id: uuid.UUID, state: str,
                              container_id: str = None, error_message: str = None,
                              image_version: str = None):
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
            # M030-S2: track image_version on provision + upgrade for the
            # dashboard's "Update available" detection (compare to catalog
            # current version). Also explicitly NULLed on launch_failed so
            # we don't mis-attribute version on retry.
            if image_version is not None:
                updates.append("image_version = %s")
                params.append(image_version)
            if state == 'running':
                updates.append("started_at = %s")
                params.append(now)
                updates.append("error_message = NULL")
            elif state == 'stopped':
                updates.append("stopped_at = %s")
                params.append(now)

            params.append(instance_id)
            cur.execute(
                f"UPDATE agent_instances SET {', '.join(updates)} WHERE id = %s",
                params
            )

    def mark_instance_upgraded(self, instance_id: uuid.UUID, image_version: str):
        """M030-S2: stamp last_upgraded_at + new image_version after upgrade()."""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET image_version = %s, last_upgraded_at = NOW() WHERE id = %s",
                (image_version, instance_id),
            )

    def migrate_legacy_user_slug(self, instance_id: uuid.UUID, new_slug: str) -> bool:
        """Forward-migrate a legacy-slug instance's `user_slug` to the current
        slug (#192), so subsequent ownership checks are an exact match instead
        of relying on slug_candidates() every time.

        Single-field, idempotent update guarded by a NOT EXISTS check against
        the `idx_instance_type_user` UNIQUE (agent_type, user_slug) index: if
        another (non-destroyed) instance of the SAME agent_type already holds
        `new_slug` — e.g. the user already re-launched under the current slug
        while their legacy instance was inaccessible — the update is skipped
        rather than raising a unique-violation. Ownership still works via
        slug_candidates() in that case; this is a best-effort cleanup, not a
        correctness requirement. Returns True iff the row was updated.
        """
        with self.cursor() as cur:
            cur.execute("""
                UPDATE agent_instances i
                SET user_slug = %s
                WHERE i.id = %s
                  AND i.user_slug != %s
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_instances o
                      WHERE o.agent_type = i.agent_type
                        AND o.user_slug = %s
                        AND o.id != i.id
                        AND o.state != 'destroyed'
                  )
            """, (new_slug, instance_id, new_slug, new_slug))
            return cur.rowcount == 1

    def update_last_accessed(self, instance_id: uuid.UUID):
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET last_accessed = NOW() WHERE id = %s",
                (instance_id,)
            )

    def delete_instance(self, instance_id: uuid.UUID):
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET state = 'destroyed' WHERE id = %s",
                (instance_id,)
            )

    def count_user_instances(self, user_slug: str, agent_type: str = None, tier: str = None):
        with self.cursor() as cur:
            if agent_type:
                cur.execute("""
                    SELECT COUNT(*) FROM agent_instances
                    WHERE user_slug = %s AND agent_type = %s AND state != 'destroyed'
                """, (user_slug, agent_type))
            elif tier:
                cur.execute("""
                    SELECT COUNT(*) FROM agent_instances i
                    JOIN agent_types t ON i.agent_type = t.id
                    WHERE i.user_slug = %s AND t.tier = %s AND i.state != 'destroyed'
                """, (user_slug, tier))
            else:
                cur.execute("""
                    SELECT COUNT(*) FROM agent_instances
                    WHERE user_slug = %s AND state != 'destroyed'
                """, (user_slug,))
            return cur.fetchone()['count']

    def count_all_instances(self):
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agent_instances WHERE state != 'destroyed'")
            return cur.fetchone()['count']

    # ------------------------------------------------------------------
    # Quota Tiers
    # ------------------------------------------------------------------

    def get_quota_tiers(self):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM quota_tiers ORDER BY priority DESC")
            return cur.fetchall()

    def get_quota_tier(self, tier_id: str):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM quota_tiers WHERE id = %s", (tier_id,))
            return cur.fetchone()

    def upsert_quota_tier(self, tier: dict):
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO quota_tiers (id, display_name, max_per_type, max_heavy, allowed_types, priority)
                VALUES (%(id)s, %(display_name)s, %(max_per_type)s, %(max_heavy)s, %(allowed_types)s, %(priority)s)
                ON CONFLICT (id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    max_per_type = EXCLUDED.max_per_type,
                    max_heavy = EXCLUDED.max_heavy,
                    allowed_types = EXCLUDED.allowed_types,
                    priority = EXCLUDED.priority
            """, tier)

    # Authentik groups whose members get implicit unlimited agent access
    # without needing a matching quota_tiers row. Without this bypass an
    # admin (only in "razzfazz.ai Super Admins") sees every agent card as
    # Restricted and can't launch anything.
    _UNLIMITED_ADMIN_GROUPS = frozenset({
        "razzfazz.ai Super Admins",
        "authentik Admins",
    })

    @staticmethod
    def _unlimited_admin_tier():
        """Synthetic tier dict — same shape as a quota_tiers row, no caps.
        `allowed_types=None` matches the existing convention for 'all types
        allowed'. max_per_type/max_heavy=99 keeps per-call quota checks
        from special-casing this case."""
        return {
            'id': '_unlimited_admin',
            'display_name': 'Admin (unlimited)',
            'max_per_type': 99,
            'max_heavy': 99,
            'allowed_types': None,
            'priority': 1000,
        }

    def resolve_user_tier(self, user_groups: list[str]):
        """Resolve the highest-priority quota tier for the user.

        Super-admin group members short-circuit to the synthetic unlimited
        tier (no DB lookup, no quota caps). Everyone else gets the highest-
        priority quota_tiers row whose id matches one of their Authentik
        groups, or None if no row matches.
        """
        if not user_groups:
            return None
        if any(g in self._UNLIMITED_ADMIN_GROUPS for g in user_groups):
            return self._unlimited_admin_tier()
        with self.cursor() as cur:
            placeholders = ','.join(['%s'] * len(user_groups))
            cur.execute(f"""
                SELECT * FROM quota_tiers
                WHERE id IN ({placeholders})
                ORDER BY priority DESC
                LIMIT 1
            """, user_groups)
            return cur.fetchone()

    # ------------------------------------------------------------------
    # Agent Settings (memory governance — #36 / PR #84)
    # ------------------------------------------------------------------

    def get_agent_setting(self, key: str, default=None):
        """Read a governance setting (text). Returns `default` if unset."""
        with self.cursor() as cur:
            cur.execute("SELECT value FROM agent_settings WHERE key = %s", (key,))
            row = cur.fetchone()
        if not row or row['value'] is None:
            return default
        return row['value']

    def set_agent_setting(self, key: str, value):
        """Upsert a governance setting (stored as text)."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO agent_settings (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW()
            """, (key, str(value)))

    def update_instance_config(self, instance_id: uuid.UUID, config: dict):
        """Persist an instance's config JSONB (e.g. the chosen mem_limit so it
        survives a recreate — provisioner.upgrade re-reads config)."""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET config = %s WHERE id = %s",
                (psycopg2.extras.Json(config or {}), instance_id),
            )

    def _running_mem_rows(self, user_slug: str = None):
        with self.cursor() as cur:
            if user_slug:
                cur.execute("""
                    SELECT i.config, t.mem_limit AS type_mem_limit
                    FROM agent_instances i
                    JOIN agent_types t ON i.agent_type = t.id
                    WHERE i.state = 'running' AND i.user_slug = %s
                """, (user_slug,))
            else:
                cur.execute("""
                    SELECT i.config, t.mem_limit AS type_mem_limit
                    FROM agent_instances i
                    JOIN agent_types t ON i.agent_type = t.id
                    WHERE i.state = 'running'
                """)
            return cur.fetchall()

    @staticmethod
    def _row_mem_mb(row) -> int:
        """Effective mem_limit for one running instance, in MB. Prefers the
        per-instance chosen mem_limit (config['mem_limit'], persisted at launch/
        update), falling back to the catalog type's mem_limit."""
        cfg = row.get('config') or {}
        if isinstance(cfg, str):
            try:
                import json as _json
                cfg = _json.loads(cfg)
            except (ValueError, TypeError):
                cfg = {}
        limit = cfg.get('mem_limit') or row.get('type_mem_limit') or '0'
        return parse_mem_to_mb(limit)

    def sum_running_mem_mb(self) -> int:
        """Σ mem_limit (MB) across ALL running instances (global budget usage)."""
        return sum(self._row_mem_mb(r) for r in self._running_mem_rows())

    def sum_user_running_mem_mb(self, user_slug: str) -> int:
        """Σ mem_limit (MB) across one user's running instances (per-user cap)."""
        return sum(self._row_mem_mb(r) for r in self._running_mem_rows(user_slug))

    # ------------------------------------------------------------------
    # Audit Log
    # ------------------------------------------------------------------

    def log_audit(self, user_id: str, action: str, agent_type: str = None,
                  instance_id: uuid.UUID = None, details: dict = None):
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO audit_log (user_id, action, agent_type, instance_id, details)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, action, agent_type, instance_id,
                  psycopg2.extras.Json(details or {})))

    def get_audit_log(self, limit=100, user_id: str = None):
        with self.cursor() as cur:
            if user_id:
                cur.execute("""
                    SELECT * FROM audit_log WHERE user_id = %s
                    ORDER BY timestamp DESC LIMIT %s
                """, (user_id, limit))
            else:
                cur.execute(
                    "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT %s",
                    (limit,)
                )
            return cur.fetchall()
