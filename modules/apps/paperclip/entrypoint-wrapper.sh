#!/bin/sh
set -e
# Configure optional integrations before handing off to the upstream entrypoint.
# Runs as root; docker-entrypoint.sh drops to node via gosu.

# rc6.7 #77: chown the persistent data dir to the node user. When the
# named volume is mounted at $PAPERCLIP_DATA_DIR (default /paperclip/.paperclip
# in our per-user catalog so config.json + agent JWT survive restarts),
# Docker creates the volume as root:root by default. Paperclip runs as
# `node` (uid 1000) and dies on first mkdir with `EACCES: permission
# denied, mkdir '/paperclip/.paperclip/instances/default/logs'`. Mirror
# the same chown the configure-opencode.sh already does for /paperclip/
# .local and /paperclip/.config. Idempotent — chown succeeds whether or
# not the dir is empty / pre-owned correctly.
PAPERCLIP_DATA_DIR="${PAPERCLIP_DATA_DIR:-/paperclip/.paperclip}"
mkdir -p "$PAPERCLIP_DATA_DIR" 2>/dev/null || true
chown -R node:node "$PAPERCLIP_DATA_DIR" 2>/dev/null || true

# M031-FOLLOWUPS B2: register the per-instance hostname in paperclip's
# allowed-hostname list. Without this, every fresh paperclip instance
# refuses requests with "Hostname '<host>' is not allowed" until an
# operator runs `pnpm paperclipai allowed-hostname <host>` manually.
# AGENT_INSTANCE_HOSTNAME is set by agent-manager from catalog.py
# (resolves to `paperclip-<instance-hash>.agents.<MAIN_DOMAIN>`).
# Idempotent — re-running on a second start sees the hostname already
# in the list and silently no-ops. Best-effort: failures don't block boot.
if [ -n "$AGENT_INSTANCE_HOSTNAME" ]; then
    echo "[paperclip] registering allowed hostname: $AGENT_INSTANCE_HOSTNAME"
    su -s /bin/sh node -c "
        cd /app && pnpm paperclipai allowed-hostname \"$AGENT_INSTANCE_HOSTNAME\" 2>&1
    " | tail -3 || true
fi

/usr/local/bin/configure-opencode.sh

# rc6.7 #82: complete the auto-onboard flow. Two paperclipai CLI
# commands are needed for a fresh instance:
#
#   1. `paperclipai onboard --yes --bind lan` writes the local
#      config.json + agent JWT (file at /paperclip/.paperclip/
#      instances/default/config.json). Without this the server boots
#      with "Agent JWT missing".
#   2. `paperclipai auth bootstrap-ceo` reads config.json and mints a
#      bootstrap-CEO invite URL the operator visits to claim the
#      first account.
#
# The previous version of this wrapper only ran step (2), but
# step (2) requires step (1) to have run already — on a fresh
# container there's no config.json, so step (2) errored with
# "No config found at /paperclip/.paperclip/instances/default/config.json"
# and the banner kept saying "Agent JWT missing". Run them as a pair.
#
# Both commands are idempotent:
# - `onboard --yes` writes a config + JWT only if config.json is absent
#   (the wizard re-prompts otherwise; --yes accepts the pre-pop'd
#   defaults without prompting). Re-running it is safe — it sees the
#   existing config and exits.
# - `auth bootstrap-ceo` revokes any unaccepted prior invite and mints
#   a new one. Once a CEO has claimed the invite, the CLI refuses to
#   issue new bootstrap invites and we just don't print the banner.
PAPERCLIP_CONFIG="${PAPERCLIP_DATA_DIR:-/paperclip/.paperclip}/instances/default/config.json"
if [ ! -f "$PAPERCLIP_CONFIG" ]; then
    # rc6.7 #82: paperclipai's `onboard --yes --bind lan` writes
    # config.json + agent JWT and THEN keeps running as a long-lived
    # paperclip server (despite `--run` defaulting to false). We only
    # want the config-writing side effect, not the server, because
    # docker-entrypoint.sh below will start the real (docker-managed)
    # server once we exec it. So: run the wizard in the background,
    # poll for config.json to appear, then kill the wizard.
    echo "[paperclip] No config.json yet — running first-run onboard wizard..."
    # #36 follow-up — first-boot port race (root cause).
    # `onboard --yes` doesn't just write config.json: `--yes` implies
    # shouldRun=true (cli/src/commands/onboard.ts), so it ALSO boots a full
    # paperclip server bound to config.server.port = `Number(PORT) || 3100`
    # (i.e. 3100). We must NOT change that port here — onboard persists it into
    # config.json and the REAL server reads the same file, so a throwaway port
    # (or PORT=0, which is `0 || 3100` → still 3100 anyway) would just move the
    # real server off 3100. The real defect is reaping: `kill $onboard_pid`
    # only kills the `su` wrapper, and the image ships NO procps, so the old
    # `pkill -f paperclipai.*onboard` was a silent "command not found" no-op —
    # the wizard's node server kept holding 3100 forever, and the real server
    # perpetually bumped to 3101 ("Requested port is busy; using next free
    # port 3100→3101") while agent-manager still proxied 3100 → blank page,
    # "fixed" only by a manual `docker restart`. Fix: reap the wizard's node
    # process by scanning /proc (no procps dependency) and WAIT until :3100
    # actually leaves LISTEN (via /proc/net/tcp, always present) before we exec
    # the real server. Keeps config.json's port at 3100.
    su -s /bin/sh node -c "
        cd /app && pnpm paperclipai onboard --yes --bind lan \\
            --data-dir '${PAPERCLIP_DATA_DIR:-/paperclip/.paperclip}' \\
            > /tmp/paperclip-onboard.log 2>&1
    " &
    onboard_pid=$!
    # Poll for up to 90 s for config.json to materialise. The wizard
    # typically writes it within ~5 s of boot, but the first run also
    # does an embedded-postgres sanity migration if external-postgres
    # isn't yet reachable, which can stretch.
    waited=0
    while [ ! -f "$PAPERCLIP_CONFIG" ] && [ $waited -lt 90 ]; do
        sleep 2
        waited=$((waited + 2))
    done
    if [ -f "$PAPERCLIP_CONFIG" ]; then
        echo "[paperclip] config.json written after ${waited}s — stopping wizard."
    else
        echo "[paperclip] WARN: onboard wizard didn't produce config.json within 90s. Continuing anyway."
    fi

    # Reap the wizard robustly (no procps in the image → scan /proc ourselves).
    # `kill $onboard_pid` only reaps the `su` wrapper; find the node
    # grandchild(ren) by cmdline and signal them directly. Running as root, so
    # we can signal the node-user processes.
    REAL_PORT="${PORT:-3100}"
    PORT_HEX=$(printf '%04X' "$REAL_PORT")
    _kill_wizard() {
        _sig="$1"
        for _d in /proc/[0-9]*; do
            _pid=${_d#/proc/}
            [ "$_pid" = "$$" ] && continue
            if tr '\0' ' ' < "$_d/cmdline" 2>/dev/null | grep -q 'paperclipai'; then
                kill "-$_sig" "$_pid" 2>/dev/null || true
            fi
        done
    }
    # :$REAL_PORT is in LISTEN (state 0A) in /proc/net/tcp{,6} while held.
    _port_listening() {
        awk -v p=":$PORT_HEX" '$2 ~ p"$" && $4=="0A" {found=1} END {exit found?0:1}' \
            /proc/net/tcp /proc/net/tcp6 2>/dev/null
    }
    kill "$onboard_pid" 2>/dev/null || true
    _kill_wizard TERM
    # Wait up to 20s for the wizard to release :$REAL_PORT, re-signalling each
    # second, so the real server binds it cleanly on the first try.
    port_waited=0
    while _port_listening && [ $port_waited -lt 20 ]; do
        echo "[paperclip] waiting for onboard wizard to release :${REAL_PORT} (${port_waited}s)..."
        _kill_wizard TERM
        sleep 1
        port_waited=$((port_waited + 1))
    done
    if _port_listening; then
        echo "[paperclip] :${REAL_PORT} still held after ${port_waited}s — SIGKILL the wizard."
        _kill_wizard KILL
        sleep 2
    fi
    if _port_listening; then
        echo "[paperclip] WARN: :${REAL_PORT} STILL busy — the real server may fall back to the next free port."
    else
        echo "[paperclip] :${REAL_PORT} is free — handing off to the real server."
    fi
    chown -R node:node "${PAPERCLIP_DATA_DIR:-/paperclip/.paperclip}" 2>/dev/null || true
fi

# The bootstrap-ceo CLI cannot run here — in paperclip v2026.513.0 the
# command needs the running app's DB to mint an invite, and we haven't
# exec'd docker-entrypoint yet. Pre-hotfix this block silently produced
# empty invite_output (operator-reported 2026-05-19: "Without url no
# onboarding"). The dashboard "Get bootstrap URL" button
# (agent-manager /api/bootstrap-url/<id>) runs the same CLI on demand
# after the server is up, so the operator can fetch the URL whenever
# they need it — first launch, after expiry, or after a CEO reset.

exec /usr/local/bin/docker-entrypoint.sh "$@"
