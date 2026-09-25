#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz stop | start | restart | down — agent-safe stack lifecycle (#252)
# ==============================================================================
# Raw `docker compose down/up` ignores the socket-provisioned containers
# (per-user agents from agent-manager/mcp-manager, gpustack runners): a stop
# leaves them running against a dead stack, and `--remove-orphans` would
# DELETE them (see remove_compose_orphans_safe, #252 part 1). These verbs act
# on compose services AND `razzfazz.managed=true` containers together, and
# NEVER pass --remove-orphans or -v.
#
# Usage:
#   rzfz stop               stop managed agents, then the stack (containers kept)
#   rzfz start              start the stack, wait for the core, revive the
#                           agents that were running at the last `rzfz stop`
#   rzfz restart            stop + start
#   rzfz down               stop agents, then `docker compose down` (containers
#                           removed, volumes KEPT; agents only STOPPED)
#   rzfz stop|restart <svc…>  plain compose delegation for named services
#                             (agents untouched)
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"
# #2289: `rzfz start` redeploys the standard model set when no engine runs.
# lib-owui.sh defines _llm_manager_active (profile on AND manager up) — the
# predicate post-install and upgrade use; lib-llm-manager-deploy.sh is the
# deploy itself (also sourced by status.sh). Both need SCRIPT_DIR and .env.
# shellcheck source=scripts/lib-owui.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib-owui.sh"
# shellcheck source=cli/lib-llm-manager-deploy.sh disable=SC1091
source "${SCRIPT_DIR}/cli/lib-llm-manager-deploy.sh"
# #2289 (measured on 0.91): lifecycle.sh never loaded .env, so COMPOSE_PROFILES
# was unset in this shell, _llm_manager_profile_active was false on every box,
# and the hook below returned silently — `docker compose` reads .env from the
# project directory itself, so nothing else in this file noticed. lib.sh's
# load_env PRINTS pairs and exports nothing (project rule: never eval/source
# .env), so the one value the predicate needs is read explicitly; the deploy
# library reads its own values through read_env_value.
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"
if [ -z "${COMPOSE_PROFILES:-}" ] && [ -f "$ENV_FILE" ]; then
    COMPOSE_PROFILES="$(read_env_value "$ENV_FILE" COMPOSE_PROFILES 2>/dev/null || true)"
    export COMPOSE_PROFILES
fi

# --help before anything else — the command-reference generator captures this
for _a in "$@"; do case "$_a" in -h|--help)
    sed -n '4,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0 ;;
esac; done

VERB="${RZFZ_VERB:-${1:-}}"
# When invoked via rzfz, RZFZ_VERB carries the verb and $@ are extra args;
# when run directly (cli/lifecycle.sh stop), $1 is the verb.
if [ -z "${RZFZ_VERB:-}" ] && [ $# -gt 0 ]; then shift; fi

SNAPSHOT_DIR="${HOME}/.razzfazz"
SNAPSHOT="${SNAPSHOT_DIR}/lifecycle-agents.snapshot"

_managed_running() {
    docker ps --filter "label=razzfazz.managed=true" --format '{{.Names}}' 2>/dev/null
}

_managed_all() {
    docker ps -a --filter "label=razzfazz.managed=true" --format '{{.Names}}' 2>/dev/null
}

_stop_agents() {
    local names
    names=$(_managed_running)
    mkdir -p "$SNAPSHOT_DIR" 2>/dev/null || true
    # remember who was running so `start` revives exactly that set
    printf '%s\n' $names > "$SNAPSHOT" 2>/dev/null || true
    [ -n "$names" ] || { print_info "No managed agents running."; return 0; }
    print_step "Stopping $(printf '%s\n' $names | wc -l) managed agent container(s)..."
    # shellcheck disable=SC2086
    docker stop $names >/dev/null 2>&1 || true
}

_start_agents() {
    [ -s "$SNAPSHOT" ] || { print_info "No agent snapshot — agents stay as they are (start them from the Agent Portal)."; return 0; }
    local name started=0
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        if docker start "$name" >/dev/null 2>&1; then
            started=$((started+1))
        else
            print_warning "  agent '$name' could not be started (container gone? The Agent Portal's Start recreates it, #627)."
        fi
    done < "$SNAPSHOT"
    [ "$started" -gt 0 ] && print_success "Revived ${started} managed agent(s)."
    return 0
}

_wait_core() {
    # Non-fatal core wait (#153 lesson: one sick peripheral must not block):
    # postgres + caddy healthy within a bounded window.
    local waited=0
    print_substep "Waiting for the core (postgres, caddy) to be healthy (max 120s)..."
    while [ $waited -lt 120 ]; do
        local pg caddy
        pg=$(docker inspect -f '{{.State.Health.Status}}' postgres 2>/dev/null || echo missing)
        caddy=$(docker inspect -f '{{.State.Health.Status}}' caddy 2>/dev/null || echo missing)
        [ "$pg" = "healthy" ] && { [ "$caddy" = "healthy" ] || [ "$caddy" = "missing" ]; } && return 0
        sleep 5; waited=$((waited+5))
    done
    print_warning "Core not fully healthy after 120s — continuing (check 'rzfz status')."
    return 0
}

# #2289: after `rzfz down` + `rzfz start` a Manager box came back with NO LLM
# engines — the supervisor (llm-worker-agent) never relaunches or redeploys on
# its own (measured on 0.91: engine stopped, killed, recreated-after-down and
# REMOVED — no relaunch in 180–300 s with the supervisor healthy). Before #2226
# it also left four unstartable containers pinned to the removed project
# network. So `rzfz start` asks: manager profile active, supervisor up, zero
# engines running → deploy the standard set, exactly what post-install does.
# Idempotent: with engines running it does nothing.
_redeploy_engines_if_missing() {
    # every early return says WHY: a silent `|| return 0` hid a predicate that
    # could never be true at this call site (no .env loaded) on every box
    if ! _llm_manager_profile_active; then
        print_substep "LLM Manager profile not active (COMPOSE_PROFILES=${COMPOSE_PROFILES:-<unset>}) — no model redeploy (#2289)."
        return 0
    fi
    if ! _llm_manager_running; then
        print_warning "LLM Manager profile is active but the llm-manager container is not running — cannot redeploy models; check 'rzfz status' (#2289)."
        return 0
    fi
    local n
    n=$(docker ps -q --filter label=rzfz.role=llm-engine --filter label=razzfazz.managed=true 2>/dev/null | grep -c . || true)
    if [ "${n:-0}" -gt 0 ]; then
        print_substep "LLM engines running: ${n} — no redeploy needed (#2289)."
        return 0
    fi
    print_step "No LLM engine is running after start — redeploying the standard model set (#2289)..."
    llm_manager_deploy_standard_set "${PRESET:-standard}" \
        || print_warning "The model redeploy did not complete — run 'rzfz post-install --refresh' (#2289)."
}

case "$VERB" in
    stop)
        if [ $# -gt 0 ]; then exec docker compose stop "$@"; fi
        _stop_agents
        print_step "Stopping the stack (containers kept)..."
        docker compose stop
        # #2226: the supervised LLM engines live outside the compose project
        razzfazz_stop_supervised_engines || print_warning "some LLM engines are still running — see above (#2226)"
        print_success "Stack stopped. 'rzfz start' brings it back including the agents."
        print_info "Use 'rzfz start', not 'docker compose up -d': compose brings the containers back but not the LLM engines the worker launched, and no model is served until they are redeployed."
        ;;
    start)
        # review #674 nit: named services delegate like stop/restart do
        # (plain compose start — existing containers only, agents untouched)
        if [ $# -gt 0 ]; then exec docker compose start "$@"; fi
        print_step "Starting the stack..."
        # up (not start): recreates on config change; NO --remove-orphans (#252).
        docker compose up -d
        remove_compose_orphans_safe || true
        _wait_core
        _start_agents
        _redeploy_engines_if_missing
        print_success "Stack started."
        ;;
    restart)
        if [ $# -gt 0 ]; then exec docker compose restart "$@"; fi
        # RZFZ_VERB is inherited from the rzfz wrapper — pin it per child or
        # the children would loop on "restart" forever.
        RZFZ_VERB=stop "$0"
        RZFZ_VERB=start exec "$0"
        ;;
    down)
        _stop_agents
        # #2226: engines first — they hold the project network `compose down` removes
        razzfazz_stop_supervised_engines --remove || print_warning "some LLM engines are still running — see above (#2226)"
        print_step "docker compose down (volumes KEPT; agents only stopped, never removed)..."
        docker compose down
        print_success "Stack down. 'rzfz start' recreates the services and revives the agents."
        ;;
    *)
        echo "Usage: rzfz stop|start|restart|down [service...]" >&2
        exit 2
        ;;
esac
