#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
set -eo pipefail

# Validate database name - only alphanumeric and underscore allowed
validate_db_name() {
    local db_name=$1
    if [[ ! "$db_name" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]]; then
        echo "ERROR: Invalid database name: $db_name" >&2
        return 1
    fi
    return 0
}

# Generate CREATE DATABASE statement if variable is not empty and valid.
# Idempotent: uses the SELECT/\gexec pattern so re-running against a postgres
# instance that already has the DB is a silent no-op rather than a hard error.
# Postgres has no `CREATE DATABASE IF NOT EXISTS`; \gexec lets us guard the
# CREATE behind a NOT EXISTS check without DO blocks (which can't run CREATE
# DATABASE because it's outside any transaction).
generate_create_db_sql() {
    local db_name=$1
    if [ -n "$db_name" ]; then
        if ! validate_db_name "$db_name"; then
            return 1
        fi
        echo "SELECT 'CREATE DATABASE \"$db_name\"' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db_name')\\gexec"
    fi
}

# Funktion, die CREATE EXTENSION Anweisungen generiert, wenn die Variable nicht leer ist
generate_dify_extensions_sql() {
    local db_name=$1
    if [ -n "$db_name" ]; then
        # Wichtig: \c verbindet sich mit der Dify-DB, um die Extensions im richtigen Kontext zu erstellen
        echo "\\c \"$db_name\""
        echo "CREATE EXTENSION IF NOT EXISTS vector;"
        echo "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"
    fi
}

# Generate extensions for LightRAG database (pgvector + uuid-ossp)
generate_lightrag_extensions_sql() {
    local db_name=$1
    if [ -n "$db_name" ]; then
        echo "\\c \"$db_name\""
        echo "CREATE EXTENSION IF NOT EXISTS vector;"
        echo "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"
    fi
}

# Generate extensions for Cognee database (pgvector + uuid-ossp)
generate_cognee_extensions_sql() {
    local db_name=$1
    if [ -n "$db_name" ]; then
        echo "\\c \"$db_name\""
        echo "CREATE EXTENSION IF NOT EXISTS vector;"
        echo "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"
    fi
}

# Generate CREATE DATABASE for Synapse — requires C locale (non-negotiable for Matrix).
# Same idempotent pattern as generate_create_db_sql.
generate_synapse_db_sql() {
    local db_name=$1
    if [ -n "$db_name" ]; then
        if ! validate_db_name "$db_name"; then
            return 1
        fi
        # Synapse requires C locale: https://element-hq.github.io/synapse/latest/postgres.html
        echo "SELECT 'CREATE DATABASE \"$db_name\" ENCODING ''UTF8'' LC_COLLATE = ''C'' LC_CTYPE = ''C'' TEMPLATE template0' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db_name')\\gexec"
    fi
}

# Generate CREATE USER + GRANT for a per-service database user.
# Falls back to superuser access if the _DB_USER variable is empty (backward compat).
generate_service_user_sql() {
    local db_name=$1
    local db_user=$2
    local db_pass=$3
    if [ -n "$db_user" ] && [ -n "$db_pass" ] && [ -n "$db_name" ]; then
        # Escape single quotes in password
        local safe_pass="${db_pass//\'/\'\'}"
        echo "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${db_user}') THEN CREATE ROLE \"${db_user}\" LOGIN PASSWORD '${safe_pass}'; END IF; END \$\$;"
        # BUG-6 (cascade-password fix, 2026-05-16): re-apply the password to the
        # existing role on every reconcile. Without this, the conditional CREATE
        # above skips already-existing roles and an upgrade that regenerates
        # .env secrets ends with postgres rejecting the new password —
        # authentik-migrate-reconcile then bails with "permission denied for
        # table django_migrations". Idempotent: ALTER USER on an unchanged
        # password is a no-op.
        echo "ALTER USER \"${db_user}\" WITH PASSWORD '${safe_pass}';"
        echo "GRANT ALL PRIVILEGES ON DATABASE \"${db_name}\" TO \"${db_user}\";"
        # Grant schema privileges after connecting to the database
        echo "\\c \"${db_name}\""
        echo "GRANT ALL ON SCHEMA public TO \"${db_user}\";"
        echo "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO \"${db_user}\";"
        echo "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO \"${db_user}\";"
        # BUG-6 follow-up (2026-05-16): ALTER DEFAULT PRIVILEGES only
        # covers FUTURE tables. On an upgrade, the service's tables
        # already exist (e.g. Authentik's django_migrations from the
        # original install) and the reconcile would otherwise see
        # "permission denied" on them. Apply grants to existing
        # tables + sequences explicitly. Idempotent; no-op if nothing
        # to grant.
        echo "GRANT ALL ON ALL TABLES IN SCHEMA public TO \"${db_user}\";"
        echo "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO \"${db_user}\";"
        echo "\\c \"${POSTGRES_DB}\""
    fi
}

# ONYX-SCOPED privilege grant (#26 — v2026.07-rc1 clean-install blocker).
# Onyx v4 (bumped v3.2.12 → v4.2.3 this cycle) ships a Knowledge-Graph feature
# whose Alembic migrations `495cb26ce93e` ("create knowledge graph tables") and
# `3b9f09038764` ("add read only kg user") issue `CREATE ROLE` for the
# read-only KG role. The generic per-service helper above creates onyx_user as a
# plain `CREATE ROLE … LOGIN PASSWORD …` with NO `CREATEROLE` attribute
# (rolcreaterole=false), so `onyx-api`'s `alembic upgrade head` dies with
#   asyncpg.InsufficientPrivilegeError: permission denied to create role
# → onyx-api crash-loops, web UI shows "The backend is currently unavailable".
#
# Fix: grant `CREATEROLE` to onyx_user ONLY (do NOT touch the generic helper —
# every other service stays without CREATEROLE). This is the least-privilege
# option that unblocks the migration and is strictly LESS privilege than onyx's
# upstream default deployment, which runs onyx as the postgres SUPERUSER.
# Role-name agnostic on purpose: onyx picks the KG role name internally, so we
# grant the create capability rather than pre-creating a fixed role (a bare
# upstream `CREATE ROLE` would collide with a pre-created one).
#
# Idempotent + guarded:
#  - IF EXISTS on the role → never errors if onyx_user wasn't created (e.g.
#    ONYX_DB / ONYX_DB_PASSWORD empty, or the docker-superuser fallback where
#    ONYX_DB_USER is empty and onyx connects as the superuser, which already
#    has CREATEROLE).
#  - ALTER ROLE … CREATEROLE is a no-op when the attribute is already set.
# Runs on BOTH clean install (init-db.sh via /docker-entrypoint-initdb.d/) AND
# upgrade (the postgres-db-reconcile one-shot re-runs this same script on every
# `compose up`, and onyx-api depends on it via service_completed_successfully —
# so the grant lands before onyx's alembic migration on an onyx v3 → v4 box too).
# Least-privilege MONITORING role for the postgres-exporter sidecar (#253 X5 S3).
# The exporter is a third-party image on an opt-in profile; upstream documents a
# dedicated role with `pg_monitor` for exactly this, and the rest of the stack
# already mints a <svc>_user per service. It must NEVER hold the cluster
# superuser: it only reads pg_stat_* views.
#
# Idempotent (same BUG-6 pattern as generate_service_user_sql): create-if-absent,
# then re-apply the password and the grant on every reconcile, so a box whose
# .env secrets were regenerated keeps working. The explicit NOSUPERUSER /
# NOCREATEDB / NOCREATEROLE downgrades a role that a previous install may have
# created with more than it needs.
generate_monitor_user_sql() {
    local db_user=$1
    local db_pass=$2
    if [ -n "$db_user" ] && [ -n "$db_pass" ]; then
        if ! validate_db_name "$db_user"; then
            return 1
        fi
        local safe_pass="${db_pass//\'/\'\'}"
        echo "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${db_user}') THEN CREATE ROLE \"${db_user}\" LOGIN PASSWORD '${safe_pass}'; END IF; END \$\$;"
        echo "ALTER USER \"${db_user}\" WITH PASSWORD '${safe_pass}';"
        echo "ALTER ROLE \"${db_user}\" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;"
        echo "GRANT pg_monitor TO \"${db_user}\";"
        echo "GRANT CONNECT ON DATABASE \"${POSTGRES_DB}\" TO \"${db_user}\";"
    fi
}

generate_onyx_createrole_sql() {
    local db_user=$1
    if [ -n "$db_user" ]; then
        echo "DO \$\$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = '${db_user}') THEN ALTER ROLE \"${db_user}\" CREATEROLE; END IF; END \$\$;"
    fi
}

# Alle SQL-Befehle in einer Variable zusammenbauen
# Dies ist robuster als mehrere here-documents
SQL_COMMANDS=$(cat <<-EOF
    $(generate_create_db_sql "$OPENWEBUI_DB")
    $(generate_create_db_sql "$GPUSTACK_DB")
    $(generate_create_db_sql "$DIFY_DB")
    $(generate_dify_extensions_sql "$DIFY_DB")
    $(generate_create_db_sql "$DB_PLUGIN_DATABASE")
    $(generate_create_db_sql "$AUTHENTIK_DB")
    $(generate_create_db_sql "$GITEA_DB")
    $(generate_create_db_sql "$LIGHTRAG_DB")
    $(generate_lightrag_extensions_sql "$LIGHTRAG_DB")
    $(generate_create_db_sql "$COGNEE_DB")
    $(generate_cognee_extensions_sql "$COGNEE_DB")
    $(generate_create_db_sql "$PAPERCLIP_DB")
    $(generate_synapse_db_sql "$SYNAPSE_DB")
    $(generate_create_db_sql "$PAPERLESS_DB")
    $(generate_create_db_sql "$VAULTWARDEN_DB")
    $(generate_create_db_sql "$INFISICAL_DB")
    $(generate_create_db_sql "$ONYX_DB")
    $(generate_create_db_sql "$OPENUEM_DB")
    $(generate_create_db_sql "${AGENT_MANAGER_DB:-agent_manager_db}")
    $(generate_create_db_sql "${MCP_MANAGER_DB:-mcp_manager_db}")
    $(generate_create_db_sql "${LLM_MANAGER_DB:-llm_manager_db}")
    $(generate_service_user_sql "$AUTHENTIK_DB" "$AUTHENTIK_DB_USER" "$AUTHENTIK_DB_PASSWORD")
    $(generate_service_user_sql "$OPENWEBUI_DB" "$OPENWEBUI_DB_USER" "$OPENWEBUI_DB_PASSWORD")
    $(generate_service_user_sql "$GPUSTACK_DB" "$GPUSTACK_DB_USER" "$GPUSTACK_DB_PASSWORD")
    $(generate_service_user_sql "$DIFY_DB" "$DIFY_DB_USER" "$DIFY_DB_PASSWORD")
    $(generate_service_user_sql "$DB_PLUGIN_DATABASE" "$DIFY_PLUGIN_DB_USER" "$DIFY_PLUGIN_DB_PASSWORD")
    $(generate_service_user_sql "$GITEA_DB" "$GITEA_DB_USER" "$GITEA_DB_PASSWORD")
    $(generate_service_user_sql "$PAPERLESS_DB" "$PAPERLESS_DB_USER" "$PAPERLESS_DB_PASSWORD")
    $(generate_service_user_sql "$INFISICAL_DB" "$INFISICAL_DB_USER" "$INFISICAL_DB_PASSWORD")
    $(generate_service_user_sql "$ONYX_DB" "$ONYX_DB_USER" "$ONYX_DB_PASSWORD")
    $(generate_onyx_createrole_sql "$ONYX_DB_USER")
    $(generate_service_user_sql "$OPENUEM_DB" "$OPENUEM_DB_USER" "$OPENUEM_DB_PASSWORD")
    $(generate_service_user_sql "${MCP_MANAGER_DB:-mcp_manager_db}" "$MCP_MANAGER_DB_USER" "$MCP_MANAGER_DB_PASSWORD")
    $(generate_service_user_sql "${LLM_MANAGER_DB:-llm_manager_db}" "${LLM_MANAGER_DB_USER:-llm_manager_user}" "$LLM_MANAGER_DB_PASSWORD")
    $(generate_monitor_user_sql "${POSTGRES_EXPORTER_USER:-postgres_exporter_user}" "${POSTGRES_EXPORTER_PASSWORD:-}")
EOF
)

# Überprüfen, ob überhaupt Befehle generiert wurden
if [ -n "$SQL_COMMANDS" ]; then
    echo "--- EXECUTING DATABASE INITIALIZATION ---"
    echo "$SQL_COMMANDS"
    # Alle Befehle mit einem einzigen psql-Aufruf ausführen
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<< "$SQL_COMMANDS"
    echo "--- DATABASE INITIALIZATION COMPLETE ---"
else
    echo "--- NO DATABASES TO INITIALIZE ---"
fi