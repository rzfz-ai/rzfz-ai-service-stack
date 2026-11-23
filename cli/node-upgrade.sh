#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz node-upgrade — upgrade a THIN LLM worker's agent in place (#2530)
# ==============================================================================
# Runs on the worker, from its bundle (/opt/rzfz-worker). One worker at a time;
# do NOT drain first (draining unloads the engines). Steps:
#   1. record the running agent image id and tag it :rollback (the hub keeps no
#      old manifest once :latest is re-pushed)
#   2. stage the bundle from the master (compose.thin.yml & co.) unless
#      --skip-bundle; a file the master cannot serve stops here (exit 2)
#   3. pull node/razzfazz-llm-worker-agent:<tag> through a throw-away
#      DOCKER_CONFIG and check the hub's Docker-Content-Digest is among the
#      pulled image's RepoDigests — a mismatch stops here, nothing recreated
#      (exit 3)
#   4. record the host's GPU group ids on an AMD node (#2393), install the
#      staged bundle, tag the image :latest and `compose up -d` ONLY the proxy +
#      agent (never `systemctl restart`: its ExecStop is `compose down`, which
#      removes the proxy and the network the engines hang on)
#   5. verify: agent healthy, every engine the same container with the same
#      start time, and the agent's /health lists every engine that ran before
#      (asked of the API: the docker agent logs nothing at INFO) — else roll
#      back (exit 4)
#
# What the digest check is — and is not: it proves the image that runs is the
# one the hub serves under the tag right now (two reads of the same hub agree).
# It is NOT an authenticity check (a compromised hub serves a matching pair),
# and there is NO downgrade protection (an older image under the tag, or
# --version naming one, is accepted). The bundle is root-executed code whose
# only protection is TLS to the master.
#
# Bootstrap (plan ruling 10): a worker installed before 2026.11 has no copy of
# this file and no RZFZ_MASTER_URL. It fetches the file once, then runs it with
# --master:
#   cd /opt/rzfz-worker
#   curl -fsSL https://<master>/worker-bundle/node-upgrade.sh -o cli/node-upgrade.sh
#   chmod 0755 cli/node-upgrade.sh
#   (add --cacert <LLM_WORKER_CA_FILE> when .env.node names one)
#
# Usage: rzfz node-upgrade [--master URL] [--manager-url URL] [--version TAG] [--skip-bundle] [--dry-run]
#   --master URL   the master this worker belongs to. Required ONCE on a worker
#                  installed before 2026.11 (no RZFZ_MASTER_URL in .env.node);
#                  recorded once the master served the bundle.
#   --manager-url URL  re-point this worker to another name of its master
#                  (e.g. https://llm.<domain> instead of the retired
#                  llm-manager.<domain>): LLM_MANAGER_URL and RZFZ_MASTER_URL in
#                  .env.node are rewritten in place and the agent is recreated.
#                  The new name must already resolve on this host (add it to
#                  /etc/hosts or DNS first) and answer /healthz; otherwise
#                  nothing is changed. The bundle is fetched from the new URL.
#   --version TAG  the agent tag to pull (default: latest)
#   --skip-bundle  do not refresh the bundle from the master
#   --dry-run      say what would change; tag, pull, write and recreate nothing.
#                  On a worker from before 2026.11 it DOES run code: the
#                  master-served lib.sh, to detect the GPU group ids.
# Exit: 0 upgraded (or already current); 2 usage/config (incl. a bundle file the
#   master did not serve); 3 the pull, the digest check, the platform or the
#   release check failed (nothing recreated); 4 a step after the digest check failed (rolled back).
# The digest check proves consistency with the hub, not authenticity, and there
# is no downgrade protection. A worker installed before 2026.11 fetches this
# command once from https://<master>/worker-bundle/node-upgrade.sh.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/scripts/lib.sh"

ENV_FILE="$SCRIPT_DIR/.env.node"
COMPOSE_FILE_THIN="$SCRIPT_DIR/modules/llm/node-agent/compose.thin.yml"
COMPOSE_REL="modules/llm/node-agent/compose.thin.yml"
PROJECT="${RZFZ_NODE_PROJECT:-rzfz-node}"
IMAGE="razzfazz-llm-worker-agent"
TAG="latest"; MASTER=""; MANAGER_URL_NEW=""; SKIP_BUNDLE=""; DRY_RUN=""

#: bundle file the master serves → where it sits in this tree (the layout
#: scripts/install-worker.sh.tmpl stages)
BUNDLE_FILES="compose.thin.yml:${COMPOSE_REL} worker-join.sh:cli/worker-join.sh lib.sh:scripts/lib.sh node-upgrade.sh:cli/node-upgrade.sh node.env.example:config/node.env.example metadata-block.sh:scripts/metadata-block.sh"
#: #3306: bundle files a master from before them does not serve — a warning,
#: not a stop (ensure_metadata_block says what is missing)
BUNDLE_OPTIONAL="metadata-block.sh"

usage() { sed -n '/^# Usage:/,/^# ====/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
die() { echo "node-upgrade: $1" >&2; exit "${2:-2}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --master) MASTER="${2:?--master needs a URL}"; shift 2 ;;
        --manager-url) MANAGER_URL_NEW="${2:?--manager-url needs a URL}"; shift 2 ;;
        --version) TAG="${2:?--version needs a tag}"; shift 2 ;;
        --skip-bundle) SKIP_BUNDLE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
MASTER_FLAG="${MASTER%/}"
MANAGER_URL_NEW="${MANAGER_URL_NEW%/}"
# --manager-url names the master too: the bundle comes from the NEW name (the
# old one may be the very thing that is gone), and it is the one remembered.
if [ -n "$MANAGER_URL_NEW" ]; then
    [ -z "$MASTER_FLAG" ] || [ "$MASTER_FLAG" = "$MANAGER_URL_NEW" ] \
        || die "--master and --manager-url name different URLs - pass only --manager-url"
    MASTER_FLAG="$MANAGER_URL_NEW"
fi

[ -f "$ENV_FILE" ] || die "no $ENV_FILE — this is not a thin worker (rzfz node-init / install-worker)"
REGISTRY="$(read_env_value "$ENV_FILE" LLM_WORKER_RUNNER_REGISTRY)"
[ -n "$REGISTRY" ] && [ "$REGISTRY" != "llm-registry:5000" ] \
    || die "LLM_WORKER_RUNNER_REGISTRY is empty or in-network — only a thin worker pulls its agent from the hub"
REGISTRY="${REGISTRY#*://}"

# Plan ruling 10: a worker installed before 2026.11 has no RZFZ_MASTER_URL.
SAVED_MASTER="$(read_env_value "$ENV_FILE" RZFZ_MASTER_URL)"; SAVED_MASTER="${SAVED_MASTER%/}"
if [ -z "$MASTER_FLAG" ] && [ -z "$SAVED_MASTER" ] && [ -z "$SKIP_BUNDLE" ]; then
    die "this worker has no RZFZ_MASTER_URL (installed before 2026.11) — pass --master https://<master> once; it is remembered"
fi
MASTER="${MASTER_FLAG:-$SAVED_MASTER}"
# The master URL's trust model is the installer's (scripts/install-worker.sh.tmpl):
# https expected, http accepted — loudly, because what it serves runs as root.
# Credentials never belong in it: they would be echoed and put on curl's argv.
case "$MASTER" in
    "") ;;
    *://*@*) die "the master URL must not carry credentials (user:password@) — pass https://<master>" ;;
    https://*) ;;
    http://*) echo "node-upgrade: WARN ${MASTER} is plain http — the bundle this runs as root is fetched UNENCRYPTED and unauthenticated; use https://" >&2 ;;
    *) die "--master needs a URL like https://<master> (got '${MASTER}')" ;;
esac

HARDWARE="$(read_env_value "$ENV_FILE" HARDWARE)"
REG_USER="$(read_env_value "$ENV_FILE" LLM_WORKER_REGISTRY_USER)"
REG_PW="$(read_env_value "$ENV_FILE" LLM_WORKER_REGISTRY_PASSWORD)"
CA_FILE="$(read_env_value "$ENV_FILE" LLM_WORKER_CA_FILE)"
OLD_MANAGER_URL="$(read_env_value "$ENV_FILE" LLM_MANAGER_URL)"
OLD_DIGEST="$(read_env_value "$ENV_FILE" LLM_WORKER_AGENT_DIGEST)"
# Finding 12: the engine network as it was at the start — rollback() puts the
# key and every engine back on it. Empty = compose.thin.yml's default.
OLD_ENGINE_NET="$(read_env_value "$ENV_FILE" LLM_WORKER_ENGINE_NETWORK)"
# The GPU group ids as they were at the start — rollback() restores them too.
GIDS_NOW="$(read_env_value "$ENV_FILE" RAZZFAZZ_GPU_GROUP_GIDS)"
AGENT_PORT="$(read_env_value "$ENV_FILE" LLM_WORKER_AGENT_PORT)"; AGENT_PORT="${AGENT_PORT:-8090}"
REF="${REGISTRY}/node/${IMAGE}:${TAG}"
CURL_TLS=(); [ -z "$CA_FILE" ] || CURL_TLS=(--cacert "$CA_FILE")

# --manager-url: the new name must resolve HERE and answer before anything is
# written - a worker pointed at a name it cannot reach goes stale.
MANAGER_URL_CHANGED=""
if [ -n "$MANAGER_URL_NEW" ] && [ "$MANAGER_URL_NEW" != "$OLD_MANAGER_URL" ]; then
    curl -fsS "${CURL_TLS[@]+"${CURL_TLS[@]}"}" --max-time 15 "${MANAGER_URL_NEW}/healthz" >/dev/null 2>&1 \
        || die "${MANAGER_URL_NEW}/healthz does not answer from this host. If the name does not resolve, add it to /etc/hosts or DNS first (same address as ${OLD_MANAGER_URL:-the old name}). Nothing changed."
    MANAGER_URL_CHANGED=1
fi

# Everything transient (staged bundle, throw-away docker config holding the hub
# login) lives here and goes on every exit, rollback and die included.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Record --master only once the master proved it is one (it served the bundle),
# or straight away with --skip-bundle (nothing to prove it against). A master
# given explicitly replaces a recorded one — that is how a typo is corrected.
record_master() {
    [ -n "$MASTER_FLAG" ] && [ "$MASTER_FLAG" != "$SAVED_MASTER" ] && [ -z "$DRY_RUN" ] || return 0
    update_env_value "$ENV_FILE" RZFZ_MASTER_URL "$MASTER_FLAG"
    echo "node-upgrade: recorded RZFZ_MASTER_URL=${MASTER_FLAG} — later runs need no --master"
}

engine_fingerprint() {
    local id
    for id in $(docker ps --filter label=rzfz.role=llm-engine --format '{{.ID}}' 2>/dev/null); do
        printf '%s %s\n' "$id" "$(docker inspect "$id" --format '{{.State.StartedAt}}')"
    done | sort
}

# The GIDs are handed to compose explicitly (as post-install's
# ensure_engine_gpu_group_gids does) so a stale export in the caller's shell
# cannot shadow .env.node.
compose_up() {
    RAZZFAZZ_GPU_GROUP_GIDS="$(read_env_value "$ENV_FILE" RAZZFAZZ_GPU_GROUP_GIDS)" \
        docker compose -p "$PROJECT" -f "$COMPOSE_FILE_THIN" --env-file "$ENV_FILE" \
        up -d docker-socket-proxy llm-worker-agent
}

agent_healthy() {
    local _
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 3 "http://127.0.0.1:${AGENT_PORT}/health" >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    return 1
}

# Group ids reach .env.node only as `none` or a comma-separated list of
# decimal ids — whatever lib.sh (possibly the master's) printed.
_gids_ok() { [[ "$1" =~ ^(none|[0-9]+(,[0-9]+)*)$ ]]; }

# #2393: the ONE detection is lib.sh's detect_engine_gpu_group_gids. A worker
# bootstrapping from before 2026.11 still runs its OLD scripts/lib.sh, which has
# no such function — then the staged (master-served) lib.sh answers, in a fresh
# bash so nothing of it leaks into this process. Prints the GIDs; returns 1
# when no lib.sh at hand knows the detection (--skip-bundle on an old worker).
gpu_gids_detect() {
    if declare -F detect_engine_gpu_group_gids >/dev/null; then
        detect_engine_gpu_group_gids
        return 0
    fi
    [ -f "$WORK/bundle/lib.sh" ] || return 1
    # shellcheck disable=SC2016  # $1 is expanded by the inner bash
    bash -c '. "$1" >/dev/null 2>&1 && declare -F detect_engine_gpu_group_gids >/dev/null && detect_engine_gpu_group_gids' \
        _ "$WORK/bundle/lib.sh"
}

# Fetch every bundle file into $WORK/bundle first; nothing in the tree changes
# until the digest matched. Prints the tree paths whose content differs.
stage_bundle() {
    local f name rel
    mkdir -p "$WORK/bundle"
    for f in $BUNDLE_FILES; do
        name="${f%%:*}"; rel="${f#*:}"
        if ! curl -fsSL "${CURL_TLS[@]+"${CURL_TLS[@]}"}" --max-time 60 "${MASTER}/worker-bundle/${name}" \
                -o "$WORK/bundle/${name}" 2>/dev/null; then
            case " $BUNDLE_OPTIONAL " in
                *" $name "*) rm -f "$WORK/bundle/${name}"
                             echo "node-upgrade: WARN the master does not serve ${name} — skipped" >&2
                             continue ;;
            esac
            die "could not fetch ${name} from ${MASTER}/worker-bundle/ — nothing changed"
        fi
        cmp -s "$WORK/bundle/${name}" "$SCRIPT_DIR/$rel" || printf '%s\n' "$rel"
    done
}

# Install the staged files. Each goes in by rename, so a running reader —
# including the bash executing this very file — keeps the old inode. Every file
# it replaces is kept as <file>.pre-upgrade until the run succeeded; rollback()
# puts them back. Returns 1 on the first failure (the caller rolls back).
BACKUPS=""; COMPOSE_CHANGED=""
install_bundle() {
    local f name rel mode dest
    for f in $BUNDLE_FILES; do
        name="${f%%:*}"; rel="${f#*:}"; dest="$SCRIPT_DIR/$rel"
        printf '%s\n' "$CHANGED" | grep -qxF "$rel" || continue
        case "$rel" in *.sh) mode=0755 ;; *) mode=0644 ;; esac
        if [ -f "$dest" ]; then
            cp -p "$dest" "${dest}.pre-upgrade" 2>/dev/null \
                || { echo "node-upgrade: WARN could not install ${rel} (no backup could be written)" >&2; return 1; }
            BACKUPS="${BACKUPS} ${rel}"
        fi
        [ "$rel" != "$COMPOSE_REL" ] || COMPOSE_CHANGED=1
        if ! mkdir -p "$(dirname "$dest")" 2>/dev/null \
                || ! install -m "$mode" "$WORK/bundle/${name}" "${dest}.node-upgrade.$$" 2>/dev/null \
                || ! mv -f "${dest}.node-upgrade.$$" "$dest" 2>/dev/null; then
            rm -f "${dest}.node-upgrade.$$"
            echo "node-upgrade: WARN could not install ${rel}" >&2
            return 1
        fi
    done
}

restore_bundle() {
    local rel failed=0
    for rel in $BACKUPS; do
        mv -f "$SCRIPT_DIR/${rel}.pre-upgrade" "$SCRIPT_DIR/$rel" 2>/dev/null || {
            echo "node-upgrade: WARN could not put ${rel} back from ${rel}.pre-upgrade" >&2
            failed=1
        }
    done
    BACKUPS=""
    return "$failed"
}

bundle_summary() {
    if [ -n "$SKIP_BUNDLE" ]; then echo "skipped (--skip-bundle)"
    elif [ -n "$CHANGED" ]; then echo "$(printf '%s\n' "$CHANGED" | paste -sd' ' -) $1"
    else echo "unchanged"; fi
}

# #3306: the cloud-metadata block (DOCKER-USER drop of 169.254.0.0/16,
# 100.100.100.200 and fd00:ec2::254 for every container) protects the engines
# on a rented GPU host as it protects the stack box. Installed when missing,
# idempotent (scripts/metadata-block.sh install). Root, or passwordless sudo
# like the CA step below; otherwise a WARN names the command. Never a reason to
# roll back: the agent upgrade does not depend on it.
ensure_metadata_block() {
    local mb="$SCRIPT_DIR/scripts/metadata-block.sh" run=()
    if [ ! -f "$mb" ]; then
        echo "node-upgrade: WARN scripts/metadata-block.sh is not on this worker (the master does not serve it yet): containers can reach cloud metadata addresses; run node-upgrade again after the master's upgrade" >&2
        return 0
    fi
    if [ "$(id -u)" = 0 ]; then
        run=(env)
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        run=(sudo -n env)
    else
        echo "node-upgrade: WARN cannot check the cloud metadata block without root; as root: RAZZFAZZ_ENV_FILE=${ENV_FILE} bash ${mb} install" >&2
        return 0
    fi
    if "${run[@]}" bash "$mb" status >/dev/null 2>&1; then
        echo "node-upgrade: cloud metadata block active"
    elif "${run[@]}" RAZZFAZZ_ENV_FILE="$ENV_FILE" bash "$mb" install >/dev/null 2>&1; then
        echo "node-upgrade: cloud metadata block installed (containers cannot reach cloud metadata addresses)"
    else
        echo "node-upgrade: WARN the cloud metadata block could not be installed; as root: RAZZFAZZ_ENV_FILE=${ENV_FILE} bash ${mb} install" >&2
    fi
}

# curl reads the hub login from a config on stdin (`-K -`): a `-u user:pw`
# argument is readable by every local user in `ps` while the request runs.
_curl_cfg_quote() { local v="${1//\\/\\\\}"; printf '"%s"' "${v//\"/\\\"}"; }
hub_digest() {
    local auth=""
    if [ -n "$REG_USER" ] && [ -n "$REG_PW" ]; then
        auth="user = $(_curl_cfg_quote "${REG_USER}:${REG_PW}")"
    fi
    printf '%s\n' "$auth" \
        | curl -fsSI -K - "${CURL_TLS[@]+"${CURL_TLS[@]}"}" --max-time 30 \
            -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
            "https://${REGISTRY}/v2/node/${IMAGE}/manifests/${TAG}" \
        | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}'
}

# The digests an image carries for OUR repo (one per line) — an image id can
# carry several (an older push of the same bytes), so membership is the test.
repo_digests() {
    docker image inspect "$1" --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n "s|^${REGISTRY}/node/${IMAGE}@||p" || true
}

# A network or container name goes to docker only when it looks like one.
_docker_name_ok() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; }

# Finding 12 (hardened as D20's master-side rollback): put EVERY engine the
# label filter finds — including one launched after the upgrade started — back
# on network $1, and take it off any other. Connect FIRST; the other networks
# are disconnected only after the connect succeeded, so a failed connect leaves
# the engine where it was (reachable) and says how to finish by hand.
# Returns 1 when any engine was left unrestored.
restore_engine_network() {
    local net="$1" id n nets on err failed=0
    if ! _docker_name_ok "$net"; then
        echo "node-upgrade: WARN engine network '${net}' is not a valid docker name — no engine was moved" >&2
        return 1
    fi
    for id in $(docker ps --filter label=rzfz.role=llm-engine --format '{{.ID}}' 2>/dev/null); do
        if ! _docker_name_ok "$id"; then
            echo "node-upgrade: WARN skipping engine '${id}': not a valid container name" >&2
            failed=1; continue
        fi
        if ! nets="$(docker inspect "$id" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>&1)"; then
            echo "node-upgrade: WARN cannot read the networks of engine ${id}: ${nets}" >&2
            failed=1; continue
        fi
        on=""
        for n in $nets; do [ "$n" = "$net" ] && on=1; done
        if [ -z "$on" ]; then
            if ! err="$(docker network connect "$net" "$id" 2>&1)"; then
                echo "node-upgrade: WARN engine ${id} could not be connected to ${net} (${err}) — it keeps its networks (${nets% }). Finish by hand: docker network connect ${net} ${id}" >&2
                failed=1; continue
            fi
        fi
        for n in $nets; do
            [ "$n" != "$net" ] || continue
            if ! _docker_name_ok "$n"; then
                echo "node-upgrade: WARN engine ${id}: network '${n}' is not a valid docker name — left attached" >&2
                failed=1; continue
            fi
            if ! err="$(docker network disconnect "$n" "$id" 2>&1)"; then
                echo "node-upgrade: WARN engine ${id} stays attached to ${n} (${err}) — finish by hand: docker network disconnect ${n} ${id}" >&2
                failed=1
            fi
        done
    done
    return "$failed"
}

# #2548: the network the socket proxy no longer shares with the engines, and
# the one a node installed before 2026.11 launched them on.
ENGINES_NET="rzfz-node_engines"; LEGACY_ENGINE_NET="rzfz-node_default"

# Does the installed compose.thin.yml declare ${ENGINES_NET}? An older tree
# (--skip-bundle on a node from before 2026.11) does not — or has no file at
# all — and then there is nothing to move to. Never fails the caller.
_compose_declares_engines_net() {
    grep -Eq "^[[:space:]]+name:[[:space:]]*[\"']?${ENGINES_NET}[\"']?[[:space:]]*$" "$COMPOSE_FILE_THIN" 2>/dev/null
}

# #2548: move the running engines off the network they share with the node's
# old default (where the socket proxy sat, with a route out). Runs ONLY after
# the recreate was verified (finding 12); any failure returns 1 and the caller
# rolls back through rollback(), which restores the recorded key and puts every
# engine back on the recorded network. Steps: the key first — the agent reads
# it at start, so it is recreated once more — then, per engine, connect to
# ${ENGINES_NET} BEFORE leaving ${LEGACY_ENGINE_NET}; the agent (on both)
# reaches the engine throughout. An engine that cannot be read or connected
# keeps the networks it is on. Every step is checked explicitly: this runs as
# `migrate_engine_network || rollback …`, where set -e does not apply.
MOVED_ENGINES=0
migrate_engine_network() {
    local cur id nets n on err failed=0
    cur="$(read_env_value "$ENV_FILE" LLM_WORKER_ENGINE_NETWORK 2>/dev/null || true)"
    # already moved, or chosen by the operator: not ours to change
    [ -z "$cur" ] || [ "$cur" = "$LEGACY_ENGINE_NET" ] || return 0
    if ! _compose_declares_engines_net; then
        echo "node-upgrade: WARN ${COMPOSE_REL} declares no ${ENGINES_NET} network (an older bundle) — the engines stay on ${cur:-$LEGACY_ENGINE_NET}; run node-upgrade again without --skip-bundle (#2548)" >&2
        return 0
    fi
    update_env_value "$ENV_FILE" LLM_WORKER_ENGINE_NETWORK "$ENGINES_NET" || return 1
    compose_up || { echo "node-upgrade: WARN compose up with LLM_WORKER_ENGINE_NETWORK=${ENGINES_NET} failed (#2548)" >&2; return 1; }
    for id in $(docker ps --filter label=rzfz.role=llm-engine --format '{{.ID}}' 2>/dev/null || true); do
        if ! _docker_name_ok "$id"; then
            echo "node-upgrade: WARN skipping engine '${id}': not a valid container name (#2548)" >&2
            failed=1; continue
        fi
        if ! nets="$(docker inspect "$id" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>&1)"; then
            echo "node-upgrade: WARN cannot read the networks of engine ${id}: ${nets} — it keeps its networks (#2548)" >&2
            failed=1; continue
        fi
        on=""
        for n in $nets; do [ "$n" != "$ENGINES_NET" ] || on=1; done
        if [ -z "$on" ]; then
            if ! err="$(docker network connect "$ENGINES_NET" "$id" 2>&1)"; then
                echo "node-upgrade: WARN engine ${id} could not be connected to ${ENGINES_NET} (${err}) — it keeps its networks (${nets% }) (#2548)" >&2
                failed=1; continue
            fi
        fi
        for n in $nets; do
            [ "$n" = "$LEGACY_ENGINE_NET" ] || continue
            if ! err="$(docker network disconnect "$LEGACY_ENGINE_NET" "$id" 2>&1)"; then
                echo "node-upgrade: WARN engine ${id} stays attached to ${LEGACY_ENGINE_NET} (${err}) (#2548)" >&2
                failed=1
            fi
        done
        MOVED_ENGINES=$((MOVED_ENGINES + 1))
    done
    [ "$failed" -eq 0 ] || return 1
    echo "node-upgrade: ${MOVED_ENGINES} engine(s) moved to ${ENGINES_NET}, off the socket proxy's old network (#2548)"
}

# m2: is a move still owed on a node that is otherwise current? Yes when the
# installed compose declares ${ENGINES_NET}, the key is legacy or empty, and
# either the key is legacy (new launches would land on ${LEGACY_ENGINE_NET})
# or an engine is not on ${ENGINES_NET}. An engine that cannot be read counts
# as owed — migrate_engine_network then WARNs and rolls back.
_engine_move_pending() {
    local cur id nets
    _compose_declares_engines_net || return 1
    cur="$(read_env_value "$ENV_FILE" LLM_WORKER_ENGINE_NETWORK 2>/dev/null || true)"
    [ "$cur" = "$LEGACY_ENGINE_NET" ] && return 0
    [ -z "$cur" ] || return 1
    for id in $(docker ps --filter label=rzfz.role=llm-engine --format '{{.ID}}' 2>/dev/null || true); do
        _docker_name_ok "$id" || return 0
        nets="$(docker inspect "$id" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)" || return 0
        [[ " $nets " == *" ${ENGINES_NET} "* ]] || return 0
    done
    return 1
}

# rollback() restores the image tag, the digest key, the GPU group ids, the
# bundle files this run replaced and — finding 12 — the engine network: the
# LLM_WORKER_ENGINE_NETWORK key and every engine's attachment, as recorded at
# the start. Then it checks the restored agent answers before calling it done.
rollback() {
    local restored=1
    # A second Ctrl-C/TERM must not abort a half-done rollback silently: it is
    # ignored until rollback() has finished and reported (children inherit it).
    trap '' INT TERM
    echo "node-upgrade: $1 — rolling back to the recorded image" >&2
    docker tag "${IMAGE}:rollback" "${IMAGE}:latest" \
        || { echo "node-upgrade: WARN could not re-tag ${IMAGE}:rollback as :latest" >&2; restored=""; }
    update_env_value "$ENV_FILE" LLM_WORKER_AGENT_DIGEST "$OLD_DIGEST"
    if [ "$(read_env_value "$ENV_FILE" LLM_WORKER_ENGINE_NETWORK)" != "$OLD_ENGINE_NET" ]; then
        update_env_value "$ENV_FILE" LLM_WORKER_ENGINE_NETWORK "$OLD_ENGINE_NET"
    fi
    if [ "$(read_env_value "$ENV_FILE" RAZZFAZZ_GPU_GROUP_GIDS)" != "$GIDS_NOW" ]; then
        update_env_value "$ENV_FILE" RAZZFAZZ_GPU_GROUP_GIDS "$GIDS_NOW"
    fi
    if [ -n "$MANAGER_URL_CHANGED" ]; then
        update_env_value "$ENV_FILE" LLM_MANAGER_URL "$OLD_MANAGER_URL"
        [ -z "$SAVED_MASTER" ] || update_env_value "$ENV_FILE" RZFZ_MASTER_URL "$SAVED_MASTER"
    fi
    restore_bundle || restored=""
    # #2548: this rollback undoes migrate_engine_network's move too — the key
    # above, every engine's attachment below. The move itself runs only after
    # the first verify_upgrade, so a failed recreate never reaches it.
    compose_up || { echo "node-upgrade: WARN the rollback's compose up failed — run it by hand: docker compose -p ${PROJECT} -f ${COMPOSE_FILE_THIN} --env-file ${ENV_FILE} up -d llm-worker-agent" >&2; restored=""; }
    if ! agent_healthy; then
        echo "node-upgrade: WARN the restored agent does not answer http://127.0.0.1:${AGENT_PORT}/health" >&2
        restored=""
    fi
    restore_engine_network "${OLD_ENGINE_NET:-rzfz-node_default}" || restored=""
    if [ -n "$restored" ]; then
        echo "node-upgrade: rolled back to ${OLD_ID} (${OLD_DIGEST:-digest unknown})" >&2
    else
        echo "node-upgrade: rollback INCOMPLETE — the image is back on ${OLD_ID}, but see the WARN lines above; the node is not in its recorded state until they are fixed" >&2
    fi
    exit 4
}

# The engines the agent has to know about: the names of the running engine
# containers (the agent keys an instance by its container name).
engine_names() {
    docker ps --filter label=rzfz.role=llm-engine --format '{{.Names}}' 2>/dev/null | LC_ALL=C sort || true
}

# The instances the agent lists on /health ("loaded"), one per line, sorted.
# Asked of the agent's API, never read from its log (#2924): the docker agent is
# plain uvicorn and logs nothing of its own at INFO.
agent_loaded() {
    curl -fsS --max-time 3 "http://127.0.0.1:${AGENT_PORT}/health" 2>/dev/null \
        | sed -n 's/.*"loaded"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p' \
        | tr ',' '\n' | tr -d ' "' | sed '/^$/d' | LC_ALL=C sort || true
}

# #2899: the keys a fresh NVIDIA enrolment writes (install-worker.sh.tmpl:
# write_gpu_access / write_cuda_capability), for a worker enrolled before they
# existed. Without them the agent runs `runc`, has no nvidia-smi, and cannot
# tell which GPU it sits on. An existing value is the operator's and is never
# overwritten. Needs the new compose.thin.yml to forward them, so it runs after
# the bundle install; prints nothing and returns 0 when nothing was written.
backfill_nvidia_access() {
    case "$HARDWARE" in nvidia|cuda|cuda-gb10|gb10|nvidia-gb10) ;; *) return 0 ;; esac
    local cap=""
    if [ -z "$(read_env_value "$ENV_FILE" LLM_WORKER_RUNTIME)" ]; then
        if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
            update_env_value "$ENV_FILE" LLM_WORKER_RUNTIME nvidia
            update_env_value "$ENV_FILE" NVIDIA_VISIBLE_DEVICES all
            update_env_value "$ENV_FILE" NVIDIA_DRIVER_CAPABILITIES utility
            GPU_ACCESS_CHANGED=1
            echo "node-upgrade: agent GPU access recorded: runtime=nvidia, capabilities=utility"
        else
            echo "node-upgrade: WARN the docker daemon has no 'nvidia' runtime, so the agent cannot read this GPU - install nvidia-container-toolkit and run node-upgrade again" >&2
        fi
    fi
    if [ -z "$(read_env_value "$ENV_FILE" RAZZFAZZ_CUDA_COMPUTE_CAP)" ] && command -v nvidia-smi >/dev/null 2>&1; then
        cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | tr -d '[:space:]' | head -n1 || true)"
        case "$cap" in
            ""|*[!0-9.]*|N/A) ;;
            *) update_env_value "$ENV_FILE" RAZZFAZZ_CUDA_COMPUTE_CAP "$cap"
               GPU_ACCESS_CHANGED=1
               echo "node-upgrade: CUDA compute capability ${cap} recorded" ;;
        esac
    fi
}

# One verification, reusable after any post-recreate step: the agent answers,
# every engine is the same container with the same start time, and the agent's
# /health lists every engine that ran before (a bounded wait: re-adoption runs
# a moment after startup).
verify_upgrade() {
    local label="$1" ok="" want have missing=""
    agent_healthy || rollback "${label}: the new agent did not answer /health within 60 s"
    [ "$(engine_fingerprint)" = "$BEFORE" ] || rollback "${label}: an engine was recreated or restarted"
    if [ -n "$BEFORE" ]; then
        want="$(printf '%s\n' "$BEFORE_NAMES" | grep -c . || true)"
        for _ in $(seq 1 15); do
            have="$(agent_loaded)"
            missing="$(LC_ALL=C comm -23 <(printf '%s\n' "$BEFORE_NAMES") <(printf '%s\n' "$have"))"
            if [ -z "$missing" ]; then ok=1; break; fi
            sleep 2
        done
        [ -n "$ok" ] || rollback "${label}: the agent's /health does not list the ${want} running engine(s) (#2924; missing: $(printf '%s\n' "$missing" | paste -sd' ' -))"
    fi
}

echo "node-upgrade: ${REF}"
OLD_ID="$(docker inspect llm-worker-agent --format '{{.Image}}' 2>/dev/null)" && [ -n "$OLD_ID" ] \
    || die "no llm-worker-agent container on this node"

CHANGED=""
[ -n "$SKIP_BUNDLE" ] || CHANGED="$(stage_bundle)" || exit 2
record_master

# #2393 (D18 carry): the GIDs this node's engines need, and whether the running
# agent carries them. The value is recorded below, after the digest check.
GIDS_NEW="$GIDS_NOW"
if [ "$HARDWARE" = amd ] && [ -z "$GIDS_NOW" ]; then
    if GIDS_NEW="$(gpu_gids_detect)"; then
        if [ -z "$GIDS_NEW" ]; then
            echo "node-upgrade: WARN no video/render group on this host — AMD engines keep running as root (capless) (#2393)" >&2
        elif ! _gids_ok "$GIDS_NEW"; then
            echo "node-upgrade: WARN the GPU group detection printed '${GIDS_NEW}', which is not a list of group ids — RAZZFAZZ_GPU_GROUP_GIDS not recorded (#2393)" >&2
            GIDS_NEW=""
        fi
    else
        GIDS_NEW=""
        echo "node-upgrade: WARN this worker's scripts/lib.sh predates the GPU-group detection and no bundle was staged (--skip-bundle) — RAZZFAZZ_GPU_GROUP_GIDS not recorded; run node-upgrade again without --skip-bundle (#2393)" >&2
    fi
fi
GIDS_STALE=""
if [ "$HARDWARE" = amd ]; then
    agent_gids="$(docker inspect llm-worker-agent --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^RAZZFAZZ_GPU_GROUP_GIDS=//p' | head -1 || true)"
    [ "$agent_gids" = "$GIDS_NEW" ] || GIDS_STALE=1
fi

if [ -n "$DRY_RUN" ]; then
    HUB="$(hub_digest)" || HUB=""
    [ -n "$HUB" ] || die "the hub served no digest for ${REF}" 3
    echo "node-upgrade: bundle: $(bundle_summary "would be updated")"
    [ "$GIDS_NEW" = "$GIDS_NOW" ] || echo "node-upgrade: would record RAZZFAZZ_GPU_GROUP_GIDS=${GIDS_NEW}"
    [ -z "$MANAGER_URL_CHANGED" ] || echo "node-upgrade: would point LLM_MANAGER_URL from ${OLD_MANAGER_URL:-<unset>} to ${MANAGER_URL_NEW} and recreate the agent"
    # the running image, not the recorded key, says what runs
    if grep -qxF -- "$HUB" <<<"$(repo_digests "$OLD_ID")"; then
        echo "node-upgrade: already on ${HUB}${GIDS_STALE:+ (the agent would be recreated for its GPU groups)}"
    else
        echo "node-upgrade: would move ${OLD_DIGEST:-<unknown>} → ${HUB}"
    fi
    exit 0
fi

docker tag "$OLD_ID" "${IMAGE}:rollback"
BEFORE="$(engine_fingerprint)"
BEFORE_NAMES="$(engine_names)"

cfg="$WORK/docker-config"; mkdir -m 0700 "$cfg"
# The daemon must trust the master CA for the hub, or `docker login/pull` fails
# with "x509: certificate signed by unknown authority". Inline (not from lib.sh):
# this script can run against an older lib. Idempotent; sudo -n when not root.
if [ -n "$CA_FILE" ] && [ -f "$CA_FILE" ]; then
    case "$REGISTRY" in
        llm-registry:*|localhost*|127.*) ;;
        *)
            _cdir="${RZFZ_DOCKER_CERTS_DIR:-/etc/docker/certs.d}/${REGISTRY}"
            if cmp -s "$CA_FILE" "${_cdir}/ca.crt" 2>/dev/null; then
                :
            elif { mkdir -p "$_cdir" && cp "$CA_FILE" "${_cdir}/ca.crt"; } 2>/dev/null \
              || { command -v sudo >/dev/null 2>&1 && sudo -n mkdir -p "$_cdir" 2>/dev/null && sudo -n cp "$CA_FILE" "${_cdir}/ca.crt" 2>/dev/null; }; then
                echo "node-upgrade: master CA installed for docker at ${_cdir}/ca.crt"
            else
                echo "node-upgrade: WARN cannot install the master CA for docker; if login fails with an x509 error, as root: mkdir -p ${_cdir} && cp ${CA_FILE} ${_cdir}/ca.crt" >&2
            fi ;;
    esac
fi
if [ -n "$REG_USER" ] && [ -n "$REG_PW" ]; then
    printf '%s' "$REG_PW" | DOCKER_CONFIG="$cfg" docker login "$REGISTRY" -u "$REG_USER" --password-stdin >/dev/null \
        || die "could not log in to ${REGISTRY} as ${REG_USER} — nothing recreated" 3
fi
DOCKER_CONFIG="$cfg" docker pull "$REF" >/dev/null || die "could not pull ${REF} — nothing recreated" 3
PULLED_DIGESTS="$(repo_digests "$REF")"
HUB="$(hub_digest)" || HUB=""
if [ -z "$HUB" ] || ! grep -qxF -- "$HUB" <<<"$PULLED_DIGESTS"; then
    die "digest mismatch: the hub serves ${HUB:-<no digest>} but the pulled image carries $(printf '%s\n' "${PULLED_DIGESTS:-<none>}" | paste -sd' ' -) — nothing recreated" 3
fi
PULLED="$HUB"

# #2952: the digest above only proves the pulled bytes are the hub's. It says
# nothing about WHICH image the hub serves under the tag - an image of another
# architecture (an older single-arch push) or of an older release (a stale
# arm64 half of a multi-arch list) both match their own digest. Both are
# refused here, before anything is recreated.
case "$(uname -m)" in
    x86_64|amd64) NODE_ARCH=amd64 ;;
    aarch64|arm64) NODE_ARCH=arm64 ;;
    *) NODE_ARCH="$(uname -m)" ;;
esac
PULLED_PLATFORM="$(docker image inspect "$REF" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || true)"
if [ -n "$PULLED_PLATFORM" ] && [ "$PULLED_PLATFORM" != "linux/${NODE_ARCH}" ]; then
    die "the hub serves ${REF} as ${PULLED_PLATFORM}, but this node is linux/${NODE_ARCH} - the hub has no ${NODE_ARCH} agent under ${TAG}. Nothing recreated. Publish it on the master (rzfz post-install --refresh; see scripts/verify-hub-images.sh)" 3
fi
# The release the image must carry: the tag when it names one (--version
# 2026.11-rc6), else the release the master runs (served with the bundle).
EXPECT_RELEASE="${RZFZ_EXPECT_RELEASE:-}"
case "$TAG" in [0-9][0-9][0-9][0-9].*) EXPECT_RELEASE="$TAG" ;; esac
if [ -z "$EXPECT_RELEASE" ] && [ -n "$MASTER" ]; then
    EXPECT_RELEASE="$(curl -fsSL "${CURL_TLS[@]+"${CURL_TLS[@]}"}" --max-time 30 "${MASTER}/worker-bundle/VERSION" 2>/dev/null \
        | tr -d '[:space:]' || true)"
    [[ "$EXPECT_RELEASE" =~ ^[0-9A-Za-z._-]+$ ]] || EXPECT_RELEASE=""
fi
if [ -n "$EXPECT_RELEASE" ]; then
    PULLED_RELEASE="$(docker image inspect "$REF" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^RAZZFAZZ_IMAGE_VERSION=//p' | head -1 || true)"
    if [ "$PULLED_RELEASE" != "$EXPECT_RELEASE" ]; then
        die "the hub has no ${NODE_ARCH} agent for ${EXPECT_RELEASE}: ${REF} is release ${PULLED_RELEASE:-<unlabelled>}. Nothing recreated. Publish it on the master (rzfz post-install --refresh; check with scripts/verify-hub-images.sh ${EXPECT_RELEASE})" 3
    fi
else
    echo "node-upgrade: WARN could not learn which release the master runs - the agent's release was not checked" >&2
fi

# From here on, anything that goes wrong — an interrupt included — rolls back.
trap 'rollback "interrupted"' INT TERM
if [ "$GIDS_NEW" != "$GIDS_NOW" ]; then
    update_env_value "$ENV_FILE" RAZZFAZZ_GPU_GROUP_GIDS "$GIDS_NEW"
    echo "node-upgrade: AMD engines run as nobody with GPU groups ${GIDS_NEW} (#2393)"
fi
if [ -n "$MANAGER_URL_CHANGED" ]; then
    _bak="${ENV_FILE}.pre-manager-url.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$ENV_FILE" "$_bak" 2>/dev/null && echo "node-upgrade: .env.node saved as ${_bak}" \
        || echo "node-upgrade: WARN could not write a backup of .env.node" >&2
    update_env_value "$ENV_FILE" LLM_MANAGER_URL "$MANAGER_URL_NEW"
    echo "node-upgrade: LLM_MANAGER_URL ${OLD_MANAGER_URL:-<unset>} -> ${MANAGER_URL_NEW}"
fi
if [ -n "$CHANGED" ]; then
    install_bundle || rollback "the bundle could not be installed"
fi
echo "node-upgrade: bundle: $(bundle_summary updated)"
ensure_metadata_block

GPU_ACCESS_CHANGED=""
backfill_nvidia_access

# Current = the pulled image IS the one the agent runs (not what the digest key
# claims). Nothing is recreated then unless the compose file or the GPU groups
# moved; the key is brought in line.
PULLED_ID="$(docker image inspect "$REF" --format '{{.Id}}' 2>/dev/null || true)"
if [ "$PULLED_ID" = "$OLD_ID" ] && [ -z "$COMPOSE_CHANGED" ] && [ -z "$GIDS_STALE" ] && [ -z "$GPU_ACCESS_CHANGED" ] && [ -z "$MANAGER_URL_CHANGED" ]; then
    [ "$PULLED" = "$OLD_DIGEST" ] || update_env_value "$ENV_FILE" LLM_WORKER_AGENT_DIGEST "$PULLED"
    if _engine_move_pending; then
        # m2: the image is current, but the engines still owe the #2548 move.
        # Nothing was recreated, so there is no first recreate to verify: the
        # move's own recreate (the key changes) is verified instead, and any
        # failure rolls back exactly as after an upgrade.
        echo "node-upgrade: already on ${PULLED}; the engines still sit on ${LEGACY_ENGINE_NET} — moving them (#2548)"
        migrate_engine_network || rollback "the engine-network move failed (#2548)"
        verify_upgrade "after the engine-network move (#2548)"
        trap - INT TERM
        for rel in $BACKUPS; do rm -f "$SCRIPT_DIR/${rel}.pre-upgrade"; done
        echo "node-upgrade: already on ${PULLED}; $(printf '%s\n' "$BEFORE" | grep -c . || true) engine(s) moved to ${ENGINES_NET} and kept running"
        exit 0
    fi
    for rel in $BACKUPS; do rm -f "$SCRIPT_DIR/${rel}.pre-upgrade"; done
    echo "node-upgrade: already on ${PULLED}"; exit 0
fi

docker tag "$REF" "${IMAGE}:latest" || rollback "could not tag ${REF} as :latest"
update_env_value "$ENV_FILE" LLM_WORKER_AGENT_DIGEST "$PULLED"
compose_up || rollback "compose up failed"
verify_upgrade "after the agent recreate"
# Post-verification steps, each followed by `verify_upgrade "<label>"`; a
# failure rolls back through rollback(), which restores
# LLM_WORKER_ENGINE_NETWORK and every engine's network (finding 12).
migrate_engine_network || rollback "the engine-network move failed (#2548)"
verify_upgrade "after the engine-network move (#2548)"
trap - INT TERM
for rel in $BACKUPS; do rm -f "$SCRIPT_DIR/${rel}.pre-upgrade"; done
echo "node-upgrade: ${OLD_DIGEST:-<unknown>} → ${PULLED}; $(printf '%s\n' "$BEFORE" | grep -c . || true) engine(s) kept running"
