# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# shellcheck shell=bash
# ==============================================================================
# scripts/lib.sh — shared bash library for razzfazz-*.sh management scripts
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
read_env_value() {
    local file=$1
    local key=$2
    [[ -f "$file" ]] || return 0
    local raw
    raw=$(grep -E "^${key}=" "$file" 2>/dev/null | head -n1 | cut -d'=' -f2- || true)
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

# update_env_value: idempotently set $key=$value in $file. If the key exists,
# its line is replaced; otherwise the line is appended. Returns 1 if the file
# does not exist (caller decides whether to create it).
#
# The sed substitution uses `|` as the delimiter and pre-escapes `\`, `&`,
# and `|` in the value so values containing `/` (paths, URLs) work correctly.
update_env_value() {
    local file=$1
    local key=$2
    local value=$3

    if [[ ! -f "$file" ]]; then
        return 1
    fi

    # Escape characters that are special to sed's replacement side when using
    # `|` as the delimiter: backslash, ampersand, and the delimiter itself.
    local escaped_value
    escaped_value=$(printf '%s' "$value" | sed -e 's/[\&|]/\\&/g')

    if grep -qE "^${key}=" "$file"; then
        # INODE-PRESERVING in-place edit. `sed -i` writes a NEW file and renames it
        # over the target → new inode. The razzfazz-config container bind-mounts
        # .env / .env.dify as single FILES (inode-based); a new inode makes that
        # mount go stale → /stack/.env becomes read-only in the container → the
        # Configuration Portal can no longer toggle modules ("no write permission
        # to /stack/.env"). `cat tmp > file` truncates the SAME inode, so the mount
        # stays valid. See project_config_ui_env_write_broken.
        local _t="${file}.tmp.$$"
        if sed "s|^${key}=.*|${key}=${escaped_value}|" "$file" > "$_t"; then
            cat "$_t" > "$file"
        fi
        rm -f "$_t"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
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

generate_password() {
    local length=$1
    # Drop characters that cause shell/quoting trouble (/ + =), then truncate.
    # We over-generate by asking for `length` raw bytes (base64 inflates ~1.33x)
    # so there's enough material left after stripping to satisfy `head -c`.
    openssl rand -base64 "$length" | tr -d '/+=' | head -c "$length"
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
stop_orphan_gpustack_runners() {
    local orphans
    orphans=$(docker ps -a \
        --filter 'ancestor=llama-vulkan-runner' \
        --filter 'ancestor=llama-rocm-runner' \
        --filter 'ancestor=llama-cpu-runner' \
        --filter 'ancestor=gpustack/runtime:pause' \
        --format '{{.ID}} {{.Names}} {{.Label "com.docker.compose.project"}}' 2>/dev/null \
        | awk '$3 != "razzfazz-stack" && $3 != "razzfazz_stack" {print $1, $2}')
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

# razzfazz_is_offline [envfile] — return 0 when the box is in offline mode.
razzfazz_is_offline() {
    [ "$(razzfazz_network_mode "$@")" = "offline" ]
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
        if ! python3 "$gen" --services "$svc_csv" --out "$out" 2>/dev/null; then
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
ensure_oidc_ca_superset() {
    print_step "Ensuring certs/caddy-ca.pem is an OIDC CA superset (system CA bundle + Caddy internal CA) (#152)..."
    mkdir -p certs 2>/dev/null || true

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
    [ -n "$_appended" ] || _appended="no"

    print_substep "certs/caddy-ca.pem: base = ${_base_n} system root(s); appended = ${_appended} (TLS_MODE='${_tls_mode:-letsencrypt}')."

    # 3) Restart the RUNNING OIDC-client containers so they reload the trust store
    #    (they mount the file read-only; the bind reflects the new content, but the
    #    process must restart to re-read its CA bundle). Best-effort, per-container.
    local _c
    for _c in openwebui gitea vaultwarden; do
        if command -v docker >/dev/null 2>&1 \
           && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$_c"; then
            docker restart "$_c" >/dev/null 2>&1 \
                && print_substep "Restarted $_c to reload the OIDC trust store." \
                || print_warning "Could not restart $_c — restart it manually so OIDC discovery trusts the new CA bundle."
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
