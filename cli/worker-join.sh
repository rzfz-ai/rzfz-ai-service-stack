#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz.ai Stack - Worker Join (#419 P0)
# ==============================================================================
# The NODE side of enrollment. An admin mints a join command in the LLM Manager
# console (Fleet → Add worker); the operator pastes it on the TARGET box:
#
#   rzfz worker-join --manager https://llm-manager.example.com \
#                    --token <enrollment-token> \
#                    --name worker-01 \
#                    --ca-pin sha256:<64 hex>
#
# What it does:
#   1. VERIFIES the master before talking to it, if --ca-pin was supplied.
#   2. POSTs the enrollment token to /api/workers/enroll.
#   3. Persists ONLY the per-worker command key returned by the master.
#
# ------------------------------------------------------------------------------
# Two properties this script exists to guarantee — both fail silently if broken:
#
#   * A PIN MISMATCH REFUSES. The join credential travels by copy-paste, over
#     chat or a ticket, and the node has no prior trust in the master. Without
#     the pin the node adopts whoever answers the advertised address; a pin that
#     warns instead of refusing is decoration.
#
#   * THE SHARED NODE KEY IS NEVER WRITTEN HERE (#285). The master derives every
#     worker's command key from it, so a node holding it could impersonate every
#     other worker — precisely the isolation the key split creates. In `enforce`
#     mode the master withholds it; in `allow` mode it still sends `node_key`
#     for back-compat, and this script drops it on the floor regardless.
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"
cd "$SCRIPT_DIR"

VERIFIED_CA=""
VERIFY_DIR=""
HTTP_BODY=""
HTTP_CODE_FILE=""

# ONE EXIT trap for every temp path this script creates.
#
# bash EXIT traps REPLACE, they do not accumulate: a second `trap ... EXIT`
# silently discards the first. This script had two, so the later one (the
# request scratch files) cancelled the earlier one, and the directory holding
# the fetched CA + presented chain was never removed — leaked on every pinned
# join, which is the opposite of what its comment claimed. On a box where /tmp
# is a small tmpfs that accumulates.
_cleanup() {
    [ -n "$VERIFY_DIR" ] && rm -rf "$VERIFY_DIR"
    [ -n "$HTTP_BODY" ] && rm -f "$HTTP_BODY" "${HTTP_BODY}.err"
    [ -n "$HTTP_CODE_FILE" ] && rm -f "$HTTP_CODE_FILE"
    return 0
}
trap _cleanup EXIT

MANAGER=""
TOKEN=""
NAME=""
CA_PIN=""
ENV_FILE=".env"

usage() {
    cat <<'EOF'
rzfz.ai Stack - Join this box to an LLM Manager as a worker

Usage:
  rzfz worker-join --manager <url> --token <token> [--name <name>] [--ca-pin sha256:<hex>]

Required:
  --manager <url>        LLM Manager base URL (https://llm.<domain>)
  --token <token>        Enrollment token minted by the admin (short-lived)

Optional:
  --name <name>          Preferred worker name. The TOKEN's name wins if they
                         differ — the master will not hand out another worker's
                         credential (#285).
  --ca-pin sha256:<hex>  Fingerprint of the master's CA. The master's leaf
                         certificate must chain to it or the join is REFUSED.
  --env-file <path>      Where to persist the command key (default: .env)
  --force                Allow this box to be enrolled under a DIFFERENT name
                         than it already carries. Without it a rename is
                         refused: the command key is HMAC(node_key, name), so
                         the old identity cannot be restored, and the previous
                         worker goes stale at the next agent restart with its
                         deployments orphaned (#1773).
  -h, --help             Show this help

Get the whole command, pin included, from the LLM Manager console:
  Fleet → Add worker.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manager)  MANAGER="${2:?--manager needs a URL}"; shift 2 ;;
        --token)    TOKEN="${2:?--token needs a value}"; shift 2 ;;
        --name)     NAME="${2:?--name needs a value}"; shift 2 ;;
        --ca-pin)   CA_PIN="${2:?--ca-pin needs a fingerprint}"; shift 2 ;;
        --env-file) ENV_FILE="${2:?--env-file needs a path}"; shift 2 ;;
        --force)    FORCE_RENAME=true; shift ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; echo >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$MANAGER" ] || [ -z "$TOKEN" ]; then
    echo "ERROR: --manager and --token are both required." >&2
    echo >&2
    usage >&2
    exit 2
fi

MANAGER="${MANAGER%/}"

# --- 1. verify the master BEFORE handing it anything ---------------------------
# HOW THIS WORKS, and why it is not "find the pinned cert in the TLS chain".
#
# The pin is a FINGERPRINT, and you cannot verify a certificate chain against a
# hash — you need the CA's bytes. Caddy presents leaf + intermediate and NOT its
# root, so the pinned root never appears in the handshake at all. The first
# implementation searched the presented chain for it and therefore REFUSED every
# legitimate master (measured against 0.91: 2 certs presented, neither the root).
#
# So: fetch the CA from the master, authenticate those bytes by hashing them and
# comparing to the pin we already hold, then require the presented leaf to verify
# against it. Same shape as kubeadm's --discovery-token-ca-cert-hash.
#
# Fetching over the untrusted connection is sound. Serving a forged CA fails the
# hash comparison. Replaying the REAL CA gains nothing, because step 3 needs a
# leaf actually signed by it — which needs the CA's private key.
verify_ca_pin() {
    local url=$1 pin=$2
    local host port hostport
    hostport="${url#*://}"; hostport="${hostport%%/*}"
    host="${hostport%%:*}"
    port="${hostport##*:}"
    [ "$port" = "$host" ] && port=443

    if ! command -v openssl >/dev/null 2>&1; then
        echo "ERROR: --ca-pin was supplied but openssl is not installed." >&2
        echo "       Refusing to join unverified. Install openssl and retry." >&2
        exit 2
    fi

    local dir; dir=$(mktemp -d)
    # NOT cleaned on RETURN: the verified CA becomes the trust anchor for the
    # enrolment request below. Removed by the EXIT trap instead.
    VERIFY_DIR="$dir"

    # 1. the chain the master actually presents (leaf first).
    #
    # s_client's EXIT CODE is not a usable signal here: it exits non-zero
    # whenever it cannot verify the peer, which is the NORMAL case — the
    # master's CA is precisely what we do not trust yet. Judge on whether a
    # certificate arrived.
    openssl s_client -connect "${host}:${port}" -servername "$host" \
        -showcerts </dev/null 2>/dev/null > "$dir/chain.pem" || true
    if ! grep -q 'BEGIN CERTIFICATE' "$dir/chain.pem" 2>/dev/null; then
        echo "ERROR: could not retrieve the TLS chain from ${host}:${port}." >&2
        echo "       Check the host resolves from this box and 443 is reachable." >&2
        exit 2
    fi
    awk -v d="$dir" '/BEGIN CERT/{n++} n{print > (d "/cert" n ".pem")}' "$dir/chain.pem"

    # 2. fetch the CA and AUTHENTICATE it against the pin before trusting a byte.
    if ! curl -fsSk --max-time 30 "${url}/api/workers/ca.pem" -o "$dir/ca.pem" \
         || ! grep -q 'BEGIN CERTIFICATE' "$dir/ca.pem" 2>/dev/null; then
        echo "" >&2
        echo "  ERROR: the master did not serve its CA at ${url}/api/workers/ca.pem" >&2
        echo "  A pin cannot be checked without it. Either the master predates" >&2
        echo "  this endpoint, or it has no local CA (a Let's Encrypt box has" >&2
        echo "  nothing to pin — drop --ca-pin there and rely on the public PKI)." >&2
        exit 2
    fi
    local got
    got="sha256:$(openssl x509 -in "$dir/ca.pem" -outform DER 2>/dev/null \
                  | openssl dgst -sha256 -hex 2>/dev/null | awk '{print $NF}')"

    if [ "$got" != "$pin" ]; then
        echo "" >&2
        echo "  PIN MISMATCH — refusing to join." >&2
        echo "" >&2
        echo "  Expected CA fingerprint : $pin" >&2
        echo "  Served by the master    : $got" >&2
        echo "" >&2
        echo "  Either the join command is stale, or something is answering on" >&2
        echo "  the master's address that is not the master." >&2
        echo "" >&2
        echo "  Re-copy the join command from Fleet -> Add worker. Do NOT re-run" >&2
        echo "  this without --ca-pin to 'get past' it." >&2
        exit 1
    fi

    # 3. the presented leaf must actually chain to that CA. Without this, anyone
    # could serve the genuine CA bytes while fronting their own certificate.
    if ! openssl verify -CAfile "$dir/ca.pem" -untrusted "$dir/chain.pem" \
            "$dir/cert1.pem" >/dev/null 2>&1; then
        echo "" >&2
        echo "  PIN MISMATCH — the pinned CA did not sign the presented certificate." >&2
        echo "" >&2
        echo "  The master served a CA matching the pin, but its own certificate" >&2
        echo "  does not chain to it. That is what a relay replaying a genuine CA" >&2
        echo "  in front of its own certificate looks like. Refusing to join." >&2
        exit 1
    fi

    # The whole point of authenticating the CA is to then USE it. Everything
    # after this speaks TLS pinned to it rather than trusting the system store
    # (which does not contain an internal root) or disabling verification.
    VERIFIED_CA="$dir/ca.pem"
    echo "  ✓ master verified: CA matches the pin and signed the presented cert"
}

echo "Joining ${MANAGER} as a worker..."

if [ -n "$CA_PIN" ]; then
    if ! printf '%s' "$CA_PIN" | grep -qE '^sha256:[0-9a-f]{64}$'; then
        echo "ERROR: --ca-pin must look like sha256:<64 lowercase hex>." >&2
        echo "       Got: $CA_PIN" >&2
        exit 2
    fi
    verify_ca_pin "$MANAGER" "$CA_PIN"
else
    echo "" >&2
    echo "  WARNING: no --ca-pin given — the master is NOT verified." >&2
    echo "  This box will trust whatever answers at ${MANAGER}, and will send it" >&2
    echo "  the enrollment token. Only proceed on a network you control." >&2
    echo "  The console emits a pin with the join command; prefer that." >&2
    echo "" >&2
fi

# --- 2. exchange the enrollment token for a per-worker command key -------------
# Build the body with a real JSON encoder — never string-interpolate. A `"` or
# `\` in the operator-supplied --name produced malformed JSON or injected an
# extra field into the enrolment request; cli/rename-hex-oidc-users.sh does
# this correctly two files over ("never string-interpolate").
#
# Both values travel by ENVIRONMENT, not argv: the enrolment token is a
# credential and argv is world-readable through /proc/<pid>/cmdline. (The old
# `printf` was a bash BUILTIN, so it spawned no process and leaked nothing —
# switching to an external encoder must not lose that property.)
payload=$(RZFZ_JOIN_TOKEN="$TOKEN" RZFZ_JOIN_NAME="$NAME" python3 -c '
import json, os, sys
body = {"token": os.environ["RZFZ_JOIN_TOKEN"]}
name = os.environ.get("RZFZ_JOIN_NAME", "")
if name:
    body["name"] = name
sys.stdout.write(json.dumps(body, separators=(",", ":")))
')

http_body=$(mktemp); http_code_file=$(mktemp)
# Registered with the single trap above rather than installing a second one.
HTTP_BODY="$http_body"; HTTP_CODE_FILE="$http_code_file"

# TLS for the exchange itself:
#   pinned  -> verify against the CA we just authenticated by fingerprint. This
#              is stricter than the system store, which cannot contain an
#              internal root, and it is why --ca-pin is worth supplying.
#   no pin  -> the operator was already warned the master is unverified; -k is
#              consistent with that, and refusing here would just make the
#              pin-less path unusable on every self-signed box.
tls_args=()
if [ -n "$VERIFIED_CA" ]; then
    tls_args=(--cacert "$VERIFIED_CA")
else
    tls_args=(-k)
fi

set +e
curl -fsS "${tls_args[@]}" -o "$http_body" -w '%{http_code}' \
     -H 'Content-Type: application/json' \
     -X POST "${MANAGER}/api/workers/enroll" \
     --data "$payload" > "$http_code_file" 2>"${http_body}.err"
curl_rc=$?
set -e
code=$(cat "$http_code_file" 2>/dev/null || echo 000)

if [ "$curl_rc" -ne 0 ] || [ "$code" != "200" ]; then
    echo "ERROR: enrollment failed (HTTP ${code:-000})." >&2
    case "$code" in
        401) echo "       The token is invalid or has expired. They are short-lived —" >&2
             echo "       mint a fresh one in Fleet → Add worker." >&2 ;;
        503) echo "       The master has no node key configured, so node registration" >&2
             echo "       is disabled there." >&2 ;;
        000) echo "       Could not reach ${MANAGER}." >&2
             sed 's/^/       /' "${http_body}.err" >&2 2>/dev/null || true ;;
        *)   sed 's/^/       /' "$http_body" >&2 2>/dev/null || true ;;
    esac
    rm -f "${http_body}.err"
    exit 1
fi
rm -f "${http_body}.err"

# jq is not a stack dependency; pull the two fields we need with python3, which
# every box has (the stack's own tooling requires it).
node_name=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("node_name",""))' "$http_body")
command_key=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("command_key",""))' "$http_body")
registry_user=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("registry_user",""))' "$http_body")
registry_password=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("registry_password",""))' "$http_body")

if [ -z "$command_key" ]; then
    echo "ERROR: the master did not return a command key. Nothing was written." >&2
    exit 1
fi

# --- 3. persist ONLY the per-worker credential --------------------------------
# Deliberately NOT reading .env with `source`: operator-edited values carry
# spaces, quotes and shell metacharacters, and sourcing executes them.
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE does not exist — run 'rzfz init' on this box first." >&2
    exit 1
fi

# --- 3a. #1773: an enrolment REPLACES an identity — keep the old one ----------
#
# The two writes below replace LLM_WORKER_NAME and LLM_WORKER_COMMAND_KEY in
# place. That is not a settings change, it is a new identity: the command key is
# HMAC(node_key, the worker NAME) (post-install.sh:672), so the pair only ever
# works together and the old key cannot be reconstructed once it is gone.
#
# The damage is LATENT, which is what makes it expensive. The running agent
# keeps the old values in memory and goes on working; the manager still shows
# the old worker `ready` with its instance. Only at the next agent or box
# restart does it register as a NEW worker — the old row goes stale and its
# deployments are orphaned. Weeks can pass between cause and effect, and by then
# nobody connects it to an installer run (the #1590 shape: a tool replaces
# something unrecoverable, nothing looks wrong, someone else pays later).
#
# So: copy the file first, and SAY what is being replaced. `cp -p` keeps mode
# and ownership — the file holds a credential and is 0600 root.
_worker_join_preserve_prior_identity() {
    # $1 = env file, $2 = the name the master just issued.
    # Prints the backup path on stdout (empty when there was nothing to keep),
    # and the operator-facing warning on stderr.
    local env_file="$1" new_name="$2" prior backup
    prior=$(read_env_value "$env_file" "LLM_WORKER_NAME" 2>/dev/null || true)
    [ -n "$prior" ] || return 0
    # A second-resolution stamp is not unique enough. Two enrolments in the SAME
    # second — never by hand, routinely from a retry loop — would land on the
    # same path and the second `cp` would overwrite the first copy, which is the
    # only remaining record of the identity being replaced. So: take the first
    # UNUSED name. `cp -n` alone does not do it (it returns 0 without copying,
    # so the caller would read success and go on to overwrite the identity).
    local stamp suffix=0
    stamp="${env_file}.$(date -u +%Y%m%dT%H%M%SZ)"
    backup="$stamp"
    while [ -e "$backup" ]; do
        suffix=$((suffix + 1))
        [ "$suffix" -le 99 ] || return 1     # something is very wrong; do not guess
        backup="${stamp}-${suffix}"
    done
    cp -p "$env_file" "$backup" || return 1
    printf '%s\n' "$backup"
    if [ -n "$new_name" ] && [ "$prior" != "$new_name" ]; then
        echo "" >&2
        echo "  WARNING: this box was already enrolled as '${prior}'." >&2
        echo "  Enrolling as '${new_name}' replaces that identity. The command key is" >&2
        echo "  HMAC(node_key, worker name), so the old key stops working and cannot be" >&2
        echo "  restored by writing the old name back." >&2
        echo "  '${prior}' will go STALE at the next agent restart and its deployments" >&2
        echo "  will be orphaned — the running agent keeps working until then, so" >&2
        echo "  nothing will look broken in the meantime." >&2
        echo "  Previous values kept at: ${backup}" >&2
        echo "" >&2
    fi
}
# --- 3a-i. #1773 point 3: a RENAME is not a repeat, so it is confirmed ---------
#
# Cut by the NAME, not by the run. The two cases cost differently:
#
#   same name   the legitimate repair. A box whose command key no longer matches
#               fetches a new one, the identity stays, nothing goes stale.
#               Gating THAT would hit the box that needs the command most — and
#               a gate that blocks the repair gets worked around with --force
#               the first time and then always.
#   new name    never a repeat. Irreversible (the key is HMAC over the name),
#               latent (the running agent keeps working, so it looks fine until
#               the next restart), and nobody does it by accident AND on purpose
#               at once. A deliberate confirmation is the right bar here.
#
# Same shape as the origin preflight in cli/upgrade.sh: gate the one state that
# does not match what the call assumes, not every run.
#
# A function, not an inline `if`, so the decision can be RUN by a test rather
# than read out of the source (#1805: a guard one level above the effect proves
# nothing). tests/unit/scripts/test_1773_… extracts and calls it.
_worker_join_refuse_a_rename() {
    # $1 = env file, $2 = the name the master just issued, $3 = "true" for --force.
    # Returns 0 to continue, non-zero to abort. Operator text goes to stderr.
    local env_file="$1" new_name="$2" forced="$3" prior
    prior=$(read_env_value "$env_file" "LLM_WORKER_NAME" 2>/dev/null || true)
    [ -n "$prior" ]     || return 0     # first enrolment on this box
    [ -n "$new_name" ]  || return 0     # master issued no name — nothing to compare
    [ "$prior" != "$new_name" ] || return 0   # same name: the legitimate re-key

    if [ "$forced" = true ]; then
        echo "" >&2
        echo "  --force: enrolling as '${new_name}' although this box is '${prior}'." >&2
        echo "  '${prior}' will go stale at the next agent restart." >&2
        echo "" >&2
        return 0
    fi

    echo "" >&2
    echo "  REFUSING: this box is enrolled as '${prior}', and the master issued the" >&2
    echo "  name '${new_name}'. That is a RENAME, not a re-enrolment." >&2
    echo "" >&2
    echo "  The command key is HMAC(node_key, worker name), so the old key stops" >&2
    echo "  working and cannot be restored by writing the old name back. '${prior}'" >&2
    echo "  would go STALE at the next agent restart and its deployments would be" >&2
    echo "  orphaned — and until then nothing looks broken, which is what makes it" >&2
    echo "  expensive." >&2
    echo "" >&2
    echo "  If you mean it:      rzfz worker-join … --force" >&2
    echo "  To re-key '${prior}': ask the master to issue that same name." >&2
    echo "" >&2
    return 1
}

_worker_join_refuse_a_rename "$ENV_FILE" "$node_name" "${FORCE_RENAME:-false}" || exit 1

_WORKER_JOIN_ENV_BACKUP=$(_worker_join_preserve_prior_identity "$ENV_FILE" "$node_name") || {
    echo "ERROR: could not back up $ENV_FILE before replacing the enrolment." >&2
    echo "       Nothing was written. Check permissions on $(dirname "$ENV_FILE")." >&2
    exit 1
}

update_env_value "$ENV_FILE" "LLM_WORKER_COMMAND_KEY" "$command_key"
update_env_value "$ENV_FILE" "LLM_MANAGER_URL" "$MANAGER"
[ -n "$node_name" ] && update_env_value "$ENV_FILE" "LLM_WORKER_NAME" "$node_name"

# --- 3b. persist the trust anchor too (#585) ----------------------------------
# We just authenticated the master's CA against the pin and used it for the
# enrolment request — and then threw it away in the EXIT trap. The agent
# container that starts next speaks https://<master> with the SYSTEM truststore,
# which contains no internal root, so every call fails with
# `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`.
#
# This never showed on the master itself: its embedded worker-agent talks
# in-network `http://llm-manager:8080`. A thin node is the first consumer that
# actually crosses the TLS edge.
#
# The file is the master CA FOLLOWED BY this box's public roots, not the master
# CA alone. A node that trusted only the internal CA would verify the master and
# nothing else — and a node does reach public TLS (image and weight pulls that
# are not proxied through the master). One anchor file, both jobs; OpenSSL
# ignores the comment lines between the blocks.
#
# Only when a pin was actually verified. A master on Let's Encrypt has no
# internal CA to pin, and there the system store is already the right answer —
# writing a file would only add something to keep in sync.
if [ -n "${VERIFIED_CA:-}" ] && [ -f "$VERIFIED_CA" ]; then
    ca_dir="$(cd "$(dirname "$ENV_FILE")" && pwd)/certs"
    ca_out="${ca_dir}/master-ca.pem"
    mkdir -p "$ca_dir"
    {
        echo "# rzfz.ai thin node — trust anchors (#585). GENERATED by rzfz"
        echo "# worker-join; re-running the join rewrites it."
        echo "#"
        echo "# 1) ${MANAGER}'s CA, authenticated against the join pin:"
        echo "#    ${CA_PIN:-<no pin>}"
        cat "$VERIFIED_CA"
        if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
            echo "# 2) this box's public roots, so the node can still reach"
            echo "#    public TLS endpoints (image + weight pulls)."
            cat /etc/ssl/certs/ca-certificates.crt
        fi
    } > "$ca_out"
    chmod 644 "$ca_out"
    update_env_value "$ENV_FILE" "LLM_WORKER_CA_FILE" "$ca_out"
    echo "  ✓ master CA persisted to ${ca_out}"
fi

# --- 3b2. the agent image (#1438, E2) -------------------------------------------
# A thin node never builds razzfazz-llm-worker-agent; the master publishes it
# under <hub>/node/ at post-install and this pulls it — through a throw-away
# DOCKER_CONFIG so the hub pair never lands in the box's docker config. An
# in-network registry (co-located worker) means the image is local already.
RZFZ_NODE_REPO_PREFIX="node"
RZFZ_AGENT_IMAGE="razzfazz-llm-worker-agent:latest"
# rev-B (#1438, review befund 1-4): an EMPTY LLM_WORKER_RUNNER_REGISTRY means
# "not configured", NOT "in-network" — `cli/node-init.sh` writes the key only
# with `--registry`, and `config/node.env.example` ships it empty, so the plain
# invocation hit the in-network branch and printed a green tick while no image
# existed on the box. Only the literal in-network registry may skip the check;
# every other case (empty, or a real remote) must end with a local image or say
# loudly that the service cannot start. `_JOIN_AGENT_IMAGE_MISSING` carries that
# to the closing banner (befund 3) — a `✗` on stderr followed by "enrolled" on
# stdout reads as success in a log.
_JOIN_AGENT_IMAGE_MISSING=""

_join_agent_image_remedy() {
    echo "    Build/publish it on the master ('rzfz post-install --refresh' — it builds and" >&2
    echo "    pushes node/ images), then re-join; or point this node at the hub with" >&2
    echo "    'rzfz node-init --registry hub.<master-domain> …' (writes LLM_WORKER_RUNNER_REGISTRY)." >&2
}

# The node's own architecture, in Docker's vocabulary (amd64/arm64/…): a
# single-arch image published by an x86 master pulls happily on a GB10 and then
# dies with `exec format error` at start (befund 2). Docker only warns.
_join_docker_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo "amd64" ;;
        aarch64|arm64) echo "arm64" ;;
        armv7l) echo "arm" ;;
        *) uname -m ;;
    esac
}

_join_check_agent_image_arch() {
    local have want
    have="$(docker image inspect "$RZFZ_AGENT_IMAGE" --format '{{.Architecture}}' 2>/dev/null)" || return 0
    want="$(_join_docker_arch)"
    [ -n "$have" ] && [ -n "$want" ] || return 0
    if [ "$have" != "$want" ]; then
        _JOIN_AGENT_IMAGE_MISSING="arch"
        echo "  ✗ agent image ${RZFZ_AGENT_IMAGE} is ${have}, this node is ${want} — the container would" >&2
        echo "    fail with 'exec format error'. The master published a single-arch manifest." >&2
        _join_agent_image_remedy
    fi
}

_join_pull_agent_image() {
    local registry user pw ref cfg
    registry="$(read_env_value "$ENV_FILE" LLM_WORKER_RUNNER_REGISTRY)"
    if [ "$registry" = "llm-registry:5000" ]; then
        echo "  ✓ agent image: in-network registry — using the local ${RZFZ_AGENT_IMAGE}"
        _join_check_agent_image_arch
        return 0
    fi
    if [ -z "$registry" ]; then
        # Not configured: nothing to pull from. A local image is fine (full box
        # acting as a worker, or an operator who loaded it); nothing is not.
        if docker image inspect "$RZFZ_AGENT_IMAGE" >/dev/null 2>&1; then
            echo "  ✓ agent image: no registry configured — using the local ${RZFZ_AGENT_IMAGE}"
            _join_check_agent_image_arch
        else
            _JOIN_AGENT_IMAGE_MISSING="absent"
            echo "  ✗ agent image: no LLM_WORKER_RUNNER_REGISTRY configured and no local" >&2
            echo "    ${RZFZ_AGENT_IMAGE} on this box — the worker service cannot start." >&2
            _join_agent_image_remedy
        fi
        return 0
    fi
    user="$(read_env_value "$ENV_FILE" LLM_WORKER_REGISTRY_USER)"
    pw="$(read_env_value "$ENV_FILE" LLM_WORKER_REGISTRY_PASSWORD)"
    ref="${registry}/${RZFZ_NODE_REPO_PREFIX}/${RZFZ_AGENT_IMAGE}"
    cfg="$(mktemp -d)"
    # befund 4: the throwaway config holds the base64 pair until `rm -rf`; a
    # Ctrl-C between login and cleanup would leave it in /tmp.
    trap 'rm -rf "$cfg"' RETURN
    if [ -n "$user" ] && [ -n "$pw" ]; then
        printf '%s' "$pw" | DOCKER_CONFIG="$cfg" docker login "$registry" -u "$user" --password-stdin >/dev/null 2>&1 || true
    fi
    if DOCKER_CONFIG="$cfg" docker pull "$ref" >/dev/null 2>&1 && docker tag "$ref" "$RZFZ_AGENT_IMAGE" >/dev/null 2>&1; then
        echo "  ✓ agent image pulled from ${ref} and tagged ${RZFZ_AGENT_IMAGE}"
        _join_check_agent_image_arch
    elif docker image inspect "$RZFZ_AGENT_IMAGE" >/dev/null 2>&1; then
        echo "  ! agent image: pull from ${ref} failed — using the local ${RZFZ_AGENT_IMAGE} (may be stale)." >&2
        _join_check_agent_image_arch
    else
        _JOIN_AGENT_IMAGE_MISSING="absent"
        echo "  ✗ agent image: pull from ${ref} failed and no local ${RZFZ_AGENT_IMAGE} exists — the worker service" >&2
        echo "    cannot start." >&2
        _join_agent_image_remedy
    fi
    rm -rf "$cfg"
    return 0
}

# --- 3c. hub credentials (#1408 rev-B) ----------------------------------------
# The hub's docker/blob API is HTTP-Basic at the edge (#571). Before this step
# nothing wrote LLM_WORKER_REGISTRY_USER/PASSWORD on a joining node, so every
# REMOTE runner-image or weight pull 401'd until an operator copied the pair by
# hand from `rzfz hub-credentials`. The master hands the pair over in the same
# token-gated exchange that carries the command key; a master that has not
# minted one sends nothing, and this says so instead of writing an empty pair
# over a value an operator may have set.
if [ -n "${registry_password:-}" ]; then
    update_env_value "$ENV_FILE" "LLM_WORKER_REGISTRY_USER" "${registry_user:-fleet}"
    update_env_value "$ENV_FILE" "LLM_WORKER_REGISTRY_PASSWORD" "$registry_password"
    echo "  ✓ hub credentials written to ${ENV_FILE} (user ${registry_user:-fleet})"
else
    echo "  ! the master handed over no hub credentials: every REMOTE image/weight pull from" >&2
    echo "    the hub will 401 until 'rzfz hub-credentials' has run on the master and this" >&2
    echo "    box re-joins (or the pair is set in ${ENV_FILE} by hand)." >&2
fi
_join_pull_agent_image
echo ""
echo "  ✓ enrolled as '${node_name:-<unnamed>}'"
[ -n "${_WORKER_JOIN_ENV_BACKUP:-}" ] && echo "  ✓ previous enrolment kept at ${_WORKER_JOIN_ENV_BACKUP}"
echo "  ✓ per-worker command key written to ${ENV_FILE}"
if [ -n "${_JOIN_AGENT_IMAGE_MISSING:-}" ]; then
    # rev-B befund 3: the ✗ above goes to stderr and "enrolled" to stdout — in a
    # log the success line is the last thing anyone reads. Say it here too.
    echo "  ✗ the node agent CANNOT START yet: see the agent-image message above"
    echo "    (${_JOIN_AGENT_IMAGE_MISSING}). Enrolment itself succeeded; fix the image, then start it."
fi
echo ""
echo "Next: start the node agent on this box —"
echo "    docker compose --profile llm-worker-agent up -d"
echo ""
echo "The worker appears in the console under Fleet once it registers."
