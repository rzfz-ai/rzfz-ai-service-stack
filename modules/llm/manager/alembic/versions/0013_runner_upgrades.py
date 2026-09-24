# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#549 R3: runner_upgrades — durable state for the per-node upgrade sequence.

A table, not memory, for two reasons: the manager is deliberately single-worker
with process-local state documented as fragile (#359), and an upgrade that
vanishes on a manager restart mid-sequence would leave a node drained with
nobody knowing why.

Revision ID: 0013_runner_upgrades
Revises: 0012_deployment_runner_image
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0013_runner_upgrades"
down_revision = "0012_deployment_runner_image"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runner_upgrades",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("worker_id", UUID(as_uuid=True),
                  sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("image", sa.Text(), nullable=False),
        # deploying | relaunching | done | failed | rolled_back
        sa.Column("state", sa.Text(), nullable=False,
                  server_default=sa.text("'deploying'")),
        # [{"deployment_id": ..., "prior_runner_image": ...}] captured at drain
        # time — what was serving, and what it was pinned to, so rollback can
        # restore both.
        sa.Column("captured", JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("deploy_command_id", UUID(as_uuid=True)),
        sa.Column("error", sa.Text()),
        sa.Column("deadline", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_runner_upgrades_worker", "runner_upgrades", ["worker_id"])


def downgrade() -> None:
    op.drop_index("ix_runner_upgrades_worker")
    op.drop_table("runner_upgrades")
