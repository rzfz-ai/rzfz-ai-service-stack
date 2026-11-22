# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2502 fix round 2: deployment_instances.replaced_by_instance_id — the
explicit pointer from a retiring row to the instance that is replacing it.

Round 1's recency/worker heuristics ("any ready, non-retiring sibling of the
same deployment, preferring the same worker") could not tell a PRE-EXISTING
replica (replicas > 1) from the engine actually launched to replace a
retiring row, and could not tell one concurrent overlap/reassign's
replacement from another's on the same deployment. Both mispairings retired
the wrong instance — break-before-make one level in, on the same deployment
this fix exists to protect.

`_enqueue_engine`'s caller (`reassign_deployment`, `_overlap_relaunch`) mints
the replacement's `instance_id` and creates its `scheduled` row in the SAME
transaction that marks the retiring row, so the pointer is knowable
immediately — nullable: NULL means "not retiring for a recorded reason", same
as `retiring_since` being NULL.

Revision ID: 0027_instance_replaced_by
Revises: 0026_instance_revived_at
"""
from alembic import op
import sqlalchemy as sa

revision = "0027_instance_replaced_by"
down_revision = "0026_instance_revived_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployment_instances",
                  sa.Column("replaced_by_instance_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployment_instances", "replaced_by_instance_id")
