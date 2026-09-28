#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #2241 — read the finished package back with a DIFFERENT reader and assert it
# contains what was staged.
#
# WHY THIS EXISTS. `cli/package.sh` copies the model GGUFs into the staging tree
# with a root helper, counts them, prints "Bundled N of M", and tars the tree.
# On 2026-09-16 on 0.91 the copy succeeded, the tally said four models and ~7 GB,
# and `tar` could not READ a single one of them — they were root-owned, mode
# 0600, and tar runs as the invoking user. The result was a 49 625 468 048-byte
# package containing 633 image members, 6458 plugin members and **zero** `.gguf`
# members, left sitting under its final name.
#
# #759 did not catch it: #759 inspects the packager's own tally, computed while
# copying. Nothing between the copy and the customer looked at the artefact.
#
# So this compares the ARCHIVE against the STAGING TREE, not against the tally —
# the tally is separately known to be wrong by construction (#2234), and a guard
# must not rest on a number that is already understood to be unreliable.
#
# Exit 0 = the archive carries what was staged. Non-zero = it does not, and the
# caller must NOT publish the artefact under its final name.
set -euo pipefail

ARCHIVE="${1:?archive path}"
STAGING="${2:?staging directory}"

fail() { printf 'verify-package-archive: %s\n' "$*" >&2; exit 1; }

[ -f "$ARCHIVE" ] || fail "archive does not exist: $ARCHIVE"
[ -d "$STAGING" ] || fail "staging directory does not exist: $STAGING"

# ONE pass over the archive. A 47 GB package is streamed once, not once per
# question — the counts below all come out of this single member list.
INDEX="$(mktemp "${TMPDIR:-/tmp}/rzfz-verify-XXXXXX")"
trap 'rm -f "$INDEX"' EXIT
tar tzf "$ARCHIVE" > "$INDEX" || fail "could not list the archive: $ARCHIVE"

# Counting helpers. `grep -c` exits 1 on zero matches, which under `set -e`
# would abort here and report a crash where the answer is legitimately zero.
count_in_archive() { grep -cE "$1" "$INDEX" 2>/dev/null || true; }
# Compare LIKE WITH LIKE. Two ways to get this wrong, both found on the first
# real run (2026-09-16, 0.91) and both false REDS on a good package:
#
#   * `-type f` excludes symlinks, tar counts them as members. The dify uv-cache
#     is full of them: staged=5591 vs archived=5665, a package defect that did
#     not exist. `! -type d` matches what a tar member is.
#   * an UNANCHORED pattern matches the same directory name anywhere in the
#     tree. `/images/` also hit docs/enterprise/images/ and core/help/…/images/:
#     staged=92 vs archived=590. Every pattern is anchored at the archive root.
#
# A guard that cries wolf costs a 67-minute build and teaches people to override
# it, which is how a real red gets waved through later.
count_in_staging() {
    [ -d "$STAGING/$1" ] || { echo 0; return; }
    find "$STAGING/$1" ! -type d ${2:+-name "$2"} 2>/dev/null | wc -l | tr -d ' '
}

rc=0
check() {
    local label="$1" staged="$2" archived="$3"
    if [ "$staged" -ne "$archived" ]; then
        printf 'verify-package-archive: MISMATCH %-22s staged=%s archived=%s\n' \
            "$label" "$staged" "$archived" >&2
        rc=1
    else
        printf '  %-22s staged=%s archived=%s OK\n' "$label" "$staged" "$archived"
    fi
}

# `tar czf -C "$STAGING_DIR" .` writes members as `./x/y`; some readers
# normalise the leading `./` away, so every anchor tolerates both and nothing
# else.
check "model GGUFs"    "$(count_in_staging models '*.gguf')" \
                       "$(count_in_archive '^(\./)?models/.+\.gguf$')"
check "dify plugins"   "$(count_in_staging plugins/dify)" \
                       "$(count_in_archive '^(\./)?plugins/dify/.+[^/]$')"
check "dify uv-cache"  "$(count_in_staging plugins/dify-uv-cache)" \
                       "$(count_in_archive '^(\./)?plugins/dify-uv-cache/.+[^/]$')"
check "dify wheelhouse" "$(count_in_staging plugins/dify-wheelhouse)" \
                       "$(count_in_archive '^(\./)?plugins/dify-wheelhouse/.+[^/]$')"
# #2272: "staged=0 archived=0 OK" agrees with itself while describing nothing.
# If plugin packages were bundled, the wheelhouse row must count something
# (wheels, or the builder's NO-REQUIREMENTS marker) or the package ships plugins
# that cannot install air-gapped.
if [ "$(count_in_staging plugins/dify)" -gt 0 ] && [ "$(count_in_staging plugins/dify-wheelhouse)" -eq 0 ]; then
    printf 'verify-package-archive: VACUOUS  %-22s plugins bundled=%s but no wheelhouse file staged (#2272)\n' \
        "dify wheelhouse" "$(count_in_staging plugins/dify)" >&2
    rc=1
fi
check "image archives" "$(count_in_staging images)" \
                       "$(count_in_archive '^(\./)?images/.+[^/]$')"

if [ "$rc" -ne 0 ]; then
    fail "the package does NOT contain what was staged — refusing to publish it (#2241)"
fi
printf 'verify-package-archive: the archive carries what was staged.\n'
