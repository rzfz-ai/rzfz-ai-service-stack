#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz.ai Stack - Upgrade Script
# ==============================================================================
# Upgrades the rzfz.ai service stack from the installed version to a
# target version. Supports online (git pull) and offline (upgrade package)
# modes. Automatically migrates .env and .env.dify based on the migration
# manifest in config/migrations/env-changes.json.
#
# Usage:
#   rzfz upgrade                        # Upgrade to latest (git pull)
#   rzfz upgrade --target v1.1.0        # Upgrade to specific tag
#   rzfz upgrade --package upgrade.tar.gz  # Offline upgrade from package
#   rzfz upgrade --check                # Dry run: show what would change
#   rzfz upgrade --help                 # Show all options
#
# ==============================================================================

set -eo pipefail

# Script directory. NOTE (#34/#26 cli/ move): this file lives in cli/, but
# SCRIPT_DIR must remain the REPO ROOT (one level up) — every downstream
# ${SCRIPT_DIR}/... path (scripts/lib.sh, .env, modules/…) is repo-root-relative.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# Upgrade log — opt into lib.sh's RAZZFAZZ_LOG_FILE feature so every print_*
# call also appends an ANSI-stripped, timestamped line to .upgrade.log. This
# subsumes the historical per-print_* `>> "$UPGRADE_LOG"` tee that lived in
# this script's own print_* helpers.
UPGRADE_LOG="${SCRIPT_DIR}/.upgrade.log"
export RAZZFAZZ_LOG_FILE="$UPGRADE_LOG"

# ==============================================================================
# M026 / S02 #9: source the shared library for colors, print_*,
# read_env_value, update_env_value, and check_container.
#
# Local definitions kept further down (intentional name-collision overrides):
#   - print_banner   — upgrade-specific banner text/box.
#   - migrate_env    — upgrade-specific .env migration logic with rc6.2 fixes
#                      and env-snapshot side-effects (DO NOT touch).
#   - restart_stack  — upgrade-specific orchestrator with rc6.7 #46 v3
#                      bridge-attach + monkey-patch for openhands (DO NOT
#                      touch).
#   - verify_upgrade — upgrade-specific health verification.
#   - generate_secret_upgrade / generate_password_upgrade /
#     generate_hex_secret_upgrade — thin re-exports of lib's generators under
#     their suffixed names so all 71 existing call sites stay byte-identical.
# ==============================================================================
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"
# shellcheck source=scripts/lib-journal.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib-journal.sh"

# ==============================================================================
# Utility Functions (consistent with razzfazz-init.sh)
# ==============================================================================
print_banner() {
    echo -e "${CYAN}"
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║                                                                  ║"
    echo "║                 rzfz.ai Service Stack Upgrade                    ║"
    echo "║                                                                  ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# ==============================================================================
# Help
# ==============================================================================
show_help() {
    echo "rzfz.ai Stack - Upgrade Script"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Upgrade Modes:"
    echo "  (no options)                 Pull latest from current git branch"
    echo "  --target TAG                 Checkout specific git tag (e.g., v1.1.0)"
    echo "  --package FILE               Offline upgrade from .tar.gz package"
    echo ""
    echo "Update Mode (container versions only):"
    echo "  --update                     Apply vendor-curated container version bumps"
    echo "  --update --check             Show what would change without applying"
    echo "  --update --file FILE         Apply from a local manifest file (offline)"
    echo "  --update --rollback          Restore previous container versions"
    echo ""
    echo "Options:"
    echo "  --check, --dry-run           Dry run: show changes without applying"
    echo "  --status                     Read-only posture check (delegates to"
    echo "                               razzfazz-status.sh; passes through its flags)"
    echo "  --skip-backup                Skip pre-upgrade backup (not recommended)"
    echo "  --skip-verify                Skip post-upgrade health verification"
    echo "  --skip-host-updates          Skip sysctl + host-hardening installs (no sudo needed)."
    echo "                               Required for non-interactive runs (nohup / SSH disowned)."
    echo "  --force                      Skip confirmation prompts"
    echo "  --rollback                   Roll back the last upgrade"
    echo "  --with-acceptance            (M032-S06) After successful upgrade, wait"
    echo "                               for containers to become healthy (5 min)"
    echo "                               then run rzfz test --ci-mode"
    echo "                               --acceptance all. Probe failures → exit 4"
    echo "                               (distinct from 1 = upgrade-failed). Tests"
    echo "                               only currently-enabled profiles."
    echo "  --with-acceptance-include-disabled"
    echo "                               Same as --with-acceptance but ALSO cycles"
    echo "                               each disabled profile up → probe → down"
    echo "                               (~5 min/profile; intended for handover)."
    echo "                               Override healthz timeout with"
    echo "                               RAZZFAZZ_ACCEPTANCE_HEALTHZ_TIMEOUT=600 (s)."
    echo "  -h, --help                   Show this help"
    echo ""
    echo "Environment Migration:"
    echo "  The upgrade script automatically migrates .env and .env.dify based"
    echo "  on the migration manifest in config/migrations/env-changes.json."
    echo "  User-customized values are preserved. New variables are added with"
    echo "  defaults. Removed variables trigger a warning."
    echo ""
    echo "Examples:"
    echo "  $0                                    # Pull latest, upgrade"
    echo "  $0 --target v1.2.0                    # Upgrade to v1.2.0"
    echo "  $0 --check --target v1.2.0            # Preview v1.2.0 changes"
    echo "  $0 --package razzfazz-v1.2.0.tar.gz   # Offline upgrade"
    echo "  $0 --rollback                          # Undo last upgrade"
    echo "  $0 --update                            # Apply latest container updates"
    echo "  $0 --update --check                    # Preview container updates"
    echo ""
    exit 0
}

# ==============================================================================
# .env Helper
# ==============================================================================
# update_env_value and read_env_value are provided by scripts/lib.sh sourced
# at the top of this file. lib.sh's read_env_value is the canonical rc6.2-fixed
# implementation originally lifted from this script (handles double- AND
# single-quoted values + inline-comment stripping); lib.sh's update_env_value
# uses `|` as the sed delimiter so values containing `/` (paths, URLs) work
# correctly without per-call escape rules.

# ==============================================================================
# Version Comparison
# ==============================================================================
# Supports CalVer (YYYY.MM-rcN / YYYY.MM-ga / YYYY.MM-ga.N) and semver (X.Y.Z).
# Returns 0 if $1 < $2, 1 if equal, 2 if $1 > $2
compare_versions() {
    local v1="${1#v}"
    local v2="${2#v}"

    if [ "$v1" = "$v2" ]; then
        return 1
    fi

    # Use Python for robust mixed-format comparison
    python3 -c "
import re, sys
def parse_version(v):
    v = v.lstrip('v')
    # CalVer: YYYY.MM-rcN or YYYY.MM-ga or YYYY.MM-ga.N or YYYY-MM.GA or YYYY-MM.GA.N
    m = re.match(r'^(\d{4})[.\-](\d{2})[.\-](rc(\d+)|[Gg][Aa](\.(\d+))?)$', v)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if m.group(4):  # rcN
            return (year, month, 0, int(m.group(4)))
        else:  # ga or GA or ga.N
            patch = int(m.group(6)) if m.group(6) else 0
            return (year, month, 1, patch)
    # Semver: X.Y.Z
    parts = v.split('.')
    return tuple(int(re.sub(r'[^0-9]', '', p) or 0) for p in parts)
v1, v2 = parse_version('$v1'), parse_version('$v2')
if v1 < v2: sys.exit(0)
elif v1 == v2: sys.exit(1)
else: sys.exit(2)
" 2>/dev/null
    return $?
}

# Returns 0 (true) if v1 <= v2
version_lte() {
    compare_versions "$1" "$2"
    local rc=$?
    [ $rc -eq 0 ] || [ $rc -eq 1 ]
}

# Returns 0 (true) if v1 < v2
version_lt() {
    compare_versions "$1" "$2"
    [ $? -eq 0 ]
}

# ==============================================================================
# Detect Installed Version
# ==============================================================================
detect_installed_version() {
    local version=""
    local commit=""

    # Primary: read from .env (written by razzfazz-init.sh / razzfazz-upgrade.sh)
    if [ -f ".env" ]; then
        version=$(read_env_value ".env" "RAZZFAZZ_VERSION")
        commit=$(read_env_value ".env" "RAZZFAZZ_COMMIT")
    fi

    # Fallback for commit only: use current git HEAD
    # NOTE: Do NOT fall back to VERSION file for installed version —
    # after 'git pull' VERSION already reflects the TARGET version.
    if [ -z "$commit" ] && command -v git &>/dev/null && [ -d ".git" ]; then
        commit=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    fi

    INSTALLED_VERSION="${version:-unknown}"
    INSTALLED_COMMIT="${commit:-unknown}"

    # Warn about pre-versioning installations
    if [ "$INSTALLED_VERSION" = "unknown" ]; then
        print_warning "No RAZZFAZZ_VERSION found in .env."
        print_warning "This appears to be a pre-versioning installation."
        print_info "The upgrade will treat this as a v0.0.0 baseline."
        print_info "All migrations up to the target version will be applied."
        INSTALLED_VERSION="0.0.0"
    fi
}

# ==============================================================================
# Detect Target Version (after code update)
# ==============================================================================
detect_target_version() {
    if [ -f "VERSION" ]; then
        TARGET_VERSION=$(cat VERSION | tr -d '[:space:]')
    else
        TARGET_VERSION="unknown"
    fi

    if command -v git &>/dev/null && [ -d ".git" ]; then
        TARGET_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    else
        TARGET_COMMIT="unknown"
    fi
}

# ==============================================================================
# Pre-Flight Checks
# ==============================================================================

# ------------------------------------------------------------------------------
# Release-channel / box-role constants (#28)
# ------------------------------------------------------------------------------
# INTERNAL fleet box  -> origin MUST be the SEQIS Gitea (asserted; abort on drift,
#                        per the CLAUDE.md git-remote-standard hard rule).
# PUBLIC customer box  -> origin is REDIRECTED to the public Codeberg mirror and
#                        anonymous pulls are allowed (no git credential needed).
RAZZFAZZ_INTERNAL_REMOTE="https://git.razzfazz.ai/razzfazz.ai/razzfazz-ai-service-stack.git"
# Public remote is single-sourced in scripts/lib.sh (#27) — shared with cli/init.sh
# and `.env`-overridable (RAZZFAZZ_PUBLIC_REMOTE env > .env > Codeberg default) so a
# staging/fork box can redirect origin at an alternate Codeberg for upgrade tests.
RAZZFAZZ_PUBLIC_REMOTE="$(razzfazz_public_remote)"

# Resolve the box's release channel: `internal` (default) or `public`.
# An explicit RAZZFAZZ_CHANNEL environment variable wins over the .env value so
# an operator can override per-invocation; the default is `internal` to preserve
# fleet behaviour for every box that predates this flag. Operates on
# ${SCRIPT_DIR}/.env so it is unit-testable without the network.
razzfazz_channel() {
    local channel
    channel="${RAZZFAZZ_CHANNEL:-}"
    if [ -z "$channel" ]; then
        channel=$(read_env_value "${SCRIPT_DIR}/.env" "RAZZFAZZ_CHANNEL" 2>/dev/null || echo "")
    fi
    case "$channel" in
        public)   printf 'public\n' ;;
        internal) printf 'internal\n' ;;
        "")       printf 'internal\n' ;;   # unset => fleet default
        *)        printf 'internal\n' ;;    # unrecognised => fail safe to internal
    esac
}

# ==============================================================================
# Git credential pre-flight (#3)
# ==============================================================================
# A freshly-installed box has no git credentials configured for the https
# `origin` (git.razzfazz.ai), so the upgrade's `git fetch` fails — historically
# with a cryptic "could not read Username for 'https://...': terminal prompts
# disabled" or an interactive hang. Detect the no-usable-credential condition
# here, BEFORE any fetch, and exit cleanly with an actionable message instead.
#
# Only applies to https remotes (ssh remotes carry their own auth via keys/agent)
# and only on the online git path (skipped for offline --package upgrades).
# Operates on $SCRIPT_DIR and $HOME so it is unit-testable without the network.
check_git_credentials() {
    # Offline upgrade: the git path is never taken — nothing to check.
    [ -n "${PACKAGE_FILE:-}" ] && return 0

    # No git repo (offline tree) — code_update_git handles that separately.
    [ -d "${SCRIPT_DIR}/.git" ] || return 0

    # #28: PUBLIC/customer boxes pull from an anonymous-clone public repo, so no
    # git credential is required — carve them out of the token check entirely.
    # (By the time preflight_checks calls us it has already redirected origin to
    # the public remote; we key off the channel, not the URL, so this holds even
    # when invoked standalone / before the redirect.)
    if [ "$(razzfazz_channel)" = "public" ]; then
        return 0
    fi

    local origin
    origin=$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || echo "")
    # No origin is handled by the remote check in preflight_checks; not our job.
    [ -n "$origin" ] || return 0

    # ssh / git+ssh remotes authenticate via keys/agent — the https credential
    # mechanism does not apply.
    case "$origin" in
        https://*) : ;;
        *) return 0 ;;
    esac

    # Credentials are considered usable if a credential helper is configured
    # AND, for the default `store` helper, ~/.git-credentials carries a line for
    # the remote host. (A non-store helper — e.g. a custom/manager helper — is
    # trusted to supply the credential.)
    local helper host
    helper=$(git -C "$SCRIPT_DIR" config --get credential.helper 2>/dev/null || echo "")
    # Extract host from https://host[:port]/path
    host=$(printf '%s' "$origin" | sed -E 's#^https://([^/@]*@)?([^/:]+).*#\2#')

    if [ -n "$helper" ]; then
        case "$helper" in
            store*|*--file*)
                # `store` (default) keeps creds in ~/.git-credentials (or a
                # --file=... path). Require a matching host line so we catch the
                # "helper set but no token ever entered" fresh-box case too.
                local creds_file="${HOME}/.git-credentials"
                if [ -f "$creds_file" ] && grep -q "@${host}\b" "$creds_file" 2>/dev/null; then
                    return 0
                fi
                ;;
            *)
                # Some other helper (cache, manager, libsecret, …) is configured
                # — trust it to provide credentials; don't second-guess.
                return 0
                ;;
        esac
    fi

    # No usable credential — fail cleanly with an actionable message.
    print_error "No usable git credential for https origin '${origin}'."
    print_info  "A fresh box has no credentials, so 'git fetch' against git.razzfazz.ai"
    print_info  "would fail with a cryptic error or hang on a username prompt."
    echo ""
    print_info  "Fix (operator standard — persists the token across upgrades):"
    print_info  "    git -C \"$SCRIPT_DIR\" config credential.helper store"
    print_info  "    printf 'https://<user>:<PAT>@${host}\\n' >> \"\${HOME}/.git-credentials\""
    print_info  "    chmod 600 \"\${HOME}/.git-credentials\""
    print_info  "  (create a Personal Access Token at https://${host}/user/settings/applications)"
    echo ""
    print_info  "Or upgrade offline with a package (no git creds needed):"
    print_info  "    rzfz upgrade --package <upgrade.tar.gz>"
    return 1
}

preflight_checks() {
    print_step "Running pre-flight checks..."

    # Check .env exists
    if [ ! -f ".env" ]; then
        print_error ".env file not found. Is the stack installed?"
        print_info "Run rzfz init for first-time setup."
        exit 1
    fi

    # BSB-03 / R-DEF-03 / R-COMP-13 — pre-create the host-side files
    # that are now bound as narrow :rw mounts in core/compose.yml. If
    # they don't exist, docker auto-creates them as DIRECTORIES on the
    # next `compose up`, which then breaks both the SQLite open
    # (.checksums.db) and the bind-target sanity. Older installs that
    # upgrade into BSB-03 may not have .checksums.db on disk because
    # the legacy broad :rw mount let it be created on first checksum-
    # take. Idempotent — no-op if files already exist.
    if [ ! -f ".checksums.db" ]; then
        touch .checksums.db && chmod 600 .checksums.db 2>/dev/null || true
    fi
    [ -d "backups" ] || { mkdir -p backups && chmod 700 backups 2>/dev/null || true; }

    # Check Docker
    if ! command -v docker &>/dev/null; then
        print_error "Docker is not installed."
        exit 1
    fi
    if ! docker compose version &>/dev/null; then
        print_error "Docker Compose v2 is required."
        exit 1
    fi

    # Check disk space (at least 5GB free)
    local free_gb
    free_gb=$(df -BG "$SCRIPT_DIR" | awk 'NR==2 {print $4}' | tr -d 'G')
    if [ "${free_gb:-0}" -lt 5 ]; then
        print_error "Less than 5GB disk space available (${free_gb}GB free). Upgrade aborted."
        exit 1
    fi

    # Check if stack is running (needed for DB dumps in backup)
    local running_containers
    running_containers=$(docker compose ps --format '{{.Name}}' 2>/dev/null | wc -l || true)
    if [ "${running_containers:-0}" -eq 0 ]; then
        print_warning "No containers running. Pre-upgrade backup will skip database dumps."
    fi

    # Origin-remote pre-flight. Behaviour depends on the release channel (#28):
    #   internal (default) — ASSERT the SEQIS Gitea origin; abort on drift
    #                        (protects the fleet from ad-hoc bundle/mirror remotes;
    #                        CLAUDE.md git-remote-standard hard rule).
    #   public             — REDIRECT origin to the public Codeberg mirror
    #                        (idempotent set-url); customers have no access to the
    #                        internal Gitea, so aborting would be wrong.
    local channel
    channel=$(razzfazz_channel)
    local actual_remote
    actual_remote=$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || echo "")

    if [ "$channel" = "public" ]; then
        # Customer/public box: point origin at the public release repo.
        if [ -z "$actual_remote" ]; then
            print_warning "No 'origin' git remote configured; setting it to the public repo."
            git -C "$SCRIPT_DIR" remote add origin "$RAZZFAZZ_PUBLIC_REMOTE"
            print_info "origin set to $RAZZFAZZ_PUBLIC_REMOTE"
        elif [ "$actual_remote" != "$RAZZFAZZ_PUBLIC_REMOTE" ]; then
            print_warning "RAZZFAZZ_CHANNEL=public: redirecting origin '$actual_remote' -> public repo."
            git -C "$SCRIPT_DIR" remote set-url origin "$RAZZFAZZ_PUBLIC_REMOTE"
            print_info "origin now points at the public release repo ($RAZZFAZZ_PUBLIC_REMOTE)."
        fi
        # else: already correct — silent, idempotent.
    else
        # Internal fleet box: every razzfazz.ai box pulls from the SEQIS Gitea.
        # Bundle files in /tmp (a stale rc3-era pattern) get cleaned by
        # systemd-tmpfiles on reboot and break git-fetch with a cryptic error.
        # Catch this here with an actionable message instead.
        local expected_remote="$RAZZFAZZ_INTERNAL_REMOTE"
        if [ -z "$actual_remote" ]; then
            print_error "No 'origin' git remote configured."
            print_info "Fix: git -C \"$SCRIPT_DIR\" remote add origin $expected_remote"
            exit 1
        elif [ "$actual_remote" != "$expected_remote" ]; then
            print_warning "git remote 'origin' is '$actual_remote' (expected '$expected_remote')."
            print_info "Operator standard: all razzfazz.ai boxes pull from git.razzfazz.ai."
            print_info "Fix: git -C \"$SCRIPT_DIR\" remote set-url origin $expected_remote"
            print_info "(Customer/public boxes set RAZZFAZZ_CHANNEL=public to pull from the public repo instead.)"
            if [ "${FORCE:-false}" != "true" ]; then
                print_error "Aborting. Re-run with --force to ignore this check."
                exit 1
            fi
            print_warning "Continuing because --force was passed."
        fi
    fi

    # #3: usable git credential check for the online (git) upgrade path. A fresh
    # box with no credential.helper / no ~/.git-credentials would otherwise fail
    # the upcoming `git fetch` with a cryptic error. No-op for --package upgrades.
    if ! check_git_credentials; then
        exit 1
    fi

    # Detect installed version
    detect_installed_version
    print_substep "Installed version: ${INSTALLED_VERSION} (commit: ${INSTALLED_COMMIT})"

    print_success "Pre-flight checks passed."
}

# ==============================================================================
# Pre-Upgrade Backup
# ==============================================================================
pre_upgrade_backup() {
    print_step "Creating pre-upgrade backup..."

    # Governance checksum snapshot (#22: runs on the host now).
    if RAZZFAZZ_STACK_ROOT="$SCRIPT_DIR" python3 "${SCRIPT_DIR}/cli/setup_lib/setup.py" \
            --checksum-take "Pre-upgrade from v${INSTALLED_VERSION}" 2>/dev/null; then
        print_substep "Governance checksum snapshot created."
    else
        print_warning "Could not create checksum snapshot (non-critical)."
    fi

    # Backup .env files
    cp -f ".env" ".env.pre-upgrade-backup"
    print_substep "Backed up .env → .env.pre-upgrade-backup"
    if [ -f ".env.dify" ]; then
        cp -f ".env.dify" ".env.dify.pre-upgrade-backup"
        print_substep "Backed up .env.dify → .env.dify.pre-upgrade-backup"
    fi

    # Full stack backup via razzfazz-backup.sh (if backup container is running)
    if docker ps --format '{{.Names}}' | grep -q "razzfazz-backup-management"; then
        print_substep "Running full stack backup..."
        # #158: call the repo-root rzfz explicitly — `rzfz` is NOT guaranteed on PATH
        # (it isn't on prod 8.246), and a bare `rzfz` silently fails the full-stack
        # backup ("rzfz: command not found") so an upgrade proceeds WITHOUT a backup.
        if "${SCRIPT_DIR}/rzfz" backup backup 2>&1 | tail -5; then
            print_success "Pre-upgrade backup complete."
        else
            print_warning "Backup had issues — proceeding with upgrade."
            print_warning "Your .env files are backed up as .env.pre-upgrade-backup"
        fi
    else
        print_warning "Backup container not running — skipping volume backup."
        print_warning "Your .env files are backed up as .env.pre-upgrade-backup"
    fi
}

# ==============================================================================
# Code Update
# ==============================================================================
detect_latest_release_tag() {
    # Find the latest GA release tag (v*-ga or v*-ga.N pattern) from the remote.
    # RC tags (v*-rc*) are NOT treated as releases — only GA tags are.
    git fetch --tags --force --quiet 2>&1   # #158: --force so a divergent old tag can't fail the latest-detector
    local latest
    # Match v*-ga and v*-ga.N but exclude v*-rc*
    latest=$(git tag --list 'v*-ga*' --sort=-version:refname 2>/dev/null | head -1)
    if [ -z "$latest" ]; then
        print_warning "No GA release tags found (v*-ga* pattern)."
        print_info "Use --target TAG to specify an explicit version."
    fi
    echo "$latest"
}

code_update_git() {
    print_step "Updating code via git..."

    if [ ! -d ".git" ]; then
        print_error "Not a git repository. Use --package for offline upgrades."
        exit 1
    fi

    # Check for uncommitted changes (tracked OR untracked). rc6.3 fix:
    # `-u` (--include-untracked) catches operator-added files that would
    # otherwise block `git checkout <target-tag>` if the target tag has
    # that path tracked. Most common case: razzfazz-upgrade-from-2026.04-
    # GA.x.sh fetched out-of-band via `git show ... > ...` on a 2026.04-ga
    # box (the bootstrap), then `git checkout` aborts because the file is
    # untracked locally but tracked in the target. Files in .gitignore
    # (.env, .env.dify, …) are not touched by stash -u — only tree-visible
    # untracked files are stashed.
    # #185: containers write runtime files INTO the tree as root (docling OCR
    # data, caddy local CA, searxng config). The `git stash -u` below runs as
    # the stack user and CANNOT delete root-owned untracked files →
    # "Permission denied" → the entire upgrade aborts (hit prod + 0.208).
    # Pre-emptively reclaim ownership: a ROOT docker container (no host sudo,
    # headless-safe; same `alpine chown` pattern as the media-migrator), with a
    # `sudo -n` fallback. Best-effort: a warning here is better than an abort.
    if [ -n "$(find . -path ./.git -prune -o ! -user "$(id -u)" -print -quit 2>/dev/null)" ]; then
        print_substep "Reclaiming ownership of container-written files before stash (#185)..."
        local _reclaimed=false
        if command -v docker >/dev/null 2>&1 && docker run --rm -v "$PWD":/repo alpine \
               sh -c "find /repo -path /repo/.git -prune -o ! -user $(id -u) -exec chown $(id -u):$(id -g) {} +" >/dev/null 2>&1; then
            _reclaimed=true
        elif sudo -n find . -path ./.git -prune -o ! -user "$(id -u)" -exec chown "$(id -u):$(id -g)" {} + >/dev/null 2>&1; then
            _reclaimed=true
        fi
        [ "$_reclaimed" = true ] || print_warning "Could not reclaim file ownership pre-stash; stash may abort on root-owned files (see #185)."
    fi

    local has_tracked_changes=false
    local has_untracked=false
    local STASH_UNTRACKED_ONLY=false
    git diff --quiet 2>/dev/null || has_tracked_changes=true
    git diff --cached --quiet 2>/dev/null || has_tracked_changes=true
    [ -n "$(git ls-files --others --exclude-standard 2>/dev/null)" ] && has_untracked=true
    if [ "$has_tracked_changes" = true ] || [ "$has_untracked" = true ]; then
        print_warning "Uncommitted changes detected. Stashing..."
        git stash push -u -m "razzfazz-upgrade: auto-stash before upgrade to ${TARGET_TAG:-latest}" 2>&1
        STASHED=true
        [ "$has_tracked_changes" = false ] && STASH_UNTRACKED_ONLY=true
        print_substep "Changes stashed (including untracked). Will restore after upgrade if needed."
    fi

    if [ -z "$TARGET_TAG" ]; then
        # No explicit target — resolve latest release tag
        print_substep "Fetching tags from remote..."
        TARGET_TAG=$(detect_latest_release_tag)
        if [ -z "$TARGET_TAG" ]; then
            print_error "No release tags found. Use --target TAG to specify a version."
            exit 1
        fi
        print_substep "Latest release: ${TARGET_TAG}"
    else
        # F3: skip remote fetch if the target tag is already present locally
        # (supports offline / air-gapped environments where the upgrade tag
        # was delivered out-of-band via `git bundle`, scp, or a sideloaded tag).
        if git rev-parse -q --verify "refs/tags/${TARGET_TAG}" >/dev/null 2>&1; then
            print_substep "Target tag ${TARGET_TAG} already present locally — skipping fetch."
        else
            print_substep "Fetching from remote..."
            # #158 --force: a DIVERGENT *old* tag (e.g. a historically-moved
            # v2026.05-ga.4) makes a plain `git fetch --tags` return non-zero with
            # "! [rejected] ... would clobber existing tag" — which aborted the whole
            # upgrade even though the TARGET tag fetched fine (prod 8.246, 2026-07-11).
            # Force-update tags to match origin (authoritative for GA tags).
            if ! git fetch --tags --force 2>&1; then
                print_error "git fetch failed. Seed the tag locally (git bundle / scp / manual add-remote) and retry."
                exit 1
            fi
        fi
    fi

    # #125 P1 retention guarantee: the box-local Enterprise-docs overlay lives at
    # overlay/ and is GITIGNORED. The `git stash push -u` above does NOT stash ignored
    # files (only tree-visible untracked ones), and the `git checkout <target-tag>`
    # below does NOT remove ignored files — so overlay/ (and the gated Enterprise docs
    # in it) SURVIVES an upgrade to a Codeberg tag whose tree has no docs/enterprise/.
    # Retention is by construction: do NOT add `-x`/`--include-ignored` to the stash,
    # and do NOT `git clean -x` overlay/.
    # Checkout the target tag
    print_substep "Checking out ${TARGET_TAG}..."
    if ! git checkout "$TARGET_TAG" 2>&1; then
        print_error "Failed to checkout tag ${TARGET_TAG}."
        if [ "${STASHED:-false}" = true ]; then
            git stash pop 2>/dev/null || true
        fi
        exit 1
    fi

    detect_target_version
    print_success "Code updated to version ${TARGET_VERSION} (commit: ${TARGET_COMMIT})."

    # #158: prevent pre-upgrade auto-stash accumulation (16 piled up on prod 8.246).
    # After a successful checkout, an UNTRACKED-only stash is stale runtime cruft
    # (regenerated .checksums.db / certs/caddy-ca.pem / logs) that the fresh tree +
    # provisioning supersede — drop it, and clear STASHED so the re-exec'd rollback
    # path won't try to pop a gone stash. A stash that held TRACKED operator edits is
    # KEPT (with a notice) so a rollback can still restore it.
    if [ "${STASHED:-false}" = true ]; then
        if [ "${STASH_UNTRACKED_ONLY:-false}" = true ]; then
            git stash drop 2>/dev/null && { print_substep "Dropped the pre-upgrade auto-stash (untracked runtime cruft)."; STASHED=false; } || true
        else
            print_warning "Pre-upgrade auto-stash retained (held tracked-file edits) — review: git stash list / git stash show -p stash@{0}"
        fi
    fi
}

reexec_if_updated() {
    # Re-execute the upgrade script from the new code if it changed.
    # After git checkout, the script on disk is the new version but bash
    # is still running the old one from memory. This re-launches the new
    # script with --_continue to skip code-update and proceed to migrations.
    local new_script="${SCRIPT_DIR}/legacy/razzfazz-upgrade.sh"
    if [ ! -f "$new_script" ]; then
        return 0  # Can't re-exec, continue in-place
    fi

    # Build the argument list for the re-exec'd script
    local args=("--_continue")
    [ "$SKIP_BACKUP" = true ] && args+=("--skip-backup")
    [ "$SKIP_VERIFY" = true ] && args+=("--skip-verify")
    [ "$FORCE" = true ] && args+=("--force")
    [ "$DRY_RUN" = "true" ] && args+=("--check")
    # rc6.7 #51 / M026: propagate --skip-host-updates across reexec.
    # Without this, a re-exec'd upgrade always re-runs sysctl/host-hardening
    # even when the operator opted out — and the sysctl path is fatal-on-fail
    # without sudo (F-RC5-5), so the whole upgrade exits 1 mid-flight.
    [ "${SKIP_HOST_UPDATES:-false}" = true ] && args+=("--skip-host-updates")

    # Pass version info via environment so the new script doesn't re-detect
    export _UPGRADE_FROM_VERSION="$INSTALLED_VERSION"
    export _UPGRADE_FROM_COMMIT="$INSTALLED_COMMIT"
    export _UPGRADE_TO_VERSION="$TARGET_VERSION"
    export _UPGRADE_TO_COMMIT="$TARGET_COMMIT"
    export _UPGRADE_STASHED="${STASHED:-false}"

    print_substep "Re-executing upgrade script from ${TARGET_VERSION}..."
    echo "DEBUG[reexec_pre]: FORCE=$FORCE SKIP_BACKUP=$SKIP_BACKUP DRY_RUN=$DRY_RUN args=(${args[*]})" >&2
    exec bash "$new_script" "${args[@]}"
}

code_update_package() {
    print_step "Updating code from offline package..."

    if [ ! -f "$PACKAGE_FILE" ]; then
        print_error "Package file not found: ${PACKAGE_FILE}"
        exit 1
    fi

    # Verify package integrity
    print_substep "Verifying package..."
    local pkg_dir
    pkg_dir=$(mktemp -d "${SCRIPT_DIR}/.upgrade-pkg-XXXXXX")

    # Safe extraction: validate paths before extracting
    if ! tar tzf "$PACKAGE_FILE" 2>/dev/null | while read -r member; do
        case "$member" in
            /*|../*|*/../*) echo "UNSAFE: $member"; exit 1 ;;
        esac
    done; then
        print_error "Package contains unsafe paths. Aborting."
        rm -rf "$pkg_dir"
        exit 1
    fi

    tar xzf "$PACKAGE_FILE" -C "$pkg_dir" 2>&1
    print_substep "Package extracted."

    # Check for manifest
    if [ -f "${pkg_dir}/MANIFEST.sha256" ]; then
        print_substep "Verifying package checksums..."
        if (cd "$pkg_dir" && sha256sum -c MANIFEST.sha256 --quiet 2>/dev/null); then
            print_substep "Package integrity verified."
        else
            print_warning "Some checksums did not match — proceeding with caution."
        fi
    fi

    # Copy files (exclude .env, .env.dify, .git, backups, volumes)
    # #125 P1.5: enterprise-overlay/ is a SELF-CONTAINED payload, not a tree path — keep
    # it out of the repo root and stage it into the box-local overlay/enterprise/ below.
    print_substep "Applying package files..."
    # images/ + models/ are docker-load / model-deploy payloads consumed straight
    # from ${pkg_dir} below — they must NOT be rsync'd into the repo. Copying them
    # in left a 153 GB images/ dir in the working tree (never cleaned; only pkg_dir
    # is), which then hangs the next online upgrade's `git stash -u` on the huge
    # untracked tree. Exclude them here. Refs #209.
    rsync -a --exclude='.env' --exclude='.env.dify' --exclude='.git' \
          --exclude='backups/' --exclude='.checksums.db' \
          --exclude='enterprise-overlay/' \
          --exclude='images/' --exclude='models/' \
          --exclude='.env.pre-upgrade-backup' --exclude='.env.dify.pre-upgrade-backup' \
          --exclude='.upgrade.log' \
          "${pkg_dir}/" "${SCRIPT_DIR}/"
    print_substep "Package files applied."

    # #125 P1.5: stage a bundled Enterprise overlay (subscription package) into the
    # box-local, gitignored overlay/enterprise/ so an airgapped box gets the gated docs +
    # security assessment + SBOM with no network fetch. Best-effort; community packages
    # carry no enterprise-overlay/ so this is a no-op for them. Uses the freshly-applied
    # (new) sync-enterprise-overlay.sh.
    if [ -d "${pkg_dir}/enterprise-overlay" ] && [ -x "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" ]; then
        print_substep "Staging bundled Enterprise overlay into overlay/enterprise/ (#125)..."
        "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" --from-payload "${pkg_dir}/enterprise-overlay" 2>&1 \
          || print_warning "Enterprise overlay stage failed (Help-UI may serve community-only)."
    fi

    # Load Docker images if included
    if ls "${pkg_dir}"/images/*.tar 2>/dev/null | head -1 >/dev/null; then
        print_substep "Loading Docker images from package..."
        for img in "${pkg_dir}"/images/*.tar; do
            docker load -i "$img" 2>&1 | tail -1
        done
        SKIP_PULL=true
        print_substep "Docker images loaded."
    fi

    # #184 P1 / WS7a(load): copy bundled model GGUFs into the gpustack-data
    # volume at $RAZZFAZZ_LOCAL_MODELS_DIR, BEFORE the post-restart provisioning,
    # so the OFFLINE deploy path (WS7b, source=local_path) finds them. Uses
    # `docker cp` into the running gpustack container (the stack is still up at
    # code-update time) — NO helper image, so it works on a fully air-gapped box
    # where `docker run alpine` would fail. Harmless online (the GGUFs just sit
    # in the volume until an offline deploy references them). Gate = the package
    # actually carries models/.
    if ls "${pkg_dir}"/models/* >/dev/null 2>&1; then
        print_substep "Loading bundled model GGUFs into the gpustack-data volume (${RAZZFAZZ_LOCAL_MODELS_DIR})..."
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gpustack; then
            if docker exec gpustack mkdir -p "$RAZZFAZZ_LOCAL_MODELS_DIR" >/dev/null 2>&1 \
               && docker cp "${pkg_dir}/models/." "gpustack:${RAZZFAZZ_LOCAL_MODELS_DIR}/" >/dev/null 2>&1; then
                print_substep "Model GGUFs loaded into ${RAZZFAZZ_LOCAL_MODELS_DIR}/ — offline deploy will register them via source=local_path."
            else
                print_warning "Could not copy bundled model GGUFs into the gpustack-data volume — offline model deploy may report missing GGUFs. Re-check with 'rzfz verify-models' after the upgrade."
            fi
        else
            print_warning "gpustack container not running — deferring model-GGUF load. Run 'rzfz verify-models' after the stack restarts; re-apply the package if GGUFs are missing."
        fi
    fi

    # #272: the offline rsync excludes .git, so detect_target_version's
    # `git rev-parse HEAD` stays at the PRE-upgrade commit. Read the real target
    # commit the package baked in (PACKAGE_INFO.package_commit, written by
    # cli/package.sh from `git rev-parse <TAG>`) so RAZZFAZZ_COMMIT reflects the
    # deployed code, not the stale HEAD — otherwise `rzfz status` reports a false
    # "tag has MOVED on origin".
    local pkg_commit=""
    if [ -f "${pkg_dir}/PACKAGE_INFO" ]; then
        pkg_commit=$(grep '^package_commit=' "${pkg_dir}/PACKAGE_INFO" \
                     | cut -d= -f2- | tr -d '[:space:]')
    fi

    rm -rf "$pkg_dir"

    detect_target_version            # TARGET_VERSION from the package VERSION file (fresh)
    if [ -n "$pkg_commit" ] && [ "$pkg_commit" != "unknown" ]; then
        TARGET_COMMIT="$pkg_commit"  # authoritative deployed commit from the package
    else
        TARGET_COMMIT="offline-package"   # explicit non-git sentinel
    fi
    # Record offline-package provenance so `rzfz status` treats VERSION/
    # RAZZFAZZ_VERSION as authoritative and skips the git tag-drift check (#272).
    update_env_value ".env" "RAZZFAZZ_UPGRADE_METHOD" "offline-package"
    print_success "Code updated to version ${TARGET_VERSION} from package."
}

# ==============================================================================
# Environment Migration
# ==============================================================================
migrate_env() {
    print_step "Migrating environment files..."
    
    # Snapshot .env before migration
    if [ -f "${SCRIPT_DIR}/scripts/env-snapshot.sh" ]; then
        source "${SCRIPT_DIR}/scripts/env-snapshot.sh"
        env_snapshot "pre-upgrade-migration"
    fi

    local manifest="${SCRIPT_DIR}/config/migrations/env-changes.json"
    if [ ! -f "$manifest" ]; then
        print_warning "No migration manifest found. Skipping .env migration."
        return 0
    fi

    # Parse manifest and apply changes for versions between installed and target
    local changes_applied=0
    local warnings=0

    # Use Python for JSON parsing (available in all Docker-capable systems)
    local migration_script
    migration_script=$(cat <<'PYEOF'
import json
import sys
import os

def compare_versions(v1, v2):
    """Compare version strings. Supports CalVer (YYYY.MM-rcN[.M]/ga[.N]/YYYY-MM.GA) and semver (X.Y.Z).

    Returns 5-tuple (year, month, channel, n1, n2) where channel: 0=rc, 1=ga.
    Examples:
      2026.05-rc5    → (2026, 5, 0, 5, 0)
      2026.05-rc5.1  → (2026, 5, 0, 5, 1)
      2026.05-rc6    → (2026, 5, 0, 6, 0)
      2026.05-ga     → (2026, 5, 1, 0, 0)
      2026.05-ga.1   → (2026, 5, 1, 1, 0)
    Bug fix (M023-S05.1): the prior regex did not accept rcN.M form, so
    2026.05-rc5.1 fell into the generic split-by-dot fallback and parsed as
    (2026, 5, 5, 1), which compared GREATER than 2026.05-rc6's (2026, 5, 0, 6)
    on the channel digit. migrate_env then silently skipped the rc6 entry.
    """
    import re
    def parse(v):
        v = v.lstrip('v')
        m = re.match(r'^(\d{4})[.\-](\d{2})[.\-]rc(\d+)(?:\.(\d+))?$', v)
        if m:
            return (int(m.group(1)), int(m.group(2)), 0,
                    int(m.group(3)), int(m.group(4) or 0))
        m = re.match(r'^(\d{4})[.\-](\d{2})[.\-][Gg][Aa](?:\.(\d+))?$', v)
        if m:
            return (int(m.group(1)), int(m.group(2)), 1,
                    int(m.group(3) or 0), 0)
        parts = v.split('.')
        return tuple(int(re.sub(r'[^0-9]', '', p) or 0) for p in parts)
    p1, p2 = parse(v1), parse(v2)
    if p1 < p2: return -1
    if p1 > p2: return 1
    return 0

installed = sys.argv[1]
target = sys.argv[2]
manifest_path = sys.argv[3]
check_only = sys.argv[4] == "true"

with open(manifest_path) as f:
    manifest = json.load(f)

# Find versions to apply (installed < version <= target)
applicable = []
for entry in manifest["versions"]:
    v = entry["version"]
    if compare_versions(v, installed) > 0 and compare_versions(v, target) <= 0:
        applicable.append(entry)

# Sort by version — same parser as compare_versions above, kept inline for the
# Python heredoc isolation. Bug fix (M023-S05.1): also accepts rcN.M.
def sort_key(e):
    import re
    v = e["version"]
    m = re.match(r'^(\d{4})[.\-](\d{2})[.\-]rc(\d+)(?:\.(\d+))?$', v)
    if m:
        return (int(m.group(1)), int(m.group(2)), 0,
                int(m.group(3)), int(m.group(4) or 0))
    m = re.match(r'^(\d{4})[.\-](\d{2})[.\-][Gg][Aa](?:\.(\d+))?$', v)
    if m:
        return (int(m.group(1)), int(m.group(2)), 1,
                int(m.group(3) or 0), 0)
    parts = v.split('.')
    return tuple(int(re.sub(r'[^0-9]', '', p) or 0) for p in parts)
applicable.sort(key=sort_key)

if not applicable:
    print("NO_MIGRATIONS")
    sys.exit(0)

needs_build = False
needs_pull = False

for entry in applicable:
    v = entry["version"]
    print(f"VERSION:{v}")

    if entry.get("requires_build"):
        needs_build = True
    if entry.get("requires_pull"):
        needs_pull = True

    for bc in entry.get("breaking_changes", []):
        print(f"BREAKING:{bc}")

    if entry.get("notes"):
        print(f"NOTES:{entry['notes']}")

    for change in entry.get("env_changes", []):
        action = change["action"]
        target_file = change.get("file", ".env")

        # Field separator is U+001F (ASCII Unit Separator) so URL-shaped
        # `default` values (e.g. https://creators.dify.ai) survive the bash
        # IFS=$'\x1f' read on the consumer side. Pre-rc6.2 used ':' which
        # collided with URLs and silently truncated values.
        SEP = "\x1f"
        if action == "add":
            print(f"ADD:{target_file}{SEP}{change['key']}{SEP}{change.get('default', '')}{SEP}{change.get('comment', '')}")
        elif action == "remove":
            print(f"REMOVE:{target_file}{SEP}{change['key']}")
        elif action == "rename":
            print(f"RENAME:{target_file}{SEP}{change['old_key']}{SEP}{change['new_key']}")
        elif action == "change_default":
            print(f"CHANGE_DEFAULT:{target_file}{SEP}{change['key']}{SEP}{change.get('old_default', '')}{SEP}{change.get('new_default', '')}")

if needs_build:
    print("FLAG:REQUIRES_BUILD")
if needs_pull:
    print("FLAG:REQUIRES_PULL")
PYEOF
    )

    local migration_output
    migration_output=$(python3 -c "$migration_script" \
        "$INSTALLED_VERSION" "$TARGET_VERSION" "$manifest" "$DRY_RUN" 2>&1)

    if [ $? -ne 0 ]; then
        print_error "Failed to parse migration manifest: $migration_output"
        return 1
    fi

    if [ "$migration_output" = "NO_MIGRATIONS" ]; then
        print_substep "No .env migrations needed for this version range."
        return 0
    fi

    REQUIRES_BUILD=false
    REQUIRES_PULL=false

    while IFS= read -r line; do
        local cmd="${line%%:*}"
        local rest="${line#*:}"

        case "$cmd" in
            VERSION)
                print_substep "Applying migrations for v${rest}..."
                ;;
            BREAKING)
                print_warning "BREAKING CHANGE: ${rest}"
                ((warnings++)) || true
                ;;
            NOTES)
                print_info "  ${rest}"
                ;;
            FLAG)
                case "$rest" in
                    REQUIRES_BUILD) REQUIRES_BUILD=true ;;
                    REQUIRES_PULL) REQUIRES_PULL=true ;;
                esac
                ;;
            ADD)
                IFS=$'\x1f' read -r target_file key default comment <<< "$rest"
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would add ${key}=${default} to ${target_file}"
                else
                    local current
                    current=$(read_env_value "$target_file" "$key")
                    if [ -z "$current" ]; then
                        if [ -n "$comment" ]; then
                            echo "# ${comment}" >> "$target_file"
                        fi
                        update_env_value "$target_file" "$key" "$default"
                        print_substep "  Added ${key} to ${target_file}"
                        ((changes_applied++)) || true
                    else
                        print_substep "  ${key} already exists in ${target_file} — skipped"
                    fi
                fi
                ;;
            REMOVE)
                IFS=$'\x1f' read -r target_file key <<< "$rest"
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would remove ${key} from ${target_file}"
                else
                    if grep -q "^${key}=" "$target_file" 2>/dev/null; then
                        sed -i "/^${key}=/d" "$target_file"
                        print_substep "  Removed ${key} from ${target_file}"
                        ((changes_applied++)) || true
                    fi
                fi
                ;;
            RENAME)
                IFS=$'\x1f' read -r target_file old_key new_key <<< "$rest"
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would rename ${old_key} → ${new_key} in ${target_file}"
                else
                    local old_val
                    old_val=$(read_env_value "$target_file" "$old_key")
                    if [ -n "$old_val" ]; then
                        update_env_value "$target_file" "$new_key" "$old_val"
                        sed -i "/^${old_key}=/d" "$target_file"
                        print_substep "  Renamed ${old_key} → ${new_key} in ${target_file}"
                        ((changes_applied++)) || true
                    fi
                fi
                ;;
            CHANGE_DEFAULT)
                IFS=$'\x1f' read -r target_file key old_default new_default <<< "$rest"
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would change default of ${key}: ${old_default} → ${new_default}"
                else
                    local current_val
                    current_val=$(read_env_value "$target_file" "$key")
                    if [ "$current_val" = "$old_default" ]; then
                        update_env_value "$target_file" "$key" "$new_default"
                        print_substep "  Changed ${key} default in ${target_file}"
                        ((changes_applied++)) || true
                    else
                        print_substep "  ${key} has custom value — preserved"
                    fi
                fi
                ;;
        esac
    done <<< "$migration_output"

    # ------------------------------------------------------------------
    # #169: vendor-managed version pins self-heal on upgrade.
    #
    # Every component version lives in .env as a *_VERSION pin — the
    # third-party image tags (GOTENBERG_VERSION, KOMODO_VERSION,
    # SEARXNG_VERSION, VALKEY_VERSION, AUTHENTIK_VERSION, …) AND the
    # custom-image build ARGs (OPENCODE_VERSION, GSD_PI_VERSION,
    # PAPERCLIP_VERSION). Historically a version bump touched .env.example +
    # the manifest (and, for builds, the Dockerfile ARG) but NOT the
    # operator's LIVE .env, because no change_default rule was added. So the
    # live pin drifted: third-party images stayed on the old tag and custom
    # images rebuilt from the stale pin — indefinitely, across upgrades.
    #
    # These are NOT operator-tunable configuration — version selection is a
    # release decision (to hold a version, the release holds it in
    # .env.example). So on every upgrade we force-sync EVERY non-empty
    # *_VERSION pin from the shipped .env.example to the live .env. Unlike a
    # change_default rule this is box-history-independent: it corrects ANY
    # drifted value, not just one known old default. Empty .env.example
    # values are skipped — crucially this leaves RAZZFAZZ_VERSION (empty in
    # the template, stamped by the upgrade itself) untouched. Any sync forces
    # a build + pull so the corrected pins actually take effect.
    if [ -f "config/.env.example" ]; then
        local pin want_val have_val
        for pin in $(grep -oE '^[A-Z0-9_]+_VERSION=' config/.env.example | sed 's/=$//' | sort -u); do
            want_val=$(read_env_value "config/.env.example" "$pin")
            [ -z "$want_val" ] && continue          # empty template default (e.g. RAZZFAZZ_VERSION) — never sync
            have_val=$(read_env_value ".env" "$pin")
            [ -z "$have_val" ] && continue          # absent in live .env → leave to any ADD rule
            [ "$have_val" = "$want_val" ] && continue
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would sync version pin ${pin}: ${have_val} → ${want_val}"
            else
                update_env_value ".env" "$pin" "$want_val"
                print_substep "  Synced version pin ${pin}: ${have_val} → ${want_val}"
                ((changes_applied++)) || true
                REQUIRES_BUILD=true
                REQUIRES_PULL=true
            fi
        done
    fi

    # ------------------------------------------------------------------
    # #182: physical DB datadirs must ALWAYS be excluded from the volume archive.
    #
    # 2026.07 flipped postgres + postgres-komodo to stop-during-backup=false so
    # the nightly backup no longer stops postgres (which caused a ~24 s DB-
    # unavailability blip → dependent apps 500). That is ONLY safe because the
    # physical datadirs are captured LOGICALLY — core/backup/pre-backup.sh runs
    # `pg_dumpall` (postgres_core.sql + postgres_komodo.sql) and valkey SAVE —
    # and EXCLUDED from the tar, so offen never tars a churning pg_wal (the
    # `lstat …/pg_wal/…: no such file` race that silently failed the whole
    # nightly + upgrade backup, #157).
    #
    # The matching manifest `change_default` (old `gpustack-data|speaches-data`
    # → new 5-token default) only fires when the live value EXACTLY equals the
    # old shipped default, so it MISSES boxes with a customised regexp (models
    # included, dify-plugins excluded, or the PSA hot-fix already applied). On
    # such a box the label flip would leave postgres-data IN the tar with the
    # stop OFF = #157 again. This heal closes that gap: box-history-independent
    # (same principle as the #169 version-pin sync above), it force-APPENDS any
    # of the three datadir tokens that are missing, preserving every other
    # exclusion the operator set. Idempotent — a token already present (exact
    # `|`-delimited match, so `postgres-data` ≠ `postgres-komodo-data`) is left
    # untouched. Backup-service picks up the new .env value on the post-upgrade
    # `compose up -d` (env change → recreate), so no build/pull is forced here.
    local _cur_excl _new_excl _tok
    _cur_excl=$(read_env_value ".env" "BACKUP_EXCLUDE_REGEXP")
    _new_excl="$_cur_excl"
    for _tok in postgres-data valkey-data postgres-komodo-data; do
        case "|${_new_excl}|" in
            *"|${_tok}|"*) : ;;   # already excluded — leave alone
            *) if [ -n "$_new_excl" ]; then _new_excl="${_new_excl}|${_tok}"; else _new_excl="$_tok"; fi ;;
        esac
    done
    if [ "$_new_excl" != "$_cur_excl" ]; then
        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would exclude DB datadirs from backup (#182): '${_cur_excl}' → '${_new_excl}'"
        else
            update_env_value ".env" "BACKUP_EXCLUDE_REGEXP" "$_new_excl"
            print_substep "  Excluded physical DB datadirs from backup (#182): '${_cur_excl}' → '${_new_excl}'"
            ((changes_applied++)) || true
        fi
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_info "Dry run complete. No changes applied."
    else
        print_success "Environment migration complete: ${changes_applied} changes, ${warnings} warnings."
    fi
}

# ==============================================================================
# Reconcile stale *_VERSION pins against the shipped manifest (#177)
# ==============================================================================
# migrate_env() above only rewrites a *_VERSION pin when config/migrations/
# env-changes.json carries an explicit `change_default` entry for it (or, for
# the box-history-independent case, when the #169 .env.example-derived sync a
# few lines up actually runs). Either path can miss a component: a version
# bump that never got a change_default rule, or a box whose live .env already
# diverged before that bump shipped, leaves the pin stale in `.env` forever —
# `docker compose pull`/`up -d` can't fix it because the operator's explicit
# `.env` value always wins over the compose `${VAR:-<manifest-default>}`
# fallback. Observed stuck on old baselines: KOMODO_VERSION, GOTENBERG_VERSION,
# VALKEY_VERSION (#177).
#
# config/manifests/versions.json is the single authoritative statement of
# "what this release ships" — the check-and-bump-versions workflow keeps it,
# .env.example, compose defaults, and Dockerfile ARGs in lock-step. This pass
# walks every entry under `images` and `hardcoded` that carries BOTH an
# `env_var` and a `current` value and, for any such key ALREADY present in the
# live `.env` with a value that differs from `current`, forces it to
# `current`. Entries missing `env_var`/`current` (most of `hardcoded`, which
# has no operator-facing env var) are skipped.
#
# Deliberately additive-only, same rule as the #169 sync above: this NEVER
# creates a key that doesn't already exist in `.env` — introducing a brand
# new pin is what an `add` env-changes rule is for. Version selection itself
# is a release decision, not operator-tunable config, so an existing pin that
# drifted from the manifest is always corrected, never "preserved as custom".
reconcile_version_pins_from_manifest() {
    print_step "Reconciling *_VERSION pins against manifest (#177)..."

    local manifest="${SCRIPT_DIR}/config/manifests/versions.json"
    if [ ! -f "$manifest" ]; then
        print_substep "No manifest at ${manifest} — skipping version-pin reconcile."
        return 0
    fi

    # Field separator U+001F (ASCII Unit Separator) — same convention migrate_env
    # uses for its own Python→bash handoff above, so a `current` value that
    # happens to contain `:` (e.g. an OCI digest pin) can't be misparsed.
    local pin_script
    pin_script=$(cat <<'PYEOF'
import json
import sys

manifest_path = sys.argv[1]
SEP = "\x1f"

with open(manifest_path) as f:
    manifest = json.load(f)

seen = set()
for section in ("images", "hardcoded"):
    for _name, entry in manifest.get(section, {}).items():
        env_var = entry.get("env_var")
        current = entry.get("current")
        if not env_var or not current:
            continue
        if env_var in seen:
            continue
        seen.add(env_var)
        print(f"{env_var}{SEP}{current}")
PYEOF
    )

    local pin_output
    pin_output=$(python3 -c "$pin_script" "$manifest" 2>&1)
    if [ $? -ne 0 ]; then
        print_warning "Failed to parse manifest for version-pin reconcile: ${pin_output}"
        return 0
    fi

    local reconciled=0
    local env_var current_val live_val
    while IFS=$'\x1f' read -r env_var current_val; do
        [ -n "$env_var" ] || continue
        live_val=$(read_env_value ".env" "$env_var")
        [ -n "$live_val" ] || continue            # not set in live .env — leave to an `add` rule
        [ "$live_val" = "$current_val" ] && continue

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would reconcile ${env_var}: ${live_val} → ${current_val} (manifest)"
        else
            update_env_value ".env" "$env_var" "$current_val"
            print_substep "  reconciled ${env_var}: ${live_val} → ${current_val} (manifest)"
            ((reconciled++)) || true
            REQUIRES_BUILD=true
            REQUIRES_PULL=true
        fi
    done <<< "$pin_output"

    if [ "$DRY_RUN" != "true" ]; then
        if [ "$reconciled" -gt 0 ]; then
            print_success "Reconciled ${reconciled} stale *_VERSION pin(s) to manifest."
        else
            print_substep "All *_VERSION pins already match manifest — nothing to reconcile."
        fi
    fi
}

# ==============================================================================
# Reconcile per-service DB users (#184)
# ==============================================================================
# A regression introduced in 2026.05-rc2 re-added every per-service `*_DB_USER`
# key to migrations/env-changes.json with an EMPTY default. A box that first
# enabled a profile while passing that migration got `<SVC>_DB_USER=` appended
# empty. compose then falls back to the global superuser, e.g.
#   modules/apps/onyx/compose.yml          POSTGRES_USER: ${ONYX_DB_USER:-${POSTGRES_USER:-docker}}
#   modules/doc-processing/paperless-ngx   PAPERLESS_DBUSER: ${PAPERLESS_DB_USER:-${POSTGRES_USER:-docker}}
# so the container connects as `docker` but presents the per-service password →
# "password authentication failed for user docker" crash-loop (hit onyx +
# paperless on the prod 8.246 ga.7→ga.2 upgrade, 2026-06-18).
#
# This heal is version-INDEPENDENT on purpose. A manifest `change_default` would
# only run from the *target* tree, so a box upgrading INTO an already-cut tag
# can never pick up a retro-fitted manifest line. We treat .env.example as the
# source of truth for the correct per-service role name.
#
# v2 (2026-06-20) — PROBE BEFORE HEALING. The naive heal broke OLDER boxes: on a
# box provisioned before per-service roles existed, every service connects as the
# global `docker` superuser via the empty→${POSTGRES_USER:-docker} fallback, and
# that WORKS (the docker user has the global password). Blindly setting
# `<SVC>_DB_USER=<svc>_user` there points the service at a role that doesn't
# exist / can't authenticate → "password authentication failed for user
# <svc>_user", taking authentik/openwebui/dify down (observed on the 0.208
# ga.2→ga.3 upgrade). So: only heal a given key if the per-service role can
# actually authenticate against the running postgres with its per-service
# password; otherwise leave it empty (the working docker-superuser fallback).
# - onyx/paperless box (per-service roles exist): probe succeeds → heal (the
#   original fix — un-breaks the per-service-password-vs-docker-user mismatch).
# - older docker-superuser box (no per-service role): probe fails → skip → the
#   box keeps working on the global superuser.
# Non-destructive: a populated `*_DB_USER` is always preserved.
reconcile_service_db_users() {
    print_step "Reconciling per-service DB users (#184)..."

    if [ ! -f "config/.env.example" ]; then
        print_substep "No .env.example template — skipping DB-user reconcile."
        return 0
    fi

    # Probe: can role $1 authenticate against the running postgres with
    # password $2? Returns 0 (yes) only on a clean SELECT 1. Runs inside the
    # postgres container (still up at migrate_env time, pre-restart); any
    # failure mode — role missing, wrong password, postgres down, no docker —
    # returns non-zero so we DON'T heal (safe default = leave the working
    # docker-superuser fallback in place). Connect to the maintenance `postgres`
    # DB (PUBLIC CONNECT is not revoked), so the probe tests auth, not grants.
    _db_role_authenticates() {
        local role="$1" pw="$2"
        [ -n "$role" ] || return 1
        command -v docker >/dev/null 2>&1 || return 1
        docker exec -e PGPASSWORD="$pw" postgres \
            psql -U "$role" -d postgres -h 127.0.0.1 -tAc 'SELECT 1' \
            >/dev/null 2>&1
    }

    local healed=0 skipped=0
    local key example_val live_val pwkey svc_pw
    # Candidates: every `*_DB_USER` key that ships a non-empty value in the
    # template. read_env_value is targeted (we never source .env — operator
    # values carry spaces/metachars).
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        example_val=$(read_env_value "config/.env.example" "$key")
        [ -n "$example_val" ] || continue
        live_val=$(read_env_value ".env" "$key")
        [ -z "$live_val" ] || continue   # populated → preserve
        # Only heal if the per-service role actually authenticates — otherwise
        # this is a docker-superuser box and healing would break it (v2).
        pwkey="${key%_USER}_PASSWORD"          # e.g. AUTHENTIK_DB_USER → AUTHENTIK_DB_PASSWORD
        svc_pw=$(read_env_value ".env" "$pwkey")
        if [ "$DRY_RUN" = "true" ]; then
            if _db_role_authenticates "$example_val" "$svc_pw"; then
                print_info "  Would heal empty ${key} → ${example_val} (role authenticates)"
            else
                print_info "  Would skip ${key} — role '${example_val}' does not authenticate (docker-superuser fallback preserved)"
            fi
            continue
        fi
        if _db_role_authenticates "$example_val" "$svc_pw"; then
            update_env_value ".env" "$key" "$example_val"
            print_substep "  Healed empty ${key} → ${example_val} (role authenticates)"
            ((healed++)) || true
        else
            print_substep "  Skipped ${key} — role '${example_val}' does not authenticate; left empty (global-superuser fallback)"
            ((skipped++)) || true
        fi
    done < <(grep -oE '^[A-Z0-9_]+_DB_USER=' "config/.env.example" | sed 's/=$//')

    if [ "$DRY_RUN" != "true" ]; then
        if [ "$healed" -gt 0 ] || [ "$skipped" -gt 0 ]; then
            print_success "DB-user reconcile: ${healed} healed, ${skipped} left as global-superuser fallback."
        else
            print_substep "All *_DB_USER values already populated — nothing to heal."
        fi
    fi
}

# ------------------------------------------------------------------------------
# #154 (v3 — OWNERSHIP): reconcile per-service DB *table ownership*.
#
# reconcile_service_db_users (above) fixes which ROLE a service CONNECTS as. But
# on a box first provisioned before per-service roles existed, the tables were
# CREATED by the global `docker` superuser (empty→docker fallback). After the
# *_DB_USER is (correctly) pointed at the per-service role, the service can
# CONNECT but not ALTER its own tables → a migration that adds a column dies with
# `InsufficientPrivilegeError: must be owner of table ...` (onyx-api crash-loop
# on prod 8.246, 2026-07; also paperless).
#
# `REASSIGN OWNED BY docker TO <role>` is REFUSED (docker is the bootstrap
# superuser — "objects required by the database system"), so we reassign each
# object in the service DB's public schema with a targeted ALTER … OWNER TO.
# Tables go FIRST so their composite row-types move with them; the TYPE loop then
# only sees standalone composites (ALTER TYPE on a table's row-type would error).
# Idempotent: only objects NOT already owned by the role are touched → a no-op on
# a healthy box (owner == role) and on a docker-superuser box (skipped: *_DB_USER
# empty → still connects as docker, which already owns everything). Runs right
# after reconcile_service_db_users so a just-healed *_DB_USER is picked up.
reconcile_service_db_ownership() {
    print_step "Reconciling per-service DB table ownership (#154)..."

    if [ ! -f "config/.env.example" ]; then
        print_substep "No .env.example template — skipping DB-ownership reconcile."
        return 0
    fi
    command -v docker >/dev/null 2>&1 || { print_substep "docker unavailable — skipping."; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres || {
        print_substep "postgres container not running — skipping DB-ownership reconcile."; return 0; }

    local superuser superpw
    superuser=$(read_env_value ".env" "POSTGRES_USER"); [ -n "$superuser" ] || superuser="docker"
    superpw=$(read_env_value ".env" "POSTGRES_PASSWORD")

    local reassigned=0 checked=0
    local userkey dbkey role dbname misowned sqlf
    while IFS= read -r userkey; do
        [ -n "$userkey" ] || continue
        role=$(read_env_value ".env" "$userkey")
        [ -n "$role" ] || continue                       # docker-superuser box → skip
        [ "$role" = "$superuser" ] && continue           # never "reassign to" the superuser
        printf '%s' "$role" | grep -qE '^[a-zA-Z_][a-zA-Z0-9_]*$' || continue

        dbkey="${userkey%_USER}"                          # ONYX_DB_USER -> ONYX_DB
        dbname=$(read_env_value ".env" "$dbkey")
        [ -n "$dbname" ] || dbname=$(read_env_value "config/.env.example" "$dbkey")
        [ -n "$dbname" ] || continue
        printf '%s' "$dbname" | grep -qE '^[a-zA-Z_][a-zA-Z0-9_]*$' || continue

        # DB + role must exist, else the ALTERs error. Connect to the maintenance
        # `postgres` DB explicitly — without -d, psql targets a DB named after the
        # superuser (e.g. "docker"), which does not exist → the check would always
        # fail and the function would silently skip every service.
        docker exec -e PGPASSWORD="$superpw" postgres psql -U "$superuser" -h 127.0.0.1 -d postgres \
            -tAc "SELECT 1 FROM pg_database WHERE datname='${dbname}'" 2>/dev/null | grep -q 1 || continue
        docker exec -e PGPASSWORD="$superpw" postgres psql -U "$superuser" -h 127.0.0.1 -d postgres \
            -tAc "SELECT 1 FROM pg_roles WHERE rolname='${role}'" 2>/dev/null | grep -q 1 || continue
        ((checked++)) || true

        misowned=$(docker exec -e PGPASSWORD="$superpw" postgres psql -U "$superuser" -h 127.0.0.1 -d "$dbname" -tAc \
            "SELECT (SELECT count(*) FROM pg_tables    WHERE schemaname='public' AND tableowner    <> '${role}')
                  + (SELECT count(*) FROM pg_views     WHERE schemaname='public' AND viewowner     <> '${role}')
                  + (SELECT count(*) FROM pg_sequences WHERE schemaname='public' AND sequenceowner <> '${role}')" 2>/dev/null | tr -d '[:space:]')
        [ -n "$misowned" ] || misowned=0
        if [ "$misowned" = "0" ]; then
            print_substep "  ${dbname}: all public objects already owned by ${role} — ok."
            continue
        fi
        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would reassign ${misowned}+ mis-owned objects in ${dbname} → ${role}"
            continue
        fi

        sqlf=$(mktemp)
        sed "s/__ROLE__/${role}/g" > "$sqlf" <<'SQL'
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT quote_ident(tablename) AS q FROM pg_tables
           WHERE schemaname='public' AND tableowner <> '__ROLE__'
  LOOP EXECUTE 'ALTER TABLE public.' || r.q || ' OWNER TO "__ROLE__"'; END LOOP;

  FOR r IN SELECT quote_ident(sequencename) AS q FROM pg_sequences
           WHERE schemaname='public' AND sequenceowner <> '__ROLE__'
  LOOP EXECUTE 'ALTER SEQUENCE public.' || r.q || ' OWNER TO "__ROLE__"'; END LOOP;

  FOR r IN SELECT quote_ident(viewname) AS q FROM pg_views
           WHERE schemaname='public' AND viewowner <> '__ROLE__'
  LOOP EXECUTE 'ALTER VIEW public.' || r.q || ' OWNER TO "__ROLE__"'; END LOOP;

  FOR r IN SELECT quote_ident(matviewname) AS q FROM pg_matviews
           WHERE schemaname='public' AND matviewowner <> '__ROLE__'
  LOOP EXECUTE 'ALTER MATERIALIZED VIEW public.' || r.q || ' OWNER TO "__ROLE__"'; END LOOP;

  FOR r IN SELECT quote_ident(t.typname) AS q
           FROM pg_type t
           JOIN pg_namespace ns ON ns.oid = t.typnamespace
           JOIN pg_roles o ON o.oid = t.typowner
           WHERE ns.nspname='public' AND o.rolname <> '__ROLE__' AND t.typtype IN ('e','c','d')
  LOOP EXECUTE 'ALTER TYPE public.' || r.q || ' OWNER TO "__ROLE__"'; END LOOP;
END $$;
SQL
        docker cp "$sqlf" postgres:/tmp/rzfz-reassign-owner.sql >/dev/null 2>&1
        if docker exec -e PGPASSWORD="$superpw" postgres \
              psql -U "$superuser" -h 127.0.0.1 -d "$dbname" -v ON_ERROR_STOP=1 \
              -f /tmp/rzfz-reassign-owner.sql >/dev/null 2>&1; then
            print_substep "  Reassigned ${dbname} public objects → ${role} (${misowned} were mis-owned)."
            ((reassigned++)) || true
        else
            print_warning "  Ownership reassign FAILED for ${dbname} → ${role} (left as-is; investigate)."
        fi
        docker exec postgres rm -f /tmp/rzfz-reassign-owner.sql >/dev/null 2>&1 || true
        rm -f "$sqlf"
    done < <(grep -oE '^[A-Z0-9_]+_DB_USER=' "config/.env.example" | sed 's/=$//')

    if [ "$DRY_RUN" != "true" ]; then
        if [ "$reassigned" -gt 0 ]; then
            print_success "DB-ownership reconcile: ${reassigned} database(s) reassigned to their service role."
        else
            print_substep "DB-ownership reconcile: nothing to do (${checked} service DB(s) already correctly owned)."
        fi
    fi
}

# ==============================================================================
# Dify Environment Sync
# ==============================================================================
sync_dify_env() {
    print_step "Synchronizing Dify environment..."

    if [ ! -f ".env.dify" ]; then
        print_substep "No .env.dify found — Dify not configured. Skipping."
        return 0
    fi

    if [ ! -f "config/.env.dify.example" ]; then
        print_substep "No .env.dify.example template. Skipping Dify sync."
        return 0
    fi

    local added=0
    local removed=0

    # Detect new keys in .env.dify.example not in .env.dify
    while IFS= read -r line; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue

        local key="${line%%=*}"
        # Validate key is a valid env var name
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

        if ! grep -q "^${key}=" ".env.dify" 2>/dev/null; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would add ${key} to .env.dify"
            else
                echo "$line" >> ".env.dify"
                print_substep "  Added new Dify variable: ${key}"
                ((added++)) || true
            fi
        fi
    done < "config/.env.dify.example"

    # Detect removed keys (in .env.dify but not in .env.dify.example)
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue

        local key="${line%%=*}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

        if ! grep -q "^${key}=" "config/.env.dify.example" 2>/dev/null; then
            print_warning "  Dify variable ${key} exists in .env.dify but not in template (may be obsolete)"
            ((removed++)) || true
        fi
    done < ".env.dify"

    # Sync shared secrets from .env to .env.dify
    if [ "$DRY_RUN" != "true" ]; then
        local valkey_pw
        valkey_pw=$(read_env_value ".env" "VALKEY_PASSWORD")
        if [ -n "$valkey_pw" ]; then
            local current_redis_pw
            current_redis_pw=$(read_env_value ".env.dify" "REDIS_PASSWORD")
            if [ "$current_redis_pw" != "$valkey_pw" ]; then
                update_env_value ".env.dify" "REDIS_PASSWORD" "$valkey_pw"
                update_env_value ".env.dify" "CELERY_BROKER_URL" "redis://:${valkey_pw}@valkey:6379/1"
                print_substep "  Synced VALKEY_PASSWORD → REDIS_PASSWORD in .env.dify"
            fi
        fi

        # Sync plugin keys
        for key in PLUGIN_DAEMON_KEY PLUGIN_DIFY_INNER_API_KEY; do
            local env_val dify_val
            env_val=$(read_env_value ".env" "$key")
            dify_val=$(read_env_value ".env.dify" "$key")
            if [ -n "$env_val" ] && [ "$env_val" != "$dify_val" ]; then
                update_env_value ".env.dify" "$key" "$env_val"
                print_substep "  Synced ${key} from .env → .env.dify"
            fi
        done
    fi

    print_success "Dify env sync: ${added} added, ${removed} potentially obsolete."
}

# ==============================================================================
# Generate Missing Secrets (runs after env migration)
# ==============================================================================
# For each secret key, checks if the value in .env is empty. If empty AND the
# corresponding profile is enabled (or it's a core secret), generates it.
# Uses the same generation methods as razzfazz-init.sh.
# ==============================================================================

# Thin re-exports under the historical *_upgrade names so the 71 existing
# call sites stay byte-identical. The bodies are provided by lib.sh's
# generate_secret / generate_password / generate_hex_secret with identical
# semantics (openssl rand under the hood).
generate_secret_upgrade()     { generate_secret "$@"; }
generate_password_upgrade()   { generate_password "$@"; }
generate_hex_secret_upgrade() { generate_hex_secret "$@"; }

# Check if a profile is active in COMPOSE_PROFILES
profile_active() {
    local profile="$1"
    local profiles
    profiles=$(read_env_value ".env" "COMPOSE_PROFILES")
    echo ",$profiles," | grep -q ",$profile,"
}

# Returns true if the key is empty or unset in .env
secret_is_empty() {
    local key="$1"
    local val
    val=$(read_env_value ".env" "$key")
    [ -z "$val" ]
}

generate_missing_secrets() {
    local dry_run="${1:-false}"
    local action_verb="Generating"
    [ "$dry_run" = "true" ] && action_verb="Would generate"

    if [ "$dry_run" = "true" ]; then
        print_step "Checking for missing secrets..."
    else
        print_step "Generating missing secrets..."
    fi

    local generated=0

    # ── Secret generation table ──
    # Format: generate_if_missing KEY GENERATOR_CMD CONDITION
    # CONDITION: "core" (always), or a profile name
    #
    # Helper to check and generate
    _gen_if_empty() {
        local key="$1"
        local gen_cmd="$2"
        local condition="$3"  # "core" or profile name

        if ! secret_is_empty "$key"; then
            return
        fi

        # Check profile condition
        if [ "$condition" != "core" ]; then
            if ! profile_active "$condition"; then
                return
            fi
        fi

        if [ "$dry_run" = "true" ]; then
            print_info "  ${action_verb} ${key}"
        else
            local value
            value=$(eval "$gen_cmd")
            update_env_value ".env" "$key" "$value"
            print_substep "  Generated ${key}"
        fi
        ((generated++)) || true
    }

    # ── Core secrets (always required) ──
    _gen_if_empty "WEBUI_SECRET_KEY"           'generate_hex_secret_upgrade 32'      "core"
    _gen_if_empty "AUTHENTIK_SECRET_KEY"        'generate_secret_upgrade 42'          "core"
    _gen_if_empty "AUTHENTIK_BOOTSTRAP_TOKEN"   'generate_secret_upgrade 48'          "core"
    _gen_if_empty "POSTGRES_PASSWORD"           'generate_password_upgrade 24'        "core"
    _gen_if_empty "VALKEY_PASSWORD"             'generate_password_upgrade 24'        "core"
    _gen_if_empty "GPUSTACK_SECRET_KEY"         'generate_secret_upgrade 32'          "core"
    _gen_if_empty "KOMODO_PASSKEY"              'generate_password_upgrade 24'        "core"
    _gen_if_empty "ACME_EMAIL"                  'echo ""'                             "core"  # not a secret, skip

    # Core client secrets (Authentik proxy providers — always on)
    _gen_if_empty "CHAT_CLIENT_SECRET"          'generate_secret_upgrade 64'          "core"
    _gen_if_empty "ADMIN_CLIENT_SECRET"         'generate_secret_upgrade 64'          "core"
    _gen_if_empty "LLM_CLIENT_SECRET"           'generate_secret_upgrade 64'          "core"
    _gen_if_empty "BACKUP_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "core"
    _gen_if_empty "LICENSES_CLIENT_SECRET"      'generate_hex_secret_upgrade 32'      "core"
    _gen_if_empty "SETUP_CLIENT_SECRET"         'generate_hex_secret_upgrade 32'      "core"
    _gen_if_empty "HELP_CLIENT_SECRET"          'generate_hex_secret_upgrade 32'      "core"

    # Core infrastructure secrets
    # F10: BACKUP_ENCRYPTION_PASSWORD defaults to AUTHENTIK_BOOTSTRAP_PASSWORD
    # (the password printed on the sticker). If the stack dir is lost, the
    # operator can still decrypt backups using the well-known admin password.
    # Generating a fresh random value here would orphan the backups when the
    # .env is gone.
    _gen_if_empty "BACKUP_ENCRYPTION_PASSWORD"  'read_env_value .env AUTHENTIK_BOOTSTRAP_PASSWORD' "core"
    _gen_if_empty "SMTP_INTERNAL_PASSWORD"      'generate_password_upgrade 24'        "core"

    # #184/#168 rc1 (security M-1/M-2): mint these opt-in-module secrets on EVERY
    # upgrade (gated "core" = always), not just when their profile is active — so
    # a box that ENABLES observability / mac-llm AFTER upgrading never runs with an
    # empty (fail-open) secret. CLICKHOUSE_PASSWORD auths the ClickHouse `default`
    # user (network-reachable on the docker bridge, holds LLM prompt/response
    # spans); MAC_GATEWAY_MASTER_KEY is the LiteLLM Mac-gateway's only auth.
    _gen_if_empty "CLICKHOUSE_PASSWORD"         'generate_password_upgrade 24'        "core"
    _gen_if_empty "MAC_GATEWAY_MASTER_KEY"      'generate_password_upgrade 32'        "core"

    # Per-service DB passwords (core services):
    # intentionally NOT generated on upgrade — on fresh installs, init-db.sh
    # creates dedicated PostgreSQL users (authentik_user, openwebui_user, …)
    # and razzfazz-init.sh generates these passwords to match; on an upgrade
    # from pre-2026.05, those users don't exist yet and compose always falls
    # back to ${POSTGRES_USER}/${POSTGRES_PASSWORD} (see compose files:
    # ${AUTHENTIK_DB_PASSWORD:-${POSTGRES_PASSWORD}} pattern). Generating here
    # would install a password that doesn't match what postgres stored for
    # the shared 'docker' user, breaking DB auth for every core service.
    # Creating the per-service users is the job of the ga.6 DB-user migration
    # in run_data_migrations(); once that migration is extended to cover these
    # core services, re-enable the generators below.
    #_gen_if_empty "AUTHENTIK_DB_PASSWORD"        'generate_password_upgrade 24'       "core"
    #_gen_if_empty "OPENWEBUI_DB_PASSWORD"        'generate_password_upgrade 24'       "core"
    #_gen_if_empty "GPUSTACK_DB_PASSWORD"         'generate_password_upgrade 24'       "core"

    # Per-service admin passwords (core services)
    _gen_if_empty "GPUSTACK_ADMIN_PASSWORD"      'generate_password_upgrade 24'       "core"
    _gen_if_empty "KOMODO_INIT_ADMIN_PASSWORD"   'generate_password_upgrade 24'       "core"

    # ── Chat profile ──
    _gen_if_empty "PIPELINES_API_KEY"           'generate_secret_upgrade 32'          "chat"

    # ── Dify profile ──
    _gen_if_empty "DIFY_CLIENT_SECRET"          'generate_secret_upgrade 64'          "dify"
    _gen_if_empty "PLUGIN_DAEMON_KEY"           'generate_secret_upgrade 42'          "dify"
    _gen_if_empty "PLUGIN_DIFY_INNER_API_KEY"   'generate_secret_upgrade 42'          "dify"
    _gen_if_empty "SANDBOX_API_KEY"             'generate_hex_secret_upgrade 32'      "dify"
    # DIFY_DB_PASSWORD / DIFY_PLUGIN_DB_PASSWORD — NOT generated on upgrade
    # (same reason as AUTHENTIK_DB_PASSWORD above — compose falls back to
    # POSTGRES_PASSWORD; generating would break DB auth until the ga.6
    # per-service-user migration is extended).
    #_gen_if_empty "DIFY_DB_PASSWORD"            'generate_password_upgrade 24'        "dify"
    #_gen_if_empty "DIFY_PLUGIN_DB_PASSWORD"     'generate_password_upgrade 24'        "dify"
    _gen_if_empty "DIFY_ADMIN_PASSWORD"         'generate_password_upgrade 24'        "dify"

    # ── Gitea profile ──
    _gen_if_empty "GITEA_SECRET_KEY"            'generate_hex_secret_upgrade 32'      "gitea"
    _gen_if_empty "GITEA_INTERNAL_TOKEN"        'generate_secret_upgrade 64'          "gitea"
    _gen_if_empty "GITEA_CLIENT_SECRET"         'generate_hex_secret_upgrade 32'      "gitea"
    # GITEA_DB_PASSWORD — NOT generated on upgrade (same reason as
    # AUTHENTIK_DB_PASSWORD). compose uses ${GITEA_DB_PASSWORD:-${POSTGRES_PASSWORD}}.
    #_gen_if_empty "GITEA_DB_PASSWORD"           'generate_password_upgrade 24'        "gitea"

    # ── LightRAG profile ──
    _gen_if_empty "LIGHTRAG_API_KEY"            'generate_secret_upgrade 32'          "lightrag"
    _gen_if_empty "LIGHTRAG_TOKEN_SECRET"       'generate_secret_upgrade 32'          "lightrag"
    _gen_if_empty "LIGHTRAG_CLIENT_SECRET"      'generate_hex_secret_upgrade 32'      "lightrag"

    # ── Cognee profile ──
    _gen_if_empty "COGNEE_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "cognee"
    _gen_if_empty "COGNEE_ADMIN_PASSWORD"       'generate_password_upgrade 24'        "cognee"
    _gen_if_empty "FALKORDB_PASSWORD"           'generate_secret_upgrade 32'          "cognee"

    # ── Docling profile ──
    _gen_if_empty "DOCLING_CLIENT_SECRET"       'generate_hex_secret_upgrade 32'      "docling"

    # ── Stirling-PDF profile ──
    _gen_if_empty "STIRLING_CLIENT_SECRET"      'generate_hex_secret_upgrade 32'      "stirling-pdf"

    # ── Crawl4AI profile ── (#191) forward_auth proxy-provider client_secret;
    # previously minted nowhere, so the provider applied with an empty secret.
    _gen_if_empty "CRAWL4AI_CLIENT_SECRET"      'generate_hex_secret_upgrade 32'      "crawl4ai"

    # ── Paperclip profile ──
    _gen_if_empty "PAPERCLIP_DB_PASSWORD"       'generate_hex_secret_upgrade 16'      "paperclip"
    _gen_if_empty "PAPERCLIP_BETTER_AUTH_SECRET" 'generate_hex_secret_upgrade 32'     "paperclip"
    _gen_if_empty "PAPERCLIP_CLIENT_SECRET"     'generate_hex_secret_upgrade 32'      "paperclip"

    # ── Matrix profile ──
    _gen_if_empty "SYNAPSE_DB_PASSWORD"         'generate_hex_secret_upgrade 16'      "matrix"
    _gen_if_empty "SYNAPSE_REGISTRATION_SHARED_SECRET" 'generate_hex_secret_upgrade 32' "matrix"
    _gen_if_empty "SYNAPSE_MACAROON_SECRET_KEY" 'generate_hex_secret_upgrade 32'      "matrix"
    _gen_if_empty "SYNAPSE_FORM_SECRET"         'generate_hex_secret_upgrade 16'      "matrix"
    _gen_if_empty "SYNAPSE_CLIENT_SECRET"       'generate_hex_secret_upgrade 32'      "matrix"
    _gen_if_empty "MATRIX_CLIENT_ID"            'generate_hex_secret_upgrade 16'      "matrix"
    _gen_if_empty "ELEMENT_WEB_CLIENT_SECRET"   'generate_hex_secret_upgrade 32'      "matrix"

    # ── Moltis profile ──
    _gen_if_empty "MOLTIS_PASSWORD"             'generate_hex_secret_upgrade 16'      "moltis"
    _gen_if_empty "MOLTIS_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "moltis"

    # ── Hermes profile ──
    _gen_if_empty "HERMES_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "hermes"

    # ── Paperless-ngx profile ──
    _gen_if_empty "PAPERLESS_SECRET_KEY"        'generate_secret_upgrade 64'          "paperless-ngx"
    _gen_if_empty "PAPERLESS_CLIENT_SECRET"     'generate_hex_secret_upgrade 32'      "paperless-ngx"
    _gen_if_empty "PAPERLESS_DB_PASSWORD"       'generate_password_upgrade 24'        "paperless-ngx"

    # ── Vaultwarden profile ──
    # VAULTWARDEN_ADMIN_TOKEN is intentionally NOT backfilled — empty = /admin panel
    # disabled by default. Re-generating it here would silently RE-ENABLE the shared-secret
    # /admin backdoor on every upgrade (and clobber an operator's deliberate disable).
    # Operators who want /admin set it manually (preferably an Argon2 `vaultwarden hash`).
    _gen_if_empty "VAULTWARDEN_CLIENT_SECRET"   'generate_hex_secret_upgrade 32'      "vaultwarden"

    # ── Infisical profile ──
    _gen_if_empty "INFISICAL_ENCRYPTION_KEY"    'generate_hex_secret_upgrade 16'      "infisical"
    _gen_if_empty "INFISICAL_AUTH_SECRET"       'generate_secret_upgrade 42'          "infisical"
    _gen_if_empty "INFISICAL_CLIENT_SECRET"     'generate_hex_secret_upgrade 32'      "infisical"
    _gen_if_empty "INFISICAL_DB_PASSWORD"       'generate_password_upgrade 24'        "infisical"

    # ── Onyx profile ──
    _gen_if_empty "ONYX_SECRET"                 'generate_secret_upgrade 42'          "onyx"
    _gen_if_empty "ONYX_CLIENT_SECRET"          'generate_hex_secret_upgrade 32'      "onyx"
    _gen_if_empty "ONYX_DB_PASSWORD"            'generate_password_upgrade 24'        "onyx"

    # ── OpenHands profile ──
    _gen_if_empty "OPENHANDS_CLIENT_SECRET"     'generate_hex_secret_upgrade 32'      "openhands"

    # ── Coding Tools profile ──
    _gen_if_empty "CODING_TOOLS_CLIENT_SECRET"  'generate_hex_secret_upgrade 32'      "coding-tools"

    # ── Agent Manager profile ──
    _gen_if_empty "AGENTS_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "agents"
    # C1 bypass fix (PR #84 re-review): "came through Caddy" proof secret shared
    # only Caddy↔agent-manager (X-Razzfazz-Proxy-Proof). Empty => the manager's
    # per-instance proxy fails CLOSED (refuses every agent-subdomain request), so
    # an upgraded agents-enabled box that never ran init MUST get it minted here.
    _gen_if_empty "MANAGER_PROXY_SECRET"        'generate_hex_secret_upgrade 32'      "agents"

    # ── MCP Manager profile (#36 / #61 NEW-3) ──
    # MCP_INTERNAL_TOKEN gates mcp-manager's /internal/* (agent-manager presents
    # it to fetch per-user MCP wiring at agent launch). Empty => /internal fails
    # closed (401) => the per-proxy bearer never reaches the user's agents. Only
    # init.sh minted these before; an UPGRADED mcp-enabled box never ran init's
    # secret regeneration, so they stayed empty. Mint-if-empty on upgrade too.
    # MCP_MANAGER_SECRET_KEY = base64(32 bytes) = a valid AES-256-GCM master key
    # (same `generate_secret 32` init uses; do NOT rotate an existing one — that
    # would orphan every already-encrypted credential, which secret_is_empty
    # guards against). The matching mint-if-absent self-heal also lives in
    # cli/post-install.sh ensure_mcp_manager_secret().
    _gen_if_empty "MCP_MANAGER_SECRET_KEY"      'generate_secret_upgrade 32'          "mcp"
    _gen_if_empty "MCP_MANAGER_DB_PASSWORD"     'generate_password_upgrade 24'        "mcp"
    _gen_if_empty "MCP_INTERNAL_TOKEN"          'generate_hex_secret_upgrade 32'      "mcp"
    _gen_if_empty "MCP_CLIENT_SECRET"           'generate_hex_secret_upgrade 32'      "mcp"

    # ── Monitor profile ──
    # (KOMODO_INIT_ADMIN_PASSWORD already handled as core above since Komodo is monitor profile)

    if [ $generated -eq 0 ]; then
        print_substep "All secrets are already populated."
    elif [ "$dry_run" = "true" ]; then
        print_info "${generated} secret(s) would be generated."
    else
        print_success "${generated} missing secret(s) generated."

        # Sync shared secrets to .env.dify if it exists
        if [ -f ".env.dify" ]; then
            local valkey_pw
            valkey_pw=$(read_env_value ".env" "VALKEY_PASSWORD")
            if [ -n "$valkey_pw" ]; then
                update_env_value ".env.dify" "REDIS_PASSWORD" "$valkey_pw"
                update_env_value ".env.dify" "CELERY_BROKER_URL" "redis://:${valkey_pw}@valkey:6379/1"
            fi
            for key in PLUGIN_DAEMON_KEY PLUGIN_DIFY_INNER_API_KEY SANDBOX_API_KEY; do
                local env_val
                env_val=$(read_env_value ".env" "$key")
                if [ -n "$env_val" ]; then
                    update_env_value ".env.dify" "$key" "$env_val"
                fi
            done
            local dify_db_user dify_db_pass
            dify_db_user=$(read_env_value ".env" "DIFY_DB_USER")
            dify_db_pass=$(read_env_value ".env" "DIFY_DB_PASSWORD")
            if [ -n "$dify_db_user" ] && [ -n "$dify_db_pass" ]; then
                update_env_value ".env.dify" "DB_USERNAME" "$dify_db_user"
                update_env_value ".env.dify" "DB_PASSWORD" "$dify_db_pass"
            fi
            local dify_admin_pass
            dify_admin_pass=$(read_env_value ".env" "DIFY_ADMIN_PASSWORD")
            if [ -n "$dify_admin_pass" ]; then
                update_env_value ".env.dify" "INIT_PASSWORD" "$dify_admin_pass"
            fi
        fi
    fi
}

# ==============================================================================
# Build & Pull Docker Images
# ==============================================================================
build_and_pull() {
    print_step "Updating Docker images..."

    # rc6.10 fix: REQUIRES_BUILD/_PULL are populated from migrations/env-changes.json's
    # `needs_build`/`needs_pull` flags by migrate_env(). A release that ships new
    # source for a locally-built image (Caddy, start-portal, setup, help, licenses,
    # backup, dify-web, llm-gpustack-vulkan, …) WITHOUT bumping the
    # migration manifest will silently keep stale code in containers. Caught
    # 2026-05-08 on 0.91 (M028-S05 upgrade) where start-portal kept old app.py
    # because no migration entry mentioned it. Heuristic: if HEAD actually moved
    # during this upgrade, force a rebuild + repull. Build cache hits make the
    # cost trivial when nothing changed; the cost of skipping when something
    # DID change is silent code drift.
    if [ -n "${INSTALLED_COMMIT:-}" ] && [ -n "${TARGET_COMMIT:-}" ] \
       && [ "${INSTALLED_COMMIT}" != "${TARGET_COMMIT}" ] \
       && [ "${INSTALLED_COMMIT}" != "unknown" ] \
       && [ "${TARGET_COMMIT}" != "unknown" ]; then
        if [ "${REQUIRES_BUILD:-false}" != true ]; then
            print_substep "Commit moved (${INSTALLED_COMMIT} → ${TARGET_COMMIT}); forcing image rebuild even though no manifest entry requested it."
            REQUIRES_BUILD=true
            REQUIRES_PULL=true
        fi
    fi

    if razzfazz_is_offline; then
        # #184 WS2b: an air-gapped box obtains every image from the offline package
        # (loaded by code_update_package). A `docker compose build` here would try
        # to pull base images + apt/pip/npm → firewall trip. Skip it; verify-images
        # (in restart_stack) is the hard check that the loaded set is complete.
        print_substep "OFFLINE (RAZZFAZZ_NETWORK_MODE=offline): skipping image build — images come from the offline package."
    elif [ "${REQUIRES_BUILD:-true}" = true ] || [ "${FORCE_BUILD:-false}" = true ]; then
        print_substep "Building custom images..."
        # #185: a single transient network blip during build (e.g. apt-get
        # 'Could not resolve deb.debian.org' / pip connection reset — common on
        # WiFi boxes like 0.208) used to fail the build and ROLL BACK the whole
        # upgrade. Retry the build a few times before giving up; BuildKit reuses
        # completed layers so retries are cheap.
        local _build_ok=false _attempt
        # #184 WS2a: strip the no-build overlay so the build: contexts are present
        # for this ONE intended build (ensure_nobuild_overlay composed it above,
        # before build_and_pull, so restart_stack's `up` stays build-proof).
        local _build_cf; _build_cf="$(compose_file_for_build)"
        for _attempt in 1 2 3; do
            if COMPOSE_FILE="$_build_cf" docker compose build --parallel 2>&1; then
                _build_ok=true
                break
            fi
            if [ "$_attempt" -lt 3 ]; then
                print_warning "Docker build failed (attempt ${_attempt}/3) — likely transient network; retrying in 20s..."
                sleep 20
            fi
        done
        if [ "$_build_ok" != true ]; then
            print_error "Docker build failed after 3 attempts."
            return 1
        fi
        print_substep "Custom images built."
    else
        print_substep "No rebuild required for this upgrade."
    fi

    if [ "${SKIP_PULL:-false}" = true ]; then
        print_substep "Skipping image pull (images loaded from package)."
    elif razzfazz_is_offline; then
        # #184 WS2b: never reach a registry on an air-gapped box.
        print_substep "OFFLINE (RAZZFAZZ_NETWORK_MODE=offline): skipping image pull — images come from the offline package."
    elif [ "${REQUIRES_PULL:-true}" = true ] || [ "${FORCE_BUILD:-false}" = true ]; then
        print_substep "Pulling pre-built images..."
        docker compose pull --ignore-pull-failures 2>&1 || \
            print_warning "Some images failed to pull."
        print_substep "Pre-built images updated."
    else
        print_substep "No pull required for this upgrade."
    fi

    print_success "Docker images updated."
}

# ==============================================================================
# Blueprint Sync — DISABLED for upgrades
# ==============================================================================
# Blueprints are declarative and would overwrite any manual changes made
# in the Authentik Admin UI (branding, flows, providers, groups, roles, etc.).
# They are only applied during first-time initialization (init-authentik.sh).
#
# For upgrade-time changes to Authentik objects, use run_data_migrations()
# with targeted Django ORM updates via 'docker exec authentik-worker'.
# ==============================================================================

# ==============================================================================
# Data Migrations (version-gated, runs in authentik-worker)
# ==============================================================================
# Targeted DB changes that must NOT be done via blueprint re-application,
# because blueprints are declarative and would overwrite manual UI changes.
# Each migration checks the installed version and only runs if applicable.
# ==============================================================================
run_data_migrations() {
    print_step "Running data migrations..."

    local migrations_run=0

    # --- Migration: 2026-03.GA — Set access_token_validity on all proxy providers ---
    if version_lt "$INSTALLED_VERSION" "2026-03.GA" 2>/dev/null || \
       version_lt "$INSTALLED_VERSION" "2026.03-GA" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would set access_token_validity=hours=24 on all proxy providers"
            ((migrations_run++)) || true
        else
            print_substep "Setting access_token_validity=hours=24 on proxy providers..."

            # Wait for authentik-worker to be healthy and ready
            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
                if [ $((retries % 6)) -eq 0 ]; then
                    print_substep "  Waiting for authentik-worker... (${worker_health}, $((retries * 5))s)"
                fi
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker did not become healthy. Skipping token validity migration."
                print_info "  Run manually: rzfz upgrade --check (shows pending migrations)"
            else
                cat <<'MIGRATION_EOF' > /tmp/razzfazz-migration-token-validity.py
import os, sys, time, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from django.db import OperationalError, connection
from authentik.providers.proxy.models import ProxyProvider


def _save_with_deadlock_retry(obj, **kwargs):
    # Saving a ProxyProvider fires Authentik's outpost permission rebuild
    # (outpost_related_post_save -> build_user_permissions ->
    # remove_all_perms_from_managed_role -> DELETE on guardian_rolemodelpermission).
    # Running this migration right after a MAJOR-version Authentik restart, that
    # same rebuild is happening concurrently in the worker, so a transient Postgres
    # deadlock on guardian_rolemodelpermission is possible (observed on the
    # 2026.04-ga.1 -> 2026.07 upgrade — it aborted the whole run under set -e).
    # Retry the save a few times on a transient deadlock / serialization failure.
    for attempt in range(6):
        try:
            obj.save(**kwargs)
            return
        except OperationalError as exc:
            msg = str(exc).lower()
            if "deadlock" not in msg and "could not serialize" not in msg:
                raise
            connection.close()  # drop the aborted transaction/connection
            time.sleep(1.5 * (attempt + 1))
    # Last attempt: let it raise if it still deadlocks so the caller sees it.
    obj.save(**kwargs)


updated = 0
for provider in ProxyProvider.objects.all():
    if provider.access_token_validity != "hours=24":
        old_val = provider.access_token_validity
        provider.access_token_validity = "hours=24"
        _save_with_deadlock_retry(provider, update_fields=["access_token_validity"])
        print(f"  Updated {provider.name}: {old_val} -> hours=24")
        updated += 1
    else:
        print(f"  {provider.name}: already hours=24")

print(f"\nDone: {updated} provider(s) updated.")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-token-validity.py authentik-worker:/tmp/razzfazz-migration-token-validity.py 2>/dev/null; then
                    # Non-fatal (`if PIPELINE; then`): this migration saves EVERY proxy
                    # provider and each save fires Authentik's outpost permission
                    # rebuild — which can deadlock on guardian_rolemodelpermission
                    # against the worker's own post-restart reconcile (the Python
                    # above retries; this branch is defence-in-depth). Under
                    # `set -eo pipefail` an un-guarded `python ... | while` pipeline
                    # would abort the ENTIRE upgrade on that deadlock (observed on the
                    # 2026.04-ga.1 -> 2026.07 run — verify + provisioning never ran).
                    # The other Authentik migrations below already tolerate it via
                    # their `python ... && rm` (`&&`-short-circuit) form.
                    if docker exec authentik-worker python /tmp/razzfazz-migration-token-validity.py 2>&1 | while IFS= read -r line; do
                        print_substep "$line"
                    done; then
                        ((migrations_run++)) || true
                    else
                        print_warning "Token-validity migration reported an error (non-fatal) — some proxy providers may keep their prior access-token validity. Re-run later: rzfz upgrade"
                    fi
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-token-validity.py 2>/dev/null || true
                else
                    print_warning "Could not reach authentik-worker for token validity migration."
                fi
                rm -f /tmp/razzfazz-migration-token-validity.py
            fi
        fi
    fi

    # --- Migration: 2026.04-rc2 — Create cognee_db if cognee profile is enabled ---
    if version_lt "$INSTALLED_VERSION" "2026.04-rc2" 2>/dev/null; then
        local cognee_db
        cognee_db=$(read_env_value .env COGNEE_DB)

        if [ -n "$cognee_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "cognee"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$cognee_db' with pgvector + uuid-ossp extensions"
                ((migrations_run++)) || true
            else
                print_substep "Creating Cognee database '$cognee_db'..."
                local pg_user
                pg_user=$(read_env_value .env POSTGRES_USER)
                # CREATE DATABASE is idempotent-safe via IF NOT EXISTS pattern
                docker exec postgres psql -U "${pg_user:-docker}" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$cognee_db'" 2>/dev/null | grep -q "exists" || {
                    docker exec postgres psql -U "${pg_user:-docker}" -d postgres \
                        -c "CREATE DATABASE \"$cognee_db\";" 2>/dev/null
                    docker exec postgres psql -U "${pg_user:-docker}" -d "$cognee_db" \
                        -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";" 2>/dev/null
                    print_substep "Database '$cognee_db' created with extensions."
                }
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Add future migrations here, gated by version_lt checks ---

    # --- Migration: 2026.04-ga.2 — Restructure Authentik app groups ---
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.2" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would restructure Authentik app groups (1 Use / 3 Development & APIs / 4 razzfazz.ai Admin & Tools)"
            ((migrations_run++)) || true
        else
            print_substep "Restructuring Authentik application groups..."

            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker not healthy. Skipping app group migration."
            else
                cat <<'MIGRATION_EOF' > /tmp/razzfazz-migration-app-groups.py
import os, sys, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application

GROUP_MAP = {
    # 1 Use
    "chat":                "1 Use",
    "workflow-automation": "1 Use",
    "lightrag":            "1 Use",
    "llm-management":      "1 Use",
    "stirling-pdf":        "1 Use",
    # 3 Development & APIs
    "gitea":               "3 Development & APIs",
    "docling":             "3 Development & APIs",
    "cognee":              "3 Development & APIs",
    # 4 razzfazz.ai Admin & Tools
    "administration":      "4 razzfazz.ai Admin & Tools",
    "backup":              "4 razzfazz.ai Admin & Tools",
    "help":                "4 razzfazz.ai Admin & Tools",
    "setup":               "4 razzfazz.ai Admin & Tools",
    "licenses":            "4 razzfazz.ai Admin & Tools",
}

updated = 0
for slug, new_group in GROUP_MAP.items():
    try:
        app = Application.objects.get(slug=slug)
        if app.meta_launch_url != new_group:  # reuse field name workaround
            pass
        if getattr(app, 'group', None) != new_group:
            old = getattr(app, 'group', '(none)')
            app.group = new_group
            app.save(update_fields=["group"])
            print(f"  {slug}: '{old}' -> '{new_group}'")
            updated += 1
        else:
            print(f"  {slug}: already '{new_group}'")
    except Application.DoesNotExist:
        print(f"  {slug}: not found, skipping")

print(f"\nDone: {updated} app(s) updated.")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-app-groups.py authentik-worker:/tmp/razzfazz-migration-app-groups.py 2>/dev/null; then
                    docker exec authentik-worker \
                        python /tmp/razzfazz-migration-app-groups.py 2>/dev/null && \
                        print_success "App group migration applied." || \
                        print_warning "App group migration failed — run manually if needed."
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-app-groups.py 2>/dev/null || true
                    rm -f /tmp/razzfazz-migration-app-groups.py
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.3 — Create paperclip_db and synapse_db for existing installs ---
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.3" 2>/dev/null; then

        local pg_user
        pg_user=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass
        pg_pass=$(read_env_value .env POSTGRES_PASSWORD)

        # paperclip_db — standard UTF-8
        local paperclip_db
        paperclip_db=$(read_env_value .env PAPERCLIP_DB)
        if [ -n "$paperclip_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "paperclip"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create paperclip_db if missing"
            else
                PGPASSWORD="$pg_pass" docker exec postgres psql -U "$pg_user" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$paperclip_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass" docker exec postgres psql -U "$pg_user" -d postgres \
                        -c "CREATE DATABASE \"$paperclip_db\";" 2>/dev/null
                    print_substep "Database '$paperclip_db' created."
                }
            fi
            ((migrations_run++)) || true
        fi

        # synapse_db — MUST use C locale (non-negotiable for Matrix)
        local synapse_db
        synapse_db=$(read_env_value .env SYNAPSE_DB)
        if [ -n "$synapse_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "matrix"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create synapse_db with C locale if missing"
            else
                PGPASSWORD="$pg_pass" docker exec postgres psql -U "$pg_user" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$synapse_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass" docker exec postgres psql -U "$pg_user" -d postgres \
                        -c "CREATE DATABASE \"$synapse_db\" ENCODING 'UTF8' LC_COLLATE='C' LC_CTYPE='C' TEMPLATE template0;" 2>/dev/null
                    print_substep "Database '$synapse_db' created (C locale for Matrix)."
                }
            fi
            ((migrations_run++)) || true
        fi
    fi

    # --- Migration: 2026.04-ga.4 — Add agentic AI apps to Authentik (hermes, moltis, paperclip, element-web) ---
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.4" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would create Hermes/Moltis/Paperclip/Element Web Authentik providers and apps"
            ((migrations_run++)) || true
        else
            print_substep "Adding agentic AI apps to Authentik portal..."

            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker not healthy. Skipping agentic AI app migration."
            else
                cat <<'MIGRATION_EOF' > /tmp/razzfazz-migration-agentic-apps.py
import os, sys, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application

GROUP = "2 Agentic AI"
SLUGS = ["hermes-agent", "moltis", "paperclip", "element-web"]

updated = 0
for slug in SLUGS:
    try:
        app = Application.objects.get(slug=slug)
        if getattr(app, 'group', None) != GROUP:
            app.group = GROUP
            app.save(update_fields=["group"])
            print(f"  {slug}: group set to '{GROUP}'")
            updated += 1
        else:
            print(f"  {slug}: already in '{GROUP}'")
    except Application.DoesNotExist:
        print(f"  {slug}: not found, skipping (will be created by blueprint on fresh install)")

print(f"\nDone: {updated} app(s) updated.")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-agentic-apps.py authentik-worker:/tmp/razzfazz-migration-agentic-apps.py 2>/dev/null; then
                    docker exec authentik-worker python /tmp/razzfazz-migration-agentic-apps.py 2>/dev/null && \
                        print_success "Agentic AI app group migration applied." || \
                        print_warning "Agentic AI app group migration failed — run manually if needed."
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-agentic-apps.py 2>/dev/null || true
                    rm -f /tmp/razzfazz-migration-agentic-apps.py
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.5 — Persona-driven app group taxonomy rename (M007) ---
    # Renames all existing Authentik app groups to the new 5-group persona structure:
    #   "1 Use"                     → "1 Productivity"
    #   "2 Agentic AI"              → "2 AI Assistants"
    #   "3 Development & APIs"      → "3 Development"
    #   "4 razzfazz.ai Admin & Tools" → "5 Administration"
    # Creates "4 Knowledge Engines" for LightRAG and Cognee if not already set.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.5" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would rename Authentik app groups to persona-driven 5-group taxonomy"
            ((migrations_run++)) || true
        else
            print_substep "Renaming Authentik app groups to persona-driven taxonomy..."

            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker not healthy. Skipping app group taxonomy migration."
            else
                cat <<'MIGRATION_EOF' > /tmp/razzfazz-migration-group-taxonomy.py
import os, sys, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application

# Map old group names → new group names
GROUP_RENAME = {
    "1 Use":                       "1 Productivity",
    "2 Agentic AI":                "2 AI Assistants",
    "3 Development & APIs":        "3 Development",
    "4 razzfazz.ai Admin & Tools": "5 Administration",
}

# Explicit overrides for apps that move between groups in the new taxonomy
# (only applied if the app exists; skipped silently if not found)
SLUG_OVERRIDES = {
    # → 1 Productivity
    "chat":             "1 Productivity",
    "workflow-automation": "1 Productivity",
    "stirling-pdf":     "1 Productivity",
    "paperless-ngx":    "1 Productivity",
    "vaultwarden":      "1 Productivity",
    "ai-search":        "1 Productivity",  # onyx slug
    "onyx":             "1 Productivity",
    "element-web":      "1 Productivity",
    # → 2 AI Assistants
    "hermes-agent":     "2 AI Assistants",
    "moltis":           "2 AI Assistants",
    "paperclip":        "2 AI Assistants",
    # → 3 Development
    "gitea":            "3 Development",
    "docling":          "3 Development",
    "cognee":           "3 Development",
    "openhands":        "3 Development",
    "coding-tools":     "3 Development",
    # → 4 Knowledge Engines
    "lightrag":         "4 Knowledge Engines",
    # → 5 Administration
    "administration":   "5 Administration",
    "llm-management":   "5 Administration",
    "backup":           "5 Administration",
    "help":             "5 Administration",
    "setup":            "5 Administration",
    "licenses":         "5 Administration",
    "infisical":        "5 Administration",
}

updated = 0

# Step 1: apply slug-level overrides (highest priority)
for slug, new_group in SLUG_OVERRIDES.items():
    try:
        app = Application.objects.get(slug=slug)
        old = getattr(app, 'group', '(none)')
        if old != new_group:
            app.group = new_group
            app.save(update_fields=["group"])
            print(f"  {slug}: '{old}' -> '{new_group}'")
            updated += 1
        else:
            print(f"  {slug}: already '{new_group}'")
    except Application.DoesNotExist:
        print(f"  {slug}: not installed, skipping")

# Step 2: rename any remaining apps still using old group names (catch-all)
for app in Application.objects.all():
    old = getattr(app, 'group', '') or ''
    new = GROUP_RENAME.get(old)
    if new and app.slug not in SLUG_OVERRIDES:
        app.group = new
        app.save(update_fields=["group"])
        print(f"  {app.slug}: '{old}' -> '{new}' (catch-all rename)")
        updated += 1

print(f"\nDone: {updated} app(s) updated.")

# Step 3: app name renames
NAME_RENAMES = {"onyx": "Search", "hermes-agent": "Hermes"}
for slug, new_name in NAME_RENAMES.items():
    try:
        app = Application.objects.get(slug=slug)
        if app.name != new_name:
            print(f"  {slug}: name '{app.name}' -> '{new_name}'")
            app.name = new_name
            app.save(update_fields=["name"])
    except Application.DoesNotExist:
        pass

# Step 4: remove synapse portal entry (OIDC provider stays)
try:
    Application.objects.get(slug="synapse").delete()
    print("  synapse: portal entry deleted (OIDC provider preserved)")
except Application.DoesNotExist:
    print("  synapse: already removed")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-group-taxonomy.py authentik-worker:/tmp/razzfazz-migration-group-taxonomy.py 2>/dev/null; then
                    docker exec authentik-worker \
                        python /tmp/razzfazz-migration-group-taxonomy.py 2>/dev/null && \
                        print_success "App group taxonomy migration applied." || \
                        print_warning "App group taxonomy migration failed — run manually if needed."
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-group-taxonomy.py 2>/dev/null || true
                    rm -f /tmp/razzfazz-migration-group-taxonomy.py
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.5 — Create M007 databases for existing installs ---
    # paperless_db, vaultwarden_db, infisical_db, onyx_db were added in ga.5 (M007).
    # Installations upgrading from before ga.5 need these databases created.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.5" 2>/dev/null; then

        local pg_user_m007
        pg_user_m007=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass_m007
        pg_pass_m007=$(read_env_value .env POSTGRES_PASSWORD)

        # paperless_db — standard UTF-8
        local paperless_db
        paperless_db=$(read_env_value .env PAPERLESS_DB)
        if [ -n "$paperless_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "paperless-ngx"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$paperless_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$paperless_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                        -c "CREATE DATABASE \"$paperless_db\";" 2>/dev/null
                    print_substep "Database '$paperless_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi

        # vaultwarden_db — standard UTF-8
        local vaultwarden_db
        vaultwarden_db=$(read_env_value .env VAULTWARDEN_DB)
        if [ -n "$vaultwarden_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "vaultwarden"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$vaultwarden_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$vaultwarden_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                        -c "CREATE DATABASE \"$vaultwarden_db\";" 2>/dev/null
                    print_substep "Database '$vaultwarden_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi

        # infisical_db — standard UTF-8
        local infisical_db
        infisical_db=$(read_env_value .env INFISICAL_DB)
        if [ -n "$infisical_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "infisical"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$infisical_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$infisical_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                        -c "CREATE DATABASE \"$infisical_db\";" 2>/dev/null
                    print_substep "Database '$infisical_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi

        # onyx_db — standard UTF-8
        local onyx_db
        onyx_db=$(read_env_value .env ONYX_DB)
        if [ -n "$onyx_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "onyx"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$onyx_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$onyx_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                        -c "CREATE DATABASE \"$onyx_db\";" 2>/dev/null
                    print_substep "Database '$onyx_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.6 — Create agent_manager_db for M011 Agent Manager ---
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.6" 2>/dev/null; then

        local pg_user_m011
        pg_user_m011=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass_m011
        pg_pass_m011=$(read_env_value .env POSTGRES_PASSWORD)

        local agent_manager_db
        agent_manager_db=$(read_env_value .env AGENT_MANAGER_DB)
        if [ -n "$agent_manager_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "agents"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$agent_manager_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m011" docker exec postgres psql -U "$pg_user_m011" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$agent_manager_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m011" docker exec postgres psql -U "$pg_user_m011" -d postgres \
                        -c "CREATE DATABASE \"$agent_manager_db\";" 2>/dev/null
                    print_substep "Database '$agent_manager_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.6 — Create per-service PostgreSQL users (M012 F-009) ---
    # M012 introduced per-service DB users. On fresh installs, init-db.sh creates them.
    # During upgrade, existing installations only have the shared postgres superuser.
    # This migration creates per-service users and grants them ownership of their databases.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.6" 2>/dev/null; then

        local pg_user_perservice
        pg_user_perservice=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass_perservice
        pg_pass_perservice=$(read_env_value .env POSTGRES_PASSWORD)

        # Map: env_var_db env_var_password service_user profile_name
        # Only services that have per-service DB passwords in .env are included.
        local -a db_user_migrations=(
            "PAPERCLIP_DB:PAPERCLIP_DB_PASSWORD:paperclip_user:paperclip"
            "SYNAPSE_DB:SYNAPSE_DB_PASSWORD:synapse_user:matrix"
        )

        for entry in "${db_user_migrations[@]}"; do
            local db_var password_var svc_user profile_name
            db_var=$(echo "$entry" | cut -d: -f1)
            password_var=$(echo "$entry" | cut -d: -f2)
            svc_user=$(echo "$entry" | cut -d: -f3)
            profile_name=$(echo "$entry" | cut -d: -f4)

            # Use read_env_value (pipefail-safe) — raw `grep | cut` returns 1
            # when the key is absent from .env and, as a bare `var=$(...)`
            # assignment, propagates that non-zero under `set -eo pipefail`,
            # killing the whole upgrade (incl. `--check`) before the dry-run
            # `exit 0`. Absent keys are the common case here: a pre-2026.04-ga.6
            # box that never enabled the paperclip/matrix profiles has no
            # PAPERCLIP_DB / SYNAPSE_DB line at all. Same trap the module-
            # provider block below already guards against (see its comment).
            local db_name svc_password
            db_name=$(read_env_value .env "$db_var")
            svc_password=$(read_env_value .env "$password_var")

            if [ -n "$db_name" ] && [ -n "$svc_password" ] && \
               echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "$profile_name"; then
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would create PostgreSQL user '$svc_user' for database '$db_name'"
                    ((migrations_run++)) || true
                else
                    # Create user if not exists, grant privileges on database
                    PGPASSWORD="$pg_pass_perservice" docker exec postgres psql -U "$pg_user_perservice" -d postgres \
                        -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$svc_user') THEN CREATE ROLE \"$svc_user\" WITH LOGIN PASSWORD '$(echo "$svc_password" | sed "s/'/''/g")'; END IF; END \$\$; GRANT ALL PRIVILEGES ON DATABASE \"$db_name\" TO \"$svc_user\";" 2>/dev/null && \
                        print_substep "PostgreSQL user '$svc_user' created/verified for '$db_name'." || \
                        print_warning "Failed to create PostgreSQL user '$svc_user' for '$db_name'."
                    ((migrations_run++)) || true
                fi
            fi
        done
    fi

    # --- Migration: 2026.04-ga.6 — Authentik 2025.10→2026.2 session invalidation ---
    # Old JWT session cookies crash Authentik 2026.2 due to removed User_user_permissions model.
    # Warn the operator to clear browser cookies after upgrade.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.6" 2>/dev/null; then
        print_warning "IMPORTANT: After this upgrade, all users must clear their browser cookies"
        print_warning "for auth.${MAIN_DOMAIN} (or clear all site data). Old Authentik sessions"
        print_warning "are incompatible with the new version and will cause 'Server Error'."
        ((migrations_run++)) || true
    fi

    # --- Migration: 2026.04-ga.6 — Create Authentik providers + apps for new modules ---
    # On fresh installs, blueprints 12-24 create these automatically.
    # Existing installs upgrading from pre-ga.6 need them created via Django ORM.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.6" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would create Authentik providers and apps for: docling, stirling-pdf, hermes-agent,"
            print_info "    moltis, paperclip, element-web, paperless-ngx, infisical, onyx, openhands,"
            print_info "    coding-tools (ProxyProvider), synapse, vaultwarden (OAuth2Provider)"
            ((migrations_run++)) || true
        else
            print_substep "Creating Authentik providers and apps for new modules..."

            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
                if [ $((retries % 6)) -eq 0 ]; then
                    print_substep "  Waiting for authentik-worker... (${worker_health}, $((retries * 5))s)"
                fi
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker not healthy. Skipping new module Authentik migration."
            else
                # Read secrets and domains from .env for the Python script
                local main_domain hermes_domain moltis_domain paperclip_domain
                local matrix_domain element_web_domain paperless_domain vaultwarden_domain
                local infisical_domain onyx_domain openhands_domain coding_tools_domain
                local docling_client_secret stirling_client_secret hermes_client_secret
                local moltis_client_secret paperclip_client_secret element_web_client_secret
                local paperless_client_secret infisical_client_secret onyx_client_secret
                local openhands_client_secret coding_tools_client_secret
                local synapse_client_secret matrix_client_id vaultwarden_client_secret

                # Use read_env_value (pipefail-safe) — raw `grep | cut`
                # kills the script via set -eo pipefail when a key is
                # absent from .env, which is the common upgrade-from-older
                # case (e.g. DOCLING_CLIENT_SECRET / STIRLING_CLIENT_SECRET
                # don't exist in a pre-ga.5 stack).
                main_domain=$(read_env_value .env MAIN_DOMAIN)
                hermes_domain=$(read_env_value .env HERMES_DOMAIN)
                moltis_domain=$(read_env_value .env MOLTIS_DOMAIN)
                paperclip_domain=$(read_env_value .env PAPERCLIP_DOMAIN)
                matrix_domain=$(read_env_value .env MATRIX_DOMAIN)
                element_web_domain=$(read_env_value .env ELEMENT_WEB_DOMAIN)
                paperless_domain=$(read_env_value .env PAPERLESS_DOMAIN)
                vaultwarden_domain=$(read_env_value .env VAULTWARDEN_DOMAIN)
                infisical_domain=$(read_env_value .env INFISICAL_DOMAIN)
                onyx_domain=$(read_env_value .env ONYX_DOMAIN)
                openhands_domain=$(read_env_value .env OPENHANDS_DOMAIN)
                coding_tools_domain=$(read_env_value .env CODING_TOOLS_DOMAIN)

                docling_client_secret=$(read_env_value .env DOCLING_CLIENT_SECRET)
                stirling_client_secret=$(read_env_value .env STIRLING_CLIENT_SECRET)
                hermes_client_secret=$(read_env_value .env HERMES_CLIENT_SECRET)
                moltis_client_secret=$(read_env_value .env MOLTIS_CLIENT_SECRET)
                paperclip_client_secret=$(read_env_value .env PAPERCLIP_CLIENT_SECRET)
                element_web_client_secret=$(read_env_value .env ELEMENT_WEB_CLIENT_SECRET)
                paperless_client_secret=$(read_env_value .env PAPERLESS_CLIENT_SECRET)
                infisical_client_secret=$(read_env_value .env INFISICAL_CLIENT_SECRET)
                onyx_client_secret=$(read_env_value .env ONYX_CLIENT_SECRET)
                openhands_client_secret=$(read_env_value .env OPENHANDS_CLIENT_SECRET)
                coding_tools_client_secret=$(read_env_value .env CODING_TOOLS_CLIENT_SECRET)
                synapse_client_secret=$(read_env_value .env SYNAPSE_CLIENT_SECRET)
                matrix_client_id=$(read_env_value .env MATRIX_CLIENT_ID)
                vaultwarden_client_secret=$(read_env_value .env VAULTWARDEN_CLIENT_SECRET)

                # Resolve domain defaults (matching .env.example patterns)
                : "${hermes_domain:=hermes.${main_domain}}"
                : "${moltis_domain:=moltis.${main_domain}}"
                : "${paperclip_domain:=paperclip.${main_domain}}"
                : "${matrix_domain:=matrix.${main_domain}}"
                : "${element_web_domain:=element.${main_domain}}"
                : "${paperless_domain:=paperless.${main_domain}}"
                : "${vaultwarden_domain:=vault.${main_domain}}"
                : "${infisical_domain:=infisical.${main_domain}}"
                : "${onyx_domain:=onyx.${main_domain}}"
                : "${openhands_domain:=openhands.${main_domain}}"
                : "${coding_tools_domain:=coding.${main_domain}}"

                cat <<MIGRATION_EOF > /tmp/razzfazz-migration-new-modules.py
import os, sys, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application, Group
from authentik.providers.proxy.models import ProxyProvider
from authentik.providers.oauth2.models import OAuth2Provider
from authentik.outposts.models import Outpost
from authentik.flows.models import Flow
from authentik.policies.models import PolicyBinding
from authentik.crypto.models import CertificateKeyPair
from authentik.providers.oauth2.models import ScopeMapping

# Lookup shared references
auth_flow_explicit = Flow.objects.get(slug="default-provider-authorization-explicit-consent")
auth_flow_implicit = Flow.objects.get(slug="default-provider-authorization-implicit-consent")
invalidation_flow = Flow.objects.get(slug="default-provider-invalidation-flow")

# Scope mappings for proxy providers
proxy_mappings = list(ScopeMapping.objects.filter(name__in=[
    "authentik default OAuth Mapping: OpenID 'email'",
    "authentik default OAuth Mapping: OpenID 'profile'",
    "authentik default OAuth Mapping: Proxy outpost",
]))

# Scope mappings for OIDC providers
oidc_mappings = list(ScopeMapping.objects.filter(name__in=[
    "authentik default OAuth Mapping: OpenID 'email'",
    "authentik default OAuth Mapping: OpenID 'profile'",
    "authentik default OAuth Mapping: OpenID 'openid'",
]))

# Signing key for OIDC providers
signing_key = CertificateKeyPair.objects.filter(name="authentik Self-signed Certificate").first()

created = 0
skipped = 0

def ensure_proxy_provider(name, client_id, client_secret, external_host):
    """Create a forward-auth proxy provider if it does not exist."""
    global created, skipped
    if not client_secret:
        print(f"  SKIP {name}: no client_secret in .env")
        skipped += 1
        return None
    provider, was_created = ProxyProvider.objects.get_or_create(
        name=name,
        defaults={
            "client_id": client_id,
            "client_secret": client_secret,
            "authorization_flow": auth_flow_explicit,
            "invalidation_flow": invalidation_flow,
            "external_host": external_host,
            "mode": "forward_single",
            "intercept_header_auth": True,
            "access_token_validity": "hours=24",
        },
    )
    if was_created:
        provider.property_mappings.set(proxy_mappings)
        print(f"  CREATED provider: {name}")
        created += 1
    else:
        print(f"  EXISTS  provider: {name}")
    return provider

def ensure_oauth2_provider(name, client_id, client_secret, redirect_uris, consent="explicit"):
    """Create an OAuth2/OIDC provider if it does not exist."""
    global created, skipped
    if not client_secret or not client_id:
        print(f"  SKIP {name}: missing client_id or client_secret in .env")
        skipped += 1
        return None
    flow = auth_flow_explicit if consent == "explicit" else auth_flow_implicit
    defaults = {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_type": "confidential",
        "authorization_flow": flow,
        "invalidation_flow": invalidation_flow,
        "redirect_uris": redirect_uris,
        "access_code_validity": "minutes=1",
        "access_token_validity": "hours=1",
        "refresh_token_validity": "days=30",
        "include_claims_in_id_token": True,
    }
    if signing_key:
        defaults["signing_key"] = signing_key
    provider, was_created = OAuth2Provider.objects.get_or_create(
        name=name,
        defaults=defaults,
    )
    if was_created:
        provider.property_mappings.set(oidc_mappings)
        print(f"  CREATED provider: {name}")
        created += 1
    else:
        print(f"  EXISTS  provider: {name}")
    return provider

def ensure_app(slug, name, group, provider, description="", publisher="", icon="", launch_url=""):
    """Create an Authentik application if it does not exist."""
    global created
    app, was_created = Application.objects.get_or_create(
        slug=slug,
        defaults={
            "name": name,
            "group": group,
            "provider": provider,
            "meta_description": description,
            "meta_publisher": publisher,
            "meta_launch_url": launch_url,
            "open_in_new_tab": True,
            "policy_engine_mode": "any",
        },
    )
    if was_created:
        if icon:
            app.icon = icon
            app.save(update_fields=["icon"])
        print(f"  CREATED app: {slug} ({name})")
        created += 1
    else:
        print(f"  EXISTS  app: {slug}")
    return app

def ensure_group(name):
    """Create an Authentik group if it does not exist."""
    global created
    group, was_created = Group.objects.get_or_create(name=name)
    if was_created:
        print(f"  CREATED group: {name}")
        created += 1
    else:
        print(f"  EXISTS  group: {name}")
    return group

def ensure_binding(app_slug, group_name, order=0):
    """Create a policy binding for app -> group if it does not exist."""
    try:
        app = Application.objects.get(slug=app_slug)
        group = Group.objects.get(name=group_name)
        existing = PolicyBinding.objects.filter(target_id=app.pk, group=group).first()
        if not existing:
            PolicyBinding.objects.create(target_id=app.pk, group=group, enabled=True, order=order)
            print(f"  CREATED binding: {app_slug} -> {group_name}")
        else:
            print(f"  EXISTS  binding: {app_slug} -> {group_name}")
    except (Application.DoesNotExist, Group.DoesNotExist) as e:
        print(f"  SKIP binding {app_slug} -> {group_name}: {e}")

print("=== Creating ProxyProvider + Application pairs ===")

# 1. Docling
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Docling",
    "docling_app_client_id",
    "${docling_client_secret}",
    "https://docling.${main_domain}",
)
if p:
    ensure_app("docling", "Docling Converter", "3 Development", p,
        description="Document conversion and understanding API with Gradio UI",
        publisher="IBM / Docling Project",
        icon="/media/razzfazz-ai_docling_icon.png")

# 2. Stirling-PDF
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Stirling-PDF",
    "stirling_pdf_app_client_id",
    "${stirling_client_secret}",
    "https://pdf.${main_domain}",
)
if p:
    ensure_app("stirling-pdf", "PDF Tools", "1 Productivity", p,
        description="PDF manipulation and processing suite",
        publisher="Stirling-PDF",
        icon="/media/razzfazz-ai_pdf_icon.png")

# 3. Hermes Agent
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Hermes Agent",
    "hermes_agent_app_client_id",
    "${hermes_client_secret}",
    "https://${hermes_domain}",
)
if p:
    ensure_app("hermes-agent", "Hermes", "2 AI Assistants", p,
        description="Self-improving AI agent with persistent memory, 30+ tools, and MCP client",
        publisher="Nous Research",
        icon="/media/razzfazz-ai_hermes_icon.png")

# 4. Moltis
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Moltis",
    "moltis_app_client_id",
    "${moltis_client_secret}",
    "https://${moltis_domain}",
)
if p:
    ensure_app("moltis", "Moltis", "2 AI Assistants", p,
        description="Secure persistent personal agent server",
        publisher="moltis-org",
        icon="/media/razzfazz-ai_moltis_icon.png")

# 5. Paperclip
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Paperclip",
    "paperclip_app_client_id",
    "${paperclip_client_secret}",
    "https://${paperclip_domain}",
)
if p:
    ensure_app("paperclip", "Paperclip", "2 AI Assistants", p,
        description="Open-source AI company orchestration",
        publisher="paperclipai",
        icon="/media/razzfazz-ai_paperclip_icon.png")

# 6. Element Web (Matrix browser client — forward auth on matrix domain)
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Element Web",
    "element_web_app_client_id",
    "${element_web_client_secret}",
    "https://${matrix_domain}",
)
if p:
    ensure_app("element-web", "Matrix", "1 Productivity", p,
        description="Matrix browser client — encrypted messaging, rooms, and direct messages",
        publisher="Element HQ",
        icon="/media/razzfazz-ai_matrix_icon.png")

# 7. Paperless-ngx (also creates its group)
ensure_group("razzfazz.ai Paperless Users")
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Paperless-ngx",
    "paperless_app_client_id",
    "${paperless_client_secret}",
    "https://${paperless_domain}",
)
if p:
    ensure_app("paperless-ngx", "Documents", "1 Productivity", p,
        description="Scan, archive, and search your physical documents with OCR",
        publisher="paperless-ngx Project",
        icon="/media/razzfazz-ai_documents_icon.png")

# 8. Infisical
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Infisical",
    "infisical_app_client_id",
    "${infisical_client_secret}",
    "https://${infisical_domain}",
)
if p:
    ensure_app("infisical", "Secrets Manager", "5 Administration", p,
        description="Self-hosted secrets management for developers and AI coding agents",
        publisher="Infisical, Inc.",
        icon="/media/razzfazz-ai_secrets_icon.png")

# 9. Onyx (AI Search)
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Onyx",
    "onyx_app_client_id",
    "${onyx_client_secret}",
    "https://${onyx_domain}",
)
if p:
    ensure_app("onyx", "Search", "1 Productivity", p,
        description="AI-powered enterprise search across Gitea repos and documents",
        publisher="Onyx",
        icon="/media/razzfazz-ai_search_icon.png")

# 10. OpenHands
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for OpenHands",
    "openhands_app_client_id",
    "${openhands_client_secret}",
    "https://${openhands_domain}",
)
if p:
    ensure_app("openhands", "OpenHands", "3 Development", p,
        description="AI software development agent — autonomous coding, debugging, and PR creation",
        publisher="All Hands AI",
        icon="/media/razzfazz-ai_openhands_icon.png")

# 11. Coding Tools
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Coding Tools",
    "coding_tools_app_client_id",
    "${coding_tools_client_secret}",
    "https://${coding_tools_domain}",
)
if p:
    ensure_app("coding-tools", "Coding Tools", "3 Development", p,
        description="AI coding agents — gsd-pi and opencode, pre-configured with Gitea",
        publisher="razzfazz.ai",
        icon="/media/razzfazz-ai_coding_icon.png")

print("\n=== Creating OAuth2/OIDC Provider + Application pairs ===")

# 12. Synapse (Matrix OIDC — implicit consent, no portal tile)
p = ensure_oauth2_provider(
    "Synapse Matrix OIDC Provider",
    "${matrix_client_id}",
    "${synapse_client_secret}",
    "https://${matrix_domain}/_synapse/client/oidc/callback",
    consent="implicit",
)
if p:
    ensure_app("synapse", "Matrix Synapse (OIDC)", "", p,
        description="Matrix homeserver OIDC authentication provider (internal)")

# 13. Vaultwarden (OIDC — explicit consent, portal tile)
p = ensure_oauth2_provider(
    "Vaultwarden OIDC Provider",
    "vaultwarden_oidc_client",
    "${vaultwarden_client_secret}",
    "https://${vaultwarden_domain}/identity/connect/oidc-signin",
    consent="explicit",
)
if p:
    ensure_app("vaultwarden", "Password Manager", "1 Productivity", p,
        description="Self-hosted Bitwarden-compatible password manager",
        publisher="Vaultwarden / dani-garcia",
        icon="/media/razzfazz-ai_vault_icon.png")

print("\n=== Adding all new proxy providers to embedded outpost ===")
outpost = Outpost.objects.filter(name="authentik Embedded Outpost").first()
if outpost:
    for provider in ProxyProvider.objects.all():
        if provider not in outpost.providers.all():
            outpost.providers.add(provider)
            print(f"  Added to outpost: {provider.name}")
        else:
            print(f"  Already in outpost: {provider.name}")
    outpost.save()
else:
    print("  WARNING: Embedded outpost not found — proxy providers will not work!")

print("\n=== Creating policy bindings for new apps ===")

# Docling — Super Admins only
ensure_binding("docling", "razzfazz.ai Super Admins", 0)

# Stirling-PDF — Super Admins only
ensure_binding("stirling-pdf", "razzfazz.ai Super Admins", 0)

# Paperclip — Super Admins only
ensure_binding("paperclip", "razzfazz.ai Super Admins", 0)

# Moltis — Super Admins only
ensure_binding("moltis", "razzfazz.ai Super Admins", 0)

# Hermes Agent — Super Admins only
ensure_binding("hermes-agent", "razzfazz.ai Super Admins", 0)

# Element Web — Super Admins only
ensure_binding("element-web", "razzfazz.ai Super Admins", 0)

# Paperless-ngx — Paperless Users + Super Admins
ensure_binding("paperless-ngx", "razzfazz.ai Paperless Users", 0)
ensure_binding("paperless-ngx", "razzfazz.ai Super Admins", 1)

# Vaultwarden — Super Admins only
ensure_binding("vaultwarden", "razzfazz.ai Super Admins", 0)

# Infisical — Super Admins only
ensure_binding("infisical", "razzfazz.ai Super Admins", 0)

# Onyx — Super Admins only
ensure_binding("onyx", "razzfazz.ai Super Admins", 0)

# OpenHands — Super Admins only
ensure_binding("openhands", "razzfazz.ai Super Admins", 0)

# Coding Tools — Super Admins only
ensure_binding("coding-tools", "razzfazz.ai Super Admins", 0)

print(f"\nDone: {created} object(s) created, {skipped} skipped (missing secrets).")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-new-modules.py authentik-worker:/tmp/razzfazz-migration-new-modules.py 2>/dev/null; then
                    # F-silent-halt: swallow pipefail here so the explicit
                    # exit_code check below handles failure — without this,
                    # an error inside the Python migration kills the whole
                    # upgrade script via set -eo pipefail and skips the rest.
                    { docker exec authentik-worker \
                        python /tmp/razzfazz-migration-new-modules.py 2>&1 | while IFS= read -r line; do
                        print_substep "$line"
                    done; } || true
                    local exit_code=${PIPESTATUS[0]}
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-new-modules.py 2>/dev/null || true
                    rm -f /tmp/razzfazz-migration-new-modules.py
                    if [ "${exit_code:-0}" -eq 0 ]; then
                        print_success "New module Authentik migration applied."
                    else
                        print_warning "New module Authentik migration had errors — check output above."
                    fi
                else
                    print_warning "Could not reach authentik-worker for new module migration."
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.05-rc5 — Create agent_manager_db for existing installs ---
    # On fresh installs, init-db.sh creates this. Pre-rc5 installs that enable
    # the `agents` profile need it created here, otherwise agent-manager
    # crash-loops with `database "agent_manager_db" does not exist`.
    if version_lt "$INSTALLED_VERSION" "2026.05-rc5" 2>/dev/null; then

        local pg_user_am
        pg_user_am=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass_am
        pg_pass_am=$(read_env_value .env POSTGRES_PASSWORD)

        local agent_manager_db
        agent_manager_db=$(read_env_value .env AGENT_MANAGER_DB)
        agent_manager_db="${agent_manager_db:-agent_manager_db}"

        if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "agents"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create agent_manager_db if missing"
            else
                PGPASSWORD="$pg_pass_am" docker exec postgres psql -U "$pg_user_am" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$agent_manager_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_am" docker exec postgres psql -U "$pg_user_am" -d postgres \
                        -c "CREATE DATABASE \"$agent_manager_db\";" 2>/dev/null
                    print_substep "Database '$agent_manager_db' created (M020 agent-manager)."
                }
            fi
            ((migrations_run++)) || true
        fi
    fi

    # --- Migration: Dify 1.15 — one-shot plugin-auto-upgrade backfill (#26) ---
    # Dify 1.15.0 makes plugin auto-upgrade PER-CATEGORY. Upstream requires a
    # one-shot `flask backfill-plugin-auto-upgrade` AFTER `flask db upgrade`
    # (dify-api runs the latter automatically on start via MIGRATION_ENABLED);
    # skipping it silently breaks previously-configured plugin auto-upgrade
    # settings on existing installs. run_data_migrations() runs AFTER
    # restart_stack, so dify-api is already up applying the 24 new 1.15 alembic
    # revisions. We trigger the backfill exactly on the crossing into 1.15 —
    # the new DIFY_VERSION is >= 1.15.0 while the pre-upgrade one was < 1.15.0
    # (tag-name independent; keys off the Dify version transition, not
    # INSTALLED_VERSION) — and only when the dify profile is active. dify-api
    # has no compose healthcheck and the migrations take time, so we retry the
    # backfill (idempotent-ish) until the migrated schema is ready.
    if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "dify"; then
        local dify_new dify_old
        dify_new=$(read_env_value .env DIFY_VERSION 2>/dev/null || echo "")
        dify_new="${dify_new:-1.15.0}"
        dify_old=$(read_env_value ".env.pre-upgrade-backup" DIFY_VERSION 2>/dev/null || echo "")
        # Reached >= 1.15.0 now, AND wasn't already there before (empty backup
        # => can't prove we were on 1.15 => run once; harmless, idempotent-ish).
        if ! version_lt "$dify_new" "1.15.0" 2>/dev/null \
           && { [ -z "$dify_old" ] || version_lt "$dify_old" "1.15.0" 2>/dev/null; }; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would run 'flask backfill-plugin-auto-upgrade' in dify-api (Dify ${dify_old:-?} -> ${dify_new} crossing)"
                ((migrations_run++)) || true
            else
                print_substep "Dify 1.15: backfilling per-category plugin auto-upgrade settings..."
                local bf_retries=0 bf_ok=false
                while [ $bf_retries -lt 30 ]; do
                    if docker compose exec -T dify-api flask backfill-plugin-auto-upgrade >/dev/null 2>&1; then
                        bf_ok=true
                        break
                    fi
                    sleep 10
                    bf_retries=$((bf_retries + 1))
                    if [ $((bf_retries % 3)) -eq 0 ]; then
                        print_substep "  Waiting for dify-api 1.15 migrations to finish... ($((bf_retries * 10))s)"
                    fi
                done
                if [ "$bf_ok" = true ]; then
                    print_success "Dify plugin auto-upgrade backfill complete."
                else
                    print_warning "Dify plugin auto-upgrade backfill did not complete after retries."
                    print_info "  Run manually once dify-api is up: docker compose exec -T dify-api flask backfill-plugin-auto-upgrade"
                    journal_event "warning" "warn" "Dify 1.15 backfill-plugin-auto-upgrade did not complete; run manually (#26)" 2>/dev/null || true
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    if [ $migrations_run -eq 0 ]; then
        print_substep "No data migrations needed for this version range."
    else
        print_success "Data migrations complete: ${migrations_run} migration(s) applied."
    fi
}

# ==============================================================================
# Authentik Two-Hop Upgrade (F12)
# ==============================================================================
# Authentik 2025.12 introduced a schema migration (0056_user_roles) that
# depends on a prior migration shipped in 2025.12.x. Jumping directly from
# 2025.10.x to 2026.2.x crashes Django with:
#   FieldError: Cannot resolve keyword 'group_id'
# To avoid this, we run an intermediate 2025.12.4 hop whenever we detect
# a sub-2025.12 installation and a target >= 2025.12. This function also
# migrates the authentik-media volume layout: 2025.10 wrote files at the
# volume root (mounted /media), 2025.12+ expects /data/media/<files>.
# ==============================================================================
authentik_intermediate_hop() {
    print_substep "F12: checking whether Authentik two-hop upgrade is needed..."
    local current_version=""
    local raw_image
    raw_image=$(docker inspect authentik-server --format '{{.Config.Image}}' 2>/dev/null || true)
    if [ -n "$raw_image" ]; then
        current_version="${raw_image##*:}"
    fi
    print_substep "F12: detected current Authentik image: '${raw_image:-<none>}' (version: '${current_version:-<none>}')"

    if [ -z "$current_version" ] || [ "$current_version" = "<no value>" ]; then
        print_substep "F12: no installed Authentik detected — skipping hop."
        return 0
    fi

    # Only hop from 2025.10.x or older series across the 2025.12 boundary
    local needs_hop=false
    case "$current_version" in
        2025.10.*|2025.8.*|2025.6.*|2025.4.*|2025.2.*|2024.*|2023.*)
            needs_hop=true
            ;;
    esac

    if [ "$needs_hop" != "true" ]; then
        print_substep "F12: installed version ${current_version} already ≥ 2025.12 — no hop needed."
        return 0
    fi

    print_step "Running Authentik two-hop upgrade: ${current_version} → 2025.12.4 → (target)"

    # Migrate volume layout. Authentik changed the layout twice across the
    # 2025.10 → 2026.2 jump:
    #   pre-2025.12:  /data/<file>           (root of volume)
    #   2025.12 hop:  /data/media/<file>     (Django MEDIA_ROOT moved into a subdir)
    #   2026.2:       /data/media/public/<file>  (signed-URL serving requires the
    #                 public/ subdir; Application meta_icon and brand assets all
    #                 live here)
    # We do BOTH hops in one pass: anything at the volume root → /data/media/,
    # then anything under /data/media/<file> (excluding subdirs we expect like
    # public/, application-icons/, cdrom/, floppy/) → /data/media/public/.
    # Idempotent: skips entries that already exist at the destination. Caught
    # during prod 2026.05 cutover where the second hop was missing — Authentik
    # login page logo, flow background, app library tile icons all 404'd.
    local vol_name
    vol_name=$(docker volume ls --format '{{.Name}}' | grep -E '_authentik-media$' | head -1)
    if [ -n "$vol_name" ]; then
        print_substep "Migrating authentik-media volume layout (root → /data/media → /data/media/public)..."
        docker run --rm -v "${vol_name}:/data" alpine:3.21 sh -c '
            # Hop 1: volume root → /data/media/
            mkdir -p /data/media
            for entry in /data/*; do
                [ -e "$entry" ] || continue
                name=$(basename "$entry")
                [ "$name" = "media" ] && continue
                if [ ! -e "/data/media/$name" ]; then
                    mv "$entry" "/data/media/"
                fi
            done
            # Hop 2: /data/media/* (loose files only) → /data/media/public/
            mkdir -p /data/media/public
            for entry in /data/media/*; do
                [ -e "$entry" ] || continue
                name=$(basename "$entry")
                # Skip directories that Authentik expects at /data/media/<dirname>
                # (public/ is the destination; application-icons/ used by some
                # blueprints; cdrom + floppy are leftover dev artifacts).
                case "$name" in
                    public|application-icons|cdrom|floppy) continue ;;
                esac
                # Only move regular files (not directories — those are intentional)
                if [ -f "$entry" ] && [ ! -e "/data/media/public/$name" ]; then
                    mv "$entry" "/data/media/public/"
                fi
            done
        ' >/dev/null 2>&1 || print_warning "Volume layout migration produced warnings (continuing)."
    fi

    # Restart authentik containers on 2025.12.4 using env override (no .env edit).
    print_substep "Starting Authentik 2025.12.4 intermediate hop..."
    AUTHENTIK_VERSION=2025.12.4 docker compose up -d --force-recreate \
        authentik-server authentik-worker 2>&1

    # Wait for the hop to become healthy (Django migrations run on start).
    print_substep "Waiting for 2025.12.4 migrations to complete..."
    local max_wait=420 waited=0
    while [ $waited -lt $max_wait ]; do
        local health
        health=$(docker inspect authentik-server --format '{{.State.Health.Status}}' 2>/dev/null || echo "starting")
        local status
        status=$(docker inspect authentik-server --format '{{.State.Status}}' 2>/dev/null || echo "")
        if [ "$status" = "restarting" ] || [ "$status" = "exited" ]; then
            print_error "authentik-server failed during 2025.12.4 hop (status: $status)."
            docker logs --tail 50 authentik-server 2>&1 || true
            return 1
        fi
        if [ "$health" = "healthy" ]; then
            break
        fi
        sleep 5
        waited=$((waited + 5))
        if [ $((waited % 30)) -eq 0 ]; then
            print_substep "  ... still waiting (${waited}s / ${max_wait}s, health=${health})"
        fi
    done

    if [ $waited -ge $max_wait ]; then
        print_error "Authentik 2025.12.4 hop did not become healthy within ${max_wait}s."
        docker logs --tail 50 authentik-server 2>&1 || true
        return 1
    fi

    print_success "Authentik 2025.12.4 hop complete. Proceeding to target version."
}

# ==============================================================================
# Restart Stack
# ==============================================================================
restart_stack() {
    print_step "Restarting stack with new configuration..."

    # #125 P1: refresh the box-local Enterprise-docs overlay from the freshly
    # checked-out tree BEFORE recreating razzfazz-help. On an internal box this
    # mirrors docs/enterprise/ into overlay/enterprise/docs/; on a public/Codeberg
    # box docs/enterprise/ is absent → no content sync → the overlay (populated
    # out-of-band by a USB bake / gated fetch — P1.5/P2) is left untouched, so the
    # Enterprise docs are retained across the upgrade. Best-effort.
    if [ -x "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" ]; then
        print_substep "Refreshing box-local Enterprise-docs overlay for the Help-UI (#125)..."
        "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" 2>&1 || print_warning "Enterprise-docs overlay sync failed (Help-UI may serve community-only)."
    fi

    # F12: two-hop across the 2025.12 migration boundary
    authentik_intermediate_hop

    # rc6.7 #48: stop GPUStack-spawned runner pods before recreating the
    # gpustack server. They were spawned via the docker socket so compose
    # has no record of them; without this they'd survive the recreate and
    # double-pin VRAM/RAM until manually rm'd.
    stop_orphan_gpustack_runners

    # --remove-orphans: M018 / S06.6 renamed the gpustack-experimental and
    # model-sync-experimental services to gpustack and model-sync. Without
    # this flag, the old containers (which compose no longer recognizes
    # under the new service names) cause "container name in use" errors on
    # the next docker compose up. Same applies to any future profile/service
    # rename — keep this flag on.
    #
    # NO --build here (#184 WS2a — UNIVERSAL, all modes): the runtime path must
    # NEVER build. This was `up -d --build …` (the #1 offline breaker: it forces
    # a base-image pull + apt/pip/npm on every upgrade restart, tripping the
    # firewall on proxied/air-gapped boxes, and emitting "can't pull razzfazz-*"
    # noise on online ones). A NEW custom-build service that a release adds is now
    # built EXPLICITLY by build_and_pull (upgrade) / prebuild_all_custom_images
    # (post-install) BEFORE this restart; every build service also carries
    # `pull_policy: never`, so a missing image FAILS CLEAR (surfaced by
    # `rzfz verify-images`) instead of silently building. The historical
    # cognee-frontend regression (culturehack-001, 2026-05-21) is covered by that
    # explicit pre-build, not by an implicit `up --build`.
    # #156: guarantee the governance checksum DB is a regular FILE before compose
    # up. razzfazz-config bind-mounts .checksums.db as a single file. On a box whose
    # .gitignore did NOT ignore it (older public-curated Codeberg exports, ga..ga.4)
    # the pre-checkout `git stash --include-untracked` stashes it away; Docker then
    # recreates the bind-mount source as a DIRECTORY and razzfazz-config crash-loops
    # on sqlite (`unable to open database file`). The ga.5 curated-.gitignore fix
    # only protects go-forward (the stash runs from the SOURCE box's .gitignore);
    # this belt runs from the TARGET script's restart_stack, so it also fixes the
    # TRANSITION from any older buggy export. Coerce a stray dir / absent path back
    # to an empty file (sqlite re-inits the schema) — idempotent. See #156.
    if [ -d "${SCRIPT_DIR}/.checksums.db" ]; then
        rm -rf "${SCRIPT_DIR}/.checksums.db"
        print_substep "  #156: replaced a stray .checksums.db DIRECTORY with an empty file (razzfazz-config sqlite)."
    fi
    [ -e "${SCRIPT_DIR}/.checksums.db" ] || : > "${SCRIPT_DIR}/.checksums.db"

    # #153: a pre-existing-unhealthy peripheral (e.g. an onyx-vespa that was
    # already red BEFORE the upgrade) makes `compose up` exit rc=1. Under
    # `set -eo pipefail` that aborted the ENTIRE upgrade right here — before the
    # RAZZFAZZ_VERSION sync (so the box kept reporting the OLD version in the
    # Config UI) and before the post-upgrade self-heal `--refresh` ever ran
    # (observed on prod 8.246 ga.2→ga.3, 2026-07). Make it NON-FATAL: capture the
    # rc, report which services are not up/healthy, and continue. The health-wait
    # below + the final verification/diagnose gate still surface a genuine
    # regression, but one sick peripheral no longer blocks the version bump +
    # self-heal for the rest of the stack.
    local _compose_rc=0
    docker compose up -d --force-recreate --remove-orphans 2>&1 || _compose_rc=$?
    if [ "$_compose_rc" -ne 0 ]; then
        print_warning "compose up exited rc=${_compose_rc} — not every container (re)started/healthy. Continuing (#153)."
        docker compose ps --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null \
            | awk '$2!="running" || $3=="unhealthy" || $3=="starting"' \
            | sed 's/^/    not-ready: /' || true
    else
        print_substep "Containers recreated."
    fi

    # Wait for containers to become healthy
    print_substep "Waiting for services to become healthy..."
    local max_wait=300
    local waited=0
    local interval=10

    while [ $waited -lt $max_wait ]; do
        local unhealthy
        unhealthy=$(docker compose ps --format '{{.Name}} {{.Health}}' 2>/dev/null | \
            grep -cE 'starting|unhealthy' || true)
        local total
        total=$(docker compose ps --format '{{.Name}}' 2>/dev/null | wc -l)

        if [ "$unhealthy" -eq 0 ] && [ "$total" -gt 0 ]; then
            break
        fi

        sleep $interval
        waited=$((waited + interval))
        print_substep "  Waiting... (${waited}s / ${max_wait}s, ${unhealthy} services still starting)"
    done

    if [ $waited -ge $max_wait ]; then
        print_warning "Some services did not become healthy within ${max_wait}s."
    fi

    # Wait for Authentik init if running
    if docker ps --format '{{.Names}}' | grep -q "authentik-init"; then
        print_substep "Waiting for Authentik initialization..."
        timeout 300 docker wait authentik-init 2>/dev/null || true
    fi

    # rc6.7 #46 v3: re-attach openhands to docker default `bridge` network
    # so its spawned sandbox runtimes (which always land on `bridge`) can
    # call back without going through the host. Compose can't manage this
    # attachment (aliases unsupported on default bridge). Idempotent.
    # See docs/upstream-monkey-patches.md.
    if docker ps --format '{{.Names}}' | grep -qx openhands; then
        docker network connect bridge openhands 2>/dev/null || true
        docker exec openhands sh /opt/openhands-monkeypatch.sh 2>&1 | sed 's/^/  /' || true
        docker restart openhands >/dev/null 2>&1 || true
        print_substep "OpenHands re-attached to docker default bridge + URL monkey-patch refreshed"
    fi

    # #36: re-attach Caddy to docker default `bridge` for per-user openhands
    # agents' sandbox WS routing (openhands-<hash>.agents.../sandbox/<port>/ →
    # host.docker.internal:<port>). Without it the browser shows "Failed to
    # connect to server" even though the backend agent runs fine. Gated on the
    # `agents` profile (agent-manager); pin the concrete bridge gateway IP so
    # host.docker.internal resolves (the `host-gateway` keyword can land as
    # `invalid IP`). Idempotent. Mirrors the init.sh block.
    if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "agents" \
       && docker ps --format '{{.Names}}' | grep -qx caddy; then
        docker network connect bridge caddy 2>/dev/null || true
        _bridge_gw=$(docker network inspect bridge \
            --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
        _bridge_gw="${_bridge_gw:-172.17.0.1}"
        docker exec -u root caddy sh -c \
            "grep -qF '${_bridge_gw}\thost.docker.internal' /etc/hosts 2>/dev/null || \
             { grep -v 'host.docker.internal' /etc/hosts > /tmp/.h 2>/dev/null; \
               printf '%s\thost.docker.internal\n' '${_bridge_gw}' >> /tmp/.h; \
               cat /tmp/.h > /etc/hosts; }" 2>/dev/null || true
        print_substep "Caddy re-attached to docker default bridge (per-user OpenHands sandbox routing)"
    fi

    print_success "Stack restarted."
}

# ==============================================================================
# Post-Upgrade Verification
# ==============================================================================
# #148: After an Authentik version bump, sessions serialized by the OLD version
# fail to deserialize against the new model registry → LookupError 500 on every
# /application/o/authorize ("Server Error"). Flushing the session tables
# (everyone re-logs-in — harmless, no data loss) prevents the whole class. Gated
# on an actual AUTHENTIK_VERSION change so steady-state upgrades don't log users
# out for nothing. Per-table truncate so a missing table never blocks the rest.
flush_authentik_sessions_on_version_change() {
    local new_ver
    new_ver="$(read_env_value ".env" "AUTHENTIK_VERSION")"
    if [ -z "${OLD_AUTHENTIK_VERSION:-}" ] || [ "${OLD_AUTHENTIK_VERSION}" = "${new_ver}" ]; then
        return 0
    fi
    print_step "Authentik ${OLD_AUTHENTIK_VERSION} → ${new_ver}: flushing stale sessions (#148)..."
    journal_event "remediation" "info" "authentik ${OLD_AUTHENTIK_VERSION}->${new_ver}: flush sessions"
    local pg_user pg_pass auth_db i
    pg_user="$(read_env_value ".env" "POSTGRES_USER")"; [ -z "$pg_user" ] && pg_user="docker"
    pg_pass="$(read_env_value ".env" "POSTGRES_PASSWORD")"
    auth_db="$(read_env_value ".env" "AUTHENTIK_DB")"; [ -z "$auth_db" ] && auth_db="authentik_db"
    for i in $(seq 1 30); do
        docker exec postgres pg_isready -U "$pg_user" >/dev/null 2>&1 && break
        sleep 2
    done
    local t ok=true
    for t in authentik_core_session authentik_core_authenticatedsession \
             authentik_providers_proxy_proxysession authentik_providers_saml_samlsession django_session; do
        docker exec -e PGPASSWORD="$pg_pass" postgres psql -U "$pg_user" -d "$auth_db" \
            -c "TRUNCATE ${t} CASCADE;" >/dev/null 2>&1 || ok=false
    done
    if [ "$ok" = true ]; then
        print_success "Authentik sessions flushed (users re-login on next visit)."
        journal_event "remediation" "ok" "authentik sessions flushed"
    else
        print_warning "Some Authentik session tables could not be flushed (non-fatal)."
        journal_event "remediation" "warn" "authentik session flush partial"
    fi
}

# #145/#147: reconcile the embedded-outpost provider bindings from the HOST after
# restart. init-authentik.sh attempts this from inside the bare-alpine init
# container, which fails on a restricted-network box (runtime apk can't fetch
# docker-cli) → outpost unbound → gated apps 500/404 (the MEGAS/Chris case). The
# host ALWAYS has docker, so reconcile here too; idempotent (the bindings script
# no-ops when a provider is already attached). The #143-B diagnose-gate then
# confirms attached==total.
reconcile_outpost_bindings_hostside() {
    [ -f "core/Authentik/apply-policy-bindings.py" ] || return 0
    docker ps --format '{{.Names}}' | grep -qx authentik-worker || return 0
    # authentik-worker needs its Django ORM ready; restart_stack already waited
    # for stack health, but give the worker a short settle window.
    local i
    for i in $(seq 1 24); do
        docker exec authentik-worker python -c "import django" >/dev/null 2>&1 && break
        sleep 5
    done
    print_step "Reconciling Authentik outpost bindings from host (#145)..."
    journal_event "remediation" "info" "host-side outpost binding reconcile"
    if docker cp core/Authentik/apply-policy-bindings.py authentik-worker:/tmp/apply_policy_bindings.py 2>/dev/null \
       && docker exec authentik-worker python /tmp/apply_policy_bindings.py >/dev/null 2>&1; then
        print_success "Outpost bindings reconciled (host-side)."
        journal_event "remediation" "ok" "outpost bindings reconciled (host)"
    else
        print_warning "Host-side outpost reconcile reported issues — the diagnose-gate will flag if still unbound."
        journal_event "remediation" "warn" "host-side outpost reconcile failed"
    fi
}

# #142 (lightweight upgrade observability): when an upgrade hits a problem,
# auto-produce a diagnostic log bundle and ask the operator to email it to
# support. Reuses razzfazz-logs.sh --standalone (#141, container-independent —
# works even when the stack is down) + the structured journal (#143).
# Best-effort + once-per-run: log collection must never mask the real failure.
_UPGRADE_LOGZIP_DONE=""
collect_failure_logzip() {
    local reason="${1:-upgrade-failure}"
    [ -n "$_UPGRADE_LOGZIP_DONE" ] && return 0
    _UPGRADE_LOGZIP_DONE=1
    local zip=""
    if [ -x "${SCRIPT_DIR}/rzfz" ]; then
        zip="$("${SCRIPT_DIR}/rzfz" logs take --standalone "$reason" 2>/dev/null | tail -1)"
    fi
    echo ""
    print_warning "A diagnostic log bundle was created for this upgrade:"
    if [ -n "$zip" ] && [ -f "$zip" ]; then
        echo "    $zip  ($(du -h "$zip" 2>/dev/null | cut -f1))"
    else
        echo "    (automatic collection unavailable — run: rzfz logs take --standalone)"
    fi
    echo "    Please email this file to your razzfazz.ai support contact so the"
    echo "    upgrade outcome can be diagnosed."
    echo ""
}

verify_upgrade() {
    print_step "Verifying upgrade..."

    local issues=0

    # #143 A: explicit installed->target version event (the .env still holds the
    # pre-update RAZZFAZZ_VERSION at this point; it's rewritten below).
    journal_event "versions" "info" \
        "installed=$(read_env_value ".env" "RAZZFAZZ_VERSION") target=${TARGET_VERSION:-?} commit=${TARGET_COMMIT:-?}"

    # Check for restart loops
    local restarting
    restarting=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | \
        grep -c "Restarting" || true)
    if [ "$restarting" -gt 0 ]; then
        print_error "Restart loops detected in ${restarting} container(s):"
        docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep "Restarting"
        ((issues++)) || true
    fi

    # Check for critical log errors
    local critical_errors
    critical_errors=$(docker compose logs --tail=100 2>/dev/null | \
        grep -ciE 'FATAL|panic:|Traceback \(most recent' || true)
    if [ "$critical_errors" -gt 0 ]; then
        print_warning "Found ${critical_errors} critical log entries. Check logs for details."
        ((issues++)) || true
    fi

    # Check expected containers are running
    local expected_count
    expected_count=$(docker compose config --services 2>/dev/null | wc -l)
    local running_count
    running_count=$(docker compose ps --status running --format '{{.Name}}' 2>/dev/null | wc -l)
    print_substep "Running: ${running_count} / ${expected_count} services"

    # Update version tracking in .env
    update_env_value ".env" "RAZZFAZZ_VERSION" "$TARGET_VERSION"
    update_env_value ".env" "RAZZFAZZ_COMMIT" "$TARGET_COMMIT"
    print_substep "Version tracking updated: ${TARGET_VERSION} (${TARGET_COMMIT})"

    # STACK_HOST_PATH (rc5+): set if missing or stale. Required for the
    # razzfazz-config bind-mount to match the host path the docker daemon
    # sees — see core/compose.yml razzfazz-config comments. Idempotent:
    # only writes when the value differs from the current $(realpath .).
    local current_host_path
    current_host_path=$(read_env_value ".env" "STACK_HOST_PATH")
    local actual_host_path
    actual_host_path=$(realpath .)
    if [ "$current_host_path" != "$actual_host_path" ]; then
        update_env_value ".env" "STACK_HOST_PATH" "$actual_host_path"
        print_substep "Stack host path updated: ${actual_host_path}"
    fi

    # STACK_GIT_CREDENTIALS_PATH (rc5+): wired into razzfazz-config so it
    # can fetch the upstream manifest via git smart-HTTP (Authentik-exempt
    # path on git.razzfazz.ai). Best-effort — degrades to a workaround
    # error in the UI if missing, never blocks the upgrade.
    local current_git_creds_path
    current_git_creds_path=$(read_env_value ".env" "STACK_GIT_CREDENTIALS_PATH")
    if [ -f "${HOME}/.git-credentials" ] && [ "$current_git_creds_path" != "${HOME}/.git-credentials" ]; then
        update_env_value ".env" "STACK_GIT_CREDENTIALS_PATH" "${HOME}/.git-credentials"
        print_substep "git-credentials path recorded for manifest fetch"
    fi

    # Governance checksum snapshot (#22: runs on the host now).
    if RAZZFAZZ_STACK_ROOT="$SCRIPT_DIR" python3 "${SCRIPT_DIR}/cli/setup_lib/setup.py" \
            --checksum-take "Post-upgrade to v${TARGET_VERSION}" 2>/dev/null; then
        print_substep "Governance checksum snapshot created."
    else
        print_warning "Could not create checksum snapshot."
    fi

    # BSB-06 — deploy-time per-app group-binding lint. Upgrades can ship
    # binding-script typos that pass blueprint apply but break SSO at
    # customer login (97f7e2c0 was caught by a customer post-release).
    # Read-only; counts as a verify "issue" if the lint reports a
    # mismatch — surfaces in the verify-issue summary so the operator
    # sees it on the same screen as restart loops / FATAL log entries.
    if [ -x "${SCRIPT_DIR}/scripts/post-install-group-lint.sh" ]; then
        print_substep "BSB-06 group-binding lint..."
        if ! "${SCRIPT_DIR}/scripts/post-install-group-lint.sh"; then
            print_warning "Group-binding lint reported issues — review the output above."
            ((issues++)) || true
        fi
    fi

    # #143 B: post-upgrade diagnose-gate. Consistency asserts (outpost provider
    # bindings — the MEGAS/Chris 500/404 class; container health) + reuse of the
    # read-only razzfazz-post-install.sh --verify. Writes each finding to the
    # structured journal (inherits the open $RAZZFAZZ_JOURNAL_JSON) and adds its
    # problem count to the verify issues. `|| diag_problems=$?` keeps set -e
    # from aborting on a non-zero (= problems-found) exit.
    if [ -x "${SCRIPT_DIR}/scripts/upgrade-diagnose.sh" ]; then
        local diag_problems=0
        "${SCRIPT_DIR}/scripts/upgrade-diagnose.sh" || diag_problems=$?
        if [ "${diag_problems:-0}" -gt 0 ]; then
            print_warning "Diagnose-gate flagged ${diag_problems} consistency problem(s) — see journal."
            issues=$((issues + diag_problems))
        fi
    fi

    if [ $issues -eq 0 ]; then
        print_success "Upgrade verification passed."
    else
        print_warning "Upgrade completed with ${issues} issue(s). Review logs."
        # #142 (lightweight): bundle logs + ask the operator to email them.
        collect_failure_logzip "upgrade-verify-issues"
    fi

    return $issues
}

# ==============================================================================
# Rollback
# ==============================================================================
rollback() {
    print_step "Rolling back last upgrade..."

    # Restore .env files
    if [ -f ".env.pre-upgrade-backup" ]; then
        cp -f ".env.pre-upgrade-backup" ".env"
        print_substep "Restored .env from backup"
    else
        print_error "No .env.pre-upgrade-backup found. Cannot roll back."
        exit 1
    fi

    if [ -f ".env.dify.pre-upgrade-backup" ]; then
        cp -f ".env.dify.pre-upgrade-backup" ".env.dify"
        print_substep "Restored .env.dify from backup"
    fi

    # Git rollback (if possible)
    local old_commit
    old_commit=$(read_env_value ".env" "RAZZFAZZ_COMMIT")
    if [ -n "$old_commit" ] && [ "$old_commit" != "unknown" ] && [ -d ".git" ]; then
        print_substep "Checking out previous commit: ${old_commit}"
        git checkout "$old_commit" 2>&1 || {
            print_error "Could not checkout ${old_commit}. Manual rollback needed."
            print_info "Your .env files have been restored."
            print_info "To restore volumes, use: rzfz backup restore <backup-file>"
            exit 1
        }
    fi

    # Rebuild and restart
    if razzfazz_is_offline; then
        # #184 WS2b: on an air-gapped box the rolled-back code's images are already
        # present (built/loaded at the prior install) — do NOT build or pull (that
        # would trip the firewall). Recreate from the local images.
        print_substep "OFFLINE (RAZZFAZZ_NETWORK_MODE=offline): rolling back with existing local images (no build/pull)."
    else
        print_substep "Rebuilding images with rolled-back code..."
        # #184 WS2a: strip the no-build overlay so the build: contexts are present.
        COMPOSE_FILE="$(compose_file_for_build)" docker compose build --parallel 2>&1 || print_warning "Some builds failed."
        docker compose pull --ignore-pull-failures 2>&1 || true
    fi
    docker compose up -d --force-recreate 2>&1

    print_success "Rollback complete."
    print_warning "Database schema changes (if any) are NOT reverted."
    print_info "For full rollback including data, use: rzfz backup restore <backup-file>"
}

# ==============================================================================
# Post-Upgrade Acceptance Probes (--with-acceptance, M032-S06)
# ==============================================================================
# Wait up to RAZZFAZZ_ACCEPTANCE_HEALTHZ_TIMEOUT seconds (default 300) for
# every container to leave its starting/restarting/unhealthy state, then
# invoke `rzfz test --ci-mode --acceptance all` (plus
# `--include-disabled` if the operator passed the long-form flag). Returns 0
# on probe success, 4 on probe failure (exit code 4 = probes-failed,
# distinct from 1 = upgrade-failed). Mirrors the helper in razzfazz-init.sh.
run_acceptance_probes() {
    print_step "Running post-upgrade acceptance probes..."

    local timeout="${RAZZFAZZ_ACCEPTANCE_HEALTHZ_TIMEOUT:-300}"
    local interval=10
    local elapsed=0
    local unhealthy

    while [ "$elapsed" -lt "$timeout" ]; do
        unhealthy=$(docker compose ps --format '{{.Name}}\t{{.Status}}' 2>/dev/null \
            | grep -cE 'starting|unhealthy|restarting' || true)
        unhealthy="${unhealthy:-0}"
        if [ "$unhealthy" -eq 0 ]; then
            break
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
    done

    if [ "$elapsed" -ge "$timeout" ]; then
        print_warning "Some containers did not reach a healthy state within ${timeout}s."
        print_info "Running probes anyway — they will report which container is unhealthy."
    fi

    local wrapper="$SCRIPT_DIR/legacy/razzfazz-test.sh"
    if [ ! -x "$wrapper" ]; then
        print_error "razzfazz-test.sh not found at $wrapper — cannot run acceptance probes."
        return 4
    fi

    "$wrapper" --bootstrap-only >/dev/null 2>&1 || {
        print_error "Test venv bootstrap failed — cannot run acceptance probes."
        return 4
    }

    local probe_args=(--ci-mode --acceptance)
    if [ "$WITH_ACCEPTANCE_INCLUDE_DISABLED" = true ]; then
        probe_args+=(--include-disabled)
    fi
    probe_args+=(all)

    if "$wrapper" "${probe_args[@]}"; then
        print_success "All acceptance probes passed."
        return 0
    fi
    local rc=$?
    if [ "$rc" -eq 5 ]; then
        print_error "No acceptance probes were collected — see M032 S02."
    else
        print_error "One or more acceptance probes failed (probe-suite RC=$rc)."
    fi
    print_info "Report path: $SCRIPT_DIR/tests/results/latest/acceptance-report.md"
    return 4
}

# ==============================================================================
# Container Update Engine (--update)
# ==============================================================================
# Applies vendor-curated container version bumps from the manifest without
# changing stack code, configuration, or features.

MANIFESTS_DIR="${SCRIPT_DIR}/config/manifests"
MANIFEST_FILE="${MANIFESTS_DIR}/versions.json"

update_download_manifest() {
    local manifest_url
    manifest_url=$(python3 -c "
import json
with open('${MANIFEST_FILE}') as f:
    print(json.load(f).get('manifest_url', ''))
" 2>/dev/null)

    if [ -z "$manifest_url" ]; then
        print_error "No manifest_url found in ${MANIFEST_FILE}."
        return 1
    fi

    print_substep "Downloading manifest from ${manifest_url}..."
    local tmpfile="${MANIFESTS_DIR}/.versions.json.tmp"
    if ! curl -fsSL -o "$tmpfile" "$manifest_url"; then
        print_error "Failed to download manifest."
        return 1
    fi

    # Download checksum
    local checksum_url="${manifest_url}.sha256"
    local tmpsha="${MANIFESTS_DIR}/.versions.json.sha256.tmp"
    if curl -fsSL -o "$tmpsha" "$checksum_url" 2>/dev/null; then
        local expected
        expected=$(awk '{print $1}' "$tmpsha")
        local actual
        actual=$(sha256sum "$tmpfile" | awk '{print $1}')
        if [ "$expected" != "$actual" ]; then
            print_error "Checksum mismatch!"
            print_error "  Expected: ${expected}"
            print_error "  Actual:   ${actual}"
            rm -f "$tmpfile" "$tmpsha"
            return 1
        fi
        print_success "Checksum verified."
        rm -f "$tmpsha"
    else
        print_warning "No checksum file available — skipping verification."
    fi

    mv -f "$tmpfile" "${MANIFESTS_DIR}/versions-remote.json"
    print_success "Remote manifest saved."
}

update_compute_plan() {
    # Reads the manifest and compares against current .env / compose versions.
    # Outputs the update plan as JSON to stdout.
    # Args: $1 = manifest file path
    local manifest_path="${1:-${MANIFESTS_DIR}/versions-remote.json}"

    python3 << 'PYEOF'
import json, os, re, sys

manifest_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MANIFEST_PATH", "")
env_path = os.path.join(os.environ.get("SCRIPT_DIR", "."), ".env")

# Load manifest
with open(manifest_path) as f:
    manifest = json.load(f)

# Load .env
env = {}
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

# Load enabled profiles
enabled_profiles = set()
for p in env.get('COMPOSE_PROFILES', '').split(','):
    p = p.strip()
    if p:
        enabled_profiles.add(p)
# core is always enabled
enabled_profiles.add('core')

def strip_prefix(v):
    return v.lstrip('v')

def parse_semver(v):
    """Parse version into (major, minor, patch) tuple, ignoring suffixes."""
    v = v.lstrip('v')
    # Remove non-numeric suffix after last number group
    parts = re.split(r'[.\-]', v)
    nums = []
    for p in parts:
        m = re.match(r'^(\d+)', p)
        if m:
            nums.append(int(m.group(1)))
        else:
            break
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])

def is_within_compat(current, new, compat):
    if compat == 'frozen':
        return False
    cur = parse_semver(current)
    nw = parse_semver(new)
    if compat == 'patch':
        return cur[0] == nw[0] and cur[1] == nw[1] and nw[2] >= cur[2]
    if compat == 'minor':
        return cur[0] == nw[0] and (nw[1] > cur[1] or (nw[1] == cur[1] and nw[2] >= cur[2]))
    return False

plan = {"updates": [], "skipped": [], "frozen": [], "up_to_date": []}

# ENV-controlled images
for key, entry in manifest.get("images", {}).items():
    profile = entry.get("profile", "")
    if profile not in enabled_profiles:
        plan["skipped"].append({"key": key, "reason": f"profile '{profile}' not enabled"})
        continue

    env_var = entry.get("env_var", "")
    current = env.get(env_var, "")
    manifest_ver = entry.get("current", "")
    compat = entry.get("compatibility", "patch")

    if not current:
        plan["skipped"].append({"key": key, "reason": f"env var {env_var} not set"})
        continue

    if strip_prefix(current) == strip_prefix(manifest_ver):
        plan["up_to_date"].append({"key": key, "version": current})
        continue

    if compat == "frozen":
        plan["frozen"].append({"key": key, "current": current, "manifest": manifest_ver})
        continue

    if is_within_compat(current, manifest_ver, compat):
        plan["updates"].append({
            "key": key,
            "type": "env",
            "image": entry["image"],
            "env_var": env_var,
            "from": current,
            "to": manifest_ver,
            "compat": compat,
            "profile": profile,
        })
    else:
        plan["skipped"].append({
            "key": key,
            "reason": f"version {manifest_ver} outside {compat} range from {current}",
        })

# Hardcoded images
for key, entry in manifest.get("hardcoded", {}).items():
    profile = entry.get("profile", "")
    if profile not in enabled_profiles:
        plan["skipped"].append({"key": key, "reason": f"profile '{profile}' not enabled"})
        continue

    compat = entry.get("compatibility", "patch")
    manifest_ver = entry.get("current", "")
    compose_file = entry.get("compose_file", "")

    if compat == "frozen":
        plan["frozen"].append({"key": key, "current": "(hardcoded)", "manifest": manifest_ver})
        continue

    plan["updates"].append({
        "key": key,
        "type": "hardcoded",
        "image": entry["image"],
        "compose_file": compose_file,
        "from": "(check compose)",
        "to": manifest_ver,
        "compat": compat,
        "profile": profile,
    })

# CVE alerts
plan["cve_alerts"] = manifest.get("cve_alerts", [])
plan["manifest_version"] = manifest.get("stack_version", "unknown")
plan["published"] = manifest.get("published", "unknown")

json.dump(plan, sys.stdout, indent=2)
PYEOF
}

update_display_plan() {
    local plan_json="$1"

    local update_count
    update_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['updates']))")
    local frozen_count
    frozen_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['frozen']))")
    local uptodate_count
    uptodate_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['up_to_date']))")
    local manifest_ver
    manifest_ver=$(echo "$plan_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['manifest_version'])")
    local published
    published=$(echo "$plan_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['published'])")

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║              Container Update Plan                               ║${NC}"
    echo -e "${CYAN}╠══════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║${NC} Manifest: ${manifest_ver}  Published: ${published}"
    echo -e "${CYAN}║${NC} Updates: ${update_count}  Up-to-date: ${uptodate_count}  Frozen: ${frozen_count}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    if [ "$update_count" -gt 0 ]; then
        echo -e "${BLUE}Updates to apply:${NC}"
        echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for u in plan['updates']:
    arrow = u['from'] + ' → ' + u['to']
    print(f\"  {u['key']:30s} {arrow:40s} [{u['compat']}] ({u['profile']})\")
"
        echo ""
    fi

    if [ "$frozen_count" -gt 0 ]; then
        echo -e "${YELLOW}Frozen (not updated):${NC}"
        echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for f in plan['frozen']:
    print(f\"  {f['key']:30s} {f['current']:20s} (frozen)\")
"
        echo ""
    fi

    # CVE alerts
    local cve_count
    cve_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('cve_alerts',[])))")
    if [ "$cve_count" -gt 0 ]; then
        echo -e "${RED}CVE Alerts:${NC}"
        echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for c in plan.get('cve_alerts', []):
    print(f\"  ⚠ {c['cve']} ({c['severity']}) — {c['image']} fixed in {c['fixed_in']}\")
"
        echo ""
    fi
}

update_apply() {
    local plan_json="$1"

    local update_count
    update_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['updates']))")

    if [ "$update_count" -eq 0 ]; then
        print_success "All container images are up to date. Nothing to do."
        return 0
    fi

    # Save current manifest as previous for rollback
    if [ -f "${MANIFESTS_DIR}/applied.json" ]; then
        cp -f "${MANIFESTS_DIR}/applied.json" "${MANIFESTS_DIR}/previous.json"
        print_substep "Previous manifest saved for rollback."
    fi

    # Apply env-var updates
    print_step "Applying version updates..."
    echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for u in plan['updates']:
    if u['type'] == 'env':
        print(f\"{u['env_var']}={u['to']}\")
" | while IFS='=' read -r key value; do
        update_env_value ".env" "$key" "$value"
        print_substep "  ${key}=${value}"
    done

    # Apply hardcoded updates (compose file edits)
    echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for u in plan['updates']:
    if u['type'] == 'hardcoded':
        print(f\"{u['image']}|{u['to']}|{u['compose_file']}\")
" | while IFS='|' read -r image new_ver compose_file; do
        if [ -f "$compose_file" ]; then
            # Replace image:OLD_TAG with image:NEW_TAG
            sed -i -E "s|(image: *${image}:)[^ ]*|\1${new_ver}|g" "$compose_file"
            print_substep "  ${compose_file}: ${image}:${new_ver}"
        fi
    done

    # Collect affected profiles for targeted restart
    local profiles
    profiles=$(echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
profiles = set()
for u in plan['updates']:
    profiles.add(u['profile'])
print(','.join(sorted(profiles)))
")

    # Pull updated images
    if razzfazz_is_offline; then
        # #184 WS2b: an air-gapped box gets image updates from the offline package,
        # never from a registry. Skip the pull; the load happened at package apply.
        print_step "OFFLINE (RAZZFAZZ_NETWORK_MODE=offline): skipping image pull — updates come from the offline package."
    else
        print_step "Pulling updated images..."
        docker compose pull --ignore-pull-failures 2>&1 || print_warning "Some image pulls failed."
    fi

    # Restart affected services
    print_step "Restarting affected services..."
    docker compose up -d --force-recreate 2>&1

    # Save applied manifest
    local remote_manifest="${MANIFESTS_DIR}/versions-remote.json"
    if [ -f "$remote_manifest" ]; then
        cp -f "$remote_manifest" "${MANIFESTS_DIR}/applied.json"
    fi

    print_success "Container updates applied successfully."
    print_info "Affected profiles: ${profiles}"
}

update_rollback() {
    if [ ! -f "${MANIFESTS_DIR}/previous.json" ]; then
        print_error "No previous manifest found. Cannot rollback."
        return 1
    fi

    print_step "Rolling back to previous container versions..."

    # Compute plan from previous manifest (treating it as the target)
    local plan_json
    MANIFEST_PATH="${MANIFESTS_DIR}/previous.json" \
    plan_json=$(SCRIPT_DIR="$SCRIPT_DIR" update_compute_plan "${MANIFESTS_DIR}/previous.json")

    update_display_plan "$plan_json"
    update_apply "$plan_json"

    # Swap manifests
    mv -f "${MANIFESTS_DIR}/previous.json" "${MANIFESTS_DIR}/applied.json"
    print_success "Rollback complete."
}

run_update() {
    local do_check="$1"
    local manifest_file="$2"
    local do_rollback="$3"

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║                 rzfz.ai Container Update                         ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    # Handle rollback
    if [ "$do_rollback" = "true" ]; then
        update_rollback
        return $?
    fi

    # Download or use local manifest
    if [ -n "$manifest_file" ]; then
        if [ ! -f "$manifest_file" ]; then
            print_error "Manifest file not found: ${manifest_file}"
            return 1
        fi
        cp -f "$manifest_file" "${MANIFESTS_DIR}/versions-remote.json"
        print_success "Using local manifest: ${manifest_file}"
    else
        update_download_manifest || return 1
    fi

    # Compute update plan
    print_step "Computing update plan..."
    local plan_json
    plan_json=$(SCRIPT_DIR="$SCRIPT_DIR" MANIFEST_PATH="${MANIFESTS_DIR}/versions-remote.json" \
        update_compute_plan "${MANIFESTS_DIR}/versions-remote.json")

    # Display plan
    update_display_plan "$plan_json"

    # Check mode — stop here
    if [ "$do_check" = "true" ]; then
        print_info "Dry run complete. No changes were made."
        print_info "Run without --check to apply updates."
        return 0
    fi

    local update_count
    update_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['updates']))")

    if [ "$update_count" -eq 0 ]; then
        print_success "All container images are up to date."
        return 0
    fi

    # Confirm
    if [ "$FORCE" != true ]; then
        read -p "Apply ${update_count} container update(s)? [y/N] " confirm
        if [[ ! "$confirm" =~ ^[Yy] ]]; then
            print_warning "Update aborted by user."
            return 0
        fi
    fi

    update_apply "$plan_json"
}

# ==============================================================================
# Main
# ==============================================================================

# #3: library-only sourcing hook. When set, return here so that test harnesses
# (and any other caller) can `source` this script to obtain its functions
# (e.g. check_git_credentials) WITHOUT running the upgrade body. All function
# definitions live above this point; nothing below is a function definition.
if [ "${RAZZFAZZ_UPGRADE_LIB_ONLY:-}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

# Defaults
TARGET_TAG=""
PACKAGE_FILE=""
DRY_RUN="false"
SKIP_BACKUP=false
SKIP_VERIFY=false
SKIP_HOST_UPDATES=false   # F-RC5-5: bypass install_kernel_stability_tunables fatal-on-fail
SKIP_PULL=false
FORCE=false
DO_ROLLBACK=false
STASHED=false
DO_UPDATE=false
UPDATE_FILE=""
CONTINUE_AFTER_REEXEC=false
# M032-S06: opt-in post-upgrade acceptance probe hook. Same semantics as
# razzfazz-init.sh's --with-acceptance: wait-for-healthy → run probes →
# probe failure surfaces as exit 4 (probes-failed, distinct from 1).
WITH_ACCEPTANCE=false
WITH_ACCEPTANCE_INCLUDE_DISABLED=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --_continue)
            # Internal flag: script was re-exec'd after code update
            CONTINUE_AFTER_REEXEC=true
            INSTALLED_VERSION="${_UPGRADE_FROM_VERSION:-unknown}"
            INSTALLED_COMMIT="${_UPGRADE_FROM_COMMIT:-unknown}"
            TARGET_VERSION="${_UPGRADE_TO_VERSION:-unknown}"
            TARGET_COMMIT="${_UPGRADE_TO_COMMIT:-unknown}"
            STASHED="${_UPGRADE_STASHED:-false}"
            shift
            ;;
        --update)
            DO_UPDATE=true
            shift
            ;;
        --file)
            UPDATE_FILE="$2"
            shift 2
            ;;
        --target)
            TARGET_TAG="$2"
            shift 2
            ;;
        --package)
            PACKAGE_FILE="$2"
            shift 2
            ;;
        --check|--dry-run)
            DRY_RUN="true"
            shift
            ;;
        --skip-backup)
            SKIP_BACKUP=true
            shift
            ;;
        --skip-verify)
            SKIP_VERIFY=true
            shift
            ;;
        --skip-host-updates)
            # F-RC5-5: skip the fatal-on-fail kernel-stability-tunables install
            SKIP_HOST_UPDATES=true
            shift
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --rollback)
            DO_ROLLBACK=true
            shift
            ;;
        --with-acceptance)
            # M032-S06: post-upgrade acceptance probes. Tests only currently-
            # enabled profiles; probe failure → exit 4 (probes-failed).
            WITH_ACCEPTANCE=true
            shift
            ;;
        --with-acceptance-include-disabled)
            # M032-S06: as above, plus cycle each disabled profile up→probe→down.
            WITH_ACCEPTANCE=true
            WITH_ACCEPTANCE_INCLUDE_DISABLED=true
            shift
            ;;
        --status)
            # rc5.1+: read-only assessment via razzfazz-status.sh.
            # Exec'd directly so the operator's flags pass through and exit
            # codes match the dedicated tool.
            shift
            exec "${SCRIPT_DIR}/rzfz" status "$@"
            ;;
        -h|--help)
            show_help
            ;;
        *)
            print_error "Unknown option: $1"
            echo "Use --help for usage information."
            exit 1
            ;;
    esac
done

# Initialize upgrade log
echo "=== rzfz.ai Upgrade Log ===" > "$UPGRADE_LOG"
echo "Started: $(date -Iseconds)" >> "$UPGRADE_LOG"
echo "Args: $0 $*" >> "$UPGRADE_LOG"

# #143 (A+B): open the structured upgrade journal. Gate on "not already active"
# rather than "not a re-exec continuation": when upgrading FROM a version whose
# razzfazz-upgrade.sh predates the journal (the real field case, e.g. ga.7→rc1),
# the fresh process is the OLD script (no journal_init) and only the re-exec'd
# NEW script can open it — so the continuation MUST init when RAZZFAZZ_JOURNAL_JSON
# is still unset. In the new→new case the fresh process inits, exports the var
# (survives `exec`), and the continuation sees it set and keeps the same journal.
# The EXIT trap is registered unconditionally so both finalize (no-op if inactive).
if [ -z "${RAZZFAZZ_JOURNAL_JSON:-}" ]; then
    __RZ_RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
    journal_init "${SCRIPT_DIR}/backups/logs" "$__RZ_RUN_ID" \
        "installed=${INSTALLED_VERSION:-unknown}" "dry_run=${DRY_RUN:-false}"
fi
# #142 (lightweight): on a non-zero exit of a REAL upgrade run (guarded by
# _REAL_UPGRADE_STARTED so --check/--help/--rollback don't trigger it), also
# bundle logs + ask the operator to email them. The once-guard inside
# collect_failure_logzip dedupes with the verify-issues path.
trap '__rc=$?; journal_finalize "$([ "$__rc" -eq 0 ] && echo success || echo failure)"; { [ "$__rc" -ne 0 ] && [ -n "${_REAL_UPGRADE_STARTED:-}" ] && collect_failure_logzip "upgrade-aborted-rc${__rc}"; } || true' EXIT

print_banner

# F-RC5-5 (rc5.1): prime sudo timestamp early so install_kernel_stability_
# tunables (Step 5c) and any other sudo-needing steps don't fail mid-flow.
# Skipped in dry-run, --skip-host-updates, --update, --rollback, and
# --_continue (the re-execed second invocation inherits the parent's sudo
# timestamp via TTY).
if [ "$DRY_RUN" != "true" ] \
   && [ "$SKIP_HOST_UPDATES" != true ] \
   && [ "$DO_UPDATE" != true ] \
   && [ "$DO_ROLLBACK" != true ] \
   && [ "$CONTINUE_AFTER_REEXEC" != true ]; then
    if ! sudo -n true 2>/dev/null; then
        # No cached sudo. If we have no controlling tty (nohup, disowned SSH,
        # CI), `sudo -v` would block on a password prompt that nobody can
        # answer — fail fast with an actionable message instead of hanging.
        if [ ! -t 0 ] || [ ! -t 1 ]; then
            echo ""
            print_error "Non-interactive run detected and sudo password is not cached."
            print_info  "This upgrade installs system files (kernel-stability sysctl tunables)"
            print_info  "and would prompt for a password. Either:"
            print_info  "  • re-run with --skip-host-updates (bypasses sysctl + host hardening), or"
            print_info  "  • prime sudo first:  sudo -v   (then immediately re-run the upgrade)"
            exit 1
        fi
        echo ""
        echo "This upgrade installs system files (kernel-stability sysctl tunables)."
        echo "Please enter your sudo password to prime the timestamp:"
        if ! sudo -v; then
            echo ""
            print_error "sudo authentication failed."
            print_info "Re-run with --skip-host-updates to bypass host-side changes."
            exit 1
        fi
    fi
fi

# Handle --update mode (container version updates only)
if [ "$DO_UPDATE" = true ]; then
    run_update "$DRY_RUN" "$UPDATE_FILE" "$DO_ROLLBACK"
    exit $?
fi

# Handle --update --rollback (redirect to update rollback)
if [ "$DO_ROLLBACK" = true ] && [ -n "$UPDATE_FILE" ]; then
    DO_UPDATE=true
    run_update "false" "" "true"
    exit $?
fi

# Handle rollback
if [ "$DO_ROLLBACK" = true ]; then
    rollback
    exit $?
fi

# #142 (lightweight): a real upgrade body is about to run — arm the EXIT-trap
# log-bundle-on-failure (skipped for dry-run / --check). --update / --rollback
# already returned above, so reaching here means an actual upgrade.
[ "$DRY_RUN" != "true" ] && _REAL_UPGRADE_STARTED=1

# If this is a re-exec'd continuation, skip pre-flight / backup / code update
if [ "$CONTINUE_AFTER_REEXEC" = true ]; then
    print_step "Continuing upgrade from re-exec'd script (v${TARGET_VERSION})..."
else
    # Step 1: Pre-flight checks
    preflight_checks

    # Step 2: Pre-upgrade backup
    if [ "$SKIP_BACKUP" = false ] && [ "$DRY_RUN" != "true" ]; then
        pre_upgrade_backup
    else
        if [ "$SKIP_BACKUP" = true ]; then
            print_warning "Skipping pre-upgrade backup (--skip-backup)."
        fi
    fi

    # Step 3: Code update
    if [ -n "$PACKAGE_FILE" ]; then
        code_update_package
    elif [ "$DRY_RUN" != "true" ]; then
        code_update_git
        # #272: online git upgrade → clear any offline-package provenance marker
        # so `rzfz status` resumes the git tag-drift check for this box.
        update_env_value ".env" "RAZZFAZZ_UPGRADE_METHOD" "git"
        # Re-exec the new version of this script so all migration logic
        # and post-upgrade steps use the target version's code.
        reexec_if_updated
        # If reexec returns (shouldn't normally), continue in-place
    else
        # F1: Dry run with --target — read the VERSION file at the target ref
        # without touching the working tree. Falls back to current VERSION file
        # only when --target is not supplied.
        if [ -n "$TARGET_TAG" ] && git rev-parse -q --verify "refs/tags/${TARGET_TAG}" >/dev/null 2>&1; then
            TARGET_VERSION=$(git show "${TARGET_TAG}:VERSION" 2>/dev/null | tr -d '[:space:]')
            TARGET_COMMIT=$(git rev-list -1 "${TARGET_TAG}" --abbrev-commit 2>/dev/null)
            if [ -z "$TARGET_VERSION" ]; then
                print_warning "Could not read VERSION at tag ${TARGET_TAG}; falling back to current."
                detect_target_version
            fi
            print_info "Dry run: previewing upgrade to ${TARGET_VERSION} (commit: ${TARGET_COMMIT})"
        else
            detect_target_version
            print_info "Dry run: using current code as target (${TARGET_VERSION})"
        fi
    fi
fi

# Step 4: Environment migration
# #148: capture the pre-migration Authentik version so we can flush stale
# sessions if this upgrade bumps Authentik (sessions serialized by the old
# version fail to deserialize against the new model registry → LookupError 500
# on /application/o/authorize, the "Server Error" the field hit).
OLD_AUTHENTIK_VERSION="$(read_env_value ".env" "AUTHENTIK_VERSION")"
migrate_env

# Step 4a: Reconcile stale *_VERSION pins against the shipped manifest (#177).
# Runs every upgrade, version-independent, right after migrate_env — catches
# any *_VERSION drift that no change_default rule (or the #169 sync inside
# migrate_env) ever covered, using config/manifests/versions.json as authority.
reconcile_version_pins_from_manifest

# Step 4b: Heal empty per-service *_DB_USER values (#184). Runs every upgrade,
# version-independent, right after migrate_env so any keys migrate_env added are
# already present.
reconcile_service_db_users

# Step 4c: Reassign per-service DB *table ownership* (#154). Must run after
# reconcile_service_db_users (which may have just healed *_DB_USER) and while
# postgres is up (pre-restart). No-op on healthy + docker-superuser boxes.
reconcile_service_db_ownership

# Step 5: Dify environment sync
sync_dify_env

# Step 5b: M018 Phase 6 / S06.6 — collapse the old llm-box / llm-experimental
# / llm-cpu profiles into the unified `llm` profile + HARDWARE selector.
# Idempotent (skips on stacks already on `llm`); only fires once per host.
#
# Defensive design (after 8.93/0.78 rc5 upgrade post-mortem 2026-04-28):
# always emit a step header BEFORE entering the function and an explicit
# "no migration needed" reason on early-return. Previous silent-return paths
# made it impossible for an operator (or a debug session post-fact) to
# distinguish "function never called" from "function called and saw nothing
# to do." If you ever see no Step 5b output at all in the upgrade log, that
# now strictly means the line below didn't execute — the function itself is
# guaranteed-noisy.
migrate_llm_profiles() {
    print_step "Step 5b: LLM profile migration (M018 Phase 6 / S06.6)"

    local env_file="${SCRIPT_DIR}/.env"
    if [ ! -f "$env_file" ]; then
        print_substep "No .env file at $env_file — skipping."
        return 0
    fi
    # `|| true` so a no-match doesn't propagate exit 1 through the pipeline
    # under `set -eo pipefail`, killing the script silently before the
    # explicit empty-check below can fire.
    local current_profiles
    current_profiles=$(grep '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    if [ -z "$current_profiles" ]; then
        print_substep "COMPOSE_PROFILES is empty/missing in .env — skipping."
        return 0
    fi
    print_substep "Current COMPOSE_PROFILES: $current_profiles"

    # Detect any old profile name. Capture-then-grep instead of `if echo|tr|grep -q;`
    # — that pipeline pattern is fragile under `set -o pipefail` because grep -q
    # exits early and can SIGPIPE the upstream commands, flipping the if-condition
    # to false even when the pattern matched. Doing the comma-split into a variable
    # first removes the pipefail/grep-q interaction entirely.
    local profiles_lines
    profiles_lines=$(printf '%s\n' "$current_profiles" | tr ',' '\n')
    local old_profile=""
    if printf '%s\n' "$profiles_lines" | grep -Fxq "llm-experimental"; then
        old_profile="llm-experimental"
    elif printf '%s\n' "$profiles_lines" | grep -Fxq "llm-box"; then
        old_profile="llm-box"
    elif printf '%s\n' "$profiles_lines" | grep -Fxq "llm-cpu"; then
        old_profile="llm-cpu"
    fi

    if [ -z "$old_profile" ]; then
        print_substep "No legacy LLM profile (llm-experimental/llm-box/llm-cpu) in COMPOSE_PROFILES — already migrated or fresh install. No-op."
        return 0
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_step "Would migrate LLM profile: $old_profile → llm (M018 Phase 6 / S06.6)"
    else
        print_step "Migrating LLM profile: $old_profile → llm (M018 Phase 6 / S06.6)"
    fi

    # M029-S04: target profile depends on operator's intent (preserved
    # via the original profile name they had in 2026.04-ga) AND hardware:
    #
    #   llm-experimental → `llm`         (operator already opted into experimental)
    #   llm-cpu          → `llm-cpu`     (un-deprecated; stable CPU default — no rename)
    #   llm-box          → `llm-legacy`  (AMD operators land on the stable v0.7.1 default)
    #
    # NVIDIA isn't in the old-name set (rc4 added it under `llm` directly, so
    # NVIDIA upgraders were already on `llm` and don't enter this function).
    local target_profile target_hw
    case "$old_profile" in
        llm-experimental)
            target_profile="llm"
            target_hw="amd"
            ;;
        llm-cpu)
            target_profile="llm-cpu"
            target_hw="cpu"
            ;;
        llm-box)
            target_profile="llm-legacy"
            target_hw="amd"
            ;;
    esac
    print_substep "Target profile (M029-S04): $old_profile → $target_profile (hw=$target_hw)"

    # Replace old profile name with target_profile, then dedupe.
    local new_profiles
    new_profiles=$(echo "$current_profiles" | tr ',' '\n' \
        | sed "s/^${old_profile}\$/${target_profile}/" \
        | awk 'NF && !seen[$0]++' | paste -sd,)

    # COMPOSE_FILE only applies to the `llm` (v2.x) profile — it picks the
    # device overlay. For `llm-legacy` and `llm-cpu` the compose.yml is
    # self-contained. We default to `compose.yml` (not empty) because
    # docker compose 2.40+ chokes on `COMPOSE_FILE=` (empty value), reading
    # the project root as a file: `read /path/to/stack: is a directory`,
    # aborting build/up. Found during M029-S04 big-bang test on 8.93.
    local target_compose_file="compose.yml"
    if [ "$target_profile" = "llm" ]; then
        target_compose_file="compose.yml:modules/llm/compose.devices.${target_hw}.yml"
    fi

    local existing_hw existing_cf
    existing_hw=$(grep '^HARDWARE=' "$env_file" 2>/dev/null | cut -d= -f2- || true)
    existing_cf=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- || true)

    # PRESERVE OPERATOR-ADDED OVERLAYS in COMPOSE_FILE. This function only
    # manages the `compose.yml` + `modules/llm/compose.devices.*.yml` slots
    # (and the pre-2026.07 top-level `llm/compose.devices.*.yml` form, which
    # the 2026.07 Layout-A reorg moved under `modules/` — see the `case`
    # below: old-path entries are treated as managed so they get migrated to
    # the `modules/llm/...` form rather than re-appended as a stale overlay);
    # any
    # other overlay (e.g. operator's `docker-compose.kernel-compat.yml` or a
    # custom `docker-compose.override.yml`) gets re-appended after the
    # managed slots. Caught during the prod 2026.05 cutover where the
    # operator's apparmor compat overlay was silently clobbered, recreating
    # postgres+valkey with the broken security profile and breaking the 2-hop
    # Authentik migration. See .gsd/reports/fleet-test-2026-05-09-prod.md.
    if [ -n "${existing_cf:-}" ]; then
        local extra_overlays=""
        local _entry _saved_ifs
        _saved_ifs="$IFS"
        IFS=':'
        # shellcheck disable=SC2086
        for _entry in $existing_cf; do
            case "$_entry" in
                ""|"compose.yml"\
                |"llm/compose.devices.amd.yml"|"llm/compose.devices.cpu.yml"|"llm/compose.devices.nvidia.yml"\
                |"modules/llm/compose.devices.amd.yml"|"modules/llm/compose.devices.cpu.yml"|"modules/llm/compose.devices.nvidia.yml")
                    : # managed — skip (old top-level + new modules/ form both migrate to target)
                    ;;
                *)
                    extra_overlays="${extra_overlays}:${_entry}"
                    ;;
            esac
        done
        IFS="$_saved_ifs"
        if [ -n "$extra_overlays" ]; then
            target_compose_file="${target_compose_file}${extra_overlays}"
            print_substep "Preserving operator-added COMPOSE_FILE overlay(s):${extra_overlays}"
        fi
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_substep "Would set COMPOSE_PROFILES: $current_profiles → $new_profiles"
        print_substep "Would set HARDWARE: ${existing_hw:-<unset>} → $target_hw"
        if [ -n "$target_compose_file" ]; then
            print_substep "Would set COMPOSE_FILE: ${existing_cf:-<unset>} → $target_compose_file"
        else
            print_substep "Would CLEAR COMPOSE_FILE (was: ${existing_cf:-<unset>}; legacy/cpu profiles don't use it)"
        fi
        print_info "M029-S04: 2026.04-ga AMD installs land on the STABLE v0.7.1 path (\`llm-legacy\`)."
        print_info "  To opt into v2.1.x EXPERIMENTAL, run:"
        print_info "    Configuration Portal → Modules → LLM Runtime, OR"
        print_info "    edit .env: COMPOSE_PROFILES=...,llm,... + HARDWARE=$target_hw + COMPOSE_FILE=compose.yml:modules/llm/compose.devices.$target_hw.yml"
        return 0
    fi

    update_env_value "$env_file" "COMPOSE_PROFILES" "$new_profiles"
    print_substep "COMPOSE_PROFILES: $current_profiles → $new_profiles"

    if [ "$existing_hw" != "$target_hw" ]; then
        update_env_value "$env_file" "HARDWARE" "$target_hw"
        print_substep "HARDWARE: ${existing_hw:-<unset>} → $target_hw"
    fi

    if [ -n "$target_compose_file" ] && [ "$existing_cf" != "$target_compose_file" ]; then
        update_env_value "$env_file" "COMPOSE_FILE" "$target_compose_file"
        print_substep "COMPOSE_FILE: ${existing_cf:-<unset>} → $target_compose_file"
    elif [ -z "$target_compose_file" ] && [ -n "$existing_cf" ]; then
        # Legacy / CPU profiles don't use COMPOSE_FILE — clearing prevents
        # the v2.x device overlay from being applied to a v0.7.1 deploy.
        update_env_value "$env_file" "COMPOSE_FILE" ""
        print_substep "COMPOSE_FILE: $existing_cf → (cleared; legacy profiles don't use device overlays)"
    fi

    # Verify .env actually reflects the migration. update_env_value is
    # supposed to write through, but sed on a 30-line subset of a 600-line
    # operator-edited .env can fail silently if the file has unusual line
    # endings or missing newlines. Surface that BEFORE it bites later.
    local verify_profiles verify_hw verify_cf
    verify_profiles=$(grep '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    verify_hw=$(grep '^HARDWARE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    verify_cf=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    local verify_ok=true
    if [ "$verify_profiles" != "$new_profiles" ]; then
        print_warning "VERIFY FAIL: COMPOSE_PROFILES did not update — got '$verify_profiles', expected '$new_profiles'"
        verify_ok=false
    fi
    if [ "$verify_hw" != "$target_hw" ]; then
        print_warning "VERIFY FAIL: HARDWARE did not update — got '$verify_hw', expected '$target_hw'"
        verify_ok=false
    fi
    if [ "$verify_cf" != "$target_compose_file" ]; then
        print_warning "VERIFY FAIL: COMPOSE_FILE did not update — got '$verify_cf', expected '$target_compose_file'"
        verify_ok=false
    fi
    if [ "$verify_ok" = true ]; then
        print_substep "Verified: .env now reflects the new \`llm\` profile state."
    fi

    print_warning "If the new \`llm\` path doesn't work, fall back to the frozen 0.7.1 stack:"
    print_info "  Edit .env: replace \`llm\` with \`llm-legacy\` in COMPOSE_PROFILES"
    print_info "  Then: docker compose up -d --force-recreate gpustack model-sync"

    # rc6.6 (M023-S05.3): tear down legacy-profile containers BEFORE
    # restart_stack runs `docker compose up -d`. Three services in
    # modules/llm/compose.yml share `container_name: gpustack` (gpustack-legacy
    # for llm-legacy, gpustack for llm, gpustack-cpu for llm-cpu) — same
    # for `model-sync` / `*-legacy` / `*-cpu`. They are mutually exclusive
    # by container_name, so flipping the active profile leaves a stale
    # container under the shared name unless we explicitly remove it.
    # `docker compose up --remove-orphans` does NOT clean these up because
    # the legacy services are still defined in compose.yml — they're just
    # in a profile that's no longer active, which compose treats as
    # "inactive" rather than "orphaned". The result on box-004 / 8.93
    # during the rc6.5 fleet test was:
    #   `Error response from daemon: Conflict. The container name "/gpustack"
    #    is already in use by container "<old-id>"`
    # which aborted restart_stack mid-flow.
    #
    # Stop+remove only the LEGACY services (the ones whose profile is no
    # longer in COMPOSE_PROFILES). The new `gpustack` / `model-sync` from
    # the active `llm` profile will be created cleanly by restart_stack.
    case "$old_profile" in
        llm-cpu)
            print_substep "Stopping + removing legacy llm-cpu containers (gpustack-cpu, model-sync-cpu) so the new \`llm\` profile can take their container_name slot..."
            docker stop gpustack-cpu model-sync-cpu 2>/dev/null || true
            docker rm -f gpustack-cpu model-sync-cpu 2>/dev/null || true
            # The container_name `gpustack` / `model-sync` is what the
            # next compose up needs free; if the bare names are still
            # held by the legacy containers under their container_name,
            # rm by name too.
            docker rm -f gpustack model-sync 2>/dev/null || true
            ;;
        llm-box|llm-experimental)
            print_substep "Stopping + removing legacy llm-box/llm-experimental containers..."
            # These older profiles used container_name=gpustack-experimental
            # / model-sync-experimental in M018-pre-S06.6 layouts. Match
            # both the legacy and shared names defensively.
            docker rm -f gpustack-experimental model-sync-experimental \
                          gpustack-legacy model-sync-legacy \
                          gpustack model-sync 2>/dev/null || true
            ;;
    esac
}
migrate_llm_profiles

# Step 5b.0.5: F-RC5-1 / rc6.9 — install GPUSTACK_*_HOST_BIND defaults based
# on GPUSTACK_MODE for boxes that were initialised pre-rc6.9. Pre-fix the
# compose worker-port default was 0.0.0.0 unconditionally, so standalone
# boxes had ports 10150-10151 bound on the LAN. Both gpustack-legacy and
# gpustack v2.x share the same compose default, so this fix harmonises
# hardening across both runtime profiles.
migrate_gpustack_bind() {
    print_step "Step 5b.0.5: GPUStack host-bind hardening (F-RC5-1 / rc6.9)"
    local env_file="${SCRIPT_DIR}/.env"
    [ -f "$env_file" ] || { print_substep "No .env — skipping."; return 0; }
    local mode existing_host existing_worker
    mode=$(grep '^GPUSTACK_MODE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    existing_host=$(grep '^GPUSTACK_HOST_BIND=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    existing_worker=$(grep '^GPUSTACK_WORKER_HOST_BIND=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    local target_host target_worker
    case "${mode:-standalone}" in
        master)            target_host="0.0.0.0";   target_worker="0.0.0.0" ;;
        worker)            target_host="127.0.0.1"; target_worker="0.0.0.0" ;;
        standalone|*)      target_host="127.0.0.1"; target_worker="127.0.0.1" ;;
    esac
    print_substep "GPUSTACK_MODE=${mode:-standalone} → HOST_BIND=$target_host, WORKER_HOST_BIND=$target_worker"
    if [ "$DRY_RUN" = "true" ]; then
        print_substep "  Would set GPUSTACK_HOST_BIND=$target_host (was: ${existing_host:-<unset>})"
        print_substep "  Would set GPUSTACK_WORKER_HOST_BIND=$target_worker (was: ${existing_worker:-<unset>})"
        return 0
    fi
    [ "$existing_host" != "$target_host" ] && update_env_value "$env_file" "GPUSTACK_HOST_BIND" "$target_host"
    [ "$existing_worker" != "$target_worker" ] && update_env_value "$env_file" "GPUSTACK_WORKER_HOST_BIND" "$target_worker"
    print_substep "  Updated."
}
migrate_gpustack_bind

# Step 5b.0.5b: COMPOSE_FILE Layout-A path migration (2026.07 reorg / #26).
# The 2026.07 Layout-A reorg moved every per-module dir under `modules/`, so
# the LLM device overlay moved from `llm/compose.devices.<HW>.yml` to
# `modules/llm/compose.devices.<HW>.yml`. EXISTING boxes carry the OLD
# top-level path in their LIVE .env's COMPOSE_FILE — after the code update
# `docker compose` can't find `llm/compose.devices.amd.yml` and refuses to
# compose the whole stack. This migration rewrites ONLY the device-overlay
# slot in place (`:llm/compose.devices.` → `:modules/llm/compose.devices.`),
# preserving the rest of the chain (compose.yml, operator-added overlays,
# ordering). Idempotent — already-migrated values are left untouched. Runs
# BEFORE migrate_compose_file_chain so the chain self-heal sees the new path.
# Regression-tested in tests/test-compose-file-modules-migration.sh (extracts
# the function verbatim between the markers below).
# >>> LAYOUT_A_COMPOSE_FILE_MIGRATION_BEGIN
migrate_compose_file_to_modules_layout() {
    local env_file="$1"
    [ -f "$env_file" ] || return 0
    local current
    current=$(read_env_value "$env_file" COMPOSE_FILE)
    [ -n "$current" ] || return 0
    # Only the pre-2026.07 top-level overlay slot is rewritten. The `:` anchor
    # avoids touching an already-migrated `:modules/llm/...` entry and avoids
    # matching unrelated substrings. There is exactly one device-overlay slot
    # per chain, but `//` keeps it robust if a box somehow carries two.
    case "$current" in
        *":llm/compose.devices."*)
            # NB: bash ${var//pat/rep} treats `/` inside `pat` as the
            # pattern/replacement delimiter, so we can't pattern-match a path
            # with embedded slashes there. Use sed with a `#` delimiter, and a
            # literal-dot-tolerant pattern (the `.` are fine to match literally).
            local migrated
            migrated=$(printf '%s' "$current" | sed 's#:llm/compose\.devices\.#:modules/llm/compose.devices.#g')
            update_env_value "$env_file" "COMPOSE_FILE" "$migrated"
            print_substep "  Migrated COMPOSE_FILE LLM overlay to modules/ layout (2026.07 #26):"
            print_substep "    $current → $migrated"
            ;;
        *)
            : # already on modules/ layout, or no llm overlay — no-op
            ;;
    esac
}
# <<< LAYOUT_A_COMPOSE_FILE_MIGRATION_END
migrate_compose_file_to_modules_layout "${SCRIPT_DIR}/.env"

# Step 5b.0.6: COMPOSE_FILE chain self-heal — match HARDWARE to the right
# per-device overlay. Pre-fix, boxes installed pre-M018 (or whose .env
# was manually edited at any point) could have COMPOSE_FILE=compose.yml
# alone, missing modules/llm/compose.devices.<amd|nvidia|cpu>.yml (2026.07
# Layout-A reorg moved these under modules/; this self-heal also migrates
# any box still carrying the pre-2026.07 top-level llm/ path). Symptom: the
# `gpustack` service isn't in the live compose graph, so
# `docker compose ps gpustack` returns nothing AND `compose up -d
# --force-recreate gpustack` fails with "name already in use" on the
# orphan container left from when the overlay WAS active. Repair is a
# single sed in .env. Idempotent: only writes when current ≠ desired.
# Discovered on the SEQIS prod worker (10.163) 2026-05-18 during the
# v2026.05-ga.4 cleanup pass; covered in ga.4 via the moved tag.
migrate_compose_file_chain() {
    print_step "Step 5b.0.6: COMPOSE_FILE chain self-heal (HARDWARE overlay)"
    local env_file="${SCRIPT_DIR}/.env"
    [ -f "$env_file" ] || { print_substep "No .env — skipping."; return 0; }

    local hardware profiles current llm_profile want hw path
    hardware=$(grep '^HARDWARE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    profiles=$(grep '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    current=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)

    # #448 defect 2: the device overlay is LLM-PROFILE-specific, not
    # HARDWARE-specific. Only the v2.x `llm` profile consumes one; llm-legacy and
    # llm-cpu are self-contained (cli/init.sh:1457, and the "prevents the v2.x
    # device overlay from being applied to a v0.7.1 deploy" branch below).
    # HARDWARE=amd is set on those boxes too, so keying off it alone applied the
    # wrong overlay to a v0.7.1 deploy.
    llm_profile=""
    case ",${profiles}," in
        *,llm,*)        llm_profile="llm" ;;
        *,llm-legacy,*) llm_profile="llm-legacy" ;;
        *,llm-cpu,*)    llm_profile="llm-cpu" ;;
    esac

    want=""
    if [ "$llm_profile" = "llm" ]; then
        case "${hardware}" in
            amd|nvidia|cpu) want="modules/llm/compose.devices.${hardware}.yml" ;;
            "")  print_substep "  HARDWARE not set — leaving COMPOSE_FILE as-is."; return 0 ;;
            *)   print_substep "  HARDWARE='${hardware}' unrecognised — leaving COMPOSE_FILE as-is."; return 0 ;;
        esac
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_substep "  Would reconcile the device overlay to: ${want:-<none, ${llm_profile:-no llm profile} is self-contained>}"
        print_substep "  (every other overlay in '${current}' is preserved)"
        return 0
    fi

    # #448 defect 1: this used to rebuild COMPOSE_FILE from scratch —
    #     desired="compose.yml"; case $hardware in ...) desired="$desired:<dev>" ;;
    # which DELETED every other overlay on every upgrade: compose.no-build.yml
    # (#184 WS2a — its absence silently re-enables runtime builds and internet
    # reach on an installed box), the corporate-proxy overlay (#181/#280) and
    # compose.offline.yml. Seen in the field on 0.236: "current:
    # compose.yml:compose.no-build.yml → desired:
    # compose.yml:modules/llm/compose.devices.amd.yml. Repaired."
    #
    # Reconcile ONLY the device overlay, via the idempotent helpers in
    # scripts/lib.sh. Anything else in the chain is untouched by construction
    # rather than by remembering to re-append it.
    if [ -z "$current" ]; then
        # docker compose 2.40+ chokes on an empty COMPOSE_FILE value.
        update_env_value "$env_file" "COMPOSE_FILE" "compose.yml"
    fi
    for hw in amd nvidia cpu; do
        # Both the current path and the pre-2026.07 top-level one (#119 moved
        # these under modules/), so an old box does not keep a dangling entry.
        for path in "modules/llm/compose.devices.${hw}.yml" "llm/compose.devices.${hw}.yml"; do
            [ "$path" = "$want" ] && continue
            compose_file_overlay_remove "$env_file" "$path"
        done
    done
    [ -n "$want" ] && compose_file_overlay_add "$env_file" "$want"

    local now
    now=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    if [ "$now" = "$current" ]; then
        print_substep "  COMPOSE_FILE already correct for ${llm_profile:-no-llm}/${hardware:-unset} (no change)."
    else
        print_substep "  COMPOSE_FILE: ${current:-<unset>} → ${now}"
    fi
}
migrate_compose_file_chain

# Step 5b.1: rc6.7 #63 — refresh RENDER_GID for AMD hosts on every upgrade.
# Pre-fix the migrate_env step would have stamped the manifest's static
# default (992) into .env. That covers Ubuntu 24.04 / most modern distros
# but not all — AlmaLinux 9 puts render at 989, debian-bookworm at 109,
# etc. If we wrote the wrong value, gpustack restarts via restart_stack
# (which force-recreates) would land back at ACCEL_WORKING -13 EACCES on
# the inner GPU access. Detect at upgrade-time and overwrite when wrong.
refresh_render_gid_for_amd() {
    local env_file="${SCRIPT_DIR}/.env"
    [ -f "$env_file" ] || return 0
    local hardware
    hardware=$(read_env_value "$env_file" HARDWARE)
    [ "$hardware" = "amd" ] || return 0

    local detected
    detected=$(getent group render 2>/dev/null | cut -d: -f3)
    [ -n "$detected" ] || return 0

    local current
    current=$(read_env_value "$env_file" RENDER_GID)
    if [ "$current" = "$detected" ]; then
        return 0   # already correct, silent no-op
    fi

    print_step "Refreshing RENDER_GID for AMD GPU device access (rc6.7 #63)..."
    if [ -z "$current" ]; then
        print_substep "Setting RENDER_GID=$detected (was unset)"
    else
        print_substep "Updating RENDER_GID: $current → $detected (host's render gid)"
    fi
    update_env_value "$env_file" RENDER_GID "$detected"
}
refresh_render_gid_for_amd

# Step 5c: Install / refresh kernel stability tunables (M018 / S03.5).
# Same template that razzfazz-init.sh installs on first install — keeps
# existing operators in sync as defaults evolve.
install_kernel_stability_tunables() {
    local src="${SCRIPT_DIR}/core/sysctl/99-razzfazz-stability.conf"
    local dst="/etc/sysctl.d/99-razzfazz-stability.conf"
    [ -r "$src" ] || return 0
    if [ "$DRY_RUN" = "true" ]; then
        if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst" 2>/dev/null; then
            print_step "Would install kernel stability tunables → $dst (M018 / S03.5)"
        fi
        return 0
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        return 0   # already current
    fi
    print_step "Installing kernel stability tunables (M018 / S03.5)..."
    if sudo cp "$src" "$dst" 2>/dev/null && sudo sysctl --system >/dev/null 2>&1; then
        print_substep "Installed $dst + reloaded."
        print_substep "Active: vm.panic_on_oom=$(sysctl -n vm.panic_on_oom 2>/dev/null), kernel.panic=$(sysctl -n kernel.panic 2>/dev/null), vm.swappiness=$(sysctl -n vm.swappiness 2>/dev/null)"
    else
        # F-RC5-5: was a silent print_warning; now fatal-on-fail unless operator
        # explicitly opts out via --skip-host-updates. The previous behaviour
        # silently skipped on sudo failures and the operator missed the
        # warning in long upgrade logs (proven on 0.78 rc5 upgrade — sysctl
        # tunables never landed on the box).
        if [ "${SKIP_HOST_UPDATES:-false}" = true ]; then
            print_warning "Could not install $dst (sudo required) — bypassed via --skip-host-updates."
            print_info "  Apply manually: sudo cp $src $dst && sudo sysctl --system"
        else
            print_error "Could not install $dst (sudo required)."
            print_info "  Manual: sudo cp $src $dst && sudo sysctl --system"
            print_info "  Bypass: re-run razzfazz-upgrade.sh with --skip-host-updates"
            return 1
        fi
    fi
}
if ! install_kernel_stability_tunables; then
    # F-RC5-5: fatal-on-fail unless --skip-host-updates was passed.
    # The function itself prints a clear error + manual instructions.
    exit 1
fi

# Step 5d: Host hardening (rc5.1 fleet rollout) — auto-run if not done yet.
# Marker file: /etc/razzfazz/host-hardened (written by scripts/harden-host.sh
# on success). Without --skip-host-updates and without the marker, run the
# hardening script with safe defaults (NO --restrict-ssh; SSH stays as-is).
# The operator can re-run harden-host.sh manually with --restrict-ssh later
# once they've confirmed pubkey access works.
ensure_host_hardened() {
    if [ "${SKIP_HOST_UPDATES:-false}" = true ]; then
        print_substep "Skipping host hardening check (--skip-host-updates)"
        return 0
    fi
    if [ -f /etc/razzfazz/host-hardened ]; then
        local ts
        ts=$(grep '^hardened_at=' /etc/razzfazz/host-hardened 2>/dev/null | cut -d= -f2-)
        print_substep "Host hardening already applied (${ts:-unknown date}). Skipping."
        return 0
    fi
    if [ ! -x "${SCRIPT_DIR}/scripts/harden-host.sh" ]; then
        print_warning "harden-host.sh not found or not executable — skipping host hardening"
        return 0
    fi
    if [ "$DRY_RUN" = "true" ]; then
        print_step "Would run host hardening (scripts/harden-host.sh) — no marker present"
        return 0
    fi
    print_step "Running host hardening (first time on this host)..."
    print_info "  scripts/harden-host.sh: ufw + fail2ban + sysctl + auditd + file perms"
    print_info "  SSH hardening NOT applied by default — re-run with --restrict-ssh after"
    print_info "  setting up your SSH pubkey if you want PasswordAuthentication=no."
    if sudo bash "${SCRIPT_DIR}/scripts/harden-host.sh" 2>&1 | tail -30; then
        print_success "Host hardening complete."
        return 0
    else
        print_error "Host hardening failed."
        print_info "  Inspect: sudo bash ${SCRIPT_DIR}/scripts/harden-host.sh --dry-run"
        print_info "  Bypass:  re-run razzfazz-upgrade.sh with --skip-host-updates"
        return 1
    fi
}
if ! ensure_host_hardened; then
    exit 1
fi

# Step 6: Data migrations preview (dry run only)
if [ "$DRY_RUN" = "true" ]; then
    run_data_migrations
fi

# Dry run ends here
if [ "$DRY_RUN" = "true" ]; then
    echo ""
    print_info "Dry run complete. No changes were made."
    print_info "Run without --check to perform the actual upgrade."
    exit 0
fi

# Step 6: Confirmation
echo "DEBUG[prompt_check]: FORCE=$FORCE (skip prompt iff true)" >&2
if [ "$FORCE" != true ]; then
    echo ""
    echo -e "${YELLOW}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║ Upgrade Summary                                           ║${NC}"
    echo -e "${YELLOW}╠═══════════════════════════════════════════════════════════╣${NC}"
    echo -e "${YELLOW}║${NC} From:  v${INSTALLED_VERSION} (${INSTALLED_COMMIT})"
    echo -e "${YELLOW}║${NC} To:    v${TARGET_VERSION} (${TARGET_COMMIT})"
    echo -e "${YELLOW}║${NC} Build: ${REQUIRES_BUILD:-true}  Pull: ${REQUIRES_PULL:-true}"
    echo -e "${YELLOW}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    read -p "Proceed with upgrade? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy] ]]; then
        print_warning "Upgrade aborted by user."
        # Restore .env backups
        if [ -f ".env.pre-upgrade-backup" ]; then
            cp -f ".env.pre-upgrade-backup" ".env"
            [ -f ".env.dify.pre-upgrade-backup" ] && cp -f ".env.dify.pre-upgrade-backup" ".env.dify"
            print_substep "Restored .env files."
        fi
        exit 0
    fi
fi

# Step 6b: Generate any missing secrets introduced by migrate_env / sync_dify_env
# (F-upgrade-missing-secrets-call): without this, new .env keys stay empty and
# downstream migrations / services fail when they read those values. Defined but
# unreferenced in the script prior to this fix.
# #140: capture whether SMTP_INTERNAL_PASSWORD was empty BEFORE secret-gen — if it
# gets generated now, the smtp-relay re-keys its SASL user, so any client with a
# HARDCODED old SMTP password (e.g. a Dify email/workflow node) starts failing 535.
__SMTP_PW_WAS_EMPTY=false
[ -z "$(read_env_value ".env" "SMTP_INTERNAL_PASSWORD")" ] && __SMTP_PW_WAS_EMPTY=true
generate_missing_secrets
if [ "$__SMTP_PW_WAS_EMPTY" = true ] && [ -n "$(read_env_value ".env" "SMTP_INTERNAL_PASSWORD")" ]; then
    print_warning "#140: SMTP_INTERNAL_PASSWORD was empty and is now generated — the smtp-relay re-keys its SASL user."
    print_substep "Any client with a HARDCODED SMTP password (Dify email/workflow nodes, scripts) will get 535 auth-failed until updated to the new value: grep ^SMTP_INTERNAL_PASSWORD= .env"
    journal_event "warning" "warn" "SMTP_INTERNAL_PASSWORD empty->generated; hardcoded SMTP clients need the new password (#140)"
fi

# Step 6c: Network mode (#184 WS2b) — compose the right overlay into COMPOSE_FILE
# for the box's RAZZFAZZ_NETWORK_MODE (online|proxied|offline), and back-compat-
# migrate a legacy RAZZFAZZ_CORPORATE_PROXY/RAZZFAZZ_OFFLINE box to the mode key.
# Runs BEFORE build_and_pull/restart_stack so the overlay is in effect for the
# compose ops below. Best-effort; never aborts the upgrade.
ensure_network_mode_overlay ".env"
# Step 6c.1 (#277/#280/#275): on a proxied box, REGENERATE the box-local
# corporate-proxy overlay (+ Dify SSRF-chain squid.conf) from the CURRENT
# generator so overlay-side fixes land WITHOUT a manual `rzfz setup
# --corporate-proxy`. Level-C only (no sudo/host changes, no reachability gate).
# No-op on a non-proxied box. Runs BEFORE build_and_pull so the new sidecar image
# is pulled and the refreshed NO_PROXY/CA are in effect for the restart below.
if [ "$(read_env_value ".env" RAZZFAZZ_CORPORATE_PROXY)" = "1" ]; then
    "${SCRIPT_DIR}/scripts/apply-corporate-proxy.sh" --regenerate \
        || print_warning "corporate-proxy overlay regen returned non-zero (best-effort; run 'rzfz setup --corporate-proxy' to refresh)."
fi
# #184 WS2a: compose the UNIVERSAL no-runtime-build overlay (all modes). After
# this, `docker compose up -d`/enable/disable can never build — restart_stack's
# `up` below is thus build-proof. The build_and_pull step below strips it (via
# compose_file_for_build) so the ONE intended build still has the build: contexts.
ensure_nobuild_overlay ".env"

# Step 7: Build and pull images
if ! build_and_pull; then
    print_error "Image build/pull failed. Rolling back..."
    rollback
    exit 1
fi

# Step 7b: Offline / package image verification (#184 WS5) — BEFORE restart.
# On an offline box or any package upgrade, images come from the package
# (code_update_package: docker load + SKIP_PULL=true) and the runtime never
# builds/pulls (WS2a). A missing custom/pinned image would leave a container
# un-creatable at `up`, so verify the FULL expected set first and REFUSE to
# restart with an actionable message. `rzfz verify-images` is the shared hard
# check (sibling); when it isn't present on the box we warn + continue.
if [ "${SKIP_PULL:-false}" = true ] || razzfazz_is_offline; then
    if "${SCRIPT_DIR}/rzfz" --list 2>/dev/null | grep -qx "verify-images"; then
        print_step "Verifying all expected images are present before restart (rzfz verify-images)..."
        if ! "${SCRIPT_DIR}/rzfz" verify-images; then
            print_error "One or more required images are MISSING — refusing to restart the stack (#184)."
            print_error "This box obtains images from the offline package and never builds/pulls at runtime."
            print_error "Load the missing image(s) — re-run the offline upgrade with a COMPLETE --package,"
            print_error "or 'docker load -i <image>.tar' for each — then retry 'rzfz upgrade'."
            exit 1
        fi
        print_success "All expected images present — proceeding with restart."
    else
        print_warning "rzfz verify-images not available on this box — skipping the pre-restart image check (upgrade continues)."
    fi
fi

# Step 8: Restart stack
restart_stack

# #148: flush Authentik sessions if this upgrade changed the Authentik version
# (prevents the stale-session LookupError 500 on /application/o/authorize).
flush_authentik_sessions_on_version_change

# #145/#147: reconcile outpost bindings from the host (idempotent) so a box whose
# init container couldn't run docker (restricted network) still ends up bound.
reconcile_outpost_bindings_hostside

# Step 9: Data migrations (targeted DB changes, preserves manual UI edits)
run_data_migrations

# Step 9b: M020 S07 — migrate global hermes/moltis/coding-tools volumes
# to per-user agent-manager volumes. Only acts when global volumes
# carry data; safe (idempotent) on fresh installs and re-runs.
if [ -x "${SCRIPT_DIR}/scripts/migrate-to-per-user-agents.sh" ]; then
    print_step "Migrating global agent volumes to per-user (M020 S07)..."
    if ! bash "${SCRIPT_DIR}/scripts/migrate-to-per-user-agents.sh"; then
        print_warning "Per-user agent migration exited non-zero (non-fatal)."
        print_info "See docs/migration-2026.05-per-user-agents.md for the manual runbook."
    fi
fi

# Step 9c: #226 — migrate an existing Vaultwarden SQLite DB into the shared
# Postgres (vaultwarden_db). Fully idempotent + self-guarding: no-op unless the
# vaultwarden profile is enabled AND SQLite holds data AND vaultwarden_db is
# still empty (so it skips fresh boxes and already-migrated boxes such as the
# prod hotfix). restart_stack above already recreated VW on the PG-wired compose;
# if a migration is needed the script stops VW, copies the data across, verifies
# parity, and leaves VW stopped for us to bring back up on the now-populated PG.
# A failure is non-fatal to the upgrade and leaves VW on SQLite (the script
# restarts it), so a password manager is never left broken by a bad migration.
if [ -x "${SCRIPT_DIR}/scripts/migrate-vaultwarden-to-postgres.sh" ]; then
    print_step "Migrating Vaultwarden SQLite → Postgres if needed (#226)..."
    if bash "${SCRIPT_DIR}/scripts/migrate-vaultwarden-to-postgres.sh"; then
        # Ensure VW is running on PG (the script leaves it stopped only when it
        # actually migrated; harmless if it's already up).
        docker start vaultwarden >/dev/null 2>&1 || true
    else
        print_warning "Vaultwarden SQLite→Postgres migration exited non-zero (non-fatal)."
        print_info "Vaultwarden was left on SQLite; re-run: scripts/migrate-vaultwarden-to-postgres.sh"
    fi
fi

# Step 9b: M022 / rc6.8 — build local llama-* runner images.
# All three runners (vulkan, rocm, cpu) bundle modules/llm/runners/llama-server-shim
# which translates GPUStack-style `--flag=value` backend_parameters into
# `--flag value` (the form upstream llama-server requires). Idempotent —
# docker build skips unchanged layers; we also skip entirely if the tagged
# image is already present.
COMPOSE_PROFILES_NOW=$(grep '^COMPOSE_PROFILES=' .env 2>/dev/null | cut -d= -f2- | tr -d '"')
HARDWARE_NOW=$(grep '^HARDWARE=' .env 2>/dev/null | cut -d= -f2- | tr -d '"')
if razzfazz_is_offline; then
    # #184 WS2b: llama-runner images are loaded from the offline package, never
    # built on an air-gapped box (the build pulls a base image + apt).
    print_substep "Step 9b: OFFLINE (RAZZFAZZ_NETWORK_MODE=offline) — skipping llama-runner image builds (loaded from the offline package)."
elif echo "${COMPOSE_PROFILES_NOW:-}" | tr ',' '\n' | grep -qx "llm" \
   && [ -d "${SCRIPT_DIR}/modules/llm/runners" ]; then
    _build_runner_img() {
        local img="$1" df="$2"
        if docker image inspect "$img" >/dev/null 2>&1; then
            return 0
        fi
        print_step "Building $img (llama-server-shim runner image)..."
        if docker build -t "$img" -f "${SCRIPT_DIR}/$df" "${SCRIPT_DIR}/modules/llm/runners"; then
            print_success "$img built."
        else
            print_warning "Failed to build $img; the matching custom backend will not be usable."
        fi
    }
    if [ "${HARDWARE_NOW:-amd}" = "amd" ]; then
        _build_runner_img "llama-vulkan-runner:b8943"     "modules/llm/runners/llama-vulkan/Dockerfile"
        _build_runner_img "llama-rocm-runner:rocm-7.2.1" "modules/llm/runners/llama-rocm/Dockerfile"
    fi
    if [ "${HARDWARE_NOW:-amd}" = "cpu" ]; then
        _build_runner_img "llama-cpu-runner:b8000"        "modules/llm/runners/llama-cpu/Dockerfile"
    fi
fi

# Step 9c: M018 Phase 6 / S06.4 + M022 + rc6.8 — register GPUStack custom backends.
# Three backends, all using local wrapper images that bundle the
# llama-server-shim (GPUStack `--flag=value` → `--flag value`):
#  - llama-box-vulkan-custom (M022 preferred, llama-vulkan-runner:b8943)
#  - llama-box-rocm-custom   (llama-rocm-runner:rocm-7.2.1, FROM kyuz0)
#  - llama-box-cpu-custom    (llama-cpu-runner:b8000, FROM ggml-org)
# Required by the `llm` profile because upstream gpustack v2.x ships no
# runner for gfx1151 or CPU. Idempotent — safe to re-run after every
# upgrade; reports "unchanged" when backends already match.
if echo "${COMPOSE_PROFILES_NOW:-}" | tr ',' '\n' | grep -qx "llm"; then
    print_step "Registering GPUStack custom backends (M018 / S06.4)..."
    GPUSTACK_API_KEY_NOW=$(grep '^GPUSTACK_API_KEY=' .env 2>/dev/null | cut -d= -f2-)
    GPUSTACK_PORT_NOW=$(grep '^GPUSTACK_PORT=' .env 2>/dev/null | cut -d= -f2-)
    if [ -n "$GPUSTACK_API_KEY_NOW" ] && [ -x "${SCRIPT_DIR}/modules/llm/gpustack/init-backends.py" ]; then
        if GPUSTACK_API="http://localhost:${GPUSTACK_PORT_NOW:-9090}" \
           GPUSTACK_API_KEY="$GPUSTACK_API_KEY_NOW" \
           python3 "${SCRIPT_DIR}/modules/llm/gpustack/init-backends.py"; then
            print_success "GPUStack custom backends registered."
        else
            print_warning "Backend registration exited non-zero (non-fatal)."
            print_info "Re-run: python3 modules/llm/gpustack/init-backends.py"
        fi
    else
        print_warning "GPUSTACK_API_KEY missing or init-backends.py absent — skipping."
    fi
fi

# Step 10: Post-upgrade verification
if [ "$SKIP_VERIFY" = false ]; then
    if ! verify_upgrade; then
        print_warning "Upgrade completed with issues."
        print_info "Review logs: docker compose logs -f"
        print_info "To rollback: rzfz upgrade --rollback"
    fi
fi

# Done
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                                                                  ║${NC}"
echo -e "${GREEN}║              Upgrade Complete: v${TARGET_VERSION}$(printf '%*s' $((27 - ${#TARGET_VERSION})) '')║${NC}"
echo -e "${GREEN}║                                                                  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ga.1 (upgrade day-1-green): auto-apply the idempotent post-install provisioning
# self-heal so an UPGRADED box is day-1-green, not just a fresh install. Before
# this, `rzfz upgrade` only PRINTED a reminder to run `post-install --refresh`; a
# box that skipped it kept whatever provisioning state it had — OWUI's persisted
# placeholder GPUStack key (0 models), an un-recreated model-sync (0 models),
# un-prebuilt custom-build images (later module-enable 403s on egress-restricted
# boxes), coding agents wired to the placeholder key. `--refresh` is the SAFE mode:
# it runs ONLY idempotent, non-destructive fixes and does NOT re-deploy models or
# reset Open WebUI / Dify / Gitea defaults (that is `--preset --force`, still
# operator-only below — its "already has models" short-circuit never applies to
# --refresh, so the wiring fixes run regardless of GPUStack model state). It runs
# AFTER verify (stack up + version written), is best-effort (a non-zero here warns,
# never fails the upgrade), and is skipped only in --check (no real upgrade).
if [ "$DRY_RUN" != "true" ] && [ -x "${SCRIPT_DIR}/rzfz" ]; then
    print_step "Applying post-upgrade provisioning self-heal (rzfz post-install --refresh)..."
    if "${SCRIPT_DIR}/rzfz" post-install --refresh; then
        print_success "Post-upgrade provisioning self-heal applied."
    else
        print_warning "Post-upgrade provisioning self-heal reported issues (non-fatal)."
        print_info "  Re-run manually: rzfz post-install --refresh"
    fi
    echo ""
fi

# Post-upgrade guidance. The idempotent --refresh self-heal above already ran; the
# notes below only surface the OTHER (read-only / operator-only) follow-up modes.
echo -e "${YELLOW}Post-upgrade notes${NC}"
echo "  The idempotent 'rzfz post-install --refresh' self-heal ran automatically:"
echo "    • Refreshed /etc/hosts entries for any new <module>.${MAIN_DOMAIN:-<domain>} FQDNs"
echo "    • Auto-provisioned GPUSTACK_API_KEY if it was still empty/placeholder"
echo "    • Re-wired Open WebUI / model-sync / Dify / coding agents to the live key"
echo "    • Pre-built the custom-build module images (offline-safe module enable)"
echo "  Re-run it any time — it is safe + idempotent."
echo ""
echo "  Other safe modes:"
echo "    rzfz post-install --verify    Read-only verification suite (no changes)"
echo ""
echo -e "  ${YELLOW}NOT recommended on an existing stack:${NC}"
echo "    --preset standard|developer   Destructive — will only run with --force."
echo "                                  Re-deploys default models, resets Open WebUI /"
echo "                                  Dify / Gitea defaults, re-installs Dify plugins."
echo ""

echo "Upgrade log saved to: ${UPGRADE_LOG}"
echo ""
echo "Useful commands:"
echo "  • View status:    docker compose ps"
echo "  • Assessment:     rzfz upgrade --status   (or rzfz status)"
echo "  • View logs:      docker compose logs -f"
echo "  • Rollback:       rzfz upgrade --rollback"
echo ""

# Auto-print the assessment so the operator sees the post-upgrade posture
# without having to ask for it. Skipped in --check (already not invoking
# real upgrade) and on --skip-verify (operator opted out of all checks).
if [ -x "${SCRIPT_DIR}/rzfz" ] && [ "$SKIP_VERIFY" != true ]; then
    echo ""
    "${SCRIPT_DIR}/rzfz" status --short 2>/dev/null || true
    echo ""
    echo "  (run 'rzfz status' for the full per-item report)"
    echo ""
fi

# M032-S06: opt-in post-upgrade acceptance probes. Runs LAST so every
# upgrade step (code update, image rebuild/pull, .env migration, post-
# upgrade verify, status print) has finished. Skipped in --check (no real
# upgrade happened) and skipped if --skip-verify (operator opted out of
# all post-upgrade checks). Probe failure surfaces as exit 4.
if [ "$WITH_ACCEPTANCE" = true ]; then
    if [ "$DRY_RUN" = "true" ]; then
        print_info "--with-acceptance is a no-op in --check / --dry-run mode."
    elif [ "$SKIP_VERIFY" = true ]; then
        print_warning "--skip-verify was set; skipping --with-acceptance probes too."
    else
        if run_acceptance_probes; then
            :
        else
            ACCEPTANCE_RC=$?
            echo ""
            print_error "Upgrade body completed, but acceptance probes failed (exit code $ACCEPTANCE_RC)."
            print_info "Review tests/results/latest/acceptance-report.md and decide whether to roll back."
            echo "Completed (with probe failures): $(date -Iseconds)" >> "$UPGRADE_LOG"
            exit "$ACCEPTANCE_RC"
        fi
    fi
fi

echo "Completed: $(date -Iseconds)" >> "$UPGRADE_LOG"
