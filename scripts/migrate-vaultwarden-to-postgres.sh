#!/usr/bin/env bash
# ==============================================================================
# migrate-vaultwarden-to-postgres.sh (#226)
# ==============================================================================
# One-time, IDEMPOTENT migration of an existing Vaultwarden SQLite database
# (/data/db.sqlite3 in the vaultwarden-data volume) into the shared PostgreSQL
# VAULTWARDEN_DB, so Vaultwarden stops using its SQLite default and joins the
# stack's one-Postgres-per-service model (and the Postgres backup path).
#
# Called automatically from the upgrade flow (cli/upgrade.sh) and safe to run by
# hand. It does NOTHING unless a migration is actually needed:
#   * vaultwarden profile not enabled            -> skip
#   * no SQLite data (fresh/empty box)           -> skip  (VW just builds a fresh PG schema)
#   * vaultwarden_db already populated           -> skip  (already migrated, e.g. prod hotfix)
# Only when SQLite HAS data AND vaultwarden_db is EMPTY does it migrate.
#
# Recipe (validated on prod 2026-08-01, 3472 ciphers, exact parity):
#   1. stop vaultwarden (freeze writes)
#   2. back up SQLite (in-volume .pre-pg-<ts> + off-volume copy)
#   3. throwaway vaultwarden builds the schema into vaultwarden_db
#   4. pgloader copies data (data only; the ~dup-key errors on the diesel
#      migration-tracking table are benign — do NOT add an `excluding` clause,
#      it throws ESRAP-PARSE-ERROR)
#   5. verify row-count parity; abort (leaving VW on SQLite) if it doesn't match
# The caller recreates vaultwarden on the PG-wired compose afterwards.
#
# Flags: --dry-run (print plan), --force (migrate even if counts already exist),
#        --help. Never destructive to SQLite (it stays as the rollback).
# ==============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RAZZFAZZ_ENV_FILE:-$SCRIPT_DIR/.env}"
DRY_RUN=false
FORCE=false

# print_* helpers from lib.sh if present, else minimal fallbacks.
if [ -f "$SCRIPT_DIR/scripts/lib.sh" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/scripts/lib.sh" 2>/dev/null || true
fi
type print_info  >/dev/null 2>&1 || print_info()  { echo "[INFO]  $*"; }
type print_step  >/dev/null 2>&1 || print_step()  { echo; echo "=== $* ==="; }
type print_ok    >/dev/null 2>&1 || print_ok()    { echo "[OK]    $*"; }
type print_warning >/dev/null 2>&1 || print_warning() { echo "[WARN]  $*"; }
type print_error >/dev/null 2>&1 || print_error() { echo "[ERROR] $*" >&2; }

for a in "$@"; do
    case "$a" in
        --dry-run) DRY_RUN=true ;;
        --force)   FORCE=true ;;
        --help|-h) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) print_error "Unknown option: $a"; exit 1 ;;
    esac
done

# --- targeted .env reads (never source .env — it has spaces/metachars) --------
envget() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\'' '; }
PGUSER="$(envget POSTGRES_USER)";      PGUSER="${PGUSER:-docker}"
PGPW="$(envget POSTGRES_PASSWORD)"
VWDB="$(envget VAULTWARDEN_DB)";       VWDB="${VWDB:-vaultwarden_db}"
PROFILES="$(envget COMPOSE_PROFILES)"

# --- guard 0: vaultwarden profile enabled? ------------------------------------
case ",$PROFILES," in
    *,vaultwarden,*) : ;;
    *) print_info "vaultwarden profile not enabled — nothing to migrate."; exit 0 ;;
esac
if [ -z "$PGPW" ]; then
    print_error "POSTGRES_PASSWORD empty in $ENV_FILE — cannot reach Postgres. Aborting (VW stays on SQLite)."
    exit 2
fi

# --- resolve docker objects ---------------------------------------------------
VOL="$(docker volume ls -q 2>/dev/null | grep -E 'vaultwarden-data$' | head -1)"
NET="$(docker inspect postgres --format '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null | awk '{print $1}')"
NET="${NET:-razzfazz-stack_default}"
if [ -z "$VOL" ]; then
    print_info "No vaultwarden-data volume yet (fresh box) — VW will build a clean PG schema. Nothing to migrate."
    exit 0
fi

psql_vw() { docker exec postgres psql -U "$PGUSER" -d "$VWDB" -tAc "$1" 2>/dev/null; }

# --- guard 1: does SQLite hold real data? -------------------------------------
SQLITE_USERS="$(docker run --rm -v "$VOL":/d -e Q="SELECT count(*) FROM users;" \
    --entrypoint sh dimitri/pgloader:latest -c \
    'command -v sqlite3 >/dev/null 2>&1 && sqlite3 /d/db.sqlite3 "$Q" 2>/dev/null || echo NA' 2>/dev/null)"
# pgloader image has no sqlite3 CLI; fall back to a busybox+python probe.
if [ "$SQLITE_USERS" = "NA" ] || [ -z "$SQLITE_USERS" ]; then
    SQLITE_USERS="$(docker run --rm -v "$VOL":/d python:3.12-slim python -c \
        'import sqlite3,sys
try:
  print(sqlite3.connect("/d/db.sqlite3").execute("select count(*) from users").fetchone()[0])
except Exception: print(0)' 2>/dev/null | tr -dc '0-9')"
fi
SQLITE_USERS="${SQLITE_USERS:-0}"
if [ "$SQLITE_USERS" -eq 0 ] 2>/dev/null; then
    print_info "SQLite has no users (fresh/empty box) — VW will build a clean PG schema. Nothing to migrate."
    exit 0
fi

# --- guard 2: is vaultwarden_db already populated? (idempotent) ---------------
PG_CIPHERS="$(psql_vw "SELECT count(*) FROM ciphers;" | tr -dc '0-9')"
PG_CIPHERS="${PG_CIPHERS:-0}"
if [ "$PG_CIPHERS" -gt 0 ] 2>/dev/null && ! $FORCE; then
    print_ok "vaultwarden_db already populated ($PG_CIPHERS ciphers) — already migrated. Skipping."
    exit 0
fi

print_step "Vaultwarden SQLite -> Postgres migration (SQLite users=$SQLITE_USERS, PG ciphers=$PG_CIPHERS)"
if $DRY_RUN; then
    print_info "[dry-run] would: stop VW; backup SQLite; build schema in $VWDB; pgloader; verify parity."
    exit 0
fi

TS="$(date +%Y%m%d-%H%M%S 2>/dev/null || echo manual)"
BK="$SCRIPT_DIR/backups/vaultwarden-sqlite-$TS"; mkdir -p "$BK" 2>/dev/null

print_step "1/5 stop vaultwarden (freeze writes)"
docker stop vaultwarden >/dev/null 2>&1 && print_ok "stopped" || print_info "vaultwarden not running"

print_step "2/5 back up SQLite (in-volume .pre-pg-$TS + $BK)"
for f in db.sqlite3 db.sqlite3-wal db.sqlite3-shm; do
    docker run --rm -v "$VOL":/d -v "$BK":/b busybox:1.37 sh -c \
        "[ -f /d/$f ] && cp -a /d/$f /b/ && cp -a /d/$f /d/$f.pre-pg-$TS || true" 2>/dev/null
done
print_ok "backup done"

print_step "3/5 build schema in $VWDB (throwaway vaultwarden)"
rm -rf /tmp/vwmig 2>/dev/null; mkdir -p /tmp/vwmig/empty
docker run --rm -v "$VOL":/d -v /tmp/vwmig:/c busybox:1.37 cp -a /d/db.sqlite3.pre-pg-$TS /c/db.sqlite3 2>/dev/null
docker rm -f vwmig-schema >/dev/null 2>&1
docker run -d --name vwmig-schema --network "$NET" -v /tmp/vwmig/empty:/data \
    -e DATABASE_URL="postgresql://$PGUSER:$PGPW@postgres/$VWDB" \
    vaultwarden/server:1.36.0 >/dev/null 2>&1
for _ in $(seq 1 20); do
    TBLS="$(psql_vw "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" | tr -dc '0-9')"
    [ "${TBLS:-0}" -ge 20 ] 2>/dev/null && break; sleep 1
done
docker rm -f vwmig-schema >/dev/null 2>&1
if [ "${TBLS:-0}" -lt 20 ] 2>/dev/null; then
    print_error "schema build failed (tables=$TBLS). VW stays on SQLite (restart it). Aborting."
    docker start vaultwarden >/dev/null 2>&1
    exit 4
fi
print_ok "schema built ($TBLS tables)"

print_step "4/5 pgloader data copy"
if ! docker image inspect dimitri/pgloader:latest >/dev/null 2>&1; then
    if ! docker pull dimitri/pgloader:latest >/dev/null 2>&1; then
        print_error "pgloader image missing and cannot pull (offline?). Bundle dimitri/pgloader in the offline package."
        print_error "VW stays on SQLite (restart it). Aborting migration."
        docker start vaultwarden >/dev/null 2>&1
        exit 5
    fi
fi
# truncate data tables (schema kept) so a re-run is clean; keep migration-tracking rows.
psql_vw "DO \$\$ DECLARE r RECORD; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'__diesel_schema_migrations' LOOP
    EXECUTE 'TRUNCATE TABLE public.'||quote_ident(r.tablename)||' CASCADE'; END LOOP; END \$\$;" >/dev/null 2>&1
cat > /tmp/vwmig/migrate.load <<LOAD
load database
     from sqlite:///data/db.sqlite3
     into postgresql://$PGUSER:$PGPW@postgres/$VWDB
with data only, reset sequences, batch rows = 1000, on error resume next
set work_mem to '32MB', maintenance_work_mem to '256 MB'
;
LOAD
docker run --rm --network "$NET" -v /tmp/vwmig:/data dimitri/pgloader:latest pgloader /data/migrate.load 2>&1 \
    | grep -iE 'Total import time' | tail -1
print_ok "pgloader run complete"

print_step "5/5 verify row-count parity"
PG_CIPHERS="$(psql_vw "SELECT count(*) FROM ciphers;" | tr -dc '0-9')"
SQ_CIPHERS="$(docker run --rm -v "$VOL":/d python:3.12-slim python -c \
    'import sqlite3;print(sqlite3.connect("/d/db.sqlite3").execute("select count(*) from ciphers").fetchone()[0])' 2>/dev/null | tr -dc '0-9')"
print_info "ciphers: sqlite=$SQ_CIPHERS  postgres=$PG_CIPHERS"
rm -rf /tmp/vwmig 2>/dev/null
if [ "${PG_CIPHERS:-0}" != "${SQ_CIPHERS:-x}" ] || [ "${PG_CIPHERS:-0}" -eq 0 ] 2>/dev/null; then
    print_error "cipher count mismatch (sqlite=$SQ_CIPHERS pg=$PG_CIPHERS). NOT switching — VW stays on SQLite (restart it)."
    print_error "Investigate before re-running. SQLite backup: $BK + in-volume .pre-pg-$TS"
    docker start vaultwarden >/dev/null 2>&1
    exit 6
fi
print_ok "parity confirmed ($PG_CIPHERS ciphers). Vaultwarden data is now in $VWDB."
print_info "The caller recreates vaultwarden on the PG-wired compose. Rollback: SQLite intact at $BK + in-volume .pre-pg-$TS (remove DATABASE_URL to revert)."
exit 0
