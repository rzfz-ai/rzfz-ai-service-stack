#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# release-scope.sh PREV_TAG [HEAD] [-C REPO]
#
# Prints the release checklist's scope table and its CONTROLS, derived from
# the range PREV_TAG..HEAD rather than written by hand (#2238).
#
# The scope table's zeros ("no image pin moved", "no migration added") are
# only evidence if a control shows the same diff producing a non-zero number
# on a file that DID change. Three checklists of the 2026.09 cycle carried
# that control copied forward: rc7 vouched for its zeros with a figure that
# was true of rc6's range and untrue of its own (the file was not in the
# range at all). A control nobody has to remember to update cannot go stale,
# so this script reads every number off the range it is asked about:
#
#   - control on the range: the largest product-file diff in PREV..HEAD, with
#     its real insertions/deletions (product = not tests/, releases/, docs/,
#     .gsd/); if the range holds no product file, the largest other file
#     outside releases/, and it says so;
#   - control on each pattern that returned zero: the most recent commit in
#     history that DOES match the pattern, and the pattern's count there, so a
#     zero over the range is shown to come from a live grep.
#
# Output is Markdown, meant to be pasted under "## Scope, measured against".
set -euo pipefail

REPO="."
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -C) REPO="$2"; shift 2 ;;
        *)  ARGS+=("$1"); shift ;;
    esac
done
PREV="${ARGS[0]:-}"
HEAD_REF="${ARGS[1]:-HEAD}"
if [ -z "$PREV" ]; then
    echo "usage: release-scope.sh PREV_TAG [HEAD] [-C REPO]" >&2
    exit 2
fi
g() { git -C "$REPO" "$@"; }
g rev-parse --verify -q "${PREV}^{commit}" >/dev/null || { echo "release-scope: '$PREV' does not resolve" >&2; exit 2; }
g rev-parse --verify -q "${HEAD_REF}^{commit}" >/dev/null || { echo "release-scope: '$HEAD_REF' does not resolve" >&2; exit 2; }
RANGE="${PREV}..${HEAD_REF}"

is_product() { case "$1" in tests/*|releases/*|docs/*|.gsd/*|VERSION) return 1 ;; *) return 0 ;; esac; }

# ---- counts ---------------------------------------------------------------
PRS=$(g log --format=%s --merges "$RANGE" | grep -c "Merge pull request" || true)
COMMITS=$(g rev-list --count --no-merges "$RANGE")
AUTHORS=$(g log --no-merges --format=%an "$RANGE" | sort | uniq -c | sort -rn | awk '{c=$1; $1=""; sub(/^ /,""); printf "%s%d `%s`", (n++?", ":""), c, $0}')
NUMSTAT=$(g diff --numstat "$RANGE")
PRODUCT_ROWS=$(echo "$NUMSTAT" | awk 'NF==3' | while read -r a d f; do is_product "$f" && echo "$a $d $f"; done || true)
PRODUCT_N=$(printf '%s' "$PRODUCT_ROWS" | grep -c . || true)
PIN_FILES="config/manifests/versions.json config/.env.example"
PIN_PATTERNS=('^[+-][A-Z0-9_]*_VERSION=' '^[+-].*image:|^[+-].*_VERSION=')
pattern_count() { # pattern range
    g diff "$2" -- $PIN_FILES $(g ls-tree -r --name-only "$HEAD_REF" | grep -E '^compose.*\.ya?ml$' || true) 2>/dev/null | grep -cE "$1" || true
}
PINS=$(pattern_count "${PIN_PATTERNS[1]}" "$RANGE")
ENV_VER=$(g diff "$RANGE" -- config/.env.example | grep -cE "${PIN_PATTERNS[0]}" || true)
ENV_ANY=$(g diff --numstat "$RANGE" -- config/.env.example | awk '{print "+"$1" / −"$2}')
MIG=$(g diff --numstat "$RANGE" -- config/migrations/env-changes.json | awk '{print "+"$1" / −"$2}')

echo "## Scope, measured against \`${PREV}\` (derived by \`scripts/release-scope.sh ${PREV} ${HEAD_REF}\`)"
echo
echo "| property | measured |"
echo "|---|---|"
echo "| PRs merged | **${PRS}** |"
echo "| commits | **${COMMITS}** non-merge — ${AUTHORS:-none} |"
if [ "$PRODUCT_N" -gt 0 ]; then
    LIST=$(printf '%s\n' "$PRODUCT_ROWS" | sort -rn | awk '{printf "%s`%s` (+%s / −%s)", (n++?", ":""), $3, $1, $2}')
    echo "| product files touched | **${PRODUCT_N}** — ${LIST} |"
else
    echo "| product files touched | **0** — the range holds no file outside \`tests/\`, \`releases/\`, \`docs/\`, \`.gsd/\` |"
fi
echo "| image pins moved | **${PINS}** — \`image:\` / \`*_VERSION=\` lines changed in the manifests, \`.env.example\` and compose files |"
echo "| \`.env.example\` \`*_VERSION\` changes | **${ENV_VER}** |"
echo "| \`.env.example\` other changes | ${ENV_ANY:-**0** — not in the range} |"
echo "| migrations added | ${MIG:-**0** — \`config/migrations/env-changes.json\` not in the range} |"
echo
echo "> Every zero above carries a **control**, derived from this range by the same"
echo "> script that produced the zeros (#2238)."
echo ">"
# ---- control on the range -------------------------------------------------
if [ "$PRODUCT_N" -gt 0 ]; then
    read -r CA CD CF <<<"$(printf '%s\n' "$PRODUCT_ROWS" | sort -rn | head -1)"
    echo "> **Control on the range.** \`git diff --numstat ${RANGE}\` shows \`${CF}\` at **${CA} insertions and ${CD} deletions**, so the base ref resolves and the diff path works."
else
    OTHER=$(echo "$NUMSTAT" | awk 'NF==3 && $3 !~ /^releases\//' | sort -rn | head -1)
    if [ -n "$OTHER" ]; then
        read -r CA CD CF <<<"$OTHER"
        echo "> **Control on the range.** The range holds no product file; \`git diff --numstat ${RANGE}\` shows \`${CF}\` at **${CA} insertions and ${CD} deletions**, so the base ref resolves and the diff path works."
    else
        echo "> **Control on the range: NONE AVAILABLE.** The range changes nothing outside \`releases/\`; state the merge count (${PRS}) as the only evidence the range is non-empty."
    fi
fi
echo ">"
# ---- control on each pattern ----------------------------------------------
echo "> **Control on each pattern that returned zero.** A working diff still returns zero if the grep is wrong, so each pattern is also run on the most recent commit in history that matches it:"
echo ">"
echo '> ```'
printf '> %-42s %12s   %s\n' "pattern" "${PREV}..HEAD" "positive control"
for P in "${PIN_PATTERNS[@]}"; do
    IN_RANGE=$(pattern_count "$P" "$RANGE")
    # the grep patterns carry a leading [+-]; -G wants the line content
    CONTENT="${P//^\[+-\]/}"
    CTRL=$(g log --format=%h -n1 -G "$CONTENT" "$HEAD_REF" -- $PIN_FILES $(g ls-tree -r --name-only "$HEAD_REF" | grep -E '^compose.*\.ya?ml$' || true) 2>/dev/null || true)
    if [ -n "$CTRL" ]; then
        CTRL_N=$(pattern_count "$P" "${CTRL}~1..${CTRL}")
        printf '> %-42s %12s   %s~1..%s = %s\n' "$P" "$IN_RANGE" "$CTRL" "$CTRL" "$CTRL_N"
    else
        printf '> %-42s %12s   NO COMMIT IN HISTORY MATCHES — the pattern is unproven\n' "$P" "$IN_RANGE"
    fi
done
echo '> ```'
