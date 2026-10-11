"""per-backend api_key on deployment_instances (P2-B3)

Revision ID: 0003_deployment_instance_api_key
Revises: 0002_token_only_schema
Create Date: 2026-08-11

A registered backend (e.g. a Mac Ollama gateway or an external endpoint) can
carry its OWN upstream key. The manager stores it on the instance and injects
it router-side (litellm_params.api_key) so the router can authenticate to that
backend — the client never sees it. NULL = no key (the default "none").
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_deployment_instance_api_key"
down_revision = "0002_token_only_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployment_instances",
        sa.Column("api_key", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deployment_instances", "api_key")
