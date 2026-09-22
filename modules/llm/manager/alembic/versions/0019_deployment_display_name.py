# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#284: deployments.display_name — a console-only label distinct from the
client-facing `model_name` (the LiteLLM routing id the router keys on and API
clients call, which must NOT change). Nullable: NULL means "show model_name".
Node registration keys on `model_name` and never writes this column.

Revision ID: 0019_deployment_display_name
Revises: 0018_worker_display_name
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_deployment_display_name"
down_revision = "0018_worker_display_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("display_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "display_name")
