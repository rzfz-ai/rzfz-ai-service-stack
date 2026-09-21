# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#284: workers.display_name — a manager-owned display label distinct from the
node-registered `name`. Nullable: NULL means "show the registered name", which
is the pre-#284 behaviour. Node re-registration keys on `name` and never writes
this column, so a rename survives the node's next report (the "stable-id alias").

Revision ID: 0018_worker_display_name
Revises: 0017_api_key_owner
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_worker_display_name"
down_revision = "0017_api_key_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("display_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "display_name")
