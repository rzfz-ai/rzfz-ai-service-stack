#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# cli/set-password.sh  (#54 — central password broker backend)
# ==============================================================================
# Set the password for a NAMED account (by email) in one local-auth backend.
# This is the non-interactive, machine-callable entry point that the password
# broker (core/start-portal/password_broker.py) invokes through the
# docker-socket-proxy. It is also usable by hand by an operator.
#
# Usage:
#   echo -n "<plaintext>" | rzfz set-password --app <authentik|dify|cognee> --email <addr>
#
# The plaintext password is read from STDIN ONLY — never passed as an argument
# (argv leaks via /proc, ps, history). Nothing is echoed or logged.
#
# Exit: 0 ok · 1 failure · 2 target container not running (skipped).
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"
# shellcheck source=cli/lib-set-password.sh
source "${SCRIPT_DIR}/cli/lib-set-password.sh"

APP="" EMAIL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --app)   APP="${2:?--app needs a value}"; shift 2 ;;
        --email) EMAIL="${2:?--email needs a value}"; shift 2 ;;
        -h|--help)
            sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) print_error "Unknown option: $1"; exit 1 ;;
    esac
done

[ -n "$APP" ]   || { print_error "set-password: --app required"; exit 1; }
[ -n "$EMAIL" ] || { print_error "set-password: --email required"; exit 1; }

# Read the plaintext from stdin (raw — preserve any interior chars; no -n trim
# of intentional content). We strip a single trailing newline only.
PW="$(cat)"
PW="${PW%$'\n'}"
[ -n "$PW" ] || { print_error "set-password: empty password on stdin"; exit 1; }

# Fan into the one shared implementation (password on its stdin).
printf '%s' "$PW" | rzfz_set_password_for_app "$APP" "$EMAIL"
rc=$?
exit $rc
