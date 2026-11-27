#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# image-janitor.sh — the stack's own image hygiene (#2566)
# ==============================================================================
# Removes DANGLING images (no tag) CREATED more than 24 h ago that no container
# uses — running OR stopped, agent instances included — once a night at 03:30
# UTC. (`until=24h` measures the image's creation, not since when it is
# dangling: an old build that lost its tag at 03:00 goes at 03:30. The safety
# rule — no tag, no container — does not depend on the age.)
#
# Lives OUTSIDE core/backup/ on purpose: that directory is the backup image's
# build context, and a file there would change its build-context digest
# (#2006). The script is bind-mounted, never baked into an image.
#
# Never `prune -a`, never `--force`: an image no container runs is still
# needed — a disabled module's custom build (on an offline box it cannot be
# rebuilt), the hub-tagged node images, the pre-pulled pins of profiles that
# are off. Komodo's auto_prune (`prune -a`) stays off for that reason (#539).
# A tagged image is never a candidate; an image any container references is
# skipped here AND refused by the daemon (`docker image rm` without -f).
#
# Talks to docker-socket-proxy (CONTAINERS + IMAGES + POST), never the raw
# socket. `--once` cleans now and exits (tests, and an operator by hand).
# Any listing that fails removes nothing.
# ==============================================================================
set -eu

log() { echo "[image-janitor] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

clean_once() {
    # Every image id any container references (running, stopped, created).
    cids=$(docker ps -a -q --no-trunc) || { log "cannot list containers — nothing removed"; return 1; }
    in_use=""
    if [ -n "$cids" ]; then
        # shellcheck disable=SC2086  # one argument per container id
        in_use=$(docker inspect --format '{{.Image}}' $cids) \
            || { log "cannot inspect containers — nothing removed"; return 1; }
    fi
    cands=$(docker image ls -q --no-trunc --filter dangling=true --filter until=24h) \
        || { log "cannot list dangling images — nothing removed"; return 1; }
    removed=0; kept=0; failed=0
    for id in $cands; do
        case "
$in_use
" in
            *"
$id
"*) kept=$((kept + 1)); continue ;;
        esac
        if docker image rm "$id" >/dev/null 2>&1; then
            removed=$((removed + 1))
        else
            failed=$((failed + 1))
        fi
    done
    log "dangling images removed: ${removed}, kept (used by a container): ${kept}, refused by docker: ${failed}"
    return 0
}

seconds_until_0330_utc() {
    now=$(date -u +%s)
    # A date that cannot parse the time must not crash-loop the container.
    t=$(date -u -d "$(date -u +%Y-%m-%d) 03:30:00" +%s 2>/dev/null) || { echo 3600; return 0; }
    [ "$t" -gt "$now" ] || t=$((t + 86400))
    echo $((t - now))
}

if [ "${1:-}" = "--once" ]; then
    clean_once
    exit $?
fi
while :; do
    sleep "$(seconds_until_0330_utc)"
    clean_once || log "this night's clean-up did not run"
done
