#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz-rotate-secret.sh  (#149)
# ==============================================================================
# Rotate an INFRASTRUCTURE secret/key — the dangerous half of secret management.
# Unlike razzfazz-set-admin-password.sh (#47, app LOGIN passwords), these secrets
# encrypt data at rest or are shared across services; rotating them wrong bricks
# the stack or loses data. This tool ENFORCES the danger matrix in
# docs/secret-rotation-danger-matrix.md:
#
#   🟢 SAFE         — auto-rotate (regenerate → .env → recreate the consumer).
#   🟡 COORDINATED  — ordered multi-service re-key; requires --yes.
#   🔴 DATA-LOSS    — refused unless --i-understand-data-loss.
#   ⛔ DO-NOT-ROTATE— always refused (no in-place recovery).
#
# Usage:
#   razzfazz-rotate-secret.sh --list
#   razzfazz-rotate-secret.sh --rotate <KEY> [--yes] [--i-understand-data-loss]
#
# Every rotation snapshots .env first (scripts/env-snapshot.sh) so it is
# reversible. COORDINATED rotations roll back .env + the DB on failure.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"
ENV_FILE="${SCRIPT_DIR}/.env"
[ -f "$ENV_FILE" ] || { print_error ".env not found at $ENV_FILE"; exit 1; }

PG_USER="$(read_env_value "$ENV_FILE" POSTGRES_USER)"; PG_USER="${PG_USER:-docker}"

# registry rows: KEY|CLASS|CONSUMERS|NOTE   (CONSUMERS space-separated; * = special handler)
SECRETS=(
  "AUTHENTIK_SECRET_KEY|DONOTROTATE||encrypts Authentik DB fields (OAuth secrets/tokens) — no in-place recovery; reinstall Authentik only"
  "BACKUP_ENCRYPTION_PASSWORD|DATALOSS|backup-service|existing GPG-encrypted backups become unrestorable with a new value"
  "POSTGRES_PASSWORD|COORDINATED|*|shared DB superuser — consumed by every DB-backed service"
  "VALKEY_PASSWORD|COORDINATED|*|valkey requirepass + every cache consumer"
  "SMTP_INTERNAL_PASSWORD|COORDINATED|smtp-relay|re-keys SASL; hardcoded SMTP clients (Dify email) break with 535 (#140)"
  "GPUSTACK_MASTER_SERVER_TOKEN|COORDINATED|gpustack|worker join token — workers must re-register"
  "WEBUI_SECRET_KEY|SAFE|openwebui|OpenWebUI session signing (logs users out)"
  "GITEA_SECRET_KEY|SAFE|gitea|Gitea internal signing (resets sessions)"
  "GITEA_INTERNAL_TOKEN|SAFE|gitea|Gitea internal IPC token"
  "PLUGIN_DAEMON_KEY|SAFE|dify-api dify-plugin-daemon|Dify API <-> plugin-daemon shared key"
  "LIGHTRAG_TOKEN_SECRET|SAFE|lightrag|LightRAG JWT signing (resets sessions)"
  "AUTHENTIK_BOOTSTRAP_TOKEN|SAFE|authentik-server authentik-worker|Authentik bootstrap API token"
)

_row() { local r; for r in "${SECRETS[@]}"; do [ "${r%%|*}" = "$1" ] && { echo "$r"; return 0; }; done; return 1; }
_field() { echo "$1" | cut -d'|' -f"$2"; }

class_glyph() { case "$1" in SAFE) echo "🟢 SAFE";; COORDINATED) echo "🟡 COORDINATED";; DATALOSS) echo "🔴 DATA-LOSS";; DONOTROTATE) echo "⛔ DO-NOT-ROTATE";; *) echo "$1";; esac; }

list_secrets() {
    print_step "Rotatable secrets (danger matrix — docs/secret-rotation-danger-matrix.md)"
    local r key cls note
    for r in "${SECRETS[@]}"; do
        key="$(_field "$r" 1)"; cls="$(_field "$r" 2)"; note="$(_field "$r" 4)"
        printf "  %-32s %-16s %s\n" "$key" "$(class_glyph "$cls")" "$note"
    done
    echo ""
    print_info "Rotate with: $0 --rotate <KEY> [--yes] [--i-understand-data-loss]"
}

snapshot_env() {
    if [ -x "${SCRIPT_DIR}/scripts/env-snapshot.sh" ]; then
        "${SCRIPT_DIR}/scripts/env-snapshot.sh" take "pre-rotate-${1}" >/dev/null 2>&1 || true
    fi
    cp -f "$ENV_FILE" "${ENV_FILE}.pre-rotate-bak"
    print_substep "Snapshotted .env → ${ENV_FILE}.pre-rotate-bak (+ encrypted env-snapshot)"
}

DC="$(docker_compose_cmd 2>/dev/null || echo 'docker compose')"
recreate() { ( cd "$SCRIPT_DIR" && $DC up -d --force-recreate "$@" >/dev/null 2>&1 ); }

# coordinated_health_gate <secret> <undo-recipe>
# After a COORDINATED rotation, wait for the stack to settle and count broken
# consumers. Clean → success. Broken → print the EXACT manual un-rotate recipe
# (the danger matrix promises a failed coordinated rotation is recoverable).
coordinated_health_gate() {
    local secret=$1 undo=$2
    print_substep "Waiting for consumers to settle (20s)..."
    sleep 20
    local broken
    broken="$(docker ps --format '{{.Names}} {{.Status}}' | grep -iE 'restarting|unhealthy' || true)"
    if [ -z "$broken" ]; then
        print_success "${secret} rotated (COORDINATED) — no broken consumers. .env backup: ${ENV_FILE}.pre-rotate-bak"
        return 0
    fi
    print_error "${secret} rotated, but these consumers are NOT healthy:"
    echo "$broken" | sed 's/^/    /'
    print_warning "Roll back NOW if they don't recover:"
    echo "    cp -f ${ENV_FILE}.pre-rotate-bak ${ENV_FILE}"
    echo "    ${undo}"
    echo "    cd ${SCRIPT_DIR} && ${DC} up -d"
    return 1
}

# ── handlers ──────────────────────────────────────────────────────────────────
rotate_safe() {
    local key=$1 consumers=$2
    local new; new="$(generate_secret 48)"
    snapshot_env "$key"
    update_env_value "$ENV_FILE" "$key" "$new"
    print_substep "Recreating: ${consumers:-<none>}"
    # shellcheck disable=SC2086
    [ -n "$consumers" ] && recreate $consumers
    print_success "$key rotated (SAFE). Active sessions for the affected app are reset."
}

rotate_postgres() {
    local new; new="$(generate_password 32)"
    snapshot_env POSTGRES_PASSWORD
    check_container postgres || { print_error "postgres not running."; return 1; }
    print_substep "ALTER USER ${PG_USER} PASSWORD in postgres..."
    if ! docker exec postgres psql -U "$PG_USER" -d postgres -c "ALTER USER \"${PG_USER}\" PASSWORD '${new}';" >/dev/null 2>&1; then
        print_error "ALTER USER failed — nothing changed."; return 1
    fi
    update_env_value "$ENV_FILE" POSTGRES_PASSWORD "$new"
    # keep any *_DB_PASSWORD that equalled the OLD superuser pw in lockstep
    local oldpw; oldpw="$(read_env_value "${ENV_FILE}.pre-rotate-bak" POSTGRES_PASSWORD)"
    local k v
    for k in $(grep -oE '^[A-Z_]+_DB_PASSWORD=' "$ENV_FILE" | tr -d '='); do
        v="$(read_env_value "$ENV_FILE" "$k")"
        [ "$v" = "$oldpw" ] && update_env_value "$ENV_FILE" "$k" "$new"
    done
    # Plain `up -d` (NOT --force-recreate): compose recreates ONLY services whose
    # resolved env changed — i.e. exactly the postgres consumers (incl. gpustack,
    # which holds gpustack_db). Services that don't reference the pw are left alone.
    print_substep "Recreating postgres consumers (compose detects the env change)..."
    ( cd "$SCRIPT_DIR" && $DC up -d >/dev/null 2>&1 ) || true
    coordinated_health_gate POSTGRES_PASSWORD \
        "docker exec postgres psql -U \"${PG_USER}\" -d postgres -c \"ALTER USER \\\"${PG_USER}\\\" PASSWORD '<OLD-pw-from-backup>';\""
}

rotate_valkey() {
    local new; new="$(generate_password 32)"
    snapshot_env VALKEY_PASSWORD
    check_container valkey || { print_error "valkey not running."; return 1; }
    local oldpw; oldpw="$(read_env_value "$ENV_FILE" VALKEY_PASSWORD)"
    print_substep "CONFIG SET requirepass on valkey..."
    if ! docker exec valkey valkey-cli -a "$oldpw" --no-auth-warning CONFIG SET requirepass "$new" >/dev/null 2>&1; then
        print_error "CONFIG SET requirepass failed — nothing changed."; return 1
    fi
    docker exec valkey valkey-cli -a "$new" --no-auth-warning CONFIG REWRITE >/dev/null 2>&1 || true
    update_env_value "$ENV_FILE" VALKEY_PASSWORD "$new"
    # Plain `up -d`: recreates ONLY the cache consumers whose env changed. gpustack
    # is not a valkey consumer, so it is left running — no model-reload storm.
    print_substep "Recreating valkey consumers (compose detects the env change)..."
    ( cd "$SCRIPT_DIR" && $DC up -d >/dev/null 2>&1 ) || true
    coordinated_health_gate VALKEY_PASSWORD \
        "docker exec valkey valkey-cli -a '<NEW-pw>' --no-auth-warning CONFIG SET requirepass '<OLD-pw-from-backup>'"
}

rotate_dataloss() {
    local key=$1 consumers=$2
    print_warning "⚠ Rotating ${key} makes existing encrypted backups UNRESTORABLE."
    print_info  "  Keep the current value somewhere safe — you need it to restore any backup taken before now."
    snapshot_env "$key"
    local new; new="$(generate_password 32)"
    update_env_value "$ENV_FILE" "$key" "$new"
    # shellcheck disable=SC2086
    [ -n "$consumers" ] && recreate $consumers
    print_success "$key rotated (DATA-LOSS acknowledged). OLD value preserved in ${ENV_FILE}.pre-rotate-bak."
}

# ── arg parse ─────────────────────────────────────────────────────────────────
MODE="" KEY="" ASSUME_YES=false ACK_DATALOSS=false
while [ $# -gt 0 ]; do
    case "$1" in
        --list|--status) MODE="list"; shift ;;
        --rotate) MODE="rotate"; KEY="${2:?--rotate needs a KEY}"; shift 2 ;;
        --yes|-y) ASSUME_YES=true; shift ;;
        --i-understand-data-loss) ACK_DATALOSS=true; shift ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) print_error "Unknown option: $1"; exit 1 ;;
    esac
done
[ -z "$MODE" ] && { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ "$MODE" = "list" ] && { list_secrets; exit 0; }

# rotate
row="$(_row "$KEY")" || { print_error "Unknown / non-rotatable key: $KEY. Use --list."; exit 1; }
CLASS="$(_field "$row" 2)"; CONSUMERS="$(_field "$row" 3)"; NOTE="$(_field "$row" 4)"
[ "$CONSUMERS" = "*" ] && CONSUMERS=""

print_step "Rotate ${KEY}  [$(class_glyph "$CLASS")]"
print_info "$NOTE"

case "$CLASS" in
    DONOTROTATE)
        print_error "REFUSED: ${KEY} cannot be rotated in place — there is no clean recovery."
        print_info  "The only supported path is reinstalling the affected module (you lose its config)."
        exit 2 ;;
    DATALOSS)
        if [ "$ACK_DATALOSS" != true ]; then
            print_error "REFUSED: rotating ${KEY} is destructive to existing data."
            print_info  "Re-run with --i-understand-data-loss if you accept that old encrypted backups become unrestorable."
            exit 2
        fi
        rotate_dataloss "$KEY" "$CONSUMERS" ;;
    COORDINATED)
        if [ "$ASSUME_YES" != true ]; then
            print_warning "${KEY} is a COORDINATED rotation. It recreates every service that RECEIVES"
            print_warning "this secret in its env — on LLM boxes that includes gpustack (→ a model reload)."
            printf 'Proceed? Run on 0.91 first, never first on prod. [y/N] '
            read -r ans; case "$ans" in y|Y|yes) ;; *) print_info "Aborted."; exit 0 ;; esac
        fi
        case "$KEY" in
            POSTGRES_PASSWORD) rotate_postgres ;;
            VALKEY_PASSWORD)   rotate_valkey ;;
            *)                 rotate_safe "$KEY" "$CONSUMERS" ;;  # generic env+recreate
        esac ;;
    SAFE)
        rotate_safe "$KEY" "$CONSUMERS" ;;
    *) print_error "Unknown class $CLASS for $KEY"; exit 1 ;;
esac
