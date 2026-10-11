#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# ============================================================================
# Final pre-push deliverable-review gate.
# ============================================================================
# publish-public.sh scans the repo TREE for secrets and private IPs, but nothing
# ever reviewed the human-facing DELIVERABLES — the release notes and the GitHub
# Release body. v2026.08-ga.13 shipped a public Release whose Security section
# named internal issue IDs and walked through the exact exploit mechanics of the
# holes it had just patched (forged X-Authentik headers reaching /secrets, a
# docker exec into another user's shell, a CSRF timing side-channel). In a public
# note that is both an internal-detail leak and an attack roadmap for every box
# that has not upgraded yet.
#
# This gate reviews what actually reaches customers and FAILS CLOSED on three
# classes of leak:
#
#   exploit-detail  — SSRF / forged header / docker exec / compare_digest /
#                     internal endpoint paths / "by timing" ...
#   internal-refs   — RFC1918 fleet IPs, .gsd/ .claude/ security-run/ paths,
#                     M0NN milestone IDs
#   credential      — a *_PASSWORD= / *_SECRET= / token literal in a note
#
# It does NOT hard-fail on a bare issue reference like "(#539)" — changelogs cite
# issue numbers legitimately; only IDs paired with the leak classes above matter.
# A gate that cries wolf gets bypassed.
#
# Usage:
#   review-public-deliverables.sh --notes FILE [--notes FILE ...]
#   review-public-deliverables.sh --tree DIR
#   review-public-deliverables.sh --tree DIR --notes RELEASE_BODY.md
#
# Exit: 0 clean · 1 leak(s) found · 2 usage error.

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

NOTES=()
TREE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --notes) NOTES+=("$2"); shift 2 ;;
        --tree)  TREE="$2"; shift 2 ;;
        -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ ${#NOTES[@]} -eq 0 ] && [ -z "$TREE" ]; then
    echo "usage: review-public-deliverables.sh --notes FILE | --tree DIR" >&2
    exit 2
fi

# --- what a customer must never see in a public deliverable -------------------
# High-signal exploit mechanics. Deliberately specific so benign prose ("we
# hardened authentication", "strengthened CSRF protection") does not trip it.
# ("Super Admins" is intentionally NOT here — "razzfazz.ai Super Admins" is a real
# Authentik group name that ships in defaults; X-Authentik already flags the
# forged-header case.)
EXPLOIT_RE='(\bSSRF\b|X-Authentik|docker exec|compare_digest|/secrets/regenerate|break-glass|by timing|prefix length|forged[ -]?(request|header|X-))'
# Internal infrastructure. Full dotted-quad RFC1918 (so "kernel 10.04" is safe),
# internal repo trees, and milestone IDs.
INTERNAL_RE='(192\.168\.[0-9]{1,3}\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|\.gsd/|security-run/|\.claude/|\bM0[0-9]{2}\b)'
# A credential literal. Key must look secret-bearing; value must be a real token
# (a `<placeholder>` starts with an excluded char and does not match).
CRED_RE='(PASSWORD|SECRET|TOKEN|API[_-]?KEY|ADMIN_TOKEN|ENCRYPTION_KEY|BOOTSTRAP_PASSWORD|PRIVATE_KEY)[A-Z0-9_]*[[:space:]]*[:=][[:space:]]*"?[^[:space:]<>${"]{6,}'

FINDINGS="$(mktemp)"; trap 'rm -f "$FINDINGS"' EXIT

scan_text() {
    # $1 = label; reads text to scan on stdin. Appends findings.
    local label="$1" cls re name flags tmp
    tmp=$(mktemp); cat > "$tmp"
    for cls in exploit-detail:"$EXPLOIT_RE" internal-refs:"$INTERNAL_RE" credential:"$CRED_RE"; do
        name="${cls%%:*}"; re="${cls#*:}"
        flags='-nIE'; [ "$name" = credential ] || flags='-nIEi'
        while IFS= read -r hit; do
            [ -n "$hit" ] && printf '%s\t%s\t%s\n' "$name" "$label" "$hit" >> "$FINDINGS"
        done < <(grep $flags "$re" "$tmp" 2>/dev/null)
    done
    rm -f "$tmp"
}

scan_one() {
    # $1 = file, $2 = human label for the file
    local f="$1" label="$2"
    [ -f "$f" ] || return 0
    case "$f" in
        *.json)
            # A migration/manifest JSON mixes customer-prose (notes, comment) with
            # STRUCTURAL fields (version="2026-04.M008", default="razzfazz.ai Super
            # Admins", key names). Only the prose is a customer deliverable; scan
            # exactly that so structural milestone tags / group-name defaults do not
            # false-positive. Non-parseable JSON falls back to a whole-file scan.
            if command -v jq >/dev/null 2>&1 && jq -e . "$f" >/dev/null 2>&1; then
                jq -r '[.. | objects | (.notes?, .comment?, .note?, .description?)
                        | select(type=="string")] | .[]' "$f" 2>/dev/null \
                    | scan_text "$label (notes/comment)"
            else
                scan_text "$label" < "$f"
            fi
            ;;
        *)
            scan_text "$label" < "$f"
            ;;
    esac
}

echo -e "${CYAN}[deliverable-review] reviewing what will ship publicly...${NC}"

# --- explicit notes / release-body files -------------------------------------
for f in "${NOTES[@]:-}"; do
    [ -z "$f" ] && continue
    scan_one "$f" "$f"
done

# --- tree mode: the customer-facing docs that ride along in the export --------
if [ -n "$TREE" ]; then
    while IFS= read -r f; do
        [ -n "$f" ] && scan_one "$f" "${f#"$TREE"/}"
    done < <(find "$TREE" -type f \( \
                -iname 'README*' -o -iname '*RELEASE_NOTES*' -o -iname '*WHATS_NEW*' \
                -o -iname '*changelog*' -o -path '*/config/migrations/env-changes.json' \
             \) 2>/dev/null)
fi

# --- verdict -----------------------------------------------------------------
if [ -s "$FINDINGS" ]; then
    n=$(wc -l < "$FINDINGS")
    echo -e "${RED}[✗] $n leak(s) in customer-facing deliverables — refusing to publish:${NC}"
    # group by class for readability
    for cls in exploit-detail internal-refs credential; do
        rows=$(awk -F'\t' -v c="$cls" '$1==c' "$FINDINGS")
        [ -z "$rows" ] && continue
        case "$cls" in
            exploit-detail) echo -e "${RED}  ▸ exploit mechanics (public attack roadmap):${NC}" ;;
            internal-refs)  echo -e "${RED}  ▸ internal infrastructure references:${NC}" ;;
            credential)     echo -e "${RED}  ▸ credential literals:${NC}" ;;
        esac
        printf '%s\n' "$rows" | awk -F'\t' '{printf "      %s: %s\n", $2, $3}'
    done
    echo -e "${YELLOW}    Rewrite these in customer-meaningful terms (no internal IDs, no exploit${NC}"
    echo -e "${YELLOW}    mechanics, no infra refs, no secrets) before the publish is allowed.${NC}"
    exit 1
fi

echo -e "${GREEN}[✓] deliverable review clean — no internal detail, exploit mechanics or secrets.${NC}"
exit 0
