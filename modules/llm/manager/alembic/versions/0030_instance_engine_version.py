# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The engine build an instance actually runs (`deployment_instances.engine_version`).

The worker's `engine_version` label is the node's DEFAULT runner. A deployment
pinned to another runner image ran another build while the console showed the
default's. The node now reads the build from the running engine (its /props)
and reports it per instance.

NULL until the node reports it (older agents, engines without /props).

Revision ID: 0030_instance_engine_version
Revises: 0029_unverified_ack
"""
from alembic import op
import sqlalchemy as sa

revision = "0030_instance_engine_version"
down_revision = "0029_unverified_ack"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployment_instances", sa.Column("engine_version", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployment_instances", "engine_version")
