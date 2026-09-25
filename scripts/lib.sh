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
# advice the operator has already followed (#1570 review, agent-rzfz).
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
        if sed -E "s|^([[:space:]]*(export[[:space:]]+)?)${key}=.*|\\1${key}=${escaped_value}|" "$file" > "$_t"; then
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

# razzfazz_stage_package_models SRC_DIR — copy SRC_DIR/. (a package's models/
# subtree) into the reachable runtime's models volume. Returns 0 on success,
# 1 when nothing could be staged; every outcome is printed, none is silent.
razzfazz_stage_package_models() {
    local src="$1" rt
    rt=$(razzfazz_models_runtime)
    case "$rt" in
        gpustack)
            print_substep "Loading bundled model GGUFs into the gpustack-data volume (${RAZZFAZZ_LOCAL_MODELS_DIR})..."
            if docker exec gpustack mkdir -p "$RAZZFAZZ_LOCAL_MODELS_DIR" >/dev/null 2>&1 \
               && docker cp "${src}/." "gpustack:${RAZZFAZZ_LOCAL_MODELS_DIR}/" >/dev/null 2>&1; then
                print_substep "Model GGUFs loaded into ${RAZZFAZZ_LOCAL_MODELS_DIR}/ — offline deploy will register them via source=local_path."
                return 0
            fi
            print_warning "Could not copy bundled model GGUFs into the gpustack-data volume — offline model deploy may report missing GGUFs. Re-check with 'rzfz verify-models'."
            return 1 ;;
        manager)
            print_substep "Loading bundled model GGUFs into the LLM Manager's models volume (${RAZZFAZZ_MANAGER_MODELS_CONTAINER}:${RAZZFAZZ_MANAGER_MODELS_DIR}, flat by name)... (#2227)"
            local f base n=0 failed=0 seen=""
            while IFS= read -r f; do
                base=$(basename "$f")
                case " $seen " in *" $base "*)
                    print_warning "  ${base}: a second file with this name is in the package (${f#"$src"/}) — the flat manager volume holds one; keeping the first."
                    continue ;;
                esac
                seen="$seen $base"
                if docker cp "$f" "${RAZZFAZZ_MANAGER_MODELS_CONTAINER}:${RAZZFAZZ_MANAGER_MODELS_DIR}/${base}" >/dev/null 2>&1; then
                    n=$((n + 1))
                else
                    failed=$((failed + 1))
                    print_warning "  could not copy ${f#"$src"/} into the manager volume."
                fi
            done < <(find "$src" -type f -name '*.gguf' | sort)
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

ensure_oidc_ca_superset() {
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

    # 3) Restart the RUNNING OIDC-client containers so they reload the trust store
    #    (they mount the file read-only; the bind reflects the new content, but the
    #    process must restart to re-read its CA bundle). Best-effort, per-container.
    #    #855: wazuh-indexer and wazuh-dashboard join the list because they mount
    #    this same bundle — the indexer's openid_auth_domain reads it as
    #    pemtrustedcas_filepath and the dashboard as
    #    opensearch_security.openid.root_ca, both to verify Authentik. Each entry
    #    is a no-op on a box where that container is not running, so an opt-in
    #    profile costs nothing here. The #631 guard asserts this list equals the
    #    set of caddy-ca.pem mount consumers across every compose file.
    #    #2004: openuem-worker-notification joins for the same reason in a
    #    different protocol — it mounts this bundle as SSL_CERT_FILE so go-mail
    #    can verify the relay's STARTTLS certificate, and Go reads the file once
    #    at first use. Without the restart a box that just gained the relay CA
    #    keeps failing x509 until something else recreates the container.
    local _c
    for _c in openwebui gitea vaultwarden wazuh-indexer wazuh-dashboard openuem-worker-notification; do
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

# ── Security-assessment selection (#423) ──────────────────────────────────────
# Pick the assessment that describes the code this box is RUNNING.
#
# `ls -1t` cannot do this: every assessment arrives in one checkout/package
# apply and therefore shares a single mtime, so the "newest" is an arbitrary
# tie-break. On box 0.236 that surfaced an April assessment for an August
# install, and `rzfz security-check` embeds the result in customer-facing output.
#
# Usage: razzfazz_select_assessment <installed_tag|""> <dir> [dir...]
# Prints "<path>\t<exact|fallback>" and returns 0, or returns 1 when no
# assessment exists at all. `exact` means the filename tail matches the
# installed tag; `fallback` means the newest by CalVer-sorted FILENAME and the
# caller MUST tell the operator its findings describe different code.
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
# `rzfz status` is happy. Box 0.78 sat like that for four days; culturehack-001
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
        # and was unreachable — agent-seqis measured it (#1677):
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
# Measured on Profida 2026-08-20 02:00:05 CEST: 29 image deletes in one burst.
# Nothing in this repo ever set the flag — we create the server through
# KOMODO_FIRST_SERVER and inherit Komodo's own default. Disk pressure is already
# handled deliberately elsewhere (backup retention, ClickHouse TTL #292, the disk
# guard); a blind nightly prune is not something we want to ship.

# POST a JSON body to Komodo. The body goes over STDIN on purpose: `curl -d
# '{"password":...}'` would put the admin password in argv, where ps shows it to
# every local user.
_razzfazz_komodo_post() {
    local path="$1" body="$2" jwt="${3:-}"
    if [ -n "$jwt" ]; then
        printf '%s' "$body" | curl -s -X POST "${KOMODO_BASE_URL}${path}" \
            -H 'Content-Type: application/json' \
            -H "Authorization: Bearer ${jwt}" --data @-
    else
        printf '%s' "$body" | curl -s -X POST "${KOMODO_BASE_URL}${path}" \
            -H 'Content-Type: application/json' --data @-
    fi
}

# Turn auto_prune off on every server Komodo knows about. Returns non-zero if the
# login fails, if no server comes back, or if any server still reports the flag
# set after the write — a prune that keeps running while provisioning claims it is
# off is worse than a loud failure.
razzfazz_komodo_disable_auto_prune() {
    local KOMODO_BASE_URL user pass login_body jwt ids id now rc=0
    KOMODO_BASE_URL="${KOMODO_URL:-http://127.0.0.1:${KOMODO_PORT:-8180}}"
    user="${KOMODO_INIT_ADMIN_USERNAME:-admin}"
    pass="${KOMODO_INIT_ADMIN_PASSWORD:-}"

    if [ -z "$pass" ]; then
        echo "komodo: KOMODO_INIT_ADMIN_PASSWORD is empty — cannot disable auto_prune" >&2
        return 1
    fi

    # Built in python so a password containing quotes or backslashes is escaped
    # correctly, and read from the environment so it never lands in argv.
    login_body="$(RZFZ_K_USER="$user" RZFZ_K_PASS="$pass" python3 -c 'import json, os
print(json.dumps({"username": os.environ["RZFZ_K_USER"],
                  "password": os.environ["RZFZ_K_PASS"]}))')" || return 1

    # NOTE the path. A POST to plain /auth returns 405 (allow: GET,HEAD) — that is
    # the SPA fallback route, not the API.
    jwt="$(_razzfazz_komodo_post /auth/login/LoginLocalUser "$login_body" | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
d = d if isinstance(d, dict) else {}
print((d.get("data") or {}).get("jwt") or d.get("jwt") or "")' 2>/dev/null)"

    if [ -z "$jwt" ]; then
        echo "komodo: local-auth login failed — auto_prune left unchanged" >&2
        return 1
    fi

    ids="$(_razzfazz_komodo_post /read/ListServers '{}' "$jwt" | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = []
for s in (d or []):
    if isinstance(s, dict) and s.get("id"):
        print(s["id"])' 2>/dev/null)"

    if [ -z "$ids" ]; then
        echo "komodo: no servers returned — cannot confirm auto_prune is off" >&2
        return 1
    fi

    for id in $ids; do
        _razzfazz_komodo_post /write/UpdateServer \
            "{\"id\":\"${id}\",\"config\":{\"auto_prune\":false}}" "$jwt" >/dev/null
        # Re-read: the write is only believed once the server says so.
        now="$(_razzfazz_komodo_post /read/GetServer "{\"server\":\"${id}\"}" "$jwt" | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(json.dumps((d.get("config") or {}).get("auto_prune")))' 2>/dev/null)"
        if [ "$now" = "false" ]; then
            printf '  komodo server %s: auto_prune=false\n' "$id"
        else
            printf '  komodo server %s: auto_prune is still %s\n' "$id" "${now:-unknown}" >&2
            rc=1
        fi
    done
    return "$rc"
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
# custom images of PROFILES (default: all). Args: [profiles] [source=built|package]
# Args: [profiles] [source=built|package] [package-manifest]. With source=package
# and a manifest, only refs present under the PACKAGE's image id are recorded
# (#2168): the record then says what the stick put there, replacing whatever a
# previous install on this box left in the file.
razzfazz_record_custom_image_builds() {
    local profiles="${1:-}" source="${2:-built}" manifest="${3:-}" py out n
    py="$(razzfazz_image_provenance_py)"
    if [ ! -f "$py" ]; then
        print_warning "  Build record NOT written (${py} missing) — the next init cannot tell these images from adopted ones (#2006)."
        return 0
    fi
    if out="$(python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --record ${profiles:+--profiles "$profiles"} --source "$source" ${manifest:+--package-manifest "$manifest"} 2>/dev/null)"; then
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

razzfazz_custom_image_verdicts() {
    local py; py="$(razzfazz_image_provenance_py)"
    [ -f "$py" ] || return 0
    python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --verdicts --profiles "$1" ${2:+--package-manifest "$2"} 2>/dev/null || true
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
    # #2287: a tag wins even when this box has never fetched it. The two checks
    # above only see LOCAL tags, and a box upgrading to a new release never
    # holds the new tag yet — which made the cold-tag case (the customer case)
    # the one that fell through. Ask the remote, by EXACT refpath.
    git ls-remote --exit-code --tags origin "refs/tags/${b}" >/dev/null 2>&1 && return 1
    # #2287: match the head by EXACT refpath too. `git ls-remote <pattern>`
    # globs on the ref TAIL, so the bare name 2026.09-rc12 matched our own
    # refs/heads/release/2026.09-rc12 and turned the TAG into a "branch".
    if git rev-parse -q --verify "refs/remotes/origin/${b}" >/dev/null 2>&1 \
       || git ls-remote --exit-code --heads origin "refs/heads/${b}" >/dev/null 2>&1; then
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
    git fetch origin "${b}" >/dev/null 2>&1 || return 1
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
# #707 — guarded orphan sweep for the ephemeral-test-postgres projects
# ------------------------------------------------------------------------------
# `cli/test.sh` reaps leaked `pytest-eph-pg-*` projects at the start of every
# run (a SIGKILL'd pytest leaves its postgres behind; cumulative leaks caused
# the 2026-05-16 OOM cascade). The bare version of that sweep -- `docker ps -a`
# + `docker rm -f` on everything matching the prefix -- also deleted the LIVE
# session databases of any job already inside pytest on the same runner
# (CI 1836: containers gone from the daemon, `no such object`, no exit state).
#
# The wrapper runs BEFORE pytest, so it cannot use the conftest's run marker
# (PYTEST_XDIST_TESTRUNUID does not exist yet). State + age is the equivalent
# guarantee, and deliberately the SAME age policy as the conftest sweep:
#
#   not running        -> reapable (nothing depends on a stopped container)
#   running + older    -> orphan (its session is long gone)
#   running + younger  -> LEAVE ALONE (a live run, possibly another job's)
#   age unreadable     -> treated as young, i.e. left alone while running
RAZZFAZZ_TEST_ORPHAN_PREFIX="pytest-eph-pg-"

razzfazz_reapable_test_containers() {
    # stdin: `<name>\t<state>\t<created_epoch>` per line; stdout: reapable names.
    # $1 = "now" epoch (defaults to the current time; a parameter so the policy
    # is unit-testable without sleeping).
    local now="${1:-$(date +%s)}"
    local min_age="${RAZZFAZZ_TEST_ORPHAN_MIN_AGE_S:-600}"
    local name state created
    while IFS=$'\t' read -r name state created; do
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
            exited|dead) ;;   # terminal: dead is dead, reap at any age
            *)
                # unreadable timestamp -> fail safe, treat as young
                [ -n "$created" ] && [ "$created" -ge 0 ] 2>/dev/null || continue
                [ $(( now - created )) -ge "$min_age" ] || continue
                ;;
        esac
        printf '%s\n' "$name"
    done
}

razzfazz_sweep_orphan_test_containers() {
    command -v docker >/dev/null 2>&1 || return 0
    local rows names
    rows=$(docker ps -a --filter "name=^${RAZZFAZZ_TEST_ORPHAN_PREFIX}" \
        --format '{{.Names}}	{{.State}}	{{.CreatedAt}}' 2>/dev/null || true)
    [ -n "$rows" ] || return 0
    # CreatedAt is '2026-08-25 10:53:10 +0000 UTC' -> epoch (empty on failure,
    # which the filter treats as "young" for a running container).
    rows=$(printf '%s\n' "$rows" | awk -F'\t' 'BEGIN{OFS="\t"}
        { cmd = "date -d \"" $3 "\" +%s 2>/dev/null"; cmd | getline ep; close(cmd);
          print $1, $2, ep; ep="" }')
    names=$(printf '%s\n' "$rows" | razzfazz_reapable_test_containers)
    [ -n "$names" ] || return 0
    echo "razzfazz-test: sweeping $(printf '%s\n' "$names" | wc -l) orphan ${RAZZFAZZ_TEST_ORPHAN_PREFIX}* container(s) (stopped, or running longer than ${RAZZFAZZ_TEST_ORPHAN_MIN_AGE_S:-600}s)" >&2
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

# #693 — build-phase RAM safety (the 0.208 2026-08-24 global-OOM class)
# ------------------------------------------------------------------------------
# One BuildKit trace building EVERY custom image in parallel (vulkan cmake/
# glslc, moltis rustc x5, hermes, dify-web, licenses) next to the running
# full stack pushed a 30-GB box into a global OOM freeze. Two client-side
# levers, shared by upgrade / rollback / init build sites:
#
#   razzfazz_build_parallelism  -> how many compose build jobs may run at
#       once: min(nproc, MemAvailable/4GB), floor 1. Exported as
#       COMPOSE_PARALLEL_LIMIT (compose v2 honours it for parallel ops,
#       build included). Big boxes keep nproc; small boxes serialize.
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
    local kb
    kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null)
    [ -n "$kb" ] || { echo ""; return 0; }
    echo $(( kb / 1024 / 1024 ))
}

razzfazz_build_parallelism() {
    local override cores mem_gb by_mem jobs
    override=$(grep -m1 '^RAZZFAZZ_BUILD_PARALLELISM=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    override="${RAZZFAZZ_BUILD_PARALLELISM:-$override}"
    if [ -n "$override" ] && [ "$override" -ge 1 ] 2>/dev/null; then
        echo "$override"; return 0
    fi
    cores=$(nproc 2>/dev/null || echo 4)
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
# Targets are resolved LEXICALLY (`realpath -ms`), never by following what
# happens to exist on this box: the answer must not depend on whether some
# directory in the path is itself a link today. A chain (a -> b -> /etc) is
# still caught, because every link in the tree is examined on its own.
razzfazz_find_escaping_links() {
    local root="$1"
    [ -n "$root" ] && [ -d "$root" ] || return 2

    local root_norm
    root_norm=$(realpath -ms -- "$root" 2>/dev/null) || return 2

    local found=1 link target resolved
    # `find "$root"` so every printed path carries the same prefix that
    # root_norm was normalised from — comparing two differently-rooted spellings
    # of the same tree is how this kind of check gets a false verdict.
    while IFS= read -r link; do
        [ -n "$link" ] || continue
        target=$(readlink -- "$link" 2>/dev/null) || continue
        case "$target" in
            /*) resolved=$(realpath -ms -- "$target" 2>/dev/null) ;;
            *)  resolved=$(realpath -ms -- "$(dirname -- "$link")/$target" 2>/dev/null) ;;
        esac
        [ -n "$resolved" ] || resolved="$target"
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
#   * The PRIVATE key lives in the SEQIS secrets vault. It is never generated
#     by this code, never written by it, and never travels in a package. The
#     signing half (cli/package.sh --sign-key) takes a path to it and nothing
#     more; a key that reaches this repo is a key that must be rotated.
#   * The PUBLIC key is distributed by the FLEET INSTALL CHANNEL to
#     $RAZZFAZZ_PACKAGE_PUBKEY, else /etc/razzfazz/package-keys/razzfazz-packages.pem
#     ($RAZZFAZZ_PACKAGE_PUBKEY_PATH overrides that default). That path is
#     deliberately OUTSIDE the stack checkout: an upgrade package extracts over
#     the checkout, so an in-tree trust anchor would be replaceable by the very
#     artefact it is supposed to judge — this upgrade's package choosing the
#     next upgrade's key.
#
# STILL NOT DECIDED HERE:
#   * Whether existing unsigned packages are eventually REFUSED or only warned
#     about, and for how long (a flip is a fleet-wide flag day, see above).
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

# ------------------------------------------------------------------------------
# razzfazz_sign_package <archive> <private-key> [signature-path]
#
# #781, the PRODUCING half of the check above. Writes a detached
# `openssl dgst -sha256 -sign` signature BESIDE the archive (default
# `<archive>.openssl.sig`) — never into it. Anything inside the tarball is
# something the tarball can bring along, which is the entire reason
# MANIFEST.sha256 cannot answer "is this stick genuine".
#
# NO KEY IS EVER CREATED HERE. The private key lives in the SEQIS secrets vault
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
        echo "      The key lives in the SEQIS secrets vault; none is generated here." >&2
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
#     <OK|FAIL|STOPPED|PENDING>|<name>|<exit-code>|<docker state>|<last log line>
#
#   OK      — terminal (`exited`/`dead`) with exit code 0. It did its job.
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
    local proj names name insp state code oom rc=0 ps_rc=0 errf why
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
                    printf 'OK|%s|%s|%s|\n' "$name" "$code" "$state"
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

# ── llama.cpp runner images (#1516, operator decision E5) ────────────────────
# ONE source of truth: modules/llm/runners/runners.yaml. Before this, the same
# set was written out in cli/init.sh, cli/upgrade.sh, cli/post-install.sh (build
# + publish list) and modules/llm/node-agent/app/drivers/images.py — five copies,
# and #1497 was exactly that drift (the publish list named an image nothing
# builds). Emits one TSV row per target:
#     <image:tag>\t<dockerfile>\t<hardware>\t<legacy image:tag>\t<build args>
# Silent (no rows) when PyYAML or the manifest is missing, so a caller can fall
# back rather than die.
razzfazz_runner_manifest_rows() {
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
    args = " ".join(f"{k}={v}" for k, v in (t.get("build_args") or {}).items())
    print("\t".join([f"{repo}:{t['tag']}", t["dockerfile"], t.get("hardware", ""),
                      t.get("legacy", ""), args]))
PYEOF
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
    local root="${1:-${SCRIPT_DIR:-.}}"
    local image dockerfile hw legacy args a build arch_rows arch
    arch_rows="$(razzfazz_runner_manifest_arch_rows "$root")"
    # `|| [ -n "$image" ]`: a final row without a trailing newline is otherwise
    # dropped — measured on 0.91 for the alias verdict (#979), and cheap here.
    while IFS=$'\t' read -r image dockerfile hw legacy args || [ -n "$image" ]; do
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
            # Derived from the Dockerfile's own path (…/<target>/Dockerfile ->
            # its grandparent) rather than hardcoded, so it follows the tree.
            build="${build} -t ${image} $(dirname "$(dirname "$dockerfile")")"
            arch="$(printf '%s\n' "$arch_rows" | awk -F'\t' -v i="$image" '$1==i {print $2; exit}')"
            printf 'absent\t%s\t%s\t%s\t%s\n' "$image" "$hw" "${arch:-unknown}" "$build"
        fi
        if [ -n "$legacy" ] && docker image inspect "$legacy" >/dev/null 2>&1; then
            printf 'carry\t%s\n' "$legacy"
        fi
    done <<< "$(razzfazz_runner_manifest_rows "$root")"
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
# Whoever wrote that believed `rzfz` was on PATH. Measured by DevBox-Vuko on
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
