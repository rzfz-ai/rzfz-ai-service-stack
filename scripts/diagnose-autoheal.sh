#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# #2383 — read-only capture of an autoheal restart loop. Changes nothing.
#   scripts/diagnose-autoheal.sh > autoheal-$(hostname)-$(date -u +%Y%m%dT%H%M%SZ).txt
#
# Only `docker inspect`, `docker logs`, `docker ps` and `docker events` are used
# (the last bounded with --since/--until), each under `timeout`. The container's
# environment is NEVER printed wholesale: it can hold secrets, so only the names
# on the allowlist in filter_env pass.
set -euo pipefail

NAME="${AUTOHEAL_CONTAINER:-autoheal}"
BOUND="${DIAG_TIMEOUT:-15}"

d() { timeout "$BOUND" docker "$@" 2>&1 || true; }

# stdin: KEY=VALUE lines. stdout: only the non-secret autoheal wiring keys.
filter_env() {
    grep -E '^(DOCKER_SOCK|DOCKER_HOST|AUTOHEAL_[A-Z_]+)=' || true
}

main() {
    echo "== mount (raw socket or proxy?)"
    d inspect "$NAME" --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{.Mode}}){{"\n"}}{{end}}'
    echo "== wiring env (allowlisted names only)"
    d inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' | filter_env
    echo "== state"
    d inspect "$NAME" --format 'status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} restarts={{.RestartCount}} started={{.State.StartedAt}} finished={{.State.FinishedAt}}'
    echo "== healthcheck (image and container)"
    d inspect "$NAME" --format '{{json .Config.Healthcheck}}'
    d inspect "$NAME" --format '{{json .State.Health}}'
    echo "== compose labels (project, service, config files)"
    d inspect "$NAME" --format '{{index .Config.Labels "com.docker.compose.project"}} {{index .Config.Labels "com.docker.compose.service"}} {{index .Config.Labels "com.docker.compose.project.config_files"}}'
    echo "== labelled containers it would supervise"
    d ps -a --filter label=autoheal=true --format '{{.Names}}|{{.Status}}'
    echo "== last 30 min of events (who sent the kill?)"
    timeout "$BOUND" docker events --since 30m --until "$(date +%s)" --filter "container=${NAME}" \
        --format '{{.Time}} {{.Action}} {{json .Actor.Attributes}}' 2>&1 | tail -n 60 || true
    echo "== last 80 log lines"
    d logs --tail 80 "$NAME"
}

# Sourceable for tests: only run when executed.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main
fi
