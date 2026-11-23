# SPDX-License-Identifier: BUSL-1.1
"""deployments.source_files + hf_repo — persist the weight source for replicas

Revision ID: 0009_deployment_source
Revises: 0008_instance_detail
Create Date: 2026-08-15

#298: raising a deployment's replica count must be able to SCHEDULE the extra
instances, which needs the weight files (+ HF source to fetch them) — those were
previously only carried in the transient load command, not on the deployment.
Persist them so the replica scheduler can place another instance. Additive.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_deployment_source"
down_revision = "0008_instance_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("source_files", JSONB(), nullable=False,
                                           server_default=sa.text("'[]'::jsonb")))
    op.add_column("deployments", sa.Column("hf_repo", sa.Text()))


def downgrade() -> None:
    op.drop_column("deployments", "hf_repo")
    op.drop_column("deployments", "source_files")
