#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz module — host-side module operations the Configuration Portal cannot do
# Available from 2026.11; earlier releases (2026.09-ga.5 and before) have no such command.
#
# Usage:
#   rzfz module reset-data <profile> --i-understand-data-loss
#       the module's database holds data sealed with a key that is no
#       longer in .env. Takes an encrypted safety dump into
#       backups/module-reset/, then drops and recreates the database (same
#       owner). Enable the module afterwards: it starts on fresh keys.
#       The profile must be disabled first.
#   rzfz module reset-data matrix --i-understand-data-loss
#       drops ALL Matrix data (users, rooms, messages, media, signing key)
#       and starts over under the current domain as the new server name. Takes
#       encrypted backups of synapse_db and the synapse-data volume first.
#       Use it when keeping the history is not wanted. Matrix must be disabled.
#   rzfz module prepare matrix
#       fill the matrix secrets that are empty and render Synapse's
#       config (homeserver.yaml.rendered, Element's config.json). Then enable
#       matrix in the Configuration Portal.
#   rzfz module enable matrix
#       prepare, then enable the profile, bring Synapse + Element up,
#       fix synapse-data ownership / the Caddy CA, re-apply the Synapse OIDC
#       blueprint (authentik-init), check the matrix apps' policy bindings
#       and wait for Synapse to be healthy.
# ==============================================================================
set -euo pipefail

for _arg in "$@"; do
    case "$_arg" in -h|--help) sed -n '5,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac
done

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# The stack this acts on (.env, compose, backups/). The tests point it at a
# tree of their own; on a box it is the install path.
STACK_DIR="${RZFZ_MODULE_STACK_DIR:-$SCRIPT_DIR}"
cd "$STACK_DIR"
ENV_FILE="${STACK_DIR}/.env"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"
# shellcheck source=scripts/lib-matrix.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib-matrix.sh"
refuse_on_worker_node module "Modules live on the master; a worker runs none."
[ -f "$ENV_FILE" ] || { print_error ".env not found in ${STACK_DIR}"; exit 1; }

PG_USER="$(read_env_value "$ENV_FILE" POSTGRES_USER)"; PG_USER="${PG_USER:-docker}"
# The password travels in the environment (docker exec -e PGPASSWORD, no value),
# never in an argv that ps would show.
PGPASSWORD="$(read_env_value "$ENV_FILE" POSTGRES_PASSWORD)"; export PGPASSWORD

data_bound() {  # <all|keys|database|check> [args]
    PYTHONPATH="${SCRIPT_DIR}/core/common${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -m razzfazz_common.data_bound_secrets "$1" \
        --profiles-yaml "${SCRIPT_DIR}/core/config/profiles.yaml" "${@:2}"
}

psql_c() {  # <db> <sql>: one statement, unaligned, stops on error
    docker exec -i -e PGPASSWORD postgres \
        psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$1" -tAc "$2"
}

# #2981 - the Matrix half of `reset-data matrix`. The Synapse data volume holds
# the media store and the signing key; both belong to the old server name.
matrix_volume_name() {
    local proj; proj=$(read_env_value "$ENV_FILE" COMPOSE_PROJECT_NAME)
    printf '%s_synapse-data' "${proj:-razzfazz-stack}"
}

matrix_helper_image() {   # an image the box already runs (the postgres one), never a pulled alpine
    docker inspect --type container --format '{{.Config.Image}}' postgres 2>/dev/null
}

matrix_backup_volume() {  # <backup password>: encrypted tar of the synapse-data volume
    local pw=$1 vol img out
    vol=$(matrix_volume_name)
    docker volume inspect "$vol" >/dev/null 2>&1 || { print_info "  no ${vol} volume - nothing to back up there."; return 0; }
    img=$(matrix_helper_image)
    [ -n "$img" ] || { print_error "the postgres container (whose image does the volume work) is not there - nothing changed."; return 1; }
    out="${STACK_DIR}/backups/module-reset/synapse-data-$(date +%Y%m%d-%H%M%S).tar.enc"
    if ! ( umask 077
           docker run --rm --user 0:0 --entrypoint tar -v "${vol}:/data:ro" "$img" -C /data -cf - . \
             | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 -pass fd:3 -out "$out" 3<<<"$pw" ); then
        rm -f "$out"
        print_error "backup of ${vol} failed - nothing changed."; return 1
    fi
    print_substep "synapse-data backup (encrypted, media store and signing key): $out"
}

matrix_finish_reset() {
    local vol img left name
    vol=$(matrix_volume_name)
    if docker volume inspect "$vol" >/dev/null 2>&1; then
        img=$(matrix_helper_image)
        docker run --rm --user 0:0 --entrypoint sh -v "${vol}:/data" "$img" -c 'find /data -mindepth 1 -delete' >/dev/null 2>&1 || true
        # The effect, not the exit code: read the volume back.
        left=$(docker run --rm --user 0:0 --entrypoint ls -v "${vol}:/data:ro" "$img" -A /data 2>/dev/null || echo unreadable)
        if [ -n "$(printf '%s' "$left" | tr -d '[:space:]')" ]; then
            print_error "the database was reset but ${vol} still holds files. Empty it by hand before enabling matrix; the backup is in backups/module-reset/."
            return 1
        fi
        print_substep "${vol} emptied"
    fi
    # Re-pin to the CURRENT domain: the fresh database starts under it.
    name=$(read_env_value "$ENV_FILE" MAIN_DOMAIN)
    update_env_value "$ENV_FILE" MATRIX_SERVER_NAME "$name"
    print_substep "MATRIX_SERVER_NAME pinned to ${name}"
    # The rendered config still names the old server name and the Portal's enable
    # does not render: render now, or drop the stale file so it asks for 'prepare'.
    if ! matrix_render_config "$STACK_DIR"; then
        rm -f "${STACK_DIR}/modules/apps/matrix/synapse/homeserver.yaml.rendered"
        print_warning "the Synapse config could not be rendered; run: rzfz module prepare matrix"
    fi
    print_info "Matrix is empty and ready under the server name ${name}. Enable it: rzfz module enable matrix (or the Configuration Portal)."
}

cmd_reset_data() {
    local profile="" ack=false a db owner pw dump
    for a in "$@"; do
        case "$a" in
            --i-understand-data-loss) ack=true ;;
            -*) print_error "unknown option: $a"; return 2 ;;
            *)  if [ -z "$profile" ]; then profile=$a; else print_error "one profile at a time"; return 2; fi ;;
        esac
    done
    if [ -z "$profile" ]; then
        print_error "usage: rzfz module reset-data <profile> --i-understand-data-loss"; return 2
    fi
    if [ "$profile" = matrix ]; then
        # #2981: Matrix is not sealed with a key; what ties its data to the box is the
        # server name (the suffix of every user ID). Its reset also empties the
        # Synapse data volume (media, signing key) and re-pins the name.
        db=$(read_env_value "$ENV_FILE" SYNAPSE_DB); db=${db:-synapse_db}
    else
        if ! db=$(data_bound database --profile "$profile"); then
            print_error "cannot read the data-bound secret declaration for '$profile' — nothing changed."; return 2
        fi
        if [ -z "$db" ]; then
            print_error "'$profile' declares no data_bound_secrets in core/config/profiles.yaml — nothing to reset."; return 2
        fi
    fi
    case ",$(read_env_value "$ENV_FILE" COMPOSE_PROFILES)," in
        *",$profile,"*) print_error "'$profile' is enabled — disable it first (Configuration Portal), then reset its data."; return 2 ;;
    esac
    if [ "$ack" != true ]; then
        if [ "$profile" = matrix ]; then
            print_error "REFUSED: this DELETES all Matrix data - every user, room and message in database '$db' and the media and signing key in the synapse-data volume. Backups are taken first. Re-run with --i-understand-data-loss."; return 2
        fi
        print_error "REFUSED: this DELETES everything in database '$db'. Re-run with --i-understand-data-loss."; return 2
    fi
    # Running containers: the profile's own services, and — for an always-on
    # profile that is not in COMPOSE_PROFILES — anything connected to the database.
    local dc running conns own with_p without_p enc coll ctype
    if ! dc=$(docker_compose_cmd); then print_error "docker compose is not available — nothing changed."; return 1; fi
    # The profile's OWN services: those that exist with the profile on and not
    # without it. Core services (postgres, ...) have no profile and are always
    # running; they must not block the reset.
    if ! with_p=$($dc --profile "$profile" config --services) || ! without_p=$($dc config --services); then
        print_error "cannot resolve the services of '$profile' — nothing changed."; return 1
    fi
    own=$(LC_ALL=C comm -23 <(printf '%s\n' "$with_p" | LC_ALL=C sort -u) <(printf '%s\n' "$without_p" | LC_ALL=C sort -u) | grep -v '^$' || true)
    running=""
    if [ -n "$own" ]; then
        # shellcheck disable=SC2046  # one word per service name
        if ! running=$($dc --profile "$profile" ps --status running -q $(printf '%s' "$own" | tr '\n' ' ')); then
            print_error "cannot list the containers of '$profile' — nothing changed."; return 1
        fi
    fi
    if [ -n "$(printf '%s' "$running" | tr -d '[:space:]')" ]; then
        print_error "containers of '$profile' are still running — stop them first (rzfz down / disable the module); nothing changed."; return 2
    fi
    if ! conns=$(psql_c postgres "SELECT count(*) FROM pg_stat_activity WHERE datname = '$db' AND pid <> pg_backend_pid()"); then
        print_error "postgres did not answer — nothing changed."; return 1
    fi
    conns=$(printf '%s' "$conns" | tr -d '[:space:]')
    if [ "${conns:-0}" != 0 ]; then
        print_error "${conns} connection(s) to '$db' are open (a service still uses it) — nothing changed."; return 2
    fi
    if ! owner=$(psql_c postgres "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '$db'"); then
        print_error "postgres did not answer — nothing changed."; return 1
    fi
    owner=$(printf '%s' "$owner" | tr -d '[:space:]')
    if [ -z "$owner" ]; then print_info "database '$db' does not exist — nothing to reset."; return 0; fi
    if ! [[ "$owner" =~ ^[A-Za-z0-9_]+$ ]]; then print_error "unexpected owner '$owner' — nothing changed."; return 1; fi
    # Encoding and locale, read before the drop so the new database matches.
    if ! enc=$(psql_c postgres "SELECT pg_encoding_to_char(encoding) || '|' || datcollate || '|' || datctype FROM pg_database WHERE datname = '$db'"); then
        print_error "postgres did not answer — nothing changed."; return 1
    fi
    enc=$(printf '%s' "$enc" | tr -d '[:space:]')
    IFS='|' read -r enc coll ctype <<<"$enc"
    if ! [[ "$enc|$coll|$ctype" =~ ^[A-Za-z0-9_]+\|[A-Za-z0-9_.@-]+\|[A-Za-z0-9_.@-]+$ ]]; then
        print_error "unexpected encoding/locale '$enc|$coll|$ctype' — nothing changed."; return 1
    fi
    pw=$(read_env_value "$ENV_FILE" BACKUP_ENCRYPTION_PASSWORD)
    if [ -z "$pw" ]; then
        print_error "BACKUP_ENCRYPTION_PASSWORD is empty — no encrypted safety dump possible; nothing changed."; return 1
    fi
    ( umask 077; mkdir -p "${STACK_DIR}/backups/module-reset" )
    chmod 700 "${STACK_DIR}/backups/module-reset"
    dump="${STACK_DIR}/backups/module-reset/${db}-$(date +%Y%m%d-%H%M%S).dump.enc"
    print_warning "about to DELETE database '$db' (owner $owner) with everything in it, and recreate it empty."
    print_info    "  first: encrypted safety dump -> $dump"
    if ! ( umask 077
           docker exec -e PGPASSWORD postgres pg_dump -U "$PG_USER" -Fc "$db" \
             | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 -pass fd:3 -out "$dump" 3<<<"$pw" ); then
        rm -f "$dump"
        print_error "safety dump of '$db' failed — nothing changed."; return 1
    fi
    print_substep "safety dump (encrypted with BACKUP_ENCRYPTION_PASSWORD): $dump"
    if [ "$profile" = matrix ]; then
        matrix_backup_volume "$pw" || { rm -f "$dump"; return 1; }
    else
        print_info "  Inside it the data is still sealed with the LOST module key; keep it until you are sure."
    fi
    if ! psql_c postgres "DROP DATABASE \"$db\" WITH (FORCE)" >/dev/null; then
        print_error "DROP DATABASE '$db' failed — check whether it still exists. Safety dump: $dump"; return 1
    fi
    if ! psql_c postgres "CREATE DATABASE \"$db\" OWNER \"$owner\" TEMPLATE template0 ENCODING '$enc' LC_COLLATE '$coll' LC_CTYPE '$ctype'" >/dev/null; then
        print_error "DATABASE '$db' WAS DROPPED BUT COULD NOT BE RECREATED. Its data is only in the encrypted dump:"
        print_error "  $dump"
        print_error "Restore it (BACKUP_ENCRYPTION_PASSWORD and PGPASSWORD exported from .env):"
        print_error "  openssl enc -d -aes-256-cbc -salt -pbkdf2 -iter 100000 -pass env:BACKUP_ENCRYPTION_PASSWORD -in '$dump' | docker exec -i -e PGPASSWORD postgres pg_restore -U '$PG_USER' -d postgres --create"
        return 3
    fi
    print_success "database '$db' reset (empty, owner $owner)."
    if [ "$profile" = matrix ]; then
        matrix_finish_reset || return 1
        return 0
    fi
    print_info    "Now enable '$profile' (Configuration Portal): it generates fresh keys over the empty database."
}

matrix_fill_secrets() {
    # #2470: fill-if-empty from the Portal provisioner's own table and
    # generators (PROFILE_SECRETS['matrix'], _generate): one list for the
    # Portal and the host. Written with update_env_value (inode kept, #1224).
    local plan kv
    plan="$(umask 077; mktemp "${TMPDIR:-/tmp}/rzfz-matrix.XXXXXX")"
    python3 - "${SCRIPT_DIR}/core/config/app/services/profile_provisioner.py" "$ENV_FILE" > "$plan" <<'PY'
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("profile_provisioner", sys.argv[1])
pp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp)
env = {}
for line in open(sys.argv[2], encoding="utf-8"):
    k, sep, v = line.rstrip("\n").partition("=")
    if sep and k not in env:
        env[k] = v.strip().strip('"').strip("'")
for key, kind in pp.PROFILE_SECRETS["matrix"]:
    if not env.get(key):
        sys.stdout.buffer.write(f"{key}={pp._generate(kind)}".encode() + b"\0")
PY
    while IFS= read -r -d '' kv; do
        update_env_value "$ENV_FILE" "${kv%%=*}" "${kv#*=}"
        print_substep "generated ${kv%%=*}"
    done < "$plan"
    rm -f "$plan"
}

# #2955: Caddy's entrypoint keeps a module's site names inert unless the module's
# profile is in the COMPOSE_PROFILES of the caddy CONTAINER, which is fixed when
# the container is created. After a profile change, recreate caddy iff the
# entrypoint gates names on that profile. The gate list is read from the
# entrypoint's `for _gate in KEY:PROFILE ...` line, never kept as a second list.
caddy_follow_profile() {  # <profile>
    local profile=$1 gates
    gates=$(grep -E '^for _gate in ' "${SCRIPT_DIR}/core/Caddy/entrypoint.sh" 2>/dev/null | tr ';' ' ' || true)
    case " ${gates} " in
        *:"${profile}"\ *) ;;
        *) return 0 ;;
    esac
    print_substep "recreating caddy so the '${profile}' site names go live"
    docker compose up -d --no-deps --force-recreate caddy || {
        print_error "caddy could not be recreated; the '${profile}' names stay unreachable until: docker compose up -d --no-deps --force-recreate caddy"
        return 1
    }
}

cmd_matrix() {  # <prepare|enable>
    local verb=$1 profiles state="missing" i refusal
    local tries="${RZFZ_MODULE_HEALTH_TRIES:-30}" pause="${RZFZ_MODULE_HEALTH_SLEEP:-10}"
    # F8/F9 guard: never mint a data-bound key over a database that holds data.
    # Fails closed: an unreadable declaration or database refuses.
    if ! refusal=$(data_bound check --profile matrix --env "$ENV_FILE" 2>&1); then
        print_error "${refusal:-the data-bound check failed} — nothing changed."
        return 1
    fi
    matrix_fill_secrets
    matrix_render_config "$STACK_DIR" || return 1
    if [ "$verb" = prepare ]; then
        print_info "matrix prepared. Enable it in the Configuration Portal, or run: rzfz module enable matrix"
        return 0
    fi
    profiles=$(read_env_value "$ENV_FILE" COMPOSE_PROFILES)
    case ",$profiles," in
        *,matrix,*) ;;
        *) update_env_value "$ENV_FILE" COMPOSE_PROFILES "${profiles:+$profiles,}matrix"
           print_substep "COMPOSE_PROFILES += matrix" ;;
    esac
    docker compose up -d --no-deps --force-recreate postgres-db-reconcile
    docker compose up -d synapse element-web
    caddy_follow_profile matrix || return 1   # #2955
    # #2681: the same post-start steps the Portal's enable runs; a failed one
    # fails the enable (a root-owned synapse-data crash-loops Synapse).
    matrix_post_start "$STACK_DIR" --strict || return 1
    # 18-synapse-oidc renders MATRIX_CLIENT_ID / SYNAPSE_CLIENT_SECRET from .env.
    docker compose up -d --no-deps --force-recreate authentik-init
    # #2681: after authentik-init, every matrix app carries a policy binding —
    # one with none is open to every signed-in user; it is closed to Super
    # Admins and the enable fails. The Portal runs the same check.
    matrix_export_host_path "$STACK_DIR"   # #2859: any hook that builds a compose argv needs it
    python3 "${SCRIPT_DIR}/core/config/app/services/module_hooks.py" bindings matrix --stack "$STACK_DIR" || return 1
    for i in $(seq 1 "$tries"); do
        state=$(docker inspect --format '{{.State.Health.Status}}' synapse 2>/dev/null || echo missing)
        if [ "$state" = healthy ]; then break; fi
        sleep "$pause"
    done
    if [ "$state" != healthy ]; then
        print_error "synapse is '$state' after $((tries * pause))s — see: docker logs synapse"
        return 1
    fi
    print_success "matrix enabled: Synapse healthy, the Synapse OIDC blueprint re-applied."
}

case "${1:-}" in
    reset-data) shift; cmd_reset_data "$@" ;;
    prepare|enable)
        if [ "${2:-}" != matrix ]; then
            print_error "rzfz module $1 supports 'matrix' only; enable other modules in the Configuration Portal."
            exit 2
        fi
        cmd_matrix "$1" ;;
    *) print_error "usage: rzfz module {reset-data <profile> --i-understand-data-loss | prepare matrix | enable matrix}   (rzfz module --help)"; exit 2 ;;
esac
