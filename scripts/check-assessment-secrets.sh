#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# check-assessment-secrets.sh [FILE…] — refuse a security assessment document
# that carries a live secret value (#2275, found while triaging #2274).
#
# security-run/razzfazz-ai-box-security-assessment-*.md is committed by design
# (.gitignore re-includes it) and NOTHING scanned it before the commit: the only
# secret gate is publish-public.sh HARD GATE 3, which runs on the public export
# (where security-run/ is stripped anyway) with a 24-character floor that a
# `generate_password 24` value can undercut (it strips /+= before `head -c`).
# An assessment naturally quotes config diffs, `docker compose config` output,
# tables and connection URLs — each shape can carry a value out of .env.dify.
#
# Shapes caught (secret-named key + 12+ character non-placeholder value):
#   assignment  KEY=value        KEY: value        "KEY": "value"
#   table       | KEY | value |
#   url         scheme://user:value@host
# Not a hit: ${VAR} references, <angle> placeholders, a value without both a letter
# and a digit (POSTGRES_PASSWORD as a word, a numeric id), REDACTED / CHANGEME /
# example / placeholder / dummy / fixture / sample values, a 40-hex sha, a
# `sha256:` digest.
#
# Canary control: before scanning, every shape is run against a synthetic line
# that MUST match. If one does not, exit 2 — an instrument that agrees with
# reality only while reality is zero is not an instrument.
#
# Exit 0 clean, 1 hit(s) (values masked in the output), 2 instrument blind,
# 3 usage. With no FILE argument scans every committed assessment document.
set -euo pipefail

KEYS='(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|ADMIN_TOKEN|ENCRYPTION_KEY)[A-Za-z0-9_]*'
# a value: 12+ chars, and BOTH a letter and a digit (a bare word such as
# POSTGRES_PASSWORD or a numeric id is not a secret) — PCRE lookaheads, hence grep -P.
VAL='(?=[A-Za-z0-9+/_.!@#%^&*-]*[0-9])(?=[A-Za-z0-9+/_.!@#%^&*-]*[A-Za-z])[A-Za-z0-9+/_.!@#%^&*-]{12,}'
# assignment: KEY=v, KEY: v, "KEY": "v", `KEY`=v   (optional quotes/backticks around either side)
RE_ASSIGN="${KEYS}[\"'\`]?[[:space:]]*[=:][[:space:]]*[\"'\`]?${VAL}"
# table: | KEY | v |
RE_TABLE="\|[[:space:]]*[\"'\`]?[A-Za-z0-9_]*${KEYS}[\"'\`]?[[:space:]]*\|[[:space:]]*[\"'\`]?${VAL}[\"'\`]?[[:space:]]*\|"
# url: scheme://user:v@host
RE_URL="[a-z][a-z0-9+.-]*://[A-Za-z0-9_.-]+:${VAL}@"
# what is never a live value
RE_NOT='\$\{|\$[A-Z_]|<[A-Za-z_ -]+>|REDACTED|redacted|CHANGEME|changeme|example|EXAMPLE|placeholder|PLACEHOLDER|your-|dummy|fixture|sample|0123456789abcdef|secretvalue|secretkey|clientsecret|sha256:|[0-9a-f]{40}|\.\.\.|…'

_scan() {  # _scan FILE-OR-- : prints masked hits, returns 0 when hits exist
    grep -nP "${RE_ASSIGN}|${RE_TABLE}|${RE_URL}" "$1" 2>/dev/null | grep -vE "${RE_NOT}" \
      | sed -E 's#(([^0-9]:|[=|@])["'"'"'`[:space:]]*[A-Za-z0-9+/_.!@%^*-]{4})[A-Za-z0-9+/_.!@#%^&*-]{8,}#\1…#g'

}

# ── canary control: the instrument must see each shape ──────────────────────
# The canary values are kept UNDER 24 characters on purpose: the public-export gate
# (scripts/publish-public.sh) flags secret-shaped values of 24+ characters in shipped
# source, and this file ships. The floor here is 12, so the control still sees them.
canary=$(mktemp); trap 'rm -f "$canary"' EXIT
printf '%s\n' \
  'PGVECTOR_PASSWORD=BogusCanary0ne78Value' \
  '"SECRET_KEY": "BogusCanaryValueOne2"' \
  '| API_KEY | BogusCanaryValueTwo34 |' \
  'postgresql://docker:BogusCanaryValue56@postgres:5432/dify' > "$canary"
seen=$(_scan "$canary" | wc -l)
if [ "$seen" -ne 4 ]; then
    echo "check-assessment-secrets: INSTRUMENT BLIND — canary matched ${seen}/4 shapes; refusing to report clean" >&2
    exit 2
fi

# ── the scan ────────────────────────────────────────────────────────────────
if [ $# -eq 0 ]; then
    mapfile -t files < <(git ls-files 'security-run/razzfazz-ai-box-security-assessment-*.md' 2>/dev/null)
else
    files=("$@")
fi
[ "${#files[@]}" -gt 0 ] || { echo "check-assessment-secrets: no files to scan" >&2; exit 3; }

rc=0
for f in "${files[@]}"; do
    [ -f "$f" ] || { echo "check-assessment-secrets: not a file: $f" >&2; rc=3; continue; }
    hits=$(_scan "$f" || true)
    if [ -n "$hits" ]; then
        echo "check-assessment-secrets: BLOCK — $f carries what looks like a live secret value:"
        printf '%s\n' "$hits" | head -10 | sed 's/^/    /'
        rc=1
    fi
done
[ "$rc" -eq 0 ] && echo "check-assessment-secrets: clean (${#files[@]} file(s), 4/4 canary shapes seen)"
exit "$rc"
