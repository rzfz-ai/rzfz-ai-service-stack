#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# fix-gpustack-backend-null.sh
# ==============================================================================
# One-off remediation for standard models that were registered (via
# razzfazz-post-install.sh / core/llm/sync.py) BEFORE the sync.py fix that sets
# the `backend` field at create time.
#
# Symptom: the models run fine (gpustack auto-selects llama-box at deploy), but
# their `backend` column is NULL in gpustack_db. In the gpustack UI the Backend
# field is MANDATORY and DISABLED (it can only be chosen at create time), so a
# NULL value fails the form's required-field validation — and you cannot save
# ANY edit on the deployed model (replicas, backend_parameters, …).
#
# Fix: set backend = 'llama-box' directly in the DB for every model where it is
# NULL. All standard razzfazz models are GGUF → llama-box. This is done in the
# database, NOT via an API PUT, on purpose: an API spec-change PUT would force a
# model redeploy (scale 0→1, full reload + Strix-Halo warmup). The DB update is
# reflected live by gpustack (UI/API) with NO restart and NO instance redeploy —
# the running runners are untouched.
#
# Safe to re-run (idempotent: only touches rows where backend IS NULL).
# Source-side fix so NEW installs don't need this: core/llm/sync.py payload now
# sends "backend": model.get("backend", "llama-box").
#
# Usage:  ./scripts/fix-gpustack-backend-null.sh
#         STACK_DIR=/path/to/stack ./scripts/fix-gpustack-backend-null.sh
# ==============================================================================
set -euo pipefail

STACK_DIR="${STACK_DIR:-$HOME/razzfazz-ai-service-stack}"
ENV_FILE="${STACK_DIR}/.env"
[ -f "$ENV_FILE" ] || { echo "ERROR: .env not found at $ENV_FILE (set STACK_DIR)"; exit 1; }

# Targeted reads only — never source an operator-edited .env.
val() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'; }
PGU="$(val POSTGRES_USER)";    PGU="${PGU:-docker}"
DB="$(val GPUSTACK_DB)";       DB="${DB:-gpustack_db}"
KEY="$(val GPUSTACK_API_KEY)"
PORT="${GPUSTACK_PORT:-9090}"
BACKEND="${GPUSTACK_BACKEND:-llama-box}"

echo "==> gpustack DB=$DB  backend-target=$BACKEND"
echo "==> model backends BEFORE:"
docker exec postgres psql -U "$PGU" -d "$DB" -tAc \
  "SELECT id||' | '||name||' | '||COALESCE(backend,'<NULL>') FROM models ORDER BY id;" \
  | sed 's/^/    /'

NULLS="$(docker exec postgres psql -U "$PGU" -d "$DB" -tAc \
  "SELECT count(*) FROM models WHERE backend IS NULL;" | tr -d '[:space:]')"
if [ "${NULLS:-0}" = "0" ]; then
    echo "==> Nothing to do — no models with NULL backend."
    exit 0
fi

echo "==> setting backend='${BACKEND}' on ${NULLS} model(s) with NULL backend (non-disruptive, no redeploy)…"
docker exec postgres psql -U "$PGU" -d "$DB" -c \
  "UPDATE models SET backend='${BACKEND}' WHERE backend IS NULL;"

echo "==> model backends AFTER:"
docker exec postgres psql -U "$PGU" -d "$DB" -tAc \
  "SELECT id||' | '||name||' | '||COALESCE(backend,'<NULL>') FROM models ORDER BY id;" \
  | sed 's/^/    /'

if [ -n "$KEY" ]; then
    echo "==> verify live via API (no restart) + instances still running:"
    curl -s -H "Authorization: Bearer $KEY" "http://127.0.0.1:${PORT}/v1/models?per_page=100" 2>/dev/null \
      | python3 -c "import sys,json;d=json.load(sys.stdin);[print('    %-18s backend=%r  replicas=%s ready=%s'%(m['name'],m.get('backend'),m.get('replicas'),m.get('ready_replicas'))) for m in d.get('items',[])]" \
      || echo "    (API verify skipped — could not reach gpustack on :$PORT)"
fi

echo "==> Done. Open any model in the gpustack UI — editing/saving now works."
