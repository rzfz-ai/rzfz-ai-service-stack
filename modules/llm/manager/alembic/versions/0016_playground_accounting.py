# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#350: reserved identity so playground consumption lands in usage_events.

The box is the billing source of truth (#254), but the playground consumed
real GPU time with NO usage_events row — SUM(usage_events) under-reported
actual consumption with no way to measure the gap. usage_events.api_key_id
and cost_center_id are NOT NULL FKs, so recording needs a seeded identity:

* cost_center ``operator-playground``
* api_key with key_prefix ``playground-internal``, status ``internal``

The key can NEVER authenticate on /v1: its key_hash is the digest of random
bytes generated inside this migration and discarded (nothing to present),
AND auth requires status == 'active' (app/auth.py:101) — two independent
locks. It exists purely as an accounting identity.

Idempotent: inserts only when absent, so re-running or a box that already
has the rows is a no-op.

Revision ID: 0016_playground_accounting
Revises: 0015_unique_identity
"""
from alembic import op

revision = "0016_playground_accounting"
down_revision = "0015_unique_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO cost_centers (id, name, team)
        SELECT gen_random_uuid(), 'operator-playground', 'operator'
        WHERE NOT EXISTS (
            SELECT 1 FROM cost_centers WHERE name = 'operator-playground')
    """)
    op.execute("""
        INSERT INTO api_keys (id, key_hash, key_prefix, cost_center_id, status)
        SELECT gen_random_uuid(),
               sha256(gen_random_uuid()::text::bytea || gen_random_uuid()::text::bytea),
               'playground-internal',
               (SELECT id FROM cost_centers WHERE name = 'operator-playground'),
               'internal'
        WHERE NOT EXISTS (
            SELECT 1 FROM api_keys WHERE key_prefix = 'playground-internal')
    """)


def downgrade() -> None:
    # usage rows referencing the identity keep it meaningful — do not delete
    # the FK targets from under them. Downgrade is a no-op by design.
    pass
