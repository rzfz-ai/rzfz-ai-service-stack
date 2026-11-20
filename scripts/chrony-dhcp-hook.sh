#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# chrony-dhcp-hook.sh — hand the NTP servers a DHCP lease announces to chrony
# =============================================================================
# chrony ships DHCP integration only for dhclient and NetworkManager. On a
# systemd-networkd / netplan host nothing passes the lease's NTP= servers on, so
# the "dhcp" time source (scripts/apply-time-source.sh) installs THIS script as a
# networkd-dispatcher hook. It reads the lease systemd-networkd keeps for the
# interface, writes /run/chrony-dhcp/<iface>.sources (the directory chrony reads
# through `sourcedir /run/chrony-dhcp`) and tells chrony to reload its sources.
#
# Two ways in:
#   - networkd-dispatcher: IFACE and STATE are in the environment. A routable or
#     degraded link (re)writes the sources file, any other state removes it.
#   - `--all`: every interface that has a lease right now (used right after the
#     time source is applied, so nothing waits for the next link event).
#
# A lease is data from the network: each server is checked to be an IP address or
# a plain host name before it is written into a chrony config file, so a lease
# cannot add a directive.
#
# Failure policy: the script stops on an unexpected error (set -e). The steps that
# may fail without that being an error carry an explicit `|| warn` / `|| return`,
# so a tolerated failure is a decision you can read here.
# =============================================================================
set -euo pipefail

LEASE_DIR="${RAZZFAZZ_TIME_LEASE_DIR:-/run/systemd/netif/leases}"
SYS_NET="${RAZZFAZZ_TIME_SYS_NET:-/sys/class/net}"
DHCP_DIR="${RAZZFAZZ_TIME_DHCP_DIR:-/run/chrony-dhcp}"

warn() { echo "chrony-dhcp-hook: $*" >&2; }

valid_server() {
    local s="$1"
    [ -n "$s" ] && [ "${#s}" -le 253 ] || return 1
    # IPv4, IPv6 (hex and colons), or a dotted host name.
    if [[ "$s" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then return 0; fi
    if [[ "$s" =~ ^[0-9A-Fa-f:]+$ && "$s" == *:* ]]; then return 0; fi
    if [[ "$s" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$ ]]; then return 0; fi
    return 1
}

# iface_name_ok — an interface name is a path component here: no slash, no dots-only.
iface_name_ok() { [[ "$1" =~ ^[A-Za-z0-9_.:@-]{1,15}$ && "$1" != "." && "$1" != ".." ]]; }

# write_iface <iface> — rewrite (or remove) the sources file of one interface.
write_iface() {
    local iface="$1" idx lease servers s out="" n=0
    iface_name_ok "$iface" || { warn "refusing the interface name '$iface'"; return 1; }
    idx="$(cat "$SYS_NET/$iface/ifindex" 2>/dev/null)" || idx=""
    lease="$LEASE_DIR/$idx"
    if [ -z "$idx" ] || [ ! -r "$lease" ]; then
        rm -f "$DHCP_DIR/$iface.sources" || warn "could not remove $DHCP_DIR/$iface.sources"
        return 0
    fi
    servers="$(sed -n 's/^NTP=//p' "$lease" | head -1)" || servers=""
    for s in $servers; do
        valid_server "$s" || { warn "ignoring '$s' from the lease of $iface"; continue; }
        out+="server $s iburst"$'\n'
        n=$((n + 1))
    done
    if [ "$n" -eq 0 ]; then
        rm -f "$DHCP_DIR/$iface.sources" || warn "could not remove $DHCP_DIR/$iface.sources"
        return 0
    fi
    mkdir -p "$DHCP_DIR" || { warn "could not create $DHCP_DIR"; return 1; }
    printf '%s' "$out" > "$DHCP_DIR/.$iface.tmp" && mv -f "$DHCP_DIR/.$iface.tmp" "$DHCP_DIR/$iface.sources" \
        || { warn "could not write $DHCP_DIR/$iface.sources"; return 1; }
}

# reload_chrony — a missing or silent chrony is not an error of this hook.
reload_chrony() {
    command -v chronyc >/dev/null 2>&1 || return 0
    chronyc reload sources >/dev/null 2>&1 || warn "chronyc reload sources failed (chrony down?)"
}

if [ "${1:-}" = "--all" ]; then
    for d in "$SYS_NET"/*; do
        [ -e "$d/ifindex" ] || continue
        write_iface "$(basename "$d")" || warn "skipped $(basename "$d")"
    done
    reload_chrony
    exit 0
fi

IFACE="${IFACE:-}"
[ -n "$IFACE" ] || { warn "IFACE is not set (run from networkd-dispatcher, or pass --all)"; exit 64; }
case "${STATE:-routable}" in
    routable|degraded) write_iface "$IFACE" || warn "no sources written for $IFACE" ;;
    *) if iface_name_ok "$IFACE"; then rm -f "$DHCP_DIR/$IFACE.sources" || warn "could not remove the sources of $IFACE"; fi ;;
esac
reload_chrony
exit 0
