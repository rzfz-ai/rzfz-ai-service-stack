# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#227 admission control: deployments.est_gb (estimated resident footprint).

Revision ID: 0011_deployment_est_gb
Revises: 0010_deployment_tags
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_deployment_est_gb"
down_revision = "0010_deployment_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("est_gb", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "est_gb")
