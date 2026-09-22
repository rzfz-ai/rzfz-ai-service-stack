"""baseline (O1) — establish the alembic bookkeeping head

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-10

Empty baseline: it only proves the Alembic plumbing runs end-to-end against
a fresh ``llm_manager_db`` and stamps ``alembic_version``. The token-only
table set is created by the follow-on migration ``0002_token_only_schema``
(task O2).
"""
from __future__ import annotations

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
