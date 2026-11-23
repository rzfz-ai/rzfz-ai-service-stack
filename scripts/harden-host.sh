#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz.ai Stack - Host Hardening Script
# ==============================================================================
# Applies host-level security hardening per M009 Security Review (Section 9).
#
# Addresses findings:
#   F-056  No firewall configured on host         (UFW)
#   F-057  SSH not hardened                        (sshd + fail2ban)
#   F-058  No automatic security updates           (unattended-upgrades)
#   F-059  No Docker daemon.json                   (log limits, live-restore)
#   F-060  No kernel sysctl hardening              (network + kernel params)
#   F-061  .env file permissions not restricted     (chmod 600/700)
#   F-071  No audit logging on host                (auditd)
#
# Usage:
#   sudo ./scripts/harden-host.sh                  # Apply all hardening
#   sudo ./scripts/harden-host.sh --dry-run        # Show what would be done
#   sudo ./scripts/harden-host.sh --skip-ssh       # Skip SSH hardening
#   sudo ./scripts/harden-host.sh --help
#
# This script is idempotent and safe to run multiple times.
#
# SSH restricted to a bastion: a re-run keeps an existing 22/tcp DENY, REJECT
# or LIMIT rule and a source-scoped 22/tcp allow; it adds the blanket
# `ufw allow 22/tcp` only when no such restriction exists. Rolling such a
# restriction back (from the console or the emergency path):
#   cp ~/.ssh/authorized_keys.pre-<change> ~/.ssh/authorized_keys
#   sudo ufw allow 22/tcp
# or restore a snapshot of /etc/ufw/user.rules and /etc/ufw/user6.rules taken
# before the change, then `sudo ufw reload`. Never use `ufw delete deny 22/tcp`
# as the rollback: `ufw deny 22/tcp` on a hardened box updates the existing
# allow in place, so deleting the deny leaves NO 22/tcp rule and the default
# deny keeps SSH closed.
# Requires root privileges.
# Tested on Ubuntu 22.04+ and 24.04+.
# ==============================================================================

set -eo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# read_env_value (lib.sh) and _gpustack_profile_active (lib-owui.sh): the
# firewall step asks the stack's one profile helper whether GPUStack runs on
# this box (#2614). Both files only define functions and constants.
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"
# shellcheck source=scripts/lib-owui.sh
source "${SCRIPT_DIR}/lib-owui.sh"

DRY_RUN=false
# RESTRICT_SSH default = false. SSH hardening (PasswordAuthentication no,
# PermitRootLogin no, restart sshd) is OPT-IN via --restrict-ssh because
# enabling it without verifying that the operator's pubkey is in
# authorized_keys locks them out. Operators must explicitly opt in once
# they've confirmed key auth works.
# The legacy --skip-ssh flag is kept for backward compat — it now does
# nothing (since SSH is skipped by default). --restrict-ssh is the only
# way to enable the SSH step.
RESTRICT_SSH=false
SKIP_SSH=true   # legacy alias; flipped by --restrict-ssh
AUTO_REBOOT_TIME="04:00"
# S31: default to the ACTUAL stack-dir owner, not a hardcoded name. Production/worker
# boxes run as `administrator` / `seqis-administrator`; the old hardcoded
# `razzfazz-ai-admin` made the F-061 chown step silently no-op there
# ("User 'razzfazz-ai-admin' not found -- skipping chown" on 10.163). The
# --stack-user flag still overrides this default.
STACK_USER="$(stat -c '%U' "$STACK_DIR" 2>/dev/null || echo razzfazz-ai-admin)"

# --- Helpers ------------------------------------------------------------------

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }
step()  { echo -e "\n${CYAN}=== $* ===${NC}"; }
dry()   { echo -e "${YELLOW}[DRY-RUN]${NC} Would: $*"; }

# #3018: an offline box (installed from a stick) may lack a package these steps
# install, and has no archive to fetch it from. Under `set -e` the first failed
# `apt-get install` ended the whole script, so sysctl, auditd and Docker's
# no-new-privileges were never applied. Now: already installed -> fine; else
# apt; else .deb files carried in RZFZ_HOST_DEBS_DIR; else the item is SKIPPED,
# named with the exact steps, and the rest of the hardening continues.
HOST_DEBS_DIR="${RZFZ_HOST_DEBS_DIR:-/opt/razzfazz-appliance/host-debs}"
HARDEN_SKIPPED=""
_pkg_missing() {
    local p m=""
    for p in "$@"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || m="$m $p"
    done
    echo "${m# }"
}
host_pkg_install() {
    local missing
    missing="$(_pkg_missing "$@")"
    [ -z "$missing" ] && return 0
    # shellcheck disable=SC2086 # word splitting of the package list is intended
    if apt-get install -y $missing > /dev/null 2>&1; then
        return 0
    fi
    if [ -d "$HOST_DEBS_DIR" ] && ls "$HOST_DEBS_DIR"/*.deb > /dev/null 2>&1; then
        dpkg -i "$HOST_DEBS_DIR"/*.deb > /dev/null 2>&1 || true
        # shellcheck disable=SC2086
        missing="$(_pkg_missing $missing)"
        [ -z "$missing" ] && { ok "installed from ${HOST_DEBS_DIR}: $*"; return 0; }
    fi
    warn "Could not install: ${missing} (no package source reachable) - this hardening item is SKIPPED, the rest continues."
    warn "  To add it later: on a machine with internet and the same Ubuntu release run"
    warn "    apt-get download ${missing}   (plus the dependencies it names)"
    warn "  copy the .deb files to ${HOST_DEBS_DIR}/ on this box and re-run: sudo bash scripts/harden-host.sh"
    HARDEN_SKIPPED="${HARDEN_SKIPPED:+$HARDEN_SKIPPED }${missing}"
    return 1
}

usage() {
    cat <<'USAGE'
Usage: sudo ./scripts/harden-host.sh [OPTIONS]

Options:
  --dry-run           Show what would be done without making changes
  --restrict-ssh      Apply SSH hardening (PasswordAuthentication no,
                      PermitRootLogin no, restart sshd). DEFAULT: OFF —
                      opt in only after confirming your pubkey is in
                      ~/.ssh/authorized_keys. Without this flag SSH
                      stays as-is.
  --skip-ssh          (legacy) No effect — SSH is skipped by default.
                      Kept for backward compat with older runbooks.
  --auto-reboot TIME  Set automatic reboot time for security updates (default: 04:00)
  --no-auto-reboot    Disable automatic reboot after security updates
                      (durable alternative: RAZZFAZZ_AUTO_REBOOT=false in .env)
  --stack-user USER   Stack owner username (default: razzfazz-ai-admin)
  --yes, -y           Proceed without prompting even if a stack is running.
  --help              Show this help message

Examples:
  sudo ./scripts/harden-host.sh                       # ufw + fail2ban + sysctl + auditd + perms (NO SSH change)
  sudo ./scripts/harden-host.sh --dry-run             # preview
  sudo ./scripts/harden-host.sh --restrict-ssh        # full hardening incl. SSH key-only auth
  sudo ./scripts/harden-host.sh --no-auto-reboot      # don't auto-reboot for kernel updates
USAGE
    exit 0
}

# --- Parse arguments ----------------------------------------------------------

NO_AUTO_REBOOT=false
NO_AUTO_REBOOT_FLAG=false
ASSUME_YES=false
# Set true if daemon.json was applied via `reload` on a live stack — the
# no-new-privileges setting then needs a Docker restart to take
# effect, so the final summary flags that the box isn't fully hardened yet (#2).
HARDENING_PENDING_REBOOT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)       DRY_RUN=true; shift ;;
        --restrict-ssh)  RESTRICT_SSH=true; SKIP_SSH=false; shift ;;
        --skip-ssh)      SKIP_SSH=true; shift ;;   # legacy, no-op (SSH skipped by default)
        --auto-reboot)   AUTO_REBOOT_TIME="$2"; shift 2 ;;
        --no-auto-reboot) NO_AUTO_REBOOT=true; NO_AUTO_REBOOT_FLAG=true; shift ;;
        --stack-user)    STACK_USER="$2"; shift 2 ;;
        --yes|-y)        ASSUME_YES=true; shift ;;  # proceed even if a stack is running
        --help|-h)       usage ;;
        *)               err "Unknown option: $1"; usage ;;
    esac
done

# --- Pre-flight checks -------------------------------------------------------

# RAZZFAZZ_AUTO_REBOOT in the stack .env makes the no-auto-reboot choice
# durable across re-runs: a bare re-run without --no-auto-reboot used to
# silently write Unattended-Upgrade::Automatic-Reboot "true" again (and the
# box then rebooted itself for a security update). Read the same way as the
# other stack .env reads in this script: read_env_value naming the file
# explicitly — init runs this script without RAZZFAZZ_ENV_FILE, so the no-arg
# helper would read <stack>/scripts/.env and always answer with the default.
# Unset or true = today's behaviour; false = same as --no-auto-reboot. An
# explicit --no-auto-reboot on the command line wins over the .env value.
# This lives AFTER the parse-arguments section on purpose: the parser section
# is lifted and run standalone by the tests WITHOUT lib.sh sourced, so it must
# stay free of lib.sh calls (read_env_value) — here lib.sh is available.
apply_auto_reboot_env() {
    if $NO_AUTO_REBOOT_FLAG; then
        return 0
    fi
    local v
    v="$(read_env_value "$STACK_DIR/.env" RAZZFAZZ_AUTO_REBOOT)"
    case "$v" in
        false|FALSE|False|0|no|NO|off|OFF) NO_AUTO_REBOOT=true ;;
        ""|true|TRUE|True|1|yes|YES|on|ON) ;;
        *) warn "RAZZFAZZ_AUTO_REBOOT='${v}' is not true/false — automatic reboot stays enabled" ;;
    esac
}
apply_auto_reboot_env

if [[ "$(id -u)" -ne 0 ]]; then
    err "This script must be run as root (sudo)."
    exit 1
fi

if ! grep -qiE 'ubuntu' /etc/os-release 2>/dev/null; then
    warn "This script is designed for Ubuntu 22.04+. Proceeding anyway."
fi

if $DRY_RUN; then
    echo -e "\n${YELLOW}=== DRY RUN MODE - No changes will be made ===${NC}\n"
fi

info "Stack directory: $STACK_DIR"
info "Stack user: $STACK_USER"

# --- Safety guard: don't silently disrupt a RUNNING stack -------------------
# Step 7/7 (Docker daemon hardening) writes daemon.json and RESTARTS the Docker
# daemon to apply it; SSH-restrict (if enabled) restarts sshd. On a box with a
# running stack this briefly STOPS every service — and a Docker package/state
# transition during the restart can even reset container state (containers +
# images gone, volumes kept), forcing a full rebuild. Best practice: run
# harden-host BEFORE razzfazz-init, on a fresh box with no stack started yet.
guard_live_stack() {
    $DRY_RUN && return 0
    local running
    running=$(docker ps --format '{{.Names}}' 2>/dev/null \
        | grep -cE '^(caddy|postgres|authentik-server|gpustack)$' 2>/dev/null || true)
    [[ "${running:-0}" -eq 0 ]] && return 0
    echo
    warn "A razzfazz.ai stack is RUNNING on this box (${running} core services up)."
    warn "harden-host restarts the Docker daemon (step 7/7) — this briefly STOPS the"
    warn "stack and can require a full image rebuild. Best practice: run harden-host"
    warn "BEFORE razzfazz-init, on a box with no stack started yet."
    if [[ "$ASSUME_YES" == "true" || "${RAZZFAZZ_HARDEN_ON_LIVE:-}" == "1" ]]; then
        warn "Proceeding anyway (--yes / RAZZFAZZ_HARDEN_ON_LIVE=1)."
        return 0
    fi
    if [[ -t 0 ]]; then
        local ans
        read -r -p "$(echo -e "${YELLOW}Proceed and disrupt the running stack? [y/N] ${NC}")" ans
        [[ "$ans" =~ ^[Yy]$ ]] && return 0
        err "Aborted. Re-run with --yes (or RAZZFAZZ_HARDEN_ON_LIVE=1) to force."
        exit 1
    fi
    err "Refusing to harden a LIVE stack non-interactively (would stop the stack)."
    err "Run harden-host BEFORE init, or pass --yes / set RAZZFAZZ_HARDEN_ON_LIVE=1 to force."
    exit 1
}
guard_live_stack

# ==============================================================================
# 1. FIREWALL (UFW) - F-056
# ==============================================================================

step "1/7 Firewall (UFW) [F-056]"

# --- GPUStack master ports (#2614) --------------------------------------------
# A GPUStack master needs 9090 (API), 10150-10151 (worker RPC), 40000-40103
# (runners) reachable from its external workers. 52365 (ray) is published on
# loopback only and is NOT opened (a rule tagged for it is removed). Earlier runs
# opened them to ANYWHERE whenever GPUSTACK_MODE=master, even on boxes without
# the llm-legacy profile (a preset sets master; upgraded boxes keep it).
#
# Now: open them only while the profile is on, from the subnet the workers
# reach the master on, and never from anywhere. Every rule carries a comment
# that starts with GPUSTACK_MASTER_TAG; that comment is how a later run finds
# its own rules again and removes the ones that no longer apply. The prefix is
# RESERVED for this script: a hand-made ufw rule whose comment starts with it
# is treated as ours and removed when the current scope does not want it.
GPUSTACK_MASTER_PORT_SET=(9090 10150:10151 40000:40103)
GPUSTACK_MASTER_TAG="razzfazz gpustack master"
# The exact `ufw show added` lines the pre-#2614 runs wrote. Only these are
# removed from that era; a hand-made rule on the same port is left alone.
GPUSTACK_MASTER_LEGACY_RULES=(
    "ufw allow 9090/tcp comment 'GPUStack API'"
    "ufw allow 10150:10151/tcp comment 'GPUStack RPC (master, external workers)'"
    "ufw allow 40000:40103/tcp comment 'GPUStack P2P'"
    "ufw allow 52365/tcp comment 'Ray compute'"
)

# True when the box should accept external GPUStack workers: the llm-legacy
# profile is on (the shared helper, exact token) AND GPUSTACK_MODE=master.
_gpustack_master_open_wanted() {
    local env_file="$STACK_DIR/.env" COMPOSE_PROFILES mode
    [[ -f "$env_file" ]] || return 1
    COMPOSE_PROFILES=$(read_env_value "$env_file" COMPOSE_PROFILES)
    _gpustack_profile_active || return 1
    mode=$(read_env_value "$env_file" GPUSTACK_MODE)
    [[ "$mode" == "master" ]]
}

# The connected IPv4 subnet(s) whose interface carries <ip>, from the kernel's
# own link routes (docker bridges carry their own src and never match).
# Only a plain IPv4 CIDR is passed on; anything else (a `default` or host
# route, a malformed line) is skipped with a warning on stderr, because the
# caller captures stdout and ufw would refuse the rule under set -e halfway
# through the firewall step.
_gpustack_master_subnets_of() {
    local ip="$1" net
    [[ -n "$ip" ]] || return 0
    while IFS= read -r net; do
        [[ -n "$net" ]] || continue
        if [[ "$net" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([1-9]|[12][0-9]|3[0-2])$ ]]; then
            echo "$net"
        else
            warn "GPUStack master: skipping route '${net}' for ${ip} (not an IPv4 subnet)" >&2
        fi
    done < <(ip -4 route show proto kernel scope link 2>/dev/null \
        | awk -v src="$ip" '{for (i = 1; i < NF; i++) if ($i == "src" && $(i+1) == src) print $1}' \
        | awk 'NF && !seen[$0]++' || true)
}

# Prints "<source-subnet> <destination>" per rule scope. The workers reach the
# master on GPUSTACK_HOST_BIND (modules/llm/compose.yml publishes the ports
# there): a specific LAN IP scopes to that address and its subnet; 0.0.0.0
# scopes to the subnet of the default-route interface; loopback needs no rule.
_gpustack_master_scopes() {
    local bind src s
    bind=$(read_env_value "$STACK_DIR/.env" GPUSTACK_HOST_BIND)
    bind="${bind:-127.0.0.1}"
    case "$bind" in
        127.*|::1|localhost) return 0 ;;
        0.0.0.0)
            src=$(ip -4 route get 1.1.1.1 2>/dev/null \
                | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i+1); exit}}' || true)
            for s in $(_gpustack_master_subnets_of "$src"); do echo "$s any"; done ;;
        *)
            for s in $(_gpustack_master_subnets_of "$bind"); do echo "$s $bind"; done ;;
    esac
}

# Removes the rules earlier runs added that the current scope does not want:
# every exact pre-#2614 line, and every tagged rule whose comment is not in
# the wanted list (passed as arguments). Rules without our comment stay.
_gpustack_master_remove_stale() {
    local line spec comment legacy wanted keep
    command -v ufw >/dev/null 2>&1 || return 0
    while IFS= read -r line; do
        [[ "$line" == "ufw "* ]] || continue
        keep=true
        for legacy in "${GPUSTACK_MASTER_LEGACY_RULES[@]}"; do
            [[ "$line" == "$legacy" ]] && keep=false
        done
        if $keep && [[ "$line" =~ \ comment\ \'(${GPUSTACK_MASTER_TAG}\ [^\']*)\'$ ]]; then
            comment="${BASH_REMATCH[1]}"
            keep=false
            for wanted in "$@"; do
                [[ "$comment" == "$wanted" ]] && keep=true
            done
        fi
        $keep && continue
        spec="${line#ufw }"
        spec="${spec% comment \'*}"
        if $DRY_RUN; then
            dry "Remove stale GPUStack master rule: ufw delete $spec"
            continue
        fi
        # shellcheck disable=SC2086  # the spec is ufw's own word list
        if ufw --force delete $spec > /dev/null 2>&1; then
            ok "Removed stale GPUStack master rule: $spec"
        else
            warn "Could not remove GPUStack master rule: $spec"
        fi
    done < <(ufw show added 2>/dev/null || true)
}

_gpustack_master_ports() {
    local -a scopes=() wanted=()
    local sc src dst port
    if _gpustack_master_open_wanted; then
        mapfile -t scopes < <(_gpustack_master_scopes)
        if [[ ${#scopes[@]} -eq 0 ]]; then
            warn "GPUStack master: no LAN subnet found for GPUSTACK_HOST_BIND — external worker ports stay closed."
            warn "Set GPUSTACK_HOST_BIND in .env to the LAN address the workers reach, then re-run this script."
        fi
        for sc in "${scopes[@]}"; do
            src="${sc% *}"; dst="${sc#* }"
            for port in "${GPUSTACK_MASTER_PORT_SET[@]}"; do
                wanted+=("${GPUSTACK_MASTER_TAG} ${port} from ${src} to ${dst}")
            done
        done
    fi
    _gpustack_master_remove_stale "${wanted[@]}"
    for sc in "${scopes[@]}"; do
        src="${sc% *}"; dst="${sc#* }"
        for port in "${GPUSTACK_MASTER_PORT_SET[@]}"; do
            if $DRY_RUN; then
                dry "Allow GPUStack master port ${port}/tcp from ${src} to ${dst}"
                continue
            fi
            ufw allow proto tcp from "$src" to "$dst" port "$port" \
                comment "${GPUSTACK_MASTER_TAG} ${port} from ${src} to ${dst}" > /dev/null 2>&1
        done
        $DRY_RUN || ok "GPUStack master ports opened from ${src} to ${dst} (9090, 10150-10151, 40000-40103)"
    done
    return 0
}

# --- SSH firewall rule (#2728) ------------------------------------------------
# ufw treats a rule with the same spec as an update in place, so an
# unconditional `ufw allow 22/tcp` turns an existing `ufw deny 22/tcp` (SSH
# only from a bastion) back into an allow. The step reads the rules on 22/tcp
# first and keeps a restriction; afterwards it checks `ufw status`, the running
# firewall, not its own calls.

# 0 when a ufw port spec (a number, a list, a range) covers port 22.
_ufw_port_has_22() {
    local spec="$1" p a b
    local -a parts=()
    IFS=',' read -r -a parts <<<"$spec"
    for p in "${parts[@]}"; do
        case "$p" in
            OpenSSH|ssh|22) return 0 ;;
            *:*)
                a="${p%%:*}"; b="${p#*:}"
                [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]] && (( a <= 22 && 22 <= b )) && return 0 ;;
        esac
    done
    return 1
}

# One `ufw show added` line -> "restrict" (a 22/tcp deny/reject/limit from
# anywhere, or a 22/tcp allow scoped to a source), "allow-any" (a blanket
# 22/tcp allow) or nothing (any other rule).
_ufw_ssh_rule_kind() {
    local line="${1%% comment *}"
    local -a w=()
    read -r -a w <<<"$line"
    [[ "${w[0]:-}" == "ufw" ]] || return 0
    local action="${w[1]:-}" i=2 port="" from="any" proto="" positional=""
    case "$action" in allow|deny|reject|limit) ;; *) return 0 ;; esac
    case "${w[2]:-}" in in) i=3 ;; out|route) return 0 ;; esac
    while (( i < ${#w[@]} )); do
        case "${w[i]}" in
            from)  from="${w[i+1]:-}"; i=$((i + 2)) ;;
            port)  port="${w[i+1]:-}"; i=$((i + 2)) ;;
            app)   port="${w[i+1]:-}"; i=$((i + 2)) ;;
            proto) proto="${w[i+1]:-}"; i=$((i + 2)) ;;
            to|on) i=$((i + 2)) ;;
            *)     [[ -n "$positional" ]] || positional="${w[i]}"; i=$((i + 1)) ;;
        esac
    done
    if [[ -z "$port" && -n "$positional" ]]; then
        port="${positional%%/*}"
        [[ "$positional" == */* ]] && proto="${positional#*/}"
    fi
    [[ -z "$proto" || "$proto" == "tcp" ]] || return 0
    _ufw_port_has_22 "$port" || return 0
    case "$from" in any|0.0.0.0/0|::/0)
        if [[ "$action" == "allow" ]]; then echo "allow-any"; else echo "restrict"; fi ;;
    *)
        # A source-scoped allow is the bastion pattern; a scoped deny restricts
        # nothing for everyone else.
        if [[ "$action" == "allow" ]]; then echo "restrict"; fi ;;
    esac
    return 0
}

# The user rules on 22/tcp, one per line as "<kind> <rule without comment>".
_ufw_ssh_rules() {
    local line kind
    while IFS= read -r line; do
        kind=$(_ufw_ssh_rule_kind "$line")
        if [[ -n "$kind" ]]; then printf '%s %s\n' "$kind" "${line%% comment *}"; fi
    done < <(LC_ALL=C ufw show added 2>/dev/null || true)
    return 0
}

# The rows of `ufw status` (stdin) that ALLOW 22/tcp from anywhere.
_ufw_status_ssh_open() {
    local line i act
    local -a w=() to=()
    while IFS= read -r line; do
        line="${line%%#*}"
        read -r -a w <<<"$line"
        act=-1
        for (( i = 0; i < ${#w[@]}; i++ )); do
            case "${w[i]}" in ALLOW|DENY|REJECT|LIMIT) act=$i; break ;; esac
        done
        (( act > 0 )) || continue
        [[ "${w[act]}" == "ALLOW" ]] || continue
        i=$((act + 1))
        case "${w[i]:-}" in OUT|FWD) continue ;; IN) i=$((i + 1)) ;; esac
        [[ "${w[i]:-}" == "Anywhere" ]] || continue
        to=("${w[@]:0:act}")
        for (( i = 0; i < ${#to[@]}; i++ )); do
            case "${to[i]}" in */udp) continue ;; esac
            if _ufw_port_has_22 "${to[i]%%/*}"; then printf '%s\n' "$line"; break; fi
        done
    done
    return 0
}

# Fails (non-zero, loudly) when the running firewall does not match what the
# step meant to leave: the restriction kept and no blanket allow, or, without a
# restriction, SSH reachable.
_ufw_ssh_assert_effective() {
    local restricted="$1" before="$2" status open after
    # ufw translates its status table (ALLOW, Anywhere); parse the C locale.
    if ! status=$(LC_ALL=C ufw status 2>&1); then
        err "ufw status failed; cannot confirm the effective SSH rule: ${status}"
        return 1
    fi
    open=$(_ufw_status_ssh_open <<<"$status")
    if [[ "$restricted" == "true" ]]; then
        after=$(_ufw_ssh_rules)
        if [[ -n "$open" || "$after" != "$before" ]]; then
            err "The existing SSH restriction on 22/tcp is NOT effective after the firewall step."
            err "Rules on 22/tcp before:"; sed 's/^/          /' <<<"${before:-<none>}"
            err "Rules on 22/tcp now:";    sed 's/^/          /' <<<"${after:-<none>}"
            [[ -z "$open" ]] || { err "ufw status allows 22/tcp from anywhere:"; sed 's/^/          /' <<<"$open"; }
            err "Re-assert the restriction (for example: sudo ufw deny 22/tcp) and check: sudo ufw status numbered"
            return 1
        fi
        return 0
    fi
    if [[ -z "$open" ]]; then
        err "ufw status shows no 22/tcp allow after the firewall step; SSH may be unreachable."
        err "Check: sudo ufw status numbered"
        return 1
    fi
    return 0
}

harden_firewall() {
    if $DRY_RUN; then
        dry "Install ufw"
        dry "Set default deny incoming, allow outgoing"
        local ssh_now=""
        command -v ufw >/dev/null 2>&1 && ssh_now=$(_ufw_ssh_rules)
        if grep -q '^restrict ' <<<"$ssh_now" && ! grep -q '^allow-any ' <<<"$ssh_now"; then
            dry "Keep the existing SSH restriction on 22/tcp; allow 80/tcp (HTTP), 443/tcp (HTTPS)"
        else
            dry "Allow 22/tcp (SSH), 80/tcp (HTTP), 443/tcp (HTTPS)"
        fi
        _gpustack_master_ports
        dry "Enable ufw"
        return
    fi

    host_pkg_install ufw || return 0
    ok "ufw installed"

    ufw default deny incoming > /dev/null 2>&1
    ufw default allow outgoing > /dev/null 2>&1
    ok "Default policy: deny incoming, allow outgoing"

    # SSH: keep a restriction (a 22/tcp deny/reject/limit, or a source-scoped
    # allow such as a bastion-only rule); the blanket allow only goes in when
    # there is none, or when a blanket allow is already there (#2728).
    local ssh_before ssh_restricted=false
    ssh_before=$(_ufw_ssh_rules)
    if grep -q '^restrict ' <<<"$ssh_before" && ! grep -q '^allow-any ' <<<"$ssh_before"; then
        ssh_restricted=true
    fi
    if $ssh_restricted; then
        ok "SSH 22/tcp: kept the existing restriction (no blanket allow added):"
        sed 's/^/          /' <<<"$ssh_before"
    else
        ufw allow 22/tcp comment "SSH" > /dev/null 2>&1
    fi
    ufw allow 80/tcp comment "HTTP" > /dev/null 2>&1
    ufw allow 443/tcp comment "HTTPS" > /dev/null 2>&1
    if $ssh_restricted; then
        ok "Allowed ports: 80/tcp, 443/tcp"
    else
        ok "Allowed ports: 22/tcp, 80/tcp, 443/tcp"
    fi

    # rc6.7 #17: gpustack server-in-container reaches its own embedded
    # worker via host.docker.internal:10150 → docker-bridge gateway IP.
    # That listener (bound to 0.0.0.0 since rc6.7) needs UFW INPUT
    # permission from the docker bridge subnets but NOT from the LAN —
    # 172.16.0.0/12 covers docker0 (172.17.0.0/16) and any compose-
    # created bridges (172.18+). Apply unconditionally regardless of
    # GPUSTACK_MODE because the embedded worker exists in standalone
    # too. Master-mode operators get an additional broad rule below.
    ufw allow proto tcp from 172.16.0.0/12 to any port 10150 \
        comment "GPUStack worker (docker bridge)" > /dev/null 2>&1
    ufw allow proto tcp from 172.16.0.0/12 to any port 10151 \
        comment "GPUStack worker metrics (docker bridge)" > /dev/null 2>&1
    # rc6.7 #25: GPUStack v2.x serves the OpenAI-compatible endpoint at
    # gpustack:9090/v1-openai/* but 307-redirects to the runner directly
    # (host.docker.internal:40000-40103). OpenWebUI / Dify / any caller in
    # container space follows the redirect via the docker bridge, so the
    # whole runner port range needs an INPUT rule from the bridge subnets
    # too — same scope as 10150/10151. The 8265 + 40096-40103 + 52365 ray
    # ports don't get hit by the OpenAI proxy redirect today, but they
    # belong to the same service and the rule is cheap.
    ufw allow proto tcp from 172.16.0.0/12 to any port 40000:40103 \
        comment "GPUStack runner (docker bridge)" > /dev/null 2>&1
    ufw allow proto tcp from 172.16.0.0/12 to any port 8265 \
        comment "GPUStack ray dashboard (docker bridge)" > /dev/null 2>&1
    ufw allow proto tcp from 172.16.0.0/12 to any port 52365 \
        comment "GPUStack ray compute (docker bridge)" > /dev/null 2>&1
    ok "Allowed: 10150-10151,40000-40103,8265,52365/tcp from docker bridges (gpustack)"

    # 2026.11: the bridge -> host:8000 allow an earlier release added for a
    # removed coding agent's sandbox is taken back on a re-run.
    local _ufw_rule
    while IFS= read -r _ufw_rule; do
        case "$_ufw_rule" in
            8000/tcp*172.16.0.0/12*"OpenHands sandbox"*)
                ufw delete allow proto tcp from 172.16.0.0/12 to any port 8000 > /dev/null 2>&1 || true
                break ;;
        esac
    done <<< "$(ufw status 2>/dev/null || true)"

    # GPUStack master mode: the ports external workers need, scoped to the
    # worker subnet and only while GPUStack runs here (#2614).
    _gpustack_master_ports

    ufw --force enable > /dev/null 2>&1
    ok "UFW enabled"

    _ufw_ssh_assert_effective "$ssh_restricted" "$ssh_before" || return 1
    if $ssh_restricted; then
        ok "ufw status: the SSH restriction is effective"
    else
        ok "ufw status: 22/tcp allowed"
    fi
}

harden_firewall

# ==============================================================================
# 2. SSH HARDENING + FAIL2BAN - F-057
# ==============================================================================

step "2/7 SSH Hardening [F-057]"

# --- SSH drop-in (#2728) -------------------------------------------------------
# sshd keeps the FIRST value it reads for a keyword, and the Include of
# sshd_config.d/*.conf reads the files in lexical order. A cloud image's
# 50-cloud-init.conf (PasswordAuthentication yes) therefore beat the old
# 99-razzfazz-hardening.conf. The drop-in now sorts first, and the step checks
# the EFFECTIVE values with `sshd -T`: `sshd -t` only checks the syntax.
SSHD_DROPIN_DIR="/etc/ssh/sshd_config.d"
SSHD_DROPIN="00-razzfazz-hardening.conf"
SSHD_DROPIN_OLD="99-razzfazz-hardening.conf"
SSHD_EFFECTIVE_WANT=("passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin no")
# sshd -t and -T refuse to run without the privilege separation directory. Only
# a started ssh.service creates it, so a socket-activated host (ssh.socket,
# Ubuntu 22.10 and later) that had no connection yet lacks it.
SSHD_PRIVSEP_DIR="/run/sshd"

# Debian and Ubuntu name the unit ssh.service; sshd.service there is an alias
# that exists only while ssh.service is enabled, which a socket-activated host
# does not do. Other distributions have sshd.service only.
_ssh_restart() {
    local unit
    for unit in ssh.service sshd.service; do
        if systemctl restart "$unit" 2>/dev/null; then
            ok "Restarted $unit"
            return 0
        fi
    done
    err "Could not restart ssh.service or sshd.service; the new settings take effect only after sshd restarts."
    return 1
}

# Fails (non-zero, loudly) unless `sshd -T` shows every SSHD_EFFECTIVE_WANT line.
_ssh_assert_effective() {
    local eff want key have files pat
    local -a bad=()
    if ! eff=$(sshd -T 2>&1); then
        err "sshd -T failed; cannot confirm the effective SSH settings: ${eff}"
        return 1
    fi
    for want in "${SSHD_EFFECTIVE_WANT[@]}"; do
        grep -qx "$want" <<<"$eff" || bad+=("$want")
    done
    [[ ${#bad[@]} -eq 0 ]] && return 0
    err "SSH hardening is NOT effective: sshd -T disagrees with ${SSHD_DROPIN}."
    for want in "${bad[@]}"; do
        key="${want%% *}"
        have=$(grep -i "^${key} " <<<"$eff" || true)
        pat="$key"
        [[ "$key" == "kbdinteractiveauthentication" ]] && pat="(kbdinteractiveauthentication|challengeresponseauthentication)"
        files=$(grep -ilE "^[[:space:]]*${pat}[[:space:]]" "$SSHD_DROPIN_DIR"/*.conf /etc/ssh/sshd_config 2>/dev/null \
            | tr '\n' ' ' || true)
        err "  want '${want}', sshd -T has '${have:-<unset>}'; set in: ${files:-<no file>}"
    done
    err "sshd keeps the first value it reads: a file that sorts before ${SSHD_DROPIN}, or a setting"
    err "above the Include line in /etc/ssh/sshd_config, wins. Fix it there, then re-run with --restrict-ssh."
    return 1
}

# `sshd -T` without -C prints the global values; a Match block can still set
# PasswordAuthentication yes for a user or an address. Named, not failed: such a
# block is a deliberate exception the step cannot judge.
_ssh_warn_match_blocks() {
    local files
    files=$(grep -ilE '^[[:space:]]*Match[[:space:]]' "$SSHD_DROPIN_DIR"/*.conf /etc/ssh/sshd_config 2>/dev/null \
        | tr '\n' ' ' || true)
    [[ -n "$files" ]] || return 0
    warn "sshd -T shows the global values; a Match block can override them per user or address: ${files}"
    warn "Check one connection: sudo sshd -T -C user=<name>,host=<host>,addr=<ip>"
}

# Writes the drop-in, removes the old 99- file, tests, restarts, then checks
# the effective values.
_ssh_apply_dropin() {
    local sshd_conf="${SSHD_DROPIN_DIR}/${SSHD_DROPIN}"
    mkdir -p "$SSHD_DROPIN_DIR"

    cat > "$sshd_conf" <<EOF
# razzfazz.ai host hardening - SSH (F-057)
# Generated by harden-host.sh - do not edit manually.
# The 00- prefix is deliberate: sshd keeps the first value it reads, so this
# file must sort before cloud-init's 50-cloud-init.conf and similar files.

PasswordAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin no
MaxAuthTries 3
MaxSessions 3
KbdInteractiveAuthentication no
X11Forwarding no
AllowAgentForwarding no
ClientAliveInterval 300
ClientAliveCountMax 2

# Modern cryptography only
KexAlgorithms sntrup761x25519-sha512@openssh.com,curve25519-sha256@libssh.org,curve25519-sha256
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-gcm@openssh.com
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com
EOF
    ok "Created $sshd_conf"

    if [[ ! -d "$SSHD_PRIVSEP_DIR" ]]; then
        mkdir -p "$SSHD_PRIVSEP_DIR"
        chmod 0755 "$SSHD_PRIVSEP_DIR"
    fi
    # Test sshd config before restarting. The old 99- file stays until the test
    # passes: on a failure the host keeps the hardening it had.
    if ! sshd -t 2>/dev/null; then
        err "sshd configuration test failed! Removing hardening config to prevent lockout."
        rm -f "$sshd_conf"
        err "Please check your sshd configuration manually."
        return 1
    fi
    if [[ -e "${SSHD_DROPIN_DIR}/${SSHD_DROPIN_OLD}" ]]; then
        rm -f "${SSHD_DROPIN_DIR}/${SSHD_DROPIN_OLD}"
        ok "Removed the old ${SSHD_DROPIN_DIR}/${SSHD_DROPIN_OLD} (it lost to earlier drop-ins)"
    fi
    _ssh_restart || return 1
    ok "sshd configuration tested and service restarted"

    _ssh_assert_effective || return 1
    ok "sshd -T: ${SSHD_EFFECTIVE_WANT[*]}"
    _ssh_warn_match_blocks
}

harden_ssh() {
    if $SKIP_SSH; then
        # Default-skip: SSH hardening is opt-in via --restrict-ssh. This is
        # intentional after a near-lockout incident (boxes deployed with
        # password auth only; harden-host.sh would lock the operator out
        # until a pubkey is set up). Step 2 fires only when the operator
        # explicitly passes --restrict-ssh, having confirmed key auth works.
        info "SSH hardening skipped (default — pass --restrict-ssh to enable)"
        info "Before passing --restrict-ssh, confirm:"
        info "  - your pubkey is in ~$STACK_USER/.ssh/authorized_keys, AND"
        info "  - 'ssh -i <key> -o PreferredAuthentications=publickey ${STACK_USER}@<this-host>' works"
        return
    fi

    # Safety check: warn about SSH key requirement
    if ! $DRY_RUN; then
        local has_authorized_keys=false
        if [[ -f "/home/$STACK_USER/.ssh/authorized_keys" ]] && [[ -s "/home/$STACK_USER/.ssh/authorized_keys" ]]; then
            has_authorized_keys=true
        fi
        if [[ -f "/root/.ssh/authorized_keys" ]] && [[ -s "/root/.ssh/authorized_keys" ]]; then
            has_authorized_keys=true
        fi

        if ! $has_authorized_keys; then
            warn "No SSH authorized_keys found for '$STACK_USER' or root."
            warn "Disabling password authentication will lock you out if no SSH keys are configured!"
            warn "Skipping SSH hardening. Use --skip-ssh to suppress this warning,"
            warn "or add your SSH public key first: ssh-copy-id $STACK_USER@<this-host>"
            return
        fi
    fi

    if $DRY_RUN; then
        dry "Create ${SSHD_DROPIN_DIR}/${SSHD_DROPIN} with hardened settings (remove the old ${SSHD_DROPIN_OLD})"
        dry "Test sshd config, restart sshd, check the effective values with sshd -T"
        return
    fi

    _ssh_apply_dropin

    # fail2ban setup moved to install_fail2ban() below — runs unconditionally
    # (even when --restrict-ssh is off) because brute-force defence is useful
    # against password auth too, arguably MORE useful since password auth
    # is the still-allowed inbound path.
}

harden_ssh

# fail2ban — independent of SSH-restrict. Always installed/configured.
install_fail2ban() {
    if $DRY_RUN; then
        dry "Install fail2ban + configure SSH jail (5 retries, 1h ban)"
        return
    fi
    host_pkg_install fail2ban || return 0
    ok "fail2ban installed"

    cat > /etc/fail2ban/jail.d/sshd.conf <<'EOF'
# razzfazz.ai host hardening - fail2ban SSH jail (F-057)
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 5
bantime = 3600
findtime = 600
EOF

    systemctl enable fail2ban > /dev/null 2>&1
    systemctl restart fail2ban
    ok "fail2ban configured and started (SSH jail: 5 retries, 1h ban)"
}

step "2b/7 fail2ban [F-057, brute-force defence]"
install_fail2ban

# ==============================================================================
# 3. AUTOMATIC SECURITY UPDATES - F-058
# ==============================================================================

step "3/7 Automatic Security Updates [F-058]"

# The unattended-upgrades config directory. A variable so the unit tests can
# point the writes at a scratch directory instead of /etc (the SSHD_DROPIN_DIR
# pattern).
APT_CONF_DIR="/etc/apt/apt.conf.d"

harden_auto_updates() {
    if $DRY_RUN; then
        dry "Install unattended-upgrades and apt-listchanges"
        dry "Configure security-only updates (kernel blacklisted)"
        if $NO_AUTO_REBOOT; then
            dry "Automatic reboot: disabled"
        else
            dry "Automatic reboot: enabled at $AUTO_REBOOT_TIME"
        fi
        return
    fi

    # #3234: apt-listchanges.timer (OnCalendar=hourly) makes apt-listchanges
    # fetch changelogs.ubuntu.com on every tick — an OFFLINE box must not look
    # anything up. The packages stay installed (the offline stick carries them,
    # #3223); going back online re-enables the timer
    # (scripts/apply-network-mode.sh). The systemctl calls live in the shared
    # lib.sh helper so the hardening and the network-mode switch cannot drift.
    # The mode read names the file explicitly: SCRIPT_DIR here is scripts/,
    # and init runs this script without RAZZFAZZ_ENV_FILE, so the no-arg
    # helper would read <stack>/scripts/.env and always answer online.
    # The reconcile runs BEFORE the package install and independent of it:
    # when the install fails (no package source reachable, #3018) an already
    # installed, running apt-listchanges.timer must still not survive.
    if razzfazz_is_offline "$STACK_DIR/.env"; then
        razzfazz_apt_listchanges_apply offline
        ok "Offline box: apt-listchanges.timer masked (no hourly changelogs.ubuntu.com lookups)"
    fi

    host_pkg_install unattended-upgrades apt-listchanges || return 0
    ok "unattended-upgrades installed"

    local reboot_setting="false"
    local reboot_time_setting=""
    if ! $NO_AUTO_REBOOT; then
        reboot_setting="true"
        reboot_time_setting="Unattended-Upgrade::Automatic-Reboot-Time \"$AUTO_REBOOT_TIME\";"
    fi

    cat > "${APT_CONF_DIR}/50unattended-upgrades" <<CONF
// razzfazz.ai host hardening - unattended upgrades (F-058)
Unattended-Upgrade::Allowed-Origins {
    "\${distro_id}:\${distro_codename}-security";
    "\${distro_id}ESMApps:\${distro_codename}-apps-security";
    "\${distro_id}ESM:\${distro_codename}-infra-security";
};
Unattended-Upgrade::Package-Blacklist {
    "linux-image-*";
    "linux-headers-*";
    "linux-modules-*";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "$reboot_setting";
${reboot_time_setting}
Unattended-Upgrade::Mail "root";
CONF

    cat > "${APT_CONF_DIR}/20auto-upgrades" <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
CONF

    systemctl enable unattended-upgrades > /dev/null 2>&1
    ok "Automatic security updates configured (kernel blacklisted for GPU driver compat)"
    if ! $NO_AUTO_REBOOT; then
        info "Automatic reboot enabled at $AUTO_REBOOT_TIME if required"
    else
        info "Automatic reboot disabled"
    fi
}

harden_auto_updates

# ==============================================================================
# 3b. ROBUST TIME SYNC (chrony makestep) - F-TIME
# ==============================================================================
# VMs — especially Multipass/QEMU — jump the guest clock on host suspend/resume.
# systemd-timesyncd only SLEWS small offsets and won't quickly correct a large
# jump, which makes short-lived TLS certs (Caddy's internal CA, self-signed
# boxes) look expired → HTTPS breaks (e.g. start.<domain> showing a cert error,
# mis-read as "lost the outpost") until the cert is renewed. chrony with
# `makestep 1 -1` steps the clock immediately for ANY offset on every update,
# so a resume-induced jump is corrected at once.

step "3b/7 Robust Time Sync [F-TIME]"

harden_timesync() {
    if $DRY_RUN; then
        dry "Install chrony; step-correct clock jumps (makestep 1 -1) for VM suspend/resume"
        return
    fi

    # #3018: install chrony FIRST - stopping timesyncd before a failed install left
    # the box without any time sync.
    host_pkg_install chrony || return 0
    # systemd-timesyncd and chrony conflict; chrony is the one that step-corrects.
    systemctl stop systemd-timesyncd 2>/dev/null || true
    systemctl disable systemd-timesyncd 2>/dev/null || true

    local conf=/etc/chrony/chrony.conf
    if [ -f "$conf" ]; then
        # makestep <threshold> <limit>: 1 -1 = step (not slew) for any offset >1s,
        # on every correction (no limit) — the right policy for pause/resume VMs.
        if grep -qE '^\s*makestep' "$conf"; then
            sed -i 's/^[[:space:]]*makestep.*/makestep 1 -1/' "$conf"
        else
            printf '\n# razzfazz.ai: step-correct clock jumps (VM suspend/resume)\nmakestep 1 -1\n' >> "$conf"
        fi
    fi

    systemctl enable chrony > /dev/null 2>&1 || systemctl enable chronyd > /dev/null 2>&1
    systemctl restart chrony 2>/dev/null || systemctl restart chronyd 2>/dev/null || true
    chronyc makestep > /dev/null 2>&1 || true
    ok "Time sync hardened (chrony, makestep 1 -1 — clock jumps step-corrected)"
}

harden_timesync

# ==============================================================================
# 4. KERNEL SYSCTL HARDENING - F-060
# ==============================================================================

step "4/7 Kernel Sysctl Hardening [F-060]"

harden_sysctl() {
    local sysctl_conf="/etc/sysctl.d/99-razzfazz-hardening.conf"

    if $DRY_RUN; then
        dry "Create $sysctl_conf with network + kernel hardening parameters"
        dry "Apply sysctl settings"
        return
    fi

    cat > "$sysctl_conf" <<'EOF'
# razzfazz.ai host hardening - kernel sysctl (F-060)
# Generated by harden-host.sh - do not edit manually

# Network hardening
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv6.conf.default.accept_source_route = 0
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.all.log_martians = 1
net.ipv4.conf.default.log_martians = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

# Kernel hardening
kernel.randomize_va_space = 2
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
kernel.perf_event_paranoid = 3
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
fs.protected_fifos = 2
fs.protected_regular = 2
fs.suid_dumpable = 0
kernel.sysrq = 176
net.core.bpf_jit_harden = 2
EOF

    sysctl --system > /dev/null 2>&1
    ok "Sysctl hardening parameters applied"
}

harden_sysctl

# ==============================================================================
# 5. AUDIT LOGGING - F-071
# ==============================================================================

step "5/7 Audit Logging [F-071]"

harden_auditd() {
    if $DRY_RUN; then
        dry "Install auditd and audispd-plugins"
        dry "Create /etc/audit/rules.d/razzfazz.rules"
        dry "Enable and restart auditd"
        return
    fi

    host_pkg_install auditd audispd-plugins || return 0
    ok "auditd installed"

    # The managed file is REPLACED as a whole (rendered to a temp file, then
    # moved into place) - never appended to, so a re-run cannot duplicate it.
    # augenrules/auditctl -R stop at the FIRST bad line (duplicate rule, watch
    # on a missing path) and lose every later rule including the final `-e 2`;
    # hosts came up with 14-23 of 20-39 rules, enabled=1, not locked. So: `-i`
    # first (continue past errors) and only watches whose path exists now.
    local rules_file="${AUDIT_RULES_FILE:-/etc/audit/rules.d/razzfazz.rules}"
    local root="${AUDIT_ROOT:-}"
    local rules_tmp
    rules_tmp="$(mktemp "${rules_file}.XXXXXX")" || { err "cannot create temp rules file"; return 1; }
    local rendered="${rules_tmp}.render"
    cat > "$rendered" <<EOF
# razzfazz.ai host hardening - audit rules (F-071)
# Generated by harden-host.sh - do not edit manually

# Ignore errors: one bad rule must not drop the rest or the final lock
-i

# Clear existing rules
-D

# Buffer size
-b 8192

# Docker control plane.
#
# #537: there is DELIBERATELY no watch on /var/run/docker.sock. The old
#   -w /var/run/docker.sock -p rwxa -k docker_socket
# rule loaded fine, showed up in 'auditctl -l', and recorded NOTHING:
# -w is a FILE watch (open/read/write/exec/attr), while talking to a unix
# socket is connect() on an existing inode — none of those. Measured in the
# field: 0 records across a window with a demonstrable client on the socket.
# A control that is present-but-inert gets counted by assessments and then
# costs investigation time when the trail turns out empty. A blanket
# '-S connect' syscall rule cannot be filtered to one path and would flood
# the log with every dbus/systemd connect on the host. The real answer is
# routing all consumers through docker-socket-proxy (which logs method+path)
# and shrinking the raw-socket holder set — tracked in #537's follow-up.
# What CAN be watched honestly: the socket inode's OWNERSHIP AND MODE, the
# daemon config, the unit files, and the binaries that define what the socket
# does.
#
# The '-p a' (attribute) half of the removed rule was NOT inert: a path watch
# cannot see connect(), but it does record chmod/chown on the inode — and
# 'chmod 666 /var/run/docker.sock' is a classic local-privesc step on a box
# whose socket is root-equivalent. The replacement rules below cover
# /etc/docker/, the unit files and the dockerd binary; none of them covers the
# socket's own mode/owner. Attribute-only, so it does not reintroduce the
# present-but-inert usage watch this comment block is about.
-w /var/run/docker.sock -p a -k docker_socket_perms
-w /etc/docker/ -p wa -k docker_config
-w /lib/systemd/system/docker.service -p wa -k docker_config
-w /lib/systemd/system/docker.socket -p wa -k docker_config
-w /usr/bin/dockerd -p wa -k docker_config

# Privilege escalation
-w /etc/sudoers -p wa -k sudo_changes
-w /etc/sudoers.d/ -p wa -k sudo_changes

# Identity files
-w /etc/passwd -p wa -k identity
-w /etc/group -p wa -k identity
-w /etc/shadow -p wa -k identity

# SSH config changes
-w /etc/ssh/sshd_config -p wa -k sshd_config
-w /etc/ssh/sshd_config.d/ -p wa -k sshd_config

# Cron changes
-w /etc/crontab -p wa -k cron
-w /etc/cron.d/ -p wa -k cron
-w /var/spool/cron/ -p wa -k cron

# Stack .env files
-w $STACK_DIR/.env -p wa -k stack_env

# Root commands run by a logged-in person (auid set); daemons and container
# runtimes run with auid unset and would flood the audit trail
-a always,exit -F arch=b64 -F euid=0 -F auid>=1000 -F auid!=unset -S execve -k root_commands

# Lock rules (must be last)
-e 2
EOF

    # Drop watches on paths that do not exist on this host
    local line wpath
    : > "$rules_tmp"
    while IFS= read -r line; do
        if [[ "$line" == "-w "* ]]; then
            wpath="${line#-w }"; wpath="${wpath%% *}"
            if [ ! -e "${root}${wpath}" ]; then
                info "audit: skipping watch on missing path $wpath"
                continue
            fi
        fi
        printf '%s\n' "$line" >> "$rules_tmp"
    done < "$rendered"
    rm -f "$rendered"
    chmod 640 "$rules_tmp"
    mv -f "$rules_tmp" "$rules_file"

    local pre_enabled
    pre_enabled="$(auditctl -s 2>/dev/null | awk '/^enabled/{print $2; exit}' || true)"

    systemctl enable auditd > /dev/null 2>&1
    systemctl restart auditd 2>/dev/null || augenrules --load > /dev/null 2>&1

    if [ "$pre_enabled" = "2" ]; then
        ok "auditd rules file written; audit is locked (enabled 2), the new rules apply at the next reboot"
        return 0
    fi

    local post_enabled want have
    # The restart returns before auditd has applied the rules; poll briefly.
    local tries=0
    while :; do
        post_enabled="$(auditctl -s 2>/dev/null | awk '/^enabled/{print $2; exit}' || true)"
        [ "$post_enabled" = "2" ] && break
        tries=$((tries + 1))
        [ "$tries" -ge "${AUDIT_LOCK_WAIT_TRIES:-20}" ] && break
        sleep "${AUDIT_LOCK_WAIT_STEP:-0.5}"
    done
    want="$(grep -Evc '^[[:space:]]*(#|$|-[Dbifre]( |$))' "$rules_file" || true)"
    have="$(auditctl -l 2>/dev/null | grep -vc '^No rules' || true)"
    if [ "$post_enabled" != "2" ]; then
        err "auditd is not locked after loading the rules (enabled=${post_enabled:-unknown}, expected 2)"
        return 1
    fi
    # Other files in rules.d (e.g. a SIEM's own audit keys) load too, so the
    # loaded set is a superset: fewer rules than ours, or one of our keys
    # missing, means augenrules stopped early.
    if [ "${have:-0}" -lt "${want:-0}" ]; then
        err "auditd loaded $have rules but $rules_file alone has $want"
        return 1
    fi
    local key missing=""
    for key in $(grep -Ev '^[[:space:]]*#' "$rules_file" | grep -oE -- '-k [A-Za-z0-9_]+' | awk '{print $2}' | sort -u); do
        auditctl -l 2>/dev/null | grep -qE -- "(-k |key=)${key}( |$)" || missing="$missing $key"
    done
    if [ -n "$missing" ]; then
        err "auditd did not load the rules for key(s):$missing"
        return 1
    fi
    ok "auditd configured and started ($have rules loaded, locked)"
}

harden_auditd || exit 1

# ==============================================================================
# 6. FILE PERMISSIONS - F-061
# ==============================================================================

step "6/7 File Permissions [F-061]"

harden_permissions() {
    if $DRY_RUN; then
        dry "chmod 600 .env .env.dify (if they exist)"
        dry "chmod 700 backups/ and management scripts (if they exist)"
        dry "chown -R $STACK_USER:$STACK_USER $STACK_DIR"
        return
    fi

    local changed=0

    if [[ -f "$STACK_DIR/.env" ]]; then
        chmod 600 "$STACK_DIR/.env"
        changed=$((changed + 1))
    fi
    if [[ -f "$STACK_DIR/.env.dify" ]]; then
        chmod 600 "$STACK_DIR/.env.dify"
        changed=$((changed + 1))
    fi
    if [[ -d "$STACK_DIR/backups" ]]; then
        chmod 700 "$STACK_DIR/backups"
        changed=$((changed + 1))
    fi

    # Restrict management scripts
    for script in razzfazz-init.sh razzfazz-backup.sh razzfazz-setup.sh razzfazz-upgrade.sh razzfazz-ai-box-setup.sh; do
        if [[ -f "$STACK_DIR/$script" ]]; then
            chmod 700 "$STACK_DIR/$script"
            changed=$((changed + 1))
        fi
    done

    # Ensure correct ownership
    if id "$STACK_USER" > /dev/null 2>&1; then
        chown -R "$STACK_USER":"$STACK_USER" "$STACK_DIR"
        ok "Ownership set to $STACK_USER:$STACK_USER"
    else
        warn "User '$STACK_USER' not found -- skipping chown"
    fi

    ok "File permissions hardened ($changed items updated)"
}

harden_permissions

# ==============================================================================
# 7. DOCKER DAEMON HARDENING - F-059
# ==============================================================================

step "7/7 Docker Daemon Hardening [F-059]"

harden_docker_daemon() {
    # Overridable so the shipped function can be exercised by tests against a
    # temp file instead of the host's real /etc/docker.
    local daemon_json="${DAEMON_JSON_PATH:-/etc/docker/daemon.json}"

    if $DRY_RUN; then
        if [[ -f "$daemon_json" ]]; then
            dry "Update $daemon_json (merge hardening settings)"
        else
            dry "Create $daemon_json with log limits, live-restore, no-new-privileges"
        fi
        dry "Restart Docker daemon"
        return
    fi

    # #546: this used to CREATE the file only when absent. If it already existed
    # without `no-new-privileges` it printed "please merge these manually" and
    # returned -- an instruction to a human, executed by a script nobody watches.
    #
    # That is the ONLY path a real box takes: the appliance first-boot installs
    # Docker and writes a daemon.json (log-opts, address-pools, storage-driver)
    # BEFORE the operator runs hardening. So on every appliance install this step
    # silently no-opped, and `rzfz status` then showed a green "host-hardened
    # marker present" beside a yellow "missing no-new-privileges" -- which reads
    # as a hardened box. Confirmed in the field on a customer install.
    #
    # Now it MERGES: the hardening keys are set and every other key the operator
    # or the installer put there is preserved (clobbering default-address-pools
    # would re-subnet every container network; clobbering log-opts would uncap
    # the logs).
    #
    # python3 rather than sed/append because the result must be VALID JSON -- a
    # malformed daemon.json makes dockerd refuse to START, which is worse than
    # unhardened. python3 ships with Ubuntu Server; if it is somehow absent we
    # FAIL LOUDLY and leave the file untouched, because skipping quietly is the
    # exact defect this replaces.
    #
    # `userland-proxy` is deliberately NOT set: removed 2026-05-10 after a Docker
    # bridge DNS race (containers transiently saw "server misbehaving" from
    # 127.0.0.11 during cascading recreates). The default keeps a small per-port
    # proxy that handles connection setup more resiliently; the perf cost on a
    # private bridge is negligible.
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 unavailable -- cannot safely merge $daemon_json."
        err "Docker daemon hardening NOT applied (F-059). Install python3 and re-run."
        return 1
    fi

    mkdir -p "$(dirname "$daemon_json")"
    local _before=""
    [[ -f "$daemon_json" ]] && _before="$(cat "$daemon_json")"

    # #369: determine the ACTIVE image store BEFORE touching `storage-driver`.
    # Docker >= 28/29 defaults new installs to the containerd image store
    # (`docker info` Driver: overlayfs). Writing storage-driver=overlay2 into
    # daemon.json on such a box changes nothing immediately (not in the SIGHUP
    # reload allow-list) but flips the daemon to the CLASSIC store on the next
    # restart/reboot — and every image the box holds lives in the containerd
    # store, so `docker images` comes back EMPTY. Locally-built images are on
    # no registry; on an offline appliance nothing can re-pull: the act of
    # hardening bricks the stack, days later, at the operator's reboot.
    # Removing the key after that flip does NOT flip back (an initialized
    # classic store sticks) — the deterministic control is
    # `features.containerd-snapshotter`, so that is what gets pinned instead.
    local docker_image_store="unknown"
    local _docker_driver=""
    _docker_driver=$(docker info --format '{{.Driver}}' 2>/dev/null || true)
    if [[ "$_docker_driver" == "overlayfs" ]] \
       || { [[ -n "$_docker_driver" ]] && docker info --format '{{json .DriverStatus}}' 2>/dev/null \
            | grep -q 'containerd.snapshotter'; }; then
        docker_image_store="containerd"
    elif [[ -n "$_docker_driver" ]]; then
        docker_image_store="classic"
    fi

    if ! DAEMON_JSON="$daemon_json" DOCKER_IMAGE_STORE="$docker_image_store" python3 - <<'PYHARDEN'
import json, os, sys
path = os.environ["DAEMON_JSON"]
try:
    with open(path) as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("daemon.json is not a JSON object")
except FileNotFoundError:
    cfg = {}
except Exception as exc:                       # unparseable -- do NOT overwrite
    print(f"unparseable {path}: {exc}", file=sys.stderr)
    sys.exit(2)

cfg.setdefault("log-driver", "json-file")
cfg.setdefault("log-opts", {"max-size": "50m", "max-file": "3"})
cfg.setdefault("default-address-pools", [{"base": "172.17.0.0/12", "size": 24}])
# #369: storage-driver is written ONLY when the classic store is already the
# active one (a pin of the status quo). On a containerd-image-store box the
# same line would flip the store at the next restart and hide every image
# (unrecoverable offline) — there the containerd store is pinned instead and
# an inert overlay2 entry is dropped while the flip has not happened yet.
# Unknown active store (docker down): write NOTHING store-related — guessing
# either way risks flipping somebody's store.
store = os.environ.get("DOCKER_IMAGE_STORE", "unknown")
if store == "classic":
    cfg.setdefault("storage-driver", "overlay2")
elif store == "containerd":
    sd = cfg.get("storage-driver")
    if sd == "overlay2":
        del cfg["storage-driver"]
        sd = None
    if sd is None:
        feats = cfg.setdefault("features", {})
        if isinstance(feats, dict):
            # setdefault: an operator-forced `false` is respected.
            feats.setdefault("containerd-snapshotter", True)
    else:
        print("WARNING: containerd image store is ACTIVE but daemon.json pins "
              f"storage-driver={sd!r} — the next Docker restart will switch "
              "stores and hide all current images (#369). Left untouched; "
              "resolve manually.", file=sys.stderr)
cfg["live-restore"] = True
cfg["no-new-privileges"] = True

tmp = path + ".harden-tmp"
with open(tmp, "w") as fh:
    json.dump(cfg, fh, indent=4)
    fh.write("\n")
os.replace(tmp, path)                          # atomic; never a half-written file
PYHARDEN
    then
        err "Could not merge hardening settings into $daemon_json -- left untouched."
        err "Docker daemon hardening NOT applied (F-059)."
        return 1
    fi

    if [[ "$_before" == "$(cat "$daemon_json")" ]]; then
        ok "$daemon_json already hardened -- no change"
        return
    fi
    ok "$daemon_json hardened (no-new-privileges, live-restore; existing keys preserved)"
    ok "Created $daemon_json"

    # Apply the new daemon.json. #2: on a box with a LIVE stack, prefer `reload`
    # (SIGHUP) over `restart` — a full restart briefly STOPS every container
    # (live-restore isn't active yet on this first apply, so it WOULD tear the
    # stack down). BUT Docker's SIGHUP reload only re-reads a limited allow-list
    # (live-restore, default-address-pools, registry settings, max-concurrent-*, …).
    # The SECURITY-relevant `no-new-privileges` daemon default is NOT in that
    # set — it stays inactive until a full Docker restart. So on a
    # live box we reload to avoid downtime but must WARN that hardening is incomplete
    # until the operator's next restart (review finding: never report plain success).
    # A fresh box (no stack up) restarts now to apply everything immediately.
    if systemctl is-active --quiet docker; then
        local running
        running=$(docker ps --format '{{.Names}}' 2>/dev/null \
            | grep -cE '^(caddy|postgres|authentik-server|gpustack)$' 2>/dev/null || true)
        if [[ "${running:-0}" -gt 0 ]]; then
            info "Live stack detected (${running} core services) — applying via reload (no downtime)..."
            if systemctl reload docker 2>/dev/null; then
                ok "Docker daemon reloaded (no stack downtime)."
            else
                warn "systemctl reload docker failed."
            fi
            warn "reload applies only a SUBSET of daemon.json. The security-relevant"
            warn "no-new-privileges default is NOT active yet — this box is"
            warn "not fully hardened until a Docker restart. Schedule a reboot (or"
            warn "'systemctl restart docker' in a maintenance window) to COMPLETE hardening."
            HARDENING_PENDING_REBOOT=true
        else
            info "Restarting Docker daemon to apply settings..."
            systemctl restart docker
            ok "Docker daemon restarted (full daemon.json applied)"
        fi
    else
        warn "Docker is not running -- settings will apply on next start"
    fi
}

harden_docker_daemon

# ==============================================================================
# 7b. AGENT MEMORY SLICE (#692)
# ==============================================================================
# All agent containers are created under cgroup parent razzfazz-agents.slice
# (agent-manager docker_client). This unit gives that slice a MemoryMax so a
# joint agent build/run storm OOMs INSIDE the slice — the kernel kills agent
# processes, not postgres/authentik/systemd (the host-freeze class).
# Budget: AGENT_TOTAL_MEMORY_BUDGET_GB from .env; default 30% of host RAM,
# clamped to >= 4 GB so a single default agent still fits.

step "Agent memory slice (razzfazz-agents.slice)"

harden_agent_slice() {
    if $DRY_RUN; then
        dry "Install /etc/systemd/system/razzfazz-agents.slice with MemoryMax (#692)"
        return
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemd not available — agent slice not installed (per-container limits only)."
        return 0
    fi
    local budget_gb ram_kb ram_gb
    budget_gb="$(grep -m1 '^AGENT_TOTAL_MEMORY_BUDGET_GB=' "$STACK_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
    if ! [ "$budget_gb" -ge 1 ] 2>/dev/null; then
        ram_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)
        ram_gb=$(( ${ram_kb:-0} / 1024 / 1024 ))
        budget_gb=$(( ram_gb * 30 / 100 ))
        [ "$budget_gb" -ge 4 ] || budget_gb=4
    fi
    cat > /etc/systemd/system/razzfazz-agents.slice <<EOF
# razzfazz.ai — shared memory budget for ALL agent containers (#692).
# Written by harden-host.sh; override via AGENT_TOTAL_MEMORY_BUDGET_GB in .env
# and re-run 'sudo ./scripts/harden-host.sh'. MemoryHigh throttles before the
# hard cap so builds slow down instead of dying at the first spike.
[Unit]
Description=razzfazz.ai agent containers memory budget (#692)

[Slice]
MemoryMax=${budget_gb}G
MemoryHigh=$(( budget_gb * 90 / 100 ))G
EOF
    systemctl daemon-reload 2>/dev/null || true
    ok "razzfazz-agents.slice installed (MemoryMax=${budget_gb}G; agents OOM inside the slice, not the host)."
}

harden_agent_slice

# ==============================================================================
# 7c. CLOUD METADATA BLOCK (DOCKER-USER)
# ==============================================================================
# Containers must not reach the cloud metadata addresses (instance
# credentials). scripts/metadata-block.sh owns the rule; init and every upgrade
# run it too, so a box hardened before it existed still gets it.

step "Cloud metadata block for containers (DOCKER-USER)"

harden_metadata_block() {
    if $DRY_RUN; then
        dry "Install the DOCKER-USER rule dropping 169.254.0.0/16, 100.100.100.200 and fd00:ec2::254 for containers (scripts/metadata-block.sh install)"
        return 0
    fi
    if RAZZFAZZ_ENV_FILE="$STACK_DIR/.env" bash "$SCRIPT_DIR/metadata-block.sh" install; then
        ok "Cloud metadata addresses blocked for containers."
    else
        warn "Cloud metadata block NOT applied; run: sudo bash $SCRIPT_DIR/metadata-block.sh install"
    fi
}

harden_metadata_block

# ==============================================================================
# 8. SERVICE MINIMIZATION (bonus)
# ==============================================================================

step "Bonus: Service Minimization"

minimize_services() {
    if $DRY_RUN; then
        dry "Disable unnecessary services: cups-browsed, avahi-daemon, ModemManager, bluetooth, whoopsie, apport"
        return
    fi

    local disabled=0
    for svc in cups-browsed avahi-daemon ModemManager bluetooth whoopsie apport; do
        if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            systemctl disable --now "$svc" 2>/dev/null || true
            disabled=$((disabled + 1))
        fi
    done

    if [[ $disabled -gt 0 ]]; then
        ok "Disabled $disabled unnecessary services"
    else
        ok "No unnecessary services found to disable"
    fi
}

minimize_services

# ==============================================================================
# SUMMARY
# ==============================================================================

echo ""
step "Hardening Complete"

if [ "$HARDENING_PENDING_REBOOT" = true ]; then
    echo ""
    warn "HARDENING INCOMPLETE: daemon.json was applied via reload (a live stack was"
    warn "running, so a full restart was avoided to prevent downtime). The"
    warn "no-new-privileges default does NOT take effect until Docker"
    warn "is restarted — this box is NOT fully hardened until you restart Docker or reboot."
fi

if $DRY_RUN; then
    info "Dry run complete. No changes were made."
    info "Run without --dry-run to apply all hardening measures."
else
    # Success marker — razzfazz-upgrade.sh checks for this file to avoid
    # auto-rerunning hardening on every upgrade. Records the timestamp,
    # the script version that ran, and which optional gates were applied.
    mkdir -p /etc/razzfazz
    cat > /etc/razzfazz/host-hardened <<EOF
# razzfazz.ai host hardening marker — written by scripts/harden-host.sh
# Presence of this file tells razzfazz-upgrade.sh that hardening has been
# applied at least once. Re-run harden-host.sh manually if you want to
# refresh sysctl/ufw rules to the latest version-controlled config.
hardened_at=$(date -Iseconds)
hardened_by=$(id -un -- "$SUDO_USER" 2>/dev/null || id -un)
script_version=2026.05-rc5.1+
restrict_ssh=$RESTRICT_SSH
auto_reboot=$NO_AUTO_REBOOT
skipped_packages=${HARDEN_SKIPPED}
EOF
    chmod 644 /etc/razzfazz/host-hardened
    if [ -n "$HARDEN_SKIPPED" ]; then
        warn "Hardening applied EXCEPT the items that need: ${HARDEN_SKIPPED} (not installable here, see above)."
    else
        ok "All hardening measures applied successfully."
    fi
    echo ""
    info "Verification commands:"
    echo "  ufw status verbose                              # Firewall status"
    if ! $SKIP_SSH; then
        echo "  sshd -T | grep -E 'passwordauth|permitroot'    # SSH config"
        echo "  fail2ban-client status sshd                     # fail2ban status"
    fi
    echo "  unattended-upgrades --dry-run --debug 2>&1 | head -20  # Auto-updates"
    echo "  sysctl kernel.randomize_va_space kernel.dmesg_restrict  # Sysctl"
    echo "  auditctl -l                                       # Audit rules"
    echo "  stat -c '%a %U:%G %n' \"$STACK_DIR/.env\"           # File permissions"
    echo "  docker info 2>/dev/null | grep -i 'no-new-priv'   # Docker daemon"
    echo "  bash $SCRIPT_DIR/metadata-block.sh status          # Cloud metadata block"
    echo ""
    warn "If this is a remote server, verify SSH access in a NEW terminal"
    warn "before closing your current session!"
fi
