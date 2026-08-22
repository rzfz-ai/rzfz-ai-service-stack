#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz.ai Stack - Backup Management Script
# ==============================================================================
# This script provides CLI access to backup and restore functionality.
#
# Usage:
#   rzfz backup list              # List all backups
#   rzfz backup backup            # Create a full backup (excludes model files; INCLUDES per-user agent volumes by default)
#   rzfz backup backup --include-models  # Create a full backup including model files
#   rzfz backup backup --skip-agents     # Create a full backup WITHOUT per-user agent volumes (M030-S4)
#   rzfz backup backup --partial <volume>  # Backup specific volume
#   rzfz backup restore <file>    # Restore from backup
#   rzfz backup delete <file>     # Delete a backup file
#   rzfz backup status            # Show backup configuration
#
# ==============================================================================

set -eo pipefail

# M026 / S02 #4: source the shared library for colors, print_*,
# read_env_value, and check_container.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

CONTAINER_NAME="razzfazz-backup-management"

print_help() {
    echo "rzfz.ai Backup Management"
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  list                    List all available backups"
    echo "  backup                  Create a full backup of all volumes"
    echo "                          (excludes model files; INCLUDES per-user agents by default)"
    echo "  backup --include-models Create a full backup including LLM/TTS model files"
    echo "  backup --skip-agents    Create a full backup WITHOUT per-user agent volumes (M030-S4)"
    echo "  backup --partial <vol>  Create a partial backup of specific volume"
    echo "  restore <file>          🔴 DATA-LOSS: restore from a backup file —"
    echo "                          OVERWRITES current volumes, databases and .env,"
    echo "                          then recreates the stack. Prompts to confirm."
    echo "  delete <file>           🟡 Delete a backup file (the snapshot is gone)"
    echo "  status                  Show current backup configuration"
    echo ""
    echo "Options:"
    echo "  --include-models        Include LLM/TTS model files (gpustack-data,"
    echo "                          speaches-data) in the backup. These are excluded"
    echo "                          by default to save disk space, as models can be"
    echo "                          re-downloaded."
    echo "  --skip-agents           Skip per-user agent volumes (moltis chats, hermes"
    echo "                          skills, coding-tools workspaces, openhands sessions,"
    echo "                          paperclip state). Default: INCLUDED. Skip when you"
    echo "                          want a small/fast backup of just core stack state."
    echo ""
    echo "Examples:"
    echo "  $0 list"
    echo "  $0 backup"
    echo "  $0 backup --include-models"
    echo "  $0 backup --skip-agents"
    echo "  $0 backup --partial postgres-data"
    echo "  $0 restore backup-2026-02-03-0300.tar.gz"
    echo "  $0 delete backup-2026-01-01-0300.tar.gz"
    echo ""
}

# Wrap lib's non-fatal check_container with the fatal "die if missing"
# semantics the original backup.sh enforced. PRESERVE original error wording.
require_container() {
    if ! check_container "$CONTAINER_NAME"; then
        echo -e "${RED}Error: Container '$CONTAINER_NAME' is not running.${NC}"
        echo "Please start the stack first: docker compose up -d"
        exit 1
    fi
}

show_status() {
    echo -e "${BLUE}=== Backup Configuration ===${NC}"
    echo ""
    
    # Read from .env (via lib's read_env_value — handles quoted values and
    # inline-comment stripping per the feedback_dotenv_no_source memory).
    if [ -f ".env" ]; then
        CRON=$(read_env_value .env BACKUP_CRON_EXPRESSION)
        RETENTION=$(read_env_value .env BACKUP_RETENTION_DAYS)
        INCLUDE_MODELS=$(read_env_value .env BACKUP_INCLUDE_MODEL_FILES)
        EXCLUDE_REGEXP=$(read_env_value .env BACKUP_EXCLUDE_REGEXP)

        echo -e "Schedule:        ${GREEN}${CRON:-Not set}${NC}"
        echo -e "Retention:       ${GREEN}${RETENTION:-7} days${NC}"

        if [ "${INCLUDE_MODELS:-false}" = "true" ] || [ -z "$EXCLUDE_REGEXP" ]; then
            echo -e "Model Files:     ${GREEN}Included in backup${NC}"
        else
            echo -e "Model Files:     ${YELLOW}Excluded from backup${NC} (gpustack-data, speaches-data)"
        fi

        # Encryption status
        ENC_PW=$(read_env_value .env BACKUP_ENCRYPTION_PASSWORD)
        if [ -n "$ENC_PW" ]; then
            echo -e ".env Encryption: ${GREEN}🔒 Active (dedicated encryption password)${NC}"
        else
            echo -e ".env Encryption: ${YELLOW}⚠ No BACKUP_ENCRYPTION_PASSWORD set — .env files will NOT be encrypted${NC}"
        fi
    else
        echo -e "${YELLOW}Warning: .env file not found${NC}"
    fi
    
    echo ""
    echo -e "${BLUE}=== Backup Statistics ===${NC}"
    echo ""
    
    # Count backups
    # rc6.7 #6: include `.tar.gz.gpg` (encrypted form added by rc2 F-A2).
    # Without -o, find ANDs the patterns; with -o we list either suffix.
    # Use `\(... -o ...\)` so the count covers both.
    if [ -d "backups" ]; then
        TOTAL=$(find backups -maxdepth 1 \( -name "*.tar.gz" -o -name "*.tar.gz.gpg" \) 2>/dev/null | wc -l)
        FULL=$(find backups -maxdepth 1 \( -name "backup-*.tar.gz" -o -name "backup-*.tar.gz.gpg" \) 2>/dev/null | wc -l)
        PARTIAL=$(find backups -maxdepth 1 \( -name "partial-*.tar.gz" -o -name "partial-*.tar.gz.gpg" \) 2>/dev/null | wc -l)
        ENCRYPTED=$(find backups -maxdepth 1 -name "*.tar.gz.gpg" 2>/dev/null | wc -l)
        SIZE=$(du -sh backups 2>/dev/null | cut -f1)

        echo -e "Total Backups:   ${GREEN}$TOTAL${NC}"
        echo -e "  Full:          $FULL"
        echo -e "  Partial:       $PARTIAL"
        echo -e "  Encrypted:     ${ENCRYPTED} (.tar.gz.gpg form, F-A2)"
        echo -e "Total Size:      ${GREEN}${SIZE:-Unknown}${NC}"
    else
        echo -e "${YELLOW}No backups directory found${NC}"
    fi
    echo ""
}

# M032-S09 dry-run guard (defense-in-depth for the test harness — see
# tests/scripts/_helpers.py). Honors RAZZFAZZ_TEST_DRY_RUN=1 for the
# destructive subcommands (restore, delete) and exits 0 BEFORE the
# read prompt or any docker exec.
if [ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]; then
    case "${1:-}" in
        restore|delete|backup)
            echo "DRY-RUN: would have invoked razzfazz-backup.sh ${1} ${2:-} (RAZZFAZZ_TEST_DRY_RUN=1)" >&2
            exit 0 ;;
    esac
fi

# Main logic
case "${1:-}" in
    list)
        require_container
        docker exec $([ -t 0 ] && echo -it || echo -i) "$CONTAINER_NAME" python /app/backup_manager.py list
        ;;
    backup)
        require_container
        # M030-S4: --skip-agents opts OUT of per-user agent volume snapshots
        # (default is INCLUDE — operator-friendly, no risk of forgotten data).
        # Useful for size-constrained snapshots on dev boxes or for fast
        # incremental cycles where per-user state isn't relevant.
        SKIP_AGENTS_ENV=""
        # Allow --skip-agents in any position after `backup`.
        for arg in "$@"; do
            if [ "$arg" = "--skip-agents" ]; then
                SKIP_AGENTS_ENV="-e BACKUP_SKIP_AGENTS=true"
                echo -e "${YELLOW}Per-user agent volumes will be SKIPPED.${NC}"
            fi
        done

        if [ "${2:-}" = "--partial" ] && [ -n "${3:-}" ]; then
            echo -e "${BLUE}Creating partial backup of: $3${NC}"
            docker exec $([ -t 0 ] && echo -it || echo -i) "$CONTAINER_NAME" python /app/backup_manager.py backup --type partial --target "$3"
        elif [ "${2:-}" = "--include-models" ]; then
            echo -e "${BLUE}Creating full backup (including model files)...${NC}"
            # BSB-04: invoke `razzfazz-backup` (wrapper at /usr/local/bin/
            # razzfazz-backup) instead of offen's `backup` directly.
            # Wrapper re-reads BACKUP_ENCRYPTION_PASSWORD from .env at
            # invocation time so a rotated passphrase lands without
            # needing `compose up -d --force-recreate backup-service`.
            # shellcheck disable=SC2086
            docker exec $([ -t 0 ] && echo -it || echo -i) -e BACKUP_EXCLUDE_REGEXP="" $SKIP_AGENTS_ENV "backup-service" razzfazz-backup
        else
            echo -e "${BLUE}Creating full backup (excluding model files)...${NC}"
            # BSB-04: see comment above.
            # shellcheck disable=SC2086
            docker exec $([ -t 0 ] && echo -it || echo -i) $SKIP_AGENTS_ENV "backup-service" razzfazz-backup
        fi
        ;;
    restore)
        require_container
        if [ -z "${2:-}" ]; then
            echo -e "${RED}Error: Please specify backup file to restore${NC}"
            echo "Usage: $0 restore <backup-file>"
            exit 1
        fi
        echo -e "${YELLOW}WARNING: This will restore from: $2${NC}"
        read -p "Are you sure? (yes/no): " confirm
        if [ "$confirm" = "yes" ]; then
            # #125 (cross-secret-boundary DR): the in-container manager decrypts,
            # stops the stack, restores volumes + databases + .env + agent volumes,
            # then RETURNS WITHOUT bringing the stack back up (we pass
            # RAZZFAZZ_RESTORE_HOST_RECREATE=1 so its legacy `docker start` sweep
            # is suppressed). The authoritative bring-up happens HERE, host-side,
            # with `docker compose down --remove-orphans` (KEEPING volumes) +
            # `docker compose up -d`:
            #   * recreating every container re-reads the freshly-restored .env, so
            #     each picks up the restored secrets. `docker start` (what the
            #     manager used to do) re-uses each container's stale baked env →
            #     `password authentication failed` crash loop after a restore that
            #     crosses a secret boundary (fresh init then restore older backup).
            #   * the HOST has the compose project + restored .env; the manager
            #     container does not — and a container recreating ITSELF mid-run is
            #     a chicken-and-egg we sidestep entirely by orchestrating from here.
            if docker exec $([ -t 0 ] && echo -it || echo -i) \
                    -e RAZZFAZZ_RESTORE_HOST_RECREATE=1 \
                    "$CONTAINER_NAME" \
                    python /app/backup_manager.py restore --file "$2" --type full; then
                echo -e "${BLUE}Restore extracted. Recreating stack so the restored .env applies...${NC}"
                # #125: bring the WHOLE stack down (keeping volumes!) and back up,
                # rather than `up -d --force-recreate`. Reason: `up --force-recreate`
                # only acts on the services SELECTED by the current .env's
                # COMPOSE_PROFILES + COMPOSE_FILE. If the restored .env selects a
                # DIFFERENT service variant than the one currently running under a
                # SHARED container_name — the textbook case is the four gpustack
                # services (gpustack-legacy / gpustack-cpu / gpustack-experimental,
                # all `container_name: gpustack`) when HARDWARE / llm-profile differs
                # between the backed-up box and the box being restored — then
                # `up --force-recreate` cannot recreate the existing `gpustack`
                # container (it belongs to a now-inactive service) and leaves it
                # running with its STALE pre-restore env -> `password authentication
                # failed` for that one service even though all others recovered.
                # `down` removes ALL project containers by container_name regardless
                # of which service/profile owns them; the subsequent `up -d` then
                # creates every active service fresh from the restored .env.
                #
                # CRITICAL: `down` WITHOUT -v / --volumes — the volumes we just
                # restored must NOT be removed. --remove-orphans clears any
                # leftover variant containers (e.g. the inactive gpustack-*).
                if docker compose down --remove-orphans \
                   && docker compose up -d --remove-orphans; then
                    echo -e "${GREEN}Stack recreated with restored configuration.${NC}"
                else
                    echo -e "${RED}Error: bringing the stack back up after restore failed.${NC}"
                    echo -e "${RED}Volumes/.env are restored; re-run 'docker compose up -d' manually.${NC}"
                    exit 1
                fi
            else
                echo -e "${RED}Error: in-container restore failed; stack NOT recreated.${NC}"
                exit 1
            fi
        else
            echo "Restore cancelled."
        fi
        ;;
    delete)
        require_container
        if [ -z "${2:-}" ]; then
            echo -e "${RED}Error: Please specify backup file to delete${NC}"
            exit 1
        fi
        docker exec $([ -t 0 ] && echo -it || echo -i) "$CONTAINER_NAME" python /app/backup_manager.py delete --file "$2"
        ;;
    status)
        show_status
        ;;
    help|--help|-h)
        print_help
        ;;
    *)
        if [ -n "${1:-}" ]; then
            echo -e "${RED}Unknown command: $1${NC}"
            echo ""
        fi
        print_help
        ;;
esac
