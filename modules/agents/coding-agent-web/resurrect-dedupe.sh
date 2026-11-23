#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #3143: keep ONE record per pane (and per window) in a tmux-resurrect save.
#
# A save with several identical `pane` records for the same
# session:window.pane made the restore type the pane's command once per record
# into the same pane: the first starts the program, the rest land in the running
# program's input (prod 2026-11-09: a saved `claude --resume …` typed 3 more
# times into Claude in every tab). The first record of each pane/window wins.
#
# Usage: resurrect-dedupe.sh <save-file>   (rewrites it in place; a symlink such
# as resurrect's `last` is followed). Runs as resurrect's post-save-layout hook
# and once before the boot-time restore. Never fails the caller.
f="${1:-}"
[ -n "$f" ] && [ -f "$f" ] || exit 0
real=$(readlink -f "$f" 2>/dev/null || echo "$f")
tmp="${real}.dedupe.$$"
if awk -F '\t' '
    $1 == "pane"   { k = $2 FS $3 FS $6; if (seen_p[k]++) next }
    $1 == "window" { k = $2 FS $3;       if (seen_w[k]++) next }
    { print }
' "$real" > "$tmp" 2>/dev/null; then
    if cmp -s "$tmp" "$real"; then
        rm -f "$tmp"
    else
        cat "$tmp" > "$real" && rm -f "$tmp"
        echo "[resurrect-dedupe] removed duplicate pane/window records from ${real}" >&2
    fi
else
    rm -f "$tmp"
fi
exit 0
