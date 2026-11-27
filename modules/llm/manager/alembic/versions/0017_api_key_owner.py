# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#314: API-key owner attribution — the one real schema gap the three-tier
RBAC issue names. Metering was already per-key; this adds the per-*person*
identity a "my usage" / "who owns this key" view needs.

Nullable: existing rows (including the #350 seeded `playground-internal`
accounting identity from migration 0016, which has no human owner) are left
NULL rather than back-filled with a guess.

Revision ID: 0017_api_key_owner
Revises: 0016_playground_accounting
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_api_key_owner"
down_revision = "0016_playground_accounting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("owner_username", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "owner_username")
