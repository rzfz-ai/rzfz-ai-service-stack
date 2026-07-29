#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# razzfazz-security-check.sh — customer-runnable security posture check
#
# A self-contained security review the CUSTOMER can run on their own box —
# after a self-managed upgrade, or any time a posture snapshot is wanted. It
# is also what rzfz.ai runs on each box post-install to leave an as-built
# assessment on the device.
#
# It ORCHESTRATES the tools already in the stack rather than re-implementing
# them:
#   1. razzfazz-status.sh        — host + stack posture (ufw, secrets, TLS,
#                                   backups, network exposure, …)
#   2. scripts/generate-sbom.sh  — per-image CVE scan + summary (Trivy/Grype)
#   3. signup-closed checks      — Open WebUI / Gitea / Dify (added here)
# … and folds the results into ONE Markdown report under security-run/.
#
# Read-only. No mutations. Degrades gracefully when a scanner or sudo is
# missing (each missing capability is reported, not fatal).
#
# Usage:
#   rzfz security-check                 # full check → Markdown report
#   rzfz security-check --no-cve        # skip the (slow) CVE scan
#   rzfz security-check --out <file.md> # custom report path
#
# Exit codes: 0 = no FAIL, 1 = at least one FAIL/Critical, 2 = usage error.
# =============================================================================
set -o pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh" 2>/dev/null || { echo "FATAL: scripts/lib.sh not found"; exit 2; }

RUN_CVE=1
OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-cve) RUN_CVE=0; shift ;;
        --out) OUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

cd "$SCRIPT_DIR" || exit 2
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DATESTAMP="$(date -u +%Y-%m-%d)"
VERSION="$(cat VERSION 2>/dev/null | tr -d '[:space:]')"
MAIN_DOMAIN="$(read_env_value .env MAIN_DOMAIN)"
mkdir -p security-run
OUT="${OUT:-security-run/razzfazz-ai-box-security-check-${DATESTAMP}.md}"

FAILS=0

print_step "rzfz.ai security check — ${VERSION:-unknown} @ ${NOW}"

# --- 1. posture via razzfazz-status.sh ---------------------------------------
print_substep "Collecting host + stack posture (razzfazz-status.sh)..."
POSTURE="$("${SCRIPT_DIR}/rzfz" status --no-color 2>/dev/null)"
# `grep -c` already prints exactly one integer (0 when nothing matches) and exits
# 1 on no-match; the old `|| echo 0` then appended a SECOND "0", yielding "0\n0"
# and an "arithmetic syntax error" in the $((…)) below (which silently dropped the
# host-posture FAIL count). Capture the count and strip to digits — no `|| echo`.
POSTURE_FAILS="$(printf '%s\n' "$POSTURE" | grep -cE '✗|\[FAIL\]')"
POSTURE_FAILS="${POSTURE_FAILS//[^0-9]/}"
FAILS=$((FAILS + ${POSTURE_FAILS:-0}))

# --- 2. signup-closed checks (Open WebUI / Gitea / Dify) ---------------------
print_substep "Checking public signup is closed (Open WebUI / Gitea / Dify)..."
signup_report=""
_signup() { signup_report+="- $1"$'\n'; }

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx openwebui; then
    # Read the EFFECTIVE value from the RUNNING container, not .env. The compose
    # sets `ENABLE_SIGNUP=${ENABLE_SIGNUP:-false}` (secure default), so the value
    # OWUI actually enforces lives in the container env while .env is normally
    # unset — reading .env here produced a false 'unset' ❌ even on a correctly
    # signup-closed box (mirrors how the Gitea check reads the live app.ini).
    owui_signup="$(docker exec openwebui printenv ENABLE_SIGNUP 2>/dev/null | tr -d ' \r\n')"
    if [ "$owui_signup" = "false" ]; then _signup "✅ Open WebUI: ENABLE_SIGNUP=false"
    else _signup "❌ Open WebUI: ENABLE_SIGNUP='${owui_signup:-unset}' (should be false)"; FAILS=$((FAILS+1)); fi
else _signup "— Open WebUI: not running (chat profile off)"; fi

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gitea; then
    gitea_reg="$(docker exec gitea sh -lc 'grep -i "DISABLE_REGISTRATION" /data/gitea/conf/app.ini 2>/dev/null || grep -i "DISABLE_REGISTRATION" /etc/gitea/app.ini 2>/dev/null' 2>/dev/null | tr -d ' \r')"
    if printf '%s' "$gitea_reg" | grep -qiE "DISABLE_REGISTRATION=true"; then _signup "✅ Gitea: DISABLE_REGISTRATION = true"
    else _signup "⚠ Gitea: DISABLE_REGISTRATION not confirmed true (found: '${gitea_reg:-none}') — verify in app.ini"; fi
else _signup "— Gitea: not running (gitea profile off)"; fi

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx dify-api; then
    _signup "ℹ Dify: verify Console → Settings → Members is invite-only (no API black-box assertion shipped)"
else _signup "— Dify: not running (dify profile off)"; fi

# --- 3. CVE scan via generate-sbom.sh ----------------------------------------
cve_summary_path=""
if [ "$RUN_CVE" -eq 1 ]; then
    if [ -x scripts/generate-sbom.sh ]; then
        print_substep "Running CVE scan (scripts/generate-sbom.sh ${VERSION})... this can take a while"
        print_info "  Tip: stop the stack first for a complete idle-daemon scan; a live stack may starve the scanner."
        if scripts/generate-sbom.sh "${VERSION:-local}" >/tmp/seccheck-sbom.log 2>&1; then
            cve_summary_path="releases/${VERSION#v}/sbom/cve-summary.md"
            [ -f "$cve_summary_path" ] || cve_summary_path="$(find releases -name cve-summary.md -newer /tmp/seccheck-sbom.log -print 2>/dev/null | head -1)"
        else
            print_warning "  CVE scan reported issues — see /tmp/seccheck-sbom.log (scanner may be missing; install syft+grype binaries)."
        fi
    else
        print_warning "scripts/generate-sbom.sh not present — skipping CVE scan."
    fi
fi

# --- 4. compose the Markdown report ------------------------------------------
{
    echo "# rzfz.ai Box — security check"
    echo
    echo "**Version:** \`${VERSION:-unknown}\`   **System:** \`${MAIN_DOMAIN:-unknown}\`   **Run:** ${NOW}"
    echo
    echo "Self-service posture check (host firewall, secrets, TLS, backups, network"
    echo "exposure, public-signup state, and a per-image CVE scan). Run it after any"
    echo "self-managed upgrade. For the full vendor compliance audit (NIS2 / ISO 27001 /"
    echo "OWASP LLM) see the companion \`razzfazz-ai-box-security-assessment-*.md\`."
    echo
    echo "## 1. Public signup state"
    echo
    printf '%s\n' "$signup_report"
    echo "## 2. Host + stack posture"
    echo
    echo "Summary from \`razzfazz-status.sh\` (full output below). FAIL lines need attention."
    echo
    echo '```'
    printf '%s\n' "$POSTURE"
    echo '```'
    echo
    echo "## 3. Container CVE scan"
    echo
    if [ -n "$cve_summary_path" ] && [ -f "$cve_summary_path" ]; then
        echo "Per-image Critical/High/Medium/Low counts (Syft SBOM + Grype):"
        echo
        cat "$cve_summary_path"
        echo
        echo "_Full per-image artifacts + CVE diff: \`$(dirname "$cve_summary_path")\`._"
    elif [ "$RUN_CVE" -eq 0 ]; then
        echo "_Skipped (\`--no-cve\`)._"
    else
        echo "_CVE scan unavailable — install the \`syft\` + \`grype\` binaries on this host and re-run, or run \`scripts/generate-sbom.sh ${VERSION}\` against an idle daemon (\`docker compose stop\` first)._"
    fi
    echo
    echo "---"
    echo "_Generated by \`razzfazz-security-check.sh\` on ${NOW}. Issued by razzfazz.ai GmbH - Member of SEQIS Group. Questions: your razzfazz.ai support contact._"
} > "$OUT"

print_success "Security check report written: $OUT"
[ "$FAILS" -gt 0 ] && { print_warning "${FAILS} FAIL/Critical item(s) — review the report."; exit 1; }
print_success "No FAIL items."
exit 0
