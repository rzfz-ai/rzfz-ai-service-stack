# SPDX-License-Identifier: BUSL-1.1
"""deployment_instances.detail — human phase detail (pull %, failure reason)

Revision ID: 0008_instance_detail
Revises: 0007_instance_keying
Create Date: 2026-08-15

#287/#286: carries a short human detail for the instance's current phase — e.g.
"pulling 42%" while weights download (#287 pull-on-deploy) or the failure reason
when an engine won't load. Nullable + additive; the console surfaces it next to
the status.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_instance_detail"
down_revision = "0007_instance_keying"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployment_instances", sa.Column("detail", sa.Text()))


def downgrade() -> None:
    op.drop_column("deployment_instances", "detail")
