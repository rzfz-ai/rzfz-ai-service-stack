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

# #2543: every repo file this run writes (a git checkout, a package's files,
# generated config) must be readable by the containers that bind-mount it.
# Under a CIS-hardened umask (027, or 077) git writes a changed file 0640/0600,
# and a non-root container whose uid is not the owner then cannot read it:
# on 0.91 razzfazz-help crash-looped on versions.json after `rzfz upgrade`.
# Secrets never relied on the umask; they are chmod'ed 600/700 explicitly.
umask 022

# ------------------------------------------------------------------------------
# #427: unconditional run log. Every init run tees its full output to a
# timestamped file — "did the image load fail, or was it skipped?" must never
# again require re-running a 30-minute provisioning step because the console
# scrollback is gone. Skipped when a parent razzfazz process already captures
# (upgrade re-exec), so one run = one log.
# --help/-h/help runs stay un-teed: the command-reference generator captures
# their stdout verbatim (a tee process-substitution can swallow it on fast
# exit), and a help call must not mint a junk run log.
_rzfz_wants_help=false
for _rzfz_a in "$@"; do case "$_rzfz_a" in -h|--help|help) _rzfz_wants_help=true ;; esac; done
if [ "$_rzfz_wants_help" = false ] && [ -z "${RZFZ_RUN_LOG:-}" ]; then
    RZFZ_LOG_DIR="${RZFZ_LOG_DIR:-$HOME/.razzfazz/logs}"
    # review #653: logs can carry env-adjacent output (set -x under
    # DEBUG_MODE would land here in full) — operator-only.
    # #2697: the log is the operator's alone (0600 whatever the umask, set just
    # before the tee), and the fallback gets an
    # unpredictable mktemp name in TMPDIR, never a guessable /tmp path.
    _rzfz_ts="$(date -u +%Y%m%dT%H%M%SZ)"
    if mkdir -p "$RZFZ_LOG_DIR" 2>/dev/null && chmod 700 "$RZFZ_LOG_DIR" 2>/dev/null; then
        RZFZ_RUN_LOG="$RZFZ_LOG_DIR/razzfazz-init-${_rzfz_ts}.log"
    else
        RZFZ_LOG_DIR="${TMPDIR:-/tmp}"
        RZFZ_RUN_LOG="$(mktemp --tmpdir="$RZFZ_LOG_DIR" --suffix=.log "razzfazz-init-${_rzfz_ts}-XXXXXX")" || RZFZ_RUN_LOG=/dev/null
    fi
    export RZFZ_RUN_LOG
    export RZFZ_LOG_DIR  # nested runs keep their kept files (hardening log, GRUB drop-in) here, not in /tmp
    # retention: keep the last 10 runs per verb
    # #667: on the verb's first-ever run the glob matches nothing → ls exits 2
    # → pipefail + set -e killed the script BEFORE any output. Cleanup must
    # never kill the run.
    ls -1t "$RZFZ_LOG_DIR"/razzfazz-init-*.log 2>/dev/null | tail -n +11 | xargs -r rm -f -- || true
    # #2697: 0600 before the tee opens it, and after the trim above, so this
    # run's own log never counts against the ten kept.
    if [ "$RZFZ_RUN_LOG" != /dev/null ]; then (umask 077; : >>"$RZFZ_RUN_LOG") 2>/dev/null || true; chmod 600 "$RZFZ_RUN_LOG" 2>/dev/null || true; fi
    exec > >(tee -a "$RZFZ_RUN_LOG") 2>&1
    echo "[log] full run log: $RZFZ_RUN_LOG"
    trap 'echo "[log] full run log: $RZFZ_RUN_LOG"' EXIT
fi

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
# shellcheck source=scripts/lib-matrix.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib-matrix.sh"
# shellcheck source=scripts/lib-backup-keyring.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib-backup-keyring.sh"
# shellcheck source=scripts/lib-delivery-blob.sh
source "${SCRIPT_DIR}/scripts/lib-delivery-blob.sh"

# #2857: the profiles init actually resolved. init only WRITES COMPOSE_PROFILES
# to .env; it never exports it, so the shell variable is empty on the
# offline/appliance (SKIP_BUILD) path and every gate that read it silently
# skipped its step (matrix: element-web/config.json became a root-owned
# directory). Resolve from CONFIG_PROFILES (what init writes), else from .env.
# Whole profile names only: `agents` must not match `agents-x`.
init_resolved_profiles() {
    if [ -n "${CONFIG_PROFILES:-}" ]; then
        printf '%s\n' "$CONFIG_PROFILES"
    else
        read_env_value "${SCRIPT_DIR}/.env" COMPOSE_PROFILES
    fi
}
init_profile_enabled() {
    init_resolved_profiles | tr -d ' ' | tr ',' '\n' | grep -qxF -- "$1"
}

# Default values
DEFAULT_DOMAIN="localhost"
DEFAULT_TIMEZONE="Europe/Berlin"
DEFAULT_ADMIN_PASSWORD=""
# #1292 (operator decision E3, 2026-09-05 / #979): the LLM Manager is the LLM
# front end of a 2026.09 box, and it is a TRIO of profiles — the manager itself,
# the model registry it pulls artifacts from, and the worker agent that runs the
# engines. One source for every caller (presets, wizard, default install) so the
# three can never drift apart. The presets and the wizard do not hang a GPUStack
# profile next to it — a first install should not be a decision about
# federation. That is a CHOICE, not a constraint: since #1442 the manager
# registers GPUStack as an external backend and fronts it, so the combination is
# supported on an existing box. (This comment used to say deploy and verify
# would disagree; that stopped being true with #1441 and #1442.)
RZFZ_LLM_MANAGER_PROFILES="llm-manager,llm-registry,llm-worker-agent"
DEFAULT_PROFILES="chat,dify,${RZFZ_LLM_MANAGER_PROFILES},monitor,searxng,stts,gotenberg,docling,cognee"
DEFAULT_SCENARIO="base"

# ------------------------------------------------------------------------------
# Release-channel / box-role constant (#27/#28)
# ------------------------------------------------------------------------------
# PUBLIC/customer boxes clone the public Codeberg mirror; INTERNAL fleet boxes
# clone the SEQIS Gitea. #602: `.env.example` ships RAZZFAZZ_CHANNEL=public (fleet
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
CONFIG_GOOGLE_OAUTH_DOMAIN=""
CONFIG_ENTRA_ENABLED="false"
CONFIG_ENTRA_CLIENT_ID=""
CONFIG_ENTRA_CLIENT_SECRET=""
CONFIG_ENTRA_TENANT_ID=""
CONFIG_ENTRA_OAUTH_DOMAIN=""
CONFIG_PACKAGE=""
CONFIG_CHANNEL=""   # --channel only (#2384); a preset writes PRESET_CHANNEL
CONFIG_PAT_MODE=""   # #2690: --store-pat -> true, --rental -> false; empty: settle once
PRESET_CHANNEL=""
CONFIG_HARDWARE=""
# #2512: LLM_MASTER_ROLE (standalone|fleet), set by the package preset. Empty
# = no preset spoke: write_llm_master_role keeps an existing value, else
# standalone.
CONFIG_LLM_MASTER_ROLE=""
# #2170 rev-B: empty means "use the default" — a package's presence does NOT
# derive `offline`. The operator's standing rule is package-first on ANY box, so
# a networked box that carries a package as a cache must keep its fallback. A
# value here is the operator saying so explicitly, which always wins.
CONFIG_NETWORK_MODE=""
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
# #2644: set by check_existing_installation (live volumes or containers) and by
# apply_main_domain (the domain .env named before this run, when it changes).
INIT_HAD_LIVE_STATE=false
INIT_PREVIOUS_DOMAIN=""
# #2644: only a domain the operator GAVE (--domain, or typed at the prompt) may
# move the subdomain keys and the domain-keyed database rows. A default never
# re-domains a box.
INIT_DOMAIN_EXPLICIT=false
SKIP_HOST_HARDENING=false
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
    echo "                            single-box: Base scenario, self-signed TLS, LLM Manager (HARDWARE=amd),"
    echo "                                        chat,dify,monitor,searxng,stts,gotenberg,docling,cognee,gitea"
    echo "                            master-cpu: Google scenario, Let's Encrypt TLS, LLM Manager (HARDWARE=cpu),"
    echo "                                        chat,dify,monitor,searxng,stts,gotenberg,docling,cognee"
    echo "                            testvm-cpu: Base scenario, self-signed TLS, LLM Manager (HARDWARE=cpu),"
    echo "                                        chat,dify,monitor,searxng,stts,gotenberg,docling,cognee,gitea"
    echo "                            worker-box: LEGACY. Base scenario, self-signed TLS, llm-legacy (HARDWARE=amd)"
    echo "                                        + monitor: a GPUStack worker of a REMOTE GPUStack master"
    echo "                                        (auto-enables --force; requires --gpustack-server-url and"
    echo "                                        --gpustack-token). An LLM Manager worker node is NOT this"
    echo "                                        preset — it joins the fleet with \`rzfz node-init\` (#266)."
    echo ""
    echo "                            #1292: the three stack presets ship the LLM Manager"
    echo "                            (llm-manager,llm-registry,llm-worker-agent) as the LLM front"
    echo "                            end — no GPUStack profile beside it. A GPUStack backend is a"
    echo "                            deliberate choice via --profiles. worker-box still joins a"
    echo "                            REMOTE GPUStack master and is unchanged."
    echo ""
    echo "Configuration Options:"
    echo "  -d, --domain DOMAIN       Set the main domain (e.g., example.com)"
    echo "  -t, --timezone TIMEZONE   Set timezone (e.g., Europe/Berlin, America/New_York)"
    echo "  -p, --password PASSWORD   Set admin password for Authentik, Komodo, Dify (and the legacy"
    echo "                            GPUStack backend when llm-legacy is enabled)"
    echo "      --password-file FILE  Read the admin password from FILE (first line), not from argv"
    echo "  -e, --email EMAIL         Set admin email for SSL certificates"
    echo "  --profiles PROFILES       Comma-separated list of profiles to enable"
    echo "                            Available: chat,dify,llm-manager,llm-registry,llm-worker-agent,"
    echo "                                       llm-legacy,"
    echo "                                       monitor,searxng,stts,gotenberg,gitea"
    echo "                            llm-manager — DEFAULT LLM front end (#1292). Takes the trio"
    echo "                                          llm-manager,llm-registry,llm-worker-agent: the"
    echo "                                          registry holds the model artifacts, the worker"
    echo "                                          agent runs the engines. The wizard offers one"
    echo "                                          LLM backend; a GPUStack profile NEXT TO it is"
    echo "                                          supported (#1442 federates it) but is not what"
    echo "                                          a first install should pick."
    echo "                            llm-legacy — STABLE: GPUStack v0.7.1. ONE profile for EVERY"
    echo "                                         hardware line since #1447; --hardware picks the"
    echo "                                         device overlay: amd => custom AMD Vulkan build"
    echo "                                         (Strix Halo), nvidia => custom CUDA llama.cpp"
    echo "                                         (Blackwell/sm_120, #946), cpu => the upstream"
    echo "                                         v0.7.1-cpu image. The retired \`llm-cuda\` and"
    echo "                                         \`llm-cpu\` tokens are rewritten on the command"
    echo "                                         line."

    echo "  --llm-experimental        REMOVED in 2026.09 (#979/#1447). GPUStack v2.1.x and its"
    echo "                            \`llm\` profile are gone from the product; the flag is still"
    echo "                            ACCEPTED so an older runbook does not abort mid-install, but"
    echo "                            it selects nothing and says so. The LLM Manager is the front"
    echo "                            of every box; --profiles llm-legacy adds the optional"
    echo "                            GPUStack 0.7.1 backend, which the manager federates."
    echo "  --hardware HARDWARE       Hardware target for the LLM runtime (amd|nvidia|cpu)."
    echo "                            Auto-detected if omitted (lspci / nvidia-smi / /dev/kfd)."
    echo "                            Selects modules/llm/compose.devices.<HARDWARE>.yml as the overlay."
    echo "  --network-mode MODE       Egress posture (online|proxied|offline). Default: online."
    echo "                            An offline package present on the box does NOT make it"
    echo "                            offline: the package is used first for what it carries,"
    echo "                            and fallback downloads stay permitted."
    echo "                            Pass --network-mode offline when this box has no internet."
    echo "  --scenario SCENARIO       Authentik deployment scenario (base or google)"
    echo "                            base: Standard username/password authentication"
    echo "                            google: Google SSO integration"
    echo "  --tls-mode MODE           TLS certificate mode (letsencrypt, selfsigned, or certificate)"
    echo "                            letsencrypt: Real certificates from Let's Encrypt (default)"
    echo "                            selfsigned: Self-signed certificates for local/testing"
    echo "                            certificate: Use your own wildcard certificate"
    echo "                                         Place cert.pem and key.pem in ./certs/"
    echo "  --gpustack-mode MODE      (llm-legacy only) GPUStack network mode (standalone, master, or worker)"
    echo "                            standalone: All ports bound to localhost (default)"
    echo "                            master: All ports exposed for external worker connections"
    echo "                            worker: Connect to a remote GPUStack master server"
    echo "  --gpustack-server-url URL (llm-legacy) GPUStack master URL (required for --gpustack-mode worker)"
    echo "  --gpustack-token TOKEN    (llm-legacy) GPUStack master token (required for --gpustack-mode worker)"
    echo "  --google-client-id ID     Google OAuth Client ID (required for google scenario)"
    echo "  --google-client-secret S  Google OAuth Client Secret (required for google scenario)"
    echo "  --google-domain LIST      Google Workspace domain(s) allowed to sign in, comma separated (google scenario)"
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
    echo "  --skip-host-hardening     Do not run scripts/harden-host.sh (ufw, fail2ban, sysctl,"
    echo "                            auditd, Docker no-new-privileges) during the install"
    echo "  --store-pat               Fleet box: this box may store the Gitea access token"
    echo "                            (PAT). Writes RAZZFAZZ_STORE_PAT=true to .env; init then"
    echo "                            offers to store the PAT. Only a fleet box runs the"
    echo "                            internal channel. Without it, the box is a rental box"
    echo "                            (the default): it runs the public channel (upgrades from"
    echo "                            the public mirror, or an offline package with rzfz"
    echo "                            upgrade --package), no PAT is stored, no git credential"
    echo "                            helper is set and no credential is mounted into the"
    echo "                            Settings portal."
    echo "  --rental                  Writes RAZZFAZZ_STORE_PAT=false: turns a fleet box back"
    echo "                            into a rental box. Same as the default on a new box."
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
    echo "     --profiles chat,dify,llm-manager,llm-registry,llm-worker-agent,monitor --hardware cpu --skip-interactive"
    echo ""
    echo "  # Quick setup with defaults"
    echo "  $0 --domain test.local --skip-interactive"
    echo ""
    echo "  # Package presets (recommended for most users)"
    echo "  $0 --package single-box --domain myai.local --password 'MyPassword1!'"
    echo "  $0 --package master-cpu --domain myai.com --password 'MyPassword1!'"
    echo "  $0 --package testvm-cpu --domain myai.local --password 'MyPassword1!'"
    echo ""
    echo "  # LEGACY: a GPUStack worker of a remote GPUStack master (an LLM Manager node uses rzfz node-init)"
    echo "  $0 --package worker-box --domain worker.local --password 'MyPassword1!' \\"
    echo "     --gpustack-server-url http://master:9090 --gpustack-token TOKEN"
    echo ""
    exit 0
}

# The previous install's live state: its volumes, or its running containers.
# Listed first, then matched: no SIGPIPE under pipefail. A volume list docker
# cannot give counts as volumes present (the data-loss guard runs, fails closed).
_previous_install_volumes() {
    local v
    v=$(docker volume ls --format '{{.Name}}' 2>/dev/null) || return 0
    printf '%s\n' "$v" | grep -q "gpustack-data\|authentik\|postgres"
}
_previous_install_containers() {
    local c
    c=$(docker compose ps --format '{{.Name}}' 2>/dev/null) || c=""
    printf '%s\n' "$c" | grep -q "authentik\|postgres\|gpustack\|caddy"
}

# `--force` re-mints EVERY secret (regenerate_all_secrets). The data-bound ones
# (core/config/profiles.yaml `data_bound_secrets:`) encrypt data in the previous
# install's volumes; a new value makes that data unreadable for good. Asks
# razzfazz_common.data_bound_secrets `remint` (the same declaration the Portal,
# `rzfz upgrade` and `rzfz status` use). rc 0 + one line per database = refuse;
# rc 1 = no declared database holds data. Fails closed: an unreadable
# declaration or a silent postgres refuses.
_reinit_data_bound_refusals() {
    local out rc=0
    out=$(PYTHONPATH="${SCRIPT_DIR}/core/common${PYTHONPATH:+:$PYTHONPATH}" \
          python3 -m razzfazz_common.data_bound_secrets remint \
          --profiles-yaml "${SCRIPT_DIR}/core/config/profiles.yaml" --env .env 2>&1) || rc=$?
    case "$rc" in
        0) return 1 ;;
        3) printf '%s\n' "$out" ;;
        *) echo "the data-bound secret declaration cannot be read (exit ${rc}), so a re-mint over encrypted data cannot be ruled out"
           [ -n "$out" ] && printf '%s\n' "$out" | tail -n 3 ;;
    esac
    return 0
}

# The refusal of a re-mint over encrypted data ($1 = what was asked, $2 = the
# refusal lines, $3 = the keep-the-secrets way out).
_print_remint_refusal() {
    print_error "Refusing $1 over a previous install's volumes: it would generate new secrets over data the current ones encrypt."
    printf '%s\n' "$2" | sed 's/^/    /'
    print_info  "  Keep the secrets (the usual re-init): $3"
    print_info  "  or wipe the volumes and start fresh (DATA LOSS; asks you to type DELETE ALL DATA): rzfz init --factory-default …"
}

# #2611: postgres writes its role passwords only when its data volume is
# created (core/init-db.sh runs once, from docker-entrypoint-initdb.d). A
# re-init that mints new ones over an existing volume changes .env and nothing
# else, and every service then fails to log in to its database. So while the
# volume exists, the role passwords are kept; if this .env does not hold them,
# the init refuses instead of minting.
#
# The postgres data volume, `<project>_postgres-data` (the project is
# compose.yml's `name:` unless COMPOSE_PROJECT_NAME overrides it): prints its
# name; 0 = it exists, 1 = it does not, 2 = docker cannot list volumes
# (treated as existing: fail closed).
_postgres_data_volume() {
    local v project="${COMPOSE_PROJECT_NAME:-}"
    [ -n "$project" ] || project=$(read_env_value ".env" COMPOSE_PROJECT_NAME 2>/dev/null) || project=""
    [ -n "$project" ] || project=razzfazz-stack
    v=$(docker volume ls --format '{{.Name}}' 2>/dev/null) || return 2
    printf '%s\n' "$v" | grep -qxF "${project}_postgres-data" || return 1
    printf '%s\n' "${project}_postgres-data"
}

# The .env keys that carry a postgres role's password.
_db_role_password_keys() {
    cat .env config/.env.example 2>/dev/null \
        | grep -oE '^(POSTGRES_PASSWORD|POSTGRES_EXPORTER_PASSWORD|[A-Z0-9_]+_DB_PASSWORD)=' \
        | tr -d '=' | sort -u
}

# Decides INIT_KEEP_DB_ROLE_PASSWORDS before regenerate_all_secrets runs; exits
# (writing nothing more) when the volume exists and .env cannot hold its
# passwords: POSTGRES_PASSWORD empty, or the template's value.
INIT_KEEP_DB_ROLE_PASSWORDS=false
decide_db_role_passwords() {
    local vol vrc=0 pw tpl_pw pg_user
    vol=$(_postgres_data_volume) || vrc=$?
    if [ "$vrc" -eq 1 ]; then
        INIT_KEEP_DB_ROLE_PASSWORDS=false
        return 0
    fi
    [ "$vrc" -eq 2 ] && vol="(docker could not list the volumes)"
    pw=$(read_env_value ".env" POSTGRES_PASSWORD 2>/dev/null) || pw=""
    tpl_pw=$(read_env_value "config/.env.example" POSTGRES_PASSWORD 2>/dev/null) || tpl_pw=""
    if [ -z "$pw" ] || [ "$pw" = "$tpl_pw" ]; then
        pg_user=$(read_env_value ".env" POSTGRES_USER 2>/dev/null) || pg_user=""
        pg_user="${pg_user:-docker}"
        print_error "Refusing to generate new database passwords: the postgres data volume ${vol} already exists, and its roles keep the passwords they were created with. This .env does not hold them (POSTGRES_PASSWORD is empty or the template's), so new ones would lock every service out of its database."
        print_info  "  Put the previous install's .env back (snapshots: ./scripts/env-snapshot.sh list), then re-run this command"
        print_info  "  or, if they are lost, keep the data and set new ones by hand:"
        print_info  "    1. start postgres alone: docker compose up -d postgres"
        print_info  "    2. set the superuser's password (psql asks for it twice): docker exec -it postgres psql -U ${pg_user} -d postgres -c '\\password ${pg_user}'"
        print_info  "    3. write that password into .env as POSTGRES_PASSWORD, then re-run this command (it keeps it)"
        print_info  "    4. give every service role a new password: rzfz setup --rotate-db-password all"
        print_info  "  or wipe the volumes and start fresh (DATA LOSS; asks you to type DELETE ALL DATA): rzfz init --factory-default …"
        exit 1
    fi
    INIT_KEEP_DB_ROLE_PASSWORDS=true
    print_info "The postgres data volume ${vol} exists: its database role passwords are kept ($(_db_role_password_keys | wc -l) keys); every other secret is generated anew."
}

# _mint_db_role_password KEY GENERATOR [ARGS…] — regenerate_all_secrets' only
# way to write a role password. Keep mode mints an EMPTY one, like upgrade's
# _gen_if_empty: no role was created with it (init-db.sh skips a role without
# a password), and the module's first start needs a value.
_mint_db_role_password() {
    local key="$1" cur; shift
    if [ "$INIT_KEEP_DB_ROLE_PASSWORDS" = true ]; then
        cur=$(read_env_value ".env" "$key" 2>/dev/null) || cur=""
        if [ -n "$cur" ]; then
            print_substep "${key}: kept (the postgres volume holds the role with it)"
            return 0
        fi
    fi
    update_env_value ".env" "$key" "$("$@")"
}

# `--force` over a previous install's VOLUMES keeps their data (only
# --factory-default wipes it). Volumes of another MAIN_DOMAIN made a box that
# only LOOKED installed (the reconcilers exited 2 against them); on the same
# domain, a secret re-mint still destroys the data the data-bound keys encrypt.
# Runs BEFORE setup_env_files, while .env is still the previous install's, and
# writes nothing. 0 = proceed; 1 = refused (the ways out are printed).
refuse_reinit_over_foreign_volumes() {
    local old_domain="" new_domain pending="" pw="" tpl_pw="" why="" refusals
    # A GPU worker (worker-box preset) holds no stack data and no data-bound
    # secret; its preset forces the re-init on purpose.
    [ "${CONFIG_PACKAGE:-}" = "worker-box" ] && return 0
    _previous_install_volumes || _previous_install_containers || return 0
    if [ -f .env ]; then
        old_domain=$(read_env_value ".env" MAIN_DOMAIN 2>/dev/null) || old_domain=""
        pw=$(read_env_value ".env" POSTGRES_PASSWORD 2>/dev/null) || pw=""
    fi
    tpl_pw=$(read_env_value "config/.env.example" POSTGRES_PASSWORD 2>/dev/null) || tpl_pw=""
    [ -f .redomain-pending ] && { pending=$(head -n 1 .redomain-pending | tr -d '[:space:]') || pending=""; }
    new_domain="${CONFIG_DOMAIN:-}"

    if [ ! -f .env ] || [ -z "$pw" ] || [ "$pw" = "$tpl_pw" ] || [ -z "$old_domain" ]; then
        print_error "Refusing --force over a previous install's volumes: cannot verify which domain and secrets they belong to — the previous install's .env is missing, has no MAIN_DOMAIN, or its secrets were never generated."
        print_info  "  Put the previous install's .env back (snapshots: ./scripts/env-snapshot.sh list), then re-run this command"
        print_info  "  or wipe the volumes and start fresh (DATA LOSS; asks you to type DELETE ALL DATA): rzfz init --factory-default …"
        return 1
    fi
    if [ -n "$pending" ] && [ "$pending" != "$old_domain" ]; then
        print_error "Refusing --force over a previous install's volumes: cannot verify their domain — the move from ${pending} to ${old_domain} is not finished (.redomain-pending)."
        print_info  "  Finish the move first: rzfz setup --set-domain ${old_domain}"
        print_info  "  or wipe the volumes and start fresh (DATA LOSS; asks you to type DELETE ALL DATA): rzfz init --factory-default …"
        return 1
    fi
    if [ -z "$new_domain" ]; then
        why="a re-init over existing volumes needs --domain (a run without it would keep ${old_domain}; a forced re-init names its domain)"
    elif [ "$old_domain" != "$new_domain" ]; then
        why="they belong to ${old_domain}; this init asks for ${new_domain}"
    elif [ "${REGENERATE_SECRETS:-true}" = true ] && refusals=$(_reinit_data_bound_refusals); then
        _print_remint_refusal "--force" "$refusals" "rzfz init --force --no-secrets …"
        return 1
    else
        return 0
    fi
    print_error "Refusing --force over a previous install's volumes: ${why}."
    print_info  "  Re-init on their domain, keeping the secrets: rzfz init --force --no-secrets --domain ${old_domain} …"
    print_info  "  (then move the box, if you want: rzfz setup --set-domain <new domain>)"
    print_info  "  or wipe the volumes and start fresh (DATA LOSS; asks you to type DELETE ALL DATA): rzfz init --factory-default …"
    return 1
}

# ==============================================================================
# Check for Existing Installation
# ==============================================================================
check_existing_installation() {
    local is_existing=false
    local indicators=()
    local has_live_state=false

    # Check for Docker volumes that indicate a running/previous installation
    if _previous_install_volumes; then
        is_existing=true
        has_live_state=true
        indicators+=("Docker volumes with data found")
    fi

    # Check for running containers
    if _previous_install_containers; then
        is_existing=true
        has_live_state=true
        indicators+=("Running containers detected")
    fi
    # #2644: a reconfigure over this state carries domain-keyed rows over.
    if [ "$has_live_state" = true ]; then
        INIT_HAD_LIVE_STATE=true
    fi

    # Check if .env has non-default secrets. BUG-1 (2026-05-16): this signal
    # alone is not enough to fire the guard — `docker compose down -v
    # --remove-orphans` wipes volumes + containers but leaves .env on disk,
    # so an operator who just wiped the box would be blocked from running
    # init.sh again without --force. Only treat secrets-in-.env as
    # "existing install" if there is also live state to protect.
    if [ -f ".env" ]; then
        local current_pg_pw
        # `|| true` is load-bearing: this file runs under `set -eo pipefail`, and a
        # BARE `x=$(grep … | cut …)` inherits the pipeline's status. When the key is
        # ABSENT grep exits 1, pipefail propagates it, and the run dies HERE — before
        # the very next line, which already handles the empty case. (#200; same shape
        # as the #755 regression fixed in #793.)
        current_pg_pw=$(grep -E "^POSTGRES_PASSWORD=" .env 2>/dev/null | cut -d'=' -f2-) || true
        local default_pg_pw
        # `|| true` is load-bearing: this file runs under `set -eo pipefail`, and a
        # BARE `x=$(grep … | cut …)` inherits the pipeline's status. When the key is
        # ABSENT grep exits 1, pipefail propagates it, and the run dies HERE — before
        # the very next line, which already handles the empty case. (#200; same shape
        # as the #755 regression fixed in #793.)
        default_pg_pw=$(grep -E "^POSTGRES_PASSWORD=" config/.env.example 2>/dev/null | cut -d'=' -f2-) || true
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
                    chmod 600 .env   # #2543: secrets before the first write, whatever the umask
                fi
                if [ -f "config/.env.dify.example" ]; then
                    cp config/.env.dify.example .env.dify
                    chmod 600 .env.dify   # #2543: secrets before the first write, whatever the umask
                elif [ -f "modules/dify/.env.example" ]; then
                    cp modules/dify/.env.example .env.dify
                    chmod 600 .env.dify   # #2543: secrets before the first write, whatever the umask
                fi
                print_substep "Reset .env + .env.dify from templates"
                # #2255: the template ships RAZZFAZZ_CHANNEL=public and the
                # re-seed above just overwrote the value detect_release_channel
                # decided in setup_env_files() — on a flattened fleet box the
                # install log said "internal" and .env said "public", and the
                # next `rzfz upgrade` refused in one second (#602 pre-flight).
                # Decide again on the fresh file; the decision is idempotent,
                # and so is the PAT onboarding that always follows it (it
                # returns at once when the box is already onboarded).
                detect_release_channel
                enforce_rental_public_channel
                apply_pat_mode
                offer_internal_pat_onboarding
            fi
        fi
    fi

    EXISTING_INSTALL="$is_existing"

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
        echo -e "  ${RED}•${NC} Regenerate the secrets (API keys, tokens, signing keys)"
        echo -e "  ${YELLOW}•${NC} Keep the database role passwords while the postgres volume exists (its roles are bound to them)"
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
                # The same data loss as `--force`: refused before the prompt.
                local refusals
                if refusals=$(_reinit_data_bound_refusals); then
                    _print_remint_refusal "a full re-init" "$refusals" "option 2 (Reconfigure only), or rzfz init --force --no-secrets …"
                    exit 1
                fi
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
                echo "  • Configuration Portal: https://settings.\$(your-domain)"
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
# #2620: create + delete a throwaway venv (the real workload of the python3-venv
# check). Runs in a subshell with its own cleanup trap: the directory is a
# mktemp -d under TMPDIR, removed on exit, Ctrl-C and SIGTERM alike. Returns the
# venv command's status, or 125 when the temporary directory cannot be made.
_python_venv_probe() {
    (
        _d="$(mktemp -d "${TMPDIR:-/tmp}/razzfazz_init_venv_probe.XXXXXX")" || exit 125
        trap 'rm -rf "$_d"' EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM
        python3 -m venv "$_d/venv" > /dev/null 2>&1
    )
}

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

    # #855 rev-C GATE — Compose/Engine feature floor for `volume: { subpath: }`.
    # "v2 exists" (above) is not enough: 2.24.6 has a v2 CLI and still refuses
    # to parse this repo's compose tree at all. Ordered AFTER the daemon check
    # because the Engine API version can only be read from a live daemon.
    # Aggregated into missing_deps like every other prerequisite here; the
    # actionable detail is printed by the helper itself.
    if ! razzfazz_check_compose_floor; then
        missing_deps+=("docker-compose-plugin >= ${RAZZFAZZ_MIN_COMPOSE_VERSION} (engine API >= ${RAZZFAZZ_MIN_ENGINE_API})")
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
        # #2620: the throwaway venv lives in TMPDIR and is removed by the probe's
        # own trap, so a killed run leaves nothing behind (8-24 MB each on a
        # small shared /tmp). rc 125 = the probe could not even start.
        _venv_probe_rc=0
        _python_venv_probe || _venv_probe_rc=$?
        if [ "$_venv_probe_rc" -eq 125 ]; then
            print_warning "Could not create a temporary directory in ${TMPDIR:-/tmp} — python3-venv NOT verified."
        elif [ "$_venv_probe_rc" -ne 0 ]; then
            VENV_PKG="python${PYTHON_VERSION}-venv"
            if command -v apt-get &> /dev/null; then
                print_warning "$VENV_PKG missing — installing via apt-get (may prompt for sudo)..."
                # razzfazz_sudo prompts on a terminal and fails fast without one.
                if razzfazz_sudo apt-get install -y "$VENV_PKG" python3-venv >/dev/null 2>&1; then
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
# exact match against $RAZZFAZZ_PUBLIC_REMOTE. Runs on $SCRIPT_DIR/.env.
# #2384 precedence: --channel > origin > preset default > template.
# #2384: "host/path" of a remote URL, lowercased, for an exact identity match.
# Handles https, ssh://, scp-style (git@host:org/repo), userinfo, port, a
# trailing slash and a trailing .git. A host is compared whole, so a look-alike
# such as evilrazzfazz.ai never matches. Prints "" for anything unparsable.
_origin_identity() {
    local u="$1" hp path
    case "$u" in
        *://*) u="${u#*://}"; hp="${u%%/*}"; path="${u#*/}"; [ "$hp" != "$u" ] || path="" ;;
        *:*)   hp="${u%%:*}"; path="${u#*:}" ;;
        *)     return 0 ;;
    esac
    hp="${hp##*@}"; hp="${hp%%:*}"
    path="${path#/}"; path="${path%/}"; path="${path%.git}"
    [ -n "$hp" ] && [ -n "$path" ] || return 0
    printf '%s/%s\n' "$hp" "$path" | tr '[:upper:]' '[:lower:]'
}

# #3265: is the box being initialised a fleet box? --store-pat / --rental decide
# for this run (apply_pat_mode writes them later); otherwise .env and the disk
# (razzfazz_fleet_box, the same answer upgrade and status give).
_init_fleet_box() {
    [ "$RAZZFAZZ_FLEET_MAY_USE_INTERNAL_CHANNEL" = 1 ] || return 1   # the #3265 switch
    case "${CONFIG_PAT_MODE:-}" in
        true) return 0 ;;
        false) return 1 ;;
    esac
    razzfazz_fleet_box "."
}

# #3265 (operator 2026-11-16): rental boxes run the public channel only. Runs after
# detect_release_channel: only a fleet box (--store-pat) keeps the internal channel;
# an explicit --channel internal on a rental box is refused, anything else is moved
# to public.
enforce_rental_public_channel() {
    [ -f ".env" ] || return 0
    if [ "$(read_env_value ".env" "RAZZFAZZ_CHANNEL" 2>/dev/null || true)" != public ] && ! _init_fleet_box; then
        if [ "${CONFIG_CHANNEL:-}" = internal ]; then
            if [ "$RAZZFAZZ_FLEET_MAY_USE_INTERNAL_CHANNEL" = 1 ]; then
                print_error "--channel internal is for our fleet boxes only; this is a rental box."
                print_info "A fleet box: rzfz init --store-pat --channel internal. A rental box runs the public channel (leave --channel out)."
            else
                print_error "--channel internal is not available: every box runs the public channel (leave --channel out)."
            fi
            exit 1
        fi
        update_env_value ".env" "RAZZFAZZ_CHANNEL" "public"
        print_substep "RAZZFAZZ_CHANNEL=public: a rental box runs the public channel only (a fleet box: rzfz init --store-pat)"
    fi
    return 0
}

detect_release_channel() {
    [ -f ".env" ] || return 0

    # #602/#2384: an explicit --channel beats everything.
    if [ -n "${CONFIG_CHANNEL:-}" ]; then
        update_env_value ".env" "RAZZFAZZ_CHANNEL" "$CONFIG_CHANNEL"
        print_substep "RAZZFAZZ_CHANNEL=$CONFIG_CHANNEL (--channel)"
        return 0
    fi

    local origin="" by_origin="" id="" pub_id=""
    if [ -d ".git" ]; then
        # #1738: RAW — the channel is decided by WHICH REPO this box is from,
        # and an insteadOf alias would make a fleet box look non-standard.
        origin=$(razzfazz_origin_url "$SCRIPT_DIR")
    fi
    if [ -n "$origin" ]; then
        id=$(_origin_identity "$origin")
        pub_id=$(_origin_identity "$RAZZFAZZ_PUBLIC_REMOTE")
        # #602: bidirectional — the public mirror (GitHub; legacy Codeberg, or
        # the configured RAZZFAZZ_PUBLIC_REMOTE) is public, the SEQIS Gitea is
        # a fleet box; any other origin decides nothing.
        case "$id" in
            "") ;;
            github.com/rzfz-ai/rzfz-ai-service-stack \
                | codeberg.org/rzfz-ai/rzfz-ai-service-stack) by_origin=public ;;
            git.razzfazz.ai/razzfazz.ai/razzfazz-ai-service-stack) by_origin=internal ;;
            *) [ -n "$pub_id" ] && [ "$id" = "$pub_id" ] && by_origin=public ;;
        esac
    fi

    # #2384: the origin outranks a preset's DEFAULT — a Gitea clone is a fleet box.
    if [ -n "$by_origin" ]; then
        update_env_value ".env" "RAZZFAZZ_CHANNEL" "$by_origin"
        if [ -n "${PRESET_CHANNEL:-}" ] && [ "$PRESET_CHANNEL" != "$by_origin" ]; then
            print_substep "RAZZFAZZ_CHANNEL=${by_origin}: the origin outranks the preset's default '${PRESET_CHANNEL}' (#2384)"
        else
            print_substep "origin → RAZZFAZZ_CHANNEL=${by_origin}"
        fi
        return 0
    fi
    # No decisive origin (none, no git, or a non-standard one): the preset decides.
    if [ -n "${PRESET_CHANNEL:-}" ]; then
        update_env_value ".env" "RAZZFAZZ_CHANNEL" "$PRESET_CHANNEL"
        print_substep "RAZZFAZZ_CHANNEL=${PRESET_CHANNEL} (preset default; the origin does not decide)"
    fi
    return 0
}

# ==============================================================================
# #2690: the Gitea PAT is stored only on boxes marked for it
# ==============================================================================
# Rental is the default: RAZZFAZZ_STORE_PAT=true (--store-pat) marks one of our
# fleet boxes, which may store the PAT; anything else is a rental box, which
# never does. The key is written here so every later run reads the same
# answer: init (PAT offered or not, helper, portal mount), `rzfz upgrade`
# (removes stored tokens on a rental box) and `rzfz status`. --rental writes
# false. Without a flag, a marker already in .env stays and a box without one
# is marked false: init never adopts a box as a fleet box from what it finds
# on disk. That one-time decision for boxes from before the key is the
# upgrade's (razzfazz_settle_pat_mode, cli/upgrade.sh).
apply_pat_mode() {
    [ -f ".env" ] || return 0
    case "${CONFIG_PAT_MODE:-}" in
        true)
            update_env_value ".env" "RAZZFAZZ_STORE_PAT" "true"
            print_substep "RAZZFAZZ_STORE_PAT=true (--store-pat): this fleet box may store the Gitea access token"
            ;;
        false)
            update_env_value ".env" "RAZZFAZZ_STORE_PAT" "false"
            print_substep "RAZZFAZZ_STORE_PAT=false (--rental): no Gitea access token is stored on this box"
            ;;
        *)
            if [ -z "$(razzfazz_store_pat_marker ".env")" ]; then
                update_env_value ".env" "RAZZFAZZ_STORE_PAT" "false"
                print_substep "RAZZFAZZ_STORE_PAT=false: a rental box, no Gitea access token is stored (a fleet box: rzfz init --store-pat)"
            fi
            ;;
    esac
}

# ==============================================================================
# #602: PAT onboarding for internal-channel boxes
# ==============================================================================
# A fresh fleet box has no PAT, so its first `rzfz upgrade` used to abort in
# the pre-flight (seen live on 0.79). Offer the token at init time, with a
# skip; non-interactive runs get a loud pointer instead of a hidden landmine.
offer_internal_pat_onboarding() {
    [ -f ".env" ] || return 0
    # #2690: only a box marked RAZZFAZZ_STORE_PAT=true stores the PAT; a
    # rental box (the default) is asked nothing here.
    if razzfazz_rental_box ".env"; then
        print_info "Rental box: no Gitea access token is stored. Upgrades ask for it at a terminal (kept for that run only), or use an offline package. A fleet box that should keep it: rzfz init --store-pat."
        return 0
    fi
    local channel
    channel=$(read_env_value ".env" "RAZZFAZZ_CHANNEL" 2>/dev/null || echo "")
    [ "$channel" = "internal" ] || return 0
    if [ -f "$HOME/.git-credentials" ] && grep -q "git.razzfazz.ai" "$HOME/.git-credentials" 2>/dev/null; then
        return 0   # already onboarded
    fi
    if [ "${SKIP_INTERACTIVE:-false}" = "true" ] || [ ! -t 0 ]; then
        print_warning "internal channel without a Gitea PAT — the first 'rzfz upgrade' will abort in the pre-flight."
        print_info "Onboard later: git config --global credential.helper store; then store https://<user>:<PAT>@git.razzfazz.ai in ~/.git-credentials (chmod 600)."
        return 0
    fi
    echo ""
    print_step "Gitea access for upgrades (internal channel)"
    print_info "This fleet box pulls upgrades from git.razzfazz.ai and needs a personal access token (PAT)."
    # #635 review: -s — the PAT must not echo (screen sharing / shoulder surfing)
    read -rs -p "  Enter PAT now (leave empty to skip): " _pat; echo ""
    if [ -z "$_pat" ]; then
        print_info "Skipped — onboard later before the first 'rzfz upgrade'."
        return 0
    fi
    read -r -p "  Gitea username for the token: " _pat_user
    git config --global credential.helper store
    # #2543: this script runs under umask 022 — the credentials file and its
    # staging copy are created under umask 077, so the PAT is never readable
    # to others, not even between two commands.
    (umask 077; touch "$HOME/.git-credentials")
    chmod 600 "$HOME/.git-credentials"
    # replace any stale git.razzfazz.ai line, then append the fresh one
    # a stale .tmp keeps its OWN (maybe wider) mode through `>`; remove it first
    rm -f "$HOME/.git-credentials.tmp"
    (umask 077; grep -v "git.razzfazz.ai" "$HOME/.git-credentials" > "$HOME/.git-credentials.tmp" 2>/dev/null) || true
    mv "$HOME/.git-credentials.tmp" "$HOME/.git-credentials"
    printf 'https://%s:%s@git.razzfazz.ai\n' "$_pat_user" "$_pat" >> "$HOME/.git-credentials"
    chmod 600 "$HOME/.git-credentials"
    print_success "PAT stored (~/.git-credentials, 600) — upgrades are ready."
}

# ==============================================================================
# Git credential wiring for updates (#3, rc5; #2690 rental boxes)
# ==============================================================================
# Runs once .env exists, in the stack directory.
wire_git_credentials_for_updates() {
    # #2690: a rental box gets neither the portal's credential mount nor a
    # `store` helper (a helper would write any PAT typed at a later git prompt
    # to ~/.git-credentials). A credential left from before is named, not kept
    # quietly.
    if razzfazz_rental_box ".env"; then
        if [ -n "$(read_env_value ".env" "STACK_GIT_CREDENTIALS_PATH")" ]; then
            update_env_value ".env" "STACK_GIT_CREDENTIALS_PATH" ""
        fi
        print_substep "Rental box: no git credential helper and no credential mount (the PAT is entered per use)"
        if [ -n "$(razzfazz_git_credential_scan . list)" ]; then
            print_warning "A Gitea credential is still stored on this rental box. Remove it: rzfz forget-pat"
        fi
        return 0
    fi
    # #2804: no credentials file is mounted into the web-facing Settings
    # portal any more; its update check uses the read-only STACK_UPDATE_TOKEN.
    # A path recorded by an earlier init is cleared.
    if [ -n "$(read_env_value ".env" "STACK_GIT_CREDENTIALS_PATH")" ]; then
        update_env_value ".env" "STACK_GIT_CREDENTIALS_PATH" ""
    fi
    if [ -z "$(read_env_value ".env" "STACK_UPDATE_TOKEN")" ]; then
        print_info "Update check: set a read-only Gitea token (scope read:repository) as STACK_UPDATE_TOKEN in .env (chmod 600), then: docker compose up -d razzfazz-config"
    fi

    # #3: seed the canonical `store` credential helper for an https origin so the
    # operator's PAT (entered now or later) persists in ~/.git-credentials and the
    # upgrade-path `git fetch` authenticates without prompting. Idempotent: only
    # sets it when this is a git checkout with an https origin and no helper is
    # already configured. The PAT itself is still operator-supplied — this only
    # wires up where git will look for it.
    if [ -d .git ]; then
        local _init_origin
        _init_origin=$(git remote get-url origin 2>/dev/null || echo "")
        case "$_init_origin" in
            https://*)
                if [ -z "$(git config --get credential.helper 2>/dev/null || echo "")" ]; then
                    git config credential.helper store && \
                        print_substep "Configured git credential.helper=store for upgrade fetches (#3)"
                fi
                ;;
        esac
    fi
    return 0
}

# ==============================================================================
# Setup Environment Files
# ==============================================================================
setup_env_files() {
    print_step "Setting up environment files..."

    local env_created=false
    if [ ! -f ".env" ]; then
        if [ -f "config/.env.example" ]; then
            cp config/.env.example .env
            chmod 600 .env   # #2543: secrets before the first write, whatever the umask
            env_created=true
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
    # 2026-07; legacy Codeberg origins still recognised). #602: `.env.example`
    # ships `public` now; detection is bidirectional (Gitea origin → internal)
    # an explicit --channel wins over detection, and detection over a preset default (#2384).
    detect_release_channel
    # #3265: same rule as apply_pat_mode below — an existing .env is only written
    # with a flag (a refused re-init leaves it untouched); upgrade and status catch
    # a rental box left on the internal channel.
    if [ -n "${CONFIG_PAT_MODE:-}${CONFIG_CHANNEL:-}" ] || [ "$env_created" = true ]; then
        enforce_rental_public_channel
    fi
    # #2690/#2115: an existing .env is not written here without a flag: this
    # runs before the existing-installation menu, and a refused re-init must
    # leave .env untouched. Its default marker is written at the end of init
    # (before wire_git_credentials_for_updates); until then an empty marker
    # reads as rental, the same answer.
    if [ -n "${CONFIG_PAT_MODE:-}" ] || [ "$env_created" = true ]; then
        apply_pat_mode
    fi
    offer_internal_pat_onboarding

    if [ ! -f ".env.dify" ]; then
        if [ -f "config/.env.dify.example" ]; then
            cp config/.env.dify.example .env.dify
            chmod 600 .env.dify   # #2543: secrets before the first write, whatever the umask
            print_substep "Created .env.dify from template"
        elif [ -f "modules/dify/.env.example" ]; then
            cp modules/dify/.env.example .env.dify
            chmod 600 .env.dify   # #2543: secrets before the first write, whatever the umask
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
    # Reject known shipped-default secrets that must never survive into a
    # production .env. Stored as sha256 HASHES, not plaintext, so this public
    # denylist does not itself publish the values it rejects — one legacy shipped
    # default is also reused as a fleet operations credential, and GitHub secret
    # scanning (correctly) flagged the old plaintext array. To add a value:
    #     printf '%s' '<value>' | sha256sum
    local -A KNOWN_DEFAULT_HASHES=(
        [718d33b350ad619ad1d21bd3c3ae98e04e6bc149cc9940c46d3d0d3eb6ecd637]=1
        [32211caa22d2b9e5c838facacee8ccd61c01d6e196c2d8921e4e1f4c7b6cb4ca]=1
        [3bc74b4d766666b31d3aa9980047e2364383e277975d7706dddd8c12d400f793]=1
        [a4b744973756871c1d844ccf4a34d21170bf2e6a54ee13af60c4395a632cdca2]=1
        [50978d400e049509cf62f118f3b10a7dd31c4948e5669e9bbac1c1ae44b37c24]=1
        [a8ae6e6ee929abea3afcfc5258c8ccd6f85273e0d4626d26c7279f3250f77c8e]=1
        [7c5928fbbea28e6106031415e3d1c866cbaab3c98f0ccc379bb7465a5e2e241f]=1
        [9392f21d2de2a45c2b52feaa0b60fdead2eecd59eb06959d6cdf90e3d71bd385]=1
        [2deb4c1f6ade4d0f663f09c490f7795936350ad624aaa8ed0749e88051da8d3c]=1
        [cd210f14d1315f4d61fd9047d4ab39d6ff59ddfa9dade2593731d1e6000bbc99]=1
        [d794483dce2ecc0c8395ef231fce3292a57d0e204264ba40e6bfe92935312380]=1
        [7e80be71c002ae60ffbe401fc524998d077b5eab16480a52bd5038685dc88d21]=1
        [d33d743a9d1e138206cd396590423e9696745caa0d7a9d6e1419f978c0b1652d]=1
    )

    local found_defaults=false
    # Hash every value assigned in .env and test membership. We read (never
    # source) so operator values with spaces/metacharacters stay safe.
    local _key _val _h
    while IFS= read -r _line; do
        # Split on the FIRST '=' only (#2663): `IFS='=' read -r k v` drops one
        # trailing '=' from the last field, truncating base64 secrets.
        _key="${_line%%=*}"; _val="${_line#*=}"
        [ -z "$_val" ] && continue
        _val="${_val%\"}"; _val="${_val#\"}"; _val="${_val%\'}"; _val="${_val#\'}"
        _h="$(printf '%s' "$_val" | sha256sum | cut -d' ' -f1)"
        if [ -n "${KNOWN_DEFAULT_HASHES[$_h]:-}" ]; then
            if [ "$found_defaults" = false ]; then
                print_warning "Known default secrets detected in .env — they will be regenerated."
                found_defaults=true
            fi
        fi
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env 2>/dev/null)
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
    _mint_db_role_password "POSTGRES_PASSWORD" generate_password 24

    print_substep "Generating VALKEY_PASSWORD..."
    update_env_value ".env" "VALKEY_PASSWORD" "$(generate_password 24)"
    # #2977: a new box's password was never in a log line; it starts at the current
    # rotation generation, so no upgrade rotates it needlessly.
    update_env_value ".env" "VALKEY_PASSWORD_ROTATION" "$VALKEY_ROTATION_GENERATION"

    print_substep "Generating GPUSTACK_SECRET_KEY..."
    update_env_value ".env" "GPUSTACK_SECRET_KEY" "$(generate_secret 32)"

    print_substep "Generating PIPELINES_API_KEY..."
    update_env_value ".env" "PIPELINES_API_KEY" "$(generate_secret 32)"

    print_substep "Generating KOMODO_PASSKEY..."
    update_env_value ".env" "KOMODO_PASSKEY" "$(generate_password 24)"
    # #3309: the password of Komodo's own PostgreSQL cluster (never the stack
    # superuser's). Kept like a role password when the volumes are kept: the
    # postgres-komodo volume holds the role with it. Alphanumeric: it lands in
    # postgres:// and mongodb:// URLs. Not a main-cluster role, so it is not in
    # rotate-db-password.sh (that script alters roles of the main cluster).
    if [ "$INIT_KEEP_DB_ROLE_PASSWORDS" = true ] && [ -n "$(read_env_value ".env" "KOMODO_PG_PASSWORD" 2>/dev/null)" ]; then
        print_substep "KOMODO_PG_PASSWORD: kept (the postgres-komodo volume holds the role with it)"
    else
        print_substep "Generating KOMODO_PG_PASSWORD..."
        update_env_value ".env" "KOMODO_PG_PASSWORD" "$(generate_password 24)"
    fi

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
    # LLM Manager UI (#254 Phase-3) forward-auth provider secret.
    print_substep "Generating LLM_MANAGER_UI_CLIENT_SECRET..."
    update_env_value ".env" "LLM_MANAGER_UI_CLIENT_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating LLM_HUB_CLIENT_SECRET..."
    update_env_value ".env" "LLM_HUB_CLIENT_SECRET" "$(generate_hex_secret 32)"
    # #3302: the model registry's writer identity (anonymous clients only read).
    print_substep "Generating LLM_REGISTRY_WRITE_PASSWORD..."
    update_env_value ".env" "LLM_REGISTRY_WRITE_PASSWORD" "$(generate_hex_secret 32)"
    # #340: FRESH installs get true per-worker isolation from day one — the
    # shared node key is refused on the wire, every node holds only its own
    # derived key, and rotate-key actually contains a compromised node.
    # UPGRADED boxes keep their existing mode (compose default: allow) until
    # every node is (re-)enrolled with LLM_WORKER_COMMAND_KEY — flipping it for
    # them here would 401 their whole fleet mid-upgrade.
    print_substep "Setting LLM_MANAGER_COMMAND_KEY_MODE=enforce (fresh install)..."
    grep -q '^LLM_MANAGER_COMMAND_KEY_MODE=' ".env" \
        || printf 'LLM_MANAGER_COMMAND_KEY_MODE=\n' >> ".env"
    update_env_value ".env" "LLM_MANAGER_COMMAND_KEY_MODE" "enforce"

    print_substep "Generating COGNEE_CLIENT_SECRET..."
    update_env_value ".env" "COGNEE_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating COGNEE_JWT_SECRET..."
    update_env_value ".env" "COGNEE_JWT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating DOCLING_CLIENT_SECRET..."
    update_env_value ".env" "DOCLING_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating STIRLING_CLIENT_SECRET..."
    update_env_value ".env" "STIRLING_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # (#191) crawl4ai's forward_auth proxy provider (29-crawl4ai.yaml) keys off
    # this — was previously minted nowhere, so the provider applied with an
    # empty client_secret. Same hex32 shape as the other forward_auth modules.
    print_substep "Generating CRAWL4AI_CLIENT_SECRET..."
    update_env_value ".env" "CRAWL4AI_CLIENT_SECRET" "$(generate_hex_secret 32)"

    print_substep "Generating SEARXNG_CLIENT_SECRET..."
    update_env_value ".env" "SEARXNG_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # (#191) The image's startup auth guard (_resolve_auth) REFUSES to start on a
    # non-loopback bind with no credential — and the container must bind 0.0.0.0
    # for Caddy to reach it. Without this the module crash-loops. Caddy also sends
    # it upstream as the Bearer credential, so the browsable UI works at all.
    print_substep "Generating CRAWL4AI_API_TOKEN..."
    update_env_value ".env" "CRAWL4AI_API_TOKEN" "$(generate_hex_secret 32)"

    # Paperclip secrets
    print_substep "Generating PAPERCLIP_DB_PASSWORD..."
    _mint_db_role_password "PAPERCLIP_DB_PASSWORD" generate_hex_secret 16
    print_substep "Generating PAPERCLIP_BETTER_AUTH_SECRET (permanent - never rotated)..."
    update_env_value ".env" "PAPERCLIP_BETTER_AUTH_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating PAPERCLIP_CLIENT_SECRET..."
    update_env_value ".env" "PAPERCLIP_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # Synapse Matrix homeserver secrets
    print_substep "Generating SYNAPSE_DB_PASSWORD..."
    _mint_db_role_password "SYNAPSE_DB_PASSWORD" generate_hex_secret 16
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

    # OpenUEM (#1075) — console JWT signing key. Upstream's .env-example ships a
    # hardcoded demo JWT key and its own docs call replacing it "strongly
    # recommended"; we ship no default at all. 64 hex chars.
    print_substep "Generating OPENUEM_CONSOLE_JWT_KEY..."
    update_env_value ".env" "OPENUEM_CONSOLE_JWT_KEY" "$(generate_hex_secret 32)"

    print_substep "Generating OPENUEM_CLIENT_SECRET..."
    update_env_value ".env" "OPENUEM_CLIENT_SECRET" "$(generate_hex_secret 32)"

    # Wazuh (#855) — SIEM/XDR. Five secrets, ALL mandatory: the module's render
    # one-shots refuse to run (`: "${VAR:?}"` in
    # modules/apps/wazuh/configs/*/render-*.sh) without them, and every
    # long-running wazuh service gates on those one-shots completing. rev-B
    # moved that check OUT of the compose file: `${VAR:?}` there is resolved at
    # parse time, before profile filtering, and the module is included
    # unconditionally — so it bricked `docker compose` on every box that had not
    # enabled the profile. The refusal is unchanged, only its layer is.
    # It matters because the alternative is Wazuh's PUBLISHED demo credentials
    # (admin/SecretPassword, kibanaserver/kibanaserver, wazuh-wui/MyS3cr37…)
    # surviving on a real box. Minted unconditionally, like CLICKHOUSE_PASSWORD.
    # generate_password (not generate_secret) for the three indexer/API
    # passwords: the Wazuh API enforces a password policy and rejects base64
    # padding characters, which generate_secret can emit.
    print_substep "Generating WAZUH_INDEXER_PASSWORD..."
    update_env_value ".env" "WAZUH_INDEXER_PASSWORD" "$(generate_password 32)"
    print_substep "Generating WAZUH_DASHBOARD_PASSWORD..."
    update_env_value ".env" "WAZUH_DASHBOARD_PASSWORD" "$(generate_password 32)"
    print_substep "Generating WAZUH_API_PASSWORD..."
    update_env_value ".env" "WAZUH_API_PASSWORD" "$(generate_password 32)"
    # Agent-enrollment shared secret (authd). WZ-4: without it, anything that
    # can reach port 1515 can enrol itself as a trusted agent.
    print_substep "Generating WAZUH_AUTHD_PASSWORD..."
    update_env_value ".env" "WAZUH_AUTHD_PASSWORD" "$(generate_hex_secret 32)"
    # Two Authentik secrets, not one (rev-B blocker 4): WAZUH_CLIENT_SECRET is
    # the forward-auth ProxyProvider the embedded outpost matches
    # wazuh.<domain> against; WAZUH_OIDC_CLIENT_SECRET is the dashboard's own
    # native OIDC client. Same split as GITEA_CLIENT_SECRET/GITEA_OIDC_*.
    print_substep "Generating WAZUH_CLIENT_SECRET..."
    update_env_value ".env" "WAZUH_CLIENT_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating WAZUH_OIDC_CLIENT_SECRET..."
    update_env_value ".env" "WAZUH_OIDC_CLIENT_SECRET" "$(generate_hex_secret 32)"
    # #2476: OpenBao's two Authentik objects, the same split: the forward-auth
    # ProxyProvider for bao.<domain> and OpenBao's own OIDC sign-in client.
    print_substep "Generating OPENBAO_CLIENT_SECRET..."
    update_env_value ".env" "OPENBAO_CLIENT_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating OPENBAO_OIDC_CLIENT_ID..."
    update_env_value ".env" "OPENBAO_OIDC_CLIENT_ID" "$(generate_hex_secret 16)"
    print_substep "Generating OPENBAO_OIDC_CLIENT_SECRET..."
    update_env_value ".env" "OPENBAO_OIDC_CLIENT_SECRET" "$(generate_hex_secret 32)"

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
    _mint_db_role_password "AUTHENTIK_DB_PASSWORD" generate_password 24
    _mint_db_role_password "OPENWEBUI_DB_PASSWORD" generate_password 24
    _mint_db_role_password "GPUSTACK_DB_PASSWORD" generate_password 24
    _mint_db_role_password "DIFY_DB_PASSWORD" generate_password 24
    _mint_db_role_password "DIFY_PLUGIN_DB_PASSWORD" generate_password 24
    _mint_db_role_password "GITEA_DB_PASSWORD" generate_password 24
    _mint_db_role_password "PAPERLESS_DB_PASSWORD" generate_password 24
    _mint_db_role_password "INFISICAL_DB_PASSWORD" generate_password 24
    _mint_db_role_password "ONYX_DB_PASSWORD" generate_password 24
    # #1075: generate_password (not generate_secret) — this value is
    # interpolated into a postgres:// URL, and base64's / + = would corrupt it.
    _mint_db_role_password "OPENUEM_DB_PASSWORD" generate_password 24

    # Authentik proxy provider secrets for Licenses, Setup, Help UIs (F-038)
    print_substep "Generating LICENSES_CLIENT_SECRET..."
    update_env_value ".env" "LICENSES_CLIENT_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating SETUP_CLIENT_SECRET..."
    update_env_value ".env" "SETUP_CLIENT_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating HELP_CLIENT_SECRET..."
    update_env_value ".env" "HELP_CLIENT_SECRET" "$(generate_hex_secret 32)"
    # #386 (F-P4-12): the Config Portal was the one proxy provider left on a
    # hardcoded literal — and 25-config.yaml ships to the public mirror.
    print_substep "Generating CONFIG_CLIENT_SECRET..."
    update_env_value ".env" "CONFIG_CLIENT_SECRET" "$(generate_hex_secret 32)"

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
    # #2086: the relay secret between mcp-manager's MCP gateway and the
    # docling-egress sidecar (Docling's MCP server). Without it the sidecar
    # refuses to start and the gateway refuses the Docling calls (fail closed).
    print_substep "Generating DOCLING_RELAY_SECRET..."
    update_env_value ".env" "DOCLING_RELAY_SECRET" "$(generate_hex_secret 32)"
    # C1 bypass fix (PR #84 re-review): "came through Caddy" proof secret shared
    # only Caddy↔agent-manager (X-Razzfazz-Proxy-Proof). Without it the manager's
    # per-instance proxy fails closed, so it MUST be minted on every box.
    print_substep "Generating MANAGER_PROXY_SECRET..."
    update_env_value ".env" "MANAGER_PROXY_SECRET" "$(generate_hex_secret 32)"
    print_substep "Generating MCP_MANAGER_DB_PASSWORD..."
    _mint_db_role_password "MCP_MANAGER_DB_PASSWORD" generate_password 24

    # postgres-exporter's DEDICATED pg_monitor role (#253 X5 S3). Minted
    # unconditionally so enabling the observability profile later "just works"
    # — the exporter compose entry has NO ${:-POSTGRES_PASSWORD} superuser
    # fallback on purpose, so an empty value fails the exporter's own
    # connection instead of silently handing a third-party image the cluster
    # superuser.
    print_substep "Generating POSTGRES_EXPORTER_PASSWORD..."
    _mint_db_role_password "POSTGRES_EXPORTER_PASSWORD" generate_password 24

    # #3292 (H-01): the services that used to connect as the cluster superuser
    # get their own roles (core/init-db.sh). Minted unconditionally, like the
    # managers below, so enabling a module later "just works"; the compose files
    # have no superuser fallback. PAPERCLIP_DB_PASSWORD is minted further up.
    print_substep "Generating COGNEE/LIGHTRAG/AGENT_MANAGER/VAULTWARDEN DB role passwords..."
    _mint_db_role_password "COGNEE_DB_PASSWORD" generate_password 24
    _mint_db_role_password "LIGHTRAG_DB_PASSWORD" generate_password 24
    _mint_db_role_password "AGENT_MANAGER_DB_PASSWORD" generate_password 24
    _mint_db_role_password "VAULTWARDEN_DB_PASSWORD" generate_password 24

    # LLM Manager (#254, profile llm-manager). Minted unconditionally
    # (like MCP_MANAGER above) so enabling the profile later "just works":
    #   LLM_MANAGER_DB_PASSWORD — password of the dedicated postgres role
    #     llm_manager_user. The orchestrator compose has NO
    #     ${:-POSTGRES_PASSWORD} superuser fallback (unlike core services), so
    #     this MUST be a real value or the manager can't reach llm_manager_db.
    #   LITELLM_INTERNAL_KEY — the LiteLLM router master key, known only to the
    #     manager (never handed to API-key clients); fail-closed if empty.
    print_substep "Generating LLM_MANAGER_DB_PASSWORD..."
    _mint_db_role_password "LLM_MANAGER_DB_PASSWORD" generate_password 24
    print_substep "Generating LITELLM_INTERNAL_KEY..."
    update_env_value ".env" "LITELLM_INTERNAL_KEY" "$(generate_hex_secret 32)"
    #   LLM_MANAGER_NODE_KEY — shared secret a fleet node presents (Bearer) to
    #     register itself via POST /api/workers (#254 P2-B1). Distinct from the
    #     router master key; fail-closed (registration disabled) if empty. On a
    #     multi-box fleet the operator copies this to each worker box.
    print_substep "Generating LLM_MANAGER_NODE_KEY..."
    update_env_value ".env" "LLM_MANAGER_NODE_KEY" "$(generate_hex_secret 32)"
    #   OLLAMA_PROXY_CLIENT_KEY — the bearer key a caller of the Ollama-protocol
    #     proxy (ollama-proxy:11434) must send (#3301); empty = it refuses all.
    print_substep "Generating OLLAMA_PROXY_CLIENT_KEY..."
    update_env_value ".env" "OLLAMA_PROXY_CLIENT_KEY" "$(generate_hex_secret 32)"

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

        # Sync VALKEY_PASSWORD to REDIS_PASSWORD + CELERY_BROKER_URL for Dify
        # (the one shared step in scripts/lib.sh; a missing key is a no-op).
        local plugin_key inner_key
        if sync_dify_valkey_password ".env" ".env.dify"; then
            print_substep "Syncing REDIS_PASSWORD to Dify..."
        fi

        # Sync Plugin Daemon keys
        plugin_key=$(grep -E "^PLUGIN_DAEMON_KEY=" .env | cut -d'=' -f2-) || true
        inner_key=$(grep -E "^PLUGIN_DIFY_INNER_API_KEY=" .env | cut -d'=' -f2-) || true
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
        sandbox_key=$(grep -E "^SANDBOX_API_KEY=" .env | cut -d'=' -f2-) || true
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
        cur_init_pw=$(grep -E "^INIT_PASSWORD=" .env.dify | cut -d'=' -f2-) || true
        if [ -z "$cur_init_pw" ] || [ "${#cur_init_pw}" -gt 30 ]; then
            print_substep "Setting Dify INIT_PASSWORD (dedicated <=30-char setup gate)..."
            update_env_value ".env.dify" "INIT_PASSWORD" "$(generate_password 24)"
        fi

        # Sync per-service DB credentials to Dify
        local dify_db_user dify_db_pass dify_plugin_db_user dify_plugin_db_pass
        dify_db_user=$(grep -E "^DIFY_DB_USER=" .env | cut -d'=' -f2-) || true
        dify_db_pass=$(grep -E "^DIFY_DB_PASSWORD=" .env | cut -d'=' -f2-) || true
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
    # #446: `llm-box` was a PHANTOM (exists in no compose file) — the real
    # stable AMD profile is `llm-legacy` (CLAUDE.md profile table).
    # #1292 (operator decision E3): `llm-manager` is ONE entry that expands to
    # ${RZFZ_LLM_MANAGER_PROFILES} when the selection is turned into
    # COMPOSE_PROFILES below — three checkboxes for one decision would only
    # invite half a manager. It is the LLM default; the GPUStack profiles are
    # the deliberate alternative and are mutually exclusive with it.
    local -a profile_names=("chat" "dify" "llm-manager" "llm-legacy" "monitor" "searxng" "stts" "gotenberg" "gitea" "lightrag" "cognee" "paperclip" "matrix")
    local -a profile_descs=(
        "Open WebUI Chat Interface"
        "Dify Workflow Automation"
        "LLM Manager (default: manager + registry + worker agent)"
        "LLM Inference (CPU only, GPUStack 0.7.1)"
        "LLM Inference (AMD Vulkan, GPUStack 0.7.1)"
        "LLM Inference (NVIDIA CUDA, GPUStack 0.7.1)"
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
    # Defaults: llm-manager is the LLM front end; every GPUStack profile is off,
    # as are git, lightrag, cognee and the agentic/matrix services. #1448 folded
    # llm-cuda into llm-legacy, so this array is one shorter than in 2026.08 —
    # it is index-parallel to profile_names above and the wizard checks that.
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
                # Name-based so adding another backend cannot be broken by index
                # drift: enabling any one deselects the others.
                case "$name" in
                    llm-manager|llm-legacy)
                        # #465/#946: the GPUStack runtime profiles share
                        # container_name gpustack, so only one of THEM may be on.
                        # #1292 (E3): `llm-manager` joins that group as an
                        # operator DECISION, not as a technical necessity — the
                        # wizard offers one LLM backend so a first install is
                        # not a choice about federation.
                        #
                        # rerevE: the reason this block used to give ("a
                        # dual-backend box deploys nothing and still reports
                        # four red probes") has not been true since #1441 (one
                        # ownership question instead of four probes) and #1442
                        # (the manager FRONTS GPUStack once federated). Running
                        # both is supported on an existing box; the wizard just
                        # does not build it.
                        if [ "${selected[$cursor]}" = "0" ]; then
                            local _j
                            for _j in "${!profile_names[@]}"; do
                                case "${profile_names[$_j]}" in
                                    llm-manager|llm-legacy) selected[$_j]=0 ;;
                                esac
                            done
                            selected[$cursor]=1
                        else
                            selected[$cursor]=0
                        fi
                        ;;
                    *)
                        [ "${selected[$cursor]}" = "1" ] && selected[$cursor]=0 || selected[$cursor]=1
                        ;;
                esac
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
            # #1292 (E3): the one manager checkbox IS the trio — the registry
            # holds the artifacts and the worker agent runs the engines, so a
            # manager on its own serves nothing.
            if [ "${profile_names[$i]}" = "llm-manager" ]; then
                result="${result}${RZFZ_LLM_MANAGER_PROFILES}"
            else
                result="${result}${profile_names[$i]}"
            fi
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
# #3207: `rzfz init --factory-reset` from a script or `ssh host rzfz …` (no
# terminal, the confirmation piped in) completed the reset, then entered the
# setup wizard, which cannot read a terminal, and exited 1 — a reset that had
# succeeded reported failure. Without a terminal and without a non-interactive
# configuration (--package, --skip-interactive), init now stops before the
# wizard with rc 0 and says how to continue; with a terminal nothing changes.
# Returns 0 when init should stop here.
init_stop_before_wizard_after_reset() {
    [ "${FACTORY_DEFAULT:-false}" = true ] && [ "${SKIP_INTERACTIVE:-false}" = false ] && [ ! -t 0 ] || return 1
    print_success "Factory reset done. No terminal: the setup wizard is not started."
    print_info "  Configure the box at a terminal with 'rzfz init', or without one with"
    print_info "  'rzfz init --package <configuration> ...' or 'rzfz init --skip-interactive ...'."
    return 0
}

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
        [ -z "$input_domain" ] || INIT_DOMAIN_EXPLICIT=true
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
        echo "  At least 8 characters with a lowercase letter, an uppercase letter and a digit."
        echo ""
        while true; do
            read -s -p "  Admin Password: " input_pw
            echo ""
            if ! _pw_reason="$(password_policy_violation "$input_pw")"; then
                print_warning "${_pw_reason} Try again."
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
        if echo "$CONFIG_PROFILES" | grep -qE "llm-legacy|(^|,)llm(-cpu)?($|,)"; then
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
            read -p "  Allowed Google Workspace domain(s), comma separated (e.g. example.com): " CONFIG_GOOGLE_OAUTH_DOMAIN
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
            llm-legacy) echo -e "    ${GREEN}✓${NC} LLM Inference (GPUStack 0.7.1, HARDWARE=${CONFIG_HARDWARE:-amd})" ;;
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
# #2512: LLM_MASTER_ROLE from the package preset (operator decision
# 2026-09-26): testvm-cpu -> standalone (the CPU box hosts its own 4B),
# master-cpu -> fleet (a CPU master serving GPU workers hosts no models), GPU
# presets -> standalone (the master has a GPU). No preset: an existing value
# is kept (a re-init must not flip a fleet master), else standalone.
write_llm_master_role() {
    local env_file="${1:-.env}" role="${CONFIG_LLM_MASTER_ROLE:-}" cur
    if [ -z "$role" ]; then
        cur=$(read_env_value "$env_file" LLM_MASTER_ROLE 2>/dev/null) || cur=""
        role="${cur:-standalone}"
    fi
    case "$role" in
        standalone|fleet) : ;;
        *) print_warning "LLM_MASTER_ROLE='${role}' is neither standalone nor fleet — writing standalone (#2512)."; role="standalone" ;;
    esac
    # Fix round 1: `fleet` is a CPU master's role (hardware_catalog.
    # derive_master_role) — a GPU master hosts its own models. master-cpu with
    # --hardware amd would otherwise write a master that serves nothing.
    if [ "$role" = "fleet" ]; then
        local hw fam
        hw="${CONFIG_HARDWARE:-}"
        [ -n "$hw" ] || hw=$(read_env_value "$env_file" HARDWARE 2>/dev/null) || hw=""
        fam=$(HW="$hw" python3 -c '
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "core", "llm"))
import hardware_catalog as hc
print(hc.hardware_family(os.environ.get("HW", "")))' "${SCRIPT_DIR:-.}" 2>/dev/null) || fam=""
        if [ -n "$hw" ] && [ "$fam" != "cpu" ]; then
            print_warning "LLM_MASTER_ROLE=fleet is a CPU master's role, but HARDWARE=${hw} — this master has a GPU and hosts its own models; writing standalone (#2512)."
            role="standalone"
        fi
    fi
    update_env_value "$env_file" "LLM_MASTER_ROLE" "$role"
    print_substep "Set LLM_MASTER_ROLE=$role"
}

# #2815: cognee/lightrag chat models. The template default (openai/gemma4,
# qwen3.6) is not served by every box - a CPU box serves only the 4B - and a
# model the manager does not serve makes every cognify call fail with 400 for
# ever. Write the hardware-aware default chat alias, exactly as post-install's
# update_env_model_config does, but ONLY over the template default or an empty
# value: a model the operator set is theirs.
write_consumer_chat_models() {
    local env_file="${1:-.env}" hw="${2:-}" alias k cur tmpl
    [ -n "$hw" ] || hw=$(read_env_value "$env_file" HARDWARE 2>/dev/null) || hw=""
    alias=$(HW="$hw" python3 -c '
import os, sys, yaml
sys.path.insert(0, os.path.join(sys.argv[1], "core", "llm"))
import hardware_catalog as hc
spec = yaml.safe_load(open(os.path.join(sys.argv[1], "core", "llm", "standard-models.yaml")))
print(hc.defaults_for(spec, os.environ.get("HW", "")).get("chat", ""))' "${SCRIPT_DIR:-.}" 2>/dev/null) || alias=""
    [ -n "$alias" ] || return 0
    for k in COGNEE_LLM_MODEL LIGHTRAG_LLM_MODEL; do
        cur=$(read_env_value "$env_file" "$k" 2>/dev/null) || cur=""
        tmpl=$(read_env_value "${SCRIPT_DIR:-.}/config/.env.example" "$k" 2>/dev/null) || tmpl=""
        if [ -z "$cur" ] || [ "$cur" = "$tmpl" ]; then
            if [ "$k" = COGNEE_LLM_MODEL ]; then
                update_env_value "$env_file" "$k" "openai/$alias"
            else
                update_env_value "$env_file" "$k" "$alias"
            fi
            print_substep "Set $k for hardware '${hw:-default}' (chat model ${alias})"
        fi
    done
}

# #2644: MAIN_DOMAIN, and everything that has to move with it, in one place.
#
# A reconfigure (`rzfz init --no-secrets --domain <new>` over a running box)
# used to write MAIN_DOMAIN alone. The `*_DOMAIN` keys were already resolved
# literals of the OLD domain (#906 below), so Caddy, Authentik and every app
# stayed on `<sub>.<old>` while the parts that read MAIN_DOMAIN directly moved:
# a half-renamed box (the init suite's reconfigure scenario). The derived keys
# and the .env.dify URLs now move too — by razzfazz_common.redomain, the rule
# `rzfz setup --set-domain` uses — and the move follows that command's
# protocol: the old domain is recorded in .redomain-pending BEFORE the env
# files change, and settle_init_redomain removes it once the databases are
# reconciled. Until then `rzfz setup --set-domain <new>` finishes the move and
# `rzfz init --force` refuses (#2115); a later `rzfz init` on the new domain
# resumes it (below).
apply_main_domain() {
    local _prev_domain _resolved _rederived _pending=""
    _prev_domain=$(read_env_value ".env" MAIN_DOMAIN 2>/dev/null) || _prev_domain=""
    INIT_PREVIOUS_DOMAIN=""
    # The pending marker is about the databases: read and written only over
    # live state.
    if [ "$INIT_HAD_LIVE_STATE" = true ] && [ -f .redomain-pending ]; then
        _pending=$(head -n 1 .redomain-pending | tr -d '[:space:]') || _pending=""
    fi
    # #2644: the keys a previous run resolved against the OLD domain move first,
    # BEFORE the templates below are resolved: a key that is still
    # `y.ai.${MAIN_DOMAIN}` is never matched (resolved first, with `ai.kunde.at`
    # -> `kunde.at`, it would read `y.ai.kunde.at` and be moved on to
    # `y.kunde.at`). Only for a domain the operator gave.
    if [ "$INIT_DOMAIN_EXPLICIT" = true ] && [ -n "$_prev_domain" ] && [ "$_prev_domain" != "$CONFIG_DOMAIN" ]; then
        # An earlier unfinished move keeps its marker (the oldest debt). A
        # third domain over it never reaches here: refused after Step 3.
        if [ "$INIT_HAD_LIVE_STATE" = true ] && [ -z "$_pending" ]; then
            # Nothing has changed yet: without the marker no later run could
            # finish a move that stops half-way, so do not start it.
            if ! (umask 077; printf '%s\n' "$_prev_domain" > .redomain-pending.tmp && mv -f .redomain-pending.tmp .redomain-pending); then
                print_error "Could not record ${_prev_domain} in .redomain-pending: nothing was changed, MAIN_DOMAIN is still ${_prev_domain}."
                exit 1
            fi
        fi
        if ! _rederived=$(redomain_derived_domain_keys ".env" "$_prev_domain" "$CONFIG_DOMAIN" ".env.dify"); then
            print_error "Could not evaluate the re-domain rule (razzfazz_common.redomain): nothing was changed, MAIN_DOMAIN is still ${_prev_domain}."
            exit 1
        fi
        if [ -n "$_rederived" ]; then
            print_substep "Moved the subdomain keys from ${_prev_domain} to ${CONFIG_DOMAIN} (#2644):"
            printf '%s\n' "$_rederived"
        fi
        INIT_PREVIOUS_DOMAIN="$_prev_domain"
    fi
    # Resume (#2644 port review I1): an earlier run moved the env files to this
    # domain and stopped before the databases followed (.redomain-pending still
    # names the old one). .env alone reads "no change", so the marker decides:
    # the database half runs old -> this domain, explicit --domain or not.
    if [ -n "$_pending" ] && [ "$_pending" != "$CONFIG_DOMAIN" ] && [ "$_prev_domain" = "$CONFIG_DOMAIN" ]; then
        print_substep "Resuming the unfinished move from ${_pending} to ${CONFIG_DOMAIN} (.redomain-pending, #2644)."
        INIT_PREVIOUS_DOMAIN="$_pending"
    fi
    update_env_value ".env" "MAIN_DOMAIN" "$CONFIG_DOMAIN"
    print_substep "Set MAIN_DOMAIN=$CONFIG_DOMAIN"
    # #906: .env.example ships the subdomain keys as `X.${MAIN_DOMAIN}`
    # templates. Caddy expands them in a shell; every service that reads
    # .env through compose's `env_file:` does not — compose interpolates
    # nothing there. Measured on a re-domained box: OWUI got
    # `OPENID_PROVIDER_URL=https://auth.${MAIN_DOMAIN}/…` verbatim and every
    # SSO login answered 500. Write them resolved, right where the domain
    # becomes known.
    _resolved=$(resolve_domain_templates ".env")
    if [ -n "$_resolved" ]; then
        print_substep "Resolved the subdomain keys against MAIN_DOMAIN (#906):"
        printf '%s\n' "$_resolved"
    fi
}

# #2644: the domain of the box this run is reconfiguring — .env's MAIN_DOMAIN
# over live state, unless it is still the template's placeholder. Empty on a
# fresh install, so the default there stays what it was.
current_box_domain() {
    local cur tpl
    [ "$INIT_HAD_LIVE_STATE" = true ] || return 0
    cur=$(read_env_value ".env" MAIN_DOMAIN 2>/dev/null) || cur=""
    tpl=$(read_env_value "config/.env.example" MAIN_DOMAIN 2>/dev/null) || tpl=""
    case "$cur" in
        ""|*'$'*) return 0 ;;
    esac
    [ "$cur" != "$tpl" ] || return 0
    printf '%s\n' "$cur"
}

# #2644: the Open WebUI admin is the one stack identity whose KEY is the domain:
# `razzfazz-ai-admin@<MAIN_DOMAIN>` (modules/chat/compose.yml ADMIN_EMAIL, the
# seeder's sign-in). A reconfigure that keeps the databases (`--no-secrets`) and
# changes the domain left the row at `@<old>`, so openwebui-seed signed in as
# `@<new>`, got 400 "email or password incorrect", exited 2, and init ended
# INCOMPLETE (exit 6). scripts/sql/redomain-openwebui.sql rewrites Open WebUI's
# `config` rows, not its accounts, so this move is init's own. Carried over
# after .env names the new domain, before the `up` that runs the seeder.
#
# Data-safe by construction: ONE statement (data-modifying CTEs share one
# snapshot), exactly the stack's own address and nothing else, and nothing at
# all when `@<new>` is already taken — two accounts are never merged. Never
# fatal: if postgres cannot be reached the seeder still reports the outcome.
# Usage: migrate_owui_admin_identity <old_domain> <new_domain>
migrate_owui_admin_identity() {
    local old="${1:-}" new="${2:-}" pg_user owui_db out deadline
    local wait_s="${RAZZFAZZ_INIT_PG_WAIT_S:-90}"
    # Open WebUI is running during the move: a lock it holds on auth/user must
    # not hang init. One statement, so a timeout changes nothing.
    local pg_opts="-c lock_timeout=5s -c statement_timeout=30s"
    [ -n "$old" ] && [ -n "$new" ] && [ "$old" != "$new" ] || return 0
    local old_email="razzfazz-ai-admin@${old}" new_email="razzfazz-ai-admin@${new}"
    pg_user=$(read_env_value ".env" POSTGRES_USER 2>/dev/null) || pg_user=""
    pg_user="${pg_user:-docker}"
    owui_db=$(read_env_value ".env" OPENWEBUI_DB 2>/dev/null) || owui_db=""
    owui_db="${owui_db:-openwebui_db}"
    print_step "Carrying the Open WebUI admin over to ${new} (#2644)..."
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres; then
        docker compose up -d --no-deps postgres >/dev/null 2>&1 || true
    fi
    # A wall-clock deadline: the probe itself takes time too.
    deadline=$((SECONDS + wait_s))
    until docker exec postgres pg_isready -q -t 2 -U "$pg_user" >/dev/null 2>&1; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            print_warning "postgres did not become ready within ${wait_s}s — the Open WebUI admin stays ${old_email}; openwebui-seed will report the sign-in."
            return 0
        fi
        sleep 2
    done
    out=$(docker exec -i -e PGOPTIONS="$pg_opts" postgres psql -X -q -tA -v ON_ERROR_STOP=1 -U "$pg_user" -d postgres \
            -v db="$owui_db" <<'SQL' 2>&1
SELECT count(*) FROM pg_database WHERE datname = :'db';
SQL
    ) || out=""
    if [ "$out" != "1" ]; then
        print_substep "No Open WebUI database (${owui_db}) yet — nothing to carry over."
        return 0
    fi
    if ! out=$(docker exec -i -e PGOPTIONS="$pg_opts" postgres psql -X -q -tA -v ON_ERROR_STOP=1 -U "$pg_user" -d "$owui_db" \
            -v old="$old_email" -v new="$new_email" <<'SQL' 2>&1
WITH st AS (
    SELECT (EXISTS (SELECT 1 FROM auth WHERE lower(email) = lower(:'old'))
         OR EXISTS (SELECT 1 FROM "user" WHERE lower(email) = lower(:'old'))) AS has_old,
           (NOT EXISTS (SELECT 1 FROM auth WHERE lower(email) = lower(:'new'))
        AND NOT EXISTS (SELECT 1 FROM "user" WHERE lower(email) = lower(:'new'))) AS free
), a AS (
    UPDATE auth SET email = :'new'
     WHERE lower(email) = lower(:'old') AND (SELECT free FROM st) RETURNING 1
), u AS (
    UPDATE "user" SET email = :'new'
     WHERE lower(email) = lower(:'old') AND (SELECT free FROM st) RETURNING 1
)
SELECT CASE WHEN NOT (SELECT has_old FROM st) THEN 'absent'
            WHEN (SELECT free FROM st)
            THEN 'moved ' || (SELECT count(*) FROM a) || ' ' || (SELECT count(*) FROM u)
            ELSE 'taken' END;
SQL
    ); then
        print_warning "Could not carry the Open WebUI admin over (nothing was changed): $(printf '%s' "$out" | head -n 2)"
        return 0
    fi
    case "$out" in
        "moved 1 1") print_substep "Open WebUI admin ${old_email} -> ${new_email}" ;;
        absent)      print_substep "No Open WebUI admin under ${old_email} — nothing to carry over." ;;
        taken)       print_warning "${new_email} already exists in Open WebUI — ${old_email} was left as it is (two accounts are never merged)." ;;
        *)           print_warning "Open WebUI admin carry-over reported '${out}' — check the auth and user rows for ${new_email}." ;;
    esac
    return 0
}

# #2644: refuse a THIRD domain over an unfinished move. .redomain-pending says
# A, .env already says B (an earlier move A -> B stopped before its databases
# followed), and this run asks for C. Moving A -> C is not safe: each database
# of the reconcile is its own transaction, so after a partial A -> B some rows
# may already name B while others still name A, and an A -> C pass (whose
# leftover check looks for A only) would leave the B rows behind and report
# success. The account move has the same blind spot. Finishing A -> B inside
# this run and then moving B -> C would double every step that can fail. So
# this refuses, as `rzfz setup --set-domain C` does in the same state, and
# names the two steps. Asking for B (resume) or A (back) is allowed. Runs after
# Step 3, before anything domain-related is written. 0 = proceed, 1 = refused.
refuse_a_move_over_an_unfinished_one() {
    local pending="" cur=""
    [ "$INIT_HAD_LIVE_STATE" = true ] && [ -f .redomain-pending ] || return 0
    pending=$(head -n 1 .redomain-pending | tr -d '[:space:]') || pending=""
    cur=$(read_env_value ".env" MAIN_DOMAIN 2>/dev/null) || cur=""
    [ -n "$pending" ] && [ -n "$cur" ] && [ "$pending" != "$cur" ] || return 0
    case "${CONFIG_DOMAIN:-}" in
        "$cur"|"$pending") return 0 ;;
    esac
    print_error "Refusing to move this box to ${CONFIG_DOMAIN}: the move from ${pending} to ${cur} is not finished (.redomain-pending)."
    print_info  "  Finish it first: rzfz init --no-secrets --domain ${cur}, and at the existing-installation menu choose 2) Reconfigure only"
    print_info  "                   (or, non-interactive: rzfz setup --set-domain ${cur})"
    print_info  "  then move the box: rzfz setup --set-domain ${CONFIG_DOMAIN}"
    return 1
}

# #2644: the database half of an init re-domain over live data, before the `up`
# that recreates the services and runs the seeders. Two moves that do not
# overlap: the Open WebUI admin ACCOUNT (above, init's own), then the rows
# `rzfz setup --set-domain` and the Config Portal rewrite — Authentik's proxy
# providers, outpost, OAuth2 redirect URIs and launch URLs, and Open WebUI's
# `config` rows (webui.url, the OAuth provider URL), which override the env —
# by the same script they use, in its --db-only mode.
#
# Order (#2644 port review M2). The Portal re-templates the blueprints first
# and runs this SQL after; here it runs BEFORE the `up`, because Open WebUI
# reads its `config` rows only at start — rewritten after the `up`, the
# recreated Open WebUI would already hold the old webui.url. The Authentik rows
# converge either way (the blueprints and the SQL both write <new>), but this
# pass judges them before authentik-init has re-applied the blueprints, so its
# leftover verdict is provisional: settle_init_redomain runs the same --db-only
# pass again after authentik-init (idempotent), and only THAT verdict decides
# whether .redomain-pending goes.
# Usage: finish_init_redomain <old_domain> <new_domain>
finish_init_redomain() {
    local old="${1:-}" new="${2:-}" rc=0
    [ -n "$old" ] && [ -n "$new" ] && [ "$old" != "$new" ] || return 0
    migrate_owui_admin_identity "$old" "$new"
    print_step "Rewriting ${old} -> ${new} in the Authentik and Open WebUI databases (#2644)..."
    bash scripts/redomain-reconcile.sh --db-only "$old" "$new" || rc=$?
    if [ "$rc" -ne 0 ]; then
        print_info "The database pass reported exit ${rc}; it runs again once Authentik has re-applied its blueprints."
    fi
    return 0
}

# #2644: after authentik-init has re-templated the blueprints for the new
# domain (wait_for_authentik_init): the --db-only pass again — this is the
# verdict — then Authentik's cache is cleared, as the Portal and the full
# reconcile do (port review M1: a re-domain otherwise kept serving a cached
# OAuth source on the old host until the cache expired). The marker goes only
# when this pass is clean, and only when it names this move (old, or new for a
# move back). Never fatal: otherwise it stays, and init names the way out.
# Usage: settle_init_redomain <old_domain> <new_domain>
settle_init_redomain() {
    local old="${1:-}" new="${2:-}" pending="" rc=0
    [ -n "$old" ] && [ -n "$new" ] && [ "$old" != "$new" ] || return 0
    print_step "Verifying the move from ${old} to ${new} in the Authentik and Open WebUI databases (#2644)..."
    bash scripts/redomain-reconcile.sh --db-only "$old" "$new" || rc=$?
    docker exec authentik-server ak shell -c 'from django.core.cache import cache; cache.clear()' >/dev/null 2>&1 \
        || print_warning "Authentik's cache was not cleared (authentik-server did not answer); it expires on its own, or run: docker restart authentik-server"
    [ -f .redomain-pending ] && { pending=$(head -n 1 .redomain-pending | tr -d '[:space:]') || pending=""; }
    if [ "$rc" -eq 0 ]; then
        case "$pending" in
            "$old"|"$new") rm -f .redomain-pending ;;
        esac
        return 0
    fi
    print_warning "The database half of the move from ${old} to ${new} is not finished (exit ${rc}). Finish it with: rzfz setup --set-domain ${new}"
    return 0
}

apply_configuration() {
    print_step "Applying configuration to environment files..."

    # #2512: the master's role, before anything reads it.
    write_llm_master_role ".env"

    # Main domain
    if [ -n "$CONFIG_DOMAIN" ]; then
        apply_main_domain
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
        # #2002: the same address is where the stack's own alerts go (Wazuh
        # mails alerts >= WAZUH_EMAIL_ALERT_LEVEL there). Seeded once, here;
        # the operator may point it elsewhere later.
        update_env_value ".env" "RAZZFAZZ_OPERATOR_EMAIL" "$CONFIG_ADMIN_EMAIL"
        print_substep "Set RAZZFAZZ_OPERATOR_EMAIL=$CONFIG_ADMIN_EMAIL"
    fi

    # Profiles
    if [ -n "$CONFIG_PROFILES" ]; then
        update_env_value ".env" "COMPOSE_PROFILES" "$CONFIG_PROFILES"
        print_substep "Set COMPOSE_PROFILES=$CONFIG_PROFILES"
    fi

    # M018 Phase 6 / S06.5 + #1448: HARDWARE + COMPOSE_FILE.
    # `llm-legacy` (GPUStack 0.7.1) is ONE service that runs on EVERY hardware
    # line via a device overlay file (modules/llm/compose.devices.<HARDWARE>.yml)
    # — amd and nvidia since #1448, cpu since #1447 part b.
    # Auto-detect if not passed.
    # Found rc6.9: pre-fix, testvm-cpu / single-box presets left HARDWARE at
    # the .env.example default (amd) on a CPU install — which now picks the
    # WRONG IMAGE, not just the wrong label.
    local llm_profile_active=""
    case ",$CONFIG_PROFILES," in
        *,llm-legacy,*)  llm_profile_active="llm-legacy" ;;
        # #1292 (E3): a Manager box needs EVERYTHING below just as much — the
        # worker agent launches llama.cpp/vLLM engines itself, so it needs
        # HARDWARE recorded, RENDER_GID + the host Vulkan userspace on AMD, the
        # nvidia container runtime check on NVIDIA, and an explicit
        # COMPOSE_FILE (docker compose 2.40+ chokes on an empty value). Before
        # this the whole block was GPUStack-only and a manager-only box got
        # none of it.
        *,llm-worker-agent,*) llm_profile_active="llm-worker-agent" ;;
    esac
    # #1448 (cutover C8): `llm-cuda` was merged into `llm-legacy`. #1447
    # (cutover C7a): `llm` (GPUStack v2.1.x) was REMOVED, and its token is
    # rewritten the same way. Accept both retired tokens on the command line and
    # rewrite them, so an operator (or a script, or a customer runbook) that
    # still passes them gets a working box instead of a profile no compose file
    # defines — which docker compose accepts in silence, starting nothing.
    # This runs AFTER the case above on purpose: a selection that names only a
    # retired token leaves llm_profile_active empty there, and these arms are
    # what fill it in. `llm` maps by HARDWARE, because that is what the 2.x
    # profile carried: one profile, three hardware lines.
    case ",$CONFIG_PROFILES," in
        *,llm,*)
            _llm_target="llm-legacy"
            # every hardware line lands on llm-legacy since #1447 part b
            print_warning "Profile 'llm' (GPUStack v2.1.x) was REMOVED in 2026.09 (#979/#1447) — rewriting to '${_llm_target}'. The LLM Manager is the LLM front end of every box; this keeps the GPUStack 0.7.1 backend you asked for, and the manager federates it (#1442)."
            CONFIG_PROFILES=$(printf '%s\n' "$CONFIG_PROFILES" | tr ',' '\n' | sed "s/^llm$/${_llm_target}/" | awk 'NF && !seen[$0]++' | paste -sd,)
            llm_profile_active="$_llm_target"
            unset _llm_target
            print_substep "COMPOSE_PROFILES now: $CONFIG_PROFILES"
            ;;
        *,llm-cuda,*)
            print_warning "Profile 'llm-cuda' was merged into 'llm-legacy' (#1448) — rewriting; HARDWARE=nvidia selects the CUDA overlay."
            CONFIG_PROFILES=$(printf '%s\n' "$CONFIG_PROFILES" | tr ',' '\n' | sed 's/^llm-cuda$/llm-legacy/' | awk 'NF && !seen[$0]++' | paste -sd,)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-nvidia}"
            llm_profile_active="llm-legacy"
            print_substep "COMPOSE_PROFILES now: $CONFIG_PROFILES"
            ;;
        *,llm-cpu,*)
            print_warning "Profile 'llm-cpu' was merged into 'llm-legacy' (#1447) — rewriting; HARDWARE=cpu selects the CPU overlay (upstream v0.7.1-cpu image)."
            CONFIG_PROFILES=$(printf '%s\n' "$CONFIG_PROFILES" | tr ',' '\n' | sed 's/^llm-cpu$/llm-legacy/' | awk 'NF && !seen[$0]++' | paste -sd,)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-cpu}"
            llm_profile_active="llm-legacy"
            print_substep "COMPOSE_PROFILES now: $CONFIG_PROFILES"
            ;;
    esac

    # #2077: the three arms above rewrite CONFIG_PROFILES, but COMPOSE_PROFILES
    # was already written to .env further up — so without this the rewrite lived
    # only in the shell variable and in the "COMPOSE_PROFILES now:" line, while
    # the BOX kept the retired token. docker compose matches no service for it
    # and starts nothing, so the install finished green with no LLM backend and
    # the operator had just been told the token was fixed. Caught by the rc1
    # Phase 3 sweep: scenario 03 installed with `llm-cpu` and ended with
    # gpustack/model-sync/ollama-proxy not_found.
    if [ -n "$CONFIG_PROFILES" ]; then
        update_env_value ".env" "COMPOSE_PROFILES" "$CONFIG_PROFILES"
    fi
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
        # #946's NVIDIA redirect stood here: it caught `llm` on NVIDIA and
        # rewrote it to `llm-legacy`. #1447 removed the `llm` profile outright,
        # so the token is rewritten for EVERY hardware line further up and can
        # never reach this point — the branch would have been dead code that
        # reads like a live safeguard.
        # #1447 part b: HARDWARE=cpu + llm-legacy is now the SUPPORTED CPU
        # shape — the refusal that stood here belonged to the era when the CPU
        # line had its own profile.
        update_env_value ".env" "HARDWARE" "$CONFIG_HARDWARE"
        print_substep "Set HARDWARE=$CONFIG_HARDWARE"
        write_consumer_chat_models ".env" "$CONFIG_HARDWARE"   # #2815
        # The DEVICE overlay is profile-specific. #1448 (cutover C8): the
        # merged GPUStack 0.7.1 service carries NO device wiring of its own, so
        # `llm-legacy` needs the overlay — and its startup guard REFUSES rather
        # than serving from the CPU when it is missing (#434). The manager trio
        # stays self-contained.
        _cf="compose.yml"
        if [ "$llm_profile_active" = "llm-legacy" ]; then
            _cf="${_cf}:modules/llm/compose.devices.${CONFIG_HARDWARE}.yml"
        fi
        # #1014 (audit EXO-6/OPS-6): the WORKER-AGENT overlay is HARDWARE-
        # specific, not profile-specific, and it used to hang off the `llm`
        # branch above — where an NVIDIA box can never arrive. Eight lines
        # earlier the #946 redirect rewrites llm → llm-cuda under exactly the
        # condition this needed, so `llm_profile_active` is "llm-cuda" by the
        # time the branch is tested and every NVIDIA install fell to the else
        # branch with a flat COMPOSE_FILE=compose.yml. The consequence is quiet:
        # llm-worker-agent has no nvidia runtime, so nvidia-smi and NVML are
        # invisible to it, runtime._live_metrics returns nothing and the
        # console's "Load & usage" chart never leaves "Collecting live
        # samples…" (AMD reads amdgpu sysfs, which any container sees; NVIDIA
        # has no such sysfs).
        #
        # The overlay touches ONE service and only adds `runtime: nvidia` plus
        # NVIDIA_DRIVER_CAPABILITIES=utility to it, so applying it whenever the
        # box IS NVIDIA is both correct and inert where that service does not
        # run. nvidia-container-toolkit is a documented prerequisite of every
        # NVIDIA path already.
        if [ "$CONFIG_HARDWARE" = "nvidia" ]; then
            _cf="${_cf}:compose.worker-agent-nvidia.yml"
        fi
        update_env_value ".env" "COMPOSE_FILE" "$_cf"
        # Echo what was WRITTEN. The old line re-rendered the string it thought
        # it had set and therefore never showed the worker-agent overlay — the
        # secondary finding of the same audit item.
        print_substep "Set COMPOSE_FILE=$_cf"

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
            # #536 (2026.09): gpustack now runs NON-ROOT by default on this
            # profile, so this gid stopped being a nice-to-have — it is the
            # container's ONLY path to /dev/kfd + /dev/dri. Ask the DEVICE
            # first: `getent group render` describes the host's group table,
            # /dev/dri/renderD128's group is the thing the kernel actually
            # checks, and on a host where the two disagree (or where there is
            # no `render` group at all) the device wins. Falling back to the
            # 992 literal is a GUESS — say so out loud rather than shipping a
            # box that hangs at ready_replicas:0 with green health signals.
            # `|| true` on both probes: `getent group render` exits 2 when the
            # group does not exist and this script runs under `set -eo
            # pipefail`, so a bare assignment would abort the install.
            local render_gid render_src
            render_gid=$(stat -c %g /dev/dri/renderD128 2>/dev/null || true)
            render_src="/dev/dri/renderD128"
            if [ -z "$render_gid" ]; then
                render_gid=$(getent group render 2>/dev/null | cut -d: -f3 || true)
                render_src="getent group render"
            fi
            if [ -z "$render_gid" ]; then
                render_gid=992
                render_src="fallback guess"
                print_warning "Could not determine this host's render gid (no /dev/dri/renderD128, no 'render' group) — falling back to RENDER_GID=992."
                print_info "gpustack runs non-root (#536) and reaches the GPU only through this gid. If it is wrong, model deploys hang at replicas:1/ready_replicas:0 forever with 'amdgpu_query_info(ACCEL_WORKING) failed (-13)' in the logs (rc6.7 #63). Fix with: rzfz setup, or set RENDER_GID in .env by hand."
            fi
            update_env_value ".env" "RENDER_GID" "$render_gid"
            print_substep "Set RENDER_GID=$render_gid (AMD GPU device access; source: ${render_src})"

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
                if razzfazz_sudo apt-get update -qq >/dev/null 2>&1 && \
                   razzfazz_sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libvulkan1 mesa-vulkan-drivers >/dev/null 2>&1; then
                    print_substep "Host Vulkan userspace installed (Mesa RADV → the bind-mount now carries a working amdgpu Vulkan driver)."
                else
                    print_warning "Host Vulkan install FAILED — gpustack models will exit 127. Run manually: sudo apt-get install -y libvulkan1 mesa-vulkan-drivers"
                    razzfazz_defer_host_step "Host Vulkan userspace NOT installed (gpustack models exit 127)" "sudo apt-get update && sudo apt-get install -y libvulkan1 mesa-vulkan-drivers"
                fi
            else
                print_substep "Host Vulkan userspace already present."
            fi
        elif [ "$CONFIG_HARDWARE" = "nvidia" ]; then
            # #946/#1448: on NVIDIA the llm-legacy service gets `runtime: nvidia`
            # from modules/llm/compose.devices.nvidia.yml. Verify
            # the nvidia container runtime is wired (nvidia-container-toolkit),
            # so a missing toolkit fails HERE with an actionable message instead
            # of a cryptic `docker compose up` error at first model deploy.
            # Non-fatal — warn, don't abort (the daemon may name it differently).
            if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
                print_substep "NVIDIA container runtime detected (the nvidia device overlay sets runtime: nvidia)."
            else
                print_warning "NVIDIA container runtime NOT found in 'docker info' — the llm-legacy NVIDIA path needs the nvidia-container-toolkit."
                print_warning "Install: sudo apt-get install -y nvidia-container-toolkit && sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
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
            # #2927: the Google sign-in is limited to these Workspace domains.
            if [ -n "$CONFIG_GOOGLE_OAUTH_DOMAIN" ]; then
                _gd=$(printf '%s' "$CONFIG_GOOGLE_OAUTH_DOMAIN" | tr 'A-Z' 'a-z' | tr -s ', ' ',,' | sed 's/^,//; s/,$//')
                if ! printf '%s' "$_gd" | grep -Eq '^[a-z0-9-]+(\.[a-z0-9-]+)+(,[a-z0-9-]+(\.[a-z0-9-]+)+)*$'; then
                    print_error "Invalid Google domain list '${CONFIG_GOOGLE_OAUTH_DOMAIN}' (use e.g. example.com,example.org)."
                    exit 1
                fi
                update_env_value ".env" "GOOGLE_OAUTH_DOMAIN" "$_gd"
                print_substep "Set the allowed Google domains: ${_gd}"
            elif [ -z "$(read_env_value ".env" GOOGLE_OAUTH_DOMAIN 2>/dev/null)" ]; then
                # #2927: the template default is empty (it used to name one
                # company's domain). An empty list admits nobody, so a Google
                # install without a domain would come up with a login that can
                # never work: refuse here instead of finishing a broken install.
                print_error "Google sign-in needs at least one allowed Google Workspace domain: pass --google-domain example.com (a comma separated list for several)."
                exit 1
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
    _GOOGLE=$(grep -m1 "^ENABLE_GOOGLE_OAUTH=" ".env" | cut -d= -f2) || true
    _OIDC=$(grep -m1 "^ENABLE_OPENWEBUI_OIDC=" ".env" | cut -d= -f2) || true
    if [ "$_GOOGLE" = "true" ] || [ "$_OIDC" = "true" ]; then
        update_env_value ".env" "ENABLE_OAUTH_SIGNUP" "true"
        # #659: without this the auto-created user lands 'pending' and
        # waits for an admin — the second half of the first-login trap.
        update_env_value ".env" "OWUI_DEFAULT_USER_ROLE" "user"
        print_substep "Set ENABLE_OAUTH_SIGNUP=true (Google=${_GOOGLE:-false}, OIDC=${_OIDC:-false})"
    else
        update_env_value ".env" "ENABLE_OAUTH_SIGNUP" "false"
    fi

    # #29: Derive GITEA_OAUTH_AUTO_REGISTER — true if Google OR Authentik OIDC is
    # enabled, so Gitea auto-registers SSO users (the compose gates
    # GITEA__oauth2_client__ENABLE_AUTO_REGISTRATION on this). Previously tied to
    # ENABLE_GOOGLE_OAUTH only → enabling JUST Authentik OIDC left it off.
    _GITEA_OIDC=$(grep -m1 "^ENABLE_GITEA_AUTHENTIK_OIDC=" ".env" | cut -d= -f2) || true
    if [ "$_GOOGLE" = "true" ] || [ "$_GITEA_OIDC" = "true" ]; then
        update_env_value ".env" "GITEA_OAUTH_AUTO_REGISTER" "true"
        print_substep "Set GITEA_OAUTH_AUTO_REGISTER=true (Google=${_GOOGLE:-false}, Gitea-OIDC=${_GITEA_OIDC:-false})"
    else
        update_env_value ".env" "GITEA_OAUTH_AUTO_REGISTER" "false"
    fi

    # #2507: Open WebUI's own Google keys + login auto-redirect, derived from
    # ENABLE_OPENWEBUI_OIDC (scripts/lib.sh). Must run after the Google
    # credentials and the OIDC switch above are final.
    print_substep "Deriving Open WebUI login keys (#2507)..."
    derive_owui_sso_env ".env"


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
        # #2595: COGNEE_ADMIN_PASSWORD is NOT aligned to the install password.
        # cognee, Open WebUI and mcp-manager hold it in their environment, and the
        # install password is also AUTHENTIK_BOOTSTRAP_PASSWORD, a write credential
        # for the whole directory. It keeps its own random value (minted below).
        # Dify: the shared admin password becomes the Dify ADMIN LOGIN password
        # (post-install's dify_ensure_admin seeds the live account with
        # AUTHENTIK_BOOTSTRAP_PASSWORD). Align the .env record here too — like
        # every other admin account above — so DIFY_ADMIN_PASSWORD matches the
        # live account instead of the random Phase-1 seed. Leaving it at the
        # random value made .env disagree with the DB: the operator's remembered
        # password logged in, but DIFY_ADMIN_PASSWORD (and anything reading it,
        # e.g. set-admin-password's status check) pointed at a value that never
        # worked. This is the DIFY_ADMIN_PASSWORD half of "one password
        # everywhere"; INIT_PASSWORD below is a SEPARATE first-run gate.
        update_env_value ".env" "DIFY_ADMIN_PASSWORD" "$CONFIG_ADMIN_PASSWORD"
        # INIT_PASSWORD is only the first-run gate and Dify 1.14+ caps it at 30
        # chars, so keep it a dedicated short token instead of the (possibly >30)
        # admin password.
        if [ -f ".env.dify" ]; then
            local cur_init_pw2
            cur_init_pw2=$(grep -E "^INIT_PASSWORD=" .env.dify | cut -d'=' -f2-) || true
            if [ -z "$cur_init_pw2" ] || [ "${#cur_init_pw2}" -gt 30 ]; then
                update_env_value ".env.dify" "INIT_PASSWORD" "$(generate_password 24)"
            fi
        fi
        print_substep "Set shared admin password for GPUStack, Komodo, Dify"
    fi

    # #2595: cognee's admin password is its own secret. The generate-secrets
    # pass mints it; a run that skipped that pass (--no-secrets on a box that
    # never had the key) mints it here, before cognee's first boot. An existing
    # value is kept: post-install moves a box whose value is still the
    # Authentik admin's to a new one through cognee's own API.
    if [ -z "$(read_env_value ".env" COGNEE_ADMIN_PASSWORD 2>/dev/null || true)" ]; then
        update_env_value ".env" "COGNEE_ADMIN_PASSWORD" "$(generate_password 24)"
        print_substep "Generated COGNEE_ADMIN_PASSWORD"
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
            # #1594: the two wildcard vHosts (*.agents, *.mcp) cannot use the
            # customer/ACME path — a wildcard covers one label and needs dns-01.
            update_env_value ".env" "TLS_WILDCARD_ISSUER" "issuer internal"
            print_substep "Set TLS_MODE=internal (self-signed certificates)"
            print_warning "Using self-signed certificates - browsers will show security warnings"
            ;;
        certificate|custom)
            update_env_value ".env" "TLS_MODE" "certificate"
            update_env_value ".env" "TLS_DIRECTIVE" ""
            # #1594: a `*.<domain>` certificate does NOT cover `<user>.agents.<domain>`.
            # Without this the two wildcard blocks fall through to ACME and retry
            # a structurally impossible dns-01 challenge for thirty days.
            update_env_value ".env" "TLS_WILDCARD_ISSUER" "issuer internal"
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
            # #1594: unchanged for ACME — per-name HTTP-01 on first visit, gated
            # by the managers' /api/tls/ask.
            update_env_value ".env" "TLS_WILDCARD_ISSUER" "on_demand"
            print_substep "Set TLS_MODE=letsencrypt (automatic Let's Encrypt certificates)"
            ;;
        *)
            print_warning "Unknown TLS mode '$CONFIG_TLS_MODE', defaulting to letsencrypt"
            update_env_value ".env" "TLS_MODE" ""
            update_env_value ".env" "TLS_DIRECTIVE" ""
            update_env_value ".env" "TLS_WILDCARD_ISSUER" "on_demand"
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
            if echo "$hosts_line" | razzfazz_sudo tee -a /etc/hosts >/dev/null 2>&1; then
                print_substep "Added local domain entries to /etc/hosts"
            else
                print_warning "Could not add the local domain entries to /etc/hosts (sudo required). Add them by hand: echo '$hosts_line' | sudo tee -a /etc/hosts"
                razzfazz_defer_host_step "Local domain entries NOT in /etc/hosts" "echo '$hosts_line' | sudo tee -a /etc/hosts"
            fi
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
    # #2488: capture the build output instead of just streaming+swallowing it.
    # A build failure here used to print a generic "Some images failed to
    # build." warning and CONTINUE — but `set -eo pipefail` (top of this
    # script) makes the LATER `docker compose up -d` fatal the moment it hits
    # the first image that never got built. On a parallel bake, a failure in
    # one target (e.g. cognee-frontend, unreachable fonts.googleapis.com)
    # cancels every OTHER in-flight target too, so `up` can fail on a
    # completely unrelated image (caddy, mid-`go build` when cancelled) —
    # naming caddy as the culprit 150 lines of scrollback below the real
    # error (measured on 0.91, 2026-09-25). Name the actual failed target(s)
    # here, at the point of the actual failure, and stop before `up` ever runs.
    # Name the source builds, and what they cost, before the build phase starts.
    if ! razzfazz_is_offline; then
        razzfazz_announce_source_builds "$(razzfazz_source_builds_pending .env 2>/dev/null || true)" install
    fi
    local _build_log
    _build_log="$(mktemp "${TMPDIR:-/tmp}/rzfz-image-build.XXXXXX")"
    if ! COMPOSE_FILE="$(compose_file_for_build)" razzfazz_compose_build --parallel 2>&1 | tee "${_build_log}"; then
        local _failed_targets
        # buildx's `target X: failed to solve`, or the line razzfazz_compose_build
        # prints per failed service when it builds one service per call.
        _failed_targets="$(grep -oP '(?<=^target )[^:]+(?=: failed to solve)|(?<=^Image build failed for service: )\S+(?= \(exit )' "${_build_log}" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
        _failed_targets="${_failed_targets% }"
        if [ -n "${_failed_targets}" ]; then
            print_error "Docker image build failed for: ${_failed_targets}. See the build output above for why — 'docker compose up' would otherwise fail on a different, unrelated missing image and misname the cause."
        else
            print_error "Docker image build failed (docker compose build --parallel exited non-zero); see the build output above for the failing service — its name could not be parsed out of this build's output."
        fi
        rm -f "${_build_log}"
        exit 1
    fi
    rm -f "${_build_log}"
    # #2006 part 2: say which tree these images came from, so the next init can
    # tell this build from an adopted one.
    razzfazz_record_custom_image_builds "$(init_resolved_profiles)" built

    print_success "Docker images ready."
}

# ==============================================================================
# Delivery-credentials blob (Workstream I, 2026.06-ga.5)
# ------------------------------------------------------------------------------
# Records the as-shipped admin password ONCE so a full factory reset can return
# the box to delivery state (the password printed on the device sticker).
# - Written at first init (provisional: that password may be the internal
#   install password). `rzfz set-admin-password` re-seals it with the password
#   the box is handed over with; the refresh re-seals a stale one; a factory
#   reset refuses a blob that no longer matches .env.
# - Encrypted at rest with a machine-stable key. This is NOT a strong secret
#   boundary (anyone with box file access already reads the live password from
#   `.env`); it just prevents casual disk-read and survives the volume wipe.
# - Returning to the delivery password is only ever done as part of a FULL
#   factory reset (all data wiped) — so it is re-provisioning, not an access
#   bypass on a live system.
# ==============================================================================
# The functions live in scripts/lib-delivery-blob.sh (shared with
# set-admin-password, post-install and the factory reset).
write_delivery_blob() {  # $1 = admin password given to the FIRST init (provisional)
    local pw="$1"
    [ -n "$pw" ] || return 0
    [ -f "$DELIVERY_BLOB" ] && return 0            # a later init never overwrites a sealed blob
    command -v openssl >/dev/null 2>&1 || { print_warning "openssl missing — delivery-credentials blob not written (factory-reset-to-delivery unavailable)."; return 0; }
    seal_delivery_blob "$pw" && print_substep "Recorded delivery credentials (factory-reset-to-delivery enabled)." || true
}

# ==============================================================================
# #2697: a generated admin password reaches the operator once, never a log.
# ------------------------------------------------------------------------------
# stdout and stderr go through the #427 run-log tee, so anything printed with
# print_* or echo lands in razzfazz-init-*.log. With a terminal, the password is
# written straight to /dev/tty, past the tee. Without one (cron, CI, a piped
# run), it is not printed at all: it goes into the box's .env (0600), where
# apply_configuration stores it anyway, and only that path is printed.
# ==============================================================================
reveal_generated_admin_password() {  # $1 = the generated password
    local pw="$1" env_file="$PWD/.env"
    [ -f "$env_file" ] || { (umask 077; : >"$env_file"); }
    update_env_value "$env_file" "AUTHENTIK_BOOTSTRAP_PASSWORD" "$pw"
    chmod 600 "$env_file" 2>/dev/null || true
    # The terminal is not a vault: script(1), sudo I/O logging and tmux/screen
    # logging still record what is written to it. Said on screen too.
    if { printf '\n  Generated admin password (shown once, not logged): %s\n  Save it now. Terminal capture (script, sudoers I/O logging, tmux/screen logs) may still record it.\n\n' "$pw" >/dev/tty; } 2>/dev/null; then
        print_warning "No admin password provided. A generated one was shown on the terminal only (not in this log)."
    else
        print_warning "No admin password provided and no terminal: a generated password was NOT printed."
    fi
    print_info "It is stored in ${env_file} (mode 0600) as AUTHENTIK_BOOTSTRAP_PASSWORD."
}

# ==============================================================================
# Factory Reset
# ==============================================================================
factory_reset() {
    # A blob that is not the password .env holds would put a stale (possibly the
    # internal install) password on every app. Refuse BEFORE anything is wiped.
    local _dstate=0; delivery_blob_state ".env" || _dstate=$?
    if [ "$_dstate" = 1 ]; then
        print_error "Factory reset refused: the delivery credentials blob does not match the admin password in .env (it would restore an outdated password)."
        print_info  "  Seal the current one first:  rzfz set-admin-password --all   (or 'rzfz post-install --refresh'), then run the reset again."
        exit 1
    fi
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
    # #2226: the supervised LLM engines hold the models volume; `down -v` failed
    # with "volume is in use" while they ran. Supervisor first, then engines.
    razzfazz_stop_supervised_engines --remove || true
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
        backup_keyring_rotate ".env" "$dpw" || print_warning "backup passphrase keyring not updated (#2132); set BACKUP_ENCRYPTION_PASSWORD by hand"
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
    # #1247: the banner must not claim success over a razzfazz.init one-shot
    # that ended non-zero — an operator seeing "Installation Complete" above a
    # module that can never work was the actual complaint in the issue.
    # $1 = "failed" when the one-shot gate found a broken builder.
    local oneshot_verdict="${1:-ok}"
    echo ""
    if [ "$oneshot_verdict" = "failed" ]; then
        echo -e "${RED}╔══════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${RED}║                                                                  ║${NC}"
        echo -e "${RED}║         ⚠  Installation Complete WITH FAILURES  ⚠                ║${NC}"
        echo -e "${RED}║                                                                  ║${NC}"
        echo -e "${RED}╚══════════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo "One or more razzfazz.init one-shot containers exited non-zero"
        echo "(see the [FAIL] lines above). Those containers build what a module"
        echo "needs, so the modules depending on them will NOT work until they"
        echo "exit 0. Everything else below is still true."
    else
        echo -e "${GREEN}╔══════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${GREEN}║                                                                  ║${NC}"
        echo -e "${GREEN}║            🎉 Installation Complete! 🎉                          ║${NC}"
        echo -e "${GREEN}║                                                                  ║${NC}"
        echo -e "${GREEN}╚══════════════════════════════════════════════════════════════════╝${NC}"
    fi
    echo ""

    local domain="${CONFIG_DOMAIN:-localhost}"

    echo "Your rzfz.ai stack is now running!"
    echo ""
    echo "Access your services at:"
    echo "  • Chat UI:       https://chat.${domain}"
    echo "  • Dify:          https://dify.${domain}"
    echo "  • LLM Manager:   https://llm.${domain}"
    echo "  • GPUStack:      https://gpustack.${domain} (only with a GPUStack profile)"
    echo "  • Auth:          https://auth.${domain}"
    echo ""
    echo "Admin Credentials:"
    echo "  • Username:      admin (or ${RAZZFAZZ_ADMIN_USERNAME:-rzfz-admin} for Authentik)"
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
            INIT_DOMAIN_EXPLICIT=true
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
        --password-file)
            # #2746: the password from a file (first line), not from argv
            [ -r "${2:-}" ] || { echo "ERROR: --password-file: cannot read '${2:-}'" >&2; exit 1; }
            IFS= read -r CONFIG_ADMIN_PASSWORD < "$2" || [ -n "$CONFIG_ADMIN_PASSWORD" ]
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
        --network-mode)
            CONFIG_NETWORK_MODE="$2"
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
        --google-domain)
            CONFIG_GOOGLE_OAUTH_DOMAIN="$2"
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
        --store-pat)
            CONFIG_PAT_MODE=true
            shift
            ;;
        --rental)
            CONFIG_PAT_MODE=false
            shift
            ;;
        --channel)
            # #602/#635 review SF1: the comments promised an explicit
            # --channel flag; now it exists (validated; wins over origin
            # detection and the preset default, #2384).
            case "$2" in internal|public) CONFIG_CHANNEL="$2" ;;
                *) print_error "--channel must be internal|public"; exit 1 ;; esac
            shift 2
            ;;
        --package)
            CONFIG_PACKAGE="$2"
            shift 2
            ;;
        --llm-experimental)
            # #1447 (cutover C7a): GPUStack 2.x is removed, so this selects
            # nothing anywhere — not even on worker-box, which was its last
            # user. It is still ACCEPTED rather than rejected: the flag stands
            # in customer runbooks and in `rzfz init` lines people copy, and
            # aborting an install over a token that no longer has meaning
            # trades a harmless no-op for a failed run. But it says so, ONCE,
            # where it was passed — an inert flag that stays silent lets the
            # operator believe they chose a runtime (#1292 review, blocker 2).
            print_warning "--llm-experimental is REMOVED in 2026.09 (#979/#1447) and selects nothing: GPUStack v2.1.x and its 'llm' profile are gone from the product. The LLM Manager is the LLM front end of every box; add the optional GPUStack 0.7.1 backend with --profiles llm-legacy (the manager federates it, #1442). Drop the flag from your install line."
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
        --skip-host-hardening)
            # Do not run scripts/harden-host.sh during this install (Step 5g).
            SKIP_HOST_HARDENING=true
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

# An operator-supplied password must satisfy every app it is set on; refuse it
# before anything is written.
if [ -n "$CONFIG_ADMIN_PASSWORD" ]; then
    if ! _pw_reason="$(password_policy_violation "$CONFIG_ADMIN_PASSWORD")"; then
        echo "ERROR: --password: ${_pw_reason}" >&2
        echo "       Use at least 8 characters with a lowercase letter, an uppercase letter and a digit, or omit the option to have one generated." >&2
        exit 1
    fi
fi

# ==============================================================================
# Apply Package Preset (if specified)
# ==============================================================================
apply_package_preset() {
    # #1292 (E3): only `worker-box` picks a GPUStack token — it joins a REMOTE
    # GPUStack master and is not an LLM front end at all. The three stack presets
    # carry ${RZFZ_LLM_MANAGER_PROFILES} instead.
    _pick_llm_profile_token() {
        # $1 = hardware token (amd | cpu | nvidia)
        # #946/#1448: NVIDIA uses GPUStack 0.7.1 + the custom CUDA llama.cpp
        # build. Since C8 that is the SAME profile as AMD's (`llm-legacy`) — the
        # hardware is carried by HARDWARE + the device overlay, not by a second
        # profile name.
        # #1447 (cutover C7a): the experimental branch is gone with the `llm`
        # profile it selected. This function now maps hardware to a profile and
        # nothing else, which is why it no longer reads any global.
        # NVIDIA is spelled out rather than left to the catch-all: #946 is the
        # rule that NVIDIA runs GPUStack 0.7.1 + the custom CUDA llama.cpp
        # build, and a rule that survives only as a fall-through is a rule the
        # next edit can lose without noticing.
        if [ "$1" = "nvidia" ]; then
            echo "llm-legacy"
            return 0
        fi
        # #1447 part b: every hardware line is `llm-legacy`; HARDWARE picks the
        # device overlay. The function is kept (rather than inlined) because
        # `worker-box` is its caller and a fourth hardware line would land here.
        echo "llm-legacy"
    }
    case "$CONFIG_PACKAGE" in
        single-box)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-amd}"
            # #2512 (operator decision 2026-09-26): the box's own GPU hosts the models.
            CONFIG_LLM_MASTER_ROLE="${CONFIG_LLM_MASTER_ROLE:-standalone}"
            print_info "Applying package preset: single-box"
            # #602: presets carry the channel — customer-shaped presets are
            # public (upgrade from the GitHub mirror, no PAT), fleet presets
            # internal (git.razzfazz.ai + PAT onboarding below).
            PRESET_CHANNEL="public"
            print_substep "Self-signed TLS, chat+dify+monitor+search+speech+docs (docling, cognee)+Gitea, LLM Manager (${RZFZ_LLM_MANAGER_PROFILES}, HARDWARE=${CONFIG_HARDWARE})"
            CONFIG_SCENARIO="${CONFIG_SCENARIO:-base}"
            CONFIG_TLS_MODE="${CONFIG_TLS_MODE:-selfsigned}"
            CONFIG_PROFILES="${CONFIG_PROFILES:-chat,dify,${RZFZ_LLM_MANAGER_PROFILES},monitor,searxng,stts,gotenberg,docling,cognee,gitea}"
            CONFIG_GPUSTACK_MODE="${CONFIG_GPUSTACK_MODE:-standalone}"
            CONFIG_TIMEZONE="${CONFIG_TIMEZONE:-Europe/Berlin}"
            SKIP_INTERACTIVE=true
            ;;
        master-cpu)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-cpu}"
            # #2512 (operator decision 2026-09-26): a CPU master serving GPU workers hosts NO models.
            CONFIG_LLM_MASTER_ROLE="${CONFIG_LLM_MASTER_ROLE:-fleet}"
            print_info "Applying package preset: master-cpu"
            # #602: presets carry the channel — customer-shaped presets are
            # public (upgrade from the GitHub mirror, no PAT), fleet presets
            # internal (git.razzfazz.ai + PAT onboarding below).
            PRESET_CHANNEL="internal"
            print_substep "Let's Encrypt TLS, chat+dify+monitor+search+speech+docs (docling, cognee), LLM Manager (${RZFZ_LLM_MANAGER_PROFILES}, HARDWARE=${CONFIG_HARDWARE})"
            CONFIG_SCENARIO="${CONFIG_SCENARIO:-google}"
            CONFIG_TLS_MODE="${CONFIG_TLS_MODE:-letsencrypt}"
            CONFIG_PROFILES="${CONFIG_PROFILES:-chat,dify,${RZFZ_LLM_MANAGER_PROFILES},monitor,searxng,stts,gotenberg,docling,cognee}"
            CONFIG_GPUSTACK_MODE="${CONFIG_GPUSTACK_MODE:-master}"
            CONFIG_TIMEZONE="${CONFIG_TIMEZONE:-Europe/Berlin}"
            SKIP_INTERACTIVE=true
            ;;
        testvm-cpu)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-cpu}"
            # #2512 (operator decision 2026-09-26): the CPU test box IS the LLM host: qwen3-4b-instruct always-on (#2158).
            CONFIG_LLM_MASTER_ROLE="${CONFIG_LLM_MASTER_ROLE:-standalone}"
            print_info "Applying package preset: testvm-cpu"
            # #602: presets carry the channel — customer-shaped presets are
            # public (upgrade from the GitHub mirror, no PAT), fleet presets
            # internal (git.razzfazz.ai + PAT onboarding below).
            PRESET_CHANNEL="internal"
            print_substep "Self-signed TLS, chat+dify+monitor+search+speech+docs (docling, cognee)+Gitea, LLM Manager (${RZFZ_LLM_MANAGER_PROFILES}, HARDWARE=${CONFIG_HARDWARE}), base auth"
            CONFIG_SCENARIO="${CONFIG_SCENARIO:-base}"
            CONFIG_TLS_MODE="${CONFIG_TLS_MODE:-selfsigned}"
            CONFIG_PROFILES="${CONFIG_PROFILES:-chat,dify,${RZFZ_LLM_MANAGER_PROFILES},monitor,searxng,stts,gotenberg,docling,cognee,gitea}"
            CONFIG_GPUSTACK_MODE="${CONFIG_GPUSTACK_MODE:-master}"
            CONFIG_TIMEZONE="${CONFIG_TIMEZONE:-Europe/Berlin}"
            SKIP_INTERACTIVE=true
            ;;
        worker-box)
            CONFIG_HARDWARE="${CONFIG_HARDWARE:-amd}"
            llm_profile=$(_pick_llm_profile_token "$CONFIG_HARDWARE")
            print_info "Applying package preset: worker-box"
            # #602: presets carry the channel — customer-shaped presets are
            # public (upgrade from the GitHub mirror, no PAT), fleet presets
            # internal (git.razzfazz.ai + PAT onboarding below).
            PRESET_CHANNEL="public"
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

# #2613: --force re-mints the secrets of whatever .env is live; never a test
# fixture left behind by a killed test-suite run.
if [ "$FORCE_REINIT" = true ] || [ "$FACTORY_DEFAULT" = true ]; then
    refuse_leftover_env_test_backups "$SCRIPT_DIR" "rzfz init --force" || exit 1
fi

# Step 0: Check system prerequisites
check_system_prerequisites

# Factory reset if requested
if [ "$FACTORY_DEFAULT" = true ]; then
    factory_reset
fi

# Step 0.5: `--force` over a previous install's volumes of another domain, or
# re-minting the secrets that encrypt their data, refuses BEFORE anything is
# written (setup_env_files already writes .env).
if [ "$FACTORY_DEFAULT" = false ] && [ "$FORCE_REINIT" = true ] && ! refuse_reinit_over_foreign_volumes; then
    exit 1
fi

# Step 1: Setup environment files
setup_env_files

# Step 1.5: Check for existing installation (safety check)
if [ "$FACTORY_DEFAULT" = false ]; then
    check_existing_installation
fi

# A box init found fresh (or wiped) is provably unprovisioned until the first
# --preset completes; post-install's guard trusts this marker, not a silent 0.
if [ "$FACTORY_DEFAULT" = true ] || [ "${EXISTING_INSTALL:-true}" = false ]; then
    (umask 077; mkdir -p backups/state \
        && date -u +%Y-%m-%dT%H:%M:%SZ > backups/state/.fresh-init.$$ \
        && mv -f backups/state/.fresh-init.$$ backups/state/fresh-init) \
        || print_warning "Could not write backups/state/fresh-init."
    rm -f backups/state/post-install-preset.done
fi

# Step 1.7: Validate no known default secrets remain
validate_no_default_secrets

# Step 2: Generate secrets (mandatory for new installations)
if [ "$REGENERATE_SECRETS" = true ]; then
    decide_db_role_passwords
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

# #2644: a re-run over a live box without --domain keeps the box's domain. The
# default used to be `localhost` here too, so pressing Enter at the prompt
# renamed the box. (`--force` without --domain over live volumes is refused
# before this point, #2115.)
_box_domain="$(current_box_domain)"
if [ -n "$_box_domain" ]; then
    DEFAULT_DOMAIN="$_box_domain"
fi

# Step 3: Interactive or CLI configuration
if init_stop_before_wizard_after_reset; then
    exit 0
fi
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
        _admin_password_generated=true
    fi
fi

# #2644: a third domain over an unfinished move is refused before anything
# domain-related is written.
if ! refuse_a_move_over_an_unfinished_one; then
    exit 1
fi

# #2697: never print a generated password — the run-log tee would keep a copy.
# After the refusal above, so a refused re-run leaves no unseen password in .env.
if [ "${_admin_password_generated:-false}" = true ]; then
    reveal_generated_admin_password "$CONFIG_ADMIN_PASSWORD"
fi

# Step 4: Apply configuration
#
# #448 class: apply_configuration REBUILDS COMPOSE_FILE from a freshly-built
# literal instead of reconciling the existing chain, so a re-init (`--force`)
# on an installed box silently dropped every overlay it does not know about —
# compose.corporate-proxy.yml (gitignored, wired by
# scripts/apply-corporate-proxy.sh), compose.registry-mirror.yml, and any
# module overlay an operator added by hand. Snapshot the chain first, then
# re-add the unmanaged entries through the additive lib.sh primitives.
_preexisting_compose_file=""
if [ -f .env ]; then
    _preexisting_compose_file="$(read_env_value .env COMPOSE_FILE 2>/dev/null || true)"
fi

apply_configuration

# Restore overlays apply_configuration's rebuild is not responsible for.
# Deliberately EXCLUDED:
#   * compose.yml and modules/llm/compose.devices.*.yml — apply_configuration
#     owns those and has just written the correct ones for this hardware;
#   * compose.worker-agent-nvidia.yml — same, hardware-derived;
#   * compose.no-build.yml — it must NOT be in the chain during the
#     install-time `docker compose build` below; ensure_nobuild_overlay wires
#     it back AFTER that build, which is the whole point of its placement.
# compose.offline.yml / compose.corporate-proxy.yml are re-reconciled later by
# ensure_network_mode_overlay, but preserving them here means the install-time
# BUILD already runs with the box's egress reality.
if [ -n "$_preexisting_compose_file" ]; then
    _restored_overlays=""
    for _ov in $(printf '%s' "$_preexisting_compose_file" | tr ':' ' '); do
        case "$_ov" in
            compose.yml|modules/llm/compose.devices.*.yml|compose.worker-agent-nvidia.yml) continue ;;
            "$RAZZFAZZ_NOBUILD_OVERLAY") continue ;;
        esac
        [ -f "$_ov" ] || continue
        if ! compose_file_overlay_present ".env" "$_ov"; then
            compose_file_overlay_add ".env" "$_ov"
            _restored_overlays="${_restored_overlays} $_ov"
        fi
    done
    if [ -n "$_restored_overlays" ]; then
        print_substep "Preserved existing compose overlays across re-init:${_restored_overlays}"
    fi
fi

# Step 4.1: record STACK_HOST_PATH (#2831). It must be the absolute repo path on
# the host so core/compose.yml's razzfazz-config bind mount sees the same path
# inside the container as the docker daemon does outside (see core/compose.yml
# comments — without it, apply_manager's `docker compose up -d` hands the daemon
# /stack/... paths it can't resolve, and the daemon auto-creates a root-owned
# directory tree that breaks later caddy/dify recreates). It was written only at
# the very end of init, so an init that aborted earlier (at the image gate,
# #2829) left a box whose Settings portal created those dirs.
# Written here, before the image gate, the build and the pull (the steps that can
# abort). update_env_value replaces in place, so the late re-assert is a no-op.
record_stack_host_path() {
    update_env_value ".env" "STACK_HOST_PATH" "$(realpath .)"
    print_substep "Recorded stack host path $(realpath .) in .env"
}
record_stack_host_path

# Step 4.5: Load baked offline images from the appliance package (#184 offline
# appliance). build-appliance-usb.sh --offline-package bakes a
# offline image package (Enterprise) onto the ISO, and
# the autoinstall installer copies it to $APPLIANCE_OFFLINE_PKG. Firstboot leaves it
# untouched — `rzfz init` is the SINGLE consumer that loads its images/ subtree into
# the local docker cache so an OFFLINE first install provisions with ZERO downloads.
#
# Ordering (deliberate): this runs AFTER the existing-install guard (Step 1.5, so a
# `docker load` can never be misread as live state and trip it) and AFTER .env is
# written (apply_configuration, above), but BEFORE the docker compose up/build phase
# (Step 5 + the compose up further down). On a successful load we set SKIP_BUILD=true
# so Step 5 takes the exact --skip-build path — skipping BOTH `docker compose pull`
# AND `docker compose build` — and init never reaches a registry on an air-gapped
# box. The package carries the FULL image set (the Enterprise offline image package
# builds every custom image + pulls every remote/runtime-only image across all
# profiles), so nothing is left to build/pull once it's loaded.
#
# Non-fatal + idempotent (docker load of an already-present image is a no-op),
# mirroring cli/upgrade.sh's --package image-load. The package is RETAINED (never
# deleted): `rzfz post-install` stages its models/ GGUFs into the gpustack-data
# volume after gpustack is up (stage_baked_appliance_models — images are init's half,
# models are post-install's), and a future offline `rzfz upgrade --package` can reuse
# the same file. Harmless on an ONLINE box: images just get pre-seeded and we only
# force skip-build for this one run (no other offline behaviour is forced here).
APPLIANCE_OFFLINE_PKG="${APPLIANCE_OFFLINE_PKG:-/opt/razzfazz-appliance/razzfazz-offline.tar.gz}"
# #1478's appliance_archive_images_present lives in scripts/lib.sh since #271/#2120
# (id-based, shared with the offline upgrade).

# #1478: the loader as ONE function (it used to be inline), so the single-pass
# extraction and the present-image skip can be driven by a test against a
# miniature package.
# #2006 part 2 — the zero-download skip is decided against THIS tree, not the package.
#
# Measured on 0.175 (rc2 sweep, 2026-09-14, razzfazz-init-20260914T084731Z.log):
#   line 177  Loaded 79 baked image(s), expected set COMPLETE — skipping docker build/pull
#   line 355  Container gpustack  No such image: razzfazz-gpustack:vulkan
# The ga.15 package carried gpustack only under the pre-#270 registry name; its own
# expected-images.json listed that name, every one of those was present, so the
# verdict was COMPLETE — and false for the tree being installed. Scenario 18 died
# the same way on the agents' images at last release's tags. The package's
# manifest stays as a diagnostic of the STICK; the SKIP is decided by asking the
# tree's compose render what `up` will demand and, for each custom image, whether
# the image present is this tree's build (build record or the package's digest).
#
# Prints one TSV row (image, kind, verdict, detail) per custom image that is
# MISSING or STALE; empty = nothing blocks the skip. Fails CLOSED: a helper that
# cannot answer yields an `unverifiable` row (scripts/lib.sh).
appliance_tree_demand_check() {
    local manifest="$1" profiles="$2"
    if [ -z "$profiles" ]; then
        printf '(all custom images)\tcustom\tunverifiable\tCOMPOSE_PROFILES unknown at this point of init — cannot say what the tree demands\n'
        return 0
    fi
    razzfazz_custom_images_not_from_this_tree "$profiles" "$manifest"
}

# The skip decision itself, in one place so it can be driven by a test.
# Args: load-failures loaded-count expected-images.json-path missing-vs-manifest blocking-rows
# Sets SKIP_BUILD=true ONLY when every input says complete: no load failure, at
# least one image loaded, the package's expected-images.json present and fully
# satisfied (#425), and nothing THIS tree demands missing or stale (#2006 part 2).
appliance_skip_decision() {
    local appl_fail="$1" appl_n="$2" appl_expected="$3" appl_missing="$4" appl_block="$5"
    if [ "$appl_fail" -eq 0 ] && [ "$appl_n" -gt 0 ] && [ -f "$appl_expected" ] && [ -z "$appl_missing" ] && [ -z "$appl_block" ]; then
        print_success "Loaded ${appl_n} baked image(s), expected set COMPLETE — and every custom image this tree demands is this tree's build — skipping docker build/pull for this run."
        # Reuse the exact --skip-build gate (Step 5, below): skips BOTH
        # `docker compose pull` and `docker compose build`, so init never
        # reaches a registry on an air-gapped box.
        SKIP_BUILD=true
    elif [ "$appl_n" -gt 0 ]; then
        print_warning "Loaded ${appl_n} baked image(s) but the set is INCOMPLETE (${appl_fail} load failures$( [ -n "$appl_missing" ] && echo "; missing vs expected-images.json: $(echo "$appl_missing" | head -5 | tr '\n' ' ')…")$( [ ! -f "$appl_expected" ] && echo "; package carries no expected-images.json")) — leaving build/pull ENABLED so an online box can heal itself (#425)."
        if [ -n "$appl_block" ]; then
            print_warning "  Custom images THIS tree demands that are not its build (#2006 part 2) — the package predates the tree or never carried them:"
            printf '%s\n' "$appl_block" | awk -F'\t' 'NF{printf "    %s  [%s]  %s\n", $1, $3, $4}' >&2
        fi
        print_warning "On an air-gapped box this means the STICK is incomplete or outdated — rebuild the package from THIS tree; 'rzfz verify-images' shows the full gap."
        # #2167: a healing build/pull can only exist where a registry answers.
        # Fail closed where it cannot — the message above is the way out — and
        # say why the build stays enabled where it can.
        if razzfazz_is_offline; then
            print_error "RAZZFAZZ_NETWORK_MODE=offline and the stick is incomplete for this tree — there is no registry to heal from. Refusing to start a build/pull that cannot succeed (#2167). Obtain a complete offline package for THIS tree from your razzfazz.ai contact (Enterprise) and re-run init."
            return 1
        fi
        if ! razzfazz_registry_reachable; then
            print_error "No container registry answers from this box (probe timed out) and the stick is incomplete for this tree — refusing to start a build/pull that cannot succeed (#2167). Either obtain a complete offline package for THIS tree from your razzfazz.ai contact (Enterprise), or give the box a network and re-run init."
            return 1
        fi
        print_warning "  A registry answers from this box — leaving build/pull ENABLED so this online box can heal the gap (#425)."
    else
        print_warning "No baked images loaded — leaving build/pull enabled (init will build/pull as usual)."
    fi
}

# #3003: exit 0 when every tag in ARCHIVE is present locally under an image the
# box itself RECORDED (its own build, or a load from a later package) — then the
# stick's copy must not replace it. The stick at /opt/razzfazz-appliance is the
# install medium and stays at the release it was made from; on every later
# `rzfz init` it re-loaded its older builds over the box's newer ones (the
# disabled modules' images were never rebuilt, and the next code-only package
# upgrade refused them as stale). A tag that is absent, or present under an id
# the box never recorded, still loads from the stick (it heals or adopts).
appliance_archive_kept_local() {
    local archive="$1" py tags tag local_id any=0
    py="$(razzfazz_image_provenance_py)"
    [ -f "$py" ] || return 1
    tags="$(tar -xOf "$archive" manifest.json 2>/dev/null | python3 -c '
import json, sys
try:
    for m in json.load(sys.stdin):
        for t in (m.get("RepoTags") or []):
            print(t)
except Exception:
    pass
' 2>/dev/null)" || tags=""
    [ -n "$tags" ] || return 1
    while IFS= read -r tag; do
        [ -n "$tag" ] || continue
        any=1
        local_id="$(docker image inspect --format '{{.Id}}' "$tag" 2>/dev/null || true)"
        [ -n "$local_id" ] || return 1
        python3 "$py" --stack-root "${SCRIPT_DIR:-.}" --trusted-id "$tag" --image-id "$local_id" >/dev/null 2>&1 || return 1
    done <<< "$tags"
    [ "$any" -eq 1 ]
}

# #3201: will the images fit? A stick install extracts the package's image
# archives and loads them into Docker; on the full 2026.11-rc10 stick that is
# ~160 GB of archives, and a 400 GB disk (with the 76 GB package kept on it for
# post-install's models) filled up at image 69 of 105. Asked BEFORE extracting.
# The archives are deleted as they are loaded (below), so the peak on one disk is
# the images' size plus the largest archive (loaded once, deleted after) plus
# room to run. Sizes from the package's own manifest (`images_bytes`,
# `largest_image_bytes`, written by an offline package (Enterprise)); an older package: its size ×
# 2.1, the ratio of the rc10 stick. Unmeasurable (no df) = no verdict.
# Returns 1 with the message printed when the disk cannot hold them.
appliance_disk_preflight() {
    local manifest="$1" base="$2" pkg="$3" img_kb="" big_kb=0 src head_kb droot tmp_dev dock_dev tmp_av dock_av
    if [ -s "$manifest" ]; then
        read -r img_kb big_kb < <(python3 -c 'import json, sys
m = json.load(open(sys.argv[1]))
a, b = m.get("images_bytes"), m.get("largest_image_bytes") or 0
ok = lambda v: isinstance(v, int) and v > 0
print((a + 1023) // 1024 if ok(a) else "-", (b + 1023) // 1024 if ok(b) else 0)' "$manifest" 2>/dev/null) || true
    fi
    src="the package's own figures"
    case "$img_kb" in ''|*[!0-9]*) img_kb="" ;; esac
    if [ -z "$img_kb" ]; then
        img_kb=$(( $(du -k "$pkg" 2>/dev/null | cut -f1 || echo 0) * 21 / 10 ))
        src="estimated from the package size"
    fi
    case "$big_kb" in ''|*[!0-9]*) big_kb=0 ;; esac
    head_kb=$((10 * 1024 * 1024))
    droot="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
    [ -n "$droot" ] && [ -d "$droot" ] || droot=/var/lib/docker
    [ -d "$droot" ] || droot="$base"
    tmp_dev="$(df -P "$base" 2>/dev/null | awk 'NR==2{print $1}')"
    tmp_av="$(df -Pk "$base" 2>/dev/null | awk 'NR==2{print $4}')"
    dock_dev="$(df -P "$droot" 2>/dev/null | awk 'NR==2{print $1}')"
    dock_av="$(df -Pk "$droot" 2>/dev/null | awk 'NR==2{print $4}')"
    case "$tmp_av$dock_av" in ''|*[!0-9]*) return 0 ;; esac
    if [ "$tmp_dev" = "$dock_dev" ]; then
        _appliance_disk_short "$((img_kb + big_kb + head_kb))" "$tmp_av" "$base" "$src" || return 1
    else
        _appliance_disk_short "$((img_kb + head_kb))" "$tmp_av" "$base" "$src" || return 1
        _appliance_disk_short "$((img_kb + head_kb))" "$dock_av" "$droot" "$src" || return 1
    fi
    return 0
}

_appliance_disk_short() {    # need_kb avail_kb where source
    local need_g=$(( ($1 + 1048575) / 1048576 )) have_g=$(( $2 / 1048576 ))
    [ "$2" -lt "$1" ] || return 0
    print_error "Not enough disk space to load the stick's images: they need about ${need_g} GiB ($4) and ${have_g} GiB is free on $3."
    print_error "  This is not a fault of the stick. Install on a larger disk, or free space on $3, then run 'rzfz init' again."
    return 1
}

appliance_load_baked_images() {
    # #781: the stick's package is checked against a detached signature that is
    # not inside it before anything is read from it. A failed check stops init.
    razzfazz_appliance_package_gate "$APPLIANCE_OFFLINE_PKG" || return 1
    print_step "Appliance: loading baked offline images from ${APPLIANCE_OFFLINE_PKG} (zero-download install)..."
    # Transient-space check: extracting images/ needs roughly the images subtree
    # size in /var/tmp (the package size is a conservative upper bound). Soft
    # warning only — let it try, then fall back to build/pull if the load fails.
    # #1729: TMPDIR first, /var/tmp as the default. `/var/tmp` is the right
    # DEFAULT — `/tmp` is a RAM-backed tmpfs on most boxes and this extracts
    # tens of GB — but hard-coding it fails on a box that mounts /var/tmp
    # read-only or noexec, which is a common hardening (and is exactly what
    # this repo's own test sandbox does). Honouring TMPDIR is the standard
    # contract and changes nothing where it is unset.
    appl_tmp_base="${TMPDIR:-/var/tmp}"
    appl_avail_kb="$(df -Pk "$appl_tmp_base" 2>/dev/null | awk 'NR==2{print $4}')" || true
    appl_pkg_kb="$(du -k "$APPLIANCE_OFFLINE_PKG" 2>/dev/null | cut -f1)" || true
    if [ -n "$appl_avail_kb" ] && [ -n "$appl_pkg_kb" ] && [ "$appl_avail_kb" -lt "$appl_pkg_kb" ]; then
        print_warning "Low free space in ${appl_tmp_base} ($((appl_avail_kb/1024/1024)) GiB) for extracting baked images (~up to $((appl_pkg_kb/1024/1024)) GiB) — load may fail; init would then fall back to build/pull."
    fi
    appl_tmp="$(mktemp -d "${appl_tmp_base}/rzfz-appliance-images.XXXXXX" 2>/dev/null)" || appl_tmp=""
    if [ -n "$appl_tmp" ]; then
        # #428: consult the one-scan index instead of the blind two-attempt
        # extract (gzip is unseekable — each miss was a full pass over 63 GB,
        # and tar normalizes ./ so the second attempt could never succeed
        # where the first failed).
        appl_idx="$(appliance_pkg_index "$APPLIANCE_OFFLINE_PKG")" || appl_idx=""
        appl_prefix=""; appl_exp_prefix=""
        [ -n "$appl_idx" ] && appl_prefix="$(appliance_pkg_prefix "$appl_idx" images)" || true
        # #1478: the manifest is a member of the same archive — take it in the
        # SAME pass. gzip is unseekable: a separate `tar -xzOf` for the one
        # small file was a third full decompression of the whole package.
        [ -n "$appl_idx" ] && appl_exp_prefix="$(appliance_pkg_prefix "$appl_idx" expected-images.json)" || true
        # #2120: decide BEFORE extracting 155 GB whether anything needs loading.
        # The manifest is one small member near the start of the archive; from
        # it (image ids) plus this tree's own demand (#2006 part 2) the answer
        # is known without touching images/. rc 0 = nothing to load; rc 1 = some
        # (extract as before); rc 2 = cannot decide (old package without
        # image_ids — extract as before).
        appl_profiles="$(init_resolved_profiles)"
        appl_pre_rc=2; appl_pre_rows=""
        if [ -n "$appl_prefix" ] && [ -n "$appl_exp_prefix" ] \
           && appliance_extract_manifest "$APPLIANCE_OFFLINE_PKG" "$appl_tmp/expected-images.json" "$appl_exp_prefix"; then
            # `|| rc=$?`, never `; rc=$?`: this script runs `set -eo pipefail`, and a
            # bare assignment inherits the substitution's status — the helper returns
            # 2 for "cannot decide" BY DESIGN, and that killed every install from a
            # stick built before image_ids existed (0.175, Journey B, 2026-09-15;
            # the same #755/#793 shape as the symlink guard below).
            # #3201: this box's hardware line only (the others stay in the package).
            appl_hw="${CONFIG_HARDWARE:-$(read_env_value .env HARDWARE 2>/dev/null || true)}"
            appl_hw_skip="$(appliance_hardware_filter "$appl_tmp/expected-images.json" "$appl_hw")" || appl_hw_skip=""
            if [ -n "$appl_hw_skip" ]; then
                print_substep "HARDWARE=${appl_hw}: $(printf '%s\n' "$appl_hw_skip" | grep -c .) image archive(s) of the other hardware lines stay in the package, not loaded."
            fi
            appl_pre_rc=0
            appl_pre_rows="$(appliance_package_needs_loading "$appl_tmp/expected-images.json" "$appl_profiles")" || appl_pre_rc=$?
        fi
        # #3201: a stick that cannot fit is refused here, with the disk named —
        # not half-way through the load, and never as an "incomplete stick".
        if [ "$appl_pre_rc" -ne 0 ] && [ -n "$appl_prefix" ] \
           && ! appliance_disk_preflight "$appl_tmp/expected-images.json" "$appl_tmp_base" "$APPLIANCE_OFFLINE_PKG"; then
            rm -rf "$appl_tmp"
            exit 1
        fi
        if [ "$appl_pre_rc" -eq 0 ]; then
            appl_n="$(python3 -c 'import json,sys; print(len((json.load(open(sys.argv[1])).get("image_ids") or {})))' "$appl_tmp/expected-images.json" 2>/dev/null || echo 0)"
            print_success "Every image this package carries is already present under the package's own image id, and every custom image this tree demands is this tree's build — nothing extracted, nothing loaded (#271/#2120)."
            appliance_skip_decision 0 "$appl_n" "$appl_tmp/expected-images.json" "" ""
        elif [ -n "$appl_prefix" ] && appl_excl=() \
             && { [ -z "${appl_hw_skip:-}" ] || mapfile -t appl_excl < <(printf '%s\n' "$appl_hw_skip" | awk 'NF{print "--exclude=images/" $0}'); } \
             && tar xzf "$APPLIANCE_OFFLINE_PKG" -C "$appl_tmp" "${appl_excl[@]}" "$appl_prefix" ${appl_exp_prefix:+"$appl_exp_prefix"} 2>/dev/null \
             && { [ -z "${appl_hw_skip:-}" ] || appliance_hardware_filter "$appl_tmp/expected-images.json" "$appl_hw" >/dev/null; }; then
            if [ "$appl_pre_rc" -eq 1 ]; then
                print_substep "Images to load from the package: $(printf '%s\n' "$appl_pre_rows" | razzfazz_distinct_image_refs | grep -c .) distinct image(s) (#2221):"
                printf '%s\n' "$appl_pre_rows" | razzfazz_distinct_image_refs | sed 's/^/      /'
            fi
            # #782: the appliance package arrives on the SAME physical media as
            # the offline upgrade package, so it carries the same risk #755
            # closed there — a member that IS a symlink puts its danger in the
            # link TARGET, where no name check looks, and tar extracts it with
            # exit 0 and an empty stderr.
            #
            # Operator decision 2026-08-26: DISCARD the payload, do not abort.
            # This step is best-effort with a fallback, and a hard exit would
            # let a prepared stick block an installation rather than merely
            # fail to poison it. Dropping the tree makes the #425 completeness
            # gate below see zero loaded images, so the run falls back to
            # build/pull — which is exactly the "could not use the package"
            # path that already exists.
            # Tri-state (0 found / 1 clean / 2 could not look) and this script
            # runs `set -eo pipefail`, so a bare assignment would inherit the
            # CLEAN status and kill the install here. That regression shipped
            # once already, in #755 (see #793) — capture the status explicitly.
            # rc=2 discards too: "could not look" is not "it is fine", and the
            # operator decision for this site is discard-not-abort either way.
            appl_bad=""; appl_link_rc=0
            appl_bad="$(razzfazz_find_escaping_links "$appl_tmp")" || appl_link_rc=$?
            if [ -n "$appl_bad" ] || [ "$appl_link_rc" -eq 2 ]; then
                [ -n "$appl_bad" ] || appl_bad="(could not inspect ${appl_tmp})"
                print_warning "Baked appliance package contains symlinks pointing OUTSIDE the package — discarding its images (#782):"
                printf '%s\n' "$appl_bad" | sed "s|^${appl_tmp}/|    |" >&2
                print_warning "  Falling back to docker build/pull. The package is not trustworthy; do not reuse this medium."
                rm -rf "${appl_tmp:?}/images" 2>/dev/null || true
            fi
            appl_n=0; appl_fail=0; appl_present=0; appl_kept=""
            # #1478: `docker load` of an already-present image is a no-op for the
            # image store but still reads the whole archive — ~100 archives,
            # 155 GB, on a box that had every image. Judge each archive by the
            # RepoTags in its manifest.json (small, uncompressed, read without
            # unpacking) against the local image list and skip the present ones.
            appl_have="$(docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null)"
            # #3201: an archive is deleted as soon as it is done with (loaded, or
            # skipped as present), so the extracted archives and the loaded images
            # no longer pile up on one disk. A load that fails with "no space left
            # on device" stops here: the disk is full, the stick is not at fault.
            appl_total=0; appl_nospace=0
            for appl_img in "$appl_tmp"/images/*.tar; do [ -e "$appl_img" ] && appl_total=$((appl_total+1)); done
            for appl_img in "$appl_tmp"/images/*.tar; do
                [ -e "$appl_img" ] || break
                if appliance_archive_images_present "$appl_img" "$appl_have"; then
                    appl_present=$((appl_present+1))
                    rm -f "$appl_img"
                    continue
                fi
                if appliance_archive_kept_local "$appl_img"; then
                    appl_present=$((appl_present+1))
                    appl_kept="${appl_kept:+$appl_kept }$(basename "$appl_img" .tar)"
                    rm -f "$appl_img"
                    continue
                fi
                appl_err=""
                if appl_err="$(docker load -i "$appl_img" 2>&1 >/dev/null)"; then
                    appl_n=$((appl_n+1))
                    rm -f "$appl_img"
                else
                    case "$appl_err" in
                        *[Nn]"o space left on device"*) appl_nospace=1; break ;;
                    esac
                    appl_fail=$((appl_fail+1))
                    # #3201: docker's own words go into the log; a bare name said nothing.
                    print_warning "  image load FAILED: $(basename "$appl_img"): $(printf '%s\n' "$appl_err" | grep -v '^[[:space:]]*$' | tail -n 1 | cut -c1-300)"
                fi
            done
            if [ "$appl_nospace" -eq 1 ]; then
                print_error "The disk filled up while loading the stick's images ($((appl_n + appl_present)) of ${appl_total} done): no space left on device."
                print_error "  This is not a fault of the stick. Install on a larger disk, or free space, then run 'rzfz init' again."
                rm -rf "$appl_tmp"
                exit 1
            fi
            [ "$appl_n" -eq 0 ] || print_substep "${appl_n} image archive(s) loaded and removed after loading."
            [ "$appl_present" -gt 0 ] && print_substep "${appl_present} baked image archive(s) already present locally — not re-loaded (#1478)."
            [ -z "$appl_kept" ] || print_substep "Kept this box's own recorded build instead of the stick's copy: ${appl_kept}"
            appl_n=$((appl_n + appl_present))
            # #2168: what the stick put there is the record from now on — refs
            # present under the PACKAGE's image id are recorded as package
            # builds, replacing a previous install's record on this box (the
            # file lives outside the stack dir; no flatten touches it).
            if [ -s "$appl_tmp/expected-images.json" ]; then
                razzfazz_record_custom_image_builds "" package "$appl_tmp/expected-images.json"
                razzfazz_retag_digest_pins "$appl_tmp/expected-images.json"
            fi
            # #425: SKIP_BUILD only on COMPLETENESS, not on "at least one
            # loaded" — 0.236 loaded 35 of 78, skipped build/pull, and ran
            # for days missing 29 images incl. custom razzfazz-* ones. The
            # gate is the package's own expected-images.json.
            appl_expected="$appl_tmp/expected-images.json"
            # #1478: normally already on disk from the single-pass extraction
            # above; the separate package read is only the fallback.
            if [ ! -s "$appl_expected" ] && [ -n "$appl_exp_prefix" ]; then
                tar -xzOf "$APPLIANCE_OFFLINE_PKG" "$appl_exp_prefix" > "$appl_expected" 2>/dev/null || rm -f "$appl_expected"
            fi
            appl_missing=""
            if [ -f "$appl_expected" ]; then
                # #1481: a tag-less ref means :latest. #2829: a digest-pinned ref
                # (repo:tag@sha256:) never appears in `docker image ls`, because
                # `docker load` drops the digest; the string compare that stood
                # here judged every package carrying one INCOMPLETE (104 loaded,
                # 0 failed, then an offline build/pull and rc 1). The resolver
                # verify-images and the upgrade use decides.
                appl_missing="$(razzfazz_expected_images_missing "$appl_expected" 2>/dev/null)" \
                    || appl_missing="(gate errored)"
            fi
            # #2006 part 2: the package's manifest can only say the package is
            # complete relative to ITSELF. Ask what THIS tree's compose will
            # demand for the profiles this init runs, and whether each custom
            # image present is this tree's build.
            appl_block=""
            appl_block="$(appliance_tree_demand_check "$appl_expected" "$appl_profiles")" || true
            # #2167: the decision may refuse (offline, or no registry answers) —
            # stop here with its message rather than reach for a registry.
            appliance_skip_decision "$appl_fail" "$appl_n" "$appl_expected" "$appl_missing" "$appl_block" || exit 1
        elif [ -n "$appl_idx" ] && [ -z "$appl_prefix" ]; then
            print_info "  Baked package carries no images/ subtree (index consulted, no scan wasted) — leaving build/pull enabled."
        else
            print_warning "Could not index/extract images/ from ${APPLIANCE_OFFLINE_PKG} — init will build/pull images as usual."
        fi
        rm -rf "$appl_tmp"
    else
        # #1729: "as usual" was the wrong word, and on the box this feature
        # exists for it was the wrong ADVICE. The whole point of the appliance
        # package is a zero-download install; falling back to build/pull needs
        # a network, which an air-gapped box does not have. Without this the
        # run failed much later, at the registry, with an error pointing at the
        # wrong thing.
        print_warning "Cannot create a temp dir in ${appl_tmp_base} — skipping the baked-image load."
        print_warning "  This box will now try to BUILD/PULL images, which needs network access."
        print_info    "  On an air-gapped/appliance install that will fail later at the registry."
        print_info    "  Fix: make ${appl_tmp_base} writable, or set TMPDIR to a writable path with"
        print_info    "  room for the images subtree, and re-run."
    fi
    # NB: the package is intentionally NOT deleted here — see the ordering note
    # above (post-install stages models/, future offline upgrade reuses the file).
}

# The ONLY gate is that the file exists — no network-mode check, on purpose (an
# appliance is right to load its package even when it happens to be online).
# For TESTING the online path on a box that carries a package, point the
# variable at a path that does not exist:  APPLIANCE_OFFLINE_PKG=/nonexistent
# rzfz init …  — tests/test-matrix.sh does exactly this for `network: online`
# runs (#2006), and docs/enterprise/how-to/offline-install.md documents it.
if [ -f "$APPLIANCE_OFFLINE_PKG" ] && command -v docker >/dev/null 2>&1; then
    appliance_load_baked_images
fi

# #2006: an install that skips the build adopts whatever custom images the docker
# cache holds — on 0.79 a 2026.09-rc1 install ran on `:latest` images dated between
# June and September, none from the tree being installed, and nothing said so. The
# decision not to build is where that has to be visible: one line per custom image,
# tag + build date + id, so a log reader can tell an adopted image from a built one.
# Custom images are the `razzfazz-*` refs of this box's compose render (the naming
# convention the catalog and the packager rely on).
report_adopted_custom_images() {
    local refs ref out
    refs="$(docker compose config --images 2>/dev/null | grep -E '^razzfazz-' | sort -u)" || refs=""
    if [ -z "$refs" ]; then
        print_warning "  Could not enumerate the custom images this box adopts (compose render failed or names no razzfazz-* image) — the adoption is UNRECORDED (#2006)."
        return 0
    fi
    # #2006 part 2: the date says WHEN, not FROM WHICH TREE — a cached rebuild
    # keeps the old date, an old image carries the right tag. Add the provenance
    # verdict per image (this-build / package-build / stale / unknown).
    local verdicts vline
    verdicts="$(razzfazz_custom_image_verdicts "$(init_resolved_profiles)")" || verdicts=""
    print_substep "Custom images adopted WITHOUT a build in this run (#2006) — tag, build date, id, provenance:"
    while IFS= read -r ref; do
        [ -n "$ref" ] || continue
        if out="$(docker image inspect --format '{{.Created}} {{.Id}}' "$ref" 2>/dev/null)"; then
            vline="$(printf '%s\n' "$verdicts" | awk -F'\t' -v r="$(razzfazz_canonical_image_ref "$ref")" '$1==r{printf "%s (%s)", $3, $4; exit}')"
            print_substep "  adopted ${ref}  built ${out:0:19}  $(printf '%s' "$out" | awk '{print substr($2, 8, 12)}')  provenance: ${vline:-unverified}"
        else
            print_warning "  ${ref}: NOT present locally — 'docker compose up' cannot create it and the build was skipped."
        fi
    done <<<"$refs"
    if printf '%s\n' "$verdicts" | awk -F'\t' '$3=="stale"{f=1} END{exit !f}'; then
        print_warning "  At least one adopted image is STALE: present under the right tag, but not built from this tree (#2006 part 2). Online: 'rzfz post-install --refresh' rebuilds it. Offline: the package predates the tree."
    fi
}

# Step 5: Build and pull images
if [ "$SKIP_BUILD" = false ]; then
    build_and_pull_images
else
    print_warning "Skipping docker build/pull (--skip-build flag)"
    report_adopted_custom_images
fi

# Step 5b: Prepare Matrix (Synapse) configuration if matrix profile is enabled
if init_profile_enabled matrix; then
    print_step "Preparing Synapse Matrix homeserver configuration..."
    # #2470: one render for init and `rzfz module prepare|enable matrix`.
    matrix_render_config "$(pwd)" || {
        print_error "init aborted at the matrix step: the homeserver config was not rendered (see above). Fix the matrix values in .env, then re-run 'rzfz init'."
        exit 1
    }

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
        if razzfazz_sudo cp "$SYSCTL_SRC" "$SYSCTL_DST" 2>/dev/null && razzfazz_sudo sysctl --system >/dev/null 2>&1; then
            print_substep "Installed $SYSCTL_DST + reloaded."
            print_substep "Active: vm.panic_on_oom=$(sysctl -n vm.panic_on_oom 2>/dev/null), kernel.panic=$(sysctl -n kernel.panic 2>/dev/null), vm.swappiness=$(sysctl -n vm.swappiness 2>/dev/null)"
        else
            print_warning "Could not install $SYSCTL_DST (sudo required). Apply manually:"
            print_info "  sudo cp $SYSCTL_SRC $SYSCTL_DST && sudo sysctl --system"
            razzfazz_defer_host_step "Kernel stability tunables NOT installed ($SYSCTL_DST)" "sudo cp $SYSCTL_SRC $SYSCTL_DST && sudo sysctl --system"
        fi
    fi
fi

# Step 5d.1: the Settings portal's Time source field. The portal queues a request;
# a systemd path unit applies it as root (scripts/time-source-watcher.sh).
TS_WATCHER="${SCRIPT_DIR:-$(pwd)}/scripts/install-time-source-watcher.sh"
if [ -x "$TS_WATCHER" ] && command -v systemctl >/dev/null 2>&1; then
    if razzfazz_sudo "$TS_WATCHER" >/dev/null 2>&1; then
        print_substep "Time source watcher installed (razzfazz-time-source.path)."
    else
        print_warning "Could not install the time source watcher (sudo required)."
        razzfazz_defer_host_step "Time source watcher NOT installed (the Time source field of the portal applies by hand)" "sudo $TS_WATCHER"
    fi
fi

# Step 5d.2: the hourly knowledge-scope and role reconcile (Dify, cognee,
# Synapse) as a systemd timer, so a removal from a knowledge scope takes
# effect within the hour (scripts/scope-reconcile-run.sh).
SCOPE_TIMER="${SCRIPT_DIR:-$(pwd)}/scripts/install-scope-reconcile-timer.sh"
if [ -x "$SCOPE_TIMER" ] && command -v systemctl >/dev/null 2>&1; then
    if razzfazz_sudo "$SCOPE_TIMER" >/dev/null 2>&1; then
        print_substep "Hourly knowledge-scope reconcile installed (razzfazz-scope-reconcile.timer)."
    else
        print_warning "Could not install the hourly knowledge-scope reconcile (sudo required)."
        razzfazz_defer_host_step "Hourly knowledge-scope reconcile NOT installed (a removal from a knowledge scope applies only at the next upgrade or manual reconcile)" "sudo $SCOPE_TIMER"
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
    strix_tmp=$(mktemp "${RZFZ_LOG_DIR:-${TMPDIR:-/tmp}}/50-razzfazz-strix.XXXXXX.cfg" 2>/dev/null) || strix_tmp=$(mktemp)  # kept on failure: off /tmp, survives the reboot
    cat > "$strix_tmp" <<EOF
# razzfazz.ai — AMD Strix Halo (gfx1151) amdgpu stability tunables (M034 S04).
# Sizes the TTM/GTT pool to RAM so large-model load doesn't trigger the kernel
# 6.17+/7.0 kworker D-state storm. pages_limit = RAM_KB/4 (full RAM, 4 KiB
# pages); page_pool_size = half. grub.d drop-ins are sourced AFTER
# /etc/default/grub, so this appends to the existing cmdline.
GRUB_CMDLINE_LINUX_DEFAULT="\$GRUB_CMDLINE_LINUX_DEFAULT amdgpu.cwsr_enable=0 amd_iommu=off ttm.pages_limit=${STRIX_PAGES} ttm.page_pool_size=${STRIX_HALF}"
EOF
    # #2007: `|| true` is load-bearing. init.sh runs under `set -eo pipefail`,
    # so a sudo that cannot authenticate (no TTY, no NOPASSWD, no cached
    # credential) ended the whole install here — after every image was built —
    # with 2>/dev/null having already swallowed sudo's own explanation. The
    # three branches below are written to degrade to a warning on exactly that
    # failure; without this the warning was unreachable. Measured on 0.79.
    razzfazz_sudo mkdir -p /etc/default/grub.d 2>/dev/null || true
    if [ -f "$STRIX_GRUB_DST" ] && razzfazz_sudo cmp -s "$strix_tmp" "$STRIX_GRUB_DST" 2>/dev/null; then
        print_substep "Already installed and current ($STRIX_GRUB_DST)."
    elif razzfazz_sudo cp "$strix_tmp" "$STRIX_GRUB_DST" 2>/dev/null && razzfazz_sudo update-grub >/dev/null 2>&1; then
        print_substep "Wrote $STRIX_GRUB_DST (ttm.pages_limit=${STRIX_PAGES}, page_pool_size=${STRIX_HALF}); effective next reboot."
    else
        print_warning "Could not write Strix Halo GRUB tunables (sudo required). Apply manually:"
        print_info "  sudo cp $strix_tmp $STRIX_GRUB_DST && sudo update-grub"
        razzfazz_defer_host_step "Strix Halo GRUB kernel parameters NOT applied ($STRIX_GRUB_DST; then reboot)" "sudo cp $strix_tmp $STRIX_GRUB_DST && sudo update-grub && sudo reboot"
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

# HOST_HARDENING-BEGIN
# Step 5g: a fresh install hardens its host like an upgrade does. BEFORE the
# stack starts: harden-host restarts the Docker daemon and refuses a live stack
# unattended. No terminal and no grant defers (never a hang, never a failed
# init); the final summary repeats it. --skip-host-hardening opts out.
# The summary is read after Step 6, on a LIVE stack: plain harden-host then
# refuses (or asks), and Docker applies its no-new-privileges default only to
# containers it creates afterwards. So: the stack down (harden-host then takes
# its full path, Docker restart included), harden, and start = recreate.
_hh_cmd="cd ${SCRIPT_DIR} && ./rzfz down && sudo bash ${SCRIPT_DIR}/scripts/harden-host.sh && ./rzfz start"
_hh_check="cd ${SCRIPT_DIR} && ./rzfz status | grep F-059"
_hh_onfail="If harden-host fails, the stack stays down: run ./rzfz start, fix the cause from the log, and repeat."
_hh_live="the stack is running now — do it in a maintenance window: the stack goes down so every container is recreated under the new Docker defaults"
if [ "${SKIP_HOST_HARDENING:-false}" = true ]; then
    print_substep "Host hardening skipped (--skip-host-hardening). Later: sudo bash ${SCRIPT_DIR}/scripts/harden-host.sh"
    razzfazz_defer_host_step "Host hardening skipped (--skip-host-hardening); ${_hh_live}" "$_hh_cmd" "$_hh_check" "$_hh_onfail"
elif ! razzfazz_run_host_hardening "$SCRIPT_DIR"; then
    case "${RAZZFAZZ_HOST_HARDENING_STATE:-}" in
        deferred)     razzfazz_defer_host_step "Host hardening DEFERRED (no terminal and no passwordless sudo); ${_hh_live}" "$_hh_cmd" "$_hh_check" "$_hh_onfail" ;;
        refused-live) razzfazz_defer_host_step "Host hardening REFUSED: a stack was already running (log: ${RAZZFAZZ_HOST_HARDENING_LOG}); ${_hh_live}" "$_hh_cmd" "$_hh_check" "$_hh_onfail" ;;
        failed)       razzfazz_defer_host_step "Host hardening FAILED (log: ${RAZZFAZZ_HOST_HARDENING_LOG}); ${_hh_live}" "$_hh_cmd" "$_hh_check" "$_hh_onfail" ;;
        *)            razzfazz_defer_host_step "Host hardening NOT applied (scripts/harden-host.sh missing; restore it from the release first)" "$_hh_cmd" "" "$_hh_onfail" ;;
    esac
fi
unset _hh_cmd _hh_check _hh_live _hh_onfail
# HOST_HARDENING-END

# METADATA_BLOCK-BEGIN
# Step 5h: block cloud metadata addresses for every container (DOCKER-USER).
# Runs even when host hardening is skipped or deferred: it touches only one
# iptables chain and needs no Docker restart. Without a terminal and without
# passwordless sudo it is deferred and repeated in the final summary.
if ! razzfazz_apply_metadata_block "$SCRIPT_DIR"; then
    razzfazz_defer_host_step "Cloud metadata block NOT applied (${RAZZFAZZ_METADATA_BLOCK_STATE:-failed}): containers can reach 169.254.169.254" \
        "sudo bash ${SCRIPT_DIR}/scripts/metadata-block.sh install" \
        "cd ${SCRIPT_DIR} && ./rzfz status | grep -i 'metadata block'"
fi
# METADATA_BLOCK-END

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
# #1595: before seeding anything, undo what a previous `compose up` may have
# materialised — the whole set of file bind sources, not just this one file.
# An empty directory at a file bind source is by construction Docker's doing.
repair_empty_dir_bind_sources || true

mkdir -p certs
if [ -r /etc/ssl/certs/ca-certificates.crt ]; then
    cat /etc/ssl/certs/ca-certificates.crt > certs/caddy-ca.pem 2>/dev/null || true
elif [ -r /etc/pki/tls/certs/ca-bundle.crt ]; then
    cat /etc/pki/tls/certs/ca-bundle.crt > certs/caddy-ca.pem 2>/dev/null || true
fi
# Guarantee a non-empty FILE even if no system bundle was readable (never a dir).
[ -s certs/caddy-ca.pem ] || printf '# razzfazz.ai OIDC CA bundle (#152) — seeded pre-compose-up\n' > certs/caddy-ca.pem
# #2559: caddy's DIRECTORY bind source for the Mac worker bundle — the operator's, not Docker's root.
mkdir -p data/worker-bundle-generated

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
# #2170: an install from an offline package SAYS SO. Nothing else sets
# RAZZFAZZ_NETWORK_MODE — `.env.example` ships `online`, lib.sh defaults to
# `online`, and the only writer is `rzfz setup --network-mode`, a separate
# command an operator may never run. So a box installed entirely from a stick,
# with no WAN, believed it was online (measured on 0.175, journey B, #2126) and
# every belt keyed on the mode stayed disarmed: the Step 7b runner skip,
# cognee's HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE (#2143 — both absent on the
# container there), post-install's local-GGUF path, and upgrade's build/pull
# skips.
#
# The explicit flag always wins, because "the offline package is a cache, not
# offline-only" is a real install shape: a networked box may legitimately
# install from a stick and still want the internet afterwards. Intent cannot be
# read back from `.env`, whose value here is always the template's `online`, so
# the FLAG is the only honest signal.
#
# Logged either way — a mode that changes itself silently is worse than one that
# is merely wrong.
if [ -n "${CONFIG_NETWORK_MODE:-}" ]; then
    update_env_value ".env" RAZZFAZZ_NETWORK_MODE "$CONFIG_NETWORK_MODE"
    print_substep "Network mode: ${CONFIG_NETWORK_MODE} (explicit --network-mode)."
elif [ -f "$APPLIANCE_OFFLINE_PKG" ]; then
    # #2170 rev-B: a package's presence is NOT evidence of an air gap, so the
    # mode is NOT derived from it. The operator's standing rule is package-first
    # on ANY box — "if an image or model GGUF is present in the package, use it,
    # regardless of RAZZFAZZ_NETWORK_MODE; network mode controls only whether a
    # FALLBACK download is permitted at all" (2026-08-18, after post-install on
    # 0.236 re-downloaded 42 GB of GGUFs that were already in the package). The
    # case that produced that rule was a networked box carrying a package at
    # THIS path, so deriving `offline` here would flip exactly those boxes into
    # refusing the fallback they rely on.
    #
    # What is left is the thing the operator cannot otherwise see: the belts are
    # off. Said loudly, once, at the point the mode is resolved.
    print_substep "Network mode: ${RAZZFAZZ_NETWORK_MODE_DEFAULT} — an offline package is present at ${APPLIANCE_OFFLINE_PKG}, and that does NOT by itself make this box offline (#2170)."
    print_substep "  The package is used first for everything it carries, and fallback downloads stay permitted — which is correct for a networked box using the package as a cache."
    print_substep "  It also means the runtime egress belts stay DISARMED: no compose.offline.yml, and no HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE on cognee (#2143), so an air-gapped box will block on those."
    print_substep "  If this box has no internet, re-run with '--network-mode offline' (or set it once with 'rzfz setup --network-mode --mode offline')."
else
    print_substep "Network mode: ${RAZZFAZZ_NETWORK_MODE_DEFAULT} (no --network-mode given)."
fi
ensure_network_mode_overlay ".env"
# #3234 stick/firstboot path: harden-host (Step 5g, above) ran BEFORE the
# network mode was written to .env, so its offline mask branch still read the
# shipped template's `online` and skipped — a stick install booted with the
# timer only disabled and made hourly changelogs.ubuntu.com lookups. Reconcile
# the apt-listchanges units HERE, after the mode is persisted: the one call
# site on the init path (firstboot reaches it via `rzfz init --network-mode
# offline`). The implementation is the shared lib.sh helper, the same one the
# mode switch uses.
razzfazz_apt_listchanges_reconcile ".env"
# #184 WS2a: compose the UNIVERSAL no-runtime-build overlay (all modes) so the
# `up` below — and every later operator `docker compose up -d` / module enable —
# can never build. Wired here, AFTER the install-time `docker compose build`
# above (build_docker_images), so that build still saw the build: contexts.
ensure_nobuild_overlay ".env"
# #2526: the opt-in UDP 514 syslog port follows WAZUH_SYSLOG_UDP.
ensure_wazuh_syslog_udp_overlay ".env"

# #2644: a domain change over live data moves the domain-keyed database rows
# BEFORE the up below starts openwebui-seed (which signs in with the new
# address) and recreates the services that read them.
if [ "$INIT_HAD_LIVE_STATE" = true ] && [ -n "$INIT_PREVIOUS_DOMAIN" ]; then
    finish_init_redomain "$INIT_PREVIOUS_DOMAIN" "$(read_env_value .env MAIN_DOMAIN)"
fi

docker compose up -d --no-deps authentik-media-migrator 2>/dev/null || true
docker wait authentik-media-migrator >/dev/null 2>&1 || true
razzfazz_compose_up_with_dependency_retry
print_success "Stack started."

# Step 7: Wait for initialization
wait_for_authentik_init

# #2644: the database half of a re-domain is verified now that authentik-init
# has re-applied the blueprints, and Authentik's cache is cleared.
if [ "$INIT_HAD_LIVE_STATE" = true ] && [ -n "$INIT_PREVIOUS_DOMAIN" ]; then
    settle_init_redomain "$INIT_PREVIOUS_DOMAIN" "$(read_env_value .env MAIN_DOMAIN)"
fi

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
# OIDC clients so they reload the trust store. Best-effort under set -e: it
# returns 3 when a consumer that was running is not running afterwards, which
# must be reported, not abort the install here.
ensure_oidc_ca_superset \
    || print_warning "A container that mounts certs/caddy-ca.pem is not running (see above) — init continues; fix it with the command printed above, then 'rzfz post-install --refresh'."

# Step 7b: Post-start Matrix setup (volume ownership)
# CADDY_IP detection retired — see core/compose.yml caddy network aliases.
if init_profile_enabled matrix; then
    print_step "Configuring Synapse Matrix homeserver post-start setup..."
    matrix_post_start "$(pwd)"
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
        # #2090: the old line said "SSO will deny affected apps" — measured on
        # 0.91 the opposite holds for the zero-bindings case, and the wording
        # talked the reader out of looking at an open door.
        print_info    "These won't block init. An app with ZERO bindings is OPEN to every authenticated user (not denied);"
        print_info    "an app bound to the wrong groups denies everyone else. Re-run the idempotent binding pass:"
        print_info    "  rzfz post-install --refresh   (then scripts/post-install-group-lint.sh to confirm)  (#2090)"
    fi
fi

# Step 7b: Build the local llama-* runner images (M022 + rc6.8 shim).
# All three runner images bundle modules/llm/runners/llama-server-shim, which
# translates GPUStack-style `--flag=value` backend_parameters into the
# `--flag value` form upstream llama-server requires. Image build context
# is modules/llm/runners (the shared shim lives there). Idempotent — docker build
# skips unchanged layers.
#   * llama-runner:b12110-vulkan  — AMD Strix Halo Vulkan (the node-agent's AMD
#                                   default since #1188; #609 MTP-gated tag)
#   * llama-runner:b11778-vulkan  — AMD Strix Halo Vulkan rollback image (M022);
#                                   repin via RAZZFAZZ_ENGINE_IMAGE_AMD
#   * llama-rocm-runner:rocm-7.2.1 — AMD Strix Halo ROCm  (alternative/A-B)
#   * llama-runner:b12110-cpu     — CPU-only (testvm-cpu, HARDWARE=cpu)
if razzfazz_is_offline; then
    # #184 WS2b: llama-runner images are loaded from the offline package, never
    # built on an air-gapped box (the build pulls a base image + apt).
    print_substep "Step 7b: OFFLINE (RAZZFAZZ_NETWORK_MODE=offline) — skipping llama-runner image builds (loaded from the offline package)."
elif razzfazz_runner_images_wanted "$(read_env_value "${SCRIPT_DIR}/.env" COMPOSE_PROFILES)"; then
    # #1373 rev-B: the profile set comes from the .env this init just wrote —
    # `${COMPOSE_PROFILES:-}` is the OPERATOR'S shell environment, empty on
    # every normal invocation (0.175 round 2: "no consumer profile active" with
    # llm-worker-agent in .env). The old `llm` gate read the same empty variable.
    _build_runner() {
        # $1 image:tag, $2 dockerfile path, $3 optional build-arg (KEY=VALUE).
        # #628: pin the vulkan LLAMA_CPP_TAG per image — the Dockerfile default
        # moves with releases; an unpinned old-tag build gets the new binary.
        local _img="$1" _df="$2" _barg="$3"
        if docker image inspect "$_img" >/dev/null 2>&1; then
            print_info "$_img already present, skipping build."
            return 0
        fi
        print_step "Building $_img..."
        local _ctx
        _ctx=$(razzfazz_runner_build_context "${SCRIPT_DIR:-.}")   # runners.yaml build_context
        if razzfazz_docker_build -t "$_img" ${_barg:+--build-arg "$_barg"} -f "$_df" "$_ctx"; then
            print_success "$_img built."
        else
            print_warning "Failed to build $_img; the matching custom backend will not be usable."
            print_info "Re-run: docker build -t $_img ${_barg:+--build-arg $_barg} -f $_df $_ctx"
        fi
    }
    # #1516 (E5): driven by modules/llm/runners/runners.yaml — the same file
    # cli/post-install.sh and the publish step read, so the four build sites can
    # no longer drift (#1497). A `cuda` box builds BOTH CUDA targets: sm_120
    # (RTX PRO 6000) and sm_121a (GB10). CUDA 12.8's nvcc does not know sm_121,
    # so one image cannot serve both, and the wrong one dies with "no kernel
    # image is available for execution on the device" (measured on a GB10).
    # #2155: the hardware line comes from the .env this init just wrote, for
    # exactly the reason the `elif` above states for COMPOSE_PROFILES (#1373
    # rev-B). `${HARDWARE:-amd}` read the OPERATOR'S shell: cli/init.sh never
    # assigns HARDWARE — its own value is CONFIG_HARDWARE, written to .env at
    # `update_env_value ".env" "HARDWARE"` some 1700 lines above — so a
    # `--hardware cpu` install silently built the three AMD runners and never
    # built llama-runner:*-cpu. With `pull_policy: never` the box never gets
    # one afterwards. read_env_value prints empty (rc 0) for a missing file or
    # key, so `:-amd` keeps the historical default for a .env without the key.
    _rt_hw_src="$(read_env_value "${SCRIPT_DIR}/.env" HARDWARE)"
    _rt_hw=$(razzfazz_runner_hw_class "${_rt_hw_src:-amd}")
    _rt_rows=$(razzfazz_runner_manifest_rows "${SCRIPT_DIR:-.}")
    if [ -n "$_rt_rows" ]; then
        while IFS=$'\t' read -r _rt_img _rt_df _rt_class _rt_legacy _rt_args; do
            [ -n "$_rt_img" ] || continue
            [ "$_rt_class" = "$_rt_hw" ] || continue
            _build_runner "$_rt_img" "$_rt_df" $_rt_args
            # One cycle of back-compat for boxes pinned to the old name.
            if [ -n "$_rt_legacy" ] && docker image inspect "$_rt_img" >/dev/null 2>&1; then
                docker tag "$_rt_img" "$_rt_legacy" >/dev/null 2>&1 || true
            fi
        done <<< "$_rt_rows"
    else
        print_warning "modules/llm/runners/runners.yaml unreadable (PyYAML missing?) — no runner images built."
    fi
else
    # #1373: say so. A silent skip here is how a clean 2026.09 install shipped
    # with zero models (0.175: every deployment `failed`, no runner image).
    print_substep "Step 7b: no runner-image consumer profile active (one of: ${RAZZFAZZ_RUNNER_IMAGE_CONSUMER_PROFILES}) — skipping llama-runner image builds."
fi

# M032 S01: pre-warm the test venv so the first `rzfz test` invocation
# is fast (no 10-second pip-install delay). Reconciles the venv with
# tests/requirements.txt; non-fatal on failure, the cause is shown. Pure pre-warm; does NOT run any tests. The
# --with-acceptance flag wiring (which actually runs probes here) is
# below in run_acceptance_probes() (M032-S06).
bootstrap_test_venv() {
    local wrapper="$SCRIPT_DIR/cli/test.sh"
    if [ ! -x "$wrapper" ]; then
        # cli/test.sh not in this checkout (older bundle, or pre-M032
        # rollback). Skip silently — not fatal.
        return 0
    fi
    if [ ! -f "$SCRIPT_DIR/tests/requirements.txt" ]; then
        return 0
    fi
    # #2939: the venv installs from PyPI, and an offline box has no other source.
    # Trying anyway is ~400 blocked connections to pypi.org that an air-gapped
    # customer's firewall/SIEM sees from an "offline" appliance — for a
    # convenience (a faster first `rzfz test`) that cannot succeed there.
    if razzfazz_is_offline; then
        print_info "Offline box: test venv not pre-warmed (it installs from PyPI). With network access: rzfz test --bootstrap-only"
        return 0
    fi
    print_step "Pre-warming test venv (M032)..."
    local _venv_out
    if _venv_out="$("$wrapper" --bootstrap-only 2>&1)"; then
        print_success "Test venv ready at tests/.venv"
    else
        # Say WHY: swallowing the output threw away the one line that named
        # the cause.
        print_warning "Test venv bootstrap failed (non-fatal):"
        printf '%s\n' "$_venv_out" | tail -n 5 | sed 's/^/    /'
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

    # #1326: a package / public-export box ships no tests/; refuse before the
    # health wait and the venv bootstrap, so nothing is written to the box.
    if [ ! -f "$SCRIPT_DIR/tests/requirements.txt" ] || [ ! -d "$SCRIPT_DIR/tests/acceptance" ]; then
        print_error "This delivery does not ship the acceptance tests (tests/); --with-acceptance needs a development checkout."
        return 4
    fi

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

    local wrapper="$SCRIPT_DIR/cli/test.sh"
    if [ ! -x "$wrapper" ]; then
        print_error "Test runner not found at $wrapper — cannot run acceptance probes."
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

    local rc=0
    "$wrapper" "${probe_args[@]}" || rc=$?
    if [ "$rc" -eq 0 ]; then
        print_success "All acceptance probes passed."
        return 0
    fi
    if [ "$rc" -eq 5 ]; then
        print_error "No acceptance probes were collected — see M032 S02."
        print_info "Report path: $SCRIPT_DIR/tests/results/latest/acceptance-report.md"
    else
        print_error "One or more acceptance probes failed (probe-suite RC=$rc)."
        print_info "Report path: $SCRIPT_DIR/tests/results/latest/acceptance-report.md"
    fi
    return 4  # exit code 4 = probes-failed (distinct from 1 = init-failed)
}

# #1247: razzfazz.init one-shot exit-code gate. Every image-builder / *-init
# one-shot must have exited 0 — a non-zero exit means the artifact it exists to
# produce is missing and the module built on it can never work. Before #1247
# nothing on the install path read those codes: moltis-image-builder ended
# Exited(127) on the 0.91 clean install (#1246) and init still printed
# "Installation Complete" with RC 0. Discovery + classification live in the
# shared razzfazz_init_oneshot_* helpers (scripts/lib.sh), which
# `post-install --verify` and `rzfz status` read too.
#
# Evaluated HERE so the banner below can tell the truth, but turned into a
# non-zero exit only at the very bottom of the script — the remaining
# bookkeeping (version stamp, host path, checksum baseline) must still run, and
# the operator needs those recorded even on a partially-broken install.
INIT_ONESHOT_RC=0
print_step "Verifying razzfazz.init one-shot containers (#1247)..."
if ! razzfazz_init_oneshot_gate; then
    INIT_ONESHOT_RC=6   # 6 = init-oneshot-failed (distinct from 1=init-failed, 4=probes-failed)
fi

# #2873: the same FINAL health gate as `rzfz upgrade`. A compose up that aborted
# (a bad command, a missing image) leaves services in Created, and neither the
# one-shot gate above nor the banner noticed. Same rule as #1247: evaluated here
# so the banner tells the truth, turned into the exit code at the very bottom.
INIT_HEALTH_RC=0
print_step "Final health gate..."
if ! razzfazz_final_health_gate; then
    INIT_HEALTH_RC=7   # 7 = stack-not-healthy (6=init-oneshot-failed)
fi

# #1941: put `rzfz` on PATH so the documentation's bare `rzfz <cmd>` works —
# including the appliance's own first command. Never fatal: a refused or
# unwritable entry leaves a working stack that is invoked as ./rzfz, which is
# what every box does today.
print_step "Linking the rzfz CLI onto PATH (#1941)..."
razzfazz_link_cli_onto_path "$SCRIPT_DIR" || true

# Show completion message
if [ "$INIT_ONESHOT_RC" -eq 0 ] && [ "$INIT_HEALTH_RC" -eq 0 ]; then
    show_completion_message "ok"
else
    show_completion_message "failed"
fi

# Record installed version in .env
if [ -f "VERSION" ]; then
    update_env_value ".env" "RAZZFAZZ_VERSION" "$(cat VERSION | tr -d '[:space:]')"
    print_substep "Recorded stack version $(cat VERSION | tr -d '[:space:]') in .env"
fi
if command -v git &>/dev/null && [ -d ".git" ]; then
    update_env_value ".env" "RAZZFAZZ_COMMIT" "$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
    print_substep "Recorded commit hash in .env"
fi
# #2359: on a box installed from the public mirror HEAD is the curated export
# commit, which exists in no internal history. The export's PUBLIC_EXPORT_OF
# stamp names the development commit it was cut from; record that beside
# RAZZFAZZ_COMMIT so support can resolve this box to a commit we hold. Without
# a stamp (internal checkout) the helper answers HEAD and the two keys agree.
if _source_commit="$(razzfazz_source_commit .)"; then
    update_env_value ".env" "RAZZFAZZ_SOURCE_COMMIT" "$_source_commit"
    print_substep "Recorded source commit ${_source_commit} in .env"
fi
# #2441: the build record written while the images were loaded predates the
# RAZZFAZZ_VERSION line above — and RAZZFAZZ_VERSION is a build arg of the
# worker-agent image, so that record's context digest folds an EMPTY version
# and every later `rzfz verify-images` calls a byte-identical package image
# "stale". Re-record the package images now that .env is final; the on-box
# manifest copy the load left behind names exactly which images to record.
# RECORD_AFTER_ENV-BEGIN
_pkg_manifest_state="$(razzfazz_package_manifest_state_path 2>/dev/null || true)"
if [ -n "$_pkg_manifest_state" ] && [ -s "$_pkg_manifest_state" ]; then
    razzfazz_record_custom_image_builds "" package "$_pkg_manifest_state"
    razzfazz_record_digest_pins "$_pkg_manifest_state"
fi
# RECORD_AFTER_ENV-END
# STACK_HOST_PATH must be the absolute repo path on the host so
# core/compose.yml's razzfazz-config bind mount sees the same path inside
# the container as the docker daemon does outside (see core/compose.yml
# comments — without this, apply_manager's `docker compose up -d` hands
# the daemon /stack/... paths it can't resolve, and the daemon auto-
# creates a directory tree that breaks subsequent caddy/dify recreates).
# #2831: already recorded at Step 4.1, before the steps that can abort; this
# re-assert is idempotent (update_env_value replaces the line in place).
update_env_value ".env" "STACK_HOST_PATH" "$(realpath .)"
print_substep "Recorded stack host path $(realpath .) in .env"
apply_pat_mode   # #2690: idempotent; marks an existing unmarked .env now
wire_git_credentials_for_updates

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
        razzfazz_print_deferred_host_steps
        exit "$ACCEPTANCE_RC"
    fi
fi

# #1247: a razzfazz.init one-shot that ended non-zero is an install failure, not
# a footnote. Surfaced LAST so the operator still gets the version stamp, the
# checksum baseline and (with --with-acceptance) the probe report; the [FAIL]
# lines themselves were printed above, before the banner.
if [ "$INIT_ONESHOT_RC" -ne 0 ]; then
    echo ""
    print_error "Install INCOMPLETE: one or more razzfazz.init one-shots failed (#1247)."
    print_info "  Which ones:  docker ps -a --filter label=razzfazz.init"
    print_info "  Why:         docker logs <name>"
    print_info "  Re-check:    rzfz status   •   rzfz post-install --verify"
    razzfazz_print_deferred_host_steps
    exit "$INIT_ONESHOT_RC"
fi

# #2873: a stack that is not up is an install failure, whatever the one-shots say.
if [ "$INIT_HEALTH_RC" -ne 0 ]; then
    echo ""
    print_error "Install INCOMPLETE: the stack is not healthy (see the final health gate above)."
    print_info "  Re-check:    docker compose ps -a   -   rzfz status"
    razzfazz_print_deferred_host_steps
    exit "$INIT_HEALTH_RC"
fi

# The host steps that did not run, repeated where the operator reads the end.
razzfazz_print_deferred_host_steps
