# SPDX-License-Identifier: BUSL-1.1
"""deployments.task — serve mode (chat|embed|rerank) (#254 embed/rerank)

Revision ID: 0006_deployment_task
Revises: 0005_node_commands
Create Date: 2026-08-15

A deployment now declares WHAT it serves: chat (default), embed, or rerank. The
node maps it to the engine's serve-mode flags (llama.cpp ``--embeddings`` /
``--reranking``; vLLM ``--task embed``/``score``) and the manager sets the
LiteLLM ``model_info.mode`` so ``/v1/embeddings`` + ``/v1/rerank`` route to this
deployment. Without it a deployed embedding model answered ``/v1/embeddings``
with "this server does not support embeddings" (the 0.91 playground gap).

Additive + back-compat: existing rows default to ``chat``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_deployment_task"
down_revision = "0005_node_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("task", sa.Text(), nullable=False, server_default=sa.text("'chat'")),
    )


def downgrade() -> None:
    op.drop_column("deployments", "task")
