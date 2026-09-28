#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# razzfazz-ai-box-setup.sh
# Setup script for razzfazz-ai AI inference server (HP Z2 Mini G1a)
#   - Ubuntu 24.04 LTS
#   - AMD Ryzen AI Max+ PRO 395 / Radeon 8060S (gfx1151)
#   - Pins kernel to 6.14, installs amdgpu/ROCm 6.4.2, Docker
#
# Usage:
#   sudo bash razzfazz-ai-box-setup.sh
#
# Prerequisites:
#   - Fresh Ubuntu 24.04 LTS installation
#   - amdgpu-install deb at:
#     /home/razzfazz-ai-admin/razzfazz-ai-service-stack/llm/amd/amdgpu-install_6.4.60402-1_all.deb
#   - Internet connectivity
#
# WARNING: This script reboots the machine twice.
#          Run it from a persistent session (e.g., SSH with nohup or screen).
# =============================================================================

set -euo pipefail

# === SAFETY GUARD (2026.06-ga.6): legacy Ubuntu-24.04 host script, DISABLED =====
# This prepares the host for the OLD baseline: Ubuntu 24.04 with a held 6.14 kernel
# and ROCm installed as a DKMS module. The fleet now ships on Ubuntu 26.04 LTS,
# whose Canonical-SIGNED kernel enables AMD Strix Halo (gfx1151) natively and boots
# under Secure Boot. Running this on a 26.04 box pins an old kernel, reinstalls ROCm
# DKMS, and reboots twice — it can leave the box UNBOOTABLE. The appliance install
# already prepares the host; you do not need this script.
if [ "${RAZZFAZZ_ALLOW_LEGACY_HOST_SCRIPT:-0}" != "1" ]; then
    echo "[x] DISABLED: $(basename "$0") is a legacy Ubuntu-24.04 host script and is unsafe" >&2
    echo "    on Ubuntu 26.04 (the signed distro kernel already supports Strix Halo). It can" >&2
    echo "    leave the box unbootable. The appliance install already prepared the host." >&2
    echo "    Vendor emergency use only: re-run with RAZZFAZZ_ALLOW_LEGACY_HOST_SCRIPT=1" >&2
    exit 1
fi
# ================================================================================

# ---- Configuration ----------------------------------------------------------
ADMIN_USER="razzfazz-ai-admin"
AMDGPU_DEB="/home/${ADMIN_USER}/razzfazz-ai-service-stack/llm/amd/amdgpu-install_6.4.60402-1_all.deb"
TARGET_KERNEL_MAJOR="6.14"
STATE_FILE="/var/tmp/.razzfazz-setup-stage"
LOG_FILE="/var/log/razzfazz-ai-box-setup.log"

# ---- Helpers ----------------------------------------------------------------
# M026 / S02 #10: this script intentionally does NOT source scripts/lib.sh.
# It's a bootstrap installer that runs BEFORE the stack repo is laid out
# (it CREATES the user + clones the repo + pre-installs deps). Sourcing
# lib.sh would create a bootstrap chicken-and-egg. Same logging-idiom
# rationale as razzfazz-host-upgrade.sh applies. Tracked as task #146.
log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
die()  { log "FATAL: $*"; exit 1; }
banner() {
    echo "" | tee -a "$LOG_FILE"
    echo "======================================================================" | tee -a "$LOG_FILE"
    echo "  $*" | tee -a "$LOG_FILE"
    echo "======================================================================" | tee -a "$LOG_FILE"
    echo "" | tee -a "$LOG_FILE"
}

get_stage() {
    if [[ -f "$STATE_FILE" ]]; then
        cat "$STATE_FILE"
    else
        echo "0"
    fi
}

set_stage() {
    echo "$1" > "$STATE_FILE"
}

# ---- Pre-flight checks -----------------------------------------------------
[[ $EUID -eq 0 ]] || die "This script must be run as root (sudo)."
[[ -f "$AMDGPU_DEB" ]] || die "amdgpu-install deb not found at: $AMDGPU_DEB"

CURRENT_STAGE=$(get_stage)
log "Starting at stage: $CURRENT_STAGE"

# =============================================================================
# STAGE 0: Full system upgrade + install kernel 6.14 + reboot
# =============================================================================
if [[ "$CURRENT_STAGE" -le 0 ]]; then
    banner "STAGE 0: System upgrade and kernel preparation"

    log "Updating package lists..."
    apt-get update -y

    log "Upgrading all packages to latest..."
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
    DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y

    log "Installing prerequisite packages..."
    apt-get install -y dkms build-essential

    # -- Determine current kernel and target kernel --
    RUNNING_KERNEL=$(uname -r)
    log "Currently running kernel: $RUNNING_KERNEL"

    if [[ "$RUNNING_KERNEL" == ${TARGET_KERNEL_MAJOR}.* ]]; then
        log "Already on a ${TARGET_KERNEL_MAJOR} kernel — no downgrade needed."
        TARGET_KERNEL_FULL="$RUNNING_KERNEL"
    else
        log "Running kernel ($RUNNING_KERNEL) is not ${TARGET_KERNEL_MAJOR}.x"
        log "Searching for available ${TARGET_KERNEL_MAJOR} kernel packages..."

        # Find the latest available 6.14 kernel image
        TARGET_KERNEL_PKG=$(apt-cache search "^linux-image-${TARGET_KERNEL_MAJOR}\." \
            | grep -v unsigned | grep generic \
            | sort -V | tail -1 | awk '{print $1}')

        [[ -n "$TARGET_KERNEL_PKG" ]] || die "No ${TARGET_KERNEL_MAJOR}.x kernel found in repos."

        # Derive the full version string, e.g., 6.14.0-37-generic
        TARGET_KERNEL_FULL=$(echo "$TARGET_KERNEL_PKG" | sed 's/linux-image-//')
        log "Target kernel: $TARGET_KERNEL_FULL (from $TARGET_KERNEL_PKG)"

        log "Installing ${TARGET_KERNEL_MAJOR} kernel packages..."
        apt-get install -y \
            "linux-image-${TARGET_KERNEL_FULL}" \
            "linux-headers-${TARGET_KERNEL_FULL}" \
            "linux-modules-${TARGET_KERNEL_FULL}" \
            "linux-modules-extra-${TARGET_KERNEL_FULL}" \
            2>/dev/null || true
    fi

    # Persist the target kernel version for later stages
    echo "$TARGET_KERNEL_FULL" > /var/tmp/.razzfazz-target-kernel

    # -- Remove non-target kernels --
    log "Removing non-${TARGET_KERNEL_MAJOR} kernels..."
    for pkg in $(dpkg -l | grep -E "^ii\s+linux-(image|headers|modules)" \
                 | awk '{print $2}' \
                 | grep -v "$TARGET_KERNEL_MAJOR" \
                 | grep -v "linux-headers-generic" \
                 | grep -v "linux-image-generic" \
                 | grep -vE "^linux-(image|headers|modules)-generic$"); do
        log "  Removing: $pkg"
        dpkg --remove --force-depends "$pkg" 2>/dev/null || true
    done

    # Remove HWE meta-packages that would pull newer kernels
    log "Removing HWE meta-packages..."
    dpkg --remove --force-depends \
        linux-generic-hwe-24.04 \
        linux-image-generic-hwe-24.04 \
        linux-headers-generic-hwe-24.04 \
        2>/dev/null || true

    # Fix any broken state from forced removals
    apt-get --fix-broken install -y 2>/dev/null || true

    # -- Pin kernel via APT preferences --
    log "Creating APT pin to block HWE kernel upgrades..."
    cat > /etc/apt/preferences.d/pin-kernel-${TARGET_KERNEL_MAJOR} <<EOF
Package: linux-generic-hwe-24.04
Pin: release *
Pin-Priority: -1

Package: linux-image-generic-hwe-24.04
Pin: release *
Pin-Priority: -1

Package: linux-headers-generic-hwe-24.04
Pin: release *
Pin-Priority: -1
EOF

    # -- Hold kernel packages --
    log "Holding kernel packages at ${TARGET_KERNEL_FULL}..."
    apt-mark hold \
        "linux-image-${TARGET_KERNEL_FULL}" \
        "linux-headers-${TARGET_KERNEL_FULL}" \
        "linux-modules-${TARGET_KERNEL_FULL}" \
        "linux-modules-extra-${TARGET_KERNEL_FULL}" \
        2>/dev/null || true

    # -- Set GRUB default --
    log "Setting GRUB to boot ${TARGET_KERNEL_FULL}..."
    sed -i "s|^GRUB_DEFAULT=.*|GRUB_DEFAULT=\"Advanced options for Ubuntu>Ubuntu, with Linux ${TARGET_KERNEL_FULL}\"|" \
        /etc/default/grub
    update-grub

    # -- Schedule next stage and reboot --
    set_stage 1

    # Ensure this script re-runs after reboot via cron @reboot
    SCRIPT_PATH=$(readlink -f "$0")
    CRON_LINE="@reboot /usr/bin/bash ${SCRIPT_PATH} >> ${LOG_FILE} 2>&1"
    (crontab -l 2>/dev/null | grep -v "$SCRIPT_PATH"; echo "$CRON_LINE") | crontab -

    log "Stage 0 complete. Rebooting into kernel ${TARGET_KERNEL_FULL}..."
    sleep 3
    reboot
    exit 0
fi

# =============================================================================
# STAGE 1: Install amdgpu/ROCm + Docker (after reboot into 6.14)
# =============================================================================
if [[ "$CURRENT_STAGE" -le 1 ]]; then
    banner "STAGE 1: Post-reboot — installing amdgpu, ROCm, Docker"

    TARGET_KERNEL_FULL=$(cat /var/tmp/.razzfazz-target-kernel 2>/dev/null || echo "unknown")
    RUNNING_KERNEL=$(uname -r)

    log "Running kernel: $RUNNING_KERNEL"
    log "Expected kernel: $TARGET_KERNEL_FULL"

    if [[ "$RUNNING_KERNEL" != ${TARGET_KERNEL_MAJOR}.* ]]; then
        die "Booted into wrong kernel ($RUNNING_KERNEL). Expected ${TARGET_KERNEL_MAJOR}.x — check GRUB config."
    fi

    log "Kernel OK: $RUNNING_KERNEL"

    # -- Verify no other kernel headers are lurking --
    log "Final cleanup of non-${TARGET_KERNEL_MAJOR} kernel headers..."
    for pkg in $(dpkg -l | grep -E "^ii\s+linux-headers" | awk '{print $2}' | grep -v "$TARGET_KERNEL_MAJOR"); do
        log "  Removing leftover: $pkg"
        dpkg --remove --force-depends "$pkg" 2>/dev/null || true
    done
    apt-get --fix-broken install -y 2>/dev/null || true

    # -- Install amdgpu-install --
    banner "Installing amdgpu-install from local deb"
    apt-get install -y "$AMDGPU_DEB"

    # -- Install amdgpu + ROCm --
    banner "Installing amdgpu driver + ROCm stack"
    amdgpu-install -y --usecase=rocm,graphics --accept-eula

    # -- Verify DKMS --
    log "DKMS status:"
    dkms status | tee -a "$LOG_FILE"

    # -- Add admin user to render/video groups for GPU access --
    log "Adding ${ADMIN_USER} to render and video groups..."
    usermod -aG render "$ADMIN_USER"
    usermod -aG video "$ADMIN_USER"

    # -- Install Docker --
    banner "Installing Docker"
    apt-get install -y docker.io docker-compose-v2

    log "Enabling and starting Docker service..."
    systemctl enable docker
    systemctl start docker

    log "Adding ${ADMIN_USER} to docker group..."
    usermod -aG docker "$ADMIN_USER"

    # -- Cleanup --
    banner "Cleanup"
    log "Removing setup state files..."
    rm -f "$STATE_FILE" /var/tmp/.razzfazz-target-kernel

    # Remove the @reboot cron entry
    SCRIPT_PATH=$(readlink -f "$0")
    crontab -l 2>/dev/null | grep -v "$SCRIPT_PATH" | crontab - 2>/dev/null || true

    apt-get autoremove -y
    apt-get clean

    # -- Final status --
    banner "Setup complete!"
    log "Kernel:  $(uname -r)"
    log "DKMS:    $(dkms status 2>/dev/null || echo 'n/a')"
    log "Docker:  $(docker --version 2>/dev/null || echo 'n/a')"
    log "ROCm:    $(cat /opt/rocm/.info/version 2>/dev/null || echo 'n/a')"
    log ""
    log "Next steps:"
    log "  1. Reboot one final time"
    log "  2. Log in as ${ADMIN_USER}"
    log "  3. Run: rocminfo | grep gfx"
    log "  4. Run: docker ps"
    log ""
    log "Full log: $LOG_FILE"

    set_stage 2
    log "Rebooting for final clean state..."
    sleep 3
    reboot
    exit 0
fi

# =============================================================================
# STAGE 2+: Already done
# =============================================================================
banner "Setup already completed. Nothing to do."
log "If you need to re-run, delete ${STATE_FILE} and run again."
