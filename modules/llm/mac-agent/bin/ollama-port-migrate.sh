#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# Move the Mac worker's OWN Ollama (the ai.razzfazz.ollama LaunchDaemon) from
# Ollama's default port to the worker's own port (mac-agent/ollama-port.conf),
# so an Ollama the Mac runs for itself can have the default port back.
#
#   ollama-port-migrate.sh      (as the account the worker runs as; it calls
#                                sudo for the LaunchDaemon, nothing else)
#
# What it does: checks that the managed daemon is the one listening on the old
# port and that the new port is free; records the version and the models the
# daemon serves; keeps a copy of its plist and of .env.node; installs the plist
# with the new port and restarts THAT daemon only; checks the same version and
# every model answer on the new port; writes RZFZ_OLLAMA_PORT into .env.node and
# restarts the agent, which then talks to the new port. The models directory is
# not touched. Any other process — an Ollama.app, a brew service — is never
# stopped, signalled or reconfigured.
#
# Idempotent: a worker already on its own port is left as it is (exit 0).
#
# Exit: 0 moved, or already moved; 2 usage (root, the wrong account, no managed
# Ollama on this Mac, not enrolled); 4 the new port is held by another process
# (nothing changed); 5 the moved daemon did not come up with its version and
# models, or the agent could not be restarted (rolled back to the old plist and
# .env.node); 1 anything else, including a rollback that did not come back.
# bash 3.2-safe (macOS ships 3.2): no arrays, no ${v,,}, no mapfile. No python3.
set -euo pipefail
umask 077

ROOT="${RZFZ_WORKER_DIR:-/opt/rzfz-worker}"
ROOT="${ROOT%/}"
LAUNCHD_DIR="${RZFZ_LAUNCHD_DIR:-/Library/LaunchDaemons}"
LABEL="ai.razzfazz.ollama"
AGENT_LABEL="ai.razzfazz.worker-agent"
HERE="$(cd "$(dirname "$0")" && pwd)"
MAC="$(cd "$HERE/.." && pwd)"
# shellcheck source=../lib/ollama-port.sh
. "$MAC/lib/ollama-port.sh"

say() { echo "ollama-port-migrate: $*"; }
fail() { local rc="$1"; shift; echo "ollama-port-migrate: $*" >&2; exit "$rc"; }

PLIST="$LAUNCHD_DIR/$LABEL.plist"
ENV_NODE="$ROOT/.env.node"
BACKUP="$ROOT/.ollama-port-migrate"

# ── what runs now (nothing changes in this part) ─────────────────────────────
[ "$(id -u)" != 0 ] || fail 2 "run it as the account the worker runs as, not as root (it calls sudo where it must)"
[ -f "$PLIST" ] || fail 2 "no managed Ollama on this Mac ($PLIST is missing) — nothing to move"
[ -f "$ENV_NODE" ] || fail 2 "$ENV_NODE is missing — this Mac is not enrolled"
NEW="$(rzfz_ollama_conf "$MAC/ollama-port.conf" RZFZ_OLLAMA_PORT)" \
    || fail 1 "$MAC/ollama-port.conf names no valid RZFZ_OLLAMA_PORT"
# the port the daemon listens on is the one its plist gives it
OLD="$(awk '/<key>OLLAMA_HOST<\/key>/ { if (match($0, /127\.0\.0\.1:[0-9]+/)) { print substr($0, RSTART + 10, RLENGTH - 10); exit } }' "$PLIST")"
rzfz_ollama_valid_port "$OLD" || fail 1 "cannot read the loopback port from $PLIST (OLLAMA_HOST)"
OWNER="$(awk '/<key>UserName<\/key>/ { if (match($0, /<string>[^<]*<\/string>/)) { print substr($0, RSTART + 8, RLENGTH - 17); exit } }' "$PLIST")"
[ "$OWNER" = "$(id -un)" ] || fail 2 "run it as '$OWNER', the account the worker's Ollama runs as"

env_port() { awk 'index($0, "RZFZ_OLLAMA_PORT=") == 1 { v = substr($0, 18) } END { print v }' "$ENV_NODE"; }

# .env.node with RZFZ_OLLAMA_PORT=<port>: written next to it, then renamed
set_env_port() {
    awk 'index($0, "RZFZ_OLLAMA_PORT=") != 1' "$ENV_NODE" > "$ENV_NODE.tmp"
    printf 'RZFZ_OLLAMA_PORT=%s\n' "$1" >> "$ENV_NODE.tmp"
    chmod 0600 "$ENV_NODE.tmp"
    mv -f "$ENV_NODE.tmp" "$ENV_NODE"
}

restart_agent() { sudo -n /bin/launchctl kickstart -k "system/$AGENT_LABEL"; }

if [ "$OLD" = "$NEW" ]; then
    if [ "$(env_port)" != "$NEW" ]; then
        # the daemon moved, the agent was never told (an interrupted run)
        set_env_port "$NEW"
        restart_agent || fail 1 "could not restart $AGENT_LABEL — run: sudo launchctl kickstart -k system/$AGENT_LABEL"
        say "the agent now uses port $NEW"
    fi
    say "this worker's Ollama already runs on its own port $NEW — nothing to do"
    exit 0
fi

sudo -v || fail 2 "this needs sudo (it rewrites the $LABEL LaunchDaemon)"
# lsof via sudo: unprivileged, it lists only this account's sockets
listeners() { sudo lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null || true; }
pids_of() { printf '%s\n' "$1" | awk 'NR > 1 { print $2 }' | sort -u | tr '\n' ' '; }
managed_pid() {
    sudo launchctl print "system/$LABEL" 2>/dev/null \
        | awk '$1 == "pid" && $2 == "=" { print $3; exit }' || true
}

held="$(listeners "$NEW")"
if [ -n "$held" ]; then
    {
        echo "ollama-port-migrate: port $NEW — the worker's own Ollama port — is held by another process:"
        printf '%s\n' "$held" | sed 's/^/    /'
        echo "  Nothing was changed, and that process was left running."
        echo "  Stop whatever listens on port $NEW, then run this again."
    } >&2
    exit 4
fi
# whatever answers on the old port must be OUR daemon: an Ollama of the Mac's
# own there would otherwise be read as ours
mpid="$(managed_pid)"
[ -n "$mpid" ] || fail 1 "the managed $LABEL is not running — start it (sudo launchctl kickstart -k system/$LABEL) and run this again; nothing was changed"
case " $(pids_of "$(listeners "$OLD")")" in
    *" $mpid "*) ;;
    *) fail 1 "the managed $LABEL (pid $mpid) is not the process listening on port $OLD — nothing was changed" ;;
esac

# loopback never goes through the shell's http_proxy
get() { curl -fsS --noproxy '*' --max-time 10 "http://127.0.0.1:$1$2" 2>/dev/null; }
version_of() {
    local answer
    answer="$(get "$1" /api/version || true)"
    printf '%s' "$answer" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'
}
models_of() {
    local answer
    answer="$(get "$1" /api/tags)" || return 1
    printf '%s' "$answer" | grep -o '"name":"[^"]*"' | sed 's/^"name":"//; s/"$//' | sort -u || true
}
VERSION="$(version_of "$OLD")"
[ -n "$VERSION" ] || fail 1 "the managed Ollama does not answer /api/version on port $OLD — nothing was changed"
MODELS="$(models_of "$OLD")" || fail 1 "the managed Ollama does not answer /api/tags on port $OLD — nothing was changed"

# ── the new plist, rendered from the running bundle ──────────────────────────
WORK="$(mktemp -d "${TMPDIR:-/tmp}/rzfz-port.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
sed -e "s#{{USER}}#$OWNER#g" -e "s#{{ROOT}}#$ROOT#g" -e "s#{{OLLAMA_PORT}}#$NEW#g" \
    "$MAC/launchd/$LABEL.plist.tmpl" > "$WORK/$LABEL.plist"
grep -q "<string>127.0.0.1:$NEW</string>" "$WORK/$LABEL.plist" \
    || fail 1 "the bundle's $LABEL.plist.tmpl does not render port $NEW — nothing was changed"
if command -v plutil >/dev/null 2>&1; then
    plutil -lint -s "$WORK/$LABEL.plist" || fail 1 "the new $LABEL.plist does not parse — nothing was changed"
fi
# the rollback's copies: kept after the run too (0600, under the worker's root)
mkdir -p "$BACKUP"
cp "$PLIST" "$BACKUP/$LABEL.plist"
cp "$ENV_NODE" "$BACKUP/env.node"
chmod 0600 "$BACKUP/$LABEL.plist" "$BACKUP/env.node"

load() {   # <plist file>: install it and restart OUR daemon on it
    sudo install -m 0644 -o root -g wheel "$1" "$PLIST"
    sudo launchctl bootout "system/$LABEL" >/dev/null 2>&1 || true
    local tries=0
    until sudo launchctl bootstrap system "$PLIST"; do
        tries=$((tries + 1))
        [ "$tries" -lt 5 ] || return 1
        sleep 1
    done
}
answers() {   # <port>: the same version within 60 s
    local waited=0
    until [ "$(version_of "$1")" = "$VERSION" ]; do
        waited=$((waited + 1))
        [ "$waited" -lt 30 ] || return 1
        sleep 2
    done
}
rollback() {
    echo "ollama-port-migrate: $* — rolling back to port $OLD" >&2
    cp "$BACKUP/env.node" "$ENV_NODE.tmp"
    chmod 0600 "$ENV_NODE.tmp"
    mv -f "$ENV_NODE.tmp" "$ENV_NODE"
    if load "$BACKUP/$LABEL.plist" && answers "$OLD"; then
        restart_agent || echo "ollama-port-migrate: restart the agent: sudo launchctl kickstart -k system/$AGENT_LABEL" >&2
        fail 5 "rolled back: the worker's Ollama runs on port $OLD as before (logs: $ROOT/logs/ollama.log)"
    fi
    fail 1 "the rollback did not bring Ollama $VERSION back on port $OLD either — the previous plist is $BACKUP/$LABEL.plist (logs: $ROOT/logs/ollama.log)"
}

# ── the move ─────────────────────────────────────────────────────────────────
say "moving the worker's Ollama $VERSION from port $OLD to port $NEW"
load "$WORK/$LABEL.plist" || rollback "launchctl could not start $LABEL on the new plist"
answers "$NEW" || rollback "Ollama $VERSION did not answer on port $NEW within 60 s"
after="$(models_of "$NEW")" || rollback "Ollama on port $NEW does not answer /api/tags"
missing="$(printf '%s\n' "$MODELS" | while IFS= read -r m; do
    [ -z "$m" ] && continue
    # a here-string, not a pipe: grep -q's early exit would SIGPIPE printf
    grep -qxF -- "$m" <<<"$after" || printf '%s ' "$m"
done)"
[ -z "$missing" ] || rollback "models missing on port $NEW: $missing"
set_env_port "$NEW"
restart_agent || rollback "could not restart $AGENT_LABEL"
count="$(printf '%s\n' "$MODELS" | awk 'NF { n++ } END { print n + 0 }')"
say "done: the worker's Ollama $VERSION answers on 127.0.0.1:$NEW with its $count model(s); the agent uses it"
say "port $OLD is free for an Ollama of this Mac's own; the previous plist is kept in $BACKUP"
