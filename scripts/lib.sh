# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# shellcheck shell=bash
# ==============================================================================
# scripts/lib.sh — shared bash library for razzfazz-*.sh management scripts

# rzfz_normalize_arch <name> — one spelling for the image architecture (#1554).
#
# The two readers speak different namespaces: `docker version --format
# '{{.Server.Arch}}'` answers Go's (amd64/arm64), `uname -m` the kernel's
# (x86_64/aarch64). The packaging side always reaches docker — without it there
# is no `docker save` — while the receiving side can fall back when the daemon
# does not answer. Comparing the raw strings then refuses a VALID amd64 package
# on an amd64 box, on exactly the air-gapped machine the check exists for, with
# advice the operator has already followed (#1570 review).
#
# Unknown stays unknown: the caller must treat "" as "cannot say" and warn,
# never as "differs".
rzfz_normalize_arch() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        amd64|x86_64|x86-64) echo "amd64" ;;
        arm64|aarch64)       echo "arm64" ;;
        armv7l|armhf|arm)    echo "arm" ;;
        386|i386|i686)       echo "386" ;;
        "")                  echo "" ;;
        *)                   printf '%s\n' "$1" ;;   # unknown: pass through verbatim
    esac
}

# ── Multi-arch worker agent image ────────────────────────────────────────────
# The node images a worker pulls from the hub (node/razzfazz-llm-worker-agent)
# are built on the master, so a master of one architecture published a
# single-arch image and a worker of the other died with `exec format error`
# (GB10 / DGX Spark workers are arm64, most masters amd64). The agent is plain
# Python (pinned wheels that exist for both architectures), so the FOREIGN
# variant is simply built with buildx under QEMU emulation, tagged
# `<image>-<arch>`, and the hub gets a manifest list over both.
#
# These helpers are shared by post-install (publish) and package (the offline
# package must carry the foreign variant too, because an air-gapped master
# cannot build it). The CUDA RUNNER images are a different matter and stay
# governed by razzfazz_runner_package_plan (a GB10 runner needs a GB10 builder).
RAZZFAZZ_PUBLISHED_NODE_IMAGES="razzfazz-llm-worker-agent:latest"
RAZZFAZZ_NODE_IMAGE_CONTEXT="modules/llm/node-agent"

# The other of amd64/arm64, relative to this host.
rzfz_foreign_arch() {
    case "$(rzfz_normalize_arch "$(uname -m)")" in
        arm64) echo "amd64" ;;
        *)     echo "arm64" ;;
    esac
}

# razzfazz-llm-worker-agent:latest + arm64 -> razzfazz-llm-worker-agent:latest-arm64
rzfz_node_image_variant_ref() {
    printf '%s-%s\n' "$1" "$2"
}

# The build identity of the worker agent: a sha256 over every file of its build
# context (path + content), caches left out. Both architectures carry it as the
# label ai.razzfazz.build-context (Dockerfile ARG RAZZFAZZ_BUILD_CONTEXT): the
# host image through razzfazz_compose_build, the foreign variant through the
# command below. The version label alone cannot tell two builds of one release
# apart, so a variant built from older code of the same release was published as
# current and arm64 workers kept the old agent. Empty when there is no context.
RZFZ_BUILD_CONTEXT_LABEL="ai.razzfazz.build-context"
rzfz_node_context_digest() {
    local dir="${1:-.}/${RAZZFAZZ_NODE_IMAGE_CONTEXT}"
    [ -d "$dir" ] || return 0
    ( cd "$dir" && find . -type f ! -path '*/__pycache__/*' ! -path '*/.pytest_cache/*' \
          ! -path '*/.mypy_cache/*' ! -path '*/.ruff_cache/*' ! -name '*.pyc' ! -name '*.pyo' -print0 \
          | LC_ALL=C sort -z | xargs -0 -r sha256sum ) | sha256sum | cut -c1-64
}

# The build identity a local image carries; empty when it has none (built before
# the label existed, or by hand without the build argument).
rzfz_image_build_context() {
    docker image inspect "$1" --format '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}' 2>/dev/null \
        | sed -n "s/^${RZFZ_BUILD_CONTEXT_LABEL}=//p" | head -1
}

# The exact command that builds a variant (also what a WARN tells the operator).
rzfz_node_image_variant_build_cmd() {
    local image="$1" arch="$2" root="$3" version="${4:-}" context
    context="$(rzfz_node_context_digest "$root")"
    printf 'docker buildx build --platform linux/%s --load%s%s -t %s %s\n' \
        "$arch" "${version:+ --build-arg RAZZFAZZ_IMAGE_VERSION=${version}}" \
        "${context:+ --build-arg RAZZFAZZ_BUILD_CONTEXT=${context}}" \
        "$(rzfz_node_image_variant_ref "$image" "$arch")" "${root}/${RAZZFAZZ_NODE_IMAGE_CONTEXT}"
}

# Ensure the local image `<image>-<arch>` exists AND is that architecture.
# 0 = present or built; 1 = not available (a WARN names why and the exact
# command). Never fatal: the caller falls back to a single-arch publish.
rzfz_ensure_node_image_variant() {
    local image="$1" arch="$2" root="$3" version="${4:-}"
    local ref have have_v have_c context qemu cmd
    ref="$(rzfz_node_image_variant_ref "$image" "$arch")"
    have="$(docker image inspect "$ref" --format '{{.Architecture}}' 2>/dev/null || true)"
    cmd="$(rzfz_node_image_variant_build_cmd "$image" "$arch" "$root" "$version")"
    context="$(rzfz_node_context_digest "$root")"
    if [ "$(rzfz_normalize_arch "$have")" = "$arch" ]; then
        # #2952: a variant left over from an earlier release is the right
        # architecture and the WRONG code. Reuse it only when it was built for
        # this release AND from this tree's code: every build of one release
        # carries the same version, so the version alone let a variant built
        # from older code of the same release pass as current.
        have_v="$(rzfz_image_version "$ref")"
        have_c="$(rzfz_image_build_context "$ref")"
        if [ -n "$version" ] && [ "$have_v" != "$version" ]; then
            print_warning "  The local ${ref} is release ${have_v:-<unlabelled>}, not ${version} - it is stale and will not be published."
        elif [ -n "$context" ] && [ "$have_c" != "$context" ]; then
            print_warning "  The local ${ref} was built from other code than this tree (build context ${have_c:-<unlabelled>}, this tree ${context}) - it is stale and will not be published."
        else
            return 0
        fi
    fi
    if razzfazz_is_offline 2>/dev/null; then
        print_warning "  No ${arch} variant of ${image} on this master and it is offline - the ${arch} variant comes from the offline package (it carries ${ref}). Without it ${arch} workers cannot pull the agent."
        return 1
    fi
    if [ ! -d "${root}/${RAZZFAZZ_NODE_IMAGE_CONTEXT}" ]; then
        print_warning "  No build context ${root}/${RAZZFAZZ_NODE_IMAGE_CONTEXT} - cannot build the ${arch} variant of ${image}."
        return 1
    fi
    if ! docker buildx version >/dev/null 2>&1; then
        print_warning "  docker buildx is not available - the hub gets a single-arch ${image}; ${arch} workers will fail with 'exec format error'. Install docker-buildx-plugin, then: ${cmd}"
        return 1
    fi
    case "$arch" in arm64) qemu="aarch64" ;; amd64) qemu="x86_64" ;; *) qemu="$arch" ;; esac
    if [ ! -e "${RZFZ_BINFMT_DIR:-/proc/sys/fs/binfmt_misc}/qemu-${qemu}" ]; then
        docker run --privileged --rm tonistiigi/binfmt --install "$arch" >/dev/null 2>&1 || true
    fi
    if [ ! -e "${RZFZ_BINFMT_DIR:-/proc/sys/fs/binfmt_misc}/qemu-${qemu}" ]; then
        print_warning "  No QEMU/binfmt emulation for ${arch} on this host - the hub gets a single-arch ${image}. Enable it (docker run --privileged --rm tonistiigi/binfmt --install ${arch}), then: ${cmd}"
        return 1
    fi
    print_substep "  Building the ${arch} variant of ${image} under emulation (slow)..."
    if ! $cmd >/dev/null 2>&1; then
        print_warning "  The ${arch} build of ${image} failed - the hub gets a single-arch image. Run it by hand to see why: ${cmd}"
        return 1
    fi
    have="$(docker image inspect "$ref" --format '{{.Architecture}}' 2>/dev/null || true)"
    [ "$(rzfz_normalize_arch "$have")" = "$arch" ] || return 1
    [ -z "$version" ] || [ "$(rzfz_image_version "$ref")" = "$version" ] || return 1
    [ -z "$context" ] || [ "$(rzfz_image_build_context "$ref")" = "$context" ]
}

# The release a local image was built for (its RAZZFAZZ_IMAGE_VERSION env).
rzfz_image_version() {
    docker image inspect "$1" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^RAZZFAZZ_IMAGE_VERSION=//p' | head -1
}

# Make the worker's DOCKER DAEMON trust the master CA for the hub. The agent
# pins the CA for its own TLS (LLM_WORKER_CA_FILE), but `docker login/pull
# hub.<domain>` is the daemon's connection and failed with "x509: certificate
# signed by unknown authority". Docker reads /etc/docker/certs.d/<host>/ca.crt
# per registry, with no daemon restart. Idempotent (an identical file is left
# alone); needs root, via sudo -n when this is not root; never fatal - without
# permission it prints the exact commands.
#   $1 registry host[:port]   $2 the master CA (PEM)
rzfz_install_hub_ca() {
    local reg="${1:-}" ca="${2:-}" dir dest
    [ -n "$reg" ] && [ -n "$ca" ] && [ -f "$ca" ] || return 0
    case "$reg" in llm-registry:*|localhost*|127.*) return 0 ;; esac
    dir="${RZFZ_DOCKER_CERTS_DIR:-/etc/docker/certs.d}/${reg}"
    dest="${dir}/ca.crt"
    if cmp -s "$ca" "$dest" 2>/dev/null; then
        echo "  ✓ docker already trusts the master CA for ${reg}"
        return 0
    fi
    if mkdir -p "$dir" 2>/dev/null && cp "$ca" "$dest" 2>/dev/null; then
        :
    elif command -v sudo >/dev/null 2>&1 && sudo -n mkdir -p "$dir" 2>/dev/null && sudo -n cp "$ca" "$dest" 2>/dev/null; then
        :
    else
        echo "  ! cannot install the master CA for docker (no permission) - 'docker login ${reg}' will fail with" >&2
        echo "    'x509: certificate signed by unknown authority'. As root: mkdir -p ${dir} && cp ${ca} ${dest}" >&2
        return 1
    fi
    echo "  ✓ master CA installed for docker at ${dest}"
}

# #3375: the same trust for the MASTER's own worker. Its runner pulls also go
# through the host daemon to the hub (hub.<domain>), which Caddy serves with its
# internal CA on TLS_MODE=internal - and nothing put that CA into certs.d on the
# master, so the runner switch failed with x509. It went unnoticed where the hub
# resolves to 127.0.0.1: the daemon treats 127.0.0.0/8 as an insecure registry
# and skips the check. The host is the one the agent pulls from
# (allowed_runner_registry: the explicit knob, then the hub domain; neither =
# the in-network registry, nothing to trust). 0 = trusted or nothing to do,
# 1 = not installed (warned).
#   $1 env file (default .env)
rzfz_trust_box_hub_ca() {
    local env_file="${1:-.env}" tls reg main tmp rc=0
    tls="$(read_env_value "$env_file" TLS_MODE)"
    [ "$tls" = "internal" ] || return 0
    reg="$(read_env_value "$env_file" LLM_WORKER_RUNNER_REGISTRY)"
    [ -n "$reg" ] || reg="$(read_env_value "$env_file" LLM_HUB_DOMAIN)"
    main="$(read_env_value "$env_file" MAIN_DOMAIN)"
    reg="${reg//\$\{MAIN_DOMAIN\}/$main}"
    case "$reg" in ""|llm-registry:*|localhost*|127.*) return 0 ;; esac
    tmp="$(mktemp)" || return 1
    if docker exec caddy cat /data/caddy/pki/authorities/local/root.crt > "$tmp" 2>/dev/null \
       && grep -q 'BEGIN CERTIFICATE' "$tmp"; then
        rzfz_install_hub_ca "$reg" "$tmp" || rc=1
    else
        echo "  ! cannot read Caddy's internal root CA - docker does not trust ${reg}, so runner pulls on this box fail with 'x509: certificate signed by unknown authority'. Re-run 'rzfz post-install --refresh' once caddy is up." >&2
        rc=1
    fi
    rm -f "$tmp"
    return "$rc"
}

# #3302: the model registry serves anonymous READS only; writes need its
# `llm-manager` identity. Host-side `docker push` to the loopback port logs in
# first: the password (LLM_REGISTRY_WRITE_PASSWORD) goes in on stdin, never in
# argv. docker sends a stored login only after the registry challenges its
# ping, which Zot does for docker clients once named writers exist. Pair every
# login with rzfz_registry_logout. 1 = no credential in the env file or the
# login failed (the caller warns; pushes are then refused by the registry).
# Args: registry (host:port) [env file, default .env]
rzfz_registry_login() {
    local reg="$1" env_file="${2:-.env}" pw
    pw="$(read_env_value "$env_file" LLM_REGISTRY_WRITE_PASSWORD)"
    [ -n "$pw" ] || return 1
    printf '%s' "$pw" | docker login "$reg" --username llm-manager --password-stdin >/dev/null 2>&1
}

rzfz_registry_logout() {
    docker logout "$1" >/dev/null 2>&1 || true
}

# Publish `<base>:<tag>` as a manifest list over `<base>:<tag>-<arch>` refs that
# are already pushed. buildx imagetools first, `docker manifest` as the fallback.
rzfz_registry_manifest_list() {
    local target="$1"; shift
    docker buildx imagetools create -t "$target" "$@" >/dev/null 2>&1 && return 0
    docker manifest rm "$target" >/dev/null 2>&1 || true
    docker manifest create --insecure --amend "$target" "$@" >/dev/null 2>&1 \
        && docker manifest push --insecure "$target" >/dev/null 2>&1
}

# ==============================================================================
#
# Source this file from every razzfazz-*.sh script:
#
#   SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
#   # shellcheck source=scripts/lib.sh
#   source "${SCRIPT_DIR}/scripts/lib.sh"
#
# Provides:
#   - Color codes (RED/GREEN/YELLOW/BLUE/CYAN/MAGENTA/NC)
#   - Print helpers (print_step, print_substep, print_success, print_warning,
#     print_error, print_info, _print_banner_frame)
#   - Optional file logging via $RAZZFAZZ_LOG_FILE (opt-in, ANSI-stripped)
#   - .env utilities (read_env_value, update_env_value, load_env, env_get)
#   - Secret generation (generate_secret, generate_password, generate_hex_secret)
#   - Container check (check_container)
#   - Docker compose helpers (require_docker_compose, docker_compose_cmd)
#   - Prerequisite checks (require_command, check_disk_space, check_ram)
#
# Design rules (M026 / S01):
#   - Sourceable, not executable.
#   - Idempotent re-source (guarded by _RAZZFAZZ_LIB_LOADED).
#   - No side effects at source time except readonly variable definitions.
#   - read_env_value NEVER sources .env files — always grep targeted keys
#     (project memory: feedback_dotenv_no_source.md).
# ==============================================================================

# ------------------------------------------------------------------------------
# Guard against direct execution + idempotent re-source
# ------------------------------------------------------------------------------
# Detect "we are sourced": `return` only succeeds inside a sourced file.
(return 0 2>/dev/null) || {
    echo "scripts/lib.sh must be sourced, not executed" >&2
    exit 1
}

# Idempotent re-source: if a script sources lib.sh and then sources another
# script that also sources lib.sh, second-and-later sources are no-ops.
[[ -n "${_RAZZFAZZ_LIB_LOADED:-}" ]] && return 0
readonly _RAZZFAZZ_LIB_LOADED=1

# ------------------------------------------------------------------------------
# Bash version check
# ------------------------------------------------------------------------------
# We rely on bash 4+ features in some helpers (e.g. ${var,,} lowercase, [[ =~ ]]
# with BASH_REMATCH, associative arrays in callers). Refuse to load on bash 3.
if [[ -z "${BASH_VERSION:-}" ]]; then
    echo "scripts/lib.sh requires bash (current shell does not export BASH_VERSION)" >&2
    # shellcheck disable=SC2317  # `exit` is reachable when sourced from non-bash
    return 1 2>/dev/null || exit 1
fi
if (( BASH_VERSINFO[0] < 4 )); then
    echo "scripts/lib.sh requires bash >= 4.0 (found ${BASH_VERSION})" >&2
    # shellcheck disable=SC2317  # `exit` is reachable when sourced from old bash
    return 1 2>/dev/null || exit 1
fi

# ------------------------------------------------------------------------------
# Colors
# ------------------------------------------------------------------------------
# These are the canonical razzfazz colors used across init.sh / upgrade.sh /
# post-install.sh. MAGENTA and PURPLE are aliases for the same code so callers
# that historically used either name keep working.
# shellcheck disable=SC2034  # consumed by sourcing scripts
{
    readonly RED='\033[0;31m'
    readonly GREEN='\033[0;32m'
    readonly YELLOW='\033[1;33m'
    readonly BLUE='\033[0;34m'
    readonly CYAN='\033[0;36m'
    readonly MAGENTA='\033[0;35m'
    readonly PURPLE='\033[0;35m'
    # M026 #145: DIM (faint) for less-prominent output — secondary/recap
    # lines, "this is the third re-attempt", per-item progress that the
    # operator's eye should slide past unless something interesting
    # happens. Same `\033[2m` faint-attribute as the legacy razzfazz-init
    # banner; surfaced as a gap during S02 #3-#5 when migrating scripts
    # that had ad-hoc `\033[2m` literals.
    readonly DIM='\033[2m'
    readonly NC='\033[0m'
}

# ------------------------------------------------------------------------------
# File logging (opt-in via $RAZZFAZZ_LOG_FILE)
# ------------------------------------------------------------------------------
# If $RAZZFAZZ_LOG_FILE is set and non-empty, every print_* call also appends
# a timestamped, ANSI-stripped line to the file. If unset, no file IO at all.
#
# This eliminates the per-script log-tee duplication we have today in
# razzfazz-upgrade.sh.

# _strip_ansi: remove ANSI CSI sequences from a string.
_strip_ansi() {
    # Strip CSI sequences: ESC [ ... letter
    printf '%s' "$1" | sed -E $'s/\x1b\\[[0-9;]*[A-Za-z]//g'
}

# _log_to_file: append "[ISO-timestamp] <prefix>: <message>" to
# $RAZZFAZZ_LOG_FILE iff that variable is set and non-empty. Failures
# (unwritable file, missing dir) are swallowed so logging never breaks the
# caller.
_log_to_file() {
    local prefix=$1
    local msg=$2
    # Fast exit only if NEITHER sink is configured.
    [[ -z "${RAZZFAZZ_LOG_FILE:-}" && -z "${RAZZFAZZ_JOURNAL_JSON:-}" ]] && return 0
    local plain ts
    plain=$(_strip_ansi "$msg")
    ts=$(date -Iseconds)
    # Human-readable sink (unchanged behaviour).
    if [[ -n "${RAZZFAZZ_LOG_FILE:-}" ]]; then
        if [[ -n "$prefix" ]]; then
            printf '[%s] %s: %s\n' "$ts" "$prefix" "$plain" \
                >> "$RAZZFAZZ_LOG_FILE" 2>/dev/null || true
        else
            printf '[%s] %s\n' "$ts" "$plain" \
                >> "$RAZZFAZZ_LOG_FILE" 2>/dev/null || true
        fi
    fi
    # Structured (JSON-lines) sink — #143: every print_* becomes a machine-
    # readable event. Opt-in via $RAZZFAZZ_JOURNAL_JSON; self-contained escaping
    # so lib.sh stays independent of lib-journal.sh's source order.
    if [[ -n "${RAZZFAZZ_JOURNAL_JSON:-}" ]]; then
        local jmsg
        jmsg=$(printf '%s' "$plain" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\000-\037')
        # Brace-group so a redirection-open failure (e.g. dir not writable) is
        # also swallowed, not just the command's own stderr.
        { printf '{"ts":"%s","level":"%s","msg":"%s"}\n' \
            "$ts" "${prefix:-LOG}" "$jmsg" >> "$RAZZFAZZ_JOURNAL_JSON"; } 2>/dev/null || true
    fi
}

# ------------------------------------------------------------------------------
# Print helpers
# ------------------------------------------------------------------------------
# M026 #145: stream-direction option. Most print_* helpers write to stdout
# (the canonical operator-progress stream). print_error already writes to
# stderr — that's the right default for failures. Callers that need
# non-error progress on stderr (e.g. `setup.sh --json` wants stdout to be
# pure JSON, with progress to stderr so a pipe can still parse) can opt in
# via $RAZZFAZZ_PRINT_STREAM=stderr in the environment. Per-call override
# is also possible: `RAZZFAZZ_PRINT_STREAM=stderr print_step "..."`.
# Default value of "stdout" preserves byte-identical behaviour for every
# existing caller.
_print_stream() {
    case "${RAZZFAZZ_PRINT_STREAM:-stdout}" in
        stderr|2) printf '%s\n' "$1" >&2 ;;
        *)        printf '%s\n' "$1" ;;
    esac
}

print_step() {
    _print_stream "$(printf '%b[STEP]%b %s' "$BLUE" "$NC" "$*")"
    _log_to_file "STEP" "$*"
}

print_substep() {
    _print_stream "$(printf '%b  →%b %s' "$CYAN" "$NC" "$*")"
    _log_to_file "" "  -> $*"
}

print_success() {
    _print_stream "$(printf '%b[✓]%b %s' "$GREEN" "$NC" "$*")"
    _log_to_file "OK" "$*"
}

print_warning() {
    _print_stream "$(printf '%b[!]%b %s' "$YELLOW" "$NC" "$*")"
    _log_to_file "WARN" "$*"
}

# print_error: ALWAYS writes to stderr regardless of RAZZFAZZ_PRINT_STREAM.
# Failures are not progress; they belong on stderr by convention. Does NOT
# exit — callers decide.
print_error() {
    printf '%b[✗]%b %s\n' "$RED" "$NC" "$*" >&2
    _log_to_file "ERROR" "$*"
}

# #2613: the upgrade test suite parks the box's real .env / .env.dify next to
# a test fixture (.test-backup, .test-inode, .test-absent, .test-backup.partial,
# .test-restore). A run killed hard leaves the fixture live and the original
# parked; `rzfz upgrade` / `rzfz init --force` would then migrate or re-mint the
# fixture. Print one line per leftover and return 1 when any exists.
# $1 = stack dir, $2 = the command that refuses (for the message).
refuse_leftover_env_test_backups() {
    local dir="${1:-.}" cmd="${2:-this command}" f s p q found=0
    for f in .env .env.dify; do
        for s in test-backup test-inode test-absent test-backup.partial test-restore; do
            p="${dir}/${f}.${s}"
            if [ -e "$p" ] || [ -L "$p" ]; then
                found=1
                printf -v q '%q' "$p"
                print_error "Leftover from an interrupted test-suite run: ${q}"
            fi
        done
        if [ -e "${dir}/${f}.test-backup" ]; then
            printf -v q '%q' "${dir}/${f}"
            print_info "  ${f}.test-backup is your ORIGINAL; the live ${f} may be a test fixture. Restore it: mv ${q}.test-backup ${q}"
        fi
    done
    if [ "$found" = 1 ]; then
        print_error "${cmd} refuses to start while these files exist: it would work on the test fixture instead of this box's configuration. tests/test-upgrade.sh prints the exact instruction for each file. Nothing was changed."
        return 1
    fi
    return 0
}

print_info() {
    _print_stream "$(printf '%b[i]%b %s' "$MAGENTA" "$NC" "$*")"
    _log_to_file "INFO" "$*"
}

# _print_banner_frame: draw the standard 68-char box rim in the requested
# color, with the title centered between the side rules. Callers supply
# their own title (skripts have different titles); this helper just owns
# the framing so it stays consistent across scripts.
#
# Usage: _print_banner_frame "$RED" "razzfazz.ai Service Stack Initialization"
_print_banner_frame() {
    local color=${1:-$RED}
    local title=${2:-razzfazz.ai}
    local inner=66  # interior width between the ║ side rules
    local pad_left pad_right
    local title_len=${#title}
    if (( title_len > inner )); then
        title=${title:0:inner}
        title_len=$inner
    fi
    pad_left=$(( (inner - title_len) / 2 ))
    pad_right=$(( inner - title_len - pad_left ))
    printf '%b\n' "${color}"
    printf '╔══════════════════════════════════════════════════════════════════╗\n'
    printf '║%*s%*s║\n' "$inner" "" 0 ""
    printf '║%*s%s%*s║\n' "$pad_left" "" "$title" "$pad_right" ""
    printf '║%*s%*s║\n' "$inner" "" 0 ""
    printf '╚══════════════════════════════════════════════════════════════════╝\n'
    printf '%b\n' "${NC}"
}

# ==============================================================================
# rzfz_node_role — what kind of box is this? (#266, operator decision E7)
#
# Until now nothing declared it. A thin inference node was distinguished from a
# full stack only by which env file happened to exist — a fact known to exactly
# three files (cli/node-init.sh, this one, scripts/install-worker.sh.tmpl) while
# every other verb hardcoded `.env` and, on a worker, read a file that is not
# there. `rzfz status` on a worker did not say "ask the master"; it printed the
# result of a read into nothing.
#
# Prints one of:
#   full    a stack — `.env` is present
#   worker  a thin inference node — `.env.node` and no `.env` (cli/node-init.sh)
#   none    neither; not an installed box (a fresh checkout, a test tmpdir)
#
# `.env` WINS when both exist, and that is not a tie-break — it is the answer.
# A full box may also run the worker agent (the `llm-worker-agent` profile), so
# it carries `.env.node` too; it is still a stack, and every stack-bound verb
# must keep working on it.
#
# `none` is deliberately NOT `worker`. Refusing a verb needs positive evidence
# that this box IS a worker; a directory with neither file is a checkout or a
# test fixture, and answering "worker" there would break every caller that runs
# a verb outside an installed box.
#
# Reads the filesystem, never the environment: a role is what a box IS, not a
# knob an operator turns. An operator who set a variable to `worker` on a full
# stack would otherwise be the next incident.
# ==============================================================================
rzfz_node_role() {
    local root="${1:-${SCRIPT_DIR:-.}}"
    if [ -f "${root}/.env" ]; then printf 'full\n'; return 0; fi
    if [ -f "${root}/.env.node" ]; then printf 'worker\n'; return 0; fi
    printf 'none\n'
}

# ==============================================================================
# refuse_on_worker_node — a stack-bound verb says so instead of reading nothing
#
# Args: <verb> [what-to-do-instead]
# Exits 3 on a worker node; returns 0 everywhere else (a stack AND an
# uninstalled directory — see the `none` note above).
#
# Exit 3, not 1: a refusal because of WHAT THIS BOX IS is not the same outcome
# as the verb running and failing, and a caller that wraps these (rzfz, a test,
# an operator's script) must be able to tell them apart.
# ==============================================================================
refuse_on_worker_node() {
    local verb="$1" instead="${2:-Run it on the master that manages this node.}"
    [ "$(rzfz_node_role)" = "worker" ] || return 0
    print_error "\`rzfz ${verb}\` does not apply to this box."
    print_info  "This is a thin inference node (it carries .env.node and no .env):"
    print_info  "the worker agent plus the engines the master deploys — no Caddy,"
    print_info  "no Authentik, no Postgres, no portals. ${instead}"
    exit 3
}

# ------------------------------------------------------------------------------
# .env utilities
# ------------------------------------------------------------------------------
# IMPORTANT (project memory: feedback_dotenv_no_source.md):
# Operator-edited .env files routinely carry spaces, hash-comments, URL colons,
# `=` inside passwords, and shell metachars. We NEVER `source` them in a shell
# script — we always grep a targeted key and parse just that one line.
#
# read_env_value: print the value of $key in $file to stdout. Returns 0 even
# when the key is missing (caller can `[[ -z "$v" ]]` to detect "unset");
# returns 0 with empty stdout when the file does not exist.
#
# Handles:
#   - Lines with `=` inside the value (URLs, passwords with `=`):
#       cut -d'=' -f2-  preserves everything after the first `=`
#   - Inline comments on unquoted values:
#       KEY=value   # comment   →  "value"
#     Quoted values keep their interior literal — `#` inside quotes is part
#     of the value (matches upstream dotenv parsers).
#   - Single OR double quoted values: outer quotes are stripped, interior
#     is returned verbatim.
#
# This is the rc6.2 #1/#2-fixed version originally in razzfazz-upgrade.sh —
# the .env.example bumps for KOMODO_DB_VERSION etc. carry trailing
# `# ferretdb-postgres` style comments, and pre-rc6.2 readers compared the
# value+comment string against the bare default and silently treated every
# bump as a "custom value".
# #1618 — ONE notion of "this line assigns KEY", written out at every site.
#
# There were three in this tree and they disagreed. `read_env_value` and
# `update_env_value` anchored on `^KEY=`; `load_env` (below) trims leading
# whitespace and strips an `export ` prefix, because that is what an
# operator-edited file looks like and what docker compose reads. Since
# `update_env_value` APPENDS when it does not find the key, a file carrying
#
#       LLM_MANAGER_CA_PEM=          (one leading space)
#
# came out of a migration holding that key TWICE — and the two readers then
# disagreed about which value it had (#1185 is the same class in the OWUI
# connection lines).
#
# WRITTEN OUT AT EACH SITE, not called from a shared helper. The helper is the
# tidier code and the wrong engineering here: `grep -qE "$(missing_fn)"`
# expands to an EMPTY pattern, an empty ERE matches every line, so every key
# looks present, nothing is ever appended, and nothing says so. Not
# hypothetical — it is what 15 tests showed the first time this was built with
# a helper, because they slice a function out of cli/post-install.sh without
# sourcing this file. A literal cannot fail that way.
#
# A COMMENTED-OUT line is still not an assignment: `#` is neither whitespace
# nor `export`. #1571's migration rule leans on that.
#
# tests/unit/scripts/test_1618_env_line_anchoring.py DERIVES the site list from
# the tree and fails on any strict `^KEY=` left behind, so the copies cannot
# drift apart.
read_env_value() {
    local file=$1
    local key=$2
    [[ -f "$file" ]] || return 0
    local raw
    raw=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" 2>/dev/null | head -n1 | cut -d'=' -f2- || true)
    # M026 #145: CRLF-safe. Operator-edited .env files copied from a Windows
    # machine (or saved through some IDE that defaults to \r\n) carry a
    # trailing \r on every line. Without stripping it the \r ends up in the
    # value and downstream consumers see (e.g.) `2026.05-rc6.7\r` which
    # docker-compose treats as a different version than `2026.05-rc6.7`.
    raw=${raw%$'\r'}
    # Double-quoted value: KEY="..."  [optional inline comment]
    if [[ "$raw" =~ ^\"(.*)\"[[:space:]]*(#.*)?$ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi
    # Single-quoted value: KEY='...'  [optional inline comment]
    if [[ "$raw" =~ ^\'(.*)\'[[:space:]]*(#.*)?$ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi
    # Unquoted: strip a trailing ` # comment` and trailing whitespace.
    printf '%s\n' "$raw" | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//'
}

# #3303: up to 2026.09-ga.5 config/.env.example shipped a non-empty default for
# GOOGLE_OAUTH_DOMAIN, so every box `rzfz init` created with Google sign-in
# admits that domain's accounts without the operator having chosen it. 2026.11
# (#2927) made the default empty, and env migration keeps an existing value on
# purpose. The old default is kept here as a SHA-256 only, in this one place.
RAZZFAZZ_GOOGLE_DOMAIN_OLD_DEFAULT_SHA256="4000b246320949b0eb8334d85dd6524d3cb5ce2983d7eb36d066279b25121016"

# razzfazz_google_domain_is_old_default <env_file>
#   0 = Google sign-in is on, the box is not on the internal channel (an unset
#   RAZZFAZZ_CHANNEL is internal, as razzfazz_channel reads it) and
#   GOOGLE_OAUTH_DOMAIN is exactly the old template default (case, spaces and
#   stray commas ignored). 1 otherwise. Reads only; never changes the value.
razzfazz_google_domain_is_old_default() {
    local env_file="$1" enabled channel domain
    [ -f "$env_file" ] || return 1
    enabled=$(read_env_value "$env_file" ENABLE_GOOGLE_OAUTH | tr '[:upper:]' '[:lower:]')
    [ "$enabled" = true ] || return 1
    channel=$(read_env_value "$env_file" RAZZFAZZ_CHANNEL)
    [ "$channel" = public ] || return 1
    domain=$(read_env_value "$env_file" GOOGLE_OAUTH_DOMAIN | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    domain="${domain#,}"; domain="${domain%,}"
    [ -n "$domain" ] || return 1
    [ "$(printf '%s' "$domain" | sha256sum | cut -d' ' -f1)" = "$RAZZFAZZ_GOOGLE_DOMAIN_OLD_DEFAULT_SHA256" ]
}

# The one operator sentence for that condition (status row and upgrade warning).
razzfazz_google_domain_old_default_advice() {
    printf '%s' "Google sign-in admits every account of the domain in GOOGLE_OAUTH_DOMAIN, and this box still has the value the .env template shipped as its default up to 2026.09-ga.5: nobody chose it for this box. Set your own Google Workspace domain(s) in the Settings portal (Authentication), or turn Google sign-in off there."
}

# update_env_value: idempotently set $key=$value in $file. If the key exists,
# its line is replaced; otherwise the line is appended. Returns 1 if the file
# does not exist (caller decides whether to create it).
#
# The sed substitution uses `|` as the delimiter and pre-escapes `\`, `&`,
# and `|` in the value so values containing `/` (paths, URLs) work correctly.
# ==============================================================================
# #906 — write *_DOMAIN EXPANDED, never as a `X.${MAIN_DOMAIN}` template
# ==============================================================================
# `.env.example` ships the subdomain keys as templates, and `rzfz init` only
# sets MAIN_DOMAIN — so `.env` keeps `OPENWEBUI_DOMAIN=chat.${MAIN_DOMAIN}`.
# Measured on the re-domained Exoscale box: OWUI got
# `OPENID_PROVIDER_URL=https://auth.${MAIN_DOMAIN}/…` verbatim, the OIDC
# discovery fetch went to a host of that literal name, and every SSO login
# answered 500.
#
# WHY it lands there is worth getting right, because the first version of this
# comment sent the reader to the wrong place (#906 review, measured on Compose
# 2.40.3 and 5.0.2). Compose DOES resolve a nested `${MAIN_DOMAIN}` when it
# reads `.env` itself, through `env_file:` as well. What does not resolve is the
# PROCESS ENVIRONMENT: `load_env` exports the raw template, and an exported
# variable beats the file while compose does not recurse into it — it even
# escapes the `$`. The same container then gets one key right (from the file)
# and one key wrong (from the env), which is how this hides. Identical class to
# #949 / #1250 / #1486; `cli/post-install.sh` already says it in as many words
# at its own `load_env` call.
#
# That is also why the narrow `_DOMAIN` filter is RIGHT rather than too narrow:
# it bites exactly where a script exports the value or reads it directly with
# `read_env_value`, and those are the domain keys. `SMTP_FROM`,
# `SYNAPSE_SERVER_NAME` and `GITEA_ADMIN_EMAIL` carry the same template shape
# and are left alone on purpose — compose resolves them from the file, and
# nothing exports them raw.
#
# Consequence for future manifest entries: after this runs, a box holds RESOLVED
# values, so a `change_default` on a `*_DOMAIN` key can never again match a
# manifest value written as `x.${MAIN_DOMAIN}`. #1444 built its own migration
# for exactly that reason; from here on it is permanent.
#
# So the file gets the resolved value. Deliberately narrow: ONLY `${MAIN_DOMAIN}`
# and `$MAIN_DOMAIN` are substituted, in keys that end in `_DOMAIN`, and only
# when MAIN_DOMAIN itself is set and carries no template. Nothing is sourced
# (#949: an .env value may contain metacharacters), an operator's own value
# without a template is untouched, and a second run changes nothing.
#
# $2 = "dry" → report what WOULD change and write nothing (the upgrade's
# --check path must not touch .env).
#
# Prints one line per rewritten key; returns 0 even when there is nothing to do.
resolve_domain_templates() {
    local env_file="${1:-.env}" mode="${2:-write}" main key value expanded changed=0
    [ -f "$env_file" ] || return 0
    main=$(read_env_value "$env_file" MAIN_DOMAIN 2>/dev/null) || main=""
    case "$main" in
        ""|*'${MAIN_DOMAIN}'*|*'$MAIN_DOMAIN'*)
            # No usable domain (or a self-referential one) — nothing to expand.
            return 0 ;;
    esac
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        value=$(read_env_value "$env_file" "$key" 2>/dev/null) || value=""
        case "$value" in
            *'${MAIN_DOMAIN}'*|*'$MAIN_DOMAIN'*) ;;
            *) continue ;;
        esac
        expanded="${value//\$\{MAIN_DOMAIN\}/$main}"
        expanded="${expanded//\$MAIN_DOMAIN/$main}"
        [ "$expanded" != "$value" ] || continue
        [ "$mode" = "dry" ] || update_env_value "$env_file" "$key" "$expanded"
        printf '  %s: %s -> %s\n' "$key" "$value" "$expanded"
        changed=$((changed + 1))
    done <<< "$(grep -oE '^[A-Z0-9_]+_DOMAIN=' "$env_file" 2>/dev/null | sed 's/=$//' | sort -u)"
    return 0
}

# #2644 — the other half of a domain change in `rzfz init`: the keys #906
# already RESOLVED.
#
# resolve_domain_templates (above) writes `auth.<domain>` as a literal, so on
# the NEXT init with a different --domain there is no template left to expand:
# MAIN_DOMAIN moved and every `*_DOMAIN` key stayed on the old domain. Measured
# by the init suite's reconfigure scenario (`--no-secrets`, new domain): Caddy
# kept serving `auth.<old>` (`{$AUTHENTIK_DOMAIN}`), so `https://auth.<new>`
# answered TLSV1_ALERT_INTERNAL_ERROR — no site, no certificate for that name.
#
# The rule is NOT re-implemented here: it is razzfazz_common.redomain (#2376),
# the one whole-host rule `rzfz setup --set-domain` and the Config Portal use
# (and the SQL / awk of scripts/redomain-reconcile.sh mirror). A value moves
# only when it IS `<old>` or ends in `.<old>`; `*_OAUTH_DOMAIN` (the customer's
# IdP allow-list) and MAIN_DOMAIN (the caller's to write) never move; a value
# that still templates `${MAIN_DOMAIN}` follows on its own and is left as it
# is; with the new domain under the old one, a value already under the new
# domain stays (no `hub.ai.ai.kunde.at`). Ports are carried over by the rule.
# With a 4th argument, the `*_URL` / `*_ORIGINS` keys of that .env.dify move by
# the same rule (their values can carry credentials: only the key is printed).
# Values are read with read_env_value; nothing is sourced (#949).
#
# Usage: redomain_derived_domain_keys <env_file> <old_domain> <new_domain> [<dify_env_file>]
# Prints one line per rewritten key. Returns 0 when there is nothing to do, and
# 1 — having written nothing — when the rule cannot be evaluated.
redomain_derived_domain_keys() {
    local env_file="${1:-.env}" old="${2:-}" new="${3:-}" dify_file="${4:-}"
    local common pairs="" plan key value kind a b
    [ -f "$env_file" ] || return 0
    [ -n "$old" ] && [ -n "$new" ] && [ "$old" != "$new" ] || return 0
    [ -n "$dify_file" ] && [ -f "$dify_file" ] || dify_file=""
    common="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/core/common"
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        value=$(read_env_value "$env_file" "$key" 2>/dev/null) || value=""
        pairs+="${key}=${value}"$'\n'
    done <<< "$(grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Z0-9_]+_DOMAIN=' "$env_file" 2>/dev/null \
                | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' | sort -u)"
    # The plan is computed in full BEFORE anything is written.
    plan=$(printf '%s' "$pairs" | PYTHONPATH="$common" python3 -c '
import sys
from razzfazz_common.redomain import redomain_env_domains, redomain_dify_values
old, new, dify = sys.argv[1:4]
vals = {}
for line in sys.stdin.read().splitlines():
    k, sep, v = line.partition("=")
    if sep:
        vals[k] = v
for k, v in sorted(redomain_env_domains(vals, vals, old, new).items()):
    print("env\t%s\t%s\t%s" % (k, vals[k], v))
if dify:
    raw = {}
    with open(dify, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                raw[k.strip()] = v.strip()
    for k, v in sorted(redomain_dify_values(raw, old, new).items()):
        print("dify\t%s\t-\t%s" % (k, v))
' "$old" "$new" "$dify_file") || return 1
    while IFS=$'\t' read -r kind key a b; do
        case "$kind" in
            env)
                update_env_value "$env_file" "$key" "$b"
                printf '  %s: %s -> %s\n' "$key" "$a" "$b" ;;
            dify)
                update_env_value "$dify_file" "$key" "$b"
                printf '  %s (%s)\n' "$key" "$(basename "$dify_file")" ;;
        esac
    done <<< "$plan"
    return 0
}

update_env_value() {
    local file=$1
    local key=$2
    local value=$3

    if [[ ! -f "$file" ]]; then
        return 1
    fi

    if grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file"; then
        # INODE-PRESERVING in-place edit. `sed -i` writes a NEW file and renames it
        # over the target → new inode. The razzfazz-config container bind-mounts
        # .env / .env.dify as single FILES (inode-based); a new inode makes that
        # mount go stale → /stack/.env becomes read-only in the container → the
        # Configuration Portal can no longer toggle modules ("no write permission
        # to /stack/.env"). `cat tmp > file` truncates the SAME inode, so the mount
        # stays valid. See project_config_ui_env_write_broken.
        local _t="${file}.tmp.$$"
        # #1618: the capture keeps whatever the operator wrote in FRONT of
        # the key — leading whitespace, an `export ` — and only the value
        # changes. The file belongs to the operator; re-formatting his line
        # is a visible edit nobody asked for, and one a `git diff` on a
        # customer box then has to explain.
        # #2543: the scripts run under umask 022 now, so the staging copy of
        # a secrets file is created private explicitly, not by the umask.
        # #2746: the value reaches awk through its ENVIRONMENT (ENVIRON does no
        # backslash processing, so no escaping is needed either). The old sed
        # form put every password this function ever wrote on sed's argv.
        if (umask 077; RZFZ_UEV_KEY="$key" RZFZ_UEV_VAL="$value" awk '
            BEGIN { k = ENVIRON["RZFZ_UEV_KEY"]; v = ENVIRON["RZFZ_UEV_VAL"] }
            { if (match($0, "^[[:space:]]*(export[[:space:]]+)?" k "=")) print substr($0, 1, RLENGTH) v; else print }
        ' "$file" > "$_t"); then
            cat "$_t" > "$file"
        fi
        rm -f "$_t"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

# delete_env_lines FILE ERE
#
# Removes the lines matching ERE from an env file, in place (same inode, see
# update_env_value): `sed -i` renames a new file over it, which breaks the single-
# file bind of every container holding it. Nothing changes when the staging copy
# fails, and the staging copy is private, because the file holds secrets.
delete_env_lines() {
    local file=$1 ere=$2 _t rc=0
    [[ -f "$file" ]] || return 1
    _t="${file}.tmp.$$"
    (umask 077; grep -vE -- "$ere" "$file" > "$_t") || rc=$?
    # grep exits 1 when no line is left: an empty result is still a result.
    if [ "$rc" -le 1 ]; then
        cat "$_t" > "$file" || rc=2
        [ "$rc" -eq 1 ] && rc=0
    fi
    rm -f "$_t"
    return "$rc"
}

# sync_dify_valkey_password [ENV_FILE] [DIFY_ENV_FILE]
#
# Dify reaches the shared cache with the stack's VALKEY_PASSWORD, but its own
# env file keeps two copies of it: REDIS_PASSWORD and the password inside
# CELERY_BROKER_URL. THE one sync for every path that changes VALKEY_PASSWORD
# (upgrade, init, rotate-secret): both copies follow the stack value, in place
# (same inode), with the value never on argv. The compose files also derive both
# from VALKEY_PASSWORD, so a stale copy can no longer win; this keeps the file
# itself honest. Returns 0 when it rewrote something, 1 when nothing differed
# (or there is nothing to sync).
sync_dify_valkey_password() {
    local env_file=${1:-.env} dify_file=${2:-.env.dify}
    [ -f "$dify_file" ] || return 1
    local pw cur_redis cur_broker want
    pw=$(read_env_value "$env_file" VALKEY_PASSWORD)
    [ -n "$pw" ] || return 1
    want="redis://:${pw}@valkey:6379/1"
    cur_redis=$(read_env_value "$dify_file" REDIS_PASSWORD)
    cur_broker=$(read_env_value "$dify_file" CELERY_BROKER_URL)
    if [ "$cur_redis" = "$pw" ] && [ "$cur_broker" = "$want" ]; then
        return 1
    fi
    update_env_value "$dify_file" REDIS_PASSWORD "$pw"
    update_env_value "$dify_file" CELERY_BROKER_URL "$want"
    return 0
}

# ------------------------------------------------------------------------------
# #2977 - the Valkey password is rotated once per rotation generation
# ------------------------------------------------------------------------------
# docling-serve logged the RQ Redis URL, password included, at every worker
# start (2026.09 up to 2026.11-rc6), so the password sat in docker logs, log
# bundles and, on boxes that ship container logs, in the SIEM. The upgrade to
# the release that stops the log line rotates the password once, for every
# consumer together (cli/rotate-secret.sh --rotate VALKEY_PASSWORD).
#
# The marker is the .env key VALKEY_PASSWORD_ROTATION. It holds the generation
# the CURRENT password belongs to, so it travels with the password it
# describes: restoring an older .env restores both. A later release that needs
# another rotation raises the generation; an operator re-arms by emptying the
# key, or rotates on demand with `rzfz rotate-secret --rotate VALKEY_PASSWORD`
# (which stamps it too). init.sh stamps a new box: its password was never in
# a log line.
VALKEY_ROTATION_GENERATION="2026.11"

# valkey_rotation_due [ENV_FILE]: 0 when the box's password is of an older
# generation than this tree knows, 1 when it has been rotated since.
valkey_rotation_due() {
    local env_file=${1:-.env} have
    have=$(read_env_value "$env_file" VALKEY_PASSWORD_ROTATION 2>/dev/null || true)
    [ "$have" != "$VALKEY_ROTATION_GENERATION" ]
}

# containers_holding_secret <secret>: names of the RUNNING containers whose
# configured environment contains the secret as a substring (an env var of its
# own, or inside a URL such as redis://:<secret>@valkey). The secret goes to
# grep through a pattern file on a descriptor, never argv. A container that is
# not running is not listed: it holds the value but uses nothing.
containers_holding_secret() {
    local secret=${1:-} name
    [ -n "$secret" ] || return 0
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        if docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$name" 2>/dev/null \
                | grep -qF -f <(printf '%s\n' "$secret"); then
            printf '%s\n' "$name"
        fi
    done < <(docker ps --format '{{.Names}}' 2>/dev/null)
}

# valkey_accepts_password <password>: 0 when valkey ANSWERS an authenticated
# PING with it. Used to prove that the old password is refused after a
# rotation. The password travels in the exec environment by name.
valkey_accepts_password() {
    local out
    out=$(REDISCLI_AUTH="${1:-}" docker exec -e REDISCLI_AUTH valkey valkey-cli --no-auth-warning PING 2>&1) || true
    [ "$out" = "PONG" ]
}

# valkey_rotation_stale_check <old-password>: the verdict after a rotation.
# 1 (and a named list) when valkey still accepts the old password, or when a
# running container still carries it in its environment (it would fail its
# next connection, silently, long after the gate). Names only, never values.
valkey_rotation_stale_check() {
    local old=${1:-} rc=0 stale
    [ -n "$old" ] || return 0
    if valkey_accepts_password "$old"; then
        print_error "Valkey still accepts the OLD password: the rotation did not take effect."
        rc=1
    fi
    stale=$(containers_holding_secret "$old")
    if [ -n "$stale" ]; then
        print_error "These running containers still carry the OLD Valkey password and will be refused:"
        printf '%s\n' "$stale" | sed 's/^/    /'
        print_info  "  Services of the stack: docker compose up -d --force-recreate <service>."
        print_info  "  Agent instances (Paperclip): rzfz post-install --refresh re-keys them."
        rc=1
    fi
    return $rc
}

# detect_engine_gpu_group_gids: print this host's video/render group ids,
# comma-separated (e.g. `44,992`), or nothing when the host has neither.
#
# #2393: an AMD engine runs as nobody only with these GIDs — it opens /dev/kfd
# and /dev/dri through them. THE one detection, shared by the master
# (post-install's ensure_engine_gpu_group_gids), `rzfz node-init`,
# `rzfz node-upgrade` and the curl|bash worker installer (#2530). A host without
# a `render` group still yields `video`: getent's non-zero exit is not the
# answer, its output is. Reads nothing and writes nothing; whether to record the
# value (never over an operator's, `none` included) is the caller's rule.
detect_engine_gpu_group_gids() {
    { getent group video render 2>/dev/null || true; } | cut -d: -f3 | paste -sd, -
}

# repair_repo_file_modes <from-commit> <to-commit>
#
# #2543: give every file a code update WROTE the mode git records for it —
# 0644, or 0755 where git has the executable bit — when it came out unreadable
# to other users. Run from the repo root, after the checkout.
#
# Why this exists next to `umask 022` at the top of upgrade.sh: the checkout of
# a hop is done by the OLD script (the new one is only re-exec'd afterwards).
# An old script run under a CIS umask (027/077) has already written every
# changed file 0640/0600 by the time the new code runs, and a non-root
# container that bind-mounts one (razzfazz-help: versions.json) crash-loops.
#
# Scope, on purpose: only the files the hop changed (git diff from..to), and
# the directories the hop CREATED (not in the <from> tree). Files the hop did
# not touch keep their modes — including the management scripts harden-host.sh
# restricts to 0700 — and so do directories that existed before (an operator's
# 0700 certs/). Only missing read (and, for 0755, execute) bits are added
# (chmod a+r / a+rx); nothing is ever made more restrictive. Symlinks and
# submodules are skipped. A file this user cannot chmod is counted and named.
repair_repo_file_modes() {
    local from=$1 to=$2
    command -v git >/dev/null 2>&1 && [ -d .git ] || return 0
    if [ -z "$from" ] || ! git rev-parse -q --verify "${from}^{commit}" >/dev/null 2>&1; then
        # RAZZFAZZ_COMMIT is absent or not in this clone: the HEAD before the
        # checkout is the reflog's previous entry.
        from=$(git rev-parse -q --verify 'HEAD@{1}^{commit}' 2>/dev/null) || from=""
    fi
    [ -n "$from" ] || { echo "  #2543: no pre-update commit known — file modes not checked." >&2; return 0; }
    to=$(git rev-parse -q --verify "${to:-HEAD}^{commit}" 2>/dev/null) || return 0
    [ "$from" != "$to" ] || return 0

    local meta path dst need add cur d fixed=0 failed=0 failed_list=""
    local -A seen_dir=()
    while IFS= read -r -d '' meta && IFS= read -r -d '' path; do
        # meta: ":<src mode> <dst mode> <src sha> <dst sha> <status>"
        dst=${meta#:* }
        dst=${dst%% *}
        # Additive only (fix round 1): the bits git's mode promises to others
        # are ADDED; a group-writable 0670 stays group-writable.
        case "$dst" in
            100644) need=444; add="a+r" ;;
            100755) need=555; add="a+rx" ;;
            *) continue ;;   # deleted (000000), symlink, submodule
        esac
        [ -f "$path" ] && [ ! -L "$path" ] || continue
        cur=$(stat -c %a -- "$path" 2>/dev/null) || continue
        if (( (8#$cur & 8#$need) != 8#$need )); then
            if chmod "$add" -- "$path" 2>/dev/null; then
                fixed=$((fixed + 1))
            else
                failed=$((failed + 1)); failed_list="${failed_list} ${path}"
            fi
        fi
        d=$(dirname -- "$path")
        while [ "$d" != "." ] && [ -z "${seen_dir[$d]:-}" ]; do
            seen_dir[$d]=1
            # Only a directory the hop CREATED (absent from the <from> tree) got
            # its mode from this checkout's umask. One that existed before is the
            # operator's — a 0700 certs/ stays 0700 when certs/README.md changes.
            if git cat-file -e "${from}:${d}" 2>/dev/null; then
                d=$(dirname -- "$d"); continue
            fi
            if [ -d "$d" ] && [ ! -L "$d" ] && cur=$(stat -c %a -- "$d" 2>/dev/null) \
               && (( (8#$cur & 8#555) != 8#555 )); then
                if chmod a+rx -- "$d" 2>/dev/null; then
                    fixed=$((fixed + 1))
                else
                    failed=$((failed + 1)); failed_list="${failed_list} ${d}/"
                fi
            fi
            d=$(dirname -- "$d")
        done
    done < <(git diff --raw -z --no-renames "$from" "$to" --)

    if [ "$fixed" -gt 0 ]; then
        echo "  #2543: ${fixed} file(s)/dir(s) the code update wrote were unreadable to other users (restrictive umask) — set back to git's 0644/0755."
    fi
    if [ "$failed" -gt 0 ]; then
        echo "  WARNING #2543: could not fix the mode of ${failed} path(s) (not owned by $(id -un 2>/dev/null || echo this user)):${failed_list:0:400}" >&2
        echo "    Containers that bind-mount them may fail to read them. Fix: sudo chmod a+rX <path>" >&2
        return 1
    fi
    return 0
}

# load_env: print every KEY=VALUE pair from $file as one `KEY=VALUE` line per
# stdout line, with comments stripped and quotes removed. Intended for callers
# that want to feed the result into a `while read` loop or build an
# associative array — NOT for `eval`/`source`-ing into the current shell
# (see project-memory rule above).
#
# Lines starting with `#`, blank lines, and lines without `=` are skipped.
load_env() {
    local file=$1
    [[ -f "$file" ]] || return 0
    local line key raw val
    while IFS= read -r line || [[ -n "$line" ]]; do
        # skip comments and blanks
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" != *=* ]] && continue
        key=${line%%=*}
        raw=${line#*=}
        # trim leading whitespace + optional `export ` from key
        key=${key#"${key%%[![:space:]]*}"}
        key=${key#export }
        # apply the same quote/inline-comment rules as read_env_value, inline,
        # so we don't re-grep the same file (SC2094-clean).
        if [[ "$raw" =~ ^\"(.*)\"[[:space:]]*(#.*)?$ ]]; then
            val=${BASH_REMATCH[1]}
        elif [[ "$raw" =~ ^\'(.*)\'[[:space:]]*(#.*)?$ ]]; then
            val=${BASH_REMATCH[1]}
        else
            val=$(printf '%s' "$raw" | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//')
        fi
        printf '%s=%s\n' "$key" "$val"
    done < "$file"
}

# env_get: thin wrapper around read_env_value that consults a default file
# location ($RAZZFAZZ_ENV_FILE, falling back to ./.env in the current dir).
# Convenience for callers that always read from the same .env.
env_get() {
    local key=$1
    local file=${RAZZFAZZ_ENV_FILE:-.env}
    read_env_value "$file" "$key"
}

# ------------------------------------------------------------------------------
# #2507 — Open WebUI's own login keys, derived from the SSO switches
# ------------------------------------------------------------------------------
# Open WebUI 0.11.3 reads GOOGLE_CLIENT_ID/SECRET and OAUTH_AUTO_REDIRECT as
# PLAIN env (config.py:2498/2507, not PersistentConfig), and its login page
# auto-redirects only with EXACTLY ONE provider and the login form off. OWUI and
# Authentik's Google source used to share GOOGLE_CLIENT_ID, so an OIDC box
# showed two buttons (Google direct + Authentik) and never auto-redirected —
# prod carried a hand-made overlay (razzfazz-local/owui-sso.override.yml) that
# blanked GOOGLE_CLIENT_ID/SECRET for the openwebui container only and set
# OAUTH_AUTO_REDIRECT=true. These keys replace that overlay:
#
#   ENABLE_OPENWEBUI_OIDC=true -> OWUI_GOOGLE_CLIENT_ID/SECRET empty (Authentik,
#                                 which fronts Google itself, is OWUI's only
#                                 provider), OWUI_OAUTH_AUTO_REDIRECT=true
#   otherwise                  -> OWUI_GOOGLE_CLIENT_ID/SECRET mirror
#                                 GOOGLE_CLIENT_ID/SECRET (exactly what OWUI got
#                                 before the split), OWUI_OAUTH_AUTO_REDIRECT=false
#
# GOOGLE_CLIENT_ID/SECRET stay untouched: Authentik's Google source and Gitea's
# Google source still read them. Derived state — init and every upgrade
# recompute it, so an operator changes the SWITCHES, not these keys. The
# persisted half (ui.enable_login_form) is post-install's owui_reconcile_sso_login.
# Prints one line per changed key; returns 0.
derive_owui_sso_env() {
    local env_file="${1:-.env}" oidc want_id want_secret want_redirect want_active key want cur
    [ -f "$env_file" ] || return 0
    oidc=$(read_env_value "$env_file" ENABLE_OPENWEBUI_OIDC 2>/dev/null) || oidc=""
    if [ "$oidc" = "true" ]; then
        want_id=""
        want_secret=""
        want_redirect="true"
        want_active="true"
    else
        want_id=$(read_env_value "$env_file" GOOGLE_CLIENT_ID 2>/dev/null) || want_id=""
        want_secret=$(read_env_value "$env_file" GOOGLE_CLIENT_SECRET 2>/dev/null) || want_secret=""
        want_redirect="false"
        want_active=""
    fi
    # #2933: OWUI_OIDC_ACTIVE gates OAUTH_CLIENT_ID/SECRET/OPENID_PROVIDER_URL in
    # modules/chat/compose.yml, so no dead "Sign in with Authentik" button.
    for key in OWUI_GOOGLE_CLIENT_ID OWUI_GOOGLE_CLIENT_SECRET OWUI_OAUTH_AUTO_REDIRECT OWUI_OIDC_ACTIVE; do
        case "$key" in
            OWUI_GOOGLE_CLIENT_ID)     want=$want_id ;;
            OWUI_GOOGLE_CLIENT_SECRET) want=$want_secret ;;
            OWUI_OIDC_ACTIVE)          want=$want_active ;;
            *)                         want=$want_redirect ;;
        esac
        cur=$(read_env_value "$env_file" "$key" 2>/dev/null) || cur=""
        # An absent key must be WRITTEN even when the wanted value is empty:
        # compose's `${KEY:-}` would give the same empty string, but a key the
        # file does not carry reads as "never derived" to the next reader.
        if [ "$cur" != "$want" ] || ! grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$env_file"; then
            update_env_value "$env_file" "$key" "$want"
            case "$key" in
                *SECRET) printf '  %s: %s\n' "$key" "$([ -n "$want" ] && echo set || echo cleared)" ;;
                *)       printf '  %s=%s\n' "$key" "$want" ;;
            esac
        fi
    done
    return 0
}

# #2535 (#1486 class): `docker compose` resolves ${KEY:-d} / ${KEY:+x} from the
# PROCESS environment before the project .env. A script that exported the old
# .env at start (post-install's load_env) and then re-derives the Open WebUI
# keys would recreate openwebui with the STALE exported copy (auto-redirect
# still true, provider still set, with the provider gone: a sign-in page with
# no button and no form). Call this in the SHELL THAT RECREATES, after
# derive_owui_sso_env — never inside $(...), whose exports die with the subshell.
reexport_owui_sso_env() {
    local env_file="${1:-.env}" key val
    [ -f "$env_file" ] || return 0
    for key in OWUI_GOOGLE_CLIENT_ID OWUI_GOOGLE_CLIENT_SECRET OWUI_OAUTH_AUTO_REDIRECT OWUI_OIDC_ACTIVE; do
        val=$(read_env_value "$env_file" "$key" 2>/dev/null) || val=""
        export "$key=$val"
    done
    return 0
}

# ------------------------------------------------------------------------------
# Release channel / public-remote resolution (#27/#28)
# ------------------------------------------------------------------------------
# Single source of truth for the box-side public release remote. PUBLIC/customer
# boxes clone + upgrade from the Codeberg mirror; the value is used by
#   - cli/init.sh    detect_release_channel() — match `origin` → RAZZFAZZ_CHANNEL
#   - cli/upgrade.sh preflight_checks()        — redirect `origin` on public boxes
# Previously this literal was hardcoded + DUPLICATED in BOTH scripts (drift risk,
# and no way for a tester to point a box at a fork/staging Codeberg). It now lives
# here once, and is `.env`-overridable so a staging box can set e.g.
#   RAZZFAZZ_PUBLIC_REMOTE=https://codeberg.org/<fork>/rzfz-ai-service-stack.git
# NOTE: scripts/publish-public.sh has its own PUBLIC_REMOTE (the DEV *publish*
# target); that is the publish-side twin of this box-side constant — deliberately
# NOT coupled (a dev pushing a release and a box pulling one are different roles).
# Codeberg-exit (2026-07): the public channel moved to GitHub after Codeberg's ToU
# §7 banned LLM-heavy projects. Legacy Codeberg-origin boxes auto-repoint to this
# value on their next `rzfz upgrade` (cli/upgrade.sh preflight redirect).
readonly RAZZFAZZ_PUBLIC_REMOTE_DEFAULT="https://github.com/rzfz-ai/rzfz-ai-service-stack.git"

# Optional SECONDARY/fallback public remote, tried by cli/upgrade.sh only if a fetch
# from the primary fails. Empty by default (GitHub-primary, no secondary yet); set to
# a self-hosted code.rzfz.ai or a GitLab mirror when one exists. Same precedence as
# the primary (env > .env > default).
readonly RAZZFAZZ_PUBLIC_REMOTE_FALLBACK_DEFAULT=""

# razzfazz_public_remote: resolve the public release remote with the same
# precedence as razzfazz_channel(): explicit RAZZFAZZ_PUBLIC_REMOTE environment
# variable > RAZZFAZZ_PUBLIC_REMOTE in ${SCRIPT_DIR}/.env > canonical GitHub
# default. Never sources .env (grep via read_env_value). Prints the resolved URL.
razzfazz_public_remote() {
    local remote
    remote="${RAZZFAZZ_PUBLIC_REMOTE:-}"
    if [ -z "$remote" ]; then
        remote=$(read_env_value "${SCRIPT_DIR:-.}/.env" "RAZZFAZZ_PUBLIC_REMOTE" 2>/dev/null || echo "")
    fi
    [ -n "$remote" ] || remote="$RAZZFAZZ_PUBLIC_REMOTE_DEFAULT"
    printf '%s\n' "$remote"
}

# razzfazz_public_remote_fallback: resolve the optional secondary public remote
# (env RAZZFAZZ_PUBLIC_REMOTE_FALLBACK > .env > default). Prints "" when no
# fallback is configured (the common case today). Callers must no-op on empty.
razzfazz_public_remote_fallback() {
    local remote
    remote="${RAZZFAZZ_PUBLIC_REMOTE_FALLBACK:-}"
    if [ -z "$remote" ]; then
        remote=$(read_env_value "${SCRIPT_DIR:-.}/.env" "RAZZFAZZ_PUBLIC_REMOTE_FALLBACK" 2>/dev/null || echo "")
    fi
    [ -n "$remote" ] || remote="$RAZZFAZZ_PUBLIC_REMOTE_FALLBACK_DEFAULT"
    printf '%s\n' "$remote"
}

# razzfazz_origin_url: the origin URL AS CONFIGURED, with no insteadOf rewrite.
#
# #1738: `git remote get-url origin` applies `url.<base>.insteadOf`, so on a box
# that has such an alias it answers with the SUBSTITUTE, not with what stands in
# `.git/config`. Measured on an internal box whose ~/.gitconfig carries
#
#     url.http://gitea:3000/.insteadOf = https://git.razzfazz.ai/
#
#     git remote get-url origin            -> http://gitea:3000/…      (rewritten)
#     git config --get remote.origin.url   -> https://git.razzfazz.ai/… (raw)
#
# Three readers compare the origin against a canonical URL to answer WHICH REPO
# THIS BOX IS FROM — the upgrade pre-flight, the channel detection and the status
# report. All three must see the raw value; against the rewritten one the
# pre-flight aborts with "wrong origin" on a box whose origin is exactly right,
# and the fix it prints (`git remote set-url origin <that same URL>`) changes
# nothing, so the operator has no way out but --force.
#
# NOT for "which URL will git actually CONTACT" — that question wants the
# rewrite, and the two readers that ask it (the https-credential checks in
# cli/upgrade.sh and cli/init.sh) keep using `git remote get-url` on purpose.
#
# $1: the repository directory. Prints "" when there is no origin.
razzfazz_origin_url() {
    git -C "${1:-.}" config --get remote.origin.url 2>/dev/null || echo ""
}

# ------------------------------------------------------------------------------
# Secret generation
# ------------------------------------------------------------------------------
# All three use openssl rand. Lengths are interpreted differently per helper
# to match the historical razzfazz-init.sh behavior (we cannot change these
# semantics without breaking compatibility with existing seeded secrets):
#   - generate_secret <bytes>     → base64 of <bytes> random bytes, no newline
#   - generate_password <chars>   → exactly <chars> chars, alphanumeric subset
#   - generate_hex_secret <bytes> → hex of <bytes> random bytes (length = 2*<bytes>)
generate_secret() {
    local length=$1
    openssl rand -base64 "$length" | tr -d '\n'
}

# Admin-password policy: the strictest rule of every app the fleet password is
# set on (Dify: letters AND numbers, at least 8 characters). One lowercase
# letter, one uppercase letter and one digit are required; no app requires a
# symbol, so none is demanded. Prints the reason on stdout and returns 1 when
# the password does not comply.
RAZZFAZZ_PASSWORD_MIN_LENGTH=8
password_policy_violation() {
    local pw=$1
    if [ "${#pw}" -lt "$RAZZFAZZ_PASSWORD_MIN_LENGTH" ]; then
        echo "The password must be at least ${RAZZFAZZ_PASSWORD_MIN_LENGTH} characters long."
        return 1
    fi
    case "$pw" in *[[:lower:]]*) ;; *)
        echo "The password must contain at least one lowercase letter."; return 1 ;; esac
    case "$pw" in *[[:upper:]]*) ;; *)
        echo "The password must contain at least one uppercase letter."; return 1 ;; esac
    case "$pw" in *[[:digit:]]*) ;; *)
        echo "The password must contain at least one digit."; return 1 ;; esac
    return 0
}

# _rand_below <n>: sets _RAND to a uniform integer in [0, n) for n <= 256, from
# a buffer of CSPRNG bytes (openssl rand), refilled when used up. Rejection
# sampling keeps it free of modulo bias. Not for use in a subshell: the buffer
# and the result are shell globals.
_RAND_BUF="" _RAND_POS=0
_rand_below() {
    local n=$1 limit byte
    limit=$(( 256 - 256 % n ))
    while :; do
        if [ "$_RAND_POS" -ge "${#_RAND_BUF}" ]; then
            _RAND_BUF="$(openssl rand -hex 256)" || return 1
            _RAND_POS=0
        fi
        byte=$(( 16#${_RAND_BUF:_RAND_POS:2} ))
        _RAND_POS=$(( _RAND_POS + 2 ))
        if [ "$byte" -lt "$limit" ]; then _RAND=$(( byte % n )); return 0; fi
    done
}

# generate_password <chars>: alphanumeric (no shell-special characters). One
# lowercase letter, one uppercase letter and one digit are placed by
# construction (not by retrying); the other characters come from the full
# alphabet; then the whole string is Fisher-Yates shuffled so the guaranteed
# characters sit at random positions.
generate_password() {
    local length=$1
    [ "$length" -ge 3 ] 2>/dev/null || { echo "generate_password: length must be at least 3" >&2; return 1; }
    local lower=abcdefghijklmnopqrstuvwxyz upper=ABCDEFGHIJKLMNOPQRSTUVWXYZ digit=0123456789
    local all="${lower}${upper}${digit}"
    local -a c=()
    local i j t
    _rand_below 26 || return 1; c+=("${lower:_RAND:1}")
    _rand_below 26 || return 1; c+=("${upper:_RAND:1}")
    _rand_below 10 || return 1; c+=("${digit:_RAND:1}")
    for ((i = 3; i < length; i++)); do
        _rand_below 62 || return 1; c+=("${all:_RAND:1}")
    done
    for ((i = length - 1; i > 0; i--)); do
        _rand_below $((i + 1)) || return 1; j=$_RAND
        t=${c[i]}; c[i]=${c[j]}; c[j]=$t
    done
    local out=""
    for ((i = 0; i < length; i++)); do out+="${c[i]}"; done
    printf '%s' "$out"
}

generate_hex_secret() {
    local length=$1
    openssl rand -hex "$length"
}

# ------------------------------------------------------------------------------
# Container check
# ------------------------------------------------------------------------------
# check_container <name>: returns 0 if a container named <name> is currently
# running, non-zero otherwise. Callers that want the legacy "die if missing"
# behavior should test the return code and `exit 1` themselves — the library
# function is non-fatal so it's safe to use in conditionals.
check_container() {
    local name=$1
    [[ -n "$name" ]] || return 2
    docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${name}$"
}

# ------------------------------------------------------------------------------
# Docker compose helpers
# ------------------------------------------------------------------------------
# docker_compose_cmd: print the right invocation for docker compose on this
# host — `docker compose` (v2 plugin, preferred) or `docker-compose` (v1
# legacy binary). Returns non-zero if neither is available.
#
# Usage:
#   dc=$(docker_compose_cmd) || { print_error "docker compose missing"; exit 1; }
#   $dc up -d
docker_compose_cmd() {
    if docker compose version >/dev/null 2>&1; then
        printf 'docker compose\n'
        return 0
    fi
    if command -v docker-compose >/dev/null 2>&1; then
        printf 'docker-compose\n'
        return 0
    fi
    return 1
}

# ------------------------------------------------------------------------------
# Compose / Engine feature floor for `volume: { subpath: … }`  (#855 rev-C)
# ------------------------------------------------------------------------------
# The top-level `include:` in compose.yml is UNCONDITIONAL — every module file is
# parsed on every box, whether or not its profile is selected. modules/apps/wazuh
# mounts a subdirectory of a named volume (`volumes: … volume: { subpath: … }`),
# and Compose only learned that key in 2.26.0; the Engine needs API 1.45 to
# perform the mount.
#
# Older Compose does not warn and does not skip: it REJECTS the whole tree at
# schema-validation time with "Additional property subpath is not allowed",
# which happens BEFORE profile filtering. Measured on stock Ubuntu 24.04
# (`docker-compose-v2` = 2.24.6): `docker compose config` exits 15 with the
# wazuh profile DISABLED. From then on no `docker compose` command works at all
# — the same brick class as the parse-time `${VAR:?}` guards that review
# blocker 1 removed, only keyed on the tool version instead of on .env.
#
# Hence the floor is asserted in cli/init.sh's prerequisites and in
# cli/upgrade.sh's pre-flight, i.e. BEFORE the upgrade switches the code and
# strands a box mid-flight on a tree its Compose cannot parse.
RAZZFAZZ_MIN_COMPOSE_VERSION="2.26.0"
RAZZFAZZ_MIN_ENGINE_API="1.45"

# razzfazz_version_ge <have> <want>: dotted-numeric ">=" comparison.
# FAIL-CLOSED: empty, non-numeric or otherwise unparseable input returns 1
# ("too old"), because a version we cannot measure is not a version we may
# vouch for. A leading `v` and any `-suffix`/`+build` tail are tolerated
# (Docker Desktop reports e.g. `v2.29.1-desktop.1`).
razzfazz_version_ge() {
    local have=${1:-} want=${2:-}
    have=${have#v}; want=${want#v}
    have=${have%%[-+ ]*}; want=${want%%[-+ ]*}
    [[ $have =~ ^[0-9]+(\.[0-9]+)*$ ]] || return 1
    [[ $want =~ ^[0-9]+(\.[0-9]+)*$ ]] || return 1
    local -a h w
    IFS=. read -r -a h <<< "$have"
    IFS=. read -r -a w <<< "$want"
    local i n=${#h[@]}
    (( ${#w[@]} > n )) && n=${#w[@]}
    for (( i = 0; i < n; i++ )); do
        local hv=${h[i]:-0} wv=${w[i]:-0}
        (( 10#$hv > 10#$wv )) && return 0
        (( 10#$hv < 10#$wv )) && return 1
    done
    return 0
}

# razzfazz_compose_version: print the Compose v2 version (no leading `v`).
# Prints nothing and returns 1 when Compose v2 is absent or unreadable.
razzfazz_compose_version() {
    local v
    v=$(docker compose version --short 2>/dev/null) || return 1
    v=${v#v}
    [ -n "$v" ] || return 1
    printf '%s\n' "$v"
}

# razzfazz_engine_api_version: print the Docker Engine's API version.
# Needs a reachable daemon; prints nothing and returns 1 otherwise.
razzfazz_engine_api_version() {
    local v
    v=$(docker version --format '{{.Server.APIVersion}}' 2>/dev/null) || return 1
    [ -n "$v" ] || return 1
    printf '%s\n' "$v"
}

# razzfazz_check_compose_floor: assert the feature floor above.
# Returns 0 when both versions clear it; otherwise prints an actionable
# diagnosis (what is too old, WHY it matters, HOW to fix it) and returns 1.
# The CALLER owns the strictness — cli/init.sh folds it into missing_deps,
# cli/upgrade.sh aborts the run — so this never exits by itself.
razzfazz_check_compose_floor() {
    local cv ev failed=0
    cv=$(razzfazz_compose_version) || cv=""
    ev=$(razzfazz_engine_api_version) || ev=""

    if [ -z "$cv" ]; then
        print_error "Could not read the Docker Compose version ('docker compose version --short')."
        failed=1
    elif ! razzfazz_version_ge "$cv" "$RAZZFAZZ_MIN_COMPOSE_VERSION"; then
        print_error "Docker Compose $cv is too old — this stack needs >= ${RAZZFAZZ_MIN_COMPOSE_VERSION}."
        failed=1
    fi

    if [ -z "$ev" ]; then
        print_error "Could not read the Docker Engine API version — is the Docker daemon running?"
        failed=1
    elif ! razzfazz_version_ge "$ev" "$RAZZFAZZ_MIN_ENGINE_API"; then
        print_error "Docker Engine API $ev is too old — this stack needs >= ${RAZZFAZZ_MIN_ENGINE_API}."
        failed=1
    fi

    [ "$failed" -eq 0 ] && return 0

    print_info "WHY: compose.yml includes every module file unconditionally, and one of"
    print_info "     them mounts a volume SUBDIRECTORY ('volume: { subpath: ... }')."
    print_info "     Compose added 'subpath' in ${RAZZFAZZ_MIN_COMPOSE_VERSION} and the Engine mounts it from"
    print_info "     API ${RAZZFAZZ_MIN_ENGINE_API}. An older Compose rejects the whole file with"
    print_info "     \"Additional property subpath is not allowed\" at parse time — BEFORE"
    print_info "     profiles are applied — so EVERY 'docker compose' command on this box"
    print_info "     fails, no matter which modules are enabled."
    echo ""
    print_info "FIX: install Docker's own compose plugin (the distro package is too old —"
    print_info "     Ubuntu 24.04 ships 2.24.6, which is below the floor):"
    print_info "       curl -fsSL https://get.docker.com | sh"
    print_info "     or, with Docker's apt repo already configured:"
    print_info "       sudo apt-get update && sudo apt-get install -y docker-compose-plugin docker-ce"
    print_info "     Verify: docker compose version --short   # >= ${RAZZFAZZ_MIN_COMPOSE_VERSION}"
    print_info "             docker version --format '{{.Server.APIVersion}}'   # >= ${RAZZFAZZ_MIN_ENGINE_API}"
    return 1
}

# require_docker_compose: assert that docker compose (v1 or v2) is available
# AND the docker daemon is reachable. Calls print_error + exit 1 on failure.
# Returns 0 silently when everything is fine.
require_docker_compose() {
    if ! command -v docker >/dev/null 2>&1; then
        print_error "docker is not installed."
        exit 1
    fi
    if ! docker_compose_cmd >/dev/null; then
        print_error "Neither 'docker compose' (v2) nor 'docker-compose' (v1) is available."
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        print_error "Docker daemon is not running or current user lacks permissions."
        print_warning "Try: sudo systemctl start docker && sudo usermod -aG docker \$USER"
        exit 1
    fi
}

# ------------------------------------------------------------------------------
# Prerequisite checks
# ------------------------------------------------------------------------------
# require_command <cmd> [<cmd> ...]: assert each command is on PATH; prints
# error and exits 1 listing every missing one.
require_command() {
    local missing=()
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if (( ${#missing[@]} > 0 )); then
        print_error "Missing required command(s): ${missing[*]}"
        exit 1
    fi
}

# check_disk_space [<path>] [<min_gb>]: warn (don't exit) if free space at
# <path> (default `.`) is below <min_gb> (default 20). Prints the measured
# value as a substep regardless.
check_disk_space() {
    local path=${1:-.}
    local min_gb=${2:-20}
    local free_gb
    # M026 #144: force C locale so df's column layout + numeric format are
    # predictable. `df` itself isn't currently column-translated on the row
    # data, but the header is, and `df` could shift to header-relative
    # column lookup in a future coreutils release. Cheap insurance.
    free_gb=$(LC_ALL=C df -BG "$path" 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G')
    free_gb=${free_gb:-0}
    print_substep "Available disk space at ${path}: ${free_gb}GB"
    if (( free_gb < min_gb )); then
        print_warning "Low disk space at ${path}: ${free_gb}GB (recommended: ${min_gb}GB+)."
        return 1
    fi
    return 0
}

# check_ram [<min_gb>]: warn (don't exit) if total system RAM is below
# <min_gb> (default 8). Uses `-m` and divides to avoid `free -g` rounding to 0
# on small VMs.
check_ram() {
    local min_gb=${1:-8}
    local total_gb
    # M026 #144 (real fix): force C locale on `free`. Under
    # LANG=de_DE.UTF-8 (and most other non-English locales), `free -m`
    # translates the row label `Mem:` → `Speicher:`, so `awk /^Mem:/`
    # never matches and total_gb ends up empty → defaulted to 0 →
    # spurious "Low RAM: 0GB" warning on every razzfazz-init.sh run.
    # Surfaced on the German-locale dev box during M026 S01.
    total_gb=$(LC_ALL=C free -m 2>/dev/null | awk '/^Mem:/{printf "%.0f", $2/1024}')
    total_gb=${total_gb:-0}
    print_substep "Total RAM: ${total_gb}GB"
    if (( total_gb < min_gb )); then
        print_warning "Low RAM: ${total_gb}GB (recommended: ${min_gb}GB+)."
        return 1
    fi
    return 0
}

# stop_orphan_gpustack_runners: stop+remove GPUStack-spawned runner containers
# that survive `docker compose down`. GPUStack v2.x spawns model-runner pods
# via the docker socket — they're NOT part of the compose project, so compose
# down (even with --remove-orphans) leaves them running and they continue to
# pin VRAM + RAM. Tracked as rc6.7 #48.
#
# Identification: containers whose image matches one of the runner images
# (llama-{vulkan,rocm,cpu}-runner:*) OR the pause image (gpustack/runtime:pause)
# AND that lack the `com.docker.compose.project=razzfazz-stack` label.
# The label filter ensures we never touch the gpustack server itself.
#
# Idempotent: silent no-op when no orphans are present.
# #2226: the LLM Manager's per-node engines are launched by llm-worker-agent
# through the docker socket, OUTSIDE the compose project (labels
# rzfz.role=llm-engine + razzfazz.managed=true, the models volume mounted, the
# project network attached). `docker compose stop|down` never sees them, and
# stop_orphan_gpustack_runners deliberately skips them (#331). After
# "Stack stopped." they kept running — RAM/VRAM pinned, models answering with
# the manager gone — and `compose down -v` failed with "volume is in use".
# Stop the SUPERVISOR first, then the engines: a stopped supervisor cannot
# relaunch anything, which is what makes the result stable — not the length
# of any window. The re-verify after a short pause is a cheap survivor check
# (an engine that ignored the stop, a second supervisor); measured on 0.91
# (rc12), a healthy supervisor did not relaunch a manually stopped engine
# within 240 s, so no backoff figure is claimed here.
razzfazz_stop_supervised_engines() {   # [--remove]   returns 0 when none are left
    local mode="stop" ids n
    [ "${1:-}" = "--remove" ] && mode="remove"
    docker compose stop llm-worker-agent >/dev/null 2>&1 || true
    ids="$(docker ps -aq --filter label=rzfz.role=llm-engine --filter label=razzfazz.managed=true 2>/dev/null || true)"
    if [ -z "$ids" ]; then
        echo "  supervised LLM engines: none running (#2226)"
        return 0
    fi
    n=$(printf '%s\n' "$ids" | grep -c . || true)
    if [ "$mode" = "remove" ]; then
        echo "  removing ${n} supervised LLM engine container(s) (#2226)..."
        # shellcheck disable=SC2086
        docker rm -f $ids >/dev/null 2>&1 || true
    else
        echo "  stopping ${n} supervised LLM engine container(s) (#2226)..."
        # shellcheck disable=SC2086
        docker stop $ids >/dev/null 2>&1 || true
    fi
    # survivor check after a short pause (RAZZFAZZ_ENGINE_REVERIFY_SECONDS,
    # default 35 s — a bound on how long stop waits, not a measured backoff)
    sleep "${RAZZFAZZ_ENGINE_REVERIFY_SECONDS:-35}"
    ids="$(docker ps -q --filter label=rzfz.role=llm-engine --filter label=razzfazz.managed=true 2>/dev/null || true)"
    if [ -n "$ids" ]; then
        echo "  WARNING: $(printf '%s\n' "$ids" | grep -c .) supervised LLM engine(s) still running after the re-verify window (#2226):" >&2
        docker ps --filter label=rzfz.role=llm-engine --format '    {{.Names}}  {{.Status}}' >&2 || true
        return 1
    fi
    echo "  supervised LLM engines: 0 running after re-verify (#2226)"
    return 0
}

stop_orphan_gpustack_runners() {
    local orphans
    # #331 NEVER reap our own llm-engine containers. They are launched by the
    # worker-agent through docker-py, not compose, so they carry NO
    # `com.docker.compose.project` label — which is exactly what this filter
    # treats as "orphan". Before #331 that was latent: the AMD driver launched
    # `razzfazz-llama-vulkan-runner:latest`, a name nothing built, so no engine
    # ever matched `ancestor=llama-vulkan-runner`. Now that the driver launches
    # the image that IS built, `rzfz init` and `rzfz upgrade` would `docker rm -f`
    # every running engine. They are identified by their own `rzfz.role` label.
    #
    # `|`-delimited, NOT space-delimited: awk collapses runs of whitespace, so an
    # empty label shifts every later field left and the role test would read the
    # wrong column. That is invisible until a label happens to be empty — which,
    # for the compose-project label on these very containers, is always.
    # #1516 (E5): the runner repositories collapsed into ONE (`llama-runner`,
    # target in the tag). The per-accelerator names that boxes ACTUALLY ran
    # containers from stay listed for a cycle; the two CUDA ones never do,
    # because nothing in the stack built them before #1516 — and `ancestor=`
    # resolves a reference to an image ID, so the `llama-runner` filter covers
    # those images under any alias anyway.
    orphans=$(docker ps -a \
        --filter 'ancestor=llama-runner' \
        --filter 'ancestor=llama-vulkan-runner' \
        --filter 'ancestor=llama-rocm-runner' \
        --filter 'ancestor=llama-cpu-runner' \
        --filter 'ancestor=gpustack/runtime:pause' \
        --format '{{.ID}}|{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "rzfz.role"}}' 2>/dev/null \
        | awk -F'|' '$3 != "razzfazz-stack" && $3 != "razzfazz_stack" && $4 != "llm-engine" {print $1, $2}')
    if [ -z "$orphans" ]; then
        return 0
    fi
    print_substep "Stopping orphan GPUStack runners (rc6.7 #48):"
    while IFS= read -r line; do
        local cid name
        cid=$(echo "$line" | awk '{print $1}')
        name=$(echo "$line" | awk '{print $2}')
        print_substep "  rm -f $name ($cid)"
        docker rm -f "$cid" >/dev/null 2>&1 || true
    done <<< "$orphans"
}

# ------------------------------------------------------------------------------
# Network mode — online | proxied | offline   (#184, unifies #181 + #184)
# ------------------------------------------------------------------------------
# RAZZFAZZ_NETWORK_MODE is the single front-door knob for the box's egress axis.
# It SUPERSEDES the two legacy booleans, which become its internal implementation:
#   online   (default): direct egress, today's behavior. No overlay.
#   proxied  (= #181)  : egress only through the corporate proxy (+ trust its CA);
#                        drives RAZZFAZZ_CORPORATE_PROXY=1 + compose.corporate-proxy.yml.
#   offline  (= #184)  : NO internet egress (LAN stays up); drives RAZZFAZZ_OFFLINE=1
#                        + compose.offline.yml + the verify-images/egress gates.
# The three are mutually exclusive on the egress axis, so an enum is the right shape.
#
# BACK-COMPAT: when RAZZFAZZ_NETWORK_MODE is unset/blank/invalid we DERIVE it from
# the legacy booleans (RAZZFAZZ_OFFLINE, then RAZZFAZZ_CORPORATE_PROXY), so an
# existing #181 box (RAZZFAZZ_CORPORATE_PROXY=1) keeps behaving as `proxied` with
# no .env edit. ensure_network_mode_overlay() then persists the derived value.
readonly RAZZFAZZ_NETWORK_MODE_DEFAULT="online"
readonly RAZZFAZZ_OFFLINE_OVERLAY="compose.offline.yml"
readonly RAZZFAZZ_PROXY_OVERLAY="compose.corporate-proxy.yml"
# #184 WS2a — the UNIVERSAL (all-modes) no-runtime-build overlay. COMMITTED (not
# per-box generated like the offline overlay): it is deterministic (the project
# name is pinned) and is the correctness/security guarantee, so it must not hinge
# on a runtime generation step that could silently fail. `build: !reset null` on
# every custom-build service, so `up`/enable/disable can never build (pull_policy
# alone does NOT stop a build of a MISSING image). Composed in ONLINE, PROXIED and
# OFFLINE alike — the no-runtime-build rule is universal; the network mode only
# governs how install/upgrade OBTAINS content.
readonly RAZZFAZZ_NOBUILD_OVERLAY="compose.no-build.yml"

# #184 P2 — the OPT-IN air-gap registry-mirror overlay. Present only when
# RAZZFAZZ_REGISTRY_MIRROR is set. Rewrites each PULLED service's `image:` to the
# mirror-prefixed ref so install/upgrade `docker compose pull` fetches from a
# LOCAL pull-through registry instead of the upstream one. ORTHOGONAL to the
# online/proxied/offline mode (a box can be online+mirror or offline+mirror), so
# it is reconciled ALONGSIDE the mode overlays, not inside the mode switch.
# Box-local (per-box generated, like compose.offline.yml) — never committed.
readonly RAZZFAZZ_REGISTRY_MIRROR_OVERLAY="compose.registry-mirror.yml"

# #2526 — the OPT-IN UDP 514 port of the Wazuh syslog listener. COMMITTED (a
# pure COMPOSE_FILE-wiring step, nothing generated). Composed only when
# WAZUH_SYSLOG_UDP=true, so a host rsyslog on UDP 514 is never displaced.
readonly RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY="modules/apps/wazuh/compose.syslog-udp.yml"

# #184 P1 / WS7b — the in-gpustack-volume directory that holds OFFLINE-sideloaded
# GGUFs. The `gpustack-data` volume mounts at /var/lib/gpustack in the gpustack
# container(s), so this path is INSIDE the container. Offline model registration
# uses `source: local_path` pointing HERE instead of `source: huggingface`, so it
# never touches huggingface.co. The offline `--package` upgrade (cli/upgrade.sh)
# loads the bundled models/*.gguf into this dir before provisioning, and
# `rzfz verify-models` checks presence here (or the HF cache). Mirrored in
# core/llm/model_source.py::LOCAL_MODELS_DIR and core/llm/expected_models.py —
# keep the three in lockstep.
readonly RAZZFAZZ_LOCAL_MODELS_DIR="/var/lib/gpustack/local-models"

# #2227: the package's model weights go into the volume of the runtime THIS box
# runs, not into a container it does not have. Both offline loaders (the
# upgrade's WS7a block and post-install's stage_baked_appliance_models) used
# `docker cp` into a running `gpustack` container; on a 2026.09 LLM-Manager box
# that container does not exist, the loaders printed "deferring" and nothing
# deferred it — journey D on rc9 measured 5 GGUFs in the package, 0 in the
# manager volume, after an upgrade that exited 0.
#
# Runtime detection is by RUNNING CONTAINER, the same fact the old gate used:
#   gpustack           -> the legacy volume at $RAZZFAZZ_LOCAL_MODELS_DIR (unchanged)
#   llm-worker-agent   -> the node agent's models mount (razzfazz-stack_llm-node-models
#                         at /models, modules/llm/node-agent/compose.yml); the
#                         package's weight files are copied FLAT by basename (the
#                         layout the node agent produces), and the node agent's
#                         ensure_file()/missing_files() judge presence by basename
#                         at the root — a staged weight is "cached", never
#                         re-downloaded.
# The manager volume is FLAT (#1544: the node addresses weights by bare
# basename; missing_files() looks only at the root of /models), while the
# packager writes a vision sidecar under its repository path ALWAYS
# (models/<repo>/mmproj-…gguf) — so the manager arm copies every weight file
# to /models/<basename>, never the tree. Ownership is left as it arrives: the
# node agent itself writes weights as root:root mode 0600 and the engines run
# as root (measured on 0.91, journey A's box), so a package file at 1001:1001
# 0600 is readable by exactly the readers that exist; a chmod would make staged
# weights more permissive than native ones for no reader.
# No helper image is used on either path — this must work on an air-gapped box.
readonly RAZZFAZZ_MANAGER_MODELS_DIR="/models"
readonly RAZZFAZZ_MANAGER_MODELS_CONTAINER="llm-worker-agent"

# Prints "gpustack", "manager" or "" — which runtime's models volume is reachable now.
razzfazz_models_runtime() {
    local names
    names=$(docker ps --format '{{.Names}}' 2>/dev/null) || names=""
    if printf '%s\n' "$names" | grep -qx gpustack; then echo gpustack
    elif printf '%s\n' "$names" | grep -qx "$RAZZFAZZ_MANAGER_MODELS_CONTAINER"; then echo manager
    else echo ""; fi
}

# 0 when the reachable runtime's models directory already holds something.
razzfazz_models_volume_populated() {
    case "$(razzfazz_models_runtime)" in
        gpustack) docker exec gpustack sh -c "ls -A '$RAZZFAZZ_LOCAL_MODELS_DIR' 2>/dev/null | head -n1 | grep -q ." 2>/dev/null ;;
        manager)  docker exec "$RAZZFAZZ_MANAGER_MODELS_CONTAINER" sh -c "ls -A '$RAZZFAZZ_MANAGER_MODELS_DIR' 2>/dev/null | grep -q '\.gguf\$'" 2>/dev/null ;;
        *) return 1 ;;
    esac
}

# razzfazz_models_volume_missing NAMES — NAMES is a newline-separated list of
# GGUF basenames (a package's models/ subtree); prints the ones the reachable
# runtime's models volume does NOT hold, by basename (the flat manager volume
# keys files by basename; gpustack's repo-scoped tree is searched by name).
# #2440: idempotency of model staging is per FILE against the package — never
# "the volume holds something, so we already staged" (0.175: a volume holding
# an older package's six files kept a newer package's seven from ever landing,
# and the chat model failed to deploy offline).
# MODELS_VOLUME_MISSING-BEGIN
razzfazz_models_volume_missing() {
    local names="$1" have="" n
    case "$(razzfazz_models_runtime)" in
        gpustack) have=$(docker exec gpustack sh -c "find '$RAZZFAZZ_LOCAL_MODELS_DIR' -type f -name '*.gguf' 2>/dev/null" 2>/dev/null | sed 's#.*/##') ;;
        manager)  have=$(docker exec "$RAZZFAZZ_MANAGER_MODELS_CONTAINER" sh -c "find '$RAZZFAZZ_MANAGER_MODELS_DIR' -maxdepth 1 -type f -name '*.gguf' 2>/dev/null" 2>/dev/null | sed 's#.*/##') ;;
        *) printf '%s\n' "$names"; return 0 ;;
    esac
    while IFS= read -r n; do
        [ -n "$n" ] || continue
        printf '%s\n' "$have" | grep -qxF "$n" || printf '%s\n' "$n"
    done <<< "$names"
    return 0
}
# MODELS_VOLUME_MISSING-END

# #2280: the node agent's staging marker (see razzfazz_stage_package_models).
_RZFZ_STAGING_HB=""
_razzfazz_staging_mark() {     # basenames on stdin
    docker exec -i "$RAZZFAZZ_MANAGER_MODELS_CONTAINER" sh -c "cat > '${RAZZFAZZ_MANAGER_MODELS_DIR}/.rzfz-staging'" >/dev/null 2>&1 || true
}
_razzfazz_staging_touch() {
    docker exec "$RAZZFAZZ_MANAGER_MODELS_CONTAINER" touch "${RAZZFAZZ_MANAGER_MODELS_DIR}/.rzfz-staging" >/dev/null 2>&1 || true
}
_razzfazz_staging_heartbeat_start() {
    _razzfazz_staging_touch
    # Bound to the shell that started it: `$$` in the subshell is that shell's
    # PID. A SIGKILLed upgrade runs no trap, so without this check the loop
    # would keep the marker fresh forever and the agent would wait for files
    # that never land.
    ( parent=$$
      while sleep "${RAZZFAZZ_STAGING_HEARTBEAT_S:-60}" && kill -0 "$parent" 2>/dev/null; do
          _razzfazz_staging_touch
      done ) >/dev/null 2>&1 &
    _RZFZ_STAGING_HB=$!
}
_razzfazz_staging_heartbeat_stop() {
    [ -n "${_RZFZ_STAGING_HB:-}" ] || return 0
    kill "$_RZFZ_STAGING_HB" 2>/dev/null || true
    wait "$_RZFZ_STAGING_HB" 2>/dev/null || true
    _RZFZ_STAGING_HB=""
}
_RZFZ_STAGING_CP=""
_razzfazz_staging_cp() {       # docker cp, then the heartbeat stops
    # In the background and waited for: bash runs an INT/TERM trap only after a
    # FOREGROUND command returns, which for a 40 GB copy is minutes later;
    # `wait` returns at once, so the trap ends the copy and removes the mark.
    local rc=0
    docker cp "$1" "$2" >/dev/null 2>&1 &
    _RZFZ_STAGING_CP=$!
    wait "$_RZFZ_STAGING_CP" || rc=$?
    _RZFZ_STAGING_CP=""
    _razzfazz_staging_heartbeat_stop
    return "$rc"
}
_razzfazz_staging_unmark() {
    if [ -n "${_RZFZ_STAGING_CP:-}" ]; then
        kill "$_RZFZ_STAGING_CP" 2>/dev/null || true
        wait "$_RZFZ_STAGING_CP" 2>/dev/null || true
        _RZFZ_STAGING_CP=""
    fi
    _razzfazz_staging_heartbeat_stop
    docker exec "$RAZZFAZZ_MANAGER_MODELS_CONTAINER" rm -f "${RAZZFAZZ_MANAGER_MODELS_DIR}/.rzfz-staging" >/dev/null 2>&1 || true
}

# razzfazz_stage_package_models SRC_DIR — copy SRC_DIR/. (a package's models/
# subtree) into the reachable runtime's models volume. Returns 0 on success,
# 1 when nothing could be staged; every outcome is printed, none is silent.
razzfazz_stage_package_models() {
    local src="$1" rt
    rt=$(razzfazz_models_runtime)
    case "$rt" in
        gpustack)
            print_substep "Loading bundled model GGUFs into the gpustack-data volume (${RAZZFAZZ_LOCAL_MODELS_DIR})..."
            # #2280: per file, under a hidden .part name renamed into place, so a
            # copy cut short (a full disk) never sits under the weight's own name
            # and passes as present. The repo-scoped tree is kept (projectors).
            local f rel rdir base okall=1 ncp=0
            while IFS= read -r f; do
                rel="${f#"$src"/}"; base=$(basename "$rel"); rdir=$(dirname "$rel")
                [ "$rdir" = "." ] && rdir="" || rdir="/${rdir}"
                if docker exec gpustack mkdir -p "${RAZZFAZZ_LOCAL_MODELS_DIR}${rdir}" >/dev/null 2>&1 \
                   && docker cp "$f" "gpustack:${RAZZFAZZ_LOCAL_MODELS_DIR}${rdir}/.${base}.part" >/dev/null 2>&1 \
                   && docker exec gpustack mv -f "${RAZZFAZZ_LOCAL_MODELS_DIR}${rdir}/.${base}.part" "${RAZZFAZZ_LOCAL_MODELS_DIR}${rdir}/${base}" >/dev/null 2>&1; then
                    ncp=$((ncp + 1))
                else
                    okall=0
                    docker exec gpustack rm -f "${RAZZFAZZ_LOCAL_MODELS_DIR}${rdir}/.${base}.part" >/dev/null 2>&1 || true
                    print_warning "  could not copy ${rel} into the gpustack-data volume."
                fi
            done < <(find "$src" -type f -name '*.gguf' | sort)
            if [ "$okall" -eq 1 ] && [ "$ncp" -gt 0 ]; then
                print_substep "Model GGUFs loaded into ${RAZZFAZZ_LOCAL_MODELS_DIR}/ — offline deploy will register them via source=local_path."
                return 0
            fi
            print_warning "Could not copy bundled model GGUFs into the gpustack-data volume — offline model deploy may report missing GGUFs. Re-check with 'rzfz verify-models'."
            return 1 ;;
        manager)
            print_substep "Loading bundled model GGUFs into the LLM Manager's models volume (${RAZZFAZZ_MANAGER_MODELS_CONTAINER}:${RAZZFAZZ_MANAGER_MODELS_DIR}, flat by name)... (#2227)"
            local f base n=0 failed=0 seen=""
            # #2280: tell the node agent which files this copy brings in. A load
            # of one of them that arrives meanwhile (the reconciler re-placing a
            # deployment after the restart) waits instead of failing with
            # "weights not present" minutes before the file lands; any other
            # load is unaffected. The marker is refreshed before each file and
            # every RAZZFAZZ_STAGING_HEARTBEAT_S during one, so the agent can
            # tell a long copy from a dead one, and removed on success, on
            # failure and on INT/TERM (modules/llm/node-agent STAGING_MARKER).
            local _prev_int _prev_term
            _prev_int=$(trap -p INT); _prev_term=$(trap -p TERM)
            trap '_razzfazz_staging_unmark; exit 130' INT
            trap '_razzfazz_staging_unmark; exit 143' TERM
            find "$src" -type f -name '*.gguf' -exec basename {} \; | sort -u | _razzfazz_staging_mark
            while IFS= read -r f; do
                base=$(basename "$f")
                case " $seen " in *" $base "*)
                    print_warning "  ${base}: a second file with this name is in the package (${f#"$src"/}) — the flat manager volume holds one; keeping the first."
                    continue ;;
                esac
                seen="$seen $base"
                # #2280: copied under a hidden .part name and renamed into place —
                # a copy cut short never sits under the weight's name (the node
                # agent and the presence checks key on the bare *.gguf name).
                _razzfazz_staging_heartbeat_start
                if _razzfazz_staging_cp "$f" "${RAZZFAZZ_MANAGER_MODELS_CONTAINER}:${RAZZFAZZ_MANAGER_MODELS_DIR}/.${base}.part" \
                   && docker exec "$RAZZFAZZ_MANAGER_MODELS_CONTAINER" mv -f "${RAZZFAZZ_MANAGER_MODELS_DIR}/.${base}.part" "${RAZZFAZZ_MANAGER_MODELS_DIR}/${base}" >/dev/null 2>&1; then
                    n=$((n + 1))
                else
                    failed=$((failed + 1))
                    docker exec "$RAZZFAZZ_MANAGER_MODELS_CONTAINER" rm -f "${RAZZFAZZ_MANAGER_MODELS_DIR}/.${base}.part" >/dev/null 2>&1 || true
                    print_warning "  could not copy ${f#"$src"/} into the manager volume."
                fi
            done < <(find "$src" -type f -name '*.gguf' | sort)
            _razzfazz_staging_unmark
            eval "${_prev_int:-trap - INT}"; eval "${_prev_term:-trap - TERM}"
            if [ "$n" -gt 0 ] && [ "$failed" -eq 0 ]; then
                print_substep "Model GGUFs loaded into the manager volume (${n} file(s), flat) — the node agent finds them by name and downloads nothing (#2227)."
                return 0
            fi
            print_warning "Could not copy the bundled model GGUFs into the LLM Manager's models volume (${n} copied, ${failed} failed) — the offline deploy will try to download what is missing. Re-check with 'rzfz verify-models'."
            return 1 ;;
        *)
            print_warning "Neither gpustack nor ${RAZZFAZZ_MANAGER_MODELS_CONTAINER} is running — the bundled model GGUFs were NOT staged (#2227). Start the stack and re-apply the package, or run 'rzfz post-install --refresh' with APPLIANCE_OFFLINE_PKG set."
            return 1 ;;
    esac
}

# ── #2280: a per-class model bundle onto this box ────────────────────────────
# A model bundle (`an offline package (Enterprise) <tag> --model-bundle <class>`) carries one
# hardware class's default model set and its records, nothing else: models/,
# expected-models.json (per-file catalogue sha256 pins and sizes), PACKAGE_INFO,
# PAYLOAD_PROVENANCE, MANIFEST.sha256 — records first in the archive. Two
# consumers apply it: the offline upgrade (`rzfz upgrade --package <base>
# --model-bundle <bundle>`, verified and extracted BEFORE the upgrade changes
# anything) and the appliance's post-install
# (/opt/razzfazz-appliance/razzfazz-models.tar.gz). Without it an offline box
# has no weights: the manager deploys nothing and Dify gets no model provider.
#
# All reading of the archive is scripts/model_bundle.py: ONE streaming pass that
# hashes the archive, admits only the records and regular GGUFs under models/,
# checks each GGUF's size before and its pin while reading, and writes only the
# files this box lacks. It lives in scripts/ so the 2026.09 crossing pre-step
# (`tar -xzf <base> ./cli ./scripts ./rzfz`) carries it.

# razzfazz_box_hardware_class [hardware] — the box's class by the manager's rule
# (hardware_catalog.effective_family); HARDWARE from .env when not given.
# Every python3 on this path runs with -B: a check that refuses must leave the
# stack tree exactly as it was, and an import from core/llm would otherwise
# drop a __pycache__ into it (found by the union gate, which writes bytecode).
razzfazz_box_hardware_class() {
    local hardware="${1-$(read_env_value "${SCRIPT_DIR:-.}/.env" HARDWARE 2>/dev/null || true)}"
    python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import hardware_catalog as h; print(h.effective_family(sys.argv[2]))' \
        "${SCRIPT_DIR:-.}/core/llm" "$hardware" 2>/dev/null || true
}

# razzfazz_model_bundle_check <bundle> <hardware> — may this bundle be applied
# to a box whose .env says HARDWARE=<hardware>? 0 yes; 2 no, the reason on
# stderr. Reads PACKAGE_INFO only (the leading records), never the weights —
# through scripts/model_bundle.py, which bounds the size of every tar header
# it reads (a hostile PAX header is refused before it is read).
razzfazz_model_bundle_check() {
    local bundle="$1" hardware="$2" info kind cls fam
    if [ ! -f "$bundle" ] || [ ! -r "$bundle" ]; then
        echo "#2280: model bundle not found or unreadable: ${bundle:-<none>}" >&2
        return 2
    fi
    info="$(python3 -B "${SCRIPT_DIR:-.}/scripts/model_bundle.py" --bundle "$bundle" --print-info)" || info=""
    kind="$(printf '%s\n' "$info" | sed -n 's/^package_kind=//p' | head -n1)"
    cls="$(printf '%s\n' "$info" | sed -n 's/^hardware_class=//p' | head -n1)"
    if [ "$kind" != "model-bundle" ] || [ -z "$cls" ]; then
        echo "#2280: ${bundle} is not a model bundle (package_kind=${kind:-none}) — build one with 'an offline package (Enterprise) <tag> --model-bundle <class>'." >&2
        return 2
    fi
    fam="$(razzfazz_box_hardware_class "$hardware")"
    if [ "$fam" = "apple" ]; then
        echo "#2280: this box resolves to hardware class apple (HARDWARE=${hardware:-unset}); a Mac is a native thin worker and takes no model bundle — refusing." >&2
        return 2
    fi
    if [ "$cls" != "$fam" ]; then
        echo "#2280: ${bundle} carries the ${cls} model set; this box is ${fam:-unknown} (HARDWARE=${hardware:-unset}) — refusing. Use the razzfazz-<version>-models-${fam:-<class>}.tar.gz bundle." >&2
        return 2
    fi
    return 0
}

# razzfazz_models_volume_missing_sized ROWS — ROWS is "<basename>\t<bytes>" per
# line; prints each basename the reachable runtime's models volume does not hold
# AT THAT SIZE. A short file under the right name (a copy that ran out of space)
# is missing, not present. No runtime reachable: every name is missing.
razzfazz_models_volume_missing_sized() {
    local rows="$1" have="" n s
    case "$(razzfazz_models_runtime)" in
        gpustack) have=$(docker exec gpustack sh -c "find '$RAZZFAZZ_LOCAL_MODELS_DIR' -type f -name '*.gguf' -exec stat -c '%s %n' {} + 2>/dev/null" 2>/dev/null) ;;
        manager)  have=$(docker exec "$RAZZFAZZ_MANAGER_MODELS_CONTAINER" sh -c "find '$RAZZFAZZ_MANAGER_MODELS_DIR' -maxdepth 1 -type f -name '*.gguf' -exec stat -c '%s %n' {} + 2>/dev/null" 2>/dev/null) ;;
        *) printf '%s\n' "$rows" | cut -f1 | sed '/^$/d'; return 0 ;;
    esac
    # "<bytes> <path>" -> "<bytes> <basename>"
    have=$(printf '%s\n' "$have" | sed -E 's#^([0-9]+) (.*/)?#\1 #')
    while IFS=$'\t' read -r n s; do
        [ -n "$n" ] || continue
        printf '%s\n' "$have" | grep -qxF "${s} ${n}" || printf '%s\n' "$n"
    done <<< "$rows"
    return 0
}

# razzfazz_model_bundle_prepare <bundle> <catalogue.yaml> <dest> — verify the
# bundle completely and extract into <dest> (an existing empty directory) the
# GGUFs this box lacks; <dest> "" = verify only, write nothing (--check).
# Reads MODEL_BUNDLE_SHA256 (the out-of-band hash, checked inside the one pass).
# Returns 0 verified (<dest>/models holds what to stage; absent = nothing to
# stage), 1 could not run, 2 refused (reason on stderr). Nothing outside <dest>
# is written, so a refusal changes nothing on the box.
razzfazz_model_bundle_prepare() {
    local bundle="$1" catalogue="$2" dest="$3" tool cls carried missing="" need auth_rc=0 rc=0 t0
    tool="${SCRIPT_DIR:-.}/scripts/model_bundle.py"
    razzfazz_model_bundle_check "$bundle" "$(read_env_value "${SCRIPT_DIR:-.}/.env" HARDWARE 2>/dev/null || true)" || return 2
    cls="$(razzfazz_box_hardware_class)"
    carried="$(python3 -B "$tool" --bundle "$bundle" --list-carried 2>&1)" || {
        printf '%s\n' "$carried" >&2; echo "#2280: the model bundle's records cannot be read — not applied." >&2; return 2; }
    missing="$(razzfazz_models_volume_missing_sized "$carried")"
    if [ -z "$(printf '%s' "$missing" | tr -d '[:space:]')" ]; then
        print_substep "Every GGUF of the model bundle is already in the models volume at its recorded size — nothing to stage (#2280)."
        # An explicitly given hash is an explicit request: it is checked even
        # when nothing is copied, so a wrong one never reads as accepted.
        if [ -n "${MODEL_BUNDLE_SHA256:-}" ]; then
            print_substep "Checking the given --model-bundle-sha256 anyway (one read of the bundle)..."
            python3 -B "$tool" --bundle "$bundle" --verify-only --need /dev/null --expect-sha256 "$MODEL_BUNDLE_SHA256" \
                --catalogue "$catalogue" --class "$cls" || rc=$?
            case "$rc" in
                0) ;;
                2) echo "#2280: the model bundle was refused — nothing from it is applied." >&2; return 2 ;;
                *) echo "#2280: the model bundle could not be read (rc ${rc})." >&2; return 1 ;;
            esac
        fi
        return 0
    fi
    print_substep "Model bundle: $(printf '%s\n' "$missing" | sed '/^$/d' | wc -l | tr -d ' ') of $(printf '%s\n' "$carried" | sed '/^$/d' | wc -l | tr -d ' ') GGUF(s) are not in the models volume at their recorded size yet."
    # The out-of-band hash is checked inside the one pass. Without one, a
    # detached signature (with the fleet key) is the evidence; that check reads
    # the archive once more, and only then.
    if [ -z "${MODEL_BUNDLE_SHA256:-}" ]; then
        t0=$SECONDS
        RAZZFAZZ_PACKAGE_EXPECT_SHA256="" razzfazz_verify_package_authenticity "$bundle" || auth_rc=$?
        [ -f "${bundle}.openssl.sig" ] && print_substep "Model bundle signature check: $((SECONDS - t0))s."
        if [ "$auth_rc" -eq 2 ]; then
            echo "#2280: the model bundle failed its authenticity check (#781) — not applied." >&2
            return 2
        fi
        [ "$auth_rc" -eq 0 ] || print_warning "Model bundle authenticity NOT verified (#781) — applying; pass --model-bundle-sha256 or deliver a signed bundle with the fleet public key on this box."
    fi
    need="$(mktemp "${TMPDIR:-/tmp}/rzfz-bundle-need.XXXXXX")" || return 1
    printf '%s\n' "$missing" > "$need"
    print_substep "Reading the model bundle once: archive hash, member checks, per-file pins${dest:+, extracting what is missing into ${dest}}..."
    if [ -n "$dest" ]; then
        python3 -B "$tool" --bundle "$bundle" --dest "$dest" --need "$need" --expect-sha256 "${MODEL_BUNDLE_SHA256:-}" \
            --catalogue "$catalogue" --class "$cls" || rc=$?
    else
        python3 -B "$tool" --bundle "$bundle" --verify-only --need "$need" --expect-sha256 "${MODEL_BUNDLE_SHA256:-}" \
            --catalogue "$catalogue" --class "$cls" || rc=$?
    fi
    rm -f "$need"
    case "$rc" in
        0) return 0 ;;
        2) echo "#2280: the model bundle was refused — nothing from it is applied." >&2; return 2 ;;
        *) echo "#2280: the model bundle could not be read or extracted (rc ${rc})." >&2; return 1 ;;
    esac
}

# razzfazz_stage_model_bundle_dir <dest> — copy what prepare extracted into the
# runtime's models volume (only verified GGUFs are there), then remove <dest>.
# 0 staged or nothing to stage, 1 could not stage.
razzfazz_stage_model_bundle_dir() {
    local dest="$1" rc=0 t0=$SECONDS
    if [ -d "${dest}/models" ] && [ -n "$(find "${dest}/models" -type f -name '*.gguf' 2>/dev/null | head -n1)" ]; then
        razzfazz_stage_package_models "${dest}/models" || rc=1
        print_substep "Model bundle copy into the volume: $((SECONDS - t0))s."
    fi
    rm -rf "$dest"
    return "$rc"
}

# razzfazz_apply_model_bundle <bundle> — prepare into a fresh directory under the
# stack dir (checked against this box's catalogue), then stage. The appliance's
# path; the upgrade calls the two halves separately (verify before any change).
# 0 staged / nothing to stage, 1 could not stage, 2 refused.
razzfazz_apply_model_bundle() {
    local bundle="$1" dest rc=0
    if [ -z "$(razzfazz_models_runtime)" ]; then
        razzfazz_model_bundle_check "$bundle" "$(read_env_value "${SCRIPT_DIR:-.}/.env" HARDWARE 2>/dev/null || true)" || return 2
        print_warning "Neither gpustack nor ${RAZZFAZZ_MANAGER_MODELS_CONTAINER} is running — the model bundle was NOT staged (#2280). Start the stack and re-run."
        return 1
    fi
    dest="$(mktemp -d "${SCRIPT_DIR:-.}/.model-bundle-XXXXXX")" || { echo "#2280: could not create an extraction directory under ${SCRIPT_DIR:-.}." >&2; return 1; }
    razzfazz_model_bundle_prepare "$bundle" "${SCRIPT_DIR:-.}/core/llm/standard-models.yaml" "$dest" || rc=$?
    if [ "$rc" -ne 0 ]; then
        rm -rf "$dest"
        return "$rc"
    fi
    razzfazz_stage_model_bundle_dir "$dest"
}

# razzfazz_offline_egress_firewall_active — is the host egress firewall that
# scripts/harden-offline-host.sh --egress-firewall installs in force? Returns 0
# when it is, 1 when it is not. Needs no root. Signals, in order: the
# world-readable marker harden writes on install and --undo removes (#2885);
# the state of the persisting systemd unit (systemctl is-active works for any
# user); the nft table, which only counts when the caller can read it (root).
# Without root and without an active unit the answer is "not active", which can
# miss a hand-loaded table: say so wherever the answer is shown to an operator.
# RAZZFAZZ_OFFLINE_EGRESS_UNIT / _TABLE / _MARKER are test hooks.
razzfazz_offline_egress_firewall_active() {
    local unit="${RAZZFAZZ_OFFLINE_EGRESS_UNIT:-razzfazz-offline-egress.service}"
    local table="${RAZZFAZZ_OFFLINE_EGRESS_TABLE:-razzfazz_offline}"
    local marker="${RAZZFAZZ_OFFLINE_EGRESS_MARKER:-/var/lib/razzfazz/offline-harden/active}"
    [ -e "$marker" ] && return 0
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$unit" 2>/dev/null; then
        return 0
    fi
    if command -v nft >/dev/null 2>&1 && nft list table inet "$table" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# razzfazz_offline_egress_undo_command — the exact command that lifts the firewall.
razzfazz_offline_egress_undo_command() {
    printf 'sudo %s/scripts/harden-offline-host.sh --undo\n' "${SCRIPT_DIR:-.}"
}

# razzfazz_network_mode [envfile] — print the effective mode (online|proxied|offline).
# Never sources .env (grep via read_env_value). Deterministic; safe under set -e.
razzfazz_network_mode() {
    local file="${1:-${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR:-.}/.env}}"
    local mode off cp
    mode="$(read_env_value "$file" RAZZFAZZ_NETWORK_MODE)"
    case "$mode" in
        online|proxied|offline) printf '%s\n' "$mode"; return 0 ;;
    esac
    # Derive from the legacy booleans (offline wins over proxied — it is the
    # stricter egress posture). Accept 1/true/yes/on for compat with either style.
    off="$(read_env_value "$file" RAZZFAZZ_OFFLINE)"
    case "$off" in 1|true|TRUE|yes|on) printf 'offline\n'; return 0 ;; esac
    cp="$(read_env_value "$file" RAZZFAZZ_CORPORATE_PROXY)"
    case "$cp" in 1|true|TRUE|yes|on) printf 'proxied\n'; return 0 ;; esac
    printf '%s\n' "$RAZZFAZZ_NETWORK_MODE_DEFAULT"
}

# razzfazz_registry_reachable [envfile] — return 0 when a container registry
# answers this box at all (any HTTP status, 401 included: the question is
# reachability, not authorisation), 1 when nothing answers within the bound.
# #2167: the appliance loader's INCOMPLETE branch re-enabled build/pull "so an
# online box can heal itself" with no check that a registry exists — an
# air-gapped box (0.175, journey B, egress cut: 163 blocked packets from the
# host, 175 from containers, 0 successes) then pulled ~20 services into a wall.
# The mirror when the box declares one, Docker Hub otherwise; curl honours the
# proxied mode's environment.
razzfazz_registry_reachable() {
    local file="${1:-${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR:-.}/.env}}" mirror url code
    mirror="$(read_env_value "$file" RAZZFAZZ_REGISTRY_MIRROR 2>/dev/null)" || mirror=""
    if [ -n "$mirror" ]; then
        case "$mirror" in http://*|https://*) url="${mirror%/}/v2/" ;; *) url="https://${mirror%/}/v2/" ;; esac
    else
        url="https://registry-1.docker.io/v2/"
    fi
    code="$(curl -sS -o /dev/null --max-time 8 -w '%{http_code}' "$url" 2>/dev/null)" || code=""
    case "$code" in
        ''|000) return 1 ;;
        *) return 0 ;;
    esac
}

# razzfazz_is_offline [envfile] — return 0 when the box is in offline mode.
razzfazz_is_offline() {
    [ "$(razzfazz_network_mode "$@")" = "offline" ]
}

# razzfazz_apt_listchanges_apply <offline|online> — the ONE place that knows
# the systemctl calls for the apt-listchanges units at the offline boundary
# (#3234). offline: stop + disable the timer and the service (should it have
# its own trigger) and mask both — apt-listchanges.timer runs
# OnCalendar=hourly and fetches changelogs.ubuntu.com, which an offline box
# must never do. online: unmask both and enable the timer again. The packages
# stay installed either way (#3223: the offline stick carries them). Every
# call is best-effort: a unit systemd does not know must not abort the caller
# (this runs under set -e in harden-host).
razzfazz_apt_listchanges_apply() {
    case "$1" in
        offline)
            systemctl disable --now apt-listchanges.service 2>/dev/null || true
            systemctl disable --now apt-listchanges.timer 2>/dev/null || true
            systemctl mask apt-listchanges.service 2>/dev/null || true
            systemctl mask apt-listchanges.timer 2>/dev/null || true
            ;;
        online)
            systemctl unmask apt-listchanges.service 2>/dev/null || true
            systemctl unmask apt-listchanges.timer 2>/dev/null || true
            systemctl enable --now apt-listchanges.timer 2>/dev/null || true
            ;;
    esac
}

# razzfazz_apt_listchanges_reconcile [envfile] — #3234: bring the apt-listchanges
# units in line with the RESOLVED network mode of envfile. This is the ONE
# reconcile implementation: scripts/apply-network-mode.sh (the mode switch) and
# cli/init.sh (the stick/firstboot path, which persists the mode only AFTER
# harden-host ran, so harden-host's own offline branch saw the template's
# `online`) both call it, so the two paths cannot drift. offline → disable+mask
# both units; online → unmask + enable the timer; proxied → untouched. Root (or
# RAZZFAZZ_NETWORK_SKIP_ROOT_CHECK=1) applies; a non-root caller gets the exact
# sudo command and nothing runs. Best-effort: always returns 0 (callers run
# under set -e).
razzfazz_apt_listchanges_reconcile() {
    local file="${1:-${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR:-.}/.env}}"
    local mode
    mode="$(razzfazz_network_mode "$file")"
    case "$mode" in
        online|offline) ;;
        *) return 0 ;;
    esac
    if [ "$(id -u)" -eq 0 ] || [ "${RAZZFAZZ_NETWORK_SKIP_ROOT_CHECK:-0}" = "1" ]; then
        razzfazz_apt_listchanges_apply "$mode"
        if [ "$mode" = "offline" ]; then
            print_substep "Network mode=offline: apt-listchanges.timer disabled and masked (no hourly changelogs.ubuntu.com lookups)."
        else
            print_substep "Network mode=online: apt-listchanges.timer unmasked and enabled again."
        fi
    else
        local verb="unmask/enable"
        if [ "$mode" = "offline" ]; then verb="disable/mask"; fi
        print_info "apt-listchanges.timer: ${verb} needs root. Run: sudo ${SCRIPT_DIR:-.}/scripts/apply-network-mode.sh --mode ${mode}"
    fi
    return 0
}

# razzfazz_offline_skip "<action>" [envfile] — when the box is OFFLINE, print a
# standardized skip notice for an INTERNET-EGRESS action and return 0 (caller
# should skip). Return 1 otherwise (caller proceeds normally).
#
# SCOPE: offline == zero WAN/internet egress, NOT zero network. This gate is for
# outbound-to-INTERNET operations only (docker build/pull, HuggingFace pulls,
# plugin marketplace, apt, external git). It must NEVER be used to gate LAN /
# inter-container traffic — a Dify workflow reaching a local-subnet target, the
# Caddy SSRF proxy to a LAN host, service-to-service calls all stay up offline.
razzfazz_offline_skip() {
    local action="${1:-internet-egress step}"
    shift 2>/dev/null || true
    if razzfazz_is_offline "$@"; then
        print_substep "OFFLINE (RAZZFAZZ_NETWORK_MODE=offline): skipping internet-egress step — ${action}. Content comes from the offline package; run 'rzfz verify-images' to confirm what is present."
        return 0
    fi
    return 1
}

# ── Speaches STT/TTS models (post-install provisioning, restore, status) ─────
# The pinned default ids. post-install downloads them, a full restore
# (cli/backup.sh) re-runs that download because backups exclude model files
# (speaches-data is in BACKUP_EXCLUDE_REGEXP), and `rzfz status` checks that
# Speaches lists them. The Open WebUI audio config in post-install and
# modules/agents/manager/app/services/voice_config.py name the same ids.
RAZZFAZZ_SPEACHES_STT_MODEL="Systran/faster-whisper-small"
RAZZFAZZ_SPEACHES_TTS_MODEL="ufozone/piper-de_DE-jarvis-high"

speaches_download_model() {
    local model_id="$1" model_type="$2"
    local speaches_url="http://127.0.0.1:${SPEACHES_PORT:-5003}"

    # #184 WS2b: the POST below tells Speaches to DOWNLOAD the model from
    # HuggingFace → internet egress. Skip on an air-gapped box (the model ships in
    # the offline package / is preloaded into the speaches cache volume).
    if razzfazz_offline_skip "Speaches ${model_type} model download from HuggingFace ('${model_id}')"; then
        return 0
    fi

    # Check if already installed
    local installed
    # --max-time: a stalled Speaches must not hang post-install or a restore.
    installed=$(curl -s --max-time 10 "$speaches_url/v1/models/$model_id" 2>/dev/null | \
        python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('id') else 'no')" 2>/dev/null)
    
    if [ "$installed" = "yes" ]; then
        print_info "$model_type model '$model_id' already installed."
        return 0
    fi
    
    print_substep "Downloading $model_type model '$model_id'..."
    local resp
    resp=$(curl -s --max-time 300 -X POST "$speaches_url/v1/models/$model_id" 2>&1)
    
    if echo "$resp" | grep -qi "downloaded\|already exists"; then
        print_success "$model_type model '$model_id' ready."
        return 0
    else
        print_warning "$model_type model download response: ${resp:-no answer from Speaches within 300 s (a slow download may still be running)}"
        # #178 review: this is the one place a caller needs to tell "download
        # actually failed" from "already installed / just downloaded" — an
        # explicit return 1 here is what lets step_speaches_provisioning
        # record WARN instead of a silent OK for an optional model that
        # didn't come down.
        return 1
    fi
}

# razzfazz_speaches_download_defaults — speaches_download_model for the pinned
# STT and TTS ids. Returns 1 when either download failed (both are tried).
# Offline, speaches_download_model skips the HuggingFace pull and returns 0, so
# rc 0 does not mean the models are there: razzfazz_speaches_missing_models does.
razzfazz_speaches_download_defaults() {
    local failed=false
    # #2714, #3072: package first on ANY box. The models travel in the offline
    # package (speaches-models/), and a package at hand is used whatever the
    # network mode says; the mode only decides whether the per-model step below
    # may fall back to a download (online it downloads what is still missing,
    # offline it skips).
    razzfazz_speaches_load_from_package || true
    speaches_download_model "$RAZZFAZZ_SPEACHES_STT_MODEL" "STT" || failed=true
    speaches_download_model "$RAZZFAZZ_SPEACHES_TTS_MODEL" "TTS" || failed=true
    [ "$failed" = false ]
}

# ── #2714: the Speaches STT/TTS models travel in the offline package ─────────
# Layout of the package subtree `speaches-models/` (HuggingFace hub cache, but
# with regular files only — no blobs/ and no symlinks, which a package must not
# carry):
#   speaches-models/models--<org>--<name>/refs/main
#   speaches-models/models--<org>--<name>/snapshots/<rev>/<files>
#   speaches-models/SPEACHES_MODELS   the pinned ids, one per line
#   speaches-models/SHA256SUMS        every file above, relative paths
RAZZFAZZ_SPEACHES_PKG_SUBDIR="speaches-models"
RAZZFAZZ_SPEACHES_CONTAINER="${RAZZFAZZ_SPEACHES_CONTAINER:-speaches}"
RAZZFAZZ_SPEACHES_HUB_PATH="/home/ubuntu/.cache/huggingface/hub"

razzfazz_speaches_hub_dirname() {   # Org/name -> models--Org--name
    printf 'models--%s\n' "${1//\//--}"
}

# razzfazz_speaches_stage_models <dest-dir> <hub-cache-dir>
#   Copies the two pinned repos out of a HuggingFace hub cache as regular files
#   and writes SPEACHES_MODELS + SHA256SUMS. rc 1 (nothing usable left in dest)
#   when a repo is missing from the cache.
razzfazz_speaches_stage_models() {
    local dest="$1" src="$2" id d rev f
    rm -rf "$dest"; mkdir -p "$dest"
    for id in "$RAZZFAZZ_SPEACHES_STT_MODEL" "$RAZZFAZZ_SPEACHES_TTS_MODEL"; do
        d="$(razzfazz_speaches_hub_dirname "$id")"
        rev="$(cat "${src}/${d}/refs/main" 2>/dev/null)" || rev=""
        if [ -z "$rev" ] || [ ! -d "${src}/${d}/snapshots/${rev}" ]; then
            echo "Speaches model '${id}' is not in the cache ${src} (${d}/refs/main + snapshots/<rev>)." >&2
            rm -rf "$dest"; return 1
        fi
        mkdir -p "${dest}/${d}/refs" "${dest}/${d}/snapshots/${rev}"
        printf '%s' "$rev" > "${dest}/${d}/refs/main"
        # cp -rL: the hub cache's snapshot entries are symlinks into blobs/.
        cp -rL "${src}/${d}/snapshots/${rev}/." "${dest}/${d}/snapshots/${rev}/" || { rm -rf "$dest"; return 1; }
        printf '%s\n' "$id" >> "${dest}/SPEACHES_MODELS"
    done
    ( cd "$dest" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z \
        | xargs -0 sha256sum | sed 's#  \./#  #' > SHA256SUMS ) || { rm -rf "$dest"; return 1; }
    return 0
}

# razzfazz_speaches_verify_staged <dir> — the checksums hold and both pinned ids
# are listed. rc 1 otherwise.
razzfazz_speaches_verify_staged() {
    local dir="$1" id
    [ -s "${dir}/SHA256SUMS" ] && [ -s "${dir}/SPEACHES_MODELS" ] || return 1
    for id in "$RAZZFAZZ_SPEACHES_STT_MODEL" "$RAZZFAZZ_SPEACHES_TTS_MODEL"; do
        grep -qxF "$id" "${dir}/SPEACHES_MODELS" || return 1
    done
    ( cd "$dir" && sha256sum -c --quiet SHA256SUMS >/dev/null 2>&1 )
}

# razzfazz_speaches_package_step <staging-dir> — package build: stage the
# models into <staging-dir>/speaches-models. The source is
# $RAZZFAZZ_SPEACHES_CACHE_DIR (a hub cache directory) or the box's
# speaches-data volume. Not carrying them is a warning, not a failure; a cache
# that is present but yields a corrupt copy fails (rc 1).
razzfazz_speaches_package_step() {
    local staging="$1" src="${RAZZFAZZ_SPEACHES_CACHE_DIR:-}" tmp="" vol="" helper="${PACKAGE_HELPER_IMAGE:-alpine:3.24}" rc=0
    if [ -z "$src" ] && command -v docker >/dev/null 2>&1; then
        vol="$(docker volume ls -q 2>/dev/null | grep -E '(^|_)speaches-data$' | head -1 || true)"
        if [ -n "$vol" ]; then
            tmp="$(mktemp -d "${TMPDIR:-/tmp}/rzfz-speaches.XXXXXX")"
            if docker run --rm -v "${vol}":/src:ro "$helper" tar cf - -C /src . | tar -C "$tmp" -xf -; then
                src="$tmp"
            fi
        fi
    fi
    if [ -z "$src" ]; then
        print_warning "  No Speaches model cache found (set RAZZFAZZ_SPEACHES_CACHE_DIR or build on a box whose speaches-data holds the models) — the package ships WITHOUT the STT/TTS models; an offline box will have no voice (#2714)."
        [ -z "$tmp" ] || rm -rf "$tmp"
        return 0
    fi
    razzfazz_speaches_stage_models "${staging}/${RAZZFAZZ_SPEACHES_PKG_SUBDIR}" "$src" || rc=1
    [ -z "$tmp" ] || rm -rf "$tmp"
    if [ "$rc" -ne 0 ]; then
        print_warning "  The Speaches model cache lacks the pinned STT/TTS models — the package ships WITHOUT them (#2714)."
        return 0
    fi
    if ! razzfazz_speaches_verify_staged "${staging}/${RAZZFAZZ_SPEACHES_PKG_SUBDIR}"; then
        print_error "  The staged Speaches models do not match their checksums (#2714)."
        rm -rf "${staging:?}/${RAZZFAZZ_SPEACHES_PKG_SUBDIR}"
        return 1
    fi
    print_success "  Speaches STT/TTS models staged ($(du -sh "${staging}/${RAZZFAZZ_SPEACHES_PKG_SUBDIR}" | cut -f1), checksummed) (#2714)."
    return 0
}

# razzfazz_speaches_load_from_package [package] — box side, any mode: put the
# package's Speaches models into the speaches container's hub cache when
# Speaches lacks them. rc 0 loaded or nothing missing, 1 the package carries no
# (valid) models — the caller keeps today's warning behaviour.
razzfazz_speaches_load_from_package() {
    local pkg="${1:-${APPLIANCE_OFFLINE_PKG:-/opt/razzfazz-appliance/razzfazz-offline.tar.gz}}"
    local missing idx prefix tmp
    [ -f "$pkg" ] || return 1
    missing="$(razzfazz_speaches_missing_models)" || return 1
    [ -n "$missing" ] || return 0
    idx="$(appliance_pkg_index "$pkg")" || return 1
    prefix="$(appliance_pkg_prefix "$idx" "$RAZZFAZZ_SPEACHES_PKG_SUBDIR")" || {
        print_warning "The package ${pkg} carries no Speaches models."
        return 1
    }
    tmp="$(mktemp -d "${TMPDIR:-/var/tmp}/rzfz-speaches.XXXXXX")" || return 1
    if ! tar -xzf "$pkg" -C "$tmp" "$prefix" 2>/dev/null \
       || ! razzfazz_speaches_verify_staged "${tmp}/${RAZZFAZZ_SPEACHES_PKG_SUBDIR}"; then
        print_warning "The Speaches models in ${pkg} are unreadable or fail their checksums - NOT loaded."
        rm -rf "$tmp"; return 1
    fi
    if docker cp "${tmp}/${RAZZFAZZ_SPEACHES_PKG_SUBDIR}/." "${RAZZFAZZ_SPEACHES_CONTAINER}:${RAZZFAZZ_SPEACHES_HUB_PATH}/" >/dev/null 2>&1; then
        print_success "Speaches STT/TTS models loaded from the package (no download)."
        rm -rf "$tmp"; return 0
    fi
    print_warning "Could not copy the Speaches models into container ${RAZZFAZZ_SPEACHES_CONTAINER}."
    rm -rf "$tmp"; return 1
}

# razzfazz_speaches_missing_models [base-url] — which pinned STT/TTS ids
# Speaches does NOT list in GET /v1/models, one per line (nothing printed =
# both present). Returns 1 and prints nothing when Speaches does not answer
# with a model list — the caller then knows nothing about the models.
razzfazz_speaches_missing_models() {
    local url="${1:-http://127.0.0.1:${SPEACHES_PORT:-5003}}" body ids id
    body=$(curl -sf --max-time 10 "${url%/}/v1/models" 2>/dev/null) || return 1
    ids=$(printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for m in d.get("data", []):
    if isinstance(m, dict) and m.get("id"):
        print(m["id"])
' 2>/dev/null) || return 1
    # `case` on whole lines, not `| grep -q`: under pipefail an early-exiting
    # grep can SIGPIPE the printf and turn a match into a miss.
    for id in "$RAZZFAZZ_SPEACHES_STT_MODEL" "$RAZZFAZZ_SPEACHES_TTS_MODEL"; do
        case $'\n'"$ids"$'\n' in
            *$'\n'"$id"$'\n'*) ;;
            *) printf '%s\n' "$id" ;;
        esac
    done
    return 0
}

# #1373 — which profiles CONSUME the locally-built llama-* runner images
# (llama-vulkan/rocm/cpu-runner): `llm-worker-agent`, the LLM Manager
# node-agent, whose per-hardware DEFAULT is one of these images
# (modules/llm/node-agent/app/drivers/images.py).
#
# #1309: `llm` (GPUStack 2.x custom backends) USED to be the second entry and
# is gone with the profile — `core/config/profiles.yaml` knows only
# llm-legacy / llm-manager / llm-registry / llm-worker-agent. It was not
# merely stale: nothing can reach it any more. A fresh install writes its
# profiles from profiles.yaml, and on the upgrade path the profile rewrites
# (cli/upgrade.sh:6237/6379/6495, `llm` -> `llm-legacy`) all run BEFORE the
# build gate reads COMPOSE_PROFILES_NOW at :7527. `llm-legacy` is
# deliberately NOT here — it runs GPUStack 0.7.1's own Vulkan build, not
# these images.
# ONE list: init Step 7b, upgrade Step 9b and post-install's
# refresh_llama_vulkan_runner all gate on it. Before #1373 each of the three
# grepped for `llm` alone, so the 2026.09 manager-shape profile set built
# nothing and a clean install ended with every model `failed` — the node
# tried to pull the bare local name from Docker Hub (0.175, 2026-09-05).
RAZZFAZZ_RUNNER_IMAGE_CONSUMER_PROFILES="llm-worker-agent"

# razzfazz_runner_images_wanted [profiles] — 0 when at least one consumer
# profile is active. $1 = comma-separated COMPOSE_PROFILES (defaults to the
# environment's). Exact-name match: `llm-legacy` / `llm-cpu` do NOT count,
# they ship their own gpustack runner.
razzfazz_runner_images_wanted() {
    local _profiles="${1-${COMPOSE_PROFILES:-}}" _p
    for _p in $RAZZFAZZ_RUNNER_IMAGE_CONSUMER_PROFILES; do
        if echo ",${_profiles}," | tr -d ' ' | grep -q ",${_p},"; then
            return 0
        fi
    done
    return 1
}

# ---- COMPOSE_FILE overlay list helpers (generic; parameterized) --------------
# apply-corporate-proxy.sh keeps its own single-overlay variants; these generic
# ones let the mode selector reconcile EITHER overlay without collision.
compose_file_overlay_present() {   # <envfile> <overlay>
    local cur; cur="$(read_env_value "$1" COMPOSE_FILE)"
    case ":${cur}:" in *":${2}:"*) return 0 ;; esac
    return 1
}
compose_file_overlay_add() {       # <envfile> <overlay>  (idempotent)
    local file="$1" overlay="$2" cur
    cur="$(read_env_value "$file" COMPOSE_FILE)"
    case ":${cur}:" in *":${overlay}:"*) return 0 ;; esac
    # A bare/empty COMPOSE_FILE becomes compose.yml:<overlay> (docker compose
    # 2.40+ chokes on an empty value).
    [ -n "$cur" ] || cur="compose.yml"
    update_env_value "$file" COMPOSE_FILE "${cur}:${overlay}"
}
compose_file_overlay_remove() {    # <envfile> <overlay>  (idempotent)
    local file="$1" overlay="$2" cur out="" e _ifs
    cur="$(read_env_value "$file" COMPOSE_FILE)"
    [ -n "$cur" ] || return 0
    _ifs="$IFS"; IFS=':'
    for e in $cur; do
        [ "$e" = "$overlay" ] && continue
        [ -z "$e" ] && continue
        out="${out:+${out}:}${e}"
    done
    IFS="$_ifs"
    update_env_value "$file" COMPOSE_FILE "${out:-compose.yml}"
}

# razzfazz_device_overlay_repair <envfile> — #2308. Since #1448 the merged
# llm-legacy service carries no device wiring and REFUSES to start without the
# overlay for its HARDWARE. Step 5b.2 of the TARGET's upgrader adds it; an
# offline crossing driven by an older upgrader never runs that step. This is
# the same rule, run from post-install --refresh, which always runs from the
# replaced tree. 0 = overlay added (caller recreates), 1 = nothing to do.
razzfazz_device_overlay_repair() {
    local env_file="$1" profiles hw want cur o rest
    profiles=",$(read_env_value "$env_file" COMPOSE_PROFILES),"
    case "$profiles" in *",llm-legacy,"*) ;; *) return 1 ;; esac
    hw="$(read_env_value "$env_file" HARDWARE)"
    case "$hw" in amd|nvidia|cpu) ;; *) return 1 ;; esac
    want="modules/llm/compose.devices.${hw}.yml"
    compose_file_overlay_present "$env_file" "$want" && return 1
    # Every pre-2026.07 top-level spelling goes, this HARDWARE's included (the
    # path no longer exists, compose would refuse it); of the modules/ form only
    # the other hardware's. The same slots Step 5b.2 manages.
    for o in amd nvidia cpu; do
        compose_file_overlay_remove "$env_file" "llm/compose.devices.${o}.yml"
        [ "$o" = "$hw" ] && continue
        compose_file_overlay_remove "$env_file" "modules/llm/compose.devices.${o}.yml"
    done
    cur="$(read_env_value "$env_file" COMPOSE_FILE)"
    [ -n "$cur" ] || cur="compose.yml"
    case "$cur" in
        compose.yml) update_env_value "$env_file" COMPOSE_FILE "compose.yml:${want}" ;;
        compose.yml:*) rest="${cur#compose.yml:}"; update_env_value "$env_file" COMPOSE_FILE "compose.yml:${want}:${rest}" ;;
        *) update_env_value "$env_file" COMPOSE_FILE "${cur}:${want}" ;;
    esac
    return 0
}

# _gen_offline_overlay <envfile> — (re)generate compose.offline.yml from the
# CURRENTLY-active compose model (pull_policy: never on every service — belt over
# the build-only defaults baked into the module compose files, and covering the
# pinned-image services too so nothing pulls at runtime offline). Best-effort:
# discovers services via `docker compose config --services`; if docker isn't
# available it leaves any existing file untouched and returns non-zero so the
# caller does NOT wire a dangling COMPOSE_FILE reference. Never aborts the caller.
_gen_offline_overlay() {
    local file="$1"
    local root="${SCRIPT_DIR:-.}"
    local gen="${root}/scripts/gen-offline-overlay.py"
    local out="${root}/${RAZZFAZZ_OFFLINE_OVERLAY}"
    [ -f "$gen" ] || return 1
    # Enumerate the active services WITHOUT the offline overlay itself in the
    # COMPOSE_FILE chain (avoids a chicken-and-egg if the file is being rebuilt).
    local base_cf svc_csv
    base_cf="$(read_env_value "$file" COMPOSE_FILE)"
    base_cf="$(printf '%s' "$base_cf" | tr ':' '\n' | grep -vx "$RAZZFAZZ_OFFLINE_OVERLAY" | paste -sd: - 2>/dev/null)"
    [ -n "$base_cf" ] || base_cf="compose.yml"
    if command -v docker >/dev/null 2>&1; then
        svc_csv="$( cd "$root" && COMPOSE_FILE="$base_cf" docker compose config --services 2>/dev/null | paste -sd, - 2>/dev/null )"
    fi
    if [ -n "${svc_csv:-}" ]; then
        # Surface a real (re)generation failure instead of silently keeping a
        # STALE overlay. A stale copy hides a telemetry-map / pull_policy update
        # that never landed — e.g. the file is root-owned from a prior sudo'd run
        # and an unprivileged apply can't overwrite it, or the generator errored.
        # #2778: run in the project root with the overlay out of COMPOSE_FILE, so the
        # generator sees the tree's own (digest-pinned) refs, not its previous rewrite.
        if ! ( cd "$root" && COMPOSE_FILE="$base_cf" python3 "$gen" --services "$svc_csv" --out "$out" ) 2>/dev/null; then
            print_warning "Could not (re)generate ${RAZZFAZZ_OFFLINE_OVERLAY} — write blocked or generator error; a stale overlay may be in use (check the file's owner/permissions)."
        fi
    fi
    [ -s "$out" ] && return 0 || return 1
}

# ensure_network_mode_overlay [envfile] — reconcile COMPOSE_FILE + the internal
# implementation booleans (RAZZFAZZ_CORPORATE_PROXY / RAZZFAZZ_OFFLINE) with the
# resolved RAZZFAZZ_NETWORK_MODE, and persist the resolved mode (back-compat
# migration for legacy-boolean boxes). Called from init/upgrade BEFORE compose
# up so the mode's overlay is always composed. Best-effort + idempotent; must
# never abort the caller (runs under set -e) — always returns 0.
ensure_network_mode_overlay() {
    local file="${1:-${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR:-.}/.env}}"
    [ -f "$file" ] || return 0
    local mode; mode="$(razzfazz_network_mode "$file")"

    # Persist the resolved mode when it was unset/derived (idempotent).
    local cur_mode; cur_mode="$(read_env_value "$file" RAZZFAZZ_NETWORK_MODE)"
    [ "$cur_mode" = "$mode" ] || update_env_value "$file" RAZZFAZZ_NETWORK_MODE "$mode"

    case "$mode" in
        offline)
            update_env_value "$file" RAZZFAZZ_OFFLINE 1
            update_env_value "$file" RAZZFAZZ_CORPORATE_PROXY 0
            compose_file_overlay_remove "$file" "$RAZZFAZZ_PROXY_OVERLAY"
            if _gen_offline_overlay "$file"; then
                compose_file_overlay_add "$file" "$RAZZFAZZ_OFFLINE_OVERLAY"
                print_substep "Network mode=offline: composed ${RAZZFAZZ_OFFLINE_OVERLAY} (no runtime pull), RAZZFAZZ_OFFLINE=1."
            else
                print_warning "Network mode=offline but ${RAZZFAZZ_OFFLINE_OVERLAY} could not be generated (docker unavailable?) — the RAZZFAZZ_OFFLINE gates are still active; regenerate later with 'rzfz setup --network-mode --mode offline'."
            fi
            ;;
        proxied)
            update_env_value "$file" RAZZFAZZ_CORPORATE_PROXY 1
            update_env_value "$file" RAZZFAZZ_OFFLINE 0
            compose_file_overlay_remove "$file" "$RAZZFAZZ_OFFLINE_OVERLAY"
            if [ -f "${SCRIPT_DIR:-.}/${RAZZFAZZ_PROXY_OVERLAY}" ]; then
                compose_file_overlay_add "$file" "$RAZZFAZZ_PROXY_OVERLAY"
                print_substep "Network mode=proxied: composed ${RAZZFAZZ_PROXY_OVERLAY}, RAZZFAZZ_CORPORATE_PROXY=1."
            else
                print_warning "Network mode=proxied but ${RAZZFAZZ_PROXY_OVERLAY} is missing — run 'rzfz setup --corporate-proxy --proxy-url … --ca-file …' to generate it."
            fi
            ;;
        online|*)
            update_env_value "$file" RAZZFAZZ_OFFLINE 0
            update_env_value "$file" RAZZFAZZ_CORPORATE_PROXY 0
            compose_file_overlay_remove "$file" "$RAZZFAZZ_OFFLINE_OVERLAY"
            compose_file_overlay_remove "$file" "$RAZZFAZZ_PROXY_OVERLAY"
            ;;
    esac

    # #184 P2 — the registry-mirror overlay is ORTHOGONAL to the egress mode:
    # reconcile it in EVERY mode (a box can be online+mirror or offline+mirror).
    ensure_registry_mirror_overlay "$file"
    return 0
}

# _gen_registry_mirror_overlay <envfile> — (re)generate compose.registry-mirror.yml
# from the CURRENTLY-active compose model, rewriting each PULLED service's `image:`
# to `${RAZZFAZZ_REGISTRY_MIRROR}/<normalized path>:<tag>`. Best-effort: discovers
# services + their image refs via `docker compose config --format json`; if docker
# isn't available (or the mirror is unset) it leaves any existing file untouched
# and returns non-zero so the caller does NOT wire a dangling COMPOSE_FILE ref.
# Never aborts the caller. Mirrors _gen_offline_overlay.
_gen_registry_mirror_overlay() {
    local file="$1"
    local root="${SCRIPT_DIR:-.}"
    local gen="${root}/scripts/gen-registry-mirror-overlay.py"
    local out="${root}/${RAZZFAZZ_REGISTRY_MIRROR_OVERLAY}"
    [ -f "$gen" ] || return 1
    local mirror; mirror="$(read_env_value "$file" RAZZFAZZ_REGISTRY_MIRROR)"
    [ -n "$mirror" ] || return 1
    # Enumerate services WITHOUT the mirror overlay itself in the COMPOSE_FILE
    # chain (so the discovered image refs are the upstream ones, not an already-
    # mirrored set; the rewrite is idempotent anyway, but this keeps it clean).
    local base_cf
    base_cf="$(read_env_value "$file" COMPOSE_FILE)"
    base_cf="$(printf '%s' "$base_cf" | tr ':' '\n' | grep -vx "$RAZZFAZZ_REGISTRY_MIRROR_OVERLAY" | paste -sd: - 2>/dev/null)"
    [ -n "$base_cf" ] || base_cf="compose.yml"
    if command -v docker >/dev/null 2>&1; then
        # Surface a real (re)generation failure instead of silently keeping a
        # STALE overlay (a stale copy could point at an old mirror or miss a
        # newly-added service). The generator reads COMPOSE_FILE from the env.
        if ! ( cd "$root" && COMPOSE_FILE="$base_cf" python3 "$gen" --mirror "$mirror" --out "$out" ) 2>/dev/null; then
            print_warning "Could not (re)generate ${RAZZFAZZ_REGISTRY_MIRROR_OVERLAY} — write blocked or generator error; a stale overlay may be in use (check the file's owner/permissions)."
        fi
    fi
    [ -s "$out" ] && return 0 || return 1
}

# ensure_registry_mirror_overlay [envfile] — reconcile compose.registry-mirror.yml
# in COMPOSE_FILE against RAZZFAZZ_REGISTRY_MIRROR (#184 P2). Set → generate +
# compose it; unset/empty → drop it. Called from ensure_network_mode_overlay so it
# runs on every init/upgrade/apply, in ANY network mode. Best-effort + idempotent;
# must never abort the caller (runs under set -e) — always returns 0.
ensure_registry_mirror_overlay() {
    local file="${1:-${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR:-.}/.env}}"
    [ -f "$file" ] || return 0
    local mirror; mirror="$(read_env_value "$file" RAZZFAZZ_REGISTRY_MIRROR)"
    if [ -n "$mirror" ]; then
        if _gen_registry_mirror_overlay "$file"; then
            compose_file_overlay_add "$file" "$RAZZFAZZ_REGISTRY_MIRROR_OVERLAY"
            print_substep "Registry mirror set (${mirror}): composed ${RAZZFAZZ_REGISTRY_MIRROR_OVERLAY} — install/upgrade pull redirected to the mirror (orthogonal to the network mode)."
        elif compose_file_overlay_present "$file" "$RAZZFAZZ_REGISTRY_MIRROR_OVERLAY"; then
            : # keep the existing overlay (docker unavailable this run) — no dangling ref introduced.
        else
            print_warning "RAZZFAZZ_REGISTRY_MIRROR=${mirror} set but ${RAZZFAZZ_REGISTRY_MIRROR_OVERLAY} could not be generated (docker unavailable?) — it will be regenerated on the next init/upgrade where docker is present."
        fi
    else
        # No mirror configured → ensure the overlay is not composed.
        compose_file_overlay_remove "$file" "$RAZZFAZZ_REGISTRY_MIRROR_OVERLAY"
    fi
    return 0
}

# ensure_nobuild_overlay [envfile] — compose compose.no-build.yml into COMPOSE_FILE
# (#184 WS2a). UNIVERSAL: called on every init/upgrade in EVERY network mode, so
# `docker compose up -d` / module enable / disable can never build a missing
# custom image (they fail clear via verify-images instead). Idempotent; the file
# is COMMITTED so this is a pure COMPOSE_FILE-wiring step (no docker, no generation
# — cannot silently fail). Best-effort under set -e; always returns 0.
#
# The explicit install/package/upgrade build path must NOT see this overlay (it
# would have nothing to build) — those `docker compose build` sites strip it via
# compose_file_for_build(). This must be wired AFTER the install-time build in
# cli/init.sh, so the first build still has the build: contexts.
ensure_nobuild_overlay() {
    local file="${1:-${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR:-.}/.env}}"
    [ -f "$file" ] || return 0
    # Only wire it if the committed overlay is actually present in the checkout
    # (fail-open: a checkout that predates the file must not get a dangling ref).
    [ -f "${SCRIPT_DIR:-.}/${RAZZFAZZ_NOBUILD_OVERLAY}" ] || return 0
    if ! compose_file_overlay_present "$file" "$RAZZFAZZ_NOBUILD_OVERLAY"; then
        compose_file_overlay_add "$file" "$RAZZFAZZ_NOBUILD_OVERLAY"
        print_substep "Composed ${RAZZFAZZ_NOBUILD_OVERLAY} (universal no-runtime-build): up/enable/disable never build; a missing image fails clear via 'rzfz verify-images'."
    fi
    return 0
}

# ensure_wazuh_syslog_udp_overlay [envfile] — #2526: compose
# modules/apps/wazuh/compose.syslog-udp.yml into COMPOSE_FILE when
# WAZUH_SYSLOG_UDP=true, drop it otherwise. The flag drives both halves: this
# publishes the host port, the securityconfig one-shot adds the <remote> block.
# Called on every init/upgrade next to ensure_nobuild_overlay. Idempotent;
# best-effort under set -e; always returns 0.
# wazuh_syslog_udp_overlay_present <envfile> — the overlay is in COMPOSE_FILE,
# also when an operator wrote it as `./modules/...`. Read-only.
wazuh_syslog_udp_overlay_present() {
    local cur e _ifs rc=1
    cur="$(read_env_value "$1" COMPOSE_FILE)"
    _ifs="$IFS"; IFS=':'
    for e in $cur; do
        [ "${e#./}" = "$RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY" ] && rc=0
    done
    IFS="$_ifs"
    return "$rc"
}

# _wazuh_syslog_udp_overlay_normalise <envfile> — rewrite a `./`-prefixed entry
# of the overlay to the canonical spelling (and drop a second copy), so the
# generic add/remove helpers, which compare strings, see it. Writes only when
# something changes.
_wazuh_syslog_udp_overlay_normalise() {
    local file="$1" cur out="" e _ifs seen=0
    cur="$(read_env_value "$file" COMPOSE_FILE)"
    [ -n "$cur" ] || return 0
    _ifs="$IFS"; IFS=':'
    for e in $cur; do
        [ -z "$e" ] && continue
        if [ "${e#./}" = "$RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY" ]; then
            [ "$seen" = 1 ] && continue
            seen=1; e="$RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY"
        fi
        out="${out:+${out}:}${e}"
    done
    IFS="$_ifs"
    [ "$out" = "$cur" ] || update_env_value "$file" COMPOSE_FILE "$out"
}

ensure_wazuh_syslog_udp_overlay() {
    local file="${1:-${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR:-.}/.env}}"
    [ -f "$file" ] || return 0
    _wazuh_syslog_udp_overlay_normalise "$file"
    local want; want="$(read_env_value "$file" WAZUH_SYSLOG_UDP)"
    if [ "$want" = "true" ]; then
        # Fail-open: a checkout without the file gets no dangling reference.
        if [ ! -f "${SCRIPT_DIR:-.}/${RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY}" ]; then
            print_warning "WAZUH_SYSLOG_UDP=true but ${RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY} is missing from this checkout - UDP 514 stays unpublished."
            return 0
        fi
        if ! compose_file_overlay_present "$file" "$RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY"; then
            compose_file_overlay_add "$file" "$RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY"
            print_substep "WAZUH_SYSLOG_UDP=true: composed ${RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY} (UDP 514 on WAZUH_SYSLOG_HOST_BIND)."
        fi
    elif compose_file_overlay_present "$file" "$RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY"; then
        compose_file_overlay_remove "$file" "$RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY"
        print_substep "WAZUH_SYSLOG_UDP is not true: dropped ${RAZZFAZZ_WAZUH_SYSLOG_UDP_OVERLAY} (UDP 514 no longer published)."
    fi
    return 0
}

# compose_file_for_build [envfile] — print the box's COMPOSE_FILE with the
# no-build overlay STRIPPED, for the explicit `docker compose build` sites
# (install / package / upgrade / post-install pre-build). With the overlay in
# place `build:` is neutralised, so a build would find nothing to build; stripping
# it restores the build: contexts for the one place a build is intended. Never
# mutates .env — pure stdout, safe to inline as `COMPOSE_FILE="$(compose_file_for_build)"`.
compose_file_for_build() {
    local file="${1:-${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR:-.}/.env}}"
    local cur out="" e _ifs
    cur="$(read_env_value "$file" COMPOSE_FILE)"
    [ -n "$cur" ] || { printf 'compose.yml\n'; return 0; }
    _ifs="$IFS"; IFS=':'
    for e in $cur; do
        [ "$e" = "$RAZZFAZZ_NOBUILD_OVERLAY" ] && continue
        [ -z "$e" ] && continue
        out="${out:+${out}:}${e}"
    done
    IFS="$_ifs"
    printf '%s\n' "${out:-compose.yml}"
}

# ------------------------------------------------------------------------------
# compose_file_strip_included_modules [envfile]  (ga.15)
# ------------------------------------------------------------------------------
# Remove REDUNDANT `modules/**/compose.yml` entries from COMPOSE_FILE that the
# root `compose.yml` already composes via its `include:` block. Module activation
# is governed by `include:` + COMPOSE_PROFILES (each module's services carry a
# `profiles:` gate), so a directly-listed module compose file adds NOTHING — but
# a directly-listed module resolves its own `env_file: ../../.env` relative to the
# PROJECT directory (`<stack-root>/../../.env` = `/home/.env`) instead of the
# compose-file dir, which breaks `docker compose <cmd> <that module's services>`
# ("env file /home/.env not found"). Legacy pre-`include:` boxes carry such
# entries; strip them on upgrade so module compose ops work. KEEPS `compose.yml`,
# the overlays (compose.no-build / offline / proxy / registry-mirror), and the LLM
# device overlay `modules/llm/compose.devices.*.yml` (NOT an include: entry).
compose_file_strip_included_modules() {
    local file="${1:-${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR:-.}/.env}}"
    [ -f "$file" ] || return 0
    local cur; cur="$(read_env_value "$file" COMPOSE_FILE)"
    [ -n "$cur" ] || return 0
    local root="${SCRIPT_DIR:-.}/compose.yml"
    [ -f "$root" ] || root="$(dirname "$file")/compose.yml"
    [ -f "$root" ] || return 0
    # Module compose paths the root compose.yml pulls in via `include:` (strip ./).
    local included
    included="$(grep -oE 'path:[[:space:]]*\./modules/[A-Za-z0-9._/-]+\.ya?ml' "$root" \
                  | sed -E 's#path:[[:space:]]*\./##' | sort -u)"
    [ -n "$included" ] || return 0
    local out="" e _ifs changed=0
    _ifs="$IFS"; IFS=':'
    for e in $cur; do
        [ -z "$e" ] && continue
        case "$e" in
            modules/*compose.yml|modules/*compose.yaml)
                if printf '%s\n' "$included" | grep -qxF "$e"; then
                    changed=1
                    continue
                fi
                ;;
        esac
        out="${out:+${out}:}${e}"
    done
    IFS="$_ifs"
    if [ "$changed" = 1 ] && [ -n "$out" ] && [ "$out" != "$cur" ]; then
        update_env_value "$file" COMPOSE_FILE "$out"
        print_substep "Reconciled COMPOSE_FILE: dropped module entries already composed via compose.yml include: (fixes 'env file /home/.env not found')."
    fi
}

# ------------------------------------------------------------------------------
# Box-local compose overlays (#3412)
# ------------------------------------------------------------------------------
# The sanctioned place for an overlay that belongs to ONE box (a memory limit,
# a logging driver, a snapshot mount) is the stack root, named
# compose.local.<name>.yml. It is ignored by version control (clean tree, never
# in a package), inside the read-only tree the Settings portal mounts (so its
# allow-list accepts it and the portal cannot write it), and every COMPOSE_FILE
# writer here keeps entries it does not manage — this reconcile only REPORTS
# them, and applies the never-wire-a-dangling-reference rule to a missing file.
RAZZFAZZ_LOCAL_OVERLAY_GLOB='compose.local.*.yml'
# THE name rule, the same literal as razzfazz_common/compose_local.py NAME_ERE
# (the backup carries, and the restore writes back, exactly these names; a test
# pins the two strings equal). Size limit: RAZZFAZZ_LOCAL_OVERLAY_MAX_BYTES.
RAZZFAZZ_LOCAL_OVERLAY_ERE='^compose\.local\.[A-Za-z0-9._-]+\.yml$'
RAZZFAZZ_LOCAL_OVERLAY_MAX_BYTES=1048576

# compose_file_local_overlays <envfile> — COMPOSE_FILE's box-local overlay
# entries, one per line, in chain order, spelled as in .env. Read-only.
compose_file_local_overlays() {
    local cur e _ifs
    cur="$(read_env_value "$1" COMPOSE_FILE)"
    _ifs="$IFS"; IFS=':'
    for e in $cur; do
        case "${e#./}" in
            */*) ;;
            compose.local.?*.yml) printf '%s\n' "$e" ;;
        esac
    done
    IFS="$_ifs"
}

# compose_file_reconcile_local_overlays <envfile> [--dry-run] — upgrade step.
# An entry whose file exists is kept where it is and listed. An entry whose
# file is gone is DROPPED, loudly: docker compose refuses the whole chain on a
# missing file, so keeping it would stop the stack, and the overlay it names
# has no content left to apply. An entry outside the stack directory (absolute
# or home-relative) is kept and warned about: the Settings portal refuses every
# module change on such a chain. Always returns 0.
compose_file_reconcile_local_overlays() {
    local file="$1" dry="${2:-}" root e cur _ifs kept="" gone=""
    [ -f "$file" ] || return 0
    root="${SCRIPT_DIR:-$(dirname "$file")}"
    while IFS= read -r e; do
        [ -n "$e" ] || continue
        if ! [[ "${e#./}" =~ $RAZZFAZZ_LOCAL_OVERLAY_ERE ]]; then
            print_warning "COMPOSE_FILE entry '${e}' is left as it is: its name has characters outside letters, digits, '.', '_' and '-', so the Settings portal refuses the chain and a backup does not carry the file. Rename the file and fix the entry by hand."
            continue
        fi
        if [ -f "${root}/${e#./}" ]; then
            kept="${kept} ${e}"
            if [ "$(stat -c %s "${root}/${e#./}" 2>/dev/null || echo 0)" -gt "$RAZZFAZZ_LOCAL_OVERLAY_MAX_BYTES" ]; then
                print_warning "Box-local compose overlay '${e}' is larger than 1 MiB: a backup does not carry it."
            fi
        else
            gone="${gone} ${e}"
        fi
    done < <(compose_file_local_overlays "$file")
    [ -z "$kept" ] || print_substep "Box-local compose overlay(s) kept in COMPOSE_FILE:${kept}"
    for e in $gone; do
        if [ "$dry" = "--dry-run" ]; then
            print_warning "Would drop COMPOSE_FILE entry '${e}': ${root}/${e#./} does not exist, and docker compose refuses a chain that names a missing file."
        else
            compose_file_overlay_remove "$file" "$e"
            print_warning "Dropped COMPOSE_FILE entry '${e}': ${root}/${e#./} does not exist, and docker compose refuses a chain that names a missing file. Restore the file and add '${e}' to COMPOSE_FILE again if the overlay is still wanted."
        fi
    done
    cur="$(read_env_value "$file" COMPOSE_FILE)"
    _ifs="$IFS"; IFS=':'
    for e in $cur; do
        case "$e" in
            /*|'~'*)
                print_warning "COMPOSE_FILE entry '${e}' lives outside the stack directory: the Settings portal refuses every module change on such a chain. Move it into the stack directory as compose.local.<name>.yml and point COMPOSE_FILE at that name."
                ;;
        esac
    done
    IFS="$_ifs"
    return 0
}

# ------------------------------------------------------------------------------
# ensure_oidc_ca_superset  (#152 — 2026.07-ga.3)
# ------------------------------------------------------------------------------
# Rebuild certs/caddy-ca.pem as a SUPERSET trust bundle for the in-stack native-
# OIDC clients (Open WebUI, Gitea, Vaultwarden). Those containers mount this file
# as SSL_CERT_FILE / REQUESTS_CA_BUNDLE, which *REPLACE* the entire TLS trust
# store (Python httpx / authlib / requests; Go for Gitea). If the file holds only
# the Caddy internal root CA (the old TLS_MODE=internal behavior) or an empty
# placeholder (Let's Encrypt, where init used to SKIP writing it), the client can
# no longer verify PUBLIC issuers — so OIDC discovery against a Let's Encrypt
# auth.<domain>, or an upstream Google / Entra endpoint, fails with
# CERTIFICATE_VERIFY_FAILED and login 500s (#152, root-caused live on prod 8.246).
#
# The fix: ALWAYS make certs/caddy-ca.pem = (host system public CA bundle) +
# (Caddy internal root CA, appended only on TLS_MODE=internal, when it exists).
# A superset verifies BOTH public (LE / Google / Entra) AND the box's self-signed
# auth.<domain> — correct on every box, both TLS modes.
#
# Best-effort + idempotent: this runs under `set -e` in init / upgrade paths, so
# it must NEVER abort the caller. Every step is guarded and the function
# unconditionally returns 0. Must be called from the repo root (writes
# ./certs/caddy-ca.pem, reads ./.env, execs the `caddy` container).
# ==============================================================================
# #1595: a file bind-mount whose source Docker turned into a DIRECTORY
# ==============================================================================
# A missing bind source is not an error to Docker — it creates it, as an empty
# DIRECTORY. Every "does the path exist?" check is happy afterwards and the
# failure lands one layer later, somewhere unrelated:
#
#     ssl.create_default_context(cafile=cafile)
#     IsADirectoryError: [Errno 21] Is a directory
#
# `ensure_oidc_ca_superset` below already knows this repair for ONE file
# (#1080). On 2026-09-07 the same property struck in three places at once, so
# it is applied to the WHOLE set here, before `compose up` — which is the only
# moment at which it costs nothing.
#
# ONLY EMPTY directories are removed, and only inside the project. An empty
# directory at a file bind source is by construction Docker's own doing: no
# step of this stack ever creates one. A non-empty one is somebody's data and
# is reported, never touched.
repair_empty_dir_bind_sources() {
    local root="${SCRIPT_DIR:-$(pwd)}"
    local lister="${root}/scripts/file-bind-sources.py"
    [ -f "$lister" ] || return 0
    command -v python3 >/dev/null 2>&1 || return 0

    local path repaired=0 blocked=0
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        [ -d "$path" ] || continue                      # a file, or absent: nothing to do
        if rmdir "$path" 2>/dev/null; then
            repaired=$((repaired + 1))
            case "$path" in */caddy-ca.pem) RZFZ_CA_BUNDLE_SOURCE_REPLACED=yes ;; esac
            print_warning "${path} was an empty DIRECTORY where a file belongs (a bind mount materialised before the file existed, #1595) — removed."
            continue
        fi
        if [ -n "$(ls -A "$path" 2>/dev/null)" ]; then
            print_error "${path} is a non-empty DIRECTORY where this stack mounts a FILE (#1595). It is not ours to delete — move it aside and re-run."
        else
            print_error "${path} is an empty DIRECTORY this user cannot remove (#1595). Run: sudo rmdir '${path}'  — then re-run."
        fi
        blocked=$((blocked + 1))
    done < <(python3 "$lister" "$root" 2>/dev/null)

    [ "$repaired" -gt 0 ] && print_substep "Repaired ${repaired} bind source(s) Docker had turned into directories (#1595)."
    [ "$blocked" -gt 0 ] && return 1
    return 0
}

# Reload the containers that mount certs/caddy-ca.pem so they re-read the
# trust store (they mount it read-only; the process must restart to re-read its
# CA bundle). $1 = yes when the bind SOURCE changed type (the directory Docker
# had materialised was replaced by a file): `docker restart` keeps the
# container's old mount spec, which then points at a gone inode and the start
# fails (Exited 127), so only a recreate works. Only consumers that were
# running before are touched; one that was stopped stays stopped. Afterwards
# every one of them must be running again; otherwise name them, print the
# command that fixes it, and return 3.
# The consumer list must equal the set of caddy-ca.pem mount consumers across
# every compose file (the #631 guard). wazuh-indexer / wazuh-dashboard mount
# the bundle for openid_auth_domain / opensearch_security.openid.root_ca;
# openuem-worker-notification mounts it as SSL_CERT_FILE so go-mail can verify
# the relay's STARTTLS certificate, and Go reads the file once at first use.
# #2476: openbao, SSL_CERT_FILE for its OIDC client (static seal: it unseals
# itself after the restart).
# Compose calls run in the caller's cwd, i.e. with the same COMPOSE_FILE,
# COMPOSE_PROFILES and project as the stack the caller operates on.
_ca_bundle_reload_consumers() {
    local recreate="${1:-no}" _c _was="" _down="" _running _nl=$'\n'
    command -v docker >/dev/null 2>&1 || return 0
    _running=$(docker ps --format '{{.Names}}' 2>/dev/null || true)
    for _c in openwebui gitea vaultwarden wazuh-indexer wazuh-dashboard openuem-worker-notification openbao; do
        # case, not `printf | grep -q`: no early-exit reader under pipefail
        case "${_nl}${_running}${_nl}" in *"${_nl}${_c}${_nl}"*) ;; *) continue ;; esac
        _was="${_was} ${_c}"
        if [ "$recreate" = yes ]; then
            docker compose up -d --no-deps --force-recreate "$_c" >/dev/null 2>&1 \
                && print_substep "Recreated $_c (its CA-bundle mount source changed type)." \
                || print_warning "Could not recreate $_c."
        else
            docker restart "$_c" >/dev/null 2>&1 \
                && print_substep "Restarted $_c to reload the OIDC trust store." \
                || print_warning "Could not restart $_c."
        fi
    done
    for _c in $_was; do
        [ "$(docker inspect -f '{{.State.Running}}' "$_c" 2>/dev/null)" = true ] || _down="${_down} ${_c}"
    done
    if [ -n "$_down" ] && [ "$recreate" != yes ]; then
        # A plain restart failed or left a consumer down (the bind source may
        # have changed type in another process): ONE recreate of exactly it.
        local _still=""
        for _c in $_down; do
            docker compose up -d --no-deps --force-recreate "$_c" >/dev/null 2>&1 || true
            if [ "$(docker inspect -f '{{.State.Running}}' "$_c" 2>/dev/null)" = true ]; then
                print_substep "Recreated $_c (a plain restart did not bring it back)."
            else
                _still="${_still} ${_c}"
            fi
        done
        _down="$_still"
    fi
    [ -z "$_down" ] && return 0
    print_error "NOT running after the CA-bundle reload:${_down} - run: docker compose up -d --no-deps --force-recreate${_down}"
    return 3
}

ensure_oidc_ca_superset() {
    # yes also when repair_empty_dir_bind_sources already replaced the directory
    local _ca_bind_replaced="${RZFZ_CA_BUNDLE_SOURCE_REPLACED:-no}"
    print_step "Ensuring certs/caddy-ca.pem is an OIDC CA superset (system CA bundle + Caddy internal CA) (#152)..."
    mkdir -p certs 2>/dev/null || true

    # #1080: if a previous `docker compose up` ran while this file was missing,
    # Docker materialised the bind-mount target as a root-owned DIRECTORY. Every
    # write below then fails "Is a directory" (swallowed by the best-effort
    # guards) while the step still logs its success line — an unconverging
    # repair loop: the #631 status check keeps FAILing and keeps pointing at
    # `--refresh`, which keeps "succeeding". Docker creates the directory EMPTY,
    # so rmdir suffices; it is root-owned, so fall back to an rmdir as root in a
    # throwaway container (caddy's image is always present on a stack box).
    if [ -d certs/caddy-ca.pem ]; then
        local _rm_ok="no" _rm_img=""
        rmdir certs/caddy-ca.pem 2>/dev/null && _rm_ok="yes"
        if [ "$_rm_ok" = "no" ] && command -v docker >/dev/null 2>&1; then
            _rm_img="$(docker inspect caddy --format '{{.Config.Image}}' 2>/dev/null || true)"
            if [ -n "$_rm_img" ]; then
                docker run --rm --entrypoint rmdir -v "$(pwd)/certs:/c" "$_rm_img" /c/caddy-ca.pem >/dev/null 2>&1 && _rm_ok="yes"
            fi
        fi
        if [ "$_rm_ok" = "yes" ]; then
            print_warning "certs/caddy-ca.pem was a DIRECTORY (bind-mount materialised before the file existed, #1080) — removed; rebuilding the bundle."
            _ca_bind_replaced=yes
        else
            print_error "certs/caddy-ca.pem is a DIRECTORY this user cannot remove (#1080). Run: sudo rmdir certs/caddy-ca.pem  — then re-run 'rzfz post-install --refresh'. Skipping the CA-superset step (every write below would silently no-op)."
            return 0
        fi
    fi

    # 1) BASE = host system public CA bundle (Debian/Ubuntu path first, RHEL next).
    #    This is what lets the OIDC clients verify PUBLIC issuers (Let's Encrypt
    #    auth.<domain>, Google, Microsoft Entra) — the part that was missing.
    local _sys_base=""
    if [ -r /etc/ssl/certs/ca-certificates.crt ]; then
        _sys_base="/etc/ssl/certs/ca-certificates.crt"
    elif [ -r /etc/pki/tls/certs/ca-bundle.crt ]; then
        _sys_base="/etc/pki/tls/certs/ca-bundle.crt"
    fi
    if [ -n "$_sys_base" ]; then
        cat "$_sys_base" > certs/caddy-ca.pem 2>/dev/null || true
    fi
    # NEVER leave the trust anchor empty: Docker would auto-create the bind-mount
    # target as a DIRECTORY (SSL_CERT_FILE → dir → x509 error), and an empty store
    # verifies nothing. If no system bundle was readable, keep any existing content;
    # otherwise write a non-empty placeholder so the bind stays a FILE and warn.
    if [ ! -s certs/caddy-ca.pem ]; then
        print_warning "No system CA bundle found (/etc/ssl/certs/ca-certificates.crt or /etc/pki/tls/certs/ca-bundle.crt) — certs/caddy-ca.pem base is a placeholder; public OIDC issuers may NOT verify."
        printf '# razzfazz.ai OIDC CA bundle (#152) — no system CA bundle found on host\n' > certs/caddy-ca.pem 2>/dev/null || true
    fi
    local _base_n
    _base_n="$(grep -c 'BEGIN CERTIFICATE' certs/caddy-ca.pem 2>/dev/null || true)"
    _base_n="${_base_n:-0}"

    # 2) APPEND Caddy's live internal root CA on TLS_MODE=internal (self-signed
    #    auth.<domain>). Read TLS_MODE from .env WITHOUT sourcing it (operator-
    #    edited; may contain spaces / metachars — strip surrounding quotes + space).
    local _tls_mode _appended="no"
    # `|| true`: under `set -eo pipefail` a missing .env / absent TLS_MODE line
    # makes grep exit non-zero → the pipeline (pipefail) → this assignment would
    # abort the caller. Best-effort helper: swallow it (empty => letsencrypt).
    _tls_mode="$(grep -m1 '^TLS_MODE=' .env 2>/dev/null | cut -d= -f2- | tr -d '"'\'' ' || true)"
    # 2a) Append Caddy's live internal root CA whenever it exists — this covers
    #     TLS_MODE=internal (self-signed auth.<domain>) AND any box that carries a
    #     caddy-minted local CA. Checking the FILE (not the exact TLS_MODE string)
    #     is the robust test. Harmless on Let's Encrypt (the extra root is only
    #     ever used to verify caddy-internal-issued certs, which aren't public).
    local _ca_src="/data/caddy/pki/authorities/local/root.crt"
    if command -v docker >/dev/null 2>&1 \
       && docker exec caddy test -f "$_ca_src" >/dev/null 2>&1; then
        local _caddy_ca
        _caddy_ca="$(docker exec caddy cat "$_ca_src" 2>/dev/null || true)"
        # Guard: only append when it actually reads a PEM certificate.
        if printf '%s' "$_caddy_ca" | grep -q 'BEGIN CERTIFICATE'; then
            printf '%s\n' "$_caddy_ca" >> certs/caddy-ca.pem 2>/dev/null || true
            _appended="caddy-internal"
        else
            if [ "$_tls_mode" = "internal" ]; then
                print_warning "Caddy internal root CA at $_ca_src did not contain a PEM certificate — certs/caddy-ca.pem was NOT updated with the live CA. Self-signed *.\${MAIN_DOMAIN} OIDC clients (OWUI/Gitea/Vaultwarden) may still x509-fail; re-run once Caddy has finished minting its local CA."
            fi
        fi
    elif [ "$_tls_mode" = "internal" ]; then
        # #55 — on a TLS-internal box this file is expected to exist once Caddy has
        # started and issued its first internal-CA-backed certificate. Don't hard-fail
        # (Caddy may simply not be up yet on this call, e.g. a very early init retry) —
        # warn so the operator/next --refresh knows caddy-ca.pem is still stale until
        # this step succeeds.
        print_warning "Caddy internal root CA not found at $_ca_src (caddy container not running yet, or it hasn't generated its local CA) — certs/caddy-ca.pem is NOT yet trusting self-signed *.\${MAIN_DOMAIN}. Re-run ensure_oidc_ca_superset (init Step 7a / 'rzfz post-install --refresh') once Caddy is up."
    fi
    # 2b) TLS_MODE=certificate (operator brought their own cert): append the
    #     installed full-chain at certs/cert.pem (core/compose.yml mounts certs/
    #     into Caddy at /certs; TLS_CERT_PATH default /certs/cert.pem). A cert from
    #     a PUBLIC CA is already covered by the system base (2b is then a harmless
    #     dup), but a PRIVATE/corporate-CA cert's issuer is NOT in the system store
    #     — appending the operator's chain is what lets OWUI/Gitea/Vaultwarden
    #     verify auth.<domain> and complete OIDC. (#152, "prepare for all situations".)
    if [ "$_tls_mode" = "certificate" ] || [ "$_tls_mode" = "custom" ]; then
        if [ -s certs/cert.pem ] && grep -q 'BEGIN CERTIFICATE' certs/cert.pem; then
            cat certs/cert.pem >> certs/caddy-ca.pem 2>/dev/null || true
            _appended="${_appended:+${_appended}+}cert-chain"
        else
            print_warning "TLS_MODE=certificate but certs/cert.pem is missing/unreadable — a PRIVATE-CA cert's issuer won't be in the OIDC trust bundle, so login to auth.<domain>-backed OIDC may not verify. Install the full-chain at certs/cert.pem (or TLS_CERT_PATH) and re-run."
        fi
    fi

    # 2c) CORPORATE-PROXY TLS-INTERCEPT CA (#181). On a box behind a corporate
    #     forward proxy that MITM-inspects TLS, every egress container mounts THIS
    #     same certs/caddy-ca.pem as SSL_CERT_FILE/REQUESTS_CA_BUNDLE (which
    #     REPLACE the store). So the proxy's private CA must be IN this superset
    #     or all outbound TLS (HF pulls, plugin marketplace, npm/pip, git) x509-
    #     fails. Appended here — on EVERY init/upgrade rebuild of the bundle — so
    #     the trust survives code updates. Gated on the opt-in toggle; a box that
    #     never sets it is completely unaffected. Box-local CA file, never
    #     committed. See scripts/apply-corporate-proxy.sh + the design doc.
    local _cp _ca_file
    _cp="$(grep -m1 '^RAZZFAZZ_CORPORATE_PROXY=' .env 2>/dev/null | cut -d= -f2- | tr -d '"'\'' ' || true)"
    if [ "$_cp" = "1" ] || [ "$_cp" = "true" ]; then
        _ca_file="$(grep -m1 '^RAZZFAZZ_EXTRA_CA_FILE=' .env 2>/dev/null | cut -d= -f2- | tr -d '"'\'' ' || true)"
        if [ -n "$_ca_file" ] && [ -s "$_ca_file" ] && grep -q 'BEGIN CERTIFICATE' "$_ca_file" 2>/dev/null; then
            cat "$_ca_file" >> certs/caddy-ca.pem 2>/dev/null || true
            _appended="${_appended:+${_appended}+}corporate-proxy-ca"
        else
            print_warning "RAZZFAZZ_CORPORATE_PROXY=1 but RAZZFAZZ_EXTRA_CA_FILE ('${_ca_file:-unset}') is missing/unreadable — the proxy CA is NOT in the trust bundle, so egress TLS through the intercepting proxy will x509-fail. Re-run \`rzfz setup --corporate-proxy\`."
        fi
    fi
    # 2d) #2004 — the internal-services CA that signs the mail relay's STARTTLS
    #     certificate. It must be IN this bundle, because the bundle is what the
    #     verifying consumers mount: with Debian's snakeoil certificate the relay
    #     offered STARTTLS that nobody could verify, six modules turned TLS off,
    #     Infisical turned certificate verification off for its whole Node
    #     process, and OpenUEM — whose client verifies unconditionally — could do
    #     neither and simply could not send (#1992, measured on 0.91).
    #
    #     Read through the RELAY, which mounts the volume read-only, rather than
    #     `docker run` on the volume: the relay is running on any box that has a
    #     certificate to trust, and this needs no image and no extra container.
    if command -v docker >/dev/null 2>&1 \
       && docker exec smtp-relay test -f /certs/ca.crt >/dev/null 2>&1; then
        local _relay_ca
        _relay_ca="$(docker exec smtp-relay cat /certs/ca.crt 2>/dev/null || true)"
        if printf '%s' "$_relay_ca" | grep -q 'BEGIN CERTIFICATE'; then
            printf '%s\n' "$_relay_ca" >> certs/caddy-ca.pem 2>/dev/null || true
            _appended="${_appended:+${_appended}+}smtp-relay-ca"
        else
            print_warning "The mail relay's CA at /certs/ca.crt did not contain a PEM certificate — certs/caddy-ca.pem does NOT trust the relay's STARTTLS certificate, so a verifying mail client (OpenUEM) will x509-fail. Re-run once smtp-relay-certs has completed."
        fi
    fi

    [ -n "$_appended" ] || _appended="no"

    print_substep "certs/caddy-ca.pem: base = ${_base_n} system root(s); appended = ${_appended} (TLS_MODE='${_tls_mode:-letsencrypt}')."

    # 3) Reload the RUNNING consumers of the bundle (see _ca_bundle_reload_consumers).
    _ca_bundle_reload_consumers "$_ca_bind_replaced" || return 3
    return 0
}

# ------------------------------------------------------------------------------
# authentik_cacert_args — TLS verification args for Authentik API calls (#857)
# ------------------------------------------------------------------------------
# Every call that carries the Authentik admin bootstrap token (or a plaintext
# password) MUST verify TLS. `-k`/`--insecure` disables chain *and* hostname
# verification, so anything that answers for auth.<domain> collects a
# stack-admin API token. On TLS_MODE=internal the box's own Caddy CA is not in
# the system trust store, so verification needs certs/caddy-ca.pem — the OIDC
# CA superset ensure_oidc_ca_superset() maintains (#152).
#
# Sets the global array RZFZ_AUTHENTIK_CACERT_ARGS to `(--cacert <bundle>)`
# when that bundle exists and is non-empty, otherwise to `()` (system trust
# store). Never emits `-k`. Always returns 0 so it is safe under `set -e`.
#
# Usage:
#   authentik_cacert_args
#   curl -s "${RZFZ_AUTHENTIK_CACERT_ARGS[@]}" -H "Authorization: Bearer $tok" …
#
# The repo root is resolved from this file's own location (scripts/lib.sh), so
# it does not depend on the caller's cwd; the cwd-relative path is kept as a
# fallback for callers that run from a copied/staged tree.
authentik_cacert_args() {
    RZFZ_AUTHENTIK_CACERT_ARGS=()
    local _root _ca _candidates=()
    _root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)" || _root=""
    if [ -n "$_root" ]; then
        _candidates+=("${_root}/certs/caddy-ca.pem")
    fi
    _candidates+=("./certs/caddy-ca.pem")
    for _ca in "${_candidates[@]}"; do
        if [ -s "$_ca" ]; then
            RZFZ_AUTHENTIK_CACERT_ARGS=(--cacert "$_ca")
            return 0
        fi
    done
    return 0
}

# ------------------------------------------------------------------------------
# Sysadmin-style helpers (M026 #146)
# ------------------------------------------------------------------------------
# These exist so the two scripts that DON'T fit the print_step / print_info
# idiom (razzfazz-host-upgrade.sh and razzfazz-ai-box-setup.sh) can still
# source lib.sh and use a shared implementation instead of carrying their own
# log/die/warn/info copies.
#
# Differences from print_step / print_info:
#   - Timestamped (`[YYYY-MM-DD HH:MM:SS] ...`) — operators want a clear
#     time-since-start when stepping through long flows like a kernel
#     downgrade reboot dance.
#   - No colors — output is captured to a host-side log file via tee and
#     ANSI escapes look ugly there. (The canonical print_* helpers now have
#     a file-log path that strips ANSI, but the sysadmin scripts predate
#     that and ship to log files directly.)
#   - tee to $LOG_FILE built in. The caller is expected to set LOG_FILE in
#     the script's preamble (e.g. `LOG_FILE=/var/log/razzfazz-host-upgrade.log`).
#     If LOG_FILE is unset/empty, output goes to stdout only.
#   - sa_run wraps a shell command with DRY_RUN gating + automatic logging,
#     mirroring the existing run() helpers in razzfazz-host-upgrade.sh.
#
# Naming: prefixed with `sa_` (sysadmin) so they don't collide with the
# existing local definitions during a careful migration. Once the sysadmin
# scripts source lib.sh the local copies can be deleted and the prefix
# kept (consistent across calls).

# _sa_emit: write a single timestamped line to stdout + tee to $LOG_FILE
# when set. Internal helper — sa_log/info/warn/die all funnel through here.
_sa_emit() {
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    local line="[$ts] $*"
    if [ -n "${LOG_FILE:-}" ]; then
        printf '%s\n' "$line" | tee -a "$LOG_FILE"
    else
        printf '%s\n' "$line"
    fi
}

sa_log()  { _sa_emit "$*"; }
sa_info() { _sa_emit "INFO:  $*"; }
sa_ok()   { _sa_emit "OK:    $*"; }
sa_warn() { _sa_emit "WARN:  $*"; }
sa_die()  { _sa_emit "FATAL: $*"; exit 1; }

# sa_banner <title>: 4-line section separator.
sa_banner() {
    local rule='==============================================================='
    if [ -n "${LOG_FILE:-}" ]; then
        printf '\n%s\n  %s\n%s\n' "$rule" "$*" "$rule" | tee -a "$LOG_FILE"
    else
        printf '\n%s\n  %s\n%s\n' "$rule" "$*" "$rule"
    fi
}

# sa_run <cmd> [<args>...]: log the command, run it (or dry-run-log it if
# the caller exported DRY_RUN=true), tee combined output to $LOG_FILE.
# Returns the wrapped command's exit code so callers can branch.
sa_run() {
    if [ "${DRY_RUN:-false}" = true ]; then
        _sa_emit "DRY:   $*"
        return 0
    fi
    _sa_emit "RUN:   $*"
    if [ -n "${LOG_FILE:-}" ]; then
        "$@" 2>&1 | tee -a "$LOG_FILE"
        return "${PIPESTATUS[0]}"
    fi
    "$@"
}

# ── Artifact age (#380 / #423) ────────────────────────────────────────────────
# `stat -c %Y` (file mtime) is NOT a timestamp source for anything that arrives
# via `git checkout`, `git clone` or the offline-package rsync: every file gets
# the mtime of the moment it landed. Two real defects came from assuming
# otherwise:
#
#   #380  scripts/pre-tag-check.sh gated the security assessment on "mtime is
#         younger than 7 days". On a fresh clone every assessment is 0 days old,
#         so the freshness gate passed vacuously — it could never block.
#   #423  cli/status.sh picked the "latest" assessment with `ls -1t`. All 42
#         assessments share one checkout mtime, so the tie-break is arbitrary
#         and on box 0.236 it reported an April file for an August install.
#
# Git records when content was actually authored, and that survives clone,
# checkout and rsync. So: prefer the commit date; fall back to mtime ONLY when
# the file is untracked or locally modified, which is exactly the case where
# mtime IS meaningful (the operator just wrote it and has not committed yet).
#
# Prints the age in whole days on stdout, or nothing when it cannot be
# determined. Callers must treat "no output" as unknown — never as zero.
razzfazz_artifact_age_days() {
    local file="$1"
    [ -f "$file" ] || return 1

    local epoch=""
    if git rev-parse --git-dir >/dev/null 2>&1; then
        # A SHALLOW clone has no real history: `git log -1 --format=%ct <file>`
        # returns the only commit it has — the checkout — so every file looks
        # brand new. That is the SAME failure shape as mtime, and it is not
        # hypothetical: `actions/checkout` defaults to fetch-depth 1, and CI
        # reported a 2026-04 assessment as 0 days old, which would have made the
        # #380 freshness gate vacuous again on exactly the machine that runs it.
        #
        # There is no honest age to compute here, so return NOTHING (unknown).
        # Callers already treat empty as "cannot verify" and fail closed — the
        # one thing that must never happen is reporting unknown as zero.
        if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
            return 1
        fi
        # Locally modified or untracked → the working copy is newer than any
        # commit date, so mtime is the honest answer.
        if git diff --quiet HEAD -- "$file" 2>/dev/null \
           && git ls-files --error-unmatch "$file" >/dev/null 2>&1; then
            epoch=$(git log -1 --format=%ct -- "$file" 2>/dev/null)
        fi
    fi
    [ -n "$epoch" ] || epoch=$(stat -c %Y "$file" 2>/dev/null)
    [ -n "$epoch" ] || return 1

    echo $(( ( $(date +%s) - epoch ) / 86400 ))
}

# True when the age came from git (i.e. is trustworthy across a fresh clone)
# rather than from a checkout-stamped mtime. Callers that must fail closed use
# this to tell "genuinely fresh" from "cannot tell".
razzfazz_artifact_age_is_from_git() {
    local file="$1"
    git rev-parse --git-dir >/dev/null 2>&1 || return 1
    # A shallow clone's commit dates are the checkout's, not the content's.
    [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ] && return 1
    git ls-files --error-unmatch "$file" >/dev/null 2>&1 || return 1
    git diff --quiet HEAD -- "$file" 2>/dev/null || return 1
    [ -n "$(git log -1 --format=%ct -- "$file" 2>/dev/null)" ]
}

# ── Source-commit provenance (#2359) ─────────────────────────────────────────
# The commit in the DEVELOPMENT repository this tree was cut from.
#
# `scripts/publish-public.sh` stages an allow-list copy, `git init`s a fresh
# repository and force-pushes ONE commit as the public mirror's `main`, so every
# mirror sha exists in no internal history. A box installed from the mirror
# stamped that sha as RAZZFAZZ_COMMIT (measured on 0.79, 2026-09-21: `83aef85`)
# and support could not resolve it to a commit we hold. The export therefore
# writes PUBLIC_EXPORT_OF at the mirror root, and init/upgrade record its
# `source_commit` beside RAZZFAZZ_COMMIT as RAZZFAZZ_SOURCE_COMMIT.
#
# RAZZFAZZ_COMMIT keeps naming THIS checkout: `rzfz upgrade --rollback` does
# `git checkout $RAZZFAZZ_COMMIT` and `rzfz status` compares it against the
# tag on origin — on a mirror box both need the mirror's own sha, which is why
# the source commit is a second key and not a replacement.
#
# Usage: razzfazz_source_commit <repo-root> [fallback]
# Prints the stamp's source_commit when the stamp is present and well-formed;
# otherwise the fallback when one is given (the upgrade passes the commit it
# already resolved, so an offline package does not read a stale HEAD, #272);
# otherwise the checkout's short HEAD. Returns 1 and prints nothing when none
# of the three exists — the caller then simply does not write the key.
RAZZFAZZ_EXPORT_STAMP_FILE="PUBLIC_EXPORT_OF"
razzfazz_source_commit() {
    local root="${1:-.}" fallback="${2:-}" stamp commit
    stamp="${root%/}/${RAZZFAZZ_EXPORT_STAMP_FILE}"
    if [ -f "$stamp" ]; then
        commit=$(grep -m1 '^source_commit=' "$stamp" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
        # Only a sha is a source commit: a malformed stamp must not become the
        # box's identity, it falls through to what git can say.
        if printf '%s' "$commit" | grep -qE '^[0-9a-f]{7,40}$'; then
            printf '%s\n' "$commit"
            return 0
        fi
    fi
    if [ -n "$fallback" ]; then
        printf '%s\n' "$fallback"
        return 0
    fi
    if command -v git >/dev/null 2>&1 && [ -d "${root%/}/.git" ]; then
        commit=$(git -C "$root" rev-parse --short HEAD 2>/dev/null) || commit=""
        if [ -n "$commit" ]; then
            printf '%s\n' "$commit"
            return 0
        fi
    fi
    return 1
}

# ── Is origin's tagged commit the public export of the deployed one? (#2707) ──
# On the public mirror a release tag points at the curated export commit, never
# at the internal commit it was cut from. A box installed from an internal
# checkout and later switched to the public channel records the INTERNAL commit
# as RAZZFAZZ_COMMIT, so a plain sha comparison calls the tag "moved". The
# export's PUBLIC_EXPORT_OF names what it was cut from: `source_commit`, and on
# a notice-fix export (--source) also `release_tag_commit`, the tag's own
# commit. Either one naming the deployed commit means "same release".
#
# The stamp is read from the TAGGED COMMIT's git objects, never from the working
# tree — the checkout may be on a different commit, and a working-tree file can
# be edited by hand.
#
# Usage: razzfazz_export_stamp_names <repo> <commit> <recorded_commit>
# Prints the stamp key that matched (release_tag_commit or source_commit) and
# returns 0. Returns 1 and prints nothing when the commit is not in <repo>, has
# no stamp, the stamp is malformed, or it names a different commit.
razzfazz_export_stamp_names() {
    local repo="${1:-.}" commit="${2:-}" rec="${3:-}" stamp key val
    printf '%s' "$rec" | grep -qE '^[0-9a-f]{7,40}$' || return 1
    printf '%s' "$commit" | grep -qE '^[0-9a-f]{7,40}$' || return 1
    stamp=$(git -C "$repo" show "${commit}:${RAZZFAZZ_EXPORT_STAMP_FILE}" 2>/dev/null) || return 1
    for key in release_tag_commit source_commit; do
        val=$(printf '%s\n' "$stamp" | grep -m1 "^${key}=" | cut -d= -f2- | tr -d '[:space:]')
        printf '%s' "$val" | grep -qE '^[0-9a-f]{7,40}$' || continue
        # Short and full ids both occur (.env holds a short one, the stamp a
        # full one); a match is one being a prefix of the other.
        case "$val" in "$rec"*) printf '%s\n' "$key"; return 0 ;; esac
        case "$rec" in "$val"*) printf '%s\n' "$key"; return 0 ;; esac
    done
    return 1
}

# ── Security-assessment selection (#423) ──────────────────────────────────────
# Pick the assessment that describes the code this box is RUNNING.
#
# `ls -1t` cannot do this: every assessment arrives in one checkout/package
# apply and therefore shares a single mtime, so the "newest" is an arbitrary
# tie-break. On box 0.236 that surfaced an April assessment for an August
# install, and `rzfz security-check` embeds the result in customer-facing output.
#
# Usage: razzfazz_select_assessment <installed_tag|""> <dir> [dir...]
# Prints "<path>\t<exact|cycle|fallback>" and returns 0, or returns 1 when no
# assessment exists at all.
#   exact    the filename tail matches the installed tag.
#   cycle    (task G1) the installed tag is a GA patch (2026.09-ga.3) with no
#            assessment of its own; this is the newest assessment of the SAME
#            YYYY.MM cycle whose patch number is <= the installed one — never
#            one for a later, not-yet-installed patch. Describes closely
#            related code; the caller may say so without the "different code"
#            warning.
#   fallback the newest by CalVer-sorted FILENAME, cycle-agnostic, and the
#            caller MUST tell the operator its findings describe different code.
razzfazz_select_assessment() {
    local tag="${1:-}"; shift
    local d cand base
    tag="${tag#v}"

    if [ -n "$tag" ]; then
        for d in "$@"; do
            [ -d "$d" ] || continue
            for cand in "$d/"*assessment*.md; do
                [ -f "$cand" ] || continue
                base=$(basename "$cand" .md)
                # Exact TAIL match, so v2026.08-ga.1 never matches v2026.08-ga.10.
                case "$base" in
                    *"-v$tag"|*"-$tag") printf '%s\t%s\n' "$cand" exact; return 0 ;;
                esac
            done
        done
    fi

    # ── Same-cycle fallback for a GA patch release (task G1) ────────────────
    # A GA patch (2026.09-ga.3) usually ships with no assessment of its own —
    # only earlier patches of the SAME cycle do (2026.09-ga, 2026.09-ga.2).
    # The global "newest on disk" fallback below can walk past the cycle
    # entirely to an unrelated older cycle, so try the cycle first: the
    # newest assessment of the installed tag's own YYYY.MM cycle whose patch
    # number is <= the installed one. "ga" < "ga.2" < "ga.3", compared
    # numerically (so "ga.10" sorts after "ga.9"), and an assessment for a
    # NEWER, not-yet-installed patch (ga.5 on a ga.4 box) is never picked
    # here — it describes code this box does not run.
    #
    # rc tags are unchanged: this block only fires for an installed "ga" tag,
    # so an rc install falls straight through to the fallback loop below,
    # exactly as before this block existed.
    if [ -n "$tag" ] && [[ "$tag" =~ ^([0-9]{4}\.[0-9]{2})-(ga|rc)\.?([0-9]+)?$ ]]; then
        local cyc_t="${BASH_REMATCH[1]}" chan_t="${BASH_REMATCH[2]}" n_t="${BASH_REMATCH[3]:-0}"
        if [ "$chan_t" = ga ]; then
            local best="" best_n=-1 c_cyc c_n
            for d in "$@"; do
                [ -d "$d" ] || continue
                for cand in "$d/"*assessment*.md; do
                    [ -f "$cand" ] || continue
                    base=$(basename "$cand" .md)
                    if [[ "$base" =~ ([0-9]{4}\.[0-9]{2})-ga\.?([0-9]+)?$ ]]; then
                        c_cyc="${BASH_REMATCH[1]}"; c_n="${BASH_REMATCH[2]:-0}"
                        if [ "$c_cyc" = "$cyc_t" ] && [ "$c_n" -le "$n_t" ] && [ "$c_n" -gt "$best_n" ]; then
                            best="$cand"; best_n="$c_n"
                        fi
                    fi
                done
            done
            if [ -n "$best" ]; then
                printf '%s\t%s\n' "$best" cycle; return 0
            fi
        fi
    fi

    for d in "$@"; do
        [ -d "$d" ] || continue
        # CalVer-aware ordering. Plain `sort -V` gets this wrong twice:
        # `-ga.10` must beat `-ga.9` (numeric, not lexical), and `-rc1` must
        # LOSE to `-ga` / `-ga.N` for the same cycle — but "ga" < "rc"
        # alphabetically, so a release candidate would win the fallback.
        # Build an explicit key: <cycle> <channel-rank> <n>.
        cand=$(ls -1 "$d/"*assessment*.md 2>/dev/null | awk -F/ '{
            base = $NF; sub(/\.md$/, "", base)
            cycle = "0000.00"; rank = 0; n = 0
            # tag-named: ...-v2026.08-ga.10 / ...-2026.06-ga / ...-v2026.08-rc1
            # NB: explicit repeats, not {4}/{2} — mawk/busybox awk do not enable ERE
            # interval expressions by default, so the match silently never fired.
            if (match(base, /[0-9][0-9][0-9][0-9]\.[0-9][0-9]-(ga|rc)/)) {
                tail = substr(base, RSTART)
                split(tail, a, "-")
                cycle = a[1]
                # The counter is separated by a DOT, not a dash: the cycle
                # field is "ga", "ga.7", "rc1" — so split a[2] again on ".".
                # (Splitting only on "-" left a[2]=="ga.10", which failed the
                # == "ga" test and silently ranked every ga.N as unknown.)
                if (a[2] ~ /^rc/)      { rank = 1; n = a[2]; sub(/^rc/, "", n) }
                else if (a[2] ~ /^ga/) { rank = 2; n = a[2]; sub(/^ga\.?/, "", n) }
                if (n == "" || n !~ /^[0-9]+$/) n = 0
            } else if (match(base, /[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/)) {
                # legacy date-named files predate the per-tag scheme: rank them
                # below every tagged assessment rather than interleaving them.
                cycle = "0000.00"; rank = 0; n = 0
            }
            printf "%s %d %05d\t%s\n", cycle, rank, n, $0
        }' | sort | tail -1 | cut -f2-)
        if [ -n "$cand" ] && [ -f "$cand" ]; then
            printf '%s\t%s\n' "$cand" fallback; return 0
        fi
    done
    return 1
}

# ── Orphaned forward-auth provider detection (#459) ───────────────────────────
# A ProxyProvider that exists but is attached to NO outpost makes Authentik
# answer Caddy's forward_auth with 404, and Caddy passes that through — so the
# app's domain returns 404 while /healthz is 200, the container is healthy, and
# `rzfz status` is happy. Box 0.78 sat like that for four days; a test box
# hit the same thing on 2026-05-21 with 14 providers unbound
# (core/init-authentik.sh:100-112).
#
# The REPAIR has existed since then and is correct — apply-policy-bindings.py
# reconciles idempotently on the init fast-path. What was missing is DETECTION:
# nothing notices, because the repair only runs at init and `docker compose up -d`
# is not init. So a box that never re-runs init stays 404 indefinitely with every
# health signal green.
#
# This is the pure verdict half, kept out of status.sh so it can be tested
# without a stack: the caller supplies the raw "<name>\t<attached>" listing that
# the Django snippet produces inside authentik-worker, and this decides.
#
# Usage:  razzfazz_orphaned_providers <<< "$listing"
# Input:  one provider per line, "<name>\t<0|1>" (1 = attached to an outpost)
# Output: the names of orphaned providers, one per line
# Return: 0 when none are orphaned, 1 when at least one is
# Extract the provider listing from `ak shell -c` output (#459/#464 review).
#
# `ak shell` is a SHELL: banners, deprecation warnings and startup noise share
# stdout with the snippet's own writes. `razzfazz_orphaned_providers` treats any
# line without an attached-flag as an orphan, so unfiltered noise becomes a
# phantom 404 alarm. The snippet therefore tags each real line "RZFZ-PROVIDER\t"
# and this drops everything else.
#
# Usage:  razzfazz_ak_provider_lines <<< "$raw"
# Input:  raw stdout from the probe
# Output: "<name>\t<0|1>" lines only, sentinel stripped
razzfazz_ak_provider_lines() {
    # $'...' so the TAB is explicit: a literal tab here would be invisible in the
    # source and silently destroyed by any editor that expands whitespace.
    local sentinel=$'RZFZ-PROVIDER\t' line
    while IFS= read -r line; do
        case "$line" in
            "$sentinel"*) printf '%s\n' "${line#"$sentinel"}" ;;
        esac
    done
}

# razzfazz_orphaned_stack_volumes  (#1667)
#
# A volume that no container mounts is invisible: `rzfz status` looks at
# containers, and nothing looks at storage. Measured on 0.91 — 72 GB of model
# weights in a volume from a compose project that no longer exists:
#
#     razzfazz-stack_llm-node-models   4 links   83.44 GB   <- mounted
#     rzfz-node_llm-node-models        0 links   77.52 GB   <- nobody
#     llm-node-models                  0 links    0.27 GB   <- nobody
#
# A volume inherits the project name when it is created; change the project and
# a NEW volume appears while the old one stays behind, with everything in it.
# The container move was clean; the volumes did not come along.
#
# WHICH orphans are ours is decided by the listing itself, not by a hand-kept
# list of names: a 0-link volume counts when some MOUNTED volume carries the
# same base name under a different project prefix. That is precisely the shape
# this defect has — "a volume of a kind we are using, from a project that is
# gone" — and it needs no maintenance when a module adds a volume. A hand-kept
# list would go stale the first time someone adds one, and go stale silently.
#
# This is the pure verdict half, kept out of status.sh so it can be tested
# without a box.
#
# Usage:  razzfazz_orphaned_stack_volumes <<< "$listing"
# Input:  one volume per line, "<name>\t<links>\t<size>" (docker system df -v)
# Output: "<name>\t<size>" for each orphan, one per line
# Return: 0 when none are orphaned, 1 when at least one is
# razzfazz_backup_only_volumes  (#1709)
#
# The orphan report above answers "nobody mounts this". There is a second
# shape it cannot see, and it is bigger: a volume that IS mounted — but only by
# the backup plumbing, which does not use its contents.
#
# Measured on 0.79, a box that has the cutover behind it (profiles
# `llm-manager,llm-registry,llm-worker-agent`, no GPUStack container left):
#
#     razzfazz-stack_gpustack-data   2 links   44.28 GB
#         /backup-service              (core/compose.yml:814, `:ro`)
#         /razzfazz-backup-management  (core/compose.yml:1090, restore target)
#
# Both doors are shut. `docker volume prune` will not touch it — it is not
# dangling. `razzfazz_orphaned_stack_volumes` will not report it — it has two
# links. So 46 GB of dead state sits on the box and every tool we ship says the
# box is clean.
#
# The rule is read off the LISTING, not off a hand-kept list of volume names —
# the same reason as above: a name list goes stale the first time someone adds a
# volume, and goes stale silently. A volume qualifies when every container
# holding it is infrastructure: the backup service and the backup/restore
# manager. Those two mount volumes in order to copy them, never to use them —
# so "held only by them" means "no service on this box uses this data".
#
# It is a REPORT, not a cleanup. The backup `:ro` mount is deliberately left in
# place: `BACKUP_EXCLUDE_REGEXP` excludes this volume's contents by DEFAULT, and
# a default is not a promise — an operator who removes the exclusion wants that
# mount back. Removing a mount because today's default makes it idle would break
# that operator silently. And a status command that deletes volumes is a status
# command nobody dares to run.
#
# Usage:  razzfazz_backup_only_volumes <<< "$listing"
# Input:  "<name>\t<links>\t<size>\t<holders>" per line, holders
#         comma-separated container names (empty = unknown, never guessed)
# Output: "<name>\t<size>\t<holders>" per line
# Return: 0 when none qualify, 1 when at least one does
#
#: The containers that mount a volume in order to COPY it. Kept here rather than
#: in status.sh so the rule and its list travel together.
RAZZFAZZ_BACKUP_ONLY_HOLDERS="backup-service razzfazz-backup-management"

razzfazz_backup_only_volumes() {
    local line name links size holders h known found=1
    while IFS=$'\t' read -r name links size holders; do
        name="${name#"${name%%[![:space:]]*}"}"
        name="${name%"${name##*[![:space:]]}"}"
        [ -n "$name" ] || continue
        case "$name" in \#*) continue ;; esac
        # No holders recorded is NOT "held by nobody" — that is the orphan
        # report's job, and guessing here would report a live volume as dead.
        [ -n "$holders" ] || continue
        case "$links" in ''|*[!0-9]*) continue ;; esac
        [ "$links" -gt 0 ] || continue

        # Commas to spaces rather than an `IFS=,` around the loop: this body
        # runs inside a `while IFS=$'\t' read`, and touching IFS here changes
        # how the NEXT line is split — the kind of action-at-a-distance that
        # makes a rule work on the first row and quietly not on the rest.
        known=1
        for h in ${holders//,/ }; do
            h="${h#/}"
            [ -n "$h" ] || continue
            case " $RAZZFAZZ_BACKUP_ONLY_HOLDERS " in
                *" $h "*) : ;;
                *) known=0; break ;;
            esac
        done
        if [ "$known" -eq 1 ]; then
            printf '%s\t%s\t%s\n' "$name" "${size:-unknown}" "$holders"
            found=0
        fi
    done
    # Same convention as razzfazz_orphaned_stack_volumes above: 1 means "there
    # is something to report". Callers use `|| true`.
    [ "$found" -eq 0 ] && return 1
    return 0
}

# razzfazz_failed_runner_upgrades  (#1677)
#
# A runner switch that fails leaves the worker DRAINED and its engine stopped.
# That is a deliberate trade — `modules/llm/manager/app/api/runner_upgrade.py`
# argues it in its own header: putting traffic back on a node whose runner state
# is unknown is worse, so the failure waits for an operator decision (rollback
# or investigate).
#
# The trade only works if the operator LEARNS about it. Today only the manager's
# API knows: `rzfz status` says nothing, and a drained node with no engine looks
# from the outside like a quiet box. Measured on 0.79 (#1677): after a failed
# switch the worker sat `draining`, `qwen3.6` was `pending` with no instance and
# no engine container ran — and every display we ship was silent.
#
# So the state row the manager keeps for exactly this purpose is put in front of
# the operator, together with the two things it decides: that the node is
# serving nothing, and that `rollback` is the way back (measured to work:
# `{"state":"rolled_back","relaunched":1}`).
#
# Reported, never repaired — the same rule as the two reports above. Rolling
# back automatically is the decision this function deliberately does NOT take.
#
# Usage:  razzfazz_failed_runner_upgrades <<< "$rows"
# Input:  "<worker>\t<state>\t<upgrade_id>\t<image>\t<error>" per line
# Output: "<worker>\t<upgrade_id>\t<image>\t<reason>" per line
# Return: 0 when none need an operator, 1 when at least one does
razzfazz_failed_runner_upgrades() {
    local worker state uid image err reason found=1
    while IFS=$'\t' read -r worker state uid image err; do
        worker="${worker#"${worker%%[![:space:]]*}"}"
        [ -n "$worker" ] || continue
        case "$worker" in \#*) continue ;; esac
        # Only `failed` needs a person. `deploying`/`relaunching` are a switch in
        # progress and `done`/`rolled_back` are finished states — reporting them
        # would turn this into a running commentary nobody reads, which is how a
        # report stops being read before it stops being right.
        [ "$state" = "failed" ] || continue
        reason="${err:-no error recorded}"
        # NOT flattened here, and that is not an oversight: `read` has already
        # split on the newline before this body runs, so a substitution here can
        # never see one. The line that used to stand here looked like protection
        # and was unreachable — a reviewer measured it (#1677):
        #
        #     read lieferte: worker=[worker-a]           state=[failed]  err=[pull failed:]
        #     read lieferte: worker=[  manifest unknown] state=[]        err=[]
        #
        # The continuation line becomes its own record, fails the `failed`
        # filter and disappears, so the operator reads "pull failed:" and the
        # reason — the entire point of the report — is gone.
        #
        # Flattening therefore belongs to the CALLER, before the record is
        # written: cli/status.sh does it in the python that serialises the row.
        # Dead protection is worse than none, because it is believed.
        printf '%s\t%s\t%s\t%s\n' "$worker" "${uid:-unknown}" "${image:-unknown}" "$reason"
        found=0
    done
    [ "$found" -eq 0 ] && return 1
    return 0
}

# razzfazz_engine_expectation  (#3293)
#
# What the RUNNING llm-worker-agent launches an engine with right now: its
# hardware, the runner image it would start (bare and as the reference its
# registry serves), the user, capabilities, root filesystem and pids limit
# (`engine_hardening`), and the worker name it registers under. JSON on stdout;
# non-zero when the agent did not answer.
#
# Asked of the agent, not derived here: the runner pin, the operator's image
# override, the GPU group ids (an AMD engine without them stays root by design)
# and the `LLM_ENGINE_HARDENING=off` hatch all live in the agent's environment,
# and the agent's own code is what applies them at the next launch. A copy of
# that logic in the shell would be a second authority that drifts.
razzfazz_engine_expectation() {
    docker exec llm-worker-agent python3 -c '
import json, os
from app.drivers import select_driver
from app.drivers.base import engine_hardening
from app.drivers.images import ref_from_allowed_registry, registry_ref_for
hw = os.environ.get("HARDWARE") or "amd"
image = select_driver(hw).image
h = engine_hardening(hw)
# #3403: the experimental runner this agent launches for a deployment on that
# channel — also a runner of this release. An agent without the channel says none.
try:
    from app.drivers.images import channel_image
    experimental = channel_image(hw, "experimental")
except Exception:
    experimental = None
print(json.dumps({
    "hardware": hw,
    "worker": os.environ.get("LLM_WORKER_NAME") or os.environ.get("HOSTNAME") or "node",
    "image": image,
    "experimental_image": experimental,
    "ref": image if ref_from_allowed_registry(image) else registry_ref_for(image),
    "user": h["user"], "cap_drop": h["cap_drop"], "read_only": h["read_only"],
    "pids_limit": h["pids_limit"]}))
' 2>/dev/null
}

# razzfazz_engine_runtime_verdict <expectation-json>  (#3293)
#
# Stdin: `docker inspect` of the engine containers (a JSON array). One line per
# engine:
#   OK<TAB><name><TAB><image name:tag>
#   STALE<TAB><name><TAB><image name:tag><TAB><reasons, "; "-joined>
# compared against the expectation above. The image is compared by name and
# tag (`llama-runner:b12110-vulkan`): registry host and digest differ between
# the locally built runner and its registry twin, which are the same build.
# Hardening is judged only where the agent would apply it, so a root AMD engine
# without GPU group ids, or every engine under `LLM_ENGINE_HARDENING=off`, is
# measured against what the agent itself would launch. Non-zero on unreadable
# input. #3403: an engine on the agent's EXPERIMENTAL runner (the expectation's
# `experimental_image`) is on this release's runner too — a deployment on that
# channel runs there on purpose, and switching it to the stable runner is
# exactly what must not happen (its model may not load there).
razzfazz_engine_runtime_verdict() {
    EXPECT_JSON="$1" python3 -c '
import json, os, sys

def leaf(ref):
    ref = str(ref or "").split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    return last if ":" in last else last + ":latest"

ROOT = {"", "0", "root"}

def is_root(user):
    u = str(user or "").strip()
    return u.split(":", 1)[0] in ROOT

try:
    exp = json.loads(os.environ["EXPECT_JSON"])
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(2)
if not isinstance(rows, list):
    sys.exit(2)
want = leaf(exp.get("image"))
release = {want} | ({leaf(exp["experimental_image"])} if exp.get("experimental_image") else set())
for c in rows:
    cfg = c.get("Config") or {}
    host = c.get("HostConfig") or {}
    name = str(c.get("Name") or "?").lstrip("/")
    img = leaf(cfg.get("Image"))
    why = []
    if img not in release:
        why.append("runner %s, this release launches %s" % (img, want))
    if exp.get("user") and is_root(cfg.get("User")):
        why.append("runs as root")
    drop = [str(x).upper() for x in (host.get("CapDrop") or [])]
    if exp.get("cap_drop") and not ({"ALL", "CAP_ALL"} & set(drop)):
        why.append("keeps its capabilities")
    if exp.get("read_only") and not host.get("ReadonlyRootfs"):
        why.append("writable root filesystem")
    pids = host.get("PidsLimit")
    if exp.get("pids_limit") and not (isinstance(pids, int) and pids > 0):
        why.append("no pids limit")
    if why:
        print("STALE\t%s\t%s\t%s" % (name, img, "; ".join(why)))
    else:
        print("OK\t%s\t%s" % (name, img))
'
}

#: The one name every consumer dials since #1445/#1601. It is a NETWORK ALIAS on
#: the manager, not a container name — `modules/llm/manager/compose.yml` sets it
#: on all three networks. Kept here so the reporter and its guard read the same
#: string; a second literal would drift the first time the name changes.
RAZZFAZZ_CANONICAL_LLM_ALIAS="llm"

# razzfazz_missing_canonical_alias  (#979)
#
# Does the running manager answer to the backend-invariant name?
#
# Measured on 0.91: the alias has been in the compose file since #1445, and the
# container there predates it — never recreated, so the network carries only
# `llm-manager`, and `openwebui` resolving "llm" gets NXDOMAIN. Everything looks
# healthy: `docker compose ps` says healthy, `/health` answers, the manager runs.
# Only the NAME is missing.
#
# Harmless while the consumers are wired to `llm-manager` (0.91 is internally
# consistent). Fatal the moment an upgrade repoints them at the canonical
# address without recreating the container — every consumer then holds a name
# no DNS knows. That is the #976 class with the sign flipped: not the consumer
# pointing at the wrong thing, but the right thing not existing.
#
# Input:  one line per network, "<network>\t<space-separated aliases>"
# Output: the networks that lack the canonical alias, one per line
# Return: 0 when every network carries it, 1 when at least one does not
razzfazz_missing_canonical_alias() {
    local want="${RAZZFAZZ_CANONICAL_LLM_ALIAS:-llm}"
    local net aliases found=1
    # `|| [ -n "$net" ]`: a final line WITHOUT a trailing newline is otherwise
    # dropped — `read` returns non-zero on it and the loop ends before the body
    # runs. Measured on 0.91: `$(docker inspect …)` strips the trailing newline,
    # so the LAST network silently fell out of the verdict and the reporter
    # named two of three. A guard that under-reports is worse than none.
    while IFS=$'\t' read -r net aliases || [ -n "$net" ]; do
        net="${net#"${net%%[![:space:]]*}"}"
        [ -n "$net" ] || continue
        case "$net" in \#*) continue ;; esac
        case " ${aliases} " in
            *" ${want} "*) : ;;
            *) printf '%s\n' "$net"; found=0 ;;
        esac
    done
    # An empty reading yields no rows and therefore no verdict — which is
    # correct here and NOT this function's job to dress up. "We could not read
    # the networks" is a different sentence from "every network is fine", and
    # the CALLER says it (cli/status.sh warns before ever getting here). A
    # `seen` flag was in this spot until a mutation showed it changed nothing:
    # empty input returns 0 with or without it. Dead defence is worse than
    # none, because it is believed (#1751).
    [ "$found" -eq 0 ] && return 1
    return 0
}

# razzfazz_stuck_deployments  (#1713)
#
# A deployment row that is not running is invisible: `rzfz status` says nothing
# about deployments at all, and post-install only warns INSIDE its own wait
# window — a later look at the box learns nothing. Measured on 0.79:
#
#     qwen3.6           active   hf_repo unsloth/Qwen3.6-35B-A3B-GGUF
#     qwen3-embedding   pending  no weight source
#     qwen3-reranker    pending  no weight source
#     nomic-embed-text  pending  no weight source
#
# Consequence, also measured: `/v1/models` served only `qwen3.6`,
# `/v1/embeddings` answered 400, and cognee — healthy, green health gate — had
# no embedding target. Chat worked, RAG and reranking were dead, and every
# display we ship was green. The worker had 47.2 GB free; it was never capacity.
#
# A row with NO weight source (neither hf_repo nor files) is worse than slow:
# `POST /api/deployments/<id>/start` refuses it with 409 "no weight source
# recorded", so it can only be healed by deploying again from the catalog. That
# distinction is in the output, because it decides what the operator does next.
#
# Reported, never repaired: a status command that starts deploying models is a
# status command nobody dares run.
#
# This is the pure verdict half, kept out of status.sh so it can be tested
# without a box.
#
# Usage:  razzfazz_stuck_deployments <<< "$rows"
# Input:  one deployment per line, "<name>\t<status>\t<yes|no has source>"
# Output: "<name>\t<status>\t<what the operator can do>" per stuck row
# Return: 0 when none are stuck, 1 when at least one is
razzfazz_stuck_deployments() {
    local name status ready replicas instances err found=1 why
    #: States that mean "on its way" — a box mid-deploy is not a broken box.
    #: `pending` is deliberately NOT here: with ORCH_RECONCILE=observe (the
    #: shipped default) nothing places a pending row on its own, so pending is
    #: where a deployment STOPS, not where it passes through.
    local in_motion=" starting loading pulling downloading "
    while IFS=$'\t' read -r name status ready replicas instances err; do
        name="${name#"${name%%[![:space:]]*}"}"
        name="${name%"${name##*[![:space:]]}"}"
        [ -n "$name" ] || continue
        case "$name" in \#*) continue ;; esac
        [ "$status" = "active" ] && continue
        case "$in_motion" in *" $status "*) continue ;; esac
        # ZERO instances is the observable form of "this was never placed" —
        # measured on 0.79, where three rows had no instance at all and
        # `POST /api/deployments/<id>/start` refused them with 409 "no weight
        # source recorded". The list endpoint does NOT expose the weight source
        # (its fields are status/health/ready_instances/replicas/instances/
        # last_error), so the report says what it can SEE and not what it would
        # have to guess.
        if [ "${instances:-0}" = "0" ]; then
            why="no instance was ever placed — deploy it again from the catalog"
        else
            why="${ready:-0}/${replicas:-1} instance(s) ready — read its engine log"
        fi
        [ -n "$err" ] && [ "$err" != "None" ] && [ "$err" != "null" ] && why="$why: $err"
        printf '%s\t%s\t%s\n' "$name" "$status" "$why"
        found=0
    done
    [ "$found" -eq 0 ] && return 1
    return 0
}

razzfazz_orphaned_stack_volumes() {
    local line name links size
    local -a names=() sizes=() idle=()
    local -a bases=()
    while IFS=$'\t' read -r name links size; do
        name="${name#"${name%%[![:space:]]*}"}"
        name="${name%"${name##*[![:space:]]}"}"
        [ -n "$name" ] || continue
        case "$name" in \#*) continue ;; esac
        # A non-numeric link count is not a zero link count. `docker system df`
        # prints "N/A" for a volume it cannot size, and treating that as 0 would
        # report a mounted volume as an orphan — the report only works if it is
        # never wrong in that direction.
        case "$links" in
            ''|*[!0-9]*) continue ;;
        esac
        if [ "$links" -gt 0 ]; then
            # Two base names per mounted volume, and no more. The full name
            # covers the mounted-side-has-no-prefix case (`llm-node-models`
            # mounted, `rzfz-node_llm-node-models` orphaned); the part after the
            # last underscore covers the measured one. Every intermediate
            # boundary was tried and thrown away: matching is by suffix, so a
            # name that ends in `_<middle>_<last>` already ends in `_<last>` —
            # the extra bases changed no verdict any test could construct, and
            # code nothing executes is exactly what this repo keeps paying for.
            bases+=("$name")
            [ "${name#*_}" != "$name" ] && bases+=("${name##*_}")
        else
            names+=("$name")
            sizes+=("${size:-unknown}")
        fi
    done

    local found=1 i b n
    for i in "${!names[@]}"; do
        n="${names[$i]}"
        for b in "${bases[@]}"; do
            # `= "$b"` catches the un-prefixed leftover; `_$b` the re-projected
            # one. Never flag the mounted volume itself.
            if [ "$n" = "$b" ] || [ "${n%"_$b"}" != "$n" ]; then
                printf '%s\t%s\n' "$n" "${sizes[$i]}"
                found=0
                break
            fi
        done
    done
    [ "$found" -eq 0 ] && return 1
    return 0
}

razzfazz_orphaned_providers() {
    local line name attached _host found=1
    # `_host` is read but unused HERE: without a third variable, `read`
    # assigns the rest of the line to `attached`, so "1\thttps://…" != "1"
    # and EVERY provider — attached ones included — is reported orphaned.
    # The host is rendered by razzfazz_provider_host_for, not by this verdict.
    while IFS=$'\t' read -r name attached _host; do
        # Trim: a whitespace-only line is not a provider. `[ -n "   " ]` is TRUE,
        # so an untrimmed check reports blank lines as orphans and the report
        # cries wolf — which costs it exactly the credibility it needs.
        name="${name#"${name%%[![:space:]]*}"}"
        name="${name%"${name##*[![:space:]]}"}"
        [ -n "$name" ] || continue
        case "$name" in \#*) continue ;; esac
        if [ "${attached:-0}" != "1" ]; then
            printf '%s\n' "$name"
            found=0
        fi
    done
    [ "$found" -eq 0 ] && return 1
    return 0
}

# Look up a provider's REAL host from the probe listing (#527).
#
# Authentik knows the host; we were guessing it. The guess is wrong whenever the
# host differs from the display name — measured on 0.78: provider
# "…for Stirling-PDF" derives `stirling-pdf.<domain>` but is served at
# `pdf.<domain>`, so the operator was sent to a hostname that does not exist.
# And when MAIN_DOMAIN is not in scope (status.sh keeps it in a LOCAL), the
# derivation degrades to printing the provider name, i.e. no domain at all.
#
# Usage:  razzfazz_provider_host_for "<name>" <<< "$listing"
# Input:  "<name>\t<0|1>\t<external_host>" lines
# Output: the external_host for that provider, empty if absent/blank
razzfazz_provider_host_for() {
    local want="$1" name attached host
    while IFS=$'\t' read -r name attached host; do
        [ "$name" = "$want" ] || continue
        printf '%s\n' "$host"
        return 0
    done
    return 1
}

# Map a ProxyProvider name to the domain a user would see 404 on, so the report
# names the thing the operator can actually check in a browser. Falls back to the
# provider name when the convention does not match — an unrecognised name is
# still worth reporting, just less precisely.
razzfazz_provider_domain() {
    local name="$1" domain="${2:-${MAIN_DOMAIN:-}}" slug=""
    case "$name" in
        *"Forward Auth Provider for "*) slug="${name##*Forward Auth Provider for }" ;;
        *) printf '%s\n' "$name"; return 0 ;;
    esac
    slug="$(printf '%s' "$slug" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' \
            | sed 's/-\{1,\}/-/g; s/^-//; s/-$//')"
    if [ -n "$slug" ] && [ -n "$domain" ]; then
        printf '%s.%s\n' "$slug" "$domain"
    else
        printf '%s\n' "$name"
    fi
}

# --- Komodo hardening (#539) -------------------------------------------------
# Komodo's `Server.auto_prune` defaults to TRUE. Komodo Core then has
# komodo-periphery run `docker image prune -a -f` once a day at 00:00 UTC, over
# the raw /var/run/docker.sock (it does not pass through docker-socket-proxy, and
# it logs nothing on success). `prune -a` deletes every image no container
# references, so the victims are precisely:
#   * DISABLED modules — their custom-built images, which on an offline or
#     air-gapped box cannot be rebuilt or re-pulled. The module can then never be
#     enabled again.
#   * modules that only half-started (#538).
# Measured on a customer box 2026-08-20 02:00:05 CEST: 29 image deletes in one burst.
# Nothing in this repo ever set the flag — we create the server through
# KOMODO_FIRST_SERVER and inherit Komodo's own default. Disk pressure is already
# handled deliberately elsewhere (backup retention, ClickHouse TTL #292, the disk
# guard); a blind nightly prune is not something we want to ship.

# #2577/#2682: curl INSIDE a container with credential headers that reach no
# argv. The host-side `docker exec` argv names only the variable
# (`-e RZ_CURL_HEADERS`); inside, the headers are written to a 0600 temp file
# that curl reads with -K, so the container's own `ps` shows `-K /tmp/x`, not a
# token either. Call: RZ_CURL_HEADERS=$'Authorization: Bearer '"$tok" \
#   rz_ctr_curl <container> <curl args...>   (body on stdin; </dev/null if none).
# A curl -K config line for a bearer header (print it into `-K <(…)`; the
# value is an argument of this function, never in a curl argv).
rz_curl_bearer_cfg() { printf 'header = "Authorization: Bearer %s"\n' "$1"; }

rz_ctr_curl() {
    local ctr="$1"; shift
    docker exec -i -e RZ_CURL_HEADERS "$ctr" sh -c \
        'umask 077; cfg=$(mktemp) || exit 1; trap "rm -f \"$cfg\"" EXIT
         printf "%s\n" "$RZ_CURL_HEADERS" | while IFS= read -r h; do
             [ -n "$h" ] && printf "header = \"%s\"\n" "$h"
         done > "$cfg"
         curl -K "$cfg" "$@"' _ "$@"
}

# #2566: every Komodo request goes through ONE curl call. Its headers come from
# a curl config read from a PIPE (process substitution: curl's argv shows only
# /dev/fd/N, nothing touches the disk, so neither a failing curl nor a signal
# can leave a credential behind), and its body goes over STDIN. Neither the
# admin password, the stack's API key pair nor a session token is in curl's
# argv, where ps shows it to every local user. Args: <path> <body> [header...]
_razzfazz_komodo_curl() {
    local path="$1" body="$2"
    shift 2
    printf '%s' "$body" | curl -s --max-time 20 -X POST \
        --config <(_razzfazz_komodo_curl_config "$@") \
        "${KOMODO_BASE_URL}${path}" --data @-
}

# The curl config for _razzfazz_komodo_curl (printf is a builtin: no argv).
_razzfazz_komodo_curl_config() {
    local h
    printf 'header = "Content-Type: application/json"\n'
    for h in "$@"; do
        h="${h//$'\n'/}"; h="${h//$'\r'/}"; h="${h//\\/\\\\}"; h="${h//\"/\\\"}"
        printf 'header = "%s"\n' "$h"
    done
}

# POST a JSON body to Komodo, optionally with a session token (JWT).
_razzfazz_komodo_post() {
    local path="$1" body="$2" jwt="${3:-}"
    if [ -n "$jwt" ]; then
        _razzfazz_komodo_curl "$path" "$body" "Authorization: Bearer ${jwt}"
    else
        _razzfazz_komodo_curl "$path" "$body"
    fi
}

# --- #2566: Komodo access that survives a password changed in the UI ----------
# #539 logged in with .env's KOMODO_INIT_ADMIN_PASSWORD. After an operator
# changed the admin password in the Komodo UI that login failed for good and
# auto_prune stayed ON (a fleet master, after the 2026.09-ga.4 hop). Order: the
# stack's API key (minted by razzfazz_komodo_ensure_api_key while the .env
# password still works), then the login, then - for the prune flag only -
# Komodo's own store. Endpoint names from the Komodo 2.3.3 source (auth routes
# are mogh_auth_server's /auth/manage/{variant}); E26 Step 0 measures them.
RZFZ_KOMODO_CREATE_KEY_PATH="/auth/manage/CreateApiKey"
# _PW_, not _PASSWORD_: publish-public.sh's secret gate reads a long value
# after a *PASSWORD*= name as a leaked password.
RZFZ_KOMODO_UPDATE_PW_PATH="/auth/manage/UpdatePassword"

# The JWT of a local-auth login with KOMODO_INIT_ADMIN_USERNAME/_PASSWORD, or
# nothing. The body is built in python from the environment, so a password with
# quotes or backslashes is escaped and never lands in argv.
_razzfazz_komodo_login_jwt() {
    local body
    [ -n "${KOMODO_INIT_ADMIN_PASSWORD:-}" ] || return 0
    body="$(RZFZ_K_USER="${KOMODO_INIT_ADMIN_USERNAME:-admin}" RZFZ_K_PASS="$KOMODO_INIT_ADMIN_PASSWORD" python3 -c 'import json, os
print(json.dumps({"username": os.environ["RZFZ_K_USER"], "password": os.environ["RZFZ_K_PASS"]}))')" || return 0
    # NOTE the path. A POST to plain /auth returns 405 (allow: GET,HEAD) — that is
    # the SPA fallback route, not the API.
    _razzfazz_komodo_post /auth/login/LoginLocalUser "$body" | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
d = d if isinstance(d, dict) else {}
print((d.get("data") or {}).get("jwt") or d.get("jwt") or "")' 2>/dev/null
}

# POST with the auth _razzfazz_komodo_auth chose: the key pair, else the JWT.
_razzfazz_komodo_call() {
    local path="$1" body="$2"
    if [ -n "${_RZFZ_K_KEY:-}" ]; then
        _razzfazz_komodo_curl "$path" "$body" \
            "X-Api-Key: ${_RZFZ_K_KEY}" "X-Api-Secret: ${_RZFZ_K_SECRET:-}"
    else
        _razzfazz_komodo_post "$path" "$body" "${_RZFZ_K_JWT:-}"
    fi
}

_razzfazz_komodo_server_ids() {
    _razzfazz_komodo_call /read/ListServers '{}' | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = []
for s in (d if isinstance(d, list) else []):
    if isinstance(s, dict) and s.get("id"):
        print(s["id"])' 2>/dev/null
}

_razzfazz_komodo_server_auto_prune() {   # <id> → true | false | (empty)
    _razzfazz_komodo_call /read/GetServer "{\"server\":\"$1\"}" | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
v = ((d if isinstance(d, dict) else {}).get("config") or {}).get("auto_prune")
print(json.dumps(v) if isinstance(v, bool) else "")' 2>/dev/null
}

# Sets _RZFZ_K_KEY/_RZFZ_K_SECRET or _RZFZ_K_JWT. Callers declare
# `local KOMODO_BASE_URL _RZFZ_K_KEY _RZFZ_K_SECRET _RZFZ_K_JWT` first (bash's
# dynamic scope keeps them the caller's). 0 = authenticated, 1 = neither works.
# Is the key pair in _RZFZ_K_KEY/_RZFZ_K_SECRET accepted? Prints ok (ListServers
# answered a JSON list, empty or not), refused (Komodo answered something else,
# e.g. its error object) or unknown (no answer: Komodo down or unreachable).
_razzfazz_komodo_key_state() {
    _razzfazz_komodo_call /read/ListServers '{}' | python3 -c 'import json, sys
raw = sys.stdin.read()
if not raw.strip():
    print("unknown"); sys.exit(0)
try:
    d = json.loads(raw)
except Exception:
    print("refused"); sys.exit(0)
print("ok" if isinstance(d, list) else "refused")' 2>/dev/null || echo unknown
}

_razzfazz_komodo_auth() {
    KOMODO_BASE_URL="${KOMODO_URL:-http://127.0.0.1:${KOMODO_PORT:-8180}}"
    _RZFZ_K_KEY=""; _RZFZ_K_SECRET=""; _RZFZ_K_JWT=""
    if [ -n "${KOMODO_API_KEY:-}" ] && [ -n "${KOMODO_API_SECRET:-}" ]; then
        _RZFZ_K_KEY="$KOMODO_API_KEY"; _RZFZ_K_SECRET="$KOMODO_API_SECRET"
        if [ "$(_razzfazz_komodo_key_state)" = ok ]; then
            echo "komodo: authenticated with the stack's API key" >&2
            return 0
        fi
        echo "komodo: the API key in .env was refused — trying the admin login" >&2
        _RZFZ_K_KEY=""; _RZFZ_K_SECRET=""
    fi
    _RZFZ_K_JWT="$(_razzfazz_komodo_login_jwt)"
    [ -n "$_RZFZ_K_JWT" ] && return 0
    echo "komodo: local-auth login failed (the admin password in .env is not Komodo's)" >&2
    return 1
}

# #2566: Komodo's own store — FerretDB over postgres-komodo, collection
# komodo.Server. The password reaches docker through its environment
# (`-e PGPASSWORD` without a value), never argv. SQL on stdin.
_razzfazz_komodo_store_sql() {
    PGPASSWORD="${POSTGRES_PASSWORD:-}" docker exec -i -e PGPASSWORD postgres-komodo \
        psql -U "${POSTGRES_USER:-docker}" -d postgres -tA -v ON_ERROR_STOP=1
}

# Prints the number of Komodo servers whose stored auto_prune is true; rc 1 when
# the store cannot be read or the answer has no count.
razzfazz_komodo_store_auto_prune_count() {
    local out
    command -v docker >/dev/null 2>&1 || return 1
    out=$(printf '%s\n' "SELECT documentdb_api.count_query('komodo', '{\"count\": \"Server\", \"query\": {\"config.auto_prune\": true}}');" \
          | _razzfazz_komodo_store_sql 2>/dev/null) || return 1
    printf '%s' "$out" | python3 -c 'import re, sys
m = re.search(r"\"n\"\s*:\s*(?:\{\s*\"\$number\w+\"\s*:\s*)?\"?(\d+)", sys.stdin.read())
sys.exit(1) if not m else print(m.group(1))'
}

# The update the operator applied by hand on the fleet master (2026-09-29), for
# every server; komodo-core re-reads its servers after a restart. Believed only
# once the store is re-counted.
razzfazz_komodo_ferretdb_disable_auto_prune() {
    local n
    command -v docker >/dev/null 2>&1 || { echo "komodo: no docker — Komodo's store cannot be reached" >&2; return 1; }
    printf '%s\n' "SELECT documentdb_api.update('komodo', '{\"update\": \"Server\", \"updates\": [{\"q\": {}, \"u\": {\"\$set\": {\"config.auto_prune\": false}}, \"multi\": true}]}');" \
        | _razzfazz_komodo_store_sql >/dev/null 2>&1 \
        || { echo "komodo: the store update failed — auto_prune left unchanged" >&2; return 1; }
    docker restart komodo-core >/dev/null 2>&1 || true
    n=$(razzfazz_komodo_store_auto_prune_count) \
        || { echo "komodo: the store was updated, but the flag cannot be re-read" >&2; return 1; }
    if [ "$n" = "0" ]; then
        echo "  komodo: auto_prune=false on every server (written to Komodo's store; the API refused the stack's credentials)"
        return 0
    fi
    echo "  komodo: ${n} server(s) still have auto_prune=true after the store update" >&2
    return 1
}

# #2566: the stack's own API key, minted once while the .env admin password
# still logs in (init's post-install on a fresh box). Idempotent: a key in
# <envfile> that Komodo accepts (or that cannot be checked because Komodo does
# not answer) is kept; one Komodo REFUSES (revoked in the UI) is replaced, if
# the .env password still logs in. Nothing is minted when <envfile> is not
# writable (no orphan keys in Komodo). Written with update_env_value (same
# inode, so owner and mode stay — .env is 0600 — and the staging copy is
# private; the value does pass through sed's argv for that moment, like every
# .env writer of this repo).
# 0 = a working (or uncheckable) key is in <envfile>; 1 = none could be minted.
razzfazz_komodo_ensure_api_key() {
    local env_file="$1" KOMODO_BASE_URL _RZFZ_K_KEY="" _RZFZ_K_SECRET="" _RZFZ_K_JWT="" pair key secret
    [ -f "$env_file" ] || { echo "komodo: ${env_file} not found — no API key minted" >&2; return 1; }
    KOMODO_BASE_URL="${KOMODO_URL:-http://127.0.0.1:${KOMODO_PORT:-8180}}"
    _RZFZ_K_KEY="$(read_env_value "$env_file" KOMODO_API_KEY)"
    _RZFZ_K_SECRET="$(read_env_value "$env_file" KOMODO_API_SECRET)"
    if [ -n "$_RZFZ_K_KEY" ] && [ -n "$_RZFZ_K_SECRET" ]; then
        [ "$(_razzfazz_komodo_key_state)" = refused ] || return 0
        echo "komodo: the API key in .env is refused by Komodo (revoked?) — minting a new one" >&2
    fi
    _RZFZ_K_KEY=""; _RZFZ_K_SECRET=""
    [ -w "$env_file" ] || { echo "komodo: ${env_file} is not writable — no API key minted" >&2; return 1; }
    _RZFZ_K_JWT="$(_razzfazz_komodo_login_jwt)"
    if [ -z "$_RZFZ_K_JWT" ]; then
        echo "komodo: cannot mint the stack's API key — the admin password in .env does not log in" >&2
        return 1
    fi
    pair=$(_razzfazz_komodo_call "$RZFZ_KOMODO_CREATE_KEY_PATH" '{"name":"razzfazz-stack","expires":0}' | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
d = d if isinstance(d, dict) else {}
k, s = d.get("key"), d.get("secret")
if isinstance(k, str) and isinstance(s, str) and k and s and " " not in k + s:
    print(k, s)' 2>/dev/null)
    key="${pair%% *}"; secret="${pair#* }"
    if [ -z "$pair" ] || [ -z "$key" ] || [ -z "$secret" ] || [ "$key" = "$pair" ]; then
        echo "komodo: ${RZFZ_KOMODO_CREATE_KEY_PATH} returned no key pair" >&2
        return 1
    fi
    update_env_value "$env_file" KOMODO_API_KEY "$key" \
        && update_env_value "$env_file" KOMODO_API_SECRET "$secret" \
        || { echo "komodo: the API key was minted but could not be written to ${env_file}" >&2; return 1; }
    echo "  komodo: API key minted for the stack (KOMODO_API_KEY in .env)"
}

# #2566: the LIVE flag for `rzfz status`: prints on | off | unknown. Read-only:
# never writes, never restarts anything.
razzfazz_komodo_auto_prune_state() {
    local KOMODO_BASE_URL _RZFZ_K_KEY _RZFZ_K_SECRET _RZFZ_K_JWT ids id v on=false n
    if _razzfazz_komodo_auth 2>/dev/null; then
        ids="$(_razzfazz_komodo_server_ids)"
        for id in $ids; do
            v="$(_razzfazz_komodo_server_auto_prune "$id")"
            case "$v" in true) on=true ;; false) ;; *) ids=""; break ;; esac
        done
        if [ -n "$ids" ]; then
            if [ "$on" = true ]; then echo on; else echo off; fi
            return 0
        fi
    fi
    if n=$(razzfazz_komodo_store_auto_prune_count 2>/dev/null); then
        if [ "$n" -gt 0 ]; then echo on; else echo off; fi
        return 0
    fi
    echo unknown
}

# Turn auto_prune off on every server Komodo knows about. Returns non-zero if no
# way in works, or if any server still reports the flag set after the write — a
# prune that keeps running while provisioning claims it is off is worse than a
# loud failure.
# #2566: works without the admin password (API key, else Komodo's store).
razzfazz_komodo_disable_auto_prune() {
    local KOMODO_BASE_URL _RZFZ_K_KEY _RZFZ_K_SECRET _RZFZ_K_JWT ids id now rc=0
    if _razzfazz_komodo_auth; then
        ids="$(_razzfazz_komodo_server_ids)"
        if [ -z "$ids" ]; then
            echo "komodo: no servers returned — trying Komodo's store" >&2
        else
            for id in $ids; do
                _razzfazz_komodo_call /write/UpdateServer \
                    "{\"id\":\"${id}\",\"config\":{\"auto_prune\":false}}" >/dev/null
                # Re-read: the write is only believed once the server says so.
                now="$(_razzfazz_komodo_server_auto_prune "$id")"
                if [ "$now" = "false" ]; then
                    printf '  komodo server %s: auto_prune=false\n' "$id"
                else
                    printf '  komodo server %s: auto_prune is still %s\n' "$id" "${now:-unknown}" >&2
                    rc=1
                fi
            done
            [ "$rc" -eq 0 ] && return 0
            echo "komodo: the API write did not take — trying Komodo's store" >&2
        fi
    fi
    razzfazz_komodo_ferretdb_disable_auto_prune
}

# ==============================================================================
# #428: appliance-package index — scan the 63 GB archive ONCE, consult forever.
# ==============================================================================
# gzip is not seekable: every `tar xzf <pkg> <subtree>` miss costs a FULL pass
# over the archive, and GNU tar normalizes the leading ./ so the two-attempt
# `./images || images` pattern was the identical scan run twice. One
# `tar -tzf` pass builds a member index cached beside the package (fallback
# /var/tmp when the package dir is read-only), keyed on size+mtime so a
# replaced package invalidates it. init, post-install and upgrade consult the
# index to learn IF a subtree exists and at WHICH prefix, then extract once.

# ── #2006 part 2: custom-image provenance ───────────────────────────────────
# "present" must mean "built from THIS tree". core/config/app/services/
# image_provenance.py computes a build-context digest per custom image, keeps a
# record of what was built (image id + digest) and answers, per image the tree
# demands, whether it is this-build / package-build / stale / missing. These
# wrappers are the ONLY way init, post-install, upgrade and the packager talk to
# it, so the four sites cannot drift. Every wrapper is best-effort towards the
# caller (never aborts an install) — but the one that gates a SKIP fails
# CLOSED: when the helper cannot answer, it prints an `unverifiable` row so
# nothing is judged complete on silence.
razzfazz_image_provenance_py() {
    printf '%s\n' "${RAZZFAZZ_IMAGE_PROVENANCE:-${SCRIPT_DIR:-.}/core/config/app/services/image_provenance.py}"
}

# After a `docker compose build`: remember image id + context digest for the
# custom images THIS build built. Args: [profiles] [source=built|package]
# [package-manifest] [built-refs]. source=built: PROFILES is the build's scope
# (default: the COMPOSE_PROFILES the bare build used — the box's .env); a 4th
# argument, even empty, narrows it to those comma-joined refs (a pass that built
# single services). #2659: a present image the build did not build keeps its
# record — the old "every present image" stamp made an upgrade with a profile
# off certify that profile's old image as the new tree's build.
# With source=package and a manifest, only refs present under the PACKAGE's
# image id are recorded (#2168): the record then says what the stick put there,
# replacing whatever a previous install on this box left in the file.
razzfazz_record_custom_image_builds() {
    local profiles="${1:-}" source="${2:-built}" manifest="${3:-}" py out n
    local -a built=()
    [ "$#" -lt 4 ] || built=(--built "$4")
    py="$(razzfazz_image_provenance_py)"
    if [ ! -f "$py" ]; then
        print_warning "  Build record NOT written (${py} missing) — the next init cannot tell these images from adopted ones (#2006)."
        return 0
    fi
    if out="$(python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --record ${profiles:+--profiles "$profiles"} --source "$source" ${manifest:+--package-manifest "$manifest"} ${built[@]+"${built[@]}"} 2>/dev/null)"; then
        n=$(printf '%s\n' "$out" | grep -c . || true)
        if [ "$source" = "package" ]; then
            print_substep "  Build record: ${n} custom image(s) recorded as loaded from the package (#2168)."
        else
            print_substep "  Build record: ${n} custom image(s) recorded as built from this tree (#2006)."
        fi
    else
        print_warning "  Build record NOT written (provenance helper failed) — the next init cannot tell these images from adopted ones (#2006)."
    fi
    return 0
}

# TSV `image<TAB>kind<TAB>verdict<TAB>detail` for every image PROFILES demand.
# Empty when the helper cannot answer. Args: profiles [package-manifest]
# #2441: the on-box copy of the package manifest the provenance helper keeps
# beside its build record (image_provenance.default_package_manifest_path).
razzfazz_package_manifest_state_path() {
    local st="${RAZZFAZZ_IMAGE_STATE:-$HOME/.razzfazz/state/custom-image-builds.json}"
    printf '%s/package-manifest.json\n' "$(dirname "$st")"
}

# #3205: under sudo ($HOME=/root) every rzfz command reads the build record of
# the user who installed the stack, not /root's (which does not exist): `sudo rzfz
# status` called every custom image of a fresh stick install STALE. The rule is
# image_provenance.default_state_path; exported here so every reader (the
# provenance and digest-pin helpers, the path above) follows it. Only when
# running as root or under sudo, only when nothing set it already.
razzfazz_export_image_state() {
    [ -z "${RAZZFAZZ_IMAGE_STATE:-}" ] || return 0
    [ -n "${SUDO_USER:-}" ] || [ "${EUID:-}" = 0 ] || return 0
    local root py st
    root="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)}"
    py="${root}/core/config/app/services/image_provenance.py"
    [ -f "$py" ] || return 0
    st="$(python3 "$py" --stack-root "$root" --state-path 2>/dev/null)" || return 0
    [ -n "$st" ] && [ "$st" != "${HOME:-}/.razzfazz/state/custom-image-builds.json" ] || return 0
    export RAZZFAZZ_IMAGE_STATE="$st"
}
razzfazz_export_image_state

# #2778: keep the package's digest attestations (image ids recorded for the
# `tag@sha256:` refs) beside the build record, where a later code-only package
# cannot supersede them. `docker load` carries no repo digest; the id is what a
# box can compare. Best-effort; never aborts the caller.
razzfazz_record_digest_pins() {
    local manifest="$1" py="${SCRIPT_DIR:-.}/core/config/app/services/digest_pins.py"
    [ -s "$manifest" ] && [ -f "$py" ] || return 0
    python3 "$py" --record "$manifest" >/dev/null 2>&1 || true
    return 0
}

# #2778: after `docker load`, tag the package image of every digest-pinned ref
# as its repo:tag when it arrived untagged (a tar saved from a digest ref has no
# RepoTags). One line per re-tag; verify-images still judges the result.
# Best-effort; never aborts the caller.
razzfazz_retag_digest_pins() {
    local manifest="$1" py="${SCRIPT_DIR:-.}/core/config/app/services/digest_pins.py" out
    [ -s "$manifest" ] && [ -f "$py" ] || return 0
    out="$(python3 "$py" --retag "$manifest" 2>&1)" || true
    [ -z "$out" ] || printf '%s\n' "$out" | while IFS= read -r _l; do print_substep "$_l (#2778)"; done
    return 0
}

# #2829: the images a package's expected-images.json lists that this box does
# not hold, one per line ("ref" or "ref (reason)"). A digest-pinned ref is judged
# by digest_pins' resolver (the one verify-images and the upgrade use), never by
# a string compare: `docker load` drops the digest. rc 2 when it cannot judge.
# The helper is found beside this library, not via SCRIPT_DIR: the gate must not
# turn into "(gate errored)" because a caller points SCRIPT_DIR elsewhere.
razzfazz_expected_images_missing() {
    local manifest="$1" py
    py="${RAZZFAZZ_DIGEST_PINS_PY:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/core/config/app/services/digest_pins.py}"
    [ -f "$py" ] || return 2
    python3 "$py" --missing "$manifest"
}

# #2659: the one spelling the verdicts and build records key on — an implicit
# tag is :latest (build_preflight.canonical_ref is the same rule). Look a verdict
# up with this, never with the raw ref compose printed.
razzfazz_canonical_image_ref() {
    local ref="$1"
    case "$ref" in
        ""|*@*) printf '%s\n' "$ref" ;;
        *) case "${ref##*/}" in *:*) printf '%s\n' "$ref" ;; *) printf '%s:latest\n' "$ref" ;; esac ;;
    esac
}

razzfazz_custom_image_verdicts() {
    local py; py="$(razzfazz_image_provenance_py)"
    [ -f "$py" ] || return 0
    python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --verdicts --profiles "$1" ${2:+--package-manifest "$2"} 2>/dev/null || true
}

# #2566: a custom image present ONLY under the hub tag (#1472 publishes
# 127.0.0.1:<LLM_REGISTRY_PORT>/{node,runners}/<ref>; a prune took the local
# tag). The hub copy is TRUSTED only when its image id IS the image the build
# record (or the last offline package) holds for <ref>; a hub copy of another
# build is not. Read-only: prints the trusted image id and the hub ref, 0;
# 1 = no trusted hub copy. Never pulls.
razzfazz_hub_copy_trusted() {
    local ref="$1" py port prefix cand cid
    py="$(razzfazz_image_provenance_py)"
    [ -f "$py" ] || return 1
    port=$(read_env_value "${SCRIPT_DIR:-.}/.env" LLM_REGISTRY_PORT 2>/dev/null || true)
    port="${port:-8093}"
    for prefix in node runners; do
        cand="127.0.0.1:${port}/${prefix}/${ref}"
        cid=$(docker image inspect -f '{{.Id}}' "$cand" 2>/dev/null) || continue
        [ -n "$cid" ] || continue
        if python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --trusted-id "$ref" --image-id "$cid" >/dev/null 2>&1; then
            printf '%s %s\n' "$cid" "$cand"
            return 0
        fi
        echo "  ${cand} is not the build this box recorded for ${ref} — not re-tagged" >&2
    done
    return 1
}

# The lock-holding verbs (upgrade, post-install) give a trusted hub copy its
# local name back — not rebuilt, not pulled. `rzfz status` only reports it.
# The tag is set on the verified id. 0 = re-tagged, 1 = no trusted hub copy.
razzfazz_retag_from_hub() {
    local ref="$1" found cid
    found="$(razzfazz_hub_copy_trusted "$ref")" || return 1
    cid="${found%% *}"
    docker tag "$cid" "$ref" >/dev/null 2>&1 || return 1
    [ "$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null)" = "$cid" ]
}

# The provenance verdict word (this-build|package-build|stale|missing|unknown)
# of ONE ref for PROFILES; empty when the helper cannot answer. Args: profiles ref
razzfazz_custom_image_verdict_of() {
    local py; py="$(razzfazz_image_provenance_py)"
    [ -f "$py" ] || return 0
    python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --verdict-of "$2" --profiles "$1" 2>/dev/null \
        | awk -F'\t' '{print $3; exit}' || true
}

# #2566: `post-install --refresh` rebuilt the fleet master's worker-agent on
# every run ("present but NOT this tree's build") and minted an unused image
# the nightly prune then took along with the local tag. The running container
# is the truth: when it runs the build the record holds for this tree — same
# commit AND same build-context digest (build args, uncommitted edits) — keep
# it and put the local tag back on it. 0 = keep, 1 = rebuild.
# Args: svc img [profiles the verdicts were rendered for]
razzfazz_refresh_keeps_running_build() {
    local svc="$1" img="$2" profiles="${3:-}" py cid run_id
    py="$(razzfazz_image_provenance_py)"
    [ -f "$py" ] || return 1
    cid=$(docker compose ps -q "$svc" 2>/dev/null | head -n 1)
    [ -n "$cid" ] || return 1
    run_id=$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null)
    [ -n "$run_id" ] || return 1
    python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --running-is-this-build "$img" \
        --running-id "$run_id" ${profiles:+--profiles "$profiles"} >/dev/null 2>&1 || return 1
    if [ "$(docker image inspect -f '{{.Id}}' "$img" 2>/dev/null)" != "$run_id" ]; then
        docker tag "$run_id" "$img" >/dev/null 2>&1 || return 1
    fi
    return 0
}

# The rows that must BLOCK a zero-download skip: custom images PROFILES demand
# that are missing or stale. Empty output = nothing blocks. Fails CLOSED: when
# the helper is absent or errors, one `unverifiable` row is printed instead of
# nothing. Args: profiles [package-manifest]
razzfazz_custom_images_not_from_this_tree() {
    local py rows rc=0
    py="$(razzfazz_image_provenance_py)"
    if [ ! -f "$py" ]; then
        printf '(all custom images)\tcustom\tunverifiable\tprovenance helper %s is missing — cannot prove the loaded set is this tree\x27s build\n' "$py"
        return 0
    fi
    rows="$(python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --blocking --profiles "$1" ${2:+--package-manifest "$2"} 2>/dev/null)" || rc=$?
    if [ "$rc" -gt 1 ] || { [ "$rc" -eq 1 ] && [ -z "$rows" ]; }; then
        printf '(all custom images)\tcustom\tunverifiable\tprovenance helper exited %s without an answer — cannot prove the loaded set is this tree\x27s build\n' "$rc"
        return 0
    fi
    [ -n "$rows" ] && printf '%s\n' "$rows"
    return 0
}

appliance_pkg_index() {
    # $1 = package path → prints the index file path (builds/caches it).
    local pkg="$1" idx meta cur
    idx="${pkg}.index"; meta="${pkg}.index.meta"
    if ! ( : >> "$idx" ) 2>/dev/null; then
        local key; key="$(echo "$pkg" | sha256sum | cut -c1-16)"
        idx="/var/tmp/rzfz-appliance-${key}.index"; meta="${idx}.meta"
    fi
    cur="$(stat -c '%s:%Y' "$pkg" 2>/dev/null)" || return 1
    if [ -s "$idx" ] && [ "$(cat "$meta" 2>/dev/null)" = "$cur" ]; then
        echo "$idx"; return 0
    fi
    tar -tzf "$pkg" > "${idx}.tmp" 2>/dev/null || { rm -f "${idx}.tmp"; return 1; }
    mv -f "${idx}.tmp" "$idx"
    echo "$cur" > "$meta"
    echo "$idx"
}

# ── #271 / #2120: nothing is extracted or loaded when nothing needs loading ──
# Three partial fixes (#271 two reads, #428 the index, #1478 the per-archive load
# skip) each removed one pass and closed one issue; the extraction of images/
# stayed unconditional in init AND in the offline upgrade, and the upgrade never
# had the load skip at all. Measured on 0.175 (2026-09-14): a 102 GB package,
# 155 GB extracted to /var/tmp, 8+ minutes of gzip, 0 images loaded — all 106
# were already present. These helpers are shared by init, post-install and
# upgrade so the property holds at every call site:
#   "given a package whose images are all present, nothing is extracted and
#    nothing is loaded."
# "Present" is decided by IMAGE ID, never by tag: a tag says which name exists,
# not which build (#2006, #2105).

# Extract ONLY the package's expected-images.json (member near the start of the
# archive; --occurrence=1 stops tar at the first match instead of reading on).
# $1 = package, $2 = destination file, $3 = member prefix from appliance_pkg_prefix.
appliance_extract_manifest() {
    local pkg="$1" dest="$2" prefix="$3"
    [ -n "$prefix" ] || return 1
    tar -xzf "$pkg" -O --occurrence=1 "$prefix" > "$dest" 2>/dev/null && [ -s "$dest" ]
}

# #3201: a stick carries every hardware line's images (amd, cpu, nvidia: the
# gpustack builds and the llama runners); a box loads only its own line's. With
# the package's `hardware_images` (one list per line, written by an offline package (Enterprise))
# and this box's HARDWARE, rewrite MANIFEST in place to the box's set: `images`,
# `image_ids`, `images_bytes`, `largest_image_bytes` follow it, so the
# needs-loading check, the disk preflight and the completeness gate all judge the
# same set. The full manifest is kept beside it (MANIFEST.full). Prints the
# archive basenames NOT to extract. An image in no line (the common services) is
# always loaded; no hardware_images, or a HARDWARE the package does not name =
# nothing filtered. Idempotent: a manifest already filtered is read from .full.
# #3403: $3 = `stable` (the box has not opted in to the experimental runner):
# the package's `experimental_runner_images` are skipped too, whatever the line.
# `all` or empty: they are loaded with the rest of the box's line.
appliance_hardware_filter() {
    local manifest="$1" hw="$2" channels="${3:-all}"
    { [ -n "$hw" ] || [ "$channels" = stable ]; } && [ -s "$manifest" ] || return 0
    # #3215: the rule is hardware_lines.skipped, shared with verify-images.
    # #3490: RAZZFAZZ_HARDWARE_LINES_DIR = the package's copy on a crossing whose tree has none.
    RZFZ_HL_DIR="${RAZZFAZZ_HARDWARE_LINES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../core/config/app/services" 2>/dev/null && pwd)}" \
    python3 - "$manifest" "$hw" "$channels" <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.environ.get("RZFZ_HL_DIR") or ".")
try:
    from hardware_lines import skipped
except ImportError:
    # #3490: a tree onto which only cli/ scripts/ rzfz were pre-placed. Say so,
    # and filter nothing: never guess away an image.
    sys.stderr.write("  The hardware-line filter is not available (core/config/app/services/"
                     "hardware_lines.py is missing): every hardware line's images and the "
                     "experimental runners stay in the load.\n")
    sys.exit(0)
path, hw, channels = sys.argv[1], sys.argv[2], sys.argv[3]
full = path + ".full"
m = json.load(open(path, encoding="utf-8"))
if m.get("hardware_filtered") or m.get("experimental_filtered"):
    m = json.load(open(full, encoding="utf-8"))
lines = m.get("hardware_images") or {}
skip = skipped(lines, hw) if hw in lines else set()
if channels == "stable":
    skip |= {r for r in (m.get("experimental_runner_images") or []) if r in (m.get("images") or [])
             or r in (m.get("image_ids") or {})}
if not skip:
    sys.exit(0)
json.dump(m, open(full, "w", encoding="utf-8"), indent=2, sort_keys=True)
arch = lambda r: r.replace("/", "_").replace(":", "_") + ".tar"
out = dict(m)
out["images"] = [r for r in m.get("images") or [] if r not in skip]
out["image_ids"] = {r: i for r, i in (m.get("image_ids") or {}).items() if r not in skip}
out["count"] = len(out["images"])
if hw in lines:
    out["hardware_filtered"] = hw
if channels == "stable":
    out["experimental_filtered"] = True
out["hardware_skipped"] = sorted(skip)
sizes = m.get("archive_bytes") or {}
if sizes:
    kept = [n for a, n in sizes.items() if a not in {arch(r) for r in skip}]
    out["images_bytes"] = sum(kept)
    out["largest_image_bytes"] = max(kept, default=0)
json.dump(out, open(path, "w", encoding="utf-8"), indent=2, sort_keys=True)
for r in sorted(skip):
    print(arch(r))
PYEOF
}

# Does this docker-save archive's content already exist locally — same IMAGE ID,
# not merely the same tag? manifest.json is the archive's own index (small,
# uncompressed); its Config names the image id. Any RepoTag whose local id equals
# it means the content is present. $1 = archive, $2 = (kept for callers) the
# `docker image ls` output, consulted only when no RepoTag can be inspected.
appliance_archive_images_present() {
    local archive="$1" have="${2:-}" rows row id tag ok=0 any=0
    rows="$(tar -xOf "$archive" manifest.json 2>/dev/null | python3 -c '
import json, sys, os
try:
    for m in json.load(sys.stdin):
        cfg = os.path.basename(str(m.get("Config") or ""))
        cfg = cfg[:-5] if cfg.endswith(".json") else cfg
        for t in (m.get("RepoTags") or []):
            print(f"{t}\t{cfg}")
except Exception:
    pass
' 2>/dev/null)" || rows=""
    [ -n "$rows" ] || return 1
    while IFS=$'\t' read -r tag id; do
        [ -n "$tag" ] || continue
        any=1
        local local_id; local_id="$(docker image inspect --format '{{.Id}}' "$tag" 2>/dev/null || true)"
        if [ -n "$local_id" ] && [ -n "$id" ]; then
            [ "$local_id" = "sha256:${id}" ] || return 1
        elif [ -n "$have" ]; then
            printf '%s\n' "$have" | grep -qxF "$tag" || return 1
        else
            return 1
        fi
    done <<< "$rows"
    [ "$any" -eq 1 ]
}

# Decide from the package's expected-images.json alone — before anything is
# extracted — whether its images/ subtree needs loading. Prints the refs that do
# (TSV image<TAB>reason). rc 0 = nothing needs loading; 1 = some do; 2 = cannot
# decide (no image_ids in the manifest: a package built before #2120 — extract
# as before). With PROFILES given (init), the tree's own demand is part of the
# conjunction: a custom image THIS tree demands that is missing or stale blocks
# the skip too (#2006 part 2) — a manifest-only gate would skip the one archive
# that could have supplied the image.
# $1 = expected-images.json path, $2 = profiles (optional)
appliance_package_needs_loading() {
    local manifest="$1" profiles="${2:-}" py rows rc=0 tree
    py="$(razzfazz_image_provenance_py)"
    [ -f "$py" ] && [ -s "$manifest" ] || return 2
    rows="$(python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --needs-loading --package-manifest "$manifest" 2>/dev/null)" || rc=$?
    [ "$rc" -le 1 ] || return 2
    if [ -n "$profiles" ]; then
        tree="$(razzfazz_custom_images_not_from_this_tree "$profiles" "$manifest")"
        if [ -n "$tree" ]; then
            rows="${rows:+${rows}
}$(printf '%s\n' "$tree" | awk -F'\t' 'NF{printf "%s\t%s (%s)\n", $1, $3, $4}')"
            rc=1
        fi
    fi
    [ -n "$rows" ] && printf '%s\n' "$rows"
    return "$rc"
}

# #2221: one canonical ref per IMAGE (a bare repo is repo:latest to Docker), from
# refs or TSV rows on stdin. Counting rows counted one image twice.
razzfazz_distinct_image_refs() {
    cut -f1 | awk 'NF { r = $0; n = split(r, p, "/"); if (p[n] !~ /[:@]/) r = r ":latest"; print r }' | sort -u
}

# razzfazz_dify_plugin_pin <vendor/name> — "<version>\t<identifier>" from the one
# pin table (#2545); rc 1 for a plugin the table does not pin.
razzfazz_dify_plugin_pin() {
    local name="$1" table="${RAZZFAZZ_DIFY_PLUGIN_PINS:-${SCRIPT_DIR:-.}/config/dify-plugin-pins.tsv}"
    [ -f "$table" ] || return 1
    # CR stripped: a table saved with CRLF endings must not hand post-install an
    # identifier ending in \r (its offline fallback install then fails).
    awk -F'\t' -v n="$name" '{ sub(/\r$/, "") } !/^#/ && $1 == n { print $2 "\t" $3; f = 1; exit } END { exit !f }' "$table"
}

# razzfazz_dify_plugin_pinned_names — every plugin the pin table pins, one
# vendor/name per line (#3233: these are kept out of Dify's auto-upgrade).
razzfazz_dify_plugin_pinned_names() {
    local table="${RAZZFAZZ_DIFY_PLUGIN_PINS:-${SCRIPT_DIR:-.}/config/dify-plugin-pins.tsv}"
    [ -f "$table" ] || return 1
    awk -F'\t' '{ sub(/\r$/, "") } !/^#/ && NF >= 3 && $1 ~ /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/ { print $1 }' "$table"
}

# razzfazz_dify_structured_output_probe <admin_email> — ONE row `SO|<verdict>|<text>`
# from scripts/dify_structured_output_probe.py run inside dify-api (verdicts:
# OK, REFUSED with Dify's exact error, SKIP, ERROR = no verdict). Bounded twice:
# the probe's own alarm and `timeout` here, so a hung model never hangs the caller.
razzfazz_dify_structured_output_probe() {
    local email="$1" probe="${SCRIPT_DIR:-.}/scripts/dify_structured_output_probe.py" out rc=0
    if [ ! -f "$probe" ]; then
        printf 'SO|ERROR|%s is missing\n' "$probe"
        return 0
    fi
    out=$(timeout "${RZFZ_DIFY_SO_PROBE_TIMEOUT:-120}" docker exec -i -e RZFZ_DIFY_SO_PROBE_ALARM="${RZFZ_DIFY_SO_PROBE_ALARM:-75}" dify-api python3 - "$email" < "$probe" 2>/dev/null) || rc=$?
    out=$(printf '%s\n' "$out" | grep -m1 '^SO|' || true)
    if [ -n "$out" ]; then
        printf '%s\n' "$out"
    elif [ "$rc" = 124 ]; then
        printf 'SO|ERROR|no answer within %s s - no verdict\n' "${RZFZ_DIFY_SO_PROBE_TIMEOUT:-120}"
    else
        printf 'SO|ERROR|the probe printed no verdict (rc %s) - no verdict\n' "$rc"
    fi
}

appliance_pkg_prefix() {
    # $1 = index file, $2 = subtree (images|models|<file>): prints the member
    # prefix as stored in the tar (./<sub> or <sub>); rc 1 when absent.
    local idx="$1" sub="$2"
    if grep -q "^\./${sub}\(/\|$\)" "$idx" 2>/dev/null; then echo "./${sub}"; return 0; fi
    if grep -q "^${sub}\(/\|$\)" "$idx" 2>/dev/null; then echo "${sub}"; return 0; fi
    return 1
}

# ── Release-tag helpers (#665: bare tags from 2026.09 on) ────────────────────
# Operator decision 2026-08-24: new release tags are bare (2026.09-ga,
# 2026.09-ga.N); pre-2026.09 tags keep their v prefix FOREVER (never rewrite
# published tags). Matchers are therefore permanently dual-form; constructors
# emit only the bare form. These two helpers are the shared implementation —
# pre-tag-check.sh, prepare-release.sh, and cli/upgrade.sh all go through
# them so the mixed-form ordering/resolution logic exists exactly once.

# List every GA release tag, BOTH forms, ordered by NORMALIZED version
# (newest first). A plain `git tag --sort=-v:refname` over the mixed list
# compares 'v2026…' against '2026…' lexically and misorders across the
# scheme change; sorting on the v-stripped form keeps one version line.
razzfazz_list_ga_tags() {
    git tag --list 'v*-ga*' '20*-ga*' 2>/dev/null \
        | awk '{ norm = $0; sub(/^v/, "", norm); print norm "\t" $0 }' \
        | sort -t "$(printf '\t')" -k1,1rV \
        | cut -f2
}

# Every release tag of EITHER form (v2026.08-ga.15 / 2026.09-rc1), newest first,
# in RELEASE order: cycle, then a cycle's GA above its release candidates, then
# the numeric suffixes. `git tag --sort=-v:refname` cannot give this — it ranks
# `rc5` above `ga.15` because 'r' > 'g', and a 'v*' glob cannot see bare tags
# at all (#665). Both defects sat in prepare-release.sh's migration-baseline
# picker (#2123): it chose v2026.08-rc5 as "previous release" and stayed blind
# to every 2026.09 tag, so the .env.example diff it judged was 991 lines against
# the real 0. Foreign tags (semver, ad-hoc) are dropped, not mis-sorted.
razzfazz_list_release_tags() {
    git tag --list 'v20*' '20*' 2>/dev/null \
        | awk '{
            t = $0; n = t; sub(/^v/, "", n)
            if (n !~ /^[0-9][0-9][0-9][0-9]\.[0-9][0-9]-(rc|ga)(\.?[0-9]+)?(\.[0-9]+)?$/) next
            split(n, a, "-"); cycle = a[1]; rest = a[2]
            phase = (rest ~ /^ga/) ? 2 : 1
            sub(/^(rc|ga)\.?/, "", rest)
            split(rest, b, "."); n1 = (b[1] == "" ? 0 : b[1]); n2 = (b[2] == "" ? 0 : b[2])
            printf "%s\t%d\t%d\t%d\t%s\n", cycle, phase, n1, n2, t
        }' | sort -t "$(printf '\t')" -k1,1rV -k2,2rn -k3,3rn -k4,4rn | cut -f5
}

# The newest release tag that is not the version being cut (either spelling).
# Args: [version-being-cut]
razzfazz_previous_release_tag() {
    local cut="${1:-}" bare
    bare="${cut#v}"
    razzfazz_list_release_tags \
        | { if [ -n "$bare" ]; then grep -v -x -e "$bare" -e "v${bare}"; else cat; fi; } \
        | head -1
}

# #2706: `git` for a command that reaches origin. When `rzfz upgrade` holds a
# token typed in for this run (_RZFZ_GIT_PAT, a shell variable that is never
# exported), it is handed to THIS git process only — through the environment
# of the one command, read back by scripts/git-askpass-env.sh — with the
# configured credential helpers reset, so `store` cannot write it down. No
# child of the upgrade inherits it, and a re-exec does not carry it. xtrace is
# off while the token is expanded, so `set -x` and the run log never show it.
razzfazz_git_origin() {
    # Tested without expanding it: a traced `[ -z "$tok" ]` would print it.
    if [ -z "${_RZFZ_GIT_PAT:+set}" ]; then
        # #2690: no token typed in for this run: git must not ask for one
        # either. A prompt git opens itself would hand the answer to a
        # configured `store` helper, which writes it to disk.
        razzfazz_git_noninteractive "$@"
        return
    fi
    { local _rzfz_xt=0 _rzfz_rc=0; case $- in *x*) _rzfz_xt=1; set +x ;; esac; } 2>/dev/null
    RZFZ_GIT_ASKPASS_USER="${_RZFZ_GIT_USER:-}" RZFZ_GIT_ASKPASS_PAT="$_RZFZ_GIT_PAT" \
        GIT_ASKPASS="${_RZFZ_GIT_ASKPASS:-}" GIT_TERMINAL_PROMPT=0 \
        git -c credential.helper= "$@" || _rzfz_rc=$?
    [ "$_rzfz_xt" = 0 ] || set -x
    return "$_rzfz_rc"
}

# #2690: `git` that never asks anyone for anything. No terminal prompt
# (GIT_TERMINAL_PROMPT=0), and no askpass program either: git runs GIT_ASKPASS,
# core.askPass or SSH_ASKPASS even when terminal prompts are off, and a desktop
# session or a configured askpass would block an unattended run on a dialog.
# GIT_ASKPASS=true answers every question with nothing, so a missing
# credential fails fast; an ssh origin runs in BatchMode (no passphrase or
# host-key question). stdin is left alone (git prompts on /dev/tty, not on
# stdin, and `git credential` reads its request from stdin). For every path
# that is not the one prompt the operator asked for (`rzfz upgrade` at a
# terminal, read_git_pat_for_this_run).
razzfazz_git_noninteractive() {
    GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true SSH_ASKPASS= \
        GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh} -o BatchMode=yes" git "$@"
}

# #2690: the box's PAT mode, from RAZZFAZZ_STORE_PAT (the environment variable
# of the same name overrides .env). Prints `true` (a fleet box that may store
# the Gitea PAT: `rzfz init --store-pat`), `false` (explicitly rental), or
# nothing (no marker yet: a box installed before the key existed).
# Args: [env-file] (default .env).
razzfazz_store_pat_marker() {
    local v="${RAZZFAZZ_STORE_PAT:-}"
    [ -n "$v" ] || v=$(read_env_value "${1:-.env}" RAZZFAZZ_STORE_PAT 2>/dev/null || true)
    case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
        true|1|yes) echo true ;;
        "") ;;
        *) echo false ;;
    esac
}

# #2690: rental is the DEFAULT. Every box is a rental box, which never keeps
# the Gitea PAT (the operator enters it per use: `rzfz upgrade` at a terminal,
# or an offline package), unless RAZZFAZZ_STORE_PAT=true marks it as one of
# our fleet boxes. Args: [env-file] (default .env).
razzfazz_rental_box() {
    [ "$(razzfazz_store_pat_marker "${1:-.env}")" != true ]
}

# #2690: settle the PAT mode of a box once, in `rzfz upgrade` only (init
# writes false without a flag). A marker already set (true or false) is kept.
# Without one, a box that already holds the PAT in a place only we would have
# put it is one of ours from before the default changed: it is marked
# RAZZFAZZ_STORE_PAT=true so it keeps working. Those places are a
# git.razzfazz.ai entry in a home's credential file (~/.git-credentials,
# ~/.config/git/credentials), and user info in a git.razzfazz.ai remote URL
# of the stack checkout. Helpers, insteadOf rewrites, .env values and files
# that .env or the repo point at never decide it: anyone with admin access to
# the box can plant those, and must not be able to keep a rental box from
# being cleaned. Any other box is marked false.
# Prints `adopted<TAB>message` or `rental<TAB>message` when it wrote the key;
# nothing when a marker was already there.
# Args: <stack-dir>
razzfazz_settle_pat_mode() {
    local stack="${1:-.}" envf="${1:-.}/.env"
    [ -f "$envf" ] || return 0
    [ -z "$(razzfazz_store_pat_marker "$envf")" ] || return 0
    if razzfazz_pat_adoptable "$stack"; then
        update_env_value "$envf" RAZZFAZZ_STORE_PAT true
        printf 'adopted\t%s\n' "RAZZFAZZ_STORE_PAT=true written: this box already stores the Gitea PAT (a git.razzfazz.ai credential-file entry or remote URL), so it is treated as a fleet box and keeps it. If it is a rental box: set RAZZFAZZ_STORE_PAT=false, then rzfz forget-pat."
    else
        update_env_value "$envf" RAZZFAZZ_STORE_PAT false
        printf 'rental\t%s\n' "RAZZFAZZ_STORE_PAT=false written: no Gitea PAT is stored on this box, which stays a rental box (the PAT is entered per use)."
    fi
    return 0
}

# #3265: is this one of our fleet boxes — the only kind that may run the internal
# channel (operator 2026-11-16: rental boxes run the public channel only; an
# internal-channel box fetches development tags, which never belong at a customer
# site)? Read-only: RAZZFAZZ_STORE_PAT=true is a fleet box; a box without the
# marker is answered the way `rzfz upgrade` will settle it (an adoptable PAT makes
# it a fleet box); anything else is a rental box. Returns 0 for a fleet box.
# Args: [stack-dir] (default .)
# #3265 THE SWITCH: may our fleet boxes (RAZZFAZZ_STORE_PAT=true) run the internal
# channel? 1 = yes (rental boxes are public, fleet boxes may be internal);
# 0 = every box runs the public channel. The operator decides; this line is the
# whole change. Assigned here, never read from the environment or .env, so no box
# can switch itself back.
RAZZFAZZ_FLEET_MAY_USE_INTERNAL_CHANNEL=1

# #3265: may this box run the internal channel? Only a fleet box, and only while
# the switch above allows it. Args: [stack-dir]
razzfazz_internal_channel_allowed() {
    [ "$RAZZFAZZ_FLEET_MAY_USE_INTERNAL_CHANNEL" = 1 ] && razzfazz_fleet_box "${1:-.}"
}

razzfazz_fleet_box() {
    local stack="${1:-.}" marker
    marker=$(razzfazz_store_pat_marker "${stack}/.env")
    [ "$marker" = true ] && return 0
    [ -z "$marker" ] && razzfazz_pat_adoptable "$stack" && return 0
    return 1
}

# #3265 (operator 2026-11-16): a box on the public channel keeps no development
# history from git.razzfazz.ai. A commit is PUBLIC when it and every commit behind it
# is either stamped (PUBLIC_EXPORT_OF: written by scripts/publish-public.sh into every
# export since #2359, and by modules/tools/appliance/release-tree-bundle.sh into the
# stick's parentless release tree of #3253) or a parentless snapshot without the internal .gsd/ tree
# (the mirror's exports from before the stamp existed; publish-public.sh hard gate 1
# keeps .gsd/ out of every export). Everything else is development. Judging the tip
# alone was not enough (2026-11-17, a real box): a branch whose tip deleted .gsd/
# and internal tags from before .gsd/ existed looked public and kept 8257
# development commits reachable. The walk stops at the first development commit.
razzfazz_public_commit() {  # <repo> <rev>
    local c parents
    git -C "$1" rev-parse -q --verify "${2}^{commit}" >/dev/null 2>&1 || return 1
    while read -r c parents; do
        git -C "$1" cat-file -e "${c}:PUBLIC_EXPORT_OF" 2>/dev/null && continue
        [ -z "$parents" ] || return 1
        git -C "$1" cat-file -e "${c}:.gsd" 2>/dev/null && return 1
    done < <(git -C "$1" rev-list --parents "${2}^{commit}" 2>/dev/null)
    return 0
}

# #3265: the refs of <repo> whose commit is not public, one per line (refname).
# Symbolic refs (refs/remotes/origin/HEAD) are listed under their own name.
# RAZZFAZZ_DEV_REFS_FIRST=1: stop at the first one (rzfz status / check mode).
# Batched (measured: 91 s over the 2,899 refs of the internal repo with two
# spawns per ref): ONE for-each-ref (tip + its parents, peeled for tags) and ONE
# cat-file --batch-check for <tip>:PUBLIC_EXPORT_OF and <tip>:.gsd. An unstamped tip
# with parents, or one carrying .gsd/, is development without a walk; only a
# stamped tip with parents (a chained export) gets razzfazz_public_commit's walk.
razzfazz_dev_refs() {  # <repo> [<ref-prefix>...]
    local repo="$1" refs ref obj par st gs; shift
    refs=$(git -C "$repo" for-each-ref --format='%(refname)%1f%(objectname)%1f%(parent)%(*parent)' "$@" 2>/dev/null)
    [ -n "$refs" ] || return 0
    while IFS=$'\x1f' read -r ref obj par st gs; do
        [ -n "$ref" ] || continue
        if [ "$st" = blob ]; then
            # stamped: public unless a parent behind it is development
            [ -z "$par" ] || razzfazz_public_commit "$repo" "$obj" || { printf '%s\n' "$ref"; [ "${RAZZFAZZ_DEV_REFS_FIRST:-0}" = 1 ] && return 0; }
        elif [ -n "$par" ] || [ "$gs" = tree ]; then
            printf '%s\n' "$ref"; [ "${RAZZFAZZ_DEV_REFS_FIRST:-0}" = 1 ] && return 0
        fi
    done < <(paste -d $'\x1f' <(printf '%s\n' "$refs") \
               <(printf '%s\n' "$refs" | cut -d $'\x1f' -f2 | sed 's/$/:PUBLIC_EXPORT_OF/; p; s/:PUBLIC_EXPORT_OF$/:.gsd/' \
                   | git -C "$repo" cat-file --batch-check='%(objecttype)' 2>/dev/null \
                   | sed 's/^.* missing$/missing/' | paste -d $'\x1f' - -))
    return 0
}

# #3265: remove development history from a public-channel checkout.
#   razzfazz_prune_dev_history <repo> check   prints "0 development refs; HEAD public|development" or
#                                             "development refs present (first: <ref>); HEAD …"; rc 0 = clean
#   razzfazz_prune_dev_history <repo> tags    deletes development tags and remote-tracking refs only
#                                             (before a fetch: a development tag must never stand in
#                                             for the public tag of the same name)
#   razzfazz_prune_dev_history <repo> all     deletes every development ref (tags, branches,
#                                             remote-tracking refs, stashes, notes), the reflogs and
#                                             FETCH_HEAD/ORIG_HEAD, then gc --prune=now; then checks
#                                             that no development commit is left. Needs HEAD on a
#                                             public commit (rc 3 otherwise, nothing deleted).
# Prints one line per action. rc 0 = done/clean, 1 = check found development history,
# 2 = a git step failed, 3 = HEAD is a development commit.
razzfazz_prune_dev_history() {
    local repo="${1:-.}" mode="${2:-check}" refs n head_public=public ref
    [ -d "$repo/.git" ] || return 0
    case "$mode" in
        check)
            # stops at the first development ref: rzfz status runs this on every call
            refs=$(RAZZFAZZ_DEV_REFS_FIRST=1 razzfazz_dev_refs "$repo")
            razzfazz_public_commit "$repo" HEAD || head_public=development
            if [ -z "$refs" ]; then
                printf '0 development refs; HEAD %s\n' "$head_public"
            else
                printf 'development refs present (first: %s); HEAD %s\n' "$refs" "$head_public"
            fi
            [ -z "$refs" ] && [ "$head_public" = public ]
            return ;;
        tags)
            refs=$(razzfazz_dev_refs "$repo" refs/tags refs/remotes) ;;
        all)
            razzfazz_public_commit "$repo" HEAD || return 3
            refs=$(razzfazz_dev_refs "$repo") ;;
        *) return 2 ;;
    esac
    # one update-ref for all of them (958 refs on a real box: 1 s with one spawn per ref)
    n=$(printf '%s' "$refs" | grep -c . || true)
    if [ "$n" -gt 0 ]; then
        printf '%s\n' "$refs" | sed '/^$/d; s/^/delete /' | git -C "$repo" update-ref --no-deref --stdin 2>/dev/null || return 2
    fi
    printf 'removed %s development ref(s)\n' "$n"
    [ "$mode" = all ] || return 0
    rm -f "$repo/.git/FETCH_HEAD" "$repo/.git/ORIG_HEAD" "$repo/.git/MERGE_HEAD" "$repo/.git/CHERRY_PICK_HEAD"
    git -C "$repo" reflog expire --expire=now --expire-unreachable=now --all 2>/dev/null || return 2
    git -C "$repo" -c gc.auto=0 gc --quiet --prune=now 2>/dev/null || return 2
    # the proof: every commit object left in the store is reachable from the
    # (public) refs and HEAD — gc dropped the rest; nothing dangles.
    local left
    left=$(LC_ALL=C comm -23 \
        <(git -C "$repo" cat-file --batch-all-objects --batch-check='%(objecttype) %(objectname)' 2>/dev/null \
              | awk '$1 == "commit" {print $2}' | LC_ALL=C sort) \
        <(git -C "$repo" rev-list --all HEAD 2>/dev/null | LC_ALL=C sort) | head -1)
    [ -z "$left" ] || { printf 'a development commit is still reachable: %s\n' "$left"; return 2; }
    printf 'development history removed (gc --prune=now)\n'
    return 0
}

# #3265: the operator-facing fixes for a rental box on the internal channel, one per line.
razzfazz_rental_internal_fixes() {
    if [ "$RAZZFAZZ_FLEET_MAY_USE_INTERNAL_CHANNEL" = 1 ]; then
        printf '%s\n' \
            "Rental boxes run the public channel only: set RAZZFAZZ_CHANNEL=public in .env, then rzfz upgrade (it moves origin to the public mirror), or upgrade with an offline package." \
            "One of our fleet boxes instead: rzfz init --store-pat (marks it RAZZFAZZ_STORE_PAT=true)."
    else
        printf '%s\n' \
            "Every box runs the public channel: set RAZZFAZZ_CHANNEL=public in .env, then rzfz upgrade (it moves origin to the public mirror), or upgrade with an offline package."
    fi
}

# #2690: does this box hold the PAT where only we would have put it (see
# razzfazz_settle_pat_mode)? Returns 0 when it does. Args: <stack-dir>
razzfazz_pat_adoptable() {
    local stack="${1:-.}" h f url rc=1
    local line_re='^[A-Za-z][A-Za-z0-9+.-]*://[^/[:space:]]*@git\.razzfazz\.ai([:/[:space:]]|$)'
    { local _xt=0; case $- in *x*) _xt=1; set +x ;; esac; } 2>/dev/null
    while IFS= read -r h; do
        [ -n "$h" ] || continue
        for f in "$h/.git-credentials" "$h/.config/git/credentials"; do
            [ -f "$f" ] && grep -Eq "$line_re" "$f" 2>/dev/null && rc=0
        done
    done < <(_rzfz_cred_homes)
    if [ -f "${stack}/.git/config" ]; then
        while IFS= read -r url; do
            printf '%s' "$url" | grep -Eq '^https?://[^/@]+@git\.razzfazz\.ai([:/]|$)' && rc=0
        done < <(git config --file "${stack}/.git/config" --get-regexp '^remote\..*\.(url|pushurl)$' 2>/dev/null | awk '{print $2}')
    fi
    [ "$_xt" = 0 ] || set -x
    return "$rc"
}

# #2690: the homes whose git files may hold the PAT: $HOME, and under sudo the
# invoking user's (HOME is /root there, the PAT sits with the admin user).
_rzfz_cred_homes() {
    printf '%s\n' "$HOME"
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        local h
        h=$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)
        [ -n "$h" ] && [ "$h" != "$HOME" ] && printf '%s\n' "$h"
    fi
    return 0
}

# One finding of razzfazz_git_credential_scan: <mode> <kind> <what> <where>
# [<verb in remove mode>, default `removed`].
_rzfz_scan_say() {
    if [ "$1" = remove ]; then
        printf '%s\t%s\t%s\n' "${5:-removed}" "$3" "$4"
    else
        printf '%s\t%s\t%s\n' "$2" "$3" "$4"
    fi
}

# #2690: every place on this box where the Gitea credential (git.razzfazz.ai)
# is kept, or where git would keep one it is given.
#
#   razzfazz_git_credential_scan <stack-dir> list    one line per finding
#   razzfazz_git_credential_scan <stack-dir> remove  remove each, one line each
#
# Output lines are `<kind>\t<what>\t<where>`, kind `cred` (a credential is
# stored there) or `sink` (git or the Settings portal would store or read one
# there). In remove mode the first field is what was done: `removed`, or
# `disabled for git.razzfazz.ai` for a helper kept for other hosts. They carry NAMES only: a key, a file, a remote name. Never a value,
# a user name, a token, or a URL that holds one. xtrace is off inside: a
# traced line would print the values it looks at.
#
# Only the Gitea host is touched; other hosts' entries, URLs, rewrites and
# helpers stay as they are:
#   * credential helpers that serve the Gitea host. One scoped to it is
#     unset; the repo-local unscoped `store` (what init used to set) is unset;
#     any other unscoped helper (a keyring, the admin's own helper) is kept
#     for every other host and DISABLED for the Gitea host only, with an empty
#     `credential.https://git.razzfazz.ai.helper` in its file and in the
#     checkout's .git/config (an empty helper clears the list git built so
#     far, and the checkout's config is read last);
#   * credential files (~/.git-credentials and ~/.config/git/credentials of
#     each home, $XDG_CONFIG_HOME/git/credentials, <stack>/.git-credentials,
#     the --file of a store helper, the file STACK_GIT_CREDENTIALS_PATH
#     names): the Gitea lines go, rewritten IN PLACE (same inode, the file
#     stays). The Settings portal bind-mounts it, a running container keeps
#     reading the old inode of a renamed-over file, and a deleted bind source
#     comes back as a root-owned directory that stops the portal;
#   * the checkout's remote URLs with user info for the Gitea host, and
#     url.<base>.insteadOf rewrites whose base is the Gitea host with user info;
#   * .env: STACK_GIT_CREDENTIALS_PATH, and any value with user info for the
#     Gitea host;
#   * in remove mode, each home's credential-cache daemon is told to exit.
razzfazz_git_credential_scan() {
    { local _rzfz_xt=0 _rzfz_rc=0; case $- in *x*) _rzfz_xt=1; set +x ;; esac; } 2>/dev/null
    _razzfazz_git_credential_scan "$@" || _rzfz_rc=$?
    [ "$_rzfz_xt" = 0 ] || set -x
    return "$_rzfz_rc"
}

_razzfazz_git_credential_scan() {
    local stack="${1:-.}" mode="${2:-list}" host_re='git\.razzfazz\.ai'
    local gitea_scope='credential.https://git.razzfazz.ai.helper'
    local h f key val base cfgs=() cfg_home=() files=() homes=() envf="${stack}/.env"
    local line_re="^[A-Za-z][A-Za-z0-9+.-]*://[^/[:space:]]*@${host_re}([:/[:space:]]|\$)"
    local url_re="^https?://[^/@]+@${host_re}([:/]|\$)"
    local repo="${stack}/.git/config" i
    [ -f "$repo" ] && { cfgs+=("$repo"); cfg_home+=("$HOME"); }
    while IFS= read -r h; do
        [ -n "$h" ] || continue
        homes+=("$h")
        for f in "$h/.gitconfig" "$h/.config/git/config"; do
            [ -f "$f" ] && { cfgs+=("$f"); cfg_home+=("$h"); }
        done
        files+=("$h/.git-credentials" "$h/.config/git/credentials")
    done < <(_rzfz_cred_homes)
    # XDG_CONFIG_HOME belongs to whoever's environment this is; under sudo
    # that may be the admin's (sudo -E). Its config is read like a home's.
    if [ -n "${XDG_CONFIG_HOME:-}" ]; then
        if [ -f "$XDG_CONFIG_HOME/git/config" ]; then
            local xh="$HOME"
            for h in "${homes[@]}"; do case "$XDG_CONFIG_HOME" in "$h"/*) xh="$h" ;; esac; done
            cfgs+=("$XDG_CONFIG_HOME/git/config"); cfg_home+=("$xh")
        fi
        files+=("$XDG_CONFIG_HOME/git/credentials")
    fi
    files+=("${stack}/.git-credentials")
    val=$(read_env_value "$envf" STACK_GIT_CREDENTIALS_PATH 2>/dev/null || true)
    [ -n "$val" ] && files+=("$val")
    # A git config edit renames a new file over the old one: run as root on
    # the admin user's ~/.gitconfig, that would hand the file to root. The
    # owners are put back at the end.
    local owners=""
    if [ "$mode" = remove ] && [ "$(id -u)" = 0 ]; then
        for f in "${cfgs[@]}"; do owners+="$(stat -c '%u:%g' "$f")"$'\t'"$f"$'\n'; done
    fi

    # 1. credential helpers that serve the Gitea host. Per file, in file
    #    order: an empty helper value clears what came before. The checkout's
    #    config is read after every global one, so its empty entry (written
    #    once, repo_cleared) also covers a helper added globally later.
    local repo_cleared=false
    if [ -f "$repo" ] && git config --file "$repo" --get-regexp '^credential\..*helper$' 2>/dev/null \
            | awk -v s="$gitea_scope" 'tolower($1)==tolower(s) && NF==1' | grep -q .; then
        repo_cleared=true
    fi
    local disable_in_repo=false repo_disabled_now=false
    for i in "${!cfgs[@]}"; do
        f="${cfgs[$i]}"
        # every entry that serves the Gitea host, with whether it is still live
        # in this file (an empty value clears what came before)
        local ek=() ev=() el=() k v sub line n
        while IFS= read -r line; do
            k="${line%% *}"; v=""; [ "$k" = "$line" ] || v="${line#* }"
            sub="${k#credential.}"; sub="${sub%helper}"; sub="${sub%.}"
            if [ -n "$sub" ] && ! printf '%s' "$sub" | grep -Eq "^https?://([^/@]*@)?${host_re}([:/].*)?\$"; then
                continue    # scoped to another host
            fi
            if [ -z "$v" ]; then
                for n in "${!el[@]}"; do el[$n]=0; done
            else
                ek+=("$k"); ev+=("$v"); el+=(1)
            fi
        done < <(git config --file "$f" --get-regexp '^credential\..*helper$' 2>/dev/null)
        # A global helper counts even when the checkout's config clears it:
        # git run anywhere else (the admin's home) still reads it.
        local cls what kind p literal
        for n in "${!ek[@]}"; do
            k="${ek[$n]}"; v="${ev[$n]}"
            literal=false
            case "$v" in *password=*) literal=true ;; esac
            # A helper with a password written into its text IS a stored
            # credential, disabled or not: it is never silent.
            if [ "${el[$n]}" = 0 ] && [ "$literal" = false ]; then
                continue
            fi
            case "$v" in
                store*|*git-credential-store*) cls=store ;;
                cache*|*git-credential-cache*) cls=cache ;;
                *) cls=other ;;
            esac
            case "$v" in
                *--file=*) p="${v#*--file=}"; p="${p%% *}"; p="${p/#\~/${cfg_home[$i]}}"
                           files+=("$p") ;;
            esac
            # store and cache are named; any other helper is not, its text
            # may carry a secret (an inline `!f() { echo password=...; }; f`).
            if [ "$literal" = true ]; then kind=cred; what="$k (a credential helper with a password in its text)"
            elif [ "$cls" = other ]; then kind=cred; what="$k (a credential helper)"
            else kind=sink; what="$k=$cls"; fi
            if [ "$k" != credential.helper ]; then
                # scoped to the Gitea host: it serves nothing else
                _rzfz_scan_say "$mode" "$kind" "$what" "$f"
                # non-empty values only: an empty one is a disable we keep
                [ "$mode" = remove ] && { git config --file "$f" --unset-all "$k" '.' 2>/dev/null || true; }
            elif [ "$f" = "$repo" ] && { [ "$cls" = store ] || [ "$literal" = true ]; }; then
                # the checkout's own: the store helper init used to set, and
                # any helper with a password in its text, go
                _rzfz_scan_say "$mode" "$kind" "$what" "$f"
                if [ "$mode" = remove ]; then
                    if [ "$literal" = true ]; then
                        git config --file "$f" --unset-all credential.helper 'password=' 2>/dev/null || true
                    else
                        git config --file "$f" --unset-all credential.helper '(^store|git-credential-store)' 2>/dev/null || true
                    fi
                fi
            elif [ "${el[$n]}" = 0 ]; then
                # global, already disabled for the Gitea host, but it holds a
                # password: named on every run until someone removes it
                _rzfz_scan_say "$mode" "$kind" "$what" "$f" "kept (other hosts; disabled for git.razzfazz.ai), remove by hand"
            else
                _rzfz_scan_say "$mode" "$kind" "$what" "$f" "disabled for git.razzfazz.ai"
                if [ "$mode" = remove ]; then
                    if [ "$f" = "$repo" ]; then
                        if [ "$repo_disabled_now" = false ]; then
                            git config --file "$repo" --add "$gitea_scope" "" 2>/dev/null || true
                            repo_disabled_now=true
                        fi
                    else
                        git config --file "$f" --add "$gitea_scope" "" 2>/dev/null || true
                        disable_in_repo=true
                    fi
                fi
            fi
        done
    done
    if [ "$disable_in_repo" = true ] && [ -f "$repo" ] && [ "$repo_cleared" = false ] \
            && [ "$repo_disabled_now" = false ]; then
        git config --file "$repo" --add "$gitea_scope" "" 2>/dev/null || true
    fi
    if [ "$mode" = remove ]; then
        for h in "${homes[@]}"; do
            for p in "$h/.cache/git/credential/socket" "$h/.git-credential-cache/socket"; do
                [ -S "$p" ] && { timeout 5 git credential-cache --socket "$p" exit >/dev/null 2>&1 || true; }
            done
        done
    fi

    # 2. credential files with a line for the Gitea host.
    local seen=" "
    for f in "${files[@]}"; do
        case "$seen" in *" $f "*) continue ;; esac
        seen+="$f "
        [ -f "$f" ] || continue
        grep -Eq "$line_re" "$f" 2>/dev/null || continue
        _rzfz_scan_say "$mode" cred "git.razzfazz.ai entry" "$f"
        if [ "$mode" = remove ]; then
            local t
            t=$(umask 077; mktemp "${TMPDIR:-/tmp}/rzfz-cred.XXXXXX") || return 1
            { grep -Ev "$line_re" "$f" || true; } > "$t"
            cat "$t" > "$f"
            rm -f "$t"
        fi
    done

    # 3. the checkout's remote URLs with user info for the Gitea host, and
    # 4. insteadOf rewrites whose base is the Gitea host with user info.
    if [ -f "$repo" ]; then
        while IFS= read -r key; do
            [ -n "$key" ] || continue
            if git config --file "$repo" --get-all "$key" 2>/dev/null | grep -Eq "$url_re"; then
                _rzfz_scan_say "$mode" cred "user info in $key" "$repo"
                if [ "$mode" = remove ]; then
                    local urls u
                    urls=$(git config --file "$repo" --get-all "$key")
                    git config --file "$repo" --unset-all "$key" || true
                    while IFS= read -r u; do
                        if printf '%s' "$u" | grep -Eq "$url_re"; then
                            u=$(printf '%s' "$u" | sed -E 's#^(https?://)[^/@]+@#\1#')
                        fi
                        git config --file "$repo" --add "$key" "$u"
                    done <<< "$urls"
                fi
            fi
        done < <(git config --file "$repo" --name-only --get-regexp '^remote\..*\.(url|pushurl)$' 2>/dev/null | sort -u)
    fi
    for f in "${cfgs[@]}"; do
        while IFS= read -r key; do
            [ -n "$key" ] || continue
            base="${key#url.}"; base="${base%.*}"
            printf '%s' "$base" | grep -Eq "$url_re" || continue
            _rzfz_scan_say "$mode" cred "url rewrite with user info (url.<base>.insteadOf)" "$f"
            [ "$mode" = remove ] && { git config --file "$f" --remove-section "url.$base" || true; }
        done < <(git config --file "$f" --name-only --get-regexp '^url\..*\.(insteadof|pushinsteadof)$' 2>/dev/null | sort -u)
    done

    # 5. .env: the portal's credential mount, and any value holding user info
    #    for the Gitea host.
    if [ -f "$envf" ]; then
        val=$(read_env_value "$envf" STACK_GIT_CREDENTIALS_PATH 2>/dev/null || true)
        if [ -n "$val" ]; then
            _rzfz_scan_say "$mode" sink "STACK_GIT_CREDENTIALS_PATH" "$envf"
            [ "$mode" = remove ] && update_env_value "$envf" STACK_GIT_CREDENTIALS_PATH ""
        fi
        while IFS= read -r key; do
            [ -n "$key" ] || continue
            _rzfz_scan_say "$mode" cred "$key" "$envf"
            if [ "$mode" = remove ]; then
                val=$(read_env_value "$envf" "$key")
                update_env_value "$envf" "$key" "$(printf '%s' "$val" | sed -E "s#://[^/@[:space:]]*@(${host_re})#://\\1#g")"
            fi
        done < <(grep -E "^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=.*://[^/@[:space:]]+@${host_re}" "$envf" 2>/dev/null \
                   | sed -E 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=.*/\2/' | sort -u)
    fi
    local own
    while IFS=$'\t' read -r own f; do
        [ -n "$f" ] && [ -e "$f" ] && { chown "$own" "$f" 2>/dev/null || true; }
    done <<< "$owners"
    return 0
}

# Resolve an operator-supplied release version onto the tag that actually
# exists, accepting either spelling (v2026.08-ga.15 == 2026.08-ga.15).
# Prints the real tag name; returns non-zero when neither form exists.
razzfazz_resolve_release_tag() {
    local asked="$1" bare cand
    bare="${asked#v}"
    for cand in "$asked" "$bare" "v${bare}"; do
        if git rev-parse -q --verify "refs/tags/${cand}" >/dev/null 2>&1; then
            printf '%s\n' "$cand"
            return 0
        fi
    done
    return 1
}

# #1466: `rzfz upgrade --target <branch>`. The target path was built for tags:
# `git fetch --tags` brings no branch refs and `git checkout main` lands on the
# LOCAL main — which nothing ever advances — while git itself prints "use git
# pull to update your local branch". Measured on 0.91 (2026-09-05): a
# "successful" upgrade to main left the box on a commit from the day before,
# VERSION 2026.08-ga.15, every merge of the day missing. A branch target must
# therefore mean THE REMOTE TIP, fetched now, checked out detached exactly like
# a tag. A tag of the same name wins (release tags are the primary contract).
#
# razzfazz_upgrade_target_branch <asked>  → prints the bare branch name and
#   returns 0 when <asked> (or origin/<asked>) names a branch on origin — known
#   locally as a remote-tracking ref, or reachable via ls-remote; 1 otherwise.
razzfazz_upgrade_target_branch() {
    local asked="$1" b
    b="${asked#origin/}"
    [ -n "$b" ] || return 1
    git rev-parse -q --verify "refs/tags/${asked}" >/dev/null 2>&1 && return 1
    git rev-parse -q --verify "refs/tags/${b}" >/dev/null 2>&1 && return 1
    # #2706: a local tag in the OTHER spelling (v2026.09-ga.5 == 2026.09-ga.5)
    # is a tag too — answered here, before the ls-remote below asks origin.
    razzfazz_resolve_release_tag "$b" >/dev/null 2>&1 && return 1
    # #2287: a tag wins even when this box has never fetched it. The two checks
    # above only see LOCAL tags, and a box upgrading to a new release never
    # holds the new tag yet — which made the cold-tag case (the customer case)
    # the one that fell through. Ask the remote, by EXACT refpath.
    razzfazz_git_origin ls-remote --exit-code --tags origin "refs/tags/${b}" >/dev/null 2>&1 && return 1
    # #2287: match the head by EXACT refpath too. `git ls-remote <pattern>`
    # globs on the ref TAIL, so the bare name 2026.09-rc12 matched our own
    # refs/heads/release/2026.09-rc12 and turned the TAG into a "branch".
    if git rev-parse -q --verify "refs/remotes/origin/${b}" >/dev/null 2>&1 \
       || razzfazz_git_origin ls-remote --exit-code --heads origin "refs/heads/${b}" >/dev/null 2>&1; then
        printf '%s\n' "$b"
        return 0
    fi
    return 1
}

# razzfazz_upgrade_fetch_branch <branch> → fetches origin/<branch> NOW and
#   prints the ref to check out (`origin/<branch>`). A fetch that fails is a
#   hard failure: falling back to the last-known remote-tracking ref would be
#   the same illusion this exists to end (an offline box upgrades by tag or
#   bundle, never by branch).
razzfazz_upgrade_fetch_branch() {
    # Exit codes are load-bearing — the caller turns them into different
    # operator instructions (#2287): 1 = the fetch itself failed (no remote,
    # no credentials, offline); 2 = the fetch SUCCEEDED but origin/<b> is no
    # branch. Conflating them told 0.79 "git fetch failed" when the fetch had
    # returned 0 and put the wanted object in FETCH_HEAD.
    local b="$1"
    razzfazz_git_origin fetch origin "${b}" >/dev/null 2>&1 || return 1
    git rev-parse -q --verify "refs/remotes/origin/${b}" >/dev/null 2>&1 || return 2
    printf 'origin/%s\n' "$b"
}

# ==============================================================================
# #252: agent-safe replacement for `docker compose up --remove-orphans`
# ==============================================================================
# Socket-provisioned agents (agent-manager, mcp-manager, gpustack runners)
# carry the compose project label but NO compose service record — to
# `--remove-orphans` they are indistinguishable from a renamed service's
# leftovers, so ONE compose behavior change away from deleting every user
# agent on the box. This replicates the flag's actual purpose (clean up
# containers whose service no longer exists after a rename) while NEVER
# touching `razzfazz.managed=true`.
remove_compose_orphans_safe() {
    local proj services name svc managed
    proj=$(docker compose config 2>/dev/null | awk '/^name:/{print $2; exit}')
    [ -n "$proj" ] || proj=$(basename "$(pwd)")
    services=$(docker compose config --services 2>/dev/null) || return 0
    [ -n "$services" ] || return 0
    # Separator is '|', NOT tab: tab is IFS whitespace, and a managed agent's
    # EMPTY service label collapses "name<tab><tab>true" into two fields —
    # 'true' lands in svc, managed reads empty, and the guard deletes exactly
    # the container it exists to protect (caught by the behavioral test).
    docker ps -a --filter "label=com.docker.compose.project=${proj}" \
        --format '{{.Names}}|{{.Label "com.docker.compose.service"}}|{{.Label "razzfazz.managed"}}' 2>/dev/null \
    | while IFS='|' read -r name svc managed; do
        [ -n "$name" ] || continue
        # the landmine guard: managed agents are NEVER orphans
        [ "$managed" = "true" ] && continue
        # a container whose service still exists is not an orphan
        [ -n "$svc" ] && printf '%s\n' "$services" | grep -qx "$svc" && continue
        echo "  #252: removing compose orphan '${name}' (service '${svc:-<none>}' no longer exists)"
        docker rm -f "$name" >/dev/null 2>&1 || true
    done
    return 0
}

# ------------------------------------------------------------------------------
# #2977 - mask credentials inside URLs in text that leaves the box
# ------------------------------------------------------------------------------
# stdin -> stdout: `scheme://user:pass@host` becomes `scheme://***@host` for any
# scheme (redis, rediss, postgres, postgresql, amqp, https, ...). The userinfo
# runs to the first `/` or whitespace and may contain `@` (the last one ends it).
# Same rule as razzfazz_common.log_redaction.redact_url_credentials (Python);
# tests/unit/common/test_2977_url_credentials_are_redacted.py runs both. Pipe
# every `docker logs` that is written to a file or a bundle through it.
redact_url_credentials() {
    LC_ALL=C sed -E "s#([A-Za-z][A-Za-z0-9+.-]*://)[^/[:space:]'\"<>]+@#\1***@#g"
}

# #3367: what every collector pipes container output through. The URL rule
# above, then komodo-core's `Api key created … key="<KOMODO_API_KEY>"` line
# (an upstream INFO line with no switch of its own): the key is masked, the
# line kept. Same rule as razzfazz_common.log_redaction.redact_log_secrets.
# #3436 L-24: the marker only SELECTS the line (the `/…/` address); on it EVERY
# `key=` value is masked (`g`), a JSON-escaped `key=\"…\"` too. The old
# `s#(marker.*key=…)…#` masked only the last key and backtracked per marker.
redact_log_secrets() {
    local e=$'\033'
    redact_url_credentials | LC_ALL=C sed -E \
        "/Api key created/s#(([[:space:]]|${e}\[[0-9;]*m)key(${e}\[[0-9;]*m)*=(${e}\[[0-9;]*m)*(\\\\?\")?)[^\"\\\\[:space:]${e}]+#\1***#g"
}

# ------------------------------------------------------------------------------
# #2344 — one bounded retry for a dependency that is slow to turn healthy
# ------------------------------------------------------------------------------
# Keep what a failed install used to destroy on teardown: the dependency's health
# history, state and last log lines. SECRET RISK: `docker logs` output is whatever
# the container printed and can contain tokens or credentials, so the file is
# created 0600 (umask 077 + chmod) and only the newest 10 are kept.
razzfazz_capture_container_evidence() {
    local name="${1:-}" dir="backups/logs" f old
    # The name was parsed out of compose's error text: it is input.
    [[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || return 0
    mkdir -p "$dir" 2>/dev/null || return 0
    f="${dir}/dependency-${name}-$(date -u +%Y%m%dT%H%M%SZ).log"
    (
        umask 077
        {
            echo "== docker inspect .State.Health (${name})"
            docker inspect --format '{{json .State.Health}}' "$name" 2>&1 || true
            echo "== docker inspect .State"
            docker inspect --format '{{json .State}}' "$name" 2>&1 || true
            echo "== docker logs --tail 200"
            docker logs --tail 200 "$name" 2>&1 | redact_log_secrets || true
        } > "$f" 2>&1
    ) || true
    chmod 600 "$f" 2>/dev/null || true
    # Cap: keep the newest 10 evidence files (names are ours, glob-safe).
    ls -1t "$dir"/dependency-*.log 2>/dev/null | tail -n +11 | while IFS= read -r old; do
        rm -f -- "$old"
    done
    print_warning "Evidence for ${name} kept in ${f}"
    return 0
}

# `docker compose up -d` aborts the whole start when ONE dependency is slow to turn
# healthy ("dependency failed to start: container X is unhealthy" / "exited (N)").
# Give that dependency ONE bounded second chance. Any other failure returns
# compose's rc unchanged, with no retry. Never loops: at most two `up` runs.
razzfazz_compose_up_with_dependency_retry() {
    local out rcf rc=0 deps dep st deadline remaining
    local wait_s="${RAZZFAZZ_DEP_RETRY_WAIT_S:-300}" poll="${RAZZFAZZ_DEP_RETRY_POLL_S:-10}"
    [[ "$wait_s" =~ ^[0-9]+$ ]] || wait_s=300
    [[ "$poll" =~ ^[0-9]+$ ]] || poll=10
    out=$(mktemp "${TMPDIR:-/tmp}/rzfz-up.XXXXXX") || { docker compose up -d "$@"; return $?; }
    rcf="${out}.rc"
    # Stream to the operator while keeping a copy; the rc travels via a file so it
    # survives the pipe under set -e / pipefail alike.
    { _rc=0; docker compose up -d "$@" 2>&1 || _rc=$?; echo "$_rc" > "$rcf"; } | tee "$out" || true
    rc=$(cat "$rcf" 2>/dev/null || echo 1)
    [[ "$rc" =~ ^[0-9]+$ ]] || rc=1
    if [ "$rc" -eq 0 ]; then rm -f "$out" "$rcf"; return 0; fi
    # compose v2.40 wording (strings of the pinned binary): "dependency failed to
    # start: container %s is unhealthy" / "container %s exited (%d)" / "container
    # %s is restarting".
    deps=$(sed -nE 's/.*dependency failed to start: container ([^[:space:]]+) (is unhealthy|is restarting|exited \([0-9]+\)).*/\1/p' "$out" 2>/dev/null || true)
    rm -f "$out" "$rcf"
    dep="${deps%%$'\n'*}"
    [[ "$dep" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || return "$rc"
    razzfazz_capture_container_evidence "$dep"
    print_warning "${dep} did not become healthy in time. Waiting up to ${wait_s}s, then starting the stack once more."
    deadline=$((SECONDS + wait_s))
    while :; do
        st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$dep" 2>/dev/null || true)
        case "$st" in
            healthy|running) break ;;
            exited) docker start "$dep" >/dev/null 2>&1 || true ;;
        esac
        remaining=$((deadline - SECONDS))
        [ "$remaining" -gt 0 ] || break
        [ "$poll" -le "$remaining" ] || poll="$remaining"
        sleep "$poll"
    done
    rc=0
    docker compose up -d "$@" || rc=$?
    if [ "$rc" -ne 0 ]; then
        razzfazz_capture_container_evidence "$dep"
        print_error "${dep} still blocks the stack. See the evidence file above."
    fi
    return "$rc"
}


# ------------------------------------------------------------------------------
# #707 / #3336 — guarded orphan sweep for the ephemeral-test-postgres projects
# ------------------------------------------------------------------------------
# `cli/test.sh` reaps leaked `pytest-eph-pg-*` projects at the start of every
# run (a SIGKILL'd pytest leaves its postgres behind; cumulative leaks caused
# the 2026-05-16 OOM cascade). The bare version of that sweep -- `docker ps -a`
# + `docker rm -f` on everything matching the prefix -- also deleted the LIVE
# session databases of any job already inside pytest on the same runner
# (CI 1836: containers gone from the daemon, `no such object`, no exit state).
#
# #3336: state + age was still not ownership. A full gate keeps its databases
# for 17-40 minutes, so every `cli/test.sh` start on the same host reaped the
# databases of any session older than 600 s -- two gates lost their api tier
# that way. Each ephemeral project now carries the label `razzfazz.test.owner`
# = `<pid>:<starttime>:<boot_id>:<pid-ns inode>` of the pytest process that
# started it (tests/conftest.py exports it as RAZZFAZZ_TEST_OWNER, and
# tests/fixtures/ephemeral-postgres.yml interpolates it). The policy, kept
# identical to the conftest sweep:
#
#   exited / dead                  -> reapable at any age
#   owner alive                    -> LEAVE ALONE, whatever its age
#   owner gone (or its PID reused) -> reapable at any age
#   owner in another boot / PID ns -> cannot be judged from here: reapable
#                                     only past RAZZFAZZ_TEST_ORPHAN_FOREIGN_MAX_AGE_S
#   no owner label                 -> the old #707 rule: older than
#                                     RAZZFAZZ_TEST_ORPHAN_MIN_AGE_S
#   age unreadable                 -> treated as young, i.e. left alone
RAZZFAZZ_TEST_ORPHAN_PREFIX="pytest-eph-pg-"

# Field 22 of /proc/<pid>/stat (clock ticks after boot). The comm field may
# hold spaces and parentheses, so everything up to the LAST ") " goes first.
razzfazz_test_proc_starttime() {
    local stat
    stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 0
    stat="${stat##*) }"
    # shellcheck disable=SC2086
    set -- $stat
    printf '%s\n' "${20:-}"
}

# The owner label for the CURRENT shell, in the conftest's format. Empty when
# /proc cannot tell (then the sweeps fall back to the age rule).
razzfazz_test_owner_label() {
    local pid="${1:-$$}" start boot ns
    start=$(razzfazz_test_proc_starttime "$pid")
    boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
    ns=$(readlink "/proc/$pid/ns/pid" 2>/dev/null | tr -cd '0-9')
    [ -n "$start" ] && [ -n "$boot" ] && [ -n "$ns" ] || return 0
    printf '%s:%s:%s:%s\n' "$pid" "$start" "$boot" "$ns"
}

# $1 = a `razzfazz.test.owner` label; prints alive | dead | foreign | unknown.
razzfazz_test_owner_state() {
    local label="${1:-}" pid start boot ns here_boot here_ns cur err
    IFS=: read -r pid start boot ns <<< "$label"
    case "$pid" in ''|*[!0-9]*) echo unknown; return 0 ;; esac
    case "$start" in ''|*[!0-9]*) echo unknown; return 0 ;; esac
    if [ -z "$boot" ] || [ -z "$ns" ]; then echo unknown; return 0; fi
    here_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
    here_ns=$(readlink "/proc/$$/ns/pid" 2>/dev/null | tr -cd '0-9')
    if [ -z "$here_boot" ] || [ -z "$here_ns" ] \
        || [ "$boot" != "$here_boot" ] || [ "$ns" != "$here_ns" ]; then
        echo foreign; return 0
    fi
    cur=$(razzfazz_test_proc_starttime "$pid")
    if [ -n "$cur" ]; then
        # same PID, other start time: the owner died and the PID was reused
        if [ "$cur" = "$start" ]; then echo alive; else echo dead; fi
        return 0
    fi
    # No readable /proc entry: gone, or hidden from us (hidepid). kill -0
    # tells the two apart: EPERM means the process exists.
    if err=$(LC_ALL=C kill -0 "$pid" 2>&1); then echo alive; return 0; fi
    case "$err" in
        *"not permitted"*) echo alive ;;
        *) echo dead ;;
    esac
}

razzfazz_reapable_test_containers() {
    # stdin: `<name>\t<state>\t<created_epoch>[\t<owner label>]` per line;
    # stdout: reapable names. An empty field must be written as `-`: IFS tab
    # is whitespace to `read`, so two tabs in a row would shift the fields.
    # $1 = "now" epoch (defaults to the current time; a parameter so the policy
    # is unit-testable without sleeping).
    local now="${1:-$(date +%s)}"
    local min_age="${RAZZFAZZ_TEST_ORPHAN_MIN_AGE_S:-600}"
    local max_age="${RAZZFAZZ_TEST_ORPHAN_FOREIGN_MAX_AGE_S:-21600}"
    local name state created owner need
    while IFS=$'\t' read -r name state created owner; do
        [ -n "$name" ] || continue
        case "$name" in
            "${RAZZFAZZ_TEST_ORPHAN_PREFIX}"*) ;;
            *) continue ;;
        esac
        # #732: the line runs between TERMINAL and TRANSIENT states, not
        # between "running" and "not running". `created` is the state every
        # container passes through on its way up -- a one-second-old postgres
        # sits there while compose wires it -- so reaping it without an age
        # check kills the very databases a sibling run is starting. With eight
        # xdist workers each spinning a postgres, that window is hit constantly
        # (CI 1882: eight networks, zero containers, and the one still visible
        # stuck in `Created`). Only a container that has finished dying is
        # reapable on sight.
        case "$state" in
            exited|dead) printf '%s\n' "$name"; continue ;;
        esac
        # #3336: ownership, not age, decides for a labelled container.
        need="$min_age"
        case "$owner" in
            ''|-) ;;
            *)
                case "$(razzfazz_test_owner_state "$owner")" in
                    alive) continue ;;
                    dead) printf '%s\n' "$name"; continue ;;
                    foreign) need="$max_age" ;;
                esac
                ;;
        esac
        # unreadable timestamp -> fail safe, treat as young
        [ -n "$created" ] && [ "$created" -ge 0 ] 2>/dev/null || continue
        [ $(( now - created )) -ge "$need" ] || continue
        printf '%s\n' "$name"
    done
}

razzfazz_sweep_orphan_test_containers() {
    command -v docker >/dev/null 2>&1 || return 0
    local rows names
    rows=$(docker ps -a --filter "name=^${RAZZFAZZ_TEST_ORPHAN_PREFIX}" \
        --format '{{.Names}}	{{.State}}	{{.CreatedAt}}	{{.Label "razzfazz.test.owner"}}' \
        2>/dev/null || true)
    [ -n "$rows" ] || return 0
    # CreatedAt is '2026-08-25 10:53:10 +0000 UTC' -> epoch. An unreadable one
    # becomes `-`, which the filter treats as "young"; a missing label too.
    rows=$(printf '%s\n' "$rows" | awk -F'\t' 'BEGIN{OFS="\t"}
        { ep = ""; cmd = "date -d \"" $3 "\" +%s 2>/dev/null"; cmd | getline ep; close(cmd);
          if (ep == "") ep = "-"; own = ($4 == "") ? "-" : $4;
          print $1, $2, ep, own }')
    names=$(printf '%s\n' "$rows" | razzfazz_reapable_test_containers)
    [ -n "$names" ] || return 0
    echo "razzfazz-test: sweeping $(printf '%s\n' "$names" | wc -l) orphan ${RAZZFAZZ_TEST_ORPHAN_PREFIX}* container(s) (stopped, owner process gone, or unlabelled and older than ${RAZZFAZZ_TEST_ORPHAN_MIN_AGE_S:-600}s)" >&2
    printf '%s\n' "$names" | xargs -r docker rm -f >/dev/null 2>&1 || true
    return 0
}

# Each reaped project also leaves its compose network behind, and every leaked
# network holds a subnet from the default address pool -- enough of them and
# `compose up` fails with "all predefined address pools have been fully
# subnetted" (#160). The conftest only removes the networks of the containers
# IT just reaped; a network whose container died some other way (external
# `docker rm`, daemon restart) is never cleaned.
#
# Two conditions, not one. "Unattached" alone is NOT proof that nothing needs
# the network: `docker compose up` creates the network FIRST and the container
# after it, so a job that is coming up right now has an empty network for
# ~100-500 ms. Sweeping in that window pulls the network out from under it and
# its `compose up` dies with "network ... not found" -- the very cross-job
# interference this helper exists to end, one size smaller. So a network must
# ALSO be older than the same age guard the container sweep uses.
razzfazz_sweep_orphan_test_networks() {
    command -v docker >/dev/null 2>&1 || return 0
    local nets
    nets=$(docker network ls --filter "name=^${RAZZFAZZ_TEST_ORPHAN_PREFIX}" \
        --format '{{.Name}}' 2>/dev/null || true)
    [ -n "$nets" ] || return 0
    local n info attached created created_epoch now min_age orphans=""
    now=$(date +%s)
    min_age="${RAZZFAZZ_TEST_ORPHAN_MIN_AGE_S:-600}"
    while IFS= read -r n; do
        [ -n "$n" ] || continue
        info=$(docker network inspect "$n" --format '{{len .Containers}}|{{.Created}}' 2>/dev/null || echo "")
        [ -n "$info" ] || continue
        attached="${info%%|*}"
        created="${info#*|}"
        [ "$attached" = "0" ] || continue
        # '2026-08-25 20:35:12.694302940 +0200 CEST' -> the first three fields
        # are what `date -d` accepts (the trailing zone NAME makes it choke).
        created_epoch=$(date -d "$(printf '%s' "$created" | awk '{print $1, $2, $3}')" +%s 2>/dev/null || echo "")
        # unreadable timestamp -> treat as young and leave it alone (fail safe)
        [ -n "$created_epoch" ] || continue
        [ $(( now - created_epoch )) -ge "$min_age" ] || continue
        orphans="${orphans}${n}"$'\n'
    done <<< "$nets"
    orphans=$(printf '%s' "$orphans" | sed '/^$/d')
    [ -n "$orphans" ] || return 0
    echo "razzfazz-test: sweeping $(printf '%s\n' "$orphans" | wc -l) orphan ${RAZZFAZZ_TEST_ORPHAN_PREFIX}* network(s) (unattached and older than ${RAZZFAZZ_TEST_ORPHAN_MIN_AGE_S:-600}s)" >&2
    printf '%s\n' "$orphans" | xargs -r docker network rm >/dev/null 2>&1 || true
    return 0
}

# sudo that may PROMPT only when a person can answer. sudo reads the password
# from the controlling terminal; without one (setsid/nohup, CI, a detached SSH)
# it is `sudo -n`, so a missing grant fails at once into the caller's documented
# fallback instead of dying or waiting. A /dev/tty that opens but is not
# interactive (`ssh -t` with stdin redirected) still counts as a terminal: sudo
# prompts on /dev/tty itself, so that prompt can be answered.
razzfazz_sudo() {
    if { : </dev/tty; } 2>/dev/null; then
        sudo "$@"
    else
        sudo -n "$@"
    fi
}

# Host steps that could not run (no grant, refused, failed) are collected here
# and printed again by razzfazz_print_deferred_host_steps at the very end of a
# run, where the operator reads — a mid-run warning scrolls away. Each entry is
# "<what><TAB><exact command to run by hand>[<TAB><check>[<TAB><what to do if it fails>]]".
razzfazz_defer_host_step() {
    RAZZFAZZ_DEFERRED_HOST_STEPS="${RAZZFAZZ_DEFERRED_HOST_STEPS:-}${1}"$'\t'"${2}"$'\t'"${3:-}"$'\t'"${4:-}"$'\n'
}

razzfazz_print_deferred_host_steps() {
    [ -n "${RAZZFAZZ_DEFERRED_HOST_STEPS:-}" ] || return 0
    local what cmd check onfail
    echo ""
    print_warning "HOST STEPS NOT APPLIED — the stack runs, but these host settings are missing. Run each one by hand:"
    while IFS=$'\t' read -r what cmd check onfail; do
        [ -n "$what" ] || continue
        print_warning "  ${what}"
        print_info    "    run:   ${cmd}"
        [ -z "$check" ] || print_info "    check: ${check}"
        [ -z "$onfail" ] || print_info "    ${onfail}"
    done <<< "$RAZZFAZZ_DEFERRED_HOST_STEPS"
    echo ""
}

# Docker copies its no-new-privileges DEFAULT into a container only when it
# CREATES the container, and live-restore keeps containers running across a
# daemon restart, so the daemon's setting says nothing about the running stack.
# Prints "<service> <NoNewPrivs 0|1>" for the first RUNNING compose service that
# relies on that default (no no-new-privileges in its own security_opt), read
# from /proc/<pid>/status; prints nothing when none can be read. Run it in the
# stack directory. RAZZFAZZ_TEST_PROC: test seam for /proc.
razzfazz_nnp_sample() {
    local svcs running svc cid pid v
    local t="${RAZZFAZZ_NNP_TIMEOUT_S:-20}"   # a wedged daemon must not hang the caller
    svcs=$(timeout "$t" docker compose config --format json 2>/dev/null | python3 -c '
import json, sys
for name, svc in (json.load(sys.stdin).get("services") or {}).items():
    if not any("no-new-privileges" in str(o) for o in (svc.get("security_opt") or [])):
        print(name)' 2>/dev/null) || return 0
    running=$(timeout "$t" docker compose ps --services --status running 2>/dev/null) || return 0
    for svc in $svcs; do
        grep -qxF -- "$svc" <<<"$running" || continue
        cid=""; read -r cid < <(timeout "$t" docker compose ps -q "$svc" 2>/dev/null) || true
        [ -n "$cid" ] || continue
        pid=$(timeout "$t" docker inspect -f '{{.State.Pid}}' "$cid" 2>/dev/null) || continue
        case "$pid" in ''|0|*[!0-9]*) continue ;; esac
        v=$(awk '/^NoNewPrivs:/ {print $2}' "${RAZZFAZZ_TEST_PROC:-/proc}/${pid}/status" 2>/dev/null) || continue
        case "$v" in 0|1) echo "$svc $v"; return 0 ;; esac
    done
    return 0
}

# Harden the host (ufw, fail2ban, sysctl, auditd, Docker no-new-privileges;
# SSH untouched) the same way on install and upgrade.
#   0 = hardened, now or before (marker ${RAZZFAZZ_HOST_HARDENED_MARKER}).
#   1 = not hardened, with a WARN naming the command. Never prompts without a
#       terminal: no terminal and no passwordless sudo is DEFERRED at once.
# Sets RAZZFAZZ_HOST_HARDENING_STATE (hardened|deferred|failed|refused-live|
# missing) and, on failed/refused-live, RAZZFAZZ_HOST_HARDENING_LOG (the full
# harden-host output; kept, the newest 5 of them).
# harden-host's stdin is /dev/null: its log goes to a file, so a question it
# asked could not be seen (sudo's own password prompt uses the terminal).
razzfazz_run_host_hardening() {
    local dir="$1" marker="${RAZZFAZZ_HOST_HARDENED_MARKER:-/etc/razzfazz/host-hardened}" rc=0 log
    RAZZFAZZ_HOST_HARDENING_LOG=""
    if [ -f "$marker" ]; then
        RAZZFAZZ_HOST_HARDENING_STATE=hardened
        print_substep "Host hardening already applied ($(grep '^hardened_at=' "$marker" 2>/dev/null | cut -d= -f2-))."
        return 0
    fi
    if [ ! -x "${dir}/scripts/harden-host.sh" ]; then
        RAZZFAZZ_HOST_HARDENING_STATE=missing
        print_warning "scripts/harden-host.sh not found — the host is NOT hardened."
        return 1
    fi
    if ! { : </dev/tty; } 2>/dev/null && ! sudo -n true 2>/dev/null; then
        RAZZFAZZ_HOST_HARDENING_STATE=deferred
        print_warning "Host hardening DEFERRED: no terminal and no passwordless sudo, so harden-host.sh cannot run unattended."
        print_info    "  Run it once, in a maintenance window when the stack is running (it goes down so every container is recreated under the new Docker defaults): cd ${dir} && ./rzfz down && sudo bash ${dir}/scripts/harden-host.sh && ./rzfz start"
        print_info    "  If harden-host fails, the stack stays down: run ./rzfz start, fix the cause from the log, and repeat."
        return 1
    fi
    print_step "Hardening the host (scripts/harden-host.sh: ufw, fail2ban, sysctl, auditd, Docker no-new-privileges; SSH untouched)..."
    ls -1t "${RZFZ_LOG_DIR:-${TMPDIR:-/tmp}}"/harden-host.*.log 2>/dev/null | tail -n +5 | xargs -r rm -f -- || true
    log=$(mktemp "${RZFZ_LOG_DIR:-${TMPDIR:-/tmp}}/harden-host.XXXXXX.log" 2>/dev/null) || log=$(mktemp)
    razzfazz_sudo bash "${dir}/scripts/harden-host.sh" </dev/null >"$log" 2>&1 || rc=$?
    tail -n 30 "$log" || true
    if [ "$rc" -eq 0 ]; then
        rm -f "$log"
        RAZZFAZZ_HOST_HARDENING_STATE=hardened
        print_success "Host hardened."
        return 0
    fi
    RAZZFAZZ_HOST_HARDENING_STATE=failed
    grep -q 'Refusing to harden a LIVE stack' "$log" 2>/dev/null && RAZZFAZZ_HOST_HARDENING_STATE=refused-live
    RAZZFAZZ_HOST_HARDENING_LOG="$log"
    print_warning "harden-host.sh did not complete (exit ${rc}) — the host is NOT hardened. Full log: ${log}"
    print_info    "  Run it once, in a maintenance window when the stack is running (it goes down so every container is recreated under the new Docker defaults): cd ${dir} && ./rzfz down && sudo bash ${dir}/scripts/harden-host.sh && ./rzfz start"
    print_info    "  If harden-host fails, the stack stays down: run ./rzfz start, fix the cause from the log, and repeat."
    return 1
}

# Block cloud metadata addresses (169.254.0.0/16, 100.100.100.200,
# fd00:ec2::254) for every container in Docker's DOCKER-USER chain, on install
# AND on every upgrade, independent of the host-hardened marker: a box hardened
# before this rule existed gets it too. scripts/metadata-block.sh install is
# idempotent (own chain, flushed and refilled; one jump) and enables a unit
# that re-applies it after each Docker start, so it survives a reboot.
#   0 = applied. 1 = not applied, with a WARN naming the command; never
#   prompts without a terminal (no terminal and no passwordless sudo = deferred).
# Sets RAZZFAZZ_METADATA_BLOCK_STATE (applied|deferred|failed|missing).
# The optional second argument is the env file the allow list
# (METADATA_BLOCK_LINKLOCAL_ALLOW) is read from: <dir>/.env on a stack box,
# .env.node on a thin worker (`rzfz node-init`, #3306).
razzfazz_apply_metadata_block() {
    local dir="$1" env_file="${2:-$1/.env}" rc=0
    if [ ! -f "${dir}/scripts/metadata-block.sh" ]; then
        RAZZFAZZ_METADATA_BLOCK_STATE=missing
        print_warning "scripts/metadata-block.sh not found — containers can reach cloud metadata addresses."
        return 1
    fi
    if ! { : </dev/tty; } 2>/dev/null && ! sudo -n true 2>/dev/null; then
        RAZZFAZZ_METADATA_BLOCK_STATE=deferred
        print_warning "Cloud metadata block DEFERRED: no terminal and no passwordless sudo, so the DOCKER-USER rule cannot be installed unattended."
        print_info    "  Run once: sudo bash ${dir}/scripts/metadata-block.sh install"
        return 1
    fi
    print_step "Blocking cloud metadata addresses for all containers (DOCKER-USER)..."
    razzfazz_sudo env RAZZFAZZ_ENV_FILE="${env_file}" bash "${dir}/scripts/metadata-block.sh" install || rc=$?
    if [ "$rc" -eq 0 ]; then
        RAZZFAZZ_METADATA_BLOCK_STATE=applied
        print_success "Cloud metadata addresses blocked for containers (re-applied after every Docker start)."
        return 0
    fi
    RAZZFAZZ_METADATA_BLOCK_STATE=failed
    print_warning "Cloud metadata block NOT applied (exit ${rc}). Run once: sudo bash ${dir}/scripts/metadata-block.sh install"
    return 1
}

# #693 — build-phase RAM safety (the 0.208 2026-08-24 global-OOM class)
# ------------------------------------------------------------------------------
# One BuildKit trace building EVERY custom image in parallel (vulkan cmake/
# glslc, moltis rustc x5, hermes, dify-web, licenses) next to the running
# full stack pushed a 30-GB box into a global OOM freeze. Two client-side
# levers, shared by upgrade / rollback / init build sites:
#
#   razzfazz_build_parallelism  -> how many compose build jobs may run at
#       once: min(nproc, MemAvailable/4GB), floor 1. Exported as
#       COMPOSE_PARALLEL_LIMIT (compose v2's classic builder honours it;
#       the default builder, bake, does NOT — #2560: razzfazz_compose_build
#       serializes a limit of 1 itself). Big boxes keep nproc; small boxes serialize.
#       Override: RAZZFAZZ_BUILD_PARALLELISM in .env / environment.
#
#   razzfazz_build_ram_guard    -> loud pre-build WARN below a MemAvailable
#       threshold (default 8 GB, override RAZZFAZZ_BUILD_MIN_FREE_GB) naming
#       the two escape hatches (--stop-stack-for-build / stopping big model
#       deployments). Never blocks: an unattended upgrade must not hang on
#       a prompt — it proceeds with throttled parallelism instead.
razzfazz_mem_available_gb() {
    # test seam: unit tests fake the reading (no portable way to stub
    # /proc/meminfo); production never sets this.
    if [ -n "${RAZZFAZZ_TEST_MEM_AVAILABLE_GB:-}" ]; then
        echo "${RAZZFAZZ_TEST_MEM_AVAILABLE_GB}"; return 0
    fi
    # #2560: RAZZFAZZ_TEST_MEMINFO points the real parse at a fixture (or at a
    # missing file: a box without /proc/meminfo). Unreadable or non-numeric =
    # "" (unknown) — never a crash under the caller's set -e.
    local kb
    kb=$(awk '/^MemAvailable:/ {print $2; exit}' "${RAZZFAZZ_TEST_MEMINFO:-/proc/meminfo}" 2>/dev/null) || kb=""
    case "$kb" in ''|*[!0-9]*) echo ""; return 0 ;; esac
    echo $(( kb / 1024 / 1024 ))
}

razzfazz_build_parallelism() {
    local override cores mem_gb by_mem jobs
    override=$(grep -m1 '^RAZZFAZZ_BUILD_PARALLELISM=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    override="${RAZZFAZZ_BUILD_PARALLELISM:-$override}"
    if [ -n "$override" ] && [ "$override" -ge 1 ] 2>/dev/null; then
        echo "$override"; return 0
    fi
    cores=$(razzfazz_nproc)
    mem_gb=$(razzfazz_mem_available_gb)
    if [ -z "$mem_gb" ]; then
        echo "$cores"; return 0
    fi
    by_mem=$(( mem_gb / 4 ))
    [ "$by_mem" -ge 1 ] || by_mem=1
    jobs=$cores
    [ "$by_mem" -lt "$jobs" ] && jobs=$by_mem
    echo "$jobs"
}

razzfazz_build_ram_guard() {
    local min_gb mem_gb
    min_gb=$(grep -m1 '^RAZZFAZZ_BUILD_MIN_FREE_GB=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    min_gb="${RAZZFAZZ_BUILD_MIN_FREE_GB:-${min_gb:-8}}"
    mem_gb=$(razzfazz_mem_available_gb)
    [ -n "$mem_gb" ] || return 0
    if [ "$mem_gb" -lt "$min_gb" ] 2>/dev/null; then
        print_warning "LOW MEMORY for the build phase: MemAvailable=${mem_gb}GB < ${min_gb}GB while the stack is running."
        print_warning "Builds proceed with parallelism $(razzfazz_build_parallelism) (COMPOSE_PARALLEL_LIMIT). To free RAM first:"
        print_warning "  - rzfz upgrade --stop-stack-for-build   (stops the stack for the build phase; restart_stack brings it back)"
        print_warning "  - or stop large model deployments before upgrading (#693)."
        return 1
    fi
    return 0
}

# #2560: the #693 levers bound how many SERVICES build at once. One Moltis build
# alone ran cargo -j$(nproc) — 64 rustc at 0.5-2 GB each beside a running stack
# (PSI full ~93 %, swap 7/7 GB on a fleet box, 2026.09-ga.2 -> ga.4). These
# bound the jobs INSIDE one build, and the services across builds while the
# stack is up. Every `docker compose build` in cli/ scripts/ core/ goes through
# razzfazz_compose_build (guard: tests/unit/scripts/
# test_2560_builds_are_bounded_inside_and_across.py).
razzfazz_nproc() {
    # Test seam RAZZFAZZ_TEST_NPROC; no or garbled nproc = 4 (the #693 default).
    local n="${RAZZFAZZ_TEST_NPROC:-}"
    if [ -z "$n" ]; then
        n=$(nproc 2>/dev/null) || n=""
    fi
    case "$n" in ''|*[!0-9]*|0) n=4 ;; esac
    echo "$n"
}

# Per-build job count: the largest power of two <= min(nproc, MemAvailable/2 GB)
# (~2 GB per rustc/cc1plus job), floor 1. MemAvailable unknown (no
# /proc/meminfo) = at most 2 jobs. Override: RAZZFAZZ_BUILD_JOBS in the PROCESS
# environment (a positive integer, taken as is) — deliberately not a .env key.
# Why a power of two: the value is a build ARG, and an ARG is part of the cache
# key of the RUN it feeds. A value that tracked MemAvailable to the GB would
# rebuild Moltis on every upgrade; buckets keep the cache warm on one box. A
# box whose MemAvailable sits near a boundary (31 vs 33 GB) alternates between
# two buckets; BuildKit keeps both variants until GC, so that costs one miss each.
razzfazz_build_jobs() {
    local override cores mem_gb jobs p=1
    override="${RAZZFAZZ_BUILD_JOBS:-}"
    if [ -n "$override" ] && [ "$override" -ge 1 ] 2>/dev/null; then
        echo "$override"; return 0
    fi
    cores=$(razzfazz_nproc)
    mem_gb=$(razzfazz_mem_available_gb)
    jobs=$cores
    if [ -z "$mem_gb" ]; then
        if [ "$jobs" -gt 2 ]; then jobs=2; fi
    elif [ $(( mem_gb / 2 )) -lt "$jobs" ]; then
        jobs=$(( mem_gb / 2 ))
    fi
    if [ "$jobs" -lt 1 ]; then jobs=1; fi
    while [ $(( p * 2 )) -le "$jobs" ]; do p=$(( p * 2 )); done
    echo "$p"
}

# 0 when any container of this compose project is running (honours the
# caller's COMPOSE_FILE/COMPOSE_PROFILES). Test seam: RAZZFAZZ_TEST_STACK_UP=1|0.
# A failing probe (daemon hiccup, interpolation error) counts as UP: the safe
# side is one service at a time.
razzfazz_stack_is_up() {
    case "${RAZZFAZZ_TEST_STACK_UP:-}" in 1) return 0 ;; 0) return 1 ;; esac
    local running
    running=$(docker compose ps -q --status running 2>/dev/null) || running="probe-failed"
    [ -n "$running" ]
}

# #2820: the first TRANSIENT upstream failure named in a build/pull log, or
# nothing. One Docker Hub 502 broke a package build (the 2026.11 rehearsal) and
# one registry.npmjs.org timeout killed `rzfz init` on a test box: worth another try.
# A broken Dockerfile, a checksum or pin mismatch, a tag that does not exist or a
# denied pull is not: when the log names one of those, nothing is printed, even
# beside a transient marker, so a deterministic failure always fails fast.
# $1 = the log. Always returns 0.
razzfazz_transient_reason() {
    local log="$1"
    [ -s "$log" ] || return 0
    if grep -qiE '(checksum|hash|digest|integrity|sha256)[^[:cntrl:]]{0,40}(mismatch|did not match|does not match|do not match)|EINTEGRITY|computed checksum did not match|manifest unknown|manifest for [^ ]+ not found|not found: manifest|pull access denied|unauthorized|denied: |failed to compute cache key|dockerfile parse error|unknown instruction|failed to solve: failed to read dockerfile' "$log"; then
        return 0
    fi
    grep -oiE -m1 '(50[0234] (bad gateway|internal server error|service unavailable|gateway time-?out)|bad gateway|service unavailable|gateway time-?out|i/o timeout|tls handshake timeout|connection reset by peer|connection timed out|unexpected eof|context deadline exceeded|timeout awaiting response headers|temporary failure in name resolution|EAI_AGAIN|ETIMEDOUT|ECONNRESET|ESOCKETTIMEDOUT|ERR_SOCKET_TIMEOUT|network timeout|ReadTimeoutError|toomanyrequests|429 too many requests|failed to fetch (anonymous|oauth)? ?token|failed to do request)' \
        "$log" 2>/dev/null | head -n1 || true
    return 0
}

# #2820: run one image build or pull, again while it fails on a TRANSIENT upstream
# error: at most RAZZFAZZ_IMAGE_RETRIES (3) tries, waiting RAZZFAZZ_IMAGE_RETRY_BACKOFF
# (30) seconds, doubled each time. One line per failed attempt; the last names
# <label> and the last upstream error. A deterministic failure (see
# razzfazz_transient_reason) is not retried. An OFFLINE box gets exactly one try:
# there is no upstream to come back. Both streams stay where they were (stdout
# untouched; stderr also copied into a log the classifier reads after the
# command has ended). `--output` first: also retry an exit 0 whose output names a
# transient failure (`compose pull --ignore-pull-failures` exits 0 over one).
# Usage: razzfazz_image_retry [--output] <label> <command…>; the command's exit.
razzfazz_image_retry() {
    local on_output=false label max base n=1 rc log why
    if [ "${1:-}" = --output ]; then on_output=true; shift; fi
    label="$1"; shift
    max="${RAZZFAZZ_IMAGE_RETRIES:-3}"; base="${RAZZFAZZ_IMAGE_RETRY_BACKOFF:-30}"
    case "$max" in ''|*[!0-9]*|0) max=3 ;; esac
    case "$base" in ''|*[!0-9]*) base=30 ;; esac
    if [ "${RAZZFAZZ_OFFLINE:-}" = 1 ] || razzfazz_is_offline 2>/dev/null; then
        max=1
    fi
    log="$(mktemp "${TMPDIR:-/tmp}/rzfz-image-step.XXXXXX")" || { "$@"; return; }
    while :; do
        : > "$log"
        rc=0
        # stderr through tee into the log (and back to stderr); stdout via fd 3.
        # In a || list, so a caller's `set -e` + pipefail cannot exit before rc.
        { "$@" 2>&1 1>&3 3>&- | tee -a "$log" >&2; rc=${PIPESTATUS[0]}; } 3>&1 || true
        why="$(razzfazz_transient_reason "$log")"
        if [ "$rc" -eq 0 ] && { [ "$on_output" = false ] || [ -z "$why" ]; }; then
            [ "$n" -eq 1 ] || printf '%s: succeeded on attempt %s of %s.\n' "$label" "$n" "$max" >&2
            rm -f "$log"
            return 0
        fi
        if [ -z "$why" ]; then
            [ "$rc" -ne 0 ] || rc=1
            printf '%s: FAILED (exit %s): not a transient upstream error, not retried.\n' "$label" "$rc" >&2
            break
        fi
        if [ "$n" -ge "$max" ]; then
            [ "$rc" -ne 0 ] || rc=1
            if [ "$max" -eq 1 ]; then
                printf '%s: FAILED (exit %s; last upstream error: %s); offline box, not retried.\n' "$label" "$rc" "$why" >&2
            else
                printf '%s: FAILED after %s attempts (exit %s; last upstream error: %s).\n' "$label" "$n" "$rc" "$why" >&2
            fi
            break
        fi
        printf '%s: attempt %s of %s failed (%s); retrying in %ss.\n' "$label" "$n" "$max" "$why" "$(( base * (1 << (n - 1)) ))" >&2
        sleep "$(( base * (1 << (n - 1)) ))"
        n=$((n + 1))
    done
    rm -f "$log"
    return "$rc"
}

# The services with a `build:` section in the caller's compose model (its
# COMPOSE_FILE/COMPOSE_PROFILES), sorted, one per line. A failed listing is an
# error naming the command and a non-zero return — never "nothing to build".
razzfazz_compose_build_services() {
    local json rc=0
    json=$(docker compose config --format json) || rc=$?
    if [ "$rc" -eq 0 ] && [ -n "$json" ]; then
        printf '%s' "$json" | python3 -c 'import json, sys
c = json.load(sys.stdin)
for n, s in sorted((c.get("services") or {}).items()):
    if isinstance(s, dict) and s.get("build"):
        print(n)' && return 0
        rc=$?
    fi
    [ "$rc" -ne 0 ] || rc=1
    printf 'ERROR: could not list the build services (docker compose config --format json, exit %s); not building them all at once instead (#2560).\n' "$rc" >&2
    return "$rc"
}

# razzfazz_release_version — the release this tree IS: ./VERSION, trimmed; empty
# without one. #2818: .env's RAZZFAZZ_VERSION is the upgrade's completion stamp,
# written last (#1593), so mid-upgrade it names the OLD release. Builds take the
# value from here; the image digest uses the same source
# (build_preflight.release_compose_env), and so does the packager (the Enterprise package tooling).
razzfazz_release_version() {
    [ -f VERSION ] || return 0
    tr -d '[:space:]' < VERSION
}

# `docker compose build <args…>` with both bounds set: RAZZFAZZ_BUILD_JOBS (the
# build arg the source Dockerfiles turn into CARGO_BUILD_JOBS/MAKEFLAGS/-j) and
# COMPOSE_PARALLEL_LIMIT = razzfazz_build_parallelism, or 1 while the stack is
# up unless the operator set RAZZFAZZ_BUILD_PARALLELISM. Callers keep their
# COMPOSE_FILE/COMPOSE_PROFILES prefix; the exit status is docker's.
# A limit of 1 is enforced HERE: bake (compose's default builder) builds every
# target of one call at once and ignores COMPOSE_PARALLEL_LIMIT (compose v2.40
# build_bake.go). So at 1 every service gets its own call, in sorted order —
# the named ones, or every build service of the caller's model — and the first
# failure stops the sequence with that exit code.
# #2818: RAZZFAZZ_VERSION comes from ./VERSION (razzfazz_release_version), not from
# .env, so a build during an upgrade carries the release it builds.
razzfazz_compose_build() {
    local jobs limit override a svc listed rc want_value=false what="" release
    local -a opts=() names=() release_env=()
    jobs=$(razzfazz_build_jobs)
    release=$(razzfazz_release_version)
    [ -z "$release" ] || release_env=(env "RAZZFAZZ_VERSION=${release}")
    # The worker agent's build identity (rzfz_node_context_digest): post-install
    # compares it with the tree before publishing the image to the hub.
    local node_context; node_context=$(rzfz_node_context_digest .)
    if [ -n "$node_context" ]; then
        [ "${#release_env[@]}" -gt 0 ] || release_env=(env)
        release_env+=("RZFZ_NODE_AGENT_BUILD_CONTEXT=${node_context}")
    fi
    limit=$(razzfazz_build_parallelism)
    override=$(grep -m1 '^RAZZFAZZ_BUILD_PARALLELISM=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    override="${RAZZFAZZ_BUILD_PARALLELISM:-$override}"
    if ! { [ -n "$override" ] && [ "$override" -ge 1 ] 2>/dev/null; } && razzfazz_stack_is_up; then
        limit=1   # a running stack plus a build = one service at a time
    fi
    printf 'Build bounds (#2560): %s job(s) inside each build, %s service(s) at a time.\n' "$jobs" "$limit" >&2
    if [ "$limit" = 1 ]; then
        for a in "$@"; do
            if [ "$want_value" = true ]; then opts+=("$a"); want_value=false; continue; fi
            case "$a" in
                --build-arg|--builder|--memory|-m|--progress|--ssh|--sbom|--provenance)
                    opts+=("$a"); want_value=true ;;
                -*) opts+=("$a") ;;
                *)  names+=("$a") ;;
            esac
        done
        if [ "${#names[@]}" -eq 0 ]; then
            listed=$(razzfazz_compose_build_services) || return $?
            [ -n "$listed" ] && mapfile -t names <<< "$listed"
        fi
    else
        opts=("$@"); names=("")   # one call, the caller's arguments unchanged
        for a in "$@"; do           # #2820: only to name the services in the retry lines
            if [ "$want_value" = true ]; then want_value=false; continue; fi
            case "$a" in
                --build-arg|--builder|--memory|-m|--progress|--ssh|--sbom|--provenance) want_value=true ;;
                -*) ;;
                *)  what="${what:+${what} }$a" ;;
            esac
        done
    fi
    for svc in ${names[@]+"${names[@]}"}; do
        # #2820: a transient upstream failure is retried inside, a real one is not.
        RAZZFAZZ_BUILD_JOBS="$jobs" CARGO_BUILD_JOBS="$jobs" COMPOSE_PARALLEL_LIMIT="$limit" \
            razzfazz_image_retry "Image build (${svc:-${what:-every service of this call}})" \
            ${release_env[@]+"${release_env[@]}"} docker compose build ${opts[@]+"${opts[@]}"} ${svc:+"$svc"} || {
            rc=$?
            # One fixed line per failed service: bake's own one-target output
            # need not carry `target X:`; cli/init.sh parses this line too.
            [ -z "$svc" ] || printf 'Image build failed for service: %s (exit %s)\n' "$svc" "$rc" >&2
            return "$rc"
        }
    done
}

# What the source builds cost, ONE table: service|language|minutes|GB free.
# Every figure is an ESTIMATE, not a measurement; the announcements say so.
#   moltis-image       Rust, 60 min, 16 GB: estimate from the #2560 incident on a
#                      fleet box (a single Moltis build under cargo -j<nproc>
#                      beside a running stack). Not timed with the bounds of
#                      razzfazz_compose_build; a bounded build is slower.
#   hermes-agent-image Python native wheels, 20 min, 8 GB: estimate, never timed.
# Change a number here and nowhere else.
razzfazz_source_build_cost_table() {
    cat <<'TABLE'
moltis-image|Rust|60|16
hermes-agent-image|Python native wheels|20|8
TABLE
}

# razzfazz_source_builds_pending [envfile]: which source builds will THIS run
# compile? A pinned image that is not present locally cannot be a cache hit.
# One line per service: <service>|<image ref>|<language>|<minutes>|<GB>|<state>.
# state: "build" (docker says the image is absent) or "unknown" (docker could
# not answer, e.g. no daemon); the caller must never read "unknown" as "nothing
# to build". Empty output means the profile is off or the images are present.
# The env file is read with read_env_value, never sourced.
razzfazz_source_builds_pending() {
    local env_file="${1:-.env}" profiles mv hv svc ref ver lang min gb state out rc key repo cf
    profiles=",$(read_env_value "$env_file" COMPOSE_PROFILES),"
    case "$profiles" in *",agents,"*) ;; *) return 0 ;; esac
    cf="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/modules/agents/compose.yml"
    while IFS='|' read -r svc lang min gb; do
        case "$svc" in
            moltis-image) key=MOLTIS_VERSION; repo=razzfazz-stack-moltis ;;
            hermes-agent-image) key=HERMES_AGENT_VERSION; repo=razzfazz-stack-hermes-agent ;;
            *) continue ;;
        esac
        ver=$(read_env_value "$env_file" "$key")
        # An unset pin builds the compose default: read it from the compose file.
        if [ -z "$ver" ] && [ -f "$cf" ]; then
            ver=$(grep -o "\${${key}:-[^}]*}" "$cf" 2>/dev/null | head -n1 | sed 's/^[^-]*:-//; s/}$//' || true)
        fi
        # A value that is not a plain tag never enters the record (the field
        # separator is a pipe) and is never handed to docker.
        if ! [[ "$ver" =~ ^[A-Za-z0-9_.-]+$ ]]; then
            printf '%s|%s|%s|%s|%s|%s\n' "$svc" "${repo}:(unknown)" "$lang" "$min" "$gb" version-unknown
            continue
        fi
        ref="${repo}:${ver}"
        rc=0
        out=$(docker image inspect "$ref" 2>&1) || rc=$?
        [ "$rc" -ne 0 ] || continue
        case "$out" in
            *"No such image"*|*"no such image"*|*"No such object"*) state=build ;;
            *) state=unknown ;;
        esac
        printf '%s|%s|%s|%s|%s|%s\n' "$svc" "$ref" "$lang" "$min" "$gb" "$state"
    done < <(razzfazz_source_build_cost_table)
    return 0
}

# Print the announcement for the lines razzfazz_source_builds_pending gave
# ($1). $2 names the operation ("upgrade" | "install"). Prints nothing for an
# empty list. Needs print_step/print_info (the callers' own).
razzfazz_announce_source_builds() {
    local lines="$1" op="${2:-upgrade}" svc ref lang min gb state
    [ -n "$lines" ] || return 0
    print_step "This ${op} compiles from source (estimates, not measurements; bounded to $(razzfazz_build_jobs) job(s) per build):"
    while IFS='|' read -r svc ref lang min gb state; do
        [ -n "$svc" ] || continue
        if [ "$state" = version-unknown ]; then
            print_info "  ${svc} (${lang}): version unknown, it will build the compose default; about ${min} min (estimate), >= ${gb} GB free recommended"
        elif [ "$state" = unknown ]; then
            print_info "  ${svc} (${lang}): ${ref}: cannot tell whether it is already built; if not, about ${min} min (estimate), >= ${gb} GB free recommended"
        else
            print_info "  ${svc} (${lang}): ${ref}: about ${min} min (estimate), >= ${gb} GB free recommended"
        fi
    done <<< "$lines"
    return 0
}

# `docker build <args…>` (the runner images, Paperclip, the Caddy lint) with the
# per-build job bound as a build arg; the runner Dockerfiles turn it into
# cmake -j. One image per call — the callers build one image at a time.
# A Dockerfile that never declares the ARG (Paperclip, Caddy) ignores it: a
# harmless no-op, at most a "build-arg not consumed" warning.
razzfazz_docker_build() {
    local jobs a prev="" tag=""
    jobs=$(razzfazz_build_jobs)
    printf 'Build bounds (#2560): %s job(s) inside this build.\n' "$jobs" >&2
    for a in "$@"; do
        case "$prev" in -t|--tag) tag="$a" ;; esac
        prev="$a"
    done
    # #2820: a transient upstream failure is retried inside, a real one is not.
    razzfazz_image_retry "Image build (${tag:-docker build})" docker build --build-arg "RAZZFAZZ_BUILD_JOBS=${jobs}" "$@"
}

# _razzfazz_walk_link <link> <root>
#
# #2603: resolve <link> physically WITHOUT realpath, component by component,
# readlink per hop (relative targets against the directory they were found in).
# Prints the final path and returns 0; after 40 hops it is a loop: prints
# LOOP-INSIDE (rc 1) when no step ever left <root>, else OUT-OF-TREE (rc 1).
_razzfazz_walk_link() {
    local link="$1" root="$2" hops=0 out_seen=0
    local cur rest comp cand t dir
    dir=$(cd -- "$(dirname -- "$link")" 2>/dev/null && pwd -P) || { echo "OUT-OF-TREE"; return 1; }
    cur="$dir"
    rest="$(basename -- "$link")"
    while [ -n "$rest" ]; do
        comp="${rest%%/*}"
        case "$rest" in */*) rest="${rest#*/}" ;; *) rest="" ;; esac
        case "$comp" in
            ""|.) continue ;;
            ..) cur=$(dirname -- "$cur") ;;
            *)
                cand="${cur%/}/$comp"
                if [ -L "$cand" ]; then
                    hops=$((hops + 1))
                    if [ "$hops" -gt 40 ]; then
                        if [ "$out_seen" -eq 0 ]; then echo "LOOP-INSIDE"; else echo "OUT-OF-TREE"; fi
                        return 1
                    fi
                    t=$(readlink -- "$cand" 2>/dev/null) || { echo "OUT-OF-TREE"; return 1; }
                    case "$t" in
                        /*) cur="/" ;;
                    esac
                    rest="$t${rest:+/$rest}"
                else
                    cur="$cand"
                fi
                ;;
        esac
        case "$cur" in
            "$root"|"$root"/*) ;;
            *) out_seen=1 ;;
        esac
    done
    printf '%s\n' "$cur"
}

# ------------------------------------------------------------------------------
# razzfazz_find_escaping_links <dir>
#
# #755: print every symlink under <dir> whose target leaves <dir>, one
# "path -> target" per line. Returns 0 when at least one was found, 1 when the
# tree is clean, 2 when <dir> is unusable.
#
# The offline-package path check looks at MEMBER NAMES only. tar refuses a `..`
# member and (with the "Removing leading" check in cli/upgrade.sh) an absolute
# one — but a member that IS a symlink carries its danger in the LINK TARGET,
# which no name check ever inspects. Measured with GNU tar 1.34 and 9.1: an
# archive whose single member is `certs/leak.pem -> /etc/shadow` extracts with
# exit 0 and an empty stderr. Same for `certs/rel.pem -> ../../../../etc/shadow`.
# The rsync that follows then carries that link into the installation, and
# `certs/` is exactly the directory containers mount their CA bundle from.
#
# Checked and NOT covered here: a hardlink member naming a file outside the
# archive. tar stores it as a regular file (the target is not a member), so it
# arrives as a copy, not as a reference — no escape.
#
# #2603: every link is resolved PHYSICALLY (`realpath -m`: existing links on the
# path are followed, missing components are allowed). A lexical resolve misses a
# two-link chain: `a/up -> ..` plus `x -> a/up/..` each look harmless, but
# `x` really lands one level above the root. The tree under <dir> is the extracted
# package, so "what exists" is the package itself. A link loop inside the tree
# is not an escape (it resolves to nothing outside); one that leads out is.
razzfazz_find_escaping_links() {
    local root="$1"
    [ -n "$root" ] && [ -d "$root" ] || return 2

    local root_norm
    root_norm=$(realpath -m -- "$root" 2>/dev/null) || return 2

    local found=1 link target resolved
    # `find "$root"` so every printed path carries the same prefix that
    # root_norm was normalised from — comparing two differently-rooted spellings
    # of the same tree is how this kind of check gets a false verdict.
    while IFS= read -r link; do
        [ -n "$link" ] || continue
        target=$(readlink -- "$link" 2>/dev/null) || continue
        case "$target" in
            /*) resolved=$(realpath -m -- "$target" 2>/dev/null) ;;
            *)  resolved=$(realpath -m -- "$(dirname -- "$link")/$target" 2>/dev/null) ;;
        esac
        if [ -z "$resolved" ]; then
            # #2603: a realpath failure means "unresolvable", not "escaping".
            # uutils realpath (the default on Ubuntu 26.04) ERRORS on a symlink
            # loop even with -m; GNU returns the path. Walk the chain by hand,
            # hop-bounded, so the verdict is the same under both.
            if resolved=$(_razzfazz_walk_link "$link" "$root_norm"); then
                :
            elif [ "$resolved" = "LOOP-INSIDE" ]; then
                continue   # a loop that never left the tree is not an escape
            else
                resolved="${resolved:-$target}"
            fi
        fi
        case "$resolved" in
            "$root_norm"|"$root_norm"/*) ;;
            *) printf '%s -> %s\n' "$link" "$target"; found=0 ;;
        esac
    done <<EOF
$(find "$root" -type l 2>/dev/null)
EOF
    return $found
}

# ------------------------------------------------------------------------------
# razzfazz_verify_package_authenticity <archive>
#
# #781: decide whether an offline package is AUTHENTIC — using evidence that
# does NOT travel inside the package.
#
# WHY THIS IS NOT MANIFEST.sha256. That manifest is the only integrity
# statement a package makes today, it sits INSIDE the archive, and
# cli/upgrade.sh checks it AFTER extracting. It is therefore self-attesting: it
# proves a stick was not corrupted in transit, not that the stick is ours. A
# prepared package simply brings a matching manifest along. The delivery route
# is what makes that real — offline packages arrive physically, at the
# customer, not over an authenticated channel. #755's path/symlink checks
# harden the STRUCTURE of a package; they say nothing about its origin, and a
# well-formed hostile package walks straight through them. That was the
# explicit "point 1 without point 2 is half an answer" of #755.
#
# TRI-STATE, deliberately mirroring razzfazz_find_escaping_links so callers
# read the same way:
#   0  AUTHENTICATED — at least one out-of-package check ran and PASSED
#   1  UNVERIFIED    — no such evidence was available at all
#   2  FAILED        — evidence was available and it did NOT pass
#
# UNVERIFIED is deliberately distinct from FAILED. Every package built before
# this change is UNVERIFIED, and turning that into a refusal is a fleet-wide
# flag day, not a patch — so the POLICY (refuse vs warn during a transition)
# belongs to the caller, and the operator decision that sets it is still open.
# The one thing decided here is that "could not look" never renders as "fine":
# a missing verifier binary, an unreadable key, or an unreadable archive is
# never AUTHENTICATED.
#
# TWO FORMS OF EVIDENCE, both from the #781 sketch:
#   1. A DETACHED SIGNATURE beside the archive (`<archive>.openssl.sig` for the
#      adopted openssl scheme; `<archive>.minisig` for minisign/signify and
#      `<archive>.asc`/`.sig` for GPG remain readable), verified against a
#      public key named from OUTSIDE the delivery — $RAZZFAZZ_PACKAGE_PUBKEY,
#      else the fleet path (openssl) or config/package-keys/razzfazz-packages.pub
#      (legacy schemes). A key found NEXT TO the package is explicitly NOT a
#      trust anchor: whoever prepared a hostile stick prepared the key on it too.
#   2. An OUT-OF-BAND TRANSPORTED HASH — $RAZZFAZZ_PACKAGE_EXPECT_SHA256, fed
#      by `rzfz upgrade --expect-sha256 <hash>`, read by the operator from the
#      release notes or the fleet channel. This is what works TODAY, with no
#      key management whatsoever, and it is why it is implemented alongside
#      the signature rather than after it.
#
# THE SCHEME — operator decision 2026-09-02, binding. It is **openssl**: a
# detached `openssl dgst -sha256 -sign` signature, `<archive>.openssl.sig`,
# verified with `openssl dgst -sha256 -verify`. Chosen because every box in the
# fleet already has openssl (measured: gpgv/openssl/sha256sum and nothing else)
# — minisign would have meant a new package on every box before a single
# signature could be checked, i.e. a fleet rollout in front of the security fix
# rather than behind it. The digest is pinned to SHA-256 on BOTH halves; an
# unpinned digest is a downgrade knob the signature itself cannot object to.
#
# WHERE THE KEYS LIVE — also operator-side, and NOT in this repository:
#   * The PRIVATE key lives in the release secrets vault. It is never generated
#     by this code, never written by it, and never travels in a package. The
#     signing half (the Enterprise package tooling --sign-key) takes a path to it and nothing
#     more; a key that reaches this repo is a key that must be rotated.
#   * The PUBLIC key is distributed by the FLEET INSTALL CHANNEL to
#     $RAZZFAZZ_PACKAGE_PUBKEY, else /etc/razzfazz/package-keys/razzfazz-packages.pem
#     ($RAZZFAZZ_PACKAGE_PUBKEY_PATH overrides that default). That path is
#     deliberately OUTSIDE the stack checkout: an upgrade package extracts over
#     the checkout, so an in-tree trust anchor would be replaceable by the very
#     artefact it is supposed to judge — this upgrade's package choosing the
#     next upgrade's key.
#
# DECIDED (#781, 2026-09-02): unsigned packages are REFUSED from the release
# after the first GA that ships signed packages; the override env stays. No GA
# has shipped signed yet; signing starts with 2027.01-ga, so the flip is
# 2027.03 (cli/upgrade.sh, the
# comment above the "NOT verified" warning, and the offline delivery decision
# record, section 8).
#
# STILL NOT DECIDED HERE:
#   * Whether the appliance package (APPLIANCE_OFFLINE_PKG) gets the same
#     treatment — it arrives by the same route (#782 hardened its structure).
#
# MALFORMED IS NOT MISSING. An ABSENT or unreadable public key is missing
# evidence (UNVERIFIED): this box simply has not been given the anchor yet. A
# key that is PRESENT and readable but not a usable public key, or a signature
# file that is present but garbage, is a check that RAN AND FAILED (2) — the
# material for this stick was looked at and it did not hold up, and degrading
# that to "nothing to check" would let a corrupted or substituted key file turn
# a refusal into a warning.
#
# THE OVERRIDE AND ITS LIMIT. $RAZZFAZZ_ALLOW_UNVERIFIED_PACKAGE=1 is the
# emergency lever the issue asks for, logged like RAZZFAZZ_SKIP_SECURITY_REVIEW.
# It covers MISSING evidence only. It never rescues a check that ran and came
# back negative — a hash the operator typed that does not match, or a signature
# that does not verify, is evidence ABOUT THIS STICK, which is the opposite of
# having none. (That limit is a judgement call, not something #781 states; it
# is pinned by test_the_override_never_rescues_a_check_that_actually_failed so
# a later reader can overturn it deliberately rather than by accident.)
#: Where a box may hold the trust anchor for a signed package, in the order the
#: verifier consults them. Named here so the verifier and the status reporter
#: read ONE list — a second copy drifts the first time a path moves, and then
#: `rzfz status` says "you can check signatures" about a location nothing reads
#: (the shape #1755 and #1769 both landed on).
#:
#: The fleet path is deliberately OUTSIDE the stack checkout: an in-tree anchor
#: is overwritable by the very package it is meant to judge.
RAZZFAZZ_PACKAGE_KEY_FLEET_DEFAULT="/etc/razzfazz/package-keys/razzfazz-packages.pem"
RAZZFAZZ_PACKAGE_KEY_REPO_RELATIVE="config/package-keys/razzfazz-packages.pub"

# razzfazz_package_key_source  (#781)
#
# Can this box check a signed delivery at all, and with which anchor?
#
# Prints "<source>\t<path>" for the first readable anchor, where <source> is
# `env`, `fleet` or `repo`; prints nothing when the box holds none.
# Return: 0 when an anchor was found, 1 when none was.
#
# WHY A STATUS QUESTION AND NOT ONLY AN UPGRADE ONE. Until #781's rollout,
# `cli/upgrade.sh` is the only place that mentions the key — and it does so
# while the operator is already holding the stick. That is too late to answer
# either half of the rollout question: which boxes have it yet, and did the
# rollout arrive. Reading it costs a stat; not reading it costs a fleet-wide
# unknown.
#
# $1: the stack checkout, for the in-tree fallback (optional).
razzfazz_package_key_source() {
    local root="${1:-${SCRIPT_DIR:-}}" cand
    if [ -n "${RAZZFAZZ_PACKAGE_PUBKEY:-}" ] && [ -r "${RAZZFAZZ_PACKAGE_PUBKEY}" ]; then
        printf 'env\t%s\n' "$RAZZFAZZ_PACKAGE_PUBKEY"; return 0
    fi
    cand="${RAZZFAZZ_PACKAGE_PUBKEY_PATH:-$RAZZFAZZ_PACKAGE_KEY_FLEET_DEFAULT}"
    if [ -r "$cand" ]; then
        printf 'fleet\t%s\n' "$cand"; return 0
    fi
    if [ -n "$root" ] && [ -r "${root}/${RAZZFAZZ_PACKAGE_KEY_REPO_RELATIVE}" ]; then
        printf 'repo\t%s\n' "${root}/${RAZZFAZZ_PACKAGE_KEY_REPO_RELATIVE}"; return 0
    fi
    return 1
}

razzfazz_verify_package_authenticity() {
    local archive="$1"
    local allow="${RAZZFAZZ_ALLOW_UNVERIFIED_PACKAGE:-}"
    local expect="${RAZZFAZZ_PACKAGE_EXPECT_SHA256:-}"
    local verdict=1          # UNVERIFIED until something says otherwise
    local checked=false

    if [ -z "$archive" ] || [ ! -f "$archive" ] || [ ! -r "$archive" ]; then
        echo "#781: package file is missing or unreadable: ${archive:-<none>}" >&2
        return 2
    fi

    # --- form 2: the out-of-band transported hash -----------------------------
    if [ -n "$expect" ]; then
        checked=true
        # Normalise: operators paste `<hash>  <filename>` straight out of a
        # release note often enough that accepting only the bare hash would
        # turn a correct paste into a "malformed" refusal.
        expect="${expect%% *}"
        expect="$(printf '%s' "$expect" | tr '[:upper:]' '[:lower:]')"
        local actual
        actual="$(sha256sum -- "$archive" 2>/dev/null | cut -d' ' -f1)"
        if [ -z "$actual" ]; then
            echo "#781: could not hash ${archive}." >&2
            return 2
        fi
        case "$expect" in
            # A truncated or mistyped hash must NOT degrade to "nothing to
            # check" — pasting half a hash out of a release note is the likely
            # operator error, and silently skipping would turn an explicit
            # authenticity request into no check at all.
            [0-9a-f][0-9a-f]*)
                if [ "${#expect}" -ne 64 ]; then
                    echo "#781: --expect-sha256 value is not a 64-character SHA-256: '${expect}'" >&2
                    echo "      Refusing to continue: a malformed expected hash is an operator" >&2
                    echo "      error, not an absent check." >&2
                    return 2
                fi ;;
            *)
                echo "#781: --expect-sha256 value is not a hexadecimal SHA-256: '${expect}'" >&2
                return 2 ;;
        esac
        if [ "$actual" = "$expect" ]; then
            echo "#781: package matches the out-of-band SHA-256 (${actual})."
            verdict=0
        else
            echo "#781: PACKAGE DOES NOT MATCH the out-of-band SHA-256." >&2
            echo "      expected: ${expect}" >&2
            echo "      actual:   ${actual}" >&2
            echo "      This is the check MANIFEST.sha256 structurally cannot make:" >&2
            echo "      a prepared package carries its own matching manifest." >&2
            return 2
        fi
    fi

    # --- form 1: the detached signature ---------------------------------------
    # `.openssl.sig` first, and spelled out rather than reusing the bare `.sig`
    # that GPG conventionally writes: the two are indistinguishable by content
    # to the shell, and guessing wrong turns a GOOD gpg signature into a
    # refusal (openssl cannot read it) — a false accusation, which is the one
    # verdict a security check must never invent.
    local sig="" sig_kind=""
    if [ -f "${archive}.openssl.sig" ]; then
        sig="${archive}.openssl.sig"; sig_kind="openssl"
    elif [ -f "${archive}.minisig" ]; then
        sig="${archive}.minisig"; sig_kind="minisign"
    elif [ -f "${archive}.asc" ]; then
        sig="${archive}.asc"; sig_kind="gpg"
    elif [ -f "${archive}.sig" ]; then
        sig="${archive}.sig"; sig_kind="gpg"
    fi

    if [ -n "$sig" ]; then
        # The trust anchor is named from OUTSIDE the delivery. Never derived
        # from the archive's own directory: a key lying next to a hostile stick
        # is the attacker's key.
        local key="${RAZZFAZZ_PACKAGE_PUBKEY:-}"
        if [ -z "$key" ]; then
            case "$sig_kind" in
                openssl)
                    # The fleet install channel puts it here. Outside the stack
                    # checkout on purpose — see the header: an in-tree anchor is
                    # overwritable by the package it is meant to judge.
                    key="${RAZZFAZZ_PACKAGE_PUBKEY_PATH:-$RAZZFAZZ_PACKAGE_KEY_FLEET_DEFAULT}"
                    ;;
                *)
                    if [ -n "${SCRIPT_DIR:-}" ]; then
                        key="${SCRIPT_DIR}/${RAZZFAZZ_PACKAGE_KEY_REPO_RELATIVE}"
                    fi
                    ;;
            esac
        fi
        if [ -z "$key" ] || [ ! -r "$key" ]; then
            echo "#781: ${archive} carries a detached signature, but this box holds no" >&2
            echo "      trusted public key to check it with (RAZZFAZZ_PACKAGE_PUBKEY unset" >&2
            echo "      or unreadable). NOT treating that as authentic." >&2
        else
            local tool="" rc=0 vout=""
            case "$sig_kind" in
                openssl)
                    if command -v openssl >/dev/null 2>&1; then tool=openssl; fi ;;
                minisign)
                    if command -v minisign >/dev/null 2>&1; then tool=minisign
                    elif command -v signify >/dev/null 2>&1; then tool=signify
                    fi ;;
                gpg)
                    if command -v gpgv >/dev/null 2>&1; then tool=gpgv
                    elif command -v gpg >/dev/null 2>&1; then tool=gpg
                    fi ;;
            esac
            if [ -z "$tool" ]; then
                # minisign is NOT installed on the fleet's boxes. "No tool to
                # look with" is not "it is fine" — same fail-safe as #755.
                echo "#781: ${archive} carries a ${sig_kind} signature but no verifier is" >&2
                echo "      installed on this box. NOT treating that as authentic." >&2
            else
                checked=true
                case "$tool" in
                    openssl)
                        # -sha256 is pinned to match the signing half. openssl
                        # returns non-zero both for "signature does not match"
                        # and for "that file is not a public key" — deliberately
                        # NOT separated here: both mean the material presented
                        # for THIS stick did not hold up, which is a failure and
                        # not an absence. Its message is kept and printed below,
                        # because "Verification Failure" and "could not read
                        # public key" need very different operator responses.
                        vout="$(openssl dgst -sha256 -verify "$key" \
                                    -signature "$sig" "$archive" 2>&1)" || rc=$? ;;
                    minisign|signify) "$tool" -V -p "$key" -x "$sig" -m "$archive" >/dev/null 2>&1 || rc=$? ;;
                    gpgv)             gpgv --keyring "$key" "$sig" "$archive" >/dev/null 2>&1 || rc=$? ;;
                    gpg)              gpg --no-default-keyring --keyring "$key" \
                                          --verify "$sig" "$archive" >/dev/null 2>&1 || rc=$? ;;
                esac
                if [ "$rc" -eq 0 ]; then
                    echo "#781: detached ${sig_kind} signature verified against ${key}."
                    verdict=0
                else
                    echo "#781: DETACHED SIGNATURE DID NOT VERIFY (${tool}, key ${key})." >&2
                    echo "      The package was not signed for what arrived on this medium." >&2
                    if [ -n "$vout" ]; then
                        echo "      ${tool}: ${vout}" >&2
                    fi
                    return 2
                fi
            fi
        fi
    fi

    if [ "$checked" = false ] || [ "$verdict" -ne 0 ]; then
        if [ "$verdict" -ne 0 ]; then
            echo "#781: no out-of-package authenticity evidence for ${archive}." >&2
            echo "      MANIFEST.sha256 travels INSIDE the archive and is checked after" >&2
            echo "      extraction, so it proves the stick was not corrupted — NOT that it" >&2
            echo "      is genuine. Supply --expect-sha256 <hash> from the release notes," >&2
            echo "      or deliver a signed package." >&2
            if [ "$allow" = "1" ]; then
                echo "      RAZZFAZZ_ALLOW_UNVERIFIED_PACKAGE=1 is set — proceeding anyway." >&2
                echo "      This override is logged; it covers MISSING evidence only." >&2
            fi
        fi
    fi
    return "$verdict"
}

# razzfazz_appliance_package_gate <package>
#   #781 for the appliance stick: the same out-of-package check `rzfz upgrade
#   --package` makes, applied before `rzfz init` loads anything from the baked
#   package. 0 = authentic or nothing to check (the latter warns, like upgrade
#   until the unsigned-refuse flip), 2 = a check ran and failed (never
#   overridable).
razzfazz_appliance_package_gate() {
    local pkg="$1" rc=0
    razzfazz_verify_package_authenticity "$pkg" || rc=$?
    case "$rc" in
        0) return 0 ;;
        2)
            print_error "The appliance package ${pkg} failed its authenticity check (#781)."
            return 2 ;;
        *)
            print_warning "Appliance package authenticity was NOT verified (#781): no signature or no fleet public key on this box."
            return 0 ;;
    esac
}

# ------------------------------------------------------------------------------
# razzfazz_sign_package <archive> <private-key> [signature-path]
#
# #781, the PRODUCING half of the check above. Writes a detached
# `openssl dgst -sha256 -sign` signature BESIDE the archive (default
# `<archive>.openssl.sig`) — never into it. Anything inside the tarball is
# something the tarball can bring along, which is the entire reason
# MANIFEST.sha256 cannot answer "is this stick genuine".
#
# NO KEY IS EVER CREATED HERE. The private key lives in the release secrets vault
# (operator decision 2026-09-02); this function takes a path to a key the caller
# already has and refuses when it does not. A helper that could `genpkey` its
# way out of a missing key would, on the day someone runs it by accident,
# produce a package signed by a key nobody trusts and no way to tell that from
# the real thing.
#
# THE FRESH SIGNATURE IS VERIFIED BEFORE THIS RETURNS, with `-prverify` against
# the same private key. That is not paranoia about openssl: it is the only
# moment where a wrong digest, a truncated write, or a key file that is not
# actually a signing key is cheap to notice. The alternative is noticing on the
# customer's box, where the verdict is indistinguishable from a tampered stick.
# `-prverify` also means no public key has to be written to disk next to the
# archive — a pubkey lying beside a package is exactly the non-anchor the
# verifying half refuses to trust.
#
# Env: RAZZFAZZ_PACKAGE_SIGN_PASSPHRASE — passphrase for an encrypted vault key.
#      Unset with an encrypted key means openssl prompts (interactive) or fails
#      (no tty); it never signs unprompted.
#
#   0  signed, and the signature verified against the key that produced it
#   1  refused — nothing usable was written
# ------------------------------------------------------------------------------
razzfazz_sign_package() {
    local archive="$1"
    local keyfile="${2:-}"
    local sig="${3:-${archive}.openssl.sig}"

    if [ -z "$archive" ] || [ ! -r "$archive" ]; then
        echo "#781: cannot sign — package file is missing or unreadable: ${archive:-<none>}" >&2
        return 1
    fi
    if [ -z "$keyfile" ] || [ ! -r "$keyfile" ]; then
        echo "#781: cannot sign — signing key is missing or unreadable: ${keyfile:-<none>}" >&2
        echo "      The key lives in the release secrets vault; none is generated here." >&2
        return 1
    fi
    if ! command -v openssl >/dev/null 2>&1; then
        echo "#781: cannot sign — openssl is not installed on this build machine." >&2
        return 1
    fi

    local -a passin=()
    if [ -n "${RAZZFAZZ_PACKAGE_SIGN_PASSPHRASE:-}" ]; then
        passin=(-passin env:RAZZFAZZ_PACKAGE_SIGN_PASSPHRASE)
    fi

    local out=""
    if ! out="$(openssl dgst -sha256 -sign "$keyfile" \
                    ${passin[@]+"${passin[@]}"} \
                    -out "$sig" "$archive" 2>&1)"; then
        echo "#781: signing FAILED (openssl): ${out}" >&2
        rm -f -- "$sig"
        return 1
    fi
    # No separate empty-file check: an empty signature is a signature that does
    # not verify, and the self-check below already refuses it. A second branch
    # for the same outcome is one nothing exercises.
    if ! out="$(openssl dgst -sha256 -prverify "$keyfile" \
                    ${passin[@]+"${passin[@]}"} \
                    -signature "$sig" "$archive" 2>&1)"; then
        echo "#781: the signature just written does NOT verify against the key that" >&2
        echo "      made it (openssl: ${out}). Refusing to hand out a package whose" >&2
        echo "      signature would look like tampering on the receiving box." >&2
        rm -f -- "$sig"
        return 1
    fi
    echo "#781: detached openssl signature written beside the package: ${sig}"
    return 0
}

# ==============================================================================
# rename_thin_node_agent  (#1059 P1.3 — the HARD rename, on an installed node)
# ------------------------------------------------------------------------------
# The rename of `llm-node-agent` (#1059-old-identity) -> `llm-worker-agent` is
# a HARD rename with no back-compat alias (spec 2026-08-31, binding decision 1).
# On a THIN NODE the
# service name is not just a label: container_name is fixed, the manager dials
# the node's engines by name, and the compose `environment:` block is an
# allow-list. So an installed thin node has to be moved across in the same
# upgrade that ships the new names — and the one outcome the spec explicitly
# rules out is a worker left half-renamed:
#
#     "the upgrade must rename-or-refuse, not silently skip"
#
# CONTRACT
#   0  nothing to rename (this box runs no old-name agent) — reported, not silent
#   0  renamed: the old container is gone and the new one is up
#   1  REFUSED. Either a precondition failed and NOTHING was touched, or the
#      new container did not come up and the OLD one was put back. The caller
#      must abort the upgrade; it must not carry on as if the fleet were
#      consistent.
#
# ORDER MATTERS. The obvious sequence (`rm -f` the old, then `up` the new) has
# a window in which a failed `up` leaves the node with no agent at all and
# nothing to restart — precisely the stranded worker the spec forbids. Both
# containers publish 127.0.0.1:8090, so they cannot overlap either. Hence:
# STOP (reversible) -> UP the new -> only on success REMOVE the old; on failure
# START the old again and refuse.
#
# Args: [compose-file] [env-file]   (defaults: the thin compose + .env.node)
# Env:  RZFZ_NODE_PROJECT — compose project name (default rzfz-node, the name
#       node-init pins; the node's volume/network names derive from it).
# ==============================================================================
rename_thin_node_agent() {
    local compose_file="${1:-modules/llm/node-agent/compose.thin.yml}"
    local env_file="${2:-.env.node}"
    local project="${RZFZ_NODE_PROJECT:-rzfz-node}"
    local old_name="llm-node-agent"  # #1059-old-identity (one-shot migration)
    local new_name="llm-worker-agent"

    # --- 0. is there anything to rename at all? -------------------------------
    if ! docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$old_name"; then
        print_substep "  no ${old_name} container on this box — nothing to rename"
        return 0
    fi

    # --- 0b. is it OURS? (#1059 rev-B, PR #1104 review finding B) -------------
    # The NAME alone does not identify a thin node: a full box with the llm
    # profile runs a compose-managed agent under the same container name, owned
    # by the STACK's compose project (e.g. razzfazz-stack) with no ${env_file}.
    # Only the thin-node project's container is ours to rename here. On a full
    # box the stack compose brings up the renamed service itself later in this
    # upgrade — but compose does not remove the old-named service without
    # --remove-orphans, and both publish the same host port, so the one thing
    # this step must do there is clear the old container out of the way.
    local owner
    owner=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$old_name" 2>/dev/null || true)
    if [ "$owner" != "$project" ]; then
        print_substep "  ${old_name} is owned by compose project '${owner:-<none>}' (full box)"
        print_substep "  removing the old-named service so ${new_name} can bind its port at restart"
        if ! docker rm -f "$old_name" >/dev/null 2>&1; then
            print_error "REFUSING: could not remove the old-named agent container (project '${owner:-<none>}');"
            print_error "  the renamed ${new_name} service would collide on its host port when the"
            print_error "  stack restarts. Remove it by hand (docker rm -f ${old_name}) and re-run."
            return 1
        fi
        return 0
    fi

    # --- 1. EVERY precondition, checked BEFORE the first mutation -------------
    # Collected rather than short-circuited: an operator who has to fix this by
    # hand should see the whole list once, not discover it one upgrade at a time.
    local problems=()
    docker info >/dev/null 2>&1 || problems+=("the docker daemon is not reachable")
    [ -f "$compose_file" ] || problems+=("${compose_file} is missing — broken checkout or package")
    [ -f "$env_file" ]     || problems+=("${env_file} is missing — this node has no configuration to carry over")
    if [ -f "$env_file" ] && [ ! -w "$env_file" ]; then
        problems+=("${env_file} is not writable")
    fi
    if [ -f "$env_file" ] && grep -qE '^LLM_NODE_[A-Z0-9_]+=' "$env_file" 2>/dev/null; then
        # migrate_env renames the keys earlier in the same upgrade. If the old
        # spelling is still there, the new container would start with an EMPTY
        # command key (#319 allow-list) and drop out of the fleet with a 401.
        problems+=("${env_file} still carries LLM_NODE_* keys — migrate_env has not run")
    fi
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$new_name"; then
        problems+=("a ${new_name} container already exists — refusing to run two agents against one docker socket")
    fi

    if [ "${#problems[@]}" -gt 0 ]; then
        print_error "REFUSING to rename ${old_name} -> ${new_name} on this node:"
        local p
        for p in "${problems[@]}"; do print_error "    - ${p}"; done
        print_error "  NOTHING was changed. The node still runs ${old_name} and is"
        print_error "  still reachable; fix the above and re-run the upgrade."
        return 1
    fi

    # --- 2. the rename, reversible until the last step ------------------------
    print_substep "  renaming ${old_name} -> ${new_name} (project ${project})"
    if ! docker stop "$old_name" >/dev/null 2>&1; then
        print_error "REFUSING: could not stop ${old_name}; nothing else was touched."
        return 1
    fi

    if ! docker compose -p "$project" -f "$compose_file" --env-file "$env_file" \
            up -d "$new_name" >/dev/null 2>&1; then
        print_error "REFUSED: ${new_name} did not start."
        if docker start "$old_name" >/dev/null 2>&1; then
            print_error "  ${old_name} was restarted — this node is UNCHANGED, not half-renamed."
        else
            print_error "  ${old_name} could also not be restarted. This node now runs NO"
            print_error "  agent. Start it by hand before retrying:"
            print_error "      docker start ${old_name}"
        fi
        return 1
    fi

    # Only now is the old container redundant. Removing it any earlier is what
    # would have made a failed `up` unrecoverable.
    if ! docker rm -f "$old_name" >/dev/null 2>&1; then
        print_warning "  ${new_name} is up, but the stopped ${old_name} container could"
        print_warning "  not be removed. Remove it by hand: docker rm -f ${old_name}"
    fi
    print_substep "  ${new_name} is up; ${old_name} removed"
    return 0
}


# ==============================================================================
# #1247 — razzfazz.init one-shot exit-code gate
# ==============================================================================
# Every build-only / bootstrap container in the stack carries the compose label
# `razzfazz.init: "true"`: the agent image-builders (hermes, moltis, paperclip,
# the coding-agent set), the cognee auth-shim builder, and the `*-init`
# one-shots (authentik-init, dify-init-permissions, the openuem and — since
# #1303 — the four wazuh bootstraps). The gate covers exactly what carries the
# label, nothing more; `tests/unit/consistency/test_1303_init_oneshot_labels.py`
# keeps every `restart: "no"` service labelled, so this sentence follows the
# tree instead of running ahead of it (rzfz review NIT on #1279).
# Their entire job is to run once and exit 0 — a non-zero exit
# means the artifact they exist to produce (an agent image, a permission fixup,
# an Authentik bootstrap) does not exist, and the module built on top of it can
# never work.
#
# Nothing on the install path read those exit codes. On the 0.91 clean install
# `moltis-image-builder` ended `Exited (127)` (a broken image, #1246) and
# `rzfz init` still printed "Installation Complete", wrote "Baseline checksum
# snapshot created" and returned RC 0. The day-1 tier caught it after the fact
# (#1230), but the install path itself has to say so — "RC 0" is never
# "it works" (#1201).
#
# razzfazz_init_oneshot_status is the ONE place that discovers and classifies
# them; `rzfz init`, `post-install --verify` and `rzfz status` each render its
# records in their own idiom. Discovery is by LABEL, never by a hard-coded name
# list, so a module that adds a one-shot is covered the day it lands.
# ------------------------------------------------------------------------------
RAZZFAZZ_INIT_ONESHOT_LABEL="${RAZZFAZZ_INIT_ONESHOT_LABEL:-razzfazz.init}"
# Cap on the log excerpt carried in a record. Keeps one runaway line from
# turning an operator report into a wall of text; the full log is one
# `docker logs <name>` away and every renderer says so.
RAZZFAZZ_INIT_ONESHOT_LOG_MAX="${RAZZFAZZ_INIT_ONESHOT_LOG_MAX:-200}"

# razzfazz_init_oneshot_last_log <container> — the last line that actually says
# something, flattened to ONE line so it can never break the record format.
# Only called for containers that already failed, so this is one `docker logs`
# per broken one-shot, not per container.
razzfazz_init_oneshot_last_log() {
    local name="$1" line
    # `--tail` bounds the read on a builder that logged for 20 minutes; the
    # `grep -v` drops the trailing blank line nearly every image build ends on
    # (without it the excerpt would be empty exactly when it matters most).
    line=$(docker logs --tail 40 "$name" 2>&1 \
        | tr -d '\r' | grep -v '^[[:space:]]*$' | tail -1 || true)
    line=$(_strip_ansi "$line" | tr '\n\t|' '   ')
    # Trim the whitespace the flattening can leave behind.
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] || line="(no log output)"
    printf '%s' "${line:0:$RAZZFAZZ_INIT_ONESHOT_LOG_MAX}"
}

# razzfazz_init_oneshot_status — classify every labelled one-shot.
#
# stdout: one record per container, '|'-separated, log excerpt LAST so a
#         consumer can read it with `IFS='|' read -r st name code state log`:
#
#     <OK|WARN|FAIL|STOPPED|PENDING>|<name>|<exit-code>|<docker state>|<last log line>
#
#   OK      — terminal (`exited`/`dead`) with exit code 0. It did its job.
#   WARN    — exit 0, but the last log line says `[seeder] SKIPPED`: it
#             skipped its work on purpose (e.g. the admin password in .env is
#             not the box's). Completed, and said so; never fatal.
#   FAIL    — terminal with a non-zero exit code it produced ITSELF. The
#             artifact is missing. This is the #1247 case (moltis: 127).
#   STOPPED — terminal by a SIGNAL that means "the stack went down under it":
#             130/SIGINT, 137/SIGKILL, 143/SIGTERM, and NOT OOM-killed. Not a
#             verdict on the one-shot, so never fatal. This matters: several
#             agent image-builders carry `command: ["true"]` but sit on an
#             entrypoint that ignores argv, so they run until the stack stops
#             and every one of them shows 137 afterwards. A gate that FAILs a
#             stopped-but-healthy box is a gate operators learn to ignore.
#             Deliberately NOT extended to every 128+n code — 134/SIGABRT and
#             139/SIGSEGV are the builder crashing, and stay FAIL.
#             An OOM kill IS a verdict: 137 with OOMKilled=true stays FAIL
#             (that is the #693 build-RAM class, and it must stay loud).
#   PENDING — created/running/restarting/paused, i.e. not finished yet.
#             Reported, never fatal: a heavy image build (moltis is 10-30 min)
#             legitimately outlives the step that started it, and the existing
#             init health gate does not fail on a still-starting container.
#
# Return: 1 if at least one FAIL was emitted, else 0.
razzfazz_init_oneshot_status() {
    # No docker CLI at all stays QUIET (test_1247 pins it): `rzfz status` runs
    # on such boxes and reports the missing CLI itself (cli/status.sh, STACK
    # STATE) — a second record here would be the same fact twice. #1301 is
    # about a CLI that exists and cannot reach the daemon (below).
    command -v docker >/dev/null 2>&1 || return 0
    local proj names name insp state code oom rc=0 ps_rc=0 errf why _last
    local filters=(--filter "label=${RAZZFAZZ_INIT_ONESHOT_LABEL}")
    # Scope to THIS stack's compose project when it can be determined, so a
    # leftover one-shot from another project (a second checkout, an ephemeral
    # pytest stack) cannot FAIL a healthy box. Same detection as
    # remove_compose_orphans_safe. When it cannot be determined, fall back to
    # the bare label filter: over-reporting is recoverable by reading the name,
    # whereas silently reporting nothing IS the #1247 defect.
    proj=$(docker compose config 2>/dev/null | awk '/^name:/{print $2; exit}' || true)
    [ -n "$proj" ] && filters+=(--filter "label=com.docker.compose.project=${proj}")
    # #1301: keep `docker ps`'s exit code. A daemon that is unreachable, a user
    # outside the docker group, a socket proxy that is gone — all of them used
    # to produce the exact output of "this box has no one-shots": nothing, RC 0,
    # and --verify turned that into a green PASS. Now it is its own record.
    errf=$(mktemp "${TMPDIR:-/tmp}/rzfz-oneshot.XXXXXX" 2>/dev/null) || errf=/dev/null
    names=$(docker ps -a "${filters[@]}" --format '{{.Names}}' 2>"$errf") || ps_rc=$?
    if [ "$ps_rc" -ne 0 ]; then
        why=$(head -1 "$errf" 2>/dev/null | tr -d '|' || true)
        [ "$errf" != /dev/null ] && rm -f "$errf"
        [ -n "$why" ] || why="docker ps exited $ps_rc"
        printf 'UNKNOWN|docker ps|%s|unreachable|%s\n' "$ps_rc" "$why"
        return 2
    fi
    [ "$errf" != /dev/null ] && rm -f "$errf"
    names=$(printf '%s\n' "$names" | sort)
    # #1302: the EXPECTED set. `docker compose config` resolves the active
    # profiles, so every labelled service in it is a one-shot this box MUST
    # have created. One compose never got to create (an image build that
    # aborted, a create that failed) is otherwise simply absent from the
    # classified set — "All N exited 0" over a set without the broken case.
    # Non-JSON output (older compose, no project here) yields no expectation
    # and therefore no MISSING rows: unknown, not silently green.
    local label_key="${RAZZFAZZ_INIT_ONESHOT_LABEL%%=*}" expected="" present="" svc
    expected=$(docker compose config --format json 2>/dev/null | LABEL_KEY="$label_key" python3 -c '
import json, os, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(0)
key = os.environ["LABEL_KEY"]
for name, svc in sorted((doc.get("services") or {}).items()):
    labels = (svc or {}).get("labels") or {}
    if isinstance(labels, list):
        labels = dict(item.split("=", 1) for item in labels if "=" in item)
    if str(labels.get(key, "")).lower() == "true":
        print(name)
' 2>/dev/null) || expected=""
    if [ -n "$expected" ]; then
        present=$(docker ps -a "${filters[@]}" --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null || true)
    fi
    if [ -z "$names" ] && [ -z "$expected" ]; then
        return 0
    fi
    [ -n "$names" ] && while IFS= read -r name; do
        [ -n "$name" ] || continue
        # ONE inspect per container for all three fields.
        insp=$(docker inspect \
            -f '{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}' \
            "$name" 2>/dev/null || true)
        IFS='|' read -r state code oom <<< "$insp"
        [ -n "$state" ] || state="unknown"
        [ -n "$code" ]  || code="?"
        case "$state" in
            exited|dead)
                # #855 rev-B lesson: `.State.ExitCode` reads 0 on a container
                # that has not run yet, so the STATE has to be terminal before
                # the code means anything. Reading the code alone is how a 127
                # gets waved through.
                if [ "$code" = "0" ]; then
                    _last=$(razzfazz_init_oneshot_last_log "$name")
                    case "$_last" in
                        "[seeder] SKIPPED"*) printf 'WARN|%s|%s|%s|%s\n' "$name" "$code" "$state" "$_last" ;;
                        *) printf 'OK|%s|%s|%s|\n' "$name" "$code" "$state" ;;
                    esac
                elif [ "$oom" != "true" ] && { [ "$code" = "137" ] || [ "$code" = "143" ] || [ "$code" = "130" ]; }; then
                    printf 'STOPPED|%s|%s|%s|\n' "$name" "$code" "$state"
                else
                    printf 'FAIL|%s|%s|%s|%s\n' "$name" "$code" "$state" \
                        "$(razzfazz_init_oneshot_last_log "$name")"
                    rc=1
                fi
                ;;
            *)
                printf 'PENDING|%s|%s|%s|\n' "$name" "$code" "$state"
                ;;
        esac
    done <<< "$names"
    if [ -n "$expected" ]; then
        while IFS= read -r svc; do
            [ -n "$svc" ] || continue
            printf '%s\n' "$present" | grep -Fxq -- "$svc" && continue
            printf 'MISSING|%s|?|absent|labelled %s but no container was created — compose up never got that far (image build or container create failed)\n' \
                "$svc" "$RAZZFAZZ_INIT_ONESHOT_LABEL"
            rc=1
        done <<< "$expected"
    fi
    return "$rc"
}

# razzfazz_init_oneshot_gate — the operator-facing rendering used by `rzfz init`.
# Emits the line the issue asks for, verbatim:
#
#     [FAIL] <name> exited <code>: <last log line>
#
# Return: 1 if any one-shot failed, else 0. PENDING/STOPPED are reported and
# ignored — neither is a verdict on the one-shot itself.
razzfazz_init_oneshot_gate() {
    local records st name code state log fails=0 unfinished=0 ok=0 unknown=0
    records=$(razzfazz_init_oneshot_status) || true
    if [ -z "$records" ]; then
        print_substep "  no ${RAZZFAZZ_INIT_ONESHOT_LABEL} one-shot containers on this box"
        return 0
    fi
    while IFS='|' read -r st name code state log; do
        [ -n "$st" ] || continue
        case "$st" in
            OK)
                ok=$((ok + 1))
                ;;
            WARN)
                ok=$((ok + 1))
                print_warning "[WARN] $name: $log (docker logs $name)"
                ;;
            PENDING)
                unfinished=$((unfinished + 1))
                print_warning "[PENDING] $name is still ${state} — it has not finished, so its exit code cannot be judged yet"
                ;;
            STOPPED)
                unfinished=$((unfinished + 1))
                print_warning "[STOPPED] $name was signalled ($code) before it finished — nothing was proven either way; re-check once the stack is up"
                ;;
            FAIL)
                fails=$((fails + 1))
                print_error "[FAIL] $name exited $code: $log"
                print_info "        full log: docker logs $name"
                ;;
            MISSING)
                # #1302: labelled in the active profiles, never created.
                fails=$((fails + 1))
                print_error "[MISSING] $name — $log"
                ;;
            UNKNOWN)
                # #1301: nothing was measured — say so, never claim a verdict.
                unknown=$((unknown + 1))
                print_warning "[UNKNOWN] the ${RAZZFAZZ_INIT_ONESHOT_LABEL} one-shots could not be inspected ($name: $log) — nothing was proven either way; fix docker access and re-check with 'rzfz status'"
                ;;
        esac
    done <<< "$records"
    if [ "$fails" -eq 0 ]; then
        if [ "$unknown" -gt 0 ] && [ "$ok" -eq 0 ]; then
            print_warning "No ${RAZZFAZZ_INIT_ONESHOT_LABEL} one-shot was inspected — this gate has no verdict (#1301)"
            return 0
        fi
        local suffix=""
        [ "$unfinished" -gt 0 ] && suffix=" (${unfinished} unfinished)"
        print_success "All $ok completed ${RAZZFAZZ_INIT_ONESHOT_LABEL} one-shot(s) exited 0${suffix}"
        return 0
    fi
    print_error "$fails ${RAZZFAZZ_INIT_ONESHOT_LABEL} one-shot(s) failed — the modules that depend on them cannot work"
    return 1
}

# ── Final health gate (#2873) ────────────────────────────────────────────────
# A `docker compose up` that aborts (a bad command, a missing image, a port
# clash) leaves every container it had not reached in `Created`. The callers
# tolerate one failing up so later steps still run (#153), and the run used to
# end rc 0 with 36 of 67 services never started. This census is the FINAL
# verdict, read from `docker compose ps -a` (only the ENABLED profiles are
# listed there):
#   - a CORE service must be running and not unhealthy/starting. The core set
#     is the fixed list below, narrowed to the services the active compose
#     config defines (`docker compose config --services`), so a box without
#     the llm module is not failed for llm-manager;
#   - ANY other ACTIVE service in `created` never ran: FAIL, it is not "starting";
#   - ANY other ACTIVE service in `exited` with a non-zero code, `dead`, or
#     `restarting`: FAIL; so is a `running` one that is `unhealthy` or whose
#     RestartCount rose while the gate watched it (#2957). One classifier,
#     razzfazz_service_problem, serves the core and the non-core loop.
# `ps -a` lists every container of the project, including the stopped ones of a
# module that was switched off (a Portal disable stops, never removes; docker
# stop's kill leaves Exited 137). "Active" is ONE set for both loops: the
# services `docker compose config --services` defines under the box's current
# COMPOSE_PROFILES (#2957). A container outside it is expected state, never a
# finding; razzfazz_inactive_leftovers lists it for an informational line. If
# the config cannot be read the set is empty and nothing is narrowed (fail safe).
# One record per finding on stdout: `<service>|<reason>`. Return 1 if any.
# An unreadable census (docker or compose unreachable) is itself a finding.
RAZZFAZZ_CORE_SERVICES="authentik-server authentik-worker caddy postgres valkey llm-manager razzfazz-config razzfazz-start-portal"
# razzfazz_service_is_active <defined> <service> - 0 when the service is in the
# active compose config; also 0 when that set is empty (unreadable config).
razzfazz_service_is_active() {
    [ -z "$1" ] && return 0
    printf '%s\n' "$1" | grep -qx -- "$2"
}

# razzfazz_inactive_leftovers - one service name per line: containers `ps -a`
# lists whose service is NOT in the active compose config (#2957). Informational.
razzfazz_inactive_leftovers() {
    local defined ps svc
    command -v docker >/dev/null 2>&1 || return 0
    defined=$(docker compose config --services 2>/dev/null || true)
    [ -n "$defined" ] || return 0
    ps=$(docker compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>/dev/null | awk -F'|' '{print $1}') || return 0
    while IFS= read -r svc; do
        [ -n "$svc" ] || continue
        razzfazz_service_is_active "$defined" "$svc" || printf '%s\n' "$svc"
    done <<< "$(printf '%s\n' "$ps" | LC_ALL=C sort -u)"
    return 0
}

# razzfazz_inactive_leftovers_hint - the one informational line (empty if none).
razzfazz_inactive_leftovers_hint() {
    local left
    left=$(razzfazz_inactive_leftovers | tr '\n' ' ' | sed 's/ *$//')
    [ -n "$left" ] || return 0
    printf 'Stopped containers left over from a disabled module (ignored): %s - remove with: docker compose rm -f %s\n' "$left" "$left"
}

# razzfazz_restart_counts - `<service>|<RestartCount>` per container of the
# project (the highest count wins when a service has several). Empty when the
# counts cannot be read; callers then treat "no restart data" as "no signal".
razzfazz_restart_counts() {
    local ids
    ids=$(docker compose ps -aq 2>/dev/null) || return 0
    [ -n "$ids" ] || return 0
    # shellcheck disable=SC2086
    docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}|{{.RestartCount}}' $ids 2>/dev/null \
        | awk -F'|' '$1!="" && $2 ~ /^[0-9]+$/ { if (!($1 in m) || $2+0 > m[$1]) m[$1]=$2+0 } END { for (k in m) print k "|" m[k] }' \
        | LC_ALL=C sort
}

# razzfazz_service_problem <core 0|1> <state> <health> <exit code> <restarts gained>
# - THE classification both loops of the final gate (and so `rzfz status`) use.
# Prints the reason and returns 0 when an ACTIVE service is not fine; prints
# nothing and returns 1 otherwise (#2957). A `restarting` row carries an empty
# Health and ExitCode 0, so the state itself is the signal; a restart loop that
# is caught between two crashes shows `running`, so the signal there is a
# RestartCount that rose inside the sample window. `unhealthy` is only reported
# by docker once the start period is over, `starting` is a finding for a core
# service only.
razzfazz_service_problem() {
    local core="$1" state="$2" health="$3" code="$4" gained="${5:-0}"
    case "$state" in
        running)
            if [ "$gained" -gt 0 ] 2>/dev/null; then
                printf 'restart loop - restarted %s time(s) while the gate watched it\n' "$gained"
            elif [ "$health" = "unhealthy" ]; then
                if [ "$core" = 1 ]; then printf 'core service is running but unhealthy\n'
                else printf 'running but unhealthy - its healthcheck fails\n'; fi
            elif [ "$health" = "starting" ] && [ "$core" = 1 ]; then
                printf 'core service is running but starting\n'
            else
                return 1
            fi ;;
        restarting)
            if [ "$core" = 1 ]; then printf 'core service is restarting, not running\n'
            else printf 'restarting - it is crash-looping\n'; fi ;;
        created)
            if [ "$core" = 1 ]; then printf 'core service is created, not running\n'
            else printf 'left in Created - it never ran\n'; fi ;;
        dead)
            if [ "$core" = 1 ]; then printf 'core service is dead, not running\n'
            else printf 'dead\n'; fi ;;
        exited)
            if [ "$core" = 1 ]; then printf 'core service is exited, not running\n'
            elif [ "${code:-0}" != "0" ]; then printf 'exited with code %s\n' "$code"
            else return 1; fi ;;
        *)
            if [ "$core" = 1 ]; then printf 'core service is %s, not running\n' "$state"
            else return 1; fi ;;
    esac
    return 0
}

# Seconds the gate watches RestartCount before it judges (0 turns the watch off).
RAZZFAZZ_HEALTH_RESTART_WINDOW="${RAZZFAZZ_HEALTH_RESTART_WINDOW:-8}"

razzfazz_final_health_findings() {
    command -v docker >/dev/null 2>&1 || { printf 'docker|the docker CLI is not available, so the stack state cannot be read\n'; return 1; }
    local ps defined core svc state health code bad=0 line why counts1 counts2 gained
    # First RestartCount sample. Only a box with a restart on record needs the
    # window; a healthy one has none, so the gate does not wait for nothing.
    counts1=$(razzfazz_restart_counts)
    if ! ps=$(docker compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>/dev/null); then
        printf 'docker compose|"docker compose ps -a" failed, so the stack state cannot be read\n'
        return 1
    fi
    if [ "${RAZZFAZZ_HEALTH_RESTART_WINDOW:-0}" -gt 0 ] 2>/dev/null \
       && { printf '%s\n' "$counts1" | grep -qv '|0$' && [ -n "$counts1" ] || printf '%s\n' "$ps" | grep -q '|restarting|'; }; then
        sleep "$RAZZFAZZ_HEALTH_RESTART_WINDOW"
        ps=$(docker compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>/dev/null) || ps="$ps"
    fi
    counts2=$(razzfazz_restart_counts)
    defined=$(docker compose config --services 2>/dev/null || true)
    for core in $RAZZFAZZ_CORE_SERVICES; do
        razzfazz_service_is_active "$defined" "$core" || continue
        line=$(printf '%s\n' "$ps" | awk -F'|' -v s="$core" '$1==s{print; exit}')
        if [ -z "$line" ]; then
            printf '%s|core service has no container (never created)\n' "$core"; bad=1; continue
        fi
        IFS='|' read -r svc state health code <<< "$line"
        gained=$(razzfazz_restart_gain "$core" "$counts1" "$counts2")
        if why=$(razzfazz_service_problem 1 "$state" "$health" "$code" "$gained"); then
            printf '%s|%s\n' "$core" "$why"; bad=1
        fi
    done
    while IFS='|' read -r svc state health code; do
        [ -n "$svc" ] || continue
        case " $RAZZFAZZ_CORE_SERVICES " in *" $svc "*) continue ;; esac
        razzfazz_service_is_active "$defined" "$svc" || continue
        gained=$(razzfazz_restart_gain "$svc" "$counts1" "$counts2")
        if why=$(razzfazz_service_problem 0 "$state" "$health" "$code" "$gained"); then
            printf '%s|%s\n' "$svc" "$why"; bad=1
        fi
    done <<< "$ps"
    return $bad
}

# razzfazz_restart_gain <service> <counts before> <counts after> - how many
# restarts the service gained between the two samples (0 when either is missing).
razzfazz_restart_gain() {
    local a b
    a=$(printf '%s\n' "$2" | awk -F'|' -v s="$1" '$1==s{print $2; exit}')
    b=$(printf '%s\n' "$3" | awk -F'|' -v s="$1" '$1==s{print $2; exit}')
    if [ -n "$a" ] && [ -n "$b" ] && [ "$b" -gt "$a" ] 2>/dev/null; then echo $((b - a)); else echo 0; fi
}

# razzfazz_final_health_gate - print the findings and the next step; return 1
# when there are any. The caller decides what a failure costs (upgrade: no
# version stamp and a non-zero exit; init: exit 7).
razzfazz_final_health_gate() {
    local findings name why left
    if findings=$(razzfazz_final_health_findings); then
        print_success "Final health gate: every core service is up and nothing is left in Created."
        left=$(razzfazz_inactive_leftovers_hint); [ -z "$left" ] || print_info "  $left"
        return 0
    fi
    print_error "Final health gate FAILED - the stack is not up:"
    while IFS='|' read -r name why; do
        [ -n "$name" ] || continue
        print_error "    ${name}: ${why}"
    done <<< "$findings"
    left=$(razzfazz_inactive_leftovers_hint); [ -z "$left" ] || print_info "  $left"
    print_info "  Why:        docker compose ps -a   -   docker compose logs <service>"
    print_info "  Next step:  fix the cause above, run: docker compose up -d   - then re-run this command"
    return 1
}

# ── llama.cpp runner images (#1516, operator decision E5) ────────────────────
# ONE source of truth: modules/llm/runners/runners.yaml. Before this, the same
# set was written out in cli/init.sh, cli/upgrade.sh, cli/post-install.sh (build
# + publish list) and modules/llm/node-agent/app/drivers/images.py — five copies,
# and #1497 was exactly that drift (the publish list named an image nothing
# builds). Emits one TSV row per target:
#     <image:tag>\t<dockerfile>\t<hardware>\t<legacy image:tag>\t<build args>
# Silent (no rows) when PyYAML or the manifest is missing, so a caller can fall
# back rather than die.
# #3403: $2 = which channels: `all` (default — the packager, the SBOM, the
# manifest guards) or `stable` (a box that has not opted in to the experimental
# runner: its targets are left out, so nothing builds, loads or publishes them).
# A box's own answer is razzfazz_runner_channels <env file>.
razzfazz_runner_manifest_rows() {
    local root="${1:-${SCRIPT_DIR:-.}}"
    RZFZ_RUNNER_CHANNELS="${2:-all}" YAML_PATH="${root}/modules/llm/runners/runners.yaml" python3 - <<'PYEOF'
import os, sys
try:
    import yaml
except ImportError:
    sys.exit(0)
try:
    with open(os.environ["YAML_PATH"], encoding="utf-8") as fh:
        m = yaml.safe_load(fh) or {}
except OSError:
    sys.exit(0)
repo = m.get("repository") or "llama-runner"
stable_only = os.environ.get("RZFZ_RUNNER_CHANNELS", "all") == "stable"
for t in m.get("targets") or []:
    if stable_only and t.get("channel") == "experimental":
        continue
    args = " ".join(f"{k}={v}" for k, v in (t.get("build_args") or {}).items())
    print("\t".join([f"{repo}:{t['tag']}", t["dockerfile"], t.get("hardware", ""),
                      t.get("legacy", ""), args]))
PYEOF
}

# razzfazz_experimental_runner_enabled <env file> — #3403: has this box opted
# in to the experimental runner channel (RAZZFAZZ_EXPERIMENTAL_RUNNER)? Off
# unless the key says true/1/yes/on: an absent key is the upgrade's default.
razzfazz_experimental_runner_enabled() {
    local v
    v="$(read_env_value "${1:-.env}" RAZZFAZZ_EXPERIMENTAL_RUNNER)"
    case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
        true|1|yes|on) return 0 ;;
    esac
    return 1
}

# razzfazz_runner_channels <env file> — `all` when the box opted in, `stable`
# otherwise: the second argument of razzfazz_runner_manifest_rows and
# razzfazz_runner_package_plan on every BOX-side caller (build, load, publish).
razzfazz_runner_channels() {
    if razzfazz_experimental_runner_enabled "${1:-.env}"; then echo all; else echo stable; fi
}

# razzfazz_runner_experimental_refs <stack_root> — #3403: `<repo>:<tag>` of
# every experimental-channel target, one per line (the packager records them in
# expected-images.json so a box that has not opted in leaves them unloaded).
razzfazz_runner_experimental_refs() {
    local root="${1:-${SCRIPT_DIR:-.}}"
    YAML_PATH="${root}/modules/llm/runners/runners.yaml" python3 - 2>/dev/null <<'PYEOF'
import os
try:
    import yaml
    m = yaml.safe_load(open(os.environ["YAML_PATH"], encoding="utf-8")) or {}
except Exception:
    m = {}
repo = m.get("repository") or "llama-runner"
for t in m.get("targets") or []:
    if t.get("channel") == "experimental" and t.get("tag"):
        print(f"{repo}:{t['tag']}")
PYEOF
    return 0
}

# razzfazz_runner_build_context <stack_root> — the docker build CONTEXT of every
# runner target, relative to the stack root: runners.yaml's `build_context`.
# It is the runners directory, not a target's own directory: the Dockerfiles
# `COPY llama-server-shim`, which lives beside them, and a build from
# <target>/ fails only at that COPY, after the base image pulls (and on the CUDA
# targets after a long compile stage). A separate reader for the same reason as
# razzfazz_runner_manifest_arch_rows: the rows are read positionally. Prints the
# historical default when PyYAML or the key is missing, so a build never loses
# its context.
razzfazz_runner_build_context() {
    local root="${1:-${SCRIPT_DIR:-.}}" ctx
    ctx=$(YAML_PATH="${root}/modules/llm/runners/runners.yaml" python3 - 2>/dev/null <<'PYEOF'
import os
try:
    import yaml
    with open(os.environ["YAML_PATH"], encoding="utf-8") as fh:
        c = (yaml.safe_load(fh) or {}).get("build_context") or ""
except Exception:
    c = ""
print(str(c).strip().rstrip("/"))
PYEOF
) || ctx=""
    printf '%s\n' "${ctx:-modules/llm/runners}"
}

# The hardware CLASS a runner target is keyed on. The fleet writes HARDWARE in
# several dialects (`nvidia` from the installer, `cuda-gb10` from a GB10
# enrolment); the manifest speaks amd | cuda | cpu.
razzfazz_runner_hw_class() {
    case "${1:-}" in
        nvidia|cuda|cuda-gb10|gb10|nvidia-gb10) echo "cuda" ;;
        amd|amd-*|rocm|vulkan)                  echo "amd" ;;
        cpu)                                    echo "cpu" ;;
        *)                                      echo "${1:-}" ;;
    esac
}

# razzfazz_runner_package_plan <stack_root> — what an OFFLINE PACKAGE should
# carry for the llama.cpp runners. One decision per line:
#
#     carry<TAB><ref>
#     absent<TAB><ref><TAB><hw-class><TAB><arch><TAB><the build command that produces it>
#
# WHY THIS IS NOT `engine_runner_images()`. That one answers "which runner does
# THIS box launch" and returns exactly one BY DESIGN — its docstring is explicit
# that a flat list would tell every correctly-provisioned box it is missing two
# images. A package answers a different question: which runner could ANY box
# that unpacks this archive launch? Measured on the AMD dev box, 2026-09-08:
# `runners.yaml` defines six targets and the packager saved ONE, so an NVIDIA
# target box found no runner in the archive at all — and `pull_policy: never`
# means it never gets one afterwards. Two questions, one function (#1309).
#
# `absent` is a first-class output, not an omission. A packaging host cannot
# carry what it does not have, and the arm64 GB10 target cannot even be built on
# an amd64 host (runners.yaml: every tag is single-architecture by
# construction). Naming what is missing is the fix; dropping it silently was the
# defect.
#
# `<arch>` is on the row because NAMING the gap was not enough (#2154): the
# packager still exited 0 and wrote the archive. To refuse instead, the caller
# has to separate "this host could have built it and did not" from "this host
# never could", and only the manifest knows which is which.
#
# Legacy aliases are carried when present — a box pinned via
# RAZZFAZZ_ENGINE_IMAGE_* resolves the old name, and offline there is no second
# try — but their ABSENCE is not reported: it only ever means this host did not
# build that target, which the canonical line already says.
#
# Presence is asked of docker, so a host without docker reports every target
# `absent`. That is the truthful answer for a host that can carry nothing, and
# it keeps the packager's warning honest instead of silently empty.
# razzfazz_runner_manifest_arch_rows <stack_root> — `<ref><TAB><arch>` per target.
#
# A SEPARATE reader rather than a sixth column on razzfazz_runner_manifest_rows:
# cli/init.sh, cli/upgrade.sh and cli/post-install.sh all read those rows
# positionally and take the BUILD ARGS last, so an appended field would be handed
# to `docker build --build-arg`. Adding a reader costs one function; widening the
# row costs three silent call sites.
razzfazz_runner_manifest_arch_rows() {
    local root="${1:-${SCRIPT_DIR:-.}}"
    YAML_PATH="${root}/modules/llm/runners/runners.yaml" python3 - <<'PYEOF'
import os, sys
try:
    import yaml
except ImportError:
    sys.exit(0)
try:
    with open(os.environ["YAML_PATH"], encoding="utf-8") as fh:
        m = yaml.safe_load(fh) or {}
except OSError:
    sys.exit(0)
repo = m.get("repository") or "llama-runner"
for t in m.get("targets") or []:
    print("\t".join([f"{repo}:{t['tag']}", t.get("arch", "")]))
PYEOF
}

razzfazz_runner_package_plan() {
    local root="${1:-${SCRIPT_DIR:-.}}" channels="${2:-all}"
    local row image dockerfile hw legacy args a build arch_rows arch rest ctx
    arch_rows="$(razzfazz_runner_manifest_arch_rows "$root")"
    ctx="$(razzfazz_runner_build_context "$root")"
    # `|| [ -n "$row" ]`: a final row without a trailing newline is otherwise
    # dropped — measured on 0.91 for the alias verdict (#979), and cheap here.
    # #2666: the row is split with parameter expansion, NOT `IFS=$'\t' read`.
    # Tab is IFS whitespace for `read`, so two tabs in a row collapse and an
    # empty field (a target without a legacy alias) vanishes — the build args
    # then slide into the alias slot and the package carries a bogus ref.
    while IFS= read -r row || [ -n "$row" ]; do
        case "$row" in *$'\t'*) ;; *) row="${row}"$'\t\t\t\t' ;; esac   # image only
        image="${row%%$'\t'*}";      rest="${row#*$'\t'}"
        dockerfile="${rest%%$'\t'*}"; rest="${rest#*$'\t'}"
        hw="${rest%%$'\t'*}";         rest="${rest#*$'\t'}"
        legacy="${rest%%$'\t'*}";     args="${rest#*$'\t'}"
        [ "$rest" = "$args" ] && args=""   # no tab left: the row ended early
        [ -n "$image" ] || continue
        if docker image inspect "$image" >/dev/null 2>&1; then
            printf 'carry\t%s\n' "$image"
        else
            build="docker build -f ${dockerfile}"
            for a in $args; do build="${build} --build-arg ${a}"; done
            # #2208: the CONTEXT is the runners directory, not the repo root.
            # Three of the five runner Dockerfiles do `COPY llama-server-shim …`,
            # and the shim lives beside them at modules/llm/runners/. With `.`
            # the emitted line dies in three seconds on
            #     COPY failed: stat llama-server-shim: file does not exist
            # — a message that names neither the context nor the file's real
            # home. This text is the REMEDIATION printed by #2154's fail-closed
            # gate, so an operator who follows it and watches it fail concludes
            # the packaging system is broken rather than the instruction.
            # Read from runners.yaml's `build_context`, the same value every
            # build site uses.
            build="${build} -t ${image} ${ctx}"
            arch="$(printf '%s\n' "$arch_rows" | awk -F'\t' -v i="$image" '$1==i {print $2; exit}')"
            printf 'absent\t%s\t%s\t%s\t%s\n' "$image" "$hw" "${arch:-unknown}" "$build"
        fi
        if [ -n "$legacy" ] && docker image inspect "$legacy" >/dev/null 2>&1; then
            printf 'carry\t%s\n' "$legacy"
        fi
    done <<< "$(razzfazz_runner_manifest_rows "$root" "$channels")"
    # Explicit: the last `docker image inspect` must not become this function's
    # exit status — every caller runs under `set -e`.
    return 0
}

# razzfazz_link_cli_onto_path <stack_root> [target_dir] — put `rzfz` on PATH (#1941).
#
# WHY THIS EXISTS. The customer documentation invokes the CLI **bare** —
# `rzfz status`, `rzfz backup backup`, `rzfz upgrade` — across 369 call sites in
# `docs/enterprise/`, and `modules/tools/appliance/autoinstall/user-data:8`
# documents the FIRST command after an appliance install as
# `cd ~/razzfazz-ai-service-stack && rzfz init`, which does not work as written:
# after the `cd` the shell still searches PATH, not the working directory.
# Whoever wrote that believed `rzfz` was on PATH. Measured on
# 0.91 (2026-09-11): it is on PATH nowhere — not /usr/bin, not /usr/local/bin,
# no alias, no shell function — and no installer in the tree created an entry.
# A customer following the documentation literally gets `command not found`,
# starting with the very first command.
#
# Operator decision (2026-09-10): the install creates the entry; the
# documentation stays as it is. The alternative was rewriting 369 lines, which
# makes the documentation worse for the people it is written for.
#
# ORDER MATTERS: this needs `rzfz` to resolve its own symlink (#1942), which it
# does since that change. Creating the entry WITHOUT it turns `command not
# found` into `No such file or directory` — a message that reads like a broken
# stack rather than a missing install step, i.e. strictly worse.
#
# A FOREIGN ENTRY IS NEVER OVERWRITTEN. An existing `rzfz` on PATH pointing at a
# DIFFERENT stack directory is the one case where being helpful does damage:
# two checkouts on one box, and the installer silently rewires the operator's
# commands to the other one. It is named and refused instead. (Old boxes carry
# exactly such leftovers — 0.78 does.)
#
# #1941 REOPENED (journey A's dry run, 0.91, 2026-09-15): the default target
# is /usr/local/bin, `rzfz init` runs as the ordinary operator, so `ln -s`
# there fails with EACCES on EVERY normal install — the function returned 3,
# the caller's `|| true` kept the install green, and the entry never existed.
# Return 3 was the NORMAL outcome, not the exceptional one. So: when the
# default target cannot be written, try it with passwordless sudo (never a
# prompt), and otherwise fall back to the operator's own ~/.local/bin — which
# Ubuntu's ~/.profile puts on PATH at login when the directory exists — and
# say which one was used and what it takes to see it (a new login). The
# foreign-entry refusal applies to the fallback location too.
#
# Test hooks: RZFZ_PATH_TARGET_DIR (the default target), RZFZ_PATH_FALLBACK_DIR
# (default $HOME/.local/bin), RZFZ_PATH_PROFILE (default $HOME/.profile),
# RZFZ_PATH_TRY_SUDO=0 (skip the sudo attempt).
#
# Returns: 0 linked or already correct (at the target or the fallback),
#          2 refused (foreign entry), 3 could not write anywhere — never fatal
#          to the caller.
razzfazz_link_cli_onto_path() {
    local stack_root="${1:?stack_root required}"
    local explicit_target="${2:-}"
    local target_dir="${2:-${RZFZ_PATH_TARGET_DIR:-/usr/local/bin}}"
    local src="${stack_root%/}/rzfz"

    if [ ! -x "$src" ]; then
        echo "[rzfz-path] no executable rzfz at $src — not linking" >&2
        return 3
    fi

    # Resolve BOTH sides before comparing: the existing entry may itself be a
    # symlink chain, and `$src` may be reached through one.
    local src_real; src_real="$(readlink -f "$src" 2>/dev/null || printf '%s' "$src")"

    # _rzfz_path_try DIR → 0 linked/already correct, 2 foreign entry, 3 unwritable
    _rzfz_path_try() {
        local dir="$1" link
        link="${dir%/}/rzfz"
        if [ -e "$link" ] || [ -L "$link" ]; then
            local link_real; link_real="$(readlink -f "$link" 2>/dev/null || printf '%s' "$link")"
            if [ "$link_real" = "$src_real" ]; then
                return 0                  # already correct — idempotent
            fi
            echo "[rzfz-path] REFUSING to replace $link" >&2
            echo "            it points at : ${link_real:-<unresolvable>}" >&2
            echo "            this stack is: $src_real" >&2
            echo "            Two stack checkouts on one box: rewiring the operator's" >&2
            echo "            commands to the other one is worse than doing nothing." >&2
            echo "            Remove or repoint it by hand, then re-run (#1941)." >&2
            return 2
        fi
        ln -s "$src_real" "$link" 2>/dev/null || return 3
        echo "[rzfz-path] linked $link -> $src_real"
        return 0
    }

    local rc=0
    _rzfz_path_try "$target_dir" || rc=$?
    [ "$rc" -ne 3 ] && return "$rc"

    # The default target is not writable by this user (the normal install).
    if [ -n "$explicit_target" ]; then
        echo "[rzfz-path] could not create ${target_dir%/}/rzfz (not writable?)." >&2
        echo "            Run this once as root: ln -s '$src_real' '${target_dir%/}/rzfz'" >&2
        return 3
    fi
    if [ "${RZFZ_PATH_TRY_SUDO:-1}" = "1" ] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        if sudo -n ln -s "$src_real" "${target_dir%/}/rzfz" 2>/dev/null; then
            echo "[rzfz-path] linked ${target_dir%/}/rzfz -> $src_real (via passwordless sudo)"
            return 0
        fi
    fi

    local fallback="${RZFZ_PATH_FALLBACK_DIR:-${HOME:-/nonexistent}/.local/bin}"
    mkdir -p "$fallback" 2>/dev/null || true
    rc=0
    _rzfz_path_try "$fallback" || rc=$?
    if [ "$rc" -eq 2 ]; then
        return 2
    fi
    if [ "$rc" -ne 0 ]; then
        echo "[rzfz-path] could not create ${target_dir%/}/rzfz (not writable) nor $fallback/rzfz." >&2
        echo "            Run this once as root: ln -s '$src_real' '${target_dir%/}/rzfz'" >&2
        return 3
    fi
    # Make sure a LOGIN shell sees the fallback. Ubuntu's stock ~/.profile adds
    # ~/.local/bin when the directory exists; a profile without that clause
    # gets one line, appended once.
    local profile="${RZFZ_PATH_PROFILE:-${HOME:-/nonexistent}/.profile}"
    if [ -e "$profile" ] && ! grep -q '\.local/bin' "$profile" 2>/dev/null; then
        printf '\n# rzfz (#1941): the CLI lives here when /usr/local/bin is not writable\nexport PATH="%s:$PATH"\n' "$fallback" >> "$profile" 2>/dev/null || true
    elif [ ! -e "$profile" ]; then
        printf '# rzfz (#1941): the CLI lives here when /usr/local/bin is not writable\nexport PATH="%s:$PATH"\n' "$fallback" > "$profile" 2>/dev/null || true
    fi
    echo "[rzfz-path] ${target_dir%/} is not writable by $(id -un 2>/dev/null || echo "$USER"); rzfz is on PATH for LOGIN shells via $fallback."
    echo "            In THIS shell: export PATH=\"$fallback:\$PATH\"   — or log in again."
    echo "            System-wide (once, as root): ln -s '$src_real' '${target_dir%/}/rzfz'"
    return 0
}

# ------------------------------------------------------------------------------
# #154 (v3 — OWNERSHIP) / #2524: reconcile per-service DB *object ownership*.
#
# reconcile_service_db_users (cli/upgrade.sh) fixes which ROLE a service
# CONNECTS as. But on a box first provisioned before per-service roles existed,
# the objects were CREATED by the global `docker` superuser (empty→docker
# fallback). After the *_DB_USER is (correctly) pointed at the per-service role,
# the service can CONNECT but not ALTER its own objects → a migration dies with
# `must be owner of table ...` (onyx-api crash-loop, 2026-07; also paperless).
#
# #2524: tables, views and sequences in `public` were not enough. A staging
# refresh of a 2026.09-ga.5 backup measured Authentik's migration dying seven
# times at authentik_tasks.0008 with `must be owner of function
# _pgtrigger_should_ignore` while this step reported every DB correctly owned:
# a function. So the check and the reassign cover EVERY object a migration may
# have to alter, in every schema of the database: the schemas themselves,
# tables (also partitioned and foreign), views, materialized views, sequences,
# functions, procedures, aggregates, and enum/composite/domain/range types.
# Objects that belong to an extension (pgvector's functions …) and objects that
# are an internal part of another one (a range type's constructors) are left
# alone: they are not the service's, and ALTER on them is wrong or refused.
#
# `REASSIGN OWNED BY docker TO <role>` is REFUSED (docker is the bootstrap
# superuser — "objects required by the database system"), so each object is
# reassigned with a targeted ALTER … OWNER TO. Tables go FIRST so their
# row-types, indexes and owned sequences move with them; the later loops
# re-read the catalogue and only see what is left. Idempotent: a no-op on a
# healthy box (owner == role) and on a docker-superuser box (skipped: *_DB_USER
# empty → still connects as docker, which already owns everything).
#
# Called by the upgrade (Step 4c, and therefore --migrations-only) and by the
# full restore before the stack comes up (cli/backup.sh): a restore replays
# the owners the backed-up box had, mis-owned objects included.
# shellcheck disable=SC2016
RZFZ_OWNED_SCHEMAS_SQL='SELECT n.oid FROM pg_namespace n
  WHERE n.nspname NOT IN ('"'"'pg_catalog'"'"', '"'"'information_schema'"'"', '"'"'pg_toast'"'"')
    AND n.nspname NOT LIKE '"'"'pg\_temp\_%'"'"' AND n.nspname NOT LIKE '"'"'pg\_toast\_temp\_%'"'"'
    AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '"'"'pg_namespace'"'"'::regclass
                     AND d.objid = n.oid AND d.deptype = '"'"'e'"'"')'
# The "not an extension member / not an internal part of another object" test
# for an object of catalogue __CAT__ with oid __OID__.
RZFZ_FREE_OBJECT_SQL='NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '"'"'__CAT__'"'"'::regclass
                     AND d.objid = __OID__ AND d.deptype IN ('"'"'e'"'"', '"'"'i'"'"'))'

# The SQL that counts (mode=count) or reassigns (mode=reassign) every object of
# the connected database not owned by role $1. Prints the SQL.
service_db_ownership_sql() {
    local role="$1" mode="$2" ns free_c free_p free_t
    ns="$RZFZ_OWNED_SCHEMAS_SQL"
    # pg_class: extension members only — a partitioned table records an
    # internal dependency on its own partition-key column.
    free_c="${RZFZ_FREE_OBJECT_SQL//__CAT__/pg_class}";  free_c="${free_c//__OID__/c.oid}"
    free_c="${free_c//\'i\'/\'e\'}"
    free_p="${RZFZ_FREE_OBJECT_SQL//__CAT__/pg_proc}";   free_p="${free_p//__OID__/p.oid}"
    free_t="${RZFZ_FREE_OBJECT_SQL//__CAT__/pg_type}";   free_t="${free_t//__OID__/t.oid}"
    # relkind: r table, p partitioned, f foreign, v view, m matview, S sequence.
    # A sequence OWNED BY a column moves with its table and cannot be altered on
    # its own, so the sequence loop skips those (deptype 'a' / identity 'i').
    local rel_where="c.relnamespace IN (${ns}) AND c.relowner <> r.oid AND ${free_c}"
    local seq_linked="EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i'))"
    local proc_where="p.pronamespace IN (${ns}) AND p.proowner <> r.oid AND ${free_p}"
    # enum, domain, range and STANDALONE composite types (a table's row type
    # moves with the table; ALTER TYPE on it is refused).
    local type_where="t.typnamespace IN (${ns}) AND t.typowner <> r.oid AND ${free_t}
        AND (t.typtype IN ('e', 'd', 'r') OR (t.typtype = 'c' AND (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c'))"
    local schema_where="n.oid IN (${ns}) AND n.nspowner <> r.oid"
    if [ "$mode" = list ]; then
        # what the count counts, named (at most 20): "<kind> <name> (owner <role>)"
        cat <<SQL
SELECT kind || ' ' || name || ' (owner ' || owner || ')' FROM (
  SELECT 1 AS o, 'schema' AS kind, quote_ident(n.nspname) AS name, pg_get_userbyid(n.nspowner) AS owner
    FROM pg_namespace n, pg_roles r WHERE r.rolname = '${role}' AND ${schema_where}
  UNION ALL
  SELECT 2, CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence'
                           WHEN 'f' THEN 'foreign table' ELSE 'table' END, c.oid::regclass::text, pg_get_userbyid(c.relowner)
    FROM pg_class c, pg_roles r WHERE r.rolname = '${role}' AND ${rel_where}
     AND (c.relkind IN ('r','p','f','v','m') OR (c.relkind = 'S' AND NOT ${seq_linked}))
  UNION ALL
  SELECT 3, CASE p.prokind WHEN 'p' THEN 'procedure' WHEN 'a' THEN 'aggregate' ELSE 'function' END,
         p.oid::regprocedure::text, pg_get_userbyid(p.proowner)
    FROM pg_proc p, pg_roles r WHERE r.rolname = '${role}' AND ${proc_where}
  UNION ALL
  SELECT 4, 'type', t.oid::regtype::text, pg_get_userbyid(t.typowner)
    FROM pg_type t, pg_roles r WHERE r.rolname = '${role}' AND ${type_where}
) x ORDER BY o, name LIMIT 20;
SQL
        return 0
    fi
    if [ "$mode" = count ]; then
        cat <<SQL
SELECT (SELECT count(*) FROM pg_namespace n WHERE ${schema_where})
     + (SELECT count(*) FROM pg_class c WHERE c.relkind IN ('r','p','f','v','m') AND ${rel_where})
     + (SELECT count(*) FROM pg_class c WHERE c.relkind = 'S' AND ${rel_where} AND NOT ${seq_linked})
     + (SELECT count(*) FROM pg_proc p WHERE ${proc_where})
     + (SELECT count(*) FROM pg_type t WHERE ${type_where})
  FROM pg_roles r WHERE r.rolname = '${role}';
SQL
        return 0
    fi
    cat <<SQL
DO \$rzfz\$
DECLARE r record; o record;
BEGIN
  SELECT oid, rolname INTO r FROM pg_roles WHERE rolname = '${role}';
  IF NOT FOUND THEN RAISE EXCEPTION 'role % does not exist', '${role}'; END IF;
  FOR o IN SELECT n.nspname FROM pg_namespace n WHERE ${schema_where}
  LOOP EXECUTE format('ALTER SCHEMA %I OWNER TO %I', o.nspname, r.rolname); END LOOP;

  FOR o IN SELECT c.oid::regclass AS q, c.relkind FROM pg_class c
           WHERE c.relkind IN ('r','p','f') AND ${rel_where}
  LOOP EXECUTE format(CASE o.relkind WHEN 'f' THEN 'ALTER FOREIGN TABLE %s OWNER TO %I'
                                     ELSE 'ALTER TABLE %s OWNER TO %I' END, o.q, r.rolname); END LOOP;

  FOR o IN SELECT c.oid::regclass AS q, c.relkind FROM pg_class c
           WHERE c.relkind IN ('v','m') AND ${rel_where}
  LOOP EXECUTE format(CASE o.relkind WHEN 'm' THEN 'ALTER MATERIALIZED VIEW %s OWNER TO %I'
                                     ELSE 'ALTER VIEW %s OWNER TO %I' END, o.q, r.rolname); END LOOP;

  FOR o IN SELECT c.oid::regclass AS q FROM pg_class c
           WHERE c.relkind = 'S' AND ${rel_where} AND NOT ${seq_linked}
  LOOP EXECUTE format('ALTER SEQUENCE %s OWNER TO %I', o.q, r.rolname); END LOOP;

  FOR o IN SELECT p.oid::regprocedure AS q FROM pg_proc p WHERE ${proc_where}
  LOOP EXECUTE format('ALTER ROUTINE %s OWNER TO %I', o.q, r.rolname); END LOOP;

  FOR o IN SELECT t.oid::regtype AS q, t.typtype FROM pg_type t WHERE ${type_where}
  LOOP EXECUTE format(CASE o.typtype WHEN 'd' THEN 'ALTER DOMAIN %s OWNER TO %I'
                                     ELSE 'ALTER TYPE %s OWNER TO %I' END, o.q, r.rolname); END LOOP;
END \$rzfz\$;
SQL
}

# _reown_service_db DBNAME ROLE — count, name and reassign every object of
# DBNAME not owned by ROLE (the body of reconcile_service_db_ownership's loop,
# shared with cognee's per-dataset databases, #3292). Needs superuser /
# superpw from the caller. Sets _rzfz_reown_rc: 0 nothing to do, 1 failed,
# 2 reassigned.
_reown_service_db() {
    local dbname="$1" role="$2" misowned named sqlf
    _rzfz_reown_rc=0
    misowned=$(PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d "$dbname" -tAc \
        "$(service_db_ownership_sql "$role" count)" 2>/dev/null | tr -d '[:space:]')
    if ! printf '%s' "$misowned" | grep -qE '^[0-9]+$'; then
        # #2524: an unreadable count is not "all owned".
        print_warning "  ${dbname}: could not read the object owners — ownership NOT verified."
        _rzfz_reown_rc=1
        return 0
    fi
    if [ "$misowned" = "0" ]; then
        print_substep "  ${dbname}: every object already owned by ${role} — ok."
        return 0
    fi
    # Name them: "N mis-owned" alone said nothing about WHICH object a
    # migration was about to trip over.
    named=$(PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d "$dbname" -tAc \
        "$(service_db_ownership_sql "$role" list)" 2>/dev/null | sed '/^$/d' | paste -sd ';' - | sed 's/;/; /g')
    print_substep "  ${dbname}: ${misowned} object(s) not owned by ${role}: ${named:-?}$([ "$misowned" -gt 20 ] && echo " …")"
    if [ "$DRY_RUN" = "true" ]; then
        print_info "  Would reassign ${misowned} mis-owned object(s) in ${dbname} → ${role}"
        return 0
    fi

    sqlf=$(mktemp)
    service_db_ownership_sql "$role" reassign > "$sqlf"
    docker cp "$sqlf" postgres:/tmp/rzfz-reassign-owner.sql >/dev/null 2>&1
    if PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres \
          psql -U "$superuser" -h 127.0.0.1 -d "$dbname" -v ON_ERROR_STOP=1 \
          -f /tmp/rzfz-reassign-owner.sql >/dev/null 2>&1; then
        print_substep "  Reassigned ${dbname} → ${role} (${misowned} object(s) were mis-owned)."
        _rzfz_reown_rc=2
    else
        print_warning "  Ownership reassign FAILED for ${dbname} → ${role} (left as-is; investigate)."
        _rzfz_reown_rc=1
    fi
    docker exec postgres rm -f /tmp/rzfz-reassign-owner.sql >/dev/null 2>&1 || true
    rm -f "$sqlf"
}

reconcile_service_db_ownership() {
    print_step "Reconciling per-service DB object ownership..."

    if [ ! -f "config/.env.example" ]; then
        print_substep "No .env.example template — skipping DB-ownership reconcile."
        return 0
    fi
    command -v docker >/dev/null 2>&1 || { print_substep "docker unavailable — skipping."; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x postgres >/dev/null || {
        print_substep "postgres container not running — skipping DB-ownership reconcile."; return 0; }

    local superuser superpw
    superuser=$(read_env_value ".env" "POSTGRES_USER"); [ -n "$superuser" ] || superuser="docker"
    superpw=$(read_env_value ".env" "POSTGRES_PASSWORD")

    # One login probe first: the per-DB lookups below read "no row" and "could
    # not log in" alike, so a wrong superuser password used to skip every DB
    # and report "nothing to do".
    local probe
    probe=$(PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d postgres \
        -tAc "SELECT 1" 2>/dev/null | tr -d '[:space:]') || probe=""
    if [ "$probe" != "1" ]; then
        print_warning "DB-ownership reconcile NOT run: cannot log in to postgres as ${superuser} with the .env's POSTGRES_PASSWORD — no service DB was checked."
        return 1
    fi

    local reassigned=0 checked=0 failed=0
    local userkey dbkey role dbname
    while IFS= read -r userkey; do
        [ -n "$userkey" ] || continue
        role=$(read_env_value ".env" "$userkey")
        [ -n "$role" ] || continue                       # docker-superuser box → skip
        [ "$role" = "$superuser" ] && continue           # never "reassign to" the superuser
        printf '%s' "$role" | grep -qE '^[a-zA-Z_][a-zA-Z0-9_]*$' || continue

        dbkey="${userkey%_USER}"                          # ONYX_DB_USER -> ONYX_DB
        # #2401 (0.208 repair, step 3): the Dify plugin DB is named by
        # DB_PLUGIN_DATABASE, not by a DIFY_PLUGIN_DB key — without this map the
        # reconcile silently skipped dify_plugin_db (13 tables left superuser-owned).
        [ "$userkey" = "DIFY_PLUGIN_DB_USER" ] && dbkey="DB_PLUGIN_DATABASE"
        dbname=$(read_env_value ".env" "$dbkey")
        [ -n "$dbname" ] || dbname=$(read_env_value "config/.env.example" "$dbkey")
        [ -n "$dbname" ] || continue
        printf '%s' "$dbname" | grep -qE '^[a-zA-Z_][a-zA-Z0-9_]*$' || continue

        # DB + role must exist, else the ALTERs error. Connect to the maintenance
        # `postgres` DB explicitly — without -d, psql targets a DB named after the
        # superuser (e.g. "docker"), which does not exist → the check would always
        # fail and the function would silently skip every service.
        PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d postgres \
            -tAc "SELECT 1 FROM pg_database WHERE datname='${dbname}'" 2>/dev/null | grep -q 1 || continue
        PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d postgres \
            -tAc "SELECT 1 FROM pg_roles WHERE rolname='${role}'" 2>/dev/null | grep -q 1 || continue
        ((checked++)) || true

        _reown_service_db "$dbname" "$role"
        case "$_rzfz_reown_rc" in 1) ((failed++)) || true ;; 2) ((reassigned++)) || true ;; esac
    done < <(grep -oE '^[A-Z0-9_]+_DB_USER=' "config/.env.example" | sed 's/=$//')

    # #3292 (H-01): cognee creates one database per dataset (its pgvector
    # handler, access control on) and records each in cognee_db's
    # dataset_database table. Those created while cognee connected as the
    # superuser are the superuser's: hand the database AND every object in it
    # to cognee_user, or the next cognify / dataset delete fails on a
    # permission error. Databases cognee_user creates are its own already.
    local cg_role cg_db ds ds_owner ds_list
    cg_role=$(read_env_value ".env" "COGNEE_DB_USER")
    cg_db=$(read_env_value ".env" "COGNEE_DB"); [ -n "$cg_db" ] || cg_db=$(read_env_value "config/.env.example" "COGNEE_DB")
    if [ -n "$cg_role" ] && [ "$cg_role" != "$superuser" ] \
       && printf '%s' "$cg_role" | grep -qE '^[a-zA-Z_][a-zA-Z0-9_]*$' \
       && printf '%s' "$cg_db" | grep -qE '^[a-zA-Z_][a-zA-Z0-9_]*$' \
       && PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d postgres \
            -tAc "SELECT 1 FROM pg_roles WHERE rolname='${cg_role}'" 2>/dev/null | grep -q 1 \
       && PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d postgres \
            -tAc "SELECT 1 FROM pg_database WHERE datname='${cg_db}'" 2>/dev/null | grep -q 1; then
        # No table yet (cognee never started, or access control off) = no datasets.
        ds_list=$(PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d "$cg_db" -tAc \
            "SELECT DISTINCT d.vector_database_name FROM dataset_database d JOIN pg_database p ON p.datname = d.vector_database_name WHERE d.vector_dataset_database_handler = 'pgvector' AND p.datname <> '${cg_db}' ORDER BY 1" 2>/dev/null) || ds_list=""
        while IFS= read -r ds; do
            [ -n "$ds" ] || continue
            # cognee names them by dataset UUID; anything else is not touched.
            printf '%s' "$ds" | grep -qE '^[0-9a-fA-F-]{32,36}$' || { print_warning "  cognee dataset database '${ds}': unexpected name — not re-owned."; ((failed++)) || true; continue; }
            ((checked++)) || true
            ds_owner=$(PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d postgres -tAc \
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='${ds}'" 2>/dev/null | tr -d '[:space:]')
            if [ "$ds_owner" != "$cg_role" ]; then
                print_substep "  cognee dataset database ${ds}: owned by ${ds_owner:-?}, not ${cg_role}."
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would hand database ${ds} to ${cg_role}"
                elif PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres psql -U "$superuser" -h 127.0.0.1 -d postgres \
                        -v ON_ERROR_STOP=1 -tAc "ALTER DATABASE \"${ds}\" OWNER TO \"${cg_role}\"" >/dev/null 2>&1; then
                    ((reassigned++)) || true
                else
                    print_warning "  Could not hand database ${ds} to ${cg_role} (left as-is; investigate)."
                    ((failed++)) || true
                    continue
                fi
            fi
            _reown_service_db "$ds" "$cg_role"
            case "$_rzfz_reown_rc" in 1) ((failed++)) || true ;; 2) ((reassigned++)) || true ;; esac
        done <<< "$ds_list"
    fi

    if [ "$DRY_RUN" != "true" ]; then
        if [ "$reassigned" -gt 0 ]; then
            print_success "DB-ownership reconcile: ${reassigned} database(s) reassigned to their service role."
        elif [ "$failed" -eq 0 ]; then
            print_substep "DB-ownership reconcile: nothing to do (${checked} service DB(s) already correctly owned)."
        fi
    fi
    [ "$failed" -eq 0 ]
}

# SCRAM-VERIFIER-BEGIN (kept byte-identical in scripts/lib.sh and core/init-db.sh; a test pins it)
# scram_sha256_verifier VARNAME — prints the PostgreSQL SCRAM-SHA-256 verifier
# (SCRAM-SHA-256$4096:<salt>$<StoredKey>:<ServerKey>) of the password held in
# the environment variable NAMED by $1. The value is read from the environment
# only: it never reaches argv, a file or a SQL statement (#2746). bash +
# openssl + od + base64, because the postgres image has no python3. The
# PBKDF2 step is `openssl enc -pbkdf2 -P` (password via -pass env:), whose
# first 32 key bytes ARE PBKDF2-HMAC-SHA256 with dkLen 32; HMAC is built from
# two SHA-256 runs fed on stdin, so the derived key never reaches argv either.
scram_sha256_verifier() {
    local var=$1 salt_hex salted ck sk i b ipad opad hx
    salt_hex=$(openssl rand -hex 8) || return 1
    salted=$(SCRAM_PW="${!var}" openssl enc -aes-256-cbc -pbkdf2 -iter 4096 -md sha256 \
        -S "$salt_hex" -pass env:SCRAM_PW -P 2>/dev/null | sed -n 's/^key *= *//p')
    [ "${#salted}" -eq 64 ] || return 1
    ipad=""; opad=""
    for ((i = 0; i < 64; i++)); do
        if [ "$i" -lt 32 ]; then b=$((16#${salted:$((i * 2)):2})); else b=0; fi
        printf -v hx '\\x%02x' $((b ^ 0x36)); ipad+=$hx
        printf -v hx '\\x%02x' $((b ^ 0x5c)); opad+=$hx
    done
    _scram_hmac() {
        { printf "$ipad"; printf '%s' "$1"; } | openssl dgst -sha256 -binary \
            | { printf "$opad"; cat; } | openssl dgst -sha256 -binary
    }
    ck=$(_scram_hmac "Client Key" | od -An -v -tx1 | tr -d ' \n') || return 1
    sk=$(_scram_hmac "Server Key" | base64 -w0) || return 1
    printf 'SCRAM-SHA-256$4096:%s$%s:%s\n' \
        "$(printf '%s' "$salt_hex" | sed 's/../\\x&/g' | { read -r hx; printf "$hx"; } | base64 -w0)" \
        "$(printf '%s' "$ck" | sed 's/../\\x&/g' | { read -r hx; printf "$hx"; } | openssl dgst -sha256 -binary | base64 -w0)" \
        "$sk"
    unset -f _scram_hmac
}
# SCRAM-VERIFIER-END

# ── Vaultwarden mail host (#3044) ────────────────────────────────────────────
# Vaultwarden mails only when SMTP_HOST (= VAULTWARDEN_SMTP_HOST) is set, and
# with mail on it REFUSES to set a first master password when the mail cannot
# be sent: "SMTP error: Connection error: failed to lookup address
# information". So a host that does not resolve inside the container, or the
# internal relay not running, locks every new user out of the vault.
#
# vaultwarden_smtp_verdict <host> — prints ONE line "STATE|message":
#   off   mail is off (empty host): nothing to check
#   pass  the host resolves inside the vaultwarden container (and the relay runs)
#   fail  it does not, or smtp-relay is down — the message names the fix
#   skip  cannot be checked (no docker, vaultwarden not running, no getent)
# The caller decides that the vaultwarden profile is enabled.
# VAULTWARDEN_SMTP_VERDICT-BEGIN
vaultwarden_smtp_verdict() {
    local host="${1:-}" names rc=0
    local consequence="new users cannot set their master password (Vaultwarden refuses it while mail cannot be sent)"
    local fix="Fix: save the Config Portal SMTP page (Settings → SMTP), or clear VAULTWARDEN_SMTP_HOST in .env and recreate vaultwarden (docker compose up -d --no-deps vaultwarden)"
    if [ -z "$host" ]; then
        echo "off|Vaultwarden mail is off (VAULTWARDEN_SMTP_HOST empty) — no invitation or verification mails; the Config Portal SMTP page turns it on"
        return 0
    fi
    command -v docker >/dev/null 2>&1 || { echo "skip|Vaultwarden mail host '${host}': docker CLI not available"; return 0; }
    names=$(docker ps --format '{{.Names}}' 2>/dev/null) || names=""
    if ! printf '%s\n' "$names" | grep -qx vaultwarden; then
        echo "skip|Vaultwarden mail host '${host}': vaultwarden is not running — not checked"
        return 0
    fi
    if [ "$host" = "smtp-relay" ] && ! printf '%s\n' "$names" | grep -qx smtp-relay; then
        echo "fail|Vaultwarden mails through smtp-relay, but the smtp-relay container is not running — ${consequence}. Start it (docker compose up -d smtp-relay). ${fix}"
        return 0
    fi
    timeout 20 docker exec vaultwarden getent hosts "$host" >/dev/null 2>&1 || rc=$?
    case "$rc" in
        0)       echo "pass|Vaultwarden mail host '${host}' resolves inside the vaultwarden container" ;;
        124)     echo "skip|Vaultwarden mail host '${host}': the lookup inside the vaultwarden container timed out — not checked" ;;
        126|127) echo "skip|Vaultwarden mail host '${host}': getent not available in the vaultwarden container — not checked" ;;
        *)       echo "fail|Vaultwarden mail host '${host}' does not resolve inside the vaultwarden container — ${consequence}. ${fix}" ;;
    esac
    return 0
}
# VAULTWARDEN_SMTP_VERDICT-END
