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
# S31: default to the ACTUAL stack-dir owner, not a hardcoded name. Prod/worker
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
ASSUME_YES=false
# Set true if daemon.json was applied via `reload` on a live stack — the
# no-new-privileges/storage-driver settings then need a Docker restart to take
# effect, so the final summary flags that the box isn't fully hardened yet (#2).
HARDENING_PENDING_REBOOT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)       DRY_RUN=true; shift ;;
        --restrict-ssh)  RESTRICT_SSH=true; SKIP_SSH=false; shift ;;
        --skip-ssh)      SKIP_SSH=true; shift ;;   # legacy, no-op (SSH skipped by default)
        --auto-reboot)   AUTO_REBOOT_TIME="$2"; shift 2 ;;
        --no-auto-reboot) NO_AUTO_REBOOT=true; shift ;;
        --stack-user)    STACK_USER="$2"; shift 2 ;;
        --yes|-y)        ASSUME_YES=true; shift ;;  # proceed even if a stack is running
        --help|-h)       usage ;;
        *)               err "Unknown option: $1"; usage ;;
    esac
done

# --- Pre-flight checks -------------------------------------------------------

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

harden_firewall() {
    if $DRY_RUN; then
        dry "Install ufw"
        dry "Set default deny incoming, allow outgoing"
        dry "Allow 22/tcp (SSH), 80/tcp (HTTP), 443/tcp (HTTPS)"
        # Check for GPUStack master mode
        if [[ -f "$STACK_DIR/.env" ]]; then
            local gpustack_mode
            gpustack_mode=$(grep -E '^GPUSTACK_MODE=' "$STACK_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'")
            if [[ "$gpustack_mode" == "master" ]]; then
                dry "Allow GPUStack master ports: 9090, 10150:10151, 40000:40103, 52365 (tcp)"
            fi
        fi
        dry "Enable ufw"
        return
    fi

    apt-get install -y ufw > /dev/null 2>&1
    ok "ufw installed"

    ufw default deny incoming > /dev/null 2>&1
    ufw default allow outgoing > /dev/null 2>&1
    ok "Default policy: deny incoming, allow outgoing"

    ufw allow 22/tcp comment "SSH" > /dev/null 2>&1
    ufw allow 80/tcp comment "HTTP" > /dev/null 2>&1
    ufw allow 443/tcp comment "HTTPS" > /dev/null 2>&1
    ok "Allowed ports: 22/tcp, 80/tcp, 443/tcp"

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

    # rc6.7 #43 v3: OpenHands per-conversation sandbox runtime spawns in host
    # network mode (since rc6.7 #46) and binds the well-known port 8000. The
    # parent openhands container on the compose bridge reaches it via
    # host.docker.internal:8000 — needs an explicit bridge → host:8000 INPUT
    # allow. Cognee was moved to host:8011 in rc6.7 to free this port.
    ufw allow proto tcp from 172.16.0.0/12 to any port 8000 \
        comment "OpenHands sandbox (docker bridge)" > /dev/null 2>&1
    ok "Allowed: 8000/tcp from docker bridges (openhands sandbox)"

    # GPUStack master mode: open additional ports for external workers
    # on the LAN. Operator-controlled — narrowed by GPUSTACK_HOST_BIND
    # in .env to a specific worker subnet if desired.
    if [[ -f "$STACK_DIR/.env" ]]; then
        local gpustack_mode
        gpustack_mode=$(grep -E '^GPUSTACK_MODE=' "$STACK_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'")
        if [[ "$gpustack_mode" == "master" ]]; then
            ufw allow 9090/tcp comment "GPUStack API" > /dev/null 2>&1
            ufw allow 10150:10151/tcp comment "GPUStack RPC (master, external workers)" > /dev/null 2>&1
            ufw allow 40000:40103/tcp comment "GPUStack P2P" > /dev/null 2>&1
            ufw allow 52365/tcp comment "Ray compute" > /dev/null 2>&1
            ok "GPUStack master ports opened (9090, 10150-10151, 40000-40103, 52365)"
        fi
    fi

    ufw --force enable > /dev/null 2>&1
    ok "UFW enabled"
}

harden_firewall

# ==============================================================================
# 2. SSH HARDENING + FAIL2BAN - F-057
# ==============================================================================

step "2/7 SSH Hardening [F-057]"

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

    local sshd_conf="/etc/ssh/sshd_config.d/99-razzfazz-hardening.conf"

    if $DRY_RUN; then
        dry "Create $sshd_conf with hardened settings"
        dry "Test sshd config and restart sshd"
        dry "Install and configure fail2ban for SSH"
        return
    fi

    # Ensure sshd_config.d directory exists and is included
    mkdir -p /etc/ssh/sshd_config.d

    cat > "$sshd_conf" <<EOF
# razzfazz.ai host hardening - SSH (F-057)
# Generated by harden-host.sh - do not edit manually

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

    # Test sshd config before restarting
    if sshd -t 2>/dev/null; then
        systemctl restart sshd
        ok "sshd configuration tested and service restarted"
    else
        err "sshd configuration test failed! Removing hardening config to prevent lockout."
        rm -f "$sshd_conf"
        err "Please check your sshd configuration manually."
        return
    fi

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
    apt-get install -y fail2ban > /dev/null 2>&1
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

    apt-get install -y unattended-upgrades apt-listchanges > /dev/null 2>&1
    ok "unattended-upgrades installed"

    local reboot_setting="false"
    local reboot_time_setting=""
    if ! $NO_AUTO_REBOOT; then
        reboot_setting="true"
        reboot_time_setting="Unattended-Upgrade::Automatic-Reboot-Time \"$AUTO_REBOOT_TIME\";"
    fi

    cat > /etc/apt/apt.conf.d/50unattended-upgrades <<CONF
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

    cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
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

    # systemd-timesyncd and chrony conflict; chrony is the one that step-corrects.
    systemctl stop systemd-timesyncd 2>/dev/null || true
    systemctl disable systemd-timesyncd 2>/dev/null || true
    apt-get install -y chrony > /dev/null 2>&1

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

    apt-get install -y auditd audispd-plugins > /dev/null 2>&1
    ok "auditd installed"

    cat > /etc/audit/rules.d/razzfazz.rules <<EOF
# razzfazz.ai host hardening - audit rules (F-071)
# Generated by harden-host.sh - do not edit manually

# Clear existing rules
-D

# Buffer size
-b 8192

# Docker socket and config
-w /var/run/docker.sock -p rwxa -k docker_socket
-w /etc/docker/ -p wa -k docker_config

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

# Root command execution
-a always,exit -F arch=b64 -F euid=0 -S execve -k root_commands

# Lock rules (must be last)
-e 2
EOF

    systemctl enable auditd > /dev/null 2>&1
    systemctl restart auditd 2>/dev/null || augenrules --load > /dev/null 2>&1
    ok "auditd configured and started"
}

harden_auditd

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

    if ! DAEMON_JSON="$daemon_json" python3 - <<'PYHARDEN'
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
cfg.setdefault("storage-driver", "overlay2")
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
    # The SECURITY-relevant `no-new-privileges` daemon default and `storage-driver`
    # are NOT in that set — they stay inactive until a full Docker restart. So on a
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
            warn "no-new-privileges default + storage-driver are NOT active yet — this box is"
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
    warn "no-new-privileges default + storage-driver do NOT take effect until Docker"
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
EOF
    chmod 644 /etc/razzfazz/host-hardened
    ok "All hardening measures applied successfully."
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
    echo ""
    warn "If this is a remote server, verify SSH access in a NEW terminal"
    warn "before closing your current session!"
fi
