# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#336: unique constraints behind every check-then-insert identity.

workers.name, models.name and the deployment-instance key were guarded only
by application-level one_or_none() — two concurrent requests both see None,
both insert, and every LATER call raises MultipleResultsFound → permanent
500s on the 30s node-report path until manual table surgery. The DB now
refuses the second insert; the app retries and finds the winner's row.

Existing duplicates are repaired first (FKs repointed to the surviving row,
newest state wins), so the constraint creation cannot fail on a live box.

instance_id is NULL for endpoint-only backends (Mac gateway), and Postgres
treats NULLs as distinct — so the instance key needs TWO partial unique
indexes, one per NULLness.

Revision ID: 0015_unique_identity
Revises: 0014_enroll_hardening
"""
from alembic import op

revision = "0015_unique_identity"
down_revision = "0014_enroll_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- workers: keep the most recently heartbeated row per name ------------
    op.execute("""
        CREATE TEMP TABLE _worker_keep AS
        SELECT id AS dup_id,
               first_value(id) OVER (PARTITION BY name
                   ORDER BY last_heartbeat DESC NULLS LAST, id) AS keep_id
        FROM workers
    """)
    # review #657: node_commands.worker_id is ondelete=CASCADE — without the
    # repoint the dup-DELETE would cascade away the loser-duplicate's whole
    # command history (tail_logs results included).
    for tbl, col in (("deployment_instances", "worker_id"),
                     ("runner_upgrades", "worker_id"),
                     ("node_commands", "worker_id")):
        op.execute(f"""
            UPDATE {tbl} t SET {col} = k.keep_id
            FROM _worker_keep k
            WHERE t.{col} = k.dup_id AND k.dup_id <> k.keep_id
        """)
    op.execute("""
        DELETE FROM workers w USING _worker_keep k
        WHERE w.id = k.dup_id AND k.dup_id <> k.keep_id
    """)
    op.execute("DROP TABLE _worker_keep")

    # --- models: repoint every FK to the surviving row, then delete dups -----
    op.execute("""
        CREATE TEMP TABLE _model_keep AS
        SELECT id AS dup_id,
               first_value(id) OVER (PARTITION BY name ORDER BY id) AS keep_id
        FROM models
    """)
    for tbl in ("model_artifacts", "model_presets", "deployments"):
        op.execute(f"""
            UPDATE {tbl} t SET model_id = k.keep_id
            FROM _model_keep k
            WHERE t.model_id = k.dup_id AND k.dup_id <> k.keep_id
        """)
    op.execute("""
        DELETE FROM models m USING _model_keep k
        WHERE m.id = k.dup_id AND k.dup_id <> k.keep_id
    """)
    op.execute("DROP TABLE _model_keep")

    # --- deployment_instances: newest state wins per key ---------------------
    op.execute("""
        DELETE FROM deployment_instances di USING (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY deployment_id, worker_id, instance_id
                       ORDER BY started_at DESC NULLS LAST, id) AS rn
            FROM deployment_instances
        ) d
        WHERE di.id = d.id AND d.rn > 1
    """)

    op.create_unique_constraint("uq_workers_name", "workers", ["name"])
    op.create_unique_constraint("uq_models_name", "models", ["name"])
    op.execute("""
        CREATE UNIQUE INDEX uq_depinst_dep_worker_instance
        ON deployment_instances (deployment_id, worker_id, instance_id)
        WHERE instance_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_depinst_dep_worker_null_instance
        ON deployment_instances (deployment_id, worker_id)
        WHERE instance_id IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_depinst_dep_worker_null_instance")
    op.execute("DROP INDEX IF EXISTS uq_depinst_dep_worker_instance")
    op.drop_constraint("uq_models_name", "models", type_="unique")
    op.drop_constraint("uq_workers_name", "workers", type_="unique")
