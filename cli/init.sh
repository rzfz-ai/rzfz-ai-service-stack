#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz.ai Stack - First-Time Initialization Script
# ==============================================================================
# This script handles the first-time setup of the rzfz.ai service stack.
# It automatically generates secure secrets, configures basic settings, and
# starts all services.
#
# This script should be run on a clean Ubuntu 24.04 installation after cloning
# the git repository.
#
# Usage:
#   rzfz init                          # Interactive setup
#   rzfz init --domain example.com     # Non-interactive with parameters
#   rzfz init --factory-default        # Reset everything and start fresh
#   rzfz init --help                   # Show all options
#
# Package Presets (shortcut for common configurations):
#   rzfz init --package single-box --domain myai.local --password 'Secret123!'
#   rzfz init --package master-cpu --domain myai.com --password 'Secret123!'
#   rzfz init --package testvm-cpu --domain myai.local --password 'Secret123!'
#   rzfz init --package worker-box --domain worker.local --password 'Secret123!' \
#                      --gpustack-server-url http://master:9090 --gpustack-token TOKEN
#
# ==============================================================================

set -eo pipefail

# ==============================================================================
# M026 / S02 #8: source the shared library for colors, print_*,
# update_env_value, and secret generators. This is the first-time-setup
# script (~2000 lines, runs at fresh install AND on `--package single-box`
# etc.) — see scripts/lib.sh for what's pulled in.
#
# Local overrides defined further down INTENTIONALLY shadow lib helpers:
#   - print_banner: lib has _print_banner_frame, but the historical razzfazz
#     init banner has asymmetric padding (14/12 inside the rim) that the
#     shared frame helper would emit symmetrically (13/13). Kept local to
#     preserve byte-identical first-run output.
#
# Removed (now provided by lib.sh, identical semantics for our use):
#   - color codes (RED/GREEN/YELLOW/BLUE/CYAN/MAGENTA/NC)
#   - print_step / print_substep / print_success / print_warning /
#     print_error / print_info
#   - generate_secret / generate_password / generate_hex_secret
#   - update_env_value (lib's version escapes `\` and `|` in addition to `&`;
#     init's old version only escaped `/&`. With `|` as the sed delimiter, `/`
#     does not need escaping and `\`/`|` in env values would have silently
#     mis-escaped before — strict superset of behavior.)
#
# init.sh has no env-snapshot side-effect on update_env_value (verified with
# grep), so no override is needed for that — unlike post-install.sh.
# ==============================================================================

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

# Default values
DEFAULT_DOMAIN="localhost"
DEFAULT_TIMEZONE="Europe/Berlin"
DEFAULT_ADMIN_PASSWORD=""
DEFAULT_PROFILES="chat,dify,llm,monitor,searxng,stts,gotenberg"
DEFAULT_SCENARIO="base"

# ------------------------------------------------------------------------------
# Release-channel / box-role constant (#27/#28)
# ------------------------------------------------------------------------------
# PUBLIC/customer boxes clone the public Codeberg mirror; INTERNAL fleet boxes
# clone the SEQIS Gitea. `.env.example` ships RAZZFAZZ_CHANNEL=internal (fleet
# default), but a customer who clones Codeberg + runs `rzfz init` would stay
# `internal` and their first `rzfz upgrade` would abort trying to redirect origin
# to git.razzfazz.ai. Fix: auto-detect the origin remote at init time (below) and
# flip the generated .env to RAZZFAZZ_CHANNEL=public when origin is the Codeberg
# mirror. Single-sourced + `.env`-overridable via scripts/lib.sh (#27) so the
# value stays in lockstep with cli/upgrade.sh and a staging/fork box can point at
# an alternate Codeberg via RAZZFAZZ_PUBLIC_REMOTE in .env (or the environment).
RAZZFAZZ_PUBLIC_REMOTE="$(razzfazz_public_remote)"

# Configuration variables (can be set via CLI or interactively)
CONFIG_DOMAIN=""
CONFIG_TIMEZONE=""
CONFIG_ADMIN_PASSWORD=""
CONFIG_PROFILES=""
CONFIG_ADMIN_EMAIL=""
CONFIG_SCENARIO=""
CONFIG_GOOGLE_CLIENT_ID=""
CONFIG_GOOGLE_CLIENT_SECRET=""
CONFIG_ENTRA_ENABLED="false"
CONFIG_ENTRA_CLIENT_ID=""
CONFIG_ENTRA_CLIENT_SECRET=""
CONFIG_ENTRA_TENANT_ID=""
CONFIG_ENTRA_OAUTH_DOMAIN=""
CONFIG_PACKAGE=""
CONFIG_HARDWARE=""
CONFIG_SMTP_MODE=""
CONFIG_SMTP_RELAY_HOST=""
CONFIG_SMTP_RELAY_USERNAME=""
CONFIG_SMTP_RELAY_PASSWORD=""

# Flags
FACTORY_DEFAULT=false
SKIP_BUILD=false
SKIP_INTERACTIVE=false
REGENERATE_SECRETS=true
FORCE_REINIT=false
# M032-S06: opt-in post-deploy acceptance probe hook. When set, after a
# successful init the wrapper waits for containers to become healthy
# (5 min default) and runs `rzfz test --ci-mode --acceptance all`.
# Probe failures cause init to exit 4 (probes-failed, distinct from 1).
WITH_ACCEPTANCE=false
WITH_ACCEPTANCE_INCLUDE_DISABLED=false

# ==============================================================================
# Utility Functions
# ==============================================================================
# print_step/substep/success/warning/error/info come from scripts/lib.sh.
# print_banner is kept locally (see top-of-file note) for byte-identical
# first-run output with asymmetric 14/12 inner padding.
print_banner() {
    echo -e "${RED}"
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║                                                                  ║"
    echo "║                 rzfz.ai Service Stack Initialization             ║"
    echo "║                                                                  ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# ==============================================================================
# Help Function
# ==============================================================================
show_help() {
    echo "rzfz.ai Stack - First-Time Initialization Script"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Package Presets:"
    echo "  --package PACKAGE         Use a predefined configuration package."
    echo "                            Only --domain and --password are required additionally."
    echo "                            Available packages:"
    echo "                            single-box: Base scenario, self-signed TLS, llm-legacy (HARDWARE=amd),"
    echo "                                        chat,dify,monitor,searxng,stts,gotenberg,gitea"
    echo "                                        GPUStack standalone (v0.7.1 + custom Vulkan build)"
    echo "                            master-cpu: Google scenario, Let's Encrypt TLS, llm-cpu (HARDWARE=cpu),"
    echo "                                        chat,dify,monitor,searxng,stts,gotenberg"
    echo "                                        GPUStack master (v0.7.1-cpu)"
    echo "                            testvm-cpu: Base scenario, self-signed TLS, llm-cpu (HARDWARE=cpu),"
    echo "                                        chat,dify,monitor,searxng,stts,gotenberg,gitea"
    echo "                                        GPUStack master (v0.7.1-cpu)"
    echo "                            worker-box: Base scenario, self-signed TLS, llm-legacy (HARDWARE=amd) + monitor,"
    echo "                                        GPUStack worker (auto-enables --force)"
    echo "                                        (requires --gpustack-server-url and --gpustack-token)"
    echo ""
    echo "                            Add --llm-experimental to any preset to opt into the v2.1.x"
    echo "                            EXPERIMENTAL runtime (\`llm\` profile) instead of the stable"
    echo "                            \`llm-legacy\` / \`llm-cpu\` defaults. See M029 for context."
    echo ""
    echo "Configuration Options:"
    echo "  -d, --domain DOMAIN       Set the main domain (e.g., example.com)"
    echo "  -t, --timezone TIMEZONE   Set timezone (e.g., Europe/Berlin, America/New_York)"
    echo "  -p, --password PASSWORD   Set admin password for Authentik, GPUStack, Komodo, Dify"
    echo "  -e, --email EMAIL         Set admin email for SSL certificates"
    echo "  --profiles PROFILES       Comma-separated list of profiles to enable"
    echo "                            Available: chat,dify,llm,llm-cpu,llm-legacy,"
    echo "                                       monitor,searxng,stts,gotenberg,gitea"
    echo "                            llm-legacy — STABLE: GPUStack v0.7.1 + custom AMD Vulkan build."
    echo "                                         Default for AMD Strix Halo (M029-S04)."
    echo "                            llm-cpu    — STABLE: GPUStack v0.7.1-cpu (CPU-only)."
    echo "                                         Default for CPU deployments (M029-S04)."
    echo "                            llm        — EXPERIMENTAL: GPUStack v2.1.x. Opt-in via"
    echo "                                         --llm-experimental; auto-picks AMD/NVIDIA/CPU"
    echo "                                         runner via HARDWARE. Has known stop/start"
    echo "                                         memory leak (rc6.7 #48 / upstream PR #5255)."
    echo "  --llm-experimental        Use GPUStack v2.1.x EXPERIMENTAL (\`llm\` profile) instead of"
    echo "                            the stable v0.7.1 default. NVIDIA installs always use \`llm\`"
    echo "                            (vLLM upstream); --llm-experimental is a no-op there."
    echo "  --hardware HARDWARE       Hardware target for the llm profile (amd|nvidia|cpu)."
    echo "                            Auto-detected if omitted (lspci / nvidia-smi / /dev/kfd)."
    echo "                            Selects modules/llm/compose.devices.<HARDWARE>.yml as the overlay."
    echo "  --scenario SCENARIO       Authentik deployment scenario (base or google)"
    echo "                            base: Standard username/password authentication"
    echo "                            google: Google SSO integration"
    echo "  --tls-mode MODE           TLS certificate mode (letsencrypt, selfsigned, or certificate)"
    echo "                            letsencrypt: Real certificates from Let's Encrypt (default)"
    echo "                            selfsigned: Self-signed certificates for local/testing"
    echo "                            certificate: Use your own wildcard certificate"
    echo "                                         Place cert.pem and key.pem in ./certs/"
    echo "  --gpustack-mode MODE      GPUStack network mode (standalone, master, or worker)"
    echo "                            standalone: All ports bound to localhost (default)"
    echo "                            master: All ports exposed for external worker connections"
    echo "                            worker: Connect to a remote GPUStack master server"
    echo "  --gpustack-server-url URL Master server URL (required for worker mode)"
    echo "  --gpustack-token TOKEN    Master server token (required for worker mode)"
    echo "  --google-client-id ID     Google OAuth Client ID (required for google scenario)"
    echo "  --google-client-secret S  Google OAuth Client Secret (required for google scenario)"
    echo "  --enable-entra            Enable Microsoft Entra ID SSO (requires --entra-* credentials)"
    echo "  --entra-client-id ID      Entra App Registration client ID"
    echo "  --entra-client-secret S   Entra App Registration client secret"
    echo "  --entra-tenant-id ID      Entra Directory (tenant) ID"
    echo "  --entra-oauth-domain D    Optional: restrict enrollment to this email domain (e.g. corp.com)"
    echo "  --smtp-mode MODE          SMTP relay mode: relay (via external SMTP) or direct (to MX servers)"
    echo "  --smtp-relay-host HOST    External SMTP relay host (e.g., smtp.gmail.com)"
    echo "  --smtp-relay-user USER    SMTP relay username (e.g., email address)"
    echo "  --smtp-relay-pass PASS    SMTP relay password (e.g., app password)"
    echo ""
    echo "Installation Options:"
    echo "  --skip-build              Skip docker build and pull steps"
    echo "  --skip-interactive        Skip interactive prompts (use defaults/CLI values)"
    echo "  --no-secrets              Don't regenerate secrets (use existing values)"
    echo "  --force                   Force re-initialization on existing installation"
    echo ""
    echo "Maintenance Options:"
    echo "  --factory-default         🔴 DATA-LOSS: delete ALL data (volumes,"
    echo "                            databases, secrets) and start fresh. Irreversible."
    echo "                            Alias: --factory-reset"
    echo ""
    echo "Verification Options (M032-S06):"
    echo "  --with-acceptance         After successful init, wait for containers"
    echo "                            to become healthy (5 min) then run"
    echo "                            rzfz test --ci-mode --acceptance all."
    echo "                            Probe failures cause init to exit 4"
    echo "                            (distinct from 1 = init-failed). Tests only"
    echo "                            currently-enabled profiles."
    echo "  --with-acceptance-include-disabled"
    echo "                            Same as --with-acceptance but ALSO cycles each"
    echo "                            disabled profile up → probe → down. Slower"
    echo "                            (~5 min per disabled profile); intended for"
    echo "                            customer handover and the release cycle."
    echo "  Override healthz timeout: RAZZFAZZ_ACCEPTANCE_HEALTHZ_TIMEOUT=600 (seconds)"
    echo ""
    echo "Other Options:"
    echo "  -h, --help                Show this help message"
    echo ""
    echo "Examples:"
    echo "  # Interactive setup (recommended for first time)"
    echo "  $0"
    echo ""
    echo "  # Fully automated setup"
    echo "  $0 --domain mycompany.ai --timezone Europe/Berlin --password 'SecurePass123!' \\"
    echo "     --profiles chat,dify,llm-cpu,monitor --skip-interactive"
    echo ""
    echo "  # Quick setup with defaults"
    echo "  $0 --domain test.local --skip-interactive"
    echo ""
    echo "  # Package presets (recommended for most users)"
    echo "  $0 --package single-box --domain myai.local --password 'MyPassword!'"
    echo "  $0 --package master-cpu --domain myai.com --password 'MyPassword!'"
    echo "  $0 --package testvm-cpu --domain myai.local --password 'MyPassword!'"
    echo "  $0 --package worker-box --domain worker.local --password 'MyPassword!' \\"
    echo "     --gpustack-server-url http://master:9090 --gpustack-token TOKEN"
    echo ""
    exit 0
}

# ==============================================================================
# Check for Existing Installation
# ==============================================================================
check_existing_installation() {
    local is_existing=false
    local indicators=()
    local has_live_state=false

    # Check for Docker volumes that indicate a running/previous installation
    if docker volume ls --format '{{.Name}}' 2>/dev/null | grep -q "gpustack-data\|authentik\|postgres"; then
        is_existing=true
        has_live_state=true
        indicators+=("Docker volumes with data found")
    fi

    # Check for running containers
    if docker compose ps --format '{{.Name}}' 2>/dev/null | grep -q "authentik\|postgres\|gpustack\|caddy"; then
        is_existing=true
        has_live_state=true
        indicators+=("Running containers detected")
    fi

    # Check if .env has non-default secrets. BUG-1 (2026-05-16): this signal
    # alone is not enough to fire the guard — `docker compose down -v
    # --remove-orphans` wipes volumes + containers but leaves .env on disk,
    # so an operator who just wiped the box would be blocked from running
    # init.sh again without --force. Only treat secrets-in-.env as
    # "existing install" if there is also live state to protect.
    if [ -f ".env" ]; then
        local current_pg_pw
        current_pg_pw=$(grep -E "^POSTGRES_PASSWORD=" .env 2>/dev/null | cut -d'=' -f2-)
        local default_pg_pw
        default_pg_pw=$(grep -E "^POSTGRES_PASSWORD=" config/.env.example 2>/dev/null | cut -d'=' -f2-)
        if [ -n "$current_pg_pw" ] && [ "$current_pg_pw" != "$default_pg_pw" ] && [ ${#current_pg_pw} -gt 16 ]; then
            if [ "$has_live_state" = true ]; then
                indicators+=("Secrets already generated in .env")
            else
                print_warning "Stale secrets found in .env (no live volumes/containers) — regenerating as fresh install"
                # Stale .env: replace with fresh .env.example template so secret
                # generation has a valid file to write to. setup_env_files()
                # already ran above and saw the stale file as "exists" — we
                # need to overwrite, not just delete.
                if [ -f "config/.env.example" ]; then
                    cp config/.env.example .env
                fi
                if [ -f "config/.env.dify.example" ]; then
                    cp config/.env.dify.example .env.dify
                elif [ -f "modules/dify/.env.example" ]; then
                    cp modules/dify/.env.example .env.dify
                fi
                print_substep "Reset .env + .env.dify from templates"
            fi
        fi
    fi

    if [ "$is_existing" = true ]; then
        echo ""
        echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${YELLOW}║                  ⚠ Existing Installation Detected               ║${NC}"
        echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        for indicator in "${indicators[@]}"; do
            print_warning "$indicator"
        done
        echo ""
        print_warning "Running razzfazz-init.sh on an existing installation will:"
        echo -e "  ${RED}•${NC} Regenerate ALL secrets (database passwords, API keys, tokens)"
        echo -e "  ${RED}•${NC} Break connections to existing databases"
        echo -e "  ${RED}•${NC} Invalidate all active sessions and tokens"
        echo -e "  ${RED}•${NC} Potentially make existing data inaccessible"
        echo ""

        if [ "$FORCE_REINIT" = true ]; then
            print_warning "Proceeding with --force flag. Existing data may be affected."
            echo ""
            return 0
        fi

        if [ "$SKIP_INTERACTIVE" = true ]; then
            print_error "Existing installation detected. Use --force to override or --no-secrets to skip secret regeneration."
            exit 1
        fi

        echo -e "${CYAN}Options:${NC}"
        echo "  1) Abort - Exit without making changes (recommended)"
        echo "  2) Reconfigure only - Change settings without regenerating secrets"
        echo "  3) Full re-init - Regenerate everything (DANGER: may break existing data)"
        echo ""
        read -p "  Select option [1]: " reinit_choice
        case "$reinit_choice" in
            2)
                REGENERATE_SECRETS=false
                print_info "Continuing without secret regeneration. Only configuration will be updated."
                ;;
            3)
                echo ""
                read -p "  Type 'I UNDERSTAND' to confirm full re-initialization: " reinit_confirm
                if [ "$reinit_confirm" != "I UNDERSTAND" ]; then
                    print_error "Re-initialization aborted."
                    exit 1
                fi
                print_warning "Full re-initialization confirmed."
                ;;
            *)
                print_info "Aborted. No changes were made."
                echo ""
                echo "To reconfigure without risks, use:"
                echo "  • Configuration Portal: https://config.\$(your-domain)"
                echo "  • CLI: $0 --no-secrets --skip-build"
                exit 0
                ;;
        esac
        echo ""
    fi
}

# ==============================================================================
# Check System Prerequisites
# ==============================================================================
check_system_prerequisites() {
    print_step "Checking system prerequisites..."
    local missing_deps=()

    # Check Docker
    if ! command -v docker &> /dev/null; then
        missing_deps+=("docker")
        print_error "Docker is not installed."
    else
        DOCKER_VERSION=$(docker --version | grep -oP '\d+\.\d+' | head -1)
        print_substep "Docker version: $DOCKER_VERSION"
    fi

    # Check Docker Compose v2
    if ! docker compose version &> /dev/null; then
        missing_deps+=("docker-compose-v2")
        print_error "Docker Compose v2 is not available."
    else
        COMPOSE_VERSION=$(docker compose version --short 2>/dev/null || echo "unknown")
        print_substep "Docker Compose version: $COMPOSE_VERSION"
    fi

    # Check if Docker daemon is running
    if ! docker info &> /dev/null; then
        print_error "Docker daemon is not running or current user lacks permissions."
        print_warning "Try: sudo systemctl start docker && sudo usermod -aG docker \$USER"
        exit 1
    fi

    # Check openssl for secret generation
    if ! command -v openssl &> /dev/null; then
        missing_deps+=("openssl")
        print_error "openssl is not installed (required for secret generation)."
    else
        print_substep "openssl available"
    fi

    # M032 S01-DEC-03 (operator-resolved 2026-05-15 → option 1):
    # Check python3 + python3-venv for the test framework's bootstrap_test_venv()
    # helper called at end of init. Without python3-venv, `python3 -m venv` fails
    # with a friendly-but-non-actionable apt-install hint.
    if ! command -v python3 &> /dev/null; then
        missing_deps+=("python3")
        print_error "python3 is not installed (required for test framework bootstrap)."
    else
        PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        print_substep "python3 version: $PYTHON_VERSION"
        # BUG-2 (2026-05-16): the previous `python3 -c 'import venv'` check
        # passes even when the venv module exists but `ensurepip` is missing
        # (the actual blocker for `python3 -m venv` on Ubuntu without
        # python3-venv installed). Exercise the real workload instead:
        # create + delete a throwaway venv.
        _venv_probe="/tmp/_razzfazz_init_venv_probe.$$"
        if ! python3 -m venv "$_venv_probe" &> /dev/null; then
            rm -rf "$_venv_probe" 2>/dev/null
            VENV_PKG="python${PYTHON_VERSION}-venv"
            if command -v apt-get &> /dev/null; then
                print_warning "$VENV_PKG missing — installing via apt-get (may prompt for sudo)..."
                # Drop `-n` so apt-get can prompt for sudo password on first-init
                # boxes that don't have NOPASSWD configured. The interactive
                # prompt blocks the script but is the right behavior for a
                # foreground init.
                if sudo apt-get install -y "$VENV_PKG" python3-venv >/dev/null 2>&1; then
                    print_substep "$VENV_PKG installed"
                else
                    missing_deps+=("$VENV_PKG (sudo apt-get install $VENV_PKG)")
                    print_error "$VENV_PKG missing and auto-install failed."
                fi
            else
                missing_deps+=("$VENV_PKG (apt-get not available — install manually)")
                print_error "$VENV_PKG missing and apt-get not found."
            fi
        else
            rm -rf "$_venv_probe" 2>/dev/null
            print_substep "python3-venv functional (probe created + destroyed a venv)"
        fi
    fi

    # Check disk space
    FREE_SPACE=$(df -BG . | tail -1 | awk '{print $4}' | tr -d 'G')
    print_substep "Available disk space: ${FREE_SPACE}GB"
    if [ "$FREE_SPACE" -lt 20 ]; then
        print_warning "Low disk space! Recommended: 20GB+ free."
    fi

    # Check RAM (use -m and divide to avoid free -g rounding to 0)
    TOTAL_RAM=$(free -m 2>/dev/null | awk '/^Mem:/{printf "%.0f", $2/1024}')
    TOTAL_RAM=${TOTAL_RAM:-0}
    print_substep "Total RAM: ${TOTAL_RAM}GB"
    if [ "$TOTAL_RAM" -lt 8 ]; then
        print_warning "Low RAM! Recommended: 8GB+ (16GB+ for LLM)."
    fi

    # Exit if critical dependencies missing
    if [ ${#missing_deps[@]} -gt 0 ]; then
        echo ""
        print_error "Missing required dependencies: ${missing_deps[*]}"
        echo ""
        echo "Please install Docker first:"
        echo "  curl -fsSL https://get.docker.com | sh"
        echo "  sudo usermod -aG docker \$USER"
        exit 1
    fi

    print_success "System prerequisites checked."
}

# ==============================================================================
# Detect release channel from the `origin` remote (#27/#28)
# ==============================================================================
# Mirror of cli/upgrade.sh's channel logic: a box whose `origin` points at the
# public Codeberg mirror is a PUBLIC/customer box. `.env.example` ships the fleet
# default (RAZZFAZZ_CHANNEL=internal); on a Codeberg-origin box we flip the
# generated .env to `public` so the very first `rzfz upgrade` pulls anonymously
# from Codeberg instead of aborting on the git.razzfazz.ai origin assertion.
# Matching is robust to a trailing/absent `.git`, oauth2-token-in-URL, and a
# trailing slash: we key off the canonical `codeberg.org/rzfz-ai/...` path OR an
# exact match against $RAZZFAZZ_PUBLIC_REMOTE. Runs on $SCRIPT_DIR/.env; a no-op
# when there is no git repo, no origin, or the origin is not the Codeberg mirror.
detect_release_channel() {
    [ -f ".env" ] || return 0
    [ -d ".git" ] || return 0

    local origin
    origin=$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || echo "")
    [ -n "$origin" ] || return 0

    case "$origin" in
        "$RAZZFAZZ_PUBLIC_REMOTE" \
            | *github.com/rzfz-ai/rzfz-ai-service-stack* \
            | *codeberg.org/rzfz-ai/rzfz-ai-service-stack*)
            update_env_value ".env" "RAZZFAZZ_CHANNEL" "public"
            print_substep "origin is the public mirror (GitHub; legacy Codeberg) → RAZZFAZZ_CHANNEL=public (community/public box)"
            ;;
        *)
            : # internal/fleet or non-standard origin — keep the .env.example default
            ;;
    esac
}

# ==============================================================================
# Setup Environment Files
# ==============================================================================
setup_env_files() {
    print_step "Setting up environment files..."

    if [ ! -f ".env" ]; then
        if [ -f "config/.env.example" ]; then
            cp config/.env.example .env
            print_substep "Created .env from template"
        else
            print_error "config/.env.example not found!"
            exit 1
        fi
    else
        print_substep ".env already exists"
    fi

    # #27/#28: A box cloned from the public mirror is a PUBLIC/customer box — pin
    # RAZZFAZZ_CHANNEL=public in the generated .env so its first `rzfz upgrade`
    # pulls anonymously from the public repo instead of aborting on the
    # git.razzfazz.ai origin-redirect (which a public-origin box can never
    # satisfy). Public mirror = GitHub `rzfz-ai/rzfz-ai-service-stack` (Codeberg-exit
    # 2026-07; legacy Codeberg origins still recognised). `.env.example` ships
    # `internal` (fleet default); we only flip to `public` when origin points at
    # the public mirror.
    detect_release_channel

    if [ ! -f ".env.dify" ]; then
        if [ -f "config/.env.dify.example" ]; then
            cp config/.env.dify.example .env.dify
            print_substep "Created .env.dify from template"
        elif [ -f "modules/dify/.env.example" ]; then
            cp modules/dify/.env.example .env.dify
            print_substep "Created .env.dify from template"
        fi
    else
        print_substep ".env.dify already exists"
    fi

    print_success "Environment files ready."
}

# ==============================================================================
# Validate No Known Default Secrets Remain
# ==============================================================================
validate_no_default_secrets() {
    # Known default values that must never be used in production
    local -a KNOWN_DEFAULTS=(
        "Run4Quality!"
        "pgIx7sbk9UtlpSNO7iCFUvJJ"
        "kxio5e1XTmaR1koIBVjk60YO"
        "dify-sandbox"
        "this-is-a-very-long-secure-bootstrap-token"
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        "mailpass"
        "your-lightrag-api-key-here"
        "lightrag-jwt-secret-change-me"
        "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2"
        "licenses_static_secret_no_need_to_rotate"
        "setup_app_client_secret_static"
        "help_app_client_secret_static"
    )

    local found_defaults=false
    for default_val in "${KNOWN_DEFAULTS[@]}"; do
        if grep -qF "$default_val" .env 2>/dev/null; then
            if [ "$found_defaults" = false ]; then
                print_warning "Known default secrets detected in .env — they will be regenerated."
                found_defaults=true
            fi
        fi
    done
}

# ==============================================================================
# Generate Secure Secrets
# ==============================================================================
# generate_secret / generate_password / generate_hex_secret come from
# scripts/lib.sh — identical implementations (the lib copies were derived
# from this script's originals in S01).

# detect_hardware: emit one of "amd", "nvidia", "cpu" based on what the host
# advertises. Used by the `llm` profile (M018+) to pick the right device
# overlay (modules/llm/compose.devices.<HARDWARE>.yml). NVIDIA wins over AMD if both
# are present (rare; user can override with --hardware).
detect_hardware() {
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
        echo "nvidia"
        return
    fi
    if [ -e /dev/kfd ] || (command -v lspci >/dev/null 2>&1 && \
       lspci -nn 2>/dev/null | grep -qiE "VGA.*(AMD|ATI|Radeon)|Display.*(AMD|ATI|Radeon)"); then
        echo "amd"
        return
    fi
    echo "cpu"
}

# update_env_value comes from scripts/lib.sh (lib's escapes `\&|` for use
# with `|` as the sed delimiter — strict superset of the historical
# `/&` escape that lived here, since `/` does not need escaping with a
# `|` delimiter).

regenerate_all_secrets() {
    print_step "Generating cryptographically secure secrets..."
    print_warning "This is required for a secure installation."
    echo ""

    # .env secrets
    print_substep "Generating WEBUI_SECRET_KEY..."
    update_env_value ".env" "WEBUI_SECRET_KEY" "$(generate_hex_secret 32)"

    # #30 (True-SSO): Open WebUI NATIVE OIDC client credentials. Seeded
    # unconditionally (like VAULTWARDEN_CLIENT_SECRET) so that flipping
    # ENABLE_OPENWEBUI_OIDC=true later "just works" — init-authentik.sh only
    # applies the openwebui-oidc blueprint when BOTH id and secret are non-empty
    # (else it skips with a WARNING), and the blueprint + compose reference these
    # exact vars. Stable client_id (matches the vaultwarden convention), random
    # hex secret. NOTE: distinct from CHAT_CLIENT_SECRET, which is the Caddy
    # forward-auth proxy secret — this pair is OWUI's own OIDC login.
    print_substep "Generating OPENWEBUI_OIDC_CLIENT_ID/SECRET (#30 native OIDC)..."
    update_env_value ".env" "OPENWEBUI_OIDC_CLIENT_ID" "openwebui_oidc_client"
    update_env_value ".env" "OPENWEBUI_OIDC_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating AUTHENTIK_SECRET_KEY..."
    update_env_value ".env" "AUTHENTIK_SECRET_KEY" "$(generate_secret 42)"

    print_substep "Generating AUTHENTIK_BOOTSTRAP_TOKEN..."
    update_env_value ".env" "AUTHENTIK_BOOTSTRAP_TOKEN" "$(generate_secret 48)"

    print_substep "Generating POSTGRES_PASSWORD..."
    update_env_value ".env" "POSTGRES_PASSWORD" "$(generate_password 24)"

    print_substep "Generating VALKEY_PASSWORD..."
    update_env_value ".env" "VALKEY_PASSWORD" "$(generate_password 24)"

    print_substep "Generating GPUSTACK_SECRET_KEY..."
    update_env_value ".env" "GPUSTACK_SECRET_KEY" "$(generate_secret 32)"

    print_substep "Generating PIPELINES_API_KEY..."
    update_env_value ".env" "PIPELINES_API_KEY" "$(generate_secret 32)"

    print_substep "Generating KOMODO_PASSKEY..."
    update_env_value ".env" "KOMODO_PASSKEY" "$(generate_password 24)"

    print_substep "Generating CHAT_CLIENT_SECRET..."
    update_env_value ".env" "CHAT_CLIENT_SECRET" "$(generate_secret 64)"

    print_substep "Generating DIFY_CLIENT_SECRET..."
    update_env_value ".env" "DIFY_CLIENT_SECRET" "$(generate_secret 64)"

    print_substep "Generating ADMIN_CLIENT_SECRET..."
    update_env_value ".env" "ADMIN_CLIENT_SECRET" "$(generate_secret 64)"

    print_substep "Generating LLM_CLIENT_SECRET..."
    update_env_value ".env" "LLM_CLIENT_SECRET" "$(generate_secret 64)"

    print_substep "Generating BACKUP_CLIENT_SECRET..."
    update_env_value ".env" "BACKUP_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating GITEA_SECRET_KEY..."
    update_env_value ".env" "GITEA_SECRET_KEY" "$(generate_hex_secret 32)"

    print_substep "Generating GITEA_INTERNAL_TOKEN..."
    update_env_value ".env" "GITEA_INTERNAL_TOKEN" "$(generate_secret 64)"

    print_substep "Generating GITEA_CLIENT_SECRET..."
    update_env_value ".env" "GITEA_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # #29 (True-SSO): Gitea NATIVE OIDC client credentials. Seeded unconditionally
    # (like VAULTWARDEN_CLIENT_SECRET) so flipping ENABLE_GITEA_AUTHENTIK_OIDC=true
    # later "just works": init-authentik.sh applies the gitea-oidc blueprint only
    # when BOTH id and secret are non-empty, and modules/gitea/init-gitea.sh runs
    # `gitea admin auth add-oauth ... --auto-discover-url .../gitea-oidc/...` with
    # them (Gitea auto-registers OIDC users). Stable client_id (vaultwarden
    # convention), random hex secret. Distinct from GITEA_CLIENT_SECRET, which is
    # the Caddy forward-auth proxy secret.
    print_substep "Generating GITEA_OIDC_CLIENT_ID/SECRET (#29 native OIDC)..."
    update_env_value ".env" "GITEA_OIDC_CLIENT_ID" "gitea_oidc_client"
    update_env_value ".env" "GITEA_OIDC_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating LIGHTRAG_API_KEY..."
    update_env_value ".env" "LIGHTRAG_API_KEY" "$(generate_secret 32)"

    print_substep "Generating LIGHTRAG_TOKEN_SECRET..."
    update_env_value ".env" "LIGHTRAG_TOKEN_SECRET" "$(generate_secret 32)"

    print_substep "Generating LIGHTRAG_CLIENT_SECRET..."
    update_env_value ".env" "LIGHTRAG_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating COGNEE_CLIENT_SECRET..."
    update_env_value ".env" "COGNEE_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating DOCLING_CLIENT_SECRET..."
    update_env_value ".env" "DOCLING_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating STIRLING_CLIENT_SECRET..."
    update_env_value ".env" "STIRLING_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # (#191) crawl4ai's forward_auth proxy provider (29-crawl4ai.yaml) keys off
    # this — was previously minted nowhere, so the provider applied with an
    # empty client_secret. Same hex32 shape as the other forward_auth modules.
    print_substep "Generating CRAWL4AI_CLIENT_SECRET..."
    update_env_value ".env" "CRAWL4AI_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # Paperclip secrets
    print_substep "Generating PAPERCLIP_DB_PASSWORD..."
    update_env_value ".env" "PAPERCLIP_DB_PASSWORD" "$(generate_hex_secret 16)"
    print_substep "Generating PAPERCLIP_BETTER_AUTH_SECRET (permanent - never rotated)..."
    update_env_value ".env" "PAPERCLIP_BETTER_AUTH_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating PAPERCLIP_CLIENT_SECRET..."
    update_env_value ".env" "PAPERCLIP_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # Synapse Matrix homeserver secrets
    print_substep "Generating SYNAPSE_DB_PASSWORD..."
    update_env_value ".env" "SYNAPSE_DB_PASSWORD" "$(generate_hex_secret 16)"
    print_substep "Generating SYNAPSE_REGISTRATION_SHARED_SECRET..."
    update_env_value ".env" "SYNAPSE_REGISTRATION_SHARED_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating SYNAPSE_MACAROON_SECRET_KEY (permanent - never rotated)..."
    update_env_value ".env" "SYNAPSE_MACAROON_SECRET_KEY" "$(generate_hex_secret 32)"
    print_substep "Generating SYNAPSE_FORM_SECRET..."
    update_env_value ".env" "SYNAPSE_FORM_SECRET" "$(generate_hex_secret 16)"
    print_substep "Generating SYNAPSE_CLIENT_SECRET..."
    update_env_value ".env" "SYNAPSE_CLIENT_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating MATRIX_CLIENT_ID..."
    update_env_value ".env" "MATRIX_CLIENT_ID" "$(generate_hex_secret 16)"
    print_substep "Generating ELEMENT_WEB_CLIENT_SECRET..."
    update_env_value ".env" "ELEMENT_WEB_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # M020 S07 — moltis + hermes secrets removed; both moved to per-user via
    # agent-manager. The provisioner generates per-instance secrets at launch
    # and stores them in the per-instance config JSONB column.

    print_substep "Generating PAPERLESS_SECRET_KEY..."
    update_env_value ".env" "PAPERLESS_SECRET_KEY" "$(generate_secret 64 | tr -d '/+=')"

    print_substep "Generating PAPERLESS_CLIENT_SECRET..."
    update_env_value ".env" "PAPERLESS_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating FALKORDB_PASSWORD..."
    update_env_value ".env" "FALKORDB_PASSWORD" "$(generate_secret 32)"

    # VAULTWARDEN_ADMIN_TOKEN intentionally left EMPTY — an empty token DISABLES the
    # /admin server panel (Vaultwarden logs "admin page will be disabled" and serves a
    # disabled page; it does NOT open token-free without an explicit DISABLE_ADMIN_TOKEN).
    # /admin is a shared-secret, user-agnostic backdoor with no day-to-day benefit — all
    # admin work is done via Authentik SSO Org-Owner roles. To enable it for break-glass,
    # set VAULTWARDEN_ADMIN_TOKEN to an Argon2 hash from `vaultwarden hash` (not plaintext).
    print_substep "VAULTWARDEN_ADMIN_TOKEN left empty (/admin panel disabled by default)."

    print_substep "Generating VAULTWARDEN_CLIENT_SECRET..."
    update_env_value ".env" "VAULTWARDEN_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating INFISICAL_ENCRYPTION_KEY..."
    update_env_value ".env" "INFISICAL_ENCRYPTION_KEY" "$(generate_hex_secret 16)"

    print_substep "Generating INFISICAL_AUTH_SECRET..."
    update_env_value ".env" "INFISICAL_AUTH_SECRET" "$(generate_secret 42)"

    print_substep "Generating INFISICAL_CLIENT_SECRET..."
    update_env_value ".env" "INFISICAL_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating ONYX_SECRET..."
    update_env_value ".env" "ONYX_SECRET" "$(generate_secret 42)"

    print_substep "Generating ONYX_CLIENT_SECRET..."
    update_env_value ".env" "ONYX_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating OPENHANDS_CLIENT_SECRET..."
    update_env_value ".env" "OPENHANDS_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # M020 S07 — coding-tools moved to per-user via agent-manager;
    # OPENCODE_SERVER_PASSWORD is generated by the provisioner per instance.

    print_substep "Generating PLUGIN_DAEMON_KEY..."
    update_env_value ".env" "PLUGIN_DAEMON_KEY" "$(generate_secret 42)"

    print_substep "Generating PLUGIN_DIFY_INNER_API_KEY..."
    update_env_value ".env" "PLUGIN_DIFY_INNER_API_KEY" "$(generate_secret 42)"

    print_substep "Generating SANDBOX_API_KEY..."
    update_env_value ".env" "SANDBOX_API_KEY" "$(generate_hex_secret 32)"

    # Per-service database user passwords (F-009)
    print_substep "Generating per-service database passwords..."
    update_env_value ".env" "AUTHENTIK_DB_PASSWORD" "$(generate_password 24)"
    update_env_value ".env" "OPENWEBUI_DB_PASSWORD" "$(generate_password 24)"
    update_env_value ".env" "GPUSTACK_DB_PASSWORD" "$(generate_password 24)"
    update_env_value ".env" "DIFY_DB_PASSWORD" "$(generate_password 24)"
    update_env_value ".env" "DIFY_PLUGIN_DB_PASSWORD" "$(generate_password 24)"
    update_env_value ".env" "GITEA_DB_PASSWORD" "$(generate_password 24)"
    update_env_value ".env" "PAPERLESS_DB_PASSWORD" "$(generate_password 24)"
    update_env_value ".env" "INFISICAL_DB_PASSWORD" "$(generate_password 24)"
    update_env_value ".env" "ONYX_DB_PASSWORD" "$(generate_password 24)"

    # Authentik proxy provider secrets for Licenses, Setup, Help UIs (F-038)
    print_substep "Generating LICENSES_CLIENT_SECRET..."
    update_env_value ".env" "LICENSES_CLIENT_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating SETUP_CLIENT_SECRET..."
    update_env_value ".env" "SETUP_CLIENT_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating HELP_CLIENT_SECRET..."
    update_env_value ".env" "HELP_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # Observability profile (M019) — OpenLIT + ClickHouse
    print_substep "Generating OBSERVABILITY_CLIENT_SECRET..."
    update_env_value ".env" "OBSERVABILITY_CLIENT_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating CLICKHOUSE_PASSWORD..."
    update_env_value ".env" "CLICKHOUSE_PASSWORD" "$(generate_password 24)"

    # Mac LLM gateway (#168) — LiteLLM master key (the gateway's only auth).
    # Mint unconditionally (like CLICKHOUSE_PASSWORD) so the opt-in mac-llm
    # profile never comes up fail-open with an empty master_key (security M-2).
    print_substep "Generating MAC_GATEWAY_MASTER_KEY..."
    update_env_value ".env" "MAC_GATEWAY_MASTER_KEY" "$(generate_password 32)"
    # #168: seed the gateway config.yaml from the template so the :ro bind mount
    # (modules/llm/mac-gateway/compose.yml) mounts a FILE, not a Docker-created
    # DIRECTORY. Without a pre-existing file, `docker compose up llm-mac-gateway`
    # makes config.yaml a dir, and the config UI's atomic write (config.yaml.tmp +
    # os.replace) then 500s with IsADirectoryError — the whole Mac-backend panel
    # breaks. Seed unconditionally (harmless when mac-llm is disabled; gitignored).
    if [ ! -e modules/llm/mac-gateway/config.yaml ] && [ -f modules/llm/mac-gateway/config.example.yaml ]; then
        cp modules/llm/mac-gateway/config.example.yaml modules/llm/mac-gateway/config.yaml
        print_substep "  Seeded modules/llm/mac-gateway/config.yaml from template (#168)."
    fi

    # Personal MCP Manager (#36) — AES-256-GCM master key for the per-user
    # credential vault (base64 of 32 random bytes = a valid AES-256 key), the
    # mcp.<domain> forward-auth client secret, and the manager DB password.
    print_substep "Generating MCP_MANAGER_SECRET_KEY..."
    update_env_value ".env" "MCP_MANAGER_SECRET_KEY" "$(generate_secret 32)"
    print_substep "Generating MCP_CLIENT_SECRET..."
    update_env_value ".env" "MCP_CLIENT_SECRET" "$(generate_hex_secret 32)"
    # #61 (HIGH-1): shared service-to-service secret gating mcp-manager's
    # /internal/* routes (agent-manager presents it). Without it those routes
    # fail closed (401), so it MUST be minted on every box.
    print_substep "Generating MCP_INTERNAL_TOKEN..."
    update_env_value ".env" "MCP_INTERNAL_TOKEN" "$(generate_hex_secret 32)"
    # C1 bypass fix (PR #84 re-review): "came through Caddy" proof secret shared
    # only Caddy↔agent-manager (X-Razzfazz-Proxy-Proof). Without it the manager's
    # per-instance proxy fails closed, so it MUST be minted on every box.
    print_substep "Generating MANAGER_PROXY_SECRET..."
    update_env_value ".env" "MANAGER_PROXY_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating MCP_MANAGER_DB_PASSWORD..."
    update_env_value ".env" "MCP_MANAGER_DB_PASSWORD" "$(generate_password 24)"

    # Backup encryption password (F-048 / F-A2) — deliberately re-uses the
    # stack admin password so the customer can decrypt from the sticker on the
    # box. Treat the admin password as the single recovery secret; losing it
    # also loses backup decryption.
    # NOTE: set in apply_configuration() from $CONFIG_ADMIN_PASSWORD — the actual
    # admin password is NOT available as a shell var here (this generate-secrets
    # pass runs before the admin password is resolved), so setting it here wrote
    # an EMPTY value (ga.4 0.91 finding → backups refused on fresh install).

    # Internal SMTP relay SASL password (F-012)
    print_substep "Generating SMTP_INTERNAL_PASSWORD..."
    update_env_value ".env" "SMTP_INTERNAL_PASSWORD" "$(generate_password 24)"

    # Per-service admin passwords (F-010) — each service gets a unique admin password
    print_substep "Generating GPUSTACK_ADMIN_PASSWORD..."
    update_env_value ".env" "GPUSTACK_ADMIN_PASSWORD" "$(generate_password 24)"
    print_substep "Generating KOMODO_INIT_ADMIN_PASSWORD..."
    update_env_value ".env" "KOMODO_INIT_ADMIN_PASSWORD" "$(generate_password 24)"
    print_substep "Generating DIFY_ADMIN_PASSWORD..."
    update_env_value ".env" "DIFY_ADMIN_PASSWORD" "$(generate_password 24)"
    print_substep "Generating COGNEE_ADMIN_PASSWORD..."
    update_env_value ".env" "COGNEE_ADMIN_PASSWORD" "$(generate_password 24)"
    # Gitea admin password is set from CONFIG_ADMIN_PASSWORD in apply_configuration

    # .env.dify secrets - sync shared passwords and keys from .env
    if [ -f ".env.dify" ]; then
        print_substep "Generating Dify SECRET_KEY..."
        update_env_value ".env.dify" "SECRET_KEY" "$(generate_secret 42)"

        # Sync VALKEY_PASSWORD to REDIS_PASSWORD for Dify
        local valkey_pw plugin_key inner_key
        valkey_pw=$(grep -E "^VALKEY_PASSWORD=" .env | cut -d'=' -f2-)
        if [ -n "$valkey_pw" ]; then
            print_substep "Syncing REDIS_PASSWORD to Dify..."
            update_env_value ".env.dify" "REDIS_PASSWORD" "$valkey_pw"
            update_env_value ".env.dify" "CELERY_BROKER_URL" "redis://:${valkey_pw}@valkey:6379/1"
        fi

        # Sync Plugin Daemon keys
        plugin_key=$(grep -E "^PLUGIN_DAEMON_KEY=" .env | cut -d'=' -f2-)
        inner_key=$(grep -E "^PLUGIN_DIFY_INNER_API_KEY=" .env | cut -d'=' -f2-)
        if [ -n "$plugin_key" ]; then
            print_substep "Syncing PLUGIN_DAEMON_KEY to Dify..."
            update_env_value ".env.dify" "PLUGIN_DAEMON_KEY" "$plugin_key"
        fi
        if [ -n "$inner_key" ]; then
            print_substep "Syncing PLUGIN_DIFY_INNER_API_KEY to Dify..."
            update_env_value ".env.dify" "PLUGIN_DIFY_INNER_API_KEY" "$inner_key"
        fi

        # Sync SANDBOX_API_KEY
        local sandbox_key
        sandbox_key=$(grep -E "^SANDBOX_API_KEY=" .env | cut -d'=' -f2-)
        if [ -n "$sandbox_key" ]; then
            print_substep "Syncing SANDBOX_API_KEY to Dify..."
            update_env_value ".env.dify" "SANDBOX_API_KEY" "$sandbox_key"
        fi

        # Dify INIT_PASSWORD is ONLY the first-run setup GATE, never the admin
        # login password. Dify 1.14+ caps it at 30 chars (init_validate.py:
        # password max_length=30), so it must NOT be set to the (often longer,
        # operator-generated) admin password — doing so makes /console/api/init
        # 422 forever and Dify can never be set up. Use a dedicated short token;
        # post-install creates the admin with the real box admin password.
        local cur_init_pw
        cur_init_pw=$(grep -E "^INIT_PASSWORD=" .env.dify | cut -d'=' -f2-)
        if [ -z "$cur_init_pw" ] || [ "${#cur_init_pw}" -gt 30 ]; then
            print_substep "Setting Dify INIT_PASSWORD (dedicated <=30-char setup gate)..."
            update_env_value ".env.dify" "INIT_PASSWORD" "$(generate_password 24)"
        fi

        # Sync per-service DB credentials to Dify
        local dify_db_user dify_db_pass dify_plugin_db_user dify_plugin_db_pass
        dify_db_user=$(grep -E "^DIFY_DB_USER=" .env | cut -d'=' -f2-)
        dify_db_pass=$(grep -E "^DIFY_DB_PASSWORD=" .env | cut -d'=' -f2-)
        if [ -n "$dify_db_user" ] && [ -n "$dify_db_pass" ]; then
            print_substep "Syncing Dify DB credentials to .env.dify..."
            update_env_value ".env.dify" "DB_USERNAME" "$dify_db_user"
            update_env_value ".env.dify" "DB_PASSWORD" "$dify_db_pass"
        fi
    fi

    echo ""
    print_success "All secrets generated successfully."
    print_info "Secrets are stored in .env and .env.dify"
}

# ==============================================================================
# Interactive Profile Selection with Arrow Keys
# ==============================================================================
select_profiles_interactive() {
    echo -e "${CYAN}Service Modules${NC}"
    echo "  Use ↑/↓ to navigate, SPACE to toggle, ENTER to confirm."
    echo ""

    # Profile definitions: name|description|default_selected
    # M020 S07 + M023 S06: hermes, moltis, coding-tools are NOT compose profiles —
    # they're per-user agent types provisioned by the `agents` profile via
    # agent-manager. Users provision their own instances from the My Agents
    # Authentik drawer. paperclip remains as a compose profile (still has
    # modules/apps/paperclip/compose.yml).
    local -a profile_names=("chat" "dify" "llm-cpu" "llm-box" "monitor" "searxng" "stts" "gotenberg" "gitea" "lightrag" "cognee" "paperclip" "matrix")
    local -a profile_descs=(
        "Open WebUI Chat Interface"
        "Dify Workflow Automation"
        "LLM Inference (CPU only)"
        "LLM Inference (AMD Vulkan)"
        "Komodo Container Management"
        "SearXNG Privacy Search"
        "Speech-to-Text/Text-to-Speech"
        "PDF/Document Conversion"
        "Gitea Git Repository"
        "LightRAG Knowledge Base (experimental)"
        "Cognee Knowledge Engine (experimental)"
        "Paperclip AI Company Orchestration (experimental)"
        "Matrix Homeserver + Element Web (experimental)"
    )
    # Defaults: all except llm-box, git, lightrag, cognee, and new agentic/matrix services
    local -a selected=(1 1 1 0 1 1 1 1 0 0 0 0 0)

    local num_profiles=13
    local cursor=0

    # Hide cursor
    printf '\e[?25l'

    # Draw the menu
    draw_profile_menu() {
        local i
        for ((i=0; i<num_profiles; i++)); do
            # Clear the line
            printf '\e[2K\r'

            local check=" "
            if [ "${selected[$i]}" = "1" ]; then
                check="\e[32m✓\e[0m"
            fi

            if [ $i -eq $cursor ]; then
                printf "  \e[36m▸ [%b] %-12s %s\e[0m\n" "$check" "${profile_names[$i]}" "${profile_descs[$i]}"
            else
                printf "    [%b] %-12s %s\n" "$check" "${profile_names[$i]}" "${profile_descs[$i]}"
            fi
        done
        printf '\e[2K\n'
        printf '\e[2K  \e[33m↑↓\e[0m Navigate  \e[33mSPACE\e[0m Toggle  \e[33mENTER\e[0m Confirm\n'
    }

    # Initial draw
    draw_profile_menu

    # Input loop
    while true; do
        IFS= read -rsn1 key

        if [ "$key" = $'\x1b' ]; then
            read -rsn2 -t 0.1 rest || true
            key="${key}${rest}"
        fi

        case "$key" in
            $'\x1b[A'|$'\x1bOA')  # Up arrow
                cursor=$((cursor - 1))
                [ $cursor -lt 0 ] && cursor=$((num_profiles - 1))
                printf '\e[%dA' $((num_profiles + 2))
                draw_profile_menu
                ;;
            $'\x1b[B'|$'\x1bOB')  # Down arrow
                cursor=$((cursor + 1))
                [ $cursor -ge $num_profiles ] && cursor=0
                printf '\e[%dA' $((num_profiles + 2))
                draw_profile_menu
                ;;
            ' ')  # Space - toggle
                local name="${profile_names[$cursor]}"
                if [ "$name" = "llm-cpu" ] && [ "${selected[$cursor]}" = "0" ]; then
                    selected[3]=0  # Disable llm-box
                    selected[$cursor]=1
                elif [ "$name" = "llm-box" ] && [ "${selected[$cursor]}" = "0" ]; then
                    selected[2]=0  # Disable llm-cpu
                    selected[$cursor]=1
                else
                    [ "${selected[$cursor]}" = "1" ] && selected[$cursor]=0 || selected[$cursor]=1
                fi
                printf '\e[%dA' $((num_profiles + 2))
                draw_profile_menu
                ;;
            '')  # Enter
                break
                ;;
        esac
    done

    # Show cursor
    printf '\e[?25h'

    # Build result
    local result=""
    local i
    for ((i=0; i<num_profiles; i++)); do
        if [ "${selected[$i]}" = "1" ]; then
            [ -n "$result" ] && result="${result},"
            result="${result}${profile_names[$i]}"
        fi
    done

    CONFIG_PROFILES="$result"
    echo ""
    print_success "Selected modules: $CONFIG_PROFILES"
    echo ""
}

# ==============================================================================
# Interactive Configuration
# ==============================================================================
interactive_config() {
    echo ""
    echo -e "${MAGENTA}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${MAGENTA}║                    Initial Configuration                         ║${NC}"
    echo -e "${MAGENTA}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    # Domain
    if [ -z "$CONFIG_DOMAIN" ]; then
        echo -e "${CYAN}Main Domain${NC}"
        echo "  Enter your main domain name. All services will be accessible as subdomains."
        echo "  Example: mycompany.ai → chat.mycompany.ai, dify.mycompany.ai, etc."
        echo ""
        read -p "  Domain [$DEFAULT_DOMAIN]: " input_domain
        CONFIG_DOMAIN="${input_domain:-$DEFAULT_DOMAIN}"
        echo ""
    fi

    # Timezone
    if [ -z "$CONFIG_TIMEZONE" ]; then
        echo -e "${CYAN}Timezone${NC}"
        echo "  Select your timezone for correct scheduling and logging."
        echo "  Common options: Europe/Berlin, Europe/London, America/New_York, Asia/Tokyo"
        echo ""
        read -p "  Timezone [$DEFAULT_TIMEZONE]: " input_tz
        CONFIG_TIMEZONE="${input_tz:-$DEFAULT_TIMEZONE}"
        echo ""
    fi

    # Admin Password
    if [ -z "$CONFIG_ADMIN_PASSWORD" ]; then
        echo -e "${CYAN}Admin Password${NC}"
        echo "  Set the initial admin password for Authentik, GPUStack, Komodo, and Dify."
        echo "  This should be a strong password (min 8 characters recommended)."
        echo ""
        while true; do
            read -s -p "  Admin Password: " input_pw
            echo ""
            if [ ${#input_pw} -lt 8 ]; then
                print_warning "Password should be at least 8 characters. Try again."
            else
                read -s -p "  Confirm Password: " input_pw2
                echo ""
                if [ "$input_pw" = "$input_pw2" ]; then
                    CONFIG_ADMIN_PASSWORD="$input_pw"
                    break
                else
                    print_warning "Passwords don't match. Try again."
                fi
            fi
        done
        echo ""
    fi

    # Admin Email (optional)
    if [ -z "$CONFIG_ADMIN_EMAIL" ]; then
        echo -e "${CYAN}Admin Email (optional)${NC}"
        echo "  Used for SSL certificate notifications from Let's Encrypt."
        echo ""
        read -p "  Email []: " input_email
        CONFIG_ADMIN_EMAIL="$input_email"
        echo ""
    fi

    # TLS Mode Selection
    if [ -z "$CONFIG_TLS_MODE" ]; then
        echo -e "${CYAN}TLS Certificate Mode${NC}"
        echo "  Choose how SSL/TLS certificates are managed."
        echo ""
        echo "  1) letsencrypt  - Automatic Let's Encrypt certificates (production)"
        echo "                    Requires: Public domain, ports 80/443 open"
        echo "  2) selfsigned   - Self-signed certificates (local testing)"
        echo "                    Browsers will show security warnings"
        echo "  3) certificate  - Use your own wildcard certificate"
        echo "                    Place cert.pem and key.pem in ./certs/"
        echo ""
        read -p "  Select TLS mode [1]: " tls_choice
        case "$tls_choice" in
            2|selfsigned|self-signed|internal)
                CONFIG_TLS_MODE="selfsigned"
                ;;
            3|certificate|custom)
                CONFIG_TLS_MODE="certificate"
                ;;
            *)
                CONFIG_TLS_MODE="letsencrypt"
                ;;
        esac
        echo ""
    fi

    # Profiles - Interactive checkbox selection
    if [ -z "$CONFIG_PROFILES" ]; then
        select_profiles_interactive
    fi

    # GPUStack Mode Selection (only if an LLM profile is selected)
    if [ -z "$CONFIG_GPUSTACK_MODE" ]; then
        if echo "$CONFIG_PROFILES" | grep -qE "llm-cpu|llm-box|llm-experimental|llm-legacy|(^|,)llm($|,)"; then
            echo -e "${CYAN}GPUStack Network Mode${NC}"
            echo "  Choose how GPUStack network ports are exposed."
            echo ""
            echo "  1) standalone - All ports bound to localhost only (default)"
            echo "                  No external workers can connect"
            echo "  2) master     - All ports exposed on all interfaces"
            echo "                  External workers can connect to this server"
            echo "  3) worker     - Connect to a remote GPUStack master server"
            echo "                  This node acts as a compute worker"
            echo ""
            read -p "  Select GPUStack mode [1]: " gpustack_choice
            case "$gpustack_choice" in
                2|master)
                    CONFIG_GPUSTACK_MODE="master"
                    ;;
                3|worker)
                    CONFIG_GPUSTACK_MODE="worker"
                    ;;
                *)
                    CONFIG_GPUSTACK_MODE="standalone"
                    ;;
            esac
            echo ""

            # Worker mode requires server URL and token
            if [ "$CONFIG_GPUSTACK_MODE" = "worker" ]; then
                if [ -z "$CONFIG_GPUSTACK_SERVER_URL" ]; then
                    echo -e "${CYAN}GPUStack Worker Configuration${NC}"
                    echo "  Enter the connection details for the master GPUStack server."
                    echo "  The server URL should be http://<master-ip>:9090"
                    echo "  The token can be found in GPUStack UI → API Keys on the master."
                    echo ""
                    read -p "  Master Server URL: " CONFIG_GPUSTACK_SERVER_URL
                    read -p "  Master Server Token: " CONFIG_GPUSTACK_TOKEN
                    echo ""
                fi

                if [ -z "$CONFIG_GPUSTACK_SERVER_URL" ] || [ -z "$CONFIG_GPUSTACK_TOKEN" ]; then
                    print_error "Worker mode requires both --gpustack-server-url and --gpustack-token"
                    exit 1
                fi
            fi
        fi
    fi

    # Authentik Scenario Selection
    if [ -z "$CONFIG_SCENARIO" ]; then
        echo -e "${CYAN}Authentication Scenario${NC}"
        echo "  Choose how users will authenticate to the system."
        echo ""
        echo "  1) base   - Standard username/password authentication"
        echo "  2) google - Google SSO integration (requires Google OAuth credentials)"
        echo ""
        read -p "  Select scenario [1]: " scenario_choice
        case "$scenario_choice" in
            2|google)
                CONFIG_SCENARIO="google"
                ;;
            *)
                CONFIG_SCENARIO="base"
                ;;
        esac
        echo ""
    fi

    # Google OAuth Credentials (if google scenario selected)
    if [ "$CONFIG_SCENARIO" = "google" ]; then
        if [ -z "$CONFIG_GOOGLE_CLIENT_ID" ]; then
            echo -e "${CYAN}Google OAuth Configuration${NC}"
            echo "  Enter your Google OAuth 2.0 credentials."
            echo "  Get them from: https://console.cloud.google.com/apis/credentials"
            echo ""
            read -p "  Google Client ID: " CONFIG_GOOGLE_CLIENT_ID
            read -p "  Google Client Secret: " CONFIG_GOOGLE_CLIENT_SECRET
            echo ""
        fi
    fi

    # Microsoft Entra ID SSO (optional, independent of Google)
    if [ "$CONFIG_ENTRA_ENABLED" = "false" ] && [ -z "$CONFIG_ENTRA_CLIENT_ID" ]; then
        echo -e "${CYAN}Microsoft Entra ID SSO (optional)${NC}"
        echo "  Add Microsoft Entra ID (Azure AD) as a second SSO source."
        echo "  Can be enabled alongside or instead of Google SSO."
        echo "  Requires an App Registration at: https://portal.azure.com"
        echo "    Redirect URI: https://auth.<domain>/source/oauth/callback/entra/"
        echo ""
        echo "  1) Enable  - Configure Entra ID SSO"
        echo "  2) Skip    - Configure later via razzfazz-setup.sh"
        echo ""
        read -p "  Select [2]: " entra_choice
        case "$entra_choice" in
            1|enable|Enable)
                CONFIG_ENTRA_ENABLED="true"
                read -p "  Entra Client ID (Application ID): " CONFIG_ENTRA_CLIENT_ID
                read -s -p "  Entra Client Secret: " CONFIG_ENTRA_CLIENT_SECRET
                echo ""
                read -p "  Entra Tenant ID (Directory ID): " CONFIG_ENTRA_TENANT_ID
                read -p "  Email domain restriction (optional, e.g. corp.com — leave empty for all): " CONFIG_ENTRA_OAUTH_DOMAIN
                echo ""
                ;;
            *)
                CONFIG_ENTRA_ENABLED="false"
                ;;
        esac
    fi


    if [ -z "$CONFIG_SMTP_MODE" ]; then
        echo -e "${CYAN}SMTP Relay (optional)${NC}"
        echo "  The stack includes a centralized SMTP relay for sending emails"
        echo "  from Authentik, Dify, Gitea, and Open WebUI."
        echo ""
        echo "  1) relay  - Forward via external SMTP provider (Gmail, Mailgun, etc.)"
        echo "  2) direct - Send directly to recipient mail servers"
        echo "              (requires SPF/DKIM/DMARC DNS records and port 25 open)"
        echo "  3) skip   - Configure later via the Configuration Portal or 'rzfz setup'"
        echo ""
        read -p "  Select SMTP mode [3]: " smtp_choice
        case "$smtp_choice" in
            1|relay)
                CONFIG_SMTP_MODE="relay"
                if [ -z "$CONFIG_SMTP_RELAY_HOST" ]; then
                    read -p "  SMTP Relay Host [smtp.gmail.com]: " smtp_host
                    CONFIG_SMTP_RELAY_HOST="${smtp_host:-smtp.gmail.com}"
                fi
                if [ -z "$CONFIG_SMTP_RELAY_USERNAME" ]; then
                    read -p "  SMTP Username: " CONFIG_SMTP_RELAY_USERNAME
                fi
                if [ -z "$CONFIG_SMTP_RELAY_PASSWORD" ]; then
                    read -s -p "  SMTP Password: " CONFIG_SMTP_RELAY_PASSWORD
                    echo ""
                fi
                ;;
            2|direct)
                CONFIG_SMTP_MODE="direct"
                ;;
            *)
                CONFIG_SMTP_MODE=""
                ;;
        esac
        echo ""
    fi

    # Confirmation
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}                    Configuration Summary                          ${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  Domain:       $CONFIG_DOMAIN"
    echo "  Timezone:     $CONFIG_TIMEZONE"
    echo "  Admin Email:  ${CONFIG_ADMIN_EMAIL:-Not set}"
    echo "  Admin PW:     ********"
    echo "  Auth Mode:    $CONFIG_SCENARIO"
    echo "  TLS Mode:     $CONFIG_TLS_MODE"
    if [ -n "$CONFIG_GPUSTACK_MODE" ]; then
    echo "  GPUStack:     $CONFIG_GPUSTACK_MODE"
    if [ "$CONFIG_GPUSTACK_MODE" = "worker" ]; then
    echo "  Master URL:   $CONFIG_GPUSTACK_SERVER_URL"
    echo "  Master Token: ****${CONFIG_GPUSTACK_TOKEN: -4}"
    fi
    fi
    if [ -n "$CONFIG_SMTP_MODE" ]; then
    echo "  SMTP Mode:    $CONFIG_SMTP_MODE"
    if [ "$CONFIG_SMTP_MODE" = "relay" ] && [ -n "$CONFIG_SMTP_RELAY_HOST" ]; then
    echo "  SMTP Relay:   $CONFIG_SMTP_RELAY_HOST (user: ${CONFIG_SMTP_RELAY_USERNAME:-none})"
    fi
    fi
    echo ""
    echo "  Enabled Modules:"
    # Display modules in a nice format
    IFS=',' read -ra PROFILE_ARRAY <<< "$CONFIG_PROFILES"
    for profile in "${PROFILE_ARRAY[@]}"; do
        case "$profile" in
            chat)      echo -e "    ${GREEN}✓${NC} Chat (Open WebUI)" ;;
            dify)      echo -e "    ${GREEN}✓${NC} Dify Workflow Automation" ;;
            llm-cpu)   echo -e "    ${GREEN}✓${NC} LLM Inference (CPU)" ;;
            llm-box)   echo -e "    ${GREEN}✓${NC} LLM Inference (AMD Vulkan)" ;;
            llm-experimental) echo -e "    ${GREEN}✓${NC} LLM Inference (Experimental)" ;;
            monitor)   echo -e "    ${GREEN}✓${NC} Komodo Container Management" ;;
            searxng)   echo -e "    ${GREEN}✓${NC} SearXNG Privacy Search" ;;
            stts)      echo -e "    ${GREEN}✓${NC} Speech-to-Text/Text-to-Speech" ;;
            gotenberg) echo -e "    ${GREEN}✓${NC} Gotenberg PDF Conversion" ;;
            git)       echo -e "    ${GREEN}✓${NC} Gitea Git Repository" ;;
            *)         echo -e "    ${GREEN}✓${NC} $profile" ;;
        esac
    done
    echo ""

    read -p "  Proceed with this configuration? [Y/n]: " confirm
    if [[ "$confirm" =~ ^[Nn] ]]; then
        print_error "Configuration cancelled."
        exit 1
    fi
}

# ==============================================================================
# Apply Configuration to .env
# ==============================================================================
apply_configuration() {
    print_step "Applying configuration to environment files..."

    # Main domain
    if [ -n "$CONFIG_DOMAIN" ]; then
        update_env_value ".env" "MAIN_DOMAIN" "$CONFIG_DOMAIN"
        print_substep "Set MAIN_DOMAIN=$CONFIG_DOMAIN"
    fi

    # Timezone
    if [ -n "$CONFIG_TIMEZONE" ]; then
        update_env_value ".env" "TZ" "$CONFIG_TIMEZONE"
        update_env_value ".env" "GENERIC_TIMEZONE" "$CONFIG_TIMEZONE"
        print_substep "Set TZ=$CONFIG_TIMEZONE"
    fi

    # Admin Email (used by Caddy for Let's Encrypt certificate notifications)
    if [ -n "$CONFIG_ADMIN_EMAIL" ]; then
        update_env_value ".env" "LETSENCRYPT_EMAIL" "$CONFIG_ADMIN_EMAIL"
        print_substep "Set LETSENCRYPT_EMAIL=$CONFIG_ADMIN_EMAIL"
    fi

    # Profiles
    if [ -n "$CONFIG_PROFILES" ]; then
        update_env_value ".env" "COMPOSE_PROFILES" "$CONFIG_PROFILES"
        print_substep "Set COMPOSE_PROFILES=$CONFIG_PROFILES"
    fi

    # M018 Phase 6 / S06.5: HARDWARE + COMPOSE_FILE for the `llm` profile.
    # `llm` selects a single gpustack service that runs on AMD / NVIDIA / CPU
    # via a device overlay file (modules/llm/compose.devices.<HARDWARE>.yml). Legacy
    # profiles (llm-legacy, llm-cpu) use a self-contained compose.yml — no
    # overlay needed, but HARDWARE is still recorded so razzfazz-status.sh
    # and post-install can reason about it. Auto-detect if not passed.
    # Found rc6.9: pre-fix, testvm-cpu / single-box presets left HARDWARE
    # at the .env.example default (amd) on llm-cpu / llm-legacy installs.
    local llm_profile_active=""
    case ",$CONFIG_PROFILES," in
        *,llm,*)         llm_profile_active="llm" ;;
        *,llm-legacy,*)  llm_profile_active="llm-legacy" ;;
        *,llm-cpu,*)     llm_profile_active="llm-cpu" ;;
    esac
    if [ -n "$llm_profile_active" ]; then
        if [ -z "$CONFIG_HARDWARE" ]; then
            CONFIG_HARDWARE=$(detect_hardware)
            print_substep "Auto-detected HARDWARE=$CONFIG_HARDWARE"
        fi
        case "$CONFIG_HARDWARE" in
            amd|nvidia|cpu) ;;
            *)
                print_error "Invalid --hardware value: $CONFIG_HARDWARE (expected amd|nvidia|cpu)"
                exit 1
                ;;
        esac
        update_env_value ".env" "HARDWARE" "$CONFIG_HARDWARE"
        print_substep "Set HARDWARE=$CONFIG_HARDWARE"
        if [ "$llm_profile_active" = "llm" ]; then
            update_env_value ".env" "COMPOSE_FILE" "compose.yml:modules/llm/compose.devices.${CONFIG_HARDWARE}.yml"
            print_substep "Set COMPOSE_FILE=compose.yml:modules/llm/compose.devices.${CONFIG_HARDWARE}.yml"
        else
            # Legacy / CPU profiles: compose.yml is self-contained. Set
            # COMPOSE_FILE=compose.yml explicitly (not empty) — docker
            # compose 2.40+ chokes on an empty COMPOSE_FILE value.
            update_env_value ".env" "COMPOSE_FILE" "compose.yml"
            print_substep "Set COMPOSE_FILE=compose.yml ($llm_profile_active is self-contained)"
        fi

        # rc6.7 #63: detect host's render gid for AMD/ROCm device access.
        # /dev/dri/renderD128 + /dev/kfd are owned by root:render on the
        # host. The gpustack container needs the same numeric gid as a
        # supplementary group — passing `render` by name doesn't work
        # because docker-compose looks names up in the IMAGE's /etc/group
        # (gpustack debian image has render at gid 109; host typically
        # 992 on Ubuntu 24.04). Without this, the container runtime can
        # see /dev/dri/* but can't read it → ACCEL_WORKING -13 EACCES,
        # every model deploy hangs at ready_replicas:0 indefinitely.
        if [ "$CONFIG_HARDWARE" = "amd" ]; then
            local render_gid
            render_gid=$(getent group render 2>/dev/null | cut -d: -f3)
            render_gid=${render_gid:-992}
            update_env_value ".env" "RENDER_GID" "$render_gid"
            print_substep "Set RENDER_GID=$render_gid (for AMD GPU device access)"

            # CRITICAL (clean-install): the AMD Vulkan runner path (llm-legacy custom
            # Vulkan build, and the `llm` profile runner) bind-mounts the HOST's
            # /usr/lib/x86_64-linux-gnu + /usr/share/vulkan/icd.d INTO the gpustack
            # container so it uses the host's amdgpu (Mesa RADV) Vulkan driver. A
            # minimal Ubuntu Server install ships NO Vulkan userspace, so that
            # bind-mount delivers an EMPTY /usr/lib → llama-server aborts
            # "libvulkan.so.1: cannot open shared object file" → EVERY model errors
            # (exit 127). The gpustack image's own libvulkan is irrelevant here — the
            # host's /usr/lib shadows it at runtime. Install the Vulkan loader + RADV
            # driver on the host so the bind-mount actually carries a working Vulkan.
            if ! ls /usr/lib/x86_64-linux-gnu/libvulkan.so.1 >/dev/null 2>&1; then
                print_substep "Installing host Vulkan userspace (libvulkan1 + mesa-vulkan-drivers) for the AMD GPU..."
                if sudo apt-get update -qq >/dev/null 2>&1 && \
                   sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libvulkan1 mesa-vulkan-drivers >/dev/null 2>&1; then
                    print_substep "Host Vulkan userspace installed (Mesa RADV → the bind-mount now carries a working amdgpu Vulkan driver)."
                else
                    print_warning "Host Vulkan install FAILED — gpustack models will exit 127. Run manually: sudo apt-get install -y libvulkan1 mesa-vulkan-drivers"
                fi
            else
                print_substep "Host Vulkan userspace already present."
            fi
        fi
    fi

    # Authentik Deployment Scenario
    if [ -n "$CONFIG_SCENARIO" ]; then
        update_env_value ".env" "DEPLOYMENT_SCENARIO" "$CONFIG_SCENARIO"
        print_substep "Set DEPLOYMENT_SCENARIO=$CONFIG_SCENARIO"

        # Enable/Disable Google OAuth based on scenario
        if [ "$CONFIG_SCENARIO" = "google" ]; then
            update_env_value ".env" "ENABLE_GOOGLE_OAUTH" "true"
            # Set Google credentials if provided
            if [ -n "$CONFIG_GOOGLE_CLIENT_ID" ]; then
                update_env_value ".env" "GOOGLE_CLIENT_ID" "$CONFIG_GOOGLE_CLIENT_ID"
                update_env_value ".env" "GOOGLE_CLIENT_SECRET" "$CONFIG_GOOGLE_CLIENT_SECRET"
                print_substep "Set Google OAuth credentials"
            fi
            print_substep "Enabled Google OAuth for OpenWebUI, Gitea, and Authentik"
        else
            update_env_value ".env" "ENABLE_GOOGLE_OAUTH" "false"
            # Clear Google credentials for base scenario
            update_env_value ".env" "GOOGLE_CLIENT_ID" ""
            update_env_value ".env" "GOOGLE_CLIENT_SECRET" ""
            print_substep "Disabled Google OAuth (base scenario)"
        fi
    fi

    # Enable/Disable Microsoft Entra ID SSO
    if [ "$CONFIG_ENTRA_ENABLED" = "true" ] && [ -n "$CONFIG_ENTRA_CLIENT_ID" ] && [ -n "$CONFIG_ENTRA_TENANT_ID" ]; then
        update_env_value ".env" "ENABLE_ENTRA_OAUTH" "true"
        update_env_value ".env" "ENTRA_CLIENT_ID" "$CONFIG_ENTRA_CLIENT_ID"
        update_env_value ".env" "ENTRA_CLIENT_SECRET" "$CONFIG_ENTRA_CLIENT_SECRET"
        update_env_value ".env" "ENTRA_TENANT_ID" "$CONFIG_ENTRA_TENANT_ID"
        update_env_value ".env" "ENTRA_OAUTH_DOMAIN" "${CONFIG_ENTRA_OAUTH_DOMAIN:-}"
        print_substep "Enabled Microsoft Entra ID SSO (tenant: $CONFIG_ENTRA_TENANT_ID)"
    else
        update_env_value ".env" "ENABLE_ENTRA_OAUTH" "false"
        print_substep "Microsoft Entra ID SSO disabled"
    fi

    # Derive ENABLE_OAUTH_SIGNUP — true if Google SSO or Open WebUI OIDC is enabled
    _GOOGLE=$(grep -m1 "^ENABLE_GOOGLE_OAUTH=" ".env" | cut -d= -f2)
    _OIDC=$(grep -m1 "^ENABLE_OPENWEBUI_OIDC=" ".env" | cut -d= -f2)
    if [ "$_GOOGLE" = "true" ] || [ "$_OIDC" = "true" ]; then
        update_env_value ".env" "ENABLE_OAUTH_SIGNUP" "true"
        print_substep "Set ENABLE_OAUTH_SIGNUP=true (Google=${_GOOGLE:-false}, OIDC=${_OIDC:-false})"
    else
        update_env_value ".env" "ENABLE_OAUTH_SIGNUP" "false"
    fi

    # #29: Derive GITEA_OAUTH_AUTO_REGISTER — true if Google OR Authentik OIDC is
    # enabled, so Gitea auto-registers SSO users (the compose gates
    # GITEA__oauth2_client__ENABLE_AUTO_REGISTRATION on this). Previously tied to
    # ENABLE_GOOGLE_OAUTH only → enabling JUST Authentik OIDC left it off.
    _GITEA_OIDC=$(grep -m1 "^ENABLE_GITEA_AUTHENTIK_OIDC=" ".env" | cut -d= -f2)
    if [ "$_GOOGLE" = "true" ] || [ "$_GITEA_OIDC" = "true" ]; then
        update_env_value ".env" "GITEA_OAUTH_AUTO_REGISTER" "true"
        print_substep "Set GITEA_OAUTH_AUTO_REGISTER=true (Google=${_GOOGLE:-false}, Gitea-OIDC=${_GITEA_OIDC:-false})"
    else
        update_env_value ".env" "GITEA_OAUTH_AUTO_REGISTER" "false"
    fi


    if [ -n "$CONFIG_ADMIN_PASSWORD" ]; then
        # Authentik uses the user-provided password (the single password the admin remembers)
        update_env_value ".env" "AUTHENTIK_BOOTSTRAP_PASSWORD" "$CONFIG_ADMIN_PASSWORD"
        # Backup-archive encryption re-uses the admin password (single recovery
        # secret on the sticker). Set HERE, where the real password is known —
        # the generate-secrets pass earlier cannot (it ran with the var unset,
        # which left BACKUP_ENCRYPTION_PASSWORD empty → backups refused; ga.4
        # 0.91 finding). compose has no fallback (${BACKUP_ENCRYPTION_PASSWORD:-}).
        update_env_value ".env" "BACKUP_ENCRYPTION_PASSWORD" "$CONFIG_ADMIN_PASSWORD"
        # Record the as-shipped admin password once (immutable) so a full
        # factory reset can return the box to delivery state (Workstream I).
        write_delivery_blob "$CONFIG_ADMIN_PASSWORD"
        # Gitea admin also uses the user-provided password
        update_env_value ".env" "GITEA_ADMIN_PASSWORD" "$CONFIG_ADMIN_PASSWORD"
        # LightRAG uses AUTH_ACCOUNTS format: user:password
        update_env_value ".env" "LIGHTRAG_AUTH_ACCOUNTS" "admin:$CONFIG_ADMIN_PASSWORD"
        print_substep "Set admin password for Authentik, Gitea, LightRAG"
        # rc6.7 #D: when the operator typed a password, ALL admin accounts use it —
        # users expect a single password they remember, not per-service randoms.
        # Earlier behaviour (only sync in --no-secrets mode) left GPUStack/Komodo/
        # Dify with the random init seeds the operator never sees, blocking login.
        update_env_value ".env" "GPUSTACK_ADMIN_PASSWORD" "$CONFIG_ADMIN_PASSWORD"
        update_env_value ".env" "KOMODO_INIT_ADMIN_PASSWORD" "$CONFIG_ADMIN_PASSWORD"
        # M033 S17: cognee's fastapi-users seeds its admin from COGNEE_ADMIN_PASSWORD
        # on first boot. It was being left at the random generated above, so the
        # operator's single remembered password never logged in to cognee. Align it
        # here (before first boot) like every other admin account.
        update_env_value ".env" "COGNEE_ADMIN_PASSWORD" "$CONFIG_ADMIN_PASSWORD"
        # Dify: the shared admin password becomes the Dify ADMIN LOGIN password
        # (set during post-install setup), NOT INIT_PASSWORD. INIT_PASSWORD is
        # only the first-run gate and Dify 1.14+ caps it at 30 chars, so keep it
        # a dedicated short token instead of the (possibly >30) admin password.
        if [ -f ".env.dify" ]; then
            local cur_init_pw2
            cur_init_pw2=$(grep -E "^INIT_PASSWORD=" .env.dify | cut -d'=' -f2-)
            if [ -z "$cur_init_pw2" ] || [ "${#cur_init_pw2}" -gt 30 ]; then
                update_env_value ".env.dify" "INIT_PASSWORD" "$(generate_password 24)"
            fi
        fi
        print_substep "Set shared admin password for GPUStack, Komodo, Cognee, Dify"
    fi

    # SMTP Relay configuration
    if [ -n "$CONFIG_SMTP_MODE" ]; then
        update_env_value ".env" "SMTP_MODE" "$CONFIG_SMTP_MODE"
        print_substep "Set SMTP_MODE=$CONFIG_SMTP_MODE"

        if [ "$CONFIG_SMTP_MODE" = "relay" ]; then
            if [ -n "$CONFIG_SMTP_RELAY_HOST" ]; then
                update_env_value ".env" "SMTP_RELAY_HOST" "$CONFIG_SMTP_RELAY_HOST"
                print_substep "Set SMTP_RELAY_HOST=$CONFIG_SMTP_RELAY_HOST"
            fi
            if [ -n "$CONFIG_SMTP_RELAY_USERNAME" ]; then
                update_env_value ".env" "SMTP_RELAY_USERNAME" "$CONFIG_SMTP_RELAY_USERNAME"
                print_substep "Set SMTP relay username"
            fi
            if [ -n "$CONFIG_SMTP_RELAY_PASSWORD" ]; then
                update_env_value ".env" "SMTP_RELAY_PASSWORD" "$CONFIG_SMTP_RELAY_PASSWORD"
                print_substep "Set SMTP relay password"
            fi
        elif [ "$CONFIG_SMTP_MODE" = "direct" ]; then
            # Clear relay credentials for direct mode
            update_env_value ".env" "SMTP_RELAY_HOST" ""
            update_env_value ".env" "SMTP_RELAY_USERNAME" ""
            update_env_value ".env" "SMTP_RELAY_PASSWORD" ""
            print_substep "Set SMTP to direct mode (no relay credentials)"
        fi
    fi

    # Auto-detect CPU architecture for Docker platform
    local arch=$(uname -m)
    if [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then
        update_env_value ".env" "DOCKER_PLATFORM" "linux/arm64"
        print_substep "Detected ARM64 architecture - Set DOCKER_PLATFORM=linux/arm64"
    else
        update_env_value ".env" "DOCKER_PLATFORM" "linux/amd64"
        print_substep "Detected x86_64 architecture - Set DOCKER_PLATFORM=linux/amd64"
    fi

    # TLS Mode configuration
    # Default to letsencrypt if not specified
    if [ -z "$CONFIG_TLS_MODE" ]; then
        CONFIG_TLS_MODE="letsencrypt"
    fi

    case "$CONFIG_TLS_MODE" in
        selfsigned|internal|self-signed)
            update_env_value ".env" "TLS_MODE" "internal"
            update_env_value ".env" "TLS_DIRECTIVE" "tls internal"
            print_substep "Set TLS_MODE=internal (self-signed certificates)"
            print_warning "Using self-signed certificates - browsers will show security warnings"
            ;;
        certificate|custom)
            update_env_value ".env" "TLS_MODE" "certificate"
            update_env_value ".env" "TLS_DIRECTIVE" ""
            print_substep "Set TLS_MODE=certificate (custom wildcard certificate)"
            if [ ! -f "./certs/cert.pem" ] || [ ! -f "./certs/key.pem" ]; then
                print_warning "Certificate files not found in ./certs/. Place cert.pem and key.pem before starting."
            else
                print_substep "Found certificate files in ./certs/"
            fi
            ;;
        letsencrypt|production|acme)
            update_env_value ".env" "TLS_MODE" ""
            update_env_value ".env" "TLS_DIRECTIVE" ""
            print_substep "Set TLS_MODE=letsencrypt (automatic Let's Encrypt certificates)"
            ;;
        *)
            print_warning "Unknown TLS mode '$CONFIG_TLS_MODE', defaulting to letsencrypt"
            update_env_value ".env" "TLS_MODE" ""
            update_env_value ".env" "TLS_DIRECTIVE" ""
            ;;
    esac

    # F-041: Set Vaultwarden SSO cert validation based on TLS mode
    case "$CONFIG_TLS_MODE" in
        selfsigned|internal|self-signed)
            update_env_value ".env" "VAULTWARDEN_SSO_ACCEPT_INVALID_CERTS" "true"
            ;;
        *)
            update_env_value ".env" "VAULTWARDEN_SSO_ACCEPT_INVALID_CERTS" "false"
            ;;
    esac

    # GPUStack Mode configuration
    # rc6.9 / F-RC5-1: bind worker ports (10150-10151) to loopback in
    # standalone mode. Pre-rc6.9 the compose default was always 0.0.0.0
    # (correct for master, leaky for standalone) and razzfazz-init.sh
    # never wrote GPUSTACK_WORKER_HOST_BIND, so standalone boxes had
    # gpustack worker ports exposed on the LAN. Both gpustack-legacy
    # and gpustack (v2.x) inherit this from the same compose default,
    # so both profiles get the same hardening level.
    if [ -n "$CONFIG_GPUSTACK_MODE" ]; then
        case "$CONFIG_GPUSTACK_MODE" in
            master)
                update_env_value ".env" "GPUSTACK_MODE" "master"
                update_env_value ".env" "GPUSTACK_BIND" "0.0.0.0"
                update_env_value ".env" "GPUSTACK_HOST_BIND" "0.0.0.0"
                update_env_value ".env" "GPUSTACK_WORKER_HOST_BIND" "0.0.0.0"
                update_env_value ".env" "GPUSTACK_MASTER_SERVER_URL" ""
                update_env_value ".env" "GPUSTACK_MASTER_SERVER_TOKEN" ""
                update_env_value ".env" "GPUSTACK_WORKER_IP" ""
                update_env_value ".env" "GPUSTACK_WORKER_NAME" ""
                print_substep "Set GPUSTACK_MODE=master (ports exposed on all interfaces)"
                print_warning "Ensure firewall rules allow ports 9090, 10150-10151, 40000-40103, 52365"
                ;;
            worker)
                update_env_value ".env" "GPUSTACK_MODE" "worker"
                update_env_value ".env" "GPUSTACK_BIND" "0.0.0.0"
                update_env_value ".env" "GPUSTACK_HOST_BIND" "127.0.0.1"
                update_env_value ".env" "GPUSTACK_WORKER_HOST_BIND" "0.0.0.0"
                if [ -n "$CONFIG_GPUSTACK_SERVER_URL" ]; then
                    update_env_value ".env" "GPUSTACK_MASTER_SERVER_URL" "$CONFIG_GPUSTACK_SERVER_URL"
                fi
                if [ -n "$CONFIG_GPUSTACK_TOKEN" ]; then
                    update_env_value ".env" "GPUSTACK_MASTER_SERVER_TOKEN" "$CONFIG_GPUSTACK_TOKEN"
                fi
                # Auto-detect host IP (non-loopback, non-docker) for worker registration
                local worker_ip
                worker_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)
                if [ -z "$worker_ip" ]; then
                    worker_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
                fi
                update_env_value ".env" "GPUSTACK_WORKER_IP" "${worker_ip:-}"
                update_env_value ".env" "GPUSTACK_WORKER_NAME" "$(hostname)"
                print_substep "Set GPUSTACK_MODE=worker (connecting to master: $CONFIG_GPUSTACK_SERVER_URL)"
                print_substep "Worker IP: ${worker_ip:-auto}, Worker Name: $(hostname)"
                ;;
            standalone|*)
                update_env_value ".env" "GPUSTACK_MODE" "standalone"
                update_env_value ".env" "GPUSTACK_BIND" "127.0.0.1"
                update_env_value ".env" "GPUSTACK_HOST_BIND" "127.0.0.1"
                update_env_value ".env" "GPUSTACK_WORKER_HOST_BIND" "127.0.0.1"
                update_env_value ".env" "GPUSTACK_MASTER_SERVER_URL" ""
                update_env_value ".env" "GPUSTACK_MASTER_SERVER_TOKEN" ""
                update_env_value ".env" "GPUSTACK_WORKER_IP" ""
                update_env_value ".env" "GPUSTACK_WORKER_NAME" ""
                print_substep "Set GPUSTACK_MODE=standalone (ports bound to localhost only)"
                ;;
        esac
    fi

    # Host IP (for documentation / help center)
    local host_ip
    host_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [ -n "$host_ip" ]; then
        update_env_value ".env" "HOST_IP" "$host_ip"
        print_substep "Set HOST_IP=$host_ip"
    fi

    # Local /etc/hosts entries so domains resolve on the box itself via 127.0.0.1
    local domain="${CONFIG_DOMAIN:-$(grep '^MAIN_DOMAIN=' .env 2>/dev/null | cut -d= -f2-)}"
    if [ -n "$domain" ]; then
        local subdomains="chat auth dify llm admin setup help backup license git"
        local hosts_line="127.0.0.1"
        for sub in $subdomains; do
            hosts_line="$hosts_line ${sub}.${domain}"
        done
        # Only add if not already present
        if ! grep -qF "chat.${domain}" /etc/hosts 2>/dev/null; then
            echo "$hosts_line" | sudo tee -a /etc/hosts >/dev/null
            print_substep "Added local domain entries to /etc/hosts"
        else
            print_substep "/etc/hosts already contains domain entries"
        fi
    fi

    print_success "Configuration applied."
}

# ==============================================================================
# Build and Pull Docker Images
# ==============================================================================
build_and_pull_images() {
    print_step "Building and pulling Docker images..."

    # #184 WS2b: a day-0 air-gapped install obtains every image from the offline
    # package (loaded out-of-band) — building/pulling here would trip the firewall.
    # (The common P0 case installs ONLINE and is cut off afterwards, so this gate
    # is normally inactive at init.) 'rzfz verify-images' is the completeness gate.
    if razzfazz_offline_skip "image build + pull at install"; then
        print_info "  Offline install: images must be pre-loaded from the package (docker load). Confirm with 'rzfz verify-images'."
        return 0
    fi

    print_warning "This may take several minutes on first run."
    echo ""

    # T11 (2026-05-16): PULL before BUILD, not after. Otherwise a service
    # that has BOTH a registry `image:` tag AND a local `build:` (e.g.
    # gpustack-legacy) ends up overwritten by the registry pull AFTER
    # the local build succeeded — losing the locally-baked patches (per
    # memory project_gemma4_swa_prompt_cache_patched_build, the patched
    # llama-cpp binaries baked by BSB-08 in Dockerfile.vulkan would be
    # silently replaced by the registry image's stock b9101 binaries
    # whenever pull ran after build).
    print_substep "Pulling pre-built images..."
    # --ignore-buildable: skip services that carry a build: section (caddy,
    # backup-service, coding-tools, hermes/moltis, gpustack-legacy, …). They are
    # built locally a few lines below, never pulled, so attempting to pull them
    # only produces noisy "pull access denied" lines for razzfazz-* image tags.
    # (caddy was already silent only because it declares no image:; agent-manager
    # and gpustack-legacy were silent via pull_policy build/never — this makes
    # every buildable service uniform.) --ignore-pull-failures keeps any genuine
    # third-party pull miss non-fatal.
    docker compose pull --ignore-pull-failures --ignore-buildable 2>&1 || print_warning "Some images failed to pull."

    print_substep "Building custom images (overrides any pulled image)..."
    # #184 WS2a: strip the no-build overlay for the ONE intended build (harmless
    # no-op on first install where it isn't composed yet; correct when init is
    # re-run on a box that already has it in COMPOSE_FILE).
    COMPOSE_FILE="$(compose_file_for_build)" docker compose build --parallel 2>&1 || print_warning "Some images failed to build."

    print_success "Docker images ready."
}

# ==============================================================================
# Delivery-credentials blob (Workstream I, 2026.06-ga.5)
# ------------------------------------------------------------------------------
# Records the as-shipped admin password ONCE so a full factory reset can return
# the box to delivery state (the password printed on the device sticker).
# - Immutable: written once at first init, NEVER rewritten — so rotating the
#   admin password does not change it.
# - Encrypted at rest with a machine-stable key. This is NOT a strong secret
#   boundary (anyone with box file access already reads the live password from
#   `.env`); it just prevents casual disk-read and survives the volume wipe.
# - Returning to the delivery password is only ever done as part of a FULL
#   factory reset (all data wiped) — so it is re-provisioning, not an access
#   bypass on a live system.
# ==============================================================================
DELIVERY_BLOB="${SCRIPT_DIR}/.delivery-credentials.enc"

_delivery_key() {
    { cat /etc/machine-id 2>/dev/null; echo "razzfazz-delivery-v1"; } | sha256sum | cut -d' ' -f1
}

write_delivery_blob() {  # $1 = delivery admin password
    local pw="$1"
    [ -n "$pw" ] || return 0
    [ -f "$DELIVERY_BLOB" ] && return 0            # immutable — never overwrite
    command -v openssl >/dev/null 2>&1 || { print_warning "openssl missing — delivery-credentials blob not written (factory-reset-to-delivery unavailable)."; return 0; }
    if printf 'AUTHENTIK_BOOTSTRAP_PASSWORD=%s\n' "$pw" \
        | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "pass:$(_delivery_key)" -out "$DELIVERY_BLOB" 2>/dev/null; then
        chmod 600 "$DELIVERY_BLOB"
        print_substep "Recorded delivery credentials (factory-reset-to-delivery enabled)."
    fi
}

read_delivery_blob() {   # prints the delivery admin password, or nothing
    [ -f "$DELIVERY_BLOB" ] || return 1
    command -v openssl >/dev/null 2>&1 || return 1
    openssl enc -d -aes-256-cbc -pbkdf2 -pass "pass:$(_delivery_key)" -in "$DELIVERY_BLOB" 2>/dev/null \
        | sed -n 's/^AUTHENTIK_BOOTSTRAP_PASSWORD=//p'
}

# ==============================================================================
# Factory Reset
# ==============================================================================
factory_reset() {
    echo ""
    echo -e "${RED}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║                        ⚠ WARNING ⚠                               ║${NC}"
    echo -e "${RED}║                                                                  ║${NC}"
    echo -e "${RED}║   You are about to perform a FACTORY RESET!                      ║${NC}"
    echo -e "${RED}║                                                                  ║${NC}"
    echo -e "${RED}║   This will DELETE:                                              ║${NC}"
    echo -e "${RED}║   • All Docker volumes (databases, configs, user data)           ║${NC}"
    echo -e "${RED}║   • All container state                                          ║${NC}"
    echo -e "${RED}║                                                                  ║${NC}"
    echo -e "${RED}║   Backups in ./backups/ will be PRESERVED.                       ║${NC}"
    echo -e "${RED}║                                                                  ║${NC}"
    echo -e "${RED}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    read -p "Type 'DELETE ALL DATA' to confirm: " confirmation

    if [ "$confirmation" != "DELETE ALL DATA" ]; then
        print_error "Factory reset aborted."
        exit 1
    fi

    print_warning "Factory reset confirmed. Proceeding..."
    docker compose down -v --remove-orphans 2>/dev/null || true
    # rc6.7 #48: GPUStack runner pods were spawned via the docker socket and
    # are NOT part of the compose project — they survive `compose down`. On
    # factory reset we want them gone too.
    stop_orphan_gpustack_runners

    # Workstream I (ga.5): return to delivery state — restore the as-shipped
    # (sticker) admin password into .env so the box comes back up exactly as
    # delivered. Blob first; manual sticker entry as fallback.
    local dpw; dpw="$(read_delivery_blob 2>/dev/null)"
    if [ -z "$dpw" ] && [ -t 0 ]; then
        print_warning "No usable delivery-credentials blob (missing/undecryptable)."
        read -p "Enter the delivery (sticker) password to restore [blank = keep current .env]: " dpw
    fi
    if [ -n "$dpw" ]; then
        update_env_value ".env" "AUTHENTIK_BOOTSTRAP_PASSWORD" "$dpw"
        update_env_value ".env" "BACKUP_ENCRYPTION_PASSWORD" "$dpw"
        print_success "Restored the delivery (sticker) password to .env."
        print_info "  Bring the stack back up; then run 'rzfz set-admin-password --reset-to-default'"
        print_info "  so every per-app admin returns to the delivery password too."
    else
        print_warning "Delivery password not restored — .env keeps its current (possibly rotated) values."
    fi
    print_success "Factory reset complete."
}

# ==============================================================================
# Wait for Authentik Init
# ==============================================================================
wait_for_authentik_init() {
    print_step "Waiting for Authentik initialization..."
    print_warning "This may take 2-3 minutes on first startup."

    local max_wait=300
    local waited=0

    # Wait for authentik-init container
    while [ $waited -lt 60 ]; do
        if docker ps --format '{{.Names}}' | grep -q "authentik-init"; then
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done

    if docker ps --format '{{.Names}}' | grep -q "authentik-init"; then
        timeout $max_wait docker compose logs -f authentik-init 2>&1 | while IFS= read -r line; do
            echo "  $line"
            if echo "$line" | grep -q "Initialization complete\|already initialized\|Skipping initialization"; then
                break
            fi
        done || true
    fi

    sleep 5
    if docker ps --format '{{.Names}}' | grep -q "authentik-init"; then
        docker wait authentik-init 2>/dev/null || true
    fi

    print_success "Authentik initialization complete."
}

# ==============================================================================
# Show Completion Message
# ==============================================================================
show_completion_message() {
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                                                                  ║${NC}"
    echo -e "${GREEN}║            🎉 Installation Complete! 🎉                          ║${NC}"
    echo -e "${GREEN}║                                                                  ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    local domain="${CONFIG_DOMAIN:-localhost}"

    echo "Your rzfz.ai stack is now running!"
    echo ""
    echo "Access your services at:"
    echo "  • Chat UI:       https://chat.${domain}"
    echo "  • Dify:          https://dify.${domain}"
    echo "  • LLM:           https://llm.${domain}"
    echo "  • Auth:          https://auth.${domain}"
    echo ""
    echo "Admin Credentials:"
    echo "  • Username:      admin (or akadmin for Authentik)"
    echo "  • Password:      (the password you configured)"
    echo ""
    echo "Useful commands:"
    echo "  • View status:    docker compose ps"
    echo "  • View logs:      docker compose logs -f"
    echo "  • Reconfigure:    rzfz setup --init"
    echo "  • Manage backups: rzfz backup"
    echo ""
    echo -e "${YELLOW}Important: Bookmark your admin password securely!${NC}"
    echo ""
}

# ==============================================================================
# Main Script
# ==============================================================================

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -d|--domain)
            CONFIG_DOMAIN="$2"
            shift 2
            ;;
        -t|--timezone)
            CONFIG_TIMEZONE="$2"
            shift 2
            ;;
        -p|--password)
            CONFIG_ADMIN_PASSWORD="$2"
            shift 2
            ;;
        -e|--email)
            CONFIG_ADMIN_EMAIL="$2"
            shift 2
            ;;
        --profiles)
            CONFIG_PROFILES="$2"
            shift 2
            ;;
        --hardware)
            CONFIG_HARDWARE="$2"
            shift 2
            ;;
        --scenario)
            CONFIG_SCENARIO="$2"
            shift 2
            ;;
        --tls-mode)
            CONFIG_TLS_MODE="$2"
            shift 2
            ;;
        --gpustack-mode)
            CONFIG_GPUSTACK_MODE="$2"
            shift 2
            ;;
        --gpustack-server-url)
            CONFIG_GPUSTACK_SERVER_URL="$2"
            shift 2
            ;;
        --gpustack-token)
            CONFIG_GPUSTACK_TOKEN="$2"
            shift 2
            ;;
        --google-client-id)
            CONFIG_GOOGLE_CLIENT_ID="$2"
            shift 2
            ;;
        --google-client-secret)
            CONFIG_GOOGLE_CLIENT_SECRET="$2"
            shift 2
            ;;
        --enable-entra)
            CONFIG_ENTRA_ENABLED="true"
            shift 1
            ;;
        --entra-client-id)
            CONFIG_ENTRA_CLIENT_ID="$2"
            CONFIG_ENTRA_ENABLED="true"
            shift 2
            ;;
        --entra-client-secret)
            CONFIG_ENTRA_CLIENT_SECRET="$2"
            shift 2
            ;;
        --entra-tenant-id)
            CONFIG_ENTRA_TENANT_ID="$2"
            shift 2
            ;;
        --entra-oauth-domain)
            CONFIG_ENTRA_OAUTH_DOMAIN="$2"
            shift 2
            ;;
        --smtp-mode)
            CONFIG_SMTP_MODE="$2"
            shift 2
            ;;
        --smtp-relay-host)
            CONFIG_SMTP_RELAY_HOST="$2"
            shift 2
            ;;
        --smtp-relay-user)
            CONFIG_SMTP_RELAY_USERNAME="$2"
            shift 2
            ;;
        --smtp-relay-pass)
            CONFIG_SMTP_RELAY_PASSWORD="$2"
            shift 2
            ;;
        --package)
            CONFIG_PACKAGE="$2"
            shift 2
            ;;
        --llm-experimental)
            # M029-S04: opt into the GPUStack v2.1.x EXPERIMENTAL runtime
            # (`llm` profile). Default is the stable `llm-legacy` for AMD or
            # `llm-cpu` for CPU. The flag is consumed by apply_package_preset
            # below, which substitutes the right profile token + may also
            # restore the COMPOSE_FILE override that the v2.1.x path uses.
            CONFIG_LLM_EXPERIMENTAL=true
            shift 1
            ;;
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        --skip-interactive)
            SKIP_INTERACTIVE=true
            shift
            ;;
        --no-secrets)
            REGENERATE_SECRETS=false
            shift
            ;;
        --force)
            FORCE_REINIT=true
            shift
            ;;
        --factory-default|--factory-reset)
            FACTORY_DEFAULT=true
            shift
            ;;
        --with-acceptance)
            # M032-S06: run post-deploy acceptance probes after a successful
            # init. Probes only test currently-enabled profiles; pair with
            # --with-acceptance-include-disabled for the thorough check.
            WITH_ACCEPTANCE=true
            shift
            ;;
        --with-acceptance-include-disabled)
            # M032-S06: run probes AND cycle each disabled profile up→probe→down.
            # Slower (~5 min/profile); intended for handover or release-cycle use.
            WITH_ACCEPTANCE=true
            WITH_ACCEPTANCE_INCLUDE_DISABLED=true
            shift
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

# ==============================================================================
# Apply Package Preset (if specified)
# ==============================================================================
apply_package_preset() {
    # M029-S04: pick the LLM profile token based on hardware + experimental
    # opt-in. Stable defaults: `llm-legacy` for AMD (GPUStack 0.7.1 + custom
    # Vulkan build), `llm-cpu` for CPU (GPUStack 0.7.1-cpu). EXPERIMENTAL
    # opt-in (`--llm-experimental`): `llm` for both (GPUStack 2.1.x +
    # HARDWARE=<amd|cpu> overlay). Customers on AMD Strix Halo who hit
    # known v2.1.x issues (rc6.7 #48 leak / upstream PR #5255) can flip
    # via the Configuration Portal "Modules → LLM Runtime" toggle without
    # reinitialising — that path also writes COMPOSE_PROFILES.
    _pick_llm_profile_token() {
        # $1 = hardware token (amd | cpu | nvidia)
        if [ "${CONFIG_LLM_EXPERIMENTAL:-false}" = "true" ]; then
            echo "llm"
        else
            case "$1" in
                amd)        echo "llm-legacy" ;;
                cpu)        echo "llm-cpu" ;;
                nvidia)     echo "llm" ;;  # NVIDIA only on the new path; vLLM upstream
                *)          echo "llm-legacy" ;;
            esac
        fi
    }
    case "$CONFIG_PACKAGE" in
        single-box)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-amd}"
            llm_profile=$(_pick_llm_profile_token "$CONFIG_HARDWARE")
            print_info "Applying package preset: single-box"
            print_substep "Self-signed TLS, all profiles incl. Gitea, ${llm_profile} (HARDWARE=${CONFIG_HARDWARE}), GPUStack standalone"
            CONFIG_SCENARIO="${CONFIG_SCENARIO:-base}"
            CONFIG_TLS_MODE="${CONFIG_TLS_MODE:-selfsigned}"
            CONFIG_PROFILES="${CONFIG_PROFILES:-chat,dify,${llm_profile},monitor,searxng,stts,gotenberg,gitea}"
            CONFIG_GPUSTACK_MODE="${CONFIG_GPUSTACK_MODE:-standalone}"
            CONFIG_TIMEZONE="${CONFIG_TIMEZONE:-Europe/Berlin}"
            SKIP_INTERACTIVE=true
            ;;
        master-cpu)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-cpu}"
            llm_profile=$(_pick_llm_profile_token "$CONFIG_HARDWARE")
            print_info "Applying package preset: master-cpu"
            print_substep "Let's Encrypt TLS, all profiles, ${llm_profile} (HARDWARE=${CONFIG_HARDWARE}), GPUStack master"
            CONFIG_SCENARIO="${CONFIG_SCENARIO:-google}"
            CONFIG_TLS_MODE="${CONFIG_TLS_MODE:-letsencrypt}"
            CONFIG_PROFILES="${CONFIG_PROFILES:-chat,dify,${llm_profile},monitor,searxng,stts,gotenberg}"
            CONFIG_GPUSTACK_MODE="${CONFIG_GPUSTACK_MODE:-master}"
            CONFIG_TIMEZONE="${CONFIG_TIMEZONE:-Europe/Berlin}"
            SKIP_INTERACTIVE=true
            ;;
        testvm-cpu)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-cpu}"
            llm_profile=$(_pick_llm_profile_token "$CONFIG_HARDWARE")
            print_info "Applying package preset: testvm-cpu"
            print_substep "Self-signed TLS, all profiles, ${llm_profile} (HARDWARE=${CONFIG_HARDWARE}), GPUStack master, base auth"
            CONFIG_SCENARIO="${CONFIG_SCENARIO:-base}"
            CONFIG_TLS_MODE="${CONFIG_TLS_MODE:-selfsigned}"
            CONFIG_PROFILES="${CONFIG_PROFILES:-chat,dify,${llm_profile},monitor,searxng,stts,gotenberg,gitea}"
            CONFIG_GPUSTACK_MODE="${CONFIG_GPUSTACK_MODE:-master}"
            CONFIG_TIMEZONE="${CONFIG_TIMEZONE:-Europe/Berlin}"
            SKIP_INTERACTIVE=true
            ;;
        worker-box)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-amd}"
            llm_profile=$(_pick_llm_profile_token "$CONFIG_HARDWARE")
            print_info "Applying package preset: worker-box"
            print_substep "Self-signed TLS, core + ${llm_profile} (HARDWARE=${CONFIG_HARDWARE}) + monitor, GPUStack worker"
            CONFIG_SCENARIO="${CONFIG_SCENARIO:-base}"
            CONFIG_TLS_MODE="${CONFIG_TLS_MODE:-selfsigned}"
            CONFIG_PROFILES="${CONFIG_PROFILES:-${llm_profile},monitor}"
            CONFIG_GPUSTACK_MODE="${CONFIG_GPUSTACK_MODE:-worker}"
            CONFIG_TIMEZONE="${CONFIG_TIMEZONE:-Europe/Berlin}"
            SKIP_INTERACTIVE=true
            # Worker re-init should not be blocked by existing installation check
            FORCE_REINIT=true
            # Validate required worker parameters
            if [ -z "$CONFIG_GPUSTACK_SERVER_URL" ] || [ -z "$CONFIG_GPUSTACK_TOKEN" ]; then
                print_error "Package 'worker-box' requires --gpustack-server-url and --gpustack-token"
                echo ""
                echo "Usage:"
                echo "  $0 --package worker-box --domain worker.local --password 'Secret!' \\"
                echo "     --gpustack-server-url http://master:9090 --gpustack-token YOUR_TOKEN"
                exit 1
            fi
            ;;
        "")
            # No package specified, continue normally
            return 0
            ;;
        *)
            print_error "Unknown package: $CONFIG_PACKAGE"
            echo "Available packages: single-box, master-cpu, testvm-cpu, worker-box"
            echo "Use --help for details."
            exit 1
            ;;
    esac
    echo ""
}

# Start
print_banner

# Apply package preset before anything else (sets defaults for subsequent steps)
if [ -n "$CONFIG_PACKAGE" ]; then
    apply_package_preset
fi

# Step 0: Check system prerequisites
check_system_prerequisites

# Factory reset if requested
if [ "$FACTORY_DEFAULT" = true ]; then
    factory_reset
fi

# Step 1: Setup environment files
setup_env_files

# Step 1.5: Check for existing installation (safety check)
if [ "$FACTORY_DEFAULT" = false ]; then
    check_existing_installation
fi

# Step 1.7: Validate no known default secrets remain
validate_no_default_secrets

# Step 2: Generate secrets (mandatory for new installations)
if [ "$REGENERATE_SECRETS" = true ]; then
    regenerate_all_secrets
fi

# Step 2.5: Restrict .env file permissions (F-061)
chmod 600 .env 2>/dev/null || true
[ -f ".env.dify" ] && chmod 600 .env.dify 2>/dev/null || true
mkdir -p backups && chmod 700 backups 2>/dev/null || true
# BSB-03 / R-DEF-03 / R-COMP-13 — pre-create .checksums.db so the
# narrow rw bind mount in core/compose.yml (razzfazz-config) is a FILE,
# not a docker-auto-created directory. (#22: razzfazz-setup removed.)
# Without this, first compose up creates /stack/.checksums.db as a dir
# and checksum_manager.py raises IsADirectoryError on first sqlite open.
[ -f ".checksums.db" ] || touch .checksums.db
chmod 600 .checksums.db 2>/dev/null || true

# Step 3: Interactive or CLI configuration
if [ "$SKIP_INTERACTIVE" = false ]; then
    interactive_config
else
    # Apply defaults if not set via CLI
    CONFIG_DOMAIN="${CONFIG_DOMAIN:-$DEFAULT_DOMAIN}"
    CONFIG_TIMEZONE="${CONFIG_TIMEZONE:-$DEFAULT_TIMEZONE}"
    CONFIG_PROFILES="${CONFIG_PROFILES:-$DEFAULT_PROFILES}"
    CONFIG_SCENARIO="${CONFIG_SCENARIO:-$DEFAULT_SCENARIO}"

    if [ -z "$CONFIG_ADMIN_PASSWORD" ]; then
        # Generate a random password if not provided
        CONFIG_ADMIN_PASSWORD="$(generate_password 16)"
        print_warning "No admin password provided. Generated: $CONFIG_ADMIN_PASSWORD"
        print_warning "Please save this password!"
    fi
fi

# Step 4: Apply configuration
apply_configuration

# Step 5: Build and pull images
if [ "$SKIP_BUILD" = false ]; then
    build_and_pull_images
else
    print_warning "Skipping docker build/pull (--skip-build flag)"
fi

# Step 5b: Prepare Matrix (Synapse) configuration if matrix profile is enabled
if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "matrix"; then
    print_step "Preparing Synapse Matrix homeserver configuration..."

    # The matrix module lives under modules/apps/matrix/ after the 2026.07 dir
    # reorg (#26). Render the .rendered / config.json artifacts INTO the tracked
    # module dir (compose.yml bind-mounts ./synapse and ./element-web/config.json
    # relative to itself). The pre-reorg top-level matrix/ dir is untracked and
    # absent on a fresh clone — never render or mount from there.
    # NOTE: this Step 5b block runs at the script's top level (init.sh is exec'd
    # by rzfz, not sourced into a function), so `local` is invalid here and would
    # abort under `set -e` — use plain assignments.
    matrix_dir="modules/apps/matrix"

    # Render homeserver.yaml from template (Synapse requires pre-rendered config)
    if [ -f "${matrix_dir}/synapse/homeserver.yaml" ]; then
        export SYNAPSE_SERVER_NAME="${MAIN_DOMAIN}"
        export POSTGRES_USER="${POSTGRES_USER:-docker}"
        export POSTGRES_PASSWORD
        export SYNAPSE_DB="${SYNAPSE_DB:-synapse_db}"
        export MATRIX_DOMAIN="${MATRIX_DOMAIN:-matrix.${MAIN_DOMAIN}}"
        export AUTHENTIK_DOMAIN="${AUTHENTIK_DOMAIN:-auth.${MAIN_DOMAIN}}"
        export MATRIX_CLIENT_ID
        export SYNAPSE_CLIENT_SECRET
        export SYNAPSE_MACAROON_SECRET_KEY
        export SYNAPSE_FORM_SECRET
        export SYNAPSE_REGISTRATION_SHARED_SECRET
        envsubst < "${matrix_dir}/synapse/homeserver.yaml" > "${matrix_dir}/synapse/homeserver.yaml.rendered"
        print_success "homeserver.yaml rendered for server_name=${MAIN_DOMAIN}"
    fi

    # Render Element Web config.json from template. The template references BOTH
    # ${MAIN_DOMAIN} and ${MATRIX_DOMAIN} (the homeserver base_url) — the restricted
    # envsubst allow-list must include both, or base_url ships as the literal
    # "https://${MATRIX_DOMAIN}" and Element can't reach the homeserver. MATRIX_DOMAIN
    # was exported above; export MAIN_DOMAIN and re-assert MATRIX_DOMAIN here so this
    # render is correct even if the synapse branch above was skipped.
    if [ -f "${matrix_dir}/element-web/config.json.tpl" ]; then
        export MAIN_DOMAIN
        export MATRIX_DOMAIN="${MATRIX_DOMAIN:-matrix.${MAIN_DOMAIN}}"
        envsubst '${MAIN_DOMAIN} ${MATRIX_DOMAIN}' < "${matrix_dir}/element-web/config.json.tpl" > "${matrix_dir}/element-web/config.json"
        print_success "Element Web config.json rendered"
    fi

    # Seed the Synapse trust store (compose sets
    # SSL_CERT_FILE=/synapse-config/caddy-local-ca.crt). Start from the host's
    # public CA bundle so Let's Encrypt endpoints (auth.<domain>) verify. For
    # TLS_MODE=internal, Step 7b appends Caddy's live internal root CA post-start
    # so the self-signed auth.<domain>/matrix.<domain> also verify. This file was
    # previously an untracked pre-reorg leftover — nothing provisioned it, so a
    # fresh clone had no trust anchor and OIDC login failed (#26).
    if [ -r /etc/ssl/certs/ca-certificates.crt ]; then
        cp /etc/ssl/certs/ca-certificates.crt "${matrix_dir}/synapse/caddy-local-ca.crt"
        print_substep "Seeded Synapse CA trust store from host public CA bundle"
    else
        : > "${matrix_dir}/synapse/caddy-local-ca.crt"
    fi

    # Note: ${AUTHENTIK_DOMAIN} and ${MATRIX_DOMAIN} are registered
    # as Docker network aliases on the caddy container (core/compose.yml),
    # so Synapse reaches them via embedded DNS — no CADDY_IP detection
    # needed.
fi

# Step 5d: Install kernel stability tunables (M018 / S03.5 second half).
# Writes /etc/sysctl.d/99-razzfazz-stability.conf and reloads. The defaults
# make the kernel reboot promptly on OOM / oops instead of going into a
# silent hang (observed during M018 stress testing on 0.91). Idempotent —
# skips if the target file already matches the source.
SYSCTL_SRC="${SCRIPT_DIR:-$(pwd)}/core/sysctl/99-razzfazz-stability.conf"
SYSCTL_DST="/etc/sysctl.d/99-razzfazz-stability.conf"
if [ -r "$SYSCTL_SRC" ]; then
    print_step "Installing kernel stability tunables..."
    if [ -f "$SYSCTL_DST" ] && cmp -s "$SYSCTL_SRC" "$SYSCTL_DST"; then
        print_substep "Already installed and current ($SYSCTL_DST)."
    else
        if sudo cp "$SYSCTL_SRC" "$SYSCTL_DST" 2>/dev/null && sudo sysctl --system >/dev/null 2>&1; then
            print_substep "Installed $SYSCTL_DST + reloaded."
            print_substep "Active: vm.panic_on_oom=$(sysctl -n vm.panic_on_oom 2>/dev/null), kernel.panic=$(sysctl -n kernel.panic 2>/dev/null), vm.swappiness=$(sysctl -n vm.swappiness 2>/dev/null)"
        else
            print_warning "Could not install $SYSCTL_DST (sudo required). Apply manually:"
            print_info "  sudo cp $SYSCTL_SRC $SYSCTL_DST && sudo sysctl --system"
        fi
    fi
fi

# Step 5e: AMD Strix Halo GRUB cmdline tunables (M034 S04 / was M033 S32b).
# kernel 6.17+ / 7.0 in-tree amdgpu silently ignores amdgpu.gtt_size on Strix
# Halo (gfx1151); without ttm.pages_limit + ttm.page_pool_size sized to RAM, a
# large-model load triggers a 12-22k kworker D-state pile-up (verified on
# worker-box-1: load 453 → 1.97 after the fix). The deprecated
# scripts/upgrade-host-kernel.sh applied these on the mainline-kernel path; a
# FRESH 26.04 install never got them (S02 finding). Write a drop-in (idempotent,
# survives grub package updates) rather than editing /etc/default/grub directly.
# Gated on Strix Halo detection so non-Strix AMD / NVIDIA / CPU boxes are
# untouched. Takes effect on next reboot (init does not reboot).
STRIX_GRUB_DST="/etc/default/grub.d/50-razzfazz-strix.cfg"
if lspci -nn 2>/dev/null | grep -qiE "Strix.*Halo|\[1022:150[789]\]"; then
    print_step "Configuring AMD Strix Halo GRUB tunables (kworker-storm fix)..."
    RAM_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
    STRIX_PAGES=$(( RAM_KB / 4 ))      # full RAM in 4 KiB pages
    STRIX_HALF=$(( STRIX_PAGES / 2 ))  # page_pool_size = half (proven formula)
    strix_tmp=$(mktemp)
    cat > "$strix_tmp" <<EOF
# razzfazz.ai — AMD Strix Halo (gfx1151) amdgpu stability tunables (M034 S04).
# Sizes the TTM/GTT pool to RAM so large-model load doesn't trigger the kernel
# 6.17+/7.0 kworker D-state storm. pages_limit = RAM_KB/4 (full RAM, 4 KiB
# pages); page_pool_size = half. grub.d drop-ins are sourced AFTER
# /etc/default/grub, so this appends to the existing cmdline.
GRUB_CMDLINE_LINUX_DEFAULT="\$GRUB_CMDLINE_LINUX_DEFAULT amdgpu.cwsr_enable=0 amd_iommu=off ttm.pages_limit=${STRIX_PAGES} ttm.page_pool_size=${STRIX_HALF}"
EOF
    sudo mkdir -p /etc/default/grub.d 2>/dev/null
    if [ -f "$STRIX_GRUB_DST" ] && sudo cmp -s "$strix_tmp" "$STRIX_GRUB_DST" 2>/dev/null; then
        print_substep "Already installed and current ($STRIX_GRUB_DST)."
    elif sudo cp "$strix_tmp" "$STRIX_GRUB_DST" 2>/dev/null && sudo update-grub >/dev/null 2>&1; then
        print_substep "Wrote $STRIX_GRUB_DST (ttm.pages_limit=${STRIX_PAGES}, page_pool_size=${STRIX_HALF}); effective next reboot."
    else
        print_warning "Could not write Strix Halo GRUB tunables (sudo required). Apply manually:"
        print_info "  sudo cp $strix_tmp $STRIX_GRUB_DST && sudo update-grub"
        strix_tmp=""  # keep the temp file for the manual step
    fi
    [ -n "$strix_tmp" ] && rm -f "$strix_tmp"
fi

# Step 5f: Secure Boot verification (M034 S08). After the 24.04→26.04 migration
# the box runs a Canonical-SIGNED kernel (7.0.x), so Secure Boot CAN and — for
# NIS2 posture — SHOULD be on. The migration path disables it (mainline-kernel
# era); on 26.04 it's re-enableable. This is a post-install ADVISORY (not a
# gate): warn if it's off so the operator re-enables it in BIOS/UEFI. Per-vendor
# steps are in docs/upgrade-guide-26.04-lts.md.
if command -v mokutil >/dev/null 2>&1; then
    SB_STATE="$(mokutil --sb-state 2>/dev/null || echo unknown)"
    if echo "$SB_STATE" | grep -qi 'enabled'; then
        print_substep "Secure Boot: enabled ✓"
    else
        print_warning "Secure Boot is OFF ($SB_STATE)."
        print_info "  26.04 ships a signed kernel — re-enable Secure Boot in BIOS/UEFI"
        print_info "  for NIS2 posture (per-vendor steps: docs/upgrade-guide-26.04-lts.md)."
    fi
fi

# Step 6: Start the stack
print_step "Starting the stack..."
# #27 / #152 (Codeberg/public export + OIDC CA superset): the dev repo tracks
# certs/caddy-ca.pem, bind-mounted into the native-OIDC clients (Open WebUI,
# Gitea, Vaultwarden) as SSL_CERT_FILE / REQUESTS_CA_BUNDLE — which REPLACE the
# whole TLS trust store. publish-public.sh SCRUBS certs/ (GATE 1, correct for
# hygiene), so on an EXPORT install the file is absent; Docker would then auto-
# create the bind target as a DIRECTORY → SSL_CERT_FILE points at a dir → native
# OIDC 500s with IsADirectoryError. It MUST exist as a FILE before `compose up`.
# SEED it NOW with the host system PUBLIC CA bundle so the OIDC clients start with
# a working public trust store (Let's Encrypt auth.<domain>, Google, Entra); the
# ensure_oidc_ca_superset call after Caddy is up re-runs the same base + APPENDS
# the box's internal root CA on TLS_MODE=internal + restarts the clients. This
# base seed is the #152 fix for Let's Encrypt boxes (init previously wrote NOTHING
# on LE, leaving only a placeholder → authlib CERTIFICATE_VERIFY_FAILED → login 500).
mkdir -p certs
if [ -r /etc/ssl/certs/ca-certificates.crt ]; then
    cat /etc/ssl/certs/ca-certificates.crt > certs/caddy-ca.pem 2>/dev/null || true
elif [ -r /etc/pki/tls/certs/ca-bundle.crt ]; then
    cat /etc/pki/tls/certs/ca-bundle.crt > certs/caddy-ca.pem 2>/dev/null || true
fi
# Guarantee a non-empty FILE even if no system bundle was readable (never a dir).
[ -s certs/caddy-ca.pem ] || printf '# razzfazz.ai OIDC CA bundle (#152) — seeded pre-compose-up\n' > certs/caddy-ca.pem

# #125 P1: populate the box-local Enterprise-docs overlay (overlay/enterprise/docs)
# BEFORE `compose up` so the razzfazz-help bind-mount has a source directory. On an
# internal box this mirrors docs/enterprise/ into the overlay so the Help-UI serves
# the gated Enterprise docs; on a public/Codeberg box docs/enterprise/ is absent → the
# helper only creates the (empty) overlay dir → the Help-UI serves community-only.
# Best-effort — never abort init on a docs-overlay hiccup.
if [ -x "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" ]; then
    print_substep "Populating box-local Enterprise-docs overlay for the Help-UI (#125)..."
    "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" 2>&1 || print_warning "Enterprise-docs overlay sync failed (Help-UI will serve community-only)."
fi
# Pre-populate the authentik-media volume BEFORE the full `up`. The authentik
# image ships /data/media as a `media -> /media` symlink; Docker copies image
# content into an *empty* named volume on first mount. When authentik-server,
# authentik-worker and authentik-migrate-hop are CREATED concurrently by
# `compose up` (depends_on orders start, not create/volume-populate), they race
# to create that symlink and one loses with "failed to create symlink ... file
# exists", aborting init. Running the lightweight alpine media-migrator alone
# first makes the volume non-empty (creates /data/media), so the full `up`
# triggers no image-copy and no race. Idempotent + a no-op on a warm volume.
# #184 WS2b: compose the network-mode overlay into COMPOSE_FILE (offline →
# compose.offline.yml + RAZZFAZZ_OFFLINE; proxied → compose.corporate-proxy.yml;
# online → neither) BEFORE the full `up` so it takes effect on the first start.
# Best-effort; a fresh online install is a no-op.
ensure_network_mode_overlay ".env"
# #184 WS2a: compose the UNIVERSAL no-runtime-build overlay (all modes) so the
# `up` below — and every later operator `docker compose up -d` / module enable —
# can never build. Wired here, AFTER the install-time `docker compose build`
# above (build_docker_images), so that build still saw the build: contexts.
ensure_nobuild_overlay ".env"

docker compose up -d --no-deps authentik-media-migrator 2>/dev/null || true
docker wait authentik-media-migrator >/dev/null 2>&1 || true
docker compose up -d
print_success "Stack started."

# rc6.7 #46 v3: openhands needs an interface on docker's default `bridge`
# network so its spawned sandbox runtimes (which always land on `bridge`)
# can call back to it without going through the host. Compose can't
# attach a service to the default bridge (network-scoped aliases aren't
# supported there), so we do it here at runtime. Idempotent: docker
# silently no-ops if the container is already attached. Watch on
# upstream OpenHands version bumps — see docs/upstream-monkey-patches.md.
if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "openhands"; then
    if docker ps --format '{{.Names}}' | grep -qx openhands; then
        docker network connect bridge openhands 2>/dev/null || true
        # Re-run the URL substitution monkey-patch now that openhands has
        # its bridge IP — initial entrypoint pass ran without it.
        docker exec openhands sh /opt/openhands-monkeypatch.sh 2>&1 | sed 's/^/  /' || true
        # Restart so the patched .py files get re-imported on next module load
        docker restart openhands >/dev/null 2>&1 || true
        print_substep "OpenHands attached to docker default bridge + URL monkey-patch applied"
    fi
fi

# #36: Caddy → docker default `bridge`, for the PER-USER openhands agents
# (agent-manager / `agents` profile). Those instances advertise their sandbox
# to the browser as `openhands-<hash>.agents.<domain>/sandbox/<port>/`, which
# Caddy reverse-proxies to `host.docker.internal:<port>`. Without Caddy on the
# bridge (+ a resolvable host.docker.internal) that proxy fails and the browser
# shows "Failed to connect to server" / the sandbox stream never connects — even
# though the backend agent runs fine. Compose can't attach Caddy to the default
# bridge (network-scoped aliases unsupported there), and the `host-gateway`
# keyword resolves to `invalid IP` on some daemons, so pin the concrete gateway
# IP into Caddy's /etc/hosts. Gated on `agents` (that's what enables the
# agent-manager); idempotent. NB: core/compose.yml referenced a
# `connect_caddy_to_bridge()` in scripts/lib.sh that never existed — this is
# the real implementation.
if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "agents"; then
    if docker ps --format '{{.Names}}' | grep -qx caddy; then
        docker network connect bridge caddy 2>/dev/null || true
        _bridge_gw=$(docker network inspect bridge \
            --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
        _bridge_gw="${_bridge_gw:-172.17.0.1}"
        docker exec -u root caddy sh -c \
            "grep -qF '${_bridge_gw}\thost.docker.internal' /etc/hosts 2>/dev/null || \
             { grep -v 'host.docker.internal' /etc/hosts > /tmp/.h 2>/dev/null; \
               printf '%s\thost.docker.internal\n' '${_bridge_gw}' >> /tmp/.h; \
               cat /tmp/.h > /etc/hosts; }" 2>/dev/null || true
        print_substep "Caddy attached to docker default bridge (per-user OpenHands sandbox routing)"
    fi
fi

# Step 7: Wait for initialization
wait_for_authentik_init

# Step 7a: Rebuild certs/caddy-ca.pem as an OIDC CA SUPERSET for in-stack clients
# (#55/#70/#152). The native-OIDC clients (Open WebUI, Gitea, Vaultwarden) mount
# certs/caddy-ca.pem as SSL_CERT_FILE / REQUESTS_CA_BUNDLE, which REPLACE the
# entire TLS trust store. It must therefore trust BOTH:
#   - PUBLIC issuers (Let's Encrypt auth.<domain>, Google, Microsoft Entra) — the
#     system CA bundle seeded above as the base; AND
#   - the box's own Caddy internal root CA on TLS_MODE=internal (self-signed
#     auth.<domain>), appended live from the running caddy container.
# Prior to #152 init wrote ONLY the internal CA (TLS_MODE=internal) and NOTHING on
# Let's Encrypt, so LE boxes trusted a placeholder → authlib authorize_redirect
# threw CERTIFICATE_VERIFY_FAILED → login 500 on EVERY LE box (root-caused on prod
# 8.246). ensure_oidc_ca_superset (scripts/lib.sh, sourced at the top of init.sh)
# handles BOTH TLS modes, both public + internal issuers, and restarts the running
# OIDC clients so they reload the trust store. Best-effort under set -e.
ensure_oidc_ca_superset

# Step 7b: Post-start Matrix setup (volume ownership)
# CADDY_IP detection retired — see core/compose.yml caddy network aliases.
if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "matrix"; then
    print_step "Configuring Synapse Matrix homeserver post-start setup..."

    # Pre-chown synapse-data volume to uid 991 (synapse user)
    # Fresh Docker volumes are root-owned; Synapse needs ownership to create signing keys
    # Top-level (not in a function): `local` would abort under `set -e` (#26).
    synapse_vol="${COMPOSE_PROJECT_NAME:-razzfazz-stack}_synapse-data"
    if docker volume inspect "$synapse_vol" >/dev/null 2>&1; then
        docker run --rm -v "${synapse_vol}:/data" alpine chown 991:991 /data 2>/dev/null || true
        print_substep "synapse-data volume ownership set to 991:991"
    fi

    # Append Caddy's live internal root CA to Synapse's trust store so OIDC to
    # auth.<domain> (and federation to matrix.<domain>) — both self-signed behind
    # Caddy under TLS_MODE=internal — verify. The public-CA baseline was seeded in
    # Step 5b; here we append the box-specific Caddy root (#26/#55). Non-fatal.
    # Read TLS_MODE from .env directly (never `source` operator-edited .env).
    # Top-level block: plain assignments only, no `local` (would abort set -e).
    _mx_tls_mode="$(grep -m1 '^TLS_MODE=' .env 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'" ' )"
    if [ "$_mx_tls_mode" = "internal" ]; then
        _mx_ca_src="/data/caddy/pki/authorities/local/root.crt"
        if docker exec caddy test -f "$_mx_ca_src" 2>/dev/null \
           && docker exec caddy cat "$_mx_ca_src" >> "modules/apps/matrix/synapse/caddy-local-ca.crt" 2>/dev/null; then
            print_substep "Appended Caddy internal root CA to Synapse trust store"
        else
            print_warning "Could not append Caddy CA to Synapse trust store — OIDC login may fail cert verification (fix: docker exec caddy cat $_mx_ca_src >> modules/apps/matrix/synapse/caddy-local-ca.crt && docker restart synapse)."
        fi
    fi

    # Restart Synapse with correct volume ownership
    if docker ps --format '{{.Names}}' | grep -q "^synapse$"; then
        print_substep "Restarting Synapse to apply volume ownership..."
        docker compose restart synapse >/dev/null 2>&1 || true
    fi
fi

print_step "Waiting for other initialization tasks..."
sleep 5
if docker ps --format '{{.Names}}' | grep -q "dify-init-permissions"; then
    docker wait dify-init-permissions 2>/dev/null || true
fi
print_success "All initialization tasks complete."

# BSB-06 — deploy-time per-app group-binding lint. apply-policy-bindings.py
# (run inside authentik-worker via init-authentik.sh) wraps every binding
# in try/except and silently logs "Group not found." on a typo'd group
# name. The blueprint apply path returns success regardless, so a 4-way
# binding mismatch (cf. 97f7e2c0) can ship without anyone noticing —
# until a customer hits "Erlaubnis verweigert" at SSO login.
#
# This lint reads the canonical (slug, group) map from
# core/Authentik/apply-policy-bindings.py and asserts the matching
# PolicyBinding exists in the live Authentik DB. Read-only — never
# mutates Authentik state. Failure is a warning, not fatal: init proper
# already succeeded; the operator should fix the binding script before
# the next stack restart but doesn't need to abort init for it.
if [ -x "${SCRIPT_DIR:-.}/scripts/post-install-group-lint.sh" ]; then
    print_step "BSB-06 group-binding lint..."
    if "${SCRIPT_DIR:-.}/scripts/post-install-group-lint.sh"; then
        : # success message already printed by the script
    else
        print_warning "Group-binding lint reported issues — review the output above."
        print_info    "These won't block init, but SSO will deny affected apps until fixed."
    fi
fi

# Step 7b: Build the local llama-* runner images (M022 + rc6.8 shim).
# All three runner images bundle modules/llm/runners/llama-server-shim, which
# translates GPUStack-style `--flag=value` backend_parameters into the
# `--flag value` form upstream llama-server requires. Image build context
# is modules/llm/runners (the shared shim lives there). Idempotent — docker build
# skips unchanged layers.
#   * llama-vulkan-runner:b8943   — AMD Strix Halo Vulkan (M022 preferred)
#   * llama-rocm-runner:rocm-7.2.1 — AMD Strix Halo ROCm  (alternative/A-B)
#   * llama-cpu-runner:b8000      — CPU-only (testvm-cpu, HARDWARE=cpu)
if razzfazz_is_offline; then
    # #184 WS2b: llama-runner images are loaded from the offline package, never
    # built on an air-gapped box (the build pulls a base image + apt).
    print_substep "Step 7b: OFFLINE (RAZZFAZZ_NETWORK_MODE=offline) — skipping llama-runner image builds (loaded from the offline package)."
elif echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "llm"; then
    _build_runner() {
        # $1 image:tag, $2 dockerfile path
        local _img="$1" _df="$2"
        if docker image inspect "$_img" >/dev/null 2>&1; then
            print_info "$_img already present, skipping build."
            return 0
        fi
        print_step "Building $_img..."
        if docker build -t "$_img" -f "$_df" modules/llm/runners; then
            print_success "$_img built."
        else
            print_warning "Failed to build $_img; the matching custom backend will not be usable."
            print_info "Re-run: docker build -t $_img -f $_df modules/llm/runners"
        fi
    }
    if [ "${HARDWARE:-amd}" = "amd" ]; then
        _build_runner "llama-vulkan-runner:b8943"     "modules/llm/runners/llama-vulkan/Dockerfile"
        _build_runner "llama-rocm-runner:rocm-7.2.1" "modules/llm/runners/llama-rocm/Dockerfile"
    fi
    if [ "${HARDWARE:-amd}" = "cpu" ]; then
        _build_runner "llama-cpu-runner:b8000"        "modules/llm/runners/llama-cpu/Dockerfile"
    fi
fi

# Step 7c: Register GPUStack custom backends (M018 Phase 6 / S06.4 + M022 + rc6.8)
# The llm profile uses upstream gpustack v2.x which doesn't ship a runner
# for AMD Strix Halo (gfx1151) or for CPU-only inference. We register
# three backends, all using local wrapper images that bundle the
# llama-server-shim (GPUStack `--flag=value` → `--flag value`):
#  - llama-box-vulkan-custom (M022 preferred, llama-vulkan-runner:b8943)
#  - llama-box-rocm-custom   (llama-rocm-runner:rocm-7.2.1, FROM kyuz0)
#  - llama-box-cpu-custom    (llama-cpu-runner:b8000, FROM ggml-org)
# Idempotent — safe to re-run; skips if backends already match.
if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "llm"; then
    print_step "Registering GPUStack custom backends..."
    GPUSTACK_API_KEY=$(grep '^GPUSTACK_API_KEY=' .env | cut -d= -f2-)
    if [ -n "$GPUSTACK_API_KEY" ]; then
        if GPUSTACK_API="http://localhost:${GPUSTACK_PORT:-9090}" \
           GPUSTACK_API_KEY="$GPUSTACK_API_KEY" \
           python3 modules/llm/gpustack/init-backends.py; then
            print_success "GPUStack custom backends registered."
        else
            print_warning "Backend registration exited non-zero (non-fatal)."
            print_info "Re-run: python3 modules/llm/gpustack/init-backends.py"
        fi
    else
        print_warning "GPUSTACK_API_KEY not set in .env — skipping backend registration."
        print_info "Set the key, then run: python3 modules/llm/gpustack/init-backends.py"
    fi
fi

# M032 S01: pre-warm the test venv so the first `rzfz test` invocation
# is fast (no 10-second pip-install delay). Idempotent — exits 0 if the venv
# is already present. Pure pre-warm; does NOT run any tests. The
# --with-acceptance flag wiring (which actually runs probes here) is
# below in run_acceptance_probes() (M032-S06).
bootstrap_test_venv() {
    local wrapper="$SCRIPT_DIR/legacy/razzfazz-test.sh"
    if [ ! -x "$wrapper" ]; then
        # razzfazz-test.sh not in this checkout (older bundle, or pre-M032
        # rollback). Skip silently — not fatal.
        return 0
    fi
    if [ ! -f "$SCRIPT_DIR/tests/requirements.txt" ]; then
        return 0
    fi
    print_step "Pre-warming test venv (M032)..."
    if "$wrapper" --bootstrap-only >/dev/null 2>&1; then
        print_success "Test venv ready at tests/.venv"
    else
        print_warning "Test venv bootstrap failed (non-fatal)."
        print_info "Re-run later: rzfz test --bootstrap-only"
    fi
}
bootstrap_test_venv

# M032-S06: post-deploy acceptance probe hook. Only runs when the operator
# passed --with-acceptance (or --with-acceptance-include-disabled). Wait for
# every container to leave its starting/restarting/unhealthy state (5 min
# default; override via RAZZFAZZ_ACCEPTANCE_HEALTHZ_TIMEOUT env var), then
# invoke `rzfz test --ci-mode --acceptance all` and translate its
# exit code into the init's exit code (0 → success, 4 → probes-failed,
# 5 → no-probes-found, treated as 4 here for the operator).
run_acceptance_probes() {
    print_step "Running post-deploy acceptance probes..."

    local timeout="${RAZZFAZZ_ACCEPTANCE_HEALTHZ_TIMEOUT:-300}"
    local interval=10
    local elapsed=0
    local unhealthy

    while [ "$elapsed" -lt "$timeout" ]; do
        # docker compose ps exits non-zero when no compose project is up;
        # default unhealthy=0 so we don't spin on a missing stack.
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

    # Bootstrap the venv (idempotent — no-op if pre-warmed above).
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
        print_info "Report path: $SCRIPT_DIR/tests/results/latest/acceptance-report.md"
    else
        print_error "One or more acceptance probes failed (probe-suite RC=$rc)."
        print_info "Report path: $SCRIPT_DIR/tests/results/latest/acceptance-report.md"
    fi
    return 4  # exit code 4 = probes-failed (distinct from 1 = init-failed)
}

# Show completion message
show_completion_message

# Record installed version in .env
if [ -f "VERSION" ]; then
    update_env_value ".env" "RAZZFAZZ_VERSION" "$(cat VERSION | tr -d '[:space:]')"
    print_substep "Recorded stack version $(cat VERSION | tr -d '[:space:]') in .env"
fi
if command -v git &>/dev/null && [ -d ".git" ]; then
    update_env_value ".env" "RAZZFAZZ_COMMIT" "$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
    print_substep "Recorded commit hash in .env"
fi
# STACK_HOST_PATH must be the absolute repo path on the host so
# core/compose.yml's razzfazz-config bind mount sees the same path inside
# the container as the docker daemon does outside (see core/compose.yml
# comments — without this, apply_manager's `docker compose up -d` hands
# the daemon /stack/... paths it can't resolve, and the daemon auto-
# creates a directory tree that breaks subsequent caddy/dify recreates).
update_env_value ".env" "STACK_HOST_PATH" "$(realpath .)"
print_substep "Recorded stack host path $(realpath .) in .env"
# STACK_GIT_CREDENTIALS_PATH lets image_checker fetch the upstream manifest
# via the smart-HTTP git protocol (the only thing Authentik's proxy on
# git.razzfazz.ai exempts). Falls back to /dev/null in compose if missing,
# which makes the manifest check show its workaround error instead of
# crashing — see core/config/app/services/image_checker.py.
if [ -f "${HOME}/.git-credentials" ]; then
    update_env_value ".env" "STACK_GIT_CREDENTIALS_PATH" "${HOME}/.git-credentials"
    print_substep "Recorded git-credentials path for manifest fetch"
fi

# #3: seed the canonical `store` credential helper for an https origin so the
# operator's PAT (entered now or later) persists in ~/.git-credentials and the
# upgrade-path `git fetch` authenticates without prompting. Idempotent: only
# sets it when this is a git checkout with an https origin and no helper is
# already configured. The PAT itself is still operator-supplied — this only
# wires up where git will look for it.
if [ -d .git ]; then
    _init_origin=$(git remote get-url origin 2>/dev/null || echo "")
    case "$_init_origin" in
        https://*)
            if [ -z "$(git config --get credential.helper 2>/dev/null || echo "")" ]; then
                git config credential.helper store && \
                    print_substep "Configured git credential.helper=store for upgrade fetches (#3)"
            fi
            ;;
    esac
    unset _init_origin
fi

# Take baseline checksum governance snapshot
# #22: runs on the host now (razzfazz-setup web container removed).
print_step "Taking baseline checksum governance snapshot..."
if RAZZFAZZ_STACK_ROOT="$SCRIPT_DIR" python3 "${SCRIPT_DIR}/cli/setup_lib/setup.py" \
        --checksum-take "Baseline after init" 2>/dev/null; then
    print_success "Baseline checksum snapshot created."
else
    print_warning "Could not create baseline checksum (non-critical)."
fi

# M032-S06: opt-in post-deploy acceptance probe hook. Runs LAST so that
# every other init step (compose up, secret rotation, post-install,
# baseline checksum) has finished and the stack has had a moment to
# stabilise. We've already reached this point only because the init body
# above did not abort (set -eo pipefail), so we know "init proper"
# succeeded and probe failures here represent a post-state issue rather
# than a config error. Operator decides whether to roll back; we just
# surface the signal via exit code 4.
if [ "$WITH_ACCEPTANCE" = true ]; then
    if run_acceptance_probes; then
        :
    else
        ACCEPTANCE_RC=$?
        echo ""
        print_error "Init body completed, but acceptance probes failed (exit code $ACCEPTANCE_RC)."
        print_info "Review tests/results/latest/acceptance-report.md and decide whether to roll back."
        exit "$ACCEPTANCE_RC"
    fi
fi
