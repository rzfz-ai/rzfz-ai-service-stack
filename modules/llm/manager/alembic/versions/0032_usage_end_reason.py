"""Why a request ended, when it did not end normally (`usage_events.end_reason`).

#3041: a generation whose caller gave up (cognee, LightRAG: a client timeout of
300 s while the engine generated 117k tokens for 100 min) left no usage row at
all, so the spend was invisible and nothing told an operator it had happened.
Such requests are now recorded, and this column says why they ended:
`client_disconnected`, `upstream_timeout`, `upstream_error` or `token_ceiling`
(the box's per-request token ceiling stopped the generation). NULL for a
request that ended normally, and for every row written before this revision.

Revision ID: 0032_usage_end_reason
Revises: 0031_instance_runner_image
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_usage_end_reason"
down_revision = "0031_instance_runner_image"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("end_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_events", "end_reason")
