# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2394: a deployment of a model that is not a verified catalogue entry
records who acknowledged the risk notice, and when (`{"user", "at"}`).

NULL for every verified deployment and for every row that predates this
migration.

Revision ID: 0029_unverified_ack
Revises: 0028_deployment_pins
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0029_unverified_ack"
down_revision = "0028_deployment_pins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("unverified_ack", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "unverified_ack")
