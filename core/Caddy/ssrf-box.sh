# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# The Dify SSRF deny/allow lists, computed ONE way for both proxies (#2675)
# ==============================================================================
# Sourced (POSIX sh, BusyBox ash compatible) by:
#   * core/Caddy/entrypoint.sh  — the stack's :8195 forward proxy, at start;
#   * modules/dify/ssrf-chain/render.sh — the squid chain that replaces :8195
#     on a corporate-proxy box, when scripts/apply-corporate-proxy.sh renders it.
#
# Functions (results on stdout, space-separated; diagnostics on stderr):
#   ssrf_static_denies CADDYFILE  every CIDR the :8195 acl denies, read from the
#                                 Caddyfile itself: the ONE list both proxies use
#   ssrf_box_list                 the box's own addresses (/32, /128):
#       HOST_IP, plus every address MAIN_DOMAIN and the *_DOMAIN values under
#       it (never *_OAUTH_DOMAIN) resolve to here — or, when DIFY_SSRF_DENY_BOX_OVERRIDE is set, that
#       list instead of the resolved ones ("none" = no resolved address; for a
#       box behind a CDN or load balancer, whose names resolve to edge
#       addresses that must not be denied wholesale) — plus
#       DIFY_SSRF_DENY_BOX_EXTRA. Only IP addresses that are neither loopback
#       nor unspecified are kept.
#   ssrf_allow_ranges             DIFY_SSRF_ALLOW_RANGES, validated: IPv4 CIDRs
#       inside 100.64.0.0/10 or 198.18.0.0/15 only (Tailscale, Zscaler ZPA,
#       WARP, fake-IP DNS). Anything else is dropped with a WARN.
#
# Test hooks: SSRF_RESOLVE_TIMEOUT (seconds per lookup, default 3; all names
# are looked up in parallel).
# ==============================================================================

ssrf_warn() { echo "[ssrf] WARN(#2675): $*" >&2; }

# A usable box address as a CIDR ("<ip>/32", "<ip>/128"), or nothing.
ssrf_box_cidr() {
    _sa="$1"
    case "$_sa" in
        *:*)
            case "$_sa" in
                *[!0-9A-Fa-f:]*|*:::*|*::*::*|::|::1|::0|0::1) return 0 ;;
            esac
            _sn=$(printf '%s' "$_sa" | awk -F: '{print NF-1}')
            case "$_sa" in
                *::*) [ "$_sn" -ge 2 ] && [ "$_sn" -le 7 ] || return 0 ;;
                *)    [ "$_sn" -eq 7 ] || return 0 ;;
            esac
            for _sg in $(printf '%s' "$_sa" | tr ':' ' '); do
                [ "${#_sg}" -le 4 ] || return 0
            done
            echo "$_sa/128" ;;
        *.*.*.*)
            _ssrf_ipv4_ok "$_sa" || return 0
            _so1=$(echo "$_sa" | cut -d. -f1)
            [ "$_so1" -ne 0 ] && [ "$_so1" -ne 127 ] || return 0
            echo "$_sa/32" ;;
    esac
    return 0
}

# Four decimal octets 0..255, no leading zero (octal to some parsers).
_ssrf_ipv4_ok() {
    case "$1" in *[!0-9.]*|*..*|.*|*.) return 1 ;; esac
    [ "$(echo "$1" | cut -d. -f5-)" = "" ] || return 1
    [ "$(echo "$1" | cut -d. -f4)" != "" ] || return 1
    for _so in $(echo "$1" | tr '.' ' '); do
        case "$_so" in 0?*) return 1 ;; esac
        [ "${#_so}" -le 3 ] && [ "$_so" -le 255 ] || return 1
    done
    return 0
}

# Every A/AAAA answer for one name, one per line. Bounded by a timeout so a
# box without a resolver does not stall the proxy's start.
_ssrf_resolve() {
    _st="${SSRF_RESOLVE_TIMEOUT:-3}"
    _sto=""
    command -v timeout >/dev/null 2>&1 && _sto="timeout $_st"
    _sr=""
    if command -v getent >/dev/null 2>&1; then
        _sr=$($_sto getent ahosts "$1" 2>/dev/null | awk '{print $1}' | sort -u || true)
    fi
    if [ -z "$_sr" ] && command -v nslookup >/dev/null 2>&1; then
        # BusyBox nslookup: the answer's "Address:" lines follow its "Name:"
        # line (the ones before it name the DNS server).
        _sr=$($_sto nslookup "$1" 2>/dev/null \
            | awk '/^Name:/ {ans=1; next} ans && /^Address/ {print $NF}' | sort -u || true)
    fi
    [ -z "$_sr" ] || printf '%s\n' "$_sr"
}

# The box's own host names: MAIN_DOMAIN and every *_DOMAIN value that equals
# it or ends in ".MAIN_DOMAIN" ("*.x" and ".x" -> x, case-insensitive).
# *_OAUTH_DOMAIN keys are never box names: GOOGLE_OAUTH_DOMAIN is the
# organisation's own domain, whose web and CDN addresses must stay reachable.
# A value outside MAIN_DOMAIN is not resolved either (add such an address
# with DIFY_SSRF_DENY_BOX_EXTRA).
_ssrf_domains() {
    _sm=$(printf '%s' "${MAIN_DOMAIN:-}" | tr 'A-Z' 'a-z' | sed 's/^\*\.//; s/^\.//; s/\.$//')
    [ -n "$_sm" ] || return 0
    env | sed -n 's/^\([A-Z0-9_]*_DOMAIN\)=\(.*\)$/\1 \2/p' | while read -r _sk _sv; do
        case "$_sk" in *_OAUTH_DOMAIN) continue ;; esac
        _sv=$(printf '%s' "$_sv" | tr 'A-Z' 'a-z' | sed 's/^\*\.//; s/^\.//; s/\.$//')
        case "$_sv" in
            "$_sm"|*."$_sm") printf '%s\n' "$_sv" ;;
        esac
    done | grep -E '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' | sort -u
}

ssrf_box_list() {
    _sb=$(ssrf_box_cidr "${HOST_IP:-}")
    [ -z "${HOST_IP:-}" ] || [ -n "$_sb" ] || ssrf_warn "HOST_IP '${HOST_IP}' is not a usable box address; ignored"
    _sres=""
    if [ -n "${DIFY_SSRF_DENY_BOX_OVERRIDE:-}" ]; then
        if [ "$DIFY_SSRF_DENY_BOX_OVERRIDE" != none ]; then
            _sres=$(printf '%s\n' $DIFY_SSRF_DENY_BOX_OVERRIDE)
        fi
    else
        _sdir=$(mktemp -d 2>/dev/null || echo "/tmp/ssrf-box.$$")
        mkdir -p "$_sdir"
        _si=0
        for _sd in $(_ssrf_domains); do
            _si=$((_si + 1))
            ( _ssrf_resolve "$_sd" > "$_sdir/$_si" ) &
        done
        wait
        _sres=$(cat "$_sdir"/* 2>/dev/null || true)
        rm -rf "$_sdir"
        [ -n "$_sres" ] || [ "$_si" -eq 0 ] || \
            ssrf_warn "no *_DOMAIN name resolves here: the box's public addresses are NOT denied, only HOST_IP (set DIFY_SSRF_DENY_BOX_EXTRA)"
    fi
    _sres="$_sres
$(printf '%s\n' ${DIFY_SSRF_DENY_BOX_EXTRA:-})"
    for _sa2 in $_sres; do
        _sc=$(ssrf_box_cidr "$_sa2")
        [ -n "$_sc" ] && _sb="$_sb $_sc"
    done
    printf '%s\n' $_sb | awk 'NF' | sort -u | tr '\n' ' ' | sed 's/ $//'
}

ssrf_allow_ranges() {
    _sok=""
    for _sx in ${DIFY_SSRF_ALLOW_RANGES:-}; do
        _sip="${_sx%/*}"; _sl="${_sx#*/}"
        if [ "$_sip" = "$_sx" ] || ! _ssrf_ipv4_ok "$_sip"; then
            ssrf_warn "DIFY_SSRF_ALLOW_RANGES entry '$_sx' is not an IPv4 CIDR; ignored"; continue
        fi
        case "$_sl" in ''|*[!0-9]*|0?*) ssrf_warn "DIFY_SSRF_ALLOW_RANGES entry '$_sx' has no valid prefix length; ignored"; continue ;; esac
        _s1=$(echo "$_sip" | cut -d. -f1); _s2=$(echo "$_sip" | cut -d. -f2)
        _s3=$(echo "$_sip" | cut -d. -f3); _s4=$(echo "$_sip" | cut -d. -f4)
        _sv=$(( (_s1 << 24) | (_s2 << 16) | (_s3 << 8) | _s4 ))
        if [ "$_sl" -ge 10 ] && [ "$_sl" -le 32 ] && [ $((_sv >> 22)) -eq $(( (100 << 2) | 1 )) ]; then :
        elif [ "$_sl" -ge 15 ] && [ "$_sl" -le 32 ] && [ $((_sv >> 17)) -eq $(( (198 << 7) | 9 )) ]; then :
        else
            ssrf_warn "DIFY_SSRF_ALLOW_RANGES entry '$_sx' is not inside 100.64.0.0/10 or 198.18.0.0/15; ignored"; continue
        fi
        if [ "$_sl" -lt 32 ] && [ $(( _sv & ((1 << (32 - _sl)) - 1) )) -ne 0 ]; then
            ssrf_warn "DIFY_SSRF_ALLOW_RANGES entry '$_sx' has host bits set; ignored"; continue
        fi
        _sok="$_sok $_sx"
    done
    printf '%s\n' $_sok | awk 'NF' | sort -u | tr '\n' ' ' | sed 's/ $//'
}

ssrf_static_denies() {
    awk '
        /^:8195 \{/ { site = 1 }
        site && /^[ \t]*acl \{/ { acl = 1; next }
        acl && /^[ \t]*allow all/ { exit }
        acl {
            sub(/#.*/, "")
            if ($1 == "deny")
                for (i = 2; i <= NF; i++) if ($i !~ /^\{/) printf "%s ", $i
        }
    ' "$1" | sed 's/ $//'
}
