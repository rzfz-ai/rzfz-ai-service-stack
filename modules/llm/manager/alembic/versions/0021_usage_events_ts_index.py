# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1153: index `usage_events (ts)` so the time-windowed reports stop seq-scanning.

Every usage read is a RANGE over `ts` with no leading equality:

* `_usage_rows` (`/api/usage`, `/ui/usage`) filters `ts >= now() - 90 days`;
* `_usage_series` (`/api/usage/series`) filters `[from, to)`;
* the retention sweep (`app/retention.py`) deletes by `ts <`.

`usage_events` already carries two composite indexes from 0002 —
`ix_usage_events_cost_center_ts (cost_center_id, ts)` and
`ix_usage_events_api_key_ts (api_key_id, ts)`. Neither serves those reads: a
btree can only range-scan on `ts` once every preceding column is pinned by an
equality, and none of the three call sites filters on a cost-centre or a key.
So the planner falls back to a seq scan whose cost grows with every request the
box ever served. `(ts)` alone is the missing access path, and it is also the
one a retention delete can use.

The composite `(cost_center_id, ts)` the issue proposes as a candidate is
therefore NOT created here: it already exists, and a second identical index
would only double the write amplification on the hottest insert path in the
manager. `tests/unit/llm-manager/test_1153_usage_events_ts_index.py` pins that
0002 still ships it, so this claim reddens if it is ever dropped.

Revision ID: 0021_usage_events_ts_index
Revises: 0020_worker_agent_rename
"""
from alembic import op

revision = "0021_usage_events_ts_index"
down_revision = "0020_worker_agent_rename"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_usage_events_ts"


def upgrade() -> None:
    op.create_index(INDEX_NAME, "usage_events", ["ts"])


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="usage_events")
