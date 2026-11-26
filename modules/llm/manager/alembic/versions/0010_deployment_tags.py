# SPDX-License-Identifier: BUSL-1.1
"""deployments.tags — free-form operator tags, central catalog derived from these

Revision ID: 0010_deployment_tags
Revises: 0009_deployment_source
Create Date: 2026-08-15

#296: let the operator tag a deployment (chips + create-new). Tags are a SEPARATE
column — NOT stored in `params`, because params flow to the engine as CLI flags and
a `--tags` flag would crash llama.cpp. The "central" tag catalog (GET /api/tags) is
derived from the distinct tags across all deployments. Additive.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_deployment_tags"
down_revision = "0009_deployment_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("tags", JSONB(), nullable=False,
                                           server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("deployments", "tags")
