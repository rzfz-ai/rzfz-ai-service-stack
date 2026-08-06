#!/bin/sh
# rc6.7 #46 v3 — OpenHands sandbox URL monkey-patch.
#
# Upstream openhands hardcodes `host.docker.internal:<host_port>` in TWO places
# that the spawned sandbox container talks back through:
#   1. /app/openhands/app_server/sandbox/docker_sandbox_service.py
#      — `OH_WEBHOOKS_0_BASE_URL = http://host.docker.internal:<port>/api/v1/webhooks`
#   2. /app/openhands/app_server/app_conversation/live_status_app_conversation_service.py
#      — `web_url = f'http://host.docker.internal:<port>'` (used to build the
#        MCP server URL the sandbox reads via env)
#
# In our deploy:
#   - openhands lives on the compose `default` bridge AND the docker default
#     `bridge` (attached at runtime by razzfazz-init/upgrade — compose can't
#     manage the default bridge because aliases aren't supported there).
#   - sandbox containers always land on the docker default `bridge`.
#   - openhands publishes to host:OPENHANDS_PORT (default 3005) — but the
#     hardcoded URL points the sandbox at host:3000, where Gitea actually
#     lives on our box.
#
# This patch substitutes openhands' OWN docker-default-bridge IP into the two
# URL builders, so the sandbox can call openhands DIRECTLY across the bridge
# (no host loopback, no UFW rule, no port-bind dance, no Gitea collision).
#
# Idempotent: re-running the sed is a no-op once the substitution is applied.
# Watch on upstream bumps (see docs/upstream-monkey-patches.md).
set -e

# Resolve openhands' bridge IP. openhands is multi-homed; pick the one in
# 172.17.0.0/16 (docker default bridge subnet).
BRIDGE_IP=$(python3 - <<'PY'
import socket, ipaddress
target = ipaddress.ip_network('172.17.0.0/16')
try:
    for info in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
        ip = info[4][0]
        if ipaddress.ip_address(ip) in target:
            print(ip)
            break
except Exception:
    pass
PY
)

if [ -z "$BRIDGE_IP" ]; then
    echo "[rc6.7-monkey-patch] WARNING: openhands has no IP on 172.17.0.0/16; skipping URL substitution. Sandbox callbacks will likely fail."
    exit 0
fi

PORT="${OPENHANDS_INTERNAL_PORT:-3000}"
TARGET="http://${BRIDGE_IP}:${PORT}"

A=/app/openhands/app_server/sandbox/docker_sandbox_service.py
B=/app/openhands/app_server/app_conversation/live_status_app_conversation_service.py

# Replace host.docker.internal:{self.host_port} (or {sandbox_service.host_port})
# with the literal bridge IP + container port. The host_port placeholder is
# retained syntactically (still expanded by the f-string) but the result is
# discarded because we hard-pin the URL to the bridge target.
patch_one() {
    f="$1"
    if [ ! -f "$f" ]; then
        echo "[rc6.7-monkey-patch] $f not found; openhands image layout changed?"
        return 0
    fi
    # rc6.7 #43 v6: ALWAYS restore-then-patch. The previous "skip if file
    # already contains current BRIDGE_IP" guard left STALE IPs in place
    # when bridge IPs got reassigned across recreates (e.g. openhands
    # was 172.17.0.2 last run, gpustack got 172.17.0.2 this run, openhands
    # is now 172.17.0.3 → file still says 172.17.0.2 which is gpustack,
    # webhooks fail). Backup-on-first-run, restore-from-backup-then-sed
    # on every subsequent run guarantees we always patch with the current
    # IP no matter how many recreates have shuffled the addressing.
    if [ ! -f "${f}.razzfazz-orig" ]; then
        cp -p "$f" "${f}.razzfazz-orig"
    else
        cp -p "${f}.razzfazz-orig" "$f"
    fi
    # docker_sandbox_service.py: webhook URL
    sed -i "s|f'http://host\\.docker\\.internal:{self\\.host_port}/api/v1/webhooks'|f'${TARGET}/api/v1/webhooks'|g" "$f"
    # live_status_app_conversation_service.py: MCP base web URL
    sed -i "s|f'http://host\\.docker\\.internal:{sandbox_service\\.host_port}'|f'${TARGET}'|g" "$f"
}

patch_one "$A"
patch_one "$B"

echo "[rc6.7-monkey-patch] Substituted host.docker.internal → ${BRIDGE_IP}:${PORT} in webhook + MCP URLs"

# rc6.7 #91: extend replace_localhost_hostname_for_docker so the backend's
# OWN readiness probe can hit a sandbox whose public URL is the per-user
# `https://<type>-<slug>.agents.<domain>/sandbox/<port>/...` pattern. Without
# this, container_url_pattern's value is used verbatim for both browser-facing
# URLs AND the in-container probe — and inside agent-openhands, the public
# subdomain doesn't resolve, so the probe fails with "Name or service not
# known" and the conversation 500s with "Sandbox entered error state".
#
# Strategy: idempotently overwrite docker_utils.py with an extended version
# that recognizes the /sandbox/<port>(/...) path on ANY hostname and rewrites
# the URL to http://host.docker.internal:<port>/<rest>. Localhost path is
# preserved exactly as upstream.
DU=/app/openhands/app_server/utils/docker_utils.py
# #36: version-robust import for is_running_in_docker. OpenHands moved this
# helper across releases — 1.6.0 exposed it at `openhands.utils.environment`,
# 1.8.0 moved it to `openhands.app_server.utils.environment` (the old
# `openhands.utils` package no longer exists, so the previous hardcoded import
# raised ModuleNotFoundError at startup and CRASH-LOOPED the whole container).
# Probe the installed image for whichever module actually provides it and emit
# the matching import line, so the patch degrades across version bumps instead
# of bricking the agent. If neither is found, skip the docker_utils overwrite
# entirely — the /sandbox/<port> rewrite is an enhancement, NOT load-bearing for
# the backend to boot, so a future layout change stays non-fatal.
OH_ENV_IMPORT=$(python3 - <<'PY'
import importlib
for mod in ("openhands.app_server.utils.environment", "openhands.utils.environment"):
    try:
        m = importlib.import_module(mod)
        if hasattr(m, "is_running_in_docker"):
            print(f"from {mod} import is_running_in_docker")
            break
    except Exception:
        continue
PY
)
if [ -f "$DU" ] && [ -n "$OH_ENV_IMPORT" ]; then
    cat > "$DU" <<PYEOF
# razzfazz monkey-patch (rc6.7 #91): extend localhost rewriter to also handle
# the per-user agents-domain /sandbox/<port>/<rest> pattern, so the backend's
# in-container readiness probe doesn't try to resolve the public hostname.
import re
from urllib.parse import urlparse, urlunparse

${OH_ENV_IMPORT}

_SANDBOX_PATH_RE = re.compile(r'^/sandbox/(\\d+)(/.*)?\$')


def replace_localhost_hostname_for_docker(
    url: str, replacement: str = 'host.docker.internal'
) -> str:
    if not is_running_in_docker():
        return url
    parsed = urlparse(url)
    if parsed.hostname == 'localhost':
        netloc = parsed.netloc.replace('localhost', replacement, 1)
        return urlunparse(parsed._replace(netloc=netloc))
    m = _SANDBOX_PATH_RE.match(parsed.path or '')
    if m:
        port = m.group(1)
        # Default to empty (NOT '/') — callers append their own paths and a
        # trailing slash here produces double-slash URLs that 404 on
        # exact-match routes (e.g. health_check_path='/health' became
        # \`host.docker.internal:<port>//health\`).
        rest = m.group(2) or ''
        return f'http://{replacement}:{port}{rest}'
    return url
PYEOF
    echo "[rc6.7-monkey-patch] Patched $DU with /sandbox/<port> path rewrite ($OH_ENV_IMPORT)"
elif [ -f "$DU" ]; then
    echo "[rc6.7-monkey-patch] is_running_in_docker import not found in this OpenHands build; skipping docker_utils overwrite (non-fatal — backend still boots)."
else
    echo "[rc6.7-monkey-patch] $DU not found; openhands image layout changed?"
fi

# rc6.7 #43 v5: re-seed openhands settings.json with gpustack's CURRENT bridge
# IP. The settings file is written once at install time but bakes in a literal
# IP — bridge-network IPs get reassigned across recreates, so settings goes
# stale and the agent's first LLM call hangs in "Starting" with no endpoint.
# This re-seed runs every openhands start, so a recreate of EITHER openhands
# or gpustack self-heals on next openhands restart.
#
# Uses the docker socket (openhands has it mounted for sandbox spawning) via
# python docker SDK. Skips if gpustack is not on bridge (e.g. operator hasn't
# attached it yet). Idempotent — only writes if the resolved IP differs from
# what's already in settings.json.
# LLM_API_KEY is set by openhands compose env from ${GPUSTACK_API_KEY}.
GPU_API_KEY="${LLM_API_KEY:-${GPUSTACK_API_KEY:-}}"
if [ -z "$GPU_API_KEY" ] || [ "$GPU_API_KEY" = "openhands" ]; then
    echo "[rc6.7-monkey-patch] LLM_API_KEY/GPUSTACK_API_KEY not set in env; skipping settings re-seed."
    exit 0
fi

python3 - "$GPU_API_KEY" <<'PY' || true
import json, os, sys
api_key = sys.argv[1]
try:
    import docker
    client = docker.from_env()
    gpu = client.containers.get('gpustack')
    nets = gpu.attrs.get('NetworkSettings', {}).get('Networks', {})
    bridge = nets.get('bridge', {}).get('IPAddress', '')
except Exception as e:
    print(f"[rc6.7-monkey-patch] Could not resolve gpustack bridge IP via docker socket: {e!r}; skipping settings re-seed.", file=sys.stderr)
    sys.exit(0)
if not bridge:
    print("[rc6.7-monkey-patch] gpustack not attached to docker default bridge; skipping settings re-seed (run `docker network connect bridge gpustack`).", file=sys.stderr)
    sys.exit(0)

# rc6.7 #92: only update the HOST/IP portion of llm_base_url, preserving
# whatever path the operator picked (/v1 vs /v1-openai work identically
# on GPUStack but the operator may have chosen one deliberately). The
# original always-overwrite behavior wiped manual UI edits on every
# container restart. Only run on the first boot OR when the cached IP is
# stale (different from the current gpustack bridge IP).
# /v1-openai is GPUStack's OpenAI-compatible endpoint; /v1 is GPUStack's
# NATIVE protocol and is NOT compatible with OpenAI/litellm clients (it
# accepts requests but the response shape diverges, manifesting as empty
# `content` with `reasoning_content` populated, or tool-call confusion).
# OpenHands uses litellm which is OpenAI-compatible — must target /v1-openai.
default_path = '/v1-openai'
changed = False
import re as _re
from urllib.parse import urlparse, urlunparse

for path in ('/.openhands/settings.json', '/.openhands-state/settings.json'):
    if not os.path.exists(path):
        continue
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        print(f"[rc6.7-monkey-patch] {path}: parse failed ({e!r}); skipping.", file=sys.stderr)
        continue
    needs_write = False
    current = data.get('llm_base_url') or ''
    if not current:
        # First boot — fill with our default.
        data['llm_base_url'] = f"http://{bridge}:9090{default_path}"
        needs_write = True
    else:
        try:
            parsed = urlparse(current)
            host_in_url = parsed.hostname or ''
            # Only rewrite if the URL points at AN IP IN THE DOCKER BRIDGE
            # SUBNET (172.17.0.0/16) that's no longer gpustack — i.e. a
            # stale bridge IP from a prior recreate. Hostnames like
            # `gpustack`, `localhost`, or ANY non-172.17 IP are user-chosen
            # and left alone.
            looks_like_stale_bridge = bool(_re.match(r'^172\.17\.\d+\.\d+$', host_in_url) and host_in_url != bridge)
            if looks_like_stale_bridge:
                new_netloc = bridge + (f":{parsed.port}" if parsed.port else '')
                data['llm_base_url'] = urlunparse(parsed._replace(netloc=new_netloc))
                needs_write = True
        except Exception as e:
            print(f"[rc6.7-monkey-patch] could not parse llm_base_url={current!r}: {e}", file=sys.stderr)
    if not data.get('llm_api_key'):
        data['llm_api_key'] = api_key
        needs_write = True
    # Only fix the model if it's the broken upstream SaaS placeholder.
    if str(data.get('llm_model','')).startswith('openhands/'):
        data['llm_model'] = 'openai/qwen3-coder-next'
        needs_write = True
    if needs_write:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
        changed = True
        print(f"[rc6.7-monkey-patch] {path}: updated (llm_base_url={data.get('llm_base_url')})")

if not changed:
    print(f"[rc6.7-monkey-patch] settings.json already targets gpustack at {bridge}:9090; no change.")
PY
