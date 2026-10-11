#!/bin/sh
# ==============================================================================
# razzfazz-backup — re-reads BACKUP_ENCRYPTION_PASSWORD from .env at every
#                   invocation, then exec's offen's `backup` binary
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
# offen's outer-tarball encryption), then `exec`s the real `backup` binary.
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
exec "$BACKUP_BIN" "$@"
