#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# rzfz auth-recovery <username> — break-glass login link for Authentik (#2551).
#
# Admin MFA is enforced (a dedicated MFA stage configures a missing factor).
# An admin who lost their authenticator signs in once with this single-use
# recovery link, then removes the lost device under Settings → MFA devices and
# enrols a new one. Wraps `ak create_recovery_key`; run on the box as the operator.
# The link is valid RZFZ_RECOVERY_MINUTES minutes (default 60) and grants a session
# as <username>: hand it over out of band and delete unused tokens under
# Admin → Directory → Tokens (intent: recovery).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${SCRIPT_DIR}"
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

usage() { sed -n '5,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac
user="${1:-}"
if ! [[ "$user" =~ ^[A-Za-z0-9._@+-]{1,150}$ ]]; then
    echo "ERROR: give the Authentik username (letters, digits, . _ @ + -)" >&2
    usage >&2
    exit 2
fi
# `ak create_recovery_key <duration> <user>`: the duration is in MINUTES
# (measured on the 2026.8.3 source: timedelta(minutes=duration)), so the
# requested validity goes in directly and the token is never long-lived.
mins="${RZFZ_RECOVERY_MINUTES:-60}"
if ! [[ "$mins" =~ ^[0-9]{1,4}$ ]] || [ "$((10#$mins))" -lt 1 ]; then
    echo "ERROR: RZFZ_RECOVERY_MINUTES must be a number of minutes, 1 or more" >&2
    exit 2
fi
mins=$((10#$mins))
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx authentik-server; then
    echo "ERROR: authentik-server is not running" >&2
    exit 1
fi
# Taken before the call, so the printed expiry is never later than the real one.
exp=$(date -u -d "+${mins} minutes" +%Y-%m-%dT%H:%M:%SZ)
out=$(docker exec authentik-server ak create_recovery_key "$mins" "$user" 2>&1) || {
    printf '%s\n' "$out" >&2
    exit 1
}
path=$(printf '%s\n' "$out" | grep -oE '(https?://[^[:space:]]+)?/recovery/use-token/[^[:space:]]+' | tail -1 || true)
[ -n "$path" ] || { printf 'ERROR: no recovery link in the output:\n%s\n' "$out" >&2; exit 1; }
case "$path" in
    http*) link="$path" ;;
    *) domain=$(read_env_value .env AUTHENTIK_DOMAIN 2>/dev/null || true)
       [ -n "$domain" ] || domain="auth.$(read_env_value .env MAIN_DOMAIN)"
       link="https://${domain}${path}" ;;
esac
printf 'RECOVERY-LINK %s\n' "$link"
printf 'EXPIRES %s\n' "$exp"
echo "Single use. After signing in: Settings → MFA devices → remove the lost device, enrol a new one."
echo "Delete unused recovery tokens: Admin → Directory → Tokens (intent: recovery)."
