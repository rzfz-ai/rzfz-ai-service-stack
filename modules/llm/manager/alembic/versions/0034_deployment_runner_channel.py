# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The runner CHANNEL a deployment runs on (`deployments.runner_channel`, #3403).

A node serves two llama.cpp builds per hardware line: the stable default and an
experimental one, for model architectures only a newer llama.cpp knows. The
channel is stored, not resolved to an image here, because WHICH image is the
experimental one is a property of the worker the deployment lands on (its
hardware line, a CUDA card's compute capability) — the node resolves it at
every launch. An explicit `runner_image` pin still wins over the channel.

NULL = the stable channel (the node's default runner): every existing row is
unchanged.

Revision ID: 0034_deployment_runner_channel
Revises: 0033_usage_completion_nullable
"""
from alembic import op
import sqlalchemy as sa

revision = "0034_deployment_runner_channel"
down_revision = "0033_usage_completion_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("runner_channel", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "runner_channel")
