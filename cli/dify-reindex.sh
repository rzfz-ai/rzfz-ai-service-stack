#!/usr/bin/env bash
set -euo pipefail
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# rzfz dify-reindex — re-embed Dify knowledge bases whose embedding model no
# provider serves (#2427). Dry run by default: lists every KB and what would
# happen. --apply enqueues Dify's own re-index as each KB's OWNER and waits a
# bounded time for it; it runs for hours on a large KB (one embedding request
# at a time). Source documents are never deleted. Safe to re-run: finished KBs
# are skipped, failed ones (including documents left in "error") retried.
usage() {
    cat <<'EOF2'
Usage: rzfz dify-reindex [--dataset <id>] [--apply]

  (no flags)        dry run: list every knowledge base and the action
  --dataset <id>    only this knowledge base
  --apply           enqueue the re-embedding for KBs marked "reembed", then wait
                    up to RZFZ_DIFY_REINDEX_WAIT_SECONDS (default 300) for it;
                    a KB still running then is reported "pending"

Exit: 0 nothing to do / applied · 2 Dify not reachable · 3 dry run found KBs to
re-embed · 4 --apply: at least one KB failed or its documents ended in "error"
(re-run to retry) · 64 bad usage
EOF2
}
dataset="" apply=0
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --apply) apply=1 ;;
        --dataset)
            [ $# -ge 2 ] || { echo "--dataset needs an id" >&2; usage >&2; exit 64; }
            dataset="$2"; shift ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 64 ;;
    esac
    shift
done
if [ -n "$dataset" ] && ! [[ "$dataset" =~ ^[0-9A-Fa-f-]{8,64}$ ]]; then
    echo "--dataset expects a dataset id (UUID), got: $dataset" >&2; exit 64
fi
args=()
[ -z "$dataset" ] || args+=(--dataset "$dataset")
wait_s="${RZFZ_DIFY_REINDEX_WAIT_SECONDS:-300}"
if ! [[ "$wait_s" =~ ^[0-9]{1,6}$ ]]; then
    echo "RZFZ_DIFY_REINDEX_WAIT_SECONDS expects seconds, got: $wait_s" >&2; exit 64
fi
[ "$apply" -eq 0 ] || args+=(--apply --wait "$wait_s")

RZFZ_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
running="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
if ! grep -qx dify-api <<<"$running"; then
    echo "dify-api is not running" >&2; exit 2
fi
rc=0
docker exec -i -w /app/api dify-api /app/api/.venv/bin/python - ${args[@]+"${args[@]}"} \
    < "$RZFZ_ROOT/modules/dify/scripts/reindex_datasets.py" || rc=$?
case "$rc" in
    0) ;;
    3) echo "Knowledge bases above marked \"reembed\" need a re-embedding. Run: rzfz dify-reindex --apply" ;;
    4) echo "Some knowledge bases failed (see \"error\" above); fix the cause and re-run: rzfz dify-reindex --apply" >&2 ;;
    *) echo "dify-reindex failed (rc=$rc)" >&2; exit 2 ;;
esac
exit "$rc"
