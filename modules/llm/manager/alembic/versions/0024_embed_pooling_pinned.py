# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2435 — existing embedding deployments keep the pooling their indexes were built with.

Revision ID: 0024_embed_pooling_pinned
Revises: 0023_instance_retiring_since

Until 2026.09-ga.2 the node forced `--pooling mean` on every llama.cpp
embedding engine. Qwen3-Embedding declares LAST-token pooling in its GGUF, and
GPUStack (every 2026.08 box) ran it that way. The node now applies the model's
own pooling. Two populations follow from that, with opposite needs:

* A box whose manager is created by this release (a 2026.08 box upgrading, a
  fresh install) has no deployment when this migration runs: the manager's
  entrypoint applies migrations before it serves, and deployments exist only
  through its API. Nothing is pinned; the model's own pooling applies, which
  matches the GPUStack-built indexes (2026.08-ga.15 shipped no manager at all).
* A box that already ran the manager (2026.09-ga / 2026.09-ga.1) built its
  indexes with `mean`. Switching its engine to `last` would put every new query
  in a different space from every stored vector — the 0.208 failure, reversed.
  Its embedding deployments get `pooling: mean` written into their params, so
  the next launch keeps today's space. `rzfz status` measures each index; an
  operator who re-indexes removes the pin in the console.

An explicit pooling in any spelling (0.208 carries `pooling: last`) is left
alone, and only llama.cpp deployments are touched: `--pooling` is a llama.cpp
flag. Downgrade removes nothing — a pin that an operator may have confirmed is
indistinguishable from this one, and the older node forced `mean` anyway.
"""
from __future__ import annotations

from alembic import op

revision = "0024_embed_pooling_pinned"
down_revision = "0023_instance_retiring_since"
branch_labels = None
depends_on = None

EMBED_TASKS = ("embed", "embedding", "embeddings")
PINNED = "mean"

# A params key names pooling when it normalises to `pooling` the way the node's
# normalize_flag does: leading dashes stripped, underscores to dashes, lowercase.
PIN_SQL = f"""
UPDATE deployments
   SET params = coalesce(params, '{{}}'::jsonb) || jsonb_build_object('pooling', '{PINNED}')
 WHERE engine = 'llamacpp'
   AND lower(trim(coalesce(task, ''))) IN {EMBED_TASKS!r}
   AND NOT EXISTS (
         SELECT 1 FROM jsonb_object_keys(coalesce(params, '{{}}'::jsonb)) AS k
          WHERE replace(lower(ltrim(k, '-')), '_', '-') = 'pooling')
"""


def upgrade() -> None:
    op.execute(PIN_SQL)


def downgrade() -> None:
    pass
