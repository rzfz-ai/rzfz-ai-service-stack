#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# upgrade-host-kernel.sh — install + boot an Ubuntu mainline kernel on a
# razzfazz.ai box. Validated target for AMD Strix Halo: 6.18.27.
#
# ⚠ DEPRECATED (M034 S09, 2026-05-24) — the fleet is on Ubuntu 26.04 LTS, which
#   ships a Canonical-SIGNED kernel (7.0.x) that boots under Secure Boot and
#   enables Strix Halo (gfx1151) out of the box. The mainline-.deb path this
#   script automates (UNSIGNED kernels, Secure-Boot-off, run-parts/postinst.d
#   workarounds) is no longer the supported way to get a newer kernel — use the
#   distro kernel from 26.04, or `razzfazz-upgrade-os.sh` to migrate.
#   KEPT for emergency hardware-enablement only (e.g. a brand-new GPU needing a
#   kernel newer than what 26.04 currently ships). Do NOT use on the normal path.
#   The Strix Halo GRUB tunables it applied now live in razzfazz-init.sh Step 5e.
#
# Codifies the 8-step procedure + 5 gotchas the operator hand-walked on
# culturehack-001 (2026-05-21), all linked in
# .gsd/reports/bug-retrospective-2026-05-22.md → Pattern E.
#
# Default behaviour: --check (dry run). Pass --apply to actually install.
#
# Gotchas this script handles:
#   1. mainline .debs need /usr/share/kernel/postinst.d/ to exist + be
#      populated with hooks; Ubuntu 24.04 ships them at /etc/kernel/
#      postinst.d/ only → symlink them across.
#   2. mainline .debs' postinst.d/ hook is what triggers update-initramfs;
#      without the symlink, initramfs is never generated → boot panics
#      with "unable to mount root fs on unknown-block(0,0)".
#   3. Strix Halo (gfx1151) on 6.17+ silently ignores `amdgpu.gtt_size`,
#      needs new ttm.pages_limit + ttm.page_pool_size cmdline tunables
#      sized to actual host RAM. Without these, large-model load triggers
#      kworker D-state pile-up (load avg 400+).
#   4. mainline kernels are UNSIGNED → Secure Boot must be off. Detect
#      via mokutil --sb-state and refuse to proceed if enabled.
#   5. GRUB_DEFAULT may be `saved` with saved_entry pointing at the
#      previous kernel; we explicitly grub-set-default the new one after
#      install + verify GRUB_TIMEOUT is non-zero so the menu can appear
#      if the boot panics.
#
# Reference: docs/upgrade-guide-test-single-box-2026.04-ga.1-to-2026.05-ga.4.md
#   §"Kernel note" + .gsd/reports/bug-retrospective-2026-05-22.md.

set -euo pipefail

# ============================================================================
# Args + defaults
# ============================================================================
TARGET_KERNEL="6.18.27"
APPLY=false
SKIP_GRUB_TUNABLES=false
SKIP_SECUREBOOT_CHECK=false
VERBOSE=false

usage() {
    cat <<EOF
Usage: $0 [options]

⚠ DEPRECATED (M034 S09): the fleet runs Ubuntu 26.04 LTS with a Canonical-SIGNED
  kernel (Secure-Boot-capable, Strix Halo gfx1151 enabled out of the box). This
  mainline-.deb path ships UNSIGNED kernels and is kept for emergency
  hardware-enablement only. Normal path: 26.04's distro kernel /
  razzfazz-upgrade-os.sh.


Options:
  --target <ver>            Mainline kernel version to install (default: $TARGET_KERNEL).
                            Use kernel.ubuntu.com/mainline/?C=M;O=D format (e.g. 6.18.30, 6.19.1).
  --apply                   Actually install. Default is --check (dry run).
  --check                   Dry run: show what would be installed; change nothing
                            (this is the default if neither --apply nor --check given).
  --skip-grub-tunables      Don't append the Strix Halo amdgpu GRUB cmdline params.
                            Use on non-Strix-Halo AMD or non-AMD hardware.
  --skip-secureboot-check   Override the Secure Boot precheck. UNSAFE if Secure Boot
                            is actually on — boot will fail. Only for known-off systems
                            where mokutil reports a misleading state.
  --verbose, -v             Print every step's command output, not just summaries.
  -h | --help               This help.

Examples:
  $0                                              # dry-run install of 6.18.27 (default)
  $0 --apply                                      # actually install 6.18.27
  $0 --target 6.18.30 --apply                     # install a newer 6.18.x point release
  $0 --skip-grub-tunables --apply                 # install for non-Strix-Halo hardware
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --target)              TARGET_KERNEL="$2"; shift 2 ;;
        --apply)               APPLY=true; shift ;;
        --check)               APPLY=false; shift ;;
        --skip-grub-tunables)  SKIP_GRUB_TUNABLES=true; shift ;;
        --skip-secureboot-check) SKIP_SECUREBOOT_CHECK=true; shift ;;
        --verbose|-v)          VERBOSE=true; shift ;;
        -h|--help)             usage; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# M034 S09: loud runtime deprecation notice. This mainline-kernel path is legacy
# now the fleet is on 26.04 (signed kernels). Continue only for emergency
# hardware-enablement; the normal path is 26.04's distro kernel.
echo "⚠ DEPRECATED (M034 S09): upgrade-host-kernel.sh installs UNSIGNED mainline" >&2
echo "  kernels. The supported path is Ubuntu 26.04's Canonical-signed kernel" >&2
echo "  (Secure-Boot-capable, Strix Halo enabled). Emergency hardware-enablement only." >&2

# === SAFETY GUARD (2026.06-ga.6): hard-stop, not just a warning ==================
# Installing an UNSIGNED mainline kernel on a 26.04 box DOWNGRADES the signed distro
# kernel and BREAKS Secure Boot — the deprecation echo above used to print and then
# continue anyway. Refuse outright unless the vendor explicitly opts in.
if [ "${RAZZFAZZ_ALLOW_LEGACY_HOST_SCRIPT:-0}" != "1" ]; then
    echo "[x] DISABLED: this unsigned mainline-kernel cutover is unsafe on Ubuntu 26.04 and" >&2
    echo "    breaks Secure Boot. The signed 26.04 kernel already supports Strix Halo." >&2
    echo "    Vendor emergency use only: re-run with RAZZFAZZ_ALLOW_LEGACY_HOST_SCRIPT=1" >&2
    exit 1
fi
# ================================================================================

if [ "$(id -u)" -ne 0 ]; then
    # Re-exec with sudo so the operator can run as their own user (-E preserves
    # RAZZFAZZ_ALLOW_LEGACY_HOST_SCRIPT so the guard stays satisfied post-sudo)
    exec sudo -E "$0" "$@"
fi

# ============================================================================
# Output helpers
# ============================================================================
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
step()    { printf "${BLUE}[STEP]${NC} %s\n" "$*"; }
ok()      { printf "${GREEN}[✓]${NC} %s\n" "$*"; }
warn()    { printf "${YELLOW}[!]${NC} %s\n" "$*"; }
err()     { printf "${RED}[✗]${NC} %s\n" "$*"; }
info()    { printf "${CYAN}[i]${NC} %s\n" "$*"; }

# Run a command, respecting --apply (dry-run vs real).
run() {
    if [ "$APPLY" = true ]; then
        if [ "$VERBOSE" = true ]; then printf "  + %s\n" "$*"; fi
        eval "$@"
    else
        printf "  DRY-RUN: %s\n" "$*"
    fi
}

# ============================================================================
# Phase 0 — Pre-flight
# ============================================================================
step "Phase 0 — Pre-flight"

CURRENT_KERNEL=$(uname -r)
info "Current kernel: $CURRENT_KERNEL"
info "Target kernel:  $TARGET_KERNEL (mainline-build, unsigned)"
info "Mode:           $([ "$APPLY" = true ] && echo APPLY || echo "DRY-RUN (use --apply to install)")"

# Gotcha #4 — Secure Boot must be off for unsigned mainline kernels.
if [ "$SKIP_SECUREBOOT_CHECK" != true ]; then
    if command -v mokutil >/dev/null 2>&1; then
        SB_STATE=$(mokutil --sb-state 2>&1 || true)
        if echo "$SB_STATE" | grep -qi "enabled"; then
            err "Secure Boot is ENABLED."
            err "  mainline kernels are unsigned and will not boot under Secure Boot."
            err "  Either:"
            err "    1. Reboot into UEFI/BIOS, disable Secure Boot, come back — OR"
            err "    2. Re-run with --skip-secureboot-check if you've confirmed it's already off."
            exit 1
        fi
        info "Secure Boot: $(echo "$SB_STATE" | head -1)"
    else
        warn "mokutil not installed — cannot verify Secure Boot. Install with: apt install mokutil"
        warn "  Continuing anyway. If boot fails, Secure Boot is the likely cause."
    fi
fi

# Disk space — need ~500 MB in /boot, ~1 GB in /var
BOOT_AVAIL_MB=$(df -m /boot 2>/dev/null | tail -1 | awk '{print $4}')
VAR_AVAIL_MB=$(df -m /var 2>/dev/null | tail -1 | awk '{print $4}')
info "Disk free: /boot=${BOOT_AVAIL_MB}MB, /var=${VAR_AVAIL_MB}MB"
if [ "${BOOT_AVAIL_MB:-0}" -lt 500 ]; then
    err "/boot has <500MB free. Free space first."
    exit 1
fi

# Skip if already on target
if [ "$CURRENT_KERNEL" = "${TARGET_KERNEL}-061827-generic" ] \
   || echo "$CURRENT_KERNEL" | grep -q "^${TARGET_KERNEL}-"; then
    ok "Already running $CURRENT_KERNEL → on $TARGET_KERNEL. Nothing to do."
    exit 0
fi

# ============================================================================
# Phase 1 — Discover real .deb filenames from kernel.ubuntu.com/mainline
# ============================================================================
step "Phase 1 — Discovering mainline .deb filenames"

BASE="https://kernel.ubuntu.com/mainline/v${TARGET_KERNEL}/amd64"
TMPDIR="/tmp/kernel-upgrade-${TARGET_KERNEL}"
mkdir -p "$TMPDIR" && cd "$TMPDIR"

info "Fetching dir listing: $BASE/"
LISTING=$(curl -s --max-time 30 "$BASE/" || true)
if [ -z "$LISTING" ]; then
    err "Could not fetch $BASE/ — check connectivity to kernel.ubuntu.com."
    exit 1
fi

# Extract the 4 .debs we need. Their names have a build-timestamp suffix
# (YYYYMMDDhhmm) that we can't predict, so we parse the listing.
DEB_ALL=$(echo "$LISTING"     | grep -oE "linux-headers-${TARGET_KERNEL//./\\.}-061827_[^\"']+_all\.deb"           | head -1)
DEB_HDR=$(echo "$LISTING"     | grep -oE "linux-headers-${TARGET_KERNEL//./\\.}-061827-generic_[^\"']+_amd64\.deb" | head -1)
DEB_IMG=$(echo "$LISTING"     | grep -oE "linux-image-unsigned-${TARGET_KERNEL//./\\.}-061827-generic_[^\"']+_amd64\.deb" | head -1)
DEB_MOD=$(echo "$LISTING"     | grep -oE "linux-modules-${TARGET_KERNEL//./\\.}-061827-generic_[^\"']+_amd64\.deb" | head -1)

for f in "$DEB_ALL" "$DEB_HDR" "$DEB_IMG" "$DEB_MOD"; do
    if [ -z "$f" ]; then
        err "Could not find one of the expected .deb files in $BASE/"
        err "  Got: ALL='$DEB_ALL' HDR='$DEB_HDR' IMG='$DEB_IMG' MOD='$DEB_MOD'"
        err "  The mainline build may use a different naming convention for this version."
        exit 1
    fi
done
info "Found: $DEB_ALL"
info "       $DEB_HDR"
info "       $DEB_IMG"
info "       $DEB_MOD"

# ============================================================================
# Phase 2 — Download
# ============================================================================
step "Phase 2 — Downloading .debs"
for f in "$DEB_ALL" "$DEB_HDR" "$DEB_IMG" "$DEB_MOD"; do
    if [ -f "$f" ] && [ "$(stat -c%s "$f")" -gt 1000000 ]; then
        info "  cached: $f"
    else
        run "wget -nv '$BASE/$f' -O '$f.partial' && mv '$f.partial' '$f'"
    fi
done
info "Downloaded to $TMPDIR/"

# ============================================================================
# Phase 3 — Fix gotcha #1 + #2: postinst.d hook symlinks
# ============================================================================
step "Phase 3 — Ensuring /usr/share/kernel/postinst.d/ is populated (gotcha #1+#2)"
# Mainline .debs invoke run-parts on /usr/share/kernel/postinst.d/ for
# initramfs-tools + update-grub triggers. Ubuntu 24.04 installs those scripts
# only under /etc/kernel/postinst.d/. Without this, initramfs is never
# generated → boot panics with "unable to mount root fs on unknown-block(0,0)".
run "mkdir -p /usr/share/kernel/{preinst.d,postinst.d,prerm.d,postrm.d,header_postinst.d}"
if [ -d /etc/kernel/postinst.d/ ]; then
    HOOK_COUNT=0
    for hook in /etc/kernel/postinst.d/*; do
        [ -f "$hook" ] || continue
        DEST="/usr/share/kernel/postinst.d/$(basename "$hook")"
        if [ ! -L "$DEST" ]; then
            run "ln -sf '$hook' '$DEST'"
            HOOK_COUNT=$((HOOK_COUNT + 1))
        fi
    done
    ok "Linked $HOOK_COUNT postinst hook(s) into /usr/share/kernel/postinst.d/"
else
    warn "/etc/kernel/postinst.d/ doesn't exist — initramfs-tools may not be installed."
fi

# ============================================================================
# Phase 4 — Install
# ============================================================================
step "Phase 4 — Installing .debs"
run "dpkg -i '$TMPDIR/$DEB_ALL' '$TMPDIR/$DEB_HDR' '$TMPDIR/$DEB_MOD' '$TMPDIR/$DEB_IMG'"
run "apt-get install -f -y"
ok "Installed."

# ============================================================================
# Phase 5 — Ensure initramfs exists for the new kernel (gotcha #2 belt+braces)
# ============================================================================
step "Phase 5 — Verifying initramfs for $TARGET_KERNEL-061827-generic"
NEW_INITRD="/boot/initrd.img-${TARGET_KERNEL}-061827-generic"
if [ "$APPLY" = true ]; then
    if [ ! -f "$NEW_INITRD" ] || [ "$(stat -c%s "$NEW_INITRD")" -lt 50000000 ]; then
        warn "Initramfs missing or suspiciously small ($(stat -c%s "$NEW_INITRD" 2>/dev/null || echo 0) bytes)."
        warn "  Regenerating manually."
        run "update-initramfs -c -k ${TARGET_KERNEL}-061827-generic"
    fi
    # Verify key drivers are inside (LVM + storage)
    if command -v lsinitramfs >/dev/null 2>&1; then
        if ! lsinitramfs "$NEW_INITRD" 2>/dev/null | grep -qE 'dm-mod|dm_mod|lvm'; then
            warn "Initramfs may be missing LVM / device-mapper drivers."
            warn "  If your / is on LVM, boot will fail. Check /etc/initramfs-tools/modules."
        fi
        if ! lsinitramfs "$NEW_INITRD" 2>/dev/null | grep -qE 'nvme|virtio_blk|ahci'; then
            warn "Initramfs may be missing storage drivers (nvme/virtio/ahci)."
        fi
    fi
    ok "Initramfs OK: $(ls -lh "$NEW_INITRD" | awk '{print $5}')"
else
    info "DRY-RUN: would verify $NEW_INITRD exists + has dm-mod / nvme drivers"
fi

# ============================================================================
# Phase 6 — Strix Halo GRUB cmdline (gotcha #3)
# ============================================================================
if [ "$SKIP_GRUB_TUNABLES" = true ]; then
    step "Phase 6 — SKIP GRUB tunables (--skip-grub-tunables passed)"
else
    step "Phase 6 — Applying Strix Halo amdgpu GRUB cmdline tunables (gotcha #3)"

    # Detect AMD GPU
    if ! lspci -nn 2>/dev/null | grep -qE "VGA|3D|Display" | grep -qi "amd\|advanced micro devices"; then
        if ! lspci -nn 2>/dev/null | grep -iE "VGA|3D|Display" | grep -qi "amd"; then
            warn "No AMD GPU detected in lspci output. Skipping GRUB amdgpu tunables."
            warn "  Re-run with --skip-grub-tunables to suppress this on non-AMD hardware."
        fi
    fi

    RAM_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
    PAGES=$(( RAM_KB / 4 ))
    HALF=$(( PAGES / 2 ))
    info "RAM_KB=$RAM_KB PAGES=$PAGES HALF=$HALF"

    # Take a backup of /etc/default/grub once
    run "cp -n /etc/default/grub /etc/default/grub.upgrade-host-kernel.bak"

    # The 4 Strix Halo amdgpu params
    PARAMS="amdgpu.cwsr_enable=0 amd_iommu=off ttm.pages_limit=$PAGES ttm.page_pool_size=$HALF"

    CURRENT_CMDLINE=$(grep -E '^GRUB_CMDLINE_LINUX_DEFAULT=' /etc/default/grub | cut -d'"' -f2)
    info "Current GRUB_CMDLINE_LINUX_DEFAULT: $CURRENT_CMDLINE"

    NEW_CMDLINE="$CURRENT_CMDLINE"
    for p in $PARAMS; do
        key=$(echo "$p" | cut -d= -f1)
        if echo "$NEW_CMDLINE" | grep -qE "(^| )${key}="; then
            # Already present — replace
            NEW_CMDLINE=$(echo "$NEW_CMDLINE" | sed -E "s#(^| )${key}=[^ ]*# \\1${p}#g")
        else
            NEW_CMDLINE="$NEW_CMDLINE $p"
        fi
    done
    NEW_CMDLINE=$(echo "$NEW_CMDLINE" | sed 's/^ *//; s/ *$//; s/  */ /g')

    if [ "$NEW_CMDLINE" = "$CURRENT_CMDLINE" ]; then
        ok "GRUB cmdline already up-to-date."
    else
        info "New GRUB_CMDLINE_LINUX_DEFAULT: $NEW_CMDLINE"
        run "sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=\"[^\"]*\"|GRUB_CMDLINE_LINUX_DEFAULT=\"${NEW_CMDLINE}\"|' /etc/default/grub"
        ok "GRUB cmdline updated."
    fi
fi

# ============================================================================
# Phase 7 — GRUB default + timeout (gotcha #5)
# ============================================================================
step "Phase 7 — Pinning GRUB default + ensuring menu visibility (gotcha #5)"

# Ensure timeout is non-zero so we can interrupt if the new kernel panics
CURRENT_TIMEOUT=$(grep -E '^GRUB_TIMEOUT=' /etc/default/grub | head -1 | cut -d= -f2)
if [ "${CURRENT_TIMEOUT:-0}" -eq 0 ]; then
    warn "GRUB_TIMEOUT=0 — menu won't be visible on boot. Setting to 5."
    run "sed -i 's|^GRUB_TIMEOUT=.*|GRUB_TIMEOUT=5|' /etc/default/grub"
fi
# Style might also be "hidden" — flip to "menu"
if grep -qE '^GRUB_TIMEOUT_STYLE=hidden' /etc/default/grub; then
    run "sed -i 's|^GRUB_TIMEOUT_STYLE=hidden|GRUB_TIMEOUT_STYLE=menu|' /etc/default/grub"
fi

# GRUB_DEFAULT=saved + grub-set-default the new kernel
if ! grep -qE '^GRUB_DEFAULT=saved' /etc/default/grub; then
    run "sed -i 's|^GRUB_DEFAULT=.*|GRUB_DEFAULT=saved|' /etc/default/grub"
fi

# Update GRUB so menuentries reflect what's installed now
run "update-grub"

# Pin saved_entry to the new kernel
NEW_ENTRY="Advanced options for Ubuntu>Ubuntu, with Linux ${TARGET_KERNEL}-061827-generic"
run "grub-set-default '$NEW_ENTRY'"
ok "GRUB saved_entry = $NEW_ENTRY"

# ============================================================================
# Phase 8 — Summary + reboot prompt
# ============================================================================
step "Phase 8 — Summary"
if [ "$APPLY" = true ]; then
    cat <<EOF

  ${GREEN}✓ Installation complete.${NC}

  Current kernel:      $CURRENT_KERNEL
  After next reboot:   ${TARGET_KERNEL}-061827-generic

  ${YELLOW}IMPORTANT — before rebooting:${NC}
    - GRUB menu will appear for 5 seconds. If the new kernel panics,
      tap ESC during boot to interrupt + pick the previous kernel from
      "Advanced options for Ubuntu".
    - Capture any panic message from /var/lib/systemd/pstore/ after
      falling back to the old kernel.

  ${CYAN}Verification commands after successful reboot:${NC}
    uname -r           # expect ${TARGET_KERNEL}-061827-generic
    cat /proc/cmdline  # expect the amdgpu + ttm tunables visible
    dmesg | grep -iE 'amdgpu|ttm|mt7925'   # check driver init messages

  Reboot now with:
    sudo reboot
EOF
else
    cat <<EOF

  ${YELLOW}DRY-RUN complete. No changes were made.${NC}

  To actually install, re-run with --apply:
    sudo $0 --apply

  Or for non-AMD or non-Strix-Halo hardware:
    sudo $0 --apply --skip-grub-tunables
EOF
fi
