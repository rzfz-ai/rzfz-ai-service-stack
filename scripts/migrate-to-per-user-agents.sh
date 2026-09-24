#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# Migrate global hermes / moltis / coding-tools volumes to per-user (M020 S09)
# ==============================================================================
# 2026.05 deletes the global hermes/moltis/coding-tools profiles in favour of
# per-user instances managed by agent-manager. Existing 2026.04 deployments
# with populated global volumes need to copy that state into per-user volumes
# so the user doesn't lose their agent's memory / workspace / config.
#
# Strategy:
#   - SINGLE-ADMIN install (typical razzfazz box): auto-migrate. Detect the
#     one Authentik user with admin role, provision a per-user agent of each
#     populated type for them, copy global volume → per-user volume, rename
#     the global volume to <name>-pre-migration-2026.05 (recoverable for 30
#     days, then prune).
#   - MULTI-USER install: print a yellow warning + path to the manual runbook
#     and exit 0 without modifying volumes. Operator runs the per-user
#     mappings manually from docs/migration-2026.05-per-user-agents.md.
#
# Idempotent: re-running with the global volume already migrated (renamed
# to <name>-pre-migration-2026.05) skips that volume.
#
# Invoked from razzfazz-upgrade.sh Step 9b. Manual invocation:
#   bash scripts/migrate-to-per-user-agents.sh
# ==============================================================================

set -eo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

# Read just the .env keys we need. (Don't `source .env` — operator-edited
# files can carry unquoted values with spaces or shell metacharacters
# (`<`, `>`, `|`) that break a naive source. Targeted grep is robust.)
if [ -f .env ]; then
    POSTGRES_USER=$(grep '^POSTGRES_USER=' .env | cut -d= -f2- | sed 's/^"//;s/"$//')
    POSTGRES_PASSWORD=$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2- | sed 's/^"//;s/"$//')
    AUTHENTIK_DB=$(grep '^AUTHENTIK_DB=' .env | cut -d= -f2- | sed 's/^"//;s/"$//')
    export POSTGRES_USER POSTGRES_PASSWORD AUTHENTIK_DB
fi

GLOBAL_VOLUMES=(
    "razzfazz-stack_hermes-data"
    "razzfazz-stack_moltis-data"
    "razzfazz-stack_moltis-config"
    "razzfazz-stack_coding-tools-workspace"
)

# Map global volume → (agent_type, per-user volume suffix)
get_target_for_global() {
    case "$1" in
        razzfazz-stack_hermes-data)            echo "hermes:agent" ;;
        razzfazz-stack_moltis-data)            echo "moltis:data" ;;
        razzfazz-stack_moltis-config)          echo "moltis:config" ;;
        razzfazz-stack_coding-tools-workspace) echo "coding-tools:workspace" ;;
        *) echo "" ;;
    esac
}

log() { echo "[migrate-per-user] $*"; }
warn() { echo "[migrate-per-user] WARN: $*" >&2; }

# ── Detect what's actually populated ──────────────────────────────────────────
populated=()
for vol in "${GLOBAL_VOLUMES[@]}"; do
    [ -z "$vol" ] && continue
    # Skip already-migrated volumes
    if docker volume inspect "${vol}-pre-migration-2026.05" >/dev/null 2>&1; then
        log "skip: $vol already migrated (renamed sentinel exists)"
        continue
    fi
    # Skip non-existent volumes (fresh install path)
    docker volume inspect "$vol" >/dev/null 2>&1 || continue
    # Skip empty volumes (>=1 entry inside)
    contents=$(docker run --rm -v "${vol}:/v:ro" alpine sh -c \
        'find /v -mindepth 1 -maxdepth 2 | head -1' 2>/dev/null || true)
    if [ -z "$contents" ]; then
        log "skip: $vol exists but is empty"
        continue
    fi
    populated+=("$vol")
done

if [ ${#populated[@]} -eq 0 ]; then
    log "nothing to migrate (no populated global volumes); exiting clean."
    exit 0
fi

log "populated global volumes detected: ${populated[*]}"

# ── User detection ────────────────────────────────────────────────────────────
# Query Authentik DB for users with admin role.
ADMIN_COUNT=0
ADMIN_USER=""
ADMIN_USER_ID=""

if docker ps --format '{{.Names}}' | grep -q '^postgres$'; then
    AUTH_USERS=$(docker exec -e PGPASSWORD="${POSTGRES_PASSWORD:-}" postgres \
        psql -U "${POSTGRES_USER:-docker}" -d "${AUTHENTIK_DB:-authentik_db}" -tAc \
        "SELECT username FROM authentik_core_user u
         JOIN authentik_core_user_groups ug ON ug.user_id = u.id
         JOIN authentik_core_group g ON g.group_uuid = ug.group_id
         WHERE g.name IN ('authentik Admins', 'razzfazz.ai Admins')
         AND u.is_active = true" 2>/dev/null || true)
    ADMIN_COUNT=$(echo "$AUTH_USERS" | grep -c . || true)
    ADMIN_USER=$(echo "$AUTH_USERS" | head -1)
fi

if [ "$ADMIN_COUNT" -eq 0 ]; then
    log "could not detect admin user from Authentik DB (postgres down? or new schema?)."
    log "deferring to manual runbook: docs/migration-2026.05-per-user-agents.md"
    exit 0
fi

if [ "$ADMIN_COUNT" -gt 1 ]; then
    cat >&2 <<EOF

  ┌─────────────────────────────────────────────────────────────────────────┐
  │  MULTI-USER MIGRATION REQUIRED — manual step                            │
  │                                                                         │
  │  Detected ${ADMIN_COUNT} admin users in Authentik. Auto-migration is restricted to │
  │  single-admin installs to avoid silently picking the wrong owner for    │
  │  the global agent state.                                                │
  │                                                                         │
  │  Populated global volumes:                                              │
EOF
    for v in "${populated[@]}"; do
        printf '  │       • %-65s│\n' "$v" >&2
    done
    cat >&2 <<EOF
  │                                                                         │
  │  Per-user migration recipe: docs/migration-2026.05-per-user-agents.md   │
  │                                                                         │
  │  Until migrated, the per-user agent flow has empty state. Existing      │
  │  data in the global volumes is preserved as-is (no destructive          │
  │  action).                                                               │
  └─────────────────────────────────────────────────────────────────────────┘

EOF
    exit 0
fi

# ── Single-admin path ────────────────────────────────────────────────────────
USER_SLUG=$(echo "$ADMIN_USER" | tr '[:upper:] -' '[:lower:]--' | tr -cd 'a-z0-9-')
log "single-admin install — auto-migrating into user_slug=${USER_SLUG} (Authentik username: ${ADMIN_USER})"

# We need agent-manager up + responsive. Skip auto-migration if not.
if ! docker ps --format '{{.Names}}' | grep -q '^agent-manager$'; then
    warn "agent-manager not running — auto-migration requires it. Re-run after starting agents profile, or follow manual runbook."
    exit 0
fi

# Provision (or find) per-user agents for the admin
provision_or_find() {
    local agent_type="$1"
    docker exec agent-manager curl -s -X POST \
        -H "X-Authentik-Username: ${ADMIN_USER}" \
        -H "X-Authentik-Uid: ${ADMIN_USER}" \
        -H "X-Authentik-Groups: razzfazz.ai Admins" \
        "http://localhost:5000/api/launch/${agent_type}" >/dev/null 2>&1 || true
    # Wait briefly for the instance to register
    sleep 2
}

migrate_volume() {
    local global_vol="$1"
    local agent_type="$2"
    local suffix="$3"
    local target_vol="agent-${agent_type}-${USER_SLUG}-${suffix}"

    if ! docker volume inspect "$target_vol" >/dev/null 2>&1; then
        log "creating target volume $target_vol"
        docker volume create "$target_vol" >/dev/null
    fi

    log "copying $global_vol → $target_vol ..."
    docker run --rm \
        -v "${global_vol}:/from:ro" \
        -v "${target_vol}:/to" \
        alpine sh -c 'cp -a /from/. /to/'

    log "renaming $global_vol → ${global_vol}-pre-migration-2026.05 (recoverable for 30 days)"
    docker volume create "${global_vol}-pre-migration-2026.05" >/dev/null 2>&1 || true
    docker run --rm \
        -v "${global_vol}:/from:ro" \
        -v "${global_vol}-pre-migration-2026.05:/to" \
        alpine sh -c 'cp -a /from/. /to/'
    docker volume rm "$global_vol" >/dev/null 2>&1 || \
        warn "could not remove original $global_vol — operator can prune later via 'docker volume rm $global_vol'"
}

# Provision per-user agents for each affected type, then copy
declare -A provisioned
for vol in "${populated[@]}"; do
    target=$(get_target_for_global "$vol")
    [ -z "$target" ] && continue
    agent_type="${target%%:*}"
    suffix="${target##*:}"

    if [ -z "${provisioned[$agent_type]:-}" ]; then
        log "provisioning per-user $agent_type for $ADMIN_USER ..."
        provision_or_find "$agent_type"
        provisioned[$agent_type]=1
    fi

    migrate_volume "$vol" "$agent_type" "$suffix"
done

log "auto-migration complete."
log "Pre-migration backups kept as <name>-pre-migration-2026.05 — prune after 30 days with:"
log "  docker volume ls -q | grep '\\-pre-migration-2026\\.05$' | xargs -r docker volume rm"
exit 0
