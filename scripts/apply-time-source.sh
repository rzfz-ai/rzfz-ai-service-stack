#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# apply-time-source.sh — the box's time source is a SETTING
# =============================================================================
# One host-side implementation behind `rzfz setup --time-source` (and its alias
# `--ntp`), the Settings portal and scripts/harden-offline-host.sh. The setting
# lives in .env:
#
#   TIME_SOURCE   internet | dhcp | lan | none        (default: internet)
#   TIME_SERVERS  space- or comma-separated IPs / host names (mode lan)
#
#   internet  the distro's chrony pools (what every box did before this setting)
#   dhcp      the NTP servers the DHCP lease announces. chrony only knows that for
#             dhclient and NetworkManager; on systemd-networkd a networkd-
#             dispatcher hook (scripts/chrony-dhcp-hook.sh) writes
#             /run/chrony-dhcp/<iface>.sources and chrony reads that directory
#   lan       the configured servers, `server X iburst`
#   none      no sources. chrony KEEPS RUNNING (no masking): the clock is free-
#             running, `chronyc makestep`/`settime` still work by hand, and the
#             daemon is already in place when the setting changes back
#
# Whatever the mode, the script owns exactly ONE chrony drop-in
# (conf.d/20-razzfazz-time-source.conf). Sources the distro ships (conf.d/*.conf,
# sources.d/*.sources, pool/server lines in chrony.conf) are PARKED (renamed to
# *.razzfazz-parked, lines prefixed) for every mode but internet, and restored by
# internet. An offline box (RAZZFAZZ_NETWORK_MODE=offline) cannot reach pools, so
# the effective mode of `internet` there is `none`; the setting itself is kept, and
# going online again brings the pools back.
#
# Usage:
#   sudo scripts/apply-time-source.sh                         # apply the .env setting
#   sudo scripts/apply-time-source.sh --mode lan --servers 10.0.0.1,10.0.0.2
#   sudo scripts/apply-time-source.sh --ntp 10.0.0.1          # alias of --mode lan
#   scripts/apply-time-source.sh --status                     # setting + measured sync
#   scripts/apply-time-source.sh --mode dhcp --dry-run        # print the plan
# =============================================================================
#
# Failure policy: set -e is on. A step that may fail without that being fatal is
# written `step || warn "what that means"`; nothing is silently ignored.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

ENV_FILE="${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR}/.env}"
CHRONY_DIR="${RAZZFAZZ_TIME_CHRONY_DIR:-/etc/chrony}"
DISPATCH_DIR="${RAZZFAZZ_TIME_DISPATCHER_DIR:-/etc/networkd-dispatcher}"
DHCP_DIR="${RAZZFAZZ_TIME_DHCP_DIR:-/run/chrony-dhcp}"
TS_DIR="${RAZZFAZZ_TIME_TIMESYNCD_DIR:-/etc/systemd}"
PENDING_FILE="${RAZZFAZZ_TIME_QUEUE_DIR:-${SCRIPT_DIR}/.probe-queue}/time-source.pending"
SKIP_ROOT="${RAZZFAZZ_TIME_SKIP_ROOT_CHECK:-0}"
SYNC_WAIT="${RAZZFAZZ_TIME_SYNC_WAIT_SECS:-0}"

DROPIN="$CHRONY_DIR/conf.d/20-razzfazz-time-source.conf"
TS_DROPIN="$TS_DIR/timesyncd.conf.d/20-razzfazz-time-source.conf"
HOOK_NAME="50-razzfazz-chrony-dhcp"
HOOK_STATES=(routable degraded off no-carrier carrier)
PARK_SUFFIX=".razzfazz-parked"
PARK_MARK="#razzfazz-parked# "

MODE_ARG=""; SERVERS_ARG=""; HAVE_SERVERS=false; ACTION="apply"; DRY_RUN=false

die()  { print_error "$*"; exit "${2:-1}"; }
warn() { print_warning "$*"; }

usage() { grep -E '^#( |$)' "$0" | sed -E 's/^# ?//' | sed -n '1,45p'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)      MODE_ARG="${2:-}"; shift ;;
        --mode=*)    MODE_ARG="${1#*=}" ;;
        --servers)   SERVERS_ARG="${2:-}"; HAVE_SERVERS=true; shift ;;
        --servers=*) SERVERS_ARG="${1#*=}"; HAVE_SERVERS=true ;;
        --ntp)       MODE_ARG="lan"; SERVERS_ARG="${2:-}"; HAVE_SERVERS=true; shift ;;
        --ntp=*)     MODE_ARG="lan"; SERVERS_ARG="${1#*=}"; HAVE_SERVERS=true ;;
        --status)    ACTION="status" ;;
        --dry-run|--check) DRY_RUN=true ;;
        -h|--help)   usage; exit 0 ;;
        internet|dhcp|lan|none) MODE_ARG="$1" ;;
        *) die "Unknown argument: $1" 64 ;;
    esac
    shift
done
if [ "${RAZZFAZZ_HARDEN_DRYRUN:-0}" = "1" ]; then DRY_RUN=true; fi

# --- validation (the portal's services/time_source.py applies the same rules) ---------
valid_host() {
    local s="$1"
    [ -n "$s" ] && [ "${#s}" -le 253 ] || return 1
    if [[ "$s" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
        local o; for o in ${s//./ }; do [ "$o" -le 255 ] || return 1; done
        return 0
    fi
    if [[ "$s" =~ ^[0-9A-Fa-f:]+$ && "$s" == *:* ]]; then return 0; fi
    [[ "$s" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$ ]]
}

# normalise_servers <list> — print the unique servers, one space-separated line;
# return 1 (naming the offender on stderr) on an invalid entry.
normalise_servers() {
    local s out=""
    # one line of separators and names: a control character is never a separator
    case "$1" in *[[:cntrl:]]*) printf '%s\n' "(control character)" >&2; return 1 ;; esac
    for s in $(printf '%s' "$1" | tr ',;' '  '); do
        valid_host "$s" || { echo "$s" >&2; return 1; }
        case " $out " in *" $s "*) ;; *) out="${out:+$out }$s" ;; esac
    done
    printf '%s\n' "$out"
}

# --- the setting ------------------------------------------------------------------------
CONF_MODE="$(read_env_value "$ENV_FILE" TIME_SOURCE 2>/dev/null)" || CONF_MODE=""
CONF_MODE="${CONF_MODE:-internet}"
CONF_SERVERS="$(read_env_value "$ENV_FILE" TIME_SERVERS 2>/dev/null)" || CONF_SERVERS=""
NET_MODE="$(razzfazz_network_mode "$ENV_FILE" 2>/dev/null)" || NET_MODE="online"

PERSIST=false
if [ -n "$MODE_ARG" ]; then CONF_MODE="$MODE_ARG"; PERSIST=true; fi
if $HAVE_SERVERS; then CONF_SERVERS="$SERVERS_ARG"; PERSIST=true; fi
case "$CONF_MODE" in
    internet|dhcp|lan|none) ;;
    *) die "Invalid time source '$CONF_MODE' (expected internet|dhcp|lan|none)." 64 ;;
esac
if ! SERVERS="$(normalise_servers "$CONF_SERVERS" 2>&1)"; then
    die "Invalid time server '$SERVERS' (expected an IP address or a host name)." 64
fi
if [ "$CONF_MODE" = "lan" ] && [ -z "$SERVERS" ]; then
    die "Time source 'lan' needs at least one server (--servers 10.0.0.1[,10.0.0.2])." 64
fi

# An offline box has no way to the pools: internet reads as none there.
EFFECTIVE="$CONF_MODE"
if [ "$NET_MODE" = "offline" ] && [ "$CONF_MODE" = "internet" ]; then EFFECTIVE="none"; fi

# --- helpers ----------------------------------------------------------------------------
# run <cmd...> — execute, or in --dry-run only print. Returns the command's status;
# the CALLER decides what a failure means (`run x || warn "..."`).
run() {
    if $DRY_RUN; then echo "[DRY-RUN] Would: $*"; return 0; fi
    "$@"
}

unit_exists() { systemctl cat "$1.service" >/dev/null 2>&1; }
chrony_unit() {
    local u
    for u in chrony chronyd; do
        if unit_exists "$u"; then printf '%s\n' "$u"; return 0; fi
    done
    return 1
}

write_dropin() {   # <path> <content>
    if $DRY_RUN; then echo "[DRY-RUN] Would: write $1"; return 0; fi
    mkdir -p "$(dirname "$1")" && printf '%s\n' "$2" > "$1" || die "could not write $1"
}

park_sources() {
    local f
    for f in "$CHRONY_DIR"/conf.d/*.conf "$CHRONY_DIR"/sources.d/*.sources; do
        [ -e "$f" ] || continue
        [ "$f" = "$DROPIN" ] && continue
        case "$f" in *.conf) grep -qE '^[[:space:]]*(pool|server|peer|sourcedir)[[:space:]]' "$f" || continue ;; esac
        run mv -f "$f" "$f$PARK_SUFFIX" || die "could not park $f"
    done
    if [ -f "$CHRONY_DIR/chrony.conf" ] && grep -qE '^(pool|server|peer)[[:space:]]' "$CHRONY_DIR/chrony.conf"; then
        if $DRY_RUN; then echo "[DRY-RUN] Would: park pool/server lines in $CHRONY_DIR/chrony.conf"
        else sed -i -E "s/^(pool|server|peer)[[:space:]]/${PARK_MARK}&/" "$CHRONY_DIR/chrony.conf" || die "could not park the pool lines in $CHRONY_DIR/chrony.conf"; fi
    fi
}

restore_sources() {
    local f
    for f in "$CHRONY_DIR"/conf.d/*"$PARK_SUFFIX" "$CHRONY_DIR"/sources.d/*"$PARK_SUFFIX"; do
        [ -e "$f" ] || continue
        run mv -f "$f" "${f%"$PARK_SUFFIX"}" || die "could not restore $f"
    done
    if [ -f "$CHRONY_DIR/chrony.conf" ] && grep -qF "$PARK_MARK" "$CHRONY_DIR/chrony.conf"; then
        if $DRY_RUN; then echo "[DRY-RUN] Would: restore pool/server lines in $CHRONY_DIR/chrony.conf"
        else sed -i "s/^${PARK_MARK}//" "$CHRONY_DIR/chrony.conf" || die "could not restore the pool lines in $CHRONY_DIR/chrony.conf"; fi
    fi
}

has_source_lines() {
    grep -qE '^[[:space:]]*(pool|server|peer)[[:space:]]' "$CHRONY_DIR/chrony.conf" "$CHRONY_DIR"/conf.d/*.conf "$CHRONY_DIR"/sources.d/*.sources 2>/dev/null
}

install_hook() {
    local st d src="${SCRIPT_DIR}/scripts/chrony-dhcp-hook.sh"
    [ -f "$src" ] || die "missing $src"
    for st in "${HOOK_STATES[@]}"; do
        d="$DISPATCH_DIR/$st.d"
        if $DRY_RUN; then echo "[DRY-RUN] Would: install $d/$HOOK_NAME"; continue; fi
        { mkdir -p "$d" && install -m 0755 "$src" "$d/$HOOK_NAME"; } || warn "could not install $d/$HOOK_NAME (that link state will not refresh the servers)"
    done
    run mkdir -p "$DHCP_DIR" || warn "could not create $DHCP_DIR"
}

remove_hook() {
    local st
    for st in "${HOOK_STATES[@]}"; do
        [ -e "$DISPATCH_DIR/$st.d/$HOOK_NAME" ] || continue
        if $DRY_RUN; then echo "[DRY-RUN] Would: rm $DISPATCH_DIR/$st.d/$HOOK_NAME"; else rm -f "$DISPATCH_DIR/$st.d/$HOOK_NAME" || warn "could not remove $DISPATCH_DIR/$st.d/$HOOK_NAME"; fi
    done
    if [ -d "$DHCP_DIR" ] && ! $DRY_RUN; then rm -f "$DHCP_DIR"/*.sources || warn "could not clear $DHCP_DIR"; fi
}

# --- the chrony path ----------------------------------------------------------------------
apply_chrony() {
    local unit="$1" body
    case "$EFFECTIVE" in
        internet)
            remove_hook
            restore_sources
            if has_source_lines; then
                if $DRY_RUN; then echo "[DRY-RUN] Would: remove $DROPIN"; else rm -f "$DROPIN" || die "could not remove $DROPIN"; fi
            else
                write_dropin "$DROPIN" "# Managed by razzfazz-ai apply-time-source.sh (time source: internet).
pool ntp.ubuntu.com iburst maxsources 4"
            fi ;;
        dhcp)
            park_sources
            install_hook
            body="# Managed by razzfazz-ai apply-time-source.sh (time source: dhcp)."
            grep -qE '^[[:space:]]*sourcedir[[:space:]]+/run/chrony-dhcp' "$CHRONY_DIR/chrony.conf" 2>/dev/null \
                || body="$body
sourcedir $DHCP_DIR"
            write_dropin "$DROPIN" "$body" ;;
        lan)
            remove_hook
            park_sources
            body="# Managed by razzfazz-ai apply-time-source.sh (time source: lan)."
            local s; for s in $SERVERS; do body="$body
server $s iburst"; done
            write_dropin "$DROPIN" "$body" ;;
        none)
            remove_hook
            park_sources
            write_dropin "$DROPIN" "# Managed by razzfazz-ai apply-time-source.sh (time source: none).
# No sources on purpose: the clock is free-running. chrony stays up." ;;
    esac
    if unit_exists systemd-timesyncd; then
        run systemctl disable --now systemd-timesyncd || warn "could not stop systemd-timesyncd: two time daemons may fight over the clock"
    fi
    run systemctl unmask "$unit" || warn "could not unmask $unit"
    run systemctl enable "$unit" || warn "could not enable $unit: it may not start at boot"
    run systemctl restart "$unit" || die "$unit did not restart: check 'systemctl status $unit' and 'journalctl -u $unit'."
    if $DRY_RUN; then return 0; fi
    systemctl is-active --quiet "$unit" 2>/dev/null || die "$unit is not running after the restart."
    if [ "$EFFECTIVE" = "dhcp" ]; then
        RAZZFAZZ_TIME_DHCP_DIR="$DHCP_DIR" bash "${SCRIPT_DIR}/scripts/chrony-dhcp-hook.sh" --all \
            || warn "could not read the current DHCP leases: the servers arrive with the next link event"
    fi
}

# --- the timesyncd path (no chrony on this host) -------------------------------------------
apply_timesyncd() {
    case "$EFFECTIVE" in
        lan)
            write_dropin "$TS_DROPIN" "# Managed by razzfazz-ai apply-time-source.sh (time source: lan).
[Time]
NTP=$SERVERS
FallbackNTP="
            run systemctl unmask systemd-timesyncd || warn "could not unmask systemd-timesyncd"
            run systemctl restart systemd-timesyncd || warn "systemd-timesyncd did not restart" ;;
        none)
            drop_ts_dropin
            run systemctl stop systemd-timesyncd || warn "could not stop systemd-timesyncd"
            run systemctl mask systemd-timesyncd || warn "could not mask systemd-timesyncd" ;;
        *)
            drop_ts_dropin
            run systemctl unmask systemd-timesyncd || warn "could not unmask systemd-timesyncd"
            run systemctl restart systemd-timesyncd || warn "systemd-timesyncd did not restart" ;;
    esac
}

drop_ts_dropin() {
    [ -e "$TS_DROPIN" ] || return 0
    if $DRY_RUN; then echo "[DRY-RUN] Would: remove $TS_DROPIN"; else rm -f "$TS_DROPIN" || warn "could not remove $TS_DROPIN"; fi
}

clock_line() {
    command -v chronyc >/dev/null 2>&1 || return 0
    { chronyc tracking 2>/dev/null | grep -E '^(Leap status|System time)' | tr '\n' ';'; } || return 0
}

# --- status ---------------------------------------------------------------------------------
if [ "$ACTION" = "status" ]; then
    print_step "Time source"
    print_substep "TIME_SOURCE   = ${CONF_MODE} (effective: ${EFFECTIVE})"
    print_substep "TIME_SERVERS  = ${SERVERS:-<none>}"
    print_substep "network mode  = ${NET_MODE}"
    CLOCK="$(clock_line)"
    if [ -n "$CLOCK" ]; then print_substep "chrony        = $CLOCK"; fi
    exit 0
fi

# --- apply ----------------------------------------------------------------------------------
if ! $DRY_RUN && [ "$SKIP_ROOT" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    die "This script must be run as root (sudo) to change the host's time source. Preview without root: --dry-run."
fi

print_step "Time source → ${CONF_MODE}${SERVERS:+ ($SERVERS)}"
if [ "$EFFECTIVE" != "$CONF_MODE" ]; then print_warning "This box is offline: '${CONF_MODE}' cannot reach the pools, so the clock runs free (effective: ${EFFECTIVE}). Set 'lan' or 'dhcp' for a time server on the customer network."; fi
if unit="$(chrony_unit)"; then
    apply_chrony "$unit"
    DAEMON="$unit"
elif unit_exists systemd-timesyncd; then
    apply_timesyncd
    DAEMON="systemd-timesyncd"
else
    $DRY_RUN || die "No time daemon found (no chrony, no systemd-timesyncd): install chrony, then run this again."
    DAEMON="none"
fi

if $PERSIST && ! $DRY_RUN; then
    if [ -f "$ENV_FILE" ]; then
        { update_env_value "$ENV_FILE" TIME_SOURCE "$CONF_MODE" && update_env_value "$ENV_FILE" TIME_SERVERS "$SERVERS"; } \
            || warn "the setting was applied but could not be saved to $ENV_FILE"
    fi
fi
if ! $DRY_RUN; then
    rm -f "$PENDING_FILE" || warn "could not remove the request file $PENDING_FILE"
fi

print_success "Time source applied: ${CONF_MODE} (effective ${EFFECTIVE}, daemon ${DAEMON})"
echo "TIME_SOURCE_RESULT mode=${CONF_MODE} effective=${EFFECTIVE} daemon=${DAEMON}"
exit 0
