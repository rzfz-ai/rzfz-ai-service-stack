# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#549 R1: deployments.runner_image — which runner version serves this model.

Nullable on purpose: NULL means "the node's default for its hardware class"
(app/drivers/images.py on the node), which is exactly the pre-R1 behaviour, so
every existing deployment keeps doing what it did.

Revision ID: 0012_deployment_runner_image
Revises: 0011_deployment_est_gb
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_deployment_runner_image"
down_revision = "0011_deployment_est_gb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("runner_image", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "runner_image")
