# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# Where the Mac worker's own Ollama listens — sourced by the installer,
# bin/ollama-switch.sh, bin/ollama-port-migrate.sh and bin/agent-upgrade.sh.
# No top-level `set` (a library). POSIX awk only (macOS ships BSD awk).
#
#   rzfz_ollama_conf <ollama-port.conf> <KEY>       one port of the bundle's conf
#   rzfz_ollama_port <ollama-port.conf> <.env.node> the port THIS worker's Ollama
#        runs on: the RZFZ_OLLAMA_PORT the installer wrote into .env.node, or
#        the conf's legacy port for a worker installed before the port moved.
# Both print nothing and return 1 when the value is missing or not a port.
rzfz_ollama_conf() {
    local v
    v="$(awk -v key="$2" 'index($0, key "=") == 1 { v = substr($0, length(key) + 2) } END { print v }' "$1" 2>/dev/null || true)"
    rzfz_ollama_valid_port "$v" || return 1
    printf '%s\n' "$v"
}

rzfz_ollama_port() {
    local v=""
    if [ -f "$2" ]; then
        v="$(awk 'index($0, "RZFZ_OLLAMA_PORT=") == 1 { v = substr($0, 18) } END { print v }' "$2" 2>/dev/null || true)"
    fi
    if rzfz_ollama_valid_port "$v"; then
        printf '%s\n' "$v"
        return 0
    fi
    rzfz_ollama_conf "$1" RZFZ_OLLAMA_LEGACY_PORT
}

# 1024-65535, plain digits, no leading zero
rzfz_ollama_valid_port() {
    case "$1" in
        ''|*[!0-9]*|0*) return 1 ;;
    esac
    [ "${#1}" -le 5 ] && [ "$1" -ge 1024 ] && [ "$1" -le 65535 ]
}
