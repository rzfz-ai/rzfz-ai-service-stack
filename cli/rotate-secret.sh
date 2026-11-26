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

# `--help`/`-h` must render WITHOUT a .env — the docs generator and a fresh
# checkout have none. Handle it before the .env precondition below so the help
# is environment-independent. Previously the missing-.env error truncated the
# whole help block, so the docs-freshness gate could never pass on a box (or CI
# checkout) that has no .env. Mirrors the late `-h|--help` handler verbatim.
for _arg in "$@"; do case "$_arg" in
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac; done

# #266 (E7): a stack-bound verb refuses on a thin inference node instead of
# reading a .env that is not there. Placed AFTER the early --help handler so
# help still renders anywhere, and BEFORE the .env precondition so the
# operator gets "this is a worker" rather than ".env not found".
refuse_on_worker_node rotate-secret "The secrets it rotates live in the master's .env; a worker holds only its own enrolment key."

ENV_FILE="${SCRIPT_DIR}/.env"
[ -f "$ENV_FILE" ] || { print_error ".env not found at $ENV_FILE"; exit 1; }

PG_USER="$(read_env_value "$ENV_FILE" POSTGRES_USER)"; PG_USER="${PG_USER:-docker}"

# registry rows: KEY|CLASS|CONSUMERS|NOTE   (CONSUMERS space-separated; * = special handler)
SECRETS=(
  "AUTHENTIK_SECRET_KEY|DONOTROTATE||encrypts Authentik DB fields (OAuth secrets/tokens) — no in-place recovery; reinstall Authentik only"
  "BACKUP_ENCRYPTION_PASSWORD|DATALOSS|backup razzfazz-backup-management|archives need the value they were sealed with — the keyring keeps the last 3 (#2132); older ones become unrestorable"
  "POSTGRES_PASSWORD|COORDINATED|*|shared DB superuser — consumed by every DB-backed service"
  "VALKEY_PASSWORD|COORDINATED|*|valkey requirepass + every cache consumer"
  "SMTP_INTERNAL_PASSWORD|COORDINATED|smtp-relay|re-keys SASL; hardcoded SMTP clients (Dify email) break with 535 (#140)"
  "GPUSTACK_MASTER_SERVER_TOKEN|COORDINATED|gpustack-legacy|worker join token — workers must re-register"
  "WEBUI_SECRET_KEY|SAFE|openwebui|OpenWebUI session signing (logs users out)"
  "GITEA_SECRET_KEY|SAFE|gitea|Gitea internal signing (resets sessions)"
  "GITEA_INTERNAL_TOKEN|SAFE|gitea|Gitea internal IPC token"
  "PLUGIN_DAEMON_KEY|SAFE|dify-api dify-plugin-daemon|Dify API <-> plugin-daemon shared key"
  "LIGHTRAG_TOKEN_SECRET|SAFE|lightrag|LightRAG JWT signing (resets sessions)"
  "AUTHENTIK_BOOTSTRAP_TOKEN|SAFE|authentik-server authentik-worker|Authentik bootstrap API token"
  "DOCLING_RELAY_SECRET|SAFE|docling-egress mcp-manager|MCP gateway <-> Docling egress relay (both read .env; no session state)"
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
    # The copy holds EVERY secret in the clear (and the OLD value of the one being
    # rotated), so it is owner-only: `cp -f` onto an existing file keeps the old mode,
    # hence the explicit chmod. It is kept on purpose — the rollback recipes and the
    # "OLD value preserved" promise depend on it; delete it once the rotation is verified.
    ( umask 077; cp -f "$ENV_FILE" "${ENV_FILE}.pre-rotate-bak" )
    chmod 600 "${ENV_FILE}.pre-rotate-bak"
    print_substep "Snapshotted .env → ${ENV_FILE}.pre-rotate-bak (mode 0600, kept for rollback — delete it when the rotation is verified; + encrypted env-snapshot)"
}

DC="$(docker_compose_cmd 2>/dev/null || echo 'docker compose')"
# Consumers are compose SERVICE names (not container names). stdout is quiet,
# stderr stays visible so a failed recreate says why instead of a bare exit 1.
recreate() {
    ( cd "$SCRIPT_DIR" && $DC up -d --force-recreate "$@" >/dev/null ) \
        || { print_error "Could not recreate: $* — the consumer keeps the OLD secret in its environment."; return 1; }
}

# Container table: one line per container, "name|id|status|compose-service".
_container_table() {
    docker ps -a --no-trunc --format '{{.Names}}|{{.ID}}|{{.Status}}|{{.Label "com.docker.compose.service"}}' 2>/dev/null || true
}

# A status line counts as settled when the container is up and not still starting,
# not unhealthy, not restarting; a one-shot that exited 0 is settled too.
_status_ok() {
    case "$1" in
        *unhealthy*|*"health: starting"*|Restarting*|Created*|Dead*|Removing*|Paused*) return 1 ;;
        Up*|"Exited (0)"*) return 0 ;;
        *) return 1 ;;
    esac
}

# Taken BEFORE the rotation changes anything: the table of containers as they are.
# The gate compares against it, so a container that was already unhealthy (or is
# not one of the recreated consumers) does not decide the verdict.
PRE_ROTATION_TABLE=""
health_snapshot() { PRE_ROTATION_TABLE="$(_container_table)"; }

# Seconds the consumers may take to settle: the largest start_period + interval x
# retries of any service's healthcheck, at least 120. RZFZ_ROTATE_HEALTH_TIMEOUT overrides.
_health_deadline() {
    if [ -n "${RZFZ_ROTATE_HEALTH_TIMEOUT:-}" ]; then echo "$RZFZ_ROTATE_HEALTH_TIMEOUT"; return 0; fi
    local d
    d="$( ( cd "$SCRIPT_DIR" && $DC config --format json 2>/dev/null ) | python3 -c '
import json, re, sys
def secs(v, default):
    if v in (None, ""):
        return default
    t = 0
    for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", str(v)):
        t += float(n) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[u]
    return t or default
best = 120
try:
    for svc in (json.load(sys.stdin).get("services") or {}).values():
        hc = svc.get("healthcheck") or {}
        if not hc or hc.get("disable"):
            continue
        best = max(best, secs(hc.get("start_period"), 0) + secs(hc.get("interval"), 30) * int(hc.get("retries") or 3))
except Exception:
    pass
print(int(best))' 2>/dev/null || true)"
    case "$d" in ''|*[!0-9]*) d=120 ;; esac
    [ "$d" -ge 120 ] || d=120
    echo "$d"
}

# coordinated_health_gate <secret> <undo-recipe>
# After a COORDINATED rotation, poll the consumers that were RECREATED (new
# container id compared to health_snapshot) until each is up and healthy, or the
# deadline passes. Containers that were already unhealthy before the rotation do
# not decide the verdict; they are named separately. Clean -> success. Broken ->
# print the EXACT manual un-rotate recipe (the danger matrix promises a failed
# coordinated rotation is recoverable).
coordinated_health_gate() {
    local secret=$1 undo=$2
    local deadline interval waited=0 line name id st svc
    deadline="$(_health_deadline)"; interval="${RZFZ_ROTATE_HEALTH_POLL:-5}"
    print_substep "Waiting for the recreated consumers to settle (up to ${deadline}s)..."
    local bad table prior
    while :; do
        table="$(_container_table)"
        bad=""; prior=""
        while IFS='|' read -r name id st svc; do
            [ -n "$name" ] || continue
            local pre_line pre_id pre_st
            pre_line="$(printf '%s\n' "$PRE_ROTATION_TABLE" | awk -F'|' -v n="$name" '$1 == n { print; exit }')"
            pre_id="$(printf '%s' "$pre_line" | cut -d'|' -f2)"
            pre_st="$(printf '%s' "$pre_line" | cut -d'|' -f3)"
            if [ -n "$pre_line" ] && [ "$pre_id" = "$id" ]; then
                # not recreated: only worth a mention when it was already broken before
                _status_ok "$st" || { [ -n "$pre_st" ] && ! _status_ok "$pre_st" && prior="${prior}${name} ${st}"$'\n'; }
                continue
            fi
            if [ -n "$pre_st" ] && ! _status_ok "$pre_st"; then
                _status_ok "$st" || prior="${prior}${name} ${st}"$'\n'
                continue
            fi
            _status_ok "$st" && continue
            bad="${bad}${name} ${st}"$'\n'
        done <<< "$table"
        [ -n "$bad" ] || break
        [ "$waited" -lt "$deadline" ] || break
        sleep "$interval"
        waited=$((waited + interval))
    done
    if [ -n "$prior" ]; then
        print_warning "Already unhealthy before the rotation (not part of this verdict):"
        printf '%s' "$prior" | sed 's/^/    /'
    fi
    if [ -z "$bad" ]; then
        print_success "${secret} rotated (COORDINATED) — no broken consumers. .env backup: ${ENV_FILE}.pre-rotate-bak"
        return 0
    fi
    print_error "${secret} rotated, but these consumers are NOT healthy after ${waited}s:"
    printf '%s' "$bad" | sed 's/^/    /'
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
    health_snapshot
    check_container postgres || { print_error "postgres not running."; return 1; }
    print_substep "ALTER USER ${PG_USER} PASSWORD in postgres..."
    # #2746: the new password reaches PostgreSQL only as a SCRAM-SHA-256
    # verifier, on stdin: not in argv (`ps`), not as plaintext DDL in the server log.
    local verifier
    verifier="$(RZFZ_ROLE_PW="$new" scram_sha256_verifier RZFZ_ROLE_PW)" \
        || { print_error "Could not compute the password verifier — nothing changed."; return 1; }
    if ! printf 'ALTER USER "%s" PASSWORD '"'"'%s'"'"';\n' "$PG_USER" "$verifier" \
            | docker exec -i postgres psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -f - >/dev/null 2>&1; then
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
    health_snapshot
    check_container valkey || { print_error "valkey not running."; return 1; }
    local oldpw; oldpw="$(read_env_value "$ENV_FILE" VALKEY_PASSWORD)"
    print_substep "CONFIG SET requirepass on valkey..."
    # #2746: the passwords are never on argv: the auth one travels in the exec
    # environment by name (REDISCLI_AUTH), the new one on stdin.
    local qnew="${new//\\/\\\\}"; qnew="${qnew//\"/\\\"}"   # valkey-cli parses "..." with \\ and \" escapes
    local setout
    setout="$(printf 'CONFIG SET requirepass "%s"\n' "$qnew" \
            | REDISCLI_AUTH="$oldpw" docker exec -i -e REDISCLI_AUTH valkey valkey-cli --no-auth-warning 2>&1)" || setout=""
    if [ "$setout" != "OK" ]; then
        print_error "CONFIG SET requirepass failed — nothing changed."; return 1
    fi
    REDISCLI_AUTH="$new" docker exec -e REDISCLI_AUTH valkey valkey-cli --no-auth-warning CONFIG REWRITE >/dev/null 2>&1 || true
    update_env_value "$ENV_FILE" VALKEY_PASSWORD "$new"
    # #2977: the marker says which rotation generation this password belongs to,
    # so the upgrade that rotates it for the log leak does not rotate it again.
    update_env_value "$ENV_FILE" VALKEY_PASSWORD_ROTATION "$VALKEY_ROTATION_GENERATION" || true
    # Dify keeps its own copies (REDIS_PASSWORD, the password inside
    # CELERY_BROKER_URL) in .env.dify: they follow, or its workers crash-loop on
    # "invalid username-password pair" while the rotation reports success.
    local dify_synced=0
    if sync_dify_valkey_password "$ENV_FILE" "${SCRIPT_DIR}/.env.dify"; then
        dify_synced=1
        print_substep "Synced the new password into .env.dify (REDIS_PASSWORD + CELERY_BROKER_URL)."
    fi
    # Plain `up -d`: recreates ONLY the cache consumers whose env changed. gpustack
    # is not a valkey consumer, so it is left running — no model-reload storm.
    print_substep "Recreating valkey consumers (compose detects the env change)..."
    ( cd "$SCRIPT_DIR" && $DC up -d >/dev/null 2>&1 ) || true
    if [ "$dify_synced" = 1 ]; then
        # An edited env_file is not always seen as a config change: recreate the
        # Dify services that exist, WITHOUT their dependencies (valkey, postgres).
        local dsvc present=""
        for dsvc in dify-api dify-worker dify-worker-beat dify-worker-dataset; do
            _container_table | awk -F'|' -v s="$dsvc" '$4 == s { f = 1 } END { exit !f }' && present="$present $dsvc"
        done
        if [ -n "$present" ]; then
            print_substep "Recreating the Dify services:${present}"
            # shellcheck disable=SC2086
            ( cd "$SCRIPT_DIR" && $DC up -d --no-deps --force-recreate $present >/dev/null ) \
                || print_error "Could not recreate:${present} — they keep the OLD password in their environment."
        fi
    fi
    local gate_rc=0
    coordinated_health_gate VALKEY_PASSWORD \
        "docker exec valkey valkey-cli -a '<NEW-pw>' --no-auth-warning CONFIG SET requirepass '<OLD-pw-from-backup>'" \
        || gate_rc=1
    # #2977: a container that is up and healthy can still hold the old password
    # (an agent instance is not a compose service; a recreate that failed
    # quietly leaves the old env). The old one must be refused by valkey and be
    # in no running container's environment: otherwise this rotation FAILS here,
    # by name, not later as a refused connection.
    valkey_rotation_stale_check "$oldpw" || gate_rc=1
    return $gate_rc
}

rotate_dataloss() {
    local key=$1 consumers=$2
    snapshot_env "$key"
    local new; new="$(generate_password 32)"
    if [ "$key" = "BACKUP_ENCRYPTION_PASSWORD" ]; then
        # #2132: keep the previous generations; restores try them. Sourced here,
        # not at the top: a worker ships no keyring library and must still reach
        # --help and the worker refusal (#266).
        # shellcheck source=scripts/lib-backup-keyring.sh
        source "${SCRIPT_DIR}/scripts/lib-backup-keyring.sh"
        backup_keyring_rotate "$ENV_FILE" "$new" \
            || { print_error "keyring update failed — ${key} is unchanged."; exit 1; }
    else
        print_warning "⚠ Rotating ${key} makes existing encrypted backups UNRESTORABLE."
        print_info  "  Keep the current value somewhere safe — you need it to restore any backup taken before now."
        update_env_value "$ENV_FILE" "$key" "$new"
    fi
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
            print_info  "Re-run with --i-understand-data-loss. The current value is kept as BACKUP_ENCRYPTION_PASSWORD_PREVIOUS_1 (#2132); a 4th-oldest generation falls off."
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
