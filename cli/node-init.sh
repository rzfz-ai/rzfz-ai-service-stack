#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# rzfz node-init — install this box as a THIN inference node (#549 R0).
#
#   rzfz node-init --manager https://llm-manager.example.com \
#                  --token <enrollment-token> \
#                  [--name <worker-name>] [--ca-pin sha256:<hex>] \
#                  [--hardware amd|cpu|nvidia] [--advertise <host:port>] \
#                  [--registry <host:port>]
#
# What a thin node is: the worker-agent + a scoped docker-socket-proxy + the
# engines the master deploys. NOT a stack — no Caddy, no Authentik, no
# Postgres, no portals; machine-authenticated, managed from the master,
# CLI-only on the box. This command is the whole local install:
#
#   1. write .env.node from config/node.env.example + the arguments
#   2. enrol via the SAME worker-join flow a full box uses (CA pin and all),
#      pointed at .env.node — one enrolment implementation, not two
#   3. print the compose command that starts the node
#
# It deliberately does NOT require `rzfz init` — that requirement was the seam
# #419 named: the thin path existed in principle and was impossible in
# practice, because enrolment demanded a full-stack .env first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# ------------------------------------------------------------------------------
# #427: unconditional run log — the same preamble the other box-mutating CLI
# entry points carry. node-init is a first-install, box-mutating surface of
# exactly that class (writes .env.node, performs token enrolment, invokes
# worker-join), and a failed thin-node enrolment used to leave nothing but
# console scrollback.
# --help/-h runs stay un-teed: the command-reference generator captures their
# stdout verbatim and a help call must not mint a junk run log.
_rzfz_wants_help=false
for _rzfz_a in "$@"; do case "$_rzfz_a" in -h|--help|help) _rzfz_wants_help=true ;; esac; done
if [ "$_rzfz_wants_help" = false ] && [ -z "${RZFZ_RUN_LOG:-}" ]; then
    RZFZ_LOG_DIR="${RZFZ_LOG_DIR:-$HOME/.razzfazz/logs}"
    # review #653: the log carries env-adjacent output — operator-only.
    mkdir -p "$RZFZ_LOG_DIR" 2>/dev/null && chmod 700 "$RZFZ_LOG_DIR" 2>/dev/null || RZFZ_LOG_DIR="/tmp"
    export RZFZ_RUN_LOG="$RZFZ_LOG_DIR/razzfazz-node-init-$(date -u +%Y%m%dT%H%M%SZ).log"
    # retention: keep the last 10 runs per verb. #667: cleanup must never kill
    # the run (an empty glob makes ls exit 2 under pipefail).
    ls -1t "$RZFZ_LOG_DIR"/razzfazz-node-init-*.log 2>/dev/null | tail -n +11 | xargs -r rm -f -- || true
    exec > >(tee -a "$RZFZ_RUN_LOG") 2>&1
    echo "[log] full run log: $RZFZ_RUN_LOG"
    trap 'echo "[log] full run log: $RZFZ_RUN_LOG"' EXIT
fi

# shellcheck source=/dev/null
source "${SCRIPT_DIR}/scripts/lib.sh"

ENV_FILE=".env.node"
TEMPLATE="config/node.env.example"
MANAGER=""; TOKEN=""; NAME=""; CA_PIN=""; HARDWARE="amd"; ADVERTISE=""; REGISTRY=""

usage() {
    sed -n '5,12p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manager)   MANAGER="${2:?--manager needs a URL}"; shift 2 ;;
        --token)     TOKEN="${2:?--token needs a value}"; shift 2 ;;
        # #840 class: `${2:-}` + an unconditional `shift 2` exits 1 with NO
        # output when the flag is last on the command line (one positional
        # left to shift, under `set -euo pipefail`). These three take an
        # optional-looking value, so they get the `:?` guard the other flags
        # already have — the operator hears which flag was left dangling
        # instead of a silent exit 1.
        --name)      NAME="${2:?--name needs a value}"; shift 2 ;;
        --ca-pin)    CA_PIN="${2:?--ca-pin needs a fingerprint}"; shift 2 ;;
        --hardware)  HARDWARE="${2:?--hardware needs amd|cpu|nvidia}"; shift 2 ;;
        --advertise) ADVERTISE="${2:?--advertise needs a host:port}"; shift 2 ;;
        --registry)  REGISTRY="${2:?--registry needs a host:port}"; shift 2 ;;
        -h|--help)   usage 0 ;;
        *) echo "unknown arg: $1" >&2; usage 2 ;;
    esac
done
[ -n "$MANAGER" ] || { echo "ERROR: --manager is required." >&2; usage 2; }
[ -n "$TOKEN" ]   || { echo "ERROR: --token is required (mint one in Fleet → Add worker)." >&2; usage 2; }
# #1517 (E5): a GB10 is just an NVIDIA box — which llama.cpp binary it gets is
# decided on the node from the GPU's CUDA compute capability, not from a class
# name an operator has to know. The retired GB10 spellings are still accepted so
# a documented command keeps working; they normalise onto `nvidia`.
case "$HARDWARE" in
    gb10|nvidia-gb10|cuda-gb10)
        echo "NOTE: --hardware $HARDWARE is retired; a GB10 enrols as 'nvidia' and its" >&2
        echo "      runner is chosen from the GPU's compute capability (#1517)." >&2
        HARDWARE="nvidia" ;;
esac
case "$HARDWARE" in amd|cpu|nvidia) ;; *)
    echo "ERROR: --hardware must be amd, cpu or nvidia (got '$HARDWARE')." >&2; exit 2 ;;
esac

# ── 1. the minimal env ────────────────────────────────────────────────────────
if [ -f "$ENV_FILE" ]; then
    echo "  .env.node exists — keeping it (values below update in place)."
else
    [ -f "$TEMPLATE" ] || { echo "ERROR: $TEMPLATE missing — broken checkout/package." >&2; exit 1; }
    cp "$TEMPLATE" "$ENV_FILE"
    echo "  wrote ${ENV_FILE} from ${TEMPLATE}"
fi
# 0600 UNCONDITIONALLY, before anything secret is written into it. The template
# is 0644 in the tree, so `cp` lands .env.node world-readable — and worker-join
# then writes LLM_WORKER_COMMAND_KEY (this node's machine credential to the LLM
# Manager) into it, with LLM_WORKER_REGISTRY_PASSWORD added by the operator
# later. The full-stack installer chmods .env / .env.dify for exactly this
# reason; doing it unconditionally also repairs a pre-existing loose file.
chmod 600 "$ENV_FILE" 2>/dev/null || echo "  ⚠ could not chmod 600 ${ENV_FILE} — it will hold this node's command key."
update_env_value "$ENV_FILE" "HARDWARE" "$HARDWARE"
[ -n "$ADVERTISE" ] && update_env_value "$ENV_FILE" "LLM_WORKER_ADVERTISE_ADDR" "$ADVERTISE"
if [ -n "$REGISTRY" ]; then
    # OPS-5: default to https so the hub's Basic credentials (#559/#571) never
    # travel in cleartext — matching node.env.example, which the template above
    # already documents as https. Respect an explicit scheme if the operator
    # passed one (e.g. an air-gapped http mirror), but never silently downgrade a
    # bare host:port to http.
    case "$REGISTRY" in
        http://*|https://*) REGISTRY_URL="$REGISTRY" ;;
        *)                  REGISTRY_URL="https://${REGISTRY}" ;;
    esac
    update_env_value "$ENV_FILE" "LLM_REGISTRY_URL" "$REGISTRY_URL"
    # runner registry is a docker image-ref prefix — bare host:port, no scheme
    update_env_value "$ENV_FILE" "LLM_WORKER_RUNNER_REGISTRY" "${REGISTRY#*://}"
fi

# ── 2. enrol — the SAME flow a full box uses, pointed at .env.node ───────────
# worker-join verifies the CA pin, exchanges the token for the per-worker
# command key, and persists LLM_WORKER_COMMAND_KEY + LLM_MANAGER_URL (+ name)
# into the env file we just created. One enrolment implementation; this script
# adds no second one.
join_args=(--manager "$MANAGER" --token "$TOKEN" --env-file "$ENV_FILE")
[ -n "$NAME" ]   && join_args+=(--name "$NAME")
[ -n "$CA_PIN" ] && join_args+=(--ca-pin "$CA_PIN")
bash "${SCRIPT_DIR}/cli/worker-join.sh" "${join_args[@]}"

# ── 3. say exactly what starts the node ──────────────────────────────────────
# #571: a routed registry (the hub) is basic-authed — a set URL with no
# credential pair means every pull will 401 and the operator should hear it
# NOW, not from a failed deploy_runner three steps later.
if grep -qE '^LLM_REGISTRY_URL=.+' "$ENV_FILE" \
   && ! grep -qE '^LLM_WORKER_REGISTRY_USER=.+' "$ENV_FILE"; then
    echo "  ⚠ LLM_REGISTRY_URL is set but LLM_WORKER_REGISTRY_USER/_PASSWORD are"
    echo "    empty: a hub-edge registry refuses unauthenticated pulls (401)."
    echo "    Get the pair from the master: rzfz hub-credentials"
fi
if ! grep -qE '^LLM_REGISTRY_URL=.+' "$ENV_FILE"; then
    echo ""
    echo "  ⚠ LLM_REGISTRY_URL is unset: this node cannot pull weights or runner"
    echo "    images until the master's registry is reachable from here (see the"
    echo "    REACHABILITY note in ${ENV_FILE}). Pass --registry <host:port> once"
    echo "    the master exposes it on a routed address."
fi
echo ""
echo "Start the node:"
echo "    docker compose -p rzfz-node \\"
echo "        -f modules/llm/node-agent/compose.thin.yml \\"
echo "        --env-file ${ENV_FILE} up -d --build"
echo ""
echo "The worker appears in the console under Fleet once it registers"
echo "(pending approval if the master runs approval_mode=manual, #419)."
