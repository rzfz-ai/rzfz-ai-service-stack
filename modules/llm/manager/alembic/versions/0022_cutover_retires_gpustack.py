# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1640 + #1634 — the cutover reaches the manager's own rows.

Revision ID: 0022_cutover_retires_gpustack
Revises: 0021_usage_events_ts_index

MEASURED on 0.79 after a real ga.15 → main cutover upgrade (C6/C7). The
migration moved profiles, compose and the eight stores; the manager's DATABASE
kept the old world:

    model_name       | engine   | status  |     workers
    -----------------+----------+---------+--------------------------------
     nomic-embed-text| gpustack | pending |   gpustack  ready   (dead 1½h)
     qwen3-embedding | gpustack | pending |   master    ready
     qwen3-reranker  | gpustack | pending |
     qwen3.6         | llamacpp | pending |

Three of the four standard-set models asked for a `gpustack` worker. A Manager
box has none — except the leftover ROW, which points at the container name
`gpustack` that the profile migration removed. So the three sat on `pending`
for ever, with no error anywhere: either placement found no candidate at all,
or it found one that does not exist. Both end the same way.

The two halves are one migration on purpose, because they are one state:

* `deployments.engine = 'gpustack'` → `llamacpp`. vLLM was retired in #1518 and
  GPUStack 2.x in #1447, so llama.cpp is the container engine this fleet has;
  `mlx` rows belong to Mac gateways, which are registered as external backends
  (#307) and never carried `gpustack`.
* the worker row for the retired runtime goes, together with the instances that
  named it. An instance on a worker that does not exist cannot start and cannot
  be rescheduled while it holds the deployment's only placement — leaving it is
  what made the box look like it had somewhere to run.

WHY DELETE RATHER THAN MARK: a marked row still answers `GET /api/workers`, and
the console counts it ("NODES 2 · 2 ready"). Placement already skips it — a
stale heartbeat fails `_placeability` — so the row's only remaining effect is to
tell an operator that a worker exists. That is the effect being removed.

The helpers are pure so the rules can be RUN in a test without a database; the
SQL below does the same thing set-wise.
"""
from __future__ import annotations

from typing import Optional

from alembic import op

revision = "0022_cutover_retires_gpustack"
down_revision = "0021_usage_events_ts_index"
branch_labels = None
depends_on = None

#: the retired runtime, as it appears in `deployments.engine`, `workers.engine`
#: (label) and `workers.address` (the container name it dialled).
RETIRED_ENGINE = "gpustack"
#: what a container deployment runs on this fleet after #1518 (vLLM out) and
#: #1447 (GPUStack 2.x out).
REPLACEMENT_ENGINE = "llamacpp"


def migrated_engine(engine: Optional[str]) -> Optional[str]:
    """`gpustack` → `llamacpp`; everything else untouched.

    Deliberately an exact match: a worker or deployment an operator called
    e.g. `gpustack-archive` is a name, not the runtime.
    """
    return REPLACEMENT_ENGINE if engine == RETIRED_ENGINE else engine


def is_retired_worker(*, engine: Optional[str], address: Optional[str]) -> bool:
    """Is this row the leftover of the retired runtime?

    BOTH signals must agree — the row says it runs the retired engine AND it
    dials the service name that the profile migration removed. Either alone is
    not enough: an operator may have named a real worker `gpustack`, and a row
    with `engine=gpustack` that dials a live agent is a worker whose label is
    merely stale (the engine rewrite above fixes that one instead).
    """
    if engine != RETIRED_ENGINE:
        return False
    addr = (address or "").split(":", 1)[0]
    return addr == RETIRED_ENGINE


def upgrade() -> None:
    # 1. deployments stop asking for a runtime this fleet no longer has (#1640)
    op.execute(
        f"""
        UPDATE deployments
           SET engine = '{REPLACEMENT_ENGINE}'
         WHERE engine = '{RETIRED_ENGINE}'
        """
    )
    # 2. the instances that named the retired worker, then the worker (#1634).
    #    Order matters: deployment_instances.worker_id has no ON DELETE, so the
    #    row has to go first or the delete below fails on the constraint.
    op.execute(
        f"""
        DELETE FROM deployment_instances
         WHERE worker_id IN (
               SELECT id FROM workers
                WHERE labels->>'engine' = '{RETIRED_ENGINE}'
                  AND split_part(address, ':', 1) = '{RETIRED_ENGINE}')
        """
    )
    op.execute(
        f"""
        DELETE FROM workers
         WHERE labels->>'engine' = '{RETIRED_ENGINE}'
           AND split_part(address, ':', 1) = '{RETIRED_ENGINE}'
        """
    )


def downgrade() -> None:
    """Not reversible, and saying so is the honest answer.

    The engine rewrite cannot be undone without knowing which rows were
    `gpustack` before — and the deleted worker row cannot be recreated at all,
    because the runtime it described is gone from the box. A downgrade past
    this point is a restore, not a migration (#1621 covers that direction).
    """
    pass
