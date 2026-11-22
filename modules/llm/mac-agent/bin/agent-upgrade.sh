#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# #2559 (with the #2530 protocol): the Mac agent upgrades itself to the
# master's CURRENT bundle. It records the running release, fetches the bundle's
# sha256 and then the bundle over the master's pinned CA, verifies the bytes
# BEFORE unpacking, unpacks into a fresh releases/<sha12>/, flips `current`
# atomically, restarts the agent, and waits until the new agent's loopback
# /health (serve_health, 127.0.0.1:8090) answers "ok" with the new digest —
# "ok" only once its first registration was accepted and its loops run. If it
# does not, `current` goes back to the release it came from.
#
# Each release has its own venv, releases/<sha12>/venv, built before the flip;
# the LaunchDaemon runs <root>/current/venv/bin/python, so the flip and the
# rollback move the code and the interpreter together.
#
#   agent-upgrade.sh            (no argument: D30's _spawn runs it detached,
#                                with an environment without credentials; it
#                                reads the master's URL from <root>/.env.node)
#
# Exit: 0 upgraded, or already on the master's bundle (nothing changed);
# 3 the master's sha256 is malformed, the bundle does not match it, does not
# unpack, carries links leaving it or special files, or is not a Mac agent
# bundle (nothing changed); 4 the new agent could not be restarted or did not
# answer "ok" with the new digest within max(90, 3 x the report interval
# + 10) s (rolled back to the previous
# release); 1 anything else (nothing flipped).
#
# Releases kept: the new one and the one it came from (the rollback target of
# the NEXT upgrade); older releases/<sha12> are removed after a verified
# upgrade only. The pins travel inside the bundle (finding 16): nothing is
# copied out of a release.
# bash 3.2-safe (macOS ships 3.2): no arrays, no ${v,,}, no mapfile. No python3
# (on a stock Mac it is the Command Line Tools shim).
set -euo pipefail

ROOT="${RZFZ_WORKER_DIR:-/opt/rzfz-worker}"
ROOT="${ROOT%/}"
LABEL="ai.razzfazz.worker-agent"
HEALTH="http://127.0.0.1:8090/health"
CA="$ROOT/certs/master-ca.pem"

say() { echo "agent-upgrade: $*"; }
fail() { local rc="$1"; shift; echo "agent-upgrade: $*" >&2; exit "$rc"; }

# A worker installed before its Ollama got a port of its own still runs it on
# Ollama's default port (.env.node has no RZFZ_OLLAMA_PORT). Moving it rewrites
# a LaunchDaemon, which this unprivileged upgrade cannot do: it says how, once
# per upgrade, and changes nothing.
ollama_port_hint() {
    local migrate="$ROOT/current/mac-agent/bin/ollama-port-migrate.sh"
    [ -f "$migrate" ] || return 0
    if awk 'index($0, "RZFZ_OLLAMA_PORT=") == 1 { found = 1 } END { exit !found }' "$ROOT/.env.node"; then
        return 0
    fi
    say "this worker's Ollama still uses Ollama's default port; to move it to its own port (models stay, any other Ollama on this Mac is left alone), run once as this account: $migrate"
}

# the agent's restart (launchctl kickstart -k) must not take this script with
# it: _spawn starts it in its own session; a hangup is ignored as well
trap '' HUP

hex_re='^[0-9a-f]{64}$'
rel_re='^[0-9a-f]{12}$'
# https only: over http the CA pin means nothing (no userinfo, no spaces/quotes)
master_re='^https://[]A-Za-z0-9._:/[-]+$'

# ── what runs now (nothing changes before the sha256 check) ──────────────────
[ -f "$ROOT/.env.node" ] || fail 1 "$ROOT/.env.node is missing — this Mac is not enrolled"
[ -f "$CA" ] || fail 1 "$CA is missing — re-run the installer from Fleet → Add worker"
MASTER="$(awk 'index($0, "LLM_MANAGER_URL=") == 1 { v = substr($0, 17) } END { print v }' "$ROOT/.env.node")"
MASTER="${MASTER%/}"
# the wait for the new agent's "ok" spans three registration intervals (the
# first registration is immediate; two more chances if the master is slow):
# max(90, 3 x LLM_WORKER_REPORT_INTERVAL + 10) seconds, polled every 2 s
INTERVAL="$(awk 'index($0, "LLM_WORKER_REPORT_INTERVAL=") == 1 { v = substr($0, 28) } END { print v }' "$ROOT/.env.node")"
int_re='^[0-9]{1,4}$'
[[ $INTERVAL =~ $int_re ]] || INTERVAL=30
BOUND=$((3 * INTERVAL + 10))
[ "$BOUND" -ge 90 ] || BOUND=90
[[ $MASTER =~ $master_re ]] || fail 1 "LLM_MANAGER_URL in $ROOT/.env.node is not an https:// URL — the bundle is fetched only over the pinned CA"
PREV="$(readlink "$ROOT/current" 2>/dev/null || true)"
case "$PREV" in
    "$ROOT/releases/"*) ;;
    *) fail 1 "$ROOT/current does not point into $ROOT/releases/ (it is '${PREV:-missing}') — re-run the installer" ;;
esac
[[ ${PREV##*/} =~ $rel_re ]] && [ -d "$PREV" ] || fail 1 "$ROOT/current points at '$PREV', not a release"
RUNNING="$(cat "$PREV/.digest" 2>/dev/null || true)"

# one upgrade at a time (no flock on macOS: mkdir is atomic). A lock whose
# owner is gone (SIGKILL, a reboot mid-upgrade) is broken, not obeyed forever.
LOCK="$ROOT/.agent-upgrade.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    other="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        fail 1 "another upgrade (pid $other) is running ($LOCK)"
    fi
    rm -rf "$LOCK"
    mkdir "$LOCK" 2>/dev/null || fail 1 "could not take the lock $LOCK — remove it if no upgrade is running"
fi
echo "$$" > "$LOCK/pid"
WORK="$(mktemp -d "$ROOT/.upgrade.XXXXXX")"
cleanup() { rm -rf "$WORK" "$LOCK" "$ROOT/.current.new"; }
trap cleanup EXIT

# every master fetch over the CA the installer pinned — never -k, and never
# down to plain http through a redirect
mget() { curl -fsSL --proto =https --proto-redir =https --cacert "$CA" --max-time 900 "$@"; }
mget -o "$WORK/mac-agent.sha256" "$MASTER/worker-bundle/generated/mac-agent.tar.gz.sha256" \
    || fail 1 "could not fetch the bundle's sha256 from $MASTER/worker-bundle/generated/"
want="$(awk '{ print $1; exit }' "$WORK/mac-agent.sha256")"
[[ $want =~ $hex_re ]] || fail 3 "the master's bundle sha256 is malformed — nothing changed"
if [ "$RUNNING" = "sha256:$want" ]; then
    say "already on the master's bundle sha256:$want"
    ollama_port_hint
    exit 0
fi
mget -o "$WORK/mac-agent.tar.gz" "$MASTER/worker-bundle/generated/mac-agent.tar.gz" \
    || fail 1 "could not fetch the agent bundle from $MASTER/worker-bundle/generated/"
got="$(shasum -a 256 "$WORK/mac-agent.tar.gz" | awk '{ print $1 }')"
[ "$got" = "$want" ] || fail 3 "bundle sha256 $got does not match the master's $want — nothing changed"

# ── a fresh release next to the running one ──────────────────────────────────
REL="$ROOT/releases/$(printf '%s' "$got" | cut -c1-12)"
[ "$REL" != "$PREV" ] || fail 1 "the new bundle's release name ${REL##*/} is the running one's, with another digest"
mkdir "$WORK/rel"
tar -xzf "$WORK/mac-agent.tar.gz" -C "$WORK/rel" \
    || fail 3 "the bundle does not unpack — nothing changed"
# only files, directories and links that stay inside the release: an absolute
# target or a `..` would make `current` import from somewhere else
odd="$(find "$WORK/rel" ! -type f ! -type d ! -type l | head -n 1)"
[ -z "$odd" ] || fail 3 "the bundle carries a special file (${odd#"$WORK/rel/"}) — nothing changed"
links="$(find "$WORK/rel" -type l)"
if [ -n "$links" ]; then
    while IFS= read -r l; do
        t="$(readlink "$l")"
        case "/$t/" in
            //*|*/../*) fail 3 "the bundle's link ${l#"$WORK/rel/"} -> $t leaves the release — nothing changed" ;;
        esac
    done <<EOF_LINKS
$links
EOF_LINKS
fi
for need in app/native_runtime.py mac-agent/bin/agent-upgrade.sh mac-agent/bin/ollama-switch.sh \
            mac-agent/pins.json mac-agent/requirements-macos.txt mac-agent/ollama-port.conf \
            mac-agent/lib/ollama-port.sh mac-agent/bin/ollama-port-migrate.sh; do
    [ -f "$WORK/rel/$need" ] || fail 3 "the bundle is not a Mac agent bundle (no $need) — nothing changed"
done
[ ! -e "$WORK/rel/venv" ] || fail 3 "the bundle carries a venv/ of its own — nothing changed"
echo "sha256:$got" > "$WORK/rel/.digest"
chmod -R go-w "$WORK/rel"
rm -rf "$REL"
mv "$WORK/rel" "$REL"

# the release's OWN venv, before anything flips: the running release's venv
# is never touched, so a rollback is a rollback of the interpreter too. uv's
# cache (<root>/.uv-cache) and the shared managed Python keep it cheap.
#
# Offline first: with unchanged requirements the cache already holds every
# wheel, and an upgrade then needs no network at all. PyPI is the fallback for
# a requirements change (or a cold cache) only. `--offline` makes uv resolve
# from the cache and fail rather than reach out, so a miss is a clean failure
# the fallback can pick up.
uvenv() { UV_PYTHON_INSTALL_DIR="$ROOT/python" UV_CACHE_DIR="$ROOT/.uv-cache" "$ROOT/uv/uv" "$@"; }
build_venv() {
    uvenv venv --managed-python --python 3.12 "$REL/venv" || return 1
    if uvenv pip install --offline --python "$REL/venv/bin/python" \
           -r "$REL/mac-agent/requirements-macos.txt"; then
        say "requirements installed from the local cache (no network needed)"
        return 0
    fi
    say "the local cache does not cover the requirements — falling back to the package index"
    uvenv pip install --python "$REL/venv/bin/python" \
        -r "$REL/mac-agent/requirements-macos.txt"
}
if ! build_venv; then
    rm -rf "$REL"
    fail 1 "the new release's venv did not build — still on ${PREV##*/}, nothing changed"
fi
# built after the go-w pass above, under the caller's umask: a 002 umask left
# the interpreter the LaunchDaemon runs group-writable
chmod -R go-w "$REL/venv"

# ── flip, restart, verify ────────────────────────────────────────────────────
# atomic: a new link renamed over `current` (ln -sfn unlinks first, so a
# reader could find no `current` at all). BSD mv -h / GNU mv -T: rename the
# link itself instead of moving into the directory it points at.
flip() {
    rm -f "$ROOT/.current.new"
    ln -s "$1" "$ROOT/.current.new"
    if [ "$(uname -s)" = Darwin ]; then
        mv -fh "$ROOT/.current.new" "$ROOT/current"
    else
        mv -fT "$ROOT/.current.new" "$ROOT/current"
    fi
}
restart() { sudo -n /bin/launchctl kickstart -k "system/$LABEL"; }
rollback() {
    flip "$PREV"
    restart || echo "agent-upgrade: the restart on ${PREV##*/} failed too — check the sudoers drop-in" >&2
}

flip "$REL"
say "current → ${REL##*/} (previous ${PREV##*/}); restarting $LABEL"
if ! restart; then
    echo "agent-upgrade: could not restart $LABEL on ${REL##*/} — rolling back to ${PREV##*/}" >&2
    rollback
    exit 4
fi
start=$SECONDS
tries=0
while [ "$tries" -lt $((BOUND / 2)) ] && [ $((SECONDS - start)) -lt "$BOUND" ]; do
    tries=$((tries + 1))
    # loopback: never through a proxy (the spawn env passes the site's
    # http_proxy for the master). Captured, not piped into grep -q (pipefail).
    # "ok" AND the new digest: "starting" (503) is an agent that has not yet
    # registered, and the old digest is the old agent.
    # Either JSON spacing (json.dumps' default or compact separators).
    answer="$(curl -fsS --noproxy '*' --max-time 2 "$HEALTH" 2>/dev/null || true)"
    case "$answer" in
        *'"status": "ok"'*|*'"status":"ok"'*) status_ok=1 ;;
        *) status_ok=0 ;;
    esac
    case "$status_ok:$answer" in
        1:*"\"sha256:$got\""*)
            # verified: keep this release and the one it came from (the next
            # upgrade's rollback target); older ones go
            for d in "$ROOT"/releases/*; do
                [ -d "$d" ] || continue
                [[ ${d##*/} =~ $rel_re ]] || continue
                if [ "$d" = "$REL" ] || [ "$d" = "$PREV" ]; then continue; fi
                rm -rf "$d"
            done
            say "now on sha256:$got (previous ${PREV##*/})"
            ollama_port_hint
            exit 0 ;;
    esac
    sleep 2
done

echo "agent-upgrade: the new agent did not answer \"ok\" with sha256:$got on $HEALTH within $BOUND s — rolling back to ${PREV##*/}" >&2
rollback
exit 4
