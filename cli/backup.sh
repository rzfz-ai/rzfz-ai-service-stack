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
#   rzfz backup restore <file>    # Restore from backup (full stack)
#   rzfz backup restore <file> --database <name>  # #279: restore ONE database only
#   rzfz backup delete <file>     # Delete a backup file
#   rzfz backup status            # Show backup configuration
#
# ==============================================================================

set -eo pipefail

# M026 / S02 #4: source the shared library for colors, print_*,
# read_env_value, and check_container.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# House rule, matching init.sh / upgrade.sh / lifecycle.sh / setup.sh /
# worker-join.sh / hub-credentials.sh — `rzfz` execs the target WITHOUT
# changing directory, and this script reads a bare `.env` and runs
# `docker compose` with a relative COMPOSE_FILE chain. Run from anywhere but
# the stack root, it reported a configured box as unconfigured.
cd "$SCRIPT_DIR"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"
# shellcheck source=cli/lib-backup-run.sh disable=SC1091
source "${SCRIPT_DIR}/cli/lib-backup-run.sh"

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
    echo "  restore <file> --database <name>"
    echo "                          🟡 restore ONLY database <name> from the"
    echo "                          backup file. Rest of the stack keeps running —"
    echo "                          no stop, no recreate. Use for a single corrupt"
    echo "                          DB (e.g. gpustack_db) instead of a full restore."
    echo "  delete <file>           🟡 Delete a backup file (the snapshot is gone)"
    echo "  status                  Show current backup configuration"
    echo ""
    echo "Options:"
    echo "  --include-models        Include LLM/TTS model files (gpustack-data,"
    echo "                          speaches-data, llm-node-models, llm-registry-data)"
    echo "                          in the backup. These are excluded"
    echo "                          by default to save disk space, as models can be"
    echo "                          re-downloaded."
    echo "  --skip-agents           Skip per-user agent volumes (moltis chats, hermes"
    echo "                          skills, coding-tools workspaces, paperclip state)."
    echo "                          Default: INCLUDED. Skip when you"
    echo "                          want a small/fast backup of just core stack state."
    echo "  --database <name>       (restore only) Target a single database instead"
    echo "                          of a full-stack restore. May appear before or"
    echo "                          after the backup file argument."
    echo "  --no-recreate           (full restore only) Do not recreate the stack from"
    echo "                          the restored .env: nothing starts on it. Only the"
    echo "                          database containers the restore needs (on their"
    echo "                          existing container config), backup-service and"
    echo "                          docker-socket-proxy stay up. Bring the rest up"
    echo "                          yourself with 'rzfz start'. Same as"
    echo "                          RZFZ_RESTORE_NO_RECREATE=1."
    echo "  --keep-env              (full restore with --no-recreate only) Leave this"
    echo "                          box's .env and .env.dify as they are. The backup's"
    echo "                          copies are written to backups/.restored-env/"
    echo "                          (mode 600) instead; move them out of there."
    echo ""
    echo "Examples:"
    echo "  $0 list"
    echo "  $0 backup"
    echo "  $0 backup --include-models"
    echo "  $0 backup --skip-agents"
    echo "  $0 backup --partial postgres-data"
    echo "  $0 restore backup-2026-02-03-0300.tar.gz"
    echo "  $0 restore backup-2026-02-03-0300.tar.gz --database gpustack_db"
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

# After a full restore, Speaches has no STT/TTS model (#2700): the restore
# recreates the speaches-data volume, and backups exclude model files by design.
# Re-run post-install's provisioning (scripts/lib.sh: the pinned default ids,
# skipped offline) and then ask Speaches what it lists. Never fails the restore:
# when the models are still missing, say so plainly with the commands to run.
#   $1 = now  — the stack was just brought up: wait for Speaches, provision
#   $1 = later — nothing was started (--no-recreate): only say what to run
# RZFZ_SPEACHES_WAIT_SECONDS bounds the wait for Speaches to answer (120 s).
# #3412: put the backup's box-local compose overlays (compose.local.<name>.yml)
# back into the stack root. The backup manager mounts the stack read-only, so it
# only hands them over (`compose-local-export`, a vetted tar on stdout) and this
# host side writes them, BEFORE compose runs on the restored .env. An entry the
# restored COMPOSE_FILE names that is still missing afterwards is named, then
# dropped by the same reconcile the upgrade runs (compose refuses a chain that
# names a missing file). Never fails the restore.
restore_compose_local_overlays() {
    local tmp names e missing=""
    print_step "Putting back the box-local compose overlays..."
    if ! tmp=$(mktemp "${TMPDIR:-/tmp}/rzfz-compose-local.XXXXXX"); then
        print_warning "No temp file for the compose overlays; they are NOT put back. Check COMPOSE_FILE before starting the stack."
    elif ! docker exec -i "$CONTAINER_NAME" python /app/backup_manager.py compose-local-export > "$tmp"; then
        print_warning "The backup's box-local compose overlays could not be read (see the error above); they are NOT put back."
    elif ! names=$(python3 "$SCRIPT_DIR/scripts/compose_local_restore.py" "$tmp" "$SCRIPT_DIR"); then
        print_warning "The backup's box-local compose overlays were refused (see the error above); they are NOT put back."
    elif [ -n "$names" ]; then
        print_substep "Restored box-local compose overlay(s): ${names}"
    else
        print_substep "The backup carries no box-local compose overlays."
    fi
    if [ -n "${tmp:-}" ]; then
        shred -u "$tmp" 2>/dev/null || rm -f "$tmp"
    fi
    while IFS= read -r e; do
        [ -n "$e" ] || continue
        [ -f "$SCRIPT_DIR/${e#./}" ] || missing="${missing} ${e}"
    done < <(compose_file_local_overlays "$SCRIPT_DIR/.env")
    if [ -n "$missing" ]; then
        print_warning "The restored .env names box-local compose overlay(s) that the backup does not carry:${missing}. Their settings are not on this box; the entries are removed from COMPOSE_FILE so the stack can start. Recreate the files and add the entries again if you need them."
    fi
    compose_file_reconcile_local_overlays "$SCRIPT_DIR/.env"
    return 0
}

restore_reprovision_speaches() {
    local when="${1:-now}" profiles port url hand missing waited=0
    local max_wait="${RZFZ_SPEACHES_WAIT_SECONDS:-120}"
    profiles=$(read_env_value ".env" COMPOSE_PROFILES)
    case ",${profiles}," in *,stts,*) ;; *) return 0 ;; esac
    port=$(read_env_value ".env" SPEACHES_PORT)
    case "$port" in ''|*[!0-9]*) port=5003 ;; esac
    url="http://127.0.0.1:${port}"
    hand="curl -X POST ${url}/v1/models/${RAZZFAZZ_SPEACHES_STT_MODEL} && curl -X POST ${url}/v1/models/${RAZZFAZZ_SPEACHES_TTS_MODEL}"
    if [ "$when" = later ]; then
        print_warning "Speaches STT/TTS models are not part of a backup. After 'rzfz start', download them with: ${hand}"
        return 0
    fi
    print_step "Speaches: re-provisioning the STT/TTS models (backups exclude model files)..."
    until razzfazz_speaches_missing_models "$url" >/dev/null; do
        if [ "$waited" -ge "$max_wait" ]; then
            print_warning "Speaches did not answer at ${url} within ${max_wait}s: its STT/TTS models were NOT re-provisioned. Once it is up, run: ${hand}"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    SPEACHES_PORT="$port" razzfazz_speaches_download_defaults || true
    if ! missing=$(razzfazz_speaches_missing_models "$url"); then
        print_warning "Speaches stopped answering at ${url}: the STT/TTS models are unconfirmed. Check 'rzfz status', or run: ${hand}"
        return 0
    fi
    if [ -z "$missing" ]; then
        print_success "Speaches lists the STT/TTS models: ${RAZZFAZZ_SPEACHES_STT_MODEL}, ${RAZZFAZZ_SPEACHES_TTS_MODEL}"
        return 0
    fi
    missing=$(printf '%s' "$missing" | paste -sd, - | sed 's/,/, /g')
    if razzfazz_is_offline ".env"; then
        print_warning "Speaches has NO model ${missing} after the restore, and this box is offline, so nothing was downloaded. Put the models into the speaches-data volume, or on a box with internet access run: ${hand}"
    else
        print_warning "Speaches has NO model ${missing} after the restore. Run by hand: ${hand}"
    fi
    return 0
}

# #2589: `docker compose down` + `up` gives the project networks new IDs, and
# the containers created outside the project still name the old ones, so
# `docker start` fails on them with "network … not found":
#   * LLM engines: relaunched here through the LLM Manager (the `rzfz start`
#     path: cli/lifecycle.sh's internal revive-engines verb);
#   * per-user agents: the agent manager recreates every agent its restored
#     records list as running, on the new networks and the same volumes.
restore_revive_out_of_project() {
    print_step "Relaunching the LLM engines the restore stopped..."
    RZFZ_VERB=revive-engines bash "${SCRIPT_DIR}/cli/lifecycle.sh" \
        || print_warning "The LLM engines were not relaunched. Run: rzfz start"
    print_info "Per-user agents that were running come back on their own within about two minutes: the agent manager recreates them on the new networks."
    return 0
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
            echo -e "Model Files:     ${YELLOW}Excluded from backup${NC} (gpustack-data, speaches-data, llm-node-models, llm-registry-data)"
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
        if check_container "$CONTAINER_NAME" >/dev/null 2>&1; then
            # A failed probe must never read as "all keys held": no output
            # is not the same as zero archives without a key.
            KEY_ROWS=""; KEY_RC=0
            KEY_ROWS=$(docker exec "$CONTAINER_NAME" python /app/backup_manager.py list --check-keys 2>/dev/null) || KEY_RC=$?
            if [ "$KEY_RC" -ne 0 ]; then
                echo -e "Key check:       ${YELLOW}unknown${NC} (the key probe in $CONTAINER_NAME exited ${KEY_RC})"
            else
                NOT_HELD=$(printf '%s\n' "$KEY_ROWS" | grep -c 'NOT HELD' || true)
                if [ "${NOT_HELD:-0}" -gt 0 ]; then
                    echo -e "Key not held:    ${RED}${NOT_HELD}${NC} archive(s) no keyring passphrase opens — 'rzfz backup list' names them"
                else
                    echo -e "Key not held:    ${GREEN}0${NC}"
                fi
            fi
        fi
        echo -e "Total Size:      ${GREEN}${SIZE:-Unknown}${NC}"
    else
        echo -e "${YELLOW}No backups directory found${NC}"
    fi
    echo ""
    echo -e "${BLUE}=== Last backup run ===${NC}"
    echo ""
    local st rc errs warns agents fin arch started lock hook line label worst=""
    st=$(backup_run_report)
    lock=$(backup_run_lock_state)
    hook=$(backup_run_hook_lock_state)
    # #2584: the archive verdict judges the run, the schedule and the archive
    # together; a run that wrote no archive (or none ran) is a FAIL, whatever
    # the hook said. It may be two lines — a missed slot first, then the last
    # run — and `rzfz status` prints the same lines.
    arch=$(backup_run_archive_verdict backups "$st" "$lock" \
        "$(backup_run_cron_from_env .env)" "$(read_env_value .env TZ)" \
        "$(read_env_value .env BACKUP_FILENAME)" "$hook" "$(backup_run_service_started)")
    # The library puts a FAIL first whenever there is one (the missed slot).
    worst=${arch%%|*}
    _st() { printf '%s\n' "$st" | sed -n "s/^$1=//p" | head -n 1; }
    rc=$(_st rc); errs=$(_st errors); warns=$(_st warnings); agents=$(_st agent_failures)
    fin=$(_st finished_epoch); started=$(_st started_epoch)
    local skipped cap pair
    skipped=$(_st skipped_volumes); cap=$(_st vol_cap_mb)
    local detail; detail=$(_st skipped_detail)
    if [ -n "$fin" ]; then
        echo -e "Pre-backup hook: exit ${rc:-?}, ${errs:-0} error(s), ${warns:-0} warning(s), ${agents:-0} agent volume(s) NOT backed up"
        echo -e "Finished:        $(date -d "@${fin}" '+%Y-%m-%d %H:%M' 2>/dev/null || echo unknown)"
    elif [ -n "$started" ]; then
        if [ "$hook" = held ]; then
            echo -e "Pre-backup hook: started $(date -d "@${started}" '+%Y-%m-%d %H:%M' 2>/dev/null || echo unknown), still running"
        else
            echo -e "Pre-backup hook: started $(date -d "@${started}" '+%Y-%m-%d %H:%M' 2>/dev/null || echo unknown), not finished"
        fi
    else
        echo -e "Pre-backup hook: no report on this box"
    fi
    label="Archive:        "
    while IFS= read -r line; do
        case "${line%%|*}" in
            OK)      echo -e "$label ${GREEN}${line#*|}${NC}" ;;
            RUNNING) echo -e "$label ${YELLOW}${line#*|}${NC}" ;;
            FAIL)    echo -e "$label ${RED}FAIL${NC} — ${line#*|}" ;;
            INFO)    echo -e "$label ${line#*|}" ;;
            *)       continue ;;
        esac
        label="                "
    done <<EOF
$arch
EOF
    if [ "$worst" = "FAIL" ]; then
        echo -e "Verdict:         ${RED}FAILED${NC} — see Archive above"
    elif [ "$worst" = "RUNNING" ] && [ -z "$fin" ]; then
        echo -e "Verdict:         ${YELLOW}RUNNING${NC}"
    elif [ -z "$fin" ]; then
        :
    elif [ "${rc:-1}" != "0" ]; then
        echo -e "Verdict:         ${RED}FAILED${NC}"
    elif [ -n "$(printf '%s' "$detail" | tr ',' '\n' | awk -F: 'NF >= 2 && $1 != "" && $2 != "missing_volume"')" ]; then
        # #3146: any agent volume that holds data but is not in the archive
        # (size cap, a snapshot that died, …), named with its reason below.
        echo -e "Verdict:         ${RED}PARTIAL${NC} — agent volume(s) with data are NOT backed up (see Skipped volumes)"
    elif [ -n "$skipped" ]; then
        echo -e "Verdict:         ${RED}PARTIAL${NC} — above BACKUP_AGENT_VOL_MAX_MB (${cap:-?} MiB), NOT backed up:"
        for pair in $(printf '%s' "$skipped" | tr ',' ' '); do
            echo -e "                 ${pair%%:*} (${pair#*:} MiB)"
        done
    elif [ "${errs:-0}" -gt 0 ] || [ "${warns:-0}" -gt 0 ] || [ "${agents:-0}" -gt 0 ]; then
        echo -e "Verdict:         ${YELLOW}DEGRADED${NC} — details: docker exec backup-service cat /backup/databases/pre-backup.log"
    else
        echo -e "Verdict:         ${GREEN}OK${NC}"
    fi
    # #2709: every skipped agent volume with the reason the backup recorded
    if [ -n "$detail" ]; then
        echo -e "Skipped volumes:"
        for pair in $(printf '%s' "$detail" | tr ',' ' '); do
            local v r z c rest
            v=${pair%%:*}; rest=${pair#*:}
            r=${rest%%:*}; rest=${rest#*:}
            z=${rest%%:*}; c=${rest#*:}
            echo -e "                 ${v} - ${r:-unknown}$([ "${z:--}" != - ] && echo ", ${z} MiB")$([ "${c:--}" != - ] && echo " (cap ${c} MiB)")"
        done
    fi
    echo ""
}

# M032-S09 dry-run guard (defense-in-depth for the test harness — see
# tests/scripts/_helpers.py). Honors RAZZFAZZ_TEST_DRY_RUN=1 for the
# destructive subcommands (delete, backup) and exits 0 BEFORE the
# read prompt or any docker exec.
# #279: `restore` is handled inside its own case branch below instead —
# it needs to parse `--database` first so the dry-run line (and the guard
# test asserting on it) reflects the ACTUAL manager argv the non-dry-run
# path would use, rather than a hand-duplicated echo of raw $1/$2.
if [ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]; then
    case "${1:-}" in
        delete|backup)
            echo "DRY-RUN: would have invoked razzfazz-backup.sh ${1} ${2:-} (RAZZFAZZ_TEST_DRY_RUN=1)" >&2
            exit 0 ;;
    esac
fi

# Main logic
case "${1:-}" in
    list)
        require_container
        docker exec $([ -t 0 ] && echo -it || echo -i) "$CONTAINER_NAME" python /app/backup_manager.py list --check-keys
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
        # #279: parse `--database <name>` out of the remaining args (any
        # position after the first non-flag token, which is the backup
        # file). Presence of --database implies --type single to the
        # manager; its absence keeps the pre-#279 --type full behavior
        # byte-for-byte. RESTORE_FILE/RESTORE_DB/MANAGER_TYPE/
        # MANAGER_RESTORE_ARGS below are the SINGLE source of truth used
        # both by the dry-run print and the real docker exec — so the two
        # cannot drift apart.
        RESTORE_FILE=""
        RESTORE_DB=""
        DB_FLAG_SEEN=false
        # #2524: --no-recreate (or RZFZ_RESTORE_NO_RECREATE=1) leaves the stack
        # DOWN after a full restore instead of recreating it from the restored
        # .env. A staging box restoring a production backup needs this: it puts
        # its own .env back first, so nothing ever starts on production's.
        NO_RECREATE=false
        [ "${RZFZ_RESTORE_NO_RECREATE:-0}" = "1" ] && NO_RECREATE=true
        # #2524: --keep-env leaves the box's .env / .env.dify alone; the
        # manager hands the backup's over in backups/.restored-env/ (mode 600).
        # Only with --no-recreate: recreating the stack on the box's own .env
        # while the databases hold the backup's roles would not come up.
        KEEP_ENV=false
        shift
        while [ $# -gt 0 ]; do
            case "$1" in
                --database)
                    # #840 review: track FLAG PRESENCE separately
                    # from the value, and support both `--database <name>` and
                    # `--database=<name>`. The old exact-token-only parser let
                    # `--database=db` fall through to the positional branch, so
                    # RESTORE_DB stayed empty and the dispatch silently chose a
                    # FULL restore.
                    DB_FLAG_SEEN=true
                    RESTORE_DB="${2:-}"
                    shift
                    # #840: guard the value-shift — a trailing valueless
                    # `--database` has nothing to shift; `shift 2` here would
                    # `exit 1` under `set -eo pipefail` (Z.22) with no message.
                    if [ $# -gt 0 ]; then shift; fi ;;
                --database=*)
                    DB_FLAG_SEEN=true
                    RESTORE_DB="${1#*=}"
                    shift ;;
                --no-recreate)
                    NO_RECREATE=true
                    shift ;;
                --keep-env)
                    KEEP_ENV=true
                    shift ;;
                *)
                    if [ -z "$RESTORE_FILE" ]; then
                        RESTORE_FILE="$1"
                    fi
                    shift ;;
            esac
        done

        if [ -z "$RESTORE_FILE" ]; then
            echo -e "${RED}Error: Please specify backup file to restore${NC}"
            echo "Usage: $0 restore <backup-file> [--database <name>]"
            exit 1
        fi

        # #840 review: decide single-vs-full on FLAG PRESENCE, not
        # on RESTORE_DB being non-empty. Otherwise `--database=name` (equals
        # form), `--database ""`, or a caller variable that resolved empty would
        # SILENTLY fall back to a FULL restore behind the SAME "Are you sure?"
        # prompt — dropping every database, volume and .env. A present-but-empty
        # or malformed name aborts LOUDLY here, BEFORE the confirm prompt.
        if [ "$DB_FLAG_SEEN" = true ]; then
            # Mirror the manager's own gate (db_name_problem: no control
            # character, double quote or backslash — a UUID-named database such
            # as Cognee's per-dataset ones is valid) client-side so an invalid
            # name never reaches the confirm prompt as a disguised full restore.
            # `[[ =~ ]]` (whole-string, no per-line match) rather than a piped
            # grep, so an embedded newline can't slip a partial match through.
            db_name_re='^[^[:cntrl:]"\\]+$'
            if ! [[ "$RESTORE_DB" =~ $db_name_re ]]; then
                echo -e "${RED}Error: --database requires a database name without control characters, double quotes or backslashes (got: '${RESTORE_DB}').${NC}" >&2
                echo -e "${RED}Refusing to fall back to a FULL restore — that would drop every database, volume and .env.${NC}" >&2
                exit 1
            fi
            MANAGER_TYPE="single"
            MANAGER_RESTORE_ARGS=(--file "$RESTORE_FILE" --type single --database "$RESTORE_DB")
        else
            MANAGER_TYPE="full"
            MANAGER_RESTORE_ARGS=(--file "$RESTORE_FILE" --type full)
        fi
        if [ "$KEEP_ENV" = true ]; then
            if [ "$MANAGER_TYPE" != "full" ] || [ "$NO_RECREATE" != true ]; then
                echo -e "${RED}Error: --keep-env needs a full restore with --no-recreate (the stack must not be recreated on this box's own .env while the databases hold the backup's roles).${NC}" >&2
                exit 1
            fi
            MANAGER_RESTORE_ARGS+=(--keep-env)
        fi

        # M032-S09 dry-run guard (see comment at the top of this script) —
        # lives HERE (after parsing) rather than in the generic top-of-file
        # block, so it prints the manager argv that parsing just resolved
        # instead of a hand-duplicated echo of raw $1/$2.
        if [ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]; then
            echo "DRY-RUN: would have invoked razzfazz-backup.sh restore ${MANAGER_RESTORE_ARGS[*]} (RAZZFAZZ_TEST_DRY_RUN=1)" >&2
            exit 0
        fi

        require_container
        if [ -n "$RESTORE_DB" ]; then
            # #279: single-database restore never stops/recreates the
            # stack — restore_single_database() in the manager deliberately
            # leaves every other service running untouched (customer-box
            # incident #278: fixing one corrupt DB must not revert
            # unrelated work). No RAZZFAZZ_RESTORE_HOST_RECREATE, no
            # compose down/up dance below.
            echo -e "${YELLOW}WARNING: This will restore database '$RESTORE_DB' from: $RESTORE_FILE${NC}"
            echo -e "${YELLOW}The rest of the stack keeps running untouched.${NC}"
        else
            echo -e "${YELLOW}WARNING: This will restore from: $RESTORE_FILE${NC}"
        fi
        read -p "Are you sure? (yes/no): " confirm
        if [ "$confirm" = "yes" ]; then
            if [ "$MANAGER_TYPE" = "single" ]; then
                if docker exec $([ -t 0 ] && echo -it || echo -i) \
                        "$CONTAINER_NAME" \
                        python /app/backup_manager.py restore "${MANAGER_RESTORE_ARGS[@]}"; then
                    echo -e "${GREEN}Database '$RESTORE_DB' restored from $RESTORE_FILE. Rest of the stack was left running.${NC}"
                else
                    echo -e "${RED}Error: single-database restore failed; rest of the stack was left untouched.${NC}"
                    exit 1
                fi
                exit 0
            fi
            # #125 (cross-secret-boundary DR): the in-container manager decrypts,
            # stops the stack, restores volumes + databases + .env + agent volumes,
            # then RETURNS WITHOUT bringing the stack back up (we pass
            # RAZZFAZZ_RESTORE_HOST_RECREATE=1 so its legacy `docker start` sweep
            # is suppressed). The authoritative bring-up happens HERE, host-side,
            # with `docker compose down` (KEEPING volumes; the orphan flag is
            # BANNED since #252 — agents would be deleted as false orphans) +
            # `docker compose up -d` + remove_compose_orphans_safe:
            #   * recreating every container re-reads the freshly-restored .env, so
            #     each picks up the restored secrets. `docker start` (what the
            #     manager used to do) re-uses each container's stale baked env →
            #     `password authentication failed` crash loop after a restore that
            #     crosses a secret boundary (fresh init then restore older backup).
            #   * the HOST has the compose project + restored .env; the manager
            #     container does not — and a container recreating ITSELF mid-run is
            #     a chicken-and-egg we sidestep entirely by orchestrating from here.
            # #2464 fix round 1: the manager's exit code has THREE values.
            #   0  complete                           -> recreate the stack
            #   3  PARTIAL: databases + .env restored, a non-critical core
            #      volume (#2712), certs/, the box-local state (.checksums.db,
            #      overlay/) or an agent volume NOT -> recreate the stack
            #      (it has its data back and must not stay down), then say
            #      loudly what is missing; exit 3
            #   4  refused BEFORE anything was stopped (#2678): the stack is
            #      running and untouched -> say so; exit 4
            #   *  failed (the databases), and every usage error -> stack NOT
            #      recreated. Fix round 2: NOT 2 — argparse and python use 2
            #      for "the restore never ran" (`restore -h`, a `-`-file).
            #   The manager's own "PARTIAL RESTORE: …" lines name what is
            #   missing (a core volume, certs/, …); they scroll away under the
            #   recreate's output, so its stdout (where the manager logs) is
            #   also captured (a 0600 temp file, removed at once) and the
            #   closing banner repeats them; its stderr stays stderr. The
            #   verdict is the MANAGER's exit code (PIPESTATUS[0]), never the
            #   pipeline's: a tee that fails (a full /tmp) must not turn a
            #   good restore into "failed" and leave the stack down. No temp
            #   file (mktemp failed): the banner points at the output above.
            restore_rc=0
            if restore_out=$(mktemp "${TMPDIR:-/tmp}/rzfz-restore.XXXXXX" 2>/dev/null); then
                trap 'rm -f "$restore_out"' EXIT
            else
                echo -e "${YELLOW}Note: no temp file for the restore's output; the summary at the end will point at the output above.${NC}" >&2
                restore_out=/dev/null
            fi
            set +e
            # #3478: an incomplete staged volume copy (wazuh-manager-data) is
            # restored only when the operator says so; pass that through.
            allow_incomplete=()
            if [ "${RAZZFAZZ_RESTORE_ALLOW_INCOMPLETE_STAGED:-}" = "1" ]; then
                allow_incomplete=(-e RAZZFAZZ_RESTORE_ALLOW_INCOMPLETE_STAGED=1)
            fi
            docker exec $([ -t 0 ] && echo -it || echo -i) \
                    -e RAZZFAZZ_RESTORE_HOST_RECREATE=1 \
                    "${allow_incomplete[@]}" \
                    "$CONTAINER_NAME" \
                    python /app/backup_manager.py restore "${MANAGER_RESTORE_ARGS[@]}" \
                | tee "$restore_out"
            restore_rc=${PIPESTATUS[0]}
            set -e
            restore_missing=""
            if [ "$restore_out" != /dev/null ]; then
                restore_missing=$(tr -d '\r' < "$restore_out" 2>/dev/null | grep -o 'PARTIAL RESTORE:.*' || true)
                rm -f "$restore_out"
            fi
            if [ "$restore_rc" -eq 0 ] || [ "$restore_rc" -eq 3 ]; then
                # #2524: the dump replays the owners the backed-up box had —
                # also an object its bootstrap superuser created before the
                # per-service role existed (Authentik's pgtrigger function, which
                # failed every Authentik migration after a restore). Hand every
                # object of each service DB to its role before anything starts
                # on it. postgres is up: the restore started it.
                reconcile_service_db_ownership \
                    || print_warning "DB-ownership reconcile incomplete after the restore: a service whose objects stay mis-owned fails its next migration (see above)."
                # #3412: before anything runs compose on the restored .env.
                # With --keep-env the box keeps its own .env, so its own
                # overlays stay too.
                if [ "$KEEP_ENV" != true ]; then
                    restore_compose_local_overlays
                fi
            fi
            if { [ "$restore_rc" -eq 0 ] || [ "$restore_rc" -eq 3 ]; } && [ "$NO_RECREATE" = true ]; then
                # #2524: the caller owns the bring-up (see NO_RECREATE above).
                echo -e "${YELLOW}Restore extracted (--no-recreate): nothing was started on the restored .env. Only the database containers the restore needed (on their existing container config), backup-service and docker-socket-proxy are up.${NC}"
                echo -e "${YELLOW}Put the right .env in place, then bring the stack up with: rzfz start${NC}"
                if [ "$KEEP_ENV" = true ]; then
                    # #2524: the handover holds the backup's secrets; say where, and how to get rid of it.
                    handover="$SCRIPT_DIR/backups/.restored-env"
                    echo -e "${YELLOW}WARNING (--keep-env): this box's .env and .env.dify were NOT changed. The backup's copies — its secrets — are in ${handover}/ (mode 600).${NC}" >&2
                    echo -e "${YELLOW}Take what you need, then destroy them: shred -u ${handover}/.env ${handover}/.env.dify ${handover}/restore-status.json 2>/dev/null; rmdir ${handover}${NC}" >&2
                fi
                restore_reprovision_speaches later
                if [ "$restore_rc" -eq 3 ]; then
                    echo -e "${RED}PARTIAL RESTORE: NOT everything came back; put it back by hand. The manager said:${NC}" >&2
                    printf '%s\n' "${restore_missing:-(no PARTIAL RESTORE line was captured; see the output above)}" >&2
                fi
                exit "$restore_rc"
            elif [ "$restore_rc" -eq 0 ] || [ "$restore_rc" -eq 3 ]; then
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
                # restored must NOT be removed. #252/#670 review: NO
                # --remove-orphans either — socket-provisioned agents look
                # like orphans to compose and a restore would delete them
                # all; remove_compose_orphans_safe (lib.sh) covers the
                # leftover-variant case (e.g. the inactive gpustack-*)
                # agent-safely.
                if docker compose down \
                   && { remove_compose_orphans_safe || true; } \
                   && docker compose up -d; then
                    echo -e "${GREEN}Stack recreated with restored configuration.${NC}"
                    restore_reprovision_speaches now
                    restore_revive_out_of_project
                else
                    echo -e "${RED}Error: bringing the stack back up after restore failed.${NC}"
                    echo -e "${RED}Volumes/.env are restored; re-run 'docker compose up -d' manually.${NC}"
                    exit 1
                fi
                if [ "$restore_rc" -eq 3 ]; then
                    echo -e "${RED}==================================================================${NC}" >&2
                    echo -e "${RED}PARTIAL RESTORE: the databases (and the .env, when the backup carries one) are restored and the stack${NC}" >&2
                    echo -e "${RED}is back up, but NOT everything came back: core volumes, certs/ (TLS),${NC}" >&2
                    echo -e "${RED}.checksums.db + overlay/ (governance baseline, Enterprise docs)${NC}" >&2
                    echo -e "${RED}and/or per-user agent volumes. Put those back by hand. The manager said:${NC}" >&2
                    printf '%s\n' "${restore_missing:-(no PARTIAL RESTORE line was captured; see the output above)}" >&2
                    echo -e "${RED}==================================================================${NC}" >&2
                    exit 3
                fi
            elif [ "$restore_rc" -eq 4 ]; then
                # #2678: the manager refused BEFORE it stopped anything (the
                # archive did not verify, decrypt or pass validation, or is
                # too new). Nothing was stopped or written: say so, and do not
                # tell the operator to keep the stack down.
                echo -e "${RED}Restore refused before anything was changed: the manager's error above says why.${NC}"
                echo -e "${RED}No service was stopped and no volume or .env was touched; the stack keeps running.${NC}"
                exit 4
            else
                echo -e "${RED}Error: in-container restore failed; stack NOT recreated.${NC}"
                echo -e "${RED}The manager's error above names what failed. If it names the .env:${NC}"
                echo -e "${RED}the volumes and databases may already hold the backup's data while .env is${NC}"
                echo -e "${RED}the box's own. Put the backup's BACKUP_ENCRYPTION_PASSWORD into .env and${NC}"
                echo -e "${RED}run the restore again; do not start the stack before.${NC}"
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
