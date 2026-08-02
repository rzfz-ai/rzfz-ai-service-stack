#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz-upgrade-os.sh — automated Ubuntu 24.04 → 26.04 LTS host upgrade
# ==============================================================================
# M034 S06. Customer-facing, idempotent. Encodes every lesson from the M034 S01
# in-place migration (.gsd/reports/26.04-migration-091-in-place-2026-05-22.md):
#
#   * NON-INTERACTIVE by default — the S01 run lost ~2h to silent prompts
#     (foreign-packages, apt-listchanges pager, libc6 + docker debconf TUIs).
#     We pre-configure all of them so do-release-upgrade never blocks.
#   * STOP THE STACK FIRST (folds in the deferred M033 S30) — incl. the
#     gpustack runner pods that survive `docker compose down`.
#   * TWO PHASES around the unavoidable reboot: `upgrade` (default) runs the
#     dist-upgrade (which reboots); `--reconcile` (run after the box comes
#     back) restarts the stack, health-checks, and reminds about kernel
#     cutover + Secure Boot. A breadcrumb file records which phase is due.
#
# The Strix Halo GRUB tunables + kernel-stability sysctls are owned by
# razzfazz-init.sh (Steps 5d/5e) — re-run `razzfazz-init.sh --package <p>` (or
# the Config UI) after this completes if those need refreshing; this script
# does not duplicate them.
#
# NOT a substitute for a pre-upgrade SNAPSHOT. do-release-upgrade is not
# reliably reversible; take a Clonezilla/VM snapshot before running unless you
# accept a rebuild-from-init recovery.
# ==============================================================================
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
cd "$SCRIPT_DIR"

OS_UPGRADE_LOG="${RAZZFAZZ_UPGRADE_OS_LOG:-${SCRIPT_DIR}/.upgrade-os.log}"
export RAZZFAZZ_LOG_FILE="$OS_UPGRADE_LOG"
# shellcheck source=scripts/lib.sh
. "${SCRIPT_DIR}/scripts/lib.sh"

BREADCRUMB="/var/tmp/razzfazz-upgrade-os.phase"
TARGET_RELEASE="26.04"
DRY_RUN=false
SKIP_BACKUP_PROMPT=false
ASSUME_YES=false
MODE="upgrade"   # upgrade | reconcile

# ── issue #172: pinned-6.14 kernel · ROCm-DKMS · GRUB-cmdline preservation ─────
# Old 24.04 appliance boxes (Care Solutions, tester VMs) carry a kernel PINNED at
# 6.14 (`apt-mark hold`; HWE removed because the HWE meta breaks ROCm on Strix
# Halo) AND build ROCm/amdgpu via DKMS. A held linux-* package makes
# do-release-upgrade refuse; the 6.14-built DKMS fails to rebuild against 26.04's
# 7.0.x kernel (broken DKMS state); and the upgrade can drop the Strix Halo
# amdgpu/ttm/iommu cmdline. State persists across the mid-upgrade reboot in
# /var/tmp (same store as $BREADCRUMB) so --reconcile can verify/restore.
# Paths are env-overridable purely so the unit test can inject fixtures.
STATE_DIR="${RAZZFAZZ_UPGRADE_OS_STATE_DIR:-/var/tmp/razzfazz-upgrade-os}"
HELD_KERNEL_STATE="${STATE_DIR}/held-kernel-packages"
GRUB_SNAPSHOT_DIR="${STATE_DIR}/grub-snapshot"
GRUB_DEFAULT_FILE="${RAZZFAZZ_GRUB_DEFAULT_FILE:-/etc/default/grub}"
GRUB_DROPIN_DIR="${RAZZFAZZ_GRUB_DROPIN_DIR:-/etc/default/grub.d}"
PROC_CMDLINE="${RAZZFAZZ_PROC_CMDLINE:-/proc/cmdline}"
# The Strix Halo hardware cmdline tokens we must preserve across the upgrade —
# canonical set written by cli/init.sh Step 5e:
#   amdgpu.cwsr_enable=0 amd_iommu=off ttm.pages_limit=<N> ttm.page_pool_size=<N>
GRUB_HWPARAM_RE='^(amdgpu\.|ttm\.|amd_iommu=|iommu=)'

usage() {
    cat <<EOF
razzfazz-upgrade-os.sh — automated Ubuntu 24.04 -> 26.04 LTS host upgrade.

🟡 COORDINATED: stops the stack and REBOOTS the host mid-upgrade. Snapshot first;
   run --check before the real run.

USAGE:
  sudo ./scripts/razzfazz-upgrade-os.sh [--check] [--yes] [--skip-backup-prompt]
  sudo ./scripts/razzfazz-upgrade-os.sh --reconcile     # AFTER the box reboots

PHASES:
  (default)      Pre-flight, stop the stack, non-interactive do-release-upgrade.
                 The box REBOOTS at the end of the dist-upgrade.
  --reconcile    Run once the box is back on $TARGET_RELEASE: restart the stack,
                 health-check, and print kernel-cutover + Secure-Boot guidance.

OPTIONS:
  --check               Dry run: show what each phase would do; change nothing.
  --yes                 Don't prompt for the destructive confirmation.
  --skip-backup-prompt  Skip the "have you snapshotted?" gate (you accept the risk).
  -h, --help            This help.

The reboot splits the work: run the default phase, let the box reboot, then run
--reconcile. A breadcrumb at $BREADCRUMB records which phase is due.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) DRY_RUN=true ;;
        --yes) ASSUME_YES=true ;;
        --skip-backup-prompt) SKIP_BACKUP_PROMPT=true ;;
        --reconcile) MODE="reconcile" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

run() {
    # Echo + execute, or just echo under --check.
    if [ "$DRY_RUN" = true ]; then
        print_info "[dry-run] $*"
    else
        print_info "+ $*"
        eval "$@"
    fi
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        print_error "Run with sudo (host package operations require root)."
        exit 1
    fi
}

# ── issue #172 helpers: pinned kernel · ROCm-DKMS · GRUB cmdline ───────────────

# detect_held_kernels: print held linux-* packages (one per line), empty if none.
# `apt-mark showhold` needs no root and never mutates — safe under --check.
detect_held_kernels() {
    command -v apt-mark >/dev/null 2>&1 || return 0
    apt-mark showhold 2>/dev/null | grep -E '^linux-' || true
}

# detect_rocm_dkms: print ROCm/amdgpu `dkms status` lines, empty if none/absent.
# Read-only — safe under --check.
detect_rocm_dkms() {
    command -v dkms >/dev/null 2>&1 || return 0
    dkms status 2>/dev/null | grep -iE 'amdgpu|rocm' || true
}

# current_grub_hwparams: print the deduplicated amdgpu/ttm/iommu cmdline tokens
# found across $GRUB_DEFAULT_FILE, its drop-ins, and the live cmdline. Read-only.
current_grub_hwparams() {
    local tokens="" src vals
    for src in "$GRUB_DEFAULT_FILE" "$GRUB_DROPIN_DIR"/*; do
        [ -f "$src" ] || continue
        vals="$(grep -hoE '^[[:space:]]*GRUB_CMDLINE_LINUX(_DEFAULT)?="[^"]*"' "$src" 2>/dev/null \
                | sed -E 's#^[^"]*"##; s#"$##')"
        tokens="$tokens $vals"
    done
    [ -r "$PROC_CMDLINE" ] && tokens="$tokens $(cat "$PROC_CMDLINE" 2>/dev/null || true)"
    printf '%s' "$tokens" | tr ' ' '\n' \
        | grep -E "$GRUB_HWPARAM_RE" \
        | awk 'NF && !seen[$0]++' \
        | paste -sd' ' - 2>/dev/null || true
}

# purge_rocm_dkms <dkms-status-lines>: `dkms remove` each amdgpu/rocm module then
# purge the packages. 26.04 ships in-kernel amdgpu (gfx1151) so the out-of-tree
# DKMS is no longer needed. Honours --check via run().
purge_rocm_dkms() {
    local lines="$1" line mod ver
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        # `dkms status` forms: "amdgpu/6.14.5, KVER: installed" (new) or
        # "amdgpu, 6.14.5, KVER: installed" (old). Grab module name + version.
        mod="$(printf '%s' "$line" | sed -E 's#^([A-Za-z0-9_.+-]+)[/,].*#\1#')"
        ver="$(printf '%s' "$line" | sed -E 's#^[A-Za-z0-9_.+-]+[/,][[:space:]]*([0-9][A-Za-z0-9_.+-]*).*#\1#')"
        if [ -n "$mod" ] && [ -n "$ver" ] && [ "$mod" != "$line" ]; then
            run "dkms remove -m '$mod' -v '$ver' --all >/dev/null 2>&1 || true"
        fi
    done <<<"$lines"
    run "DEBIAN_FRONTEND=noninteractive apt-get purge -y amdgpu-dkms rocm-dkms amdgpu-dkms-firmware >/dev/null 2>&1 || true"
    print_success "ROCm/amdgpu DKMS purged (26.04 in-kernel amdgpu takes over)."
}

# snapshot_grub_cmdline: record the amdgpu/ttm/iommu tokens + copy the grub config
# sources so --reconcile can verify/restore them after the reboot.
snapshot_grub_cmdline() {
    local params
    params="$(current_grub_hwparams)"
    if [ -z "$params" ]; then
        print_substep "GRUB cmdline: no amdgpu/ttm/iommu params present to preserve."
    else
        print_substep "GRUB cmdline params to preserve across the upgrade: $params"
    fi
    if [ "$DRY_RUN" = true ]; then
        print_info "[dry-run] would snapshot ${GRUB_DEFAULT_FILE} + ${GRUB_DROPIN_DIR}/ + params to ${GRUB_SNAPSHOT_DIR}"
        return 0
    fi
    mkdir -p "$GRUB_SNAPSHOT_DIR" 2>/dev/null || true
    [ -f "$GRUB_DEFAULT_FILE" ] && cp -a "$GRUB_DEFAULT_FILE" "$GRUB_SNAPSHOT_DIR/grub" 2>/dev/null || true
    if [ -d "$GRUB_DROPIN_DIR" ]; then
        mkdir -p "$GRUB_SNAPSHOT_DIR/grub.d" 2>/dev/null || true
        cp -a "$GRUB_DROPIN_DIR"/. "$GRUB_SNAPSHOT_DIR/grub.d/" 2>/dev/null || true
    fi
    printf '%s\n' "$params" > "$GRUB_SNAPSHOT_DIR/hwparams" 2>/dev/null || true
    print_success "Snapshotted GRUB cmdline sources + params to ${GRUB_SNAPSHOT_DIR}."
}

# preflight_hwenable: issue #172 pre-flight — unhold a pinned 6.14 kernel, purge
# ROCm/amdgpu DKMS, and snapshot the GRUB cmdline. Detection is read-only and
# runs even under --check (reports the findings); mutations honour --check via
# run()/DRY_RUN, and the GPU-driver DKMS purge is gated on confirmation.
preflight_hwenable() {
    print_step "Pre-flight: pinned 6.14 kernel · ROCm-DKMS · GRUB cmdline (issue #172)"

    # (1) Held 6.14 kernel — unhold so do-release-upgrade proceeds; record it.
    local held
    held="$(detect_held_kernels)"
    if [ -n "$held" ]; then
        print_warning "Held kernel package(s) detected — do-release-upgrade refuses to proceed while a linux-* package is held:"
        while IFS= read -r p; do [ -n "$p" ] && print_substep "  held: $p"; done <<<"$held"
        if [ "$DRY_RUN" = true ]; then
            print_info "[dry-run] would 'apt-mark unhold' the above and record them to ${HELD_KERNEL_STATE}"
        else
            mkdir -p "$STATE_DIR" 2>/dev/null || true
            printf '%s\n' "$held" > "$HELD_KERNEL_STATE" 2>/dev/null || true
            while IFS= read -r p; do
                [ -n "$p" ] || continue
                run "apt-mark unhold '$p'"
            done <<<"$held"
            print_success "Unheld kernel package(s); recorded to ${HELD_KERNEL_STATE}."
        fi
    else
        print_substep "No held linux-* kernel packages (apt-mark showhold clean)."
    fi

    # (2) ROCm/amdgpu DKMS — purge before the upgrade (loud, GPU-driver path).
    local dkms
    dkms="$(detect_rocm_dkms)"
    if [ -n "$dkms" ]; then
        print_warning "ROCm/amdgpu DKMS module(s) detected — these were built for 6.14 and will NOT rebuild against 26.04's 7.0.x kernel (broken DKMS state):"
        while IFS= read -r d; do [ -n "$d" ] && print_substep "  dkms: $d"; done <<<"$dkms"
        print_warning "26.04 ships in-kernel amdgpu for Strix Halo (gfx1151); purging the out-of-tree DKMS so the upgrade doesn't leave it half-built. THIS TOUCHES THE GPU DRIVER PATH."
        if [ "$DRY_RUN" = true ]; then
            print_info "[dry-run] would 'dkms remove' each module + 'apt-get purge amdgpu-dkms rocm-dkms'."
        else
            if [ "$ASSUME_YES" = false ] && [ "$SKIP_BACKUP_PROMPT" = false ]; then
                read -r -p "Purge ROCm/amdgpu DKMS now (required for the 26.04 upgrade)? Type 'yes' to continue: " ans
                [ "$ans" = "yes" ] || { print_error "Aborted — DKMS purge declined. do-release-upgrade will likely fail rebuilding the 6.14 DKMS against 26.04. Re-run with --yes to auto-confirm."; exit 1; }
            fi
            purge_rocm_dkms "$dkms"
        fi
    else
        print_substep "No ROCm/amdgpu DKMS present (dkms status clean or dkms not installed)."
    fi

    # (3) GRUB cmdline — snapshot the Strix Halo amdgpu/ttm/iommu tunables.
    snapshot_grub_cmdline
}

# report_unheld_kernels: --reconcile advisory — remind the operator which kernel
# packages we unheld pre-upgrade. We deliberately do NOT re-hold: on 26.04 the
# Canonical-signed 7.0.x kernel is the supported one (6.14 is gone).
report_unheld_kernels() {
    [ -f "$HELD_KERNEL_STATE" ] || return 0
    print_step "Pre-upgrade held kernel packages (issue #172)"
    print_substep "These were 'apt-mark unhold'ed before the upgrade (NOT re-held — 26.04's signed 7.0.x kernel is the supported one):"
    while IFS= read -r p; do [ -n "$p" ] && print_substep "  was held: $p"; done < "$HELD_KERNEL_STATE"
    print_info "If you intentionally pin a kernel on 26.04, re-hold manually: sudo apt-mark hold <pkg>"
}

# verify_restore_grub_cmdline: --reconcile — confirm the preserved amdgpu/ttm/
# iommu params survived the upgrade; if do-release-upgrade dropped them (replaced
# /etc/default/grub), restore them as a drop-in + update-grub (needs a reboot).
verify_restore_grub_cmdline() {
    print_step "GRUB cmdline preservation check (Strix Halo ttm/iommu/amdgpu, issue #172)"
    local want=""
    [ -f "$GRUB_SNAPSHOT_DIR/hwparams" ] && want="$(cat "$GRUB_SNAPSHOT_DIR/hwparams" 2>/dev/null || true)"
    if [ -z "$want" ]; then
        print_substep "No pre-upgrade GRUB hardware params were recorded — nothing to verify."
        return 0
    fi
    local live missing="" tok key
    live="$(cat "$PROC_CMDLINE" 2>/dev/null || true)"
    for tok in $want; do
        key="${tok%%=*}"
        grep -qE "(^| )${key}=" <<<"$live" || missing="$missing $tok"
    done
    missing="$(printf '%s' "$missing" | sed 's/^ *//; s/ *$//')"
    if [ -z "$missing" ]; then
        print_success "All preserved GRUB hardware params are present on the running kernel cmdline."
        return 0
    fi
    print_warning "GRUB cmdline LOST hardware param(s) across the OS upgrade: $missing"
    print_warning "  do-release-upgrade likely replaced ${GRUB_DEFAULT_FILE}. Restoring from the pre-upgrade snapshot as a drop-in."
    if [ "$DRY_RUN" = true ]; then
        print_info "[dry-run] would write ${GRUB_DROPIN_DIR}/50-razzfazz-strix-preserved.cfg + update-grub (reboot to apply)."
        return 0
    fi
    mkdir -p "$GRUB_DROPIN_DIR" 2>/dev/null || true
    {
        printf '# Restored by razzfazz-upgrade-os.sh --reconcile (issue #172).\n'
        printf '# Re-applies the Strix Halo amdgpu/ttm/iommu cmdline dropped by the 24.04->26.04 upgrade.\n'
        printf '# grub.d drop-ins are sourced AFTER /etc/default/grub, so this appends.\n'
        printf 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT %s"\n' "$want"
    } > "${GRUB_DROPIN_DIR}/50-razzfazz-strix-preserved.cfg" 2>/dev/null || true
    run "update-grub"
    print_warning "Restored the GRUB cmdline drop-in. REBOOT to apply the amdgpu/ttm/iommu tunables: sudo reboot"
}

# ── Phase: upgrade ────────────────────────────────────────────────────────────
phase_upgrade() {
    print_step "razzfazz OS upgrade — Ubuntu -> ${TARGET_RELEASE} LTS"

    # 1. Sanity
    local cur
    cur="$(lsb_release -rs 2>/dev/null || echo unknown)"
    print_substep "Current release: $cur"
    if [ "$cur" = "$TARGET_RELEASE" ]; then
        print_success "Already on ${TARGET_RELEASE}. Nothing to upgrade. (Run --reconcile if the stack isn't up.)"
        exit 0
    fi
    if [ "$cur" != "24.04" ]; then
        print_warning "Expected 24.04 as the upgrade source; found '$cur'. Proceeding only if you know the path is supported."
    fi
    require_command do-release-upgrade

    # 2. Backup gate
    if [ "$SKIP_BACKUP_PROMPT" = false ] && [ "$ASSUME_YES" = false ] && [ "$DRY_RUN" = false ]; then
        print_warning "do-release-upgrade is NOT reliably reversible."
        print_warning "Take a Clonezilla/VM SNAPSHOT now if you haven't. Stack data (volumes) is preserved by this script, but a failed dist-upgrade can leave the box unbootable."
        read -r -p "Snapshot taken (or accept the risk)? Type 'yes' to continue: " ans
        [ "$ans" = "yes" ] || { print_error "Aborted — take a snapshot first, or pass --skip-backup-prompt."; exit 1; }
    fi

    # 2b. Hardware-enablement pre-flight (issue #172): unhold a pinned 6.14
    #     kernel, purge 6.14-built ROCm/amdgpu DKMS, snapshot the Strix Halo GRUB
    #     cmdline. Must run BEFORE do-release-upgrade (a held linux-* package
    #     makes it refuse; the DKMS breaks rebuilding against 26.04's kernel).
    preflight_hwenable

    # 3. Stop the stack (incl. orphan gpustack runner pods) — folds in M033 S30.
    print_step "Stopping the stack before the OS upgrade (M033 S30)..."
    if command -v docker >/dev/null 2>&1; then
        run "docker compose down --remove-orphans || true"
        # gpustack runner pods are spawned via the docker socket and survive
        # compose down — stop them too so nothing holds VRAM/RAM or the daemon
        # mid-upgrade. Names end in -pause / -run-<N> (see razzfazz-status.sh).
        run "docker ps --format '{{.Names}}' | grep -E '\\-pause\$|\\-run-[0-9]+\$' | xargs -r docker stop || true"
    else
        print_substep "docker not present — skipping stack stop."
    fi

    # 4. Pre-configure NON-INTERACTIVE so do-release-upgrade never blocks (S01).
    print_step "Pre-configuring non-interactive upgrade (S01 silent-prompt fixes)..."
    export DEBIAN_FRONTEND=noninteractive
    export APT_LISTCHANGES_FRONTEND=none
    # apt-listchanges pager cost ~60 min on S01 — disable it for the run.
    if [ -f /etc/apt/listchanges.conf ]; then
        run "sed -i 's/^frontend=.*/frontend=none/' /etc/apt/listchanges.conf"
    fi
    # libc6 + docker service-restart debconf TUIs (~60 min on S01) — auto-yes.
    run "echo 'libc6 libraries/restart-without-asking boolean true' | debconf-set-selections"
    run "echo 'libraries/restart-without-asking boolean true' | debconf-set-selections"

    # 5. Clear pending updates on 24.04 first (do-release-upgrade requires it).
    print_step "Clearing pending 24.04 updates..."
    run "apt-get update -y"
    run "apt-get -y -o Dpkg::Options::=--force-confold full-upgrade"
    run "apt-get -y autoremove"

    # 6. The dist-upgrade itself — non-interactive frontend (S01: -d required
    #    until 26.04.1, DistUpgradeViewNonInteractive answers prompts).
    print_step "Running do-release-upgrade (non-interactive). The box will REBOOT at the end."
    echo "upgrade-started" > "$BREADCRUMB" 2>/dev/null || true
    if [ "$DRY_RUN" = true ]; then
        print_info "[dry-run] DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none do-release-upgrade -d -f DistUpgradeViewNonInteractive"
        print_success "[dry-run] Upgrade phase complete (no changes made)."
        return 0
    fi
    echo "reconcile-due" > "$BREADCRUMB" 2>/dev/null || true
    do-release-upgrade -d -f DistUpgradeViewNonInteractive
    # do-release-upgrade usually reboots itself; if it returns without rebooting:
    print_warning "do-release-upgrade returned without rebooting. Reboot now, then run:"
    print_info "  sudo ./scripts/razzfazz-upgrade-os.sh --reconcile"
}

# ── Phase: reconcile ──────────────────────────────────────────────────────────
phase_reconcile() {
    print_step "Post-upgrade reconciliation"
    local cur
    cur="$(lsb_release -rs 2>/dev/null || echo unknown)"
    print_substep "Current release: $cur (kernel $(uname -r))"
    if [ "$cur" != "$TARGET_RELEASE" ]; then
        print_warning "Not on ${TARGET_RELEASE} (found '$cur'). The dist-upgrade may not have completed — review $OS_UPGRADE_LOG."
    fi

    # Kernel cutover guidance (S01 Phase 5): the migration may leave a mainline/
    # OEM kernel as default. Prefer the Canonical-signed 7.0 (Secure-Boot-able).
    print_step "Kernel check"
    print_substep "Running: $(uname -r)"
    local newest_signed
    newest_signed="$(ls -1 /boot/vmlinuz-*-generic 2>/dev/null | sed 's#.*/vmlinuz-##' | sort -V | tail -1)"
    if [ -n "$newest_signed" ] && [ "$newest_signed" != "$(uname -r)" ]; then
        print_warning "Newer signed generic kernel installed ($newest_signed) but not booted."
        print_info "  Make it default: sudo grub-set-default \"gnulinux-advanced-\$(findmnt -no UUID /)>gnulinux-${newest_signed}-advanced-\$(findmnt -no UUID /)\" && sudo update-grub && reboot"
    else
        print_success "Booted kernel is the newest signed generic kernel."
    fi

    # issue #172: report the kernel packages we unheld pre-upgrade (advisory;
    # NOT re-held) and verify/restore the Strix Halo amdgpu/ttm/iommu cmdline.
    report_unheld_kernels
    verify_restore_grub_cmdline

    # Restart the stack.
    print_step "Restarting the stack..."
    if command -v docker >/dev/null 2>&1; then
        run "docker compose up -d --remove-orphans"
        if [ "$DRY_RUN" = false ]; then
            sleep 10
            local unhealthy
            unhealthy="$(docker ps --format '{{.Status}}' | grep -ciE 'unhealthy|Restarting' || true)"
            if [ "$unhealthy" -eq 0 ]; then
                print_success "Stack up; no unhealthy/restarting containers."
            else
                print_warning "$unhealthy container(s) unhealthy/restarting — check 'docker compose ps' + 'docker compose logs'."
            fi
        fi
    fi

    # Secure Boot reminder (S08): the migration disables Secure Boot; with the
    # Canonical-signed kernel it can (and for NIS2 should) be re-enabled.
    print_step "Secure Boot"
    if command -v mokutil >/dev/null 2>&1; then
        local sb; sb="$(mokutil --sb-state 2>/dev/null || echo unknown)"
        print_substep "$sb"
        echo "$sb" | grep -qi 'enabled' || \
            print_warning "Secure Boot is OFF. Re-enable it in BIOS/UEFI now that the kernel is Canonical-signed (per-vendor steps in docs/upgrade-guide-26.04-lts.md)."
    else
        print_info "mokutil not installed; can't check Secure Boot state."
    fi

    [ "$DRY_RUN" = false ] && rm -f "$BREADCRUMB" 2>/dev/null || true
    print_success "Reconciliation complete. Verify the UIs (config.<domain>, chat.<domain>, …)."
}

# ── main ──────────────────────────────────────────────────────────────────────
# --check changes nothing, so it doesn't need root (lets operators preview the
# plan without sudo). Every real phase requires root.
[ "$DRY_RUN" = false ] && require_root
case "$MODE" in
    upgrade)   phase_upgrade ;;
    reconcile) phase_reconcile ;;
esac
