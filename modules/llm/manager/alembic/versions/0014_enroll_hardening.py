# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#340: enrollment replay protection + per-worker key revocation.

Two small pieces of durable state:

* ``enroll_jti_spent`` — one row per USED enrollment token (keyed on the
  token's random jti). The exchange claims the jti with an INSERT … ON
  CONFLICT DO NOTHING; a second exchange finds the row and is refused. A
  table, not Valkey: CI and the api tier run without redis, the manager
  restarts must not reopen the replay window, and enrollments are rare.
  Rows expire functionally with the token TTL; a sweep on each enroll
  deletes rows older than the ceiling so the table stays bounded.

* ``workers.key_epoch`` — folded into the per-worker command-key HMAC.
  Bumping it (POST /api/workers/{id}/rotate-key) invalidates that ONE
  worker's credential instantly, so a compromised node no longer forces a
  fleet-wide node_key rotation. Epoch 0 keeps the legacy HMAC input so
  every already-enrolled node's key stays valid.

Revision ID: 0014_enroll_hardening
Revises: 0013_runner_upgrades
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_enroll_hardening"
down_revision = "0013_runner_upgrades"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enroll_jti_spent",
        sa.Column("jti", sa.Text, primary_key=True),
        sa.Column("worker_name", sa.Text, nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.add_column("workers", sa.Column("key_epoch", sa.Integer, nullable=False,
                                       server_default=sa.text("0")))


def downgrade() -> None:
    op.drop_column("workers", "key_epoch")
    op.drop_table("enroll_jti_spent")
