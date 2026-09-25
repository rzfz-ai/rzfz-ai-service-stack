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

# #266 (E7): a stack-bound verb refuses on a thin inference node instead of
# reading a .env that is not there. Placed AFTER the early --help handler so
# help still renders anywhere, and BEFORE the .env precondition so the
# operator gets "this is a worker" rather than ".env not found".
refuse_on_worker_node security-check "The stack it audits — Caddy, Authentik, the portals — runs on the master."

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

# --- 2b. passwordless sudo grants (#2210) ------------------------------------
# The posture report is the evidence base for the ISO 27001 chapter, and it
# never looked at /etc/sudoers.d. Measured on 0.175 (2026-09-16): a three-day-old
# `99-rc1-tmp` drop-in granting `ALL=(ALL) NOPASSWD:ALL` to the admin account,
# invisible to this report. Nothing the stack ships creates such a file; an
# operator, an imaging step, a support session or a test harness can — and it
# is exactly what an audit expects this tool to surface.
#   FAIL  a NOPASSWD grant whose command list is ALL (a standing root escalation)
#   WARN  a NOPASSWD grant scoped to named commands (a legitimate pattern, listed)
#   WARN  a drop-in whose NAME looks temporary (tmp/temp/test/rcN) whatever it holds
#   ℹ     the files could not be read (no passwordless sudo here) — unverified
# Test hooks: RZFZ_SUDOERS_D, RZFZ_SUDOERS_FILE, RZFZ_SUDOERS_READER (default:
# `sudo -n cat`, falling back to a plain read where the file is readable).
print_substep "Checking passwordless sudo grants (/etc/sudoers, /etc/sudoers.d)..."
sudoers_report=""
_sudoers() { sudoers_report+="- $1"$'\n'; }
_sudoers_read() {
    # $1 = file → its contents, or return 1 when unreadable
    if [ -r "$1" ]; then cat "$1"; return 0; fi
    if [ -n "${RZFZ_SUDOERS_READER:-}" ]; then ${RZFZ_SUDOERS_READER} "$1" 2>/dev/null; return $?; fi
    command -v sudo >/dev/null 2>&1 && sudo -n cat "$1" 2>/dev/null
}
_sudoers_classify() {
    # $1 = file, stdin = contents. Prints one report line per NOPASSWD grant.
    local f="$1" line rule cmds found=0
    while IFS= read -r line; do
        line="${line%%#*}"
        printf '%s' "$line" | grep -q "NOPASSWD" || continue
        found=1
        # the command list is everything after the last "NOPASSWD:"
        cmds="$(printf '%s' "$line" | sed -E 's/.*NOPASSWD:[[:space:]]*//; s/[[:space:]]+$//')"
        rule="$(printf '%s' "$line" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+/ /g')"
        if printf '%s' "$cmds" | grep -qE '(^|,[[:space:]]*)ALL([[:space:]]*,|$)'; then
            _sudoers "❌ FAIL ${f}: passwordless root for every command — \`${rule}\`"
            FAILS=$((FAILS+1))
        else
            _sudoers "⚠ ${f}: passwordless sudo scoped to named commands — \`${rule}\` (confirm it is intended)"
        fi
    done
    return 0
}
SUDOERS_D="${RZFZ_SUDOERS_D:-/etc/sudoers.d}"
SUDOERS_FILE="${RZFZ_SUDOERS_FILE:-/etc/sudoers}"
sudoers_unreadable=0
for f in "$SUDOERS_FILE" "$SUDOERS_D"/*; do
    [ -e "$f" ] || continue
    case "$(basename "$f")" in
        README|*~|*.dpkg-*|.*) continue ;;
    esac
    if [ "$f" != "$SUDOERS_FILE" ] && printf '%s' "$(basename "$f")" | grep -qiE '(^|[^a-z])(tmp|temp|test)([^a-z]|$)|(^|[^a-z0-9])rc[0-9]+([^a-z0-9]|$)'; then
        _sudoers "⚠ ${f}: drop-in with a temporary-looking name — remove it when its purpose is over (#2210)"
    fi
    if content="$(_sudoers_read "$f")"; then
        # a here-string, not a pipe: the classifier increments FAILS and appends
        # to the report, and a pipeline would do both in a subshell that is thrown away
        _sudoers_classify "$f" <<< "$content"
    else
        sudoers_unreadable=$((sudoers_unreadable+1))
    fi
done
if [ "$sudoers_unreadable" -gt 0 ]; then
    # #2210 box acceptance (0.91): the FAIL case is self-enabling — a blanket
    # NOPASSWD:ALL is what lets `sudo -n cat` read the directory — while a box
    # with only a SCOPED grant, or none, cannot read it without root. So the
    # WARN verdict generally needs root, and this line must never read as clean.
    _sudoers "⚠ UNVERIFIED — ${sudoers_unreadable} sudoers file(s) are not readable without root; run as root to check (\`sudo rzfz security-check\`). A blanket NOPASSWD:ALL grant would have been visible; a scoped grant, or none, is only visible as root."
fi
[ -n "$sudoers_report" ] || _sudoers "✅ no passwordless sudo grant found"

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
    echo "## 1b. Passwordless sudo grants"
    echo
    printf '%s\n' "$sudoers_report"
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
