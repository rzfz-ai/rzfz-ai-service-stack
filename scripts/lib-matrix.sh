# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# scripts/lib-matrix.sh — host-side matrix (Synapse + Element Web) setup,
# shared by `rzfz init` (Step 5b/7b) and `rzfz module prepare|enable matrix`
# (#2470). SOURCED: no top-level `set`. Needs scripts/lib.sh (print_*,
# read_env_value). Every value comes from <stack_dir>/.env.
# ==============================================================================

MATRIX_RENDER_REQUIRED="MAIN_DOMAIN POSTGRES_PASSWORD MATRIX_CLIENT_ID SYNAPSE_CLIENT_SECRET SYNAPSE_MACAROON_SECRET_KEY SYNAPSE_FORM_SECRET SYNAPSE_REGISTRATION_SHARED_SECRET"

# --- The pinned server name (#2981) -----------------------------------------
# A Synapse server_name is the suffix of every user ID (@user:<name>) and is
# PERMANENT: Synapse refuses to start when the users in its database are not
# native to the configured name. The name therefore lives in .env as
# MATRIX_SERVER_NAME, written ONCE, and the render never derives it from
# MAIN_DOMAIN again. A re-domain moves the public hostnames (MATRIX_DOMAIN,
# ELEMENT_WEB_DOMAIN, public_baseurl) and leaves the name where it is.

_matrix_valid_server_name() {   # <name>: host[:port], nothing else
    [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]]
}

# matrix_detect_server_name <stack_dir>
#   Prints the server_name this box's Synapse data was created with, or
#   nothing when there is no Synapse data. Sources, in order of authority:
#   the users in synapse_db (what Synapse itself checks at start), the
#   <name>.signing.key in the synapse-data volume, and last the server_name in
#   the existing homeserver.yaml.rendered (which a render after a domain
#   change has already overwritten - hence last).
matrix_detect_server_name() {
    local stack=$1 env name="" db user proj vol img f pw
    env="$stack/.env"
    if command -v docker >/dev/null 2>&1; then
        db=$(read_env_value "$env" SYNAPSE_DB); db=${db:-synapse_db}
        user=$(read_env_value "$env" POSTGRES_USER); user=${user:-docker}
        pw=$(read_env_value "$env" POSTGRES_PASSWORD)
        name=$(PGPASSWORD="$pw" docker exec -e PGPASSWORD postgres psql -X -U "$user" -d "$db" -tAc \
               "SELECT substr(name, strpos(name, ':') + 1) FROM users ORDER BY creation_ts LIMIT 1" \
               2>/dev/null | head -n1 | tr -d '[:space:]') || name=""
        if ! _matrix_valid_server_name "$name"; then
            name=""
            proj=$(read_env_value "$env" COMPOSE_PROJECT_NAME); vol="${proj:-razzfazz-stack}_synapse-data"
            if docker volume inspect "$vol" >/dev/null 2>&1 \
               && img=$(docker inspect --type container --format '{{.Config.Image}}' postgres 2>/dev/null) \
               && [ -n "$img" ]; then
                f=$(docker run --rm --tmpfs /var/lib/postgresql/data --user 0:0 --entrypoint ls -v "$vol:/data:ro" "$img" /data 2>/dev/null \
                    | grep -m1 '\.signing\.key$' || true)
                name=${f%.signing.key}
            fi
        fi
    fi
    if ! _matrix_valid_server_name "$name"; then
        name=""
        f="$stack/modules/apps/matrix/synapse/homeserver.yaml.rendered"
        [ -f "$f" ] && name=$(sed -n 's/^server_name:[[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}[[:space:]]*$/\1/p' "$f" | head -n1)
    fi
    if _matrix_valid_server_name "$name"; then printf '%s\n' "$name"; fi
    return 0
}

# matrix_pin_server_name <stack_dir> [<fallback>]
#   Makes sure .env holds MATRIX_SERVER_NAME and prints it. An existing value
#   is never touched. Otherwise: the name Synapse data already carries
#   (matrix_detect_server_name), else <fallback> (the render passes MAIN_DOMAIN:
#   the first enable of a box with no Synapse data), else nothing is written
#   (returns 1). Written in place (update_env_value keeps the inode).
matrix_pin_server_name() {
    local stack=$1 fallback=${2:-} env name how
    env="$stack/.env"
    name=$(read_env_value "$env" MATRIX_SERVER_NAME)
    if [ -n "$name" ]; then printf '%s\n' "$name"; return 0; fi
    name=$(matrix_detect_server_name "$stack"); how="the existing Synapse data"
    if [ -z "$name" ] && [ -n "$fallback" ]; then name=$fallback; how="the domain at first enable"; fi
    [ -n "$name" ] || return 1
    if ! grep -qE '^[[:space:]]*(export[[:space:]]+)?MATRIX_SERVER_NAME=' "$env"; then
        printf '\nMATRIX_SERVER_NAME=\n' >> "$env"
    fi
    update_env_value "$env" MATRIX_SERVER_NAME "$name"
    print_substep "matrix: server name pinned to ${name} (from ${how}); it stays when the domain changes" >&2
    printf '%s\n' "$name"
}

# matrix_render_config <stack_dir>
#   Renders synapse/homeserver.yaml.rendered (0644 — Synapse runs as uid 991)
#   and element-web/config.json, and seeds the Synapse CA trust store from the
#   host bundle. Returns 1 and writes nothing when a value the homeserver
#   embeds is empty: an empty client id/secret leaves the 18-synapse-oidc
#   blueprint in error (#2470).
matrix_render_config() {
    local stack=$1 env dir k missing=""
    env="$stack/.env"
    dir="$stack/modules/apps/matrix"
    # A box without the matrix module files has nothing to render: skip, as
    # init always did. Only an empty secret refuses.
    if [ ! -f "$dir/synapse/homeserver.yaml" ]; then
        print_warning "matrix: $dir/synapse/homeserver.yaml is missing — skipping the homeserver render"
        return 0
    fi
    for k in $MATRIX_RENDER_REQUIRED; do
        [ -n "$(read_env_value "$env" "$k")" ] || missing="$missing $k"
    done
    if [ -n "$missing" ]; then
        print_error "matrix: empty in .env:${missing} — homeserver.yaml NOT rendered. Fix: run 'rzfz setup' or set them in ${env}, then re-run 'rzfz init'"
        return 1
    fi
    (
        main=$(read_env_value "$env" MAIN_DOMAIN)
        # #2981: the server name is pinned in .env (first enable: MAIN_DOMAIN;
        # a box with Synapse data: the name that data carries), never derived
        # from MAIN_DOMAIN once data exists.
        sname=$(matrix_pin_server_name "$stack" "$main") || exit 1
        export SYNAPSE_SERVER_NAME="$sname" MATRIX_SERVER_NAME="$sname" MAIN_DOMAIN="$main"
        v=$(read_env_value "$env" POSTGRES_USER);    export POSTGRES_USER="${v:-docker}"
        v=$(read_env_value "$env" SYNAPSE_DB);       export SYNAPSE_DB="${v:-synapse_db}"
        v=$(read_env_value "$env" MATRIX_DOMAIN);    export MATRIX_DOMAIN="${v:-matrix.${main}}"
        v=$(read_env_value "$env" AUTHENTIK_DOMAIN); export AUTHENTIK_DOMAIN="${v:-auth.${main}}"
        # #2001: Synapse's mail sender. Optional — a default, never a refusal.
        # .env ships it as `razzfazz-ai-box-1@${MAIN_DOMAIN}` and envsubst does
        # not recurse, so the domain reference is resolved here; a literal
        # `${MAIN_DOMAIN}` sender would be refused by the relay.
        v=$(read_env_value "$env" SMTP_FROM)
        v=${v//'${MAIN_DOMAIN}'/$main}; v=${v//'$MAIN_DOMAIN'/$main}
        case "$v" in
            *'$'*) print_warning "matrix: SMTP_FROM '$v' holds a reference that cannot be resolved here - Synapse sends from noreply@${main} instead"
                   v="" ;;
        esac
        export SMTP_FROM="${v:-noreply@${main}}"
        for k in POSTGRES_PASSWORD MATRIX_CLIENT_ID SYNAPSE_CLIENT_SECRET SYNAPSE_MACAROON_SECRET_KEY \
                 SYNAPSE_FORM_SECRET SYNAPSE_REGISTRATION_SHARED_SECRET; do
            export "$k=$(read_env_value "$env" "$k")"
        done
        umask 022
        envsubst < "$dir/synapse/homeserver.yaml" > "$dir/synapse/homeserver.yaml.rendered.tmp" \
            && mv "$dir/synapse/homeserver.yaml.rendered.tmp" "$dir/synapse/homeserver.yaml.rendered" \
            && chmod 0644 "$dir/synapse/homeserver.yaml.rendered" || exit 1
        if [ -f "$dir/element-web/config.json.tpl" ]; then
            envsubst '${MATRIX_SERVER_NAME} ${MATRIX_DOMAIN}' < "$dir/element-web/config.json.tpl" \
                > "$dir/element-web/config.json" || exit 1
        fi
        if [ -r /etc/ssl/certs/ca-certificates.crt ]; then
            cp /etc/ssl/certs/ca-certificates.crt "$dir/synapse/caddy-local-ca.crt" || exit 1
        else
            : > "$dir/synapse/caddy-local-ca.crt"
        fi
        # #2681: on TLS_MODE=internal the Caddy root goes in at render time
        # when Caddy runs (rzfz module prepare matrix on an installed box):
        # the Portal's enable cannot write this file (read-only repo mount)
        # and fails when the root is missing. Init's render runs before Caddy
        # does; its post-start step appends the root then.
        if [ "$(read_env_value "$env" TLS_MODE)" = internal ] \
           && ca_root=$(docker exec caddy cat /data/caddy/pki/authorities/local/root.crt 2>/dev/null) \
           && [ -n "$ca_root" ]; then
            printf '%s\n' "$ca_root" >> "$dir/synapse/caddy-local-ca.crt" || exit 1
        fi
    ) || { print_error "matrix: rendering failed (see above)"; return 1; }
    print_success "homeserver.yaml rendered for server_name=$(read_env_value "$env" MATRIX_SERVER_NAME)"
}

# matrix_refresh_config <stack_dir> [--no-recreate]
#   (#2578) Re-render homeserver.yaml.rendered from the template of the tree
#   the box runs NOW, for a box whose matrix profile is active. Only init and
#   `rzfz module prepare|enable matrix` rendered before, so no change to the
#   template (the OIDC mapping module that refuses case-variant logins) ever
#   reached a box that already ran Matrix. When the rendered file changed and
#   a synapse container exists, synapse is recreated (the config is a
#   single-file bind mount: a restart keeps the old inode). --no-recreate: the
#   caller recreates every container itself right after (rzfz upgrade).
#   Returns 0 when nothing was needed or the refresh worked, 1 when the render
#   was refused; callers WARN, they do not abort.
matrix_refresh_config() {
    local stack=$1 mode=${2:-} profiles old="" new="" rendered
    profiles=${COMPOSE_PROFILES:-}
    [ -n "$profiles" ] || profiles=$(read_env_value "$stack/.env" COMPOSE_PROFILES 2>/dev/null || true)
    # #2981: a box that ran Matrix before the pin existed gets its name from the
    # data, whether or not the profile is on now - the next enable re-renders.
    if [ -z "$(read_env_value "$stack/.env" MATRIX_SERVER_NAME)" ]; then
        matrix_pin_server_name "$stack" >/dev/null || true
    fi
    case ",${profiles// /}," in *,matrix,*) ;; *) return 0 ;; esac
    rendered="$stack/modules/apps/matrix/synapse/homeserver.yaml.rendered"
    [ -f "$rendered" ] && old=$(cksum < "$rendered")
    matrix_render_config "$stack" || return 1
    [ -f "$rendered" ] && new=$(cksum < "$rendered")
    [ "$old" != "$new" ] || return 0
    print_substep "matrix: homeserver.yaml.rendered changed - Synapse must be recreated to read it."
    [ "$mode" = "--no-recreate" ] && return 0
    docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx synapse || return 0
    ( cd "$stack" && docker compose up -d --no-deps --force-recreate synapse ) \
        || { print_warning "matrix: synapse could not be recreated - run: docker compose up -d --no-deps --force-recreate synapse"; return 1; }
}

# matrix_export_host_path <stack_dir>
#   #2859: module_hooks.py runs on the HOST here and its compose_argv refuses
#   to run without STACK_HOST_PATH (#2831). The host shell never has it; it
#   lives in the stack's .env. Export it from there, else from the stack
#   directory's real path (init records the same value). An existing value in
#   .env wins over whatever the shell holds.
matrix_export_host_path() {
    local stack=$1 v
    v="$(read_env_value "$stack/.env" STACK_HOST_PATH 2>/dev/null || true)"
    [ -n "$v" ] || v="$(cd "$stack" && pwd -P)"
    export STACK_HOST_PATH="$v"
}

# matrix_post_start <stack_dir> [--strict]
#   synapse-data owned by 991:991 (Synapse writes its signing key there), the
#   Caddy internal root in Synapse's trust store for TLS_MODE=internal,
#   Synapse restarted when it runs. #2681: the steps live in ONE place,
#   core/config/app/services/module_hooks.py (POST_START_HOOKS), which the
#   Configuration Portal's enable runs too. --strict (rzfz module enable):
#   a failed step returns 1. Without it (rzfz init): a failed step WARNs.
matrix_post_start() {
    local stack=$1 strict=${2:-} hooks
    hooks="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/core/config/app/services/module_hooks.py"
    matrix_export_host_path "$stack"
    if python3 "$hooks" post-start matrix --stack "$stack"; then
        return 0
    fi
    if [ "$strict" = --strict ]; then
        return 1
    fi
    print_warning "matrix post-start steps failed (see above) — Synapse may not start. Fix: rzfz module enable matrix"
    return 0
}
