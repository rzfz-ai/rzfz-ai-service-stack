#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# LLM Manager entrypoint: wait for postgres, run migrations, then serve.
set -eu

echo "[orchestrator] waiting for postgres ..."
tries=0
until python -c "import sqlalchemy,os; sqlalchemy.create_engine(os.environ['LLM_MANAGER_DATABASE_URL']).connect()" 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
        echo "[orchestrator] postgres not reachable after 60 tries — aborting" >&2
        exit 1
    fi
    sleep 2
done

# #359: unconditional on every start. Safe for ONE manager container — which is
# what the stack runs — but two starting together race on the same schema. Alembic
# takes a lock for the migration itself, so the realistic failure is the loser
# aborting and its container restarting, not a corrupt schema. Worth knowing
# before anyone scales this service to replicas.
echo "[orchestrator] applying database migrations (alembic upgrade head) ..."
alembic upgrade head

echo "[orchestrator] starting: $*"
exec "$@"
