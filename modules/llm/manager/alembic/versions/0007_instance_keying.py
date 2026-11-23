# SPDX-License-Identifier: BUSL-1.1
"""deployment_instances.instance_id — key instances by engine, not (dep,worker)

Revision ID: 0007_instance_keying
Revises: 0006_deployment_task
Create Date: 2026-08-15

Q2/#267: an instance was unique per (deployment_id, worker_id), so two engines of
the same model on one worker (replicas / same-model-per-worker) collapsed into a
single row — the second registration overwrote the first's endpoint. Adding the
engine's instance_id (container name / node instance) lets the manager key by
(deployment_id, worker_id, instance_id) so each engine is a distinct row.

Additive + back-compat: existing rows get NULL instance_id (they continue to key
by (deployment, worker) until the node re-registers with an instance_id).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_instance_keying"
down_revision = "0006_deployment_task"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployment_instances", sa.Column("instance_id", sa.Text()))
    # Speeds the (deployment, worker, instance) upsert lookup on registration.
    op.create_index(
        "ix_deployment_instances_dep_worker_instance",
        "deployment_instances",
        ["deployment_id", "worker_id", "instance_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_deployment_instances_dep_worker_instance", table_name="deployment_instances")
    op.drop_column("deployment_instances", "instance_id")
