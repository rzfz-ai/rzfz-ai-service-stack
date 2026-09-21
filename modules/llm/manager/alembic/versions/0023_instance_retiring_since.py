# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2019 — an instance can be marked "retire me once a replacement is serving".

Revision ID: 0023_instance_retiring_since
Revises: 0022_cutover_retires_gpustack

`apply-params` is unload-first: it stops the only engine, deletes the instance
row and only then starts a new one. With `replicas: 1` the deployment has no
`ready` instance in between, `router_config.generate_from_db` drops it from
`model_list` entirely, and the model is not slow during the restart — it is
ABSENT. Measured on 0.79, 4 threads on `/v1/rerank`: 17.76 s, 1744 requests
answered `404 no ready rerank engine for model 'qwen3-reranker'`.

#1867 already built the blue-green overlap for the runner switch, but it
sequences the two halves across `advance_runner_upgrade` — start the new engine,
wait until the router serves it, THEN retire the old containers. `apply-params`
is a single HTTP request and has nowhere to keep the second half.

This column is that memory. `retiring_since` on the OLD instance means: unload
me as soon as a READY sibling of the same deployment exists on this worker.

Why a timestamp and not a foreign key to the replacement: at the moment the
decision is made the replacement has no row at all. The instance row is created
by the node's own report (`api/workers.py`), so there is nothing to point at.
A timestamp also carries the one other thing this state needs — its age — so an
overlap whose new engine never becomes ready can be abandoned by the clock and
leave the old engine serving, which is the safe direction and the behaviour the
box had before the overlap existed.

Nullable with no default and no backfill: every existing instance is simply not
retiring, which is exactly true.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_instance_retiring_since"
down_revision = "0022_cutover_retires_gpustack"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployment_instances",
        sa.Column("retiring_since", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deployment_instances", "retiring_since")
