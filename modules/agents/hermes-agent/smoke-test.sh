#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# Box smoke test for the per-user Hermes agent image (#3076).
#
# Run on a box after the image is built (`rzfz upgrade`, or
# `docker compose build hermes-agent-image`), from the stack directory:
#
#   bash modules/agents/hermes-agent/smoke-test.sh
#
# It checks the image the stack pins (HERMES_AGENT_VERSION from .env), starts
# nothing that stays running, and changes no box state. What it proves:
#   1. the image exists under the pinned tag;
#   2. the installed Hermes is the version that tag ships;
#   3. the extras the Dockerfile installs import (providers, messaging, Matrix,
#      the web server), under the image's own user on a read-only root
#      filesystem, as the Agent Manager runs it;
#   4. the built dashboard and the terminal UI are in the image.
# The end-to-end check (start a Hermes agent from My Agents, open it, send one
# message to a local model) is the manual step after this script.
set -euo pipefail

cd "$(dirname "$0")/../../.."
ENV_FILE=${ENV_FILE:-.env}

pin=$(sed -n 's/^HERMES_AGENT_VERSION=//p' "$ENV_FILE" | tail -n 1)
pin=${pin:-$(sed -n 's/^HERMES_AGENT_VERSION=//p' config/.env.example | tail -n 1)}
image="razzfazz-stack-hermes-agent:${pin}"
fail=0
ok()  { printf '  [ok]   %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; fail=1; }

echo "Hermes smoke test: ${image}"

if ! docker image inspect "$image" >/dev/null 2>&1; then
    bad "image ${image} is not on this box (build it first)"
    exit 1
fi
ok "image present"

run_py() {
    docker run --rm --read-only --tmpfs /tmp --tmpfs /opt/data:uid=10000,gid=10000 \
        --user 10000:10000 --network none --entrypoint /opt/hermes/.venv/bin/python \
        "$image" -c "$1"
}

# Upstream tags map to Hermes releases (v2026.9.24 == 0.21.5); the installed
# distribution says which one the image really carries.
expected=${HERMES_EXPECTED_VERSION:-0.21.5}
got=$(run_py 'from importlib.metadata import version; print(version("hermes-agent"))' 2>/dev/null || true)
if [ "$got" = "$expected" ]; then ok "hermes-agent ${got}"; else bad "hermes-agent is '${got}', expected ${expected}"; fi

if out=$(run_py '
import importlib
mods = ["hermes_cli", "anthropic", "boto3", "azure.identity", "mautrix", "telegram", "slack_bolt", "fastapi", "uvicorn"]
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        missing.append(f"{m}: {type(e).__name__}: {e}")
print("\n".join(missing))
' 2>&1); then
    if [ -z "$out" ]; then ok "extras import (providers, messaging, matrix, web)"; else bad "imports failed:"; printf '%s\n' "$out" | sed 's/^/         /'; fi
else
    bad "python did not run in the image: ${out}"
fi

if docker run --rm --read-only --user 10000:10000 --network none --entrypoint /bin/sh "$image" \
        -c 'test -f /opt/hermes/hermes_cli/web_dist/index.html && test -f /opt/hermes/ui-tui/dist/entry.js' 2>/dev/null; then
    ok "dashboard and terminal UI built"
else
    bad "dashboard (hermes_cli/web_dist/index.html) or terminal UI (ui-tui/dist/entry.js) missing"
fi

if [ "$fail" -eq 0 ]; then
    echo "RESULT: PASS"
else
    echo "RESULT: FAIL"
    exit 1
fi
