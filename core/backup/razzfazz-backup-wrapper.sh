#!/bin/sh
# ==============================================================================
# razzfazz-backup — re-reads BACKUP_ENCRYPTION_PASSWORD from .env at every
#                   invocation, then runs offen's `backup` and prints the
#                   pre-backup hook's report (#2549)
# ==============================================================================
# BSB-04 / R-DEF-04: pre-fix, the backup-service container's env was frozen
# at container start. After rotating BACKUP_ENCRYPTION_PASSWORD in .env, the
# operator had to `compose up -d --force-recreate backup-service` for the
# new value to take effect — silent stale-passphrase trap if forgotten.
#
# This wrapper, installed in the image at /usr/local/bin/razzfazz-backup,
# is invoked by:
#   * razzfazz-backup.sh  (CLI: `razzfazz-backup.sh backup`)
#   * BackupManager.trigger_full_backup()  (UI button)
#
# It reads BACKUP_ENCRYPTION_PASSWORD from /scripts/dot-env (the host .env,
# bind-mounted by core/compose.yml `../.env:/scripts/dot-env:ro`),
# validates the format, exports it as both BACKUP_ENCRYPTION_PASSWORD (for
# pre-backup.sh's openssl-encrypt-the-.env step) and GPG_PASSPHRASE (for
# offen's outer-tarball encryption), then runs the real `backup` binary and reports (#2549).
#
# Fail-closed semantics: empty passphrase → exit 2 with an actionable
# message. NEVER silently invoke `backup` with an empty/missing passphrase
# (would write a plain-text archive — F-A2 regression).
#
# Residual gap: scheduled backups via offen's internal cron (BACKUP_CRON_
# EXPRESSION, default `0 3 * * *`) STILL use the daemon's frozen env,
# because the cron tick fires inside the same Go process. Tracked in
# BSB-04-decisions: the right fix is to disable offen's internal cron and
# add a host-side cron that calls `docker exec backup-service razzfazz-
# backup`. That's a follow-up — see BSB-04-DEC-02.
# ==============================================================================
set -eu

# Override-points (used by the unit test harness; in production these stay
# at the defaults baked into core/backup/Dockerfile + core/compose.yml).
ENV_FILE="${RAZZFAZZ_BACKUP_ENV_FILE:-/scripts/dot-env}"
BACKUP_BIN="${RAZZFAZZ_BACKUP_BIN:-/usr/bin/backup}"

err() { printf 'razzfazz-backup: %s\n' "$*" >&2; }

# --- 1. Validate .env presence -----------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
    err "ERROR: ${ENV_FILE} not found."
    err "Cannot read BACKUP_ENCRYPTION_PASSWORD without the bind-mounted .env."
    err "Check that core/compose.yml binds ../.env:/scripts/dot-env:ro, or that"
    err "the host /stack/.env file exists."
    exit 2
fi

# --- 2. Re-read BACKUP_ENCRYPTION_PASSWORD from .env at every invocation ----
# Mirrors the parsing in `decrypt_backup_to_temp` (backup_manager.py) and
# `read_env_value` (app.py / razzfazz_common.env_utils):
#   * grep first matching `KEY=...` line (operator-edited file may carry
#     duplicates from sloppy edits; first wins matches what `source` would do)
#   * strip trailing ` # comment`
#   * strip surrounding double or single quotes
#   * strip leading/trailing whitespace
#
# Don't `source` the file — operator-edited .env carries spaces and shell
# metachars (memory: feedback_dotenv_no_source.md). Use grep+sed.
RAW_LINE="$(grep -m1 '^BACKUP_ENCRYPTION_PASSWORD=' "$ENV_FILE" 2>/dev/null || true)"
if [ -z "$RAW_LINE" ]; then
    err "ERROR: BACKUP_ENCRYPTION_PASSWORD not present in ${ENV_FILE}."
    err "Set a value with razzfazz-setup.sh or the Backup Settings UI before"
    err "running a backup (or accept that the archive will be unencrypted —"
    err "but this wrapper refuses that path; rebuild with ENCRYPTION_OPTIONAL=1"
    err "if you really want it)."
    exit 2
fi

# Strip the leading `KEY=`.
RAW_VALUE="${RAW_LINE#BACKUP_ENCRYPTION_PASSWORD=}"

# Strip trailing ` # comment` (space-hash, like `value # set 2026-05-15`).
# POSIX-portable: use sed.
STRIPPED="$(printf '%s' "$RAW_VALUE" | sed 's/[[:space:]]\{1,\}#.*$//')"

# Strip surrounding double or single quotes (only if BOTH ends match).
case "$STRIPPED" in
    \"*\")
        case "$STRIPPED" in
            \"*\")  STRIPPED="${STRIPPED#\"}"; STRIPPED="${STRIPPED%\"}" ;;
        esac
        ;;
    \'*\')
        STRIPPED="${STRIPPED#\'}"; STRIPPED="${STRIPPED%\'}"
        ;;
esac

# Strip leading/trailing whitespace.
PASSPHRASE="$(printf '%s' "$STRIPPED" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

# --- 3. Validate non-empty + format -----------------------------------------
if [ -z "$PASSPHRASE" ]; then
    err "ERROR: BACKUP_ENCRYPTION_PASSWORD is empty in ${ENV_FILE}."
    err "Refusing to invoke backup without a passphrase — would write an"
    err "unencrypted archive (F-A2 regression)."
    err "Set a value with: rzfz setup  or via the Backup Settings UI."
    exit 2
fi

# Light format guard: reject passphrases that contain embedded newlines
# (these would corrupt the env var passed to the spawned `backup`
# process). NUL bytes can't appear in shell variables anyway. Use a
# literal newline in a here-string to test — POSIX `case` with `$(printf
# '\n')` collapses the newline via command substitution so doesn't work.
NL='
'
case "$PASSPHRASE" in
    *"$NL"*)
        err "ERROR: BACKUP_ENCRYPTION_PASSWORD contains a newline; rejected."
        err "Re-set without line breaks via razzfazz-setup.sh."
        exit 2
        ;;
esac

# --- 4. Export and exec ------------------------------------------------------
# Export both names: BACKUP_ENCRYPTION_PASSWORD is consumed by pre-backup.sh
# (the EXEC_PRE_BACKUP hook) for the openssl-encrypted .env tarball;
# GPG_PASSPHRASE is consumed by offen's Go binary for the outer-archive
# symmetric GPG encryption.
export BACKUP_ENCRYPTION_PASSWORD="$PASSPHRASE"
export GPG_PASSPHRASE="$PASSPHRASE"

# Pass through any extra args (offen accepts none today, but keep the
# contract clean for future versions).
# --- 4b. Never archive the hook's staging dir (#2584) --------------------------
# pre-backup.sh writes its files under /backup/databases/.staging (ClickHouse
# its zip under /backup/clickhouse-backup/.staging) and renames them into place
# when complete. core/compose.yml appends this token
# for the scheduled run; a manual run can arrive with BACKUP_EXCLUDE_REGEXP
# replaced (`rzfz backup backup --include-models` passes it empty), so the
# token is added here whenever it is missing.
STAGING_EXCLUDE='^/backup/(databases|clickhouse-backup)/\.staging(/|\z)'
case "${BACKUP_EXCLUDE_REGEXP:-}" in
    *"$STAGING_EXCLUDE"*) ;;
    '') BACKUP_EXCLUDE_REGEXP="$STAGING_EXCLUDE" ;;
    *) BACKUP_EXCLUDE_REGEXP="${BACKUP_EXCLUDE_REGEXP}|${STAGING_EXCLUDE}" ;;
esac
export BACKUP_EXCLUDE_REGEXP

# --- 5. Run, then say what the pre-backup hook could NOT capture (#2549) ------
LOG_DIR="${RZFZ_PRE_BACKUP_LOG_DIR:-/backup/databases}"
STATUS_FILE="$LOG_DIR/pre-backup.status"
LOG_FILE="$LOG_DIR/pre-backup.log"
# A backup asked for now does not wait for running knowledge ingestion
# (knowledge-wait.sh); the scheduled run, which has no wrapper, does.
ON_DEMAND_MARK="${RZFZ_BACKUP_ON_DEMAND_MARK:-/tmp/rzfz-backup-on-demand}"
date +%s > "$ON_DEMAND_MARK" 2>/dev/null || true
trap 'rm -f "$ON_DEMAND_MARK"' EXIT
started=$(date +%s)
rc=0
"$BACKUP_BIN" "$@" || rc=$?

status_val() { sed -n "s/^$1=//p" "$STATUS_FILE" 2>/dev/null | head -n 1; }
fin=$(status_val finished_epoch)
case "$fin" in ''|*[!0-9]*) fin=0 ;; esac
if [ "$fin" -lt "$started" ]; then
    echo "razzfazz-backup: WARNING: the pre-backup hook left no report for this run ($STATUS_FILE) — database dumps and agent snapshots are UNVERIFIED."
    if [ "$rc" -ne 0 ]; then echo "razzfazz-backup: FAILED (exit $rc)."; else echo "razzfazz-backup: DEGRADED — see the WARNING above."; fi
    exit "$rc"
fi
echo "razzfazz-backup: pre-backup hook report:"
grep -E '(^|[[:space:]])(WARNING|ERROR|CRITICAL|SKIP):' "$LOG_FILE" 2>/dev/null | sed 's/^/  /' || true
hook_rc=$(status_val rc); warnings=$(status_val warnings); errors=$(status_val errors)
agent_failures=$(status_val agent_failures)
skipped=$(status_val skipped_volumes); vol_cap=$(status_val vol_cap_mb)
# #3146: every agent volume that holds data but is NOT in this archive, whatever
# the reason (#2709 records <vol>:<reason>:<size>:<cap>). The size cap was the
# only reason that made a backup PARTIAL; a snapshot that died (tar killed at its
# output cap by SIGXFSZ, or any tar error) left the volume just as missing, every
# night, behind a DEGRADED exit 0. A volume that does not exist (missing_volume)
# has nothing to back up and stays DEGRADED.
not_in=$(status_val skipped_detail | tr ',' '\n' | awk -F: 'NF >= 2 && $1 != "" && $2 != "missing_volume"')
echo "razzfazz-backup: hook exit ${hook_rc:-?}, ${errors:-0} error(s), ${warnings:-0} warning(s), ${agent_failures:-0} agent volume(s) NOT in this backup."
if [ "$rc" -ne 0 ]; then
    echo "razzfazz-backup: FAILED (exit $rc)."
elif [ "$(status_val archive_began_early)" = "1" ]; then
    # #2584: offen walked /backup before the hook had finished — the archive
    # exists but may lack what the hook published after that.
    echo "razzfazz-backup: FAILED — the archive step started before the pre-backup step finished; this archive may lack files written after that. Run the backup again."
    exit 1
elif [ -n "$not_in" ] || [ -n "$skipped" ]; then
    # #2549: a volume above the size cap is missing from EVERY nightly — never
    # a quiet exit 0. PARTIAL = 3, the stack's 0/3/1 scheme (as the restore).
    # #3146: the same for every other reason a volume with data is left out.
    n=0
    if [ -n "$not_in" ]; then
        while IFS=: read -r vol reason size cap; do
            n=$((n + 1))
            case "$reason" in
                size_cap)
                    echo "razzfazz-backup: WARN: agent volume ${vol} (${size} MiB) is NOT in this backup: above BACKUP_AGENT_VOL_MAX_MB (${cap:-${vol_cap:-?}} MiB). Raise BACKUP_AGENT_VOL_MAX_MB in .env (the backup volume needs that much free space) or shrink the volume." ;;
                snapshot_failed)
                    echo "razzfazz-backup: WARN: agent volume ${vol} is NOT in this backup: archiving it failed (see its 'failed to snapshot' line above; a tar killed by signal 25 stopped at its output cap, 2 x BACKUP_AGENT_VOL_MAX_MB)." ;;
                *)
                    echo "razzfazz-backup: WARN: agent volume ${vol} is NOT in this backup (${reason}; see its SKIP line above)." ;;
            esac
        done <<NOT_IN
$not_in
NOT_IN
    else
        for pair in $(printf '%s' "$skipped" | tr ',' ' '); do
            n=$((n + 1))
            echo "razzfazz-backup: WARN: agent volume ${pair%%:*} (${pair#*:} MiB) is NOT in this backup: above BACKUP_AGENT_VOL_MAX_MB (${vol_cap:-?} MiB). Raise BACKUP_AGENT_VOL_MAX_MB in .env (the backup volume needs that much free space) or shrink the volume."
        done
    fi
    echo "razzfazz-backup: PARTIAL — the archive was written without ${n} agent volume(s) that hold data (exit 3)."
    exit 3
elif [ "${errors:-0}" -gt 0 ] || [ "${warnings:-0}" -gt 0 ] || [ "${agent_failures:-0}" -gt 0 ]; then
    echo "razzfazz-backup: DEGRADED — the archive was written, but the lines above name what it does not hold (or holds inconsistently)."
else
    echo "razzfazz-backup: OK — the pre-backup hook reported no warning."
fi
exit "$rc"
