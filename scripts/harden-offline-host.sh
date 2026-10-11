#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz.ai Stack — Offline HOST-daemon egress hardening (#184, 2026.08)
# ==============================================================================
# OPT-IN, idempotent, REVERSIBLE helper for a CONFIRMED air-gapped box. It masks
# the Ubuntu HOST daemons that make BACKGROUND internet calls a customer firewall
# logs as noise — the "OS-daemon egress" class found in the offline live-
# acceptance (2026-07-21): ~31 blocked pkts / 240 s idle to
#   ubuntu-content-cache-*.canonical.com:80   (apt / snap store)
#   connectivity-check-ubuntu-com-*:80         (NetworkManager connectivity check)
#   ntp-*.canonical.com:123                     (systemd-timesyncd)
# plus a lone git-prefetch to codeberg-* (git maintenance.auto).
#
# This is DISTINCT from the app-telemetry class (handled automatically + safely by
# the offline compose overlay, scripts/gen-offline-overlay.py). Masking these HOST
# daemons is a host-level, RISKY change (no NTP, no auto-updates), so — unlike the
# overlay — it is NEVER run automatically by init/upgrade. The operator runs it by
# hand, once, on a box they KNOW is air-gapped, and can fully undo it.
#
# What it does (each step is idempotent + reversed by --undo):
#   1. snapd auto-refresh     → mask snapd.refresh.{timer,service} (+ snap refresh --hold)
#   2. NetworkManager check   → connectivity.enabled=false in a conf.d drop-in
#   3. systemd-timesyncd      → point at a LAN NTP (--ntp HOST) OR mask it
#   4. apt periodic timers    → mask apt-daily.timer + apt-daily-upgrade.timer
#   5. git maintenance.auto   → git config --system maintenance.auto false
#   6. egress firewall        → OPT-IN (--egress-firewall): nftables DROP of all
#                               non-LAN egress (host + containers), reboot-persisted
#
# Usage:
#   sudo ./scripts/harden-offline-host.sh                 # apply (box must be offline-mode)
#   sudo ./scripts/harden-offline-host.sh --ntp 10.0.0.1  # keep NTP via a LAN server
#   sudo ./scripts/harden-offline-host.sh --dry-run       # print the plan, change nothing
#   sudo ./scripts/harden-offline-host.sh --undo          # fully reverse every change
#   sudo ./scripts/harden-offline-host.sh --force         # apply even if not offline-mode
#   sudo ./scripts/harden-offline-host.sh --ntp 10.0.0.1 --egress-firewall  # + kernel egress DROP
#   ./scripts/harden-offline-host.sh --help
#
# GUARDS: refuses to APPLY unless the box is in offline mode (RAZZFAZZ_NETWORK_MODE
# =offline) or --force is given; requires root for real changes (a --dry-run needs
# no root). Trade-offs on the tin: after this, the box has NO automatic time sync
# (unless --ntp), NO OS security auto-updates, and NO snap auto-refresh — the
# operator owns those out of band. See docs/enterprise/how-to/offline-install.md.
# ==============================================================================

# NB: not `set -e` — a single tolerated failure (e.g. a unit that doesn't exist on
# this box) must not abort the remaining steps. Individual failures are logged.
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Options ------------------------------------------------------------------
DRY_RUN=false
UNDO=false
FORCE=false
NTP_HOST=""
EGRESS_FW=false

# Test / integration hooks (override the /etc + /var paths so the logic is unit-
# testable without touching the real host; NONE of these grant privileges).
ENV_FILE="${RAZZFAZZ_ENV_FILE:-$STACK_DIR/.env}"
STATE_DIR="${RAZZFAZZ_HARDEN_STATE_DIR:-/var/lib/razzfazz/offline-harden}"
NM_CONF_DIR="${RAZZFAZZ_HARDEN_NM_DIR:-/etc/NetworkManager}"
TIMESYNCD_DIR="${RAZZFAZZ_HARDEN_TIMESYNCD_DIR:-/etc/systemd}"
# RAZZFAZZ_HARDEN_DRYRUN=1 is an alias for --dry-run (env-driven, test-friendly).
[ "${RAZZFAZZ_HARDEN_DRYRUN:-0}" = "1" ] && DRY_RUN=true
# RAZZFAZZ_HARDEN_SKIP_ROOT_CHECK=1 bypasses ONLY the friendly root guard (used by
# the PATH-shim unit test). It does NOT grant privileges — real systemctl/etc.
# calls still need root on a real box; under the test's command shims they succeed.
SKIP_ROOT="${RAZZFAZZ_HARDEN_SKIP_ROOT_CHECK:-0}"

NM_DROPIN="$NM_CONF_DIR/conf.d/20-razzfazz-offline.conf"
TS_DROPIN="$TIMESYNCD_DIR/timesyncd.conf.d/20-razzfazz-offline.conf"
APPLIED_MARKER="$STATE_DIR/applied"
# Egress-firewall (opt-in, --egress-firewall) paths.
SYSTEMD_UNIT_DIR="${RAZZFAZZ_HARDEN_UNIT_DIR:-/etc/systemd/system}"
EGRESS_NFT_FILE="$STATE_DIR/egress.nft"
EGRESS_UNIT="$SYSTEMD_UNIT_DIR/razzfazz-offline-egress.service"

# --- Helpers ------------------------------------------------------------------
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }
step()  { echo -e "\n${CYAN}=== $* ===${NC}"; }
dry()   { echo -e "${YELLOW}[DRY-RUN]${NC} Would: $*"; }

# run <cmd...> — execute a mutating command, or (in --dry-run) just print it.
# Never aborts the script: a failure is logged and tolerated (set -e is off).
run() {
    if $DRY_RUN; then dry "$*"; return 0; fi
    if "$@"; then return 0; else warn "command failed (continuing): $*"; return 1; fi
}

# write_file <path> <content> — create/overwrite a conf file (dry-run prints it).
write_file() {
    local path="$1" content="$2"
    if $DRY_RUN; then dry "write $path"; return 0; fi
    mkdir -p "$(dirname "$path")" 2>/dev/null || true
    if printf '%s\n' "$content" > "$path" 2>/dev/null; then ok "wrote $path"; else warn "could not write $path"; fi
}

# remove_file <path> — delete a conf file if present (dry-run prints it).
remove_file() {
    local path="$1"
    [ -e "$path" ] || { $DRY_RUN && dry "rm -f $path (absent)"; return 0; }
    if $DRY_RUN; then dry "rm -f $path"; return 0; fi
    rm -f "$path" 2>/dev/null && ok "removed $path" || warn "could not remove $path"
}

usage() {
    cat <<'USAGE'
Usage: sudo ./scripts/harden-offline-host.sh [OPTIONS]

OPT-IN host-daemon egress hardening for a CONFIRMED air-gapped box. Masks the
Ubuntu daemons that phone home in the background (snap/apt store, NetworkManager
connectivity check, NTP, git auto-maintenance). Fully reversible with --undo.

Options:
  --ntp HOST     Keep time sync via a LAN NTP server HOST instead of masking
                 systemd-timesyncd (recommended — otherwise the clock will drift).
  --egress-firewall  ALSO install a kernel-enforced egress firewall (nftables):
                 DROP every outbound connection to non-LAN (WAN) addresses, for
                 BOTH host processes AND containers. LAN (RFC1918), loopback,
                 link-local, multicast and established/related flows stay allowed,
                 so on-box services, inter-container traffic, LAN DNS/NTP and
                 inbound sessions keep working. This makes "offline" enforced by
                 the kernel, not just by config (steps 1-5 only mask host daemons).
                 Persisted across reboot; removed by --undo. (alias: --firewall)
  --dry-run      Print exactly what would change; make NO changes. Needs no root.
  --undo         Fully reverse every change this script makes (unmask/remove/unset).
  --force        Apply even when the box is not in offline mode (RAZZFAZZ_NETWORK_MODE
                 != offline). Use only if you are certain the box is air-gapped.
  --help         Show this help.

Guards:
  • Refuses to APPLY unless RAZZFAZZ_NETWORK_MODE=offline (override with --force).
  • Real changes require root; --dry-run does not.

Trade-offs (READ THIS): after applying, the host has NO automatic time sync
(unless --ntp), NO OS security auto-updates, and NO snap auto-refresh. Those
become the operator's out-of-band responsibility. --undo restores all of them.
USAGE
}

# --- Argument parsing ---------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --undo)    UNDO=true; shift ;;
        --force)   FORCE=true; shift ;;
        --ntp)     NTP_HOST="${2:-}"; shift 2 ;;
        --ntp=*)   NTP_HOST="${1#*=}"; shift ;;
        --egress-firewall|--firewall) EGRESS_FW=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) err "Unknown option: $1"; echo; usage; exit 1 ;;
    esac
done

# --- offline-mode detection (never sources .env; grep targeted keys) ----------
is_offline_box() {
    local mode off
    mode="$(grep -E '^RAZZFAZZ_NETWORK_MODE=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\'' ')"
    case "$mode" in
        offline)         return 0 ;;
        online|proxied)  return 1 ;;
    esac
    # Back-compat: derive from the legacy boolean if the enum is unset.
    off="$(grep -E '^RAZZFAZZ_OFFLINE=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\'' ')"
    case "$off" in 1|true|TRUE|yes|on) return 0 ;; esac
    return 1
}

# --- Guards -------------------------------------------------------------------
# The offline-mode gate protects APPLY only — --undo must work anywhere (it only
# RESTORES the OS daemons, which is always safe, e.g. after re-connecting a box).
if ! $UNDO && ! $FORCE; then
    if ! is_offline_box; then
        err "This box is NOT in offline mode (RAZZFAZZ_NETWORK_MODE != offline in $ENV_FILE)."
        err "Masking timesyncd / snapd / apt on an ONLINE box breaks time sync and updates."
        err "Refusing. If the box really is air-gapped, re-run with --force."
        exit 2
    fi
fi

# Root is required for real (un)masking; a dry-run changes nothing so it does not.
if ! $DRY_RUN && [ "$SKIP_ROOT" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    err "This script must be run as root (sudo) to (un)mask host daemons."
    err "Tip: preview without root using --dry-run."
    exit 1
fi

# ==============================================================================
# Step functions — each honours the UNDO flag and is idempotent.
# ==============================================================================

# 1) snapd auto-refresh (apt/snap store egress) --------------------------------
step_snapd() {
    if ! command -v snap >/dev/null 2>&1; then info "snapd not installed — skipping snap-refresh step"; return 0; fi
    if $UNDO; then
        step "snapd auto-refresh — RESTORE"
        run systemctl unmask snapd.refresh.timer || true
        run systemctl unmask snapd.refresh.service || true
        run snap refresh --unhold || true
        ok "snapd auto-refresh restored"
    else
        step "snapd auto-refresh — HOLD (stops snap-store egress)"
        run systemctl stop snapd.refresh.timer || true
        run systemctl mask snapd.refresh.timer || true
        run systemctl mask snapd.refresh.service || true
        # Belt: hold all snap refreshes (snapd >= 2.58). Best-effort; the mask above
        # is the reliable primary.
        run snap refresh --hold || true
        ok "snapd auto-refresh held"
    fi
}

# 2) NetworkManager connectivity check ----------------------------------------
step_nm_connectivity() {
    if [ ! -d "$NM_CONF_DIR" ]; then info "NetworkManager not present ($NM_CONF_DIR) — skipping connectivity-check step"; return 0; fi
    if $UNDO; then
        step "NetworkManager connectivity-check — RESTORE"
        remove_file "$NM_DROPIN"
        run systemctl reload NetworkManager || run nmcli general reload || true
        ok "NetworkManager connectivity-check restored (default)"
    else
        step "NetworkManager connectivity-check — DISABLE (stops connectivity-check.ubuntu.com)"
        write_file "$NM_DROPIN" "# Managed by razzfazz-ai harden-offline-host.sh (#184). Remove with --undo.
[connectivity]
enabled=false
uri=
interval=0"
        run systemctl reload NetworkManager || run nmcli general reload || true
        ok "NetworkManager connectivity-check disabled"
    fi
}

# 3) systemd-timesyncd (NTP egress) -------------------------------------------
step_timesync() {
    if $UNDO; then
        step "systemd-timesyncd — RESTORE"
        remove_file "$TS_DROPIN"
        run systemctl unmask systemd-timesyncd || true
        run systemctl restart systemd-timesyncd || true
        # Also lift a chrony mask we may have applied.
        run systemctl unmask chrony || true
        run systemctl unmask chronyd || true
        ok "time sync restored (systemd-timesyncd)"
        return 0
    fi
    if [ -n "$NTP_HOST" ]; then
        step "systemd-timesyncd — POINT AT LAN NTP ($NTP_HOST)"
        write_file "$TS_DROPIN" "# Managed by razzfazz-ai harden-offline-host.sh (#184). Remove with --undo.
[Time]
NTP=$NTP_HOST
FallbackNTP="
        run systemctl restart systemd-timesyncd || true
        ok "time sync via LAN NTP $NTP_HOST (no WAN NTP)"
        if systemctl is-active --quiet chrony 2>/dev/null || systemctl is-active --quiet chronyd 2>/dev/null; then
            warn "chrony is ALSO active and does its own NTP egress — edit /etc/chrony/chrony.conf to use $NTP_HOST and remove any pool/server pointing at the internet."
        fi
    else
        step "systemd-timesyncd — MASK (no NTP egress)"
        warn "No --ntp given: the host clock will NOT be synced. Clock drift is now YOUR responsibility (set time manually or via a LAN NTP with --ntp HOST)."
        run systemctl stop systemd-timesyncd || true
        run systemctl mask systemd-timesyncd || true
        if systemctl is-active --quiet chrony 2>/dev/null || systemctl is-active --quiet chronyd 2>/dev/null; then
            warn "chrony is active and does NTP egress — masking it too (restore with --undo)."
            run systemctl stop chrony || run systemctl stop chronyd || true
            run systemctl mask chrony || true
            run systemctl mask chronyd || true
        fi
        ok "systemd-timesyncd masked"
    fi
}

# 4) apt periodic / unattended-upgrades timers --------------------------------
step_apt_timers() {
    if $UNDO; then
        step "apt periodic timers — RESTORE"
        run systemctl unmask apt-daily.timer || true
        run systemctl unmask apt-daily-upgrade.timer || true
        run systemctl enable apt-daily.timer || true
        run systemctl enable apt-daily-upgrade.timer || true
        ok "apt periodic timers restored"
    else
        step "apt periodic timers — MASK (stops apt content-cache egress)"
        run systemctl stop apt-daily.timer || true
        run systemctl stop apt-daily-upgrade.timer || true
        run systemctl mask apt-daily.timer || true
        run systemctl mask apt-daily-upgrade.timer || true
        ok "apt periodic timers masked (run 'apt update/upgrade' by hand when needed)"
    fi
}

# 5) git auto-maintenance (background prefetch) -------------------------------
step_git_maintenance() {
    if ! command -v git >/dev/null 2>&1; then info "git not installed — skipping git-maintenance step"; return 0; fi
    if $UNDO; then
        step "git maintenance.auto — RESTORE"
        run git config --system --unset maintenance.auto || true
        ok "git maintenance.auto restored (default)"
    else
        step "git maintenance.auto — DISABLE (stops background git prefetch)"
        run git config --system maintenance.auto false || true
        ok "git maintenance.auto disabled system-wide"
    fi
}

# 6) egress firewall (kernel-enforced air-gap) — OPT-IN via --egress-firewall --
# Steps 1-5 only mask HOST daemons; a container (or any binary) can still open a
# WAN socket. This step installs an nftables table that DROPS all egress to
# non-LAN destinations for BOTH host (output hook) AND containers (forward hook),
# so "offline" is enforced by the kernel. Destination-based (interface-agnostic):
# anything not in the LAN/loopback/link-local/multicast sets — or an established/
# related flow — is dropped. The `inet` table is independent of Docker's own
# iptables-nft rules (Docker never flushes it); the forward hook runs at priority
# -5 so a WAN-bound container packet is dropped before Docker routes it, while
# LAN + established flows fall through to Docker unchanged (inbound services,
# inter-container traffic and LAN DNS/NTP keep working). Reboot-persisted via a
# systemd unit ordered After=docker.service. Fully removed by --undo.
step_egress_firewall() {
    if $UNDO; then
        step "egress firewall — REMOVE (restores WAN egress)"
        run systemctl disable --now razzfazz-offline-egress.service || true
        remove_file "$EGRESS_UNIT"
        remove_file "$EGRESS_NFT_FILE"
        run systemctl daemon-reload || true
        if command -v nft >/dev/null 2>&1; then
            run nft delete table inet razzfazz_offline || true
        fi
        ok "egress firewall removed"
        return 0
    fi
    $EGRESS_FW || { info "egress firewall not requested (--egress-firewall) — skipping"; return 0; }
    if ! command -v nft >/dev/null 2>&1; then
        err "nft (nftables) not installed — cannot install the egress firewall."
        err "Install the 'nftables' package, or re-run without --egress-firewall."
        return 1
    fi
    step "egress firewall — INSTALL (DROP all non-LAN egress: host + containers)"
    warn "This blocks ALL outbound traffic to non-LAN/WAN addresses (host AND containers)."
    warn "LAN (RFC1918), loopback, link-local, multicast and established flows stay allowed."
    warn "DNS/NTP must resolve on the LAN or locally — a WAN resolver (e.g. 8.8.8.8) will be blocked."
    # inet family = IPv4 + IPv6. Named sets keep the LAN/local allow-list readable.
    local ruleset='table inet razzfazz_offline {
    set lan4 {
        type ipv4_addr
        flags interval
        elements = { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16,
                     127.0.0.0/8, 169.254.0.0/16, 224.0.0.0/4,
                     255.255.255.255 }
    }
    set lan6 {
        type ipv6_addr
        flags interval
        elements = { ::1, fe80::/10, fc00::/7, ff00::/8 }
    }
    chain output {
        type filter hook output priority 0; policy accept;
        ct state established,related accept
        oifname "lo" accept
        meta nfproto ipv4 ip daddr != @lan4 drop
        meta nfproto ipv6 ip6 daddr != @lan6 drop
    }
    chain forward {
        type filter hook forward priority -5; policy accept;
        ct state established,related accept
        meta nfproto ipv4 ip daddr != @lan4 drop
        meta nfproto ipv6 ip6 daddr != @lan6 drop
    }
}'
    write_file "$EGRESS_NFT_FILE" "$ruleset"
    if run nft -f "$EGRESS_NFT_FILE"; then
        ok "egress ruleset loaded (live)"
    else
        err "nft failed to load the ruleset — removing any partial table and aborting this step."
        run nft delete table inet razzfazz_offline || true
        remove_file "$EGRESS_NFT_FILE"
        return 1
    fi
    # Reboot persistence: reload the ruleset on boot, AFTER docker sets up its own.
    write_file "$EGRESS_UNIT" "[Unit]
Description=razzfazz.ai offline egress firewall (DROP non-LAN egress)
Documentation=razzfazz-ai harden-offline-host.sh (#184)
After=network-online.target docker.service nftables.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $EGRESS_NFT_FILE
ExecStop=/usr/sbin/nft delete table inet razzfazz_offline

[Install]
WantedBy=multi-user.target"
    run systemctl daemon-reload || true
    run systemctl enable razzfazz-offline-egress.service || true
    ok "egress firewall installed + enabled (persists across reboot; --undo removes it)"
    warn "If LAN access to this box breaks, console in and run: nft delete table inet razzfazz_offline"
}

# --- Marker (operator visibility; NOT relied on for undo correctness) ---------
write_marker() {
    $DRY_RUN && return 0
    if $UNDO; then
        rm -f "$APPLIED_MARKER" 2>/dev/null || true
        return 0
    fi
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    printf 'applied_at=%s\nntp_host=%s\negress_firewall=%s\nby=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)" \
        "${NTP_HOST:-<masked>}" \
        "$EGRESS_FW" \
        "$(id -un -- "${SUDO_USER:-}" 2>/dev/null || id -un 2>/dev/null || echo unknown)" \
        > "$APPLIED_MARKER" 2>/dev/null || true
}

# ==============================================================================
# Run
# ==============================================================================
if $UNDO; then
    echo -e "${CYAN}razzfazz.ai — REVERSING offline host-daemon hardening${NC}"
else
    echo -e "${CYAN}razzfazz.ai — offline host-daemon egress hardening${NC}"
fi
$DRY_RUN && info "DRY-RUN: no changes will be made."
info "env-file: $ENV_FILE | offline-mode: $(is_offline_box && echo yes || echo no) | undo: $UNDO | force: $FORCE"

step_snapd
step_nm_connectivity
step_timesync
step_apt_timers
step_git_maintenance
step_egress_firewall
write_marker

echo
if $DRY_RUN; then
    ok "Dry-run complete — nothing was changed."
elif $UNDO; then
    ok "Undo complete — the masked host daemons have been restored."
    warn "A restored box will resume its normal internet egress (NTP/apt/snap/connectivity-check)."
else
    ok "Offline host-daemon hardening applied."
    warn "Reminder: no auto time-sync${NTP_HOST:+ except via $NTP_HOST}, no OS auto-updates, no snap auto-refresh until you --undo."
    $EGRESS_FW && warn "Egress firewall ACTIVE: all non-LAN (WAN) egress is dropped for host + containers. Verify LAN access still works; --undo removes it."
fi
exit 0
