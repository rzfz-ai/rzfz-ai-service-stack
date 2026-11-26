#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #3396: after every tmux-resurrect save, keep `last` on a save that can still
# bring the user's tabs back.
#
# Runs as resurrect's post-save-all hook (resurrect has already repointed
# `last` at the new save and rewritten pane_contents.tar.gz by then) and does
# two things:
#
#   1. `last` must point at a save with a complete pane record — the same
#      repair the boot restore runs (resurrect-valid-last.sh).
#   2. THE RULE: until the user has WORKED for a bounded time after a boot
#      whose restore did not complete, a save may not shrink `last`.
#      At boot the entrypoint writes the pin file (.boot-restore-pending):
#      line 1 the save it restores from, line 2 empty (the clock has not
#      started), line 3 how long the pin holds once it has
#      (RZFZ_TMUX_PIN_SECONDS, default 24 h); it also copies
#      pane_contents.tar.gz beside it. It removes both once the restore
#      brought every session of that save back.
#      The clock starts at the FIRST save after boot that holds a session —
#      when the user actually starts working — not at boot: measured in
#      #3396, the container booted at 06:39 and nobody attached before
#      14:00, so a clock started at boot had long run out when the fresh
#      one-session tmux was saved. From then on, line 2 holds the expiry.
#      While the pin is live, a new save holding FEWER sessions than the
#      pinned save does not become `last`: `last` is pointed back at the
#      pinned save AND the pinned save's pane contents are put back
#      (resurrect keeps one archive for all saves, so the new save had
#      overwritten them). The pin is released — and the user's saves count
#      again — as soon as a save holds at least as many sessions, or the
#      pin expires; both are logged.

# The rule counts sessions only, not which sessions: ten unrelated sessions
# release a pin on a ten-session save. That is deliberate — the guard is
# against the near-empty tmux a skipped restore leaves behind, not a diff.
#
# No save is lost: a kept-out save stays on disk (only a byte-identical repeat
# of the previous kept-out save is removed, the same thing resurrect itself
# does with a save identical to `last`). A user who wants to start fresh at
# once removes the pin file. Every decision is also appended to keep-last.log
# in the resurrect dir: under continuum and the entrypoint's loop the hook's
# stderr goes nowhere.
#
# Usage: resurrect-keep-last.sh [resurrect-dir]
#   default dir: ~/.local/share/tmux/resurrect
# Never fails the caller.
dir="${1:-${HOME:-/home/agent}/.local/share/tmux/resurrect}"
last="${dir}/last"
pin="${dir}/.boot-restore-pending"
pin_contents="${pin}.pane_contents.tar.gz"
rejected="${dir}/.boot-restore-rejected"
contents="${dir}/pane_contents.tar.gz"
logf="${dir}/keep-last.log"
here=$(dirname "$0")

[ -d "$dir" ] || exit 0

say() {
    echo "[resurrect-keep-last] $*" >&2
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$logf" 2>/dev/null || true
    # bounded: keep the newest 200 lines once the log passes 64 KiB
    if [ "$(wc -c < "$logf" 2>/dev/null || echo 0)" -gt 65536 ]; then
        tail -n 200 "$logf" > "${logf}.tmp" 2>/dev/null && mv -f "${logf}.tmp" "$logf"
    fi
}

release() {
    rm -f "$pin" "$pin_contents" "$rejected"
}

"${here}/resurrect-valid-last.sh" "$dir" || true

[ -f "$pin" ] || exit 0
pinned_name=$(sed -n 1p "$pin" 2>/dev/null)
expires=$(sed -n 2p "$pin" 2>/dev/null)
hold=$(sed -n 3p "$pin" 2>/dev/null)
case "$hold" in
    ''|*[!0-9]*) hold=86400 ;;
esac
pinned="${dir}/${pinned_name}"
if [ -z "$pinned_name" ] || [ ! -f "$pinned" ]; then
    say "the save pinned at boot (${pinned_name:-none}) is gone; guard released"
    release
    exit 0
fi

# Number of distinct sessions with a pane record in a save.
sessions_in() {
    awk -F '\t' '$1 == "pane" && !seen[$2]++ { n++ } END { print n + 0 }' "$1" 2>/dev/null || echo 0
}

current=$(readlink -f "$last" 2>/dev/null || echo "$last")
[ -f "$current" ] || exit 0
[ "$current" = "$(readlink -f "$pinned" 2>/dev/null || echo "$pinned")" ] && exit 0
n_cur=$(sessions_in "$current")
n_pin=$(sessions_in "$pinned")
now=$(date +%s)

case "$expires" in
    '')
        # The clock has not started: it starts with this save if it holds a
        # session (a save without one restores nothing and is no work).
        [ "$n_cur" -ge 1 ] || exit 0
        expires=$(( now + hold ))
        printf '%s\n%s\n%s\n' "$pinned_name" "$expires" "$hold" > "$pin" 2>/dev/null || true
        say "first save with a session since boot ($(basename "$current"), ${n_cur} session(s)): the pin on ${pinned_name} holds for ${hold}s from now"
        ;;
    *[!0-9]*)
        expires=0 ;;
esac
if [ "$now" -ge "$expires" ]; then
    say "pin on ${pinned_name} expired; last follows the current saves again (${pinned_name} stays on disk for a manual restore)"
    release
    exit 0
fi

if [ "$n_cur" -ge "$n_pin" ]; then
    say "$(basename "$current") holds ${n_cur} session(s), the pinned ${pinned_name} ${n_pin}; guard released"
    release
    exit 0
fi

ln -sfn "$pinned_name" "$last"
if [ -f "$pin_contents" ]; then
    cp -f "$pin_contents" "$contents" 2>/dev/null || true
fi
say "kept last on ${pinned_name} (${n_pin} session(s)): $(basename "$current") holds ${n_cur} and the boot restore has not completed"

prev=$(head -n 1 "$rejected" 2>/dev/null)
if [ -n "$prev" ] && [ "$prev" != "$(basename "$current")" ] && [ -f "${dir}/${prev}" ] \
        && cmp -s "${dir}/${prev}" "$current"; then
    rm -f "$current"
else
    basename "$current" > "$rejected"
fi
exit 0
