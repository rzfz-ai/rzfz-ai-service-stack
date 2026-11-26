# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# shellcheck shell=bash
#
# #2719: which RUNNING containers of this compose project still carry a domain
# the box no longer has. Sourced library (no top-level `set`): used by
# scripts/redomain-reconcile.sh and by `rzfz status`.
#
# A container's environment is fixed when the container is created; a restart
# or a reboot never refreshes it. So after a re-domain every container that was
# not recreated keeps the old MAIN_DOMAIN / *_DOMAIN values (on a delivered box:
# 20 containers, the start portal's tiles pointing at the old domain). This is
# measured here, never assumed from a list of service names. The host rule is
# the re-domain's own (scripts/lib/redomain-hosts.awk, reused by
# scripts/lib/domain-env-drift.awk).
#
#   razzfazz_domain_env_drift <env-file> <old-domain|""> <compose-project-dir> [<new-domain>]
#
# prints one line per stale value:  <service> <container> <KEY>
# and, when more containers run than it measures, a first line
#   "# measured <N> of <M>"
# Only containers whose compose working_dir label is <compose-project-dir>
# count (per-user agent containers and other projects are never touched).
# Read-only and bounded: every docker call runs under `timeout`, at most
# RAZZFAZZ_DRIFT_MAX_CONTAINERS (default 200) containers in ONE inspect call.
# Returns 0 measured (no output = nothing stale) · 2 NOT measured (docker did
# not answer, no container of the project matched, or the judge failed) ·
# 3 measured only partly (more containers than the cap; the lines are still
# printed).

# Services that are never recreated to follow a domain (they do not route by
# it, and recreating them interrupts the box): the shared datastores, the LLM
# runtime, the backup services, the socket proxy and the config UI itself
# (it runs the apply). The same exclusions as ApplyManager._DOMAIN_BAKING_SERVICES
# (core/config/app/services/apply_manager.py) states in its comment. A stale
# value in one of them is reported as a WARN, never as a FAIL with a
# recreate command.
RAZZFAZZ_DOMAIN_NO_RECREATE_RE='^(postgres.*|valkey.*|ferretdb.*|clickhouse.*|onyx-vespa|vespa.*|mongo.*|redis.*|gpustack.*|model-sync.*|llm-manager-router|llm-worker-agent|llm-engine.*|llm-registry|llm-mac-gateway|ollama-proxy|backup|backup-.*|razzfazz-backup-management|razzfazz-config|docker-socket-proxy|.*-init|wazuh-indexer)$'

# Not recreated by the re-domain either, but a stale value there is a FAIL that
# names the exact command, never a warning: the LLM worker agent runs engines,
# so the operator drains them first. (llm-manager restarts in seconds and is
# recreated like any other service.)
RAZZFAZZ_DOMAIN_MANUAL_RE='^(llm-worker-agent)$'

# The one protected service that a CLI-run re-domain DOES recreate, last: the
# config UI carries MAIN_DOMAIN (and the other .env keys) in its own compose
# environment, and keeps the old values until it is recreated. It is never
# recreated by a run that the config UI itself performs (--db-only): it would
# kill the apply it is running.
RAZZFAZZ_DOMAIN_LAST_RE='^(razzfazz-config)$'

razzfazz_domain_env_last() {        # <service>: 0 when a CLI re-domain recreates it last
    [[ "$1" =~ $RAZZFAZZ_DOMAIN_LAST_RE ]]
}

razzfazz_domain_env_protected() {   # <service>: 0 when it is never auto-recreated
    [[ "$1" =~ $RAZZFAZZ_DOMAIN_NO_RECREATE_RE ]]
}

razzfazz_domain_env_manual() {      # <service>: 0 when a stale value needs a manual recreate
    [[ "$1" =~ $RAZZFAZZ_DOMAIN_MANUAL_RE ]]
}

razzfazz_domain_env_drift() {
    # The inspect output holds every container's secrets: never trace it.
    local -
    set +x
    set -o pipefail
    local env_file="$1" old="$2" proj="$3" new="${4:-}"
    local t="${RAZZFAZZ_DRIFT_TIMEOUT_S:-20}" max="${RAZZFAZZ_DRIFT_MAX_CONTAINERS:-200}"
    local lib_dir ids total out res
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -r "$env_file" ] || return 2
    command -v docker >/dev/null 2>&1 || return 2
    # No pipe on the docker call: `| head` would report head's status and turn
    # a docker that timed out into "no containers", i.e. a silent "nothing stale".
    ids="$(timeout "$t" docker ps -q --filter "label=com.docker.compose.project.working_dir=${proj}" 2>/dev/null)" || return 2
    # Every caller runs with the stack up; no container of the project is a
    # wrong project dir or a docker that lies, never "nothing stale".
    [ -n "$ids" ] || return 2
    total="$(printf '%s\n' "$ids" | grep -c .)"
    ids="$(printf '%s\n' "$ids" | head -n "$max")"
    # shellcheck disable=SC2086
    out="$(timeout "$t" docker inspect --format '@@{{.Name}}|{{index .Config.Labels "com.docker.compose.service"}}{{range .Config.Env}}{{"\n"}}{{.}}{{end}}' $ids 2>/dev/null)" || return 2
    res="$(printf '%s\n' "$out" | OLD="$(printf '%s' "$old" | tr '[:upper:]' '[:lower:]')" \
        NEW="$(printf '%s' "$new" | tr '[:upper:]' '[:lower:]')" \
        awk -v mode=drift -v envfile="$env_file" \
            -f "$lib_dir/redomain-hosts.awk" -f "$lib_dir/domain-env-drift.awk")" || return 2
    if [ "$total" -gt "$max" ]; then
        printf '# measured %s of %s\n' "$max" "$total"
        [ -n "$res" ] && printf '%s\n' "$res"
        return 3
    fi
    [ -n "$res" ] && printf '%s\n' "$res"
    return 0
}

# <lines> recreate|protected|manual|last|envfile: the stale lines of that class (the
# "# measured" line dropped). envfile: the value the env file itself holds.
razzfazz_domain_env_drift_filter() {
    local line svc
    while IFS= read -r line; do
        case "$line" in ''|'#'*) continue ;; esac
        svc="${line%% *}"
        if [ "${line##* }" = env ]; then
            [ "$2" = envfile ] && printf '%s\n' "$line"
        elif razzfazz_domain_env_manual "$svc"; then
            [ "$2" = manual ] && printf '%s\n' "$line"
        elif razzfazz_domain_env_protected "$svc"; then
            [ "$2" = protected ] && printf '%s\n' "$line"
            if [ "$2" = last ] && razzfazz_domain_env_last "$svc"; then printf '%s\n' "$line"; fi
        else
            [ "$2" = recreate ] && printf '%s\n' "$line"
        fi
    done <<< "$1"
    return 0
}

# One line for an operator: "<containers> | <recreate command>" for the lines
# given (callers pass the "recreate" class only), or nothing.
razzfazz_domain_env_drift_summary() {
    local lines="$1" svcs ctrs
    [ -n "$lines" ] || return 0
    svcs="$(printf '%s\n' "$lines" | awk 'NF && $1 !~ /^#/{print $1}' | sort -u | tr '\n' ' ')"
    ctrs="$(printf '%s\n' "$lines" | awk 'NF && $1 !~ /^#/{print $2}' | sort -u | tr '\n' ' ')"
    printf '%s|docker compose up -d --no-deps --force-recreate %s\n' "${ctrs% }" "${svcs% }"
}

# The container names of <lines>, space-separated.
razzfazz_domain_env_drift_names() {
    printf '%s\n' "$1" | awk 'NF && $1 !~ /^#/{print $2}' | sort -u | tr '\n' ' ' | sed 's/ $//'
}
