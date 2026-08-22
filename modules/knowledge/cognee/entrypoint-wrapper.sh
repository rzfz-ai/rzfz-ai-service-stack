#!/bin/bash
# ==============================================================================
# Custom entrypoint for Cognee with FalkorDB adapter registration
# Registers the FalkorDB community adapter before running the original entrypoint.
# ==============================================================================

set -e

# Register FalkorDB adapter if GRAPH_DATABASE_PROVIDER is falkor
if [ "${GRAPH_DATABASE_PROVIDER}" = "falkor" ]; then
    echo "Verifying FalkorDB adapter registration..."
    python -c "import cognee_community_hybrid_adapter_falkor.register; print('FalkorDB adapter registered successfully.')"
fi

# Disable aiohttp transport in litellm (use httpx instead)
# Required because GPUStack returns duplicate 'Server' headers which aiohttp rejects
python -c "import litellm; litellm.disable_aiohttp_transport = True; print(f'LiteLLM aiohttp transport disabled (using httpx).')"

# #186: proactive ladybug 0.16 -> 0.17 graph-format migration + empty-graph
# guards. cognee's built-in auto-migration never fires for this crossing (a 0.17
# engine opens a 0.16 store without error and needs_migration() only flags
# <0.15.0), so the knowledge-graph viz silently reads empty. Run our
# storage-format-gated, backup-before-migrate, non-fatal helper BEFORE the server
# boots — it no-ops on fresh/current stores, backs up before any migration, warns
# loudly (instead of failing silently) on the access-control per-dataset gap
# (Defect 2), honours COGNEE_KUZU_AUTO_MIGRATE=false, and never blocks startup.
if [ -f /app/kuzu-migrate.py ]; then
    echo "Checking ladybug graph on-disk format (#186)..."
    python /app/kuzu-migrate.py || true
fi

# Execute the original entrypoint
exec /app/entrypoint.sh "$@"
