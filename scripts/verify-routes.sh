#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# verify-routes.sh — Caddy Route Health Verification
# ==============================================================================
# Checks that every Caddy-routed subdomain is reachable and its upstream
# container is running. Useful during upgrade verification and as a standalone
# health check.
#
# Usage:
#   ./scripts/verify-routes.sh              # Check all enabled routes
#   ./scripts/verify-routes.sh --all        # Check all routes (including disabled profiles)
#   ./scripts/verify-routes.sh --json       # Output as JSON
#
# Environment:
#   Reads COMPOSE_PROFILES and MAIN_DOMAIN from .env in the script's parent dir.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${STACK_DIR}/.env"

# ---------- CLI flags ----------
CHECK_ALL=false
JSON_OUTPUT=false
for arg in "$@"; do
    case "$arg" in
        --all)  CHECK_ALL=true ;;
        --json) JSON_OUTPUT=true ;;
        --help|-h)
            echo "Usage: $0 [--all] [--json]"
            echo "  --all   Check all routes, not just enabled profiles"
            echo "  --json  Output results as JSON array"
            exit 0
            ;;
    esac
done

# ---------- Load .env ----------
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found. Run from the stack root or set STACK_DIR." >&2
    exit 1
fi

# Source only the variables we need (avoid executing dangerous expansions)
get_env() {
    local key="$1"
    grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | sed 's/^["'\'']//;s/["'\'']$//'
}

MAIN_DOMAIN="$(get_env MAIN_DOMAIN)"
COMPOSE_PROFILES="$(get_env COMPOSE_PROFILES)"

if [ -z "$MAIN_DOMAIN" ]; then
    echo "ERROR: MAIN_DOMAIN not set in $ENV_FILE" >&2
    exit 1
fi

# ---------- Route table ----------
# Format: profile|subdomain|container|port|health_path
# profile=core means always-on (no profile gate)
# health_path is the path Caddy exposes as /healthz (or the native health endpoint)
ROUTES=(
    "dify|dify.${MAIN_DOMAIN}|dify-api|5001|/healthz"
    "dify|dify.${MAIN_DOMAIN}|dify-web|3000|"
    "chat|chat.${MAIN_DOMAIN}|openwebui|8080|/healthz"
    # #1447 part c (cutover C7): the gate used to read
    # `llm-cpu,llm-box,llm-experimental` — three RETIRED profile names. A
    # current box runs `llm-legacy`, so the gate never matched and this
    # route was silently never checked. `--all` hid it further: there the
    # gate is skipped entirely, so the line looked alive whenever anyone
    # tested the checker itself.
    "llm-legacy|gpustack.${MAIN_DOMAIN}|gpustack|9090|/healthz"
    # #1444 (cutover C4): the manager took over the customer-facing name.
    # BOTH vHosts are checked because they are not interchangeable —
    # `llm.<domain>` is what clients are told to use, `llm-manager.<domain>`
    # is the console, and a Caddy block can serve one and 404 the other.
    # Like the gpustack line above, the names are BUILT from MAIN_DOMAIN:
    # an operator who overrides LLM_DOMAIN/GPUSTACK_DOMAIN is not covered
    # (get_env does not expand `${MAIN_DOMAIN}` out of the .env value).
    "llm-manager|llm.${MAIN_DOMAIN}|llm-manager|8080|/healthz"
    "llm-manager|llm-manager.${MAIN_DOMAIN}|llm-manager|8080|/healthz"
    "monitor|admin.${MAIN_DOMAIN}|komodo-core|8180|/healthz"
    "core|auth.${MAIN_DOMAIN}|authentik-server|9000|/healthz"
    "core|backup.${MAIN_DOMAIN}|razzfazz-backup-management|5000|/healthz"
    "core|license.${MAIN_DOMAIN}|razzfazz-licenses|5000|/healthz"
    "core|help.${MAIN_DOMAIN}|razzfazz-help|5000|/healthz"
    "gitea|git.${MAIN_DOMAIN}|gitea|3000|/healthz"
    "cognee|cognee.${MAIN_DOMAIN}|cognee|8000|/healthz"
    "lightrag|rag.${MAIN_DOMAIN}|lightrag|9621|/healthz"
    "docling|docling.${MAIN_DOMAIN}|docling|5001|/healthz"
    "stirling-pdf|pdf.${MAIN_DOMAIN}|stirling-pdf|8080|/healthz"
    "paperclip|paperclip.${MAIN_DOMAIN}|paperclip|3100|/api/health"
    "matrix|matrix.${MAIN_DOMAIN}|synapse|8008|"
    "matrix|element.${MAIN_DOMAIN}|element-web|80|"
    "paperless-ngx|paperless.${MAIN_DOMAIN}|paperless-ngx|8000|"
    "vaultwarden|vault.${MAIN_DOMAIN}|vaultwarden|80|"
    "infisical|infisical.${MAIN_DOMAIN}|infisical|8080|/api/status"
    "onyx|onyx.${MAIN_DOMAIN}|onyx-web|3000|"
    "onyx|onyx.${MAIN_DOMAIN}|onyx-api|8080|"
    "openhands|openhands.${MAIN_DOMAIN}|openhands|3000|/api/options"
    # #1447 part c: the three rows that used to stand here — moltis., hermes.
    # and coding. — checked vHosts that no longer exist. M020 S07 removed the
    # global site blocks; agents live at per-user subdomains
    # (`coding-tools-<slug>.${AGENTS_DOMAIN}`, registered dynamically) behind
    # the dashboard below, and the containers they named
    # (`moltis`/`hermes-agent`/`coding-tools`) are gone too — only the
    # *-image-builder one-shots remain. Their profiles had been retired as
    # well, so the gate never matched and nothing said so: FOUR vHosts, this
    # one and the LLM row above, stopped being checked in silence.
    #
    # The per-user subdomains cannot go in a static table. The dashboard is the
    # one fixed name, and it is what an operator checks first.
    "agents|agents.${MAIN_DOMAIN}|agent-manager|5000|/healthz"
    "core|${MAIN_DOMAIN}|caddy|-|"
)

# ---------- Profile check ----------
is_profile_enabled() {
    local route_profiles="$1"
    if [ "$route_profiles" = "core" ]; then
        return 0
    fi
    if $CHECK_ALL; then
        return 0
    fi
    # Check if any of the route's profiles are in COMPOSE_PROFILES
    IFS=',' read -ra rp <<< "$route_profiles"
    IFS=',' read -ra ep <<< "$COMPOSE_PROFILES"
    for r in "${rp[@]}"; do
        for e in "${ep[@]}"; do
            if [ "$r" = "$e" ]; then
                return 0
            fi
        done
    done
    return 1
}

# ---------- Container status ----------
is_container_running() {
    local name="$1"
    if [ "$name" = "caddy" ] || [ "$name" = "-" ]; then
        # Caddy is always running if stack is up
        docker inspect --format='{{.State.Running}}' caddy 2>/dev/null | grep -q true
        return $?
    fi
    docker inspect --format='{{.State.Running}}' "$name" 2>/dev/null | grep -q true
}

# ---------- Health check ----------
check_health() {
    local subdomain="$1"
    local health_path="$2"
    if [ -z "$health_path" ]; then
        # No health endpoint — just check if the subdomain responds
        local code
        code=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 \
            "https://${subdomain}/" 2>/dev/null) || code="000"
        echo "$code"
        return
    fi
    local code
    code=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 \
        "https://${subdomain}${health_path}" 2>/dev/null) || code="000"
    echo "$code"
}

# ---------- DNS resolution check ----------
can_resolve() {
    local subdomain="$1"
    # Try getent first (works with /etc/hosts), fall back to nslookup
    getent hosts "$subdomain" >/dev/null 2>&1 || nslookup "$subdomain" >/dev/null 2>&1
}

# ---------- Main ----------
# Table header
RESULTS=()
PASS=0
FAIL=0
SKIP=0

if ! $JSON_OUTPUT; then
    printf "%-30s %-28s %-6s %-10s %-12s %s\n" \
        "SUBDOMAIN" "CONTAINER" "PORT" "RUNNING" "HTTP STATUS" "RESULT"
    printf "%s\n" "$(printf '%.0s-' {1..110})"
fi

# Deduplicate by subdomain+container (some routes have multiple containers)
declare -A SEEN
for entry in "${ROUTES[@]}"; do
    IFS='|' read -r profile subdomain container port health_path <<< "$entry"

    # Skip if profile not enabled
    if ! is_profile_enabled "$profile"; then
        continue
    fi

    # Skip duplicates
    local_key="${subdomain}|${container}"
    if [ -n "${SEEN[$local_key]+x}" ]; then
        continue
    fi
    SEEN[$local_key]=1

    # Check container status
    if [ "$container" = "-" ] || [ "$container" = "caddy" ]; then
        running="n/a"
        result="SKIP"
        http_status="-"
        SKIP=$((SKIP + 1))
    elif is_container_running "$container"; then
        running="yes"
        # Check health endpoint
        if [ -n "$health_path" ]; then
            http_status=$(check_health "$subdomain" "$health_path")
        else
            http_status=$(check_health "$subdomain" "")
        fi
        if [ "$http_status" = "200" ] || [ "$http_status" = "302" ] || [ "$http_status" = "301" ]; then
            result="OK"
            PASS=$((PASS + 1))
        elif [ "$http_status" = "000" ]; then
            # Can't connect — check DNS
            if can_resolve "$subdomain"; then
                result="UNREACHABLE"
            else
                result="NO DNS"
            fi
            FAIL=$((FAIL + 1))
        else
            result="HTTP ${http_status}"
            FAIL=$((FAIL + 1))
        fi
    else
        running="no"
        http_status="-"
        result="DOWN"
        FAIL=$((FAIL + 1))
    fi

    if $JSON_OUTPUT; then
        local json_port="${port}"
        if [ "$json_port" = "-" ]; then json_port="null"; fi
        RESULTS+=("{\"subdomain\":\"${subdomain}\",\"container\":\"${container}\",\"port\":${json_port},\"running\":\"${running}\",\"http_status\":\"${http_status}\",\"result\":\"${result}\"}")
    else
        printf "%-30s %-28s %-6s %-10s %-12s %s\n" \
            "$subdomain" "$container" "$port" "$running" "$http_status" "$result"
    fi
done

if $JSON_OUTPUT; then
    echo "["
    for i in "${!RESULTS[@]}"; do
        if [ "$i" -lt $(( ${#RESULTS[@]} - 1 )) ]; then
            echo "  ${RESULTS[$i]},"
        else
            echo "  ${RESULTS[$i]}"
        fi
    done
    echo "]"
else
    echo ""
    echo "Summary: ${PASS} OK, ${FAIL} FAILED, ${SKIP} SKIPPED"
    if [ "$FAIL" -gt 0 ]; then
        exit 1
    fi
fi
