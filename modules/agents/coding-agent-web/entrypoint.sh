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

# #2423: the hardened container IS the sandbox (non-root, read-only root fs,
# CapDrop ALL, no-new-privileges, pids limit, the egress-fenced coding-agents
# net). Codex's own bubblewrap sandbox needs user/mount namespaces the
# container denies; inside it a "sandboxed" command silently does not run
# (openai/codex#46246). So the inner sandbox is off, by configuration.
write_codex_config() {
    # codex config.toml points at the shim with wire_api="responses" (codex
    # dropped "chat"). ALWAYS (re)written — it's generated, and pre-fix volumes
    # carry a stale wire_api="chat" config codex now refuses to load.
    CODEX_DIR="${HOME_DIR}/.codex"
    mkdir -p "${CODEX_DIR}"
    CODEX_CATALOG_LINE="# model catalog not written; codex runs on fallback metadata (no file edits)"
    if write_codex_model_catalog "${CODEX_DIR}/model-catalog.json"; then
        CODEX_CATALOG_LINE="model_catalog_json = \"${CODEX_DIR}/model-catalog.json\""
    else
        echo "[coding-agent] codex model catalog not written; codex cannot edit files"
    fi
    cat > "${CODEX_DIR}/config.toml" << EOF
# razzfazz.ai — GPUStack local model via the local-model shim (#36).
model = "${LLM_MODEL_VAL}"
model_provider = "gpustack"
# #2423: the container is the sandbox; codex's inner (bubblewrap) sandbox off.
sandbox_mode = "danger-full-access"
${CODEX_CATALOG_LINE}

[model_providers.gpustack]
name = "GPUStack (local, via shim)"
base_url = "http://127.0.0.1:${SHIM_PORT}/v1"
wire_api = "responses"
env_key = "GPUSTACK_API_KEY"
EOF
}

# Without a catalog entry for the local model, codex runs on "fallback model
# metadata", and that metadata offers no apply_patch tool: the agent can read
# files but never edit them (it loops on `cat`). One entry per model the
# session picker offers (LLM_MODEL plus AGENT_MODEL_ALIASES), apply_patch as
# codex's freeform tool, which the local-model shim translates for the
# Chat-Completions upstream. context_window is the stack's per-slot context
# when the manager passed it (CODEX_MODEL_CONTEXT_JSON = {alias: tokens}),
# else a conservative default. The instructions are codex's own
# (codex-model-messages.json, Apache-2.0, attributed there and in NOTICE).
# The keys written are the ones codex 0.151 requires plus the ones later
# releases require, so a CLI bump does not silently drop the catalog.
# codex's instructions are written for GPT models, which get apply_patch's
# grammar natively; on the Chat-Completions path the local model only sees it
# as text and writes malformed patches. So razzfazz.ai's own short, exact
# apply_patch spec with two examples (codex-local-patch-instructions.md) is
# appended to codex's instructions. Its effect on patch attempts per edit is
# not yet measured.
CODEX_MODEL_MESSAGES="${CODEX_MODEL_MESSAGES:-/opt/coding-agent-web/codex-model-messages.json}"
CODEX_PATCH_INSTRUCTIONS="${CODEX_PATCH_INSTRUCTIONS:-/opt/coding-agent-web/codex-local-patch-instructions.md}"
CODEX_DEFAULT_CONTEXT_WINDOW="${CODEX_DEFAULT_CONTEXT_WINDOW:-131072}"
write_codex_model_catalog() {
    python3 - "$1" "${CODEX_MODEL_MESSAGES}" "${LLM_MODEL_VAL}" \
        "${CODEX_DEFAULT_CONTEXT_WINDOW}" "${CODEX_PATCH_INSTRUCTIONS}" << 'PYEOF'
import json, os, sys
out, messages_path, default_model, default_ctx, patch_path = sys.argv[1:6]
try:
    with open(messages_path) as fh:
        messages = json.load(fh)["model_messages"]
    if not (messages.get("instructions_template") or "").strip():
        raise KeyError("instructions_template")
except (OSError, ValueError, KeyError, AttributeError) as e:
    print(f"[coding-agent] codex model messages unusable: {e!r}")
    sys.exit(1)
try:
    with open(patch_path) as fh:
        patch_spec = fh.read().strip()
except OSError as e:
    patch_spec = ""
    print(f"[coding-agent] apply_patch instructions not added: {e!r}")
if patch_spec:
    messages = dict(messages)
    messages["instructions_template"] = (
        messages["instructions_template"].rstrip() + "\n\n" + patch_spec + "\n")


def _env_json(name, kind):
    try:
        v = json.loads(os.environ.get(name, "") or "null")
    except ValueError:
        return kind()
    return v if isinstance(v, kind) else kind()


contexts = _env_json("CODEX_MODEL_CONTEXT_JSON", dict)
slugs = []
for s in [default_model] + _env_json("AGENT_MODEL_ALIASES", list):
    if isinstance(s, str) and s and s not in slugs:
        slugs.append(s)
models = []
for i, slug in enumerate(slugs):
    try:
        ctx = int(contexts.get(slug) or 0)
    except (TypeError, ValueError):
        ctx = 0
    models.append({
        "slug": slug,
        "display_name": slug,
        "description": None,
        "apply_patch_tool_type": "freeform",
        "input_modalities": ["text"],
        "supports_parallel_tool_calls": False,
        "context_window": ctx if ctx > 0 else int(default_ctx),
        "supported_reasoning_levels": [],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": i,
        "support_verbosity": False,
        "default_verbosity": None,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "experimental_supported_tools": [],
        "include_apps_usage_instructions": False,
        "model_messages": messages,
    })
tmp = out + ".rzfz-tmp"
with open(tmp, "w") as fh:
    json.dump({"models": models}, fh, indent=1)
    fh.write("\n")
os.replace(tmp, out)
PYEOF
}

# #2423: Claude Code (installed by the user into any coding agent) has an
# opt-in sandbox that needs the same namespaces. Pin it off in the user's
# settings; every other key the user set survives. An unreadable file is left
# exactly as it is (warned, not rewritten).
pin_claude_code_sandbox_off() {
    mkdir -p "${HOME_DIR}/.claude"
    python3 - "${HOME_DIR}/.claude/settings.json" << 'PYEOF' || echo "[coding-agent] Claude Code sandbox pin skipped"
import json, os, sys
path = sys.argv[1]
doc = {}
if os.path.exists(path):
    try:
        with open(path) as fh:
            doc = json.load(fh) or {}
    except (ValueError, OSError):
        print("[coding-agent] ~/.claude/settings.json unreadable; sandbox pin skipped")
        sys.exit(0)
if not isinstance(doc, dict):
    sys.exit(0)
sb = doc.get("sandbox") if isinstance(doc.get("sandbox"), dict) else {}
sb["enabled"] = False
doc["sandbox"] = sb
tmp = path + ".rzfz-tmp"
with open(tmp, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
PYEOF
}

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
    write_codex_config
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

# #2423: inner sandboxes off for every coding-agent kind (the container is it).
case "${AGENT_KIND}" in
  codex|opencode|gsd-pi|user-defined)
    pin_claude_code_sandbox_off
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
#
# #3143: the restore no longer depends on continuum's auto-restore. That runs in
# the BACKGROUND and silently skips itself whenever `ps` shows any other process
# starting with "tmux" (it takes one for "another tmux server"): the web UI's
# first tmux call or a browser's `tmux attach` racing it was enough, so after a
# reboot one agent got its tabs back and another, same image, got none. And a
# bare `tmux start-server` with tmux's default `exit-empty on` exits again while
# the server holds no session, so a restore that has not created one yet is lost
# with the server. So: keep the server alive, switch continuum's auto-restore off
# with its own halt file (it keeps auto-SAVING), clean the save, and run
# resurrect's restore here, synchronously, before the web UI starts. The
# settings are applied here, not only in ~/.tmux.conf: that file lives on the
# per-user home volume, which an image update never refreshes.
RESURRECT_DIR="${HOME_DIR}/.local/share/tmux/resurrect"
RESURRECT_RESTORE=/opt/tmux-plugins/tmux-resurrect/scripts/restore.sh
RESURRECT_DEDUPE=/opt/coding-agent-web/resurrect-dedupe.sh
RESURRECT_VALID_LAST=/opt/coding-agent-web/resurrect-valid-last.sh
RESURRECT_KEEP_LAST=/opt/coding-agent-web/resurrect-keep-last.sh
# #3396: the pin names the save the boot restore uses (line 1), when it
# expires (line 2 — empty until the user's first save with a session, when
# resurrect-keep-last.sh starts the clock) and how long it holds from then
# (line 3); its pane contents are copied beside it. While it is live,
# resurrect-keep-last.sh keeps a smaller save from becoming `last`. Removed
# first: a pin from the previous container life says nothing about this boot.
# The clock starts at the first save with a session, not at boot: an agent
# can sit for hours after a refresh before anyone attaches (measured: boot
# 06:39, first attach 14:00). Bounded so a failed or skipped restore cannot
# keep the user's own work out of `last` for longer than
# RZFZ_TMUX_PIN_SECONDS of that work (default 24 h, so a day of reconnects
# does not use it up).
RESURRECT_PIN="${RESURRECT_DIR}/.boot-restore-pending"
RZFZ_TMUX_PIN_SECONDS="${RZFZ_TMUX_PIN_SECONDS:-86400}"
case "${RZFZ_TMUX_PIN_SECONDS}" in
    ''|*[!0-9]*) RZFZ_TMUX_PIN_SECONDS=86400 ;;
esac
mkdir -p "${RESURRECT_DIR}"
rm -f "${RESURRECT_PIN}" "${RESURRECT_PIN}.pane_contents.tar.gz" "${RESURRECT_DIR}/.boot-restore-rejected"
export TMUX_TMPDIR="${TMUX_TMPDIR:-/tmp}"
touch "${HOME_DIR}/tmux_no_auto_restore" 2>/dev/null || true
tmux start-server \; set-option -g exit-empty off 2>/dev/null || true
tmux set-option -g @resurrect-hook-post-save-layout "${RESURRECT_DEDUPE}" 2>/dev/null || true
tmux set-option -g @resurrect-hook-post-save-all "${RESURRECT_KEEP_LAST} ${RESURRECT_DIR}" 2>/dev/null || true

# Sessions (one per line) that have a pane record in save $1.
tmux_sessions_of_save() {
    awk -F '\t' '$1 == "pane" && !seen[$2]++ { print $2 }' "$1" 2>/dev/null || true
}

# #3396: every branch below logs — a restore skipped without a word is how a
# user's tabs were lost after an agent refresh. The fallback runs FIRST, for
# every branch: a dangling `last` (`[ -e ]` follows the symlink, so it read as
# "no save") is repointed to the newest valid save before anything is decided.
# #3238: `last` can also point at an empty/truncated save that continuum wrote
# while the tmux server was being torn down on a container stop; the same
# fallback repoints it (saves are never deleted; skipped ones are logged).
if [ -L "${RESURRECT_DIR}/last" ] && [ ! -e "${RESURRECT_DIR}/last" ]; then
    echo "[coding-agent] tmux restore: last is dangling (-> $(readlink "${RESURRECT_DIR}/last")); repairing it from the newest valid save"
fi
"${RESURRECT_VALID_LAST}" "${RESURRECT_DIR}" || true
# A live server with no session answers list-sessions with rc 0 and no output,
# so "no sessions yet" is judged by the output, not the exit code.
TMUX_BOOT_SESSIONS=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | tr '\n' ' ')
if [ ! -e "${RESURRECT_DIR}/last" ]; then
    if [ -L "${RESURRECT_DIR}/last" ]; then
        echo "[coding-agent] tmux restore skipped: last is dangling and no valid save is left in ${RESURRECT_DIR}; tabs start empty"
    else
        echo "[coding-agent] tmux restore skipped: no tmux save in ${RESURRECT_DIR} yet (first start)"
    fi
else
    TMUX_BOOT_SAVE=$(basename "$(readlink -f "${RESURRECT_DIR}/last" 2>/dev/null || echo last)")
    printf '%s\n\n%s\n' "${TMUX_BOOT_SAVE}" "${RZFZ_TMUX_PIN_SECONDS}" \
        > "${RESURRECT_PIN}" 2>/dev/null || true
    if [ -f "${RESURRECT_DIR}/pane_contents.tar.gz" ]; then
        cp -f "${RESURRECT_DIR}/pane_contents.tar.gz" "${RESURRECT_PIN}.pane_contents.tar.gz" 2>/dev/null || true
    fi
    if [ -n "${TMUX_BOOT_SESSIONS}" ]; then
        echo "[coding-agent] tmux restore skipped: sessions already present at boot: ${TMUX_BOOT_SESSIONS}— last stays on ${TMUX_BOOT_SAVE} until a save holds as many sessions or the user has worked ${RZFZ_TMUX_PIN_SECONDS}s"
    else
        "${RESURRECT_DEDUPE}" "${RESURRECT_DIR}/last"
        tmux run-shell "${RESURRECT_RESTORE}" 2>/dev/null && TMUX_RESTORE_RC=0 || TMUX_RESTORE_RC=$?
        # restore.sh answers 0 even when it brought nothing back, so the
        # restore counts only when every session of the save now exists.
        TMUX_NOW=$(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
        TMUX_MISSING=""
        for s in $(tmux_sessions_of_save "${RESURRECT_DIR}/${TMUX_BOOT_SAVE}"); do
            printf '%s\n' "${TMUX_NOW}" | grep -Fqx -- "$s" || TMUX_MISSING="${TMUX_MISSING}${s} "
        done
        if [ "${TMUX_RESTORE_RC}" -ne 0 ]; then
            echo "[coding-agent] tmux restore failed (rc=${TMUX_RESTORE_RC}); save kept in ${RESURRECT_DIR}; last stays on ${TMUX_BOOT_SAVE} for ${RZFZ_TMUX_PIN_SECONDS}s of work"
        elif [ -z "${TMUX_NOW}" ]; then
            echo "[coding-agent] tmux restore failed: restore ran but restored nothing from ${TMUX_BOOT_SAVE}; last stays on it for ${RZFZ_TMUX_PIN_SECONDS}s of work"
        elif [ -n "${TMUX_MISSING}" ]; then
            echo "[coding-agent] tmux restore incomplete: missing ${TMUX_MISSING}from ${TMUX_BOOT_SAVE}; last stays on it for ${RZFZ_TMUX_PIN_SECONDS}s of work"
        else
            rm -f "${RESURRECT_PIN}" "${RESURRECT_PIN}.pane_contents.tar.gz"
            echo "[coding-agent] restored tmux sessions: $(printf '%s\n' "${TMUX_NOW}" | tr '\n' ' ')(from ${TMUX_BOOT_SAVE})"
        fi
    fi
fi

# ── tmux: periodic save that needs no attached client (#3396) ────────────────
# Continuum saves from the status line, i.e. only while a client is attached.
# A user who works through Claude's remote control never attaches the browser
# terminal, so nothing was saved for days and a restart restored a days-old
# snapshot. This loop runs resurrect's own save script every
# RZFZ_TMUX_SAVE_INTERVAL seconds (0 = off) whether or not a client is
# attached. It skips a round:
#   * with no session (such a save would restore nothing),
#   * while the container stops (the TERM trap kills the loop and sets
#     @continuum-save-interval 0, which the loop reads as well),
#   * when continuum saved less than an interval ago (a client is attached
#     and continuum is saving), so the two do not write the same second's
#     file. After its own VALID save the loop stamps continuum's last-save
#     time for the same reason.
# A save never shrinks `last` before the boot restore completed (bounded):
# see resurrect-keep-last.sh (the post-save-all hook set above).
#
# The LOCALE is load-bearing. resurrect's save.sh reads the layout with
# `tmux list-panes -F` using TAB separators, and a tmux CLIENT that sees no
# UTF-8 locale prints them as `_`: the save then holds no pane record at all.
# Measured with the image's own environment (no LANG/LC_*): every save from
# outside tmux was useless, while the same script under `tmux run-shell`
# (continuum, the boot restore) was fine. tmux decides from the variable's
# text, so LC_ALL=C.UTF-8 on the save call fixes it whatever the image sets;
# and a round counts only when the save IT wrote is valid.
# POSIX sh ONLY (dash): no arrays, no $SECONDS.
RESURRECT_DIR="${RESURRECT_DIR:-${HOME_DIR:-${HOME:-/home/agent}}/.local/share/tmux/resurrect}"
RESURRECT_VALID_LAST="${RESURRECT_VALID_LAST:-/opt/coding-agent-web/resurrect-valid-last.sh}"
RESURRECT_KEEP_LAST="${RESURRECT_KEEP_LAST:-/opt/coding-agent-web/resurrect-keep-last.sh}"
RESURRECT_SAVE="${RESURRECT_SAVE:-/opt/tmux-plugins/tmux-resurrect/scripts/save.sh}"
RZFZ_TMUX_SAVE_INTERVAL="${RZFZ_TMUX_SAVE_INTERVAL:-120}"
RZFZ_TMUX_SAVE_TIMEOUT="${RZFZ_TMUX_SAVE_TIMEOUT:-60}"
# The stop save has to fit the stop grace period (10 s by default).
RZFZ_TMUX_STOP_SAVE_TIMEOUT="${RZFZ_TMUX_STOP_SAVE_TIMEOUT:-5}"
TMUX_SAVE_LOOP_PID=""

# tmux bounded in time: a hung server must not eat the stop grace period.
tmux_q() {
    timeout 3 tmux "$@"
}

tmux_has_sessions() {
    [ -n "$(tmux_q list-sessions -F '#{session_name}' 2>/dev/null)" ]
}

tmux_save_files() {
    ls "${RESURRECT_DIR}"/tmux_resurrect_*.txt 2>/dev/null || true
}

# Did the save of THIS round produce a valid save? $1 = the save files listed
# before it ran. A file that is new since then must have a complete pane
# record. With no new file, resurrect found the state identical to `last`
# and dropped the new save (or keep-last dropped a repeat), so `last` must be
# valid. Judging by "the newest file" instead let an older cut-off save that
# stayed newest (a save killed mid-write, the layout unchanged since) fail
# every later round.
tmux_round_saved() {
    tmux_new=""
    for tmux_f in $(tmux_save_files); do
        printf '%s\n' "$1" | grep -Fqx -- "${tmux_f}" || tmux_new="${tmux_f}"
    done
    if [ -n "${tmux_new}" ]; then
        "${RESURRECT_VALID_LAST}" --check "${tmux_new}"
    else
        "${RESURRECT_VALID_LAST}" --check "$(readlink -f "${RESURRECT_DIR}/last" 2>/dev/null || echo "${RESURRECT_DIR}/last")"
    fi
}

# A save killed mid-write (a stop, the timeout) before resurrect linked it
# leaves no `last` in a fresh directory: reproduced with real tmux and
# tmux-resurrect, a stop landing inside the first save of a 1 s-interval loop
# left either a cut file or even a COMPLETE save with no `last`. Never leave
# a run without `last` while a valid save exists: repair after every round
# and after the loop is stopped.
tmux_repair_last() {
    "${RESURRECT_VALID_LAST}" "${RESURRECT_DIR}" >/dev/null 2>&1 || true
}

# #3401: the keep-last decision of the post-save-all hook, run once more on
# stop. resurrect repoints `last` BEFORE it calls the hook, so a stop landing
# between the two (the loop's save killed there, or continuum's) leaves a
# save as `last` that the rule never judged; when the stop save then fails
# too, the next boot would restore that save — the small post-boot tmux —
# although the pin is still live. Run after the loop is stopped and the stop
# save attempted, so nothing races it; a no-op when the hook already ran
# (`last` is the pinned save, or the pin is gone). Bounded like every tmux
# call on the stop path: it must not eat the stop grace period.
tmux_keep_last() {
    timeout 3 "${RESURRECT_KEEP_LAST}" "${RESURRECT_DIR}" >/dev/null 2>&1 || true
}

# One resurrect save, bounded by $1 seconds. Returns non-zero when the server
# holds no session (or is not up) or the save holds no pane record.
tmux_save_now() {
    tmux_has_sessions || return 1
    # own names: this runs in the TERM handler, in the middle of the
    # supervisor, which has an `rc` of its own
    tmux_before=$(tmux_save_files)
    tmux_rc=0
    LC_ALL=C.UTF-8 LANG=C.UTF-8 timeout "$1" "${RESURRECT_SAVE}" quiet >/dev/null 2>&1 || tmux_rc=$?
    tmux_repair_last
    [ "${tmux_rc}" -eq 0 ] || return 1
    tmux_round_saved "${tmux_before}"
}

tmux_save_loop() {
    TMUX_SAVE_PID=""
    TMUX_SLEEP_PID=""
    # TERM ends the loop at once, with its sleep; a save in flight is killed
    # before `last` is touched (resurrect repoints `last` only after the file
    # is written).
    # The killed save is waited for, so nothing of it still runs when the
    # stop handler repairs `last` and takes its own save.
    trap 'kill ${TMUX_SAVE_PID} ${TMUX_SLEEP_PID} 2>/dev/null || true; [ -z "${TMUX_SAVE_PID}" ] || wait "${TMUX_SAVE_PID}" 2>/dev/null || true; exit 0' TERM
    while :; do
        sleep "${RZFZ_TMUX_SAVE_INTERVAL}" &
        TMUX_SLEEP_PID=$!
        wait "${TMUX_SLEEP_PID}" || true
        TMUX_SLEEP_PID=""
        [ "${RZFZ_STOPPING:-0}" -eq 0 ] || exit 0
        [ "$(tmux_q show-option -gqv @continuum-save-interval 2>/dev/null)" != "0" ] || continue
        now=$(date +%s)
        cont=$(tmux_q show-option -gqv @continuum-save-last-timestamp 2>/dev/null) || cont=0
        case "${cont}" in
            ''|*[!0-9]*) cont=0 ;;
        esac
        [ $(( now - cont )) -ge "${RZFZ_TMUX_SAVE_INTERVAL}" ] || continue
        tmux_has_sessions || continue
        # In the background so the TERM trap can kill it: $! is `timeout`
        # itself, which passes the TERM on to the save script.
        rc=0
        before=$(tmux_save_files)
        LC_ALL=C.UTF-8 LANG=C.UTF-8 timeout "${RZFZ_TMUX_SAVE_TIMEOUT}" "${RESURRECT_SAVE}" quiet >/dev/null 2>&1 &
        TMUX_SAVE_PID=$!
        wait "${TMUX_SAVE_PID}" || rc=$?
        TMUX_SAVE_PID=""
        tmux_repair_last
        if [ "${rc}" -ne 0 ]; then
            echo "[coding-agent] periodic tmux save failed (rc=${rc}); next try in ${RZFZ_TMUX_SAVE_INTERVAL}s"
        elif ! tmux_round_saved "${before}"; then
            echo "[coding-agent] periodic tmux save wrote no pane record; next try in ${RZFZ_TMUX_SAVE_INTERVAL}s"
        else
            tmux_q set-option -g @continuum-save-last-timestamp "$(date +%s)" 2>/dev/null || true
        fi
    done
}

start_tmux_save_loop() {
    case "${RZFZ_TMUX_SAVE_INTERVAL}" in
        ''|*[!0-9]*|0)
            echo "[coding-agent] periodic tmux save off (RZFZ_TMUX_SAVE_INTERVAL=${RZFZ_TMUX_SAVE_INTERVAL})"
            return 0 ;;
    esac
    tmux_save_loop &
    TMUX_SAVE_LOOP_PID=$!
    echo "[coding-agent] periodic tmux save every ${RZFZ_TMUX_SAVE_INTERVAL}s, with or without an attached client"
}

stop_tmux_save_loop() {
    if [ -n "${TMUX_SAVE_LOOP_PID}" ]; then
        kill "${TMUX_SAVE_LOOP_PID}" 2>/dev/null || true
        wait "${TMUX_SAVE_LOOP_PID}" 2>/dev/null || true
        TMUX_SAVE_LOOP_PID=""
        # the loop's save in flight may have been killed before resurrect
        # linked it
        tmux_repair_last
    fi
}

start_tmux_save_loop

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

# #3238: a container stop must not let continuum's periodic save write an
# empty save that becomes `last` while the tmux server is torn down (that is
# how a 10-byte save ended up as `last` on a self-rebooted prod box, and the
# next boot restored nothing). Continuum re-reads @continuum-save-interval on
# every status refresh, so setting it to 0 stops the periodic save at once.
# The trap is installed on PID 1 (without it the kernel would ignore SIGTERM
# here), and the supervise loop below runs the UI — and every backoff sleep
# slice — in the background on `wait`, so the trap fires immediately instead
# of after the current foreground command exits.
#
# #3396: the signal is also the last moment the tmux server is intact, so the
# handler saves ONCE before the container goes: periodic saves stop first
# (continuum's interval to 0, the entrypoint's own loop killed) so nothing
# races the stop save, then one save bounded by RZFZ_TMUX_STOP_SAVE_TIMEOUT so
# it cannot eat the stop grace period. With no session there is nothing to
# save and nothing is written. The empty #3238 save came from the teardown
# AFTER this point, which the disabled interval still prevents.
RZFZ_STOPPING=0
on_container_signal() {
    if [ "${RZFZ_STOPPING:-0}" -eq 1 ]; then
        return 0
    fi
    RZFZ_STOPPING=1
    echo "[coding-agent] $1 received — stopping continuum's periodic save so a dying tmux server cannot write an empty save"
    tmux_q set-option -g @continuum-save-interval 0 2>/dev/null || true
    stop_tmux_save_loop
    if tmux_save_now "${RZFZ_TMUX_STOP_SAVE_TIMEOUT:-5}"; then
        echo "[coding-agent] saved tmux sessions before stopping: $(tmux_q list-sessions -F '#{session_name}' 2>/dev/null | tr '\n' ' ')"
    else
        echo "[coding-agent] no valid tmux save on stop (no session, no pane record, or not done within ${RZFZ_TMUX_STOP_SAVE_TIMEOUT:-5}s)"
    fi
    tmux_keep_last
}
trap 'on_container_signal TERM' TERM
trap 'on_container_signal INT' INT

supervise_web_ui() {
    # Self-sufficient: RZFZ_STOPPING is defined OUTSIDE this function, but the
    # test harness lifts the function alone and runs it under a bare /bin/sh,
    # so every read defaults to 0 — `[ "" -eq 0 ]` would error and the loop
    # would never start.
    restarts=0
    backoff="${WEB_UI_BACKOFF_START}"
    while [ "${RZFZ_STOPPING:-0}" -eq 0 ]; do
        started=$(date +%s)
        # `|| rc=$?` is load-bearing, not style. This script runs under `set -e`
        # (line 19), so a BARE `run_web_ui` that exits non-zero terminates the
        # whole entrypoint before the next line can even read `$?` — the
        # container then exits, `restart_policy: unless-stopped` bounces it, and
        # the user's tmux sessions die. That is precisely the outcome this
        # supervisor exists to avoid, and it is what happened on 0.91: killing
        # the UI restarted the CONTAINER (`Up 3 seconds`, `tmux ls` → no server)
        # instead of just the UI.
        #
        # The UI runs in the BACKGROUND and is waited on, so a trapped signal
        # interrupts `wait` at once (#3238); a bare foreground call would defer
        # the trap until the UI exits, and the container would be SIGKILLed
        # with the tmux server mid-teardown.
        rc=0
        run_web_ui &
        UI_PID=$!
        wait "${UI_PID}" || rc=$?
        if [ "${RZFZ_STOPPING:-0}" -eq 1 ]; then
            kill "${UI_PID}" 2>/dev/null || true
            wait "${UI_PID}" 2>/dev/null || true
            echo "[coding-agent] stopping on signal — web UI halted, container exits"
            exit 0
        fi
        ran_for=$(( $(date +%s) - started ))
        restarts=$(( restarts + 1 ))
        # Reset the backoff when the UI had actually been serving: an agent that
        # ran for a week must not wait out a capped delay on its first crash.
        if [ "${ran_for}" -ge "${WEB_UI_HEALTHY_SECONDS}" ]; then
            backoff="${WEB_UI_BACKOFF_START}"
        fi
        echo "[coding-agent] web UI exited (rc=${rc}) after ${ran_for}s — restart #${restarts} in ${backoff}s"
        # The backoff wait is INTERRUPTIBLE (#3238): each 1s slice runs in the
        # BACKGROUND on `wait`, so a trapped TERM/INT interrupts the slice at
        # once, and RZFZ_STOPPING is re-checked between slices. A foreground
        # `sleep` would make dash defer the trap until the sleep ended, and
        # with the shipped 30s ceiling a container stop could outrun its grace
        # period with continuum's periodic save still enabled; a stopping
        # supervisor exits instead of restarting the UI.
        slept=0
        while [ "${RZFZ_STOPPING:-0}" -eq 0 ] && \
              [ "$(awk -v s="${slept}" -v b="${backoff}" 'BEGIN { print (s < b) ? 1 : 0 }')" = "1" ]; do
            sleep 1 &
            BACKOFF_PID=$!
            wait "${BACKOFF_PID}" 2>/dev/null || true
            slept=$(( slept + 1 ))
        done
        # Grow towards the ceiling so a UI that can NEVER start (bad config,
        # port taken) does not spin as fast as the CPU allows — that would turn
        # this self-healing measure into the outage.
        backoff=$(awk -v b="${backoff}" -v m="${WEB_UI_BACKOFF_MAX}" \
                       'BEGIN { b = b * 2; if (b > m) b = m; print b }')
    done
    # The loop only exits when RZFZ_STOPPING is set: a signal arrived during
    # the backoff — exit instead of restarting the UI.
    if [ "${RZFZ_STOPPING:-0}" -eq 1 ]; then
        echo "[coding-agent] stopping on signal — web UI halted, container exits"
        exit 0
    fi
}

echo "[coding-agent] kind=${AGENT_KIND} label=${AGENT_LABEL:-$AGENT_KIND}"
echo "[coding-agent] Starting web UI on port ${CODING_TOOLS_WEB_PORT:-3004}..."
echo "[coding-agent] Container ready (user=$(id -un), uid=$(id -u))."
supervise_web_ui
