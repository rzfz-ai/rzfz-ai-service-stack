#!/bin/sh
# ==============================================================================
# Caddy Entrypoint - Derives TLS_DIRECTIVE from TLS_MODE
# ==============================================================================
# This script ensures TLS_DIRECTIVE is always correctly set based on TLS_MODE,
# even if TLS_DIRECTIVE is missing or empty in .env. It also derives
# ACME_EMAIL_DIRECTIVE so an empty LETSENCRYPT_EMAIL never crash-loops Caddy
# under non-ACME TLS modes (#60).
#
# TLS_MODE values:
#   "internal"    → TLS_DIRECTIVE="tls internal"           (self-signed certificates)
#   "certificate" → TLS_DIRECTIVE="tls /certs/cert.pem ..." (custom wildcard certificate)
#   "" (empty)    → TLS_DIRECTIVE=""                        (Let's Encrypt / ACME)
#
# ACME_EMAIL_DIRECTIVE (injected into the Caddyfile global block):
#   ACME + non-empty LETSENCRYPT_EMAIL → "email <addr>"
#   otherwise (certificate/internal, or ACME w/o email) → "" (harmless)
# ==============================================================================

# ── Caddy hygiene: stale ACME lock cleanup (B-9 / M031-FOLLOWUPS C1) ─────────
# Caddy holds an `issue_cert_<host>.lock` while it's issuing or renewing a
# cert. If Caddy is killed mid-issuance (container restart, OOM, host reboot,
# upstream ACME timeout), the lock file survives and blocks every subsequent
# attempt for that hostname until manually removed. Manual fix used to be
# `rm -f /data/caddy/locks/issue_cert_*.lock` + `compose restart caddy` —
# documented in memory `project_caddy_stale_acme_lock_blocks_renewal.md`
# and BLIND-SPOTS B-9. Now automated here.
#
# Safety:
#   - This runs at container START, BEFORE `exec caddy run`. There is no
#     live Caddy holding a real lock at this point, so we cannot race.
#   - We only consider locks OLDER than CADDY_ACME_LOCK_MAX_AGE_SECONDS
#     (default 3600 = 1 h). A genuine in-flight issuance never lives that
#     long; anything older is by definition orphaned from a prior crash.
#   - We only touch files matching `issue_cert_*.lock`. Other files in
#     the locks dir (if Caddy ever adds new lock kinds) are untouched.
#
# Test hooks:
#   - CADDY_LOCKS_DIR overrides /data/caddy/locks (used by test harness).
#   - CADDY_CERTS_DIR overrides /data/caddy/certificates/local.
#   - CADDY_DRY_RUN_NO_EXEC=1 makes the script return instead of exec'ing
#     caddy — used so the cleanup logic can be exercised under pytest
#     without needing the caddy binary.
CADDY_LOCKS_DIR="${CADDY_LOCKS_DIR:-/data/caddy/locks}"
CADDY_CERTS_DIR="${CADDY_CERTS_DIR:-/data/caddy/certificates/local}"
CADDY_ACME_LOCK_MAX_AGE_SECONDS="${CADDY_ACME_LOCK_MAX_AGE_SECONDS:-3600}"

if [ -d "$CADDY_LOCKS_DIR" ]; then
    # Build the candidate list ourselves so we can age-filter portably.
    # `find -mmin` would round to whole minutes; we want second-granularity
    # so test_max_age_env_var_honored (10 s threshold) works deterministically.
    now=$(date +%s)
    removed=0
    kept_fresh=0
    for lock in "$CADDY_LOCKS_DIR"/issue_cert_*.lock; do
        [ -e "$lock" ] || continue   # no matches → glob stays literal
        # Get mtime in epoch seconds; portable across BusyBox + GNU stat.
        lock_mtime=$(stat -c %Y "$lock" 2>/dev/null || stat -f %m "$lock" 2>/dev/null || echo 0)
        age=$((now - lock_mtime))
        if [ "$age" -ge "$CADDY_ACME_LOCK_MAX_AGE_SECONDS" ]; then
            echo "[entrypoint] removing stale ACME lock $(basename "$lock") (age ${age}s ≥ ${CADDY_ACME_LOCK_MAX_AGE_SECONDS}s)"
            rm -f "$lock"
            removed=$((removed + 1))
        else
            kept_fresh=$((kept_fresh + 1))
        fi
    done
    if [ "$removed" -gt 0 ]; then
        echo "[entrypoint] ACME lock cleanup: removed $removed stale lock(s); kept $kept_fresh fresh"
    elif [ "$kept_fresh" -gt 0 ]; then
        echo "[entrypoint] ACME lock cleanup: no stale locks found ($kept_fresh fresh lock(s) preserved)"
    else
        echo "[entrypoint] ACME lock cleanup: no stale locks found (locks dir empty)"
    fi
else
    # Locks dir absent on a fresh install — nothing to clean.
    :
fi

# C2: a per-host cert dir with .crt but no .key is a half-issued state from
# an interrupted issuance. Caddy's renewal logic looks for the missing key
# and loops forever. Wipe such dirs so on-demand TLS re-issues cleanly.
if [ -d "$CADDY_CERTS_DIR" ]; then
    for d in "$CADDY_CERTS_DIR"/*/; do
        [ -d "$d" ] || continue
        crt=$(ls "$d"*.crt 2>/dev/null | head -1)
        key=$(ls "$d"*.key 2>/dev/null | head -1)
        if [ -n "$crt" ] && [ -z "$key" ]; then
            echo "[entrypoint] cert dir $(basename "$d") has no .key — wiping for re-issuance"
            rm -rf "$d"
        fi
    done
fi

# Internal (self-signed) certs from Caddy's local CA have a FIXED ~12h leaf
# lifetime — the `lifetime` knob on the internal issuer parses but does NOT
# extend the leaf (verified on 0.91: cert stays 12h). That's fine as long as the
# host clock is correct: Caddy renews at ~6h, no gap. The recurring breakage on
# VMs (Multipass) where start.<domain> shows a cert error — mis-read as "lost
# the outpost" — is a CLOCK JUMP on host suspend/resume making the 12h cert look
# expired. The fix lives in scripts/harden-host.sh (chrony `makestep 1 -1`,
# step-corrects the clock), NOT here. Keep this directive plain.
case "$TLS_MODE" in
    internal|selfsigned|self-signed)
        export TLS_DIRECTIVE="tls internal"
        echo "[entrypoint] TLS_MODE=$TLS_MODE → Using self-signed certificates (tls internal)"
        ;;
    certificate|custom)
        CERT_FILE="${TLS_CERT_PATH:-/certs/cert.pem}"
        KEY_FILE="${TLS_KEY_PATH:-/certs/key.pem}"
        if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
            echo "[entrypoint] ERROR: TLS_MODE=certificate but certificate files not found!"
            echo "[entrypoint]   Expected cert: $CERT_FILE"
            echo "[entrypoint]   Expected key:  $KEY_FILE"
            echo "[entrypoint]   Place your wildcard certificate files in ./certs/ on the host."
            echo "[entrypoint]   Falling back to self-signed certificates."
            export TLS_DIRECTIVE="tls internal"
        else
            export TLS_DIRECTIVE="tls $CERT_FILE $KEY_FILE"
            echo "[entrypoint] TLS_MODE=certificate → Using custom certificate ($CERT_FILE)"
        fi
        ;;
    *)
        # If TLS_MODE is empty/unset → Let's Encrypt (ACME)
        # Force TLS_DIRECTIVE empty regardless of what env_file set
        export TLS_DIRECTIVE=""
        if [ -z "$TLS_MODE" ]; then
            echo "[entrypoint] TLS_MODE is empty → Using Let's Encrypt (ACME)"
        else
            echo "[entrypoint] TLS_MODE=$TLS_MODE (unknown) → Defaulting to Let's Encrypt (ACME)"
        fi
        ;;
esac

# ── #60: derive ACME_EMAIL_DIRECTIVE from TLS_MODE + LETSENCRYPT_EMAIL ───────
# The Caddyfile global block injects `{$ACME_EMAIL_DIRECTIVE:}` instead of a
# hard-coded `email {$LETSENCRYPT_EMAIL}`. That old form expanded to a bare
# `email` (no argument) whenever LETSENCRYPT_EMAIL was empty — which is the
# LEGITIMATE case under TLS_MODE=certificate (operator-provided signed cert)
# and TLS_MODE=internal (self-signed), where ACME/Let's-Encrypt isn't used.
# A bare `email` fails Caddyfile adaptation ("wrong argument count") and Caddy
# crash-loops. We now only emit the `email <addr>` line when ACME is actually
# in use (TLS_MODE empty/unknown → TLS_DIRECTIVE empty) AND the address is
# non-empty; otherwise the directive is empty and harmless.
#
# Mirrors the existing TLS_DIRECTIVE env-injection pattern.
_le_email="${LETSENCRYPT_EMAIL:-}"
# Trim surrounding whitespace so a whitespace-only value counts as empty.
_le_email="$(printf '%s' "$_le_email" | tr -d '[:space:]')"
if [ -z "$TLS_DIRECTIVE" ] && [ -n "$_le_email" ]; then
    # ACME path with a real address → set the account email.
    export ACME_EMAIL_DIRECTIVE="email $_le_email"
    echo "[entrypoint] ACME account email set (LETSENCRYPT_EMAIL=$_le_email)"
else
    # Non-ACME (certificate/internal) OR ACME with no email → no directive.
    export ACME_EMAIL_DIRECTIVE=""
    if [ -z "$TLS_DIRECTIVE" ]; then
        echo "[entrypoint] ACME in use but LETSENCRYPT_EMAIL empty → no account email (zero-config ACME)"
    else
        echo "[entrypoint] Non-ACME TLS → LETSENCRYPT_EMAIL ignored (no email directive)"
    fi
fi

if [ -n "${CADDY_DRY_RUN_NO_EXEC:-}" ]; then
    echo "[entrypoint] CADDY_DRY_RUN_NO_EXEC set — skipping caddy exec (test mode)"
    exit 0
fi

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
