#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# razzfazz-host-upgrade.sh
#
# Idempotent host-OS upgrade for razzfazz.ai AI-inference boxes.
#
# STATUS (#16) — LEGACY: this is the Ubuntu-24.04-era box baseline path (kernel +
# ROCm). The fleet now ships on Ubuntu 26.04 LTS and this script's running-stack
# guard is disabled on 26.04. For the DISTRO upgrade 24.04 → 26.04, use
# `scripts/razzfazz-upgrade-os.sh`. Kept for reference / pre-26.04 boxes only.
# Brings a freshly-installed AMD Strix Halo (gfx1151) box from the
# `razzfazz-ai-box-setup.sh` baseline (kernel 6.14.0-37 held + ROCm 6.4 DKMS)
# to the production target documented in .gsd/milestones/M018:
#
#   - kernel `linux-oem-24.04d` (currently 6.17.0-1017-oem), security-tracked
#   - ROCm 7.2.x installed via `amdgpu-install --no-dkms` (in-tree amdgpu)
#   - GRUB_DEFAULT=0 (boots newest installed kernel; future OEM bumps land
#     automatically via Canonical's security train)
#   - `linux-image-*` blacklist lifted from /etc/apt/apt.conf.d/50unattended-
#     upgrades so kernel security updates flow unattended
#
# This is the automation of M018 S01.3 + S01.5, generalised so it can run
# on any AMD Strix Halo box. Three stages, two reboots:
#
#   A  pre-reboot   ROCm 6.4 → 7.2 (DKMS uninstall + 7.2 install --no-dkms)
#       reboot 1    boot 6.14, swap live DKMS module → in-tree amdgpu.ko
#   B  post-reboot1 sanity-gate ROCm 7.2 in-tree, then kernel: unhold 6.14,
#                   install linux-oem-24.04d, GRUB_DEFAULT=0, update-grub,
#                   lift blacklist
#       reboot 2    boot linux-oem-24.04d
#   C  post-reboot2 sanity-gate kernel + ROCm together (M018 S01.4 gate),
#                   clean up, disable resume unit, exit 0
#
# Resume across reboots is via a one-shot systemd unit installed in stage A
# and disabled in stage C (matches the existing razzfazz-ai-box-setup.sh
# pattern but uses systemd instead of cron for clean disable-on-success).
#
# Usage:
#   sudo ./razzfazz-host-upgrade.sh                    # full run
#   sudo ./razzfazz-host-upgrade.sh --dry-run          # show what would happen
#   sudo ./razzfazz-host-upgrade.sh --force            # skip running-stack guard
#   sudo ./razzfazz-host-upgrade.sh --stage B          # manual stage override
#   sudo ./razzfazz-host-upgrade.sh --amdgpu-install-deb /path/to.deb
#                                                      # use local deb instead
#                                                      # of fetching from AMD
#
# WARNING: This script reboots the machine TWICE.
#          Run from a persistent session (SSH + nohup/screen, or as systemd
#          unit). The state file at /var/tmp/.razzfazz-host-upgrade-stage
#          tracks progress and survives reboots.
#
# Rollback: GRUB submenu still has 6.14.0-37-generic. To revert, boot it
#           from the menu, then `apt-mark hold` the 6.14 packages and
#           re-install ROCm 6.4 DKMS via the deb in llm/amd/.
#
# Reference: .gsd/milestones/M018/M018-ROADMAP.md (S01.3, S01.4, S01.5)
# =============================================================================

set -euo pipefail

# === SAFETY GUARD (2026.06-ga.6): legacy Ubuntu-24.04 host script, DISABLED =====
# This upgrades the host FROM the old 24.04 + held-6.14-kernel + ROCm-DKMS baseline
# TO an oem kernel / ROCm 7.2 — three stages, two reboots, kernel + GRUB surgery.
# The fleet now ships on Ubuntu 26.04 LTS, whose Canonical-SIGNED kernel enables
# AMD Strix Halo (gfx1151) natively and boots under Secure Boot. Running this on a
# 26.04 box performs the wrong kernel/ROCm surgery and can leave the box UNBOOTABLE.
# The appliance install already prepares the host; you do not need this script.
if [ "${RAZZFAZZ_ALLOW_LEGACY_HOST_SCRIPT:-0}" != "1" ]; then
    echo "[x] DISABLED: $(basename "$0") is a legacy Ubuntu-24.04 host script and is unsafe" >&2
    echo "    on Ubuntu 26.04 (the signed distro kernel already supports Strix Halo). It can" >&2
    echo "    leave the box unbootable. The appliance install already prepared the host." >&2
    echo "    Vendor emergency use only: re-run with RAZZFAZZ_ALLOW_LEGACY_HOST_SCRIPT=1" >&2
    exit 1
fi
# ================================================================================

# Non-interactive apt: kernel install would otherwise prompt for grub
# config diffs / dpkg conffiles and stall the unattended flow.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a    # auto-restart services without prompting

# ---- Configuration ----------------------------------------------------------
ROCM_VERSION_TARGET="7.2"
AMDGPU_INSTALL_DEB_URL="https://repo.radeon.com/amdgpu-install/${ROCM_VERSION_TARGET}/ubuntu/noble/amdgpu-install_7.2.70200-1_all.deb"
AMDGPU_INSTALL_DEB_CACHE="/var/cache/razzfazz/amdgpu-install_7.2.70200-1_all.deb"

OEM_KERNEL_META="linux-oem-24.04d"  # M018 documented head; script auto-detects newer
OLD_KERNEL_VER="6.14.0-37-generic"  # what `razzfazz-ai-box-setup.sh` pins to

GPU_PCI_ID="1002:1586"              # AMD Strix Halo gfx1151
EXPECTED_GFX="gfx1151"

STATE_FILE="/var/tmp/.razzfazz-host-upgrade-stage"
LOG_FILE="/var/log/razzfazz-host-upgrade.log"
SYSTEMD_UNIT="/etc/systemd/system/razzfazz-host-upgrade-resume.service"
UNATTENDED_CONF="/etc/apt/apt.conf.d/50unattended-upgrades"
GRUB_CONF="/etc/default/grub"

# ---- CLI flags --------------------------------------------------------------
DRY_RUN=false
FORCE=false
STAGE_OVERRIDE=""
AMDGPU_INSTALL_DEB_OVERRIDE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)              DRY_RUN=true; shift ;;
        --force)                FORCE=true; shift ;;
        --stage)                STAGE_OVERRIDE="$2"; shift 2 ;;
        --amdgpu-install-deb)   AMDGPU_INSTALL_DEB_OVERRIDE="$2"; shift 2 ;;
        -h|--help)              sed -n '2,/^# ===/p' "$0" | head -60; exit 0 ;;
        *) echo "Unknown flag: $1" >&2; exit 2 ;;
    esac
done

# ---- Helpers ----------------------------------------------------------------
# M026 / S02 #10: this script intentionally does NOT source scripts/lib.sh.
# It runs on bare metal during host upgrades (often pre-stack-up, sometimes
# unattended via systemd unit) where the CLI-style colored print_* output
# from lib.sh adds no value but the per-line `[YYYY-MM-DD HH:MM:SS]` timestamp
# + tee-to-LOG_FILE pattern is the operationally important behavior. Migrating
# would mean either rewriting ~70 log/info/ok/warn/die call sites (loses
# timestamp format + adds noisy ANSI codes) or extending lib.sh with sysadmin-
# style helpers (out-of-scope for S02; tracked as task #146 follow-up).
log()    { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
info()   { log "INFO:  $*"; }
ok()     { log "OK:    $*"; }
warn()   { log "WARN:  $*"; }
die()    { log "FATAL: $*"; exit 1; }
banner() {
    echo "" | tee -a "$LOG_FILE"
    echo "===============================================================" | tee -a "$LOG_FILE"
    echo "  $*" | tee -a "$LOG_FILE"
    echo "===============================================================" | tee -a "$LOG_FILE"
}
run() {
    if $DRY_RUN; then
        log "DRY:   $*"
    else
        log "RUN:   $*"
        "$@" 2>&1 | tee -a "$LOG_FILE"
    fi
}

get_stage() { [[ -f "$STATE_FILE" ]] && cat "$STATE_FILE" || echo "A"; }
set_stage() { $DRY_RUN || echo "$1" > "$STATE_FILE"; log "→ stage = $1"; }

# ---- Pre-flight checks (always run) ----------------------------------------
if [[ $EUID -ne 0 ]] && ! $DRY_RUN; then
    die "Must run as root (sudo). Pass --dry-run to plan without root."
fi

# Dry-run as non-root: fall back to a user-writable log location
if [[ $EUID -ne 0 ]] && $DRY_RUN; then
    LOG_FILE="/tmp/razzfazz-host-upgrade.log"
fi

# Hardware gate: refuse on non-AMD-Strix-Halo. Capture first to dodge
# pipefail+grep-q SIGPIPE: grep -q exits on first match, lspci dies of
# SIGPIPE, pipefail propagates non-zero, the negation flips it.
gpu_match=$(lspci -nn 2>/dev/null | grep -F "$GPU_PCI_ID" || true)
if [[ -z "$gpu_match" ]]; then
    die "GPU $GPU_PCI_ID (AMD Strix Halo gfx1151) not detected on this host. \
Refusing to run — this script is AMD-Strix-Halo-specific."
fi

# Stack gate: refuse if any containers are running
if ! $FORCE && command -v docker >/dev/null 2>&1; then
    running_containers=$(docker ps --format '{{.Names}}' 2>/dev/null || true)
    if [[ -n "$running_containers" ]]; then
        die "Docker containers are running ($(echo "$running_containers" | wc -l)). \
Run \`docker compose down\` from the stack root, then re-run this script. \
(Use --force to override at your own risk.)"
    fi
fi

if [[ $EUID -eq 0 ]]; then
    mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$AMDGPU_INSTALL_DEB_CACHE")"
fi
touch "$LOG_FILE" 2>/dev/null || true

# Determine stage (CLI override wins)
CURRENT_STAGE="${STAGE_OVERRIDE:-$(get_stage)}"

banner "razzfazz-host-upgrade.sh — stage $CURRENT_STAGE"
info "host:           $(hostname) ($(hostname -I | awk '{print $1}'))"
info "running kernel: $(uname -r)"
info "ROCm version:   $(cat /opt/rocm/.info/version 2>/dev/null || echo 'not installed')"
info "amdgpu module:  $(modinfo amdgpu 2>/dev/null | awk '/^filename/{print $2}' || echo 'n/a')"
info "dry-run:        $DRY_RUN"
info "force:          $FORCE"

# =============================================================================
# Helper: install the systemd resume unit (called by stage A and B)
# =============================================================================
install_resume_unit() {
    local script_path
    script_path=$(readlink -f "$0")
    if $DRY_RUN; then
        log "DRY:   write systemd unit $SYSTEMD_UNIT pointing at $script_path"
        return
    fi
    cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=razzfazz.ai host-upgrade — resume after reboot
After=network-online.target docker.service
Wants=network-online.target
ConditionPathExists=$STATE_FILE

[Service]
Type=oneshot
ExecStart=/usr/bin/bash $script_path
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable razzfazz-host-upgrade-resume.service >/dev/null 2>&1
    ok "systemd resume unit installed and enabled"
}

disable_resume_unit() {
    if $DRY_RUN; then
        log "DRY:   disable + remove systemd resume unit"
        return
    fi
    systemctl disable razzfazz-host-upgrade-resume.service >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_UNIT"
    systemctl daemon-reload
    ok "systemd resume unit removed"
}

# =============================================================================
# Helper: fetch the amdgpu-install 7.2 deb (cache-aware)
# =============================================================================
# Sets global AMDGPU_DEB_PATH on success. (Setting a global rather than
# returning via stdout so the function's info/log lines don't get sucked
# into a $() capture and mixed with the return value.)
AMDGPU_DEB_PATH=""
fetch_amdgpu_install_deb() {
    if [[ -n "$AMDGPU_INSTALL_DEB_OVERRIDE" ]]; then
        [[ -f "$AMDGPU_INSTALL_DEB_OVERRIDE" ]] || die "--amdgpu-install-deb \
not found: $AMDGPU_INSTALL_DEB_OVERRIDE"
        AMDGPU_DEB_PATH="$AMDGPU_INSTALL_DEB_OVERRIDE"
        return
    fi
    if [[ -f "$AMDGPU_INSTALL_DEB_CACHE" ]]; then
        info "using cached amdgpu-install deb: $AMDGPU_INSTALL_DEB_CACHE"
        AMDGPU_DEB_PATH="$AMDGPU_INSTALL_DEB_CACHE"
        return
    fi
    info "fetching $AMDGPU_INSTALL_DEB_URL"
    if $DRY_RUN; then
        AMDGPU_DEB_PATH="$AMDGPU_INSTALL_DEB_CACHE"
        return
    fi
    curl -fsSL "$AMDGPU_INSTALL_DEB_URL" -o "$AMDGPU_INSTALL_DEB_CACHE.tmp" \
        || die "Failed to fetch $AMDGPU_INSTALL_DEB_URL — pass --amdgpu-install-deb \
/path/to.deb to use a local copy."
    mv "$AMDGPU_INSTALL_DEB_CACHE.tmp" "$AMDGPU_INSTALL_DEB_CACHE"
    ok "amdgpu-install deb cached at $AMDGPU_INSTALL_DEB_CACHE"
    AMDGPU_DEB_PATH="$AMDGPU_INSTALL_DEB_CACHE"
}

# =============================================================================
# Helper: M018 S01.4 sanity gate (ROCm side)
# =============================================================================
sanity_rocm() {
    local rocm_ver gfx_present amdgpu_path
    rocm_ver=$(cat /opt/rocm/.info/version 2>/dev/null || echo "unknown")
    gfx_present=$(rocminfo 2>/dev/null | grep -c "Name:.*${EXPECTED_GFX}" || true)
    amdgpu_path=$(modinfo amdgpu 2>/dev/null | awk '/^filename/{print $2}' || echo "")

    info "ROCm version:    $rocm_ver"
    info "gfx1151 visible: $gfx_present matches in rocminfo"
    info "amdgpu path:     $amdgpu_path"

    if $DRY_RUN; then
        warn "(dry-run) skipping ROCm sanity assertions — would check: 7.2.x version, \
${EXPECTED_GFX} visible, amdgpu not under updates/dkms/"
        return
    fi
    [[ "$rocm_ver" == 7.2* ]] || die "ROCm sanity FAIL: expected 7.2.x, got $rocm_ver"
    [[ "$gfx_present" -ge 1 ]] || die "ROCm sanity FAIL: rocminfo does not see ${EXPECTED_GFX}"
    if [[ "$amdgpu_path" == */updates/dkms/* ]]; then
        die "ROCm sanity FAIL: amdgpu module is still under updates/dkms/ \
($amdgpu_path) — DKMS→in-tree swap did not happen. Reboot may have used the \
old initramfs. Try: depmod -a; update-initramfs -u; reboot."
    fi
    ok "ROCm 7.2 in-tree path verified"
}

# =============================================================================
# Helper: M018 S01.4 sanity gate (kernel side)
# =============================================================================
sanity_kernel() {
    local running
    running=$(uname -r)
    info "running kernel: $running"
    if $DRY_RUN; then
        warn "(dry-run) skipping kernel sanity — would check: 6.17+ OEM, *-oem suffix"
        return
    fi
    if [[ ! "$running" =~ ^6\.(1[7-9]|[2-9][0-9])\. ]] && [[ "$running" != *-oem ]]; then
        die "Kernel sanity FAIL: expected 6.17+ OEM, got $running. \
Check GRUB: grep ^GRUB_DEFAULT $GRUB_CONF; for menuentry in /boot; ls /boot/vmlinuz-*."
    fi
    ok "kernel $running matches OEM target"
}

# =============================================================================
# STAGE A — ROCm 6.4 DKMS  →  ROCm 7.2 in-tree
# =============================================================================
stage_a() {
    banner "STAGE A — ROCm 7.2 install (--no-dkms)"

    # Skip if ROCm 7.2 already installed AND amdgpu is already in-tree
    if [[ "$(cat /opt/rocm/.info/version 2>/dev/null || echo)" == 7.2* ]] \
       && ! modinfo amdgpu 2>/dev/null | grep -q "/updates/dkms/"; then
        ok "ROCm 7.2 already installed and in-tree — skipping stage A"
        set_stage "B"
        # No reboot — drop straight into stage B
        stage_b
        return
    fi

    info "Step 1/4: uninstall ROCm 6.4 (DKMS removal)"
    # Three paths, in order of preference:
    #   1. `amdgpu-uninstall -y`            — convenience binary (7.2 deb ships it)
    #   2. `amdgpu-install --uninstall -y`  — AMD-documented canonical path (works on 6.4)
    #   3. `apt purge amdgpu-dkms ...`      — last-resort, brute-force
    if [[ -x /usr/bin/amdgpu-uninstall ]]; then
        run /usr/bin/amdgpu-uninstall -y
    elif [[ -x /usr/bin/amdgpu-install ]]; then
        run /usr/bin/amdgpu-install -y --uninstall
    elif dpkg -s amdgpu-dkms >/dev/null 2>&1 || dpkg -s amdgpu-install >/dev/null 2>&1; then
        warn "no amdgpu-install/uninstall binary found — falling back to apt purge"
        run apt-get purge -y amdgpu-install amdgpu-dkms || true
    else
        warn "no ROCm/amdgpu install detected — skipping uninstall step"
    fi

    info "Step 2/4: fetch amdgpu-install 7.2 deb"
    fetch_amdgpu_install_deb

    info "Step 3/4: install amdgpu-install deb + add 7.2 repo"
    run apt-get install -y "$AMDGPU_DEB_PATH"

    info "Step 4/4: amdgpu-install --no-dkms --usecase=graphics,rocm"
    run amdgpu-install -y --no-dkms --usecase=graphics,rocm

    install_resume_unit
    set_stage "B"
    banner "Stage A complete — REBOOT 1 (DKMS → in-tree amdgpu)"
    if $DRY_RUN; then
        log "DRY:   would reboot here"
        return
    fi
    sleep 3
    reboot
}

# =============================================================================
# STAGE B — verify ROCm 7.2 in-tree + kernel swap to linux-oem-24.04d
# =============================================================================
stage_b() {
    banner "STAGE B — verify ROCm + kernel swap to OEM"

    info "Sanity gate: ROCm side (post reboot 1)"
    sanity_rocm

    info "Step 1/5: unhold 6.14 kernel packages"
    run apt-mark unhold \
        "linux-image-${OLD_KERNEL_VER}" \
        "linux-headers-${OLD_KERNEL_VER}" \
        "linux-modules-${OLD_KERNEL_VER}" \
        "linux-modules-extra-${OLD_KERNEL_VER}" \
        || true

    info "Step 2/5: purge orphaned non-OEM 6.17 generic if present"
    if dpkg -l | awk '$1=="rc"{print $2}' | grep -q "^linux-image-6\.17\..*-generic$"; then
        # shellcheck disable=SC2046
        run dpkg --purge $(dpkg -l | awk '$1=="rc"{print $2}' | grep "^linux-image-6\.17\..*-generic$" || true) || true
    fi

    info "Step 3/5: detect newest non-transitional linux-oem-24.04 meta"
    # Auto-detect: walk linux-oem-24.04{d,e,f,...} and pick the newest one
    # whose Description is NOT "transitional package".
    local meta_pkg=""
    if ! $DRY_RUN; then
        apt-get update -y >/dev/null 2>&1 || warn "apt-get update returned non-zero — proceeding anyway"
    fi
    for suffix in d e f g h; do
        local cand="linux-oem-24.04${suffix}"
        local desc
        desc=$(apt-cache show "$cand" 2>/dev/null | awk '/^Description-en:/{$1=""; print; exit}' || true)
        if [[ -n "$desc" ]] && [[ "$desc" != *transitional* ]]; then
            meta_pkg="$cand"
        fi
    done
    [[ -n "$meta_pkg" ]] || meta_pkg="$OEM_KERNEL_META"
    info "selected OEM kernel meta: $meta_pkg"

    info "Step 4/5: install $meta_pkg (leaves it unheld, security-tracked)"
    run apt-get install -y "$meta_pkg"

    info "Step 5/5: GRUB_DEFAULT=0 + lift kernel blacklist + update-grub"
    if grep -q '^GRUB_DEFAULT=' "$GRUB_CONF"; then
        run sed -i 's|^GRUB_DEFAULT=.*|GRUB_DEFAULT=0|' "$GRUB_CONF"
    else
        run bash -c "echo 'GRUB_DEFAULT=0' >> $GRUB_CONF"
    fi
    # Saved-default off — use first menu entry, which on Ubuntu is the newest installed kernel
    if grep -q '^GRUB_SAVEDEFAULT=' "$GRUB_CONF"; then
        run sed -i 's|^GRUB_SAVEDEFAULT=.*|GRUB_SAVEDEFAULT=false|' "$GRUB_CONF"
    fi
    run update-grub

    # Lift kernel blacklist from unattended-upgrades so OEM security
    # updates flow. Comment-out rather than delete so an operator can see
    # the prior policy (and it's also reversible).
    if [[ -f "$UNATTENDED_CONF" ]] && grep -q '^\s*"linux-image-\*";' "$UNATTENDED_CONF"; then
        if $DRY_RUN; then
            log "DRY:   comment-out linux-{image,headers,modules}-* in $UNATTENDED_CONF"
        else
            sed -i \
                -e 's|^\(\s*\)\("linux-image-\*";\)|\1// \2 // razzfazz-host-upgrade: lifted; OEM kernel auto-tracks Canonical security train|' \
                -e 's|^\(\s*\)\("linux-headers-\*";\)|\1// \2 // razzfazz-host-upgrade: lifted|' \
                -e 's|^\(\s*\)\("linux-modules-\*";\)|\1// \2 // razzfazz-host-upgrade: lifted|' \
                "$UNATTENDED_CONF"
            ok "kernel blacklist lifted in $UNATTENDED_CONF"
        fi
    else
        info "kernel blacklist not present in $UNATTENDED_CONF — nothing to lift"
    fi

    set_stage "C"
    banner "Stage B complete — REBOOT 2 (boot OEM kernel)"
    if $DRY_RUN; then
        log "DRY:   would reboot here"
        return
    fi
    sleep 3
    reboot
}

# =============================================================================
# STAGE C — verify kernel + ROCm together, clean up
# =============================================================================
stage_c() {
    banner "STAGE C — final sanity gate + cleanup"

    info "Sanity gate: kernel side (post reboot 2)"
    sanity_kernel

    info "Sanity gate: ROCm side (re-verify against new kernel)"
    sanity_rocm

    info "Cleanup"
    disable_resume_unit
    if ! $DRY_RUN; then
        rm -f "$STATE_FILE"
    fi
    ok "state file cleared"

    banner "razzfazz-host-upgrade.sh — DONE"
    info "kernel:    $(uname -r)"
    info "ROCm:      $(cat /opt/rocm/.info/version 2>/dev/null)"
    info "amdgpu:    $(modinfo amdgpu | awk '/^filename/{print $2}')"
    info "blacklist: kernel auto-updates ENABLED via OEM security train"
    info "log:       $LOG_FILE"
    info ""
    info "Next: bring the stack back up:"
    info "  cd ~/razzfazz-ai-service-stack && docker compose up -d"
}

# =============================================================================
# Dispatch
# =============================================================================
case "$CURRENT_STAGE" in
    A) stage_a ;;
    B) stage_b ;;
    C) stage_c ;;
    *) die "Unknown stage: $CURRENT_STAGE (expected A, B, or C)" ;;
esac
