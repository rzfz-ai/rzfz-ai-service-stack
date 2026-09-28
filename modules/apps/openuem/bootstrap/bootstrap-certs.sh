#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# ==============================================================================
# OpenUEM certificate bootstrap (#1075)
# ==============================================================================
# Runs INSIDE openuem/openuem-cert-manager, replacing upstream's
# `entrypoint: /bin/configure.sh`. It does three things:
#
#   1. runs upstream's own generators, unmodified;
#   2. adds a loopback-only NATS monitoring endpoint so `openuem-nats` can have
#      a real healthcheck (the generated config enables none);
#   3. distributes a flat, per-consumer copy of the material each service needs
#      into one named volume per consumer.
#
# WHY (3): upstream bind-mounts a host directory and then mounts INDIVIDUAL
# FILES out of it. This repo keeps state in Docker-managed named volumes, and a
# named volume always materialises as a DIRECTORY — mounting one at a file path
# gives the consumer an EISDIR, silently. So every consumer here gets its own
# small volume containing exactly the flat filenames its image expects.
#
# IDEMPOTENT BY CONSTRUCTION. `restart: "no"` plus
# `service_completed_successfully` means this runs on EVERY `docker compose up`.
# Upstream's create-certs.sh guards every generation step behind
# `[ ! -f <target> ]`, so a second run is a no-op over an existing PKI;
# generate-nats-conf.sh rewrites nats.cfg from scratch, deterministically, from
# the same environment; and the distribution below is `install`, overwriting
# with identical content. Nothing here removes write permission from a
# directory it will need again.
#
# AIR-GAP: no network fetch. The only network peer is the stack's Postgres,
# where the cert-manager registers each issued certificate. See DECISION-7.
# ==============================================================================
set -euo pipefail

log() { printf '[openuem-certs] %s\n' "$*"; }

# -- 1. Upstream generators --------------------------------------------------
log "generating / verifying the PKI in /certificates"
/bin/create-certs.sh

# NATS_DEBUG is deliberately never set: upstream's generate-nats-conf.sh writes
# the debug block with `>` (truncate) rather than `>>`, which would DESTROY the
# tls{} and authorization{} blocks it had just written — leaving an open broker.
unset NATS_DEBUG || true
log "generating /etc/nats/nats.cfg"
/bin/generate-nats-conf.sh

# -- 2. Loopback-only monitoring endpoint for the NATS healthcheck ------------
# Bound to 127.0.0.1 INSIDE the nats container; never published, never routed.
# Gives /healthz, which is a real readiness signal instead of a TCP accept.
if ! grep -q '^http: "127.0.0.1:8222"' /etc/nats/nats.cfg; then
    printf '\nhttp: "127.0.0.1:8222"\n' >> /etc/nats/nats.cfg
    log "added loopback monitoring endpoint to nats.cfg"
fi

# -- 3. Per-consumer distribution --------------------------------------------
# dist <target-dir> <src> [<src> ...] — copies with an explicit mode. Files land
# root:root 0640; every consuming image runs as root (verified in Task 2), so no
# ownership change is needed — which matters, because cap_drop:[ALL] removes
# CAP_CHOWN and any such call would fail.
dist() {
    local dst=$1; shift
    mkdir -p "$dst"
    local f
    for f in "$@"; do
        if [ ! -f "$f" ]; then
            echo "[openuem-certs] FATAL: expected certificate material missing: $f" >&2
            exit 1
        fi
        install -m 0640 "$f" "$dst/$(basename "$f")"
    done
}

CA=/certificates/ca/ca.cer

dist /dist/nats \
    /certificates/nats/nats.cer /certificates/nats/nats.key "$CA"

dist /dist/ocsp \
    /certificates/ocsp/ocsp.cer /certificates/ocsp/ocsp.key "$CA"

dist /dist/console \
    /certificates/console/console.cer /certificates/console/console.key \
    /certificates/console/sftp.cer /certificates/console/sftp.key "$CA"

dist /dist/worker-agents \
    /certificates/agents-worker/worker.cer /certificates/agents-worker/worker.key "$CA"

dist /dist/worker-notification \
    /certificates/notification-worker/worker.cer /certificates/notification-worker/worker.key "$CA"

dist /dist/worker-cert-manager \
    /certificates/cert-manager-worker/worker.cer /certificates/cert-manager-worker/worker.key "$CA"

# The cert-manager worker is the ONLY component that legitimately holds the CA
# PRIVATE key: it signs the per-agent certificate issued when an administrator
# admits an agent in the console. No other consumer receives the CA private key.
# Kept as its own single-line dist call so that the ONE line naming the CA
# private key also names its ONLY destination — the guard test reads it that way.
dist /dist/worker-cert-manager /certificates/ca/ca.key

log "PKI ready."
log "  agent enrolment material : /certificates/agents/agent.cer + agent.key"
log "  admin user certificate   : /certificates/users/admin.pfx"
log "  fetch with: docker cp openuem-certs:/certificates/users/admin.pfx ."
