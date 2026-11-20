#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
set -euo pipefail
# scripts/staging/isolate-egress.sh — #2524: a staging box's CONTAINERS cannot
# reach production or any mail port. The HOST keeps its own egress (it fetches
# release tags); containers traverse FORWARD -> DOCKER-USER, the host uses
# OUTPUT, so every rule lives in DOCKER-USER. Idempotent (iptables -C first).
#
# What --check proves, because each one is a way around the rules:
#   * every rule is present, and in front of any untagged rule that can let a
#     packet out of DOCKER-USER (ACCEPT, RETURN, a jump/goto to another chain);
#   * no Docker network has IPv6 enabled (the rules are IPv4 only);
#   * no running container uses host networking (it skips FORWARD entirely);
#   * the boot unit is active (a unit that failed at boot left a window).
# --boot (used by the unit's own ExecStartPost, before docker.service runs)
# checks the rules only.
#
# Usage:
#   isolate-egress.sh --deny-ip IP [--deny-ip IP ...] [--mail-ports 25,465,587]
#   isolate-egress.sh ... --check [--boot]  # exit 0 iff isolation is complete
#   isolate-egress.sh ... --install-unit    # apply, and re-apply on every boot
#                                           # BEFORE docker.service starts
# Exit: 0 ok; 1 --check found a gap; 2 bad arguments.

TAG="rzfz-staging-isolation"
UNIT_NAME="rzfz-staging-isolation.service"
SBIN_PATH="/usr/local/sbin/rzfz-staging-isolate-egress"
DESTDIR="${RZFZ_ISOLATE_DESTDIR:-}"        # tests only: a root to install under
DENY_IPS=()
MAIL_PORTS="25,465,587"
MODE=apply
BOOT=0

die() { echo "[isolate-egress] ERROR: $*" >&2; exit 2; }
valid_ip() {
    [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    local o; for o in "${BASH_REMATCH[@]:1}"; do [ "$o" -le 255 ] || return 1; done
}

while [ $# -gt 0 ]; do
    case "$1" in
        --deny-ip)      valid_ip "${2:-}" || die "--deny-ip needs an IPv4 address (got '${2:-}')"
                        DENY_IPS+=("$2"); shift 2 ;;
        --mail-ports)   [[ "${2:-}" =~ ^[0-9]{1,5}(,[0-9]{1,5})*$ ]] || die "--mail-ports needs N[,N...] (got '${2:-}')"
                        MAIL_PORTS="$2"; shift 2 ;;
        --check)        MODE=check; shift ;;
        --boot)         BOOT=1; shift ;;
        --install-unit) MODE=install; shift ;;
        -h|--help)      sed -n '/^# Usage:/,/^# Exit:/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)              die "unknown argument '$1'" ;;
    esac
done
[ "${#DENY_IPS[@]}" -gt 0 ] || die "at least one --deny-ip is required (production's addresses)"
if [ "${RZFZ_ISOLATE_SKIP_ROOT_CHECK:-0}" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    die "must run as root"
fi

RULES=()
for ip in "${DENY_IPS[@]}"; do
    RULES+=("DOCKER-USER -d $ip -m comment --comment $TAG -j REJECT --reject-with icmp-host-prohibited")
done
RULES+=("DOCKER-USER -p tcp -m multiport --dports $MAIL_PORTS -m comment --comment $TAG -j REJECT --reject-with tcp-reset")

# At boot this runs before docker.service, so the chain may not exist yet.
# Docker keeps an existing DOCKER-USER chain and only adds its jump to it.
if [ "$MODE" != check ]; then
    iptables -w -N DOCKER-USER 2>/dev/null || true
fi

gaps=0
gap() { echo "[isolate-egress] $*"; gaps=$((gaps + 1)); }

for rule in "${RULES[@]}"; do
    # shellcheck disable=SC2086  # a rule is a word list by construction
    if iptables -w -C $rule 2>/dev/null; then
        continue
    fi
    if [ "$MODE" = check ]; then
        gap "MISSING: $rule"
    else
        # shellcheck disable=SC2086
        iptables -w -I $rule
        echo "[isolate-egress] added: $rule"
    fi
done

check_order() {
    # Every tagged rule must come before the first untagged rule that can let
    # a packet out of DOCKER-USER (any -j/-g other than DROP, REJECT, LOG).
    local line seen_exit="" n=0
    while IFS= read -r line; do
        case "$line" in -A\ DOCKER-USER*) ;; *) continue ;; esac
        n=$((n + 1))
        if [[ "$line" == *"--comment $TAG"* ]]; then
            [ -z "$seen_exit" ] || gap "ORDER: rule $n ($line) comes after '$seen_exit' and is never reached"
        elif [[ "$line" =~ (^|\ )-[jg]\ ([^ ]+) ]]; then
            # Only a terminal refusal or a LOG is harmless ahead of our rules:
            # ACCEPT/RETURN end the chain, and a jump or goto to another chain
            # can ACCEPT there.
            case "${BASH_REMATCH[2]}" in
                DROP|REJECT|LOG) ;;
                *) [ -n "$seen_exit" ] || seen_exit="$line" ;;
            esac
        fi
    done < <(iptables -w -S DOCKER-USER 2>/dev/null)
}

check_unit() {
    local state
    if ! command -v systemctl >/dev/null 2>&1; then
        gap "UNIT: systemctl not available, cannot prove $UNIT_NAME re-applies the rules at boot"
        return
    fi
    state="$(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)"
    case "$state" in
        active|activating) ;;
        *) gap "UNIT: $UNIT_NAME is '${state:-unknown}' — the rules are not re-applied before docker at boot (run --install-unit, then: systemctl status $UNIT_NAME)" ;;
    esac
}

check_docker_bypasses() {
    local line ids
    if ! docker info >/dev/null 2>&1; then
        gap "DOCKER: the docker daemon cannot be asked — IPv6 and host-network bypasses are unproven"
        return
    fi
    # shellcheck disable=SC2046  # one id per word
    while IFS=' ' read -r name v6; do
        [ -n "$name" ] || continue
        [ "$v6" = true ] && gap "IPv6: docker network '$name' has IPv6 enabled; the rules are IPv4 only"
    done < <(docker network inspect -f '{{.Name}} {{.EnableIPv6}}' $(docker network ls -q) 2>/dev/null || true)
    ids="$(docker ps -q 2>/dev/null || true)"
    [ -n "$ids" ] || return 0
    # shellcheck disable=SC2086
    while IFS=' ' read -r name mode; do
        [ -n "$name" ] || continue
        [ "$mode" = host ] && gap "HOST-NETWORK: container $name uses network_mode host and bypasses DOCKER-USER"
    done < <(docker inspect -f '{{.Name}} {{.HostConfig.NetworkMode}}' $ids 2>/dev/null || true)
    return 0
}

if [ "$MODE" = check ]; then
    check_order
    if [ "$BOOT" -eq 0 ]; then
        check_unit
        check_docker_bypasses
    fi
    if [ "$gaps" -eq 0 ]; then
        echo "[isolate-egress] isolation complete: ${#RULES[@]} rule(s) present and in front$([ "$BOOT" -eq 1 ] && echo ' (boot check: rules only)')"
        exit 0
    fi
    echo "[isolate-egress] $gaps gap(s) — staging's containers are not isolated"
    exit 1
fi

if [ "$MODE" = install ]; then
    args=""; for ip in "${DENY_IPS[@]}"; do args="$args --deny-ip $ip"; done
    args="$args --mail-ports $MAIL_PORTS"
    mkdir -p "$DESTDIR/usr/local/sbin" "$DESTDIR/etc/systemd/system"
    install -m 0755 "$0" "$DESTDIR$SBIN_PATH"
    cat > "$DESTDIR/etc/systemd/system/$UNIT_NAME" <<EOF
[Unit]
Description=rzfz.ai staging: containers cannot reach production or mail ports
DefaultDependencies=no
# Ordering only: a firewall service that flushes the ruleset runs first, so it
# cannot wipe these rules afterwards.
After=local-fs.target nftables.service firewalld.service netfilter-persistent.service ufw.service iptables.service
Before=docker.service network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=$SBIN_PATH$args
ExecStartPost=$SBIN_PATH$args --check --boot
RemainAfterExit=yes

[Install]
# Required, not wanted: if this unit (or its --check --boot) fails, docker
# does not start at all, rather than starting unisolated.
RequiredBy=docker.service
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$UNIT_NAME"
    echo "[isolate-egress] unit installed ($DESTDIR$SBIN_PATH): re-applied before docker.service on every boot"
fi
