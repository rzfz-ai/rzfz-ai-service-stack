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
#   * vaultwarden_db already populated           -> skip  (already migrated, e.g. production hotfix)
# Only when SQLite HAS data AND vaultwarden_db is EMPTY does it migrate.
#
# Recipe (validated on production 2026-08-01, 3472 ciphers, exact parity):
#   1. stop vaultwarden (freeze writes)
#   2. back up SQLite (in-volume .pre-pg-<ts> + off-volume copy)
#   3. throwaway vaultwarden builds the schema into vaultwarden_db
#   4. pgloader copies data (data only; the ~dup-key errors on the diesel
#      migration-tracking table are benign — do NOT add an `excluding` clause,
#      it throws ESRAP-PARSE-ERROR)
#   5. verify row-count parity; abort (leaving VW on SQLite) if it doesn't match
# The caller recreates vaultwarden on the PG-wired compose afterwards.
#
# FAIL CLOSED (#2872): "could not check" is never "0 users". The SQLite probe runs
# a PINNED python image (no network, --pull=never) over the volume; if that image
# is missing, the container errors, or the answer is not a plain number, the
# script exits 7 and nothing is switched or discarded. The images are pinned by
# digest below and carried by the offline package (versions.json runtime_only).
#
# Flags: --dry-run (print plan), --force (migrate even if counts already exist),
#        --preflight (read-only: only prove the check CAN run and that the
#        images a migration needs are here; the upgrade runs this BEFORE it
#        recreates Vaultwarden on Postgres), --help.
#        Never destructive to SQLite (it stays as the rollback).
# Exit codes: 0 ok/skip, 2 no Postgres password, 4 schema build failed,
#        5 a required image is missing, 6 parity mismatch, 7 cannot check SQLite.
# ==============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RAZZFAZZ_ENV_FILE:-$SCRIPT_DIR/.env}"
DRY_RUN=false
FORCE=false
PREFLIGHT=false

# Pinned (tag + digest) images; keep in step with config/manifests/versions.json
# (runtime_only entries vaultwarden-probe-python / vaultwarden-pgloader), which is
# how the offline package collects them.
VW_PROBE_IMAGE='python:3.12.14-alpine3.24@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111'
VW_PGLOADER_IMAGE='ghcr.io/dimitri/pgloader:3.6.9@sha256:1acbe4d185ce9a0f0db9d4f72e4f9c70288361eb6269018148e9d95bb0c8cf75'
VW_BUSYBOX_IMAGE='busybox:1.37'

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
        --preflight) PREFLIGHT=true ;;
        --help|-h) sed -n '2,48p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) print_error "Unknown option: $a"; exit 1 ;;
    esac
done

# --- targeted .env reads (never source .env — it has spaces/metachars) --------
envget() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\'' '; }
PGUSER="$(envget POSTGRES_USER)";      PGUSER="${PGUSER:-docker}"
PGPW="$(envget POSTGRES_PASSWORD)"
VWDB="$(envget VAULTWARDEN_DB)";       VWDB="${VWDB:-vaultwarden_db}"
PROFILES="$(envget COMPOSE_PROFILES)"
VWMIG="${RAZZFAZZ_VWMIG_DIR:-/tmp/vwmig}"   # scratch dir for the schema build + pgloader

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

# --- images: pinned, local, never pulled at probe/run time (#2872) -------------
# A digest-pinned ref does not resolve on a box whose images came from a package
# (docker load keeps the tag, not the digest), so the tag form is accepted too.
vw_image() {
    local ref="$1"
    if docker image inspect "$ref" >/dev/null 2>&1; then printf '%s\n' "$ref"; return 0; fi
    if docker image inspect "${ref%@*}" >/dev/null 2>&1; then printf '%s\n' "${ref%@*}"; return 0; fi
    return 1
}
# Present locally, or (online only, one explicit pull of the PINNED ref) fetched.
# Offline never pulls. The probe/migration containers themselves use --pull=never.
vw_ensure_image() {
    local ref="$1"
    vw_image "$ref" >/dev/null && return 0
    if type razzfazz_is_offline >/dev/null 2>&1 && razzfazz_is_offline "$ENV_FILE" 2>/dev/null; then
        return 1
    fi
    timeout 600 docker pull "$ref" >/dev/null 2>&1 || return 1
    vw_image "$ref" >/dev/null
}
# The Vaultwarden image the stack itself pins (the schema builder must match it).
vw_server_image() {
    grep -m1 -E '^[[:space:]]*image:[[:space:]]*vaultwarden/server:[^[:space:]$]+' \
        "$SCRIPT_DIR/modules/apps/vaultwarden/compose.yml" 2>/dev/null | awk '{print $2}' | tr -d '"'
}

# vw_count <table> -> prints the row count of <table> in the SQLite file.
# rc 0 = a real answer ("0" only when the file or the table genuinely does not
# exist); rc 1 = UNKNOWN (image missing, container error, odd output). Never
# collapses unknown into 0.
VW_PROBE_PY='import os, sqlite3, sys
p = "/d/db.sqlite3"
if not os.path.exists(p):
    print("COUNT=0"); sys.exit(0)
try:
    c = sqlite3.connect("file:" + p + "?mode=ro", uri=True)
    try:
        n = c.execute("select count(*) from " + os.environ["TBL"]).fetchone()[0]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            print("COUNT=0"); sys.exit(0)
        raise
    print("COUNT=%d" % n)
except Exception as e:
    print("UNKNOWN=%s" % e); sys.exit(3)'
vw_count() {
    local img out
    img="$(vw_image "$VW_PROBE_IMAGE")" || return 1
    out="$(docker run --rm --pull=never --network none -v "$VOL":/d -e TBL="$1" \
        "$img" python -c "$VW_PROBE_PY" 2>/dev/null)" || return 1
    case "$out" in
        COUNT=[0-9]*) out="${out#COUNT=}"; case "$out" in *[!0-9]*) return 1 ;; esac
                      printf '%s\n' "$out"; return 0 ;;
    esac
    return 1
}

# --- guard 1: is vaultwarden_db already populated? (idempotent) ---------------
# Positive evidence only: a failed/empty psql answer counts as "not populated",
# which falls through to the SQLite check below (the safe direction).
PG_CIPHERS="$(psql_vw "SELECT count(*) FROM ciphers;" | tr -dc '0-9')"
PG_CIPHERS="${PG_CIPHERS:-0}"
if [ "$PG_CIPHERS" -gt 0 ] 2>/dev/null && ! $FORCE; then
    print_ok "vaultwarden_db already populated ($PG_CIPHERS ciphers) — already migrated. Skipping."
    exit 0
fi

# --- guard 2: does SQLite hold real data? (fail closed, #2872) -----------------
cannot_check() {
    print_error "Cannot check whether Vaultwarden's SQLite database holds users: $1"
    print_error "NOT treating that as 'empty'. Nothing was switched to Postgres and the SQLite data is untouched."
    print_error "Next step: make the probe image available, then re-run the upgrade (or scripts/migrate-vaultwarden-to-postgres.sh):"
    print_error "  offline: load a COMPLETE offline package (it carries $VW_PROBE_IMAGE and $VW_PGLOADER_IMAGE)"
    print_error "  online : docker pull $VW_PROBE_IMAGE"
    exit 7
}
if ! vw_ensure_image "$VW_PROBE_IMAGE"; then
    cannot_check "the pinned probe image $VW_PROBE_IMAGE is not on this box and could not be fetched."
fi
if ! SQLITE_USERS="$(vw_count users)"; then
    cannot_check "the probe container failed or gave no usable answer."
fi
if [ "$SQLITE_USERS" -eq 0 ] 2>/dev/null; then
    print_info "SQLite has no users (fresh/empty box) — VW will build a clean PG schema. Nothing to migrate."
    exit 0
fi

# Users exist: a migration WILL be needed. Prove now that everything it needs is
# on this box, so a gap is found before anything is switched, not halfway through.
VW_SERVER_IMAGE="$(vw_server_image)"
_missing=""
for _img in "$VW_PGLOADER_IMAGE" "$VW_BUSYBOX_IMAGE" "$VW_SERVER_IMAGE"; do
    if [ -z "$_img" ] || ! vw_ensure_image "$_img"; then _missing="$_missing ${_img:-<vaultwarden image not found in compose>}"; fi
done
if [ -n "$_missing" ]; then
    print_error "SQLite holds $SQLITE_USERS user(s) that must be migrated, but these images are missing:$_missing"
    print_error "Nothing was switched; the SQLite data is untouched. Load a COMPLETE offline package (or, online, pull them), then re-run the upgrade."
    exit 5
fi
if $PREFLIGHT; then
    print_ok "preflight: SQLite holds $SQLITE_USERS user(s); every image the migration needs is present."
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
    docker run --rm --pull=never --network none -v "$VOL":/d -v "$BK":/b "$VW_BUSYBOX_IMAGE" sh -c \
        "[ -f /d/$f ] && cp -a /d/$f /b/ && cp -a /d/$f /d/$f.pre-pg-$TS || true" 2>/dev/null
done
print_ok "backup done"

print_step "3/5 build schema in $VWDB (throwaway vaultwarden)"
rm -rf "$VWMIG" 2>/dev/null; mkdir -p "$VWMIG"/empty
docker run --rm --pull=never --network none -v "$VOL":/d -v "$VWMIG":/c "$VW_BUSYBOX_IMAGE" cp -a /d/db.sqlite3.pre-pg-$TS /c/db.sqlite3 2>/dev/null
docker rm -f vwmig-schema >/dev/null 2>&1
docker run -d --pull=never --name vwmig-schema --network "$NET" -v "$VWMIG"/empty:/data \
    -e DATABASE_URL="postgresql://$PGUSER:$PGPW@postgres/$VWDB" \
    "$VW_SERVER_IMAGE" >/dev/null 2>&1
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
PGLOADER_REF="$(vw_image "$VW_PGLOADER_IMAGE")" || {
    print_error "pgloader image $VW_PGLOADER_IMAGE is not on this box (offline packages carry it)."
    print_error "VW stays on SQLite (restart it). Aborting migration."
    docker start vaultwarden >/dev/null 2>&1
    exit 5
}
# truncate data tables (schema kept) so a re-run is clean; keep migration-tracking rows.
psql_vw "DO \$\$ DECLARE r RECORD; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'__diesel_schema_migrations' LOOP
    EXECUTE 'TRUNCATE TABLE public.'||quote_ident(r.tablename)||' CASCADE'; END LOOP; END \$\$;" >/dev/null 2>&1
cat > "$VWMIG"/migrate.load <<LOAD
load database
     from sqlite:///data/db.sqlite3
     into postgresql://$PGUSER:$PGPW@postgres/$VWDB
with data only, reset sequences, batch rows = 1000, on error resume next
set work_mem to '32MB', maintenance_work_mem to '256 MB'
;
LOAD
docker run --rm --pull=never --network "$NET" -v "$VWMIG":/data "$PGLOADER_REF" pgloader /data/migrate.load 2>&1 \
    | grep -iE 'Total import time' | tail -1
print_ok "pgloader run complete"

print_step "5/5 verify row-count parity"
PG_CIPHERS="$(psql_vw "SELECT count(*) FROM ciphers;" | tr -dc '0-9')"
SQ_CIPHERS="$(vw_count ciphers)" || SQ_CIPHERS=""
print_info "ciphers: sqlite=$SQ_CIPHERS  postgres=$PG_CIPHERS"
rm -rf "$VWMIG" 2>/dev/null
if [ "${PG_CIPHERS:-0}" != "${SQ_CIPHERS:-x}" ] || [ "${PG_CIPHERS:-0}" -eq 0 ] 2>/dev/null; then
    print_error "cipher count mismatch (sqlite=$SQ_CIPHERS pg=$PG_CIPHERS). NOT switching — VW stays on SQLite (restart it)."
    print_error "Investigate before re-running. SQLite backup: $BK + in-volume .pre-pg-$TS"
    docker start vaultwarden >/dev/null 2>&1
    exit 6
fi
print_ok "parity confirmed ($PG_CIPHERS ciphers). Vaultwarden data is now in $VWDB."
print_info "The caller recreates vaultwarden on the PG-wired compose. Rollback: SQLite intact at $BK + in-volume .pre-pg-$TS (remove DATABASE_URL to revert)."
exit 0
