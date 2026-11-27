# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2392: deployments remember the catalogue pins they were created with, and
whether they are a verified catalogue model.

Every existing row reads `unverified` with no pins after this migration; the
reconciler's `backfill_catalogue_pins` gives the catalogue rows their pins on
its first pass.

Revision ID: 0028_deployment_pins
Revises: 0027_instance_replaced_by
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028_deployment_pins"
down_revision = "0027_instance_replaced_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("pins", JSONB, nullable=False,
                                           server_default=sa.text("'{}'::jsonb")))
    op.add_column("deployments", sa.Column("verification", sa.Text, nullable=False,
                                           server_default="unverified"))


def downgrade() -> None:
    op.drop_column("deployments", "verification")
    op.drop_column("deployments", "pins")
