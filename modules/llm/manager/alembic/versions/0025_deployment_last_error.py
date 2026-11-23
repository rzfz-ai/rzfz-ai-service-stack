# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2514: deployments.last_error — the console half of the admission-charge fix.
A scale-up refused for lack of VRAM room returns `admission_limited: true` but
otherwise leaves no trace on the deployment; this persists the refusal reason
so `GET /api/deployments` carries it. Nullable: NULL means nothing to report.

Revision ID: 0025_deployment_last_error
Revises: 0024_embed_pooling_pinned
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_deployment_last_error"
down_revision = "0024_embed_pooling_pinned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "last_error")
