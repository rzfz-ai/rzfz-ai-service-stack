# ==============================================================================
# coding-agent split image (#36) — ONE parametrized Dockerfile, per-TYPE build.
#
# The agent split ships four per-type images that all share this Dockerfile,
# the shared Flask web terminal (web/), the tmux persistence config (tmux.conf)
# and the shared entrypoint (entrypoint.sh). The specific agent installed and
# its identity are selected by build args:
#
#   AGENT_KIND=opencode      → opencode-ai        (opencode-image builder)
#   AGENT_KIND=gsd-pi        → gsd-pi             (gsd-pi-image builder)
#   AGENT_KIND=codex         → @openai/codex      (codex-image builder; Apache-2.0)
#   AGENT_KIND=user-defined  → nothing (bare)     (user-defined-image builder)
#
# Each agents/compose.yml builder passes the matching AGENT_KIND + install args.
# Persistence: tmux-resurrect + tmux-continuum are vendored at /opt/tmux-plugins
# and driven by tmux.conf; the provisioner mounts per-user named volumes over
# /workspace + /home/agent so repos, installed tools, config and tmux resurrect
# saves survive a container restart.
#
# Base: node:24-bookworm-slim (node needed for opencode/codex/gsd npm installs)
# User: agent (uid 1000) — not root
# ==============================================================================

FROM node:24-bookworm-slim

ARG AGENT_KIND=coding-tools
ARG AGENT_LABEL=Coding
ARG AGENT_LAUNCH_CMD=bash
# npm pins (opencode/gsd/codex). Empty for user-defined. Bump via the
# check-and-bump-versions skill — never re-pin to "latest".
#
# These defaults are the reproducible floor: the agents/compose.yml builders
# override them from .env (${GSD_PI_VERSION:-...} etc), but a bare `docker build`
# of this context must still install a KNOWN version, never "whatever npm serves
# now". Pinned 2026.07 (#36 / PR #84):
#   codex 0.142.5    — current @openai/codex `latest` (0.x scheme, not an alpha);
#                      needs the Responses→Chat shim (entrypoint) + config.toml.
#   opencode 1.17.13 — current opencode-ai `latest`; same 1.17.x TUI series as
#                      the known-good 1.17.5 (post noexec-tmpfs fix), verified.
#   gsd-pi           — #36 FEATURE 2 (PR #84): gsd-pi is RETIRED as a dedicated
#                      agent, so NO builder passes GSD_PI_VERSION and the default
#                      is now EMPTY — gsd is NOT baked into any split image
#                      (opencode/codex/user-defined). gsd-pi is instead a User
#                      Defined Agent install suggestion (`npm install -g
#                      @opengsd/gsd-pi`, CLI `gsd`), installed by the user under
#                      the writable ~/.npm-global prefix (see entrypoint.sh) and
#                      persisted on the per-user home volume. Passing a non-empty
#                      GSD_PI_VERSION still bakes @opengsd/gsd-pi (kept so the
#                      legacy `gsd`/`~/.gsd/**` entrypoint config path — and any
#                      future re-introduction — remains one build-arg away).
ARG GSD_PI_VERSION=
ARG OPENCODE_VERSION=1.18.25
ARG CODEX_VERSION=0.151.0
# tmux plugin pins (vendored — no runtime network).
ARG TMUX_RESURRECT_VERSION=v4.0.0
ARG TMUX_CONTINUUM_VERSION=v3.1.0

# ── Extra system (apt) packages baked at BUILD time (#212) ────────────────────
# The sandbox runs NON-ROOT on a READ-ONLY root fs with NO sudo (hostile-container
# hardening), so `apt install` can NEVER work at runtime. Userspace tools install
# sudo-less into the persistent home volume already (npm -g → ~/.npm-global,
# pip --user → ~/.local, cargo → ~/.cargo, conda/micromamba); the one gap is
# SYSTEM LIBRARIES (apt), which must be baked while we're still root at build time.
# This is the supported knob for that — no hand-editing this Dockerfile: append
# package names to USER_DEFINED_APT_PACKAGES in .env, rebuild (rzfz post-install
# --refresh), then re-provision from My Agents. The running sandbox stays hardened
# (build-time root only). Default = the Playwright/Chromium runtime lib set
# (bookworm names) so headless-browser automation works out of the box —
# `pip install --user playwright && playwright install chromium` just works.
# Adding a package is one more name on this one .env line.
ARG USER_DEFINED_APT_PACKAGES="libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libxshmfence1 libx11-6 libxcb1 libxext6 libxi6 fonts-liberation"

ENV AGENT_KIND=${AGENT_KIND}
ENV AGENT_LABEL=${AGENT_LABEL}
ENV AGENT_LAUNCH_CMD=${AGENT_LAUNCH_CMD}

# ── Bring-your-own-tool install prefixes (#36 / PR #84) ───────────────────────
# The sandbox runs with a READ-ONLY root fs (correct — hostile-container
# hardening in the provisioner). npm's default global prefix is /usr/local
# (read-only), so the documented `npm install -g @openai/codex` flow — the whole
# point of the User Defined Agent (bring + persist your own agent) — fails with
# EROFS. Only /home/agent + /workspace are writable AND persistent (per-user
# named volumes). So we repoint every common package manager's install location
# under /home/agent and put their bins on PATH. Baked here (image env) so it
# applies to the entrypoint's tmux server → every terminal shell; the entrypoint
# additionally seeds a ~/.bashrc snippet on the persistent volume so interactive
# login shells (and existing volumes that pre-date this fix) get it too.
#   * npm:   NPM_CONFIG_PREFIX → global installs land in ~/.npm-global (npm
#            cache ~/.npm is already persistent).
#   * pip:   `pip install --user` → ~/.local (persistent); ~/.local/bin on PATH.
#            (We do NOT bake PIP_USER=1 as a build ENV — it makes pip refuse to
#            install into the image's /opt/webui-venv virtualenv. The entrypoint
#            sets PIP_USER=1 at RUNTIME for the user's terminal instead, which
#            never touches the venv pip. The system python3 honours --user.)
#   * cargo: CARGO_HOME → `cargo install` lands in ~/.cargo (codex is also a Rust
#            binary; future-proofs "any agent").
# We do NOT make /usr/local writable — every other sandbox control is unchanged.
ENV NPM_CONFIG_PREFIX=/home/agent/.npm-global
ENV CARGO_HOME=/home/agent/.cargo
ENV PATH=/home/agent/.npm-global/bin:/home/agent/.local/bin:/home/agent/.cargo/bin:$PATH

# System deps (git, tmux for session persistence, python for the web UI).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates procps tmux \
    python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Rename the base image's node user (uid 1000) → agent.
RUN usermod -l agent -d /home/agent -m node && groupmod -n agent node

# Python web UI deps.
RUN python3 -m venv /opt/webui-venv \
    && /opt/webui-venv/bin/pip install --no-cache-dir flask flask-sock ptyprocess \
    # #257 (careless-kill class): the sandbox control services (web UI +
    # local-model shim) run under a DISTINCTLY NAMED interpreter. An agent's
    # broad `pkill python3` / `pkill -f python3` (observed live: it reaped
    # app.py + the shim, #255/#257) no longer matches — comm becomes
    # `rzfz-webui` and the cmdline carries no `python3` substring. This blocks
    # the ACCIDENT class only; a same-uid agent can still kill by exact
    # name/pid (full uid/PID separation is tracked in #257, options 2/4).
    && ln -s python3 /opt/webui-venv/bin/rzfz-webui

# Vendor the tmux persistence plugins (resurrect + continuum) into the image.
RUN mkdir -p /opt/tmux-plugins \
    && git clone --depth 1 --branch ${TMUX_RESURRECT_VERSION} \
        https://github.com/tmux-plugins/tmux-resurrect /opt/tmux-plugins/tmux-resurrect \
    && git clone --depth 1 --branch ${TMUX_CONTINUUM_VERSION} \
        https://github.com/tmux-plugins/tmux-continuum /opt/tmux-plugins/tmux-continuum \
    && rm -rf /opt/tmux-plugins/*/.git

# Install the per-type coding agent (skipped when the version arg is empty).
RUN set -eux; \
    PKGS=""; \
    [ -n "${GSD_PI_VERSION}" ]   && PKGS="${PKGS} @opengsd/gsd-pi@${GSD_PI_VERSION}"; \
    [ -n "${OPENCODE_VERSION}" ] && PKGS="${PKGS} opencode-ai@${OPENCODE_VERSION}"; \
    [ -n "${CODEX_VERSION}" ]    && PKGS="${PKGS} @openai/codex@${CODEX_VERSION}"; \
    if [ -n "${PKGS}" ]; then npm install -g ${PKGS} && npm cache clean --force; fi

# Codex is Apache-2.0 — ship the license + OpenAI notice in the image (#36).
COPY licenses/ /opt/licenses/

# Workspace + persisted home dirs (created here so volume mounts inherit
# ownership on first run).
RUN mkdir -p /workspace \
             /home/agent/.gsd \
             /home/agent/.config/opencode \
             /home/agent/.config/razzfazz-agent \
             /home/agent/.codex \
             /home/agent/.local/share/opencode \
             /home/agent/.local/state/opencode \
             /home/agent/.cache/opencode \
             /home/agent/.local/share/tmux/resurrect \
             /home/agent/.npm-global/bin \
             /home/agent/.local/bin \
             /home/agent/.cargo/bin \
    && chown -R agent:agent /workspace /home/agent

WORKDIR /workspace

# Shared web UI, tmux config, entrypoint.
COPY web/ /opt/coding-agent-web/
COPY tmux.conf /home/agent/.tmux.conf
# Local-model shim (#36 / PR #84). Makes the bundled CLIs run against GPUStack:
# codex (Responses→Chat translation — codex dropped wire_api="chat") + opencode
# and gsd-pi (chat-completions passthrough that injects enable_thinking=false so
# the qwen3.x thinking model doesn't burn the budget and hang). The entrypoint
# starts it on 127.0.0.1 and points each CLI's config at it. Stdlib only — runs
# on the same webui venv python.
COPY local_model_shim.py /opt/coding-agent-web/local_model_shim.py
COPY entrypoint.sh /entrypoint.sh
RUN chown -R agent:agent /opt/coding-agent-web /home/agent/.tmux.conf \
    && chmod +x /entrypoint.sh

# Bake extra system (apt) packages while still root — see USER_DEFINED_APT_PACKAGES
# ARG above. Runs AFTER all image apt work and BEFORE the drop to `agent`, so the
# running sandbox stays hardened (build-time root only; no runtime apt/sudo).
RUN if [ -n "${USER_DEFINED_APT_PACKAGES}" ]; then \
        apt-get update && apt-get install -y --no-install-recommends ${USER_DEFINED_APT_PACKAGES} \
        && rm -rf /var/lib/apt/lists/*; \
    fi

EXPOSE 3004

# #255 — Docker must be able to see the difference between "up" and "serving".
# Without this a wedged agent (UI dead, container Up) reads as healthy to every
# tool that looks at container state, which is exactly how it went unnoticed on
# prod. Shell form on purpose: the port is configurable at runtime via
# CODING_TOOLS_WEB_PORT, and a hardcoded 3004 would report a perfectly healthy
# container as unhealthy the moment an operator moves it.
#
# The entrypoint's supervisor is the primary recovery path — this is the signal,
# not the cure. start-period covers first boot, where the CLIs' runtimes unpack
# before the UI binds.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${CODING_TOOLS_WEB_PORT:-3004}/health" || exit 1

USER agent
ENTRYPOINT ["/entrypoint.sh"]
