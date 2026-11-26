#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# #2559: switch the Mac worker's Ollama to the PINNED version — side by side
# under <root>/ollama/<version>/, sha256-verified before anything changes, a
# `current` symlink flip, and a rollback when the new one does not come up.
#
#   ollama-switch.sh <version>
#
# Exit: 0 switched (or already current); 2 <version> is not the pin of the
# running bundle; 3 the download's sha256 does not match the pin (nothing
# changed); 4 the new Ollama could not be restarted, or did not answer
# /api/version with <version> within 60 s (rolled back to the previous one);
# 1 anything else (nothing flipped).
#
# The pin is the RUNNING bundle's, <root>/current/mac-agent/pins.json
# (finding 16): a new bundle brings its own, and no copy exists anywhere else.
# bash 3.2-safe (macOS ships 3.2): no associative arrays, no ${v,,}, no mapfile.
set -euo pipefail

ROOT="${RZFZ_WORKER_DIR:-/opt/rzfz-worker}"
WANT="${1:-}"
LABEL="ai.razzfazz.ollama"
PINS="$ROOT/current/mac-agent/pins.json"
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../lib/pins.sh
. "$HERE/../lib/pins.sh"
# shellcheck source=../lib/ollama-port.sh
. "$HERE/../lib/ollama-port.sh"

say() { echo "ollama-switch: $*"; }
fail() { local rc="$1"; shift; echo "ollama-switch: $*" >&2; exit "$rc"; }

version_re='^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-rc[0-9]{1,3})?$'
[[ $WANT =~ $version_re ]] || fail 2 "usage: ollama-switch.sh <X.Y.Z> (got '${WANT}')"
[ -f "$PINS" ] || fail 2 "no pin: $PINS is missing"
# the port THIS worker's Ollama runs on (.env.node; the legacy port for a worker
# installed before it moved) — read from the bundle that runs this script
PORT="$(rzfz_ollama_port "$HERE/../ollama-port.conf" "$ROOT/.env.node")" \
    || fail 1 "cannot tell which port this worker's Ollama uses ($HERE/../ollama-port.conf)"
PINNED="$(rzfz_pin "$PINS" ollama version)"
URL="$(rzfz_pin "$PINS" ollama url)"
SHA="$(rzfz_pin "$PINS" ollama sha256)"
if [ "$PINNED" != "$WANT" ] || [ -z "$URL" ] || [ -z "$SHA" ]; then
    fail 2 "$WANT is not the pinned version in $PINS (pinned: ${PINNED:-none})"
fi

mkdir -p "$ROOT/ollama"
PREV="$(readlink "$ROOT/ollama/current" 2>/dev/null || true)"
TARGET="$ROOT/ollama/$WANT"
if [ "$PREV" = "$TARGET" ] && [ -x "$TARGET/ollama" ]; then
    say "already on $WANT"
    exit 0
fi

# one switch at a time (no flock on macOS: mkdir is atomic). A lock whose
# owner is gone (SIGKILL, a reboot mid-switch) is broken, not obeyed forever.
LOCK="$ROOT/ollama/.switch.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    other="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        fail 1 "another switch (pid $other) is running ($LOCK)"
    fi
    rm -rf "$LOCK"
    mkdir "$LOCK" 2>/dev/null || fail 1 "could not take the lock $LOCK — remove it if no switch is running"
fi
echo "$$" > "$LOCK/pid"
STAGE="$(mktemp -d "$ROOT/ollama/.stage.XXXXXX")"
cleanup() { rm -rf "$STAGE" "$LOCK"; }
trap cleanup EXIT

curl -fsSL --max-time 900 -o "$STAGE/ollama-darwin.tgz" "$URL" || fail 1 "download of $URL failed"
GOT="$(shasum -a 256 "$STAGE/ollama-darwin.tgz" | cut -d' ' -f1)"
[ "$GOT" = "$SHA" ] || fail 3 "sha256 $GOT of $URL does not match the pin $SHA — nothing changed"

mkdir "$STAGE/x"
tar -xzf "$STAGE/ollama-darwin.tgz" -C "$STAGE/x"
[ -x "$STAGE/x/ollama" ] || fail 1 "the pinned asset has no executable 'ollama' at its top level — nothing changed"
rm -rf "$TARGET"
mv "$STAGE/x" "$TARGET"
ln -sfn "$TARGET" "$ROOT/ollama/current"

# first install: the installer bootstraps the service after this
if [ "${RZFZ_SWITCH_INSTALL_ONLY:-}" = 1 ]; then
    say "installed $WANT"
    exit 0
fi

rollback() {
    if [ -n "$PREV" ] && [ -d "$PREV" ]; then
        ln -sfn "$PREV" "$ROOT/ollama/current"
        sudo -n /bin/launchctl kickstart -k "system/$LABEL" \
            || echo "ollama-switch: the restart of ${PREV##*/} failed too — check the sudoers drop-in" >&2
    fi
}
if ! sudo -n /bin/launchctl kickstart -k "system/$LABEL"; then
    echo "ollama-switch: could not restart $LABEL on $WANT — rolling back to ${PREV##*/}" >&2
    rollback
    exit 4
fi
start=$SECONDS
tries=0
while [ "$tries" -lt 30 ] && [ $((SECONDS - start)) -lt 60 ]; do
    tries=$((tries + 1))
    # captured, not piped into grep -q: under pipefail an early grep exit
    # SIGPIPEs curl and turns a match into a failure. Loopback never goes
    # through a proxy: the agent's spawn env passes the site's http_proxy.
    answer="$(curl -fsS --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/api/version" 2>/dev/null || true)"
    case "$answer" in
        *"\"$WANT\""*) say "now on $WANT (previous: ${PREV##*/})"; exit 0 ;;
    esac
    sleep 2
done

echo "ollama-switch: $WANT did not answer /api/version within 60 s — rolling back to ${PREV##*/}" >&2
rollback
exit 4
