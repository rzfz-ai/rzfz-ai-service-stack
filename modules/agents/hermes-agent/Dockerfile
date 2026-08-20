# ==============================================================================
# hermes-agent — built from upstream source (rc6.7 #56; refreshed for v0.18.0)
#
# Why we build instead of pulling ghcr.io/nousresearch/hermes-agent:
# the GHCR image is private (HTTP 403 for anonymous pulls), but the source
# is fully public on GitHub at github.com/NousResearch/hermes-agent and the
# release tags are stable. Building locally avoids GHCR PAT distribution
# across the SEQIS Group fleet and matches the pattern coding-tools / paperclip
# already use.
#
# This Dockerfile mirrors the upstream Dockerfile inline (instead of copying
# it verbatim) so we have one single source of truth. If upstream changes
# their build, bump HERMES_AGENT_VERSION and adjust the RUN steps below to
# match. The upstream Dockerfile reference is at
# https://github.com/NousResearch/hermes-agent/blob/<TAG>/Dockerfile
#
# ── v0.18.0 ("The Judgment Release", tag v2026.7.1) — MAJOR build changes ─────
# The v0.16→v0.18 line reorganized the container substantially. This Dockerfile
# was re-derived from the v2026.7.1 upstream Dockerfile:
#   * s6-overlay REPLACES tini as PID 1. `/init` supervises a `dashboard`
#     s6 service + a no-op `main-hermes` slot; the container CMD runs as
#     /init's "main program" via docker/main-wrapper.sh (Architecture B).
#   * Node 22 LTS is COPIED from node:22-bookworm-slim (Debian trixie's node
#     20 reached EOL 2026-04) — Debian's nodejs is NOT installed.
#   * Python deps install via `uv sync --frozen` with a handpicked extras set
#     (NOT `uv pip install -e ".[all]"`); provider + matrix + hindsight extras
#     are baked so the sealed /opt/hermes venv needs no runtime lazy-install.
#   * Frontend build inputs changed: there is NO web/package-lock.json or
#     ui-tui/package-lock.json in the tree; only the ROOT package-lock.json.
#     apps/shared/ is a new `file:` workspace dep referenced by web/.
#   * New system deps: g++ make cmake libolm-dev (python-olm/matrix builds
#     from source), python-is-python3, xz-utils, iputils-ping.
#   * The dashboard's auth gate is now MANDATORY on a non-loopback bind
#     (June-2026 hardening): HERMES_DASHBOARD_INSECURE is accepted but IGNORED.
#     The per-user catalog entry supplies HERMES_DASHBOARD_BASIC_AUTH_* to
#     satisfy it (see modules/agents/manager/app/services/catalog.py).
#
# Image is tagged razzfazz-stack-hermes-agent:<HERMES_AGENT_VERSION>; the
# agent-manager catalog points at this name + tag.
# ==============================================================================

# --- pin upstream third-party stages exactly as upstream does ---
FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie@sha256:b3c543b6c4f23a5f2df22866bd7857e5d304b67a564f4feab6ac22044dde719b AS uv_source
# Node 22 LTS source stage (bookworm-slim → glibc 2.36, runs on our trixie).
FROM node:22-bookworm-slim@sha256:7af03b14a13c8cdd38e45058fd957bf00a72bbe17feac43b1c15a689c029c732 AS node_source

# --- clone upstream source at the pinned tag ---
FROM debian:13.6 AS clone
ARG HERMES_AGENT_VERSION=v2026.8.3
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${HERMES_AGENT_VERSION} \
        https://github.com/NousResearch/hermes-agent /src && \
    rm -rf /src/.git

# --- final image (mirrors upstream Dockerfile from the tag above) ---
FROM debian:13.6
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright

# ── Extra system (apt) packages baked at BUILD time (#212) ────────────────────
# See modules/agents/coding-agent-web/Dockerfile for the full rationale: per-user
# agents run non-root with no runtime sudo, so `apt install` only works at build
# time. This knob bakes extra libs without editing the Dockerfile — append names
# to USER_DEFINED_APT_PACKAGES in .env, rebuild (rzfz post-install --refresh),
# re-provision. Default = the Playwright/Chromium runtime lib set. NOTE the TRIXIE
# names: the t64 time_t transition renamed libcups2→libcups2t64, libasound2→
# libasound2t64, libatk1.0-0→libatk1.0-0t64, libatk-bridge2.0-0→libatk-bridge2.0-0t64,
# libatspi2.0-0→libatspi2.0-0t64 (the bookworm images use the un-suffixed names).
# hermes also runs `playwright install --with-deps chromium` below, which already
# pulls these; the default keeps the knob consistent and is a no-op when satisfied.
# The final image legitimately stays root (s6-overlay /init is PID 1 and drops to
# the hermes user via main-wrapper.sh) — the apt RUN below is the last root step.
ARG USER_DEFINED_APT_PACKAGES="libnss3 libnspr4 libdbus-1-3 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 libxkbcommon0 libatspi2.0-0t64 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 libxshmfence1 libx11-6 libxcb1 libxext6 libxi6 fonts-liberation"

# System deps (v0.18: adds g++ make cmake libolm-dev python-is-python3
# xz-utils iputils-ping; drops the Debian nodejs/npm — Node comes from the
# node_source stage below).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl iputils-ping python3 python-is-python3 ripgrep \
        ffmpeg gcc g++ make cmake python3-dev python3-venv libffi-dev libolm-dev \
        procps git openssh-client docker-cli xz-utils && \
    rm -rf /var/lib/apt/lists/*

# ---------- s6-overlay install (PID 1 = /init) ----------
# Checksum-verified per upstream. Single-arch (amd64) — the SEQIS fleet is
# x86_64; upstream's multi-arch TARGETARCH mapping is collapsed to amd64 here.
ARG S6_OVERLAY_VERSION=3.2.3.0
ARG S6_OVERLAY_NOARCH_SHA256=b720f9d9340efc8bb07528b9743813c836e4b02f8693d90241f047998b4c53cf
ARG S6_OVERLAY_X86_64_SHA256=a93f02882c6ed46b21e7adb5c0add86154f01236c93cd82c7d682722e8840563
ARG S6_OVERLAY_SYMLINKS_SHA256=a60dc5235de3ecbcf874b9c1f18d73263ab99b289b9329aa950e8729c4789f0e
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-symlinks-noarch.tar.xz /tmp/
RUN set -eu; \
    curl -fsSL --retry 3 -o /tmp/s6-overlay-arch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz"; \
    { \
        printf '%s  %s\n' "${S6_OVERLAY_NOARCH_SHA256}" /tmp/s6-overlay-noarch.tar.xz; \
        printf '%s  %s\n' "${S6_OVERLAY_X86_64_SHA256}" /tmp/s6-overlay-arch.tar.xz; \
        printf '%s  %s\n' "${S6_OVERLAY_SYMLINKS_SHA256}" /tmp/s6-overlay-symlinks-noarch.tar.xz; \
    } > /tmp/s6-overlay.sha256; \
    sha256sum -c /tmp/s6-overlay.sha256; \
    tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-overlay-arch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-overlay-symlinks-noarch.tar.xz; \
    rm /tmp/s6-overlay-*.tar.xz /tmp/s6-overlay.sha256; \
    ln -sf /init /usr/bin/tini

# Non-root user; UID can be overridden via HERMES_UID at runtime.
RUN useradd -u 10000 -m -d /opt/data hermes

COPY --chmod=0755 --from=uv_source /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/

# Node 22 LTS: copy node + bundled npm/corepack from the node_source stage.
COPY --chmod=0755 --from=node_source /usr/local/bin/node /usr/local/bin/
COPY --from=node_source /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=node_source /usr/local/lib/node_modules/corepack /usr/local/lib/node_modules/corepack
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    ln -sf /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack

WORKDIR /opt/hermes

# Force npm to symlink `file:` deps (npm 10+ default; explicit for safety).
ENV npm_config_install_links=false

# ---------- Layer-cached npm dependency install ----------
# v0.18: only the ROOT package-lock.json exists; web/ and ui-tui/ have NO
# lockfiles. ui-tui/packages/hermes-ink/ and apps/shared/ are `file:` workspace
# deps and must be copied IN FULL before `npm install` can resolve them.
COPY --from=clone /src/package.json /src/package-lock.json ./
COPY --from=clone /src/web/package.json web/
COPY --from=clone /src/ui-tui/package.json ui-tui/
COPY --from=clone /src/ui-tui/packages/hermes-ink/ ui-tui/packages/hermes-ink/
COPY --from=clone /src/apps/shared/ apps/shared/

RUN npm install --prefer-offline --no-audit && \
    npx playwright install --with-deps chromium --only-shell && \
    npm cache clean --force

# ---------- Layer-cached Python dependency install ----------
# v0.18: `uv sync --frozen` with the handpicked extras set (NOT `.[all]`).
# README.md is referenced by pyproject.toml but excluded by .dockerignore —
# touch a placeholder so uv's build frontend can stat it (restored by COPY . .).
COPY --from=clone /src/pyproject.toml /src/uv.lock ./
RUN touch ./README.md
RUN uv sync --frozen --no-install-project \
        --extra all --extra messaging --extra anthropic --extra bedrock \
        --extra azure-identity --extra hindsight --extra matrix

# ---------- Frontend build ----------
COPY --from=clone /src/web/ web/
COPY --from=clone /src/ui-tui/ ui-tui/
COPY --from=clone /src/apps/shared/ apps/shared/
RUN cd web && npm run build && \
    cd ../ui-tui && npm run build

# ---------- Source code ----------
# node_modules is excluded from the COPY mask by the clone's tree (we only
# copied manifests + built dirs above; the full source overlays them now).
# chown to hermes so any runtime read/traverse works; root retains write.
COPY --from=clone --chown=hermes:hermes /src/ ./

# Editable link (deps already installed above; --no-deps = fast egg-link).
RUN uv pip install --no-cache-dir --no-deps -e "."

# #36 Option B: Host-rewriting reverse proxy for the loopback-bound dashboard.
# Bind the dashboard on 127.0.0.1 (auth gate = no-op) and front it with this
# stdlib proxy on 0.0.0.0:9119 so the agent-manager proxy reaches it over
# docker DNS without tripping the dashboard's DNS-rebind Host check. Started by
# the catalog boot command. See catalog.py HERMES_DASHBOARD_* env note.
COPY hermes-dash-proxy.py /opt/hermes/docker/hermes-dash-proxy.py
RUN chmod 0755 /opt/hermes/docker/hermes-dash-proxy.py

# Exec shim + install-method stamp (mirrors upstream).
USER root
RUN mkdir -p /opt/hermes/bin && \
    cp /opt/hermes/docker/hermes-exec-shim.sh /opt/hermes/bin/hermes && \
    chmod 0755 /opt/hermes/bin/hermes && \
    printf 'docker\n' > /opt/hermes/.install_method && \
    chmod -R a+rX /opt/hermes && \
    chown -R hermes:hermes /opt/hermes/ui-tui /opt/hermes/node_modules

# ---------- s6-overlay service wiring ----------
# Static services: main-hermes (no-op slot) + dashboard (enabled by
# HERMES_DASHBOARD=1). Per-profile gateways register dynamically at runtime.
COPY --from=clone /src/docker/s6-rc.d/ /etc/s6-overlay/s6-rc.d/
RUN mkdir -p /etc/cont-init.d && \
    printf '#!/command/with-contenv sh\nexec /opt/hermes/docker/stage2-hook.sh\n' \
        > /etc/cont-init.d/01-hermes-setup && \
    chmod +x /etc/cont-init.d/01-hermes-setup && \
    cp /opt/hermes/docker/cont-init.d/015-supervise-perms /etc/cont-init.d/015-supervise-perms && \
    cp /opt/hermes/docker/cont-init.d/02-reconcile-profiles /etc/cont-init.d/02-reconcile-profiles && \
    chmod 0755 /etc/cont-init.d/015-supervise-perms /etc/cont-init.d/02-reconcile-profiles

# ---------- Runtime env (mirrors upstream v0.18) ----------
ENV HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
ENV HERMES_TUI_DIR=/opt/hermes/ui-tui
ENV HERMES_HOME=/opt/data
ENV HERMES_WRITE_SAFE_ROOT=/opt/data
ENV HERMES_DISABLE_LAZY_INSTALLS=1
ENV HERMES_LAZY_INSTALL_TARGET=/opt/data/lazy-packages
ENV PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:${PATH}"
RUN mkdir -p /opt/data

# Bake extra system (apt) packages — see USER_DEFINED_APT_PACKAGES ARG at the top
# of this stage. Last root step before the s6 /init ENTRYPOINT; the running
# container still drops to the hermes user (build-time root only; no runtime apt).
RUN if [ -n "${USER_DEFINED_APT_PACKAGES}" ]; then \
        apt-get update && apt-get install -y --no-install-recommends ${USER_DEFINED_APT_PACKAGES} \
        && rm -rf /var/lib/apt/lists/*; \
    fi

VOLUME [ "/opt/data" ]

# s6-overlay /init is PID 1; main-wrapper.sh runs the container CMD as the
# main program (drops to the hermes user, routes args). See upstream rationale.
ENTRYPOINT [ "/init", "/opt/hermes/docker/main-wrapper.sh" ]
CMD [ ]
