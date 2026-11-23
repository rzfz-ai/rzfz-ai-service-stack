#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# rzfz-redomain-fixup-ga5.sh — finish a domain change on a 2026.09-ga.5 box (#2932).
#
#   ./rzfz-redomain-fixup-ga5.sh --old <old-domain> [--new <new-domain>] [--apply]
#                                [--stack-dir <dir>] [--any-version]
#
# On 2026.09-ga.5 `rzfz setup --set-domain <new>` only rewrites .env. A set of
# application rows keeps the old domain (Open WebUI config + admin account,
# Dify accounts, Gitea e-mails + login source, Authentik admin e-mail, a cognee
# duplicate admin, Authentik outpost providers). 2026.11 automates this
# (scripts/redomain-reconcile.sh); this is the self-contained port for ga.5
# boxes that cannot upgrade yet: offline, only `docker exec` into the running
# containers (no image pull, no pip, no internet).
#
# Default is a DRY RUN: every change runs inside a transaction that is rolled
# back, so the counts shown are exactly what --apply would do. --apply:
#   1. refuses unless .env MAIN_DOMAIN is the new domain and no running
#      container still carries the old domain (guide steps 2-4 come first);
#   2. pg_dump --data-only of every table it will touch into
#      ~/redomain-backup-<UTC timestamp>/ (0700 dir, 0600 files);
#   3. rewrites each application in ONE transaction (sql/<app>.sql);
#   4. restarts openwebui / gitea when their rows changed, re-runs
#      openwebui-seed, re-attaches the Authentik outpost providers;
#   5. re-counts: every count must be 0 -> "OK", otherwise "NICHT OK" with the
#      next action.
# A second --apply finds nothing to change and changes nothing.
#
# Operator-facing messages are German (customer tool); comments are English.
# Never prints a secret: no env dumps; the database user is read with
# `docker exec postgres printenv POSTGRES_USER`.
#
# Exit: 0 OK (or a clean dry run) · 1 NICHT OK / refused precondition after
# checks · 2 usage · 3 postgres not running (nothing changed).
set -euo pipefail

TOOL_VERSION="2026.09-ga.5-fixup.1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_DIR="${HERE}/sql"
WAIT_S="${RZFZ_FIXUP_WAIT_S:-300}"
SEED_WAIT_S="${RZFZ_FIXUP_SEED_WAIT_S:-600}"
POLL_S="${RZFZ_FIXUP_POLL_S:-5}"

usage() {
    cat >&2 <<'EOF'
Aufruf:
  ./rzfz-redomain-fixup-ga5.sh --old <ALTE-DOMAIN>            (Trockenlauf, ändert nichts)
  ./rzfz-redomain-fixup-ga5.sh --old <ALTE-DOMAIN> --apply    (führt die Änderungen aus)

Optionen:
  --old <domain>      die bisherige Domain (Pflicht), z. B. razzfazz-ai.slite.own
  --new <domain>      zur Kontrolle: muss mit MAIN_DOMAIN in .env übereinstimmen
  --apply             Änderungen wirklich ausführen (ohne: nur anzeigen)
  --stack-dir <dir>   Installationsverzeichnis (Standard: aktuelles Verzeichnis,
                      sonst ~/razzfazz-ai-service-stack)
  --any-version       auch auf einer anderen Version als 2026.09-ga.5 laufen
                      (nur nach Rücksprache mit dem Support)
EOF
    exit 2
}

die() { echo "FEHLER: $*" >&2; exit "${2:-2}"; }

OLD=""; NEW_ARG=""; APPLY=false; STACK=""; ANY_VERSION=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --old) [ "$#" -ge 2 ] || usage; OLD="$2"; shift 2 ;;
        --old=*) OLD="${1#--old=}"; shift ;;
        --new) [ "$#" -ge 2 ] || usage; NEW_ARG="$2"; shift 2 ;;
        --new=*) NEW_ARG="${1#--new=}"; shift ;;
        --apply) APPLY=true; shift ;;
        --stack-dir) [ "$#" -ge 2 ] || usage; STACK="$2"; shift 2 ;;
        --any-version) ANY_VERSION=true; shift ;;
        -h|--help) usage ;;
        *) echo "Unbekannte Option: $1" >&2; usage ;;
    esac
done
[ -n "$OLD" ] || { echo "Die alte Domain fehlt (--old <ALTE-DOMAIN>)." >&2; usage; }

_lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
_is_domain() { [[ "$1" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] && [[ "$1" == *.* ]]; }
OLD="$(_lc "$OLD")"; NEW_ARG="$(_lc "$NEW_ARG")"
_is_domain "$OLD" || die "'${OLD}' ist kein gültiger Domainname (--old)."

if [ -z "$STACK" ]; then
    if [ -f ./.env ] && [ -f ./compose.yml ]; then STACK="$(pwd)"; else STACK="${HOME}/razzfazz-ai-service-stack"; fi
fi
[ -f "${STACK}/.env" ] || die "Keine .env in ${STACK} gefunden. Bitte im Installationsverzeichnis ausführen (cd ~/razzfazz-ai-service-stack) oder --stack-dir angeben."
cd "$STACK"
for f in prelude openwebui dify gitea cognee authentik llm-manager; do
    [ -f "${SQL_DIR}/${f}.sql" ] || die "Datei sql/${f}.sql fehlt neben dem Skript: bitte das Paket vollständig entpacken."
done
command -v docker >/dev/null 2>&1 || die "docker wurde nicht gefunden."

# .env is read by grep, never sourced (values may hold spaces / metacharacters).
_env() { { grep -m1 "^$1=" .env 2>/dev/null || true; } | cut -d= -f2- | tr -d "\"'\r" | sed 's/[[:space:]]*$//'; }
NEW="$(_lc "$(_env MAIN_DOMAIN)")"
[ -n "$NEW" ] || die "MAIN_DOMAIN ist in .env nicht gesetzt."
if [ -n "$NEW_ARG" ] && [ "$NEW_ARG" != "$NEW" ]; then
    die "In .env steht MAIN_DOMAIN=${NEW}, nicht ${NEW_ARG}. Bitte zuerst Schritt 2 der Anleitung ausführen: rzfz setup --set-domain ${NEW_ARG}"
fi
if [ "$OLD" = "$NEW" ]; then
    die "Die alte Domain (${OLD}) ist gleich MAIN_DOMAIN in .env. Der Domainwechsel (Schritt 2: rzfz setup --set-domain <NEU>) wurde noch nicht ausgeführt, oder bei --old steht die neue statt der alten Domain."
fi
_is_domain "$NEW" || die "MAIN_DOMAIN in .env ('${NEW}') ist kein gültiger Domainname."
VERSION_ENV="$(_env RAZZFAZZ_VERSION)"
if [ "$VERSION_ENV" != "2026.09-ga.5" ] && [ "$ANY_VERSION" != true ]; then
    die "Dieses Werkzeug ist für Version 2026.09-ga.5 gebaut; diese Box meldet RAZZFAZZ_VERSION='${VERSION_ENV}'. Bitte den Support kontaktieren."
fi

OW_DB="$(_env OPENWEBUI_DB)"; OW_DB="${OW_DB:-openwebui_db}"
DF_DB="$(_env DIFY_DB)"; DF_DB="${DF_DB:-dify_db}"
GT_DB="$(_env GITEA_DB)"; GT_DB="${GT_DB:-gitea_db}"
CG_DB="$(_env COGNEE_DB)"; CG_DB="${CG_DB:-cognee_db}"
AK_DB="$(_env AUTHENTIK_DB)"; AK_DB="${AK_DB:-authentik_db}"
LM_DB="$(_env LLM_MANAGER_DB)"; LM_DB="${LM_DB:-llm_manager_db}"

_nl=$'\n'
_in_list() { case "${_nl}$2${_nl}" in *"${_nl}$1${_nl}"*) return 0 ;; esac; return 1; }
RUNNING="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
if ! _in_list postgres "$RUNNING"; then
    echo "Der Datenbank-Container 'postgres' läuft nicht: es wurde nichts geändert."
    echo "Bitte zuerst die Dienste starten (docker compose up -d) und das Werkzeug erneut aufrufen."
    exit 3
fi
PG_USER="$(docker exec postgres printenv POSTGRES_USER 2>/dev/null | tr -d '\r' || true)"
[ -n "$PG_USER" ] || die "Der Datenbank-Benutzer konnte nicht ermittelt werden (docker exec postgres printenv POSTGRES_USER)." 1

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR=""
if [ "$APPLY" = true ]; then
    umask 077
    # mktemp -d: 0700 and unique, so two runs in the same second never collide.
    BACKUP_DIR="$(mktemp -d "${HOME}/redomain-backup-${TS}-XXXX")" \
        || die "Das Sicherungsverzeichnis in ${HOME} konnte nicht angelegt werden." 1
    # Everything below is also written to the run's log for the support.
    exec > >(tee -a "${BACKUP_DIR}/fixup.log") 2>&1
fi

line() { printf '%s\n' "------------------------------------------------------------------------"; }
line
echo "razzfazz.ai Domainwechsel – Nacharbeiten für 2026.09-ga.5 (Werkzeug ${TOOL_VERSION})"
echo "  Installationsverzeichnis : ${STACK}"
echo "  alte Domain              : ${OLD}"
echo "  neue Domain (aus .env)   : ${NEW}"
if [ "$APPLY" = true ]; then echo "  Modus                    : AUSFÜHREN (--apply)"
else echo "  Modus                    : TROCKENLAUF – es wird nichts geändert"; fi
line

psql_q() {   # <db> <sql>: one value, read-only
    docker exec -i postgres psql -X -q -tA -U "$PG_USER" -d "$1" -v ON_ERROR_STOP=1 <<<"$2"
}
db_exists() { [ "$(psql_q postgres "SELECT count(*) FROM pg_database WHERE datname = '$1'" 2>/dev/null || echo 0)" = 1 ]; }

# <db> <table.column ...>: prints the targets this database lacks
missing_columns() {
    local db="$1"; shift
    local vals="" tc
    for tc in "$@"; do vals="${vals}${vals:+,}('${tc%%.*}','${tc#*.}')"; done
    psql_q "$db" "SELECT w.t || '.' || w.c FROM (VALUES ${vals}) AS w(t, c)
        WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns i
                           WHERE i.table_schema = current_schema() AND i.table_name = w.t AND i.column_name = w.c)"
}

# The running containers whose environment still names the old domain (guide step 4).
_old_re="$(printf '%s' "$OLD" | sed 's/[.]/\\./g')"
_new_re="$(printf '%s' "$NEW" | sed 's/[.]/\\./g')"
env_names_old() {   # stdin: environment lines
    # strip the new domain first, so a new domain below the old one is not a hit
    # (grep reads ALL input: an early `grep -q` exit would SIGPIPE the writer and,
    # under pipefail, turn a hit into a miss)
    sed "s/${_new_re}//Ig" | grep -iE "(^|[^a-z0-9-])${_old_re}(\$|[^a-z0-9.-])" >/dev/null
}
stale_containers() {   # prints "<container> <compose service or -> <compose|agent|other>"
    local c svc kind
    for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
        if docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" 2>/dev/null | env_names_old; then
            svc="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$c" 2>/dev/null || true)"
            # Per-user agents (Agent Manager) carry the compose service label of
            # their IMAGE service but are not that service: never recreate them
            # through compose. Compose's own containers carry a container number.
            if [ -n "$(docker inspect -f '{{index .Config.Labels "razzfazz.agent.instance"}}' "$c" 2>/dev/null || true)" ]; then
                kind=agent
            elif [ -n "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.container-number"}}' "$c" 2>/dev/null || true)" ]; then
                kind=compose
            else
                kind=other
            fi
            printf '%s %s %s\n' "$c" "${svc:--}" "$kind"
        fi
    done
}
# sets STALE_COMPOSE (services), STALE_AGENTS and STALE_OTHER (container names)
stale_split() {
    STALE_COMPOSE="$(printf '%s\n' "$1" | awk 'NF && $3 == "compose" {print $2}' | sort -u | tr '\n' ' ' | sed 's/ $//')"
    STALE_AGENTS="$(printf '%s\n' "$1" | awk 'NF && $3 == "agent" {print $1}' | sort -u | tr '\n' ' ' | sed 's/ $//')"
    STALE_OTHER="$(printf '%s\n' "$1" | awk 'NF && $3 == "other" {print $1}' | sort -u | tr '\n' ' ' | sed 's/ $//')"
}
# .env.dify of an aged box can hold the domain literally (newer templates use
# ${DIFY_DOMAIN}); 2026.09-ga.5 set-domain does not touch it. Prints the count.
envdify_old() {
    [ -f .env.dify ] || { echo 0; return 0; }
    { grep -v '^[[:space:]]*#' .env.dify || true; } | sed "s/${_new_re}//Ig" \
        | { grep -ciE "(^|[^a-z0-9-])${_old_re}(\$|[^a-z0-9.-])" || true; }
}

APPS="openwebui dify gitea cognee authentik llm-manager"
app_db() {
    case "$1" in openwebui) echo "$OW_DB" ;; dify) echo "$DF_DB" ;; gitea) echo "$GT_DB" ;;
        cognee) echo "$CG_DB" ;; authentik) echo "$AK_DB" ;; llm-manager) echo "$LM_DB" ;; esac
}
app_label() {
    case "$1" in openwebui) echo "Open WebUI (Chat)" ;; dify) echo "Dify" ;; gitea) echo "Gitea" ;;
        cognee) echo "Cognee" ;; authentik) echo "Authentik (Anmeldung)" ;; llm-manager) echo "LLM-Konsole" ;; esac
}
app_columns() {
    case "$1" in
        openwebui) echo "config.value user.id user.email auth.id auth.email" ;;
        dify) echo "accounts.id accounts.email accounts.normalized_email" ;;
        gitea) echo "user.id user.email user.avatar_email email_address.id email_address.email email_address.lower_email login_source.cfg external_login_user.email" ;;
        cognee) echo "users.id users.email principals.id principals.type" ;;
        authentik) echo "authentik_core_user.email authentik_providers_proxy_proxyprovider.external_host authentik_providers_proxy_proxyprovider.cookie_domain authentik_outposts_outpost._config authentik_providers_oauth2_oauth2provider._redirect_uris authentik_core_application.meta_launch_url" ;;
        llm-manager) echo "workers.address" ;;
    esac
}
# the tables a change can touch (cognee: the account rows and what a delete cascades to)
app_tables() {
    case "$1" in
        openwebui) echo 'config "user" auth' ;;
        dify) echo 'accounts' ;;
        gitea) echo '"user" email_address login_source external_login_user' ;;
        cognee) echo 'principals users user_api_key dataset_database principal_configuration user_default_permissions' ;;
        authentik) echo 'authentik_core_user authentik_providers_proxy_proxyprovider authentik_outposts_outpost authentik_providers_oauth2_oauth2provider authentik_core_application' ;;
        llm-manager) echo 'workers' ;;
    esac
}
target_label() {
    case "$1" in
        openwebui.config) echo "Einstellungen (u. a. Anwendungs-URL)" ;;
        openwebui.user|openwebui.auth) echo "Benutzerkonten (${1#openwebui.})" ;;
        dify.accounts) echo "Benutzerkonten (E-Mail)" ;;
        gitea.user) echo "Benutzer-E-Mail" ;;
        gitea.user.avatar_email) echo "Avatar-E-Mail" ;;
        gitea.email_address) echo "E-Mail-Adressen" ;;
        gitea.login_source) echo "Anmeldequelle" ;;
        gitea.external_login_user) echo "verknüpfte SSO-Konten" ;;
        cognee.users) echo "Benutzerkonten (E-Mail)" ;;
        authentik.user) echo "Benutzer-E-Mail (u. a. Administrator)" ;;
        authentik.proxyprovider) echo "Proxy-Provider" ;;
        authentik.outpost) echo "Outpost-Konfiguration" ;;
        authentik.redirect_uris) echo "Redirect-URIs" ;;
        authentik.launch_url) echo "Start-URLs der Anwendungen" ;;
        llm-manager.workers) echo "registrierte Worker" ;;
        *) echo "$1" ;;
    esac
}

declare -A BEFORE CHANGED AFTER
declare -A APP_STATE   # absent | schema | error | stop | ok
STOP_LINES=""; NOTE_LINES=""; INFO_LINES=""
ORDER=""

# <app> <on|off>: run the app's file, parse its protocol into the arrays
run_app() {
    local app="$1" mode="$2" db out rc=0 kind tgt rest n
    db="$(app_db "$app")"
    out="$(cat "${SQL_DIR}/prelude.sql" "${SQL_DIR}/${app}.sql" \
        | docker exec -i postgres psql -X -q -tA -U "$PG_USER" -d "$db" \
            -v ON_ERROR_STOP=1 -v old="$OLD" -v new="$NEW" -v apply="$mode" 2>&1)" || rc=$?
    if [ "$rc" -ne 0 ]; then
        APP_STATE[$app]=error
        echo "  FEHLER bei $(app_label "$app"): die Datenbank meldet einen Fehler, es wurde dort nichts geändert."
        printf '%s\n' "$out" | grep -E '^(psql:|ERROR:|FEHLER:)' | sed -n '1,3s/^/    /p' || true
        return 0
    fi
    while read -r kind tgt rest; do
        n="${rest%% *}"
        case "$kind" in
            before) BEFORE[$tgt]="$n"; _in_list "$tgt" "$ORDER" || ORDER="${ORDER}${tgt}${_nl}" ;;
            changed) CHANGED[$tgt]="$n" ;;
            after) AFTER[$tgt]="$n" ;;
            stop) STOP_LINES="${STOP_LINES}${rest}${_nl}"; APP_STATE[$app]=stop ;;
            note) NOTE_LINES="${NOTE_LINES}${tgt} ${rest}${_nl}" ;;
            info) if [ "$n" != 0 ]; then INFO_LINES="${INFO_LINES}${tgt} ${n}${_nl}"; fi ;;
        esac
    done <<<"$out"
    [ "${APP_STATE[$app]:-}" = stop ] || APP_STATE[$app]=ok
}

reset_counts() { BEFORE=(); CHANGED=(); AFTER=(); STOP_LINES=""; NOTE_LINES=""; INFO_LINES=""; ORDER=""; }

# <mode> : measure (off) or change (on) every installed application
pass() {
    local mode="$1" app miss
    for app in $APPS; do
        if ! db_exists "$(app_db "$app")"; then APP_STATE[$app]=absent; continue; fi
        # shellcheck disable=SC2046
        miss="$(missing_columns "$(app_db "$app")" $(app_columns "$app") 2>/dev/null || echo '?')"
        if [ -n "$miss" ]; then
            APP_STATE[$app]=schema
            echo "  $(app_label "$app"): die Datenbank hat nicht die erwartete Struktur (fehlt: $(printf '%s' "$miss" | tr '\n' ' ')) – übersprungen."
            continue
        fi
        run_app "$app" "$mode"
    done
}

app_changes() {   # <app>: rows the last pass changed / would change
    local t s=0
    for t in "${!CHANGED[@]}"; do case "$t" in "$1".*) s=$((s + CHANGED[$t])) ;; esac; done
    echo "$s"
}
total() {   # <array name>
    local -n _a="$1"; local t s=0
    for t in "${!_a[@]}"; do s=$((s + _a[$t])); done
    echo "$s"
}

print_table() {   # <headline for the change column>
    local app t lbl
    printf '  %-24s %-38s %6s %8s %7s\n' "Anwendung" "Bereich" "alt" "$1" "danach"
    for app in $APPS; do
        case "${APP_STATE[$app]:-absent}" in
            absent) printf '  %-24s %s\n' "$(app_label "$app")" "nicht installiert – übersprungen"; continue ;;
            schema|error) printf '  %-24s %s\n' "$(app_label "$app")" "NICHT GEPRÜFT (siehe oben)"; continue ;;
        esac
        while IFS= read -r t; do
            [ -n "$t" ] || continue
            case "$t" in "$app".*) ;; *) continue ;; esac
            lbl="$(target_label "$t")"
            printf '  %-24s %-38s %6s %8s %7s\n' "$(app_label "$app")" "$lbl" "${BEFORE[$t]:-?}" "${CHANGED[$t]:-0}" "${AFTER[$t]:-?}"
        done <<<"$ORDER"
    done
}

print_cognee() {
    local email oo ne onn tgt a d e n
    if [ -n "$NOTE_LINES" ]; then
        while read -r tgt a _b _c d e; do
            [ -n "$tgt" ] || continue
            # note cognee.deleted <deleted address> 0 keep <kept address> <rows>
            echo "  Cognee: doppeltes Konto ${a} besitzt keine Daten und wird entfernt; ${d} (${e} Datensätze) bleibt erhalten."
        done <<<"$NOTE_LINES"
    fi
    if [ -n "$STOP_LINES" ]; then
        while read -r email oo ne onn; do
            [ -n "$email" ] || continue
            echo "  ACHTUNG Cognee: es gibt zwei Konten, und BEIDE besitzen Daten:"
            echo "      ${email} (${oo} Datensätze) und ${ne} (${onn} Datensätze)."
            echo "      Das Werkzeug ändert in Cognee deshalb NICHTS. Bitte kein Konto selbst löschen,"
            echo "      sondern den Support kontaktieren (siehe Zusammenfassung)."
        done <<<"$STOP_LINES"
    fi
    if [ -n "$INFO_LINES" ]; then
        while read -r tgt n; do
            [ -n "$tgt" ] || continue
            if [ "$tgt" = authentik.regex_redirect_uris ]; then
                echo "  Hinweis Authentik: ${n} Redirect-URI(s) im Regex-Modus nennen noch ${OLD}; diese bitte in der Authentik-Oberfläche anpassen (Anwendungen > Provider > Redirect URIs)."
            fi
        done <<<"$INFO_LINES"
    fi
}

outpost_check() {   # prints "<attached> <providers> <missing>" or "? ? ?"
    local out a p m
    out="$(docker exec authentik-worker ak shell -c "from authentik.outposts.models import Outpost as O; from authentik.providers.proxy.models import ProxyProvider as P; o=O.objects.get(name='authentik Embedded Outpost'); a=set(o.providers.values_list('pk',flat=True)); allp=list(P.objects.all()); print('attached',len(a),'proxyproviders',len(allp)); [print('MISSING',p.name) for p in allp if p.pk not in a]" 2>/dev/null || true)"
    a="$(printf '%s\n' "$out" | awk '$1=="attached"{print $2}' | tail -1)"
    p="$(printf '%s\n' "$out" | awk '$1=="attached"{print $4}' | tail -1)"
    m="$(printf '%s\n' "$out" | grep -c '^MISSING' || true)"
    printf '%s %s %s\n' "${a:-?}" "${p:-?}" "${m:-0}"
}

outpost_reattach() {
    local profiles
    profiles="$(_env COMPOSE_PROFILES)"
    docker cp core/Authentik/apply-policy-bindings.py authentik-worker:/tmp/apply_policy_bindings.py >/dev/null 2>&1 || return 1
    docker exec -e COMPOSE_PROFILES="$profiles" authentik-worker python /tmp/apply_policy_bindings.py >/dev/null 2>&1 || return 1
}

wait_ready() {   # <container>: running (and healthy when it has a healthcheck)
    local w=0 st
    while :; do
        st="$(docker inspect -f '{{.State.Status}}{{if .State.Health}}/{{.State.Health.Status}}{{end}}' "$1" 2>/dev/null || true)"
        case "$st" in running|running/healthy) return 0 ;; esac
        [ "$w" -ge "$WAIT_S" ] && return 1
        sleep "$POLL_S"; w=$((w + POLL_S))
    done
}

seed_state() {   # prints: ok | absent | failed | stale | running
    local st
    st="$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' openwebui-seed 2>/dev/null || true)"
    [ -n "$st" ] || { echo absent; return 0; }
    if docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' openwebui-seed 2>/dev/null | env_names_old; then echo stale; return 0; fi
    case "$st" in "exited 0") echo ok ;; running*|restarting*|created*) echo running ;; *) echo failed ;; esac
}

seed_rerun() {
    local w=0 st
    docker compose up -d --no-deps --force-recreate --pull never openwebui-seed >/dev/null 2>&1 || return 1
    while :; do
        st="$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' openwebui-seed 2>/dev/null || true)"
        case "$st" in "exited 0") return 0 ;; exited*|dead*) return 1 ;; esac
        [ "$w" -ge "$SEED_WAIT_S" ] && return 1
        sleep "$POLL_S"; w=$((w + POLL_S))
    done
}

# ---------------------------------------------------------------------------
echo "1) Container, die noch mit der alten Domain laufen (Schritt 4 der Anleitung):"
STALE="$(stale_containers)"
stale_split "$STALE"
DIFY_ENV_OLD="$(envdify_old)"
if [ -z "$STALE_COMPOSE" ] && [ -z "$STALE_OTHER" ] && [ -z "$STALE_AGENTS" ]; then
    echo "  keine – in Ordnung."
else
    if [ -n "$STALE_COMPOSE" ]; then echo "  Dienste: ${STALE_COMPOSE}"; fi
    if [ -n "$STALE_AGENTS" ]; then
        echo "  Hinweis: persönliche Agenten laufen noch mit ${OLD}: ${STALE_AGENTS}"
        echo "           (kein Hindernis; sie werden bei Bedarf unter \"Meine Agenten\" neu gestartet)."
    fi
    if [ -n "$STALE_OTHER" ]; then echo "  Hinweis: weitere Container (nicht von docker compose): ${STALE_OTHER}"; fi
fi
if [ "$DIFY_ENV_OLD" != 0 ]; then
    echo "  Die Datei .env.dify nennt noch ${DIFY_ENV_OLD}x die alte Domain (ältere Installation)."
fi

# What must be done BEFORE this tool may change anything (guide steps 2-4).
precondition_text() {
    if [ "$DIFY_ENV_OLD" != 0 ]; then
        echo "Die Datei .env.dify nennt noch die alte Domain ${OLD}. Bitte korrigieren und die Dienste neu starten:"
        # In place (the Settings portal and the backup pair mount .env.dify as a
        # single file; a replaced file leaves them on the old copy), dots escaped.
        echo "  install -m 600 .env.dify ~/env.dify.vor-domainwechsel"
        echo "  sed \"s/$(printf '%s' "$OLD" | sed 's/\./\\./g')/${NEW}/g\" ~/env.dify.vor-domainwechsel > .env.dify"
        echo "  docker compose up -d"
    elif [ -n "$STALE_COMPOSE" ]; then
        echo "Diese Dienste laufen noch mit der alten Domain ${OLD} (Schritt 4 der Anleitung):"
        echo "  docker compose up -d --no-deps --force-recreate ${STALE_COMPOSE}"
    fi
}
PRECOND="$(precondition_text)"

echo
echo "2) Datenbank-Einträge mit der alten Domain:"
reset_counts
pass off
PLANNED="$(total CHANGED)"
print_table "ändern"
print_cognee

echo
echo "3) Authentik: Anwendungen am Anmelde-Gateway (Outpost):"
read -r OA OP OM < <(outpost_check)
if [ "$OA" = "?" ]; then echo "  nicht prüfbar (authentik-worker antwortet nicht)."
else echo "  ${OA} von ${OP} verbunden, fehlend: ${OM}"; fi
SEED0="$(seed_state)"

if [ "$APPLY" != true ]; then
    echo
    line
    blockers=0
    for app in $APPS; do case "${APP_STATE[$app]:-}" in schema|error) blockers=1 ;; esac; done
    if [ -n "$PRECOND" ]; then
        echo "TROCKENLAUF: Bevor das Werkzeug etwas ändern darf, ist noch ein Schritt nötig."
        printf '%s\n' "$PRECOND"
        echo "Danach das Werkzeug erneut aufrufen."
        exit 1
    fi
    if [ "$blockers" -ne 0 ]; then
        echo "TROCKENLAUF: Mindestens eine Anwendung konnte nicht geprüft werden (siehe oben). Bitte den Support kontaktieren."
        exit 1
    fi
    if [ "$PLANNED" -eq 0 ] && [ -z "$STOP_LINES" ] && [ "$OM" = 0 ] && [ "$SEED0" = ok ]; then
        echo "TROCKENLAUF: Es ist nichts mehr zu tun – alle Einträge verwenden bereits ${NEW}."
    else
        echo "TROCKENLAUF: ${PLANNED} Einträge würden auf ${NEW} umgestellt (Spalte \"ändern\")."
        [ -n "$STOP_LINES" ] && echo "  Cognee würde NICHT geändert (beide Konten besitzen Daten, siehe oben)."
        [ "$OM" != 0 ] && [ "$OM" != "" ] && echo "  Authentik: ${OM} Anwendung(en) würden wieder mit dem Outpost verbunden."
        case "$SEED0" in stale|failed) echo "  Der Chat-Einrichtungsdienst (openwebui-seed) würde neu gestartet." ;; esac
        echo
        echo "Zum Ausführen denselben Befehl mit --apply aufrufen:"
        echo "  $0 --old ${OLD} --apply"
    fi
    exit 0
fi

# --------------------------------------------------------------------------- apply
if [ -n "$PRECOND" ]; then
    echo
    line
    echo "ABGEBROCHEN – es wurde nichts geändert."
    printf '%s\n' "$PRECOND"
    echo "Danach das Werkzeug erneut aufrufen."
    exit 1
fi

CHANGED_APPS=""
if [ "$PLANNED" -gt 0 ]; then
    echo
    echo "4) Sicherung der betroffenen Tabellen nach ${BACKUP_DIR}:"
    for app in $APPS; do
        [ "${APP_STATE[$app]:-}" = ok ] || continue
        [ "$(app_changes "$app")" -gt 0 ] || continue
        for t in $(app_tables "$app"); do
            f="${BACKUP_DIR}/$(app_db "$app")__$(printf '%s' "$t" | tr -d '"').sql"
            # pg_dump -t takes a pattern: the quotes keep "user" exact
            if ! docker exec postgres pg_dump -U "$PG_USER" -d "$(app_db "$app")" --data-only -t "public.${t}" > "$f" 2>"${f}.err"; then
                echo "  FEHLER: Sicherung von $(app_db "$app").${t} fehlgeschlagen – ABGEBROCHEN, es wurde nichts geändert."
                sed -n '1,3s/^/    /p' "${f}.err"
                exit 1
            fi
            rm -f "${f}.err"
        done
        echo "  $(app_label "$app"): gesichert"
    done
    chmod 600 "${BACKUP_DIR}"/*.sql 2>/dev/null || true
    echo "  (Die Sicherung enthält Benutzerdaten inkl. Kennwort-Hashes: nach erfolgreicher Prüfung löschen.)"

    echo
    echo "5) Änderungen ausführen:"
    for app in $APPS; do
        [ "${APP_STATE[$app]:-}" = ok ] || continue
        [ "$(app_changes "$app")" -gt 0 ] || continue
        CHANGED_APPS="${CHANGED_APPS} ${app}"
    done
    reset_counts
    for app in $CHANGED_APPS; do
        run_app "$app" on
        echo "  $(app_label "$app"): $(app_changes "$app") Einträge umgestellt"
    done
    print_cognee
else
    echo
    echo "4) Keine Datenbank-Einträge zu ändern – keine Sicherung nötig."
fi
_changed() { case " ${CHANGED_APPS} " in *" $1 "*) return 0 ;; esac; return 1; }

echo
echo "6) Dienste neu starten und Einrichtung abschließen:"
ACTION_FAIL=""
if _changed authentik; then
    docker exec authentik-server ak shell -c 'from django.core.cache import cache; cache.clear()' >/dev/null 2>&1 \
        && echo "  Authentik: Zwischenspeicher geleert" \
        || echo "  Authentik: Zwischenspeicher nicht geleert (authentik-server antwortet nicht)"
fi
RUNNING="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
if _changed openwebui && _in_list openwebui "$RUNNING"; then
    docker restart openwebui >/dev/null
    if wait_ready openwebui; then echo "  Open WebUI: neu gestartet"
    else echo "  Open WebUI: nach dem Neustart nicht bereit"; ACTION_FAIL="${ACTION_FAIL} openwebui"; fi
fi
if _changed gitea && _in_list gitea "$RUNNING"; then
    docker restart gitea >/dev/null
    if wait_ready gitea; then echo "  Gitea: neu gestartet"
    else echo "  Gitea: nach dem Neustart nicht bereit"; ACTION_FAIL="${ACTION_FAIL} gitea"; fi
fi
SEED="$(seed_state)"
if [ "$SEED" != absent ] && { _changed openwebui || [ "$SEED" = stale ] || [ "$SEED" = failed ]; }; then
    if seed_rerun; then echo "  Chat-Einrichtungsdienst (openwebui-seed): erfolgreich durchgelaufen"
    else echo "  Chat-Einrichtungsdienst (openwebui-seed): FEHLGESCHLAGEN (docker logs --tail 30 openwebui-seed)"; ACTION_FAIL="${ACTION_FAIL} openwebui-seed"; fi
elif [ "$SEED" = ok ]; then
    echo "  Chat-Einrichtungsdienst (openwebui-seed): bereits in Ordnung"
fi
read -r OA OP OM < <(outpost_check)
if [ -n "$CHANGED_APPS" ] || [ "$OM" != 0 ] || [ "$OA" != "$OP" ]; then
    if outpost_reattach; then echo "  Authentik: Anwendungen mit dem Outpost verbunden"
    else echo "  Authentik: Verbinden mit dem Outpost FEHLGESCHLAGEN"; ACTION_FAIL="${ACTION_FAIL} outpost"; fi
    read -r OA OP OM < <(outpost_check)
fi

# --------------------------------------------------------------------------- verify
echo
echo "7) Kontrolle:"
reset_counts
pass off
print_table "offen"
LEFT="$(total AFTER)"
if [ "$OA" = "?" ]; then echo "  Authentik-Outpost: nicht prüfbar"
else echo "  Authentik-Outpost: ${OA} von ${OP} verbunden, fehlend: ${OM}"; fi
SEED="$(seed_state)"
echo "  Chat-Einrichtungsdienst: ${SEED}"
STALE="$(stale_containers)"
stale_split "$STALE"

echo
line
problems=""
for app in $APPS; do
    case "${APP_STATE[$app]:-}" in
        schema|error) problems="${problems}  - $(app_label "$app") konnte nicht bearbeitet werden (siehe oben).${_nl}" ;;
    esac
done
[ "$LEFT" -gt 0 ] && problems="${problems}  - ${LEFT} Einträge nennen noch ${OLD} (Spalte \"offen\").${_nl}"
[ -n "$STOP_LINES" ] && problems="${problems}  - Cognee: zwei Konten mit Daten – nicht automatisch lösbar.${_nl}"
{ [ "$OA" = "?" ] || [ "$OM" != 0 ] || [ "$OA" != "$OP" ]; } && problems="${problems}  - Authentik: nicht alle Anwendungen sind mit dem Outpost verbunden.${_nl}"
case "$SEED" in ok|absent) ;; *) problems="${problems}  - Der Chat-Einrichtungsdienst (openwebui-seed) ist nicht erfolgreich gelaufen.${_nl}" ;; esac
[ -n "$STALE_COMPOSE" ] && problems="${problems}  - Diese Dienste laufen noch mit ${OLD}: ${STALE_COMPOSE}.${_nl}"
[ -n "$ACTION_FAIL" ] && problems="${problems}  - Nicht abgeschlossen:${ACTION_FAIL}.${_nl}"

if [ -z "$problems" ]; then
    echo "ERGEBNIS: OK – alle geprüften Einträge verwenden jetzt ${NEW}."
    [ -n "$CHANGED_APPS" ] && echo "Sicherung und Protokoll: ${BACKUP_DIR}"
    [ -z "$CHANGED_APPS" ] && echo "Es war nichts mehr zu ändern. Protokoll: ${BACKUP_DIR}/fixup.log"
    echo "Weiter mit Schritt 9 der Anleitung (Abschlussprüfung)."
    exit 0
fi
echo "ERGEBNIS: NICHT OK"
printf '%s' "$problems"
echo
echo "Nächster Schritt:"
echo "  - Das Werkzeug kann gefahrlos noch einmal mit --apply aufgerufen werden; bereits"
echo "    Umgestelltes bleibt unverändert."
echo "  - Bleibt das Ergebnis NICHT OK: Protokollpaket erstellen und mit der Datei"
echo "    ${BACKUP_DIR}/fixup.log an Ihren razzfazz.ai-Ansprechpartner senden:"
echo "      rzfz setup --logs-take \"Domainwechsel\""
echo "Sicherung der geänderten Tabellen: ${BACKUP_DIR}"
exit 1
