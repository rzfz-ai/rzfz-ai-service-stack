#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# Finish a re-domain after .env / .env.dify name the new domain:
#   scripts/redomain-reconcile.sh [--db-only] <old-domain> <new-domain>
#
# Full mode (`rzfz setup --set-domain`):
#   1. authentik-init re-templates the blueprints for the new domain.
#   2. The stores the env cannot reach are rewritten, each in ONE transaction:
#      Authentik (proxy providers, outpost, OAuth2 redirect URIs, launch URLs)
#      and the Open WebUI config row(s), the LLM Manager workers and cognee's
#      user e-mails, Dify's account e-mails and Gitea's user e-mails
#      (scripts/sql/redomain-*.sql); Authentik's user e-mails move with its rows.
#   3. Authentik's cache is cleared; the domain-baking services that were
#      running are recreated (only those, in this compose project) and must be
#      running again afterwards.
#   4. Verified by measurement: no stack key in .env / .env.dify, no Authentik
#      or Open WebUI row, and no host in Caddy's LIVE config (its admin API)
#      still names the old domain.
# --db-only (the Config Portal, which rewrote the env files, re-templates and
# recreates itself): step 2 and the database part of step 4 only.
#
# Order: the env files are written FIRST (by the caller), the databases after.
# The env is what authentik-init templates from and what every recreated
# service reads; the database rows are derived state this script can always
# recompute from <old> <new>. A failure here therefore never leaves anything
# that a re-run of this script (or `rzfz setup --set-domain <new>`, which
# remembers the pending old domain) cannot finish. Each database step is one
# transaction, so a failure inside it changes nothing in that database.
#
# Exit: 0 done and verified · 1 something still names the old domain or a
# step failed (each named, with the next step) · 2 usage · 3 postgres is not
# running (nothing changed) · 4 Caddy's live config could not be read, so
# what the box serves is not verified.
set -euo pipefail

cd "$(dirname "$0")/.."

usage() { echo "usage: scripts/redomain-reconcile.sh [--db-only] <old-domain> <new-domain>" >&2; exit 2; }

DB_ONLY=false
if [ "${1:-}" = "--db-only" ]; then DB_ONLY=true; shift; fi
[ "$#" -eq 2 ] || usage
OLD="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
NEW="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
[ -n "$OLD" ] && [ -n "$NEW" ] && [ "$OLD" != "$NEW" ] || usage
case "$OLD$NEW" in *[!a-z0-9.-]*) echo "not a domain name: $1 / $2" >&2; exit 2 ;; esac
OLD_RE="$(printf '%s' "$OLD" | sed 's/[.-]/\\&/g')"
WAIT_S="${RAZZFAZZ_REDOMAIN_WAIT_S:-300}"
if [ "$DB_ONLY" = true ]; then
    RERUN="scripts/redomain-reconcile.sh --db-only ${OLD} ${NEW}"
else
    RERUN="scripts/redomain-reconcile.sh ${OLD} ${NEW}"
fi

# Own reader: scripts/lib.sh is written for `set -eo` callers, not `-u`.
_env() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- | tr -d "\"'" || true; }
PG_USER="$(_env POSTGRES_USER)"; PG_USER="${PG_USER:-docker}"
AK_DB="$(_env AUTHENTIK_DB)"; AK_DB="${AK_DB:-authentik_db}"
OW_DB="$(_env OPENWEBUI_DB)"; OW_DB="${OW_DB:-openwebui_db}"
# Passed to `docker exec -e PGPASSWORD` by NAME, never on a command line.
PGPASSWORD="$(_env POSTGRES_PASSWORD)"; export PGPASSWORD
LM_DB="$(_env LLM_MANAGER_DB)"; LM_DB="${LM_DB:-llm_manager_db}"
CG_DB="$(_env COGNEE_DB)"; CG_DB="${CG_DB:-cognee_db}"
DF_DB="$(_env DIFY_DB)"; DF_DB="${DF_DB:-dify_db}"
GT_DB="$(_env GITEA_DB)"; GT_DB="${GT_DB:-gitea_db}"

# The services whose container bakes the domain in at start. Keep equal to
# ApplyManager._DOMAIN_BAKING_SERVICES (core/config/app/services/apply_manager.py).
DOMAIN_BAKING_SERVICES="caddy authentik-server authentik-worker openwebui pipelines dify-api dify-web dify-worker dify-worker-dataset dify-plugin-daemon gitea razzfazz-start-portal openlit synapse element-web"

# #2719: which running containers still carry a domain the box no longer has.
# shellcheck source=scripts/lib/domain-env-drift.sh
. scripts/lib/domain-env-drift.sh

_nl=$'\n'
_in_list() { case "${_nl}$2${_nl}" in *"${_nl}$1${_nl}"*) return 0 ;; esac; return 1; }

running_containers="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
if ! _in_list postgres "$running_containers"; then
    echo "postgres is not running: nothing was reconciled."
    echo "Start the stack (docker compose up -d), then run: ${RERUN}"
    exit 3
fi

# #2719: authentik-init re-templates every blueprint and waits for the blueprint
# controller to settle: it overran a flat 300 s on a box with 57 containers.
# The budget scales with the running containers (600 s + 10 s each); an explicit
# RAZZFAZZ_REDOMAIN_INIT_WAIT_S or RAZZFAZZ_REDOMAIN_WAIT_S wins.
_nrun="$(printf '%s\n' "$running_containers" | grep -c . || true)"
if [ -n "${RAZZFAZZ_REDOMAIN_INIT_WAIT_S:-}" ]; then
    INIT_WAIT_S="$RAZZFAZZ_REDOMAIN_INIT_WAIT_S"; INIT_NOTE=""
elif [ -n "${RAZZFAZZ_REDOMAIN_WAIT_S:-}" ]; then
    INIT_WAIT_S="$RAZZFAZZ_REDOMAIN_WAIT_S"; INIT_NOTE=""
else
    INIT_WAIT_S=$((600 + 10 * _nrun))
    INIT_NOTE=" (600 s + 10 s per running container: ${_nrun})"
fi

rc=0
fail() { echo "✗ $*"; rc=1; }

# The compose project these containers belong to (its working_dir label as the
# daemon sees it; inside the Config Portal container the path differs from
# `pwd`). Only this project's containers are measured and recreated (#2719).
proj_label="$(timeout 20 docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' postgres 2>/dev/null || true)"
[ -n "$proj_label" ] || proj_label="$(pwd -P)"
# Sets STALE (the stale lines, "service container key") and STALE_RC
# (0 measured · 2 not measured · 3 measured only partly; see the library).
_stale_env() {
    STALE_RC=0
    STALE="$(razzfazz_domain_env_drift .env "$OLD" "$proj_label" "$NEW")" || STALE_RC=$?
}

if [ "$DB_ONLY" = false ]; then
    # Compose reads COMPOSE_FILE / COMPOSE_PROFILES from ./.env, so it acts on
    # this project only if the running stack IS this project.
    proj="$(timeout 20 docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' postgres 2>/dev/null || true)"
    if [ -n "$proj" ] && [ "$(cd "$proj" 2>/dev/null && pwd -P || echo "$proj")" != "$(pwd -P)" ]; then
        echo "✗ The running stack belongs to the compose project in ${proj}, not $(pwd -P): nothing was changed."
        echo "  Run from there: cd ${proj} && ${RERUN}"
        exit 1
    fi
    if ! running_services="$(docker compose ps --services --status running 2>/dev/null)"; then
        echo "✗ docker compose could not list the running services: nothing was changed. Fix compose, then run: ${RERUN}"
        exit 1
    fi

    echo "→ Re-templating the Authentik blueprints for ${NEW} (authentik-init, up to ${INIT_WAIT_S}s${INIT_NOTE})..."
    if docker compose up -d --no-deps --force-recreate authentik-init >/dev/null 2>&1; then
        _w=0
        while :; do
            st="$(docker inspect -f '{{.State.Status}}|{{.State.ExitCode}}' authentik-init 2>/dev/null || true)"
            case "$st" in
                "exited|0") break ;;
                exited\|*) fail "authentik-init exited ${st#exited|} (docker logs authentik-init). Then run: ${RERUN}"; break ;;
            esac
            if [ "$_w" -ge "$INIT_WAIT_S" ]; then
                fail "authentik-init did not finish within ${INIT_WAIT_S}s${INIT_NOTE} (docker logs authentik-init). Then run: ${RERUN}"; break
            fi
            sleep 5; _w=$((_w + 5))
        done
    else
        fail "could not start authentik-init (docker compose up -d --no-deps --force-recreate authentik-init). Then run: ${RERUN}"
    fi
fi

# Printed (never run) when cognee holds an account on the old domain AND
# one on the new: the old one may own the cognee-mcp API key, the new one the
# data. Merging two accounts is a choice for whoever runs the box, so this is a recipe.
_cognee_merge_recipe() {
    cat <<RECIPE
    Both an old-domain and a new-domain cognee admin exist. If the old one owns
    nothing but its API key, merge it by hand (database ${CG_DB}):
      1. Back up first:
           docker exec postgres pg_dump -U ${PG_USER} -Fc ${CG_DB} > cognee-before-merge.dump
      2. Find the two ids (the old user is the one on ${OLD}):
           SELECT id, email, created_at FROM users ORDER BY created_at;
      3. Check that the old user owns nothing except user_api_key (every count must be 0):
           SELECT (SELECT count(*) FROM datasets WHERE owner_id = '<old-id>') AS datasets,
                  (SELECT count(*) FROM dataset_database WHERE owner_id = '<old-id>') AS dataset_database,
                  (SELECT count(*) FROM data WHERE owner_id = '<old-id>') AS data,
                  (SELECT count(*) FROM acls WHERE principal_id = '<old-id>') AS acls,
                  (SELECT count(*) FROM tenants WHERE owner_id = '<old-id>') AS tenants,
                  (SELECT count(*) FROM user_roles WHERE user_id = '<old-id>') AS roles,
                  (SELECT count(*) FROM principal_configuration WHERE owner_id = '<old-id>') AS config;
         If any count is not 0 you must stop: the two accounts hold data (deleting the
         old user would cascade-delete dataset_database rows) and need a decision.
      4. Move the key and the tenant memberships, then remove the old user, in ONE
         transaction (user_tenants.user_id blocks the delete while rows remain):
           BEGIN;
           UPDATE user_api_key SET user_id = '<new-id>' WHERE user_id = '<old-id>';
           UPDATE user_tenants SET user_id = '<new-id>' WHERE user_id = '<old-id>'
             AND tenant_id NOT IN (SELECT tenant_id FROM user_tenants WHERE user_id = '<new-id>');
           DELETE FROM user_tenants WHERE user_id = '<old-id>';
           DELETE FROM principals WHERE id = '<old-id>' AND type = 'user';
           COMMIT;
      5. Then run: ${RERUN}
RECIPE
}

# <label> <db> <sql file>: run one database's rewrite (one transaction) and
# judge its report (scripts/sql/redomain-*.sql, header): `leftover` lines and a
# missing required target fail; `warn` / `warn-regex` lines are printed for the
# operator; `skipped` lines are INFO. Only names and counts are printed.
_reconcile_db() {
    local out prc=0 left line
    out="$(docker exec -i -e PGPASSWORD postgres psql -X -q -tA -U "$PG_USER" -d "$2" \
        -v ON_ERROR_STOP=1 -v old_re="$OLD_RE" -v new="$NEW" < "$3" 2>&1)" || prc=$?
    if [ "$prc" -ne 0 ]; then
        if printf '%s\n' "$out" | grep -q 'WARNING: *missing '; then
            printf '%s\n' "$out" | grep -oE 'missing [A-Za-z0-9_.|]+' | sed "s/^/✗ $1: /" || true
            fail "$1: this version lacks a table or column the re-domain must rewrite; nothing was changed in ${2}. Report it together with the image version (docker ps --format '{{.Image}}')."
            return 0
        fi
        fail "$1: psql exited ${prc}, the transaction was rolled back (nothing changed in ${2}). Run: ${RERUN}"
        printf '%s\n' "$out" | grep -E '^(psql:|ERROR:)' | sed -n '1,3s/^/    /p' || true
        return 0
    fi
    left="$(printf '%s\n' "$out" | tail -n 1)"
    case "$left" in ''|*[!0-9]*) fail "$1: unexpected psql output, not verified. Run: ${RERUN}"; return 0 ;; esac
    while read -r kind tc a b; do
        case "$kind" in
            rewritten) echo "  rewritten ${tc}: ${a} row(s)" ;;
            skipped) echo "  INFO: skipped ${tc} (this version does not have it)" ;;
            leftover) fail "$1: ${a} row(s) in ${tc} still name ${OLD}; they are not rewritten automatically. Inspect them, then run: ${RERUN}"
                [ "$3" = scripts/sql/redomain-cognee.sql ] && _cognee_merge_recipe ;;
            warn) echo "  WARN: ${a} row(s) in ${tc} still name ${OLD} (reported only, not rewritten)." ;;
            warn-regex) echo "  WARN: ${a} regex-mode redirect URI(s) in ${b} OAuth2 provider(s) still name ${OLD}. They are never rewritten automatically: edit them in the Authentik admin UI (Applications > Providers > the provider > Redirect URIs)." ;;
        esac
    done < <(printf '%s\n' "$out")
    if [ "$left" != 0 ] && ! printf '%s\n' "$out" | grep -q '^leftover '; then
        fail "$1: ${left} row(s) still name ${OLD}. Run: ${RERUN}"
    fi
}

echo "→ Rewriting ${OLD} → ${NEW} in the Authentik and Open WebUI databases..."
_reconcile_db Authentik "$AK_DB" scripts/sql/redomain-authentik.sql
_reconcile_db "Open WebUI" "$OW_DB" scripts/sql/redomain-openwebui.sql
# #2719: the LLM Manager keeps its registered workers' addresses in its own
# database; one registered under the old domain is unreachable after the move.
if _in_list llm-manager "$running_containers" && [ -f scripts/sql/redomain-llm-manager.sql ]; then
    echo "→ Re-pointing the LLM Manager's registered workers (${OLD} → ${NEW})..."
    _reconcile_db "LLM Manager workers" "$LM_DB" scripts/sql/redomain-llm-manager.sql
fi

# #2719: cognee keys its users by e-mail. Left on the old domain, the first OIDC
# login on the new one finds no user and creates a second admin account.
if _in_list cognee "$running_containers" && [ -f scripts/sql/redomain-cognee.sql ]; then
    echo "→ Re-pointing cognee's user e-mail addresses (${OLD} → ${NEW})..."
    _reconcile_db "cognee users" "$CG_DB" scripts/sql/redomain-cognee.sql
fi

# #2928: Gitea keeps the admin's e-mail in its own rows; left on the old domain,
# SSO matching by e-mail and the mail notifications use the stale address.
if _in_list gitea "$running_containers" && [ -f scripts/sql/redomain-gitea.sql ]; then
    echo "→ Re-pointing Gitea's user e-mail addresses (${OLD} → ${NEW})..."
    _reconcile_db "Gitea users" "$GT_DB" scripts/sql/redomain-gitea.sql
fi

# Dify keys its accounts by e-mail, and the Authentik SSO login sends the
# new domain: an admin left on the old one cannot be matched.
if _in_list dify-api "$running_containers" && [ -f scripts/sql/redomain-dify.sql ]; then
    echo "→ Re-pointing Dify's account e-mail addresses (${OLD} → ${NEW})..."
    _reconcile_db "Dify accounts" "$DF_DB" scripts/sql/redomain-dify.sql
fi

if [ "$DB_ONLY" = false ]; then
    docker exec authentik-server ak shell -c 'from django.core.cache import cache; cache.clear()' >/dev/null 2>&1 \
        || echo "  (Authentik cache not cleared: authentik-server is not answering; the recreate below restarts it.)"

    targets=""
    for s in $DOMAIN_BAKING_SERVICES; do
        _in_list "$s" "$running_services" && targets="${targets} ${s}"
    done
    # #2719: the list is only a floor. Every running service whose container
    # still carries the old domain is recreated too: measured, not by name.
    # Never the datastores, the LLM runtime, backup or the config UI
    # (razzfazz_domain_env_protected): they do not route by the domain, and
    # recreating them interrupts the box. They are reported below instead.
    _stale_env
    if [ "$STALE_RC" -eq 2 ]; then
        echo "  WARN: the containers' environments could not be read (docker inspect); only the list is recreated."
    else
        for s in $(razzfazz_domain_env_drift_filter "$STALE" recreate | awk 'NF{print $1}' | sort -u); do
            _in_list "$s" "$running_services" || continue
            case " ${targets} " in *" ${s} "*) ;; *) targets="${targets} ${s}" ;; esac
        done
    fi
    if [ -n "$targets" ]; then
        echo "→ Recreating the services that read the domain at start:${targets}"
        # shellcheck disable=SC2086
        docker compose up -d --no-deps --force-recreate $targets >/dev/null 2>&1 || true
        _w=0; down="$targets"
        while :; do
            now="$(docker compose ps --services --status running 2>/dev/null || true)"
            down=""
            for s in $targets; do _in_list "$s" "$now" || down="${down} ${s}"; done
            [ -z "$down" ] && break
            [ "$_w" -ge "$WAIT_S" ] && break
            sleep 5; _w=$((_w + 5))
        done
        [ -n "$down" ] && fail "not running after the recreate:${down}. Run: docker compose up -d --no-deps --force-recreate${down}"
    fi

    # The config UI carries MAIN_DOMAIN in its own compose environment and
    # keeps the old value until it is recreated. This run is the CLI (the
    # config UI is not inside it), so it is recreated here, LAST. A run the
    # config UI performs itself is --db-only and never reaches this block.
    if [ "$STALE_RC" -ne 2 ]; then
        for s in $(razzfazz_domain_env_drift_filter "$STALE" last | awk 'NF{print $1}' | sort -u); do
            _in_list "$s" "$running_services" || continue
            echo "→ Recreating ${s} last (its environment carries the domain)"
            if ! docker compose up -d --no-deps --force-recreate "$s" >/dev/null 2>&1; then
                fail "could not recreate ${s}. Run: docker compose up -d --no-deps --force-recreate ${s}; then ${RERUN}"
            fi
        done
    fi

    # Only the keys the re-domain rewrites can be leftovers: .env *_DOMAIN
    # except the IdP allow-list (*_OAUTH_DOMAIN) and .env.dify *_URL /
    # *_ORIGINS. An admin@<old> address or a comment is not a stack host.
    _key_leftovers() {   # <file> <key ERE> <excluded-key ERE> → "<n> <keys>"
        [ -f "$1" ] || { echo 0; return 0; }
        OLD="$OLD" NEW="$NEW" KEY="$2" SKIP="$3" awk -f scripts/lib/redomain-hosts.awk -v mode=env "$1"
    }
    # an awk that did not run leaves n empty: that is "not verified", never 0
    _num() { case "${n:-}" in ''|*[!0-9]*) fail "$1: the leftover check did not run, NOT verified. Run: ${RERUN}"; n=0 ;; esac; }
    read -r n keys < <(_key_leftovers .env '^[A-Z0-9_]+_DOMAIN$' '(^MAIN_DOMAIN$|_OAUTH_DOMAIN$)') || true
    _num .env
    [ "$n" -gt 0 ] && fail ".env: ${n} key(s) still name ${OLD}: ${keys}. Run: rzfz setup --set-domain ${NEW}"
    read -r n keys < <(_key_leftovers .env.dify '^[A-Z0-9_]+_(URL|ORIGINS)$' '^$') || true
    _num .env.dify
    [ "$n" -gt 0 ] && fail ".env.dify: ${n} key(s) still name ${OLD}: ${keys}. Run: rzfz setup --set-domain ${NEW}"
    main="$(_env MAIN_DOMAIN | tr '[:upper:]' '[:lower:]')"
    [ "$main" = "$NEW" ] || fail ".env: MAIN_DOMAIN is '${main}', not ${NEW}. Run: rzfz setup --set-domain ${NEW}"

    # What the box SERVES: the host names in Caddy's live config.
    if _in_list caddy "$running_services"; then
        cfg=""; _w=0
        while :; do
            cfg="$(docker exec caddy wget -q -T 10 -O- http://127.0.0.1:2019/config/ 2>/dev/null || true)"
            [ -n "$cfg" ] && break
            [ "$_w" -ge "$WAIT_S" ] && break
            sleep 5; _w=$((_w + 5))
        done
        if [ -z "$cfg" ]; then
            echo "✗ Could not read Caddy's live config (docker exec caddy wget -q -T 10 -O- http://127.0.0.1:2019/config/): what the box serves is NOT verified."
            echo "  Check: docker logs caddy; then run: ${RERUN}"
            [ "$rc" -eq 0 ] && rc=4
        else
            read -r n hosts < <(printf '%s\n' "$cfg" | sed 's/\\\\//g; s/\\"//g' | grep -oE '"[^"]*"' \
                | OLD="$OLD" NEW="$NEW" awk -f scripts/lib/redomain-hosts.awk -v mode=hosts) || true
            _num "Caddy's live config"
            [ "$n" -gt 0 ] && fail "Caddy still serves ${n} host(s) under ${OLD}: ${hosts}. Run: docker compose up -d --no-deps --force-recreate caddy; then ${RERUN}"
        fi
    else
        echo "✗ caddy is not running: what the box serves is NOT verified. Start it (docker compose up -d caddy), then run: ${RERUN}"
        [ "$rc" -eq 0 ] && rc=4
    fi
fi

# #2653: the Open WebUI administrator is the one account whose KEY is the
# domain (`razzfazz-ai-admin@<MAIN_DOMAIN>`, modules/chat/compose.yml
# ADMIN_EMAIL, the seeder's sign-in). redomain-openwebui.sql rewrites config
# rows, not accounts, so after a move the seeder signed in as `@<new>` and got
# 400. ONE statement (data-modifying CTEs share a snapshot), exactly the stack's
# own address, and nothing at all when `@<new>` already exists: two accounts are
# never merged. Same statement as `rzfz init`'s migrate_owui_admin_identity
# (#2644); idempotent, so running after init's own move reports "absent".
# Kept the LAST step of the list: nothing else here depends on it.
echo "→ Moving the Open WebUI admin account (${OLD} → ${NEW})..."
_ow_old="razzfazz-ai-admin@${OLD}"; _ow_new="razzfazz-ai-admin@${NEW}"
_ow_have="$(docker exec -i -e PGPASSWORD postgres psql -X -q -tA -U "$PG_USER" -d postgres \
    -v ON_ERROR_STOP=1 -v old_re="$OLD_RE" -v old="$OLD" -v new="$NEW" -v db="$OW_DB" <<'SQL' 2>&1 || true
SELECT count(*) FROM pg_database WHERE datname = :'db';
SQL
)"
if [ "$_ow_have" != "1" ]; then
    echo "  INFO: no Open WebUI database (${OW_DB}) yet: nothing to carry over."
else
    # Open WebUI is running during the move: a lock it holds on auth/user must
    # not hang the run (\gset hides the SET result; one statement follows).
    _ow_prc=0
    _ow_out="$(docker exec -i -e PGPASSWORD postgres psql -X -q -tA -U "$PG_USER" -d "$OW_DB" \
        -v ON_ERROR_STOP=1 -v old_re="$OLD_RE" -v old="$OLD" -v new="$NEW" <<'SQL' 2>&1
SELECT set_config('lock_timeout', '5s', false), set_config('statement_timeout', '30s', false) \gset
-- a database Open WebUI has not migrated yet has no account tables: nothing to move
SELECT (to_regclass('auth') IS NOT NULL AND to_regclass('"user"') IS NOT NULL) AS has_tables \gset
\if :has_tables
WITH em AS (
    SELECT lower('razzfazz-ai-admin@' || :'old') AS o, lower('razzfazz-ai-admin@' || :'new') AS n
), st AS (
    SELECT (EXISTS (SELECT 1 FROM auth, em WHERE lower(auth.email) = em.o)
         OR EXISTS (SELECT 1 FROM "user", em WHERE lower("user".email) = em.o)) AS has_old,
           (NOT EXISTS (SELECT 1 FROM auth, em WHERE lower(auth.email) = em.n)
        AND NOT EXISTS (SELECT 1 FROM "user", em WHERE lower("user".email) = em.n)) AS free
), a AS (
    UPDATE auth SET email = 'razzfazz-ai-admin@' || :'new'
     WHERE lower(email) = (SELECT o FROM em) AND (SELECT free FROM st) RETURNING 1
), u AS (
    UPDATE "user" SET email = 'razzfazz-ai-admin@' || :'new'
     WHERE lower(email) = (SELECT o FROM em) AND (SELECT free FROM st) RETURNING 1
)
SELECT CASE WHEN NOT (SELECT has_old FROM st) THEN 'absent'
            WHEN (SELECT free FROM st)
            THEN 'moved ' || (SELECT count(*) FROM a) || ' ' || (SELECT count(*) FROM u)
            ELSE 'taken' END;
\else
SELECT 'absent';
\endif
SQL
    )" || _ow_prc=$?
    if [ "$_ow_prc" -ne 0 ]; then
        fail "Open WebUI admin: psql exited ${_ow_prc}, nothing was changed. Run: ${RERUN}"
        printf '%s\n' "$_ow_out" | grep -E '^(psql:|ERROR:)' | sed -n '1,3s/^/    /p' || true
    else
        case "$_ow_out" in
            "moved 1 1") echo "  rewritten Open WebUI admin: ${_ow_old} → ${_ow_new}" ;;
            absent)      echo "  INFO: no Open WebUI admin under ${_ow_old}: nothing to carry over." ;;
            taken)       echo "  WARN: ${_ow_new} already exists in Open WebUI: ${_ow_old} was left as it is (two accounts are never merged)." ;;
            *)           fail "Open WebUI admin: unexpected result '${_ow_out}', check the auth and user rows for ${_ow_new}. Run: ${RERUN}" ;;
        esac
    fi
fi

# #2719: no running container may still carry the old domain. A container's
# environment is fixed at creation; anything not recreated keeps it forever.
_stale_env
if [ "$STALE_RC" -eq 2 ]; then
    echo "✗ The containers' environments could not be read (docker inspect): NOT verified that none names ${OLD}."
    if [ "$DB_ONLY" = false ] && [ "$rc" -eq 0 ]; then rc=4; fi
else
    if [ "$STALE_RC" -eq 3 ]; then
        echo "✗ Only part of the running containers was measured ($(printf '%s\n' "$STALE" | sed -n 's/^# //p')): the rest is NOT verified."
        if [ "$DB_ONLY" = false ] && [ "$rc" -eq 0 ]; then rc=4; fi
    fi
    rec="$(razzfazz_domain_env_drift_filter "$STALE" recreate)"
    prot="$(razzfazz_domain_env_drift_filter "$STALE" protected)"
    man="$(razzfazz_domain_env_drift_filter "$STALE" manual)"
    envk="$(razzfazz_domain_env_drift_filter "$STALE" envfile | awk '{print $3}' | sort -u | tr '\n' ' ' | sed 's/ $//')"
    if [ -n "$envk" ]; then
        # the containers hold exactly what .env says: .env is the cause
        for k in $envk; do
            if [ "$DB_ONLY" = true ]; then
                echo "  WARN: .env key ${k} still names ${OLD}: fix its value in .env, then run: ${RERUN}"
            else
                fail ".env key ${k} still names ${OLD}: fix its value in .env, then run: ${RERUN}"
            fi
        done
    fi
    if [ -n "$rec" ]; then
        sum="$(razzfazz_domain_env_drift_summary "$rec")"
        if [ "$DB_ONLY" = true ]; then
            # The Config Portal / init recreate their own services right after this
            # step, so this is a report, not a failure; nothing is recreated here.
            echo "  WARN: these running containers still carry ${OLD} in their environment: ${sum%%|*}."
            echo "  If \`rzfz status\` still lists them once the apply has finished, run: ${sum#*|}"
        else
            fail "these running containers still carry ${OLD} in their environment (fixed when a container is created): ${sum%%|*}. Run: ${sum#*|}; then ${RERUN}"
        fi
    fi
    if [ -n "$man" ]; then
        sum="$(razzfazz_domain_env_drift_summary "$man")"
        if [ "$DB_ONLY" = true ]; then
            echo "  WARN: ${sum%%|*} still carry ${OLD} in their environment. They run engines and are not recreated here: drain the engines, then run: ${sum#*|}"
        else
            fail "${sum%%|*} still carry ${OLD} in their environment and are never recreated automatically (they run engines). Three steps: (1) in the LLM Manager console, Workers screen, drain the worker(s) on ${sum%%|*}; (2) run: ${sum#*|}; (3) undrain the worker(s) in the same screen (undrain schedules their models again), and once the engines are healthy, run: rzfz setup --set-domain ${NEW}"
        fi
    fi
    if [ "$DB_ONLY" = false ]; then
        lst="$(razzfazz_domain_env_drift_filter "$STALE" last)"
        if [ -n "$lst" ]; then
            fail "$(razzfazz_domain_env_drift_names "$lst") still carry ${OLD} in their environment after the recreate. Run: docker compose up -d --no-deps --force-recreate $(printf '%s\n' "$lst" | awk 'NF{print $1}' | sort -u | tr '\n' ' '); then ${RERUN}"
        fi
        prot="$(razzfazz_domain_env_drift_filter "$STALE" protected | grep -v '^razzfazz-config ' || true)"
    fi
    if [ -n "$prot" ]; then
        echo "  WARN: $(razzfazz_domain_env_drift_names "$prot") still carry ${OLD} in their environment. They do not route by the domain and are never recreated automatically (datastore, LLM runtime, backup or the config UI); they pick up ${NEW} at their next planned recreate."
    fi
fi

# #2770: the handover PDFs embed MAIN_DOMAIN. Caddy serves the new domain by now
# (and gotenberg is a plain container, so this needs no recreate). Full mode only:
# the Config Portal path (--db-only) has no docker socket for the generators.
# Idempotent and non-fatal: a failure prints a WARN, the exit code is untouched.
if [ "$DB_ONLY" = false ] && [ -x scripts/regenerate-handover-pdfs.sh ]; then
    echo "→ Regenerating getting-started.pdf and quickstart.pdf for ${NEW}..."
    scripts/regenerate-handover-pdfs.sh || echo "  WARN: the handover PDFs were not regenerated. Run: ./scripts/regenerate-handover-pdfs.sh"
fi

# #2981: Matrix keeps its server name. It is the suffix of every user ID and
# Synapse refuses to start under another one, so the move leaves it (and says so).
_mx="$(_env MATRIX_SERVER_NAME | tr '[:upper:]' '[:lower:]')"
if [ -n "$_mx" ] && [ "$_mx" != "$NEW" ]; then
    echo "  Matrix: user IDs keep the server name ${_mx} (@user:${_mx}); a Matrix server name cannot change. Synapse and Element are reached on the new hosts (matrix.${NEW}, element.${NEW}) and users sign in as before. To start Matrix over under ${NEW} instead (deletes all Matrix data): rzfz module reset-data matrix --i-understand-data-loss"
fi

if [ "$rc" -eq 0 ]; then
    if [ "$DB_ONLY" = true ]; then
        echo "✓ No Authentik or Open WebUI row names ${OLD}."
    else
        echo "✓ Re-domain complete: Caddy serves ${NEW}; no stack key in .env / .env.dify and no Authentik or Open WebUI row names ${OLD}."
        # #2781: a clean full verdict settles the move. Without this the marker
        # outlived a finished move and refused the next `rzfz setup --set-domain`.
        # Only a marker that names THIS move goes (old, or new for a move back).
        if [ -f .redomain-pending ]; then
            _pending="$(head -n 1 .redomain-pending | tr -d '[:space:]')" || _pending=""
            case "$_pending" in "$OLD"|"$NEW") rm -f .redomain-pending ;; esac
        fi
    fi
fi
exit "$rc"
