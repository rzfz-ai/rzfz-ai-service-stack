# SPDX-License-Identifier: BUSL-1.1
"""node_commands — manager→node command queue (#261 control plane)

Revision ID: 0005_node_commands
Revises: 0004_runtime_settings
Create Date: 2026-08-12

The manager enqueues a command for a worker (restart/stop/load/unload an engine,
tail logs); the worker's worker-agent LONG-POLLS for its pending commands
(outbound-only — works for NAT'd remote workers), executes via its drivers, and
POSTs the result back. Fail-closed auth: enqueue = admin; claim/result =
node-key. No arbitrary shell — only enumerated driver ops.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_node_commands"
down_revision = "0004_runtime_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_commands",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("worker_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("args", sa.dialects.postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("result", sa.dialects.postgresql.JSONB()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_index("ix_node_commands_worker_status", "node_commands", ["worker_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_node_commands_worker_status", table_name="node_commands")
    op.drop_table("node_commands")
