# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The runner image an instance's engine container actually runs
(`deployment_instances.runner_image`).

A deployment whose `runner_image` is empty runs on the node's DEFAULT runner,
and the default moves with the node agent: on a box measured during a runner
upgrade, both engines' rows were empty, so the upgrade recorded "previous
image: none" and a rollback had nothing to go back to — the node's current
default was already the NEW build. The node now reports the image of each
engine container, and the upgrade's capture records it.

NULL until the node reports it (older agents).

Revision ID: 0031_instance_runner_image
Revises: 0030_instance_engine_version
"""
from alembic import op
import sqlalchemy as sa

revision = "0031_instance_runner_image"
down_revision = "0030_instance_engine_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployment_instances", sa.Column("runner_image", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployment_instances", "runner_image")
