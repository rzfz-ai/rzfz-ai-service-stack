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
# Release docs are rendered ONLY by the Config portal (settings.<domain>,
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

# Check 2: the notes that become the PUBLIC GitHub Release body / ship in the public
# mirror MUST be customer-safe. publish-public.sh feeds the per-tag (then cycle)
# RELEASE_NOTES.md to `gh release create --notes-file`, and the public export strips
# the internal trees (security-run/, .gsd/, .claude/, docs/enterprise/) — so an
# internal-only reference dangles on the public side (this is the v2026.08-ga.12 defect:
# the Security section cited security-run/…md + "the release checklist", neither on
# GitHub). Guard the CURRENT release's notes (from $1, else the VERSION file) so bare
# runs over historical releases don't fail on grandfathered content.
TARGET_VER="${1:-$(cat VERSION 2>/dev/null || true)}"

# #2101 — fail CLOSED. The argument is a VERSION (`2026.09-rc1`), not a path.
# Given `releases/2026.09` this script used to build
# `releases/releases/2026.09/RELEASE_NOTES.md`, find nothing, and print
# "Release docs lint clean" — a whole day of notes commits carried that claim
# (2026-09-13). A check that inspected no file must not report success.
if [ -n "$TARGET_VER" ]; then
    case "$TARGET_VER" in
        */*|.|..)
            echo -e "${RED}[✗] lint-release-docs takes a VERSION (e.g. 2026.09-rc1), not a path: '${TARGET_VER}'.${NC}" >&2
            exit 2 ;;
    esac
    # Enterprise-only markers must be balanced, or the public strip refuses at publish time.
    _eo_files=()
    for _nf in "releases/${TARGET_VER}/RELEASE_NOTES.md" "releases/${TARGET_VER}/WHATS_NEW.md" "releases/${TARGET_VER}/changelog.md"; do
        [ -f "$_nf" ] && _eo_files+=("$_nf")
    done
    if [ "${#_eo_files[@]}" -gt 0 ] && ! python3 "$(dirname "$0")/strip-enterprise-only.py" --check "${_eo_files[@]}" 2>/tmp/lint-eo.$$; then
        echo -e "${RED}[✗] enterprise-only markers unbalanced:${NC}" >&2; sed 's/^/    /' /tmp/lint-eo.$$ >&2; rm -f /tmp/lint-eo.$$; ISSUES=$((ISSUES + 1))
    fi; rm -f /tmp/lint-eo.$$
    if [ ! -f "releases/${TARGET_VER}/RELEASE_NOTES.md" ]; then
        echo -e "${RED}[✗] releases/${TARGET_VER}/RELEASE_NOTES.md does not exist — nothing was linted, so nothing is clean.${NC}" >&2
        exit 2
    fi
fi

# #2101 — a per-tag notes file becomes the release page (Gitea, and GitHub at
# GA): relative links dangle there. Only absolute https:// links and in-page
# anchors survive. The cycle document is read in place and may link relatively.
if [ -n "$TARGET_VER" ] && [ "${TARGET_VER#*-}" != "$TARGET_VER" ]; then
    echo "[lint-release-docs] links in releases/${TARGET_VER}/ are absolute or in-page..."
    REL_HITS=$(grep -noE '\]\([^)#][^)]*\)' "releases/${TARGET_VER}/RELEASE_NOTES.md" "releases/${TARGET_VER}/WHATS_NEW.md" 2>/dev/null \
        | grep -vE '\]\(https?://' || true)
    if [ -n "$REL_HITS" ]; then
        echo -e "${RED}[!] relative link(s) in per-tag notes — they dangle on the release page.${NC}"
        echo "$REL_HITS" | sed 's/^/    /'
        echo -e "${YELLOW}    Fix: name the file as a plain path (public-safe on every surface) or use an absolute https:// link that is public-safe.${NC}"
        ISSUES=$((ISSUES + 1))
    fi
fi

# #2101 — the "no bumps" conflation. Three copies of "No version bumps were
# made" surfaced on 2026-09-13 under lists of nineteen moved pins; each copy
# was written from the previous one. A "no … bump(s)" sentence must qualify
# itself in the same line: further / additional / available / required.
if [ -n "$TARGET_VER" ]; then
    echo "[lint-release-docs] 'no … bumps' sentences are qualified..."
    BUMP_FILES="releases/${TARGET_VER}/RELEASE_NOTES.md"
    [ -f "releases/${TARGET_VER%%-*}/RELEASE_NOTES.md" ] && [ "${TARGET_VER%%-*}" != "$TARGET_VER" ] && BUMP_FILES="$BUMP_FILES releases/${TARGET_VER%%-*}/RELEASE_NOTES.md"
    BUMP_HITS=$(grep -niE '\bno [a-z -]*(bumps?|bumped)\b' $BUMP_FILES 2>/dev/null | grep -viE 'further|additional|available|required|needs?\b' || true)
    if [ -n "$BUMP_HITS" ]; then
        echo -e "${RED}[!] unqualified 'no … bumps' sentence — reads as 'nothing was bumped this cycle'.${NC}"
        echo "$BUMP_HITS" | sed 's/^/    /'
        echo -e "${YELLOW}    Fix: say what is meant in the same sentence — 'no FURTHER/ADDITIONAL bump was AVAILABLE/REQUIRED for …'.${NC}"
        ISSUES=$((ISSUES + 1))
    fi
fi
# #2238 — a checklist's scope CONTROL must be true of the range it vouches for.
# Three rc checklists carried "`cli/post-install.sh` … **154 insertions and 5
# deletions** over the same range" copied forward; for rc7 that file was not
# in the range at all. A control that is inherited is not a control. So every
# bold "**N insertions and M deletion(s)**" claim that names a file in
# RELEASE_CHECKLIST.md is re-measured here with `git diff --numstat` over the
# range the checklist itself declares ("## Scope, measured against `<ref>`")
# up to HEAD — the release tip when this runs inside the cut's acceptance.
# Fails CLOSED: a claim whose file is not in the range, whose numbers differ,
# or whose checklist declares no range, is an issue. Derive the paragraph with
# scripts/release-scope.sh instead of writing it by hand.
CHECKLIST="releases/${TARGET_VER}/RELEASE_CHECKLIST.md"
if [ -n "$TARGET_VER" ] && [ -f "$CHECKLIST" ]; then
    echo "[lint-release-docs] scope controls in ${CHECKLIST} are true of their range..."
    CTRL_RANGE_HEAD="${LINT_RANGE_HEAD:-HEAD}"
    # join wrapped blockquote lines so a claim split over two lines is one string
    FLAT=$(sed -e 's/^> \{0,1\}//' "$CHECKLIST" | tr '\n' ' ')
    PREV_REF=$(printf '%s' "$FLAT" | grep -oE 'measured against `[^`]+`' | head -1 | sed -E 's/.*`([^`]+)`/\1/' || true)
    CLAIMS=$(printf '%s' "$FLAT" | grep -oE '`[^` ]+`[^`*]{0,60}\*\*[0-9]+ insertions? and [0-9]+ deletions?\*\*' || true)
    if [ -n "$CLAIMS" ]; then
        if [ -z "$PREV_REF" ] || ! git rev-parse --verify -q "${PREV_REF}^{commit}" >/dev/null; then
            echo -e "${RED}[!] ${CHECKLIST} claims a control but declares no resolvable range ('## Scope, measured against \`<ref>\`' missing or '${PREV_REF:-<none>}' does not resolve).${NC}"
            ISSUES=$((ISSUES + 1))
        else
            while IFS= read -r CLAIM; do
                [ -n "$CLAIM" ] || continue
                CFILE=$(printf '%s' "$CLAIM" | sed -E 's/^`([^`]+)`.*/\1/')
                CINS=$(printf '%s' "$CLAIM" | sed -E 's/.*\*\*([0-9]+) insertions? and ([0-9]+) deletions?\*\*.*/\1/')
                CDEL=$(printf '%s' "$CLAIM" | sed -E 's/.*\*\*([0-9]+) insertions? and ([0-9]+) deletions?\*\*.*/\2/')
                ACTUAL=$(git diff --numstat "${PREV_REF}..${CTRL_RANGE_HEAD}" -- "$CFILE" | awk '{print $1" "$2}')
                if [ -z "$ACTUAL" ]; then
                    echo -e "${RED}[!] control gone stale: ${CHECKLIST} claims \`${CFILE}\` at ${CINS}/${CDEL} over ${PREV_REF}..${CTRL_RANGE_HEAD}, but the file is NOT in that range — the figure was copied from an earlier cut (#2238).${NC}"
                    ISSUES=$((ISSUES + 1))
                elif [ "$ACTUAL" != "${CINS} ${CDEL}" ]; then
                    echo -e "${RED}[!] control does not match its range: ${CHECKLIST} claims \`${CFILE}\` at ${CINS}/${CDEL}, git diff --numstat ${PREV_REF}..${CTRL_RANGE_HEAD} says ${ACTUAL// //} (#2238).${NC}"
                    ISSUES=$((ISSUES + 1))
                fi
            done <<< "$CLAIMS"
        fi
    fi
fi

echo "[lint-release-docs] public-safety of ${TARGET_VER:-<none>} release notes..."
if [ -n "$TARGET_VER" ]; then
    # The checker must see what the PUBLIC reader sees: the Enterprise-only blocks are
    # stripped at publish time, and their markers are themselves a forbidden pattern
    # (a marker surviving into a public body is a defect). Lint the stripped view.
    _pub_dir="$(mktemp -d)"
    _pub_files=()
    for _nf in "releases/${TARGET_VER}/RELEASE_NOTES.md" "releases/${TARGET_VER%%-*}/RELEASE_NOTES.md"; do
        [ -f "$_nf" ] || continue
        mkdir -p "$_pub_dir/$(dirname "$_nf")"
        python3 "$SCRIPT_DIR/strip-enterprise-only.py" "$_nf" > "$_pub_dir/$_nf" || { echo -e "${RED}[✗] could not strip $_nf${NC}"; ISSUES=$((ISSUES + 1)); }
        _pub_files+=("$_pub_dir/$_nf")
    done
    if ! "$SCRIPT_DIR/check-public-notes-safe.sh" "${_pub_files[@]}"; then
        echo -e "${RED}[!] Release notes reference internal-only artifacts — they would dangle on the public GitHub Release.${NC}"
        echo -e "${YELLOW}    Fix: drop security-run/ .gsd/ .claude/ git.razzfazz.ai M0NN refs; point customers to 'your razzfazz.ai support contact'.${NC}"
        ISSUES=$((ISSUES + 1))
    fi
    rm -rf "$_pub_dir"
else
    echo -e "${YELLOW}    (no VERSION file / arg — skipped; pass a version to lint its notes)${NC}"
fi

if [ "$ISSUES" -eq 0 ]; then
    echo -e "${GREEN}[✓] Release docs lint clean.${NC}"
    exit 0
fi

echo -e "${RED}[✗] Release docs lint: $ISSUES issue(s) found.${NC}"
exit 1
