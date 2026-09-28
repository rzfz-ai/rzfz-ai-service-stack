#!/usr/bin/env bash
set -euo pipefail
# modules/apps/wazuh/configs/wazuh_certs/distribute-certs.sh — #855 rev-B
#
# Runs as ROOT at the tail of the wazuh-certs-generator one-shot, after the
# upstream entrypoint has (re)generated the PKI, and re-runs unchanged on every
# `docker compose up` (it is idempotent by construction: it rewrites the same
# files from the same inputs).
#
# WHY THIS EXISTS — three review findings, one mechanism (blockers 2 + 3,
# MEDIUM 9 of the agent-seqis rev-A review):
#
#   Blocker 3 — the upstream entrypoint finishes with `chmod -R 500
#   /certificates` + `chmod 400` on the files, leaving the volume ROOT as
#   `dr-x------ root:root`. Upstream's reference compose gets away with it
#   because it host-bind-mounts INDIVIDUAL FILES, so the traversal is done by
#   the daemon as root. This module mounts DIRECTORIES (WZ-8), which means the
#   consumer process traverses the path itself — and wazuh-indexer,
#   wazuh-dashboard and wazuh-securityadmin are all uid 1000 with `cap_drop:
#   ALL`. They cannot even open root-ca.pem. Reproduced with `docker run` on
#   the real permission sequence.
#
#   MEDIUM 9 — one shared volume meant the CA SIGNING KEY and admin-key.pem
#   were readable by all three long-running services. The manager is the
#   service that parses hostile agent/syslog input; a compromise there could
#   mint arbitrary node/admin certificates and rewrite the security index —
#   destroying exactly the log integrity the module exists to provide.
#
#   Blocker 2 — `/wazuh-config-mount` exists in no image, so Docker creates the
#   mountpoint of the fresh wazuh-manager-config volume as root:root 0755, and
#   wazuh-securityconfig-init (uid 1000) got EACCES on `mkdir -p …/etc`,
#   killing the one-shot every other service gates on.
#
# THE FIX: material is sorted into per-consumer subdirectories which each
# consumer mounts via `volumes: … volume: subpath:`. With a subpath mount the
# DAEMON resolves the path as root and the container sees the subdirectory as
# its mount root, so no consumer needs to traverse /certificates at all — and
# each one sees ONLY its own material. The CA signing keys stay in the volume
# root, which nothing mounts any more.
#
# Consumer -> subdir -> owning gid (root always keeps access):
#   wazuh-indexer        dist/indexer     1000   root-ca + its own keypair
#   wazuh-dashboard      dist/dashboard   1000   root-ca + its own keypair
#   wazuh-securityadmin  dist/admin       1000   root-ca + admin keypair
#   wazuh-manager        dist/manager      999   root-ca-manager + its keypair
#
# Nothing outside this script writes into dist/, and dist/ is rebuilt from the
# authoritative material on every run, so an operator poking at a consumer's
# copy is corrected at the next `up`.

CERTS=/certificates
DIST="$CERTS/dist"
MGR_MOUNT=/wazuh-config-mount

[ -f "$CERTS/root-ca.pem" ] || {
    echo "ERROR(#855): $CERTS/root-ca.pem missing after generation — refusing" >&2
    echo "  to distribute an incomplete PKI." >&2
    exit 1
}

# ── the CA signing keys never leave the volume root ───────────────────────
# root-ca-manager.key is a byte-identical copy of root-ca.key that the
# generator makes for the `server` node group and that nothing ever signs
# with. A second copy of a signing key is a second thing to leak, so it goes.
rm -f "$CERTS/root-ca-manager.key"
chmod 0700 "$CERTS"
chown root:root "$CERTS"

# ── per-consumer subdirs ──────────────────────────────────────────────────
# `dist` itself is traversal-only for everyone: the subpath mount is resolved
# by the daemon, so no container ever needs to list it.
mkdir -p "$DIST"
chown root:root "$DIST"
chmod 0755 "$DIST"

# publish <subdir> <gid> <file>...
#   0550 dir / 0440 files, group-owned by the consumer's gid: readable by the
#   one service that needs it, by nobody else, and never writable.
publish() {
    local sub="$1" gid="$2"; shift 2
    local dir="$DIST/$sub" f
    mkdir -p "$dir"
    # Drop anything a previous layout left behind, so a renamed/removed cert
    # cannot linger in a consumer path.
    # #2128: NOT `find`. This script runs inside wazuh/wazuh-certs-generator
    # (Amazon Linux 2023 since the 0.0.4 bump), which ships neither find nor
    # xargs — `line 85: find: command not found`, exit 127, and the whole wazuh
    # profile could not be installed in 2026.09-rc1/rc2. A bash glob needs
    # nothing outside the shell; dotfiles included, directories kept.
    local stale
    for stale in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
        [ -f "$stale" ] && rm -f "$stale"
    done
    for f in "$@"; do
        [ -f "$CERTS/$f" ] || {
            echo "ERROR(#855): expected $CERTS/$f — the certs tool did not" >&2
            echo "  produce it; check modules/apps/wazuh/configs/wazuh_certs_config.yml" >&2
            exit 1
        }
        cp -f "$CERTS/$f" "$dir/$f"
    done
    chown -R "root:$gid" "$dir"
    chmod 0550 "$dir"
    chmod 0440 "$dir"/*
    echo "[wazuh-certs-generator] published $sub (gid $gid): $*"
}

publish indexer   1000 root-ca.pem wazuh-indexer.pem wazuh-indexer-key.pem
publish dashboard 1000 root-ca.pem wazuh-dashboard.pem wazuh-dashboard-key.pem
publish admin     1000 root-ca.pem admin.pem admin-key.pem
publish manager    999 root-ca-manager.pem wazuh-manager.pem wazuh-manager-key.pem

# ── blocker 2: pre-create the manager config drop point ───────────────────
# wazuh-securityconfig-init runs as uid 1000 and writes MGR_MOUNT/etc, but the
# volume root belongs to root:root 0755 on a fresh volume, so its `mkdir -p`
# failed with EACCES and took every dependent service down with it. This
# container is the module's only root one-shot, so it is where the directory
# gets created — the securityconfig one-shot gates on it completing.
if [ -d "$MGR_MOUNT" ]; then
    mkdir -p "$MGR_MOUNT/etc"
    chown 1000:1000 "$MGR_MOUNT" "$MGR_MOUNT/etc"
    chmod 0750 "$MGR_MOUNT/etc"
    echo "[wazuh-certs-generator] prepared $MGR_MOUNT/etc for uid 1000"
else
    echo "ERROR(#855): $MGR_MOUNT is not mounted into the certs one-shot —" >&2
    echo "  wazuh-securityconfig-init will fail with EACCES on a fresh volume." >&2
    exit 1
fi

echo "[wazuh-certs-generator] distribution complete"
