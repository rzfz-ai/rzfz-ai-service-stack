#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# docling-egress entrypoint (#2086; design record §7, rulings I3/R2-1)
# ==============================================================================
# Resolves the box's own addresses, which the egress proxy must also refuse,
# into DOCLING_EGRESS_DENY_EXTRA (a parse-time list the Caddyfile reads),
# turns the relay secret into a bcrypt hash for the relay's basic_auth, then
# execs caddy with ./Caddyfile (the plain secret is unset first).
#
# The box's own addresses (ruling of B8 fix round 1, amending the design
# record's "refuse when MAIN_DOMAIN does not resolve"):
#   * HOST_IP (cli/init.sh writes it), when set, and
#   * every A/AAAA address MAIN_DOMAIN resolves to INSIDE the container.
# A domain that exists only in the host's /etc/hosts (the stack's own
# setup_local_dns) or on an offline box does not resolve here; with HOST_IP set
# the sidecar still starts, with a WARN line naming the gap, and `rzfz status`
# WARNs (it reads the state file below). It REFUSES TO START when:
#   * no usable box address survives into the deny list (HOST_IP and
#     MAIN_DOMAIN's addresses are validated: not an IP, loopback or
#     unspecified does not count); the state file says so either way;
#   * the relay secret is missing or not a plain token of >= 32 characters;
#   * there is no default route (the Docker host, via docling-edge).
# Also denied: every directly connected IPv4 subnet of this container, so the
# peers on docling-edge (mcp-manager) stay unreachable through the proxy even
# on a Docker address pool outside the RFC1918 ranges.
#
# BusyBox sh (the caddy image is Alpine). Test hooks:
#   DOCLING_EGRESS_ROUTE_FILE   default /proc/net/route
#   DOCLING_EGRESS_STATUS_FILE  default /tmp/docling-egress.status
#   DOCLING_EGRESS_CONFIG       default /etc/docling-egress/Caddyfile
#   CADDY_BIN                   default caddy
#   DOCLING_EGRESS_DRY_RUN=1    print the list instead of exec'ing caddy
# ==============================================================================
set -eu

die() { echo "[docling-egress] ERROR: $*" >&2; exit 1; }

warn() { echo "[docling-egress] WARN: $*" >&2; }

secret="${DOCLING_RELAY_SECRET:-}"
[ -n "$secret" ] || die "DOCLING_RELAY_SECRET is empty (cli/init.sh / rzfz upgrade generate it)"
# hex (init/upgrade) or base64 (rotate-secret): no whitespace, quote or `*`.
case "$secret" in
    *[!A-Za-z0-9+/=_-]*) die "DOCLING_RELAY_SECRET must be hex or base64 characters only" ;;
esac
[ "${#secret}" -ge 32 ] || die "DOCLING_RELAY_SECRET is shorter than 32 characters"
# bcrypt reads only the first 72 bytes of a password: a longer secret would
# be accepted on its first 72 alone. The relay's header regexp admits what
# 32..72 characters encode to. (init/upgrade mint 64 hex characters,
# rotate-secret 64 base64 characters: none changes.)
[ "${#secret}" -le 72 ] || die "DOCLING_RELAY_SECRET is longer than 72 characters (bcrypt reads only the first 72): regenerate it with 'rzfz rotate-secret --rotate DOCLING_RELAY_SECRET'"
unset secret

status_file="${DOCLING_EGRESS_STATUS_FILE:-/tmp/docling-egress.status}"

# A usable box address as a CIDR ("<ip>/32", "<ip>/128"), or nothing: not an
# IP address, loopback or unspecified (those are denied statically anyway and
# name no box).
box_cidr() {
    a="$1"
    case "$a" in
        *:*)
            case "$a" in
                *[!0-9A-Fa-f:]*|*:::*|*::*::*|::|::1|::0|0::1) return 0 ;;
            esac
            n=$(printf '%s' "$a" | awk -F: '{print NF-1}')
            case "$a" in
                *::*) [ "$n" -ge 2 ] && [ "$n" -le 7 ] || return 0 ;;
                *)    [ "$n" -eq 7 ] || return 0 ;;
            esac
            for g in $(printf '%s' "$a" | tr ':' ' '); do
                [ "${#g}" -le 4 ] || return 0
            done
            echo "$a/128" ;;
        *.*.*.*)
            case "$a" in *[!0-9.]*|*..*|.*|*.) return 0 ;; esac
            o1=$(echo "$a" | cut -d. -f1); o2=$(echo "$a" | cut -d. -f2)
            o3=$(echo "$a" | cut -d. -f3); o4=$(echo "$a" | cut -d. -f4)
            [ "$(echo "$a" | cut -d. -f5-)" = "" ] || return 0
            for o in "$o1" "$o2" "$o3" "$o4"; do
                # a leading zero is octal to some parsers ("010" = 8): refused
                case "$o" in 0?*) return 0 ;; esac
                [ "${#o}" -le 3 ] && [ "$o" -le 255 ] || return 0
            done
            [ "$o1" -ne 0 ] && [ "$o1" -ne 127 ] || return 0
            echo "$a/32" ;;
    esac
    return 0
}

# HOST_IP: the host's LAN/public address as init recorded it.
host_ip="${HOST_IP:-}"
host_cidr=$(box_cidr "$host_ip")
if [ -n "$host_cidr" ]; then
    host_state=set
elif [ -n "$host_ip" ]; then
    host_state=invalid
    warn "HOST_IP '$host_ip' is not a usable box address; ignored"
else
    host_state=unset
fi

# MAIN_DOMAIN: every A and AAAA record, as resolved inside this container.
domain="${MAIN_DOMAIN:-}"
addrs=""
if [ -n "$domain" ]; then
    if command -v getent >/dev/null 2>&1; then
        addrs=$(getent ahosts "$domain" 2>/dev/null | awk '{print $1}' | sort -u || true)
    fi
    if [ -z "$addrs" ] && command -v nslookup >/dev/null 2>&1; then
        # BusyBox nslookup: the answer's "Address:" lines follow its "Name:"
        # line (the ones before it name the DNS server).
        addrs=$(nslookup "$domain" 2>/dev/null \
            | awk '/^Name:/ {ans=1; next} ans && /^Address/ {print $NF}' | sort -u || true)
    fi
fi
domain_cidrs=""
for a in $addrs; do
    c=$(box_cidr "$a")
    [ -n "$c" ] && domain_cidrs="$domain_cidrs $c"
done
if [ -n "$domain_cidrs" ]; then
    main_state=resolved
elif [ -n "$domain" ]; then
    main_state=unresolved
else
    main_state=unset
fi

# The box addresses that SURVIVE into the deny list decide everything below:
# the refusal and the state file `rzfz status` reads.
box_cidrs=$(printf '%s\n' $host_cidr $domain_cidrs | awk 'NF' | sort -u | tr '\n' ' ')
if [ -n "$box_cidrs" ]; then deny_state=ok; else deny_state=none; fi
printf 'main_domain=%s\nhost_ip=%s\nbox_addresses=%s\n' "$main_state" "$host_state" "$deny_state" \
    > "$status_file" 2>/dev/null || warn "cannot write $status_file"
[ "$deny_state" = ok ] || die "no usable box address (HOST_IP '$host_ip': $host_state; MAIN_DOMAIN '$domain': $main_state here): refusing to start without the box's own addresses in the deny list"
if [ "$main_state" != resolved ]; then
    warn "MAIN_DOMAIN '$domain' is $main_state inside the container: its public addresses are NOT in the deny list, only HOST_IP $host_ip (a domain that lives only in the host's /etc/hosts, or an offline box without a resolver)"
fi

# /proc/net/route: hex little-endian IPv4. The default route's gateway is the
# Docker host; every route without a gateway is a directly connected subnet.
route_file="${DOCLING_EGRESS_ROUTE_FILE:-/proc/net/route}"
[ -r "$route_file" ] || die "cannot read $route_file"
hex_ip() {
    h="$1"
    printf '%d.%d.%d.%d' "0x$(echo "$h" | cut -c7-8)" "0x$(echo "$h" | cut -c5-6)" \
        "0x$(echo "$h" | cut -c3-4)" "0x$(echo "$h" | cut -c1-2)"
}
mask_bits() {
    m="$1"; n=0
    for b in $(echo "$m" | cut -c1-2) $(echo "$m" | cut -c3-4) $(echo "$m" | cut -c5-6) $(echo "$m" | cut -c7-8); do
        v=$(printf '%d' "0x$b")
        while [ "$v" -gt 0 ]; do n=$((n + (v & 1))); v=$((v >> 1)); done
    done
    echo "$n"
}
gateway=""
subnets=""
# shellcheck disable=SC2034
while read -r iface dest gw flags refcnt use metric mask rest; do
    case "$dest" in Destination) continue ;; esac
    if [ "$dest" = "00000000" ] && [ "$gw" != "00000000" ]; then
        gateway="$(hex_ip "$gw")/32"
    elif [ "$gw" = "00000000" ] && [ "$dest" != "00000000" ]; then
        subnets="$subnets $(hex_ip "$dest")/$(mask_bits "$mask")"
    fi
done < "$route_file"
[ -n "$gateway" ] || die "no default route: cannot name the Docker host to deny"

extra="$box_cidrs $gateway$subnets"
DOCLING_EGRESS_DENY_EXTRA=$(echo "$extra" | tr -s ' ' | sed 's/^ //')
export DOCLING_EGRESS_DENY_EXTRA
echo "[docling-egress] also denied (box addresses, Docker host, own subnets): $DOCLING_EGRESS_DENY_EXTRA"

if [ -n "${DOCLING_EGRESS_DRY_RUN:-}" ]; then
    echo "DENY_EXTRA=$DOCLING_EGRESS_DENY_EXTRA"
    exit 0
fi

# The relay's credential, as a bcrypt hash: basic_auth compares against it in
# time that does not depend on the guess. The secret goes in on stdin (never
# argv); caddy itself never sees it. Cost 4: the secret is random, so
# stretching adds nothing, and a cheap check keeps wrong guesses cheap.
# The tool's stderr is NOT discarded: it goes to the container log, so a
# failure names itself (a binary the kernel refuses to exec said only "could
# not hash the relay secret" while stderr went to /dev/null — #2086).
caddy_bin="${CADDY_BIN:-caddy}"
DOCLING_RELAY_HASH=$(printf '%s\n' "$DOCLING_RELAY_SECRET" \
    | "$caddy_bin" hash-password --algorithm bcrypt --bcrypt-cost 4 | tail -n 1)
case "$DOCLING_RELAY_HASH" in
    '$2'*) ;;
    *) die "could not hash the relay secret ('$caddy_bin hash-password' failed; its own error is above)" ;;
esac
export DOCLING_RELAY_HASH
unset DOCLING_RELAY_SECRET
exec "$caddy_bin" run --config "${DOCLING_EGRESS_CONFIG:-/etc/docling-egress/Caddyfile}" --adapter caddyfile
