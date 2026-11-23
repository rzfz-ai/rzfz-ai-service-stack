#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/apply-network-mode.sh   (#184 — 2026.08 network-mode selector)
# =============================================================================
# The single front-door knob for the box's egress axis. Sets
# RAZZFAZZ_NETWORK_MODE (online|proxied|offline) and reconciles the COMPOSE_FILE
# overlays + the internal implementation booleans (RAZZFAZZ_CORPORATE_PROXY /
# RAZZFAZZ_OFFLINE) to match — mirroring how apply-corporate-proxy.sh manages its
# overlay. Idempotent; the same reconcile runs automatically on every init/upgrade
# via scripts/lib.sh::ensure_network_mode_overlay.
#
#   online   : direct egress (default). Removes both overlays.
#   proxied  : egress only through the corporate proxy (#181). Composes
#              compose.corporate-proxy.yml IF present; otherwise points you at
#              `rzfz setup --corporate-proxy …` which also installs the proxy CA
#              into the host trust store + docker daemon (this flag does not).
#   offline  : no internet egress, LAN stays up (#184). Generates + composes
#              compose.offline.yml and activates the RAZZFAZZ_OFFLINE egress gates.
#
# USAGE
#   apply-network-mode.sh --mode online|proxied|offline
#   apply-network-mode.sh --status        # show current mode + overlay state
#   apply-network-mode.sh --check         # dry-run: print intended actions
#
# Going online is refused while the host egress firewall from
# scripts/harden-offline-host.sh is active: lift it first with
#   sudo scripts/harden-offline-host.sh --undo
# (reading the nft table needs root; the persisting unit is checked without it).
#
# After applying, recreate the stack:  docker compose up -d --force-recreate
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

cd "$SCRIPT_DIR"

ENV_FILE="${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR}/.env}"

MODE_ARG=""
ACTION="apply"

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)     MODE_ARG="${2:-}"; shift ;;
        --status)   ACTION="status" ;;
        --check)    ACTION="check" ;;
        -h|--help)  grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 0 ;;
        online|proxied|offline) MODE_ARG="$1" ;;   # bare positional convenience
        *) print_error "Unknown argument: $1"; exit 64 ;;
    esac
    shift
done

if [ "$ACTION" = "status" ]; then
    print_step "Network-mode status"
    print_substep "RAZZFAZZ_NETWORK_MODE     = $(read_env_value "$ENV_FILE" RAZZFAZZ_NETWORK_MODE || true)"
    print_substep "effective mode (resolved) = $(razzfazz_network_mode "$ENV_FILE")"
    print_substep "RAZZFAZZ_CORPORATE_PROXY  = $(read_env_value "$ENV_FILE" RAZZFAZZ_CORPORATE_PROXY || true)"
    print_substep "RAZZFAZZ_OFFLINE          = $(read_env_value "$ENV_FILE" RAZZFAZZ_OFFLINE || true)"
    print_substep "COMPOSE_FILE              = $(read_env_value "$ENV_FILE" COMPOSE_FILE || true)"
    if compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_OFFLINE_OVERLAY"; then
        print_substep "offline overlay           = in COMPOSE_FILE"
    else
        print_substep "offline overlay           = not composed"
    fi
    if compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_PROXY_OVERLAY"; then
        print_substep "corporate-proxy overlay   = in COMPOSE_FILE"
    else
        print_substep "corporate-proxy overlay   = not composed"
    fi
    # #184 P2 — registry mirror is orthogonal to the egress mode (any mode + mirror).
    print_substep "RAZZFAZZ_REGISTRY_MIRROR  = $(read_env_value "$ENV_FILE" RAZZFAZZ_REGISTRY_MIRROR || true)"
    if compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_REGISTRY_MIRROR_OVERLAY"; then
        print_substep "registry-mirror overlay   = in COMPOSE_FILE"
    else
        print_substep "registry-mirror overlay   = not composed"
    fi
    if razzfazz_offline_egress_firewall_active; then
        print_substep "host egress firewall      = ACTIVE (drops all non-LAN egress; lift it: $(razzfazz_offline_egress_undo_command))"
    elif [ "$(id -u)" -ne 0 ]; then
        print_substep "host egress firewall      = not active (the nft table is only readable as root; the persisting unit is not running)"
    else
        print_substep "host egress firewall      = not active"
    fi
    exit 0
fi

case "$MODE_ARG" in
    online|proxied|offline) : ;;
    "") print_error "Pass --mode online|proxied|offline (or --status)."; exit 64 ;;
    *)  print_error "Invalid mode '$MODE_ARG' (expected online|proxied|offline)."; exit 64 ;;
esac

# Going online while the host egress firewall is still in force breaks the stack:
# the recreate waits on outbound look-ups the firewall drops, and the box stays
# without internet. Refuse before anything is written, and name the way out.
refuse_online_behind_firewall() {
    [ "$MODE_ARG" = "online" ] || return 0
    razzfazz_offline_egress_firewall_active || return 0
    print_error "The host egress firewall from harden-offline-host.sh is still active: it drops all non-LAN egress, so an online box would have no internet and the recreate would fail."
    print_error "Lift it first, then switch the mode:"
    print_error "  $(razzfazz_offline_egress_undo_command)"
    print_error "  ${SCRIPT_DIR}/scripts/apply-network-mode.sh --mode online"
    return 1
}

if [ "$ACTION" = "check" ]; then
    print_step "[dry-run] Would set RAZZFAZZ_NETWORK_MODE=${MODE_ARG} and reconcile overlays"
    print_substep "[dry-run] current effective mode: $(razzfazz_network_mode "$ENV_FILE")"
    case "$MODE_ARG" in
        offline) print_substep "[dry-run] would generate ${RAZZFAZZ_OFFLINE_OVERLAY} + add to COMPOSE_FILE, set RAZZFAZZ_OFFLINE=1, RAZZFAZZ_CORPORATE_PROXY=0, drop ${RAZZFAZZ_PROXY_OVERLAY}" ;;
        proxied) print_substep "[dry-run] would add ${RAZZFAZZ_PROXY_OVERLAY} (if present) to COMPOSE_FILE, set RAZZFAZZ_CORPORATE_PROXY=1, RAZZFAZZ_OFFLINE=0, drop ${RAZZFAZZ_OFFLINE_OVERLAY}" ;;
        online)  print_substep "[dry-run] would drop both overlays from COMPOSE_FILE, set RAZZFAZZ_CORPORATE_PROXY=0, RAZZFAZZ_OFFLINE=0" ;;
    esac
    refuse_online_behind_firewall || { print_error "[dry-run] the real run would refuse."; exit 1; }
    exit 0
fi

# ---- apply ------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
    print_error "No .env at ${ENV_FILE} — run 'rzfz init' first."
    exit 1
fi

refuse_online_behind_firewall || exit 1

print_step "Setting network mode → ${MODE_ARG}"
update_env_value "$ENV_FILE" RAZZFAZZ_NETWORK_MODE "$MODE_ARG"
# ensure_network_mode_overlay reads the just-written mode and reconciles
# everything (booleans, overlay generation, COMPOSE_FILE wiring).
RAZZFAZZ_ENV_FILE="$ENV_FILE" ensure_network_mode_overlay "$ENV_FILE"

# #3234: the apt-listchanges timer is a host setting that flips at the offline
# boundary: harden-host masks it OFFLINE (an hourly changelogs.ubuntu.com
# lookup breaks the offline promise); going back ONLINE unmasks and enables it
# again. The switch does not re-run harden-host, so the reconcile lives here —
# for BOTH directions: a box switched offline after install must lose the
# timer too, not only the next hardening run. The implementation is the shared
# lib.sh helper razzfazz_apt_listchanges_reconcile, so this switch and the
# init/firstboot path (which persists the mode only after harden-host ran)
# cannot drift; the mode was just written to $ENV_FILE above, so resolving it
# from the file is the same as MODE_ARG.
reconcile_apt_listchanges() {
    razzfazz_apt_listchanges_reconcile "$ENV_FILE"
}
reconcile_apt_listchanges

# The time source is a setting that outlives the mode switch, but what it means
# on the wire changes at the offline boundary (internet reads as none offline).
# Re-apply it so chrony follows; the setting itself is never touched here.
reconcile_time_source() {
    [ -d "${RAZZFAZZ_TIME_CHRONY_DIR:-/etc/chrony}" ] || return 0
    if [ "$(id -u)" -eq 0 ] || [ "${RAZZFAZZ_TIME_SKIP_ROOT_CHECK:-0}" = "1" ]; then
        RAZZFAZZ_ENV_FILE="$ENV_FILE" bash "${SCRIPT_DIR}/scripts/apply-time-source.sh" \
            || print_warning "Could not re-apply the time source: run ${SCRIPT_DIR}/scripts/apply-time-source.sh"
    else
        local ts; ts="$(read_env_value "$ENV_FILE" TIME_SOURCE || true)"
        print_info "Time source: ${ts:-internet} is kept. Re-apply it for this mode: sudo ${SCRIPT_DIR}/scripts/apply-time-source.sh"
    fi
}
reconcile_time_source

print_step "Done"
print_substep "COMPOSE_FILE = $(read_env_value "$ENV_FILE" COMPOSE_FILE || true)"
if [ "$MODE_ARG" = "proxied" ] && ! compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_PROXY_OVERLAY"; then
    print_info "proxied mode is set but no proxy overlay is composed yet. Run the full setup:"
    print_info "  rzfz setup --corporate-proxy --proxy-url http://proxy.corp:3128 --ca-file /path/to/proxy-ca.pem"
fi
print_info "Apply with: docker compose up -d --force-recreate"
exit 0
