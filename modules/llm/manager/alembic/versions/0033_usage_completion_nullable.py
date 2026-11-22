"""`usage_events.completion_tokens` may be NULL: the count is unknown.

#3188: a non-streaming request whose client left (#3041) is cancelled before
the engine answers, so the engine's token count never reaches the manager. Such
a row was written with completion_tokens = 0, which claims "nothing generated"
although the engine had produced tokens (about 398 in the measured case). The
row now carries an estimate (the model's measured output rate × the seconds the
request ran, estimated=true) or, with no rate measured yet, NULL. Every reader
sums the column with coalesce, so a NULL adds nothing to a total.

Revision ID: 0033_usage_completion_nullable
Revises: 0032_usage_end_reason
"""
from alembic import op
import sqlalchemy as sa

revision = "0033_usage_completion_nullable"
down_revision = "0032_usage_end_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("usage_events", "completion_tokens", existing_type=sa.Integer(),
                    nullable=True, existing_server_default=sa.text("0"))


def downgrade() -> None:
    op.execute("UPDATE usage_events SET completion_tokens = 0 WHERE completion_tokens IS NULL")
    op.alter_column("usage_events", "completion_tokens", existing_type=sa.Integer(),
                    nullable=False, existing_server_default=sa.text("0"))
