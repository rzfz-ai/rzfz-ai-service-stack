# SPDX-License-Identifier: BUSL-1.1
"""runtime_settings key/value overrides (Phase-3 S5)

Revision ID: 0004_runtime_settings
Revises: 0003_deployment_instance_api_key
Create Date: 2026-08-11

A tiny key/value table for operator-set runtime overrides that the management
UI can change WITHOUT a container restart (the env-derived Settings dataclass
is immutable + read fresh per call). Currently the single consumer is the
billing-meter CAP mode (LLM_MANAGER_METERING_MODE): the env value is the
default/floor, and a row here overrides it live. Kept generic so future
UI-settable knobs reuse it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_runtime_settings"
down_revision = "0003_deployment_instance_api_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
