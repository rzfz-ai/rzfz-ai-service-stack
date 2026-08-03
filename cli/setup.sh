#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
set -eo pipefail

# M026 / S02 #7: source the shared library for print_error (stderr-routed
# error helper). This script is a thin wrapper around the Python CLI inside
# the razzfazz-setup container — there are no colors, env-utilities or
# secret-generation primitives here to dedupe (the roadmap entry assumed
# duplications that actually live inside cli/setup.py, not in the wrapper).
# The migration is therefore minimal: replace the one bare `echo "Error:"`
# with print_error so the wrapper's error path matches the rest of the
# razzfazz-*.sh family.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

# Wrapper script to run the razzfazz-setup CLI tool
# Usage: rzfz setup [COMMAND]
#
# Commands:
#   --status                       Show current configuration status
#   --init                         Run initialization wizard (interactive)
#   --reset                        🔴 DATA-LOSS: Factory Reset — deletes all
#                                  stack data/secrets. Irreversible.
#   --regenerate-secrets           🟡 COORDINATED: regenerate all secrets and
#                                  passwords (re-keys live services; plan it)
#   --rotate-bootstrap-password [<NEW_PW>]
#                                  BSB-02: rotate the Authentik bootstrap admin
#                                  password in one shot. Updates Authentik DB
#                                  via API + .env + cascades to
#                                  BACKUP_ENCRYPTION_PASSWORD (if equal to old)
#                                  + force-recreates backup-service.
#                                  Omit <NEW_PW> to generate a strong 32-char one.
#                                  See docs/security-architecture.md §8 + §16.

#   --rotate-db-password <mod> [PW]
#                                  BSB-05: rotate a per-module Postgres user
#                                  password in one shot. Updates Postgres via
#                                  ALTER USER + .env <MOD>_DB_PASSWORD +
#                                  force-recreates the module's containers +
#                                  best-effort healthz probe. Omit PW to
#                                  generate a strong 32-char one. Use 'all'
#                                  for the module name to iterate every
#                                  configured module. --list-modules shows
#                                  what's available. See
#                                  docs/security-architecture.md §10 + §16.
#   --secrets-status               Show status of all secrets
#   --set-gpustack-api-key <KEY>   Set the GPUStack API key for model sync
#   --gpustack-api-key-status      Show GPUStack API key status
#   --smtp-status                  Show SMTP relay configuration status
#   --set-smtp-mode <MODE>         Set SMTP mode (relay or direct)
#   --set-smtp-relay H U P         Set SMTP relay credentials (host user pass)
#   --set-tls-mode <MODE>          Set TLS mode (letsencrypt, selfsigned, certificate)
#   --install-certificate C K      Install custom wildcard TLS certificate
#   --certificate-status           Show TLS and certificate file status
#   --set-backup-encryption-password [PASS]  Set custom backup encryption password (omit to clear)
#   --backup-encryption-status     Show backup encryption status
#   --set-entra-credentials ID SECRET TENANT [DOMAIN]
#                                  Set Microsoft Entra ID SSO credentials and enable it [EXPERIMENTAL].
#                                  DOMAIN is optional (email domain restriction).
#                                  Restarts Authentik to apply blueprint changes.
#   --set-openwebui-oidc ID SECRET
#                                  Set Open WebUI Authentik OIDC client credentials and enable [EXPERIMENTAL].
#                                  Extract ID/SECRET from Authentik → Applications → Open WebUI (OIDC).
#   --disable-openwebui-oidc       Disable Open WebUI native OIDC login [EXPERIMENTAL]
#   --set-gitea-oidc ID SECRET     Set Gitea Authentik OIDC credentials and enable [EXPERIMENTAL].
#                                  Extract ID/SECRET from Authentik → Applications → Gitea (OIDC).
#   --disable-gitea-oidc           Disable Gitea native Authentik OIDC [EXPERIMENTAL]
#   --set-lightrag-models M E [R]  Set LightRAG models (LLM, Embedding, optional Reranker) [EXPERIMENTAL]
#   --lightrag-status              Show LightRAG model configuration [EXPERIMENTAL]
#   --set-cognee-models M E [D]    Set Cognee models (LLM, Embedding, optional Dim) [EXPERIMENTAL]
#   --cognee-status                Show Cognee model configuration [EXPERIMENTAL]
#   --paperclip-status             Show Paperclip profile status [EXPERIMENTAL]
#   --moltis-status                Show Moltis profile status [EXPERIMENTAL]
#   --hermes-status                Show Hermes Agent profile status [EXPERIMENTAL]
#   --matrix-status                Show Matrix (Synapse + Element Web) profile status [EXPERIMENTAL]
#   --corporate-proxy ...          #181: enable/disable CORPORATE-PROXY mode
#                                  (TLS-intercept CA trust + HTTP(S)_PROXY egress).
#                                  Off by default. HOST-side (needs sudo + docker):
#                                    --corporate-proxy --proxy-url URL --ca-file PATH
#                                        [--dns IP[,IP]] [--egress-set standard|all]
#                                    --corporate-proxy --status   (show state)
#                                    --corporate-proxy --check    (dry-run)
#                                    --corporate-proxy --off      (disable overlay)
#                                  See docs/enterprise/how-to/corporate-proxy.md.
#   --network-mode ...             #184: the single front-door for the egress axis.
#                                    --network-mode --mode online|proxied|offline
#                                    --network-mode --status   (show mode + overlays)
#                                    --network-mode --check    (dry-run)
#                                  online=direct, proxied=via corporate proxy (#181),
#                                  offline=no internet egress / LAN stays up (#184).
#   --checksum-take [COMMENT]      Take a checksum governance snapshot
#   --checksum-history             Show checksum governance history
#   --checksum-detail <SET_ID>     Show details of a checksum set
#   --checksum-diff <ID_A> <ID_B>  Compare two checksum sets
#   --checksum-status              Show current governance fingerprint
#   --logs-take [REASON]           Create a log snapshot
#   --logs-list                    List available log snapshots
#   -h, --help                     Show help

# Ensure we're in the project root directory (#26 cli/ move: $0 now lives in
# cli/, so cd to SCRIPT_DIR which already resolves one level up to the root).
cd "$SCRIPT_DIR"

# M032-S09 dry-run guard (defense-in-depth for the test harness — see
# tests/scripts/_helpers.py). Honors RAZZFAZZ_TEST_DRY_RUN=1 for
# destructive subcommands and exits 0 BEFORE any container exec.
if [ "${RAZZFAZZ_TEST_DRY_RUN:-0}" = "1" ]; then
    case "${1:-}" in
        --reset|--regenerate-secrets)
            echo "DRY-RUN: would have invoked razzfazz-setup.sh $1 (RAZZFAZZ_TEST_DRY_RUN=1)" >&2
            exit 0 ;;
    esac
fi

# BSB-02: --rotate-bootstrap-password runs on the HOST (not inside the
# razzfazz-setup container) because it must force-recreate backup-service
# via the host's docker socket. Route it BEFORE the docker check so the
# wrapper still works on a box where docker isn't installed yet (the
# rotation script itself will gate on docker only when it actually needs it).
if [ "${1:-}" = "--rotate-bootstrap-password" ]; then
    shift
    exec "${SCRIPT_DIR}/scripts/rotate-bootstrap-password.sh" "$@"
fi

# BSB-05 — --rotate-db-password is delegated to a host-side script. It must
# run on the host (not inside razzfazz-setup) because it needs to:
#   - docker exec into the postgres container (ALTER USER), and
#   - docker compose up -d --force-recreate the affected module containers.
# Both require the host docker socket, which razzfazz-setup does not have.
# Routing happens BEFORE the container exec dispatch so this is a no-op
# inside razzfazz-setup's CLI.
if [ "${1:-}" = "--rotate-db-password" ]; then
    shift
    exec "${SCRIPT_DIR}/scripts/rotate-db-password.sh" "$@"
fi

# #181 — --corporate-proxy runs on the HOST (not inside razzfazz-setup). It must
# install the customer proxy CA into the host trust store (update-ca-certificates),
# write the docker daemon proxy/DNS config, regenerate certs/caddy-ca.pem, and
# force-recreate egress containers — all needing the host docker socket + sudo,
# which the container backend does not have. Routed before the container dispatch
# so it works even on a box where the setup backend isn't running.
#   rzfz setup --corporate-proxy --proxy-url URL --ca-file PATH [--dns IP] [--egress-set standard|all]
#   rzfz setup --corporate-proxy --status
#   rzfz setup --corporate-proxy --check
#   rzfz setup --corporate-proxy --off
if [ "${1:-}" = "--corporate-proxy" ]; then
    shift
    exec "${SCRIPT_DIR}/scripts/apply-corporate-proxy.sh" "$@"
fi

# #184 — --network-mode is the single front-door for the egress axis
# (online|proxied|offline). It sets RAZZFAZZ_NETWORK_MODE and reconciles the
# COMPOSE_FILE overlays + internal booleans on the HOST (same reason as
# --corporate-proxy above: it edits .env / generates the box-local overlay and
# needs the host docker socket to enumerate services).
#   rzfz setup --network-mode --mode online|proxied|offline
#   rzfz setup --network-mode --status
#   rzfz setup --network-mode --check
if [ "${1:-}" = "--network-mode" ]; then
    shift
    exec "${SCRIPT_DIR}/scripts/apply-network-mode.sh" "$@"
fi

# #22: the razzfazz-setup WEB container has been removed. The CLI backend
# now runs on the HOST (pure-stdlib Python under cli/setup_lib/). We export
# RAZZFAZZ_STACK_ROOT so the backend's .env / certs / governance-DB paths
# resolve to this repo's root instead of the old in-container /stack.
if ! command -v python3 &> /dev/null; then
    print_error "python3 is not installed (required by the setup CLI)"
    exit 1
fi

export RAZZFAZZ_STACK_ROOT="$SCRIPT_DIR"
exec python3 "${SCRIPT_DIR}/cli/setup_lib/setup.py" "$@"
