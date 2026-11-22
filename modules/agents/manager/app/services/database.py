# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""PostgreSQL database layer for the Agent Manager."""

import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# #2517: module-level, so the exception class a caller catches is the one raised
# here (a lazy import can bind a second copy after a sys.modules reload).
from app.services.names import NameTaken, name_key, suggest_name
from razzfazz_common.rbac_guard import allows

logger = logging.getLogger(__name__)


class AdvisoryLockBusy(RuntimeError):
    """#3187 review round 3 — the shared cross-process per-instance lock is
    held by another process beyond the wait window. Module-level on purpose
    (same rule as NameTaken): the class a caller catches must be the one
    raised here."""


def _instance_lock_key(instance_id) -> str:
    """The normalised lock key text — the SAME normalisation the provisioner's
    per-instance lock and the sessions registry use, so every lock in the
    system addresses one instance with one key."""
    try:
        return str(uuid.UUID(str(instance_id)))
    except (ValueError, AttributeError, TypeError):
        return str(instance_id)

# Register UUID adapter
psycopg2.extras.register_uuid()


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
        icon_url VARCHAR(255) DEFAULT '',
        pids_limit INTEGER
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

    # Version 9 (#221 follow-up, ga.9): pids_limit was defined per agent-type
    # in the catalog but never had a column here, so upsert_agent_type dropped
    # it and get_agent_type (SELECT *) never returned it — the provisioner then
    # passed None and docker_client fell back to its 512 default. Result: the
    # coding family's 2048 PID cap silently reverted to 512 on EVERY
    # (re)provision (only a manual `docker update` made it stick, and that was
    # lost on the next recreate). Add the column; the startup seed re-upserts
    # each type's catalog value (2048 for the coding family) into it.
    """
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS pids_limit INTEGER;

    INSERT INTO schema_version (version) VALUES (9) ON CONFLICT DO NOTHING;
    """,

    # Version 10 (Rework-W2, #244 P2): per-user portal preferences — grid
    # layout, pane assignments, tree folders. One JSON blob per user, written
    # by the portal (debounced), read on open — "reopens exactly how you left
    # it" is the whole point of the single-entry-portal (#244 extension 1).
    # TEXT, not JSONB: the manager never queries INTO the blob, and the
    # server-side size cap lives in the route, not the type.
    """
    CREATE TABLE IF NOT EXISTS portal_prefs (
        username VARCHAR(150) PRIMARY KEY,
        prefs TEXT NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    INSERT INTO schema_version (version) VALUES (10) ON CONFLICT DO NOTHING;
    """,

    # Version 11 (#959/W3): per-user max-running-instances quota, ENFORCED
    # (not just displayed). NULL = unlimited, matching the existing
    # max_per_type/max_heavy convention. Backfill NULL for every existing
    # tier — an admin opts in per tier via the (already-existing) PUT
    # /admin/api/tiers/<id> route once this column exists.
    """
    ALTER TABLE quota_tiers ADD COLUMN IF NOT EXISTS max_running INTEGER;

    INSERT INTO schema_version (version) VALUES (11) ON CONFLICT DO NOTHING;
    """,
    # Version 12 (#1988): instances are real. The UNIQUE (agent_type, user_slug)
    # index forbade a second instance of a type outright - while quota_tiers.
    # max_per_type promised 99 and the tree already grouped nodes per type.
    # Every existing row becomes instance #1 of its type (DEFAULT 1), keeping
    # its container name, volumes and per-instance DB untouched; the index is
    # widened to (agent_type, user_slug, instance_no). Re-runnable: every
    # statement is IF (NOT) EXISTS / ON CONFLICT.
    """
    ALTER TABLE agent_instances ADD COLUMN IF NOT EXISTS instance_no INTEGER NOT NULL DEFAULT 1;
    DROP INDEX IF EXISTS idx_instance_type_user;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_instance_type_user_no
        ON agent_instances(agent_type, user_slug, instance_no)
        WHERE state != 'destroyed';

    INSERT INTO schema_version (version) VALUES (12) ON CONFLICT DO NOTHING;
    """,

    # Version 13 (D7, #2518): explicit type-level deprecation marker, distinct
    # from `enabled`. `enabled=False` already hid a type from the New Agent
    # list; it also (as a bug) made an EXISTING instance of that type
    # invisible in `/api/tree`, because the tree only ever walked
    # `get_agent_types(enabled_only=True)`. `deprecated` lets the tree keep
    # showing a still-running instance of a retired type (badged "deprecated:
    # migrate or delete", still stoppable/deletable) instead of silently
    # dropping it. `replaced_by` is a small JSON hint (list of successor type
    # ids) the badge can show. Both default to the pre-existing behaviour
    # (not deprecated, no hint) so every current row is unaffected until the
    # catalog seed explicitly sets them (coding-tools / gsd-pi).
    """
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS deprecated BOOLEAN NOT NULL DEFAULT FALSE;
    ALTER TABLE agent_types ADD COLUMN IF NOT EXISTS replaced_by JSONB;

    INSERT INTO schema_version (version) VALUES (13) ON CONFLICT DO NOTHING;
    """,

    # Version 14 (E1, #2518): the volume names an instance's containers ACTUALLY
    # mount, persisted on the row. Recreate (upgrade / start's self-heal) and
    # delete used to RECOMPUTE them from `row.user_slug` + the CURRENT catalog;
    # after a slug migration (#192) or a catalog rename that computed set named
    # volumes that did not exist, so a recreate mounted fresh EMPTY volumes
    # (prod 2026-09-26: the operator's opencode) and a delete left the real ones
    # behind. Shape: {"primary": {name: mount}, "companion": {name: mount}}.
    # NULL = not recorded yet; the startup back-fill (services/slug_migration.py)
    # fills it once from the running container's Mounts and never overwrites.
    """
    ALTER TABLE agent_instances ADD COLUMN IF NOT EXISTS mounted_volumes JSONB;

    INSERT INTO schema_version (version) VALUES (14) ON CONFLICT DO NOTHING;
    """,

    # Version 15 (2026.09-ga.4, #2517 §3): every live instance carries a name,
    # unique per user. Per user_id (SQL cannot recompute a user's slug
    # candidates; user_id is the same Authentik uid under both slugs):
    #   pass 1 — every EXPLICIT name claims its key, first come (oldest) per
    #            key. A winner is never written, not even cosmetically:
    #            " Research" and "Mol  tis" stay byte-identical.
    #   pass 2 — the explicit losers (oldest first), then the unnamed rows
    #            (their type's display name), get the smallest " N" (N >= 2)
    #            whose key no row holds yet, cut to 64 chars. Seeding `seen`
    #            with ALL explicit keys first is what keeps a suffix from
    #            taking a later row's own explicit "work 2" (fix round 1).
    # The key approximates services/names.name_key: NFC, every Unicode
    # whitespace (NBSP included) collapsed, trimmed, lower() under an ICU or
    # UTF-8 libc collation when the server has one (a C-collation database's
    # lower() folds ASCII only; the probe uses a mid-word capital sigma, since
    # ICU lowers a word-final one to ς), then ß -> ss and ς -> σ (casefold's
    # two common extras). At runtime only name_key decides.
    # Idempotent: after one run every live name is explicit with a unique key,
    # so pass 1 claims them all and pass 2 writes nothing.
    #
    # This runs on every box's live database, prod included, at agent-manager
    # start — a failure here keeps the manager down. So it never raises on
    # data: a SQL NULL or JSON null config is treated as {}, and a config that
    # is not a JSON object at all (the app would not read a name from it
    # either) is left untouched with a NOTICE instead of failing jsonb_set.
    # No backslash and no percent sign in this SQL: the first would have to
    # survive two quoting layers, the second breaks a driver that formats an
    # args tuple into the statement.
    """
    DO $$
    DECLARE
        o RECORD; r RECORD;
        seen TEXT[]; winners UUID[]; losers UUID[];
        utf8 BOOLEAN; ws TEXT; coll TEXT := NULL; c TEXT; probe TEXT;
        raw TEXT; clean TEXT; base TEXT; cand TEXT; k TEXT; n INT;
    BEGIN
        SELECT pg_encoding_to_char(encoding) = 'UTF8' INTO utf8
          FROM pg_database WHERE datname = current_database();
        -- Python's str whitespace: TAB..CR, FS..US, space, and (UTF-8 only)
        -- NEL, NBSP, OGHAM, EN QUAD..HAIR SPACE, LS, PS, NNBSP, MMSP, IDEOSP.
        ws := '[' || chr(9) || '-' || chr(13) || chr(28) || '-' || chr(32);
        IF utf8 THEN
            ws := ws || chr(133) || chr(160) || chr(5760) || chr(8192) || '-' || chr(8202)
                     || chr(8232) || chr(8233) || chr(8239) || chr(8287) || chr(12288);
            -- the first collation whose lower() folds a non-ASCII pair. #2536
            -- item 37: a MID-WORD sigma. ICU lowers a word-final capital sigma
            -- to the final form, so a final-sigma probe made und-x-icu lose
            -- to C.utf8.
            FOREACH c IN ARRAY ARRAY['und-x-icu', 'C.utf8', 'C.UTF-8', 'en_US.utf8', 'en_US.UTF-8'] LOOP
                BEGIN
                    EXECUTE 'SELECT lower($1 COLLATE ' || quote_ident(c) || ')'
                       INTO probe USING chr(196) || chr(931) || chr(196);
                    IF probe = chr(228) || chr(963) || chr(228) THEN coll := c; EXIT; END IF;
                EXCEPTION WHEN OTHERS THEN
                    NULL;   -- not present / not supported by this build
                END;
            END LOOP;
        END IF;
        ws := ws || ']+';
        RAISE NOTICE USING MESSAGE = 'migration 15: name key collation ' || COALESCE(coll, 'database default');

        FOR o IN SELECT DISTINCT user_id FROM agent_instances WHERE state != 'destroyed' LOOP
            seen := '{}'; winners := '{}'; losers := '{}';

            -- pass 1: explicit names claim their keys, oldest first
            FOR r IN
                SELECT i.id, i.config->>'custom_name' AS raw
                  FROM agent_instances i
                 WHERE i.state != 'destroyed' AND i.user_id = o.user_id
                   AND jsonb_typeof(i.config) = 'object'
                   AND i.config->>'custom_name' IS NOT NULL
                 ORDER BY i.created_at, i.id
            LOOP
                clean := CASE WHEN utf8 THEN normalize(r.raw, NFC) ELSE r.raw END;
                clean := btrim(regexp_replace(clean, ws, ' ', 'g'));
                CONTINUE WHEN clean = '';          -- whitespace only: unnamed
                IF coll IS NOT NULL THEN
                    EXECUTE 'SELECT lower($1 COLLATE ' || quote_ident(coll) || ')' INTO k USING clean;
                ELSE
                    k := lower(clean);
                END IF;
                IF utf8 THEN k := replace(replace(k, chr(223), 'ss'), chr(962), chr(963)); END IF;
                IF k = ANY(seen) THEN
                    losers := losers || r.id;
                ELSE
                    seen := seen || k;
                    winners := winners || r.id;
                END IF;
            END LOOP;

            -- pass 2: losers, then unnamed rows, take the next free key
            FOR r IN
                SELECT i.id, jsonb_typeof(i.config) AS cfg_type,
                       CASE WHEN jsonb_typeof(i.config) = 'object'
                            THEN i.config->>'custom_name' END AS raw,
                       COALESCE(NULLIF(btrim(t.display_name), ''), i.agent_type) AS type_name
                  FROM agent_instances i LEFT JOIN agent_types t ON t.id = i.agent_type
                 WHERE i.state != 'destroyed' AND i.user_id = o.user_id
                   AND NOT (i.id = ANY(winners))
                 ORDER BY (i.id = ANY(losers)) DESC, i.created_at, i.id
            LOOP
                base := CASE WHEN r.id = ANY(losers) THEN r.raw ELSE r.type_name END;
                base := CASE WHEN utf8 THEN normalize(base, NFC) ELSE base END;
                base := rtrim(left(btrim(regexp_replace(base, ws, ' ', 'g')), 64));
                cand := base; n := 1;
                LOOP
                    IF coll IS NOT NULL THEN
                        EXECUTE 'SELECT lower($1 COLLATE ' || quote_ident(coll) || ')' INTO k USING cand;
                    ELSE
                        k := lower(cand);
                    END IF;
                    IF utf8 THEN k := replace(replace(k, chr(223), 'ss'), chr(962), chr(963)); END IF;
                    EXIT WHEN NOT (k = ANY(seen));
                    n := n + 1;
                    cand := rtrim(left(base, 64 - length(' ' || n))) || ' ' || n;
                END LOOP;
                seen := seen || k;
                IF r.cfg_type IS NULL OR r.cfg_type IN ('object', 'null') THEN
                    UPDATE agent_instances
                       SET config = jsonb_set(
                               CASE WHEN jsonb_typeof(config) = 'object'
                                    THEN config ELSE '{}'::jsonb END,
                               '{custom_name}', to_jsonb(cand))
                     WHERE id = r.id;
                ELSE
                    -- Built by concatenation, with no placeholder characters.
                    RAISE NOTICE USING MESSAGE = 'migration 15: instance ' || r.id
                        || ' has a non-object config (' || r.cfg_type || '), name left unset';
                END IF;
            END LOOP;
        END LOOP;
    END $$;

    INSERT INTO schema_version (version) VALUES (15) ON CONFLICT DO NOTHING;
    """,

    # Version 16 (2026.11): the OpenHands agent type is removed from the stack.
    # The seed no longer carries it, so its existing catalog row would stay
    # launchable with a dead image. It stays as a row (the instances reference
    # it), disabled and deprecated: no new launch, and an existing instance is
    # listed to its owner with the "migrate or delete" badge. The upgrade has
    # removed its containers, so a row still marked running is stopped; the
    # provisioner refuses to start or recreate it (provisioner.REMOVED_TYPES).
    # Its volumes are untouched; deleting the instance removes them.
    """
    UPDATE agent_types SET enabled = FALSE, deprecated = TRUE, replaced_by = '["opencode"]'::jsonb
        WHERE id = 'openhands';
    UPDATE agent_instances SET state = 'stopped'
        WHERE agent_type = 'openhands' AND state NOT IN ('destroyed', 'stopped');

    INSERT INTO schema_version (version) VALUES (16) ON CONFLICT DO NOTHING;
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
            # Check if schema_version table exists -- resolved through THIS
            # connection's search_path, exactly as the unqualified SELECT below
            # and the migrations' CREATE TABLE resolve it (2026.09-ga.4).
            # `information_schema.tables` without a schema filter spans every
            # schema of the database: a schema_version table in any other
            # schema made a fresh one look migrated and the SELECT raised
            # UndefinedTable, so the Agent Manager did not start.
            cur.execute("SELECT to_regclass('schema_version') IS NOT NULL")
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

            # #2517/#2518 (2026.09-ga.4): report the version the DATABASE is
            # at, read back after the migrations ran. A hand-kept constant said
            # "schema version 11" on a DB just migrated to 15 (DevBox's prod
            # dry run) -- a log line an operator checks after an upgrade.
            cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
            schema_version = cur.fetchone()[0]
            cur.close()
        logger.info(f"Database at schema version {schema_version}")

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

    # #511: the four fields `PUT /api/types/<id>` advertises as mutable. They
    # belong to the OPERATOR; everything else in a catalog row belongs to the
    # code. The catalog seed runs on every agent-manager start and used to
    # rewrite these from SEED_TYPES, so an admin's disable or a raised
    # mem_limit lasted only until the next upgrade / reboot / `compose up -d` —
    # silently, with no audit row and no UI hint. Reverting a raised mem_limit
    # reintroduces the OOM it was raised to fix.
    ADMIN_OWNED_FIELDS = ('enabled', 'mem_limit', 'cpu_limit', 'idle_timeout')

    def upsert_agent_type(self, type_data: dict, preserve_admin_fields: bool = False):
        """Insert or update a catalog row.

        `preserve_admin_fields=True` (used by the catalog seed) keeps the
        EXISTING values of `ADMIN_OWNED_FIELDS` when the row is already there,
        while still applying them on a genuine INSERT so a *new* agent type
        gets its seed defaults. The admin `PUT` leaves it False and writes
        everything — it IS the operator.

        Everything else stays always-upsert on purpose: that is how an image
        rename reaches installed boxes (migration v5 moved hermes/moltis off
        GHCR refs that had gone 403/404), and freezing whole rows would strand
        boxes on a dead image reference.
        """
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
            # #221 follow-up (ga.9): coding family = 2048; other types leave it
            # NULL so docker_client keeps its 512 fork-bomb-hardening default.
            'pids_limit': None,
            # D7 (#2518): a type is not deprecated / has no successor hint
            # unless the seed says so — every pre-existing SEED_TYPES entry
            # (and legacy DB row) upserts cleanly as "not deprecated".
            'deprecated': False,
            'replaced_by': None,
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
                    'entrypoint', 'command', 'pre_stop_command', 'replaced_by'):
            v = td.get(key)
            if v is not None and not isinstance(v, str):
                td[key] = psycopg2.extras.Json(v)
        # #511: build the DO UPDATE SET list so the seed can hand the
        # admin-owned columns back to the row that already exists. Assembled
        # from a fixed tuple of identifiers — no caller-supplied text reaches
        # the SQL; the VALUES are still bound parameters.
        update_cols = [c for c in (
            'display_name', 'tier', 'image', 'version', 'ports', 'volumes',
            'env_template', 'requires_db', 'requires_docker_socket',
            'mem_limit', 'cpu_limit', 'idle_timeout', 'enabled', 'description',
            'icon_url', 'companion_image', 'companion_version',
            'companion_suffix', 'companion_volumes', 'companion_env_template',
            'companion_command', 'entrypoint', 'command', 'pre_stop_command',
            'pre_stop_timeout', 'pids_limit', 'deprecated', 'replaced_by',
        ) if not (preserve_admin_fields and c in self.ADMIN_OWNED_FIELDS)]
        do_update = ",\n                    ".join(
            f"{c} = EXCLUDED.{c}" for c in update_cols)

        with self.cursor() as cur:
            cur.execute(f"""
                INSERT INTO agent_types (
                    id, display_name, tier, image, version, ports, volumes,
                    env_template, requires_db, requires_docker_socket,
                    mem_limit, cpu_limit, idle_timeout, enabled, description, icon_url,
                    companion_image, companion_version, companion_suffix,
                    companion_volumes, companion_env_template, companion_command,
                    entrypoint, command, pre_stop_command, pre_stop_timeout,
                    pids_limit, deprecated, replaced_by
                ) VALUES (
                    %(id)s, %(display_name)s, %(tier)s, %(image)s, %(version)s,
                    %(ports)s, %(volumes)s, %(env_template)s, %(requires_db)s,
                    %(requires_docker_socket)s, %(mem_limit)s, %(cpu_limit)s,
                    %(idle_timeout)s, %(enabled)s, %(description)s, %(icon_url)s,
                    %(companion_image)s, %(companion_version)s, %(companion_suffix)s,
                    %(companion_volumes)s, %(companion_env_template)s, %(companion_command)s,
                    %(entrypoint)s, %(command)s,
                    %(pre_stop_command)s, %(pre_stop_timeout)s,
                    %(pids_limit)s, %(deprecated)s, %(replaced_by)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    {do_update}
            """, td)

    # ------------------------------------------------------------------
    # Agent Instances
    # ------------------------------------------------------------------

    def get_user_instances(self, user_slug, user_id: str | None = None):
        """`user_slug` may be a single slug (str) or an iterable of candidate
        slugs (#192 — e.g. `[current, legacy]` from
        `razzfazz_common.user_slug.slug_candidates`) so a caller can list a
        user's instances even when some were provisioned before the
        make_user_slug hash-suffix change and are still stored under the
        legacy slug.

        D7 (#2518): LEFT JOIN, not JOIN. An instance whose `agent_type` row
        has since been deleted from the catalog entirely (not just disabled)
        must still come back — with NULL type_* columns — so a caller like
        `/api/tree` can surface it as an "unknown type" instead of the INNER
        JOIN silently dropping the row from every result.
        """
        slugs, uid = self._owner_params(user_slug, user_id)
        where, params = self._owner_where('i', slugs, uid)
        with self.cursor() as cur:
            cur.execute(f"""
                SELECT i.*, t.display_name as type_display_name, t.tier, t.icon_url,
                       t.deprecated as type_deprecated, t.replaced_by as type_replaced_by,
                       t.enabled as type_enabled
                FROM agent_instances i
                LEFT JOIN agent_types t ON i.agent_type = t.id
                WHERE {where} AND i.state != 'destroyed'
                ORDER BY i.created_at
            """, params)
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

    def get_instance_by_type_and_user(self, agent_type: str, user_slug,
                                      user_id: str | None = None):
        """`user_slug` may be a single slug (str) or an iterable of candidate
        slugs (#192 — see get_user_instances)."""
        slugs, uid = self._owner_params(user_slug, user_id)
        where, params = self._owner_where('i', slugs, uid)
        with self.cursor() as cur:
            cur.execute(f"""
                SELECT i.*, t.display_name as type_display_name, t.tier, t.icon_url, t.ports
                FROM agent_instances i
                JOIN agent_types t ON i.agent_type = t.id
                WHERE i.agent_type = %s AND {where} AND i.state != 'destroyed'
                ORDER BY i.instance_no
                LIMIT 1
            """, (agent_type, *params))
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

    # ------------------------------------------------------------------
    # #2517 §3 (2026.09-ga.4): names are required and unique per user.
    # ------------------------------------------------------------------
    # The check and the write run in ONE transaction that holds an advisory
    # lock keyed on the user's CANONICAL slug (scope[0] — slug_candidates puts
    # the current slug first), and the check reads every slug in the scope. A
    # partial unique index cannot express this: "same name" is name_key
    # (NFC + whitespace collapse + casefold), and "same user" spans the
    # canonical and the legacy slug (#192).
    _NAME_LOCK = "rzfz-agent-name:"

    @staticmethod
    def _scope(name_scope) -> list:
        return [name_scope] if isinstance(name_scope, str) else list(name_scope)

    _NAMES_SQL = """SELECT config->>'custom_name' AS n FROM agent_instances
                    WHERE user_slug = ANY(%s) AND state != 'destroyed'
                      AND jsonb_typeof(config) = 'object'
                      AND (%s::uuid IS NULL OR id != %s::uuid)"""

    def _names_locked(self, cur, scope, exclude_id=None) -> list:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self._NAME_LOCK + scope[0],))
        ex = str(exclude_id) if exclude_id else None
        cur.execute(self._NAMES_SQL, (list(scope), ex, ex))
        return [r['n'] for r in cur.fetchall() if r['n']]

    def user_instance_names(self, user_slugs, exclude_id=None) -> list:
        """The stored names of the user's live instances (non-empty only)."""
        ex = str(exclude_id) if exclude_id else None
        with self.cursor() as cur:
            cur.execute(self._NAMES_SQL, (self._scope(user_slugs), ex, ex))
            return [r['n'] for r in cur.fetchall() if r['n']]

    def set_instance_name(self, instance_id, name, name_scope) -> None:
        """Rename under the name lock; raises NameTaken if a sibling holds it."""
        scope = self._scope(name_scope)
        with self.cursor() as cur:
            taken = self._names_locked(cur, scope, exclude_id=instance_id)
            # Fix round 1: re-saving the row's OWN name (any spelling of the
            # same key) is always allowed — it cannot add a collision. Without
            # this, a pair migration 15's SQL key could not see as equal
            # (ß/SS, NFC/NFD, NBSP) left each row unable to save itself.
            cur.execute("SELECT config->>'custom_name' AS n FROM agent_instances "
                        "WHERE id = %s AND jsonb_typeof(config) = 'object'", (str(instance_id),))
            row = cur.fetchone()
            own = name_key(row['n']) if row and row['n'] else None
            if name_key(name) != own and name_key(name) in {name_key(t) for t in taken}:
                raise NameTaken(name, suggest_name(name, taken))
            cur.execute("""UPDATE agent_instances
                              SET config = jsonb_set(CASE WHEN jsonb_typeof(config) = 'object'
                                                          THEN config ELSE '{}'::jsonb END,
                                                     '{custom_name}', to_jsonb(%s::text))
                            WHERE id = %s""", (name, str(instance_id)))

    def create_instance(self, agent_type: str, user_id: str, user_slug: str,
                        container_name: str, config: dict = None,
                        instance_no: int = 1, name_scope=None) -> uuid.UUID:
        """Insert a 'provisioning' row. With `name_scope` (the owner's slug
        candidates, canonical first) the insert runs under the name lock and
        raises NameTaken when `config['custom_name']` is already one of the
        owner's names; without it the behaviour is the pre-#2517 one."""
        instance_id = uuid.uuid4()
        with self.cursor() as cur:
            if name_scope is not None:
                scope = self._scope(name_scope)
                wanted = (config or {}).get('custom_name') or ''
                taken = self._names_locked(cur, scope)
                if name_key(wanted) in {name_key(t) for t in taken}:
                    raise NameTaken(wanted, suggest_name(wanted, taken))
            cur.execute("""
                INSERT INTO agent_instances
                    (id, agent_type, user_id, user_slug, container_name, state, config, instance_no)
                VALUES (%s, %s, %s, %s, %s, 'provisioning', %s, %s)
            """, (instance_id, agent_type, user_id, user_slug, container_name,
                  psycopg2.extras.Json(config or {}), int(instance_no or 1)))
        return instance_id

    def next_instance_no(self, agent_type: str, user_slug) -> int:
        """#1988: the number the NEXT instance of `agent_type` for this user
        gets — one above the highest live one, so a destroyed #2 is never
        reused while a #3 exists (names would collide on the docker side).
        `user_slug` may be a str or an iterable of candidate slugs (#192)."""
        slugs = [user_slug] if isinstance(user_slug, str) else list(user_slug)
        with self.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(MAX(instance_no), 0) + 1 AS next_no FROM agent_instances
                WHERE agent_type = %s AND user_slug = ANY(%s) AND state != 'destroyed'
            """, (agent_type, slugs))
            row = cur.fetchone()
            return int(row['next_no'] if row else 1)

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

    def set_mounted_volumes(self, instance_id: uuid.UUID, mounted: dict):
        """E1 (#2518): record the volumes the instance's containers were just
        created with ({"primary": {name: mount}, "companion": {...}}). Written
        by the provisioner at launch and after every recreate — it is the
        authority on what the containers mount, so it overwrites."""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET mounted_volumes = %s WHERE id = %s",
                (psycopg2.extras.Json(mounted), instance_id),
            )

    def backfill_mounted_volumes(self, instance_id: uuid.UUID, mounted: dict) -> bool:
        """E1 (#2518): the one-time back-fill for rows created before the
        column existed. Never overwrites names that are already persisted
        (`WHERE mounted_volumes IS NULL`), so re-running it is a no-op.
        Returns True iff this call wrote the row."""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET mounted_volumes = %s "
                "WHERE id = %s AND mounted_volumes IS NULL",
                (psycopg2.extras.Json(mounted), instance_id),
            )
            return cur.rowcount == 1

    def list_live_instance_volume_rows(self):
        """E1 (#2518): every non-destroyed instance with what the volume
        logic needs — the back-fill plan and delete's "is this volume still
        another live instance's?" guard."""
        with self.cursor() as cur:
            cur.execute("""
                SELECT id, agent_type, user_id, user_slug, container_name,
                       instance_no, state, mounted_volumes
                FROM agent_instances
                WHERE state != 'destroyed'
                ORDER BY created_at
            """)
            return cur.fetchall()

    def per_instance_db_exists(self, db_name: str, db_user: str) -> tuple[bool, bool]:
        """#2529: `(database exists, role exists)`. pg_database and pg_roles
        are cluster-wide catalogs, so the agent-manager's own connection sees
        every per-instance database and role."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s) AS has_db, "
                "EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) AS has_role",
                (db_name, db_user),
            )
            row = cur.fetchone()
            return bool(row['has_db']), bool(row['has_role'])

    def list_live_db_instance_rows(self):
        """#2529/#1039: every non-destroyed instance of a type with a
        per-instance database, with its config — the start-up back-fill's input."""
        with self.cursor() as cur:
            cur.execute("""
                SELECT i.id, i.agent_type, i.user_id, i.user_slug, i.container_name,
                       i.instance_no, i.config
                FROM agent_instances i
                JOIN agent_types t ON i.agent_type = t.id
                WHERE i.state != 'destroyed' AND t.requires_db
                ORDER BY i.created_at
            """)
            return cur.fetchall()

    def update_last_accessed(self, instance_id: uuid.UUID):
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET last_accessed = NOW() WHERE id = %s",
                (instance_id,)
            )

    # ── #3187 review round 3: cross-process per-instance mutex ─────────────
    # A `rzfz agents update` runs via `docker exec` in a SECOND process with
    # its own provisioner and in-process locks, so those cannot serialize it
    # against the serving manager. A session-scoped PostgreSQL advisory lock
    # can: both processes share this database, and the lock is released
    # automatically if the holder crashes (its connection drops). No schema
    # change — advisory locks are session state, not table state.

    SHARED_LOCK_WAIT_SECONDS = 120.0

    @contextmanager
    def instance_advisory_lock(self, instance_id, wait_seconds=None):
        """Hold the cross-process mutex for `instance_id` for the whole
        `with` body, on a DEDICATED connection (the short-lived cursor()
        connections cannot hold a session lock). Polls pg_try_advisory_lock
        until acquired or `wait_seconds` elapse, then raises AdvisoryLockBusy.
        The key is derived server-side (hashtextextended), so every process
        computes the identical key for the same instance id."""
        key_text = _instance_lock_key(instance_id)
        wait = (self.SHARED_LOCK_WAIT_SECONDS if wait_seconds is None
                else max(0.0, float(wait_seconds)))
        conn = psycopg2.connect(self._dsn)
        cur = conn.cursor()
        deadline = time.monotonic() + wait
        try:
            while True:
                cur.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s::text, 0))",
                    (key_text,))
                if cur.fetchone()[0]:
                    conn.commit()
                    break
                conn.rollback()
                if time.monotonic() >= deadline:
                    raise AdvisoryLockBusy(
                        f"instance {key_text} is locked by another process")
                time.sleep(0.25)
            try:
                yield
            finally:
                try:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s::text, 0))",
                        (key_text,))
                    conn.commit()
                except Exception:  # noqa: BLE001 — closing releases it anyway
                    conn.rollback()
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
            conn.close()

    def instance_lock_held(self, instance_id) -> bool:
        """True while ANY process holds the instance's advisory lock. A probe:
        pg_try_advisory_lock acquires when free (immediately released again),
        and simply returns false when another session holds it — a pure
        read, it never blocks and never steals."""
        key_text = _instance_lock_key(instance_id)
        conn = psycopg2.connect(self._dsn)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s::text, 0))",
                (key_text,))
            free = bool(cur.fetchone()[0])
            conn.rollback()
            if free:
                cur.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s::text, 0))",
                    (key_text,))
                conn.commit()
                return False
            return True
        finally:
            conn.close()

    def delete_instance(self, instance_id: uuid.UUID):
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET state = 'destroyed' WHERE id = %s",
                (instance_id,)
            )

    def count_user_instances(self, user_slug, agent_type: str = None, tier: str = None,
                             user_id: str | None = None):
        """`user_slug` may be a single slug (str) or an iterable of candidate
        slugs (AGM-8 / #192 — see get_user_instances). Counting only the
        current slug hid every instance a user still holds under the legacy
        pre-hash slug from the quota gate.

        #1039: the count is the OWNER's (it backs max_per_type/max_heavy): a
        row also counts by `user_id` (the Authentik uid), so a row under a
        slug that is no longer a candidate (a renamed username) still counts.
        An empty uid never matches — same owner match as
        count_user_running_instances."""
        slugs, uid = self._owner_params(user_slug, user_id)
        where, params = self._owner_where('i', slugs, uid)
        with self.cursor() as cur:
            if agent_type:
                cur.execute(f"""
                    SELECT COUNT(*) FROM agent_instances i
                    WHERE {where}
                      AND i.agent_type = %s AND i.state != 'destroyed'
                """, (*params, agent_type))
            elif tier:
                cur.execute(f"""
                    SELECT COUNT(*) FROM agent_instances i
                    JOIN agent_types t ON i.agent_type = t.id
                    WHERE {where}
                      AND t.tier = %s AND i.state != 'destroyed'
                """, (*params, tier))
            else:
                cur.execute(f"""
                    SELECT COUNT(*) FROM agent_instances i
                    WHERE {where}
                      AND i.state != 'destroyed'
                """, params)
            return cur.fetchone()['count']

    def count_all_instances(self):
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agent_instances WHERE state != 'destroyed'")
            return cur.fetchone()['count']

    @staticmethod
    def _owner_where(alias: str, slugs, uid: str):
        """#2596: the uid-first owner predicate, ``(sql, params)``.

        With a uid: a row is the caller's when its recorded ``user_id`` is the
        uid (under ANY slug), or when it has NO ``user_id`` and sits under one
        of the caller's slug candidates that no other uid holds. A row
        recorded for another uid never matches by slug. Without a uid
        (internal callers that only know a slug) it is the plain slug match."""
        p = f"{alias}." if alias else ""
        if not uid:
            return f"{p}user_slug = ANY(%s)", (slugs,)
        return (
            f"({p}user_id = %s OR ({p}user_id = '' AND {p}user_slug = ANY(%s) "
            f"AND NOT EXISTS (SELECT 1 FROM agent_instances o "
            f"WHERE o.user_slug = {p}user_slug AND o.state != 'destroyed' "
            f"AND o.user_id <> '' AND o.user_id <> %s)))",
            (uid, slugs, uid),
        )

    def slug_holder_uids(self, slug: str) -> set:
        """#2596: the distinct non-empty uids with a live row under ``slug``."""
        with self.cursor() as cur:
            cur.execute("SELECT DISTINCT user_id FROM agent_instances "
                        "WHERE user_slug = %s AND state != 'destroyed' AND user_id <> ''",
                        (slug,))
            return {r['user_id'] for r in cur.fetchall()}

    @staticmethod
    def _owner_params(user_slug, user_id):
        """The owner match shared by the running-count and memory quotas
        (#1039): the slug candidates as a list, and the uid ('' = none)."""
        slugs = [user_slug] if isinstance(user_slug, str) else list(user_slug or [])
        uid = user_id or ''
        return slugs, uid

    def count_user_running_instances(self, user_slug, user_id: str | None = None) -> int:
        """#959/W3: how many of this OWNER's instances are currently RUNNING —
        the count the per-user max_running cap is enforced against. Distinct
        from count_user_instances(), which counts everything not destroyed
        (including stopped/provisioning instances).

        AGM-8 / #1039: `user_slug` is a slug (str) or the owner's slug
        candidates (#192), and a row also counts by `user_id` (the Authentik
        uid, the same under both slugs). An empty uid never matches — two
        users without one are not the same user."""
        slugs, uid = self._owner_params(user_slug, user_id)
        where, params = self._owner_where('i', slugs, uid)
        with self.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) FROM agent_instances i
                WHERE i.state = 'running'
                  AND {where}
            """, params)
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
                INSERT INTO quota_tiers (id, display_name, max_per_type, max_heavy, allowed_types, priority, max_running)
                VALUES (%(id)s, %(display_name)s, %(max_per_type)s, %(max_heavy)s, %(allowed_types)s, %(priority)s, %(max_running)s)
                ON CONFLICT (id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    max_per_type = EXCLUDED.max_per_type,
                    max_heavy = EXCLUDED.max_heavy,
                    allowed_types = EXCLUDED.allowed_types,
                    priority = EXCLUDED.priority,
                    max_running = EXCLUDED.max_running
            """, {**tier, 'max_running': tier.get('max_running')})

    # Members of the groups the role catalogue lists under agent-manager/unlimited
    # get implicit unlimited agent access without needing a matching quota_tiers
    # row (#2473). Without this bypass an admin sees every agent card as
    # Restricted and can't launch anything.

    #: #1635 — the group the MODULE ITSELF grants (profiles.yaml `user_group`).
    #: Being in it is what puts the tile on someone's portal, so it has to mean
    #: at least the basic tier: "may see the agent portal" and "may start an
    #: agent" must not be able to drift apart. The three `agent-*` groups still
    #: win when present (they are the gradation); this is the floor beneath
    #: them, and it is what makes the module's promise — "everyone gets their
    #: own agent, without a ticket" — true on a box where nobody has assigned a
    #: tier group yet.
    _PORTAL_GATE_GROUP = "razzfazz.ai AI Agents Users"
    _PORTAL_GATE_TIER = "agent-basic"

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
            'max_running': None,
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
        if allows('agent-manager', 'unlimited', user_groups):
            return self._unlimited_admin_tier()
        # #1635: the portal gate counts as the basic tier. Measured on 0.91: a
        # user in ONLY the module's own group could start 0 of 7 agents while
        # every card said "Contact admin for access" — which is the ticket the
        # module exists to abolish. Appended, not substituted: an explicit
        # `agent-power`/`agent-admin` membership still wins on priority.
        lookup = list(user_groups)
        if self._PORTAL_GATE_GROUP in lookup and self._PORTAL_GATE_TIER not in lookup:
            lookup.append(self._PORTAL_GATE_TIER)
        user_groups = lookup
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


    # ── Rework-W2 (#244): per-user portal prefs ─────────────────────────────
    def get_portal_prefs(self, username: str):
        """The user's portal prefs JSON string, or None."""
        with self.cursor() as cur:
            cur.execute("SELECT prefs FROM portal_prefs WHERE username = %s",
                        (username,))
            row = cur.fetchone()
            return row['prefs'] if row else None

    def set_portal_prefs(self, username: str, prefs: str) -> None:
        """Upsert the user's portal prefs (JSON string; route enforces the cap)."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO portal_prefs (username, prefs, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (username)
                DO UPDATE SET prefs = EXCLUDED.prefs, updated_at = NOW()
            """, (username, prefs))

    def update_instance_config(self, instance_id: uuid.UUID, config: dict):
        """Persist an instance's config JSONB (e.g. the chosen mem_limit so it
        survives a recreate — provisioner.upgrade re-reads config)."""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET config = %s WHERE id = %s",
                (psycopg2.extras.Json(config or {}), instance_id),
            )

    def merge_instance_config(self, instance_id: uuid.UUID, patch: dict):
        """Merge `patch` into the row's config JSONB in ONE statement, so a
        concurrent writer of another key is not overwritten
        (update_instance_config replaces the whole document)."""
        # #2536 review: `||` on a JSON string or null builds an ARRAY (the
        # patch is no longer a key, the string's keys are gone). An object is
        # patched; a string holding an object's JSON is read as that object;
        # anything else starts from {}. CASE evaluates in order, so the cast
        # only runs on text pg_input_is_valid (PostgreSQL 16+) accepted.
        with self.cursor() as cur:
            cur.execute(
                "UPDATE agent_instances SET config = (CASE jsonb_typeof(config) "
                "WHEN 'object' THEN config "
                "WHEN 'string' THEN CASE WHEN pg_input_is_valid(config #>> '{}', 'jsonb') "
                "THEN CASE WHEN jsonb_typeof((config #>> '{}')::jsonb) = 'object' "
                "THEN (config #>> '{}')::jsonb ELSE '{}'::jsonb END "
                "ELSE '{}'::jsonb END "
                "ELSE '{}'::jsonb END) || %s::jsonb "
                "WHERE id = %s",
                (psycopg2.extras.Json(patch), instance_id),
            )

    def _running_mem_rows(self, user_slug=None, user_id=None):
        with self.cursor() as cur:
            if user_slug is not None or user_id:
                slugs, uid = self._owner_params(user_slug, user_id)
                where, params = self._owner_where('i', slugs, uid)
                cur.execute(f"""
                    SELECT i.config, t.mem_limit AS type_mem_limit
                    FROM agent_instances i
                    JOIN agent_types t ON i.agent_type = t.id
                    WHERE i.state = 'running'
                      AND {where}
                """, params)
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

    def sum_user_running_mem_mb(self, user_slug, user_id: str | None = None) -> int:
        """Σ mem_limit (MB) across one OWNER's running instances (per-user cap);
        same owner match as count_user_running_instances (#1039)."""
        return sum(self._row_mem_mb(r) for r in self._running_mem_rows(user_slug, user_id))

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
