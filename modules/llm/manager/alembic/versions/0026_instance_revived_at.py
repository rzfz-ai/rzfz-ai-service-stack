# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2509 fix round 2: deployment_instances.revived_at — durable evidence that
a row was restored from `lost` (#1455 SCH3) back to a reported status, so a
later SURPLUS reap (reconciler.py's `_surplus_reap_key`) can tell "this
replica was down and may already be covered by a rescue elsewhere" from "this
replica has been serving continuously" — a distinction neither `status` nor
`started_at` can make once the row is `ready` again (both get overwritten /
were never touched by the outage). Nullable: NULL means never revived, or the
reconciler already cleared it because there was no surplus left to break a
tie over.

Revision ID: 0026_instance_revived_at
Revises: 0025_deployment_last_error
"""
from alembic import op
import sqlalchemy as sa

revision = "0026_instance_revived_at"
down_revision = "0025_deployment_last_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployment_instances",
                  sa.Column("revived_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("deployment_instances", "revived_at")
