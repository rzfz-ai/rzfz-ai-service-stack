#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# rzfz hub-credentials — mint the fleet hub's /v2 docker-API credential (#559).
#
# The hub edge (hub.<domain>) forward_auths its /v2 path to the manager's
# /api/hub-auth, which checks HTTP Basic against LLM_HUB_REGISTRY_USER/
# _PASSWORD from .env. Until a password exists the edge is FAIL-CLOSED (401
# for everyone), so this command is the explicit provisioning step:
#   1. generate a high-entropy hex password (interpolation-safe: no $ or
#      quotes, so compose env handling can never mangle it),
#   2. write it into .env (update_env_value — inode-preserving),
#   3. restart llm-manager so the gate reads the new value,
#   4. print the docker-login instruction for nodes/operators.
#
# --rotate replaces an existing password (every consumer must re-login).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${SCRIPT_DIR}"
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

# #1661: argument handling BEFORE anything reads or writes the box. The script
# used to inspect exactly one argument (`--rotate`) and treat everything else —
# including `--help` — as "provision". On a box whose password is still empty,
# which is every box before this command has been run, `rzfz hub-credentials
# --help` therefore MINTED A CREDENTIAL. `--help` is the one flag an operator
# types when they are not sure what a command does; it has to be a read.
#
# It also made `scripts/generate-command-reference.py` bake the generating
# machine's provisioning state into docs/enterprise/reference/commands.md — the
# doc drift that surfaced this.
usage() { sed -n '5,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
ROTATE=false
# #2500 fix round 1: internal flag for OTHER rzfz scripts (cli/upgrade.sh),
# not advertised in usage() — it changes nothing an interactive operator would
# ever want. upgrade.sh's own Step 8 (restart_stack()) runs `docker compose up
# -d --force-recreate` right after minting — an UNCONDITIONAL recreate of
# every container, which already covers llm-manager/llm-worker-agent picking
# up the changed .env value; this script's own recreate at that point in the
# upgrade would be redundant (and premature — it runs before the new images
# are even pulled). --no-restart lets upgrade.sh reuse this script's mint +
# mirror + partial-state-repair logic (single source of truth, no more
# duplicated writes) without also triggering that premature restart.
NO_RESTART=false
for _arg in "$@"; do
    case "${_arg}" in
        -h|--help)     usage; exit 0 ;;
        --rotate)      ROTATE=true ;;
        --no-restart)  NO_RESTART=true ;;
        "")            : ;;
        *)             echo "rzfz hub-credentials: unknown argument '${_arg}'" >&2
                       echo "" >&2; usage >&2; exit 2 ;;
    esac
done

ENV_FILE=".env"
[ -f "${ENV_FILE}" ] || { echo "ERROR: no .env — run on an installed box." >&2; exit 1; }

user="$(read_env_value "${ENV_FILE}" LLM_HUB_REGISTRY_USER)"
user="${user:-fleet}"
hub_domain="$(read_env_value "${ENV_FILE}" LLM_HUB_DOMAIN)"

existing="$(read_env_value "${ENV_FILE}" LLM_HUB_REGISTRY_PASSWORD)"
if [ -n "${existing}" ] && [ "${ROTATE}" != true ]; then
    # #2500 fix round 1: a HUB password can exist with the WORKER pair still
    # empty or stale — #559 (which minted only the hub pair) predates #1677
    # (which added the worker mirror), so any box provisioned in that window,
    # or one that had its hub password set by some other means, reaches this
    # branch with a partial state. Before this fix that state was invisible:
    # this branch printed "already provisioned" and exited, mirroring
    # NOTHING, and the co-located worker-agent 401ed forever with no warning
    # from anywhere (the caller saw exit 0 and moved on). Repair it here,
    # silently, on every call that lands in this branch — mint NO new
    # password (that would force every already-logged-in consumer to
    # re-login for no reason); just copy the EXISTING hub credential over.
    worker_user="$(read_env_value "${ENV_FILE}" LLM_WORKER_REGISTRY_USER)"
    worker_pw="$(read_env_value "${ENV_FILE}" LLM_WORKER_REGISTRY_PASSWORD)"
    if [ "${worker_user}" != "${user}" ] || [ "${worker_pw}" != "${existing}" ]; then
        grep -q '^LLM_WORKER_REGISTRY_USER=' "${ENV_FILE}" || printf 'LLM_WORKER_REGISTRY_USER=\n' >> "${ENV_FILE}"
        grep -q '^LLM_WORKER_REGISTRY_PASSWORD=' "${ENV_FILE}" || printf 'LLM_WORKER_REGISTRY_PASSWORD=\n' >> "${ENV_FILE}"
        update_env_value "${ENV_FILE}" LLM_WORKER_REGISTRY_USER "${user}"
        update_env_value "${ENV_FILE}" LLM_WORKER_REGISTRY_PASSWORD "${existing}"
        echo "Hub credentials already provisioned for user '${user}'; the worker credential (#1677) was missing/stale — mirrored it from the existing hub password (no new password minted)."
        if [ "${NO_RESTART}" != true ] && docker inspect llm-worker-agent >/dev/null 2>&1; then
            echo "Recreating llm-worker-agent so it picks up the mirrored credential..."
            docker compose up -d llm-worker-agent >/dev/null 2>&1 \
                || echo "WARN: could not recreate llm-worker-agent — runner pulls keep the old credential." >&2
        fi
    else
        echo "Hub credentials already provisioned for user '${user}'."
    fi
    echo "Re-print the login command with: grep ^LLM_HUB_REGISTRY .env"
    echo "Replace the password with: rzfz hub-credentials --rotate"
    exit 0
fi

pw="$(openssl rand -hex 24)"
# Missing keys (box installed before #559): append rather than silently no-op —
# update_env_value only rewrites existing lines.
grep -q '^LLM_HUB_REGISTRY_USER=' "${ENV_FILE}" || printf 'LLM_HUB_REGISTRY_USER=%s\n' "${user}" >> "${ENV_FILE}"
grep -q '^LLM_HUB_REGISTRY_PASSWORD=' "${ENV_FILE}" || printf 'LLM_HUB_REGISTRY_PASSWORD=\n' >> "${ENV_FILE}"
update_env_value "${ENV_FILE}" LLM_HUB_REGISTRY_PASSWORD "${pw}"

# #1677: the CO-LOCATED worker-agent needs this credential too. Its runner pull
# is executed by the host's docker daemon, which cannot resolve the compose name
# `llm-registry` — so it goes to the hub domain, and the hub's /v2 is HTTP-Basic
# (measured on 0.91: 401 without a credential). Without this the fix to the
# coordinate merely swaps a DNS failure for a 401.
#
# `worker-join.sh` writes exactly this pair on a thin node; the master was the
# asymmetric case. Written into `.env` rather than defaulted in compose on
# purpose: the module compose's environment block is an allow-list whose values
# must stay empty-default (NODE-13 / #1037), and an operator can see a value in
# `.env`.
grep -q '^LLM_WORKER_REGISTRY_USER=' "${ENV_FILE}" || printf 'LLM_WORKER_REGISTRY_USER=\n' >> "${ENV_FILE}"
grep -q '^LLM_WORKER_REGISTRY_PASSWORD=' "${ENV_FILE}" || printf 'LLM_WORKER_REGISTRY_PASSWORD=\n' >> "${ENV_FILE}"
update_env_value "${ENV_FILE}" LLM_WORKER_REGISTRY_USER "${user}"
update_env_value "${ENV_FILE}" LLM_WORKER_REGISTRY_PASSWORD "${pw}"

echo "Hub credential written to .env (user '${user}')."
echo "  … and mirrored to LLM_WORKER_REGISTRY_USER/PASSWORD so the local"
echo "    worker-agent can pull runner images from the hub (#1677)."
if [ "${NO_RESTART}" = true ]; then
    echo "NOTE: --no-restart — the caller recreates the affected container(s) itself."
elif docker inspect llm-manager >/dev/null 2>&1; then
    # #568 review R3 (proven live): `compose restart` keeps the OLD container
    # env — the mint would print success while the gate stays fail-closed.
    # `up -d` recreates on env diff, which is what actually injects the value.
    echo "Recreating llm-manager so the gate picks up the new credential..."
    docker compose up -d llm-manager >/dev/null 2>&1 \
        || echo "WARN: could not recreate llm-manager — run 'docker compose up -d llm-manager' before logging in." >&2
    # Same reason for the agent: it reads the credential from its ENV, and a
    # `restart` would keep the old one (#568 review R3, proven live).
    if docker inspect llm-worker-agent >/dev/null 2>&1; then
        docker compose up -d llm-worker-agent >/dev/null 2>&1 \
            || echo "WARN: could not recreate llm-worker-agent — runner pulls keep the old credential." >&2
    fi
else
    echo "NOTE: llm-manager is not running here — the credential takes effect when it starts."
fi

echo
echo "NOTE: the hub's SSO'd web UI needs the Authentik provider (34-hub.yaml);"
echo "      after first enablement the outpost takes ~30-60s to converge —"
echo "      early probes 404, then settle. The /v2 docker API is independent."
echo "On each consumer (worker node, operator workstation):"
echo "    docker login ${hub_domain:-hub.<your-domain>} -u ${user}"
echo "    (password: grep ^LLM_HUB_REGISTRY_PASSWORD= .env   on the master)"
[ "${ROTATE}" = true ] && echo "ROTATED: every previously logged-in consumer must docker login again."
exit 0
