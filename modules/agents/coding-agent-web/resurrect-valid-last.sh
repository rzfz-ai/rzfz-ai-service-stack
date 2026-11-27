#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #3238: repoint resurrect's `last` to the newest save that actually holds
# pane records.
#
# Measured in #3238: as the tmux server was torn down during an unplanned
# reboot, continuum's periodic save wrote a 10-byte save and `last` pointed at
# it — the next boot restored nothing while the good save sat next to it in
# the resurrect dir. A save counts as valid only when it holds at least one
# COMPLETE `pane` record; a save without one (empty, or clearly truncated mid-
# write) restores nothing.
#
# #3396: a DANGLING `last` (its save is gone) and a missing `last` next to
# saves are repaired the same way. `[ -e ]` follows the symlink, so the old
# `[ -e last ] || exit 0` left a dangling `last` alone and the boot restore
# was skipped without a word.
#
# Usage: resurrect-valid-last.sh [resurrect-dir]
#   default dir: ~/.local/share/tmux/resurrect
# Only the `last` symlink is repointed — no save file is ever deleted or
# modified, and every skipped save is logged. When `last` already points at a
# valid save, or no save in the dir has a complete pane record, the dir is
# left as it is. Never fails the caller.
#
#        resurrect-valid-last.sh --check <save-file>
# exits 0 when that save is valid by the rule below, 1 when it is not (#3396:
# the entrypoint counts a save as done only when it is valid).
# A save is valid only when it ends with a newline (tmux-resurrect terminates
# every line, so a missing one means the write was cut) AND holds at least one
# COMPLETE `pane` record in the format tmux-resurrect's save.sh writes:
# 11 tab-separated fields — pane, session, window index, window active,
# :window_flags, pane index, pane title, :current_path, pane active,
# pane command, :full_command. A record cut mid-write (e.g. a bare
# `pane<TAB>s1<TAB>`) has too few fields and restores nothing.
save_is_valid() {
    f="$1"
    [ -f "$f" ] || return 1
    [ -n "$(tail -c 1 "$f")" ] && return 1
    awk -F '\t' '
        $1 == "pane" && NF >= 11 && \
        substr($5, 1, 1) == ":" && substr($8, 1, 1) == ":" && substr($11, 1, 1) == ":" {
            found = 1
        }
        END { exit !found }
    ' "$f" 2>/dev/null
}

if [ "${1:-}" = "--check" ]; then
    save_is_valid "${2:-}"
    exit $?
fi

dir="${1:-${HOME:-/home/agent}/.local/share/tmux/resurrect}"
last="${dir}/last"
[ -d "$dir" ] || exit 0
if [ ! -e "$last" ] && [ ! -L "$last" ]; then
    # No `last` at all: nothing to do on a first start (no save either).
    [ -n "$(ls "$dir"/tmux_resurrect_*.txt 2>/dev/null)" ] || exit 0
fi

if [ -L "$last" ] && [ ! -e "$last" ]; then
    echo "[resurrect-valid-last] ${last} is dangling (-> $(readlink "$last")); looking for the newest valid save" >&2
elif [ ! -e "$last" ]; then
    echo "[resurrect-valid-last] ${last} is missing; looking for the newest valid save" >&2
else
    target=$(readlink -f "$last" 2>/dev/null || echo "$last")
    if save_is_valid "$target"; then
        exit 0
    fi
    echo "[resurrect-valid-last] ${last} -> $(basename "$target") has no complete pane record; looking for the newest valid save" >&2
fi

best=""
for f in $(ls -t "$dir"/tmux_resurrect_*.txt 2>/dev/null); do
    [ -f "$f" ] || continue
    if save_is_valid "$f"; then
        best="$f"
        break
    fi
    echo "[resurrect-valid-last] skipping $(basename "$f") (no complete pane record)" >&2
done

if [ -n "$best" ]; then
    ln -sfn "$(basename "$best")" "$last"
    echo "[resurrect-valid-last] repointed last -> $(basename "$best")" >&2
else
    echo "[resurrect-valid-last] no save with a complete pane record in ${dir}; last left as is" >&2
fi
exit 0
