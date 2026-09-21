#!/bin/sh
# Shared entrypoint for the razzfazz.ai coding-agent split images (#36).
#
# One entrypoint serves every per-type image (opencode / gsd-pi / codex /
# user-defined). Behaviour is driven by $AGENT_KIND. Responsibilities:
#   1. git identity + Gitea credential helper
#   2. per-agent model config (gsd models.json / opencode config.json /
#      codex config.toml with wire_api="responses" via a local Responses→Chat
#      shim) + start the codex shim for the codex image
#   3. start the tmux server so tmux-continuum AUTO-RESTORES the user's
#      sessions after a container restart (Phase-1 persistence)
#   4. start the shared Flask web terminal
#
# The container then stays alive on `tail -f`. All user state (repos, installed
# tools, config, tmux resurrect saves) lives under the persistent per-user
# volumes the provisioner mounts (/workspace + /home/agent), so a restart is
# non-destructive.

set -e

HOME_DIR="${HOME:-/home/agent}"
export HOME="${HOME_DIR}"
AGENT_KIND="${AGENT_KIND:-coding-tools}"
# The per-session model/auth picker is offered for the coding agents, but NOT
# for user-defined (a bare terminal — bring your own agent, no model config).
if [ "${AGENT_KIND}" = "user-defined" ]; then
    export AGENT_MODEL_CONFIG=0
else
    export AGENT_MODEL_CONFIG="${AGENT_MODEL_CONFIG:-1}"
fi
GPUSTACK_KEY="${GPUSTACK_API_KEY:-}"
BASE_URL="${OPENAI_BASE_URL:-http://llm:8080/v1}"
LLM_MODEL_VAL="${LLM_MODEL:-qwen3.6}"

# ── bring-your-own-tool install prefixes (#36 / PR #84) ──────────────────────
# The sandbox root fs is READ-ONLY; only /home/agent + /workspace are writable
# and persistent. npm's default global prefix is /usr/local (read-only), so
# `npm install -g <agent>` — the User Defined Agent's whole premise — would fail
# with EROFS. The image bakes NPM_CONFIG_PREFIX / PIP_USER / CARGO_HOME + PATH
# (see Dockerfile) so the tmux server (started below) and its shells inherit
# them. Here we (a) ensure the writable prefix dirs exist even on EXISTING
# per-user volumes that pre-date this fix (a populated named volume shadows the
# image's baked /home/agent), and (b) seed a ~/.bashrc snippet so interactive
# login shells the user actually types into get the env+PATH regardless. Both
# land on the persistent /home/agent volume, so a user-installed tool survives a
# container restart. We never touch /usr/local (stays read-only).
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-${HOME_DIR}/.npm-global}"
export PIP_USER="${PIP_USER:-1}"
# Debian bookworm's system python is PEP-668 externally-managed, so a bare
# `pip install --user` errors out. The sandbox python is a throwaway inside a
# read-only-root container (pip can only ever write to the persistent ~/.local),
# so allow user installs out of the box for the bring-your-own-tool flow.
export PIP_BREAK_SYSTEM_PACKAGES="${PIP_BREAK_SYSTEM_PACKAGES:-1}"
export CARGO_HOME="${CARGO_HOME:-${HOME_DIR}/.cargo}"
export PATH="${NPM_CONFIG_PREFIX}/bin:${HOME_DIR}/.local/bin:${CARGO_HOME}/bin:${PATH}"
mkdir -p "${NPM_CONFIG_PREFIX}/bin" "${HOME_DIR}/.local/bin" "${CARGO_HOME}/bin" 2>/dev/null || true
# Persist the env for the shells the user actually types into. We write a single
# self-contained env file on the persistent volume and source it from BOTH
# ~/.bash_profile (login shells) AND ~/.bashrc (interactive non-login shells) —
# the distinction matters because Debian's default ~/.bashrc has a top-of-file
# `case $- in *i*) ;; *) return;; esac` guard that returns before any appended
# snippet in a NON-interactive login shell, so ~/.bashrc alone is not enough.
# Sourcing the standalone file from ~/.bash_profile (which has no such guard)
# covers every flavour. Rewritten every boot so a bumped image updates existing
# volumes.
ENV_FILE="${HOME_DIR}/.razzfazz-agent-env.sh"
if [ -w "${HOME_DIR}" ]; then
    cat > "${ENV_FILE}" << EOF
# razzfazz.ai coding-agent install prefixes (managed — regenerated every boot).
# Writable+persistent package-manager prefixes under /home/agent (the root fs is
# read-only). npm install -g / pip install --user / cargo install all land here
# and PERSIST across a container restart (per-user volume). Do not edit.
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX}"
export PIP_USER="${PIP_USER}"
export PIP_BREAK_SYSTEM_PACKAGES="${PIP_BREAK_SYSTEM_PACKAGES}"
export CARGO_HOME="${CARGO_HOME}"
# #556: DeepSeek Harness has NO documented DSH_HOME default (developer
# preview) — pin it so the seeded settings.yaml below is authoritative.
export DSH_HOME="\$HOME/.dsh"
case ":\$PATH:" in
  *":\$NPM_CONFIG_PREFIX/bin:"*) : ;;
  *) export PATH="\$NPM_CONFIG_PREFIX/bin:\$HOME/.local/bin:\$CARGO_HOME/bin:\$PATH" ;;
esac
EOF
    # Source the env file from ~/.bash_profile (login) and ~/.bashrc (interactive).
    for RC in "${HOME_DIR}/.bash_profile" "${HOME_DIR}/.bashrc"; do
        touch "${RC}" 2>/dev/null || true
        if ! grep -q 'razzfazz-agent-env.sh' "${RC}" 2>/dev/null; then
            {
                echo '# razzfazz coding-agent install prefixes (managed)'
                echo '[ -f "$HOME/.razzfazz-agent-env.sh" ] && . "$HOME/.razzfazz-agent-env.sh"'
            } >> "${RC}" 2>/dev/null || true
        fi
    done
    # ~/.bash_profile must also load ~/.bashrc so interactive login shells get the
    # user's normal bashrc customisations (Debian doesn't do this by default).
    if ! grep -q 'razzfazz coding-agent bashrc source' "${HOME_DIR}/.bash_profile" 2>/dev/null; then
        {
            echo '# razzfazz coding-agent bashrc source (managed)'
            echo '[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"'
        } >> "${HOME_DIR}/.bash_profile" 2>/dev/null || true
    fi
fi

# ── Port-preview MOTD hint (#166) ─────────────────────────────────────────────
# A dev server you start here (e.g. `npm run dev`) is previewable in the browser
# via the per-instance "🔌 Ports" chip / the /__port/<port>/ path — BUT only if
# it binds 0.0.0.0: the preview proxy dials this container BY NAME, so a
# 127.0.0.1/localhost-only server is unreachable. The web UI surfaces the link;
# this MOTD teaches the 0.0.0.0 requirement in the terminal itself. Printed once
# per interactive shell (guarded), from the persistent home volume.
if [ -w "${HOME_DIR}" ]; then
    MOTD_FILE="${HOME_DIR}/.razzfazz-ports-motd.txt"
    cat > "${MOTD_FILE}" << 'EOF'

  ── Preview a web server you run here ──────────────────────────────
   Bind it to 0.0.0.0 (NOT 127.0.0.1 / localhost), e.g.
     npm run dev -- --host 0.0.0.0     python3 -m http.server -b 0.0.0.0 8000
   Then open it from the "🔌 Ports" chip at the top of this page, or
   browse to   <this-agent-URL>/__port/<PORT>/
   (A 127.0.0.1-only server is NOT reachable from your browser.)
  ───────────────────────────────────────────────────────────────────
EOF
    if ! grep -q 'razzfazz coding-agent ports motd' "${HOME_DIR}/.bashrc" 2>/dev/null; then
        {
            echo '# razzfazz coding-agent ports motd (managed)'
            echo 'case $- in *i*)'
            echo '  if [ -z "${RZFZ_PORTS_MOTD_SHOWN:-}" ] && [ -f "$HOME/.razzfazz-ports-motd.txt" ]; then'
            echo '    cat "$HOME/.razzfazz-ports-motd.txt"; RZFZ_PORTS_MOTD_SHOWN=1;'
            echo '  fi ;; esac'
        } >> "${HOME_DIR}/.bashrc" 2>/dev/null || true
    fi
fi

# ── git identity ─────────────────────────────────────────────────────────────
if [ -n "${GIT_AUTHOR_NAME}" ]; then
    git config --global user.name "${GIT_AUTHOR_NAME}"
    git config --global user.email "${GIT_AUTHOR_EMAIL:-agent@localhost}"
fi
git config --global --add safe.directory '*' 2>/dev/null || true

# ── Gitea git credentials (per-user token from agent-manager; user token via UI)
if [ -n "${GITEA_API_TOKEN}" ]; then
    git config --global credential.helper store
    GITEA_HOST=$(echo "${GITEA_INTERNAL_URL:-http://gitea:3000}" | sed 's|http[s]*://||' | cut -d/ -f1)
    echo "http://agent:${GITEA_API_TOKEN}@${GITEA_HOST}" > "${HOME_DIR}/.git-credentials"
    chmod 600 "${HOME_DIR}/.git-credentials"
fi

# ── Gitea URL rewrite (#165): copy-pasted external UI URL → reachable host ─────
# The Gitea web UI shows https://git.<domain>/… but the sandbox has no DNS for
# the box's public domain (and the box CA isn't trusted, so TLS would fail next).
# Only the internal http://gitea:3000 is reachable on the agent network. Rewrite
# the external forms transparently — git `insteadOf` applies to clone/fetch/push
# — so `git clone <copy-pasted-UI-URL>` just works: for PUBLIC repos with no
# token at all, and for PRIVATE repos via the credential helper above. Idempotent
# (clear our own insteadOf entries for this internal key each boot, then re-add).
GITEA_INT_URL="${GITEA_INTERNAL_URL:-http://gitea:3000}"
GITEA_INT_URL="${GITEA_INT_URL%/}/"
git config --global --unset-all "url.${GITEA_INT_URL}.insteadOf" 2>/dev/null || true
_add_gitea_rewrite() {
    _e="${1%/}/"
    [ -n "${_e}" ] && [ "${_e}" != "/" ] || return 0
    git config --global --add "url.${GITEA_INT_URL}.insteadOf" "${_e}" 2>/dev/null || true
}
[ -n "${GITEA_EXTERNAL_URL}" ] && _add_gitea_rewrite "${GITEA_EXTERNAL_URL}"
if [ -n "${MAIN_DOMAIN}" ]; then
    _add_gitea_rewrite "https://git.${MAIN_DOMAIN}"
    _add_gitea_rewrite "https://gitea.${MAIN_DOMAIN}"
fi

# ── local-model shim (#36 / PR #84) ──────────────────────────────────────────
# The bundled CLIs can't talk to GPUStack unaided: codex dropped wire_api="chat"
# (GPUStack is Chat-Completions-only, 404s /responses), and opencode+gsd-pi hang
# on qwen3.x because thinking-on burns the whole token budget on reasoning. A
# single tiny stdlib shim on 127.0.0.1 fixes both — it translates codex's
# Responses→Chat AND injects chat_template_kwargs.enable_thinking=false on every
# /chat/completions (codex's translated call + opencode/gsd's direct calls). We
# start it for all three; each CLI's config points at it. Upstream is the real
# GPUStack endpoint (BASE_URL); the shim binds 127.0.0.1 only.
#
# user-defined ALSO starts the shim (#36 / PR #84): we pre-seed Pi's config
# (~/.pi/agent/models.json) to point its gpustack provider at the shim, so a
# user-installed `pi` (an INSTALL_SUGGESTIONS one-liner) talks to qwen3.x with
# enable_thinking=false injected — the exact treatment the dedicated agents get.
SHIM_PORT="${CODING_SHIM_PORT:-${CODEX_SHIM_PORT:-8123}}"
SHIM_URL="http://127.0.0.1:${SHIM_PORT}/v1-openai"   # opencode/gsd/pi (chat) base

# ── supervise the shim (#257) ────────────────────────────────────────────────
# The shim used to be a bare `... &` with nothing watching it. That mattered
# because of how it dies: the agent's shell and BOTH control services share one
# PID namespace and one uid (agent/1000), and same-uid signalling needs no
# CAP_KILL. A broad `pkill -f 'python3'` from inside the sandbox — the exact
# command that took prod down on 2026-08-11 — reaps app.py AND this shim.
#
# #255 gave app.py a supervisor, so the UI now comes back on its own. The shim
# did not have one, which left the WORSE half of the same kill: the terminal
# works, the agent looks healthy, and every model call fails because nothing
# listens on 127.0.0.1:${SHIM_PORT} any more. A visible outage heals itself; a
# silent one waits for the user to report "the model is broken".
#
# This does NOT prevent the kill — see the issue. Prevention needs the control
# services on a different uid from the agent's shell, and under the sandbox's
# own constraints (USER agent as PID 1, CapEff=0, NoNewPrivs=1, read-only
# rootfs) there is no in-container way to get there. Completing the healing is
# what IS available, and it closes the gap that supervision already assumed.
#
# POSIX sh only (dash) — same constraint that crash-looped the #255 loop's
# first cut: no arrays, no $SECONDS, and `if` rather than `test && assign`
# (a false test under `set -e` would terminate the entrypoint).
SHIM_LOG="${SHIM_LOG:-/tmp/local-model-shim.log}"
SHIM_BACKOFF_START="${SHIM_BACKOFF_START:-1}"
SHIM_BACKOFF_MAX="${SHIM_BACKOFF_MAX:-30}"
SHIM_HEALTHY_SECONDS="${SHIM_HEALTHY_SECONDS:-60}"

run_local_model_shim() {
    CODEX_SHIM_UPSTREAM="${BASE_URL}" \
    CODEX_SHIM_PORT="${SHIM_PORT}" \
    CODEX_SHIM_DISABLE_THINKING="${CODING_SHIM_DISABLE_THINKING:-1}" \
        /opt/webui-venv/bin/rzfz-webui /opt/coding-agent-web/local_model_shim.py \
        >> "${SHIM_LOG}" 2>&1
}

supervise_local_model_shim() {
    restarts=0
    backoff="${SHIM_BACKOFF_START}"
    while true; do
        started=$(date +%s)
        rc=0
        run_local_model_shim || rc=$?
        ran_for=$(( $(date +%s) - started ))
        restarts=$(( restarts + 1 ))
        # A shim that had been serving must not inherit a backoff earned days
        # ago on its first crash.
        if [ "${ran_for}" -ge "${SHIM_HEALTHY_SECONDS}" ]; then
            backoff="${SHIM_BACKOFF_START}"
        fi
        echo "[coding-agent] local-model shim exited (rc=${rc}) after ${ran_for}s — restart #${restarts} in ${backoff}s"
        sleep "${backoff}"
        # Grow towards the ceiling: a shim that can NEVER bind (port taken by
        # the user's own dev server) must not spin as fast as the CPU allows.
        backoff=$(awk -v b="${backoff}" -v m="${SHIM_BACKOFF_MAX}" \
                      'BEGIN { b = b * 2; if (b > m) b = m; print b }')
    done
}

case "${AGENT_KIND}" in
  codex|opencode|gsd-pi|user-defined)
    supervise_local_model_shim &
    echo "[coding-agent] local-model shim supervised on 127.0.0.1:${SHIM_PORT}"
    ;;
esac

# ── per-agent model config ───────────────────────────────────────────────────
case "${AGENT_KIND}" in
  gsd-pi)
    # gsd reads ~/.gsd/agent/models.json. Point the gpustack provider baseUrl at
    # the shim so enable_thinking=false is injected (else qwen3.x thinking-burns
    # and the turn never completes). Regenerated every boot from GSD_MODELS_JSON
    # (generated config, not user-edited) so the shim URL always wins.
    GSD_AGENT_DIR="${HOME_DIR}/.gsd/agent"
    mkdir -p "${GSD_AGENT_DIR}"
    if [ -n "${GSD_MODELS_JSON}" ]; then
        printf '%s\n' "${GSD_MODELS_JSON}" \
            | sed "s|${BASE_URL}|${SHIM_URL}|g" > "${GSD_AGENT_DIR}/models.json"
    fi
    # gsd 3.x seeds settings.json with its BUILT-IN default provider/model
    # (openai / codex-mini-latest) on first run — that's the OpenAI cloud, not
    # our local model, so the status bar shows "openai · API key" and every
    # prompt 401s (no OpenAI key). models.json only registers the gpustack
    # PROVIDER; the ACTIVE selection lives in settings.json. Pin it to the
    # gpustack provider + local model so gsd talks to the shim/GPUStack, not
    # OpenAI. Regenerated every boot (generated config, not user-edited) so a
    # stale openai default on an existing volume can't win. (#36 / PR #84)
    cat > "${GSD_AGENT_DIR}/settings.json" << EOF
{
  "quietStartup": true,
  "collapseChangelog": true,
  "defaultProvider": "gpustack",
  "defaultModel": "${LLM_MODEL_VAL}",
  "defaultThinkingLevel": "off"
}
EOF
    PREFS="${HOME_DIR}/.gsd/PREFERENCES.md"
    if [ ! -f "${PREFS}" ]; then
        cat > "${PREFS}" << EOF
---
version: 1
models:
  research: gpustack/${LLM_MODEL_VAL}
  planning: gpustack/${LLM_MODEL_VAL}
  execution: gpustack/${LLM_MODEL_VAL}
  completion: gpustack/${LLM_MODEL_VAL}
  validation: gpustack/${LLM_MODEL_VAL}
---
EOF
    fi
    ;;
  opencode)
    # opencode reads ~/.config/opencode/config.json (from OPENCODE_CONFIG_JSON).
    # opencode 1.17.x has NO config path to add request-body params, so we point
    # its provider baseURL at the shim (which injects enable_thinking=false).
    # Regenerated every boot so the shim URL always wins on existing volumes.
    OC_CONFIG_DIR="${HOME_DIR}/.config/opencode"
    mkdir -p "${OC_CONFIG_DIR}"
    if [ -n "${OPENCODE_CONFIG_JSON}" ]; then
        printf '%s\n' "${OPENCODE_CONFIG_JSON}" \
            | sed "s|${BASE_URL}|${SHIM_URL}|g" > "${OC_CONFIG_DIR}/config.json"
    fi
    ;;
  codex)
    # codex config.toml points at the shim with wire_api="responses" (codex
    # dropped "chat"). ALWAYS (re)written — it's generated, and pre-fix volumes
    # carry a stale wire_api="chat" config codex now refuses to load.
    CODEX_DIR="${HOME_DIR}/.codex"
    mkdir -p "${CODEX_DIR}"
    cat > "${CODEX_DIR}/config.toml" << EOF
# razzfazz.ai — GPUStack local model via the local-model shim (#36).
model = "${LLM_MODEL_VAL}"
model_provider = "gpustack"

[model_providers.gpustack]
name = "GPUStack (local, via shim)"
base_url = "http://127.0.0.1:${SHIM_PORT}/v1"
wire_api = "responses"
env_key = "GPUSTACK_API_KEY"
EOF
    # #36 follow-up: per-user MCP servers → [mcp_servers.<id>]. Appended to the
    # regenerated config.toml every boot from the manager's CURRENT wiring
    # (RAZZFAZZ_CODEX_MCP_JSON = {<id>:{url,bearer_token}}). config.toml is a
    # fully-generated file for codex (regenerated each boot), so appending here
    # is the managed scope; the user manages MCP via Claude Code's .mcp.json.
    if [ -n "${RAZZFAZZ_CODEX_MCP_JSON:-}" ]; then
        python3 - "${CODEX_DIR}/config.toml" << 'PYEOF' || echo "[coding-agent] codex MCP wiring skipped"
import json, os, sys
path = sys.argv[1]
try:
    servers = json.loads(os.environ.get("RAZZFAZZ_CODEX_MCP_JSON", "") or "{}")
except ValueError:
    servers = {}
lines = []
for sid, cfg in servers.items():
    if not isinstance(cfg, dict):
        continue
    lines.append(f"\n[mcp_servers.{sid}]")
    url = cfg.get("url", "")
    lines.append(f'url = {json.dumps(url)}')
    bt = cfg.get("bearer_token")
    if bt:
        lines.append(f'bearer_token = {json.dumps(bt)}')
if lines:
    with open(path, "a") as fh:
        fh.write("\n# razzfazz.ai managed — per-user MCP proxies (regenerated each boot)\n")
        fh.write("\n".join(lines) + "\n")
    print(f"[coding-agent] wired {len(servers)} MCP server(s) into codex config.toml")
PYEOF
    fi
    ;;
  user-defined)
    # Pre-wire Pi (@earendil-works/pi-coding-agent, pi.dev) to the local model
    # (#36 / PR #84). user-defined ships NO agent — the terminal is bare — but
    # Pi is a first-class INSTALL_SUGGESTIONS entry, so we seed its config file
    # UP FRONT: the moment the user runs `npm install -g
    # @earendil-works/pi-coding-agent` and then `pi`, it already speaks to
    # qwen3.6 via GPUStack (no OpenAI 401, no cloud). Pi reads
    # ~/.pi/agent/models.json — the SAME `providers.<name>` schema gsd uses
    # (they share lineage), so PI_MODELS_JSON is gsd_models_json() verbatim.
    # We point the gpustack provider baseUrl at the shim (SHIM_URL) so
    # enable_thinking=false is injected on every call (qwen3.x thinking-burn
    # guard) — Pi could pass it natively via compat.chatTemplateKwargs, but the
    # shim path is the same fallback the other agents use and is authoritative.
    #
    # IDEMPOTENT — unlike the generated dedicated-agent configs, this one is
    # seeded ONLY IF ABSENT so a user who hand-edits ~/.pi/agent/models.json
    # (adds their own cloud provider, tweaks the model) is never clobbered on
    # restart. The dir lives under /home/agent (persistent per-user volume), so
    # the seeded config survives a container recreate.
    if [ -n "${PI_MODELS_JSON}" ]; then
        PI_AGENT_DIR="${HOME_DIR}/.pi/agent"
        PI_MODELS_FILE="${PI_AGENT_DIR}/models.json"
        if [ ! -f "${PI_MODELS_FILE}" ]; then
            mkdir -p "${PI_AGENT_DIR}"
            printf '%s\n' "${PI_MODELS_JSON}" \
                | sed "s|${BASE_URL}|${SHIM_URL}|g" > "${PI_MODELS_FILE}"
            echo "[coding-agent] seeded Pi local-model config at ${PI_MODELS_FILE}"
        else
            echo "[coding-agent] Pi config exists at ${PI_MODELS_FILE} — leaving user copy untouched"
        fi
    fi
    # Pre-wire DeepSeek Harness (dsh, @deepseek-ai/dsh) the same way (#556).
    # dsh embeds a fork of Pi's LLM layer: $DSH_HOME/settings.yaml nests the
    # SAME providers schema under an `llm-pi-ai:` root (with baseURL spelled
    # capital-URL). DSH_HOME is pinned to ~/.dsh by the managed env file above.
    # Model list comes from PI_MODELS_JSON (same authority as the Pi seed);
    # baseURL points at the SHIM so the qwen3.x thinking-burn guard applies.
    # apiKeyEnv, never a literal key: GPUSTACK_API_KEY is already in the env,
    # and dsh treats literal keys as write-only credentials anyway.
    # Seeded ONLY IF ABSENT — a hand-edited settings.yaml is never clobbered.
    if [ -n "${PI_MODELS_JSON}" ]; then
        DSH_HOME_DIR="${HOME_DIR}/.dsh"
        DSH_SETTINGS="${DSH_HOME_DIR}/settings.yaml"
        if [ ! -f "${DSH_SETTINGS}" ]; then
            mkdir -p "${DSH_HOME_DIR}"
            PI_MODELS_JSON="${PI_MODELS_JSON}" DSH_SHIM_URL="${SHIM_URL}"                 python3 - "${DSH_SETTINGS}" << 'PYEOF' || echo "[coding-agent] dsh seed skipped"
import json, os, sys
path = sys.argv[1]
prov = json.loads(os.environ["PI_MODELS_JSON"]).get("providers", {}).get("gpustack") or {}
models = [m.get("id") for m in prov.get("models", []) if m.get("id")]
if not models:
    raise SystemExit("no chat models in PI_MODELS_JSON")
lines = [
    "# razzfazz.ai seeded (#556) — dsh wired to the box's local model via the",
    "# shim. Seeded only if absent: edit freely, this file is never regenerated.",
    "llm-pi-ai:",
    "  providers:",
    "    gpustack:",
    "      api: openai-completions",
    "      baseURL: " + os.environ["DSH_SHIM_URL"],
    "      apiKeyEnv: GPUSTACK_API_KEY",
    "      compat:",
    "        supportsDeveloperRole: false",
    "        maxTokensField: max_tokens",
    "      models:",
] + ["        - id: " + m for m in models]
with open(path, "w") as fh:
    fh.write("\n".join(lines) + "\n")
print(f"[coding-agent] seeded dsh local-model config at {path} ({len(models)} model(s))")
PYEOF
        else
            echo "[coding-agent] dsh config exists at ${DSH_SETTINGS} — leaving user copy untouched"
        fi
    fi
    ;;
esac

# ── Claude Code per-user MCP wiring (all coding-agent kinds) ─────────────────
# #36 follow-up. Claude Code (user-installed into any coding-agent container)
# reads project MCP servers from ./.mcp.json (mcpServers map). We write a MANAGED
# block into /workspace/.mcp.json every boot from the manager's CURRENT wiring
# (RAZZFAZZ_CLAUDE_MCP_JSON = the mcpServers map with fresh URL + bearer). The
# block is scoped by a per-server marker ("_razzfazz_managed": true) so a user's
# manual `claude mcp add` entries (which land WITHOUT the marker) are preserved
# across recreates: on regeneration we drop only OUR previously-managed keys and
# re-add the current set, leaving every unmarked (user-owned) key untouched.
# Reachability: the URLs are public opaque-token mcp.<domain> subdomains reached
# over the coding-agents non-internal egress → Caddy (no allow-list entry
# needed; the bearer + SSO forward-auth gate the hop).
case "${AGENT_KIND}" in
  coding-tools|opencode|codex|gsd-pi|user-defined)
    CLAUDE_PROJECT_DIR="/workspace"
    mkdir -p "${CLAUDE_PROJECT_DIR}" 2>/dev/null || true
    CLAUDE_MCP_FILE="${CLAUDE_PROJECT_DIR}/.mcp.json"
    python3 - "${CLAUDE_MCP_FILE}" << 'PYEOF' || echo "[coding-agent] Claude Code MCP wiring skipped"
import json, os, sys
path = sys.argv[1]
MARK = "_razzfazz_managed"
try:
    managed = json.loads(os.environ.get("RAZZFAZZ_CLAUDE_MCP_JSON", "") or "{}")
except ValueError:
    managed = {}
if not isinstance(managed, dict):
    managed = {}
# Load whatever the user (or a prior boot) left in place.
doc = {}
if os.path.exists(path):
    try:
        with open(path) as fh:
            doc = json.load(fh) or {}
    except (ValueError, OSError):
        doc = {}
if not isinstance(doc, dict):
    doc = {}
servers = doc.get("mcpServers")
if not isinstance(servers, dict):
    servers = {}
# Drop ONLY our previously-managed keys; keep every user-added (unmarked) entry
# so a manual `claude mcp add` survives recreate (never clobbered).
servers = {k: v for k, v in servers.items()
           if not (isinstance(v, dict) and v.get(MARK) is True)}
# Re-add the current managed set with the marker + fresh url/bearer.
for sid, cfg in managed.items():
    if not isinstance(cfg, dict):
        continue
    entry = dict(cfg)
    entry[MARK] = True
    servers[sid] = entry
doc["mcpServers"] = servers
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(doc, fh, indent=2)
os.replace(tmp, path)
n = sum(1 for v in servers.values() if isinstance(v, dict) and v.get(MARK) is True)
print(f"[coding-agent] wrote {n} managed MCP server(s) to {path} "
      f"({len(servers) - n} user entr(y/ies) preserved)")
PYEOF
    ;;
esac

# ── tmux: start the server so continuum auto-restores prior sessions ─────────
# The resurrect save dir lives on the persistent volume; continuum's
# @continuum-restore 'on' triggers a restore when the server starts. We start
# it eagerly here (before the web UI) so a `docker restart` brings back the
# user's tabs without any browser interaction.
mkdir -p "${HOME_DIR}/.local/share/tmux/resurrect"
export TMUX_TMPDIR="${TMUX_TMPDIR:-/tmp}"
tmux start-server 2>/dev/null || true
# Give continuum a moment to run its restore hook on server start.
sleep 1

# ── supervise the shared web UI (#255) ───────────────────────────────────────
# This used to start the UI once in the background and then `exec tail -f
# /dev/null`. When the UI died — an in-sandbox action killed it on prod
# 2026-08-11 — nothing ever listened on :3004 again: the container stayed **Up**
# with no restart and no OOMKilled, the readiness probe failed forever, and the
# only cure was a manual `docker restart`. `tail` also never wait()s, so the
# dead UI lingered as a zombie.
#
# The loop below is the container's foreground process, so the UI is waited on
# (reaped) and restarted for as long as the container lives.
#
# Making the UI itself the container's main process was the obvious alternative
# and is the wrong trade: with `restart_policy: unless-stopped` the WHOLE
# container would bounce on every UI crash, taking the user's tmux sessions with
# it. Supervising in place is what keeps their work.
#
# POSIX sh ONLY — this file runs under dash (`#!/bin/sh`), not bash. The first
# cut of this loop used a bash array for the command and `$SECONDS` for the
# clock; both parse fine under `bash -n` and die under dash with
# `Syntax error: "(" unexpected`, which crash-looped every coding agent. The
# command therefore lives in a FUNCTION (overridable, no array needed) and the
# clock is `date +%s`.
run_web_ui() {
    CODING_TOOLS_WEB_PORT="${CODING_TOOLS_WEB_PORT:-3004}" \
        /opt/webui-venv/bin/rzfz-webui /opt/coding-agent-web/app.py \
        >> "${WEB_UI_LOG}" 2>&1
}
WEB_UI_LOG="${WEB_UI_LOG:-/tmp/coding-agent-web.log}"
WEB_UI_BACKOFF_START="${WEB_UI_BACKOFF_START:-1}"
WEB_UI_BACKOFF_MAX="${WEB_UI_BACKOFF_MAX:-30}"
# A run longer than this counts as "it was working", so the next crash restarts
# promptly instead of inheriting a backoff earned days ago.
WEB_UI_HEALTHY_SECONDS="${WEB_UI_HEALTHY_SECONDS:-60}"

supervise_web_ui() {
    restarts=0
    backoff="${WEB_UI_BACKOFF_START}"
    while true; do
        started=$(date +%s)
        # `|| rc=$?` is load-bearing, not style. This script runs under `set -e`
        # (line 19), so a BARE `run_web_ui` that exits non-zero terminates the
        # whole entrypoint before the next line can even read `$?` — the
        # container then exits, `restart_policy: unless-stopped` bounces it, and
        # the user's tmux sessions die. That is precisely the outcome this
        # supervisor exists to avoid, and it is what happened on 0.91: killing
        # the UI restarted the CONTAINER (`Up 3 seconds`, `tmux ls` → no server)
        # instead of just the UI.
        rc=0
        run_web_ui || rc=$?
        ran_for=$(( $(date +%s) - started ))
        restarts=$(( restarts + 1 ))
        # Reset the backoff when the UI had actually been serving: an agent that
        # ran for a week must not wait out a capped delay on its first crash.
        if [ "${ran_for}" -ge "${WEB_UI_HEALTHY_SECONDS}" ]; then
            backoff="${WEB_UI_BACKOFF_START}"
        fi
        echo "[coding-agent] web UI exited (rc=${rc}) after ${ran_for}s — restart #${restarts} in ${backoff}s"
        sleep "${backoff}"
        # Grow towards the ceiling so a UI that can NEVER start (bad config,
        # port taken) does not spin as fast as the CPU allows — that would turn
        # this self-healing measure into the outage.
        backoff=$(awk -v b="${backoff}" -v m="${WEB_UI_BACKOFF_MAX}" \
                      'BEGIN { b = b * 2; if (b > m) b = m; print b }')
    done
}

echo "[coding-agent] kind=${AGENT_KIND} label=${AGENT_LABEL:-$AGENT_KIND}"
echo "[coding-agent] Starting web UI on port ${CODING_TOOLS_WEB_PORT:-3004}..."
echo "[coding-agent] Container ready (user=$(id -un), uid=$(id -u))."
supervise_web_ui
