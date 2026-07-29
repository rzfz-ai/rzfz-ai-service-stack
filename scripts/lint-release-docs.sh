#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# Lint release documentation under releases/ for common rendering bugs.
#
# Surfaces:
#   - Image paths that 404 on Caddy-served subdomains
#     (/branding/media/... should be /branding/... — see core/Caddy/Caddyfile
#     `branding_static` snippet vs core/config/app/auth.py static_url_path)
#   - Anything else we add as the renderer surface grows.
#
# Exit codes:
#   0  clean
#   1  one or more issues found

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$STACK_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ISSUES=0

# Check 1: asset paths in release docs MUST use the /branding/media/ form.
#
# Release docs are rendered ONLY by the Config portal (config.<domain>,
# core/config/app/blueprints/api.py::release_notes). The Caddyfile deliberately
# does NOT `import branding_static` on the config vhost (see the explicit note
# there) — so on config, Flask's static_url_path=/branding serves files from the
# app's own static/ dir, where the module icons live under media/ (the same
# convention styles.css uses: url('/branding/media/razzfazz.png')). Therefore
# the correct asset URL is /branding/media/<icon>.png; a bare /branding/<icon>.png
# (no media/ subdir) maps to static/<icon>.png, which does not exist → 404.
#
# (An earlier version of this check had the rule INVERTED — it assumed the notes
# render on Caddy-`branding_static`-stripped subdomains and flagged
# /branding/media/. They don't render there; ga.4 shipped /branding/media/
# fleet-wide and the Config portal renders it correctly. ga.5 corrects the check.)
#
# Scope: only real asset references (src=/href="/url( immediately before
# /branding/). Prose mentions of a path, and inline-code (backtick) mentions,
# are skipped so the check stays focused on renderer-targeted URLs.
echo "[lint-release-docs] /branding/ asset paths use the media/ subdir..."
HITS=$(grep -rn '/branding/' releases/ 2>/dev/null \
    | awk -F: '{
        # Strip prefix "file:line:" before checking content.
        line=$0; sub(/^[^:]+:[0-9]+:/, "", line);
        idx = index(line, "/branding/");
        before = substr(line, 1, idx-1);
        # Inside an odd number of backticks → inline code → skip.
        n = gsub(/`/, "&", before);
        if (n % 2 == 1) next;
        # Only real asset refs: the char before /branding/ is a quote or paren.
        prev = substr(line, idx-1, 1);
        if (prev != "\"" && prev != "\x27" && prev != "(") next;
        # Correct form (/branding/media/...) → skip.
        after = substr(line, idx + length("/branding/"));
        if (after ~ /^media\//) next;
        print $0;
    }' \
    || true)
if [ -n "$HITS" ]; then
    echo -e "${RED}[!] /branding/<x> asset path missing the media/ subdir — 404s on the Config portal.${NC}"
    echo "$HITS" | while read -r line; do
        echo "    $line"
    done
    echo -e "${YELLOW}    Fix: use '/branding/media/<icon>.png' (Config Flask static_url_path=/branding serves static/media/).${NC}"
    ISSUES=$((ISSUES + 1))
fi

if [ "$ISSUES" -eq 0 ]; then
    echo -e "${GREEN}[✓] Release docs lint clean.${NC}"
    exit 0
fi

echo -e "${RED}[✗] Release docs lint: $ISSUES issue(s) found.${NC}"
exit 1
