#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz.ai Stack — block cloud metadata addresses for every container (#2959)
# ==============================================================================
# A cloud instance answers on its metadata address with the instance's
# credentials (169.254.169.254 on AWS/GCP/Azure/Hetzner/DigitalOcean/Oracle,
# 100.100.100.200 on Alibaba Cloud, fd00:ec2::254 for AWS over IPv6). Any
# container on the box reaches it through Docker's NAT unless the host stops
# it. Models were seen probing it unasked, so the box blocks it for all
# containers, not per service.
#
# Where: Docker's DOCKER-USER chain. Container traffic is FORWARDed through it;
# the host's own traffic (DHCP, its own cloud agent, a link-local GPU
# interconnect) uses OUTPUT and is not touched. DOCKER-USER jumps to our own
# chain RZFZ-METADATA, which is flushed and refilled on every apply, so a re-run
# never stacks duplicates.
#
#   1. the metadata addresses: LOG (rate-limited, prefix rzfz-metadata-drop) + DROP
#   2. METADATA_BLOCK_LINKLOCAL_ALLOW (.env, CIDRs inside 169.254.0.0/16 a
#      container really needs, e.g. a device on a link-local subnet): RETURN
#   3. the rest of 169.254.0.0/16: LOG + DROP
#   IPv6 (only when Docker manages an ip6tables DOCKER-USER chain): fd00:ec2::254.
#
# Persistence: Docker keeps DOCKER-USER across its own restart; a reboot empties
# it. The unit rzfz-metadata-block.service is PartOf= and WantedBy=
# docker.service, so it re-applies after every Docker start and restart. It
# runs an installed copy of this script with the allow list written to
# /etc/razzfazz/metadata-block.conf, so it does not depend on the stack dir.
#
# Usage (root):
#   scripts/metadata-block.sh install   install copy + conf + unit, enable, apply
#   scripts/metadata-block.sh apply     (re)apply the rules now (what the unit runs)
#   scripts/metadata-block.sh status    exit 0 active, 1 missing/incomplete, 3 not readable
#   scripts/metadata-block.sh remove    remove rules, unit, copy and conf
#   scripts/metadata-block.sh rules     print the rules apply installs (no root needed)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${RZFZ_STACK_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# Paths are overridable for the unit tests; none of them grants privileges.
ENV_FILE="${RAZZFAZZ_ENV_FILE:-$STACK_DIR/.env}"
CONF_FILE="${RZFZ_METADATA_BLOCK_CONF:-/etc/razzfazz/metadata-block.conf}"
UNIT_DIR="${RZFZ_METADATA_BLOCK_UNIT_DIR:-/etc/systemd/system}"
INSTALLED_COPY="${RZFZ_METADATA_BLOCK_BIN:-/usr/local/sbin/rzfz-metadata-block}"
UNIT_NAME="rzfz-metadata-block.service"
CHAIN="RZFZ-METADATA"
LOG_PREFIX="rzfz-metadata-drop "

METADATA_V4=(169.254.169.254/32 100.100.100.200/32)
LINKLOCAL_V4="169.254.0.0/16"
METADATA_V6=(fd00:ec2::254/128)

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }

# The allow list: the conf file (what the unit uses) or, when given, .env.
# Only IPv4 CIDRs/addresses INSIDE 169.254.0.0/16 are accepted, and never the
# metadata address itself: the exception is for link-local devices, not for
# the credential endpoint.
read_allow_list() {
    local src="$1" raw="" tok out=""
    [ -r "$src" ] || { printf ''; return 0; }
    raw=$(grep -m1 '^METADATA_BLOCK_LINKLOCAL_ALLOW=' "$src" 2>/dev/null | cut -d= -f2- || true)
    raw="${raw%\"}"; raw="${raw#\"}"; raw="${raw%\'}"; raw="${raw#\'}"
    raw="${raw//,/ }"
    for tok in $raw; do
        if [[ "$tok" =~ ^169\.254\.([0-9]{1,3})\.([0-9]{1,3})(/([0-9]{1,2}))?$ ]] \
            && [ "${BASH_REMATCH[1]}" -le 255 ] && [ "${BASH_REMATCH[2]}" -le 255 ] \
            && { [ -z "${BASH_REMATCH[4]}" ] || { [ "${BASH_REMATCH[4]}" -ge 16 ] && [ "${BASH_REMATCH[4]}" -le 32 ]; }; }; then
            case "$tok" in
                169.254.169.254|169.254.169.254/32)
                    warn "METADATA_BLOCK_LINKLOCAL_ALLOW: $tok is the metadata address itself; ignored" ; continue ;;
            esac
            if [ "${BASH_REMATCH[4]:-32}" -lt 32 ] && _cidr_covers "$tok" 169.254.169.254; then
                warn "METADATA_BLOCK_LINKLOCAL_ALLOW: $tok covers the metadata address; it stays blocked, the rest of the range is allowed"
            fi
            out="${out:+$out }$tok"
        else
            warn "METADATA_BLOCK_LINKLOCAL_ALLOW: '$tok' is not an address or CIDR inside 169.254.0.0/16; ignored"
        fi
    done
    printf '%s' "$out"
}

_ip2int() { local IFS=.; set -- $1; echo $(( ($1 << 24) + ($2 << 16) + ($3 << 8) + $4 )); }
_cidr_covers() {
    local net="${1%/*}" bits="${1#*/}" mask
    [ "$bits" = "$1" ] && bits=32
    mask=$(( bits == 0 ? 0 : (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
    [ $(( $(_ip2int "$net") & mask )) -eq $(( $(_ip2int "$2") & mask )) ]
}

# One rule per line, as iptables arguments after "-A <chain>".
rules_v4() {
    local allow="$1" d c
    for d in "${METADATA_V4[@]}"; do
        echo "-d $d -m limit --limit 10/min --limit-burst 20 -j LOG --log-prefix ${LOG_PREFIX}--log-level 4"
        echo "-d $d -j DROP"
    done
    for c in $allow; do
        echo "-d $c -j RETURN"
    done
    echo "-d $LINKLOCAL_V4 -m limit --limit 10/min --limit-burst 20 -j LOG --log-prefix ${LOG_PREFIX}--log-level 4"
    echo "-d $LINKLOCAL_V4 -j DROP"
}

rules_v6() {
    local d
    for d in "${METADATA_V6[@]}"; do
        echo "-d $d -m limit --limit 10/min --limit-burst 20 -j LOG --log-prefix ${LOG_PREFIX}--log-level 4"
        echo "-d $d -j DROP"
    done
}

# _ipt <iptables|ip6tables> <args...> — the log prefix carries a trailing
# space, so rule lines are split on "--log-prefix" and the prefix is passed as
# one argument.
_add_rule() {
    local tool="$1" line="$2" pre post
    if [[ "$line" == *"--log-prefix ${LOG_PREFIX}"* ]]; then
        pre="${line%%--log-prefix *}"
        post="${line#*--log-prefix ${LOG_PREFIX}}"
        # shellcheck disable=SC2086
        "$tool" -w -A "$CHAIN" $pre --log-prefix "$LOG_PREFIX" $post
    else
        # shellcheck disable=SC2086
        "$tool" -w -A "$CHAIN" $line
    fi
}

_has_docker_user() { "$1" -w -S DOCKER-USER >/dev/null 2>&1; }

# apply_family <tool> <rules...>: 0 applied, 2 no DOCKER-USER chain.
apply_family() {
    local tool="$1"; shift
    local line
    _has_docker_user "$tool" || return 2
    "$tool" -w -S "$CHAIN" >/dev/null 2>&1 || "$tool" -w -N "$CHAIN" || return 1
    "$tool" -w -F "$CHAIN" || return 1
    for line in "$@"; do
        _add_rule "$tool" "$line" || return 1
    done
    # First in DOCKER-USER, ahead of any RETURN/ACCEPT someone added there.
    if ! "$tool" -w -C DOCKER-USER -j "$CHAIN" >/dev/null 2>&1; then
        "$tool" -w -I DOCKER-USER 1 -j "$CHAIN" || return 1
    else
        # Present, but must stay first: move it back to the top when it is not.
        local first
        first=$("$tool" -w -S DOCKER-USER 2>/dev/null | grep '^-A DOCKER-USER' | head -1 || true)
        if [ "$first" != "-A DOCKER-USER -j $CHAIN" ]; then
            "$tool" -w -D DOCKER-USER -j "$CHAIN" && "$tool" -w -I DOCKER-USER 1 -j "$CHAIN" || return 1
        fi
    fi
    return 0
}

cmd_apply() {
    local allow_src="$CONF_FILE" allow rc4 rc6 v4=() v6=() l
    [ -r "$CONF_FILE" ] || allow_src="$ENV_FILE"
    allow="$(read_allow_list "$allow_src")"
    while IFS= read -r l; do v4+=("$l"); done < <(rules_v4 "$allow")
    while IFS= read -r l; do v6+=("$l"); done < <(rules_v6)

    command -v iptables >/dev/null 2>&1 || { say "metadata block: iptables not installed, nothing applied"; return 1; }
    rc4=0; apply_family iptables "${v4[@]}" || rc4=$?
    case "$rc4" in
        0) say "metadata block: IPv4 active (169.254.0.0/16 and 100.100.100.200 dropped for containers${allow:+; allowed: $allow})" ;;
        2) say "metadata block: Docker's DOCKER-USER chain does not exist yet (Docker not started); the unit applies it after Docker starts"; return 2 ;;
        *) say "metadata block: FAILED to apply the IPv4 rules"; return 1 ;;
    esac
    if command -v ip6tables >/dev/null 2>&1; then
        rc6=0; apply_family ip6tables "${v6[@]}" || rc6=$?
        case "$rc6" in
            0) say "metadata block: IPv6 active (fd00:ec2::254 dropped for containers)" ;;
            2) say "metadata block: IPv6 not needed (Docker manages no IPv6 DOCKER-USER chain, so containers have no IPv6 route out)" ;;
            *) say "metadata block: FAILED to apply the IPv6 rules"; return 1 ;;
        esac
    fi
    return 0
}

# status: 0 = active, 1 = missing or incomplete, 3 = cannot read (not root).
cmd_status() {
    local out rc=0
    command -v iptables >/dev/null 2>&1 || { say "iptables not installed"; return 1; }
    if ! out=$(iptables -w -S DOCKER-USER 2>&1); then
        case "$out" in
            *ermission*|*"must be root"*|*"Operation not permitted"*) say "rules not readable (needs root)"; return 3 ;;
        esac
        say "DOCKER-USER chain missing (Docker not running?)"; return 1
    fi
    if ! iptables -w -C DOCKER-USER -j "$CHAIN" >/dev/null 2>&1; then
        say "IPv4: DOCKER-USER does not jump to $CHAIN"; rc=1
    elif ! iptables -w -C "$CHAIN" -d 169.254.169.254/32 -j DROP >/dev/null 2>&1 \
        || ! iptables -w -C "$CHAIN" -d 100.100.100.200/32 -j DROP >/dev/null 2>&1 \
        || ! iptables -w -C "$CHAIN" -d "$LINKLOCAL_V4" -j DROP >/dev/null 2>&1; then
        say "IPv4: $CHAIN is incomplete"; rc=1
    else
        say "IPv4: active"
    fi
    if command -v ip6tables >/dev/null 2>&1 && _has_docker_user ip6tables; then
        if ip6tables -w -C DOCKER-USER -j "$CHAIN" >/dev/null 2>&1 \
            && ip6tables -w -C "$CHAIN" -d fd00:ec2::254/128 -j DROP >/dev/null 2>&1; then
            say "IPv6: active"
        else
            say "IPv6: missing"; rc=1
        fi
    else
        say "IPv6: no IPv6 DOCKER-USER chain (nothing to block)"
    fi
    return "$rc"
}

write_unit() {
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/$UNIT_NAME" <<EOF
# razzfazz.ai — block cloud metadata addresses for all containers.
# Written by scripts/metadata-block.sh install; re-applies the DOCKER-USER rules
# after every Docker start and restart (a reboot empties the chain).
[Unit]
Description=razzfazz.ai cloud metadata block for containers (DOCKER-USER)
After=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$INSTALLED_COPY apply

[Install]
WantedBy=docker.service
EOF
}

cmd_install() {
    local allow rc
    mkdir -p "$(dirname "$CONF_FILE")" "$(dirname "$INSTALLED_COPY")" || return 1
    install -m 0755 "${BASH_SOURCE[0]}" "$INSTALLED_COPY" || return 1
    allow="$(read_allow_list "$ENV_FILE")"
    {
        echo "# Written by scripts/metadata-block.sh from the stack's .env; rewritten on every"
        echo "# install (init, upgrade, harden-host). Edit METADATA_BLOCK_LINKLOCAL_ALLOW in .env."
        echo "METADATA_BLOCK_LINKLOCAL_ALLOW=\"$allow\""
    } > "$CONF_FILE.tmp" && mv -f "$CONF_FILE.tmp" "$CONF_FILE" || return 1
    chmod 0644 "$CONF_FILE" 2>/dev/null || true
    write_unit || return 1
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl enable "$UNIT_NAME" >/dev/null 2>&1 || warn "could not enable $UNIT_NAME"
    else
        warn "systemd not available: the rules are applied now but not re-applied after a reboot"
    fi
    rc=0; cmd_apply || rc=$?
    [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ] || return 1
    # Mark the oneshot active so a non-root `systemctl is-active` sees it.
    if command -v systemctl >/dev/null 2>&1; then
        systemctl start "$UNIT_NAME" >/dev/null 2>&1 || true
    fi
    return 0
}

remove_family() {
    local tool="$1"
    command -v "$tool" >/dev/null 2>&1 || return 0
    while "$tool" -w -D DOCKER-USER -j "$CHAIN" >/dev/null 2>&1; do :; done
    "$tool" -w -F "$CHAIN" >/dev/null 2>&1 || true
    "$tool" -w -X "$CHAIN" >/dev/null 2>&1 || true
}

cmd_remove() {
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now "$UNIT_NAME" >/dev/null 2>&1 || true
    fi
    rm -f "$UNIT_DIR/$UNIT_NAME" "$INSTALLED_COPY" "$CONF_FILE"
    if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload >/dev/null 2>&1 || true; fi
    remove_family iptables
    remove_family ip6tables
    say "metadata block removed (containers can reach cloud metadata addresses again)"
}

cmd_rules() {
    local allow src="$ENV_FILE"
    [ -r "$CONF_FILE" ] && src="$CONF_FILE"
    allow="$(read_allow_list "$src")"
    rules_v4 "$allow" | sed "s/^/iptables -A $CHAIN /"
    rules_v6 | sed "s/^/ip6tables -A $CHAIN /"
    echo "iptables -I DOCKER-USER 1 -j $CHAIN"
    echo "ip6tables -I DOCKER-USER 1 -j $CHAIN"
}

need_root() {
    [ "${RZFZ_METADATA_BLOCK_SKIP_ROOT_CHECK:-0}" = 1 ] && return 0
    [ "$(id -u)" -eq 0 ] && return 0
    say "metadata block: needs root (run with sudo)"
    exit 3
}

case "${1:-apply}" in
    install) need_root; cmd_install ;;
    apply)   need_root; cmd_apply ;;
    status)  cmd_status ;;
    remove)  need_root; cmd_remove ;;
    rules)   cmd_rules ;;
    -h|--help|help) sed -n '2,40p' "${BASH_SOURCE[0]}" ;;
    *) say "usage: $0 install|apply|status|remove|rules"; exit 4 ;;
esac
