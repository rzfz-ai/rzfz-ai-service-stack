#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# Render the dify-ssrf-chain squid.conf (#277, #2675)
# ==============================================================================
# Usage: render.sh TEMPLATE OUT WSA_HOST WSA_PORT DOCTOOLS ENV_FILE CADDYFILE
#
# The SSRF lists come from core/Caddy/ssrf-box.sh, the code the stack's Caddy
# entrypoint uses for :8195, so both proxies deny the same set:
#   to_internal        every range the :8195 acl denies (read from CADDYFILE)
#   to_box             the box's own addresses
#   ssrf_allow_ranges  the operator's validated DIFY_SSRF_ALLOW_RANGES
#   ssrf_deny_names / ssrf_deny_nets  DIFY_SSRF_DENY_HOSTS (#2524), as :8195
#                      applies it: host names (`*.x` = x and every name below
#                      it, case-insensitive) refused even for a doc-tools host,
#                      IP/CIDR entries refused after the doc-tools allow
# ENV_FILE is read, never sourced: only HOST_IP, the *_DOMAIN keys and the
# DIFY_SSRF_* keys are taken from it, as plain values.
# ==============================================================================
set -eu
[ "$#" -eq 7 ] || { echo "usage: $0 TEMPLATE OUT WSA_HOST WSA_PORT DOCTOOLS ENV_FILE CADDYFILE" >&2; exit 2; }
tmpl=$1 out=$2 wsa_host=$3 wsa_port=$4 doctools=$5 env_file=$6 caddyfile=$7
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../../../core/Caddy/ssrf-box.sh"

# Only the keys the lists read; anything inherited is dropped first.
for _k in $(env | sed -nE 's/^(HOST_IP|[A-Z0-9_]*_DOMAIN|DIFY_SSRF_[A-Z_]*)=.*/\1/p'); do
    unset "$_k"
done
if [ -f "$env_file" ]; then
    while IFS= read -r _line || [ -n "$_line" ]; do
        case "$_line" in
            HOST_IP=*|*_DOMAIN=*|DIFY_SSRF_ALLOW_RANGES=*|DIFY_SSRF_DENY_BOX_EXTRA=*|DIFY_SSRF_DENY_BOX_OVERRIDE=*|DIFY_SSRF_DENY_HOSTS=*) ;;
            *) continue ;;
        esac
        _k=${_line%%=*}
        case "$_k" in *[!A-Z0-9_]*|'') continue ;; esac
        _v=${_line#*=}
        case "$_v" in \"*\") _v=${_v#\"}; _v=${_v%\"} ;; \'*\') _v=${_v#\'}; _v=${_v%\'} ;; esac
        export "$_k=$_v"
    done < "$env_file"
fi

static=$(ssrf_static_denies "$caddyfile")
[ -n "$static" ] || { echo "render.sh: no deny range found in $caddyfile" >&2; exit 1; }
box=$(ssrf_box_list)
allow=$(ssrf_allow_ranges)

# DIFY_SSRF_DENY_HOSTS: names in squid's dstdomain form (`.x` = x and below),
# IPv4/IPv6 addresses and CIDRs as dst values; anything else is dropped (it
# could not be parsed by :8195 either). squid rejects a name that a `.x`
# entry already covers, so those are left out.
names="" nets=""
for _h in ${DIFY_SSRF_DENY_HOSTS:-}; do
    [ "$_h" = disabled.invalid ] && continue
    # an address has a colon (IPv6) or only digits, dots and a slash (IPv4)
    case "$_h" in
        *:*|*[!0-9./]*) case "$_h" in *:*) _isname=0 ;; *) _isname=1 ;; esac ;;
        *) _isname=0 ;;
    esac
    case "$_isname" in
        1)
            if printf '%s' "$_h" | grep -Eq '^(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'; then
                names="$names $(printf '%s' "$_h" | tr 'A-Z' 'a-z' | sed 's/^\*\././')"
            else
                ssrf_warn "DIFY_SSRF_DENY_HOSTS entry '$_h' is not a host name or address; not rendered"
            fi ;;
        *)
            _ip=${_h%/*}; _len=${_h#"$_ip"}; _len=${_len#/}
            case "$_ip" in
                *:*) _max=128; case "$_ip" in *[!0-9A-Fa-f:]*) _ip="" ;; esac ;;
                *) _max=32; _ssrf_ipv4_ok "$_ip" || _ip="" ;;
            esac
            case "$_len" in *[!0-9]*) _ip="" ;; esac
            if [ -n "$_ip" ] && { [ -z "$_len" ] || [ "$_len" -le "$_max" ]; }; then
                nets="$nets $_h"
            else
                ssrf_warn "DIFY_SSRF_DENY_HOSTS entry '$_h' is not a host name or address; not rendered"
            fi ;;
    esac
done
names=$(printf '%s\n' $names | awk 'NF' | sort -u | awk '
    { n[NR] = $0 }
    END {
        for (i = 1; i <= NR; i++) {
            keep = 1
            for (j = 1; j <= NR; j++) {
                if (i == j || substr(n[j], 1, 1) != ".") continue
                b = n[j]
                if (n[i] == substr(b, 2) || (length(n[i]) > length(b) && substr(n[i], length(n[i]) - length(b) + 1) == b)) keep = 0
            }
            if (keep) print n[i]
        }
    }' | tr '\n' ' ' | sed 's/ $//')
nets=$(printf '%s\n' $nets | awk 'NF' | sort -u | tr '\n' ' ' | sed 's/ $//')

blocks=$(mktemp -d)
trap 'rm -rf "$blocks"' EXIT
# squid accepts many values per acl line; eight per line keeps it readable
printf '%s\n' $static | awk '{ a[++n] = $0 } END { for (i = 1; i <= n; i += 8) {
    line = "acl to_internal dst"; for (j = i; j < i + 8 && j <= n; j++) line = line " " a[j]; print line } }' \
    > "$blocks/SSRF_ACLS"
[ -z "$box" ] || echo "acl to_box dst $box" >> "$blocks/SSRF_ACLS"
[ -z "$allow" ] || echo "acl ssrf_allow_ranges dst $allow" >> "$blocks/SSRF_ACLS"
[ -z "$names" ] || echo "acl ssrf_deny_names dstdomain $names" >> "$blocks/SSRF_ACLS"
[ -z "$nets" ] || echo "acl ssrf_deny_nets dst $nets" >> "$blocks/SSRF_ACLS"
: > "$blocks/SSRF_BOX_ACCESS"; : > "$blocks/SSRF_ALLOW_ACCESS"; : > "$blocks/SSRF_ALLOW_DIRECT"
: > "$blocks/SSRF_DENY_NAMES_ACCESS"
[ -z "$names" ] || echo "http_access deny  ssrf_deny_names   # DIFY_SSRF_DENY_HOSTS names (beat the doc-tools allow)" > "$blocks/SSRF_DENY_NAMES_ACCESS"
[ -z "$nets" ] || echo "http_access deny  ssrf_deny_nets    # DIFY_SSRF_DENY_HOSTS addresses" > "$blocks/SSRF_BOX_ACCESS"
[ -z "$box" ] || echo "http_access deny  to_box            # SSRF: the box's own addresses" >> "$blocks/SSRF_BOX_ACCESS"
if [ -n "$allow" ]; then
    echo "http_access allow ssrf_allow_ranges # operator-allowed CGNAT/benchmark ranges" > "$blocks/SSRF_ALLOW_ACCESS"
    echo "always_direct allow ssrf_allow_ranges" > "$blocks/SSRF_ALLOW_DIRECT"
fi

mkdir -p "$(dirname "$out")"
awk -v dir="$blocks" -v h="$wsa_host" -v p="$wsa_port" -v d="$doctools" '
    /^@@SSRF_[A-Z_]+@@$/ {
        f = dir "/" substr($0, 3, length($0) - 4)
        while ((getline l < f) > 0) print l
        close(f); next
    }
    { gsub(/@@WSA_HOST@@/, h); gsub(/@@WSA_PORT@@/, p); gsub(/@@DOCTOOLS_DOMAINS@@/, d); print }
' "$tmpl" > "$out"
if grep -q '@@' "$out"; then
    echo "render.sh: unrendered placeholder in $out" >&2; exit 1
fi
